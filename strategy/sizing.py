"""
Per-entry position sizing for the 0DTE straddle algo.

Two modes are supported per session, configured via ``Session.sizing_mode``:

    fixed_btc   → ``qty_per_leg`` is a hard BTC quantity. Same size every
                  entry. Same behaviour as the historical algo.
    pct_equity  → premium-as-percent-of-equity. The qty_per_leg is
                  computed at entry time so that the straddle's expected
                  USD premium ≈ ``pct_equity × current_equity``.

Why "premium-as-pct-of-equity" is the natural sizing for long straddles
=======================================================================
For a long-only straddle the maximum theoretical loss is the entire
premium paid (both legs expire worthless). Sizing by "% of equity per
entry" therefore equals "% of equity at risk per entry" — the textbook
risk-budget formulation. This module deliberately does NOT support
notional-as-pct (which would size by underlying exposure) or
margin-as-pct (which is meaningless for long options on OKX since
OKX charges no margin on long options past the premium).

Live equity source
==================
We read ``portfolio.equity`` (a USD value the algo syncs from OKX's
account balance after every session close, plus on startup). This is
typically <10 minutes stale. Going to a live OKX balance call here
would add a network hop to the entry-hot-path; the staleness is well
within the dispersion of any single session's P&L so the trade-off
favours speed.

Premium estimator
=================
We estimate the straddle's per-BTC USD premium using **ASK** prices
for both legs (with mark as a fallback if ask is zero on a thin demo
book). Using ASK — not MID — is deliberate:

  • The maker chase walks UP from initial bid towards ask. Empirically
    most fills land at or near ASK, not at MID. Using MID would
    systematically undersize the budget by ~½ × spread, which on a
    20%-spread demo book means the operator's "50% pct_equity" silently
    becomes "55% pct_equity" of effective risk.
  • ``position_sizer.size_position`` (the next stage) also computes
    costs from ASK. Matching here means the qty resolved by sizing
    flows cleanly into size_position without phase-shifting the
    capital-fit math.
  • Worst case (tight book) we undersize by < 1% — operator gets
    slightly less risk than configured, never more. That asymmetry
    is the correct direction.

The estimator converts native premium → USD via
``family.native_premium_to_usd`` so CM (BTC-quoted) and UM (USD-quoted)
share the same downstream code path.

Safety guards (in priority order)
================================
1.  If ``sizing_mode == "fixed_btc"``, return ``session.qty_per_leg``
    unchanged. No equity / premium logic runs.
2.  If equity ≤ 0, fall back to ``session.qty_per_leg`` and log a warning.
    Pct-of-zero is undefined; the operator probably wants the legacy
    fixed size while equity sync is broken.
3.  If the per-BTC premium estimate is ≤ 0 (chain not refreshed yet,
    bid/ask/mark all zero), fall back to ``session.qty_per_leg``.
4.  Round DOWN to OKX contract granularity (0.01 BTC). Rounding up
    would push us above the budget; rounding down keeps premium ≤ target.
5.  Enforce a hard cap of ``config.MAX_QTY_PER_LEG_BTC`` (default 5.0
    BTC). Catches a runaway equity-tracking bug. When the cap binds
    we log ``sizing_capped_to_max`` at INFO so the operator can bump it.
6.  If the rounded qty is below ``config.MIN_QTY_PER_LEG_BTC`` (default
    0.01 BTC = 1 contract), return 0.0 to signal "skip this session".
    The caller (main._on_entry) treats 0.0 as a clean skip.

The function also returns a decision-audit dict for the
``sizing_decision`` log event so the operator can reconstruct the
sizing call after the fact.
"""
from __future__ import annotations

import math

import structlog

import config
from core import family
from strategy.option_selector import StraddlePair

log = structlog.get_logger(__name__)

# Float-precision tolerance for contract-count flooring. Without this,
# 0.29 / 0.01 = 28.999999999999996 in IEEE-754 double, and int() / floor()
# return 28 instead of 29, silently undersizing by 1 contract. 1e-9 is
# wide enough to absorb any FP artefact at the scale of BTC×contract
# (max ~5 BTC / 0.01 = 500.0), and tight enough that a legitimately-just-
# under value (e.g. 28.5 contracts) still floors to the lower count.
_CONTRACT_FLOOR_EPS = 1e-9


def _round_down_to_contract(qty_btc: float) -> float:
    """Round qty DOWN to the nearest contract (``OKX_CONTRACT_SIZE_BTC``).

    Rounding DOWN ensures we never exceed the operator's pct_equity
    budget. The remainder (< 1 contract) is dropped — at 0.01 BTC and
    $80k spot that is < $800 of underlying notional per leg.

    Uses an FP-tolerance epsilon to avoid the off-by-one caused by
    floats like 0.29/0.01 evaluating to 28.999999996 → floor()=28.
    """
    contract = config.OKX_CONTRACT_SIZE_BTC
    if contract <= 0:
        return qty_btc
    contracts = math.floor(qty_btc / contract + _CONTRACT_FLOOR_EPS)
    contracts = max(0, contracts)
    return round(contracts * contract, 8)


def _estimate_per_btc_premium_usd(
    pair: StraddlePair, spot_usd: float,
) -> float:
    """Estimate USD premium per 1 BTC of notional for both legs combined.

    Uses ASK prices (mark as fallback), matching what ``position_sizer``
    and the entry-chase actually pay on average. See module docstring
    "Premium estimator" for why ASK and not MID.

    Returns 0.0 if no usable price is available for either leg.
    """
    def _leg_native(leg) -> float:
        if leg.ask > 0:
            return leg.ask
        return leg.mark or 0.0

    call_native = _leg_native(pair.call)
    put_native = _leg_native(pair.put)
    if call_native <= 0 or put_native <= 0:
        return 0.0

    call_usd = family.native_premium_to_usd(call_native, 1.0, spot_usd)
    put_usd = family.native_premium_to_usd(put_native, 1.0, spot_usd)
    return call_usd + put_usd


def compute_qty_per_leg(
    session: config.Session,
    *,
    equity_usd: float,
    pair: StraddlePair,
    spot_usd: float,
) -> tuple[float, dict]:
    """Resolve the BTC qty/leg for THIS entry under the session's mode.

    Returns ``(qty_btc, audit_dict)``. ``qty_btc == 0.0`` means skip the
    session (insufficient equity for at least 1 contract). The audit
    dict is consumed by the ``sizing_decision`` log event in main.py.
    """
    audit: dict = {
        "session": session.name,
        "sizing_mode": session.sizing_mode,
        "pct_equity_config": session.pct_equity,
        "fallback_qty_btc": session.qty_per_leg,
        "equity_usd": round(equity_usd, 2),
        "spot_usd": round(spot_usd, 2),
        "min_qty_btc": config.MIN_QTY_PER_LEG_BTC,
        "max_qty_btc": config.MAX_QTY_PER_LEG_BTC,
        "contract_size_btc": config.OKX_CONTRACT_SIZE_BTC,
    }

    # Fast path: fixed_btc mode short-circuits all the dynamic logic.
    if session.sizing_mode != "pct_equity":
        qty = _round_down_to_contract(session.qty_per_leg)
        qty = min(qty, config.MAX_QTY_PER_LEG_BTC)
        audit.update({
            "decision": "fixed_btc",
            "final_qty_btc": qty,
        })
        return qty, audit

    # ── pct_equity mode ──
    if equity_usd <= 0:
        fallback = _round_down_to_contract(session.qty_per_leg)
        fallback = min(fallback, config.MAX_QTY_PER_LEG_BTC)
        audit.update({
            "decision": "fallback_zero_equity",
            "final_qty_btc": fallback,
            "warning": "equity ≤ 0; falling back to session.qty_per_leg",
        })
        log.warning("sizing_zero_equity_fallback", **audit)
        return fallback, audit

    per_btc_premium_usd = _estimate_per_btc_premium_usd(pair, spot_usd)
    audit["est_premium_per_btc_usd"] = round(per_btc_premium_usd, 2)

    if per_btc_premium_usd <= 0:
        fallback = _round_down_to_contract(session.qty_per_leg)
        fallback = min(fallback, config.MAX_QTY_PER_LEG_BTC)
        audit.update({
            "decision": "fallback_no_premium_estimate",
            "final_qty_btc": fallback,
            "warning": "premium estimate ≤ 0; falling back to session.qty_per_leg",
        })
        log.warning("sizing_no_premium_fallback", **audit)
        return fallback, audit

    target_premium_usd = equity_usd * session.pct_equity
    raw_qty = target_premium_usd / per_btc_premium_usd
    audit["target_premium_usd"] = round(target_premium_usd, 2)
    audit["raw_qty_btc"] = round(raw_qty, 6)

    rounded_raw = _round_down_to_contract(raw_qty)
    qty = min(rounded_raw, config.MAX_QTY_PER_LEG_BTC)
    if qty < rounded_raw:
        # Cap binding silently truncates the operator's pct_equity
        # target. Log it so the operator notices and can bump the cap.
        audit["capped_to_max"] = True
        log.info(
            "sizing_capped_to_max",
            session=session.name,
            raw_qty_btc=rounded_raw,
            capped_qty_btc=qty,
            max_qty_btc=config.MAX_QTY_PER_LEG_BTC,
            target_premium_usd=audit["target_premium_usd"],
            note=(
                "Final qty capped by MAX_QTY_PER_LEG_BTC. Bump the env "
                "var to honour the configured pct_equity at higher equity."
            ),
        )

    if qty < config.MIN_QTY_PER_LEG_BTC:
        audit.update({
            "decision": "skip_below_min_qty",
            "final_qty_btc": 0.0,
            # Avoid '<' in user-visible text — the message ends up in
            # Telegram HTML mode where bare '<' followed by a letter is
            # interpreted as an open tag and breaks the render.
            "skip_reason": (
                f"target_premium=${target_premium_usd:,.2f}, "
                f"per_btc=${per_btc_premium_usd:,.2f}, "
                f"raw_qty={raw_qty:.4f} BTC → rounded to {qty} BTC "
                f"(below MIN_QTY_PER_LEG_BTC={config.MIN_QTY_PER_LEG_BTC} BTC)"
            ),
        })
        return 0.0, audit

    audit.update({
        "decision": "pct_equity",
        "final_qty_btc": qty,
    })
    return qty, audit


def telegram_summary_line(audit: dict, qty: float, num_straddles: int) -> str:
    """One-line "how was this sized?" string for the entry Telegram banner.

    Two flavours depending on what audit['decision'] says:

        fixed_btc:   "Sized fixed_btc: 0.50 BTC × 1 straddle"
        pct_equity:  "Sized pct_equity (25% × $7,761 = $1,940 target → 0.74 BTC × 1)"
        fallback*:   "Sized fallback (… reason …) → 0.50 BTC × 1"
        skip:        "Sized: SKIPPED (… reason …)"
    """
    decision = audit.get("decision", "?")
    qty_str = f"{qty:.4f} BTC × {num_straddles}"

    if decision == "fixed_btc":
        return f"Sized fixed_btc: {qty_str}"

    if decision == "pct_equity":
        return (
            f"Sized pct_equity ("
            f"{audit['pct_equity_config']:.0%} × "
            f"${audit['equity_usd']:,.0f} = "
            f"${audit['target_premium_usd']:,.0f} target → "
            f"{qty_str})"
        )

    if decision.startswith("fallback"):
        return (
            f"Sized fallback ({audit.get('warning', decision)}) → "
            f"{qty_str}"
        )

    if decision == "skip_below_min_qty":
        return (
            f"Sized: SKIPPED — "
            f"{audit.get('skip_reason', 'qty below minimum')}"
        )

    return f"Sized {decision}: {qty_str}"
