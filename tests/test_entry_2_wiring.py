"""v5.10.4 \u2014 Entry 2 wiring unit tests.

Stage 2 of v5.10.4 wires the Section III Entry 2 (50% scale-in) into
the live hot path of trade_genius.py. v5.10.0 / v5.10.3 shipped the
pure evaluator (`eot.evaluate_entry_2`) and orchestrator surface
(`eot_glue.evaluate_entry_2_decision`, `eot_glue.record_entry_2`) but
no live caller. v5.10.4 adds `check_entry_2` and `execute_entry_2`.

These tests exercise the orchestrator surface, not the live scan loop.
The integration goal here: when a position is open and DI crosses the
30 threshold with a fresh extreme, the evaluator should fire and the
state should advance to Phase B with a break-even stop.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import eye_of_tiger as eot  # noqa: E402
import v5_10_1_integration as eot_glue  # noqa: E402


def _reset_state(ticker: str) -> None:
    eot_glue.clear_position_state(ticker, eot.SIDE_LONG)
    eot_glue.clear_position_state(ticker, eot.SIDE_SHORT)


def test_entry_2_fires_on_di_cross_with_fresh_nhod_long():
    _reset_state("TEST")
    from datetime import datetime
    eot_glue.init_position_state_on_entry_1(
        "TEST", eot.SIDE_LONG,
        entry_price=100.0, shares=100,
        entry_ts=datetime(2026, 4, 28, 10, 0),
        hwm_at_entry=100.0,
    )
    eot_glue.evaluate_entry_2_decision(
        "TEST", eot.SIDE_LONG,
        entry_1_active=True,
        permit_open_at_trigger=True,
        di_1m_now=29.0,
        fresh_nhod_or_nlod=True,
        entry_2_already_fired=False,
    )
    decision = eot_glue.evaluate_entry_2_decision(
        "TEST", eot.SIDE_LONG,
        entry_1_active=True,
        permit_open_at_trigger=True,
        di_1m_now=31.0,
        fresh_nhod_or_nlod=True,
        entry_2_already_fired=False,
    )
    assert decision["fire"] is True


def test_entry_2_blocked_by_no_fresh_nhod_long():
    _reset_state("TEST")
    from datetime import datetime
    eot_glue.init_position_state_on_entry_1(
        "TEST", eot.SIDE_LONG,
        entry_price=100.0, shares=100,
        entry_ts=datetime(2026, 4, 28, 10, 0),
        hwm_at_entry=100.0,
    )
    eot_glue.evaluate_entry_2_decision(
        "TEST", eot.SIDE_LONG,
        entry_1_active=True,
        permit_open_at_trigger=True,
        di_1m_now=29.0,
        fresh_nhod_or_nlod=False,
        entry_2_already_fired=False,
    )
    decision = eot_glue.evaluate_entry_2_decision(
        "TEST", eot.SIDE_LONG,
        entry_1_active=True,
        permit_open_at_trigger=True,
        di_1m_now=35.0,
        fresh_nhod_or_nlod=False,
        entry_2_already_fired=False,
    )
    assert decision["fire"] is False


def test_entry_2_blocked_when_permit_closes_at_trigger():
    _reset_state("TEST")
    from datetime import datetime
    eot_glue.init_position_state_on_entry_1(
        "TEST", eot.SIDE_LONG,
        entry_price=100.0, shares=100,
        entry_ts=datetime(2026, 4, 28, 10, 0),
        hwm_at_entry=100.0,
    )
    eot_glue.evaluate_entry_2_decision(
        "TEST", eot.SIDE_LONG,
        entry_1_active=True,
        permit_open_at_trigger=False,  # QQQ flipped against side
        di_1m_now=29.0,
        fresh_nhod_or_nlod=True,
        entry_2_already_fired=False,
    )
    decision = eot_glue.evaluate_entry_2_decision(
        "TEST", eot.SIDE_LONG,
        entry_1_active=True,
        permit_open_at_trigger=False,
        di_1m_now=35.0,
        fresh_nhod_or_nlod=True,
        entry_2_already_fired=False,
    )
    assert decision["fire"] is False


def test_record_entry_2_updates_avg_and_phase_b():
    _reset_state("TEST")
    from datetime import datetime
    eot_glue.init_position_state_on_entry_1(
        "TEST", eot.SIDE_LONG,
        entry_price=100.0, shares=100,
        entry_ts=datetime(2026, 4, 28, 10, 0),
        hwm_at_entry=100.0,
    )
    state = eot_glue.record_entry_2(
        "TEST", eot.SIDE_LONG,
        entry_2_price=110.0, entry_2_shares=50,
        entry_2_ts=datetime(2026, 4, 28, 10, 5),
    )
    assert state["entry_2_fired"] is True
    expected_avg = (100.0 * 100 + 110.0 * 50) / 150
    assert abs(state["avg_entry"] - expected_avg) < 1e-9
    assert state["phase"] == eot.PHASE_NEUT_LAYERED


def test_entry_2_short_di_minus_cross():
    _reset_state("TEST")
    from datetime import datetime
    eot_glue.init_position_state_on_entry_1(
        "TEST", eot.SIDE_SHORT,
        entry_price=100.0, shares=100,
        entry_ts=datetime(2026, 4, 28, 10, 0),
        hwm_at_entry=100.0,
    )
    eot_glue.evaluate_entry_2_decision(
        "TEST", eot.SIDE_SHORT,
        entry_1_active=True,
        permit_open_at_trigger=True,
        di_1m_now=28.0,
        fresh_nhod_or_nlod=True,
        entry_2_already_fired=False,
    )
    decision = eot_glue.evaluate_entry_2_decision(
        "TEST", eot.SIDE_SHORT,
        entry_1_active=True,
        permit_open_at_trigger=True,
        di_1m_now=33.0,
        fresh_nhod_or_nlod=True,
        entry_2_already_fired=False,
    )
    assert decision["fire"] is True
