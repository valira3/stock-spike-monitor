#!/usr/bin/env python3
"""v7.2.8 regime-mitigation 83-day sweep.

Variants:
  - baseline           : no env tweaks (sanity check vs existing baseline_clean)
  - be_arm_2r          : BE_ARM_R_MULT=2.0, STAGE2_ARM_R_MULT=2.0
  - be_arm_15r         : BE_ARM_R_MULT=1.5, STAGE2_ARM_R_MULT=1.5
  - streak3_30         : LOSS_STREAK_KILL_N=3, WINDOW=30
  - streak4_30         : LOSS_STREAK_KILL_N=4, WINDOW=30
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
ROOT = Path("/home/user/workspace/v730_regime_mitigations")
ISOLATE = Path("/tmp/v730_regime_isolate")
SHARED_BAR_CACHE = ISOLATE / "_shared_bar_cache"
DATES_FILE = Path("/home/user/workspace/canonical_backtest_data_v707/days_84.txt")
PROGRESS = ROOT / "PROGRESS.json"
FINAL = ROOT / "FINAL.json"
WARMUP_BARS = Path("/home/user/workspace/v6_15_6_warmup_data/bars")
WORKERS = 2

PROD_BASE = {
    "POST_LOSS_COOLDOWN_MIN_LONG": "30",
    "POST_LOSS_COOLDOWN_MIN_SHORT": "30",
    "VOLUME_GATE_ENABLED": "false",
}

VARIANTS = [
    ("baseline", {}),
    ("be_arm_2r", {"BE_ARM_R_MULT": "2.0", "STAGE2_ARM_R_MULT": "2.0"}),
    ("be_arm_15r", {"BE_ARM_R_MULT": "1.5", "STAGE2_ARM_R_MULT": "1.5"}),
    ("streak3_30", {"LOSS_STREAK_KILL_N": "3", "LOSS_STREAK_KILL_WINDOW_MIN": "30"}),
    ("streak4_30", {"LOSS_STREAK_KILL_N": "4", "LOSS_STREAK_KILL_WINDOW_MIN": "30"}),
]


def build_env(slot_dir: Path, extras: dict) -> dict:
    bar_archive = slot_dir / "bars"
    or_dir = slot_dir / "or"
    forensics = slot_dir / "forensics"
    volprof = slot_dir / "volume_profile"
    for d in (slot_dir, bar_archive, or_dir, forensics, volprof, SHARED_BAR_CACHE):
        d.mkdir(parents=True, exist_ok=True)
    for stale in (slot_dir / "paper_state.json", slot_dir / "state.db",
                  slot_dir / "trade_log.jsonl", slot_dir / "paper_trade.log"):
        if stale.exists():
            stale.unlink()
    env = {
        **os.environ,
        "TG_DATA_ROOT": str(slot_dir),
        "SSM_BAR_CACHE_DIR": str(SHARED_BAR_CACHE),
        "STATE_DB_PATH": str(slot_dir / "state.db"),
        "BAR_ARCHIVE_BASE": str(bar_archive),
        "UNIVERSE_GUARD_PATH": str(slot_dir / "tickers.json"),
        "TRADE_LOG_PATH": str(slot_dir / "trade_log.jsonl"),
        "OR_DIR": str(or_dir),
        "FORENSICS_DIR": str(forensics),
        "INGEST_AUDIT_DB_PATH": str(slot_dir / "ingest_audit.db"),
        "VOLUME_PROFILE_DIR": str(volprof),
        "SSM_SMOKE_TEST": "1",
        "TELEGRAM_BOT_TOKEN": "000:fake",
        "FMP_API_KEY": "sweep_dummy_key",
        "LOG_LEVEL": "WARNING",
        "PAPER_STATE_PATH": str(slot_dir / "paper_state.json"),
        "PAPER_LOG_PATH": str(slot_dir / "paper_trade.log"),
    }
    env.update(PROD_BASE)
    env.update(extras)
    return env


def run_one_day(args):
    date, slot_dir_str, extras, out_path = args
    env_dict = build_env(Path(slot_dir_str), extras)
    try:
        r = subprocess.run(
            [sys.executable, "-m", "backtest.replay_v511_full",
             "--date", date, "--bars-dir", BARS, "--output", str(out_path)],
            cwd=REPO, env=env_dict, capture_output=True, text=True, timeout=180,
        )
        ok = r.returncode == 0 and Path(out_path).exists()
        result = {"date": date, "rc": r.returncode, "ok": ok}
        if not ok:
            result["stderr"] = r.stderr[-1500:]
            result["stdout"] = r.stdout[-300:]
        else:
            d = json.loads(Path(out_path).read_text())
            summary = d.get("summary") or {}
            pp = d.get("pnl_pairs", [])
            result["entries"] = summary.get("entries", 0)
            result["exits"] = summary.get("exits", 0)
            result["pnl_pairs"] = len(pp)
            result["net_pnl"] = sum(p.get("pnl_dollars", p.get("pnl", 0)) for p in pp)
            result["wins"] = sum(1 for p in pp if p.get("pnl_dollars", p.get("pnl", 0)) > 0)
            result["losses"] = sum(1 for p in pp if p.get("pnl_dollars", p.get("pnl", 0)) <= 0)
        return result
    except subprocess.TimeoutExpired:
        return {"date": date, "rc": -1, "ok": False, "stderr": "TIMEOUT"}
    except Exception as e:
        return {"date": date, "rc": -2, "ok": False, "stderr": str(e)}


def seed_warmup(slot_dir: Path) -> int:
    """Copy warmup bar data into slot's bar archive. Returns # date dirs seeded."""
    bar_archive = slot_dir / "bars"
    bar_archive.mkdir(parents=True, exist_ok=True)
    seeded = 0
    if WARMUP_BARS.exists():
        for d in sorted(WARMUP_BARS.iterdir()):
            if not d.is_dir():
                continue
            tgt = bar_archive / d.name
            if not tgt.exists():
                shutil.copytree(d, tgt)
                seeded += 1
    return seeded


def write_progress(state):
    PROGRESS.write_text(json.dumps(state, indent=2))


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    ISOLATE.mkdir(parents=True, exist_ok=True)
    SHARED_BAR_CACHE.mkdir(parents=True, exist_ok=True)
    dates = [d.strip() for d in DATES_FILE.read_text().splitlines() if d.strip()]
    print(f"Loaded {len(dates)} dates: {dates[0]} -> {dates[-1]}", flush=True)

    state = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "version": "v7.2.8 + experimental gates",
        "corpus": "v707_84d_sip",
        "variants": [],
    }
    write_progress(state)
    overall_start = time.time()

    for variant_name, extras in VARIANTS:
        v_start = time.time()
        state["current_variant"] = variant_name
        write_progress(state)
        print(f"\n=== START {variant_name} extras={extras} ===", flush=True)

        per_day_dir = ROOT / variant_name / "raw"
        per_day_dir.mkdir(parents=True, exist_ok=True)

        slot_dirs = []
        for i in range(WORKERS):
            s = ISOLATE / f"{variant_name}_slot{i}"
            s.mkdir(parents=True, exist_ok=True)
            seeded = seed_warmup(s)
            print(f"  {variant_name}/slot{i}: warmup seeded {seeded} date dirs", flush=True)
            slot_dirs.append(str(s))

        tasks = []; skipped = 0
        for i, d in enumerate(dates):
            out = per_day_dir / f"{d}.json"
            if out.exists() and out.stat().st_size > 100:
                try:
                    j = json.loads(out.read_text())
                    if (j.get("summary") or {}).get("entries") is not None:
                        skipped += 1; continue
                except Exception:
                    pass
            slot = slot_dirs[i % WORKERS]
            tasks.append((d, slot, extras, out))
        if skipped:
            print(f"  Resuming: skipping {skipped} already-completed days", flush=True)
        print(f"  Running {len(tasks)} days with {WORKERS} workers...", flush=True)

        results = []
        empty_streak = 0
        with cf.ProcessPoolExecutor(max_workers=WORKERS) as ex:
            futures = {ex.submit(run_one_day, t): t[0] for t in tasks}
            for fut in cf.as_completed(futures):
                r = fut.result()
                results.append(r)
                if r["ok"]:
                    empty_streak = 0
                    print(f"  OK   {r['date']} entries={r.get('entries',0):>3} pairs={r.get('pnl_pairs',0):>3} pnl=${r.get('net_pnl',0):+.2f}", flush=True)
                else:
                    empty_streak += 1
                    print(f"  FAIL {r['date']} rc={r['rc']} {r.get('stderr','')[-200:]}", flush=True)
                done = len(results)
                if done % 5 == 0:
                    ok_so_far = sum(1 for x in results if x["ok"])
                    pnl_so_far = sum(x.get("net_pnl", 0) for x in results if x["ok"])
                    elapsed = (time.time() - v_start) / 60
                    print(f"  PROGRESS {done}/{len(tasks)} ok={ok_so_far} pnl={pnl_so_far:.2f} elapsed={elapsed:.1f}min", flush=True)
                if empty_streak > 5:
                    print(f"  ABORT empty_streak={empty_streak}", flush=True)
                    for f in futures: f.cancel()
                    break

        # Merge resumed
        run_dates = {r["date"] for r in results}
        for d in dates:
            if d in run_dates: continue
            out = per_day_dir / f"{d}.json"
            if not out.exists(): continue
            try:
                jd = json.loads(out.read_text())
                summ = jd.get("summary") or {}
                pp = jd.get("pnl_pairs", [])
                results.append({"date": d, "rc": 0, "ok": True,
                    "entries": summ.get("entries", 0),
                    "exits": summ.get("exits", 0),
                    "pnl_pairs": len(pp),
                    "net_pnl": sum(p.get("pnl_dollars", p.get("pnl", 0)) for p in pp),
                    "wins": sum(1 for p in pp if p.get("pnl_dollars", p.get("pnl", 0)) > 0),
                    "losses": sum(1 for p in pp if p.get("pnl_dollars", p.get("pnl", 0)) <= 0),
                    "resumed": True})
            except Exception:
                pass

        results.sort(key=lambda x: x["date"])
        ok_count = sum(1 for x in results if x["ok"])
        net_pnl = sum(x.get("net_pnl", 0) for x in results if x["ok"])
        entries = sum(x.get("entries", 0) for x in results if x["ok"])
        pnl_pairs = sum(x.get("pnl_pairs", 0) for x in results if x["ok"])
        wins = sum(x.get("wins", 0) for x in results if x["ok"])
        losses = sum(x.get("losses", 0) for x in results if x["ok"])
        wr = (wins / (wins + losses) * 100) if (wins + losses) else 0.0
        wall_min = round((time.time() - v_start) / 60, 1)

        summary = {
            "variant": variant_name, "extras": extras,
            "days_total": len(dates), "days_ok": ok_count,
            "net_pnl_83d": round(net_pnl, 2),
            "entry_count": entries, "pnl_pair_count": pnl_pairs,
            "wins": wins, "losses": losses, "win_rate": round(wr, 2),
            "wall_min": wall_min,
        }
        (ROOT / variant_name / "summary.json").write_text(json.dumps(summary, indent=2))
        (ROOT / variant_name / "raw_results.json").write_text(json.dumps(results, indent=2))
        state["variants"].append(summary)
        write_progress(state)
        print(f"=== DONE {variant_name}: ok={ok_count}/{len(dates)} pnl=${net_pnl:.2f} wr={wr:.1f}% wall={wall_min}min ===", flush=True)

    state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    state["total_wall_min"] = round((time.time() - overall_start) / 60, 1)
    write_progress(state)
    FINAL.write_text(json.dumps(state, indent=2))
    print(f"\n=== ALL DONE in {state['total_wall_min']} min ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
