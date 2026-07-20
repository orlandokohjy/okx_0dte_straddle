"""
Read-only diagnostic for a SHORT-WING sell that hit ``51008`` (insufficient
balance) on entry.

WHY
---
``check_roll_margin.py`` only sizes the LONG body straddle (premium-funded, no
margin). It says nothing about the SHORT wing, whose sell-to-open reserves
INITIAL MARGIN. On OKX, BTC options are coin-margined, so a short's margin is
posted in BTC — an account that is USDT-heavy (with BTC borrowed) can show
plenty of USD-equivalent free equity yet still fail a wing sell with 51008.

This tool asks OKX directly, for the exact wing instrument, HOW MUCH it will
let you sell right now under both ``isolated`` and ``cross`` margin, plus the
account mode + autoborrow setting that decides whether your USDT can back a
BTC-margined short. It is STRICTLY READ-ONLY — it never places or cancels
anything.

USAGE
-----
    docker-compose exec algo python tools/check_wing_margin.py BTC-USD-260721-64500-P 1.0
    #                                                          <instId>            <qty_btc>

    # qty_btc defaults to the plain-BTC per-leg size if omitted.

EXIT CODES
----------
    0  query ran (see the per-mode availSell verdict)
    2  no OKX credentials in env
    3  could not fetch account/instrument data (network/auth)
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from core.exchange import OKXExchange

_ACCT_LV = {
    "1": "Simple",
    "2": "Single-currency margin",
    "3": "Multi-currency margin",
    "4": "Portfolio margin",
}


async def _run() -> int:
    if not config.HAS_OKX_CREDS:
        print("[check_wing_margin] No OKX credentials configured. Aborting.")
        return 2

    inst = sys.argv[1] if len(sys.argv) > 1 else ""
    qty_btc = float(sys.argv[2]) if len(sys.argv) > 2 else float(
        config.QTY_PER_LEG)

    ex = OKXExchange()
    ex.connect()

    # ── 1) Account MODE + autoborrow (settles "can I even sell in this mode") ─
    try:
        cfg = ex._data_or_empty(
            await ex._call(ex._account.get_account_config))
    except Exception as exc:
        print(f"[check_wing_margin] account-config fetch failed: {exc}")
        return 3
    acct_lv = auto_loan = pos_mode = "?"
    if cfg:
        c = cfg[0]
        acct_lv = c.get("acctLv", "?")
        auto_loan = str(c.get("autoLoan", "?"))
        pos_mode = c.get("posMode", "?")

    print("=" * 72)
    print("ACCOUNT MODE")
    print("-" * 72)
    print(f"  acctLv   : {acct_lv}  ({_ACCT_LV.get(acct_lv, 'unknown')})")
    print(f"  autoLoan : {auto_loan}   (autoborrow — USDT can back a "
          f"BTC-margined short only when TRUE)")
    print(f"  posMode  : {pos_mode}")

    # ── 2) Balance snapshot: USD-equivalent AND per-coin (BTC is the one that
    #       actually margins a coin-margined short) ──
    try:
        rows = ex._data_or_empty(
            await ex._call(ex._account.get_account_balance))
    except Exception as exc:
        print(f"[check_wing_margin] balance fetch failed: {exc}")
        return 3
    if rows:
        acct = rows[0]
        print("-" * 72)
        print("BALANCE")
        print(f"  totalEq (USD)   : ${ex._f(acct, 'totalEq'):,.2f}")
        print(f"  availEq (USD)   : ${ex._f(acct, 'availEq'):,.2f}")
        for d in acct.get("details") or []:
            if d.get("ccy") in ("BTC", "USDT", "ETH"):
                print(f"  {d.get('ccy'):<4} eq/avail  : "
                      f"{ex._f(d, 'eq'):,.6f} / avail "
                      f"{ex._f(d, 'availBal'):,.6f}  "
                      f"(availEq ${ex._f(d, 'availEq'):,.2f})")

    if not inst:
        print("-" * 72)
        print("No instrument given — pass the wing instId to see sellable size,")
        print("e.g.  python tools/check_wing_margin.py BTC-USD-260721-64500-P 1.0")
        print("=" * 72)
        return 0

    # ── 3) Contract size + current book (for px-based max-order query) ──
    ct_val = 0.01
    try:
        meta = await ex.get_instrument_meta(inst)
        ct_val = float(meta.get("ctVal", 0.01) or 0.01) * float(
            meta.get("ctMult", 1) or 1)
    except Exception:
        pass
    want_contracts = qty_btc / ct_val if ct_val else 0.0

    bid = ask = 0.0
    try:
        t = await ex.get_ticker(inst)
        bid, ask = float(t.bid or 0.0), float(t.ask or 0.0)
    except Exception:
        pass

    print("-" * 72)
    print(f"WING: {inst}")
    print(f"  want to SELL     : {qty_btc:.4f} BTC = {want_contracts:.0f} "
          f"contracts (ctVal={ct_val})")
    print(f"  live bid / ask   : {bid} / {ask}")
    print("-" * 72)
    print("MAX SELLABLE NOW (per OKX, this account, this instrument):")

    px = str(bid or ask or "")
    for td in ("isolated", "cross"):
        avail_sell = max_sell = "?"
        try:
            a = ex._data_or_empty(await ex._call(
                ex._account.get_max_avail_size, instId=inst, tdMode=td))
            if a:
                avail_sell = a[0].get("availSell", "?")
        except Exception as exc:
            avail_sell = f"err: {exc}"
        try:
            kwargs = {"instId": inst, "tdMode": td}
            if px:
                kwargs["px"] = px
            m = ex._data_or_empty(
                await ex._call(ex._account.get_max_order_size, **kwargs))
            if m:
                max_sell = m[0].get("maxSell", "?")
        except Exception as exc:
            max_sell = f"err: {exc}"

        def _btc(v):
            try:
                return f"  (= {float(v) * ct_val:.4f} BTC)"
            except Exception:
                return ""

        verdict = ""
        try:
            if float(avail_sell) >= want_contracts > 0:
                verdict = "  -> ENOUGH to sell the wing"
            else:
                verdict = "  -> NOT ENOUGH for the wing"
        except Exception:
            pass
        print(f"  tdMode={td:<9} availSell={avail_sell}{_btc(avail_sell)}  "
              f"maxSell={max_sell}{verdict}")

    print("=" * 72)
    print("READING IT:")
    print("  • If acctLv=3/4 (multi-ccy / PM) AND autoLoan=true, a CROSS sell")
    print("    should show availSell >= want — the USDT backs the BTC short.")
    print("  • If autoLoan=false, or acctLv=2 (single-ccy) with no free BTC,")
    print("    the short can't source BTC margin -> availSell ~0 -> 51008.")
    print("  • Compare isolated vs cross: if cross is much larger, the fix is")
    print("    tdMode=cross (+ autoborrow), NOT more cash.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
