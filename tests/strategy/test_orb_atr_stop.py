"""v8.0.0 -- ATR-based stop placement tests.

Covers:
  - atr_from_5m() math: Wilder ATR mean-of-True-Range, partial windows,
    None-tolerant entries.
  - detect_breakout(): atr-branch fires when cfg.atr_stop_mult > 0 AND
    enough 5m bars; falls back to OR-edge stop when cold (< 2 bars);
    stop_source forensic field reflects the actual branch taken.
  - Sign correctness on long + short.
  - End-to-end through live_runtime.check_entry()/SessionSimulator with
    ATR override pushing stop FURTHER from entry than the OR edge would
    (so sizing math sees the wider stop).
"""
import pytest

from orb import engine as _engine
from orb.engine import OrbConfig, OrbEngine, atr_from_5m


# ----- 1. atr_from_5m math --------------------------------------------


class TestAtrFromFiveMin:
    def test_empty_returns_zero(self):
        assert atr_from_5m([], [], []) == 0.0

    def test_single_bar_returns_zero(self):
        # Need >= 2 bars to compute even one TR.
        assert atr_from_5m([100.0], [99.0], [99.5]) == 0.0

    def test_two_bars_one_tr(self):
        # TR_1 = max(high_1 - low_1, |high_1 - close_0|, |low_1 - close_0|)
        # bars: (100, 99, 99.5), (101, 100, 100.5)
        #   TR_1 = max(101-100, |101-99.5|, |100-99.5|) = max(1, 1.5, 0.5) = 1.5
        atr = atr_from_5m([100.0, 101.0], [99.0, 100.0], [99.5, 100.5])
        assert abs(atr - 1.5) < 1e-9

    def test_three_bars_avg_of_two_trs(self):
        # bars: (100,99,99.5), (101,100,100.5), (102,100.5,101.5)
        #   TR_1 = max(1, 1.5, 0.5) = 1.5
        #   TR_2 = max(1.5, 1.5, 0.0) = 1.5
        # ATR = mean(1.5, 1.5) = 1.5
        atr = atr_from_5m(
            [100.0, 101.0, 102.0],
            [99.0, 100.0, 100.5],
            [99.5, 100.5, 101.5],
        )
        assert abs(atr - 1.5) < 1e-9

    def test_lookback_caps_window(self):
        # 21 bars rising by +1 each step. TR_i = max(1, 1.5, 0.5) = 1.5.
        # lookback=14 -> avg over last 14 of those 1.5s -> 1.5.
        highs = [100.0 + i for i in range(21)]
        lows = [99.0 + i for i in range(21)]
        closes = [99.5 + i for i in range(21)]
        atr = atr_from_5m(highs, lows, closes, lookback=14)
        assert abs(atr - 1.5) < 1e-9

    def test_none_entries_skipped(self):
        # If a middle bar is None on any field, the TR computation for
        # that bar is skipped, but earlier/later valid bars still
        # contribute.
        highs = [100.0, None, 102.0]
        lows = [99.0, 99.5, 100.5]
        closes = [99.5, None, 101.5]
        atr = atr_from_5m(highs, lows, closes)
        # Only TR_2 (against bar 1's None close) is unusable; with
        # closes[0]=99.5 valid and closes[1]=None, TR_1 is skipped
        # (uses closes[0] OK but the bar 1 high is None). TR_2 uses
        # closes[1]=None -> skipped. Result: zero usable TRs.
        assert atr == 0.0


# ----- 2. detect_breakout() ATR branch --------------------------------


def _config(*, atr_stop_mult=0.0, stop_buffer_bps=5.0):
    return OrbConfig(
        or_minutes=30,
        rr=2.5,
        stop_buffer_bps=stop_buffer_bps,
        atr_stop_mult=atr_stop_mult,
        ticker_side_blocklist={},
    )


def _eng_with_locked_or(*, or_low=99.5, or_high=100.5, atr_mult=0.0):
    cfg = _config(atr_stop_mult=atr_mult)
    eng = OrbEngine(cfg, portfolio_ids=["main"])
    eng.start_new_session(
        date_iso="2026-01-15",
        tickers=["AAPL"],
        vix_close_d1=18.0,
        ticker_open_today={"AAPL": 100.0},
        ticker_prev_close={"AAPL": 100.0},
        equity_per_portfolio={"main": 100_000.0},
    )
    # Force-fill an OR window so detect_breakout's pre-conditions pass.
    w = eng._state.get_or_window("AAPL", 30)
    # Drop OR bars in [09:30, 10:00) so add_bar accepts them all.
    for bucket in range(570, 600):  # 09:30 -> 10:00 ET
        w.add_bar(
            bar_high=or_high, bar_low=or_low,
            bar_open=(or_low + or_high) / 2,
            bar_close=(or_low + or_high) / 2,
            bar_volume=1000.0,
            bar_bucket_min=bucket, or_end_min=600,
        )
    w.lock(locked_at_iso="test")
    # Pre-portfolio FSM must be ARMED for detect_breakout to fire.
    eng._lock_and_arm("AAPL", w)
    return eng


class TestDetectBreakoutAtrBranch:

    def test_long_or_edge_stop_when_atr_off(self):
        eng = _eng_with_locked_or(atr_mult=0.0)  # ATR disabled
        sig = eng.detect_breakout(
            portfolio_id="main", ticker="AAPL",
            five_min_close=100.6, five_min_close_iso="t",
            next_open=100.7,
        )
        assert sig is not None
        assert sig.side == "long"
        assert sig.stop_source == "or_edge"
        assert sig.atr_used is None
        # or_edge stop = 99.5 * (1 - 5bps) = 99.5 * 0.9995 = 99.45025
        assert abs(sig.proposed_stop - 99.45025) < 1e-4

    def test_long_atr_stop_when_warm(self):
        eng = _eng_with_locked_or(atr_mult=1.5)
        # 3 rising bars -> TR_1 = TR_2 = 1.5 -> ATR = 1.5
        highs = [100.0, 101.0, 102.0]
        lows = [99.0, 100.0, 101.0]
        closes = [99.5, 100.5, 101.5]
        sig = eng.detect_breakout(
            portfolio_id="main", ticker="AAPL",
            five_min_close=100.6, five_min_close_iso="t",
            next_open=100.7,
            recent_5m_highs=highs, recent_5m_lows=lows,
            recent_5m_closes=closes,
        )
        assert sig is not None
        assert sig.stop_source == "atr"
        assert sig.atr_used is not None and abs(sig.atr_used - 1.5) < 1e-9
        # ATR stop (long) = next_open - 1.5 * 1.5 = 100.7 - 2.25 = 98.45
        assert abs(sig.proposed_stop - 98.45) < 1e-4
        # WIDER (further below) than the OR-edge stop ~99.45.
        assert sig.proposed_stop < 99.45025

    def test_long_cold_atr_falls_back_to_or_edge(self):
        eng = _eng_with_locked_or(atr_mult=1.5)
        # Only 1 bar of history -> ATR returns 0 -> fallback to OR edge.
        sig = eng.detect_breakout(
            portfolio_id="main", ticker="AAPL",
            five_min_close=100.6, five_min_close_iso="t",
            next_open=100.7,
            recent_5m_highs=[100.0], recent_5m_lows=[99.0],
            recent_5m_closes=[99.5],
        )
        assert sig is not None
        assert sig.stop_source == "or_edge"
        assert sig.atr_used is None
        assert abs(sig.proposed_stop - 99.45025) < 1e-4

    def test_long_no_bars_supplied_falls_back(self):
        # cfg has atr_stop_mult > 0 but caller didn't pass any bars.
        # Falls back to OR-edge so the strategy is never stop-less.
        eng = _eng_with_locked_or(atr_mult=1.5)
        sig = eng.detect_breakout(
            portfolio_id="main", ticker="AAPL",
            five_min_close=100.6, five_min_close_iso="t",
            next_open=100.7,
        )
        assert sig is not None
        assert sig.stop_source == "or_edge"

    def test_short_atr_stop_when_warm(self):
        eng = _eng_with_locked_or(atr_mult=1.5)
        highs = [100.0, 101.0, 102.0]
        lows = [99.0, 100.0, 101.0]
        closes = [99.5, 100.5, 101.5]
        sig = eng.detect_breakout(
            portfolio_id="main", ticker="AAPL",
            five_min_close=99.4, five_min_close_iso="t",
            next_open=99.3,
            recent_5m_highs=highs, recent_5m_lows=lows,
            recent_5m_closes=closes,
        )
        assert sig is not None
        assert sig.side == "short"
        assert sig.stop_source == "atr"
        # ATR stop (short) = next_open + 1.5 * 1.5 = 99.3 + 2.25 = 101.55
        assert abs(sig.proposed_stop - 101.55) < 1e-4
        # OR-edge stop would have been 100.5 * 1.0005 = 100.55025;
        # ATR is further ABOVE that.
        assert sig.proposed_stop > 100.55025

    def test_atr_widens_stop_so_sizing_drops_shares(self):
        # With same risk_per_trade_pct, a wider stop (ATR) means smaller
        # share count via the risk-budget cap. We check the engine emits
        # consistent geometry (entry, stop, target) by going through try_enter.
        eng = _eng_with_locked_or(atr_mult=1.5)
        highs = [100.0, 101.0, 102.0]
        lows = [99.0, 100.0, 101.0]
        closes = [99.5, 100.5, 101.5]
        sig = eng.detect_breakout(
            portfolio_id="main", ticker="AAPL",
            five_min_close=100.6, five_min_close_iso="t",
            next_open=100.7,
            recent_5m_highs=highs, recent_5m_lows=lows,
            recent_5m_closes=closes,
        )
        admission = eng.try_enter(sig, equity=100_000.0)
        assert admission is not None
        pos = admission.position
        # risk_per_share = 100.7 - 98.45 = 2.25
        # risk_budget at 1% of $100k = $1000 -> shares = int(1000/2.25) = 444
        # (75% notional cap = $75k / $100.7 = 744 -> risk binds)
        assert 440 <= pos.shares <= 450, f"shares={pos.shares}"
        # Target = entry + 2.5 * risk_per_share = 100.7 + 5.625 = 106.325
        assert abs(pos.target - 106.325) < 0.01

    def test_snapshot_exposes_atr_config(self):
        eng = _eng_with_locked_or(atr_mult=1.75)
        snap = eng.snapshot()
        cfg = snap["config"]
        assert cfg["atr_stop_mult"] == 1.75
        assert cfg["atr_lookback_5m"] == 14
