"""Tests for orb.live_runtime -- the production wiring singleton."""
from __future__ import annotations

import os

import pytest

from orb import live_runtime


@pytest.fixture(autouse=True)
def reset_runtime_between_tests():
    live_runtime._reset_for_testing()
    yield
    live_runtime._reset_for_testing()


@pytest.fixture
def isolated_env(monkeypatch):
    """Clear ORB_* env vars so tests have a clean slate."""
    for k in list(os.environ):
        if k.startswith("ORB_"):
            monkeypatch.delenv(k, raising=False)
    yield monkeypatch


# ------------------ live mode flag ------------------


class TestLiveModeFlag:

    def test_default_on(self, isolated_env):
        assert live_runtime.is_live_mode_on() is True

    def test_explicit_zero_turns_off(self, isolated_env):
        isolated_env.setenv("ORB_LIVE_MODE", "0")
        assert live_runtime.is_live_mode_on() is False

    def test_explicit_one_turns_on(self, isolated_env):
        isolated_env.setenv("ORB_LIVE_MODE", "1")
        assert live_runtime.is_live_mode_on() is True

    def test_invalid_value_treated_as_off(self, isolated_env):
        # Any non-"1" value is treated as off (defensive)
        isolated_env.setenv("ORB_LIVE_MODE", "yes")
        assert live_runtime.is_live_mode_on() is False


# ------------------ bootstrap ------------------


class TestBootstrap:

    def test_bootstrap_creates_engine_and_adapters(self, isolated_env):
        live_runtime.bootstrap()
        assert live_runtime.get_engine() is not None
        # At least the "main" portfolio adapter should exist
        assert live_runtime.get_adapter("main") is not None

    def test_bootstrap_idempotent(self, isolated_env):
        live_runtime.bootstrap()
        engine_first = live_runtime.get_engine()
        live_runtime.bootstrap()  # second call
        engine_second = live_runtime.get_engine()
        assert engine_first is engine_second

    def test_bootstrap_force_rebuilds(self, isolated_env):
        live_runtime.bootstrap()
        engine_first = live_runtime.get_engine()
        live_runtime.bootstrap(force=True)
        engine_second = live_runtime.get_engine()
        assert engine_first is not engine_second

    def test_bootstrap_reads_env_config(self, isolated_env):
        isolated_env.setenv("ORB_RR", "3.0")
        isolated_env.setenv("ORB_OR_MINUTES", "15")
        isolated_env.setenv("ORB_SKIP_VIX_ABOVE", "30")
        live_runtime.bootstrap()
        eng = live_runtime.get_engine()
        assert eng.cfg.rr == 3.0
        assert eng.cfg.or_minutes == 15
        assert eng.cfg.skip_vix_above == 30.0

    def test_bootstrap_reads_blocklist_json(self, isolated_env):
        isolated_env.setenv(
            "ORB_TICKER_SIDE_BLOCKLIST",
            '{"META":["LONG","SHORT"],"MSFT":["LONG"]}',
        )
        live_runtime.bootstrap()
        eng = live_runtime.get_engine()
        bl = eng.cfg.ticker_side_blocklist
        assert bl == {"META": ["LONG", "SHORT"], "MSFT": ["LONG"]}

    def test_bootstrap_handles_invalid_blocklist(self, isolated_env):
        isolated_env.setenv("ORB_TICKER_SIDE_BLOCKLIST", "{not valid json")
        live_runtime.bootstrap()
        eng = live_runtime.get_engine()
        # Falls back to None (no blocklist)
        assert eng.cfg.ticker_side_blocklist is None

    def test_bootstrap_compounding_default_on(self, isolated_env):
        """Manager-flagged regression test: rule #11b says compounding
        is the DEFAULT. The live_runtime bootstrap must not silently
        drop this. We verify by checking that risk-per-trade-pct (which
        is the compounding-driven sizing percentage) stays at 2.0 (v10
        keystone) so per-trade $ scales with current account balance.

        The actual COMPOUND_DAILY toggle lives in tools/orb_backtest.py
        config; the live engine compounds implicitly via per-day
        equity refresh in ensure_session_started (each session start
        receives the current equity from the broker). This test
        asserts that path is taken: equity_per_portfolio is the
        authoritative sizing base and the engine uses it.
        """
        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-01-02",
            tickers=["AAPL"], vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 105000.0},  # NOT $100k baseline
        )
        eng = live_runtime.get_engine()
        rb = eng._risk.get("main")
        # Equity refreshed -> compounding effective. Risk cap stays the
        # configured ceiling but per-trade sizing percent applies to the
        # current balance ($105k), not the static $100k.
        assert rb.equity == 105000.0
        # Cfg risk_per_trade_pct is preserved (2% of current equity)
        assert eng.cfg.risk_per_trade_pct == 2.0
        # max_concurrent_risk_dollars is the absolute cap ($2k), not %
        assert rb.max_risk_dollars == 2000.0


# ------------------ session lifecycle ------------------


class TestSessionLifecycle:

    def _bootstrap_helper(self, isolated_env):
        live_runtime.bootstrap()

    def test_ensure_session_started_first_call(self, isolated_env):
        self._bootstrap_helper(isolated_env)
        ok = live_runtime.ensure_session_started(
            date_iso="2026-01-02",
            tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        assert ok

    def test_ensure_session_idempotent_same_date(self, isolated_env):
        self._bootstrap_helper(isolated_env)
        live_runtime.ensure_session_started(
            date_iso="2026-01-02",
            tickers=["AAPL"], vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        # Second call with same date -> no-op (returns False)
        ok = live_runtime.ensure_session_started(
            date_iso="2026-01-02",
            tickers=["AAPL"], vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        assert ok is False

    def test_session_advances_on_new_date(self, isolated_env):
        self._bootstrap_helper(isolated_env)
        live_runtime.ensure_session_started(
            date_iso="2026-01-02",
            tickers=["AAPL"], vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        ok = live_runtime.ensure_session_started(
            date_iso="2026-01-03",  # next day
            tickers=["AAPL"], vix_close_d1=17.5,
            ticker_open_today={"AAPL": 101.0},
            ticker_prev_close={"AAPL": 100.5},
            equity_per_portfolio={"main": 100000.0},
        )
        assert ok

    def test_ensure_session_pre_bootstrap_returns_false(self, isolated_env):
        # Did NOT call bootstrap()
        ok = live_runtime.ensure_session_started(
            date_iso="2026-01-02",
            tickers=["AAPL"], vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        assert ok is False


# ------------------ feed_bar / check_entry / check_exit ------------------


class TestPerTickAPI:

    def _setup(self, isolated_env):
        # Open OR + provide locked window for AAPL
        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-01-02",
            tickers=["AAPL"], vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        # Feed all OR bars
        for m in range(570, 600):
            h = 101.0 if m == 580 else 100.5
            l = 99.0 if m == 585 else 100.0
            live_runtime.feed_bar(
                ticker="AAPL",
                bar_high=h, bar_low=l,
                bar_open=100.0, bar_close=100.0,
                bar_volume=10000, bar_bucket_min=m,
            )

    def test_feed_bar_no_op_when_off(self, isolated_env):
        isolated_env.setenv("ORB_LIVE_MODE", "0")
        live_runtime.bootstrap()
        live_runtime.feed_bar(
            ticker="AAPL", bar_high=101.0, bar_low=100.0,
            bar_open=100.0, bar_close=100.5,
            bar_volume=10000, bar_bucket_min=570,
        )
        # Engine should NOT have an OR window (live mode off short-circuits)
        eng = live_runtime.get_engine()
        assert "AAPL" not in eng._state.or_windows

    def test_check_entry_long(self, isolated_env):
        self._setup(isolated_env)
        result = live_runtime.check_entry(
            portfolio_id="main", ticker="AAPL", side="long",
            five_min_close=101.5, next_open=101.5, equity=100000.0,
        )
        assert result.ok
        assert result.side == "long"
        assert result.shares > 0

    def test_check_entry_no_signal(self, isolated_env):
        self._setup(isolated_env)
        result = live_runtime.check_entry(
            portfolio_id="main", ticker="AAPL", side="long",
            five_min_close=100.5, next_open=100.5, equity=100000.0,
        )
        assert not result.ok
        assert result.reason_no == "no_signal"

    def test_check_entry_unknown_portfolio(self, isolated_env):
        self._setup(isolated_env)
        result = live_runtime.check_entry(
            portfolio_id="not_a_portfolio", ticker="AAPL", side="long",
            five_min_close=101.5, next_open=101.5, equity=100000.0,
        )
        assert not result.ok
        assert "no_adapter" in result.reason_no

    def test_check_entry_when_live_mode_off(self, isolated_env):
        isolated_env.setenv("ORB_LIVE_MODE", "0")
        live_runtime.bootstrap()
        result = live_runtime.check_entry(
            portfolio_id="main", ticker="AAPL", side="long",
            five_min_close=101.5, next_open=101.5, equity=100000.0,
        )
        assert not result.ok
        assert result.reason_no == "live_mode_off"

    def test_check_exit_target(self, isolated_env):
        self._setup(isolated_env)
        result = live_runtime.check_entry(
            portfolio_id="main", ticker="AAPL", side="long",
            five_min_close=101.5, next_open=101.5, equity=100000.0,
        )
        ex = live_runtime.check_exit(
            portfolio_id="main", ticker="AAPL", ticket_id=result.ticket_id,
            bar_high=110.0, bar_low=104.0, bar_close=108.0,
            bar_bucket_min=605,
        )
        assert ex.exit
        assert ex.reason == "target"


# ------------------ snapshot ------------------


class TestSnapshot:

    def test_snapshot_pre_bootstrap(self, isolated_env):
        snap = live_runtime.snapshot()
        assert snap["bootstrapped"] is False
        assert "live_mode" in snap

    def test_snapshot_post_bootstrap(self, isolated_env):
        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-01-02",
            tickers=["AAPL"], vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        snap = live_runtime.snapshot()
        assert snap["bootstrapped"] is True
        assert snap["live_mode"] is True
        assert snap["session_date"] == "2026-01-02"
        assert "config" in snap
        assert "day_status" in snap
        assert "or_windows" in snap
        assert "risk_books" in snap


# ------------------ reset ------------------


class TestReset:

    def test_reset_session_clears_date(self, isolated_env):
        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-01-02",
            tickers=["AAPL"], vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        live_runtime.reset_session()
        # Now session_date is empty; ensure_session_started should
        # work for the same date again.
        ok = live_runtime.ensure_session_started(
            date_iso="2026-01-02",
            tickers=["AAPL"], vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        assert ok
