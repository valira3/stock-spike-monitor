"""v5.10.4 \u2014 Phase A/B/C wiring unit tests.

Stage 3 of v5.10.4 wires the v5.10.0 Triple-Lock phase machine into
manage_positions / manage_short_positions. Helpers
(`step_two_bar_lock_on_5m`, `step_phase_c_if_eligible`,
`evaluate_phase_c_exit`) shipped in v5.10.0 / v5.10.3 but had no live
caller. v5.10.4 adds `_phase_machine_step` to drive them on each
manage cycle.

These tests exercise the orchestrator surface for transitions:
Survival -> NEUT_LAYERED on Entry-2, NEUT_LAYERED -> NEUT_LOCKED on
two consecutive favorable 5m closes, NEUT_LOCKED -> EXTRACTION on
EMA seed, EXTRACTION exit on 5m close cross EMA9.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import eye_of_tiger as eot  # noqa: E402
import v5_10_1_integration as eot_glue  # noqa: E402


def _seed_long_layered(ticker: str = "PHX") -> dict:
    eot_glue.clear_position_state(ticker, eot.SIDE_LONG)
    eot_glue.init_position_state_on_entry_1(
        ticker, eot.SIDE_LONG,
        entry_price=100.0, shares=100,
        entry_ts=datetime(2026, 4, 28, 10, 0),
        hwm_at_entry=100.0,
    )
    return eot_glue.record_entry_2(
        ticker, eot.SIDE_LONG,
        entry_2_price=110.0, entry_2_shares=50,
        entry_2_ts=datetime(2026, 4, 28, 10, 5),
    )


def test_two_bar_lock_advances_after_two_favorable_closes_long():
    state = _seed_long_layered("PHX1")
    assert state["phase"] == eot.PHASE_NEUT_LAYERED
    eot_glue.step_two_bar_lock_on_5m("PHX1", eot.SIDE_LONG, 110.0, 111.0)
    eot_glue.step_two_bar_lock_on_5m("PHX1", eot.SIDE_LONG, 111.0, 112.0)
    final = eot_glue.get_position_state("PHX1", eot.SIDE_LONG)
    assert final["phase"] == eot.PHASE_NEUT_LOCKED


def test_two_bar_lock_resets_on_unfavorable_close_long():
    _seed_long_layered("PHX2")
    eot_glue.step_two_bar_lock_on_5m("PHX2", eot.SIDE_LONG, 110.0, 111.0)
    eot_glue.step_two_bar_lock_on_5m("PHX2", eot.SIDE_LONG, 111.0, 110.5)
    state = eot_glue.get_position_state("PHX2", eot.SIDE_LONG)
    assert state["phase"] == eot.PHASE_NEUT_LAYERED


def test_phase_c_extraction_advances_when_ema_seeded_long():
    _seed_long_layered("PHX3")
    eot_glue.step_two_bar_lock_on_5m("PHX3", eot.SIDE_LONG, 110.0, 111.0)
    eot_glue.step_two_bar_lock_on_5m("PHX3", eot.SIDE_LONG, 111.0, 112.0)
    eot_glue.step_phase_c_if_eligible("PHX3", eot.SIDE_LONG, 110.5, True)
    state = eot_glue.get_position_state("PHX3", eot.SIDE_LONG)
    assert state["phase"] == eot.PHASE_EXTRACTION


def test_evaluate_phase_c_exit_long_fires_below_ema():
    _seed_long_layered("PHX4")
    eot_glue.step_two_bar_lock_on_5m("PHX4", eot.SIDE_LONG, 110.0, 111.0)
    eot_glue.step_two_bar_lock_on_5m("PHX4", eot.SIDE_LONG, 111.0, 112.0)
    eot_glue.step_phase_c_if_eligible("PHX4", eot.SIDE_LONG, 112.0, True)
    assert eot_glue.evaluate_phase_c_exit("PHX4", eot.SIDE_LONG, 111.5) is True
    assert eot_glue.evaluate_phase_c_exit("PHX4", eot.SIDE_LONG, 112.5) is False


def test_evaluate_phase_c_exit_short_fires_above_ema():
    eot_glue.clear_position_state("PHX5", eot.SIDE_SHORT)
    eot_glue.init_position_state_on_entry_1(
        "PHX5", eot.SIDE_SHORT,
        entry_price=100.0, shares=100,
        entry_ts=datetime(2026, 4, 28, 10, 0),
        hwm_at_entry=100.0,
    )
    eot_glue.record_entry_2(
        "PHX5", eot.SIDE_SHORT,
        entry_2_price=90.0, entry_2_shares=50,
        entry_2_ts=datetime(2026, 4, 28, 10, 5),
    )
    eot_glue.step_two_bar_lock_on_5m("PHX5", eot.SIDE_SHORT, 90.0, 89.0)
    eot_glue.step_two_bar_lock_on_5m("PHX5", eot.SIDE_SHORT, 89.0, 88.0)
    eot_glue.step_phase_c_if_eligible("PHX5", eot.SIDE_SHORT, 89.5, True)
    assert eot_glue.evaluate_phase_c_exit("PHX5", eot.SIDE_SHORT, 90.0) is True
    assert eot_glue.evaluate_phase_c_exit("PHX5", eot.SIDE_SHORT, 89.0) is False
