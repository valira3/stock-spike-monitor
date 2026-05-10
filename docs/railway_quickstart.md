# Railway Sweep Worker — Quick Start

5-step deployment for the second Railway service (sweep worker, separate from prod bot).

## Steps

1. **Railway dashboard → existing project → New Service → GitHub Repo →** `valira3/stock-spike-monitor`

2. **Settings → Build:**
   - Method: **Dockerfile**
   - Dockerfile path: `Dockerfile.sweep-worker`
   - Branch: `main`

3. **Settings → Variables → add env vars:**

   | Variable | Value |
   |---|---|
   | `GIT_REPO_URL` | `https://github.com/valira3/stock-spike-monitor.git` |
   | `GIT_BRANCH` | `main` |
   | `GITHUB_TOKEN` | new fine-grained PAT, **read-only** on this repo |
   | `R2_ENDPOINT_URL` | `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com` |
   | `R2_BUCKET` | your R2 bucket name |
   | `R2_ACCESS_KEY_ID` | from existing R2 secrets |
   | `R2_SECRET_ACCESS_KEY` | same |
   | `RAILWAY_WORKERS` | `4` (legacy, kept for compat — see RAILWAY_PARALLEL_VARIANTS below) |
   | `RAILWAY_PARALLEL_VARIANTS` | `8` (number of variants run simultaneously — set per CPU sizing below) |
   | `SWEEP_WORKERS` | `4` (per-variant inner concurrency — set per CPU sizing below) |
   | `RAILWAY_POLL_INTERVAL_SEC` | `60` |

   **Sizing for your plan**: total active processes = `RAILWAY_PARALLEL_VARIANTS × SWEEP_WORKERS`, should ≤ vCPU count.

   On 32 vCPU / 32 GB **Pro plan** (current):

   | Use case | Recommended config | Active procs | 5-var STRIDE=1 wall |
   |---|---|---:|---:|
   | Small batches (≤8 variants) | `PARALLEL_VARIANTS=8 SWEEP_WORKERS=4` | 32 | ~20 min |
   | Medium batches (9-16 variants) | `PARALLEL_VARIANTS=16 SWEEP_WORKERS=2` | 32 | ~40 min for 16 |
   | Large grids (50+ variants) | `PARALLEL_VARIANTS=16 SWEEP_WORKERS=2` | 32 | ~2.5 hr for 50 |

   On 24 vCPU / 24 GB Hobby Pro (legacy reference):

   | Use case | Recommended config | Active procs | 5-var STRIDE=1 wall |
   |---|---|---:|---:|
   | Small batches (≤6 variants) | `PARALLEL_VARIANTS=6 SWEEP_WORKERS=4` | 24 | ~26 min |
   | Medium batches (7-12 variants) | `PARALLEL_VARIANTS=12 SWEEP_WORKERS=2` | 24 | ~53 min for 12 |
   | Large grids (50+ variants) | `PARALLEL_VARIANTS=12 SWEEP_WORKERS=2` | 24 | ~3.5 hr for 50 |

4. **Settings → Usage Limits →** set spending cap **$10/mo**

5. **Deploy.** Done.

## How it works after deploy

- I push a JSON file to `.github/railway-sweep-trigger/<name>.json` on main
- Worker polls every 60s, picks it up, runs the variants
- Results land in R2 at `sweep-results/railway/<name>/`
- Marker file in R2 prevents re-runs of the same trigger

## Isolation from prod bot

| | Prod bot | Sweep worker |
|---|---|---|
| Dockerfile | `Dockerfile` | `Dockerfile.sweep-worker` |
| Network | Alpaca live, Telegram | git fetch + R2 only |
| Env | live API keys | read-only token + R2 |
| Restart impact | trading halt | none |

Worker can **never** push to git, **never** call Alpaca, **never** touch prod state.

## Validation (optional)

Before the first real run, set `RAILWAY_DRY_RUN=1` as an extra env var. The worker will log triggers but skip execution. Remove the var to enable real runs.

## Reading results

```bash
aws --endpoint-url=$R2_ENDPOINT_URL s3 ls s3://$R2_BUCKET/sweep-results/railway/
aws --endpoint-url=$R2_ENDPOINT_URL s3 cp \
    s3://$R2_BUCKET/sweep-results/railway/<name>/ ./out/ \
    --recursive --include="*/summary.json"
```

## Cost expectations

- 50-variant Tier-2 (STRIDE=1) full-corpus grid: **~$3–5**, ~7 hours wall on 4 parallel workers
- Idle worker (polling only): **<$1/month**
