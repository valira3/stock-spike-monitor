"""v8.3.20 -- OrbEngine.find_phantom_recover_tickets +
release_recover_ticket tests.

v8.3.15's sweep only catches FSM rows where `in_position=True`. But
v8.3.12's `_unmirror_position_from_engine` does the cleanup in two
steps (flip in_position, then pop ticket); v8.3.4's per-cycle dump
can fire between them so the persisted snapshot ends up with
in_position=False AND a still-present recover ticket. On the next
boot, v8.3.4 rehydrate restores that inconsistent state and v8.3.15
misses it. The leaked ticket consumes open_risk + open_notional
forever, surfacing as the watchdog `no_phantom_positions` invariant
("main has 1 position but RiskBook open_count=4") AND blocking new
entries with risk_reject:notional_cap.

v8.3.20 adds a second-level sweep targeting orphan `recover-*`
tickets regardless of FSM in_position state.
"""
from __future__ import annotations

import pytest

from orb.engine import OrbConfig, OrbEngine
import orb.risk_book as _rb_mod


def _cfg():
    return OrbConfig(
        or_minutes=30,
        skip_earnings_window=False,
        fail_closed_on_missing_vix=False,
        ticker_side_blocklist=None,
    )


def _insert_recover_ticket(eng, pid, ticker,
                           *, risk_dollars=150.0, notional=15000.0):
    """Helper: insert a `recover-{pid}-{ticker}` ticket into RiskBook
    WITHOUT flipping in_position=True (simulates the post-rehydrate
    orphan state)."""
    rb = eng._risk.get(pid)
    if rb is None:
        return None
    ticket_id = f"recover-{pid}-{ticker}"
    with rb._lock:
        rb._open_tickets[ticket_id] = _rb_mod._Ticket(
            ticket_id=ticket_id,
            risk_dollars=risk_dollars,
            notional=notional,
        )
        rb._open_risk += risk_dollars
        rb._open_notional += notional
    return ticket_id


class TestFindPhantomRecoverTickets:

    def test_empty_state_no_phantoms(self):
        eng = OrbEngine(_cfg(), portfolio_ids=["main", "val"])
        assert eng.find_phantom_recover_tickets(
            held_tickers_by_pid={"main": set(), "val": set()},
        ) == []

    def test_one_orphan_ticket_detected(self):
        """Operator's scenario: recover-main-AMZN ticket exists in
        RiskBook but AMZN is not in main's held set."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _insert_recover_ticket(eng, "main", "AMZN")
        result = eng.find_phantom_recover_tickets(
            held_tickers_by_pid={"main": set()},
        )
        assert result == [("main", "recover-main-AMZN", "AMZN")]

    def test_clean_state_no_phantoms_when_ticker_held(self):
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _insert_recover_ticket(eng, "main", "AAPL")
        result = eng.find_phantom_recover_tickets(
            held_tickers_by_pid={"main": {"AAPL"}},
        )
        assert result == []

    def test_multiple_orphans_across_pids(self):
        eng = OrbEngine(_cfg(), portfolio_ids=["main", "val"])
        _insert_recover_ticket(eng, "main", "AMZN")
        _insert_recover_ticket(eng, "main", "NFLX")
        _insert_recover_ticket(eng, "val", "NVDA")
        _insert_recover_ticket(eng, "main", "AAPL")  # this one IS held
        result = eng.find_phantom_recover_tickets(
            held_tickers_by_pid={"main": {"AAPL"}, "val": set()},
        )
        # AAPL is held by main -> NOT a phantom; the other 3 are
        assert sorted(result) == sorted([
            ("main", "recover-main-AMZN", "AMZN"),
            ("main", "recover-main-NFLX", "NFLX"),
            ("val", "recover-val-NVDA", "NVDA"),
        ])

    def test_non_recover_tickets_ignored(self):
        """uuid-style tickets from try_admit shouldn't be flagged --
        we can't safely map them back to a ticker without v7.81.0
        rollback tracking."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        rb = eng._risk.get("main")
        with rb._lock:
            rb._open_tickets["abc123def456"] = _rb_mod._Ticket(
                ticket_id="abc123def456",
                risk_dollars=100.0, notional=10000.0,
            )
            rb._open_risk += 100.0
            rb._open_notional += 10000.0
        result = eng.find_phantom_recover_tickets(
            held_tickers_by_pid={"main": set()},
        )
        assert result == []  # uuid ticket not touched

    def test_missing_pid_in_held_map_skipped(self):
        """Defensive: if held data isn't available for a pid (e.g.
        gene executor not bootstrapped), don't touch its tickets."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main", "gene"])
        _insert_recover_ticket(eng, "gene", "AAPL")
        result = eng.find_phantom_recover_tickets(
            held_tickers_by_pid={"main": set()},  # no gene
        )
        assert result == []


class TestReleaseRecoverTicket:

    def test_releases_ticket_and_decrements_risk_notional(self):
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _insert_recover_ticket(eng, "main", "AMZN",
                               risk_dollars=200.0, notional=20000.0)
        rb = eng._risk.get("main")
        assert rb.open_count == 1
        assert rb.open_risk == pytest.approx(200.0)
        assert rb.open_notional == pytest.approx(20000.0)
        assert eng.release_recover_ticket("main", "recover-main-AMZN") is True
        assert rb.open_count == 0
        assert rb.open_risk == pytest.approx(0.0)
        assert rb.open_notional == pytest.approx(0.0)

    def test_missing_ticket_returns_false(self):
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        assert eng.release_recover_ticket("main", "recover-main-nonexistent") is False

    def test_unknown_pid_returns_false(self):
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        assert eng.release_recover_ticket("alice", "recover-alice-AMZN") is False

    def test_clamps_negative_risk_notional(self):
        """Defensive: rounding could push risk/notional slightly
        negative on edge cases. Clamp to 0."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _insert_recover_ticket(eng, "main", "AMZN",
                               risk_dollars=100.0, notional=10000.0)
        rb = eng._risk.get("main")
        # Manually corrupt the running totals to simulate fp drift
        with rb._lock:
            rb._open_risk = 99.9999  # slightly less than ticket value
            rb._open_notional = 9999.999
        eng.release_recover_ticket("main", "recover-main-AMZN")
        assert rb.open_risk == 0.0
        assert rb.open_notional == 0.0


class TestOperatorScenario:
    """v8.3.20 -- operator's exact watchdog symptom: 'main has 1
    position in /api/state but RiskBook reports open_count=4' AND
    risk_reject:notional_cap rejects (would-be $300k > $202k cap)
    because 3 phantom tickets eat 3 x ~$43k of notional headroom."""

    def test_recover_three_phantoms_unblocks_cap(self):
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        # 1 real position (held), 3 phantom recover tickets
        _insert_recover_ticket(eng, "main", "GOOG",
                               risk_dollars=190.0, notional=73000.0)
        _insert_recover_ticket(eng, "main", "AMZN",
                               risk_dollars=150.0, notional=43000.0)
        _insert_recover_ticket(eng, "main", "NFLX",
                               risk_dollars=130.0, notional=43000.0)
        _insert_recover_ticket(eng, "main", "AAPL",
                               risk_dollars=120.0, notional=43000.0)
        rb = eng._risk.get("main")
        # Pre-sweep: 4 tickets, notional ~$202K (matches operator's bug)
        assert rb.open_count == 4
        assert rb.open_notional == pytest.approx(202000.0)
        # Run the sweep -- main holds only GOOG
        phantoms = eng.find_phantom_recover_tickets(
            held_tickers_by_pid={"main": {"GOOG"}},
        )
        # 3 phantoms: AMZN, NFLX, AAPL
        assert len(phantoms) == 3
        for pid, tid, ticker in phantoms:
            assert eng.release_recover_ticket(pid, tid) is True
        # Post-sweep: 1 ticket (GOOG real), notional ~$73K
        assert rb.open_count == 1
        assert rb.open_notional == pytest.approx(73000.0)
        # The notional budget freed up; future admits up to
        # max_notional - $73K should now succeed instead of being
        # rejected with risk_reject:notional_cap.

    def test_v8315_and_v8320_complementary(self):
        """v8.3.15 catches phantoms where in_position=True; v8.3.20
        catches the in_position=False orphans. Together they cover
        both directions of state drift."""
        from orb.state import PHASE_IN_POS
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        # Setup A: FSM in_position=True + ticket (v8.3.15 territory)
        ds_a = eng._state.get_day_state("main", "PHA")
        ds_a.in_position = True
        ds_a.transition(PHASE_IN_POS)
        _insert_recover_ticket(eng, "main", "PHA")
        # Setup B: FSM in_position=False + ticket (v8.3.20 territory)
        _insert_recover_ticket(eng, "main", "PHB")
        # held set: only "X" (PHA and PHB are both phantom)
        held = {"main": {"X"}}
        # v8.3.15 catches PHA only
        in_pos_phantoms = eng.find_phantom_in_pos(held_tickers_by_pid=held)
        assert in_pos_phantoms == [("main", "PHA")]
        # v8.3.20 catches both (both have orphan tickets)
        ticket_phantoms = eng.find_phantom_recover_tickets(
            held_tickers_by_pid=held,
        )
        assert {p[2] for p in ticket_phantoms} == {"PHA", "PHB"}
