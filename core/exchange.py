"""
OKX exchange wrapper.

Provides async helpers around the (sync) python-okx SDK for:
  - Spot index price
  - Option chain ticker fetch (bulk)
  - Account balance & open positions
  - Maker-only order placement (post_only) with reject-on-cross
  - Maker-only chase: 50% bid-ask gap narrowing, fair-value cap, deadline
  - Cancel-all stale orders (called at startup)
  - Live instrument-metadata fetch (tick size, contract size) on connect

OKX BTC option naming: e.g.  BTC-USD-260418-65000-C
Contract size = 0.01 BTC (verified per instrument via get_instrument_meta).

UNIT CONVENTIONS for BTC-USD coin-margined inverse options:
  • Premium px is quoted in BTC, as a fraction of the underlying notional
    (e.g. 0.0065 BTC means 0.65% of the BTC underlying)
  • Tick size = 0.0005 BTC across the OKX BTC option family
  • To get USD premium per contract:  px × ctVal × spot
  • To get USD premium per BTC notional: px × spot
The previous codebase comment that premiums are "quoted in USD" was wrong
— this caused a critical bug where OPTION_TICK_SIZE=5.0 (USD) was added to
BTC-quoted bids, producing nonsense prices like 5.0055 BTC.

NOTE: RFQ / Block Trading is stubbed — see send_rfq() / send_rfq_sell().
Most retail accounts cannot use it, so leg-by-leg chase is the default.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

import config

log = structlog.get_logger(__name__)


def _utc_iso(t_unix: float) -> str:
    """Convert a unix timestamp to a UTC ISO8601 string."""
    return datetime.fromtimestamp(t_unix, tz=timezone.utc).isoformat()


async def _notify_chase_failure(
    *,
    side: str,
    instrument: str,
    qty_btc: float,
    reason: str,
    sCode: str = "",
    sMsg: str = "",
    attempt: int = 0,
) -> None:
    """Telegram alert for any chase that returns None.

    Imported lazily to avoid an import cycle (notifier imports nothing from
    exchange today, but this future-proofs it). Failures inside the notifier
    must never propagate up — the caller is already on a critical error path.
    """
    try:
        from core import notifier
        title = (
            "CHASE FATAL REJECT" if reason == "fatal_reject"
            else "CHASE DEADLINE EXHAUSTED"
        )
        body_lines = [
            f"<b>{title}</b>",
            f"Side: {side.upper()}",
            f"Instrument: {instrument}",
            f"Qty (BTC): {qty_btc}",
            f"Attempts: {attempt}",
        ]
        if sCode:
            body_lines.append(f"OKX sCode: {sCode}")
        if sMsg:
            body_lines.append(f"OKX msg: {sMsg}")
        body_lines.append("")
        if reason == "fatal_reject":
            body_lines.append(
                "Order was rejected by OKX with a non-recoverable code. "
                "Check account/margin/td_mode."
            )
        else:
            body_lines.append(
                "Maker-only chase exhausted its time budget without filling. "
                "If this was an entry leg, the session is being aborted."
            )
        await notifier.send("\n".join(body_lines))
    except Exception:
        log.warning("chase_failure_notify_skipped", exc_info=True)


def _build_fill_metrics(
    *,
    side: str,
    instrument: str,
    qty_btc: float,
    fill_price: float,
    t_started: float,
    t_filled: float,
    attempts: int,
    ref_bid: float,
    ref_ask: float,
    ref_mark: float,
) -> dict:
    """
    Build the fill-quality metrics dict that flows from chase_buy/sell
    back to the straddle builder/exit manager and ultimately into the
    daily report.

    Slippage is positive when we paid more than mark (buys) or
    received less than mark (sells) — i.e. execution worse than fair
    value. Negative = better than fair value.
    """
    duration = max(0.0, t_filled - t_started)
    ref_mid = (ref_bid + ref_ask) / 2 if ref_bid > 0 and ref_ask > 0 else 0.0

    if side.lower() in ("buy", "b"):
        slip_mark = ((fill_price - ref_mark) / ref_mark
                     if ref_mark > 0 else 0.0)
        slip_mid = ((fill_price - ref_mid) / ref_mid
                    if ref_mid > 0 else 0.0)
        # As a maker buy, the taker alternative was paying the ask.
        taker_price = ref_ask
        saved_per_btc = (ref_ask - fill_price) if ref_ask > 0 else 0.0
        saved_pct = (saved_per_btc / ref_ask) if ref_ask > 0 else 0.0
    else:  # sell
        slip_mark = ((ref_mark - fill_price) / ref_mark
                     if ref_mark > 0 else 0.0)
        slip_mid = ((ref_mid - fill_price) / ref_mid
                    if ref_mid > 0 else 0.0)
        # As a maker sell, the taker alternative was hitting the bid.
        taker_price = ref_bid
        saved_per_btc = (fill_price - ref_bid) if ref_bid > 0 else 0.0
        saved_pct = (saved_per_btc / ref_bid) if ref_bid > 0 else 0.0

    return {
        "instrument": instrument,
        "side": side,
        "qty_btc": qty_btc,
        "t_started_iso": _utc_iso(t_started),
        "t_filled_iso": _utc_iso(t_filled),
        "duration_sec": round(duration, 2),
        "attempts": attempts,
        "ref_bid": round(ref_bid, 4),
        "ref_ask": round(ref_ask, 4),
        "ref_mid": round(ref_mid, 4),
        "ref_mark": round(ref_mark, 4),
        "fill_price": round(fill_price, 4),
        "slippage_vs_mark_pct": round(slip_mark * 100, 4),
        "slippage_vs_mid_pct": round(slip_mid * 100, 4),
        "taker_price_at_start": round(taker_price, 4),
        "saved_vs_taker_per_btc_usd": round(saved_per_btc, 4),
        "saved_vs_taker_pct": round(saved_pct * 100, 4),
        "saved_vs_taker_total_usd": round(saved_per_btc * qty_btc, 2),
    }


# ─────────────────────────── Data classes ────────────────────────────

@dataclass
class Ticker:
    instrument: str
    bid: float = 0.0
    ask: float = 0.0
    mark: float = 0.0
    last: float = 0.0


# ─────────────────────────── Exchange ────────────────────────────────

class OKXExchange:
    """Thin async wrapper around python-okx sync clients."""

    def __init__(self) -> None:
        self._market = None
        self._public = None
        self._trade = None
        self._account = None
        self._connected: bool = False
        self.error_count: int = 0
        # Per-instrument metadata cache populated by prime_instrument_meta /
        # get_instrument_meta. Each entry: {tickSz, ctVal, lotSz, minSz}
        self._inst_meta: dict[str, dict] = {}
        # Default tick size for the BTC option family. Seeded from config but
        # overwritten on startup via prime_option_tick_size() with the live
        # value from /api/v5/public/instruments. Used by chase_buy/chase_sell
        # when an instrument-specific tick isn't cached.
        self._default_option_tick: float = config.OPTION_TICK_SIZE

    # ──────────────────── Connection ──────────────────────────────

    def connect(self) -> None:
        """Initialise OKX SDK clients."""
        try:
            from okx import MarketData, PublicData, Trade, Account
        except ImportError as exc:
            log.error("python_okx_missing",
                      hint="pip install python-okx",
                      exc_info=True)
            raise RuntimeError("python-okx not installed") from exc

        # Block Trading is optional — only import when USE_RFQ is on.
        BlockTrading = None
        if config.USE_RFQ:
            try:
                from okx import BlockTrading as _BT
                BlockTrading = _BT
            except ImportError:
                log.warning("python_okx_block_trading_missing",
                            note="upgrade python-okx to enable RFQ")

        creds_ok = bool(
            config.OKX_API_KEY and config.OKX_API_SECRET
            and config.OKX_PASSPHRASE
        )
        if not creds_ok and not config.DRY_RUN:
            raise RuntimeError(
                "Missing OKX credentials — set OKX_API_KEY, "
                "OKX_API_SECRET, OKX_PASSPHRASE in .env (or set "
                "DRY_RUN=true to smoke-test without credentials)"
            )

        flag = config.OKX_FLAG  # "0"=live, "1"=demo
        domain = config.OKX_DOMAIN
        self._market = MarketData.MarketAPI(
            flag=flag, domain=domain, debug=False,
        )
        self._public = PublicData.PublicAPI(
            flag=flag, domain=domain, debug=False,
        )
        self._trade = Trade.TradeAPI(
            config.OKX_API_KEY or "x",
            config.OKX_API_SECRET or "x",
            config.OKX_PASSPHRASE or "x",
            False, flag=flag, domain=domain, debug=False,
        )
        self._account = Account.AccountAPI(
            config.OKX_API_KEY or "x",
            config.OKX_API_SECRET or "x",
            config.OKX_PASSPHRASE or "x",
            False, flag=flag, domain=domain, debug=False,
        )
        self._block = None
        if BlockTrading is not None:
            try:
                self._block = BlockTrading.BlockTradingAPI(
                    config.OKX_API_KEY or "x",
                    config.OKX_API_SECRET or "x",
                    config.OKX_PASSPHRASE or "x",
                    False, flag=flag, domain=domain, debug=False,
                )
            except Exception:
                log.warning("block_trading_init_failed", exc_info=True)
                self._block = None
        self._connected = True
        log.info("okx_connected",
                 mode="DEMO" if flag == "1" else "LIVE",
                 domain=domain,
                 dry_run=config.DRY_RUN,
                 rfq=config.USE_RFQ and self._block is not None)

    # ──────────────────── Internal call helper ────────────────────

    async def _call(self, fn, *args, **kwargs) -> dict:
        """Run a sync SDK call in a thread; bump error_count on failure."""
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception:
            self.error_count += 1
            log.error("okx_call_failed", fn=fn.__name__, exc_info=True)
            raise

    @staticmethod
    def _data_or_empty(resp: Any) -> list:
        if not isinstance(resp, dict):
            return []
        if resp.get("code") not in ("0", 0, None):
            log.warning("okx_response_error", code=resp.get("code"),
                        msg=resp.get("msg"))
            return []
        return resp.get("data") or []

    @staticmethod
    def _f(d: dict, key: str, default: float = 0.0) -> float:
        v = d.get(key)
        if v is None or v == "":
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    # ──────────────────── Market data ─────────────────────────────

    async def get_spot_price(self) -> float:
        """Return BTC index price (USD)."""
        resp = await self._call(
            self._market.get_index_tickers,
            instId=f"{config.BASE_COIN}-{config.QUOTE_COIN}",
        )
        rows = self._data_or_empty(resp)
        if not rows:
            log.warning("spot_price_empty")
            return 0.0
        return self._f(rows[0], "idxPx")

    async def get_ticker(self, instrument: str) -> Ticker:
        """Single-instrument bid/ask/last."""
        resp = await self._call(self._market.get_ticker, instId=instrument)
        rows = self._data_or_empty(resp)
        if not rows:
            return Ticker(instrument=instrument)
        r = rows[0]
        return Ticker(
            instrument=instrument,
            bid=self._f(r, "bidPx"),
            ask=self._f(r, "askPx"),
            last=self._f(r, "last"),
            mark=self._f(r, "last"),  # OKX ticker has no mark; use last
        )

    async def get_tickers_for_underlying(
        self, underlying: str,
    ) -> dict[str, Ticker]:
        """Bulk fetch all option tickers for an underlying (e.g. BTC-USD)."""
        resp = await self._call(
            self._market.get_tickers,
            instType="OPTION", uly=underlying,
        )
        rows = self._data_or_empty(resp)
        out: dict[str, Ticker] = {}
        for r in rows:
            inst = r.get("instId")
            if not inst:
                continue
            out[inst] = Ticker(
                instrument=inst,
                bid=self._f(r, "bidPx"),
                ask=self._f(r, "askPx"),
                last=self._f(r, "last"),
                mark=self._f(r, "last"),
            )
        return out

    async def get_option_mark_price(self, instrument: str) -> float:
        """Fetch OKX option mark price (separate endpoint from ticker)."""
        resp = await self._call(
            self._public.get_mark_price,
            instType="OPTION", instId=instrument,
        )
        rows = self._data_or_empty(resp)
        if not rows:
            return 0.0
        return self._f(rows[0], "markPx")

    # ──────────────────── Instrument metadata ─────────────────────

    async def get_instrument_meta(self, instrument: str) -> dict:
        """
        Fetch and cache per-instrument metadata (tickSz, ctVal, lotSz, minSz)
        from /api/v5/public/instruments. Returns empty dict on failure so
        callers can fall back to defaults.
        """
        cached = self._inst_meta.get(instrument)
        if cached is not None:
            return cached

        resp = await self._call(
            self._public.get_instruments,
            instType="OPTION", instId=instrument,
        )
        rows = self._data_or_empty(resp)
        if not rows:
            return {}

        r = rows[0]
        meta = {
            "tickSz": self._f(r, "tickSz"),
            "ctVal": self._f(r, "ctVal"),
            "lotSz": self._f(r, "lotSz"),
            "minSz": self._f(r, "minSz"),
        }
        self._inst_meta[instrument] = meta
        return meta

    async def prime_option_tick_size(
        self, sample_underlying: str = "",
    ) -> float:
        """
        Read the live tick size for the BTC option family from OKX and
        update self._default_option_tick. Returns the tick size in use.

        We query the full instrument list for instType=OPTION+uly=<base-quote>
        and pick the first row's tickSz. OKX BTC options share a common tick
        across strikes, so any sample is fine.
        """
        underlying = (
            sample_underlying
            or f"{config.BASE_COIN}-{config.QUOTE_COIN}"
        )
        try:
            resp = await self._call(
                self._public.get_instruments,
                instType="OPTION", uly=underlying,
            )
        except Exception:
            log.warning("prime_tick_failed",
                        underlying=underlying, exc_info=True)
            return self._default_option_tick

        rows = self._data_or_empty(resp)
        if not rows:
            log.warning("prime_tick_empty",
                        underlying=underlying,
                        fallback=self._default_option_tick)
            return self._default_option_tick

        # Cache every row while we have the data — saves later RTTs.
        # Only count rows that are *real options* (instId ends in "-C" or
        # "-P" and matches the BASE-QUOTE-EXPIRY-STRIKE-{C|P} shape) toward
        # the default-tick computation. OKX sometimes returns non-option
        # entries when instType+uly are loose; on 2026-05-07 this caused
        # _default_option_tick to be set to 5.0 (futures tick) which would
        # have been a latent footgun if the per-instrument cache missed.
        ticks: list[float] = []
        ct_vals: list[float] = []
        option_rows = 0
        for r in rows:
            inst = r.get("instId")
            if not inst:
                continue
            meta = {
                "tickSz": self._f(r, "tickSz"),
                "ctVal": self._f(r, "ctVal"),
                "lotSz": self._f(r, "lotSz"),
                "minSz": self._f(r, "minSz"),
            }
            self._inst_meta[inst] = meta

            # Filter for genuine option instruments (ends in -C or -P)
            parts = inst.split("-")
            is_option = (
                len(parts) == 5 and parts[-1] in ("C", "P")
                and (r.get("instType") or "OPTION") == "OPTION"
            )
            if not is_option:
                continue
            option_rows += 1
            if meta["tickSz"] > 0:
                ticks.append(meta["tickSz"])
            if meta["ctVal"] > 0:
                ct_vals.append(meta["ctVal"])

        if ticks:
            # Use the most common tick — OKX BTC options are uniform
            # 0.0001 across the family, so this should be unambiguous.
            from collections import Counter
            most_common_tick = Counter(ticks).most_common(1)[0][0]
            self._default_option_tick = most_common_tick

        live_ct = ct_vals[0] if ct_vals else 0.0

        # Sanity: an option tick > 0.01 BTC is almost certainly wrong (real
        # OKX BTC options are 0.0001). If we got something nonsensical,
        # refuse to override the sensible config default.
        if self._default_option_tick > 0.01:
            log.warning("prime_tick_implausible",
                        candidate=self._default_option_tick,
                        action="keeping config fallback",
                        config_value=config.OPTION_TICK_SIZE)
            self._default_option_tick = config.OPTION_TICK_SIZE

        log.info("instrument_meta_primed",
                 underlying=underlying,
                 total_rows=len(rows),
                 option_rows=option_rows,
                 tick_size=self._default_option_tick,
                 contract_size=live_ct)

        # Sanity: if OKX advertises a different contract size than our config,
        # warn loudly so the operator can fix .env.
        if live_ct > 0 and abs(live_ct - config.OKX_CONTRACT_SIZE_BTC) > 1e-9:
            log.warning("contract_size_mismatch",
                        config=config.OKX_CONTRACT_SIZE_BTC,
                        live=live_ct,
                        action="update OKX_CONTRACT_SIZE_BTC in .env")

        return self._default_option_tick

    def get_tick_size(self, instrument: str = "") -> float:
        """Per-instrument tick size if cached, else the family default."""
        if instrument:
            meta = self._inst_meta.get(instrument)
            if meta and meta.get("tickSz", 0) > 0:
                return meta["tickSz"]
        return self._default_option_tick

    # ──────────────────── Account / positions ─────────────────────

    async def get_account_equity(self, ccy: str = "USDT") -> float:
        """Return total trading-account equity in `ccy`."""
        resp = await self._call(self._account.get_account_balance, ccy=ccy)
        rows = self._data_or_empty(resp)
        if not rows:
            return 0.0
        details = rows[0].get("details") or []
        for d in details:
            if d.get("ccy") == ccy:
                return self._f(d, "eq")
        return self._f(rows[0], "totalEq")

    async def list_open_positions(self) -> list[dict]:
        """List all option positions for BTC-USD.

        python-okx >=0.4.1 dropped the `uly=` parameter from
        `Account.get_positions`. We now fetch all OPTION positions and
        filter to BASE_COIN-QUOTE_COIN in code.
        """
        resp = await self._call(
            self._account.get_positions,
            instType="OPTION",
        )
        rows = self._data_or_empty(resp)
        family_prefix = f"{config.BASE_COIN}-{config.QUOTE_COIN}-"
        out = []
        for r in rows:
            pos = self._f(r, "pos")
            if pos == 0:
                continue
            inst = r.get("instId", "")
            if not inst.startswith(family_prefix):
                continue
            out.append({
                "instrument_name": inst,
                "amount": pos,
                "average_price": self._f(r, "avgPx"),
                "mark_price": self._f(r, "markPx"),
                "unrealized_pnl": self._f(r, "upl"),
            })
        return out

    async def get_option_position(self, instrument: str) -> float:
        """Return signed quantity (in contracts) of a single option position."""
        resp = await self._call(
            self._account.get_positions,
            instType="OPTION", instId=instrument,
        )
        rows = self._data_or_empty(resp)
        for r in rows:
            if r.get("instId") == instrument:
                return self._f(r, "pos")
        return 0.0

    # ──────────────────── Order management ────────────────────────

    async def list_open_orders(self) -> list[dict]:
        """List all currently open option orders."""
        resp = await self._call(
            self._trade.get_order_list,
            instType="OPTION",
        )
        rows = self._data_or_empty(resp)
        return rows

    async def cancel_all_open_orders(self) -> int:
        """Cancel every resting option order. Returns count cancelled."""
        orders = await self.list_open_orders()
        if not orders:
            return 0

        cancelled = 0
        for o in orders:
            inst = o.get("instId")
            oid = o.get("ordId")
            if not inst or not oid:
                continue
            try:
                resp = await self._call(
                    self._trade.cancel_order, instId=inst, ordId=oid,
                )
                if resp.get("code") in ("0", 0):
                    cancelled += 1
                else:
                    log.warning("cancel_failed",
                                instId=inst, ordId=oid,
                                code=resp.get("code"), msg=resp.get("msg"))
            except Exception:
                log.warning("cancel_exception", instId=inst, ordId=oid,
                            exc_info=True)
        log.info("startup_orders_cancelled",
                 total=len(orders), cancelled=cancelled)
        return cancelled

    async def get_order_status(self, instrument: str, order_id: str) -> dict:
        """Fetch latest status for a single order."""
        resp = await self._call(
            self._trade.get_order,
            instId=instrument, ordId=order_id,
        )
        rows = self._data_or_empty(resp)
        return rows[0] if rows else {}

    # ──────────────────── Order placement ─────────────────────────

    def _qty_to_contracts(self, qty_btc: float) -> str:
        """Convert BTC quantity to OKX contract count (rounded)."""
        contracts = qty_btc / config.OKX_CONTRACT_SIZE_BTC
        return str(int(round(contracts)))

    async def _place_limit_order(
        self,
        instrument: str,
        side: str,   # "buy" or "sell"
        qty_btc: float,
        price: float,
        post_only: bool = True,
    ) -> dict:
        """
        Place a single limit order.

        post_only=True means the order is rejected if it would cross — this
        guarantees maker fills (no taker fees, no slippage from crossing).
        """
        if config.DRY_RUN:
            log.info("dry_run_order",
                     instrument=instrument, side=side,
                     qty_btc=qty_btc, price=price, post_only=post_only)
            return {
                "ordId": f"dry-{int(time.time() * 1000)}",
                "sCode": "0", "sMsg": "dry_run",
            }

        sz = self._qty_to_contracts(qty_btc)
        ord_type = "post_only" if post_only else "limit"

        # OKX OPTIONS only accept tdMode in {"cross", "isolated"}.
        # `cash` is for SPOT trading. Using cash here previously caused
        # the API to reject every order with the wrapper error
        # `code=1 / msg="All operations failed"`.
        td_mode = config.OKX_TD_MODE  # "cross" or "isolated"

        resp = await self._call(
            self._trade.place_order,
            instId=instrument,
            tdMode=td_mode,
            side=side,
            ordType=ord_type,
            sz=sz,
            px=str(price),
        )

        # OKX returns code=1 / msg="All operations failed" as a wrapper
        # whenever ANY leg fails, with the real reason in data[0].sCode /
        # data[0].sMsg. We must read the inner row regardless of the
        # outer code so the chase loop can react correctly.
        outer_code = str(resp.get("code") or "")
        outer_msg = resp.get("msg") or ""
        rows = (resp.get("data") or []) if isinstance(resp, dict) else []
        if not rows:
            log.warning("order_no_data",
                        instrument=instrument, side=side,
                        outer_code=outer_code, outer_msg=outer_msg)
            return {"sCode": outer_code or "no_data", "sMsg": outer_msg}
        r = rows[0]
        log.info("order_placed",
                 instrument=instrument, side=side, qty_btc=qty_btc,
                 sz=sz, price=price, post_only=post_only,
                 td_mode=td_mode,
                 outer_code=outer_code, outer_msg=outer_msg,
                 ord_id=r.get("ordId"), sCode=r.get("sCode"),
                 sMsg=r.get("sMsg"))
        return r

    async def _wait_for_fill(
        self, instrument: str, order_id: str, timeout: float,
    ) -> dict:
        """Poll order status until filled / cancelled / timeout.

        Race-condition guard: when OKX reports state=canceled, an order can
        STILL have a non-zero accFillSz (partial or full fill that landed
        microseconds before our cancel landed). In that case we promote the
        status to ``filled`` so the caller doesn't drop a real position on
        the floor (orphan-position scenario, 2026-05-07).
        """
        deadline = time.time() + timeout
        last: dict = {}
        while time.time() < deadline:
            try:
                last = await self.get_order_status(instrument, order_id)
                state = last.get("state", "")
                acc_fill = self._f(last, "accFillSz")
                if state == "filled":
                    return last
                if state in ("canceled", "cancelled"):
                    if acc_fill > 0:
                        log.warning("wait_for_fill_canceled_but_filled",
                                    instrument=instrument, order_id=order_id,
                                    accFillSz=acc_fill,
                                    avgPx=last.get("avgPx"))
                        last = dict(last)
                        last["state"] = "filled"
                    return last
            except Exception:
                log.warning("order_status_failed", exc_info=True)
            await asyncio.sleep(1.0)
        # Final post-deadline sanity check: catch a fill that landed during
        # the last sleep window before we return "no fill".
        try:
            last = await self.get_order_status(instrument, order_id)
            if self._f(last, "accFillSz") > 0:
                last = dict(last)
                last["state"] = "filled"
                log.warning("wait_for_fill_late_fill_detected",
                            instrument=instrument, order_id=order_id,
                            accFillSz=last.get("accFillSz"),
                            avgPx=last.get("avgPx"))
        except Exception:
            pass
        return last

    # ──────────────────── Maker-only chase (BUY) ──────────────────

    async def chase_buy(
        self, instrument: str, qty_btc: float, initial_bid: float,
    ) -> Optional[dict]:
        """
        Maker-only buy chase: walks toward the ask by narrowing the gap by
        OPTION_CHASE_GAP_NARROW_PCT each retry, never crossing past
        mark × OPTION_CHASE_MAX_SLIPPAGE_FACTOR. Bails on deadline.

        Tight-spread handling: if (bid + tick) ≥ ask (1-tick wide market,
        common in liquid 0DTE options), the floor collapses to `bid` so we
        place a non-crossing maker bid AT the bid level instead of getting
        stuck rejecting against the ask forever.

        Returns dict with average_price + order_id + metrics on full fill,
        else None. The `metrics` dict captures execution-quality data:
            t_started_iso, t_filled_iso, duration_sec, attempts,
            ref_bid, ref_ask, ref_mark, ref_mid,
            fill_price, qty_btc, side, instrument,
            slippage_vs_mark_pct, slippage_vs_mid_pct,
            taker_price_at_start (the ask), saved_vs_taker_pct,
            saved_vs_taker_per_btc_usd  (premium in BTC per BTC notional)
        """
        deadline = time.time() + config.OPTION_CHASE_DEADLINE_MIN * 60
        attempt = 0
        last_price = max(0.0, initial_bid)
        t_started = time.time()
        ref_bid = ref_ask = ref_mark = 0.0
        captured_ref = False
        tick = self.get_tick_size(instrument)
        if tick <= 0:
            tick = config.OPTION_TICK_SIZE

        while time.time() < deadline:
            attempt += 1
            ticker = await self.get_ticker(instrument)
            mark = await self.get_option_mark_price(instrument)
            if mark <= 0:
                mark = ticker.last if ticker.last > 0 else ticker.ask

            bid, ask = ticker.bid, ticker.ask
            if ask <= 0:
                log.warning("chase_no_ask",
                            instrument=instrument, attempt=attempt,
                            bid=bid, ask=ask)
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            # Capture the very first usable market state — this is the
            # "decision-time" reference for slippage and savings metrics.
            if not captured_ref:
                ref_bid, ref_ask, ref_mark = bid, ask, mark
                captured_ref = True

            # If bid is missing (empty bid side, common on demo), seed it
            # using mark so we can still place a maker bid below ask.
            effective_bid = bid if bid > 0 else max(
                mark - tick, tick,
            )

            # 50% gap-narrowing: narrow remaining gap to (ask − tick) by pct
            target_top = max(effective_bid, ask - tick)
            new_price = last_price + (target_top - last_price) \
                * config.OPTION_CHASE_GAP_NARROW_PCT

            # Floor: prefer one tick above effective_bid (front of bid queue).
            # But in tight 1-tick-wide spreads, that floor lands AT the ask
            # which post_only would reject every time. In that case, drop
            # the floor to effective_bid (queue at bid level instead).
            improvement_floor = effective_bid + tick
            if improvement_floor >= ask:
                # Tight spread: queue at bid; still maker, just slower.
                floor_price = effective_bid
            else:
                floor_price = improvement_floor
            new_price = max(new_price, floor_price)

            # Hard ceiling: never AT or above ask (would cross → post_only
            # reject loop). Stay at least one tick below.
            ceiling_price = ask - tick
            if ceiling_price < effective_bid:
                # Tight spread again — settle at effective_bid.
                ceiling_price = effective_bid
            new_price = min(new_price, ceiling_price)

            # Fair-value cap: never bid above mark × max_slippage_factor
            max_price = mark * config.OPTION_CHASE_MAX_SLIPPAGE_FACTOR
            if new_price > max_price:
                log.warning("chase_buy_cap_hit",
                            instrument=instrument, new_price=new_price,
                            mark=mark, max_price=max_price, attempt=attempt)
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            new_price = round(new_price / tick) * tick
            last_price = new_price

            log.info("chase_buy_attempt",
                     instrument=instrument, attempt=attempt,
                     price=new_price, bid=bid, ask=ask, mark=mark,
                     tick=tick)

            order = await self._place_limit_order(
                instrument, "buy", qty_btc, new_price, post_only=True,
            )
            ord_id = order.get("ordId")
            sCode = str(order.get("sCode") or "")
            sMsg = str(order.get("sMsg") or "")

            # Post-only rejected (would cross) → narrow more next loop
            # OKX docs: 51120 = "Order would immediately match" (post-only).
            # 51008 is *insufficient margin*, NOT post-only — fatal.
            if sCode == "51120" or "would immediately match" in sMsg.lower() \
                    or "post_only" in sMsg.lower():
                log.info("chase_buy_post_only_rejected",
                         instrument=instrument, attempt=attempt)
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            # Structural / config errors: bail immediately, don't burn
            # API rate-limit retrying something that will never succeed.
            FATAL_CODES = {
                "51000",   # Parameter error
                "51001",   # Instrument doesn't exist
                "51008",   # Insufficient {ccy} margin (BTC for inverse options!)
                "51010",   # tdMode/instType incompatible
                "51016",   # Insufficient balance (general)
                "51019",   # Net long not allowed under cross margin (use isolated)
                "51020",   # Account in restricted mode
                "51115",   # Margin mode not enabled / account-mode wrong
                "51121",   # Position direction restriction
                "51169",   # Pricing limit
                "51198",   # Options trading not yet activated by user
            }
            if sCode in FATAL_CODES:
                log.error("chase_buy_fatal_reject",
                          instrument=instrument, sCode=sCode, sMsg=sMsg,
                          attempt=attempt,
                          hint="check OKX_TD_MODE / account margin mode / balance")
                await _notify_chase_failure(
                    side="buy", instrument=instrument,
                    qty_btc=qty_btc, reason="fatal_reject",
                    sCode=sCode, sMsg=sMsg, attempt=attempt,
                )
                # Return None so straddle_builder treats this as a real
                # failure and skips the session — NEVER fall through with
                # a placeholder price (would create a phantom position).
                return None

            if not ord_id or sCode not in ("0", ""):
                log.warning("chase_buy_order_rejected",
                            instrument=instrument, sCode=sCode, sMsg=sMsg,
                            attempt=attempt)
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            # Wait for fill
            status = await self._wait_for_fill(
                instrument, ord_id, config.OPTION_CHASE_INTERVAL_SEC,
            )
            state = status.get("state", "")
            if state == "filled":
                avg_px = self._f(status, "avgPx", default=new_price)
                t_filled = time.time()
                metrics = _build_fill_metrics(
                    side="buy",
                    instrument=instrument,
                    qty_btc=qty_btc,
                    fill_price=avg_px,
                    t_started=t_started,
                    t_filled=t_filled,
                    attempts=attempt,
                    ref_bid=ref_bid, ref_ask=ref_ask, ref_mark=ref_mark,
                )
                log.info("chase_buy_filled",
                         instrument=instrument, avg=avg_px, attempt=attempt,
                         duration_sec=metrics["duration_sec"],
                         slippage_vs_mark_pct=metrics["slippage_vs_mark_pct"],
                         saved_vs_taker_total_usd=metrics["saved_vs_taker_total_usd"])
                return {
                    "average_price": avg_px,
                    "order_id": ord_id,
                    "avgPrice": avg_px,
                    "metrics": metrics,
                }
            # Cancel the resting order before placing the next one
            try:
                await self._call(
                    self._trade.cancel_order,
                    instId=instrument, ordId=ord_id,
                )
            except Exception:
                pass

        log.error("chase_buy_deadline_exhausted", instrument=instrument)
        await _notify_chase_failure(
            side="buy", instrument=instrument,
            qty_btc=qty_btc, reason="deadline_exhausted",
            sCode="", sMsg="", attempt=attempt,
        )
        return None

    # ──────────────────── Maker-only chase (SELL) ─────────────────

    async def chase_sell(
        self, instrument: str, qty_btc: float, initial_ask: float,
    ) -> Optional[dict]:
        """
        Maker-only sell chase: walks toward the bid by narrowing the gap by
        OPTION_CHASE_GAP_NARROW_PCT each retry, never below
        mark / OPTION_CHASE_MAX_SLIPPAGE_FACTOR. Bails on deadline.

        Tight-spread handling: if (ask − tick) ≤ bid (1-tick wide market),
        the ceiling collapses to `ask` so we place a non-crossing maker
        offer AT the ask level instead of looping forever rejecting.
        """
        deadline = time.time() + config.OPTION_CHASE_DEADLINE_MIN * 60
        attempt = 0
        last_price = max(0.0, initial_ask)
        t_started = time.time()
        ref_bid = ref_ask = ref_mark = 0.0
        captured_ref = False
        tick = self.get_tick_size(instrument)
        if tick <= 0:
            tick = config.OPTION_TICK_SIZE

        while time.time() < deadline:
            attempt += 1
            ticker = await self.get_ticker(instrument)
            mark = await self.get_option_mark_price(instrument)
            if mark <= 0:
                mark = ticker.last if ticker.last > 0 else ticker.bid

            bid, ask = ticker.bid, ticker.ask
            if bid <= 0 and mark <= 0:
                log.warning("chase_no_bid_or_mark",
                            instrument=instrument, attempt=attempt)
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            # Capture decision-time market state for slippage metrics.
            if not captured_ref:
                ref_bid, ref_ask, ref_mark = bid, ask, mark
                captured_ref = True

            # If ask is missing (empty offer side), seed it using mark
            # so we can still place a maker offer above bid.
            effective_ask = ask if ask > 0 else max(mark + tick, tick * 2)
            effective_bid = bid if bid > 0 else max(mark - tick, tick)

            target_bot = min(effective_ask, effective_bid + tick)
            new_price = last_price - (last_price - target_bot) \
                * config.OPTION_CHASE_GAP_NARROW_PCT

            # Ceiling: ideally one tick below effective_ask (front of ask queue).
            # In tight 1-tick spreads that lands AT the bid; collapse to the
            # ask level so we still post a non-crossing offer.
            improvement_ceiling = effective_ask - tick
            if improvement_ceiling <= effective_bid:
                ceiling_price = effective_ask
            else:
                ceiling_price = improvement_ceiling
            new_price = min(new_price, ceiling_price)

            # Floor: never AT or below bid (would cross → post_only reject).
            floor_price = effective_bid + tick
            if floor_price > effective_ask:
                floor_price = effective_ask
            new_price = max(new_price, floor_price)

            min_price = mark / config.OPTION_CHASE_MAX_SLIPPAGE_FACTOR
            if new_price < min_price:
                log.warning("chase_sell_floor_hit",
                            instrument=instrument, new_price=new_price,
                            mark=mark, min_price=min_price, attempt=attempt)
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            new_price = round(new_price / tick) * tick
            last_price = new_price

            log.info("chase_sell_attempt",
                     instrument=instrument, attempt=attempt,
                     price=new_price, bid=bid, ask=ask, mark=mark,
                     tick=tick)

            order = await self._place_limit_order(
                instrument, "sell", qty_btc, new_price, post_only=True,
            )
            ord_id = order.get("ordId")
            sCode = str(order.get("sCode") or "")
            sMsg = str(order.get("sMsg") or "")

            if sCode == "51120" or "would immediately match" in sMsg.lower() \
                    or "post_only" in sMsg.lower():
                log.info("chase_sell_post_only_rejected",
                         instrument=instrument, attempt=attempt)
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            FATAL_CODES = {
                "51000", "51001", "51008", "51010", "51016", "51019",
                "51020", "51115", "51121", "51169", "51198",
            }
            if sCode in FATAL_CODES:
                log.error("chase_sell_fatal_reject",
                          instrument=instrument, sCode=sCode, sMsg=sMsg,
                          attempt=attempt)
                await _notify_chase_failure(
                    side="sell", instrument=instrument,
                    qty_btc=qty_btc, reason="fatal_reject",
                    sCode=sCode, sMsg=sMsg, attempt=attempt,
                )
                # Return None — caller (unwind logic) will retry / alert.
                return None

            if not ord_id or sCode not in ("0", ""):
                log.warning("chase_sell_order_rejected",
                            instrument=instrument, sCode=sCode, sMsg=sMsg,
                            attempt=attempt)
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            status = await self._wait_for_fill(
                instrument, ord_id, config.OPTION_CHASE_INTERVAL_SEC,
            )
            state = status.get("state", "")
            if state == "filled":
                avg_px = self._f(status, "avgPx", default=new_price)
                t_filled = time.time()
                metrics = _build_fill_metrics(
                    side="sell",
                    instrument=instrument,
                    qty_btc=qty_btc,
                    fill_price=avg_px,
                    t_started=t_started,
                    t_filled=t_filled,
                    attempts=attempt,
                    ref_bid=ref_bid, ref_ask=ref_ask, ref_mark=ref_mark,
                )
                log.info("chase_sell_filled",
                         instrument=instrument, avg=avg_px, attempt=attempt,
                         duration_sec=metrics["duration_sec"],
                         slippage_vs_mark_pct=metrics["slippage_vs_mark_pct"],
                         saved_vs_taker_total_usd=metrics["saved_vs_taker_total_usd"])
                return {
                    "average_price": avg_px,
                    "order_id": ord_id,
                    "avgPrice": avg_px,
                    "metrics": metrics,
                }
            try:
                await self._call(
                    self._trade.cancel_order,
                    instId=instrument, ordId=ord_id,
                )
            except Exception:
                pass

        log.error("chase_sell_deadline_exhausted", instrument=instrument)
        await _notify_chase_failure(
            side="sell", instrument=instrument,
            qty_btc=qty_btc, reason="deadline_exhausted",
            sCode="", sMsg="", attempt=attempt,
        )
        return None

    # ──────────────────── RFQ (Block Trading) ─────────────────────
    #
    # OKX Block Trading API:
    #   POST /api/v5/rfq/create-rfq      — submit a multi-leg RFQ
    #   GET  /api/v5/rfq/quotes          — poll counterparty quotes
    #   POST /api/v5/rfq/execute-quote   — execute the chosen quote
    #   POST /api/v5/rfq/cancel-rfq      — cancel an open RFQ
    #
    # Requires OKX Block Trading entitlement on the account (live trading)
    # or demo flag=1 (which has limited RFQ support). We always fall back
    # to leg-by-leg chase if RFQ returns no quotes inside the window.

    async def _rfq_send_two_leg(
        self,
        call_inst: str,
        put_inst: str,
        qty_btc: float,
        direction: str,  # "buy" or "sell"
    ) -> Optional[dict]:
        """
        Submit a 2-leg RFQ, wait for quotes, execute the best, return fills.

        direction='buy'  -> we are long both legs (pay premium)
        direction='sell' -> we are short both legs (receive premium)

        Returns None if RFQ is unavailable, no quotes received, or execution
        fails — caller will fall back to leg-by-leg chase.
        """
        if not config.USE_RFQ or self._block is None:
            return None

        sz = self._qty_to_contracts(qty_btc)
        if sz <= 0:
            return None

        legs = [
            {"instId": call_inst, "tdMode": config.OKX_TD_MODE, "sz": str(sz),
             "side": direction},
            {"instId": put_inst, "tdMode": config.OKX_TD_MODE, "sz": str(sz),
             "side": direction},
        ]

        # 1. Create RFQ (broadcast to all eligible market makers)
        try:
            create_resp = await self._call(
                self._block.create_rfq,
                counterparties=[],  # broadcast
                anonymous=True,
                legs=legs,
            )
        except Exception:
            log.warning("rfq_create_failed", exc_info=True)
            return None

        rows = self._data_or_empty(create_resp)
        if not rows:
            log.warning("rfq_create_empty", direction=direction)
            return None

        rfq_id = rows[0].get("rfqId", "")
        if not rfq_id:
            return None

        log.info("rfq_created", id=rfq_id, direction=direction,
                 call=call_inst, put=put_inst, qty=qty_btc, sz=sz)

        # 2. Poll quotes for up to RFQ_QUOTE_WAIT_SEC
        deadline = time.time() + config.RFQ_QUOTE_WAIT_SEC
        best_quote: Optional[dict] = None
        while time.time() < deadline:
            try:
                qresp = await self._call(self._block.get_quotes,
                                         rfqId=rfq_id)
                quotes = self._data_or_empty(qresp)
            except Exception:
                quotes = []

            if quotes:
                best_quote = self._pick_best_quote(quotes, direction)
                if best_quote is not None:
                    break
            await asyncio.sleep(1.0)

        if best_quote is None:
            log.warning("rfq_no_quotes", id=rfq_id,
                        wait_sec=config.RFQ_QUOTE_WAIT_SEC)
            try:
                await self._call(self._block.cancel_rfq, rfqId=rfq_id)
            except Exception:
                pass
            return None

        quote_id = best_quote.get("quoteId", "")
        log.info("rfq_quote_picked", id=rfq_id, quote=quote_id,
                 legs=best_quote.get("legs"))

        # 3. Execute the chosen quote
        try:
            exec_resp = await self._call(
                self._block.execute_quote,
                rfqId=rfq_id, quoteId=quote_id,
            )
        except Exception:
            log.warning("rfq_execute_failed", exc_info=True)
            return None

        if exec_resp.get("code") not in ("0", 0, None):
            log.warning("rfq_execute_error",
                        code=exec_resp.get("code"),
                        msg=exec_resp.get("msg"))
            return None

        # 4. Extract fill prices per leg
        call_price = 0.0
        put_price = 0.0
        for leg in best_quote.get("legs", []) or []:
            inst = leg.get("instId", "")
            px = self._f(leg, "px")
            if inst == call_inst:
                call_price = px
            elif inst == put_inst:
                put_price = px

        if call_price <= 0 or put_price <= 0:
            log.warning("rfq_missing_leg_price",
                        call_price=call_price, put_price=put_price)
            return None

        log.info("rfq_executed", id=rfq_id, quote=quote_id,
                 call_price=call_price, put_price=put_price,
                 direction=direction)

        return {
            "rfq_id": rfq_id,
            "quote_id": quote_id,
            "call_price": call_price,
            "put_price": put_price,
        }

    @staticmethod
    def _pick_best_quote(quotes: list[dict], direction: str) -> Optional[dict]:
        """Pick the cheapest buy quote or richest sell quote across legs."""
        scored = []
        for q in quotes:
            legs = q.get("legs") or []
            total = 0.0
            ok = True
            for leg in legs:
                v = leg.get("px")
                try:
                    total += float(v)
                except (TypeError, ValueError):
                    ok = False
                    break
            if ok and total > 0:
                scored.append((total, q))
        if not scored:
            return None
        if direction == "buy":
            scored.sort(key=lambda x: x[0])  # cheapest first
        else:
            scored.sort(key=lambda x: x[0], reverse=True)  # richest first
        return scored[0][1]

    async def send_rfq(
        self, call_inst: str, put_inst: str, qty_btc: float,
    ) -> Optional[dict]:
        """Atomic RFQ buy of (long call + long put). Returns fills or None."""
        return await self._rfq_send_two_leg(call_inst, put_inst, qty_btc,
                                            "buy")

    async def send_rfq_sell(
        self, call_inst: str, put_inst: str, qty_btc: float,
    ) -> Optional[dict]:
        """Atomic RFQ sell of (short call + short put). Returns fills or None."""
        return await self._rfq_send_two_leg(call_inst, put_inst, qty_btc,
                                            "sell")
