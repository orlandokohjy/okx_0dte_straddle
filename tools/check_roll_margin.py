"""
Read-only margin headroom check for the concurrent-maker-roll feature.

WHY
---
The concurrent maker roll closes the OLD straddle and opens the NEW one
at the SAME boundary, so for the brief overlap window the account holds
~2x the normal straddle exposure (the old legs are still resting on the
maker-sell while the new legs are already filled). This script verifies
the live OKX trading account has enough free balance to fund that
transient 2x hold BEFORE we deploy the feature.

Long options are PREMIUM-FUNDED: buying a call+put costs the premium and
carries no maintenance margin / liquidation risk (max loss = premium
paid). So the only question is cash: can the account pay for a SECOND
straddle's premium while the FIRST is still open? That reduces to:

    total_equity  >=  2 x (one_straddle_premium)  x  COLLATERAL_BUFFER_FACTOR

This script pulls the live numbers and prints a verdict. It is strictly
READ-ONLY: it never places, cancels or modifies any order.

USAGE
-----
    docker-compose exec algo python tools/check_roll_margin.py
    # or, on the host with the venv active:
    python tools/check_roll_margin.py

EXIT CODES
----------
    0  headroom OK for transient 2x hold (deploy is safe on margin)
    2  no OKX credentials in env
    3  could not fetch account balance / chain (network/auth)
    4  INSUFFICIENT headroom for 2x — do NOT deploy concurrent roll as-is
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from core import family
from core.exchange import OKXExchange
from data.option_chain import OptionChain
from strategy.option_selector import select_straddle_pair


def _largest_enabled_qty() -> tuple[str, float]:
    """Return (session_name, qty_per_leg) of the largest enabled fixed_btc
    session — the worst case for the 2x hold. Falls back to config default."""
    best_name, best_qty = "", 0.0
    for s in config.SESSIONS:
        if not s.enabled:
            continue
        if s.qty_per_leg > best_qty:
            best_name, best_qty = s.name, s.qty_per_leg
    if best_qty <= 0:
        return "default", float(config.QTY_PER_LEG)
    return best_name, best_qty


async def _run() -> int:
    if not config.HAS_OKX_CREDS:
        print("[check_roll_margin] No OKX credentials configured. Aborting.")
        return 2

    ex = OKXExchange()
    ex.connect()

    # ── 1) Raw account balance snapshot ──
    try:
        resp = await ex._call(ex._account.get_account_balance)
        rows = ex._data_or_empty(resp)
    except Exception as exc:
        print(f"[check_roll_margin] balance fetch failed: {exc}")
        return 3
    if not rows:
        print("[check_roll_margin] empty balance response.")
        return 3

    acct = rows[0]
    total_eq = ex._f(acct, "totalEq")
    avail_eq = ex._f(acct, "availEq")     # account-level free (cross) USD
    iso_eq = ex._f(acct, "isoEq")         # equity locked in isolated positions
    ord_froz = ex._f(acct, "ordFroz")     # margin frozen by pending orders
    mgn_ratio = ex._f(acct, "mgnRatio")

    # Per-ccy USDT detail (true spendable cash for isolated long-option buys).
    usdt_avail_bal = 0.0
    usdt_eq = 0.0
    for d in acct.get("details") or []:
        if d.get("ccy") == "USDT":
            usdt_avail_bal = ex._f(d, "availBal")
            usdt_eq = ex._f(d, "eq")
            break

    print("=" * 72)
    print(f"[check_roll_margin] family={config.OPTION_FAMILY}  "
          f"td_mode={config.OKX_TD_MODE}")
    print("-" * 72)
    print("ACCOUNT (USD-equivalent unless noted):")
    print(f"  totalEq             : ${total_eq:,.2f}")
    print(f"  availEq (free)      : ${avail_eq:,.2f}")
    print(f"  isoEq (in iso pos)  : ${iso_eq:,.2f}")
    print(f"  ordFroz (pending)   : ${ord_froz:,.2f}")
    if mgn_ratio:
        print(f"  mgnRatio            : {mgn_ratio:,.2f}")
    print(f"  USDT eq / availBal  : ${usdt_eq:,.2f} / ${usdt_avail_bal:,.2f}")

    # ── 2) Price one live straddle at the worst-case (largest) qty ──
    chain = OptionChain(ex)
    try:
        count = await chain.refresh()
        spot = await ex.get_spot_price()
    except Exception as exc:
        print(f"[check_roll_margin] chain/spot fetch failed: {exc}")
        return 3
    if count == 0 or spot <= 0:
        print(f"[check_roll_margin] no chain data (count={count}, spot={spot}).")
        return 3

    pair = select_straddle_pair(chain, spot)
    if pair is None:
        print(f"[check_roll_margin] no valid ITM pair near spot ${spot:,.0f}.")
        return 3

    sess_name, qty = _largest_enabled_qty()

    # Native premium per BTC of notional → USD, then × qty for one straddle.
    call_usd_per_btc = family.native_premium_to_usd(
        pair.call.ask, qty_btc=1.0, spot_usd=spot,
    )
    put_usd_per_btc = family.native_premium_to_usd(
        pair.put.ask, qty_btc=1.0, spot_usd=spot,
    )
    one_straddle_premium = (call_usd_per_btc + put_usd_per_btc) * qty
    two_straddle_premium = 2.0 * one_straddle_premium
    required_2x = two_straddle_premium * config.COLLATERAL_BUFFER_FACTOR

    print("-" * 72)
    print(f"WORST-CASE STRADDLE (largest enabled session: {sess_name}, "
          f"{qty:.4f} BTC/leg):")
    print(f"  spot                : ${spot:,.2f}")
    print(f"  strike              : ${pair.strike:,.0f}")
    print(f"  call ask / put ask  : "
          f"${call_usd_per_btc:,.2f} / ${put_usd_per_btc:,.2f}  per BTC")
    print(f"  1x straddle premium : ${one_straddle_premium:,.2f}")
    print(f"  2x straddle premium : ${two_straddle_premium:,.2f}  (transient hold)")
    print(f"  required (x{config.COLLATERAL_BUFFER_FACTOR:.2f} buffer): "
          f"${required_2x:,.2f}")

    # ── 3) Verdict ──
    # Free balance is the binding constraint for the second buy. We use
    # availEq when populated, else USDT availBal, else totalEq as a loose
    # upper bound (long options add their premium back as position value).
    free = avail_eq if avail_eq > 0 else (
        usdt_avail_bal if usdt_avail_bal > 0 else total_eq
    )
    free_basis = (
        "availEq" if avail_eq > 0
        else "USDT availBal" if usdt_avail_bal > 0
        else "totalEq (loose)"
    )
    headroom = free - required_2x
    pct_util = (required_2x / total_eq * 100.0) if total_eq > 0 else 999.0

    print("-" * 72)
    print(f"FREE BALANCE BASIS    : {free_basis} = ${free:,.2f}")
    print(f"HEADROOM vs 2x req    : ${headroom:,.2f}")
    print(f"2x req as % of totalEq: {pct_util:.1f}%")
    print("=" * 72)

    if headroom >= 0:
        print("VERDICT: OK — the account can fund the transient 2x hold "
              "with margin to spare. Concurrent maker roll is safe on "
              "margin grounds.")
        return 0
    print("VERDICT: INSUFFICIENT — free balance does not cover a 2x "
          "transient hold at the worst-case size. Do NOT deploy the "
          "concurrent roll until equity grows or per-leg qty is reduced.")
    return 4


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
