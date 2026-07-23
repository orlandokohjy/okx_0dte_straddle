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
    def __init__(self, h, m):
        self.entry_utc = time(h, m)


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
