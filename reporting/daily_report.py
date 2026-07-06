"""
Daily and weekly performance reports for the OKX straddle algo.

Reads the trade log CSV, computes quant metrics over the full history
(daily) or the current ISO week (weekly), and formats Telegram-ready
HTML reports.

Trading day grouping
--------------------
OKX BTC 0DTE options expire daily at 08:00 UTC. A "trading day" is the
calendar UTC date of that expiry. Sessions firing AFTER the cutoff
(e.g. afternoon at 13:30 UTC) trade NEXT-day-expiry options, so their
trade_log row's ``date`` (entry UTC date) is one day BEFORE the trading
day. Sessions firing BEFORE the cutoff (e.g. morning at 01:00 UTC)
trade SAME-day-expiry options.

Reports group by trading_day so afternoon (Mon 13:30 UTC) + morning
(Tue 01:00 UTC) roll into ONE Tuesday report.
"""
from __future__ import annotations

import csv
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import structlog

import config
from core import family as option_family

log = structlog.get_logger(__name__)

TRADING_DAYS_PER_YEAR = 252
RISK_FREE_RATE = 0.0


@dataclass
class ExecutionMetrics:
    """Per-leg fill quality for one trade."""
    duration_sec: float = 0.0
    attempts: int = 0
    ref_mark: float = 0.0
    ref_quote: float = 0.0   # ask for entries, bid for exits
    slippage_vs_mark_pct: float = 0.0
    saved_vs_taker_usd: float = 0.0


@dataclass
class TradeRow:
    date: str
    net_pnl: float
    capital_before: float
    capital_after: float
    strike: float
    call_premium_entry: float
    call_premium_exit: float
    put_premium_entry: float
    put_premium_exit: float
    num_straddles: int
    straddle_cost: float
    exit_reason: str
    total_capital_used: float = 0.0
    entry_spot: float = 0.0
    exit_spot: float = 0.0
    # Multi-session metadata: empty string means a legacy single-session
    # row that pre-dates the multi-session refactor.
    session: str = ""
    qty_per_leg: float = 0.0
    # Option family at the time of the trade. "CM" for legacy rows
    # (pre-2026-05-15) and any row written before the family
    # abstraction landed. "UM" for trades on the linear family.
    family: str = "CM"
    # Trading day = expiry date in UTC (08:00 UTC cutoff). Computed at
    # load time from the entry_time so reports can group afternoon
    # (Mon 13:30 UTC) + morning (Tue 01:00 UTC) into ONE Tuesday row.
    trading_day: str = ""
    entry_time: str = ""
    call_entry_exec: ExecutionMetrics = None
    put_entry_exec: ExecutionMetrics = None
    call_exit_exec: ExecutionMetrics = None
    put_exit_exec: ExecutionMetrics = None


@dataclass
class DailyMetrics:
    trade_date: str
    trade_pnl: float
    trade_return_pct: float
    strike: float
    num_straddles: int

    equity: float
    initial_capital: float

    total_trades: int
    total_pnl: float
    cumulative_return_pct: float

    wins: int
    losses: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    best_trade: float
    worst_trade: float

    current_streak: int
    max_win_streak: int
    max_loss_streak: int

    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float

    max_drawdown_pct: float
    current_drawdown_pct: float
    high_water_mark: float

    expectancy: float
    expectancy_ratio: float

    daily_vol: float
    annualised_vol: float

    call_premium_entry: float
    call_premium_exit: float
    put_premium_entry: float
    put_premium_exit: float
    total_capital_used: float

    # Equity snapshots (starting = before today's trade, equity = after)
    starting_equity: float = 0.0

    # Spot price snapshots — useful for verifying strike selection
    entry_spot: float = 0.0
    exit_spot: float = 0.0

    # Execution-quality (last trade of the day; legacy single-session view)
    call_entry_exec: Optional[ExecutionMetrics] = None
    put_entry_exec: Optional[ExecutionMetrics] = None
    call_exit_exec: Optional[ExecutionMetrics] = None
    put_exit_exec: Optional[ExecutionMetrics] = None
    call_instrument: str = ""
    put_instrument: str = ""

    # Multi-session: every trade that ran on the report date so the
    # formatter can render a per-session breakdown alongside the
    # aggregate row.
    today_trades: list = None
    qty_per_leg: float = 0.0

    # Inception (the very first trade in the trade log) — surfaces
    # what the cumulative-return % is anchored against so the operator
    # can reconcile the line with their actual deposit.
    inception_equity: float = 0.0
    inception_date: str = ""

    # Period aggregates for the brief MTD / YTD blocks rendered
    # alongside the cumulative line. Computed from the same trade-log
    # pass that built the daily/weekly metrics so we never re-load the
    # CSV. Optional so legacy callers that build DailyMetrics by hand
    # still work.
    mtd: object = None  # PeriodMetrics
    ytd: object = None  # PeriodMetrics


def _f(row: dict, key: str, default: float = 0.0) -> float:
    """Best-effort float parse — returns default if blank or invalid."""
    raw = row.get(key, "")
    if raw == "" or raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _i(row: dict, key: str, default: int = 0) -> int:
    raw = row.get(key, "")
    if raw == "" or raw is None:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _exec_from_row(
    row: dict, prefix: str, quote_key: str,
) -> ExecutionMetrics:
    """Extract one leg/side's execution metrics from a CSV row."""
    return ExecutionMetrics(
        duration_sec=_f(row, f"{prefix}_duration_sec"),
        attempts=_i(row, f"{prefix}_attempts"),
        ref_mark=_f(row, f"{prefix}_ref_mark"),
        ref_quote=_f(row, f"{prefix}_{quote_key}"),
        slippage_vs_mark_pct=_f(row, f"{prefix}_slippage_vs_mark_pct"),
        saved_vs_taker_usd=_f(row, f"{prefix}_saved_vs_taker_usd"),
    )


def _trading_day_from_entry_time(entry_time_iso: str, fallback_date: str) -> str:
    """Compute trading_day (= expiry UTC date) from an entry timestamp.

    Sessions firing at/after EXPIRY_CUTOFF_UTC (08:00) trade next-day
    expiry → trading_day = entry_date + 1. Sessions firing before
    08:00 trade same-day expiry → trading_day = entry_date.

    Falls back to ``fallback_date`` if the entry_time is missing or
    unparseable so legacy rows still group sensibly.
    """
    if not entry_time_iso:
        return fallback_date
    try:
        dt = datetime.fromisoformat(entry_time_iso.replace("Z", "+00:00"))
    except Exception:
        return fallback_date
    cutoff = config.EXPIRY_CUTOFF_UTC
    if (dt.hour, dt.minute) >= (cutoff.hour, cutoff.minute):
        return (dt + timedelta(days=1)).date().strftime("%Y-%m-%d")
    return dt.date().strftime("%Y-%m-%d")


def _session_chronological_key(t: "TradeRow") -> tuple:
    """Order TradeRows in trading-day-chronological order.

    Within a trading day the afternoon entry (previous calendar day)
    comes BEFORE the morning entry (same calendar day). We sort by
    entry_time when available and fall back to a session-name hint.
    """
    # Chronological order within a trading day. Both canonical
    # ("utc_HHMM") and legacy ("afternoon"/"morning") names map onto
    # the same slot for backward-compatible historical rows.
    name_order = {
        # WEEKDAY trading days: utc_0900 → utc_1330 → utc_2330 → utc_0100
        "utc_0900":  0,
        "utc_1330":  1,
        "utc_2330":  2,
        "utc_0100":  3,
        # WEEKEND trading days (Sun, Mon): only utc_1430 + utc_2230 fire,
        # so 0+1 are reused — they never collide with weekday rows on
        # the same trading_day because weekday/weekend trading_days are
        # disjoint by construction.
        "utc_1430":  0,
        "utc_2230":  1,
        "afternoon": 1,  # legacy → utc_1330
        "morning":   3,  # legacy → utc_0100
    }
    return (t.entry_time or "", name_order.get(t.session, 99))


def _inception_equity(trades: list["TradeRow"]) -> float:
    """Return the actual starting equity (i.e. equity just before the
    first-ever trade), falling back to INITIAL_CAPITAL_USD if no history.

    Why: config.INITIAL_CAPITAL_USD is a default placeholder — the user's
    real starting balance can differ (e.g., the OKX live-equity sync on
    first boot returned $7,786 vs the $8,000 default). Anchoring
    cumulative return on this true starting equity gives an honest
    since-inception number; anchoring on the placeholder bakes a phantom
    return into day-1 reporting.
    """
    if trades and trades[0].capital_before > 0:
        return trades[0].capital_before
    return config.INITIAL_CAPITAL_USD


def _load_trades(
    *,
    family_filter: Optional[str] = None,
    include_all_families: bool = False,
) -> list[TradeRow]:
    """Load all trade rows from ``trade_log.csv``.

    By default, returns only rows that match the **active option family**
    (``core.family.label()``) — so a daily report rendered while running
    UM never accidentally mixes in the historical CM rows. Pre-2026-05-15
    rows are tagged ``CM`` by the migration step in
    ``Portfolio._migrate_trade_log``.

    Args:
        family_filter: explicit family to keep ("CM" / "UM"). Defaults to
            the currently active family. Has no effect when
            ``include_all_families=True``.
        include_all_families: load every row regardless of family. Used
            by ad-hoc / cross-family analysis tools and by weekly reports
            that want to show "lifetime" volume across both families.
    """
    path = config.TRADE_LOG_FILE
    if not os.path.exists(path):
        return []
    target_family = (
        None
        if include_all_families
        else (family_filter or option_family.label()).upper()
    )
    trades: list[TradeRow] = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                # qty_per_leg defaults to legacy QTY_PER_LEG for any
                # row that pre-dates the multi-session refactor — those
                # rows have a blank cell in the new column.
                qpl = _f(
                    row, "qty_per_leg", default=config.QTY_PER_LEG,
                )
                if qpl <= 0:
                    qpl = config.QTY_PER_LEG
                entry_time = row.get("entry_time", "") or ""
                date_str = row["date"]
                trading_day = _trading_day_from_entry_time(
                    entry_time, fallback_date=date_str,
                )
                row_family = (row.get("family") or "CM").strip().upper() \
                    or "CM"
                if target_family is not None and row_family != target_family:
                    continue
                trades.append(TradeRow(
                    date=date_str,
                    net_pnl=float(row["net_pnl"]),
                    capital_before=float(row["capital_before"]),
                    capital_after=float(row["capital_after"]),
                    strike=float(row.get("strike", 0)),
                    call_premium_entry=float(row.get("call_premium_entry", 0)),
                    call_premium_exit=float(row.get("call_premium_exit", 0)),
                    put_premium_entry=float(row.get("put_premium_entry", 0)),
                    put_premium_exit=float(row.get("put_premium_exit", 0)),
                    num_straddles=int(row["num_straddles"]),
                    straddle_cost=float(row["straddle_cost"]),
                    exit_reason=row.get("exit_reason", ""),
                    total_capital_used=float(row.get("total_capital_used", 0)),
                    entry_spot=_f(row, "entry_spot"),
                    exit_spot=_f(row, "exit_spot"),
                    session=row.get("session", "") or "",
                    qty_per_leg=qpl,
                    family=row_family,
                    trading_day=trading_day,
                    entry_time=entry_time,
                    call_entry_exec=_exec_from_row(
                        row, "call_entry", "ref_ask",
                    ),
                    put_entry_exec=_exec_from_row(
                        row, "put_entry", "ref_ask",
                    ),
                    call_exit_exec=_exec_from_row(
                        row, "call_exit", "ref_bid",
                    ),
                    put_exit_exec=_exec_from_row(
                        row, "put_exit", "ref_bid",
                    ),
                ))
            except (ValueError, KeyError):
                continue
    return trades


def _compute_drawdown_series(
    equities: list[float],
) -> tuple[float, float, float]:
    if not equities:
        return 0.0, 0.0, config.INITIAL_CAPITAL_USD
    hwm = equities[0]
    max_dd = 0.0
    for eq in equities:
        hwm = max(hwm, eq)
        dd = (hwm - eq) / hwm if hwm > 0 else 0.0
        max_dd = max(max_dd, dd)
    current_hwm = max(equities)
    current_dd = (
        (current_hwm - equities[-1]) / current_hwm
        if current_hwm > 0 else 0.0
    )
    return max_dd, current_dd, current_hwm


def _compute_streaks(pnls: list[float]) -> tuple[int, int, int]:
    if not pnls:
        return 0, 0, 0
    streak = 0
    max_win = 0
    max_loss = 0
    for p in pnls:
        if p >= 0:
            streak = streak + 1 if streak > 0 else 1
        else:
            streak = streak - 1 if streak < 0 else -1
        max_win = max(max_win, streak) if streak > 0 else max_win
        max_loss = min(max_loss, streak) if streak < 0 else max_loss
    return streak, max_win, abs(max_loss)


def compute_report(
    equity: float, trading_day: Optional[str] = None,
) -> Optional[DailyMetrics]:
    """Compute full daily report. Returns None if no trades for the day.

    Multi-session aware: groups trades by ``trading_day`` (= 0DTE
    expiry UTC date) so afternoon (entered the previous calendar day)
    + morning (entered the expiry calendar day) roll into ONE report.
    The full per-session list is exposed via ``DailyMetrics.today_trades``
    sorted chronologically (afternoon first, morning second).

    Args:
        equity: current portfolio equity used for cumulative metrics.
        trading_day: optional override (YYYY-MM-DD). Defaults to the
            current UTC trading day, computed from now() with the
            08:00 UTC cutoff.
    """
    trades = _load_trades()
    if not trades:
        return None

    if trading_day is None:
        # The 17:00 UTC daily report fires AFTER the morning close
        # (02:00 UTC) of the same UTC date. By that time today's
        # trading_day is fully complete, so we use today's UTC date
        # directly. We do NOT call _trading_day_from_entry_time(now)
        # here because that would shift forward to TOMORROW's trading
        # day once now() crosses the 08:00 UTC cutoff.
        trading_day = datetime.utcnow().strftime("%Y-%m-%d")

    today_trades = [t for t in trades if t.trading_day == trading_day]
    today_trades.sort(key=_session_chronological_key)
    if not today_trades:
        return None

    latest = today_trades[-1]

    inception = _inception_equity(trades)
    pnls = [t.net_pnl for t in trades]
    returns = [
        t.net_pnl / t.capital_before if t.capital_before > 0 else 0.0
        for t in trades
    ]
    equities = [inception]
    for t in trades:
        equities.append(t.capital_after)

    wins = [p for p in pnls if p >= 0]
    losses = [p for p in pnls if p < 0]
    n_wins, n_losses = len(wins), len(losses)
    total = len(trades)
    win_rate = n_wins / total if total > 0 else 0.0
    avg_win = sum(wins) / n_wins if n_wins > 0 else 0.0
    avg_loss = sum(losses) / n_losses if n_losses > 0 else 0.0

    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    profit_factor = (
        gross_wins / gross_losses if gross_losses > 0 else float("inf")
    )

    current_streak, max_win_streak, max_loss_streak = _compute_streaks(pnls)
    max_dd, current_dd, hwm = _compute_drawdown_series(equities)

    mean_ret = sum(returns) / len(returns) if returns else 0.0
    daily_vol = (
        (sum((r - mean_ret) ** 2 for r in returns) / len(returns)) ** 0.5
        if len(returns) > 1 else 0.0
    )
    ann_vol = daily_vol * math.sqrt(TRADING_DAYS_PER_YEAR)

    sharpe = (
        ((mean_ret - RISK_FREE_RATE / TRADING_DAYS_PER_YEAR)
         / daily_vol * math.sqrt(TRADING_DAYS_PER_YEAR))
        if daily_vol > 0 else 0.0
    )

    downside_returns = [r for r in returns if r < 0]
    downside_vol = (
        (sum(r ** 2 for r in downside_returns) / len(downside_returns)) ** 0.5
        if downside_returns else 0.0
    )
    sortino = (
        ((mean_ret - RISK_FREE_RATE / TRADING_DAYS_PER_YEAR)
         / downside_vol * math.sqrt(TRADING_DAYS_PER_YEAR))
        if downside_vol > 0 else 0.0
    )

    ann_return = (
        (equity / inception) ** (TRADING_DAYS_PER_YEAR / max(total, 1)) - 1
        if inception > 0 else 0.0
    )
    calmar = ann_return / max_dd if max_dd > 0 else 0.0

    expectancy = sum(pnls) / total if total > 0 else 0.0
    expectancy_ratio = expectancy / abs(avg_loss) if avg_loss != 0 else 0.0

    # Day-level aggregates across every session that ran today.
    today_pnl = sum(t.net_pnl for t in today_trades)
    today_capital = sum(t.total_capital_used for t in today_trades)
    today_straddles = sum(t.num_straddles for t in today_trades)

    # MTD / YTD aggregates (rendered as brief blocks under Portfolio).
    # Trading day, not calendar entry date, anchors period membership.
    from reporting.period_metrics import (
        compute_period_metrics, month_window, year_window,
    )
    try:
        td_for_period = datetime.strptime(
            trading_day, "%Y-%m-%d",
        ).date()
    except ValueError:
        td_for_period = datetime.utcnow().date()
    m_start, m_end = month_window(td_for_period)
    y_start, y_end = year_window(td_for_period)
    mtd = compute_period_metrics(
        all_trades=trades,
        period_start=m_start,
        period_end=m_end,
        label=f"MTD ({td_for_period.strftime('%Y-%m')})",
        current_equity=equity,
    )
    ytd = compute_period_metrics(
        all_trades=trades,
        period_start=y_start,
        period_end=y_end,
        label=f"YTD ({td_for_period.year})",
        current_equity=equity,
    )
    starting_equity = (
        today_trades[0].capital_before
        if today_trades and today_trades[0].capital_before > 0
        else 0.0
    )
    trade_return = (
        today_pnl / starting_equity if starting_equity > 0 else 0.0
    )
    cum_return = (equity - inception) / inception if inception > 0 else 0.0

    return DailyMetrics(
        trade_date=trading_day,
        trade_pnl=today_pnl,
        trade_return_pct=trade_return,
        strike=latest.strike,
        num_straddles=today_straddles,
        equity=equity,
        initial_capital=inception,
        total_trades=total,
        total_pnl=sum(pnls),
        cumulative_return_pct=cum_return,
        wins=n_wins,
        losses=n_losses,
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        profit_factor=profit_factor,
        best_trade=max(pnls) if pnls else 0.0,
        worst_trade=min(pnls) if pnls else 0.0,
        current_streak=current_streak,
        max_win_streak=max_win_streak,
        max_loss_streak=max_loss_streak,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        calmar_ratio=calmar,
        max_drawdown_pct=max_dd,
        current_drawdown_pct=current_dd,
        high_water_mark=hwm,
        expectancy=expectancy,
        expectancy_ratio=expectancy_ratio,
        daily_vol=daily_vol,
        annualised_vol=ann_vol,
        call_premium_entry=latest.call_premium_entry,
        call_premium_exit=latest.call_premium_exit,
        put_premium_entry=latest.put_premium_entry,
        put_premium_exit=latest.put_premium_exit,
        total_capital_used=today_capital,
        starting_equity=starting_equity,
        entry_spot=latest.entry_spot,
        exit_spot=latest.exit_spot,
        call_entry_exec=latest.call_entry_exec,
        put_entry_exec=latest.put_entry_exec,
        call_exit_exec=latest.call_exit_exec,
        put_exit_exec=latest.put_exit_exec,
        today_trades=today_trades,
        qty_per_leg=latest.qty_per_leg or config.QTY_PER_LEG,
        inception_equity=inception,
        inception_date=trades[0].date if trades else "",
        mtd=mtd,
        ytd=ytd,
    )


def _format_exec_block(
    leg: str, side: str,
    entry_price: float, exit_price: float,
    e: Optional[ExecutionMetrics],
    spot_usd: float = 0.0,
) -> list[str]:
    """Format one leg+side execution block. Returns empty if no metrics.

    `e.ref_mark` and `e.ref_quote` are stored in BTC (per BTC of
    notional) since that's what OKX quotes. The fill displayed (via
    entry_price/exit_price) was already pre-converted to USD by the
    portfolio writer, so we multiply the ref-side BTC values by the
    leg's spot before formatting to keep all numbers comparable.
    """
    if e is None or e.duration_sec <= 0:
        return []
    fill = exit_price if side == "exit" else entry_price
    quote_label = "Ask" if side == "entry" else "Bid"
    ref_mark_usd = e.ref_mark * spot_usd if spot_usd > 0 else e.ref_mark
    ref_quote_usd = e.ref_quote * spot_usd if spot_usd > 0 else e.ref_quote
    return [
        f"  {leg.upper()} ({side})",
        f"    Time to fill: {e.duration_sec:.1f}s, {e.attempts} attempt(s)",
        f"    Mark at start: ${ref_mark_usd:,.2f} -> Fill: ${fill:,.2f}",
        f"    Slippage vs mark: {e.slippage_vs_mark_pct:+.2f}%",
        f"    {quote_label} at start: ${ref_quote_usd:,.2f}  "
        f"Maker P&L (vs initial taker): ${e.saved_vs_taker_usd:+,.2f}",
    ]


def _legs_for_trade(t: TradeRow) -> list[tuple[str, ExecutionMetrics, float, float]]:
    """Return the leg execution metrics for a trade row, in display order.

    Returns list of (label, metrics, fill_usd, spot_usd). Skips entries with
    no recorded execution metrics (legacy rows or RFQ unwinds).
    """
    out: list[tuple[str, ExecutionMetrics, float, float]] = []
    entry_spot = t.entry_spot or 0.0
    exit_spot = t.exit_spot or entry_spot
    legs = [
        ("PUT (entry)",  t.put_entry_exec,  t.put_premium_entry,  entry_spot),
        ("CALL (entry)", t.call_entry_exec, t.call_premium_entry, entry_spot),
        ("CALL (exit)",  t.call_exit_exec,  t.call_premium_exit,  exit_spot),
        ("PUT (exit)",   t.put_exit_exec,   t.put_premium_exit,   exit_spot),
    ]
    for label, exec_m, fill_usd, spot in legs:
        if exec_m and exec_m.duration_sec > 0:
            out.append((label, exec_m, fill_usd, spot))
    return out


def _format_leg_compact(
    label: str, e: ExecutionMetrics, fill_usd: float, spot_usd: float,
) -> list[str]:
    """One leg's execution metrics, compact format used for per-session blocks."""
    quote_label = "Ask" if "(entry)" in label else "Bid"
    ref_mark_usd = e.ref_mark * spot_usd if spot_usd > 0 else e.ref_mark
    ref_quote_usd = e.ref_quote * spot_usd if spot_usd > 0 else e.ref_quote
    return [
        f"    {label}",
        f"      Time to fill: {e.duration_sec:.1f}s, {e.attempts} attempt(s)",
        f"      Mark at start: ${ref_mark_usd:,.2f} -> Fill: ${fill_usd:,.2f}"
        f"  (slip {e.slippage_vs_mark_pct:+.2f}%)",
        f"      {quote_label} at start: ${ref_quote_usd:,.2f}  "
        f"Maker P&L (vs initial taker): ${e.saved_vs_taker_usd:+,.2f}",
    ]


def _format_execution_quality(m: DailyMetrics) -> list[str]:
    """Build the execution-quality section.

    Multi-session days render per-session blocks with all four legs
    inline (PUT/CALL entry + CALL/PUT exit), then a daily aggregate
    averaging across every leg of the day. Single-session / legacy
    rows fall back to the simpler split-by-side layout.
    """
    today = m.today_trades or []
    blocks: list[str] = []

    # ── Multi-session: per-session blocks ────────────────────────
    if len(today) > 1:
        for t in today:
            legs = _legs_for_trade(t)
            if not legs:
                continue
            label = _session_time_label(t.session) or (
                t.session or "session"
            ).upper()
            ordinal = _LEG_ORDINAL.get(t.session, "")
            if blocks:
                blocks.append("")
            blocks.append(
                f"<b>Execution — [{label}] {ordinal}</b>"
            )
            for leg_label, exec_m, fill_usd, spot in legs:
                blocks += _format_leg_compact(
                    leg_label, exec_m, fill_usd, spot,
                )

    # ── Single-session (or legacy): split entry / exit ───────────
    else:
        entry_spot = m.entry_spot or 0.0
        exit_spot = m.exit_spot or entry_spot

        entry_blocks = []
        entry_blocks += _format_exec_block(
            "put", "entry",
            m.put_premium_entry, m.put_premium_entry, m.put_entry_exec,
            spot_usd=entry_spot,
        )
        entry_blocks += _format_exec_block(
            "call", "entry",
            m.call_premium_entry, m.call_premium_entry, m.call_entry_exec,
            spot_usd=entry_spot,
        )
        if entry_blocks:
            blocks.append("<b>Entry execution</b>")
            blocks += entry_blocks

        exit_blocks = []
        exit_blocks += _format_exec_block(
            "call", "exit",
            m.call_premium_exit, m.call_premium_exit, m.call_exit_exec,
            spot_usd=exit_spot,
        )
        exit_blocks += _format_exec_block(
            "put", "exit",
            m.put_premium_exit, m.put_premium_exit, m.put_exit_exec,
            spot_usd=exit_spot,
        )
        if exit_blocks:
            if blocks:
                blocks.append("")
            blocks.append("<b>Exit execution</b>")
            blocks += exit_blocks

    if not blocks:
        return []

    # ── Daily aggregate across EVERY leg of EVERY session ─────────
    if today:
        legs: list[ExecutionMetrics] = []
        for t in today:
            for _, exec_m, _, _ in _legs_for_trade(t):
                legs.append(exec_m)
    else:
        legs = [
            m.call_entry_exec, m.put_entry_exec,
            m.call_exit_exec, m.put_exit_exec,
        ]
        legs = [x for x in legs if x and x.duration_sec > 0]

    if legs:
        total_saved = sum(x.saved_vs_taker_usd for x in legs)
        total_attempts = sum(x.attempts for x in legs)
        avg_dur = sum(x.duration_sec for x in legs) / len(legs)
        avg_slip = sum(x.slippage_vs_mark_pct for x in legs) / len(legs)
        blocks.append("")
        scope = (
            f"across {len(today)} sessions" if len(today) > 1
            else f"across {len(legs)} legs"
        )
        blocks.append(f"<b>Execution summary</b> ({scope})")
        blocks.append(
            f"  Avg time to fill: {avg_dur:.1f}s "
            f"({total_attempts} total attempts across {len(legs)} legs)"
        )
        blocks.append(f"  Avg slippage vs mark: {avg_slip:+.2f}%")
        blocks.append(
            f"  Total maker P&L (vs initial taker): ${total_saved:+,.2f}"
        )

    return blocks


_LEG_ORDINAL = {
    # Within a trading day (= day of the 08:00 UTC expiry), sessions fire
    # in this chronological order.
    #
    # WEEKDAY trading days (Tue-Sat):
    #   utc_0900  → 09:00-09:30 UTC,  ~23h to expiry  (1st)
    #   utc_1330  → 13:30-15:30 UTC,  ~18h to expiry  (2nd)
    #   utc_2330  → 23:30-24:00 UTC,  ~8h  to expiry  (3rd)
    #   utc_0100  → 01:00-02:00 UTC,  ~7h  to expiry  (4th = LAST close)
    #
    # WEEKEND trading days (Sun, Mon):
    #   utc_1430  → 14:30-15:30 UTC,  ~17.5h to expiry (1st)
    #   utc_2230  → 22:30-24:00 UTC,  ~9.5h  to expiry (2nd = LAST close)
    #
    # Weekday and weekend trading days are disjoint by construction
    # (weekday sessions don't fire Sat/Sun entries → never produce
    # Sun/Mon trading_days; weekend sessions only fire Sat/Sun entries
    # → never produce Tue-Sat trading_days). So the two ordinal spaces
    # never collide on the same trading_day.
    #
    # Legacy aliases ("morning"/"afternoon") still look up so reports
    # rendered against pre-2026-05-20 trade-log rows still pretty-print.
    "utc_0900":  "1st entry",
    "utc_1330":  "2nd entry",
    "utc_2330":  "3rd entry",
    "utc_0100":  "4th entry",
    "utc_1430":  "weekend 1st",
    "utc_2230":  "weekend 2nd",
    # Legacy:
    "afternoon": "2nd entry",  # afternoon → utc_1330
    "morning":   "4th entry",  # morning   → utc_0100
}


def _session_time_label(name: str) -> str:
    """Return ``HH:MM-HH:MM UTC`` for a session name, or '' if unknown.

    The trade log stores the session by short name. As of 2026-05-20
    the canonical form is ``utc_HHMM``; pre-migration rows may still
    use the legacy ``morning``/``afternoon`` aliases. We try the
    canonical name first, then fall back to a direct match so reports
    render correctly across the rename boundary.
    """
    canonical = config.canonical_session_name(name)
    for s in config.SESSIONS:
        if s.name == canonical:
            return s.time_label
    # Direct match for any name still in SESSIONS but not aliased.
    for s in config.SESSIONS:
        if s.name == name:
            return s.time_label
    return ""


def _format_today_block(m: DailyMetrics) -> list[str]:
    """Per-session breakdown for the trading day's pair.

    today_trades is sorted chronologically (afternoon -> morning) by
    compute_report. We keep the legacy single-session layout when only
    one session is present to preserve backward-compatible reports.
    """
    today = m.today_trades or []
    pnl_sign = "+" if m.trade_pnl >= 0 else ""

    if len(today) <= 1:
        strike_line = f"  Strike: ${m.strike:,.0f}"
        if m.entry_spot > 0:
            strike_line += f"  (spot ${m.entry_spot:,.0f}"
            if m.exit_spot > 0:
                strike_line += f" -> ${m.exit_spot:,.0f}"
            strike_line += ")"
        return [
            "<b>Today's Trade</b>",
            f"  P&L: {pnl_sign}${m.trade_pnl:,.2f} "
            f"({pnl_sign}{m.trade_return_pct:.2%})",
            strike_line,
            f"  Call: ${m.call_premium_entry:,.2f} → "
            f"${m.call_premium_exit:,.2f}",
            f"  Put: ${m.put_premium_entry:,.2f} → "
            f"${m.put_premium_exit:,.2f}",
            f"  Straddles: {m.num_straddles}",
        ]

    lines = [
        f"<b>Trading day {m.trade_date} — {len(today)} sessions</b>",
    ]
    for t in today:
        # Prefer the time-window label ("13:30-15:30 UTC") so messages
        # are unambiguous across timezones; fall back to the legacy
        # session name if no matching session is configured.
        sess_label = _session_time_label(t.session) or (
            t.session or "session"
        ).upper()
        ordinal = _LEG_ORDINAL.get(t.session, "")
        sign = "+" if t.net_pnl >= 0 else ""
        ret = (
            t.net_pnl / t.capital_before if t.capital_before > 0 else 0.0
        )
        # entry_time -> human-friendly UTC hh:mm (best-effort).
        timing = ""
        if t.entry_time:
            try:
                dt = datetime.fromisoformat(
                    t.entry_time.replace("Z", "+00:00"),
                )
                timing = f" entered {dt.strftime('%a %H:%M')} UTC"
            except Exception:
                pass
        strike_line = f"    Strike: ${t.strike:,.0f}"
        if t.entry_spot > 0:
            strike_line += f"  (spot ${t.entry_spot:,.0f}"
            if t.exit_spot > 0:
                strike_line += f" -> ${t.exit_spot:,.0f}"
            strike_line += ")"
        header = (
            f"  <b>[{sess_label}] {ordinal}</b>{timing}  qty "
            f"{t.qty_per_leg:.4f} {config.BASE_COIN}/leg"
        )
        lines.extend([
            header,
            f"    P&L: {sign}${t.net_pnl:,.2f} "
            f"({sign}{ret:.2%})",
            strike_line,
            f"    Call: ${t.call_premium_entry:,.2f} → "
            f"${t.call_premium_exit:,.2f}",
            f"    Put: ${t.put_premium_entry:,.2f} → "
            f"${t.put_premium_exit:,.2f}",
            f"    Straddles: {t.num_straddles}",
        ])
    lines.append(
        f"  <b>Combined</b>: {pnl_sign}${m.trade_pnl:,.2f} "
        f"across {len(today)} session(s)"
    )
    return lines


def _usd_bracket(usd: float) -> str:
    """Return ' ($X,XXX)' or '' (when spot unknown / sum is zero).

    The USD figure is suppressed (rather than printed as $0) when the
    underlying trade rows are missing entry_spot/exit_spot, so legacy
    CSV rows pre-multi-session don't render misleading "$0" volumes.
    """
    return f" (${usd:,.0f})" if usd > 0 else ""


def _format_volume_block(m: DailyMetrics) -> list[str]:
    """Render the Volume section.

    Each straddle round-trips: we BUY 1 call + 1 put at the open and
    SELL them back at the close. The exchange sees four fills per
    straddle (call open, put open, call close, put close), each at
    qty_per_leg BTC of underlying notional. We surface:

      • per-session round-trip notional in BTC, with USD valued at
        each session's own entry_spot (opens) + exit_spot (closes)
      • aggregate opened / closed totals across all sessions
      • total traded notional (opened + closes) — the figure that
        matches OKX VIP tier monthly volume reporting

    USD figures are computed per-session using that session's spot
    prices, NOT a single reference price, so a multi-session day
    where BTC moved between sessions reports volume accurately.
    """
    today = m.today_trades or []
    if today:
        opened_calls = sum(t.qty_per_leg * t.num_straddles for t in today)
        opened_puts = opened_calls
        # Aggregate USD: sum each session's contribution at its own spot.
        opened_usd = sum(
            2 * t.qty_per_leg * t.num_straddles * (t.entry_spot or 0.0)
            for t in today
        )
        closed_usd = sum(
            2 * t.qty_per_leg * t.num_straddles * (t.exit_spot or 0.0)
            for t in today
        )
    else:
        opened_calls = m.qty_per_leg * m.num_straddles
        opened_puts = opened_calls
        opened_usd = 2 * opened_calls * (m.entry_spot or 0.0)
        closed_usd = 2 * opened_calls * (m.exit_spot or m.entry_spot or 0.0)
    closed_calls, closed_puts = opened_calls, opened_puts
    open_total = opened_calls + opened_puts
    close_total = closed_calls + closed_puts
    traded_total = open_total + close_total
    traded_usd = opened_usd + closed_usd

    if len(today) > 1:
        per_session_lines = []
        for t in today:
            label = _session_time_label(t.session) or (
                t.session or "session"
            ).upper()
            ordinal = _LEG_ORDINAL.get(t.session, "")
            session_open = t.qty_per_leg * t.num_straddles  # per leg
            session_traded = 4 * session_open  # 2 legs × open + close
            session_traded_usd = (
                2 * session_open * (t.entry_spot or 0.0)
                + 2 * session_open * (t.exit_spot or 0.0)
            )
            per_session_lines.append(
                f"  [{label}] {ordinal}: {t.num_straddles} straddle × "
                f"{t.qty_per_leg:.4f} {config.BASE_COIN}/leg → "
                f"{session_traded:.4f} {config.BASE_COIN} traded"
                f"{_usd_bracket(session_traded_usd)}"
            )
        return [
            "<b>Volume</b>",
            *per_session_lines,
            f"  Opened total: {open_total:.4f} {config.BASE_COIN}{_usd_bracket(opened_usd)} "
            f"(calls {opened_calls:.4f} + puts {opened_puts:.4f})",
            f"  Closed total: {close_total:.4f} {config.BASE_COIN}{_usd_bracket(closed_usd)} "
            f"(calls {closed_calls:.4f} + puts {closed_puts:.4f})",
            f"  <b>Total traded notional: {traded_total:.4f} {config.BASE_COIN}"
            f"{_usd_bracket(traded_usd)}</b>  (opens + closes)",
        ]

    qpl = m.qty_per_leg or config.QTY_PER_LEG
    return [
        "<b>Volume</b>",
        f"  Position: {m.num_straddles} straddle × {qpl:.4f} {config.BASE_COIN}/leg",
        f"  Opened: {open_total:.4f} {config.BASE_COIN}{_usd_bracket(opened_usd)} "
        f"(calls {opened_calls:.4f} + puts {opened_puts:.4f})",
        f"  Closed: {close_total:.4f} {config.BASE_COIN}{_usd_bracket(closed_usd)} "
        f"(calls {closed_calls:.4f} + puts {closed_puts:.4f})",
        f"  <b>Total traded notional: {traded_total:.4f} {config.BASE_COIN}"
        f"{_usd_bracket(traded_usd)}</b>  (opens + closes)",
    ]


def _session_short_label(name: str) -> str:
    """Short column header from the session's ENTRY time, e.g. '1430'.

    Derived from config (not the session key) so a renamed-but-shifted
    session like utc_1330 (now 14:30 entry) shows its real time.
    """
    try:
        canonical = config.canonical_session_name(name)
    except Exception:
        canonical = name
    for s in config.SESSIONS:
        if s.name in (canonical, name):
            return s.entry_utc.strftime("%H%M")
    return (name or "?").replace("utc_", "").replace("UTC_", "")[:4] or "?"


def _format_session_matrix(m: DailyMetrics) -> list[str]:
    """Compact session × metric table for the trading day.

    Columns are the day's sessions (in chronological order) plus a
    TOTAL; rows are PnL ($), Volume (BTC / USD / contracts) and average
    slippage vs mark (in basis points). Rendered as a monospace <pre>
    block so the columns line up in Telegram.
    """
    today = m.today_trades or []
    if not today:
        return []

    contract_sz = getattr(config, "OKX_CONTRACT_SIZE_BTC", 0.01) or 0.01

    cols: list[dict] = []
    for t in today:
        open_leg = (t.qty_per_leg or 0.0) * (t.num_straddles or 1)
        traded_btc = 4 * open_leg  # 2 legs × (open + close)
        entry_spot = t.entry_spot or 0.0
        exit_spot = t.exit_spot or entry_spot
        traded_usd = (2 * open_leg * entry_spot) + (2 * open_leg * exit_spot)
        contracts = traded_btc / contract_sz if contract_sz else 0.0
        legs = _legs_for_trade(t)
        slip_bps = (
            (sum(e.slippage_vs_mark_pct for _, e, _, _ in legs) / len(legs))
            * 100.0
        ) if legs else 0.0
        cols.append({
            "hdr": _session_short_label(t.session),
            "pnl": t.net_pnl,
            "btc": traded_btc,
            "usd": traded_usd,
            "contracts": contracts,
            "slip_bps": slip_bps,
            "has_slip": bool(legs),
        })

    pnl_t = sum(c["pnl"] for c in cols)
    btc_t = sum(c["btc"] for c in cols)
    usd_t = sum(c["usd"] for c in cols)
    contr_t = sum(c["contracts"] for c in cols)
    slip_cols = [c for c in cols if c["has_slip"]]
    slip_w = sum(c["btc"] for c in slip_cols)
    slip_t = (
        sum(c["slip_bps"] * c["btc"] for c in slip_cols) / slip_w
        if slip_cols and slip_w > 0 else 0.0
    )

    def _usd_k(v: float) -> str:
        return f"{v / 1000:.0f}k" if abs(v) >= 1000 else f"{v:.0f}"

    headers = [c["hdr"] for c in cols] + ["TOT"]
    rows = [
        ("PnL $",   [f"{c['pnl']:+.1f}" for c in cols] + [f"{pnl_t:+.1f}"]),
        (f"Vol {config.BASE_COIN}", [f"{c['btc']:.2f}" for c in cols] + [f"{btc_t:.2f}"]),
        ("Vol $",   [_usd_k(c["usd"]) for c in cols] + [_usd_k(usd_t)]),
        ("Contr",   [f"{c['contracts']:.0f}" for c in cols] + [f"{contr_t:.0f}"]),
        ("Slip bp", [(f"{c['slip_bps']:+.0f}" if c["has_slip"] else "-")
                     for c in cols] + [f"{slip_t:+.0f}"]),
    ]

    label_w = max(len(r[0]) for r in rows)
    col_w = [
        max(len(headers[i]), *(len(vals[i]) for _, vals in rows))
        for i in range(len(headers))
    ]

    def _line(label: str, vals: list[str]) -> str:
        cells = " ".join(v.rjust(col_w[i]) for i, v in enumerate(vals))
        return f"{label.ljust(label_w)}  {cells}"

    return [
        "<b>Session matrix</b>",
        "<pre>",
        _line("", headers),
        *[_line(lbl, vals) for lbl, vals in rows],
        "</pre>",
    ]


def format_telegram_report(m: DailyMetrics) -> str:
    """Full daily report as HTML Telegram message."""
    streak_txt = ""
    if m.current_streak > 0:
        streak_txt = f" ({m.current_streak}W)"
    elif m.current_streak < 0:
        streak_txt = f" ({abs(m.current_streak)}L)"

    if m.starting_equity > 0:
        equity_line = (
            f"  Equity: ${m.starting_equity:,.2f} -> ${m.equity:,.2f}"
        )
    else:
        equity_line = f"  Equity: ${m.equity:,.2f}"

    lines = [
        f"<b>DAILY REPORT — {m.trade_date}</b> "
        f"<i>[{option_family.label()} family]</i>",
        "",
        *_format_today_block(m),
        "",
        *_format_session_matrix(m),
        "",
        *_format_volume_block(m),
        "",
        "<b>Capital</b>",
        f"  <b>Total deployed: ${m.total_capital_used:,.2f}</b>",
        equity_line,
        "",
        "<b>Portfolio</b>",
        # Cumulative P&L is anchored on the LIVE wallet (equity − inception)
        # so the dollar number always matches the percentage. The
        # "Trade ledger" line below reports the algorithmic sum of per-
        # trade P&Ls, and "Drift" = wallet − ledger. The drift comes from
        # MTM on BTC-denominated margin (auto-borrow), settlement vs.
        # closing-trade price, maker rebates, and borrow interest — none
        # of which are captured in the per-trade P&L snapshot.
        f"  Cumulative P&L: ${(m.equity - m.inception_equity):,.2f} "
        f"({m.cumulative_return_pct:+.1%})"
        + (
            f"  <i>since ${m.inception_equity:,.0f} on {m.inception_date}</i>"
            if m.inception_equity > 0 else ""
        ),
        f"  Trade ledger: ${m.total_pnl:,.2f}  "
        f"Drift vs wallet: "
        f"${(m.equity - m.inception_equity - m.total_pnl):+,.2f}  "
        f"<i>(MTM/settlement/fees)</i>",
        f"  High Water Mark: ${m.high_water_mark:,.2f}",
        "",
        f"<b>Win/Loss ({m.total_trades} trades)</b>",
        f"  Win rate: {m.win_rate:.1%} ({m.wins}W / {m.losses}L)"
        f"{streak_txt}",
        f"  Avg win: ${m.avg_win:,.2f} | Avg loss: ${m.avg_loss:,.2f}",
        f"  Best: ${m.best_trade:,.2f} | Worst: ${m.worst_trade:,.2f}",
        f"  Profit factor: {m.profit_factor:.2f}",
        f"  Streaks: {m.max_win_streak}W max / {m.max_loss_streak}L max",
    ]

    # ── MTD / YTD brief blocks ───────────────────────────────────
    # The dedicated MONTH-END / YEAR-END deep reports fire on the
    # last trading day of the month/year (see core/notifier.py and
    # main._on_close); these inline blocks are the always-on
    # summary so the operator can monitor period progress daily.
    from reporting.period_metrics import format_brief_period_block
    if m.mtd is not None:
        lines.append("")
        lines.extend(format_brief_period_block(m.mtd))
    if m.ytd is not None:
        lines.append("")
        lines.extend(format_brief_period_block(m.ytd))

    exec_lines = _format_execution_quality(m)
    if exec_lines:
        lines.append("")
        lines.extend(exec_lines)

    return "\n".join(lines)


def format_telegram_summary(m: DailyMetrics) -> str:
    """Slim TRADE SUMMARY (kept for ad-hoc use / future report chat).

    The production daily push uses :func:`format_telegram_report` so
    this slim variant is currently unreferenced; keeping the function
    around lets ops / debug scripts opt in to a smaller payload
    without recomputing metrics.
    """
    if m.starting_equity > 0:
        equity_line = (
            f"  Equity: ${m.starting_equity:,.2f} -> ${m.equity:,.2f}"
        )
    else:
        equity_line = f"  Equity: ${m.equity:,.2f}"

    lines = [
        f"<b>TRADE SUMMARY — {m.trade_date}</b>",
        "",
        *_format_today_block(m),
        equity_line,
        "",
        *_format_volume_block(m),
    ]

    exec_lines = _format_execution_quality(m)
    if exec_lines:
        lines.append("")
        lines.extend(exec_lines)

    return "\n".join(lines)


# ═══════════════════════ Weekly Report ═══════════════════════════════

def _monday_of_week(date_str: str) -> str:
    dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


def compute_weekly_report(equity: float) -> Optional[DailyMetrics]:
    """Compute a report scoped to the current ISO week (Mon–Fri)."""
    all_trades = _load_trades()
    if not all_trades:
        return None

    today = datetime.utcnow()
    week_monday = today - timedelta(days=today.weekday())
    week_start = week_monday.strftime("%Y-%m-%d")

    trades = [t for t in all_trades if _monday_of_week(t.date) == week_start]
    if not trades:
        return None

    pnls = [t.net_pnl for t in trades]
    returns = [
        t.net_pnl / t.capital_before if t.capital_before > 0 else 0.0
        for t in trades
    ]

    equity_start = trades[0].capital_before
    equities = [equity_start]
    for t in trades:
        equities.append(t.capital_after)

    wins = [p for p in pnls if p >= 0]
    losses = [p for p in pnls if p < 0]
    n_wins, n_losses = len(wins), len(losses)
    total = len(trades)
    win_rate = n_wins / total if total > 0 else 0.0
    avg_win = sum(wins) / n_wins if n_wins > 0 else 0.0
    avg_loss = sum(losses) / n_losses if n_losses > 0 else 0.0

    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    profit_factor = (
        gross_wins / gross_losses if gross_losses > 0 else float("inf")
    )

    current_streak, max_win_streak, max_loss_streak = _compute_streaks(pnls)
    max_dd, current_dd, hwm = _compute_drawdown_series(equities)

    mean_ret = sum(returns) / len(returns) if returns else 0.0
    daily_vol = (
        (sum((r - mean_ret) ** 2 for r in returns) / len(returns)) ** 0.5
        if len(returns) > 1 else 0.0
    )
    ann_vol = daily_vol * math.sqrt(TRADING_DAYS_PER_YEAR)
    sharpe = (
        (mean_ret / daily_vol * math.sqrt(TRADING_DAYS_PER_YEAR))
        if daily_vol > 0 else 0.0
    )

    downside_returns = [r for r in returns if r < 0]
    downside_vol = (
        (sum(r ** 2 for r in downside_returns) / len(downside_returns)) ** 0.5
        if downside_returns else 0.0
    )
    sortino = (
        (mean_ret / downside_vol * math.sqrt(TRADING_DAYS_PER_YEAR))
        if downside_vol > 0 else 0.0
    )

    weekly_return = sum(pnls) / equity_start if equity_start > 0 else 0.0
    calmar = (weekly_return * 52) / max_dd if max_dd > 0 else 0.0

    expectancy = sum(pnls) / total if total > 0 else 0.0
    expectancy_ratio = expectancy / abs(avg_loss) if avg_loss != 0 else 0.0

    latest = trades[-1]
    inception = _inception_equity(all_trades)
    cum_return = (equity - inception) / inception if inception > 0 else 0.0

    total_straddles = sum(t.num_straddles for t in trades)

    # MTD / YTD aggregates for the weekly report — anchored to "today"
    # so end-of-week reports always carry the up-to-date period summary.
    from reporting.period_metrics import (
        compute_period_metrics, month_window, year_window,
    )
    try:
        td_for_period = datetime.strptime(
            latest.trading_day or week_start, "%Y-%m-%d",
        ).date()
    except ValueError:
        td_for_period = datetime.utcnow().date()
    m_start, m_end = month_window(td_for_period)
    y_start, y_end = year_window(td_for_period)
    mtd = compute_period_metrics(
        all_trades=all_trades,
        period_start=m_start,
        period_end=m_end,
        label=f"MTD ({td_for_period.strftime('%Y-%m')})",
        current_equity=equity,
    )
    ytd = compute_period_metrics(
        all_trades=all_trades,
        period_start=y_start,
        period_end=y_end,
        label=f"YTD ({td_for_period.year})",
        current_equity=equity,
    )

    return DailyMetrics(
        trade_date=week_start,
        trade_pnl=sum(pnls),
        trade_return_pct=weekly_return,
        strike=latest.strike,
        num_straddles=total_straddles,
        equity=equity,
        initial_capital=inception,
        total_trades=total,
        total_pnl=sum(pnls),
        cumulative_return_pct=cum_return,
        wins=n_wins,
        losses=n_losses,
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        profit_factor=profit_factor,
        best_trade=max(pnls) if pnls else 0.0,
        worst_trade=min(pnls) if pnls else 0.0,
        current_streak=current_streak,
        max_win_streak=max_win_streak,
        max_loss_streak=max_loss_streak,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        calmar_ratio=calmar,
        max_drawdown_pct=max_dd,
        current_drawdown_pct=current_dd,
        high_water_mark=hwm,
        expectancy=expectancy,
        expectancy_ratio=expectancy_ratio,
        daily_vol=daily_vol,
        annualised_vol=ann_vol,
        call_premium_entry=latest.call_premium_entry,
        call_premium_exit=latest.call_premium_exit,
        put_premium_entry=latest.put_premium_entry,
        put_premium_exit=latest.put_premium_exit,
        total_capital_used=sum(t.total_capital_used for t in trades),
        starting_equity=trades[0].capital_before if trades else 0.0,
        entry_spot=latest.entry_spot,
        exit_spot=latest.exit_spot,
        # Reuse `today_trades` to carry the full week's rows so the
        # weekly formatter can sum per-trade qty_per_leg correctly.
        today_trades=trades,
        qty_per_leg=latest.qty_per_leg or config.QTY_PER_LEG,
        inception_equity=inception,
        inception_date=trades[0].date if trades else "",
        mtd=mtd,
        ytd=ytd,
    )


def format_weekly_report(m: DailyMetrics) -> str:
    pnl_sign = "+" if m.trade_pnl >= 0 else ""

    # Weekly volume sums per-trade qty_per_leg so heterogeneous session
    # sizes count correctly (utc_0900 / utc_1330 / utc_2330 / utc_0100,
    # each potentially under pct_equity sizing with a different qty per
    # entry). Each straddle round-trips (open + close) for both legs,
    # so total traded notional = 4 × qty_per_leg × num_straddles per row.
    # USD values are computed per-trade using each row's own
    # entry_spot/exit_spot so weekly totals are accurate across the
    # spot moves between sessions.
    week_trades = m.today_trades or []
    if week_trades:
        opened_calls = sum(
            t.qty_per_leg * t.num_straddles for t in week_trades
        )
        opened_usd = sum(
            2 * t.qty_per_leg * t.num_straddles * (t.entry_spot or 0.0)
            for t in week_trades
        )
        closed_usd = sum(
            2 * t.qty_per_leg * t.num_straddles * (t.exit_spot or 0.0)
            for t in week_trades
        )
    else:
        qpl = m.qty_per_leg or config.QTY_PER_LEG
        opened_calls = qpl * m.num_straddles
        opened_usd = 2 * opened_calls * (m.entry_spot or 0.0)
        closed_usd = 2 * opened_calls * (m.exit_spot or m.entry_spot or 0.0)
    opened_puts = opened_calls
    closed_calls, closed_puts = opened_calls, opened_puts
    open_total = opened_calls + opened_puts
    close_total = closed_calls + closed_puts
    traded_total = open_total + close_total
    traded_usd = opened_usd + closed_usd

    cum_pnl_wallet = m.equity - m.inception_equity if m.inception_equity > 0 else 0.0
    drift = cum_pnl_wallet - m.total_pnl
    inception_suffix = (
        f"  <i>since ${m.inception_equity:,.0f} on {m.inception_date}</i>"
        if m.inception_equity > 0 else ""
    )

    lines = [
        f"<b>WEEKLY REPORT — Week of {m.trade_date}</b> "
        f"<i>[{option_family.label()} family]</i>",
        "",
        f"  Weekly P&L: {pnl_sign}${m.trade_pnl:,.2f} "
        f"({pnl_sign}{m.trade_return_pct:.2%})",
        f"  Trades: {m.total_trades} ({m.wins}W / {m.losses}L)",
        f"  Equity: ${m.equity:,.2f}",
        f"  Cumulative: ${cum_pnl_wallet:,.2f} "
        f"({m.cumulative_return_pct:+.1%}){inception_suffix}",
        f"  Trade ledger: ${m.total_pnl:,.2f}  "
        f"Drift vs wallet: ${drift:+,.2f}  "
        f"<i>(MTM/settlement/fees)</i>",
        "",
        "<b>Volume (this week)</b>",
        f"  Straddles: {m.num_straddles}",
        f"  Opened: {open_total:.4f} {config.BASE_COIN}{_usd_bracket(opened_usd)} "
        f"(calls {opened_calls:.4f} + puts {opened_puts:.4f})",
        f"  Closed: {close_total:.4f} {config.BASE_COIN}{_usd_bracket(closed_usd)} "
        f"(calls {closed_calls:.4f} + puts {closed_puts:.4f})",
        f"  <b>Total traded notional: {traded_total:.4f} {config.BASE_COIN}"
        f"{_usd_bracket(traded_usd)}</b>  (opens + closes)",
    ]

    # MTD / YTD brief blocks for the weekly report too — same content
    # as the daily push so operators get a consistent summary regardless
    # of which scheduled message they're looking at.
    from reporting.period_metrics import format_brief_period_block
    if m.mtd is not None:
        lines.append("")
        lines.extend(format_brief_period_block(m.mtd))
    if m.ytd is not None:
        lines.append("")
        lines.extend(format_brief_period_block(m.ytd))

    return "\n".join(lines)


# ═══════════════════════ Weekend Recap ════════════════════════════════
#
# The weekend recap is the W-B counterpart to the weekly report. It
# fires after the LAST close of the weekend (utc_2230 Sun → Mon 00:00
# UTC) and covers ONLY the weekend-entry trades — keeping weekday
# strategy P&L cleanly separated from weekend strategy P&L.
#
# Why filter by session NAME instead of trading_day weekday:
#   • A weekend trade entered Sat 14:30 UTC has trading_day=Sun and
#     entry_date=Sat. A weekend trade entered Sun 22:30 UTC has
#     trading_day=Mon and entry_date=Sun. Filtering on either field
#     alone misses one of the two days. Filtering on session name
#     guarantees we capture every weekend-strategy trade regardless of
#     how its trading_day mapped through the 08:00 UTC cutoff.
#   • Operationally simpler: when the operator adds a third weekend
#     session in the future, they just add it to WEEKEND_SESSION_NAMES.

# Sessions that belong to the weekend recap window. SINGLE SOURCE OF
# TRUTH is ``config.WEEKEND_SESSION_NAMES`` (derived from the weekend
# schedule); re-exported here so existing importers of
# ``daily_report.WEEKEND_SESSION_NAMES`` keep working and can never drift
# from the live schedule.
WEEKEND_SESSION_NAMES: frozenset[str] = config.WEEKEND_SESSION_NAMES


def _weekend_window(today_utc: datetime) -> tuple[str, str]:
    """Return (sat_date, sun_date) ISO strings for the most-recent weekend.

    "Most-recent" means the Sat-Sun pair that just passed at the time
    the recap fires. With the canonical fire timing (Mon 00:00 UTC, after
    Sun utc_2230 close), this resolves to:

        ``today.weekday() == 0`` (Mon)  →  Sat = today - 2d, Sun = today - 1d

    For other call times (manual recap re-run from tools/) we still pick
    the most recent past Sat-Sun, walking back from `today_utc.date()`:

        Tue–Sat call → previous calendar week's Sat/Sun.
        Sun call     → today's Sat-Sun (the recap is firing during the
                       weekend itself; useful for ad-hoc operator inspection).
        Sun  near 23:59 UTC just before the trigger → still the current weekend.
    """
    d = today_utc.date()
    wd = d.weekday()  # 0=Mon..6=Sun
    if wd == 0:
        # Mon — point to the Sat-Sun pair we just finished.
        sat = d - timedelta(days=2)
    elif wd == 6:
        # Sun — point to the Sat-Sun pair currently in progress.
        sat = d - timedelta(days=1)
    elif wd == 5:
        # Sat — current weekend, recap will likely be empty until Sun.
        sat = d
    else:
        # Tue-Fri call (manual re-run) — previous weekend.
        # last Mon = d - wd days; Sun before = last Mon - 1, Sat = -2.
        sat = d - timedelta(days=wd + 2)
    sun = sat + timedelta(days=1)
    return sat.strftime("%Y-%m-%d"), sun.strftime("%Y-%m-%d")


def compute_weekend_recap(equity: float) -> Optional[DailyMetrics]:
    """Build a DailyMetrics scoped to the most-recent weekend's trades.

    Returns ``None`` if no weekend-strategy trades fall in the window
    (e.g. operator disabled both weekend sessions, or first weekend
    after deploy). The caller suppresses an empty Telegram push.

    Membership rule: ``t.session in WEEKEND_SESSION_NAMES`` AND
    ``t.date in {sat_date, sun_date}``.
    """
    all_trades = _load_trades()
    if not all_trades:
        return None

    sat_date, sun_date = _weekend_window(datetime.utcnow())
    weekend_dates = {sat_date, sun_date}
    trades = [
        t for t in all_trades
        if t.session in WEEKEND_SESSION_NAMES
        and (t.date or "")[:10] in weekend_dates
    ]
    if not trades:
        return None

    pnls = [t.net_pnl for t in trades]
    returns = [
        t.net_pnl / t.capital_before if t.capital_before > 0 else 0.0
        for t in trades
    ]

    equity_start = trades[0].capital_before
    equities = [equity_start]
    for t in trades:
        equities.append(t.capital_after)

    wins = [p for p in pnls if p >= 0]
    losses = [p for p in pnls if p < 0]
    n_wins, n_losses = len(wins), len(losses)
    total = len(trades)
    win_rate = n_wins / total if total > 0 else 0.0
    avg_win = sum(wins) / n_wins if n_wins > 0 else 0.0
    avg_loss = sum(losses) / n_losses if n_losses > 0 else 0.0

    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    profit_factor = (
        gross_wins / gross_losses if gross_losses > 0 else float("inf")
    )

    current_streak, max_win_streak, max_loss_streak = _compute_streaks(pnls)
    max_dd, current_dd, hwm = _compute_drawdown_series(equities)

    mean_ret = sum(returns) / len(returns) if returns else 0.0
    daily_vol = (
        (sum((r - mean_ret) ** 2 for r in returns) / len(returns)) ** 0.5
        if len(returns) > 1 else 0.0
    )
    # Annualisation factor is fuzzy for a 2-day strategy. We DON'T
    # multiply by sqrt(252) here — the resulting Sharpe would be
    # apples-to-oranges with the weekday weekly Sharpe. Caller can
    # eyeball mean_ret / daily_vol if they want.
    sharpe = mean_ret / daily_vol if daily_vol > 0 else 0.0

    expectancy = sum(pnls) / total if total > 0 else 0.0
    expectancy_ratio = expectancy / abs(avg_loss) if avg_loss != 0 else 0.0

    weekend_return = sum(pnls) / equity_start if equity_start > 0 else 0.0

    latest = trades[-1]
    inception = _inception_equity(all_trades)
    cum_return = (equity - inception) / inception if inception > 0 else 0.0

    total_straddles = sum(t.num_straddles for t in trades)

    return DailyMetrics(
        trade_date=f"{sat_date} → {sun_date}",
        trade_pnl=sum(pnls),
        trade_return_pct=weekend_return,
        strike=latest.strike,
        num_straddles=total_straddles,
        equity=equity,
        initial_capital=inception,
        total_trades=total,
        total_pnl=sum(pnls),
        cumulative_return_pct=cum_return,
        wins=n_wins,
        losses=n_losses,
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        profit_factor=profit_factor,
        best_trade=max(pnls) if pnls else 0.0,
        worst_trade=min(pnls) if pnls else 0.0,
        current_streak=current_streak,
        max_win_streak=max_win_streak,
        max_loss_streak=max_loss_streak,
        sharpe_ratio=sharpe,
        sortino_ratio=0.0,
        calmar_ratio=0.0,
        max_drawdown_pct=max_dd,
        current_drawdown_pct=current_dd,
        high_water_mark=hwm,
        expectancy=expectancy,
        expectancy_ratio=expectancy_ratio,
        daily_vol=daily_vol,
        annualised_vol=0.0,
        call_premium_entry=latest.call_premium_entry,
        call_premium_exit=latest.call_premium_exit,
        put_premium_entry=latest.put_premium_entry,
        put_premium_exit=latest.put_premium_exit,
        total_capital_used=sum(t.total_capital_used for t in trades),
        starting_equity=trades[0].capital_before if trades else 0.0,
        entry_spot=latest.entry_spot,
        exit_spot=latest.exit_spot,
        today_trades=trades,
        qty_per_leg=latest.qty_per_leg or config.QTY_PER_LEG,
        inception_equity=inception,
        inception_date=trades[0].date if trades else "",
    )


def format_weekend_recap(m: DailyMetrics) -> str:
    """Format the weekend recap for Telegram (HTML).

    Mirrors the weekly report style but explicitly labels itself as
    "WEEKEND RECAP" so the operator never confuses it with the
    Mon-Fri weekly report. Includes a per-session breakdown and the
    same volume / equity blocks; SKIPS the MTD/YTD blocks (those live
    on the daily report and are duplicated already).
    """
    pnl_sign = "+" if m.trade_pnl >= 0 else ""
    weekend_trades = m.today_trades or []

    if weekend_trades:
        opened_calls = sum(
            t.qty_per_leg * t.num_straddles for t in weekend_trades
        )
        opened_usd = sum(
            2 * t.qty_per_leg * t.num_straddles * (t.entry_spot or 0.0)
            for t in weekend_trades
        )
        closed_usd = sum(
            2 * t.qty_per_leg * t.num_straddles * (t.exit_spot or 0.0)
            for t in weekend_trades
        )
    else:
        qpl = m.qty_per_leg or config.QTY_PER_LEG
        opened_calls = qpl * m.num_straddles
        opened_usd = 2 * opened_calls * (m.entry_spot or 0.0)
        closed_usd = 2 * opened_calls * (m.exit_spot or m.entry_spot or 0.0)
    opened_puts = opened_calls
    closed_calls, closed_puts = opened_calls, opened_puts
    open_total = opened_calls + opened_puts
    close_total = closed_calls + closed_puts
    traded_total = open_total + close_total
    traded_usd = opened_usd + closed_usd

    cum_pnl_wallet = (
        m.equity - m.inception_equity if m.inception_equity > 0 else 0.0
    )
    inception_suffix = (
        f"  <i>since ${m.inception_equity:,.0f} on {m.inception_date}</i>"
        if m.inception_equity > 0 else ""
    )

    lines = [
        f"<b>WEEKEND RECAP — {m.trade_date}</b> "
        f"<i>[{option_family.label()} family]</i>",
        "",
        f"  Weekend P&L: {pnl_sign}${m.trade_pnl:,.2f} "
        f"({pnl_sign}{m.trade_return_pct:.2%})",
        f"  Trades: {m.total_trades} ({m.wins}W / {m.losses}L)",
        f"  Equity: ${m.equity:,.2f}",
        f"  Cumulative: ${cum_pnl_wallet:,.2f} "
        f"({m.cumulative_return_pct:+.1%}){inception_suffix}",
        "",
        "<b>Performance</b>",
        f"  Win rate: {m.win_rate:.1%}",
        f"  Avg win: ${m.avg_win:,.2f}  |  Avg loss: ${m.avg_loss:,.2f}",
        f"  Best: ${m.best_trade:,.2f}  |  Worst: ${m.worst_trade:,.2f}",
        f"  Profit factor: {m.profit_factor:.2f}",
        f"  Expectancy: ${m.expectancy:,.2f} "
        f"(× {m.expectancy_ratio:.2f} avg loss)",
        "",
        "<b>Volume (this weekend)</b>",
        f"  Straddles: {m.num_straddles}",
        f"  Opened: {open_total:.4f} {config.BASE_COIN}{_usd_bracket(opened_usd)} "
        f"(calls {opened_calls:.4f} + puts {opened_puts:.4f})",
        f"  Closed: {close_total:.4f} {config.BASE_COIN}{_usd_bracket(closed_usd)} "
        f"(calls {closed_calls:.4f} + puts {closed_puts:.4f})",
        f"  <b>Total traded notional: {traded_total:.4f} {config.BASE_COIN}"
        f"{_usd_bracket(traded_usd)}</b>",
    ]

    # Per-session breakdown so operators can see whether utc_1430 or
    # utc_2230 carried the weekend's P&L.
    by_session: dict[str, list] = {}
    for t in weekend_trades:
        by_session.setdefault(t.session, []).append(t)
    if by_session:
        lines.extend(["", "<b>By session</b>"])

        def _session_sort_key(s: str):
            # Chronological within the trading day. Prefer the live
            # Session's close-minutes ordering; fall back to the legacy
            # _LEG_ORDINAL hint for historical names no longer in config.
            sess = config.get_session(s)
            if sess is not None:
                return (0, sess.close_minutes_in_trading_day)
            return (1, _LEG_ORDINAL.get(s, ""))

        for sname in sorted(by_session.keys(), key=_session_sort_key):
            rows = by_session[sname]
            sub_pnl = sum(t.net_pnl for t in rows)
            sub_sign = "+" if sub_pnl >= 0 else ""
            sub_wins = sum(1 for t in rows if t.net_pnl >= 0)
            sub_losses = len(rows) - sub_wins
            ordinal = _LEG_ORDINAL.get(sname, "")
            label = _session_time_label(sname) or sname
            _ord_suffix = f" ({ordinal})" if ordinal else ""
            lines.append(
                f"  • {label}{_ord_suffix}  "
                f"{sub_sign}${sub_pnl:,.2f}  "
                f"({sub_wins}W/{sub_losses}L over {len(rows)} entries)"
            )

    return "\n".join(lines)
