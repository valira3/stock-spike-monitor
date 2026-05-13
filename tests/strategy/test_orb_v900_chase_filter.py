"""v9.0.0 -- chase-prevention filter tests.

Covers the two new pre-admission filters in `engine.OrbEngine.try_enter`:

  1. min_break_bps (ORB_MIN_BREAK_BPS): reject when the signal-bar
     close is too close to the OR boundary (weak breakout).
  2. max_vwap_dev_bps (ORB_MAX_VWAP_DEV_BPS) + per-ticker fence
     (ORB_MAX_VWAP_DEV_TICKERS): reject when entry has already moved
     too far past session VWAP in the breakout direction.

Default-ON behavior at v9: min_break_bps=5, max_vwap_dev_bps=25,
fence=(META, MSFT, AAPL, AMZN, GOOG, AVGO). Tests override the
config to isolate each filter.
"""

import pytest

from orb.engine import OrbConfig, OrbEngine


def _config(*, min_break_bps=0.0, max_vwap_dev_bps=0.0, max_vwap_dev_tickers=()):
    return OrbConfig(
        or_minutes=30,
        rr=2.5,
        stop_buffer_bps=5.0,
        range_min_pct=0.001,
        range_max_pct=0.5,
        max_trades_per_day=5,
        risk_per_trade_pct=1.0,
        max_concurrent_risk_dollars=10_000.0,
        max_concurrent_notional_mult=10.0,
        max_trade_notional_pct=200.0,
        daily_loss_kill_pct=10.0,
        ticker_side_blocklist={},
        skip_vix_above=0.0,
        skip_earnings_window=False,
        skip_gap_above_pct=0.0,
        skip_prior_spy_ret_lt_bps=0.0,  # disable SPY gate in these tests
        atr_stop_mult=0.0,  # OR-edge stops to keep math simple
        partial_profit_at_1r=False,
        min_break_bps=min_break_bps,
        max_vwap_dev_bps=max_vwap_dev_bps,
        max_vwap_dev_tickers=max_vwap_dev_tickers,
    )


def _eng_with_locked_or(cfg, *, ticker="AAPL", or_low=99.5, or_high=100.5):
    eng = OrbEngine(cfg, portfolio_ids=["main"])
    eng.start_new_session(
        date_iso="2026-01-15",
        tickers=[ticker],
        vix_close_d1=18.0,
        ticker_open_today={ticker: 100.0},
        ticker_prev_close={ticker: 100.0},
        equity_per_portfolio={"main": 100_000.0},
    )
    w = eng._state.get_or_window(ticker, 30)
    for bucket in range(570, 600):
        w.add_bar(
            bar_high=or_high,
            bar_low=or_low,
            bar_open=(or_low + or_high) / 2,
            bar_close=(or_low + or_high) / 2,
            bar_volume=1000.0,
            bar_bucket_min=bucket,
            or_end_min=600,
        )
    w.lock(locked_at_iso="test")
    eng._lock_and_arm(ticker, w)
    return eng


# ------------------ min_break_bps tests ------------------------------


class TestMinBreakBps:
    def test_disabled_admits_marginal_break(self):
        # mbr=0: filter inactive.
        cfg = _config(min_break_bps=0.0)
        eng = _eng_with_locked_or(cfg)
        sig = eng.detect_breakout(
            portfolio_id="main",
            ticker="AAPL",
            five_min_close=100.51,
            five_min_close_iso="t",
            next_open=100.55,
        )
        assert sig is not None
        adm = eng.try_enter(sig, equity=100_000.0)
        assert adm is not None
        assert eng._mbr_reject_count == 0

    def test_rejects_weak_break_long(self):
        # Threshold 10bps. 100.51 above 100.5 = ~1bps -> reject.
        cfg = _config(min_break_bps=10.0)
        eng = _eng_with_locked_or(cfg)
        sig = eng.detect_breakout(
            portfolio_id="main",
            ticker="AAPL",
            five_min_close=100.51,
            five_min_close_iso="t",
            next_open=100.55,
        )
        assert sig is not None
        adm = eng.try_enter(sig, equity=100_000.0)
        assert adm is None
        assert eng._mbr_reject_count == 1

    def test_accepts_strong_break_long(self):
        # Threshold 10bps. 100.6 above 100.5 = ~10bps -> at boundary;
        # 100.7 above 100.5 = ~20bps -> accept.
        cfg = _config(min_break_bps=10.0)
        eng = _eng_with_locked_or(cfg)
        sig = eng.detect_breakout(
            portfolio_id="main",
            ticker="AAPL",
            five_min_close=100.71,
            five_min_close_iso="t",
            next_open=100.75,
        )
        assert sig is not None
        adm = eng.try_enter(sig, equity=100_000.0)
        assert adm is not None
        assert eng._mbr_reject_count == 0

    def test_rejects_weak_break_short(self):
        cfg = _config(min_break_bps=10.0)
        eng = _eng_with_locked_or(cfg)
        sig = eng.detect_breakout(
            portfolio_id="main",
            ticker="AAPL",
            five_min_close=99.49,
            five_min_close_iso="t",
            next_open=99.45,
        )
        assert sig is not None
        assert sig.side == "short"
        adm = eng.try_enter(sig, equity=100_000.0)
        assert adm is None
        assert eng._mbr_reject_count == 1

    def test_counter_resets_on_new_session(self):
        cfg = _config(min_break_bps=10.0)
        eng = _eng_with_locked_or(cfg)
        sig = eng.detect_breakout(
            portfolio_id="main",
            ticker="AAPL",
            five_min_close=100.51,
            five_min_close_iso="t",
            next_open=100.55,
        )
        eng.try_enter(sig, equity=100_000.0)
        assert eng._mbr_reject_count == 1
        eng.start_new_session(
            date_iso="2026-01-16",
            tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100_000.0},
        )
        assert eng._mbr_reject_count == 0


# ------------------ max_vwap_dev_bps tests ---------------------------


class TestMaxVwapDevBps:
    def test_disabled_admits_chase_entry(self):
        cfg = _config(max_vwap_dev_bps=0.0)
        eng = _eng_with_locked_or(cfg)
        sig = eng.detect_breakout(
            portfolio_id="main",
            ticker="AAPL",
            five_min_close=101.0,
            five_min_close_iso="t",
            next_open=101.5,
        )
        adm = eng.try_enter(sig, equity=100_000.0, session_vwap=100.0)
        assert adm is not None
        assert eng._vwap_chase_reject_count == 0

    def test_rejects_long_far_above_vwap_global(self):
        # No fence -> filter applies globally.
        cfg = _config(max_vwap_dev_bps=25.0)
        eng = _eng_with_locked_or(cfg)
        sig = eng.detect_breakout(
            portfolio_id="main",
            ticker="AAPL",
            five_min_close=101.0,
            five_min_close_iso="t",
            next_open=101.5,
        )
        # entry 101.5 vs vwap 100.0 = +150bps -> reject (> 25bps).
        adm = eng.try_enter(sig, equity=100_000.0, session_vwap=100.0)
        assert adm is None
        assert eng._vwap_chase_reject_count == 1

    def test_accepts_long_near_vwap(self):
        cfg = _config(max_vwap_dev_bps=25.0)
        eng = _eng_with_locked_or(cfg)
        sig = eng.detect_breakout(
            portfolio_id="main",
            ticker="AAPL",
            five_min_close=100.6,
            five_min_close_iso="t",
            next_open=100.7,
        )
        # entry 100.7 vs vwap 100.5 = ~20bps -> pass.
        adm = eng.try_enter(sig, equity=100_000.0, session_vwap=100.5)
        assert adm is not None
        assert eng._vwap_chase_reject_count == 0

    def test_rejects_short_far_below_vwap(self):
        cfg = _config(max_vwap_dev_bps=25.0)
        eng = _eng_with_locked_or(cfg)
        sig = eng.detect_breakout(
            portfolio_id="main",
            ticker="AAPL",
            five_min_close=99.0,
            five_min_close_iso="t",
            next_open=98.5,
        )
        assert sig.side == "short"
        # vwap=100, entry=98.5 -> dev = (100-98.5)/100*10000 = +150bps -> reject.
        adm = eng.try_enter(sig, equity=100_000.0, session_vwap=100.0)
        assert adm is None
        assert eng._vwap_chase_reject_count == 1

    def test_fence_skips_non_listed_ticker(self):
        # Fence to MSFT only. AAPL signal should pass even with chase.
        cfg = _config(max_vwap_dev_bps=25.0, max_vwap_dev_tickers=("MSFT",))
        eng = _eng_with_locked_or(cfg, ticker="AAPL")
        sig = eng.detect_breakout(
            portfolio_id="main",
            ticker="AAPL",
            five_min_close=101.0,
            five_min_close_iso="t",
            next_open=101.5,
        )
        adm = eng.try_enter(sig, equity=100_000.0, session_vwap=100.0)
        assert adm is not None
        assert eng._vwap_chase_reject_count == 0

    def test_fence_applies_to_listed_ticker(self):
        cfg = _config(max_vwap_dev_bps=25.0, max_vwap_dev_tickers=("AAPL",))
        eng = _eng_with_locked_or(cfg, ticker="AAPL")
        sig = eng.detect_breakout(
            portfolio_id="main",
            ticker="AAPL",
            five_min_close=101.0,
            five_min_close_iso="t",
            next_open=101.5,
        )
        adm = eng.try_enter(sig, equity=100_000.0, session_vwap=100.0)
        assert adm is None
        assert eng._vwap_chase_reject_count == 1

    def test_missing_vwap_fails_open(self):
        # session_vwap=None: filter is bypassed (fail-open).
        cfg = _config(max_vwap_dev_bps=25.0)
        eng = _eng_with_locked_or(cfg)
        sig = eng.detect_breakout(
            portfolio_id="main",
            ticker="AAPL",
            five_min_close=101.0,
            five_min_close_iso="t",
            next_open=101.5,
        )
        adm = eng.try_enter(sig, equity=100_000.0, session_vwap=None)
        assert adm is not None
        assert eng._vwap_chase_reject_count == 0

    def test_zero_vwap_fails_open(self):
        cfg = _config(max_vwap_dev_bps=25.0)
        eng = _eng_with_locked_or(cfg)
        sig = eng.detect_breakout(
            portfolio_id="main",
            ticker="AAPL",
            five_min_close=101.0,
            five_min_close_iso="t",
            next_open=101.5,
        )
        adm = eng.try_enter(sig, equity=100_000.0, session_vwap=0.0)
        assert adm is not None


# ------------------ snapshot exposure tests --------------------------


class TestSnapshotExposes:
    def test_config_fields_in_snapshot(self):
        cfg = _config(
            min_break_bps=5.0, max_vwap_dev_bps=25.0, max_vwap_dev_tickers=("AAPL", "MSFT")
        )
        eng = OrbEngine(cfg, portfolio_ids=["main"])
        snap = eng.snapshot()
        assert snap["config"]["min_break_bps"] == 5.0
        assert snap["config"]["max_vwap_dev_bps"] == 25.0
        assert snap["config"]["max_vwap_dev_tickers"] == ["AAPL", "MSFT"]
        assert "skip_prior_spy_ret_lt_bps" in snap["config"]

    def test_reject_counters_in_snapshot(self):
        cfg = _config(min_break_bps=10.0)
        eng = _eng_with_locked_or(cfg)
        sig = eng.detect_breakout(
            portfolio_id="main",
            ticker="AAPL",
            five_min_close=100.51,
            five_min_close_iso="t",
            next_open=100.55,
        )
        eng.try_enter(sig, equity=100_000.0)
        snap = eng.snapshot()
        assert snap["mbr_reject_count"] == 1
        assert snap["vwap_chase_reject_count"] == 0
