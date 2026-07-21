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

    print(f"{len(positions)} open position(s):")
    for p in positions:
        inst = p.get("instrument_name", "?")
        amt = float(p.get("amount", 0.0) or 0.0)
        mark = float(p.get("mark_price", 0.0) or 0.0)
        upnl = float(p.get("unrealized_pnl", 0.0) or 0.0)
        side = "LONG " if amt > 0 else "SHORT" if amt < 0 else "flat "
        print(f"  {side} {inst}  amt={amt:+.4f}  "
              f"mark=${mark:,.4f}  uPnL=${upnl:+,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
