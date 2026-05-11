# Dashboard monitor setup

Shipped in **v7.65.0**. Live RTH validator for the production dashboard.

## What it does

Every 5 minutes during US market hours (Mon-Fri, 13:00-20:00 UTC) a
GitHub Actions cron job runs `tools/dashboard_monitor.py`, which:

1. Hits the production read endpoints with an HMAC-signed session
   cookie minted from `DASHBOARD_SESSION_SECRET`:
     - `GET /api/state`
     - `GET /api/executor/val`
     - `GET /api/executor/gene`
     - `GET /api/v10/projection`
2. Runs every invariant in `tools/dashboard_monitor_invariants.py`
   against the responses.
3. On any violation:
     - Posts a structured alert to `TELEGRAM_ADMIN_CHAT_ID`
     - Files a GitHub issue with full diagnostic context, including a
       `@claude` mention so the Claude Code GitHub app drafts a
       **draft** fix PR (never auto-merged; operator reviews).
4. Exits 0 even on violations (the issue + Telegram alert is the
   signal; we don't want red badges on the Actions tab for every
   transient blip).

## Required GitHub Actions secrets

Set these at **Settings → Secrets and variables → Actions → New
repository secret**:

| Secret | Value |
|---|---|
| `DASHBOARD_BASE_URL` | `https://tradegenius.up.railway.app` (no trailing slash) |
| `DASHBOARD_SESSION_SECRET` | Same hex value the live bot has for `DASHBOARD_SESSION_SECRET` on Railway (Settings → Environment Variables → `DASHBOARD_SESSION_SECRET`). Must be ≥ 32 bytes hex (= 64 hex chars). |
| `TELEGRAM_BOT_TOKEN` | Existing bot token (the same `TELEGRAM_TOKEN` the live bot uses) |
| `TELEGRAM_ADMIN_CHAT_ID` | Chat ID where alerts post. Get from `@userinfobot` on Telegram, or look up the existing `TELEGRAM_CHAT_ID` env var on Railway. |

`GITHUB_TOKEN` for issue filing is provided automatically by the
workflow runner; nothing to set.

## Verifying setup

After adding the secrets, manually trigger a dry run:

1. **Actions** tab → **dashboard-monitor** → **Run workflow** → set
   "Dry run" to `true` → **Run workflow**.
2. The job will fetch + run invariants without posting Telegram or
   filing issues. Check the step output to see what it observed and
   whether any invariants fired.

If the dry run says everything passes and the production state looks
real (non-empty `v10` block, real ticker prices, etc.), you're good.
Flip "Dry run" to `false` for the next manual run to test the alert
sinks end-to-end with a synthetic violation, or just wait for the
cron to fire.

## Invariants

Listed in `tools/dashboard_monitor_invariants.py:INVARIANTS`. Current
set:

| Invariant | What it guards |
|---|---|
| `state_reachable` | `/api/state` returns 200 with parsable JSON |
| `executors_reachable` | `/api/executor/val` + `/api/executor/gene` reachable |
| `version_advertised` | `bot_version` parses, major >= 7 |
| `v10_live_mode_on` | During RTH, v10 is bootstrapped + live_mode=true (catches legacy fallback) |
| `equity_matches_baseline` | Equity KPI == v10 Baseline live_balance (v7.64.0 regression guard) |
| `val_gene_trades_match_main` | Mirror-mode: Val/Gene broker trade counts match Main's |
| `top_ticker_within_cap` | No (pid, ticker) day_state has `trades_today > max_trades_per_day` |
| `open_risk_within_cap` | Every pid's `open_risk <= max_risk_dollars` |
| `or_window_well_formed` | Locked OR windows have `or_low <= or_high` and sane width |
| `no_phantom_positions` | Main `positions.length == risk_books.main.open_count` |
| `daily_kill_consistency` | `daily_kill_triggered` matches `realized_pnl_today <= -threshold` |

Add a new invariant by appending a function to the `INVARIANTS` list
at the bottom of `dashboard_monitor_invariants.py`. The function
takes an `InvariantContext` and returns `{"name", "ok", "summary",
"detail"}`.

## Auto-fix flow

When an invariant fires, the filed GitHub issue body ends with:

> @claude please investigate the violation(s) above and open a
> **draft** PR with a proposed fix. Do not auto-merge -- the operator
> reviews every monitor-triggered change before it ships.

This relies on the **Claude Code GitHub app** being installed on this
repo with issue-mention triggers enabled. If the app is configured:

1. Issue is filed by the workflow.
2. Claude Code picks up the mention, investigates, opens a **draft**
   PR with the fix.
3. Operator reviews the draft PR. If acceptable, mark ready-for-review
   and merge through the standard PR flow.

If the Claude Code GitHub app is **not** installed, the issue still
gets filed with full diagnostic context for manual debugging; the
`@claude` mention is harmless.

## Disable / pause

To temporarily disable monitoring:

- **Pause cron**: Comment out the `schedule:` block in
  `.github/workflows/dashboard-monitor.yml` and commit.
- **Pause Telegram alerts only**: Delete the `TELEGRAM_BOT_TOKEN` or
  `TELEGRAM_ADMIN_CHAT_ID` secret; the monitor logs the alert and
  carries on filing the GH issue.
- **Pause GH issue filing only**: Set the workflow's `issues`
  permission to `read`.

## Local testing

```bash
DASHBOARD_BASE_URL=https://tradegenius.up.railway.app \
DASHBOARD_SESSION_SECRET=<hex> \
MONITOR_DRY_RUN=1 \
python -m tools.dashboard_monitor
```

Dry run prints what would be posted/filed without doing it.
