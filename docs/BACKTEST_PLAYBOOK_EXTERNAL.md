# TradeGenius backtest playbook — for external platforms

Run a multi-day or full-year backtest of a new strategy theory **without
touching the live bot or running anything locally**. Everything happens in
GitHub Actions; results land on a dedicated `sweep-results` branch and can
be read back through the GitHub web UI or API.

This doc is the "from another platform" recipe — written so an operator
working from **Perplexity Comet**, a generic web browser, ChatGPT with
browsing, the GitHub mobile app, or any HTTP-capable agent can drive the
full cycle. The companion `.claude/skills/gha-backtest-lever-sweep/SKILL.md`
covers the same flow for Claude Code running inside this repository.

---

## Architecture in one screen

```
┌────────────────────────────────────────────────────────────────────┐
│  YOU (Perplexity Comet / browser / curl)                           │
│                                                                    │
│   1. Edit a JSON file in this repo:                                │
│        .github/sweep-trigger/<name>.json                           │
│                                                                    │
│   2. Commit + push to main.                                        │
│                                                                    │
│      OR — without committing, paste the variants into              │
│      `lever-sweep` workflow_dispatch UI.                           │
└──────────────────────┬─────────────────────────────────────────────┘
                       │
                       ▼
┌────────────────────────────────────────────────────────────────────┐
│  GitHub Actions runners (one job per variant, matrix-parallel)     │
│                                                                    │
│   • Checks out main                                                │
│   • Pulls the bar corpus from `data-extensions/rth-expand`         │
│     branch into the runner's filesystem (full year, ~10 tickers)   │
│   • Executes `python -m tools.orb_backtest` once per variant       │
│     with the env-var overrides specified in the JSON               │
│   • Commits `summary.json` + per-day diagnostics to                │
│     `sweep-results` branch under                                   │
│     `sweeps/run-<id>/<vid>/`                                       │
└──────────────────────┬─────────────────────────────────────────────┘
                       │
                       ▼
┌────────────────────────────────────────────────────────────────────┐
│  YOU read results — three equivalent options:                      │
│                                                                    │
│   A. GitHub web UI: browse `sweep-results` branch                  │
│      → `sweeps/run-<id>/<vid>/summary.json`                        │
│                                                                    │
│   B. GitHub API:                                                   │
│      GET /repos/valira3/stock-spike-monitor/contents/              │
│          sweeps/run-<id>/<vid>/summary.json?ref=sweep-results      │
│                                                                    │
│   C. git clone --branch sweep-results, locally inspect             │
└────────────────────────────────────────────────────────────────────┘
```

---

## Setup (one-time)

**Required access:**
* A GitHub account with **write access** to `valira3/stock-spike-monitor`.
  Write access is needed for:
    * pushing to `main` (to drop the trigger JSON), OR
    * dispatching `lever-sweep.yml` via workflow_dispatch.
* That's it. No Railway access, no Alpaca keys, no Polygon. The corpus
  and the compute are entirely inside GitHub.

**Required reading:**
* `.claude/rules/strategy.md` — the 12 research rules. **R3** (quarterly
  CV) and **R4** (plateau-test the winner) are mandatory for any sweep
  before declaring a winner.
* `docs/pl_optimization_final_report_v<N>.md` (currently `v13`) — the
  "what's already been falsified" list. Before proposing a theory,
  search this report and any `docs/research/r<N>_*.py` files for the
  hypothesis under any name. Avoid re-running falsified hypotheses.

---

## Two execution paths

### Path A — Workflow dispatch via GitHub web UI (simplest)

Best for **one-off sweeps with a small variant set** (1-5 variants). Zero
file commits required.

1. Open `https://github.com/valira3/stock-spike-monitor/actions/workflows/lever-sweep.yml`
2. Click **`Run workflow`** (top-right dropdown).
3. Fill the `variants` input with a JSON array:
   ```json
   [
     {"vid": "baseline", "env": {}, "stride": "4"},
     {"vid": "r18_tsla_in_fence", "env": {"ORB_MAX_VWAP_DEV_TICKERS": "META,MSFT,AAPL,AMZN,GOOG,AVGO,TSLA"}, "stride": "4"}
   ]
   ```
   Each variant:
    * `vid` — unique string identifier (becomes the result subdirectory)
    * `env` — dict of env-var overrides applied to `tools/orb_backtest.py`'s `ORBConfig.from_env`
    * `stride` — day-stride for the corpus walk. `1` = every trading day (full
      year, ~251 days). `4` = every 4th day (~63 days, faster). `8` =
      smoke-test. Always use `1` for the final validation run.
4. Optionally set `max_parallel` (default 6).
5. Click green **`Run workflow`**. Browse to the workflow run page to
   watch progress.

The workflow's matrix fans out one job per variant. Typical full-year
single-variant run is 5-15 minutes; a 6-variant batch wall-clock is the
same (parallel).

### Path B — Auto-trigger from a committed trigger file

Best for **iterating on a hypothesis** with many sweep rounds. Drops the
variants into a versioned file so the JSON itself becomes part of the
research history.

1. Create or edit a file at `.github/sweep-trigger/<descriptive-name>.json`
   on `main`. Schema:
   ```json
   {
     "max_parallel": 6,
     "variants": [
       {"vid": "...", "env": {"...": "..."}, "stride": "4"},
       ...
     ]
   }
   ```
   The flat-array form (no wrapper dict) also works:
   ```json
   [
     {"vid": "...", "env": {"...": "..."}, "stride": "4"}
   ]
   ```
2. Commit + push to `main`. The `lever-sweep-auto.yml` workflow auto-fires
   on every push that touches `.github/sweep-trigger/**.json` (per
   `lever-sweep-auto.yml:on.push.paths`).
3. Browse to the workflow run page to watch.

Examples of existing trigger files: `.github/sweep-trigger/batch_a_r2.json`,
`stop_pct_grid_2d.json`, `radical_explorations.json` — read these for
schema reference.

---

## Anatomy of a sweep variant

The `env` dict is the ENTIRE configuration surface. Whatever env-var
`tools/orb_backtest.py:ORBConfig.from_env` accepts can be overridden here.
The strategy levers most commonly tuned:

| Env var | Default | Notes |
|---|---|---|
| `ORB_TIME_CUTOFF_ET` | `11:00` | Entry-window upper bound (R12 winner) |
| `ORB_MIN_BREAK_BPS` | `5` | Min breakout magnitude (R7) |
| `ORB_MAX_VWAP_DEV_BPS` | `25` | Chase-prevention threshold (R10) |
| `ORB_MAX_VWAP_DEV_TICKERS` | `META,MSFT,AAPL,AMZN,GOOG,AVGO` | Chase fence (R10b) |
| `ORB_SKIP_PRIOR_SPY_RET_LT_BPS` | `-40` | Day-skip on prior SPY drawdown (R12) |
| `ORB_RR` | `2.5` | Take-profit at N * R |
| `ORB_RISK_PER_TRADE_PCT` | `1.0` | Risk dollars sized to N% of equity (Phase 14) |
| `ORB_DAILY_LOSS_KILL_PCT` | `2.0` | Daily-loss circuit breaker |
| `ORB_ATR_STOP_MULT` | `1.75` | ATR-based stop placement (v8.0.0) |
| `ORB_EOD_REVERSAL_ENABLED` | `1` | v9.1 EOD reversal addon |
| `ORB_EOD_LONG_TICKERS` | `ORCL,AAPL,MSFT,AVGO` | EOD per-side fence (long) |
| `ORB_EOD_SHORT_TICKERS` | `ORCL,NFLX,AAPL,MSFT` | EOD per-side fence (short) |

Every variant must include a `baseline` for comparison. Without it the
sweep results have no reference frame and you can't tell if the lever moved
P&L vs noise.

---

## Reading results

After the workflow completes (~5-15 min), every variant emits one
`summary.json` plus per-day diagnostics. The summary shape:

```json
{
  "vid": "r18_tsla_in_fence",
  "env": {"ORB_MAX_VWAP_DEV_TICKERS": "...,TSLA"},
  "n_days": 251,
  "fy_net_pnl": 28341.50,
  "wr_pct": 60.2,
  "cagr_pct": 24.1,
  "sharpe": 2.73,
  "max_dd_pct": 3.51,
  "neg_quarters": 1,
  "by_quarter": {
    "2025Q2": {"net": -1832.10, "wr": 55.3, "n": 63},
    "2025Q3": {"net":  8240.51, "wr": 62.8, "n": 63},
    ...
  }
}
```

The key fields for R3 (quarterly CV):
* `fy_net_pnl` — full-year net P&L. Higher is better.
* `wr_pct` — win rate. Reference 61.8% (v13 winner).
* `neg_quarters` — count of quarters with negative P&L. **R3 mandates
  0/4 or 0/5.** A variant with `neg_quarters >= 1` fails the robustness
  test and is rejected even if its `fy_net_pnl` looks great.
* `by_quarter` — per-quarter breakdown for spot-checking.

**Path to read** (substitute `<run-id>` and `<vid>` from the workflow run):

* GitHub web: `https://github.com/valira3/stock-spike-monitor/blob/sweep-results/sweeps/run-<id>/<vid>/summary.json`
* GitHub API:
  ```
  GET https://api.github.com/repos/valira3/stock-spike-monitor/contents/sweeps/run-<id>/<vid>/summary.json?ref=sweep-results
  Accept: application/vnd.github.raw
  ```
* git clone:
  ```bash
  git clone --branch sweep-results --depth 1 \
    https://github.com/valira3/stock-spike-monitor sweep-results
  cat sweep-results/sweeps/run-<id>/<vid>/summary.json
  ```

---

## Notes for Perplexity Comet (and similar browser agents)

Comet runs in a real browser session with full internet egress. Drive it
like a human would:

1. **Sign in to GitHub** (the agent prompts for credentials or uses a
   stored session). Confirm the user has write access to
   `valira3/stock-spike-monitor` — without it, dispatch + push both fail.
2. **For Path A** — navigate to the lever-sweep workflow page
   (`/actions/workflows/lever-sweep.yml`), click `Run workflow`,
   paste the variants JSON, click the green button. Comet can read the
   resulting run URL and poll it for completion.
3. **For Path B** — open the GitHub file editor for a
   `.github/sweep-trigger/<name>.json` path, paste the JSON, commit
   directly to `main` via the web UI's "Commit changes" dialog. The
   `lever-sweep-auto.yml` workflow fires within ~10 seconds of the push.
4. **To read results** — navigate to the `sweep-results` branch and open
   `summary.json` for each variant. Comet's "extract values from JSON"
   primitive works directly.

**Comet-specific tips:**
* GitHub's web UI sometimes lags after a workflow_dispatch — refresh the
  Actions page after 30s if the new run doesn't appear.
* Workflow runs that fail to dispatch (e.g. missing permission) show the
  error inline; Comet should report the inline error rather than retrying
  silently.
* For large sweeps (10+ variants), the matrix-results page is more useful
  than the per-job logs. Each matrix cell links to its own log + the
  committed `summary.json`.

---

## Anti-patterns

* **Don't build parallel infrastructure.** `tools/lever_sweep_runner.py` +
  `lever-sweep.yml` + `tools/orb_backtest.py` is the production research
  path. New `tools/corpus_backtest.py` or `corpus-backtest.yml` reinvents
  the wheel. See `.claude/skills/gha-backtest-lever-sweep/SKILL.md` for
  the rationale.
* **Don't re-run falsified hypotheses.** Check
  `docs/pl_optimization_final_report_v<N>.md`'s "Falsified" section and
  the top-of-file docstrings in `docs/research/r<N>_*.py` before
  proposing a sweep. Time on a known-dead theory is wasted.
* **Don't ship a variant that fails R3.** Even a +$10k/yr lift with
  `neg_quarters >= 1` is rejected. The R3 rule exists because in-sample
  optima with one bad quarter never survive out-of-sample.

---

## Where else to look

* `.claude/skills/gha-backtest-lever-sweep/SKILL.md` — the same approach,
  formatted for Claude Code sessions inside the repo.
* `.claude/rules/strategy.md` — the 12 research rules. R3, R4, R5 are
  the ones most commonly violated by sweep proposals.
* `docs/pl_optimization_final_report_v13.md` — current source-of-truth
  research report. Future rounds extend it as `v14`, `v15`, etc.
* `docs/research/r17_afternoon_backtest_report.md` — example of a
  completed research round (the v9.1 EOD reversal hypothesis).
* `docs/BACKLOG.md` — open research items including the
  TSLA + NVDA chase-fence expansion surfaced 2026-05-13.
