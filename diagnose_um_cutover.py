"""
Read-only UM (linear) family pre-flight diagnostic.

Runs from the VPS BEFORE flipping OPTION_FAMILY=UM. Verifies — without
placing a single order — that the algo's UM unit assumptions agree with
what OKX returns over the wire:

    1.  UM family lists 0DTE BTC options at all
        (instType=OPTION + instFamily=BTC-USD_UM).
    2.  Tick size is 5 USD (matches family.default_tick() for UM).
    3.  Contract size assumption (0.01 BTC per contract) is consistent
        with the live ctVal × ctMult returned for at least one ITM option.
    4.  UM premium quotes are in USD-per-BTC-of-notional, NOT BTC.
        (Cross-family probe: UM_mark ≈ CM_mark × spot within tolerance.)
    5.  Account margin currency is USDT/USDC, not BTC. UM auto-borrowing
        BTC for an option position would defeat the entire migration.
    6.  Simulated chase price stays inside the USD range (50..50,000)
        and OUT of the BTC range (0.0001..0.5) — catches a unit-confusion
        regression in the chase ladder.

CRITICAL — OKX shares ``uly=BTC-USD`` between CM and UM. The actual
discriminator is the ``instFamily`` field on /api/v5/public/instruments:

    CM: instFamily="BTC-USD"
    UM: instFamily="BTC-USD_UM"

Querying ``uly=BTC-USD_UM`` returns ``code=51014 "Index doesn't exist."``
(this script v1 hit that bug 2026-05-18). All UM-specific listing
queries below pass ``instFamily=BTC-USD_UM`` for that reason.

Output is a single PASS / FAIL verdict block at the end of the run.
ANY single failure means the cutover is unsafe — do NOT flip the env
var until each check passes.

Usage on the VPS::

    cd ~/okx_0dte_straddle
    docker-compose run --rm --entrypoint python algo diagnose_um_cutover.py

(Note the ``--entrypoint python`` — without it, compose appends the
script as args to the existing ``python main.py`` ENTRYPOINT and runs
the live algo by accident.)

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
# Both CM and UM share uly=BTC-USD on OKX. Pass instFamily=BTC-USD_UM
# to get only the UM (linear, USD-settled) rows. Querying
# uly=BTC-USD_UM returns code=51014 "Index doesn't exist."
try:
    r = public.get_instruments(
        instType="OPTION", uly="BTC-USD", instFamily="BTC-USD_UM",
    )
    um_rows = r.get("data") or []
    record("um_family_listed", len(um_rows) > 0,
           f"{len(um_rows)} instruments under instFamily=BTC-USD_UM")
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

hdr("CHECK 3 — UM tick size and contract metadata (ctVal × ctMult)")
sample_um_ticks = []
sample_um_min_sz = []
sample_um_lot_sz = []
sample_um_ct_val = []
sample_um_ct_mult = []
sample_um_effective = []
for r in um_today[:50]:  # bounded scan
    try:
        tick = float(r.get("tickSz") or 0)
        ctv = float(r.get("ctVal") or 0)
        ctm = float(r.get("ctMult") or 0)
        sample_um_ticks.append(tick)
        sample_um_min_sz.append(float(r.get("minSz") or 0))
        sample_um_lot_sz.append(float(r.get("lotSz") or 0))
        sample_um_ct_val.append(ctv)
        sample_um_ct_mult.append(ctm)
        if ctv > 0 and ctm > 0:
            sample_um_effective.append(ctv * ctm)
    except (TypeError, ValueError):
        continue

if not sample_um_ticks:
    record("um_metadata_readable", False, "no tickSz fields parseable")
    sys.exit(3)

# Take the most-common tick / minSz / lotSz / ctVal / ctMult
from collections import Counter

mc_tick = Counter(sample_um_ticks).most_common(1)[0][0]
mc_min = Counter(sample_um_min_sz).most_common(1)[0][0]
mc_lot = Counter(sample_um_lot_sz).most_common(1)[0][0]
mc_ct_val = Counter(sample_um_ct_val).most_common(1)[0][0]
mc_ct_mult = Counter(sample_um_ct_mult).most_common(1)[0][0]
mc_effective = (
    Counter(sample_um_effective).most_common(1)[0][0]
    if sample_um_effective else 0
)

kv("Live UM tickSz (most common)", f"${mc_tick}")
kv("Live UM minSz / lotSz", f"{mc_min} / {mc_lot}")
kv("Live UM ctVal (most common)", f"{mc_ct_val} {sample_um_effective and 'BTC' or ''}")
kv("Live UM ctMult (most common)", mc_ct_mult)
kv("Effective size = ctVal × ctMult",
   f"{mc_effective} BTC per contract")

# Tick should be 5 USD across the family
tick_ok = abs(mc_tick - ASSUMED_TICK_USD_UM) < 0.01
record("um_tick_5_usd", tick_ok,
       f"live={mc_tick}, assumed={ASSUMED_TICK_USD_UM}")

# Contract-size verification: ctVal × ctMult is the EMPIRICAL BTC size
# per contract. Both CM and UM return ctVal=1, ctMult=0.01 ⇒ 0.01 BTC.
# (Verified live 2026-05-15 across 1,200 instruments: 730 CM + 470 UM.)
contract_size_ok = abs(
    mc_effective - ASSUMED_CONTRACT_SIZE_BTC_UM,
) < 1e-6
record("um_contract_size_via_ctval_ctmult", contract_size_ok,
       f"live={mc_effective} BTC, assumed={ASSUMED_CONTRACT_SIZE_BTC_UM} BTC. "
       f"Source: ctVal × ctMult from /api/v5/public/instruments.")

# minSz / lotSz shape check (independent of ctVal × ctMult)
shape_ok = (mc_min == 1.0 and mc_lot == 1.0)
record("um_min_lot_size_shape", shape_ok,
       f"minSz={mc_min}, lotSz={mc_lot} (expected 1/1)")

# Belt and suspenders: if you sent 50 contracts under the verified size,
# what's the implied notional? Print it so the operator sees the
# expected position size before Monday.
sample_qty_btc = 50 * mc_effective if mc_effective else 50 * ASSUMED_CONTRACT_SIZE_BTC_UM
sample_qty_usd = sample_qty_btc * spot
kv("If sent 50 contracts (verified)",
   f"{sample_qty_btc:.4f} BTC ≈ ${sample_qty_usd:,.0f}")
kv("If sent 25 contracts (verified)",
   f"{25 * mc_effective if mc_effective else 0.25:.4f} BTC ≈ "
   f"${(25 * mc_effective if mc_effective else 0.25) * spot:,.0f}")


# ── 4. Cross-family premium-unit probe ────────────────────────────

hdr("CHECK 4 — UM premiums quoted in USD (cross-family mark probe)")
# For SAMPLE_STRIKES strikes closest to spot, fetch the OKX MARK price
# (more stable than ask, which can be empty on thin books) for both
# the UM and CM same-strike same-expiry option. Then verify that
#     UM_mark_usd ≈ CM_mark_btc × spot
# within tolerance. Verified live 2026-05-15: typical agreement is
# within ~2% (e.g. CM mark 0.001804 BTC × $80,377 = $145, UM mark $142).

um_strikes = sorted({float(r["instId"].split("-")[3]) for r in um_today})
um_strikes_near_spot = sorted(
    um_strikes, key=lambda s: abs(s - spot),
)[:SAMPLE_STRIKES]

# Pull CM listing for the same expiry. instFamily=BTC-USD pins us to the
# inverse family (688 rows) and excludes the UM rows that would also be
# returned by a bare uly=BTC-USD query (1162 rows).
cm_resp = public.get_instruments(
    instType="OPTION", uly="BTC-USD", instFamily="BTC-USD",
)
cm_rows = cm_resp.get("data") or []
cm_puts_by_strike = {
    float(r["instId"].split("-")[3]): r
    for r in cm_rows
    if r.get("instId", "").startswith(f"BTC-USD-{soonest}-")
    and r["instId"].endswith("-P")  # puts only
    and r.get("instFamily") == "BTC-USD"
}
um_puts_by_strike = {
    float(r["instId"].split("-")[3]): r
    for r in um_today
    if r["instId"].endswith("-P")
}

agreements = []
print(f"\n  Checking {len(um_strikes_near_spot)} strike(s) closest to "
      f"${spot:,.0f} (puts):\n")
print(f"  {'Strike':>10}  {'UM mark ($)':>12}  {'CM mark (BTC)':>15}  "
      f"{'CM × spot':>11}  {'rel_err':>9}  {'agree?':>8}")
for k in um_strikes_near_spot:
    um_inst = um_puts_by_strike.get(k)
    cm_inst = cm_puts_by_strike.get(k)
    if not um_inst or not cm_inst:
        continue
    try:
        um_mark_resp = public.get_mark_price(
            instType="OPTION", instId=um_inst["instId"],
        )
        cm_mark_resp = public.get_mark_price(
            instType="OPTION", instId=cm_inst["instId"],
        )
    except Exception:
        continue
    um_mark = float(
        ((um_mark_resp.get("data") or [{}])[0]).get("markPx") or 0,
    )
    cm_mark = float(
        ((cm_mark_resp.get("data") or [{}])[0]).get("markPx") or 0,
    )
    if um_mark <= 0 or cm_mark <= 0:
        continue
    cm_mark_usd = cm_mark * spot
    rel_err = (
        abs(um_mark - cm_mark_usd) / cm_mark_usd
        if cm_mark_usd > 0 else 1.0
    )
    agree = rel_err <= PRICE_AGREEMENT_TOLERANCE_PCT
    agreements.append(agree)
    print(f"  {k:>10,.0f}  {um_mark:>12,.2f}  {cm_mark:>15.6f}  "
          f"{cm_mark_usd:>11,.2f}  {rel_err:>8.1%}  "
          f"{('YES' if agree else 'NO'):>8}")

# Sanity bounds on UM mark: an ITM 0DTE option mark in USD-per-BTC
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
           "No strike pairs returned valid mark prices. Re-run during "
           "active hours.")


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
# Build a strike-keyed map of UM puts at the soonest expiry. The
# previous version of this script referenced an undefined
# ``um_strikes_for_expiry`` and would have NameError'd here had
# CHECK 2 not failed first.
um_puts_for_expiry = {
    float(r["instId"].split("-")[3]): r
    for r in um_today
    if r.get("instId", "").endswith("-P")
}

um_itm_put = None
for k in um_strikes_near_spot:
    if k > spot:  # ITM put: strike > spot
        inst = um_puts_for_expiry.get(k)
        if inst:
            um_itm_put = inst
            break
if um_itm_put is None and um_strikes_near_spot:
    # Fallback: any UM put close to spot with valid metadata
    for k in um_strikes_near_spot:
        inst = um_puts_for_expiry.get(k)
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
