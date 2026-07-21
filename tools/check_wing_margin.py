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


def _fmt_margin_fields(obj: dict) -> list[str]:
    """Pull any margin-ish fields (imr/mmr/mr/eq) out of a position-builder
    row, USD-equivalent. Keys vary by SDK version, so match loosely."""
    out = []
    for k, v in obj.items():
        kl = k.lower()
        if any(t in kl for t in ("imr", "mmr", "margin", "mr", "eq")) \
                and not isinstance(v, (list, dict)):
            try:
                out.append(f"    {k:<16}: {float(v):,.2f}")
            except (TypeError, ValueError):
                out.append(f"    {k:<16}: {v}")
    return out


async def _sim_pm(ex, leg_args: list) -> int:
    """Simulate a CROSS portfolio under Portfolio Margin and print its margin.

    Each leg arg is ``instId:signedContracts`` (+long / -short). This is a
    pure hypothetical (inclRealPosAndEq=False) so it shows the PM margin of
    the intended iron fly independent of current messy state.
    """
    import json

    # position_builder requires an entry price per leg (avgPx). Optionally
    # given as INST:SIGNED_CONTRACTS:PX; otherwise auto-fetched from mark.
    sim_pos = []
    for a in leg_args:
        parts = a.split(":")
        if len(parts) < 2:
            print(f"[sim] bad leg (want INST:SIGNED_CONTRACTS[:PX]): {a}")
            return 3
        inst = parts[0]
        pos = parts[1]
        px = parts[2] if len(parts) > 2 else ""
        if not px:
            try:
                mk = await ex.get_option_mark_price(inst)
                if not mk:
                    t = await ex.get_ticker(inst)
                    bid, ask = float(t.bid or 0.0), float(t.ask or 0.0)
                    mk = (bid + ask) / 2 if (bid and ask) else (bid or ask)
                px = str(mk)
            except Exception as exc:
                print(f"[sim] could not fetch avgPx for {inst}: {exc}")
                return 3
        sim_pos.append({
            "instId": inst,
            "pos": str(int(float(pos))),
            "avgPx": str(px),
        })

    print("=" * 72)
    print("PM POSITION-BUILDER SIMULATION (cross, hypothetical)")
    print("-" * 72)
    for p in sim_pos:
        side = "LONG " if float(p["pos"]) > 0 else "SHORT"
        print(f"  {side} {p['instId']}  {p['pos']} contracts  "
              f"@ avgPx={p['avgPx']}")
    print("-" * 72)

    try:
        resp = await ex._call(
            ex._account.position_builder,
            inclRealPosAndEq=False,
            simPos=sim_pos,
        )
    except Exception as exc:
        print(f"[sim] position_builder call failed: {exc}")
        return 3

    if str(resp.get("code")) != "0":
        print(f"[sim] OKX error code={resp.get('code')} "
              f"msg={resp.get('msg')}")
        # still dump any data
    rows = ex._data_or_empty(resp)
    if not rows:
        print("[sim] empty response — raw below:")
        print(json.dumps(resp, indent=2)[:2000])
        return 0

    top = rows[0]
    print("PORTFOLIO MARGIN (USD-equivalent):")
    for line in _fmt_margin_fields(top):
        print(line)
    # per-risk-unit detail if present
    for key in ("riskUnitData", "marginBalance", "assetsData"):
        val = top.get(key)
        if isinstance(val, list) and val:
            print(f"  {key}:")
            for ru in val[:6]:
                ident = ru.get("riskUnit") or ru.get("ccy") or "?"
                print(f"    • {ident}")
                for line in _fmt_margin_fields(ru):
                    print("    " + line.strip())

    # Compare to free equity so the verdict is obvious.
    try:
        bal = ex._data_or_empty(
            await ex._call(ex._account.get_account_balance))
        avail_eq = ex._f(bal[0], "availEq") if bal else 0.0
        print("-" * 72)
        print(f"  availEq (free, USD): ${avail_eq:,.2f}")
        print("  If total IMR above << availEq, the covered fly fits under PM.")
    except Exception:
        pass
    print("=" * 72)
    print("Full raw response (for exact fields):")
    print(json.dumps(rows, indent=2)[:3000])
    return 0


async def _run() -> int:
    if not config.HAS_OKX_CREDS:
        print("[check_wing_margin] No OKX credentials configured. Aborting.")
        return 2

    ex = OKXExchange()
    ex.connect()

    # ── PM position-builder simulation ────────────────────────────────────
    # For PM accounts OKX blocks the simple max-size endpoint (59202) because
    # margin is computed holistically. The position-builder simulates a whole
    # CROSS portfolio and returns its real PM margin, so we can prove the
    # covered iron fly (long body offsets short wings) fits BEFORE flipping
    # OKX_TD_MODE=cross. Usage:
    #   check_wing_margin.py --sim INST:SIGNED_CONTRACTS [INST:SIGNED ...]
    #   e.g. --sim BTC-...-64000-C:100 BTC-...-64000-P:100 \
    #             BTC-...-65400-C:-100 BTC-...-63500-P:-100
    # (+contracts = long, -contracts = short; 100 contracts = 1.0 BTC.)
    if len(sys.argv) > 1 and sys.argv[1] == "--sim":
        return await _sim_pm(ex, sys.argv[2:])

    inst = sys.argv[1] if len(sys.argv) > 1 else ""
    qty_btc = float(sys.argv[2]) if len(sys.argv) > 2 else float(
        config.QTY_PER_LEG)

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
