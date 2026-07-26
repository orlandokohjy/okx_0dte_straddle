"""
Select the LONG OTM STRANGLE (call + put at different strikes) for the body.

Strategy (this ETH stack = long OTM strangle): buy the next OTM call (nearest
listed strike strictly ABOVE spot with a tradable ask) and the next OTM put
(nearest listed strike strictly BELOW spot with a tradable ask). Two different
strikes straddling spot → net debit, long-vol. (No short-wing overlay on the
ETH stack — the sold "further strike" wings from the BTC stack are not ported
here; enable them on request.)
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
    Find the LONG OTM strangle: next OTM call + next OTM put.

    - Call leg: the nearest listed strike strictly ABOVE spot that has a
      tradable ask (we're buying). This is the first OTM call.
    - Put leg:  the nearest listed strike strictly BELOW spot that has a
      tradable ask. This is the first OTM put.

    The two legs have DIFFERENT strikes (a strangle straddling spot). Returns
    None if either side has no tradable OTM strike. ``StraddlePair.strike``
    carries the CALL strike for compatibility with legacy single-strike call
    sites; authoritative per-leg strikes are ``pair.call.strike`` /
    ``pair.put.strike``.
    """
    log.info("chain_summary",
             total_calls=len(chain.calls),
             total_puts=len(chain.puts),
             spot=spot,
             call_strikes_above_spot=sorted(c.strike for c in chain.calls
                                            if c.strike > spot)[:5],
             put_strikes_below_spot=sorted((p.strike for p in chain.puts
                                            if p.strike < spot),
                                           reverse=True)[:5])

    # Strikes that have a tradable (ask > 0) contract. First occurrence per
    # strike wins (chains list one contract per strike).
    calls_by_strike: dict[float, OptionInfo] = {}
    for c in chain.calls:
        if c.ask > 0 and c.strike not in calls_by_strike:
            calls_by_strike[c.strike] = c
    puts_by_strike: dict[float, OptionInfo] = {}
    for p in chain.puts:
        if p.ask > 0 and p.strike not in puts_by_strike:
            puts_by_strike[p.strike] = p

    calls_above = sorted(s for s in calls_by_strike if s > spot)
    puts_below = sorted((s for s in puts_by_strike if s < spot), reverse=True)
    if not calls_above or not puts_below:
        log.warning("no_otm_strangle",
                    spot=spot,
                    calls_above=calls_above[:5],
                    puts_below=puts_below[:5])
        return None

    call_strike = calls_above[0]   # first OTM call (nearest above spot)
    put_strike = puts_below[0]     # first OTM put  (nearest below spot)
    best_call = calls_by_strike[call_strike]
    matching_put = puts_by_strike[put_strike]

    spread_call = _spread_pct(best_call.bid, best_call.ask, best_call.mark)
    spread_put = _spread_pct(matching_put.bid, matching_put.ask,
                             matching_put.mark)

    log.info("otm_strangle_selected",
             call_strike=best_call.strike,
             put_strike=matching_put.strike,
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
