"""Per-rule behavioural tests for the Tiger Sovereign trading spec.

This module owns one test per rule ID extracted from ``STRATEGY.md``
(Tiger Sovereign spec v2026-04-28h, adopted in the v5.13.0 series).

v5.13.2 (Track D) — Each test now EXERCISES the actual evaluator
(``eye_of_tiger.evaluate_global_permit``, ``engine.sentinel.check_alarm_*``,
``engine.velocity_ratchet.evaluate_velocity_ratchet``, ``engine.timing.is_after_*``,
``broker.order_types.order_type_for_reason``) rather than asserting that
a constant string appears in source. Test names + rule IDs are unchanged
so downstream cron / audit tooling that greps test names still works.

v5.16.0 \u2014 ``engine.titan_grip`` shim removed; Velocity Ratchet under
``engine.velocity_ratchet`` is the canonical Alarm C evaluator.

The deeper unit tests (``test_velocity_ratchet.py``, ``test_sentinel.py``,
``test_phase2_gates.py``, ``test_timing_rules.py``) own exhaustive
behavioural coverage. This file pins the spec-level "this rule is
implemented and behaves as written" contract for each rule ID.

Naming convention: ``test_<rule_id_with_underscores>``
(e.g. ``L-P4-C-S1`` -> ``test_L_P4_C_S1``).
"""

from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# LONG (Bison) — Phase 1: Global Market Shield
# ---------------------------------------------------------------------------


def test_L_P1_S1():
    """L-P1-S1: QQQ 5m close BELOW 9-EMA closes the LONG permit."""
    from eye_of_tiger import evaluate_global_permit, SIDE_LONG

    # Shield aligned (5m close > ema9), anchor aligned -> open.
    res_open = evaluate_global_permit(
        side=SIDE_LONG,
        qqq_5m_close=420.0,
        qqq_5m_ema9=419.0,
        qqq_current_price=420.5,
        qqq_avwap_0930=420.0,
    )
    assert res_open["open"] is True

    # Shield misaligned (close <= ema9) -> permit closed regardless of anchor.
    res_closed = evaluate_global_permit(
        side=SIDE_LONG,
        qqq_5m_close=418.0,
        qqq_5m_ema9=419.0,
        qqq_current_price=420.5,
        qqq_avwap_0930=420.0,
    )
    assert res_closed["open"] is False
    assert "shield" in res_closed["reason"]


def test_L_P1_S2():
    """L-P1-S2: QQQ current price BELOW 9:30 anchor VWAP closes LONG permit."""
    from eye_of_tiger import evaluate_global_permit, SIDE_LONG

    # Anchor misaligned (current <= avwap) -> permit closed.
    res = evaluate_global_permit(
        side=SIDE_LONG,
        qqq_5m_close=420.0,
        qqq_5m_ema9=419.0,
        qqq_current_price=419.5,
        qqq_avwap_0930=420.0,
    )
    assert res["open"] is False
    assert "anchor" in res["reason"]


# ---------------------------------------------------------------------------
# LONG — Phase 2: Ticker-Specific Permits
# ---------------------------------------------------------------------------


def test_L_P2_S3():
    """L-P2-S3: Volume must reach 100% of the 55-day rolling per-minute baseline.

    Behavioural assertion against ``gate_volume_pass``. The gate auto-passes
    when ``feature_flags.VOLUME_GATE_ENABLED`` is False (production default
    as of v5.13.1) or when the baseline is None (cold-start). When the gate
    is enabled and a baseline exists, current_volume / baseline must be >= 1.00.
    """
    from engine import volume_baseline
    from engine.volume_baseline import gate_volume_pass, THRESHOLD_RATIO

    assert THRESHOLD_RATIO == 1.00

    # Force the runtime flag ON for this assertion (production default is OFF).
    # Mutate via volume_baseline._ff so we set the attribute on the SAME module
    # object the production code path holds — earlier tests that do
    # ``del sys.modules['engine.feature_flags']`` (test_dashboard_state_v5_13_2,
    # test_startup_smoke) cause a re-import to return a fresh module, but
    # volume_baseline still holds its original reference.
    _ff = volume_baseline._ff
    prev = _ff.VOLUME_GATE_ENABLED
    _ff.VOLUME_GATE_ENABLED = True
    try:
        # Below baseline -> fail.
        ok, ratio = gate_volume_pass(current_volume=900.0, baseline=1000.0)
        assert ok is False
        assert ratio is not None and ratio < 1.00

        # At baseline -> pass.
        ok, ratio = gate_volume_pass(current_volume=1000.0, baseline=1000.0)
        assert ok is True
        assert ratio == pytest.approx(1.00)

        # Above baseline -> pass.
        ok, ratio = gate_volume_pass(current_volume=1500.0, baseline=1000.0)
        assert ok is True
        assert ratio is not None and ratio > 1.00

        # Cold-start (baseline=None) -> pass-through.
        ok, ratio = gate_volume_pass(current_volume=10.0, baseline=None)
        assert ok is True
        assert ratio is None
    finally:
        _ff.VOLUME_GATE_ENABLED = prev


def test_L_P2_S4():
    """L-P2-S4: TWO consecutive 1m candles closed strictly ABOVE 5m OR High.
    v5.13.9: contract owned by eye_of_tiger.evaluate_boundary_hold.
    """
    import eye_of_tiger as eot

    or_high = 100.0
    or_low = 99.0
    # Two strict-above closes -> hold.
    res = eot.evaluate_boundary_hold("LONG", or_high, or_low, [100.5, 101.0])
    assert bool(res.get("hold")) is True
    # One above + one below -> no hold.
    res = eot.evaluate_boundary_hold("LONG", or_high, or_low, [101.0, 99.5])
    assert bool(res.get("hold")) is False
    # At-boundary close breaks hold (strict >).
    res = eot.evaluate_boundary_hold("LONG", or_high, or_low, [100.5, 100.0])
    assert bool(res.get("hold")) is False
    # Insufficient closes -> no hold.
    res = eot.evaluate_boundary_hold("LONG", or_high, or_low, [101.0])
    assert bool(res.get("hold")) is False
    # No OR_high -> no hold.
    res = eot.evaluate_boundary_hold("LONG", None, or_low, [101.0, 102.0])
    assert bool(res.get("hold")) is False


# ---------------------------------------------------------------------------
# LONG — Phase 3: The Strike
# ---------------------------------------------------------------------------


def test_L_P3_S5():
    """L-P3-S5: 5m DI+ > 25 AND 1m DI+ > 25 AND price at NHOD -> Entry 1 fires."""
    from eye_of_tiger import evaluate_entry_1, SIDE_LONG, ENTRY_1_DI_THRESHOLD

    assert ENTRY_1_DI_THRESHOLD == 25.0

    base_kwargs = dict(
        permit_open=True,
        volume_bucket_ok=True,
        boundary_hold_ok=True,
        is_nhod_or_nlod=True,
    )

    # All gates pass -> fire.
    fire = evaluate_entry_1(SIDE_LONG, di_5m=30.0, di_1m=28.0, **base_kwargs)
    assert fire["fire"] is True

    # DI threshold is strict `>`. At exactly 25 -> no fire.
    no_fire_5m = evaluate_entry_1(SIDE_LONG, di_5m=25.0, di_1m=28.0, **base_kwargs)
    assert no_fire_5m["fire"] is False
    assert no_fire_5m["reason"] == "di_5m"

    no_fire_1m = evaluate_entry_1(SIDE_LONG, di_5m=30.0, di_1m=25.0, **base_kwargs)
    assert no_fire_1m["fire"] is False
    assert no_fire_1m["reason"] == "di_1m"

    # No NHOD print -> no fire.
    no_extreme = evaluate_entry_1(
        SIDE_LONG,
        di_5m=30.0,
        di_1m=28.0,
        permit_open=True,
        volume_bucket_ok=True,
        boundary_hold_ok=True,
        is_nhod_or_nlod=False,
    )
    assert no_extreme["fire"] is False
    assert no_extreme["reason"] == "no_extreme_print"


def test_L_P3_S6():
    """L-P3-S6: Entry 2 fires on 1m DI+ crossing strictly above 30 + fresh NHOD."""
    from eye_of_tiger import evaluate_entry_2, SIDE_LONG, ENTRY_2_DI_THRESHOLD

    assert ENTRY_2_DI_THRESHOLD == 30.0

    base_kwargs = dict(
        entry_1_active=True,
        permit_open_at_trigger=True,
        fresh_nhod_or_nlod=True,
        entry_2_already_fired=False,
    )

    # Edge transition <=30 -> >30 with fresh NHOD -> fire.
    fire = evaluate_entry_2(SIDE_LONG, di_1m_prev=29.0, di_1m_now=30.5, **base_kwargs)
    assert fire["fire"] is True

    # Already > 30 (not a crossing) -> no fire.
    not_a_crossing = evaluate_entry_2(SIDE_LONG, di_1m_prev=31.0, di_1m_now=32.0, **base_kwargs)
    assert not_a_crossing["fire"] is False
    assert not_a_crossing["reason"] == "no_crossing"

    # At exactly 30 -> no crossing (strict `>`).
    boundary = evaluate_entry_2(SIDE_LONG, di_1m_prev=29.0, di_1m_now=30.0, **base_kwargs)
    assert boundary["fire"] is False
    assert boundary["reason"] == "no_crossing"

    # Crossing without fresh NHOD -> no fire.
    no_fresh = evaluate_entry_2(
        SIDE_LONG,
        di_1m_prev=29.0,
        di_1m_now=31.0,
        entry_1_active=True,
        permit_open_at_trigger=True,
        fresh_nhod_or_nlod=False,
        entry_2_already_fired=False,
    )
    assert no_fresh["fire"] is False
    assert no_fresh["reason"] == "no_fresh_extreme"


# ---------------------------------------------------------------------------
# LONG — Phase 4: Sentinel Loop
# ---------------------------------------------------------------------------


def test_L_P4_A():
    """L-P4-A: Trade lost $500 (A1) OR price dropped 1% in single minute (A2)."""
    from engine.sentinel import (
        check_alarm_a,
        ALARM_A_HARD_LOSS_DOLLARS,
        ALARM_A_VELOCITY_WINDOW_SECONDS,
        ALARM_A_VELOCITY_THRESHOLD,
    )

    assert ALARM_A_HARD_LOSS_DOLLARS == -500.0
    assert ALARM_A_VELOCITY_WINDOW_SECONDS == 60
    assert ALARM_A_VELOCITY_THRESHOLD == pytest.approx(-0.01)

    # A1 fires on -$500 hard floor.
    fired = check_alarm_a(
        side="LONG",
        unrealized_pnl=-500.0,
        position_value=10000.0,
        pnl_history=None,
        now_ts=1000.0,
    )
    codes = {a.alarm for a in fired}
    assert "A_LOSS" in codes

    # A1 does not fire above floor.
    fired_safe = check_alarm_a(
        side="LONG",
        unrealized_pnl=-499.99,
        position_value=10000.0,
        pnl_history=None,
        now_ts=1000.0,
    )
    assert {a.alarm for a in fired_safe} == set()

    # A2 fires on >= 1% drop in 60s window.
    history = [(940.0, 0.0)]  # 60s ago pnl was 0
    fired_vel = check_alarm_a(
        side="LONG",
        unrealized_pnl=-150.0,  # delta=-150, 150/10000=1.5% drop
        position_value=10000.0,
        pnl_history=history,
        now_ts=1000.0,
    )
    assert "A_FLASH" in {a.alarm for a in fired_vel}


def test_L_P4_B():
    """L-P4-B: 5-minute candle CLOSE below 5m 9-EMA -> Alarm B."""
    from engine.sentinel import check_alarm_b

    # Long: close < ema9 fires.
    fired = check_alarm_b(side="LONG", last_5m_close=99.0, last_5m_ema9=100.0)
    assert len(fired) == 1
    assert fired[0].alarm == "B"

    # Long: close >= ema9 does not fire.
    safe = check_alarm_b(side="LONG", last_5m_close=100.0, last_5m_ema9=100.0)
    assert safe == []

    # Missing data -> no fire.
    no_data = check_alarm_b(side="LONG", last_5m_close=None, last_5m_ema9=100.0)
    assert no_data == []


# ---------------------------------------------------------------------------
# SHORT (Wounded Buffalo) — mirrors of long rules
# ---------------------------------------------------------------------------


def test_S_P1_S1():
    """S-P1-S1: QQQ 5m close ABOVE 9-EMA closes the SHORT permit."""
    from eye_of_tiger import evaluate_global_permit, SIDE_SHORT

    res_open = evaluate_global_permit(
        side=SIDE_SHORT,
        qqq_5m_close=418.0,
        qqq_5m_ema9=419.0,
        qqq_current_price=419.0,
        qqq_avwap_0930=420.0,
    )
    assert res_open["open"] is True

    res_closed = evaluate_global_permit(
        side=SIDE_SHORT,
        qqq_5m_close=420.0,
        qqq_5m_ema9=419.0,
        qqq_current_price=419.0,
        qqq_avwap_0930=420.0,
    )
    assert res_closed["open"] is False
    assert "shield" in res_closed["reason"]


def test_S_P1_S2():
    """S-P1-S2: QQQ current price ABOVE 9:30 anchor VWAP closes SHORT permit."""
    from eye_of_tiger import evaluate_global_permit, SIDE_SHORT

    res = evaluate_global_permit(
        side=SIDE_SHORT,
        qqq_5m_close=418.0,
        qqq_5m_ema9=419.0,
        qqq_current_price=421.0,
        qqq_avwap_0930=420.0,
    )
    assert res["open"] is False
    assert "anchor" in res["reason"]


def test_S_P2_S3():
    """S-P2-S3: Volume mirror of L-P2-S3 — 100% baseline threshold."""
    from engine import volume_baseline
    from engine.volume_baseline import gate_volume_pass, THRESHOLD_RATIO

    _ff = volume_baseline._ff

    assert THRESHOLD_RATIO == 1.00

    prev = _ff.VOLUME_GATE_ENABLED
    _ff.VOLUME_GATE_ENABLED = True
    try:
        ok, _ = gate_volume_pass(current_volume=999.99, baseline=1000.0)
        assert ok is False
        ok, _ = gate_volume_pass(current_volume=1000.01, baseline=1000.0)
        assert ok is True
    finally:
        _ff.VOLUME_GATE_ENABLED = prev


def test_S_P2_S4():
    """S-P2-S4: TWO consecutive 1m candles closed strictly BELOW 5m OR Low.
    v5.13.9: contract owned by eye_of_tiger.evaluate_boundary_hold.
    """
    import eye_of_tiger as eot

    or_high = 101.0
    or_low = 100.0
    res = eot.evaluate_boundary_hold("SHORT", or_high, or_low, [99.5, 99.0])
    assert bool(res.get("hold")) is True
    res = eot.evaluate_boundary_hold("SHORT", or_high, or_low, [99.5, 100.5])
    assert bool(res.get("hold")) is False
    # At-boundary close breaks hold.
    res = eot.evaluate_boundary_hold("SHORT", or_high, or_low, [99.5, 100.0])
    assert bool(res.get("hold")) is False
    res = eot.evaluate_boundary_hold("SHORT", or_high, or_low, [99.5])
    assert bool(res.get("hold")) is False
    res = eot.evaluate_boundary_hold("SHORT", or_high, None, [99.0, 98.5])
    assert bool(res.get("hold")) is False


def test_S_P3_S5():
    """S-P3-S5: 5m DI- > 25 AND 1m DI- > 25 AND price at NLOD -> Entry 1 SHORT fires."""
    from eye_of_tiger import evaluate_entry_1, SIDE_SHORT, ENTRY_1_DI_THRESHOLD

    assert ENTRY_1_DI_THRESHOLD == 25.0

    base_kwargs = dict(
        permit_open=True,
        volume_bucket_ok=True,
        boundary_hold_ok=True,
        is_nhod_or_nlod=True,
    )
    fire = evaluate_entry_1(SIDE_SHORT, di_5m=30.0, di_1m=28.0, **base_kwargs)
    assert fire["fire"] is True

    boundary = evaluate_entry_1(SIDE_SHORT, di_5m=25.0, di_1m=28.0, **base_kwargs)
    assert boundary["fire"] is False
    assert boundary["reason"] == "di_5m"


def test_S_P3_S6():
    """S-P3-S6: Entry 2 SHORT fires on 1m DI- crossing strictly above 30 + fresh NLOD."""
    from eye_of_tiger import evaluate_entry_2, SIDE_SHORT, ENTRY_2_DI_THRESHOLD

    assert ENTRY_2_DI_THRESHOLD == 30.0

    base_kwargs = dict(
        entry_1_active=True,
        permit_open_at_trigger=True,
        fresh_nhod_or_nlod=True,
        entry_2_already_fired=False,
    )
    fire = evaluate_entry_2(SIDE_SHORT, di_1m_prev=28.0, di_1m_now=30.5, **base_kwargs)
    assert fire["fire"] is True

    not_crossing = evaluate_entry_2(SIDE_SHORT, di_1m_prev=31.0, di_1m_now=32.0, **base_kwargs)
    assert not_crossing["fire"] is False


def test_S_P4_A():
    """S-P4-A: SHORT trade lost $500 (A1) OR price spiked 1% in 60s (A2)."""
    from engine.sentinel import check_alarm_a, ALARM_A_HARD_LOSS_DOLLARS

    assert ALARM_A_HARD_LOSS_DOLLARS == -500.0

    fired = check_alarm_a(
        side="SHORT",
        unrealized_pnl=-500.01,
        position_value=10000.0,
        pnl_history=None,
        now_ts=1000.0,
    )
    assert "A_LOSS" in {a.alarm for a in fired}

    # SHORT velocity test: pnl drops by >1% of position value in 60s.
    fired_vel = check_alarm_a(
        side="SHORT",
        unrealized_pnl=-200.0,
        position_value=10000.0,
        pnl_history=[(940.0, 0.0)],
        now_ts=1000.0,
    )
    assert "A_FLASH" in {a.alarm for a in fired_vel}


def test_S_P4_B():
    """S-P4-B: 5-minute candle CLOSE above 5m 9-EMA -> Alarm B for SHORT."""
    from engine.sentinel import check_alarm_b

    # Short: close > ema9 fires.
    fired = check_alarm_b(side="SHORT", last_5m_close=101.0, last_5m_ema9=100.0)
    assert len(fired) == 1
    assert fired[0].alarm == "B"

    # Short: close <= ema9 does not fire.
    safe = check_alarm_b(side="SHORT", last_5m_close=100.0, last_5m_ema9=100.0)
    assert safe == []


# ---------------------------------------------------------------------------
# SHARED rules
# ---------------------------------------------------------------------------


def test_SHARED_CUTOFF():
    """SHARED-CUTOFF: New-position cutoff at 15:44:59 ET."""
    from engine.timing import NEW_POSITION_CUTOFF_ET, is_after_cutoff_et

    assert NEW_POSITION_CUTOFF_ET == time(15, 44, 59)

    before = datetime(2026, 4, 28, 15, 44, 58, tzinfo=ET)
    at_cutoff = datetime(2026, 4, 28, 15, 44, 59, tzinfo=ET)
    after = datetime(2026, 4, 28, 15, 45, 0, tzinfo=ET)
    assert is_after_cutoff_et(before) is False
    # is_after_cutoff_et returns True at-or-after the cutoff.
    assert is_after_cutoff_et(at_cutoff) is True
    assert is_after_cutoff_et(after) is True


def test_SHARED_CB():
    """SHARED-CB: Daily circuit breaker at -$1,500."""
    from eye_of_tiger import (
        DAILY_CIRCUIT_BREAKER_DOLLARS,
        daily_circuit_breaker_tripped,
    )

    assert DAILY_CIRCUIT_BREAKER_DOLLARS == -1500.0
    assert daily_circuit_breaker_tripped(-1500.01) is True
    assert daily_circuit_breaker_tripped(-1500.0) is True
    assert daily_circuit_breaker_tripped(-1499.99) is False
    assert daily_circuit_breaker_tripped(0.0) is False


def test_SHARED_EOD():
    """SHARED-EOD: EOD flush at 15:49:59 ET."""
    from engine.timing import EOD_FLUSH_ET, is_after_eod_et

    assert EOD_FLUSH_ET == time(15, 49, 59)

    before = datetime(2026, 4, 28, 15, 49, 58, tzinfo=ET)
    at_eod = datetime(2026, 4, 28, 15, 49, 59, tzinfo=ET)
    after = datetime(2026, 4, 28, 15, 50, 0, tzinfo=ET)
    assert is_after_eod_et(before) is False
    assert is_after_eod_et(at_eod) is True
    assert is_after_eod_et(after) is True


def test_SHARED_HUNT():
    """SHARED-HUNT: Unlimited hunting until the 15:44:59 cutoff."""
    from engine.timing import (
        HUNT_START_ET,
        HUNT_END_ET,
        NEW_POSITION_CUTOFF_ET,
        is_in_hunt_window,
    )

    assert HUNT_START_ET == time(9, 35, 0)
    assert HUNT_END_ET == NEW_POSITION_CUTOFF_ET == time(15, 44, 59)

    in_window = datetime(2026, 4, 28, 12, 0, 0, tzinfo=ET)
    pre_open = datetime(2026, 4, 28, 9, 30, 0, tzinfo=ET)
    after_cutoff = datetime(2026, 4, 28, 15, 45, 0, tzinfo=ET)
    assert is_in_hunt_window(in_window) is True
    assert is_in_hunt_window(pre_open) is False
    assert is_in_hunt_window(after_cutoff) is False


def test_SHARED_ORDER_PROFIT():
    """SHARED-ORDER-PROFIT: All profit-taking exits via LIMIT orders."""
    from broker.order_types import (
        order_type_for_reason,
        REASON_STAGE1_HARVEST,
        REASON_STAGE3_HARVEST,
        ORDER_TYPE_LIMIT,
    )

    assert order_type_for_reason(REASON_STAGE1_HARVEST) == ORDER_TYPE_LIMIT
    assert order_type_for_reason(REASON_STAGE3_HARVEST) == ORDER_TYPE_LIMIT


def test_SHARED_ORDER_STOP():
    """SHARED-ORDER-STOP: All defensive stops via STOP MARKET orders."""
    from broker.order_types import (
        order_type_for_reason,
        REASON_ALARM_A,
        REASON_ALARM_B,
        REASON_RATCHET,
        REASON_RUNNER_EXIT,
        ORDER_TYPE_STOP_MARKET,
    )

    assert order_type_for_reason(REASON_ALARM_A) == ORDER_TYPE_STOP_MARKET
    assert order_type_for_reason(REASON_ALARM_B) == ORDER_TYPE_STOP_MARKET
    assert order_type_for_reason(REASON_RATCHET) == ORDER_TYPE_STOP_MARKET
    assert order_type_for_reason(REASON_RUNNER_EXIT) == ORDER_TYPE_STOP_MARKET
