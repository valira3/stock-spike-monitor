#!/usr/bin/env bash
# build_replay_week.sh -- one-step regeneration of the weekly counterfactual replay.
#
# Pulls any missing Alpaca SIP bars, runs `tools.orb_replay_day` end-to-end against
# the live v10 ORB engine for each (date, portfolio), runs `tools/afternoon_backtest.py`
# for the EOD r17 leg, then renders replay_week.html.
#
# Usage:
#     bash scripts/build_replay_week.sh                          # last 5 trading days
#     DATES=2026-05-12,2026-05-13 bash scripts/build_replay_week.sh
#     OUT=public/replay.html bash scripts/build_replay_week.sh
#
# Requires:
#     .env.monitor with VAL_ALPACA_PAPER_KEY / VAL_ALPACA_PAPER_SECRET
#     .venv/bin/python with alpaca-py installed (for the bar fetch step)

set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"

DATES="${DATES:-2026-05-11,2026-05-12,2026-05-13,2026-05-14,2026-05-15}"
OUT="${OUT:-replay_week.html}"
TICKERS="AAPL,AMZN,AVGO,GOOG,META,MSFT,NFLX,NVDA,ORCL,QQQ,SPY,TSLA"

# Prior-day VIX close for each session date (Sep 2025 -> May 2026, lookup table).
# Sourced from data/external/vix-daily.csv. Add new dates as needed.
declare -A VIX_D1=(
    [2026-05-11]=17.19
    [2026-05-12]=18.38
    [2026-05-13]=17.99
    [2026-05-14]=17.87
    [2026-05-15]=17.26
)

# Keystone v9.1.114 morning ORB lever set. Must match production.
MORN_ENV="ORB_OR_MINUTES=30 ORB_RR=2.5 ORB_RISK_PER_TRADE_PCT=1.0 \
  ORB_RANGE_MIN_PCT=0.008 ORB_RANGE_MAX_PCT=0.025 \
  ORB_MAX_TRADES_PER_DAY=5 ORB_MAX_CONCURRENT_RISK_DOLLARS=2000 \
  ORB_DAILY_LOSS_KILL_PCT=2.0 ORB_ATR_STOP_MULT=1.75 ORB_ATR_LOOKBACK_5M=14 \
  ORB_PARTIAL_PROFIT_AT_1R=1 ORB_MOVE_TO_BE_AFTER_1R=1 \
  ORB_TIME_CUTOFF_ET=11:00 ORB_EOD_CUTOFF_ET=15:55 \
  ORB_MAX_VWAP_DEV_BPS=15.0 \
  ORB_MAX_VWAP_DEV_TICKERS=META,MSFT,AAPL,AMZN,GOOG,AVGO \
  ORB_SKIP_VIX_ABOVE=25.0 ORB_POST_TRADE_COOLDOWN_MIN=10 \
  ORB_SKIP_GAP_ABOVE_PCT=1.5 ORB_SKIP_EARNINGS_WINDOW=1 \
  ORB_TICKER_SIDE_BLOCKLIST={} ORB_LIVE_MODE=1 \
  ORB_MAX_TRADE_NOTIONAL_PCT=75"

# Keystone v9.1.114 r17 EOD reversal lever set.
EOD_ENV="AFT_STRATEGY=eod_reversal \
  AFT_EOD_UNIVERSE=ORCL,AAPL,MSFT,AVGO,NFLX,TSLA \
  AFT_EOD_LONG_TICKERS=ORCL,AAPL,MSFT,AVGO,TSLA \
  AFT_EOD_SHORT_TICKERS=ORCL,NFLX,AAPL,MSFT,TSLA \
  AFT_EOD_TOP_N=1 AFT_NOTIONAL_PCT=35 AFT_SIZING_MODE=fixed_notional \
  AFT_ENTRY_BUCKET=900 AFT_EXIT_BUCKET=958 \
  AFT_ENTRY_SLIP_BPS=1.5 AFT_EXIT_SLIP_BPS=1.5 AFT_COMPOUND_DAILY=0"

echo "=== [1/4] Pulling any missing SIP bars from Alpaca ==="
IFS=',' read -ra DATE_LIST <<< "$DATES"
MISSING=()
for d in "${DATE_LIST[@]}"; do
    if [ ! -d "data/$d" ] || [ "$(ls -1 data/$d/*.jsonl 2>/dev/null | wc -l)" != "12" ]; then
        MISSING+=("$d")
    fi
done
if [ "${#MISSING[@]}" -gt 0 ]; then
    if [ ! -f .env.monitor ]; then
        echo "[ERR] missing dates: ${MISSING[*]}, but .env.monitor not found"
        exit 1
    fi
    set -a; source .env.monitor; set +a
    PY=".venv/bin/python"
    [ -x "$PY" ] || PY="python3"
    SORTED=$(printf '%s\n' "${MISSING[@]}" | sort)
    START=$(echo "$SORTED" | head -1)
    END=$(echo "$SORTED" | tail -1)
    "$PY" tools/fetch_alpaca_bars.py \
        --start "$START" --end "$END" \
        --base-dir ./data --feed sip
else
    echo "  all dates already in corpus -- skipping fetch"
fi

echo "=== [2/4] Live-engine morning replay (orb_replay_day) ==="
mkdir -p results/week_replay_v2
PIDS=()
for d in "${DATE_LIST[@]}"; do
    vix="${VIX_D1[$d]:-18.0}"
    for portfolio in main val; do
        [ "$portfolio" = "main" ] && eq=100000 || eq=30185
        out="results/week_replay_v2/${portfolio}_${d}.jsonl"
        (env $MORN_ENV python3 -m tools.orb_replay_day \
            --date "$d" --base-dir data --tickers "$TICKERS" \
            --equity "$eq" --portfolio "$portfolio" --vix-d1 "$vix" \
            --out "$out" 2>&1 | tail -1) &
        PIDS+=($!)
    done
done
for p in "${PIDS[@]}"; do wait "$p" || true; done

echo "=== [3/4] r17 EOD backtest (afternoon_backtest) ==="
for portfolio in main val; do
    [ "$portfolio" = "main" ] && eq=100000 || eq=30185
    env $EOD_ENV AFT_ACCOUNT="$eq" python3 tools/afternoon_backtest.py \
        --strategy eod_reversal --corpus data \
        --out "results/week_replay/${portfolio}_eod" --year-prefix 20 2>&1 | tail -1 &
done
wait

echo "=== [4/4] Render HTML ==="
python3 scripts/replay_backtest_week.py --out "$OUT" --dates "$DATES"
echo "[OK] $OUT ready -- open in browser"
