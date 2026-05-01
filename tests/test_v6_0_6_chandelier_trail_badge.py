# tests/test_v6_0_6_chandelier_trail_badge.py
# v6.0.6 -- Surface Alarm-F chandelier trail in dashboard TRAIL badge.
#
# Background: the dashboard's TRAIL badge (shown next to the Stop column
# in Open Positions) was driven solely by the legacy pos["trail_active"]
# flag, which is set by the Phase B/C breakeven trail. Alarm F (Hybrid
# Chandelier Trailing Stop) does not touch trail_active -- it overwrites
# pos["stop"] directly via the Sentinel pipeline (broker/positions.py
# lines 547/565). So a position with an actively ratcheting chandelier
# (stage 1/2/3) looked indistinguishable from a static hard stop on the
# dashboard, even though the engine was tightening the stop every minute.
#
# v6.0.6 surfaces the chandelier stage from pos["trail_state"] (the
# engine.alarm_f_trail.TrailState dataclass) on every position row as
# the new "chandelier_stage" field. The frontend TRAIL badge now fires
# when EITHER the legacy trail_active is True OR chandelier_stage >= 1.
#
# These tests exercise the serializer directly. No em-dashes in this
# file (constraint for .py test files).
from __future__ import annotations

import os

os.environ.setdefault("SSM_SMOKE_TEST", "1")

from dashboard_server import _chandelier_stage, _serialize_positions
from engine.alarm_f_trail import TrailState


# ---------------------------------------------------------------------
# _chandelier_stage helper
# ---------------------------------------------------------------------


def test_chandelier_stage_reads_trail_state_stage():
    ts = TrailState()
    ts.stage = 2
    pos = {"trail_state": ts}
    assert _chandelier_stage(pos) == 2


def test_chandelier_stage_returns_zero_when_trail_state_missing():
    assert _chandelier_stage({}) == 0


def test_chandelier_stage_returns_zero_for_none_pos():
    # Defensive: we feed it dict.get() output everywhere, but be safe.
    assert _chandelier_stage(None) == 0


def test_chandelier_stage_handles_malformed_trail_state():
    # If the on-disk save was truncated and stage is something weird.
    class Junk:
        stage = "not-an-int"

    assert _chandelier_stage({"trail_state": Junk()}) == 0


def test_chandelier_stage_handles_zero_stage():
    ts = TrailState()
    ts.stage = 0
    assert _chandelier_stage({"trail_state": ts}) == 0


def test_chandelier_stage_handles_stage_3_tight():
    ts = TrailState()
    ts.stage = 3
    assert _chandelier_stage({"trail_state": ts}) == 3


# ---------------------------------------------------------------------
# _serialize_positions wiring
# ---------------------------------------------------------------------


def _long_pos_with_stage(stage):
    ts = TrailState()
    ts.stage = stage
    return {
        "entry_price": 100.0,
        "shares": 10,
        "stop": 99.5,
        "entry_time": "10:00:00",
        "entry_count": 1,
        "phase": "A",
        "trail_state": ts,
    }


def _short_pos_with_stage(stage):
    ts = TrailState()
    ts.stage = stage
    return {
        "entry_price": 100.0,
        "shares": 10,
        "stop": 100.5,
        "entry_time": "10:00:00",
        "phase": "A",
        "trail_state": ts,
    }


def test_serialize_positions_long_includes_chandelier_stage_zero():
    rows = _serialize_positions(
        {"AAPL": _long_pos_with_stage(0)}, {}, {"AAPL": 101.0}
    )
    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAPL"
    assert rows[0]["chandelier_stage"] == 0
    assert rows[0]["trail_active"] is False


def test_serialize_positions_long_armed_chandelier_stage_one():
    # Stage 1 = BREAKEVEN trail engaged.
    rows = _serialize_positions(
        {"AAPL": _long_pos_with_stage(1)}, {}, {"AAPL": 101.0}
    )
    assert rows[0]["chandelier_stage"] == 1


def test_serialize_positions_long_chandelier_stage_two_wide():
    rows = _serialize_positions(
        {"AAPL": _long_pos_with_stage(2)}, {}, {"AAPL": 101.0}
    )
    assert rows[0]["chandelier_stage"] == 2


def test_serialize_positions_long_chandelier_stage_three_tight():
    rows = _serialize_positions(
        {"AAPL": _long_pos_with_stage(3)}, {}, {"AAPL": 101.0}
    )
    assert rows[0]["chandelier_stage"] == 3


def test_serialize_positions_short_includes_chandelier_stage():
    # Mirror the long path for SHORT branch coverage. NFLX-on-prod
    # scenario: stage 3 chandelier ratcheting down on a winning short.
    rows = _serialize_positions(
        {}, {"NFLX": _short_pos_with_stage(3)}, {"NFLX": 92.1}
    )
    assert len(rows) == 1
    assert rows[0]["ticker"] == "NFLX"
    assert rows[0]["side"] == "SHORT"
    assert rows[0]["chandelier_stage"] == 3
    assert rows[0]["trail_active"] is False  # legacy flag stays False


def test_serialize_positions_missing_trail_state_emits_stage_zero():
    # Position created before Alarm F first armed: no trail_state yet.
    pos = {
        "entry_price": 100.0,
        "shares": 10,
        "stop": 99.5,
        "entry_time": "10:00:00",
        "entry_count": 1,
        "phase": "A",
    }
    rows = _serialize_positions({"AAPL": pos}, {}, {"AAPL": 100.5})
    assert rows[0]["chandelier_stage"] == 0


def test_serialize_positions_legacy_trail_still_works():
    # Legacy Phase B/C trail (no Alarm F): trail_active=True without
    # any trail_state. The badge logic in app.js OR's the two, so the
    # serializer must still report trail_active=True even when stage=0.
    pos = {
        "entry_price": 100.0,
        "shares": 10,
        "stop": 99.5,
        "trail_active": True,
        "trail_stop": 99.8,
        "trail_high": 102.0,
        "entry_time": "10:00:00",
        "phase": "C",
    }
    rows = _serialize_positions({"AAPL": pos}, {}, {"AAPL": 101.0})
    assert rows[0]["trail_active"] is True
    assert rows[0]["chandelier_stage"] == 0
    # effective_stop should follow the legacy trail_stop, not hard_stop.
    assert rows[0]["effective_stop"] == 99.8


def test_serialize_positions_both_trail_paths_armed_simultaneously():
    # Pathological but possible: legacy trail_active + Alarm F chandelier
    # both armed (e.g. Phase C kicked in after chandelier already moved).
    # Both flags surface independently; UI ORs them.
    ts = TrailState()
    ts.stage = 2
    pos = {
        "entry_price": 100.0,
        "shares": 10,
        "stop": 99.5,
        "trail_active": True,
        "trail_stop": 99.7,
        "trail_high": 102.0,
        "entry_time": "10:00:00",
        "phase": "C",
        "trail_state": ts,
    }
    rows = _serialize_positions({"AAPL": pos}, {}, {"AAPL": 101.0})
    assert rows[0]["trail_active"] is True
    assert rows[0]["chandelier_stage"] == 2


def test_serialize_positions_empty_inputs_no_crash():
    # Sanity: empty dicts produce empty list, do not crash on missing
    # trail_state lookup.
    rows = _serialize_positions({}, {}, {})
    assert rows == []
