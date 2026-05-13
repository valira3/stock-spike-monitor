"""v8.1.6 -- compute annualized Sharpe under the v8.1.3 production
config from a full-year backtest.

Config matches orb/live_runtime.py env-fallback defaults as of v8.1.3:
  risk_per_trade_pct=1.0
  atr_stop_mult=1.75
  atr_lookback_5m=14
  partial_profit_at_1r=1
  + all v10 anchor knobs

Method:
  1. Run tools/orb_backtest.py on /tmp/rth-data/data (251 trading days)
  2. Read per-day JSON files from out_dir/per_day/
  3. Sum pnl_dollars per day -> per-day P&L series in dollars
  4. Convert to per-day returns (% of $100k starting equity)
  5. annualized Sharpe = (mean_daily_return / std_daily_return) * sqrt(252)

Risk-free rate assumed 0 (sandbox; the spread vs T-bill is small
relative to strategy volatility).
"""
import json
import math
import os
import subprocess
from pathlib import Path

UNIV = "AAPL,MSFT,NVDA,TSLA,META,GOOG,AMZN,AVGO,NFLX,ORCL,SPY,QQQ"
T5 = (
    '{"META":["LONG","SHORT"],"MSFT":["LONG","SHORT"],'
    '"AAPL":["LONG","SHORT"],"AMZN":["LONG","SHORT"],'
    '"GOOG":["LONG","SHORT"],"AVGO":["LONG","SHORT"]}'
)

# v8.1.3 production config
ENV = {
    "ORB_COMPOUND_DAILY": "1",
    "ORB_STOP_BUFFER_BPS": "5",
    "ORB_OR_MINUTES": "30",
    "ORB_RR": "2.5",
    "ORB_RANGE_MIN_PCT": "0.008",
    "ORB_RANGE_MAX_PCT": "0.025",
    "ORB_MAX_TRADES_PER_DAY": "5",
    "ORB_RISK_PER_TRADE_PCT": "1.00",
    "ORB_MAX_CONCURRENT_RISK_DOLLARS": "2000",
    "ORB_DAILY_LOSS_KILL_PCT": "2.0",
    "ORB_MAX_TRADE_NOTIONAL_PCT": "75",
    "ORB_MOVE_TO_BE_AFTER_1R": "1",
    "ORB_TICKER_SIDE_BLOCKLIST": T5,
    "ORB_SKIP_GAP_ABOVE_PCT": "1.5",
    "ORB_SKIP_EARNINGS_WINDOW": "1",
    "ORB_EARNINGS_DAYS_BEFORE": "1",
    "ORB_SKIP_VIX_ABOVE": "20",
    "ORB_TIME_CUTOFF_ET": "11:00",
    "ORB_EOD_CUTOFF_ET": "15:55",
    "ORB_ACCOUNT": "100000",
    "ORB_ENTRY_SLIPPAGE_BPS": "1.5",
    "ORB_EXIT_SLIPPAGE_BPS": "1.5",
    "ORB_STOP_KICK_BPS": "5.0",
    "ORB_SHORT_PENALTY_BPS": "1.0",
    "ORB_ATR_STOP_MULT": "1.75",
    "ORB_PARTIAL_PROFIT_AT_1R": "1",
}

OUT = Path("/tmp/v816_sharpe")
OUT.mkdir(parents=True, exist_ok=True)

env = {**os.environ, **ENV}
print("running backtest with v8.1.3 config...")
subprocess.run(
    ["python3", "tools/orb_backtest.py",
     "--corpus", "/tmp/rth-data/data",
     "--out", str(OUT),
     "--year-prefix", "202",
     "--tickers", UNIV],
    env=env, check=True, capture_output=True, timeout=600,
)

with open(OUT / "summary.json") as f:
    summary = json.load(f)
print(f"net_pnl=${summary['net_pnl']:+,.2f}")
print(f"win_rate_pct={summary.get('win_rate_pct')}%")
print(f"entries={summary.get('entries')}")

# Per-day P&L series
per_day = OUT / "per_day"
day_files = sorted(per_day.iterdir())
n_days = len(day_files)
print(f"per-day files: {n_days}")

returns = []  # daily return as fraction of $100k starting equity
equity = 100_000.0
for jf in day_files:
    with open(jf) as fh:
        d = json.load(fh)
    pnl = sum(p.get("pnl_dollars", 0) for p in d.get("pnl_pairs", []))
    # Daily return relative to start-of-day equity (compound-aware).
    # Matches what the backtest internally does: equity grows by today's
    # net pnl, next day's risk_per_trade is 1% of that new equity.
    r = pnl / equity if equity > 0 else 0.0
    returns.append(r)
    equity += pnl

n = len(returns)
mean_r = sum(returns) / n
var_r = sum((r - mean_r) ** 2 for r in returns) / (n - 1) if n > 1 else 0.0
std_r = math.sqrt(var_r)
# Annualization: 252 trading days/year, no risk-free rate (proxy 0%)
sharpe_ann = (mean_r / std_r) * math.sqrt(252) if std_r > 0 else 0.0
print()
print("===== Sharpe (annualized) =====")
print(f"n_days       = {n}")
print(f"mean_daily   = {mean_r * 100:.4f}%")
print(f"stdev_daily  = {std_r * 100:.4f}%")
print(f"sharpe_ann   = {sharpe_ann:.3f}")
print()
print(f"final_equity = ${equity:,.2f}")
print(f"total_return = {(equity / 100_000.0 - 1.0) * 100:+.2f}%")
# Also compute max drawdown for confirmation
peak = 100_000.0
mdd = 0.0
equity = 100_000.0
for r in returns:
    equity *= 1.0 + r
    if equity > peak:
        peak = equity
    dd = (peak - equity) / peak
    if dd > mdd:
        mdd = dd
print(f"max_drawdown = {mdd * 100:.2f}%")
