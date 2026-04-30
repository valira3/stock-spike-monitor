"""v5.15.0 PR-4 \u2014 unit tests for engine.velocity_ratchet.

Spec rules covered:
* SENT-C velocity ratchet  \u2014 fires on three strictly-decreasing 1m
  ADX samples; tightens stop by 0.25%.
* SENT-C strictly monotone \u2014 equality on either pair fails the trigger.
* SENT-C ratchet does not loosen \u2014 if existing stop is already tighter,
  emit nothing.
"""

from __future__ import annotations

import pytest

from engine.momentum_state import ADXTrendWindow
from engine.velocity_ratchet import (
    EXIT_REASON_VELOCITY_RATCHET,
    RATCHET_STOP_PCT,
    RatchetDecision,
    evaluate_velocity_ratchet,
)


def _decreasing_window(samples=(40.0, 30.0, 20.0)) -> ADXTrendWindow:
    w = ADXTrendWindow()
    for s in samples:
        w.push(s)
    return w


def test_constant_is_25_basis_points():
    assert RATCHET_STOP_PCT == pytest.approx(0.0025)


def test_long_fires_when_strictly_decreasing_and_no_existing_stop():
    """Strictly-decreasing 1m ADX + no existing stop \u2192 emit at -0.25%."""
    w = _decreasing_window()
    decision = evaluate_velocity_ratchet(
        side="LONG",
        adx_window=w,
        current_price=100.0,
        existing_stop_price=None,
    )
    assert decision.should_emit_stop is True
    assert decision.new_stop_price == pytest.approx(100.0 * (1 - 0.0025))
    assert decision.reason == "velocity_ratchet_fired"


def test_short_fires_with_mirrored_stop():
    """SHORT mirror: protective stop is current_price * (1 + 0.25%)."""
    w = _decreasing_window()
    decision = evaluate_velocity_ratchet(
        side="SHORT",
        adx_window=w,
        current_price=200.0,
        existing_stop_price=None,
    )
    assert decision.should_emit_stop is True
    assert decision.new_stop_price == pytest.approx(200.0 * (1 + 0.0025))


def test_no_fire_when_window_has_fewer_than_three_samples():
    """Empty / partial window never fires \u2014 spec needs 3 samples."""
    for samples in [(), (40.0,), (40.0, 30.0)]:
        w = ADXTrendWindow()
        for s in samples:
            w.push(s)
        d = evaluate_velocity_ratchet(
            side="LONG",
            adx_window=w,
            current_price=100.0,
            existing_stop_price=None,
        )
        assert d.should_emit_stop is False
        assert d.reason == "adx_not_strictly_decreasing"


def test_no_fire_when_window_is_flat_or_increasing():
    """Equality on either pair fails the strict-monotone trigger."""
    cases = [
        (30.0, 30.0, 30.0),  # flat
        (40.0, 30.0, 30.0),  # tail equal
        (30.0, 30.0, 20.0),  # head equal
        (20.0, 30.0, 40.0),  # increasing
        (20.0, 40.0, 30.0),  # zig-zag
    ]
    for samples in cases:
        w = ADXTrendWindow()
        for s in samples:
            w.push(s)
        d = evaluate_velocity_ratchet(
            side="LONG",
            adx_window=w,
            current_price=100.0,
            existing_stop_price=None,
        )
        assert d.should_emit_stop is False, f"window {samples} should NOT fire"


def test_long_does_not_loosen_when_existing_stop_already_tighter():
    """LONG never-loosen: existing stop higher than proposal \u2192 no emit."""
    w = _decreasing_window()
    proposed = 100.0 * (1 - 0.0025)  # 99.75
    existing_tighter = proposed + 0.10  # higher = tighter for LONG
    d = evaluate_velocity_ratchet(
        side="LONG",
        adx_window=w,
        current_price=100.0,
        existing_stop_price=existing_tighter,
    )
    assert d.should_emit_stop is False
    assert d.reason == "not_tighter_than_existing_stop"
    # Telemetry: proposal still recorded for observability.
    assert d.new_stop_price == pytest.approx(proposed)


def test_short_does_not_loosen_when_existing_stop_already_tighter():
    """SHORT never-loosen: existing stop lower than proposal \u2192 no emit."""
    w = _decreasing_window()
    proposed = 200.0 * (1 + 0.0025)  # 200.50
    existing_tighter = proposed - 0.10  # lower = tighter for SHORT
    d = evaluate_velocity_ratchet(
        side="SHORT",
        adx_window=w,
        current_price=200.0,
        existing_stop_price=existing_tighter,
    )
    assert d.should_emit_stop is False
    assert d.reason == "not_tighter_than_existing_stop"


def test_long_fires_when_existing_stop_is_below_proposal():
    """LONG: existing stop lower than proposal is looser \u2192 ratchet emits."""
    w = _decreasing_window()
    proposed = 100.0 * (1 - 0.0025)
    existing_looser = proposed - 0.50
    d = evaluate_velocity_ratchet(
        side="LONG",
        adx_window=w,
        current_price=100.0,
        existing_stop_price=existing_looser,
    )
    assert d.should_emit_stop is True
    assert d.new_stop_price == pytest.approx(proposed)


def test_short_fires_when_existing_stop_is_above_proposal():
    """SHORT: existing stop higher than proposal is looser \u2192 emit."""
    w = _decreasing_window()
    proposed = 200.0 * (1 + 0.0025)
    existing_looser = proposed + 0.50
    d = evaluate_velocity_ratchet(
        side="SHORT",
        adx_window=w,
        current_price=200.0,
        existing_stop_price=existing_looser,
    )
    assert d.should_emit_stop is True
    assert d.new_stop_price == pytest.approx(proposed)


def test_decision_is_immutable_dataclass():
    """RatchetDecision is frozen \u2014 callers cannot mutate it."""
    d = RatchetDecision(should_emit_stop=False, new_stop_price=None, reason="x")
    with pytest.raises((AttributeError, Exception)):
        d.reason = "y"  # type: ignore[misc]


def test_invalid_side_raises():
    """Bad side string is a programmer error \u2014 raise loudly."""
    w = _decreasing_window()
    with pytest.raises(ValueError):
        evaluate_velocity_ratchet(
            side="WRONG",  # type: ignore[arg-type]
            adx_window=w,
            current_price=100.0,
            existing_stop_price=None,
        )


def test_exit_reason_constant_is_stable_string():
    """Downstream telemetry / broker code keys off this string."""
    assert EXIT_REASON_VELOCITY_RATCHET == "sentinel_velocity_ratchet"
