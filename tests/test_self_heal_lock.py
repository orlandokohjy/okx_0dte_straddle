"""
Offline unit tests for the self-healing entry lock (orphan auto-release).

No network / OKX SDK required. Runnable with pytest
(``python -m pytest tests/test_self_heal_lock.py``) or directly
(``python tests/test_self_heal_lock.py``) — every async case is wrapped in
``asyncio.run`` so pytest-asyncio is NOT needed.

Coverage:
  1. _set_entry_lock records the clearable flag, and a later kill-switch lock
     can NEVER inherit a stale clearable flag from an earlier orphan lock.
  2. A clearable orphan lock auto-releases when the exchange is confirmed flat.
  3. It stays latched when: the exchange still has a position, the lock is a
     kill-switch (non-clearable), the feature flag is off, creds are absent,
     or the position fetch fails (fail-closed).
"""
from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config
import main


class _FakeExchange:
    """Minimal stand-in exposing only ``list_open_positions``."""

    def __init__(self, positions=None, raises=False):
        self._positions = positions or []
        self._raises = raises
        self.calls = 0

    async def list_open_positions(self):
        self.calls += 1
        if self._raises:
            raise RuntimeError("simulated fetch failure")
        return list(self._positions)


def _algo(exchange):
    """Build an Algo WITHOUT running __init__ (avoids all network deps),
    then set only the attributes the lock methods touch."""
    a = main.Algo.__new__(main.Algo)
    a.exchange = exchange
    a._entry_locked = False
    a._lock_reason = ""
    a._lock_clearable_when_flat = False
    return a


class _Cfg:
    """Context manager to set config flags and restore them afterwards."""

    def __init__(self, **kw):
        self._kw = kw
        self._saved = {}

    def __enter__(self):
        for k, v in self._kw.items():
            self._saved[k] = getattr(config, k)
            setattr(config, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            setattr(config, k, v)


# Silence Telegram — _maybe_release_orphan_lock sends a success note.
async def _noop_send(*a, **k):
    return None


main.notifier.send = _noop_send


# ─────────────────────────── tests ───────────────────────────────

def test_set_entry_lock_flag_and_no_stale_inheritance():
    a = _algo(_FakeExchange())

    # Kill-switch lock → not clearable.
    a._set_entry_lock("config broke")
    assert a._entry_locked is True
    assert a._lock_clearable_when_flat is False

    # Orphan lock → clearable.
    a._set_entry_lock("orphan", clearable_when_flat=True)
    assert a._lock_clearable_when_flat is True

    # A subsequent kill-switch lock must RESET the flag (no stale True).
    a._set_entry_lock("circuit breaker")
    assert a._lock_clearable_when_flat is False, \
        "kill-switch lock inherited a stale clearable flag"
    print("PASS: _set_entry_lock flag + no stale inheritance")


def test_auto_release_when_flat():
    with _Cfg(SELF_HEAL_LOCK_ON_FLAT=True, HAS_OKX_CREDS=True):
        ex = _FakeExchange(positions=[])  # flat
        a = _algo(ex)
        a._set_entry_lock("Post-close orphan", clearable_when_flat=True)

        released = asyncio.run(a._maybe_release_orphan_lock("utc_0900"))
        assert released is True
        assert a._entry_locked is False
        assert a._lock_reason == ""
        assert a._lock_clearable_when_flat is False
        assert ex.calls == 1  # it actually re-queried the exchange
    print("PASS: orphan lock auto-releases when exchange is flat")


def test_stays_locked_when_not_flat():
    with _Cfg(SELF_HEAL_LOCK_ON_FLAT=True, HAS_OKX_CREDS=True):
        ex = _FakeExchange(positions=[{"instrument_name": "ETH-USD-P",
                                       "amount": 20.0}])
        a = _algo(ex)
        a._set_entry_lock("Post-close orphan", clearable_when_flat=True)

        released = asyncio.run(a._maybe_release_orphan_lock("utc_0900"))
        assert released is False
        assert a._entry_locked is True  # still locked
    print("PASS: stays locked while a live position remains")


def test_killswitch_lock_never_clears():
    with _Cfg(SELF_HEAL_LOCK_ON_FLAT=True, HAS_OKX_CREDS=True):
        ex = _FakeExchange(positions=[])  # even though flat…
        a = _algo(ex)
        a._set_entry_lock("Chase-pricing self-test failed")  # kill-switch

        released = asyncio.run(a._maybe_release_orphan_lock("utc_0900"))
        assert released is False
        assert a._entry_locked is True
        assert ex.calls == 0  # short-circuits before hitting the exchange
    print("PASS: kill-switch lock never auto-clears (no exchange call)")


def test_flag_off_disables_autorelease():
    with _Cfg(SELF_HEAL_LOCK_ON_FLAT=False, HAS_OKX_CREDS=True):
        ex = _FakeExchange(positions=[])
        a = _algo(ex)
        a._set_entry_lock("Post-close orphan", clearable_when_flat=True)

        released = asyncio.run(a._maybe_release_orphan_lock("utc_0900"))
        assert released is False
        assert a._entry_locked is True
        assert ex.calls == 0
    print("PASS: feature flag off keeps the old manual behaviour")


def test_fetch_failure_fails_closed():
    with _Cfg(SELF_HEAL_LOCK_ON_FLAT=True, HAS_OKX_CREDS=True):
        ex = _FakeExchange(raises=True)
        a = _algo(ex)
        a._set_entry_lock("Post-close orphan", clearable_when_flat=True)

        released = asyncio.run(a._maybe_release_orphan_lock("utc_0900"))
        assert released is False
        assert a._entry_locked is True  # fail-closed on error
    print("PASS: position-fetch failure fails closed (stays locked)")


def test_no_creds_stays_locked():
    with _Cfg(SELF_HEAL_LOCK_ON_FLAT=True, HAS_OKX_CREDS=False):
        ex = _FakeExchange(positions=[])
        a = _algo(ex)
        a._set_entry_lock("Post-close orphan", clearable_when_flat=True)

        released = asyncio.run(a._maybe_release_orphan_lock("utc_0900"))
        assert released is False
        assert a._entry_locked is True
        assert ex.calls == 0
    print("PASS: no credentials keeps the lock latched")


if __name__ == "__main__":
    test_set_entry_lock_flag_and_no_stale_inheritance()
    test_auto_release_when_flat()
    test_stays_locked_when_not_flat()
    test_killswitch_lock_never_clears()
    test_flag_off_disables_autorelease()
    test_fetch_failure_fails_closed()
    test_no_creds_stays_locked()
    print("\nAll self-heal-lock tests passed.")
