"""
Period (MTD / YTD) performance aggregates.

Source of truth for everything is ``trade_log.csv`` — each row already
carries P&L (gross + net), entry/exit spot (for USD volume), and full
execution-quality metrics. We never need a separate persistence layer;
all monthly/yearly aggregates are computed from the trade log on demand.

The same primitives power three Telegram surfaces:
    1. Brief MTD/YTD blocks appended to every DAILY REPORT
       (``format_brief_period_block``).
    2. Comprehensive MONTH-END REPORT fired after the last trading-day
       close of each month (``format_period_report``).
    3. Comprehensive YEAR-END REPORT fired after the last trading-day
       close of December.

Trading day = 0DTE expiry UTC date (08:00 UTC cutoff). Period membership
is keyed off the trade row's ``trading_day`` field, NOT the calendar
``date`` of entry — so afternoon entries that fire late in the previous
calendar month but settle on the 1st of the new month are correctly
attributed to the new month.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import config


# ─────────────────────────── data classes ────────────────────────────

@dataclass
class PeriodMetrics:
    label: str               # "MTD (2026-05)", "YTD (2026)", "May 2026", "2026"
    period_start: str        # "2026-05-01"
    period_end: str          # "2026-05-15"

    # Equity / P&L
    starting_equity: float
    ending_equity: float
    pnl_wallet: float        # ending - starting   ← truth from OKX
    pnl_ledger: float        # sum of net_pnl in period
    return_pct: float        # pnl_wallet / starting_equity
    drift: float             # pnl_wallet - pnl_ledger

    # Trade stats
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    best_trade: float
    worst_trade: float
    max_win_streak: int
    max_loss_streak: int
    expectancy: float

    # Volume
    volume_btc: float        # opens + closes (matches OKX VIP-tier reporting)
    volume_usd: float
    opened_btc: float
    closed_btc: float
    opened_usd: float
    closed_usd: float

    # Execution
    total_saved_vs_taker_usd: float
    total_attempts: int
    total_legs: int
    avg_fill_seconds: float

    # Best/worst trading days
    best_day_pnl: float = 0.0
    best_day_date: str = ""
    worst_day_pnl: float = 0.0
    worst_day_date: str = ""


# ─────────────────────────── computation ─────────────────────────────

def _trade_in_period(trading_day: str, period_start: date, period_end: date) -> bool:
    if not trading_day:
        return False
    try:
        td = datetime.strptime(trading_day[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return period_start <= td <= period_end


def _legs_for(t) -> list:
    """Return the list of present execution-quality leg blocks for a trade."""
    out = []
    for e in (
        getattr(t, "put_entry_exec", None),
        getattr(t, "call_entry_exec", None),
        getattr(t, "call_exit_exec", None),
        getattr(t, "put_exit_exec", None),
    ):
        if e is not None and getattr(e, "duration_sec", 0.0) > 0:
            out.append(e)
    return out


def _streaks(pnls: list[float]) -> tuple[int, int]:
    if not pnls:
        return 0, 0
    streak = 0
    max_w = 0
    max_l = 0
    for p in pnls:
        if p >= 0:
            streak = streak + 1 if streak > 0 else 1
        else:
            streak = streak - 1 if streak < 0 else -1
        if streak > 0:
            max_w = max(max_w, streak)
        else:
            max_l = min(max_l, streak)
    return max_w, abs(max_l)


def compute_period_metrics(
    *,
    all_trades: list,
    period_start: date,
    period_end: date,
    label: str,
    current_equity: float,
) -> PeriodMetrics:
    """Aggregate all trades whose ``trading_day`` falls in the inclusive window.

    ``all_trades`` is the FULL list returned by ``daily_report._load_trades``
    so the caller can compute multiple periods (MTD + YTD) from a single
    CSV pass without re-loading.

    Wallet-based P&L uses ``capital_before`` of the first trade in the
    period as the starting equity; current_equity is the period-end
    snapshot (i.e. live wallet at report time).
    """
    trades = [
        t for t in all_trades
        if _trade_in_period(t.trading_day, period_start, period_end)
    ]
    trades.sort(key=lambda t: (t.trading_day, t.entry_time or ""))

    if trades:
        starting_equity = trades[0].capital_before or 0.0
    else:
        starting_equity = current_equity

    pnls = [t.net_pnl for t in trades]
    pnl_ledger = sum(pnls)
    pnl_wallet = current_equity - starting_equity
    return_pct = (
        pnl_wallet / starting_equity if starting_equity > 0 else 0.0
    )
    drift = pnl_wallet - pnl_ledger

    wins_list = [p for p in pnls if p >= 0]
    losses_list = [p for p in pnls if p < 0]
    n_wins, n_losses = len(wins_list), len(losses_list)
    total = len(trades)
    win_rate = n_wins / total if total > 0 else 0.0
    avg_win = sum(wins_list) / n_wins if n_wins > 0 else 0.0
    avg_loss = sum(losses_list) / n_losses if n_losses > 0 else 0.0
    gross_w = sum(wins_list)
    gross_l = abs(sum(losses_list))
    profit_factor = gross_w / gross_l if gross_l > 0 else float("inf")
    expectancy = pnl_ledger / total if total > 0 else 0.0
    max_win_streak, max_loss_streak = _streaks(pnls)

    # Volume — round-trip (open + close) for both legs of every straddle.
    opened_btc = sum(t.qty_per_leg * t.num_straddles * 2 for t in trades)
    closed_btc = opened_btc
    volume_btc = opened_btc + closed_btc
    opened_usd = sum(
        2 * t.qty_per_leg * t.num_straddles * (t.entry_spot or 0.0)
        for t in trades
    )
    closed_usd = sum(
        2 * t.qty_per_leg * t.num_straddles * (t.exit_spot or 0.0)
        for t in trades
    )
    volume_usd = opened_usd + closed_usd

    # Execution quality — aggregate across every recorded leg.
    legs = []
    for t in trades:
        legs.extend(_legs_for(t))
    total_saved = sum(e.saved_vs_taker_usd for e in legs)
    total_attempts = sum(e.attempts for e in legs)
    total_legs = len(legs)
    avg_fill = (
        sum(e.duration_sec for e in legs) / total_legs
        if total_legs > 0 else 0.0
    )

    # Best / worst trading day (sums across all sessions in the same day).
    by_day: dict[str, float] = {}
    for t in trades:
        by_day[t.trading_day] = by_day.get(t.trading_day, 0.0) + t.net_pnl
    if by_day:
        best_d = max(by_day.items(), key=lambda kv: kv[1])
        worst_d = min(by_day.items(), key=lambda kv: kv[1])
        best_day_pnl, best_day_date = best_d[1], best_d[0]
        worst_day_pnl, worst_day_date = worst_d[1], worst_d[0]
    else:
        best_day_pnl = best_day_date = ""  # type: ignore[assignment]
        worst_day_pnl = worst_day_date = ""  # type: ignore[assignment]
        best_day_pnl, worst_day_pnl = 0.0, 0.0
        best_day_date, worst_day_date = "", ""

    return PeriodMetrics(
        label=label,
        period_start=period_start.strftime("%Y-%m-%d"),
        period_end=period_end.strftime("%Y-%m-%d"),
        starting_equity=starting_equity,
        ending_equity=current_equity,
        pnl_wallet=pnl_wallet,
        pnl_ledger=pnl_ledger,
        return_pct=return_pct,
        drift=drift,
        total_trades=total,
        wins=n_wins,
        losses=n_losses,
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        profit_factor=profit_factor,
        best_trade=max(pnls) if pnls else 0.0,
        worst_trade=min(pnls) if pnls else 0.0,
        max_win_streak=max_win_streak,
        max_loss_streak=max_loss_streak,
        expectancy=expectancy,
        volume_btc=volume_btc,
        volume_usd=volume_usd,
        opened_btc=opened_btc,
        closed_btc=closed_btc,
        opened_usd=opened_usd,
        closed_usd=closed_usd,
        total_saved_vs_taker_usd=total_saved,
        total_attempts=total_attempts,
        total_legs=total_legs,
        avg_fill_seconds=avg_fill,
        best_day_pnl=best_day_pnl,
        best_day_date=best_day_date,
        worst_day_pnl=worst_day_pnl,
        worst_day_date=worst_day_date,
    )


# ─────────────────────────── period windows ──────────────────────────

def month_window(today: date) -> tuple[date, date]:
    """[first day of month, last day of month] for the month of `today`."""
    first = today.replace(day=1)
    if today.month == 12:
        next_first = today.replace(year=today.year + 1, month=1, day=1)
    else:
        next_first = today.replace(month=today.month + 1, day=1)
    last = next_first - timedelta(days=1)
    return first, last


def year_window(today: date) -> tuple[date, date]:
    return today.replace(month=1, day=1), today.replace(month=12, day=31)


def _next_scheduled_trading_day(
    after_trading_day: date, max_offset_days: int = 60,
) -> Optional[date]:
    """Find the next trading_day strictly greater than `after_trading_day`
    that any configured ``config.SESSIONS`` would produce.

    Walks forward day by day; for each calendar day on which a session
    is configured to fire, computes that session's ``trading_day_for``
    and returns the smallest trading_day still > `after_trading_day`.
    """
    candidates: list[date] = []
    for offset in range(0, max_offset_days + 1):
        cal_day = after_trading_day + timedelta(days=offset)
        weekday = cal_day.weekday()
        for s in config.SESSIONS:
            if weekday in s.weekdays:
                session_dt = datetime.combine(cal_day, s.entry_utc)
                td = config.trading_day_for(session_dt)
                if td > after_trading_day:
                    candidates.append(td)
        if candidates:
            return min(candidates)
    return None


def is_last_trading_day_of_month(today_trading_day: date) -> bool:
    """True iff no future scheduled trading_day lands in the same month/year."""
    nxt = _next_scheduled_trading_day(today_trading_day)
    if nxt is None:
        return True
    return (nxt.year, nxt.month) != (today_trading_day.year, today_trading_day.month)


def is_last_trading_day_of_year(today_trading_day: date) -> bool:
    nxt = _next_scheduled_trading_day(today_trading_day)
    if nxt is None:
        return True
    return nxt.year != today_trading_day.year


# ─────────────────────────── formatters ──────────────────────────────

def _usd_bracket(usd: float) -> str:
    return f" (${usd:,.0f})" if usd > 0 else ""


def _pnl_sign(x: float) -> str:
    return "+" if x >= 0 else ""


def format_brief_period_block(m: PeriodMetrics) -> list[str]:
    """Rich 7-line summary for inline use in the daily report.

    Suppresses the ``Maker P&L (vs initial taker)`` line if no leg has
    recorded the USD-correct value yet (e.g. backfill before the
    saved-vs-taker fix).
    """
    if m.total_trades == 0:
        return [
            f"<b>{m.label}</b>",
            "  No trades in this period yet.",
        ]
    sw, sl = _pnl_sign(m.pnl_wallet), _pnl_sign(m.pnl_ledger)
    streak_line = (
        f"  Streaks: {m.max_win_streak}W max / {m.max_loss_streak}L max"
    )
    saved_line = (
        f"  Maker P&L (vs initial taker): "
        f"${m.total_saved_vs_taker_usd:+,.2f}"
        if m.total_legs > 0 else ""
    )
    bw_line = (
        f"  Best day: {_pnl_sign(m.best_day_pnl)}${m.best_day_pnl:,.2f} "
        f"({m.best_day_date}) | "
        f"Worst: {_pnl_sign(m.worst_day_pnl)}${m.worst_day_pnl:,.2f} "
        f"({m.worst_day_date})"
        if m.total_trades > 1 else ""
    )
    lines = [
        f"<b>{m.label}</b>",
        f"  P&L: {sw}${m.pnl_wallet:,.2f} ({m.return_pct:+.2%}) wallet"
        f"  |  {sl}${m.pnl_ledger:,.2f} ledger",
        f"  Trades: {m.total_trades} ({m.wins}W/{m.losses}L, "
        f"{m.win_rate:.0%})",
        f"  Volume: {m.volume_btc:.4f} BTC{_usd_bracket(m.volume_usd)}",
    ]
    if saved_line:
        lines.append(saved_line)
    if bw_line:
        lines.append(bw_line)
    lines.append(streak_line)
    return lines


def format_period_report(m: PeriodMetrics, *, header: str) -> str:
    """Comprehensive standalone MTD / YTD report (Telegram HTML).

    Header example: ``MONTH-END REPORT — May 2026`` or
    ``YEAR-END REPORT — 2026``.
    """
    sw, sl = _pnl_sign(m.pnl_wallet), _pnl_sign(m.pnl_ledger)

    activity = []
    if m.total_trades > 0:
        activity = [
            f"  Trades: {m.total_trades}  "
            f"({m.wins}W / {m.losses}L, {m.win_rate:.1%})",
            f"  Avg win:  ${m.avg_win:+,.2f}",
            f"  Avg loss: ${m.avg_loss:+,.2f}",
            f"  Profit factor: {m.profit_factor:.2f}",
            f"  Best trade:  ${m.best_trade:+,.2f}",
            f"  Worst trade: ${m.worst_trade:+,.2f}",
            f"  Best day:  {_pnl_sign(m.best_day_pnl)}"
            f"${m.best_day_pnl:,.2f}  ({m.best_day_date})",
            f"  Worst day: {_pnl_sign(m.worst_day_pnl)}"
            f"${m.worst_day_pnl:,.2f}  ({m.worst_day_date})",
            f"  Streaks: {m.max_win_streak}W max / "
            f"{m.max_loss_streak}L max",
            f"  Expectancy: ${m.expectancy:+,.2f} per trade",
        ]
    else:
        activity = ["  No trades in this period."]

    exec_block = []
    if m.total_legs > 0:
        exec_block = [
            "",
            "<b>Execution</b>",
            f"  Total maker P&L (vs initial taker): "
            f"${m.total_saved_vs_taker_usd:+,.2f}",
            f"  Avg time to fill: {m.avg_fill_seconds:.1f}s",
            f"  Attempts: {m.total_attempts} across {m.total_legs} legs",
        ]

    lines = [
        f"<b>{header}</b>",
        f"<i>{m.period_start} → {m.period_end}</i>",
        "",
        "<b>Equity</b>",
        f"  Starting equity: ${m.starting_equity:,.2f}",
        f"  Ending equity:   ${m.ending_equity:,.2f}",
        f"  Wallet P&L:      {sw}${m.pnl_wallet:,.2f} "
        f"({m.return_pct:+.2%})",
        f"  Trade ledger:    {sl}${m.pnl_ledger:,.2f}",
        f"  Drift:           ${m.drift:+,.2f}  "
        f"<i>(MTM/settlement/fees)</i>",
        "",
        "<b>Activity</b>",
        *activity,
        "",
        "<b>Volume</b>",
        f"  Opened: {m.opened_btc:.4f} BTC{_usd_bracket(m.opened_usd)}",
        f"  Closed: {m.closed_btc:.4f} BTC{_usd_bracket(m.closed_usd)}",
        f"  <b>Total traded: {m.volume_btc:.4f} BTC"
        f"{_usd_bracket(m.volume_usd)}</b>  (opens + closes)",
        *exec_block,
    ]
    return "\n".join(lines)


def month_label(d: date) -> str:
    return d.strftime("%B %Y")  # "May 2026"


def year_label(d: date) -> str:
    return d.strftime("%Y")     # "2026"
