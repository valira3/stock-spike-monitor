#!/usr/bin/env python3
"""v15.0.0-experimental Tiger Sovereign 83-day SIP sweep.

Two variants on the v7.0.7 SIP corpus (83 trading days, 12 prod tickers):
  v15_full     -- all 4 v15 flags ON (default in this fork)
  v15_baseline -- all 4 v15 flags OFF (mimics v7.4.0 main behaviour)

The baseline mirrors v7.4.0 production: V7.0.2 recursive strike unlock,
1m DI floor 22.0, no 5m ADX gate, ALARM_E_ENABLED=False. Comparison
quantifies the spec-conformance cost in P/L over a full corpus.
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
ROOT = Path("/home/user/workspace/v15_84day_sweep")
ISOLATE = Path("/tmp/v15_isolate")
SHARED_BAR_CACHE = ISOLATE / "_shared_bar_cache"
WARMUP_SRC = Path("/home/user/workspace/v6_15_6_warmup_data/bars")
DATES_FILE = Path("/home/user/workspace/canonical_backtest_data_v707/days_84.txt")
PROGRESS = ROOT / "PROGRESS.json"
FINAL = ROOT / "FINAL.json"
WORKERS = 2

PROD_BASE = {
    "POST_LOSS_COOLDOWN_MIN": "30",
    "POST_LOSS_COOLDOWN_MIN_LONG": "30",
    "POST_LOSS_COOLDOWN_MIN_SHORT": "30",
    "VOLUME_GATE_ENABLED": "false",
    # v7.4.0 production knobs (already on main)
    "V730_STOP_HYSTERESIS_ENABLED": "1",
    "V730_STOP_HYSTERESIS_BARS": "2",
    "V730_STOP_DEEP_FRAC": "0.0075",
    "V740_MFE_RATCHET_ENABLED": "1",
    "V740_MFE_RATCHET_ARM_R": "1.0",
    "V740_MFE_RATCHET_FRAC": "0.5",
}

# v15 spec: hard cap, scaled DI floor 25, 5m ADX > 20, Alarm E post on.
V15_FULL_FLAGS = {
    "V15_HARD_STRIKE_CAP": "1",
    "V15_SCALED_DI_FLOOR_ENABLED": "1",
    "V15_SCALED_DI_FLOOR": "25.0",
    "V15_REQUIRE_5M_ADX_20": "1",
    "V15_MOMENTUM_ADX_5M_MIN": "20.0",
    "V15_ALARM_E_POST_ENABLED": "1",
}
# Mirror v7.4.0 main behaviour by disabling every v15 flag.
V15_BASELINE_FLAGS = {
    "V15_HARD_STRIKE_CAP": "0",
    "V15_SCALED_DI_FLOOR_ENABLED": "0",
    "V15_REQUIRE_5M_ADX_20": "0",
    "V15_ALARM_E_POST_ENABLED": "0",
}

VARIANTS = [
    ("v15_full", {**PROD_BASE, **V15_FULL_FLAGS}, "v15_full"),
    ("v15_baseline", {**PROD_BASE, **V15_BASELINE_FLAGS}, "v15_baseline"),
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
        shutil.copytree(src, dst)
        seeded += 1
    return seeded


def build_env(slot_dir: Path, extras: dict) -> dict:
    tg_root = slot_dir
    bar_archive = tg_root / "bars"
    or_dir = tg_root / "or"
    forensics = tg_root / "forensics"
    volprof = tg_root / "volume_profile"
    for d in (tg_root, bar_archive, or_dir, forensics, volprof, SHARED_BAR_CACHE):
        d.mkdir(parents=True, exist_ok=True)
    for stale in (
        tg_root / "paper_state.json",
        tg_root / "state.db",
        tg_root / "trade_log.jsonl",
        tg_root / "paper_trade.log",
    ):
        if stale.exists():
            stale.unlink()
    env = {
        **os.environ,
        "TG_DATA_ROOT": str(tg_root),
        "SSM_BAR_CACHE_DIR": str(SHARED_BAR_CACHE),
        "STATE_DB_PATH": str(tg_root / "state.db"),
        "BAR_ARCHIVE_BASE": str(bar_archive),
        "UNIVERSE_GUARD_PATH": str(tg_root / "tickers.json"),
        "TRADE_LOG_PATH": str(tg_root / "trade_log.jsonl"),
        "OR_DIR": str(or_dir),
        "FORENSICS_DIR": str(forensics),
        "INGEST_AUDIT_DB_PATH": str(tg_root / "ingest_audit.db"),
        "VOLUME_PROFILE_DIR": str(volprof),
        "SSM_SMOKE_TEST": "1",
        "TELEGRAM_BOT_TOKEN": "000:fake",
        "FMP_API_KEY": "sweep_dummy_key",
        "PAPER_STATE_PATH": str(tg_root / "paper_state.json"),
        "PAPER_LOG_PATH": str(tg_root / "paper_trade.log"),
        "LOG_LEVEL": "WARNING",
    }
    env.update(extras)
    return env


def run_one_day(args):
    date, slot_dir_str, extras, out_path = args
    out_path = Path(out_path)
    env_dict = build_env(Path(slot_dir_str), extras)
    try:
        r = subprocess.run(
            [sys.executable, "-m", "backtest.replay_v511_full",
             "--date", date, "--bars-dir", BARS, "--output", str(out_path)],
            cwd=REPO, env=env_dict, capture_output=True, text=True, timeout=180,
        )
        ok = r.returncode == 0 and out_path.exists()
        result = {"date": date, "rc": r.returncode, "ok": ok}
        if not ok:
            result["stderr"] = r.stderr[-2000:]
            result["stdout"] = r.stdout[-500:]
        elif out_path.exists():
            try:
                d = json.loads(out_path.read_text())
                summary = d.get("summary") or {}
                result["entries"] = summary.get("entries", 0) or len(d.get("entries", []))
                result["exits"] = summary.get("exits", 0) or len(d.get("exits", []))
                pp = d.get("pnl_pairs", [])
                result["pnl_pairs"] = len(pp)
                result["net_pnl"] = sum(p.get("pnl_dollars", p.get("pnl", 0)) for p in pp)
                result["wins"] = sum(1 for p in pp if p.get("pnl_dollars", p.get("pnl", 0)) > 0)
                result["losses"] = sum(1 for p in pp if p.get("pnl_dollars", p.get("pnl", 0)) <= 0)
                if not d.get("_clean_state_run"):
                    d["_clean_state_run"] = True
                    out_path.write_text(json.dumps(d, indent=2))
            except Exception as e:
                result["parse_err"] = str(e)
        return result
    except subprocess.TimeoutExpired:
        return {"date": date, "rc": -1, "ok": False, "stderr": "TIMEOUT"}
    except Exception as e:
        return {"date": date, "rc": -2, "ok": False, "stderr": str(e)}


def main() -> int:
    ROOT.mkdir(parents=True, exist_ok=True)
    ISOLATE.mkdir(parents=True, exist_ok=True)
    SHARED_BAR_CACHE.mkdir(parents=True, exist_ok=True)
    dates = [d.strip() for d in DATES_FILE.read_text().splitlines() if d.strip()]
    print(f"Loaded {len(dates)} dates", flush=True)
    state = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "variants": []}
    PROGRESS.write_text(json.dumps(state, indent=2))
    overall_start = time.time()

    for variant_name, extras, isolate_subdir in VARIANTS:
        v_start = time.time()
        print(f"\n=== START {variant_name} ===", flush=True)
        out_root = ROOT / variant_name
        per_day_dir = out_root / "per_day"
        per_day_dir.mkdir(parents=True, exist_ok=True)

        slot_dirs = []
        for i in range(WORKERS):
            s = ISOLATE / f"{isolate_subdir}_slot{i}"
            s.mkdir(parents=True, exist_ok=True)
            seeded = seed_warmup(s)
            print(f"  {s.name}: warmup seeded {seeded} dirs", flush=True)
            slot_dirs.append(str(s))

        tasks = []
        skipped = 0
        for i, d in enumerate(dates):
            out = per_day_dir / f"{d}.json"
            if out.exists():
                try:
                    j = json.loads(out.read_text())
                    summ = j.get("summary") or {}
                    if j.get("_clean_state_run") and summ.get("entries") is not None:
                        skipped += 1
                        continue
                except Exception:
                    pass
            tasks.append((d, slot_dirs[i % WORKERS], extras, out))
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
                    print(f"  OK  {r['date']} entries={r.get('entries',0)} pnl={r.get('net_pnl',0):.2f}", flush=True)
                else:
                    empty_streak += 1
                    print(f"  FAIL {r['date']} rc={r['rc']} | {r.get('stderr','')[-200:]}", flush=True)
                done = len(results)
                if done % 5 == 0 or done == len(tasks):
                    ok_so_far = sum(1 for x in results if x["ok"])
                    pnl_so_far = sum(x.get("net_pnl", 0) for x in results if x["ok"])
                    elapsed = (time.time() - v_start) / 60
                    print(f"  PROGRESS {done}/{len(tasks)} ok={ok_so_far} pnl={pnl_so_far:.2f} elapsed={elapsed:.1f}min", flush=True)
                if empty_streak > 5:
                    print(f"  ABORT empty_streak={empty_streak}", flush=True)
                    for f in futures: f.cancel()
                    return 1

        ok_count = sum(1 for x in results if x["ok"])
        net_pnl = sum(x.get("net_pnl", 0) for x in results if x["ok"])
        wins = sum(x.get("wins", 0) for x in results if x["ok"])
        losses = sum(x.get("losses", 0) for x in results if x["ok"])
        wr = (wins / max(1, wins + losses)) * 100
        wall_min = round((time.time() - v_start) / 60, 1)
        summary = {
            "variant": variant_name, "extras": extras,
            "days_total": len(dates), "days_run": len(tasks), "days_ok": ok_count,
            "net_pnl_84d": round(net_pnl, 2),
            "wins": wins, "losses": losses, "win_rate": round(wr, 2),
            "wall_min": wall_min,
        }
        (out_root / "summary.json").write_text(json.dumps(summary, indent=2))
        state["variants"].append(summary)
        PROGRESS.write_text(json.dumps(state, indent=2))
        print(f"\n=== DONE {variant_name}: ok={ok_count}/{len(dates)} pnl={net_pnl:.2f} wr={wr:.1f}% wall={wall_min}min ===", flush=True)

    state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    state["total_wall_min"] = round((time.time() - overall_start) / 60, 1)
    FINAL.write_text(json.dumps(state, indent=2))
    print(f"\n=== ALL DONE in {state['total_wall_min']}min ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
