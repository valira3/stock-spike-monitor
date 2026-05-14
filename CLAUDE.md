# stock-spike-monitor — agent guide

## Current strategy: v10 ORB anchor + v9.1.0 EOD reversal addon

Production runs the **v10 ORB anchor** strategy for morning entries (9:30-11:00 ET) and the **v9.1.0 EOD reversal addon** for a single afternoon trade (15:30-15:59 ET). Tiger Sovereign / V570-STRIKE / V560-GATE are retired.

**Morning ORB path** (decision flow):
- **Entry**: `orb/live_runtime.py` → `orb/engine.py` → `orb/state.py` (per-portfolio FSM) → `orb/risk_book.py` (concurrent risk + notional caps) → `orb/day_gates.py` (VIX / earnings / gap / blocklist / SPY-regime) → `orb/exits.py` (RR=2.5 + move-to-BE-after-1R).
- **Per-portfolio fanout**: Main / Val / Gene each run their own RiskBook + FSM. `engine/scan.py:_orb_long_entry`/`_orb_short_entry` iterates portfolios and calls `live_runtime.check_entry(...)` per portfolio.
- **Broker fire**: Main goes through `callbacks.execute_entry` (legacy path). Val/Gene route through `executors/base.py:fire_long`/`fire_short` when `ORB_PORTFOLIO_FIRE=1` (default `1` since v8.3.23).
- **Kill switch**: `ORB_LIVE_MODE=0` falls back to legacy strategy (rollback path).
- **Forensic**: `[V79-ORB-BOOT/RESET/OR-LOCK/GATE/RISK-OK/RISK-NO/ENTRY/REJECT/EXIT/ADMIT/FIRE]` + `[V10-FIRE]` + `[V79-ORB-EQUITY]` + `[V900-MBR-REJECT]` + `[V900-VWAP-CHASE]` + `[V900-SPY-GATE]`.

**EOD reversal addon path** (v9.1.0+):
- **Module**: `orb/eod_reversal.py` (`EodReversalEngine`, independent from `OrbEngine`)
- **Entry**: `engine/scan.py:_eod_reversal_pass` invoked once per scan cycle. At 15:30 ET fires top-1/top-1 long/short selection on `ORB_EOD_UNIVERSE` (default `ORCL,AAPL,MSFT,AVGO,NFLX`). At 15:59 ET flattens.
- **Selection**: per-side fence via `ORB_EOD_LONG_TICKERS` + `ORB_EOD_SHORT_TICKERS`. Drops "retail-momentum" mega-caps (META, GOOG, TSLA, AMZN, NVDA) which fail the reversal pattern per R17 forensic.
- **Sizing**: 35% notional per leg (fixed, not stop-based).
- **Broker fire gate**: `ORB_EOD_FIRE_BROKER=0` (default) keeps it in paper-fire-observation mode — engine tracks positions + P&L for the dashboard but doesn't place real orders. Flip to `1` after 5+ clean paper days.
- **Forensic**: `[V910-EOD-RESET/ENTRY/EXIT/FIRE/CLOSE-FIRE/NO-SIGNAL]`.
- **Backtest backing**: docs/r17_afternoon_backtest_report.md (combined v9 morning + v9.1 EOD = $+29,386/yr / +18.6% over v9 alone / 0/5 neg quarters).

## Where things live
- v10 strategy core: `orb/` package (`live_runtime.py`, `engine.py`, `state.py`, `risk_book.py`, `exits.py`, `day_gates.py`, `live_adapter.py`)
- Per-portfolio books: `engine/portfolio_book.py` (`PORTFOLIOS`, `ALL_PORTFOLIO_IDS`, `PortfolioBook.current_equity`)
- Scan loop: `engine/scan.py` (per-cycle bootstrap + equity refresh + per-portfolio fanout)
- Val/Gene executors: `executors/{base,bootstrap,val,gene}.py`
- Multi-layered verification: `tools/orb_session_sim.py` + `tests/strategy/test_orb_session_sim.py` (15 scenarios); `tools/orb_replay_day.py` + `tests/strategy/test_orb_replay_day.py` (archive replay)
- Universe / tickers: code expects `/data/tickers.json` on persistent volume; default in `config.py` UNIVERSE
- Version: `bot_version.py` (`BOT_VERSION`); mirrored in `trade_genius.py`
- Bar archive writer: `bar_archive.py` (writes to `/data/bars/YYYY-MM-DD/<TICKER>.jsonl`)
- Retirement plan: `docs/v10_retirement_plan.md` (gates legacy code physical-deletion on 5-day paper-fire observation)
- Legacy still on disk (hidden in UI under `body.v10-live` but not yet physically deleted): `tiger_buffalo_v5.py`, `_pmtx*` JS, `.pmtx-*` CSS. Removal scheduled for post-paper-fire PRs.

## Retired (do not reference as live)
- `entry_gate_v5.py`, `bison_v5.py` — deleted pre-v7.24.0
- Shadow configs / SHADOW_CONFIGS evaluator / shadow_positions table / Saturday weekly report cron — retired v5.14.0
- Tiger Sovereign Phase 1–4 weather check + Permit Matrix UI — hidden v7.27.0 via `body.v10-live`; physical deletion pending

## Mandatory PR rules
- Bump `BOT_VERSION` in `bot_version.py` AND mirror in `trade_genius.py`
- Add new heading `## v7.x.y — <date>` at TOP of `CHANGELOG.md`
- Update `ARCHITECTURE.md` if behavior changes
- Update `trade_genius_algo.pdf` ONLY when algo text changes (most PRs do not)
- Git author: `git -c user.email=valira3@gmail.com -c user.name=valira3 commit -F /tmp/commit_msg.txt`
- String literals: use `—` escape, NEVER literal em-dash. CHANGELOG/ARCHITECTURE/README MAY use real em-dash.
- Never use words "scrape/crawl/scraping/crawling" anywhere
- Never hide `#h-tick`, never drop the health-pill count
- Telegram mobile code-block: ≤34 chars per line
- **UI changes propagate across all tabs (added v8.3.18).** When a feature lands on one tab (Main / Val / Gene), it must also land on the other two unless there's a documented reason it doesn't apply. The dashboard has three parallel renderer paths: `renderPositions` / `renderTrades` / `renderV10ActivityFeed` for Main (in IIFE-1, app.js ~lines 320–520, 600–740, 6190–6260) and `renderV10PerPortfolio` / `renderExecTrades` / per-pid panels for Val/Gene (in IIFE-2, app.js ~lines 3990–4200, 4565–4750). Changes to one without the other have already shipped twice as separate PR pairs (v8.3.1 + v8.3.16 for ET conversion; v8.3.8/v8.3.10 + v8.3.18 for position-row columns). Audit every UI PR against both renderer paths before merge. CSS via shared `#pos-body, [data-f="pos-body"]` selectors handles this automatically once the JS structure matches.
- **Section order parity across tabs (added v8.3.21).** The vertical order of cards/sections on Val and Gene tabs must mirror Main's order so the operator's eye-trace is identical across portfolios. Canonical order: (1) killswitch banner, (2) KPI row, (3) Open positions, (4) v10 ORB header/gauges, (5) v10 Proximity, (6) Recent activity, (7) Today's trades, (8) Account diagnostics (Val/Gene only). When adding or moving a card, audit BOTH `index.html` (Main, ~lines 107–301) AND `execSkeleton` in `app.js` (~lines 3870–3987). The Val/Gene `v10 ORB` gauges card is the analog of Main's `v10-day-status` + `v10-baseline` + `v10-ticker-matrix-section` rolled into one — it occupies the equivalent slot.

- **Independent-mode default (added v8.3.23).** `ORB_PORTFOLIO_FIRE` env default is now `"1"`, so each portfolio runs its own `OrbEngine.try_enter` and dispatches entries via `engine/scan.py:_v10_dispatch_executor_fire` → `executor.fire_long`/`fire_short`. The legacy bus listener `executors/base.py:_on_signal` skips `ENTRY_LONG`/`ENTRY_SHORT` when the flag is `"1"` to prevent double-fire. **EXIT signals still flow through `_on_signal`** because `orb.live_runtime.check_exit` is implemented but has no production caller yet — Main's bus-emitted `EXIT_*` is the canonical exit path for all three portfolios. **Limitation**: a position Val/Gene admits that Main rejected (different RiskBook decision) won't get an exit signal from the bus — those Val-only positions close only at EOD flush. Set `ORB_PORTFOLIO_FIRE=0` in Railway env to revert to pre-v8.3.23 mirror mode. A future v8.3.24+ will wire `check_exit` into a per-portfolio sentinel loop to close this gap.

- **Major-version releases (added v9.0.0).** When the operator requests a major release (vN.0.0 from vN-1.x.x) or "build and deploy in a loop", follow the 7-step checklist in `.claude/skills/major-build/SKILL.md`: (1) UI parity across Main+Val+Gene, (2) all levers ON by default, (3) code-quality + algorithm correctness audit, (4) smoke tests work locally + post-deploy, (5) data fully filled with fail-open + auto-rebuild on missing, (6) state persists across restart/redeploy (or naturally re-derivable), (7) iterate on PR until CI + post-deploy-smoke both green. The skill codifies the v9.0.0 template; subsequent major releases extend it as new patterns emerge.

- **Bundled SEV-1 hotfixes (added v9.1.25).** When fixing a SEV-1, AUDIT the surrounding ~30 lines (and any helper functions called from inside the same try/except wrapper) for sibling bugs of the same class BEFORE opening the fix PR. If a second SEV-1 is found in the same code block, bundle it into the SAME PR rather than serializing. On 2026-05-13 the v9.1.20 + v9.1.21 fixes shipped serially as separate PRs even though both bugs lived in the same 12-line function; the second bug was discovered while auditing the first PR. The "serial-merge → wait for Railway deploy → discover next layer" pattern cost ~28 minutes of recovery time and was the difference between landing a working EOD reversal engine inside today's 15:00–15:59 entry window (recoverable) and missing it entirely (the unrecoverable outcome). Rule of thumb: if a try/except wrapper swallowed a crash that took ≥5 min to diagnose, audit ALL paths inside that wrapper, not just the line that crashed. A silent wrapper means there are likely more bugs hiding in the same block. Annualized cost of the not-bundled pattern: ~$200–600/yr if the bot keeps shipping ≥1 EOD-class SEV-1 per quarter.

## GHA-driven backtest via lever-sweep (added v8.3.26)

When the operator asks for a multi-day or full-year backtest of a new
theory, **do not build parallel infrastructure**. The existing path:

1. **Corpus lives on `data-extensions/rth-expand` branch.** Bar files
   at `data/<YYYY-MM-DD>/<TICKER>.jsonl`. Backfill missing dates by
   dropping a JSON trigger under `.github/rth-trigger/` (e.g.
   `fill-2026-05-12.json`) — the `pull-rth-bars.yml` workflow
   auto-fires on push.
2. **Add new theory env vars to `tools/orb_backtest.py`.** Pattern:
   field on `ORBConfig`, default 0/False (off), `_envf`/`_envs` parse
   in `from_env`, behavior in the simulate loop, surface in per-day
   diagnostics. Keep changes minimal and tested in
   `tests/strategy/test_orb_backtest_v18_rules.py` (or new file).
3. **Write a sweep script** under `docs/research/r<N>_<theme>.py`
   mirroring `r2`-`r5`. Theories are a list of
   `(vid, env_overrides)` tuples layered on `BASE` (which encodes the
   v12-winning config). Evaluate each theory on full-year + quarterly
   slices to catch in-sample fits.
4. **Dispatch the sweep via `Lever Sweep`** workflow (Actions tab ->
   "Lever Sweep" -> "Run workflow"). The `variants` input takes the
   JSON tuple from `python3 docs/research/r<N>_<theme>.py --print-variants`.
   Results commit to the `sweep-results` branch under
   `sweeps/run-<id>/<vid>/`.
5. **Retrieve results via MCP**:
   ```
   mcp__github__get_file_contents(
     owner="valira3", repo="stock-spike-monitor",
     path="sweeps/run-<id>/<vid>/summary.json",
     ref="sweep-results"
   )
   ```

**Anti-pattern**: building a new `tools/corpus_backtest.py` or new
`corpus-backtest.yml` workflow. The `lever-sweep.yml` +
`tools/lever_sweep_runner.py` + `tools/orb_backtest.py` chain is the
production research path. Falsified theories live in `r2`-`r5`'s
top-of-file docstrings AND in `docs/pl_optimization_final_report_v12.md`
("Falsified" section). Check there before re-running a dead theory.

**Source-of-truth report**: `docs/pl_optimization_final_report_v<N>.md`
(currently v12). v8.3.26's R6 results will land in v13 after the
sweep runs.

## Keystone — canonical production baseline

**Keystone** is the locked production strategy benchmark as of 2026-05-13.

**Strategy:** v10 ORB morning (9:30-11:00 ET) + r17 EOD reversal addon (15:30-15:59 ET). No blocklist. VWAP-chase gate is the production discriminator for the 6 mega-caps.

**Results (SIP corpus, Jan 2025-May 2026, 341 days, with 30-min post-loss cooldown):**

| Component | Ann/yr | Notes |
|---|---:|---|
| Morning ORB | +$31,449 | VWAP 25bps gate + 30min cooldown |
| EOD reversal (r17) | +$10,036 | ORCL/AAPL/MSFT/AVGO fence, 35% notional, 15:00-15:59 ET |
| **Combined** | **+$41,485** | **+58.8% on $100k / 17mo / 1/6 neg quarters** |

| Quarter | Combined P&L |
|---|---:|
| 2025-Q1 | -$5,183 |
| 2025-Q2 | +$11,430 |
| 2025-Q3 | +$8,479 |
| 2025-Q4 | +$15,208 |
| 2026-Q1 | +$8,316 |
| 2026-Q2 | +$20,521 |

**How to run (morning ORB):**
```bash
ORB_OR_MINUTES=30 ORB_RR=2.5 ORB_RISK_PER_TRADE_PCT=1.0 \
ORB_RANGE_MIN_PCT=0.008 ORB_RANGE_MAX_PCT=0.025 \
ORB_MAX_TRADES_PER_DAY=5 ORB_MAX_CONCURRENT_RISK_DOLLARS=2000 \
ORB_DAILY_LOSS_KILL_PCT=2.0 ORB_ATR_STOP_MULT=1.75 ORB_ATR_LOOKBACK_5M=14 \
ORB_PARTIAL_PROFIT_AT_1R=1 ORB_MOVE_TO_BE_AFTER_1R=1 \
ORB_STOP_BUFFER_BPS=5.0 ORB_ENTRY_SLIPPAGE_BPS=1.5 \
ORB_EXIT_SLIPPAGE_BPS=1.5 ORB_STOP_KICK_BPS=5.0 ORB_SHORT_PENALTY_BPS=1.0 \
ORB_MAX_TRADE_NOTIONAL_PCT=75 ORB_SKIP_GAP_ABOVE_PCT=1.5 \
ORB_SKIP_VIX_ABOVE=22.0 ORB_SKIP_PRIOR_SPY_RET_LT_BPS=-40.0 \
ORB_SKIP_EARNINGS_WINDOW=1 ORB_TIME_CUTOFF_ET=11:00 ORB_EOD_CUTOFF_ET=15:55 \
ORB_ACCOUNT=100000 ORB_COMPOUND_DAILY=1 ORB_TICKER_SIDE_BLOCKLIST='{}' \
ORB_MAX_VWAP_DEV_BPS=25.0 ORB_MAX_VWAP_DEV_TICKERS='META,MSFT,AAPL,AMZN,GOOG,AVGO' \
ORB_POST_LOSS_COOLDOWN_MIN=30 \
python tools/orb_backtest.py --corpus data --out results/keystone_verify \
  --year-prefix 20 \
  --tickers AAPL,AMZN,AVGO,GOOG,META,MSFT,NFLX,NVDA,ORCL,QQQ,SPY,TSLA
```

**How to run (EOD r17):**
```bash
AFT_STRATEGY=eod_reversal AFT_EOD_UNIVERSE=ORCL,AAPL,MSFT,AVGO,NFLX \
AFT_EOD_LONG_TICKERS=ORCL,AAPL,MSFT,AVGO AFT_EOD_SHORT_TICKERS=ORCL,NFLX,AAPL,MSFT \
AFT_EOD_TOP_N=1 AFT_NOTIONAL_PCT=35 AFT_SIZING_MODE=fixed_notional \
AFT_ENTRY_BUCKET=900 AFT_EXIT_BUCKET=959 \
AFT_ENTRY_SLIP_BPS=1.5 AFT_EXIT_SLIP_BPS=1.5 AFT_ACCOUNT=100000 AFT_COMPOUND_DAILY=1 \
python tools/afternoon_backtest.py --strategy eod_reversal \
  --corpus data --out results/keystone/eod --year-prefix 20
# NOTE: AFT_ENTRY_BUCKET=900 (15:00 ET) is required — afternoon_backtest.py
# defaults to 930 (15:30) but production eod_reversal.py has used 15:00 since v9.1.2.
```

**Key techniques & levers:**

| Lever | Value | Why |
|---|---|---|
| `ORB_MAX_VWAP_DEV_BPS=25` on 6 mega-caps | Production gate | Blocks entries where price has already chased >25bps past session VWAP on META/MSFT/AAPL/AMZN/GOOG/AVGO — replaces the old T5 blocklist without hard-blocking tickers |
| `ORB_POST_LOSS_COOLDOWN_MIN=30` | Production parity | Mirrors `POST_LOSS_COOLDOWN_MIN=30` in Railway env; prevents same-(ticker,side) re-entry within 30 min of a stop — eliminates 66% of bad-day double-fires in backtest |
| `ORB_ATR_STOP_MULT=1.75` | Volatility-adaptive stop | ATR(14) × 1.75 instead of OR-edge ± buffer; wider on volatile days, tighter on quiet days |
| `ORB_PARTIAL_PROFIT_AT_1R=1` | Partial exit at 1R | Half-close at 1R, runner rides to 2.5R target with BE stop |
| r17 EOD fence | ORCL/AAPL/MSFT/AVGO long, ORCL/NFLX/AAPL/MSFT short | Per-(ticker,side) fence discovered in r17 forensic; drops retail-momentum stocks that reverse momentum instead of mean-reverting |

**Backtest speed — pkl cache:**
- First run: ~30s (reads JSONL, writes `data/.bt_cache/<TICKER>.pkl`)
- Subsequent runs: ~7s (loads pkl — 7× faster)
- Cache auto-invalidates when any JSONL file is newer than its pkl
- Delete `data/.bt_cache/` to force a full rebuild

**Artifacts:** `results/keystone/keystone.json`, `results/keystone/morning/per_day/`, `results/keystone/eod/per_day/`

**Anti-patterns to avoid when sweeping:**
- Do not confuse with v12 Config A (uses T5 blocklist, no VWAP gate, OR-edge stop)
- Do not add `ORB_REQUIRE_RVOL_ABOVE` — kills +$27k/yr of edge on this corpus
- Q1 2025 is the known weak quarter (pre-live-production, NFLX-driven whack-a-mole)
- Do not use `ORB_MAX_TRADES_PER_DAY=1` — halves P&L by blocking profitable double-fires alongside losing ones

## Local smoke test runner (replaces GHA post-deploy-smoke.yml)

`scripts/run_smoke.py` runs the full smoke suite locally after a push. Reads credentials from `.env.monitor`.

```bash
# Full flow: wait for Railway rollout, then run local + prod tests
python scripts/run_smoke.py

# Skip Railway wait (if version already deployed or testing locally)
python scripts/run_smoke.py --no-wait

# Local tests only (no prod hit, no Railway wait)
python scripts/run_smoke.py --local-only
```

- **Local tests** — imports `trade_genius` in-process with synthetic state. Requires `FMP_API_KEY` and `SSM_SMOKE_TEST=1` (both in `.env.monitor`).
- **Prod tests (9)** — hits `https://tradegenius.up.railway.app` directly: login, `/api/state`, `/stream`, rate limiter.
- **Telegram alert** — fires via `@tgval3_bot` on any failure (same bot as monitor alerts).
- **Railway wait** — polls `/api/version` every 10s for up to 5 min until new BOT_VERSION appears.

The GHA `post-deploy-smoke.yml` cron is disabled; `workflow_dispatch` kept for emergency runs.

## Local monitor loop (replaces GHA monitor.yml)

`scripts/run_monitor.py` runs `tools.unified_monitor` every 5 min during RTH (Mon-Fri 07:00-19:00 ET). Results land in `data/monitor/latest.json` (same schema as the old `monitor-live` branch output).

```bash
# First-time setup: copy and fill in credentials
cp .env.monitor.example .env.monitor   # then edit it

# Run (RTH-gated by default)
python scripts/run_monitor.py

# One-off manual check
python scripts/run_monitor.py --once

# Run 24/7 (bypass RTH gate — for testing)
python scripts/run_monitor.py --always
```

Required vars in `.env.monitor`: `DASHBOARD_BASE_URL`, `DASHBOARD_PASSWORD`, `VAL/GENE_ALPACA_PAPER_KEY/SECRET`, `RAILWAY_API_TOKEN`, `RAILWAY_SERVICE_ID`, `TELEGRAM_TP_TOKEN`, `TELEGRAM_TP_CHAT_ID`. See `.env.monitor.example` for the full template.

The GHA `monitor.yml` cron is disabled (schedule trigger removed); `workflow_dispatch` is kept for emergency ad-hoc runs.

## Retrieving live state (added v8.3.24)

**From the local machine** (Railway CLI + curl both work):
```bash
# Quick version check
curl -sk https://tradegenius.up.railway.app/api/version

# Full state snapshot (positions, RiskBook, day_states, trade_log)
curl -sk https://tradegenius.up.railway.app/api/state | python -m json.tool

# Per-portfolio executor state
curl -sk https://tradegenius.up.railway.app/api/executor/val
curl -sk https://tradegenius.up.railway.app/api/executor/gene

# Railway logs (requires `railway login` in an external terminal first)
railway logs --project stock-spike-monitor
```

**From a GHA/CI sandbox** (firewalled from the Railway host): pull the latest snapshot from the `snapshots-live` branch via GitHub MCP:
```
mcp__github__get_file_contents(
    owner="valira3", repo="stock-spike-monitor",
    path="data/snapshots/latest.json", ref="snapshots-live"
)
```

The cron workflow `.github/workflows/state-snapshot.yml` updates `latest.json` every 10 min during US RTH (Mon-Fri, 13:00-21:00 UTC) by running `python -m tools.state_snapshot` against `/api/state` + `/api/executor/val` + `/api/executor/gene`. Daily JSONL history at `data/snapshots/YYYY-MM-DD.jsonl`.

For an immediate refresh outside the cron window: Actions tab -> state-snapshot -> Run workflow (`workflow_dispatch`).

## Operator preferences
- **Timezone (updated v7.89.0)**: always show times to the operator in US Eastern Time (ET — EDT during DST, EST otherwise). When referencing market hours or schedules, list ET first and only include UTC alongside if necessary for disambiguation. Example: "next cron tick at 09:57 ET (13:57 UTC)". The previous CT preference (v7.72.0) is retired so user-facing times match the market clock the bot keys all decisions off of. Internal code, log timestamps, and forensic tags continue to use UTC/ET as designed; storage-layer ISO timestamps remain UTC.

## Before pushing

**Primary (cross-platform, Windows/macOS/Linux):**
```
python scripts/run_ci.py
```
Runs all local CI checks in sequence with clear pass/fail output:
1. `pytest tests/strategy/` — 231+ fast ORB unit tests (no telegram dep)
2. BOT_VERSION consistency — `bot_version.py` == `trade_genius.py` == CHANGELOG top heading
3. CURRENT_MAIN_NOTE guard — top CHANGELOG entry must match `vX.Y.Z`
4. Em-dash literal check — new .py lines added vs `origin/main` must not carry literal `—` (U+2014); use `—` escape
5. ruff check + ruff format --check (skipped gracefully if ruff not installed)

Optional flags:
- `--smoke` — also runs `python smoke_test.py` (31 local smoke tests; needs env vars)
- `--slow` — includes `pytest.mark.slow` tests (~70s extra)
- `--all` — both of the above

**bash alternative (Linux/macOS/WSL only):**
`bash scripts/preflight.sh` — equivalent bash script; same checks.

**Focused pytest (when telegram module is missing in sandbox):**
`pytest tests/strategy/` — the v10 path is fully covered there (231+ tests).

## Post-deploy smoke

**Default path — already automated.** The `.github/workflows/post-deploy-smoke.yml` workflow auto-fires on every push to `main`. It uses GHA secrets (`DASHBOARD_PASSWORD`, `TELEGRAM_TP_TOKEN`, `TELEGRAM_TP_CHAT_ID`) — no Railway API token. It waits up to 5 min for Railway to roll out the new `BOT_VERSION` (polls `https://tradegenius.up.railway.app/api/version`), then runs **31 local + 9 prod smoke tests** via `python smoke_test.py` and `python smoke_test.py --prod`. On failure it Telegram-alerts the TP chat with the failing test names + the Action URL.

→ **Do NOT propose running `scripts/post_deploy_smoke.sh <version>` from a local sandbox after a merge.** It's a different code path (sources `scripts/lib/checks.sh` for deploy-status / universe / log-tag / bar-archive / `/api/state` checks) that needs `RAILWAY_API_TOKEN` directly. Reserve it for the rare "GHA is broken, need a manual smoke" case. CI's `post-deploy-smoke.yml` is the canonical post-release gate; silence = pass, Telegram ping = fail.

v5.14.0 dropped the shadow_db row-count check along with the rest of the shadow strategy.

## Tests
- `pytest tests/strategy/` — v10 ORB suite (231+ tests, fast)
- `pytest tests/` (full suite — requires `telegram` module installed)
- `pytest tests/test_<module>.py -k <name>` (focused)
- `python -m tools.orb_session_sim --scenario golden_long -v` — end-to-end smoke against live runtime
- New algo PRs: add a unit test under `tests/strategy/`

## Architectural landmines

- **`trade_genius.py` is a 12k-line legacy monolith.** It is excluded from ruff linting (`extend-exclude` in `pyproject.toml`). Do NOT add new features to it — all new algo/strategy code belongs in `orb/`, `engine/`, or `executors/` packages. It's the host process (Telegram bot + scheduler + scan loop bootstrap) but its internals are not the production path for v10 decisions.
- **`COMMANDS.md` is a v3.4.x-era artifact.** It documents the old Telegram command surface accurately for that version but does not reflect the current command set. Do not use it as a reference for what commands exist today.
- **`STRATEGY.md` documents the retired Tiger Sovereign spec (v15.0).** It is not current behavior. The live strategy is fully described in this file and `ARCHITECTURE.md`.
- **`dashboard_server.py` is read-only by design.** No endpoint mutates bot state. Useful API endpoints for diagnostics: `/api/version`, `/api/state`, `/api/executor/val`, `/api/executor/gene`, `/stream` (SSE, 2s push).

## Common gotchas
- Universe drift: `/data/tickers.json` on persistent volume can lag code's `UNIVERSE` list. v5.8.0 startup guard auto-rewrites it.
- Railway redeploy != restart: use `deploymentRedeploy` mutation, NOT `deploymentRestart` (which can hang in 502).
- Forensic logs: every new release should add a `[V7xy-<TAG>]` schema; document it in CHANGELOG.
- Per-portfolio fire is env-gated: `ORB_PORTFOLIO_FIRE=0` keeps Val/Gene mirroring Main via the legacy signal bus; `=1` enables their own admissions to fire.
- v10 live-mode kill switch: `ORB_LIVE_MODE=0` reverts to legacy. Leave on `1` in production.

## PR submission
- `gh pr create --title "v7.x.y: <summary>" --body-file /tmp/pr_body.md`
- `gh pr merge <N> --squash --admin` after CI passes

## Saturday weekly report
RETIRED in v5.14.0. `scripts/saturday_weekly_report.py` and the cron `873854a1` were both deleted with the shadow strategy. The bar archive at `/data/bars/YYYY-MM-DD/<TICKER>.jsonl` and the live-engine forensic logs (`[V79-ORB-ENTRY]`, `[V79-ORB-EXIT]`, `[V10-FIRE]`, `[ENTRY]`, `[TRADE_CLOSED]`, etc.) remain available for any future weekly-report harness. The replacement should consume `trade_log.jsonl` for actual entries.
