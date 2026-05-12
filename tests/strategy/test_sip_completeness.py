"""v8.3.2 -- SIP completeness helper tests.

Exercises the pure helpers used by _fetch_1min_bars_alpaca to detect
thin SIP results and merge with an IEX retry. These functions are duck-
typed on bar objects (only require a .timestamp attribute), so we use
plain SimpleNamespace stand-ins instead of pulling in alpaca-py.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from engine.data_completeness import (
    OR_START_MIN_ET,
    OR_END_MIN_ET,
    _or_expected_bars,
    _count_alpaca_rows_in_or_window,
    _is_or_coverage_thin,
    _merge_alpaca_rows_by_timestamp,
)


ET = ZoneInfo("America/New_York")


def _bar_at(et_hh: int, et_mm: int, date_iso: str = "2026-05-12"):
    """Make a fake alpaca-py Bar at a given ET clock time on date_iso.

    Returns SimpleNamespace(timestamp=tz-aware-UTC-datetime).
    """
    y, m, d = map(int, date_iso.split("-"))
    et_dt = datetime(y, m, d, et_hh, et_mm, tzinfo=ET)
    utc_dt = et_dt.astimezone(timezone.utc)
    return SimpleNamespace(timestamp=utc_dt)


def _bars_consecutive(start_hh: int, start_mm: int, n: int,
                      date_iso: str = "2026-05-12"):
    """Build n consecutive 1m bars starting at start_hh:start_mm ET."""
    out = []
    cur = start_hh * 60 + start_mm
    for _ in range(n):
        hh, mm = divmod(cur, 60)
        out.append(_bar_at(hh, mm, date_iso))
        cur += 1
    return out


# ------------------ _or_expected_bars ------------------


class TestOrExpectedBars:

    def test_premarket_returns_zero(self):
        # 09:15 ET, before OR opens
        now_et = datetime(2026, 5, 12, 9, 15, tzinfo=ET)
        assert _or_expected_bars(now_et) == 0

    def test_at_or_open_returns_zero(self):
        # 09:30:00 ET exactly -- no bars have closed yet
        now_et = datetime(2026, 5, 12, 9, 30, tzinfo=ET)
        assert _or_expected_bars(now_et) == 0

    def test_mid_or_partial_count(self):
        # 09:45 ET -- expect 15 bars (09:30..09:44 closed)
        now_et = datetime(2026, 5, 12, 9, 45, tzinfo=ET)
        assert _or_expected_bars(now_et) == 15

    def test_at_or_close_returns_30(self):
        # 10:00 ET -- the full OR window has closed
        now_et = datetime(2026, 5, 12, 10, 0, tzinfo=ET)
        assert _or_expected_bars(now_et) == 30

    def test_well_past_or_returns_30(self):
        # 14:07 ET (the time on the operator's screenshot) -- still 30
        now_et = datetime(2026, 5, 12, 14, 7, tzinfo=ET)
        assert _or_expected_bars(now_et) == 30


# ------------------ _count_alpaca_rows_in_or_window ------------------


class TestCountAlpacaRowsInOrWindow:

    def test_empty_rows(self):
        assert _count_alpaca_rows_in_or_window([], ET) == 0

    def test_only_premarket_bars_counted_as_zero(self):
        rows = _bars_consecutive(9, 25, 5)  # 09:25..09:29
        assert _count_alpaca_rows_in_or_window(rows, ET) == 0

    def test_only_in_window_bars(self):
        rows = _bars_consecutive(9, 30, 30)  # 09:30..09:59
        assert _count_alpaca_rows_in_or_window(rows, ET) == 30

    def test_mixed_in_and_out(self):
        rows = (
            _bars_consecutive(9, 25, 5)    # 09:25..09:29 (out, premarket)
            + _bars_consecutive(9, 30, 30)  # 09:30..09:59 (in)
            + _bars_consecutive(10, 0, 5)   # 10:00..10:04 (out, post-OR)
        )
        assert _count_alpaca_rows_in_or_window(rows, ET) == 30

    def test_post_or_bar_excluded(self):
        # 10:00:00 ET is the exclusive upper bound -- should NOT count
        rows = [_bar_at(10, 0)]
        assert _count_alpaca_rows_in_or_window(rows, ET) == 0

    def test_or_start_bar_included(self):
        # 09:30:00 ET is the inclusive lower bound
        rows = [_bar_at(9, 30)]
        assert _count_alpaca_rows_in_or_window(rows, ET) == 1

    def test_handles_naive_timestamp(self):
        """A naive datetime is treated as UTC. We craft a UTC moment
        that's inside the OR window after ET conversion."""
        # 2026-05-12 13:35 UTC == 09:35 ET (during EDT, UTC-4)
        rows = [SimpleNamespace(timestamp=datetime(2026, 5, 12, 13, 35))]
        assert _count_alpaca_rows_in_or_window(rows, ET) == 1

    def test_skips_rows_without_timestamp(self):
        rows = [
            SimpleNamespace(timestamp=None),
            _bar_at(9, 30),
            SimpleNamespace(other_field=123),  # no timestamp attr
        ]
        assert _count_alpaca_rows_in_or_window(rows, ET) == 1


# ------------------ _is_or_coverage_thin ------------------


class TestIsOrCoverageThin:

    def test_full_coverage_not_thin(self):
        assert _is_or_coverage_thin(30, 30) is False

    def test_above_80_pct_not_thin(self):
        assert _is_or_coverage_thin(24, 30) is False  # 80%
        assert _is_or_coverage_thin(25, 30) is False  # 83%

    def test_below_80_pct_is_thin(self):
        # The operator screenshot: 9 of 30
        assert _is_or_coverage_thin(9, 30) is True

    def test_zero_coverage_is_thin(self):
        assert _is_or_coverage_thin(0, 30) is True

    def test_small_expected_window_is_never_thin(self):
        # Pre-RTH / early-OR: too few expected bars to distinguish
        # noise from a real gap. Don't fire the retry.
        assert _is_or_coverage_thin(0, 0) is False
        assert _is_or_coverage_thin(0, 4) is False
        assert _is_or_coverage_thin(1, 4) is False

    def test_hard_threshold_more_lenient(self):
        # hard=True fires at <50%, not <80%
        assert _is_or_coverage_thin(20, 30, hard=False) is True   # 67% < 80%
        assert _is_or_coverage_thin(20, 30, hard=True) is False   # 67% > 50%
        assert _is_or_coverage_thin(14, 30, hard=True) is True    # 47% < 50%


# ------------------ _merge_alpaca_rows_by_timestamp ------------------


class TestMergeAlpacaRowsByTimestamp:

    def test_merge_empty_lists(self):
        assert _merge_alpaca_rows_by_timestamp([], []) == []

    def test_merge_disjoint_unions(self):
        sip = _bars_consecutive(9, 30, 5)    # 09:30..09:34
        iex = _bars_consecutive(9, 35, 5)    # 09:35..09:39
        merged = _merge_alpaca_rows_by_timestamp(sip, iex)
        assert len(merged) == 10
        # Oldest first
        assert merged[0].timestamp == sip[0].timestamp
        assert merged[-1].timestamp == iex[-1].timestamp

    def test_sip_wins_on_duplicate(self):
        """When SIP + IEX both have the same minute, SIP wins."""
        sip_bar = SimpleNamespace(
            timestamp=_bar_at(9, 35).timestamp, source="SIP")
        iex_bar = SimpleNamespace(
            timestamp=_bar_at(9, 35).timestamp, source="IEX")
        merged = _merge_alpaca_rows_by_timestamp([sip_bar], [iex_bar])
        assert len(merged) == 1
        assert merged[0].source == "SIP"

    def test_thin_sip_filled_by_iex(self):
        """Simulate the operator's failure mode: SIP has 9 OR-window
        bars (e.g. 09:30, 09:32, 09:34, ..., 09:46), IEX has 30. The
        merge should be ~30."""
        sip = [_bar_at(9, 30 + 2 * i) for i in range(9)]  # every other minute
        iex = _bars_consecutive(9, 30, 30)
        merged = _merge_alpaca_rows_by_timestamp(sip, iex)
        # All 30 in-window minutes present
        in_or = _count_alpaca_rows_in_or_window(merged, ET)
        assert in_or == 30

    def test_skips_unparseable_timestamps(self):
        good = _bar_at(9, 30)
        bad = SimpleNamespace(timestamp="not a datetime")
        merged = _merge_alpaca_rows_by_timestamp([good, bad])
        assert len(merged) == 1
        assert merged[0] is good
