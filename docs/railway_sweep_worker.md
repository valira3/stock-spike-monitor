# Railway Sweep Worker

Long-lived Railway service that processes lever-sweep batches in
parallel with the GitHub Actions matrix workflow. Designed for
final-phase Tier-2 (STRIDE=1, full 81-day) mega-grid validations
where the GH Actions free-tier 20-job concurrency cap matters.

## Architecture

```
git repo                                 R2 bucket
────────                                 ─────────
.github/railway-sweep-trigger/           sweep-results/railway/
  └─ <trigger-name>.json    ─poll──►       └─ <trigger-name>/
                                             ├─ <vid>/summary.json
                                             ├─ <vid>/per_day/*.json
                                             └─ _processed.marker
```

The worker:

1. Polls main every 60s, hard-resets local clone to remote
2. Lists `.github/railway-sweep-trigger/*.json`
3. For each trigger NOT yet marked processed (R2 marker check):
   - Loads variants from the JSON
   - Runs each via `tools/lever_sweep_runner.py`
   - Uploads per-variant results to R2 under
     `sweep-results/railway/<trigger>/<vid>/`
   - Writes `_processed.marker` to R2 to prevent re-processing

Trigger file format (same as `.github/sweep-trigger/`):

```json
{
  "max_parallel": 4,
  "variants": [
    {"vid": "...", "env": {...}, "stride": "1"}
  ]
}
```

## Deployment

### One-time setup

1. **Provision a new Railway service** (separate from the prod bot)
   - Source: this repo
   - Branch: `main`
   - Build: Docker, Dockerfile = `Dockerfile.sweep-worker`
   - Start command: (blank — Dockerfile sets ENTRYPOINT)
2. **Set service env vars** in Railway:

   | Var | Value |
   |---|---|
   | `GIT_REPO_URL` | `https://github.com/valira3/stock-spike-monitor.git` |
   | `GIT_BRANCH` | `main` |
   | `GITHUB_TOKEN` | a PAT with `contents: read` on this repo |
   | `R2_ENDPOINT_URL` | `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com` |
   | `R2_BUCKET` | the R2 bucket name |
   | `R2_ACCESS_KEY_ID` | from the existing `R2_*` secrets |
   | `R2_SECRET_ACCESS_KEY` | same |
   | `RAILWAY_WORKERS` | `4` (or match plan vCPU count) |
   | `RAILWAY_POLL_INTERVAL_SEC` | `60` |

3. **Validate with a dry-run** first:
   - Set `RAILWAY_DRY_RUN=1` temporarily
   - Push a small trigger file (1 variant)
   - Verify the worker logs the trigger, doesn't actually run sweeps
4. **Deploy for real**: unset `RAILWAY_DRY_RUN` and push a real trigger.

### Resource sizing

A STRIDE=1 (81-day) variant takes ~33 min wall on a 2-vCPU runner.
Railway's Hobby plan offers 8 vCPU / 8 GB; can run 4 concurrent
sweep variants without thrashing. Hobby pricing is ~$5/month base
plus pro-rated CPU/RAM usage; a 50-variant Tier-2 batch should
complete in ~50 × 33min / 4 = ~7 hours wall = ~$3-5 of CPU time.

## Triggering a sweep

Same pattern as the GH Actions auto-trigger:

```bash
# 1. Create the trigger config
cat > .github/railway-sweep-trigger/tier2_final.json <<'EOF'
{
  "variants": [
    {"vid": "final_L25_S45", "env": {"STOP_PCT_LONG": "0.0025", "STOP_PCT_SHORT": "0.0045"}, "stride": "1"},
    ...
  ]
}
EOF

# 2. Commit & push to main (PR or direct)
git add .github/railway-sweep-trigger/tier2_final.json
git commit -m "trigger: tier2 final validation"
git push

# 3. Wait — Railway worker picks it up within RAILWAY_POLL_INTERVAL_SEC.
# 4. Watch results land in R2 at sweep-results/railway/tier2_final/
```

## Reading results

```bash
# Via aws CLI (R2 is S3-compatible)
aws --endpoint-url=$R2_ENDPOINT_URL s3 ls s3://$R2_BUCKET/sweep-results/railway/tier2_final/

# Pull all summary.json
aws --endpoint-url=$R2_ENDPOINT_URL s3 cp \
    s3://$R2_BUCKET/sweep-results/railway/tier2_final/ \
    ./tier2_results/ --recursive --exclude="*" --include="*/summary.json"
```

## When NOT to use this

- **Discovery iteration (STRIDE=8/STRIDE=4 batches)**: GH Actions matrix
  is faster end-to-end (no polling delay, parallel jobs already there).
  Railway worker is for Tier-2 only.
- **<10 variant batches**: GH free-tier handles them directly. The
  Railway worker amortizes its setup cost only on bigger grids.

## Operational notes

- **Idempotent**: re-pushing the same trigger file does NOT re-run
  variants (R2 marker check prevents).
- **Resume on failure**: the worker doesn't track per-variant
  progress within a trigger, so a mid-batch crash means the whole
  batch re-runs. For really long batches, split into multiple
  trigger files.
- **Log retention**: Railway logs are ephemeral. Per-variant
  `summary.json` includes wall time and config; that's the durable
  record.
- **Cost cap**: Railway Hobby plan auto-stops at $5/month cap by
  default. Bump the cap in Railway settings before kicking off a
  big batch.
