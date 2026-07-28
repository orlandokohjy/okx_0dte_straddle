"""
Tests for the wings-ATM variant:
  1. select_straddle_pair now picks the NEAREST listed strike to spot
     (true ATM) — can be ABOVE spot, unlike the legacy always-ITM selector.
  2. Wings are time-gated per session (config.session_wings_enabled).

Runnable directly (python tests/test_atm_wings.py) or via pytest.
"""
from __future__ import annotations

import os
import sys
from datetime import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config
from data.option_chain import OptionChain, OptionInfo
from strategy.option_selector import select_straddle_pair


def _chain(strikes, *, no_ask=()):
    """Chain with a call+put at each strike (ask>0 unless strike in no_ask)."""
    ch = OptionChain(exchange=None)
    for s in strikes:
        ask = 0.0 if s in no_ask else 2.0
        ch.calls.append(OptionInfo(symbol=f"C-{int(s)}", strike=s,
                                   option_type="C", bid=1.0, ask=ask, mark=1.5))
        ch.puts.append(OptionInfo(symbol=f"P-{int(s)}", strike=s,
                                  option_type="P", bid=1.0, ask=ask, mark=1.5))
    ch.calls.sort(key=lambda x: x.strike)
    ch.puts.sort(key=lambda x: x.strike)
    return ch


# ───────────────────── nearest-strike selection ──────────────────────

def test_nearest_rounds_down_below_midpoint():
    # 1000-spaced strikes; spot 65490 → 65000 is nearest (the user's example).
    ch = _chain([64000, 65000, 66000, 67000])
    pair = select_straddle_pair(ch, 65490.0)
    assert pair is not None and pair.strike == 65000.0


def test_nearest_rounds_up_above_midpoint():
    # spot 65510 → 66000 is nearest (ABOVE spot). Legacy would have picked
    # 65000 (always-ITM). This is the behaviour change.
    ch = _chain([64000, 65000, 66000, 67000])
    pair = select_straddle_pair(ch, 65510.0)
    assert pair is not None and pair.strike == 66000.0


def test_nearest_picks_above_spot_when_closest():
    # 500-spaced; spot 65490 → 65500 (10 away) beats 65000 (490 away).
    ch = _chain([65000, 65500, 66000])
    pair = select_straddle_pair(ch, 65490.0)
    assert pair is not None and pair.strike == 65500.0


def test_nearest_tie_breaks_to_lower_strike():
    # spot exactly at the midpoint 65500 → tie → lower strike 65000.
    ch = _chain([65000, 66000])
    pair = select_straddle_pair(ch, 65500.0)
    assert pair is not None and pair.strike == 65000.0


def test_same_strike_for_call_and_put():
    ch = _chain([64000, 65000, 66000])
    pair = select_straddle_pair(ch, 64800.0)
    assert pair.call.strike == pair.put.strike == pair.strike == 65000.0


def test_skips_strike_without_tradable_ask():
    # Nearest strike (65000) has no ask on the put side → fall to next nearest.
    ch = _chain([64000, 65000, 66000], no_ask=(65000,))
    pair = select_straddle_pair(ch, 65100.0)
    assert pair is not None and pair.strike == 66000.0


def test_returns_none_when_no_tradable_common_strike():
    ch = _chain([65000], no_ask=(65000,))
    assert select_straddle_pair(ch, 65000.0) is None


# ───────────────────── per-session wing gating ───────────────────────

class _S:
    def __init__(self, h, m, weekdays=None):
        self.entry_utc = time(h, m)
        # session_wings_enabled() gates weekends via WING_WEEKENDS_ENABLED,
        # so a stub needs entry days. Default to Mon-Fri.
        self.weekdays = frozenset({0, 1, 2, 3, 4}) if weekdays is None \
            else weekdays


def test_wing_window_gating():
    prev = config.ENABLE_WINGS
    config.ENABLE_WINGS = True
    try:
        # Inside [13:00, 14:30] → wings ON
        for h, m in [(13, 0), (13, 30), (14, 0), (14, 30)]:
            assert config.session_wings_enabled(_S(h, m)), f"{h}:{m} should wing"
        # Outside → wings OFF (note 15:00 excluded: last wing entry is 14:30)
        for h, m in [(12, 30), (15, 0), (9, 0), (23, 30)]:
            assert not config.session_wings_enabled(_S(h, m)), \
                f"{h}:{m} should NOT wing"
    finally:
        config.ENABLE_WINGS = prev


def test_master_switch_off_disables_all_wings():
    prev = config.ENABLE_WINGS
    config.ENABLE_WINGS = False
    try:
        assert not config.session_wings_enabled(_S(13, 30))
    finally:
        config.ENABLE_WINGS = prev


def test_weekend_session_gets_no_wings_by_default():
    prev = config.ENABLE_WINGS
    config.ENABLE_WINGS = True
    try:
        weekend = _S(13, 30, weekdays=frozenset({5, 6}))
        assert not config.session_wings_enabled(weekend)
    finally:
        config.ENABLE_WINGS = prev


# ──────────── decoupled wing / non-wing entry-chase budgets ───────────

def test_non_wing_session_gets_full_entry_budget():
    """A non-wing session chases the body once → 1× the long budget."""
    prev = config.ENABLE_WINGS
    config.ENABLE_WINGS = True
    try:
        s = _S(9, 0)  # outside the wing window
        assert not config.session_wings_enabled(s)
        assert config.session_entry_chase_deadline_min(s) == \
            config.OPTION_ENTRY_CHASE_DEADLINE_MIN
        assert config.session_entry_total_budget_min(s) == \
            config.OPTION_ENTRY_CHASE_DEADLINE_MIN
    finally:
        config.ENABLE_WINGS = prev


def test_wing_session_uses_short_budget_twice():
    """A wing session chases body THEN wings → 2× the short wing budget."""
    prev = config.ENABLE_WINGS
    config.ENABLE_WINGS = True
    try:
        s = _S(13, 30)
        assert config.session_wings_enabled(s)
        assert config.session_entry_chase_deadline_min(s) == \
            config.WING_ENTRY_CHASE_DEADLINE_MIN
        assert config.session_entry_total_budget_min(s) == \
            config.WING_ENTRY_CHASE_DEADLINE_MIN * 2
    finally:
        config.ENABLE_WINGS = prev


def test_every_session_budget_fits_its_window():
    """The real schedule must satisfy budget ≤ window − 5 for every enabled
    session, so the startup validator can never lock entries on 20/10."""
    prev_enable = config.ENABLE_WINGS
    prev_body = config.OPTION_ENTRY_CHASE_DEADLINE_MIN
    prev_wing = config.WING_ENTRY_CHASE_DEADLINE_MIN
    config.ENABLE_WINGS = True
    config.OPTION_ENTRY_CHASE_DEADLINE_MIN = 20.0
    config.WING_ENTRY_CHASE_DEADLINE_MIN = 10.0
    try:
        for s in config.SESSIONS:
            if not s.enabled:
                continue
            entry_min = s.entry_utc.hour * 60 + s.entry_utc.minute
            close_min = s.close_utc.hour * 60 + s.close_utc.minute
            if close_min < entry_min:
                close_min += 24 * 60
            max_safe = (close_min - entry_min) - 5.0
            budget = config.session_entry_total_budget_min(s)
            assert budget <= max_safe, (
                f"{s.name}: budget={budget} > max_safe={max_safe}"
            )
    finally:
        config.ENABLE_WINGS = prev_enable
        config.OPTION_ENTRY_CHASE_DEADLINE_MIN = prev_body
        config.WING_ENTRY_CHASE_DEADLINE_MIN = prev_wing


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
