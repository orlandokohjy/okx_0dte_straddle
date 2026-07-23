"""
Tests for the ATM strike selection on the ETH variant.

select_straddle_pair now picks the NEAREST listed strike to spot (true ATM,
can be ABOVE spot), replacing the legacy always-ITM selector.

Runnable directly (python tests/test_atm_selection.py) or via pytest.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

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


# ETH-style strikes (25 apart near ATM).

def test_nearest_rounds_down_below_midpoint():
    # spot 1841 → 1850 (9 away) beats 1825 (16 away)
    ch = _chain([1800, 1825, 1850, 1875])
    pair = select_straddle_pair(ch, 1841.0)
    assert pair is not None and pair.strike == 1850.0


def test_nearest_rounds_up_above_spot():
    # spot 1838 → 1850 is above spot and nearest (12) vs 1825 (13). Legacy
    # always-ITM would have picked 1825; new selector picks the true ATM.
    ch = _chain([1800, 1825, 1850, 1875])
    pair = select_straddle_pair(ch, 1838.0)
    assert pair is not None and pair.strike == 1850.0


def test_nearest_rounds_to_below_when_closer():
    # spot 1830 → 1825 (5) beats 1850 (20)
    ch = _chain([1800, 1825, 1850, 1875])
    pair = select_straddle_pair(ch, 1830.0)
    assert pair is not None and pair.strike == 1825.0


def test_tie_breaks_to_lower_strike():
    # spot exactly midway 1837.5 → tie → lower 1825
    ch = _chain([1825, 1850])
    pair = select_straddle_pair(ch, 1837.5)
    assert pair is not None and pair.strike == 1825.0


def test_same_strike_for_call_and_put():
    ch = _chain([1800, 1825, 1850])
    pair = select_straddle_pair(ch, 1820.0)
    assert pair.call.strike == pair.put.strike == pair.strike == 1825.0


def test_skips_strike_without_tradable_ask():
    # nearest (1825) has no ask → fall to next nearest with a tradable pair
    ch = _chain([1800, 1825, 1850], no_ask=(1825,))
    pair = select_straddle_pair(ch, 1828.0)
    assert pair is not None and pair.strike in (1800.0, 1850.0)


def test_returns_none_when_no_tradable_common_strike():
    ch = _chain([1825], no_ask=(1825,))
    assert select_straddle_pair(ch, 1825.0) is None


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
