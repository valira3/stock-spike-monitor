"""v5.20.1 \u2014 Premarket-only DI seed + 09:31 ET recompute.

Tests the new seeder semantics:
  * Window is exactly 08:00\u219209:30 ET (no prior-day fallback).
  * Cache is written only when bars_premarket \u2265 15.
  * `sufficient` flag is the truth of the \u226515 bar threshold.
  * `recompute_di_for_unseeded` is a no-op on already-seeded tickers
    and re-runs `seed_di_buffer` only on cache-empty / insufficient ones.
  * Scheduler JOBS table has a 09:31 row whose lambda reaches
    `di_recompute_0931`.
"""

from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("SSM_SMOKE_TEST", "1")


@pytest.fixture
def seeders_module(monkeypatch):
    """Import engine.seeders fresh and stub the trade_genius module accessor."""
    import trade_genius as tg
    from engine import seeders

    # Reset DI cache between tests
    tg._DI_SEED_CACHE.clear()

    # Stub TRADE_TICKERS for deterministic iteration
    monkeypatch.setattr(tg, "TRADE_TICKERS", ["AAPL", "MSFT"])
    return seeders, tg


def _stub_alpaca(monkeypatch, seeders, tg, n_5m_buckets):
    """Make seed_di_buffer's Alpaca call return n_5m_buckets worth of 1m rows.

    Each 5m bucket is filled with 5x 1m rows so the bucketing logic
    aggregates them into exactly one closed 5m candle each. All bars
    are forced fully INSIDE today's 08:00\u219209:30 ET window so the
    drop-newest-forming-bucket guard does not trim them.
    """
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    # Force "now" to be 09:30 ET so the seeder treats every fetched
    # bar as fully closed (drop-newest only triggers when now < bucket_end).
    fake_now = now_et.replace(hour=9, minute=30, second=0, microsecond=0)

    class FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return fake_now.astimezone(tz)
            return fake_now

    monkeypatch.setattr(seeders, "datetime", FakeDateTime)

    # Build n_5m_buckets x 5 = 5n synthetic 1m rows starting at 08:00 ET.
    win_start = fake_now.replace(hour=8, minute=0, second=0, microsecond=0)
    rows = []
    for bucket_idx in range(n_5m_buckets):
        for minute in range(5):
            ts_et = win_start.replace(
                minute=(bucket_idx * 5 + minute) % 60,
                hour=8 + ((bucket_idx * 5 + minute) // 60),
            )
            ts_utc = ts_et.astimezone(timezone.utc)
            row = MagicMock()
            row.timestamp = ts_utc
            row.high = 100.0 + bucket_idx * 0.1 + minute * 0.01
            row.low = 99.0 + bucket_idx * 0.1
            row.close = 99.5 + bucket_idx * 0.1 + minute * 0.005
            rows.append(row)

    fake_resp = MagicMock()
    fake_resp.data = {"AAPL": rows, "MSFT": rows}
    fake_client = MagicMock()
    fake_client.get_stock_bars.return_value = fake_resp
    monkeypatch.setattr(tg, "_alpaca_data_client", lambda: fake_client)


def test_premarket_seed_window_is_0800_to_0930_et(seeders_module, monkeypatch):
    """The seeder must request 08:00\u219209:30 ET, not 04:00 or prior-day."""
    seeders, tg = seeders_module
    _stub_alpaca(monkeypatch, seeders, tg, n_5m_buckets=18)

    from engine.seeders import (
        PREMARKET_DI_WINDOW_END_HHMM,
        PREMARKET_DI_WINDOW_START_HHMM,
        seed_di_buffer,
    )

    # Constants check
    assert PREMARKET_DI_WINDOW_START_HHMM == (8, 0)
    assert PREMARKET_DI_WINDOW_END_HHMM == (9, 30)

    result = seed_di_buffer("AAPL")
    assert result["window_et"] == "08:00-09:30"
    # 18 5m buckets fed in, but the 09:25\u219209:30 bucket is dropped because
    # its end aligns exactly with fake_now=09:30 (boundary check is
    # strict: drop only when now < bucket_end). With now == bucket_end,
    # the bucket is kept. So expect the full 18 here.
    assert result["bars_premarket"] == 18


def test_sufficient_threshold_is_15(seeders_module, monkeypatch):
    """Cache is written only when bars_premarket >= 15."""
    seeders, tg = seeders_module

    # 14 buckets \u2192 insufficient, no cache write
    _stub_alpaca(monkeypatch, seeders, tg, n_5m_buckets=14)
    seeders.seed_di_buffer("AAPL")
    assert "AAPL" not in tg._DI_SEED_CACHE

    # 15 buckets \u2192 sufficient, cache written
    _stub_alpaca(monkeypatch, seeders, tg, n_5m_buckets=15)
    result = seeders.seed_di_buffer("AAPL")
    assert result["sufficient"] is True
    assert "AAPL" in tg._DI_SEED_CACHE
    assert len(tg._DI_SEED_CACHE["AAPL"]) == 15


def test_no_prior_day_fallback(seeders_module, monkeypatch):
    """When premarket has 0 bars, no prior-day fetch must occur."""
    seeders, tg = seeders_module

    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    fake_now = datetime.now(et).replace(hour=9, minute=30, second=0, microsecond=0)

    class FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return fake_now.astimezone(tz)
            return fake_now

    monkeypatch.setattr(seeders, "datetime", FakeDateTime)

    fake_resp = MagicMock()
    fake_resp.data = {"AAPL": []}
    fake_client = MagicMock()
    fake_client.get_stock_bars.return_value = fake_resp
    monkeypatch.setattr(tg, "_alpaca_data_client", lambda: fake_client)

    result = seeders.seed_di_buffer("AAPL")
    assert result["bars_premarket"] == 0
    assert result["sufficient"] is False
    assert "AAPL" not in tg._DI_SEED_CACHE
    # Critical: only ONE Alpaca call should have happened (premarket only).
    assert fake_client.get_stock_bars.call_count == 1


def test_recompute_skips_already_seeded(seeders_module, monkeypatch):
    """recompute_di_for_unseeded must NOT re-fetch tickers with \u226515 bars."""
    seeders, tg = seeders_module

    # Pre-seed AAPL with 15 buckets, leave MSFT empty
    tg._DI_SEED_CACHE["AAPL"] = [
        {"bucket": i, "high": 1.0, "low": 0.5, "close": 0.75} for i in range(15)
    ]

    seed_one = MagicMock()
    monkeypatch.setattr(seeders, "seed_di_buffer", seed_one)

    result = seeders.recompute_di_for_unseeded(["AAPL", "MSFT"])

    assert result["already_seeded"] == 1
    assert result["recomputed"] == 1
    assert seed_one.call_count == 1
    assert seed_one.call_args.args[0] == "MSFT"


def test_recompute_resseeds_partial_cache(seeders_module, monkeypatch):
    """A cache entry with <15 bars must be considered insufficient and re-seeded."""
    seeders, tg = seeders_module

    # 10 buckets \u2014 below the 15 threshold, should be re-seeded
    tg._DI_SEED_CACHE["AAPL"] = [
        {"bucket": i, "high": 1.0, "low": 0.5, "close": 0.75} for i in range(10)
    ]

    seed_one = MagicMock()
    monkeypatch.setattr(seeders, "seed_di_buffer", seed_one)

    result = seeders.recompute_di_for_unseeded(["AAPL"])

    assert result["recomputed"] == 1
    assert result["already_seeded"] == 0
    assert seed_one.call_count == 1


def test_recompute_non_fatal_on_per_ticker_error(seeders_module, monkeypatch):
    """A crash during one ticker's recompute must not kill the rest."""
    seeders, tg = seeders_module

    def boom(ticker):
        if ticker == "AAPL":
            raise RuntimeError("simulated")

    monkeypatch.setattr(seeders, "seed_di_buffer", boom)

    result = seeders.recompute_di_for_unseeded(["AAPL", "MSFT"])
    assert result["failed"] == 1
    # MSFT should still have been attempted
    assert result["recomputed"] == 1


def test_di_recompute_0931_in_jobs_table():
    """Confirm the scheduler JOBS literal wires 09:31 to di_recompute_0931."""
    import trade_genius as tg

    src = inspect.getsource(tg.scheduler_thread)
    assert '"09:31"' in src
    assert "di_recompute_0931" in src


def test_di_recompute_0931_function_exists_and_is_safe():
    """di_recompute_0931 must be defined and call recompute_di_for_unseeded."""
    import trade_genius as tg

    assert hasattr(tg, "di_recompute_0931")
    assert callable(tg.di_recompute_0931)

    # Calling it with a stubbed recompute should not raise.
    called = {"n": 0}

    def fake_recompute(tickers):
        called["n"] += 1
        return {"recomputed": 0, "already_seeded": len(tickers), "failed": 0}

    original = tg._recompute_di_for_unseeded
    tg._recompute_di_for_unseeded = fake_recompute
    try:
        tg.di_recompute_0931()
        assert called["n"] == 1
    finally:
        tg._recompute_di_for_unseeded = original


def test_premarket_seed_kill_switch(seeders_module, monkeypatch):
    """DI_PREMARKET_SEED=0 must short-circuit before any Alpaca call."""
    seeders, tg = seeders_module

    fake_client = MagicMock()
    monkeypatch.setattr(tg, "_alpaca_data_client", lambda: fake_client)
    monkeypatch.setenv("DI_PREMARKET_SEED", "0")

    result = seeders.seed_di_buffer("AAPL")
    assert result["bars_premarket"] == 0
    assert result["sufficient"] is False
    assert fake_client.get_stock_bars.call_count == 0


def test_premarket_min_bars_constant_matches_di_period():
    """PREMARKET_DI_MIN_BARS must equal DI_PERIOD (the operator's '15 bars')."""
    import trade_genius as tg
    from engine.seeders import PREMARKET_DI_MIN_BARS

    assert PREMARKET_DI_MIN_BARS == tg.DI_PERIOD == 15
