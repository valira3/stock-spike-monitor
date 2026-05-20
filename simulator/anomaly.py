"""simulator.anomaly -- batch run + correlate against expectations.

CLI:
    python -m simulator.anomaly                       # representative 30 days
    python -m simulator.anomaly --all                 # every indexed day
    python -m simulator.anomaly --categories gap_up_1_5pct,vix_high
    python -m simulator.anomaly --per-category 5      # how many days/category

Builds the corpus index on demand (if simulator/corpus/day_index.json
is missing or --rebuild is passed), then runs the simulator on the
selected days in parallel, then evaluates every DEFAULT_RULES rule
against every day, and emits a structured anomaly report.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List

from simulator.batch import BatchConfig, run_days
from simulator.corpus_index import (
    build_index,
    load_index,
    pick_representative,
)
from simulator.expectations import DEFAULT_RULES, RuleFailure, evaluate


def _ensure_index(corpus_root: str, path: str, rebuild: bool) -> List[dict]:
    if rebuild or not os.path.isfile(path):
        print(f"[anomaly] building corpus index ({corpus_root}) -> {path}")
        rows = build_index(corpus_root=corpus_root, out_path=path)
    else:
        rows = load_index(path)
        print(f"[anomaly] loaded existing index: {len(rows)} days  ({path})")
    return rows


def _print_anomaly_report(per_day_failures: Dict[str, List[RuleFailure]],
                          day_rows_by_date: Dict[str, dict],
                          day_results_by_date: Dict[str, dict]):
    print()
    print("=" * 78)
    print(" Anomaly Report")
    print("=" * 78)
    print()

    n_days = len(per_day_failures)
    n_with_anom = sum(1 for fs in per_day_failures.values() if fs)
    total = sum(len(fs) for fs in per_day_failures.values())
    sev_counts: Dict[str, int] = {}
    for fs in per_day_failures.values():
        for f in fs:
            sev_counts[f.severity] = sev_counts.get(f.severity, 0) + 1

    print(f"  Days run:          {n_days}")
    print(f"  Days w/ anomaly:   {n_with_anom}")
    print(f"  Total anomalies:   {total}")
    if sev_counts:
        sev_str = "  ".join(f"{k}={v}" for k, v in sorted(sev_counts.items()))
        print(f"  By severity:       {sev_str}")
    print()

    # Per-day rows.
    print(
        f"  {'date':10s}  {'categories':32s}  {'entries':>7s}  {'P&L':>10s}  status"
    )
    print("  " + "-" * 74)
    for date in sorted(per_day_failures):
        failures = per_day_failures[date]
        row = day_rows_by_date.get(date, {})
        result = day_results_by_date.get(date, {})
        cats = ",".join(row.get("categories", []))[:32]
        n_e = len(result.get("entries", []))
        pl = result.get("realized_pl_total", 0.0)
        if not failures:
            status = "OK"
        else:
            errs = sum(1 for f in failures if f.severity == "ERROR")
            warns = sum(1 for f in failures if f.severity == "WARN")
            status = f"{errs}E/{warns}W"
        print(f"  {date:10s}  {cats:32s}  {n_e:>7d}  ${pl:>+8.2f}  {status}")

    # Anomaly details.
    if total > 0:
        print()
        print(" Anomaly Details")
        print("-" * 78)
        for date in sorted(per_day_failures):
            failures = per_day_failures[date]
            if not failures:
                continue
            row = day_rows_by_date.get(date, {})
            result = day_results_by_date.get(date, {})
            print(f"\n  {date}  categories={row.get('categories')}")
            print(f"    gap={row.get('spy_gap_pct'):.2f}%  "
                  f"or_range={row.get('spy_or_range_pct'):.2f}%  "
                  f"entries={len(result.get('entries', []))}  "
                  f"P&L=${result.get('realized_pl_total', 0):+.2f}")
            for f in failures:
                print(f"    [{f.severity:5s}] {f.rule_name}")
                print(f"            why:        {f.why}")
                print(f"            why_fail:   {f.why_fail}")
    print()


def _main(argv=None):
    p = argparse.ArgumentParser(description="Simulator-based anomaly detection")
    p.add_argument("--corpus-root", default="data")
    p.add_argument("--index", default="simulator/corpus/day_index.json")
    p.add_argument("--rebuild", action="store_true",
                   help="Rebuild the corpus index before running")
    p.add_argument("--all", action="store_true",
                   help="Run every indexed day (default: representative sample)")
    p.add_argument("--per-category", type=int, default=3,
                   help="Sample N days per category (default 3)")
    p.add_argument("--categories", help="Comma-separated subset of categories")
    p.add_argument("--tickers", default="AAPL,MSFT,NVDA,AMZN,META,GOOG,AVGO,NFLX,ORCL,TSLA,QQQ,SPY")
    p.add_argument("--workers", type=int, default=0)
    p.add_argument("--out-json", help="Write the full report to this JSON file")
    args = p.parse_args(argv)

    # 1. Index.
    rows = _ensure_index(args.corpus_root, args.index, args.rebuild)
    if not rows:
        print("[anomaly] no corpus days found -- nothing to do", file=sys.stderr)
        return 1

    # 2. Pick the days.
    cats = [c.strip() for c in (args.categories or "").split(",") if c.strip()] or None
    if args.all:
        dates = sorted(r["date"] for r in rows)
    else:
        dates = pick_representative(rows, per_category=args.per_category,
                                    categories=cats)
    if not dates:
        print("[anomaly] no days match the filter", file=sys.stderr)
        return 1
    print(f"[anomaly] running {len(dates)} days  (workers={args.workers or 'auto'})")

    universe = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    cfg = BatchConfig(workers=args.workers, corpus_root=args.corpus_root)

    # 3. Run the batch.
    results = run_days(dates, universe, cfg)
    results_by_date = {r["date"]: r for r in results}
    rows_by_date = {r["date"]: r for r in rows}

    # 4. Evaluate rules.
    per_day_failures: Dict[str, List[RuleFailure]] = {}
    for date in dates:
        row = rows_by_date.get(date, {})
        result = results_by_date.get(date, {})
        per_day_failures[date] = evaluate(row, result, DEFAULT_RULES)

    # 5. Report.
    _print_anomaly_report(per_day_failures, rows_by_date, results_by_date)

    if args.out_json:
        out = {
            "dates_run": dates,
            "results": [results_by_date[d] for d in dates if d in results_by_date],
            "rows": [rows_by_date[d] for d in dates if d in rows_by_date],
            "failures": {
                d: [{"rule": f.rule_name, "severity": f.severity,
                     "why": f.why, "why_fail": f.why_fail}
                    for f in fs]
                for d, fs in per_day_failures.items()
            },
        }
        with open(args.out_json, "w") as fh:
            json.dump(out, fh, indent=2, default=str)
        print(f"[anomaly] wrote {args.out_json}")

    # Exit 1 if any ERROR severity present.
    has_error = any(f.severity == "ERROR" for fs in per_day_failures.values() for f in fs)
    return 1 if has_error else 0


if __name__ == "__main__":
    sys.exit(_main())
