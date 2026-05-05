"""
Manual close for OKX positions — use BEFORE 08:00 UTC option expiry to
flatten any open straddle if the scheduled 16:00 UTC close would miss
it (e.g. the algo was started outside the normal session window).

Reads OKX credentials from .env and sells every non-zero position with
a maker-only chase. No equity / portfolio side-effects — purely
unwinds whatever the exchange has open.

Usage:
    python manual_close.py
    python manual_close.py --dry-run     # print only, no orders
    python manual_close.py --silent      # no Telegram notifications
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import structlog

import config
from core.exchange import OKXExchange
from core import notifier
from utils.logging_config import setup_logging

log = structlog.get_logger(__name__)


async def manual_close(dry_run: bool = False, silent: bool = False) -> int:
    setup_logging()

    # Force DRY_RUN if requested (overrides .env)
    if dry_run:
        config.DRY_RUN = True

    exchange = OKXExchange()
    exchange.connect()

    log.info("manual_close_start",
             okx_flag=config.OKX_FLAG,
             dry_run=config.DRY_RUN)

    if not silent and not dry_run:
        await notifier.send(
            "<b>MANUAL CLOSE STARTED</b>\n"
            f"Mode: {'DEMO' if config.OKX_FLAG == '1' else 'LIVE'}\n"
            "Flattening all open OKX positions…"
        )

    # 1. Cancel any resting orders first so they don't block the unwind
    try:
        cancelled = await exchange.cancel_all_open_orders()
        log.info("manual_close_orders_cancelled", count=cancelled)
    except Exception:
        log.warning("manual_close_cancel_failed", exc_info=True)

    # 2. List open positions
    try:
        positions = await exchange.list_open_positions()
    except Exception:
        log.error("manual_close_list_failed", exc_info=True)
        if not silent:
            await notifier.notify_error(
                "Manual close",
                "Could not list positions — check logs",
            )
        return 2

    if not positions:
        log.info("manual_close_no_positions")
        if not silent:
            await notifier.send(
                "<b>MANUAL CLOSE</b>\n"
                "No open positions on OKX — already flat."
            )
        return 0

    log.info("manual_close_positions_found",
             count=len(positions),
             positions=[f"{p['instrument_name']} {p['amount']:+.4f}"
                        for p in positions])

    if not silent:
        details = "\n".join(
            f"  • {p['instrument_name']} amt={p['amount']:+.4f}  "
            f"mark=${p['mark_price']:,.2f}  "
            f"uPnL=${p['unrealized_pnl']:+,.2f}"
            for p in positions
        )
        await notifier.send(
            f"<b>MANUAL CLOSE — found {len(positions)} positions</b>\n"
            f"{details}\n\nFlattening with maker-only chase…"
        )

    # 3. Sell each non-zero position
    sold = 0
    failed = 0
    for p in positions:
        inst = p["instrument_name"]
        amt = float(p["amount"])
        if amt == 0:
            continue

        # Side opposite of current holding
        side = "sell" if amt > 0 else "buy"
        qty_btc = abs(amt)

        try:
            t = await exchange.get_ticker(inst)
        except Exception:
            log.warning("manual_close_ticker_failed",
                        instrument=inst, exc_info=True)
            failed += 1
            continue

        if t.bid <= 0 or t.ask <= 0:
            log.warning("manual_close_no_quote",
                        instrument=inst, bid=t.bid, ask=t.ask)
            failed += 1
            continue

        log.info("manual_close_unwinding",
                 instrument=inst, side=side, qty_btc=qty_btc,
                 bid=t.bid, ask=t.ask)

        if config.DRY_RUN:
            log.info("manual_close_dry_run_skip",
                     instrument=inst, side=side, qty=qty_btc)
            sold += 1
            continue

        try:
            if side == "sell":
                result = await exchange.chase_sell(inst, qty_btc, t.ask)
            else:
                result = await exchange.chase_buy(inst, qty_btc, t.bid)
        except Exception:
            log.error("manual_close_chase_failed",
                      instrument=inst, exc_info=True)
            failed += 1
            continue

        if result is None:
            log.warning("manual_close_chase_no_fill", instrument=inst)
            failed += 1
            continue

        log.info("manual_close_filled",
                 instrument=inst,
                 fill_price=result.get("avg_fill_price"))
        sold += 1

    # 4. Verify flat
    try:
        residuals = await exchange.list_open_positions()
    except Exception:
        residuals = positions  # be safe

    msg_lines = [
        f"<b>MANUAL CLOSE DONE</b>",
        f"  Closed: {sold}",
        f"  Failed: {failed}",
        f"  Residual: {len(residuals)} position(s)",
    ]
    if residuals:
        msg_lines.append("")
        msg_lines.append("<b>Still open — investigate:</b>")
        for p in residuals:
            msg_lines.append(
                f"  • {p['instrument_name']}  amt={p['amount']:+.4f}"
            )

    log.info("manual_close_done",
             sold=sold, failed=failed, residual=len(residuals))
    if not silent:
        await notifier.send("\n".join(msg_lines))

    return 0 if (failed == 0 and not residuals) else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manually flatten all open OKX positions."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print actions without sending orders.")
    parser.add_argument("--silent", action="store_true",
                        help="Suppress Telegram notifications.")
    args = parser.parse_args()

    rc = asyncio.run(manual_close(args.dry_run, args.silent))
    sys.exit(rc)


if __name__ == "__main__":
    main()
