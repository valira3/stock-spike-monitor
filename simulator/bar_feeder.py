"""simulator.bar_feeder -- read 1m bars from the on-disk JSONL corpus.

Serves bars to the mock Alpaca StockHistoricalDataClient. The corpus
layout matches production's bar-archive convention:

    data/YYYY-MM-DD/<TICKER>.jsonl

Each line is a JSON record with at minimum:

    {
      "timestamp_utc": "2025-05-15T13:30:00Z",
      "open": 174.12, "high": 174.30, "low": 174.05, "close": 174.25,
      "iex_volume": 18420,
      "total_volume": 22150,
      ...
    }

The feeder pre-loads a day's worth of bars per ticker, indexes by
bar bucket (minutes since ET midnight), and serves them on request.

For synthetic scenarios (no real corpus), build bars in-memory via
``BarFeeder.from_synthetic({...})``.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")


class BarFeeder:
    def __init__(self):
        # bars_by_ticker[ticker] = list of bar dicts in chronological order
        self._bars_by_ticker: Dict[str, List[dict]] = {}
        self._date: Optional[str] = None

    # ---- factories ------------------------------------------------------

    @classmethod
    def from_corpus(cls, date: str, tickers: List[str], corpus_root: str = "data") -> "BarFeeder":
        """Load bars for `date` (YYYY-MM-DD) from the on-disk corpus.

        Missing tickers are silently skipped -- the caller decides whether
        a missing roster constitutes a scenario failure.
        """
        feeder = cls()
        feeder._date = date
        day_dir = os.path.join(corpus_root, date)
        if not os.path.isdir(day_dir):
            return feeder
        for ticker in tickers:
            path = os.path.join(day_dir, f"{ticker}.jsonl")
            if not os.path.isfile(path):
                continue
            with open(path, "r") as fh:
                rows = []
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
                feeder._bars_by_ticker[ticker.upper()] = rows
        return feeder

    @classmethod
    def from_synthetic(cls, date: str, bars_by_ticker: Dict[str, List[dict]]) -> "BarFeeder":
        """Build from an in-memory dict (handy for golden-path tests)."""
        feeder = cls()
        feeder._date = date
        for t, bars in bars_by_ticker.items():
            feeder._bars_by_ticker[t.upper()] = list(bars)
        return feeder

    # ---- queries --------------------------------------------------------

    @property
    def date(self) -> Optional[str]:
        return self._date

    def tickers(self) -> List[str]:
        return sorted(self._bars_by_ticker)

    def bars_up_to(self, ticker: str, bucket_min: int) -> List[dict]:
        """Return all bars for `ticker` whose ET bucket <= bucket_min."""
        out = []
        for b in self._bars_by_ticker.get(ticker.upper(), []):
            bk = _bar_bucket(b)
            if bk is not None and bk <= bucket_min:
                out.append(b)
        return out

    def bar_at(self, ticker: str, bucket_min: int) -> Optional[dict]:
        """Return the single bar whose ET bucket matches bucket_min."""
        for b in self._bars_by_ticker.get(ticker.upper(), []):
            bk = _bar_bucket(b)
            if bk == bucket_min:
                return b
        return None


def _bar_bucket(bar: dict) -> Optional[int]:
    """ET minutes-since-midnight. Tries the pre-computed et_bucket
    field first (much faster); falls back to parsing the timestamp.

    Recognized timestamp field names: ``ts``, ``timestamp_utc``,
    ``timestamp``, ``t``."""
    et_bucket = bar.get("et_bucket")
    if et_bucket is not None:
        # et_bucket is "HHMM" string ("0930") in the corpus.
        try:
            s = str(et_bucket)
            if len(s) == 4 and s.isdigit():
                return int(s[:2]) * 60 + int(s[2:])
            if isinstance(et_bucket, int):
                return et_bucket
        except Exception:
            pass
    raw = bar.get("ts") or bar.get("timestamp_utc") or bar.get("timestamp") \
        or bar.get("t") or ""
    if not raw:
        return None
    try:
        if isinstance(raw, str) and raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw) if isinstance(raw, str) else raw
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    et = dt.astimezone(_NY)
    return et.hour * 60 + et.minute


def _parse_ts(bar: dict) -> datetime:
    """Legacy helper -- kept for back-compat with anything that imports it."""
    raw = bar.get("ts") or bar.get("timestamp_utc") or bar.get("timestamp") \
        or bar.get("t") or ""
    if isinstance(raw, str) and raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw) if isinstance(raw, str) else raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_NY)


# ---- builder for golden-path scenarios --------------------------------


def make_bar(
    date: str,
    hh: int,
    mm: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: int = 10_000,
) -> dict:
    """Build a synthetic bar dict matching the corpus schema."""
    et = datetime(int(date[:4]), int(date[5:7]), int(date[8:10]), hh, mm, 0, tzinfo=_NY)
    return {
        "timestamp_utc": et.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "open": float(open_),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "iex_volume": int(volume),
        "total_volume": int(volume),
    }
