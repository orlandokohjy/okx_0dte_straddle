"""Monthly volume tracking for option contracts and BTC notional."""
from __future__ import annotations

import csv
import os
from datetime import datetime

import structlog

import config

log = structlog.get_logger(__name__)


def _current_month_key() -> str:
    return datetime.utcnow().strftime("%Y-%m")


def record_trade(num_straddles: int, qty_per_leg: float) -> None:
    """
    Append volume for one session's trades.

    Per straddle (pure: 1 call + 1 put):
      option_contracts = 4  (buy call + buy put + sell call + sell put)
      option_btc       = qty_per_leg × 2 legs × 2 sides

    qty_per_leg is the BTC notional per leg passed in by the caller —
    typically the entry-time RESOLVED qty from
    ``strategy.sizing.compute_qty_per_leg``. Under fixed_btc this is
    the session's static qty (e.g. 0.5 BTC); under pct_equity it's the
    qty derived from current equity at fire-time (e.g. 2.85 BTC for a
    50% session at $7.7k equity). Volume rows therefore record the
    actual notional traded for THIS specific entry, not the historical
    default.
    """
    contracts_per = 4
    option_btc_per = qty_per_leg * 2 * 2

    # qty_per_leg lives at the END so existing volume.csv files (which
    # were written before the multi-session refactor) stay column-aligned
    # for the first four fields when appended to. New rows simply gain
    # one extra column at the right; older rows have it blank when read.
    row = {
        "month": _current_month_key(),
        "num_straddles": num_straddles,
        "option_contracts": contracts_per * num_straddles,
        "option_btc_notional": option_btc_per * num_straddles,
        "qty_per_leg": qty_per_leg,
    }

    os.makedirs(os.path.dirname(config.VOLUME_FILE), exist_ok=True)
    file_exists = os.path.exists(config.VOLUME_FILE)

    with open(config.VOLUME_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    log.info("volume_recorded", **row)
