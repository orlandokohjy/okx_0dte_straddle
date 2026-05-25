"""Self-contained test for the OKXExchange._call retry layer.

Verifies the four invariants of the retry policy added to fix the
2026-05-23 / 2026-05-25 RemoteProtocolError chase aborts:

  1. A whitelisted read (e.g. get_ticker) that raises a transient
     RemoteProtocolError ONCE before succeeding is retried internally
     and ultimately returns the success payload — the caller (the
     chase loop) sees no error.

  2. A whitelisted read that raises RemoteProtocolError on EVERY
     attempt eventually exhausts _RETRY_MAX_RETRIES and re-raises the
     original exception — failure semantics unchanged after exhaustion.

  3. A non-whitelisted write (place_order) that raises
     RemoteProtocolError is NEVER retried — re-raised on the FIRST
     failure with attempt=1. This is the safety-critical invariant
     that prevents duplicate-order fills.

  4. A non-transient exception (ValueError) raised by ANY call —
     whitelisted or not — is re-raised immediately on attempt 1
     without retry.

Run inside the algo container so httpx / httpcore are present:

    docker-compose run --rm --entrypoint "" algo \
        python tools/test_call_retry.py

The script prints PASS / FAIL per case and exits non-zero on any
failure. No live OKX calls are made.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any

# Make `core.*` importable when run as a script from the repo root or
# from inside `tools/`. Inside the algo container `/app` is on sys.path
# already; this is just so the file is also runnable on the host
# (e.g. `python3 tools/test_call_retry.py`).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _build_exchange():
    """Construct an OKXExchange without calling .connect() so we don't
    touch the live OKX SDK or hit the network."""
    # Importing here so a syntax / import error surfaces as test failure
    # rather than a top-level import crash.
    from core.exchange import OKXExchange  # noqa: WPS433
    ex = OKXExchange()
    return ex


def _make_transient_exc():
    """Return a fresh httpcore.RemoteProtocolError matching what OKX's
    HTTP/2 frontend produces in the wild ("Server disconnected")."""
    import httpcore  # noqa: WPS433
    return httpcore.RemoteProtocolError("Server disconnected")


class _SyncFn:
    """Callable wrapper that mimics a python-okx SDK method.

    SDK methods are sync (we wrap them with asyncio.to_thread). The
    `__name__` is what _call uses to decide retry-eligibility, so we
    let the caller spoof that explicitly.
    """

    def __init__(self, name: str, behaviour):
        self.__name__ = name
        self._behaviour = behaviour
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self._behaviour(self.calls)


async def _case_1_retry_then_succeed(ex) -> tuple[bool, str]:
    """Whitelisted read fails once with RemoteProtocolError, then
    returns a normal payload. Retry should swallow the failure."""
    expected_payload = {"code": "0", "data": [{"last": "0.005"}]}

    def behaviour(call_num: int) -> Any:
        if call_num == 1:
            raise _make_transient_exc()
        return expected_payload

    fn = _SyncFn("get_ticker", behaviour)
    t0 = time.time()
    try:
        result = await ex._call(fn, instId="BTC-USD-260526-77250-C")
    except Exception as exc:  # noqa: BLE001
        return False, f"unexpected exception {type(exc).__name__}: {exc}"
    elapsed = time.time() - t0

    if result != expected_payload:
        return False, f"wrong payload returned: {result!r}"
    if fn.calls != 2:
        return False, f"expected 2 calls (1 failure + 1 retry), got {fn.calls}"
    # First retry backoff is 0.2s; total elapsed should be >= 0.2s
    if elapsed < 0.18:
        return False, f"backoff not honoured (elapsed={elapsed:.3f}s)"
    if elapsed > 1.0:
        return False, f"unexpected elapsed time {elapsed:.3f}s"
    return True, f"calls={fn.calls} elapsed={elapsed:.3f}s"


async def _case_2_retry_exhausted(ex) -> tuple[bool, str]:
    """Whitelisted read fails on every attempt → re-raises after
    _RETRY_MAX_RETRIES retries (4 total attempts)."""
    from core.exchange import _RETRY_MAX_RETRIES  # noqa: WPS433
    expected_total_attempts = _RETRY_MAX_RETRIES + 1

    def behaviour(call_num: int) -> Any:
        raise _make_transient_exc()

    fn = _SyncFn("get_ticker", behaviour)
    raised: type | None = None
    try:
        await ex._call(fn, instId="BTC-USD-260526-77250-C")
    except Exception as exc:  # noqa: BLE001
        raised = type(exc)

    if raised is None:
        return False, "expected exception but call returned"
    if "RemoteProtocolError" not in raised.__name__:
        return False, f"wrong exception type re-raised: {raised.__name__}"
    if fn.calls != expected_total_attempts:
        return False, (
            f"expected {expected_total_attempts} attempts, got {fn.calls}"
        )
    return True, f"raised={raised.__name__} attempts={fn.calls}"


async def _case_3_place_order_not_retried(ex) -> tuple[bool, str]:
    """place_order is NOT whitelisted → must re-raise on FIRST failure
    with no retry. This is the safety invariant."""
    def behaviour(call_num: int) -> Any:
        raise _make_transient_exc()

    fn = _SyncFn("place_order", behaviour)
    raised: type | None = None
    try:
        await ex._call(
            fn,
            instId="BTC-USD-260526-77250-C",
            side="buy",
            sz="59",
            px="0.006",
            tdMode="cross",
            ordType="post_only",
        )
    except Exception as exc:  # noqa: BLE001
        raised = type(exc)

    if raised is None:
        return False, "expected exception but call returned"
    if fn.calls != 1:
        return False, (
            f"place_order MUST NOT be retried — got {fn.calls} calls "
            f"(expected 1). Duplicate-order risk!"
        )
    return True, f"raised={raised.__name__} attempts={fn.calls}"


async def _case_4_non_transient_not_retried(ex) -> tuple[bool, str]:
    """A non-transient exception (ValueError) on a whitelisted read
    is re-raised immediately. The retry path must only trigger for
    transient HTTP errors."""
    def behaviour(call_num: int) -> Any:
        raise ValueError("malformed JSON response")

    fn = _SyncFn("get_ticker", behaviour)
    raised: type | None = None
    try:
        await ex._call(fn, instId="BTC-USD-260526-77250-C")
    except Exception as exc:  # noqa: BLE001
        raised = type(exc)

    if raised is not ValueError:
        return False, f"expected ValueError, got {raised}"
    if fn.calls != 1:
        return False, (
            f"non-transient exception was retried {fn.calls} times "
            f"(expected exactly 1)"
        )
    return True, f"raised={raised.__name__} attempts={fn.calls}"


async def _main() -> int:
    ex = _build_exchange()

    cases = [
        ("1. whitelisted read: 1 transient → retry → success",
         _case_1_retry_then_succeed),
        ("2. whitelisted read: always fails → re-raise after retries",
         _case_2_retry_exhausted),
        ("3. place_order: transient → re-raise IMMEDIATELY (no retry)",
         _case_3_place_order_not_retried),
        ("4. whitelisted read: non-transient → re-raise immediately",
         _case_4_non_transient_not_retried),
    ]

    failures = 0
    print("=" * 72)
    for label, case in cases:
        try:
            ok, detail = await case(ex)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"test crashed: {type(exc).__name__}: {exc}"
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {label}")
        print(f"        {detail}")
        if not ok:
            failures += 1
    print("=" * 72)
    if failures:
        print(f"FAILED: {failures}/{len(cases)} cases")
        return 1
    print(f"OK: {len(cases)}/{len(cases)} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
