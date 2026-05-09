#!/usr/bin/env python3
"""v7.3.0-experimental — clean baseline + block15 pair.

Re-runs BOTH variants with the harness-record-after-delegate fix in
backtest/replay_v511_full.py so the baseline is comparable apples-to-
apples. The original baseline at
v730_regime_c_skip_backtest/baseline/per_day/ was built with the
phantom-entry harness and may have inflated entries from early-return
guards (post-loss cooldown, daily-loss-limit). Re-running both ensures
the comparison is honest.
"""
from __future__ import annotations
import concurrent.futures as cf
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = "/tmp/ssm_v661"
BARS = "/home/user/workspace/canonical_backtest_data_v707/replay_layout"
ROOT = Path("/home/user/workspace/v730_first15_block_backtest")
ISOLATE = Path("/tmp/v730_clean_pair_isolate")
SHARED_BAR_CACHE = ISOLATE / "_shared_bar_cache"
WARMUP_SRC = Path("/home/user/workspace/v6_15_6_warmup_data/bars")
DATES_FILE = Path("/home/user/workspace/canonical_backtest_data_v707/days_84.txt")
PROGRESS = ROOT / "PROGRESS_clean.json"
FINAL = ROOT / "FINAL_clean.json"
WORKERS = 2

PROD_BASE = {
    "POST_LOSS_COOLDOWN_MIN": "30",
    "POST_LOSS_COOLDOWN_MIN_LONG": "30",
    "POST_LOSS_COOLDOWN_MIN_SHORT": "30",
    "VOLUME_GATE_ENABLED": "true",
    "VOLUME_BUCKET_THRESHOLD_RATIO": "0.85",
    "POST_EXIT_SAME_TICKER_COOLDOWN_SEC": "1",
}

VARIANTS = [
    ("baseline_clean", {**PROD_BASE}, "baseline_clean"),
    ("block15_clean", {**PROD_BASE, "V730_FIRST_N_MIN_BLOCK": "15"}, "block15_clean"),
]


def seed_warmup(slot_dir: Path) -> int:
    bar_archive = slot_dir / "bars"
    bar_archive.mkdir(parents=True, exist_ok=True)
    seeded = 0
    for src in WARMUP_SRC.iterdir():
        if not src.is_dir():
            continue
        dst = bar_archive / src.name
        if dst.exists():
            continue
        try:
            shutil.copytree(src, dst)
            seeded += 1
        except Exception:
            pass
    return seeded


def run_one_day(args):
    date, slot_dir, extras, out_path = args
    # per-day cleanup
    for stale in ("paper_state.json", "state.db", "trade_log.jsonl",
                  "paper_trade.log", "ingest_audit.db"):
        p = slot_dir / stale
        if p.exists():
            try: p.unlink()
            except: pass

    env = {
        **os.environ,
        "SSM_SMOKE_TEST": "1",
        "TELEGRAM_BOT_TOKEN": "000:fake",
        "FMP_API_KEY": "sweep_dummy_key",
        "LOG_LEVEL": "WARNING",
        "TG_DATA_ROOT": str(slot_dir),
        "STATE_DB_PATH": str(slot_dir / "state.db"),
        "BAR_ARCHIVE_BASE": str(slot_dir / "bars"),
        "OR_DIR": str(slot_dir / "or"),
        "FORENSICS_DIR": str(slot_dir / "forensics"),
        "VOLUME_PROFILE_DIR": str(slot_dir / "volume_profile"),
        "INGEST_AUDIT_DB_PATH": str(slot_dir / "ingest_audit.db"),
        "TRADE_LOG_PATH": str(slot_dir / "trade_log.jsonl"),
        "UNIVERSE_GUARD_PATH": str(slot_dir / "tickers.json"),
        "PAPER_STATE_PATH": str(slot_dir / "paper_state.json"),
        "PAPER_LOG_PATH": str(slot_dir / "paper_trade.log"),
        "SSM_BAR_CACHE_DIR": str(SHARED_BAR_CACHE),
    }
    env.update(extras)
    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "backtest.replay_v511_full",
             "--date", date, "--bars-dir", BARS, "--output", str(out_path)],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=180,
        )
        ok = proc.returncode == 0 and out_path.exists()
        if ok:
            try:
                j = json.loads(out_path.read_text())
                entries = (j.get("summary") or {}).get("entries", 0)
                pairs = len(j.get("pnl_pairs", []))
                pnl = sum(p.get("pnl_dollars", 0) for p in j.get("pnl_pairs", []))
                return (date, "OK", entries, pairs, round(pnl, 2), round(time.time()-t0, 1))
            except Exception as e:
                return (date, f"PARSE_ERR:{e}", 0, 0, 0, round(time.time()-t0, 1))
        return (date, f"RC{proc.returncode}", 0, 0, 0, round(time.time()-t0, 1))
    except subprocess.TimeoutExpired:
        return (date, "TIMEOUT", 0, 0, 0, round(time.time()-t0, 1))


def already_done(out: Path) -> bool:
    if not out.exists() or out.stat().st_size < 100:
        return False
    try:
        j = json.loads(out.read_text())
        return (j.get("summary") or {}).get("entries") is not None
    except Exception:
        return False


def run_variant(name: str, extras: dict, out_subdir: str):
    print(f"\n=== {name} ===", flush=True)
    raw_dir = ROOT / out_subdir / "per_day"
    raw_dir.mkdir(parents=True, exist_ok=True)

    var_iso = ISOLATE / name
    var_iso.mkdir(parents=True, exist_ok=True)
    slot_dirs = [var_iso / f"slot{i}" for i in range(WORKERS)]
    for s in slot_dirs:
        s.mkdir(parents=True, exist_ok=True)
        n = seed_warmup(s)
        print(f"  {s.name}: warmup seeded {n} date dirs", flush=True)

    dates = [d.strip() for d in DATES_FILE.read_text().splitlines() if d.strip()]
    tasks = []
    for i, date in enumerate(dates):
        out = raw_dir / f"{date}.json"
        if already_done(out):
            continue
        tasks.append((date, slot_dirs[i % WORKERS], extras, out))
    print(f"  {len(tasks)}/{len(dates)} days to run", flush=True)
    if not tasks:
        return

    completed = 0
    fails = 0
    t_start = time.time()
    with cf.ProcessPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(run_one_day, t): t[0] for t in tasks}
        for fut in cf.as_completed(futs):
            try:
                date, status, entries, pairs, pnl, dur = fut.result()
            except Exception as e:
                print(f"  FUT_ERR: {e}", flush=True)
                fails += 1
                continue
            completed += 1
            if status != "OK":
                fails += 1
            if completed % 5 == 0 or status != "OK":
                elapsed = round(time.time() - t_start, 1)
                print(f"  [{completed}/{len(tasks)}] {date} {status} entries={entries} pairs={pairs} pnl={pnl} {dur}s (elapsed {elapsed}s, fails={fails})", flush=True)
    print(f"  DONE {name}: {completed} done, {fails} failed, wall {round(time.time()-t_start,1)}s", flush=True)


def main():
    SHARED_BAR_CACHE.mkdir(parents=True, exist_ok=True)
    for name, extras, out_subdir in VARIANTS:
        run_variant(name, extras, out_subdir)
    FINAL.write_text(json.dumps({"done_at": time.time()}, indent=2))
    print("\nALL DONE.")


if __name__ == "__main__":
    main()
