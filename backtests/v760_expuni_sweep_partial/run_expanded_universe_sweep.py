#!/usr/bin/env python3
"""12-variant 83-day sweep: expanded static universe x earnings overlay.

Universe layer (4):
  static_12      -- prod baseline
  static_28      -- 28 expansion (10 mega-cap + 18 expansion)
  static_30      -- 28 + SPY + QQQ
  static_top10   -- top 10 by P/L from static_30 (Phase 2, after Phase 1)

Earnings overlay (3):
  none           -- no guard (baseline)
  blackout       -- block release-day + open-after entries
  blackout_dampen-- above + 0.5x sizing within +/- 5 trading days

Phase 1: 9 variants (static_12 / static_28 / static_30 x 3 layers)
Phase 2: 3 variants (static_top10 x 3 layers) -- universe derived after Phase 1.

WORKERS=3 (compresses ~75min wall to ~50min).
"""
from __future__ import annotations
import concurrent.futures as cf
import json
import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import date as Date
from pathlib import Path

REPO = "/tmp/ssm_v661"
BARS = "/home/user/workspace/canonical_backtest_data_v707/replay_layout"
ROOT = Path("/home/user/workspace/v760_dynuni_sweep")
ISOLATE = Path("/tmp/v760_expuni_isolate")
SHARED_BAR_CACHE = ISOLATE / "_shared_bar_cache"
WARMUP_SRC = Path("/home/user/workspace/v6_15_6_warmup_data/bars")
DATES_FILE = Path("/home/user/workspace/canonical_backtest_data_v707/days_84.txt")
EARNINGS_FIXTURE = ROOT / "earnings_fixture.json"

PROGRESS = ROOT / "EXPUNI_PROGRESS.json"
FINAL = ROOT / "EXPUNI_FINAL.json"
WORKERS = 3

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

# Universe rosters
TICKERS_12 = ["AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOG", "AMZN",
              "AVGO", "NFLX", "ORCL", "SPY", "QQQ"]
TICKERS_28_EDGE = ["AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOG", "AMZN",
                   "AVGO", "NFLX", "ORCL", "AMD", "CRM", "ADBE", "INTC",
                   "QCOM", "MU", "PLTR", "COIN", "SHOP", "UBER", "ABNB",
                   "SNOW", "DDOG", "JPM", "BAC", "WMT", "DIS", "BA"]
TICKERS_30 = TICKERS_28_EDGE + ["SPY", "QQQ"]

UNIVERSE_ROSTERS = {
    "static_12": TICKERS_12,
    "static_28": TICKERS_28_EDGE,
    "static_30": TICKERS_30,
    # static_top10 is filled in Phase 2 from Phase 1 results
}

EARNINGS_LAYERS = {
    "none": {},
    "blackout": {
        "TG_EARNINGS_BLACKOUT": "1",
        "TG_EARNINGS_FIXTURE_PATH": str(EARNINGS_FIXTURE),
    },
    "blackout_dampen": {
        "TG_EARNINGS_BLACKOUT": "1",
        "TG_EARNINGS_DAMPEN": "1",
        "TG_EARNINGS_DAMPEN_WINDOW": "5",
        "TG_EARNINGS_DAMPEN_SCALE": "0.5",
        "TG_EARNINGS_FIXTURE_PATH": str(EARNINGS_FIXTURE),
    },
}


def variant_id(uni: str, layer: str) -> str:
    return f"{uni}__{layer}"


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


def write_tickers_json(slot_dir: Path, roster: list[str]) -> Path:
    """Write the slot's tickers.json so trade_genius _load_tickers_file picks it up."""
    p = slot_dir / "tickers.json"
    payload = {
        "tickers": list(roster),
        "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "bot_version": "7.6.0",
    }
    p.write_text(json.dumps(payload, indent=2))
    return p


def build_env(slot_dir: Path, extras: dict, roster: list[str]) -> dict:
    tg_root = slot_dir
    bar_archive = tg_root / "bars"
    or_dir = tg_root / "or"
    forensics = tg_root / "forensics"
    volprof = tg_root / "volume_profile"
    for d in (tg_root, bar_archive, or_dir, forensics, volprof,
              SHARED_BAR_CACHE):
        d.mkdir(parents=True, exist_ok=True)
    for stale in (
        tg_root / "paper_state.json",
        tg_root / "state.db",
        tg_root / "trade_log.jsonl",
        tg_root / "paper_trade.log",
    ):
        if stale.exists():
            stale.unlink()
    write_tickers_json(tg_root, roster)
    env = {
        **os.environ,
        "TG_DATA_ROOT": str(tg_root),
        "SSM_BAR_CACHE_DIR": str(SHARED_BAR_CACHE),
        "STATE_DB_PATH": str(tg_root / "state.db"),
        "BAR_ARCHIVE_BASE": str(bar_archive),
        "UNIVERSE_GUARD_PATH": str(tg_root / "tickers.json"),
        "TICKERS_FILE": str(tg_root / "tickers.json"),
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
    # Make sure no stale dynamic-universe override leaks in.
    env.pop("TG_DYNAMIC_UNIVERSE_PATH", None)
    env.update(extras)
    return env


def run_one_day(args):
    date_str, slot_dir_str, extras, out_path_str, roster = args
    out_path = Path(out_path_str)
    env_dict = build_env(Path(slot_dir_str), extras, roster)
    # The replay loads bars only for the explicit --tickers list (+SPY/QQQ
    # auto-appended). Pass the roster so all tickers in the universe load
    # bar data; otherwise OR collection burns 60-180s/day on retries.
    edge_only = [t for t in roster if t not in ("SPY", "QQQ")]
    try:
        r = subprocess.run(
            [sys.executable, "-m", "backtest.replay_v511_full",
             "--date", date_str, "--bars-dir", BARS, "--output", str(out_path),
             "--tickers", ",".join(edge_only)],
            cwd=REPO, env=env_dict, capture_output=True, text=True,
            timeout=300,
        )
        ok = r.returncode == 0 and out_path.exists()
        result = {"date": date_str, "rc": r.returncode, "ok": ok}
        if not ok:
            result["stderr"] = (r.stderr or "")[-2000:]
            result["stdout"] = (r.stdout or "")[-500:]
        elif out_path.exists():
            try:
                d = json.loads(out_path.read_text())
                summary = d.get("summary") or {}
                result["entries"] = (summary.get("entries", 0)
                                     or len(d.get("entries", [])))
                result["exits"] = (summary.get("exits", 0)
                                   or len(d.get("exits", [])))
                pp = d.get("pnl_pairs", [])
                result["pnl_pairs"] = len(pp)
                result["net_pnl"] = sum(p.get("pnl_dollars", p.get("pnl", 0))
                                        for p in pp)
                result["wins"] = sum(1 for p in pp
                                     if p.get("pnl_dollars",
                                              p.get("pnl", 0)) > 0)
                result["losses"] = sum(1 for p in pp
                                       if p.get("pnl_dollars",
                                                p.get("pnl", 0)) <= 0)
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


def run_variant(uni_name: str, layer_name: str, dates_str: list[str],
                roster: list[str]) -> dict:
    vid = variant_id(uni_name, layer_name)
    v_start = time.time()
    print(f"\n=== START {vid} ===", flush=True)
    out_root = ROOT / vid
    per_day_dir = out_root / "per_day"
    per_day_dir.mkdir(parents=True, exist_ok=True)

    layer_extras = EARNINGS_LAYERS[layer_name]
    extras = {**PROD_BASE, **V15_FULL_FLAGS, **layer_extras}

    slot_dirs = []
    for i in range(WORKERS):
        s = ISOLATE / f"{vid}_slot{i}"
        s.mkdir(parents=True, exist_ok=True)
        seeded = seed_warmup(s)
        if seeded:
            print(f"  {s.name}: warmup seeded {seeded} dirs", flush=True)
        slot_dirs.append(str(s))

    tasks = []
    skipped = 0
    for i, d in enumerate(dates_str):
        out = per_day_dir / f"{d}.json"
        if out.exists():
            try:
                j = json.loads(out.read_text())
                summ = j.get("summary") or {}
                if (j.get("_clean_state_run")
                        and summ.get("entries") is not None):
                    skipped += 1
                    continue
            except Exception:
                pass
        tasks.append((d, slot_dirs[i % WORKERS], extras, str(out), list(roster)))
    if skipped:
        print(f"  Resuming: skipping {skipped} already-completed days",
              flush=True)
    print(f"  Running {len(tasks)} days with {WORKERS} workers...",
          flush=True)

    results = []
    empty_streak = 0
    with cf.ProcessPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(run_one_day, t): t[0] for t in tasks}
        for fut in cf.as_completed(futures):
            r = fut.result()
            results.append(r)
            if r["ok"]:
                empty_streak = 0
            else:
                empty_streak += 1
                print(f"  FAIL {r['date']} rc={r['rc']} | "
                      f"{(r.get('stderr') or '')[-200:]}", flush=True)
            done = len(results)
            if done % 10 == 0 or done == len(tasks):
                ok_so_far = sum(1 for x in results if x["ok"])
                pnl_so_far = sum(x.get("net_pnl", 0)
                                 for x in results if x["ok"])
                elapsed = (time.time() - v_start) / 60
                print(f"  PROG {done}/{len(tasks)} ok={ok_so_far} "
                      f"pnl={pnl_so_far:.2f} elapsed={elapsed:.1f}min",
                      flush=True)
            if empty_streak > 12:
                print(f"  ABORT empty_streak={empty_streak}", flush=True)
                for f in futures:
                    f.cancel()
                break

    ok_count = sum(1 for x in results if x["ok"])
    net_pnl = sum(x.get("net_pnl", 0) for x in results if x["ok"])
    wins = sum(x.get("wins", 0) for x in results if x["ok"])
    losses = sum(x.get("losses", 0) for x in results if x["ok"])
    wr = (wins / max(1, wins + losses)) * 100
    wall_min = round((time.time() - v_start) / 60, 1)
    summary = {
        "variant": vid,
        "universe": uni_name,
        "earnings_layer": layer_name,
        "roster_size": len(roster),
        "extras_layer": layer_extras,
        "days_total": len(dates_str), "days_run": len(tasks),
        "days_ok": ok_count,
        "net_pnl_84d": round(net_pnl, 2),
        "wins": wins, "losses": losses, "win_rate": round(wr, 2),
        "wall_min": wall_min,
    }
    (out_root / "summary.json").write_text(json.dumps(summary, indent=2))
    # Free tmpfs: variant slots aren't reused across variants.
    for s in slot_dirs:
        try:
            shutil.rmtree(s, ignore_errors=True)
        except Exception:
            pass
    print(f"=== DONE {vid}: ok={ok_count}/{len(dates_str)} "
          f"pnl={net_pnl:.2f} wr={wr:.1f}% wall={wall_min}min ===",
          flush=True)
    return summary


def compute_top10_from_static30(dates_str: list[str]) -> list[str]:
    """Read static_30__none per_day outputs, aggregate per-ticker P/L,
    return top 10 by net P/L. Always include SPY + QQQ at minimum.
    """
    per_day = ROOT / "static_30__none" / "per_day"
    pnl_by_ticker: dict[str, float] = defaultdict(float)
    for d in dates_str:
        f = per_day / f"{d}.json"
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        for p in data.get("pnl_pairs", []):
            t = p.get("ticker") or p.get("symbol")
            if not t:
                continue
            pnl_by_ticker[t] += p.get("pnl_dollars", p.get("pnl", 0)) or 0
    if not pnl_by_ticker:
        # Fallback: prod 12 minus SPY/QQQ (top10 mega-cap).
        return TICKERS_12
    ranked = sorted(pnl_by_ticker.items(), key=lambda x: -x[1])
    edges = [t for t, _ in ranked if t not in ("SPY", "QQQ")]
    top10 = edges[:8] + ["SPY", "QQQ"]
    print(f"  static_top10 picked from static_30 P/L ranking: {top10}",
          flush=True)
    print(f"    full ranking: " + ", ".join(f"{t}:{p:+.0f}"
                                            for t, p in ranked[:15]),
          flush=True)
    return top10


def main() -> int:
    ROOT.mkdir(parents=True, exist_ok=True)
    ISOLATE.mkdir(parents=True, exist_ok=True)
    SHARED_BAR_CACHE.mkdir(parents=True, exist_ok=True)
    if not EARNINGS_FIXTURE.exists():
        raise SystemExit(f"Missing earnings fixture at {EARNINGS_FIXTURE}")

    dates_str = [d.strip() for d in DATES_FILE.read_text().splitlines()
                 if d.strip()]
    print(f"Loaded {len(dates_str)} dates", flush=True)

    state = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
             "variants": []}
    PROGRESS.write_text(json.dumps(state, indent=2))
    overall_start = time.time()

    # PHASE 1: 9 variants
    phase1 = [
        (uni, layer)
        for uni in ("static_12", "static_28", "static_30")
        for layer in ("none", "blackout", "blackout_dampen")
    ]
    for uni, layer in phase1:
        roster = UNIVERSE_ROSTERS[uni]
        s = run_variant(uni, layer, dates_str, roster)
        state["variants"].append(s)
        PROGRESS.write_text(json.dumps(state, indent=2))

    # PHASE 2: derive top10, run 3 variants
    print("\n=== PHASE 2: deriving static_top10 from static_30 P/L ranking ===",
          flush=True)
    top10 = compute_top10_from_static30(dates_str)
    UNIVERSE_ROSTERS["static_top10"] = top10
    (ROOT / "static_top10_roster.json").write_text(
        json.dumps({"roster": top10}, indent=2))
    for layer in ("none", "blackout", "blackout_dampen"):
        s = run_variant("static_top10", layer, dates_str, top10)
        state["variants"].append(s)
        PROGRESS.write_text(json.dumps(state, indent=2))

    state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    state["total_wall_min"] = round((time.time() - overall_start) / 60, 1)
    FINAL.write_text(json.dumps(state, indent=2))
    print(f"\n=== ALL DONE in {state['total_wall_min']}min ===",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
