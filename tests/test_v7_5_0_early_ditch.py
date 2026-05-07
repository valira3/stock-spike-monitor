"""v7.5.0 \\u2014 Early-Ditch (Filter #3) unit tests.

Locks the contract:
  1. Flag OFF \\u2014 evaluator never emits the v750 alarm.
  2. Flag ON, in window, red enough \\u2014 alarm fires, full-exit set, reason wins.
  3. Flag ON, past window \\u2014 no fire.
  4. Flag ON, in window, not red enough \\u2014 no fire.
  5. Flag ON, in window, on the LONG side, deep red \\u2014 fires.
  6. Flag ON, in window, on the SHORT side, deep red \\u2014 fires.
  7. Flag ON, but entry_ts_utc missing \\u2014 silent no-op.
  8. Flag ON, but entry_ts_utc malformed \\u2014 silent no-op.
  9. Threshold is treated as ABS (caller-provided sign doesn't matter).
"""
from __future__ import annotations
import importlib
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def reload_modules(monkeypatch):
    """Reload v750_flags + sentinel after env mutation so module-level
    constants pick up the test's chosen values."""
    def _reload(env: dict[str, str]):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        # Drop and re-import in dependency order.
        for mod in ("engine.sentinel", "engine.v750_flags"):
            if mod in sys.modules:
                del sys.modules[mod]
        v750 = importlib.import_module("engine.v750_flags")
        sent = importlib.import_module("engine.sentinel")
        return v750, sent
    return _reload


def _entry_iso(now_dt: datetime, age_sec: float) -> str:
    return (now_dt - timedelta(seconds=age_sec)).isoformat()


def _now() -> datetime:
    return datetime(2026, 5, 7, 14, 30, 0, tzinfo=timezone.utc)


def test_flag_off_no_fire(reload_modules):
    _v, s = reload_modules({"V750_EARLY_DITCH_ENABLED": "0"})
    now_dt = _now()
    res = s.evaluate_sentinel(
        side=s.SIDE_LONG,
        unrealized_pnl=-50.0,
        position_value=10000.0,
        pnl_history=None,
        now_ts=now_dt.timestamp(),
        last_5m_close=None,
        last_5m_ema9=None,
        entry_ts_utc=_entry_iso(now_dt, 30.0),
    )
    assert "V750_EARLY_DITCH" not in [a.alarm for a in res.alarms]


def test_in_window_deep_red_long_fires(reload_modules):
    v, s = reload_modules({
        "V750_EARLY_DITCH_ENABLED": "1",
        "V750_EARLY_DITCH_WINDOW_SEC": "90",
        "V750_EARLY_DITCH_RED_DOLLARS": "10",
    })
    now_dt = _now()
    res = s.evaluate_sentinel(
        side=s.SIDE_LONG,
        unrealized_pnl=-15.0,
        position_value=10000.0,
        pnl_history=None,
        now_ts=now_dt.timestamp(),
        last_5m_close=None,
        last_5m_ema9=None,
        entry_ts_utc=_entry_iso(now_dt, 30.0),
    )
    codes = [a.alarm for a in res.alarms]
    assert "V750_EARLY_DITCH" in codes
    assert res.has_full_exit is True
    assert res.exit_reason == v.EXIT_REASON_V750_EARLY_DITCH


def test_in_window_deep_red_short_fires(reload_modules):
    v, s = reload_modules({
        "V750_EARLY_DITCH_ENABLED": "1",
        "V750_EARLY_DITCH_WINDOW_SEC": "90",
        "V750_EARLY_DITCH_RED_DOLLARS": "10",
    })
    now_dt = _now()
    res = s.evaluate_sentinel(
        side=s.SIDE_SHORT,
        unrealized_pnl=-25.0,
        position_value=10000.0,
        pnl_history=None,
        now_ts=now_dt.timestamp(),
        last_5m_close=None,
        last_5m_ema9=None,
        entry_ts_utc=_entry_iso(now_dt, 60.0),
    )
    assert "V750_EARLY_DITCH" in [a.alarm for a in res.alarms]


def test_past_window_no_fire(reload_modules):
    v, s = reload_modules({
        "V750_EARLY_DITCH_ENABLED": "1",
        "V750_EARLY_DITCH_WINDOW_SEC": "90",
        "V750_EARLY_DITCH_RED_DOLLARS": "10",
    })
    now_dt = _now()
    res = s.evaluate_sentinel(
        side=s.SIDE_LONG,
        unrealized_pnl=-100.0,  # very red
        position_value=10000.0,
        pnl_history=None,
        now_ts=now_dt.timestamp(),
        last_5m_close=None,
        last_5m_ema9=None,
        entry_ts_utc=_entry_iso(now_dt, 91.0),  # 1 sec past window
    )
    assert "V750_EARLY_DITCH" not in [a.alarm for a in res.alarms]


def test_in_window_not_red_enough_no_fire(reload_modules):
    v, s = reload_modules({
        "V750_EARLY_DITCH_ENABLED": "1",
        "V750_EARLY_DITCH_WINDOW_SEC": "90",
        "V750_EARLY_DITCH_RED_DOLLARS": "10",
    })
    now_dt = _now()
    res = s.evaluate_sentinel(
        side=s.SIDE_LONG,
        unrealized_pnl=-9.99,
        position_value=10000.0,
        pnl_history=None,
        now_ts=now_dt.timestamp(),
        last_5m_close=None,
        last_5m_ema9=None,
        entry_ts_utc=_entry_iso(now_dt, 30.0),
    )
    assert "V750_EARLY_DITCH" not in [a.alarm for a in res.alarms]


def test_in_window_green_no_fire(reload_modules):
    v, s = reload_modules({"V750_EARLY_DITCH_ENABLED": "1"})
    now_dt = _now()
    res = s.evaluate_sentinel(
        side=s.SIDE_LONG,
        unrealized_pnl=+25.0,
        position_value=10000.0,
        pnl_history=None,
        now_ts=now_dt.timestamp(),
        last_5m_close=None,
        last_5m_ema9=None,
        entry_ts_utc=_entry_iso(now_dt, 30.0),
    )
    assert "V750_EARLY_DITCH" not in [a.alarm for a in res.alarms]


def test_entry_ts_missing_no_fire(reload_modules):
    v, s = reload_modules({"V750_EARLY_DITCH_ENABLED": "1"})
    now_dt = _now()
    res = s.evaluate_sentinel(
        side=s.SIDE_LONG,
        unrealized_pnl=-50.0,
        position_value=10000.0,
        pnl_history=None,
        now_ts=now_dt.timestamp(),
        last_5m_close=None,
        last_5m_ema9=None,
        entry_ts_utc=None,
    )
    assert "V750_EARLY_DITCH" not in [a.alarm for a in res.alarms]


def test_entry_ts_malformed_no_crash(reload_modules):
    v, s = reload_modules({"V750_EARLY_DITCH_ENABLED": "1"})
    now_dt = _now()
    res = s.evaluate_sentinel(
        side=s.SIDE_LONG,
        unrealized_pnl=-50.0,
        position_value=10000.0,
        pnl_history=None,
        now_ts=now_dt.timestamp(),
        last_5m_close=None,
        last_5m_ema9=None,
        entry_ts_utc="not-a-date",
    )
    assert "V750_EARLY_DITCH" not in [a.alarm for a in res.alarms]


def test_threshold_is_abs_value(reload_modules):
    """V750_EARLY_DITCH_RED_DOLLARS is taken as |x|; users may pass it as
    +10 (the dollar magnitude) and the comparator still works."""
    v, s = reload_modules({
        "V750_EARLY_DITCH_ENABLED": "1",
        "V750_EARLY_DITCH_RED_DOLLARS": "10",  # positive magnitude
    })
    now_dt = _now()
    res = s.evaluate_sentinel(
        side=s.SIDE_LONG,
        unrealized_pnl=-10.01,
        position_value=10000.0,
        pnl_history=None,
        now_ts=now_dt.timestamp(),
        last_5m_close=None,
        last_5m_ema9=None,
        entry_ts_utc=_entry_iso(now_dt, 5.0),
    )
    assert "V750_EARLY_DITCH" in [a.alarm for a in res.alarms]


def test_priority_v750_wins_over_a_stop(reload_modules):
    """If both v750 and the price-stop fire on the same tick, v750
    should win the canonical exit_reason because it carries the
    cleanest attribution for an instant-stop-out."""
    v, s = reload_modules({"V750_EARLY_DITCH_ENABLED": "1"})
    now_dt = _now()
    # Long entry at 100, current at 95 \\u2014 price stop at 99.5 would also fire.
    res = s.evaluate_sentinel(
        side=s.SIDE_LONG,
        unrealized_pnl=-50.0,
        position_value=10000.0,
        pnl_history=None,
        now_ts=now_dt.timestamp(),
        last_5m_close=None,
        last_5m_ema9=None,
        current_price=95.0,
        entry_price=100.0,
        current_stop_price=99.5,
        current_shares=10,
        entry_ts_utc=_entry_iso(now_dt, 10.0),
    )
    assert res.has_full_exit
    assert res.exit_reason == v.EXIT_REASON_V750_EARLY_DITCH
