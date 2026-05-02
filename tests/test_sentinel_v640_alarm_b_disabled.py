"""v6.4.0 - ALARM_B_ENABLED=False ship gate.

The Apr 27 - May 1 sweep showed disabling Sentinel B (5m close vs 9-EMA
cross) plus tightening the Alarm F Chandelier multipliers to 1.5/0.7
swung the week +$217.93 (+$831.50 -> +$1,049.43, 60 pairs, WR 45% -> 62%).

These tests assert two things in lock-step:

  1. With the new module-level constant ALARM_B_ENABLED=False (the v6.4.0
     production default), evaluate_sentinel must NOT call check_alarm_b
     and must NOT emit any EXIT_REASON_ALARM_B alarms even on a
     known-cross fixture.

  2. Flipping ALARM_B_ENABLED back to True restores the legacy behaviour
     so the rollback path is tested (a future release can flip the flag
     without code edits).

  3. The Alarm F default multipliers ship as 1.5 / 0.7 (the winning
     sweep config). A regression that reverts these to 2.0 / 1.0 should
     fail the test rather than silently double the trail width.
"""

from __future__ import annotations

import pytest

import engine.sentinel as sentinel_mod
import engine.alarm_f_trail as f_trail_mod
from engine.sentinel import (
    SIDE_LONG,
    EXIT_REASON_ALARM_B,
    evaluate_sentinel,
    new_pnl_history,
    reset_ema_cross_pending,
)


@pytest.fixture(autouse=True)
def _reset_state():
    reset_ema_cross_pending()
    yield
    reset_ema_cross_pending()


def _eval_known_b_cross(*, side: str = SIDE_LONG):
    """Drive evaluate_sentinel through a textbook B-cross condition twice.

    LONG cross: last_5m_close < last_5m_ema9 (price below EMA9). The
    v6.1.0 stateful path requires two consecutive cross bars before B
    fires; we issue two calls so the cross would fire under any legacy
    setting if ALARM_B_ENABLED is True.
    """
    common = dict(
        side=side,
        unrealized_pnl=0.0,
        position_value=10_000.0,
        pnl_history=new_pnl_history(),
        now_ts=0.0,
        last_5m_close=99.0,
        last_5m_ema9=100.0,
        prev_5m_close=99.0,
        prev_5m_ema9=100.0,
        alarm_b_confirm_bars=1,
        position_id="v640-smoke-pid",
        # entry_price + last_1m_atr left None so noise-cross filter sits
        # out and any cross would fire under legacy v6.3.0+v6.1.0 path.
        current_price=99.0,
    )
    evaluate_sentinel(**common)
    return evaluate_sentinel(**common)


def test_v640_alarm_b_disabled_default_does_not_fire_on_cross():
    """ALARM_B_ENABLED=False is the v6.4.0 ship default."""
    assert sentinel_mod.ALARM_B_ENABLED is False, (
        "v6.4.0 ships with ALARM_B_ENABLED=False as the production default. "
        "If this assertion fails the constant has been changed; a future "
        "release that wants B back on should be a separate, signed-off PR."
    )

    result = _eval_known_b_cross()

    b_alarms = [a for a in result.alarms if a.reason == EXIT_REASON_ALARM_B]
    assert len(b_alarms) == 0, (
        f"Expected zero Alarm B firings with ALARM_B_ENABLED=False on a "
        f"known-cross fixture; got {len(b_alarms)}: {b_alarms}"
    )


def test_v640_alarm_b_enable_via_monkeypatch_restores_firing(monkeypatch):
    """Flipping ALARM_B_ENABLED=True restores legacy B-cross behaviour."""
    monkeypatch.setattr(sentinel_mod, "ALARM_B_ENABLED", True)

    result = _eval_known_b_cross()

    b_alarms = [a for a in result.alarms if a.reason == EXIT_REASON_ALARM_B]
    assert len(b_alarms) >= 1, (
        f"Expected at least one Alarm B firing on a known-cross fixture "
        f"when ALARM_B_ENABLED is monkeypatched back to True; got "
        f"{len(b_alarms)}. The rollback path is broken."
    )


def test_v640_alarm_f_default_multipliers_are_winning_sweep_config():
    """Alarm F WIDE_MULT/TIGHT_MULT must match the winning sweep config."""
    assert f_trail_mod.WIDE_MULT == pytest.approx(1.5), (
        f"v6.4.0 ships WIDE_MULT=1.5 (winning sweep config). "
        f"Got {f_trail_mod.WIDE_MULT}; a regression to 2.0 wipes "
        f"out ~$152/wk of expected backtest upside."
    )
    assert f_trail_mod.TIGHT_MULT == pytest.approx(0.7), (
        f"v6.4.0 ships TIGHT_MULT=0.7 (winning sweep config). "
        f"Got {f_trail_mod.TIGHT_MULT}."
    )


def test_v640_check_alarm_b_not_called_when_disabled(monkeypatch):
    """evaluate_sentinel must skip check_alarm_b entirely (no log noise)."""
    call_count = {"n": 0}

    real_check_b = sentinel_mod.check_alarm_b

    def _spy_check_b(*args, **kwargs):
        call_count["n"] += 1
        return real_check_b(*args, **kwargs)

    monkeypatch.setattr(sentinel_mod, "check_alarm_b", _spy_check_b)
    # ALARM_B_ENABLED is False by default in v6.4.0; do not flip it.
    _eval_known_b_cross()

    assert call_count["n"] == 0, (
        f"With ALARM_B_ENABLED=False, evaluate_sentinel must not call "
        f"check_alarm_b at all (no state mutation, no log lines). Got "
        f"{call_count['n']} calls."
    )
