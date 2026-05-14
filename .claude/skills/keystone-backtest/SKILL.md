---
name: keystone-backtest
description: Re-run, verify, or extend the Keystone production baseline backtest (v10 ORB morning + r17 EOD reversal). Use when the operator asks to "run keystone", "verify baseline", "update the benchmark", or "check if results still hold". Encodes the exact commands, corpus location, pkl cache behavior, and anti-patterns discovered during the 2026-05-13 baselining session.
---

# Keystone Backtest

**Keystone** is the locked production strategy benchmark. It runs two components independently then reports combined figures:

| Component | Ann/yr | Notes |
|---|---:|---|
| Morning ORB | +$31,449 | VWAP 25bps gate + 30min cooldown |
| EOD reversal (r17) | +$10,036 | ORCL/AAPL/MSFT/AVGO fence, 35% notional |
| **Combined** | **+$41,485** | **+58.8% on $100k / 17mo / 1/6 neg quarters** |

Reference: `results/keystone/keystone.json` (v2.1).

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
ORB_SKIP_VIX_ABOVE=22.0 ORB_SKIP_PRIOR_SPY_RET_LT_BPS=-40.0 \
ORB_SKIP_EARNINGS_WINDOW=1 ORB_TIME_CUTOFF_ET=11:00 ORB_EOD_CUTOFF_ET=15:55 \
ORB_ACCOUNT=100000 ORB_COMPOUND_DAILY=1 ORB_TICKER_SIDE_BLOCKLIST='{}' \
ORB_MAX_VWAP_DEV_BPS=25.0 ORB_MAX_VWAP_DEV_TICKERS='META,MSFT,AAPL,AMZN,GOOG,AVGO' \
ORB_POST_LOSS_COOLDOWN_MIN=30 \
python tools/orb_backtest.py --corpus data --out results/keystone_verify \
  --year-prefix 20 \
  --tickers AAPL,AMZN,AVGO,GOOG,META,MSFT,NFLX,NVDA,ORCL,QQQ,SPY,TSLA
```

**Critical flags:**
- `--year-prefix 20` — without this the tool defaults to `2026-` and silently drops all 2025 data
- `ORB_POST_LOSS_COOLDOWN_MIN=30` — mirrors `POST_LOSS_COOLDOWN_MIN=30` in Railway env; without it the same-ticker re-entry pattern inflates bad days significantly
- `ORB_MAX_VWAP_DEV_BPS=25.0` on the 6 mega-caps — this is the production gate that replaced the old T5 blocklist; must be present

## Step 2 — EOD reversal component

```bash
AFT_STRATEGY=eod_reversal AFT_EOD_UNIVERSE=ORCL,AAPL,MSFT,AVGO,NFLX \
AFT_EOD_LONG_TICKERS=ORCL,AAPL,MSFT,AVGO AFT_EOD_SHORT_TICKERS=ORCL,NFLX,AAPL,MSFT \
AFT_EOD_TOP_N=1 AFT_NOTIONAL_PCT=35 AFT_SIZING_MODE=fixed_notional \
AFT_ENTRY_BUCKET=900 AFT_EXIT_BUCKET=959 \
AFT_ENTRY_SLIP_BPS=1.5 AFT_EXIT_SLIP_BPS=1.5 AFT_ACCOUNT=100000 AFT_COMPOUND_DAILY=1 \
python tools/afternoon_backtest.py --strategy eod_reversal \
  --corpus data --out results/keystone/eod --year-prefix 20
```

**Critical flag:** `AFT_ENTRY_BUCKET=900` (15:00 ET). The tool defaults to 930 (15:30) but production `orb/eod_reversal.py` has used 15:00 since v9.1.2. Running with 930 understates EOD P&L.

---

## Pkl bar cache

- First run: ~30s (reads JSONL, writes `data/.bt_cache/<TICKER>.pkl`)
- Subsequent runs: ~7s (loads pkl — 4x faster)
- Cache auto-invalidates when any JSONL file is newer than its pkl
- Delete `data/.bt_cache/` to force full rebuild

---

## Interpreting results

The ORB backtest prints per-day and per-quarter P&L. Known characteristics:

- **Q1 2025 is the structurally weak quarter** — NFLX dominated bad days before the VWAP gate; -$5,183 in Q1 is expected, not a regression
- **P&L compounds daily** (`ORB_COMPOUND_DAILY=1`) so dollar figures grow as equity grows
- If combined result deviates >10% from +$41,485 (morning) or >15% from +$10,036 (EOD), investigate corpus gaps or config drift before concluding the strategy changed

Artifacts: `results/keystone/keystone.json`, `results/keystone/morning/per_day/`, `results/keystone/eod/per_day/`

---

## Anti-patterns

| Pattern | Why it breaks Keystone |
|---|---|
| Missing `--year-prefix 20` | Silently uses only 2026 data; looks like a ~6-month backtest |
| Missing `ORB_POST_LOSS_COOLDOWN_MIN=30` | Same-ticker re-entry after stop inflates bad days; backtest diverges from production behavior |
| `ORB_REQUIRE_RVOL_ABOVE` > 0 | Kills +$27k/yr of edge on this corpus; do not add |
| `ORB_MAX_TRADES_PER_DAY=1` | Halves P&L by blocking profitable double-fires alongside losing ones |
| Using T5 blocklist instead of VWAP gate | v12 Config A artifact; Keystone uses VWAP gate, no blocklist |
| `AFT_ENTRY_BUCKET=930` (EOD) | 15:30 entry misses 30 min of the reversal window; understates EOD by ~20% |
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

`tools/dashboard_analysis.py` checks all 22 Keystone config fields against live `/api/state.v10.config` and flags any drift as WARN.

---

## Updating Keystone after a strategy change

When a new lever is validated in a sweep (e.g., a new gate or sizing rule) and promoted to production:

1. Re-run both Step 1 and Step 2 with the updated config
2. Update `results/keystone/keystone.json` (version field + all result fields)
3. Update the Keystone table in `CLAUDE.md` (combined P&L, quarter breakdown)
4. Update `KEYSTONE` dict in `tools/dashboard_analysis.py` to include the new lever's expected value
5. Update ARCHITECTURE.md if the strategy description changes
6. Commit as `vX.Y.Z: update Keystone to include <lever>`
