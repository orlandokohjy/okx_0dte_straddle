"""
Straddle construction and teardown on OKX.

One straddle = 1 ITM call + 1 put (same strike) per `qty_per_leg` BTC.
The qty is sourced from the firing Session (config.SESSIONS) — the
afternoon session may use 0.5 BTC while the morning uses 0.25 BTC.

Entry strategy:
  1. Pre-entry spread gate — skip session if either leg's spread is too wide
  2. Optional RFQ atomic entry (if USE_RFQ=true and block trading available)
  3. Otherwise both legs fire CONCURRENTLY with maker-only chase
       - both fail  : skip session, no exposure
       - both fill  : straddle complete, register & notify
       - put fills, call fails : emergency-sell put + alert
       - call fills, put fails : emergency-sell call + alert

Concurrent firing reduces inter-leg slippage (both reference the same market
snapshot) and shrinks the orphan-position window: one leg can no longer be
sitting half-filled while the other is still waiting in the queue.

Exit: RFQ if available, else BOTH legs sold concurrently with maker chase.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Optional

import structlog

import config
from core import family, notifier
from core.exchange import OKXExchange, _TRANSIENT_HTTP_EXCEPTIONS
from core.portfolio import Portfolio, Straddle, StraddleLeg
from data.market_data import MarketData
from strategy.option_selector import StraddlePair, WingLeg, WingPair
from utils.time_utils import now_utc

log = structlog.get_logger(__name__)


def _format_leg_fill_message(
    *,
    leg: str,
    side: str,
    straddle_id: str,
    symbol: str,
    result: dict,
) -> str:
    """
    Format a per-leg Telegram fill message with execution-quality metrics.

    `side` is the human-friendly action label ("entry" for buys at the
    open, "exit" for sells at the close). The metrics dict is produced by
    core.exchange._build_fill_metrics; we surface the five numbers that
    matter most to a desk: avg fill, slippage vs mark, time to fill,
    attempts, and "Maker P&L (vs initial taker)" — the USD difference
    between the final maker fill and what a taker order at the START of
    the chase would have paid. Negative values mean the chase ended up
    costing more than just taking the initial offer.

    Native price formatting depends on the active family:
        CM → "0.0035 BTC"
        UM → "$285"
    """
    metrics = result.get("metrics") or {}
    avg = float(result.get("average_price", 0) or 0)
    mark = float(metrics.get("ref_mark", 0) or 0)
    slip = float(metrics.get("slippage_vs_mark_pct", 0) or 0)
    duration = float(metrics.get("duration_sec", 0) or 0)
    attempts = int(metrics.get("attempts", 0) or 0)
    # `saved_vs_taker_total_usd` compares the final maker fill against
    # the taker price *captured at chase START* (ref_ask for buys,
    # ref_bid for sells). Negative values mean the chase paid MORE than
    # if we had taken the initial offer immediately — i.e. the market
    # drifted away from us during the chase window. Surface as
    # "Maker P&L vs initial taker" so the operator doesn't mistake it
    # for a comparison against the *current* taker price.
    chase_pnl_usd = float(metrics.get("saved_vs_taker_total_usd", 0) or 0)
    fully = bool(result.get("fully_filled", True))
    qty_btc = float(result.get("filled_qty_btc", 0) or 0)

    header = f"LEG {'FILLED' if side == 'entry' else 'UNWOUND'} — {leg}"
    fill_line = f"Avg fill: {family.format_native_price(avg)}"
    if mark > 0:
        fill_line += f"  (mark {family.format_native_price(mark)})"
    slip_line = (
        f"Slippage vs mark: {slip:+.2f}%"
        if mark > 0 else "Slippage vs mark: n/a"
    )
    timing_line = f"Time to fill: {duration:.1f}s, attempts: {attempts}"
    saved_line = (
        f"Maker P&L (vs initial taker): ${chase_pnl_usd:+.2f}"
        if chase_pnl_usd != 0
        else "Maker P&L (vs initial taker): $0.00"
    )
    qty_line = f"Filled qty: {qty_btc:.4f} BTC"
    fully_line = "" if fully else "  ⚠️ PARTIAL"

    return (
        f"<b>{header}</b>{fully_line} [{straddle_id}] [{family.label()}]\n"
        f"Symbol: {symbol}\n"
        f"{fill_line}\n"
        f"{slip_line}\n"
        f"{timing_line}\n"
        f"{qty_line}\n"
        f"{saved_line}\n"
        f"Order id: {result.get('order_id', '')}"
    )


def _spread_pct(bid: float, ask: float, mark: float = 0.0) -> float:
    """
    Bid-ask spread as a fraction of mid (or mark if bid is missing).

    Thin demo books often have bid=0 with a valid ask. Falling back to
    `(ask − mark) / mark` keeps the spread gate meaningful instead of
    returning 100% and skipping every session.
    """
    if bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
        return (ask - bid) / mid if mid > 0 else 1.0
    if ask > 0 and mark > 0:
        return (ask - mark) / mark
    return 1.0


async def build_straddle(
    exchange: OKXExchange,
    market: MarketData,
    portfolio: Portfolio,
    pair: StraddlePair,
    num_straddles: int,
    qty_per_leg: float,
    session_name: str,
    entry_spot: float = 0.0,
    chase_deadline_min: Optional[float] = None,
) -> Optional[Straddle]:
    """
    Execute the entry for N identical straddle units.

    Order: PUT first, then CALL. If puts fail, the session is skipped
    entirely (no spot/call exposure). If calls fail after puts filled, the
    puts are sold back via emergency unwind.

    qty_per_leg is the BTC notional for a single leg unit and comes from
    the Session that fired the entry. session_name is stamped onto the
    resulting Straddle so close handlers and the daily report can keep
    each session's results separable.
    """
    # Session-distinguishing tag for the straddle id. utc_HHMM names map
    # to the 4-digit time so all four sessions get unique tags ("0900",
    # "1330", "2330", "0100"); legacy names fall back to the first letter
    # ("M" for morning, "A" for afternoon) for backward compatibility.
    if session_name.startswith("utc_") and len(session_name) >= 8:
        sess_tag = session_name[4:8]
    else:
        sess_tag = (session_name[:1] or "X").upper()
    straddle_id = f"OKX-{sess_tag}-{uuid.uuid4().hex[:8]}"
    total_qty = qty_per_leg * num_straddles

    log.info("building_straddle", id=straddle_id, session=session_name,
             strike=pair.strike, qty_per_leg=qty_per_leg,
             call=pair.call.symbol, put=pair.put.symbol, num=num_straddles)

    # ── Pre-entry spread gate ──
    call_spread = _spread_pct(pair.call.bid, pair.call.ask, pair.call.mark)
    put_spread = _spread_pct(pair.put.bid, pair.put.ask, pair.put.mark)
    if (call_spread > config.OPTION_MAX_ENTRY_SPREAD_PCT
            or put_spread > config.OPTION_MAX_ENTRY_SPREAD_PCT):
        msg = (
            f"Entry spread too wide — call={call_spread:.1%}, "
            f"put={put_spread:.1%}, "
            f"limit={config.OPTION_MAX_ENTRY_SPREAD_PCT:.0%}"
        )
        log.warning("spread_gate_skip", id=straddle_id, msg=msg)
        await notifier.notify_skip(msg)
        return None

    # ── Optional RFQ atomic entry ──
    rfq_result = await exchange.send_rfq(
        pair.call.symbol, pair.put.symbol, total_qty,
    )
    if rfq_result is not None:
        # We store OKX-native fill prices directly on the Straddle
        # (BTC for CM, USD-per-BTC-notional for UM). The portfolio's
        # call_pnl/put_pnl is family-aware and produces USD P&L for
        # both. See ``core.portfolio.Straddle.call_pnl`` for details.
        call_fill = float(rfq_result["call_price"])
        put_fill = float(rfq_result["put_price"])
        rfq_id = rfq_result.get("rfq_id", "")
        log.info("rfq_filled", id=straddle_id, rfq_id=rfq_id,
                 family=family.label(),
                 call=call_fill, put=put_fill,
                 unit=family.native_quote_unit_label())

        call_leg = StraddleLeg(
            instrument=pair.call.symbol, side="Buy",
            qty=total_qty, entry_price=call_fill,
            order_id=rfq_id, avg_fill_price=call_fill,
        )
        put_leg = StraddleLeg(
            instrument=pair.put.symbol, side="Buy",
            qty=total_qty, entry_price=put_fill,
            order_id=rfq_id, avg_fill_price=put_fill,
        )
    else:
        # ── Concurrent leg firing: PUT and CALL fire at the same time ──
        # Both reference the same market snapshot (decision-time bid/mark)
        # so the inter-leg skew that comes from sequential fills is gone.
        put_ref = pair.put.bid if pair.put.bid > 0 else pair.put.mark
        call_ref = pair.call.bid if pair.call.bid > 0 else pair.call.mark
        log.info("legs_firing_concurrently", id=straddle_id,
                 call=pair.call.symbol, put=pair.put.symbol,
                 call_ref=call_ref, put_ref=put_ref, qty=total_qty)

        put_task = asyncio.create_task(
            exchange.chase_buy(pair.put.symbol, total_qty, put_ref,
                               deadline_min=chase_deadline_min),
            name=f"chase_buy_put_{straddle_id}",
        )
        call_task = asyncio.create_task(
            exchange.chase_buy(pair.call.symbol, total_qty, call_ref,
                               deadline_min=chase_deadline_min),
            name=f"chase_buy_call_{straddle_id}",
        )
        # gather will return both results (or propagate the first exception
        # from either leg). We treat any unhandled exception as a None fill
        # for that leg and let the four-way handler decide what to do.
        results = await asyncio.gather(
            put_task, call_task, return_exceptions=True,
        )
        put_result, call_result = results

        if isinstance(put_result, BaseException):
            log.error("put_chase_exception", id=straddle_id,
                      exc_info=put_result)
            put_result = None
        if isinstance(call_result, BaseException):
            log.error("call_chase_exception", id=straddle_id,
                      exc_info=call_result)
            call_result = None

        # Promote partial-fill chase results to "failed" so they go through
        # the emergency-unwind path. The chase still returns a dict carrying
        # the partial filled_qty_btc + VWAP so we can flatten exactly what's
        # exposed instead of guessing the size.
        put_partial_qty: float = 0.0
        call_partial_qty: float = 0.0
        if put_result is not None and not put_result.get("fully_filled", True):
            put_partial_qty = float(put_result.get("filled_qty_btc", 0.0))
            log.warning("put_partial_fill_treating_as_failure",
                        id=straddle_id,
                        filled_qty_btc=put_partial_qty,
                        target_qty_btc=total_qty)
            put_result = None
        if call_result is not None and not call_result.get("fully_filled", True):
            call_partial_qty = float(call_result.get("filled_qty_btc", 0.0))
            log.warning("call_partial_fill_treating_as_failure",
                        id=straddle_id,
                        filled_qty_btc=call_partial_qty,
                        target_qty_btc=total_qty)
            call_result = None

        # Per-leg fill notification fires for FULL-fill legs only. Partial
        # fills are reported separately by the chase_*'s own notifier so
        # the operator sees both messages: "PARTIAL FILL DETECTED" first,
        # then the partial-leg-failure handling below.
        if put_result is not None:
            await notifier.send(
                _format_leg_fill_message(
                    leg="PUT",
                    side="entry",
                    straddle_id=straddle_id,
                    symbol=pair.put.symbol,
                    result=put_result,
                )
            )
        if call_result is not None:
            await notifier.send(
                _format_leg_fill_message(
                    leg="CALL",
                    side="entry",
                    straddle_id=straddle_id,
                    symbol=pair.call.symbol,
                    result=call_result,
                )
            )

        # ── Outcome dispatch: 4 cases ──
        # Note: put_partial_qty / call_partial_qty (set above) carry the
        # actual exposure if a leg was promoted from partial → failed. We
        # use those for emergency-sell sizing instead of `total_qty` so the
        # unwind targets exactly the live position, not the original target.
        if put_result is None and call_result is None:
            log.error("both_legs_failed_skipping_session",
                      id=straddle_id,
                      put_partial_qty=put_partial_qty,
                      call_partial_qty=call_partial_qty)
            await notifier.send(
                f"<b>SESSION SKIPPED</b> [{straddle_id}]\n"
                f"Both legs failed to fill within deadline.\n"
                f"Put partial residual: {put_partial_qty:.4f} BTC\n"
                f"Call partial residual: {call_partial_qty:.4f} BTC\n"
                f"Flattening any partial exposure now."
            )
            if put_partial_qty > 0:
                await _emergency_sell(
                    exchange, pair.put.symbol, put_partial_qty,
                    pair.put.ask,
                )
            if call_partial_qty > 0:
                await _emergency_sell(
                    exchange, pair.call.symbol, call_partial_qty,
                    pair.call.ask,
                )
            return None

        if put_result is not None and call_result is None:
            put_fill_for_emer = float(
                put_result.get("average_price", pair.put.ask),
            )
            put_qty_for_emer = float(
                put_result.get("filled_qty_btc", total_qty),
            )
            log.error("call_leg_failed_unwinding_put",
                      id=straddle_id, put_symbol=pair.put.symbol,
                      put_qty=put_qty_for_emer,
                      call_partial_qty=call_partial_qty)
            await notifier.send(
                f"<b>⚠️ PARTIAL FILL — CALL FAILED</b> [{straddle_id}]\n"
                f"Put filled {put_qty_for_emer:.4f} BTC @ {put_fill_for_emer:.4f}\n"
                f"Call partial residual: {call_partial_qty:.4f} BTC\n"
                f"Flattening both."
            )
            await _emergency_sell(
                exchange, pair.put.symbol, put_qty_for_emer, put_fill_for_emer,
            )
            if call_partial_qty > 0:
                await _emergency_sell(
                    exchange, pair.call.symbol, call_partial_qty,
                    pair.call.ask,
                )
            return None

        if call_result is not None and put_result is None:
            call_fill_for_emer = float(
                call_result.get("average_price", pair.call.ask),
            )
            call_qty_for_emer = float(
                call_result.get("filled_qty_btc", total_qty),
            )
            log.error("put_leg_failed_unwinding_call",
                      id=straddle_id, call_symbol=pair.call.symbol,
                      call_qty=call_qty_for_emer,
                      put_partial_qty=put_partial_qty)
            await notifier.send(
                f"<b>⚠️ PARTIAL FILL — PUT FAILED</b> [{straddle_id}]\n"
                f"Call filled {call_qty_for_emer:.4f} BTC @ {call_fill_for_emer:.4f}\n"
                f"Put partial residual: {put_partial_qty:.4f} BTC\n"
                f"Flattening both."
            )
            await _emergency_sell(
                exchange, pair.call.symbol, call_qty_for_emer,
                call_fill_for_emer,
            )
            if put_partial_qty > 0:
                await _emergency_sell(
                    exchange, pair.put.symbol, put_partial_qty,
                    pair.put.ask,
                )
            return None

        # Both filled — build the legs.
        # Storage convention: OKX-native premium per BTC of notional.
        #   CM (inverse) → BTC per BTC of notional (e.g. 0.0035)
        #   UM (linear)  → USD per BTC of notional (e.g. 285)
        # Portfolio.Straddle.call_pnl is family-aware; it multiplies CM
        # premiums by spot to convert to USD, while UM premiums are
        # already USD and used directly. This avoids the precision loss
        # from converting through a BTC-equivalent intermediate when
        # spot drifts during the chase.
        put_fill = float(put_result.get("average_price", pair.put.ask))
        call_fill = float(call_result.get("average_price", pair.call.ask))
        put_metrics = put_result.get("metrics", {}) or {}
        call_metrics = call_result.get("metrics", {}) or {}
        log.info("both_legs_filled", id=straddle_id, family=family.label(),
                 call=call_fill, put=put_fill,
                 unit=family.native_quote_unit_label())

        put_leg = StraddleLeg(
            instrument=pair.put.symbol, side="Buy",
            qty=total_qty, entry_price=put_fill,
            order_id=put_result.get("order_id", ""),
            avg_fill_price=put_fill,
            entry_metrics=put_metrics,
        )
        call_leg = StraddleLeg(
            instrument=pair.call.symbol, side="Buy",
            qty=total_qty, entry_price=call_fill,
            order_id=call_result.get("order_id", ""),
            avg_fill_price=call_fill,
            entry_metrics=call_metrics,
        )

    # ── Register ──
    # straddle_cost is in OKX-native units per straddle:
    #   CM (inverse) → BTC of premium per straddle
    #   UM (linear)  → USD of premium per straddle
    # Portfolio renderers convert to USD via family-aware Straddle helpers,
    # so this stays the source-of-truth value the rest of the system
    # consumes. The "cost" log line below is for human eyes only and is
    # converted to USD via family.native_premium_to_usd so the operator
    # never sees a misleading "$0.01" for a CM trade (regression caught
    # 2026-05-18: a 0.5 BTC × 0.0085 BTC/BTC straddle was being printed
    # as "$0.01" because BTC was being formatted with a $ prefix).
    straddle_cost = qty_per_leg * (call_fill + put_fill)

    # Capture spot if caller didn't provide it (rare path).
    if entry_spot <= 0:
        try:
            entry_spot = await exchange.get_spot_price()
        except Exception:
            entry_spot = 0.0

    straddle = Straddle(
        id=straddle_id,
        call_leg=call_leg,
        put_leg=put_leg,
        strike=pair.strike,
        qty_per_leg=qty_per_leg,
        entry_time=now_utc().isoformat(),
        entry_call_price=call_fill,
        entry_put_price=put_fill,
        straddle_cost=straddle_cost,
        num_straddles=num_straddles,
        entry_spot_price=entry_spot,
        session_name=session_name,
        family=family.label(),
    )
    portfolio.set_straddle(straddle)

    # Best-effort implied-vol capture (analytics only). The entry is ALREADY
    # filled and persisted above, so nothing here can affect execution — a
    # failure/timeout just leaves IV at 0.0. Re-persist only if we got a
    # value so positions.json reflects it.
    try:
        ivs = await exchange.get_option_iv_batch([
            straddle.call_leg.instrument, straddle.put_leg.instrument,
        ])
        straddle.entry_call_iv = ivs.get(
            straddle.call_leg.instrument, {}).get("mark_vol", 0.0)
        straddle.entry_put_iv = ivs.get(
            straddle.put_leg.instrument, {}).get("mark_vol", 0.0)
        if straddle.entry_call_iv or straddle.entry_put_iv:
            portfolio.set_straddle(straddle)
            log.info("entry_iv_captured", id=straddle_id,
                     call_iv=straddle.entry_call_iv,
                     put_iv=straddle.entry_put_iv)
    except Exception:
        log.warning("entry_iv_capture_failed", id=straddle_id, exc_info=True)

    # Total premium paid across all N straddles, converted to USD for the
    # log line. CM: native is BTC → USD via spot. UM: native is already
    # USD → spot is a no-op. If spot is unavailable on a CM run we fall
    # back to a "n/a" string rather than printing a wrong dollar value.
    total_cost_usd = family.native_premium_to_usd(
        call_fill + put_fill,
        qty_per_leg * num_straddles,
        entry_spot,
    )
    if total_cost_usd > 0:
        cost_str = f"${total_cost_usd:,.2f}"
    else:
        cost_str = (f"n/a (spot=0, native={straddle_cost * num_straddles:.4f} "
                    f"{family.native_quote_unit_label()})")
    log.info("straddle_built", id=straddle_id, session=session_name,
             num=num_straddles,
             cost=cost_str,
             call=call_fill, put=put_fill, strike=pair.strike,
             spot=entry_spot,
             family=family.label(),
             unit=family.native_quote_unit_label())
    return straddle


async def unwind_straddle(
    exchange: OKXExchange,
    market: MarketData,
    portfolio: Portfolio,
    reason: str = "hard_close",
) -> float:
    """
    Close the open straddle.
    Primary: RFQ sell both legs atomically (if USE_RFQ=true).
    Fallback: leg-by-leg maker-only chase.
    """
    straddle = portfolio.open_straddle
    if straddle is None:
        return 0.0

    log.info("unwinding", id=straddle.id, reason=reason)

    # Capture spot at exit for context — best-effort, never blocks the unwind.
    try:
        exit_spot = await exchange.get_spot_price()
    except Exception:
        exit_spot = 0.0
    if exit_spot > 0:
        straddle.exit_spot_price = exit_spot

    # ── SHORTS-FIRST: buy back the covered wings BEFORE selling the body ──
    # If we sold the body first we would momentarily hold only the short
    # wings = a naked short strangle. Closing the shorts first leaves us
    # holding the long body (safe) until the body sells.
    wing_exits: dict = {"call": None, "put": None}
    if straddle.has_wings:
        log.info("closing_wings_first", id=straddle.id,
                 has_call_wing=straddle.has_call_wing,
                 has_put_wing=straddle.has_put_wing)
        wing_exits = await unwind_wings(exchange, market, portfolio, straddle)

    # ── SHORTS-FIRST body sell ──────────────────────────────────────────
    # Never sell a body leg while its covering short wing is still open:
    # unwind_wings re-read the live book and told us which wings are STILL
    # short (call_open / put_open). We HOLD those body legs (each long body
    # covers its short wing) and let the shorts-first post-close reconcile
    # buy the wing back first, then sell the body. This closes the
    # naked-short window that existed when a wing buyback failed but the
    # body sold anyway. Initialise exit prices to entry now so a held or
    # failed leg gets 0 P&L instead of crashing on missing fields.
    exit_call_price = straddle.entry_call_price
    exit_put_price = straddle.entry_put_price
    defer_call_body = bool(wing_exits.get("call_open"))
    defer_put_body = bool(wing_exits.get("put_open"))
    if defer_call_body or defer_put_body:
        log.warning("body_sell_deferred_wing_open", id=straddle.id,
                    defer_call=defer_call_body, defer_put=defer_put_body)
        await notifier.send(
            f"<b>⛔ HOLDING BODY LEG — WING STILL SHORT</b> [{straddle.id}]\n"
            f"A short wing did not buy back, so its covering LONG body leg is "
            f"HELD (not sold) to avoid a naked short. The shorts-first "
            f"post-close reconcile buys the wing back FIRST, then sells the "
            f"body.\n"
            f"Holding call body: {defer_call_body} | "
            f"put body: {defer_put_body}"
        )

    # Skip the atomic 2-leg RFQ when deferring — it sells BOTH legs at once,
    # which would flatten a body leg we must hold as cover.
    rfq_result = None
    if not (defer_call_body or defer_put_body):
        rfq_result = await exchange.send_rfq_sell(
            straddle.call_leg.instrument,
            straddle.put_leg.instrument,
            straddle.call_leg.qty,
        )
    if rfq_result is not None:
        # Store native exit prices directly — Straddle.call_pnl is
        # family-aware (BTC × spot for CM, USD × 1 for UM).
        exit_call_price = float(rfq_result["call_price"])
        exit_put_price = float(rfq_result["put_price"])
        log.info("rfq_unwind_filled",
                 id=straddle.id, family=family.label(),
                 call=exit_call_price, put=exit_put_price,
                 unit=family.native_quote_unit_label())
    else:
        # ── Per-leg maker chase: sell ONLY the legs whose covering wing is
        # closed/absent; a deferred leg is held as cover (result stays None).

        _, call_ask = await market.get_option_bid_ask(
            straddle.call_leg.instrument,
        )
        _, put_ask = await market.get_option_bid_ask(
            straddle.put_leg.instrument,
        )

        async def _sell_leg(symbol: str, qty: float, ref_ask: float):
            if ref_ask <= 0:
                return None
            try:
                return await exchange.chase_sell(symbol, qty, ref_ask)
            except _TRANSIENT_HTTP_EXCEPTIONS as exc:
                # A transient disconnect (e.g. httpx.RemoteProtocolError —
                # OKX's HTTP/2 frontend dropping the socket mid-chase)
                # aborts the WHOLE chase and re-raises. chase_sell already
                # cancelled its resting order before re-raising, so a retry
                # starts from a clean book. Without this, a single network
                # blip on close leaves the leg unsold and falls back to the
                # entry price → phantom close + orphan (2026-06-18 wd_1400
                # incident).
                #
                # CRITICAL: re-query the LIVE position before retrying. The
                # disconnect can happen *after* OKX accepted/filled some or
                # all of the original chase — a blind re-send of the full
                # `qty` would then oversell into a SHORT (the 2026-06-2x
                # post-close orphan: -50 contracts on both legs). Only resell
                # whatever long remains; if already flat/short, do nothing.
                log.warning("sell_leg_transient_retry",
                            instrument=symbol,
                            exc_type=type(exc).__name__)
                await asyncio.sleep(2.0)
                try:
                    positions = await exchange.list_open_positions()
                except Exception:
                    log.error("sell_leg_requery_failed",
                              instrument=symbol, exc_info=True)
                    return None
                remaining_contracts = 0.0
                for p in positions:
                    if p.get("instrument_name") == symbol:
                        remaining_contracts = float(p.get("amount", 0.0))
                        break
                if remaining_contracts <= 0:
                    # Original chase already flattened (or even shorted) the
                    # leg before the disconnect — nothing left to sell.
                    log.info("sell_leg_already_flat_after_transient",
                             instrument=symbol,
                             remaining_contracts=remaining_contracts)
                    return None
                remaining_qty = remaining_contracts * config.OKX_CONTRACT_SIZE_BTC
                try:
                    _, fresh_ask = await market.get_option_bid_ask(symbol)
                except Exception:
                    fresh_ask = 0.0
                use_ask = fresh_ask if fresh_ask > 0 else ref_ask
                try:
                    return await exchange.chase_sell(
                        symbol, remaining_qty, use_ask,
                    )
                except _TRANSIENT_HTTP_EXCEPTIONS:
                    # Second transient failure — give up; the caller's
                    # leg-failure path + post-close persistent re-flatten
                    # (then orphan lock) take over.
                    log.error("sell_leg_transient_retry_failed",
                              instrument=symbol, exc_info=True)
                    return None

        call_task = (
            asyncio.create_task(_sell_leg(
                straddle.call_leg.instrument,
                straddle.call_leg.qty, call_ask,
            ), name=f"chase_sell_call_{straddle.id}")
            if not defer_call_body else None
        )
        put_task = (
            asyncio.create_task(_sell_leg(
                straddle.put_leg.instrument,
                straddle.put_leg.qty, put_ask,
            ), name=f"chase_sell_put_{straddle.id}")
            if not defer_put_body else None
        )

        async def _await_or_none(task):
            return await task if task is not None else None

        results = await asyncio.gather(
            _await_or_none(call_task), _await_or_none(put_task),
            return_exceptions=True,
        )
        call_result, put_result = results

        if isinstance(call_result, BaseException):
            log.error("call_unwind_exception", exc_info=call_result)
            call_result = None
        if isinstance(put_result, BaseException):
            log.error("put_unwind_exception", exc_info=put_result)
            put_result = None

        if call_result:
            call_metrics = call_result.get("metrics", {}) or {}
            exit_call_price = float(
                call_result.get("average_price", call_ask),
            )
            straddle.call_leg.exit_metrics = call_metrics
            call_fully = call_result.get("fully_filled", True)
            call_filled_btc = float(
                call_result.get("filled_qty_btc", straddle.call_leg.qty),
            )
            log.info("call_sold", price=exit_call_price,
                     family=family.label(),
                     unit=family.native_quote_unit_label(),
                     fully_filled=call_fully,
                     filled_btc=call_filled_btc,
                     target_btc=straddle.call_leg.qty)
            await notifier.send(
                _format_leg_fill_message(
                    leg="CALL",
                    side="exit",
                    straddle_id=straddle.id,
                    symbol=straddle.call_leg.instrument,
                    result=call_result,
                )
            )
            if not call_fully:
                await notifier.send(
                    f"<b>⚠️ CALL UNWIND PARTIAL</b> [{straddle.id}]\n"
                    f"Symbol: {straddle.call_leg.instrument}\n"
                    f"Sold {call_filled_btc:.4f} of {straddle.call_leg.qty:.4f} BTC\n"
                    f"Residual: {straddle.call_leg.qty - call_filled_btc:.4f} BTC\n"
                    f"post_close_reconcile will flag the orphan."
                )
        elif defer_call_body:
            log.info("call_body_held_as_cover", id=straddle.id,
                     instrument=straddle.call_leg.instrument)
        else:
            log.warning("call_sell_failed",
                        instrument=straddle.call_leg.instrument)
            await notifier.send(
                f"<b>⚠️ CALL UNWIND FAILED</b> [{straddle.id}]\n"
                f"Symbol: {straddle.call_leg.instrument}\n"
                f"Could not sell within deadline. Manual action may be needed."
            )

        if put_result:
            put_metrics = put_result.get("metrics", {}) or {}
            exit_put_price = float(
                put_result.get("average_price", put_ask),
            )
            straddle.put_leg.exit_metrics = put_metrics
            put_fully = put_result.get("fully_filled", True)
            put_filled_btc = float(
                put_result.get("filled_qty_btc", straddle.put_leg.qty),
            )
            log.info("put_sold", price=exit_put_price,
                     family=family.label(),
                     unit=family.native_quote_unit_label(),
                     fully_filled=put_fully,
                     filled_btc=put_filled_btc,
                     target_btc=straddle.put_leg.qty)
            await notifier.send(
                _format_leg_fill_message(
                    leg="PUT",
                    side="exit",
                    straddle_id=straddle.id,
                    symbol=straddle.put_leg.instrument,
                    result=put_result,
                )
            )
            if not put_fully:
                await notifier.send(
                    f"<b>⚠️ PUT UNWIND PARTIAL</b> [{straddle.id}]\n"
                    f"Symbol: {straddle.put_leg.instrument}\n"
                    f"Sold {put_filled_btc:.4f} of {straddle.put_leg.qty:.4f} BTC\n"
                    f"Residual: {straddle.put_leg.qty - put_filled_btc:.4f} BTC\n"
                    f"post_close_reconcile will flag the orphan."
                )
        elif defer_put_body:
            log.info("put_body_held_as_cover", id=straddle.id,
                     instrument=straddle.put_leg.instrument)
        else:
            log.warning("put_sell_failed",
                        instrument=straddle.put_leg.instrument)
            await notifier.send(
                f"<b>⚠️ PUT UNWIND FAILED</b> [{straddle.id}]\n"
                f"Symbol: {straddle.put_leg.instrument}\n"
                f"Could not sell within deadline. Manual action may be needed."
            )

    # Best-effort exit IV (analytics only). Runs AFTER the legs are unwound,
    # so a slow/failed snapshot can never delay or abort the close; on any
    # error IV stays 0.0. Set before close_straddle so _log_trade records it.
    try:
        ivs = await exchange.get_option_iv_batch([
            straddle.call_leg.instrument, straddle.put_leg.instrument,
        ])
        straddle.exit_call_iv = ivs.get(
            straddle.call_leg.instrument, {}).get("mark_vol", 0.0)
        straddle.exit_put_iv = ivs.get(
            straddle.put_leg.instrument, {}).get("mark_vol", 0.0)
    except Exception:
        log.warning("exit_iv_capture_failed", id=straddle.id, exc_info=True)

    pnl = portfolio.close_straddle(
        exit_call_price, exit_put_price, reason,
        exit_call_wing_price=wing_exits.get("call"),
        exit_put_wing_price=wing_exits.get("put"),
    )
    log.info("straddle_unwound", id=straddle.id, reason=reason,
             pnl=f"${pnl:,.2f}",
             exit_call=exit_call_price, exit_put=exit_put_price,
             wing_call_exit=wing_exits.get("call"),
             wing_put_exit=wing_exits.get("put"))
    return pnl


async def build_wings(
    exchange: OKXExchange,
    market: MarketData,
    portfolio: Portfolio,
    straddle: Straddle,
    wings: WingPair,
    *,
    chase_deadline_min: Optional[float] = None,
) -> None:
    """Sell the covered wings AFTER the body is filled (LONGS-FIRST entry).

    Best-effort and NON-FATAL: an unsold or partially-sold wing simply
    leaves the position body-only (long straddle) on that side — always
    covered, never naked-short. Sold wings are recorded onto ``straddle``
    and persisted. Uses maker-only ``chase_sell(opening=True)`` so a 51008
    margin reject aborts that wing instead of taker-opening a short.
    """
    if wings is None or not wings.any:
        log.info("no_wings_to_sell", id=straddle.id)
        return

    # Wing qty matches the body leg qty (one wing per straddle unit).
    total_qty = straddle.call_leg.qty
    deadline = (
        chase_deadline_min if chase_deadline_min is not None
        else config.WING_CHASE_DEADLINE_MIN
    )

    async def _sell_wing(wing: Optional[WingLeg], label: str):
        if wing is None:
            return None
        opt = wing.option
        spr = _spread_pct(opt.bid, opt.ask, opt.mark)
        if spr > config.WING_MAX_ENTRY_SPREAD_PCT:
            log.warning("wing_spread_gate_skip", leg=label,
                        symbol=opt.symbol, spread=spr,
                        limit=config.WING_MAX_ENTRY_SPREAD_PCT)
            await notifier.send(
                f"<b>WING SKIPPED — wide spread</b> [{straddle.id}]\n"
                f"{label} {opt.symbol} strike ${wing.strike:,.0f}\n"
                f"spread={spr:.1%} &gt; {config.WING_MAX_ENTRY_SPREAD_PCT:.0%}"
            )
            return None
        # Sell: start near the ask and walk DOWN toward the bid (maker).
        ref_ask = opt.ask if opt.ask > 0 else opt.mark
        if ref_ask <= 0:
            log.warning("wing_no_ask", leg=label, symbol=opt.symbol)
            return None
        try:
            return await exchange.chase_sell(
                opt.symbol, total_qty, ref_ask,
                deadline_min=deadline, opening=True,
            )
        except _TRANSIENT_HTTP_EXCEPTIONS as exc:
            log.warning("wing_sell_transient", leg=label, symbol=opt.symbol,
                        exc_type=type(exc).__name__)
            return None

    call_task = asyncio.create_task(
        _sell_wing(wings.call, "CALL_WING"), name=f"wing_call_{straddle.id}")
    put_task = asyncio.create_task(
        _sell_wing(wings.put, "PUT_WING"), name=f"wing_put_{straddle.id}")
    call_res, put_res = await asyncio.gather(
        call_task, put_task, return_exceptions=True,
    )
    if isinstance(call_res, BaseException):
        log.error("wing_call_exception", exc_info=call_res)
        call_res = None
    if isinstance(put_res, BaseException):
        log.error("wing_put_exception", exc_info=put_res)
        put_res = None

    if call_res and wings.call is not None:
        # Even a partial wing fill is covered (long call K covers short
        # call K+n). Record exactly what filled so the close buys back the
        # right size.
        px = float(call_res.get("average_price", 0.0))
        filled_btc = float(call_res.get("filled_qty_btc", total_qty))
        if filled_btc > 0 and px > 0:
            straddle.call_wing_leg = StraddleLeg(
                instrument=wings.call.option.symbol, side="Sell",
                qty=filled_btc, entry_price=px,
                order_id=call_res.get("order_id", ""), avg_fill_price=px,
                entry_metrics=call_res.get("metrics", {}) or {},
            )
            straddle.call_wing_strike = wings.call.strike
            straddle.entry_call_wing_price = px
            await notifier.send(_format_leg_fill_message(
                leg="CALL WING (short)", side="entry",
                straddle_id=straddle.id,
                symbol=wings.call.option.symbol, result=call_res))

    if put_res and wings.put is not None:
        px = float(put_res.get("average_price", 0.0))
        filled_btc = float(put_res.get("filled_qty_btc", total_qty))
        if filled_btc > 0 and px > 0:
            straddle.put_wing_leg = StraddleLeg(
                instrument=wings.put.option.symbol, side="Sell",
                qty=filled_btc, entry_price=px,
                order_id=put_res.get("order_id", ""), avg_fill_price=px,
                entry_metrics=put_res.get("metrics", {}) or {},
            )
            straddle.put_wing_strike = wings.put.strike
            straddle.entry_put_wing_price = px
            await notifier.send(_format_leg_fill_message(
                leg="PUT WING (short)", side="entry",
                straddle_id=straddle.id,
                symbol=wings.put.option.symbol, result=put_res))

    portfolio.set_straddle(straddle)
    log.info("wings_built", id=straddle.id,
             call_wing=straddle.call_wing_strike if straddle.has_call_wing else None,
             put_wing=straddle.put_wing_strike if straddle.has_put_wing else None,
             call_credit=straddle.entry_call_wing_price,
             put_credit=straddle.entry_put_wing_price)
    if straddle.has_wings:
        await notifier.send(
            f"<b>WINGS SOLD</b> [{straddle.id}]\n"
            f"Structure is now a covered iron fly.\n"
            f"Call wing: "
            f"{('$%.0f' % straddle.call_wing_strike) if straddle.has_call_wing else '—'}\n"
            f"Put wing:  "
            f"{('$%.0f' % straddle.put_wing_strike) if straddle.has_put_wing else '—'}"
        )


async def unwind_wings(
    exchange: OKXExchange,
    market: MarketData,
    portfolio: Portfolio,
    straddle: Straddle,
) -> dict:
    """Buy-to-close the short wings FIRST (SHORTS-FIRST close) so the body is
    never left naked mid-unwind. Returns ``{'call': px|None, 'put': px|None}``
    with the buy-back debit prices for close P&L. Best-effort: a wing that
    fails to buy back is left for the position-aware post-close reconcile,
    and is covered by the body in the meantime.
    """
    result: dict = {"call": None, "put": None}

    async def _buy_close(leg, label: str):
        if leg is None:
            return None
        bid, _ask = await market.get_option_bid_ask(leg.instrument)
        ref_bid = bid if bid > 0 else leg.entry_price
        if ref_bid <= 0:
            return None
        try:
            return await exchange.chase_buy(
                leg.instrument, leg.qty, ref_bid,
                deadline_min=config.WING_CHASE_DEADLINE_MIN,
            )
        except _TRANSIENT_HTTP_EXCEPTIONS as exc:
            log.warning("wing_buyback_transient", leg=label,
                        instrument=leg.instrument,
                        exc_type=type(exc).__name__)
            return None

    call_task = asyncio.create_task(
        _buy_close(straddle.call_wing_leg, "CALL_WING"),
        name=f"wing_close_call_{straddle.id}")
    put_task = asyncio.create_task(
        _buy_close(straddle.put_wing_leg, "PUT_WING"),
        name=f"wing_close_put_{straddle.id}")
    call_res, put_res = await asyncio.gather(
        call_task, put_task, return_exceptions=True,
    )
    if isinstance(call_res, BaseException):
        log.error("wing_close_call_exception", exc_info=call_res)
        call_res = None
    if isinstance(put_res, BaseException):
        log.error("wing_close_put_exception", exc_info=put_res)
        put_res = None

    if call_res and straddle.call_wing_leg is not None:
        px = float(call_res.get("average_price", 0.0))
        if px > 0:
            result["call"] = px
            straddle.call_wing_leg.exit_metrics = call_res.get("metrics", {}) or {}
            await notifier.send(_format_leg_fill_message(
                leg="CALL WING (buy-to-close)", side="exit",
                straddle_id=straddle.id,
                symbol=straddle.call_wing_leg.instrument, result=call_res))
        if not call_res.get("fully_filled", True):
            await notifier.send(
                f"<b>⚠️ CALL WING BUYBACK PARTIAL</b> [{straddle.id}]\n"
                f"{straddle.call_wing_leg.instrument} — residual short may "
                f"remain; post-close reconcile will buy it back."
            )
    elif straddle.call_wing_leg is not None:
        await notifier.send(
            f"<b>⚠️ CALL WING BUYBACK FAILED</b> [{straddle.id}]\n"
            f"{straddle.call_wing_leg.instrument} — still short; body still "
            f"covers it. Post-close reconcile will buy it back."
        )

    if put_res and straddle.put_wing_leg is not None:
        px = float(put_res.get("average_price", 0.0))
        if px > 0:
            result["put"] = px
            straddle.put_wing_leg.exit_metrics = put_res.get("metrics", {}) or {}
            await notifier.send(_format_leg_fill_message(
                leg="PUT WING (buy-to-close)", side="exit",
                straddle_id=straddle.id,
                symbol=straddle.put_wing_leg.instrument, result=put_res))
        if not put_res.get("fully_filled", True):
            await notifier.send(
                f"<b>⚠️ PUT WING BUYBACK PARTIAL</b> [{straddle.id}]\n"
                f"{straddle.put_wing_leg.instrument} — residual short may "
                f"remain; post-close reconcile will buy it back."
            )
    elif straddle.put_wing_leg is not None:
        await notifier.send(
            f"<b>⚠️ PUT WING BUYBACK FAILED</b> [{straddle.id}]\n"
            f"{straddle.put_wing_leg.instrument} — still short; body still "
            f"covers it. Post-close reconcile will buy it back."
        )

    # ── Authoritative "is the short wing STILL open?" verdict ────────────
    # The caller (unwind_straddle) must NOT sell the covering body leg while
    # its short wing is still open (shorts-first). A confirmed-flat live read
    # is the ONLY thing that releases the body; a wing that existed but whose
    # buyback we can't verify (fetch error / partial) is treated as STILL
    # OPEN so we err on the side of holding the cover. A side that never had
    # a wing is False (nothing to cover → body sells normally).
    call_open = straddle.call_wing_leg is not None
    put_open = straddle.put_wing_leg is not None
    try:
        live = await exchange.list_open_positions()
        live_amt = {
            p.get("instrument_name"): float(p.get("amount", 0.0) or 0.0)
            for p in live
        }
        if straddle.call_wing_leg is not None:
            call_open = live_amt.get(
                straddle.call_wing_leg.instrument, 0.0) < 0
        if straddle.put_wing_leg is not None:
            put_open = live_amt.get(
                straddle.put_wing_leg.instrument, 0.0) < 0
    except Exception:
        log.warning("wing_close_position_recheck_failed", id=straddle.id,
                    exc_info=True)
        # keep fail-safe defaults (a wing that existed is assumed still open)
    result["call_open"] = call_open
    result["put_open"] = put_open

    log.info("wings_unwound", id=straddle.id,
             call_exit=result["call"], put_exit=result["put"],
             call_open=call_open, put_open=put_open)
    return result


async def _emergency_sell(
    exchange: OKXExchange, instrument: str,
    qty: float, entry_price: float,
) -> None:
    """Sell a leg that filled during a failed build (rollback).

    qty is in BTC notional and represents the actual position to flatten —
    NOT the original target. Caller is responsible for passing the real
    filled_qty_btc when a partial fill is being unwound.
    """
    if qty <= 0:
        log.info("emergency_sell_skip_zero_qty", instrument=instrument)
        return
    try:
        # Defensive: clear any stuck buy orders for this instrument before
        # selling. Otherwise a not-yet-cancelled buy could fill more
        # contracts while our sell is resting, leaving a residual.
        try:
            cleared = await exchange.cancel_orders_for_instrument(instrument)
            if cleared > 0:
                log.info("emergency_sell_pre_cancel_done",
                         instrument=instrument, cancelled=cleared)
        except Exception:
            log.warning("emergency_sell_pre_cancel_failed",
                        instrument=instrument, exc_info=True)

        ticker = await exchange.get_ticker(instrument)
        ask = ticker.ask if ticker.ask > 0 else entry_price
        result = await exchange.chase_sell(instrument, qty, ask)
        if result:
            avg_px = result.get("average_price", 0)
            fully = result.get("fully_filled", True)
            sold_btc = float(result.get("filled_qty_btc", qty))
            log.info("emergency_sell_done", instrument=instrument,
                     price=avg_px, fully_filled=fully,
                     sold_btc=sold_btc, target_btc=qty)
            if fully:
                await notifier.send(
                    f"<b>ROLLBACK COMPLETE</b>\n"
                    f"Sold {qty:.4f} BTC @ {avg_px:.4f}\n"
                    f"Symbol: {instrument}"
                )
            else:
                await notifier.send(
                    f"<b>⚠️ ROLLBACK PARTIAL</b>\n"
                    f"Sold {sold_btc:.4f} of {qty:.4f} BTC @ {avg_px:.4f}\n"
                    f"Residual: {qty - sold_btc:.4f} BTC\n"
                    f"Symbol: {instrument}\n"
                    f"<b>MANUAL ACTION REQUIRED for residual.</b>"
                )
        else:
            log.error("emergency_sell_chase_exhausted", instrument=instrument)
            await notifier.send(
                f"<b>⚠️ ROLLBACK FAILED</b>\n"
                f"Could not sell {qty:.4f} BTC of {instrument} within deadline.\n"
                f"<b>MANUAL ACTION REQUIRED.</b>"
            )
    except Exception:
        log.error("emergency_sell_failed", instrument=instrument,
                  exc_info=True)
        await notifier.send(
            f"<b>⚠️ ROLLBACK EXCEPTION</b>\n"
            f"Could not sell {qty} of {instrument}: see logs.\n"
            f"<b>MANUAL ACTION REQUIRED.</b>"
        )
