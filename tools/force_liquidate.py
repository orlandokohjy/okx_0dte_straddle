"""
Force-liquidate a single OKX option position to flat.

Designed as an OPERATOR EMERGENCY TOOL — NOT to be run while the live
algo container is up. The chase_sell loop in the live algo polls its
own order every 5s and would reprice into a new order; if this script
runs in parallel, you risk going SHORT after the script's manual sell
fills (the algo's new order can fill afterwards as the buyer side).

Required workflow:
    1. docker-compose stop algo            # release the chase_sell loop
    2. python tools/force_liquidate.py <SYMBOL>
    3. docker-compose start algo           # startup_reconcile will see flat

The script:
    a. Cancels every open order on the symbol (algo's resting orders + any
       leftovers from manual UI clicks).
    b. Reads the actual exchange position size.
    c. Places a TAKER limit sell at the live bid for the full position
       (or a limit buy at the ask if the position is short — handles
       both directions).
    d. Polls until position == 0 or 60s elapses.
    e. Cancels any leftover orders and prints a final status.

Usage:
    python tools/force_liquidate.py BTC-USD-260521-77250-P
    python tools/force_liquidate.py BTC-USD-260521-77250-P --dry-run

Tick-tier note: if the symbol is a CM BTC option above 0.005 BTC,
OKX rounds price submissions to the nearest 0.0005-tick. This script
intentionally crosses the spread (sells at the BID for a long
position, buys at the ASK for a short position) so the order will
take liquidity instantly and tick rounding doesn't matter.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from core.exchange import OKXExchange
from utils.logger import log


async def _liquidate(symbol: str, *, dry_run: bool) -> int:
    """Returns 0 on flat success, non-zero on failure."""
    if not config.HAS_OKX_CREDS:
        print("[force_liquidate] No OKX credentials configured. Aborting.")
        return 2

    exchange = OKXExchange()
    exchange.connect()

    # ── A) cancel any open orders on this symbol ──
    try:
        cleared = await exchange.cancel_orders_for_instrument(symbol)
        print(f"[force_liquidate] cancelled {cleared} open order(s) on {symbol}")
    except Exception as exc:
        print(f"[force_liquidate] cancel failed: {exc}")
        return 3

    # ── B) read actual exchange position ──
    try:
        positions = await exchange.list_open_positions()
    except Exception as exc:
        print(f"[force_liquidate] position fetch failed: {exc}")
        return 4

    target = next(
        (p for p in positions if p["instrument_name"] == symbol), None,
    )
    if target is None:
        print(f"[force_liquidate] no live position on {symbol} — already flat.")
        return 0

    pos_amount = float(target.get("amount", 0.0))
    if abs(pos_amount) < 1e-9:
        print(f"[force_liquidate] position size is zero — already flat.")
        return 0

    print(
        f"[force_liquidate] live position: "
        f"{symbol} amount={pos_amount:+.4f} "
        f"avg=${target.get('average_price', 0):,.4f} "
        f"mark=${target.get('mark_price', 0):,.4f} "
        f"uPnL=${target.get('unrealized_pnl', 0):+,.4f}"
    )

    # ── C) get bid/ask, decide side & price ──
    try:
        ticker = await exchange.get_ticker(symbol)
    except Exception as exc:
        print(f"[force_liquidate] ticker fetch failed: {exc}")
        return 5

    bid = float(ticker.bid)
    ask = float(ticker.ask)
    if bid <= 0 or ask <= 0:
        print(f"[force_liquidate] invalid book bid={bid} ask={ask}")
        return 6

    if pos_amount > 0:
        side = "sell"
        price = bid           # cross to bid → instant taker fill
        qty = pos_amount      # OKX expects same units our code uses
    else:
        side = "buy"
        price = ask           # cross to ask
        qty = abs(pos_amount)

    print(
        f"[force_liquidate] flatten plan: side={side} qty={qty:.4f} "
        f"price={price} (book bid/ask={bid}/{ask})"
    )

    if dry_run:
        print("[force_liquidate] --dry-run — no order placed.")
        return 0

    # ── D) place taker limit at the opposite side ──
    order = await exchange._place_limit_order(
        symbol, side, qty, price, post_only=False,
    )
    sCode = str(order.get("sCode") or "")
    sMsg = str(order.get("sMsg") or "")
    ord_id = order.get("ordId", "")
    print(
        f"[force_liquidate] order placed: ord_id={ord_id} "
        f"sCode={sCode} sMsg={sMsg}"
    )
    if sCode not in ("0", ""):
        print(
            f"[force_liquidate] order REJECTED. "
            f"Inspect manually before retrying."
        )
        return 7

    # ── E) poll for flat (up to 60s) ──
    for attempt in range(12):  # 12 * 5s = 60s
        await asyncio.sleep(5)
        try:
            pos_now = await exchange.list_open_positions()
        except Exception:
            continue
        live = next(
            (p for p in pos_now if p["instrument_name"] == symbol), None,
        )
        if live is None or abs(float(live.get("amount", 0.0))) < 1e-9:
            print(
                f"[force_liquidate] FLAT confirmed after "
                f"{(attempt + 1) * 5}s. Cleanup OK."
            )
            try:
                await exchange.cancel_orders_for_instrument(symbol)
            except Exception:
                pass
            return 0
        print(
            f"[force_liquidate] still has position "
            f"{live.get('amount'):+.4f}, waiting..."
        )

    print(
        f"[force_liquidate] TIMEOUT — position still open after 60s. "
        f"Inspect manually."
    )
    return 8


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "symbol",
        help="OKX option instId, e.g. BTC-USD-260521-77250-P",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan but do not place any orders.",
    )
    args = parser.parse_args(argv)

    return asyncio.run(_liquidate(args.symbol, dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
