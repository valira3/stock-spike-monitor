# TradeGenius — Staging, Operations & Promotion Design

**Status:** Draft — 2026-05-14  
**Scope:** Staging environment, backup, security, promotion workflow, hotfix path.

---

## 1. Environment Topology

```
feature/* branches
      |
      v
  [staging]  ←── integration & validation
      |
      v
   [main]    ←── production (Railway auto-deploys)
```

Three tiers:

| Tier | Git Branch | Railway Env | Bot Token | Alpaca Account | Fire Mode |
|---|---|---|---|---|---|
| Local dev | any | — (run_monitor.py --local-only) | none | paper | off |
| Staging | `staging` | `staging` | separate bot | paper | paper-only |
| Production | `main` | `production` | prod bot | paper + live | live (Val) |

---

## 2. Staging Environment Setup

### 2.1 Railway

1. In Railway dashboard: Project → Settings → Environments → **Add Environment** → name `staging`.
2. Railway clones all service config. Set staging-specific overrides:

```
# Strategy — all in observation/paper mode
ORB_LIVE_MODE=0
ORB_EOD_FIRE_BROKER=0

# Staging Telegram bot (separate from prod)
TELEGRAM_BOT_TOKEN=<staging_bot_token>
TELEGRAM_TP_TOKEN=<staging_tp_token>
TELEGRAM_TP_CHAT_ID=<staging_chat_id>

# Staging dashboard password (different from prod)
DASHBOARD_PASSWORD=<staging_password>

# All Alpaca keys → paper accounts only
VAL_ALPACA_PAPER_KEY=<val_paper_key>
VAL_ALPACA_PAPER_SECRET=<val_paper_secret>
# Leave VAL_ALPACA_LIVE_KEY / SECRET unset in staging
# Gene similarly paper-only
```

3. Railway deploys the `staging` branch automatically on push.
4. Staging URL: a separate Railway-assigned domain (e.g. `tradegenius-staging.up.railway.app`).
5. Staging has its own persistent volume (`/data/`) — isolated from production state.

### 2.2 Telegram Bot

Create a dedicated staging bot via @BotFather:
- Name: `TradeGenius Staging`
- All staging alerts, commands, and trade notifications route here
- Production bot is never touched during staging work

### 2.3 .env.monitor.staging

Local monitor can target staging:

```bash
cp .env.monitor .env.monitor.staging
# edit: DASHBOARD_BASE_URL=https://tradegenius-staging.up.railway.app
#       DASHBOARD_PASSWORD=<staging_password>
#       TELEGRAM_TP_TOKEN=<staging_tp_token>

python scripts/run_monitor.py  # reads .env.monitor.staging when MONITOR_ENV=staging
```

Add `MONITOR_ENV` support to `run_monitor.py`: if set to `staging`, load `.env.monitor.staging` instead of `.env.monitor`.

---

## 3. Branch & Version Strategy

```
main
 |
 +── feature/xyz     # short-lived, branched from main
 |
 +── staging         # integration branch, reset-to-main weekly or on demand
```

Rules:
- **Feature branches** merge into `staging` (not `main`) via PR.
- **Staging** is the only branch that merges into `main`.
- **BOT_VERSION** is bumped on every merge to `staging` (minor patch increment).
- Staging version carries a `-rc` suffix in CHANGELOG only (not in code — CI rejects non-numeric versions).
- Production version is the staging version with the `-rc` stripped at promotion time (if needed, or just carry the same number through).

---

## 4. Backup Story

### 4.1 What Needs Backing Up

| Asset | Location | Criticality | Loss Impact |
|---|---|---|---|
| Trade log (`trade_log.jsonl`) | Railway `/data/` | HIGH | Lose today's trade history |
| EOD trade log (`eod_trade_log.jsonl`) | Railway `/data/` | HIGH | Lose EOD P&L record |
| Executor state (`tradegenius_val_live.json`) | Railway `/data/` | HIGH | Val reverts to paper |
| Paper state (`paper_state_main.json`) | Railway `/data/` | MEDIUM | ORB state resets |
| Bar cache (`.bt_cache/`) | local `data/` | LOW | Rebuilt automatically |
| Env vars | Railway | CRITICAL | Service inoperable |

### 4.2 Backup Mechanisms

**State snapshot branch (already live):**  
`.github/workflows/state-snapshot.yml` commits `/api/state` + `/api/executor/val` + `/api/executor/gene` to `snapshots-live` branch every 10 min during RTH. Provides a rolling record of positions, mode, and equity — enough to manually reconstruct state after a volume wipe.

**Nightly backup workflow (to implement):**  
Add `.github/workflows/backup-data.yml`:
- Trigger: cron `0 2 * * 1-5` (02:00 UTC = 22:00 ET, after market close)
- Via Railway API: `railway run -- tar czf /tmp/data-backup.tar.gz /data/`
- Upload tarball to GitHub release artifact or an R2 bucket
- Retain 7 days of backups

**Env var backup:**  
Store a redacted env var manifest (keys only, no values) in `docs/env_manifest.md`. Store actual values in a password manager (1Password/Bitwarden) tagged `railway-production`. Do this after any env var change.

**Manual restore procedure:**  
1. Download latest backup tarball.
2. Railway shell: `tar xzf data-backup.tar.gz -C /`.
3. Redeploy to pick up restored files.
4. Issue `/mode val live confirm` if executor state was wiped.

### 4.3 Railway Volume Risk

Railway persistent volumes survive redeploys but NOT project deletion or volume detachment. Mitigation: the nightly backup + state-snapshot branch provides recovery to within 10 min of any RTH state.

---

## 5. Security Model

### 5.1 Credential Isolation

| Secret | Production | Staging | Local Dev |
|---|---|---|---|
| Telegram bot token | prod bot | staging bot | none |
| Alpaca live keys | Val (live) | **unset** | none |
| Alpaca paper keys | Val+Gene (paper) | Val+Gene (paper, same OK) | from .env.monitor |
| Dashboard password | strong, unique | different from prod | from .env.monitor |
| Railway API token | in .env.monitor | in .env.monitor.staging | same |
| FMP API key | prod | staging (same OK) | from .env.monitor |

Rules:
- Production live Alpaca keys **never** appear in staging env vars.
- Staging dashboard password is different from production.
- `.env.monitor` and `.env.monitor.staging` are in `.gitignore` and never committed.
- All Railway env vars set via dashboard UI or `railway variables set` — never in code.

### 5.2 Access Control

- Railway project: only `valira3@gmail.com` has owner access.
- GitHub repo: private. No external collaborators.
- Telegram bots: owner-only via `auth_guard` in `executors/base.py` (chat ID whitelist).
- Dashboard: password-gated; no unauthenticated state mutation endpoints (read-only by design).

### 5.3 Secrets Rotation Policy

- Alpaca paper keys: rotate if leaked; no financial impact since paper accounts.
- Alpaca live keys: rotate immediately on any suspected leak. Procedure: (1) disable in Alpaca dashboard, (2) generate new, (3) update Railway env var, (4) redeploy, (5) verify Val live mode restored.
- Telegram bot tokens: rotate via @BotFather if leaked. Old token instantly invalid on rotation.
- Dashboard password: rotate via Railway env var + redeploy. No session invalidation needed (stateless auth).

### 5.4 Code Security

- No secrets in source code or CHANGELOG.
- `ruff` lint catches common issues; `run_ci.py` gates all pushes.
- No `eval`, `exec`, or shell injection surfaces in bot command handlers.
- Dashboard endpoints are read-only; no endpoint accepts arbitrary code.

---

## 6. Promotion Process: Staging → Production

### 6.1 Pre-Promotion Checklist

Run these before opening the promotion PR:

```
[ ] python scripts/run_ci.py              # 988+ tests pass, ruff clean, em-dash clean
[ ] python scripts/run_monitor.py --once  # system check against staging: no CRIT/WARN
[ ] Keystone backtest unchanged           # run_ci.py or manual: results/keystone/keystone.json
[ ] UI audit: all three tabs (Main/Val/Gene) render correctly in staging dashboard
[ ] Section order parity (CLAUDE.md rule): new cards land in correct vertical slot on all tabs
[ ] BOT_VERSION bumped in bot_version.py AND trade_genius.py
[ ] CHANGELOG heading matches BOT_VERSION
[ ] ARCHITECTURE.md updated if behavior changed
```

### 6.2 Staging Equivalence Check

Before promoting, run a side-by-side comparison of staging vs production API state:

```python
# tools/compare_envs.py (to implement)
# Fetches /api/state from both envs, diffs:
# - version (expected to differ)
# - regime.mode (should match market hours)
# - v10 config levers (should match between envs)
# - executor modes (staging=paper expected, prod=live for Val)
# Reports any unexpected divergence in strategy config
```

Manual checks:
- Staging dashboard loads without JS errors (browser console clean)
- SSE stream connects and pushes within 5s
- `/api/version` returns expected BOT_VERSION
- Telegram `/status` command responds on staging bot

### 6.3 Promotion PR

```bash
# From staging branch, after all checks pass:
gh pr create \
  --base main \
  --head staging \
  --title "v9.x.y: <summary>" \
  --body-file /tmp/pr_body.md

# PR body must include:
# - Summary of changes
# - Pre-promotion checklist (checked)
# - Keystone result (unchanged / delta)
# - Test plan
```

Merge strategy: **squash merge** (`gh pr merge <N> --squash --admin`).

### 6.4 Post-Promotion Validation

Automated (GHA `post-deploy-smoke.yml` fires on push to main):
- Waits up to 5 min for Railway to deploy new BOT_VERSION
- Runs 31 local + 9 prod smoke tests
- Telegram alert on failure

Manual (within 15 min of deploy):
```bash
curl -sk https://tradegenius.up.railway.app/api/version
# Confirm Val still live:
python scripts/run_monitor.py --once
```

If anything is wrong: **rollback immediately** (see §8).

---

## 7. Comparison: Staging vs Production

Key behavioral differences that are **expected** and should not fail comparison:

| Check | Staging | Production | Why |
|---|---|---|---|
| `executor.val.mode` | paper | live | intentional |
| `eod.fire_mode` | paper | LIVE ORDERS | `ORB_EOD_FIRE_BROKER` diff |
| `orb_live_mode` | 0 (legacy) | 1 (v10) | `ORB_LIVE_MODE` diff |
| Open positions | empty (no real trading) | real paper positions | expected |
| Trade log | empty or synthetic | real day's trades | expected |

Key things that **must match** between environments:

| Check | Expected |
|---|---|
| Strategy config levers (OR minutes, RR, risk%, etc.) | identical |
| Universe tickers | identical |
| Dashboard section order (Main/Val/Gene) | identical |
| `/api/state` schema (field names/types) | identical |
| CHANGELOG top entry = BOT_VERSION | identical |

---

## 8. Hotfix Path (SEV-1 Emergency)

A SEV-1 is any bug causing live P&L loss or preventing entries during RTH.

### 8.1 Definition

- Bot crash during RTH (positions unmanaged)
- NameError/TypeError blocking EOD entry or exit
- Executor reverting to paper during live trading
- Val firing duplicate orders (double-entry bug)

### 8.2 Hotfix Procedure

```
1. ASSESS (< 2 min)
   - Check Railway logs: railway logs --project stock-spike-monitor
   - Check /api/state: curl -sk .../api/state | python -m json.tool
   - Determine blast radius: positions at risk? orders misfired?

2. CONTAIN (immediate)
   - If positions at risk: /halt val in Telegram (kills new entries, manages exits)
   - If duplicate orders: manually cancel in Alpaca dashboard
   - If bot down: Railway dashboard → Deployments → rollback to last good deploy

3. FIX (branch directly from main, NOT staging)
   git checkout main
   git checkout -b hotfix/sev1-<brief-description>
   # ... make minimal fix ...
   # AUDIT: read surrounding 30 lines + all paths in same try/except (CLAUDE.md rule)
   # Bundle sibling bugs in the SAME PR (do not serialize)
   python scripts/run_ci.py
   git commit -m "vX.Y.Z SEV-1 HOTFIX: <description>"
   git push origin hotfix/sev1-<brief-description>

4. DEPLOY (skip staging for true SEV-1)
   gh pr create --base main --head hotfix/sev1-... --title "vX.Y.Z SEV-1 HOTFIX: ..."
   gh pr merge <N> --squash --admin   # bypasses normal staging gate

5. VERIFY (< 5 min post-deploy)
   python scripts/run_monitor.py --once
   curl -sk .../api/version  # confirm new version
   /status val in Telegram   # confirm live mode + sane state

6. BACKPORT
   git checkout staging
   git cherry-pick <hotfix-commit-sha>
   git push  # staging gets the fix too
```

### 8.3 Rollback (if hotfix makes things worse)

Railway keeps the last 5 deployments. Rollback:
1. Railway dashboard → Service → Deployments → click previous deploy → **Redeploy**
2. OR via API: `deploymentRedeploy` mutation with the previous deployment ID (not `deploymentRestart` — it can 502).

### 8.4 Post-Mortem

Within 24h of any SEV-1:
- Add entry to `docs/incident_log.md` (date, duration, root cause, fix, prevention)
- Update CLAUDE.md if a new class of bug was found (e.g., the 2026-05-13 bundled-SEV-1 rule was added this way)
- Add a regression test in `tests/strategy/` covering the failure mode

---

## 9. Staging Reset Policy

Staging branch drifts from main as features accumulate. Reset quarterly or before any major release:

```bash
git checkout staging
git reset --hard main
git push --force-with-lease origin staging
# Railway re-deploys staging from current main state
```

This clears staging of any long-lived experimental branches that weren't promoted.

---

## 10. Implementation Roadmap

| Item | Priority | Effort | Owner |
|---|---|---|---|
| Create Railway staging environment | HIGH | 30 min | manual in Railway dashboard |
| Create staging Telegram bot (@BotFather) | HIGH | 10 min | manual |
| Add `MONITOR_ENV` support to run_monitor.py | HIGH | 1 hr | Claude Code |
| Write `tools/compare_envs.py` | MEDIUM | 2 hr | Claude Code |
| Nightly backup GHA workflow | MEDIUM | 2 hr | Claude Code |
| `docs/env_manifest.md` (keys only) | MEDIUM | 30 min | manual |
| `docs/incident_log.md` template | LOW | 15 min | Claude Code |
| Backfill incident log (2026-05-13 SEV-1 chain) | LOW | 30 min | manual |

---

*Document owner: valira3@gmail.com — revisit before first staging deploy.*
