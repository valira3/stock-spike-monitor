"""Tests for v7.45.0 -- recent activity ring buffer in orb.live_runtime."""
from __future__ import annotations

import os

import pytest

from orb import live_runtime


@pytest.fixture(autouse=True)
def reset_runtime():
    live_runtime._reset_for_testing()
    live_runtime.clear_recent_activity()
    yield
    live_runtime._reset_for_testing()
    live_runtime.clear_recent_activity()


@pytest.fixture
def isolated_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("ORB_"):
            monkeypatch.delenv(k, raising=False)
    yield monkeypatch


class TestActivityRingBuffer:

    def test_empty_initially(self, isolated_env):
        assert live_runtime.get_recent_activity() == []

    def test_record_single_event(self, isolated_env):
        live_runtime._record_activity(kind="admit", ticker="AAPL",
                                      pid="main", detail="LONG · 742 sh @ 101.00")
        events = live_runtime.get_recent_activity()
        assert len(events) == 1
        e = events[0]
        assert e["kind"] == "admit"
        assert e["ticker"] == "AAPL"
        assert e["pid"] == "main"
        assert "LONG" in e["detail"]
        assert "ts_iso" in e

    def test_newest_first(self, isolated_env):
        for i in range(5):
            live_runtime._record_activity(kind="reject", ticker=f"T{i}",
                                          pid="main", detail=str(i))
        events = live_runtime.get_recent_activity()
        # Newest (T4) should be first
        assert events[0]["ticker"] == "T4"
        assert events[-1]["ticker"] == "T0"

    def test_ring_buffer_caps_at_50(self, isolated_env):
        for i in range(80):
            live_runtime._record_activity(kind="admit", ticker="X",
                                          pid="main", detail=str(i))
        # Ring buffer drops oldest; max length is 50
        events = live_runtime.get_recent_activity(limit=100)
        assert len(events) == 50
        # Newest in the buffer is 79; oldest is 30 (80-50)
        assert events[0]["detail"] == "79"
        assert events[-1]["detail"] == "30"

    def test_clear_recent_activity(self, isolated_env):
        live_runtime._record_activity(kind="admit", ticker="AAPL",
                                      pid="main", detail="x")
        assert len(live_runtime.get_recent_activity()) == 1
        live_runtime.clear_recent_activity()
        assert live_runtime.get_recent_activity() == []

    def test_get_recent_respects_limit(self, isolated_env):
        for i in range(20):
            live_runtime._record_activity(kind="admit", ticker="T",
                                          pid="main", detail=str(i))
        assert len(live_runtime.get_recent_activity(limit=5)) == 5
        assert len(live_runtime.get_recent_activity(limit=15)) == 15

    def test_session_start_records_event(self, isolated_env):
        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-05-11",
            tickers=["AAPL"],
            vix_close_d1=18.5,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100_000.0},
        )
        events = live_runtime.get_recent_activity()
        assert any(e["kind"] == "session_start" for e in events)
        ss = next(e for e in events if e["kind"] == "session_start")
        assert "2026-05-11" in ss["detail"]
        assert "18.5" in ss["detail"]

    def test_session_start_records_day_block(self, isolated_env):
        live_runtime.bootstrap()
        # VIX above 22 -> day block
        live_runtime.ensure_session_started(
            date_iso="2026-05-11",
            tickers=["AAPL"],
            vix_close_d1=30.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100_000.0},
        )
        events = live_runtime.get_recent_activity()
        kinds = [e["kind"] for e in events]
        assert "session_start" in kinds
        assert "day_block" in kinds

    def test_snapshot_includes_activity(self, isolated_env):
        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-05-11",
            tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100_000.0},
        )
        snap = live_runtime.snapshot()
        assert "activity" in snap
        assert isinstance(snap["activity"], list)
        assert len(snap["activity"]) >= 1
        assert snap["activity"][0]["kind"] == "session_start"
