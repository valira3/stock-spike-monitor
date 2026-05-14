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
    monkeypatch.setenv("ORB_PARTIAL_PROFIT_AT_1R", "0")  # v8.1.3 legacy default
    # v8.3.20 -- the env default for ORB_MAX_CONCURRENT_NOTIONAL_MULT
    # dropped from 2.0 -> 0.95 (over-leverage protection per operator
    # directive). Tests that assert against the legacy 2.0 multiplier
    # opt back in here so they continue exercising the same math; new
    # production deploys get the safer 0.95 by default.
    monkeypatch.setenv("ORB_MAX_CONCURRENT_NOTIONAL_MULT", "2.0")
    # Disable SPY-regime gate (default -40bps since v9.0.0) so tests using
    # real market dates aren't blocked by actual prior-day SPY drops when
    # the local bar archive contains real SPY data.
    monkeypatch.setenv("ORB_SKIP_PRIOR_SPY_RET_LT_BPS", "0")
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
        is the compounding-driven sizing percentage) stays at 1.0 (v10
        keystone post-v7.109) so per-trade $ scales with current account balance.

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
            tickers=["AAPL"],
            vix_close_d1=18.0,
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
        # Cfg risk_per_trade_pct is preserved (1% of current equity in v7.109+)
        assert eng.cfg.risk_per_trade_pct == 1.0
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
            tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        # Second call with same date -> no-op (returns False)
        ok = live_runtime.ensure_session_started(
            date_iso="2026-01-02",
            tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        assert ok is False

    def test_session_advances_on_new_date(self, isolated_env):
        self._bootstrap_helper(isolated_env)
        live_runtime.ensure_session_started(
            date_iso="2026-01-02",
            tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        ok = live_runtime.ensure_session_started(
            date_iso="2026-01-03",  # next day
            tickers=["AAPL"],
            vix_close_d1=17.5,
            ticker_open_today={"AAPL": 101.0},
            ticker_prev_close={"AAPL": 100.5},
            equity_per_portfolio={"main": 100000.0},
        )
        assert ok

    def test_ensure_session_pre_bootstrap_returns_false(self, isolated_env):
        # Did NOT call bootstrap()
        ok = live_runtime.ensure_session_started(
            date_iso="2026-01-02",
            tickers=["AAPL"],
            vix_close_d1=18.0,
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
            tickers=["AAPL"],
            vix_close_d1=18.0,
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
                bar_high=h,
                bar_low=l,
                bar_open=100.0,
                bar_close=100.0,
                bar_volume=10000,
                bar_bucket_min=m,
            )

    def test_feed_bar_no_op_when_off(self, isolated_env):
        isolated_env.setenv("ORB_LIVE_MODE", "0")
        live_runtime.bootstrap()
        live_runtime.feed_bar(
            ticker="AAPL",
            bar_high=101.0,
            bar_low=100.0,
            bar_open=100.0,
            bar_close=100.5,
            bar_volume=10000,
            bar_bucket_min=570,
        )
        # Engine should NOT have an OR window (live mode off short-circuits)
        eng = live_runtime.get_engine()
        assert "AAPL" not in eng._state.or_windows

    def test_check_entry_long(self, isolated_env):
        self._setup(isolated_env)
        result = live_runtime.check_entry(
            portfolio_id="main",
            ticker="AAPL",
            side="long",
            five_min_close=101.5,
            next_open=101.5,
            equity=100000.0,
        )
        assert result.ok
        assert result.side == "long"
        assert result.shares > 0

    def test_check_entry_no_signal(self, isolated_env):
        self._setup(isolated_env)
        result = live_runtime.check_entry(
            portfolio_id="main",
            ticker="AAPL",
            side="long",
            five_min_close=100.5,
            next_open=100.5,
            equity=100000.0,
        )
        assert not result.ok
        assert result.reason_no == "no_signal"

    def test_check_entry_unknown_portfolio(self, isolated_env):
        self._setup(isolated_env)
        result = live_runtime.check_entry(
            portfolio_id="not_a_portfolio",
            ticker="AAPL",
            side="long",
            five_min_close=101.5,
            next_open=101.5,
            equity=100000.0,
        )
        assert not result.ok
        assert "no_adapter" in result.reason_no

    def test_check_entry_when_live_mode_off(self, isolated_env):
        isolated_env.setenv("ORB_LIVE_MODE", "0")
        live_runtime.bootstrap()
        result = live_runtime.check_entry(
            portfolio_id="main",
            ticker="AAPL",
            side="long",
            five_min_close=101.5,
            next_open=101.5,
            equity=100000.0,
        )
        assert not result.ok
        assert result.reason_no == "live_mode_off"

    def test_check_exit_target(self, isolated_env):
        self._setup(isolated_env)
        result = live_runtime.check_entry(
            portfolio_id="main",
            ticker="AAPL",
            side="long",
            five_min_close=101.5,
            next_open=101.5,
            equity=100000.0,
        )
        ex = live_runtime.check_exit(
            portfolio_id="main",
            ticker="AAPL",
            ticket_id=result.ticket_id,
            bar_high=110.0,
            bar_low=104.0,
            bar_close=108.0,
            bar_bucket_min=605,
        )
        assert ex.exit
        assert ex.reason == "target"


# ------------------ v8.3.0 OR auto-backfill wrapper ------------------


class TestBackfillOrWindowsWrapper:
    """v8.3.0 -- live_runtime.backfill_or_windows is a thin wrapper
    around OrbEngine.backfill_or_windows. Verify the live-mode + not-
    bootstrapped guard rails."""

    def _bars_30(self):
        return [
            (
                570 + i,
                101.0 if (570 + i) == 580 else 100.5,
                99.0 if (570 + i) == 585 else 100.0,
                100.0,
                100.0,
                10000.0,
            )
            for i in range(30)
        ]

    def test_returns_empty_when_not_bootstrapped(self, isolated_env):
        out = live_runtime.backfill_or_windows(
            bars_by_ticker={"AAPL": self._bars_30()},
            current_et_minutes=11 * 60,
        )
        assert out == {}

    def test_returns_empty_when_live_mode_off(self, isolated_env):
        isolated_env.setenv("ORB_LIVE_MODE", "0")
        live_runtime.bootstrap()
        out = live_runtime.backfill_or_windows(
            bars_by_ticker={"AAPL": self._bars_30()},
            current_et_minutes=11 * 60,
        )
        assert out == {}

    def test_rebuilds_or_when_bootstrapped(self, isolated_env):
        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-05-12",
            tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        out = live_runtime.backfill_or_windows(
            bars_by_ticker={"AAPL": self._bars_30()},
            current_et_minutes=11 * 60,
        )
        assert out["backfilled"] == 1
        eng = live_runtime.get_engine()
        w = eng._state.or_windows["AAPL"]
        assert w.locked
        assert w.or_high == 101.0
        assert w.or_low == 99.0


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
            tickers=["AAPL"],
            vix_close_d1=18.0,
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
            tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        live_runtime.reset_session()
        # Now session_date is empty; ensure_session_started should
        # work for the same date again.
        ok = live_runtime.ensure_session_started(
            date_iso="2026-01-02",
            tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        assert ok


# ------------------ intraday equity refresh (v7.24.0) ------------------


class _StubBook:
    def __init__(self, eq):
        self._eq = float(eq)
        self.paper_cash = float(eq)

    def current_equity(self, prices=None):
        return self._eq


class TestRefreshEquityFromBooks:
    def test_no_op_when_not_bootstrapped(self, isolated_env):
        out = live_runtime.refresh_equity_from_books()
        assert out == {}

    def test_pushes_equity_into_each_riskbook(self, isolated_env, monkeypatch):
        live_runtime.bootstrap()
        # The runtime created RiskBooks with 100k each; intercept
        # PORTFOLIOS to pretend mark-to-market has moved equity.
        import engine.portfolio_book as pb

        new_books = {"main": _StubBook(120000.0), "val": _StubBook(95000.0)}
        monkeypatch.setattr(pb, "PORTFOLIOS", new_books)
        monkeypatch.setattr(pb, "ALL_PORTFOLIO_IDS", list(new_books.keys()))
        out = live_runtime.refresh_equity_from_books()
        assert out.get("main") == 120000.0
        eng = live_runtime.get_engine()
        rb_main = eng._risk.get("main")
        assert rb_main is not None
        assert abs(rb_main.equity - 120000.0) < 0.01

    def test_max_notional_tracks_new_equity(self, isolated_env, monkeypatch):
        live_runtime.bootstrap()
        eng = live_runtime.get_engine()
        rb = eng._risk.get("main")
        before = rb.max_notional
        import engine.portfolio_book as pb

        monkeypatch.setattr(pb, "PORTFOLIOS", {"main": _StubBook(50000.0)})
        monkeypatch.setattr(pb, "ALL_PORTFOLIO_IDS", ["main"])
        live_runtime.refresh_equity_from_books()
        # max_concurrent_notional_mult defaults to 2.0 -> 100k cap
        assert abs(rb.max_notional - 100000.0) < 0.01
        assert rb.max_notional != before

    def test_swallows_book_failure(self, isolated_env, monkeypatch):
        """A misbehaving PortfolioBook should NOT explode the refresh.

        v7.77.0 -- when book.current_equity() raises, the refresh now
        falls through to resolve_equity which reads tg.paper_cash for
        main. That's the canonical source of truth for main's cash
        (v7.72.0 bridge), so this is more correct than the pre-v7.77.0
        fallback to book.paper_cash. Test now patches both so we can
        assert a deterministic chain.
        """
        live_runtime.bootstrap()

        class _Broken:
            paper_cash = 88000.0

            def current_equity(self, prices=None):
                raise RuntimeError("simulated failure")

        import engine.portfolio_book as pb
        import engine.portfolio_equity as pe

        monkeypatch.setattr(pb, "PORTFOLIOS", {"main": _Broken()})
        monkeypatch.setattr(pb, "ALL_PORTFOLIO_IDS", ["main"])
        # Force resolve_equity to fail too so we exercise the final
        # book.paper_cash fallback that the test was originally
        # validating.
        monkeypatch.setattr(pe, "resolve_equity", lambda pid: 0.0)
        out = live_runtime.refresh_equity_from_books()
        # Final fallback: book.paper_cash
        assert out.get("main") == 88000.0

    def test_v7_77_0_main_resolve_equity_fallback_when_book_raises(self, isolated_env, monkeypatch):
        """v7.77.0 -- when main's book.current_equity() raises, the
        refresh falls back to resolve_equity (= tg.paper_cash) instead
        of book.paper_cash. tg.paper_cash is the canonical source."""
        live_runtime.bootstrap()

        class _Broken:
            paper_cash = 88000.0

            def current_equity(self, prices=None):
                raise RuntimeError("simulated failure")

        import engine.portfolio_book as pb
        import engine.portfolio_equity as pe

        monkeypatch.setattr(pb, "PORTFOLIOS", {"main": _Broken()})
        monkeypatch.setattr(pb, "ALL_PORTFOLIO_IDS", ["main"])
        monkeypatch.setattr(pe, "resolve_equity", lambda pid: 105000.0 if pid == "main" else 0.0)
        out = live_runtime.refresh_equity_from_books()
        # resolve_equity wins over book.paper_cash.
        assert out.get("main") == 105000.0

    def test_v7_77_0_val_equity_from_alpaca_not_zero(self, isolated_env, monkeypatch):
        """v7.77.0 regression -- pre-fix the refresh path read
        book.current_equity() directly, which for Val/Gene returns 0
        (paper_cash defaults to 0, never bridged from Alpaca). Every
        scan cycle would overwrite v7.76.0's session-start equity seed
        back to 0, perpetuating the $0 notional cap.

        Post-fix: refresh_equity_from_books goes through
        engine.portfolio_equity.resolve_equity, which prefers Alpaca's
        actual account equity for non-main books.
        """
        live_runtime.bootstrap()

        # Stub: Val's book has paper_cash=0 (the broken default).
        class _ValBookZero:
            paper_cash = 0.0

            def current_equity(self, prices=None):
                return 0.0  # the pre-v7.77.0 wrong answer

        import engine.portfolio_book as pb

        monkeypatch.setattr(pb, "PORTFOLIOS", {"val": _ValBookZero()})
        monkeypatch.setattr(pb, "ALL_PORTFOLIO_IDS", ["val"])

        # Patch resolve_equity at the import site that
        # refresh_equity_from_books pulls from -- engine.portfolio_equity.
        import engine.portfolio_equity as pe

        monkeypatch.setattr(pe, "resolve_equity", lambda pid: 99273.10 if pid == "val" else 0.0)

        # Also seed the engine's risk registry so it has a 'val' book.
        eng = live_runtime.get_engine()
        if eng._risk.get("val") is None:
            from orb.risk_book import RiskBook

            eng._risk._books["val"] = RiskBook(
                "val",
                equity=1.0,
                max_risk_dollars=2000.0,
                max_concurrent_notional_mult=2.0,
                max_trade_notional_pct=75.0,
                daily_loss_kill_pct=2.0,
            )

        out = live_runtime.refresh_equity_from_books()
        # v7.77.0 -- equity should now be $99,273.10 (Alpaca), NOT 0.
        assert out.get("val") == 99273.10, f"refresh path didn't pick up Alpaca equity: {out}"
        rb_val = eng._risk.get("val")
        assert abs(rb_val.equity - 99273.10) < 0.01
        # max_notional = equity * 2.0 = $198K -- entries no longer reject.
        assert abs(rb_val.max_notional - 198546.20) < 0.01


# ---------------------------------------------------------------------
# rollback_admit (v7.81.0)
# ---------------------------------------------------------------------


class TestRollbackAdmit:
    """v7.81.0 -- when the downstream broker call early-returns without
    populating tg.positions, rollback the v10 admit so the FSM doesn't
    stick at IN_POS (phantom IN_POS pattern caught by inv_v10_in_pos_
    has_internal_position in v7.76.0+)."""

    def test_no_op_when_not_bootstrapped(self, isolated_env):
        # Without bootstrap, rollback is a safe no-op.
        assert live_runtime.rollback_admit("main", "AAPL", "x", "test") is False

    def test_rolls_back_fsm_to_armed_and_releases_ticket(self, isolated_env):
        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-05-11",
            tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        eng = live_runtime.get_engine()
        rb = eng._risk.get("main")
        ds = eng._state.get_day_state("main", "AAPL")
        # Manually transition to IN_POS + reserve ticket (simulates the
        # state after try_enter admitted but before broker call ran).
        ticket = rb.try_admit(risk_dollars=500.0, notional=10000.0)
        assert ticket is not None
        from orb import state as _state

        ds.transition(_state.PHASE_ARMED)  # need to start from ARMED to go IN_POS legally
        ds.transition(_state.PHASE_IN_POS)
        ds.in_position = True
        # Sanity
        assert ds.phase == _state.PHASE_IN_POS
        assert rb.open_count == 1
        # Rollback
        ok = live_runtime.rollback_admit("main", "AAPL", ticket.ticket_id, reason="test")
        assert ok is True
        # FSM should be back at ARMED
        assert ds.phase == _state.PHASE_ARMED
        assert ds.in_position is False
        # RiskBook ticket should be released
        assert rb.open_count == 0
        assert rb.open_risk == 0.0
        assert rb.open_notional == 0.0

    def test_rollback_safe_when_no_ticket_id(self, isolated_env):
        """If we lost the ticket id but still want to undo the FSM,
        the helper should at least flip the phase back."""
        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-05-11",
            tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        eng = live_runtime.get_engine()
        ds = eng._state.get_day_state("main", "AAPL")
        from orb import state as _state

        ds.transition(_state.PHASE_ARMED)
        ds.transition(_state.PHASE_IN_POS)
        ds.in_position = True

        ok = live_runtime.rollback_admit("main", "AAPL", "", reason="no-id")
        assert ok is True  # FSM was rolled back even without ticket id
        assert ds.phase == _state.PHASE_ARMED

    def test_rollback_no_op_when_fsm_not_in_pos(self, isolated_env):
        """If FSM is already ARMED (or any non-IN_POS state) and the
        ticket id is unknown, rollback is a complete no-op."""
        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-05-11",
            tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        ok = live_runtime.rollback_admit("main", "AAPL", "no-such-id", reason="nothing-to-undo")
        assert ok is False
