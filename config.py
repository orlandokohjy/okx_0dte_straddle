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
# day" as the calendar UTC date of that expiry. Sessions are named by
# their UTC entry time so the schedule is unambiguous across timezones
# (a session's name like "morning" was ambiguous between UTC-morning
# and SGT-morning).
#
# WEEKDAY (Mon-Fri) — pct_equity sized — six trading days/wk (Tue-Sat)
# ───────────────────────────────────────────────────────────────────
#   utc_0900  →  09:00–09:30 UTC  Mon-Fri  ~23h to expiry   10% pct_equity
#   utc_1330  →  14:30–15:30 UTC  Mon-Fri  ~17.5h to expiry 25% pct_equity
#   utc_2330  →  23:30–24:00 UTC  Mon-Fri  ~8.5h to expiry  10% pct_equity
#   utc_0100  →  01:00–02:00 UTC  Tue-Sat  ~7h to expiry    50% pct_equity
#                                          ↑ LAST close → daily report
#
# WEEKEND (Sat-Sun) — fixed_btc 0.5 BTC sized — Sun + Mon trading days
# ───────────────────────────────────────────────────────────────────
#   utc_1430  →  14:30–15:30 UTC  Sat,Sun  ~17.5h to expiry 0.5 BTC fixed
#   utc_2230  →  23:00–24:00 UTC  Sat,Sun  ~9h to expiry    0.5 BTC fixed
#                                          ↑ LAST close on weekend
#                                            trading day → daily report
#                                          ↑ Sun→Mon close also triggers
#                                            the WEEKEND RECAP.
#
# Daily reports CHAIN off the LAST close of every trading day:
#   • Tue-Sat trading_days  →  utc_0100 close fires daily report.
#   • Sun trading_day        →  utc_2230 (Sat entry) close fires daily.
#   • Mon trading_day        →  utc_2230 (Sun entry) close fires daily
#                               AND the weekend recap (Sat+Sun trades).
# Weekly report still fires on Sat 02:00 UTC after utc_0100 Sat close,
# covering Mon-Fri entries only (weekend trades are reported via the
# weekend recap so weekday vs weekend strategy P&L stay separated).
#
# All sessions share the SAME straddle structure (1 ITM call + 1 ITM
# put at the same strike) and post to the same trade log. Sessions are
# independent: any one can fail without affecting the others.
#
# SIZING — TWO MODES PER SESSION
# -------------------------------
# Each Session has a sizing_mode ("fixed_btc" or "pct_equity"):
#
#   fixed_btc   → qty_per_leg is a hard BTC value. Same size every entry.
#   pct_equity  → premium-as-pct-of-equity. The qty_per_leg is computed
#                 at entry-time so that the straddle's expected USD
#                 premium ≈ pct_equity × current_equity. Each session's
#                 pct_equity can be overridden via env: <NAME>_PCT_EQUITY.
#
# The pct_equity formula (see strategy/sizing.py) interprets "x% of
# equity" as: max-loss-budget = x% of equity = expected straddle premium.
# This is the natural Kelly-style sizing for long straddles since premium
# paid IS the maximum theoretical loss.
#
# Switch a session between modes via env:
#   <NAME>_SIZING=pct_equity     (default) or "fixed_btc"
#   <NAME>_PCT_EQUITY=0.10       (10% — used when SIZING=pct_equity)
#   <NAME>_QTY_PER_LEG=0.25      (BTC — used when SIZING=fixed_btc, also
#                                  the fallback when pct_equity sizing
#                                  fails for any reason)
EXPIRY_CUTOFF_UTC: time = time(8, 0)


# Hard sanity cap on per-leg qty. Catches a runaway equity-tracking bug:
# if equity is mis-reported as some 100x value, the pct_equity math
# would otherwise produce enormous orders. With this cap the worst-case
# is bounded at MAX BTC per leg even if equity blows up.
#
# Default 5.0 BTC sized to leave headroom: at last-night's CM premium
# levels (~$1,360 USD per 1 BTC straddle) and current ~$7,761 equity,
# a 50% pct_equity session targets ~2.85 BTC. 5.0 BTC default lets
# equity grow ~75% before this cap binds. Operator should bump via
# MAX_QTY_PER_LEG_BTC=10.0 (or similar) once equity grows past ~$13k.
# When the cap DOES bind, sizing.py logs an INFO event so it's visible.
MAX_QTY_PER_LEG_BTC: float = float(os.getenv("MAX_QTY_PER_LEG_BTC", "5.0"))
# Lower bound below which we skip the session entirely. OKX's minimum
# is 1 contract = 0.01 BTC; below that we cannot place an order.
MIN_QTY_PER_LEG_BTC: float = float(os.getenv("MIN_QTY_PER_LEG_BTC", "0.01"))


def _session_env_str(name: str, key: str, default: str) -> str:
    """Read ``{NAME}_{KEY}`` from env (case-insensitive name) or default."""
    return os.getenv(f"{name.upper()}_{key.upper()}", default).strip()


def _session_env_float(name: str, key: str, default: float) -> float:
    """Read ``{NAME}_{KEY}`` as a float from env or fall back to default."""
    raw = _session_env_str(name, key, str(default))
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Session:
    """One scheduled trading session within a trading day.

    A trading day = the UTC date of the 08:00 UTC option expiry. A
    session enters and exits long-straddle positions on options
    expiring at that 08:00 UTC. Sizing is per-entry, governed by
    ``sizing_mode``.
    """
    name: str               # short identifier; canonical form is "utc_HHMM"
    entry_utc: time         # cron-style UTC hh:mm to fire entry
    close_utc: time         # cron-style UTC hh:mm to fire hard close.
                            # If close_utc < entry_utc, the close rolls
                            # over to the NEXT calendar day (e.g. utc_2330
                            # entry at Mon 23:30, close at Tue 00:00).
    qty_per_leg: float      # BTC qty per leg under sizing_mode=fixed_btc;
                            # ALSO the fallback qty if pct_equity sizing
                            # cannot resolve (e.g. zero equity, missing
                            # marks, or both bid/ask are 0).
    sizing_mode: str = "fixed_btc"   # "fixed_btc", "pct_equity" or "fixed_usd"
    pct_equity: float = 0.0          # used iff sizing_mode == "pct_equity"
    fixed_usd: float = 0.0           # target premium USD/entry iff
                                     # sizing_mode == "fixed_usd". The qty is
                                     # solved from live prices each entry so
                                     # the dollar allocation stays constant
                                     # regardless of equity; the implied
                                     # %-of-equity floats (= fixed_usd/equity).
    weekdays: frozenset[int] = field(  # UTC weekdays (0=Mon..6=Sun) for
        default_factory=lambda: frozenset({0, 1, 2, 3, 4}),  # ENTRY firing
    )
    enabled: bool = True             # if False, the scheduler does NOT
                                     # register entry/close jobs and the
                                     # session is excluded from "next-trading-
                                     # day" walkers + last-close detection.
                                     # Operator panic-button: set
                                     # <NAME>_ENABLED=false in .env to surgically
                                     # take a session out of rotation without
                                     # a code change.

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
    def crosses_midnight(self) -> bool:
        """True if the close fires on the calendar day AFTER the entry.

        Triggered when ``close_utc < entry_utc`` (e.g. utc_2330 entry
        Mon 23:30 UTC, close Tue 00:00 UTC). The scheduler must use a
        weekday set shifted +1 day for the close cron in this case.
        """
        return self.close_utc < self.entry_utc

    @property
    def close_weekdays(self) -> frozenset[int]:
        """UTC weekdays for the CLOSE cron job.

        Same as ``weekdays`` (entry weekdays) unless the close rolls
        past midnight UTC, in which case shifted +1 day so the close
        fires on the calendar day after each entry.
        """
        if not self.crosses_midnight:
            return self.weekdays
        return frozenset((d + 1) % 7 for d in self.weekdays)

    @property
    def close_minutes_in_trading_day(self) -> int:
        """Minutes from 08:00 UTC trading-day-start to this session's close.

        Used to chronologically order sessions within a trading day.
        Handles utc_2330's close-at-midnight cleanly: the close rolls
        forward 24h before subtracting the trading-day-start offset.

        Sanity:
            utc_0900 close 09:30  →   90 min into trading day
            utc_1330 close 15:30  →  450 min
            utc_2330 close 24:00  →  960 min  (rolled +24h from 00:00)
            utc_0100 close 02:00  → 1080 min  (offset=0 path)
        """
        EXPIRY_HOUR = EXPIRY_CUTOFF_UTC.hour  # 8
        entry_min = self.entry_utc.hour * 60 + self.entry_utc.minute
        close_min = self.close_utc.hour * 60 + self.close_utc.minute
        if close_min < entry_min:
            close_min += 24 * 60
        if self.trading_day_offset_days == 1:
            return close_min - EXPIRY_HOUR * 60
        return close_min + (24 - EXPIRY_HOUR) * 60

    @property
    def time_label(self) -> str:
        """Human-friendly entry/close window for telegram messages.

        Example: ``13:30-15:30 UTC``. utc_2330's display close-time is
        rendered as ``24:00`` instead of ``00:00`` for clarity that it
        belongs to the same trading session.
        """
        close_str = self.close_utc.strftime("%H:%M")
        if self.crosses_midnight and self.close_utc.hour == 0 \
                and self.close_utc.minute == 0:
            close_str = "24:00"
        return f"{self.entry_utc.strftime('%H:%M')}-{close_str} UTC"

    def describe_sizing(self) -> str:
        """Compact human-readable summary of the session's sizing config.

        Includes a ``DISABLED`` marker when ``enabled=False`` so the
        startup banner / scheduled-jobs log makes the operator state
        unmissable.
        """
        if not self.enabled:
            base = "DISABLED"
        elif self.sizing_mode == "pct_equity":
            base = f"pct_equity={self.pct_equity:.0%}"
        elif self.sizing_mode == "fixed_usd":
            base = f"fixed_usd=${self.fixed_usd:,.0f}/entry"
        else:
            base = f"fixed_btc={self.qty_per_leg} BTC"
        return base


def _build_session(
    name: str,
    entry_utc: time,
    close_utc: time,
    *,
    weekdays: frozenset[int],
    default_sizing_mode: str = "pct_equity",
    default_pct_equity: float = 0.0,
    default_qty_per_leg: float = 0.5,
    default_fixed_usd: float = 5500.0,
) -> Session:
    """Construct a Session with per-session env-var overrides.

    Each session reads five env vars (case-insensitive name prefix):
        <NAME>_SIZING        — "fixed_btc"|"pct_equity"|"fixed_usd" (default: pct_equity)
        <NAME>_PCT_EQUITY    — float, e.g. 0.25 for 25%      (default: arg)
        <NAME>_QTY_PER_LEG   — float, BTC                    (default: arg)
        <NAME>_FIXED_USD     — float, target premium USD     (default: arg)
        <NAME>_ENABLED       — bool ("true"/"false"/"1"/"0") (default: true)

    ``<NAME>_ENABLED=false`` is the operator panic-button: it removes
    the session from the scheduler without a code change. All other
    knobs still parse so the session can be re-enabled instantly.

    The defaults baked into config.SESSIONS reflect the agreed
    deployment plan (2026-05-20): all four sessions enabled, in
    pct_equity mode at the operator-chosen percentages.
    """
    sizing_mode = _session_env_str(
        name, "SIZING", default_sizing_mode,
    ).lower()
    if sizing_mode not in ("fixed_btc", "pct_equity", "fixed_usd"):
        sizing_mode = default_sizing_mode
    pct_equity = _session_env_float(name, "PCT_EQUITY", default_pct_equity)
    qty_per_leg = _session_env_float(name, "QTY_PER_LEG", default_qty_per_leg)
    fixed_usd = _session_env_float(name, "FIXED_USD", default_fixed_usd)
    enabled_raw = _session_env_str(name, "ENABLED", "true").lower()
    enabled = enabled_raw not in ("false", "0", "no", "off", "")
    return Session(
        name=name,
        entry_utc=entry_utc,
        close_utc=close_utc,
        qty_per_leg=qty_per_leg,
        sizing_mode=sizing_mode,
        pct_equity=pct_equity,
        fixed_usd=fixed_usd,
        weekdays=weekdays,
        enabled=enabled,
    )


# ─────────────────── Session schedule (2026-06-14) ────────────────
# High-frequency 30-minute straddle windows. Each entry opens a long
# straddle; the close fires 30 min later (full hold) OR 5 min before the
# next contiguous entry (a "chained" roll) so the next entry fires on
# time after the maker-only unwind has flattened. See _SESSION_ROLL_
# BUFFER_MIN below.
#
# Naming: wd_HHMM = weekday (Mon-Fri entry), we_HHMM = weekend (Sat-Sun
# entry). HHMM is the UTC entry time. Names are unique across the day-
# types even when the HHMM collides (e.g. wd_1430 vs we_1430). Legacy
# utc_*/morning/afternoon names remain resolvable for historical reports
# via LEGACY_SESSION_NAMES + get_session().
#
# All sessions: fixed_btc 0.5 BTC/leg (operator-overridable per session
# via <NAME>_SIZING / <NAME>_QTY_PER_LEG in .env). Only ONE straddle is
# ever open at a time — windows never overlap within a day-type.

# Minutes before the next contiguous entry that a "chained" window
# closes, leaving a maker-only unwind buffer so the next entry isn't
# blocked by an still-open prior straddle.
_SESSION_ROLL_BUFFER_MIN: int = 5

# (name, entry HH:MM) — closes are derived below. Entries within a list
# that are exactly 30 min apart form a contiguous roll-chain; the close
# of every chained window is set to next_entry − _SESSION_ROLL_BUFFER_MIN.
# The last window of a chain (and every standalone window) holds the full
# 30 min.
_WEEKDAY_ENTRIES: list[tuple[str, int, int]] = [
    ("wd_0900", 9, 0),
    ("wd_1100", 11, 0),
    ("wd_1130", 11, 30),
    ("wd_1200", 12, 0),
    ("wd_1230", 12, 30),
    ("wd_1300", 13, 0),
    ("wd_1330", 13, 30),
    ("wd_1400", 14, 0),
    ("wd_1430", 14, 30),
    ("wd_1500", 15, 0),
    ("wd_1530", 15, 30),
    ("wd_2330", 23, 30),
    ("wd_0100", 1, 0),
]
_WEEKDAY_DAYS: frozenset[int] = frozenset({0, 1, 2, 3, 4})  # Mon-Fri entry

_WEEKEND_ENTRIES: list[tuple[str, int, int]] = [
    ("we_1100", 11, 0),
    ("we_1200", 12, 0),
    ("we_1230", 12, 30),
    ("we_1330", 13, 30),
    ("we_1430", 14, 30),
    ("we_1500", 15, 0),
    ("we_1700", 17, 0),
    ("we_1900", 19, 0),
    ("we_2200", 22, 0),
]
_WEEKEND_DAYS: frozenset[int] = frozenset({5, 6})  # Sat,Sun entry


def _derive_close(
    entry_h: int, entry_m: int, next_entry_min: int | None,
) -> time:
    """Close = entry+30 (full hold) unless the next entry is exactly 30
    min away (a chained roll), in which case close = next_entry − buffer.

    ``next_entry_min`` is the next window's entry as minutes-since-midnight
    on the SAME day, or None if there is no contiguous follow-on.
    """
    entry_min = entry_h * 60 + entry_m
    full_close_min = entry_min + 30
    if next_entry_min is not None and next_entry_min == full_close_min:
        # chained roll — leave the unwind buffer before the next entry
        close_min = next_entry_min - _SESSION_ROLL_BUFFER_MIN
    else:
        close_min = full_close_min
    close_min %= (24 * 60)  # wrap past midnight (e.g. 23:30 + 30 = 00:00)
    return time(close_min // 60, close_min % 60)


def _build_schedule(
    entries: list[tuple[str, int, int]], weekdays: frozenset[int],
) -> list[Session]:
    """Build a list of fixed_btc 0.5-BTC sessions from (name, h, m) specs,
    deriving each close from the next contiguous entry (chained roll) or
    a full 30-min hold (standalone / chain tail)."""
    entry_mins = {h * 60 + m for (_, h, m) in entries}
    out: list[Session] = []
    for (name, h, m) in entries:
        nxt = h * 60 + m + 30
        next_entry_min = nxt if nxt in entry_mins else None
        out.append(_build_session(
            name,
            entry_utc=time(h, m),
            close_utc=_derive_close(h, m, next_entry_min),
            weekdays=weekdays,
            default_sizing_mode="fixed_btc",
            default_qty_per_leg=0.5,
        ))
    return out


SESSIONS: list[Session] = (
    _build_schedule(_WEEKDAY_ENTRIES, _WEEKDAY_DAYS)
    + _build_schedule(_WEEKEND_ENTRIES, _WEEKEND_DAYS)
)


# Legacy session names. trade_log.csv rows from before 2026-05-20 use
# "morning"/"afternoon" as session_name; new code uses "utc_HHMM". This
# table maps legacy → canonical so reports can reconcile historical
# rows seamlessly. The migration script (tools/migrate_session_names.py)
# rewrites the CSV in-place with a .bak backup, but until that's run
# (or for any pre-migration row that survives), report code looks here.
LEGACY_SESSION_NAMES: dict[str, str] = {
    "morning": "utc_0100",
    "afternoon": "utc_1330",
}


def canonical_session_name(name: str) -> str:
    """Map legacy ``morning``/``afternoon`` to canonical ``utc_HHMM``.

    Returns ``name`` unchanged if it's not a known legacy alias.
    """
    return LEGACY_SESSION_NAMES.get(name, name)


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


def enabled_sessions() -> list[Session]:
    """Return the subset of SESSIONS that are runtime-enabled.

    Disabled sessions stay in ``SESSIONS`` so legacy lookups
    (``get_session(name)`` / report rendering / ENTRY_NOW) still find
    them, but every scheduling / next-trading-day / last-close path
    routes through this filter so a disabled session is invisible to
    the live algo.
    """
    return [s for s in SESSIONS if s.enabled]


def _last_close_session_name(sessions: list[Session]) -> str:
    """The ENABLED session whose close time is the LAST event of a trading day.

    Sorts by ``close_minutes_in_trading_day`` (minutes from 08:00 UTC
    trading-day-start to close), which handles cross-midnight closes
    correctly so utc_2330 (close=960 min) ranks AFTER utc_1330
    (close=450 min) but BEFORE utc_0100 (close=1080 min). The
    last-close session triggers the combined DAILY SUMMARY report.

    Disabled sessions are excluded — if an operator turns off
    utc_0100, the daily summary instead chains off whichever enabled
    session has the latest close.
    """
    pool = [s for s in sessions if s.enabled]
    if not pool:
        return ""
    return max(pool, key=lambda s: s.close_minutes_in_trading_day).name


LAST_CLOSE_SESSION_NAME: str = _last_close_session_name(SESSIONS)


def get_session(name: str) -> Session | None:
    """Lookup a session by name, or None if not configured.

    Accepts both the canonical ``utc_HHMM`` form AND legacy aliases
    (``morning``/``afternoon``) so an operator-typed ENTRY_NOW=morning
    keeps working after the rename.
    """
    canonical = canonical_session_name(name)
    for s in SESSIONS:
        if s.name == canonical:
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

# ──────────────────── External Trade-Gate Signal ─────────────────
# Optional per-window entry gate driven by an external JSON signal file
# (e.g. the vsn-vol-forecaster). When enabled, each scheduled entry is
# allowed ONLY if the signal file positively says so for that window.
# Default OFF — unset leaves entry behaviour completely unchanged.
#
# The producer writes its output dir on the host; mount it READ-ONLY into
# the container (see docker-compose.yml) so the path resolves to
# TRADE_GATE_FILE inside /app. See risk/trade_gate.py for the schema and
# the fail-safe semantics.
TRADE_GATE_ENABLED: bool = os.getenv(
    "TRADE_GATE_ENABLED", "false",
).lower() in ("true", "1", "yes", "on")

# Path to the JSON signal file, relative to the app working dir (/app).
TRADE_GATE_FILE: str = os.getenv(
    "TRADE_GATE_FILE", "signals/trade_gate.json",
)

# Max age of the file's ``generated_at_utc`` before the signal is treated
# as stale and entries are blocked (fail-safe against a frozen producer).
# Default 900s (15 min) comfortably clears a 5-min producer cadence.
TRADE_GATE_MAX_AGE_SEC: float = float(
    os.getenv("TRADE_GATE_MAX_AGE_SEC", "900")
)

# Also require the file's ``weekday`` flag to match the firing session's
# day-type. Guards the 13:30 / 14:30 / 15:00 entry-time collisions between
# the weekday and weekend schedules.
TRADE_GATE_MATCH_WEEKDAY: bool = os.getenv(
    "TRADE_GATE_MATCH_WEEKDAY", "true",
).lower() in ("true", "1", "yes", "on")

# When True, a missing / unreadable / stale signal file ALLOWS the entry
# (fail-open). Default False = fail-safe: anything we cannot positively
# verify blocks the entry. An explicit should_trade=false ALWAYS blocks
# regardless of this flag.
TRADE_GATE_FAIL_OPEN: bool = os.getenv(
    "TRADE_GATE_FAIL_OPEN", "false",
).lower() in ("true", "1", "yes", "on")

# The producer publishes a window's signal slightly AFTER the entry instant
# (e.g. 13:00:40 for a 13:00 entry). Rather than read once at the entry
# cron and miss it, the gate POLLS the file until it carries this window's
# signal, bounded by TRADE_GATE_WAIT_SEC. Keep this small relative to the
# session window so it never eats into the entry chase (default 90s ≪ the
# 25-30 min windows). On timeout with no matching signal, fail-open/closed
# applies. TRADE_GATE_POLL_SEC is the re-read interval.
TRADE_GATE_WAIT_SEC: float = float(os.getenv("TRADE_GATE_WAIT_SEC", "90"))
TRADE_GATE_POLL_SEC: float = float(os.getenv("TRADE_GATE_POLL_SEC", "3"))

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
