"""R26 quarterly stability check.

Re-runs combined_replay per-quarter for:
  - prod baseline (R21 14:00 only)
  - R26 winner candidate (1430_floor0)
  - R26 runner-up (1400_floor05)

Confirms the R26 lever is positive across all quarters (not driven by
a single lucky window).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

QUARTERS = {
    "Q3-2025": ["2025-07", "2025-08", "2025-09"],
    "Q4-2025": ["2025-10", "2025-11", "2025-12"],
    "Q1-2026": ["2026-01", "2026-02", "2026-03"],
    "Q2-2026": ["2026-04", "2026-05"],
}

VARIANTS = [
    "R26_baseline_prod_R21_only",
    "R26_full_1430_floor0",
    "R26_full_1400_floor05",
    "R26_full_1330_floor05",
]

CORPUS = "/tmp/rth-data/data"


def _replay_one(morning_dir: Path, eod_dir: Path, equity: int, prefix: str) -> dict:
    """Run combined_replay for a single month-prefix and parse numbers."""
    proc = subprocess.run(
        [sys.executable, "tools/combined_replay.py",
         "--morning", str(morning_dir),
         "--eod", str(eod_dir),
         "--corpus", CORPUS,
         "--equity", str(equity),
         "--gross-cap", "1.9",
         "--year-prefix", prefix],
        capture_output=True, text=True, cwd=REPO, timeout=60,
    )

    def grab_dollars(label):
        for line in proc.stdout.splitlines():
            if label in line:
                toks = [t for t in line.split() if any(c.isdigit() for c in t)]
                if toks:
                    v = toks[-1].replace("$", "").replace(",", "").replace("%", "").lstrip("+")
                    try:
                        return float(v)
                    except ValueError:
                        pass
        return 0.0

    def grab_int(label):
        for line in proc.stdout.splitlines():
            if label in line:
                toks = line.split()
                for t in reversed(toks):
                    if t.replace(",", "").isdigit():
                        return int(t.replace(",", ""))
        return 0

    return {
        "n_days": grab_int("N days replayed"),
        "morning_pnl": grab_dollars("Net P&L morning"),
        "eod_pnl": grab_dollars("Net P&L EOD admitted"),
        "combined_pnl": grab_dollars("Net P&L combined"),
    }


def main():
    print("R26 QUARTERLY STABILITY CHECK")
    print("=" * 78)
    t0 = time.time()

    results = {}
    for variant in VARIANTS:
        results[variant] = {}
        print(f"\n--- {variant} ---", flush=True)
        for acct_label, acct in [("VAL", 30_185), ("MAIN", 100_000)]:
            morning_dir = REPO / "results" / "r26" / f"{variant}_acct{acct}" / "morning"
            eod_dir = REPO / "results" / "r22" / f"eod_acct{acct}"
            if not morning_dir.exists():
                print(f"  [SKIP] {acct_label} missing morning_dir: {morning_dir}")
                continue
            for q, prefixes in QUARTERS.items():
                q_total = {"n_days": 0, "morning_pnl": 0.0, "eod_pnl": 0.0, "combined_pnl": 0.0}
                for p in prefixes:
                    r = _replay_one(morning_dir, eod_dir, acct, p)
                    for k in q_total:
                        q_total[k] += r[k]
                results[variant].setdefault(acct_label, {})[q] = q_total
                print(
                    f"  {acct_label} {q}: "
                    f"days={q_total['n_days']:>3} "
                    f"morn=${q_total['morning_pnl']:>+8,.0f} "
                    f"eod=${q_total['eod_pnl']:>+7,.0f} "
                    f"combined=${q_total['combined_pnl']:>+8,.0f}",
                    flush=True,
                )

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.0f}s\n" + "=" * 110)
    print("R26 LEVER DELTAS vs prod baseline (R21 14:00 only)")
    print("=" * 110)
    baseline = results["R26_baseline_prod_R21_only"]
    for variant in VARIANTS[1:]:
        v_res = results[variant]
        print(f"\n  {variant}:")
        for acct_label in ("VAL", "MAIN"):
            print(f"    {acct_label}:")
            total_delta = 0.0
            for q in QUARTERS:
                base_pnl = baseline.get(acct_label, {}).get(q, {}).get("combined_pnl", 0.0)
                var_pnl = v_res.get(acct_label, {}).get(q, {}).get("combined_pnl", 0.0)
                delta = var_pnl - base_pnl
                total_delta += delta
                mark = "+" if delta >= 0 else "-"
                print(f"      {q}: base=${base_pnl:>+8,.0f}  var=${var_pnl:>+8,.0f}  delta={mark}${abs(delta):>7,.0f}")
            print(f"      {'='*8} sum delta: ${total_delta:>+,.0f}")

    out = REPO / "results" / "r26" / "quarterly.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\nJSON: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
