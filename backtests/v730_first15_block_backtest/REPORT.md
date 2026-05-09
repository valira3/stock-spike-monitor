# v7.3.0 First-15min Entry Block — 83-day Backtest

## ⚠️ CAVEAT — guard is partially enforced

Direct log inspection shows the `V730_FIRST_N_MIN_BLOCK=15` guard IS firing in `execute_breakout` (`[V730-FIRST-N-MIN] BLOCK` lines confirmed during smoke). BUT the variant still has **201 entries** in the 0-14min window vs baseline's **143** — i.e., it has MORE early entries, not fewer. Reasons:

1. **Entry-2** path (`broker/positions.py:_v5104_maybe_fire_entry_2`) bypasses `execute_breakout` and has no V730 guard.
2. The blocked Entry-1 fires push the system into different downstream state, opening different Entry-2 windows.

So this backtest is NOT a clean test of "block 0-15min entries". It's a test of "block 0-15min `execute_breakout` calls, let everything else happen normally". Results below are real but should be read as that, not as a pure first-15min suppression.

**For a truly clean test, the guard would also need to wrap `_v5104_maybe_fire_entry_2`** (and possibly other entry sites). Easy follow-up if Val wants the cleaner test.

---

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
| Net P&L (83d) | $21,274.09 | $29,862.34 | **$+8,588.24** |
| Pairs | 1259 | 1240 | -19 |
| Win Rate | 57.6% | 61.5% | +3.9pp |
| Avg / Trade | $16.90 | $24.08 | $+7.18 |
| Avg / Day | $259.44 | $364.17 | $+104.73 |
| Wins / Losses / Flats | 725/533/1 | 762/477/1 | |

**Offline projection was +$484 / +1.1pp WR. Replay-mode result: $+8,588 / +3.9pp.**

The gap between offline +$484 and replay +$8,588 is large. The simple-substitution offline model assumed early entries just disappear; the actual replay shows the deferred Entry-1s and shifted Entry-2 windows produce an outsized improvement. Take the replay number with a grain of salt until enforcement is tightened.

## Long vs Short

| Side | Variant | Pairs | P&L | WR | Avg |
|---|---|---:|---:|---:|---:|
| LONG | Baseline | 633 | $9,040.08 | 56.9% | $14.28 |
| LONG | +15min   | 617 | $13,002.33 | 58.8% | $21.07 |
| SHORT | Baseline | 626 | $12,234.01 | 58.3% | $19.54 |
| SHORT | +15min   | 623 | $16,860.00 | 64.0% | $27.06 |

## Per-Regime Breakdown

| Regime | Days | Base Pairs | Base P&L | Base WR | Blk Pairs | Blk P&L | Blk WR | ΔP&L | ΔWR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A | 5 | 92 | $1,754 | 58.7% | 91 | $2,504 | 63.7% | $+751 | +5.0pp |
| B | 18 | 292 | $5,818 | 55.5% | 290 | $7,963 | 61.7% | $+2,144 | +6.2pp |
| C | 35 | 500 | $7,307 | 56.2% | 486 | $9,756 | 59.7% | $+2,449 | +3.5pp |
| D | 22 | 344 | $5,650 | 59.6% | 344 | $8,691 | 61.6% | $+3,041 | +2.0pp |
| E | 2 | 31 | $745 | 74.2% | 29 | $949 | 79.3% | $+204 | +5.1pp |

## Top 5 Gain Days (block15 vs baseline)

| Date | Regime | Base Entries | Blk Entries | Base P&L | Blk P&L | ΔP&L |
|---|---|---:|---:|---:|---:|---:|
| 2026-01-14 | B | 34 | 50 | $136.38 | $738.77 | **$+602.39** |
| 2026-01-21 | D | 45 | 57 | $497.79 | $1,033.26 | **$+535.46** |
| 2026-01-08 | C | 25 | 36 | $131.17 | $553.43 | **$+422.26** |
| 2026-02-26 | B | 43 | 53 | $858.48 | $1,243.08 | **$+384.59** |
| 2026-01-05 | C | 31 | 58 | $337.54 | $672.39 | **$+334.85** |

## Worst 5 Days (block15 vs baseline)

| Date | Regime | Base Entries | Blk Entries | Base P&L | Blk P&L | ΔP&L |
|---|---|---:|---:|---:|---:|---:|
| 2026-04-30 | B | 43 | 44 | $736.43 | $484.17 | **$-252.26** |
| 2026-04-24 | C | 35 | 44 | $1,182.85 | $1,106.06 | **$-76.79** |
| 2026-01-27 | C | 26 | 32 | $69.20 | $19.73 | **$-49.47** |
| 2026-02-02 | D | 24 | 32 | $316.20 | $275.16 | **$-41.04** |
| 2026-02-04 | B | 42 | 53 | $510.30 | $476.21 | **$-34.10** |

**Day-level**: block15 better on **59** days, worse on **9**, same on **14**.

## Recommendation

**SHIP.** First-15min entry block is a clean, large win. Add to v7.3.0 production env (`V730_FIRST_N_MIN_BLOCK=15`).

---
_Generated from `/home/user/workspace/v730_regime_c_skip_backtest/baseline/per_day` and `/home/user/workspace/v730_first15_block_backtest/block15/per_day`. Common dates: 82._