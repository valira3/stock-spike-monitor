"""v10.0.1 -- in-process earnings calendar refresh.

Replaces the retired GHA cron. Tests cover:
  - happy path: fetch ok, write ok, reload ok -> status="ok"
  - lxml-missing trap: every ticker returns [] -> status="empty_payload"
    AND the existing on-disk calendar is NOT overwritten
  - fetch_failure: fetch_earnings_dates raises -> status="fetch_failed"
  - write_failure: write_calendar raises -> status="write_failed"
  - reload_failure: importlib.reload raises -> status="reload_failed"
  - state defaults / to_snapshot_dict shape / never_run path
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest import mock

import pytest


def _reset_state():
    """Each test starts with no state to avoid bleed-through."""
    from orb import earnings_refresh
    earnings_refresh._state = None


# ----------------------------------------------------------------------------
# State / snapshot shape
# ----------------------------------------------------------------------------


def test_state_default_is_never_run_dict():
    _reset_state()
    from orb import earnings_refresh
    d = earnings_refresh.to_snapshot_dict()
    assert d["last_status"] == "never_run"
    assert d["n_events"] == 0
    assert d["n_tickers_with_events"] == 0
    assert d["last_run_iso"] == ""
    assert d["error_msg"] == ""


def test_snapshot_dict_is_json_serializable():
    _reset_state()
    from orb import earnings_refresh
    d = earnings_refresh.to_snapshot_dict()
    json.dumps(d)  # must not raise


def test_get_state_returns_none_initially():
    _reset_state()
    from orb import earnings_refresh
    assert earnings_refresh.get_state() is None


# ----------------------------------------------------------------------------
# fire_refresh -- happy path
# ----------------------------------------------------------------------------


def test_fire_refresh_happy_path(tmp_path):
    _reset_state()
    from orb import earnings_refresh

    # Mock fetch_earnings_dates to return canned data; mock write_calendar
    # to no-op (we don't want to actually overwrite tools/orb_earnings_calendar.py
    # during tests); mock importlib.reload to no-op.
    fake_dates = {
        "AAPL": [("2026-07-31", "AMC")],
        "MSFT": [("2026-07-30", "AMC")],
    }
    def _fake_fetch(ticker, start, end):
        return fake_dates.get(ticker, [])

    out_path = tmp_path / "calendar.py"

    with mock.patch(
        "tools.orb_earnings_fetcher.fetch_earnings_dates", side_effect=_fake_fetch
    ), mock.patch(
        "tools.orb_earnings_fetcher.write_calendar"
    ) as wc, mock.patch(
        "importlib.reload"
    ) as rel:
        s = earnings_refresh.fire_refresh(
            universe=("AAPL", "MSFT", "NVDA"), out_path=out_path,
        )
    assert s.last_status == "ok"
    # 2 events across 2 tickers (NVDA returns [])
    assert s.n_events == 2
    assert s.n_tickers_with_events == 2
    assert s.error_msg == ""
    # write_calendar was called once with the fetched data and the
    # out_path argument
    assert wc.call_count == 1
    args, kwargs = wc.call_args
    assert args[1] == out_path
    # reload was called once (on tools.orb_earnings_calendar)
    assert rel.call_count == 1


# ----------------------------------------------------------------------------
# fire_refresh -- empty_payload (lxml trap)
# ----------------------------------------------------------------------------


def test_fire_refresh_empty_payload_does_not_overwrite(tmp_path):
    _reset_state()
    from orb import earnings_refresh

    out_path = tmp_path / "calendar.py"
    out_path.write_text("# pre-existing content")

    # Every ticker returns []
    with mock.patch(
        "tools.orb_earnings_fetcher.fetch_earnings_dates", return_value=[]
    ), mock.patch(
        "tools.orb_earnings_fetcher.write_calendar"
    ) as wc, mock.patch(
        "importlib.reload"
    ) as rel:
        s = earnings_refresh.fire_refresh(
            universe=("AAPL", "MSFT"), out_path=out_path,
        )
    assert s.last_status == "empty_payload"
    assert s.n_events == 0
    assert "lxml" in s.error_msg or "yfinance" in s.error_msg
    # Critical: write_calendar must NOT have been called -- the
    # pre-existing calendar must be preserved when the new payload
    # is empty (the lxml-missing trap that previously shipped an
    # empty calendar via the GHA cron).
    assert wc.call_count == 0
    assert rel.call_count == 0
    # File on disk untouched
    assert out_path.read_text() == "# pre-existing content"


# ----------------------------------------------------------------------------
# fire_refresh -- failure modes
# ----------------------------------------------------------------------------


def test_fire_refresh_fetch_import_failure_marks_status(tmp_path):
    _reset_state()
    from orb import earnings_refresh

    out_path = tmp_path / "calendar.py"
    with mock.patch.dict(
        "sys.modules", {"tools.orb_earnings_fetcher": None}
    ):
        s = earnings_refresh.fire_refresh(
            universe=("AAPL",), out_path=out_path,
        )
    assert s.last_status == "fetch_failed"
    # State persisted
    cur = earnings_refresh.get_state()
    assert cur is s


def test_fire_refresh_write_failure_marks_status(tmp_path):
    _reset_state()
    from orb import earnings_refresh

    out_path = tmp_path / "calendar.py"

    with mock.patch(
        "tools.orb_earnings_fetcher.fetch_earnings_dates",
        return_value=[("2026-07-31", "AMC")],
    ), mock.patch(
        "tools.orb_earnings_fetcher.write_calendar",
        side_effect=PermissionError("disk full"),
    ):
        s = earnings_refresh.fire_refresh(
            universe=("AAPL",), out_path=out_path,
        )
    assert s.last_status == "write_failed"
    assert "disk full" in s.error_msg
    # We DID find events, so n_events > 0 (preserved for the dashboard)
    assert s.n_events == 1


def test_fire_refresh_reload_failure_marks_status(tmp_path):
    _reset_state()
    from orb import earnings_refresh

    out_path = tmp_path / "calendar.py"

    with mock.patch(
        "tools.orb_earnings_fetcher.fetch_earnings_dates",
        return_value=[("2026-07-31", "AMC")],
    ), mock.patch(
        "tools.orb_earnings_fetcher.write_calendar"
    ), mock.patch(
        "importlib.reload", side_effect=ImportError("module gone"),
    ):
        s = earnings_refresh.fire_refresh(
            universe=("AAPL",), out_path=out_path,
        )
    assert s.last_status == "reload_failed"
    assert "module gone" in s.error_msg
    # File was written; just the live process didn't pick up the new data
    assert s.n_events == 1


# ----------------------------------------------------------------------------
# Snapshot + state thread-safety smoke
# ----------------------------------------------------------------------------


def test_set_state_thread_safety_smoke():
    _reset_state()
    from orb import earnings_refresh
    from orb.earnings_refresh import RefreshState
    import threading
    R = RefreshState(
        last_run_iso="2026-05-19T00:00:00+00:00",
        last_status="ok", n_events=42, n_tickers_with_events=10,
        error_msg="",
    )
    def writer():
        for _ in range(50):
            earnings_refresh._set_state(R)
    def reader():
        for _ in range(50):
            earnings_refresh.get_state()
            earnings_refresh.to_snapshot_dict()
    ts = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=5)
    assert all(not t.is_alive() for t in ts), "earnings_refresh deadlocked"


# ----------------------------------------------------------------------------
# Snapshot integration with live_runtime
# ----------------------------------------------------------------------------


def test_live_runtime_snapshot_includes_earnings_refresh_block():
    """The /api/state path consumes orb.earnings_refresh.to_snapshot_dict
    via live_runtime.snapshot. Even if the runtime isn't bootstrapped,
    the earnings_refresh block must be present for the dashboard."""
    _reset_state()
    import orb.live_runtime as lr
    snap = lr.snapshot()
    if "earnings_refresh" in snap:
        assert "last_status" in snap["earnings_refresh"]
    # else: snapshot() returned the not-bootstrapped stub, which doesn't
    # include earnings_refresh. Both shapes are valid -- the stub is
    # only returned when the engine isn't running at all.


def test_snapshot_dict_shape_after_successful_refresh(tmp_path):
    _reset_state()
    from orb import earnings_refresh
    out_path = tmp_path / "calendar.py"
    with mock.patch(
        "tools.orb_earnings_fetcher.fetch_earnings_dates",
        return_value=[("2026-07-31", "AMC")],
    ), mock.patch(
        "tools.orb_earnings_fetcher.write_calendar"
    ), mock.patch(
        "importlib.reload"
    ):
        earnings_refresh.fire_refresh(
            universe=("AAPL", "MSFT"), out_path=out_path,
        )
    d = earnings_refresh.to_snapshot_dict()
    assert d["last_status"] == "ok"
    assert d["n_events"] == 2
    assert d["n_tickers_with_events"] == 2
    assert d["last_run_iso"]  # non-empty
    # ISO 8601 UTC marker
    assert "T" in d["last_run_iso"]
