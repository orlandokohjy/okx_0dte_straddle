"""
Daily and weekly performance reports for the OKX straddle algo.

Reads the trade log CSV, computes quant metrics over the full history
(daily) or the current ISO week (weekly), and formats Telegram-ready
HTML reports.
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

    # Execution-quality (today's trade only)
    call_entry_exec: Optional[ExecutionMetrics] = None
    put_entry_exec: Optional[ExecutionMetrics] = None
    call_exit_exec: Optional[ExecutionMetrics] = None
    put_exit_exec: Optional[ExecutionMetrics] = None
    call_instrument: str = ""
    put_instrument: str = ""


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


def _load_trades() -> list[TradeRow]:
    path = config.TRADE_LOG_FILE
    if not os.path.exists(path):
        return []
    trades: list[TradeRow] = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                trades.append(TradeRow(
                    date=row["date"],
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


def compute_report(equity: float) -> Optional[DailyMetrics]:
    """Compute full daily report. Returns None if no trade today."""
    trades = _load_trades()
    if not trades:
        return None

    latest = trades[-1]
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    if latest.date[:10] != today_str:
        return None

    pnls = [t.net_pnl for t in trades]
    returns = [
        t.net_pnl / t.capital_before if t.capital_before > 0 else 0.0
        for t in trades
    ]
    equities = [config.INITIAL_CAPITAL_USD]
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
        (equity / config.INITIAL_CAPITAL_USD)
        ** (TRADING_DAYS_PER_YEAR / max(total, 1)) - 1
    )
    calmar = ann_return / max_dd if max_dd > 0 else 0.0

    expectancy = sum(pnls) / total if total > 0 else 0.0
    expectancy_ratio = expectancy / abs(avg_loss) if avg_loss != 0 else 0.0

    trade_return = (
        latest.net_pnl / latest.capital_before
        if latest.capital_before > 0 else 0.0
    )
    cum_return = (
        (equity - config.INITIAL_CAPITAL_USD) / config.INITIAL_CAPITAL_USD
    )

    return DailyMetrics(
        trade_date=latest.date,
        trade_pnl=latest.net_pnl,
        trade_return_pct=trade_return,
        strike=latest.strike,
        num_straddles=latest.num_straddles,
        equity=equity,
        initial_capital=config.INITIAL_CAPITAL_USD,
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
        total_capital_used=latest.total_capital_used,
        starting_equity=latest.capital_before,
        entry_spot=latest.entry_spot,
        exit_spot=latest.exit_spot,
        call_entry_exec=latest.call_entry_exec,
        put_entry_exec=latest.put_entry_exec,
        call_exit_exec=latest.call_exit_exec,
        put_exit_exec=latest.put_exit_exec,
    )


def _format_exec_block(
    leg: str, side: str,
    entry_price: float, exit_price: float,
    e: Optional[ExecutionMetrics],
) -> list[str]:
    """Format one leg+side execution block. Returns empty if no metrics."""
    if e is None or e.duration_sec <= 0:
        return []
    fill = exit_price if side == "exit" else entry_price
    quote_label = "Ask" if side == "entry" else "Bid"
    return [
        f"  {leg.upper()} ({side})",
        f"    Time to fill: {e.duration_sec:.1f}s, {e.attempts} attempt(s)",
        f"    Mark at start: ${e.ref_mark:,.2f} -> Fill: ${fill:,.2f}",
        f"    Slippage vs mark: {e.slippage_vs_mark_pct:+.2f}%",
        f"    {quote_label} at start: ${e.ref_quote:,.2f}  "
        f"Saved vs taker: ${e.saved_vs_taker_usd:+,.2f}",
    ]


def _format_execution_quality(m: DailyMetrics) -> list[str]:
    """Build the execution-quality section for the daily report."""
    blocks: list[str] = []

    entry_blocks = []
    entry_blocks += _format_exec_block(
        "put", "entry",
        m.put_premium_entry, m.put_premium_entry, m.put_entry_exec,
    )
    entry_blocks += _format_exec_block(
        "call", "entry",
        m.call_premium_entry, m.call_premium_entry, m.call_entry_exec,
    )
    if entry_blocks:
        blocks.append("<b>Entry execution</b>")
        blocks += entry_blocks

    exit_blocks = []
    exit_blocks += _format_exec_block(
        "call", "exit",
        m.call_premium_exit, m.call_premium_exit, m.call_exit_exec,
    )
    exit_blocks += _format_exec_block(
        "put", "exit",
        m.put_premium_exit, m.put_premium_exit, m.put_exit_exec,
    )
    if exit_blocks:
        if blocks:
            blocks.append("")
        blocks.append("<b>Exit execution</b>")
        blocks += exit_blocks

    if not blocks:
        return []

    # Aggregate summary
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
        blocks.append("<b>Execution summary</b>")
        blocks.append(
            f"  Avg time to fill: {avg_dur:.1f}s "
            f"({total_attempts} total attempts across {len(legs)} legs)"
        )
        blocks.append(f"  Avg slippage vs mark: {avg_slip:+.2f}%")
        blocks.append(
            f"  Total saved vs taker: ${total_saved:+,.2f}"
        )

    return blocks


def format_telegram_report(m: DailyMetrics) -> str:
    """Full daily report as HTML Telegram message."""
    streak_txt = ""
    if m.current_streak > 0:
        streak_txt = f" ({m.current_streak}W)"
    elif m.current_streak < 0:
        streak_txt = f" ({abs(m.current_streak)}L)"

    pnl_sign = "+" if m.trade_pnl >= 0 else ""

    call_btc = config.QTY_PER_LEG * m.num_straddles
    put_btc = config.QTY_PER_LEG * m.num_straddles
    total_btc = call_btc + put_btc

    strike_line = f"  Strike: ${m.strike:,.0f}"
    if m.entry_spot > 0:
        strike_line += f"  (spot ${m.entry_spot:,.0f}"
        if m.exit_spot > 0:
            strike_line += f" -> ${m.exit_spot:,.0f}"
        strike_line += ")"

    if m.starting_equity > 0:
        equity_line = (
            f"  Equity: ${m.starting_equity:,.2f} -> ${m.equity:,.2f}"
        )
    else:
        equity_line = f"  Equity: ${m.equity:,.2f}"

    lines = [
        f"<b>DAILY REPORT — {m.trade_date}</b>",
        "",
        "<b>Today's Trade</b>",
        f"  P&L: {pnl_sign}${m.trade_pnl:,.2f} "
        f"({pnl_sign}{m.trade_return_pct:.2%})",
        strike_line,
        f"  Call: ${m.call_premium_entry:,.2f} → "
        f"${m.call_premium_exit:,.2f}",
        f"  Put: ${m.put_premium_entry:,.2f} → ${m.put_premium_exit:,.2f}",
        f"  Straddles: {m.num_straddles}",
        "",
        "<b>Volume</b>",
        f"  Calls: {m.num_straddles} × {config.QTY_PER_LEG} = "
        f"{call_btc:.1f} BTC",
        f"  Puts: {m.num_straddles} × {config.QTY_PER_LEG} = "
        f"{put_btc:.1f} BTC",
        f"  Total notional: {total_btc:.1f} BTC",
        "",
        "<b>Capital</b>",
        f"  Call premium: ${m.call_premium_entry * call_btc:,.2f}",
        f"  Put premium: ${m.put_premium_entry * put_btc:,.2f}",
        f"  <b>Total deployed: ${m.total_capital_used:,.2f}</b>",
        equity_line,
        "",
        "<b>Portfolio</b>",
        f"  Cumulative P&L: ${m.total_pnl:,.2f} "
        f"({m.cumulative_return_pct:+.1%})",
        f"  High Water Mark: ${m.high_water_mark:,.2f}",
        "",
        f"<b>Win/Loss ({m.total_trades} trades)</b>",
        f"  Win rate: {m.win_rate:.1%} ({m.wins}W / {m.losses}L)"
        f"{streak_txt}",
        f"  Avg win: ${m.avg_win:,.2f} | Avg loss: ${m.avg_loss:,.2f}",
        f"  Best: ${m.best_trade:,.2f} | Worst: ${m.worst_trade:,.2f}",
        f"  Profit factor: {m.profit_factor:.2f}",
        f"  Streaks: {m.max_win_streak}W max / {m.max_loss_streak}L max",
        "",
        "<b>Risk Metrics</b>",
        f"  Sharpe: {m.sharpe_ratio:.2f}",
        f"  Sortino: {m.sortino_ratio:.2f}",
        f"  Calmar: {m.calmar_ratio:.2f}",
        f"  Max DD: {m.max_drawdown_pct:.2%}",
        f"  Current DD: {m.current_drawdown_pct:.2%}",
        f"  Daily vol: {m.daily_vol:.2%} | Ann. vol: {m.annualised_vol:.1%}",
        "",
        "<b>Edge</b>",
        f"  Expectancy: ${m.expectancy:,.2f}/trade",
        f"  Expectancy ratio: {m.expectancy_ratio:.2f}",
    ]

    exec_lines = _format_execution_quality(m)
    if exec_lines:
        lines.append("")
        lines.extend(exec_lines)

    return "\n".join(lines)


def format_telegram_summary(m: DailyMetrics) -> str:
    """Short summary for the report group chat."""
    pnl_sign = "+" if m.trade_pnl >= 0 else ""

    call_btc = config.QTY_PER_LEG * m.num_straddles
    put_btc = config.QTY_PER_LEG * m.num_straddles
    total_btc = call_btc + put_btc

    # Strike line shows spot at entry (and exit, if captured) so the
    # reader can immediately verify the strike was ITM / picked sanely.
    strike_line = f"  Strike: ${m.strike:,.0f}"
    if m.entry_spot > 0:
        strike_line += f"  (spot ${m.entry_spot:,.0f}"
        if m.exit_spot > 0:
            strike_line += f" -> ${m.exit_spot:,.0f}"
        strike_line += ")"

    # Equity line shows starting and ending side by side.
    if m.starting_equity > 0:
        equity_line = (
            f"  Equity: ${m.starting_equity:,.2f} -> ${m.equity:,.2f}"
        )
    else:
        equity_line = f"  Equity: ${m.equity:,.2f}"

    lines = [
        f"<b>TRADE SUMMARY — {m.trade_date}</b>",
        "",
        "<b>Today's Trade</b>",
        f"  P&L: {pnl_sign}${m.trade_pnl:,.2f} "
        f"({pnl_sign}{m.trade_return_pct:.2%})",
        strike_line,
        f"  Call: ${m.call_premium_entry:,.2f} → "
        f"${m.call_premium_exit:,.2f}",
        f"  Put: ${m.put_premium_entry:,.2f} → ${m.put_premium_exit:,.2f}",
        equity_line,
        "",
        "<b>Volume</b>",
        f"  Straddles: {m.num_straddles}",
        f"  Calls: {m.num_straddles} × {config.QTY_PER_LEG} = "
        f"{call_btc:.1f} BTC",
        f"  Puts: {m.num_straddles} × {config.QTY_PER_LEG} = "
        f"{put_btc:.1f} BTC",
        f"  Total: {total_btc:.1f} BTC",
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
    cum_return = (
        (equity - config.INITIAL_CAPITAL_USD) / config.INITIAL_CAPITAL_USD
    )

    total_straddles = sum(t.num_straddles for t in trades)

    return DailyMetrics(
        trade_date=week_start,
        trade_pnl=sum(pnls),
        trade_return_pct=weekly_return,
        strike=latest.strike,
        num_straddles=total_straddles,
        equity=equity,
        initial_capital=config.INITIAL_CAPITAL_USD,
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
    )


def format_weekly_report(m: DailyMetrics) -> str:
    pnl_sign = "+" if m.trade_pnl >= 0 else ""

    call_btc = config.QTY_PER_LEG * m.num_straddles
    put_btc = config.QTY_PER_LEG * m.num_straddles

    lines = [
        f"<b>WEEKLY REPORT — Week of {m.trade_date}</b>",
        "",
        f"  Weekly P&L: {pnl_sign}${m.trade_pnl:,.2f} "
        f"({pnl_sign}{m.trade_return_pct:.2%})",
        f"  Trades: {m.total_trades} ({m.wins}W / {m.losses}L)",
        f"  Equity: ${m.equity:,.2f}",
        f"  Cumulative: {m.cumulative_return_pct:+.1%}",
        "",
        "<b>Volume (this week)</b>",
        f"  Straddles: {m.num_straddles}",
        f"  Calls: {call_btc:.1f} BTC",
        f"  Puts: {put_btc:.1f} BTC",
    ]
    return "\n".join(lines)
