# Backlog — research + engineering followups

Loosely-prioritized list of items surfaced during live-trading observation that
deserve a dedicated PR / research round. Add the date you found each item so we
can age the list out periodically.

When you take something off the backlog, move it into `docs/research/r<N>_…`
(if it's a research sweep) or into a feature PR's CHANGELOG entry (if it's an
engineering task). Don't leave silently completed items here.

---

## Research

### Broad-market premarket breakout scanner → dynamic daily watchlist (2026-05-19)

**Original ask.** "What if we look at the very broad market for potential breakout signals during premarket and add those stocks to our breakout watch during the day (temporarily, just for that day). Can you build a backtest for that?"

**Status to date.** Operator + prior session explored the direction in R18-R21 (dynamic-universe research). Expansion to **S&P 500 was the most promising** breadth — broader than that introduced too much noise, narrower than that left edge on the table. The premarket scanner + R18-R21 sweep scripts + ~32 GB of premarket bar corpus under `data_pm_universe/` are local on **ValsSpectre** (prior dev machine), NOT in this repo.

The headline from §9 of the local `research_guide.md` (paraphrased): the single highest-ROI next test is **concurrent-cap relief** — at relaxed entry filters, the R18 NR-N premarket signal showed crowd-out of ~$25k/yr against the fixed `ORB_MAX_CONCURRENT_RISK_DOLLARS=$2000` cap. Raising the cap to $3k-$4k may flip the OOS R18 nr5_top10 variant (-$1.9k/yr) into positive territory **without any new signal infrastructure**.

**Production morning ORB context.** Currently runs a fixed 12-ticker universe (`AAPL, AMZN, AVGO, GOOG, META, MSFT, NFLX, NVDA, ORCL, QQQ, SPY, TSLA`). Every day starts on the same 12 names regardless of which had premarket setups. The dynamic-universe layer would sit upstream of the existing engine — R21/R26 exits, VWAP-chase gate, etc. still apply.

**Two parallel test paths.**

1. **Concurrent-cap relief sweep** (cheap, fast) — 4 cells: `ORB_MAX_CONCURRENT_RISK_DOLLARS ∈ {$2000, $2500, $3000, $4000}`. Runs against existing 12-ticker corpus; no new data needed. Tests whether the cap is currently the binding constraint on edge.
2. **S&P 500 premarket scanner** (expensive, real direction) — requires retrieving the ValsSpectre artifacts OR rebuilding: (a) pull S&P 500 premarket bar corpus via Polygon REST (the consolidated `pull-bars.yml` workflow — see Engineering Phase 4), (b) port `orb/premarket_scanner.py` + tests, (c) wire a `ORB_DYNAMIC_UNIVERSE` env that, when ON, replaces the static `UNIVERSE` with the top-K scanner output, (d) A/B in the `combined_replay` harness.

**Action.** Start with test path (1) — it's a 4-cell, ~2-minute sweep on the warm cache and directly tests the §9 thesis. If positive, the result strengthens the case for (2). If negative, the cap isn't the binding constraint and the broader scanner is the only remaining lever.

**Owner.** Next research round. Path (2) blocked on artifact retrieval from ValsSpectre OR rebuild.

---

### Chase-fence expansion to TSLA + NVDA (2026-05-13)

**Source.** Live observation 2026-05-13: TSLA + NVDA dominated today's trading
(5 TSLA + 1 NVDA out of 6 round-trip closes). Today's net was $+203 but a
reconstruction with v9.1.7+v9.1.8 cutoff active all day would have been
$+552 — the after-11 entries the cutoff finally blocks were collectively
$-348 net. Within the entries that DID run, the −$647 TSLA close at 11:58
gave back most of the day. That entry was admitted right at the top of a
TSLA chase that no filter caught.

The v9.0.0 R10 chase-prevention fence is currently
`ORB_MAX_VWAP_DEV_TICKERS=META,MSFT,AAPL,AMZN,GOOG,AVGO` — the mega-cap set.
R10 deliberately left high-vol names (TSLA, NVDA) OUT because their wider
intraday volatility flagged them as "different beasts". Today's tape
suggests that exclusion is leaving real money on the table.

**Hypothesis:** adding TSLA + NVDA to the fence with the same 25-bps deviation
threshold catches today's biggest losers without sacrificing the wins. Sample
size is one day so the actual sweep needs to validate this across the full
year.

**Action.** Run a lever sweep with `ORB_MAX_VWAP_DEV_TICKERS` variants:

* `META,MSFT,AAPL,AMZN,GOOG,AVGO` (current production / R10 winner)
* `META,MSFT,AAPL,AMZN,GOOG,AVGO,TSLA`
* `META,MSFT,AAPL,AMZN,GOOG,AVGO,NVDA`
* `META,MSFT,AAPL,AMZN,GOOG,AVGO,TSLA,NVDA`

Optionally split the threshold per ticker (TSLA may need 35-bps tolerance
given wider intraday vol), but start with the same 25-bps to keep the sweep
simple. The dispatch pattern is the standard GHA lever-sweep — see
`.claude/skills/gha-backtest-lever-sweep/SKILL.md`.

**Owner.** Next research round.

---

### Trailing stop on runner after 1R (2026-05-15)

**Source.** Operator question during live session: on a genuinely explosive breakout, the current exit logic leaves significant P&L on the table because the runner's stop is hard-pinned at break-even (entry price) from 1R onward with a fixed 2.5R target.

**Current behavior.** After partial profit at 1R:
- Stop moves to entry (break-even) and stays there permanently
- Runner exits at 2.5R target or at entry (be_stop) — no further trailing

**Hypothesis.** On days with strong directional momentum, a chandelier-style trail on the runner from 1R onward (e.g. `entry + N * ATR` trailing, or a fixed % trail from the high-water mark) could capture more on explosive moves without meaningfully hurting the median case. The risk: tighter trails get shaken out on the normal "move, pause, continue" ORB pattern and convert winning runners into break-even exits.

**Questions to answer in backtest:**
1. Does any trailing variant (ATR-trail, % from HWM, step-up at 1.5R/2R) improve annual P&L vs the fixed 2.5R target?
2. What is the win-rate cost — does trailing increase or decrease the runner close rate?
3. Is there an asymmetry by ticker (NVDA/TSLA explosive movers benefit more than MSFT/AAPL)?
4. Does the improvement survive the 30-min post-loss cooldown and VWAP gate interactions?

**Action.** Add `ORB_TRAIL_AFTER_1R` env lever to `tools/orb_backtest.py` (default off). Variants to sweep:
- `off` (current production, baseline)
- `hwm_atr_1x` — trail at HWM minus 1x ATR(14)
- `hwm_atr_1_5x` — trail at HWM minus 1.5x ATR(14)
- `step_2r` — hard stop steps up to +1R profit at 2R (no full trail, just a step)
- `step_1_5r_and_2r` — stop steps to +0.5R at 1.5R, then to +1R at 2R

Run full-year sweep locally via `python tools/orb_backtest.py` with env var overrides. Compare to keystone baseline.

**Owner.** Next research round.

**Falsification criteria.** A sweep variant must improve FY net P&L AND keep
the 0/4 negative-quarter property of the v13 winner. If TSLA/NVDA inclusion
drops a quarter into the red, mark this hypothesis as falsified and document
in `docs/pl_optimization_final_report_v<N>.md`.

---

## Engineering

### Phase 3: merge `lever-sweep.yml` + `lever-sweep-auto.yml` (2026-05-13)

**Source.** Workflow audit during v9.1.18 (unified monitor) found these two workflows are tightly coupled: `lever-sweep-auto.yml` resolves variants from a pushed JSON file in `.github/sweep-trigger/` and then dispatches the same matrix that `lever-sweep.yml` runs from `workflow_dispatch`. Two workflows, one job.

**Action.** Merge into a single `lever-sweep.yml` with two trigger paths:

```yaml
on:
  workflow_dispatch:
    inputs:
      variants:    # JSON string
      max_parallel:
  push:
    branches: [main]
    paths: [".github/sweep-trigger/**.json"]
```

The job's first step reads variants from either the input (workflow_dispatch) or the newly-pushed file (push). Saves one workflow file and one cron-contention slot. Estimated ~45 min including testing.

### Phase 4: consolidate `pull-rth-bars` + `pull-premarket` + `pull-tick-data` (2026-05-13)

**Source.** Same audit. Three workflows that all pull historical bar data via different sources (Polygon REST for RTH, Polygon REST for pre-market, Alpaca SIP for ticks) into different branches. Identical scaffolding (checkout + setup-python + install + run + commit-to-branch).

**Action.** Single `pull-bars.yml` parameterized on `bar_type` (`rth` | `premarket` | `tick`) + `tickers` + `date_range`. Each pull-type calls its own underlying tool. Identical commit path. Estimated ~1h including testing.

### Chart canvas survives state-poll re-renders (2026-05-13)

**Source.** Operator reported mid-drag interactivity on charts gets killed
every ~5s during a state poll. v9.1.13 tried to fix this via a panel-DOM
cache + `appendChild` transplant; that broke ALL chart interactivity (likely
pointer-capture loss + layout race during transplant) and was reverted in
v9.1.14. The fundamental issue remains: parent tables call
`body.innerHTML = html` on every state poll, which destroys any chart
canvas inside them.

**Two paths to consider** (both bigger than today's iterative fixes):

1. **Diff-render the proximity + positions tables.** Replace
   `body.innerHTML = html` with a node-level diff (`morphdom` or hand-rolled
   per-cell update) so the existing canvas DOM is preserved when only mark
   prices or pnl values change.
2. **Move the chart canvas outside the table tree** entirely. Render the
   table as it is today; render expanded charts in a sibling overlay panel
   keyed by ticker. Table re-renders don't touch the chart container.

(2) is probably cleaner — keeps the table re-render logic simple.

**Owner.** TBD — needs a session where the operator can repro v9.1.13's
"nothing interactive" failure in a browser dev-tools session so we
understand exactly why `appendChild` killed it, before swinging at the
bigger refactor.

### Risk-book `realized_pnl_today` doesn't stay in sync with broker fills (2026-05-13)

**Source.** Live snapshot at 2026-05-13T17:08:15Z showed
`v10.risk_books.{main,val,gene}.realized_pnl_today = $0.00` for all three
portfolios while `trades_today` carried $+203 of round-trip closes. Either
v9.1.8's `dump_state_to_disk` cycle didn't fire between the closes and the
snapshot, or one of today's redeploys reset the engine state and the
rehydrate path didn't repopulate per-pid P&L.

**Action.** Add a forensic log at every `RiskBook.record_realized_pnl` call
plus at every `persist_engine_state()` call, then watch a session-day to
confirm the two streams stay synchronized. If they diverge, audit the
fill→engine path on the Val/Gene executor side specifically — Main's
realized P&L feeds through the legacy bus, but Val/Gene go through
`executors/base.py:_fire_v10_market_order` which records the position
locally but might not loop back into `RiskBook.record_realized_pnl`.

### `spy_d1_ret_bps` still `None` post-v9.1.3 (2026-05-13)

**Source.** v9.1.3 added an Alpaca REST fallback for the prior-SPY-return
loader, but live state at 17:26Z still shows `spy_d1_ret_bps: None`. The
Alpaca credential pool resolves at session-start; either the credentials
weren't reachable when the v9.1.3 deploy did its first session-start, or
the fallback raised an exception that got swallowed.

**Action.** Add a `logger.info("[V913-SPY-LOADER] alpaca path entered with
credentials, fetched=…")` line at every entry point in
`tools/orb_spy_loader.py:_alpaca_prior_close` so the next Railway-log scan
tells us exactly which fallback branch ran. Won't surface until the next
session reset (typically the overnight redeploy or 9:30 ET market open).
