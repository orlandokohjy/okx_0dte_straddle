"""
Select the ITM call and its matching put for the pure straddle.

Strategy: pick the nearest strike where the CALL is ITM (strike < spot),
then use the same strike for the put (which will be OTM). This creates
a standard straddle with a slight long-delta bias.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import structlog

from data.option_chain import OptionChain, OptionInfo

log = structlog.get_logger(__name__)


@dataclass
class StraddlePair:
    call: OptionInfo
    put: OptionInfo
    strike: float


def _spread_pct(bid: float, ask: float, mark: float = 0.0) -> float:
    """
    Bid-ask spread as % of mid (or mark if bid is missing).

    On thin demo books, bid can be 0 with a valid ask. In that case
    we measure spread vs mark price so the gate still works.
    """
    if bid > 0 and ask > 0:
        mid = (bid + ask) / 2
        return (ask - bid) / mid * 100 if mid > 0 else 999
    if ask > 0 and mark > 0:
        return (ask - mark) / mark * 100
    return 999


def select_straddle_pair(chain: OptionChain, spot: float) -> Optional[StraddlePair]:
    """
    Find the nearest ITM call strike and its matching put.

    ITM call = strike < spot. We pick the closest strike below spot
    that has both a call and a put with a valid ask (we're buying,
    so ask > 0 is the relevant liquidity check; bid may be 0 on
    thin demo books).
    """
    log.info("chain_summary",
             total_calls=len(chain.calls),
             total_puts=len(chain.puts),
             spot=spot,
             call_strikes_below_spot=[c.strike for c in chain.calls
                                       if c.strike < spot][:10],
             call_strikes_above_spot=[c.strike for c in chain.calls
                                       if c.strike >= spot][:5])

    itm_calls = [c for c in chain.calls if c.strike < spot and c.ask > 0]
    if not itm_calls:
        all_itm = [c for c in chain.calls if c.strike < spot]
        log.warning("no_itm_calls",
                    spot=spot,
                    itm_strikes_present=[c.strike for c in all_itm],
                    itm_with_zero_ask=[
                        f"{c.strike}@bid={c.bid}/ask={c.ask}"
                        for c in all_itm if c.ask <= 0
                    ])
        return None

    best_call = max(itm_calls, key=lambda c: c.strike)

    matching_put = None
    for p in chain.puts:
        if p.strike == best_call.strike and p.ask > 0:
            matching_put = p
            break

    if matching_put is None:
        same_strike_puts = [p for p in chain.puts if p.strike == best_call.strike]
        log.warning("no_matching_put",
                    strike=best_call.strike,
                    spot=spot,
                    puts_at_strike=[
                        f"bid={p.bid}/ask={p.ask}" for p in same_strike_puts
                    ])
        return None

    spread_call = _spread_pct(best_call.bid, best_call.ask, best_call.mark)
    spread_put = _spread_pct(matching_put.bid, matching_put.ask,
                             matching_put.mark)

    log.info("straddle_pair_selected",
             strike=best_call.strike,
             call_bid=best_call.bid, call_ask=best_call.ask,
             call_mark=best_call.mark,
             call_spread=f"{spread_call:.1f}%",
             put_bid=matching_put.bid, put_ask=matching_put.ask,
             put_mark=matching_put.mark,
             put_spread=f"{spread_put:.1f}%",
             spot=spot)

    return StraddlePair(
        call=best_call,
        put=matching_put,
        strike=best_call.strike,
    )
