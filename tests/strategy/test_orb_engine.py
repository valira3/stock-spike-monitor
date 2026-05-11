"""Tests for orb.engine -- public surface (state + risk + gates + exits)."""
from __future__ import annotations

import pytest

from orb.engine import OrbConfig, OrbEngine, BreakoutSignal, Admission
from orb.state import (
    PHASE_WARMUP, PHASE_OR_LOCKED, PHASE_ARMED, PHASE_IN_POS,
    PHASE_CLOSED, PHASE_BLOCKED_VIX, PHASE_BLOCKED_BLOCKLIST,
    PHASE_BLOCKED_RANGE,
)


def make_config(**overrides):
    """v10 keystone defaults; override individual fields per test."""
    cfg = OrbConfig(
        or_minutes=30,
        rr=2.5,
        range_min_pct=0.008,
        range_max_pct=0.025,
        max_trades_per_day=5,
        risk_per_trade_pct=2.0,
        max_concurrent_risk_dollars=2000.0,
        max_trade_notional_pct=75.0,
        skip_vix_above=22.0,
        skip_earnings_window=False,  # most tests disable for clarity
        skip_gap_above_pct=1.5,
        fail_closed_on_missing_vix=False,
        ticker_side_blocklist={"META": ["LONG", "SHORT"]},
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def feed_or_window(eng: OrbEngine, ticker: str, *, or_high=101.0, or_low=99.0):
    """Helper: feed exactly 30 1-min bars to populate + lock the OR window."""
    # 09:30 = 570; 30-min OR ends at 600. Feed 570..599 (last bar at 599).
    for m in range(570, 600):
        # Make first bar set open + ensure high/low cover the window
        h = or_high if m == 580 else or_high - 0.5
        l = or_low if m == 585 else or_low + 0.5
        eng.on_bar_arrival(
            ticker=ticker,
            bar_high=h, bar_low=l,
            bar_open=100.0, bar_close=100.0,
            bar_volume=10000, bar_bucket_min=m,
        )


# ------------------ session lifecycle ------------------


class TestSessionLifecycle:

    def test_start_session_clears_state(self):
        cfg = make_config()
        eng = OrbEngine(cfg, portfolio_ids=["main", "val", "gene"])
        eng.start_new_session(
            date_iso="2026-01-02",
            tickers=["AAPL", "META"],
            vix_close_d1=18.5,
            ticker_open_today={"AAPL": 100.0, "META": 500.0},
            ticker_prev_close={"AAPL": 100.0, "META": 500.0},
            equity_per_portfolio={"main": 100000.0, "val": 50000.0, "gene": 25000.0},
        )
        # main on AAPL: warmup (no block)
        ds = eng._state.get_day_state("main", "AAPL")
        assert ds.phase == PHASE_WARMUP
        # main on META: blocked (blocklist)
        ds_meta = eng._state.get_day_state("main", "META")
        assert ds_meta.phase == PHASE_BLOCKED_BLOCKLIST

    def test_vix_block_sets_all_tickers_blocked(self):
        cfg = make_config()
        eng = OrbEngine(cfg, portfolio_ids=["main"])
        eng.start_new_session(
            date_iso="2026-01-02",
            tickers=["AAPL", "NVDA"],
            vix_close_d1=25.0,  # > threshold 22
            ticker_open_today={"AAPL": 100.0, "NVDA": 100.0},
            ticker_prev_close={"AAPL": 100.0, "NVDA": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        for tk in ("AAPL", "NVDA"):
            ds = eng._state.get_day_state("main", tk)
            assert ds.phase == PHASE_BLOCKED_VIX

    def test_per_portfolio_independent_equity(self):
        cfg = make_config()
        eng = OrbEngine(cfg, portfolio_ids=["main", "val"])
        eng.start_new_session(
            date_iso="2026-01-02", tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0, "val": 50000.0},
        )
        assert eng._risk.get("main").equity == 100000.0
        assert eng._risk.get("val").equity == 50000.0


# ------------------ on_bar_arrival ------------------


class TestOnBarArrival:

    def test_bars_during_or_window_added(self):
        cfg = make_config()
        eng = OrbEngine(cfg, portfolio_ids=["main"])
        eng.start_new_session(
            date_iso="2026-01-02", tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        # Feed first bar at 09:30 = 570
        eng.on_bar_arrival(ticker="AAPL", bar_high=101.0, bar_low=100.0,
                           bar_open=100.0, bar_close=100.5,
                           bar_volume=10000, bar_bucket_min=570)
        w = eng._state.or_windows["AAPL"]
        assert w.bars_seen == 1
        assert not w.locked

    def test_bar_at_last_bucket_locks_window(self):
        cfg = make_config()
        eng = OrbEngine(cfg, portfolio_ids=["main"])
        eng.start_new_session(
            date_iso="2026-01-02", tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        # 30-min OR; feed bars 570..599
        # Make high-low spread = 1% (within 0.8-2.5% range_min/max)
        feed_or_window(eng, "AAPL", or_high=101.0, or_low=100.0)
        w = eng._state.or_windows["AAPL"]
        assert w.locked
        assert w.bars_seen == 30
        # Width = (101 - 100) / 100.5 ≈ 0.995% -> within band -> ARMED
        ds = eng._state.get_day_state("main", "AAPL")
        assert ds.phase == PHASE_ARMED

    def test_or_window_too_narrow_blocks_ticker(self):
        cfg = make_config()
        eng = OrbEngine(cfg, portfolio_ids=["main"])
        eng.start_new_session(
            date_iso="2026-01-02", tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        # OR width = 0.5% (below range_min 0.8%)
        feed_or_window(eng, "AAPL", or_high=100.50, or_low=100.0)
        ds = eng._state.get_day_state("main", "AAPL")
        assert ds.phase == PHASE_BLOCKED_RANGE

    def test_missed_last_bar_post_window_bar_triggers_lock_fallback(self):
        """v7.73.0 -- when the exact-last-bar (bucket 599) is missed but
        the OR window has bars and a POST-window bar arrives, lock now.

        Pre-v7.73.0, missing the 09:59 bar -> OR never locks for the day
        because subsequent bars (>= or_end_min) are rejected by add_bar
        and the strict equality check on the lock trigger meant no later
        path could rescue. Symptom from prod 2026-05-11: 0/10 tickers
        locked at 10:14 ET after a Railway redeploy during OR window.
        """
        cfg = make_config()
        eng = OrbEngine(cfg, portfolio_ids=["main"])
        eng.start_new_session(
            date_iso="2026-01-02", tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        # Feed bars 570..598 but SKIP 599 (the magic bucket pre-fix
        # depended on). Width still ~1% so the OR is valid.
        for m in range(570, 599):
            h = 101.0 if m == 580 else 100.5
            l = 100.0 if m == 585 else 100.5
            eng.on_bar_arrival(
                ticker="AAPL", bar_high=h, bar_low=l,
                bar_open=100.0, bar_close=100.0,
                bar_volume=10000, bar_bucket_min=m,
            )
        # No lock yet -- 599 was the trigger bucket pre-fix.
        w = eng._state.or_windows["AAPL"]
        assert not w.locked, "pre-fix path would also be unlocked here"
        # Now a post-window bar arrives (e.g. 600 = 10:00 ET). With the
        # fallback in place this triggers the missed-lock rescue.
        eng.on_bar_arrival(
            ticker="AAPL", bar_high=102.0, bar_low=101.5,
            bar_open=101.8, bar_close=101.9,
            bar_volume=10000, bar_bucket_min=600,
        )
        assert w.locked, "fallback should lock OR on first post-window bar"
        assert w.bars_seen == 29, "29 bars accumulated, 1 missed"
        ds = eng._state.get_day_state("main", "AAPL")
        # 29 >= or_minutes // 2 (15), and width ~1% is inside the band,
        # so the FSM should be ARMED, not BLOCKED.
        assert ds.phase == PHASE_ARMED

    def test_post_window_bar_with_empty_or_does_not_lock(self):
        """v7.73.0 -- defensive: if zero OR bars accumulated (bot was down
        for the ENTIRE OR window), the fallback must NOT lock. There's
        nothing meaningful to lock against.
        """
        cfg = make_config()
        eng = OrbEngine(cfg, portfolio_ids=["main"])
        eng.start_new_session(
            date_iso="2026-01-02", tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        # No in-window bars at all. First bar arrives at 600 (post-OR).
        eng.on_bar_arrival(
            ticker="AAPL", bar_high=102.0, bar_low=101.5,
            bar_open=101.8, bar_close=101.9,
            bar_volume=10000, bar_bucket_min=600,
        )
        w = eng._state.or_windows["AAPL"]
        assert not w.locked, "no bars accumulated -> nothing to lock"
        ds = eng._state.get_day_state("main", "AAPL")
        assert ds.phase == PHASE_WARMUP, (
            "FSM should stay WARMUP rather than transition to a "
            "synthetic ARMED/BLOCKED state on an empty OR"
        )

    def test_already_locked_post_window_bar_is_noop(self):
        """v7.73.0 -- the fallback path must NOT re-lock an already-locked
        window. add_bar returns False for both 'outside window' and
        'already locked'; the fallback gate must distinguish.
        """
        cfg = make_config()
        eng = OrbEngine(cfg, portfolio_ids=["main"])
        eng.start_new_session(
            date_iso="2026-01-02", tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        feed_or_window(eng, "AAPL", or_high=101.0, or_low=100.0)
        w = eng._state.or_windows["AAPL"]
        assert w.locked
        locked_at_iso_before = w.locked_at_iso
        # Post-window bar arrives.
        eng.on_bar_arrival(
            ticker="AAPL", bar_high=102.0, bar_low=101.5,
            bar_open=101.8, bar_close=101.9,
            bar_volume=10000, bar_bucket_min=600,
        )
        # Still locked with the original timestamp -- no re-lock.
        assert w.locked
        assert w.locked_at_iso == locked_at_iso_before


# ------------------ detect_breakout ------------------


class TestDetectBreakout:

    def test_long_breakout_above_or_high(self):
        cfg = make_config()
        eng = OrbEngine(cfg, portfolio_ids=["main"])
        eng.start_new_session(
            date_iso="2026-01-02", tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        feed_or_window(eng, "AAPL", or_high=101.0, or_low=100.0)
        # 5m close at $101.5 (above OR high 101.0)
        sig = eng.detect_breakout(
            portfolio_id="main", ticker="AAPL",
            five_min_close=101.5, five_min_close_iso="2026-01-02T15:05:00+00:00",
            next_open=101.6,
        )
        assert sig is not None
        assert sig.side == "long"
        assert sig.proposed_entry == 101.6
        # Stop is below or_low - 5bp = 100.0 * (1 - 0.0005) = 99.95
        assert abs(sig.proposed_stop - 99.95) < 0.001

    def test_short_breakout_below_or_low(self):
        cfg = make_config()
        eng = OrbEngine(cfg, portfolio_ids=["main"])
        eng.start_new_session(
            date_iso="2026-01-02", tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        feed_or_window(eng, "AAPL", or_high=101.0, or_low=100.0)
        sig = eng.detect_breakout(
            portfolio_id="main", ticker="AAPL",
            five_min_close=99.5, five_min_close_iso="2026-01-02T15:05:00+00:00",
            next_open=99.4,
        )
        assert sig is not None
        assert sig.side == "short"

    def test_no_signal_inside_or_range(self):
        cfg = make_config()
        eng = OrbEngine(cfg, portfolio_ids=["main"])
        eng.start_new_session(
            date_iso="2026-01-02", tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        feed_or_window(eng, "AAPL", or_high=101.0, or_low=100.0)
        sig = eng.detect_breakout(
            portfolio_id="main", ticker="AAPL",
            five_min_close=100.5, five_min_close_iso="2026-01-02T15:05:00+00:00",
            next_open=100.5,
        )
        assert sig is None

    def test_no_signal_when_blocked(self):
        cfg = make_config()
        eng = OrbEngine(cfg, portfolio_ids=["main"])
        eng.start_new_session(
            date_iso="2026-01-02", tickers=["META"],  # blocklisted
            vix_close_d1=18.0,
            ticker_open_today={"META": 500.0},
            ticker_prev_close={"META": 500.0},
            equity_per_portfolio={"main": 100000.0},
        )
        feed_or_window(eng, "META", or_high=505.0, or_low=500.0)
        sig = eng.detect_breakout(
            portfolio_id="main", ticker="META",
            five_min_close=510.0, five_min_close_iso="2026-01-02T15:05:00+00:00",
            next_open=510.5,
        )
        assert sig is None


# ------------------ try_enter / on_exit ------------------


class TestTryEnterOnExit:

    def _setup(self):
        cfg = make_config()
        eng = OrbEngine(cfg, portfolio_ids=["main"])
        eng.start_new_session(
            date_iso="2026-01-02", tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        feed_or_window(eng, "AAPL", or_high=101.0, or_low=100.0)
        return cfg, eng

    def test_admission_creates_position_and_advances_state(self):
        _cfg, eng = self._setup()
        sig = eng.detect_breakout(
            portfolio_id="main", ticker="AAPL",
            five_min_close=101.5, five_min_close_iso="t",
            next_open=101.5,
        )
        adm = eng.try_enter(sig, equity=100000.0)
        assert adm is not None
        assert adm.position.side == "long"
        assert adm.position.entry_price == 101.5
        # FSM transitioned
        ds = eng._state.get_day_state("main", "AAPL")
        assert ds.phase == PHASE_IN_POS
        assert ds.in_position
        # Risk book has open ticket
        rb = eng._risk.get("main")
        assert rb.open_count == 1

    def test_exit_releases_risk_and_advances_to_closed(self):
        _cfg, eng = self._setup()
        sig = eng.detect_breakout(
            portfolio_id="main", ticker="AAPL",
            five_min_close=101.5, five_min_close_iso="t",
            next_open=101.5,
        )
        adm = eng.try_enter(sig, equity=100000.0)
        # Now exit
        from orb.exits import ExitDecision, EXIT_TARGET
        exit_d = ExitDecision(reason=EXIT_TARGET, price=105.0)
        eng.on_exit(adm.position, exit_d, exit_iso="t2")
        ds = eng._state.get_day_state("main", "AAPL")
        assert ds.phase == PHASE_CLOSED
        assert not ds.in_position
        assert ds.trades_today == 1
        rb = eng._risk.get("main")
        assert rb.open_count == 0

    def test_re_entry_after_exit(self):
        """After CLOSED, a new breakout can re-arm + re-enter (up to cap)."""
        _cfg, eng = self._setup()
        sig1 = eng.detect_breakout(portfolio_id="main", ticker="AAPL",
                                    five_min_close=101.5, five_min_close_iso="t1",
                                    next_open=101.5)
        adm1 = eng.try_enter(sig1, equity=100000.0)
        assert adm1 is not None
        from orb.exits import ExitDecision, EXIT_STOP
        eng.on_exit(adm1.position, ExitDecision(reason=EXIT_STOP, price=99.95),
                    exit_iso="t2")
        # CLOSED -> can_enter is True again
        ds = eng._state.get_day_state("main", "AAPL")
        assert ds.phase == PHASE_CLOSED
        assert ds.can_enter(max_trades_per_day=5)
        # Second signal
        sig2 = eng.detect_breakout(portfolio_id="main", ticker="AAPL",
                                    five_min_close=102.0, five_min_close_iso="t3",
                                    next_open=102.0)
        adm2 = eng.try_enter(sig2, equity=100000.0)
        assert adm2 is not None

    def test_max_trades_cap_blocks_after_n(self):
        # Disable the daily-loss kill (v7.29.0) for this test so all 5
        # stop-outs are reached before the cap fires; the kill is
        # tested separately in test_orb_daily_kill.py.
        cfg = make_config(daily_loss_kill_pct=0.0)
        eng = OrbEngine(cfg, portfolio_ids=["main"])
        eng.start_new_session(
            date_iso="2026-01-02", tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        feed_or_window(eng, "AAPL", or_high=101.0, or_low=100.0)
        from orb.exits import ExitDecision, EXIT_STOP
        for i in range(5):
            sig = eng.detect_breakout(portfolio_id="main", ticker="AAPL",
                                       five_min_close=101.5 + i*0.1,
                                       five_min_close_iso=f"t{i}",
                                       next_open=101.5 + i*0.1)
            adm = eng.try_enter(sig, equity=100000.0)
            assert adm is not None, f"admission {i} failed unexpectedly"
            eng.on_exit(adm.position, ExitDecision(reason=EXIT_STOP, price=99.95),
                        exit_iso=f"x{i}")
        # 6th attempt should fail (cap reached)
        ds = eng._state.get_day_state("main", "AAPL")
        assert ds.trades_today == 5
        assert not ds.can_enter(max_trades_per_day=5)
        sig6 = eng.detect_breakout(portfolio_id="main", ticker="AAPL",
                                    five_min_close=102.5,
                                    five_min_close_iso="t6",
                                    next_open=102.5)
        assert sig6 is None  # detect_breakout itself rejects


# ------------------ snapshot ------------------


class TestSnapshot:

    def test_snapshot_shape(self):
        cfg = make_config()
        eng = OrbEngine(cfg, portfolio_ids=["main", "val"])
        eng.start_new_session(
            date_iso="2026-01-02", tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0, "val": 50000.0},
        )
        snap = eng.snapshot()
        assert "config" in snap
        assert "day_status" in snap
        assert "or_windows" in snap
        assert "day_states" in snap
        assert "risk_books" in snap
        # Day status reflects VIX pass
        assert snap["day_status"]["block_day"] is False
        assert snap["day_status"]["vix_d1_close"] == 18.0
        # 2 risk books (one per portfolio)
        assert set(snap["risk_books"].keys()) == {"main", "val"}
        assert snap["risk_books"]["main"]["equity"] == 100000.0
        assert snap["risk_books"]["val"]["equity"] == 50000.0
