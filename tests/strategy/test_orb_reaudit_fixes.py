"""Tests for v7.32.0 -- close the new gaps found in the re-audit.

Covers six fixes:

  1. live_runtime read paths (feed_bar/check_entry/check_exit/
     check_exit_by_ticker/snapshot) snapshot _engine/_adapters under
     _bootstrap_lock so a concurrent bootstrap can't race.
  2. on_exit defensive P&L validation: shares<=0 or entry/exit_price<=0
     are skipped with a WARNING (NOT silently counted as $0 P&L which
     would mask buggy positions).
  3. reset_session also clears _pending_v10_sizes (was persisting
     stale sizes across sessions).
  4. _reset_for_testing acquires both locks.
  5. Dispatch ERROR-level logging when fire_long/fire_short raises a
     non-broker exception (exc_info=True so the traceback is in the
     log file even when callbacks is None).
  6. Tighter "no_open_v10_position" assertion on the EOD-no-positions
     test (lives in test_orb_coverage_gaps.py).
"""
from __future__ import annotations

import logging
import os
import threading

import pytest

from orb import engine as _engine
from orb import exits as _exits
from orb import live_runtime
from orb.risk_book import RiskBook


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


# ----- 1. Snapshot pattern on read paths -------------------------


class TestReadPathsSnapshotRefs:
    """Smoke tests for the snapshot-then-deref pattern: after
    bootstrap, all four read paths return a sensible result; after
    _reset_for_testing, they fail-soft (no AttributeError on None)."""

    def test_feed_bar_no_op_after_reset(self, isolated_env):
        live_runtime.bootstrap()
        live_runtime._reset_for_testing()
        # Must not raise
        live_runtime.feed_bar(
            ticker="AAPL", bar_high=100.0, bar_low=99.0, bar_open=99.5,
            bar_close=99.8, bar_volume=1000, bar_bucket_min=570,
        )

    def test_check_entry_no_op_after_reset(self, isolated_env):
        live_runtime.bootstrap()
        live_runtime._reset_for_testing()
        r = live_runtime.check_entry(
            portfolio_id="main", ticker="AAPL", side="long",
            five_min_close=100.0, next_open=100.0, equity=100_000.0,
        )
        assert r.ok is False
        assert r.reason_no == "live_mode_off"

    def test_check_exit_no_op_after_reset(self, isolated_env):
        live_runtime.bootstrap()
        live_runtime._reset_for_testing()
        r = live_runtime.check_exit(
            portfolio_id="main", ticker="AAPL", ticket_id="T1",
            bar_high=100.0, bar_low=99.0, bar_close=99.5,
            bar_bucket_min=570,
        )
        assert r.exit is False

    def test_check_exit_by_ticker_no_op_after_reset(self, isolated_env):
        live_runtime.bootstrap()
        live_runtime._reset_for_testing()
        r = live_runtime.check_exit_by_ticker(
            portfolio_id="main", ticker="AAPL",
            bar_high=100.0, bar_low=99.0, bar_close=99.5,
            bar_bucket_min=570,
        )
        assert r.exit is False

    def test_snapshot_after_reset(self, isolated_env):
        live_runtime.bootstrap()
        live_runtime._reset_for_testing()
        snap = live_runtime.snapshot()
        assert snap["bootstrapped"] is False


# ----- 2. Defensive P&L validation in on_exit --------------------


def _make_engine_kill_on():
    cfg = _engine.OrbConfig(
        daily_loss_kill_pct=2.0,
        risk_per_trade_pct=2.0,
        max_concurrent_risk_dollars=10_000.0,
        max_concurrent_notional_mult=5.0,
    )
    eng = _engine.OrbEngine(cfg, portfolio_ids=["main"])
    eng.start_new_session(
        date_iso="2026-05-10", tickers=["AAPL"],
        vix_close_d1=15.0,
        ticker_open_today={"AAPL": 100.0},
        ticker_prev_close={"AAPL": 100.0},
        equity_per_portfolio={"main": 100_000.0},
    )
    return eng


class TestOnExitDefensive:

    def test_zero_shares_skips_pnl_with_warning(self, caplog):
        eng = _make_engine_kill_on()
        rb = eng._risk.get("main")
        pos = _exits.make_position(
            portfolio_id="main", ticker="AAPL", side="long",
            entry_price=100.0, stop=99.0, rr=2.5,
            shares=10, risk_ticket_id="T1",
        )
        # Forcibly zero out shares post-construction to simulate a
        # buggy upstream caller.
        pos.shares = 0
        with caplog.at_level(logging.WARNING, logger="orb.engine"):
            eng.on_exit(pos, _exits.ExitDecision(reason="stop", price=99.0))
        # P&L MUST NOT have moved -- with shares=0 the old code would
        # have counted $0 and silently suppressed the kill threshold.
        assert rb.realized_pnl_today == 0.0
        assert any("skipping P&L accounting" in r.message
                   for r in caplog.records)

    def test_zero_entry_price_skips_pnl_with_warning(self, caplog):
        eng = _make_engine_kill_on()
        rb = eng._risk.get("main")
        pos = _exits.make_position(
            portfolio_id="main", ticker="AAPL", side="long",
            entry_price=100.0, stop=99.0, rr=2.5,
            shares=10, risk_ticket_id="T1",
        )
        pos.entry_price = 0.0
        with caplog.at_level(logging.WARNING, logger="orb.engine"):
            eng.on_exit(pos, _exits.ExitDecision(reason="stop", price=99.0))
        assert rb.realized_pnl_today == 0.0
        assert any("skipping P&L accounting" in r.message
                   for r in caplog.records)

    def test_well_formed_position_records_normally(self):
        eng = _make_engine_kill_on()
        rb = eng._risk.get("main")
        pos = _exits.make_position(
            portfolio_id="main", ticker="AAPL", side="long",
            entry_price=100.0, stop=99.0, rr=2.5,
            shares=10, risk_ticket_id="T1",
        )
        eng.on_exit(pos, _exits.ExitDecision(reason="stop", price=99.0))
        assert rb.realized_pnl_today == pytest.approx(-10.0)


# ----- 3. reset_session clears _pending_v10_sizes ----------------


class TestResetClearsStashedSizes:

    def test_stash_cleared_on_reset_session(self, isolated_env):
        live_runtime.bootstrap()
        live_runtime.stash_v10_size("main", "AAPL", 500)
        assert live_runtime.peek_v10_size("main", "AAPL") == 500
        live_runtime.reset_session()
        assert live_runtime.peek_v10_size("main", "AAPL") is None

    def test_stash_cleared_on_reset_for_testing(self, isolated_env):
        live_runtime.bootstrap()
        live_runtime.stash_v10_size("val", "MSFT", 200)
        live_runtime._reset_for_testing()
        # Pop should return None after reset
        assert live_runtime.consume_v10_size("val", "MSFT") is None


# ----- 4. _reset_for_testing under contention --------------------


class TestResetUnderLoad:

    def test_reset_during_concurrent_stash_does_not_crash(self, isolated_env):
        live_runtime.bootstrap()

        stop_flag = threading.Event()
        errors = []

        def stasher():
            i = 0
            while not stop_flag.is_set() and i < 5000:
                try:
                    live_runtime.stash_v10_size("main", f"T{i % 50}", i)
                except Exception as e:
                    errors.append(e)
                i += 1

        t = threading.Thread(target=stasher)
        t.start()
        # Hammer reset_for_testing while stasher runs
        for _ in range(50):
            try:
                live_runtime._reset_for_testing()
                live_runtime.bootstrap()
            except Exception as e:
                errors.append(e)
        stop_flag.set()
        t.join()
        assert not errors, f"unexpected races: {errors}"


# ----- 5. Dispatch error logging -----------------------------------


class TestDispatchErrorLogging:

    def test_executor_fire_raise_logs_at_error_level(self, isolated_env,
                                                     caplog):
        try:
            from engine.scan import _v10_dispatch_executor_fire
        except (ModuleNotFoundError, ImportError) as e:
            if "telegram" in str(e):
                pytest.skip("telegram unavailable in sandbox")
            raise
        from unittest.mock import MagicMock, patch
        isolated_env.setenv("ORB_LIVE_MODE", "1")
        isolated_env.setenv("ORB_PORTFOLIO_FIRE", "1")

        fake_ex = MagicMock()
        fake_ex.fire_long.side_effect = RuntimeError("simulated bug")

        with patch("executors.bootstrap.get_executor",
                   return_value=fake_ex):
            with caplog.at_level(logging.ERROR, logger="engine.scan"):
                _v10_dispatch_executor_fire(
                    pid="val", side="long", ticker="AAPL",
                    price=100.0, shares=10, callbacks=None,
                )
        # Must log at ERROR level (not WARNING) with traceback
        err_recs = [r for r in caplog.records
                    if r.levelname == "ERROR"
                    and "fire raised" in r.message]
        assert err_recs, f"expected ERROR-level log, got: {[r.message for r in caplog.records]}"
        # exc_info=True attaches a traceback
        assert err_recs[0].exc_info is not None
