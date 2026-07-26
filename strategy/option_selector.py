"""
Select the LONG OTM STRANGLE (call + put at different strikes) for the body.

Strategy (this stack = reverse / long iron condor, wings OFF by default):
buy the next OTM call (nearest listed strike strictly ABOVE spot with a
tradable ask) and the next OTM put (nearest listed strike strictly BELOW
spot with a tradable ask). Two different strikes straddling spot → net
debit, long-vol. The optional short wings (``select_wings``) sit one strike
FURTHER out beyond each long leg and are disabled unless ENABLE_WINGS +
the session wing-window are both on.
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


@dataclass
class WingLeg:
    """A single short wing option (call above body, or put below body)."""
    option: OptionInfo
    strike: float


@dataclass
class WingPair:
    """Short wings around a straddle body. Either side may be None when no
    valid adjacent strike with a live bid exists — the caller sells only
    what is present (the body always covers whatever fills)."""
    call: Optional[WingLeg]  # short call, WING_CALL_STRIKE_OFFSET strikes ABOVE body
    put: Optional[WingLeg]   # short put,  WING_PUT_STRIKE_OFFSET strikes BELOW body

    @property
    def any(self) -> bool:
        return self.call is not None or self.put is not None


def select_wings(
    chain: OptionChain,
    *,
    call_body_strike: float,
    put_body_strike: float,
    call_offset: int,
    put_offset: int,
) -> WingPair:
    """Pick the short wings by walking ADJACENT LISTED strikes from EACH long
    leg of the strangle body.

    The strangle body has two strikes, so wings are measured PER LEG:
      - the short CALL wing is ``call_offset`` listed strikes ABOVE the long
        call strike (``call_body_strike``),
      - the short PUT wing is ``put_offset`` listed strikes BELOW the long
        put strike (``put_body_strike``).

    We are SELLING these, so a valid **bid** (bid > 0) is the relevant
    liquidity check — a maker sell rests at/above the bid. A wing side with
    no adjacent strike or no live bid is returned as None and simply not
    sold; the long leg still stands, so the structure is never left naked.

    ``call_offset`` / ``put_offset`` are counts of adjacent listed strikes
    away from the respective long leg (1 = the very next strike further out).
    """
    call_strikes = sorted({c.strike for c in chain.calls})
    put_strikes = sorted({p.strike for p in chain.puts})

    call_wing: Optional[WingLeg] = None
    above = [s for s in call_strikes if s > call_body_strike]
    if call_offset >= 1 and len(above) >= call_offset:
        target = above[call_offset - 1]
        cand = next(
            (c for c in chain.calls if c.strike == target and c.bid > 0), None,
        )
        if cand is not None:
            call_wing = WingLeg(option=cand, strike=target)
        else:
            log.warning("call_wing_unavailable", target_strike=target,
                        reason="no_live_bid_at_strike")
    else:
        log.warning("call_wing_no_strike", body=call_body_strike,
                    strikes_above=len(above), offset=call_offset)

    put_wing: Optional[WingLeg] = None
    below_desc = sorted((s for s in put_strikes if s < put_body_strike),
                        reverse=True)
    if put_offset >= 1 and len(below_desc) >= put_offset:
        target = below_desc[put_offset - 1]
        cand = next(
            (p for p in chain.puts if p.strike == target and p.bid > 0), None,
        )
        if cand is not None:
            put_wing = WingLeg(option=cand, strike=target)
        else:
            log.warning("put_wing_unavailable", target_strike=target,
                        reason="no_live_bid_at_strike")
    else:
        log.warning("put_wing_no_strike", body=put_body_strike,
                    strikes_below=len(below_desc), offset=put_offset)

    log.info("wings_selected",
             call_body_strike=call_body_strike,
             put_body_strike=put_body_strike,
             call_wing_strike=call_wing.strike if call_wing else None,
             call_wing_bid=call_wing.option.bid if call_wing else None,
             put_wing_strike=put_wing.strike if put_wing else None,
             put_wing_bid=put_wing.option.bid if put_wing else None)
    return WingPair(call=call_wing, put=put_wing)


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
    sites (logging / sizing); the authoritative per-leg strikes are
    ``pair.call.strike`` and ``pair.put.strike``.
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
