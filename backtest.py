#!/usr/bin/env python3
"""
Stock Spike Monitor — 30-Day Backtest Engine
=============================================
Standalone script that replays the bot's paper-trading signal engine
against historical market data and produces a comprehensive PDF report.

Usage:
    python backtest.py                    # default 30 trading days
    python backtest.py --days 60          # custom look-back
    python backtest.py --capital 50000    # custom starting capital

Output:
    backtest_report.pdf   — full visual report
"""

import argparse
import math
import os
import sys
import warnings
from collections import defaultdict, deque
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.gridspec import GridSpec
import numpy as np
import yfinance as yf
import requests

warnings.filterwarnings("ignore", category=FutureWarning)

# ────────────────────────────────────────────────────────────────
# CONFIGURATION  (mirrors stock_spike_monitor.py defaults)
# ────────────────────────────────────────────────────────────────
CORE_TICKERS = [
    "NVDA","TSLA","AMD","AAPL","AMZN","META","MSFT","GOOGL","SMCI","ARM",
    "MU","AVGO","QCOM","INTC","HIMS","PLTR","SOFI","RIVN","NIO","MARA",
    "AMC","GME","LCID","BYND","PFE","BAC","JPM","XOM","CVX","AAL",
]

TICKER_SECTORS = {
    "AAPL": "Technology", "MSFT": "Technology", "GOOGL": "Technology",
    "AMZN": "Consumer Discretionary", "META": "Technology", "NVDA": "Technology",
    "TSLA": "Consumer Discretionary", "AMD": "Technology", "INTC": "Technology",
    "JPM": "Financials", "BAC": "Financials",
    "XOM": "Energy", "CVX": "Energy",
    "PFE": "Healthcare", "HIMS": "Healthcare",
    "AVGO": "Technology", "QCOM": "Technology", "MU": "Technology",
    "ARM": "Technology", "SMCI": "Technology", "PLTR": "Technology",
    "SOFI": "Financials", "MARA": "Financials",
    "RIVN": "Consumer Discretionary", "NIO": "Consumer Discretionary",
    "LCID": "Consumer Discretionary", "GME": "Consumer Discretionary",
    "AMC": "Communication", "BYND": "Consumer Staples", "AAL": "Industrials",
}

SECTOR_ETF = {
    "Technology": "XLK", "Financials": "XLF", "Energy": "XLE",
    "Healthcare": "XLV", "Industrials": "XLI", "Communication": "XLC",
    "Consumer Discretionary": "XLY", "Consumer Staples": "XLP",
}

# Base trading parameters (user_config defaults)
BASE_THRESHOLD  = 65
BASE_TP         = 0.10    # 10% take-profit
BASE_SL         = 0.06    # 6% hard stop
BASE_TRAIL      = 0.03    # 3% trailing stop
BASE_MAX_POS    = 8
MAX_POS_PCT     = 0.20    # 20% of portfolio per position
MAX_ACTIONS_DAY = 3       # per ticker per day
STARTING_CAPITAL = 100_000.0

# ────────────────────────────────────────────────────────────────
# TECHNICAL INDICATOR FUNCTIONS (exact copies from bot)
# ────────────────────────────────────────────────────────────────
def compute_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def compute_bollinger(prices, period=20, num_std=2.0):
    if len(prices) < period:
        return None, None, None, None, None
    window = prices[-period:]
    mid    = sum(window) / period
    var    = sum((p - mid) ** 2 for p in window) / period
    std    = math.sqrt(var)
    upper  = mid + num_std * std
    lower  = mid - num_std * std
    price  = prices[-1]
    pct_b  = (price - lower) / (upper - lower) if (upper - lower) != 0 else 0.5
    bw     = (upper - lower) / mid if mid != 0 else 0
    return round(mid, 2), round(upper, 2), round(lower, 2), round(pct_b, 3), round(bw, 4)


def _compute_ema(prices, period):
    if len(prices) < period:
        return None
    k   = 2.0 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema


def _compute_macd(prices):
    if len(prices) < 26:
        return None, None, None
    ema12 = _compute_ema(prices, 12)
    ema26 = _compute_ema(prices, 26)
    if ema12 is None or ema26 is None:
        return None, None, None
    macd = ema12 - ema26
    macd_series = []
    for i in range(26, len(prices) + 1):
        e12 = _compute_ema(prices[:i], 12)
        e26 = _compute_ema(prices[:i], 26)
        if e12 and e26:
            macd_series.append(e12 - e26)
    if len(macd_series) >= 9:
        sig = _compute_ema(macd_series, 9)
        hist = macd - sig if sig else 0
        return round(macd, 4), round(sig, 4), round(hist, 4)
    return round(macd, 4), None, None


def compute_squeeze_score_from_prices(prices):
    """Squeeze score from a price window (no API calls)."""
    rsi            = compute_rsi(prices)
    _, _, _, pct_b, bw = compute_bollinger(prices)
    score      = 0
    if rsi is not None:
        rsi_pts = max(0, 30 - abs(rsi - 40))
        score  += rsi_pts
    if bw is not None:
        bw_pts = max(0, 25 * (1 - bw / 0.1)) if bw < 0.1 else 0
        score += bw_pts
    if pct_b is not None:
        pb_pts = max(0, 20 * (1 - pct_b)) if pct_b < 0.5 else 0
        score += pb_pts
    if len(prices) >= 10:
        recent_avg = sum(prices[-5:]) / 5
        prior_avg  = sum(prices[-10:-5]) / 5
        if prior_avg > 0:
            vol_trend = (recent_avg - prior_avg) / prior_avg
            vt_pts    = min(15, max(0, vol_trend * 100))
            score    += vt_pts
    return round(min(score, 100), 1)


# ────────────────────────────────────────────────────────────────
# SIGNAL ENGINE  (replicates compute_paper_signal for backtesting)
# ────────────────────────────────────────────────────────────────
def compute_signal(prices_window, daily_candles_window, volume_today, avg_volume_10d):
    """
    Compute the composite signal score using the 7 fully-backtestable
    components plus the support/resistance modifier.

    Components replicated:
      1. RSI Momentum          — 20 pts max
      2. Bollinger Band %B     — 15 pts max
      3. MACD Crossover        — 15 pts max
      4. Volume Confirmation   — 15 pts max
      5. Squeeze Score         — 10 pts max
      6. Price Slope           — 10 pts max
      7. Multi-Day Trend       — 15 pts max  (SMA 6 + Mom 5 + DailyVol 4)
      S/R modifier             — ±5 pts

    NOT replicated (neutral estimate = 0 pts each):
      - Claude AI Direction    (15 pts)
      - AI Watchlist Conviction(10 pts)
      - News Sentiment         (15 pts)

    Theoretical max from backtestable components: 105 (+ 5 from S/R)
    Live bot max: 140 (+ 5 from S/R)
    """
    score = 0
    detail = {}

    # ── 1. RSI Momentum (20 pts) ──────────────────────
    rsi = compute_rsi(prices_window)
    if rsi is not None:
        if 50 <= rsi <= 65:      pts = 20
        elif 65 < rsi <= 72:     pts = 10
        elif 40 <= rsi < 50:     pts = 8
        else:                    pts = 0
        score += pts
        detail["rsi"] = rsi
        detail["rsi_pts"] = pts

    # ── 2. Bollinger Band %B (15 pts) ─────────────────
    _, _, _, pct_b, bw = compute_bollinger(prices_window)
    if pct_b is not None:
        if 0.5 <= pct_b <= 0.85:     pts = 15
        elif 0.85 < pct_b <= 1.0:    pts = 8
        elif 0.3 <= pct_b < 0.5:     pts = 10
        else:                        pts = max(0, int(pct_b * 10))
        score += pts
        detail["pct_b"] = pct_b
        detail["bb_pts"] = pts

    # ── 3. MACD Crossover (15 pts) ────────────────────
    macd_line, sig_line, hist_val = _compute_macd(prices_window)
    if macd_line is not None:
        if macd_line > 0 and (sig_line is None or macd_line > sig_line):
            pts = 15
        elif macd_line > 0:
            pts = 8
        elif hist_val is not None and hist_val > 0:
            pts = 5
        else:
            pts = 0
        score += pts
        detail["macd"] = macd_line
        detail["macd_pts"] = pts

    # ── 4. Volume Confirmation (15 pts) ───────────────
    if volume_today and avg_volume_10d and avg_volume_10d > 0:
        ratio = volume_today / avg_volume_10d
        if ratio >= 2.0:       pts = 15
        elif ratio >= 1.5:     pts = 10
        elif ratio >= 1.0:     pts = 5
        else:                  pts = 0
        score += pts
        detail["vol_ratio"] = round(ratio, 2)
        detail["vol_pts"] = pts

    # ── 5. Squeeze Score (10 pts) ─────────────────────
    sq = compute_squeeze_score_from_prices(prices_window)
    sq_pts = round(sq / 10, 1)
    score += sq_pts
    detail["squeeze"] = sq
    detail["sq_pts"] = sq_pts

    # ── 6. Price Slope (10 pts) ───────────────────────
    if len(prices_window) >= 10:
        xs = list(range(10))
        ys = prices_window[-10:]
        n  = 10
        x_mean = sum(xs) / n
        y_mean = sum(ys) / n
        num = sum((xs[i] - x_mean) * (ys[i] - y_mean) for i in range(n))
        den = sum((xs[i] - x_mean) ** 2 for i in range(n))
        slope = num / den if den != 0 else 0
        slope_pct = slope / y_mean * 100 if y_mean else 0
        if slope_pct >= 0.3:     pts = 10
        elif slope_pct >= 0.1:   pts = 6
        elif slope_pct >= 0:     pts = 2
        else:                    pts = 0
        score += pts
        detail["slope_pct"] = round(slope_pct, 3)
        detail["slope_pts"] = pts

    # ── 7. Multi-Day Trend (15 pts) ───────────────────
    if daily_candles_window and len(daily_candles_window) >= 10:
        closes  = [d["close"] for d in daily_candles_window]
        volumes = [d["volume"] for d in daily_candles_window]

        # 7a. SMA Trend (6 pts)
        sma5  = sum(closes[-5:]) / 5
        sma20 = sum(closes[-min(20, len(closes)):]) / min(20, len(closes))
        cur   = closes[-1]
        sma_pts = 0
        if cur > sma5 > sma20:          sma_pts = 6
        elif cur > sma5 and sma5 <= sma20: sma_pts = 3
        elif cur < sma5 and sma5 > sma20:  sma_pts = 1
        score += sma_pts

        # 7b. Multi-Day Momentum (5 pts)
        mom_pts = 0
        if len(closes) >= 6:
            ret_5d = (closes[-1] - closes[-6]) / closes[-6]
            if -0.08 <= ret_5d < -0.03:     mom_pts = 5
            elif 0.01 <= ret_5d <= 0.05:    mom_pts = 4
            elif 0.05 < ret_5d <= 0.10:     mom_pts = 2
            elif -0.03 <= ret_5d < 0.01:    mom_pts = 1
            score += mom_pts

        # 7c. Daily Volume Trend (4 pts)
        avg_dv = sum(volumes[-10:]) / min(10, len(volumes[-10:]))
        today_dv = volumes[-1]
        vol_d_pts = 0
        if avg_dv > 0:
            vdr = today_dv / avg_dv
            if vdr >= 1.5:     vol_d_pts = 4
            elif vdr >= 1.2:   vol_d_pts = 2
        score += vol_d_pts

        detail["multi_day_pts"] = sma_pts + mom_pts + vol_d_pts

    # ── Support / Resistance modifier (±5 pts) ────────
    if daily_candles_window and len(daily_candles_window) >= 10:
        highs = [d["high"] for d in daily_candles_window[-20:]]
        lows  = [d["low"]  for d in daily_candles_window[-20:]]
        resistance = max(highs)
        support    = min(lows)
        cur_price  = prices_window[-1] if prices_window else None
        if cur_price and resistance > 0 and abs(cur_price - resistance) / resistance <= 0.01:
            score -= 5
            detail["sr_mod"] = -5
        elif cur_price and support > 0 and abs(cur_price - support) / support <= 0.01 and cur_price > support:
            score += 5
            detail["sr_mod"] = 5

    return round(min(score, 110), 1), detail


# ────────────────────────────────────────────────────────────────
# ADAPTIVE THRESHOLD (replicated from _apply_adaptive_config)
# ────────────────────────────────────────────────────────────────
def adaptive_params(fg, vix):
    """Return (threshold, tp, sl, trail, max_pos) given Fear&Greed and VIX."""
    threshold = BASE_THRESHOLD
    if fg >= 75:    threshold += 10
    elif fg >= 60:  threshold += 5
    elif fg <= 25:  threshold -= 10
    elif fg <= 40:  threshold -= 5
    if vix >= 30:   threshold += 5
    elif vix <= 15: threshold -= 3
    threshold = max(45, min(85, threshold))

    tp = BASE_TP
    if fg <= 25:      tp *= 1.3
    elif fg <= 40:    tp *= 1.15
    elif fg >= 75:    tp *= 0.85
    elif fg >= 60:    tp *= 0.9
    tp = round(max(0.05, min(0.20, tp)), 3)

    sl = BASE_SL
    if vix >= 30:     sl *= 1.3
    elif vix >= 25:   sl *= 1.15
    elif vix <= 15:   sl *= 0.85
    sl = round(max(0.03, min(0.12, sl)), 3)

    trail = BASE_TRAIL
    if vix >= 30:     trail *= 1.3
    elif vix >= 25:   trail *= 1.15
    elif vix <= 15:   trail *= 0.85
    trail = round(max(0.02, min(0.08, trail)), 3)

    max_pos = BASE_MAX_POS
    if fg <= 25:      max_pos = min(max_pos + 3, 15)
    elif fg <= 40:    max_pos = min(max_pos + 1, 12)
    elif fg >= 75:    max_pos = max(max_pos - 2, 4)
    elif fg >= 60:    max_pos = max(max_pos - 1, 5)

    return threshold, tp, sl, trail, max_pos


# ────────────────────────────────────────────────────────────────
# DATA FETCHING
# ────────────────────────────────────────────────────────────────
def fetch_historical_data(tickers, lookback_calendar_days):
    """
    Download daily OHLCV for each ticker + VIX + sector ETFs.
    Returns {ticker: DataFrame} with columns [Open,High,Low,Close,Volume].
    """
    # Add extra days for indicator warm-up (RSI needs ~26 bars, MACD needs 35+)
    extra = 50
    total_days = lookback_calendar_days + extra
    end   = datetime.now()
    start = end - timedelta(days=total_days)

    all_syms = list(set(
        tickers
        + ["^VIX"]
        + list(SECTOR_ETF.values())
    ))
    print(f"  Downloading daily data for {len(all_syms)} symbols...")
    data = {}
    # Download in batches to be more reliable
    batch_size = 10
    for i in range(0, len(all_syms), batch_size):
        batch = all_syms[i:i+batch_size]
        batch_str = " ".join(batch)
        try:
            df = yf.download(batch_str, start=start.strftime("%Y-%m-%d"),
                             end=end.strftime("%Y-%m-%d"), progress=False,
                             group_by="ticker", auto_adjust=True)
            if len(batch) == 1:
                sym = batch[0]
                if not df.empty:
                    data[sym] = df[["Open","High","Low","Close","Volume"]].dropna()
            else:
                for sym in batch:
                    try:
                        sdf = df[sym][["Open","High","Low","Close","Volume"]].dropna()
                        if not sdf.empty:
                            data[sym] = sdf
                    except (KeyError, TypeError):
                        pass
        except Exception as e:
            print(f"    Warning: batch download failed for {batch}: {e}")

    print(f"  Got data for {len(data)} symbols")
    return data


def fetch_fear_greed_historical(days):
    """Fetch historical Fear & Greed index values."""
    try:
        r = requests.get(f"https://api.alternative.me/fng/?limit={days}", timeout=15)
        entries = r.json().get("data", [])
        # Returns newest first; reverse to chronological
        result = {}
        for e in entries:
            ts = int(e["timestamp"])
            dt = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
            result[dt] = int(e["value"])
        return result
    except Exception as e:
        print(f"  Warning: Could not fetch Fear & Greed history: {e}")
        return {}


# ────────────────────────────────────────────────────────────────
# BACKTEST ENGINE
# ────────────────────────────────────────────────────────────────
class BacktestEngine:
    def __init__(self, starting_capital, data, fg_history, trading_days):
        self.starting_capital = starting_capital
        self.cash = starting_capital
        self.data = data                   # {sym: DataFrame}
        self.fg_history = fg_history       # {date_str: int}
        self.trading_days = trading_days   # list of date strings to simulate
        self.positions = {}                # {ticker: {shares, avg_cost, high, entry_date}}
        self.trades = []                   # all completed trades
        self.daily_values = []             # [(date, portfolio_value)]
        self.daily_actions = defaultdict(int)   # "TICKER:date" -> count
        self.sector_counts = defaultdict(int)
        self.daily_params = []             # [(date, threshold, tp, sl, trail, max_pos)]
        self.signals_log = []              # for analysis

    def portfolio_value(self, date_str):
        total = self.cash
        for ticker, pos in self.positions.items():
            price = self._get_close(ticker, date_str)
            if price:
                total += pos["shares"] * price
            else:
                total += pos["shares"] * pos["avg_cost"]
        return total

    def _get_close(self, ticker, date_str):
        df = self.data.get(ticker)
        if df is None:
            return None
        try:
            # Find the closest date on or before date_str
            mask = df.index <= date_str
            if mask.any():
                row = df.loc[mask].iloc[-1]
                return float(row["Close"])
        except Exception:
            pass
        return None

    def _get_daily_candles(self, ticker, date_str, lookback=30):
        """Get up to `lookback` daily candle dicts ending on date_str."""
        df = self.data.get(ticker)
        if df is None:
            return []
        try:
            mask = df.index <= date_str
            sub  = df.loc[mask].tail(lookback)
            return [
                {"open": float(r.Open), "high": float(r.High),
                 "low": float(r.Low), "close": float(r.Close),
                 "volume": float(r.Volume)}
                for _, r in sub.iterrows()
            ]
        except Exception:
            return []

    def _get_price_window(self, ticker, date_str, lookback=60):
        """Get list of close prices ending on date_str (for intraday-like window)."""
        df = self.data.get(ticker)
        if df is None:
            return []
        try:
            mask = df.index <= date_str
            sub  = df.loc[mask].tail(lookback)
            return [float(c) for c in sub["Close"]]
        except Exception:
            return []

    def _get_volume_data(self, ticker, date_str):
        """Return (today_volume, 10d_avg_volume)."""
        df = self.data.get(ticker)
        if df is None:
            return None, None
        try:
            mask = df.index <= date_str
            sub  = df.loc[mask].tail(11)
            if len(sub) < 2:
                return None, None
            today_vol = float(sub.iloc[-1]["Volume"])
            avg_vol   = float(sub.iloc[:-1]["Volume"].mean()) if len(sub) > 1 else today_vol
            return today_vol, avg_vol
        except Exception:
            return None, None

    def _get_vix(self, date_str):
        df = self.data.get("^VIX")
        if df is None:
            return 20.0
        try:
            mask = df.index <= date_str
            if mask.any():
                return float(df.loc[mask].iloc[-1]["Close"])
        except Exception:
            pass
        return 20.0

    def _get_sector_change(self, ticker, date_str):
        """Return sector ETF daily % change for this ticker's sector."""
        sector = TICKER_SECTORS.get(ticker)
        if not sector:
            return 0.0
        etf = SECTOR_ETF.get(sector)
        if not etf:
            return 0.0
        df = self.data.get(etf)
        if df is None:
            return 0.0
        try:
            mask = df.index <= date_str
            sub  = df.loc[mask].tail(2)
            if len(sub) >= 2:
                prev_c = float(sub.iloc[-2]["Close"])
                cur_c  = float(sub.iloc[-1]["Close"])
                if prev_c > 0:
                    return (cur_c - prev_c) / prev_c * 100
        except Exception:
            pass
        return 0.0

    def _recalc_sector_counts(self):
        self.sector_counts.clear()
        for t in self.positions:
            s = TICKER_SECTORS.get(t, "Other")
            self.sector_counts[s] += 1

    def run(self):
        print(f"\n  Simulating {len(self.trading_days)} trading days across {len(CORE_TICKERS)} tickers...\n")
        for day_idx, date_str in enumerate(self.trading_days):
            fg  = self.fg_history.get(date_str, 50)
            vix = self._get_vix(date_str)
            threshold, tp, sl, trail, max_pos = adaptive_params(fg, vix)
            self.daily_params.append((date_str, threshold, tp, sl, trail, max_pos, fg, vix))

            # ── Check existing positions for exits ──
            tickers_to_sell = []
            for ticker in list(self.positions.keys()):
                pos   = self.positions[ticker]
                price = self._get_close(ticker, date_str)
                if not price:
                    continue

                cost    = pos["avg_cost"]
                pnl_pct = (price - cost) / cost

                # Update high-water mark
                if price > pos.get("high", cost):
                    self.positions[ticker]["high"] = price

                should_sell = False
                sell_reason = ""

                if pnl_pct >= tp:
                    should_sell = True
                    sell_reason = f"TAKE-PROFIT +{pnl_pct*100:.1f}%"
                elif pnl_pct <= -sl:
                    should_sell = True
                    sell_reason = f"HARD-STOP {pnl_pct*100:.1f}%"
                elif price <= pos.get("high", cost) * (1 - trail):
                    should_sell = True
                    peak_pnl = ((pos.get("high", cost) - cost) / cost) * 100
                    sell_reason = f"TRAILING-STOP {pnl_pct*100:+.1f}% (peak +{peak_pnl:.1f}%)"
                else:
                    # Signal collapse check
                    pw = self._get_price_window(ticker, date_str)
                    dc = self._get_daily_candles(ticker, date_str)
                    tv, av = self._get_volume_data(ticker, date_str)
                    sig_score, _ = compute_signal(pw, dc, tv, av)
                    if sig_score <= 30 and pnl_pct > 0:
                        should_sell = True
                        sell_reason = f"SIGNAL-COLLAPSE score={sig_score:.0f} pnl={pnl_pct*100:+.1f}%"

                if should_sell:
                    tickers_to_sell.append((ticker, price, sell_reason, pnl_pct))

            for ticker, price, reason, pnl_pct in tickers_to_sell:
                pos = self.positions[ticker]
                shares   = pos["shares"]
                proceeds = shares * price
                cost_b   = shares * pos["avg_cost"]
                realized = proceeds - cost_b
                self.cash += proceeds
                entry_date = pos["entry_date"]
                del self.positions[ticker]
                self._recalc_sector_counts()
                self.trades.append({
                    "action": "SELL", "ticker": ticker, "shares": shares,
                    "price": price, "pnl": realized, "pnl_pct": pnl_pct * 100,
                    "reason": reason, "date": date_str, "entry_date": entry_date,
                    "portfolio_value": self.portfolio_value(date_str),
                })
                key = f"{ticker}:{date_str}"
                self.daily_actions[key] = self.daily_actions.get(key, 0) + 1

            # ── Check for new buys ──
            for ticker in CORE_TICKERS:
                if ticker in self.positions:
                    continue
                if len(self.positions) >= max_pos:
                    break
                if self.cash < 200:
                    break

                key = f"{ticker}:{date_str}"
                if self.daily_actions.get(key, 0) >= MAX_ACTIONS_DAY:
                    continue

                price = self._get_close(ticker, date_str)
                if not price or price <= 0:
                    continue

                pw = self._get_price_window(ticker, date_str)
                dc = self._get_daily_candles(ticker, date_str)
                tv, av = self._get_volume_data(ticker, date_str)

                sig_score, sig_detail = compute_signal(pw, dc, tv, av)

                # Sector rotation modifier
                sector_chg = self._get_sector_change(ticker, date_str)
                if sector_chg < -1.5:
                    sig_score -= 5
                elif sector_chg > 1.0:
                    sig_score += 3

                if sig_score < threshold:
                    continue

                # RSI overbought guard
                rsi = sig_detail.get("rsi")
                if rsi and rsi > 72:
                    continue

                # Sector concentration guard (max 2 per sector)
                sector = TICKER_SECTORS.get(ticker)
                if sector and self.sector_counts.get(sector, 0) >= 2:
                    continue

                # Position sizing
                pv = self.portfolio_value(date_str)
                max_dollars = pv * MAX_POS_PCT
                strength = min(1.0, (sig_score - threshold) / max(1, (100 - threshold)))
                dollars  = max_dollars * (0.5 + 0.5 * strength)
                dollars  = min(dollars, self.cash * 0.95)
                if dollars < 100:
                    continue
                shares = max(1, int(dollars / price))
                cost   = shares * price

                self.cash -= cost
                self.positions[ticker] = {
                    "shares": shares, "avg_cost": price,
                    "high": price, "entry_date": date_str,
                }
                self._recalc_sector_counts()
                self.daily_actions[key] = self.daily_actions.get(key, 0) + 1

                self.trades.append({
                    "action": "BUY", "ticker": ticker, "shares": shares,
                    "price": price, "cost": cost, "signal_score": sig_score,
                    "date": date_str,
                    "portfolio_value": self.portfolio_value(date_str),
                })

            # Record end-of-day portfolio value
            pv = self.portfolio_value(date_str)
            self.daily_values.append((date_str, pv))

            if (day_idx + 1) % 5 == 0 or day_idx == len(self.trading_days) - 1:
                print(f"    Day {day_idx+1}/{len(self.trading_days)} "
                      f"({date_str}): ${pv:,.0f}  "
                      f"({len(self.positions)} positions, "
                      f"thresh={threshold}, F&G={fg}, VIX={vix:.1f})")

        # Close remaining positions at final prices
        final_date = self.trading_days[-1]
        for ticker in list(self.positions.keys()):
            pos   = self.positions[ticker]
            price = self._get_close(ticker, final_date)
            if not price:
                price = pos["avg_cost"]
            shares   = pos["shares"]
            proceeds = shares * price
            cost_b   = shares * pos["avg_cost"]
            realized = proceeds - cost_b
            pnl_pct  = (price - pos["avg_cost"]) / pos["avg_cost"] * 100
            self.cash += proceeds
            self.trades.append({
                "action": "SELL", "ticker": ticker, "shares": shares,
                "price": price, "pnl": realized, "pnl_pct": pnl_pct,
                "reason": "END-OF-BACKTEST", "date": final_date,
                "entry_date": pos["entry_date"],
                "portfolio_value": self.portfolio_value(final_date),
            })
        self.positions.clear()

    def get_results(self):
        buys  = [t for t in self.trades if t["action"] == "BUY"]
        sells = [t for t in self.trades if t["action"] == "SELL"]
        winners = [t for t in sells if t.get("pnl", 0) > 0]
        losers  = [t for t in sells if t.get("pnl", 0) <= 0]

        total_pnl = sum(t.get("pnl", 0) for t in sells)
        final_val = self.daily_values[-1][1] if self.daily_values else self.starting_capital

        # Drawdown calculation
        peak = self.starting_capital
        max_dd = 0
        max_dd_date = ""
        for d, v in self.daily_values:
            if v > peak:
                peak = v
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd
                max_dd_date = d

        # Per-ticker summary
        ticker_pnl = defaultdict(lambda: {"trades": 0, "pnl": 0, "wins": 0})
        for t in sells:
            tk = t["ticker"]
            ticker_pnl[tk]["trades"] += 1
            ticker_pnl[tk]["pnl"] += t.get("pnl", 0)
            if t.get("pnl", 0) > 0:
                ticker_pnl[tk]["wins"] += 1

        # Sell reason breakdown
        reason_counts = defaultdict(int)
        for t in sells:
            r = t.get("reason", "")
            for key in ["TAKE-PROFIT", "HARD-STOP", "TRAILING-STOP", "SIGNAL-COLLAPSE", "END-OF-BACKTEST"]:
                if key in r:
                    reason_counts[key] += 1
                    break

        # Average hold time
        hold_days = []
        for t in sells:
            ed = t.get("entry_date")
            sd = t.get("date")
            if ed and sd:
                try:
                    d1 = datetime.strptime(ed, "%Y-%m-%d")
                    d2 = datetime.strptime(sd, "%Y-%m-%d")
                    hold_days.append((d2 - d1).days)
                except ValueError:
                    pass

        return {
            "starting_capital": self.starting_capital,
            "final_value": final_val,
            "total_return_pct": (final_val - self.starting_capital) / self.starting_capital * 100,
            "total_pnl": total_pnl,
            "total_buys": len(buys),
            "total_sells": len(sells),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": len(winners) / len(sells) * 100 if sells else 0,
            "avg_win": (sum(t["pnl"] for t in winners) / len(winners)) if winners else 0,
            "avg_loss": (sum(t["pnl"] for t in losers) / len(losers)) if losers else 0,
            "best_trade": max(sells, key=lambda t: t.get("pnl", 0)) if sells else None,
            "worst_trade": min(sells, key=lambda t: t.get("pnl", 0)) if sells else None,
            "max_drawdown": max_dd * 100,
            "max_drawdown_date": max_dd_date,
            "ticker_pnl": dict(ticker_pnl),
            "reason_counts": dict(reason_counts),
            "daily_values": self.daily_values,
            "daily_params": self.daily_params,
            "avg_hold_days": (sum(hold_days) / len(hold_days)) if hold_days else 0,
            "trades": self.trades,
        }


# ────────────────────────────────────────────────────────────────
# PDF REPORT GENERATOR
# ────────────────────────────────────────────────────────────────
# Dark theme colors
BG       = "#0f1117"
PANEL_BG = "#1a1d27"
TEXT     = "#e0e0e0"
ACCENT   = "#4fc3f7"
GREEN    = "#66bb6a"
RED      = "#ef5350"
AMBER    = "#ffa726"
GRID     = "#2a2d37"
MUTED    = "#888888"

def _style_ax(ax, title=""):
    ax.set_facecolor(PANEL_BG)
    ax.tick_params(colors=TEXT, labelsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(GRID)
    ax.spines["bottom"].set_color(GRID)
    ax.grid(True, color=GRID, linewidth=0.5, alpha=0.5)
    if title:
        ax.set_title(title, fontsize=11, color=TEXT, fontweight="bold", pad=8, loc="left")


def generate_report(results, output_path, num_days):
    dates    = [r[0] for r in results["daily_values"]]
    values   = [r[1] for r in results["daily_values"]]
    x_dates  = [datetime.strptime(d, "%Y-%m-%d") for d in dates]

    with PdfPages(output_path) as pdf:
        # ═══════════════════════════════════════════
        # PAGE 1: Summary + Equity Curve
        # ═══════════════════════════════════════════
        fig = plt.figure(figsize=(11, 8.5), facecolor=BG)
        gs = GridSpec(3, 2, figure=fig, hspace=0.45, wspace=0.35,
                      left=0.08, right=0.95, top=0.88, bottom=0.08)

        # Title
        fig.text(0.08, 0.95, "STOCK SPIKE MONITOR", fontsize=18, color=ACCENT,
                 fontweight="bold", fontfamily="monospace")
        fig.text(0.08, 0.91, f"{num_days}-Day Backtest Report  |  Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                 fontsize=9, color=MUTED, fontfamily="monospace")

        # ── Equity curve ──
        ax1 = fig.add_subplot(gs[0, :])
        _style_ax(ax1, "Portfolio Value")
        ax1.plot(x_dates, values, color=ACCENT, linewidth=1.5, zorder=3)
        ax1.fill_between(x_dates, results["starting_capital"], values,
                         where=[v >= results["starting_capital"] for v in values],
                         color=GREEN, alpha=0.15, interpolate=True)
        ax1.fill_between(x_dates, results["starting_capital"], values,
                         where=[v < results["starting_capital"] for v in values],
                         color=RED, alpha=0.15, interpolate=True)
        ax1.axhline(results["starting_capital"], color=MUTED, linewidth=0.8, linestyle="--", zorder=2)
        ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"${x:,.0f}"))
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        ax1.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
        ax1.tick_params(axis="x", rotation=30)

        # Mark buy/sell trades on equity curve
        for t in results["trades"]:
            try:
                td = datetime.strptime(t["date"], "%Y-%m-%d")
                if t["action"] == "BUY":
                    ax1.scatter(td, t.get("portfolio_value", results["starting_capital"]),
                               marker="^", color=GREEN, s=18, zorder=5, alpha=0.7)
                elif t["action"] == "SELL" and t.get("reason", "") != "END-OF-BACKTEST":
                    ax1.scatter(td, t.get("portfolio_value", results["starting_capital"]),
                               marker="v", color=RED, s=18, zorder=5, alpha=0.7)
            except (ValueError, KeyError):
                pass

        # ── KPI panel ──
        ax2 = fig.add_subplot(gs[1, 0])
        ax2.set_facecolor(PANEL_BG)
        ax2.axis("off")
        ret = results["total_return_pct"]
        ret_color = GREEN if ret >= 0 else RED
        kpis = [
            ("Starting Capital",  f"${results['starting_capital']:,.0f}", TEXT),
            ("Final Value",       f"${results['final_value']:,.0f}", ret_color),
            ("Total Return",      f"{ret:+.2f}%", ret_color),
            ("Total P&L",         f"${results['total_pnl']:+,.0f}", ret_color),
            ("Max Drawdown",      f"-{results['max_drawdown']:.2f}%", AMBER),
            ("Avg Hold (days)",   f"{results['avg_hold_days']:.1f}", TEXT),
        ]
        for i, (label, val, col) in enumerate(kpis):
            y = 0.92 - i * 0.155
            ax2.text(0.05, y, label, fontsize=9, color=MUTED, transform=ax2.transAxes)
            ax2.text(0.95, y, val, fontsize=10, color=col, fontweight="bold",
                     transform=ax2.transAxes, ha="right")
        ax2.set_title("Key Metrics", fontsize=11, color=TEXT, fontweight="bold", pad=8, loc="left")

        # ── Trade stats panel ──
        ax3 = fig.add_subplot(gs[1, 1])
        ax3.set_facecolor(PANEL_BG)
        ax3.axis("off")
        tstats = [
            ("Total Buys",     f"{results['total_buys']}", ACCENT),
            ("Total Sells",    f"{results['total_sells']}", ACCENT),
            ("Winners",        f"{results['winners']}", GREEN),
            ("Losers",         f"{results['losers']}", RED),
            ("Win Rate",       f"{results['win_rate']:.1f}%", GREEN if results['win_rate'] >= 50 else RED),
            ("Avg Win / Loss", f"${results['avg_win']:+,.0f} / ${results['avg_loss']:+,.0f}", TEXT),
        ]
        for i, (label, val, col) in enumerate(tstats):
            y = 0.92 - i * 0.155
            ax3.text(0.05, y, label, fontsize=9, color=MUTED, transform=ax3.transAxes)
            ax3.text(0.95, y, val, fontsize=10, color=col, fontweight="bold",
                     transform=ax3.transAxes, ha="right")
        ax3.set_title("Trade Statistics", fontsize=11, color=TEXT, fontweight="bold", pad=8, loc="left")

        # ── Exit reason breakdown ──
        ax4 = fig.add_subplot(gs[2, 0])
        _style_ax(ax4, "Exit Reasons")
        rc = results["reason_counts"]
        if rc:
            labels = list(rc.keys())
            vals   = list(rc.values())
            colors = []
            for l in labels:
                if "TAKE-PROFIT" in l: colors.append(GREEN)
                elif "HARD-STOP" in l: colors.append(RED)
                elif "TRAILING" in l: colors.append(AMBER)
                elif "SIGNAL" in l: colors.append("#9575cd")
                else: colors.append(MUTED)
            bars = ax4.barh(labels, vals, color=colors, height=0.6)
            ax4.set_xlabel("Count", fontsize=8, color=MUTED)
            for bar, v in zip(bars, vals):
                ax4.text(bar.get_width() + 0.3, bar.get_y() + bar.get_height()/2,
                         str(v), va="center", fontsize=8, color=TEXT)
        else:
            ax4.text(0.5, 0.5, "No exits", ha="center", va="center", color=MUTED, fontsize=10,
                     transform=ax4.transAxes)

        # ── Drawdown chart ──
        ax5 = fig.add_subplot(gs[2, 1])
        _style_ax(ax5, "Drawdown from Peak")
        peak = results["starting_capital"]
        drawdowns = []
        for d, v in results["daily_values"]:
            if v > peak:
                peak = v
            drawdowns.append(-(peak - v) / peak * 100)
        ax5.fill_between(x_dates, 0, drawdowns, color=RED, alpha=0.35)
        ax5.plot(x_dates, drawdowns, color=RED, linewidth=0.8)
        ax5.set_ylabel("%", fontsize=8, color=MUTED)
        ax5.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        ax5.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
        ax5.tick_params(axis="x", rotation=30)

        pdf.savefig(fig, facecolor=BG)
        plt.close(fig)

        # ═══════════════════════════════════════════
        # PAGE 2: Per-Ticker + Adaptive Params + Best/Worst
        # ═══════════════════════════════════════════
        fig2 = plt.figure(figsize=(11, 8.5), facecolor=BG)
        gs2 = GridSpec(3, 2, figure=fig2, hspace=0.45, wspace=0.35,
                       left=0.08, right=0.95, top=0.92, bottom=0.08)

        fig2.text(0.08, 0.96, "DETAILED ANALYSIS", fontsize=14, color=ACCENT,
                  fontweight="bold", fontfamily="monospace")

        # ── Per-ticker P&L bar chart ──
        ax6 = fig2.add_subplot(gs2[0, :])
        _style_ax(ax6, "P&L by Ticker")
        tp = results["ticker_pnl"]
        if tp:
            sorted_tickers = sorted(tp.keys(), key=lambda t: tp[t]["pnl"], reverse=True)
            top_n = sorted_tickers[:15]  # show top 15
            pnls = [tp[t]["pnl"] for t in top_n]
            bar_colors = [GREEN if p >= 0 else RED for p in pnls]
            ax6.barh(top_n[::-1], pnls[::-1], color=bar_colors[::-1], height=0.6)
            ax6.axvline(0, color=MUTED, linewidth=0.8)
            ax6.set_xlabel("P&L ($)", fontsize=8, color=MUTED)
            for i, (t, p) in enumerate(zip(top_n[::-1], pnls[::-1])):
                trades = tp[t]["trades"]
                wins   = tp[t]["wins"]
                label  = f" ${p:+,.0f} ({wins}/{trades})"
                ax6.text(max(p, 0) + max(abs(max(pnls, default=1)), abs(min(pnls, default=1))) * 0.02,
                         i, label, va="center", fontsize=7, color=TEXT)
        else:
            ax6.text(0.5, 0.5, "No trades", ha="center", va="center",
                     color=MUTED, fontsize=10, transform=ax6.transAxes)

        # ── Adaptive threshold over time ──
        ax7 = fig2.add_subplot(gs2[1, 0])
        _style_ax(ax7, "Adaptive Threshold")
        if results["daily_params"]:
            p_dates  = [datetime.strptime(p[0], "%Y-%m-%d") for p in results["daily_params"]]
            p_thresh = [p[1] for p in results["daily_params"]]
            ax7.plot(p_dates, p_thresh, color=ACCENT, linewidth=1.2)
            ax7.axhline(BASE_THRESHOLD, color=MUTED, linewidth=0.8, linestyle="--", label=f"Base ({BASE_THRESHOLD})")
            ax7.legend(fontsize=7, facecolor=PANEL_BG, edgecolor=GRID, labelcolor=TEXT)
            ax7.set_ylabel("Score", fontsize=8, color=MUTED)
            ax7.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
            ax7.tick_params(axis="x", rotation=30)

        # ── Fear & Greed + VIX ──
        ax8 = fig2.add_subplot(gs2[1, 1])
        _style_ax(ax8, "Fear & Greed / VIX")
        if results["daily_params"]:
            p_dates = [datetime.strptime(p[0], "%Y-%m-%d") for p in results["daily_params"]]
            p_fg    = [p[6] for p in results["daily_params"]]
            p_vix   = [p[7] for p in results["daily_params"]]
            ax8.plot(p_dates, p_fg, color=AMBER, linewidth=1.2, label="F&G")
            ax8_twin = ax8.twinx()
            ax8_twin.plot(p_dates, p_vix, color="#e57373", linewidth=1.2, label="VIX", linestyle="--")
            ax8_twin.tick_params(colors=TEXT, labelsize=7)
            ax8_twin.spines["right"].set_color(GRID)
            ax8.legend(fontsize=7, loc="upper left", facecolor=PANEL_BG, edgecolor=GRID, labelcolor=TEXT)
            ax8_twin.legend(fontsize=7, loc="upper right", facecolor=PANEL_BG, edgecolor=GRID, labelcolor=TEXT)
            ax8.set_ylabel("F&G Index", fontsize=8, color=MUTED)
            ax8_twin.set_ylabel("VIX", fontsize=8, color=MUTED)
            ax8.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
            ax8.tick_params(axis="x", rotation=30)

        # ── Best & Worst trades table ──
        ax9 = fig2.add_subplot(gs2[2, :])
        ax9.set_facecolor(PANEL_BG)
        ax9.axis("off")
        ax9.set_title("Top 5 Best & Worst Trades", fontsize=11, color=TEXT,
                       fontweight="bold", pad=8, loc="left")
        sells = [t for t in results["trades"] if t["action"] == "SELL" and t.get("reason") != "END-OF-BACKTEST"]
        if sells:
            sorted_sells = sorted(sells, key=lambda t: t.get("pnl", 0), reverse=True)
            best5  = sorted_sells[:5]
            worst5 = sorted_sells[-5:][::-1]

            headers = ["", "Ticker", "Entry", "Exit", "P&L", "P&L %", "Reason"]
            col_x   = [0.01, 0.08, 0.18, 0.32, 0.50, 0.65, 0.78]

            y = 0.92
            for i, h in enumerate(headers):
                ax9.text(col_x[i], y, h, fontsize=8, color=ACCENT, fontweight="bold",
                         transform=ax9.transAxes)
            y -= 0.08
            ax9.text(0.01, y, "BEST", fontsize=8, color=GREEN, fontweight="bold",
                     transform=ax9.transAxes)
            y -= 0.07
            for t in best5:
                vals = [
                    "", t["ticker"],
                    t.get("entry_date", ""),
                    t["date"],
                    f"${t.get('pnl',0):+,.0f}",
                    f"{t.get('pnl_pct',0):+.1f}%",
                    t.get("reason","")[:22],
                ]
                col = GREEN if t.get("pnl", 0) >= 0 else RED
                for i, v in enumerate(vals):
                    ax9.text(col_x[i], y, v, fontsize=7.5, color=col if i >= 4 else TEXT,
                             transform=ax9.transAxes, fontfamily="monospace")
                y -= 0.065

            y -= 0.03
            ax9.text(0.01, y, "WORST", fontsize=8, color=RED, fontweight="bold",
                     transform=ax9.transAxes)
            y -= 0.07
            for t in worst5:
                vals = [
                    "", t["ticker"],
                    t.get("entry_date", ""),
                    t["date"],
                    f"${t.get('pnl',0):+,.0f}",
                    f"{t.get('pnl_pct',0):+.1f}%",
                    t.get("reason","")[:22],
                ]
                col = GREEN if t.get("pnl", 0) >= 0 else RED
                for i, v in enumerate(vals):
                    ax9.text(col_x[i], y, v, fontsize=7.5, color=col if i >= 4 else TEXT,
                             transform=ax9.transAxes, fontfamily="monospace")
                y -= 0.065
        else:
            ax9.text(0.5, 0.5, "No completed trades to display", ha="center",
                     va="center", color=MUTED, fontsize=10, transform=ax9.transAxes)

        pdf.savefig(fig2, facecolor=BG)
        plt.close(fig2)

        # ═══════════════════════════════════════════
        # PAGE 3: Full Trade Log
        # ═══════════════════════════════════════════
        all_trades = results["trades"]
        trades_per_page = 32
        pages_needed = max(1, math.ceil(len(all_trades) / trades_per_page))

        for page_num in range(pages_needed):
            fig3 = plt.figure(figsize=(11, 8.5), facecolor=BG)
            ax_log = fig3.add_subplot(111)
            ax_log.set_facecolor(PANEL_BG)
            ax_log.axis("off")

            if page_num == 0:
                fig3.text(0.08, 0.96, "TRADE LOG", fontsize=14, color=ACCENT,
                          fontweight="bold", fontfamily="monospace")
                fig3.text(0.50, 0.96, f"(page {page_num+1}/{pages_needed})", fontsize=9,
                          color=MUTED, fontfamily="monospace")
            else:
                fig3.text(0.08, 0.96, f"TRADE LOG (page {page_num+1}/{pages_needed})",
                          fontsize=14, color=ACCENT, fontweight="bold", fontfamily="monospace")

            headers = ["Date", "Action", "Ticker", "Shares", "Price", "Cost/Proceeds", "P&L", "Signal", "Reason"]
            col_x   = [0.01, 0.11, 0.19, 0.27, 0.34, 0.44, 0.57, 0.68, 0.76]

            y_start = 0.93
            for i, h in enumerate(headers):
                ax_log.text(col_x[i], y_start, h, fontsize=7.5, color=ACCENT, fontweight="bold",
                            transform=ax_log.transAxes, fontfamily="monospace")

            start_idx = page_num * trades_per_page
            end_idx   = min(start_idx + trades_per_page, len(all_trades))
            y = y_start - 0.025

            for t in all_trades[start_idx:end_idx]:
                y -= 0.028
                if y < 0.02:
                    break
                is_buy = t["action"] == "BUY"
                row_color = GREEN if is_buy else RED
                pnl_str = ""
                if not is_buy:
                    pnl_str = f"${t.get('pnl', 0):+,.0f}"
                sig_str = f"{t.get('signal_score', ''):.0f}" if t.get("signal_score") else ""
                reason_str = t.get("reason", "")[:20] if not is_buy else ""
                cost_str = f"${t.get('cost', t.get('price',0)*t.get('shares',0)):,.0f}"
                if not is_buy:
                    cost_str = f"${t.get('price',0)*t.get('shares',0):,.0f}"

                vals = [
                    t["date"], t["action"], t["ticker"],
                    str(t.get("shares", "")),
                    f"${t.get('price', 0):.2f}",
                    cost_str, pnl_str, sig_str, reason_str,
                ]
                for i, v in enumerate(vals):
                    c = row_color if i in [1, 6] else TEXT
                    ax_log.text(col_x[i], y, v, fontsize=6.5, color=c,
                                transform=ax_log.transAxes, fontfamily="monospace")

            pdf.savefig(fig3, facecolor=BG)
            plt.close(fig3)

        # ═══════════════════════════════════════════
        # PAGE 4: Methodology & Disclaimers
        # ═══════════════════════════════════════════
        fig4 = plt.figure(figsize=(11, 8.5), facecolor=BG)
        ax_m = fig4.add_subplot(111)
        ax_m.set_facecolor(BG)
        ax_m.axis("off")
        fig4.text(0.08, 0.96, "METHODOLOGY & DISCLAIMERS", fontsize=14, color=ACCENT,
                  fontweight="bold", fontfamily="monospace")

        methodology = """
SIGNAL ENGINE (7 of 10 components replicated — 105/140 max score)

  Replicated:                           NOT replicated (set to 0):
  1. RSI Momentum         (20 pts)      8.  Claude AI Direction   (15 pts)
  2. Bollinger Band %B    (15 pts)      9.  AI Watchlist Bonus    (10 pts)
  3. MACD Crossover       (15 pts)      10. News Sentiment        (15 pts)
  4. Volume Confirmation  (15 pts)
  5. Squeeze Score        (10 pts)      Threshold is scaled proportionally:
  6. Price Slope          (10 pts)      live threshold * (105/140) to account
  7. Multi-Day Trend      (15 pts)      for the missing 35 pts of headroom.
  + Support/Resistance    (±5 pts)

ADAPTIVE PARAMETERS
  Threshold, take-profit, stop-loss, trailing stop, and max positions
  all adjust daily based on historical Fear & Greed Index and VIX values
  — exactly mirroring the live bot's _apply_adaptive_config() logic.

SELL LOGIC (fully replicated)
  • Take-profit:     adaptive (base 10%, scales with F&G)
  • Hard stop:       adaptive (base 6%, scales with VIX)
  • Trailing stop:   adaptive (base 3% from high-water mark, scales with VIX)
  • Signal collapse: sell if score drops to ≤30 while position is profitable
  • End-of-backtest: all remaining positions closed at final price

POSITION SIZING
  • Max 20% of portfolio per position
  • Size scales with signal strength (50%-100% of max allocation)
  • Max 2 positions per sector (concentration guard)
  • Max 3 actions per ticker per day

DATA SOURCE
  • Daily OHLCV from Yahoo Finance (yfinance)
  • Fear & Greed Index from alternative.me
  • VIX from Yahoo Finance (^VIX)
  • Sector ETF performance from Yahoo Finance

IMPORTANT LIMITATIONS
  • Uses daily bars (not intraday 5-min bars) — the live bot scans
    every ~60 seconds, so real entry/exit timing would differ
  • No slippage or commission modeling
  • AI components (40/140 pts) are zeroed — live performance could
    differ by ~15-20% due to AI-driven entries
  • Earnings proximity guard uses approximate dates
  • Past performance does not guarantee future results
"""
        ax_m.text(0.05, 0.90, methodology, fontsize=7.5, color=TEXT,
                  transform=ax_m.transAxes, fontfamily="monospace",
                  verticalalignment="top", linespacing=1.4)

        pdf.savefig(fig4, facecolor=BG)
        plt.close(fig4)

    print(f"\n  Report saved to: {output_path}")


# ────────────────────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Stock Spike Monitor Backtester")
    parser.add_argument("--days", type=int, default=30, help="Trading days to simulate (default: 30)")
    parser.add_argument("--capital", type=float, default=100_000, help="Starting capital (default: 100000)")
    parser.add_argument("--output", type=str, default="backtest_report.pdf", help="Output PDF path")
    args = parser.parse_args()

    global STARTING_CAPITAL, BASE_THRESHOLD
    STARTING_CAPITAL = args.capital

    # Scale the base threshold down to account for missing AI components
    # Live bot has 140 max score with threshold 65. We have 105 max.
    # Scale: 65 * (105/140) ≈ 49. But we keep it a bit higher for quality.
    BASE_THRESHOLD = max(45, int(65 * 105 / 140))

    print("=" * 60)
    print("  STOCK SPIKE MONITOR — BACKTEST ENGINE")
    print("=" * 60)
    print(f"\n  Config: {args.days} trading days, ${STARTING_CAPITAL:,.0f} capital")
    print(f"  Scaled threshold: {BASE_THRESHOLD} (live: 65, adjusted for 105/140 score range)")
    print(f"  Tickers: {len(CORE_TICKERS)}")

    # Calendar days needed for N trading days (rough: ~1.5x)
    cal_days = int(args.days * 1.5) + 10

    print(f"\n  [1/4] Fetching market data...")
    data = fetch_historical_data(CORE_TICKERS, cal_days)

    print(f"  [2/4] Fetching Fear & Greed history...")
    fg_history = fetch_fear_greed_historical(cal_days + 50)

    # Determine actual trading days from SPY/AAPL data
    ref_sym = "AAPL" if "AAPL" in data else list(data.keys())[0]
    ref_df  = data[ref_sym]
    all_dates = sorted([d.strftime("%Y-%m-%d") for d in ref_df.index])
    trading_days = all_dates[-args.days:] if len(all_dates) >= args.days else all_dates

    print(f"  [3/4] Running backtest ({trading_days[0]} → {trading_days[-1]})...")
    engine = BacktestEngine(STARTING_CAPITAL, data, fg_history, trading_days)
    engine.run()

    print(f"\n  [4/4] Generating PDF report...")
    results = engine.get_results()
    generate_report(results, args.output, args.days)

    # Print summary
    print("\n" + "=" * 60)
    print("  BACKTEST SUMMARY")
    print("=" * 60)
    ret = results["total_return_pct"]
    print(f"  Starting Capital:  ${results['starting_capital']:>12,.0f}")
    print(f"  Final Value:       ${results['final_value']:>12,.0f}")
    print(f"  Total Return:       {ret:>+11.2f}%")
    print(f"  Max Drawdown:       {results['max_drawdown']:>11.2f}%")
    print(f"  Win Rate:           {results['win_rate']:>11.1f}%")
    print(f"  Total Trades:       {results['total_buys'] + results['total_sells']:>11}")
    print(f"  Avg Hold (days):    {results['avg_hold_days']:>11.1f}")
    print("=" * 60)


if __name__ == "__main__":
    main()
