"""Tests for orb.live_adapter -- bridge between OrbEngine and scan.py."""
from __future__ import annotations

import pytest

from orb.engine import OrbConfig, OrbEngine
from orb.live_adapter import LiveAdapter, LiveAdapterRegistry, EntryResult, ExitResult


def make_engine_with_armed_or(or_high=101.0, or_low=99.0,
                               portfolio_ids=None,
                               equity_per_portfolio=None):
    """Helper: build an engine with a locked, armed OR window for AAPL."""
    cfg = OrbConfig(
        or_minutes=30, rr=2.5,
        range_min_pct=0.008, range_max_pct=0.025,
        max_trades_per_day=5,
        risk_per_trade_pct=2.0, max_concurrent_risk_dollars=2000.0,
        skip_vix_above=22.0, skip_earnings_window=False,
        skip_gap_above_pct=0.0, fail_closed_on_missing_vix=False,
        ticker_side_blocklist=None,
    )
    pids = portfolio_ids or ["main"]
    eq = equity_per_portfolio or {pid: 100000.0 for pid in pids}
    eng = OrbEngine(cfg, portfolio_ids=pids)
    eng.start_new_session(
        date_iso="2026-01-02", tickers=["AAPL"],
        vix_close_d1=18.0,
        ticker_open_today={"AAPL": 100.0},
        ticker_prev_close={"AAPL": 100.0},
        equity_per_portfolio=eq,
    )
    # Feed 30 1-min bars; lock window
    for m in range(570, 600):
        h = or_high if m == 580 else or_high - 0.5
        l = or_low if m == 585 else or_low + 0.5
        eng.on_bar_arrival(
            ticker="AAPL",
            bar_high=h, bar_low=l,
            bar_open=100.0, bar_close=100.0,
            bar_volume=10000, bar_bucket_min=m,
        )
    return cfg, eng


# ------------------ feed_bar ------------------


class TestFeedBar:

    def test_bar_routed_to_engine(self):
        cfg = OrbConfig(or_minutes=30, range_min_pct=0.008,
                        range_max_pct=0.025, skip_vix_above=22.0,
                        skip_earnings_window=False, skip_gap_above_pct=0.0,
                        fail_closed_on_missing_vix=False)
        eng = OrbEngine(cfg, portfolio_ids=["main"])
        eng.start_new_session(date_iso="2026-01-02", tickers=["AAPL"],
                              vix_close_d1=18.0,
                              ticker_open_today={"AAPL": 100.0},
                              ticker_prev_close={"AAPL": 100.0},
                              equity_per_portfolio={"main": 100000.0})
        adapter = LiveAdapter(eng, portfolio_id="main")
        adapter.feed_bar(ticker="AAPL", bar_high=101.0, bar_low=100.0,
                         bar_open=100.0, bar_close=100.5,
                         bar_volume=10000, bar_bucket_min=570)
        # Engine's OR window should have 1 bar
        w = eng._state.or_windows["AAPL"]
        assert w.bars_seen == 1


# ------------------ check_entry ------------------


class TestCheckEntry:

    def test_long_entry_admitted(self):
        cfg, eng = make_engine_with_armed_or()
        adapter = LiveAdapter(eng, portfolio_id="main")
        result = adapter.check_entry(
            "AAPL", side="long",
            five_min_close=101.5, next_open=101.5, equity=100000.0,
        )
        assert result.ok
        assert result.side == "long"
        assert result.price == 101.5
        assert result.shares > 0
        assert result.ticket_id != ""
        # Adapter tracks the position
        assert adapter.open_position_count() == 1
        assert adapter.has_position("AAPL")

    def test_short_entry_admitted(self):
        cfg, eng = make_engine_with_armed_or()
        adapter = LiveAdapter(eng, portfolio_id="main")
        result = adapter.check_entry(
            "AAPL", side="short",
            five_min_close=98.5, next_open=98.5, equity=100000.0,
        )
        assert result.ok
        assert result.side == "short"

    def test_no_signal_inside_or(self):
        cfg, eng = make_engine_with_armed_or()
        adapter = LiveAdapter(eng, portfolio_id="main")
        result = adapter.check_entry(
            "AAPL", side="long",
            five_min_close=100.5, next_open=100.5, equity=100000.0,
        )
        assert not result.ok
        assert result.reason_no == "no_signal"

    def test_invalid_side_rejected(self):
        cfg, eng = make_engine_with_armed_or()
        adapter = LiveAdapter(eng, portfolio_id="main")
        result = adapter.check_entry(
            "AAPL", side="UP",  # invalid
            five_min_close=101.5, next_open=101.5, equity=100000.0,
        )
        assert not result.ok
        assert "invalid_side" in result.reason_no

    def test_opposite_side_rejected(self):
        """If the breakout is short but caller asks for long, return ok=False."""
        cfg, eng = make_engine_with_armed_or()
        adapter = LiveAdapter(eng, portfolio_id="main")
        # Short breakout (close below or_low)
        result = adapter.check_entry(
            "AAPL", side="long",  # but signal is short
            five_min_close=98.5, next_open=98.5, equity=100000.0,
        )
        assert not result.ok
        assert "opposite_side" in result.reason_no

    def test_risk_cap_rejection(self):
        """Set a tiny risk cap so admission fails."""
        cfg = OrbConfig(or_minutes=30, range_min_pct=0.008,
                        range_max_pct=0.025, skip_vix_above=22.0,
                        skip_earnings_window=False, skip_gap_above_pct=0.0,
                        fail_closed_on_missing_vix=False,
                        max_concurrent_risk_dollars=10.0,  # very low
                        risk_per_trade_pct=2.0)
        eng = OrbEngine(cfg, portfolio_ids=["main"])
        eng.start_new_session(date_iso="2026-01-02", tickers=["AAPL"],
                              vix_close_d1=18.0,
                              ticker_open_today={"AAPL": 100.0},
                              ticker_prev_close={"AAPL": 100.0},
                              equity_per_portfolio={"main": 100000.0})
        for m in range(570, 600):
            eng.on_bar_arrival(ticker="AAPL", bar_high=101.0, bar_low=99.0,
                               bar_open=100.0, bar_close=100.0,
                               bar_volume=10000, bar_bucket_min=m)
        adapter = LiveAdapter(eng, portfolio_id="main")
        result = adapter.check_entry(
            "AAPL", side="long",
            five_min_close=101.5, next_open=101.5, equity=100000.0,
        )
        assert not result.ok
        assert "risk_reject" in result.reason_no


# ------------------ check_exit ------------------


class TestCheckExit:

    def _setup_with_open_long(self):
        cfg, eng = make_engine_with_armed_or()
        adapter = LiveAdapter(eng, portfolio_id="main")
        result = adapter.check_entry(
            "AAPL", side="long",
            five_min_close=101.5, next_open=101.5, equity=100000.0,
        )
        return cfg, eng, adapter, result.ticket_id

    def test_no_exit_in_range(self):
        _cfg, _eng, adapter, ticket = self._setup_with_open_long()
        ex = adapter.check_exit("AAPL", ticket,
                                bar_high=102.0, bar_low=101.0, bar_close=101.5,
                                bar_bucket_min=605)
        assert not ex.exit

    def test_target_exit(self):
        _cfg, _eng, adapter, ticket = self._setup_with_open_long()
        # entry $101.5; stop = OR_low * (1 - 5bp) = 99.0 * 0.9995 = 98.9505
        # risk = 101.5 - 98.9505 = 2.5495
        # target = 101.5 + 2.5 * 2.5495 = 107.87
        ex = adapter.check_exit("AAPL", ticket,
                                bar_high=110.0, bar_low=104.0, bar_close=108.0,
                                bar_bucket_min=605)
        assert ex.exit
        assert ex.reason == "target"
        # Position should be removed from adapter map
        assert adapter.open_position_count() == 0

    def test_stop_exit(self):
        _cfg, _eng, adapter, ticket = self._setup_with_open_long()
        ex = adapter.check_exit("AAPL", ticket,
                                bar_high=101.0, bar_low=98.0, bar_close=98.5,
                                bar_bucket_min=605)
        assert ex.exit
        assert ex.reason == "stop"

    def test_unknown_ticket_returns_no_exit(self):
        _cfg, _eng, adapter, _ticket = self._setup_with_open_long()
        ex = adapter.check_exit("AAPL", "fake_ticket",
                                bar_high=110.0, bar_low=104.0, bar_close=108.0,
                                bar_bucket_min=605)
        assert not ex.exit
        assert ex.reason == "unknown_position"


# ------------------ session reset ------------------


class TestSessionReset:

    def test_reset_clears_open_positions(self):
        _cfg, _eng = make_engine_with_armed_or()
        adapter = LiveAdapter(_eng, portfolio_id="main")
        adapter.check_entry("AAPL", side="long", five_min_close=101.5,
                            next_open=101.5, equity=100000.0)
        assert adapter.open_position_count() == 1
        adapter.reset_session()
        assert adapter.open_position_count() == 0


# ------------------ multi-portfolio ------------------


class TestMultiPortfolioAdapter:

    def test_independent_per_portfolio(self):
        _cfg, eng = make_engine_with_armed_or(
            portfolio_ids=["main", "val", "gene"],
            equity_per_portfolio={"main": 100000.0, "val": 50000.0, "gene": 25000.0},
        )
        a_main = LiveAdapter(eng, "main")
        a_val = LiveAdapter(eng, "val")
        a_gene = LiveAdapter(eng, "gene")
        # Each portfolio takes its own entry on AAPL
        r_main = a_main.check_entry("AAPL", side="long", five_min_close=101.5,
                                     next_open=101.5, equity=100000.0)
        r_val = a_val.check_entry("AAPL", side="long", five_min_close=101.5,
                                   next_open=101.5, equity=50000.0)
        r_gene = a_gene.check_entry("AAPL", side="long", five_min_close=101.5,
                                     next_open=101.5, equity=25000.0)
        assert r_main.ok
        assert r_val.ok
        assert r_gene.ok
        # Each adapter tracks its own position
        assert a_main.open_position_count() == 1
        assert a_val.open_position_count() == 1
        assert a_gene.open_position_count() == 1
        # Different ticket ids
        assert r_main.ticket_id != r_val.ticket_id
        assert r_val.ticket_id != r_gene.ticket_id


class TestRegistry:

    def test_registry_one_adapter_per_portfolio(self):
        _cfg, eng = make_engine_with_armed_or(
            portfolio_ids=["main", "val", "gene"],
            equity_per_portfolio={"main": 100000.0, "val": 50000.0, "gene": 25000.0},
        )
        reg = LiveAdapterRegistry(eng)
        assert set(reg.all_ids()) == {"main", "val", "gene"}
        assert reg.get("main") is not reg.get("val")
        assert reg.get("missing") is None

    def test_registry_reset_all_sessions(self):
        _cfg, eng = make_engine_with_armed_or(
            portfolio_ids=["main", "val"],
            equity_per_portfolio={"main": 100000.0, "val": 50000.0},
        )
        reg = LiveAdapterRegistry(eng)
        reg.get("main").check_entry("AAPL", side="long",
                                      five_min_close=101.5, next_open=101.5,
                                      equity=100000.0)
        reg.get("val").check_entry("AAPL", side="long",
                                     five_min_close=101.5, next_open=101.5,
                                     equity=50000.0)
        assert reg.get("main").open_position_count() == 1
        assert reg.get("val").open_position_count() == 1
        reg.reset_all_sessions()
        assert reg.get("main").open_position_count() == 0
        assert reg.get("val").open_position_count() == 0
