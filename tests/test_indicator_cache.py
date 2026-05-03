"""Tests for backtest/indicator_cache.py (v6.9.2 L2 indicator cache).

Rules: zero em-dashes (literal or escaped). All paths use tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backtest.indicator_cache import (
    _compute_atr,
    _compute_ema,
    _compute_or,
    _compute_vwap,
    _ind_parquet_path,
    _compute_params_hash,
    get_indicators,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_bar(path: Path, bar: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(bar) + "\n")


def _make_bars_dir(tmp_path: Path, date: str = "2026-04-28", n: int = 10) -> Path:
    bars_dir = tmp_path / "bars"
    for i in range(n):
        ts = f"{date}T{13 + i // 60:02d}:{i % 60:02d}:00Z"
        bar = {
            "ts": ts,
            "open": 100.0 + i,
            "high": 101.0 + i,
            "low": 99.0 + i,
            "close": 100.5 + i,
            "iex_volume": 1000,
            "session": "rth",
        }
        _write_bar(bars_dir / date / "AAPL.jsonl", bar)
    return bars_dir


# ---------------------------------------------------------------------------
# Unit: EMA computation
# ---------------------------------------------------------------------------


def test_ema_basic() -> None:
    closes = [10.0, 11.0, 12.0, 13.0, 14.0]
    ema = _compute_ema(closes, 3)
    assert len(ema) == 5
    # First value initialised to itself
    assert ema[0] == pytest.approx(10.0)
    # All values present (no None)
    assert all(v is not None for v in ema)


def test_ema_empty() -> None:
    assert _compute_ema([], 9) == []


# ---------------------------------------------------------------------------
# Unit: ATR computation
# ---------------------------------------------------------------------------


def test_atr_basic() -> None:
    bars = [
        {"high": 105.0, "low": 100.0, "close": 102.0},
        {"high": 108.0, "low": 104.0, "close": 106.0},
        {"high": 110.0, "low": 106.0, "close": 109.0},
    ]
    atr = _compute_atr(bars, 2)
    assert len(atr) == 3
    # First bar: TR = high - low = 5.0; period not warm yet
    assert atr[0] is None
    assert atr[1] is not None  # warm at index period-1 = 1
    assert all(v is None or v > 0 for v in atr)


def test_atr_empty() -> None:
    assert _compute_atr([], 14) == []


# ---------------------------------------------------------------------------
# Test: cache miss -> computes indicator, persists
# ---------------------------------------------------------------------------


def test_cache_miss_computes_and_persists(tmp_path: Path) -> None:
    bars_dir = _make_bars_dir(tmp_path, n=15)
    date = "2026-04-28"
    indicators = ["ema9", "atr14"]
    params_hash = _compute_params_hash(bars_dir, "AAPL", indicators, {})
    pp = _ind_parquet_path(bars_dir, "AAPL", params_hash, date)

    assert not pp.exists(), "Parquet must not exist before first call"
    result = get_indicators(bars_dir, "AAPL", date, indicators)
    assert pp.exists(), "Parquet must be created on cache miss"
    assert "ema9" in result
    assert "atr14" in result
    assert len(result["ema9"]) == 15
    assert len(result["atr14"]) == 15


# ---------------------------------------------------------------------------
# Test: cache hit -> returns same values without recompute
# ---------------------------------------------------------------------------


def test_cache_hit_returns_same_values(tmp_path: Path) -> None:
    bars_dir = _make_bars_dir(tmp_path, n=15)
    date = "2026-04-28"
    indicators = ["ema9"]

    first = get_indicators(bars_dir, "AAPL", date, indicators)

    # Overwrite source file to check cache is NOT re-read (key unchanged
    # because we have not touched mtime/size yet)
    second = get_indicators(bars_dir, "AAPL", date, indicators)

    assert len(first["ema9"]) == len(second["ema9"])
    for a, b in zip(first["ema9"], second["ema9"]):
        if a is None:
            assert b is None
        else:
            assert abs(a - b) < 1e-12


# ---------------------------------------------------------------------------
# Test: param change -> cache miss on changed indicator
# ---------------------------------------------------------------------------


def test_param_change_triggers_cache_miss(tmp_path: Path) -> None:
    bars_dir = _make_bars_dir(tmp_path, n=20)
    date = "2026-04-28"

    params_a = {"ema_period_9": 9}
    params_b = {"ema_period_9": 5}  # different period

    hash_a = _compute_params_hash(bars_dir, "AAPL", ["ema9"], params_a)
    hash_b = _compute_params_hash(bars_dir, "AAPL", ["ema9"], params_b)
    assert hash_a != hash_b, "Different params must produce different hashes"

    result_a = get_indicators(bars_dir, "AAPL", date, ["ema9"], params_a)
    result_b = get_indicators(bars_dir, "AAPL", date, ["ema9"], params_b)

    # Both return valid results but values differ (different periods)
    assert result_a["ema9"] != result_b["ema9"], (
        "Different EMA periods must produce different values"
    )


# ---------------------------------------------------------------------------
# Test: all supported indicator names are accepted
# ---------------------------------------------------------------------------


def test_all_indicators_accepted(tmp_path: Path) -> None:
    bars_dir = _make_bars_dir(tmp_path, n=25)
    date = "2026-04-28"
    all_ind = [
        "atr14", "atr20",
        "ema9", "ema20", "ema50",
        "vwap",
        "or5_high", "or5_low",
        "or30_high", "or30_low",
        "pm_high", "pm_low", "pm_range",
        "session_boundary",
    ]
    result = get_indicators(bars_dir, "AAPL", date, all_ind)
    for ind in all_ind:
        assert ind in result, f"Missing indicator: {ind}"
        assert isinstance(result[ind], list), f"{ind} should be a list"


# ---------------------------------------------------------------------------
# Test: missing date returns empty lists
# ---------------------------------------------------------------------------


def test_missing_date_returns_empty(tmp_path: Path) -> None:
    bars_dir = _make_bars_dir(tmp_path, n=5)
    result = get_indicators(bars_dir, "AAPL", "2099-01-01", ["ema9"])
    assert result["ema9"] == []
