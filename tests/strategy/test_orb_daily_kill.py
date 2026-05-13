"""Tests for v7.29.0 -- daily-loss kill switch.

The `daily_loss_kill_pct` config (default 2.0%) was parsed from env and
stored but never enforced in pre-v7.29 code. This PR closes the live
vs. backtest divergence by:

  - Tracking per-portfolio realized P&L on every on_exit
  - Computing the threshold against session-start equity
  - Blocking new admissions (atomic via RiskBook.try_admit) once
    realized P&L <= -threshold
  - Transitioning all eligible (portfolio, ticker) FSM rows to
    PHASE_BLOCKED_DAILY_KILL when the kill triggers, so no further
    signals fire today
  - Surfacing kill state on RiskBook.snapshot() for the dashboard

These tests exercise the live path end-to-end.
"""
from __future__ import annotations

import os

import pytest

from orb import engine as _engine
from orb import exits as _exits
from orb import state as _state
from orb.risk_book import RiskBook


@pytest.fixture
def isolated_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("ORB_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ORB_PARTIAL_PROFIT_AT_1R", "0")  # v8.1.3 legacy default
    yield monkeypatch


# ----- RiskBook unit tests --------------------------------------


class TestRiskBookDailyKill:

    def test_default_no_kill(self):
        rb = RiskBook(portfolio_id="main", equity=100_000.0,
                      daily_loss_kill_pct=2.0)
        assert rb.daily_kill_triggered is False
        assert rb.realized_pnl_today == 0.0
        assert rb.daily_kill_threshold_dollars == 2000.0

    def test_pnl_accumulates(self):
        rb = RiskBook(portfolio_id="main", equity=100_000.0,
                      daily_loss_kill_pct=2.0)
        rb.record_realized_pnl(-500.0)
        rb.record_realized_pnl(-300.0)
        assert rb.realized_pnl_today == -800.0
        assert rb.daily_kill_triggered is False

    def test_kill_triggers_at_threshold(self):
        rb = RiskBook(portfolio_id="main", equity=100_000.0,
                      daily_loss_kill_pct=2.0)
        # 2% of 100k = $2000; -$2100 cumulative breaches
        rb.record_realized_pnl(-1500.0)
        assert rb.daily_kill_triggered is False
        first_kill = rb.record_realized_pnl(-600.0)
        assert first_kill is True  # crossed on this exit
        assert rb.daily_kill_triggered is True

    def test_kill_returns_false_on_repeat(self):
        rb = RiskBook(portfolio_id="main", equity=100_000.0,
                      daily_loss_kill_pct=2.0)
        rb.record_realized_pnl(-2500.0)
        assert rb.daily_kill_triggered is True
        # Subsequent losses don't re-trigger the "just-crossed" return
        repeat = rb.record_realized_pnl(-100.0)
        assert repeat is False
        assert rb.daily_kill_triggered is True

    def test_try_admit_blocks_when_killed(self):
        rb = RiskBook(portfolio_id="main", equity=100_000.0,
                      max_concurrent_risk_dollars=2000.0,
                      daily_loss_kill_pct=2.0)
        rb.record_realized_pnl(-2500.0)
        ticket = rb.try_admit(risk_dollars=500.0, notional=10_000.0)
        assert ticket is None
        assert rb.last_reject_reason.startswith("daily_kill")

    def test_reset_session_clears_kill(self):
        rb = RiskBook(portfolio_id="main", equity=100_000.0,
                      daily_loss_kill_pct=2.0)
        rb.record_realized_pnl(-2500.0)
        assert rb.daily_kill_triggered is True
        rb.reset_session()
        assert rb.daily_kill_triggered is False
        assert rb.realized_pnl_today == 0.0
        # Threshold re-pins to current equity at reset
        assert rb.session_start_equity == rb.equity

    def test_snapshot_exposes_kill_state(self):
        rb = RiskBook(portfolio_id="main", equity=100_000.0,
                      daily_loss_kill_pct=2.0)
        rb.record_realized_pnl(-2100.0)
        snap = rb.snapshot()
        assert snap["daily_kill_triggered"] is True
        assert snap["realized_pnl_today"] == -2100.0
        assert snap["daily_kill_threshold"] == 2000.0
        assert snap["daily_loss_kill_pct"] == 2.0

    def test_zero_kill_pct_never_triggers(self):
        rb = RiskBook(portfolio_id="main", equity=100_000.0,
                      daily_loss_kill_pct=0.0)
        # threshold = 0 -> always disabled (the safety check is
        # `if threshold > 0` in record_realized_pnl)
        rb.record_realized_pnl(-50_000.0)
        assert rb.daily_kill_triggered is False

    def test_concurrent_admit_after_killing_pnl_record(self):
        """Atomicity: record_realized_pnl flipping the flag AND a
        try_admit reading the flag must both go through the RLock
        without interleaving. This is a smoke check, not a real race
        reproducer."""
        rb = RiskBook(portfolio_id="main", equity=100_000.0,
                      daily_loss_kill_pct=2.0)
        rb.record_realized_pnl(-2500.0)
        # Even a same-thread try_admit immediately after must see the
        # killed state.
        ticket = rb.try_admit(risk_dollars=100.0, notional=1000.0)
        assert ticket is None


# ----- Engine integration ---------------------------------------


def _make_engine(*, daily_loss_kill_pct: float = 2.0,
                  portfolio_ids: list[str] | None = None):
    cfg = _engine.OrbConfig(
        daily_loss_kill_pct=daily_loss_kill_pct,
        risk_per_trade_pct=2.0,
        max_concurrent_risk_dollars=10_000.0,
        max_concurrent_notional_mult=5.0,
    )
    e = _engine.OrbEngine(cfg, portfolio_ids=portfolio_ids or ["main"])
    e.start_new_session(
        date_iso="2026-05-10",
        tickers=["AAPL", "MSFT"],
        vix_close_d1=15.0,
        ticker_open_today={"AAPL": 100.0, "MSFT": 200.0},
        ticker_prev_close={"AAPL": 100.0, "MSFT": 200.0},
        equity_per_portfolio={pid: 100_000.0
                              for pid in (portfolio_ids or ["main"])},
    )
    return e


def _make_position(pid="main", ticker="AAPL", side="long",
                   entry=100.0, stop=99.0, shares=100,
                   ticket_id="T1"):
    return _exits.make_position(
        portfolio_id=pid, ticker=ticker, side=side,
        entry_price=entry, stop=stop, rr=2.5,
        shares=shares, risk_ticket_id=ticket_id,
    )


class TestEngineDailyKill:

    def test_on_exit_records_long_loss(self, isolated_env):
        e = _make_engine()
        rb = e._risk.get("main")
        pos = _make_position(entry=100.0, stop=99.0, shares=500)
        # Simulate a stop hit: exit at $99 -> realized P&L = -$500
        decision = _exits.ExitDecision(reason="stop", price=99.0)
        e.on_exit(pos, decision)
        assert rb.realized_pnl_today == pytest.approx(-500.0)

    def test_on_exit_records_short_loss(self, isolated_env):
        e = _make_engine()
        rb = e._risk.get("main")
        pos = _make_position(side="short", entry=100.0, stop=101.0,
                             shares=500)
        # Short stop hit at $101: pnl = 500 * (100 - 101) = -$500
        decision = _exits.ExitDecision(reason="stop", price=101.0)
        e.on_exit(pos, decision)
        assert rb.realized_pnl_today == pytest.approx(-500.0)

    def test_on_exit_records_winning_trade(self, isolated_env):
        e = _make_engine()
        rb = e._risk.get("main")
        pos = _make_position(entry=100.0, stop=99.0, shares=500)
        # Target hit at $102.50 (RR=2.5, risk=$1): pnl = 500 * 2.50 = +$1250
        decision = _exits.ExitDecision(reason="target", price=102.50)
        e.on_exit(pos, decision)
        assert rb.realized_pnl_today == pytest.approx(1250.0)

    def test_cumulative_loss_triggers_kill(self, isolated_env):
        e = _make_engine()
        rb = e._risk.get("main")
        # First losing trade: -$1500
        pos1 = _make_position(entry=100.0, stop=99.0, shares=1500,
                              ticket_id="T1")
        e.on_exit(pos1, _exits.ExitDecision(reason="stop", price=99.0))
        assert rb.daily_kill_triggered is False
        # Second losing trade: -$600 -> cumulative -$2100 > -$2000 kill
        pos2 = _make_position(entry=100.0, stop=99.0, shares=600,
                              ticket_id="T2")
        e.on_exit(pos2, _exits.ExitDecision(reason="stop", price=99.0))
        assert rb.daily_kill_triggered is True

    def test_kill_blocks_other_tickers_on_same_portfolio(self, isolated_env):
        e = _make_engine()
        # Push MSFT FSM past WARMUP to ARMED first so the block transition
        # has somewhere to come from.
        msft_state = e._state.get_day_state("main", "MSFT")
        msft_state.transition(_state.PHASE_ARMED)
        # Lose enough on AAPL to trigger the kill.
        pos = _make_position(ticker="AAPL", entry=100.0, stop=99.0,
                             shares=2500)
        e.on_exit(pos, _exits.ExitDecision(reason="stop", price=99.0))
        # MSFT should now be blocked.
        msft_after = e._state.get_day_state("main", "MSFT")
        assert msft_after.phase == _state.PHASE_BLOCKED_DAILY_KILL
        assert "daily_loss_kill" in msft_after.block_reason

    def test_kill_isolated_per_portfolio(self, isolated_env):
        e = _make_engine(portfolio_ids=["main", "val"])
        # Both portfolios start with the same equity in _make_engine
        rb_main = e._risk.get("main")
        rb_val = e._risk.get("val")
        # Kill main only.
        pos = _make_position(pid="main", entry=100.0, stop=99.0,
                             shares=2500)
        e.on_exit(pos, _exits.ExitDecision(reason="stop", price=99.0))
        assert rb_main.daily_kill_triggered is True
        assert rb_val.daily_kill_triggered is False

    def test_kill_does_not_block_in_position_row(self, isolated_env):
        e = _make_engine()
        # Push MSFT to IN_POS (simulate an existing open position).
        msft_state = e._state.get_day_state("main", "MSFT")
        msft_state.transition(_state.PHASE_IN_POS)
        # Trigger the kill via AAPL.
        pos = _make_position(ticker="AAPL", entry=100.0, stop=99.0,
                             shares=2500)
        e.on_exit(pos, _exits.ExitDecision(reason="stop", price=99.0))
        # MSFT's open position should NOT be force-blocked; the live
        # exit logic still manages it to its target / stop.
        msft_after = e._state.get_day_state("main", "MSFT")
        assert msft_after.phase == _state.PHASE_IN_POS

    def test_session_reset_clears_kill(self, isolated_env):
        e = _make_engine()
        rb = e._risk.get("main")
        pos = _make_position(entry=100.0, stop=99.0, shares=2500)
        e.on_exit(pos, _exits.ExitDecision(reason="stop", price=99.0))
        assert rb.daily_kill_triggered is True
        # Start a new session -> kill state must reset.
        e.start_new_session(
            date_iso="2026-05-11",
            tickers=["AAPL"],
            vix_close_d1=15.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100_000.0},
        )
        assert rb.daily_kill_triggered is False
        assert rb.realized_pnl_today == 0.0

    def test_winning_then_losing_does_not_trigger_too_early(self, isolated_env):
        e = _make_engine()
        rb = e._risk.get("main")
        # +$1000 winner
        win = _make_position(entry=100.0, stop=99.0, shares=500,
                             ticket_id="T1")
        e.on_exit(win, _exits.ExitDecision(reason="target", price=102.0))
        assert rb.realized_pnl_today == pytest.approx(1000.0)
        # -$2200 loser leaves cumulative at -$1200 -- still above threshold
        loser = _make_position(entry=100.0, stop=99.0, shares=2200,
                               ticket_id="T2")
        e.on_exit(loser, _exits.ExitDecision(reason="stop", price=99.0))
        assert rb.realized_pnl_today == pytest.approx(-1200.0)
        assert rb.daily_kill_triggered is False
