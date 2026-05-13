---
name: infrastructure-access
description: Where production state lives, how to reach it, and what's firewalled from the sandbox. Covers GHA workflow dispatch + trigger-JSON convention, GitHub MCP repo-scoping, the "branches as data stores" pattern (snapshots-live, sweep-results, data-extensions, tick-validation-results), R2 storage credentials + the access/secret-key-swap gotcha, Railway dashboard endpoints + the firewall + post-deploy-smoke flow, local corpus mirror layout, and Telegram alerting. Read this when you need to fetch production data, dispatch a workflow, run a sweep, or wire a new data feed. Distilled from the v8.3.24 through v9.0.0 work.
---

# Infrastructure access patterns

The sandbox is firewalled from `tradegenius.up.railway.app`, `r2.cloudflarestorage.com`, `query1.finance.yahoo.com`, and most direct data sources ("Host not in allowlist"). Production state is reachable only through the `mcp__github__*` tools (repo-scoped to `valira3/stock-spike-monitor` only) and pre-staged data committed to specific branches. This skill maps the entry points so research isn't blocked by a Host-not-allowed error.

## 1. GitHub MCP (the only network surface)

Scope: `valira3/stock-spike-monitor` only. Cannot read or write any other repo.

Useful methods (load via `ToolSearch query="select:mcp__github__<name>"`):
- `get_file_contents` — read any file at any ref. Large files (>~200KB) return a "result exceeds maximum allowed tokens" message + a path to a temp file; use `jq` or `python3` to slice rather than `Read`-ing the whole thing.
- `list_commits` / `get_commit` — for branch state without checking out
- `create_pull_request` / `merge_pull_request` (`merge_method: squash`) / `list_pull_requests`
- `pull_request_read` — methods include `get_check_runs`, `get_review_comments`, `get_status`, `get_diff`, `get_files`
- `subscribe_pr_activity` (PR number) — once subscribed, CI failures + comments arrive as `<github-webhook-activity>` messages. Don't poll.
- `push_files` (multi-file commit), `create_or_update_file` (single file), `create_branch`

Anti-patterns:
- Polling unauthenticated GitHub from inside a `Monitor` shell hits the 60/hr rate limit fast. Always use the MCP tools (authenticated).
- Using `get_file_contents` on the entire `latest.json` (300KB) wastes context. Slice with `jq` after download.

## 2. The "branches as data stores" pattern

Production writes results to dedicated branches so they're readable via MCP `get_file_contents` without needing dashboard auth or direct HTTP.

| Branch | Written by | Schema | Refresh cadence |
|---|---|---|---|
| `snapshots-live` | `.github/workflows/state-snapshot.yml` | `data/snapshots/latest.json` + `data/snapshots/<DATE>.jsonl` | every 10 min, Mon-Fri 13:00-21:00 UTC |
| `data-extensions/rth-expand` | `.github/workflows/pull-rth-bars.yml` | `data/<DATE>/<TICKER>.jsonl` (1m bars) | on trigger-JSON push |
| `sweep-results` | `.github/workflows/lever-sweep.yml` | `sweeps/run-<id>/<vid>/summary.json` + `per_day/` | on workflow_dispatch |
| `tick-validation-results` | `.github/workflows/tick-atr-validation.yml` | `tick_vs_bar/<DATE>/<TICKER>.json` | on workflow_dispatch |
| `main` (R2 mirror via cron) | `.github/workflows/r2-export-results.yml` | indirect — points at R2 keys | nightly |

To fetch via MCP:
```python
mcp__github__get_file_contents(
    owner="valira3", repo="stock-spike-monitor",
    path="sweeps/run-<id>/<vid>/summary.json",
    ref="sweep-results",
)
```

When a snapshot you need is outside the cron window, the operator (not you) dispatches the workflow via Actions tab → workflow → "Run workflow". The sandbox cannot dispatch workflows directly even with GH MCP — workflow_dispatch requires write scope that's not granted.

## 3. Dispatching GHA workflows via trigger-JSON files

Workflows that need parameterized invocations follow a convention: drop a JSON file under `.github/<workflow>-trigger/<name>.json` and push. The workflow watches that directory via `on: push: paths:` and auto-fires.

| Workflow | Trigger dir | Example |
|---|---|---|
| `pull-rth-bars.yml` | `.github/rth-trigger/` | `fill-2026-05-12.json` |
| `pull-premarket.yml` | `.github/premarket-trigger/` | `full_corpus.json` |
| `railway-sweep` | `.github/railway-sweep-trigger/` | `v789_finer_gradient_explore.json` |

Trigger JSON schema is workflow-specific; copy from existing files in the same dir.

Some workflows (`lever-sweep.yml`, `tick-atr-validation.yml`) take a `workflow_dispatch` JSON `variants` input directly from the Actions UI rather than a trigger file. For those, output the JSON locally with `python3 docs/research/r<N>_<theme>.py --print-variants` and paste into the dispatch form.

To bypass the version-bump-check workflow on a docs-only or trigger-only PR, include `[skip-version]` anywhere in the PR title.

## 4. R2 (Cloudflare R2 object storage)

Used for tick data + large sweep artifacts. Credentials live in GHA secrets:
- `R2_ACCESS_KEY_ID` — **32 characters** (NOT 64)
- `R2_SECRET_ACCESS_KEY` — typically 64 characters
- `R2_ENDPOINT` — e.g. `https://<acct>.r2.cloudflarestorage.com`
- `R2_BUCKET` — e.g. `tradegenius-tick-data`

Gotcha that ate ~2 hours in v8.3.29: if the access key reports "Credential access key has length 64, should be 32", the values are **swapped** — your `R2_ACCESS_KEY_ID` secret actually holds the secret-access-key. Fix by checking length in the workflow before the boto3 call and erroring early:

```python
import os
ak = os.environ["R2_ACCESS_KEY_ID"]
sk = os.environ["R2_SECRET_ACCESS_KEY"]
assert len(ak) == 32, f"R2_ACCESS_KEY_ID has length {len(ak)}, should be 32"
```

Tick data layout: `s3://<bucket>/ticks/<DATE>/<TICKER>.jsonl.gz`. See `tools/fetch_alpaca_ticks.py`.

Sandbox cannot reach R2 directly. Workflows download → process → commit-to-results-branch so the sandbox reads via MCP afterwards.

## 5. Railway dashboard endpoints

URL: `https://tradegenius.up.railway.app/`. Sandbox is firewalled from this host.

| Endpoint | Auth | Used for |
|---|---|---|
| `/api/version` | none (public) | `post-deploy-smoke.yml` polls this for the new BOT_VERSION after a Railway deploy |
| `/api/state` | `DASHBOARD_PASSWORD` | full v10 engine snapshot (config, day_status, risk_books, day_states, or_windows, activity, mbr_reject_count, vwap_chase_reject_count) |
| `/api/trade_log?limit=N` | password | closed-leg trade history with `entry_time` (ET), `exit_time` (UTC), `pnl`, `reason`, `portfolio` |
| `/api/executor/val`, `/api/executor/gene` | password | per-portfolio Alpaca account + diagnostics |
| `/api/daily_stats` | password | daily stats including per-ticker prev-close + open |
| `/health/tick` | `#h-tick` header | scan-loop heartbeat (per CLAUDE.md, never hide the count) |

Workaround for the sandbox firewall: use the `state-snapshot-retrieval` skill — the `snapshots-live` branch contains `/api/state` + executor + trade_log every 10 min during RTH. Read via MCP.

Railway redeploy semantics:
- A push to `main` triggers redeploy automatically
- For programmatic redeploy: GraphQL `deploymentRedeploy` mutation, **NOT** `deploymentRestart` (the latter can hang Railway in 502)
- Requires `RAILWAY_API_TOKEN`; only used by `scripts/post_deploy_smoke.sh` (the manual-smoke path, not the auto-CI path)
- Auto-CI smoke uses GHA secrets only (DASHBOARD_PASSWORD + TELEGRAM_TP_TOKEN + TELEGRAM_TP_CHAT_ID); no Railway API token needed

## 6. Post-deploy smoke flow

`.github/workflows/post-deploy-smoke.yml` auto-fires on push to `main`:
1. Polls `https://tradegenius.up.railway.app/api/version` for up to 5 min until the new `BOT_VERSION` appears
2. Runs `python smoke_test.py` (31 local + 9 prod checks)
3. On any failure: Telegram-alerts the TP chat with the failing test names + Action URL
4. Silent = pass

**Do NOT propose running `scripts/post_deploy_smoke.sh <version>` locally** unless GHA is broken. That's a different code path (sources `scripts/lib/checks.sh`) that needs `RAILWAY_API_TOKEN` directly.

## 7. Data feeds + auto-refresh

| Feed | Cron | Source | Stored at |
|---|---|---|---|
| VIX daily | `refresh-data-feeds.yml`, 12:00 UTC daily | datahub.io `finance-vix` mirror via `curl` | `data/external/vix-daily.csv` (committed) |
| Earnings calendar | `refresh-data-feeds.yml` same job | yfinance + `tools/orb_earnings_fetcher.py` | `tools/orb_earnings_calendar.py` (committed) |
| SPY daily (v9.0.0+) | (no cron) | reads bar archive `/data/bars/<DATE>/SPY.jsonl` directly via `tools/orb_spy_loader.py` | (computed on-demand at session start) |
| RTH 1m bars | `pull-rth-bars.yml` on trigger-JSON push | Alpaca IEX feed (free) | `data-extensions/rth-expand` branch + `/data/bars/` in production |
| Tick data | `pull-tick-data.yml` on workflow_dispatch | Alpaca SIP (paid) | R2 |

Fail-open principle: each data feed's gate in `orb/day_gates.py:DayGateConfig` has a `fail_closed_on_missing_X: bool = False` field. Default fail-open so a data outage doesn't strand the strategy. Operator sets `=True` in Railway env only if paranoia warranted.

## 8. Local corpus mirror

For local research without dispatching GHA:
- `/tmp/rth-data/data/<DATE>/<TICKER>.jsonl` — git worktree of `data-extensions/rth-expand`, 251 days
- `/tmp/cv_q2_2025/`, `/tmp/cv_q3_2025/`, `/tmp/cv_q4_2025/`, `/tmp/cv_q1q2_2026/` — quarterly symlink slices for CV
- `data/external/vix-daily.csv` — checked in; loaded by `tools/orb_vix_loader.py`

When local backtest results differ from a GHA sweep run, suspect corpus drift — the GHA cron may have refreshed bars since you last pulled. Re-fetch the worktree from the latest `data-extensions/rth-expand` ref.

## 9. Telegram alerting

GHA secrets: `TELEGRAM_TP_TOKEN` + `TELEGRAM_TP_CHAT_ID` (the "TP" = Trade Pilot channel).

Used by:
- `post-deploy-smoke.yml` — fires on failure with the failing test names
- `dashboard-monitor.yml` — periodic invariant checker
- Manual scripts at `scripts/notify_telegram.sh` (if needed)

Format constraint (CLAUDE.md): mobile code-block lines ≤ 34 chars. Wrap long output before sending.

## 10. The 60/hr rate-limit trap

A `Bash` invocation of `curl https://api.github.com/...` from the sandbox is unauthenticated and counts against the 60/hr per-IP limit. The GHA runners hit the same limit shared across all sandbox-side invocations. Symptoms: 403 with `X-RateLimit-Remaining: 0`.

Always use `mcp__github__*` tools instead — they use a session-scoped GitHub App token with much higher limits.

## 11. Snapshot file size budget

`data/snapshots/latest.json` is ~150-400KB. Reading via `mcp__github__get_file_contents` reports a token-budget overflow + writes to a temp file. **Don't fight it**: use `jq` or inline `python3 -c "..."` to extract specific fields rather than reading the whole thing into context.

Pattern:
```bash
SNAP=/tmp/<the-temp-path-returned>
python3 -c "
import json, re
raw = json.load(open('$SNAP'))
text = raw[1]['text']
m = re.search(r'\]\s*(\{.*)', text, re.DOTALL)
snap = json.loads(m.group(1))
# now drill into snap['endpoints']['/api/state']['v10']...
"
```

## 12. The repo-restriction reminder

If the operator asks you to interact with a different repo (e.g., "check valira3/dotfiles"), refuse. The MCP server is configured to allow only `valira3/stock-spike-monitor`. Calls outside this scope are denied at the server level.
