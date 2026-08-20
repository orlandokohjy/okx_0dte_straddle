"""Session journal — every scheduled window, not only booked P&L.

``trade_log.csv`` stays the ledger: one row per fully opened + closed
straddle. Skips, no-fills, one-leg rollbacks, and orphan flattens never
belong there.

This module writes two files under ``state/``:

* ``session_events.jsonl`` — append-only, one JSON object per action
* ``session_summary.csv`` — one row per scheduled session, upserted as
  the window progresses (gate → entry → close)

Join key on every record: ``stack``, ``trading_day``, ``session``,
``session_id`` (``{trading_day}_{session}_{stack}``).

Never raises. Journal I/O is fail-open so a disk error cannot block
trading. Same schema on the 30-min and 1h AIML stacks.
"""
from __future__ import annotations

import csv
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import structlog

import config
from utils.time_utils import now_utc

log = structlog.get_logger(__name__)

_LOCK = threading.Lock()

SUMMARY_FIELDS = [
    "stack",
    "trading_day",
    "session",
    "session_id",
    "entry_ts",
    "close_ts",
    "gate",
    "should_trade",
    "intent",
    "entry_result",
    "entry_reason",
    "close_result",
    "held_sec",
    "qty_per_leg",
    "strike",
    "call_symbol",
    "put_symbol",
    "net_pnl",
    "signal_lag_sec",
    "entry_chase_sec",
    "exit_chase_sec",
    "gate_reason",
]


@dataclass(frozen=True)
class SessionCtx:
    stack: str
    trading_day: str
    session: str
    session_id: str
    label: str = ""


def stack_name() -> str:
    return (
        os.getenv("CONTAINER_NAME")
        or os.getenv("COMPOSE_PROJECT_NAME")
        or "unknown"
    )


def trading_day_from_ts(when: Optional[datetime] = None) -> str:
    """Trading day = 0DTE expiry UTC date (08:00 cutoff).

    Same rule as ``reporting.daily_report._trading_day_from_entry_time``:
    at/after the expiry cutoff the window belongs to the next calendar
    day's expiry; before it, same calendar day.
    """
    when = when or now_utc()
    cutoff = config.EXPIRY_CUTOFF_UTC
    if (when.hour, when.minute) >= (cutoff.hour, cutoff.minute):
        return (when + timedelta(days=1)).date().strftime("%Y-%m-%d")
    return when.date().strftime("%Y-%m-%d")


def ctx_for(session: "config.Session", when: Optional[datetime] = None) -> SessionCtx:
    when = when or now_utc()
    day = trading_day_from_ts(when)
    stack = stack_name()
    return SessionCtx(
        stack=stack,
        trading_day=day,
        session=session.name,
        session_id=f"{day}_{session.name}_{stack}",
        label=getattr(session, "time_label", "") or "",
    )


def ctx_for_name(session_name: str, when: Optional[datetime] = None) -> SessionCtx:
    sess = _session_by_name(session_name)
    if sess is not None:
        return ctx_for(sess, when)
    when = when or now_utc()
    day = trading_day_from_ts(when)
    stack = stack_name()
    return SessionCtx(
        stack=stack,
        trading_day=day,
        session=session_name or "unknown",
        session_id=f"{day}_{session_name or 'unknown'}_{stack}",
    )


def _session_by_name(name: str) -> Optional["config.Session"]:
    if not name:
        return None
    for s in getattr(config, "SESSIONS", ()) or ():
        if getattr(s, "name", None) == name:
            return s
    return None


def _events_path() -> str:
    return getattr(config, "SESSION_EVENTS_FILE", f"{config.STATE_DIR}/session_events.jsonl")


def _summary_path() -> str:
    return getattr(config, "SESSION_SUMMARY_FILE", f"{config.STATE_DIR}/session_summary.csv")


def emit(event: str, ctx: SessionCtx, **fields: Any) -> None:
    """Append one JSONL event. Never raises."""
    try:
        payload: dict[str, Any] = {
            "ts": now_utc().isoformat(),
            "event": event,
            "stack": ctx.stack,
            "trading_day": ctx.trading_day,
            "session": ctx.session,
            "session_id": ctx.session_id,
        }
        if ctx.label:
            payload["label"] = ctx.label
        for key, val in fields.items():
            if val is not None:
                payload[key] = val
        line = json.dumps(payload, default=str, separators=(",", ":"))
        path = _events_path()
        with _LOCK:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a") as f:
                f.write(line + "\n")
    except Exception:
        log.warning("session_journal_emit_failed", journal_event=event, exc_info=True)


def _fmt(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, float):
        if val != val:  # NaN
            return ""
        return f"{val:.6g}"
    return str(val)


def _blank_row(ctx: SessionCtx) -> dict[str, str]:
    row = {k: "" for k in SUMMARY_FIELDS}
    row["stack"] = ctx.stack
    row["trading_day"] = ctx.trading_day
    row["session"] = ctx.session
    row["session_id"] = ctx.session_id
    return row


def _read_summary_unlocked() -> list[dict[str, str]]:
    path = _summary_path()
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _write_summary_unlocked(rows: list[dict[str, str]]) -> None:
    path = _summary_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in SUMMARY_FIELDS})


def _held_sec(row: dict[str, str]) -> str:
    entry = row.get("entry_ts") or ""
    close = row.get("close_ts") or ""
    if not entry or not close:
        return row.get("held_sec") or ""
    try:
        e = datetime.fromisoformat(entry.replace("Z", "+00:00"))
        c = datetime.fromisoformat(close.replace("Z", "+00:00"))
        return str(int((c - e).total_seconds()))
    except Exception:
        return row.get("held_sec") or ""


def upsert_summary(ctx: SessionCtx, **fields: Any) -> None:
    """Merge non-empty fields into the session's summary row. Never raises."""
    try:
        with _LOCK:
            rows = _read_summary_unlocked()
            idx = next(
                (i for i, r in enumerate(rows) if r.get("session_id") == ctx.session_id),
                None,
            )
            row = dict(rows[idx]) if idx is not None else _blank_row(ctx)
            for key, val in fields.items():
                if key not in SUMMARY_FIELDS:
                    continue
                if val is None or val == "":
                    continue
                row[key] = _fmt(val)
            row["held_sec"] = _held_sec(row)
            if idx is None:
                rows.append(row)
            else:
                rows[idx] = row
            _write_summary_unlocked(rows)
    except Exception:
        log.warning("session_journal_summary_failed",
                    session_id=ctx.session_id, exc_info=True)


def find_summary(session_id: str) -> Optional[dict[str, str]]:
    try:
        with _LOCK:
            for row in _read_summary_unlocked():
                if row.get("session_id") == session_id:
                    return row
    except Exception:
        log.warning("session_journal_summary_read_failed", exc_info=True)
    return None


def gate_label(gate: Any, *, enabled: bool = True) -> str:
    """Map a GateDecision (or timeout reason) onto the summary ``gate`` cell."""
    if not enabled:
        return "n/a"
    reason = str(getattr(gate, "reason", "") or "")
    if "fail-open" in reason:
        return "timeout_failopen"
    if "fail-safe" in reason:
        return "timeout_failsafe"
    should = getattr(gate, "should_trade", None)
    if should is True:
        return "true"
    if should is False:
        return "false"
    return "unknown"


def record_session_due(ctx: SessionCtx, **fields: Any) -> None:
    emit("session_due", ctx, **fields)
    upsert_summary(
        ctx,
        entry_ts=now_utc().isoformat(),
        **{k: v for k, v in fields.items() if k in SUMMARY_FIELDS},
    )


def record_gate_wait(ctx: SessionCtx, reason: str, elapsed_sec: float = 0.0) -> None:
    emit("gate_wait", ctx, reason=reason, elapsed_sec=round(elapsed_sec, 2))


def record_gate_decision(
    ctx: SessionCtx,
    gate: Any,
    *,
    wait_sec: float = 0.0,
    enabled: bool = True,
    intent: str = "",
) -> None:
    allowed = bool(getattr(gate, "allowed", False))
    should = getattr(gate, "should_trade", None)
    reason = str(getattr(gate, "reason", "") or "")
    emit(
        "gate_decision",
        ctx,
        should_trade=should,
        allowed=allowed,
        reason=reason,
        wait_sec=round(wait_sec, 2),
    )
    upsert_summary(
        ctx,
        gate=gate_label(gate, enabled=enabled),
        should_trade=("true" if should is True else "false" if should is False else "unknown"),
        gate_reason=reason,
        signal_lag_sec=round(wait_sec, 2) if wait_sec else "",
        intent=intent,
    )


def record_entry_blocked(ctx: SessionCtx, reason: str, *, result: str = "blocked") -> None:
    emit("entry_blocked", ctx, reason=reason, result=result)
    upsert_summary(
        ctx,
        entry_result=result,
        entry_reason=reason,
        intent="skip",
        close_result="n/a",
    )


def record_entry_outcome(
    ctx: SessionCtx, result: str, reason: str = "", **extra: Any,
) -> None:
    emit("entry_outcome", ctx, result=result, reason=reason, **extra)
    extra = dict(extra)
    intent = extra.pop("intent", None)
    if intent is None:
        intent = "buy" if result == "opened" else "skip"
    summary_extra = {k: v for k, v in extra.items() if k in SUMMARY_FIELDS}
    close = "" if result == "opened" else "n/a"
    upsert_summary(
        ctx,
        entry_result=result,
        entry_reason=reason,
        intent=intent,
        close_result=close,
        **summary_extra,
    )


def record_close_outcome(ctx: SessionCtx, result: str, reason: str = "", **extra: Any) -> None:
    emit("close_outcome", ctx, result=result, reason=reason, **extra)
    upsert_summary(
        ctx,
        close_result=result,
        close_ts=now_utc().isoformat(),
        **{k: v for k, v in extra.items() if k in SUMMARY_FIELDS},
    )


def record_straddle_booked(ctx: SessionCtx, net_pnl: float, **extra: Any) -> None:
    emit("straddle_booked", ctx, net_pnl=net_pnl, **extra)
    upsert_summary(
        ctx,
        net_pnl=round(float(net_pnl), 2),
        close_ts=now_utc().isoformat(),
        **{k: v for k, v in extra.items() if k in SUMMARY_FIELDS},
    )


def record_orphan_flatten(ctx: SessionCtx, **fields: Any) -> None:
    emit("orphan_flatten", ctx, **fields)


class EntryRecorder:
    """Per-entry helper: ``session_due`` on construct, one terminal outcome.

    ``finish_if_needed`` is a safety net for unexpected returns — if the
    builder already wrote ``entry_result``, this is a no-op.
    """

    def __init__(self, session: "config.Session") -> None:
        self.ctx = ctx_for(session)
        self._terminal = False
        try:
            record_session_due(
                self.ctx,
                window_start=session.entry_utc.strftime("%H:%M"),
                window_end=session.close_utc.strftime("%H:%M"),
                label=session.time_label,
            )
        except Exception:
            log.warning("session_journal_session_due_failed", exc_info=True)

    def gate_wait(self, reason: str, elapsed_sec: float = 0.0) -> None:
        try:
            record_gate_wait(self.ctx, reason, elapsed_sec)
        except Exception:
            log.warning("session_journal_gate_wait_failed", exc_info=True)

    def gate_decision(
        self, gate: Any, *, wait_sec: float = 0.0, intent: str = "",
    ) -> None:
        try:
            record_gate_decision(
                self.ctx, gate,
                wait_sec=wait_sec,
                enabled=True,
                intent=intent,
            )
        except Exception:
            log.warning("session_journal_gate_decision_failed", exc_info=True)

    def blocked(self, reason: str, *, result: str = "blocked") -> None:
        if self._terminal:
            return
        self._terminal = True
        try:
            record_entry_blocked(self.ctx, reason, result=result)
        except Exception:
            log.warning("session_journal_blocked_failed", exc_info=True)

    def outcome(self, result: str, reason: str = "", **extra: Any) -> None:
        if self._terminal:
            return
        self._terminal = True
        try:
            record_entry_outcome(self.ctx, result, reason, **extra)
        except Exception:
            log.warning("session_journal_outcome_failed", exc_info=True)

    def finish_if_needed(self) -> None:
        if self._terminal:
            return
        try:
            existing = find_summary(self.ctx.session_id)
            if existing and existing.get("entry_result"):
                self._terminal = True
                return
            self.outcome("unknown", "entry_exited_without_outcome")
        except Exception:
            log.warning("session_journal_finish_failed", exc_info=True)
