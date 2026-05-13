"""v8.3.30 -- tests for tools/compare_atr.py.

Network-touching paths (R2 download) are stubbed; the test
exercises the pure-Python aggregation + ATR computation +
verdict logic against synthetic ticks and bars.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone

import pytest

# Stub boto3 so compare_atr imports cleanly in sandboxes without it.
if "boto3" not in sys.modules:
    sys.modules["boto3"] = types.ModuleType("boto3")

from tools.compare_atr import (
    aggregate_1m_to_5m,
    aggregate_ticks_to_5m,
    compute_atr,
    verdict_for,
)


def _utc(h, m, s=0):
    return datetime(2026, 5, 12, h + 4, m, s, tzinfo=timezone.utc)  # ET offset for EDT


class TestAggregateTicksTo5m:

    def test_groups_into_5m_windows(self):
        # 5 ticks at 09:30-09:34 ET form one bar ending at bucket 574
        ticks = [
            {"ts": _utc(9, 30).isoformat(), "price": 100.0},
            {"ts": _utc(9, 31).isoformat(), "price": 101.0},
            {"ts": _utc(9, 32).isoformat(), "price": 99.0},
            {"ts": _utc(9, 33).isoformat(), "price": 100.5},
            {"ts": _utc(9, 34).isoformat(), "price": 100.2},
        ]
        bars = aggregate_ticks_to_5m(ticks)
        assert len(bars) == 1
        bucket, hi, lo, close = bars[0]
        assert bucket == 9 * 60 + 34  # 574
        assert hi == 101.0
        assert lo == 99.0
        assert close == 100.2  # last tick of the window

    def test_close_is_last_tick_in_window(self):
        ticks = [
            {"ts": _utc(9, 31).isoformat(), "price": 100.0},
            {"ts": _utc(9, 34, 30).isoformat(), "price": 99.5},
            {"ts": _utc(9, 34, 59).isoformat(), "price": 100.7},  # last in window
        ]
        bars = aggregate_ticks_to_5m(ticks)
        assert len(bars) == 1
        assert bars[0][3] == 100.7

    def test_two_windows(self):
        ticks = [
            {"ts": _utc(9, 30).isoformat(), "price": 100.0},
            {"ts": _utc(9, 34, 59).isoformat(), "price": 102.0},
            {"ts": _utc(9, 35).isoformat(), "price": 102.5},
            {"ts": _utc(9, 39).isoformat(), "price": 101.0},
        ]
        bars = aggregate_ticks_to_5m(ticks)
        assert len(bars) == 2
        # First window 09:30-09:34
        assert bars[0][0] == 9 * 60 + 34
        # Second window 09:35-09:39
        assert bars[1][0] == 9 * 60 + 39

    def test_handles_missing_fields(self):
        ticks = [
            {"ts": _utc(9, 30).isoformat(), "price": 100.0},
            {"price": 99.0},                                # missing ts
            {"ts": _utc(9, 31).isoformat()},                # missing price
            {"ts": "garbage"},
        ]
        bars = aggregate_ticks_to_5m(ticks)
        assert len(bars) == 1
        assert bars[0][3] == 100.0


class TestAggregate1mTo5m:

    def test_groups_consistent_with_ticks(self):
        bars_1m = [
            {"ts": _utc(9, 30).isoformat(), "high": 101, "low": 99,  "close": 100.0},
            {"ts": _utc(9, 31).isoformat(), "high": 100, "low": 98,  "close": 99.0},
            {"ts": _utc(9, 32).isoformat(), "high": 100, "low": 99,  "close": 99.5},
            {"ts": _utc(9, 33).isoformat(), "high": 101, "low": 100, "close": 100.5},
            {"ts": _utc(9, 34).isoformat(), "high": 102, "low": 100, "close": 100.2},
        ]
        bars = aggregate_1m_to_5m(bars_1m)
        assert len(bars) == 1
        bucket, hi, lo, close = bars[0]
        assert bucket == 9 * 60 + 34
        assert hi == 102
        assert lo == 98
        assert close == 100.2

    def test_close_uses_last_minute_in_window(self):
        bars_1m = [
            {"ts": _utc(9, 30).isoformat(), "high": 100, "low": 99, "close": 99.5},
            {"ts": _utc(9, 34).isoformat(), "high": 101, "low": 99, "close": 100.7},
        ]
        out = aggregate_1m_to_5m(bars_1m)
        assert out[0][3] == 100.7


class TestComputeATR:

    def _flat_bars(self, n: int, start_bucket: int = 9*60+34):
        # Generate n consecutive 5m bars, each with hi=101 lo=99 close=100
        # (TR = 2.0 between adjacent bars; ATR(14) = 2.0)
        return [(start_bucket + 5 * i, 101.0, 99.0, 100.0) for i in range(n)]

    def test_insufficient_bars_returns_none(self):
        bars = self._flat_bars(5)
        # Anchor 100 buckets past the last bar -- all 5 bars eligible
        last_bucket = bars[-1][0]
        assert compute_atr(bars, anchor_bucket=last_bucket + 100, lookback=14) is None

    def test_simple_atr(self):
        bars = self._flat_bars(20)
        last_bucket = bars[-1][0]
        atr = compute_atr(bars, anchor_bucket=last_bucket, lookback=14)
        # All TRs are 2.0 (high-low), so ATR(14) ≈ 2.0
        assert atr is not None
        assert abs(atr - 2.0) < 0.01

    def test_anchor_filters_late_bars(self):
        bars = self._flat_bars(20)
        first_bucket = bars[0][0]
        # Anchor 2 bars in -- only 2 bars eligible, not enough for ATR(14)
        atr_late = compute_atr(bars, anchor_bucket=first_bucket + 5, lookback=14)
        assert atr_late is None


class TestVerdict:

    def test_no_data(self):
        v = verdict_for([])
        assert v["verdict"] == "no_data"
        assert v["n_compared"] == 0

    def test_meaningful_tighter(self):
        rows = [{"ratio_tick_over_1m": 0.6}, {"ratio_tick_over_1m": 0.7},
                {"ratio_tick_over_1m": 0.65}, {"ratio_tick_over_1m": 0.55}]
        v = verdict_for(rows)
        assert v["verdict"] == "tick_atr_meaningfully_tighter"
        assert v["n_compared"] == 4

    def test_no_difference(self):
        rows = [{"ratio_tick_over_1m": 0.98}, {"ratio_tick_over_1m": 1.01},
                {"ratio_tick_over_1m": 1.0}, {"ratio_tick_over_1m": 0.99}]
        v = verdict_for(rows)
        assert v["verdict"] == "no_meaningful_difference"

    def test_modestly_tighter(self):
        rows = [{"ratio_tick_over_1m": 0.85}, {"ratio_tick_over_1m": 0.90},
                {"ratio_tick_over_1m": 0.92}]
        v = verdict_for(rows)
        assert v["verdict"] == "tick_atr_modestly_tighter"

    def test_skips_missing_ratios(self):
        rows = [
            {"ratio_tick_over_1m": None},
            {"ratio_tick_over_1m": 0.5},
            {},
        ]
        v = verdict_for(rows)
        assert v["n_compared"] == 1


class TestEndToEndSynthetic:
    """The whole pipeline on a synthetic case: ticks have intra-minute
    volatility that 1m bars by definition cannot capture, so ATR-from-
    ticks should be greater-or-equal to ATR-from-1m-OHLC for the same
    underlying movement.

    NOTE the production hypothesis is the OPPOSITE direction (tick ATR
    tighter than 1m ATR) because production's 1m bars are LIVE STREAMING
    aggregates with their own quirks while our 1m archive is Alpaca SIP
    summaries. The hypothesis is empirical, not algebraic; this test
    only checks that our aggregation/computation is internally
    consistent on synthetic data where ticks ARE a superset of 1m OHLC.
    """

    def test_zero_volatility_zero_atr(self):
        # All ticks at the same price -> zero intra-bar volatility
        ticks = []
        bars_1m = []
        for m in range(35):  # 09:30 to 10:05 ET
            ts = _utc(9, 30 + m if m < 30 else 30 - 30 + m,
                      0).isoformat() if m < 30 else _utc(10, m - 30, 0).isoformat()
            ticks.append({"ts": ts, "price": 100.0})
            bars_1m.append({
                "ts": ts, "high": 100.0, "low": 100.0, "close": 100.0,
            })
        tick_bars = aggregate_ticks_to_5m(ticks)
        bar_bars = aggregate_1m_to_5m(bars_1m)
        atr_t = compute_atr(tick_bars, anchor_bucket=10*60-1, lookback=5)
        atr_b = compute_atr(bar_bars, anchor_bucket=10*60-1, lookback=5)
        assert atr_t == 0.0 or atr_t is None
        assert atr_b == 0.0 or atr_b is None
