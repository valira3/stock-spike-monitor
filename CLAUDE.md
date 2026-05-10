# stock-spike-monitor — agent guide

## Current strategy: v10 ORB anchor (as of v7.27.0)

Production runs the **v10 ORB anchor** strategy. Tiger Sovereign / V570-STRIKE / V560-GATE are retired. The live decision path is:

- **Entry**: `orb/live_runtime.py` → `orb/engine.py` → `orb/state.py` (per-portfolio FSM) → `orb/risk_book.py` (concurrent risk + notional caps) → `orb/day_gates.py` (VIX / earnings / gap / blocklist) → `orb/exits.py` (RR=2.5 + move-to-BE-after-1R).
- **Per-portfolio fanout**: Main / Val / Gene each run their own RiskBook + FSM. `engine/scan.py:_orb_long_entry`/`_orb_short_entry` iterates portfolios and calls `live_runtime.check_entry(...)` per portfolio.
- **Broker fire**: Main goes through `callbacks.execute_entry` (legacy path). Val/Gene route through `executors/base.py:fire_long`/`fire_short` when `ORB_PORTFOLIO_FIRE=1` (default `0` until 5-day paper-fire observation completes).
- **Kill switch**: `ORB_LIVE_MODE=0` falls back to legacy strategy (rollback path).
- **Forensic**: `[V79-ORB-BOOT/RESET/OR-LOCK/GATE/RISK-OK/RISK-NO/ENTRY/REJECT/EXIT/ADMIT/FIRE]` + `[V10-FIRE]` + `[V79-ORB-EQUITY]`.

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

## Before pushing
Run `bash scripts/preflight.sh` — mirrors CI checks locally:
- pytest
- version-bump consistency (`BOT_VERSION` matches CHANGELOG heading AND `trade_genius.py`)
- em-dash literal check on .py files
- ruff/black format check

In sandbox where `telegram` is missing, `pytest tests/strategy/` is the focused alternative (231+ tests; the v10 path is fully covered there).

## Post-deploy smoke
Run `bash scripts/post_deploy_smoke.sh <version>` after every release (the script sources `scripts/lib/checks.sh` for the checks: deploy status, universe loaded, log-tag schema, no errors, bar archive today, dashboard /api/state). v5.14.0 dropped the shadow_db row-count check along with the rest of the shadow strategy. Failures are informational — they do NOT block automated merges; post the output as a PR comment so the author sees it. CI will eventually invoke this automatically; the existing `post-deploy-smoke.yml` workflow remains the blocking gate.

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
