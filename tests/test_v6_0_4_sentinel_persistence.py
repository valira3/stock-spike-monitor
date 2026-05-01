# tests/test_v6_0_4_sentinel_persistence.py
# v6.0.4 -- Sentinel persistence rehydration hotfix.
#
# Background: paper_state.save_paper_state used to round-trip pnl_history
# (a deque) and trail_state (a TrailState dataclass) through json.dump
# with default=str. The default=str callback stringified them on save
# and json.load returned strings on load, so the next Sentinel tick blew
# up on history.append and Alarms A/B/C/F never ran.
#
# v6.0.4 fixes this in two layers:
#   (1) STRIP-ON-SAVE  -- _strip_runtime_caches drops the runtime caches
#       from the snapshot dict before json.dump is called.
#   (2) REHYDRATE-ON-LOAD -- _rehydrate_runtime_caches converts any str
#       remnants from older saves back into fresh deque / TrailState.
#
# These tests exercise the helpers directly so they remain meaningful
# even if the live save_paper_state code path is hard to spin up under
# pytest. Plus a guard test confirming the runtime cache keys list is
# correct.
#
# No em-dashes in this file (constraint for .py test files).
from __future__ import annotations

import collections
import json

from engine.alarm_f_trail import TrailState
from engine.sentinel import new_pnl_history
from paper_state import (
    _RUNTIME_CACHE_KEYS,
    _rehydrate_runtime_caches,
    _strip_runtime_caches,
)


def test_runtime_cache_keys_list_is_canonical():
    # Adding to this set requires updating both helpers; pin the contract.
    assert _RUNTIME_CACHE_KEYS == (
        "pnl_history",
        "trail_state",
        "v531_prior_alarm_codes",
    )


def test_strip_runtime_caches_removes_pnl_history():
    pos_map = {
        "TSLA": {
            "entry_price": 100.0,
            "shares": 10,
            "pnl_history": new_pnl_history(),
        }
    }
    out = _strip_runtime_caches(pos_map)
    assert "pnl_history" not in out["TSLA"]
    assert out["TSLA"]["entry_price"] == 100.0
    # Original dict is untouched (engine still uses it live).
    assert "pnl_history" in pos_map["TSLA"]


def test_strip_runtime_caches_removes_trail_state():
    pos_map = {
        "NVDA": {
            "entry_price": 200.0,
            "shares": 25,
            "trail_state": TrailState.fresh(),
        }
    }
    out = _strip_runtime_caches(pos_map)
    assert "trail_state" not in out["NVDA"]
    assert "trail_state" in pos_map["NVDA"]


def test_strip_runtime_caches_removes_v531_prior_alarm_codes():
    pos_map = {
        "NFLX": {
            "entry_price": 90.0,
            "shares": 100,
            "v531_prior_alarm_codes": ["A", "B"],
        }
    }
    out = _strip_runtime_caches(pos_map)
    assert "v531_prior_alarm_codes" not in out["NFLX"]


def test_strip_runtime_caches_handles_empty_and_none():
    assert _strip_runtime_caches({}) == {}
    assert _strip_runtime_caches(None) == {}


def test_strip_runtime_caches_output_is_json_serializable():
    # The whole point of stripping is to make the snapshot survive
    # json.dump -- if any deque or TrailState escaped, json.dumps would
    # raise TypeError without default= set.
    pos_map = {
        "TSLA": {
            "entry_price": 100.0,
            "shares": 10,
            "pnl_history": new_pnl_history(),
            "trail_state": TrailState.fresh(),
            "v531_prior_alarm_codes": ["F"],
            "stop": 99.5,
        }
    }
    out = _strip_runtime_caches(pos_map)
    # No default= callback -- must be naturally JSON-friendly.
    serialized = json.dumps(out)
    assert "pnl_history" not in serialized
    assert "trail_state" not in serialized


def test_rehydrate_runtime_caches_repairs_string_pnl_history():
    pos_map = {
        "TSLA": {
            "entry_price": 100.0,
            "shares": 10,
            # The exact shape v6.0.3 left on disk after default=str.
            "pnl_history": "deque([(1.0, 0.5), (2.0, 1.5)], maxlen=120)",
        }
    }
    _rehydrate_runtime_caches(pos_map)
    ph = pos_map["TSLA"]["pnl_history"]
    assert isinstance(ph, collections.deque)
    # Helper repairs the type but discards the corrupted samples (a
    # short-lived gap in velocity history is acceptable; a wedged
    # AttributeError every tick is not).
    assert len(ph) == 0
    # And it must accept .append again (the original failure mode).
    ph.append((3.0, 2.5))
    assert len(ph) == 1


def test_rehydrate_runtime_caches_repairs_string_trail_state():
    pos_map = {
        "TSLA": {
            "entry_price": 100.0,
            "shares": 10,
            "trail_state": (
                "TrailState(stage=0, peak_close=None, "
                "stage2_arm_favorable=None, stage2_arm_atr=None, "
                "last_proposed_stop=None, bars_seen=5)"
            ),
        }
    }
    _rehydrate_runtime_caches(pos_map)
    ts = pos_map["TSLA"]["trail_state"]
    assert isinstance(ts, TrailState)
    # Reset to fresh -- no half-parsed state leaking through.
    assert ts.stage == 0
    assert ts.bars_seen == 0
    assert ts.peak_close is None


def test_rehydrate_runtime_caches_repairs_missing_caches():
    # Older saves (before stripping landed) plus brand-new positions
    # might omit these keys entirely. Helper installs fresh objects.
    pos_map = {"GOOG": {"entry_price": 150.0, "shares": 5}}
    _rehydrate_runtime_caches(pos_map)
    assert isinstance(pos_map["GOOG"]["pnl_history"], collections.deque)
    assert isinstance(pos_map["GOOG"]["trail_state"], TrailState)


def test_rehydrate_runtime_caches_preserves_live_objects():
    # Live objects (correctly typed) MUST NOT be replaced -- replacing
    # them mid-session would wipe in-flight Alarm F stage progression.
    live_history = new_pnl_history()
    live_history.append((1.0, 5.0))
    live_trail = TrailState(stage=2, peak_close=105.0, bars_seen=20)
    pos_map = {
        "TSLA": {
            "entry_price": 100.0,
            "shares": 10,
            "pnl_history": live_history,
            "trail_state": live_trail,
        }
    }
    _rehydrate_runtime_caches(pos_map)
    assert pos_map["TSLA"]["pnl_history"] is live_history
    assert pos_map["TSLA"]["trail_state"] is live_trail
    # Live trail state retains its stage / peak / bars_seen.
    assert pos_map["TSLA"]["trail_state"].stage == 2
    assert pos_map["TSLA"]["trail_state"].peak_close == 105.0


def test_rehydrate_runtime_caches_handles_empty_and_none():
    # No-op on empty / None pos_map.
    _rehydrate_runtime_caches({})
    _rehydrate_runtime_caches(None)


def test_rehydrate_repairs_v531_prior_alarm_codes_string():
    pos_map = {
        "TSLA": {
            "entry_price": 100.0,
            "shares": 10,
            "v531_prior_alarm_codes": "['A', 'B']",
        }
    }
    _rehydrate_runtime_caches(pos_map)
    assert pos_map["TSLA"]["v531_prior_alarm_codes"] == []


def test_save_load_round_trip_against_string_corruption():
    # End-to-end: simulate the exact corruption v6.0.3 produced.
    # 1) Live position with deque + TrailState.
    live = {
        "TSLA": {
            "entry_price": 392.31,
            "shares": 12,
            "stop": 390.35,
            "pnl_history": new_pnl_history(),
            "trail_state": TrailState.fresh(),
        }
    }
    live["TSLA"]["pnl_history"].append((1.0, 0.0))
    # 2) Strip and serialize the way save_paper_state now does.
    stripped = _strip_runtime_caches(live)
    blob = json.dumps(stripped)
    # 3) Round-trip and confirm the runtime caches are gone (not
    #    stringified -- gone outright).
    reloaded = json.loads(blob)
    assert "pnl_history" not in reloaded["TSLA"]
    assert "trail_state" not in reloaded["TSLA"]
    # 4) Rehydrate replenishes both as live objects.
    _rehydrate_runtime_caches(reloaded)
    assert isinstance(reloaded["TSLA"]["pnl_history"], collections.deque)
    assert isinstance(reloaded["TSLA"]["trail_state"], TrailState)
    # 5) The first Sentinel tick after restart must succeed -- no
    #    AttributeError on history.append (the original v6.0.3 failure).
    reloaded["TSLA"]["pnl_history"].append((2.0, 1.0))
    assert len(reloaded["TSLA"]["pnl_history"]) == 1


def test_sentinel_critical_log_dedup_keys():
    # Defensive smoke check: the broker.positions module exposes the
    # set used to dedup [SENTINEL][CRITICAL] log lines. Exists so we
    # can clear it from teardown logic in future tests if needed.
    from broker import positions as bp

    assert hasattr(bp, "_sentinel_critical_seen")
    assert isinstance(bp._sentinel_critical_seen, set)
