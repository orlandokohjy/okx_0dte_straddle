"""
Select the ATM strike (call + put) for the straddle.

Strategy: pick the listed strike CLOSEST to spot (|strike − spot| minimised,
either side of spot) and use it for both the call and the put. This creates
a balanced ATM straddle. (The legacy selector always rounded down to an ITM
call, giving a long-delta bias — changed 2026-07-23.)
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
    Find the strike CLOSEST to spot (true ATM) and its call+put.

    We pick the listed strike with the smallest |strike − spot| that has
    BOTH a call and a put with a valid ask (we're buying, so ask > 0 is the
    relevant liquidity check; bid may be 0 on thin demo books). The nearest
    strike can be ABOVE spot (slightly-OTM call / ITM put) or below — unlike
    the legacy selector which always rounded DOWN to an ITM call. Same strike
    is used for both legs → a balanced ATM straddle (minimal delta bias).
    """
    log.info("chain_summary",
             total_calls=len(chain.calls),
             total_puts=len(chain.puts),
             spot=spot,
             call_strikes_below_spot=[c.strike for c in chain.calls
                                       if c.strike < spot][:10],
             call_strikes_above_spot=[c.strike for c in chain.calls
                                       if c.strike >= spot][:5])

    # Strikes that have a tradable (ask > 0) call AND put. First occurrence
    # per strike wins (chains list one contract per strike).
    calls_by_strike: dict[float, OptionInfo] = {}
    for c in chain.calls:
        if c.ask > 0 and c.strike not in calls_by_strike:
            calls_by_strike[c.strike] = c
    puts_by_strike: dict[float, OptionInfo] = {}
    for p in chain.puts:
        if p.ask > 0 and p.strike not in puts_by_strike:
            puts_by_strike[p.strike] = p

    common = sorted(set(calls_by_strike) & set(puts_by_strike))
    if not common:
        log.warning("no_tradable_common_strike",
                    spot=spot,
                    call_strikes=sorted(calls_by_strike)[:10],
                    put_strikes=sorted(puts_by_strike)[:10])
        return None

    # Nearest strike to spot. Ties (spot exactly at a midpoint) break to the
    # LOWER strike via the stable sort + <= comparison in min().
    strike = min(common, key=lambda s: (abs(s - spot), s))
    best_call = calls_by_strike[strike]
    matching_put = puts_by_strike[strike]

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
