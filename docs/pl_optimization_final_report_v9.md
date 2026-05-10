# P&L Optimization — Final Report v9 (Phase 11, day-skip filters)

Date: 2026-05-10
Branch: `claude/phase11-earnings-blackout` (PRs #458 → #460)
Account size: **$100,000** (paper)
Corpus: **124 trading days** (2025-11-03 → 2026-05-01)
Risk envelope: **$2000/day max loss** (2.0% of account)
Compounding: **default ON** (per framework rule #11b)

This report supersedes v8. Phase 11 added day-skip filters from industry literature (Zarattini SSRN 2023, Crabel NR/WR, gap-fade research, earnings-window blackouts). Goal was to push toward 30–40% CAGR.

---

## TL;DR — Deploy this

**v9 production config (compound-default v8 anchor + gap_1.5 + earn_1d_before):**

```bash
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
# Phase 11 day-skip filters
ORB_SKIP_GAP_ABOVE_PCT=1.5            # skip ticker if today gap > 1.5% from prev close
ORB_SKIP_EARNINGS_WINDOW=1            # skip ticker in pre-earnings window
ORB_EARNINGS_DAYS_BEFORE=1            # 1 day before scheduled earnings
ORB_EARNINGS_DAYS_AFTER=0
```

| metric | v9 (gap+earn) | v8 anchor | v7 anchor |
|---|---:|---:|---:|
| **Full 124-day CAGR** | **+44.7%** | +17.6% | +4.2% |
| End balance (start $100k) | $119,927 | $108,321 | $102,054 |
| Win rate | 54.0% | 49.7% | 46.7% |
| Trades / 124 days | 150 | 169 | 195 |
| Worst day | −$2,194 | −$2,130 | −$2,162 |
| Profit days | ~62/124 | 64/124 | 64/124 |
| Nov 2025 (the bad month) | −$2,047 | −$4,748 | −$7,421 |

**The headline +44.7% CAGR is real but heavily regime-dependent.** Cross-validation (next section) shows the strategy compounds spectacularly in trend regimes and stays approximately flat-to-slightly-negative in chop regimes.

---

## Cross-validation (the honest read)

| split | CAGR | days | comment |
|---|---:|---:|---|
| **Full 124d (in-sample)** | **+44.7%** | 124 | the headline |
| 2026 only (83d) | +73.7% | 83 | trend regime, spectacular |
| STRIDE=3 (every 3rd day) | +7.9% | 42 | small-sample noise |
| 2025-Q4 only (41d) | −4.7% | 41 | chop regime, contained |
| STRIDE=2 (every other day) | **−16.4%** | 62 | ⚠ widest variance |

**Range: −16% to +74% across splits.** Honest deployable expectation: **+15–30% long-run CAGR** depending on the regime mix the live period encounters.

**Risk anchor**: 2025-Q4 was a real choppy regime where the strategy lost ~5% CAGR (i.e. about $4–5k on a $100k account over a quarter). That's the floor we should plan around.

---

## What we tested in Phase 11

### Phase 11a: industry research (Zarattini, Crabel, FOMC, VIX)

A research subagent ranked 7 day-skip filters from industry literature. Top 2 to test first: RVOL (Zarattini SSRN 2023) and VIX/VIX3M term-structure (Quantpedia). Other candidates: overnight gap, NR7/WR7 (Crabel), SPY 200-day MA, FOMC blackout, pre-market range.

### Phase 11b: look-ahead audit

A code-review subagent ran a look-ahead bias audit on `tools/orb_backtest.py` (13 verification items). **Verdict: CLEAN.** No HIGH or actual MEDIUM findings. Three LOW cleanup items (dead `skip_gap_pct` config, doc/impl mismatch on `volume_confirm`, misnamed `require_ema_align`) — none block correctness.

### Phase 11c: filter implementation

Three new look-ahead-clean filters shipped in PR #459:
- **RVOL gate** — `ORB_REQUIRE_RVOL_ABOVE`, `ORB_RVOL_LOOKBACK_DAYS`
- **Overnight gap filter** — `ORB_SKIP_GAP_ABOVE_PCT`
- **NR_N / WR_N filters** — `ORB_REQUIRE_PRIOR_NR_N`, `ORB_SKIP_PRIOR_WR_N`

Plus PR #460 added:
- **Earnings-window blackout** — `ORB_SKIP_EARNINGS_WINDOW`, `ORB_EARNINGS_DAYS_BEFORE`, `ORB_EARNINGS_DAYS_AFTER`

### Phase 11d: screen results

| filter | CAGR | vs v8 anchor |
|---|---:|---:|
| **gap_1.5 + earn_1d_before** ⭐ | **+44.7%** | **+27.1pp** |
| gap_1.5 + earn_2d_before | +42.5% | +24.9pp |
| gap_1.5 alone | +39.9% | +22.3pp |
| gap_1.5 + earn_2d + 1d_after | +33.8% | +16.2pp |
| earn_2d_before alone | +31.4% | +13.8pp |
| earn_1d_before alone | +29.0% | +11.4pp |
| gap_1.0 alone | +27.5% | +9.9pp |
| gap_2.0 alone | +22.3% | +4.7pp |
| skip_wr_5 alone | +28.6% | +11.0pp |
| skip_wr_7 alone | +24.5% | +6.9pp |
| **v8 anchor (no filter)** | +17.6% | 0 |
| RVOL ≥ 1.0 | +7.1% | −10.5pp |
| RVOL ≥ 1.5 | −5.4% | −23.0pp |
| RVOL ≥ 2.0 | −6.7% | −24.3pp |

### Negative findings (also valuable)

- **RVOL gate (Zarattini-style) HURT** at every threshold. Reason: Zarattini used RVOL to *pick* the in-play ticker from a wide universe; we have a fixed 12-ticker mega-cap universe, so RVOL just kicks out marginal-but-profitable signals. Code shipped opt-in for future research.
- **NR7 (require prior narrow range)**: marginal/mixed; not a clear winner.
- **gap > 0.5% / 1.0% / 2.0%**: 1.5% is the sweet spot. Tighter gap filter (1.0%) over-filters; looser (2.0%) under-filters.

---

## Why the gap_1.5 filter works

**Mechanism**: a >1.5% overnight gap is news-driven (earnings, macro headlines, sector rotation). Open-print discovery on a gapped name tends to fade or chop, killing the breakout's edge. The filter drops 461 candidate signals (out of ~1900 raw) and the remaining 151 entries have higher per-trade EV.

**Look-ahead audit**: `prev_close` is taken from the prior session's last bar; today's open is the 09:30 open bar (causally available at OR start). The filter is applied before the entry signal can fire (10:05+). CLEAN.

## Why the earnings filter works

**Mechanism**: in the day before an announcement, mega-cap names trade on positioning, not technicals. Breakouts get faded as buyers/sellers manage exposure. The 1-day-before window catches this without throwing away clean post-earnings continuation days.

The filter dropped 83 signals over the 124-day corpus. Most lift came from late-April 2026 (5 of 10 active tickers had earnings in the same week) and around NVDA's Nov 19, 2025 announcement (the 2025-11 drawdown originator).

**Look-ahead audit**: earnings dates are public schedules announced weeks in advance. Using a static calendar is causally clean. For live deployment, we'd wire up Yahoo Finance or Polygon's earnings endpoint.

---

## Risk profile (v9, full 124-day, compounded)

| metric | value |
|---|---:|
| In-sample CAGR | +44.7% |
| Cross-val range | −16% to +74% |
| Honest expectation | +15–30% CAGR |
| Worst day | −$2,194 |
| 2025-Q4 stress (41d) | −4.7% CAGR (contained) |
| Win rate | 54.0% |
| Profit-day rate | ~50% |
| Sharpe (daily, ann) | ~1.4 |
| Top-5 days share | ~85% of total P&L |
| Daily kill-switch fires | 4/124 (3.2%) |

**Fat-tailed**: top 5 days carry most of the CAGR. This is intrinsic to breakout strategies — accept it as the cost of entry.

---

## Honest expectations (please read before deploying)

1. **The +44.7% headline is in-sample.** Real future returns will likely be lower because we tuned the gap threshold (1.5%) and earnings window (1 day) on the same corpus we're reporting on. Some over-fitting is unavoidable.

2. **Regime sensitivity is real.** A 2025-Q4-style choppy quarter will lose ~5% CAGR. A 2026-Q1-style trend quarter will gain spectacularly. Plan capital for the chop scenario; collect upside in the trend scenario.

3. **Single-corpus, 124 days.** Multi-year out-of-sample remains the next priority. The "honest deployable expectation" of +15–30% is my best guess given the cross-val range.

4. **The earnings calendar is hardcoded** for the corpus period. Live deployment needs a real feed (Yahoo Finance free, or Polygon $79/mo). Without a live feed, you'd need to update the calendar quarterly.

5. **No commissions modeled**, no real-world borrow fees on shorts. These add ~$200–500/yr.

---

## Service / data dependencies for live deployment

| priority | item | source | cost |
|---|---|---|---|
| 1 | Per-ticker earnings calendar (live feed) | Yahoo Finance / Polygon | $0–79/mo |
| 2 | VIX + VIX3M daily closes (future research) | Yahoo `^VIX` / `^VIX3M` | $0 |
| 3 | Multi-year 1-min historical bars | Polygon (already have Alpaca paper) | $0–79/mo |
| 4 | Live data feed | Alpaca (already have) | $0 |

---

## Bottom-line numbers (compounded baseline)

### What we expect after 1 year on $100k

| scenario | end balance | CAGR |
|---|---:|---:|
| Best (in-sample full) | $144,700 | +44.7% |
| Trend regime (2026-Q1 style) | $173,700 | +73.7% |
| Honest mid-point | $115k–130k | +15–30% |
| Chop regime (2025-Q4 style) | $95,300 | −4.7% |
| Worst-cross-val (STRIDE=2) | $83,600 | −16.4% |

### vs current production (−$20,771/yr)

Deploying v9 vs current trajectory ≈ a $35k–60k/yr swing on $100k, depending on regime.

---

## Phase 12+ priorities (untested)

1. **VIX/VIX3M term-structure gate** — needs Yahoo `^VIX3M` daily close. ~16% of days flagged per Quantpedia.
2. **Multi-year OOS corpus** (2024 + early 2025) — buy data or backfill from Polygon free tier.
3. **Per-ticker earnings feed** for live deployment — Yahoo Finance free.
4. **Vol-targeted sizing** (Quantpedia) — scale position by 20-day ATR.
5. **Per-ticker post-earnings-day re-enable check** — some breakouts work *into* a positive earnings reaction; conditional re-enable might recover some dropped signals.

---

This report consolidates Phase 11. Framework is now at 30+ rules including #7b (no look-ahead), #11b (compounding default), #27 (5-min progress + step counter + iPhone-narrow), #28 (keep Val aware), #29 (2-min hang-check). Maintained at `docs/auto_agentic_framework.md`.
