"""Unit tests for engine/sentinel.py (v5.13.0 PR 2).

Covers Tiger Sovereign Phase 4 alarms A (-$500 / -1%/min) and B
(closed 5m vs 9-EMA). Asserts both fire INDEPENDENTLY in the same
tick (parallel-not-sequential) and that exactly one exit reason is
emitted even when multiple alarms trip.
"""
from __future__ import annotations

from collections import deque

import pytest

from engine.sentinel import (
    ALARM_A_HARD_LOSS_DOLLARS,
    EXIT_REASON_ALARM_A,
    EXIT_REASON_ALARM_B,
    SIDE_LONG,
    SIDE_SHORT,
    check_alarm_a,
    check_alarm_b,
    evaluate_sentinel,
    new_pnl_history,
    record_pnl,
)


# ---------------------------------------------------------------------------
# Alarm A1 \u2014 hard floor at -$500
# ---------------------------------------------------------------------------


def test_alarm_a1_fires_at_exactly_minus_500():
    """A1 must fire at the spec-literal boundary: pnl <= -$500."""
    fired = check_alarm_a(
        side=SIDE_LONG,
        unrealized_pnl=-500.0,
        position_value=10000.0,
        pnl_history=None,
        now_ts=1000.0,
    )
    assert any(a.alarm == "A1" for a in fired), (
        "A1 must fire at exactly -$500 (boundary inclusive)"
    )
    a1 = next(a for a in fired if a.alarm == "A1")
    assert a1.reason == EXIT_REASON_ALARM_A


def test_alarm_a1_does_not_fire_at_minus_499():
    """At -$499 the position still has -$1 of headroom \u2014 no fire."""
    fired = check_alarm_a(
        side=SIDE_LONG,
        unrealized_pnl=-499.0,
        position_value=10000.0,
        pnl_history=None,
        now_ts=1000.0,
    )
    assert not any(a.alarm == "A1" for a in fired)


def test_alarm_a1_constant_is_exactly_minus_500():
    assert ALARM_A_HARD_LOSS_DOLLARS == -500.0


# ---------------------------------------------------------------------------
# Alarm A2 \u2014 -1% over 60 seconds
# ---------------------------------------------------------------------------


def test_alarm_a2_fires_when_60s_velocity_is_minus_1_01_percent():
    """A2 fires when (delta / position_value) <= -0.01 over the last 60s.

    Delta of -101 on a $10,000 notional is -1.01% \u2014 fires.
    """
    history = new_pnl_history()
    record_pnl(history, ts=1000.0, pnl=0.0)
    fired = check_alarm_a(
        side=SIDE_LONG,
        unrealized_pnl=-101.0,
        position_value=10000.0,
        pnl_history=history,
        now_ts=1060.0,
    )
    assert any(a.alarm == "A2" for a in fired), (
        "A2 must fire when 60s velocity is -1.01%"
    )


def test_alarm_a2_does_not_fire_at_minus_0_99_percent():
    """At -0.99% over 60s, A2 does NOT fire."""
    history = new_pnl_history()
    record_pnl(history, ts=1000.0, pnl=0.0)
    fired = check_alarm_a(
        side=SIDE_LONG,
        unrealized_pnl=-99.0,
        position_value=10000.0,
        pnl_history=history,
        now_ts=1060.0,
    )
    assert not any(a.alarm == "A2" for a in fired)


def test_alarm_a2_does_not_fire_without_history():
    """No history sample at-or-before now-60s \u2014 cannot evaluate."""
    history = deque()
    fired = check_alarm_a(
        side=SIDE_LONG,
        unrealized_pnl=-200.0,
        position_value=10000.0,
        pnl_history=history,
        now_ts=1060.0,
    )
    assert not any(a.alarm == "A2" for a in fired)


def test_alarm_a2_short_side_uses_signed_pnl():
    """Short side: P&L convention (entry - current) * shares.

    A short whose P&L drops by -$200 over 60s on $10k notional is
    -2% velocity \u2014 fires.
    """
    history = new_pnl_history()
    record_pnl(history, ts=1000.0, pnl=0.0)
    fired = check_alarm_a(
        side=SIDE_SHORT,
        unrealized_pnl=-200.0,
        position_value=10000.0,
        pnl_history=history,
        now_ts=1060.0,
    )
    assert any(a.alarm == "A2" for a in fired)


# ---------------------------------------------------------------------------
# Alarm B \u2014 closed 5m close vs 9-EMA
# ---------------------------------------------------------------------------


def test_alarm_b_long_fires_on_close_below_ema9():
    fired = check_alarm_b(
        side=SIDE_LONG,
        last_5m_close=99.50,
        last_5m_ema9=100.00,
    )
    assert len(fired) == 1
    assert fired[0].alarm == "B"
    assert fired[0].reason == EXIT_REASON_ALARM_B


def test_alarm_b_long_does_not_fire_on_close_above_ema9():
    fired = check_alarm_b(
        side=SIDE_LONG,
        last_5m_close=100.50,
        last_5m_ema9=100.00,
    )
    assert fired == []


def test_alarm_b_short_fires_on_close_above_ema9():
    fired = check_alarm_b(
        side=SIDE_SHORT,
        last_5m_close=100.50,
        last_5m_ema9=100.00,
    )
    assert len(fired) == 1
    assert fired[0].alarm == "B"


def test_alarm_b_short_does_not_fire_on_close_below_ema9():
    fired = check_alarm_b(
        side=SIDE_SHORT,
        last_5m_close=99.50,
        last_5m_ema9=100.00,
    )
    assert fired == []


def test_alarm_b_skipped_when_ema9_unseeded():
    """No EMA9 yet (less than 9 closed 5m bars) \u2014 alarm is silent."""
    assert check_alarm_b(
        side=SIDE_LONG,
        last_5m_close=99.0,
        last_5m_ema9=None,
    ) == []


# ---------------------------------------------------------------------------
# PARALLEL semantics \u2014 the headline test
# ---------------------------------------------------------------------------


def test_alarms_a_and_b_fire_independently_in_same_tick():
    """Construct a scenario where BOTH A and B trip on the same tick.

    The spec emphasizes "These Alarms are NOT a sequence." This test
    is the parallel-not-sequential guarantee: both must appear in the
    SentinelResult, not just whichever evaluator runs first.
    """
    history = new_pnl_history()
    record_pnl(history, ts=1000.0, pnl=0.0)

    result = evaluate_sentinel(
        side=SIDE_LONG,
        unrealized_pnl=-600.0,        # triggers A1 (hard floor)
        position_value=10000.0,
        pnl_history=history,
        now_ts=1060.0,                # 60s after history start
        last_5m_close=99.0,           # below EMA9 \u2014 triggers B
        last_5m_ema9=100.0,
    )

    codes = result.alarm_codes
    assert "A1" in codes, f"expected A1 in {codes}"
    assert "B" in codes, f"expected B in {codes}"
    # And A2 because pnl dropped from 0 to -600 over 60s on $10k = -6%
    assert "A2" in codes, f"expected A2 in {codes}"


def test_one_exit_reason_even_with_multiple_alarms():
    """If A and B both fire, ``exit_reason`` returns exactly one code.

    The downstream broker emits one exit order \u2014 but the full alarm
    list is preserved for telemetry.
    """
    history = new_pnl_history()
    record_pnl(history, ts=1000.0, pnl=0.0)
    result = evaluate_sentinel(
        side=SIDE_LONG,
        unrealized_pnl=-600.0,
        position_value=10000.0,
        pnl_history=history,
        now_ts=1060.0,
        last_5m_close=99.0,
        last_5m_ema9=100.0,
    )
    assert result.exit_reason == EXIT_REASON_ALARM_A  # A wins precedence
    assert len(result.alarms) >= 2  # but B is still in the list


def test_evaluate_sentinel_no_fire_at_clean_state():
    """Healthy position: no history conflict, close above EMA9, pnl flat."""
    history = new_pnl_history()
    record_pnl(history, ts=1000.0, pnl=0.0)
    result = evaluate_sentinel(
        side=SIDE_LONG,
        unrealized_pnl=10.0,
        position_value=10000.0,
        pnl_history=history,
        now_ts=1060.0,
        last_5m_close=101.0,
        last_5m_ema9=100.0,
    )
    assert not result.fired
    assert result.exit_reason is None


def test_evaluate_sentinel_only_b_fires_when_a_quiet():
    """Alarm B must fire even when A is silent. Independence test."""
    history = new_pnl_history()
    record_pnl(history, ts=1000.0, pnl=-50.0)
    result = evaluate_sentinel(
        side=SIDE_LONG,
        unrealized_pnl=-90.0,          # well above A1 floor (-$500)
        position_value=10000.0,        # delta of -$40 / $10k = -0.40% (< 1%)
        pnl_history=history,
        now_ts=1060.0,
        last_5m_close=99.0,            # below EMA9
        last_5m_ema9=100.0,
    )
    assert result.alarm_codes == ["B"]
    assert result.exit_reason == EXIT_REASON_ALARM_B


def test_pnl_history_is_bounded():
    """Memory hygiene: history deque caps at PNL_HISTORY_MAXLEN."""
    history = new_pnl_history()
    for i in range(500):
        record_pnl(history, ts=float(i), pnl=float(i))
    from engine.sentinel import PNL_HISTORY_MAXLEN
    assert len(history) == PNL_HISTORY_MAXLEN


# ---------------------------------------------------------------------------
# Spec wiring sanity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("side", [SIDE_LONG, SIDE_SHORT])
def test_a_and_b_fire_for_both_sides(side):
    """Spec rule mirrors: the same semantics apply long and short."""
    history = new_pnl_history()
    record_pnl(history, ts=1000.0, pnl=0.0)
    if side == SIDE_LONG:
        close, ema = 99.0, 100.0
    else:
        close, ema = 101.0, 100.0
    result = evaluate_sentinel(
        side=side,
        unrealized_pnl=-700.0,
        position_value=10000.0,
        pnl_history=history,
        now_ts=1060.0,
        last_5m_close=close,
        last_5m_ema9=ema,
    )
    codes = result.alarm_codes
    assert "A1" in codes and "B" in codes
