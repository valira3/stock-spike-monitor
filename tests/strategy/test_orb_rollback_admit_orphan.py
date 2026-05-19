"""v9.1.132 -- rollback_admit must clean up only the SPECIFIC ticket it
was called with, not whatever currently maps to the ticker.

Reproduces the 2026-05-19 TSLA Main incident: an original successful
admit at 14:11 registered ticker_to_ticket["TSLA"] = ticket_A. At
14:32+, 10 re-admit/rollback cycles ran. Each re-admit OVERWROTE
ticker_to_ticket["TSLA"] with a new ticket_B/C/.... The old
rollback_admit then looked up the CURRENT mapping and deleted whatever
was there -- including the bare-position ticket_A whose ticker entry
had been stomped.

Symptom: check_exit_by_ticker("TSLA") returned `no_open_v10_position`
because ticker_to_ticket was empty even though _open_positions still
held the original position. Legacy sentinel A then fired the exit.

Fix: rollback_admit only deletes _open_positions[ticket_id] when
ticket_id matches and only clears _ticker_to_ticket[ticker] when it
currently points to ticket_id being rolled back.
"""
from __future__ import annotations

from unittest import mock

import pytest

from orb import live_runtime as lr


class _FakeAdapter:
    """Minimal stand-in for LiveAdapter used by rollback_admit."""
    def __init__(self):
        self._open_positions: dict = {}
        self._ticker_to_ticket: dict = {}


class _FakeRiskBook:
    def release_by_id(self, _tid):
        return False


class _FakeState:
    def get_day_state(self, _pid, _ticker):
        ds = mock.Mock()
        ds.phase = "ARMED"  # not IN_POS so step 2 is a no-op
        ds.in_position = False
        return ds


class _FakeEngine:
    def __init__(self, adapter):
        self._adapter = adapter
        self._risk = {"main": _FakeRiskBook()}
        self._state = _FakeState()


@pytest.fixture
def patched_runtime():
    """Patch live_runtime's module-level engine + adapter accessor for the test."""
    adapter = _FakeAdapter()
    engine = _FakeEngine(adapter)
    with mock.patch.object(lr, "_engine", engine), \
         mock.patch.object(lr, "get_adapter", return_value=adapter), \
         mock.patch.object(lr, "persist_engine_state", lambda: None), \
         mock.patch.object(lr, "_rollback_history", {}):
        yield adapter


def test_rollback_does_not_orphan_existing_position(patched_runtime):
    """Pre-fix bug repro: a second admit overwrites the ticker map; rollback of
    the SECOND ticket must not delete the FIRST ticket's adapter state."""
    adapter = patched_runtime
    # Original successful admit at 14:11
    adapter._open_positions["ticket_A"] = "pos_A"
    adapter._ticker_to_ticket["TSLA"] = "ticket_A"
    # Re-admit at 14:32 overwrites the reverse-lookup
    adapter._open_positions["ticket_B"] = "pos_B"
    adapter._ticker_to_ticket["TSLA"] = "ticket_B"  # stomp
    # Roll back the second admit (broker fire failed)
    lr.rollback_admit("main", "TSLA", ticket_id="ticket_B", reason="test", side="short")
    # The B-ticket should be cleaned up
    assert "ticket_B" not in adapter._open_positions
    # The A-ticket MUST survive (the bug deleted it)
    assert adapter._open_positions.get("ticket_A") == "pos_A"
    # The reverse lookup: pre-fix this was empty; post-fix we leave it
    # pointing wherever it was (ticket_B was popped; nothing points to A).
    # The orphan was: position_A existed in _open_positions but
    # _ticker_to_ticket["TSLA"] was None. Post-fix we expect the same
    # cleanup of B's entry but the original's _open_positions[ticket_A]
    # entry survives so legacy callers using ticket-id directly still work.
    # _ticker_to_ticket["TSLA"] correctly equals neither A nor B after rollback,
    # which means later admits will set it cleanly.


def test_rollback_with_correct_specific_ticket(patched_runtime):
    """Single ticket rollback: cleans up both _open_positions[tid] and the
    reverse-lookup if it points to tid."""
    adapter = patched_runtime
    adapter._open_positions["ticket_X"] = "pos_X"
    adapter._ticker_to_ticket["AAPL"] = "ticket_X"
    lr.rollback_admit("main", "AAPL", ticket_id="ticket_X", reason="test", side="long")
    assert "ticket_X" not in adapter._open_positions
    assert "AAPL" not in adapter._ticker_to_ticket


def test_rollback_without_ticket_id_is_noop_for_adapter(patched_runtime):
    """No ticket_id passed -> adapter cleanup should not touch _open_positions
    or _ticker_to_ticket (backward compat for callers that don't have a tid)."""
    adapter = patched_runtime
    adapter._open_positions["ticket_Y"] = "pos_Y"
    adapter._ticker_to_ticket["MSFT"] = "ticket_Y"
    lr.rollback_admit("main", "MSFT", ticket_id="", reason="test", side="long")
    # The unrelated state should be untouched (ticket_id="" means we can't
    # safely target a specific position; better to leave both than guess).
    assert "ticket_Y" in adapter._open_positions
    assert adapter._ticker_to_ticket.get("MSFT") == "ticket_Y"


def test_rollback_ticket_not_in_positions_does_not_clear_reverse_lookup(
    patched_runtime,
):
    """If we're rolling back ticket_B but the reverse-lookup points to ticket_A
    (an unrelated valid admit), the reverse-lookup must NOT be cleared."""
    adapter = patched_runtime
    adapter._open_positions["ticket_A"] = "pos_A"
    adapter._ticker_to_ticket["NFLX"] = "ticket_A"
    # ticket_B isn't in _open_positions (already gone or never registered).
    lr.rollback_admit("main", "NFLX", ticket_id="ticket_B", reason="test", side="short")
    # ticket_A's mapping stays intact
    assert adapter._open_positions.get("ticket_A") == "pos_A"
    assert adapter._ticker_to_ticket.get("NFLX") == "ticket_A"
