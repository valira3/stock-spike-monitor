"""R28 (2026-05-19) -- partial-ladder probe. FALSIFIED.

All 4 variants hurt the 50%-at-1R baseline on the 252-day rth-expand
corpus:

  ladder_1R_2R           -$2,177/yr  (2R partial eats runner extension)
  ladder_1R_2R_3R        -$2,177/yr  (3R never fires; RR=2.5 target hits first)
  ladder_1R_2R_trail50   -$2,643/yr  (adding MFE trail costs more)
  ladder_full_trail50    -$2,643/yr  (same; 3R doesn't matter)

Root cause: locking in MORE share-fractions earlier reduces the
runner-share size that compensates for the 1R-and-reverse cohort. The
1R-only partial is the local optimum.

By extension, the sub-1R variant (30% at 0.75R, R28b plan) would face
the same pathology and is NOT pursued. The strategy correctly
identifies 1R as the optimal partial level.

---

Original probe design kept below for reproducibility.

Tests whether splitting profit-take into more stages beats the single
50%-at-1R partial used in Keystone. Uses ONLY existing levers (no new
code) to validate the hypothesis cheaply before extending with a
sub-1R lever in R28b.

Existing ladder levers (in tools/orb_backtest.py):
  - partial_profit_at_1r: 50% close at 1R (Keystone production-on)
  - partial_at_2r: half of remaining (= 25% of original) at 2R
  - partial_at_3r: half of remaining post-2R (= 12.5% of original) at 3R
  - runner_mfe_trail_bps: trail stop after partials fire (in bps)

Variants (5):
  baseline_1R           50% at 1R, 50% runner (Keystone)
  ladder_1R_2R          50% at 1R + 25% at 2R + 25% runner
  ladder_1R_2R_3R       50% + 25% + 12.5% + 12.5% runner
  ladder_1R_2R_trail50  50% + 25% + 25% runner with 50bps MFE trail
  ladder_full_trail50   50% + 25% + 12.5% + 12.5% runner with 50bps MFE trail

Baseline = Keystone production (1R partial only).

LOCAL sweep. Runs against /tmp/rth-data/data (252-day rth-expand).

Usage:
    python3 docs/research/r28_partial_ladder.py --run \\
        --out results/r28 --corpus /tmp/rth-data/data
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import subprocess
from pathlib import Path

BASE: dict[str, str] = {
    "ORB_OR_MINUTES": "30",
    "ORB_RR": "2.5",
    "ORB_RISK_PER_TRADE_PCT": "1.0",
    "ORB_RANGE_MIN_PCT": "0.008",
    "ORB_RANGE_MAX_PCT": "0.025",
    "ORB_MAX_TRADES_PER_DAY": "5",
    "ORB_MAX_CONCURRENT_RISK_DOLLARS": "2000",
    "ORB_DAILY_LOSS_KILL_PCT": "2.0",
    "ORB_ATR_STOP_MULT": "1.75",
    "ORB_ATR_LOOKBACK_5M": "14",
    "ORB_PARTIAL_PROFIT_AT_1R": "1",
    "ORB_MOVE_TO_BE_AFTER_1R": "1",
    "ORB_STOP_BUFFER_BPS": "5.0",
    "ORB_ENTRY_SLIPPAGE_BPS": "1.5",
    "ORB_EXIT_SLIPPAGE_BPS": "1.5",
    "ORB_STOP_KICK_BPS": "5.0",
    "ORB_SHORT_PENALTY_BPS": "1.0",
    "ORB_MAX_TRADE_NOTIONAL_PCT": "75",
    "ORB_SKIP_GAP_ABOVE_PCT": "1.5",
    "ORB_SKIP_VIX_ABOVE": "25.0",
    "ORB_SKIP_PRIOR_SPY_RET_LT_BPS": "-40.0",
    "ORB_SKIP_EARNINGS_WINDOW": "1",
    "ORB_TIME_CUTOFF_ET": "11:00",
    "ORB_EOD_CUTOFF_ET": "15:55",
    "ORB_ACCOUNT": "100000",
    "ORB_COMPOUND_DAILY": "1",
    "ORB_TICKER_SIDE_BLOCKLIST": "{}",
    "ORB_MAX_VWAP_DEV_BPS": "15.0",
    "ORB_MAX_VWAP_DEV_TICKERS": "META,MSFT,AAPL,AMZN,GOOG,AVGO",
    "ORB_POST_TRADE_COOLDOWN_MIN": "10",
    "ORB_RUNNER_EOD_PREP_ET": "14:00",
    "ORB_STALE_FULL_EXIT_ET": "14:30",
    "ORB_STALE_FULL_EXIT_MFE_FLOOR_R": "0.0",
}


def _variants() -> list[tuple[str, dict[str, str]]]:
    return [
        ("baseline_1R", {}),
        ("ladder_1R_2R", {"ORB_PARTIAL_AT_2R": "1"}),
        ("ladder_1R_2R_3R", {"ORB_PARTIAL_AT_2R": "1", "ORB_PARTIAL_AT_3R": "1"}),
        ("ladder_1R_2R_trail50", {
            "ORB_PARTIAL_AT_2R": "1",
            "ORB_RUNNER_MFE_TRAIL_BPS": "50",
        }),
        ("ladder_full_trail50", {
            "ORB_PARTIAL_AT_2R": "1",
            "ORB_PARTIAL_AT_3R": "1",
            "ORB_RUNNER_MFE_TRAIL_BPS": "50",
        }),
    ]


def _run_one(vid, overrides, out_root, tickers, year_prefix, corpus):
    out_dir = out_root / vid
    out_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(BASE)
    env.update(overrides)
    cmd = [
        sys.executable, "tools/orb_backtest.py",
        "--corpus", corpus, "--out", str(out_dir),
        "--year-prefix", year_prefix, "--tickers", tickers,
    ]
    print(f"  [{vid}] running ...", flush=True)
    res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        return {"vid": vid, "ok": False, "error": res.stderr[-500:]}
    s = json.loads((out_dir / "summary.json").read_text())
    return {
        "vid": vid, "ok": True, "overrides": overrides,
        "pnl_dollars": s.get("net_pnl", 0.0),
        "trades": s.get("entries", 0),
        "win_rate_pct": s.get("win_rate_pct", 0.0),
    }


def _print_variants():
    for vid, ov in _variants():
        print(f"{vid}\t{json.dumps(ov)}")


def _run_sweep(out_root, tickers, year_prefix, corpus):
    out_root.mkdir(parents=True, exist_ok=True)
    results = [_run_one(vid, ov, out_root, tickers, year_prefix, corpus)
               for vid, ov in _variants()]
    baseline = next((r for r in results if r["vid"] == "baseline_1R"), None)
    base_pnl = baseline.get("pnl_dollars") if baseline and baseline.get("ok") else None
    print()
    print("=" * 90)
    print(f"{'variant':<30} {'pnl ($)':>12} {'wr %':>6} {'trades':>7} {'delta vs base':>15}")
    print("-" * 90)
    for r in sorted(results, key=lambda x: -x.get("pnl_dollars", 0.0)):
        if not r.get("ok"):
            print(f"{r['vid']:<30} FAILED ({r.get('error','?')[:30]})")
            continue
        pnl = r["pnl_dollars"]
        if base_pnl is not None and r["vid"] != "baseline_1R":
            delta = pnl - base_pnl
            print(f"{r['vid']:<30} ${pnl:>10,.0f} {r['win_rate_pct']:>5.1f} {r['trades']:>7d} ${delta:>+12,.0f}")
        else:
            print(f"{r['vid']:<30} ${pnl:>10,.0f} {r['win_rate_pct']:>5.1f} {r['trades']:>7d} {'(baseline)':>15}")
    print("=" * 90)
    (out_root / "sweep_summary.json").write_text(json.dumps({
        "baseline_vid": "baseline_1R",
        "baseline_pnl_dollars": base_pnl,
        "results": results,
    }, indent=2))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--print-variants", action="store_true")
    p.add_argument("--run", action="store_true")
    p.add_argument("--out", type=Path, default=Path("results/r28_partial_ladder"))
    p.add_argument("--corpus", default="/tmp/rth-data/data")
    p.add_argument("--tickers", default="AAPL,AMZN,AVGO,GOOG,META,MSFT,NFLX,NVDA,ORCL,QQQ,SPY,TSLA")
    p.add_argument("--year-prefix", default="20")
    args = p.parse_args()
    if args.print_variants:
        _print_variants(); return 0
    if args.run:
        _run_sweep(args.out, args.tickers, args.year_prefix, args.corpus)
        return 0
    p.print_help(); return 1


if __name__ == "__main__":
    sys.exit(main())
