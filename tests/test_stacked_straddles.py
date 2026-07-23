"""
Offline unit tests for STACKED (overlapping) straddles.

No network / OKX SDK required. Runnable with pytest
(``python -m pytest tests/test_stacked_straddles.py``) or directly
(``python tests/test_stacked_straddles.py``) — every async case is wrapped
in ``asyncio.run`` so pytest-asyncio is NOT needed.

Coverage — the invariants that keep a same-strike sibling straddle safe:
  1. Portfolio holds several open straddles keyed by session_name.
  2. Closing straddle A books/attributes A's own P&L and removes ONLY A —
     a same-strike sibling B stays open and untouched.
  3. expected_open_contracts() reports the net contracts still held by the
     OTHER open straddles (the "floor" the unwind/reconcile must not cross),
     and honours exclude_session.
  4. The stacked post-close reconcile is ALERT-ONLY and sibling-aware: it
     stays silent when the live book matches the tracked straddles, and
     alerts (without locking) only on a genuine EXCESS.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config

# Deterministic test environment: UM family (P&L = qty × Δprice, no spot),
# 0.01 BTC contract size, stacking on, isolated temp state dir.
config.STACKED_STRADDLES = True
config.OKX_CONTRACT_SIZE_BTC = 0.01
_TMP = tempfile.mkdtemp(prefix="okx_stack_test_")
config.STATE_DIR = _TMP
config.EQUITY_FILE = os.path.join(_TMP, "equity.json")
config.POSITIONS_FILE = os.path.join(_TMP, "positions.json")
config.TRADE_LOG_FILE = os.path.join(_TMP, "trade_log.csv")

from core.portfolio import Portfolio, Straddle, StraddleLeg  # noqa: E402
import main  # noqa: E402


def _straddle(session, strike, call_inst, put_inst,
              entry_call=100.0, entry_put=120.0, qty=0.25):
    """A minimal UM straddle: long call + long put, qty BTC each leg."""
    return Straddle(
        id=f"{session}-{strike}",
        call_leg=StraddleLeg(call_inst, "buy", qty, entry_call),
        put_leg=StraddleLeg(put_inst, "buy", qty, entry_put),
        strike=strike,
        qty_per_leg=qty,
        entry_time="2026-07-24T13:00:00+00:00",
        entry_call_price=entry_call,
        entry_put_price=entry_put,
        straddle_cost=(entry_call + entry_put) * qty,
        num_straddles=1,
        session_name=session,
        family="UM",
    )


def _fresh_portfolio():
    for f in (config.EQUITY_FILE, config.POSITIONS_FILE, config.TRADE_LOG_FILE):
        try:
            os.remove(f)
        except OSError:
            pass
    return Portfolio()


# ── 1. Portfolio holds multiple open straddles ────────────────────────

def test_two_sessions_open_concurrently_same_strike():
    pf = _fresh_portfolio()
    # SAME strike ⇒ SAME instruments (the netting-hazard case).
    a = _straddle("wd_1030", 65000, "C-65000", "P-65000")
    b = _straddle("wd_1100", 65000, "C-65000", "P-65000")
    pf.set_straddle(a)
    pf.set_straddle(b)

    assert pf.has_open
    assert pf.open_count == 2
    assert pf.get_open("wd_1030") is a
    assert pf.get_open("wd_1100") is b
    print("OK two_sessions_open_concurrently_same_strike")


# ── 2. Close A leaves B open + attributes A's own P&L ─────────────────

def test_close_one_leaves_sibling_open():
    pf = _fresh_portfolio()
    a = _straddle("wd_1030", 65000, "C-65000", "P-65000")
    b = _straddle("wd_1100", 65000, "C-65000", "P-65000")
    pf.set_straddle(a)
    pf.set_straddle(b)

    # Close A at a profit: call 100→150, put 120→130.
    #   gross = 0.25*(150-100) + 0.25*(130-120) = 12.5 + 2.5 = 15.0
    pnl = pf.close_straddle(150.0, 130.0, "session_close",
                            session_name="wd_1030")
    assert abs(pnl - 15.0) < 1e-9, pnl

    # A gone, B still open and untouched.
    assert pf.open_count == 1
    assert pf.get_open("wd_1030") is None
    assert pf.get_open("wd_1100") is b
    assert pf.has_open
    # A is now the most-recently-closed for reporting.
    assert pf.last_closed_straddle is a
    assert a.status == "closed"
    assert b.status == "open"
    print("OK close_one_leaves_sibling_open")


def test_close_targets_named_session_not_sole():
    """close_straddle must respect session_name even with several open."""
    pf = _fresh_portfolio()
    a = _straddle("wd_1030", 65000, "C-65000", "P-65000")
    b = _straddle("wd_1100", 66000, "C-66000", "P-66000")
    pf.set_straddle(a)
    pf.set_straddle(b)

    # Close the SECOND one explicitly.
    pf.close_straddle(150.0, 130.0, "session_close", session_name="wd_1100")
    assert pf.get_open("wd_1100") is None
    assert pf.get_open("wd_1030") is a  # untouched
    print("OK close_targets_named_session_not_sole")


# ── 3. expected_open_contracts (the sibling floor) ────────────────────

def test_expected_open_contracts_floor():
    pf = _fresh_portfolio()
    a = _straddle("wd_1030", 65000, "C-65000", "P-65000")  # 0.25 BTC/leg
    b = _straddle("wd_1100", 65000, "C-65000", "P-65000")  # SAME strike
    pf.set_straddle(a)
    pf.set_straddle(b)

    # 0.25 BTC / 0.01 = 25 contracts per leg, ×2 straddles = 50 on each inst.
    allc = pf.expected_open_contracts()
    assert abs(allc["C-65000"] - 50.0) < 1e-9, allc
    assert abs(allc["P-65000"] - 50.0) < 1e-9, allc

    # Excluding A's session leaves only B's 25 contracts (the floor the
    # unwind of A must never cross).
    floor = pf.expected_open_contracts(exclude_session="wd_1030")
    assert abs(floor["C-65000"] - 25.0) < 1e-9, floor
    assert abs(floor["P-65000"] - 25.0) < 1e-9, floor
    print("OK expected_open_contracts_floor")


# ── 4. Stacked post-close reconcile: sibling-aware, alert-only ────────

def _algo_with_portfolio(pf):
    a = main.Algo.__new__(main.Algo)
    a.portfolio = pf

    async def _fake_fmt(positions):
        return "<book>"

    a._fmt_positions_with_book = _fake_fmt
    return a


def test_stacked_reconcile_silent_when_matches_tracked():
    pf = _fresh_portfolio()
    b = _straddle("wd_1100", 65000, "C-65000", "P-65000")
    pf.set_straddle(b)  # one still-open straddle ⇒ expect 25/25 contracts
    algo = _algo_with_portfolio(pf)

    sent = []

    async def _send(msg):
        sent.append(msg)

    orig = main.notifier.send
    main.notifier.send = _send
    try:
        # Live book exactly matches the tracked sibling → NO alert.
        positions = [
            {"instrument_name": "C-65000", "amount": 25.0},
            {"instrument_name": "P-65000", "amount": 25.0},
        ]
        asyncio.run(algo._post_close_reconcile_stacked(positions))
    finally:
        main.notifier.send = orig

    assert sent == [], f"unexpected alert: {sent}"
    print("OK stacked_reconcile_silent_when_matches_tracked")


def test_stacked_reconcile_alerts_on_excess_only():
    pf = _fresh_portfolio()
    b = _straddle("wd_1100", 65000, "C-65000", "P-65000")
    pf.set_straddle(b)  # expect 25 contracts on each instrument
    algo = _algo_with_portfolio(pf)

    sent = []

    async def _send(msg):
        sent.append(msg)

    orig = main.notifier.send
    main.notifier.send = _send
    try:
        # Live book has 40 on the call (15 more than the tracked sibling) →
        # a genuine unclosed leg → ALERT (but never locks).
        positions = [
            {"instrument_name": "C-65000", "amount": 40.0},
            {"instrument_name": "P-65000", "amount": 25.0},
        ]
        asyncio.run(algo._post_close_reconcile_stacked(positions))
    finally:
        main.notifier.send = orig

    assert len(sent) == 1, sent
    assert "EXCESS" in sent[0]
    # Must NOT have locked entries (stacked schedule keeps running).
    assert getattr(algo, "_entry_locked", False) is False
    print("OK stacked_reconcile_alerts_on_excess_only")


if __name__ == "__main__":
    test_two_sessions_open_concurrently_same_strike()
    test_close_one_leaves_sibling_open()
    test_close_targets_named_session_not_sole()
    test_expected_open_contracts_floor()
    test_stacked_reconcile_silent_when_matches_tracked()
    test_stacked_reconcile_alerts_on_excess_only()
    print("\nAll stacked-straddle tests passed.")
