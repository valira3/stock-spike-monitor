"""v6.9.2 -- bar_cache LRU tests.

Verify that the second call to get_bars for the same (ticker, date)
does not re-read disk (LRU cache hit), and that get_indicators
similarly skips disk after the first call.

Rules: zero em-dashes (literal or escaped). All paths use tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from backtest.bar_cache import _lru_read_bars, get_bars
from backtest.indicator_cache import _lru_read_indicators, get_indicators


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_bar(path: Path, bar: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(bar) + "\n")


def _build_fixture(tmp_path: Path, date: str = "2026-05-01") -> Path:
    bars_dir = tmp_path / "bars"
    ts = f"{date}T14:00:00Z"
    _write_bar(
        bars_dir / date / "AAPL.jsonl",
        {"ts": ts, "open": 150.0, "high": 151.0, "low": 149.0,
         "close": 150.5, "iex_volume": 500, "session": "rth"},
    )
    return bars_dir


# ---------------------------------------------------------------------------
# Test: second get_bars call does not re-read Parquet from disk
# ---------------------------------------------------------------------------


def test_lru_prevents_second_disk_read(tmp_path: Path) -> None:
    """After the first get_bars call, the second call must use LRU (no disk)."""
    bars_dir = _build_fixture(tmp_path)
    date = "2026-05-01"
    ticker = "AAPL"

    # Clear LRU to ensure a clean state for this test
    _lru_read_bars.cache_clear()

    # First call: cold, hits disk and populates LRU
    bars_first = get_bars(bars_dir, ticker, date)
    assert bars_first, "Expected at least one bar"
    assert _lru_read_bars.cache_info().misses >= 1

    misses_after_first = _lru_read_bars.cache_info().misses
    hits_after_first = _lru_read_bars.cache_info().hits

    # Second call: must hit LRU, NOT read Parquet
    with patch("backtest.bar_cache._get_bars_uncached",
               side_effect=AssertionError("Disk read must not happen on LRU hit")):
        bars_second = get_bars(bars_dir, ticker, date)

    assert len(bars_first) == len(bars_second), (
        f"LRU returned different bar count: first={len(bars_first)} second={len(bars_second)}"
    )
    assert _lru_read_bars.cache_info().hits > hits_after_first, (
        "LRU hit counter did not increment on second call"
    )
    assert _lru_read_bars.cache_info().misses == misses_after_first, (
        "LRU miss counter incremented on second call (unexpected disk read)"
    )


# ---------------------------------------------------------------------------
# Test: different (ticker, date) pairs each get their own LRU entry
# ---------------------------------------------------------------------------


def test_lru_separate_entries_per_key(tmp_path: Path) -> None:
    """Different (ticker, date) pairs must each produce independent LRU entries."""
    bars_dir = tmp_path / "bars"
    for date, ticker, close in [
        ("2026-05-02", "AAPL", 160.0),
        ("2026-05-02", "NVDA", 700.0),
        ("2026-05-03", "AAPL", 161.0),
    ]:
        ts = f"{date}T14:00:00Z"
        _write_bar(
            bars_dir / date / f"{ticker}.jsonl",
            {"ts": ts, "open": close, "high": close + 1, "low": close - 1,
             "close": close, "iex_volume": 200, "session": "rth"},
        )

    _lru_read_bars.cache_clear()

    get_bars(bars_dir, "AAPL", "2026-05-02")
    get_bars(bars_dir, "NVDA", "2026-05-02")
    get_bars(bars_dir, "AAPL", "2026-05-03")

    info = _lru_read_bars.cache_info()
    assert info.currsize >= 3, (
        f"Expected at least 3 LRU entries (one per unique key), got {info.currsize}"
    )


# ---------------------------------------------------------------------------
# Test: LRU cache_clear resets hit counters
# ---------------------------------------------------------------------------


def test_lru_cache_clear_works(tmp_path: Path) -> None:
    """cache_clear() must reset the LRU so next call re-reads disk."""
    bars_dir = _build_fixture(tmp_path)
    date = "2026-05-01"
    ticker = "AAPL"

    _lru_read_bars.cache_clear()
    get_bars(bars_dir, ticker, date)
    size_before = _lru_read_bars.cache_info().currsize
    assert size_before >= 1

    _lru_read_bars.cache_clear()
    size_after = _lru_read_bars.cache_info().currsize
    assert size_after == 0, f"LRU cache not cleared: currsize={size_after}"


# ---------------------------------------------------------------------------
# Test: get_indicators LRU prevents second disk read
# ---------------------------------------------------------------------------


def test_indicator_lru_prevents_second_disk_read(tmp_path: Path) -> None:
    """After the first get_indicators call, the second must use LRU."""
    bars_dir = _build_fixture(tmp_path)
    date = "2026-05-01"
    indicators = ["ema9"]

    _lru_read_indicators.cache_clear()
    _lru_read_bars.cache_clear()

    # First call: cold
    first = get_indicators(bars_dir, "AAPL", date, indicators)
    assert "ema9" in first

    misses_after_first = _lru_read_indicators.cache_info().misses

    # Second call: must hit LRU
    second = get_indicators(bars_dir, "AAPL", date, indicators)
    assert len(first["ema9"]) == len(second["ema9"])

    assert _lru_read_indicators.cache_info().hits >= 1, (
        "Indicator LRU hit counter did not increment on second call"
    )
    assert _lru_read_indicators.cache_info().misses == misses_after_first, (
        "Indicator LRU miss counter incremented (unexpected compute)"
    )
