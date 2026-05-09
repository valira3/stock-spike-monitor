#!/usr/bin/env python3
"""v7.3.0-experimental — Regime-C entry suppression 84-day backtest.

Two variants on the v7.0.7 SIP corpus (83 days, 12 prod tickers):
  1. baseline   — current prod settings, regime-C entries fire as today
  2. skip_c     — V730_SKIP_REGIME_C=1, broker.orders blocks entries on regime C days

Production settings as of 2026-05-07 (re-queried Railway):
  POST_LOSS_COOLDOWN_MIN_LONG=30
  POST_LOSS_COOLDOWN_MIN_SHORT=30
  VOLUME_GATE_ENABLED=true
  VOLUME_BUCKET_THRESHOLD_RATIO=0.85

Expected wall: ~16-22 min (two variants x ~8-11 min each).
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
ROOT = Path("/home/user/workspace/v730_regime_c_skip_backtest")
ISOLATE = Path("/tmp/v730_regime_c_isolate")
SHARED_BAR_CACHE = ISOLATE / "_shared_bar_cache"
WARMUP_SRC = Path("/home/user/workspace/v6_15_6_warmup_data/bars")
DATES_FILE = Path("/home/user/workspace/canonical_backtest_data_v707/days_84.txt")
PROGRESS = ROOT / "PROGRESS.json"
FINAL = ROOT / "FINAL.json"
WORKERS = 2

# Live production settings (re-queried Railway 2026-05-07).
PROD_BASE = {
    "POST_LOSS_COOLDOWN_MIN": "30",
    "POST_LOSS_COOLDOWN_MIN_LONG": "30",
    "POST_LOSS_COOLDOWN_MIN_SHORT": "30",
    "VOLUME_GATE_ENABLED": "true",
    "VOLUME_BUCKET_THRESHOLD_RATIO": "0.85",
    "POST_EXIT_SAME_TICKER_COOLDOWN_SEC": "1",
}

VARIANTS: list[tuple[str, dict, str]] = [
    ("baseline", {**PROD_BASE}, "baseline"),
    # skip_c uses an offline regime classifier (replay's tick path doesn't
    # capture 09:30 anchor reliably): runner pre-classifies each day from
    # corpus SPY bars and writes a synthetic empty result for regime-C days.
    ("skip_c", {**PROD_BASE}, "skipc"),
]


def classify_regime_offline(date: str) -> str | None:
    """Classify SPY 09:30->10:00 regime using corpus bars.

    Returns 'A'/'B'/'C'/'D'/'E' or None if anchors missing.
    Mirrors spy_regime.SpyRegime._classify with default boundaries
    (-0.50% / -0.15% lower/upper).
    """
    spy_path = Path(BARS) / date / "SPY.jsonl"
    if not spy_path.exists():
        return None
    p_open = p_1000 = None
    for line in spy_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            b = json.loads(line)
        except Exception:
            continue
        bucket = b.get("et_bucket")
        if bucket == "0930" and p_open is None:
            p_open = b.get("close") or b.get("c")
        elif bucket == "1000" and p_1000 is None:
            p_1000 = b.get("close") or b.get("c")
            break
    if p_open is None or p_1000 is None or p_open <= 0:
        return None
    ret_pct = (p_1000 - p_open) / p_open * 100.0
    if ret_pct <= -0.50:
        return "A"
    if -0.50 < ret_pct < -0.15:
        return "B"
    if -0.15 <= ret_pct <= 0.15:
        return "C"
    if 0.15 < ret_pct <= 0.50:
        return "D"
    return "E"


def synth_skip_result(date: str, out_path: Path) -> dict:
    """Write a synthetic empty replay result for a regime-C-skipped day."""
    payload = {
        "date": date,
        "version": "v7.3.0-experimental",
        "minutes_processed": 0,
        "tickers": [],
        "entries": [],
        "exits": [],
        "orders": [],
        "cancellations": [],
        "closes_raw": [],
        "telegram_messages": [],
        "alerts": [],
        "errors": [],
        "pnl_pairs": [],
        "summary": {
            "entries": 0, "exits": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "total_pnl_per_share": 0.0,
            "pairs_missing_shares": 0,
        },
        "_clean_state_run": True,
        "_v730_regime_c_skipped": True,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    return {
        "date": date, "rc": 0, "ok": True,
        "entries": 0, "exits": 0, "pnl_pairs": 0,
        "net_pnl": 0.0, "wins": 0, "losses": 0,
        "version": "v7.3.0-experimental",
        "v730_skipped": True,
    }


def seed_warmup(slot_dir: Path) -> int:
    """Copy warmup bar dirs into the slot before launch (only if missing)."""
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


def run_one_day(args: tuple) -> dict:
    date, slot_dir_str, extras, out_path = args
    out_path = Path(out_path)
    env_dict = build_env(Path(slot_dir_str), extras)
    try:
        r = subprocess.run(
            [sys.executable, "-m", "backtest.replay_v511_full",
             "--date", date,
             "--bars-dir", BARS,
             "--output", str(out_path)],
            cwd=REPO,
            env=env_dict,
            capture_output=True,
            text=True,
            timeout=180,
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
                result["version"] = d.get("version", summary.get("version", "?"))
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


def write_progress(state: dict) -> None:
    PROGRESS.write_text(json.dumps(state, indent=2))


def main() -> int:
    ROOT.mkdir(parents=True, exist_ok=True)
    ISOLATE.mkdir(parents=True, exist_ok=True)
    SHARED_BAR_CACHE.mkdir(parents=True, exist_ok=True)

    dates = [d.strip() for d in DATES_FILE.read_text().splitlines() if d.strip()]
    print(f"Loaded {len(dates)} dates: {dates[0]} -> {dates[-1]}", flush=True)

    state: dict = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "version": "v7.3.0-experimental",
        "corpus": "v707_83d_sip",
        "dates_count": len(dates),
        "variants": [],
        "current_variant": None,
    }
    write_progress(state)
    overall_start = time.time()

    for variant_name, extras, isolate_subdir in VARIANTS:
        v_start = time.time()
        state["current_variant"] = variant_name
        write_progress(state)
        print(f"\n=== START {variant_name} extras={extras} ===", flush=True)

        out_root = ROOT / variant_name
        per_day_dir = out_root / "per_day"
        per_day_dir.mkdir(parents=True, exist_ok=True)

        slot_dirs = []
        for i in range(WORKERS):
            s = ISOLATE / f"{isolate_subdir}_slot{i}"
            s.mkdir(parents=True, exist_ok=True)
            seeded = seed_warmup(s)
            print(f"  {s.name}: warmup seeded {seeded} date dirs", flush=True)
            slot_dirs.append(str(s))

        # Resume support + offline regime-C skip for skip_c variant
        is_skip_c = variant_name == "skip_c"
        tasks = []
        skipped = 0
        regime_c_skipped = 0
        synth_results = []
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
            # skip_c variant: classify offline, synth-empty for C days
            if is_skip_c and classify_regime_offline(d) == "C":
                synth_results.append(synth_skip_result(d, out))
                regime_c_skipped += 1
                continue
            slot = slot_dirs[i % WORKERS]
            tasks.append((d, slot, extras, out))
        if is_skip_c and regime_c_skipped:
            print(f"  Regime-C synth-skipped: {regime_c_skipped} days", flush=True)
        if skipped:
            print(f"  Resuming: skipping {skipped} already-completed days", flush=True)
        print(f"  Running {len(tasks)} days with {WORKERS} workers...", flush=True)

        results = list(synth_results) if is_skip_c else []
        empty_streak = 0
        aborted = False

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
                    print(f"  FAIL {r['date']} rc={r['rc']} streak={empty_streak} | {r.get('stderr','')[-200:]}", flush=True)

                done = len(results)
                if done % 5 == 0 or done == len(tasks):
                    ok_so_far = sum(1 for x in results if x["ok"])
                    pnl_so_far = sum(x.get("net_pnl", 0) for x in results if x["ok"])
                    elapsed = (time.time() - v_start) / 60
                    print(f"  PROGRESS {done}/{len(tasks)} ok={ok_so_far} pnl={pnl_so_far:.2f} elapsed={elapsed:.1f}min", flush=True)
                    state["progress"] = {"variant": variant_name, "done": done, "total": len(tasks), "ok": ok_so_far, "pnl": round(pnl_so_far, 2), "elapsed_min": round(elapsed, 1)}
                    write_progress(state)
                if empty_streak > 5:
                    print(f"  ABORT: empty_streak={empty_streak} > 5", flush=True)
                    for f in futures:
                        f.cancel()
                    aborted = True
                    break

        if aborted:
            state["aborted"] = True
            write_progress(state)
            return 1

        # Merge resumed days
        run_dates = {r["date"] for r in results}
        for d in dates:
            if d in run_dates:
                continue
            out = per_day_dir / f"{d}.json"
            if not out.exists():
                continue
            try:
                jd = json.loads(out.read_text())
                summ = jd.get("summary") or {}
                pp = jd.get("pnl_pairs", [])
                results.append({
                    "date": d, "rc": 0, "ok": True,
                    "entries": summ.get("entries", 0) or len(jd.get("entries", [])),
                    "exits": summ.get("exits", 0) or len(jd.get("exits", [])),
                    "pnl_pairs": len(pp),
                    "net_pnl": sum(p.get("pnl_dollars", p.get("pnl", 0)) for p in pp),
                    "wins": sum(1 for p in pp if p.get("pnl_dollars", p.get("pnl", 0)) > 0),
                    "losses": sum(1 for p in pp if p.get("pnl_dollars", p.get("pnl", 0)) <= 0),
                    "version": jd.get("version", summ.get("version", "?")),
                    "resumed": True,
                })
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
            "variant": variant_name,
            "version": "v7.3.0-experimental",
            "corpus": "v707_83d_sip",
            "extras": extras,
            "days_total": len(dates),
            "days_run": len(tasks),
            "days_ok": ok_count,
            "net_pnl_84d": round(net_pnl, 2),
            "entry_count": entries,
            "pnl_pair_count": pnl_pairs,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wr, 2),
            "wall_min": wall_min,
        }
        (out_root / "summary.json").write_text(json.dumps(summary, indent=2))
        (out_root / "raw_results.json").write_text(json.dumps(results, indent=2))
        state["variants"].append(summary)
        write_progress(state)
        print(f"\n=== DONE {variant_name}: ok={ok_count}/{len(dates)} pnl={net_pnl:.2f} wr={wr:.1f}% wall={wall_min}min ===", flush=True)

    state["current_variant"] = None
    state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    state["total_wall_min"] = round((time.time() - overall_start) / 60, 1)
    write_progress(state)
    FINAL.write_text(json.dumps(state, indent=2))
    print(f"\n=== ALL DONE in {state['total_wall_min']} min ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
