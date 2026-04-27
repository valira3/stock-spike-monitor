"""v5.5.5 — observability tests for WebsocketBarConsumer._on_bar.

These tests are standalone (no pytest dependency) so they slot into the
project's existing smoke harness style: each test is a top-level
function, the runner reports pass/fail, and exit code is 0 on success.
Run directly:

    python test_v5_5_5_volprofile_observability.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import volume_profile as vp  # noqa: E402

UTC = timezone.utc
ET = vp.ET


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]


def _attach_capture() -> _LogCapture:
    cap = _LogCapture()
    cap.setLevel(logging.DEBUG)
    vp.logger.addHandler(cap)
    vp.logger.setLevel(logging.DEBUG)
    return cap


def _detach_capture(cap: _LogCapture) -> None:
    vp.logger.removeHandler(cap)


def _make_bar(symbol: str, ts_et_str: str, vol: int):
    """Build a minimal bar duck-type the way alpaca-py does."""
    ts = datetime.strptime(ts_et_str, "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    return SimpleNamespace(symbol=symbol, timestamp=ts.astimezone(UTC), volume=vol)


def _run(coro):
    asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_on_bar_increments_counter() -> None:
    c = vp.WebsocketBarConsumer(["AAPL"], "k", "s")
    assert c._bars_received == 0
    _run(c._on_bar(_make_bar("AAPL", "2026-04-27 10:31", 1234)))
    assert c._bars_received == 1, c._bars_received
    assert c._last_bar_ts is not None
    assert c._last_handler_error is None


def test_first_5_bars_emit_sample_log() -> None:
    cap = _attach_capture()
    try:
        c = vp.WebsocketBarConsumer(["AAPL"], "k", "s")
        for i in range(7):
            _run(c._on_bar(_make_bar("AAPL", "2026-04-27 10:31", 100 + i)))
        sample_msgs = [m for m in cap.messages() if "sample bar #" in m]
        assert len(sample_msgs) == 5, sample_msgs
        # First sample should be #1
        assert "sample bar #1" in sample_msgs[0]
        assert "sample bar #5" in sample_msgs[-1]
    finally:
        _detach_capture(cap)


def test_100th_bar_triggers_heartbeat() -> None:
    cap = _attach_capture()
    try:
        c = vp.WebsocketBarConsumer(["AAPL"], "k", "s")
        for _ in range(100):
            _run(c._on_bar(_make_bar("AAPL", "2026-04-27 10:31", 1)))
        beats = [m for m in cap.messages() if "heartbeat" in m]
        assert any("total=100" in m for m in beats), beats
    finally:
        _detach_capture(cap)


def test_handler_exception_records_last_error(monkeypatch=None) -> None:
    c = vp.WebsocketBarConsumer(["AAPL"], "k", "s")
    real = vp.session_bucket

    def boom(_ts):
        raise RuntimeError("forced")

    vp.session_bucket = boom
    try:
        _run(c._on_bar(_make_bar("AAPL", "2026-04-27 10:31", 1)))
    finally:
        vp.session_bucket = real
    assert c._last_handler_error is not None, c._last_handler_error
    assert c._last_handler_error.startswith("RuntimeError"), c._last_handler_error
    # Counter must NOT increment when the handler hit the exception path.
    assert c._bars_received == 0, c._bars_received


def test_stats_snapshot_returns_expected_keys_and_is_threadsafe() -> None:
    c = vp.WebsocketBarConsumer(["AAPL", "QQQ"], "k", "s")
    _run(c._on_bar(_make_bar("AAPL", "2026-04-27 10:31", 1234)))
    _run(c._on_bar(_make_bar("QQQ", "2026-04-27 10:31", 4321)))

    # Hammer snapshot from many threads to make sure the lock is taken
    # (ThreadSanitizer style — if the lock weren't held this would race
    # against the dict-comprehension iteration of self._volumes).
    errs: list[BaseException] = []

    def _hit():
        for _ in range(50):
            try:
                snap = c.stats_snapshot()
                assert "bars_received" in snap
            except BaseException as e:  # noqa: BLE001
                errs.append(e)

    threads = [threading.Thread(target=_hit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs, errs

    snap = c.stats_snapshot()
    for k in (
        "bars_received", "last_bar_ts", "last_handler_error",
        "volumes_size_per_symbol", "tickers", "watchdog_reconnects",
        "silence_threshold_sec",
    ):
        assert k in snap, (k, snap)
    assert snap["bars_received"] == 2
    assert snap["volumes_size_per_symbol"]["AAPL"] == 1
    assert snap["volumes_size_per_symbol"]["QQQ"] == 1
    assert set(snap["tickers"]) == {"AAPL", "QQQ"}


def test_time_since_last_bar_seconds_none_when_no_bars() -> None:
    c = vp.WebsocketBarConsumer(["AAPL"], "k", "s")
    assert c.time_since_last_bar_seconds() is None
    _run(c._on_bar(_make_bar("AAPL", "2026-04-27 10:31", 1)))
    val = c.time_since_last_bar_seconds()
    assert val is not None and val >= 0.0, val


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_on_bar_increments_counter,
    test_first_5_bars_emit_sample_log,
    test_100th_bar_triggers_heartbeat,
    test_handler_exception_records_last_error,
    test_stats_snapshot_returns_expected_keys_and_is_threadsafe,
    test_time_since_last_bar_seconds_none_when_no_bars,
]


def main() -> int:
    fails = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  +  {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  X  {fn.__name__}: {type(e).__name__}: {e}")
    total = len(TESTS)
    print(f"\n  {total - fails} passed · {fails} failed · {total} total\n")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
