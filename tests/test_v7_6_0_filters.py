"""Tests for v7.6.0 Filter #7 (post-v750 cooldown) and Filter #8 (opening delay).

Both filters are pure flag-module helpers; integration with broker.orders
is covered by tracing the entry path manually but isolated unit tests are
sufficient guarantees that the gate logic itself is correct.
"""
from __future__ import annotations

import os
import sys
import importlib
from datetime import datetime, time, timedelta, timezone


# --------------------------------------------------------------------------- helpers


def _reload_v770(env_overrides=None):
    """Reload the v770_flags module with a fresh registry and env overrides."""
    if env_overrides is None:
        env_overrides = {}
    for k in [
        "V770_POST_DITCH_COOLDOWN_ENABLED",
        "V770_POST_DITCH_COOLDOWN_MIN",
    ]:
        if k in env_overrides:
            os.environ[k] = str(env_overrides[k])
        else:
            os.environ.pop(k, None)
    if "engine.v770_flags" in sys.modules:
        del sys.modules["engine.v770_flags"]
    return importlib.import_module("engine.v770_flags")


def _reload_v780(env_overrides=None):
    if env_overrides is None:
        env_overrides = {}
    for k in [
        "V780_OPENING_DELAY_ENABLED",
        "V780_OPENING_DELAY_UNTIL_ET",
    ]:
        if k in env_overrides:
            os.environ[k] = str(env_overrides[k])
        else:
            os.environ.pop(k, None)
    if "engine.v780_flags" in sys.modules:
        del sys.modules["engine.v780_flags"]
    return importlib.import_module("engine.v780_flags")


# --------------------------------------------------------------------------- v7.7.0 tests


def test_v770_disabled_by_default_is_no_op():
    m = _reload_v770()
    assert m.V770_POST_DITCH_COOLDOWN_ENABLED is False
    m.record_v750_fire("ORCL", "LONG", "2026-05-08T15:21:00+00:00")
    blocked, remaining = m.is_in_cooldown(
        "ORCL", "LONG", datetime(2026, 5, 8, 15, 25, tzinfo=timezone.utc)
    )
    assert blocked is False
    assert remaining is None


def test_v770_blocks_same_ticker_same_side_within_window():
    m = _reload_v770({"V770_POST_DITCH_COOLDOWN_ENABLED": "1"})
    m.record_v750_fire("ORCL", "LONG", "2026-05-08T15:21:00+00:00")
    # 30 minutes later \u2014 still in 30-min window? No, exactly at window edge.
    # Test 5 min after: clearly inside.
    blocked, remaining = m.is_in_cooldown(
        "ORCL", "LONG", datetime(2026, 5, 8, 15, 26, tzinfo=timezone.utc)
    )
    assert blocked is True
    assert remaining is not None and remaining > 0


def test_v770_does_not_block_opposite_side():
    m = _reload_v770({"V770_POST_DITCH_COOLDOWN_ENABLED": "1"})
    m.record_v750_fire("ORCL", "LONG", "2026-05-08T15:21:00+00:00")
    blocked, _ = m.is_in_cooldown(
        "ORCL", "SHORT", datetime(2026, 5, 8, 15, 26, tzinfo=timezone.utc)
    )
    assert blocked is False


def test_v770_does_not_block_other_ticker():
    m = _reload_v770({"V770_POST_DITCH_COOLDOWN_ENABLED": "1"})
    m.record_v750_fire("ORCL", "LONG", "2026-05-08T15:21:00+00:00")
    blocked, _ = m.is_in_cooldown(
        "TSLA", "LONG", datetime(2026, 5, 8, 15, 26, tzinfo=timezone.utc)
    )
    assert blocked is False


def test_v770_releases_after_window():
    m = _reload_v770({"V770_POST_DITCH_COOLDOWN_ENABLED": "1"})
    m.record_v750_fire("ORCL", "LONG", "2026-05-08T15:21:00+00:00")
    # 30 min later == window edge, should release (>= window)
    blocked, _ = m.is_in_cooldown(
        "ORCL", "LONG", datetime(2026, 5, 8, 15, 51, 0, tzinfo=timezone.utc)
    )
    assert blocked is False
    # 31 min later: clearly out
    blocked, _ = m.is_in_cooldown(
        "ORCL", "LONG", datetime(2026, 5, 8, 15, 52, 0, tzinfo=timezone.utc)
    )
    assert blocked is False


def test_v770_custom_window_minutes():
    m = _reload_v770(
        {"V770_POST_DITCH_COOLDOWN_ENABLED": "1", "V770_POST_DITCH_COOLDOWN_MIN": "10"}
    )
    m.record_v750_fire("ORCL", "LONG", "2026-05-08T15:21:00+00:00")
    # 9 min later: still in
    blocked, _ = m.is_in_cooldown(
        "ORCL", "LONG", datetime(2026, 5, 8, 15, 30, 0, tzinfo=timezone.utc)
    )
    assert blocked is True
    # 11 min later: out
    blocked, _ = m.is_in_cooldown(
        "ORCL", "LONG", datetime(2026, 5, 8, 15, 32, 0, tzinfo=timezone.utc)
    )
    assert blocked is False


def test_v770_record_extends_window_on_later_fire():
    m = _reload_v770({"V770_POST_DITCH_COOLDOWN_ENABLED": "1"})
    m.record_v750_fire("ORCL", "LONG", "2026-05-08T15:21:00+00:00")
    # 25 min later, fire again
    m.record_v750_fire("ORCL", "LONG", "2026-05-08T15:46:00+00:00")
    # 50 min after first fire (= 25 min after second): still in cooldown
    blocked, _ = m.is_in_cooldown(
        "ORCL", "LONG", datetime(2026, 5, 8, 16, 11, 0, tzinfo=timezone.utc)
    )
    assert blocked is True


def test_v770_unparseable_ts_is_safe():
    m = _reload_v770({"V770_POST_DITCH_COOLDOWN_ENABLED": "1"})
    # Should silently no-op
    m.record_v750_fire("ORCL", "LONG", "not-a-timestamp")
    m.record_v750_fire("", "LONG", "2026-05-08T15:21:00+00:00")
    m.record_v750_fire("ORCL", "", "2026-05-08T15:21:00+00:00")
    m.record_v750_fire(None, "LONG", "2026-05-08T15:21:00+00:00")
    blocked, _ = m.is_in_cooldown(
        "ORCL", "LONG", datetime(2026, 5, 8, 15, 26, tzinfo=timezone.utc)
    )
    assert blocked is False


# --------------------------------------------------------------------------- v7.8.0 tests


def test_v780_disabled_by_default_is_no_op():
    m = _reload_v780()
    assert m.V780_OPENING_DELAY_ENABLED is False
    blocked, _ = m.is_before_open_delay(
        datetime(2026, 5, 8, 13, 30, tzinfo=timezone.utc)  # 09:30 ET EDT
    )
    assert blocked is False


def test_v780_blocks_before_945_et_default():
    m = _reload_v780({"V780_OPENING_DELAY_ENABLED": "1"})
    # 09:30 ET = 13:30 UTC during EDT
    blocked, et_str = m.is_before_open_delay(
        datetime(2026, 5, 8, 13, 30, tzinfo=timezone.utc)
    )
    assert blocked is True
    assert et_str is not None and et_str.startswith("09:30")
    # 09:44 ET still blocked
    blocked, _ = m.is_before_open_delay(
        datetime(2026, 5, 8, 13, 44, tzinfo=timezone.utc)
    )
    assert blocked is True


def test_v780_allows_at_and_after_945_et():
    m = _reload_v780({"V780_OPENING_DELAY_ENABLED": "1"})
    # Exactly 09:45 ET = 13:45 UTC EDT
    blocked, _ = m.is_before_open_delay(
        datetime(2026, 5, 8, 13, 45, tzinfo=timezone.utc)
    )
    assert blocked is False
    # 09:46 ET clearly out
    blocked, _ = m.is_before_open_delay(
        datetime(2026, 5, 8, 13, 46, tzinfo=timezone.utc)
    )
    assert blocked is False
    # 14:00 ET clearly out
    blocked, _ = m.is_before_open_delay(
        datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc)
    )
    assert blocked is False


def test_v780_custom_cutoff():
    m = _reload_v780(
        {"V780_OPENING_DELAY_ENABLED": "1", "V780_OPENING_DELAY_UNTIL_ET": "10:00"}
    )
    # 09:55 ET still blocked
    blocked, _ = m.is_before_open_delay(
        datetime(2026, 5, 8, 13, 55, tzinfo=timezone.utc)
    )
    assert blocked is True
    # 10:00 ET allowed
    blocked, _ = m.is_before_open_delay(
        datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc)
    )
    assert blocked is False


def test_v780_handles_naive_datetime_as_utc():
    m = _reload_v780({"V780_OPENING_DELAY_ENABLED": "1"})
    blocked, _ = m.is_before_open_delay(datetime(2026, 5, 8, 13, 30))
    assert blocked is True


def test_v780_iso_string_input():
    m = _reload_v780({"V780_OPENING_DELAY_ENABLED": "1"})
    blocked, _ = m.is_before_open_delay("2026-05-08T13:30:00+00:00")
    assert blocked is True
    blocked, _ = m.is_before_open_delay("2026-05-08T13:46:00+00:00")
    assert blocked is False


def test_v780_malformed_input_does_not_block():
    m = _reload_v780({"V780_OPENING_DELAY_ENABLED": "1"})
    blocked, et = m.is_before_open_delay("not-a-time")
    assert blocked is False
    assert et is None
