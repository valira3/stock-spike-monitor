# P&L Optimization — Final Report v13 (Phase 15, fenced chase-prevention)

Date: 2026-05-13 ET
Branch: `claude/r7-min-break-bps-lever`
Account: **$100,000** (paper)
Corpus: **251 trading days** (2025-05-12 → 2026-05-11) — same full-year corpus as v12, RTH-only on `data-extensions/rth-expand`
Universe: 12 tickers (AAPL, MSFT, NVDA, TSLA, META, GOOG, AMZN, AVGO, NFLX, ORCL, SPY, QQQ)
Risk envelope: $2000/day concurrent cap, risk_per_trade=1.0%, ATR×1.75 stops, partial-at-1R, move-to-BE-after-1R
Compounding: ON

---

## TL;DR

v12's headline winner (Config A: risk=1.0% + 6-ticker T5 block + cut=11:00 + VIX≤20) was layered on top of an **OR-edge stop assumption**. Production runs ATR×1.75 stops (live default since v8.0.1). Re-baselining v12 under the production stack and adding two new code levers (`min_break_bps`, `max_vwap_dev_bps` with ticker fence) reproduces **and beats** the T5-block result — **without banning any tickers**.

| Rank | Config (delta from current production) | FY net | neg_q | Q4-25 | Tickers banned |
|---|---|---:|:---:|---:|:---:|
| 1 | **`vwap_dev≤25` on 6 mega-caps + `min_break_bps=5` + cut=11 + VIX≤20** | **$+17,266** | **0/4** | $+4,694 | **0** |
| 2 | T5 block + ATR + `min_break_bps=5` + cut=11 + VIX≤20 (R7 winner) | $+16,720 | 0/4 | $+5,159 | 6 |
| 3 | T5 block + ATR + cut=11 + VIX≤20 (just env, no code) | $+16,220 | 1/4 | $+6,961 | 6 |
| 4 | T5 block (no ATR, OR-edge stops) — v12's claimed Config A baseline | $+10,861 | 0/4 | $+1,757 | 6 |
| 5 | Current production (no v12 levers in Railway env) | **−$29,290** | **4/4** | −$9,117 | 0 |

**Recommended for production: Config #1 — the fenced chase-prevention rule.** Same headline as the full-ban approach, no ticker bans, and the rule encodes a forensically-validated failure mode (long entries chasing >25bps past session VWAP on mega-caps).

---

## Why v12's "win" understated the real picture

v12 reported R5_recheck_risk1pt0 = +$24,875 / 0/4 neg. That backtest used `ORB_ENTRY_SLIPPAGE_BPS=1.5` and **did not include ATR stops** (`ORB_ATR_STOP_MULT` unset → defaults to 0.0 in the classical backtest). Production has been running:
- `ORB_ENTRY_SLIPPAGE_BPS=5.0` (per `r6_drawdown_rules.py` BASE)
- `ORB_ATR_STOP_MULT=1.75` (live engine default since v8.0.1)
- `ORB_MAX_CONCURRENT_NOTIONAL_MULT=0.95` (v8.3.20)
- `ORB_PARTIAL_PROFIT_AT_1R=1` (v8.1.3)

Re-running v12 Config A under that **production-realistic stack** gives **$+10,861/yr** — not $+24,875. The $14K gap is mostly the slippage realism delta.

**Most importantly, ATR×1.75 stops were an un-swept lever in v12.** Switching them ON in the classical backtest (matching live engine default) on top of v12 Config A lifts FY from $+10,861 → $+16,220 (R7 finding). **Q4-25 specifically goes from $+1,757 → $+6,961** — exactly the historical pain quarter where ATR's tighter stops shrink risk_dollars and let `risk_per_trade=1.0%` size up share count, amplifying winners on a quarter that was previously thin.

---

## Production state (audited 2026-05-12 via snapshots-live branch)

The live engine has:
- `atr_stop_mult` = 1.75 ✓
- `risk_per_trade_pct` = 1.0 ✓
- `partial_profit_at_1r` = True ✓
- `move_to_be_after_1r` = True ✓
- `max_concurrent_notional_mult` = 0.95 ✓
- v8.3.34 loss-lock + dd-halt code installed (defaulted OFF)

But Railway env does **NOT** have:
- `ORB_TICKER_SIDE_BLOCKLIST` — empty → all 12 tickers tradeable
- `ORB_TIME_CUTOFF_ET` — defaults to 15:55 → trades all day
- `ORB_SKIP_VIX_ABOVE` — defaults to 22 → relaxed VIX gate

This explains why production is operating closer to the "no v12 levers" baseline. The single v10 trading day (2026-05-12) saw Main reach $1,080 peak intraday and give back to $35 — consistent with the un-filtered config's daily-giveback pattern.

---

## The chase-prevention forensic (R8 → R10)

### Per-(ticker, side) bleed on the no-block control
Running the full universe at production-realistic settings with ATR ON gave −$14,081 FY (3/4 neg). The 6 mega-caps collectively contributed −$15,749; the non-T5 6 tickers (NVDA, TSLA, NFLX, ORCL, SPY, QQQ) collectively contributed +$1,668.

| Pair | FY net | WR | Winner break-bps med | Loser break-bps med |
|---|---:|---:|---:|---:|
| MSFT/long | −$2,861 | 35% | +19.3 | +13.3 |
| AMZN/short | −$2,705 | 20% | +36.3 | +23.0 |
| AAPL/long | −$2,093 | 31% | +17.5 | +12.6 |
| AAPL/short | −$1,748 | 20% | +21.1 | +18.6 |
| GOOG/short | −$1,784 | 42% | +16.2 | +17.8 |
| AVGO/short | −$1,267 | 43% | +18.2 | +25.4 |
| META/short | −$1,200 | 40% | +22.1 | +16.6 |
| GOOG/long | −$838 | 59% | +13.6 | +16.4 |
| AVGO/long | −$826 | 50% | +12.4 | +13.2 |
| META/long | −$377 | 22% | +44.8 | +21.8 |
| AMZN/long | −$72 | 57% | +20.0 | +38.3 |
| MSFT/short | +$22 | 33% | +17.8 | +9.1 |

### The dominant signal: vwap_dev_bps
Rich-indicator forensics (gap, OR-shape, OR-direction, VWAP deviation, signal-bar volume, premkt direction) on 132 T5 trades showed the cleanest separator of wins vs losses across all 6 tickers is **`vwap_dev_bps`** — the signed distance from session VWAP at entry. Losers consistently enter when price has already extended **far past VWAP in the breakout direction**:

| Pair | vwap_dev (wins) | vwap_dev (losses) | Δ |
|---|---:|---:|---:|
| AAPL/long | −23bps | +53bps | −76 |
| META/long | −39bps | +74bps | −113 |
| AMZN/long | +51bps | +121bps | −70 |
| META/short | +26bps | +82bps | −57 |
| GOOG/short | +1bps | +70bps | −69 |
| MSFT/short | +57bps | +27bps | +30 (inverted) |

Across 5 of 6 long-side pairs and 2 of 3 short-side pairs, losers chase the move >50bps past session VWAP. This is the classic "FOMO chase" failure mode — ORB breakouts are highest-edge while still attached to VWAP.

### Why universal application kills good trades
Applied to the full universe, `ORB_MAX_VWAP_DEV_BPS≤30` filters out NVDA/ORCL/SPY-style chase wins where chasing extension IS the working pattern. T5 + universal vwap≤30 = $+4,724 vs T5 + nothing = $+16,720 — universal filter erases the gain.

### The fenced solution
Apply `vwap_dev≤25` **only to META, MSFT, AAPL, AMZN, GOOG, AVGO**. Leave the rest free. The 6 mega-caps trade — but only the early/attached breakouts, never the extensions. The other 6 tickers keep their chase-style winners.

---

## Threshold robustness (R10b)

| Threshold | FY net | neg_q | entries |
|---|---:|:---:|---:|
| vwap≤15 | $+17,249 | 0/4 | 185 |
| vwap≤18 | $+17,249 | 0/4 | 185 |
| vwap≤20 | $+16,750 | 0/4 | 186 |
| vwap≤22 | $+16,712 | 0/4 | 186 |
| **vwap≤25** | **$+17,266** | **0/4** | **187** |
| vwap≤27 | $+17,266 | 0/4 | 187 |
| vwap≤30 | $+15,632 | 0/4 | 187 |
| vwap≤35 | $+13,534 | 0/4 | 193 |
| vwap≤50 | $+1,296 (with T5 BLOCK) | 1/4 | — |

A wide plateau between 15-27bps hits $+16,750 to $+17,266. The exact threshold within this band doesn't matter — the filter is essentially "tight enough to catch all extensions". Going looser (35+) bleeds back into the losing-extension pattern.

---

## Falsified during R8 / R8b / R8c / R9 / R10

These were tested as alternatives to the T5 block (or fence) and DID NOT recover the lift. Future research should skip these:

- **Universal `min_break_bps=10, 15, 20`** — over-filters non-T5 winners; best alone = −$2K
- **N-bar confirmation (N=2 or N=3) globally** — wrecks Q4 grit
- **Universal `vwap_dev≤30` (no fence)** — kills the non-T5 chase winners; even T5 + universal vwap≤30 drops $12K
- **ADX > 20 or 25** — feature returns 0 entries in this corpus (warm-up issue suspected)
- **RVOL > 1.0, 1.2** — small effect, still negative without block
- **`max_trades_per_day=1`** — small effect alone, no synergy with chase filter
- **`peak_dd_halt=500`, `loss_lock=150` (R6)** — STACKED ON ATR HURTS by $1-1.5K. ATR's tighter stops already neutralize the bleed pattern R6 was designed for.
- **Asymmetric vwap (long-tight, short-loose)** — both sides need the tight filter equally; symmetric beats asymmetric by $5K
- **Smaller fence** (drop GOOG, AVGO, or any single mega-cap) — performance falls; all 6 share the chase pattern
- **Larger fence** (add NFLX or TSLA to fence) — kills their chase winners
- **Premkt-direction filter (`ORB_PREMKT_ALIGN_BPS`)** — per-pair direction non-universal (works for some, hurts others). Plus a fidelity bug when premkt bars missing.
- **Universal range_max tightening (0.018-0.022)** — already falsified in v12
- **`skip_first_5min`** — already falsified in v12
- **VWAP-align (strict directional)** — already falsified in v12 (R8)
- **`require_ema_align`** — small sample on intraday data

---

## Implementation path

### v8.3.35 code ship (one PR)

Three new env levers + their plumbing to live engine:

1. **`ORB_MIN_BREAK_BPS=N`** (default 0/off): require signal close to be N bps past OR_high (long) or OR_low (short) before admitting. Port from `tools/orb_backtest.py:ORBConfig` (R7) to `orb/engine.py:detect_breakout`.

2. **`ORB_MAX_VWAP_DEV_BPS=N`** (default 0/off): reject if entry price is more than N bps past session VWAP in the breakout direction. Needs session-VWAP tracking in live runtime — production's `scan.py` already aggregates bars; expose cumulative `price*volume` and `volume`. Port from `tools/orb_backtest.py:session_vwap_at` to `orb/state.py` or a new `orb/vwap.py`.

3. **`ORB_MAX_VWAP_DEV_TICKERS=AAPL,MSFT,...`** (default empty/global): comma-separated ticker fence for the vwap filter. When non-empty, the filter only applies to listed tickers.

Tests in `tests/strategy/test_orb_v8335_chase_filter.py` covering:
- Symmetric global threshold
- Per-ticker fence (filter applies only to listed tickers)
- Defaults-off (legacy behavior preserved)
- Session VWAP computation across day boundary

### Operator activation (Railway env, no deploy)

Once v8.3.35 ships, operator sets:

```bash
ORB_TIME_CUTOFF_ET=11:00
ORB_SKIP_VIX_ABOVE=20
ORB_MIN_BREAK_BPS=5
ORB_MAX_VWAP_DEV_BPS=25
ORB_MAX_VWAP_DEV_TICKERS=META,MSFT,AAPL,AMZN,GOOG,AVGO
# Leave ORB_TICKER_SIDE_BLOCKLIST unset
```

Expected lift vs current production: **+$46,556/yr** (−$29,290 → +$17,266).

### Operator override path

If the fenced filter falls behind in live operation, the operator can:
- Tighten threshold further: `ORB_MAX_VWAP_DEV_BPS=18` (still in robust plateau)
- Loosen if filtering too aggressively: `=30` (slightly less effective but still 0/4 neg)
- Expand fence list: add/remove tickers without deploy
- Disable: `=0` (back to v8.3.34 behavior)

---

## What was tested and validated

- R7 (`tools/orb_backtest.py` + r6 BASE): `min_break_bps=5` alone adds $500 over ATR-only, restores 0/4 neg
- R9 (per-pair forensic): vwap_dev is the dominant separator of wins vs losses on T5
- R9c (ticker fence): fenced vwap on T5 only captures 93% of T5-block value
- R10 (asymmetric / fence subsets): symmetric beats asymmetric; full T5 fence beats partial; T5+TSLA or T5+NFLX fences worse
- R10b (micro-sweep): 15-27bps threshold range stable; tighter optimum at 25bps

---

## Caveats

- **Sample sizes**: 7-17 trades per (ticker, side) pair on the full year. Per-pair forensic observations could be noisy. The aggregate fenced result holds across all 4 quarters which is the main robustness check.
- **VWAP computation in live engine** is a new dependency. Production's `scan.py:_5m` ring buffers don't currently track cumulative pv/v — need to add. Risk: VWAP value at admission may differ from the backtest's bar-aligned VWAP by sub-second timing. Slippage budget already absorbs this.
- **Quarterly cross-validation** only goes back to Q2-2025. The fenced filter has not been tested on Q1-2025 or earlier — those archives don't exist on `data-extensions/rth-expand`.
- The "live beats backtest" observation from the operator was based on **one trading day** (2026-05-12, 12 v8.x.x trades, net +$35 after $1,080 peak intraday). Backtest is the better signal until we have 30+ days of v10 live data.

---

## Artifacts

- `tools/orb_backtest.py` — R7 + R9 + R9c + R10 + R11 levers added (branch `claude/r7-min-break-bps-lever`)
- `/tmp/r7_v12/` — initial v12 reproduction (matched R5 within rounding)
- `/tmp/r7_prod/` — production-realistic baseline + ATR sweep
- `/tmp/r9_annotated.json` — 132 T5-ticker trades with rich indicators (gap, vwap_dev, premkt_dir, OR-shape, signal-bar volume)
- `/tmp/r10/`, `/tmp/r10b/` — fenced vwap_dev sweeps + asymmetric thresholds
- This report supersedes `docs/pl_optimization_final_report_v12.md` (v12 numbers were under-baselined for ATR ON).
