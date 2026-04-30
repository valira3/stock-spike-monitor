"""v5.20.5 \u2014 Dashboard expanded card metric payload tests.

Covers the new metric blocks shipped per /api/state position by
``v5_10_6_snapshot._per_position_v510``:

  - sovereign_brake: {unrealized_pct, brake_threshold_pct, time_in_position_min}
  - velocity_fuse:   {last_5m_move_pct, fuse_threshold_pct}
  - strikes:         {strikes_count, strike_history}

Each block is null-safe \u2014 missing source data renders empty/None values
rather than raising. The existing ``phase`` /
``sovereign_brake_distance_dollars`` / ``entry_2_fired`` legacy fields
must continue to ship unchanged so older dashboard JS clients keep
working until the v5.20.5 dashboard rolls out.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import v5_10_6_snapshot as snap  # noqa: E402


# ---------------------------------------------------------------------
# Per-position card metrics
# ---------------------------------------------------------------------


def test_per_position_includes_all_metric_blocks():
    """A LONG position with all source data populated renders every
    expected metric field with a finite numeric value (or list).
    """
    entry_time = (datetime.now(tz=timezone.utc) - timedelta(minutes=12)).isoformat()
    longs = {
        "AAPL": {
            "entry_price": 200.0,
            "shares": 50,
            "phase": "B",
            "v5104_entry2_fired": True,
            "entry_time": entry_time,
            "current_1m_open": 199.50,
        }
    }
    prices = {"AAPL": 199.20}
    out = snap._per_position_v510(longs, {}, prices)

    pos = out["AAPL:LONG"]
    # Legacy fields preserved.
    assert pos["phase"] == "B"
    assert pos["entry_2_fired"] is True
    assert isinstance(pos["sovereign_brake_distance_dollars"], float)

    # Sovereign Brake card.
    sb = pos["sovereign_brake"]
    assert isinstance(sb["unrealized_pct"], float)
    assert isinstance(sb["brake_threshold_pct"], float)
    # Brake fires at -$500. On a $10,000 position that is -5%.
    assert abs(sb["brake_threshold_pct"] - (-5.0)) < 1e-6
    # Position value 200 * 50 = 10000; pnl (199.2 - 200) * 50 = -40 \u2192 -0.4%
    assert abs(sb["unrealized_pct"] - (-0.4)) < 1e-6
    assert sb["brake_threshold_dollars"] == -500.0
    assert isinstance(sb["time_in_position_min"], float)
    # ~12 minutes \u00b1 small wallclock drift.
    assert 11.0 <= sb["time_in_position_min"] <= 13.5

    # Velocity Fuse card.
    vf = pos["velocity_fuse"]
    # last_5m_move_pct: open=199.50, last=199.20 \u2192 -0.1503%
    assert isinstance(vf["last_5m_move_pct"], float)
    assert abs(vf["last_5m_move_pct"] - (-0.1504)) < 0.01
    # fuse_threshold_pct: 0.01 fraction \u2192 surfaced as 1.0 (percent).
    assert abs(vf["fuse_threshold_pct"] - 1.0) < 1e-6

    # Strikes card.
    stk = pos["strikes"]
    assert "strikes_count" in stk
    assert isinstance(stk["strike_history"], list)


def test_per_position_short_uses_inverted_pnl():
    """SHORT pnl = (entry - mark) * shares; brake threshold pct is
    relative to entry value, not mark.
    """
    shorts = {
        "TSLA": {
            "entry_price": 250.0,
            "shares": 40,
            "phase": "A",
            "v5104_entry2_fired": False,
        }
    }
    prices = {"TSLA": 252.50}
    out = snap._per_position_v510({}, shorts, prices)
    pos = out["TSLA:SHORT"]
    # Pnl = (250 - 252.5) * 40 = -100. Position value = 250 * 40 = 10000.
    sb = pos["sovereign_brake"]
    assert abs(sb["unrealized_pct"] - (-1.0)) < 1e-6
    assert abs(sb["brake_threshold_pct"] - (-5.0)) < 1e-6


def test_per_position_card_fields_null_when_data_missing():
    """A position with NO entry_time, NO current_1m_open, and zero
    shares must still render (no exception) with null/None values
    inside each block. The dashboard grays these rows out.
    """
    longs = {
        "ABC": {
            "entry_price": 0.0,
            "shares": 0,
            "phase": "A",
            "v5104_entry2_fired": False,
        }
    }
    out = snap._per_position_v510(longs, {}, {})
    pos = out["ABC:LONG"]

    sb = pos["sovereign_brake"]
    assert sb["unrealized_pct"] is None
    assert sb["brake_threshold_pct"] is None
    assert sb["time_in_position_min"] is None

    vf = pos["velocity_fuse"]
    assert vf["last_5m_move_pct"] is None
    # The threshold itself is a CONSTANT \u2014 always shipped, never null.
    assert vf["fuse_threshold_pct"] == 1.0

    stk = pos["strikes"]
    # strike_history is a list (possibly empty) regardless.
    assert isinstance(stk["strike_history"], list)


def test_per_position_handles_malformed_entry_time():
    """An unparseable entry_time string must NOT raise; the field
    silently returns None.
    """
    longs = {
        "ABC": {
            "entry_price": 100.0,
            "shares": 10,
            "phase": "A",
            "entry_time": "not-an-iso-timestamp",
        }
    }
    out = snap._per_position_v510(longs, {}, {"ABC": 100.0})
    assert out["ABC:LONG"]["sovereign_brake"]["time_in_position_min"] is None


def test_per_position_does_not_clobber_legacy_fields():
    """v5.20.5 must be additive only \u2014 every field present in
    v5.20.4 (phase, sovereign_brake_distance_dollars, entry_2_fired)
    must still ship verbatim so the existing dashboard JS keeps working.
    """
    longs = {
        "X": {
            "entry_price": 50.0,
            "shares": 100,
            "phase": "C",
            "v5104_entry2_fired": True,
        }
    }
    out = snap._per_position_v510(longs, {}, {"X": 50.0})
    pos = out["X:LONG"]
    assert set(pos.keys()) >= {
        "phase",
        "sovereign_brake_distance_dollars",
        "entry_2_fired",
        "sovereign_brake",
        "velocity_fuse",
        "strikes",
    }
    assert pos["phase"] == "C"
    assert pos["entry_2_fired"] is True


# ---------------------------------------------------------------------
# Helper-level null safety
# ---------------------------------------------------------------------


def test_time_in_position_min_handles_missing_and_iso_z_format():
    """ISO with trailing Z (UTC zulu) must parse; missing returns None."""
    assert snap._time_in_position_min({"entry_time": None}) is None
    assert snap._time_in_position_min({}) is None
    iso_z = (datetime.now(tz=timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    val = snap._time_in_position_min({"entry_time": iso_z})
    assert val is not None
    assert 4.0 <= val <= 6.0


def test_last_5m_move_pct_handles_zero_and_missing():
    """A zero or missing current_1m_open must NOT divide by zero \u2014 helper
    returns None.
    """
    assert snap._last_5m_move_pct("X", {}, {"X": 100.0}) is None
    assert snap._last_5m_move_pct("X", {"current_1m_open": 0.0}, {"X": 100.0}) is None
    assert snap._last_5m_move_pct("X", {"current_1m_open": 100.0}, {}) is None
