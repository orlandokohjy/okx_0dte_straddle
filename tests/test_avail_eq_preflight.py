"""Isolated availEq pre-flight + buy-51008 is retryable (2026-08-20)."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.exchange import (
    BUY_FATAL_CODES,
    BUY_RETRYABLE_INSUFFICIENT,
    MarginSnapshot,
)
from strategy.sizing import fit_qty_to_spendable, telegram_summary_line


def test_buy_51008_is_retryable_not_fatal():
    assert "51008" in BUY_RETRYABLE_INSUFFICIENT
    assert "51016" in BUY_RETRYABLE_INSUFFICIENT
    assert "51008" not in BUY_FATAL_CODES
    assert "51016" not in BUY_FATAL_CODES
    # Cross-margin net-long remains fatal — do not flip AIML to cross.
    assert "51019" in BUY_FATAL_CODES


def test_spendable_prefers_present_avail_eq():
    snap = MarginSnapshot(total_eq=1229.37, avail_eq=606.12)
    assert snap.spendable == 606.12


def test_spendable_zero_avail_eq_is_real_zero():
    snap = MarginSnapshot(total_eq=1229.37, avail_eq=0.0)
    assert snap.spendable == 0.0


def test_spendable_falls_back_when_avail_eq_omitted():
    snap = MarginSnapshot(total_eq=1229.37, avail_eq=None)
    assert snap.spendable == 1229.37


def test_fit_qty_scales_down_to_contract():
    # 2026-08-20 numbers: 0.50 BTC required ~$951 IM, availEq ~$606.
    fitted = fit_qty_to_spendable(0.50, required_usd=951.0, spendable_usd=606.0)
    assert fitted == 0.30
    assert fitted < 0.50


def test_fit_qty_unchanged_when_avail_covers():
    assert fit_qty_to_spendable(0.50, 792.0, 983.0) == 0.50


def test_fit_qty_zero_when_even_min_does_not_fit():
    assert fit_qty_to_spendable(0.50, 951.0, spendable_usd=10.0) == 0.0


def test_telegram_avail_eq_fit_line():
    line = telegram_summary_line(
        {"decision": "avail_eq_fit", "prior_qty_btc": 0.5},
        0.30,
        1,
    )
    assert "0.5000" in line
    assert "0.3000" in line
    assert "availEq" in line
