"""
Read-only snapshot of LIVE open positions on the OKX trading account.

Strictly read-only: never places/cancels orders and never touches the
algo PID lock, so it is safe to run while the algo is live.

USAGE
-----
    docker-compose exec algo python tools/show_positions.py
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


async def _run() -> int:
    if not config.HAS_OKX_CREDS:
        print("[show_positions] No OKX credentials configured. Aborting.")
        return 2

    ex = OKXExchange()
    ex.connect()
    try:
        positions = await ex.list_open_positions()
    except Exception as exc:
        print(f"[show_positions] fetch failed: {exc}")
        return 3

    if not positions:
        print("FLAT — no open positions.")
        return 0

    # For CM (inverse) options OKX returns upl/avgPx/markPx in BTC, so the
    # USD value is upl_BTC × spot. For UM (linear) it is already USD.
    is_um = family.is_um()
    spot = 0.0
    if not is_um:
        try:
            spot = await ex.get_spot_price()
        except Exception:
            spot = 0.0
    unit = "USD" if is_um else "BTC"

    print(f"{len(positions)} open position(s)  "
          f"[{family.label()}]  spot=${spot:,.0f}")
    for p in positions:
        inst = p.get("instrument_name", "?")
        amt = float(p.get("amount", 0.0) or 0.0)
        avg = float(p.get("average_price", 0.0) or 0.0)
        mark = float(p.get("mark_price", 0.0) or 0.0)
        upnl = float(p.get("unrealized_pnl", 0.0) or 0.0)     # native (BTC/USD)
        upnl_usd = upnl if is_um else upnl * spot
        side = "LONG " if amt > 0 else "SHORT" if amt < 0 else "flat "
        btc = amt * config.OKX_CONTRACT_SIZE_BTC
        print(f"  {side} {inst}  amt={amt:+.0f} ({btc:+.4f} BTC)  "
              f"avg={avg:g} mark={mark:g} {unit}  "
              f"uPnL={upnl:+.6f} {unit} (${upnl_usd:+,.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
