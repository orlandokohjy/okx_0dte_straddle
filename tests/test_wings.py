"""
Offline unit tests for the iron-fly wings feature.

No network / OKX SDK required. Runnable either with pytest
(``python -m pytest tests/test_wings.py``) or directly
(``python tests/test_wings.py``) — every async case is wrapped in
``asyncio.run`` so pytest-asyncio is NOT needed.

Coverage:
  1. Wing strike selection (adjacent listed strikes, valid-bid gate,
     insufficient-strike fallback).
  2. Short-leg (wing) P&L sign + magnitude, CM and UM.
  3. close_straddle folds wing P&L + wing fees into net/equity.
  4. SHORTS-FIRST close ordering invariant (wings bought back before the
     body is sold) — the core safety property.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config
from data.option_chain import OptionChain, OptionInfo
from strategy.option_selector import select_wings
from core.portfolio import Portfolio, Straddle, StraddleLeg


# ────────────────────────── helpers ──────────────────────────────

def _chain(call_strikes, put_strikes, *, bid=1.0):
    """Build an OptionChain with the given strikes. ``bid`` may be a dict
    {strike: bid} to selectively zero out a strike's bid."""
    ch = OptionChain(exchange=None)  # select_wings only reads .calls/.puts

    def _bid(s):
        return bid[s] if isinstance(bid, dict) else bid

    for s in call_strikes:
        ch.calls.append(OptionInfo(
            symbol=f"BTC-USD-260709-{int(s)}-C", strike=s, option_type="C",
            bid=_bid(s), ask=_bid(s) + 5, mark=_bid(s) + 2))
    for s in put_strikes:
        ch.puts.append(OptionInfo(
            symbol=f"BTC-USD-260709-{int(s)}-P", strike=s, option_type="P",
            bid=_bid(s), ask=_bid(s) + 5, mark=_bid(s) + 2))
    ch.calls.sort(key=lambda x: x.strike)
    ch.puts.sort(key=lambda x: x.strike)
    return ch


def _leg(sym, side, qty, px):
    return StraddleLeg(instrument=sym, side=side, qty=qty, entry_price=px,
                       avg_fill_price=px)


def _straddle_with_wings(*, family="UM", num=1, qty=0.5):
    """A body straddle plus both short wings, prices chosen so P&L is exact
    and easy to reason about in UM (USD-native) units."""
    s = Straddle(
        id="OKX-TEST-0001",
        call_leg=_leg("BTC-USD_UM-260709-62000-C", "Buy", qty, 300.0),
        put_leg=_leg("BTC-USD_UM-260709-62000-P", "Buy", qty, 280.0),
        strike=62000.0, qty_per_leg=qty,
        entry_time="2026-07-09T13:30:00+00:00",
        entry_call_price=300.0, entry_put_price=280.0,
        straddle_cost=(300.0 + 280.0) * qty, num_straddles=num,
        family=family, entry_spot_price=62000.0,
    )
    # short call wing @ 63000, sold for 120; short put wing @ 61500, sold 110
    s.call_wing_leg = _leg("BTC-USD_UM-260709-63000-C", "Sell", qty, 120.0)
    s.put_wing_leg = _leg("BTC-USD_UM-260709-61500-P", "Sell", qty, 110.0)
    s.call_wing_strike = 63000.0
    s.put_wing_strike = 61500.0
    s.entry_call_wing_price = 120.0
    s.entry_put_wing_price = 110.0
    return s


# ────────────────────────── 1. wing selection ─────────────────────

def test_select_wings_adjacent_offsets():
    # strikes spaced 500 apart; body at 62000
    strikes = [61000, 61500, 62000, 62500, 63000, 63500]
    ch = _chain(strikes, strikes)
    w = select_wings(ch, 62000.0, call_offset=2, put_offset=1)
    assert w.call is not None and w.put is not None
    # call wing = 2 strikes above body: 62500 (1st), 63000 (2nd)
    assert w.call.strike == 63000.0
    # put wing = 1 strike below body: 61500
    assert w.put.strike == 61500.0


def test_select_wings_requires_live_bid():
    strikes = [61500, 62000, 62500, 63000]
    # zero the bid on the intended call wing (63000) → must be rejected
    ch = _chain(strikes, strikes, bid={61500: 1.0, 62000: 1.0,
                                       62500: 1.0, 63000: 0.0})
    w = select_wings(ch, 62000.0, call_offset=2, put_offset=1)
    assert w.call is None            # no live bid at 63000
    assert w.put is not None and w.put.strike == 61500.0


def test_select_wings_insufficient_strikes():
    # only one strike above body → cannot satisfy call_offset=2
    ch = _chain([62000, 62500], [61500, 62000])
    w = select_wings(ch, 62000.0, call_offset=2, put_offset=1)
    assert w.call is None
    assert w.put is not None


# ────────────────────────── 2. short-leg P&L ──────────────────────

def test_short_wing_pnl_sign_um():
    s = _straddle_with_wings(family="UM", qty=0.5, num=2)
    # short call sold @120, bought back @70 → profit (120-70)=50 per BTC
    pnl = s._short_leg_usd_pnl(entry_px=120.0, exit_px=70.0)
    assert abs(pnl - (0.5 * 2 * (120.0 - 70.0))) < 1e-9  # = 50.0
    # buying back HIGHER than sold = loss (negative)
    loss = s._short_leg_usd_pnl(entry_px=120.0, exit_px=150.0)
    assert loss < 0


def test_short_wing_pnl_sign_cm():
    # CM: USD = qty*num*(entry_px*entry_spot - exit_px*exit_spot)
    s = _straddle_with_wings(family="CM", qty=1.0, num=1)
    s.entry_spot_price = 60000.0
    pnl = s._short_leg_usd_pnl(entry_px=0.002, exit_px=0.001, exit_spot=60000.0)
    assert abs(pnl - (1.0 * 1 * (0.002 * 60000 - 0.001 * 60000))) < 1e-6


def test_wings_pnl_combines_both_sides():
    s = _straddle_with_wings(family="UM", qty=0.5, num=1)
    # buy both wings back at half the credit → profit = 0.5*(60 + 55)
    total = s.wings_pnl(exit_call_wing=60.0, exit_put_wing=55.0)
    expected = 0.5 * ((120 - 60) + (110 - 55))
    assert abs(total - expected) < 1e-9


# ─────────────────── 3. close_straddle folds wings ────────────────

def test_close_straddle_includes_wing_pnl_and_fees(tmp_state=None):
    tmpdir = tempfile.mkdtemp()
    config.STATE_DIR = tmpdir
    config.EQUITY_FILE = os.path.join(tmpdir, "equity.json")
    config.POSITIONS_FILE = os.path.join(tmpdir, "positions.json")
    config.TRADE_LOG_FILE = os.path.join(tmpdir, "trade_log.csv")

    pf = Portfolio()
    start_equity = pf.equity
    s = _straddle_with_wings(family="UM", qty=0.5, num=1)
    # give the wings a known maker fee (credit) so we can verify it flows
    s.call_wing_leg.entry_metrics = {"fee_usd": 0.10}
    s.put_wing_leg.entry_metrics = {"fee_usd": 0.10}
    pf.set_straddle(s)

    # Body: call 300→350 (+25 on 0.5), put 280→250 (-15 on 0.5) = +10
    # Wings bought back: call 120→60 (+30), put 110→55 (+27.5) = +57.5
    net = pf.close_straddle(
        350.0, 250.0, "test",
        exit_call_wing_price=60.0, exit_put_wing_price=55.0,
    )
    body = 0.5 * ((350 - 300) + (250 - 280))            # = +10.0
    wings = 0.5 * ((120 - 60) + (110 - 55))              # = +57.5
    fees = 0.20                                          # wing maker credits
    assert abs(net - (body + wings + fees)) < 1e-6
    assert abs((pf.equity - start_equity) - net) < 1e-6
    # trade log wrote a row with the wing columns present
    with open(config.TRADE_LOG_FILE) as f:
        header = f.readline()
    assert "wings_pnl" in header and "call_wing_strike" in header


# ─────────────── 4. SHORTS-FIRST close ordering (safety) ──────────

class _FakeMarket:
    async def get_option_bid_ask(self, sym):
        return (10.0, 12.0)


class _FakeExchange:
    """Records the ORDER of operations so we can assert shorts-first."""
    def __init__(self):
        self.ops: list[str] = []

    async def get_spot_price(self):
        return 62000.0

    async def send_rfq_sell(self, *a, **k):
        return None  # force the leg-by-leg chase path

    async def chase_buy(self, instrument, qty, ref, **k):
        self.ops.append(f"BUY {instrument}")   # wing buy-to-close
        return {"average_price": 60.0, "order_id": "b1",
                "filled_qty_btc": qty, "fully_filled": True, "metrics": {}}

    async def chase_sell(self, instrument, qty, ref, **k):
        self.ops.append(f"SELL {instrument}")  # body sell-to-close
        return {"average_price": 340.0, "order_id": "s1",
                "filled_qty_btc": qty, "fully_filled": True, "metrics": {}}

    async def get_option_iv_batch(self, syms):
        return {}

    async def list_open_positions(self):
        # After the (successful) wing buy-backs the book is flat, so both
        # body legs are free to sell.
        return []


def test_close_is_shorts_first():
    from strategy import straddle_builder

    tmpdir = tempfile.mkdtemp()
    config.STATE_DIR = tmpdir
    config.EQUITY_FILE = os.path.join(tmpdir, "equity.json")
    config.POSITIONS_FILE = os.path.join(tmpdir, "positions.json")
    config.TRADE_LOG_FILE = os.path.join(tmpdir, "trade_log.csv")

    pf = Portfolio()
    s = _straddle_with_wings(family="UM", qty=0.5, num=1)
    pf.set_straddle(s)

    ex, mk = _FakeExchange(), _FakeMarket()
    asyncio.run(straddle_builder.unwind_straddle(ex, mk, pf, reason="test"))

    # Both wing buy-backs must occur BEFORE any body sell.
    first_sell = next(i for i, o in enumerate(ex.ops) if o.startswith("SELL"))
    buy_idxs = [i for i, o in enumerate(ex.ops) if o.startswith("BUY")]
    assert buy_idxs, f"expected wing buy-to-close ops, got {ex.ops}"
    assert max(buy_idxs) < first_sell, (
        f"SHORTS-FIRST violated: {ex.ops}")
    # Exchange is flat (list_open_positions == []), so the two-phase finalize
    # must BOOK the close: straddle is no longer open.
    assert not pf.has_open, "flat exchange must finalize the close"


# ─────── 4b. NAKED-SHORT GUARD: hold body leg if wing not closed ──────

class _StuckWingExchange:
    """Call-wing buy-back FAILS and the book still shows it short; the put
    wing closes cleanly. The call BODY leg must therefore be HELD (not sold)
    while the put body sells normally — never a naked short call."""
    def __init__(self, call_wing_inst: str):
        self.ops: list[str] = []
        self._call_wing_inst = call_wing_inst

    async def get_spot_price(self):
        return 62000.0

    async def send_rfq_sell(self, *a, **k):
        self.ops.append("RFQ")            # must NOT be used when deferring
        return None

    async def chase_buy(self, instrument, qty, ref, **k):
        self.ops.append(f"BUY {instrument}")
        if instrument == self._call_wing_inst:
            return None                   # call wing buy-back fails
        return {"average_price": 55.0, "order_id": "b2",
                "filled_qty_btc": qty, "fully_filled": True, "metrics": {}}

    async def chase_sell(self, instrument, qty, ref, **k):
        self.ops.append(f"SELL {instrument}")
        return {"average_price": 340.0, "order_id": "s2",
                "filled_qty_btc": qty, "fully_filled": True, "metrics": {}}

    async def get_option_iv_batch(self, syms):
        return {}

    async def list_open_positions(self):
        # Call wing is STILL short; everything else is flat.
        return [{"instrument_name": self._call_wing_inst, "amount": -1.0}]


def test_body_leg_held_when_wing_buyback_fails():
    from strategy import straddle_builder

    tmpdir = tempfile.mkdtemp()
    config.STATE_DIR = tmpdir
    config.EQUITY_FILE = os.path.join(tmpdir, "equity.json")
    config.POSITIONS_FILE = os.path.join(tmpdir, "positions.json")
    config.TRADE_LOG_FILE = os.path.join(tmpdir, "trade_log.csv")

    pf = Portfolio()
    s = _straddle_with_wings(family="UM", qty=0.5, num=1)
    pf.set_straddle(s)

    call_wing_inst = s.call_wing_leg.instrument
    ex, mk = _StuckWingExchange(call_wing_inst), _FakeMarket()
    asyncio.run(straddle_builder.unwind_straddle(ex, mk, pf, reason="test"))

    call_body = s.call_leg.instrument
    put_body = s.put_leg.instrument
    sells = [o for o in ex.ops if o.startswith("SELL")]
    # The naked-short guard: the call BODY (covers the stuck short call wing)
    # must NOT be sold; the put body sells normally.
    assert f"SELL {call_body}" not in ex.ops, (
        f"naked-short guard failed — sold covering call body: {ex.ops}")
    assert f"SELL {put_body}" in ex.ops, (
        f"put body should still sell: {ex.ops}")
    # And we must NOT have used the atomic RFQ (it would flatten both legs).
    assert "RFQ" not in ex.ops, f"RFQ must be skipped when deferring: {ex.ops}"


# ───── 4c. TWO-PHASE FINALIZE: defer the close while a leg is open ────

class _PartialBodyExchange:
    """The put body sells, but the CALL body sell fails and the live book
    still shows it long. The unwind must DEFER finalization (leave the
    straddle open) rather than book a phantom close with an entry-price
    exit — the exact bug behind 'SESSION CLOSE with P&L while legs open'."""
    def __init__(self, stuck_inst: str):
        self.ops: list[str] = []
        self._stuck = stuck_inst

    async def get_spot_price(self):
        return 62000.0

    async def send_rfq_sell(self, *a, **k):
        return None

    async def chase_buy(self, instrument, qty, ref, **k):
        return {"average_price": 55.0, "order_id": "b",
                "filled_qty_btc": qty, "fully_filled": True, "metrics": {}}

    async def chase_sell(self, instrument, qty, ref, **k):
        self.ops.append(f"SELL {instrument}")
        if instrument == self._stuck:
            return None                      # call body sell fails
        return {"average_price": 340.0, "order_id": "s",
                "filled_qty_btc": qty, "fully_filled": True, "metrics": {}}

    async def get_option_iv_batch(self, syms):
        return {}

    async def list_open_positions(self):
        # Call body is STILL long → NOT flat.
        return [{"instrument_name": self._stuck, "amount": 1.0}]


def test_unwind_defers_finalize_when_not_flat():
    from strategy import straddle_builder

    tmpdir = tempfile.mkdtemp()
    config.STATE_DIR = tmpdir
    config.EQUITY_FILE = os.path.join(tmpdir, "equity.json")
    config.POSITIONS_FILE = os.path.join(tmpdir, "positions.json")
    config.TRADE_LOG_FILE = os.path.join(tmpdir, "trade_log.csv")

    pf = Portfolio()
    s = _straddle_with_wings(family="UM", qty=0.5, num=1)
    s.call_wing_leg = None   # isolate the BODY defer (no wings)
    s.put_wing_leg = None
    pf.set_straddle(s)

    stuck = s.call_leg.instrument
    ex, mk = _PartialBodyExchange(stuck), _FakeMarket()
    pnl = asyncio.run(
        straddle_builder.unwind_straddle(ex, mk, pf, reason="test"))

    # DEFER: the straddle must remain OPEN (not booked) and no P&L returned.
    assert pf.has_open, "must defer finalize while a leg is still open"
    assert pnl == 0.0
    # The put body that DID fill recorded its real exit fill for the later
    # two-phase finalize; the stuck call body did not.
    assert s.put_leg.instrument in s.exit_fills
    assert s.call_leg.instrument not in s.exit_fills


def test_record_exit_fill_ignores_bad_values():
    s = _straddle_with_wings(family="UM", qty=0.5, num=1)
    s.record_exit_fill("X", 0.0)        # non-positive
    s.record_exit_fill("Y", None)       # type: ignore[arg-type]
    s.record_exit_fill("", 10.0)        # empty instrument
    s.record_exit_fill("Z", 12.5)       # valid
    assert s.exit_fills == {"Z": 12.5}


# ─────────────── 5. SESSION CLOSE message renders wings ───────────

def test_close_message_renders_wings():
    from core.notifier import _format_close_message
    s = _straddle_with_wings(family="UM", qty=0.5, num=1)
    # simulate a completed close with wing buy-backs recorded
    s.exit_call_price = 350.0
    s.exit_put_price = 250.0
    s.exit_call_wing_price = 60.0
    s.exit_put_wing_price = 55.0
    s.gross_pnl = 67.5
    s.fees = 0.0
    s.pnl = 67.5
    msg = _format_close_message(67.5, session_label="13:30-15:30 UTC",
                                straddle=s)
    assert "Call wing (short)" in msg
    assert "Put wing (short)" in msg
    assert "(call + put + wings)" in msg


def test_close_message_no_wings_unchanged():
    from core.notifier import _format_close_message
    s = _straddle_with_wings(family="UM", qty=0.5, num=1)
    s.call_wing_leg = None
    s.put_wing_leg = None
    s.exit_call_price = 350.0
    s.exit_put_price = 250.0
    s.gross_pnl = 10.0
    s.fees = 0.0
    s.pnl = 10.0
    msg = _format_close_message(10.0, straddle=s)
    assert "wing" not in msg.lower()
    assert "(call + put)" in msg


def test_close_message_unfilled_wing_shows_open_not_phantom_pnl():
    from core.notifier import _format_close_message
    s = _straddle_with_wings(family="UM", qty=1.0, num=1)
    s.exit_call_price = 350.0
    s.exit_put_price = 250.0
    # Call wing bought back; PUT wing buy-back FAILED (exit price stays None).
    s.exit_call_wing_price = 60.0
    s.exit_put_wing_price = None
    s.gross_pnl = 5.0
    s.fees = 0.0
    s.pnl = 5.0
    msg = _format_close_message(5.0, straddle=s)
    # The unfilled put wing must NOT render its full credit as realised P&L.
    assert "STILL OPEN" in msg
    assert "unrealised" in msg


def test_iron_fly_entry_summary_renders_both_sides():
    from strategy.straddle_builder import _format_iron_fly_entry
    s = _straddle_with_wings(family="CM", qty=1.0, num=1)
    s.entry_spot_price = 64085.0
    msg = _format_iron_fly_entry(s)
    assert "IRON FLY ENTERED" in msg
    assert "LONG body" in msg
    assert "SHORT wings" in msg
    assert "Call wing" in msg and "Put wing" in msg
    assert "Net entry" in msg


def test_iron_fly_entry_summary_marks_missing_wing():
    from strategy.straddle_builder import _format_iron_fly_entry
    s = _straddle_with_wings(family="CM", qty=1.0, num=1)
    s.entry_spot_price = 64085.0
    s.call_wing_leg = None  # one side degraded to body-only
    msg = _format_iron_fly_entry(s)
    assert "not sold" in msg
    assert "Put wing" in msg


# ────────────────────────── runner ────────────────────────────────

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
