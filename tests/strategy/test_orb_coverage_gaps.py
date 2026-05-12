"""Tests for v7.31.0 -- close MEDIUM coverage gaps from the deep audit.

The audit flagged five untested branches in the v10 production path:

  1. refresh_equity_from_books integration: equity drop should shrink
     RiskBook.max_notional (the cap used by try_admit).
  2. Multi-portfolio admit-and-reject same tick: Main admits while Val
     rejects (different equity); isolation must hold.
  3. Zero-equity admission rejection: portfolio with 0 equity should
     reject.
  4. Bar with high < low (data corruption): defensive evaluate path.
  5. EOD with no open positions: defensive no-op smoke.

Also includes a VIX-fail-closed observability check (v7.31.0 added a
[V79-ORB-VIX] warning when VIX is missing in production).
"""
from __future__ import annotations

import logging
import os

import pytest

from orb import engine as _engine
from orb import exits as _exits
from orb import live_runtime
from orb import state as _state
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
    monkeypatch.setenv("ORB_PARTIAL_PROFIT_AT_1R", "0")  # v8.1.3 legacy default
    yield monkeypatch


# ----- 1. refresh_equity_from_books integration ------------------


class _StubBook:
    def __init__(self, eq):
        self._eq = float(eq)
        self.paper_cash = float(eq)
    def current_equity(self, prices=None):
        return self._eq


class TestEquityRefreshIntegration:

    def test_max_notional_shrinks_on_equity_drop(self, isolated_env,
                                                  monkeypatch):
        """Audit gap #1: when MTM drops equity mid-session, the
        RiskBook's max_notional cap (equity * max_concurrent_notional_mult)
        must shrink so over-sized admissions get rejected."""
        live_runtime.bootstrap()
        eng = live_runtime.get_engine()
        rb = eng._risk.get("main")
        # Default boot equity is 100_000; cap_mult=2.0 -> max_notional=200k
        assert rb.max_notional == pytest.approx(200_000.0)
        # MTM drop to 50k
        import engine.portfolio_book as pb
        monkeypatch.setattr(pb, "PORTFOLIOS",
                            {"main": _StubBook(50_000.0)})
        monkeypatch.setattr(pb, "ALL_PORTFOLIO_IDS", ["main"])
        live_runtime.refresh_equity_from_books()
        assert rb.max_notional == pytest.approx(100_000.0)

    def test_admission_blocked_after_equity_drop(self, isolated_env,
                                                  monkeypatch):
        """An admission that would have fit at 100k equity now exceeds
        the cap at 50k equity."""
        live_runtime.bootstrap()
        eng = live_runtime.get_engine()
        rb = eng._risk.get("main")
        # Drop equity
        import engine.portfolio_book as pb
        monkeypatch.setattr(pb, "PORTFOLIOS",
                            {"main": _StubBook(50_000.0)})
        monkeypatch.setattr(pb, "ALL_PORTFOLIO_IDS", ["main"])
        live_runtime.refresh_equity_from_books()
        # max_notional is now 100k. A trade with notional=120k should reject.
        ticket = rb.try_admit(risk_dollars=500.0, notional=120_000.0)
        assert ticket is None
        assert "notional_cap" in rb.last_reject_reason


# ----- 2. Multi-portfolio admit + reject same tick ---------------


class TestMultiPortfolioIsolation:

    def test_main_admits_val_rejects_same_tick(self, isolated_env):
        """Audit gap: Main admits, Val rejects (e.g. risk cap). The two
        portfolios' risk_books must remain independent -- Val rejection
        does not affect Main's admission and vice versa."""
        cfg = _engine.OrbConfig(
            daily_loss_kill_pct=0.0,           # disable for this test
            max_concurrent_risk_dollars=2_000.0,
        )
        eng = _engine.OrbEngine(cfg, portfolio_ids=["main", "val"])
        eng.start_new_session(
            date_iso="2026-05-10", tickers=["AAPL"],
            vix_close_d1=15.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100_000.0, "val": 100_000.0},
        )
        # Pre-fill val's risk_book so its cap is exhausted
        val_rb = eng._risk.get("val")
        pre_fill = val_rb.try_admit(risk_dollars=1_900.0, notional=10_000.0)
        assert pre_fill is not None
        # Now both portfolios attempt fresh admissions of size 500 risk.
        main_rb = eng._risk.get("main")
        main_ticket = main_rb.try_admit(risk_dollars=500.0, notional=10_000.0)
        val_ticket = val_rb.try_admit(risk_dollars=500.0, notional=10_000.0)
        assert main_ticket is not None, "Main should admit"
        assert val_ticket is None, "Val should reject (cap reached)"
        assert "risk_cap" in val_rb.last_reject_reason
        # Main's state untouched by Val's reject
        assert main_rb.last_reject_reason == ""


# ----- 3. Zero-equity admission ---------------------------------


class TestZeroEquity:

    def test_zero_equity_caps_max_notional(self):
        """Audit gap: portfolio with 0 equity should fail all
        notional-cap admissions (cap = 0 * mult = 0)."""
        rb = RiskBook(portfolio_id="main", equity=0.0,
                      max_concurrent_risk_dollars=2_000.0,
                      max_concurrent_notional_mult=2.0)
        # Even tiny notional should reject
        ticket = rb.try_admit(risk_dollars=10.0, notional=100.0)
        assert ticket is None
        assert "notional_cap" in rb.last_reject_reason

    def test_zero_equity_admits_zero_risk(self):
        """Pathological: zero risk + zero notional admits, but the
        position is meaningless. Defensive coverage of the lower bound."""
        rb = RiskBook(portfolio_id="main", equity=0.0,
                      max_concurrent_risk_dollars=2_000.0,
                      max_concurrent_notional_mult=2.0)
        ticket = rb.try_admit(risk_dollars=0.0, notional=0.0)
        assert ticket is not None


# ----- 4. Bar with high < low (data corruption) ------------------


class TestDataCorruption:

    def test_exit_eval_does_not_crash_on_inverted_bar(self):
        """Audit gap: exits.evaluate assumes bar_high >= bar_low. If a
        corrupted bar arrives with high<low, the evaluator should not
        raise -- prefer graceful degradation."""
        pos = _exits.make_position(
            portfolio_id="main", ticker="AAPL", side="long",
            entry_price=100.0, stop=99.0, rr=2.5, shares=100,
            risk_ticket_id="T1",
        )
        # Inverted bar; should produce a sensible (or absent) decision,
        # NOT a Python exception.
        result = _exits.evaluate(
            pos,
            bar_high=99.0, bar_low=100.0, bar_close=99.5,
            bar_bucket_min=605,
            eod_cutoff_min=955,
        )
        # Either None (no decision) or a valid ExitDecision is acceptable.
        assert result is None or isinstance(result, _exits.ExitDecision)


# ----- 5. EOD with no open positions -----------------------------


class TestEodNoPositions:

    def test_eod_check_with_no_open_positions_is_noop(self, isolated_env):
        """Audit gap: defensive smoke -- when no v10 positions are open
        at 15:55 ET, check_exit / check_exit_by_ticker should return a
        no-op cleanly (not raise)."""
        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-05-10", tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100_000.0},
        )
        # No entry has been made; ask for an EOD exit
        result = live_runtime.check_exit_by_ticker(
            portfolio_id="main", ticker="AAPL",
            bar_high=100.1, bar_low=99.9, bar_close=100.0,
            bar_bucket_min=955,
        )
        assert result is not None
        assert result.exit is False
        # Tightened in v7.32.0: must be the exact "no_open_v10_position"
        # sentinel; previously accepted three different reasons which
        # masked accidental "live_mode_off" / "" matches.
        assert result.reason == "no_open_v10_position"


# ----- 6. VIX missing -> [V79-ORB-VIX] warning -------------------


class TestVixMissingObservability:

    def test_missing_vix_with_fail_closed_logs_warning(self, isolated_env,
                                                       caplog):
        """v7.31.0: a missing VIX with fail_closed_on_missing_vix=True
        must emit a WARNING-level [V79-ORB-VIX] forensic so the operator
        notices the day-block isn't a strategy decision but a data
        outage. Default OrbConfig has fail_closed=True."""
        cfg = _engine.OrbConfig(
            daily_loss_kill_pct=0.0,
            fail_closed_on_missing_vix=True,
        )
        eng = _engine.OrbEngine(cfg, portfolio_ids=["main"])
        with caplog.at_level(logging.WARNING, logger="orb.day_gates"):
            result = eng.start_new_session(
                date_iso="2026-05-10", tickers=["AAPL"],
                vix_close_d1=None,
                ticker_open_today={"AAPL": 100.0},
                ticker_prev_close={"AAPL": 100.0},
                equity_per_portfolio={"main": 100_000.0},
            )
        assert result.block_day is True
        assert result.block_reason == "missing_vix"
        assert any("[V79-ORB-VIX]" in r.message for r in caplog.records), \
            f"expected [V79-ORB-VIX] warning, got: {[r.message for r in caplog.records]}"

    def test_missing_vix_with_fail_open_logs_warning(self, isolated_env,
                                                     caplog):
        """The fail-open path (backtest parity) should ALSO log a
        warning. This branch should never run in production but the
        warning surfaces if it's accidentally enabled."""
        cfg = _engine.OrbConfig(
            daily_loss_kill_pct=0.0,
            fail_closed_on_missing_vix=False,
        )
        eng = _engine.OrbEngine(cfg, portfolio_ids=["main"])
        with caplog.at_level(logging.WARNING, logger="orb.day_gates"):
            result = eng.start_new_session(
                date_iso="2026-05-10", tickers=["AAPL"],
                vix_close_d1=None,
                ticker_open_today={"AAPL": 100.0},
                ticker_prev_close={"AAPL": 100.0},
                equity_per_portfolio={"main": 100_000.0},
            )
        assert result.block_day is False
        assert any("[V79-ORB-VIX]" in r.message for r in caplog.records)
