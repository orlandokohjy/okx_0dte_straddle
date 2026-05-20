"""
Force-flatten OKX option positions and cancel pending orders.

OPERATOR EMERGENCY PANIC-BUTTON. Designed for the case where the live
algo's auto-recovery is stuck (e.g. tier-tick rounding making chase_sell
ineffective, or a partial fill that exceeded the chase deadline) and
the operator needs to MANUALLY restore flat state before letting the
algo run again.

CRITICAL: must be run with the live algo STOPPED:

    docker-compose stop algo

Running this script while the algo container is up creates a race: the
algo's chase_sell loop polls every 5s and may place a NEW maker sell
right after this script's taker sell fills, potentially flipping the
operator into an unintended SHORT position.

The script refuses to run if it detects the algo's lock file
(state/algo.pid) is held — pass --force to override (NOT recommended).

USAGE
-----

    # Flatten ALL option positions + cancel ALL option orders (default)
    python tools/force_liquidate.py

    # Surgical: single symbol only
    python tools/force_liquidate.py BTC-USD-260521-77250-P

    # Preview without placing orders
    python tools/force_liquidate.py --dry-run
    python tools/force_liquidate.py BTC-USD-260521-77250-P --dry-run

    # Override the lock-file safety check (only if you KNOW the algo is
    # really stopped, e.g. the lock file is stale from a crashed run)
    python tools/force_liquidate.py --force

BEHAVIOUR (per call)
--------------------
    A. Refuse to run if state/algo.pid is held by a live process.
    B. Discover all live OKX option positions + all pending option orders
       (filtered by the active OPTION_FAMILY: CM or UM).
    C. Print a plan summary.
    D. Cancel every open order on every targeted instrument.
    E. For each non-zero position:
         * Long  → place TAKER limit SELL at the live BID (crosses the spread)
         * Short → place TAKER limit BUY  at the live ASK
       Crossing the spread sidesteps tier-tick rounding (any tier tick we
       round to is still inside the book) and guarantees a near-instant fill.
    F. Poll up to 60 s per instrument for confirmation of zero position.
    G. Final cancel sweep + summary print.

EXIT CODES
----------
    0  flat across all targeted instruments — algo can be restarted safely
    2  no OKX credentials in env
    3  lock file held by a live PID (pass --force to override)
    4  position fetch failed (network/auth)
    5  order placement failed for at least one instrument
    6  timeout — at least one instrument still has live position after 60 s
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from core.exchange import OKXExchange


def _check_lock(force: bool) -> int:
    """Refuse to run if state/algo.pid is held by a live process.

    The algo writes its PID to state/algo.pid via _acquire_singleton_lock
    in main.py. Running this script while the algo is up causes a race
    against the chase_sell reprice loop. Returns 0 if safe to proceed,
    3 if the lock is held and --force was not passed.
    """
    lock_path = f"{config.STATE_DIR}/algo.pid"
    if not os.path.exists(lock_path):
        return 0
    try:
        with open(lock_path, "r") as f:
            pid = int((f.read() or "0").strip())
    except Exception:
        pid = 0
    if pid <= 0:
        if force:
            return 0
        print(
            f"[force_liquidate] lock file {lock_path} exists but is empty "
            "or unreadable. Pass --force to override."
        )
        return 3
    # Docker PID-1 collision: when the algo runs in a container as PID 1,
    # it writes "1" to state/algo.pid. If the operator runs this script
    # via `docker-compose run --rm algo python tools/force_liquidate.py`
    # AFTER stopping the algo, the one-off container is also PID 1 and
    # the state/ volume is shared. os.kill(1, 0) would falsely succeed
    # against ourselves. Detect & treat as stale.
    if pid == os.getpid():
        print(
            f"[force_liquidate] note: lock file pid={pid} matches our own "
            "PID. Classic Docker PID-1 collision (stopped algo container "
            "shared state/ with this one-off run). Lock is stale — "
            "proceeding."
        )
        return 0
    # Probe the PID. We're typically running OUTSIDE the container so the
    # PID space is the host's; the algo's PID 1 inside the container won't
    # exist on the host. We treat "PID does not exist on this host" as
    # ambiguous — warn but proceed. The DEFINITIVE check is whether you
    # ran `docker-compose stop algo`.
    try:
        os.kill(pid, 0)
        pid_alive = True
    except (ProcessLookupError, PermissionError):
        pid_alive = False
    except Exception:
        pid_alive = False

    if pid_alive and not force:
        print(
            f"[force_liquidate] LOCK HELD: {lock_path} → pid={pid} is alive "
            "on this host. Refusing to run.\n"
            "   ACTION: docker-compose stop algo\n"
            "   THEN re-run this script.\n"
            "   (or pass --force if you are CERTAIN the algo is stopped)"
        )
        return 3
    if pid_alive and force:
        print(
            f"[force_liquidate] WARN: lock {lock_path} held by pid={pid} but "
            "--force given. Proceeding (race-condition risk)."
        )
        return 0
    print(
        f"[force_liquidate] note: lock file {lock_path} present but pid={pid} "
        "is not visible on this host. Likely stale or container-internal. "
        "Proceeding."
    )
    return 0


async def _flatten_one(
    exchange: OKXExchange,
    symbol: str,
    *,
    dry_run: bool,
) -> tuple[bool, str]:
    """Flatten a single instrument. Returns (success, status_line).

    UNIT-SAFETY NOTE
    ----------------
    OKX position rows expose `pos` in CONTRACTS (1 contract = 0.01 BTC for CM
    BTC options; same for UM linear). Our internal `_place_limit_order`
    expects `qty_btc` in BTC NOTIONAL. We always convert at the boundary
    here so the order amount sent to OKX matches the position size exactly,
    NOT 100× more.
    """
    contract_size_btc = config.OKX_CONTRACT_SIZE_BTC

    # ── A) cancel any open orders on this symbol ──
    try:
        cancelled = await exchange.cancel_orders_for_instrument(symbol)
    except Exception as exc:
        return False, f"{symbol}: cancel-orders failed: {exc}"
    if cancelled > 0:
        print(f"  [{symbol}] cancelled {cancelled} open order(s)")

    # ── B) read actual exchange position (contracts) ──
    try:
        positions = await exchange.list_open_positions()
    except Exception as exc:
        return False, f"{symbol}: position fetch failed: {exc}"
    target = next(
        (p for p in positions if p["instrument_name"] == symbol), None,
    )
    if target is None or abs(float(target.get("amount", 0.0))) < 1e-9:
        return True, f"{symbol}: already flat"

    # `amount` is signed contract count (positive=long, negative=short).
    pos_contracts = float(target.get("amount", 0.0))
    pos_btc = pos_contracts * contract_size_btc
    print(
        f"  [{symbol}] live position: {pos_contracts:+.0f} contract(s) "
        f"(= {pos_btc:+.4f} BTC notional) "
        f"avg=${target.get('average_price', 0):,.4f} "
        f"mark=${target.get('mark_price', 0):,.4f} "
        f"uPnL=${target.get('unrealized_pnl', 0):+,.2f}"
    )

    # ── C) get bid/ask, decide side & price ──
    try:
        ticker = await exchange.get_ticker(symbol)
    except Exception as exc:
        return False, f"{symbol}: ticker fetch failed: {exc}"
    bid = float(ticker.bid)
    ask = float(ticker.ask)
    if bid <= 0 or ask <= 0:
        return False, f"{symbol}: invalid book bid={bid} ask={ask}"

    if pos_contracts > 0:
        side = "sell"
        price = bid
        qty_btc = pos_btc
        qty_contracts_planned = pos_contracts
    else:
        side = "buy"
        price = ask
        qty_btc = abs(pos_btc)
        qty_contracts_planned = abs(pos_contracts)

    # SAFETY GUARD: round-trip BTC→contracts and refuse if it disagrees
    # with the position size we just read. Catches any future
    # unit-conversion regression before it sends a wrong-sized order.
    recomputed_contracts = round(qty_btc / contract_size_btc)
    if abs(recomputed_contracts - qty_contracts_planned) > 0.5:
        return False, (
            f"{symbol}: SAFETY ABORT — qty_btc={qty_btc} maps to "
            f"{recomputed_contracts} contracts, but position is "
            f"{qty_contracts_planned}. Refusing to place order."
        )

    print(
        f"  [{symbol}] flatten plan: side={side} "
        f"qty={qty_contracts_planned:.0f} contract(s) "
        f"({qty_btc:.4f} BTC) "
        f"price={price} (book bid/ask={bid}/{ask})"
    )
    if dry_run:
        return True, f"{symbol}: dry-run plan ok"

    # ── D) place taker limit at the opposite side ──
    order = await exchange._place_limit_order(
        symbol, side, qty_btc, price, post_only=False,
    )
    sCode = str(order.get("sCode") or "")
    sMsg = str(order.get("sMsg") or "")
    ord_id = order.get("ordId", "")
    print(f"  [{symbol}] order placed: ord_id={ord_id} sCode={sCode} sMsg={sMsg}")
    if sCode not in ("0", ""):
        return False, f"{symbol}: order REJECTED sCode={sCode} sMsg={sMsg}"

    # ── E) poll for flat (up to 60 s) ──
    for attempt in range(12):  # 12 * 5 s = 60 s
        await asyncio.sleep(5)
        try:
            pos_now = await exchange.list_open_positions()
        except Exception:
            continue
        live = next(
            (p for p in pos_now if p["instrument_name"] == symbol), None,
        )
        if live is None or abs(float(live.get("amount", 0.0))) < 1e-9:
            try:
                await exchange.cancel_orders_for_instrument(symbol)
            except Exception:
                pass
            return True, (
                f"{symbol}: FLAT confirmed after {(attempt + 1) * 5}s"
            )
        print(
            f"  [{symbol}] still has position {live.get('amount'):+.4f}, "
            f"waiting..."
        )
    return False, f"{symbol}: TIMEOUT — position still open after 60 s"


async def _liquidate(
    symbol: str | None, *, dry_run: bool,
) -> int:
    if not config.HAS_OKX_CREDS:
        print("[force_liquidate] No OKX credentials configured. Aborting.")
        return 2
    exchange = OKXExchange()
    exchange.connect()

    # ── 1) DISCOVER ──
    try:
        positions = await exchange.list_open_positions()
        orders = await exchange.list_open_orders()
    except Exception as exc:
        print(f"[force_liquidate] discovery failed: {exc}")
        return 4

    if symbol is not None:
        positions = [
            p for p in positions if p["instrument_name"] == symbol
        ]
        orders = [o for o in orders if o.get("instId") == symbol]

    pos_symbols = {p["instrument_name"] for p in positions}
    ord_symbols = {o.get("instId", "") for o in orders if o.get("instId")}
    targets = sorted(pos_symbols | ord_symbols)

    print("=" * 72)
    print(f"[force_liquidate] mode={'SINGLE' if symbol else 'ALL'}  "
          f"dry_run={dry_run}  family={config.OPTION_FAMILY}")
    print(f"[force_liquidate] live positions: {len(positions)}")
    contract_size_btc = config.OKX_CONTRACT_SIZE_BTC
    for p in positions:
        contracts = float(p.get('amount', 0))
        btc = contracts * contract_size_btc
        print(
            f"  • {p['instrument_name']:36s}  "
            f"{contracts:+.0f} contract(s)  "
            f"(={btc:+.4f} BTC)  "
            f"mark=${float(p.get('mark_price', 0)):,.4f}  "
            f"uPnL=${float(p.get('unrealized_pnl', 0)):+,.2f}"
        )
    print(f"[force_liquidate] open orders:    {len(orders)}")
    for o in orders:
        print(
            f"  • {o.get('instId', '?'):36s}  "
            f"side={o.get('side', '?')}  px={o.get('px', '?')}  "
            f"sz={o.get('sz', '?')}  state={o.get('state', '?')}"
        )
    print(f"[force_liquidate] symbols to flatten: {len(targets)}")
    print("=" * 72)

    if not targets:
        print("[force_liquidate] nothing to do — all flat, no open orders.")
        return 0

    # ── 2) FLATTEN each target ──
    results: list[tuple[bool, str]] = []
    for sym in targets:
        ok, line = await _flatten_one(exchange, sym, dry_run=dry_run)
        results.append((ok, line))
        print(f"[force_liquidate] {'OK ' if ok else 'ERR'}  {line}")

    # ── 3) Summary ──
    print("=" * 72)
    fails = [(ok, line) for ok, line in results if not ok]
    if fails:
        print(f"[force_liquidate] {len(fails)} failures out of "
              f"{len(results)} targets:")
        for _, line in fails:
            print(f"  ✗ {line}")
        return 5 if any("REJECTED" in line or "failed" in line
                        for _, line in fails) else 6

    if dry_run:
        print("[force_liquidate] DRY-RUN OK. No orders placed.")
        return 0

    print(
        f"[force_liquidate] ALL CLEAR — {len(results)} symbol(s) flat. "
        "Safe to docker-compose start algo."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "symbol",
        nargs="?",
        default=None,
        help=(
            "Optional OKX option instId (e.g. BTC-USD-260521-77250-P). "
            "If omitted, ALL option positions and orders for the active "
            "family are flattened."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan but do not place any orders.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Bypass the algo lock-file check. Use ONLY when you are "
            "certain the algo container is stopped (e.g. lock file is "
            "stale from a crashed run)."
        ),
    )
    args = parser.parse_args(argv)

    rc = _check_lock(args.force)
    if rc != 0:
        return rc

    return asyncio.run(
        _liquidate(args.symbol, dry_run=args.dry_run),
    )


if __name__ == "__main__":
    sys.exit(main())
