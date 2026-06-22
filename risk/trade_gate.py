"""External trade-gate signal reader.

Optional entry gate: an external producer (e.g. the vsn-vol-forecaster)
writes a JSON file every few minutes describing, per trading window,
whether the algo should open a straddle. ``main._run_entry`` consults
this at each scheduled entry; a stale file, a window mismatch, or a
``should_trade=false`` all SKIP the entry (fail-safe), so a dead or
frozen producer can never wave a trade through.

Schema (only the fields we use; any extras are ignored):

    {
      "generated_at_utc": "2026-06-22T02:46:11Z",  # file freshness anchor
      "weekday": true,                              # day-type of the signal
      "active_window": {
        "entry_utc": "13:30:00",                    # matched to session entry
        "should_trade": true                         # allow (true) / block
      }
    }

Design notes:
  • Freshness is judged off ``generated_at_utc`` (the file-write time),
    bounded by ``config.TRADE_GATE_MAX_AGE_SEC``. A producer whose data
    feed froze but which keeps rewriting the file will still pass this
    check — guard the *upstream* data freshness separately if needed.
  • The gate is per-window: ``active_window.entry_utc`` must match the
    firing session's UTC entry time (HH:MM). If the producer hasn't
    rolled forward to this window, the mismatch fails safe (skip).
  • Never raises. Any path we cannot positively verify resolves per
    ``config.TRADE_GATE_FAIL_OPEN`` (default: block). An explicit
    ``should_trade=false`` ALWAYS blocks, regardless of fail-open.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime

import structlog

import config
from utils.time_utils import now_utc

log = structlog.get_logger(__name__)


@dataclass
class GateDecision:
    allowed: bool
    reason: str
    # True when the outcome could change if we simply wait and re-read —
    # i.e. the producer may not have published THIS window's signal yet
    # (file missing, stale, mid-write, or still pointing at the previous
    # window). The caller polls these. False marks a TERMINAL decision:
    #   • allowed=True  → should_trade=true for this window (enter now)
    #   • allowed=False → should_trade=false for this window (skip now)
    # Note: fail-open/closed for a persistently-retryable state is decided
    # by the CALLER at timeout, not here.
    retryable: bool = False


def _parse_utc(ts: str) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp, tolerating a trailing 'Z'."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def evaluate_trade_gate(session: "config.Session") -> GateDecision:
    """Evaluate the signal file ONCE for ``session``. Never raises.

    Returns a ``GateDecision`` whose ``retryable`` flag tells the caller
    whether the outcome might change by simply waiting and re-reading:

      • Terminal allow  — matched, fresh window with should_trade=true.
      • Terminal block  — matched, fresh window with should_trade=false.
      • Retryable       — anything we cannot YET positively verify (file
                          missing / mid-write / stale / still pointing at
                          the previous window). The producer may publish
                          THIS window's signal a little after the entry
                          instant (e.g. 13:00:40 for a 13:00 entry), so
                          the caller polls these up to a bounded timeout
                          and applies fail-open/closed only if they persist.
    """
    path = config.TRADE_GATE_FILE

    def _retry(reason: str) -> GateDecision:
        return GateDecision(False, reason, retryable=True)

    if not os.path.exists(path):
        return _retry(f"file not found: {path}")

    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as exc:
        # Could be a partial read while the producer rewrites the file —
        # retry on the next poll.
        return _retry(f"unreadable/invalid JSON: {type(exc).__name__}")

    if not isinstance(data, dict):
        return _retry("top-level JSON is not an object")

    # ── Freshness on generated_at_utc ──
    gen = _parse_utc(str(data.get("generated_at_utc", "")))
    if gen is None:
        return _retry("missing/invalid generated_at_utc")
    age_sec = (now_utc() - gen).total_seconds()
    max_age = config.TRADE_GATE_MAX_AGE_SEC
    if age_sec > max_age:
        return _retry(
            f"stale: generated {age_sec / 60:.1f} min ago "
            f"(max {max_age / 60:.0f} min)"
        )
    if age_sec < -max_age:
        # Future timestamp beyond tolerance → clock skew / bad producer.
        return _retry(
            f"generated_at_utc is {(-age_sec) / 60:.1f} min in the future"
        )

    # ── Active-window block ──
    aw = data.get("active_window")
    if not isinstance(aw, dict):
        return _retry("missing/invalid active_window")

    sig_entry = str(aw.get("entry_utc", "")).strip()
    if not sig_entry:
        return _retry("active_window.entry_utc missing")
    session_entry_hm = session.entry_utc.strftime("%H:%M")
    if sig_entry[:5] != session_entry_hm:
        # Producer's current window is not this session's entry — most
        # likely it hasn't rolled forward to this window yet (the publish
        # lands a few seconds after the entry instant). Retry until it does.
        return _retry(
            f"window not yet current: signal entry_utc={sig_entry} != "
            f"session {session.name} entry {session_entry_hm}"
        )

    # ── Optional day-type match (wd vs we 13:30/14:30/15:00 collisions) ──
    if config.TRADE_GATE_MATCH_WEEKDAY and "weekday" in data:
        sig_weekday = bool(data.get("weekday"))
        session_is_weekday = (
            all(d <= 4 for d in session.weekdays) if session.weekdays else True
        )
        if sig_weekday != session_is_weekday:
            return _retry(
                f"day-type mismatch: signal weekday={sig_weekday}, "
                f"session {session.name} weekday={session_is_weekday}"
            )

    # ── Terminal go / no-go for THIS window ──
    should = aw.get("should_trade")
    if should is True:
        return GateDecision(
            True,
            f"signal OK (window {sig_entry}, fresh {age_sec / 60:.1f} min)",
            retryable=False,
        )
    if should is False:
        return GateDecision(
            False, "signal should_trade=false (no-entry)", retryable=False,
        )
    # Malformed value — treat as not-yet-verified and retry.
    return _retry(f"active_window.should_trade not boolean: {should!r}")
