# stock-spike-monitor — agent guide

## Where things live
- Entry decision logic: `entry_gate_v5.py` (the V570-STRIKE / V560-GATE path)
- Exit logic (Bison hard-stop + Buffalo trail): `tiger_buffalo_v5.py`, `bison_v5.py`
- Shadow configs: `shadow_configs.py` (4 SHADOW_CONFIGS: TICKER+QQQ 70/100, TICKER_ONLY 70, QQQ_ONLY 100, GEMINI_A 110/85)
- Universe / tickers: code expects `/data/tickers.json` on persistent volume; default in `config.py` UNIVERSE
- Version: `bot_version.py` (`BOT_VERSION = "5.x.y"`)
- Bar archive writer: `bar_archive.py` (writes to `/data/bars/YYYY-MM-DD/<TICKER>.jsonl`)

## Mandatory PR rules
- Bump `BOT_VERSION` in `bot_version.py`
- Add new heading `## v5.x.y — <date>` at TOP of `CHANGELOG.md`
- Update `ARCHITECTURE.md` if behavior changes
- Update `trade_genius_algo.pdf` ONLY when algo text changes (most PRs do not)
- Git author: `git -c user.email=valira3@gmail.com -c user.name=valira3 commit -F /tmp/commit_msg.txt`
- String literals: use `\u2014` escape, NEVER literal em-dash. CHANGELOG/ARCHITECTURE/README MAY use real em-dash.
- Never use words "scrape/crawl/scraping/crawling" anywhere
- Never hide `#h-tick`, never drop the health-pill count
- Telegram mobile code-block: ≤34 chars per line

## Before pushing
Run `bash scripts/preflight.sh` — mirrors CI checks locally:
- pytest
- version-bump consistency (BOT_VERSION matches CHANGELOG heading)
- em-dash literal check on .py files
- ruff/black format check

## Post-deploy smoke
Run `bash scripts/post_deploy_smoke.sh <version>` after every release (the script sources `scripts/lib/checks.sh` for the 7 checks: deploy status, universe loaded, log-tag schema, no errors, bar archive today, shadow_db count, dashboard /api/state). Failures are informational — they do NOT block automated merges; post the output as a PR comment so the author sees it. CI will eventually invoke this automatically; the existing `post-deploy-smoke.yml` workflow remains the blocking gate.

## Tests
- `pytest tests/` (full suite)
- `pytest tests/test_<module>.py -k <name>` (focused)
- New algo PRs: add a unit test under `tests/strategy/`

## Common gotchas
- Universe drift: `/data/tickers.json` on persistent volume can lag code's `UNIVERSE` list. v5.8.0 startup guard auto-rewrites it.
- Railway redeploy != restart: use `deploymentRedeploy` mutation, NOT `deploymentRestart` (which can hang in 502).
- Shadow logs: every new release should add a `[V5xy-<TAG>]` schema; document it in CHANGELOG.

## PR submission
- `gh pr create --title "v5.x.y: <summary>" --body-file /tmp/pr_body.md`
- `gh pr merge <N> --squash --admin` after CI passes
