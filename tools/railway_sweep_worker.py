#!/usr/bin/env python3
"""Railway sweep worker.

Polls a queue directory in git (.github/railway-sweep-trigger/*.json)
for unprocessed sweep configs, runs them locally, and uploads results
to Cloudflare R2. Designed to run as a long-lived service on Railway.

Why this exists: GitHub Actions free tier caps at 20 concurrent jobs.
For final-phase Tier-2 STRIDE=1 mega-grid validations (50+ variants),
that cap matters. A Railway service with N parallel processes can
process the queue faster, and is reusable for continuous validation
sweeps in the future.

Architecture:
    git repo (main)                     R2 bucket
    ──────────────                      ─────────
    .github/railway-sweep-trigger/      sweep-results/
      └─ <trigger-name>.json    ───►     └─ railway/<trigger-name>/
                                              ├─ <vid>/summary.json
                                              ├─ <vid>/per_day/*.json
                                              └─ _processed.marker

    Worker loop:
      1. git fetch origin main
      2. ls .github/railway-sweep-trigger/*.json
      3. For each, check R2 for sweep-results/railway/<name>/_processed.marker
      4. If absent: run all variants in the trigger file (parallel),
         upload per-variant summary + per_day to R2, then write the
         marker.
      5. Sleep RAILWAY_POLL_INTERVAL_SEC (default 60) and repeat.

Required env (Railway service config):
    GIT_REPO_URL          full https URL or ssh URL
    GIT_BRANCH            "main"
    GITHUB_TOKEN          PAT with read access (for private repo clone)
    R2_ENDPOINT_URL       https://<account>.r2.cloudflarestorage.com
    R2_BUCKET             bucket name
    R2_ACCESS_KEY_ID
    R2_SECRET_ACCESS_KEY
    RAILWAY_WORKERS       parallel sweep processes per batch (default 4)
    RAILWAY_POLL_INTERVAL_SEC  default 60
    SWEEP_REPO_DIR        local clone path (default /workspace/repo)

Optional:
    RAILWAY_DRY_RUN=1     don't actually run sweeps; just log what would
                          be processed. For deployment validation.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("railway_sweep_worker")


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    v = os.environ.get(name, default)
    if required and not v:
        raise RuntimeError(f"required env var {name} not set")
    return v or ""


GIT_REPO_URL = _env("GIT_REPO_URL", required=True)
GIT_BRANCH = _env("GIT_BRANCH", "main")
GITHUB_TOKEN = _env("GITHUB_TOKEN", required=True)
SWEEP_REPO_DIR = Path(_env("SWEEP_REPO_DIR", "/workspace/repo"))
TRIGGER_DIR = SWEEP_REPO_DIR / ".github" / "railway-sweep-trigger"

R2_ENDPOINT_URL = _env("R2_ENDPOINT_URL", required=True)
R2_BUCKET = _env("R2_BUCKET", required=True)
R2_ACCESS_KEY_ID = _env("R2_ACCESS_KEY_ID", required=True)
R2_SECRET_ACCESS_KEY = _env("R2_SECRET_ACCESS_KEY", required=True)

RAILWAY_WORKERS = int(_env("RAILWAY_WORKERS", "4"))
RAILWAY_POLL_INTERVAL_SEC = int(_env("RAILWAY_POLL_INTERVAL_SEC", "60"))
RAILWAY_DRY_RUN = _env("RAILWAY_DRY_RUN", "0") == "1"


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    log.debug("run: %s (cwd=%s)", " ".join(cmd), cwd)
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, check=check)


def ensure_repo() -> None:
    """Clone or fast-forward the repo at SWEEP_REPO_DIR."""
    auth_url = GIT_REPO_URL.replace("https://", f"https://x-access-token:{GITHUB_TOKEN}@")
    if not SWEEP_REPO_DIR.exists():
        SWEEP_REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
        log.info("cloning %s -> %s", GIT_REPO_URL, SWEEP_REPO_DIR)
        _run(["git", "clone", "--depth=1", "-b", GIT_BRANCH, auth_url, str(SWEEP_REPO_DIR)])
    else:
        _run(["git", "remote", "set-url", "origin", auth_url], cwd=SWEEP_REPO_DIR, check=False)
        _run(["git", "fetch", "origin", GIT_BRANCH], cwd=SWEEP_REPO_DIR)
        _run(["git", "reset", "--hard", f"origin/{GIT_BRANCH}"], cwd=SWEEP_REPO_DIR)


def _r2_client():
    import boto3  # local import — only used if a sweep runs
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def _r2_key_marker(trigger_name: str) -> str:
    return f"sweep-results/railway/{trigger_name}/_processed.marker"


def is_processed(trigger_name: str) -> bool:
    s3 = _r2_client()
    try:
        s3.head_object(Bucket=R2_BUCKET, Key=_r2_key_marker(trigger_name))
        return True
    except Exception:
        return False


def mark_processed(trigger_name: str, summary: dict) -> None:
    s3 = _r2_client()
    body = json.dumps(summary, indent=2).encode("utf-8")
    s3.put_object(Bucket=R2_BUCKET, Key=_r2_key_marker(trigger_name),
                  Body=body, ContentType="application/json")
    log.info("marked %s processed in R2", trigger_name)


def upload_variant_results(trigger_name: str, vid: str, output_dir: Path) -> int:
    """Upload all files under output_dir/<vid>/ to R2 under the
    sweep-results/railway/<trigger>/<vid>/ prefix. Returns count."""
    s3 = _r2_client()
    base = output_dir / vid
    if not base.is_dir():
        return 0
    n = 0
    for fp in base.rglob("*"):
        if not fp.is_file():
            continue
        rel = fp.relative_to(base).as_posix()
        key = f"sweep-results/railway/{trigger_name}/{vid}/{rel}"
        s3.upload_file(str(fp), R2_BUCKET, key)
        n += 1
    return n


def run_variant(variant: dict, trigger_name: str, output_root: Path) -> dict:
    """Run a single variant via tools/lever_sweep_runner.py."""
    vid = variant["vid"]
    stride = variant.get("stride", "1")
    overrides = variant.get("env", {})

    env = os.environ.copy()
    # PROD_BASE matches the GH Actions workflow defaults.
    env.update({
        "POST_LOSS_COOLDOWN_MIN": "30",
        "POST_LOSS_COOLDOWN_MIN_LONG": "30",
        "POST_LOSS_COOLDOWN_MIN_SHORT": "30",
        "VOLUME_GATE_ENABLED": "false",
        "V730_STOP_HYSTERESIS_ENABLED": "1",
        "V730_STOP_HYSTERESIS_BARS": "2",
        "V730_STOP_DEEP_FRAC": "0.0075",
        "V740_MFE_RATCHET_ENABLED": "1",
        "V740_MFE_RATCHET_ARM_R": "1.0",
        "V740_MFE_RATCHET_FRAC": "0.5",
        "V750_EARLY_DITCH_ENABLED": "1",
        "V750_EARLY_DITCH_WINDOW_SEC": "180",
        "V750_EARLY_DITCH_RED_DOLLARS": "10",
        "V770_POST_DITCH_COOLDOWN_ENABLED": "1",
        "V770_POST_DITCH_COOLDOWN_MIN": "30",
        "V780_OPENING_DELAY_ENABLED": "1",
        "V780_OPENING_DELAY_UNTIL_ET": "09:45",
        "V15_HARD_STRIKE_CAP": "1",
        "V15_SCALED_DI_FLOOR_ENABLED": "1",
        "V15_SCALED_DI_FLOOR": "25.0",
        "V15_REQUIRE_5M_ADX_20": "1",
        "V15_MOMENTUM_ADX_5M_MIN": "20.0",
        "V15_ALARM_E_POST_ENABLED": "1",
        "SSM_SMOKE_TEST": "1",
        "TELEGRAM_BOT_TOKEN": "000:fake",
        "FMP_API_KEY": "sweep_dummy_key",
        "LOG_LEVEL": "WARNING",
        "VID": vid,
        "DATES_STRIDE": str(stride),
        "WARMUP_ENABLED": "0",
        "SWEEP_WORKERS": "2",
        "SWEEP_OUTPUT_ROOT": str(output_root),
        "SWEEP_ISOLATE_ROOT": f"/tmp/railway_isolate/{trigger_name}_{vid}",
    })
    # Variant overrides win.
    env.update({k: str(v) for k, v in overrides.items()})

    log.info("[%s/%s] starting (stride=%s, %d overrides)",
             trigger_name, vid, stride, len(overrides))
    if RAILWAY_DRY_RUN:
        log.info("[%s/%s] DRY_RUN — skipping execution", trigger_name, vid)
        return {"vid": vid, "rc": 0, "dry_run": True}

    cmd = [sys.executable, "tools/lever_sweep_runner.py"]
    proc = subprocess.run(cmd, cwd=str(SWEEP_REPO_DIR), env=env,
                          capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        log.error("[%s/%s] FAILED rc=%d stderr=%s", trigger_name, vid,
                  proc.returncode, proc.stderr[-1000:])
        return {"vid": vid, "rc": proc.returncode,
                "stderr_tail": proc.stderr[-2000:]}

    n_uploaded = upload_variant_results(trigger_name, vid, output_root)
    log.info("[%s/%s] done, uploaded %d files", trigger_name, vid, n_uploaded)
    return {"vid": vid, "rc": 0, "uploaded": n_uploaded}


def process_trigger(trigger_path: Path) -> None:
    name = trigger_path.stem
    log.info("processing trigger %s", name)
    try:
        config = json.loads(trigger_path.read_text())
    except Exception as e:
        log.error("could not parse %s: %s", trigger_path, e)
        return

    variants = config.get("variants") if isinstance(config, dict) else config
    if not isinstance(variants, list):
        log.error("trigger %s has no variants list", name)
        return

    output_root = Path(f"/tmp/railway_output/{name}")
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    # Run variants in parallel via ThreadPoolExecutor. Each variant
    # spawns its own subprocess (lever_sweep_runner.py), so the parent
    # is mostly waiting on subprocess.run -- threads are appropriate.
    # Each subprocess uses ProcessPoolExecutor internally with
    # SWEEP_WORKERS workers; total active processes = parallel_variants
    # * SWEEP_WORKERS, must be sized to vCPU.
    #
    # Sizing examples on a 24-vCPU Railway plan:
    #   parallel_variants=6,  SWEEP_WORKERS=4  -> 24 procs (best for
    #                                              small batches: 6 vars
    #                                              finish in ~26 min)
    #   parallel_variants=12, SWEEP_WORKERS=2  -> 24 procs (best for
    #                                              large batches: 12+
    #                                              vars in ~53 min)
    parallel_variants = max(1, int(os.environ.get(
        "RAILWAY_PARALLEL_VARIANTS", str(RAILWAY_WORKERS))))
    parallel_variants = min(parallel_variants, len(variants))
    log.info("[%s] running %d variants with parallel_variants=%d",
             name, len(variants), parallel_variants)

    results = []
    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(max_workers=parallel_variants) as ex:
        futures = {
            ex.submit(run_variant, v, name, output_root): v
            for v in variants
        }
        completed = 0
        for fut in _cf.as_completed(futures):
            completed += 1
            try:
                r = fut.result()
            except Exception as e:
                v = futures[fut]
                log.error("[%s/%s] threw: %s",
                          name, v.get("vid", "?"), e)
                r = {"vid": v.get("vid"), "rc": -99, "exc": str(e)}
            results.append(r)
            log.info("[%s] %d/%d variants complete (latest: %s)",
                     name, completed, len(variants), r.get("vid"))

    summary = {
        "trigger": name,
        "variants": len(variants),
        "succeeded": sum(1 for r in results if r["rc"] == 0),
        "failed": sum(1 for r in results if r["rc"] != 0),
        "results": results,
    }
    mark_processed(name, summary)


def loop_once() -> int:
    """One iteration of the polling loop. Returns count of processed triggers."""
    ensure_repo()
    if not TRIGGER_DIR.is_dir():
        log.debug("trigger dir %s does not exist; nothing to do", TRIGGER_DIR)
        return 0
    triggers = sorted(TRIGGER_DIR.glob("*.json"))
    n_processed = 0
    for trigger_path in triggers:
        name = trigger_path.stem
        if is_processed(name):
            continue
        process_trigger(trigger_path)
        n_processed += 1
    return n_processed


def main() -> int:
    log.info("Railway sweep worker starting (workers=%d, poll=%ds, dry_run=%s)",
             RAILWAY_WORKERS, RAILWAY_POLL_INTERVAL_SEC, RAILWAY_DRY_RUN)
    while True:
        try:
            n = loop_once()
            if n:
                log.info("processed %d triggers; sleeping %ds",
                         n, RAILWAY_POLL_INTERVAL_SEC)
            else:
                log.debug("no new triggers; sleeping %ds",
                          RAILWAY_POLL_INTERVAL_SEC)
        except Exception:
            log.exception("loop iteration failed; continuing")
        time.sleep(RAILWAY_POLL_INTERVAL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
