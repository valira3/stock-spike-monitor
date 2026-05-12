"""v8.3.15 -- OrbEngine.find_phantom_in_pos + clear_phantom_in_pos tests.

Covers the boot-time consistency sweep that self-heals stale engine FSM
rows. The pair lets the live runtime detect "FSM says IN_POS but
executor doesn't hold the ticker" and clear the bad row without manual
intervention.
"""
from __future__ import annotations

import pytest

from orb.engine import OrbConfig, OrbEngine
from orb.state import (
    PHASE_WARMUP, PHASE_ARMED, PHASE_IN_POS, PHASE_CLOSED,
    PHASE_BLOCKED_BLOCKLIST,
)
import orb.risk_book as _rb_mod


def _cfg():
    return OrbConfig(
        or_minutes=30,
        skip_earnings_window=False,
        fail_closed_on_missing_vix=False,
        ticker_side_blocklist=None,
    )


def _seed_in_pos(engine, pid, ticker, *,
                 risk_dollars=150.0, notional=15000.0,
                 with_synthetic_ticket=True):
    """Helper: mark (pid, ticker) IN_POS with optional synthetic ticket."""
    ds = engine._state.get_day_state(pid, ticker)
    ds.in_position = True
    ds.transition(PHASE_IN_POS)
    if with_synthetic_ticket:
        rb = engine._risk.get(pid)
        if rb is not None:
            ticket_id = f"recover-{pid}-{ticker}"
            with rb._lock:
                rb._open_tickets[ticket_id] = _rb_mod._Ticket(
                    ticket_id=ticket_id,
                    risk_dollars=risk_dollars,
                    notional=notional,
                )
                rb._open_risk += risk_dollars
                rb._open_notional += notional


class TestFindPhantomInPos:

    def test_empty_engine_no_phantoms(self):
        eng = OrbEngine(_cfg(), portfolio_ids=["main", "val"])
        assert eng.find_phantom_in_pos(
            held_tickers_by_pid={"main": set(), "val": set()},
        ) == []

    def test_clean_state_no_phantoms(self):
        """FSM IN_POS for tickers actually held -- no phantoms."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _seed_in_pos(eng, "main", "AAPL")
        result = eng.find_phantom_in_pos(
            held_tickers_by_pid={"main": {"AAPL"}},
        )
        assert result == []

    def test_one_phantom_detected(self):
        """AMZN is marked IN_POS but not in main's held set."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _seed_in_pos(eng, "main", "AMZN")
        result = eng.find_phantom_in_pos(
            held_tickers_by_pid={"main": set()},  # main holds nothing
        )
        assert result == [("main", "AMZN")]

    def test_multiple_phantoms_across_pids(self):
        eng = OrbEngine(_cfg(), portfolio_ids=["main", "val"])
        _seed_in_pos(eng, "main", "AMZN")
        _seed_in_pos(eng, "val", "NFLX")
        _seed_in_pos(eng, "main", "AAPL")  # this one IS held
        result = eng.find_phantom_in_pos(
            held_tickers_by_pid={"main": {"AAPL"}, "val": set()},
        )
        assert set(result) == {("main", "AMZN"), ("val", "NFLX")}

    def test_missing_pid_in_held_map_skipped(self):
        """If we don't have data for a pid (e.g. gene executor not
        bootstrapped), we can't determine phantoms; the row is left
        alone rather than auto-wiped."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main", "gene"])
        _seed_in_pos(eng, "gene", "AAPL")
        result = eng.find_phantom_in_pos(
            held_tickers_by_pid={"main": set()},  # no gene key
        )
        # gene row stays unchecked
        assert result == []

    def test_in_position_false_ignored(self):
        """Rows that aren't IN_POS shouldn't be flagged."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        ds = eng._state.get_day_state("main", "AAPL")
        ds.in_position = False  # default
        ds.phase = PHASE_CLOSED
        result = eng.find_phantom_in_pos(
            held_tickers_by_pid={"main": set()},
        )
        assert result == []


class TestClearPhantomInPos:

    def test_clears_in_position_flag_and_phase(self):
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _seed_in_pos(eng, "main", "AMZN")
        ds_before = eng._state.get_day_state("main", "AMZN")
        assert ds_before.in_position is True
        assert ds_before.phase == PHASE_IN_POS
        cleared = eng.clear_phantom_in_pos("main", "AMZN")
        assert cleared is True
        ds_after = eng._state.get_day_state("main", "AMZN")
        assert ds_after.in_position is False
        assert ds_after.phase == PHASE_CLOSED

    def test_releases_synthetic_ticket(self):
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _seed_in_pos(eng, "main", "AMZN",
                     risk_dollars=200.0, notional=16000.0)
        rb = eng._risk.get("main")
        assert rb.open_count == 1
        assert rb.open_risk == pytest.approx(200.0)
        assert rb.open_notional == pytest.approx(16000.0)
        eng.clear_phantom_in_pos("main", "AMZN")
        assert rb.open_count == 0
        assert rb.open_risk == pytest.approx(0.0)
        assert rb.open_notional == pytest.approx(0.0)

    def test_idempotent(self):
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _seed_in_pos(eng, "main", "AMZN")
        assert eng.clear_phantom_in_pos("main", "AMZN") is True
        # Second call: nothing to clear
        assert eng.clear_phantom_in_pos("main", "AMZN") is False

    def test_doesnt_clobber_blocked_phase(self):
        """If somehow in_position=True AND phase=BLOCKED_BLOCKLIST,
        clearing in_position should NOT transition the phase
        (BLOCKED_* phases stay sticky)."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        ds = eng._state.get_day_state("main", "META")
        ds.transition(PHASE_BLOCKED_BLOCKLIST, reason="test setup")
        ds.in_position = True
        eng.clear_phantom_in_pos("main", "META")
        ds_after = eng._state.get_day_state("main", "META")
        assert ds_after.in_position is False
        # Phase unchanged: BLOCKED_BLOCKLIST stays
        assert ds_after.phase == PHASE_BLOCKED_BLOCKLIST

    def test_no_ticket_still_clears_fsm(self):
        """When the FSM is IN_POS but there's no synthetic ticket
        (e.g. mirrored without ticket, or ticket already released),
        the clear still flips in_position off."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _seed_in_pos(eng, "main", "AMZN", with_synthetic_ticket=False)
        rb = eng._risk.get("main")
        assert rb.open_count == 0
        cleared = eng.clear_phantom_in_pos("main", "AMZN")
        assert cleared is True
        ds = eng._state.get_day_state("main", "AMZN")
        assert ds.in_position is False
        assert ds.phase == PHASE_CLOSED


class TestOperatorScenario:
    """v8.3.15 -- replicates the operator's exact watchdog symptom:
    `main/AMZN: phase='in_pos' in_position=True last_entry=''` while
    main's positions map is empty (AMZN was closed in a prior process,
    but the orb_state_<date>.json snapshot still has IN_POS for it)."""

    def test_amzn_phantom_detected_and_cleared(self):
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        # Simulate v8.3.4 rehydrate landing a stale row.
        _seed_in_pos(eng, "main", "AMZN")
        # Main currently holds NO positions (AMZN closed earlier).
        held = {"main": set()}
        phantoms = eng.find_phantom_in_pos(held_tickers_by_pid=held)
        assert phantoms == [("main", "AMZN")]
        # Caller (scan.py:_orb_phantom_sweep) clears each.
        for pid, tk in phantoms:
            eng.clear_phantom_in_pos(pid, tk)
        # State is clean now
        ds = eng._state.get_day_state("main", "AMZN")
        assert ds.in_position is False
        rb = eng._risk.get("main")
        assert rb.open_count == 0
        # Re-running the sweep returns empty (idempotent)
        assert eng.find_phantom_in_pos(held_tickers_by_pid=held) == []
