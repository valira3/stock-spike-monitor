---
name: keystone-backtest
description: Re-run, verify, or extend the Keystone production baseline backtest (v10 ORB morning + r17 EOD reversal). Use when the operator asks to "run keystone", "verify baseline", "update the benchmark", or "check if results still hold". Encodes the exact commands, corpus location, pkl cache behavior, and anti-patterns. v3.0 = first archived clean re-verify of the v9.1.114 (Keystone v5) baseline on 2026-05-17.
---

# Keystone Backtest

**Keystone** is the locked production strategy benchmark (v9.1.114 / Keystone v5). It runs two components independently then reports combined figures:

| Component | Ann/yr | Notes |
|---|---:|---|
| Morning ORB | +$37,466 | VWAP 15bps gate + VIX ≤25 + sym-10m cooldown |
| EOD reversal (r17) | +$12,620 | ORCL/AAPL/MSFT/AVGO/TSLA fence, 35% notional |
| **Combined** | **+$50,086** | **+67.8% on $100k / 17mo / 1/6 neg quarters** |

Reference: `results/keystone/keystone.json` (v3.0, archived 2026-05-17).

The v9.1.114 CHANGELOG initially claimed $52,518/yr combined from the lever sweep; the morning leg was revised to $37,466/yr on 2026-05-17 after the first clean re-verify. EOD reproduces the original claim exactly. Drift attributed to earnings-calendar population (commit `713aa2b1`) and weekly VIX refreshes that landed AFTER the v9.1.114 sweep numbers were captured.

---

## Step 1 — Morning ORB component

```bash
ORB_OR_MINUTES=30 ORB_RR=2.5 ORB_RISK_PER_TRADE_PCT=1.0 \
ORB_RANGE_MIN_PCT=0.008 ORB_RANGE_MAX_PCT=0.025 \
ORB_MAX_TRADES_PER_DAY=5 ORB_MAX_CONCURRENT_RISK_DOLLARS=2000 \
ORB_DAILY_LOSS_KILL_PCT=2.0 ORB_ATR_STOP_MULT=1.75 ORB_ATR_LOOKBACK_5M=14 \
ORB_PARTIAL_PROFIT_AT_1R=1 ORB_MOVE_TO_BE_AFTER_1R=1 \
ORB_STOP_BUFFER_BPS=5.0 ORB_ENTRY_SLIPPAGE_BPS=1.5 \
ORB_EXIT_SLIPPAGE_BPS=1.5 ORB_STOP_KICK_BPS=5.0 ORB_SHORT_PENALTY_BPS=1.0 \
ORB_MAX_TRADE_NOTIONAL_PCT=75 ORB_SKIP_GAP_ABOVE_PCT=1.5 \
ORB_SKIP_PRIOR_SPY_RET_LT_BPS=-40.0 \
ORB_SKIP_EARNINGS_WINDOW=1 ORB_TIME_CUTOFF_ET=11:00 ORB_EOD_CUTOFF_ET=15:55 \
ORB_ACCOUNT=100000 ORB_COMPOUND_DAILY=1 ORB_TICKER_SIDE_BLOCKLIST='{}' \
ORB_MAX_VWAP_DEV_BPS=15.0 ORB_MAX_VWAP_DEV_TICKERS='META,MSFT,AAPL,AMZN,GOOG,AVGO' \
ORB_SKIP_VIX_ABOVE=25.0 ORB_POST_TRADE_COOLDOWN_MIN=10 \
python tools/orb_backtest.py --corpus data --out results/keystone/morning \
  --year-prefix 20 \
  --tickers AAPL,AMZN,AVGO,GOOG,META,MSFT,NFLX,NVDA,ORCL,QQQ,SPY,TSLA
```

**Critical flags:**
- `--year-prefix 20` — without this the tool defaults to `2026-` and silently drops all 2025 data
- `ORB_POST_TRADE_COOLDOWN_MIN=10` — symmetric (win+loss) sym-10m cooldown deployed v9.1.111; replaces the older `ORB_POST_LOSS_COOLDOWN_MIN=30`
- `ORB_MAX_VWAP_DEV_BPS=15.0` on the 6 mega-caps — production gate tightened from 25 to 15 in v9.1.114
- `ORB_SKIP_VIX_ABOVE=25.0` — VIX ceiling raised from 22 to 25 in v9.1.114

## Step 2 — EOD reversal component

```bash
AFT_STRATEGY=eod_reversal AFT_EOD_UNIVERSE=ORCL,AAPL,MSFT,AVGO,NFLX,TSLA \
AFT_EOD_LONG_TICKERS=ORCL,AAPL,MSFT,AVGO,TSLA AFT_EOD_SHORT_TICKERS=ORCL,NFLX,AAPL,MSFT,TSLA \
AFT_EOD_TOP_N=1 AFT_NOTIONAL_PCT=35 AFT_SIZING_MODE=fixed_notional \
AFT_ENTRY_BUCKET=900 AFT_EXIT_BUCKET=958 \
AFT_ENTRY_SLIP_BPS=1.5 AFT_EXIT_SLIP_BPS=1.5 AFT_ACCOUNT=100000 AFT_COMPOUND_DAILY=1 \
python tools/afternoon_backtest.py --strategy eod_reversal \
  --corpus data --out results/keystone/eod --year-prefix 20
```

**Critical flags:**
- `AFT_ENTRY_BUCKET=900` (15:00 ET). The tool defaults to 930 (15:30) but production `orb/eod_reversal.py` has used 15:00 since v9.1.2. Running with 930 understates EOD P&L by ~20%.
- `AFT_EXIT_BUCKET=958` (15:58 ET) — v9.1.109 alignment; 959 understates by one bar.
- `AFT_EOD_UNIVERSE` MUST include TSLA in both long and short fences (added v9.1.113, +$3,930/yr).

---

## Pkl bar cache

- First run: ~30s (reads JSONL, writes `data/.bt_cache/<TICKER>.pkl`)
- Subsequent runs: ~6s (loads pkl — 5x faster)
- Cache auto-invalidates when any JSONL file is newer than its pkl
- Delete `data/.bt_cache/` to force full rebuild

---

## Interpreting results

The ORB backtest prints per-day and per-quarter P&L. Known characteristics:

- **Q1 2025 is the structurally weak quarter** — NFLX dominated bad days before the VWAP gate; -$5,550 morning in Q1 2025 is expected, not a regression
- **P&L compounds daily** (`ORB_COMPOUND_DAILY=1`) so dollar figures grow as equity grows; small early-year divergences amplify in later quarters
- If combined result deviates >10% from +$50,086/yr (or morning >10% from +$37,466, EOD >15% from +$12,620), investigate corpus gaps or config drift before concluding the strategy changed

**v3.0 per-quarter target:**

| Quarter | Morning | EOD | Combined |
|---|---:|---:|---:|
| 2025-Q1 | -$5,550 | +$1,102 | -$4,448 |
| 2025-Q2 | +$6,281 | +$6,994 | +$13,274 |
| 2025-Q3 | +$9,959 | -$1,074 | +$8,886 |
| 2025-Q4 | +$13,733 | +$2,046 | +$15,779 |
| 2026-Q1 | +$5,094 | +$6,414 | +$11,508 |
| 2026-Q2 | +$21,181 | +$1,594 | +$22,775 |

Artifacts: `results/keystone/keystone.json`, `results/keystone/morning/per_day/`, `results/keystone/eod/per_day/`

---

## Anti-patterns

| Pattern | Why it breaks Keystone |
|---|---|
| Missing `--year-prefix 20` | Silently uses only 2026 data; looks like a ~6-month backtest |
| Missing `ORB_POST_TRADE_COOLDOWN_MIN=10` | Same-ticker re-entry inflates bad days; backtest diverges from production behavior |
| Using `ORB_POST_LOSS_COOLDOWN_MIN=30` instead | Pre-v9.1.111 lever; sym-10m is the production cooldown since v9.1.111 |
| `ORB_REQUIRE_RVOL_ABOVE` > 0 | Kills +$27k/yr of edge on this corpus; do not add |
| `ORB_MAX_TRADES_PER_DAY=1` | Halves P&L by blocking profitable double-fires alongside losing ones |
| Using T5 blocklist instead of VWAP gate | v12 Config A artifact; Keystone uses VWAP 15bps gate, no blocklist |
| `AFT_ENTRY_BUCKET=930` (EOD) | 15:30 entry misses 30 min of the reversal window; understates EOD by ~20% |
| `AFT_EXIT_BUCKET=959` (EOD) | Pre-v9.1.109; use 958 to match production flush timing |
| TSLA missing from `AFT_EOD_*_TICKERS` | -$3,930/yr (v9.1.113 added TSLA to both long and short fences) |
| Confusing ORB equity with risk-book equity | Dashboard shows mark-to-market ($100,657 after returns); risk books use configured $100k; sizing is always based on risk-book equity |

---

## Verifying against production

After running Keystone, compare results with live performance via:

```bash
# Pull current dashboard state
curl -sk https://tradegenius.up.railway.app/api/state | python -m json.tool

# Or run the dashboard analysis tool (checks config vs Keystone)
DASHBOARD_PASSWORD=<pw> python tools/dashboard_analysis.py
```

`tools/dashboard_analysis.py` checks all Keystone config fields against live `/api/state.v10.config` and flags any drift as WARN. Mirrors KEYSTONE dict in `tools/system_check_bot.py`.

---

## Updating Keystone after a strategy change

When a new lever is validated in a sweep and promoted to production:

1. Re-run both Step 1 and Step 2 with the updated config
2. Update `results/keystone/keystone.json` (bump version, append changelog entry, update result fields + per-quarter table)
3. Update the Keystone table in `CLAUDE.md` (combined P&L, quarter breakdown)
4. Update `KEYSTONE` dict in both `tools/dashboard_analysis.py` AND `tools/system_check_bot.py` (they must agree)
5. Update the headline numbers + Step 1/2 commands at the top of this SKILL.md
6. Update ARCHITECTURE.md if the strategy description changes
7. Commit as `vX.Y.Z: update Keystone to include <lever>`
