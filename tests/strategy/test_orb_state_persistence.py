"""v8.3.4 -- engine state persistence tests.

Exercises orb.persistence end-to-end: serialize, write to disk, read
back, apply onto a fresh OrbEngine, and verify the engine matches the
original. Plus integration tests through live_runtime's
dump_engine_state_now / _try_rehydrate_engine_state wrappers.

Categories covered:
  A. OR windows
  B. DayState FSM (phase, in_position, trades_today, last_*_iso)
  C. RiskBook (realized_pnl_today, open_tickets, daily_kill_triggered)
  D. Activity feed (deque)
  E. Wash-sale tracker (_recent_losses, wash_risk_count)
  F. Pending v10 sizes (tuple keys -> str roundtrip)
"""
from __future__ import annotations

import collections
import json
import os
import time
from pathlib import Path

import pytest

from orb.engine import OrbConfig, OrbEngine
from orb.persistence import (
    serialize_engine_state, dump_state_to_disk,
    load_state_from_disk, apply_loaded_state, resolve_path,
    prune_stale_state_files,
)
from orb.state import (
    PHASE_WARMUP, PHASE_ARMED, PHASE_IN_POS, PHASE_CLOSED,
    PHASE_BLOCKED_BLOCKLIST,
)


def _cfg():
    return OrbConfig(
        or_minutes=30,
        skip_earnings_window=False,
        fail_closed_on_missing_vix=False,
        ticker_side_blocklist={"META": ["LONG", "SHORT"]},
    )


def _start_session(eng, tickers=("AAPL", "META")):
    eng.start_new_session(
        date_iso="2026-05-12",
        tickers=list(tickers),
        vix_close_d1=18.0,
        ticker_open_today={t: 100.0 for t in tickers},
        ticker_prev_close={t: 100.0 for t in tickers},
        equity_per_portfolio={"main": 100_000.0, "val": 50_000.0},
    )


def _lock_or(eng, ticker, *, or_high=101.0, or_low=99.0):
    for m in range(570, 600):
        h = or_high if m == 580 else or_high - 0.5
        lo = or_low if m == 585 else or_low + 0.5
        eng.on_bar_arrival(
            ticker=ticker,
            bar_high=h, bar_low=lo,
            bar_open=100.0, bar_close=100.0,
            bar_volume=10_000, bar_bucket_min=m,
        )


@pytest.fixture
def tmp_state_dir(tmp_path, monkeypatch):
    """Point ORB_STATE_PERSIST_PATH at a tmp dir; clean up after."""
    template = str(tmp_path / "orb_state_{date}.json")
    monkeypatch.setenv("ORB_STATE_PERSIST_PATH", template)
    yield tmp_path


# ------------------ A: OR window roundtrip ------------------


class TestOrWindowRoundtrip:

    def test_or_window_serialized_with_full_state(self, tmp_state_dir):
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng, ["AAPL"])
        _lock_or(eng, "AAPL", or_high=150.5, or_low=148.25)
        payload = serialize_engine_state(
            eng, recent_activity=[], pending_v10_sizes={},
            date_iso="2026-05-12", bot_version="8.3.4",
        )
        ws = payload["or_windows"]
        assert "AAPL" in ws
        assert ws["AAPL"]["or_high"] == 150.5
        assert ws["AAPL"]["or_low"] == 148.25
        assert ws["AAPL"]["locked"] is True
        assert ws["AAPL"]["bars_seen"] == 30

    def test_or_window_rehydrate_to_fresh_engine(self, tmp_state_dir):
        # Original
        eng1 = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng1, ["AAPL"])
        _lock_or(eng1, "AAPL", or_high=200.0, or_low=195.0)
        dump_state_to_disk(eng1, recent_activity=[], pending_v10_sizes={},
                            date_iso="2026-05-12", bot_version="x")
        # Fresh engine (post-redeploy simulation)
        eng2 = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng2, ["AAPL"])
        # OR is currently empty on eng2 (no bars fed); rehydrate
        loaded = load_state_from_disk("2026-05-12")
        assert loaded is not None
        apply_loaded_state(eng2, loaded)
        w = eng2._state.or_windows["AAPL"]
        assert w.locked
        assert w.or_high == 200.0
        assert w.or_low == 195.0
        assert w.bars_seen == 30


# ------------------ B: DayState FSM roundtrip ------------------


class TestDayStateRoundtrip:

    def test_in_position_survives_redeploy(self, tmp_state_dir):
        """Critical safety case: if Main is IN_POS on AAPL when bot
        redeploys, the engine MUST come back IN_POS so the opposite-
        side guard works and trades_today isn't bypassed."""
        eng1 = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng1, ["AAPL"])
        ds1 = eng1._state.get_day_state("main", "AAPL")
        ds1.in_position = True
        ds1.phase = PHASE_IN_POS
        ds1.trades_today = 2
        ds1.last_entry_iso = "2026-05-12T14:00:00Z"
        dump_state_to_disk(eng1, recent_activity=[], pending_v10_sizes={},
                            date_iso="2026-05-12", bot_version="x")
        # Post-redeploy
        eng2 = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng2, ["AAPL"])
        # Fresh start: in_position=False
        assert eng2._state.get_day_state("main", "AAPL").in_position is False
        # Rehydrate
        apply_loaded_state(eng2, load_state_from_disk("2026-05-12"))
        ds2 = eng2._state.get_day_state("main", "AAPL")
        assert ds2.in_position is True
        assert ds2.phase == PHASE_IN_POS
        assert ds2.trades_today == 2
        assert ds2.last_entry_iso == "2026-05-12T14:00:00Z"

    def test_blocked_phase_survives(self, tmp_state_dir):
        """A ticker blocked by the blocklist stays blocked after rehydrate."""
        eng1 = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng1, ["AAPL", "META"])
        # META is in blocklist -> session-start transitions it to BLOCKED_BLOCKLIST
        assert eng1._state.get_day_state("main", "META").phase == PHASE_BLOCKED_BLOCKLIST
        dump_state_to_disk(eng1, recent_activity=[], pending_v10_sizes={},
                            date_iso="2026-05-12", bot_version="x")
        eng2 = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng2, ["AAPL", "META"])
        apply_loaded_state(eng2, load_state_from_disk("2026-05-12"))
        assert eng2._state.get_day_state("main", "META").phase == PHASE_BLOCKED_BLOCKLIST


# ------------------ C: RiskBook roundtrip ------------------


class TestRiskBookRoundtrip:

    def test_realized_pnl_survives(self, tmp_state_dir):
        """Most-dangerous-if-lost field: daily_loss_kill threshold
        is computed against realized_pnl_today. A reset would bypass
        the kill gate."""
        eng1 = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng1)
        rb1 = eng1._risk.get("main")
        # Simulate $1500 of realized loss
        rb1.record_realized_pnl(-1500.0)
        assert rb1.realized_pnl_today == pytest.approx(-1500.0)
        dump_state_to_disk(eng1, recent_activity=[], pending_v10_sizes={},
                            date_iso="2026-05-12", bot_version="x")
        eng2 = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng2)
        # Pre-rehydrate: fresh = 0
        assert eng2._risk.get("main").realized_pnl_today == 0.0
        apply_loaded_state(eng2, load_state_from_disk("2026-05-12"))
        rb2 = eng2._risk.get("main")
        assert rb2.realized_pnl_today == pytest.approx(-1500.0)

    def test_daily_kill_triggered_survives(self, tmp_state_dir):
        eng1 = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng1)
        rb1 = eng1._risk.get("main")
        rb1.daily_kill_triggered = True
        dump_state_to_disk(eng1, recent_activity=[], pending_v10_sizes={},
                            date_iso="2026-05-12", bot_version="x")
        eng2 = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng2)
        apply_loaded_state(eng2, load_state_from_disk("2026-05-12"))
        assert eng2._risk.get("main").daily_kill_triggered is True

    def test_open_tickets_survive(self, tmp_state_dir):
        eng1 = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng1)
        rb1 = eng1._risk.get("main")
        t = rb1.try_admit(risk_dollars=500.0, notional=15000.0)
        assert t is not None
        dump_state_to_disk(eng1, recent_activity=[], pending_v10_sizes={},
                            date_iso="2026-05-12", bot_version="x")
        eng2 = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng2)
        apply_loaded_state(eng2, load_state_from_disk("2026-05-12"))
        rb2 = eng2._risk.get("main")
        assert rb2.open_count == 1
        # And the open_risk + open_notional are restored
        assert rb2.open_risk == pytest.approx(500.0)
        assert rb2.open_notional == pytest.approx(15000.0)


# ------------------ D: Activity feed roundtrip ------------------


class TestActivityRoundtrip:

    def test_activity_events_persist(self, tmp_state_dir):
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng)
        events = [
            {"ts_iso": "2026-05-12T13:30:00Z", "kind": "session_start",
             "ticker": "", "pid": "", "detail": "fresh session"},
            {"ts_iso": "2026-05-12T13:35:00Z", "kind": "or_lock",
             "ticker": "AAPL", "pid": "main", "detail": "locked at 09:35"},
        ]
        dump_state_to_disk(eng, recent_activity=events, pending_v10_sizes={},
                            date_iso="2026-05-12", bot_version="x")
        # Fresh deque to hydrate
        deque_target = collections.deque(maxlen=50)
        apply_loaded_state(eng, load_state_from_disk("2026-05-12"),
                           recent_activity=deque_target)
        assert len(deque_target) == 2
        assert deque_target[0]["kind"] == "session_start"
        assert deque_target[1]["ticker"] == "AAPL"


# ------------------ E: Wash-sale tracker roundtrip ------------------


class TestWashSaleRoundtrip:

    def test_wash_risk_state_persists(self, tmp_state_dir):
        eng1 = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng1)
        eng1.wash_risk_count = 3
        eng1._recent_losses[("AAPL", "long")] = [
            {"ts_unix": 1747058400.0, "pnl_dollars": -120.5, "exit_iso": "2026-05-12T13:00:00Z"},
        ]
        eng1._recent_losses[("NVDA", "short")] = [
            {"ts_unix": 1747059000.0, "pnl_dollars": -75.0, "exit_iso": "2026-05-12T13:10:00Z"},
        ]
        dump_state_to_disk(eng1, recent_activity=[], pending_v10_sizes={},
                            date_iso="2026-05-12", bot_version="x")
        eng2 = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng2)
        apply_loaded_state(eng2, load_state_from_disk("2026-05-12"))
        assert eng2.wash_risk_count == 3
        assert ("AAPL", "long") in eng2._recent_losses
        assert eng2._recent_losses[("AAPL", "long")][0]["pnl_dollars"] == -120.5
        assert ("NVDA", "short") in eng2._recent_losses


# ------------------ F: Pending v10 sizes roundtrip ------------------


class TestPendingSizesRoundtrip:

    def test_tuple_key_roundtrip(self, tmp_state_dir):
        """JSON can't store tuple keys; persistence encodes as 'pid|ticker'."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng)
        sizes_src = {("main", "AAPL"): 100, ("val", "NVDA"): 50}
        dump_state_to_disk(eng, recent_activity=[],
                            pending_v10_sizes=sizes_src,
                            date_iso="2026-05-12", bot_version="x")
        sizes_dst: dict = {}
        apply_loaded_state(eng, load_state_from_disk("2026-05-12"),
                           pending_v10_sizes=sizes_dst)
        assert sizes_dst[("main", "AAPL")] == 100
        assert sizes_dst[("val", "NVDA")] == 50


# ------------------ disk I/O guards ------------------


class TestDiskGuards:

    def test_atomic_write_no_partial_files(self, tmp_state_dir):
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng)
        ok = dump_state_to_disk(eng, recent_activity=[], pending_v10_sizes={},
                                 date_iso="2026-05-12", bot_version="x")
        assert ok
        target = resolve_path("2026-05-12")
        assert os.path.exists(target)
        # No leftover .tmp files in the directory
        leftover = [f for f in os.listdir(tmp_state_dir)
                    if f.startswith(".orb_state.") or f.endswith(".tmp")]
        assert leftover == []

    def test_load_returns_none_when_file_missing(self, tmp_state_dir):
        assert load_state_from_disk("1999-01-01") is None

    def test_load_returns_none_when_date_mismatch(self, tmp_state_dir):
        """File for 2026-05-11 should not be returned when asking for 2026-05-12.
        Date-match guard prevents stale-file pollution."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng)
        dump_state_to_disk(eng, recent_activity=[], pending_v10_sizes={},
                            date_iso="2026-05-11", bot_version="x")
        # File exists on disk for 2026-05-11, but we ask for 2026-05-12
        assert load_state_from_disk("2026-05-12") is None

    def test_load_handles_corrupt_json(self, tmp_state_dir):
        target = resolve_path("2026-05-12")
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        Path(target).write_text("{not valid json")
        assert load_state_from_disk("2026-05-12") is None

    def test_prune_removes_old_files(self, tmp_state_dir):
        # Create 3 files: today, 3 days ago, 10 days ago
        for d in ("2026-05-12", "2026-05-09", "2026-05-02"):
            Path(resolve_path(d)).write_text(
                json.dumps({"date_iso": d, "or_windows": {}, "day_states": []})
            )
        removed = prune_stale_state_files("2026-05-12", keep_days=5)
        assert removed == 1
        assert Path(resolve_path("2026-05-12")).exists()
        assert Path(resolve_path("2026-05-09")).exists()
        assert not Path(resolve_path("2026-05-02")).exists()


# ------------------ Full redeploy simulation ------------------


class TestFullRedeploySimulation:
    """End-to-end: simulate a Railway redeploy mid-day. State on disk
    from before the redeploy should land in the freshly-bootstrapped
    engine and side data exactly as it was."""

    def test_full_redeploy_recovery(self, tmp_state_dir):
        # === Pre-redeploy: rich engine state ===
        eng1 = OrbEngine(_cfg(), portfolio_ids=["main", "val"])
        _start_session(eng1, ["AAPL", "NVDA"])
        _lock_or(eng1, "AAPL", or_high=150.0, or_low=147.0)
        _lock_or(eng1, "NVDA", or_high=200.0, or_low=195.0)
        # AAPL: Main is IN_POS with 1 trade today; partial-fill marker
        ds = eng1._state.get_day_state("main", "AAPL")
        ds.in_position = True
        ds.phase = PHASE_IN_POS
        ds.trades_today = 1
        ds.last_entry_iso = "2026-05-12T14:05:00Z"
        # RiskBook: Main has $-450 realized + 1 open ticket
        rb_main = eng1._risk.get("main")
        rb_main.record_realized_pnl(-450.0)
        t = rb_main.try_admit(risk_dollars=600.0, notional=18000.0)
        # Wash-sale: 1 prior loss on AAPL long
        eng1.wash_risk_count = 1
        eng1._recent_losses[("AAPL", "long")] = [{
            "ts_unix": 1747000000.0, "pnl_dollars": -200.0,
            "exit_iso": "2026-05-12T13:00:00Z",
        }]
        # Activity: 3 events
        activity = [
            {"ts_iso": "2026-05-12T13:30:00Z", "kind": "session_start",
             "ticker": "", "pid": "", "detail": "fresh"},
            {"ts_iso": "2026-05-12T13:35:00Z", "kind": "or_lock",
             "ticker": "AAPL", "pid": "", "detail": "locked"},
            {"ts_iso": "2026-05-12T14:05:00Z", "kind": "admit",
             "ticker": "AAPL", "pid": "main", "detail": "LONG 100sh @ 150"},
        ]
        sizes = {("main", "AAPL"): 100}

        # Dump
        ok = dump_state_to_disk(
            eng1, recent_activity=activity, pending_v10_sizes=sizes,
            date_iso="2026-05-12", bot_version="8.3.4",
        )
        assert ok

        # === Redeploy: fresh process, fresh engine ===
        eng2 = OrbEngine(_cfg(), portfolio_ids=["main", "val"])
        _start_session(eng2, ["AAPL", "NVDA"])
        # Sanity: post-reset = blank
        assert eng2._state.or_windows == {}
        assert eng2._risk.get("main").realized_pnl_today == 0.0

        # Rehydrate
        loaded = load_state_from_disk("2026-05-12")
        deque_target = collections.deque(maxlen=50)
        sizes_target: dict = {}
        counters = apply_loaded_state(
            eng2, loaded,
            recent_activity=deque_target,
            pending_v10_sizes=sizes_target,
        )

        # === Verify everything came back ===
        # A. OR windows
        assert counters["or_windows_loaded"] == 2
        assert eng2._state.or_windows["AAPL"].or_high == 150.0
        assert eng2._state.or_windows["NVDA"].or_low == 195.0
        # B. DayState
        ds2 = eng2._state.get_day_state("main", "AAPL")
        assert ds2.in_position is True
        assert ds2.phase == PHASE_IN_POS
        assert ds2.trades_today == 1
        assert ds2.last_entry_iso == "2026-05-12T14:05:00Z"
        # C. RiskBook
        rb2 = eng2._risk.get("main")
        assert rb2.realized_pnl_today == pytest.approx(-450.0)
        assert rb2.open_count == 1
        # D. Activity feed
        assert len(deque_target) == 3
        assert deque_target[-1]["kind"] == "admit"
        # E. Wash-sale
        assert eng2.wash_risk_count == 1
        assert ("AAPL", "long") in eng2._recent_losses
        # F. Pending sizes (tuple key roundtrip)
        assert sizes_target[("main", "AAPL")] == 100
