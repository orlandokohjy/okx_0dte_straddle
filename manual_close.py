"""
Manual close for OKX positions — flatten any open option position
without going through the OKX UI.

Reads OKX credentials from .env. Uses chase-sell (maker-only) by default
or `--taker` for an immediate fill at the bid (pays 1-tick spread).

Usage:
    python manual_close.py                # maker-only chase (slow, no fees)
    python manual_close.py --taker        # taker fill (fast, ~1 tick cost)
    python manual_close.py --dry-run      # print only, no orders
    python manual_close.py --silent       # no Telegram notifications
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import structlog

import config
from core.exchange import OKXExchange
from core import notifier
from utils.logging_config import setup_logging

log = structlog.get_logger(__name__)


async def _close_one_position(
    exchange: OKXExchange,
    inst: str,
    pos_contracts: float,
    taker: bool,
) -> tuple[bool, dict | None]:
    """Close a single option position. Returns (ok, fill_result)."""
    # OKX `pos` is in contracts. Convert to BTC notional for chase_*.
    qty_btc = abs(pos_contracts) * config.OKX_CONTRACT_SIZE_BTC
    side = "sell" if pos_contracts > 0 else "buy"

    try:
        ticker = await exchange.get_ticker(inst)
    except Exception:
        log.warning("manual_close_ticker_failed", instrument=inst, exc_info=True)
        return False, None

    if ticker.bid <= 0 or ticker.ask <= 0:
        log.warning("manual_close_no_quote",
                    instrument=inst, bid=ticker.bid, ask=ticker.ask)
        return False, None

    log.info("manual_close_unwinding",
             instrument=inst, side=side,
             pos_contracts=pos_contracts, qty_btc=qty_btc,
             bid=ticker.bid, ask=ticker.ask, taker=taker)

    if config.DRY_RUN:
        log.info("manual_close_dry_run_skip",
                 instrument=inst, side=side,
                 qty_btc=qty_btc, taker=taker)
        return True, None

    if taker:
        # Lift the opposite side immediately. Sell → cross to bid, Buy → cross to ask.
        # We pad by 1 tick to ensure the cross happens even if mid moves slightly.
        tick = exchange.get_tick_size(inst)
        if tick <= 0:
            tick = config.OPTION_TICK_SIZE
        if side == "sell":
            taker_price = max(ticker.bid - tick, tick)
        else:
            taker_price = ticker.ask + tick

        log.info("manual_close_taker_order",
                 instrument=inst, side=side, price=taker_price,
                 qty_btc=qty_btc, post_only=False)

        try:
            order = await exchange._place_limit_order(
                instrument=inst, side=side,
                qty_btc=qty_btc, price=taker_price,
                post_only=False,
            )
        except Exception:
            log.error("manual_close_taker_failed",
                      instrument=inst, exc_info=True)
            return False, None

        ord_id = order.get("ordId")
        sCode = str(order.get("sCode") or "")
        if not ord_id or sCode not in ("0", ""):
            log.error("manual_close_taker_rejected",
                      instrument=inst, sCode=sCode,
                      sMsg=order.get("sMsg"))
            return False, None

        # Wait briefly for fill confirmation
        status = await exchange._wait_for_fill(inst, ord_id, timeout=10.0)
        state = status.get("state", "")
        if state == "filled":
            avg_px = exchange._f(status, "avgPx", default=taker_price)
            log.info("manual_close_taker_filled",
                     instrument=inst, fill_price=avg_px, ord_id=ord_id)
            return True, {"average_price": avg_px, "order_id": ord_id}

        # Not filled within 10s — try once more with even more aggressive crossing
        log.warning("manual_close_taker_no_fill_yet",
                    instrument=inst, state=state, ord_id=ord_id)
        return False, None

    # Maker-only chase (default)
    try:
        if side == "sell":
            result = await exchange.chase_sell(inst, qty_btc, ticker.ask)
        else:
            result = await exchange.chase_buy(inst, qty_btc, ticker.bid)
    except Exception:
        log.error("manual_close_chase_failed",
                  instrument=inst, exc_info=True)
        return False, None

    if result is None:
        log.warning("manual_close_chase_no_fill", instrument=inst)
        return False, None

    avg_px = result.get("average_price", 0.0)
    fully = result.get("fully_filled", True)
    filled_btc = result.get("filled_qty_btc", qty_btc)
    if not fully:
        log.warning("manual_close_chase_partial",
                    instrument=inst, fill_price=avg_px,
                    filled_qty_btc=filled_btc, target_qty_btc=qty_btc,
                    residual_btc=qty_btc - filled_btc)
        return False, result
    log.info("manual_close_chase_filled",
             instrument=inst, fill_price=avg_px,
             filled_qty_btc=filled_btc)
    return True, result


async def manual_close(
    dry_run: bool = False,
    silent: bool = False,
    taker: bool = False,
) -> int:
    setup_logging()

    if dry_run:
        config.DRY_RUN = True

    exchange = OKXExchange()
    exchange.connect()

    log.info("manual_close_start",
             okx_flag=config.OKX_FLAG,
             dry_run=config.DRY_RUN,
             taker=taker)

    if not silent and not dry_run:
        await notifier.send(
            "<b>MANUAL CLOSE STARTED</b>\n"
            f"Mode: {'DEMO' if config.OKX_FLAG == '1' else 'LIVE'} "
            f"({'TAKER' if taker else 'MAKER'})\n"
            "Flattening all open OKX positions…"
        )

    # 1. Cancel any resting orders first so they don't interfere
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
            f"  • {p['instrument_name']}  pos={p['amount']:+.4f}  "
            f"mark={p['mark_price']:.4f}  uPnL={p['unrealized_pnl']:+.4f}"
            for p in positions
        )
        await notifier.send(
            f"<b>MANUAL CLOSE — found {len(positions)} positions</b>\n"
            f"{details}\n\n"
            f"Flattening with {'TAKER' if taker else 'maker-only chase'}…"
        )

    # 3. Close each non-zero position
    sold = 0
    failed = 0
    for p in positions:
        inst = p["instrument_name"]
        amt = float(p["amount"])
        if amt == 0:
            continue
        ok, _result = await _close_one_position(
            exchange, inst, amt, taker=taker,
        )
        if ok:
            sold += 1
        else:
            failed += 1

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
                f"  • {p['instrument_name']}  pos={p['amount']:+.4f}"
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
    parser.add_argument("--taker", action="store_true",
                        help="Use taker order (instant fill, ~1-tick cost) "
                             "instead of maker-only chase.")
    args = parser.parse_args()

    rc = asyncio.run(manual_close(args.dry_run, args.silent, args.taker))
    sys.exit(rc)


if __name__ == "__main__":
    main()
