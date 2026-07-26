"""
Tests for the OTM-strangle selection on the ETH variant.

select_straddle_pair buys the next OTM call (nearest listed strike strictly
ABOVE spot) and next OTM put (nearest listed strike strictly BELOW spot) —
two DIFFERENT strikes straddling spot. (Replaces the ATM nearest-strike
selector; no short-wing overlay on the ETH stack.)

Runnable directly (python tests/test_otm_strangle.py) or via pytest.
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

def test_picks_next_otm_call_and_put():
    # spot 1838 → call = 1850 (next above), put = 1825 (next below).
    ch = _chain([1800, 1825, 1850, 1875])
    pair = select_straddle_pair(ch, 1838.0)
    assert pair is not None
    assert pair.call.strike == 1850.0
    assert pair.put.strike == 1825.0
    assert pair.call.strike != pair.put.strike


def test_strangle_straddles_spot():
    # spot 1830 → call = 1850, put = 1825 (spot sits between the legs).
    ch = _chain([1800, 1825, 1850, 1875])
    pair = select_straddle_pair(ch, 1830.0)
    assert pair.put.strike < 1830.0 < pair.call.strike
    assert pair.call.strike == 1850.0 and pair.put.strike == 1825.0


def test_strike_field_is_call_strike():
    ch = _chain([1800, 1825, 1850])
    pair = select_straddle_pair(ch, 1820.0)
    assert pair.strike == pair.call.strike == 1825.0
    assert pair.put.strike == 1800.0


def test_skips_call_strike_without_tradable_ask():
    # first OTM call (1850) has no ask → next OTM call 1875.
    ch = _chain([1800, 1825, 1850, 1875], no_ask=(1850,))
    pair = select_straddle_pair(ch, 1838.0)
    assert pair is not None
    assert pair.call.strike == 1875.0
    assert pair.put.strike == 1825.0


def test_returns_none_when_no_otm_call():
    ch = _chain([1800, 1825])
    assert select_straddle_pair(ch, 1900.0) is None


def test_returns_none_when_no_otm_put():
    ch = _chain([1825, 1850])
    assert select_straddle_pair(ch, 1800.0) is None


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
