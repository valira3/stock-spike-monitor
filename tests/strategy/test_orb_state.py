"""Tests for orb.state -- OR window + per-portfolio FSM."""
from __future__ import annotations

import pytest

from orb.state import (
    OrWindow,
    TickerDayState,
    OrbStateRegistry,
    PHASE_WARMUP,
    PHASE_OR_LOCKED,
    PHASE_ARMED,
    PHASE_IN_POS,
    PHASE_CLOSED,
    PHASE_BLOCKED_VIX,
    PHASE_BLOCKED_EARNINGS,
    PHASE_BLOCKED_GAP,
    PHASE_BLOCKED_BLOCKLIST,
    ALL_BLOCKED_PHASES,
)


# -------------------- OrWindow --------------------

class TestOrWindow:

    def test_initial_state_is_empty(self):
        w = OrWindow(ticker="AAPL", or_minutes=30)
        assert w.or_high is None
        assert w.or_low is None
        assert w.or_open is None
        assert w.or_close is None
        assert w.or_volume == 0.0
        assert w.bars_seen == 0
        assert not w.locked
        assert w.or_width_pct is None

    def test_first_bar_sets_open_and_initializes_high_low(self):
        w = OrWindow(ticker="AAPL", or_minutes=30)
        # 09:30 = 570 minutes; or_end at 30-min OR = 600
        added = w.add_bar(bar_high=100.5, bar_low=99.8, bar_open=100.0,
                          bar_close=100.2, bar_volume=10000,
                          bar_bucket_min=570, or_end_min=600)
        assert added
        assert w.or_open == 100.0
        assert w.or_high == 100.5
        assert w.or_low == 99.8
        assert w.or_close == 100.2
        assert w.or_volume == 10000
        assert w.bars_seen == 1

    def test_subsequent_bars_update_high_low_close_volume(self):
        w = OrWindow(ticker="AAPL", or_minutes=30)
        w.add_bar(100.5, 99.8, 100.0, 100.2, 10000, 570, 600)
        w.add_bar(101.0, 99.5, 100.2, 100.8, 5000, 571, 600)
        assert w.or_open == 100.0           # unchanged
        assert w.or_high == 101.0           # updated up
        assert w.or_low == 99.5             # updated down
        assert w.or_close == 100.8          # always last
        assert w.or_volume == 15000
        assert w.bars_seen == 2

    def test_bar_at_or_end_is_rejected(self):
        """Half-open window [09:30, 10:00). The 10:00 bar belongs to post-OR."""
        w = OrWindow(ticker="AAPL", or_minutes=30)
        added = w.add_bar(100.5, 99.8, 100.0, 100.2, 10000,
                          bar_bucket_min=600, or_end_min=600)
        assert not added
        assert w.bars_seen == 0

    def test_bar_after_or_end_is_rejected(self):
        w = OrWindow(ticker="AAPL", or_minutes=30)
        added = w.add_bar(100.5, 99.8, 100.0, 100.2, 10000,
                          bar_bucket_min=605, or_end_min=600)
        assert not added

    def test_lock_freezes_window(self):
        w = OrWindow(ticker="AAPL", or_minutes=30)
        w.add_bar(100.5, 99.8, 100.0, 100.2, 10000, 570, 600)
        w.lock("2026-01-02T15:00:00+00:00")
        assert w.locked
        assert w.locked_at_iso == "2026-01-02T15:00:00+00:00"
        # After lock, add_bar must reject
        added = w.add_bar(102.0, 99.0, 100.5, 101.5, 50000, 595, 600)
        assert not added
        assert w.or_high == 100.5  # unchanged from before lock
        assert w.bars_seen == 1

    def test_or_width_pct_only_after_lock(self):
        w = OrWindow(ticker="AAPL", or_minutes=30)
        w.add_bar(101.0, 99.0, 100.0, 100.5, 10000, 570, 600)
        # Not locked yet
        assert w.or_width_pct is None
        w.lock("2026-01-02T15:00:00+00:00")
        # Width = (101 - 99) / 100 = 0.02 = 2%
        assert abs(w.or_width_pct - 0.02) < 1e-9

    def test_or_width_pct_handles_zero_mid(self):
        w = OrWindow(ticker="WEIRD", or_minutes=30)
        w.or_high = 0.0
        w.or_low = 0.0
        w.locked = True
        assert w.or_width_pct is None


# -------------------- TickerDayState --------------------

class TestTickerDayState:

    def test_initial_phase_is_warmup_not_blocked(self):
        s = TickerDayState(portfolio_id="main", ticker="AAPL")
        assert s.phase == PHASE_WARMUP
        assert not s.is_blocked()
        assert not s.in_position
        assert s.trades_today == 0

    def test_can_enter_only_in_armed_or_closed(self):
        s = TickerDayState(portfolio_id="main", ticker="AAPL")
        assert not s.can_enter(max_trades_per_day=5)  # warmup
        s.transition(PHASE_ARMED)
        assert s.can_enter(max_trades_per_day=5)
        s.in_position = True
        assert not s.can_enter(max_trades_per_day=5)  # in_position blocks
        s.in_position = False
        s.trades_today = 5
        assert not s.can_enter(max_trades_per_day=5)  # cap reached
        s.trades_today = 4
        assert s.can_enter(max_trades_per_day=5)
        s.transition(PHASE_CLOSED)
        assert s.can_enter(max_trades_per_day=5)
        s.transition(PHASE_BLOCKED_VIX, reason="vix_high")
        assert s.is_blocked()
        assert not s.can_enter(max_trades_per_day=5)

    def test_blocked_phases_set_block_reason(self):
        s = TickerDayState(portfolio_id="main", ticker="META")
        s.transition(PHASE_BLOCKED_BLOCKLIST, reason="blocklist_long_short")
        assert s.is_blocked()
        assert s.block_reason == "blocklist_long_short"

    def test_unblocking_clears_block_reason(self):
        s = TickerDayState(portfolio_id="main", ticker="AAPL")
        s.transition(PHASE_BLOCKED_GAP, reason="gap (3.0% > 1.5%)")
        assert s.block_reason
        s.transition(PHASE_ARMED)
        assert s.block_reason == ""


# -------------------- OrbStateRegistry --------------------

class TestOrbStateRegistry:

    def test_get_or_window_lazy_create(self):
        r = OrbStateRegistry()
        w = r.get_or_window("AAPL", or_minutes=30)
        assert w.ticker == "AAPL"
        assert w.or_minutes == 30
        assert "AAPL" in r.or_windows
        # Second call returns same instance
        w2 = r.get_or_window("AAPL", or_minutes=30)
        assert w is w2

    def test_get_day_state_lazy_create_per_portfolio(self):
        r = OrbStateRegistry()
        s_main = r.get_day_state("main", "AAPL")
        s_val = r.get_day_state("val", "AAPL")
        s_gene = r.get_day_state("gene", "AAPL")
        assert s_main is not s_val
        assert s_main is not s_gene
        assert s_val is not s_gene
        # Same portfolio + same ticker = same state
        s_main2 = r.get_day_state("main", "AAPL")
        assert s_main is s_main2

    def test_independent_phase_per_portfolio(self):
        r = OrbStateRegistry()
        s_main = r.get_day_state("main", "AAPL")
        s_val = r.get_day_state("val", "AAPL")
        s_main.transition(PHASE_IN_POS)
        s_val.transition(PHASE_ARMED)
        assert s_main.phase == PHASE_IN_POS
        assert s_val.phase == PHASE_ARMED

    def test_lock_all_or_windows(self):
        r = OrbStateRegistry()
        for tk in ("AAPL", "NVDA", "TSLA"):
            w = r.get_or_window(tk, 30)
            w.add_bar(100.0, 99.0, 99.5, 99.8, 10000, 570, 600)
        r.lock_all_or_windows("2026-01-02T15:00:00+00:00")
        for tk in ("AAPL", "NVDA", "TSLA"):
            assert r.or_windows[tk].locked

    def test_reset_for_new_session_clears_state(self):
        r = OrbStateRegistry()
        r.get_or_window("AAPL", 30).add_bar(100.0, 99.0, 99.5, 99.8, 10000, 570, 600)
        r.get_day_state("main", "AAPL").trades_today = 3
        r.reset_for_new_session("2026-01-03")
        assert "AAPL" not in r.or_windows
        assert ("main", "AAPL") not in r.day_states
        assert r.session_date == "2026-01-03"

    def test_reset_idempotent_same_date(self):
        r = OrbStateRegistry()
        r.reset_for_new_session("2026-01-02")
        r.get_or_window("AAPL", 30).add_bar(100.0, 99.0, 99.5, 99.8, 10000, 570, 600)
        # Same date should NOT clear
        r.reset_for_new_session("2026-01-02")
        assert "AAPL" in r.or_windows

    def test_snapshot_or_windows_shape(self):
        r = OrbStateRegistry()
        w = r.get_or_window("AAPL", 30)
        w.add_bar(101.0, 99.0, 100.0, 100.5, 10000, 570, 600)
        w.lock("2026-01-02T15:00:00+00:00")
        snap = r.snapshot_or_windows()
        assert "AAPL" in snap
        assert snap["AAPL"]["or_high"] == 101.0
        assert snap["AAPL"]["or_low"] == 99.0
        assert snap["AAPL"]["locked"] is True
        assert abs(snap["AAPL"]["or_width_pct"] - 0.02) < 1e-9

    def test_snapshot_day_states_shape(self):
        r = OrbStateRegistry()
        r.get_day_state("main", "AAPL").transition(PHASE_ARMED)
        r.get_day_state("val", "AAPL").transition(PHASE_BLOCKED_GAP, reason="gap (2.0% > 1.5%)")
        snap = r.snapshot_day_states()
        assert len(snap) == 2
        # Sort for stable comparison
        snap.sort(key=lambda d: d["portfolio_id"])
        assert snap[0]["portfolio_id"] == "main"
        assert snap[0]["phase"] == "armed"
        assert snap[1]["portfolio_id"] == "val"
        assert snap[1]["phase"] == "blocked_gap"
        assert "gap" in snap[1]["block_reason"]


# -------------------- Phase constants --------------------

class TestPhaseConstants:

    def test_all_blocked_phases_subset(self):
        assert PHASE_BLOCKED_VIX in ALL_BLOCKED_PHASES
        assert PHASE_BLOCKED_EARNINGS in ALL_BLOCKED_PHASES
        assert PHASE_BLOCKED_GAP in ALL_BLOCKED_PHASES
        assert PHASE_BLOCKED_BLOCKLIST in ALL_BLOCKED_PHASES
        assert PHASE_WARMUP not in ALL_BLOCKED_PHASES
        assert PHASE_ARMED not in ALL_BLOCKED_PHASES
        assert PHASE_IN_POS not in ALL_BLOCKED_PHASES
        assert PHASE_CLOSED not in ALL_BLOCKED_PHASES
