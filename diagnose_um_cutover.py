"""
Read-only UM (linear) family pre-flight diagnostic.

Runs from the VPS BEFORE flipping OPTION_FAMILY=UM. Verifies — without
placing a single order — that the algo's UM unit assumptions agree with
what OKX returns over the wire:

    1.  UM family lists 0DTE BTC options at all (uly=BTC-USD_UM).
    2.  Tick size is 5 USD (matches family.default_tick() for UM).
    3.  Contract size assumption (0.01 BTC per contract) is consistent
        with the live minSz/lotSz returned for at least one ITM option.
    4.  UM premium quotes are in USD-per-BTC-of-notional, NOT BTC.
        (Cross-family probe: UM_ask / spot ≈ CM_ask within tolerance.)
    5.  Account margin currency is USDT/USDC, not BTC. UM auto-borrowing
        BTC for an option position would defeat the entire migration.

Output is a single PASS / FAIL verdict block at the end of the run.
ANY single failure means the cutover is unsafe — do NOT flip the env
var until each check passes.

Usage on the VPS::

    cd ~/okx_0dte_straddle
    docker-compose run --rm algo python diagnose_um_cutover.py

The script does NOT require OPTION_FAMILY=UM to be set in the env —
it queries both families directly so you can run it on the live CM
container without disturbing it.
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()


# ───────────────────────── Config ──────────────────────────────────

api_key = os.environ.get("OKX_API_KEY", "")
api_secret = os.environ.get("OKX_API_SECRET", "")
passphrase = os.environ.get("OKX_PASSPHRASE", "")
flag = os.environ.get("OKX_FLAG", "0")  # "0"=live, "1"=demo
domain = os.environ.get("OKX_DOMAIN", "https://www.okx.com")

# Same defaults the live algo will use under OPTION_FAMILY=UM.
ASSUMED_CONTRACT_SIZE_BTC_UM = float(
    os.environ.get("OKX_CONTRACT_SIZE_BTC_UM", "0.01"),
)
ASSUMED_TICK_USD_UM = float(os.environ.get("OPTION_TICK_SIZE", "5"))

# Cross-family check tolerance: UM_ask_usd / spot vs. CM_ask_btc. They
# won't match exactly because the two books are independent makers,
# but they should agree within ±15% on a same-strike same-expiry pair.
PRICE_AGREEMENT_TOLERANCE_PCT = 0.15

# Number of strikes to sample for cross-family verification.
SAMPLE_STRIKES = 5


# ───────────────────────── Pretty print ────────────────────────────

PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"
INFO = "[INFO]"


def hdr(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def kv(k: str, v) -> None:
    print(f"  {k:<28} {v}")


# ───────────────────────── Connection ──────────────────────────────

if not all([api_key, api_secret, passphrase]):
    print(f"{FAIL} Missing OKX credentials in environment.")
    sys.exit(2)

try:
    from okx.MarketData import MarketAPI
    from okx.PublicData import PublicAPI
    from okx.Account import AccountAPI
except ImportError:
    print(f"{FAIL} python-okx not installed. Run inside the algo container.")
    sys.exit(2)

market = MarketAPI(flag=flag, domain=domain, debug=False)
public = PublicAPI(flag=flag, domain=domain, debug=False)
account = AccountAPI(api_key, api_secret, passphrase, False,
                     flag=flag, domain=domain, debug=False)


# ───────────────────────── Result tracking ─────────────────────────

results: list[tuple[str, bool, str]] = []  # (check_name, passed, detail)


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    status = PASS if ok else FAIL
    print(f"{status} {name}" + (f" — {detail}" if detail else ""))


# ───────────────────────── Diagnostic body ─────────────────────────

print("=" * 70)
print("UM (LINEAR / USD-MARGINED) PRE-FLIGHT DIAGNOSTIC")
print("=" * 70)
kv("Run UTC", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
kv("OKX flag", f"{flag} ({'DEMO' if flag == '1' else 'LIVE'})")
kv("OKX domain", domain)
kv("Assumed UM contract size", f"{ASSUMED_CONTRACT_SIZE_BTC_UM} BTC")
kv("Assumed UM tick size", f"${ASSUMED_TICK_USD_UM}")
kv("Cross-family tolerance", f"±{PRICE_AGREEMENT_TOLERANCE_PCT:.0%}")


# ── 1. Spot price ─────────────────────────────────────────────────

hdr("CHECK 1 — BTC spot index reachable")
try:
    r = market.get_index_tickers(instId="BTC-USD")
    rows = r.get("data") or []
    if not rows:
        record("spot_index", False, f"empty data, code={r.get('code')}")
        sys.exit(3)
    spot = float(rows[0]["idxPx"])
    record("spot_index", True, f"BTC-USD = ${spot:,.2f}")
except Exception as e:
    record("spot_index", False, f"exception: {e!r}")
    sys.exit(3)


# ── 2. UM instrument family exists ────────────────────────────────

hdr("CHECK 2 — UM family lists 0DTE BTC options")
try:
    r = public.get_instruments(instType="OPTION", uly="BTC-USD_UM")
    um_rows = r.get("data") or []
    record("um_family_listed", len(um_rows) > 0,
           f"{len(um_rows)} instruments under uly=BTC-USD_UM")
    if not um_rows:
        sys.exit(3)
except Exception as e:
    record("um_family_listed", False, f"exception: {e!r}")
    sys.exit(3)


# Find today's 0DTE expiry — UM instId format: BTC-USD_UM-YYMMDD-STRIKE-{C|P}
def parse_um(inst_id: str):
    parts = inst_id.split("-")
    if len(parts) != 5 or parts[0] != "BTC" or parts[1] != "USD_UM":
        return None
    if parts[-1] not in ("C", "P"):
        return None
    try:
        strike = float(parts[3])
    except ValueError:
        return None
    return parts[2], strike, parts[-1]


# Today's expiry in UTC = next 08:00 UTC. We accept the soonest expiry
# returned by the API since the algo will pick that anyway.
expiries: dict[str, list[dict]] = {}
for r in um_rows:
    parsed = parse_um(r.get("instId", ""))
    if parsed is None:
        continue
    yymmdd, strike, opt = parsed
    expiries.setdefault(yymmdd, []).append(r)

if not expiries:
    record("um_0dte_present", False, "no parseable expiries in UM listing")
    sys.exit(3)

soonest = sorted(expiries.keys())[0]
um_today = expiries[soonest]
record("um_0dte_present", True,
       f"earliest expiry={soonest} with {len(um_today)} instruments")


# ── 3. Tick size and contract-size metadata ───────────────────────

hdr("CHECK 3 — UM tick size and contract metadata")
sample_um_ticks = []
sample_um_min_sz = []
sample_um_lot_sz = []
sample_um_ct_val = []
for r in um_today[:50]:  # bounded scan
    try:
        sample_um_ticks.append(float(r.get("tickSz") or 0))
        sample_um_min_sz.append(float(r.get("minSz") or 0))
        sample_um_lot_sz.append(float(r.get("lotSz") or 0))
        sample_um_ct_val.append(float(r.get("ctVal") or 0))
    except (TypeError, ValueError):
        continue

if not sample_um_ticks:
    record("um_metadata_readable", False, "no tickSz fields parseable")
    sys.exit(3)

# Take the most-common tick / minSz / lotSz / ctVal
from collections import Counter

mc_tick = Counter(sample_um_ticks).most_common(1)[0][0]
mc_min = Counter(sample_um_min_sz).most_common(1)[0][0]
mc_lot = Counter(sample_um_lot_sz).most_common(1)[0][0]
mc_ct = Counter(sample_um_ct_val).most_common(1)[0][0]

kv("Live UM tickSz (most common)", mc_tick)
kv("Live UM minSz (most common)", mc_min)
kv("Live UM lotSz (most common)", mc_lot)
kv("Live UM ctVal (most common)", mc_ct)

# Tick should be 5 USD across the family
tick_ok = abs(mc_tick - ASSUMED_TICK_USD_UM) < 0.01
record("um_tick_5_usd", tick_ok,
       f"live={mc_tick}, assumed={ASSUMED_TICK_USD_UM}")

# Contract size assumption: minSz=1 contract represents some BTC notional.
# Empirically OKX BTC options use 0.01 BTC per contract for both families.
# We can't read the BTC quantity directly from the API (ctVal=1 is a quote-
# currency field, not a BTC quantity), but if minSz=1 and lotSz=1 the
# inheritance from CM behavior is the strongest evidence we can get over
# the wire alone. Definitive verification requires either OKX UI or the
# very first live order. We FLAG this for the operator instead of
# silently passing.
contract_consistency_msg = (
    f"minSz={mc_min}, lotSz={mc_lot}, ctVal={mc_ct} → "
    f"ASSUMED 1 contract = {ASSUMED_CONTRACT_SIZE_BTC_UM} BTC. "
    f"This matches CM-family convention; first UM trade should "
    f"confirm via OKX UI position size."
)
# Pass if minSz and lotSz both equal 1 (the same shape as CM, which we
# know empirically uses 0.01 BTC/contract). Fail if they differ — that
# would mean UM has a different shape and we can't blindly inherit.
shape_matches_cm = (mc_min == 1.0 and mc_lot == 1.0)
record("um_contract_shape_matches_cm", shape_matches_cm,
       contract_consistency_msg)

# Belt and suspenders: if you sent 50 contracts under our assumption,
# what's the implied notional? Print it so the operator sees the
# expected position size before Monday.
sample_qty_btc = 50 * ASSUMED_CONTRACT_SIZE_BTC_UM
sample_qty_usd = sample_qty_btc * spot
kv("If sent 50 contracts (assumed)",
   f"{sample_qty_btc:.4f} BTC ≈ ${sample_qty_usd:,.0f}")
kv("If sent 25 contracts (assumed)",
   f"{0.25:.4f} BTC ≈ ${0.25 * spot:,.0f}")


# ── 4. Cross-family premium-unit probe ────────────────────────────

hdr("CHECK 4 — UM premiums quoted in USD (not BTC)")
# Pick the SAMPLE_STRIKES strikes closest to spot. For each, fetch the
# UM put + CM put and verify UM_ask_usd / spot ≈ CM_ask_btc.

um_strikes = sorted({float(r["instId"].split("-")[3]) for r in um_today})
um_strikes_near_spot = sorted(
    um_strikes, key=lambda s: abs(s - spot),
)[:SAMPLE_STRIKES]

# Pull CM listing for the same expiry
cm_resp = public.get_instruments(instType="OPTION", uly="BTC-USD")
cm_rows = cm_resp.get("data") or []
cm_strikes_for_expiry = {
    float(r["instId"].split("-")[3]): r
    for r in cm_rows
    if r.get("instId", "").startswith(f"BTC-USD-{soonest}-")
    and r["instId"].endswith("-P")  # puts only
}
um_strikes_for_expiry = {
    float(r["instId"].split("-")[3]): r
    for r in um_today
    if r["instId"].endswith("-P")
}

agreements = []
print(f"\n  Checking {len(um_strikes_near_spot)} strike(s) closest to "
      f"${spot:,.0f}:\n")
print(f"  {'Strike':>10}  {'UM ask ($)':>12}  {'CM ask (BTC)':>14}  "
      f"{'UM/spot':>10}  {'agree?':>10}")
for k in um_strikes_near_spot:
    um_inst = um_strikes_for_expiry.get(k)
    cm_inst = cm_strikes_for_expiry.get(k)
    if not um_inst or not cm_inst:
        continue
    try:
        um_ticker = market.get_ticker(instId=um_inst["instId"])
        cm_ticker = market.get_ticker(instId=cm_inst["instId"])
    except Exception:
        continue
    um_data = (um_ticker.get("data") or [{}])[0]
    cm_data = (cm_ticker.get("data") or [{}])[0]
    um_ask = float(um_data.get("askPx") or 0)
    cm_ask = float(cm_data.get("askPx") or 0)
    if um_ask <= 0 or cm_ask <= 0:
        continue
    um_implied_btc_per_btc = um_ask / spot
    rel_err = (
        abs(um_implied_btc_per_btc - cm_ask) / cm_ask
        if cm_ask > 0 else 1.0
    )
    agree = rel_err <= PRICE_AGREEMENT_TOLERANCE_PCT
    agreements.append(agree)
    print(f"  {k:>10,.0f}  {um_ask:>12,.0f}  {cm_ask:>14.4f}  "
          f"{um_implied_btc_per_btc:>10.4f}  "
          f"{('YES' if agree else 'NO'):>10}")

# Sanity bounds on UM ask: an ITM 0DTE option premium in USD-per-BTC
# notional should land between $50 and $50,000 on a $80k-spot day.
# A premium between 0.001 and 0.5 (BTC range) would catch a unit
# regression where the wire is actually BTC-quoted.
if agreements:
    pass_rate = sum(agreements) / len(agreements)
    cross_ok = pass_rate >= 0.6  # ≥ 60% agreement across samples
    record("um_premium_unit_is_usd", cross_ok,
           f"{sum(agreements)}/{len(agreements)} strike(s) agreed within "
           f"±{PRICE_AGREEMENT_TOLERANCE_PCT:.0%} of CM × spot")
else:
    record("um_premium_unit_is_usd", False,
           "No strike pairs returned valid quotes (low liquidity or "
           "expiry rolled?). Re-run during active hours.")


# ── 5. Account currency check ─────────────────────────────────────

hdr("CHECK 5 — Account margin in USDT/USDC, not BTC")
try:
    r = account.get_account_balance()
    rows = r.get("data") or []
    if not rows:
        record("account_currency", False, f"no data, code={r.get('code')}")
    else:
        details = rows[0].get("details") or []
        ccy_eq = []
        for d in details:
            ccy = d.get("ccy", "")
            try:
                eq_usd = float(d.get("eqUsd") or d.get("eq") or 0)
            except (TypeError, ValueError):
                eq_usd = 0
            if eq_usd > 1:  # ignore dust
                ccy_eq.append((ccy, eq_usd))
        ccy_eq.sort(key=lambda x: -x[1])
        for ccy, eq in ccy_eq:
            kv(f"Currency {ccy}", f"${eq:,.2f}")
        # Pass if largest balance is USDT/USDC/USD; warn if BTC-dominated
        if ccy_eq and ccy_eq[0][0] in ("USDT", "USDC", "USD"):
            record("account_currency", True,
                   f"main balance in {ccy_eq[0][0]}")
        elif not ccy_eq:
            record("account_currency", False, "no balances above $1")
        else:
            record("account_currency", False,
                   f"main balance is {ccy_eq[0][0]} (UM is USD-margined; "
                   f"first trade may auto-borrow USDT)")
except Exception as e:
    record("account_currency", False, f"exception: {e!r}")
    traceback.print_exc()


# ── 6. Self-test: simulate a chase price round on a UM ITM put ────

hdr("CHECK 6 — Simulated UM chase price stays in USD range")
um_itm_put = None
for k in um_strikes_near_spot:
    if k > spot:  # ITM put: strike > spot
        inst = um_strikes_for_expiry.get(k)
        if inst:
            um_itm_put = inst
            break
if um_itm_put is None and um_strikes_near_spot:
    # Fallback: any UM put with valid quotes
    for k in um_strikes_near_spot:
        inst = um_strikes_for_expiry.get(k)
        if inst:
            um_itm_put = inst
            break

if um_itm_put is None:
    record("um_chase_simulation", False, "no UM ITM put available")
else:
    try:
        ticker = market.get_ticker(instId=um_itm_put["instId"])
        mark_resp = public.get_mark_price(
            instType="OPTION", instId=um_itm_put["instId"],
        )
    except Exception as e:
        record("um_chase_simulation", False, f"exception: {e!r}")
    else:
        td = (ticker.get("data") or [{}])[0]
        md = (mark_resp.get("data") or [{}])[0]
        bid = float(td.get("bidPx") or 0)
        ask = float(td.get("askPx") or 0)
        mark = float(md.get("markPx") or 0)
        kv("Sample instrument", um_itm_put["instId"])
        kv("Bid / Ask / Mark", f"${bid:,.0f} / ${ask:,.0f} / ${mark:,.0f}")
        kv("Tick", f"${mc_tick}")
        # Chase math: 50% gap-narrow from bid to ask
        if ask > 0 and bid >= 0:
            target_top = max(bid, ask - mc_tick)
            new_price = bid + (target_top - bid) * 0.5
            new_price = round(new_price / mc_tick) * mc_tick
            # Sanity range: USD-priced 0DTE ITM premium should fall in
            # [50, 50000]. BTC-priced would fall in [0.0001, 0.5].
            in_usd_range = 50 <= new_price <= 50_000
            in_btc_range = 0.0001 <= new_price <= 0.5
            record("um_chase_simulation",
                   in_usd_range and not in_btc_range,
                   f"chase price = ${new_price:,.0f} "
                   f"(USD range: 50≤x≤50000)")
        else:
            record("um_chase_simulation", False,
                   "no valid bid/ask on sample")


# ───────────────────────── Verdict ─────────────────────────────────

hdr("VERDICT")
total = len(results)
passed = sum(1 for _, ok, _ in results if ok)
print()
for name, ok, detail in results:
    tag = PASS if ok else FAIL
    line = f"  {tag} {name}"
    if detail:
        line += f" — {detail}"
    print(line)
print()
if passed == total:
    print("=" * 70)
    print(f"  ALL CHECKS PASSED ({passed}/{total})")
    print("  UM cutover is SAFE to proceed.")
    print("=" * 70)
    sys.exit(0)
else:
    print("=" * 70)
    print(f"  FAILED CHECKS: {total - passed}/{total}")
    print("  UM cutover is NOT SAFE. Investigate failures above before "
          "flipping OPTION_FAMILY=UM.")
    print("=" * 70)
    sys.exit(1)
