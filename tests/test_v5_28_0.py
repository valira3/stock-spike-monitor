"""v5.28.0 \u2014 Alarm F (Hybrid Chandelier Trail) unit tests.

Spec reference: /home/user/workspace/v528_trailing_stops_research.md \u00a76.
"""

from __future__ import annotations

import random

from engine.alarm_f_trail import (
    ATR_PERIOD,
    BE_ARM_R_MULT,
    EXIT_REASON_ALARM_F,
    EXIT_REASON_ALARM_F_EXIT,
    MIN_BARS_BEFORE_ARM,
    STAGE2_ARM_R_MULT,
    STAGE_BREAKEVEN,
    STAGE_CHANDELIER_TIGHT,
    STAGE_CHANDELIER_WIDE,
    STAGE_INACTIVE,
    TIGHT_MULT,
    TrailState,
    WIDE_MULT,
    atr_from_bars,
    chandelier_level,
    propose_stop,
    should_exit_on_close_cross,
    update_trail,
)
from engine.sentinel import (
    SIDE_LONG,
    SIDE_SHORT,
    check_alarm_f,
    evaluate_sentinel,
)
from engine import alarm_f_trail as _alarm_f_module


# v5.28.0: stage-transition mechanics tests use the ORIGINAL conservative
# thresholds (S2=2.0R, S3=1.5*ATR) so the test fixture price walks have
# enough headroom to demonstrate clean Stage 1 \u2192 2 \u2192 3 separation.
# The tuned production defaults (S2=1.0R, S3=0.5*ATR) compress these
# transitions and would make the walks ambiguous. The constant-defaults
# test below pins the production values.
def _restore_original_thresholds(monkeypatch):
    monkeypatch.setattr(_alarm_f_module, "STAGE2_ARM_R_MULT", 2.0)
    monkeypatch.setattr(_alarm_f_module, "STAGE3_ARM_ATR_MULT", 1.5)
    monkeypatch.setattr(_alarm_f_module, "WIDE_MULT", 3.0)
    monkeypatch.setattr(_alarm_f_module, "TIGHT_MULT", 2.0)


def _walk_long(state, *, entry, prices, r, shares, atr):
    stages = []
    for p in prices:
        update_trail(
            state=state,
            side=SIDE_LONG,
            entry_price=entry,
            last_close=p,
            atr_value=atr,
            r_dollars=r,
            shares=shares,
        )
        stages.append(state.stage)
    return stages


def test_stage_transitions_long_walk_through_all_four(monkeypatch):
    _restore_original_thresholds(monkeypatch)
    state = TrailState.fresh()
    # Bars 0,1,2 in noise window; bar 3 arms Stage 1 (+1R = $5);
    # bar 4 arms Stage 2 (+2R); bar 5 arms Stage 3.
    prices = [100.5, 101.0, 102.0, 105.0, 110.0, 112.0]
    stages = _walk_long(state, entry=100.0, prices=prices, r=50.0, shares=10, atr=1.0)
    assert stages[0] == STAGE_INACTIVE
    assert stages[1] == STAGE_INACTIVE
    assert stages[2] == STAGE_INACTIVE
    assert stages[3] == STAGE_BREAKEVEN
    assert stages[4] == STAGE_CHANDELIER_WIDE
    assert stages[5] == STAGE_CHANDELIER_TIGHT


def test_stage_transitions_one_way_only(monkeypatch):
    _restore_original_thresholds(monkeypatch)
    state = TrailState.fresh()
    for p in [100.5, 101.0, 102.0, 105.0, 110.0]:
        update_trail(
            state=state,
            side=SIDE_LONG,
            entry_price=100.0,
            last_close=p,
            atr_value=1.0,
            r_dollars=50.0,
            shares=10,
        )
    assert state.stage == STAGE_CHANDELIER_WIDE
    for p in [101.0, 100.5, 99.0]:
        update_trail(
            state=state,
            side=SIDE_LONG,
            entry_price=100.0,
            last_close=p,
            atr_value=1.0,
            r_dollars=50.0,
            shares=10,
        )
    assert state.stage >= STAGE_CHANDELIER_WIDE


def test_be_arm_requires_full_1R(monkeypatch):
    # This test asserts BE arms ONLY at +1R, not before. With S2_ARM=1.0R
    # (production default), Stage 2 arms simultaneously with Stage 1 at
    # the +$5 bar, which still satisfies stage \u2265 BREAKEVEN, but the
    # original test assertion is `== STAGE_BREAKEVEN`. Restore original
    # S2=2.0R so the bar landing at +1R parks at Stage 1.
    _restore_original_thresholds(monkeypatch)
    state = TrailState.fresh()
    for p in [100.5, 101.0, 102.0, 103.0, 104.0, 104.99]:
        update_trail(
            state=state,
            side=SIDE_LONG,
            entry_price=100.0,
            last_close=p,
            atr_value=1.0,
            r_dollars=50.0,
            shares=10,
        )
    assert state.stage == STAGE_INACTIVE
    update_trail(
        state=state,
        side=SIDE_LONG,
        entry_price=100.0,
        last_close=105.0,
        atr_value=1.0,
        r_dollars=50.0,
        shares=10,
    )
    assert state.stage == STAGE_BREAKEVEN


def test_be_proposed_stop_is_entry_plus_one_cent_long():
    state = TrailState.fresh()
    state.stage = STAGE_BREAKEVEN
    state.peak_close = 105.0
    proposed = propose_stop(
        state=state, side=SIDE_LONG, entry_price=100.0, atr_value=None, current_stop_price=99.0
    )
    assert proposed == 100.01


def test_be_proposed_stop_is_entry_minus_one_cent_short():
    state = TrailState.fresh()
    state.stage = STAGE_BREAKEVEN
    state.peak_close = 95.0
    proposed = propose_stop(
        state=state, side=SIDE_SHORT, entry_price=100.0, atr_value=None, current_stop_price=101.0
    )
    assert proposed == 99.99


def test_stop_monotonic_long_random_walk():
    rng = random.Random(0xC0FFEE)
    state = TrailState.fresh()
    last_proposed = None
    price = 100.0
    for _ in range(200):
        price *= 1.0 + rng.uniform(-0.005, 0.006)
        update_trail(
            state=state,
            side=SIDE_LONG,
            entry_price=100.0,
            last_close=price,
            atr_value=0.5,
            r_dollars=50.0,
            shares=10,
        )
        proposed = propose_stop(
            state=state,
            side=SIDE_LONG,
            entry_price=100.0,
            atr_value=0.5,
            current_stop_price=last_proposed,
        )
        if proposed is None:
            continue
        if last_proposed is not None:
            assert proposed >= last_proposed - 1e-9
        last_proposed = proposed


def test_stop_monotonic_short_random_walk():
    rng = random.Random(0xBADF00D)
    state = TrailState.fresh()
    last_proposed = None
    price = 100.0
    for _ in range(200):
        price *= 1.0 + rng.uniform(-0.006, 0.005)
        update_trail(
            state=state,
            side=SIDE_SHORT,
            entry_price=100.0,
            last_close=price,
            atr_value=0.5,
            r_dollars=50.0,
            shares=10,
        )
        proposed = propose_stop(
            state=state,
            side=SIDE_SHORT,
            entry_price=100.0,
            atr_value=0.5,
            current_stop_price=last_proposed,
        )
        if proposed is None:
            continue
        if last_proposed is not None:
            assert proposed <= last_proposed + 1e-9
        last_proposed = proposed


def test_side_symmetry_chandelier_level(monkeypatch):
    # Walk reaches +$10 favorable (peak 110); to land precisely at
    # STAGE_CHANDELIER_WIDE (and not jump to TIGHT), use original S2=2.0R.
    _restore_original_thresholds(monkeypatch)
    long_state = TrailState.fresh()
    short_state = TrailState.fresh()
    for p in [100.5, 101.0, 102.0, 105.0, 110.0]:
        update_trail(
            state=long_state,
            side=SIDE_LONG,
            entry_price=100.0,
            last_close=p,
            atr_value=1.0,
            r_dollars=50.0,
            shares=10,
        )
    for p in [99.5, 99.0, 98.0, 95.0, 90.0]:
        update_trail(
            state=short_state,
            side=SIDE_SHORT,
            entry_price=100.0,
            last_close=p,
            atr_value=1.0,
            r_dollars=50.0,
            shares=10,
        )
    assert long_state.stage == STAGE_CHANDELIER_WIDE
    assert short_state.stage == STAGE_CHANDELIER_WIDE
    long_stop = propose_stop(
        state=long_state, side=SIDE_LONG, entry_price=100.0, atr_value=1.0, current_stop_price=None
    )
    short_stop = propose_stop(
        state=short_state,
        side=SIDE_SHORT,
        entry_price=100.0,
        atr_value=1.0,
        current_stop_price=None,
    )
    # Read live module value: monkeypatch above swaps WIDE_MULT to 3.0
    # for this test, but the imported `WIDE_MULT` symbol is bound at
    # module load (production default 2.0). Use the live value.
    live_wide = _alarm_f_module.WIDE_MULT
    assert long_stop == 110.0 - live_wide * 1.0
    assert short_stop == 90.0 + live_wide * 1.0
    assert (long_stop - 100.0) == -(short_stop - 100.0)


def test_evaluate_sentinel_emits_alarm_f_when_state_armed():
    state = TrailState.fresh()
    for p in [100.5, 101.0, 102.0, 105.0]:
        update_trail(
            state=state,
            side=SIDE_LONG,
            entry_price=100.0,
            last_close=p,
            atr_value=0.5,
            r_dollars=50.0,
            shares=10,
        )
    assert state.stage >= STAGE_BREAKEVEN
    result = evaluate_sentinel(
        side=SIDE_LONG,
        unrealized_pnl=50.0,
        position_value=1000.0,
        pnl_history=None,
        now_ts=0.0,
        last_5m_close=None,
        last_5m_ema9=None,
        portfolio_value=None,
        adx_window=None,
        current_price=105.0,
        current_shares=10,
        current_stop_price=99.0,
        ticker=None,
        trail_state=state,
        entry_price=100.0,
        last_1m_close=105.0,
        last_1m_atr=0.5,
    )
    f_actions = [a for a in result.alarms if a.alarm == "F"]
    assert len(f_actions) == 1
    f = f_actions[0]
    assert f.reason == EXIT_REASON_ALARM_F
    assert f.detail_stop_price is not None
    assert f.detail_stop_price > 99.0


def test_evaluate_sentinel_skips_alarm_f_when_state_missing():
    result = evaluate_sentinel(
        side=SIDE_LONG,
        unrealized_pnl=50.0,
        position_value=1000.0,
        pnl_history=None,
        now_ts=0.0,
        last_5m_close=None,
        last_5m_ema9=None,
        current_price=105.0,
        current_shares=10,
        current_stop_price=99.0,
        ticker=None,
    )
    f_actions = [a for a in result.alarms if a.alarm == "F"]
    assert f_actions == []


def test_alarm_f_does_not_loosen_long():
    state = TrailState.fresh()
    state.stage = STAGE_BREAKEVEN
    state.peak_close = 105.0
    proposed = propose_stop(
        state=state, side=SIDE_LONG, entry_price=100.0, atr_value=None, current_stop_price=104.0
    )
    assert proposed is None


def test_alarm_f_check_alarm_f_returns_empty_when_no_tighter_proposal():
    state = TrailState.fresh()
    state.stage = STAGE_BREAKEVEN
    state.peak_close = 105.0
    actions = check_alarm_f(
        state=state,
        side=SIDE_LONG,
        entry_price=100.0,
        last_close=105.0,
        atr_value=None,
        r_dollars=50.0,
        shares=10,
        current_stop_price=104.0,
    )
    assert actions == []


def test_atr_from_bars_returns_none_until_period_seen():
    highs = [10.0, 10.5, 11.0, 10.8, 10.7]
    lows = [9.5, 10.0, 10.5, 10.3, 10.2]
    closes = [10.0, 10.3, 10.8, 10.5, 10.4]
    assert atr_from_bars(highs, lows, closes, period=14) is None


def test_atr_from_bars_positive_with_full_window():
    highs = [100.0 + i * 0.5 for i in range(20)]
    lows = [99.0 + i * 0.5 for i in range(20)]
    closes = [99.5 + i * 0.5 for i in range(20)]
    val = atr_from_bars(highs, lows, closes, period=14)
    assert val is not None
    assert val > 0.0


def test_min_bars_before_arm_blocks_early_arming(monkeypatch):
    _restore_original_thresholds(monkeypatch)
    state = TrailState.fresh()
    update_trail(
        state=state,
        side=SIDE_LONG,
        entry_price=100.0,
        last_close=105.0,
        atr_value=1.0,
        r_dollars=50.0,
        shares=10,
    )
    assert state.stage == STAGE_INACTIVE
    update_trail(
        state=state,
        side=SIDE_LONG,
        entry_price=100.0,
        last_close=106.0,
        atr_value=1.0,
        r_dollars=50.0,
        shares=10,
    )
    assert state.stage == STAGE_INACTIVE
    update_trail(
        state=state,
        side=SIDE_LONG,
        entry_price=100.0,
        last_close=107.0,
        atr_value=1.0,
        r_dollars=50.0,
        shares=10,
    )
    assert state.stage == STAGE_BREAKEVEN


def test_constants_match_spec_defaults():
    # v6.4.0 tightened defaults (from Apr 27\u2013May 1 sweep): WIDE 2.0\u21921.5,
    # TIGHT 1.0\u21920.7. v5.28.0 tuned baseline was WIDE=2.0/TIGHT=1.0;
    # original conservative pre-v5.28 defaults (S2=2.0R, WIDE=3.0, TIGHT=2.0,
    # S3=1.5*ATR) are documented in v528 research doc \u00a76.4 for reference.
    assert BE_ARM_R_MULT == 1.0
    assert STAGE2_ARM_R_MULT == 1.0
    assert WIDE_MULT == 1.5
    assert TIGHT_MULT == 0.7
    assert ATR_PERIOD == 14
    assert MIN_BARS_BEFORE_ARM == 3


# ---------------------------------------------------------------------------
# v5.28.0 Re-design tests: closed-bar exit (F_EXIT)
# ---------------------------------------------------------------------------


def test_chandelier_level_returns_none_until_stage_2():
    state = TrailState.fresh()
    state.stage = STAGE_INACTIVE
    state.peak_close = 110.0
    assert chandelier_level(state=state, side=SIDE_LONG, atr_value=1.0) is None
    state.stage = STAGE_BREAKEVEN
    assert chandelier_level(state=state, side=SIDE_LONG, atr_value=1.0) is None
    state.stage = STAGE_CHANDELIER_WIDE
    level = chandelier_level(state=state, side=SIDE_LONG, atr_value=1.0)
    assert level is not None
    # peak - WIDE_MULT * atr
    assert abs(level - (110.0 - WIDE_MULT * 1.0)) < 1e-6


def test_chandelier_level_short_inverts():
    state = TrailState.fresh()
    state.stage = STAGE_CHANDELIER_WIDE
    state.peak_close = 90.0  # short trough
    level = chandelier_level(state=state, side=SIDE_SHORT, atr_value=1.0)
    # trough + WIDE_MULT * atr
    assert level is not None and abs(level - (90.0 + WIDE_MULT * 1.0)) < 1e-6


def test_chandelier_level_tight_uses_tight_mult():
    state = TrailState.fresh()
    state.stage = STAGE_CHANDELIER_TIGHT
    state.peak_close = 110.0
    # peak - TIGHT_MULT * atr
    assert (
        abs(
            chandelier_level(state=state, side=SIDE_LONG, atr_value=1.0)
            - (110.0 - TIGHT_MULT * 1.0)
        )
        < 1e-6
    )


def test_should_exit_on_close_cross_long_fires_below_level():
    state = TrailState.fresh()
    state.stage = STAGE_CHANDELIER_WIDE
    state.peak_close = 110.0
    expected_level = 110.0 - WIDE_MULT * 1.0
    # last_close half a dollar below the level should fire
    out = should_exit_on_close_cross(
        state=state, side=SIDE_LONG, last_close=expected_level - 0.5, atr_value=1.0
    )
    assert out is not None
    assert abs(out - expected_level) < 1e-6


def test_should_exit_on_close_cross_long_no_fire_above_level():
    state = TrailState.fresh()
    state.stage = STAGE_CHANDELIER_WIDE
    state.peak_close = 110.0
    expected_level = 110.0 - WIDE_MULT * 1.0
    out = should_exit_on_close_cross(
        state=state, side=SIDE_LONG, last_close=expected_level + 1.0, atr_value=1.0
    )
    assert out is None


def test_should_exit_on_close_cross_short_fires_above_level():
    state = TrailState.fresh()
    state.stage = STAGE_CHANDELIER_WIDE
    state.peak_close = 90.0
    expected_level = 90.0 + WIDE_MULT * 1.0
    out = should_exit_on_close_cross(
        state=state, side=SIDE_SHORT, last_close=expected_level + 0.5, atr_value=1.0
    )
    assert out is not None
    assert abs(out - expected_level) < 1e-6


def test_should_exit_on_close_cross_no_fire_stage_1():
    state = TrailState.fresh()
    state.stage = STAGE_BREAKEVEN  # NOT armed for chandelier exit
    state.peak_close = 110.0
    out = should_exit_on_close_cross(state=state, side=SIDE_LONG, last_close=99.0, atr_value=1.0)
    assert out is None


def test_check_alarm_f_emits_f_exit_when_close_crosses():
    state = TrailState.fresh()
    state.stage = STAGE_CHANDELIER_WIDE
    state.peak_close = 110.0
    state.bars_seen = 5
    actions = check_alarm_f(
        state=state,
        side=SIDE_LONG,
        entry_price=100.0,
        last_close=106.0,  # below level 107
        atr_value=1.0,
        r_dollars=50.0,
        shares=10,
        current_stop_price=99.0,
    )
    codes = [a.alarm for a in actions]
    assert "F_EXIT" in codes
    f_exit = next(a for a in actions if a.alarm == "F_EXIT")
    assert f_exit.reason == EXIT_REASON_ALARM_F_EXIT


def test_evaluate_sentinel_f_exit_marks_full_exit():
    """Confirm SentinelResult.has_full_exit / exit_reason recognize F_EXIT."""
    state = TrailState.fresh()
    state.stage = STAGE_CHANDELIER_WIDE
    state.peak_close = 110.0
    state.bars_seen = 5
    result = evaluate_sentinel(
        side=SIDE_LONG,
        unrealized_pnl=0.0,  # not -$500, no Alarm A trigger
        position_value=1000.0,
        pnl_history=None,
        now_ts=1000.0,
        last_5m_close=109.0,
        last_5m_ema9=108.5,  # above EMA, no Alarm B
        prev_5m_close=109.5,
        prev_5m_ema9=108.5,
        alarm_b_confirm_bars=2,
        trail_state=state,
        entry_price=100.0,
        last_1m_close=106.0,  # below chandelier 107
        last_1m_atr=1.0,
        current_shares=10,
    )
    assert result.has_full_exit
    assert result.exit_reason == EXIT_REASON_ALARM_F_EXIT


def test_v5_28_simplified_portfolio_disables_c_d_e_by_default():
    """Smoke-check the v5.28.0 portfolio simplification flags."""
    from engine.sentinel import (
        ALARM_C_ENABLED,
        ALARM_D_ENABLED,
        ALARM_E_ENABLED,
    )

    assert ALARM_C_ENABLED is False
    assert ALARM_D_ENABLED is False
    assert ALARM_E_ENABLED is False
