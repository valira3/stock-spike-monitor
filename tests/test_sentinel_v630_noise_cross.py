"""v6.3.0 — Sentinel B noise-cross filter.

Forensics from the Apr 27 - May 1 weekly backtest (v620_week_backtest)
showed Sentinel B at -$278 / 6% wins vs Sentinel A at +$259 / 70% wins.
Sixteen of 17 B losers closed at adverse moves of -0.05% to -0.37% -
within typical 1m noise. The noise-cross filter gates the v6.1.0 2-bar
EMA-cross exit on a minimum adverse move of k x 1m ATR from entry.

These tests cover:
    1. LONG: adverse < k x ATR -> sit out (no exit)
    2. LONG: adverse >= k x ATR -> exit fires
    3. SHORT: adverse < k x ATR -> sit out
    4. SHORT: adverse >= k x ATR -> exit fires
    5. Bypass: any of entry/current/atr is None -> legacy fires
    6. Bypass: atr <= 0 -> legacy fires
    7. Counter does NOT reset when blocked (next-bar fires when adverse clears)
"""

from __future__ import annotations

import pytest

import engine.sentinel as sentinel_mod
from engine.sentinel import (
    SIDE_LONG,
    SIDE_SHORT,
    check_alarm_b,
    reset_ema_cross_pending,
)


@pytest.fixture(autouse=True)
def _enable_v610_and_reset():
    reset_ema_cross_pending()
    orig = sentinel_mod._V610_EMA_CONFIRM_ENABLED
    sentinel_mod._V610_EMA_CONFIRM_ENABLED = True
    try:
        yield
    finally:
        sentinel_mod._V610_EMA_CONFIRM_ENABLED = orig
        reset_ema_cross_pending()


def _two_long_crosses(position_id, **noise_kwargs):
    """Drive two consecutive LONG cross bars through check_alarm_b.

    Returns the result of the second call (the one that may fire).
    Uses last_5m_close=99.0 < last_5m_ema9=100.0 (LONG cross condition).
    """
    common = dict(
        side=SIDE_LONG,
        last_5m_close=99.0,
        last_5m_ema9=100.0,
        position_id=position_id,
    )
    common.update(noise_kwargs)
    check_alarm_b(**common)
    return check_alarm_b(**common)


def _two_short_crosses(position_id, **noise_kwargs):
    """Drive two consecutive SHORT cross bars (close > ema9).

    Returns the result of the second call.
    """
    common = dict(
        side=SIDE_SHORT,
        last_5m_close=101.0,
        last_5m_ema9=100.0,
        position_id=position_id,
    )
    common.update(noise_kwargs)
    check_alarm_b(**common)
    return check_alarm_b(**common)


# ---------------------------------------------------------------------------
# 1. LONG: adverse < k x ATR -> sit out
# ---------------------------------------------------------------------------

def test_long_adverse_below_threshold_sits_out():
    # entry=100, current=99.99 -> adverse=0.01. k=0.10, atr=1.0 -> threshold=0.10.
    # 0.01 < 0.10 so the filter must block the exit even on the second cross bar.
    fired = _two_long_crosses(
        "pos_long_below",
        entry_price=100.0,
        current_price=99.99,
        last_1m_atr=1.0,
    )
    assert fired == [], "Adverse below k x ATR must sit out"


# ---------------------------------------------------------------------------
# 2. LONG: adverse >= k x ATR -> exit fires
# ---------------------------------------------------------------------------

def test_long_adverse_above_threshold_fires():
    # entry=100, current=99.5 -> adverse=0.50. k=0.10, atr=1.0 -> threshold=0.10.
    # 0.50 >= 0.10 so on the second consecutive cross bar the exit must fire.
    fired = _two_long_crosses(
        "pos_long_above",
        entry_price=100.0,
        current_price=99.5,
        last_1m_atr=1.0,
    )
    assert len(fired) == 1, "Adverse above k x ATR must fire"
    assert fired[0].alarm == "B"


# ---------------------------------------------------------------------------
# 3. SHORT: adverse < k x ATR -> sit out
# ---------------------------------------------------------------------------

def test_short_adverse_below_threshold_sits_out():
    # SHORT entry=100, current=100.01 -> adverse=(current-entry)=0.01. Below 0.10.
    fired = _two_short_crosses(
        "pos_short_below",
        entry_price=100.0,
        current_price=100.01,
        last_1m_atr=1.0,
    )
    assert fired == [], "SHORT adverse below k x ATR must sit out"


# ---------------------------------------------------------------------------
# 4. SHORT: adverse >= k x ATR -> exit fires
# ---------------------------------------------------------------------------

def test_short_adverse_above_threshold_fires():
    # SHORT entry=100, current=100.5 -> adverse=0.50 >= 0.10.
    fired = _two_short_crosses(
        "pos_short_above",
        entry_price=100.0,
        current_price=100.5,
        last_1m_atr=1.0,
    )
    assert len(fired) == 1, "SHORT adverse above k x ATR must fire"
    assert fired[0].alarm == "B"


# ---------------------------------------------------------------------------
# 5. Bypass: any of the three inputs is None -> legacy path fires
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kwargs",
    [
        dict(entry_price=None, current_price=99.99, last_1m_atr=1.0),
        dict(entry_price=100.0, current_price=None, last_1m_atr=1.0),
        dict(entry_price=100.0, current_price=99.99, last_1m_atr=None),
        # No noise kwargs at all: legacy v6.1.0 path.
        dict(),
    ],
)
def test_none_inputs_bypass_filter(kwargs):
    # When any input is None the filter is skipped and the legacy v6.1.0
    # 2-bar path fires on the second consecutive cross bar.
    fired = _two_long_crosses("pos_bypass_" + str(id(kwargs)), **kwargs)
    assert len(fired) == 1, "Filter must be bypassed when any input is None"


# ---------------------------------------------------------------------------
# 6. Bypass: ATR <= 0 -> filter bypassed (legacy fires)
# ---------------------------------------------------------------------------

def test_zero_atr_bypasses_filter():
    # ATR <= 0 is treated as missing data -> filter bypassed -> legacy fires.
    fired = _two_long_crosses(
        "pos_zero_atr",
        entry_price=100.0,
        current_price=99.99,
        last_1m_atr=0.0,
    )
    assert len(fired) == 1, "ATR <= 0 must bypass the filter"


# ---------------------------------------------------------------------------
# 7. Counter does NOT reset when blocked - subsequent bar fires when adverse clears
# ---------------------------------------------------------------------------

def test_counter_no_reset_when_blocked():
    pid = "pos_no_reset"
    common = dict(
        side=SIDE_LONG,
        last_5m_close=99.0,
        last_5m_ema9=100.0,
        position_id=pid,
        last_1m_atr=1.0,
        entry_price=100.0,
    )

    # Bar 1: cross True, count=1 -> no fire (below 2).
    fired1 = check_alarm_b(current_price=99.99, **common)
    assert fired1 == []
    assert sentinel_mod._ema_cross_pending[pid] == 1

    # Bar 2: cross True, count=2, but adverse=0.01 < 0.10 -> filter blocks.
    # Critical: counter must remain at 2 (NOT reset).
    fired2 = check_alarm_b(current_price=99.99, **common)
    assert fired2 == []
    assert sentinel_mod._ema_cross_pending[pid] == 2, (
        "Counter must NOT reset when filter blocks - the position waits for "
        "either price to confirm or the cross to flip naturally"
    )

    # Bar 3: cross still True, count=3, now adverse=0.5 >= 0.10 -> fires.
    fired3 = check_alarm_b(current_price=99.5, **common)
    assert len(fired3) == 1
    assert fired3[0].alarm == "B"
