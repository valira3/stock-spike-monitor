#!/usr/bin/env python3
"""Spot-check Arm B: P3_FULL_DI_THRESHOLD=25 vs baseline=30 on 3 days.

Compares FULL-tier conversion impact on a small representative sample
before committing to a full 83-day sweep.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = "/tmp/ssm_v661"
BARS = "/home/user/workspace/canonical_backtest_data_v707/replay_layout"
WARMUP = "/home/user/workspace/v6_15_6_warmup_data/bars"
OUT = Path("/home/user/workspace/v730_no_entry2_backtest/spot_B")
OUT.mkdir(parents=True, exist_ok=True)

DAYS = ["2026-02-03", "2026-02-17", "2026-03-31"]
ARMS = {
    "baseline": {"P3_FULL_DI_THRESHOLD": "30.0"},   # current prod default
    "armB":     {"P3_FULL_DI_THRESHOLD": "25.0"},   # FULL tier easier
}

# Production settings (re-queried 2026-05-04): L=30/S=30/VOL_GATE=true
PROD = {
    "POST_LOSS_COOLDOWN_MIN_LONG": "30",
    "POST_LOSS_COOLDOWN_MIN_SHORT": "30",
    "VOLUME_GATE_ENABLED": "true",
    "VOLUME_BUCKET_THRESHOLD_RATIO": "0.85",
    "POST_EXIT_SAME_TICKER_COOLDOWN_SEC": "1",
}


def _env(slot: Path, extras: dict) -> dict:
    return {
        **os.environ,
        "SSM_SMOKE_TEST": "1",
        "TELEGRAM_BOT_TOKEN": "000:fake",
        "FMP_API_KEY": "spot_dummy",
        "LOG_LEVEL": "WARNING",
        "TG_DATA_ROOT": str(slot),
        "STATE_DB_PATH": str(slot / "state.db"),
        "BAR_ARCHIVE_BASE": str(slot / "bars"),
        "OR_DIR": str(slot / "or"),
        "FORENSICS_DIR": str(slot / "forensics"),
        "VOLUME_PROFILE_DIR": str(slot / "volume_profile"),
        "INGEST_AUDIT_DB_PATH": str(slot / "ingest_audit.db"),
        "TRADE_LOG_PATH": str(slot / "trade_log.jsonl"),
        "UNIVERSE_GUARD_PATH": str(slot / "tickers.json"),
        "PAPER_STATE_PATH": str(slot / "paper_state.json"),
        "PAPER_LOG_PATH": str(slot / "paper_trade.log"),
        "SSM_BAR_CACHE_DIR": "/home/user/workspace/v730_no_entry2_backtest/spot_B/bar_cache",
        "ENTRY_2_DISABLED": "1",  # match clean baseline
        **PROD,
        **extras,
    }


def _seed_warmup(slot: Path):
    bars = slot / "bars"
    bars.mkdir(parents=True, exist_ok=True)
    n = 0
    for src in Path(WARMUP).iterdir():
        if not src.is_dir():
            continue
        dst = bars / src.name
        if dst.exists():
            continue
        shutil.copytree(src, dst)
        n += 1
    return n


def run_day(arm: str, date: str):
    slot = OUT / arm / "slot"
    slot.mkdir(parents=True, exist_ok=True)
    # Reset per-day state files
    for stale in ("paper_state.json", "state.db", "trade_log.jsonl", "paper_trade.log"):
        p = slot / stale
        if p.exists():
            p.unlink()
    # Seed warmup once per arm
    if not (slot / "bars").exists() or len(list((slot / "bars").iterdir())) < 50:
        seeded = _seed_warmup(slot)
        print(f"  [{arm}] seeded {seeded} warmup dirs")

    out_path = OUT / arm / f"{date}.json"
    env = _env(slot, ARMS[arm])
    proc = subprocess.run(
        [sys.executable, "-m", "backtest.replay_v511_full",
         "--date", date, "--bars-dir", BARS, "--output", str(out_path)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=240,
    )
    if proc.returncode != 0:
        print(f"  [{arm}] {date} FAILED rc={proc.returncode}")
        print("  stderr:", proc.stderr[-500:])
        return None
    try:
        d = json.loads(out_path.read_text())
        s = d.get("summary", {})
        pairs = d.get("pnl_pairs", [])
        full = sum(1 for p in pairs if p["shares"] * p["entry_price"] >= 8500)
        half = sum(1 for p in pairs if p["shares"] * p["entry_price"] < 6500)
        return {
            "date": date,
            "entries": s.get("entries", 0),
            "pairs": len(pairs),
            "wins": s.get("wins", 0),
            "losses": s.get("losses", 0),
            "pnl": s.get("total_pnl", 0.0),
            "full": full,
            "half": half,
        }
    except Exception as e:
        print(f"  [{arm}] {date} parse error: {e}")
        return None


def main():
    results = {arm: [] for arm in ARMS}
    for arm in ARMS:
        print(f"\n=== {arm} (P3_FULL={ARMS[arm]['P3_FULL_DI_THRESHOLD']}) ===")
        for date in DAYS:
            r = run_day(arm, date)
            if r:
                results[arm].append(r)
                print(f"  {date}: pairs={r['pairs']} full={r['full']} half={r['half']} "
                      f"W/L={r['wins']}/{r['losses']} pnl=${r['pnl']:+.2f}")

    # Comparison table
    print("\n" + "=" * 78)
    print(f"{'date':12s} {'baseline':>30s} {'armB (P3_FULL=25)':>30s}")
    print(f"{'':12s} {'pairs F/H W/L pnl':>30s} {'pairs F/H W/L pnl':>30s}")
    print("-" * 78)
    by_date = {arm: {r["date"]: r for r in results[arm]} for arm in ARMS}
    for date in DAYS:
        b = by_date["baseline"].get(date)
        a = by_date["armB"].get(date)
        bs = f"{b['pairs']:3d} {b['full']:2d}/{b['half']:2d} {b['wins']:2d}/{b['losses']:2d} ${b['pnl']:+8.2f}" if b else "MISSING"
        as_ = f"{a['pairs']:3d} {a['full']:2d}/{a['half']:2d} {a['wins']:2d}/{a['losses']:2d} ${a['pnl']:+8.2f}" if a else "MISSING"
        print(f"{date:12s} {bs:>30s} {as_:>30s}")

    bt = sum(r["pnl"] for r in results["baseline"])
    at_ = sum(r["pnl"] for r in results["armB"])
    print("-" * 78)
    print(f"{'TOTAL':12s} {' '*23}${bt:+8.2f}  {' '*23}${at_:+8.2f}")
    print(f"\nDelta: ${at_ - bt:+.2f} ({100*(at_-bt)/abs(bt) if bt else 0:+.1f}%)")

    (OUT / "summary.json").write_text(json.dumps({
        "results": results,
        "totals": {"baseline": bt, "armB": at_, "delta": at_ - bt},
    }, indent=2))


if __name__ == "__main__":
    main()
