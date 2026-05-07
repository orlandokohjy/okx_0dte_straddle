"""
OKX 0DTE Pure Straddle — Configuration.

All tunables in one place. Env-var overrides for deployment.

Default mode: Demo Trading (OKX_FLAG=1) so you can test safely.
For production, set OKX_FLAG=0 in .env.
"""
from __future__ import annotations

import os
from datetime import time

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
QUOTE_COIN: str = os.getenv("QUOTE_COIN", "USD")  # "USD" (coin-margined) or "USDT"
QTY_PER_LEG: float = float(os.getenv("QTY_PER_LEG", "0.5"))

# OKX BTC options: 1 contract = 0.01 BTC (coin-margined). Verify per instrument
# via SDK get_instruments → ctVal field on first run. Update if your account
# uses a different contract size.
OKX_CONTRACT_SIZE_BTC: float = float(os.getenv("OKX_CONTRACT_SIZE_BTC", "0.01"))

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

# ──────────────────── Session Schedule (UTC) ──────────────────────
SESSION_ENTRY_UTC: time = time(12, 0)
SESSION_CLOSE_UTC: time = time(16, 0)
REPORT_UTC: time = time(17, 0)
WEEKLY_REPORT_UTC: time = time(18, 0)
ALLOWED_WEEKDAYS: set[int] = {0, 1, 2, 3, 4}  # Mon–Fri

# ──────────────────── Execution Settings ──────────────────────────
OPTION_CHASE_INTERVAL_SEC: float = 5.0

# OKX BTC-USD coin-margined options:
#   • Premium quoted in BTC (per BTC of underlying notional, dimensionless)
#   • Tick size = 0.0005 BTC across all strikes/expiries (verified 2026-05)
# This default is used as a fallback only — on startup the algo queries
# /api/v5/public/instruments and overrides with the live tickSz from OKX.
OPTION_TICK_SIZE: float = float(os.getenv("OPTION_TICK_SIZE", "0.0005"))

# Sanity-check bounds for the chase-pricing self-test on startup. If the
# chase math yields a price outside these bounds for a real ITM option
# (relative to its mark), the algo aborts with a clear error rather than
# attempting orders. Guards against unit-conversion regressions like the
# OPTION_TICK_SIZE=5.0 (USD) bug.
CHASE_SELFTEST_MAX_OVER_MARK: float = 1.5     # never > 1.5× mark
CHASE_SELFTEST_MAX_ABSOLUTE_BTC: float = 0.5  # never > 0.5 BTC absolute

# Maker-only chase: 50% bid-ask gap narrowing per retry, fair-value cap, deadline
OPTION_CHASE_GAP_NARROW_PCT: float = float(
    os.getenv("OPTION_CHASE_GAP_NARROW_PCT", "0.5")
)
OPTION_CHASE_MAX_SLIPPAGE_FACTOR: float = float(
    os.getenv("OPTION_CHASE_MAX_SLIPPAGE_FACTOR", "1.15")
)
OPTION_CHASE_DEADLINE_MIN: float = float(
    os.getenv("OPTION_CHASE_DEADLINE_MIN", "10.0")
)

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
