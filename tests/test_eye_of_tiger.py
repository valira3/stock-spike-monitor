"""v5.10.0 Project Eye of the Tiger \u2014 unit tests for Sections I\u2013VI.

Pure-function tests over plain dicts. No external dependencies. Run with:

    pytest tests/test_eye_of_tiger.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, time
from pathlib import Path

# Allow `import eye_of_tiger` from repo root when running via pytest.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import eye_of_tiger as eot


# ---------------------------------------------------------------------
# Section I \u2014 Global Permit
# ---------------------------------------------------------------------


def test_permit_long_open_when_above_ema_and_avwap():
    r = eot.evaluate_global_permit(
        eot.SIDE_LONG,
        qqq_5m_close=400.0,
        qqq_5m_ema9=399.0,
        qqq_current_price=400.5,
        qqq_avwap_0930=399.5,
    )
    assert r["open"] is True


def test_permit_long_closed_when_below_avwap():
    r = eot.evaluate_global_permit(
        eot.SIDE_LONG,
        qqq_5m_close=400.0,
        qqq_5m_ema9=399.0,
        qqq_current_price=399.0,
        qqq_avwap_0930=399.5,
    )
    assert r["open"] is False
    assert r["reason"] == "anchor_misaligned"


def test_permit_short_open_when_below_both():
    r = eot.evaluate_global_permit(
        eot.SIDE_SHORT,
        qqq_5m_close=399.0,
        qqq_5m_ema9=400.0,
        qqq_current_price=398.5,
        qqq_avwap_0930=399.5,
    )
    assert r["open"] is True


def test_permit_data_missing_closes_gate():
    r = eot.evaluate_global_permit(
        eot.SIDE_LONG,
        qqq_5m_close=None,
        qqq_5m_ema9=399.0,
        qqq_current_price=400.0,
        qqq_avwap_0930=399.5,
    )
    assert r["open"] is False
    assert r["reason"] == "data_missing"


# ---------------------------------------------------------------------
# Section II.2 \u2014 Boundary Hold
# ---------------------------------------------------------------------


def test_boundary_hold_long_satisfied_with_two_outside():
    r = eot.evaluate_boundary_hold(
        eot.SIDE_LONG, or_high=100.0, or_low=99.0, last_n_1m_closes=[100.5, 100.7]
    )
    assert r["hold"] is True


def test_boundary_hold_long_one_close_insufficient():
    r = eot.evaluate_boundary_hold(
        eot.SIDE_LONG, or_high=100.0, or_low=99.0, last_n_1m_closes=[99.9, 100.5]
    )
    assert r["hold"] is False


def test_boundary_hold_long_equality_breaks_hold():
    # close exactly at OR_High should NOT count (strict >)
    r = eot.evaluate_boundary_hold(
        eot.SIDE_LONG, or_high=100.0, or_low=99.0, last_n_1m_closes=[100.5, 100.0]
    )
    assert r["hold"] is False


def test_boundary_hold_short_mirror():
    r = eot.evaluate_boundary_hold(
        eot.SIDE_SHORT, or_high=100.0, or_low=99.0, last_n_1m_closes=[98.5, 98.4]
    )
    assert r["hold"] is True


def test_boundary_hold_earliest_satisfaction_time_is_0936():
    # v15.0 SPEC: ORH/ORL freeze at 09:35:59 ET. With
    # BOUNDARY_HOLD_REQUIRED_CLOSES=2, the first 1m close strictly
    # after the OR freeze is the 09:36 candle (close at 09:36:59);
    # the second qualifying close is the 09:37 candle. So the
    # earliest theoretical satisfaction wall-clock time is 09:37.
    t = eot.boundary_hold_earliest_satisfaction_et()
    assert t == time(9, 37, 0)


def test_boundary_hold_or_not_set():
    r = eot.evaluate_boundary_hold(
        eot.SIDE_LONG, or_high=None, or_low=None, last_n_1m_closes=[100.5, 100.7]
    )
    assert r["hold"] is False
    assert r["reason"] == "or_not_set"


# ---------------------------------------------------------------------
# Section III \u2014 Entry triggers
# ---------------------------------------------------------------------


def test_entry_1_long_full_stack_passes():
    r = eot.evaluate_entry_1(
        eot.SIDE_LONG,
        permit_open=True,
        volume_bucket_ok=True,
        boundary_hold_ok=True,
        di_5m=27.0,
        di_1m=26.0,
        is_nhod_or_nlod=True,
    )
    assert r["fire"] is True


def test_entry_1_no_nhod_fails():
    r = eot.evaluate_entry_1(
        eot.SIDE_LONG,
        permit_open=True,
        volume_bucket_ok=True,
        boundary_hold_ok=True,
        di_5m=27.0,
        di_1m=26.0,
        is_nhod_or_nlod=False,
    )
    assert r["fire"] is False
    assert r["reason"] == "no_extreme_print"


def test_entry_1_di_5m_24_fails():
    r = eot.evaluate_entry_1(
        eot.SIDE_LONG,
        permit_open=True,
        volume_bucket_ok=True,
        boundary_hold_ok=True,
        di_5m=24.0,
        di_1m=26.0,
        is_nhod_or_nlod=True,
    )
    assert r["fire"] is False
    assert r["reason"] == "di_5m"


def test_entry_1_di_5m_25_exactly_fails_strict():
    r = eot.evaluate_entry_1(
        eot.SIDE_LONG,
        permit_open=True,
        volume_bucket_ok=True,
        boundary_hold_ok=True,
        di_5m=25.0,
        di_1m=26.0,
        is_nhod_or_nlod=True,
    )
    assert r["fire"] is False


def test_entry_1_short_mirror_passes():
    r = eot.evaluate_entry_1(
        eot.SIDE_SHORT,
        permit_open=True,
        volume_bucket_ok=True,
        boundary_hold_ok=True,
        di_5m=28.0,
        di_1m=26.5,
        is_nhod_or_nlod=True,
    )
    assert r["fire"] is True


def test_entry_1_blocked_by_volume_bucket():
    r = eot.evaluate_entry_1(
        eot.SIDE_LONG,
        permit_open=True,
        volume_bucket_ok=False,
        boundary_hold_ok=True,
        di_5m=27.0,
        di_1m=26.0,
        is_nhod_or_nlod=True,
    )
    assert r["fire"] is False
    assert r["reason"] == "volume_bucket"


def test_entry_2_crossing_edge_with_fresh_nhod_fires():
    r = eot.evaluate_entry_2(
        eot.SIDE_LONG,
        entry_1_active=True,
        permit_open_at_trigger=True,
        di_1m_prev=29.5,
        di_1m_now=30.5,
        fresh_nhod_or_nlod=True,
        entry_2_already_fired=False,
    )
    assert r["fire"] is True


def test_entry_2_no_fresh_nhod_fails():
    r = eot.evaluate_entry_2(
        eot.SIDE_LONG,
        entry_1_active=True,
        permit_open_at_trigger=True,
        di_1m_prev=29.5,
        di_1m_now=30.5,
        fresh_nhod_or_nlod=False,
        entry_2_already_fired=False,
    )
    assert r["fire"] is False
    assert r["reason"] == "no_fresh_extreme"


def test_entry_2_sustained_di_without_crossing_fails():
    # DI was already >30 at Entry 1 time; no new crossing edge
    r = eot.evaluate_entry_2(
        eot.SIDE_LONG,
        entry_1_active=True,
        permit_open_at_trigger=True,
        di_1m_prev=31.0,
        di_1m_now=32.0,
        fresh_nhod_or_nlod=True,
        entry_2_already_fired=False,
    )
    assert r["fire"] is False
    assert r["reason"] == "no_crossing"


def test_entry_2_never_reaches_30_fails():
    r = eot.evaluate_entry_2(
        eot.SIDE_LONG,
        entry_1_active=True,
        permit_open_at_trigger=True,
        di_1m_prev=27.0,
        di_1m_now=29.5,
        fresh_nhod_or_nlod=True,
        entry_2_already_fired=False,
    )
    assert r["fire"] is False
    assert r["reason"] == "no_crossing"


def test_entry_2_fires_only_once():
    r = eot.evaluate_entry_2(
        eot.SIDE_LONG,
        entry_1_active=True,
        permit_open_at_trigger=True,
        di_1m_prev=29.5,
        di_1m_now=30.5,
        fresh_nhod_or_nlod=True,
        entry_2_already_fired=True,
    )
    assert r["fire"] is False
    assert r["reason"] == "already_fired"


def test_fresh_nhod_helper():
    assert eot.is_fresh_nhod(101.0, 100.5) is True
    assert eot.is_fresh_nhod(100.5, 100.5) is False  # equality not strict
    assert eot.is_fresh_nhod(100.0, 100.5) is False


def test_fresh_nlod_helper():
    assert eot.is_fresh_nlod(99.0, 99.5) is True
    assert eot.is_fresh_nlod(99.5, 99.5) is False


# ---------------------------------------------------------------------
# Section IV \u2014 Tick overrides
# ---------------------------------------------------------------------


def test_sovereign_brake_fires_at_minus_500_01():
    assert eot.evaluate_sovereign_brake(-500.01) is True


def test_sovereign_brake_does_not_fire_at_minus_499_99():
    assert eot.evaluate_sovereign_brake(-499.99) is False


def test_sovereign_brake_fires_at_exactly_minus_500():
    # threshold uses <=, so exactly -$500 fires (per spec rule "loss reaches -$500")
    assert eot.evaluate_sovereign_brake(-500.0) is True


def test_velocity_fuse_long_at_1_011_pct_fires():
    op = 100.0
    cp = op * (1 - 0.01011)
    assert eot.evaluate_velocity_fuse(eot.SIDE_LONG, current_price=cp, current_1m_open=op) is True


def test_velocity_fuse_long_at_exactly_1_pct_does_not_fire():
    # strict <
    op = 100.0
    cp = op * (1 - 0.01)  # exactly 1.0%
    assert eot.evaluate_velocity_fuse(eot.SIDE_LONG, current_price=cp, current_1m_open=op) is False


def test_velocity_fuse_short_at_1_5_pct_fires():
    op = 100.0
    cp = op * (1 + 0.015)
    assert eot.evaluate_velocity_fuse(eot.SIDE_SHORT, current_price=cp, current_1m_open=op) is True


# ---------------------------------------------------------------------
# Section V \u2014 Stops
# ---------------------------------------------------------------------


def test_maffei_long_inside_or_lower_low_exits():
    # Candle: opens above OR_High, closes back inside (re-entry); current_low < prior_low
    r = eot.evaluate_maffei_inside_or(
        eot.SIDE_LONG,
        or_high=100.0,
        or_low=99.0,
        current_1m_open=100.2,
        current_1m_close=99.8,
        current_1m_low=99.5,
        current_1m_high=100.3,
        prior_1m_low=99.7,
        prior_1m_high=100.4,
    )
    assert r["gated"] is True and r["decision"] == "EXIT"


def test_maffei_long_inside_or_higher_low_holds():
    r = eot.evaluate_maffei_inside_or(
        eot.SIDE_LONG,
        or_high=100.0,
        or_low=99.0,
        current_1m_open=100.2,
        current_1m_close=99.8,
        current_1m_low=99.7,
        current_1m_high=100.3,
        prior_1m_low=99.5,
        prior_1m_high=100.4,
    )
    assert r["gated"] is True and r["decision"] == "STAY"


def test_maffei_long_inside_or_equal_low_holds():
    r = eot.evaluate_maffei_inside_or(
        eot.SIDE_LONG,
        or_high=100.0,
        or_low=99.0,
        current_1m_open=100.2,
        current_1m_close=99.8,
        current_1m_low=99.5,
        current_1m_high=100.3,
        prior_1m_low=99.5,
        prior_1m_high=100.4,
    )
    assert r["gated"] is True and r["decision"] == "STAY"


def test_maffei_outside_or_no_gate():
    # Candle stayed above OR_High the whole time -> gate not triggered
    r = eot.evaluate_maffei_inside_or(
        eot.SIDE_LONG,
        or_high=100.0,
        or_low=99.0,
        current_1m_open=100.2,
        current_1m_close=100.5,
        current_1m_low=100.1,
        current_1m_high=100.7,
        prior_1m_low=100.0,
        prior_1m_high=100.4,
    )
    assert r["gated"] is False


def test_maffei_short_inside_or_higher_high_exits():
    r = eot.evaluate_maffei_inside_or(
        eot.SIDE_SHORT,
        or_high=100.0,
        or_low=99.0,
        current_1m_open=98.8,
        current_1m_close=99.2,
        current_1m_low=98.7,
        current_1m_high=99.5,
        prior_1m_low=98.6,
        prior_1m_high=99.3,
    )
    assert r["gated"] is True and r["decision"] == "EXIT"


def test_two_bar_lock_advances_on_favorable():
    s = eot.two_bar_lock_step(eot.SIDE_LONG, counter=0, candle_open=100.0, candle_close=100.5)
    assert s["counter"] == 1 and s["locked"] is False
    s2 = eot.two_bar_lock_step(
        eot.SIDE_LONG, counter=s["counter"], candle_open=100.5, candle_close=100.9
    )
    assert s2["counter"] == 2 and s2["locked"] is True


def test_two_bar_lock_resets_on_unfavorable():
    s = eot.two_bar_lock_step(eot.SIDE_LONG, counter=1, candle_open=100.0, candle_close=99.8)
    assert s["counter"] == 0


def test_two_bar_lock_short_mirror():
    s = eot.two_bar_lock_step(eot.SIDE_SHORT, counter=1, candle_open=100.0, candle_close=99.5)
    assert s["counter"] == 2 and s["locked"] is True


def test_ema_trail_long_exits_below_ema():
    assert eot.evaluate_ema_trail(eot.SIDE_LONG, candle_5m_close=99.0, ema_9_5m=100.0) is True


def test_ema_trail_long_holds_above_ema():
    assert eot.evaluate_ema_trail(eot.SIDE_LONG, candle_5m_close=100.5, ema_9_5m=100.0) is False


def test_ema_trail_short_mirror():
    assert eot.evaluate_ema_trail(eot.SIDE_SHORT, candle_5m_close=101.0, ema_9_5m=100.0) is True


# ---------------------------------------------------------------------
# Section VI \u2014 Machine rules
# ---------------------------------------------------------------------


def test_circuit_breaker_trips_at_minus_1500_01():
    assert eot.daily_circuit_breaker_tripped(-1500.01) is True


def test_circuit_breaker_does_not_trip_at_minus_1499_99():
    assert eot.daily_circuit_breaker_tripped(-1499.99) is False


def test_circuit_breaker_trips_at_exactly_minus_1500():
    assert eot.daily_circuit_breaker_tripped(-1500.0) is True


def test_canonical_eod_constant_is_15_49_59():
    from datetime import time
    from engine.timing import EOD_FLUSH_ET

    assert EOD_FLUSH_ET == time(15, 49, 59)


# ---------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------


def test_new_position_state_long():
    s = eot.new_position_state(eot.SIDE_LONG)
    assert s["side"] == eot.SIDE_LONG
    assert s["phase"] == eot.PHASE_SURVIVAL
    assert s["entry_2_fired"] is False


def test_transition_phase_on_entry_2_layered_shield():
    s = eot.new_position_state(eot.SIDE_LONG)
    s["entry_1_price"] = 100.0
    s2 = eot.transition_phase_on_entry_2(s)
    assert s2["phase"] == eot.PHASE_NEUT_LAYERED
    assert s2["entry_2_fired"] is True
    assert s2["current_stop"] == 100.0


def test_transition_phase_on_two_bar_lock():
    s = eot.new_position_state(eot.SIDE_LONG)
    s["avg_entry"] = 100.5
    s2 = eot.transition_phase_on_two_bar_lock(s)
    assert s2["phase"] == eot.PHASE_NEUT_LOCKED
    assert s2["current_stop"] == 100.5


def test_exit_reason_enum_complete():
    expected = {
        "sovereign_brake",
        "velocity_fuse",
        "forensic_stop",
        "be_stop",
        "ema_trail",
        "daily_circuit_breaker",
        "eod",
        "manual",
    }
    assert set(eot.VALID_EXIT_REASONS) == expected
