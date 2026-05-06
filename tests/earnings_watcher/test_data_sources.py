"""Tests for earnings_watcher.data_sources.

Smoke tests that use live API calls (skipped if credentials not set).
These tests validate the interface contracts without mocking.
"""
from __future__ import annotations

import os
import pytest


# ---------------------------------------------------------------------------
# get_earnings_calendar (FMP)
# ---------------------------------------------------------------------------

def test_get_earnings_calendar_smoke():
    """Smoke test: FMP call returns a list. Skip if FMP_API_KEY not set."""
    if not os.getenv("FMP_API_KEY"):
        pytest.skip("FMP_API_KEY not set in environment")

    from earnings_watcher.data_sources import get_earnings_calendar

    result = get_earnings_calendar("2026-05-05")
    assert isinstance(result, list), f"Expected list, got {type(result)}"
    # Even if no events, should return a list
    if result:
        first = result[0]
        assert "ticker" in first, f"Missing 'ticker' key in result: {first}"
        assert "date" in first
        # time field may be empty string
        assert "time" in first


def test_get_earnings_calendar_empty_without_key(monkeypatch):
    """Without FMP_API_KEY, returns empty list gracefully."""
    monkeypatch.delenv("FMP_API_KEY", raising=False)

    from earnings_watcher.data_sources import get_earnings_calendar

    result = get_earnings_calendar("2026-05-05")
    assert result == []


def test_get_earnings_calendar_bad_date():
    """Invalid date string should return empty list (not crash)."""
    if not os.getenv("FMP_API_KEY"):
        pytest.skip("FMP_API_KEY not set in environment")

    from earnings_watcher.data_sources import get_earnings_calendar

    result = get_earnings_calendar("1800-01-01")
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# get_account_equity (Alpaca)
# ---------------------------------------------------------------------------

def test_get_account_equity_returns_float_or_none():
    """Smoke test: returns float if Alpaca creds are set, else None."""
    has_creds = (
        bool(os.getenv("VAL_ALPACA_PAPER_KEY"))
        and bool(os.getenv("VAL_ALPACA_PAPER_SECRET"))
    )
    if not has_creds:
        pytest.skip("VAL_ALPACA_PAPER_KEY/SECRET not set in environment")

    from earnings_watcher.data_sources import get_account_equity

    result = get_account_equity()
    # Either a positive float or None (on API error)
    assert result is None or isinstance(result, float)
    if result is not None:
        assert result > 0, f"equity={result} should be positive for paper account"


def test_get_account_equity_returns_none_without_creds(monkeypatch):
    """Without Alpaca creds, returns None gracefully (no exception)."""
    monkeypatch.delenv("VAL_ALPACA_PAPER_KEY", raising=False)
    monkeypatch.delenv("VAL_ALPACA_PAPER_SECRET", raising=False)

    from earnings_watcher.data_sources import get_account_equity

    result = get_account_equity()
    assert result is None


# ---------------------------------------------------------------------------
# fetch_minute_bars (Alpaca)
# ---------------------------------------------------------------------------

def test_fetch_minute_bars_returns_list_without_creds(monkeypatch):
    """Without Alpaca creds, returns empty list (no exception)."""
    monkeypatch.delenv("VAL_ALPACA_PAPER_KEY", raising=False)
    monkeypatch.delenv("VAL_ALPACA_PAPER_SECRET", raising=False)

    from earnings_watcher.data_sources import fetch_minute_bars
    from datetime import datetime, timezone, timedelta

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=1)

    result = fetch_minute_bars("AAPL", start, end)
    assert isinstance(result, list)
    assert result == []


def test_fetch_minute_bars_bar_schema():
    """If Alpaca creds are set, returned bars must have required schema keys."""
    has_creds = (
        bool(os.getenv("VAL_ALPACA_PAPER_KEY"))
        and bool(os.getenv("VAL_ALPACA_PAPER_SECRET"))
    )
    if not has_creds:
        pytest.skip("VAL_ALPACA_PAPER_KEY/SECRET not set in environment")

    from earnings_watcher.data_sources import fetch_minute_bars
    from datetime import datetime, timezone, timedelta

    # Pull a known date+time range (AMD AMC 2026-05-05 19:00-19:10 UTC)
    start = datetime(2026, 5, 5, 19, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 5, 19, 10, 0, tzinfo=timezone.utc)
    result = fetch_minute_bars("AMD", start, end)

    # May be empty if data not available; if non-empty, schema must match
    if result:
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        for bar in result:
            for k in required:
                assert k in bar, f"Bar missing key '{k}': {bar}"
            assert isinstance(bar["timestamp"], str)
            assert isinstance(bar["volume"], int)
            assert float(bar["close"]) > 0
