# tests/test_v6_0_5_yahoo_trailing_none.py
# v6.0.5 -- Yahoo trailing-None hotfix for Alarm F.
#
# Background: trade_genius.fetch_1min_bars surfaces Yahoo's raw 1m
# series, where the in-progress current minute lands as None on every
# series (highs/lows/closes/volumes). broker/positions.py:_run_sentinel
# was naively reading closes[-1] for last_1m_close and feeding the raw
# H/L/C arrays to atr_from_bars. Both blew up on float(None), the
# enclosing try silently set last_1m_close to None, and
# evaluate_sentinel skipped Alarm F's update_trail call every cycle.
# Net effect on prod (v6.0.4): bars_seen stuck at 0 or 1 forever, the
# Stage 1 BREAKEVEN arm never reached, and the protective stop never
# ratcheted off the entry-time hard stop.
#
# v6.0.5 walks closes_1m_raw backward to the most recent finite close,
# and builds aligned finite-only H/L/C lists for ATR. Both helpers in
# tests below mirror what _run_sentinel does so we lock in the
# resilience without spinning up the full sentinel harness.
#
# No em-dashes in this file (constraint for .py test files).
from __future__ import annotations


def _last_finite_close(closes_raw):
    """Mirror of broker/positions.py v6.0.5 last_1m_close walk-back."""
    out = None
    for i in range(len(closes_raw) - 1, -1, -1):
        c = closes_raw[i]
        if c is not None:
            try:
                out = float(c)
            except (TypeError, ValueError):
                out = None
            break
    return out


def _aligned_finite_hlc(highs_raw, lows_raw, closes_raw):
    """Mirror of broker/positions.py v6.0.5 ATR alignment block."""
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    n = min(len(highs_raw), len(lows_raw), len(closes_raw))
    for i in range(n):
        h = highs_raw[i]
        l_ = lows_raw[i]
        c = closes_raw[i]
        if h is None or l_ is None or c is None:
            continue
        try:
            highs.append(float(h))
            lows.append(float(l_))
            closes.append(float(c))
        except (TypeError, ValueError):
            continue
    return highs, lows, closes


# ---------------------------------------------------------------------
# last_1m_close walk-back
# ---------------------------------------------------------------------


def test_trailing_none_close_falls_back_to_prior_finite():
    closes = [100.0, 100.5, 101.0, None]
    assert _last_finite_close(closes) == 101.0


def test_multiple_trailing_nones_still_finds_finite():
    closes = [100.0, 100.5, 101.0, None, None, None]
    assert _last_finite_close(closes) == 101.0


def test_no_finite_closes_returns_none():
    assert _last_finite_close([None, None, None]) is None
    assert _last_finite_close([]) is None


def test_finite_last_close_passes_through_unchanged():
    closes = [100.0, 100.5, 101.0, 101.5]
    assert _last_finite_close(closes) == 101.5


def test_pre_v605_bug_repro_naive_indexing_raises():
    # Document the prior bug: float(closes[-1]) on a None tail blew up.
    closes = [100.0, None]
    raised = False
    try:
        float(closes[-1])
    except TypeError:
        raised = True
    assert raised, "regression: float(None) should raise TypeError"


# ---------------------------------------------------------------------
# Aligned finite H/L/C for ATR
# ---------------------------------------------------------------------


def test_aligned_drops_trailing_none_row():
    highs_raw = [100.5, 101.0, 102.0, None]
    lows_raw = [99.5, 100.0, 101.0, None]
    closes_raw = [100.0, 100.5, 101.5, None]
    h, l_, c = _aligned_finite_hlc(highs_raw, lows_raw, closes_raw)
    assert h == [100.5, 101.0, 102.0]
    assert l_ == [99.5, 100.0, 101.0]
    assert c == [100.0, 100.5, 101.5]


def test_aligned_drops_any_row_with_a_none():
    # If any one of (h,l,c) at index i is None, the whole row drops.
    highs_raw = [100.5, None, 102.0, 103.0]
    lows_raw = [99.5, 100.0, 101.0, None]
    closes_raw = [100.0, 100.5, None, 102.5]
    h, l_, c = _aligned_finite_hlc(highs_raw, lows_raw, closes_raw)
    # Only index 0 has all three finite.
    assert h == [100.5]
    assert l_ == [99.5]
    assert c == [100.0]


def test_aligned_three_lists_must_stay_same_length():
    highs_raw = [100.0, 101.0, 102.0]
    lows_raw = [99.0, 100.0, 101.0]
    closes_raw = [99.5, 100.5, 101.5]
    h, l_, c = _aligned_finite_hlc(highs_raw, lows_raw, closes_raw)
    assert len(h) == len(l_) == len(c) == 3


def test_aligned_handles_yahoo_short_series_gracefully():
    h, l_, c = _aligned_finite_hlc([], [], [])
    assert h == []
    assert l_ == []
    assert c == []


# ---------------------------------------------------------------------
# Wired check: feeding Yahoo-shaped trailing-None arrays through the
# real ATR helper produces a finite ATR (was None pre-v6.0.5).
# ---------------------------------------------------------------------


def test_atr_from_bars_with_yahoo_shaped_input():
    from engine.alarm_f_trail import atr_from_bars

    # 16 finite bars + 1 trailing None (Yahoo's forming-bar pattern).
    highs_raw = [100.0 + i * 0.1 for i in range(16)] + [None]
    lows_raw = [99.0 + i * 0.1 for i in range(16)] + [None]
    closes_raw = [99.5 + i * 0.1 for i in range(16)] + [None]
    h, l_, c = _aligned_finite_hlc(highs_raw, lows_raw, closes_raw)
    atr = atr_from_bars(h, l_, c, period=14)
    assert atr is not None
    assert atr > 0.0


def test_alarm_f_gate_passes_after_walk_back():
    # Demonstrates the end-to-end gate: trailing-None bars used to set
    # last_1m_close=None and trip the F gate; v6.0.5 keeps it finite.
    closes_raw = [100.0, 100.5, 101.0, None]
    last_1m_close = _last_finite_close(closes_raw)
    trail_state = object()  # any non-None
    entry_price = 99.0
    current_shares = 10
    f_gate = (
        trail_state is not None
        and entry_price is not None
        and last_1m_close is not None
        and current_shares > 0
    )
    assert f_gate is True
    assert last_1m_close == 101.0
