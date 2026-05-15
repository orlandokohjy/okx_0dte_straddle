"""
OKX 0DTE Pure Straddle — Configuration.

All tunables in one place. Env-var overrides for deployment.

Default mode: Demo Trading (OKX_FLAG=1) so you can test safely.
For production, set OKX_FLAG=0 in .env.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, date, time, timedelta

# ──────────────────── OKX Credentials ─────────────────────────────
OKX_API_KEY: str = os.getenv("OKX_API_KEY", "")
OKX_API_SECRET: str = os.getenv("OKX_API_SECRET", "")
OKX_PASSPHRASE: str = os.getenv("OKX_PASSPHRASE", "")

# OKX_FLAG: "0" = live trading, "1" = demo trading (paper money)
# Default to demo for safety. Override to "0" only when ready for live.
OKX_FLAG: str = os.getenv("OKX_FLAG", "1")

# OKX regional endpoint:
#   "https://www.okx.com"   — global (default)
#   "https://my.okx.com"    — OKX Singapore (SG-licensed users)
#   "https://app.okx.com"   — alt regional gateway
# Keys are scoped per-region; using the wrong domain returns 50119.
OKX_DOMAIN: str = os.getenv("OKX_DOMAIN", "https://www.okx.com")

DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"

# When true, delete state/equity.json + state/positions.json on boot. Useful
# for a clean cutover from demo state to a live deployment so we don't carry
# the demo's $5,000 equity over. Auto-disables on first boot like ENTRY_NOW.
RESET_STATE_ON_BOOT: bool = os.getenv(
    "RESET_STATE_ON_BOOT", "false",
).lower() == "true"

# True iff API credentials are present. Used to gate startup-time auth calls
# (reconcile, equity sync) independently from DRY_RUN. With creds + DRY_RUN
# we still validate auth on startup, just don't place orders.
HAS_OKX_CREDS: bool = bool(OKX_API_KEY and OKX_API_SECRET and OKX_PASSPHRASE)

# ──────────────────── Strategy Constants ──────────────────────────
BASE_COIN: str = "BTC"

# Option family selector.
#   OPTION_FAMILY=CM (default) → BTC-USD inverse / coin-margined
#                                premiums quoted in BTC, settle in BTC.
#   OPTION_FAMILY=UM           → BTC-USD_UM linear / USD-margined
#                                premiums quoted in USD, settle in USD.
#
# All family-aware behaviour (instId building, tick rounding, fee unit,
# USD conversion) is centralised in ``core.family``. Keep this knob as
# the only OPS-facing toggle — flipping it is the canonical way to
# migrate the algo from CM to UM (or back).
OPTION_FAMILY: str = os.getenv("OPTION_FAMILY", "CM").upper()

# Legacy single-quote token. With OPTION_FAMILY=CM this resolves to
# "USD"; with UM it becomes "USD_UM". Kept exposed for backward-compat
# imports (``data/option_chain.py`` historically referenced it directly)
# — new code should import ``core.family`` and call ``quote_token()``.
QUOTE_COIN: str = os.getenv(
    "QUOTE_COIN", "USD_UM" if OPTION_FAMILY == "UM" else "USD",
)

# Legacy single-session BTC notional. Kept ONLY as a fallback for older
# trade-log rows that pre-date per-session qty_per_leg. New entries source
# qty_per_leg from the Session that fired them — see SESSIONS below.
QTY_PER_LEG: float = float(os.getenv("QTY_PER_LEG", "0.5"))

# OKX BTC options: 1 contract = 0.01 BTC of underlying notional, for
# both CM and UM families on the user's account (verified empirically
# 2026-05-15 from the OKX UI: 50 contracts displayed as 0.5 BTC).
# Override per-family via OKX_CONTRACT_SIZE_BTC (CM) or
# OKX_CONTRACT_SIZE_BTC_UM (UM) if your account behaves differently.
OKX_CONTRACT_SIZE_BTC: float = float(os.getenv(
    "OKX_CONTRACT_SIZE_BTC_UM" if OPTION_FAMILY == "UM"
    else "OKX_CONTRACT_SIZE_BTC",
    "0.01",
))

# Trading mode for OKX OPTION orders.
# OKX rejects `cash` for OPTION instType (cash is spot only), so this MUST
# be one of:
#   "isolated" — Isolated-margin per position (REQUIRED for long-only buys)
#   "cross"    — Cross-margin (only valid for SHORT options / selling premium;
#                returns sCode 51019 for long buys: "No net long positions
#                can be held under cross margin mode in options").
# This algo is long-only (long call + long put), so default is `isolated`.
OKX_TD_MODE: str = os.getenv("OKX_TD_MODE", "isolated")

INITIAL_CAPITAL_USD: float = float(os.getenv("INITIAL_CAPITAL_USD", "8000.0"))
ALLOC_PCT: float = 0.80
NUM_STRADDLES_OVERRIDE: int = int(os.getenv("NUM_STRADDLES_OVERRIDE", "1"))


# ──────────────────── Multi-Session Schedule (UTC) ────────────────
#
# OKX BTC 0DTE options expire daily at 08:00 UTC. We define a "trading
# day" as the calendar UTC date of that expiry. A trading day's two
# entries straddle the previous-day boundary:
#
#   • FIRST entry  : afternoon, 13:30-15:30 UTC the day BEFORE expiry
#                    (size = 0.50 BTC notional / leg)
#   • SECOND entry : morning,   01:00-02:00 UTC the day OF expiry
#                    (size = 0.25 BTC notional / leg)
#
# Both sessions share the SAME straddle structure (1 ITM call + 1 ITM
# put at the same strike) and post to the same trade log so the daily
# report and combined DAILY SUMMARY telegram aggregate by trading_day.
#
# Per-session weekday filters give us 5 complete trading-day pairs
# per week (Tue, Wed, Thu, Fri, Sat) for a total of 10 trades:
#   • afternoon (1st) fires Mon-Fri UTC  (covers Tue-Sat trading days)
#   • morning   (2nd) fires Tue-Sat UTC  (covers Tue-Sat trading days)
# Mon and Sun are dark by design — they would only ever produce
# half-pair trading days otherwise.
EXPIRY_CUTOFF_UTC: time = time(8, 0)


@dataclass(frozen=True)
class Session:
    name: str               # short identifier ("morning", "afternoon")
    entry_utc: time         # cron-style UTC hh:mm to fire entry
    close_utc: time         # cron-style UTC hh:mm to fire hard close
    qty_per_leg: float      # BTC notional per leg for THIS session
    weekdays: frozenset[int] = field(  # UTC weekdays (0=Mon..6=Sun) on
        default_factory=lambda: frozenset({0, 1, 2, 3, 4}),  # which to fire
    )

    @property
    def trading_day_offset_days(self) -> int:
        """Calendar-day offset from entry UTC date to expiry/trading day.

        Sessions that fire AT or AFTER the 08:00 UTC expiry cutoff
        trade options that expire the NEXT calendar day, so trading
        day = entry_date + 1. Sessions firing before 08:00 UTC trade
        same-day expiry, so trading day = entry_date.
        """
        return 1 if self.entry_utc >= EXPIRY_CUTOFF_UTC else 0

    @property
    def trading_day_close_position(self) -> tuple[int, int, int]:
        """Sortable key for ordering sessions WITHIN a trading day.

        Combines the trading-day offset with the close-time so we can
        identify which session closes LAST on a given trading day —
        that's the one that triggers the combined DAILY SUMMARY.
        """
        return (
            -self.trading_day_offset_days,  # earlier-day fires first
            self.close_utc.hour,
            self.close_utc.minute,
        )

    @property
    def time_label(self) -> str:
        """Human-friendly entry/close window for telegram messages.

        Example: ``13:30-15:30 UTC``. We use the timing window as the
        primary user-visible identifier (instead of "morning" /
        "afternoon") so the labels are unambiguous across timezones.
        """
        return (
            f"{self.entry_utc.strftime('%H:%M')}-"
            f"{self.close_utc.strftime('%H:%M')} UTC"
        )


SESSIONS: list[Session] = [
    # FIRST entry of each trading day — fires the day BEFORE expiry.
    # Mon-Fri UTC, so each fire creates a Tue-Sat trading day.
    Session(
        name="afternoon",
        entry_utc=time(13, 30),
        close_utc=time(15, 30),
        qty_per_leg=float(os.getenv("AFTERNOON_QTY_PER_LEG", "0.5")),
        weekdays=frozenset({0, 1, 2, 3, 4}),  # Mon-Fri UTC
    ),
    # SECOND entry of each trading day — fires the same day as expiry.
    # Tue-Sat UTC, pairs 1:1 with the afternoon entries above.
    Session(
        name="morning",
        entry_utc=time(1, 0),
        close_utc=time(2, 0),
        qty_per_leg=float(os.getenv("MORNING_QTY_PER_LEG", "0.25")),
        weekdays=frozenset({1, 2, 3, 4, 5}),  # Tue-Sat UTC
    ),
]


def trading_day_for(entry_dt: datetime) -> date:
    """Return the trading day (= 0DTE expiry date) for a given UTC
    entry timestamp.

    Sessions firing at/after 08:00 UTC trade NEXT-day expiry options,
    so trading day = entry_dt.date() + 1. Sessions firing before 08:00
    UTC trade SAME-day expiry, so trading day = entry_dt.date().
    """
    cutoff = EXPIRY_CUTOFF_UTC
    if (entry_dt.hour, entry_dt.minute) >= (cutoff.hour, cutoff.minute):
        return (entry_dt + timedelta(days=1)).date()
    return entry_dt.date()


def _last_close_session_name(sessions: list[Session]) -> str:
    """The session whose close time is the LAST event of a trading day.

    Sorted by trading-day position so afternoon (-1d, 15:30) ranks
    BEFORE morning (0d, 02:00). The maximum of that ordering is the
    last close — that's the only session that triggers the combined
    daily summary.
    """
    if not sessions:
        return ""
    return max(sessions, key=lambda s: s.trading_day_close_position).name


LAST_CLOSE_SESSION_NAME: str = _last_close_session_name(SESSIONS)


def get_session(name: str) -> Session | None:
    """Lookup a session by name, or None if not configured."""
    for s in SESSIONS:
        if s.name == name:
            return s
    return None


# Reports are chained off the morning close (= last close of each
# trading day) by main._on_close — see scheduler.py for the entry/close
# cron jobs and main.py for the chained-report logic. These two values
# are kept for backward compatibility / introspection only.
REPORT_UTC: time = time(2, 5)        # informational: ~5 min after morning close
WEEKLY_REPORT_UTC: time = time(2, 10) # informational: ~10 min after Sat morning close
ALLOWED_WEEKDAYS: set[int] = {0, 1, 2, 3, 4}  # Mon–Fri (legacy default)

# ──────────────────── Execution Settings ──────────────────────────
OPTION_CHASE_INTERVAL_SEC: float = 5.0

# Default tick size fallback. The authoritative tick comes from
# /api/v5/public/instruments on startup — this is just the boot-time
# default if the API is unreachable. Family-specific:
#   CM (BTC-USD)    : 0.0001 BTC across all strikes/expiries
#   UM (BTC-USD_UM) : 5 USD across all strikes/expiries
# Pick the right family default automatically; allow override via env.
OPTION_TICK_SIZE: float = float(os.getenv(
    "OPTION_TICK_SIZE", "5" if OPTION_FAMILY == "UM" else "0.0001",
))

# Sanity-check bounds for the chase-pricing self-test on startup. If the
# chase math yields a price outside these bounds for a real ITM option
# (relative to its mark), the algo aborts with a clear error rather than
# attempting orders. Guards against unit-conversion regressions like the
# OPTION_TICK_SIZE=5.0 (USD) bug.
#
# The absolute ceiling is family-dependent because the native unit is
# different:
#   CM: ≤ 0.5 BTC absolute (ITM premiums never exceed half a BTC)
#   UM: ≤ $50,000 absolute (premium in USD per BTC of notional;
#       the deepest ITM 0DTE on a $80k-spot day caps around $80k)
CHASE_SELFTEST_MAX_OVER_MARK: float = 1.5         # never > 1.5× mark (any unit)
CHASE_SELFTEST_MAX_ABSOLUTE_BTC: float = 0.5      # CM: ≤ 0.5 BTC
CHASE_SELFTEST_MAX_ABSOLUTE_USD: float = 50_000.0 # UM: ≤ $50k per BTC notional

# Maker-only chase: 50% bid-ask gap narrowing per retry, fair-value cap, deadline
OPTION_CHASE_GAP_NARROW_PCT: float = float(
    os.getenv("OPTION_CHASE_GAP_NARROW_PCT", "0.5")
)
OPTION_CHASE_MAX_SLIPPAGE_FACTOR: float = float(
    os.getenv("OPTION_CHASE_MAX_SLIPPAGE_FACTOR", "1.15")
)
# ── Maker-chase deadlines: split per direction ──
# Entry chase (chase_buy) MUST finish within the session window. The
# morning session is only 60 minutes long (01:00 → 02:00 UTC), so the
# entry deadline cannot exceed 60 min without risking a race where the
# entry completes after the scheduled session close, leaving us holding
# a straddle with no scheduled unwind handler.
#
# Exit chase (chase_sell) is free to run past session close — there is
# no scheduler race, only the underlying option's 08:00 UTC expiry as
# the hard ceiling. Giving it ~2 hours dramatically improves fill quality
# in dying 0DTE books where the spread can sit one tick wide for tens of
# minutes before the ask collapses.
#
# Legacy single-knob `OPTION_CHASE_DEADLINE_MIN` is honored as a
# fallback so existing deployments keep working without an env edit.
_LEGACY_CHASE_DEADLINE_MIN = os.getenv("OPTION_CHASE_DEADLINE_MIN")
OPTION_ENTRY_CHASE_DEADLINE_MIN: float = float(
    os.getenv(
        "OPTION_ENTRY_CHASE_DEADLINE_MIN",
        _LEGACY_CHASE_DEADLINE_MIN if _LEGACY_CHASE_DEADLINE_MIN else "60.0",
    )
)
OPTION_EXIT_CHASE_DEADLINE_MIN: float = float(
    os.getenv(
        "OPTION_EXIT_CHASE_DEADLINE_MIN",
        _LEGACY_CHASE_DEADLINE_MIN if _LEGACY_CHASE_DEADLINE_MIN else "120.0",
    )
)
# Kept for backward-compatibility imports; new code should reference
# OPTION_ENTRY_CHASE_DEADLINE_MIN or OPTION_EXIT_CHASE_DEADLINE_MIN.
OPTION_CHASE_DEADLINE_MIN: float = OPTION_EXIT_CHASE_DEADLINE_MIN

# Pre-entry spread gate: skip session if put or call (ask − bid) / mid > this
OPTION_MAX_ENTRY_SPREAD_PCT: float = float(
    os.getenv("OPTION_MAX_ENTRY_SPREAD_PCT", "0.30")
)

# RFQ (Block Trading) — disabled by default. Requires Block Trading
# entitlement on the account (live), or demo flag with limited support.
# When enabled, builder tries RFQ first and falls back to leg-by-leg chase.
USE_RFQ: bool = os.getenv("USE_RFQ", "false").lower() == "true"

# Seconds to wait for counterparty quotes after submitting an RFQ before
# giving up and falling back to leg-by-leg chase.
RFQ_QUOTE_WAIT_SEC: int = int(os.getenv("RFQ_QUOTE_WAIT_SEC", "20"))

# ──────────────────── Risk Management ─────────────────────────────
MAX_DAILY_LOSS_PCT: float | None = None
CIRCUIT_BREAKER_API_ERRORS: int = 5
CIRCUIT_BREAKER_COOLDOWN_SEC: float = 300.0

# Pre-entry collateral safety buffer — entry skipped unless available
# trading-account balance ≥ expected_premium × this factor.
COLLATERAL_BUFFER_FACTOR: float = float(
    os.getenv("COLLATERAL_BUFFER_FACTOR", "1.2")
)

# Lock the algo after this many consecutive session failures.
CONSECUTIVE_FAILURE_LIMIT: int = int(
    os.getenv("CONSECUTIVE_FAILURE_LIMIT", "3")
)

# ──────────────────── Telegram ────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_REPORT_BOT_TOKEN: str = os.getenv("TELEGRAM_REPORT_BOT_TOKEN", "")
TELEGRAM_REPORT_CHAT_ID: str = os.getenv("TELEGRAM_REPORT_CHAT_ID", "")
TELEGRAM_ENABLED: bool = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

# ──────────────────── Logging & Persistence ───────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_JSON: bool = True
LOG_FILE: str = "logs/algo.log"
STATE_DIR: str = "state"
EQUITY_FILE: str = f"{STATE_DIR}/equity.json"
POSITIONS_FILE: str = f"{STATE_DIR}/positions.json"
TRADE_LOG_FILE: str = f"{STATE_DIR}/trade_log.csv"
VOLUME_FILE: str = f"{STATE_DIR}/volume.csv"
