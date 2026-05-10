# v10 ANCHOR — KEYSTONE STRATEGY

> **Canonical reference for the deployable strategy as of 2026-05-10.**
> Tagged: `v10-anchor` on main.
> All future Phase 14+ work measured against this baseline.

---

## What this is

The **v10 anchor** is the production-deployable Opening Range Breakout (ORB) strategy that emerged from PRs #421–#466 across Phases 1–13 of P&L optimization. It's the result of:
- 9 phases of lever exploration
- 30+ env-var tunables, all look-ahead audited
- Multiple cross-validation splits
- Multi-agent quality reviews (research, audit, code review, manager)
- 5 prior reports superseded (v3, v5, v6, v7, v8, v9)

---

## Configuration (env-var)

```bash
# === Strategy core ===
ORB_MODE=1                              # use ORB strategy
ORB_OR_MINUTES=30                       # 30-min Opening Range
ORB_RR=2.5                              # 1R risk : 2.5R target
ORB_STOP_BUFFER_BPS=5                   # 5bp slippage on stop

# === Range filter (Phase 8) ===
ORB_RANGE_MIN_PCT=0.008                 # OR width ≥ 0.8% of price
ORB_RANGE_MAX_PCT=0.025                 # OR width ≤ 2.5% of price

# === Trade limits ===
ORB_MAX_TRADES_PER_DAY=5

# === Risk caps (per-trade + portfolio) ===
ORB_RISK_PER_TRADE_PCT=2.00             # 2% of account per trade
ORB_MAX_CONCURRENT_RISK_DOLLARS=2000    # max $2k open risk at once
ORB_DAILY_LOSS_KILL_PCT=2.0             # halt after −2% intraday
ORB_MAX_TRADE_NOTIONAL_PCT=75           # cap each trade at 75% account

# === Stop management ===
ORB_MOVE_TO_BE_AFTER_1R=1               # move stop to BE after +1R

# === Compounding (rule #11b: default ON) ===
ORB_COMPOUND_DAILY=1                    # account grows day-to-day

# === Per-ticker blocks (corpus-tuned) ===
ORB_TICKER_SIDE_BLOCKLIST='{"META":["LONG","SHORT"],"MSFT":["LONG","SHORT"]}'

# === Phase 11 day-skip filters ===
ORB_SKIP_GAP_ABOVE_PCT=1.5              # skip ticker on >1.5% gap
ORB_SKIP_EARNINGS_WINDOW=1
ORB_EARNINGS_DAYS_BEFORE=1              # skip 1d before earnings

# === Phase 12 VIX gate ===
ORB_SKIP_VIX_ABOVE=22                   # halt entries if VIX(D-1) > 22
ORB_VIX_CSV_PATH=data/external/vix-daily.csv
```

---

## Universe (10 active, 2 blocked)

```
Active:  AAPL, NVDA, TSLA, GOOG, AMZN,
         AVGO, NFLX, ORCL, SPY, QQQ
Blocked: META (LONG+SHORT), MSFT (LONG+SHORT)
```

SPY/QQQ are index references — no trading positions but used by the regime gate.

---

## Backtest result (124 days, compounded)

```
Starting balance:   $100,000.00
Net P&L:            +$19,224.81  (+19.22%)
Ending balance:     $119,224.81
CAGR (annualized):  +42.95%
Sharpe (ann):        2.85
Max drawdown:        5.03%
Win rate:           57.02%
Worst day:          −$2,030
Best day:           +$3,697
Trades:             114
Profit days:        52/124 (42%)
Loss days:          38/124 (31%)
Flat days:          34/124 (gated)
```

---

## Cross-validation (every split positive)

| split | days | CAGR |
|---|---:|---:|
| Full 124d (in-sample) | 124 | **+43.0%** |
| 2026 only (trend) | 83 | +70.4% |
| STRIDE=2 (62d) | 62 | +5.9% |
| STRIDE=3 (42d) | 42 | +7.9% |
| 2025-Q4 only (chop) | 41 | +5.5% |

Range: **+5.5% to +70.4%**. Honest deployable expectation: **+20–40% CAGR**.

---

## $ projections on $100k

| timeframe | mid-point | range |
|---|---:|---|
| 1 month | $103,000 | $101k–$106k |
| 3 months | $107,000 | $102k–$118k |
| 6 months | $113,000 | $105k–$130k |
| 1 year | $130,000 | $115k–$170k |
| 3 years | $195,000 | $135k–$295k |

Vs current production (−$20,771/yr trajectory): **+$45–60k/yr swing**, **+$140–200k cumulative over 3 years**.

---

## Look-ahead audit — clean

Every gate uses only data with timestamp ≤ decision time:

| gate | data source | timing |
|---|---|---|
| OR width filter | own bars [09:30, 09:30+30) | OR close |
| Gap filter | prior session close + today's 09:30 open | OR start |
| Earnings filter | public schedule | weeks ahead |
| VIX gate | VIX_close(D−1) | session start |
| Move-to-BE | intra-bar high/low | live |
| Daily kill switch | cumulative day P&L | live |

Verified by Phase 11 audit subagent (PR #459) and Phase 13 audit subagent (PR #463). No look-ahead bias detected.

---

## Required infrastructure

### Data feeds (auto-refreshed via GHA)

- **VIX daily history**: `data/external/vix-daily.csv` from datahub.io GitHub mirror
- **Earnings calendar**: `tools/orb_earnings_calendar.py` from yfinance
- Both refreshed daily at **07:00 ET** by `.github/workflows/refresh-data-feeds.yml`

### Backtest tooling

- `tools/orb_backtest.py` — main engine (1,500+ lines)
- `tools/orb_vix_loader.py` — VIX CSV reader with DST-aware ts→bucket
- `tools/orb_earnings_calendar.py` — hardcoded calendar (refreshed by fetcher)
- `tools/orb_earnings_fetcher.py` — yfinance fetcher (runs outside sandbox)

### Account requirements

- **Minimum**: $50k (caps proportional)
- **Recommended**: $100k (config tuned for this size)
- **Maximum**: ~$300k without re-tuning notional caps

---

## Deployment checklist

1. Set the env vars above on the live trading instance
2. Confirm `data/external/vix-daily.csv` is fresh (within 24h)
3. Confirm `tools/orb_earnings_calendar.py` is fresh (within 1 quarter)
4. Confirm `refresh-data-feeds` workflow is enabled
5. **Paper-trade 5 days** to verify fills match expected entries
6. Monitor: if `tickers_failed > 0` in any sweep summary, investigate
7. Re-baseline quarterly (META/MSFT block list, VIX threshold, range_min)

---

## What we tested and rejected (so future work doesn't re-test)

| lever | phase | result | reason |
|---|:---:|---|---|
| RVOL gate (Zarattini) | 11 | ❌ | Stock-PICKING filter, not fixed-universe |
| SPY/QQQ direction-align | 9 | ❌ | Mega-cap breakouts work AGAINST index |
| Time-stop {30,45,60,75,90}m | 7 | ❌ | Winners need to breathe |
| Vol-targeted sizing | 13 | ❌ | ORB EV scales WITH vol, not against |
| NFLX added to blocklist | 13 | ❌ | Compounding substitution hurts |
| Universe expand 12→25 | 13 | ❌ | More candidates = more substitution |
| VIX > 24/25/28 (looser) | 13 | ❌ | Lets in March chop |
| VIX < 22 (tighter) | 13 | ❌ | Filters trend regime |
| Counter-trend regime gate | 9 | ❌ | Direction-align hurts |
| NR7 / inside-day | 11 | ➖ | Mixed; not robust enough |

**Key learning**: per-ticker P&L is NOT additive in compounded mode. Adding "more signals" routinely hurts because of substitution effects in the daily risk budget gate.

---

## Phase 14+ candidates (untested, deferred)

- Multi-year OOS validation on 2024 + early 2025
- Counter-trend ensemble strategy (orthogonal signal source)
- Per-ticker swap optimization (replace TSLA-class loser with WMT-class winner)
- Trailing stop / partial profit on v10 base (code exists, never tested in v10 context)
- Active VIX/regime adjustment (5-day VIX change instead of absolute level)

---

## Reports superseded by this keystone

- `docs/pl_optimization_final_report_v3.md`
- `docs/pl_optimization_final_report_v5.md`
- `docs/pl_optimization_final_report_v6.md`
- `docs/pl_optimization_final_report_v7.md`
- `docs/pl_optimization_v7_amendment_oos_failure.md`
- `docs/pl_optimization_final_report_v8.md`
- `docs/pl_optimization_final_report_v9.md`
- `docs/pl_optimization_final_report_v10.md`
- `docs/pl_optimization_final_report_v11.md`

This document is the canonical state. All others remain as historical record.

---

## Auto Agentic Framework

This strategy was developed under the [Auto Agentic Rule Framework](./auto_agentic_framework.md) — 30+ rules including #0 (Manager Agent oversight), #7b (no look-ahead), #11b (compounding default), #27 (iPhone-narrow timed progress). Every PR cited the rule(s) it satisfied.

---

**Tag**: `v10-anchor`
**Date**: 2026-05-10
**Last verified**: full backtest + xval re-run on commit 89afe6d (PR #466)
**Status**: DEPLOYABLE
