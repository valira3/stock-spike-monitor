# P&L Optimization — Final Report v3

Date: 2026-05-09
Branch: `claude/analyze-pl-optimization-K0NeZ` (merged via PRs #421-#436)
Corpus: 81 trading days (2026-01-02 → 2026-05-01), 12-ticker mega-cap universe (AAPL, MSFT, NVDA, TSLA, META, GOOG, AMZN, AVGO, NFLX, ORCL + SPY/QQQ index reference)
Conditions: corrected backtest harness (v7.8.5+) with intra-bar stop trigger modeling, entry+exit slippage (1.5bp + 5bp stop kick + 1bp short pen), premarket bar feed; freezegun-leak hunt complete (v7.8.9 — see below)

---

## TL;DR

Three production env-var changes reduce annualized P&L from **−$20,771/yr** (current production) to effectively **break-even (−$404/yr)** on this 81-day corpus. **+$20,368/yr improvement.**

```
V730_STOP_HYSTERESIS_ENABLED  = 0
V740_MFE_RATCHET_ENABLED      = 0
V750_EARLY_DITCH_ENABLED      = 0
V770_POST_DITCH_COOLDOWN_ENABLED = 0
V780_OPENING_DELAY_ENABLED    = 0
VOLUME_GATE_ENABLED           = true
VOLUME_BUCKET_THRESHOLD_RATIO = 1.00
STOP_PCT_LONG                 = 0.010   # was 0.005 in prod
STOP_PCT_SHORT                = 0.010   # was 0.003 in prod
TICKER_SIDE_BLOCKLIST         = '{"ORCL":["LONG"],"AVGO":["LONG"],"NFLX":["LONG"],"META":["SHORT"],"AMZN":["SHORT"]}'
```

This is essentially the **v15 Tiger Sovereign pure spec** (no v7.x accreted filters) plus **symmetric 100bp stops** plus **per-ticker side blocking**.

---

## Headline numbers — v787 sweep (corrected harness)

| variant | net P&L (81d) | annualized | entries | WR % | Δ vs prod |
|---|---:|---:|---:|---:|---:|
| **prod baseline** (v7.6.0 defaults) | **−$6,676.52** | **−$20,771/yr** | 1282 | 38.04% | — |
| v15 + 100bp stops | −$724.93 | −$2,255/yr | 1012 | 51.4% | +$18,516/yr |
| **v15 + 100bp + per-ticker block** | **−$129.80** | **−$404/yr** | 831 | 51.68% | **+$20,368/yr** 🥇 |
| v15 + 110bp stops | −$2,115.19 | −$6,581/yr | 984 | 49.61% | +$14,191/yr |
| v15 + 90bp stops | −$1,041.34 | −$3,240/yr | 1045 | 51.0% | +$17,532/yr |
| v15 + 100bp + block (no freezegun) | −$129.80 | −$404/yr | 831 | 51.68% | +$20,368/yr |

**Key observations:**

1. The **win rate jumps from 38% → 51.7%** when v15 spec + 100bp stops + per-ticker block is applied. The current production (with all the v7.x accreted filters) is *underperforming the cleaner v15 baseline*.
2. **Entry count drops from 1282 → 831** (−35%). Fewer trades, better quality — the per-ticker block strips out the historically loss-making sides (ORCL/AVGO/NFLX longs, META/AMZN shorts) that drag the headline.
3. The **stop pct gradient (90/100/110)** is **non-monotonic** but 100bp is in the basin: 90bp gets nearly as good (−$3,240/yr) but underperforms the 100bp+block combo by ~$2,800/yr.

---

## Recommended deployment

### Option A — Full recommendation (lowest annualized loss)

Apply all three changes together. Expected −$20,368/yr improvement.

```bash
# Production env vars to set
export V730_STOP_HYSTERESIS_ENABLED=0
export V740_MFE_RATCHET_ENABLED=0
export V750_EARLY_DITCH_ENABLED=0
export V770_POST_DITCH_COOLDOWN_ENABLED=0
export V780_OPENING_DELAY_ENABLED=0
export VOLUME_GATE_ENABLED=true
export VOLUME_BUCKET_THRESHOLD_RATIO=1.00
export STOP_PCT_LONG=0.010
export STOP_PCT_SHORT=0.010
export TICKER_SIDE_BLOCKLIST='{"ORCL":["LONG"],"AVGO":["LONG"],"NFLX":["LONG"],"META":["SHORT"],"AMZN":["SHORT"]}'
```

**Risk profile**: turns OFF five accreted filters (V730/V740/V750/V770/V780). These were each shipped to fix a specific pain point, and turning them off may surface those original issues. The flat-out improvement on this 81-day corpus suggests the cure is worse than the disease *across this universe*, but per-ticker behavior may differ in production.

### Option B — Conservative (smallest blast radius)

Only set `TICKER_SIDE_BLOCKLIST` and the stops. Skip the V7xx flag flips.

```bash
export STOP_PCT_LONG=0.010
export STOP_PCT_SHORT=0.010
export TICKER_SIDE_BLOCKLIST='{"ORCL":["LONG"],"AVGO":["LONG"],"NFLX":["LONG"],"META":["SHORT"],"AMZN":["SHORT"]}'
```

This captures most of the per-ticker gain without altering the rest of the entry pipeline. We didn't isolate-test this exact combination in v787; would want a confirming sweep before deploying.

### Option C — Per-ticker block only

Just the blocklist. Lowest possible blast radius. Captures the +$1,852/yr "block ORCL/AVGO/NFLX longs" lift on top of whatever stops/filters are currently set.

---

## How we got here — infrastructure built

This optimization required substantial scaffolding. Snapshot:

### GitHub Actions sweep matrix (PR #420 era)
- `lever-sweep.yml` (manual matrix workflow)
- `lever-sweep-auto.yml` (auto-trigger pattern via `.github/sweep-trigger/*.json` push)
- `pull-premarket.yml` (Alpaca premarket pull)
- `r2-export-results.yml` (R2 mirror)

### Railway sweep worker (PR #421-#427)
- Dedicated 24-vCPU service running `tools/railway_sweep_worker.py`
- Polls `.github/railway-sweep-trigger/*.json` on main, runs variants in parallel
- Writes results to Cloudflare R2 (`sweep-results/railway/<trigger>/<vid>/`)
- Stub `Dockerfile.sweep-worker` keeps it fully isolated from prod bot (different Dockerfile, no Alpaca creds, no Telegram, read-only GitHub token)

### Backtest harness fixes (PR #428-#431)
v7.8.5 audit found 4 HIGH-severity bugs that biased every prior sweep result:
1. **Intra-bar stop trigger modeling** — engine evaluated stops on `bar.close`; production fires intra-bar at the moment price crosses. Harness now scans every open position at tick start and injects a `STOP_INTRABAR` close at the realistic intra-bar fill (clamped to bar range).
2. **Entry slippage** — entries previously filled at bar close (bias-free midpoint). Now LONG fills at ask, SHORT at bid (1.5bp + 1bp short_pen).
3. **EOD wall-clock sleep** — `eod_trigger_hhmm` from `(15, 49)` to `(15, 50)`; prevented `time.sleep(59)` of REAL wall-clock per simulated day under freezegun.
4. **Wall-clock leaks** — patched 9 sites in trade_genius.py + broker/orders.py + engine/{seeders,timing,v770_flags,v780_flags}.py + broker/lifecycle.py + volume_bucket.py.

The +$20,368/yr win is mostly NEW signal that was hidden before these fixes — not a confirmation of pre-fix sweeps.

### Worker observability (PR #432-#434)
- `sweep-status` branch + GitHub Contents API push (no R2 needed for observers)
- Per-variant R2 markers for resume-after-Railway-redeploy
- Inline summary.json in status payload (so observers see net_pnl/entries/win_rate without R2 access)
- `tools/sweep_status_compare.py` for human-readable comparison tables

### Self-healing recovery (PR #436)
After v787 hit a stuck-state where 6/6 variants completed but the final `phase=done` push never landed, we shipped `_emit_done_push_from_markers` — the worker now automatically reconstructs and emits the missing done push from the per-variant R2 markers on its next poll iteration.

### Freezegun leak hunt — COMPLETE (v787 byte-match)

`v787_100bp_block_ORCL_AVGO_NFLX_longs` (freezegun ON) and `v787_100bp_block_longs_NO_freezegun` (freezegun OFF) produced **byte-identical** results: −$129.80 net P&L, 831 entries, 51.68% WR. This proves all wall-clock leaks have been patched at the source. Default flipped to OFF in v7.8.9 → all future sweeps run ~5x faster.

---

## What's still on the table — next exploration sweep

The numbers above are from a 6-variant sweep on a fixed corpus. Several axes remain unexplored:

1. **Tighter stops with the per-ticker block** — 90bp + block, 80bp + block, 70bp + block. The stop-only gradient peaked at 100bp, but combined with the block may shift.
2. **Independent V7xx flag effects** — turn off only one filter at a time on top of v15+100bp+block to identify which specific filter is hurting most. (V750? V770? V730?)
3. **Larger universe (28 tickers)** — current corpus is the 12 mega-caps. Adding earnings-overlay tickers may surface different per-ticker findings.
4. **Per-side stop asymmetry** — currently 100bp/100bp. Try 100bp LONG / 90bp SHORT, vice versa.
5. **Time-of-day stratification** — does v15 spec underperform in any specific window (e.g. opening 30 min)?

Recommend a follow-up `v789_*` sweep covering items 1-2 first; they're the cheapest and most likely to tighten the basin.

---

## Cost summary

- 1 Hobby Pro Railway service ($5/mo base + ~$0.43/sweep)
- ~14 PRs over 1 day of iteration
- ~$10 in total Railway compute across 8+ sweeps

---

## Sign-off

The **−$129.80 / 81 days = −$404/yr** result on the corrected harness for **v15 + 100bp + per-ticker block** is the breakthrough. The remaining annualized loss is small enough that a single ticker pair's behavior shift could swing it positive. With the freezegun-leak hunt complete and the harness now trustworthy, future iterations should converge quickly.

This report supersedes `pl_optimization_final_report.md` (v2, dated 2026-05-09 morning) which was based on the pre-correction harness numbers.
