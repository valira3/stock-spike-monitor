"""R27 (2026-05-19) -- time-bracketed local-max trailing exit.

FALSIFIED 2026-05-19 on the full 252-day rth-expand corpus.

Both theories tested were rejected:

1. **Local-max trailing exit** (window-bracketed giveback in R units):
   - 124-day winner (trail_g0.4_o13:30_c14:55 = +$892) disappeared on
     252-day rerun: same variant -$31 vs baseline. Classic overfit on
     the smaller sample.
   - All 6 trail variants on 252-day: -$31 to -$626 vs baseline. No
     variant beats R21+R26.
   - Spread is within noise (~$700 across 7 trail configs vs $53k base).

2. **Lunch-hour exits** (hard whole-position close before 13:00 ET):
   - Catastrophic across both corpora.
   - lunch_exit_11:30 = -$20,125/yr (38% of baseline P&L destroyed).
   - lunch_exit_12:00 = -$9,296/yr.
   - lunch_exit_12:30 = -$5,644/yr (best of the bad).
   - Root cause: most partial-at-1R fires AFTER 11:30 ET; cutting
     positions before they reach the partial line eliminates the
     runner-extension P&L.
   - "Combined trail + lunch" = same loss as lunch alone (lunch fires
     first, before trail's 13:30 window opens).

**Verdict: R21+R26 baseline (14:00 runner + 14:30 stale) is the local
optimum for the morning-position exit problem on this corpus. Do not
ship any R27 variant.**

**Sample-size lesson**: 124-day single-sided corpus was insufficient
to distinguish trail from noise. 252-day full corpus reversed the
finding entirely. Minimum bar for promotion: full-year on rth-expand
corpus.

---

Original sweep matrix kept below for reproducibility.

Usage:
    python3 docs/research/r27_local_max_trail.py --print-variants
    python3 docs/research/r27_local_max_trail.py --run --out results/r27/ \\
        --corpus /tmp/rth-data/data
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Layered on top of Keystone v9.1.114 production config (per CLAUDE.md).
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
    # R21+R26 backstops (production-on)
    "ORB_RUNNER_EOD_PREP_ET": "14:00",
    "ORB_STALE_FULL_EXIT_ET": "14:30",
    "ORB_STALE_FULL_EXIT_MFE_FLOOR_R": "0.0",
}


def _variants() -> list[tuple[str, dict[str, str]]]:
    """Variant matrix: baseline + trail-around-winner + lunch-exit + combined."""
    variants: list[tuple[str, dict[str, str]]] = []
    variants.append(("baseline_R21_R26", {}))
    # Trail focused around prior 124-day winner (0.5R / 13:30-14:55)
    for gb_r in ("0.4", "0.5", "0.7"):
        for open_et in ("13:30", "14:00"):
            close_et = "14:55"
            vid = f"trail_g{gb_r.replace('.','p')}_o{open_et.replace(':','')}_c{close_et.replace(':','')}"
            variants.append((vid, {
                "ORB_TRAIL_GIVEBACK_R": gb_r,
                "ORB_TRAIL_OPEN_ET": open_et,
                "ORB_TRAIL_CLOSE_ET": close_et,
            }))
    # Lunch-exit: hard whole-position close before lunch (uses existing
    # ORB_EOD_PREP_EXIT_ET lever, generic time-exit).
    for cutoff in ("11:30", "12:00", "12:30"):
        vid = f"lunch_exit_{cutoff.replace(':','')}"
        variants.append((vid, {
            "ORB_EOD_PREP_EXIT_ET": cutoff,
        }))
    # Combined: winner-trail + lunch-exit at noon
    variants.append(("combined_trail05_lunch1200", {
        "ORB_TRAIL_GIVEBACK_R": "0.5",
        "ORB_TRAIL_OPEN_ET": "13:30",
        "ORB_TRAIL_CLOSE_ET": "14:55",
        "ORB_EOD_PREP_EXIT_ET": "12:00",
    }))
    return variants


def _print_variants() -> None:
    for vid, overrides in _variants():
        print(f"{vid}\t{json.dumps(overrides)}")


def _run_one(vid: str, overrides: dict[str, str], out_root: Path,
             tickers: str, year_prefix: str, corpus: str) -> dict:
    out_dir = out_root / vid
    out_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(BASE)
    env.update(overrides)
    cmd = [
        sys.executable, "tools/orb_backtest.py",
        "--corpus", corpus,
        "--out", str(out_dir),
        "--year-prefix", year_prefix,
        "--tickers", tickers,
    ]
    print(f"  [{vid}] running ...", flush=True)
    res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        print(f"  [{vid}] FAILED rc={res.returncode}", flush=True)
        print(f"    stderr tail: {res.stderr[-500:]}", flush=True)
        return {"vid": vid, "ok": False, "error": res.stderr[-1000:]}
    summary_path = out_dir / "summary.json"
    if not summary_path.exists():
        return {"vid": vid, "ok": False, "error": "no summary.json"}
    with summary_path.open() as f:
        summary = json.load(f)
    pnl = summary.get("net_pnl", 0.0)
    trades = summary.get("entries", 0)
    win_rate = summary.get("win_rate_pct", 0.0)
    days = summary.get("days_ran", 0)
    return {
        "vid": vid,
        "ok": True,
        "overrides": overrides,
        "pnl_dollars": pnl,
        "trades": trades,
        "win_rate_pct": win_rate,
        "days_ran": days,
    }


def _run_sweep(out_root: Path, tickers: str, year_prefix: str, corpus: str) -> None:
    out_root.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    variants = _variants()
    print(f"R27 local-max-trail sweep -- {len(variants)} variants")
    print(f"  corpus: {corpus}  tickers: {tickers}  year_prefix: {year_prefix}")
    print(f"  out: {out_root}")
    print()
    for vid, overrides in variants:
        results.append(_run_one(vid, overrides, out_root, tickers, year_prefix, corpus))

    baseline = next((r for r in results if r["vid"] == "baseline_R21_R26"), None)
    base_pnl = baseline.get("pnl_dollars", 0.0) if baseline and baseline.get("ok") else None

    print()
    print("=" * 90)
    print(f"{'variant':<40} {'pnl ($)':>12} {'wr %':>6} {'trades':>7} {'delta vs base':>15}")
    print("-" * 90)
    for r in sorted(results, key=lambda x: -x.get("pnl_dollars", 0.0)):
        if not r.get("ok"):
            print(f"{r['vid']:<40} FAILED ({r.get('error','?')[:30]})")
            continue
        pnl = r["pnl_dollars"]
        wr = r.get("win_rate_pct", 0.0)
        n = r.get("trades", 0)
        if base_pnl is not None and r["vid"] != "baseline_R21_R26":
            delta = pnl - base_pnl
            print(f"{r['vid']:<40} ${pnl:>10,.0f} {wr:>5.1f} {n:>7d} ${delta:>+12,.0f}")
        else:
            print(f"{r['vid']:<40} ${pnl:>10,.0f} {wr:>5.1f} {n:>7d} {'(baseline)':>15}")
    print("=" * 90)

    agg_path = out_root / "sweep_summary.json"
    with agg_path.open("w") as f:
        json.dump({
            "baseline_vid": "baseline_R21_R26",
            "baseline_pnl_dollars": base_pnl,
            "results": results,
        }, f, indent=2)
    print(f"\nWrote {agg_path}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--print-variants", action="store_true")
    p.add_argument("--run", action="store_true")
    p.add_argument("--out", type=Path, default=Path("results/r27_local_max_trail"))
    p.add_argument("--corpus", default="data",
                   help="Corpus root (e.g. data, /tmp/rth-data/data)")
    p.add_argument("--tickers",
                   default="AAPL,AMZN,AVGO,GOOG,META,MSFT,NFLX,NVDA,ORCL,QQQ,SPY,TSLA")
    p.add_argument("--year-prefix", default="20")
    args = p.parse_args()
    if args.print_variants:
        _print_variants()
        return 0
    if args.run:
        if not shutil.which(sys.executable):
            print("python3 not found", file=sys.stderr)
            return 2
        _run_sweep(args.out, args.tickers, args.year_prefix, args.corpus)
        return 0
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
