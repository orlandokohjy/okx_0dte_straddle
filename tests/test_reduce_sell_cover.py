"""Offline tests: do not place a second reduce-sell when one already covers.

Reproduces the 2026-08-19 wd_0130 case: leftover long 0.3 (30 contracts)
plus a resting sell of 30, then close re-flatten wanting another 30.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.exchange import uncovered_reduce_sell_contracts

INST = "BTC-USD-260819-64300-P"


def _sell(sz, acc=0, oid="1", inst=INST):
    return {"instId": inst, "side": "sell", "sz": str(sz),
            "accFillSz": str(acc), "ordId": oid}


def test_resting_sell_matching_long_needs_zero():
    # Live long 30, resting sell 30 → already covered.
    assert uncovered_reduce_sell_contracts(30, [_sell(30)], INST) == 0


def test_no_resting_sell_needs_full_long():
    assert uncovered_reduce_sell_contracts(30, [], INST) == 30


def test_partial_resting_sell_needs_remainder():
    # Long 50, sell 30 already working → only 20 more.
    assert uncovered_reduce_sell_contracts(50, [_sell(30)], INST) == 20


def test_partially_filled_resting_sell():
    # Sell 30, 10 already filled, 20 still working; long 30 → need 10 more.
    assert uncovered_reduce_sell_contracts(30, [_sell(30, acc=10)], INST) == 10


def test_ignores_buys_and_other_instruments():
    orders = [
        {"instId": INST, "side": "buy", "sz": "30", "accFillSz": "0", "ordId": "b"},
        _sell(30, oid="s", inst="BTC-USD-260819-64300-C"),
    ]
    assert uncovered_reduce_sell_contracts(30, orders, INST) == 30


def test_flat_position_needs_zero():
    assert uncovered_reduce_sell_contracts(0, [_sell(30)], INST) == 0
    assert uncovered_reduce_sell_contracts(-10, [_sell(30)], INST) == 0


def test_two_sells_over_cover():
    # Two 30-lot sells vs 30 long → 0 (the Aug 19 overshoot).
    assert uncovered_reduce_sell_contracts(
        30, [_sell(30, oid="a"), _sell(30, oid="b")], INST,
    ) == 0


if __name__ == "__main__":
    test_resting_sell_matching_long_needs_zero()
    test_no_resting_sell_needs_full_long()
    test_partial_resting_sell_needs_remainder()
    test_partially_filled_resting_sell()
    test_ignores_buys_and_other_instruments()
    test_flat_position_needs_zero()
    test_two_sells_over_cover()
    print("ok")
