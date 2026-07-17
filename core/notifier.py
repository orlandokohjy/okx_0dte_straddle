"""Telegram notification helper.

Two channels:
  - Ops chat (TELEGRAM_CHAT_ID): startup, pre-flight, entry, close, errors
  - Report chat (TELEGRAM_REPORT_CHAT_ID): slim daily report only
"""
from __future__ import annotations

import structlog

import config

log = structlog.get_logger(__name__)


# Telegram's hard per-message ceiling is 4096 chars. We use a slightly
# tighter chunking limit so we have headroom for an eventual "(1/N)"
# footer if we ever decide to add chunk markers, and to absorb any HTML
# entity expansion (rare, but defensive).
_TELEGRAM_CHUNK_LIMIT = 4000


def _split_for_telegram(text: str, limit: int = _TELEGRAM_CHUNK_LIMIT) -> list[str]:
    """Split a long message into chunks ≤ ``limit`` chars.

    Telegram rejects any single sendMessage that exceeds 4096 chars
    (HTTP 400 ``message is too long``). The full daily report has
    exceeded this since the per-session breakdown grew, which is why
    every daily send has been silently dropped.

    Strategy:
      1. If the message fits, return it unchanged.
      2. Otherwise split on the strongest break (``\\n\\n``,
         section/paragraph boundary). HTML tags in this codebase are
         always closed within a single paragraph so this preserves
         valid HTML in every chunk.
      3. If a single paragraph still exceeds the limit (rare — would
         require a single section bigger than 4 KiB), fall back to
         single-newline split, then a hard character cut as a last
         resort. The hard-cut path is the only one that can produce
         malformed HTML; in practice the report formatter never builds
         a section that long.

    Returns at least one chunk. Each chunk is non-empty.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: str = ""

    def _push(s: str) -> None:
        if s:
            chunks.append(s)

    def _add(piece: str, separator: str) -> None:
        nonlocal current
        if not piece:
            return
        candidate = current + separator + piece if current else piece
        if len(candidate) <= limit:
            current = candidate
        else:
            _push(current)
            current = piece

    for paragraph in text.split("\n\n"):
        if len(paragraph) <= limit:
            _add(paragraph, "\n\n")
            continue
        # paragraph is itself > limit — split by single newline.
        _push(current)
        current = ""
        for line in paragraph.split("\n"):
            if len(line) <= limit:
                _add(line, "\n")
                continue
            # line is > limit — hard character cut as last resort.
            _push(current)
            current = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])

    _push(current)
    return chunks


async def _send_to(bot_token: str, chat_id: str, text: str) -> None:
    """Send a Telegram message and SURFACE any failure.

    aiohttp does NOT raise on HTTP 4xx/5xx by default; the prior version
    of this function ``await``-ed the POST and threw away the response,
    which silently swallowed Telegram API rejections (wrong chat id,
    bot kicked from group, message length > 4096, malformed HTML, etc.).
    The caller would log e.g. ``daily_report_sent`` even when nothing
    actually arrived in the chat.

    Now we:
      * Chunk the message at paragraph boundaries so single sends never
        exceed Telegram's 4096-char hard limit.
      * Read the response body and log ``telegram_send_rejected`` with
        the HTTP status + Telegram error description on any non-200.
    """
    if not bot_token or not chat_id:
        log.debug("telegram_disabled", chat_id=chat_id, msg=text[:80])
        return
    chunks = _split_for_telegram(text)
    if len(chunks) > 1:
        log.info(
            "telegram_message_chunked",
            chat_id=chat_id,
            total_len=len(text),
            num_chunks=len(chunks),
        )
    try:
        import aiohttp
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        async with aiohttp.ClientSession() as session:
            for idx, chunk in enumerate(chunks, start=1):
                async with session.post(url, json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                }) as response:
                    body = await response.text()
                    if response.status != 200:
                        # Telegram returns JSON like:
                        # {"ok":false,"error_code":400,
                        #  "description":"Bad Request: chat not found"}
                        log.warning(
                            "telegram_send_rejected",
                            chat_id=chat_id,
                            http_status=response.status,
                            body=body[:500],
                            chunk_index=idx,
                            chunk_total=len(chunks),
                            msg_len=len(chunk),
                            msg_preview=chunk[:200],
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


def _fmt_signed_usd(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}${v:,.2f}"


def _format_close_message(
    pnl: float,
    session_label: str = "",
    straddle: object | None = None,
    equity_before: float | None = None,
    equity_after: float | None = None,
) -> str:
    """Render the SESSION CLOSE message body.

    Falls back to the legacy two-line format (header + Net P&L) if the
    straddle reference isn't supplied — keeps the function safe for
    legacy call-sites and for the "nothing was open" path that should
    never reach the rich branch in practice.
    """
    header = "<b>SESSION CLOSE</b>"
    if session_label:
        header = f"<b>SESSION CLOSE [{session_label}]</b>"

    if straddle is None:
        return f"{header}\nNet P&amp;L: {_fmt_signed_usd(pnl)}\n"

    # Pull what we need off the Straddle dataclass without importing it
    # (avoids a circular import). Every attribute used here is set on
    # the straddle by core.portfolio.close_straddle before notify_close
    # is invoked.
    s = straddle
    is_um = (getattr(s, "family", "") or "CM").upper() == "UM"
    quote_unit = "USD" if is_um else "BTC"

    qty = float(getattr(s, "qty_per_leg", 0.0))
    num = int(getattr(s, "num_straddles", 1))
    strike = float(getattr(s, "strike", 0.0))
    entry_spot = float(getattr(s, "entry_spot_price", 0.0) or 0.0)
    exit_spot = float(getattr(s, "exit_spot_price", 0.0) or 0.0) or entry_spot
    spot_delta = exit_spot - entry_spot

    call_leg = getattr(s, "call_leg", None)
    put_leg = getattr(s, "put_leg", None)
    call_inst = getattr(call_leg, "instrument", "?") if call_leg else "?"
    put_inst = getattr(put_leg, "instrument", "?") if put_leg else "?"

    entry_call = float(getattr(s, "entry_call_price", 0.0) or 0.0)
    entry_put = float(getattr(s, "entry_put_price", 0.0) or 0.0)
    exit_call = float(getattr(s, "exit_call_price", 0.0) or 0.0)
    exit_put = float(getattr(s, "exit_put_price", 0.0) or 0.0)

    # Per-leg USD value at entry / exit. For CM, native_premium × spot.
    # For UM, native_premium IS USD.
    if is_um:
        call_entry_usd = entry_call * qty * num
        call_exit_usd = exit_call * qty * num
        put_entry_usd = entry_put * qty * num
        put_exit_usd = exit_put * qty * num
    else:
        call_entry_usd = entry_call * entry_spot * qty * num
        call_exit_usd = exit_call * exit_spot * qty * num
        put_entry_usd = entry_put * entry_spot * qty * num
        put_exit_usd = exit_put * exit_spot * qty * num

    call_pnl = call_exit_usd - call_entry_usd
    put_pnl = put_exit_usd - put_entry_usd

    gross = float(getattr(s, "gross_pnl", None) or (call_pnl + put_pnl))
    fees = float(getattr(s, "fees", None) or 0.0)
    net = float(getattr(s, "pnl", pnl) or pnl)

    # Native-quote formatting: BTC needs 4 dp, USD 2 dp. Handles both.
    def _fmt_native(v: float) -> str:
        return f"{v:.4f} {quote_unit}" if not is_um else f"${v:,.2f}"

    lines: list[str] = [header]
    lines.append(f"ID: {getattr(s, 'id', '?')}")

    if strike > 0:
        if entry_spot > 0 and abs(spot_delta) > 0.01:
            lines.append(
                f"Strike: ${strike:,.0f}  |  "
                f"Spot: ${entry_spot:,.0f} → ${exit_spot:,.0f} "
                f"({_fmt_signed_usd(spot_delta)})"
            )
        elif entry_spot > 0:
            lines.append(
                f"Strike: ${strike:,.0f}  |  Spot: ${entry_spot:,.0f}"
            )
        else:
            lines.append(f"Strike: ${strike:,.0f}")

    qty_line = f"Qty: {qty:.4f} BTC/leg"
    if num != 1:
        qty_line += f" × {num} straddles"
    lines.append(qty_line)
    lines.append("")  # blank line before legs

    # ── Call leg ──
    lines.append(f"<b>Call</b> {call_inst}")
    lines.append(
        f"  Entry: {_fmt_native(entry_call)} "
        f"({_fmt_signed_usd(call_entry_usd).lstrip('+')})"
    )
    lines.append(
        f"  Exit:  {_fmt_native(exit_call)} "
        f"({_fmt_signed_usd(call_exit_usd).lstrip('+')})"
    )
    lines.append(f"  Leg P&amp;L: {_fmt_signed_usd(call_pnl)}")

    # ── Put leg ──
    lines.append(f"<b>Put</b> {put_inst}")
    lines.append(
        f"  Entry: {_fmt_native(entry_put)} "
        f"({_fmt_signed_usd(put_entry_usd).lstrip('+')})"
    )
    lines.append(
        f"  Exit:  {_fmt_native(exit_put)} "
        f"({_fmt_signed_usd(put_exit_usd).lstrip('+')})"
    )
    lines.append(f"  Leg P&amp;L: {_fmt_signed_usd(put_pnl)}")
    lines.append("")

    # ── Wings (short overlay) — rendered only when present ──
    # A short wing profits when we BUY it back below the credit received,
    # so leg P&L = credit_in − debit_out (opposite sign of a long leg).
    call_wing_leg = getattr(s, "call_wing_leg", None)
    put_wing_leg = getattr(s, "put_wing_leg", None)
    has_call_wing = call_wing_leg is not None
    has_put_wing = put_wing_leg is not None
    has_wings = has_call_wing or has_put_wing
    if has_wings:
        f_entry = 1.0 if is_um else entry_spot
        f_exit = 1.0 if is_um else exit_spot
        if has_call_wing:
            e_cw = float(getattr(s, "entry_call_wing_price", 0.0) or 0.0)
            x_cw = float(getattr(s, "exit_call_wing_price", 0.0) or 0.0)
            cw_strike = float(getattr(s, "call_wing_strike", 0.0) or 0.0)
            cw_credit = e_cw * f_entry * qty * num
            cw_debit = x_cw * f_exit * qty * num
            cw_pnl = cw_credit - cw_debit
            lines.append(
                f"<b>Call wing (short)</b> "
                f"{getattr(call_wing_leg, 'instrument', '?')}  "
                f"${cw_strike:,.0f}")
            lines.append(
                f"  Sold:   {_fmt_native(e_cw)} "
                f"({_fmt_signed_usd(cw_credit).lstrip('+')})")
            lines.append(
                f"  Bought: {_fmt_native(x_cw)} "
                f"({_fmt_signed_usd(cw_debit).lstrip('+')})")
            lines.append(f"  Leg P&amp;L: {_fmt_signed_usd(cw_pnl)}")
        if has_put_wing:
            e_pw = float(getattr(s, "entry_put_wing_price", 0.0) or 0.0)
            x_pw = float(getattr(s, "exit_put_wing_price", 0.0) or 0.0)
            pw_strike = float(getattr(s, "put_wing_strike", 0.0) or 0.0)
            pw_credit = e_pw * f_entry * qty * num
            pw_debit = x_pw * f_exit * qty * num
            pw_pnl = pw_credit - pw_debit
            lines.append(
                f"<b>Put wing (short)</b> "
                f"{getattr(put_wing_leg, 'instrument', '?')}  "
                f"${pw_strike:,.0f}")
            lines.append(
                f"  Sold:   {_fmt_native(e_pw)} "
                f"({_fmt_signed_usd(pw_credit).lstrip('+')})")
            lines.append(
                f"  Bought: {_fmt_native(x_pw)} "
                f"({_fmt_signed_usd(pw_debit).lstrip('+')})")
            lines.append(f"  Leg P&amp;L: {_fmt_signed_usd(pw_pnl)}")
        lines.append("")

    # ── P&L breakdown ──
    # ``fees`` is signed: positive = maker rebate received (credit),
    # negative = fee paid (cost). Label and sign accordingly.
    gross_note = "(call + put + wings)" if has_wings else "(call + put)"
    lines.append(f"<b>Gross P&amp;L:</b> {_fmt_signed_usd(gross)}  "
                 f"<i>{gross_note}</i>")
    if fees >= 0:
        lines.append(f"<b>Rebate:</b>    +${fees:,.2f}")
    else:
        lines.append(f"<b>Fees:</b>      -${abs(fees):,.2f}")
    lines.append(f"<b>Net P&amp;L:</b>   {_fmt_signed_usd(net)}")

    if equity_before is not None and equity_after is not None:
        lines.append("")
        lines.append(
            f"Equity: ${equity_before:,.2f} → ${equity_after:,.2f}"
        )

    return "\n".join(lines)


async def notify_close(
    pnl: float, exit_reason: str, session_label: str = "",
    straddle: object | None = None,
    equity_before: float | None = None,
    equity_after: float | None = None,
) -> None:
    """SESSION CLOSE message.

    Backward-compatible: ``straddle`` / ``equity_before`` / ``equity_after``
    are optional. When supplied, the message renders entry/exit prices
    per leg, gross P&L, fees, net P&L, and the equity delta. When not,
    falls back to the legacy two-line format so existing call-sites
    don't crash.
    """
    body = _format_close_message(
        pnl,
        session_label=session_label,
        straddle=straddle,
        equity_before=equity_before,
        equity_after=equity_after,
    )
    await send(body)


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


async def send_weekend_recap(equity: float) -> None:
    """Generate and send the weekend recap to the report group chat.

    Fires after the LAST close of the weekend (utc_2230 Sun → Mon
    00:00 UTC) so weekend-strategy trades are reported separately from
    the Mon-Fri weekly. Skipped silently if no weekend trades exist
    in the most recent Sat-Sun window (e.g. operator disabled both
    weekend sessions).
    """
    from reporting.daily_report import (
        compute_weekend_recap, format_weekend_recap,
    )
    try:
        metrics = compute_weekend_recap(equity)
        if metrics is None:
            log.info(
                "weekend_recap_skipped",
                reason="no weekend trades this window",
            )
            return
        await send_report(format_weekend_recap(metrics))
        log.info(
            "weekend_recap_sent",
            window=metrics.trade_date,
            trades=metrics.total_trades,
        )
    except Exception:
        log.warning("weekend_recap_failed", exc_info=True)


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
