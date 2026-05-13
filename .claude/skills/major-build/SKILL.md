---
name: major-build
description: 7-step checklist for any major-version release of TradeGenius (vN.0.0 from vN-1.x.x). Use when the operator requests a "major release" or "build + deploy in a loop". Codifies the v9.0.0 build template — UI parity, levers ON, code audit, full tests, persisted state, auto-rebuild on missing data, iterate until perfection.
---

# Major-build template (v9.0.0 + later)

Use this when the operator asks for a major-version release, a "build and deploy in a loop", or "ship the validated config". Skip for incremental v8.3.x-style patches.

## Mandatory 7-step checklist

Each step has a verifier listed; do not advance until the previous step's verifier passes.

### 1. UI parity across all three portfolios

New strategy state MUST surface on **Main + Val + Gene** tabs. The dashboard renders these tabs through two different code paths:
- **Main**: `dashboard_static/index.html` + `renderV10DayStatus(s, pidFilter)` in `app.js` IIFE-1
- **Val/Gene**: `execSkeleton()` HTML template + `renderV10PerPortfolio()` in IIFE-2

Both paths must show the new state. Common pattern:
- HTML pill nodes in `index.html` for Main (rendered by `renderV10DayStatus`)
- Inline-injected chips inside `renderV10PerPortfolio`'s `v10-pid-body` HTML for Val/Gene

Section order parity (CLAUDE.md `body.v10-live` rule): `(1) killswitch banner, (2) KPI row, (3) Open positions, (4) v10 ORB header/gauges, (5) v10 Proximity, (6) Recent activity, (7) Today's trades, (8) Account diagnostics (Val/Gene only)`.

**Verifier**: search both `renderV10DayStatus` and `renderV10PerPortfolio` for the new state field name. Both must reference it.

### 2. Turn ALL levers ON by default

A major release ships the **validated production config** as the default. No shadow mode. Every new env lever's `_build_config_from_env` default in `orb/live_runtime.py` matches the recommended production value, not 0/off.

For levers where the value is risky in some environments (e.g., a per-ticker fence string), default to the v13-report-validated value, and document the rollback path in CHANGELOG ("set to 0 to disable").

**Verifier**: `grep _f("ORB_<NEW_LEVER>",` in `orb/live_runtime.py` — the second arg is the default; it should match the v13 report's recommended value.

### 3. Code-quality + algorithm-correctness audit

Per CLAUDE.md, the bar is high. Audit checklist:
- No literal em-dashes in `.py` files (CHANGELOG/ARCHITECTURE/README may use real em-dash)
- No `scrape/crawl/scraping/crawling` words anywhere
- `BOT_VERSION` matches in `bot_version.py` AND `trade_genius.py` AND top CHANGELOG heading
- New filters are positioned AFTER the FSM `can_enter` check (so they don't burn trade-counter capacity on filtered entries)
- Session-scoped counters reset in `start_new_session()` and are exposed via `snapshot()`
- Algorithm correctness verified by tests:
  - mbr filter rejects in original direction (not flipped direction if fade-mode flips later)
  - vwap chase computed at signal-bar's last 1m bucket (matches backtest's `session_vwap_at(sig.bucket + 4)`)
  - Per-ticker fence: empty tuple = filter applies globally
  - All thresholds: 0 = filter off (not "auto-default")

**Verifier**: `python3 -m pytest tests/strategy/ -q` passes 100% (no skipped tests other than the 8 pre-existing `s` ones).

### 4. Smoke tests work

Local + prod smoke paths must continue to function:
- `python smoke_test.py` (local) — should pass
- `python smoke_test.py --prod` (prod) — fires automatically after merge via `.github/workflows/post-deploy-smoke.yml`

The post-deploy-smoke workflow polls `https://tradegenius.up.railway.app/api/version` for the new BOT_VERSION before running smoke checks. If the new release adds API endpoints, add smoke tests for them.

**Verifier**: `bash scripts/preflight.sh` exit 0.

### 5. Data fully present + auto-rebuild on missing

Any new external data dependency (e.g., SPY daily closes for v9.0.0) MUST:
1. Have a primary source (production bar archive `/data/bars/<DATE>/<TICKER>.jsonl` for SPY)
2. Have a fallback source (CSV at `data/external/<feed>.csv` for backtest parity)
3. Have fail-open behavior in the gate (DayGateConfig field `fail_closed_on_missing_X`, default False) so a data outage doesn't strand the system
4. Have a re-fetch path (either an auto-load on session start OR a GHA cron at `.github/workflows/refresh-data-feeds.yml`)

For SPY in v9.0.0: the bar archive is rebuilt every session by `bar_archive.py` (no separate cron needed). The CSV is a manual backup.

**Verifier**: simulate a missing-data scenario in tests (the v9.0.0 `test_orb_v900_spy_regime.py::test_fail_open_on_missing_data` is the template).

### 6. State persists across restart / redeploy

A Railway redeploy mid-RTH wipes in-memory state. New session-scoped state must either:
- Be naturally re-derivable on restart (e.g., session VWAP recomputes from the bar series fed via `callbacks.fetch_1min_bars` — no persistence needed)
- Be persisted to `/data/<state>.json` via the existing engine-state-persistence hook (see `live_runtime._try_rehydrate_engine_state` and the v8.3.4 pattern)

For v9.0.0: rejection counters (`mbr_reject_count`, `vwap_chase_reject_count`) are session-scoped diagnostic data; reset on `start_new_session` is acceptable behavior — a mid-day restart will under-count these. SPY return loaded fresh from bar archive on every `ensure_session_started`.

**Verifier**: ask "what happens if Railway redeploys at 11:00 ET?" for each new feature. The answer must be safe.

### 7. Iterate until perfection

After the initial PR opens:
- `mcp__github__subscribe_pr_activity` to monitor CI + post-deploy-smoke
- If a CI check fails: investigate root cause, fix, push to the same branch
- If post-deploy-smoke fails after merge: revert immediately (Railway redeploy via PR revert), then root-cause + re-ship
- Do not advance to the next major release until the current one is green on live for 24+ hours

**Verifier**: PR is green; post-deploy-smoke is green; Telegram TP channel is silent for 24h.

## Mandatory file artifacts

Every major release commits these:
- `BOT_VERSION` bump in `bot_version.py` + `trade_genius.py`
- `## vX.0.0 — <date>` heading at TOP of `CHANGELOG.md` (existing convention)
- `docs/pl_optimization_final_report_vN.md` linking the backtest source-of-truth that justifies the new defaults
- New tests under `tests/strategy/test_orb_v<NNN>_<feature>.py` — minimum 20 tests for filter levers, 10 tests for day-gates
- This skill file updated if the checklist evolves

## Conventions inherited from CLAUDE.md

- Use `git -c user.email=valira3@gmail.com -c user.name=valira3 commit -F /tmp/commit_msg.txt` for commits
- Branch name: `claude/v<X>.0.0-<theme>` (e.g. `claude/v9.0.0-chase-prevention`)
- PR title: `v<X>.0.0: <one-line summary>` (e.g. `v9.0.0: chase-prevention + SPY regime gate`)
- Open PR via `gh pr create --body-file /tmp/pr_body.md`
- Squash-merge with `--admin` after CI passes: `gh pr merge <N> --squash --admin`

## Anti-patterns (do not repeat)

- **Shipping levers in shadow mode** — defeats the purpose of a major release. Either ship ON or don't ship.
- **Forgetting Val/Gene parity** — has shipped wrong twice (v8.3.1+v8.3.16, v8.3.8+v8.3.18). Always audit both renderer paths.
- **Building a parallel CSV refresher when the bar archive already has the data** — see `bar_archive.py`. SPY/QQQ daily closes are derivable from `/data/bars/`. Reserve external CSV for true external data (VIX).
- **Failing closed on missing data without an operator escape hatch** — production outages happen. Always have a `fail_closed_on_missing_X=False` default with a `_FAIL_CLOSED_X` env override for paranoid operators.
- **Skipping the v13-style P&L optimization report** — major releases need a justifying backtest report committed alongside the code. Reviewer must be able to verify "+$54K/yr" claim.

## v9.0.0 reference application

The v9.0.0 release (this skill's origin) followed this checklist exactly. See:
- `docs/pl_optimization_final_report_v13.md` — backtest source-of-truth
- Branch: `claude/v9.0.0-chase-prevention`
- 4 new env levers: `ORB_MIN_BREAK_BPS`, `ORB_MAX_VWAP_DEV_BPS`, `ORB_MAX_VWAP_DEV_TICKERS`, `ORB_SKIP_PRIOR_SPY_RET_LT_BPS`
- 28 new tests; 898 total strategy tests pass
- UI: 3 new pills on Main + 3 new chips on Val/Gene
- New data source: `tools/orb_spy_loader.py` (bar archive + CSV fallback)
