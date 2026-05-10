"""Tests for orb.day_gates -- session-start filters."""
from __future__ import annotations

import pytest

from orb.day_gates import (
    DayGateConfig,
    DayGateResult,
    TickerGateResult,
    evaluate_day,
)


# Helper for per-ticker gate inputs
def _full_inputs(tickers, default_open=100.0, default_prev_close=100.0):
    open_today = {t: default_open for t in tickers}
    prev_close = {t: default_prev_close for t in tickers}
    return open_today, prev_close


class TestVixGate:

    def test_vix_below_threshold_passes(self):
        cfg = DayGateConfig(skip_vix_above=22.0)
        tk = ["AAPL"]
        ot, pc = _full_inputs(tk)
        r = evaluate_day(cfg, date_iso="2026-01-02", vix_close_d1=18.5,
                         tickers=tk, ticker_open_today=ot, ticker_prev_close=pc)
        assert not r.block_day
        assert r.vix_d1_close == 18.5

    def test_vix_above_threshold_blocks_day(self):
        cfg = DayGateConfig(skip_vix_above=22.0)
        tk = ["AAPL", "NVDA"]
        ot, pc = _full_inputs(tk)
        r = evaluate_day(cfg, date_iso="2026-01-02", vix_close_d1=24.5,
                         tickers=tk, ticker_open_today=ot, ticker_prev_close=pc)
        assert r.block_day
        assert "vix_high" in r.block_reason
        assert "24.50" in r.block_reason

    def test_vix_at_exact_threshold_passes(self):
        """22.0 > 22.0 is False; only strictly above blocks."""
        cfg = DayGateConfig(skip_vix_above=22.0)
        r = evaluate_day(cfg, date_iso="2026-01-02", vix_close_d1=22.0,
                         tickers=["AAPL"], ticker_open_today={"AAPL": 100},
                         ticker_prev_close={"AAPL": 100})
        assert not r.block_day

    def test_missing_vix_fail_open_default(self):
        cfg = DayGateConfig(skip_vix_above=22.0, fail_closed_on_missing_vix=False)
        r = evaluate_day(cfg, date_iso="2026-01-02", vix_close_d1=None,
                         tickers=["AAPL"], ticker_open_today={"AAPL": 100},
                         ticker_prev_close={"AAPL": 100})
        assert not r.block_day
        assert r.vix_d1_close is None

    def test_missing_vix_fail_closed_when_configured(self):
        cfg = DayGateConfig(skip_vix_above=22.0, fail_closed_on_missing_vix=True)
        r = evaluate_day(cfg, date_iso="2026-01-02", vix_close_d1=None,
                         tickers=["AAPL"], ticker_open_today={"AAPL": 100},
                         ticker_prev_close={"AAPL": 100})
        assert r.block_day
        assert r.block_reason == "missing_vix"

    def test_vix_threshold_zero_disables_gate(self):
        cfg = DayGateConfig(skip_vix_above=0.0)
        r = evaluate_day(cfg, date_iso="2026-01-02", vix_close_d1=999.0,
                         tickers=["AAPL"], ticker_open_today={"AAPL": 100},
                         ticker_prev_close={"AAPL": 100})
        assert not r.block_day


class TestBlocklistGate:

    def test_blocklisted_long_short_blocks_both_sides(self):
        cfg = DayGateConfig(
            skip_vix_above=22.0,
            ticker_side_blocklist={"META": ["LONG", "SHORT"]},
            skip_earnings_window=False,
            skip_gap_above_pct=0.0,
        )
        r = evaluate_day(cfg, date_iso="2026-01-02", vix_close_d1=18.0,
                         tickers=["META"], ticker_open_today={"META": 500},
                         ticker_prev_close={"META": 498})
        assert not r.block_day
        meta = r.per_ticker["META"]
        assert meta.blocked
        assert set(meta.blocked_sides) == {"LONG", "SHORT"}
        assert not r.is_ticker_allowed("META", "LONG")
        assert not r.is_ticker_allowed("META", "SHORT")

    def test_blocklisted_one_side_only(self):
        cfg = DayGateConfig(
            skip_vix_above=22.0,
            ticker_side_blocklist={"AAPL": ["LONG"]},
            skip_earnings_window=False,
            skip_gap_above_pct=0.0,
        )
        r = evaluate_day(cfg, date_iso="2026-01-02", vix_close_d1=18.0,
                         tickers=["AAPL"], ticker_open_today={"AAPL": 100},
                         ticker_prev_close={"AAPL": 100})
        aapl = r.per_ticker["AAPL"]
        assert aapl.blocked
        assert aapl.blocked_sides == ("LONG",)
        assert not r.is_ticker_allowed("AAPL", "LONG")
        assert r.is_ticker_allowed("AAPL", "SHORT")

    def test_no_blocklist_allows_both_sides(self):
        cfg = DayGateConfig(skip_vix_above=22.0, ticker_side_blocklist=None,
                            skip_earnings_window=False, skip_gap_above_pct=0.0)
        r = evaluate_day(cfg, date_iso="2026-01-02", vix_close_d1=18.0,
                         tickers=["AAPL"], ticker_open_today={"AAPL": 100},
                         ticker_prev_close={"AAPL": 100})
        assert r.is_ticker_allowed("AAPL", "LONG")
        assert r.is_ticker_allowed("AAPL", "SHORT")


class TestEarningsGate:

    def _make_earnings_fn(self, blocked_set):
        """blocked_set: a set of (ticker, date_iso) that should return True."""
        def fn(ticker, date_iso, days_before, days_after):
            return (ticker, date_iso) in blocked_set
        return fn

    def test_earnings_blocks_ticker(self):
        cfg = DayGateConfig(skip_vix_above=22.0, skip_earnings_window=True,
                            earnings_days_before=1, skip_gap_above_pct=0.0)
        fn = self._make_earnings_fn({("AAPL", "2026-01-02")})
        r = evaluate_day(cfg, date_iso="2026-01-02", vix_close_d1=18.0,
                         tickers=["AAPL", "NVDA"],
                         ticker_open_today={"AAPL": 100, "NVDA": 100},
                         ticker_prev_close={"AAPL": 100, "NVDA": 100},
                         is_earnings_window_fn=fn)
        assert r.per_ticker["AAPL"].blocked
        assert r.per_ticker["AAPL"].block_reason == "earnings"
        assert r.per_ticker["AAPL"].earnings_within_window
        assert not r.per_ticker["NVDA"].blocked

    def test_earnings_disabled_when_flag_off(self):
        cfg = DayGateConfig(skip_vix_above=22.0, skip_earnings_window=False,
                            skip_gap_above_pct=0.0)
        fn = self._make_earnings_fn({("AAPL", "2026-01-02")})
        r = evaluate_day(cfg, date_iso="2026-01-02", vix_close_d1=18.0,
                         tickers=["AAPL"], ticker_open_today={"AAPL": 100},
                         ticker_prev_close={"AAPL": 100},
                         is_earnings_window_fn=fn)
        assert not r.per_ticker["AAPL"].blocked

    def test_earnings_fn_none_skips_gate(self):
        """If no callback provided, gate is silently skipped."""
        cfg = DayGateConfig(skip_vix_above=22.0, skip_earnings_window=True,
                            skip_gap_above_pct=0.0)
        r = evaluate_day(cfg, date_iso="2026-01-02", vix_close_d1=18.0,
                         tickers=["AAPL"], ticker_open_today={"AAPL": 100},
                         ticker_prev_close={"AAPL": 100},
                         is_earnings_window_fn=None)
        assert not r.per_ticker["AAPL"].blocked

    def test_earnings_fn_exception_treated_as_not_in_window(self):
        cfg = DayGateConfig(skip_vix_above=22.0, skip_earnings_window=True,
                            skip_gap_above_pct=0.0)
        def bad_fn(*a, **k):
            raise ValueError("bad")
        r = evaluate_day(cfg, date_iso="2026-01-02", vix_close_d1=18.0,
                         tickers=["AAPL"], ticker_open_today={"AAPL": 100},
                         ticker_prev_close={"AAPL": 100},
                         is_earnings_window_fn=bad_fn)
        assert not r.per_ticker["AAPL"].blocked


class TestGapGate:

    def test_gap_below_threshold_passes(self):
        cfg = DayGateConfig(skip_vix_above=22.0, skip_earnings_window=False,
                            skip_gap_above_pct=1.5)
        # 1.0% gap = 100 -> 101
        r = evaluate_day(cfg, date_iso="2026-01-02", vix_close_d1=18.0,
                         tickers=["AAPL"], ticker_open_today={"AAPL": 101.0},
                         ticker_prev_close={"AAPL": 100.0})
        assert not r.per_ticker["AAPL"].blocked
        assert abs(r.per_ticker["AAPL"].gap_pct - 1.0) < 1e-9

    def test_gap_above_threshold_blocks_ticker(self):
        cfg = DayGateConfig(skip_vix_above=22.0, skip_earnings_window=False,
                            skip_gap_above_pct=1.5)
        # 2.0% gap
        r = evaluate_day(cfg, date_iso="2026-01-02", vix_close_d1=18.0,
                         tickers=["AAPL"], ticker_open_today={"AAPL": 102.0},
                         ticker_prev_close={"AAPL": 100.0})
        assert r.per_ticker["AAPL"].blocked
        assert "gap" in r.per_ticker["AAPL"].block_reason

    def test_gap_uses_absolute_value(self):
        """Down-gap of 2% also triggers the gate."""
        cfg = DayGateConfig(skip_vix_above=22.0, skip_earnings_window=False,
                            skip_gap_above_pct=1.5)
        r = evaluate_day(cfg, date_iso="2026-01-02", vix_close_d1=18.0,
                         tickers=["AAPL"], ticker_open_today={"AAPL": 98.0},
                         ticker_prev_close={"AAPL": 100.0})
        assert r.per_ticker["AAPL"].blocked

    def test_gap_disabled_when_threshold_zero(self):
        cfg = DayGateConfig(skip_vix_above=22.0, skip_earnings_window=False,
                            skip_gap_above_pct=0.0)
        r = evaluate_day(cfg, date_iso="2026-01-02", vix_close_d1=18.0,
                         tickers=["AAPL"], ticker_open_today={"AAPL": 105.0},
                         ticker_prev_close={"AAPL": 100.0})
        assert not r.per_ticker["AAPL"].blocked

    def test_gap_missing_data_fail_open(self):
        cfg = DayGateConfig(skip_vix_above=22.0, skip_earnings_window=False,
                            skip_gap_above_pct=1.5)
        r = evaluate_day(cfg, date_iso="2026-01-02", vix_close_d1=18.0,
                         tickers=["AAPL"],
                         ticker_open_today={"AAPL": 102.0},
                         ticker_prev_close={"AAPL": None},
                         is_earnings_window_fn=None)
        assert not r.per_ticker["AAPL"].blocked


class TestCombinedGates:

    def test_vix_block_short_circuits_per_ticker(self):
        """When VIX blocks the day, per_ticker is empty (no work done)."""
        cfg = DayGateConfig(skip_vix_above=22.0)
        r = evaluate_day(cfg, date_iso="2026-01-02", vix_close_d1=25.0,
                         tickers=["AAPL", "NVDA"],
                         ticker_open_today={"AAPL": 100, "NVDA": 100},
                         ticker_prev_close={"AAPL": 100, "NVDA": 100})
        assert r.block_day
        assert r.per_ticker == {}

    def test_v10_keystone_config(self):
        """Smoke test of the actual v10 production config."""
        cfg = DayGateConfig(
            skip_vix_above=22.0,
            skip_earnings_window=True,
            earnings_days_before=1,
            skip_gap_above_pct=1.5,
            ticker_side_blocklist={"META": ["LONG", "SHORT"], "MSFT": ["LONG", "SHORT"]},
        )
        tickers = ["AAPL", "NVDA", "TSLA", "META", "GOOG", "AMZN", "AVGO",
                   "NFLX", "ORCL", "MSFT", "SPY", "QQQ"]
        r = evaluate_day(cfg, date_iso="2026-01-02", vix_close_d1=18.5,
                         tickers=tickers,
                         ticker_open_today={t: 100.0 for t in tickers},
                         ticker_prev_close={t: 100.0 for t in tickers})
        assert not r.block_day
        # META + MSFT blocked both sides
        assert not r.is_ticker_allowed("META", "LONG")
        assert not r.is_ticker_allowed("META", "SHORT")
        assert not r.is_ticker_allowed("MSFT", "LONG")
        assert not r.is_ticker_allowed("MSFT", "SHORT")
        # Others allowed
        assert r.is_ticker_allowed("AAPL", "LONG")
        assert r.is_ticker_allowed("NVDA", "SHORT")

    def test_is_ticker_allowed_unknown_ticker(self):
        """Tickers not in evaluate_day call default to allowed."""
        cfg = DayGateConfig()
        r = evaluate_day(cfg, date_iso="2026-01-02", vix_close_d1=18.0,
                         tickers=["AAPL"], ticker_open_today={"AAPL": 100},
                         ticker_prev_close={"AAPL": 100})
        assert r.is_ticker_allowed("UNKNOWN", "LONG")
