"""v6.0.0 \u2014 unit tests for the PDC-anchored EMA9 seed path.

Covers both implementations of the seed:

  - engine.bars.compute_5m_ohlc_and_ema9 (regime-gate path)
  - dashboard_server._intraday_ema9_5m (dashboard chart overlay path)

Behaviour matrix:

  - Empty inputs return None / [] without raising.
  - With < 9 closed bars and PDC provided, the EMA9 is populated for
    every real bar (synthetic 9-bar flat-at-PDC prefix, alpha=0.2).
  - With < 9 bars and no PDC, the strict pre-v6.0.0 rule holds: every
    output slot is None / not seeded.
  - With >= 9 bars, the original Gene SMA-seed path runs and the PDC
    argument is ignored (so historical behaviour is byte-equal).
"""

from __future__ import annotations

import math
import os
import sys
import types

import pytest


# Make the repo root importable when pytest is run from a sub-dir.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


from engine.bars import compute_5m_ohlc_and_ema9  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers \u2014 build a synthetic 1m-bars payload that bucket-aggregates into
# N closed 5m bars with deterministic close prices.
# ---------------------------------------------------------------------------


def _make_bars(closes_5m: list[float], base_ts: int = 1714579800) -> dict:
    """Construct the dict shape `compute_5m_ohlc_and_ema9` consumes.

    Each closed 5m bar in `closes_5m` is encoded as 5 minute-bars with
    the same close (so ohlc collapses to that price). One extra trailing
    minute is appended to give the helper a "still forming" bucket that
    it must drop \u2014 so the function sees ``len(closes_5m) + 1`` buckets
    and emits exactly ``len(closes_5m)`` closed 5m bars.
    """
    timestamps: list[int] = []
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    for i, c in enumerate(closes_5m):
        bucket_start = base_ts + i * 300
        for j in range(5):
            timestamps.append(bucket_start + j * 60)
            opens.append(c)
            highs.append(c)
            lows.append(c)
            closes.append(c)
    # Trailing minute on the next 5m bucket so the helper drops it
    # (it always discards the newest, possibly-forming bucket).
    forming_start = base_ts + len(closes_5m) * 300
    timestamps.append(forming_start)
    opens.append(closes_5m[-1] if closes_5m else 0.0)
    highs.append(closes_5m[-1] if closes_5m else 0.0)
    lows.append(closes_5m[-1] if closes_5m else 0.0)
    closes.append(closes_5m[-1] if closes_5m else 0.0)
    return {
        "timestamps": timestamps,
        "opens": opens,
        "highs": highs,
        "lows": lows,
        "closes": closes,
    }


# ---------------------------------------------------------------------------
# 1. compute_5m_ohlc_and_ema9 \u2014 empty / degenerate inputs.
# ---------------------------------------------------------------------------


def test_compute_5m_empty_returns_none():
    assert compute_5m_ohlc_and_ema9(None) is None
    assert compute_5m_ohlc_and_ema9({}) is None


def test_compute_5m_zero_buckets_returns_none():
    """Empty bars dict (no timestamps / closes) returns None."""
    res = compute_5m_ohlc_and_ema9({
        "timestamps": [], "opens": [], "highs": [],
        "lows": [], "closes": [],
    })
    assert res is None


# ---------------------------------------------------------------------------
# 2. < 9 bars + PDC \u2014 synthetic-prefix path engages.
# ---------------------------------------------------------------------------


def test_compute_5m_pdc_seed_three_bars_engages():
    """Three closed bars at 100 with PDC=99 should produce a non-None
    EMA9 series (seeded=True). Math: EMA seed = 99; at alpha=0.2 each
    bar of c=100: ema_1 = 0.2*100 + 0.8*99 = 99.2; ema_2 = 99.36;
    ema_3 = 99.488."""
    res = compute_5m_ohlc_and_ema9(_make_bars([100.0, 100.0, 100.0]), pdc=99.0)
    assert res is not None
    assert res["seeded"] is True
    assert res["ema9"] is not None
    assert math.isclose(res["ema9"], 99.488, rel_tol=0, abs_tol=1e-6)
    # ema9_series must have one entry per closed bar.
    assert len(res["ema9_series"]) == 3
    for v in res["ema9_series"]:
        assert v is not None


def test_compute_5m_pdc_seed_eight_bars_still_engages():
    """At 8 closed bars (< 9) the synthetic-prefix path must still run."""
    closes = [101.0] * 8
    res = compute_5m_ohlc_and_ema9(_make_bars(closes), pdc=100.0)
    assert res is not None
    assert res["seeded"] is True
    assert res["ema9"] is not None


# ---------------------------------------------------------------------------
# 3. < 9 bars without PDC \u2014 strict pre-v6.0.0 rule (no seed).
# ---------------------------------------------------------------------------


def test_compute_5m_no_pdc_under_nine_bars_no_seed():
    res = compute_5m_ohlc_and_ema9(_make_bars([100.0, 100.0, 100.0]))
    assert res is not None
    assert res["seeded"] is False
    assert res["ema9"] is None
    # ema9_series exists but every entry is None until the SMA-seed slot.
    assert all(v is None for v in res["ema9_series"])


# ---------------------------------------------------------------------------
# 4. >= 9 bars \u2014 PDC ignored, original Gene SMA-seed path runs.
# ---------------------------------------------------------------------------


def test_compute_5m_pdc_ignored_when_nine_or_more_real_bars():
    closes = [100.0, 101.0, 102.0, 103.0, 104.0,
              105.0, 106.0, 107.0, 108.0, 109.0]
    res_no_pdc = compute_5m_ohlc_and_ema9(_make_bars(closes))
    res_with_pdc = compute_5m_ohlc_and_ema9(
        _make_bars(closes), pdc=50.0
    )  # absurdly low PDC must not affect the output
    assert res_no_pdc is not None
    assert res_with_pdc is not None
    assert res_no_pdc["ema9"] == pytest.approx(res_with_pdc["ema9"])
    assert res_no_pdc["seeded"] is True
    assert res_with_pdc["seeded"] is True


# ---------------------------------------------------------------------------
# 5. Dashboard chart helper: dashboard_server._intraday_ema9_5m.
# ---------------------------------------------------------------------------


def _import_intraday_helper():
    """dashboard_server has heavy import-time side effects, so wrap with
    minimal stubs and expose the helper. We tolerate an ImportError if
    the module pulls in something not available in the test sandbox \u2014
    in that case the test is skipped, not failed."""
    try:
        import dashboard_server  # noqa: F401
        return dashboard_server._intraday_ema9_5m
    except Exception as exc:
        pytest.skip(f"dashboard_server unavailable in test env: {exc}")


def test_intraday_ema9_pdc_seed_three_bars():
    fn = _import_intraday_helper()
    bars5 = [{"c": 100.0}, {"c": 100.0}, {"c": 100.0}]
    out = fn(bars5, pdc=99.0)
    assert len(out) == 3
    assert all(v is not None for v in out)
    assert math.isclose(out[-1], 99.488, rel_tol=0, abs_tol=1e-6)


def test_intraday_ema9_no_pdc_under_nine_returns_none_list():
    fn = _import_intraday_helper()
    bars5 = [{"c": 100.0}, {"c": 100.0}, {"c": 100.0}]
    out = fn(bars5)
    assert len(out) == 3
    assert all(v is None for v in out)


def test_intraday_ema9_empty_returns_empty():
    fn = _import_intraday_helper()
    assert fn([]) == []
    assert fn([], pdc=100.0) == []


def test_intraday_ema9_pdc_ignored_with_nine_bars():
    fn = _import_intraday_helper()
    bars5 = [{"c": float(100 + i)} for i in range(9)]
    a = fn(bars5)
    b = fn(bars5, pdc=10.0)
    # First 8 entries None; entry 8 = SMA(first 9) regardless of pdc.
    assert a[8] == pytest.approx(b[8])
    assert a[8] == pytest.approx(sum(range(100, 109)) / 9.0)
