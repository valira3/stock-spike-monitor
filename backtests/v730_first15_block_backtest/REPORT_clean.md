# v7.3.0 First-15min Entry Block — 83-day Backtest

**Hypothesis**: blocking new entries within 15 min of 09:30 ET (i.e., no entries before 09:45 ET) avoids the worst-WR window across all regimes.

**Setup**
- Bot version: v7.2.7 (current prod)
- Settings: L=30 / S=30 / VOLUME_GATE_ENABLED=true / RATIO=0.85 (live prod as of 2026-05-07)
- Corpus: 83 days, 2026-01-02 → 2026-05-01, v7.0.7 SIP archive
- Universe: 12 prod tickers + warmup seeded (55 days of pre-corpus history)
- Variant: `V730_FIRST_N_MIN_BLOCK=15` env-gated guard in `broker/orders.py`

## Headline

| Metric | Baseline | +15min Block | Δ |
|---|---:|---:|---:|
| Net P&L (83d) | $1,665.12 | $249.11 | **$-1,416.01** |
| Pairs | 1296 | 1259 | -37 |
| Win Rate | 49.5% | 48.9% | -0.6pp |
| Avg / Trade | $1.28 | $0.20 | $-1.09 |
| Avg / Day | $20.06 | $3.00 | $-17.06 |
| Wins / Losses / Flats | 642/654/0 | 616/643/0 | |

**Offline projection was +$484 / +1.1pp WR. Replay-mode result: $-1,416 / -0.6pp.**

## Long vs Short

| Side | Variant | Pairs | P&L | WR | Avg |
|---|---|---:|---:|---:|---:|
| LONG | Baseline | 644 | $151.40 | 49.8% | $0.24 |
| LONG | +15min   | 621 | $-970.93 | 48.1% | $-1.56 |
| SHORT | Baseline | 652 | $1,513.72 | 49.2% | $2.32 |
| SHORT | +15min   | 638 | $1,220.04 | 49.7% | $1.91 |

## Per-Regime Breakdown

| Regime | Days | Base Pairs | Base P&L | Base WR | Blk Pairs | Blk P&L | Blk WR | ΔP&L | ΔWR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A | 5 | 92 | $520 | 48.9% | 91 | $548 | 52.7% | $+28 | +3.8pp |
| B | 19 | 319 | $-459 | 45.5% | 309 | $-457 | 46.9% | $+3 | +1.5pp |
| C | 35 | 503 | $602 | 49.9% | 486 | $-561 | 47.7% | $-1,163 | -2.2pp |
| D | 22 | 351 | $951 | 52.4% | 344 | $594 | 50.9% | $-357 | -1.5pp |
| E | 2 | 31 | $52 | 54.8% | 29 | $125 | 55.2% | $+73 | +0.3pp |

## Top 5 Gain Days (block15 vs baseline)

| Date | Regime | Base Entries | Blk Entries | Base P&L | Blk P&L | ΔP&L |
|---|---|---:|---:|---:|---:|---:|
| 2026-01-30 | C | 15 | 13 | $-194.58 | $-33.49 | **$+161.09** |
| 2026-01-16 | B | 17 | 15 | $-106.36 | $48.34 | **$+154.70** |
| 2026-03-04 | C | 9 | 7 | $-63.89 | $82.26 | **$+146.15** |
| 2026-04-06 | D | 16 | 15 | $8.18 | $142.98 | **$+134.80** |
| 2026-02-06 | D | 12 | 11 | $-218.11 | $-117.37 | **$+100.73** |

## Worst 5 Days (block15 vs baseline)

| Date | Regime | Base Entries | Blk Entries | Base P&L | Blk P&L | ΔP&L |
|---|---|---:|---:|---:|---:|---:|
| 2026-01-05 | C | 19 | 18 | $51.19 | $-324.69 | **$-375.88** |
| 2026-03-02 | D | 23 | 23 | $-26.48 | $-330.01 | **$-303.53** |
| 2026-01-02 | C | 24 | 23 | $345.21 | $74.92 | **$-270.28** |
| 2026-02-09 | D | 19 | 19 | $334.84 | $123.30 | **$-211.54** |
| 2026-01-14 | B | 22 | 22 | $101.99 | $-82.77 | **$-184.76** |

**Day-level**: block15 better on **31** days, worse on **36**, same on **16**.

## Recommendation

**Don't ship.** Replay-mode test failed to reproduce the offline projection.

---
_Generated from `/home/user/workspace/v730_first15_block_backtest/baseline_clean/per_day` and `/home/user/workspace/v730_first15_block_backtest/block15_clean/per_day`. Common dates: 83._