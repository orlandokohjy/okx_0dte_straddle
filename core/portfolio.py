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
from core import family
from utils.time_utils import now_utc

log = structlog.get_logger(__name__)

TRADE_LOG_FIELDS = [
    "date", "session", "family", "qty_per_leg",
    "entry_time", "exit_time", "exit_reason",
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
    # Implied volatility (OKX mark IV, decimal — 0.58 = 58%) captured
    # best-effort at entry and exit. Blank when the snapshot was
    # unavailable; never affects execution.
    "call_entry_iv", "put_entry_iv",
    "call_exit_iv", "put_exit_iv",
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

    # Which Session fired this straddle. Canonical names are utc_HHMM
    # (utc_0900 / utc_1330 / utc_2330 / utc_0100). Pre-2026-05-20 rows
    # may carry the legacy "morning" / "afternoon" aliases; reports
    # canonicalise those at read-time. Empty for legacy single-session
    # state files; reports treat empty as "unknown" but still display
    # the row.
    session_name: str = ""

    # Option family at the time of entry — frozen on the Straddle so a
    # mid-flight family change (operator restart with a new env var)
    # cannot break the close-out P&L. Empty string is treated as "CM"
    # (legacy default) by ``call_pnl`` for backward compat with existing
    # positions.json files written before the family abstraction landed.
    family: str = ""

    # Spot prices captured for context (verify strike selection, exit
    # context). Optional — older state files may not have them.
    entry_spot_price: float = 0.0
    exit_spot_price: float = 0.0

    # OKX mark implied vol (decimal, 0.58 = 58%) captured best-effort at
    # entry and exit for analytics. 0.0 means "not captured" (snapshot
    # unavailable) — purely informational, never used in P&L or execution.
    entry_call_iv: float = 0.0
    entry_put_iv: float = 0.0
    exit_call_iv: float = 0.0
    exit_put_iv: float = 0.0

    status: str = "open"
    exit_time: Optional[str] = None
    exit_call_price: Optional[float] = None
    exit_put_price: Optional[float] = None
    pnl: Optional[float] = None  # NET P&L (gross − fees)
    gross_pnl: Optional[float] = None  # Pre-fee P&L (call + put leg P&L)
    fees: Optional[float] = None  # Total maker fees across all 4 legs (USD)

    def _is_um(self) -> bool:
        """Treat empty-string family as CM (legacy)."""
        return (self.family or "CM").upper() == "UM"

    def call_pnl(self, call_now: float, exit_spot: float = 0.0) -> float:
        """USD P&L on the call leg.

        Two pricing conventions co-exist depending on the option family
        the straddle was opened against:

          CM (BTC-USD inverse): premiums quoted in BTC per BTC of
              notional. USD value = price × spot. We cost the entry
              leg at entry-time spot and credit the exit leg at
              exit-time spot — the USDT-account convention where
              account value rises when premium-in-BTC × spot rises.

          UM (BTC-USD_UM linear): premiums already in USD per BTC of
              notional. USD value = price × 1. P&L is the direct
              difference, no spot multiplication. Eliminates BTC
              currency drift entirely.

        Falls back gracefully when spot information is missing (legacy
        state files written without spot-capture).
        """
        if self._is_um():
            return (
                self.qty_per_leg
                * self.num_straddles
                * (call_now - self.entry_call_price)
            )
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
        if self._is_um():
            return (
                self.qty_per_leg
                * self.num_straddles
                * (put_now - self.entry_put_price)
            )
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
            "session_name": self.session_name,
            "family": self.family,
            "entry_spot_price": self.entry_spot_price,
            "exit_spot_price": self.exit_spot_price,
            "entry_call_iv": self.entry_call_iv,
            "entry_put_iv": self.entry_put_iv,
            "exit_call_iv": self.exit_call_iv,
            "exit_put_iv": self.exit_put_iv,
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
        # Single-mode compat: the one open straddle (legacy). In stacked
        # mode this is unused — `_open` holds every open straddle keyed by
        # session_name, and `_last_closed` carries the most-recent close
        # for reporting. Single mode keeps `_open` in sync (≤1 entry) so
        # every accessor works identically regardless of mode.
        self._straddle: Optional[Straddle] = None
        self._open: dict[str, Straddle] = {}
        self._last_closed: Optional[Straddle] = None
        self._daily_pnl: float = 0.0
        self._load_equity()
        self._migrate_trade_log()

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
        return len(self._open) > 0

    @property
    def open_count(self) -> int:
        return len(self._open)

    def open_straddles(self) -> list[Straddle]:
        """Every currently-open straddle (0..N in stacked mode)."""
        return list(self._open.values())

    def get_open(self, session_name: str) -> Optional[Straddle]:
        """The open straddle fired by `session_name`, or None."""
        return self._open.get(session_name)

    @property
    def open_straddle(self) -> Optional[Straddle]:
        """Back-compat single-straddle accessor. Returns the sole open
        straddle when exactly one is open; in stacked mode with several
        open it returns the most recently opened (callers that must target
        a specific session should use ``get_open``)."""
        if not self._open:
            return None
        vals = list(self._open.values())
        return vals[0] if len(vals) == 1 else vals[-1]

    @property
    def last_closed_straddle(self) -> Optional[Straddle]:
        """The most recently closed Straddle, or None if no straddle has
        ever been closed. Used by the SESSION CLOSE Telegram message to
        render entry/exit detail."""
        if self._last_closed is None or self._last_closed.status != "closed":
            return None
        return self._last_closed

    def expected_open_contracts(
        self, exclude_session: Optional[str] = None,
    ) -> dict[str, float]:
        """Signed net contracts per instrument summed over all OPEN
        straddles (long straddle ⇒ +contracts on both legs), optionally
        excluding one session. Contracts = leg BTC qty / contract size, to
        match the exchange ``amount`` field. Used by the stacked-mode
        reconcile to avoid touching legs that legitimately belong to a
        still-open sibling straddle.
        """
        ct = config.OKX_CONTRACT_SIZE_BTC or 1.0
        agg: dict[str, float] = {}
        for name, s in self._open.items():
            if exclude_session is not None and name == exclude_session:
                continue
            if s.status != "open":
                continue
            for leg in (s.call_leg, s.put_leg):
                # A straddle is long both legs; qty is stored positive.
                agg[leg.instrument] = agg.get(leg.instrument, 0.0) + (
                    leg.qty / ct
                )
        return agg

    def set_straddle(self, s: Straddle) -> None:
        self._straddle = s
        self._open[s.session_name] = s
        self._save_positions()

    def close_straddle(
        self, exit_call_price: float, exit_put_price: float, exit_reason: str,
        session_name: Optional[str] = None,
    ) -> float:
        # Resolve WHICH straddle to close. In stacked mode the caller passes
        # the owning session so we book/reduce exactly that straddle; single
        # mode falls back to the sole open straddle.
        if session_name is not None:
            s = self._open.get(session_name)
        else:
            s = self.open_straddle
        if s is None or s.status != "open":
            return 0.0

        # USD P&L using entry_spot for the entry leg and exit_spot for the
        # exit leg (set on Straddle by unwind_straddle just before close).
        gross_pnl = s.combined_pnl(
            exit_call_price, exit_put_price, exit_spot=s.exit_spot_price,
        )
        # Sum signed maker fees across all 4 legs (BTC fees converted to
        # USD at fill-time spot by the exchange layer). The sign follows
        # OKX: negative = fee paid (cost), positive = maker rebate
        # received (credit). ADDING to gross gives a net P&L that matches
        # the wallet equity to within MTM/settlement drift only. On a VIP
        # tier with a maker rebate (post_only fills), total_fees_usd is
        # positive and correctly increases net P&L.
        ce = s.call_leg.entry_metrics or {}
        cx = s.call_leg.exit_metrics or {}
        pe = s.put_leg.entry_metrics or {}
        px = s.put_leg.exit_metrics or {}
        total_fees_usd = (
            float(ce.get("fee_usd") or 0.0)
            + float(cx.get("fee_usd") or 0.0)
            + float(pe.get("fee_usd") or 0.0)
            + float(px.get("fee_usd") or 0.0)
        )
        net_pnl = gross_pnl + total_fees_usd

        s.status = "closed"
        s.exit_time = now_utc().isoformat()
        s.exit_call_price = exit_call_price
        s.exit_put_price = exit_put_price
        s.pnl = net_pnl  # ← ledger snapshot now matches wallet behaviour
        s.gross_pnl = gross_pnl
        s.fees = total_fees_usd

        # Remove from the open set and remember it for reporting. In single
        # mode this leaves `_open` empty; in stacked mode any sibling
        # straddles stay open and untouched.
        self._open.pop(s.session_name, None)
        self._last_closed = s
        if self._straddle is s:
            self._straddle = None

        self._equity += net_pnl
        self._daily_pnl += net_pnl
        self._save_equity()
        self._save_positions()
        self._log_trade(s, exit_reason, gross_pnl=gross_pnl, fees=total_fees_usd)

        log.info("straddle_closed",
                 gross_pnl=f"${gross_pnl:,.2f}",
                 fees=f"${total_fees_usd:,.2f}",
                 net_pnl=f"${net_pnl:,.2f}",
                 equity=f"${self._equity:,.2f}")
        return net_pnl

    def reset_daily(self) -> None:
        # Reset the rolling daily P&L accumulator. Do NOT clear open
        # straddles here: close_straddle already removes a straddle when it
        # is booked, and in stacked mode a sibling straddle may still be
        # open when a different session closes (clearing `_open` here would
        # silently drop tracking of a live position).
        self._daily_pnl = 0.0
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
        # Stacked mode can hold several open straddles → persist a list so
        # the on-disk snapshot is observable. Single mode writes the sole
        # open straddle as a bare dict (legacy shape) for backward compat
        # with any external reader of positions.json.
        open_list = list(self._open.values())
        if config.STACKED_STRADDLES:
            data = [s.to_dict() for s in open_list]
        else:
            data = open_list[0].to_dict() if open_list else None
        with open(config.POSITIONS_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def _log_trade(
        self, s: Straddle, exit_reason: str,
        *, gross_pnl: float = 0.0, fees: float = 0.0,
    ) -> None:
        """Append one trade row to ``trade_log.csv``.

        ``gross_pnl`` and ``fees`` are passed in by ``close_straddle`` so
        the row's gross/net/fees columns align with the wallet-equity
        update done at the same moment (``s.pnl`` carries the NET, which
        is what ``self._equity`` was just bumped by). Defaults of 0.0
        keep legacy unit tests / ad-hoc invocations working.
        """
        os.makedirs(config.STATE_DIR, exist_ok=True)

        # Normalize premiums to USD-per-BTC-of-notional for the CSV so
        # downstream reports can read raw numeric columns without family
        # awareness.
        #   CM: native BTC-quoted, multiply by spot at the relevant fill
        #   UM: native already USD-per-BTC, store as-is
        # Daily/weekly reports read these as USD amounts and the math
        # works for both families.
        entry_spot = s.entry_spot_price or 0.0
        exit_spot = s.exit_spot_price or entry_spot or 0.0
        is_um = (s.family or "CM").upper() == "UM"
        if is_um:
            call_entry_usd = s.entry_call_price or 0.0
            put_entry_usd = s.entry_put_price or 0.0
            call_exit_usd = s.exit_call_price or 0.0
            put_exit_usd = s.exit_put_price or 0.0
        else:
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

        # Fall back to summing leg metrics if caller didn't pass them in
        # (legacy callers or ad-hoc replays).
        if gross_pnl == 0.0 and fees == 0.0:
            fees = (
                float(ce.get("fee_usd") or 0.0)
                + float(cx.get("fee_usd") or 0.0)
                + float(pe.get("fee_usd") or 0.0)
                + float(px.get("fee_usd") or 0.0)
            )
            # net = gross + signed_fees → back out gross = net − signed_fees
            gross_pnl = (s.pnl or 0.0) - fees
        net_pnl = s.pnl or 0.0  # always NET (wallet-aligned)

        row = {
            "date": s.entry_time[:10],
            "session": s.session_name,
            "family": (s.family or "CM").upper(),
            "qty_per_leg": s.qty_per_leg,
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
            # capital_before backs out NET P&L from the post-trade equity
            # so capital_after − capital_before == net_pnl exactly.
            "capital_before": round(self._equity - (s.pnl or 0), 2),
            "call_pnl": round(call_pnl_usd, 2),
            "put_pnl": round(put_pnl_usd, 2),
            "gross_pnl": round(gross_pnl, 2),
            "fees": round(fees, 2),
            "net_pnl": round(net_pnl, 2),
            "capital_after": round(self._equity, 2),
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
            # Implied vol (OKX mark IV, decimal) — best-effort; blank if the
            # snapshot was unavailable at entry/exit.
            "call_entry_iv": s.entry_call_iv or "",
            "put_entry_iv": s.entry_put_iv or "",
            "call_exit_iv": s.exit_call_iv or "",
            "put_exit_iv": s.exit_put_iv or "",
        }

        needs_header = not os.path.exists(config.TRADE_LOG_FILE)
        with open(config.TRADE_LOG_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
            if needs_header:
                writer.writeheader()
            writer.writerow(row)

    def _migrate_trade_log(self) -> None:
        """Rewrite an older trade-log CSV in place when the schema grows.

        Pre-multi-session deployments wrote rows without ``session`` /
        ``qty_per_leg`` columns. Appending new-format rows to such a
        file with csv.DictWriter would silently misalign columns with
        the header. This one-shot migration:

          • Detects an existing header missing the new columns.
          • Backfills the new columns: session = "" (legacy single
            session), qty_per_leg = legacy config.QTY_PER_LEG default.
          • Rewrites the file in-place with the new TRADE_LOG_FIELDS
            order so future appends line up.

        Safe to run on every boot — no-op when the schema already
        matches.
        """
        path = config.TRADE_LOG_FILE
        if not os.path.exists(path):
            return
        try:
            with open(path, newline="") as f:
                reader = csv.reader(f)
                rows = list(reader)
        except Exception:
            log.warning("trade_log_migration_read_failed",
                        path=path, exc_info=True)
            return

        if not rows:
            return

        existing_header = rows[0]
        if existing_header == TRADE_LOG_FIELDS:
            return

        missing = [c for c in TRADE_LOG_FIELDS if c not in existing_header]
        if not missing:
            log.info("trade_log_migration_skip_reorder_only",
                     existing=len(existing_header),
                     target=len(TRADE_LOG_FIELDS))

        old_rows = []
        for raw in rows[1:]:
            if len(raw) < len(existing_header):
                raw = raw + [""] * (len(existing_header) - len(raw))
            d = dict(zip(existing_header, raw))
            d.setdefault("session", "")
            d.setdefault("qty_per_leg", str(config.QTY_PER_LEG))
            # Pre-2026-05-15 trade-log rows are all CM by definition —
            # the UM family didn't exist as a deployment option yet.
            # Backfilling with "CM" lets the family-filter in the
            # reporting layer treat historical rows correctly without
            # any further migration step.
            d.setdefault("family", "CM")
            if not (d.get("family") or "").strip():
                d["family"] = "CM"
            old_rows.append(d)

        try:
            tmp_path = path + ".migrating"
            with open(tmp_path, "w", newline="") as f:
                writer = csv.DictWriter(
                    f, fieldnames=TRADE_LOG_FIELDS, extrasaction="ignore",
                )
                writer.writeheader()
                for d in old_rows:
                    writer.writerow({k: d.get(k, "") for k in TRADE_LOG_FIELDS})
            os.replace(tmp_path, path)
            log.info("trade_log_migrated",
                     path=path, rows=len(old_rows),
                     added_columns=missing)
        except Exception:
            log.warning("trade_log_migration_write_failed",
                        path=path, exc_info=True)
