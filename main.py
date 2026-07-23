"""
OKX 0DTE BTC Pure Straddle Algo.

Multi-session: every Session in `config.SESSIONS` has its own weekday
filter, qty_per_leg and entry / close window. Default deployment fires
ten trades per week, paired into five complete trading days (Tue-Sat):

  • afternoon (1st entry) 13:30-15:30 UTC  Mon-Fri  @ 0.50 BTC / leg
  • morning   (2nd entry) 01:00-02:00 UTC  Tue-Sat  @ 0.25 BTC / leg

A "trading day" is the 0DTE expiry UTC date (08:00 UTC cutoff). The
afternoon session that fires Mon 13:30 UTC and the morning session that
fires Tue 01:00 UTC both expire Tue 08:00 UTC, so they roll up into ONE
Tuesday trading-day report.

Reports are CHAINED off the morning close (the last close of each
trading day), not run on a separate cron. After the Tue 02:00 UTC
morning close finishes you get, in order:
    SESSION CLOSE → DAILY REPORT
On the Saturday morning close (the last trading day of the week) you
additionally get:
    SESSION CLOSE → DAILY REPORT → WEEKLY REPORT

Position: 1 ITM call + 1 put (same strike) per session's qty_per_leg.
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
from datetime import datetime, timedelta

import structlog

import config
from core import family, notifier
from core.exchange import OKXExchange
from core.portfolio import Portfolio
from core.scheduler import Scheduler
from data.market_data import MarketData
from data.option_chain import OptionChain
from risk.risk_manager import RiskManager
from strategy.exit_manager import ExitManager
from strategy.option_selector import select_straddle_pair, select_wings
from strategy.position_sizer import size_position
from strategy.sizing import compute_qty_per_leg, telegram_summary_line
from strategy.straddle_builder import build_straddle, build_wings, unwind_straddle
from utils import volume_tracker
from utils.logging_config import setup_logging
from utils.time_utils import format_utc_sgt, now_utc

log = structlog.get_logger(__name__)


# Weekend-strategy session names. Imported lazily by the daily-report
# loader (reporting.daily_report.WEEKEND_SESSION_NAMES) too — keep both
# in sync. Used in main._on_close to (a) exclude weekend sessions from
# the weekly-report anchor and (b) gate the WEEKEND RECAP firing.
_WEEKEND_SESSION_NAMES: frozenset[str] = frozenset({
    "we_1100", "we_1200", "we_1230", "we_1330", "we_1430",
    "we_1500", "we_1700", "we_1900", "we_2200",
})

# WEEKEND RECAP trigger: session name + trading_day weekday that
# together identify "we just finished the weekend". Under the 2026-06-14
# schedule the weekday wd_0100 entry fires Mon-Fri, so the Monday wd_0100
# close (Mon 01:30 UTC) is the LAST close of the Mon trading_day — which
# also contains all of Sunday's weekend sessions (they target the Mon
# 08:00 expiry). We therefore anchor the recap to that close: when
# wd_0100 closes on a Mon trading_day, fire the Sat+Sun weekend recap.
_WEEKEND_RECAP_TRIGGER_SESSION: str = "wd_0100"
_WEEKEND_RECAP_TRIGGER_WEEKDAY: int = 0  # Mon


def _disable_entry_now_in_env_file(env_path: str = ".env") -> None:
    """Rewrite any "live" ENTRY_NOW value to ``false`` in the local .env.

    Catches every form the algo accepts as a fire trigger:
      - ``true`` / ``True`` / ``TRUE`` / ``1`` / ``yes``  (legacy boolean)
      - ``afternoon`` / ``morning``                       (session-name)
    Anything matching ``false`` / ``0`` / ``no`` / blank is left alone.

    History: the original regex only caught the boolean form, so a
    ``ENTRY_NOW=afternoon`` value would survive across container
    restarts and silently re-fire the entry every time the algo came
    back up. Caught 2026-05-19 after the user used the session-name
    form during the UM cutover dry-run.
    """
    try:
        if not os.path.exists(env_path):
            log.debug("entry_now_disable_skipped", reason="no_env_file")
            return
        with open(env_path, "r") as f:
            content = f.read()
        # Match every form main.py treats as a fire trigger:
        #   booleans:    true / TRUE / True / 1 / yes / YES / Yes
        #   legacy:      afternoon / AFTERNOON / morning / MORNING
        #   canonical:   utc_<4 digits> (case-insensitive prefix; covers
        #                utc_0100 / utc_0900 / utc_1330 / utc_2330 etc.)
        # Anything matching false / 0 / no / blank is left alone.
        new_content = re.sub(
            r"^(\s*ENTRY_NOW\s*=\s*)"
            r"(true|TRUE|True|1|yes|YES|Yes|"
            r"afternoon|AFTERNOON|Afternoon|"
            r"morning|MORNING|Morning|"
            r"[Uu][Tt][Cc]_\d{4})\b.*$",
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
        # True only for POSITION/orphan locks that may auto-release once the
        # exchange is confirmed flat (see _set_entry_lock / _maybe_release_
        # orphan_lock). Kill-switch locks leave this False → manual restart.
        self._lock_clearable_when_flat: bool = False
        self._consecutive_failures: int = 0
        # >0 while a session-close handler is mid-flight (unwind + equity
        # sync + reports + reset_daily). A deferred entry must wait until
        # this is 0 AND the position is flat, else _on_close's trailing
        # reset_daily() could wipe a freshly-opened straddle. See
        # _wait_for_flat / _run_entry.
        self._close_in_progress: int = 0
        # Reentrancy guard for the persistent post-close re-flatten. Sessions
        # each have their own close cron, so a later close can fire while an
        # earlier re-flatten loop is still running (the budget can span the
        # next 30-min window). Two concurrent loops would chase_sell the same
        # residual instrument and oversell into a short. The running loop
        # already re-reads the family-wide position set each round, so a
        # second reconcile can safely skip.
        self._reconcile_active: bool = False

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
                 reset_state=config.RESET_STATE_ON_BOOT,
                 option_family=family.label(),
                 option_family_display=family.display_name(),
                 option_family_underlying=family.underlying(),
                 option_family_instfamily=family.instfamily(),
                 option_family_raw_env=family.RAW)
        if family.RAW and family.RAW != family.label():
            log.warning("option_family_alias_resolved",
                        raw=family.RAW, resolved=family.label(),
                        hint="set OPTION_FAMILY=CM or UM in .env to "
                             "silence this warning")

        self.exchange.connect()

        # Prime per-instrument metadata (tick size, contract size). Public
        # endpoint, no auth required. Sets the runtime tick that chase_buy/
        # chase_sell use, replacing the old hardcoded OPTION_TICK_SIZE=5.0.
        # Also auto-verifies that ctVal × ctMult from the live API matches
        # config.OKX_CONTRACT_SIZE_BTC; sets exchange._contract_size_mismatch
        # on any deviation, which the lock-check below converts into an
        # entry lock.
        try:
            await self.exchange.prime_option_tick_size()
        except Exception:
            log.warning("prime_option_tick_failed", exc_info=True)

        if getattr(self.exchange, "_contract_size_mismatch", False):
            self._set_entry_lock(
                "Contract size mismatch — OKX API's ctVal × ctMult "
                "differs from config.OKX_CONTRACT_SIZE_BTC. See "
                "contract_size_api_mismatch log entry."
            )
            await notifier.send(
                "<b>STARTUP CONTRACT-SIZE MISMATCH</b>\n"
                "OKX's live ctVal × ctMult does not match the algo's "
                "configured BTC-per-contract value. This would cause "
                "catastrophic position sizing on the next trade.\n\n"
                "<b>Entries are LOCKED</b> until reconciled.\n"
                "Action: align OKX_CONTRACT_SIZE_BTC (CM) or "
                "OKX_CONTRACT_SIZE_BTC_UM (UM) in .env with the live "
                "API value, then restart."
            )

        # Validate that OPTION_ENTRY_CHASE_DEADLINE_MIN fits inside every
        # session's entry-window (entry_utc → close_utc). The chase MUST
        # complete before the session-close cron fires; otherwise close
        # runs on a partial position. With the 4-session schedule the
        # shortest windows are utc_0900 and utc_2330 at 30 min each, so
        # any deadline > 25 min violates the 5-min safety buffer for
        # those sessions. We hard-lock instead of warn — running with
        # the wrong knob is a P0 risk on first deploy.
        chase_ok, chase_reason = self._validate_chase_deadline_fits_sessions()
        if not chase_ok:
            log.error("chase_deadline_validation_failed",
                      reason=chase_reason,
                      deadline=config.OPTION_ENTRY_CHASE_DEADLINE_MIN)
            if not self._entry_locked:
                self._set_entry_lock(chase_reason)
                await notifier.send(
                    "<b>STARTUP CHASE-DEADLINE MISMATCH</b>\n"
                    f"{chase_reason}\n\n"
                    "<b>Entries are LOCKED</b> until reconciled.\n"
                    "Action: in .env, set\n"
                    "  <code>OPTION_ENTRY_CHASE_DEADLINE_MIN=25</code>\n"
                    "(or any value ≤ shortest_session_window − 5 min) "
                    "and restart the container."
                )
        else:
            log.info("chase_deadline_validation_ok",
                     deadline=config.OPTION_ENTRY_CHASE_DEADLINE_MIN)

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
            self._set_entry_lock(
                "Chase-pricing self-test failed — see logs. "
                "Tick size / unit conversion likely misconfigured."
            )

        # UM-only: verify the contract-size and quote-unit assumptions
        # against live OKX metadata before trading. Catches the case
        # where UM behaves differently from CM (e.g. minSz=1 contract
        # representing a different BTC notional than the 0.01 we inherit
        # from the CM family). Runs only when OPTION_FAMILY=UM so the
        # CM hot path stays on its existing fast boot.
        if family.is_um() and not self._entry_locked:
            um_ok = await self._um_unit_assumption_guard(spot)
            if not um_ok:
                self._set_entry_lock(
                    "UM unit-assumption guard failed — see logs. "
                    "Contract size / premium unit not as expected."
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
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

        def _days_str(weekdays: frozenset[int]) -> str:
            ordered = sorted(weekdays)
            if len(ordered) > 1 and ordered == list(
                range(ordered[0], ordered[-1] + 1)
            ):
                return f"{day_names[ordered[0]]}-{day_names[ordered[-1]]}"
            return ",".join(day_names[d] for d in ordered) or "—"

        # Boot-time pct_equity preview: for each pct_equity session,
        # estimate the qty/leg the next entry would resolve to using
        # the current spot and an indicative ITM premium. We skip live
        # chain-data here (the chain isn't refreshed yet at startup
        # banner time, and a faulty refresh shouldn't block the banner)
        # and instead use a heuristic 1.7%-of-spot/leg straddle premium —
        # representative of recent ITM 0DTE marks at the current vol regime.
        # Operators see a concrete USD-equivalent risk number BEFORE the
        # first entry fires, so a runaway equity bug or surprise pct
        # config is caught at boot rather than at trade time.
        equity_now = self.portfolio.equity
        BOOT_PREVIEW_PREMIUM_PCT_OF_SPOT = 0.017
        per_btc_premium_est = (
            spot * BOOT_PREVIEW_PREMIUM_PCT_OF_SPOT if spot > 0 else 0.0
        )

        def _session_preview(s: config.Session) -> str:
            base = (
                f"  • [{s.time_label}]  "
                f"{_days_str(s.weekdays)}  @ {s.describe_sizing()}"
                f"{'  (close +1d)' if s.crosses_midnight else ''}"
                f"{'  (with wings)' if config.session_wings_enabled(s) else ''}"
            )
            if s.sizing_mode not in ("pct_equity", "fixed_usd") \
                    or per_btc_premium_est <= 0:
                return base
            if s.sizing_mode == "fixed_usd":
                target_premium_usd = s.fixed_usd
            elif equity_now <= 0:
                return base
            else:
                target_premium_usd = equity_now * s.pct_equity
            est_qty = target_premium_usd / per_btc_premium_est
            est_qty = min(est_qty, config.MAX_QTY_PER_LEG_BTC)
            preview = (
                f"\n      preview @ ${equity_now:,.0f} equity, "
                f"${per_btc_premium_est:,.0f}/BTC indicative premium → "
                f"~{est_qty:.2f} BTC/leg, ~${target_premium_usd:,.0f} premium"
            )
            return base + preview

        sessions_lines = "\n".join(
            _session_preview(s) for s in config.SESSIONS
        )
        # Wings legend — only shown when the wing overlay is enabled, so the
        # "(with wings)" tags above are unambiguous.
        wings_note = ""
        if config.ENABLE_WINGS:
            wings_note = (
                f"\n<i>Wings: covered iron fly sold on entries in the "
                f"{config.WING_ENTRY_START_UTC.strftime('%H:%M')}–"
                f"{config.WING_ENTRY_END_UTC.strftime('%H:%M')} UTC window "
                f"only; all other entries are plain ATM straddles.</i>"
            )
        # Group sessions for the banner so weekend vs weekday is visually
        # obvious at a glance.
        weekend_enabled = any(
            s.enabled and s.name in _WEEKEND_SESSION_NAMES
            for s in config.SESSIONS
        )
        report_lines = [
            "  Reports:",
            "    • Daily — after each trading day's last close "
            "(Tue-Fri after wd_0100; Sat after wd_2330 Fri; "
            "Sun after we_2200 Sat; Mon after wd_0100)"
            if weekend_enabled
            else "    • Daily — after wd_0100 close (Tue-Fri), "
            "wd_2330 close (Sat)",
            "    • Weekly (Mon-Fri) — Sat ~00:00 UTC after wd_2330 Fri close",
        ]
        if weekend_enabled:
            report_lines.append(
                "    • Weekend recap (Sat-Sun) — Mon ~01:30 UTC after "
                "wd_0100 Mon close"
            )
        await notifier.send(
            f"<b>OKX STRADDLE ALGO STARTED</b>\n"
            f"Mode: {mode}"
            f"{' (DRY RUN)' if config.DRY_RUN else ''}\n"
            f"Family: {family.label()} ({family.display_name()})\n"
            f"Spot: ${spot:,.2f}\n"
            f"Equity: ${self.portfolio.equity:,.2f}\n"
            f"Time: {format_utc_sgt(now_utc())}\n"
            f"\n<b>Sessions:</b>\n"
            f"{sessions_lines}\n"
            f"{wings_note}\n"
            + "\n".join(report_lines)
            + f"{lock_line}\n"
        )

        self.scheduler.register_session(
            on_entry=self._on_entry,
            on_close=self._on_close,
        )
        self.scheduler.start()

        fire_times = self.scheduler.get_next_fire_times()
        for job_id, ft in fire_times.items():
            if ft:
                log.info("next_fire", job=job_id, time=format_utc_sgt(ft))

        # ENTRY_NOW supports either "true" (legacy single-session boolean)
        # or a session name. As of 2026-05-20 the canonical session names
        # are utc_0900 / utc_1330 / utc_2330 / utc_0100; legacy aliases
        # ("morning" → utc_0100, "afternoon" → utc_1330) still work via
        # config.get_session(). Boolean form picks the first ENABLED
        # session so toggling a session off in .env doesn't accidentally
        # cause ENTRY_NOW=true to fire it. Auto-disabled after firing so
        # a restart never silently re-fires.
        entry_now_raw = os.getenv("ENTRY_NOW", "").strip().lower()
        if entry_now_raw and entry_now_raw not in ("false", "0", "no"):
            target: config.Session | None = None
            if entry_now_raw in ("true", "1", "yes"):
                enabled_pool = [s for s in config.SESSIONS if s.enabled]
                target = enabled_pool[0] if enabled_pool else None
            else:
                target = config.get_session(entry_now_raw)
            if target is None:
                log.warning("immediate_entry_unknown_session",
                            raw=entry_now_raw,
                            valid=[s.name for s in config.SESSIONS])
            elif not target.enabled:
                # Operator explicitly typed a disabled session. Refuse
                # to fire it — the disabled flag is the panic-button
                # and shouldn't be silently overridden by ENTRY_NOW.
                log.warning("immediate_entry_session_disabled",
                            session=target.name,
                            note=(
                                "Session is disabled "
                                f"({target.name.upper()}_ENABLED=false). "
                                "ENTRY_NOW will NOT fire it. Re-enable "
                                "the session in .env first."
                            ))
                _disable_entry_now_in_env_file()
                if notifier:
                    await notifier.send(
                        "<b>⚠️ ENTRY_NOW IGNORED — SESSION DISABLED</b>\n"
                        f"Requested: <code>{target.name}</code>\n"
                        f"Reason: <code>{target.name.upper()}_ENABLED=false</code>\n"
                        "ENTRY_NOW has been auto-disabled in .env. "
                        "Re-enable the session in .env, then set ENTRY_NOW "
                        "again if you want to force-fire."
                    )
            else:
                log.info("immediate_entry_triggered",
                         session=target.name,
                         sizing=target.describe_sizing(),
                         fallback_qty_per_leg=target.qty_per_leg)
                _disable_entry_now_in_env_file()
                await self._on_entry(target)

        log.info("algo_running")
        await self._shutdown.wait()

    # ──────────────────── Startup Safeguards ──────────────────────

    @staticmethod
    def _validate_chase_deadline_fits_sessions() -> tuple[bool, str]:
        """Sanity-check OPTION_ENTRY_CHASE_DEADLINE_MIN against every session.

        Returns (ok, reason). The chase deadline must be ≤ session window
        − ``CLOSE_RACE_BUFFER_MIN`` so a worst-case-deadline chase fill
        completes before the close cron fires. Cross-midnight sessions
        compute their window correctly (close_utc + 24h when < entry_utc).
        """
        CLOSE_RACE_BUFFER_MIN = 5.0  # min cushion between chase end and close
        # Wings are now PER-SESSION (config.session_wings_enabled): only
        # sessions in the wing window run the body chase AND THEN the wing
        # chase sequentially, so ONLY those need the 2× (body+wing) budget.
        # Non-wing sessions need only the single body-entry budget. Validate
        # each session against its OWN worst-case so a tight non-wing window
        # isn't rejected for a wing budget it never incurs.
        body_deadline = float(config.OPTION_ENTRY_CHASE_DEADLINE_MIN)
        violations: list[str] = []
        any_wing_violation = False
        # Disabled sessions don't fire, so their windows can't race the
        # close cron — skip them. This lets an operator surgically
        # disable a misconfigured session without the validator (and
        # the resulting hard entry-lock) blocking the rest of the
        # algo from booting.
        for s in config.SESSIONS:
            if not s.enabled:
                continue
            has_wings = config.session_wings_enabled(s)
            deadline = body_deadline * (2 if has_wings else 1)
            entry_min = s.entry_utc.hour * 60 + s.entry_utc.minute
            close_min = s.close_utc.hour * 60 + s.close_utc.minute
            if close_min < entry_min:
                close_min += 24 * 60
            window = close_min - entry_min
            max_safe_deadline = window - CLOSE_RACE_BUFFER_MIN
            if deadline > max_safe_deadline:
                any_wing_violation = any_wing_violation or has_wings
                violations.append(
                    f"{s.name}{'(+wings)' if has_wings else ''}: "
                    f"window={window:.0f} min, "
                    f"max_safe_deadline={max_safe_deadline:.0f} min, "
                    f"configured={deadline:.0f} min"
                )
        if violations:
            label = (
                "OPTION_ENTRY_CHASE_DEADLINE_MIN×2 (body+wing)"
                if any_wing_violation
                else f"OPTION_ENTRY_CHASE_DEADLINE_MIN={body_deadline:.0f}"
            )
            return False, (
                f"{label} "
                f"would race the close cron on: " + "; ".join(violations)
            )
        return True, ""

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

                # Family-aware absolute ceiling: CM premiums are
                # quoted in BTC (≤ 0.5 BTC sane), UM in USD-per-BTC-
                # notional (≤ $50,000 sane on a $80k-spot day).
                abs_ceiling = (
                    config.CHASE_SELFTEST_MAX_ABSOLUTE_USD
                    if family.is_um()
                    else config.CHASE_SELFTEST_MAX_ABSOLUTE_BTC
                )
                ok_absolute = capped <= abs_ceiling
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
            abs_ceiling = (
                config.CHASE_SELFTEST_MAX_ABSOLUTE_USD
                if family.is_um()
                else config.CHASE_SELFTEST_MAX_ABSOLUTE_BTC
            )
            unit = family.native_quote_unit_label()
            log.error("chase_selftest_failed",
                      family=family.label(),
                      instrument=sample.symbol,
                      pre_cap_price=pre_cap, capped_price=capped,
                      mark=mark, tick=tick,
                      max_over_mark=config.CHASE_SELFTEST_MAX_OVER_MARK,
                      max_absolute=abs_ceiling)
            await notifier.send(
                f"<b>STARTUP SELF-TEST FAILED</b>\n"
                f"Every sampled ITM option produced an out-of-bound price. "
                f"This is a likely unit-conversion bug, NOT a wide-spread "
                f"issue.\n"
                f"Family: {family.label()} ({family.display_name()})\n"
                f"Last sample: {sample.symbol}\n"
                f"Bid/Ask: {bid} / {ask} {unit}\n"
                f"Mark: {mark} {unit}, tick: {tick}\n"
                f"Pre-cap price: {pre_cap} {unit}, "
                f"post-cap: {capped} {unit}\n"
                f"Entry will be LOCKED until restart with fix."
            )
            return False
        except Exception:
            log.error("chase_selftest_exception", exc_info=True)
            return True  # don't block on transient errors

    async def _um_unit_assumption_guard(self, spot: float) -> bool:
        """UM-only pre-trade live verification of the unit assumptions.

        Defends the first UM cutover trade against the latent risk that
        OKX's BTC-USD_UM family quotes / sizes its options differently
        from the assumed convention.

        The PRIMARY contract-size verification happens earlier in the
        startup sequence (``prime_option_tick_size`` reads ctVal × ctMult
        from the API and sets ``exchange._contract_size_mismatch`` on any
        deviation). This guard adds three additional UM-specific checks
        that prove the *quote unit* and *position-sizing math* will land
        correctly:

          1. Tick size is in plausible USD range (1 ≤ tick ≤ 100). Real
             value is 5 USD; we accept anything sane to allow OKX to
             widen ticks during a market disruption without locking us out.
          2. Sample UM ITM ask falls in USD range ($50 – $50,000),
             NOT in BTC range (0.0001 – 0.5). Catches the catastrophic
             case where the wire is somehow still BTC-quoted.
          3. CROSS-FAMILY PROBE (verified live 2026-05-15 against OKX
             public API): for the same strike + same expiry, the UM
             USD-mark must roughly equal the CM BTC-mark × spot. A
             ≥30% disagreement suggests our unit assumption is wrong.

        Returns False on any failure so the caller can lock entries
        and alert the operator. CM bypasses this method entirely.
        """
        if not family.is_um():
            return True

        try:
            # ── 1. Tick size sanity ──
            tick = self.exchange.get_tick_size()
            tick_ok = 1.0 <= tick <= 100.0
            if not tick_ok:
                log.error("um_guard_tick_failed",
                          live_tick=tick,
                          range="[1, 100] USD",
                          hint="UM tick should be 5 USD on OKX")

            # ── Pick a sample UM ITM put (most reliable for cross-
            # family verification because deep-ITM puts always have a
            # tight intrinsic anchor that makes the cross-check robust).
            await self.chain.refresh()
            sample = None
            for p in self.chain.puts:
                if p.strike > spot and p.bid > 0 and p.ask > 0:
                    sample = p
                    break
            if sample is None:
                for c in self.chain.calls:
                    if c.strike < spot and c.bid > 0 and c.ask > 0:
                        sample = c
                        break
            if sample is None:
                log.warning("um_guard_no_sample",
                            note="no UM 0DTE quotes available; "
                                 "deferring guard until first live data")
                return True  # don't lock on transient empty chain

            # ── 2. Live ask in USD range, NOT BTC range ──
            ask = sample.ask
            in_usd_range = 50.0 <= ask <= 50_000.0
            in_btc_range = 0.0001 <= ask <= 0.5
            quote_ok = in_usd_range and not in_btc_range
            if not quote_ok:
                log.error("um_guard_quote_unit_failed",
                          instrument=sample.symbol,
                          ask=ask,
                          in_usd_range=in_usd_range,
                          in_btc_range=in_btc_range,
                          hint=("UM ask must be USD-per-BTC-of-notional "
                                "($50-$50000); BTC-range ask means the "
                                "wire is still inverse-quoted"))

            # ── 3. Cross-family probe ──
            # Build the equivalent CM instId by swapping the family
            # token. UM: BTC-USD_UM-{exp}-{strike}-{C|P}
            # CM: BTC-USD-{exp}-{strike}-{C|P}
            cm_inst_id = sample.symbol.replace("BTC-USD_UM-", "BTC-USD-", 1)
            cross_ok = True  # default-pass when CM peer is unavailable
            cross_detail = "skipped (no CM peer)"
            try:
                um_mark = await self.exchange.get_option_mark_price(
                    sample.symbol,
                )
                cm_mark = await self.exchange.get_option_mark_price(
                    cm_inst_id,
                )
                if um_mark > 0 and cm_mark > 0 and spot > 0:
                    cm_mark_usd = cm_mark * spot
                    rel_err = (
                        abs(um_mark - cm_mark_usd) / cm_mark_usd
                        if cm_mark_usd > 0 else 1.0
                    )
                    cross_ok = rel_err <= 0.30  # ≤30% (verified ~2% in practice)
                    cross_detail = (
                        f"UM=${um_mark:.0f}, CM={cm_mark:.4f}BTC "
                        f"(${cm_mark_usd:.0f}), rel_err={rel_err:.1%}"
                    )
                    if not cross_ok:
                        log.error("um_guard_cross_family_failed",
                                  instrument=sample.symbol,
                                  cm_peer=cm_inst_id,
                                  um_mark_usd=um_mark,
                                  cm_mark_btc=cm_mark,
                                  cm_mark_usd_via_spot=cm_mark_usd,
                                  rel_err=rel_err,
                                  hint=("UM USD-mark and CM BTC-mark×spot "
                                        "should agree within ~5%; large "
                                        "divergence means the unit "
                                        "interpretation is wrong"))
            except Exception:
                log.warning("um_guard_cross_family_skipped",
                            instrument=sample.symbol,
                            exc_info=True)

            # ── 4. Implied position size sanity ──
            # Use the largest session's FALLBACK qty (the value that
            # would fire under fixed_btc OR if pct_equity sizing fails)
            # to compute "how many contracts is that and what's the USD
            # notional". Under pct_equity the actual qty at fire-time
            # may be substantially larger (e.g. 50% × $7.7k equity →
            # ~2.85 BTC vs 0.5 BTC fallback), so this is a LOWER-bound
            # sanity check, not a forecast. The fire-time entry log
            # surfaces the resolved qty in ``sizing_decision``.
            sample_qty_btc = max(
                (s.qty_per_leg for s in config.SESSIONS if s.enabled),
                default=0.5,
            )
            sample_contracts = sample_qty_btc / config.OKX_CONTRACT_SIZE_BTC
            sample_usd = sample_qty_btc * spot

            log.info("um_guard_summary",
                     family=family.label(),
                     tick=tick, tick_ok=tick_ok,
                     sample_instrument=sample.symbol,
                     sample_ask_usd=ask,
                     quote_unit_ok=quote_ok,
                     cross_family=cross_detail,
                     cross_family_ok=cross_ok,
                     largest_session_qty_btc=sample_qty_btc,
                     implied_contracts=sample_contracts,
                     implied_notional_usd=round(sample_usd, 0),
                     contract_size_btc=(
                         config.OKX_CONTRACT_SIZE_BTC
                     ))

            all_ok = tick_ok and quote_ok and cross_ok
            if not all_ok:
                fail_lines = []
                if not tick_ok:
                    fail_lines.append(
                        f"  • tick={tick} USD (expected 1-100)"
                    )
                if not quote_ok:
                    fail_lines.append(
                        f"  • sample ask={ask} "
                        f"(expected USD range $50-$50000)"
                    )
                if not cross_ok:
                    fail_lines.append(
                        f"  • cross-family: {cross_detail}"
                    )
                await notifier.send(
                    f"<b>UM UNIT-ASSUMPTION GUARD FAILED</b>\n"
                    f"OKX returned UM metadata that does not match the "
                    f"algo's UM unit assumptions.\n"
                    f"Sample instrument: {sample.symbol}\n"
                    + "\n".join(fail_lines) + "\n\n"
                    f"<b>Entries are LOCKED</b> until investigated.\n"
                    f"Run diagnose_um_cutover.py for a full probe."
                )
            return all_ok
        except Exception:
            log.error("um_guard_exception", exc_info=True)
            # Don't block on a transient exception — the chase pricing
            # self-test already runs above and would catch a math bug.
            return True

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
            self._set_entry_lock("Could not fetch positions from OKX")
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
            details = await self._fmt_positions_with_book(exchange_positions)
            self._set_entry_lock(
                f"Exchange has {len(exchange_positions)} open position(s) "
                f"but algo state is empty — possible orphan",
                clearable_when_flat=True,
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
            # NOT auto-clearable: the exchange is already flat here, so a
            # flat-recheck would wrongly release instantly. The fault is a
            # stale local positions.json that needs an operator reset.
            self._set_entry_lock(
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

    async def _wait_for_flat(
        self, session: config.Session, label: str,
    ) -> bool:
        """Wait for any prior straddle to finish unwinding before a late
        entry, bounded by a safe-margin cutoff.

        Returns True if the position went flat (and no close is in
        progress) with enough of THIS window left for a safe chase —
        the caller should then enter (possibly late). Returns False if
        the cutoff passed first — the caller should skip.

        Cutoff: we must leave at least
        ``OPTION_ENTRY_CHASE_DEADLINE_MIN + CLOSE_RACE_BUFFER_MIN`` minutes
        before this session's own close, so a deferred entry never races
        the close cron / violates the chase-deadline margin.
        """
        POLL_SEC = 5.0
        CLOSE_RACE_BUFFER_MIN = 5.0
        required_min = (
            float(config.OPTION_ENTRY_CHASE_DEADLINE_MIN)
            + CLOSE_RACE_BUFFER_MIN
        )

        def _is_flat() -> bool:
            return (not self.portfolio.has_open) and \
                self._close_in_progress == 0

        if _is_flat():
            return True

        now = now_utc()
        # Resolve this window's close as a wall-clock datetime (handles
        # cross-midnight closes, e.g. entry 23:30 → close 00:00 next day).
        close_dt = now.replace(
            hour=session.close_utc.hour, minute=session.close_utc.minute,
            second=0, microsecond=0,
        )
        if close_dt <= now:
            close_dt += timedelta(days=1)
        cutoff_dt = close_dt - timedelta(minutes=required_min)

        log.warning(
            "entry_deferred_waiting_for_flat",
            session=session.name,
            has_open=self.portfolio.has_open,
            close_in_progress=self._close_in_progress,
            cutoff_utc=cutoff_dt.isoformat(),
            required_min=required_min,
        )
        await notifier.notify_skip(
            f"[{label}] Prior straddle still closing — holding the entry "
            f"until flat (will skip if &lt;{required_min:.0f} min remain).",
        )

        while True:
            if _is_flat():
                log.info(
                    "entry_deferred_now_flat", session=session.name,
                    late_by_sec=(now_utc() - now).total_seconds(),
                )
                return True
            if now_utc() >= cutoff_dt:
                log.warning(
                    "entry_deferred_cutoff_skip", session=session.name,
                    has_open=self.portfolio.has_open,
                    close_in_progress=self._close_in_progress,
                )
                await notifier.notify_skip(
                    f"[{label}] Prior straddle still not flat and the "
                    f"safe-entry cutoff passed — skipping this entry.",
                )
                return False
            await asyncio.sleep(POLL_SEC)

    async def _on_entry(self, session: config.Session) -> None:
        label = session.time_label
        try:
            await self._run_entry(session)
        except Exception:
            log.error("entry_error", session=session.name,
                      label=label, exc_info=True)
            await notifier.notify_error(
                f"Entry [{label}]",
                "Unhandled exception — check logs",
            )

    async def _run_entry(self, session: config.Session) -> None:
        label = session.time_label
        log.info("session_entry_start",
                 session=session.name,
                 label=label,
                 sizing_mode=session.sizing_mode,
                 pct_equity=session.pct_equity,
                 fallback_qty_per_leg=session.qty_per_leg)

        if self._entry_locked:
            # Orphan/position locks may auto-release once the exchange is
            # confirmed flat (e.g. a worthless leg settled at expiry) so we
            # don't need a manual restart. Kill-switch locks never clear here.
            released = await self._maybe_release_orphan_lock(session.name)
            if not released:
                log.warning("entry_blocked_lock",
                            session=session.name, reason=self._lock_reason)
                await notifier.notify_skip(
                    f"[{label}] Entry locked: {self._lock_reason}",
                )
                return

        api_check = self.risk.check_api_health(self.exchange.error_count)
        if not api_check.allowed:
            log.warning("entry_blocked_api",
                        session=session.name, reason=api_check.reason)
            await notifier.notify_skip(
                f"[{label}] {api_check.reason}",
            )
            return

        loss_check = self.risk.check_daily_loss()
        if not loss_check.allowed:
            log.warning("entry_blocked_loss",
                        session=session.name, reason=loss_check.reason)
            await notifier.notify_skip(
                f"[{label}] {loss_check.reason}",
            )
            return

        if self.portfolio.has_open or self._close_in_progress > 0:
            # A prior session's straddle is still unwinding (maker-only
            # close hasn't filled yet) or its close handler is still
            # finishing. Rather than skip outright, WAIT for the flat and
            # then enter late — but only while enough of THIS window
            # remains for a safe chase (see _wait_for_flat). If the cutoff
            # passes first, skip.
            flat = await self._wait_for_flat(session, label)
            if not flat:
                return

        # ── Pre-entry exchange-flat guard (defence-in-depth) ──
        # Local state can be WRONG: a phantom close (both sell legs failing
        # on a transient disconnect) marks the straddle closed + resets
        # local state while the exchange still holds the legs. The local
        # `has_open` guard above then waves a new entry through, stacking a
        # fresh straddle on top of the orphan (2026-06-18 incident). So
        # before opening, query the EXCHANGE directly; if it is not flat,
        # refuse and lock rather than trusting local state alone.
        if config.HAS_OKX_CREDS:
            try:
                live_positions = await self.exchange.list_open_positions()
            except Exception:
                log.warning("preentry_position_check_failed",
                            session=session.name, exc_info=True)
                live_positions = []
            if live_positions:
                detail = ", ".join(
                    f"{p['instrument_name']} {p['amount']:+.4f}"
                    for p in live_positions
                )
                self._set_entry_lock(
                    f"Pre-entry exchange not flat: {len(live_positions)} "
                    f"open position(s) — possible orphan, refusing to stack",
                    clearable_when_flat=True,
                )
                log.error("entry_blocked_exchange_not_flat",
                          session=session.name, positions=detail)
                await notifier.send(
                    f"<b>⚠️ ENTRY BLOCKED — EXCHANGE NOT FLAT</b> [{label}]\n"
                    f"Refusing to open a new straddle on top of an existing "
                    f"position (stacking guard).\n\n"
                    f"Live position(s): {detail}\n\n"
                    f"<b>ENTRIES ARE NOW LOCKED.</b> Flatten with "
                    f"tools/force_liquidate.py, then restart to clear."
                )
                return

        total_options = await self.chain.refresh()
        if total_options == 0:
            log.error("no_0dte_options", session=session.name)
            await notifier.notify_skip(
                f"[{label}] No 0DTE options found on OKX",
            )
            return

        spot = await self.exchange.get_spot_price()
        pair = select_straddle_pair(self.chain, spot)
        if pair is None:
            await notifier.notify_skip(
                f"[{label}] No valid ITM call + put pair near "
                f"spot ${spot:,.0f}",
            )
            return

        if config.HAS_OKX_CREDS:
            live_equity = await self.exchange.get_account_equity()
            if live_equity > 0:
                self.portfolio.sync_equity(live_equity)

        equity = self.portfolio.equity

        # ── Per-entry qty resolution ──
        # Replaces the old "session.qty_per_leg" hard-wire. With
        # ``sizing_mode=pct_equity`` (default) the qty is computed at
        # entry-time from current equity + live mid prices so that
        # premium ≈ pct_equity × equity. With ``sizing_mode=fixed_btc``
        # the session's qty_per_leg is used unchanged. See strategy/sizing.py.
        resolved_qty, sizing_audit = compute_qty_per_leg(
            session,
            equity_usd=equity,
            pair=pair,
            spot_usd=spot,
        )
        log.info("sizing_decision", **sizing_audit)

        if resolved_qty <= 0:
            msg = (
                f"[{label}] Sizing skipped this entry.\n"
                f"Reason: {sizing_audit.get('skip_reason', sizing_audit.get('decision'))}\n"
                f"Equity: ${equity:,.2f}, "
                f"target_pct: {session.pct_equity:.0%}"
            )
            log.warning("entry_skipped_by_sizing", **sizing_audit)
            await notifier.notify_skip(msg)
            return

        # Premium quotes are in BTC; sizer needs spot to compute USD costs.
        # Use the RESOLVED qty (not session.qty_per_leg) so the capital-fit
        # check below operates on the size we'll actually trade.
        sizing = size_position(
            equity, pair.call.ask, pair.put.ask, spot,
            qty_per_leg=resolved_qty,
        )

        # Number-of-straddles override:
        #   pct_equity mode  → ALWAYS 1 straddle. The resolved BTC qty
        #                      from compute_qty_per_leg() already encodes
        #                      the operator's target premium; multiplying
        #                      by num_straddles>1 would overshoot.
        #   fixed_btc mode   → respect NUM_STRADDLES_OVERRIDE (legacy),
        #                      so existing test runs still work.
        if session.sizing_mode in ("pct_equity", "fixed_usd"):
            forced_n = 1
        elif config.NUM_STRADDLES_OVERRIDE > 0:
            forced_n = config.NUM_STRADDLES_OVERRIDE
        else:
            forced_n = sizing.num_straddles  # capital-fit default

        if forced_n != sizing.num_straddles:
            sizing.num_straddles = forced_n
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
                     forced=forced_n,
                     reason=(f"{session.sizing_mode}_always_1"
                             if session.sizing_mode in ("pct_equity", "fixed_usd")
                             else "NUM_STRADDLES_OVERRIDE"))

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

        sizing_summary = telegram_summary_line(
            sizing_audit, resolved_qty, sizing.num_straddles,
        )
        await notifier.send(
            f"<b>PRE-FLIGHT CHECK [{label}]</b>\n"
            f"{sizing_summary}\n"
            f"Straddles: {sizing.num_straddles}\n"
            f"BTC per leg: {resolved_qty:.4f}\n"
            f"Spot: ${spot:,.0f} | Strike: ${pair.strike:,.0f}\n"
            f"\n<b>Per straddle:</b>\n"
            f"  Call cost ({resolved_qty:.4f} BTC): "
            f"${sizing.call_cost_per:,.2f}\n"
            f"  Put cost ({resolved_qty:.4f} BTC): "
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
            qty_per_leg=resolved_qty,
            session_name=session.name,
            entry_spot=spot,
        )
        if straddle:
            self._consecutive_failures = 0
            volume_tracker.record_trade(
                sizing.num_straddles, qty_per_leg=resolved_qty,
            )
            # Convert OKX-native premiums to USD for human-readable display.
            # CM: native is BTC, multiply by spot. UM: native is already
            # USD-per-BTC-of-notional, identity. ``family.native_premium_to_usd``
            # handles both with qty_btc=1.0 (= per BTC of notional).
            entry_spot_usd = straddle.entry_spot_price or spot
            call_fill_usd = family.native_premium_to_usd(
                straddle.entry_call_price, qty_btc=1.0, spot_usd=entry_spot_usd,
            )
            put_fill_usd = family.native_premium_to_usd(
                straddle.entry_put_price, qty_btc=1.0, spot_usd=entry_spot_usd,
            )
            call_cost_total_usd = (
                call_fill_usd * resolved_qty * sizing.num_straddles
            )
            put_cost_total_usd = (
                put_fill_usd * resolved_qty * sizing.num_straddles
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
                session_label=label,
                qty_per_leg=resolved_qty,
                wings_enabled=config.session_wings_enabled(session),
            )
            log.info("session_entry_done",
                     session=session.name,
                     qty_per_leg=resolved_qty,
                     sizing_decision=sizing_audit.get("decision"),
                     num_straddles=sizing.num_straddles,
                     family=family.label(),
                     call_fill_native=straddle.entry_call_price,
                     put_fill_native=straddle.entry_put_price,
                     call_fill_usd=call_fill_usd,
                     put_fill_usd=put_fill_usd)

            # ── Iron-fly wings: sell covered wings AFTER the body filled ──
            # LONGS-FIRST — the body is fully filled here (build_straddle
            # returns None on any partial), so every wing is covered. Wing
            # failures are non-fatal: the plain straddle stands and is safe.
            if config.session_wings_enabled(session):
                try:
                    wings = select_wings(
                        self.chain, pair.strike,
                        call_offset=config.WING_CALL_STRIKE_OFFSET,
                        put_offset=config.WING_PUT_STRIKE_OFFSET,
                    )
                    if wings.any:
                        await build_wings(
                            self.exchange, self.market, self.portfolio,
                            straddle, wings,
                        )
                    else:
                        log.warning("no_valid_wings_body_only",
                                    session=session.name, strike=pair.strike)
                        await notifier.send(
                            f"<b>NO WINGS AVAILABLE [{label}]</b>\n"
                            f"No adjacent strike with a live bid — holding "
                            f"the plain straddle (safe)."
                        )
                except Exception:
                    log.error("wing_build_error", session=session.name,
                              exc_info=True)
                    await notifier.notify_error(
                        "Wings",
                        "Wing sell failed — body straddle is intact and "
                        "covered; check logs.")
        else:
            log.error("straddle_build_failed", session=session.name)
            self._register_session_failure(
                f"[{session.name}] build_straddle returned None",
            )

    # ──────────────────── Failure tracking / circuit breaker ─────

    def _register_session_failure(self, reason: str) -> None:
        """Increment failure counter; lock entries if threshold exceeded."""
        self._consecutive_failures += 1
        log.warning("session_failure_recorded",
                    count=self._consecutive_failures,
                    limit=config.CONSECUTIVE_FAILURE_LIMIT, reason=reason)
        if self._consecutive_failures >= config.CONSECUTIVE_FAILURE_LIMIT:
            self._set_entry_lock(
                f"{self._consecutive_failures} consecutive session failures "
                f"— restart algo to reset"
            )
            asyncio.create_task(notifier.send(
                f"<b>⚠️ CIRCUIT BREAKER TRIPPED</b>\n"
                f"{self._consecutive_failures} consecutive session failures.\n"
                f"Entry LOCKED until restart."
            ))

    # ──────────────────── End-of-session reconciliation ──────────

    @staticmethod
    def _fmt_positions(positions: list[dict]) -> str:
        return "\n".join(
            f"  • {p['instrument_name']}  amt={p['amount']:+.4f}  "
            f"mark=${p['mark_price']:,.2f}  uPnL=${p['unrealized_pnl']:+,.2f}"
            for p in positions
        )

    async def _fmt_positions_with_book(self, positions: list[dict]) -> str:
        """Like ``_fmt_positions`` but augments each leg with its LIVE bid/ask
        and a plain-language SELLABILITY verdict, so a stuck-position alert
        tells us *why* it is stuck instead of leaving us to guess from mark=0:

          • a LONG (amt>0) closes by SELLING → it needs a **bid**; bid=0 means
            it is genuinely unsellable and only expiry settlement will clear it.
          • a SHORT (amt<0) closes by BUYING → it needs an **ask**; ask=0 means
            it cannot be bought back right now.

        A leg that still shows a live bid/ask on the closeable side means the
        chaser is missing real liquidity and needs investigation (not expiry).
        Best-effort: a ticker fetch failure just omits the book for that leg.
        """
        lines: list[str] = []
        for p in positions:
            inst = p.get("instrument_name", "?")
            amt = float(p.get("amount", 0.0) or 0.0)
            mark = float(p.get("mark_price", 0.0) or 0.0)
            upnl = float(p.get("unrealized_pnl", 0.0) or 0.0)
            book = ""
            verdict = ""
            try:
                t = await self.exchange.get_ticker(inst)
                bid, ask = float(t.bid or 0.0), float(t.ask or 0.0)
                book = f"  bid=${bid:,.4f} ask=${ask:,.4f}"
                if amt > 0:  # long → sell to close → need a bid
                    verdict = (" → NO BID: unsellable, settles at expiry"
                               if bid <= 0 else " → has bid: sellable")
                elif amt < 0:  # short → buy to close → need an ask
                    verdict = (" → NO ASK: cannot buy back now"
                               if ask <= 0 else " → has ask: closeable")
            except Exception:
                log.warning("orphan_book_fetch_failed",
                            instrument=inst, exc_info=True)
            lines.append(
                f"  • {inst}  amt={amt:+.4f}  mark=${mark:,.2f}  "
                f"uPnL=${upnl:+,.2f}{book}{verdict}")
        return "\n".join(lines)

    def _set_entry_lock(
        self, reason: str, *, clearable_when_flat: bool = False,
    ) -> None:
        """Engage the entry lock, recording whether it may auto-release.

        ``clearable_when_flat=True`` marks a POSITION/orphan lock (post-close
        residual, startup / pre-entry "exchange not flat") that becomes moot
        the instant the exchange is genuinely flat — e.g. a worthless 0DTE leg
        settling at 08:00 UTC expiry. Kill-switch locks (config/self-test/API/
        stale-state/circuit-breaker) pass False so ONLY an operator restart
        clears them. Centralising this ensures a later stacked lock can never
        inherit a stale clearable flag from an earlier one.
        """
        self._entry_locked = True
        self._lock_reason = reason
        self._lock_clearable_when_flat = clearable_when_flat

    async def _maybe_release_orphan_lock(self, session_name: str) -> bool:
        """Auto-release an orphan/position entry-lock once the exchange is
        confirmed flat. Returns True when released (entry may proceed), False
        when the caller must keep blocking.

        Fail-closed: disabled flag, a non-clearable (kill-switch) lock, no
        credentials, a fetch failure, or ANY live position all keep the lock
        latched. Only a clean, credentialed, genuinely-flat read releases it —
        the same authority the pre-entry stacking guard already trusts.
        """
        if not config.SELF_HEAL_LOCK_ON_FLAT:
            return False
        if not self._lock_clearable_when_flat:
            return False
        if not config.HAS_OKX_CREDS:
            return False
        try:
            positions = await self.exchange.list_open_positions()
        except Exception:
            log.warning("orphan_lock_recheck_fetch_failed",
                        session=session_name, exc_info=True)
            return False
        if positions:
            log.info("orphan_lock_still_not_flat",
                     session=session_name, positions=len(positions))
            return False
        prior = self._lock_reason
        self._entry_locked = False
        self._lock_reason = ""
        self._lock_clearable_when_flat = False
        log.warning("orphan_lock_auto_released",
                    session=session_name, prior_reason=prior)
        await notifier.send(
            "<b>✅ ENTRY LOCK AUTO-CLEARED</b>\n"
            "The exchange is now flat — the earlier orphan/position lock has "
            "released on its own (e.g. a worthless leg settled at expiry). "
            "Trading resumes.\n\n"
            f"<i>Cleared lock:</i> {prior}"
        )
        return True

    async def _flatten_residual_until_flat(
        self, positions: list[dict],
    ) -> list[dict]:
        """Persistently close any residual legs left after the unwind.

        Maker-only. Each round re-reads the LIVE position and chases only
        the *remaining* qty (long → sell, short → buy back), so it can never
        oversell into a short. Loops until flat or CLOSE_FLATTEN_BUDGET_MIN
        is exhausted. Entries stay blocked (_close_in_progress > 0) the whole
        time, so nothing stacks on top of the residual. Returns the still-open
        positions (empty list ⇒ now flat).
        """
        budget_min = config.CLOSE_FLATTEN_BUDGET_MIN
        round_min = config.CLOSE_FLATTEN_ROUND_MIN
        ct = config.OKX_CONTRACT_SIZE_BTC
        deadline = now_utc() + timedelta(minutes=budget_min)

        log.warning("post_close_residual_reflatten_start",
                    positions=len(positions), budget_min=budget_min,
                    round_min=round_min)
        await notifier.send(
            f"<b>♻️ POST-CLOSE RESIDUAL — RE-FLATTENING</b>\n"
            f"Unwind left {len(positions)} open leg(s). Persistently "
            f"closing (maker-only, up to {budget_min:.0f} min). Entries "
            f"stay blocked until flat.\n\n"
            f"{self._fmt_positions(positions)}"
        )

        round_no = 0
        while now_utc() < deadline:
            round_no += 1
            remaining_min = (deadline - now_utc()).total_seconds() / 60.0
            if remaining_min <= 0:
                break
            eff_round_min = min(round_min, remaining_min)

            # SHORTS-FIRST: a residual LONG may be a body leg still covering a
            # residual SHORT wing (held back by unwind_straddle). Selling that
            # long while the short is open = a naked short. So while ANY short
            # remains, buy back shorts ONLY and hold every long; longs are sold
            # only once the round re-read confirms no short is left.
            shorts_present = any(
                float(p.get("amount", 0.0)) < 0 for p in positions
            )

            # TAKER ESCALATION: once maker rounds have failed to reach flat,
            # stop chasing maker (which can be stranded forever when the book
            # won't lift a capped order) and CROSS the spread to guarantee the
            # residual closes. Risk-reducing on both sides (sell long / buy
            # back short), still shorts-first via the gate below.
            use_taker = round_no > config.CLOSE_FLATTEN_TAKER_AFTER_ROUNDS

            def _record_reflatten_fill(symbol: str, res):
                """Feed a residual leg's realized close price into the open
                straddle's exit-fills so the deferred two-phase finalize books
                the REAL exit (never the entry-price placeholder). No-op when
                the residual is a true orphan with no tracked straddle."""
                s = self.portfolio.open_straddle
                if s is None or not isinstance(res, dict):
                    return
                px = float(res.get("average_price", 0.0) or 0.0)
                if px > 0:
                    s.record_exit_fill(symbol, px)

            async def _chase_leg(symbol: str, amt: float):
                """Close ONE residual leg for this round. Longs → sell,
                shorts → buy-back. Maker chase for the first rounds, then
                taker-cross once ``use_taker``. Returns result or None."""
                try:
                    if use_taker:
                        if amt > 0:
                            log.warning("reflatten_taker_escalate_long",
                                        instrument=symbol, round=round_no)
                            res = await self.exchange._taker_flatten_long(
                                symbol, amt * ct)
                        else:
                            log.warning("reflatten_taker_escalate_short",
                                        instrument=symbol, round=round_no)
                            res = await self.exchange._taker_flatten_short(
                                symbol, abs(amt) * ct)
                        _record_reflatten_fill(symbol, res)
                        return res

                    bid, ask = await self.market.get_option_bid_ask(symbol)
                    if amt > 0:
                        if ask <= 0:
                            log.info("reflatten_skip_no_ask",
                                     instrument=symbol, round=round_no)
                            return None
                        res = await self.exchange.chase_sell(
                            symbol, amt * ct, ask, deadline_min=eff_round_min,
                        )
                    else:
                        if bid <= 0:
                            log.info("reflatten_skip_no_bid",
                                     instrument=symbol, round=round_no)
                            return None
                        res = await self.exchange.chase_buy(
                            symbol, abs(amt) * ct, bid,
                            deadline_min=eff_round_min,
                        )
                    _record_reflatten_fill(symbol, res)
                    return res
                except Exception:
                    log.warning("reflatten_chase_error", instrument=symbol,
                                round=round_no, exc_info=True)
                    return None

            # Fire every ELIGIBLE same-side leg CONCURRENTLY (a stuck maker
            # order on one leg must not block the other). The shorts-first
            # gate above guarantees the concurrent batch is single-sided:
            # shorts-only while any short is open, longs-only once flat — so
            # concurrency never opens a naked-short window.
            tasks = []
            for p in positions:
                symbol = p.get("instrument_name", "")
                amt = float(p.get("amount", 0.0))
                if not symbol or amt == 0:
                    continue
                if shorts_present and amt > 0:
                    log.info("reflatten_hold_long_until_shorts_flat",
                             instrument=symbol, round=round_no)
                    continue
                tasks.append(_chase_leg(symbol, amt))

            any_progress = False
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                any_progress = any(
                    (r is not None and not isinstance(r, BaseException))
                    for r in results
                )

            try:
                positions = await self.exchange.list_open_positions()
            except Exception:
                log.warning("reflatten_requery_failed", round=round_no,
                            exc_info=True)
                await asyncio.sleep(5.0)
                continue

            if not positions:
                log.info("post_close_residual_cleared", rounds=round_no)
                await notifier.send(
                    f"<b>✅ POST-CLOSE RESIDUAL CLEARED</b>\n"
                    f"Flat after {round_no} re-flatten round(s)."
                )
                return positions

            if not any_progress:
                # No fills this round (e.g. no bid/ask on a dying 0DTE leg).
                # Wait for the book to move before the next round rather than
                # busy-looping the budget away.
                wait_sec = min(
                    30.0, max(0.0,
                              (deadline - now_utc()).total_seconds()),
                )
                if wait_sec > 0:
                    await asyncio.sleep(wait_sec)

        log.warning("post_close_residual_reflatten_exhausted",
                    rounds=round_no, remaining=len(positions))
        return positions

    async def _post_close_reconcile(self) -> None:
        """After unwind, verify exchange is actually flat. Alert on orphans."""
        # A re-flatten loop from an earlier session's close may still be
        # running (its budget can span the next window). It re-reads the
        # family-wide position set each round and will absorb any residual,
        # so a concurrent reconcile must skip — two loops chasing the same
        # instrument would oversell into a short.
        if self._reconcile_active:
            log.info("post_close_reconcile_skip_already_active")
            return
        self._reconcile_active = True
        try:
            await self._post_close_reconcile_inner()
        finally:
            self._reconcile_active = False

    async def _post_close_reconcile_inner(self) -> None:
        try:
            positions = await self.exchange.list_open_positions()
        except Exception:
            log.warning("post_close_reconcile_fetch_failed", exc_info=True)
            return

        if not positions:
            log.info("post_close_flat_ok")
            return

        # Residual after unwind — keep trying to close it (maker-only) before
        # locking. The lock is a genuine last resort, not the first response.
        positions = await self._flatten_residual_until_flat(positions)
        if not positions:
            return

        details = await self._fmt_positions_with_book(positions)
        log.warning("post_close_orphan_detected",
                    positions=len(positions), detail=details)
        # Lock entries. Reached only AFTER the persistent maker re-flatten
        # loop (CLOSE_FLATTEN_BUDGET_MIN) failed to clear the residual — a
        # genuine stuck state (e.g. no bid on a dying 0DTE leg). A running
        # algo must not keep firing on top of this (the 2026-06-18
        # wd_1400→wd_1430 stacking incident), so block every subsequent
        # entry until an operator flattens and restarts. The 08:00 UTC
        # expiry settles a worthless residual on its own.
        self._set_entry_lock(
            f"Post-close orphan: exchange still has {len(positions)} "
            f"open position(s) after {config.CLOSE_FLATTEN_BUDGET_MIN:.0f} "
            f"min of maker re-flatten",
            clearable_when_flat=True,
        )
        heal_note = (
            "This lock will AUTO-CLEAR on the next session once the exchange "
            "is flat again (e.g. a worthless 0DTE leg settling at 08:00 UTC "
            "expiry) — no restart needed. To resume sooner, flatten with "
            "tools/force_liquidate.py."
            if config.SELF_HEAL_LOCK_ON_FLAT else
            "<b>ACTION</b>: flatten the position(s) with "
            "tools/force_liquidate.py, then restart the algo to clear the "
            "lock. (A worthless 0DTE leg will also settle at 08:00 UTC "
            "expiry.)"
        )
        await notifier.send(
            f"<b>⚠️ POST-CLOSE ORPHAN — RE-FLATTEN EXHAUSTED</b>\n"
            f"Kept trying to close (maker-only) for "
            f"{config.CLOSE_FLATTEN_BUDGET_MIN:.0f} min but the exchange "
            f"still has {len(positions)} position(s):\n\n"
            f"{details}\n\n"
            f"<b>ENTRIES ARE NOW LOCKED</b> — the algo will NOT open new "
            f"straddles until this is resolved.\n"
            f"{heal_note}"
        )

    async def _finalize_deferred_close(
        self, equity_before: float, session_label: str,
    ) -> float | None:
        """Book the close for a straddle whose unwind DEFERRED finalization
        (couldn't confirm flat). Called after the post-close re-flatten, so the
        exit fills captured during unwind + re-flatten are used to compute the
        REAL P&L. Emits the SESSION CLOSE summary that hard_close skipped.

        Runs even if the re-flatten EXHAUSTED (still not flat / orphan-locked):
        the straddle must never be left open or every future entry blocks. For
        any leg with no recorded fill we fall back to the entry price (≈0 leg
        P&L, i.e. unrealised); the equity delta below is the honest headline.
        """
        s = self.portfolio.open_straddle
        if s is None:
            return None
        fills = s.exit_fills
        exit_call = fills.get(s.call_leg.instrument, s.entry_call_price)
        exit_put = fills.get(s.put_leg.instrument, s.entry_put_price)
        wing_call_exit = (
            fills.get(s.call_wing_leg.instrument)
            if s.call_wing_leg is not None else None
        )
        wing_put_exit = (
            fills.get(s.put_wing_leg.instrument)
            if s.put_wing_leg is not None else None
        )
        pnl = self.portfolio.close_straddle(
            exit_call, exit_put, "session_close",
            exit_call_wing_price=wing_call_exit,
            exit_put_wing_price=wing_put_exit,
        )
        # close_straddle bumped the ledger by a COMPUTED net; the wallet (which
        # includes any still-open leg's uPnL) is authoritative. Re-sync so the
        # SESSION CLOSE equity delta stays honest.
        if config.HAS_OKX_CREDS:
            try:
                live_equity = await self.exchange.get_account_equity()
                if live_equity > 0:
                    self.portfolio.sync_equity(live_equity)
            except Exception:
                log.warning("finalize_equity_resync_failed", exc_info=True)
        closed = self.portfolio.last_closed_straddle
        await notifier.notify_close(
            pnl, "session_close",
            session_label=session_label,
            straddle=closed,
            equity_before=equity_before,
            equity_after=self.portfolio.equity,
        )
        log.info("deferred_close_finalized", id=s.id, pnl=f"${pnl:,.2f}")
        return pnl

    # ──────────────────── Close ───────────────────────────────────

    async def _on_close(self, session: config.Session) -> None:
        label = session.time_label
        self._close_in_progress += 1
        try:
            equity_before = self.portfolio.equity
            pnl = await self.exit_mgr.hard_close(
                session_name=session.name, session_label=label,
            )

            if config.HAS_OKX_CREDS:
                live_equity = await self.exchange.get_account_equity()
                if live_equity > 0:
                    self.portfolio.sync_equity(live_equity)
                await self._post_close_reconcile()

            # TWO-PHASE FINALIZE: if the unwind DEFERRED (couldn't confirm flat)
            # the straddle is still open. The re-flatten above has now run, so
            # finalize the close with the REAL exit fills and emit the SESSION
            # CLOSE summary. Runs whether the re-flatten reached flat OR
            # exhausted (orphan-locked) — the straddle is NEVER left open, which
            # would block every future entry.
            if self.portfolio.has_open:
                finalize_pnl = await self._finalize_deferred_close(
                    equity_before, label,
                )
                if finalize_pnl is not None:
                    pnl = finalize_pnl

            actual_pnl = self.portfolio.equity - equity_before

            # Per-weekday LAST-CLOSE detection. Each trading_day weekday
            # has its own "last close" session — Tue-Sat trading_days
            # close on utc_0100, Sun/Mon trading_days (weekend strategy)
            # close on utc_2230. The daily report fires on the per-day
            # last close so weekend trading_days get reports too.
            from reporting.daily_report import (
                _load_trades,
                _trading_day_from_entry_time,
            )
            now_iso = now_utc().isoformat()
            trading_day = _trading_day_from_entry_time(
                now_iso, fallback_date=now_utc().strftime("%Y-%m-%d"),
            )
            try:
                td_dt = datetime.strptime(
                    trading_day, "%Y-%m-%d",
                ).date()
            except ValueError:
                td_dt = None

            is_last_close_of_day = (
                self._is_last_close_for_weekday(
                    session.name, td_dt.weekday(),
                ) if td_dt is not None else
                session.name == config.LAST_CLOSE_SESSION_NAME
            )

            if is_last_close_of_day:
                trades = _load_trades()
                day_trades = [
                    t for t in trades if t.trading_day == trading_day
                ]

                if day_trades:
                    # Send the comprehensive DAILY REPORT immediately
                    # after the trading-day's last close. The combined
                    # trading-day P&L breakdown is rendered inside.
                    try:
                        await notifier.send_daily_report(
                            self.portfolio.equity,
                            trading_day=trading_day,
                        )
                    except Exception:
                        log.warning(
                            "daily_report_chain_failed", exc_info=True,
                        )

                    # WEEKLY REPORT (Mon-Fri only) — fires on Sat 02:00
                    # UTC after utc_0100 Sat close, the LAST close of the
                    # last weekday trading_day. Weekend sessions are
                    # explicitly excluded from the weekly anchor so
                    # adding utc_1430/utc_2230 doesn't shift the weekly
                    # report timing or coverage. Weekend trades flow
                    # through send_weekend_recap below instead.
                    last_weekday_td = self._last_weekday_trading_day_weekday()
                    if td_dt is not None and \
                            td_dt.weekday() == last_weekday_td and \
                            session.name not in _WEEKEND_SESSION_NAMES:
                        try:
                            await notifier.send_weekly_report(
                                self.portfolio.equity,
                            )
                        except Exception:
                            log.warning(
                                "weekly_report_chain_failed",
                                exc_info=True,
                            )

                    # WEEKEND RECAP — fires on Mon 00:00 UTC after the
                    # Sun utc_2230 close. The trigger is "session is
                    # utc_2230 AND trading_day is Mon (weekday=0)" so
                    # the Sat utc_2230 close (Sun trading_day) does NOT
                    # double-fire. Operator can also re-run via
                    # tools/send_weekend_recap.py.
                    if (
                        session.name == _WEEKEND_RECAP_TRIGGER_SESSION
                        and td_dt is not None
                        and td_dt.weekday() == _WEEKEND_RECAP_TRIGGER_WEEKDAY
                    ):
                        try:
                            await notifier.send_weekend_recap(
                                self.portfolio.equity,
                            )
                        except Exception:
                            log.warning(
                                "weekend_recap_chain_failed",
                                exc_info=True,
                            )

                    # MONTH-END / YEAR-END deep reports — unchanged.
                    if td_dt is not None:
                        from reporting.period_metrics import (
                            is_last_trading_day_of_month,
                            is_last_trading_day_of_year,
                        )
                        if is_last_trading_day_of_month(td_dt):
                            try:
                                await notifier.send_month_end_report(
                                    self.portfolio.equity, trading_day,
                                )
                            except Exception:
                                log.warning(
                                    "month_end_report_chain_failed",
                                    exc_info=True,
                                )
                        if is_last_trading_day_of_year(td_dt):
                            try:
                                await notifier.send_year_end_report(
                                    self.portfolio.equity, trading_day,
                                )
                            except Exception:
                                log.warning(
                                    "year_end_report_chain_failed",
                                    exc_info=True,
                                )

            self.portfolio.reset_daily()
            log.info("session_close_done",
                     session=session.name,
                     is_last_close=is_last_close_of_day,
                     trading_day=trading_day,
                     trading_day_weekday=(
                         td_dt.weekday() if td_dt is not None else -1
                     ),
                     pnl=f"${pnl:,.2f}",
                     actual_pnl=f"${actual_pnl:,.2f}",
                     equity=f"${self.portfolio.equity:,.2f}")
        except Exception:
            log.error("close_error", session=session.name,
                      label=label, exc_info=True)
            await notifier.notify_error(
                f"Close [{label}]",
                "Unhandled exception — check logs",
            )
        finally:
            # Cleared only after reset_daily() has run, so a deferred
            # entry waiting on _wait_for_flat never races the position
            # wipe at the tail of this handler.
            self._close_in_progress = max(0, self._close_in_progress - 1)

    @staticmethod
    def _is_last_close_for_weekday(
        session_name: str, trading_day_weekday: int,
    ) -> bool:
        """True iff `session_name` is the chronologically last close
        producing a trading_day on the given UTC weekday.

        Walks every enabled session, finds those that map to the given
        trading_day weekday (via ``s.weekdays`` + ``trading_day_offset_days``),
        and picks the one with the largest ``close_minutes_in_trading_day``.
        For the canonical schedule:

            Tue trading_day (1) → utc_0100 (close 1080 min)
            Wed-Fri trading_days (2-4) → utc_0100
            Sat trading_day (5) → utc_0100
            Sun trading_day (6) → utc_2230 (close 960 min, only weekend
                                  sessions produce Sun trading_day)
            Mon trading_day (0) → utc_2230 (only weekend sessions produce
                                  Mon trading_day)

        Returns False if the weekday has no enabled session — defensive
        against the operator disabling everything for a given weekday.
        """
        candidates: list[config.Session] = []
        for s in config.SESSIONS:
            if not s.enabled:
                continue
            for d in s.weekdays:
                td_wd = (d + s.trading_day_offset_days) % 7
                if td_wd == trading_day_weekday:
                    candidates.append(s)
                    break
        if not candidates:
            return False
        last = max(
            candidates, key=lambda s: s.close_minutes_in_trading_day,
        )
        return last.name == session_name

    @staticmethod
    def _last_weekday_trading_day_weekday() -> int:
        """UTC weekday of the LAST WEEKDAY trading day (Mon-Fri only).

        Used to anchor the WEEKLY report. Excludes weekend sessions
        (utc_1430 / utc_2230) so the weekly report keeps firing Sat
        02:00 UTC regardless of whether weekend sessions are enabled.

        For the canonical schedule with utc_0100 Tue-Sat enabled this
        returns 5 (Sat). If the operator disables every weekday session
        that produces a Sat trading_day, the anchor falls back to the
        latest enabled weekday-only session.
        """
        weekdays_with_close: set[int] = set()
        for s in config.SESSIONS:
            if not s.enabled:
                continue
            if s.name in _WEEKEND_SESSION_NAMES:
                continue
            for d in s.weekdays:
                weekdays_with_close.add((d + s.trading_day_offset_days) % 7)
        return max(weekdays_with_close) if weekdays_with_close else 4

    @staticmethod
    def _last_trading_day_weekday() -> int:
        """LEGACY shim — kept for backward compat with any external caller.

        Returns the same value as ``_last_weekday_trading_day_weekday``
        so that historic call sites that gated weekly reports continue
        to work. New code should call the more-specific helper.
        """
        return Algo._last_weekday_trading_day_weekday()

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
