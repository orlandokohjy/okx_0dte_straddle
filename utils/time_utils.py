"""UTC time helpers and 0DTE expiry-date logic for OKX.

OKX BTC options expire at 08:00 UTC. Instrument id format:
  BTC-USD-YYMMDD-STRIKE-{C|P}     (coin-margined, e.g. BTC-USD-260418-65000-C)
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def _expiry_date() -> datetime:
    """
    OKX options settle at 08:00 UTC. Before 08:00 the 0DTE expiry is today;
    after 08:00 the next listed expiry is tomorrow.
    """
    now = now_utc()
    if now.hour < 8:
        return now
    return now + timedelta(days=1)


def today_expiry_instid_str() -> str:
    """OKX instrument-id date format: YYMMDD (e.g. 260418)."""
    return _expiry_date().strftime("%y%m%d")


def today_expiry_iso_str() -> str:
    """Plain ISO date for logging: YYYY-MM-DD."""
    return _expiry_date().strftime("%Y-%m-%d")


def format_utc_sgt(dt: datetime) -> str:
    sgt = dt.astimezone(timezone(timedelta(hours=8)))
    return sgt.strftime("%Y-%m-%d %H:%M SGT")


def is_weekday() -> bool:
    return now_utc().weekday() < 5
