"""Tests for earnings_watcher.exits.evaluate_exit.

Covers:
  - hard_stop triggers at -3.0%
  - trail triggers at -1% after peak +3% (trail stop = 3% - 5% = -2%... need to verify)
  - time stop triggers at 90 min
  - session_end triggers on a bar outside session windows
  - normal bar returns (False, '')
"""
from __future__ import annotations

import pytest

from earnings_watcher.exits import evaluate_exit
from earnings_watcher.sizing import (
    DMI_HARD_STOP,
    DMI_TIME_STOP_MIN,
    DMI_TRAIL_PCT,
    DMI_TRAIL_TRIGGER,
)


def _bar(ts: str, close: float, volume: int = 50_000) -> dict:
    return {
        "timestamp": ts,
        "open": close,
        "high": close + 0.10,
        "low": close - 0.10,
        "close": close,
        "volume": volume,
    }


def _state(
    entry_px: float = 100.0,
    direction: str = "long",
    peak_pct: float = 0.0,
    trough_pct: float = 0.0,
    trail_active: bool = False,
    trail_stop: float = 0.0,
    ticker: str = "TEST",
) -> dict:
    return {
        "entry_px": entry_px,
        "direction": direction,
        "peak_pct": peak_pct,
        "trough_pct": trough_pct,
        "trail_active": trail_active,
        "trail_stop": trail_stop,
        "ticker": ticker,
        "entry_ts": "2026-01-01T19:00:00+00:00",
        "qty": 10,
    }


# ---------------------------------------------------------------------------
# Hard stop
# ---------------------------------------------------------------------------

def test_hard_stop_exact_threshold():
    """At exactly -3% move, hard_stop fires."""
    entry_px = 100.0
    close_px = entry_px * (1.0 - DMI_HARD_STOP)  # exactly -3%
    state = _state(entry_px=entry_px)
    bar = _bar("2026-01-01T19:30:00+00:00", close_px)
    should_exit, reason = evaluate_exit(state, bar, elapsed_minutes=5)
    assert should_exit is True
    assert reason == "hard_stop"


def test_hard_stop_below_threshold():
    """At -3.5%, hard_stop still fires."""
    state = _state(entry_px=100.0)
    bar = _bar("2026-01-01T19:30:00+00:00", 96.0)  # -4%
    should_exit, reason = evaluate_exit(state, bar, elapsed_minutes=5)
    assert should_exit is True
    assert reason == "hard_stop"


def test_no_exit_at_small_loss():
    """At -1%, no stop fires."""
    state = _state(entry_px=100.0)
    bar = _bar("2026-01-01T19:30:00+00:00", 99.0)  # -1%
    should_exit, reason = evaluate_exit(state, bar, elapsed_minutes=5)
    assert should_exit is False
    assert reason == ""


# ---------------------------------------------------------------------------
# Trail stop
# ---------------------------------------------------------------------------

def test_trail_arms_at_trigger():
    """At +2% gain, trail becomes active."""
    state = _state(entry_px=100.0)
    bar = _bar("2026-01-01T19:30:00+00:00", 102.0)  # +2%
    should_exit, reason = evaluate_exit(state, bar, elapsed_minutes=5)
    # +2% hits trail_trigger; trail_stop = 0.02 - 0.05 = -0.03 (below entry)
    # Current chg is +2%, trail_stop = -3%, so no exit yet
    assert should_exit is False
    assert state["trail_active"] is True


def test_trail_fires_after_peak_then_pullback():
    """After +3% peak, trail_stop = -2%. Then -1% from entry triggers trail.

    trail_trigger = 2%, trail_pct = 5%.
    After peak at +3%: trail_stop = 3% - 5% = -2%.
    When chg drops to -1% (below -2%? No: -1% > -2%).
    Actually trail fires when chg <= trail_stop.
    trail_stop = chg - trail_pct = 0.03 - 0.05 = -0.02.
    At chg = -0.021 (below -0.02): trail fires.
    So test: peak=+3%, then close at -2.1% from entry -> trail fires.
    """
    entry_px = 100.0
    # First: establish peak of +3%
    state = _state(entry_px=entry_px)
    bar_peak = _bar("2026-01-01T19:30:00+00:00", 103.0)  # +3%
    should_exit, _ = evaluate_exit(state, bar_peak, elapsed_minutes=5)
    assert should_exit is False
    assert state["trail_active"] is True
    # trail_stop = 0.03 - 0.05 = -0.02

    # Now drop to -2.1% from entry: chg=-0.021, trail_stop=-0.02 -> chg <= trail_stop
    bar_drop = _bar("2026-01-01T19:31:00+00:00", 97.9)   # -2.1%
    should_exit2, reason2 = evaluate_exit(state, bar_drop, elapsed_minutes=6)
    assert should_exit2 is True
    assert reason2 == "trail"


def test_trail_does_not_fire_above_ratcheted_stop():
    """After peak=+6%, trail_stop ratchets to +1% (6%-5%). At +1.5%, trail does NOT fire.

    trail_trigger=2%, trail_pct=5%.
    After chg=+6%: trail_stop = max(0.0, 0.06 - 0.05) = 0.01.
    At chg=+1.5% (> trail_stop 1%): no fire.
    At chg=+0.5% (< trail_stop 1%): fire.
    """
    entry_px = 100.0
    state = _state(entry_px=entry_px)
    # Establish peak at +6%
    bar_peak = _bar("2026-01-01T19:30:00+00:00", 106.0)
    should_exit, _ = evaluate_exit(state, bar_peak, elapsed_minutes=5)
    assert should_exit is False
    assert state["trail_active"] is True
    assert abs(state["trail_stop"] - 0.01) < 1e-9, f"trail_stop should be 0.01 got {state['trail_stop']}"

    # At +1.5%: chg=0.015 > trail_stop=0.01 -> no fire
    bar_above = _bar("2026-01-01T19:31:00+00:00", 101.5)
    should_exit2, reason2 = evaluate_exit(state, bar_above, elapsed_minutes=6)
    assert should_exit2 is False, f"Trail should not fire at +1.5% with stop=1%; reason={reason2}"


# ---------------------------------------------------------------------------
# Time stop
# ---------------------------------------------------------------------------

def test_time_stop_at_90_min():
    """At elapsed=90, time stop fires."""
    state = _state(entry_px=100.0)
    bar = _bar("2026-01-01T20:30:00+00:00", 100.5)   # no loss, no trail
    should_exit, reason = evaluate_exit(state, bar, elapsed_minutes=DMI_TIME_STOP_MIN)
    assert should_exit is True
    assert reason == "time"


def test_time_stop_not_at_89_min():
    """At elapsed=89, time stop does NOT fire."""
    state = _state(entry_px=100.0)
    bar = _bar("2026-01-01T20:29:00+00:00", 100.5)
    should_exit, reason = evaluate_exit(state, bar, elapsed_minutes=DMI_TIME_STOP_MIN - 1)
    assert should_exit is False


# ---------------------------------------------------------------------------
# Session end
# ---------------------------------------------------------------------------

def test_session_end_outside_both_windows():
    """Bar at 14:00 UTC is outside BMO (08-13:25) and AMC (19-23:55) -> session_end."""
    state = _state(entry_px=100.0)
    bar = _bar("2026-01-01T14:00:00+00:00", 100.0)
    should_exit, reason = evaluate_exit(state, bar, elapsed_minutes=5)
    assert should_exit is True
    assert reason == "session_end"


def test_no_session_end_in_amc_window():
    """Bar at 19:00 UTC is inside AMC window -> no session_end."""
    state = _state(entry_px=100.0)
    bar = _bar("2026-01-01T19:00:00+00:00", 100.5)
    should_exit, reason = evaluate_exit(state, bar, elapsed_minutes=5)
    assert should_exit is False
    assert reason != "session_end"


def test_no_session_end_in_bmo_window():
    """Bar at 08:00 UTC is inside BMO window -> no session_end."""
    state = _state(entry_px=100.0)
    bar = _bar("2026-01-01T08:00:00+00:00", 100.5)
    should_exit, reason = evaluate_exit(state, bar, elapsed_minutes=5)
    assert should_exit is False
    assert reason != "session_end"


# ---------------------------------------------------------------------------
# State mutation tests
# ---------------------------------------------------------------------------

def test_peak_pct_updated():
    """evaluate_exit should update peak_pct in-place on a gain bar."""
    state = _state(entry_px=100.0, peak_pct=0.0)
    bar = _bar("2026-01-01T19:30:00+00:00", 101.5)   # +1.5%
    evaluate_exit(state, bar, elapsed_minutes=5)
    assert abs(state["peak_pct"] - 0.015) < 1e-9


def test_trough_pct_updated():
    """evaluate_exit should update trough_pct (negative) on a loss bar."""
    state = _state(entry_px=100.0, trough_pct=0.0)
    bar = _bar("2026-01-01T19:30:00+00:00", 99.0)    # -1%
    evaluate_exit(state, bar, elapsed_minutes=5)
    assert state["trough_pct"] < 0
