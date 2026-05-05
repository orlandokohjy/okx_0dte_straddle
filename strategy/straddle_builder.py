"""
Straddle construction and teardown on OKX.

One straddle = 1 ITM call + 1 put (same strike) per QTY_PER_LEG BTC.

Entry order is chosen to minimise risk:
  1. Pre-entry spread gate — skip session if either leg's spread is too wide
  2. Optional RFQ atomic entry (if USE_RFQ=true and block trading available)
  3. Otherwise leg-by-leg with maker-only chase: PUT first, then CALL
       - if puts fail: skip session entirely (no naked exposure)
       - if calls fail after puts filled: emergency-sell puts to flatten

Exit: RFQ if available, else leg-by-leg chase.
"""
from __future__ import annotations

import uuid
from typing import Optional

import structlog

import config
from core import notifier
from core.exchange import OKXExchange
from core.portfolio import Portfolio, Straddle, StraddleLeg
from data.market_data import MarketData
from strategy.option_selector import StraddlePair
from utils.time_utils import now_utc

log = structlog.get_logger(__name__)


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
) -> Optional[Straddle]:
    """
    Execute the entry for N identical straddle units.

    Order: PUT first, then CALL. If puts fail, the session is skipped
    entirely (no spot/call exposure). If calls fail after puts filled, the
    puts are sold back via emergency unwind.
    """
    straddle_id = f"OKX-{uuid.uuid4().hex[:8]}"
    total_qty = config.QTY_PER_LEG * num_straddles

    log.info("building_straddle", id=straddle_id, strike=pair.strike,
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
        call_fill = rfq_result["call_price"]
        put_fill = rfq_result["put_price"]
        rfq_id = rfq_result.get("rfq_id", "")
        log.info("rfq_filled", id=straddle_id, rfq_id=rfq_id,
                 call=call_fill, put=put_fill)

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
        # ── Leg-by-leg: PUT first, then CALL ──
        # Use mark as starting reference if bid is missing (thin demo books).
        put_ref = pair.put.bid if pair.put.bid > 0 else pair.put.mark
        put_result = await exchange.chase_buy(
            pair.put.symbol, total_qty, put_ref,
        )
        if put_result is None:
            # Skip session entirely — no naked exposure
            log.error("put_buy_failed_skipping_session",
                      id=straddle_id, symbol=pair.put.symbol)
            await notifier.send(
                f"<b>SESSION SKIPPED</b> [{straddle_id}]\n"
                f"Put leg failed to fill within deadline.\n"
                f"No call leg attempted — no naked exposure.\n"
                f"Symbol: {pair.put.symbol}"
            )
            return None

        put_fill = float(put_result.get("average_price", pair.put.ask))
        log.info("put_filled", id=straddle_id, price=put_fill)

        put_leg = StraddleLeg(
            instrument=pair.put.symbol, side="Buy",
            qty=total_qty, entry_price=put_fill,
            order_id=put_result.get("order_id", ""),
            avg_fill_price=put_fill,
        )

        # Now buy the call. If this fails, emergency-sell the puts.
        call_ref = pair.call.bid if pair.call.bid > 0 else pair.call.mark
        call_result = await exchange.chase_buy(
            pair.call.symbol, total_qty, call_ref,
        )
        if call_result is None:
            log.error("call_buy_failed_after_put",
                      id=straddle_id, symbol=pair.call.symbol)
            await notifier.send(
                f"<b>⚠️ CALL FILL FAILED</b> [{straddle_id}]\n"
                f"Put filled but call failed. Selling puts to flatten."
            )
            await _emergency_sell(
                exchange, pair.put.symbol, total_qty, put_fill,
            )
            return None

        call_fill = float(call_result.get("average_price", pair.call.ask))
        log.info("call_filled", id=straddle_id, price=call_fill)

        call_leg = StraddleLeg(
            instrument=pair.call.symbol, side="Buy",
            qty=total_qty, entry_price=call_fill,
            order_id=call_result.get("order_id", ""),
            avg_fill_price=call_fill,
        )

    # ── Register ──
    straddle_cost = config.QTY_PER_LEG * (call_fill + put_fill)
    straddle = Straddle(
        id=straddle_id,
        call_leg=call_leg,
        put_leg=put_leg,
        strike=pair.strike,
        qty_per_leg=config.QTY_PER_LEG,
        entry_time=now_utc().isoformat(),
        entry_call_price=call_fill,
        entry_put_price=put_fill,
        straddle_cost=straddle_cost,
        num_straddles=num_straddles,
    )
    portfolio.set_straddle(straddle)
    log.info("straddle_built", id=straddle_id, num=num_straddles,
             cost=f"${straddle_cost * num_straddles:,.2f}",
             call=call_fill, put=put_fill, strike=pair.strike)
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

    rfq_result = await exchange.send_rfq_sell(
        straddle.call_leg.instrument,
        straddle.put_leg.instrument,
        straddle.call_leg.qty,
    )
    if rfq_result is not None:
        exit_call_price = rfq_result["call_price"]
        exit_put_price = rfq_result["put_price"]
        log.info("rfq_unwind_filled",
                 id=straddle.id,
                 call_exit=exit_call_price, put_exit=exit_put_price)
    else:
        # Sell call first, then put
        exit_call_price = straddle.entry_call_price
        _, call_ask = await market.get_option_bid_ask(
            straddle.call_leg.instrument,
        )
        if call_ask > 0:
            result = await exchange.chase_sell(
                straddle.call_leg.instrument,
                straddle.call_leg.qty, call_ask,
            )
            if result:
                exit_call_price = float(
                    result.get("average_price", call_ask),
                )
                log.info("call_sold", price=exit_call_price)
            else:
                log.warning("call_sell_failed",
                            instrument=straddle.call_leg.instrument)

        exit_put_price = straddle.entry_put_price
        _, put_ask = await market.get_option_bid_ask(
            straddle.put_leg.instrument,
        )
        if put_ask > 0:
            result = await exchange.chase_sell(
                straddle.put_leg.instrument,
                straddle.put_leg.qty, put_ask,
            )
            if result:
                exit_put_price = float(
                    result.get("average_price", put_ask),
                )
                log.info("put_sold", price=exit_put_price)
            else:
                log.warning("put_sell_failed",
                            instrument=straddle.put_leg.instrument)

    pnl = portfolio.close_straddle(exit_call_price, exit_put_price, reason)
    log.info("straddle_unwound", id=straddle.id, reason=reason,
             pnl=f"${pnl:,.2f}",
             exit_call=exit_call_price, exit_put=exit_put_price)
    return pnl


async def _emergency_sell(
    exchange: OKXExchange, instrument: str,
    qty: float, entry_price: float,
) -> None:
    """Sell a leg that filled during a failed build (rollback)."""
    try:
        ticker = await exchange.get_ticker(instrument)
        ask = ticker.ask if ticker.ask > 0 else entry_price
        result = await exchange.chase_sell(instrument, qty, ask)
        if result:
            log.info("emergency_sell_done", instrument=instrument,
                     price=result.get("average_price"))
            await notifier.send(
                f"<b>ROLLBACK COMPLETE</b>\n"
                f"Sold {qty} @ ${result.get('average_price', 0):,.2f}\n"
                f"Symbol: {instrument}"
            )
        else:
            log.error("emergency_sell_chase_exhausted", instrument=instrument)
            await notifier.send(
                f"<b>⚠️ ROLLBACK FAILED</b>\n"
                f"Could not sell {qty} of {instrument} within deadline.\n"
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
