"""v7.3.0 -- stop-price hysteresis unit tests.

Acceptance criteria for the hysteresis layer:
    1. Single-tick legacy path still works when feature flag is off OR
       when position_id / last_1m_close are missing.
    2. With hysteresis active, a single bar close beyond the stop does
       NOT fire; two consecutive closes DO fire.
    3. Deep-breach override fires immediately regardless of bar count.
    4. Counter resets when a close pulls back inside the stop.
    5. Counter does not double-count within the same bar (tick-by-tick
       calls within one bar increment at most once).
    6. Both LONG and SHORT sides behave symmetrically.
"""
from __future__ import annotations

import os
import importlib

import pytest


@pytest.fixture
def sentinel_module(monkeypatch):
    """Reload engine.sentinel with hysteresis enabled and 2-bar default."""
    monkeypatch.setenv("V730_STOP_HYSTERESIS_ENABLED", "1")
    monkeypatch.setenv("V730_STOP_HYSTERESIS_BARS", "2")
    monkeypatch.setenv("V730_STOP_DEEP_FRAC", "0.0075")
    import engine.sentinel as s
    importlib.reload(s)
    # Clear per-test state so positions don't leak between tests.
    s._stop_cross_pending.clear()
    s._stop_cross_last_ts.clear()
    return s


def test_single_tick_path_when_position_id_missing(sentinel_module):
    """Without position_id, the legacy single-tick path fires immediately."""
    fired = sentinel_module.check_alarm_a_stop_price(
        side="LONG",
        current_price=99.49,
        current_stop_price=99.50,
        entry_price=100.00,
    )
    assert len(fired) == 1
    assert fired[0].reason == "sentinel_a_stop_price"


def test_single_tick_path_when_last_1m_close_missing(sentinel_module):
    """With position_id but no 1m close, falls through to legacy path."""
    fired = sentinel_module.check_alarm_a_stop_price(
        side="LONG",
        current_price=99.49,
        current_stop_price=99.50,
        entry_price=100.00,
        position_id="LONG:AAPL:1",
    )
    assert len(fired) == 1


def test_long_two_bar_confirmation_required(sentinel_module):
    """LONG: two consecutive closes below stop required to fire."""
    pid = "LONG:AAPL:1"
    # Bar 1: close below stop -- should NOT fire (counter=1, need 2).
    fired_1 = sentinel_module.check_alarm_a_stop_price(
        side="LONG",
        current_price=99.49,
        current_stop_price=99.50,
        entry_price=100.00,
        position_id=pid,
        last_1m_close=99.49,
        last_1m_close_ts=1000.0,
    )
    assert len(fired_1) == 0
    assert sentinel_module._stop_cross_pending[pid] == 1

    # Bar 2: close still below stop -- should fire (counter=2).
    fired_2 = sentinel_module.check_alarm_a_stop_price(
        side="LONG",
        current_price=99.48,
        current_stop_price=99.50,
        entry_price=100.00,
        position_id=pid,
        last_1m_close=99.48,
        last_1m_close_ts=1060.0,
    )
    assert len(fired_2) == 1
    assert "hyst_bars=2" in fired_2[0].detail


def test_short_two_bar_confirmation_required(sentinel_module):
    """SHORT: two consecutive closes above stop required to fire."""
    pid = "SHORT:AAPL:1"
    fired_1 = sentinel_module.check_alarm_a_stop_price(
        side="SHORT",
        current_price=100.51,
        current_stop_price=100.50,
        entry_price=100.00,
        position_id=pid,
        last_1m_close=100.51,
        last_1m_close_ts=1000.0,
    )
    assert len(fired_1) == 0
    assert sentinel_module._stop_cross_pending[pid] == 1

    fired_2 = sentinel_module.check_alarm_a_stop_price(
        side="SHORT",
        current_price=100.52,
        current_stop_price=100.50,
        entry_price=100.00,
        position_id=pid,
        last_1m_close=100.52,
        last_1m_close_ts=1060.0,
    )
    assert len(fired_2) == 1
    assert "hyst_bars=2" in fired_2[0].detail


def test_deep_breach_long_fires_immediately(sentinel_module):
    """LONG: if mark drops past stop by >= 0.75% of entry, fire on first bar."""
    pid = "LONG:AAPL:deep"
    # entry=100, stop=99.50; deep threshold = 100 * 0.0075 = 0.75
    # cp=98.74 -> sp - cp = 0.76 >= 0.75 -> deep breach.
    fired = sentinel_module.check_alarm_a_stop_price(
        side="LONG",
        current_price=98.74,
        current_stop_price=99.50,
        entry_price=100.00,
        position_id=pid,
        last_1m_close=98.74,
        last_1m_close_ts=1000.0,
    )
    assert len(fired) == 1
    assert "deep_breach=1" in fired[0].detail


def test_deep_breach_short_fires_immediately(sentinel_module):
    """SHORT: if mark rises past stop by >= 0.75% of entry, fire on first bar."""
    pid = "SHORT:AAPL:deep"
    # entry=100, stop=100.50; cp=101.26 -> cp - sp = 0.76 >= 0.75
    fired = sentinel_module.check_alarm_a_stop_price(
        side="SHORT",
        current_price=101.26,
        current_stop_price=100.50,
        entry_price=100.00,
        position_id=pid,
        last_1m_close=101.26,
        last_1m_close_ts=1000.0,
    )
    assert len(fired) == 1
    assert "deep_breach=1" in fired[0].detail


def test_counter_resets_when_close_pulls_back(sentinel_module):
    """Counter resets when a close pulls back inside the stop."""
    pid = "LONG:AAPL:reset"
    # Bar 1: below stop -> counter=1
    sentinel_module.check_alarm_a_stop_price(
        side="LONG", current_price=99.49, current_stop_price=99.50,
        entry_price=100.00, position_id=pid,
        last_1m_close=99.49, last_1m_close_ts=1000.0,
    )
    assert sentinel_module._stop_cross_pending[pid] == 1

    # Bar 2: close pulls back ABOVE stop -> reset.
    fired_2 = sentinel_module.check_alarm_a_stop_price(
        side="LONG", current_price=99.55, current_stop_price=99.50,
        entry_price=100.00, position_id=pid,
        last_1m_close=99.55, last_1m_close_ts=1060.0,
    )
    assert len(fired_2) == 0
    # Live mark > stop AND close > stop: counter cleared.
    assert sentinel_module._stop_cross_pending.get(pid, 0) == 0

    # Bar 3: re-cross below stop -> counter=1 again, no fire yet.
    fired_3 = sentinel_module.check_alarm_a_stop_price(
        side="LONG", current_price=99.49, current_stop_price=99.50,
        entry_price=100.00, position_id=pid,
        last_1m_close=99.49, last_1m_close_ts=1120.0,
    )
    assert len(fired_3) == 0
    assert sentinel_module._stop_cross_pending[pid] == 1


def test_no_double_count_within_same_bar(sentinel_module):
    """Multiple ticks within the same 1m bar should increment counter once."""
    pid = "LONG:AAPL:nodouble"
    # First tick of bar at ts=1000.
    sentinel_module.check_alarm_a_stop_price(
        side="LONG", current_price=99.49, current_stop_price=99.50,
        entry_price=100.00, position_id=pid,
        last_1m_close=99.49, last_1m_close_ts=1000.0,
    )
    # Second tick of the SAME bar (same ts).
    sentinel_module.check_alarm_a_stop_price(
        side="LONG", current_price=99.48, current_stop_price=99.50,
        entry_price=100.00, position_id=pid,
        last_1m_close=99.49, last_1m_close_ts=1000.0,
    )
    # Third tick of the SAME bar.
    fired = sentinel_module.check_alarm_a_stop_price(
        side="LONG", current_price=99.47, current_stop_price=99.50,
        entry_price=100.00, position_id=pid,
        last_1m_close=99.49, last_1m_close_ts=1000.0,
    )
    # Should NOT have fired -- counter still at 1.
    assert len(fired) == 0
    assert sentinel_module._stop_cross_pending[pid] == 1


def test_feature_flag_disabled_restores_legacy(monkeypatch):
    """V730_STOP_HYSTERESIS_ENABLED=0 restores single-tick behavior."""
    monkeypatch.setenv("V730_STOP_HYSTERESIS_ENABLED", "0")
    import engine.sentinel as s
    importlib.reload(s)
    fired = s.check_alarm_a_stop_price(
        side="LONG", current_price=99.49, current_stop_price=99.50,
        entry_price=100.00, position_id="x",
        last_1m_close=99.49, last_1m_close_ts=1000.0,
    )
    # Hysteresis off -> single tick fires.
    assert len(fired) == 1


def test_hysteresis_bars_one_restores_legacy(monkeypatch):
    """V730_STOP_HYSTERESIS_BARS=1 also restores single-tick behavior."""
    monkeypatch.setenv("V730_STOP_HYSTERESIS_ENABLED", "1")
    monkeypatch.setenv("V730_STOP_HYSTERESIS_BARS", "1")
    import engine.sentinel as s
    importlib.reload(s)
    fired = s.check_alarm_a_stop_price(
        side="LONG", current_price=99.49, current_stop_price=99.50,
        entry_price=100.00, position_id="x",
        last_1m_close=99.49, last_1m_close_ts=1000.0,
    )
    assert len(fired) == 1


def test_no_fire_when_close_inside_stop_but_mark_outside(sentinel_module):
    """If live mark crosses but the 1m close did NOT, don't increment counter.

    This is the core anti-noise property: bid/ask flicker on the live mark
    that doesn't survive to the bar close should not count.
    """
    pid = "LONG:AAPL:flicker"
    # cp BELOW stop (live mark wicked down) but last_1m_close is ABOVE stop.
    fired = sentinel_module.check_alarm_a_stop_price(
        side="LONG", current_price=99.49, current_stop_price=99.50,
        entry_price=100.00, position_id=pid,
        last_1m_close=99.55, last_1m_close_ts=1000.0,
    )
    assert len(fired) == 0
    # Counter should not have advanced since the close wasn't beyond.
    assert sentinel_module._stop_cross_pending.get(pid, 0) == 0
