"""Offline tests for the session journal (skips stay out of trade_log)."""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from datetime import datetime, time, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("CONTAINER_NAME", "okx_signal")

import config
from core import session_journal
from risk.trade_gate import GateDecision


def _isolate(tmp: str) -> None:
    config.STATE_DIR = tmp
    config.SESSION_EVENTS_FILE = os.path.join(tmp, "session_events.jsonl")
    config.SESSION_SUMMARY_FILE = os.path.join(tmp, "session_summary.csv")


def _session(name: str = "wd_1330") -> config.Session:
    return config.Session(
        name=name,
        entry_utc=time(13, 30),
        close_utc=time(16, 0),
        qty_per_leg=0.5,
    )


def _events() -> list[dict]:
    path = config.SESSION_EVENTS_FILE
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _summary_rows() -> list[dict]:
    path = config.SESSION_SUMMARY_FILE
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def test_trading_day_cutoff():
    before = datetime(2026, 8, 20, 7, 59, tzinfo=timezone.utc)
    after = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)
    assert session_journal.trading_day_from_ts(before) == "2026-08-20"
    assert session_journal.trading_day_from_ts(after) == "2026-08-21"


def test_skip_writes_summary_not_trade_log():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        rec = session_journal.EntryRecorder(_session())
        rec.gate_decision(
            GateDecision(False, "should_trade=false", should_trade=False),
            wait_sec=12.0,
            intent="skip",
        )
        rec.outcome("skipped_signal", "should_trade=false", intent="skip")

        events = {e["event"] for e in _events()}
        assert "session_due" in events
        assert "gate_decision" in events
        assert "entry_outcome" in events

        rows = _summary_rows()
        assert len(rows) == 1
        row = rows[0]
        assert row["session"] == "wd_1330"
        assert row["stack"] == "okx_signal"
        assert row["gate"] == "false"
        assert row["intent"] == "skip"
        assert row["entry_result"] == "skipped_signal"
        assert row["close_result"] == "n/a"
        assert row["net_pnl"] == ""
        assert not os.path.exists(os.path.join(tmp, "trade_log.csv"))


def test_partial_then_booked_upserts_same_row():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        rec = session_journal.EntryRecorder(_session("wd_0130"))
        rec.outcome(
            "partial_flattened", "put_leg_failed",
            qty_per_leg=0.5, strike=64300,
        )
        ctx = rec.ctx
        session_journal.record_close_outcome(ctx, "reflatten")
        # A later booked close of a *different* session must not clobber this.
        other = session_journal.ctx_for(_session("wd_0200"))
        session_journal.record_straddle_booked(other, net_pnl=12.5)

        rows = {r["session"]: r for r in _summary_rows()}
        assert rows["wd_0130"]["entry_result"] == "partial_flattened"
        assert rows["wd_0130"]["close_result"] == "reflatten"
        assert rows["wd_0130"]["net_pnl"] == ""
        assert rows["wd_0200"]["net_pnl"] == "12.5"


def test_opened_then_booked_fills_pnl():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        rec = session_journal.EntryRecorder(_session())
        rec.outcome(
            "opened", intent="buy",
            qty_per_leg=0.5, strike=114000,
            call_symbol="BTC-USD-260821-114000-C",
            put_symbol="BTC-USD-260821-114000-P",
        )
        session_journal.record_straddle_booked(
            rec.ctx, net_pnl=-8.25, exit_chase_sec=40,
        )
        session_journal.record_close_outcome(rec.ctx, "clean")
        row = _summary_rows()[0]
        assert row["entry_result"] == "opened"
        assert row["intent"] == "buy"
        assert row["close_result"] == "clean"
        assert row["net_pnl"] == "-8.25"
        assert row["qty_per_leg"] == "0.5"


def test_fail_open_on_write_error():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        # Point the journal at a directory path so writes fail.
        config.SESSION_EVENTS_FILE = tmp  # is a directory, not a file
        config.SESSION_SUMMARY_FILE = tmp
        rec = session_journal.EntryRecorder(_session())
        rec.outcome("skipped_signal", "boom")  # must not raise
        rec.finish_if_needed()


def test_gate_label_timeout():
    g = GateDecision(True, "stale file; waited 90s — fail-open")
    assert session_journal.gate_label(g) == "timeout_failopen"
    g2 = GateDecision(False, "missing; waited 90s — fail-safe block")
    assert session_journal.gate_label(g2) == "timeout_failsafe"
    assert session_journal.gate_label(g, enabled=False) == "n/a"


def test_finish_if_needed_is_noop_after_outcome():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        rec = session_journal.EntryRecorder(_session())
        rec.blocked("entry_locked")
        rec.finish_if_needed()
        outcomes = [e for e in _events() if e["event"] == "entry_outcome"]
        blocked = [e for e in _events() if e["event"] == "entry_blocked"]
        assert len(blocked) == 1
        assert outcomes == []
        assert _summary_rows()[0]["entry_result"] == "blocked"


if __name__ == "__main__":
    test_trading_day_cutoff()
    print("PASS: trading_day_cutoff")
    test_skip_writes_summary_not_trade_log()
    print("PASS: skip_writes_summary_not_trade_log")
    test_partial_then_booked_upserts_same_row()
    print("PASS: partial_then_booked_upserts_same_row")
    test_opened_then_booked_fills_pnl()
    print("PASS: opened_then_booked_fills_pnl")
    test_fail_open_on_write_error()
    print("PASS: fail_open_on_write_error")
    test_gate_label_timeout()
    print("PASS: gate_label_timeout")
    test_finish_if_needed_is_noop_after_outcome()
    print("PASS: finish_if_needed_is_noop_after_outcome")
    print("ALL PASS")
