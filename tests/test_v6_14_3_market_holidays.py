"""v6.14.3 unit tests: volume_bucket lookback respects US market holidays.

Verifies _trading_days_back skips both weekends and known full-closure
holidays so the 55-trading-day window actually lands on dates with
real 09:30 bars (instead of pinning days_available at 53 when the
window grazes Presidents Day or Good Friday).

NOTE: this test file is intentionally em-dash free (escaped or
literal) per the project author guidelines.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import volume_bucket as vb


def test_holiday_set_contains_known_full_closures():
    """Sanity check the enumerated holiday list covers the 2026 set
    that motivated this patch."""
    assert date(2026, 1, 1) in vb._US_MARKET_HOLIDAYS
    assert date(2026, 1, 19) in vb._US_MARKET_HOLIDAYS
    assert date(2026, 2, 16) in vb._US_MARKET_HOLIDAYS  # Presidents Day
    assert date(2026, 4, 3) in vb._US_MARKET_HOLIDAYS   # Good Friday
    assert date(2026, 5, 25) in vb._US_MARKET_HOLIDAYS
    assert date(2026, 11, 26) in vb._US_MARKET_HOLIDAYS  # Thanksgiving
    assert date(2026, 12, 25) in vb._US_MARKET_HOLIDAYS


def test_is_us_market_holiday_unknown_date_is_false():
    """Unknown dates default to non-holiday so missing entries cannot
    cause false skips."""
    assert vb._is_us_market_holiday(date(2099, 6, 15)) is False
    # A trading-day weekday in the table range that is NOT a holiday:
    assert vb._is_us_market_holiday(date(2026, 5, 4)) is False  # Mon
    assert vb._is_us_market_holiday(date(2026, 4, 15)) is False  # Wed


def test_trading_days_back_returns_n_dates_no_weekends_no_holidays():
    """The output must contain exactly N entries, none of which is a
    weekend and none of which is in the holiday set."""
    end = date(2026, 5, 4)
    days = vb._trading_days_back(end, 55)
    assert len(days) == 55
    for d in days:
        assert d.weekday() < 5, f"weekend leaked: {d}"
        assert d not in vb._US_MARKET_HOLIDAYS, f"holiday leaked: {d}"
    # Strictly before end, descending.
    assert max(days) < end
    assert days == sorted(days, reverse=True)


def test_trading_days_back_skips_presidents_day_and_good_friday():
    """The 55-day lookback from 2026-05-04 must not include either
    Presidents Day 2026-02-16 or Good Friday 2026-04-03."""
    end = date(2026, 5, 4)
    days = set(vb._trading_days_back(end, 55))
    assert date(2026, 2, 16) not in days
    assert date(2026, 4, 3) not in days


def test_trading_days_back_window_extends_when_holidays_hit():
    """With holidays excluded, the 55-day window must reach further
    back in calendar time than the legacy weekend-only walker would
    have. Legacy 55th day from 2026-05-04 was 2026-02-16; the new
    walker should reach 2026-02-13 or earlier."""
    end = date(2026, 5, 4)
    days = vb._trading_days_back(end, 55)
    earliest = min(days)
    assert earliest <= date(2026, 2, 13)


def test_trading_days_back_n_one_is_previous_trading_day():
    """For end=Tuesday 2026-05-05, the previous trading day is
    Monday 2026-05-04 (no holiday). Sanity-check the simple case."""
    end = date(2026, 5, 5)
    days = vb._trading_days_back(end, 1)
    assert days == [date(2026, 5, 4)]


def test_trading_days_back_skips_holiday_at_start():
    """end = 2026-04-06 (Mon) -> previous trading day must be
    2026-04-02 (Thu), because 2026-04-03 is Good Friday and 2026-04-04
    and 2026-04-05 are weekend."""
    end = date(2026, 4, 6)
    days = vb._trading_days_back(end, 1)
    assert days == [date(2026, 4, 2)]


def test_bot_version_is_6_14_3():
    """Version-pin parity check (matches the per-version tests on main)."""
    if "bot_version" in sys.modules:
        del sys.modules["bot_version"]
    import bot_version
    assert bot_version.BOT_VERSION == "6.14.3"
