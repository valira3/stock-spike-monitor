"""v6.9.2 -- bar_cache repartition tests.

Verify that the per-day Parquet layout ensures a single-day read
does NOT pull rows from other dates, and that each (ticker, date)
pair maps to exactly one small file.

Rules: zero em-dashes (literal or escaped). All paths use tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backtest.bar_cache import (
    _CACHE_DIR_NAME,
    _parquet_path,
    build_all,
    get_bars,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_bar(path: Path, bar: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(bar) + "\n")


def _rth_bar(ts: str, close: float) -> dict:
    return {
        "ts": ts,
        "open": close - 0.01,
        "high": close + 0.05,
        "low": close - 0.05,
        "close": close,
        "iex_volume": 1000,
        "session": "rth",
    }


def _build_multi_day_fixture(tmp_path: Path, dates: list[str]) -> Path:
    """Write one RTH bar per date for AAPL."""
    bars_dir = tmp_path / "bars"
    for date in dates:
        ts = f"{date}T14:00:00Z"
        _write_bar(bars_dir / date / "AAPL.jsonl", _rth_bar(ts, 100.0 + float(date[-2:])))
    return bars_dir


# ---------------------------------------------------------------------------
# Test: per-day file isolation -- reading date A does not load date B rows
# ---------------------------------------------------------------------------


def test_per_day_file_contains_only_one_date(tmp_path: Path) -> None:
    """Each per-day Parquet must contain rows for exactly one date."""
    dates = ["2026-01-02", "2026-01-03", "2026-01-06"]
    bars_dir = _build_multi_day_fixture(tmp_path, dates)

    # Trigger cache build for all dates
    for d in dates:
        get_bars(bars_dir, "AAPL", d)

    # Verify each Parquet exists and contains only its own date
    for d in dates:
        pp = _parquet_path(bars_dir, "AAPL", d)
        assert pp.is_file(), f"Missing per-day Parquet for date={d}"

        import pyarrow.parquet as pq
        table = pq.read_table(str(pp))
        dates_in_file = set(table.column("date").to_pylist())
        assert dates_in_file == {d}, (
            f"Parquet for {d} contains unexpected dates: {dates_in_file}"
        )


# ---------------------------------------------------------------------------
# Test: reading one date does not return rows from other dates
# ---------------------------------------------------------------------------


def test_get_bars_returns_only_requested_date(tmp_path: Path) -> None:
    """get_bars(ticker, date) must return bars only for that date."""
    dates = ["2026-02-03", "2026-02-04", "2026-02-05"]
    bars_dir = _build_multi_day_fixture(tmp_path, dates)

    for d in dates:
        bars = get_bars(bars_dir, "AAPL", d)
        returned_dates = {b["date"] for b in bars}
        assert returned_dates == {d}, (
            f"get_bars returned wrong dates for {d}: {returned_dates}"
        )


# ---------------------------------------------------------------------------
# Test: per-day Parquet is smaller than the full-ticker file would be
# ---------------------------------------------------------------------------


def test_per_day_parquet_file_count_matches_date_count(tmp_path: Path) -> None:
    """Number of per-day Parquets must equal number of distinct dates."""
    dates = ["2026-03-10", "2026-03-11", "2026-03-12", "2026-03-13"]
    bars_dir = _build_multi_day_fixture(tmp_path, dates)
    build_all(bars_dir)

    ticker_dir = bars_dir / _CACHE_DIR_NAME / "AAPL"
    parquets = sorted(ticker_dir.glob("*.parquet"))
    assert len(parquets) == len(dates), (
        f"Expected {len(dates)} per-day Parquets, found {len(parquets)}"
    )
    parquet_dates = {p.stem for p in parquets}
    assert parquet_dates == set(dates), (
        f"Parquet file names do not match expected dates: {parquet_dates}"
    )


# ---------------------------------------------------------------------------
# Test: no cross-contamination between two tickers sharing same dates
# ---------------------------------------------------------------------------


def test_per_day_file_ticker_isolation(tmp_path: Path) -> None:
    """Per-day Parquets for different tickers must not share row content."""
    bars_dir = tmp_path / "bars"
    date = "2026-04-01"
    for ticker, close in [("AAPL", 170.0), ("NVDA", 800.0)]:
        ts = f"{date}T14:30:00Z"
        _write_bar(
            bars_dir / date / f"{ticker}.jsonl",
            {"ts": ts, "open": close, "high": close + 1, "low": close - 1,
             "close": close, "iex_volume": 100, "session": "rth"},
        )

    aapl_bars = get_bars(bars_dir, "AAPL", date)
    nvda_bars = get_bars(bars_dir, "NVDA", date)

    aapl_closes = [b["close"] for b in aapl_bars]
    nvda_closes = [b["close"] for b in nvda_bars]

    assert all(abs(c - 170.0) < 1e-9 for c in aapl_closes), (
        f"AAPL bars contain unexpected close values: {aapl_closes}"
    )
    assert all(abs(c - 800.0) < 1e-9 for c in nvda_closes), (
        f"NVDA bars contain unexpected close values: {nvda_closes}"
    )
