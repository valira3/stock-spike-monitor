# tests/test_v610_atr_or_break.py
# v6.1.0 -- ATR-normalized OR-break entry gate (Improvement #3).
#
# Covers:
#   1. ATR threshold replaces fixed-cents path
#   2. Low-vol ticker: ATR threshold tighter than old fixed-cents
#   3. High-vol ticker: ATR threshold larger than old fixed-cents
#   4. Late-OR window fires when standard OR never triggered
#   5. Late-OR disabled flag prevents late break
#   6. Feature-flag disabled falls back to fixed-cents (_tiger_two_bar_*)
#   7. Short break is symmetric to long break
#
# Design: the helpers under test are pure or near-pure functions.
# We exercise them via the trade_genius module-level globals rather
# than going through the full broker.orders gate stack (which requires
# live Alpaca / Yahoo). Tests patch only the minimal globals they need.
#
# No em-dashes in this file.
from __future__ import annotations

import os
import sys
import types
import importlib
from zoneinfo import ZoneInfo
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Bootstrap: set SSM_SMOKE_TEST so trade_genius skips Telegram / Alpaca init.
# ---------------------------------------------------------------------------
os.environ.setdefault("SSM_SMOKE_TEST", "1")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ---------------------------------------------------------------------------
# Helpers: build synthetic pre-market bars recognised by pre_market_range_atr
# ---------------------------------------------------------------------------

ET = ZoneInfo("America/New_York")

# 08:30 ET on an arbitrary trading day (2026-05-01)
_BASE_PM_EPOCH = int(datetime(2026, 5, 1, 8, 30, 0, tzinfo=ET).timestamp())

_MINUTE = 60  # seconds


def _pm_bar(minute_offset: int, high: float, low: float, close: float) -> dict:
    """Build a single synthetic pre-market 1m bar at 08:30 + minute_offset."""
    ts = _BASE_PM_EPOCH + minute_offset * _MINUTE
    return {
        "ts": ts,
        "open": close,
        "high": high,
        "low": low,
        "close": close,
    }


def _make_pm_bars(n: int, high_price: float, low_price: float) -> list[dict]:
    """Return ``n`` synthetic pre-market bars with constant high/low."""
    bars = []
    for i in range(n):
        mid = (high_price + low_price) / 2.0
        bars.append(_pm_bar(i, high=high_price, low=low_price, close=mid))
    return bars


# ---------------------------------------------------------------------------
# Direct test of the indicators.pre_market_range_atr helper
# ---------------------------------------------------------------------------

from indicators import pre_market_range_atr


def test_pre_market_range_atr_low_vol():
    bars = _make_pm_bars(16, high_price=100.10, low_price=99.90)
    val = pre_market_range_atr(bars, window_minutes=15, period=5)
    # TR per bar = high - low = 0.20 (no gap between synthetic bars).
    # ATR(5) = 0.20
    assert val is not None
    assert abs(val - 0.20) < 0.01


def test_pre_market_range_atr_high_vol():
    bars = _make_pm_bars(16, high_price=102.50, low_price=100.00)
    val = pre_market_range_atr(bars, window_minutes=15, period=5)
    assert val is not None
    assert val > 1.50  # high-vol: TR ~2.50


def test_pre_market_range_atr_insufficient_bars_returns_none():
    bars = _make_pm_bars(3, high_price=100.10, low_price=99.90)
    val = pre_market_range_atr(bars, window_minutes=15, period=5)
    assert val is None


def test_pre_market_range_atr_empty_returns_none():
    assert pre_market_range_atr([]) is None
    assert pre_market_range_atr(None) is None


# ---------------------------------------------------------------------------
# Import trade_genius helpers we will test directly.
# We monkeypatch fetch_1min_bars so ATR computation reads from our fixture.
# ---------------------------------------------------------------------------

import trade_genius as tg_mod
import pytest


@pytest.fixture
def atr_or_break_enabled():
    """Force-enable the ATR OR-break flag for tests that exercise the ATR path.

    The module-level default is False (v6.1.0 ships the gate dormant
    until k is calibrated from shadow data). Tests that specifically
    cover the ATR-on behaviour use this fixture to flip it on locally.
    """
    orig = tg_mod._V610_ATR_OR_BREAK_ENABLED
    tg_mod._V610_ATR_OR_BREAK_ENABLED = True
    try:
        yield
    finally:
        tg_mod._V610_ATR_OR_BREAK_ENABLED = orig


def _set_pm_atr(ticker: str, val: float | None) -> None:
    """Directly inject a pre-market ATR value into the cache."""
    tg_mod._v610_pm_atr[ticker] = val


def _clear_state(ticker: str) -> None:
    """Clear per-ticker v6.1.0 state between tests."""
    tg_mod._v610_pm_atr.pop(ticker, None)
    tg_mod._v610_or_break_fired.pop(ticker, None)
    tg_mod._v610_late_or_high.pop(ticker, None)
    tg_mod._v610_late_or_low.pop(ticker, None)


# ---------------------------------------------------------------------------
# Test 1: ATR threshold replaces fixed-cents (known-ATR case)
# ---------------------------------------------------------------------------

def test_atr_threshold_replaces_fixed_cents(atr_or_break_enabled):
    """When ATR is known and flag is on, break requires closes > or_h + k*ATR."""
    ticker = "AAPL"
    _clear_state(ticker)
    or_h = 200.00
    k = tg_mod.V610_OR_BREAK_K           # 0.6
    atr = 0.50                            # synthetic ATR
    threshold = or_h + k * atr           # 200.30

    # Inject the pre-market ATR directly
    _set_pm_atr(ticker, atr)

    # Both closes just above plain OR high but below ATR threshold: must NOT fire
    closes_below_thresh = [or_h + 0.01, or_h + 0.01]
    result_below = tg_mod._v610_or_break_long(closes_below_thresh, or_h, ticker)
    assert not result_below, "Should NOT break when below ATR threshold"

    # Both closes above ATR threshold: MUST fire
    closes_above_thresh = [threshold + 0.01, threshold + 0.01]
    result_above = tg_mod._v610_or_break_long(closes_above_thresh, or_h, ticker)
    assert result_above, "Should break when both closes above ATR threshold"

    _clear_state(ticker)


# ---------------------------------------------------------------------------
# Test 2: Low-vol ATR threshold is tighter than old fixed-cents
# ---------------------------------------------------------------------------

def test_low_vol_atr_smaller_than_fixed(atr_or_break_enabled):
    """Low-vol stock: ATR(5) < typical fixed-cent gap; threshold is tighter."""
    ticker = "LVL"
    _clear_state(ticker)
    or_h = 50.00
    low_vol_atr = 0.05                    # very narrow intraday range
    k = tg_mod.V610_OR_BREAK_K
    atr_threshold = or_h + k * low_vol_atr   # 50.03

    # Legacy fixed-cents historically often used ~0.10-0.30 cents above OR.
    # Simulate a "fixed" 0.15-cent break: closes just above plain OR but
    # inside the ATR-based band.
    fixed_break_closes = [or_h + 0.10, or_h + 0.10]

    # ATR gate with low vol: threshold = 50.03 => these closes pass!
    _set_pm_atr(ticker, low_vol_atr)
    result = tg_mod._v610_or_break_long(fixed_break_closes, or_h, ticker)
    assert result, "Low-vol ATR threshold is tighter => fires on small break"

    _clear_state(ticker)


# ---------------------------------------------------------------------------
# Test 3: High-vol ATR threshold is larger than old fixed-cents (filters chop)
# ---------------------------------------------------------------------------

def test_high_vol_atr_larger_than_fixed(atr_or_break_enabled):
    """High-vol ticker (TSLA-like): ATR(5) is large; filters shallow breaks."""
    ticker = "TSLA"
    _clear_state(ticker)
    or_h = 390.00
    high_vol_atr = 2.50                   # TSLA-class intraday ATR
    k = tg_mod.V610_OR_BREAK_K
    atr_threshold = or_h + k * high_vol_atr   # 391.50

    # Small break that would have fired with fixed-cents: NOT ATR-confirmed
    shallow_closes = [or_h + 0.30, or_h + 0.30]
    _set_pm_atr(ticker, high_vol_atr)
    result_shallow = tg_mod._v610_or_break_long(shallow_closes, or_h, ticker)
    assert not result_shallow, "High-vol: shallow break should NOT fire"

    # Proper break above ATR threshold: fires
    deep_closes = [atr_threshold + 0.10, atr_threshold + 0.10]
    result_deep = tg_mod._v610_or_break_long(deep_closes, or_h, ticker)
    assert result_deep, "High-vol: deep break should fire"

    _clear_state(ticker)


# ---------------------------------------------------------------------------
# Test 4: Late-OR window fires when standard OR never triggered
# ---------------------------------------------------------------------------

def test_late_or_window_fires(atr_or_break_enabled):
    """No break in 9:30-10:30, but closes above late-OR high in 11:00-12:00."""
    ticker = "META"
    _clear_state(ticker)

    # Inject a late-OR range directly (simulating the 11:00-11:30 accumulation)
    late_h = 510.00
    late_l = 505.00
    tg_mod._v610_late_or_high[ticker] = late_h
    tg_mod._v610_late_or_low[ticker]  = late_l

    # Inject a small ATR so threshold = late_h + 0.6 * 0.10 = 510.06
    _set_pm_atr(ticker, 0.10)

    # No standard break fired yet
    assert not tg_mod._v610_or_break_fired.get(ticker)

    # Closes above ATR-adjusted late-OR high
    closes_break = [late_h + 0.5 * tg_mod.V610_OR_BREAK_K, late_h + 0.5 * tg_mod.V610_OR_BREAK_K]
    # Force closes to be well above threshold
    closes_above = [late_h + 1.0, late_h + 1.0]
    result = tg_mod._v610_late_or_break_long(closes_above, ticker)
    assert result, "Late-OR LONG break should fire when closes above late-OR high"

    _clear_state(ticker)


# ---------------------------------------------------------------------------
# Test 5: Late-OR disabled flag prevents late break
# ---------------------------------------------------------------------------

def test_late_or_disabled():
    """V610_LATE_OR_ENABLED=False: late break does NOT fire."""
    ticker = "AVGO"
    _clear_state(ticker)

    orig_flag = tg_mod.V610_LATE_OR_ENABLED
    tg_mod.V610_LATE_OR_ENABLED = False
    try:
        tg_mod._v610_late_or_high[ticker] = 800.00
        tg_mod._v610_late_or_low[ticker]  = 795.00
        _set_pm_atr(ticker, 0.50)

        closes_above = [801.0, 801.0]
        result = tg_mod._v610_late_or_break_long(closes_above, ticker)
        assert not result, "Late-OR must not fire when V610_LATE_OR_ENABLED=False"
    finally:
        tg_mod.V610_LATE_OR_ENABLED = orig_flag
    _clear_state(ticker)


# ---------------------------------------------------------------------------
# Test 6: Feature flag disabled falls back to fixed-cents path
# ---------------------------------------------------------------------------

def test_disabled_flag_falls_back():
    """_V610_ATR_OR_BREAK_ENABLED=False: _tiger_two_bar_* path is used."""
    ticker = "ORCL"
    _clear_state(ticker)

    orig_flag = tg_mod._V610_ATR_OR_BREAK_ENABLED
    tg_mod._V610_ATR_OR_BREAK_ENABLED = False
    try:
        or_h = 130.00
        # Inject ATR so we can confirm it is NOT used
        _set_pm_atr(ticker, 5.00)  # would give threshold = 133.00 if enabled

        # One tick above plain OR high in both closes: legacy path fires
        closes_just_above = [or_h + 0.01, or_h + 0.01]
        result = tg_mod._v610_or_break_long(closes_just_above, or_h, ticker)
        assert result, "Legacy path: closes above plain OR high should fire"

        # Same for short
        or_l = 130.00
        closes_just_below = [or_l - 0.01, or_l - 0.01]
        result_short = tg_mod._v610_or_break_short(closes_just_below, or_l, ticker)
        assert result_short, "Legacy short path: closes below plain OR low should fire"

        # Close *below* ATR threshold but *above* plain OR high should NOT
        # fire in legacy mode (it is above the OR, so that's fine -- the
        # legacy check is plain `> or_h`; let's verify with close AT or_h)
        closes_at = [or_h, or_h]  # not strictly above -> should not fire
        result_at = tg_mod._v610_or_break_long(closes_at, or_h, ticker)
        assert not result_at, "Legacy path: close exactly at OR high should NOT fire"
    finally:
        tg_mod._V610_ATR_OR_BREAK_ENABLED = orig_flag
    _clear_state(ticker)


# ---------------------------------------------------------------------------
# Test 7: Short break is symmetric to long break
# ---------------------------------------------------------------------------

def test_short_break_symmetric(atr_or_break_enabled):
    """Break-below uses the same k*ATR distance as break-above."""
    ticker = "AMZN"
    _clear_state(ticker)
    or_l = 180.00
    atr = 0.80
    k = tg_mod.V610_OR_BREAK_K
    threshold = or_l - k * atr   # 179.52

    _set_pm_atr(ticker, atr)

    # Closes just below plain OR low but above ATR threshold: must NOT fire
    closes_shallow = [or_l - 0.10, or_l - 0.10]
    result_shallow = tg_mod._v610_or_break_short(closes_shallow, or_l, ticker)
    assert not result_shallow, "Shallow short break should NOT fire"

    # Closes below ATR threshold: MUST fire
    closes_deep = [threshold - 0.01, threshold - 0.01]
    result_deep = tg_mod._v610_or_break_short(closes_deep, or_l, ticker)
    assert result_deep, "Deep short break should fire"

    # Verify the distance is exactly symmetric with LONG
    or_h = 200.00
    atr_long = atr
    _v610_pm_atr_bak = tg_mod._v610_pm_atr.get(ticker)
    _set_pm_atr(ticker, atr_long)
    long_threshold = or_h + k * atr_long
    short_threshold = or_l - k * atr
    long_offset = long_threshold - or_h
    short_offset = or_l - short_threshold
    assert abs(long_offset - short_offset) < 1e-9, "Long and short offsets must be equal"

    _clear_state(ticker)
