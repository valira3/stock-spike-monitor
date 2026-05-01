"""v6.1.0 #1: ATR-scaled trailing stop with profit-protect ratchet.

Tests for the three-stage ATR trail introduced in check_alarm_a_stop_price
and the _compute_atr_trail_distance helper.

No em-dashes in this file (raw or escaped) per project constraint.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Helpers: synthetic 1-minute bar builder
# ---------------------------------------------------------------------------


def _make_bars(n: int, high: float, low: float, close: float) -> list[dict]:
    """Return n synthetic 1-minute bars with constant OHLC values.

    Useful for building a stable ATR from known true-range values.
    With constant values, TR = high - low each bar, so
    ATR(5) = high - low.
    """
    return [{"open": close, "high": high, "low": low, "close": close}] * n


# ---------------------------------------------------------------------------
# Test 1: Stage 1 uses 1x ATR trail
# ---------------------------------------------------------------------------


def test_stage1_uses_1x_atr():
    """Stage 1: position 0.5 x ATR in profit -> stop distance == 1.0 x ATR."""
    from engine.sentinel import _compute_atr_trail_distance

    atr = 2.0
    pnl_per_share = 0.5 * atr   # 1.0 -- below the 1x ATR Stage 1 threshold

    trail = _compute_atr_trail_distance(
        atr=atr,
        position_pnl_per_share=pnl_per_share,
        peak_open_profit_per_share=pnl_per_share,
    )

    assert trail == pytest.approx(1.0 * atr), (
        f"Stage 1 trail should equal 1.0 x ATR ({1.0 * atr}), got {trail}"
    )


# ---------------------------------------------------------------------------
# Test 2: Stage 2 widens to 1.5x ATR
# ---------------------------------------------------------------------------


def test_stage2_widens_to_1_5x():
    """Stage 2: position 1.5 x ATR in profit -> stop distance == 1.5 x ATR."""
    from engine.sentinel import _compute_atr_trail_distance

    atr = 2.0
    pnl_per_share = 1.5 * atr   # between 1x and 3x ATR -- Stage 2

    trail = _compute_atr_trail_distance(
        atr=atr,
        position_pnl_per_share=pnl_per_share,
        peak_open_profit_per_share=pnl_per_share,
    )

    assert trail == pytest.approx(1.5 * atr), (
        f"Stage 2 trail should equal 1.5 x ATR ({1.5 * atr}), got {trail}"
    )


# ---------------------------------------------------------------------------
# Test 3: Stage 3 lock-in gives back at most 50% of peak open profit
# ---------------------------------------------------------------------------


def test_stage3_lockin_50pct():
    """Stage 3: pnl=4xATR, peak=5xATR -> trail == 0.5 x 5xATR == 2.5xATR."""
    from engine.sentinel import _compute_atr_trail_distance

    atr = 1.0
    pnl_per_share = 4.0 * atr      # above the 3x threshold -> Stage 3
    peak_per_share = 5.0 * atr     # peak was 5x ATR

    trail = _compute_atr_trail_distance(
        atr=atr,
        position_pnl_per_share=pnl_per_share,
        peak_open_profit_per_share=peak_per_share,
    )

    expected = 0.5 * peak_per_share  # 2.5
    assert trail == pytest.approx(expected), (
        f"Stage 3 trail should be 50% of peak ({expected}), got {trail}"
    )


# ---------------------------------------------------------------------------
# Test 4: Absolute floor of 0.3 x ATR
# ---------------------------------------------------------------------------


def test_floor_03x_atr():
    """Trail must never be tighter than 0.3 x ATR regardless of profit stage.

    Use a very small peak so the 50%-of-peak Stage 3 computation would
    return a near-zero value without the floor guard.
    """
    from engine.sentinel import _compute_atr_trail_distance

    atr = 1.0
    # Stage 3 territory (pnl > 3x ATR) but peak is tiny.
    pnl_per_share = 4.0 * atr
    peak_per_share = 0.01         # 50% of this = 0.005 -- well below floor

    trail = _compute_atr_trail_distance(
        atr=atr,
        position_pnl_per_share=pnl_per_share,
        peak_open_profit_per_share=peak_per_share,
    )

    floor = 0.3 * atr
    assert trail >= floor - 1e-9, (
        f"Trail {trail} must be >= floor {floor} (0.3 x ATR={atr})"
    )


# ---------------------------------------------------------------------------
# Test 5: Disabled flag falls back to fixed-cents path
# ---------------------------------------------------------------------------


def test_disabled_flag_falls_back(monkeypatch):
    """With _V610_ATR_TRAIL_ENABLED=False the ATR logic is skipped entirely
    and the original fixed-cents stop price is used unmodified.
    """
    import engine.sentinel as sentinel_mod
    from engine.sentinel import SIDE_LONG, EXIT_REASON_PRICE_STOP

    # Disable the v6.1.0 feature flag.
    monkeypatch.setattr(sentinel_mod, "_V610_ATR_TRAIL_ENABLED", False)

    # Build scenario: entry=100, pnl=0.5, atr=1.0
    # With ATR trail enabled, stop = 100 + 0.5 - 1.0 = 99.5
    # With flag OFF, stop stays at the supplied current_stop_price = 99.0.
    # Mark = 99.4 is above 99.0 -> should NOT fire.
    result = sentinel_mod.check_alarm_a_stop_price(
        side=SIDE_LONG,
        current_price=99.4,
        current_stop_price=99.0,   # fixed-cents stop
        atr_value=1.0,
        position_pnl_per_share=0.5,
        peak_open_profit_per_share=0.5,
        entry_price=100.0,
    )
    assert result == [], (
        "Flag OFF: mark 99.4 is above fixed-cents stop 99.0 -- must NOT fire"
    )

    # Now move mark below fixed-cents stop -> DOES fire.
    result_fire = sentinel_mod.check_alarm_a_stop_price(
        side=SIDE_LONG,
        current_price=98.9,
        current_stop_price=99.0,
        atr_value=1.0,
        position_pnl_per_share=0.5,
        peak_open_profit_per_share=0.5,
        entry_price=100.0,
    )
    assert len(result_fire) == 1
    assert result_fire[0].alarm == "A_STOP_PRICE"
    assert result_fire[0].reason == EXIT_REASON_PRICE_STOP
    # Confirm the ATR tag is NOT present (fixed-cents path ran, not ATR path).
    assert "atr_trail" not in result_fire[0].detail
