"""Tests for the v7.13.0 helpers added by PR5 prep:
  - engine.portfolio_book.PortfolioBook.current_equity()
  - engine.timing.minutes_since_et_midnight()
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest


# ---------- minutes_since_et_midnight ----------


class TestMinutesSinceEtMidnight:

    def test_dst_summer(self):
        """2026-04-30 is EDT (UTC-4). 13:30 UTC = 09:30 ET = 570."""
        from engine.timing import minutes_since_et_midnight
        ts = datetime(2026, 4, 30, 13, 30, tzinfo=timezone.utc)
        assert minutes_since_et_midnight(ts) == 9 * 60 + 30

    def test_standard_time(self):
        """2025-11-03 is EST (UTC-5). 14:30 UTC = 09:30 ET = 570."""
        from engine.timing import minutes_since_et_midnight
        ts = datetime(2025, 11, 3, 14, 30, tzinfo=timezone.utc)
        assert minutes_since_et_midnight(ts) == 9 * 60 + 30

    def test_unix_timestamp_input(self):
        from engine.timing import minutes_since_et_midnight
        # 2026-04-30T13:30:00+00:00 -> 09:30 ET
        ts = int(datetime(2026, 4, 30, 13, 30, tzinfo=timezone.utc).timestamp())
        assert minutes_since_et_midnight(ts) == 570

    def test_naive_datetime_treated_as_utc(self):
        from engine.timing import minutes_since_et_midnight
        # Naive datetime is treated as UTC
        ts = datetime(2026, 4, 30, 13, 30)  # naive
        assert minutes_since_et_midnight(ts) == 570

    def test_or_close_at_10am_et(self):
        from engine.timing import minutes_since_et_midnight
        # 10:00 ET on 2026-04-30 (EDT) = 14:00 UTC = 600
        ts = datetime(2026, 4, 30, 14, 0, tzinfo=timezone.utc)
        assert minutes_since_et_midnight(ts) == 600

    def test_invalid_type_raises(self):
        from engine.timing import minutes_since_et_midnight
        with pytest.raises(TypeError):
            minutes_since_et_midnight("2026-04-30")

    def test_eod_at_15_55_et(self):
        from engine.timing import minutes_since_et_midnight
        # 15:55 ET on 2026-04-30 (EDT) = 19:55 UTC
        ts = datetime(2026, 4, 30, 19, 55, tzinfo=timezone.utc)
        assert minutes_since_et_midnight(ts) == 15 * 60 + 55


# ---------- PortfolioBook.current_equity ----------


class TestPortfolioBookCurrentEquity:

    def _make_book(self, paper_cash=100000.0):
        from engine.portfolio_book import PortfolioBook
        b = PortfolioBook(portfolio_id="main")
        b.paper_cash = paper_cash
        return b

    def test_no_positions_returns_paper_cash(self):
        b = self._make_book(100000.0)
        assert b.current_equity() == 100000.0

    def test_long_position_uses_mark_price(self):
        b = self._make_book(50000.0)
        b.positions["AAPL"] = {"entry_price": 100.0, "shares": 100}
        # mark price $110 -> long_mv = $11,000 -> equity = $50k + $11k
        assert b.current_equity({"AAPL": 110.0}) == 61000.0

    def test_long_position_falls_back_to_entry_price(self):
        b = self._make_book(50000.0)
        b.positions["AAPL"] = {"entry_price": 100.0, "shares": 100}
        # No mark provided -> uses entry price; long_mv = $10k
        assert b.current_equity() == 60000.0

    def test_short_position_subtracts_liability(self):
        b = self._make_book(100000.0)
        # Short 100 NVDA @ $200 (got $20k cash); now NVDA is $190 (good move)
        b.short_positions["NVDA"] = {"entry_price": 200.0, "shares": 100}
        # Equity = 100k cash - (100 * 190 short_liab) = 100k - 19k = 81k
        assert b.current_equity({"NVDA": 190.0}) == 81000.0

    def test_long_and_short_combined(self):
        b = self._make_book(100000.0)
        b.positions["AAPL"] = {"entry_price": 100.0, "shares": 100}
        b.short_positions["NVDA"] = {"entry_price": 200.0, "shares": 50}
        prices = {"AAPL": 110.0, "NVDA": 195.0}
        # Cash 100k + long_mv (100*110=11000) - short_liab (50*195=9750)
        assert b.current_equity(prices) == 101250.0

    def test_missing_share_count_treated_as_zero(self):
        b = self._make_book(50000.0)
        b.positions["AAPL"] = {"entry_price": 100.0}  # no 'shares' key
        # Defaults to 0; equity = paper_cash unchanged
        assert b.current_equity({"AAPL": 110.0}) == 50000.0

    def test_returns_float(self):
        b = self._make_book(100000)
        b.positions["AAPL"] = {"entry_price": 100.0, "shares": 50}
        result = b.current_equity({"AAPL": 105.0})
        assert isinstance(result, float)
        assert result == 105250.0  # 100000 + 5250

    def test_independent_per_book(self):
        from engine.portfolio_book import PortfolioBook
        main = PortfolioBook(portfolio_id="main")
        val = PortfolioBook(portfolio_id="val")
        main.paper_cash = 100000.0
        val.paper_cash = 50000.0
        main.positions["AAPL"] = {"entry_price": 100.0, "shares": 100}
        # val has NO positions -- should NOT be affected by main's
        prices = {"AAPL": 110.0}
        assert main.current_equity(prices) == 111000.0
        assert val.current_equity(prices) == 50000.0
