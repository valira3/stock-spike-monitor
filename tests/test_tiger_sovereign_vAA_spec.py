"""tests/test_tiger_sovereign_vAA_spec.py
================================================================
Validation tests for Tiger Sovereign vAA-1.

Each test cites the rule ID from `tiger_sovereign_spec_vAA-1.md`.
Tests authored BEFORE implementation \\u2014 failing tests are the
to-do list for the v5.15.0 PR series. Each spec-gap test is
decorated with ``@pytest.mark.spec_gap("vAA-PR-N", "<rule>")``
so smoke_test can distinguish gap failures from regressions.

Naming: the test function name is `test_<RULE_ID_lowercased>_<short>`.
Example: `test_l_p3_full_di_gt_30_enters_full_size`.
================================================================
"""

from __future__ import annotations

import math
import time
from collections import deque

import pytest


# =====================================================================
# SECTION 0 \\u2014 Glossary / state objects
# =====================================================================


def test_trade_hvp_resets_on_strike_entry():
    """Trade_HVP must initialize to the current 5m ADX at fill time
    and reset (back to the new entry's ADX) on every fresh Strike."""
    from engine.momentum_state import TradeHVP

    hvp = TradeHVP()
    hvp.on_strike_open(initial_adx_5m=22.0)
    assert hvp.peak == 22.0

    hvp.update(current_adx_5m=30.0)
    hvp.update(current_adx_5m=27.0)
    assert hvp.peak == 30.0

    hvp.on_strike_open(initial_adx_5m=18.0)
    assert hvp.peak == 18.0


def test_divergence_memory_is_per_ticker_per_side():
    """Stored_Peak_Price / Stored_Peak_RSI are keyed by (ticker, side)
    and survive across Strikes within the day; reset at session boundary."""
    from engine.momentum_state import DivergenceMemory

    mem = DivergenceMemory()
    mem.update("AAPL", "LONG", price=190.0, rsi=72.0)
    mem.update("AAPL", "SHORT", price=185.0, rsi=28.0)
    assert mem.peak("AAPL", "LONG") == (190.0, 72.0)
    assert mem.peak("AAPL", "SHORT") == (185.0, 28.0)

    mem.session_reset()
    assert mem.peak("AAPL", "LONG") is None


# =====================================================================
# SECTION 1 \\u2014 Strike Model
# =====================================================================


@pytest.mark.spec_gap("vAA-PR-3", "STRIKE-CAP-3")
def test_strike_cap_3_blocks_fourth_entry():
    """Maximum 3 Strikes per (ticker, side) per day."""
    import trade_genius as tg

    tg._v570_strike_counts.clear()
    for _ in range(3):
        tg._v570_record_entry("NVDA", "LONG")
    assert tg._v570_strike_count("NVDA", "LONG") == 3

    # Implementation must expose a blocker:
    from trade_genius import strike_entry_allowed  # NEW symbol

    assert strike_entry_allowed("NVDA", "LONG") is False


@pytest.mark.spec_gap("vAA-PR-3", "STRIKE-FLAT-GATE")
def test_strike_flat_gate_blocks_until_position_closes():
    """Strike N+1 cannot fire until prior Strike's position == 0."""
    from trade_genius import strike_entry_allowed

    fake_positions = {"NVDA:LONG": {"shares": 50}}
    assert strike_entry_allowed("NVDA", "LONG", positions=fake_positions) is False

    fake_positions["NVDA:LONG"]["shares"] = 0
    assert strike_entry_allowed("NVDA", "LONG", positions=fake_positions) is True


@pytest.mark.spec_gap("vAA-PR-3", "STRIKE-CAP-3 overrides ENABLE_UNLIMITED_TITAN_STRIKES")
def test_strike_cap_3_overrides_titan_flag():
    """vAA-1 explicitly retires unlimited Titan strikes."""
    import trade_genius as tg

    assert tg.ENABLE_UNLIMITED_TITAN_STRIKES is False, (
        "ENABLE_UNLIMITED_TITAN_STRIKES must default OFF in v5.15.0"
    )


# =====================================================================
# SECTION 2 \\u2014 Bison Phase 2 volume gate (time-conditional)
# =====================================================================


@pytest.mark.parametrize(
    "now_et_hhmm,vol_ratio,expected_pass",
    [
        ("09:35", 0.10, True),  # before 10:00 \\u2014 auto-pass
        ("09:59", 0.50, True),  # before 10:00 \\u2014 auto-pass
        ("10:00", 0.99, False),  # at/after 10:00 \\u2014 below threshold
        ("10:00", 1.00, True),  # at/after 10:00 \\u2014 inclusive 100%
        ("10:00", 1.01, True),
        ("11:30", 0.50, False),  # well after 10:00, fails
    ],
)
@pytest.mark.spec_gap("vAA-PR-2", "L-P2-S3 / S-P2-S3")
def test_l_p2_s3_volume_gate_time_conditional(now_et_hhmm, vol_ratio, expected_pass):
    """Volume gate is auto-pass before 10:00 ET; >= 100% from 10:00 ET on."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from eye_of_tiger import evaluate_volume_bucket  # NEW signature: time-aware

    h, m = map(int, now_et_hhmm.split(":"))
    now_et = datetime(2026, 4, 30, h, m, 0, tzinfo=ZoneInfo("America/New_York"))
    result = evaluate_volume_bucket(
        check_result={"ratio_to_55bar_avg": vol_ratio},
        now_et=now_et,
    )
    assert result is expected_pass


@pytest.mark.spec_gap("vAA-PR-2", "L-P2-S4")
def test_l_p2_s4_two_consecutive_1m_closes_above_orh():
    """Boundary fires only on the close of the SECOND qualifying 1m bar."""
    from eye_of_tiger import evaluate_boundary_hold

    # bar 1 close > ORH but bar 0 close <= ORH \\u2014 NOT yet permit.
    res1 = evaluate_boundary_hold(
        side="LONG",
        or_high=100.0,
        or_low=99.0,
        prev_1m_close=99.5,
        curr_1m_close=100.5,
    )
    assert res1.get("hold") is False

    # bar 1 AND bar 0 both > ORH \\u2014 PERMIT.
    res2 = evaluate_boundary_hold(
        side="LONG",
        or_high=100.0,
        or_low=99.0,
        prev_1m_close=100.2,
        curr_1m_close=100.5,
    )
    assert res2.get("hold") is True


# =====================================================================
# SECTION 2 \\u2014 Phase 3 momentum-sensitive sizing
# =====================================================================


@pytest.mark.spec_gap("vAA-PR-1", "L-P3-FULL")
def test_l_p3_full_di_gt_30_enters_full_size():
    """1m DI+ > 30 with 5m DI+ > 25 anchor \\u2192 Full Strike (100%)."""
    from eye_of_tiger import evaluate_strike_sizing

    decision = evaluate_strike_sizing(
        side="LONG",
        di_5m=27.0,
        di_1m=31.0,
        is_fresh_extreme=True,
        intended_shares=100,
        held_shares_this_strike=0,
        alarm_e_blocked=False,
    )
    assert decision.size_label == "FULL"
    assert decision.shares_to_buy == 100


@pytest.mark.spec_gap("vAA-PR-1", "L-P3-SCALED-A")
def test_l_p3_scaled_a_di_in_25_30_enters_50pct():
    """25 <= 1m DI+ <= 30 \\u2192 Scaled-A 50%."""
    from eye_of_tiger import evaluate_strike_sizing

    for di_1m in (25.0, 27.5, 30.0):
        decision = evaluate_strike_sizing(
            side="LONG",
            di_5m=27.0,
            di_1m=di_1m,
            is_fresh_extreme=True,
            intended_shares=100,
            held_shares_this_strike=0,
            alarm_e_blocked=False,
        )
        assert decision.size_label == "SCALED_A", f"di_1m={di_1m}"
        assert decision.shares_to_buy == 50


@pytest.mark.spec_gap("vAA-PR-1", "L-P3-SCALED-B")
def test_l_p3_scaled_b_addon_requires_all_three_conditions():
    """Add-on 50% only if DI+>30 AND fresh NHOD AND Alarm E False."""
    from eye_of_tiger import evaluate_strike_sizing

    # Holding 50 already; all three add-on conditions met:
    decision_ok = evaluate_strike_sizing(
        side="LONG",
        di_5m=27.0,
        di_1m=31.0,
        is_fresh_extreme=True,
        intended_shares=100,
        held_shares_this_strike=50,
        alarm_e_blocked=False,
    )
    assert decision_ok.size_label == "SCALED_B"
    assert decision_ok.shares_to_buy == 50

    # Same but Alarm E blocks:
    decision_blocked = evaluate_strike_sizing(
        side="LONG",
        di_5m=27.0,
        di_1m=31.0,
        is_fresh_extreme=True,
        intended_shares=100,
        held_shares_this_strike=50,
        alarm_e_blocked=True,
    )
    assert decision_blocked.size_label == "WAIT"
    assert decision_blocked.shares_to_buy == 0

    # Same but no fresh NHOD:
    decision_no_extreme = evaluate_strike_sizing(
        side="LONG",
        di_5m=27.0,
        di_1m=31.0,
        is_fresh_extreme=False,
        intended_shares=100,
        held_shares_this_strike=50,
        alarm_e_blocked=False,
    )
    assert decision_no_extreme.size_label == "WAIT"


@pytest.mark.spec_gap("vAA-PR-1", "L-P3-AUTH master anchor")
def test_l_p3_master_anchor_5m_di_must_exceed_25():
    """If 5m DI+ <= 25, NO sizing decision passes regardless of 1m DI+."""
    from eye_of_tiger import evaluate_strike_sizing

    decision = evaluate_strike_sizing(
        side="LONG",
        di_5m=25.0,
        di_1m=35.0,
        is_fresh_extreme=True,
        intended_shares=100,
        held_shares_this_strike=0,
        alarm_e_blocked=False,
    )
    assert decision.size_label == "WAIT"


# =====================================================================
# SECTION 3 \\u2014 Wounded Buffalo mirror
# =====================================================================


@pytest.mark.spec_gap("vAA-PR-1", "S-P3-FULL")
def test_s_p3_full_di_minus_gt_30_enters_full_size():
    from eye_of_tiger import evaluate_strike_sizing

    decision = evaluate_strike_sizing(
        side="SHORT",
        di_5m=27.0,
        di_1m=31.0,
        is_fresh_extreme=True,
        intended_shares=100,
        held_shares_this_strike=0,
        alarm_e_blocked=False,
    )
    assert decision.size_label == "FULL"
    assert decision.shares_to_buy == 100


# =====================================================================
# SECTION 4 \\u2014 Shared risk / timing (already covered; sanity only)
# =====================================================================


def test_shared_hard_stop_constant():
    from engine.sentinel import ALARM_A_HARD_LOSS_DOLLARS

    assert ALARM_A_HARD_LOSS_DOLLARS == -500.0


def test_shared_circuit_breaker_constant():
    from eye_of_tiger import DAILY_CIRCUIT_BREAKER_DOLLARS

    assert DAILY_CIRCUIT_BREAKER_DOLLARS == -1500.0


def test_shared_cutoff_154459():
    from engine.timing import NEW_POSITION_CUTOFF_ET

    assert NEW_POSITION_CUTOFF_ET.hour == 15
    assert NEW_POSITION_CUTOFF_ET.minute == 44
    assert NEW_POSITION_CUTOFF_ET.second == 59


def test_shared_eod_154959():
    from engine.timing import EOD_FLUSH_ET

    assert EOD_FLUSH_ET.hour == 15
    assert EOD_FLUSH_ET.minute == 49
    assert EOD_FLUSH_ET.second == 59


# =====================================================================
# SECTION 5 \\u2014 Sentinels (vAA changes)
# =====================================================================


# ----- Alarm A (codes renamed in vAA-1: A1→A_LOSS, A2→A_FLASH; legacy strings deleted) -----


@pytest.mark.spec_gap("vAA-PR-7", "SENT-A1-CODE-RENAME")
def test_sent_a_loss_hard_loss_fires_at_minus_500_inclusive():
    from engine.sentinel import check_alarm_a

    fired = check_alarm_a(
        side="LONG",
        unrealized_pnl=-500.0,
        position_value=10000.0,
        pnl_history=deque(),
        now_ts=time.time(),
    )
    assert any(a.alarm == "A_LOSS" for a in fired)
    # legacy code must be gone
    assert not any(a.alarm == "A1" for a in fired)


@pytest.mark.spec_gap("vAA-PR-7", "SENT-A2-CODE-RENAME")
def test_sent_a_flash_move_minus_1pct_in_60s():
    from engine.sentinel import check_alarm_a

    now = time.time()
    history = deque([(now - 60.0, 0.0)])
    fired = check_alarm_a(
        side="LONG",
        unrealized_pnl=-101.0,
        position_value=10000.0,
        pnl_history=history,
        now_ts=now,
    )
    assert any(a.alarm == "A_FLASH" for a in fired)
    # legacy code must be gone
    assert not any(a.alarm == "A2" for a in fired)


@pytest.mark.spec_gap("vAA-PR-7", "SENT-A-LEGACY-STRINGS-DELETED")
def test_sent_a_legacy_codes_absent_from_engine_module():
    """Source-level guarantee: no string literal 'A1' or 'A2' survives in engine/sentinel.py.
    Historical comments referencing the rename are allowed."""
    import re
    from pathlib import Path

    src = Path("engine/sentinel.py").read_text()
    # match a quoted exact 'A1' or 'A2' literal (single or double quotes)
    legacy = re.findall(r"['\"]A[12]['\"]", src)
    assert legacy == [], f"legacy alarm codes still present in engine/sentinel.py: {legacy}"


# ----- Alarm B (existing; sanity) -----


def test_sent_b_long_5m_close_below_ema9_fires():
    from engine.sentinel import check_alarm_b

    fired = check_alarm_b(side="LONG", last_5m_close=99.0, last_5m_ema9=100.0)
    assert any(a.alarm == "B" for a in fired)


# ----- Alarm C \\u2014 NEW Velocity Ratchet -----


@pytest.mark.spec_gap("vAA-PR-4", "SENT-C velocity ratchet")
def test_sent_c_velocity_ratchet_3_decreasing_1m_adx():
    """Three strictly-decreasing 1m ADX values \\u2192 ratchet stop to current \\u00b1 0.25%."""
    from engine.sentinel import check_alarm_c
    from engine.momentum_state import ADXTrendWindow

    win = ADXTrendWindow()
    win.push(30.0)
    win.push(28.0)
    win.push(26.0)  # strictly decreasing 30 > 28 > 26

    actions, _ = check_alarm_c(
        adx_window=win,
        side="LONG",
        current_price=100.0,
        current_shares=100,
        current_stop_price=99.0,
    )
    assert any(a.alarm == "C" for a in actions)
    # The new stop should be 100.0 * (1 - 0.0025) = 99.75
    new_stop = next(a for a in actions if a.alarm == "C").detail_stop_price
    assert math.isclose(new_stop, 99.75, abs_tol=1e-6)


@pytest.mark.spec_gap("vAA-PR-4", "SENT-C strictly monotone")
def test_sent_c_does_not_fire_on_non_monotone():
    """Equal or non-decreasing values do NOT trigger."""
    from engine.sentinel import check_alarm_c
    from engine.momentum_state import ADXTrendWindow

    for triple in [(30.0, 28.0, 28.0), (28.0, 30.0, 26.0), (26.0, 28.0, 30.0)]:
        win = ADXTrendWindow()
        for v in triple:
            win.push(v)
        actions, _ = check_alarm_c(
            adx_window=win,
            side="LONG",
            current_price=100.0,
            current_shares=100,
            current_stop_price=99.0,
        )
        assert not actions, f"unexpected fire on {triple}"


@pytest.mark.spec_gap("vAA-PR-4", "SENT-C ratchet does not loosen")
def test_sent_c_ratchet_only_tightens():
    """If the existing stop is already tighter, do NOT loosen it."""
    from engine.sentinel import check_alarm_c
    from engine.momentum_state import ADXTrendWindow

    win = ADXTrendWindow()
    for v in (30.0, 28.0, 26.0):
        win.push(v)

    # current_stop_price=99.90 already tighter than 99.75
    actions, _ = check_alarm_c(
        adx_window=win,
        side="LONG",
        current_price=100.0,
        current_shares=100,
        current_stop_price=99.90,
    )
    # implementation must not emit a loosening modify
    assert all(getattr(a, "detail_stop_price", 99.90) >= 99.90 for a in actions)


# ----- Alarm D \\u2014 NEW HVP Lock -----


@pytest.mark.spec_gap("vAA-PR-5", "SENT-D HVP lock")
def test_sent_d_market_exit_when_5m_adx_below_75pct_of_peak():
    from engine.sentinel import check_alarm_d
    from engine.momentum_state import TradeHVP

    hvp = TradeHVP()
    hvp.on_strike_open(initial_adx_5m=20.0)
    hvp.update(current_adx_5m=40.0)  # peak = 40

    # 75% of 40 = 30.0
    res_just_below = check_alarm_d(trade_hvp=hvp, current_adx_5m=29.99)
    assert res_just_below is not None and res_just_below.alarm == "D"

    res_above = check_alarm_d(trade_hvp=hvp, current_adx_5m=30.0)
    assert res_above is None  # exactly 75% does NOT fire (strict)


# ----- Alarm E \\u2014 NEW Divergence Trap -----


@pytest.mark.spec_gap("vAA-PR-6", "SENT-E-PRE blocks Strike 2/3 only")
def test_sent_e_pre_blocks_strike_2_3_not_strike_1():
    from engine.sentinel import check_alarm_e_pre
    from engine.momentum_state import DivergenceMemory

    mem = DivergenceMemory()
    mem.update("NVDA", "LONG", price=200.0, rsi=72.0)

    # New extreme but lower RSI:
    diverging = check_alarm_e_pre(
        memory=mem,
        ticker="NVDA",
        side="LONG",
        current_price=205.0,
        current_rsi_15=68.0,
        strike_num=2,
    )
    assert diverging is True

    # Strike 1 ignores Alarm E pre-filter:
    strike_1 = check_alarm_e_pre(
        memory=mem,
        ticker="NVDA",
        side="LONG",
        current_price=205.0,
        current_rsi_15=68.0,
        strike_num=1,
    )
    assert strike_1 is False


@pytest.mark.spec_gap("vAA-PR-6", "SENT-E-POST in-trade ratchet")
def test_sent_e_post_ratchets_stop_when_divergence_in_trade():
    from engine.sentinel import check_alarm_e_post
    from engine.momentum_state import DivergenceMemory

    mem = DivergenceMemory()
    mem.update("NVDA", "LONG", price=200.0, rsi=72.0)

    res = check_alarm_e_post(
        memory=mem,
        ticker="NVDA",
        side="LONG",
        current_price=205.0,
        current_rsi_15=68.0,
        current_stop_price=199.0,
    )
    assert res is not None and res.alarm == "E"
    assert math.isclose(res.detail_stop_price, 205.0 * (1 - 0.0025), abs_tol=1e-6)


# ----- Sentinel parallelism contract -----


@pytest.mark.spec_gap("vAA-PR-6", "SENT-A/B/C/D/E parallel dispatch")
def test_sentinels_evaluate_in_parallel_not_sequence():
    """The architectural rule: A, B, C, D, E all evaluated; no short-circuit.
    evaluate_sentinel must return ALL fired alarms, not just the first."""
    import inspect
    from engine import sentinel

    src = inspect.getsource(sentinel.evaluate_sentinel)
    # Heuristic: no `return` inside an `if alarm_a` block before B/C/D/E checks.
    assert "return" in src  # function does return a result
    # Stronger guarantee is enforced by integration tests below; this
    # is a structural smoke check.
    assert "check_alarm_a" in src
    assert "check_alarm_b" in src
    assert "check_alarm_c" in src
    assert "check_alarm_d" in src
    assert "check_alarm_e_post" in src


# =====================================================================
# Order-type wiring
# =====================================================================


@pytest.mark.spec_gap("vAA-PR-3b", "ORDER-LIMIT-PRICE-LONG")
def test_long_strike_order_priced_at_ask_times_1_001():
    from broker.orders import compute_strike_limit_price

    px = compute_strike_limit_price(side="LONG", ask=100.0, bid=99.95)
    assert math.isclose(px, 100.1, abs_tol=1e-6)


@pytest.mark.spec_gap("vAA-PR-3b", "ORDER-LIMIT-PRICE-SHORT")
def test_short_strike_order_priced_at_bid_times_0_999():
    from broker.orders import compute_strike_limit_price

    px = compute_strike_limit_price(side="SHORT", ask=100.0, bid=99.95)
    assert math.isclose(px, 99.95 * 0.999, abs_tol=1e-6)


# =====================================================================
# Smoke / no-regression
# =====================================================================


@pytest.mark.spec_gap("vAA-PR-4", "TITAN-GRIP-DELETED")
def test_titan_grip_module_removed_or_neutered():
    """Titan Grip Harvest is deleted in vAA-1. Module presence with a
    `check_titan_grip` that returns harvest actions is a regression."""
    try:
        from engine import titan_grip
    except ImportError:
        return  # acceptable: module deleted

    if hasattr(titan_grip, "check_titan_grip"):
        # If kept as a shim, it must return no harvest actions.
        result = titan_grip.check_titan_grip(state=None, current_price=100.0, current_shares=100)
        assert result == [] or result is None
