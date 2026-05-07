"""
Equity tracking, position state, and trade logging.

Compound sizing: equity grows/shrinks with each trade's realised P&L.
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Optional

import structlog

import config
from utils.time_utils import now_utc

log = structlog.get_logger(__name__)

TRADE_LOG_FIELDS = [
    "date", "entry_time", "exit_time", "exit_reason",
    "num_straddles", "strike",
    "entry_spot", "exit_spot",
    "call_premium_entry", "call_premium_exit",
    "put_premium_entry", "put_premium_exit",
    "total_capital_used", "straddle_cost", "capital_before",
    "call_pnl", "put_pnl", "gross_pnl", "fees", "net_pnl",
    "capital_after",
    # Execution-quality metrics — entry
    "call_entry_duration_sec", "call_entry_attempts",
    "call_entry_ref_mark", "call_entry_ref_ask",
    "call_entry_slippage_vs_mark_pct",
    "call_entry_saved_vs_taker_usd",
    "put_entry_duration_sec", "put_entry_attempts",
    "put_entry_ref_mark", "put_entry_ref_ask",
    "put_entry_slippage_vs_mark_pct",
    "put_entry_saved_vs_taker_usd",
    # Execution-quality metrics — exit
    "call_exit_duration_sec", "call_exit_attempts",
    "call_exit_ref_mark", "call_exit_ref_bid",
    "call_exit_slippage_vs_mark_pct",
    "call_exit_saved_vs_taker_usd",
    "put_exit_duration_sec", "put_exit_attempts",
    "put_exit_ref_mark", "put_exit_ref_bid",
    "put_exit_slippage_vs_mark_pct",
    "put_exit_saved_vs_taker_usd",
]


@dataclass
class StraddleLeg:
    instrument: str
    side: str
    qty: float
    entry_price: float
    order_id: str = ""
    avg_fill_price: float = 0.0
    # Execution-quality metrics, captured at fill time. See
    # core.exchange.chase_buy / chase_sell for the keys produced.
    entry_metrics: dict = field(default_factory=dict)
    exit_metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Straddle:
    id: str
    call_leg: StraddleLeg
    put_leg: StraddleLeg
    strike: float
    qty_per_leg: float
    entry_time: str
    entry_call_price: float
    entry_put_price: float
    straddle_cost: float
    num_straddles: int

    # Spot prices captured for context (verify strike selection, exit
    # context). Optional — older state files may not have them.
    entry_spot_price: float = 0.0
    exit_spot_price: float = 0.0

    status: str = "open"
    exit_time: Optional[str] = None
    exit_call_price: Optional[float] = None
    exit_put_price: Optional[float] = None
    pnl: Optional[float] = None

    def call_pnl(self, call_now: float, exit_spot: float = 0.0) -> float:
        """USD P&L on the call leg.

        Premium quotes are in BTC per BTC of notional. To convert to USD we
        cost the entry leg at the entry spot and credit the exit leg at the
        exit spot — this is the USDT-account convention (account value rises
        when premium-in-BTC × spot rises). If spot is missing, we degrade
        gracefully to a single-spot model using whichever is available.
        """
        entry_spot = self.entry_spot_price
        spot_for_exit = exit_spot or self.exit_spot_price or entry_spot
        if entry_spot <= 0 and spot_for_exit <= 0:
            # No spot information at all — fall back to BTC-only number.
            # Better than crashing; the trade log will flag missing spots.
            return (
                self.qty_per_leg
                * (call_now - self.entry_call_price)
                * self.num_straddles
            )
        if entry_spot <= 0:
            entry_spot = spot_for_exit
        if spot_for_exit <= 0:
            spot_for_exit = entry_spot
        return (
            self.qty_per_leg
            * self.num_straddles
            * (call_now * spot_for_exit - self.entry_call_price * entry_spot)
        )

    def put_pnl(self, put_now: float, exit_spot: float = 0.0) -> float:
        entry_spot = self.entry_spot_price
        spot_for_exit = exit_spot or self.exit_spot_price or entry_spot
        if entry_spot <= 0 and spot_for_exit <= 0:
            return (
                self.qty_per_leg
                * (put_now - self.entry_put_price)
                * self.num_straddles
            )
        if entry_spot <= 0:
            entry_spot = spot_for_exit
        if spot_for_exit <= 0:
            spot_for_exit = entry_spot
        return (
            self.qty_per_leg
            * self.num_straddles
            * (put_now * spot_for_exit - self.entry_put_price * entry_spot)
        )

    def combined_pnl(
        self, call_now: float, put_now: float, exit_spot: float = 0.0,
    ) -> float:
        return (
            self.call_pnl(call_now, exit_spot)
            + self.put_pnl(put_now, exit_spot)
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "call_leg": self.call_leg.to_dict(),
            "put_leg": self.put_leg.to_dict(),
            "strike": self.strike,
            "qty_per_leg": self.qty_per_leg,
            "entry_time": self.entry_time,
            "entry_call_price": self.entry_call_price,
            "entry_put_price": self.entry_put_price,
            "straddle_cost": self.straddle_cost,
            "num_straddles": self.num_straddles,
            "entry_spot_price": self.entry_spot_price,
            "exit_spot_price": self.exit_spot_price,
            "status": self.status,
            "exit_time": self.exit_time,
            "exit_call_price": self.exit_call_price,
            "exit_put_price": self.exit_put_price,
            "pnl": self.pnl,
        }


class Portfolio:
    """Tracks equity and the current open straddle (at most one per day)."""

    def __init__(self) -> None:
        self._equity: float = config.INITIAL_CAPITAL_USD
        self._straddle: Optional[Straddle] = None
        self._daily_pnl: float = 0.0
        self._load_equity()

    @property
    def equity(self) -> float:
        return self._equity

    def sync_equity(self, live_equity: float) -> None:
        """Sync internal equity with the live OKX trading-account balance."""
        if live_equity <= 0:
            log.warning("sync_equity_skipped", live_equity=live_equity)
            return
        old = self._equity
        self._equity = live_equity
        self._save_equity()
        log.info("equity_synced",
                 old=f"${old:,.2f}", live=f"${live_equity:,.2f}",
                 delta=f"${live_equity - old:,.2f}")

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def has_open(self) -> bool:
        return self._straddle is not None and self._straddle.status == "open"

    @property
    def open_straddle(self) -> Optional[Straddle]:
        return self._straddle if self.has_open else None

    def set_straddle(self, s: Straddle) -> None:
        self._straddle = s
        self._save_positions()

    def close_straddle(
        self, exit_call_price: float, exit_put_price: float, exit_reason: str,
    ) -> float:
        s = self._straddle
        if s is None or s.status != "open":
            return 0.0

        # USD P&L using entry_spot for the entry leg and exit_spot for the
        # exit leg (set on Straddle by unwind_straddle just before close).
        pnl = s.combined_pnl(
            exit_call_price, exit_put_price, exit_spot=s.exit_spot_price,
        )
        s.status = "closed"
        s.exit_time = now_utc().isoformat()
        s.exit_call_price = exit_call_price
        s.exit_put_price = exit_put_price
        s.pnl = pnl

        self._equity += pnl
        self._daily_pnl += pnl
        self._save_equity()
        self._save_positions()
        self._log_trade(s, exit_reason)

        log.info("straddle_closed",
                 pnl=f"${pnl:,.2f}", equity=f"${self._equity:,.2f}")
        return pnl

    def reset_daily(self) -> None:
        self._daily_pnl = 0.0
        self._straddle = None
        self._save_positions()

    # ──────────────── Persistence ─────────────────────────────────

    def _save_equity(self) -> None:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(config.EQUITY_FILE, "w") as f:
            json.dump({"equity": self._equity}, f)

    def _load_equity(self) -> None:
        if os.path.exists(config.EQUITY_FILE):
            try:
                with open(config.EQUITY_FILE) as f:
                    self._equity = json.load(f).get(
                        "equity", config.INITIAL_CAPITAL_USD,
                    )
                log.info("equity_loaded", equity=self._equity)
            except Exception:
                log.warning("equity_load_failed", exc_info=True)

    def _save_positions(self) -> None:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        data = self._straddle.to_dict() if self._straddle else None
        with open(config.POSITIONS_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def _log_trade(self, s: Straddle, exit_reason: str) -> None:
        os.makedirs(config.STATE_DIR, exist_ok=True)

        # Convert BTC-quoted premiums to USD using the spot at the moment of
        # the relevant fill (entry_spot for entry legs, exit_spot for exit
        # legs). Daily/weekly reports read these CSV columns as USD amounts.
        entry_spot = s.entry_spot_price or 0.0
        exit_spot = s.exit_spot_price or entry_spot or 0.0
        call_entry_usd = (s.entry_call_price or 0.0) * entry_spot
        put_entry_usd = (s.entry_put_price or 0.0) * entry_spot
        call_exit_usd = (s.exit_call_price or 0.0) * exit_spot
        put_exit_usd = (s.exit_put_price or 0.0) * exit_spot
        straddle_cost_usd = (call_entry_usd + put_entry_usd) * s.qty_per_leg
        total_capital_used = straddle_cost_usd * s.num_straddles
        call_pnl_usd = s.call_pnl(
            s.exit_call_price or s.entry_call_price, exit_spot=exit_spot,
        )
        put_pnl_usd = s.put_pnl(
            s.exit_put_price or s.entry_put_price, exit_spot=exit_spot,
        )

        ce = s.call_leg.entry_metrics or {}
        cx = s.call_leg.exit_metrics or {}
        pe = s.put_leg.entry_metrics or {}
        px = s.put_leg.exit_metrics or {}

        row = {
            "date": s.entry_time[:10],
            "entry_time": s.entry_time,
            "exit_time": s.exit_time,
            "exit_reason": exit_reason,
            "num_straddles": s.num_straddles,
            "strike": s.strike,
            "entry_spot": s.entry_spot_price,
            "exit_spot": s.exit_spot_price,
            "call_premium_entry": round(call_entry_usd, 4),
            "call_premium_exit": round(call_exit_usd, 4),
            "put_premium_entry": round(put_entry_usd, 4),
            "put_premium_exit": round(put_exit_usd, 4),
            "total_capital_used": round(total_capital_used, 2),
            "straddle_cost": round(straddle_cost_usd, 4),
            "capital_before": self._equity - (s.pnl or 0),
            "call_pnl": round(call_pnl_usd, 2),
            "put_pnl": round(put_pnl_usd, 2),
            "gross_pnl": s.pnl,
            "fees": 0.0,
            "net_pnl": s.pnl,
            "capital_after": self._equity,
            # Entry execution metrics
            "call_entry_duration_sec": ce.get("duration_sec", ""),
            "call_entry_attempts": ce.get("attempts", ""),
            "call_entry_ref_mark": ce.get("ref_mark", ""),
            "call_entry_ref_ask": ce.get("ref_ask", ""),
            "call_entry_slippage_vs_mark_pct": ce.get("slippage_vs_mark_pct", ""),
            "call_entry_saved_vs_taker_usd": ce.get("saved_vs_taker_total_usd", ""),
            "put_entry_duration_sec": pe.get("duration_sec", ""),
            "put_entry_attempts": pe.get("attempts", ""),
            "put_entry_ref_mark": pe.get("ref_mark", ""),
            "put_entry_ref_ask": pe.get("ref_ask", ""),
            "put_entry_slippage_vs_mark_pct": pe.get("slippage_vs_mark_pct", ""),
            "put_entry_saved_vs_taker_usd": pe.get("saved_vs_taker_total_usd", ""),
            # Exit execution metrics
            "call_exit_duration_sec": cx.get("duration_sec", ""),
            "call_exit_attempts": cx.get("attempts", ""),
            "call_exit_ref_mark": cx.get("ref_mark", ""),
            "call_exit_ref_bid": cx.get("ref_bid", ""),
            "call_exit_slippage_vs_mark_pct": cx.get("slippage_vs_mark_pct", ""),
            "call_exit_saved_vs_taker_usd": cx.get("saved_vs_taker_total_usd", ""),
            "put_exit_duration_sec": px.get("duration_sec", ""),
            "put_exit_attempts": px.get("attempts", ""),
            "put_exit_ref_mark": px.get("ref_mark", ""),
            "put_exit_ref_bid": px.get("ref_bid", ""),
            "put_exit_slippage_vs_mark_pct": px.get("slippage_vs_mark_pct", ""),
            "put_exit_saved_vs_taker_usd": px.get("saved_vs_taker_total_usd", ""),
        }

        needs_header = not os.path.exists(config.TRADE_LOG_FILE)
        with open(config.TRADE_LOG_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
            if needs_header:
                writer.writeheader()
            writer.writerow(row)
