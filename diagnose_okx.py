"""
Standalone OKX credentials test. Bypasses the algo entirely.

Usage on VM:
    docker-compose run --rm algo python diagnose_okx.py
"""
import os
import sys
import traceback

from dotenv import load_dotenv
load_dotenv()

api_key = os.environ.get("OKX_API_KEY", "")
api_secret = os.environ.get("OKX_API_SECRET", "")
passphrase = os.environ.get("OKX_PASSPHRASE", "")
flag = os.environ.get("OKX_FLAG", "1")
domain = os.environ.get("OKX_DOMAIN", "https://www.okx.com")

print("=" * 60)
print("OKX CREDENTIALS DIAGNOSTIC")
print("=" * 60)
print(f"  Key prefix:        {api_key[:8]}...{api_key[-4:] if len(api_key) > 12 else ''}")
print(f"  Key length:        {len(api_key)}  (expected 36 for UUID format)")
print(f"  Secret length:     {len(api_secret)}  (expected 32)")
print(f"  Passphrase:        {passphrase}")
print(f"  Passphrase length: {len(passphrase)}")
print(f"  Flag:              {flag}  ({'DEMO' if flag == '1' else 'LIVE'})")
print(f"  Domain:            {domain}")
print()

if not all([api_key, api_secret, passphrase]):
    print("ERROR: missing one or more credentials in environment.")
    sys.exit(1)

print("─" * 60)
print("TEST 1 — public market data (no auth required)")
print("─" * 60)
try:
    from okx.MarketData import MarketAPI
    m = MarketAPI(flag=flag, domain=domain, debug=False)
    r = m.get_index_tickers(instId="BTC-USD")
    print(f"  code: {r.get('code')}, msg: {r.get('msg')!r}")
    if r.get("data"):
        print(f"  BTC-USD index: ${float(r['data'][0]['idxPx']):,.2f}")
    print("  → public endpoint OK\n")
except Exception:
    traceback.print_exc()
    print("  → public endpoint FAILED — VM may not have internet to OKX\n")
    sys.exit(2)

print("─" * 60)
print("TEST 2 — auth endpoint: account balance")
print("─" * 60)
try:
    from okx.Account import AccountAPI
    a = AccountAPI(api_key, api_secret, passphrase, False,
                   flag=flag, domain=domain, debug=False)
    r = a.get_account_balance()
    print(f"  code: {r.get('code')}")
    print(f"  msg:  {r.get('msg')!r}")
    if r.get("code") == "0":
        details = (r.get("data") or [{}])[0].get("details", [])
        print(f"  Got {len(details)} currency line(s):")
        for d in details[:5]:
            print(f"    {d.get('ccy'):<6} eq={d.get('eq')!r:<12} "
                  f"availBal={d.get('availBal')!r}")
        print("  → AUTH OK ✓\n")
    else:
        print()
        print("  ┌─ FAILURE TABLE ──────────────────────────────────────┐")
        print("  │ 50119: API key doesn't exist (key not in this env)   │")
        print("  │ 50105: Passphrase incorrect                           │")
        print("  │ 50113: Invalid signature (secret wrong)               │")
        print("  │ 50110: Invalid IP (whitelist mismatch)                │")
        print("  │ 50102: Timestamp expired (clock skew)                 │")
        print("  └──────────────────────────────────────────────────────┘\n")
except Exception:
    traceback.print_exc()

print("─" * 60)
print("TEST 3 — auth endpoint: list positions")
print("─" * 60)
try:
    from okx.Account import AccountAPI
    a = AccountAPI(api_key, api_secret, passphrase, False,
                   flag=flag, domain=domain, debug=False)
    r = a.get_positions(instType="OPTION")
    print(f"  code: {r.get('code')}, msg: {r.get('msg')!r}")
    if r.get("code") == "0":
        rows = r.get("data") or []
        print(f"  Got {len(rows)} position line(s)")
        for x in rows[:3]:
            print(f"    {x.get('instId'):<28} pos={x.get('pos')} "
                  f"upl={x.get('upl')}")
        print("  → AUTH OK ✓\n")
except Exception:
    traceback.print_exc()

print("─" * 60)
print("TEST 4 — same key against the OPPOSITE environment")
print("─" * 60)
opposite_flag = "0" if flag == "1" else "1"
opposite_label = "LIVE" if opposite_flag == "0" else "DEMO"
print(f"  Flipping flag to {opposite_flag} ({opposite_label})…")
try:
    from okx.Account import AccountAPI
    a = AccountAPI(api_key, api_secret, passphrase, False,
                   flag=opposite_flag, domain=domain, debug=False)
    r = a.get_account_balance()
    print(f"  code: {r.get('code')}, msg: {r.get('msg')!r}")
    print()
    if r.get("code") == "0":
        print(f"  ⚠️  AUTH WORKS UNDER {opposite_label} MODE!")
        print(f"  → Your key was created in {opposite_label} mode, "
              f"not {('DEMO' if flag == '1' else 'LIVE')} as intended.")
        print(f"  → Either flip OKX_FLAG to {opposite_flag}, or "
              f"create a fresh key in the right environment.")
    else:
        print(f"  Key fails in BOTH environments — likely deleted "
              "or never created. Recreate from scratch.")
except Exception:
    traceback.print_exc()

print("=" * 60)
print("Done.")
