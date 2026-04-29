"""v5.15.0 PR-3a \u2014 unit tests for engine.momentum_state.

Covers TradeHVP (per-Strike peak 5m ADX), DivergenceMemory
(per-(ticker, side) Stored_Peak_Price / Stored_Peak_RSI), and
ADXTrendWindow (3-element strict-decreasing ring). These are
foundation-only tests; no callers wire the classes yet.
"""

from __future__ import annotations

import pytest

from engine.momentum_state import ADXTrendWindow, DivergenceMemory, TradeHVP


# ---------------------------------------------------------------------------
# TradeHVP
# ---------------------------------------------------------------------------


def test_tradehvp_peak_before_open_raises():
    hvp = TradeHVP()
    with pytest.raises(RuntimeError):
        _ = hvp.peak


def test_tradehvp_on_strike_open_seeds_peak():
    hvp = TradeHVP()
    hvp.on_strike_open(initial_adx_5m=22.0)
    assert hvp.peak == 22.0


def test_tradehvp_update_is_max_monotone_up():
    hvp = TradeHVP()
    hvp.on_strike_open(initial_adx_5m=22.0)
    hvp.update(current_adx_5m=30.0)
    hvp.update(current_adx_5m=27.0)  # lower reading must not drop the peak
    assert hvp.peak == 30.0


def test_tradehvp_update_ignored_when_no_strike_open():
    hvp = TradeHVP()
    # No on_strike_open called: update is a no-op and peak still raises.
    hvp.update(current_adx_5m=99.0)
    with pytest.raises(RuntimeError):
        _ = hvp.peak


def test_tradehvp_on_strike_open_resets_peak():
    hvp = TradeHVP()
    hvp.on_strike_open(initial_adx_5m=22.0)
    hvp.update(current_adx_5m=40.0)
    assert hvp.peak == 40.0
    hvp.on_strike_open(initial_adx_5m=18.0)
    # New Strike: previous HVP does NOT carry over.
    assert hvp.peak == 18.0


# ---------------------------------------------------------------------------
# DivergenceMemory
# ---------------------------------------------------------------------------


def test_divergence_memory_peak_returns_none_before_update():
    mem = DivergenceMemory()
    assert mem.peak("AAPL", "LONG") is None


def test_divergence_memory_separate_state_per_ticker_and_side():
    mem = DivergenceMemory()
    mem.update("AAPL", "LONG", price=190.0, rsi=72.0)
    mem.update("AAPL", "SHORT", price=185.0, rsi=28.0)
    mem.update("MSFT", "LONG", price=410.0, rsi=68.0)
    assert mem.peak("AAPL", "LONG") == (190.0, 72.0)
    assert mem.peak("AAPL", "SHORT") == (185.0, 28.0)
    assert mem.peak("MSFT", "LONG") == (410.0, 68.0)
    # Untouched (ticker, side) keys remain None.
    assert mem.peak("MSFT", "SHORT") is None


def test_divergence_memory_long_update_only_when_both_conditions_met():
    mem = DivergenceMemory()
    mem.update("AAPL", "LONG", price=190.0, rsi=72.0)

    # Higher price BUT lower RSI: rejected (rsi >= stored_rsi violated).
    mem.update("AAPL", "LONG", price=192.0, rsi=70.0)
    assert mem.peak("AAPL", "LONG") == (190.0, 72.0)

    # Equal price BUT higher RSI: rejected (price > stored_price violated).
    mem.update("AAPL", "LONG", price=190.0, rsi=80.0)
    assert mem.peak("AAPL", "LONG") == (190.0, 72.0)

    # Higher price AND equal RSI: accepted (rsi >= is inclusive).
    mem.update("AAPL", "LONG", price=193.0, rsi=72.0)
    assert mem.peak("AAPL", "LONG") == (193.0, 72.0)

    # Higher price AND higher RSI: accepted.
    mem.update("AAPL", "LONG", price=195.0, rsi=78.0)
    assert mem.peak("AAPL", "LONG") == (195.0, 78.0)


def test_divergence_memory_short_update_mirrors_long():
    mem = DivergenceMemory()
    mem.update("NVDA", "SHORT", price=400.0, rsi=30.0)

    # Lower price BUT higher RSI: rejected (rsi <= stored_rsi violated).
    mem.update("NVDA", "SHORT", price=395.0, rsi=35.0)
    assert mem.peak("NVDA", "SHORT") == (400.0, 30.0)

    # Lower price AND lower RSI: accepted.
    mem.update("NVDA", "SHORT", price=390.0, rsi=25.0)
    assert mem.peak("NVDA", "SHORT") == (390.0, 25.0)


def test_divergence_memory_is_diverging_long_bearish():
    mem = DivergenceMemory()
    mem.update("AAPL", "LONG", price=190.0, rsi=72.0)
    # Price prints fresh high BUT 15m RSI is lower than stored: bearish
    # divergence into a price high.
    assert mem.is_diverging("AAPL", "LONG", current_price=192.0, current_rsi_15=68.0) is True
    # Price higher AND RSI also higher: NOT diverging.
    assert mem.is_diverging("AAPL", "LONG", current_price=192.0, current_rsi_15=75.0) is False
    # Price equal: NOT diverging (need strict price >).
    assert mem.is_diverging("AAPL", "LONG", current_price=190.0, current_rsi_15=60.0) is False


def test_divergence_memory_is_diverging_short_bullish():
    mem = DivergenceMemory()
    mem.update("NVDA", "SHORT", price=400.0, rsi=30.0)
    # Price prints fresh low BUT 15m RSI is higher than stored: bullish
    # divergence into a price low.
    assert mem.is_diverging("NVDA", "SHORT", current_price=395.0, current_rsi_15=35.0) is True
    # Price lower AND RSI also lower: NOT diverging.
    assert mem.is_diverging("NVDA", "SHORT", current_price=395.0, current_rsi_15=25.0) is False


def test_divergence_memory_is_diverging_returns_false_with_no_peak():
    mem = DivergenceMemory()
    # Nothing stored: cannot diverge.
    assert mem.is_diverging("AAPL", "LONG", current_price=200.0, current_rsi_15=10.0) is False


def test_divergence_memory_session_reset_clears_all():
    mem = DivergenceMemory()
    mem.update("AAPL", "LONG", price=190.0, rsi=72.0)
    mem.update("AAPL", "SHORT", price=185.0, rsi=28.0)
    mem.update("MSFT", "LONG", price=410.0, rsi=68.0)
    mem.session_reset()
    assert mem.peak("AAPL", "LONG") is None
    assert mem.peak("AAPL", "SHORT") is None
    assert mem.peak("MSFT", "LONG") is None


# ---------------------------------------------------------------------------
# ADXTrendWindow
# ---------------------------------------------------------------------------


def test_adx_trend_window_returns_false_until_three_values():
    w = ADXTrendWindow()
    assert w.is_strictly_decreasing() is False
    w.push(30.0)
    assert w.is_strictly_decreasing() is False
    w.push(25.0)
    assert w.is_strictly_decreasing() is False
    w.push(20.0)
    assert w.is_strictly_decreasing() is True


def test_adx_trend_window_strict_decrease_detected():
    w = ADXTrendWindow()
    for v in (30.0, 25.0, 20.0):
        w.push(v)
    assert w.is_strictly_decreasing() is True


def test_adx_trend_window_equality_fails_strict():
    w = ADXTrendWindow()
    for v in (30.0, 30.0, 25.0):
        w.push(v)
    assert w.is_strictly_decreasing() is False
    w2 = ADXTrendWindow()
    for v in (30.0, 25.0, 25.0):
        w2.push(v)
    assert w2.is_strictly_decreasing() is False


def test_adx_trend_window_non_monotone_returns_false():
    w = ADXTrendWindow()
    for v in (25.0, 30.0, 20.0):
        w.push(v)
    assert w.is_strictly_decreasing() is False


def test_adx_trend_window_is_a_ring_buffer_of_three():
    # Pushing four values evicts the oldest; the strict check operates
    # on the most-recent three.
    w = ADXTrendWindow()
    for v in (10.0, 30.0, 25.0, 20.0):
        w.push(v)
    # Window now holds [30, 25, 20] \u2014 strictly decreasing.
    assert w.is_strictly_decreasing() is True


def test_adx_trend_window_increasing_returns_false():
    w = ADXTrendWindow()
    for v in (20.0, 25.0, 30.0):
        w.push(v)
    assert w.is_strictly_decreasing() is False
