---
name: backtest-research-methodology
description: How to run theory-driven backtest research loops on the v10 ORB strategy. Distills the R7→R16 arc (chase prevention + SPY regime + afternoon strategies) into a reproducible playbook: reproduce-the-baseline, production-realistic env, quarterly cross-validation, per-(ticker, side) forensics, plateau testing, falsified-theory documentation. Use after the operator says "research X" or "try a theory about Y" or asks for a multi-day backtest of a non-trivial rule change. Pairs with the `gha-backtest-lever-sweep` skill (which is the infrastructure layer); this skill is the methodology layer on top.
---

# Backtest research methodology

When the operator asks for a non-trivial theory test on the v10 strategy ("does X help?", "would Y prevent the bleed?", "can we recover edge on Z?"), follow this playbook. The infrastructure is documented in `gha-backtest-lever-sweep`; this is the **disciplined process** that converts a hypothesis into a validated config.

## The 7-phase loop

Each phase has a gate; do not advance until the prior phase's gate passes. Skipping ahead is the most common source of false wins.

### 1. Reproduce the current baseline FIRST

Before testing any new theory, run the prior round's "winner" config end-to-end on the local corpus (`/tmp/rth-data/data`) and confirm you reproduce the reported FY net within rounding. Two reasons this matters:

- **Corpus drift**: the GHA workflow `pull-rth-bars.yml` continually refreshes the bar archive. A backtest you ran yesterday may not reproduce today (one more day's worth of bars; replaced day with cleaner data). The R5_recheck_risk1pt0 baseline reproduced at $+24,864 vs the reported $+24,875 — close enough; that confirms the corpus is stable.

- **BASE config drift**: different research rounds quietly redefined the BASE env vars. R6's BASE shipped with `ORB_ENTRY_SLIPPAGE_BPS=5.0`, but R5 used `1.5`. R6 dropped the T5 blocklist that R5 carried. **The reported "v12 +$24,875 winner" was R5_BASE + risk=1%, NOT a generic baseline.** This is the v9 research's biggest near-miss: an entire R6→R10 arc almost shipped on the wrong baseline. Always print the BASE env dict, side-by-side compare to the prior round's, and reconcile before testing the new theory.

Gate: prior-round winner reproduces within $200/yr.

### 2. Establish the production-realistic BASE

The classical backtest's defaults do NOT match the live engine. Specifically:
- `ORB_ATR_STOP_MULT` defaults to 0 in `tools/orb_backtest.py:ORBConfig` but **1.75** in `orb/live_runtime.py:_build_config_from_env`
- `ORB_ENTRY_SLIPPAGE_BPS` defaults to 5 in production; some research rounds used 1.5
- `ORB_MAX_CONCURRENT_NOTIONAL_MULT` defaults to 0.95 in production; backtest default is 2.0
- `ORB_PARTIAL_PROFIT_AT_1R` defaults to True in production since v8.1.3

When testing a strategy change for production, **fix the BASE to match live engine defaults**. The R7 finding ($+5,359 FY lift from turning ATR ON) was hidden in plain sight because earlier rounds ran with ATR off and reported flat results.

Document the production-realistic BASE at the top of every `docs/research/r<N>_<theme>.py` script.

Gate: BASE in your sweep script matches `orb/live_runtime.py:_build_config_from_env` for every shared field.

### 3. Hypothesis → single-variable sweep first

Test the new theory in isolation against the production-realistic BASE before stacking. Run 4-6 levels of the new lever (e.g., `mbr=5, 10, 15, 20`). Two outputs per variant: FY headline net AND quarterly cross-validation (Q2-25, Q3-25, Q4-25, Q1Q2-26).

Gate: at least one threshold value beats the baseline FY net AND has equal-or-better `neg_q` count.

### 4. Quarterly cross-validation is mandatory

A single-year backtest can hide a quarter-concentrated win. The strategy's quarterly stability (`0/4` vs `1/4` vs `4/4` neg quarters) is the headline filter we optimize. Examples of why this matters:
- R10 winner `vwap_dev<=25` had FY $+17,266 but Q3-25 was the only quarter on the edge ($+2,144 — small).
- R12 regime-skip lifted Q2/Q3/Q4 ~equally, validating it as a structural win not a Q4-only patch.
- An "R-something" that boosts FY +$3K but introduces a neg quarter is **not a ship-able win** — discard.

Gate: candidate must hold `neg_q <= baseline_neg_q` across all 4 quarterly slices.

### 5. Plateau-test the winning threshold

Don't trust a single-point optimum. Sweep around it (±5 / ±10 / ±20 units) and confirm there's a wide plateau. R10b's vwap_dev sweep showed a plateau from 15-27bps all hitting $+16,750 to $+17,266 — that's robust. If the optimum is a sharp peak (a single value much better than neighbors), suspect overfit.

Gate: the proposed threshold sits inside a ≥3-point plateau where neighboring values produce within 5% of the optimum's FY net.

### 6. Per-(ticker, side) forensic when a universal filter underperforms

If a universal filter fails (e.g., `vwap_dev<=30` applied globally was $-12K worse than not applying), don't abandon the filter — fence it. The pattern:

1. Run the no-filter control. Save per-day P&L breakdowns.
2. Aggregate by `(ticker, side)` — sort worst-first.
3. Run rich indicators per trade: gap-from-prev-close, OR-shape, OR-direction, signal-bar volume, premkt direction, **vwap_dev (signed by side)**, time-of-day bucket, sig_bar_idx.
4. For each indicator, compute median for wins vs median for losses. The largest separation indicates the filter axis.
5. Apply the filter only to the affected ticker subset (`max_vwap_dev_tickers=META,MSFT,AAPL,AMZN,GOOG,AVGO`).

R10 unfenced vwap was $+4,724 (worse than baseline). R10 fenced was $+17,266. The fence is what made the filter work.

Gate: fenced filter matches or beats the equivalent outright-block on at least 90% of the metric.

### 7. Falsified theories MUST be documented before next round

After each round, write the dead theories with their numbers + reason into:
- The top-of-file docstring of `docs/research/r<N>_<theme>.py`
- The corresponding `docs/pl_optimization_final_report_v<N>.md` "Falsified" section

The v13 report's falsified list is 16+ items long. Future rounds use it to skip dead alleys. Skipping this documentation directly caused R6 to re-test `peak_dd_halt` after R10 had already shown it conflicts with ATR.

Gate: every variant whose `neg_q > baseline OR FY < baseline - 5%` is recorded with a one-line "why it failed".

## Concrete rounds reference (v9.0.0 arc)

| Round | Hypothesis | Result |
|---|---|---|
| R7 | `min_break_bps` filter | WINNER (+$500) |
| R8/R8b/R8c | non-blocking alternatives to T5 (mbr, ADX, RVOL, max_trades, partial blocks) | all underperform full T5 block |
| R9 | universal `vwap_dev` global filter | FALSE WIN ($+3.8K on no-T5 control; -$12K when stacked on T5 BASE) |
| R9c | T5-fenced `vwap_dev` | WINNER ($+15,632, 93% of T5-block) |
| R10 | tighter vwap thresholds + asymmetric + smaller/larger fence subsets | WINNER ($+17,266 at vwap<=25) |
| R10b | plateau test 15-27bps | confirmed wide plateau |
| R11 | fenced OR-width, fenced N-bar confirm, fenced higher mbr | none improve R10 |
| R12 | prior-day SPY regime skip | WINNER (+$7K) |
| R12b/c | regime threshold micro-sweep + DLK tightening | WINNER ($+24,784 at -0.40% threshold + DLK 1.0) |
| R13/R13b | conservative trading on regime-low days | all underperform full-skip (counter-intuitive but real) |
| R14 | cutoff sweep | confirmed 11:00 is global optimum; no cutoff costs $-17K |
| R15 | afternoon fade mode | FALSIFIED |
| R16 | mid-day OR / power-hour fresh anchor | FALSIFIED |

## Bootstrap projection with regime-skip days

When the strategy skips days, the bootstrap daily-return sample MUST include zero days for the skipped portion. v9.0.0's projection initially over-sampled from 201 active days only, producing 31.99% CAGR. Adding 50 zero days dropped it to 24.78% (correct) — a 7-point difference. The math: an N-year compound projection sampling only active days assumes the strategy is active 252 days/yr, which it isn't.

Implementation:
```python
returns_daily = []
for date, pnl in active_day_pnls:
    returns_daily.append(pnl / equity_at_that_day)
returns_daily.extend([0.0] * n_skipped_days)
random.shuffle(returns_daily)
```

## Anti-patterns (do not repeat)

- **Skipping phase 1 (reproduce baseline)** — costs hours of "why doesn't my new theory show any lift?"
- **Ignoring quarterly CV when FY is impressive** — a $+5K FY win that introduces a neg quarter is not a ship.
- **Declaring a single-point optimum without plateau-testing** — overfit risk.
- **Applying a filter universally when forensic shows the bleed is concentrated** — the fence pattern is almost always better.
- **Not writing down falsified theories** — costs days of re-running dead alleys.
- **Single-year sample as the only validation** — corpus is one year; out-of-sample years may behave differently. Apply 10-20% friction discount to projections.
- **Stacking new filters with v8.3.34 R6 defenses (`loss_lock`, `peak_dd_halt`)** without re-validation — R10 explicitly showed they conflict with chase-prevention; default OFF in v9.

## Output deliverables for a research round

A research round is "complete" when it produces:
1. The sweep script `docs/research/r<N>_<theme>.py` with its top-of-file docstring listing falsified theories
2. Per-variant JSON results in `/tmp/research_r<N>/<vid>/summary.json`
3. A quarterly CV table (Markdown) in the chat
4. A plateau-confirmation table (Markdown)
5. Either: (a) ship-ready config + delta + projection, OR (b) "no improvement — record in falsified list"

The `gha-backtest-lever-sweep` skill describes how to dispatch the sweep on GHA when you want to run >12 variants × 5 corpus slices in parallel. For local single-variant iteration, run `tools/orb_backtest.py` directly.
