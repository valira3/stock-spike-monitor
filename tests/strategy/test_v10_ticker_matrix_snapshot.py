"""Tests for v7.27.0 -- v10 Ticker Matrix backend snapshot contract.

The dashboard's renderV10TickerMatrix(s) reads s.v10.day_states and
s.v10.or_windows. Pin the shape so a future refactor of orb.engine or
orb.state can't silently break the frontend.

The frontend is intentionally not unit-tested here; the index.html +
app.js wire-up is exercised manually + via tests/test_dashboard_v10_block.
"""
from __future__ import annotations

import os

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


def _bootstrap_session(isolated_env, *, tickers, vix=18.0):
    live_runtime.bootstrap()
    opens = {tk: 100.0 for tk in tickers}
    pdc = {tk: 100.0 for tk in tickers}
    live_runtime.ensure_session_started(
        date_iso="2026-05-10", tickers=list(tickers),
        vix_close_d1=vix,
        ticker_open_today=opens, ticker_prev_close=pdc,
        equity_per_portfolio={"main": 100_000.0},
    )


class TestSnapshotShape:

    def test_day_states_present(self, isolated_env):
        _bootstrap_session(isolated_env, tickers=["AAPL", "MSFT"])
        snap = live_runtime.snapshot()
        assert "day_states" in snap
        day_states = snap["day_states"]
        assert isinstance(day_states, list)
        assert len(day_states) >= 2
        for ds in day_states:
            assert "ticker" in ds
            assert "phase" in ds
            assert "trades_today" in ds
            assert "in_position" in ds
            assert "block_reason" in ds
            assert "portfolio_id" in ds

    def test_or_windows_present(self, isolated_env):
        _bootstrap_session(isolated_env, tickers=["AAPL"])
        live_runtime.feed_bar(
            ticker="AAPL",
            bar_high=100.5, bar_low=99.5, bar_open=100.0,
            bar_close=100.2, bar_volume=10000,
            bar_bucket_min=570,
        )
        snap = live_runtime.snapshot()
        assert "or_windows" in snap
        ow = snap["or_windows"]
        assert "AAPL" in ow
        w = ow["AAPL"]
        # Fields read by renderV10TickerMatrix
        assert "or_high" in w
        assert "or_low" in w
        assert "or_width_pct" in w
        assert "locked" in w

    def test_config_max_trades_per_day_present(self, isolated_env):
        _bootstrap_session(isolated_env, tickers=["AAPL"])
        snap = live_runtime.snapshot()
        assert "config" in snap
        assert "max_trades_per_day" in snap["config"]

    def test_config_v8_atr_and_partial_fields_present(self, isolated_env):
        """v8.1.2 -- frontend reads these from /api/state.v10.config to
        render the banner chips (ATR×N + Partial@1R ON/OFF). Pin them
        here so a future snapshot refactor can't silently drop them.
        v8.1.3 -- env-fallback default for partial flipped to True;
        delete the env var here so the engine sees the production
        env-fallback default (autouse + isolated_env both set =0)."""
        isolated_env.delenv("ORB_PARTIAL_PROFIT_AT_1R", raising=False)
        _bootstrap_session(isolated_env, tickers=["AAPL"])
        snap = live_runtime.snapshot()
        cfg = snap.get("config") or {}
        assert "atr_stop_mult" in cfg
        assert "atr_lookback_5m" in cfg
        assert "partial_profit_at_1r" in cfg
        # Env-default in live_runtime.py: atr_stop_mult=1.75
        # (v8.0.1+), partial_profit_at_1r=True (v8.1.3+).
        assert cfg["atr_stop_mult"] == pytest.approx(1.75)
        assert cfg["atr_lookback_5m"] == 14
        assert cfg["partial_profit_at_1r"] is True

    def test_risk_books_present_with_portfolio_ids(self, isolated_env):
        _bootstrap_session(isolated_env, tickers=["AAPL"])
        snap = live_runtime.snapshot()
        assert "risk_books" in snap
        rb = snap["risk_books"]
        assert "main" in rb

    def test_vix_block_reaches_day_state(self, isolated_env):
        """When VIX > threshold, every (portfolio, ticker) should be
        transitioned to a blocked phase with a populated block_reason."""
        _bootstrap_session(isolated_env, tickers=["AAPL"], vix=30.0)
        snap = live_runtime.snapshot()
        ds_list = snap["day_states"]
        assert any("block" in ds["phase"].lower()
                   for ds in ds_list), \
            f"expected blocked phases under VIX kill, got {ds_list}"


class TestEndToEndShape:
    """Confirm the runtime + scan side produce a dashboard-ready
    snapshot through one bar feed cycle."""

    def test_or_lock_armed_visible(self, isolated_env):
        _bootstrap_session(isolated_env, tickers=["AAPL"])
        # Feed the full 30 OR bars; last bar at bucket 599
        for i in range(30):
            live_runtime.feed_bar(
                ticker="AAPL",
                bar_high=100.5 if i == 0 else 100.1,
                bar_low=99.5 if i == 0 else 100.0,
                bar_open=100.0,
                bar_close=100.05,
                bar_volume=10000,
                bar_bucket_min=570 + i,
            )
        snap = live_runtime.snapshot()
        # OR window should be locked
        w = snap["or_windows"]["AAPL"]
        assert w["locked"] is True
        # day_state should have transitioned past WARMUP
        main_state = next(ds for ds in snap["day_states"]
                          if ds["ticker"] == "AAPL"
                          and ds["portfolio_id"] == "main")
        assert main_state["phase"] != "WARMUP"
