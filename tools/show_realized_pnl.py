"""
Read-only report of REALIZED P&L from OKX's own position-close history.

This is the AUTHORITATIVE source — it reads OKX's `positions-history`
endpoint (what the exchange actually booked when each position closed),
NOT the algo's local trade_log.csv (which can carry phantom exits from a
buggy close). Strictly read-only: no orders, no PID lock, safe to run live.

For coin-margined (CM / inverse) options OKX reports realizedPnl/fee in the
coin (BTC), so a USD estimate is shown as realizedPnl_BTC × CURRENT spot
(an approximation — the exact figure uses spot at close time).

USAGE
-----
    docker-compose run --rm --entrypoint "" algo \
        python tools/show_realized_pnl.py [--limit N] [--inst SUBSTR]

    # examples
    python tools/show_realized_pnl.py                 # last 20 option closes
    python tools/show_realized_pnl.py --limit 50
    python tools/show_realized_pnl.py --inst 260722    # only that expiry
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from core import family
from core.exchange import OKXExchange


def _ms_to_utc(ms: str) -> str:
    try:
        return datetime.fromtimestamp(
            int(ms) / 1000.0, tz=timezone.utc,
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError):
        return "?"


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


async def _run(limit: int, inst_filter: str) -> int:
    if not config.HAS_OKX_CREDS:
        print("[show_realized_pnl] No OKX credentials configured. Aborting.")
        return 2

    ex = OKXExchange()
    ex.connect()

    try:
        resp = ex._account.get_positions_history(
            instType="OPTION", limit=str(limit),
        )
    except Exception as exc:
        print(f"[show_realized_pnl] positions-history fetch failed: {exc}")
        return 3

    if str(resp.get("code")) != "0":
        print(f"[show_realized_pnl] OKX error: {resp.get('code')} "
              f"{resp.get('msg')}")
        return 3

    rows = resp.get("data", []) or []
    if inst_filter:
        rows = [r for r in rows if inst_filter in str(r.get("instId", ""))]
    if not rows:
        print("No closed option positions found "
              f"{'matching ' + inst_filter if inst_filter else ''}.")
        return 0

    is_um = family.is_um()
    spot = 0.0
    if not is_um:
        try:
            spot = await ex.get_spot_price()
        except Exception:
            spot = 0.0
    unit = "USD" if is_um else "BTC"

    print(f"REALIZED P&L — last {len(rows)} closed option position(s)  "
          f"[{family.label()}]"
          + (f"  spot=${spot:,.0f}" if not is_um else ""))
    print("=" * 78)

    total_native = 0.0
    total_fee_native = 0.0
    for r in sorted(rows, key=lambda x: x.get("uTime", "0")):
        inst = r.get("instId", "?")
        direction = r.get("direction", "?")          # long / short
        realized = _f(r.get("realizedPnl"))          # pnl + fee + funding
        pnl = _f(r.get("pnl"))                        # price P&L only
        fee = _f(r.get("fee"))                        # negative = paid
        open_px = r.get("openAvgPx", "?")
        close_px = r.get("closeAvgPx", "?")
        closed = _ms_to_utc(r.get("uTime", ""))
        total_native += realized
        total_fee_native += fee
        usd = f"  (${realized * spot:+,.2f})" if not is_um and spot else ""
        print(f"  {closed}  {inst}")
        print(f"      {direction:<5}  open={open_px:<10} close={close_px:<10}")
        print(f"      realizedPnl={realized:+.6f} {unit}{usd}   "
              f"(price={pnl:+.6f}, fee={fee:+.6f})")

    print("=" * 78)
    usd_tot = (f"  (${total_native * spot:+,.2f})"
               if not is_um and spot else "")
    print(f"TOTAL realized P&L: {total_native:+.6f} {unit}{usd_tot}")
    print(f"  of which fees:    {total_fee_native:+.6f} {unit}")
    if not is_um and spot:
        print("  NOTE: USD is an estimate at CURRENT spot; the booked USD "
              "used spot at each close time.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20,
                    help="max closed positions to fetch (default 20)")
    ap.add_argument("--inst", type=str, default="",
                    help="only show instIds containing this substring")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(_run(args.limit, args.inst)))
