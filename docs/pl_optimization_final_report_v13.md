# P&L Optimization v13 — Day-End-Giveback Defenses (R6)

**Status:** Research complete.
**Run date:** 2026-05-12
**Corpus:** 252 trading days, 2025-05-12 → 2026-05-12 (full 1-year window).
**Universe:** AAPL, MSFT, NVDA, TSLA, META, GOOG, AMZN, AVGO, NFLX, ORCL, SPY, QQQ.
**Harness:** `tools/orb_backtest.py` (classical ORB engine).

## Motivation

Live trading on 2026-05-12 produced a **97%-of-peak EOD giveback** ($1,080 peak realized → $35 close) driven by re-entries-after-stop on AMZN/GOOG shorts. v8.3.26 added two surgical research-harness env vars; this round (R6) validates them against the full-year corpus.

The two rules under test:

- **Rule #1 — `ORB_LOSS_LOCK_THRESHOLD_USD`**: After a closed leg with `pnl < -threshold`, lock that `(ticker, side)` pair for the rest of the trading day. No further entries on that pair.
- **Rule #2 — `ORB_PEAK_DD_HALT_USD`**: When intraday realized PnL drops `$X` below the day's running peak, halt all new entries for the rest of the day.

Layered on top of the v12 Config A baseline (`ORB_RISK_PER_TRADE_PCT=1.0` on the v10 keystone) with today's v8.3.x production config including:
- v8.3.20 over-leverage protection: `ORB_MAX_CONCURRENT_NOTIONAL_MULT=0.95` (was 2.0).
- v8.1.3 partial-profit-at-1R default-on.
- v8 realism: `ORB_ENTRY_SLIPPAGE_BPS=ORB_EXIT_SLIPPAGE_BPS=5.0` (was 1.5).

## Headline results

| Variant | Net PnL | Δ baseline | Win rate | Entries | Pos days | Neg days | Lock-rejects | Kill-fired days |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **baseline** | **-$28,519** | — | 41.1% | 375 | 93 | 128 | 0 | 7 |
| lock_25 | -$28,519 | $0 | 41.1% | 375 | 93 | 128 | 4 | 7 |
| lock_50 | -$28,519 | $0 | 41.1% | 375 | 93 | 128 | 4 | 7 |
| lock_100 | -$28,519 | $0 | 41.1% | 375 | 93 | 128 | 4 | 7 |
| lock_150 | -$28,519 | $0 | 41.1% | 375 | 93 | 128 | 4 | 7 |
| dd_300 | -$24,705 | +$3,814 | 42.6% | 324 | 91 | 130 | 0 | 94 |
| **dd_500** ⭐ | **-$23,717** | **+$4,802** | **42.7%** | **328** | 91 | 130 | 0 | 70 |
| dd_750 | -$25,121 | +$3,398 | 41.6% | 341 | 92 | 129 | 0 | 51 |
| dd_1000 | -$27,546 | +$973 | 41.5% | 371 | 93 | 128 | 0 | 24 |
| combo_25_500 | -$23,717 | +$4,802 | 42.7% | 328 | 91 | 130 | 0 | 70 |
| combo_100_500 | -$23,717 | +$4,802 | 42.7% | 328 | 91 | 130 | 0 | 70 |

## Findings

### Rule #1 — Loss-lock — Falsified on this corpus

Across all four thresholds tested ($25, $50, $100, $150), Rule #1 produced **exactly $0 PnL change** and only **4 lock-rejects across 252 trading days**. The classical ORB engine in `tools/orb_backtest.py` has fewer same-(ticker, side) re-entries than the live `orb.live_runtime` engine — `ORB_MAX_TRADES_PER_DAY=5` is per-ticker but the signal logic produces minimal same-side re-entries within a day.

The **live engine** behaves differently. On 2026-05-12 alone, Main fired AMZN SHORT four times (entries at 264.05 → 263.97 → 263.12 → 264.01) and GOOG SHORT twice. Rule #1 against the live trade log (separate analysis earlier today) saved **+$665 on that single day**. So the rule has real value on the live path but **the classical backtest doesn't capture this dynamic**.

**Conclusion**: Rule #1 cannot be validated by `tools/orb_backtest.py`. A proper validation requires:
- (a) `tools/orb_replay_day.py` swept across the full year (replays through `orb.live_runtime`), OR
- (b) accumulating live trade-log history (now that the snapshot stream + v8.3.25's `/api/trade_log` are live), then doing trade-log replay across 2-4 weeks of v8.3.x production data.

### Rule #2 — Peak-drawdown halt — Validated, $500 threshold optimal

| Threshold | Net Δ | Days triggered | Entries blocked |
|---|---:|---:|---:|
| $300 (tight) | +$3,814 | 94 (37%) | 51 |
| **$500 (sweet spot)** | **+$4,802** | **70 (28%)** | **47** |
| $750 | +$3,398 | 51 (20%) | 34 |
| $1000 | +$973 | 24 (10%) | 4 |

Clear concavity around $500. Tighter ($300) halts on too many recoverable down-then-up days; looser ($1000) misses too many giveback days entirely.

**+$4,802 over 252 days = +$19/day average improvement**, applied across ~28% of trading days (70 days where the rule fired).

### Combined rules — Redundant with Rule #2 alone on this corpus

`combo_25_500` and `combo_100_500` produce **identical PnL to `dd_500` alone**. Rule #1's 4 lock-rejects either don't fire on days where Rule #2 hasn't already halted, or block trades that wouldn't have happened anyway. There's no additive interaction on this corpus.

The live engine should still get Rule #1 — its asymmetric value on giveback days isn't visible in this backtest framework. Combined rules are belt-and-suspenders; cost is zero (a few extra config fields).

## Strategy-level observation

**Baseline is -$28,519 on the year.** This contrasts with v12's headline Config A FY of +$24,875. The delta comes from today's tighter config:

| Setting | v12 baseline | v8.3.x today | Effect |
|---|---|---|---|
| `ORB_MAX_CONCURRENT_NOTIONAL_MULT` | 2.0 (default) | 0.95 (v8.3.20) | -position size on big-notional fires |
| `ORB_PARTIAL_PROFIT_AT_1R` | 0 (default) | 1 (v8.1.3) | caps winner upside at 1R + RR ride |
| `ORB_ENTRY/EXIT_SLIPPAGE_BPS` | 1.5 | 5.0 (v8 realism) | wider fills, smaller winners |

Each protective change shrinks PnL on historical data. The bot's live trading has been profitable for the operator, suggesting either (a) the slippage assumption is overly pessimistic for current Alpaca paper fills, (b) the corpus has structural differences from live execution, or (c) the strategy's edge has been narrowing in 2026 vs 2025.

**This is worth a separate investigation**; for the R6 question (do giveback defenses help?), the answer is **yes for Rule #2, defer for Rule #1**.

## Recommendation

1. **Ship Rule #2 (`ORB_PEAK_DD_HALT_USD=500`) to live**. Port to `orb.risk_book` as an env-gated halt similar to the existing `daily_loss_kill_pct`. Default ON at $500 with operator override env var.

2. **Defer Rule #1** until either an `orb_replay_day` full-year sweep validates it OR accumulate 2-4 weeks of v8.3.x production trade-log history and replay.

3. **Separate investigation**: the FY-baseline regression vs v12's +$24,875 deserves its own round of research (call it R7). Possible levers to test:
   - Disable `ORB_PARTIAL_PROFIT_AT_1R` and re-run (cheapest test)
   - Sweep `ORB_MAX_CONCURRENT_NOTIONAL_MULT` from 0.95 → 1.5 → 2.0
   - Reduce slippage assumption to 2.5bps (between v12's 1.5 and v8's 5.0)

## Falsified theories (this round)

So future research doesn't retry:
- **Loss-lock at $25/$50/$100/$150** — produces zero PnL change in the classical backtest. Try with `orb_replay_day` instead.
- **Peak-DD at $300** — over-halts; loses $1k vs $500 threshold.
- **Peak-DD at $1000** — under-halts; only catches 24 worst days.
- **Lock + DD combo** — identical PnL to DD-only on the classical backtest. (Live-engine value still untested.)

## Artifacts

- `docs/research/r6_drawdown_rules.py` — the sweep script (committed in v8.3.26).
- `docs/research/r6_results.json` — raw results dict (this PR).
- `tools/orb_backtest.py` — env vars `ORB_LOSS_LOCK_THRESHOLD_USD` + `ORB_PEAK_DD_HALT_USD` (v8.3.26).

## Next research round (R7 candidate scope)

Investigate the **v12 vs v8.3.x baseline regression** before deploying any new strategy lever:

1. Pin v12's exact config (notional 2.0, slippage 1.5, partial-profit off) on the FY corpus → confirm +$24,875 number reproduces.
2. Flip one lever at a time toward today's config and measure the cost.
3. Identify which protective change (notional cap, partial-profit, slippage) is the single biggest PnL drag.
4. Decide whether to re-tune that lever or accept the cost.
