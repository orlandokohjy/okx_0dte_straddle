"""
Re-send the DAILY REPORT for a specific trading day on demand.

Useful when the scheduled chained send (in main._on_close after the
last close of the trading day) was missed or silently failed (e.g.
TELEGRAM_REPORT_CHAT_ID misconfigured; aiohttp.post returned a 4xx
that the prior _send_to swallowed).

USAGE
-----
    # Re-send today's UTC trading day report
    python tools/send_daily_report.py

    # Re-send a specific trading day (YYYY-MM-DD)
    python tools/send_daily_report.py 2026-05-21

    # Pass --no-equity to skip the live OKX equity fetch (offline mode)
    python tools/send_daily_report.py 2026-05-21 --no-equity

EXIT CODES
----------
    0  report sent (or skipped because no trades)
    2  trades load failed
    3  notifier exception
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from core import notifier
from utils.time_utils import now_utc


async def _run(trading_day: str | None, fetch_equity: bool) -> int:
    equity = 0.0
    if fetch_equity and config.HAS_OKX_CREDS:
        try:
            from core.exchange import OKXExchange
            exchange = OKXExchange()
            exchange.connect()
            equity = await exchange.get_account_equity()
            print(f"[send_daily_report] live equity from OKX: ${equity:,.2f}")
        except Exception as exc:
            print(f"[send_daily_report] live equity fetch failed: {exc}")
            print("[send_daily_report] continuing with equity=0 — the "
                  "report will compute realised P&L from the trade log "
                  "but the running-equity line may show 0.")
    elif not config.HAS_OKX_CREDS:
        print("[send_daily_report] no OKX creds — equity will be 0 "
              "in the report.")
    else:
        print("[send_daily_report] --no-equity flag set — equity will "
              "be 0 in the report.")

    if trading_day is None:
        from reporting.daily_report import _trading_day_from_entry_time
        trading_day = _trading_day_from_entry_time(
            now_utc().isoformat(),
            fallback_date=now_utc().strftime("%Y-%m-%d"),
        )
    print(f"[send_daily_report] trading_day = {trading_day}")
    print(f"[send_daily_report] sending to chat: "
          f"TELEGRAM_REPORT_CHAT_ID={config.TELEGRAM_REPORT_CHAT_ID or '<unset, falls back to TELEGRAM_CHAT_ID>'}")
    print(f"[send_daily_report] regular chat: "
          f"TELEGRAM_CHAT_ID={config.TELEGRAM_CHAT_ID or '<unset>'}")

    try:
        await notifier.send_daily_report(equity, trading_day=trading_day)
    except Exception as exc:
        print(f"[send_daily_report] FAILED: {exc}")
        return 3

    print("[send_daily_report] notifier.send_daily_report returned. "
          "Check the algo logs (or this terminal) for "
          "telegram_send_rejected entries if the chat didn't receive "
          "anything.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "trading_day",
        nargs="?",
        default=None,
        help="Trading day in YYYY-MM-DD form (UTC expiry date). "
             "Defaults to today's UTC trading day inferred from the "
             "08:00 UTC cutoff.",
    )
    parser.add_argument(
        "--no-equity",
        action="store_true",
        help="Skip the live OKX equity fetch.",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.trading_day, not args.no_equity))


if __name__ == "__main__":
    sys.exit(main())
