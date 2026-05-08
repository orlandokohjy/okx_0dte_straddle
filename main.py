"""
OKX 0DTE BTC Pure Straddle Algo.

Single daily session: 12:00–16:00 UTC, Mon–Fri.
Position: 1 ITM call + 1 put (same strike) per QTY_PER_LEG BTC.
Compound sizing: 80% of current equity, override default = 1 straddle.
Maker-only orders with 50%-gap-narrow chase, BOTH legs fired concurrently.

Default mode: Demo Trading (OKX_FLAG=1) + DRY_RUN=true. Set both to "0"/"false"
in .env when ready for live.
"""
from __future__ import annotations

import asyncio
import atexit
import errno
import os
import re
import signal
import sys

import structlog

import config
from core import notifier
from core.exchange import OKXExchange
from core.portfolio import Portfolio
from core.scheduler import Scheduler
from data.market_data import MarketData
from data.option_chain import OptionChain
from risk.risk_manager import RiskManager
from strategy.exit_manager import ExitManager
from strategy.option_selector import select_straddle_pair
from strategy.position_sizer import size_position
from strategy.straddle_builder import build_straddle, unwind_straddle
from utils import volume_tracker
from utils.logging_config import setup_logging
from utils.time_utils import format_utc_sgt, now_utc

log = structlog.get_logger(__name__)


def _disable_entry_now_in_env_file(env_path: str = ".env") -> None:
    """Rewrite ENTRY_NOW=true to ENTRY_NOW=false in the local .env file."""
    try:
        if not os.path.exists(env_path):
            log.debug("entry_now_disable_skipped", reason="no_env_file")
            return
        with open(env_path, "r") as f:
            content = f.read()
        new_content = re.sub(
            r"^(\s*ENTRY_NOW\s*=\s*)(true|TRUE|True|1)\b.*$",
            r"\1false",
            content,
            flags=re.MULTILINE,
        )
        if new_content != content:
            with open(env_path, "w") as f:
                f.write(new_content)
            log.info("entry_now_auto_disabled", env_path=env_path)
        else:
            log.debug("entry_now_disable_noop", reason="no_match")
    except Exception:
        log.warning("entry_now_disable_failed", exc_info=True)


def _disable_reset_state_in_env_file(env_path: str = ".env") -> None:
    """Rewrite RESET_STATE_ON_BOOT=true → false. Same auto-disable pattern as
    ENTRY_NOW so a container restart never silently wipes state twice."""
    try:
        if not os.path.exists(env_path):
            return
        with open(env_path, "r") as f:
            content = f.read()
        new_content = re.sub(
            r"^(\s*RESET_STATE_ON_BOOT\s*=\s*)(true|TRUE|True|1)\b.*$",
            r"\1false",
            content,
            flags=re.MULTILINE,
        )
        if new_content != content:
            with open(env_path, "w") as f:
                f.write(new_content)
            log.info("reset_state_auto_disabled", env_path=env_path)
    except Exception:
        log.warning("reset_state_auto_disable_failed", exc_info=True)


def _reset_local_state() -> None:
    """Delete state/equity.json + state/positions.json. Caller is responsible
    for the ENTRY_NOW-style auto-disable so this only runs once."""
    removed: list[str] = []
    for path in (config.EQUITY_FILE, config.POSITIONS_FILE):
        try:
            if os.path.exists(path):
                os.remove(path)
                removed.append(path)
        except Exception:
            log.warning("state_reset_unlink_failed", path=path, exc_info=True)
    log.info("state_reset_done", removed=removed)


# ── Single-instance lock ────────────────────────────────────────────────
#
# Two algo instances pointing at the same OKX API key will race each other
# to place / cancel orders, which is exactly the failure pattern that
# created the 2026-05-07 orphan put: a stray `sharp_brattain` container
# was running alongside the `docker-compose` instance and both were
# competing on `BTC-USD-...-P` orders. The result was a real fill that
# neither algo successfully matched to its own state.
#
# Implementation: a PID lock file at state/algo.pid. On startup we check
# whether the PID inside is still alive; if it is, we refuse to start.
# If it's a stale file (process gone), we overwrite it. The file is
# atexit-cleaned, plus removed by signal handlers on graceful shutdown.

_LOCK_PATH = f"{config.STATE_DIR}/algo.pid"


def _process_alive(pid: int) -> bool:
    """True if a process with this PID exists. Cross-platform-ish."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM  # exists but we can't signal it
    return True


def _acquire_singleton_lock() -> None:
    """Refuse to start if another algo with a live PID is already running."""
    try:
        os.makedirs(config.STATE_DIR, exist_ok=True)
    except Exception:
        log.warning("state_dir_mkdir_failed", path=config.STATE_DIR,
                    exc_info=True)

    if os.path.exists(_LOCK_PATH):
        try:
            with open(_LOCK_PATH, "r") as f:
                existing_pid = int(f.read().strip() or "0")
        except Exception:
            existing_pid = 0
        if existing_pid > 0 and existing_pid != os.getpid() \
                and _process_alive(existing_pid):
            log.error("singleton_lock_busy",
                      lock_path=_LOCK_PATH,
                      existing_pid=existing_pid,
                      current_pid=os.getpid(),
                      hint="another algo instance is running with the same "
                           "API keys — kill it before starting this one")
            sys.stderr.write(
                f"REFUSED TO START: another instance (pid={existing_pid}) "
                f"holds {_LOCK_PATH}.\n"
                f"If you are sure no other algo is running, delete "
                f"{_LOCK_PATH} and retry.\n"
            )
            sys.exit(2)
        if existing_pid > 0:
            log.info("singleton_lock_stale_overwrite",
                     stale_pid=existing_pid)

    try:
        with open(_LOCK_PATH, "w") as f:
            f.write(str(os.getpid()))
        log.info("singleton_lock_acquired",
                 lock_path=_LOCK_PATH, pid=os.getpid())
        atexit.register(_release_singleton_lock)
    except Exception:
        log.warning("singleton_lock_write_failed",
                    path=_LOCK_PATH, exc_info=True)


def _release_singleton_lock() -> None:
    """Best-effort lock-file cleanup. Safe to call multiple times."""
    try:
        if not os.path.exists(_LOCK_PATH):
            return
        with open(_LOCK_PATH, "r") as f:
            owner = int(f.read().strip() or "0")
        if owner == os.getpid():
            os.remove(_LOCK_PATH)
            log.info("singleton_lock_released", lock_path=_LOCK_PATH)
    except Exception:
        log.debug("singleton_lock_release_failed", exc_info=True)


class Algo:
    def __init__(self) -> None:
        self.exchange = OKXExchange()
        self.chain = OptionChain(self.exchange)
        self.market = MarketData(self.exchange, self.chain)
        self.portfolio = Portfolio()
        self.risk = RiskManager(self.portfolio)
        self.exit_mgr = ExitManager(
            self.exchange, self.market, self.portfolio,
        )
        self.scheduler = Scheduler()
        self._shutdown = asyncio.Event()
        self._entry_locked: bool = False
        self._lock_reason: str = ""
        self._consecutive_failures: int = 0

    async def start(self) -> None:
        setup_logging()
        mode = "DEMO" if config.OKX_FLAG == "1" else "LIVE"

        # Refuse to start if another algo instance is already running with
        # the same API keys. Prevents the stray-container race that caused
        # the 2026-05-07 orphan put.
        _acquire_singleton_lock()

        # Optional one-shot state reset — wipe demo equity/positions before
        # connecting so we don't leak the $5,000 demo seed into a live boot.
        if config.RESET_STATE_ON_BOOT:
            _reset_local_state()
            _disable_reset_state_in_env_file()
            self.portfolio = Portfolio()  # reload from clean state
            self.risk = RiskManager(self.portfolio)
            self.exit_mgr = ExitManager(
                self.exchange, self.market, self.portfolio,
            )

        log.info("algo_starting", mode=mode, dry_run=config.DRY_RUN,
                 has_creds=config.HAS_OKX_CREDS,
                 reset_state=config.RESET_STATE_ON_BOOT)

        self.exchange.connect()

        # Prime per-instrument metadata (tick size, contract size). Public
        # endpoint, no auth required. Sets the runtime tick that chase_buy/
        # chase_sell use, replacing the old hardcoded OPTION_TICK_SIZE=5.0.
        try:
            await self.exchange.prime_option_tick_size()
        except Exception:
            log.warning("prime_option_tick_failed", exc_info=True)

        # Auth-required startup safeguards. Run whenever we HAVE credentials,
        # regardless of DRY_RUN — this lets a dry-run boot still validate the
        # auth path + balance fetch + position reconcile, catching bad keys
        # before we ever flip to live.
        if config.HAS_OKX_CREDS:
            await self._startup_cancel_stale_orders()
            await self._startup_reconcile_positions()

        spot = await self.exchange.get_spot_price()

        if config.HAS_OKX_CREDS:
            live_equity = await self.exchange.get_account_equity()
            if live_equity > 0:
                self.portfolio.sync_equity(live_equity)

        # Self-test: simulate a chase iteration on a real ITM option and
        # abort if the math produces nonsense (defends against unit-conv
        # regressions like the 2026-05-07 OPTION_TICK_SIZE=5.0 USD bug).
        ok = await self._chase_pricing_selftest(spot)
        if not ok:
            self._entry_locked = True
            self._lock_reason = (
                "Chase-pricing self-test failed — see logs. "
                "Tick size / unit conversion likely misconfigured."
            )

        log.info("algo_initialized",
                 spot=f"${spot:,.2f}",
                 equity=f"${self.portfolio.equity:,.2f}",
                 entry_locked=self._entry_locked,
                 tick_size=self.exchange.get_tick_size())

        lock_line = (
            f"\n<b>⚠️ ENTRY LOCKED</b>: {self._lock_reason}"
            if self._entry_locked else ""
        )
        await notifier.send(
            f"<b>OKX STRADDLE ALGO STARTED</b>\n"
            f"Mode: {mode}"
            f"{' (DRY RUN)' if config.DRY_RUN else ''}\n"
            f"Spot: ${spot:,.2f}\n"
            f"Equity: ${self.portfolio.equity:,.2f}\n"
            f"Time: {format_utc_sgt(now_utc())}"
            f"{lock_line}\n"
        )

        self.scheduler.register_session(
            on_entry=self._on_entry,
            on_close=self._on_close,
            on_report=self._on_report,
            on_weekly_report=self._on_weekly_report,
        )
        self.scheduler.start()

        fire_times = self.scheduler.get_next_fire_times()
        for job_id, ft in fire_times.items():
            if ft:
                log.info("next_fire", job=job_id, time=format_utc_sgt(ft))

        if os.getenv("ENTRY_NOW", "").lower() == "true":
            log.info("immediate_entry_triggered")
            _disable_entry_now_in_env_file()
            await self._on_entry()

        log.info("algo_running")
        await self._shutdown.wait()

    # ──────────────────── Startup Safeguards ──────────────────────

    async def _chase_pricing_selftest(self, spot: float) -> bool:
        """
        Simulate one chase_buy iteration on live ITM options and verify the
        resulting price is sensible. Catches unit-conversion regressions (the
        2026-05-07 incident where OPTION_TICK_SIZE=5.0 USD added 5 BTC to a
        BTC-quoted bid, producing 5.0055 BTC) BEFORE we ever route an order.

        We mirror production's behavior: chase_buy caps the price at
        ``mark × OPTION_CHASE_MAX_SLIPPAGE_FACTOR`` and skips placing an
        order if the proposed price exceeds that cap. So a wide / stale
        spread is NOT a math bug — it is correctly handled by the cap.
        We therefore evaluate the post-cap price for sanity. The absolute
        ceiling (≤0.5 BTC) remains the hard guard against unit-conversion
        bugs, since those bugs typically produce prices ≥1 BTC.

        Returns False only if the math itself is broken on every sample we
        try (signals a true unit-conversion regression). Caller locks entry
        on False.
        """
        try:
            count = await self.chain.refresh()
            if count == 0:
                log.warning("selftest_skipped_no_chain")
                return True  # not a math bug; just no data — don't block

            samples: list = []
            for c in self.chain.calls:
                if c.strike < spot and c.ask > 0:
                    samples.append(c)
            if not samples:
                for p in self.chain.puts:
                    if p.ask > 0:
                        samples.append(p)
                        if len(samples) >= 5:
                            break
            samples = samples[:5]
            if not samples:
                log.warning("selftest_skipped_no_quotes")
                return True

            last_failure = None
            for sample in samples:
                tick = self.exchange.get_tick_size(sample.symbol)
                if tick <= 0:
                    tick = config.OPTION_TICK_SIZE

                mark = await self.exchange.get_option_mark_price(sample.symbol)
                if mark <= 0:
                    mark = sample.mark if sample.mark > 0 else sample.ask

                bid = sample.bid if sample.bid > 0 else max(mark - tick, tick)
                ask = sample.ask
                target_top = max(bid, ask - tick)
                new_price = bid + (target_top - bid) * \
                    config.OPTION_CHASE_GAP_NARROW_PCT
                improvement_floor = bid + tick
                floor = bid if improvement_floor >= ask else improvement_floor
                ceiling = bid if (ask - tick) < bid else (ask - tick)
                new_price = max(min(new_price, ceiling), floor)
                pre_cap_price = new_price

                # Mirror production cap: chase_buy never sends an order
                # priced above mark × MAX_SLIPPAGE_FACTOR. With a wide /
                # stale spread the cap kicks in and the loop skips-and-waits
                # rather than firing a bad maker.
                cap = mark * config.OPTION_CHASE_MAX_SLIPPAGE_FACTOR \
                    if mark > 0 else float("inf")
                capped = min(new_price, cap)
                cap_engaged = pre_cap_price > cap

                ok_absolute = capped <= config.CHASE_SELFTEST_MAX_ABSOLUTE_BTC
                ok_positive = capped > 0
                ok_vs_mark = (
                    mark <= 0 or
                    capped <= mark * config.CHASE_SELFTEST_MAX_OVER_MARK
                )

                log.info("chase_selftest",
                         instrument=sample.symbol,
                         bid=bid, ask=ask, mark=mark, tick=tick,
                         pre_cap_price=pre_cap_price,
                         capped_price=capped,
                         cap_engaged=cap_engaged,
                         ok_vs_mark=ok_vs_mark,
                         ok_absolute=ok_absolute,
                         ok_positive=ok_positive)

                if ok_absolute and ok_positive and ok_vs_mark:
                    return True

                last_failure = (
                    sample, bid, ask, mark, tick, pre_cap_price, capped,
                )
                # Try the next sample — wide-spread / stale-quote samples
                # should not hold up startup if a healthier strike exists.

            sample, bid, ask, mark, tick, pre_cap, capped = last_failure
            log.error("chase_selftest_failed",
                      instrument=sample.symbol,
                      pre_cap_price=pre_cap, capped_price=capped,
                      mark=mark, tick=tick,
                      max_over_mark=config.CHASE_SELFTEST_MAX_OVER_MARK,
                      max_absolute=config.CHASE_SELFTEST_MAX_ABSOLUTE_BTC)
            await notifier.send(
                f"<b>STARTUP SELF-TEST FAILED</b>\n"
                f"Every sampled ITM option produced an out-of-bound price. "
                f"This is a likely unit-conversion bug, NOT a wide-spread "
                f"issue.\n"
                f"Last sample: {sample.symbol}\n"
                f"Bid/Ask: {bid:.4f} / {ask:.4f} BTC\n"
                f"Mark: {mark:.4f} BTC, tick: {tick:.4f}\n"
                f"Pre-cap price: {pre_cap:.4f} BTC, "
                f"post-cap: {capped:.4f} BTC\n"
                f"Entry will be LOCKED until restart with fix."
            )
            return False
        except Exception:
            log.error("chase_selftest_exception", exc_info=True)
            return True  # don't block on transient errors

    async def _startup_cancel_stale_orders(self) -> None:
        try:
            cancelled = await self.exchange.cancel_all_open_orders()
            if cancelled > 0:
                await notifier.send(
                    f"<b>STARTUP CLEANUP</b>\n"
                    f"Cancelled {cancelled} stale open order(s) "
                    f"from previous run."
                )
        except Exception:
            log.error("startup_cancel_failed", exc_info=True)
            await notifier.notify_error(
                "Startup cleanup",
                "Failed to cancel stale orders — check logs manually",
            )

    async def _startup_reconcile_positions(self) -> None:
        try:
            exchange_positions = await self.exchange.list_open_positions()
        except Exception:
            log.error("reconcile_fetch_failed", exc_info=True)
            self._entry_locked = True
            self._lock_reason = "Could not fetch positions from OKX"
            await notifier.notify_error(
                "Startup reconciliation",
                "Failed to fetch exchange positions — entries blocked",
            )
            return

        exchange_has_positions = len(exchange_positions) > 0
        local_has_straddle = self.portfolio.has_open

        log.info("startup_reconcile",
                 exchange_positions=len(exchange_positions),
                 exchange_detail=[
                     f"{p['instrument_name']} {p['amount']:+.4f}"
                     for p in exchange_positions
                 ],
                 local_has_straddle=local_has_straddle)

        if exchange_has_positions and not local_has_straddle:
            details = "\n".join(
                f"  • {p['instrument_name']}  amt={p['amount']:+.4f}  "
                f"avg=${p['average_price']:,.2f}  "
                f"mark=${p['mark_price']:,.2f}  "
                f"uPnL=${p['unrealized_pnl']:+,.2f}"
                for p in exchange_positions
            )
            self._entry_locked = True
            self._lock_reason = (
                f"Exchange has {len(exchange_positions)} open position(s) "
                f"but algo state is empty — possible orphan"
            )
            await notifier.send(
                f"<b>⚠️ RECONCILIATION MISMATCH</b>\n"
                f"Exchange has open positions but algo state is empty.\n\n"
                f"<b>Exchange positions:</b>\n{details}\n\n"
                f"<b>ACTION</b>: Entry locked until manually resolved.\n"
                f"Either close the positions or update positions.json.\n"
            )
            return

        if local_has_straddle and not exchange_has_positions:
            self._entry_locked = True
            self._lock_reason = (
                "Algo state has open straddle but exchange shows flat — "
                "stale positions.json"
            )
            await notifier.send(
                f"<b>⚠️ RECONCILIATION MISMATCH</b>\n"
                f"Algo state claims open straddle but exchange shows flat.\n\n"
                f"<b>ACTION</b>: Entry locked. Clear state/positions.json "
                f"to reset."
            )
            return

        log.info("startup_reconcile_ok",
                 flat=(not exchange_has_positions and not local_has_straddle),
                 matched_open=(
                     exchange_has_positions and local_has_straddle
                 ))

    # ──────────────────── Entry ───────────────────────────────────

    async def _on_entry(self) -> None:
        try:
            await self._run_entry()
        except Exception:
            log.error("entry_error", exc_info=True)
            await notifier.notify_error(
                "Entry", "Unhandled exception — check logs",
            )

    async def _run_entry(self) -> None:
        log.info("session_entry_start")

        if self._entry_locked:
            log.warning("entry_blocked_lock", reason=self._lock_reason)
            await notifier.notify_skip(
                f"Entry locked: {self._lock_reason}",
            )
            return

        api_check = self.risk.check_api_health(self.exchange.error_count)
        if not api_check.allowed:
            log.warning("entry_blocked_api", reason=api_check.reason)
            await notifier.notify_skip(api_check.reason)
            return

        loss_check = self.risk.check_daily_loss()
        if not loss_check.allowed:
            log.warning("entry_blocked_loss", reason=loss_check.reason)
            await notifier.notify_skip(loss_check.reason)
            return

        if self.portfolio.has_open:
            log.warning("already_has_open_straddle")
            return

        total_options = await self.chain.refresh()
        if total_options == 0:
            log.error("no_0dte_options")
            await notifier.notify_skip("No 0DTE options found on OKX")
            return

        spot = await self.exchange.get_spot_price()
        pair = select_straddle_pair(self.chain, spot)
        if pair is None:
            await notifier.notify_skip(
                f"No valid ITM call + put pair near spot ${spot:,.0f}",
            )
            return

        if config.HAS_OKX_CREDS:
            live_equity = await self.exchange.get_account_equity()
            if live_equity > 0:
                self.portfolio.sync_equity(live_equity)

        equity = self.portfolio.equity
        # Premium quotes are in BTC; sizer needs spot to compute USD costs.
        sizing = size_position(equity, pair.call.ask, pair.put.ask, spot)

        if config.NUM_STRADDLES_OVERRIDE > 0:
            sizing.num_straddles = config.NUM_STRADDLES_OVERRIDE
            sizing.total_call_cost = (
                sizing.call_cost_per * sizing.num_straddles
            )
            sizing.total_put_cost = (
                sizing.put_cost_per * sizing.num_straddles
            )
            sizing.total_capital_required = (
                (sizing.total_call_cost + sizing.total_put_cost) * 1.05
            )
            log.info("straddles_override",
                     forced=config.NUM_STRADDLES_OVERRIDE)

        if sizing.num_straddles == 0:
            msg = (
                f"Insufficient capital for even 1 straddle.\n"
                f"Equity: ${equity:,.2f}\n"
                f"Available (80%): ${sizing.available_capital:,.2f}\n"
                f"Straddle cost: ${sizing.straddle_cost:,.2f}"
            )
            log.warning("zero_straddles", msg=msg)
            await notifier.notify_skip(msg)
            return

        entry_check = self.risk.check_entry(
            sizing.num_straddles, sizing.straddle_cost,
        )
        if not entry_check.allowed:
            log.warning("entry_blocked", reason=entry_check.reason)
            await notifier.notify_skip(entry_check.reason)
            return

        # ── Pre-entry collateral check ──
        if config.HAS_OKX_CREDS:
            available = await self.exchange.get_account_equity()
            required = sizing.total_capital_required \
                * config.COLLATERAL_BUFFER_FACTOR
            if available > 0 and available < required:
                msg = (
                    f"Insufficient OKX trading-account balance.\n"
                    f"Available: ${available:,.2f}\n"
                    f"Required (× {config.COLLATERAL_BUFFER_FACTOR:.2f} "
                    f"buffer): ${required:,.2f}"
                )
                log.warning("collateral_check_failed", msg=msg)
                await notifier.notify_skip(msg)
                return
            log.info("collateral_check_ok",
                     available=f"${available:,.2f}",
                     required=f"${required:,.2f}")

        log.info(
            "preflight_check_passed",
            num_straddles=sizing.num_straddles,
            call_cost_per=f"${sizing.call_cost_per:,.2f}",
            put_cost_per=f"${sizing.put_cost_per:,.2f}",
            total_call_cost=f"${sizing.total_call_cost:,.2f}",
            total_put_cost=f"${sizing.total_put_cost:,.2f}",
            total_required=f"${sizing.total_capital_required:,.2f}",
            available=f"${sizing.available_capital:,.2f}",
            headroom=(
                f"${sizing.available_capital - sizing.total_capital_required:,.2f}"
            ),
        )

        await notifier.send(
            f"<b>PRE-FLIGHT CHECK</b>\n"
            f"Straddles: {sizing.num_straddles}\n"
            f"Spot: ${spot:,.0f} | Strike: ${pair.strike:,.0f}\n"
            f"\n<b>Per straddle:</b>\n"
            f"  Call cost ({config.QTY_PER_LEG} BTC): "
            f"${sizing.call_cost_per:,.2f}\n"
            f"  Put cost ({config.QTY_PER_LEG} BTC): "
            f"${sizing.put_cost_per:,.2f}\n"
            f"  Total: ${sizing.straddle_cost:,.2f}\n"
            f"\n<b>All {sizing.num_straddles} straddles:</b>\n"
            f"  Call cost: ${sizing.total_call_cost:,.2f}\n"
            f"  Put cost: ${sizing.total_put_cost:,.2f}\n"
            f"  Total (w/ 5% buffer): ${sizing.total_capital_required:,.2f}\n"
            f"  Available: ${sizing.available_capital:,.2f}\n"
            f"  Headroom: "
            f"${sizing.available_capital - sizing.total_capital_required:,.2f}\n"
        )

        straddle = await build_straddle(
            self.exchange, self.market, self.portfolio,
            pair, sizing.num_straddles,
            entry_spot=spot,
        )
        if straddle:
            self._consecutive_failures = 0
            volume_tracker.record_trade(sizing.num_straddles)
            # Convert BTC-quoted premiums to USD for human-readable display.
            entry_spot = straddle.entry_spot_price or spot
            call_fill_usd = straddle.entry_call_price * entry_spot
            put_fill_usd = straddle.entry_put_price * entry_spot
            call_cost_total_usd = (
                call_fill_usd * config.QTY_PER_LEG * sizing.num_straddles
            )
            put_cost_total_usd = (
                put_fill_usd * config.QTY_PER_LEG * sizing.num_straddles
            )
            await notifier.notify_entry(
                num_straddles=sizing.num_straddles,
                equity=equity,
                straddle_cost=sizing.straddle_cost,
                strike=pair.strike,
                call_fill=call_fill_usd,
                put_fill=put_fill_usd,
                call_cost_total=call_cost_total_usd,
                put_cost_total=put_cost_total_usd,
            )
            log.info("session_entry_done",
                     num_straddles=sizing.num_straddles,
                     call_fill_btc=straddle.entry_call_price,
                     put_fill_btc=straddle.entry_put_price,
                     call_fill_usd=call_fill_usd,
                     put_fill_usd=put_fill_usd)
        else:
            log.error("straddle_build_failed")
            self._register_session_failure("build_straddle returned None")

    # ──────────────────── Failure tracking / circuit breaker ─────

    def _register_session_failure(self, reason: str) -> None:
        """Increment failure counter; lock entries if threshold exceeded."""
        self._consecutive_failures += 1
        log.warning("session_failure_recorded",
                    count=self._consecutive_failures,
                    limit=config.CONSECUTIVE_FAILURE_LIMIT, reason=reason)
        if self._consecutive_failures >= config.CONSECUTIVE_FAILURE_LIMIT:
            self._entry_locked = True
            self._lock_reason = (
                f"{self._consecutive_failures} consecutive session failures "
                f"— restart algo to reset"
            )
            asyncio.create_task(notifier.send(
                f"<b>⚠️ CIRCUIT BREAKER TRIPPED</b>\n"
                f"{self._consecutive_failures} consecutive session failures.\n"
                f"Entry LOCKED until restart."
            ))

    # ──────────────────── End-of-session reconciliation ──────────

    async def _post_close_reconcile(self) -> None:
        """After unwind, verify exchange is actually flat. Alert on orphans."""
        try:
            positions = await self.exchange.list_open_positions()
        except Exception:
            log.warning("post_close_reconcile_fetch_failed", exc_info=True)
            return

        if not positions:
            log.info("post_close_flat_ok")
            return

        details = "\n".join(
            f"  • {p['instrument_name']}  amt={p['amount']:+.4f}  "
            f"mark=${p['mark_price']:,.2f}  uPnL=${p['unrealized_pnl']:+,.2f}"
            for p in positions
        )
        log.warning("post_close_orphan_detected", positions=len(positions))
        await notifier.send(
            f"<b>⚠️ POST-CLOSE ORPHAN DETECTED</b>\n"
            f"Unwind ran but exchange still has {len(positions)} "
            f"position(s):\n\n"
            f"{details}\n\n"
            f"<b>ACTION</b>: investigate & close manually. Next entry "
            f"will be blocked at startup reconciliation."
        )

    # ──────────────────── Close ───────────────────────────────────

    async def _on_close(self) -> None:
        try:
            equity_before = self.portfolio.equity
            pnl = await self.exit_mgr.hard_close()

            if config.HAS_OKX_CREDS:
                live_equity = await self.exchange.get_account_equity()
                if live_equity > 0:
                    self.portfolio.sync_equity(live_equity)
                await self._post_close_reconcile()

            actual_pnl = self.portfolio.equity - equity_before
            if actual_pnl != 0.0:
                cum_return = (
                    (self.portfolio.equity - config.INITIAL_CAPITAL_USD)
                    / config.INITIAL_CAPITAL_USD
                )
                await notifier.notify_daily_summary(
                    self.portfolio.equity, actual_pnl, cum_return,
                )
            self.portfolio.reset_daily()
            log.info("session_close_done",
                     pnl=f"${pnl:,.2f}",
                     actual_pnl=f"${actual_pnl:,.2f}",
                     equity=f"${self.portfolio.equity:,.2f}")
        except Exception:
            log.error("close_error", exc_info=True)
            await notifier.notify_error(
                "Close", "Unhandled exception — check logs",
            )

    # ──────────────────── Daily Report (17:00 UTC) ────────────────

    async def _on_report(self) -> None:
        try:
            await notifier.send_daily_report(self.portfolio.equity)
        except Exception:
            log.error("report_error", exc_info=True)
            await notifier.notify_error(
                "Report", "Daily report failed — check logs",
            )

    # ──────────────────── Weekly Report (Fri 18:00 UTC) ──────────

    async def _on_weekly_report(self) -> None:
        try:
            await notifier.send_weekly_report(self.portfolio.equity)
        except Exception:
            log.error("weekly_report_error", exc_info=True)
            await notifier.notify_error(
                "Weekly Report", "Weekly report failed — check logs",
            )

    # ──────────────────── Shutdown ────────────────────────────────

    async def shutdown(self) -> None:
        log.info("shutdown_initiated")
        await notifier.send("<b>OKX STRADDLE ALGO SHUTTING DOWN</b>")

        self.scheduler.stop()

        if self.portfolio.has_open:
            log.warning("closing_remaining_position")
            await unwind_straddle(
                self.exchange, self.market, self.portfolio,
                reason="shutdown",
            )

        _release_singleton_lock()
        log.info("algo_stopped")
        self._shutdown.set()


async def main() -> None:
    algo = Algo()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig, lambda: asyncio.create_task(algo.shutdown()),
        )

    try:
        await algo.start()
    except KeyboardInterrupt:
        await algo.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
