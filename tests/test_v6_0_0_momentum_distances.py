"""v6.0.0 \u2014 unit tests for the momentum distance-to-next-trigger helper.

`v5_10_6_snapshot._momentum_distances_per_ticker` derives a set of gap
metrics (ADX gap, DI long/short gap, DI cross, VWAP %, EMA9 %) from
the existing DI block and the local-weather block. The dashboard uses
these to render \"how close to firing\" text on the Momentum card.

Coverage:

  - Math correctness for a fully populated ticker.
  - Side-aware sign of `di_cross_gap`.
  - Null-safety: missing ADX / DI / weather inputs drop only that field.
  - The ADX 5m gap is always 20 \u2212 adx_5m (Phase 3 spec).
"""

from __future__ import annotations

import math
import os
import sys
import types

import pytest


_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


from v5_10_6_snapshot import _momentum_distances_per_ticker  # noqa: E402


def _stub_module(adx_map: dict | None, threshold: float | None = 25.0):
    def adx_fn(t):
        return (adx_map or {}).get(t)

    return types.SimpleNamespace(
        TIGER_V2_DI_THRESHOLD=threshold,
        v5_adx_1m_5m=adx_fn,
    )


# ---------------------------------------------------------------------------
# 1. Fully populated long-leaning ticker.
# ---------------------------------------------------------------------------


def test_momentum_distances_long_leaning_ticker():
    m = _stub_module({"AAPL": {"adx_1m": 18.0, "adx_5m": 22.5}})
    di_blk = {"AAPL": {
        "di_plus_1m": 28.4,
        "di_minus_1m": 11.0,
        "di_plus_5m": 27.0,
        "di_minus_5m": 12.0,
        "threshold": 25.0,
        "seed_bars": 30,
        "sufficient": True,
    }}
    wx_blk = {"AAPL": {
        "last_close_5m": 192.10,
        "ema9_5m": 191.50,
        "last": 192.20,
        "avwap": 191.00,
    }}
    out = _momentum_distances_per_ticker(m, ["AAPL"], di_blk, wx_blk)
    p = out["AAPL"]
    assert p["adx_1m"] == 18.0
    assert p["adx_5m"] == 22.5
    assert p["adx_5m_gap"] == pytest.approx(-2.5)  # already passing
    assert p["di_long_gap"] == pytest.approx(-3.4)  # 25 \u2212 28.4
    assert p["di_short_gap"] == pytest.approx(14.0)
    assert p["di_cross_gap"] == pytest.approx(17.4)  # long-leaning
    # vwap_gap_pct = (192.20 \u2212 191.00) / 191.00 * 100
    assert p["vwap_gap_pct"] == pytest.approx(0.6283, abs=1e-3)
    assert p["ema9_gap_pct"] == pytest.approx(0.3655, abs=1e-3)


# ---------------------------------------------------------------------------
# 2. Short-leaning ticker has negative di_cross_gap.
# ---------------------------------------------------------------------------


def test_momentum_distances_short_leaning_cross_gap_negative():
    m = _stub_module({"NFLX": {"adx_1m": 25.0, "adx_5m": 18.0}})
    di_blk = {"NFLX": {
        "di_plus_1m": 12.0,
        "di_minus_1m": 28.5,
        "di_plus_5m": None,
        "di_minus_5m": None,
        "threshold": 25.0,
        "seed_bars": 30,
        "sufficient": True,
    }}
    wx_blk = {"NFLX": {
        "last": 480.0,
        "avwap": 485.0,
        "ema9_5m": 484.0,
    }}
    out = _momentum_distances_per_ticker(m, ["NFLX"], di_blk, wx_blk)
    p = out["NFLX"]
    assert p["di_cross_gap"] == pytest.approx(-16.5)
    assert p["di_short_gap"] == pytest.approx(-3.5)  # already passing
    # ADX 5m gap = 20 \u2212 18 = +2 (still below trigger)
    assert p["adx_5m_gap"] == pytest.approx(2.0)
    # Below AVWAP and EMA9 \u2192 negative gaps.
    assert p["vwap_gap_pct"] < 0
    assert p["ema9_gap_pct"] < 0


# ---------------------------------------------------------------------------
# 3. Missing ADX feed \u2014 ADX fields go null but DI / VWAP still computed.
# ---------------------------------------------------------------------------


def test_momentum_distances_no_adx_function():
    m = types.SimpleNamespace(TIGER_V2_DI_THRESHOLD=25.0)  # no v5_adx_1m_5m
    di_blk = {"AAPL": {
        "di_plus_1m": 22.0, "di_minus_1m": 18.0,
        "di_plus_5m": None, "di_minus_5m": None,
        "threshold": 25.0, "seed_bars": 30, "sufficient": True,
    }}
    wx_blk = {"AAPL": {"last": 100.0, "avwap": 99.0, "ema9_5m": 99.5}}
    out = _momentum_distances_per_ticker(m, ["AAPL"], di_blk, wx_blk)
    p = out["AAPL"]
    assert p["adx_1m"] is None and p["adx_5m"] is None
    assert p["adx_5m_gap"] is None
    assert p["di_long_gap"] == pytest.approx(3.0)
    assert p["di_short_gap"] == pytest.approx(7.0)
    assert p["vwap_gap_pct"] == pytest.approx(1.0101, abs=1e-3)


# ---------------------------------------------------------------------------
# 4. Missing weather block (no last/AVWAP) \u2014 those gaps go null.
# ---------------------------------------------------------------------------


def test_momentum_distances_no_weather_block():
    m = _stub_module({"AAPL": {"adx_1m": 21.0, "adx_5m": 19.0}})
    di_blk = {"AAPL": {
        "di_plus_1m": 30.0, "di_minus_1m": 10.0,
        "di_plus_5m": None, "di_minus_5m": None,
        "threshold": 25.0, "seed_bars": 30, "sufficient": True,
    }}
    out = _momentum_distances_per_ticker(m, ["AAPL"], di_blk, {})
    p = out["AAPL"]
    assert p["vwap_gap_pct"] is None
    assert p["ema9_gap_pct"] is None
    assert p["adx_5m_gap"] == pytest.approx(1.0)
    assert p["di_cross_gap"] == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# 5. Missing threshold \u2014 di_long_gap / di_short_gap fall back to None.
# ---------------------------------------------------------------------------


def test_momentum_distances_no_threshold():
    m = _stub_module({"AAPL": {"adx_1m": 21.0, "adx_5m": 25.0}}, threshold=None)
    di_blk = {"AAPL": {
        "di_plus_1m": 22.0, "di_minus_1m": 11.0,
        "di_plus_5m": None, "di_minus_5m": None,
        "threshold": None, "seed_bars": 30, "sufficient": True,
    }}
    wx_blk = {"AAPL": {"last": 100.0, "avwap": 99.5, "ema9_5m": 99.8}}
    out = _momentum_distances_per_ticker(m, ["AAPL"], di_blk, wx_blk)
    p = out["AAPL"]
    assert p["di_long_gap"] is None
    assert p["di_short_gap"] is None
    # DI cross still computable from the two DI values alone.
    assert p["di_cross_gap"] == pytest.approx(11.0)


# ---------------------------------------------------------------------------
# 6. ADX threshold is always 20.0 (Phase 3 spec gate).
# ---------------------------------------------------------------------------


def test_momentum_distances_adx_threshold_constant():
    m = _stub_module({"AAPL": {"adx_1m": 0.0, "adx_5m": 0.0}})
    out = _momentum_distances_per_ticker(
        m, ["AAPL"],
        {"AAPL": {"di_plus_1m": None, "di_minus_1m": None,
                  "threshold": None, "seed_bars": 0, "sufficient": False}},
        {},
    )
    assert out["AAPL"]["adx_threshold"] == pytest.approx(20.0)
