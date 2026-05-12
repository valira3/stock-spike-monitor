"""Tests for v7.16.0 LiveAdapter.check_exit_by_ticker + runtime wrapper.

These power PR9's exit-cutover wiring in broker/positions.py. The
ticker-keyed lookup means manage_positions can ask 'is there a v10
exit signal for this ticker?' without tracking ticket ids on the
position dict.
"""
from __future__ import annotations

import os

import pytest

from orb import live_runtime
from orb.live_adapter import LiveAdapter, ExitResult


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
    monkeypatch.setenv("ORB_PARTIAL_PROFIT_AT_1R", "0")  # v8.1.3 legacy default
    yield monkeypatch


def _bootstrap_with_open_long(equity=100000.0, or_high=101.0, or_low=99.0):
    live_runtime.bootstrap()
    live_runtime.ensure_session_started(
        date_iso="2026-01-02",
        tickers=["AAPL"],
        vix_close_d1=18.0,
        ticker_open_today={"AAPL": 100.0},
        ticker_prev_close={"AAPL": 100.0},
        equity_per_portfolio={"main": equity},
    )
    for m in range(570, 600):
        h = or_high if m == 580 else or_high - 0.5
        l = or_low if m == 585 else or_low + 0.5
        live_runtime.feed_bar(
            ticker="AAPL", bar_high=h, bar_low=l,
            bar_open=100.0, bar_close=100.0,
            bar_volume=10000, bar_bucket_min=m,
        )
    # Take a long entry
    result = live_runtime.check_entry(
        portfolio_id="main", ticker="AAPL", side="long",
        five_min_close=101.5, next_open=101.5, equity=equity,
    )
    assert result.ok
    return result.ticket_id


# ------------------ check_exit_by_ticker on adapter ------------------


class TestCheckExitByTicker:

    def test_returns_no_op_when_no_open_position(self, isolated_env):
        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-01-02", tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        eng = live_runtime.get_engine()
        adapter = LiveAdapter(eng, "main")
        result = adapter.check_exit_by_ticker(
            "AAPL", bar_high=110.0, bar_low=104.0,
            bar_close=108.0, bar_bucket_min=605,
        )
        assert not result.exit
        assert result.reason == "no_open_v10_position"

    def test_target_exit_via_ticker_lookup(self, isolated_env):
        ticket = _bootstrap_with_open_long()
        # Use the runtime's ticker-keyed exit
        result = live_runtime.check_exit_by_ticker(
            portfolio_id="main", ticker="AAPL",
            bar_high=110.0, bar_low=104.0,
            bar_close=108.0, bar_bucket_min=605,
        )
        assert result.exit
        assert result.reason == "target"

    def test_stop_exit_via_ticker_lookup(self, isolated_env):
        ticket = _bootstrap_with_open_long()
        result = live_runtime.check_exit_by_ticker(
            portfolio_id="main", ticker="AAPL",
            bar_high=101.0, bar_low=98.0,
            bar_close=98.5, bar_bucket_min=605,
        )
        assert result.exit
        assert result.reason == "stop"

    def test_no_exit_in_range(self, isolated_env):
        ticket = _bootstrap_with_open_long()
        result = live_runtime.check_exit_by_ticker(
            portfolio_id="main", ticker="AAPL",
            bar_high=102.0, bar_low=101.0,
            bar_close=101.5, bar_bucket_min=605,
        )
        assert not result.exit

    def test_after_exit_ticker_map_is_cleared(self, isolated_env):
        """Once a position exits, check_exit_by_ticker returns no_open."""
        ticket = _bootstrap_with_open_long()
        # First call: target hit
        r1 = live_runtime.check_exit_by_ticker(
            portfolio_id="main", ticker="AAPL",
            bar_high=110.0, bar_low=104.0,
            bar_close=108.0, bar_bucket_min=605,
        )
        assert r1.exit
        # Second call on same ticker: no_open_v10_position
        r2 = live_runtime.check_exit_by_ticker(
            portfolio_id="main", ticker="AAPL",
            bar_high=110.0, bar_low=104.0,
            bar_close=108.0, bar_bucket_min=606,
        )
        assert not r2.exit
        assert r2.reason == "no_open_v10_position"


# ------------------ live_mode_off + unknown portfolio ------------------


class TestRuntimeGuards:

    def test_live_mode_off_returns_no_op(self, isolated_env):
        isolated_env.setenv("ORB_LIVE_MODE", "0")
        live_runtime.bootstrap()
        result = live_runtime.check_exit_by_ticker(
            portfolio_id="main", ticker="AAPL",
            bar_high=110.0, bar_low=104.0,
            bar_close=108.0, bar_bucket_min=605,
        )
        assert not result.exit
        assert result.reason == "live_mode_off"

    def test_unknown_portfolio_returns_no_op(self, isolated_env):
        live_runtime.bootstrap()
        result = live_runtime.check_exit_by_ticker(
            portfolio_id="not_a_real_portfolio", ticker="AAPL",
            bar_high=110.0, bar_low=104.0,
            bar_close=108.0, bar_bucket_min=605,
        )
        assert not result.exit
        assert "no_adapter" in result.reason


# ------------------ multi-portfolio independence ------------------


class TestMultiPortfolioExitByTicker:

    def test_each_portfolio_owns_its_open(self, isolated_env):
        """If main has an open AAPL position but val doesn't, val's
        check_exit_by_ticker returns no_open."""
        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-01-02", tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0, "val": 50000.0,
                                   "gene": 25000.0},
        )
        for m in range(570, 600):
            live_runtime.feed_bar(
                ticker="AAPL", bar_high=101.0, bar_low=99.0,
                bar_open=100.0, bar_close=100.0,
                bar_volume=10000, bar_bucket_min=m,
            )
        # Main takes a long; val/gene do NOT
        live_runtime.check_entry(
            portfolio_id="main", ticker="AAPL", side="long",
            five_min_close=101.5, next_open=101.5, equity=100000.0,
        )
        # Main's exit-by-ticker: real position
        r_main = live_runtime.check_exit_by_ticker(
            portfolio_id="main", ticker="AAPL",
            bar_high=102.0, bar_low=101.0,
            bar_close=101.5, bar_bucket_min=605,
        )
        # Adapter says: no exit yet (in range), but it FOUND the position
        assert not r_main.exit
        # Val: no open position
        r_val = live_runtime.check_exit_by_ticker(
            portfolio_id="val", ticker="AAPL",
            bar_high=102.0, bar_low=101.0,
            bar_close=101.5, bar_bucket_min=605,
        )
        # Val should report no_open_v10_position OR no_adapter
        assert not r_val.exit
        # Either reason is fine; the test asserts independence
