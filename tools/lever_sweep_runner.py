#!/usr/bin/env python3
"""Portable lever-sweep runner.

Drop-in replacement for the workspace-local runner at
/home/user/workspace/baseline_sweep/run_baseline_sweep.py, designed to
run inside CI / GitHub Actions where the corpus is already at the repo
root and the runner can't assume any external workspace path.

Differences from the local runner:
  - Reads corpus from <repo>/data (no /home/user/workspace dependency).
  - Output goes to <cwd>/sweep_workspace/<VID>/per_day/*.json + summary.json
    (cwd is the runner step's working directory in CI).
  - Slot dirs go to /tmp/sweep_isolate/<vid>_slot<N>/ (CI-friendly).
  - All the same env knobs work: VID, DATES_STRIDE, MAX_DATES,
    WARMUP_ENABLED, V15_FLAGS_ENABLED, plus any V73x/V74x/V75x/V77x/V78x
    or PROD_BASE override the workflow injects.

Output schema (per-variant summary.json) matches the local runner so
the same aggregation tooling reads both.
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

# Discover repo root: assume runner is at <repo>/tools/lever_sweep_runner.py
REPO = Path(__file__).resolve().parents[1]
BARS = REPO / "data"
ROOT = Path(os.environ.get("SWEEP_OUTPUT_ROOT",
                           str(Path.cwd() / "sweep_workspace")))
ISOLATE = Path(os.environ.get("SWEEP_ISOLATE_ROOT",
                              "/tmp/sweep_isolate"))
SHARED_BAR_CACHE = ISOLATE / "_shared_bar_cache"
PYTHON = sys.executable

WORKERS = int(os.environ.get("SWEEP_WORKERS", "3"))
VID = os.environ.get("VID", "default_sweep")
WARMUP_ENABLED = os.environ.get("WARMUP_ENABLED", "0") == "1"
WARMUP_DATE_PREFIXES = ("2025-11-", "2025-12-")

PER_DAY = ROOT / VID / "per_day"
SUMMARY = ROOT / VID / "summary.json"

PROD_BASE = {
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
}

V15_FULL_FLAGS = {
    "V15_HARD_STRIKE_CAP": "1",
    "V15_SCALED_DI_FLOOR_ENABLED": "1",
    "V15_SCALED_DI_FLOOR": "25.0",
    "V15_REQUIRE_5M_ADX_20": "1",
    "V15_MOMENTUM_ADX_5M_MIN": "20.0",
    "V15_ALARM_E_POST_ENABLED": "1",
}

V15_FLAGS_ENABLED = os.environ.get("V15_FLAGS_ENABLED", "1") == "1"
DATES_STRIDE = int(os.environ.get("DATES_STRIDE", "1"))
MAX_DATES = int(os.environ.get("MAX_DATES", "0"))

TICKERS_12 = ["AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOG", "AMZN",
              "AVGO", "NFLX", "ORCL", "SPY", "QQQ"]


def discover_dates() -> list[str]:
    out = []
    for p in sorted(BARS.iterdir()):
        if not p.is_dir() or not p.name.startswith("2026-"):
            continue
        if all((p / f"{t}.jsonl").exists() for t in TICKERS_12):
            out.append(p.name)
    return out


def seed_warmup(slot_dir: Path) -> int:
    bar_archive = slot_dir / "bars"
    bar_archive.mkdir(parents=True, exist_ok=True)
    seeded = 0
    for src in sorted(BARS.iterdir()):
        if not src.is_dir():
            continue
        if not any(src.name.startswith(p) for p in WARMUP_DATE_PREFIXES):
            continue
        dst = bar_archive / src.name
        if dst.exists():
            continue
        shutil.copytree(src, dst)
        seeded += 1
    return seeded


def write_tickers_json(slot_dir: Path, roster: list[str]) -> Path:
    p = slot_dir / "tickers.json"
    p.write_text(json.dumps({
        "tickers": list(roster),
        "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "bot_version": "7.7.2-experimental",
    }, indent=2))
    return p


def build_env(slot_dir: Path, extras: dict, roster: list[str]) -> dict:
    bar_archive = slot_dir / "bars"
    or_dir = slot_dir / "or"
    forensics = slot_dir / "forensics"
    volprof = slot_dir / "volume_profile"
    for d in (slot_dir, bar_archive, or_dir, forensics, volprof, SHARED_BAR_CACHE):
        d.mkdir(parents=True, exist_ok=True)
    for stale in (
        slot_dir / "paper_state.json",
        slot_dir / "state.db",
        slot_dir / "trade_log.jsonl",
        slot_dir / "paper_trade.log",
    ):
        if stale.exists():
            stale.unlink()
    write_tickers_json(slot_dir, roster)
    env = {
        **os.environ,
        "TG_DATA_ROOT": str(slot_dir),
        "SSM_BAR_CACHE_DIR": str(SHARED_BAR_CACHE),
        "STATE_DB_PATH": str(slot_dir / "state.db"),
        "BAR_ARCHIVE_BASE": str(bar_archive),
        "UNIVERSE_GUARD_PATH": str(slot_dir / "tickers.json"),
        "TICKERS_FILE": str(slot_dir / "tickers.json"),
        "TRADE_LOG_PATH": str(slot_dir / "trade_log.jsonl"),
        "OR_DIR": str(or_dir),
        "FORENSICS_DIR": str(forensics),
        "INGEST_AUDIT_DB_PATH": str(slot_dir / "ingest_audit.db"),
        "VOLUME_PROFILE_DIR": str(volprof),
        "SSM_SMOKE_TEST": "1",
        "TELEGRAM_BOT_TOKEN": "000:fake",
        "FMP_API_KEY": "sweep_dummy_key",
        "PAPER_STATE_PATH": str(slot_dir / "paper_state.json"),
        "PAPER_LOG_PATH": str(slot_dir / "paper_trade.log"),
        "LOG_LEVEL": os.environ.get("LOG_LEVEL", "WARNING"),
    }
    if WARMUP_ENABLED:
        env["BAR_ARCHIVE_RETAIN_DAYS"] = "9999"
    env.pop("TG_DYNAMIC_UNIVERSE_PATH", None)
    env.update(extras)
    return env


def run_one_day(args):
    date_str, slot_dir_str, extras, out_path_str, roster = args
    out_path = Path(out_path_str)
    env_dict = build_env(Path(slot_dir_str), extras, roster)
    edge_only = [t for t in roster if t not in ("SPY", "QQQ")]
    try:
        r = subprocess.run(
            [str(PYTHON), "-m", "backtest.replay_v511_full",
             "--date", date_str, "--bars-dir", str(BARS),
             "--output", str(out_path),
             "--tickers", ",".join(edge_only)],
            cwd=str(REPO), env=env_dict, capture_output=True, text=True,
            timeout=600,
        )
        ok = r.returncode == 0 and out_path.exists()
        result = {"date": date_str, "rc": r.returncode, "ok": ok}
        if not ok:
            result["stderr"] = (r.stderr or "")[-2000:]
        else:
            try:
                d = json.loads(out_path.read_text())
                summary = d.get("summary") or {}
                result["entries"] = summary.get("entries", 0)
                result["exits"] = summary.get("exits", 0)
                pp = d.get("pnl_pairs", [])
                result["pnl_pairs"] = len(pp)
                result["net_pnl"] = sum(p.get("pnl_dollars", p.get("pnl", 0)) for p in pp)
                result["wins"] = sum(1 for p in pp
                                     if p.get("pnl_dollars", p.get("pnl", 0)) > 0)
                result["losses"] = sum(1 for p in pp
                                       if p.get("pnl_dollars", p.get("pnl", 0)) <= 0)
                if not d.get("_clean_state_run"):
                    d["_clean_state_run"] = True
                    d["_universe_used"] = list(roster)
                    out_path.write_text(json.dumps(d, indent=2))
            except Exception as e:
                result["parse_err"] = str(e)
        return result
    except subprocess.TimeoutExpired:
        return {"date": date_str, "rc": -1, "ok": False, "stderr": "TIMEOUT"}
    except Exception as e:
        return {"date": date_str, "rc": -2, "ok": False, "stderr": str(e)}


def main() -> int:
    PER_DAY.mkdir(parents=True, exist_ok=True)
    extras = {**PROD_BASE, **(V15_FULL_FLAGS if V15_FLAGS_ENABLED else {})}
    # Env-set values win over PROD_BASE defaults.
    for k in list(extras.keys()):
        if k in os.environ:
            extras[k] = os.environ[k]

    slot_dirs = []
    for i in range(WORKERS):
        s = ISOLATE / f"{VID}_slot{i}"
        s.mkdir(parents=True, exist_ok=True)
        if WARMUP_ENABLED:
            n = seed_warmup(s)
            print(f"  {s.name}: warmup seeded {n} pre-corpus dirs", flush=True)
        slot_dirs.append(str(s))

    dates = discover_dates()
    print(f"Discovered {len(dates)} dates", flush=True)
    if DATES_STRIDE > 1:
        dates = dates[::DATES_STRIDE]
        print(f"  STRIDE={DATES_STRIDE} -> {len(dates)} dates", flush=True)
    if MAX_DATES > 0 and len(dates) > MAX_DATES:
        dates = dates[:MAX_DATES]
        print(f"  MAX_DATES={MAX_DATES} -> capped at {len(dates)}", flush=True)

    tasks = []
    skipped = 0
    for i, d in enumerate(dates):
        out = PER_DAY / f"{d}.json"
        if out.exists():
            try:
                j = json.loads(out.read_text())
                summ = j.get("summary") or {}
                if j.get("_clean_state_run") and summ.get("entries") is not None:
                    skipped += 1
                    continue
            except Exception:
                pass
        tasks.append((d, slot_dirs[i % WORKERS], extras, str(out), list(TICKERS_12)))
    if skipped:
        print(f"Resume: skipping {skipped} already-completed days", flush=True)
    print(f"Running {len(tasks)} days with {WORKERS} workers", flush=True)

    v_start = time.time()
    results = []
    with cf.ProcessPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(run_one_day, t): t[0] for t in tasks}
        for fut in cf.as_completed(futures):
            r = fut.result()
            results.append(r)
            if not r["ok"]:
                print(f"FAIL {r['date']} rc={r['rc']} | {(r.get('stderr') or '')[-200:]}", flush=True)
            done = len(results)
            if done % 5 == 0 or done == len(tasks):
                ok_so_far = sum(1 for x in results if x["ok"])
                pnl_so_far = sum(x.get("net_pnl", 0) for x in results if x["ok"])
                elapsed = (time.time() - v_start) / 60
                print(f"PROG {done}/{len(tasks)} ok={ok_so_far} "
                      f"pnl={pnl_so_far:.2f} elapsed={elapsed:.1f}min", flush=True)

    ok_results = [r for r in results if r["ok"]]
    total_pnl = sum(r.get("net_pnl", 0) for r in ok_results)
    total_entries = sum(r.get("entries", 0) for r in ok_results)
    total_exits = sum(r.get("exits", 0) for r in ok_results)
    total_wins = sum(r.get("wins", 0) for r in ok_results)
    total_losses = sum(r.get("losses", 0) for r in ok_results)
    closed = total_wins + total_losses
    summary = {
        "variant": VID,
        "universe": TICKERS_12,
        "earnings_layer": "none",
        "days_planned": len(dates),
        "days_ran": len(tasks),
        "days_resumed_skip": skipped,
        "days_ok": len(ok_results),
        "days_failed": len(results) - len(ok_results),
        "net_pnl": round(total_pnl, 2),
        "entries": total_entries,
        "exits": total_exits,
        "wins": total_wins,
        "losses": total_losses,
        "win_rate_pct": round(100 * total_wins / closed, 2) if closed else None,
        "wall_min": round((time.time() - v_start) / 60, 1),
        "config": {**extras, "v15_flags_enabled": V15_FLAGS_ENABLED},
    }
    SUMMARY.write_text(json.dumps(summary, indent=2))
    print("\n=== SUMMARY ===", flush=True)
    print(json.dumps(summary, indent=2), flush=True)
    for s in slot_dirs:
        try:
            shutil.rmtree(s, ignore_errors=True)
        except Exception:
            pass
    return 0 if not [r for r in results if not r["ok"]] else 1


if __name__ == "__main__":
    raise SystemExit(main())
