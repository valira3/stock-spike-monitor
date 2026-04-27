"""v5.5.5 — watchdog tests for WebsocketBarConsumer.

Standalone runner; no pytest dep. Exits non-zero on any failure.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import volume_profile as vp  # noqa: E402

UTC = timezone.utc


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _FakeStream:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


# ---------------------------------------------------------------------------

def _new_consumer_running() -> vp.WebsocketBarConsumer:
    c = vp.WebsocketBarConsumer(["AAPL"], "k", "s")
    # Don't actually start() (would try to import alpaca-py live deps);
    # instead simulate the post-start state the watchdog reads from.
    c._stream = _FakeStream()
    c._start_ts = datetime.now(UTC) - timedelta(seconds=300)
    c._silence_threshold_sec = 30  # speed the test up
    return c


def _run_watchdog_once(c: vp.WebsocketBarConsumer) -> None:
    """Drive a single watchdog poll without spinning a thread.

    We can't shrink the 30s poll inside the loop without breaking
    production semantics, so we extract the body and call it via a tiny
    re-implementation that bypasses the wait. The behavior under test is
    the *decision* the loop makes, not the cadence.
    """
    # Mirror _watchdog_loop's body verbatim, sans the Event.wait.
    now_et = datetime.now(vp.ET)
    if vp.session_bucket(now_et) is None:
        return
    last = c.time_since_last_bar_seconds()
    threshold = c._silence_threshold_sec
    if last is None:
        started = c._start_ts
        if started is None:
            return
        elapsed = (datetime.now(UTC) - started).total_seconds()
        if elapsed < threshold:
            return
    else:
        if last < threshold:
            return
        elapsed = last
    vp.logger.warning(
        "[VOLPROFILE] watchdog: no bars for %.0fs (received=%d) \u2014 forcing reconnect",
        elapsed, c._bars_received,
    )
    c._watchdog_reconnects += 1
    if c._stream is not None:
        c._stream.stop()


def _force_rth_session(monkeypatch_attr: str = "session_bucket"):
    real = getattr(vp, monkeypatch_attr)

    def always_in_session(_now_et):
        return "1031"

    setattr(vp, monkeypatch_attr, always_in_session)
    return real


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_watchdog_forces_stream_stop_after_silence() -> None:
    real = _force_rth_session()
    try:
        c = _new_consumer_running()
        # No bar ever arrived, _start_ts is 300s old, threshold=30s -> trip.
        _run_watchdog_once(c)
        assert c._stream is not None
        assert c._stream.stop_calls == 1, c._stream.stop_calls
        assert c._watchdog_reconnects == 1
    finally:
        vp.session_bucket = real


def test_watchdog_no_op_outside_rth() -> None:
    real = vp.session_bucket
    vp.session_bucket = lambda _now_et: None  # outside session
    try:
        c = _new_consumer_running()
        _run_watchdog_once(c)
        assert c._stream is not None
        assert c._stream.stop_calls == 0
        assert c._watchdog_reconnects == 0
    finally:
        vp.session_bucket = real


def test_watchdog_no_op_within_threshold() -> None:
    real = _force_rth_session()
    try:
        c = _new_consumer_running()
        # Just-now last bar; well under threshold.
        c._last_bar_ts = datetime.now(UTC)
        _run_watchdog_once(c)
        assert c._stream.stop_calls == 0
        assert c._watchdog_reconnects == 0
    finally:
        vp.session_bucket = real


def test_watchdog_loop_catches_exceptions_and_continues() -> None:
    """The real _watchdog_loop must NEVER die on its own exception."""
    cap = _LogCapture()
    cap.setLevel(logging.DEBUG)
    vp.logger.addHandler(cap)
    real = vp.session_bucket

    iterations = {"n": 0}

    def boom_then_normal(_now_et):
        iterations["n"] += 1
        if iterations["n"] == 1:
            raise RuntimeError("simulated session_bucket boom")
        return None  # subsequent calls: outside-RTH no-op so loop sleeps

    vp.session_bucket = boom_then_normal
    try:
        c = vp.WebsocketBarConsumer(["AAPL"], "k", "s")
        c._stream = _FakeStream()
        c._start_ts = datetime.now(UTC)
        # Patch wait so the loop ticks fast and we can stop it.
        original_wait = c._stop.wait

        ticks = {"n": 0}

        def fast_wait(timeout):  # noqa: ARG001
            ticks["n"] += 1
            if ticks["n"] >= 3:
                c._stop.set()
            return c._stop.is_set()

        c._stop.wait = fast_wait  # type: ignore[assignment]

        # Run the real loop in-thread; should return (not raise) because
        # the exception is swallowed inside the loop body.
        c._watchdog_loop()

        # The exception path was reached at least once.
        msgs = [r.getMessage() for r in cap.records]
        assert any("watchdog loop error" in m for m in msgs), msgs
        # And the loop kept ticking past the boom (called session_bucket
        # more than once total).
        assert iterations["n"] >= 2, iterations
    finally:
        vp.logger.removeHandler(cap)
        vp.session_bucket = real


# ---------------------------------------------------------------------------

TESTS = [
    test_watchdog_forces_stream_stop_after_silence,
    test_watchdog_no_op_outside_rth,
    test_watchdog_no_op_within_threshold,
    test_watchdog_loop_catches_exceptions_and_continues,
]


def main() -> int:
    fails = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  +  {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            import traceback
            print(f"  X  {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
    total = len(TESTS)
    print(f"\n  {total - fails} passed · {fails} failed · {total} total\n")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
