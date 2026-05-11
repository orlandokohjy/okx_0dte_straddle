"""
Compound position sizing for a pure option straddle on OKX BTC-USD options.

OKX BTC-USD options are coin-margined inverse contracts:
  • Premium px is quoted in BTC (per BTC of underlying notional)
  • To compare to a USD equity figure, we must multiply by spot:
        usd_premium_per_btc_notional = btc_premium × spot
        usd_cost_per_straddle = qty_per_leg × (usd_call + usd_put)

Sizing math (all in USD):
    straddle_cost_usd = call_cost_per_usd + put_cost_per_usd
    num_straddles = floor(ALLOC_PCT × equity_usd / buffered_straddle_cost_usd)

`qty_per_leg` is supplied by the caller from the Session that fired the
entry (see config.SESSIONS) — afternoon may use 0.5 BTC while morning
uses 0.25 BTC, etc.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import structlog

import config

log = structlog.get_logger(__name__)

SLIPPAGE_BUFFER: float = 0.05


@dataclass
class SizingResult:
    num_straddles: int
    call_cost_per: float            # USD per straddle
    put_cost_per: float             # USD per straddle
    straddle_cost: float            # USD per straddle
    total_call_cost: float          # USD for all straddles
    total_put_cost: float           # USD for all straddles
    total_capital_required: float   # USD with buffer
    equity: float                   # USD
    available_capital: float        # USD


def size_position(
    equity: float,
    call_premium_btc: float,
    put_premium_btc: float,
    spot_usd: float,
    qty_per_leg: float,
) -> SizingResult:
    """
    Compute sizing in USD given BTC-quoted premiums and spot price.

    Args:
        equity: Trading-account equity in USD (USDT/USDC).
        call_premium_btc: Call ask in BTC (per BTC of notional).
        put_premium_btc: Put ask in BTC (per BTC of notional).
        spot_usd: BTC spot in USD, used to translate BTC premiums → USD.
        qty_per_leg: BTC notional per leg for the firing session.

    Returns:
        SizingResult with all fields denominated in USD.
    """
    if spot_usd <= 0:
        log.warning("size_position_invalid_spot",
                    spot=spot_usd, action="returning zero straddles")
        return SizingResult(
            num_straddles=0, call_cost_per=0, put_cost_per=0,
            straddle_cost=0, total_call_cost=0, total_put_cost=0,
            total_capital_required=0, equity=equity,
            available_capital=config.ALLOC_PCT * equity,
        )

    # USD premium per BTC of notional.
    call_premium_usd = call_premium_btc * spot_usd
    put_premium_usd = put_premium_btc * spot_usd

    call_cost_per = qty_per_leg * call_premium_usd
    put_cost_per = qty_per_leg * put_premium_usd
    straddle_cost = call_cost_per + put_cost_per

    if straddle_cost <= 0:
        return SizingResult(
            num_straddles=0, call_cost_per=0, put_cost_per=0,
            straddle_cost=0, total_call_cost=0, total_put_cost=0,
            total_capital_required=0, equity=equity,
            available_capital=config.ALLOC_PCT * equity,
        )

    available = config.ALLOC_PCT * equity
    buffered_cost = straddle_cost * (1 + SLIPPAGE_BUFFER)
    n = math.floor(available / buffered_cost)
    n = max(0, n)

    total_call = call_cost_per * n
    total_put = put_cost_per * n
    total_required = (total_call + total_put) * (1 + SLIPPAGE_BUFFER)

    result = SizingResult(
        num_straddles=n,
        call_cost_per=call_cost_per,
        put_cost_per=put_cost_per,
        straddle_cost=straddle_cost,
        total_call_cost=total_call,
        total_put_cost=total_put,
        total_capital_required=total_required,
        equity=equity,
        available_capital=available,
    )

    log.info(
        "position_sized",
        equity=f"${equity:,.0f}",
        spot=f"${spot_usd:,.0f}",
        available=f"${available:,.0f}",
        num_straddles=n,
        qty_per_leg=qty_per_leg,
        call_premium_btc=call_premium_btc,
        put_premium_btc=put_premium_btc,
        call_cost_per=f"${call_cost_per:,.2f}",
        put_cost_per=f"${put_cost_per:,.2f}",
        straddle_cost=f"${straddle_cost:,.2f}",
        total_required=f"${total_required:,.2f}",
        buffer=f"{SLIPPAGE_BUFFER:.0%}",
    )
    return result
