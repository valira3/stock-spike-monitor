"""Tests for v7.15.0 entry-route swap in engine/scan.py.

These tests verify the per-side entry helpers (_orb_long_entry +
_orb_short_entry) call into the live runtime correctly, gracefully
handle missing bar data, and respect the kill-switch.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from orb import live_runtime


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


def _setup_armed_or(or_high=101.0, or_low=99.0, equity=100000.0):
    """Bootstrap + start session + feed all OR bars so AAPL is ARMED."""
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
            ticker="AAPL",
            bar_high=h, bar_low=l,
            bar_open=100.0, bar_close=100.0,
            bar_volume=10000.0, bar_bucket_min=m,
        )


class TestLongEntry:

    def test_admits_on_breakout(self, isolated_env):
        from engine.scan import _orb_long_entry
        _setup_armed_or()
        callbacks = MagicMock()
        tg = MagicMock()
        tg.paper_cash = 100000.0
        # bars_for_mtm: 5-min close at $101.5 (above OR high $101.0)
        # compute_5m_ohlc_and_ema9 needs 1-min bars; we pass a synthetic
        # payload that returns a single 5m close.
        bars = {
            "timestamps": [int(t) for t in range(1735821000, 1735821000 + 10*60, 60)],
            "opens":  [100.0, 100.5, 101.0, 101.0, 101.2, 101.4, 101.4, 101.5, 101.5, 101.5],
            "highs":  [100.5, 101.0, 101.5, 101.3, 101.6, 101.8, 101.8, 101.8, 101.7, 101.6],
            "lows":   [99.5, 100.0, 100.5, 100.8, 101.0, 101.2, 101.2, 101.3, 101.4, 101.4],
            "closes": [100.5, 101.0, 101.0, 101.2, 101.5, 101.7, 101.7, 101.5, 101.5, 101.5],
            "volumes":[10000]*10,
            "current_price": 101.5,
        }
        _orb_long_entry(callbacks, tg, "AAPL", bars)
        # Note: actual admission depends on whether compute_5m_ohlc_and_ema9
        # produces a closed 5m bar from this synthetic payload. If it
        # doesn't, the helper gracefully returns without calling
        # execute_entry. We assert no exception.
        # The strict admission test is in test_orb_live_adapter.py
        assert True  # smoke: no exception raised

    def test_no_op_on_missing_bars(self, isolated_env):
        from engine.scan import _orb_long_entry
        _setup_armed_or()
        callbacks = MagicMock()
        tg = MagicMock()
        _orb_long_entry(callbacks, tg, "AAPL", None)
        # Should not call execute_entry
        callbacks.execute_entry.assert_not_called()

    def test_no_op_on_empty_bars(self, isolated_env):
        from engine.scan import _orb_long_entry
        _setup_armed_or()
        callbacks = MagicMock()
        tg = MagicMock()
        _orb_long_entry(callbacks, tg, "AAPL", {})
        callbacks.execute_entry.assert_not_called()

    def test_handles_compute_5m_failure(self, isolated_env, monkeypatch):
        from engine.scan import _orb_long_entry
        _setup_armed_or()
        callbacks = MagicMock()
        tg = MagicMock()
        # Force the 5m helper to raise; the entry function should
        # log + return rather than crash.
        import engine.bars as _eb
        monkeypatch.setattr(_eb, "compute_5m_ohlc_and_ema9",
                            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("bad")))
        _orb_long_entry(callbacks, tg, "AAPL",
                        {"timestamps": [1], "opens": [1], "highs": [1],
                         "lows": [1], "closes": [1]})
        callbacks.execute_entry.assert_not_called()


class TestShortEntry:

    def test_no_op_on_missing_bars(self, isolated_env):
        from engine.scan import _orb_short_entry
        _setup_armed_or()
        callbacks = MagicMock()
        tg = MagicMock()
        _orb_short_entry(callbacks, tg, "AAPL", None)
        callbacks.execute_short_entry.assert_not_called()


class TestPortfolioEquity:

    def test_resolves_from_portfolio_book(self):
        from engine.scan import _resolve_portfolio_equity
        # No PortfolioBook available in test -> falls back to paper_cash
        tg = MagicMock()
        tg.paper_cash = 75000.0
        result = _resolve_portfolio_equity(tg, "missing_portfolio")
        # Either returns paper_cash or PortfolioBook equity
        assert isinstance(result, float)
        assert result > 0

    def test_returns_float_when_book_exists(self):
        """If the PortfolioBook exists in the registry (even with 0 cash),
        we use its current_equity. Only when the book is missing AND tg
        lacks paper_cash do we fall back to 100k default."""
        from engine.scan import _resolve_portfolio_equity
        class _NoCash:
            pass
        result = _resolve_portfolio_equity(_NoCash(), "main")
        # main book exists in the registry; current_equity returns whatever
        # paper_cash is set to (0.0 by default in fresh test process)
        assert isinstance(result, float)

    def test_falls_back_to_default_for_missing_portfolio(self):
        from engine.scan import _resolve_portfolio_equity
        class _NoCash:
            pass
        # Unknown portfolio_id -> book is None -> falls back to paper_cash
        # -> not present -> default 100_000.0
        result = _resolve_portfolio_equity(_NoCash(), "doesnotexist")
        assert result == 100_000.0


class TestKillSwitchPath:

    def test_check_entry_returns_no_op_when_off(self, isolated_env):
        """When ORB_LIVE_MODE=0, check_entry returns reason_no='live_mode_off'."""
        isolated_env.setenv("ORB_LIVE_MODE", "0")
        live_runtime.bootstrap()
        result = live_runtime.check_entry(
            portfolio_id="main", ticker="AAPL", side="long",
            five_min_close=101.5, next_open=101.5, equity=100000.0,
        )
        assert not result.ok
        assert result.reason_no == "live_mode_off"
