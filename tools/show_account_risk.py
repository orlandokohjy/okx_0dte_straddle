"""
Read-only snapshot of LIVE account-level margin risk on OKX.

Unlike show_positions.py (which lists derivative positions), this dumps the
account margin ratio and per-currency balances/liabilities. Use it when you
get a liquidation-risk alert but show_positions reports FLAT — the driver is
then almost always a BORROWED balance (auto-borrow liability), which still
carries liquidation risk under cross / Portfolio Margin.

Strictly read-only: never trades, never touches the PID lock. Safe while
the algo is live.

USAGE
-----
    docker-compose run --rm --entrypoint python algo tools/show_account_risk.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from core.exchange import OKXExchange


def _f(d: dict, k: str) -> float:
    try:
        v = d.get(k, "")
        return float(v) if v not in ("", None) else 0.0
    except (TypeError, ValueError):
        return 0.0


async def _run() -> int:
    if not config.HAS_OKX_CREDS:
        print("[show_account_risk] No OKX credentials configured. Aborting.")
        return 2

    ex = OKXExchange()
    ex.connect()
    try:
        resp = await ex._call(ex._account.get_account_balance)
        rows = ex._data_or_empty(resp)
    except Exception as exc:
        print(f"[show_account_risk] balance fetch failed: {exc}")
        return 3

    if not rows:
        print("[show_account_risk] empty balance response.")
        return 3

    acct = rows[0]
    total_eq = _f(acct, "totalEq")
    adj_eq = _f(acct, "adjEq")
    mgn_ratio = _f(acct, "mgnRatio")
    imr = _f(acct, "imr")
    mmr = _f(acct, "mmr")
    borrow_froz = _f(acct, "borrowFroz")
    notional = _f(acct, "notionalUsd")

    print("═══════════ ACCOUNT MARGIN RISK ═══════════")
    print(f"  totalEq   : ${total_eq:,.2f}")
    print(f"  adjEq     : ${adj_eq:,.2f}")
    print(f"  mgnRatio  : {mgn_ratio:g}   (OKX cross: liquidation when this "
          f"ratio ≤ ~1.0 / margin level ≤ 100%)")
    print(f"  IMR       : ${imr:,.2f}   (initial margin req)")
    print(f"  MMR       : ${mmr:,.2f}   (maintenance margin req)")
    print(f"  borrowFroz: ${borrow_froz:,.2f}")
    print(f"  notional  : ${notional:,.2f}")

    details = acct.get("details") or []
    print(f"\n── Per-currency balances ({len(details)} ccy) ──")
    flagged = []
    for d in details:
        ccy = d.get("ccy", "?")
        eq = _f(d, "eq")
        cash = _f(d, "cashBal")
        avail = _f(d, "availEq") or _f(d, "availBal")
        liab = _f(d, "liab")
        borrow = _f(d, "borrowFroz")
        eq_usd = _f(d, "eqUsd")
        # Skip dust rows with no balance and no liability.
        if abs(eq) < 1e-9 and abs(liab) < 1e-9 and abs(cash) < 1e-9:
            continue
        marker = ""
        if liab < 0 or borrow > 0 or cash < 0 or eq < 0:
            marker = "   ⚠️ LIABILITY / BORROW"
            flagged.append((ccy, liab, cash, borrow))
        print(f"  {ccy:6s} eq={eq:+.8f} (${eq_usd:,.2f})  cashBal={cash:+.8f}"
              f"  avail={avail:+.8f}  liab={liab:+.8f}  borrowFroz={borrow:+.8f}"
              f"{marker}")

    print("\n── VERDICT ──")
    if flagged:
        print("  A BORROWED / NEGATIVE balance is present — this is what")
        print("  carries the liquidation risk even with no open positions.")
        for ccy, liab, cash, borrow in flagged:
            print(f"    • {ccy}: liab={liab:+.8f} cashBal={cash:+.8f} "
                  f"borrowFroz={borrow:+.8f}")
        print("  FIX: repay the loan — on OKX go to the subaccount → Assets →")
        print("  repay the borrowed currency (or convert/transfer in enough of")
        print("  that ccy), or disable auto-borrow. This restores the margin")
        print("  level immediately.")
    else:
        print("  No borrowed balance and no positions detected on THIS")
        print("  subaccount. The alert is likely for a DIFFERENT subaccount —")
        print("  check which OKX UID the email's 98****77 maps to and run this")
        print("  tool against that stack.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
