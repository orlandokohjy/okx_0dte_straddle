"""
Tests for the OTM-strangle variant (reverse / long iron condor, wings off
by default):
  1. select_straddle_pair buys the next OTM call (nearest strike ABOVE spot)
     and next OTM put (nearest strike BELOW spot) — two different strikes.
  2. select_wings picks the short wings ONE strike further out than EACH long
     leg (per-leg offsets).
  3. Wings are time-gated per session (config.session_wings_enabled) and
     master-switched by ENABLE_WINGS (default OFF).

Runnable directly (python tests/test_otm_strangle.py) or via pytest.
"""
from __future__ import annotations

import os
import sys
from datetime import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config
from data.option_chain import OptionChain, OptionInfo
from strategy.option_selector import select_straddle_pair, select_wings


def _chain(strikes, *, no_ask=(), no_bid=()):
    """Chain with a call+put at each strike. ask>0 unless in no_ask (buy-side
    liquidity); bid>0 unless in no_bid (sell-side liquidity for wings)."""
    ch = OptionChain(exchange=None)
    for s in strikes:
        ask = 0.0 if s in no_ask else 2.0
        bid = 0.0 if s in no_bid else 1.0
        ch.calls.append(OptionInfo(symbol=f"C-{int(s)}", strike=s,
                                   option_type="C", bid=bid, ask=ask, mark=1.5))
        ch.puts.append(OptionInfo(symbol=f"P-{int(s)}", strike=s,
                                  option_type="P", bid=bid, ask=ask, mark=1.5))
    ch.calls.sort(key=lambda x: x.strike)
    ch.puts.sort(key=lambda x: x.strike)
    return ch


# ───────────────────── OTM strangle selection ──────────────────────

def test_picks_next_otm_call_and_put():
    # spot 65490, 1000-spaced → call = 66000 (next above), put = 65000 (below).
    ch = _chain([64000, 65000, 66000, 67000])
    pair = select_straddle_pair(ch, 65490.0)
    assert pair is not None
    assert pair.call.strike == 66000.0
    assert pair.put.strike == 65000.0


def test_call_and_put_have_different_strikes():
    ch = _chain([64000, 65000, 66000])
    pair = select_straddle_pair(ch, 65500.0)
    assert pair is not None
    assert pair.call.strike == 66000.0
    assert pair.put.strike == 65000.0
    assert pair.call.strike != pair.put.strike
    # StraddlePair.strike carries the CALL strike for legacy call sites.
    assert pair.strike == pair.call.strike == 66000.0


def test_spot_just_above_a_strike():
    # spot 65010 (500-spaced) → call = 65500, put = 65000.
    ch = _chain([64500, 65000, 65500, 66000])
    pair = select_straddle_pair(ch, 65010.0)
    assert pair.call.strike == 65500.0
    assert pair.put.strike == 65000.0


def test_skips_call_strike_without_tradable_ask():
    # 65500 (first OTM call) has no ask → next OTM call is 66000.
    ch = _chain([64500, 65000, 65500, 66000], no_ask=(65500,))
    pair = select_straddle_pair(ch, 65010.0)
    assert pair is not None
    assert pair.call.strike == 66000.0
    assert pair.put.strike == 65000.0


def test_returns_none_when_no_otm_call():
    # spot above every strike → no strike strictly above → no OTM call.
    ch = _chain([64000, 65000])
    assert select_straddle_pair(ch, 70000.0) is None


def test_returns_none_when_no_otm_put():
    # spot below every strike → no strike strictly below → no OTM put.
    ch = _chain([65000, 66000])
    assert select_straddle_pair(ch, 60000.0) is None


# ───────────────────── per-leg wing selection ──────────────────────

def test_wings_one_strike_beyond_each_long_leg():
    # Long call 66000 / long put 65000, offset 1 → short call 66500,
    # short put 64500 (500-spaced).
    ch = _chain([64000, 64500, 65000, 65500, 66000, 66500, 67000])
    w = select_wings(ch, call_body_strike=66000.0, put_body_strike=65000.0,
                     call_offset=1, put_offset=1)
    assert w.call is not None and w.call.strike == 66500.0
    assert w.put is not None and w.put.strike == 64500.0


def test_wing_requires_live_bid():
    # short call target 66500 has no bid → call wing None; put wing still ok.
    ch = _chain([64500, 65000, 66000, 66500], no_bid=(66500,))
    w = select_wings(ch, call_body_strike=66000.0, put_body_strike=65000.0,
                     call_offset=1, put_offset=1)
    assert w.call is None
    assert w.put is not None and w.put.strike == 64500.0


def test_wing_missing_when_no_further_strike():
    # No strike beyond the long call (66000 is the top) → call wing None.
    ch = _chain([64500, 65000, 66000])
    w = select_wings(ch, call_body_strike=66000.0, put_body_strike=65000.0,
                     call_offset=1, put_offset=1)
    assert w.call is None
    assert w.put is not None and w.put.strike == 64500.0


# ───────────────────── per-session wing gating ───────────────────────

class _S:
    def __init__(self, h, m):
        self.entry_utc = time(h, m)


def test_wing_window_gating():
    prev = config.ENABLE_WINGS
    config.ENABLE_WINGS = True
    try:
        for h, m in [(13, 0), (13, 30), (14, 0), (14, 30)]:
            assert config.session_wings_enabled(_S(h, m)), f"{h}:{m} should wing"
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


def test_wings_off_by_default():
    # This stack ships with ENABLE_WINGS default OFF (long strangle only).
    assert config.ENABLE_WINGS is False


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
