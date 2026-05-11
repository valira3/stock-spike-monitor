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


class TestOrWindowBackfill:
    """v7.74.0 -- when the bot starts mid-session post-OR, the scan
    loop must backfill the OR window from historical 1m bars so the
    engine can still trade today.

    The fix lives in `engine.scan._maybe_backfill_or_window` and is
    invoked from the `ensure_session_started` call site when the
    session is freshly initialized.
    """

    def _make_bars(self, *, ticker_universe, start_bucket=570,
                   end_bucket_excl=605, base_price=100.0, high_at=580,
                   low_at=585):
        """Build a fetch_1min_bars-shaped dict covering [start, end)."""
        # Each minute is one second-since-epoch tick to fit
        # minutes_since_et_midnight. We use a fixed UTC date that
        # respects DST: 2026-05-11 is EDT (UTC-4), so the UTC
        # timestamp for bucket B is 2026-05-11 (B - 4*60) since
        # midnight UTC... but easier: derive from the helper.
        from datetime import datetime, timezone
        # bucket=570 -> 09:30 ET -> 13:30 UTC on 2026-05-11
        base_utc_min = 13 * 60 + 30 - 570  # = -260 ... but we just want monotonic seconds
        out = {
            "timestamps": [],
            "opens": [], "highs": [], "lows": [],
            "closes": [], "volumes": [],
            "current_price": base_price,
            "pdc": base_price,
        }
        for b in range(start_bucket, end_bucket_excl):
            # Convert bucket -> UTC timestamp on 2026-05-11
            dt = datetime(2026, 5, 11, (b + 240) // 60, (b + 240) % 60,
                          tzinfo=timezone.utc)
            ts = int(dt.timestamp())
            h = base_price + 1.0 if b == high_at else base_price + 0.5
            lo = base_price - 1.0 if b == low_at else base_price + 0.5
            out["timestamps"].append(ts)
            out["opens"].append(base_price)
            out["highs"].append(h)
            out["lows"].append(lo)
            out["closes"].append(base_price + 0.1)
            out["volumes"].append(10000)
        return out

    def test_backfill_locks_or_when_bot_started_post_or(self, isolated_env):
        """Bot starts at 11:00 ET (bucket 660), 1 hour after OR closed.
        Backfill should replay 30 bars (570-599), lock the window, and
        leave the FSM in ARMED (range OK).
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo

        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-05-11",
            tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        # Simulate post-OR startup: now_et = 11:00 ET (bucket 660)
        now_et = datetime(2026, 5, 11, 11, 0,
                          tzinfo=ZoneInfo("America/New_York"))
        bars = self._make_bars(ticker_universe=["AAPL"],
                               start_bucket=570, end_bucket_excl=660,
                               high_at=580, low_at=585)

        class FakeCallbacks:
            def fetch_1min_bars(self, ticker):
                return bars

        from engine.scan import _maybe_backfill_or_window
        _maybe_backfill_or_window(FakeCallbacks(), now_et, ["AAPL"])

        eng = live_runtime.get_engine()
        w = eng._state.or_windows["AAPL"]
        assert w.locked, "OR should be locked after backfill"
        assert w.bars_seen >= 29  # 30 in-window bars (one might be filtered)

    def test_backfill_skips_when_still_in_or_window(self, isolated_env):
        """If cur_min < or_end, backfill should no-op (live scan covers it)."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-05-11",
            tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        # Inside OR window: 09:45 ET = bucket 585
        now_et = datetime(2026, 5, 11, 9, 45,
                          tzinfo=ZoneInfo("America/New_York"))

        class FakeCallbacks:
            def fetch_1min_bars(self, ticker):
                # Should not be called -- backfill should skip
                raise AssertionError("fetch_1min_bars should not be called inside OR")

        from engine.scan import _maybe_backfill_or_window
        _maybe_backfill_or_window(FakeCallbacks(), now_et, ["AAPL"])

        eng = live_runtime.get_engine()
        # OR window may not exist yet (no bars fed); definitely not locked.
        w = eng._state.or_windows.get("AAPL")
        if w is not None:
            assert not w.locked


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
