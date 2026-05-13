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
- Version: `bot_version.py` (`BOT_VERSION = "7.x.y"`); mirrored in `trade_genius.py`
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

## Retrieving live state from sandbox (added v8.3.24)

The Claude Code sandbox is firewalled from `tradegenius.up.railway.app` (Host-not-in-allowlist). To analyze live trading state without the operator pasting JSON, pull the latest snapshot from the dedicated `snapshots-live` branch via the GitHub MCP:

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
Run `bash scripts/preflight.sh` — mirrors CI checks locally:
- pytest
- version-bump consistency (`BOT_VERSION` matches CHANGELOG heading AND `trade_genius.py`)
- em-dash literal check on .py files
- ruff/black format check

In sandbox where `telegram` is missing, `pytest tests/strategy/` is the focused alternative (231+ tests; the v10 path is fully covered there).

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
