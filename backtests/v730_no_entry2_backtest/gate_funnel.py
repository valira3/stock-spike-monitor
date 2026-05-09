#!/usr/bin/env python3
"""Run a single day with skip-logging enabled and tally rejection reasons.

Goal: see which gates are killing the most candidates so we know where
to loosen for ROI.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = "/tmp/ssm_v661"
BARS = "/home/user/workspace/canonical_backtest_data_v707/replay_layout"
WARMUP = "/home/user/workspace/v6_15_6_warmup_data/bars"
OUT = Path("/home/user/workspace/v730_no_entry2_backtest/gate_funnel")
OUT.mkdir(parents=True, exist_ok=True)

DAYS = ["2026-02-12", "2026-03-31", "2026-02-17"]  # mixed-results sample

PROD = {
    "POST_LOSS_COOLDOWN_MIN_LONG": "30",
    "POST_LOSS_COOLDOWN_MIN_SHORT": "30",
    "VOLUME_GATE_ENABLED": "true",
    "VOLUME_BUCKET_THRESHOLD_RATIO": "0.85",
    "POST_EXIT_SAME_TICKER_COOLDOWN_SEC": "1",
}

SKIP_RE = re.compile(r"\[SKIP\] ticker=(\S+) reason=(\S+)")


def _env(slot: Path) -> dict:
    return {
        **os.environ,
        "SSM_SMOKE_TEST": "1",
        "TELEGRAM_BOT_TOKEN": "000:fake",
        "FMP_API_KEY": "funnel_dummy",
        "LOG_LEVEL": "INFO",  # capture [SKIP]
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
        "SSM_BAR_CACHE_DIR": str(OUT / "bar_cache"),
        "ENTRY_2_DISABLED": "1",
        **PROD,
    }


def _seed(slot: Path) -> int:
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


def run(date: str):
    slot = OUT / "slot"
    slot.mkdir(parents=True, exist_ok=True)
    for stale in ("paper_state.json", "state.db", "trade_log.jsonl", "paper_trade.log"):
        p = slot / stale
        if p.exists():
            p.unlink()
    if not (slot / "bars").exists() or len(list((slot / "bars").iterdir())) < 50:
        seeded = _seed(slot)
        print(f"  seeded {seeded} warmup dirs")

    out_path = OUT / f"{date}.json"
    log_path = OUT / f"{date}.log"
    env = _env(slot)
    proc = subprocess.run(
        [sys.executable, "-m", "backtest.replay_v511_full",
         "--date", date, "--bars-dir", BARS, "--output", str(out_path)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=300,
    )
    log_path.write_text(proc.stdout + "\n--STDERR--\n" + proc.stderr)
    if proc.returncode != 0:
        print(f"  {date} FAILED rc={proc.returncode}")
        print(proc.stderr[-400:])
        return None

    # Tally
    skips = Counter()
    skips_by_ticker = defaultdict(Counter)
    skip_lines = 0
    for line in proc.stdout.splitlines():
        m = SKIP_RE.search(line)
        if not m:
            continue
        skip_lines += 1
        ticker, reason = m.group(1), m.group(2)
        # Bucket: top-level reason prefix (V5100_BOUNDARY:foo -> V5100_BOUNDARY)
        head = reason.split(":")[0]
        skips[head] += 1
        skips_by_ticker[ticker][head] += 1

    d = json.loads(out_path.read_text())
    s = d.get("summary", {})
    print(f"\n{date}: entries={s.get('entries')}  pairs={len(d.get('pnl_pairs',[]))}  "
          f"pnl=${s.get('total_pnl',0):+.2f}  skip_lines={skip_lines}")
    print(f"  Top skip reasons:")
    for reason, n in skips.most_common(15):
        print(f"    {n:6d}  {reason}")

    return {"date": date, "summary": s, "skips": dict(skips), "skip_lines": skip_lines}


def main():
    results = []
    for d in DAYS:
        r = run(d)
        if r:
            results.append(r)

    # Aggregate
    print("\n" + "=" * 70)
    print("AGGREGATE across", len(results), "days")
    print("=" * 70)
    agg = Counter()
    for r in results:
        for k, v in r["skips"].items():
            agg[k] += v
    total_skips = sum(agg.values())
    total_entries = sum(r["summary"].get("entries", 0) for r in results)
    print(f"Total skips: {total_skips}")
    print(f"Total entries: {total_entries}")
    print(f"Skip:Entry ratio: {total_skips / max(1,total_entries):.1f}:1")
    print()
    for reason, n in agg.most_common():
        pct = 100 * n / max(1, total_skips)
        print(f"  {n:7d} ({pct:5.1f}%)  {reason}")

    (OUT / "summary.json").write_text(json.dumps({
        "results": results,
        "aggregate": dict(agg),
    }, indent=2))


if __name__ == "__main__":
    main()
