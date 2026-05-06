"""Tests for earnings_watcher.signals.

Covers:
  - wilder_dmi against AMD fixture bars (sanity check)
  - find_nhod_dmi_breakout returns None on flat bars
  - find_nhod_dmi_breakout returns long signal on synthetic breakout sequence
  - quality_score returns score=4 bullish for beat eps+rev
  - determine_session correctly classifies AMC/BMO bars
  - filter_bars_for_session correctly slices bars
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

import pytest

from earnings_watcher.signals import (
    DMI_ADX_MIN,
    DMI_DI_MIN,
    DMI_LOOKBACK,
    DMI_MAX_ENTRY_IDX,
    DMI_MIN_VOL,
    DMI_PERIOD,
    DMI_VOL_MULT,
    determine_session,
    filter_bars_for_session,
    find_nhod_dmi_breakout,
    quality_score,
    wilder_dmi,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

AMD_BARS_PATH = (
    "/home/user/workspace/earnings_watcher_spec/replay_data/bars/AMD/2026-05-05.jsonl"
)


def _load_amd_bars() -> List[Dict[str, Any]]:
    """Load AMD AMC bars from the replay corpus fixture."""
    if not os.path.exists(AMD_BARS_PATH):
        return []
    bars = []
    with open(AMD_BARS_PATH) as fh:
        for line in fh:
            line = line.strip()
            if line:
                bars.append(json.loads(line))
    return bars


def _make_flat_bars(n: int = 40, base_px: float = 100.0, vol: int = 10_000) -> List[Dict[str, Any]]:
    """Synthetic flat bars (no trend, low volume)."""
    return [
        {
            "timestamp": f"2026-01-01T19:{i:02d}:00+00:00",
            "open": base_px,
            "high": base_px + 0.01,
            "low": base_px - 0.01,
            "close": base_px,
            "volume": vol,
        }
        for i in range(n)
    ]


def _make_breakout_bars() -> List[Dict[str, Any]]:
    """Synthetic bars with a clear NHOD + high-volume + DI+ breakout.

    Structure:
      - 30 bars of quiet consolidation at 100.0 with low volume
      - Then a runaway bar: close=105, volume=500k (3x+ median), new high
      - Then follow-through bar: close=106 (close > breakout close)
    This should trigger find_nhod_dmi_breakout as a long.
    """
    bars: List[Dict[str, Any]] = []
    base_px = 100.0

    # 30 quiet consolidation bars
    for i in range(30):
        bars.append(
            {
                "timestamp": f"2026-01-01T19:{i:02d}:00+00:00",
                "open": base_px,
                "high": base_px + 0.10 + i * 0.01,  # small rising highs
                "low": base_px - 0.05,
                "close": base_px + 0.05,
                "volume": 30_000,
            }
        )

    # Breakout bar (bar index 30): new high, massive volume
    prior_high = max(b["high"] for b in bars)
    bars.append(
        {
            "timestamp": "2026-01-01T19:30:00+00:00",
            "open": prior_high,
            "high": prior_high + 5.0,   # clear new high
            "low": prior_high - 0.50,
            "close": prior_high + 4.5,
            "volume": 600_000,          # well above DMI_MIN_VOL and 3x median
        }
    )

    # Follow-through bar (bar index 31): close > breakout close (confirmation)
    breakout_close = bars[-1]["close"]
    bars.append(
        {
            "timestamp": "2026-01-01T19:31:00+00:00",
            "open": breakout_close,
            "high": breakout_close + 1.0,
            "low": breakout_close - 0.20,
            "close": breakout_close + 0.80,  # > breakout bar close
            "volume": 200_000,
        }
    )

    return bars


# ---------------------------------------------------------------------------
# wilder_dmi tests
# ---------------------------------------------------------------------------

def test_wilder_dmi_length_matches_bars():
    """Output length must equal input bar count."""
    amd_bars = _load_amd_bars()
    if not amd_bars:
        pytest.skip("AMD bars fixture not found")
    result = wilder_dmi(amd_bars)
    assert len(result) == len(amd_bars)


def test_wilder_dmi_first_values_none():
    """First DMI_PERIOD bars should produce (None, None, None) tuples (warmup).

    The implementation returns a list of tuples (di_plus, di_minus, adx).
    Before warmup completes the tuple values are None.
    """
    bars = _make_flat_bars(n=50)
    result = wilder_dmi(bars)
    # First period positions: di_plus component should be None
    for i in range(DMI_PERIOD):
        entry = result[i]
        # May be None or a tuple of (None, None, None)
        if entry is None:
            pass  # also acceptable
        else:
            di_plus, di_minus, adx = entry
            assert di_plus is None, f"Expected di_plus=None at index {i}, got {di_plus}"


def test_wilder_dmi_amd_sanity():
    """AMD fixture: once warmed up, di_plus and adx should be positive."""
    amd_bars = _load_amd_bars()
    if not amd_bars:
        pytest.skip("AMD bars fixture not found")
    result = wilder_dmi(amd_bars)
    # Filter to non-None values
    valid = [(i, v) for i, v in enumerate(result) if v is not None and v[0] is not None]
    assert len(valid) > 0, "No valid DMI values computed from AMD bars"
    # All di_plus should be non-negative
    for _, (di_plus, di_minus, adx) in valid:
        assert di_plus >= 0, f"di_plus={di_plus} should be >= 0"
        assert di_minus >= 0, f"di_minus={di_minus} should be >= 0"
        if adx is not None:
            assert 0 <= adx <= 100, f"adx={adx} should be in [0,100]"


def test_wilder_dmi_too_short_returns_all_none():
    """Fewer than DMI_PERIOD+2 bars -> all None."""
    bars = _make_flat_bars(n=DMI_PERIOD)
    result = wilder_dmi(bars)
    assert all(v is None for v in result)


# ---------------------------------------------------------------------------
# find_nhod_dmi_breakout tests
# ---------------------------------------------------------------------------

def test_find_nhod_dmi_breakout_flat_bars_returns_none():
    """Flat, low-volume bars should not produce any breakout signal."""
    bars = _make_flat_bars(n=50)
    result = find_nhod_dmi_breakout(bars)
    assert result is None, f"Expected None for flat bars, got {result}"


def test_find_nhod_dmi_breakout_too_few_bars_returns_none():
    """Fewer than required bars should return None immediately."""
    bars = _make_flat_bars(n=5)
    result = find_nhod_dmi_breakout(bars)
    assert result is None


def test_find_nhod_dmi_breakout_long_signal_on_synthetic_breakout():
    """Synthetic breakout sequence should yield a 'long' signal."""
    bars = _make_breakout_bars()
    result = find_nhod_dmi_breakout(bars)
    assert result is not None, "Expected a breakout signal on synthetic bars"
    assert result["direction"] == "long"
    assert result["conviction"] > 1.0, f"conviction={result['conviction']} should be > 1"
    assert result["di_plus"] is not None
    assert "idx" in result
    assert "entry_ts" in result


def test_find_nhod_dmi_breakout_long_only_respected():
    """long_only=True should never return a 'short' direction."""
    bars = _make_breakout_bars()
    result = find_nhod_dmi_breakout(bars, long_only=True)
    if result is not None:
        assert result["direction"] == "long"


# ---------------------------------------------------------------------------
# quality_score tests
# ---------------------------------------------------------------------------

def test_quality_score_bullish_beat_eps_and_rev():
    """EPS beat by 100% and rev beat by 10% -> score=4, bias=bullish."""
    event = {
        "epsActual": 2.0,
        "epsEstimated": 1.0,
        "revActual": 110,
        "revEstimated": 100,
    }
    result = quality_score(event)
    assert result["score"] == 4
    assert result["bias"] == "bullish"
    assert result["components"]["beat_eps"] is True
    assert result["components"]["beat_revenue"] is True


def test_quality_score_neutral_minimal_beat():
    """Just at or below threshold -> neutral."""
    event = {
        "epsActual": 1.005,
        "epsEstimated": 1.0,  # 0.5% beat -> below 1% threshold
        "revActual": 100,
        "revEstimated": 100,
    }
    result = quality_score(event)
    assert result["score"] == 0
    assert result["bias"] == "neutral"


def test_quality_score_bearish_miss_both():
    """Miss eps and rev by >1% -> score=-4, bias=bearish."""
    event = {
        "epsActual": 0.5,
        "epsEstimated": 1.0,   # -50% miss
        "revActual": 90,
        "revEstimated": 100,   # -10% miss
    }
    result = quality_score(event)
    assert result["score"] == -4
    assert result["bias"] == "bearish"


def test_quality_score_missing_data():
    """Missing epsActual/revActual should not raise; score=0."""
    event = {}
    result = quality_score(event)
    assert result["score"] == 0
    assert result["bias"] == "neutral"
    assert result["components"]["eps_surp"] is None
    assert result["components"]["rev_surp"] is None


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def test_determine_session_amc():
    """Bars at 19:00-23:00 UTC should be classified as AMC."""
    bars = [
        {"timestamp": f"2026-01-01T19:{i:02d}:00+00:00", "volume": 50_000}
        for i in range(20)
    ]
    result = determine_session(bars)
    assert result == "amc"


def test_determine_session_bmo():
    """Bars at 08:00-13:00 UTC should be classified as BMO."""
    bars = [
        {"timestamp": f"2026-01-01T{8 + i // 60:02d}:{i % 60:02d}:00+00:00", "volume": 50_000}
        for i in range(20)
    ]
    result = determine_session(bars)
    assert result == "bmo"


def test_determine_session_empty():
    result = determine_session([])
    assert result == "unknown"


def test_filter_bars_for_session_amc():
    """AMC filter: only keeps 19:00-23:55 UTC bars."""
    bars = [
        {"timestamp": "2026-01-01T18:00:00+00:00", "volume": 1},
        {"timestamp": "2026-01-01T19:00:00+00:00", "volume": 2},
        {"timestamp": "2026-01-01T21:30:00+00:00", "volume": 3},
        {"timestamp": "2026-01-02T00:00:00+00:00", "volume": 4},
    ]
    result = filter_bars_for_session(bars, "amc")
    # Should include 19:00 and 21:30, not 18:00 or 00:00
    timestamps = [b["timestamp"] for b in result]
    assert "2026-01-01T19:00:00+00:00" in timestamps
    assert "2026-01-01T21:30:00+00:00" in timestamps
    assert "2026-01-01T18:00:00+00:00" not in timestamps
    assert "2026-01-02T00:00:00+00:00" not in timestamps


def test_filter_bars_for_session_bmo():
    """BMO filter: only keeps 08:00-13:25 UTC bars."""
    bars = [
        {"timestamp": "2026-01-01T07:00:00+00:00", "volume": 1},
        {"timestamp": "2026-01-01T08:00:00+00:00", "volume": 2},
        {"timestamp": "2026-01-01T13:25:00+00:00", "volume": 3},
        {"timestamp": "2026-01-01T13:30:00+00:00", "volume": 4},
    ]
    result = filter_bars_for_session(bars, "bmo")
    timestamps = [b["timestamp"] for b in result]
    assert "2026-01-01T08:00:00+00:00" in timestamps
    assert "2026-01-01T13:25:00+00:00" in timestamps
    assert "2026-01-01T07:00:00+00:00" not in timestamps
    assert "2026-01-01T13:30:00+00:00" not in timestamps
