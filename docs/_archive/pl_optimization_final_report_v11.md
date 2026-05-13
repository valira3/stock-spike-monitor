# P&L Optimization — Final Report v11 (Phase 13, multi-lever exploration)

Date: 2026-05-10
Branch: `claude/phase13-universe-screen`
Account: **$100,000** (paper)
Corpus: **124 trading days** (2025-11-03 → 2026-05-01)
Risk envelope: $2000/day cap
Compounding: **default ON** (rule #11b)

This report covers Phase 13's exploration of four ROI-lift candidates. **All four were rejected.** v10 anchor remains the deployable.

---

## TL;DR

| Phase 13 lever | Result | CAGR Δ |
|---|---|---:|
| #1 Add NFLX to blocklist | ❌ REJECTED | −8.4pp |
| #2 VIX threshold retune | ➖ no change | 0.0pp |
| #5 Vol-targeted sizing | ❌ REJECTED | −4 to −12pp |
| #6 Universe expand 12→25 | ❌ REJECTED | −18.1pp |

**v10 anchor stays the production config**: CAGR +43.0%, end balance $119,225 on 124-day corpus, every cross-val split positive (+5.5% to +73.7%).

---

## What we tested + why each failed

### #1 NFLX blocklist (-8.4pp)

NFLX in isolation: -$1,422 over 20 trades, 35% WR (clearly losing). Removing NFLX from the universe *increased* total trades 114 → 117 because budget slots opened for other tickers' signals on the same days.

**Why it hurts**: in compounded mode, the alternative trades that fill NFLX's slots happen at different timestamps and interact differently with the daily kill switch. The substitution path is path-dependent. Per-ticker P&L is **not additive** in compounded backtests.

**Audit subagent verified**: the mechanism is real, not a numerical bug.

### #2 VIX threshold retune (no change)

Tested VIX > {18, 20, 22, 24, 25, 28, off}.
- VIX > 22 (current): +43.0% CAGR
- VIX off: +44.7% in-sample but -16.4% on STRIDE=2 cross-val (much higher variance)
- VIX > 24 / 25 / 28: all worse on in-sample (loosening the gate let in March 2026 chop)
- VIX > 20: -3.6pp (over-conservative)

**VIX > 22 is a real local optimum** in the bias-variance trade-off space.

### #5 Vol-targeted sizing (-4 to -12pp)

Tested `target_atr_pct` ∈ {0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0}. Every threshold below v10 anchor.

**Why it hurts**: ORB is a volatility-amplified strategy — breakout EV scales with realized volatility (high-vol days produce wider signals that travel further). Down-sizing in high-ATR regimes throttles the strategy at its sweet spot. The Quantpedia paper's vol-targeting was for a mean-reverting / position-trading context, not a momentum breakout.

**Audit subagent verified**: math is correct, no look-ahead, no numerical bug. Shipped opt-in (defaults off) for future research.

### #6 Universe expand 12 → 25 (-18.1pp)

Fetched 13 new mega-cap tickers (BRK.B, JPM, V, JNJ, WMT, XOM, MA, HD, PG, COST, ABBV, CVX, KO) via new GHA workflow `pull-rth-bars.yml`. Re-ran v10 anchor on the 25-ticker universe.

| variant | CAGR | end |
|---|---:|---:|
| **v10 univ-12 (baseline)** | **+43.0%** | $119,225 |
| v10 univ-25 (expanded) | +24.9% | $111,559 |
| v10 univ-25 no NFLX | +18.0% | $108,469 |
| New-13 only | -11.2% | $94,297 |

**Counterintuitive but consistent with #1**: more candidates means more substitution. AMZN went from +$2,022 (12-tk) to **-$1,413** (25-tk) — same ticker, same strategy, just different signals competing for the daily $2k risk budget on the same dates. The compounding path completely changed.

The 13 new tickers as a standalone universe lose money (-11.2% CAGR). The 12 mega-caps were carefully selected; the broader S&P 500 mega-caps don't have the same ORB edge in this corpus.

**Worth noting**: WMT was the best-performing new ticker (+$5,234, 73% WR). A future iteration could swap a losing original ticker for WMT and screen, but that's beyond Phase 13 scope.

---

## Per-ticker analytics on the 25-ticker run

```
WMT     +$5,234  ( 11 trades, 73% WR)  ★ best new
AAPL    +$5,178  (  8 trades, 75% WR)
NVDA    +$2,803  (  9 trades, 67% WR)
JNJ     +$2,402  (  8 trades, 75% WR)  new
AVGO    +$2,002  (  9 trades, 78% WR)
KO      +$1,838  (  4 trades, 75% WR)  new
ORCL    +$1,770  (  1 trade, 100% WR)
NFLX    +$1,348  ( 11 trades, 45% WR)
XOM     +$1,289  (  5 trades, 40% WR)  new
HD      +$1,164  (  5 trades, 80% WR)  new
GOOG    +$1,034  ( 11 trades, 64% WR)
COST      +$538  (  3 trades, 67% WR)  new
V         -$190  (  6 trades, 50% WR)  new
MA      -$1,127  (  2 trades,  0% WR)  new
ABBV    -$1,128  (  4 trades, 75% WR)  new
BRK.B   -$1,209  (  5 trades, 60% WR)  new
AMZN    -$1,413  (  8 trades, 38% WR)
JPM     -$2,599  ( 14 trades, 43% WR)  new
CVX     -$3,310  (  6 trades, 17% WR)  new
TSLA    -$4,064  ( 10 trades, 40% WR)
```

7 new tickers profit, 6 lose. The losers (especially CVX -$3,310, JPM -$2,599) overwhelm the winners.

---

## Infrastructure delivered

Even though all 4 levers failed, the Phase 13 infrastructure is reusable:

1. **`tools/orb_backtest.py`** — vol-targeted sizing lever (opt-in, defaults off)
2. **`.github/workflows/pull-rth-bars.yml`** — RTH bar fetcher; can pull any ticker any date range
3. **`data-extensions/rth-expand`** — 13-ticker × 124-day data archive; already populated
4. **DST-aware bucket re-derivation** in `load_day_bars()` — handles fetcher timestamp bugs transparently
5. **Manager Agent overarching meta rule (#0)** in framework doc — supervises future workstreams

---

## Honest meta-takeaways

1. **Compounding interactions dominate** — per-ticker P&L is non-additive when daily risk budget is constrained. This is the most important learning from Phase 13.
2. **3/4 of industry-research-grounded levers don't apply** to our specific strategy + universe combo:
   - RVOL (Phase 11 finding) — wrong because Zarattini was a stock-PICKING filter
   - Vol-targeting — wrong because ORB EV scales with vol, not against it
   - Universe expand — wrong because more candidates = more substitution noise
   - NFLX block — wrong despite per-ticker losing because compounding path matters
3. **v10's tight 12-ticker universe is a feature, not a constraint.** The 12 mega-caps were chosen for their ORB-friendly volatility profile.
4. **The Manager Agent rule (#0) caught some procedural deviations** during Phase 13 (delayed merge, duplicate trigger commit). Future phases should run with the manager subagent active from the start.

---

## What would actually help next (Phase 14 candidates)

| candidate | rationale | risk |
|---|---|---|
| **Multi-year OOS test** | Validate v10 on 2024 + early 2025 data. Real out-of-sample. | Data fetch needed |
| **Per-ticker swap optimization** | Replace CVX/TSLA-equivalent loser with WMT-equivalent winner | Likely small lift |
| **Counter-trend strategy ensemble** | Run v10 alongside mean-reversion strategy on remaining capital. Lower correlation = higher Sharpe. | Significant build |
| **Active VIX/regime adjustment** | Currently uses VIX(D-1). Could add VIX5d-change or VIX/VIX9D for finer granularity. | Needs VIX9D feed |
| **Trailing stop / partial profit** | Existing code has these as opt-in but never tested in v10 context | Quick screen |

---

## Production deployment (unchanged from v10)

```bash
# v10 production config — STILL THE RECOMMENDATION
ORB_MODE=1
ORB_OR_MINUTES=30
ORB_RR=2.5
ORB_STOP_BUFFER_BPS=5
ORB_RANGE_MIN_PCT=0.008
ORB_RANGE_MAX_PCT=0.025
ORB_MAX_TRADES_PER_DAY=5
ORB_RISK_PER_TRADE_PCT=2.00
ORB_MAX_CONCURRENT_RISK_DOLLARS=2000
ORB_DAILY_LOSS_KILL_PCT=2.0
ORB_MAX_TRADE_NOTIONAL_PCT=75
ORB_MOVE_TO_BE_AFTER_1R=1
ORB_COMPOUND_DAILY=1
ORB_TICKER_SIDE_BLOCKLIST='{"META":["LONG","SHORT"],"MSFT":["LONG","SHORT"]}'
ORB_SKIP_GAP_ABOVE_PCT=1.5
ORB_SKIP_EARNINGS_WINDOW=1
ORB_EARNINGS_DAYS_BEFORE=1
ORB_SKIP_VIX_ABOVE=22
```

Honest expected CAGR: **+20–40%**, mid-point ~$120k–140k after 1 year on $100k.

---

This report is the consolidated state across PRs #463–#465. Phase 13 was a thorough negative-result investigation — every "obvious next lever" tested, all failed. Future phases should focus on cross-corpus validation (multi-year OOS) and qualitatively different signal sources (counter-trend ensemble) rather than more lever-tuning on v10.
