"""Telegram notification helper.

Two channels:
  - Ops chat (TELEGRAM_CHAT_ID): startup, pre-flight, entry, close, errors
  - Report chat (TELEGRAM_REPORT_CHAT_ID): slim daily report only
"""
from __future__ import annotations

import structlog

import config

log = structlog.get_logger(__name__)


async def _send_to(bot_token: str, chat_id: str, text: str) -> None:
    """Send a Telegram message and SURFACE any failure.

    aiohttp does NOT raise on HTTP 4xx/5xx by default; the prior version
    of this function `await`-ed the POST and threw away the response,
    which silently swallowed Telegram API rejections (wrong chat id,
    bot kicked from group, message length > 4096, malformed HTML, etc.).
    The caller would log e.g. "daily_report_sent" even when nothing
    actually arrived in the chat.

    Now we read the response body, log the HTTP status + Telegram
    error description on any non-200, and raise on transport errors so
    callers can trigger their own "report_failed" branch.
    """
    if not bot_token or not chat_id:
        log.debug("telegram_disabled", chat_id=chat_id, msg=text[:80])
        return
    try:
        import aiohttp
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
            }) as response:
                body = await response.text()
                if response.status != 200:
                    # Telegram returns JSON like:
                    # {"ok":false,"error_code":400,
                    #  "description":"Bad Request: chat not found"}
                    # Logging body[:500] gives operators the actionable
                    # signal without leaking surrounding context.
                    log.warning(
                        "telegram_send_rejected",
                        chat_id=chat_id,
                        http_status=response.status,
                        body=body[:500],
                        msg_len=len(text),
                        msg_preview=text[:200],
                    )
    except Exception:
        log.warning("telegram_send_failed", chat_id=chat_id, exc_info=True)


async def send(text: str) -> None:
    """Send to the ops/testing chat (personal bot)."""
    await _send_to(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, text)


async def send_report(text: str) -> None:
    """Send to the report group chat."""
    bot = config.TELEGRAM_REPORT_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN
    chat = config.TELEGRAM_REPORT_CHAT_ID or config.TELEGRAM_CHAT_ID
    await _send_to(bot, chat, text)


async def notify_entry(
    num_straddles: int, equity: float, straddle_cost: float,
    strike: float, call_fill: float, put_fill: float,
    call_cost_total: float, put_cost_total: float,
    session_label: str = "",
    qty_per_leg: float = 0.0,
) -> None:
    """All money values are USD. The caller is responsible for converting
    BTC-quoted OKX premiums to USD via spot before invoking this.

    session_label is a user-visible window such as ``13:30-15:30 UTC``.
    """
    header = "<b>SESSION ENTRY</b>"
    if session_label:
        header = f"<b>SESSION ENTRY [{session_label}]</b>"
    qty_line = (
        f"BTC per leg: {qty_per_leg:.4f}\n" if qty_per_leg > 0 else ""
    )
    await send(
        f"{header}\n"
        f"Straddles: {num_straddles}\n"
        f"{qty_line}"
        f"Equity: ${equity:,.2f}\n"
        f"\n<b>Fills</b>\n"
        f"Strike: ${strike:,.0f}\n"
        f"Call premium per BTC: ${call_fill:,.2f}\n"
        f"Put premium per BTC: ${put_fill:,.2f}\n"
        f"\n<b>Capital used</b>\n"
        f"Call cost: ${call_cost_total:,.2f}\n"
        f"Put cost: ${put_cost_total:,.2f}\n"
        f"Total: ${call_cost_total + put_cost_total:,.2f}\n"
    )


async def notify_close(
    pnl: float, exit_reason: str, session_label: str = "",
) -> None:
    pnl_sign = "+" if pnl >= 0 else ""
    header = "<b>SESSION CLOSE</b>"
    if session_label:
        header = f"<b>SESSION CLOSE [{session_label}]</b>"
    await send(
        f"{header}\n"
        f"P&L: {pnl_sign}${pnl:,.2f}\n"
    )


async def notify_skip(reason: str) -> None:
    await send(f"<b>SKIPPED</b>\n{reason}")


async def notify_error(context: str, message: str) -> None:
    await send(f"<b>ERROR</b> [{context}]\n{message}")


async def send_daily_report(
    equity: float, trading_day: str | None = None,
) -> None:
    """Generate and send the full DAILY REPORT to the report group chat.

    trading_day is the UTC expiry date (YYYY-MM-DD); when None, the
    report is computed for the current UTC trading day inferred from
    the 08:00 UTC cutoff.
    """
    from reporting.daily_report import compute_report, format_telegram_report
    try:
        metrics = compute_report(equity, trading_day=trading_day)
        if metrics is None:
            log.info("daily_report_skipped",
                     reason="no trades on trading day",
                     trading_day=trading_day)
            return
        await send_report(format_telegram_report(metrics))
        log.info("daily_report_sent", trading_day=metrics.trade_date)
    except Exception:
        log.warning("daily_report_failed", exc_info=True)


async def send_weekly_report(equity: float) -> None:
    """Generate and send the weekly report to the report group chat."""
    from reporting.daily_report import compute_weekly_report, format_weekly_report
    try:
        metrics = compute_weekly_report(equity)
        if metrics is None:
            log.info("weekly_report_skipped", reason="no trades this week")
            return
        await send_report(format_weekly_report(metrics))
        log.info("weekly_report_sent")
    except Exception:
        log.warning("weekly_report_failed", exc_info=True)


async def send_month_end_report(equity: float, trading_day: str) -> None:
    """Comprehensive MONTH-END REPORT — fires after the last close
    of the last trading day of the month.

    ``trading_day`` is the UTC expiry date of the trading day that
    closes the month (e.g. ``2026-05-30``). Period membership is keyed
    off the trade row's ``trading_day`` field, so afternoon entries
    that fired the previous calendar day but settled into this month
    are correctly attributed.
    """
    from datetime import datetime
    from reporting.daily_report import _load_trades
    from reporting.period_metrics import (
        compute_period_metrics, format_period_report,
        month_window, month_label,
    )
    try:
        td_dt = datetime.strptime(trading_day, "%Y-%m-%d").date()
        m_start, m_end = month_window(td_dt)
        trades = _load_trades()
        metrics = compute_period_metrics(
            all_trades=trades,
            period_start=m_start,
            period_end=m_end,
            label=month_label(td_dt),
            current_equity=equity,
        )
        header = f"MONTH-END REPORT — {month_label(td_dt)}"
        await send_report(format_period_report(metrics, header=header))
        log.info(
            "month_end_report_sent",
            month=td_dt.strftime("%Y-%m"),
            trades=metrics.total_trades,
        )
    except Exception:
        log.warning("month_end_report_failed", exc_info=True)


async def send_year_end_report(equity: float, trading_day: str) -> None:
    """Comprehensive YEAR-END REPORT — fires after the last close
    of the last trading day of the calendar year (typically Dec 31).
    """
    from datetime import datetime
    from reporting.daily_report import _load_trades
    from reporting.period_metrics import (
        compute_period_metrics, format_period_report,
        year_window, year_label,
    )
    try:
        td_dt = datetime.strptime(trading_day, "%Y-%m-%d").date()
        y_start, y_end = year_window(td_dt)
        trades = _load_trades()
        metrics = compute_period_metrics(
            all_trades=trades,
            period_start=y_start,
            period_end=y_end,
            label=year_label(td_dt),
            current_equity=equity,
        )
        header = f"YEAR-END REPORT — {year_label(td_dt)}"
        await send_report(format_period_report(metrics, header=header))
        log.info(
            "year_end_report_sent",
            year=td_dt.year,
            trades=metrics.total_trades,
        )
    except Exception:
        log.warning("year_end_report_failed", exc_info=True)
