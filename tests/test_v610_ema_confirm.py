"""v6.1.0 improvement #2: two-bar EMA-cross confirmation + lunch-chop suppression.

Tests for the stateful per-position counter in check_alarm_b and the
11:30-13:00 ET suppression window.

All six test cases use the position_id= path (v6.1.0 stateful path).
Each test calls reset_ema_cross_pending() in setup to ensure a clean slate.
"""

from __future__ import annotations

from datetime import datetime

import pytest

import engine.sentinel as sentinel_mod
from engine.sentinel import (
    SIDE_LONG,
    SIDE_SHORT,
    check_alarm_b,
    reset_ema_cross_pending,
)
from engine.timing import ET


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _long_cross(position_id: str, now_et=None):
    """Call check_alarm_b with a LONG cross condition (close < ema9)."""
    return check_alarm_b(
        side=SIDE_LONG,
        last_5m_close=99.0,
        last_5m_ema9=100.0,
        position_id=position_id,
        now_et=now_et,
    )


def _long_no_cross(position_id: str, now_et=None):
    """Call check_alarm_b with NO cross condition for LONG (close > ema9)."""
    return check_alarm_b(
        side=SIDE_LONG,
        last_5m_close=101.0,
        last_5m_ema9=100.0,
        position_id=position_id,
        now_et=now_et,
    )


def _make_et(hour: int, minute: int = 0) -> datetime:
    """Return a timezone-aware ET datetime for today at hour:minute."""
    return datetime.now(tz=ET).replace(hour=hour, minute=minute, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# test 1: single cross does not fire
# ---------------------------------------------------------------------------

def test_single_cross_does_not_fire():
    """One bar with a cross condition must NOT fire under the v6.1.0 path."""
    reset_ema_cross_pending()
    orig = sentinel_mod._V610_EMA_CONFIRM_ENABLED
    sentinel_mod._V610_EMA_CONFIRM_ENABLED = True
    try:
        fired = _long_cross("pos1")
        assert fired == [], (
            "First cross bar should not fire; two consecutive bars are required"
        )
    finally:
        sentinel_mod._V610_EMA_CONFIRM_ENABLED = orig
        reset_ema_cross_pending()


# ---------------------------------------------------------------------------
# test 2: two consecutive crosses fire
# ---------------------------------------------------------------------------

def test_two_consecutive_crosses_fires():
    """Two consecutive cross bars must trigger exit on the second bar."""
    reset_ema_cross_pending()
    orig = sentinel_mod._V610_EMA_CONFIRM_ENABLED
    sentinel_mod._V610_EMA_CONFIRM_ENABLED = True
    orig_lunch = sentinel_mod._V610_LUNCH_SUPPRESSION_ENABLED
    sentinel_mod._V610_LUNCH_SUPPRESSION_ENABLED = False
    try:
        first = _long_cross("pos2")
        assert first == [], "First bar should not fire"
        second = _long_cross("pos2")
        assert len(second) == 1, "Second consecutive cross bar must fire"
        assert second[0].alarm == "B"
        assert second[0].reason == "sentinel_b_ema_cross"
    finally:
        sentinel_mod._V610_EMA_CONFIRM_ENABLED = orig
        sentinel_mod._V610_LUNCH_SUPPRESSION_ENABLED = orig_lunch
        reset_ema_cross_pending()


# ---------------------------------------------------------------------------
# test 3: cross, then revert, resets counter
# ---------------------------------------------------------------------------

def test_cross_then_revert_resets():
    """A non-cross bar resets the counter; the next cross is again bar 1."""
    reset_ema_cross_pending()
    orig = sentinel_mod._V610_EMA_CONFIRM_ENABLED
    sentinel_mod._V610_EMA_CONFIRM_ENABLED = True
    orig_lunch = sentinel_mod._V610_LUNCH_SUPPRESSION_ENABLED
    sentinel_mod._V610_LUNCH_SUPPRESSION_ENABLED = False
    try:
        _long_cross("pos3")
        _long_no_cross("pos3")
        assert sentinel_mod._ema_cross_pending.get("pos3", 0) == 0, (
            "Counter must be 0 after non-cross bar"
        )
        third = _long_cross("pos3")
        assert third == [], (
            "After a reset, the next cross is bar 1 again and must not fire"
        )
    finally:
        sentinel_mod._V610_EMA_CONFIRM_ENABLED = orig
        sentinel_mod._V610_LUNCH_SUPPRESSION_ENABLED = orig_lunch
        reset_ema_cross_pending()


# ---------------------------------------------------------------------------
# test 4: lunch window suppresses exit
# ---------------------------------------------------------------------------

def test_lunch_window_suppresses():
    """At 12:00 ET, two cross bars must NOT fire when suppression is enabled."""
    reset_ema_cross_pending()
    orig = sentinel_mod._V610_EMA_CONFIRM_ENABLED
    sentinel_mod._V610_EMA_CONFIRM_ENABLED = True
    orig_lunch = sentinel_mod._V610_LUNCH_SUPPRESSION_ENABLED
    sentinel_mod._V610_LUNCH_SUPPRESSION_ENABLED = True
    lunchtime = _make_et(12, 0)
    try:
        first = _long_cross("pos4", now_et=lunchtime)
        assert first == [], "First cross at lunch must not fire"
        second = _long_cross("pos4", now_et=lunchtime)
        assert second == [], (
            "Two crosses inside lunch window must be suppressed regardless of count"
        )
    finally:
        sentinel_mod._V610_EMA_CONFIRM_ENABLED = orig
        sentinel_mod._V610_LUNCH_SUPPRESSION_ENABLED = orig_lunch
        reset_ema_cross_pending()


# ---------------------------------------------------------------------------
# test 5: lunch suppression disabled flag allows exit
# ---------------------------------------------------------------------------

def test_lunch_suppression_disabled_flag():
    """With _V610_LUNCH_SUPPRESSION_ENABLED=False, two crosses at 12:00 ET fire."""
    reset_ema_cross_pending()
    orig = sentinel_mod._V610_EMA_CONFIRM_ENABLED
    sentinel_mod._V610_EMA_CONFIRM_ENABLED = True
    orig_lunch = sentinel_mod._V610_LUNCH_SUPPRESSION_ENABLED
    sentinel_mod._V610_LUNCH_SUPPRESSION_ENABLED = False
    lunchtime = _make_et(12, 0)
    try:
        first = _long_cross("pos5", now_et=lunchtime)
        assert first == [], "First cross should not fire"
        second = _long_cross("pos5", now_et=lunchtime)
        assert len(second) == 1, (
            "With suppression disabled, two crosses at noon must fire"
        )
        assert second[0].alarm == "B"
    finally:
        sentinel_mod._V610_EMA_CONFIRM_ENABLED = orig
        sentinel_mod._V610_LUNCH_SUPPRESSION_ENABLED = orig_lunch
        reset_ema_cross_pending()


# ---------------------------------------------------------------------------
# test 6: _V610_EMA_CONFIRM_ENABLED=False falls back to single-bar
# ---------------------------------------------------------------------------

def test_confirm_disabled_flag_falls_back():
    """When _V610_EMA_CONFIRM_ENABLED=False, a single cross bar fires immediately."""
    reset_ema_cross_pending()
    orig = sentinel_mod._V610_EMA_CONFIRM_ENABLED
    sentinel_mod._V610_EMA_CONFIRM_ENABLED = False
    try:
        fired = check_alarm_b(
            side=SIDE_LONG,
            last_5m_close=99.0,
            last_5m_ema9=100.0,
            position_id="pos6",
        )
        assert len(fired) == 1, (
            "With _V610_EMA_CONFIRM_ENABLED=False, single bar must fire immediately"
        )
        assert fired[0].alarm == "B"
        assert fired[0].reason == "sentinel_b_ema_cross"
    finally:
        sentinel_mod._V610_EMA_CONFIRM_ENABLED = orig
        reset_ema_cross_pending()
