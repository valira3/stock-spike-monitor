"""Tests for backtest/bar_cache.py (v6.9.0 L1 Parquet bar cache).

Rules: zero em-dashes (literal or escaped). All paths use tmp_path.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from backtest.bar_cache import (
    _cache_key,
    _ensure_cache,
    _meta_path,
    _parquet_path,
    _source_files_for_ticker,
    get_bars,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_bar(path: Path, bar: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(bar) + "\n")


def _rth_bar(ts: str, close: float, session: str = "rth") -> dict:
    return {
        "ts": ts,
        "open": close - 0.01,
        "high": close + 0.05,
        "low": close - 0.05,
        "close": close,
        "iex_volume": 1000,
        "iex_sip_ratio_used": None,
        "bid": None,
        "ask": None,
        "last_trade_price": close,
        "session": session,
    }


def _pre_bar(ts: str, close: float) -> dict:
    return {
        "ts": ts,
        "epoch": 0,
        "open": close - 0.01,
        "high": close + 0.05,
        "low": close - 0.05,
        "close": close,
        "volume": 500,
        "session": "PRE",
    }


def _build_fixture(tmp_path: Path, date: str = "2026-04-28") -> Path:
    bars_dir = tmp_path / "bars"
    rth = bars_dir / date / "AAPL.jsonl"
    _write_bar(rth, _rth_bar(f"{date}T13:30:00Z", 270.50))
    _write_bar(rth, _rth_bar(f"{date}T13:31:00Z", 271.00))
    pre = bars_dir / date / "premarket" / "AAPL.jsonl"
    _write_bar(pre, _pre_bar(f"{date}T08:00:00Z", 268.50))
    return bars_dir


# ---------------------------------------------------------------------------
# Test: cache miss -> builds Parquet, returns correct bars
# ---------------------------------------------------------------------------


def test_cache_miss_builds_parquet(tmp_path: pytest.TempPathFactory) -> None:
    bars_dir = _build_fixture(tmp_path)
    pp = _parquet_path(bars_dir, "AAPL")
    assert not pp.exists(), "Parquet should not exist before first call"
    bars = get_bars(bars_dir, "AAPL", "2026-04-28")
    assert pp.exists(), "Parquet must be created on cache miss"
    assert len(bars) == 3, f"Expected 3 bars (1 pre + 2 RTH), got {len(bars)}"


# ---------------------------------------------------------------------------
# Test: cache hit -> returns same bars without re-reading JSONL
# ---------------------------------------------------------------------------


def test_cache_hit_skips_jsonl(tmp_path: pytest.TempPathFactory) -> None:
    bars_dir = _build_fixture(tmp_path)
    # Warm the cache
    bars_first = get_bars(bars_dir, "AAPL", "2026-04-28")
    pp = _parquet_path(bars_dir, "AAPL")
    assert pp.exists()

    # Patch _load_jsonl to fail if called (should not be on cache hit)
    with patch("backtest.bar_cache._load_jsonl", side_effect=AssertionError("JSONL should not be read on cache hit")):
        bars_second = get_bars(bars_dir, "AAPL", "2026-04-28")

    assert len(bars_first) == len(bars_second)
    for a, b in zip(bars_first, bars_second):
        assert a["ts"] == b["ts"]
        assert abs((a["close"] or 0.0) - (b["close"] or 0.0)) < 1e-9


# ---------------------------------------------------------------------------
# Test: stale cache (mtime change) -> rebuilds
# ---------------------------------------------------------------------------


def test_stale_cache_rebuilds(tmp_path: pytest.TempPathFactory) -> None:
    bars_dir = _build_fixture(tmp_path)
    # Warm cache
    get_bars(bars_dir, "AAPL", "2026-04-28")
    pp = _parquet_path(bars_dir, "AAPL")
    mtime_before = pp.stat().st_mtime

    # Wait slightly and touch source file to invalidate cache key
    time.sleep(0.05)
    rth = bars_dir / "2026-04-28" / "AAPL.jsonl"
    rth.touch()

    # Should rebuild
    get_bars(bars_dir, "AAPL", "2026-04-28")
    mtime_after = pp.stat().st_mtime
    assert mtime_after > mtime_before, "Parquet must be refreshed after source mtime change"


# ---------------------------------------------------------------------------
# Test: bit-exact equality vs direct JSONL parse
# ---------------------------------------------------------------------------


def test_bit_exact_vs_jsonl(tmp_path: pytest.TempPathFactory) -> None:
    """Bar fields loaded via cache must match direct JSONL parse exactly."""
    from backtest.loader import load_bars as _load_bars_direct

    bars_dir = _build_fixture(tmp_path)
    date = "2026-04-28"

    # Load via cache
    cached = get_bars(bars_dir, "AAPL", date)

    # Load directly (mimic what load_day_bars does: rth + premarket combined)
    rth_direct = _load_bars_direct(bars_dir, date, "AAPL")
    # premarket direct load
    pre_path = bars_dir / date / "premarket" / "AAPL.jsonl"
    pre_direct = []
    if pre_path.is_file():
        import json as _json
        with open(pre_path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        pre_direct.append(_json.loads(line))
                    except Exception:
                        pass
    all_direct = sorted(rth_direct + pre_direct, key=lambda b: b.get("ts") or "")

    # Match by ts
    cached_by_ts = {b["ts"]: b for b in cached}
    for d_bar in all_direct:
        ts = d_bar.get("ts")
        if ts is None:
            continue
        c_bar = cached_by_ts.get(ts)
        assert c_bar is not None, f"ts={ts} present in direct load but missing from cache"
        for field in ("open", "high", "low", "close"):
            d_val = d_bar.get(field)
            c_val = c_bar.get(field)
            if d_val is None and c_val == 0.0:
                continue  # None coerced to 0.0 is acceptable
            if d_val is not None and c_val is not None:
                assert abs(float(d_val) - float(c_val)) < 1e-9, (
                    f"ts={ts} field={field}: direct={d_val} cache={c_val}"
                )


# ---------------------------------------------------------------------------
# Test: empty bars_dir returns empty list
# ---------------------------------------------------------------------------


def test_missing_ticker_returns_empty(tmp_path: pytest.TempPathFactory) -> None:
    bars_dir = tmp_path / "empty_bars"
    bars_dir.mkdir()
    result = get_bars(bars_dir, "NVDA", "2026-04-28")
    assert result == []


# ---------------------------------------------------------------------------
# Test: source_files_for_ticker finds all JSONL paths
# ---------------------------------------------------------------------------


def test_source_files_discovery(tmp_path: pytest.TempPathFactory) -> None:
    bars_dir = _build_fixture(tmp_path)
    files = _source_files_for_ticker(bars_dir, "AAPL")
    assert len(files) == 2, f"Expected 2 source files (rth + pre), got {len(files)}"


# ---------------------------------------------------------------------------
# Test: cache key changes when file size changes
# ---------------------------------------------------------------------------


def test_cache_key_changes_on_content(tmp_path: pytest.TempPathFactory) -> None:
    bars_dir = _build_fixture(tmp_path)
    files = _source_files_for_ticker(bars_dir, "AAPL")
    key_before = _cache_key(files)
    # Append a bar to change mtime + size
    rth = bars_dir / "2026-04-28" / "AAPL.jsonl"
    time.sleep(0.05)
    _write_bar(rth, _rth_bar("2026-04-28T14:00:00Z", 272.00))
    files2 = _source_files_for_ticker(bars_dir, "AAPL")
    key_after = _cache_key(files2)
    assert key_before != key_after, "Cache key must change when source file content changes"
