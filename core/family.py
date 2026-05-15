"""
Option family abstraction — CM (inverse, BTC-margined) vs UM (linear, USD-margined).

OKX lists the BTC option family in two flavours under the same underlying:

    BTC-USD-{YYMMDD}-{STRIKE}-{C|P}        — INVERSE / coin-margined
        - settleCcy = BTC, premium quoted in BTC per BTC of notional
        - tickSz    = 0.0001 BTC
        - 730 instruments listed (deeper / more strikes)
        - Margin auto-borrows BTC against your USDT collateral, creating
          BTC currency drift on equity during open positions.

    BTC-USD_UM-{YYMMDD}-{STRIKE}-{C|P}     — LINEAR / USD-margined
        - settleCcy = USD, premium quoted in USD per BTC of notional
        - tickSz    = 5 USD
        - 470 instruments listed (slightly fewer wing strikes)
        - All P&L denominated in USDT/USD — no BTC currency drift.

This module is the SINGLE SOURCE OF TRUTH for which family is active and
provides converters so the rest of the codebase can stay in BTC-equivalent
units regardless of family.

BTC-EQUIVALENT INTERNAL CONVENTION
----------------------------------
Throughout the codebase, premiums on the ``Straddle`` / ``StraddleLeg``
objects are stored in BTC-equivalent units (= BTC per BTC of notional).
For CM this is just the native quote. For UM we divide native USD by
spot-at-fill to get the same dimensionless ratio.

The existing P&L formulas — `entry_price * entry_spot` — then yield
correct USD numbers for both families:

    CM:  (btc_per_btc * USD_per_BTC)  → USD per BTC notional ✓
    UM:  (usd_per_btc / spot * spot)  → USD per BTC notional ✓ (spot cancels)

OKX-NATIVE PRICES AT THE WIRE
-----------------------------
Order placement and fill responses use OKX-native prices (BTC for CM,
USD for UM). The chase loop must round to the family-specific tick and
return the native fill price; the strategy layer converts to BTC-eq for
storage with ``to_btc_equivalent``.
"""
from __future__ import annotations

import os


# ──────────────────── Family selection ────────────────────────────
#
# Set OPTION_FAMILY=CM (default) or OPTION_FAMILY=UM in .env. Aliases
# are tolerated so legacy ops runbooks keep working:
#
#     CM, INVERSE, COIN, BTC-USD            → CM
#     UM, LINEAR, USD,  BTC-USD_UM, USDT    → UM

_RAW = os.getenv("OPTION_FAMILY", "CM").strip().upper()


def _resolve_family(raw: str) -> str:
    cm_aliases = {"CM", "INVERSE", "COIN", "COIN-MARGINED", "BTC-USD"}
    um_aliases = {
        "UM", "LINEAR", "USD", "USD-MARGINED",
        "BTC-USD_UM", "USDT", "USDC",
    }
    if raw in cm_aliases:
        return "CM"
    if raw in um_aliases:
        return "UM"
    # Fallback: anything else gets CM with a startup warning logged
    # by the caller (we don't import logging here to keep this module
    # import-cycle-free).
    return "CM"


FAMILY: str = _resolve_family(_RAW)
RAW: str = _RAW   # exported for diagnostics ("USDC" → CM warning, etc.)


def is_cm() -> bool:
    return FAMILY == "CM"


def is_um() -> bool:
    return FAMILY == "UM"


def label() -> str:
    """Short uppercase tag used in trade-log column + reports ("CM"/"UM")."""
    return FAMILY


def display_name() -> str:
    """Human-friendly family name for logs / Telegram banners."""
    return "BTC-USD inverse (coin-margined)" if is_cm() \
        else "BTC-USD_UM linear (USD-margined)"


# ──────────────────── Symbol / parsing ────────────────────────────
#
# OKX uly + instId conventions for the two families:
#
#     CM uly = "BTC-USD"      instId = "BTC-USD-{YYMMDD}-{STRIKE}-{C|P}"
#     UM uly = "BTC-USD_UM"   instId = "BTC-USD_UM-{YYMMDD}-{STRIKE}-{C|P}"
#
# Both split into 5 dash-separated tokens because the underscore in
# "USD_UM" is preserved (it's not a delimiter). The chain parser uses
# ``quote_token()`` to filter rows to only this family's instruments.

def underlying() -> str:
    """The OKX ``uly`` field (also used as instId prefix root)."""
    return "BTC-USD" if is_cm() else "BTC-USD_UM"


def quote_token() -> str:
    """The 2nd dash-separated token of an instId (used for filtering)."""
    return "USD" if is_cm() else "USD_UM"


def instid_prefix() -> str:
    """Prefix every instrument id starts with ('BTC-USD-' or 'BTC-USD_UM-')."""
    return f"{underlying()}-"


# ──────────────────── Native units (tick / fee / fills) ───────────
#
# OKX-side native quote unit. Used for tick rounding and the
# safety-bound check that catches unit-conversion regressions.

def native_quote_unit_label() -> str:
    """For log / telegram strings (``BTC`` for CM, ``USD`` for UM)."""
    return "BTC" if is_cm() else "USD"


def default_tick() -> float:
    """Family-specific tick fallback when /api/v5/public/instruments is
    unreachable. Authoritative tick comes from OKX on connect.

    CM: 0.0001 BTC across all strikes/expiries (verified 2026-05).
    UM: 5 USD across all strikes/expiries (verified 2026-05 from the
        diagnostic ``compare_first`` probe on the live account).
    """
    return float(os.getenv("OPTION_TICK_SIZE", "0.0001" if is_cm() else "5"))


def tick_implausible_threshold() -> float:
    """Highest plausible tick for the active family. ``prime_option_tick_size``
    refuses to override its default if OKX returns something larger than
    this — guards against the 2026-05-07 unit-confusion regression.

    CM: > 0.01 BTC is impossible (real tick is 0.0001).
    UM: > 100 USD is impossible (real tick is 5).
    """
    return 0.01 if is_cm() else 100.0


def contract_size_btc() -> float:
    """BTC of underlying notional represented by ONE OKX contract.

    BOTH families use 0.01 BTC per contract on the user's account
    (verified empirically: 50 contracts = 0.5 BTC notional in the OKX
    UI on 2026-05-15). The OKX API's ``ctVal`` field returns 1.0 for
    both families which is *not* the BTC quantity — see the long note
    in ``core.exchange.prime_option_tick_size`` for the saga.

    Override via ``OKX_CONTRACT_SIZE_BTC`` (CM) or
    ``OKX_CONTRACT_SIZE_BTC_UM`` (UM) in .env if your account behaves
    differently. Startup logs the live ``minSz`` so an operator can
    spot-check before the first trade.
    """
    if is_cm():
        return float(os.getenv("OKX_CONTRACT_SIZE_BTC", "0.01"))
    return float(os.getenv("OKX_CONTRACT_SIZE_BTC_UM", "0.01"))


# ──────────────────── Unit converters ─────────────────────────────
#
# These all operate on premium *prices* (per BTC of notional). Use
# them at the boundary between OKX-native and BTC-equivalent layers.

def to_btc_equivalent(native_price: float, spot_usd: float) -> float:
    """Convert an OKX-native fill price to the BTC-equivalent price
    that the rest of the codebase expects on the Straddle objects.

    CM: native is already BTC-per-BTC, return as-is.
    UM: native is USD-per-BTC, divide by spot to get a dimensionless
        BTC-per-BTC ratio. Requires a positive spot or returns 0 to
        signal a missing-context degrade (caller must have just done
        the fill, so the spot at fill should always be available).
    """
    if is_cm():
        return native_price
    if spot_usd <= 0:
        return 0.0
    return native_price / spot_usd


def to_native_price(btc_eq_price: float, spot_usd: float) -> float:
    """Inverse of ``to_btc_equivalent`` — used by the chase initial
    bid/ask conversion when the option chain stored BTC-equivalent
    bid/ask but we need to send native to OKX.

    Currently unused (the chain stores native and we convert at fill
    time only) but provided for completeness / future refactor.
    """
    if is_cm():
        return btc_eq_price
    return btc_eq_price * spot_usd


def native_premium_to_usd(
    native_price: float, qty_btc: float, spot_usd: float,
) -> float:
    """USD value of a native premium price for a given BTC notional.

    CM:  native_btc_per_btc * qty_btc * spot_usd
    UM:  native_usd_per_btc * qty_btc           (spot cancels out)
    """
    if is_cm():
        return native_price * qty_btc * spot_usd
    return native_price * qty_btc


def fee_to_usd(fee_native: float, spot_usd: float) -> float:
    """Convert a fee from OKX-native units to USD.

    CM fees are charged in BTC; UM fees in USD. Always returns the
    absolute USD value regardless of OKX's sign convention (OKX
    reports negative ``fee`` when the trader paid).
    """
    fee_abs = abs(fee_native)
    if is_cm():
        return fee_abs * spot_usd if spot_usd > 0 else 0.0
    return fee_abs


def native_decimals() -> int:
    """Decimal places to use when rounding/formatting native prices.

    CM: 4 decimals (e.g. 0.0035 BTC) — tick is 0.0001.
    UM: 0 decimals (e.g. 285) — tick is 5 USD.
    """
    return 4 if is_cm() else 0


def format_native_price(native_price: float) -> str:
    """Human-readable native-price string for telegram / logs."""
    if is_cm():
        return f"{native_price:.4f} BTC"
    return f"${native_price:,.0f}"
