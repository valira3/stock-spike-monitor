"""v8.3.28 -- tests for tools/fetch_alpaca_ticks.py.

The Alpaca side of the world is exercised only via mocks; the
network-touching pieces are deliberately small. Tests cover the
pure-Python helpers + the JSONL.gz writer + the day enumerator.
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import types
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

# Stub alpaca-py for the import-time side of fetch_alpaca_ticks so the
# test sandbox doesn't need the SDK installed.
if "alpaca" not in sys.modules:
    pkg = types.ModuleType("alpaca")
    sys.modules["alpaca"] = pkg
    sub = types.ModuleType("alpaca.data")
    sys.modules["alpaca.data"] = sub
    sub_h = types.ModuleType("alpaca.data.historical")
    sub_h.StockHistoricalDataClient = object
    sys.modules["alpaca.data.historical"] = sub_h
    sub_r = types.ModuleType("alpaca.data.requests")
    sub_r.StockTradesRequest = object
    sys.modules["alpaca.data.requests"] = sub_r

from tools.fetch_alpaca_ticks import (
    DEFAULT_TICKERS,
    _enum_days,
    _et_window,
    _is_weekday,
    _trade_to_dict,
    _write_jsonl_gz,
)


class TestDayEnumeration:

    def test_skips_weekend(self):
        # Fri 2026-05-08 .. Mon 2026-05-11 -- weekend in between
        days = _enum_days(date(2026, 5, 8), date(2026, 5, 11))
        assert [d.weekday() for d in days] == [4, 0]  # Fri, Mon
        assert all(_is_weekday(d) for d in days)

    def test_inclusive_endpoints(self):
        days = _enum_days(date(2026, 5, 12), date(2026, 5, 12))
        assert days == [date(2026, 5, 12)]

    def test_full_week(self):
        days = _enum_days(date(2026, 5, 11), date(2026, 5, 15))
        assert len(days) == 5  # Mon-Fri


class TestETWindow:

    def test_rth_window_utc(self):
        start, end = _et_window(date(2026, 5, 12), premarket=False)
        # 09:30 ET = 13:30 UTC (EDT)
        assert start.hour == 13
        assert start.minute == 30
        # 16:00 ET = 20:00 UTC (EDT)
        assert end.hour == 20
        assert end.minute == 0

    def test_premarket_window_utc(self):
        start, _end = _et_window(date(2026, 5, 12), premarket=True)
        # 04:00 ET = 08:00 UTC (EDT)
        assert start.hour == 8
        assert start.minute == 0


class _FakeTrade:
    def __init__(self, ts, price, size, exchange="Q", conditions=None,
                 tape="C", trade_id=12345):
        self.timestamp = ts
        self.price = price
        self.size = size
        self.exchange = exchange
        self.conditions = conditions or ["@"]
        self.tape = tape
        self.id = trade_id


class TestTradeToDict:

    def test_basic_fields(self):
        ts = datetime(2026, 5, 12, 14, 0, 30, tzinfo=timezone.utc)
        t = _FakeTrade(ts, 264.41, 100)
        row = _trade_to_dict(t, "sip")
        assert row["ts"] == ts.isoformat()
        assert row["price"] == 264.41
        assert row["size"] == 100
        assert row["exchange"] == "Q"
        assert row["conditions"] == ["@"]
        assert row["tape"] == "C"
        assert row["feed_source"] == "sip"

    def test_missing_timestamp_returns_none(self):
        t = _FakeTrade(None, 264.41, 100)
        assert _trade_to_dict(t, "sip") is None

    def test_missing_price_returns_none(self):
        t = _FakeTrade(datetime.now(timezone.utc), None, 100)
        assert _trade_to_dict(t, "sip") is None

    def test_naive_timestamp_attached_to_utc(self):
        ts = datetime(2026, 5, 12, 14, 0, 30)  # naive
        t = _FakeTrade(ts, 264.41, 100)
        row = _trade_to_dict(t, "sip")
        assert "T" in row["ts"]
        assert row["ts"].endswith("+00:00") or row["ts"].endswith("Z")

    def test_int_size_preserved(self):
        ts = datetime.now(timezone.utc)
        t = _FakeTrade(ts, 100.0, 250)
        row = _trade_to_dict(t, "iex")
        assert row["size"] == 250
        assert isinstance(row["size"], int)


class TestJsonlGzWriter:

    def test_round_trip(self, tmp_path):
        rows = [
            {"ts": "2026-05-12T13:30:00+00:00", "price": 100.0, "size": 5},
            {"ts": "2026-05-12T13:30:01+00:00", "price": 100.1, "size": 7},
        ]
        out = tmp_path / "AAPL.jsonl.gz"
        n = _write_jsonl_gz(rows, out)
        assert n == 2
        assert out.exists()
        with gzip.open(out, "rt", encoding="utf-8") as fh:
            loaded = [json.loads(line) for line in fh]
        assert loaded == rows

    def test_creates_parent_dir(self, tmp_path):
        out = tmp_path / "nested" / "deep" / "TK.jsonl.gz"
        _write_jsonl_gz([{"a": 1}], out)
        assert out.exists()


class TestDefaults:

    def test_default_universe_is_12(self):
        assert len(DEFAULT_TICKERS) == 12
        for tk in ("AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOG",
                   "AMZN", "AVGO", "NFLX", "ORCL", "SPY", "QQQ"):
            assert tk in DEFAULT_TICKERS
