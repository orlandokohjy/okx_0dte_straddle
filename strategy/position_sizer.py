"""
Compound position sizing for a pure option straddle on OKX BTC options.

Two families are supported (see ``core.family``):

  CM (BTC-USD inverse, coin-margined):
    • Premium px is quoted in BTC per BTC of underlying notional
    • USD conversion: usd_premium_per_btc_notional = btc_premium × spot

  UM (BTC-USD_UM linear, USD-margined):
    • Premium px is quoted in USD per BTC of underlying notional
    • USD conversion: usd_premium_per_btc_notional = native_price (identity)

Sizing math (all in USD):
    straddle_cost_usd = call_cost_per_usd + put_cost_per_usd
    num_straddles = floor(ALLOC_PCT × equity_usd / buffered_straddle_cost_usd)

`qty_per_leg` is supplied by the caller. Under the post-2026-05-20
schedule the caller is ``main._run_entry``, which resolves the qty
via ``strategy.sizing.compute_qty_per_leg`` from the firing Session's
``sizing_mode`` (fixed_btc or pct_equity) and current portfolio equity.
Under fixed_btc this matches the historical behaviour
(``afternoon=0.5 BTC``, ``morning=0.25 BTC``); under pct_equity the
qty is computed at entry time so premium ≈ pct_equity × equity.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import structlog

import config
from core import family

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
    call_premium_native: float,
    put_premium_native: float,
    spot_usd: float,
    qty_per_leg: float,
) -> SizingResult:
    """
    Compute sizing in USD given OKX-native ask premiums and spot price.

    Args:
        equity: Trading-account equity in USD (USDT/USDC).
        call_premium_native: Call ask in OKX-native units —
            BTC per BTC of notional for CM, USD per BTC of notional for UM.
        put_premium_native: Put ask, same convention.
        spot_usd: BTC spot in USD. Required for CM (BTC → USD); ignored
            on UM (already USD), but still used as a degenerate-input
            guard.
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

    # USD premium per BTC of notional. Family-aware: identity on UM.
    call_premium_usd = family.native_premium_to_usd(
        call_premium_native, qty_btc=1.0, spot_usd=spot_usd,
    )
    put_premium_usd = family.native_premium_to_usd(
        put_premium_native, qty_btc=1.0, spot_usd=spot_usd,
    )

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
        family=family.label(),
        equity=f"${equity:,.0f}",
        spot=f"${spot_usd:,.0f}",
        available=f"${available:,.0f}",
        num_straddles=n,
        qty_per_leg=qty_per_leg,
        call_premium_native=call_premium_native,
        put_premium_native=put_premium_native,
        native_unit=family.native_quote_unit_label(),
        call_cost_per=f"${call_cost_per:,.2f}",
        put_cost_per=f"${put_cost_per:,.2f}",
        straddle_cost=f"${straddle_cost:,.2f}",
        total_required=f"${total_required:,.2f}",
        buffer=f"{SLIPPAGE_BUFFER:.0%}",
    )
    return result
