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

OKX BTC option naming (family-dependent — see ``core.family``):
    CM:  BTC-USD-{YYMMDD}-{STRIKE}-{C|P}      (inverse / coin-margined)
    UM:  BTC-USD_UM-{YYMMDD}-{STRIKE}-{C|P}   (linear / USD-margined)

UNIT CONVENTIONS — native quote unit depends on family:
  CM (inverse):
    • Premium px quoted in BTC per BTC of underlying notional
    • Tick size = 0.0001 BTC across the family
    • USD premium per BTC notional: px × spot
    • Fees charged in BTC (maker)
  UM (linear):
    • Premium px quoted in USD per BTC of underlying notional
    • Tick size = 5 USD
    • USD premium per BTC notional: px (already in USD)
    • Fees charged in USD (maker)

Internally the codebase normalises premiums to a "BTC-equivalent" ratio
(see ``core.family.to_btc_equivalent``) so the existing P&L math
(``entry_price × entry_spot``) yields correct USD numbers in either
family. Native prices are only used at the order-placement / fill-read
boundary inside this module.

The 2026-05-07 OPTION_TICK_SIZE=5.0 bug — adding a USD tick to a
BTC-quoted bid and producing 5.0055 BTC — is what motivated the family
abstraction. ``prime_option_tick_size`` enforces a family-specific
plausibility bound on the live tick to catch any future regression.

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
from core import family

log = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Transient HTTP retry policy for `_call`
# ─────────────────────────────────────────────────────────────────────
# OKX's HTTP/2 frontend periodically issues a GOAWAY / drops the socket
# mid-flight, which surfaces as ``httpcore.RemoteProtocolError("Server
# disconnected")``. Without retry, a single such blip kills whichever
# read happened to land on the dropped connection — and the chase loop
# then aborts the entire entry session. See incidents:
#   2026-05-23 utc_1430 (Sat) — get_option_mark_price → orphan put
#   2026-05-25 utc_0900 (Mon) — get_ticker → both legs aborted clean
# Both losses were transient socket-close events, not OKX rejecting the
# request. Retrying with a short backoff lets the SDK pick up a fresh
# pooled connection and the call almost always succeeds the second time.
try:
    import httpx as _httpx
    import httpcore as _httpcore
    _TRANSIENT_HTTP_EXCEPTIONS: tuple[type[BaseException], ...] = (
        _httpx.RemoteProtocolError,
        _httpx.ReadTimeout,
        _httpx.WriteTimeout,
        _httpx.ConnectError,
        _httpx.PoolTimeout,
        _httpcore.RemoteProtocolError,
        _httpcore.ReadTimeout,
        _httpcore.WriteTimeout,
        _httpcore.ConnectError,
        _httpcore.PoolTimeout,
        ConnectionResetError,
        ConnectionAbortedError,
    )
except ImportError:  # pragma: no cover — httpx/httpcore are pulled in by python-okx
    _TRANSIENT_HTTP_EXCEPTIONS = (ConnectionResetError, ConnectionAbortedError)

# OKX SDK function names that are SAFE to auto-retry on a transient
# HTTP failure. Two categories:
#   1. Read-only calls — idempotent by definition.
#   2. Cancel calls    — idempotent on OKX (re-cancelling a cancelled
#                        or filled order returns a benign code).
# DELIBERATELY EXCLUDED:
#   - place_order   (could create a duplicate order if the first
#                    request actually reached OKX before the socket
#                    died and we retry on the response read)
#   - create_rfq    (same — would broadcast a duplicate RFQ)
#   - execute_quote (same — would execute a quote twice)
# A failed write bubbles up exactly as before; the chase loop's
# try/except + cleanup block (commit 76c9427) handles it safely.
_RETRY_SAFE_FNS: frozenset = frozenset({
    # Reads
    "get_index_tickers", "get_ticker", "get_tickers",
    "get_mark_price", "get_instruments",
    "get_account_balance", "get_positions",
    "get_order_list", "get_order",
    "get_quotes",
    # Idempotent writes
    "cancel_order", "cancel_batch_orders", "cancel_rfq",
})

_RETRY_MAX_RETRIES = 3
_RETRY_BACKOFF_SEC = (0.2, 0.4, 0.8)  # cumulative ≤ 1.4 s before we abandon


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
        title_map = {
            "fatal_reject": "CHASE FATAL REJECT",
            "chase_loop_exception": "CHASE EXCEPTION (CLEANUP RAN)",
        }
        title = title_map.get(reason, "CHASE DEADLINE EXHAUSTED")
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
        elif reason == "chase_loop_exception":
            body_lines.append(
                "Chase loop raised an exception. Resting-order cleanup ran "
                "(or fallback cancel-all-for-instrument was attempted). "
                "Verify on the exchange that no orphan order survived."
            )
        else:
            body_lines.append(
                "Maker-only chase exhausted its time budget without filling. "
                "If this was an entry leg, the session is being aborted."
            )
        await notifier.send("\n".join(body_lines))
    except Exception:
        log.warning("chase_failure_notify_skipped", exc_info=True)


async def _notify_partial_fill(
    *,
    side: str,
    instrument: str,
    filled_contracts: int,
    target_contracts: int,
    vwap: float,
) -> None:
    """Telegram alert when a chase terminates with filled < target.

    The leg has *some* live exposure on the exchange that is smaller than
    the algo's intended size. The caller is responsible for deciding what
    to do (typically: emergency-flatten the partial). This notifier just
    raises operator awareness immediately.
    """
    try:
        from core import notifier
        filled_btc = filled_contracts * config.OKX_CONTRACT_SIZE_BTC
        target_btc = target_contracts * config.OKX_CONTRACT_SIZE_BTC
        body = [
            "<b>PARTIAL FILL DETECTED</b>",
            f"Side: {side.upper()}",
            f"Instrument: {instrument}",
            f"Filled: {filled_contracts} contracts ({filled_btc:.4f} BTC)",
            f"Target: {target_contracts} contracts ({target_btc:.4f} BTC)",
            f"Pct filled: {filled_contracts / target_contracts:.1%}",
            f"VWAP: {vwap:.4f} BTC",
            "",
            "The chase terminated with less than full size on the exchange. "
            "The straddle builder will treat this as a leg failure and "
            "flatten the partial position to avoid naked exposure.",
        ]
        await notifier.send("\n".join(body))
    except Exception:
        log.warning("partial_fill_notify_skipped", exc_info=True)


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
    spot_usd: float = 0.0,
    fee_native: float = 0.0,
) -> dict:
    """
    Build the fill-quality metrics dict that flows from chase_buy/sell
    back to the straddle builder/exit manager and ultimately into the
    daily report.

    All ``ref_*``, ``fill_price`` and ``fee_native`` arguments are in
    OKX-native units for the active option family:
        CM (inverse) → BTC per BTC of notional, fees in BTC
        UM (linear)  → USD per BTC of notional, fees in USD

    Slippage is positive when we paid more than mark (buys) or
    received less than mark (sells) — i.e. execution worse than fair
    value. Negative = better than fair value. Slippage is unit-free.

    USD conversion (saved-vs-taker, fee_usd):
      - CM: native is BTC, multiply by ``qty_btc * spot_usd`` to get USD.
      - UM: native is already USD per BTC of notional; multiply by
            ``qty_btc`` only.
      The caller passes ``spot_usd`` observed at fill-time; if it is 0
      we fall back to 0 USD instead of emitting a misleading number.

    The 'fee_btc' key is preserved (with the dimensional twist that on
    UM it actually contains the *USD* fee — the daily report only ever
    consumes ``fee_usd``, but the legacy column is kept for backward
    compat with old trade-log readers).
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
        saved_per_btc_native = (ref_ask - fill_price) if ref_ask > 0 else 0.0
        saved_pct = (saved_per_btc_native / ref_ask) if ref_ask > 0 else 0.0
    else:  # sell
        slip_mark = ((ref_mark - fill_price) / ref_mark
                     if ref_mark > 0 else 0.0)
        slip_mid = ((ref_mid - fill_price) / ref_mid
                    if ref_mid > 0 else 0.0)
        # As a maker sell, the taker alternative was hitting the bid.
        taker_price = ref_bid
        saved_per_btc_native = (fill_price - ref_bid) if ref_bid > 0 else 0.0
        saved_pct = (saved_per_btc_native / ref_bid) if ref_bid > 0 else 0.0

    # `saved_per_btc_native` is a price delta in OKX-native units per
    # 1 BTC of underlying notional. Convert to USD using the family
    # converter so CM (BTC × spot) and UM (USD × 1) both end up correct.
    saved_total_native = saved_per_btc_native * qty_btc
    saved_total_usd = family.native_premium_to_usd(
        saved_per_btc_native, qty_btc, spot_usd,
    ) if (saved_per_btc_native and (family.is_um() or spot_usd > 0)) else 0.0
    fee_native_abs = abs(fee_native)
    fee_usd = family.fee_to_usd(fee_native_abs, spot_usd)

    decimals = family.native_decimals()

    return {
        "family": family.label(),
        "native_unit": family.native_quote_unit_label(),
        "instrument": instrument,
        "side": side,
        "qty_btc": qty_btc,
        "spot_usd_at_fill": round(spot_usd, 2),
        "t_started_iso": _utc_iso(t_started),
        "t_filled_iso": _utc_iso(t_filled),
        "duration_sec": round(duration, 2),
        "attempts": attempts,
        "ref_bid": round(ref_bid, decimals + 2),
        "ref_ask": round(ref_ask, decimals + 2),
        "ref_mid": round(ref_mid, decimals + 2),
        "ref_mark": round(ref_mark, decimals + 2),
        "fill_price": round(fill_price, decimals + 2),
        "slippage_vs_mark_pct": round(slip_mark * 100, 4),
        "slippage_vs_mid_pct": round(slip_mid * 100, 4),
        "taker_price_at_start": round(taker_price, decimals + 2),
        # Legacy key name (per_btc_usd) kept for backward compat — the
        # value carries native units (BTC for CM, USD for UM), and is
        # NOT necessarily USD. Reports should consume ``saved_vs_taker_total_usd``.
        "saved_vs_taker_per_btc_usd": round(saved_per_btc_native, decimals + 2),
        "saved_vs_taker_pct": round(saved_pct * 100, 4),
        "saved_vs_taker_total_btc": round(saved_total_native, 6),
        "saved_vs_taker_total_usd": round(saved_total_usd, 2),
        # Legacy key name (fee_btc) — for CM holds native BTC fee, for UM
        # holds native USD fee. Daily report consumes fee_usd only.
        "fee_btc": round(fee_native_abs, 8 if family.is_cm() else 4),
        "fee_native": round(fee_native_abs, 8 if family.is_cm() else 4),
        "fee_usd": round(fee_usd, 4),
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
        # Set to True by prime_option_tick_size() when ctVal × ctMult from
        # the live OKX API does not match config.OKX_CONTRACT_SIZE_BTC.
        # main.py reads this flag during startup and locks entries on
        # mismatch — guards against the catastrophic case where the algo's
        # contract-size assumption is wrong (would cause 100× wrong
        # position sizing).
        self._contract_size_mismatch: bool = False

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
        """Run a sync SDK call in a thread; bump error_count on failure.

        For SDK calls in ``_RETRY_SAFE_FNS`` (read-only / idempotent), a
        transient HTTP failure (see ``_TRANSIENT_HTTP_EXCEPTIONS``) is
        retried up to ``_RETRY_MAX_RETRIES`` times with the backoff
        schedule in ``_RETRY_BACKOFF_SEC`` before being re-raised.

        Non-idempotent writes (``place_order``, ``create_rfq``,
        ``execute_quote``) are NEVER auto-retried — duplicate fills
        are a far worse failure mode than a missed entry, and the
        chase loop's existing try/except + cleanup block (commit
        76c9427) already prevents an orphan when a write raises.

        Non-transient exceptions (anything outside the whitelist of
        transient HTTP errors) are re-raised immediately regardless
        of which fn is being called.
        """
        fn_name = getattr(fn, "__name__", "")
        retryable = fn_name in _RETRY_SAFE_FNS
        max_attempts = _RETRY_MAX_RETRIES + 1 if retryable else 1

        last_exc: BaseException | None = None
        for attempt_idx in range(max_attempts):
            try:
                return await asyncio.to_thread(fn, *args, **kwargs)
            except _TRANSIENT_HTTP_EXCEPTIONS as exc:
                last_exc = exc
                # Either fn isn't whitelisted for retry, OR we've burned
                # our budget — surface the same error path as pre-fix.
                if not retryable or attempt_idx >= _RETRY_MAX_RETRIES:
                    self.error_count += 1
                    log.error(
                        "okx_call_failed",
                        fn=fn_name,
                        attempt=attempt_idx + 1,
                        retryable=retryable,
                        exc_type=type(exc).__name__,
                        exc_info=True,
                    )
                    raise
                backoff = _RETRY_BACKOFF_SEC[
                    min(attempt_idx, len(_RETRY_BACKOFF_SEC) - 1)
                ]
                log.warning(
                    "okx_call_transient_retry",
                    fn=fn_name,
                    attempt=attempt_idx + 1,
                    max_attempts=max_attempts,
                    backoff_sec=backoff,
                    exc_type=type(exc).__name__,
                )
                await asyncio.sleep(backoff)
            except Exception:
                # Non-transient exception (OKX business reject, code bug,
                # JSON parse failure, …) — bubble up exactly as before.
                self.error_count += 1
                log.error("okx_call_failed", fn=fn_name, exc_info=True)
                raise

        # Loop exited without returning; only reachable if every retry
        # raised a transient exception AND we somehow skipped the
        # `raise` inside the final iteration. Defensive fallback.
        if last_exc is not None:  # pragma: no cover
            raise last_exc
        raise RuntimeError(  # pragma: no cover
            f"_call exited unexpectedly for {fn_name!r}"
        )

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
        """Return BTC index price (USD).

        The BTC index ticker is always ``BTC-USD`` on OKX regardless of
        the option family in use — the linear ``BTC-USD_UM`` uly does
        not have a separate spot index. So this query is hard-coded
        rather than going through ``family.underlying()``.
        """
        resp = await self._call(
            self._market.get_index_tickers,
            instId=f"{config.BASE_COIN}-USD",
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
        inst_family: str = "",
    ) -> dict[str, Ticker]:
        """Bulk fetch option tickers, optionally filtered by ``instFamily``.

        OKX shares ``uly=BTC-USD`` between CM and UM, so the discriminator
        is ``instFamily`` (``BTC-USD`` for CM, ``BTC-USD_UM`` for UM). When
        a caller passes both, ``instFamily`` is the authoritative filter.
        Pre-2026-05-18 callers passed ``underlying="BTC-USD_UM"`` thinking
        it was the family selector — that path returns 0 rows on OKX.
        """
        resp = await self._call(
            self._market.get_tickers,
            instType="OPTION", uly=underlying,
            instFamily=inst_family,
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
            "ctMult": self._f(r, "ctMult"),
            "lotSz": self._f(r, "lotSz"),
            "minSz": self._f(r, "minSz"),
        }
        self._inst_meta[instrument] = meta
        return meta

    async def prime_option_tick_size(
        self, sample_underlying: str = "",
    ) -> float:
        """
        Read the live tick size for the active option family from OKX and
        update self._default_option_tick. Returns the tick size in use.

        We query /api/v5/public/instruments with the family-specific
        ``instFamily`` (BTC-USD for CM, BTC-USD_UM for UM) and pick the
        most-common tickSz. OKX BTC options share a common tick across
        strikes within each family (CM=0.0001 BTC, UM=5 USD), so any
        sample is fine.

        IMPORTANT — both families share ``uly=BTC-USD``. The CM/UM
        discriminator is ``instFamily``, not ``uly``. Querying
        ``uly=BTC-USD_UM`` returns ``code=51014 "Index doesn't exist."``
        (regression hit 2026-05-18 during the UM cutover diagnostic).
        """
        underlying = sample_underlying or family.underlying()
        inst_family = family.instfamily()
        try:
            resp = await self._call(
                self._public.get_instruments,
                instType="OPTION",
                uly=underlying,
                instFamily=inst_family,
            )
        except Exception:
            log.warning("prime_tick_failed",
                        underlying=underlying,
                        inst_family=inst_family,
                        exc_info=True)
            return self._default_option_tick

        rows = self._data_or_empty(resp)
        if not rows:
            log.warning("prime_tick_empty",
                        underlying=underlying,
                        inst_family=inst_family,
                        fallback=self._default_option_tick)
            return self._default_option_tick

        # Cache every row while we have the data — saves later RTTs.
        # Only count rows that are *real options* (instId ends in "-C" or
        # "-P" and matches the BASE-QUOTE-EXPIRY-STRIKE-{C|P} shape) toward
        # the default-tick computation. OKX sometimes returns non-option
        # entries when instType+uly are loose; on 2026-05-07 this caused
        # _default_option_tick to be set to 5.0 (futures tick) which would
        # have been a latent footgun if the per-instrument cache missed.
        #
        # ctVal × ctMult is the EMPIRICAL contract size in BTC (verified
        # 2026-05-15 via /api/v5/public/instruments: both CM and UM
        # families return ctVal=1, ctMult=0.01 ⇒ 0.01 BTC per contract).
        # We compute this for every option row so a startup mismatch
        # against config.OKX_CONTRACT_SIZE_BTC is caught immediately
        # instead of relying on an empirical UI verification.
        ticks: list[float] = []
        ct_vals: list[float] = []
        ct_mults: list[float] = []
        effective_sizes: list[float] = []
        option_rows = 0
        for r in rows:
            inst = r.get("instId")
            if not inst:
                continue
            meta = {
                "tickSz": self._f(r, "tickSz"),
                "ctVal": self._f(r, "ctVal"),
                "ctMult": self._f(r, "ctMult"),
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
            if meta["ctMult"] > 0:
                ct_mults.append(meta["ctMult"])
            if meta["ctVal"] > 0 and meta["ctMult"] > 0:
                effective_sizes.append(meta["ctVal"] * meta["ctMult"])

        if ticks:
            # Use the most common tick — OKX BTC options are uniform
            # 0.0001 across the family, so this should be unambiguous.
            from collections import Counter
            most_common_tick = Counter(ticks).most_common(1)[0][0]
            self._default_option_tick = most_common_tick

        live_ct_val = ct_vals[0] if ct_vals else 0.0
        live_ct_mult = ct_mults[0] if ct_mults else 0.0
        # ctVal × ctMult is the empirical BTC contract size. OKX docs
        # are ambiguous about which field carries the BTC quantity, but
        # the live values resolve it: ctVal=1, ctMult=0.01 ⇒ 0.01 BTC
        # per contract. This holds across all 1,200 BTC option
        # instruments (730 CM + 470 UM, verified 2026-05-15).
        live_effective_size = (
            sum(effective_sizes) / len(effective_sizes)
            if effective_sizes else 0.0
        )

        # Sanity: an option tick larger than the family's plausible
        # ceiling almost certainly means we got the wrong instrument
        # type back (e.g. a futures row leaking in via a loose uly+
        # instType combination). Refuse to override the sensible
        # config default in that case. CM ceiling = 0.01 BTC,
        # UM ceiling = 100 USD (real ticks: CM=0.0001 BTC, UM=5 USD).
        plausibility_ceiling = family.tick_implausible_threshold()
        if self._default_option_tick > plausibility_ceiling:
            log.warning("prime_tick_implausible",
                        family=family.label(),
                        candidate=self._default_option_tick,
                        ceiling=plausibility_ceiling,
                        action="keeping config fallback",
                        config_value=config.OPTION_TICK_SIZE)
            self._default_option_tick = config.OPTION_TICK_SIZE

        # Capture a sample minSz/lotSz so the operator can spot-check
        # the contract size assumption on the very first boot of a new
        # family. Especially important on UM where empirical contract
        # sizing has not been independently verified at desk.
        sample_min_sz = 0.0
        sample_lot_sz = 0.0
        for r in rows:
            inst = r.get("instId", "")
            parts = inst.split("-")
            if len(parts) == 5 and parts[-1] in ("C", "P"):
                sample_min_sz = self._f(r, "minSz")
                sample_lot_sz = self._f(r, "lotSz")
                break

        log.info("instrument_meta_primed",
                 family=family.label(),
                 underlying=underlying,
                 inst_family=inst_family,
                 total_rows=len(rows),
                 option_rows=option_rows,
                 tick_size=self._default_option_tick,
                 contract_size_api_ctval=live_ct_val,
                 contract_size_api_ctmult=live_ct_mult,
                 contract_size_api_effective_btc=live_effective_size,
                 contract_size_assumed_btc=config.OKX_CONTRACT_SIZE_BTC,
                 sample_min_sz=sample_min_sz,
                 sample_lot_sz=sample_lot_sz)

        # Auto-verify the contract-size assumption against the live API.
        # OKX BTC options use ctVal × ctMult = 1 × 0.01 = 0.01 BTC per
        # contract on both CM (inverse) and UM (linear) families. The
        # algo's hardcoded OKX_CONTRACT_SIZE_BTC=0.01 must match this
        # exactly — any mismatch is a deployment bug that would cause
        # catastrophic position sizing on the first trade.
        if live_effective_size > 0:
            tolerance = 1e-6
            if abs(live_effective_size - config.OKX_CONTRACT_SIZE_BTC) \
                    > tolerance:
                log.error(
                    "contract_size_api_mismatch",
                    family=family.label(),
                    live_ctval=live_ct_val,
                    live_ctmult=live_ct_mult,
                    live_effective_btc=live_effective_size,
                    config_btc=config.OKX_CONTRACT_SIZE_BTC,
                    hint=("ctVal × ctMult from /api/v5/public/instruments "
                          "does not match config.OKX_CONTRACT_SIZE_BTC. "
                          "Update OKX_CONTRACT_SIZE_BTC (CM) or "
                          "OKX_CONTRACT_SIZE_BTC_UM (UM) in .env to "
                          "match the live API value before trading."),
                )
                # Surface the mismatch so the algo's _entry_lock
                # mechanism can pick it up (callers check this flag).
                self._contract_size_mismatch = True
            else:
                log.info("contract_size_verified",
                         family=family.label(),
                         live_btc=live_effective_size,
                         config_btc=config.OKX_CONTRACT_SIZE_BTC)
                self._contract_size_mismatch = False
        else:
            log.warning("contract_size_unavailable",
                        family=family.label(),
                        note="ctVal/ctMult not returned by API — "
                             "falling back to config value")

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
        """Return total trading-account equity in USD-equivalent.

        Designed to work with all OKX account-mode/funding combinations:
          • USDT-only account with auto-borrow (BTC borrowed for BTC-USD
            inverse options — totalEq reflects USDT minus borrow value).
          • BTC-funded account (totalEq reflects BTC × spot).
          • Mixed (USDT + BTC) — totalEq sums them in USD.

        Strategy:
          1. Read the FULL account (no ccy filter) and use `totalEq` —
             OKX reports this in USD across all currencies, so it
             correctly accounts for any auto-borrow loan as a negative.
          2. Fall back to the largest per-currency `eqUsd` if totalEq is
             missing.
          3. Final fallback: explicit ccy filter (matches the pre-2026-05-08
             behavior that read $7,776 USDT directly).
        """
        try:
            resp = await self._call(self._account.get_account_balance)
            rows = self._data_or_empty(resp)
        except Exception:
            log.warning("get_account_equity_failed", exc_info=True)
            return 0.0

        if not rows:
            return 0.0

        total_eq = self._f(rows[0], "totalEq")
        if total_eq > 0:
            return total_eq

        # Fallback: pick the largest per-currency eq we can find.
        details = rows[0].get("details") or []
        best = 0.0
        for d in details:
            eq = self._f(d, "eqUsd") or self._f(d, "eq")
            if eq > best:
                best = eq
        if best > 0:
            return best

        # Final fallback: explicit ccy filter for the requested currency.
        try:
            resp = await self._call(
                self._account.get_account_balance, ccy=ccy,
            )
            rows = self._data_or_empty(resp)
        except Exception:
            return 0.0
        if not rows:
            return 0.0
        details = rows[0].get("details") or []
        for d in details:
            if d.get("ccy") == ccy:
                return self._f(d, "eqUsd") or self._f(d, "eq")
        return self._f(rows[0], "totalEq")

    async def list_open_positions(self) -> list[dict]:
        """List all option positions for the active family.

        python-okx >=0.4.1 dropped the `uly=` parameter from
        `Account.get_positions`. We now fetch all OPTION positions and
        filter by the family-specific instId prefix in code (CM:
        ``BTC-USD-`` vs UM: ``BTC-USD_UM-``).
        """
        resp = await self._call(
            self._account.get_positions,
            instType="OPTION",
        )
        rows = self._data_or_empty(resp)
        family_prefix = family.instid_prefix()
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

    async def cancel_orders_for_instrument(self, instrument: str) -> int:
        """Cancel any open orders for a specific instrument.

        Used as a safety net before _emergency_sell so we don't sell on top
        of a still-live buy from the same chase iteration (the rare case
        where the chase aborted with `still_live_after_retries`).
        """
        try:
            orders = await self.list_open_orders()
        except Exception:
            log.warning("cancel_for_instrument_list_failed",
                        instrument=instrument, exc_info=True)
            return 0
        cancelled = 0
        for o in orders:
            if o.get("instId") != instrument:
                continue
            oid = o.get("ordId")
            if not oid:
                continue
            try:
                resp = await self._call(
                    self._trade.cancel_order, instId=instrument, ordId=oid,
                )
                if str(resp.get("code")) in ("0",):
                    cancelled += 1
                else:
                    log.warning("cancel_for_instrument_failed",
                                instId=instrument, ordId=oid,
                                code=resp.get("code"), msg=resp.get("msg"))
            except Exception:
                log.warning("cancel_for_instrument_exception",
                            instId=instrument, ordId=oid, exc_info=True)
        if cancelled > 0:
            log.info("cancel_for_instrument_done",
                     instrument=instrument, cancelled=cancelled)
        return cancelled

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

    def _accumulate_fee(
        self, fees_by_ord_id: dict, ord_id: str, status: dict,
    ) -> None:
        """Track the latest cumulative fee (in BTC) for one order_id.

        OKX returns ``fee`` as the cumulative fee charged by that single
        order so far (negative when the trader paid, positive on rebate).
        Each chase iteration may cancel-and-replace, producing multiple
        order_ids per trade leg; ``fees_by_ord_id`` maps order_id → the
        latest absolute BTC fee seen for that order. Total chase fee =
        sum of values across all order_ids.

        Why we read it on every status read instead of only at terminal:
        if cancellation propagates faster than the final ``get_order``
        round trip, we may never see ``filled``/``cancelled`` for that
        ord_id again. Recording the fee on each status read guarantees
        we always have the most-recent value.
        """
        if not ord_id:
            return
        raw = self._f(status, "fee", default=0.0)
        # OKX `fee` is negative when fee was charged, positive on rebate.
        # We track absolute BTC outlay; subtract from gross P&L downstream.
        fees_by_ord_id[ord_id] = abs(raw)

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

        Returns the latest raw OKX order row. The caller is responsible for
        interpreting the {state, sz, accFillSz} triple — this function does
        NOT promote partial fills to ``filled``. Why: the chase loop needs
        to distinguish (a) a true full fill, (b) a partial fill that should
        cause the next iteration to size for only the remainder, and
        (c) a canceled-with-zero-fill that should retry with full size.
        See chase_buy/chase_sell for the post-cancel reconciliation.
        """
        deadline = time.time() + timeout
        last: dict = {}
        while time.time() < deadline:
            try:
                last = await self.get_order_status(instrument, order_id)
                state = last.get("state", "")
                if state in ("filled", "canceled", "cancelled"):
                    return last
            except Exception:
                log.warning("order_status_failed", exc_info=True)
            await asyncio.sleep(1.0)
        return last

    async def _reconcile_after_wait(
        self,
        instrument: str,
        ord_id: str,
        wait_status: dict,
        fallback_price: float,
        *,
        side: str,
        attempt: int,
        fees_by_ord_id: Optional[dict] = None,
    ) -> tuple[int, float, str]:
        """Cancel the order and reconcile the final fill state.

        Returns (filled_contracts_this_iter, avg_px, order_state). The
        order_state is one of:
          • "filled" / "canceled" / "cancelled"   — terminal (safe to proceed)
          • "still_live_after_retries"            — caller MUST abort the
              chase to avoid stacking duplicate orders on top of a live one.

        Strategy:
          1. Try to cancel.
          2. Read final order state (max accFillSz across wait + final reads).
          3. If state still "live" (cancel didn't propagate), retry cancel
             + read up to 3 times with 250ms backoff. Only then give up.
        """
        if not ord_id:
            return 0, fallback_price, ""

        async def _try_cancel() -> None:
            try:
                await self._call(
                    self._trade.cancel_order,
                    instId=instrument, ordId=ord_id,
                )
            except Exception:
                log.warning(f"chase_{side}_cancel_failed",
                            instrument=instrument, ord_id=ord_id,
                            attempt=attempt, exc_info=True)

        await _try_cancel()

        wait_acc = self._f(wait_status, "accFillSz") if wait_status else 0.0
        final_acc = 0.0
        final_best: dict = {}
        order_state = (wait_status or {}).get("state", "")
        # Capture fee from any status we already had at entry too.
        if fees_by_ord_id is not None and wait_status:
            self._accumulate_fee(fees_by_ord_id, ord_id, wait_status)

        for retry in range(3):
            final: dict = {}
            try:
                final = await self.get_order_status(instrument, ord_id)
            except Exception:
                log.warning(f"chase_{side}_final_status_read_failed",
                            instrument=instrument, ord_id=ord_id,
                            attempt=attempt, retry=retry, exc_info=True)
                final = {}
            f_acc = self._f(final, "accFillSz") if final else 0.0
            if f_acc > final_acc:
                final_acc = f_acc
                final_best = final
            if fees_by_ord_id is not None and final:
                self._accumulate_fee(fees_by_ord_id, ord_id, final)
            f_state = (final or {}).get("state", "")
            if f_state:
                order_state = f_state
            if f_state in ("filled", "canceled", "cancelled") or not f_state:
                # Terminal or empty (order vanished — also safe to proceed).
                break
            # Order still live → retry cancel and re-read briefly.
            await asyncio.sleep(0.25)
            await _try_cancel()

        if order_state in ("live", "partially_filled"):
            log.error(
                f"chase_{side}_order_still_live_after_3_retries",
                instrument=instrument, ord_id=ord_id, attempt=attempt,
                state=order_state, wait_acc=wait_acc, final_acc=final_acc,
                hint="aborting chase to avoid duplicate-order stacking",
            )
            order_state = "still_live_after_retries"

        # Pick the source whose accFillSz matches the larger reading so the
        # avgPx we read aligns with the contracts we credit. If they're tied
        # we prefer `final_best` (more recent read).
        if final_acc >= wait_acc:
            best, best_acc = final_best, final_acc
        else:
            best, best_acc = wait_status or {}, wait_acc

        filled_this = int(best_acc)
        avg_px = self._f(best, "avgPx", default=fallback_price)
        if avg_px <= 0:
            avg_px = fallback_price
        return filled_this, avg_px, order_state

    # ──────────────────── Maker-only chase (BUY) ──────────────────

    async def chase_buy(
        self, instrument: str, qty_btc: float, initial_bid: float,
    ) -> Optional[dict]:
        """
        Maker-only buy chase with partial-fill tracking and queue-priority
        preservation.

        Walks the bid toward the ask by OPTION_CHASE_GAP_NARROW_PCT each
        retry, never above mark × OPTION_CHASE_MAX_SLIPPAGE_FACTOR. Bails
        on deadline or fatal reject.

        Queue priority (added 2026-05-13):
          • If the recomputed price equals the resting order's price, the
            algo skips the cancel-replace and lets the existing order keep
            its FIFO position at that level. Reduces wasted RTTs and
            preserves time priority in thin 0DTE markets.
          • A reprice (price changed) cancels the resting order, credits
            any partial fills, and posts a fresh order at the new price.

        Partial-fill semantics:
          • Each iteration sizes the order for the REMAINING quantity, not
            the original target.
          • Per-iteration we read the order's accFillSz delta and credit
            only the new fills (preserves correctness across keep-alive
            and cancel-replace iterations).
          • Running totals: filled_contracts, weighted_value (Σ contracts×px)
            give a true VWAP across all child orders.

        Tight-spread handling: if (bid + tick) ≥ ask (1-tick wide market,
        common in liquid 0DTE options), the floor collapses to `bid` so we
        place a non-crossing maker bid AT the bid level.

        Returns:
          • dict with {average_price, order_id, filled_qty_btc,
              fully_filled, metrics} on any fill (full OR partial)
          • None only if filled_contracts == 0 (no fill at all)

        The caller checks `fully_filled` to decide whether the leg is
        usable as-is or needs to be flattened (partial-fill failure).
        """
        deadline = time.time() + config.OPTION_ENTRY_CHASE_DEADLINE_MIN * 60
        ct_val = config.OKX_CONTRACT_SIZE_BTC
        target_contracts = int(round(qty_btc / ct_val))
        if target_contracts <= 0:
            log.error("chase_buy_target_zero_contracts",
                      instrument=instrument, qty_btc=qty_btc, ct_val=ct_val)
            return None

        filled_contracts = 0
        weighted_value = 0.0  # Σ (contracts × fill_px) for VWAP
        last_ord_id = ""
        attempt = 0
        last_price = max(0.0, initial_bid)
        t_started = time.time()
        ref_bid = ref_ask = ref_mark = 0.0
        captured_ref = False
        # Base tick from OKX. CM tier-aware tick is computed PER-PRICE
        # below via family.round_price_to_tick — see family.py
        # ``effective_tick_for_price`` for OKX's silent tier boundaries
        # (0.0001 below 0.005 BTC, 0.0005 above). The base_tick is used
        # as a lower-bound floor in case OKX widens ticks during a
        # market disruption.
        base_tick = self.get_tick_size(instrument)
        if base_tick <= 0:
            base_tick = config.OPTION_TICK_SIZE

        # Resting-order state across iterations (queue-priority preserve)
        rested_ord_id: str = ""
        rested_price: float = 0.0
        rested_credited: int = 0  # contracts already credited from this resting order
        # Per-order_id BTC fee accumulator. OKX `fee` is cumulative *per
        # order_id*, not per chase, so we track the latest absolute value
        # for each order_id we touch and sum at the end.
        fees_by_ord_id: dict[str, float] = {}

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

        async def _credit_resting_fills(
            fallback_price: float,
        ) -> tuple[str, int]:
            """Read the resting order's status, credit any incremental
            fills since last credit. Returns (state, accFillSz).

            Mutates the enclosing filled_contracts / weighted_value /
            rested_credited via nonlocal."""
            nonlocal filled_contracts, weighted_value, rested_credited
            if not rested_ord_id:
                return "", 0
            try:
                status = await self.get_order_status(instrument, rested_ord_id)
            except Exception:
                return "", rested_credited
            state = (status or {}).get("state", "")
            acc = int(self._f(status, "accFillSz", default=0.0))
            avg_px = self._f(status, "avgPx", default=fallback_price)
            if avg_px <= 0:
                avg_px = fallback_price
            delta = max(0, acc - rested_credited)
            if delta > 0:
                filled_contracts += delta
                weighted_value += delta * avg_px
                rested_credited = acc
                log.info("chase_buy_resting_fill_credit",
                         instrument=instrument, attempt=attempt,
                         ord_id=rested_ord_id, delta=delta,
                         total_filled=filled_contracts,
                         vwap=weighted_value / filled_contracts,
                         state=state)
            # Capture fee field (cumulative for this order_id) on every read.
            self._accumulate_fee(fees_by_ord_id, rested_ord_id, status)
            return state, acc

        # Wrap chase loop in try/except so a transient exception
        # (e.g. httpx.RemoteProtocolError) cannot bypass the post-
        # loop cleanup below and leak a resting order as an orphan
        # position. See 2026-05-23 utc_1430 incident.
        chase_loop_exception: BaseException | None = None
        try:
            while time.time() < deadline and filled_contracts < target_contracts:
                attempt += 1

                # ── 0. Credit any fills on the existing resting order ──
                if rested_ord_id:
                    rested_state, _ = await _credit_resting_fills(rested_price)
                    if rested_state in ("filled", "canceled", "cancelled"):
                        log.info("chase_buy_resting_terminal",
                                 instrument=instrument, attempt=attempt,
                                 ord_id=rested_ord_id, state=rested_state)
                        rested_ord_id = ""
                        rested_price = 0.0
                        rested_credited = 0
                    if filled_contracts >= target_contracts:
                        break

                remaining_contracts = target_contracts - filled_contracts
                remaining_qty_btc = remaining_contracts * ct_val

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

                if not captured_ref:
                    ref_bid, ref_ask, ref_mark = bid, ask, mark
                    captured_ref = True

                # Tier-aware tick at the CURRENT mid/ask price tier. CM
                # premiums above 0.005 BTC use a 0.0005 tick; the API still
                # reports 0.0001 so we must compute it ourselves. Use the
                # ASK as the reference price for the tick lookup since the
                # buy chase is anchored on the sell side of the book.
                tick_ref_price = ask if ask > 0 else (mark or last_price)
                tick = family.effective_tick_for_price(
                    tick_ref_price, instrument_default_tick=base_tick,
                )

                effective_bid = bid if bid > 0 else max(mark - tick, tick)
                target_top = max(effective_bid, ask - tick)
                new_price = last_price + (target_top - last_price) \
                    * config.OPTION_CHASE_GAP_NARROW_PCT

                improvement_floor = effective_bid + tick
                floor_price = effective_bid if improvement_floor >= ask else improvement_floor
                new_price = max(new_price, floor_price)

                ceiling_price = ask - tick
                if ceiling_price < effective_bid:
                    ceiling_price = effective_bid
                new_price = min(new_price, ceiling_price)

                max_price = mark * config.OPTION_CHASE_MAX_SLIPPAGE_FACTOR
                if new_price > max_price:
                    log.warning("chase_buy_cap_hit",
                                instrument=instrument, new_price=new_price,
                                mark=mark, max_price=max_price, attempt=attempt)
                    await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                    continue

                # Round DOWN to the tier-effective tick — buys must never
                # round up across a tier boundary (would risk crossing).
                new_price, eff_tick = family.round_price_to_tick(
                    new_price, instrument_default_tick=base_tick, direction="down",
                )
                if eff_tick != base_tick:
                    log.debug("chase_buy_tier_tick_engaged",
                              instrument=instrument, attempt=attempt,
                              tick_ref_price=tick_ref_price,
                              base_tick=base_tick,
                              effective_tick=eff_tick,
                              new_price=new_price)
                last_price = new_price

                # ── 1. Keep-alive: same price as resting order? ──
                same_price = (rested_ord_id and
                              abs(rested_price - new_price) < eff_tick * 0.5)

                if same_price:
                    log.info("chase_buy_keep_alive",
                             instrument=instrument, attempt=attempt,
                             price=new_price, bid=bid, ask=ask, mark=mark,
                             tick=eff_tick, base_tick=base_tick,
                             ord_id=rested_ord_id,
                             remaining_contracts=remaining_contracts,
                             filled_so_far=filled_contracts,
                             target_contracts=target_contracts)
                    # Just wait for the existing order to fill; preserves
                    # FIFO queue priority at this price level.
                    await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                    continue

                # ── 2. Reprice: cancel resting order, credit any final fills ──
                if rested_ord_id:
                    log.info("chase_buy_reprice",
                             instrument=instrument, attempt=attempt,
                             from_price=rested_price, to_price=new_price,
                             ord_id=rested_ord_id)
                    stale_status = {
                        "state": "live",
                        "accFillSz": str(rested_credited),
                    }
                    filled_this, avg_px_this, order_state = \
                        await self._reconcile_after_wait(
                            instrument, rested_ord_id, stale_status,
                            rested_price, side="buy", attempt=attempt,
                            fees_by_ord_id=fees_by_ord_id,
                        )
                    # Credit any *new* fills that landed during the cancel race
                    delta = max(0, filled_this - rested_credited)
                    if delta > 0:
                        filled_contracts += delta
                        weighted_value += delta * avg_px_this
                        log.info("chase_buy_reprice_partial_credit",
                                 instrument=instrument, attempt=attempt,
                                 delta=delta, total_filled=filled_contracts,
                                 vwap=weighted_value / filled_contracts,
                                 state=order_state)
                    rested_ord_id = ""
                    rested_price = 0.0
                    rested_credited = 0
                    if order_state == "still_live_after_retries":
                        log.error("chase_buy_aborting_to_avoid_duplicate",
                                  instrument=instrument,
                                  attempt=attempt,
                                  filled_so_far=filled_contracts)
                        break
                    if filled_contracts >= target_contracts:
                        break
                    remaining_contracts = target_contracts - filled_contracts
                    remaining_qty_btc = remaining_contracts * ct_val

                log.info("chase_buy_attempt",
                         instrument=instrument, attempt=attempt,
                         price=new_price, bid=bid, ask=ask, mark=mark,
                         tick=eff_tick, base_tick=base_tick,
                         remaining_contracts=remaining_contracts,
                         filled_so_far=filled_contracts,
                         target_contracts=target_contracts)

                order = await self._place_limit_order(
                    instrument, "buy", remaining_qty_btc, new_price,
                    post_only=True,
                )
                ord_id = order.get("ordId")
                sCode = str(order.get("sCode") or "")
                sMsg = str(order.get("sMsg") or "")
                if ord_id:
                    last_ord_id = ord_id

                # Post-only reject (would cross) → narrow next loop
                if sCode == "51120" or "would immediately match" in sMsg.lower() \
                        or "post_only" in sMsg.lower():
                    log.info("chase_buy_post_only_rejected",
                             instrument=instrument, attempt=attempt)
                    await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                    continue

                if sCode in FATAL_CODES:
                    log.error("chase_buy_fatal_reject",
                              instrument=instrument, sCode=sCode, sMsg=sMsg,
                              attempt=attempt, filled_so_far=filled_contracts,
                              hint="check OKX_TD_MODE / account margin / balance")
                    await _notify_chase_failure(
                        side="buy", instrument=instrument,
                        qty_btc=qty_btc, reason="fatal_reject",
                        sCode=sCode, sMsg=sMsg, attempt=attempt,
                    )
                    # If anything filled before the fatal reject, surface it as
                    # a partial result so the caller can flatten it cleanly.
                    # If nothing filled, return None.
                    break

                if not ord_id or sCode not in ("0", ""):
                    log.warning("chase_buy_order_rejected",
                                instrument=instrument, sCode=sCode, sMsg=sMsg,
                                attempt=attempt)
                    await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                    continue

                # Order successfully placed → mark as resting and wait
                rested_ord_id = ord_id
                rested_price = new_price
                rested_credited = 0
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)

        except Exception as exc:
            chase_loop_exception = exc
            log.error("chase_buy_loop_exception",
                      instrument=instrument, attempt=attempt,
                      filled_so_far=filled_contracts,
                      rested_ord_id=rested_ord_id,
                      rested_price=rested_price,
                      rested_credited=rested_credited,
                      exc_info=True)

        # ── Loop exit / exception cleanup: cancel any resting order ──
        # Wrapped in try/except so a cleanup failure does NOT mask the
        # loop exception captured above. Last-ditch fallback uses
        # cancel_orders_for_instrument so an orphan can't accrue
        # while OKX is intermittently flaky.
        if rested_ord_id:
            try:
                stale_status = {
                    "state": "live",
                    "accFillSz": str(rested_credited),
                }
                filled_this, avg_px_this, _state = \
                    await self._reconcile_after_wait(
                        instrument, rested_ord_id, stale_status,
                        rested_price, side="buy", attempt=attempt,
                        fees_by_ord_id=fees_by_ord_id,
                    )
                delta = max(0, filled_this - rested_credited)
                if delta > 0:
                    filled_contracts += delta
                    weighted_value += delta * avg_px_this
                    log.info("chase_buy_exit_partial_credit",
                             instrument=instrument, attempt=attempt,
                             delta=delta, total_filled=filled_contracts)
                rested_ord_id = ""
                rested_price = 0.0
                rested_credited = 0
            except Exception:
                log.error("chase_buy_cleanup_reconcile_failed",
                          instrument=instrument,
                          ord_id=rested_ord_id,
                          rested_price=rested_price,
                          exc_info=True)
                try:
                    cancelled = await self.cancel_orders_for_instrument(
                        instrument,
                    )
                    log.warning("chase_buy_emergency_cancel_done",
                                instrument=instrument,
                                cancelled=cancelled,
                                note="orphan_risk_check_post_close_reconcile")
                except Exception:
                    log.error("chase_buy_emergency_cancel_failed",
                              instrument=instrument,
                              exc_info=True)
                rested_ord_id = ""
                rested_price = 0.0
                rested_credited = 0

        # If the chase loop raised, surface it AFTER cleanup so any
        # resting order has already been cancelled. The caller
        # (build_straddle) sees the same exception type as before;
        # what changes is that no orphan is left behind.
        if chase_loop_exception is not None:
            await _notify_chase_failure(
                side="buy", instrument=instrument,
                qty_btc=qty_btc, reason="chase_loop_exception",
                sCode="", sMsg=type(chase_loop_exception).__name__,
                attempt=attempt,
            )
            raise chase_loop_exception

        # ── Build result ──
        if filled_contracts == 0:
            if attempt > 0 and time.time() >= deadline:
                log.error("chase_buy_deadline_exhausted",
                          instrument=instrument, attempts=attempt)
                await _notify_chase_failure(
                    side="buy", instrument=instrument,
                    qty_btc=qty_btc, reason="deadline_exhausted",
                    sCode="", sMsg="", attempt=attempt,
                )
            return None

        vwap = weighted_value / filled_contracts
        filled_qty_btc = filled_contracts * ct_val
        fully_filled = filled_contracts >= target_contracts
        t_filled = time.time()
        # Spot at fill is needed to convert native saving deltas to USD.
        # On CM (BTC-quoted) it converts BTC × USD; on UM (USD-quoted)
        # the conversion is a no-op but we still record it for context.
        # Best-effort: a single index-tickers call; fallback to 0 if it
        # fails so we never emit a misleading $ figure.
        try:
            spot_at_fill = await self.get_spot_price()
        except Exception:
            spot_at_fill = 0.0
        # Sum all fees collected across every order_id used during this
        # chase. OKX returns fee per-order_id (cumulative for that order),
        # so summing the latest absolute value across all ord_ids gives
        # the chase total. Native unit is family-specific (BTC for CM,
        # USD for UM); _build_fill_metrics converts to USD for reports.
        total_fee_native = sum(fees_by_ord_id.values())
        metrics = _build_fill_metrics(
            side="buy",
            instrument=instrument,
            qty_btc=filled_qty_btc,
            fill_price=vwap,
            t_started=t_started,
            t_filled=t_filled,
            attempts=attempt,
            ref_bid=ref_bid, ref_ask=ref_ask, ref_mark=ref_mark,
            spot_usd=spot_at_fill,
            fee_native=total_fee_native,
        )
        if fully_filled:
            log.info("chase_buy_filled",
                     instrument=instrument, avg=vwap, attempts=attempt,
                     duration_sec=metrics["duration_sec"],
                     slippage_vs_mark_pct=metrics["slippage_vs_mark_pct"],
                     saved_vs_taker_total_usd=metrics["saved_vs_taker_total_usd"],
                     fee_usd=metrics["fee_usd"])
        else:
            log.warning("chase_buy_partial_terminated",
                        instrument=instrument,
                        filled_contracts=filled_contracts,
                        target_contracts=target_contracts,
                        filled_qty_btc=filled_qty_btc,
                        target_qty_btc=qty_btc,
                        vwap=vwap, attempts=attempt)
            await _notify_partial_fill(
                side="buy", instrument=instrument,
                filled_contracts=filled_contracts,
                target_contracts=target_contracts,
                vwap=vwap,
            )
        return {
            "average_price": vwap,
            "order_id": last_ord_id,
            "avgPrice": vwap,
            "filled_qty_btc": filled_qty_btc,
            "fully_filled": fully_filled,
            "metrics": metrics,
        }

    # ──────────────────── Maker-only chase (SELL) ─────────────────

    async def chase_sell(
        self, instrument: str, qty_btc: float, initial_ask: float,
    ) -> Optional[dict]:
        """
        Maker-only sell chase with partial-fill tracking and queue-priority
        preservation (mirrors chase_buy — see that docstring).

        Returns a dict with {average_price, order_id, filled_qty_btc,
        fully_filled, metrics} on any fill, else None on zero fill.
        Caller checks fully_filled to detect under-unwound positions.
        """
        deadline = time.time() + config.OPTION_EXIT_CHASE_DEADLINE_MIN * 60
        ct_val = config.OKX_CONTRACT_SIZE_BTC
        target_contracts = int(round(qty_btc / ct_val))
        if target_contracts <= 0:
            log.error("chase_sell_target_zero_contracts",
                      instrument=instrument, qty_btc=qty_btc, ct_val=ct_val)
            return None

        filled_contracts = 0
        weighted_value = 0.0
        last_ord_id = ""
        attempt = 0
        last_price = max(0.0, initial_ask)
        t_started = time.time()
        ref_bid = ref_ask = ref_mark = 0.0
        captured_ref = False
        # Base tick from OKX. CM tier-aware tick is computed PER-PRICE
        # below — see chase_buy for the rationale + family.py for the
        # tier table.
        base_tick = self.get_tick_size(instrument)
        if base_tick <= 0:
            base_tick = config.OPTION_TICK_SIZE

        # Resting-order state across iterations (queue-priority preserve)
        rested_ord_id: str = ""
        rested_price: float = 0.0
        rested_credited: int = 0
        # Per-order_id BTC fee accumulator (see chase_buy for rationale).
        fees_by_ord_id: dict[str, float] = {}

        FATAL_CODES = {
            "51000", "51001", "51008", "51010", "51016", "51019",
            "51020", "51115", "51121", "51169", "51198",
        }

        async def _credit_resting_fills(
            fallback_price: float,
        ) -> tuple[str, int]:
            """Mirrors chase_buy's _credit_resting_fills helper."""
            nonlocal filled_contracts, weighted_value, rested_credited
            if not rested_ord_id:
                return "", 0
            try:
                status = await self.get_order_status(instrument, rested_ord_id)
            except Exception:
                return "", rested_credited
            state = (status or {}).get("state", "")
            acc = int(self._f(status, "accFillSz", default=0.0))
            avg_px = self._f(status, "avgPx", default=fallback_price)
            if avg_px <= 0:
                avg_px = fallback_price
            delta = max(0, acc - rested_credited)
            if delta > 0:
                filled_contracts += delta
                weighted_value += delta * avg_px
                rested_credited = acc
                log.info("chase_sell_resting_fill_credit",
                         instrument=instrument, attempt=attempt,
                         ord_id=rested_ord_id, delta=delta,
                         total_filled=filled_contracts,
                         vwap=weighted_value / filled_contracts,
                         state=state)
            self._accumulate_fee(fees_by_ord_id, rested_ord_id, status)
            return state, acc

        # Wrap chase loop in try/except so a transient exception
        # (e.g. httpx.RemoteProtocolError) cannot bypass the post-
        # loop cleanup below and leak a resting order as an orphan
        # position. See 2026-05-23 utc_1430 incident.
        chase_loop_exception: BaseException | None = None
        try:
            while time.time() < deadline and filled_contracts < target_contracts:
                attempt += 1

                # ── 0. Credit any fills on the existing resting order ──
                if rested_ord_id:
                    rested_state, _ = await _credit_resting_fills(rested_price)
                    if rested_state in ("filled", "canceled", "cancelled"):
                        log.info("chase_sell_resting_terminal",
                                 instrument=instrument, attempt=attempt,
                                 ord_id=rested_ord_id, state=rested_state)
                        rested_ord_id = ""
                        rested_price = 0.0
                        rested_credited = 0
                    if filled_contracts >= target_contracts:
                        break

                remaining_contracts = target_contracts - filled_contracts
                remaining_qty_btc = remaining_contracts * ct_val

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

                if not captured_ref:
                    ref_bid, ref_ask, ref_mark = bid, ask, mark
                    captured_ref = True

                # Tier-aware tick at the CURRENT bid/mark price tier. Sell
                # chase is anchored on the bid side, so we use bid (or mark
                # fallback) as the price for tier lookup.
                tick_ref_price = bid if bid > 0 else (mark or last_price)
                tick = family.effective_tick_for_price(
                    tick_ref_price, instrument_default_tick=base_tick,
                )

                effective_ask = ask if ask > 0 else max(mark + tick, tick * 2)
                effective_bid = bid if bid > 0 else max(mark - tick, tick)

                target_bot = min(effective_ask, effective_bid + tick)
                new_price = last_price - (last_price - target_bot) \
                    * config.OPTION_CHASE_GAP_NARROW_PCT

                improvement_ceiling = effective_ask - tick
                if improvement_ceiling <= effective_bid:
                    ceiling_price = effective_ask
                else:
                    ceiling_price = improvement_ceiling
                new_price = min(new_price, ceiling_price)

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

                # Round UP to the tier-effective tick — sells must never
                # round down across a tier boundary (would risk crossing).
                new_price, eff_tick = family.round_price_to_tick(
                    new_price, instrument_default_tick=base_tick, direction="up",
                )
                if eff_tick != base_tick:
                    log.debug("chase_sell_tier_tick_engaged",
                              instrument=instrument, attempt=attempt,
                              tick_ref_price=tick_ref_price,
                              base_tick=base_tick,
                              effective_tick=eff_tick,
                              new_price=new_price)
                last_price = new_price

                # ── 1. Keep-alive: same price as resting order? ──
                same_price = (rested_ord_id and
                              abs(rested_price - new_price) < eff_tick * 0.5)

                if same_price:
                    log.info("chase_sell_keep_alive",
                             instrument=instrument, attempt=attempt,
                             price=new_price, bid=bid, ask=ask, mark=mark,
                             tick=eff_tick, base_tick=base_tick,
                             ord_id=rested_ord_id,
                             remaining_contracts=remaining_contracts,
                             filled_so_far=filled_contracts,
                             target_contracts=target_contracts)
                    await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                    continue

                # ── 2. Reprice: cancel resting order, credit any final fills ──
                if rested_ord_id:
                    log.info("chase_sell_reprice",
                             instrument=instrument, attempt=attempt,
                             from_price=rested_price, to_price=new_price,
                             ord_id=rested_ord_id)
                    stale_status = {
                        "state": "live",
                        "accFillSz": str(rested_credited),
                    }
                    filled_this, avg_px_this, order_state = \
                        await self._reconcile_after_wait(
                            instrument, rested_ord_id, stale_status,
                            rested_price, side="sell", attempt=attempt,
                            fees_by_ord_id=fees_by_ord_id,
                        )
                    delta = max(0, filled_this - rested_credited)
                    if delta > 0:
                        filled_contracts += delta
                        weighted_value += delta * avg_px_this
                        log.info("chase_sell_reprice_partial_credit",
                                 instrument=instrument, attempt=attempt,
                                 delta=delta, total_filled=filled_contracts,
                                 vwap=weighted_value / filled_contracts,
                                 state=order_state)
                    rested_ord_id = ""
                    rested_price = 0.0
                    rested_credited = 0
                    if order_state == "still_live_after_retries":
                        log.error("chase_sell_aborting_to_avoid_duplicate",
                                  instrument=instrument,
                                  attempt=attempt,
                                  filled_so_far=filled_contracts)
                        break
                    if filled_contracts >= target_contracts:
                        break
                    remaining_contracts = target_contracts - filled_contracts
                    remaining_qty_btc = remaining_contracts * ct_val

                log.info("chase_sell_attempt",
                         instrument=instrument, attempt=attempt,
                         price=new_price, bid=bid, ask=ask, mark=mark,
                         tick=eff_tick, base_tick=base_tick,
                         remaining_contracts=remaining_contracts,
                         filled_so_far=filled_contracts,
                         target_contracts=target_contracts)

                order = await self._place_limit_order(
                    instrument, "sell", remaining_qty_btc, new_price,
                    post_only=True,
                )
                ord_id = order.get("ordId")
                sCode = str(order.get("sCode") or "")
                sMsg = str(order.get("sMsg") or "")
                if ord_id:
                    last_ord_id = ord_id

                if sCode == "51120" or "would immediately match" in sMsg.lower() \
                        or "post_only" in sMsg.lower():
                    log.info("chase_sell_post_only_rejected",
                             instrument=instrument, attempt=attempt)
                    await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                    continue

                if sCode in FATAL_CODES:
                    log.error("chase_sell_fatal_reject",
                              instrument=instrument, sCode=sCode, sMsg=sMsg,
                              attempt=attempt, filled_so_far=filled_contracts)
                    await _notify_chase_failure(
                        side="sell", instrument=instrument,
                        qty_btc=qty_btc, reason="fatal_reject",
                        sCode=sCode, sMsg=sMsg, attempt=attempt,
                    )
                    break

                if not ord_id or sCode not in ("0", ""):
                    log.warning("chase_sell_order_rejected",
                                instrument=instrument, sCode=sCode, sMsg=sMsg,
                                attempt=attempt)
                    await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                    continue

                # Order successfully placed → mark as resting and wait
                rested_ord_id = ord_id
                rested_price = new_price
                rested_credited = 0
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)

        except Exception as exc:
            chase_loop_exception = exc
            log.error("chase_sell_loop_exception",
                      instrument=instrument, attempt=attempt,
                      filled_so_far=filled_contracts,
                      rested_ord_id=rested_ord_id,
                      rested_price=rested_price,
                      rested_credited=rested_credited,
                      exc_info=True)

        # ── Loop exit / exception cleanup: cancel any resting order ──
        # Wrapped in try/except so a cleanup failure does NOT mask the
        # loop exception captured above. Last-ditch fallback uses
        # cancel_orders_for_instrument so an orphan can't accrue
        # while OKX is intermittently flaky.
        if rested_ord_id:
            try:
                stale_status = {
                    "state": "live",
                    "accFillSz": str(rested_credited),
                }
                filled_this, avg_px_this, _state = \
                    await self._reconcile_after_wait(
                        instrument, rested_ord_id, stale_status,
                        rested_price, side="sell", attempt=attempt,
                        fees_by_ord_id=fees_by_ord_id,
                    )
                delta = max(0, filled_this - rested_credited)
                if delta > 0:
                    filled_contracts += delta
                    weighted_value += delta * avg_px_this
                    log.info("chase_sell_exit_partial_credit",
                             instrument=instrument, attempt=attempt,
                             delta=delta, total_filled=filled_contracts)
                rested_ord_id = ""
                rested_price = 0.0
                rested_credited = 0
            except Exception:
                log.error("chase_sell_cleanup_reconcile_failed",
                          instrument=instrument,
                          ord_id=rested_ord_id,
                          rested_price=rested_price,
                          exc_info=True)
                try:
                    cancelled = await self.cancel_orders_for_instrument(
                        instrument,
                    )
                    log.warning("chase_sell_emergency_cancel_done",
                                instrument=instrument,
                                cancelled=cancelled,
                                note="orphan_risk_check_post_close_reconcile")
                except Exception:
                    log.error("chase_sell_emergency_cancel_failed",
                              instrument=instrument,
                              exc_info=True)
                rested_ord_id = ""
                rested_price = 0.0
                rested_credited = 0

        # If the chase loop raised, surface it AFTER cleanup so any
        # resting order has already been cancelled. The caller
        # (build_straddle) sees the same exception type as before;
        # what changes is that no orphan is left behind.
        if chase_loop_exception is not None:
            await _notify_chase_failure(
                side="sell", instrument=instrument,
                qty_btc=qty_btc, reason="chase_loop_exception",
                sCode="", sMsg=type(chase_loop_exception).__name__,
                attempt=attempt,
            )
            raise chase_loop_exception

        if filled_contracts == 0:
            if attempt > 0 and time.time() >= deadline:
                log.error("chase_sell_deadline_exhausted",
                          instrument=instrument, attempts=attempt)
                await _notify_chase_failure(
                    side="sell", instrument=instrument,
                    qty_btc=qty_btc, reason="deadline_exhausted",
                    sCode="", sMsg="", attempt=attempt,
                )
            return None

        vwap = weighted_value / filled_contracts
        filled_qty_btc = filled_contracts * ct_val
        fully_filled = filled_contracts >= target_contracts
        t_filled = time.time()
        # Spot at fill is needed to convert native deltas to USD on CM
        # (no-op on UM). Best-effort; fallback to 0 if the call fails.
        try:
            spot_at_fill = await self.get_spot_price()
        except Exception:
            spot_at_fill = 0.0
        total_fee_native = sum(fees_by_ord_id.values())
        metrics = _build_fill_metrics(
            side="sell",
            instrument=instrument,
            qty_btc=filled_qty_btc,
            fill_price=vwap,
            t_started=t_started,
            t_filled=t_filled,
            attempts=attempt,
            ref_bid=ref_bid, ref_ask=ref_ask, ref_mark=ref_mark,
            spot_usd=spot_at_fill,
            fee_native=total_fee_native,
        )
        if fully_filled:
            log.info("chase_sell_filled",
                     instrument=instrument, avg=vwap, attempts=attempt,
                     duration_sec=metrics["duration_sec"],
                     slippage_vs_mark_pct=metrics["slippage_vs_mark_pct"],
                     saved_vs_taker_total_usd=metrics["saved_vs_taker_total_usd"],
                     fee_usd=metrics["fee_usd"])
        else:
            log.warning("chase_sell_partial_terminated",
                        instrument=instrument,
                        filled_contracts=filled_contracts,
                        target_contracts=target_contracts,
                        filled_qty_btc=filled_qty_btc,
                        target_qty_btc=qty_btc,
                        vwap=vwap, attempts=attempt)
            await _notify_partial_fill(
                side="sell", instrument=instrument,
                filled_contracts=filled_contracts,
                target_contracts=target_contracts,
                vwap=vwap,
            )
        return {
            "average_price": vwap,
            "order_id": last_ord_id,
            "avgPrice": vwap,
            "filled_qty_btc": filled_qty_btc,
            "fully_filled": fully_filled,
            "metrics": metrics,
        }

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
