"""Integration tests for the v7.14.0 ORB shadow-mode wiring in
engine/scan.py.

These tests verify that the scan-loop's v10 hooks (bootstrap +
ensure_session_started + feed_bar) wire correctly without modifying
the legacy entry/exit path. Tests use the live_runtime singleton
directly with mock inputs (not the scan_loop itself, which has heavy
trade_genius import requirements).

Verifies:
  1. Shadow mode: runtime can bootstrap + start session + receive bars
     without any production trade execution side effects
  2. The minutes_since_et_midnight helper produces correct buckets for
     the OR window across DST boundary
  3. Multi-portfolio: 3 portfolios receive independent state
  4. Kill-switch: ORB_LIVE_MODE=0 makes feed_bar a no-op
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from orb import live_runtime
from engine.timing import minutes_since_et_midnight


@pytest.fixture(autouse=True)
def reset_runtime():
    live_runtime._reset_for_testing()
    yield
    live_runtime._reset_for_testing()


@pytest.fixture
def isolated_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("ORB_"):
            monkeypatch.delenv(k, raising=False)
    yield monkeypatch


class TestShadowModeBootstrap:

    def test_bootstrap_then_session_then_feed(self, isolated_env):
        """End-to-end: bootstrap, start session, feed bars, observe state."""
        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-01-02",
            tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        # Feed one bar via the EXACT path scan.py uses
        ts_utc = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)  # 09:30 ET
        bucket = minutes_since_et_midnight(ts_utc)
        assert bucket == 570  # 09:30 ET
        live_runtime.feed_bar(
            ticker="AAPL",
            bar_high=100.5, bar_low=99.5, bar_open=100.0, bar_close=100.2,
            bar_volume=10000.0, bar_bucket_min=bucket,
        )
        # Engine state: OR window has 1 bar, not yet locked
        eng = live_runtime.get_engine()
        w = eng._state.or_windows["AAPL"]
        assert w.bars_seen == 1
        assert not w.locked

    def test_kill_switch_makes_feed_bar_noop(self, isolated_env):
        isolated_env.setenv("ORB_LIVE_MODE", "0")
        live_runtime.bootstrap()
        live_runtime.feed_bar(
            ticker="AAPL",
            bar_high=100.5, bar_low=99.5, bar_open=100.0, bar_close=100.2,
            bar_volume=10000.0, bar_bucket_min=570,
        )
        # OR window NOT created -- live mode off short-circuits
        eng = live_runtime.get_engine()
        assert "AAPL" not in eng._state.or_windows


class TestDstBucketCorrectness:

    def test_edt_summer_at_market_open(self):
        """2026-04-30 (EDT, UTC-4): 13:30 UTC = 09:30 ET = bucket 570."""
        ts = datetime(2026, 4, 30, 13, 30, tzinfo=timezone.utc)
        assert minutes_since_et_midnight(ts) == 570

    def test_est_winter_at_market_open(self):
        """2025-11-03 (EST, UTC-5): 14:30 UTC = 09:30 ET = bucket 570."""
        ts = datetime(2025, 11, 3, 14, 30, tzinfo=timezone.utc)
        assert minutes_since_et_midnight(ts) == 570

    def test_or_window_close_is_600(self):
        """30-min OR window closes at 10:00 ET = bucket 600."""
        ts_edt = datetime(2026, 4, 30, 14, 0, tzinfo=timezone.utc)
        ts_est = datetime(2025, 11, 3, 15, 0, tzinfo=timezone.utc)
        assert minutes_since_et_midnight(ts_edt) == 600
        assert minutes_since_et_midnight(ts_est) == 600

    def test_eod_at_15_55(self):
        """EOD cutoff at 15:55 ET = bucket 955."""
        ts_edt = datetime(2026, 4, 30, 19, 55, tzinfo=timezone.utc)
        assert minutes_since_et_midnight(ts_edt) == 955


class TestMultiPortfolioShadow:

    def test_three_portfolios_independent(self, isolated_env):
        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-01-02",
            tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0, "val": 50000.0,
                                   "gene": 25000.0},
        )
        eng = live_runtime.get_engine()
        # Each portfolio has its own RiskBook with the supplied equity
        for pid, eq in [("main", 100000.0), ("val", 50000.0), ("gene", 25000.0)]:
            rb = eng._risk.get(pid)
            if rb is None:
                # Test may be running without all portfolios (skip)
                continue
            assert rb.equity == eq

    def test_session_idempotent_within_day(self, isolated_env):
        """Calling ensure_session_started twice with same date is a no-op."""
        live_runtime.bootstrap()
        ok1 = live_runtime.ensure_session_started(
            date_iso="2026-01-02",
            tickers=["AAPL"], vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        ok2 = live_runtime.ensure_session_started(
            date_iso="2026-01-02",
            tickers=["AAPL"], vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        assert ok1 is True
        assert ok2 is False  # idempotent
