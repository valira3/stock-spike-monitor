"""Classical 15-minute Opening Range Breakout backtest.

Standalone -- imports nothing from trade_genius / engine / broker.
Reads data/<YYYY-MM-DD>/<TICKER>.jsonl (1-min bars) and outputs the
same summary.json + per_day/*.json schema as tools/lever_sweep_runner.py
so the manager and stability analyzer can rank it alongside the v15
spec variants.

Strategy (programmable, no discretion):

  1. Define the Opening Range from the first ORB_OR_MINUTES (default 15)
     of the regular session. OR_HIGH = max(high) and OR_LOW = min(low)
     across that window.

  2. After the OR window closes, scan for 5-min candle closes above
     OR_HIGH (long) or below OR_LOW (short). On signal, enter on the
     NEXT 5-min candle's open price (with slippage).

  3. Filters:
       - Range filter:    ORB_RANGE_MIN_PCT <= (OR_HIGH-OR_LOW)/midprice <= ORB_RANGE_MAX_PCT
       - Time cutoff:     no new entries after ORB_TIME_CUTOFF_ET (default 12:00)
       - Per-day cap:     at most ORB_MAX_TRADES_PER_DAY entries per ticker
                          (default 1; "first signal of the day" semantics)

  4. Risk management:
       - Stop: opposite side of OR with ORB_STOP_BUFFER_BPS slippage adder
       - Target: 1 : ORB_RR risk-reward
       - EOD flush: force close at ORB_EOD_CUTOFF_ET (default 15:55 ET)
       - Position sizing: risk ORB_RISK_PER_TRADE_PCT of ORB_ACCOUNT per trade

  5. Slippage model (matches v15 harness): entry adverse-bps + exit
     adverse-bps + stop-kick on stop-trigger exits + short-side penalty.

Usage:
    ORB_OR_MINUTES=15 ORB_RR=1.5 python tools/orb_backtest.py \
        --corpus data --out /tmp/orb_run --tickers AAPL,MSFT,...

Config env vars (all optional):
    ORB_OR_MINUTES         15
    ORB_RR                 1.5
    ORB_STOP_BUFFER_BPS    5.0
    ORB_TIME_CUTOFF_ET     12:00
    ORB_EOD_CUTOFF_ET      15:55
    ORB_RANGE_MIN_PCT      0.005
    ORB_RANGE_MAX_PCT      0.015
    ORB_VOLUME_MULT        0     (disabled; >1 enables vol filter)
    ORB_MAX_TRADES_PER_DAY 1
    ORB_RISK_PER_TRADE_PCT 1.0
    ORB_ACCOUNT            100000
    ORB_TICKER_SIDE_BLOCKLIST '{"ORCL":["LONG"],...}'  (same schema as prod)
    ORB_ENTRY_SLIPPAGE_BPS 1.5
    ORB_EXIT_SLIPPAGE_BPS  1.5
    ORB_STOP_KICK_BPS      5.0
    ORB_SHORT_PENALTY_BPS  1.0
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field, replace as dataclass_replace
from pathlib import Path
from typing import Iterator

# v15 earnings-window blackout
try:
    from tools.orb_earnings_calendar import is_earnings_window
except ImportError:
    # When run as a script with cwd=repo root, sibling import works:
    try:
        from orb_earnings_calendar import is_earnings_window  # type: ignore
    except ImportError:
        def is_earnings_window(*a, **k):  # fallback no-op
            return False

# v16 VIX gate
try:
    from tools.orb_vix_loader import load_vix_closes, vix_close_for
except ImportError:
    try:
        from orb_vix_loader import load_vix_closes, vix_close_for  # type: ignore
    except ImportError:
        def load_vix_closes(*a, **k):  # fallback empty
            return {}
        def vix_close_for(*a, **k):
            return None


# ---------- env knobs ----------
def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _envi(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _envs(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _et_to_minutes(s: str) -> int:
    """Parse "HH:MM" (ET) to minutes-from-midnight."""
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _bucket_to_minutes(b: str) -> int:
    """et_bucket is "HHMM" string. Convert to minutes-from-midnight."""
    if len(b) == 4:
        return int(b[:2]) * 60 + int(b[2:])
    return -1


def _ts_to_et_bucket_minutes(ts_str: str) -> int:
    """Convert an ISO UTC timestamp to ET minutes-from-midnight.

    Why: the workflow that fetched some ticker series (pull-rth-bars.yml)
    used a hardcoded UTC-5 offset which produces WRONG et_bucket values
    on DST dates (March 9 - November 1). This helper re-derives the bucket
    from the ts field with proper US/Eastern timezone handling, so we
    don't rely on a possibly-stale et_bucket string. Returns -1 on parse
    failure.
    """
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        et = ts.astimezone(ZoneInfo("US/Eastern"))
        return et.hour * 60 + et.minute
    except Exception:
        return -1


# ---------- bar loading ----------
@dataclass
class Bar1m:
    bucket: int          # minutes-from-midnight ET
    open: float
    high: float
    low: float
    close: float
    volume: float        # total_volume preferred, fallback iex_volume


def load_day_bars(corpus_dir: Path, date: str, ticker: str) -> list[Bar1m]:
    fp = corpus_dir / date / f"{ticker}.jsonl"
    if not fp.is_file():
        return []
    out: list[Bar1m] = []
    try:
        for line in fp.read_text().splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            # Prefer DST-aware bucket re-derived from ts; fall back to
            # the saved et_bucket only if ts is missing/unparseable.
            # The pull-rth-bars fetcher had a DST bug (hardcoded UTC-5);
            # using ts here normalizes both old and new ticker series.
            ts_str = d.get("ts", "")
            bkt = _ts_to_et_bucket_minutes(ts_str) if ts_str else -1
            if bkt < 0:
                bkt = _bucket_to_minutes(d.get("et_bucket", ""))
            if bkt < 0:
                continue
            o = d.get("open"); h = d.get("high"); l = d.get("low"); c = d.get("close")
            if o is None or h is None or l is None or c is None:
                continue
            v = d.get("total_volume") or d.get("iex_volume") or 0
            try:
                out.append(Bar1m(bkt, float(o), float(h), float(l),
                                 float(c), float(v or 0)))
            except (TypeError, ValueError):
                continue
    except OSError:
        return []
    out.sort(key=lambda b: b.bucket)
    return out


def aggregate_5m(bars_1m: list[Bar1m]) -> list[Bar1m]:
    """Aggregate 1-min bars into 5-min candles, anchored at 5-min boundaries
    (e.g. 09:30, 09:35, 09:40, ...). Returns synthetic 5-min OHLCV bars."""
    by_bucket: dict[int, list[Bar1m]] = defaultdict(list)
    for b in bars_1m:
        anchor = (b.bucket // 5) * 5
        by_bucket[anchor].append(b)
    out: list[Bar1m] = []
    for anchor in sorted(by_bucket):
        bars = by_bucket[anchor]
        if not bars:
            continue
        out.append(Bar1m(
            bucket=anchor,
            open=bars[0].open,
            high=max(b.high for b in bars),
            low=min(b.low for b in bars),
            close=bars[-1].close,
            volume=sum(b.volume for b in bars),
        ))
    return out


# ---------- strategy config ----------
@dataclass
class ORBConfig:
    or_minutes: int = 15
    rr: float = 1.5
    stop_buffer_bps: float = 5.0
    time_cutoff_et: int = field(default_factory=lambda: _et_to_minutes("12:00"))
    eod_cutoff_et: int = field(default_factory=lambda: _et_to_minutes("15:55"))
    range_min_pct: float = 0.005
    range_max_pct: float = 0.015
    volume_mult: float = 0.0
    max_trades_per_day: int = 1
    risk_per_trade_pct: float = 1.0
    account: float = 100_000.0
    blocklist: dict = field(default_factory=dict)
    entry_slippage_bps: float = 5.0   # v8 realism: was 1.5; ORB-time fills wider
    exit_slippage_bps: float = 5.0    # v8 realism: was 1.5
    stop_kick_bps: float = 5.0
    short_pen_bps: float = 1.0
    # v8 realism caps -- prevent phantom leverage / unrealistic concurrent
    # notional. Audit found 25x leverage on a $100k account; these cap it.
    max_trade_notional_pct: float = 25.0    # one trade caps at 25% account
    max_concurrent_notional_mult: float = 2.0  # all-trades concurrent <= 2x acct
    # v9 risk-budget caps -- user constraint: max daily loss = $500
    # (= 0.5% of $100k). Both caps below default to $500.
    max_concurrent_risk_dollars: float = 500.0  # sum of open risk_dollars cap
    daily_loss_kill_pct: float = 0.5  # halt new entries after -0.5% intraday
    risk_per_trade_pct_default = 0.25  # see env override below
    # v9 levers (un-tested, hypothesis-driven)
    move_to_be_after_1r: bool = False    # bump stop to entry after 1R reached
    partial_profit_at_1r: bool = False   # take half off at 1R, ride rest to RR
    require_volume_confirm: bool = False # skip signals where signal-bar vol < 1x mean
    volume_confirm_mult: float = 1.0     # multiplier on prior-day-mean vol for confirm
    require_ema_align: bool = False      # only long if signal close > 200-EMA on 5m
    # v10 industry-standard levers
    atr_stop_mult: float = 0.0           # if > 0, use entry +- atr_stop_mult * atr instead of OR-low
    require_adx_above: float = 0.0       # if > 0, skip if 14-bar ADX on 5m < this threshold
    skip_gap_pct: float = 0.0            # if > 0, skip days where |open-prev_close|/prev_close > this
    require_vwap_align: bool = False     # only long > session VWAP, short < session VWAP
    skip_first_5min: bool = False        # only fire on candles 09:50+ (skip 09:45 candle)
    trailing_stop_pct: float = 0.0       # if > 0, after BE trail by trailing_stop_pct * (entry-stop) behind extreme
    # v11 daily compounding -- account grows with cumulative P&L day-to-day,
    # and position sizing scales with the latest balance. Risk caps + per-
    # trade notional caps also scale with account. When OFF (default for
    # backwards compatibility), each day uses the static account value.
    compound_daily: bool = False
    # v12 SPY/QQQ regime gate -- targets the 2025-11 OOS failure where
    # mega-cap breakouts got faded under a choppy/down-trending index regime.
    # When set, the index's own 30-min OR direction + magnitude become a
    # day-level filter applied to all per-ticker candidate signals BEFORE
    # the concurrent-risk gate.
    regime_ticker: str = ""              # "SPY" | "QQQ" | "" (off)
    regime_dir_align: bool = False       # only allow trades aligned with index OR direction
    regime_min_or_bps: float = 0.0       # skip day if |index 30m-OR move| < this (bps); 0 = off
    # v13 RVOL gate -- Zarattini, Barbon & Aziz (SSRN 2023, "Beat the Market:
    # An Effective ORB Strategy"). Filters per-ticker per-day signals using
    # the ratio of today's OR-window volume to the same window's 20-day
    # rolling mean. CLEAN look-ahead: OR_volume is a fully-closed window
    # (09:30 to 09:30+OR_minutes) that is known by the time the first entry
    # signal can fire (after OR window closes). Baseline uses prior sessions
    # only.
    require_rvol_above: float = 0.0      # skip ticker on day if rvol < this; 0 = off
    rvol_lookback_days: int = 20         # baseline window in prior sessions
    # v14 prior-day filters -- look-ahead clean (consume only data with
    # timestamp < session start of `date`).
    skip_gap_above_pct: float = 0.0      # skip ticker on day if |today_open - prev_close|/prev_close > this; 0 = off
    require_prior_nr_n: int = 0          # require prior session range = min of last N daily ranges (Crabel NR_N); 0 = off
    skip_prior_wr_n: int = 0             # skip if prior session range = max of last N daily ranges (wide-range exhaustion); 0 = off
    # v15 earnings-window blackout -- skip per-ticker signals when the
    # ticker is within a [-N, +M] day window of its scheduled earnings
    # announcement. CLEAN look-ahead: earnings dates are public schedules
    # known weeks in advance.
    skip_earnings_window: bool = False   # if True, gate on EARNINGS_CALENDAR
    earnings_days_before: int = 1        # days before announcement to skip
    earnings_days_after: int = 0         # days after announcement to skip
    # v16 VIX absolute-level gate -- skip the entire trading day if
    # VIX_close(D-1) > threshold. Source: TOS Indicators, Options.cafe;
    # high-VIX regimes break ORB continuation. CLEAN look-ahead: prior
    # session close is fully observable.
    skip_vix_above: float = 0.0          # 0 = off; e.g. 25 to skip if VIX(D-1) > 25
    vix_csv_path: str = "data/external/vix-daily.csv"
    # v17 vol-targeted sizing -- per Quantpedia: scale risk by inverse of
    # current ATR relative to a target. When ATR is HIGH (volatile), risk
    # less; when LOW (calm), risk more. Equalizes dollar-risk-per-ATR
    # across tickers and across regimes. CLEAN look-ahead: ATR is computed
    # from candles strictly prior to + including the signal bar.
    vol_target_atr_pct: float = 0.0      # target ATR as % of price; 0 = off
    vol_target_min_scale: float = 0.5    # cap downscale (less risk per trade)
    vol_target_max_scale: float = 2.0    # cap upscale (more risk per trade)
    # v18 day-end-giveback defenses (2026-05-12). Two rules, configurable
    # independently. Both default off; tested in r6_drawdown_rules.py and
    # documented in docs/pl_optimization_final_report_v13.md.
    loss_lock_threshold_usd: float = 0.0  # >0: after a closed leg with
                                          #     pnl < -threshold, lock that
                                          #     (ticker, side) pair for the
                                          #     rest of the trading day --
                                          #     no further entries on that
                                          #     pair. 0 = off.
    peak_dd_halt_usd: float = 0.0         # >0: when intraday realized PnL
                                          #     drops this many $ below the
                                          #     running peak, halt all new
                                          #     entries for the rest of the
                                          #     day (same effect as the
                                          #     existing daily_loss_kill).
                                          #     0 = off.
    # v19 signal-magnitude / cadence-latency filter (2026-05-13). Tests the
    # hypothesis that production fires later than the first marginal break
    # because of scan-loop cadence latency. Two independent levers:
    min_break_bps: float = 0.0            # >0: require signal close to be
                                          #     min_break_bps past OR_high
                                          #     (long) or OR_low (short)
                                          #     before admitting. Suppresses
                                          #     marginal breaks like the
                                          #     observed AMZN -4.5bps fire.
    confirm_bars_n: int = 0               # >0: require the prior N 5m closes
                                          #     (including this signal bar)
                                          #     to ALL be past the OR
                                          #     boundary in the same
                                          #     direction. Approximates
                                          #     production firing on the
                                          #     "3rd consecutive bar".
    # v20 chase-prevention filters (2026-05-13). Targets the per-ticker
    # forensic finding that losers chase too far past session VWAP and
    # fire against pre-market drift. Universal levers; default off.
    max_vwap_dev_bps: float = 0.0         # >0: reject if entry price is
                                          #     more than N bps past
                                          #     session VWAP in the
                                          #     breakout direction.
                                          #     Signed: long entries
                                          #     above VWAP and short
                                          #     entries below VWAP count
                                          #     as positive deviation.
    max_vwap_dev_tickers: tuple = ()      # if non-empty, apply
                                          #     max_vwap_dev_bps ONLY to
                                          #     these tickers (per-list
                                          #     fence). Empty = global.
    max_vwap_dev_bps_long: float = 0.0    # if >0, overrides
                                          #     max_vwap_dev_bps for the
                                          #     LONG side. Lets us run
                                          #     asymmetric thresholds
                                          #     (forensic showed long
                                          #     chase-failure is sharper
                                          #     than short).
    max_vwap_dev_bps_short: float = 0.0   # if >0, overrides for SHORT.
    # v21 more fenced filters for mega-caps (2026-05-13).
    confirm_bars_n_tickers: tuple = ()    # fence list for confirm_bars_n.
                                          #     Empty = global (existing
                                          #     behavior). When non-empty,
                                          #     N-bar confirmation only
                                          #     applies to those tickers.
    min_break_bps_tickers: tuple = ()     # fence list for min_break_bps.
                                          #     Empty = global.
    fenced_or_min_pct: float = 0.0        # >0: skip the fenced tickers
                                          #     when OR width is below
                                          #     this threshold.
    fenced_or_max_pct: float = 0.0        # >0: skip the fenced tickers
                                          #     when OR width is above
                                          #     this threshold.
    fenced_or_tickers: tuple = ()         # fence list for the OR-width
                                          #     gate. Empty = no gate.
    fenced_gap_pct: float = 0.0           # >0: tighter gap-skip threshold
                                          #     applied only to
                                          #     fenced_gap_tickers.
    fenced_gap_tickers: tuple = ()        # fence list for tighter gap.
    # v22 regime-conditional day-skip (2026-05-13). Skip the entire
    # trading day when the prior session's SPY close-to-close return is
    # in the [lo, hi] bps band. R12 forensic showed the strategy bleeds
    # most on days after a moderate SPY drop (-1.0% to -0.5%): 24 days
    # in the FY corpus, -$4,988 net.
    skip_prior_spy_ret_lt_bps: float = 0.0  # >0 (or <0): skip if prior
                                            # SPY return is BELOW this
                                            # (in bps). e.g. -50 = skip
                                            # days where prior SPY < -0.5%.
    skip_prior_spy_ret_gt_bps: float = 0.0  # paired upper bound: when
                                            # both _lt and _gt set, skip
                                            # only days IN [lt, gt] band.
                                            # When only _lt set, skip
                                            # everything below _lt.
    regime_low_skip_tickers: tuple = ()     # if non-empty, on regime-low
                                            # days (per skip_prior_spy_ret_*
                                            # thresholds) skip ONLY these
                                            # tickers instead of the whole
                                            # day. Empty = whole-day skip.
                                            # R12c+ feature: keep
                                            # profitable non-T5 trading on
                                            # bad-regime days while
                                            # blocking the specific
                                            # bleeders (TSLA, NFLX, ORCL).
    # R13b conservative-on-bad-day overrides. When set (>0), replace the
    # corresponding base config field on regime-low days only. Combines
    # with regime_low_skip_tickers (each lever independent).
    regime_low_risk_per_trade_pct: float = 0.0  # halve sizing on bad days
    regime_low_atr_stop_mult: float = 0.0       # tighter stops on bad days
    regime_low_max_trades_per_day: int = 0      # cap entries on bad days
    regime_low_max_vwap_dev_bps: float = 0.0    # tighter chase fence on bad days
    regime_low_min_break_bps: float = 0.0       # require bigger break on bad days
    premkt_align_bps: float = 0.0         # >0: require pre-market move
                                          #     (09:00-09:29 ET) of at
                                          #     least N bps in the
                                          #     breakout direction.
                                          #     Filters reversal-style
                                          #     breakouts that fire
                                          #     against premkt drift.

    @classmethod
    def from_env(cls) -> "ORBConfig":
        bl_raw = _envs("ORB_TICKER_SIDE_BLOCKLIST", "")
        try:
            bl = json.loads(bl_raw) if bl_raw.strip() else {}
        except Exception:
            bl = {}
        return cls(
            or_minutes=_envi("ORB_OR_MINUTES", 15),
            rr=_envf("ORB_RR", 1.5),
            stop_buffer_bps=_envf("ORB_STOP_BUFFER_BPS", 5.0),
            time_cutoff_et=_et_to_minutes(_envs("ORB_TIME_CUTOFF_ET", "12:00")),
            eod_cutoff_et=_et_to_minutes(_envs("ORB_EOD_CUTOFF_ET", "15:55")),
            range_min_pct=_envf("ORB_RANGE_MIN_PCT", 0.005),
            range_max_pct=_envf("ORB_RANGE_MAX_PCT", 0.015),
            volume_mult=_envf("ORB_VOLUME_MULT", 0.0),
            max_trades_per_day=_envi("ORB_MAX_TRADES_PER_DAY", 1),
            # v9: risk per trade default tightened from 1.0% to 0.25%
            # ($250 risk on a $100k account) so that a single stop fire
            # is well within the $500/day loss cap.
            risk_per_trade_pct=_envf("ORB_RISK_PER_TRADE_PCT", 0.25),
            account=_envf("ORB_ACCOUNT", 100_000.0),
            blocklist=bl,
            entry_slippage_bps=_envf("ORB_ENTRY_SLIPPAGE_BPS", 5.0),
            exit_slippage_bps=_envf("ORB_EXIT_SLIPPAGE_BPS", 5.0),
            stop_kick_bps=_envf("ORB_STOP_KICK_BPS", 5.0),
            short_pen_bps=_envf("ORB_SHORT_PENALTY_BPS", 1.0),
            max_trade_notional_pct=_envf("ORB_MAX_TRADE_NOTIONAL_PCT", 25.0),
            max_concurrent_notional_mult=_envf(
                "ORB_MAX_CONCURRENT_NOTIONAL_MULT", 2.0),
            # v9 risk budget: total open risk_dollars must stay <= this
            # cap. With $500 default and $250/trade risk, max 2 open
            # positions can stop simultaneously (= $500 worst case).
            max_concurrent_risk_dollars=_envf(
                "ORB_MAX_CONCURRENT_RISK_DOLLARS", 500.0),
            # v9: daily loss kill tightened from 5.0% to 0.5% to match
            # user constraint of $500/day max loss.
            daily_loss_kill_pct=_envf("ORB_DAILY_LOSS_KILL_PCT", 0.5),
            move_to_be_after_1r=_envs("ORB_MOVE_TO_BE_AFTER_1R", "0") == "1",
            partial_profit_at_1r=_envs("ORB_PARTIAL_PROFIT_AT_1R", "0") == "1",
            require_volume_confirm=_envs("ORB_REQUIRE_VOLUME_CONFIRM", "0") == "1",
            volume_confirm_mult=_envf("ORB_VOLUME_CONFIRM_MULT", 1.0),
            require_ema_align=_envs("ORB_REQUIRE_EMA_ALIGN", "0") == "1",
            # v10 industry levers
            atr_stop_mult=_envf("ORB_ATR_STOP_MULT", 0.0),
            require_adx_above=_envf("ORB_REQUIRE_ADX_ABOVE", 0.0),
            skip_gap_pct=_envf("ORB_SKIP_GAP_PCT", 0.0),
            require_vwap_align=_envs("ORB_REQUIRE_VWAP_ALIGN", "0") == "1",
            skip_first_5min=_envs("ORB_SKIP_FIRST_5MIN", "0") == "1",
            trailing_stop_pct=_envf("ORB_TRAILING_STOP_PCT", 0.0),
            compound_daily=_envs("ORB_COMPOUND_DAILY", "0") == "1",
            regime_ticker=_envs("ORB_REGIME_TICKER", "").upper(),
            regime_dir_align=_envs("ORB_REGIME_DIR_ALIGN", "0") == "1",
            regime_min_or_bps=_envf("ORB_REGIME_MIN_OR_BPS", 0.0),
            require_rvol_above=_envf("ORB_REQUIRE_RVOL_ABOVE", 0.0),
            rvol_lookback_days=_envi("ORB_RVOL_LOOKBACK_DAYS", 20),
            skip_gap_above_pct=_envf("ORB_SKIP_GAP_ABOVE_PCT", 0.0),
            require_prior_nr_n=_envi("ORB_REQUIRE_PRIOR_NR_N", 0),
            skip_prior_wr_n=_envi("ORB_SKIP_PRIOR_WR_N", 0),
            skip_earnings_window=_envs("ORB_SKIP_EARNINGS_WINDOW", "0") == "1",
            earnings_days_before=_envi("ORB_EARNINGS_DAYS_BEFORE", 1),
            earnings_days_after=_envi("ORB_EARNINGS_DAYS_AFTER", 0),
            skip_vix_above=_envf("ORB_SKIP_VIX_ABOVE", 0.0),
            vix_csv_path=_envs("ORB_VIX_CSV_PATH", "data/external/vix-daily.csv"),
            vol_target_atr_pct=_envf("ORB_VOL_TARGET_ATR_PCT", 0.0),
            vol_target_min_scale=_envf("ORB_VOL_TARGET_MIN_SCALE", 0.5),
            vol_target_max_scale=_envf("ORB_VOL_TARGET_MAX_SCALE", 2.0),
            # v18 day-end-giveback defenses
            loss_lock_threshold_usd=_envf("ORB_LOSS_LOCK_THRESHOLD_USD", 0.0),
            peak_dd_halt_usd=_envf("ORB_PEAK_DD_HALT_USD", 0.0),
            # v19 signal-magnitude / cadence-latency filter
            min_break_bps=_envf("ORB_MIN_BREAK_BPS", 0.0),
            confirm_bars_n=_envi("ORB_CONFIRM_BARS_N", 0),
            # v20 chase-prevention filters
            max_vwap_dev_bps=_envf("ORB_MAX_VWAP_DEV_BPS", 0.0),
            max_vwap_dev_tickers=tuple(
                t.strip().upper()
                for t in _envs("ORB_MAX_VWAP_DEV_TICKERS", "").split(",")
                if t.strip()
            ),
            max_vwap_dev_bps_long=_envf("ORB_MAX_VWAP_DEV_BPS_LONG", 0.0),
            max_vwap_dev_bps_short=_envf("ORB_MAX_VWAP_DEV_BPS_SHORT", 0.0),
            confirm_bars_n_tickers=tuple(
                t.strip().upper()
                for t in _envs("ORB_CONFIRM_BARS_N_TICKERS", "").split(",")
                if t.strip()
            ),
            min_break_bps_tickers=tuple(
                t.strip().upper()
                for t in _envs("ORB_MIN_BREAK_BPS_TICKERS", "").split(",")
                if t.strip()
            ),
            fenced_or_min_pct=_envf("ORB_FENCED_OR_MIN_PCT", 0.0),
            fenced_or_max_pct=_envf("ORB_FENCED_OR_MAX_PCT", 0.0),
            fenced_or_tickers=tuple(
                t.strip().upper()
                for t in _envs("ORB_FENCED_OR_TICKERS", "").split(",")
                if t.strip()
            ),
            fenced_gap_pct=_envf("ORB_FENCED_GAP_PCT", 0.0),
            fenced_gap_tickers=tuple(
                t.strip().upper()
                for t in _envs("ORB_FENCED_GAP_TICKERS", "").split(",")
                if t.strip()
            ),
            skip_prior_spy_ret_lt_bps=_envf("ORB_SKIP_PRIOR_SPY_RET_LT_BPS", 0.0),
            skip_prior_spy_ret_gt_bps=_envf("ORB_SKIP_PRIOR_SPY_RET_GT_BPS", 0.0),
            regime_low_skip_tickers=tuple(
                t.strip().upper()
                for t in _envs("ORB_REGIME_LOW_SKIP_TICKERS", "").split(",")
                if t.strip()
            ),
            regime_low_risk_per_trade_pct=_envf("ORB_REGIME_LOW_RISK_PER_TRADE_PCT", 0.0),
            regime_low_atr_stop_mult=_envf("ORB_REGIME_LOW_ATR_STOP_MULT", 0.0),
            regime_low_max_trades_per_day=_envi("ORB_REGIME_LOW_MAX_TRADES_PER_DAY", 0),
            regime_low_max_vwap_dev_bps=_envf("ORB_REGIME_LOW_MAX_VWAP_DEV_BPS", 0.0),
            regime_low_min_break_bps=_envf("ORB_REGIME_LOW_MIN_BREAK_BPS", 0.0),
            premkt_align_bps=_envf("ORB_PREMKT_ALIGN_BPS", 0.0),
        )


SESSION_START_ET = _et_to_minutes("09:30")


# ---------- v10 industry-standard indicators ----------
def atr_5m(candles: list[Bar1m], lookback: int = 14) -> float:
    """ATR over the last `lookback` 5m candles. Returns 0 if insufficient data."""
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l = candles[i].high, candles[i].low
        pc = candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0.0
    use = trs[-lookback:]
    return sum(use) / len(use)


def dx_5m(candles: list[Bar1m], lookback: int = 14) -> float:
    """Single-window DX (directional-index) on 5m candles. Returns 0 if
    insufficient data.

    This is NOT true ADX -- it's the underlying DX computed from simple-MA
    smoothed +DM/-DM/TR over a single `lookback` window:

      +DM = if up_move > down_move and up_move > 0: up_move else 0
      -DM = if down_move > up_move and down_move > 0: down_move else 0
      TR  = max(h-l, |h-pc|, |l-pc|)
      +DI = 100 * sum(+DM, lookback) / sum(TR, lookback)
      -DI = 100 * sum(-DM, lookback) / sum(TR, lookback)
      DX  = 100 * |+DI - -DI| / (+DI + -DI)

    Used as a directional-strength proxy filter (formerly mis-named adx_5m).
    For the proper Wilder-smoothed ADX (DX averaged over `lookback` periods),
    see `adx_5m()` below.

    Reference fixture (synthetic strong-uptrend 15-bar series):
      candles where each bar's high/low/close trends up by 1.0 each step ->
      dx_5m(..., lookback=14) returns 100.0 (pure directional move, all +DM,
      no -DM); adx_5m(..., lookback=14) on the same series returns ~100.0
      after Wilder smoothing converges.
    """
    if len(candles) < lookback + 1:
        return 0.0
    pdms, ndms, trs = [], [], []
    for i in range(1, len(candles)):
        up = candles[i].high - candles[i - 1].high
        dn = candles[i - 1].low - candles[i].low
        pdm = up if (up > dn and up > 0) else 0.0
        ndm = dn if (dn > up and dn > 0) else 0.0
        tr = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i - 1].close),
            abs(candles[i].low - candles[i - 1].close),
        )
        pdms.append(pdm); ndms.append(ndm); trs.append(tr)
    if len(trs) < lookback:
        return 0.0
    p = pdms[-lookback:]
    n = ndms[-lookback:]
    t = trs[-lookback:]
    sum_t = sum(t)
    if sum_t <= 0:
        return 0.0
    pdi = 100 * sum(p) / sum_t
    ndi = 100 * sum(n) / sum_t
    if pdi + ndi <= 0:
        return 0.0
    return 100 * abs(pdi - ndi) / (pdi + ndi)


def adx_5m(candles: list[Bar1m], lookback: int = 14) -> float:
    """Proper Wilder-smoothed ADX(`lookback`) on 5m candles. Returns 0 if
    insufficient data.

    Algorithm (Wilder 1978):
      1. Compute per-bar +DM, -DM, TR (same as dx_5m).
      2. Wilder-smooth +DM, -DM, TR over `lookback` bars:
           initial smoothed = sum of first `lookback` values
           subsequent       = prev - (prev / lookback) + current
      3. +DI = 100 * smoothed(+DM) / smoothed(TR);  -DI similarly.
      4. DX = 100 * |+DI - -DI| / (+DI + -DI) for each smoothed bar.
      5. ADX = Wilder-smoothed average of DX over `lookback` periods:
           initial ADX = mean of first `lookback` DX values
           subsequent  = (prev_ADX * (lookback - 1) + current_DX) / lookback

    Requires at least 2 * `lookback` bars to produce a non-zero value.

    Reference fixture (synthetic strong-uptrend series, lookback=14):
      A series with each bar 1.0 higher than the prior on H/L/C produces
      DX = 100.0 every period (all up-moves, zero -DM), so the Wilder
      ADX of that series converges to 100.0 once warm.
    """
    n_needed = 2 * lookback
    if len(candles) < n_needed + 1:
        return 0.0
    pdms: list[float] = []
    ndms: list[float] = []
    trs: list[float] = []
    for i in range(1, len(candles)):
        up = candles[i].high - candles[i - 1].high
        dn = candles[i - 1].low - candles[i].low
        pdm = up if (up > dn and up > 0) else 0.0
        ndm = dn if (dn > up and dn > 0) else 0.0
        tr = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i - 1].close),
            abs(candles[i].low - candles[i - 1].close),
        )
        pdms.append(pdm)
        ndms.append(ndm)
        trs.append(tr)
    if len(trs) < n_needed:
        return 0.0
    # Wilder smoothing of +DM, -DM, TR -- produce smoothed series so we can
    # compute a DX value at each step after the initial warmup.
    s_pdm = sum(pdms[:lookback])
    s_ndm = sum(ndms[:lookback])
    s_tr = sum(trs[:lookback])
    dx_series: list[float] = []
    # First DX value uses the initial sums.
    if s_tr > 0:
        pdi = 100 * s_pdm / s_tr
        ndi = 100 * s_ndm / s_tr
        if pdi + ndi > 0:
            dx_series.append(100 * abs(pdi - ndi) / (pdi + ndi))
        else:
            dx_series.append(0.0)
    else:
        dx_series.append(0.0)
    # Subsequent Wilder-smoothed updates.
    for i in range(lookback, len(trs)):
        s_pdm = s_pdm - (s_pdm / lookback) + pdms[i]
        s_ndm = s_ndm - (s_ndm / lookback) + ndms[i]
        s_tr = s_tr - (s_tr / lookback) + trs[i]
        if s_tr <= 0:
            dx_series.append(0.0)
            continue
        pdi = 100 * s_pdm / s_tr
        ndi = 100 * s_ndm / s_tr
        if pdi + ndi <= 0:
            dx_series.append(0.0)
        else:
            dx_series.append(100 * abs(pdi - ndi) / (pdi + ndi))
    if len(dx_series) < lookback:
        return 0.0
    # Wilder-smooth DX into ADX.
    adx = sum(dx_series[:lookback]) / lookback
    for i in range(lookback, len(dx_series)):
        adx = (adx * (lookback - 1) + dx_series[i]) / lookback
    return adx


def session_vwap_at(bars_1m: list[Bar1m], at_bucket: int) -> float:
    """Cumulative VWAP from session open through `at_bucket` (inclusive)."""
    pv, vol = 0.0, 0.0
    for b in bars_1m:
        if b.bucket > at_bucket:
            break
        if b.bucket < SESSION_START_ET:
            continue
        typical = (b.high + b.low + b.close) / 3.0
        pv += typical * b.volume
        vol += b.volume
    return pv / vol if vol > 0 else 0.0


# ---------- backtest one ticker-day ----------
def run_ticker_day(date: str, ticker: str, bars_1m: list[Bar1m],
                   cfg: ORBConfig, current_account: float | None = None
                   ) -> list[dict]:
    """Returns a list of pnl_pair dicts (matches lever_sweep_runner schema).

    `current_account` is the running balance to use for sizing / notional
    caps when daily compounding is active. When None, falls back to
    cfg.account (so cfg is not mutated for the non-compounding path).
    """
    if current_account is None:
        current_account = cfg.account
    if not bars_1m:
        return []

    # Restrict to RTH (09:30 - 16:00).
    rth = [b for b in bars_1m if SESSION_START_ET <= b.bucket < _et_to_minutes("16:00")]
    if not rth:
        return []

    or_end = SESSION_START_ET + cfg.or_minutes
    or_window = [b for b in rth if SESSION_START_ET <= b.bucket < or_end]
    if not or_window:
        return []

    or_high = max(b.high for b in or_window)
    or_low = min(b.low for b in or_window)
    mid = (or_high + or_low) / 2.0
    or_range_pct = (or_high - or_low) / mid if mid > 0 else 0.0

    if not (cfg.range_min_pct <= or_range_pct <= cfg.range_max_pct):
        return []

    # v21 fenced OR-width gate. When the ticker is in fenced_or_tickers,
    # also enforce a tighter range. 0 thresholds = uncapped on that side.
    if cfg.fenced_or_tickers and ticker in cfg.fenced_or_tickers:
        if (cfg.fenced_or_min_pct > 0
                and or_range_pct < cfg.fenced_or_min_pct):
            return []
        if (cfg.fenced_or_max_pct > 0
                and or_range_pct > cfg.fenced_or_max_pct):
            return []

    # Aggregate post-OR bars to 5-min candles for breakout signals.
    post_or_1m = [b for b in rth if b.bucket >= or_end]
    candles_5m = aggregate_5m(post_or_1m)
    if not candles_5m:
        return []

    blocked_sides = {s.upper() for s in cfg.blocklist.get(ticker, [])}

    # v20 premkt-alignment precompute (once per ticker-day). Uses bars in
    # the 09:00-09:29 ET window. premkt_move_bps = (premkt_close - premkt_open)
    # in bps. Signed so that a long entry needs positive premkt move.
    premkt_move_bps = 0.0
    if cfg.premkt_align_bps > 0:
        pre_start = _et_to_minutes("09:00")
        pre_bars = [b for b in bars_1m
                    if pre_start <= b.bucket < SESSION_START_ET]
        if pre_bars and pre_bars[0].open > 0:
            premkt_move_bps = ((pre_bars[-1].close - pre_bars[0].open)
                               / pre_bars[0].open * 10000.0)

    pairs: list[dict] = []
    trades_today = 0

    # Walk 5-min candles, find first breakout (signal). Entry is on the
    # NEXT 5-min candle's open. So index forward: signal on candles_5m[i],
    # entry on candles_5m[i+1] (if it exists and is in time window).
    for i in range(len(candles_5m) - 1):
        if trades_today >= cfg.max_trades_per_day:
            break
        sig = candles_5m[i]
        if sig.bucket >= cfg.time_cutoff_et:
            break
        # Signal: close above OR_high (long) or below OR_low (short).
        side = None
        if sig.close > or_high:
            side = "long"
        elif sig.close < or_low:
            side = "short"
        if side is None:
            continue
        if side.upper() in blocked_sides:
            continue

        # v19: minimum break magnitude in bps. Suppresses marginal first-bar
        # breaks that production tends to miss due to scan-loop cadence
        # latency (forensic: AMZN -4.5bps was skipped, -23bps fired).
        # v21 fence: when min_break_bps_tickers non-empty, only apply to
        # those tickers.
        if cfg.min_break_bps > 0 and (
            not cfg.min_break_bps_tickers
            or ticker in cfg.min_break_bps_tickers
        ):
            if side == "long" and or_high > 0:
                break_bps = (sig.close - or_high) / or_high * 10000.0
            elif side == "short" and or_low > 0:
                break_bps = (or_low - sig.close) / or_low * 10000.0
            else:
                break_bps = 0.0
            if break_bps < cfg.min_break_bps:
                continue

        # v19: N-bar confirmation. Require the prior N 5m closes including
        # this bar to all be on the same side of the OR boundary.
        # v21 fence: when confirm_bars_n_tickers non-empty, only apply to
        # those tickers.
        if cfg.confirm_bars_n > 1 and (
            not cfg.confirm_bars_n_tickers
            or ticker in cfg.confirm_bars_n_tickers
        ):
            start = i - cfg.confirm_bars_n + 1
            if start < 0:
                continue
            window = candles_5m[start : i + 1]
            if side == "long":
                if not all(c.close > or_high for c in window):
                    continue
            else:
                if not all(c.close < or_low for c in window):
                    continue

        # v20 premkt alignment: require the 09:00-09:29 move to be in the
        # breakout direction by at least premkt_align_bps. Filters
        # counter-premkt reversal trades that bled in the per-ticker
        # forensic (AAPL/short, MSFT/short, META/short, GOOG/short).
        if cfg.premkt_align_bps > 0:
            need = cfg.premkt_align_bps
            if side == "long" and premkt_move_bps < need:
                continue
            if side == "short" and premkt_move_bps > -need:
                continue

        # v9 lever: volume confirmation -- require signal candle's
        # volume >= mult * mean(prior candles_5m). Skip if too quiet.
        if cfg.require_volume_confirm:
            prior = candles_5m[max(0, i - 12):i]  # last hour of 5m bars
            if prior:
                avg_vol = sum(c.volume for c in prior) / len(prior)
                if sig.volume < cfg.volume_confirm_mult * avg_vol:
                    continue

        # v9 lever: 200-EMA alignment -- require signal close above
        # (long) or below (short) the EMA(200) of all signal-eligible
        # 5m candles up to and including the signal bar. We approximate
        # 200-period EMA on the post-OR + pre-signal candles using the
        # simple mean as a proxy when fewer than 200 candles available
        # (this is a single-day backtest; EMA(200) is unreasonable on
        # one day's data, but a daily directional filter using prior-
        # day VWAP would be a better implementation -- not done here).
        if cfg.require_ema_align:
            ref_candles = candles_5m[: i + 1]
            if ref_candles:
                ema_proxy = sum(c.close for c in ref_candles) / len(ref_candles)
                if side == "long" and sig.close <= ema_proxy:
                    continue
                if side == "short" and sig.close >= ema_proxy:
                    continue

        # v10: skip first 5min after OR (i==0). Often noisy.
        if cfg.skip_first_5min and i == 0:
            continue

        # v10: ADX trend filter -- skip choppy days. Uses single-window DX
        # (directional-strength proxy) rather than full Wilder ADX, matching
        # the historical behavior under this lever's threshold calibration.
        if cfg.require_adx_above > 0:
            adx_val = dx_5m(candles_5m[: i + 1], lookback=14)
            if adx_val < cfg.require_adx_above:
                continue

        # v10: VWAP alignment -- long only above session VWAP
        if cfg.require_vwap_align:
            vwap = session_vwap_at(rth, sig.bucket + 4)  # signal bar's last 1m
            if vwap > 0:
                if side == "long" and sig.close <= vwap:
                    continue
                if side == "short" and sig.close >= vwap:
                    continue

        entry_candle = candles_5m[i + 1]
        # Entry at next 5-min candle open with adverse slippage.
        raw_entry = entry_candle.open
        slip_bps = cfg.entry_slippage_bps + (cfg.short_pen_bps if side == "short" else 0)
        slip = raw_entry * slip_bps / 10000.0
        entry_price = raw_entry + slip if side == "long" else raw_entry - slip

        # v20 chase-prevention: reject if entry has already moved more
        # than max_vwap_dev_bps past session VWAP in the breakout
        # direction. session VWAP computed through the signal bar's last
        # 1m (sig.bucket + 4 = signal bar's closing 1m bucket).
        # When max_vwap_dev_tickers is non-empty the filter only applies
        # to those tickers (per-list fence). Per-side overrides
        # (max_vwap_dev_bps_long/short) take precedence over the symmetric
        # threshold when set.
        side_thr = (
            cfg.max_vwap_dev_bps_long if side == "long"
            else cfg.max_vwap_dev_bps_short
        )
        effective_thr = side_thr if side_thr > 0 else cfg.max_vwap_dev_bps
        if effective_thr > 0 and (
            not cfg.max_vwap_dev_tickers
            or ticker in cfg.max_vwap_dev_tickers
        ):
            vwap_at = session_vwap_at(rth, sig.bucket + 4)
            if vwap_at > 0:
                if side == "long":
                    dev_bps = (entry_price - vwap_at) / vwap_at * 10000.0
                else:
                    dev_bps = (vwap_at - entry_price) / vwap_at * 10000.0
                if dev_bps > effective_thr:
                    continue

        # Stop: opposite side of OR with buffer adder. v10: optional ATR
        # override -- entry +- atr_stop_mult * ATR for volatility-adaptive
        # stops. ATR computed on candles up to and including signal bar.
        stop_buf = entry_price * cfg.stop_buffer_bps / 10000.0
        if cfg.atr_stop_mult > 0:
            atr = atr_5m(candles_5m[: i + 1], lookback=14)
            if atr > 0:
                if side == "long":
                    stop = entry_price - cfg.atr_stop_mult * atr
                else:
                    stop = entry_price + cfg.atr_stop_mult * atr
            else:
                # fallback to OR stop if ATR not yet warm
                if side == "long":
                    stop = or_low - stop_buf
                else:
                    stop = or_high + stop_buf
        else:
            if side == "long":
                stop = or_low - stop_buf
            else:
                stop = or_high + stop_buf

        risk = abs(entry_price - stop)
        if risk <= 0.001:
            continue
        target = entry_price + cfg.rr * risk if side == "long" else entry_price - cfg.rr * risk

        # Position sizing: risk ORB_RISK_PER_TRADE_PCT of account.
        risk_dollars = current_account * cfg.risk_per_trade_pct / 100.0
        # v17 vol-targeted sizing: when ATR is high (volatile regime), scale
        # risk DOWN; when ATR is low (calm regime), scale UP. CLEAN: ATR is
        # computed from prior bars (slice [:i+1] is up to and including the
        # signal bar; entry fires on bar i+1's open).
        if cfg.vol_target_atr_pct > 0 and sig.close > 0:
            atr_now = atr_5m(candles_5m[:i + 1], lookback=14)
            atr_pct = atr_now / sig.close * 100.0
            if atr_pct > 0:
                scale = cfg.vol_target_atr_pct / atr_pct
                scale = max(cfg.vol_target_min_scale,
                            min(cfg.vol_target_max_scale, scale))
                risk_dollars *= scale
        shares = max(1, int(risk_dollars / risk))

        # v8 realism cap: single-trade notional must not exceed
        # ORB_MAX_TRADE_NOTIONAL_PCT of the account (default 25%).
        # Without this, tight stops produce phantom leverage -- the
        # audit found one trade at $157k notional on a $100k account.
        # Real Reg T DTBP is 4x but per-trade discipline limits to
        # ~25% notional to keep diversification.
        max_notional = current_account * cfg.max_trade_notional_pct / 100.0
        if entry_price > 0:
            shares_cap = max(1, int(max_notional / entry_price))
            shares = min(shares, shares_cap)

        # Walk forward from entry_candle.bucket through 1-min bars to find
        # exit. 1-min granularity for accurate intra-bar stop/target checks.
        entry_bkt = entry_candle.bucket
        # Skip 1m bars BEFORE entry candle starts.
        forward_1m = [b for b in rth if b.bucket >= entry_bkt]

        exit_price = None
        exit_reason = None
        exit_bkt = None
        # v9 levers state -- only mutate the vars below (stop, target,
        # remaining_shares) inside the loop based on cfg toggles.
        be_moved = False  # has stop been bumped to break-even after 1R?
        partial_taken = False  # has 50% been booked at 1R?
        partial_pnl_dollars = 0.0  # P&L from the partial take, added to final
        remaining_shares = shares
        one_r_long = entry_price + risk if side == "long" else entry_price - risk
        for fb in forward_1m:
            # First check stop/target intra-bar (high/low pierce).
            if side == "long":
                # Stop-out: bar.low <= stop -> fill at min(open, stop)
                if fb.low <= stop:
                    fill = min(fb.open, stop)
                    fill = max(fb.low, fill)
                    fill = min(fb.high, fill)
                    exit_price = fill
                    exit_reason = "stop" if not be_moved else "be_stop"
                    exit_bkt = fb.bucket
                    break
                # v9 lever: move stop to break-even after 1R reached.
                if cfg.move_to_be_after_1r and (not be_moved) and fb.high >= one_r_long:
                    stop = entry_price  # BE
                    be_moved = True
                # v9 lever: take partial profit at 1R (sell 50%).
                if (cfg.partial_profit_at_1r and (not partial_taken)
                        and fb.high >= one_r_long):
                    half = remaining_shares // 2
                    if half > 0:
                        partial_pnl_dollars = (one_r_long - entry_price) * half
                        remaining_shares -= half
                        partial_taken = True
                # v10 lever: trailing stop after BE -- ratchet stop up by
                # trailing_stop_pct of initial risk behind highest high
                # since entry. trailing_stop_pct=0 disables.
                if cfg.trailing_stop_pct > 0 and be_moved:
                    new_stop = fb.high - cfg.trailing_stop_pct * risk
                    if new_stop > stop:
                        stop = new_stop
                # Target hit: bar.high >= target -> fill at max(open, target)
                if fb.high >= target:
                    fill = max(fb.open, target)
                    fill = max(fb.low, fill)
                    fill = min(fb.high, fill)
                    exit_price = fill
                    exit_reason = "target"
                    exit_bkt = fb.bucket
                    break
            else:  # short
                if fb.high >= stop:
                    fill = max(fb.open, stop)
                    fill = max(fb.low, fill)
                    fill = min(fb.high, fill)
                    exit_price = fill
                    exit_reason = "stop" if not be_moved else "be_stop"
                    exit_bkt = fb.bucket
                    break
                # v9 lever: move stop to BE after 1R (short side mirror)
                one_r_short = entry_price - risk
                if cfg.move_to_be_after_1r and (not be_moved) and fb.low <= one_r_short:
                    stop = entry_price
                    be_moved = True
                # v10 lever: trailing stop after BE (short side mirror)
                if cfg.trailing_stop_pct > 0 and be_moved:
                    new_stop = fb.low + cfg.trailing_stop_pct * risk
                    if new_stop < stop:
                        stop = new_stop
                # v9 lever: partial profit at 1R (short)
                if (cfg.partial_profit_at_1r and (not partial_taken)
                        and fb.low <= one_r_short):
                    half = remaining_shares // 2
                    if half > 0:
                        partial_pnl_dollars = (entry_price - one_r_short) * half
                        remaining_shares -= half
                        partial_taken = True
                if fb.low <= target:
                    fill = min(fb.open, target)
                    fill = max(fb.low, fill)
                    fill = min(fb.high, fill)
                    exit_price = fill
                    exit_reason = "target"
                    exit_bkt = fb.bucket
                    break
            # EOD cutoff?
            if fb.bucket >= cfg.eod_cutoff_et:
                exit_price = fb.close
                exit_reason = "eod"
                exit_bkt = fb.bucket
                break

        if exit_price is None:
            # Walked off corpus without exit -> use last bar close as forced
            # EOD (defensive; shouldn't happen for full RTH days).
            last = forward_1m[-1] if forward_1m else None
            if last is None:
                continue
            exit_price = last.close
            exit_reason = "eod_fallback"
            exit_bkt = last.bucket

        # Apply exit slippage.
        exit_slip_bps = cfg.exit_slippage_bps + (
            cfg.stop_kick_bps if exit_reason == "stop" else 0
        ) + (cfg.short_pen_bps if side == "short" else 0)
        slip = exit_price * exit_slip_bps / 10000.0
        if side == "long":
            exit_price -= slip
        else:
            exit_price += slip

        pnl_per_share = (exit_price - entry_price) if side == "long" else (entry_price - exit_price)
        # v9 partial-profit: book the partial-fill P&L on the half taken
        # at 1R, then remaining_shares ride to final exit.
        runner_shares = remaining_shares if cfg.partial_profit_at_1r else shares
        pnl_dollars = pnl_per_share * runner_shares + partial_pnl_dollars

        pairs.append({
            "ticker": ticker,
            "side": side,
            "entry_ts": _bucket_to_iso(date, entry_bkt),
            "exit_ts": _bucket_to_iso(date, exit_bkt),
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "shares": int(shares),
            "pnl_per_share": round(pnl_per_share, 4),
            "pnl_dollars": round(pnl_dollars, 4),
            "exit_reason": exit_reason,
            "or_high": round(or_high, 4),
            "or_low": round(or_low, 4),
            "or_range_pct": round(or_range_pct, 6),
            "stop_price": round(stop, 4),
            "risk_dollars": round(risk * shares, 2),
        })
        trades_today += 1

    return pairs


def _bucket_to_iso(date: str, minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{date}T{h:02d}:{m:02d}:00-05:00"  # ET (DST-naive)


# ---------- v14 prior-day filters: gap + NR_N (Crabel) ----------
def compute_daily_session_stats(corpus_dir: Path, dates: list[str],
                                tickers: list[str]) -> dict:
    """Return {(date, ticker): (session_high, session_low, session_close)}.

    Session = regular-trading-hours bars (09:30-16:00 ET). Used by the
    overnight-gap filter (today_open vs prev_close) and the NR_N filter
    (prior-session range vs N-day window of prior ranges).

    Look-ahead audit: each tuple uses ONLY data with timestamps within the
    given date's session. The CALLER must only consume tuples for dates
    strictly prior to the decision date, never the same date.
    """
    SESSION_END = _et_to_minutes("16:00")
    out: dict[tuple[str, str], tuple[float, float, float]] = {}
    for date in dates:
        for tk in tickers:
            try:
                bars = load_day_bars(corpus_dir, date, tk)
            except Exception:
                continue
            if not bars:
                continue
            rth = [b for b in bars
                   if SESSION_START_ET <= b.bucket < SESSION_END]
            if not rth:
                continue
            session_high = max(b.high for b in rth)
            session_low = min(b.low for b in rth)
            session_close = rth[-1].close
            out[(date, tk)] = (session_high, session_low, session_close)
    return out


def overnight_gap_pct(daily_stats: dict, dates: list[str], date: str,
                      ticker: str, today_first_open: float) -> float | None:
    """Return |today_open - prev_close| / prev_close * 100, or None if missing.

    Look-ahead: prev_close is from the strictly prior session date.
    today_first_open is the 09:30 open bar (causally available at OR start).
    """
    try:
        idx = dates.index(date)
    except ValueError:
        return None
    if idx == 0:
        return None
    prev_date = dates[idx - 1]
    stats = daily_stats.get((prev_date, ticker))
    if stats is None:
        return None
    _, _, prev_close = stats
    if prev_close <= 0 or today_first_open <= 0:
        return None
    return abs(today_first_open - prev_close) / prev_close * 100.0


def is_prior_nr_n(daily_stats: dict, dates: list[str], date: str,
                  ticker: str, n: int) -> bool | None:
    """True if the PRIOR session's range was the MIN of the last N prior
    sessions' ranges (Crabel NR_N). None if insufficient history.

    Look-ahead: uses only sessions strictly before `date`.
    """
    try:
        idx = dates.index(date)
    except ValueError:
        return None
    if idx < n:
        return None
    ranges = []
    for d in dates[idx - n:idx]:
        s = daily_stats.get((d, ticker))
        if s is None:
            return None
        h, lo, _ = s
        ranges.append(h - lo)
    if not ranges:
        return None
    prior_range = ranges[-1]  # range of (idx-1) -- the most recent prior session
    return prior_range == min(ranges) and prior_range > 0


def is_prior_wr_n(daily_stats: dict, dates: list[str], date: str,
                  ticker: str, n: int) -> bool | None:
    """True if the PRIOR session's range was the MAX of last N prior
    sessions' ranges (wide-range exhaustion). None if insufficient.
    """
    try:
        idx = dates.index(date)
    except ValueError:
        return None
    if idx < n:
        return None
    ranges = []
    for d in dates[idx - n:idx]:
        s = daily_stats.get((d, ticker))
        if s is None:
            return None
        h, lo, _ = s
        ranges.append(h - lo)
    if not ranges:
        return None
    prior_range = ranges[-1]
    return prior_range == max(ranges) and prior_range > 0


# ---------- v12 SPY/QQQ regime gate ----------
def compute_regime(corpus_dir: Path, date: str, ticker: str,
                   or_minutes: int) -> dict | None:
    """Compute the day-level regime from an index ticker's 30-min OR.

    Returns dict with keys:
      direction: "LONG"  if OR_close > OR_open (uptrend bias)
                 "SHORT" if OR_close < OR_open (downtrend bias)
                 "FLAT"  if OR_close == OR_open
      or_bps:    signed magnitude of (OR_close - OR_open)/OR_open in basis points
                 (positive for uptrend, negative for downtrend)

    Returns None if the index data isn't loadable or the OR is incomplete --
    callers should treat None as "no regime info; do not gate" (fail-open).
    """
    try:
        bars = load_day_bars(corpus_dir, date, ticker)
    except Exception:
        return None
    if not bars:
        return None
    or_end = SESSION_START_ET + or_minutes
    or_bars = [b for b in bars
               if SESSION_START_ET <= b.bucket < or_end]
    if not or_bars:
        return None
    or_open = or_bars[0].open
    or_close = or_bars[-1].close
    if or_open <= 0:
        return None
    or_bps = 10000.0 * (or_close - or_open) / or_open
    if or_close > or_open:
        direction = "LONG"
    elif or_close < or_open:
        direction = "SHORT"
    else:
        direction = "FLAT"
    return {"direction": direction, "or_bps": or_bps}


# ---------- v13 RVOL gate (Zarattini SSRN 2023) ----------
def compute_or_volumes(corpus_dir: Path, dates: list[str],
                       tickers: list[str], or_minutes: int) -> dict:
    """Return {(date, ticker): or_window_volume} for every date x ticker.

    The OR window is [09:30, 09:30 + or_minutes). Fully closed by the time
    the first ORB entry signal can fire (next 5-min candle's open after the
    OR window). CLEAN look-ahead: OR_volume(date, ticker) is causally
    available at decision time on `date`.

    Used by the RVOL gate to compute today's OR_volume / mean(prior_N
    OR_volumes for same ticker).
    """
    or_end = SESSION_START_ET + or_minutes
    out: dict[tuple[str, str], float] = {}
    for date in dates:
        for tk in tickers:
            try:
                bars = load_day_bars(corpus_dir, date, tk)
            except Exception:
                continue
            if not bars:
                continue
            v = sum(b.volume for b in bars
                    if SESSION_START_ET <= b.bucket < or_end)
            out[(date, tk)] = v
    return out


def rvol_for(or_volumes: dict, dates: list[str], date: str, ticker: str,
             lookback: int) -> float | None:
    """RVOL = today's OR_volume / mean(prior `lookback` sessions' OR_volume).

    Returns None if today's volume is missing OR fewer than half the
    lookback baseline samples are available (insufficient history). The
    baseline uses ONLY prior sessions for the same ticker -- no
    look-ahead. Returns 0.0 if today's volume is 0 (filtered out).
    """
    today_v = or_volumes.get((date, ticker))
    if today_v is None or today_v <= 0:
        return None
    try:
        idx = dates.index(date)
    except ValueError:
        return None
    prior = []
    for d in dates[max(0, idx - lookback):idx]:
        v = or_volumes.get((d, ticker))
        if v is not None and v > 0:
            prior.append(v)
    if len(prior) < max(5, lookback // 2):
        return None  # insufficient history -- fail-open (don't gate)
    baseline = sum(prior) / len(prior)
    if baseline <= 0:
        return None
    return today_v / baseline


# ---------- driver ----------
def discover_dates(corpus_dir: Path, year_prefix: str, tickers: list[str]) -> list[str]:
    out = []
    for p in sorted(corpus_dir.iterdir()):
        if not p.is_dir() or not p.name.startswith(year_prefix):
            continue
        if all((p / f"{t}.jsonl").exists() for t in tickers):
            out.append(p.name)
    return out


def run(corpus_dir: Path, out_dir: Path, dates: list[str],
        tickers: list[str], cfg: ORBConfig, vid: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    per_day_dir = out_dir / "per_day"
    per_day_dir.mkdir(parents=True, exist_ok=True)

    total_pnl = 0.0
    total_entries = 0
    # v11 compounding: track running account if enabled. We use a local
    # `current_account` for sizing / caps and pass it explicitly into
    # run_ticker_day; cfg.account is NEVER mutated so cfg stays reusable
    # across run() calls and summary.config.account preserves the configured
    # starting balance.
    starting_account = cfg.account
    running_account = starting_account
    daily_account_history = []
    total_wins = 0
    total_losses = 0
    days_ok = 0
    days_failed = 0
    tickers_failed = 0
    failures: list[dict] = []
    # v12 regime-gate diagnostics: count days/signals filtered by the index gate
    regime_days_skipped_flat = 0
    regime_days_skipped_low_or = 0
    regime_signals_dropped = 0
    # v13 RVOL gate: pre-compute OR-window volumes for every date x ticker
    # ONCE so per-day RVOL lookups are O(lookback). The OR window is fully
    # closed at OR_END (e.g. 10:00) -- well before the earliest possible
    # entry fill time (10:05+). No look-ahead.
    or_volumes: dict = {}
    rvol_signals_dropped = 0
    rvol_insufficient_history = 0
    if cfg.require_rvol_above > 0:
        # Include both per-ticker volumes and the regime ticker so future
        # variants can RVOL-gate on SPY/QQQ too if needed. Caching is cheap.
        rvol_universe = list(tickers)
        if cfg.regime_ticker and cfg.regime_ticker not in rvol_universe:
            rvol_universe.append(cfg.regime_ticker)
        or_volumes = compute_or_volumes(
            corpus_dir, dates, rvol_universe, cfg.or_minutes)
    # v14 prior-day filters: pre-compute per-ticker per-day session stats
    # (high/low/close) ONCE so per-day lookups are O(1). Used by
    # overnight-gap and NR_N/WR_N filters. CLEAN look-ahead: each filter
    # only consumes stats from dates strictly prior to the decision date.
    daily_stats: dict = {}
    gap_signals_dropped = 0
    nr_signals_dropped = 0
    wr_signals_dropped = 0
    earnings_signals_dropped = 0
    if (cfg.skip_gap_above_pct > 0 or cfg.require_prior_nr_n > 0
            or cfg.skip_prior_wr_n > 0):
        daily_stats = compute_daily_session_stats(
            corpus_dir, dates, list(tickers))
    # v16 VIX gate: load CSV once
    vix_closes: dict = {}
    vix_days_skipped = 0
    if cfg.skip_vix_above > 0:
        vix_closes = load_vix_closes(cfg.vix_csv_path)

    # v22: pre-compute prior-day SPY close-to-close return per date.
    # Uses SPY's last RTH bar (or last bar) as the daily close. The
    # value stored at `date` is the return FROM the day-before-prior
    # close TO the prior-day close (i.e. the regime indicator for
    # entering `date`'s session).
    prior_spy_ret_bps: dict[str, float] = {}
    if cfg.skip_prior_spy_ret_lt_bps != 0.0 or cfg.skip_prior_spy_ret_gt_bps != 0.0:
        spy_closes_per_date: dict[str, float] = {}
        for d in dates:
            try:
                bars = load_day_bars(corpus_dir, d, "SPY")
            except Exception:
                bars = []
            if not bars:
                continue
            rth = [b for b in bars
                   if SESSION_START_ET <= b.bucket < _et_to_minutes("16:00")]
            if rth:
                spy_closes_per_date[d] = rth[-1].close
        sorted_d = sorted(spy_closes_per_date.keys())
        for i, d in enumerate(sorted_d):
            if i < 2:
                continue
            pd = sorted_d[i-1]
            pp = sorted_d[i-2]
            base = spy_closes_per_date.get(pp, 0.0)
            close = spy_closes_per_date.get(pd, 0.0)
            if base > 0:
                prior_spy_ret_bps[d] = (close - base) / base * 10000.0

    spy_regime_days_skipped = 0
    for date in dates:
        # v22 prior-day SPY regime skip. Apply BEFORE per-ticker work to
        # short-circuit days entirely. Two modes:
        #   - only _lt_bps set:   skip if prior_spy_ret < _lt_bps
        #   - both set:           skip if prior_spy_ret in [_lt_bps, _gt_bps]
        # v22b: when regime_low_skip_tickers is set, skip only those
        # tickers on regime-low days instead of the whole day.
        is_regime_low_today = False
        if cfg.skip_prior_spy_ret_lt_bps != 0.0 or cfg.skip_prior_spy_ret_gt_bps != 0.0:
            prior = prior_spy_ret_bps.get(date)
            if prior is not None:
                lt = cfg.skip_prior_spy_ret_lt_bps
                gt = cfg.skip_prior_spy_ret_gt_bps
                if gt != 0.0 and lt != 0.0:
                    if lt <= prior <= gt:
                        is_regime_low_today = True
                elif lt != 0.0 and prior < lt:
                    is_regime_low_today = True
        # Full-day skip only when no partial-trade mechanism is active.
        # Partial-trade modes (any non-zero regime-low override or skip
        # list) take precedence -- they signal the operator wants to
        # trade these days under modified config.
        has_partial_mode = bool(
            cfg.regime_low_skip_tickers
            or cfg.regime_low_risk_per_trade_pct > 0
            or cfg.regime_low_atr_stop_mult > 0
            or cfg.regime_low_max_trades_per_day > 0
            or cfg.regime_low_max_vwap_dev_bps > 0
            or cfg.regime_low_min_break_bps > 0
        )
        if is_regime_low_today and not has_partial_mode:
            spy_regime_days_skipped += 1
            continue

        # v11 compounding: at the start of each day, snapshot the running
        # balance into `current_account` so position sizes (and risk caps)
        # scale with the latest balance. After the day's P&L is finalized,
        # update running_account. cfg is never mutated.
        current_account = running_account if cfg.compound_daily else starting_account

        # v12 regime gate: compute index OR direction + magnitude once per
        # day. Fail-open on missing data (treat as "no regime info"). The
        # filter is applied AFTER candidate collection so per-ticker logic
        # is unchanged when the gate is off.
        regime = None
        regime_skip_day = False
        regime_skip_reason = ""
        if cfg.regime_ticker:
            regime = compute_regime(corpus_dir, date, cfg.regime_ticker,
                                    cfg.or_minutes)
            if regime is not None:
                if regime["direction"] == "FLAT":
                    regime_skip_day = True
                    regime_skip_reason = "regime_flat"
                    regime_days_skipped_flat += 1
                elif (cfg.regime_min_or_bps > 0
                      and abs(regime["or_bps"]) < cfg.regime_min_or_bps):
                    regime_skip_day = True
                    regime_skip_reason = "regime_low_or"
                    regime_days_skipped_low_or += 1

        # v16 VIX absolute-level gate: skip whole day if VIX(D-1) > threshold.
        # Halts new entries entirely (compounding base preserved). CLEAN
        # look-ahead: prior session close is the most recent VIX value
        # available before the open.
        if cfg.skip_vix_above > 0 and not regime_skip_day:
            prior_vix = vix_close_for(vix_closes, dates, date)
            if prior_vix is not None and prior_vix > cfg.skip_vix_above:
                regime_skip_day = True
                regime_skip_reason = f"vix_high_{prior_vix:.1f}"
                vix_days_skipped += 1

        # R13b: build a regime-adjusted cfg when today is regime-low and
        # any override field is set. Keeps cfg immutable for non-regime
        # days.
        eff_cfg = cfg
        if is_regime_low_today:
            overrides = {}
            if cfg.regime_low_risk_per_trade_pct > 0:
                overrides["risk_per_trade_pct"] = cfg.regime_low_risk_per_trade_pct
            if cfg.regime_low_atr_stop_mult > 0:
                overrides["atr_stop_mult"] = cfg.regime_low_atr_stop_mult
            if cfg.regime_low_max_trades_per_day > 0:
                overrides["max_trades_per_day"] = cfg.regime_low_max_trades_per_day
            if cfg.regime_low_max_vwap_dev_bps > 0:
                overrides["max_vwap_dev_bps"] = cfg.regime_low_max_vwap_dev_bps
            if cfg.regime_low_min_break_bps > 0:
                overrides["min_break_bps"] = cfg.regime_low_min_break_bps
            if overrides:
                eff_cfg = dataclass_replace(cfg, **overrides)

        candidate_pairs: list[dict] = []
        if not regime_skip_day:
            for tk in tickers:
                # v22b: regime-low conditional per-ticker skip.
                if (is_regime_low_today
                        and cfg.regime_low_skip_tickers
                        and tk in cfg.regime_low_skip_tickers):
                    continue
                try:
                    bars = load_day_bars(corpus_dir, date, tk)
                    if not bars:
                        continue
                    pairs = run_ticker_day(date, tk, bars, eff_cfg, current_account)
                    candidate_pairs.extend(pairs)
                except (RuntimeError, AssertionError):
                    # Genuine bugs -- re-raise rather than silently dropping.
                    raise
                except Exception as e:
                    tickers_failed += 1
                    if len(failures) < 10:
                        failures.append({
                            "date": date,
                            "ticker": tk,
                            "error_class": type(e).__name__,
                            "error_msg": str(e),
                        })
                    print(f"WARN {date} {tk}: {type(e).__name__}: {e}",
                          file=sys.stderr)

        # v12 directional alignment: drop signals whose side doesn't match
        # the regime. Applied after collection so the diagnostic count is
        # accurate. When regime=None (no data) or regime_dir_align=False,
        # this is a no-op. Note: candidate_pairs use lowercase side
        # ("long"/"short") -- match case-insensitively.
        if (cfg.regime_dir_align and regime is not None
                and regime["direction"] in ("LONG", "SHORT")):
            allowed_side = regime["direction"].lower()
            kept = [p for p in candidate_pairs
                    if str(p["side"]).lower() == allowed_side]
            regime_signals_dropped += len(candidate_pairs) - len(kept)
            candidate_pairs = kept

        # v13 RVOL gate (Zarattini): drop per-ticker signals where today's
        # OR-window volume is below `require_rvol_above` x prior-N-day mean
        # for the same ticker. Fail-open on missing/insufficient history.
        # CLEAN look-ahead: today's OR_volume is fully observable at OR
        # close; entries fire AFTER OR close.
        if cfg.require_rvol_above > 0 and candidate_pairs:
            ticker_rvols: dict = {}
            kept = []
            for p in candidate_pairs:
                tk = p["ticker"]
                if tk not in ticker_rvols:
                    rv = rvol_for(or_volumes, dates, date, tk,
                                  cfg.rvol_lookback_days)
                    ticker_rvols[tk] = rv
                rv = ticker_rvols[tk]
                if rv is None:
                    rvol_insufficient_history += 1
                    kept.append(p)  # fail-open
                elif rv < cfg.require_rvol_above:
                    rvol_signals_dropped += 1
                else:
                    p["rvol"] = round(rv, 3)  # diagnostic
                    kept.append(p)
            candidate_pairs = kept

        # v14 overnight-gap filter: drop ticker on day if today's 09:30
        # open is gapped > X% from prior session close. Uses only prior
        # session data. Fail-open on missing data.
        if cfg.skip_gap_above_pct > 0 and candidate_pairs:
            kept = []
            ticker_open: dict = {}
            for p in candidate_pairs:
                tk = p["ticker"]
                if tk not in ticker_open:
                    # Get the 09:30 open bar from this day's bars
                    bars = load_day_bars(corpus_dir, date, tk)
                    open_bar = next((b for b in bars
                                     if b.bucket == SESSION_START_ET), None)
                    ticker_open[tk] = open_bar.open if open_bar else None
                today_open = ticker_open[tk]
                if today_open is None:
                    kept.append(p)  # fail-open
                    continue
                gap = overnight_gap_pct(daily_stats, dates, date, tk, today_open)
                if gap is None:
                    kept.append(p)  # fail-open (no prior day)
                elif gap > cfg.skip_gap_above_pct:
                    gap_signals_dropped += 1
                else:
                    kept.append(p)
            candidate_pairs = kept

        # v14 NR_N / WR_N filters (Crabel): require prior session was a
        # narrow-range-N (compression precedes expansion) OR drop signals
        # after a wide-range-N (range exhausted). Fail-open on missing.
        if cfg.require_prior_nr_n > 0 and candidate_pairs:
            kept = []
            ticker_ok: dict = {}
            for p in candidate_pairs:
                tk = p["ticker"]
                if tk not in ticker_ok:
                    is_nr = is_prior_nr_n(daily_stats, dates, date, tk,
                                          cfg.require_prior_nr_n)
                    ticker_ok[tk] = is_nr if is_nr is not None else True
                if not ticker_ok[tk]:
                    nr_signals_dropped += 1
                else:
                    kept.append(p)
            candidate_pairs = kept

        if cfg.skip_prior_wr_n > 0 and candidate_pairs:
            kept = []
            ticker_block: dict = {}
            for p in candidate_pairs:
                tk = p["ticker"]
                if tk not in ticker_block:
                    is_wr = is_prior_wr_n(daily_stats, dates, date, tk,
                                          cfg.skip_prior_wr_n)
                    ticker_block[tk] = bool(is_wr)
                if ticker_block[tk]:
                    wr_signals_dropped += 1
                else:
                    kept.append(p)
            candidate_pairs = kept

        # v15 earnings-window blackout: drop signals on tickers within
        # [-N, +M] days of their scheduled earnings announcement. Public
        # schedule, not a leak.
        if cfg.skip_earnings_window and candidate_pairs:
            kept = []
            for p in candidate_pairs:
                if is_earnings_window(p["ticker"], date,
                                      days_before=cfg.earnings_days_before,
                                      days_after=cfg.earnings_days_after):
                    earnings_signals_dropped += 1
                else:
                    kept.append(p)
            candidate_pairs = kept

        # v9 portfolio-level constraints (re-baseline to $500/day cap):
        #   1. Concurrent risk-dollars cap: sum of open risk_dollars
        #      <= ORB_MAX_CONCURRENT_RISK_DOLLARS (default $500).
        #      This bounds the worst-case stop-cascade loss to that cap.
        #   2. Concurrent notional cap (legacy from v8, still in force).
        #   3. Daily loss kill: halt new entries after cumulative
        #      realized day P&L <= -daily_loss_kill_pct of account.
        max_notional = current_account * cfg.max_concurrent_notional_mult
        max_risk_budget = cfg.max_concurrent_risk_dollars
        kill_threshold = -current_account * cfg.daily_loss_kill_pct / 100.0
        events = []
        for idx, p in enumerate(candidate_pairs):
            ent_ts = p["entry_ts"]
            ext_ts = p["exit_ts"]
            notional = p["entry_price"] * p["shares"]
            # Risk-dollars per trade is always written by run_ticker_day
            # (= shares * |entry - stop|). Assert it's present so any
            # future schema drift fails loudly instead of silently
            # falling back to a wrong risk-budget proxy.
            assert "risk_dollars" in p, (
                f"pnl_pair missing required 'risk_dollars' field: "
                f"{p.get('ticker')} @ {p.get('entry_ts')} -- "
                f"run_ticker_day must always populate this field"
            )
            risk = p["risk_dollars"]
            events.append((ent_ts, "entry", idx, p, notional, risk))
            events.append((ext_ts, "exit", idx, p, notional, risk))
        # Sort by ts; "exit" before "entry" at same ts to free room
        events.sort(key=lambda e: (e[0], 0 if e[1] == "exit" else 1))

        accepted_idx: set[int] = set()
        rejected_idx: set[int] = set()
        open_notional = 0.0
        open_risk = 0.0
        cum_pnl = 0.0
        peak_pnl = 0.0
        kill_active = False
        # v18 day-end-giveback defenses
        #   locked_pairs[(ticker, side)] = ts when the pair was locked.
        #   New entries on a locked pair after that ts are rejected.
        locked_pairs: dict[tuple[str, str], int] = {}
        r18_lock_rejects = 0
        for ts, kind, idx, p, notional, risk in events:
            if kind == "entry":
                if kill_active:
                    rejected_idx.add(idx)
                    continue
                # v18 rule #1 -- per-(ticker, side) lock after a losing leg
                if cfg.loss_lock_threshold_usd > 0:
                    key = (p["ticker"], p["side"])
                    if key in locked_pairs and locked_pairs[key] < ts:
                        rejected_idx.add(idx)
                        r18_lock_rejects += 1
                        continue
                if open_notional + notional > max_notional:
                    rejected_idx.add(idx)
                    continue
                if open_risk + risk > max_risk_budget:
                    rejected_idx.add(idx)
                    continue
                accepted_idx.add(idx)
                open_notional += notional
                open_risk += risk
            else:  # exit
                if idx in accepted_idx:
                    open_notional -= notional
                    open_risk -= risk
                    cum_pnl += p["pnl_dollars"]
                    if cum_pnl > peak_pnl:
                        peak_pnl = cum_pnl
                    if cum_pnl <= kill_threshold:
                        kill_active = True
                    # v18 rule #1 lock: this leg ended with a loss large
                    # enough to lock the (ticker, side) for the rest of
                    # the day.
                    if (cfg.loss_lock_threshold_usd > 0
                            and p["pnl_dollars"] < -cfg.loss_lock_threshold_usd):
                        locked_pairs[(p["ticker"], p["side"])] = ts
                    # v18 rule #2 halt: realized drawdown from peak crossed
                    # the threshold. Same effect as kill_active but a
                    # different trigger.
                    if (cfg.peak_dd_halt_usd > 0
                            and not kill_active
                            and cum_pnl <= peak_pnl - cfg.peak_dd_halt_usd):
                        kill_active = True

        day_pairs = [
            p for i, p in enumerate(candidate_pairs) if i in accepted_idx
        ]
        rejected = len(candidate_pairs) - len(day_pairs)

        n_entries = len(day_pairs)
        wins = sum(1 for p in day_pairs if p["pnl_dollars"] > 0)
        losses = sum(1 for p in day_pairs if p["pnl_dollars"] <= 0)
        day_pnl = sum(p["pnl_dollars"] for p in day_pairs)
        total_pnl += day_pnl
        # v11 compounding: update running balance with this day's P&L.
        if cfg.compound_daily:
            running_account += day_pnl
            daily_account_history.append({
                "date": date,
                "open_balance": round(current_account, 2),
                "day_pnl": round(day_pnl, 2),
                "close_balance": round(running_account, 2),
            })
        total_entries += n_entries
        total_wins += wins
        total_losses += losses
        days_ok += 1

        per_day_dir.joinpath(f"{date}.json").write_text(json.dumps({
            "date": date,
            "tickers": list(tickers),
            "entries": [{"ts": p["entry_ts"], "ticker": p["ticker"],
                         "side": p["side"], "price": p["entry_price"]}
                        for p in day_pairs],
            "exits": [{"ts": p["exit_ts"], "ticker": p["ticker"],
                       "side": p["side"], "exit_price": p["exit_price"],
                       "reason": p["exit_reason"]} for p in day_pairs],
            "pnl_pairs": day_pairs,
            "rejected_concurrent_cap": rejected,
            "kill_switch_fired": kill_active,
            # v18 diagnostics
            "r18_lock_rejects": r18_lock_rejects,
            "r18_locked_pairs": [list(k) for k in locked_pairs.keys()],
            "r18_peak_pnl": round(peak_pnl, 2),
            "regime": regime,
            "regime_skip_day": regime_skip_day,
            "regime_skip_reason": regime_skip_reason,
            "summary": {
                "entries": n_entries, "exits": n_entries,
                "wins": wins, "losses": losses,
                "total_pnl": round(day_pnl, 4),
                "pairs_missing_shares": 0,
            },
            "_orb_strategy": True,
        }, indent=2))

    closed = total_wins + total_losses
    summary = {
        "variant": vid,
        "universe": list(tickers),
        "earnings_layer": "none",
        "days_planned": len(dates),
        "days_ran": days_ok,
        "days_resumed_skip": 0,
        "days_ok": days_ok,
        "days_failed": days_failed,
        # H2 fix: surface per-ticker failures so they don't get silently
        # swallowed. tickers_failed counts every ticker-day that raised
        # a non-bug exception inside run_ticker_day; failures preserves
        # the first 10 examples for triage.
        "tickers_failed": tickers_failed,
        "failures": failures,
        "net_pnl": round(total_pnl, 2),
        "entries": total_entries,
        "exits": total_entries,
        "wins": total_wins,
        "losses": total_losses,
        "win_rate_pct": round(100 * total_wins / closed, 2) if closed else None,
        "wall_min": 0,
        # v11 compounding fields
        "starting_account": round(starting_account, 2),
        "ending_account": round(running_account, 2) if cfg.compound_daily else round(starting_account + total_pnl, 2),
        "compound_daily": cfg.compound_daily,
        # v12 regime-gate diagnostics
        "regime_ticker": cfg.regime_ticker,
        "regime_dir_align": cfg.regime_dir_align,
        "regime_min_or_bps": cfg.regime_min_or_bps,
        "regime_days_skipped_flat": regime_days_skipped_flat,
        "regime_days_skipped_low_or": regime_days_skipped_low_or,
        "regime_signals_dropped": regime_signals_dropped,
        # v13 RVOL gate diagnostics
        "require_rvol_above": cfg.require_rvol_above,
        "rvol_lookback_days": cfg.rvol_lookback_days,
        "rvol_signals_dropped": rvol_signals_dropped,
        "rvol_insufficient_history": rvol_insufficient_history,
        # v14 prior-day filter diagnostics
        "skip_gap_above_pct": cfg.skip_gap_above_pct,
        "require_prior_nr_n": cfg.require_prior_nr_n,
        "skip_prior_wr_n": cfg.skip_prior_wr_n,
        "gap_signals_dropped": gap_signals_dropped,
        "nr_signals_dropped": nr_signals_dropped,
        "wr_signals_dropped": wr_signals_dropped,
        "skip_earnings_window": cfg.skip_earnings_window,
        "earnings_signals_dropped": earnings_signals_dropped,
        # v16 VIX gate diagnostics
        "skip_vix_above": cfg.skip_vix_above,
        "vix_days_skipped": vix_days_skipped,
        "config": {
            "strategy": "orb_classical",
            "or_minutes": cfg.or_minutes,
            "rr": cfg.rr,
            "stop_buffer_bps": cfg.stop_buffer_bps,
            "time_cutoff_et": _minutes_to_et(cfg.time_cutoff_et),
            "eod_cutoff_et": _minutes_to_et(cfg.eod_cutoff_et),
            "range_min_pct": cfg.range_min_pct,
            "range_max_pct": cfg.range_max_pct,
            "volume_mult": cfg.volume_mult,
            "max_trades_per_day": cfg.max_trades_per_day,
            "risk_per_trade_pct": cfg.risk_per_trade_pct,
            "account": cfg.account,
            "blocklist": cfg.blocklist,
        },
    }
    out_dir.joinpath("summary.json").write_text(
        json.dumps(summary, indent=2)
    )
    return summary


def _minutes_to_et(m: int) -> str:
    h, mm = divmod(m, 60)
    return f"{h:02d}:{mm:02d}"


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--corpus", default="data",
                   help="Corpus root (e.g. 'data' or '/home/.../data')")
    p.add_argument("--out", required=True, help="Output dir")
    p.add_argument("--vid", default="orb_classical_15min")
    p.add_argument("--tickers", default="AAPL,MSFT,NVDA,TSLA,META,GOOG,AMZN,AVGO,NFLX,ORCL")
    p.add_argument("--year-prefix", default="2026-")
    p.add_argument("--max-dates", type=int, default=0)
    args = p.parse_args(argv[1:])

    corpus = Path(args.corpus)
    if not corpus.is_dir():
        print(f"ERROR: corpus dir {corpus} not found", file=sys.stderr)
        return 1

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    cfg = ORBConfig.from_env()

    dates = discover_dates(corpus, args.year_prefix, tickers)
    # v8 -- honor DATES_STRIDE env var for cross-validation sweeps. The
    # GHA matrix wrapper passes stride from the trigger JSON via this env
    # var. Without this, STRIDE=2/4/8 variants silently ran on the full
    # corpus, producing byte-identical results to STRIDE=1 -- making
    # cross-validation impossible.
    stride = max(1, int(os.environ.get("DATES_STRIDE", "1")))
    if stride > 1:
        dates = dates[::stride]
        print(f"ORB: DATES_STRIDE={stride} -> {len(dates)} dates",
              file=sys.stderr, flush=True)
    if args.max_dates > 0:
        dates = dates[:args.max_dates]
    if not dates:
        print("ERROR: no eligible dates found", file=sys.stderr)
        return 1

    print(f"ORB: {len(dates)} dates, {len(tickers)} tickers, "
          f"OR={cfg.or_minutes}m, RR={cfg.rr}, stride={stride}, "
          f"range=[{cfg.range_min_pct:.3f},{cfg.range_max_pct:.3f}]")

    summary = run(corpus, Path(args.out), dates, tickers, cfg, args.vid)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
