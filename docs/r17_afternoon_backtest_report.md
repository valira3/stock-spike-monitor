# R17 — Afternoon-strategy backtest (Gao 2015 + Baltussen 2024)

Date: 2026-05-13 ET
Corpus: 251 trading days (2025-05-12 → 2026-05-11), local mirror of `data-extensions/rth-expand`
Tool: `tools/afternoon_backtest.py` (new in this branch; standalone — does not modify or depend on `tools/orb_backtest.py`)

## TL;DR

Tested two academically-documented afternoon strategies on our v9 corpus:

1. **Intraday Momentum** (Gao, Han, Li, Zhou 2015; Zarattini, Aziz, Barbon 2024): sign of 09:30-10:00 ET return → trade SPY/QQQ in last 30 min same direction
2. **End-of-Day Reversal** (Baltussen, Da, Soebhag 2024): rank stocks by intraday return at 15:30 ET → long the losers, short the winners, hold to ~16:00

Result: **neither strategy is ship-able** as a v9 addition. Best variant (EOD reversal, top-1 only, optimistic 0.5bps slippage) gives +$1,329/yr but breaks v9's 0/4 negative quarters stability.

## What was tested

### Strategy 1: Intraday Momentum (SPY + QQQ)

Universe: SPY, QQQ
Signal: sign of 09:30-10:00 ET return (close at 9:59 vs open at 9:30)
Entry: 15:30 ET (the 15:30 bar's open), slipped adverse
Exit: 15:59 ET (the 15:59 bar's close)
Sizing: fixed-notional, 25% of equity per leg
Slippage tested: 0bps, 0.5bps, 1.5bps each side

| Variant | FY net | Entries | WR | avg/trade |
|---|---:|---:|---:|---:|
| Both tickers, no slippage | −$2,556 | 502 | 44.9% | −$5.09 |
| Both tickers, 0.5bps | −$3,759 | 502 | 40.8% | −$7.49 |
| Both tickers, 1.5bps | −$6,132 | 502 | 36.6% | −$12.22 |
| SPY only, 0.5bps | −$1,783 | 252 | 40.5% | −$7.07 |
| SPY only, no slippage | −$1,170 | 252 | 43.8% | −$4.64 |

**Literature claim: ~6.67% annualized (Gao 2015), Sharpe 1.08 on SPY 1993-2013.**
**Our result: negative across all variants including zero slippage.**

The strategy is negative GROSS on this corpus — WR ~44% (vs literature's ~52%). This is almost certainly:
- (a) **Alpha decay** — the Gao 2015 paper is widely known; the autocorrelation between 09:30-10:00 and 15:30-16:00 has been arbed out
- (b) **Sample regime** — 2025-26 may be unusually different from the 1993-2013 backtest
- (c) **Universe** — Zarattini 2024 reported 19.6% on SPY 2007-2024, but used more sophisticated entry timing (volatility-conditional, dynamic stops)

Either way: **don't ship**.

### Strategy 2: End-of-Day Reversal (12 mega-caps)

Universe: full v9 (AAPL, MSFT, NVDA, TSLA, META, GOOG, AMZN, AVGO, NFLX, ORCL, SPY, QQQ)
Signal: rank by intraday return (prior close → 15:30 today)
Trade: long top-N losers, short top-N winners
Entry: 15:30 ET; Exit: 15:59 ET
Sizing: 25% notional per leg

| Variant | FY net | Entries | WR | avg/trade | neg Q |
|---|---:|---:|---:|---:|:---:|
| top-1, 0.5bps slip | **+$1,329** | 502 | 49.4% | +$2.65 | 2/4 |
| top-2, 0.5bps slip | +$492 | 1002 | 49.0% | +$0.49 | 2/4 |
| top-3, 0.5bps slip | −$1,710 | 1504 | 47.9% | −$1.14 | — |
| top-2, no slippage | +$3,051 | 1002 | 50.4% | +$3.04 | — |
| top-2, 1.5bps slip | −$4,383 | 1002 | 45.7% | −$4.37 | — |

**Literature claim: 8.7-11.2% annualized (Baltussen 2024), driven by retail attention buying on biggest losers.**
**Our result: gross +$3,051 (matches the directional sign) but net of realistic slippage barely positive only at top-1 + ultra-tight slippage.**

The Baltussen effect IS real on our corpus (gross 50.4% WR with positive avg/trade) but it's small. Our 12-stock universe means top-1 = single trade per side; that's a tiny slice of the full cross-section that the paper uses (3000+ stocks decile-traded). The edge per trade is ~$3 gross; round-trip slippage at $25K notional is ~$7.50 at 1.5bps. Math doesn't work at realistic frictions.

### Quarterly stability (EOD top-1, 0.5bps slip — the best variant)

| Quarter | v9 morning-only | EOD reversal | Combined |
|---|---:|---:|---:|
| Q2-25 | +$1,222 | −$1,314 | **−$92** ← negative |
| Q3-25 | +$3,051 | −$1,097 | +$1,954 |
| Q4-25 | +$5,939 | +$809 | +$6,749 |
| Q1-26 | +$3,845 | +$2,879 | +$6,724 |
| Q2-26 (partial) | +$8,989 | +$52 | +$9,041 |
| **FY** | **+$23,047** | **+$1,329** | **+$24,376** |

EOD adds **$1,329/yr (+5.7%)** but **introduces a negative quarter** (Q2-25 combined goes from +$1,222 alone to −$92). Per the R3 rule (Quarterly CV mandatory), this fails the stability gate.

## Why the literature claims didn't transfer

1. **Alpha decay** (Gao 2015 especially). Public, well-known, widely traded patterns. Many quant funds run these.
2. **Universe size mismatch.** Baltussen uses 3000+ stocks; we have 12. The signal-to-noise on top-N=2 of 12 is much worse than top-decile of 3000.
3. **Slippage scaling.** Literature assumes institutional execution (0-0.5bps via auction). For a $100K paper account using market orders, realistic slippage is 1-3bps round-trip. That eats the small per-trade edge.
4. **Frequency tax.** EOD-reversal trades ~4 legs/day = 1000+ trades/year. Even 0.5bps per side compounds to thousands of dollars of friction.
5. **Hold-time mismatch.** Some literature uses 3:30-4:00 (30 min); we tested 15:30-15:55 first (25 min) before extending. Marginal difference.

## What was NOT tested but could change the picture

- **MOC imbalance fade** — requires NYSE/Nasdaq imbalance data feed (paid). Documented 25% returns at institutional scale in 2024, but data dependency makes it impractical for a $100K paper account.
- **0DTE gamma-driven pinning** — requires options-chain + gamma-exposure feed. Different infrastructure scale.
- **Filtered intraday-momentum** (Zarattini 2024 variant): require morning return ≥X bps, only on high-vol days, dynamic stop sizing. The base Gao strategy is what we tested; the refined Zarattini version may still have a sliver of edge.
- **Per-ticker fenced EOD reversal** — Baltussen's drivers are retail-attention; mega-caps with biggest retail flow (TSLA, NVDA, NFLX) may have larger effect than dispersed-investor names. Worth a forensic split.

## Recommendation

**Don't ship EOD reversal in v9.1.** The marginal +5.7% headline costs the 0/4 neg quarter stability that v9 was specifically optimized for. The R3 rule blocks this.

**Don't ship intraday-momentum.** Negative gross. Alpha is decayed for this strategy + this universe + this corpus.

**If you want to revisit:** the most promising variant not yet tested is **filtered intraday-momentum following Zarattini 2024** — only enter on high-vol days, use dynamic stops scaled to morning ATR. That's a different code path (not a simple sign-of-return signal). Worth a future R18 round if afternoon edge is still a priority.

For now: morning-only remains optimal. The 5 idle afternoon hours are not currently investable for this universe at this account size with realistic frictions.

## Artifacts

- `tools/afternoon_backtest.py` — standalone backtest (594 lines)
- `/tmp/r17b/` — initial sensitivity sweep (slippage + universe)
- `/tmp/r17c/` — refined low-slippage + top-N sweep
- `/tmp/r17_combined/` — v9 morning + EOD top-1 combined per-day P&L

Reproduce:
```bash
python3 tools/afternoon_backtest.py --strategy intraday_momentum \
    --corpus /tmp/rth-data/data --out /tmp/r17_im_fy

python3 tools/afternoon_backtest.py --strategy eod_reversal \
    --corpus /tmp/rth-data/data --out /tmp/r17_eod_fy \
    --slip-bps 0.5

# Per-quarter:
for q in cv_q2_2025 cv_q3_2025 cv_q4_2025 cv_q1q2_2026; do
    python3 tools/afternoon_backtest.py --strategy eod_reversal \
        --corpus /tmp/$q --out /tmp/r17_eod_$q --slip-bps 0.5
done
```
