"""simulator.replay -- replay any single day (or last N) and diff vs live.

Replaces tools/orb_replay_day.py with a simulator-driven equivalent
that:

  1. Runs the v10 decision pipeline against the on-disk bar corpus
     (`data/YYYY-MM-DD/`) with all external services mocked.
  2. Optionally diffs the simulator's entries / exits against the
     bot's live recorded trades from `data/trade_log.jsonl`.
  3. Renders a per-trade comparison table.

CLI:
    python -m simulator.replay 2026-05-15                  # one day
    python -m simulator.replay --last-7                    # last 7 corpus days
    python -m simulator.replay 2026-05-15 --diff-live      # vs trade_log.jsonl
    python -m simulator.replay --last-7 --diff-live --workers 4
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional

from simulator.batch import BatchConfig, run_days


# ----------------------------------------------------------------------
# Date resolution helpers
# ----------------------------------------------------------------------


def list_corpus_dates(root: str = "data") -> List[str]:
    if not os.path.isdir(root):
        return []
    return sorted(d for d in os.listdir(root)
                  if len(d) == 10 and d[4] == "-" and d[7] == "-"
                  and os.path.isdir(os.path.join(root, d)))


def last_n_corpus_dates(n: int, root: str = "data") -> List[str]:
    return list_corpus_dates(root=root)[-n:]


# ----------------------------------------------------------------------
# Live trade-log loader
# ----------------------------------------------------------------------


def _load_trade_log(date: str,
                    path: str = "data/trade_log.jsonl",
                    extra_paths: Optional[List[str]] = None) -> List[dict]:
    """Pull the trades for `date` from the JSONL trade log. The log
    records ALL closed trades; we filter by entry_ts / exit_ts being
    on the same ET date."""
    candidates = [path]
    if extra_paths:
        candidates.extend(extra_paths)
    rows: List[dict] = []
    for p in candidates:
        if not os.path.isfile(p):
            continue
        with open(p) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                # Trade log entries carry entry_ts_utc + exit_ts_utc;
                # accept any row where either touches our date.
                if _on_date(obj.get("entry_ts_utc"), date) or _on_date(obj.get("exit_ts_utc"), date):
                    rows.append(obj)
    return rows


def _on_date(iso_ts: Optional[str], date: str) -> bool:
    if not iso_ts:
        return False
    return iso_ts.startswith(date)


# ----------------------------------------------------------------------
# Diff renderer
# ----------------------------------------------------------------------


def _diff_one_day(sim_result: dict, live_trades: List[dict]) -> Dict:
    """Pair each simulator entry with the nearest live trade on the
    same (ticker, side); count exact matches, mismatches, sim-only,
    live-only."""
    sim_entries = sim_result.get("entries", [])
    sim_by_key = {(e["ticker"], _side_norm(e["side"])): e for e in sim_entries}
    live_by_key: Dict[tuple, dict] = {}
    for t in live_trades:
        key = (str(t.get("ticker", "")).upper(),
               _side_norm(str(t.get("side", ""))))
        live_by_key[key] = t

    rows = []
    matched: List[str] = []
    sim_only: List[str] = []
    live_only: List[str] = []
    for key, sim in sim_by_key.items():
        live = live_by_key.get(key)
        if live is None:
            sim_only.append(f"{key[0]} {key[1]}")
            rows.append({"key": key, "sim": sim, "live": None, "verdict": "SIM-ONLY"})
            continue
        sim_price = sim.get("price", 0)
        live_entry_px = live.get("entry_price") or live.get("price") or 0
        try:
            delta = abs(float(sim_price) - float(live_entry_px))
        except Exception:
            delta = 0.0
        verdict = "MATCH" if delta < 0.10 else "DRIFT"
        matched.append(f"{key[0]} {key[1]}")
        rows.append({"key": key, "sim": sim, "live": live, "verdict": verdict,
                     "price_delta": delta})

    for key in live_by_key:
        if key not in sim_by_key:
            live_only.append(f"{key[0]} {key[1]}")
            rows.append({"key": key, "sim": None, "live": live_by_key[key],
                         "verdict": "LIVE-ONLY"})

    return {
        "rows": rows,
        "matched": matched,
        "sim_only": sim_only,
        "live_only": live_only,
        "verdict": "PASS" if (not sim_only and not live_only) else "DIVERGE",
    }


def _side_norm(s: str) -> str:
    s = (s or "").upper()
    if "LONG" in s or s == "BUY":
        return "LONG"
    if "SHORT" in s or s == "SELL":
        return "SHORT"
    return s


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------


def _print_replay_summary(results: List[dict], diffs: Optional[Dict[str, dict]] = None):
    print()
    print("=" * 78)
    print(" Replay Summary")
    print("=" * 78)
    print()
    print(
        f"  {'date':10s}  {'entries':>7s}  {'exits':>5s}  {'orders':>6s}  "
        f"{'P&L':>10s}  {'open_eod':>8s}  {'tg':>3s}  status"
    )
    print("  " + "-" * 74)

    total_pl = 0.0
    for r in results:
        date = r["date"]
        n_e = len(r.get("entries", []))
        n_x = len(r.get("exits", []))
        n_o = len(r.get("alpaca_orders", []))
        pl = r.get("realized_pl_total", 0.0)
        total_pl += pl
        n_open = len(r.get("open_at_eod", []))
        n_tg = r.get("telegram_count", 0)
        if r.get("error"):
            status = "ERR"
        elif diffs and date in diffs:
            d = diffs[date]
            status = d["verdict"]
        else:
            status = "OK"
        print(f"  {date:10s}  {n_e:>7d}  {n_x:>5d}  {n_o:>6d}  "
              f"${pl:>+8.2f}  {n_open:>8d}  {n_tg:>3d}  {status}")

    print("  " + "-" * 74)
    print(f"  {'TOTAL':10s}                                ${total_pl:>+8.2f}")
    print()

    if diffs:
        print(" Per-day diff vs trade_log.jsonl")
        print("-" * 78)
        for date in sorted(diffs):
            d = diffs[date]
            if not d["rows"]:
                continue
            print(f"\n  {date}")
            for row in d["rows"]:
                key = row["key"]
                v = row["verdict"]
                sim = row["sim"]
                live = row["live"]
                if v == "MATCH":
                    print(f"    {key[0]} {key[1]}  MATCH  "
                          f"sim={sim.get('price', 0):.2f}  "
                          f"live={(live.get('entry_price') or 0):.2f}")
                elif v == "DRIFT":
                    delta = row.get("price_delta", 0)
                    print(f"    {key[0]} {key[1]}  DRIFT  "
                          f"sim={sim.get('price', 0):.2f}  "
                          f"live={(live.get('entry_price') or 0):.2f}  "
                          f"delta={delta:.2f}")
                elif v == "SIM-ONLY":
                    print(f"    {key[0]} {key[1]}  SIM-ONLY  (live did NOT fire)")
                elif v == "LIVE-ONLY":
                    print(f"    {key[0]} {key[1]}  LIVE-ONLY  (sim did NOT fire)")
        print()


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def _main(argv=None):
    p = argparse.ArgumentParser(description="Simulator-based day replay")
    p.add_argument("date", nargs="?", help="YYYY-MM-DD (single day)")
    p.add_argument("--last-7", action="store_true",
                   help="Replay the last 7 corpus days")
    p.add_argument("--last", type=int,
                   help="Replay the last N corpus days")
    p.add_argument("--diff-live", action="store_true",
                   help="Diff simulator outcomes vs data/trade_log.jsonl")
    p.add_argument("--trade-log", default="data/trade_log.jsonl",
                   help="Path to the live trade-log JSONL")
    p.add_argument("--tickers", default="AAPL,MSFT,NVDA,AMZN,META,GOOG,AVGO,NFLX,ORCL,TSLA,QQQ,SPY")
    p.add_argument("--workers", type=int, default=0)
    p.add_argument("--corpus-root", default="data")
    p.add_argument("--out-json", help="Write the full report to this JSON file")
    args = p.parse_args(argv)

    # Resolve dates.
    if args.last_7:
        dates = last_n_corpus_dates(7, root=args.corpus_root)
    elif args.last:
        dates = last_n_corpus_dates(args.last, root=args.corpus_root)
    elif args.date:
        dates = [args.date]
    else:
        raise SystemExit("Provide a date, --last-7, or --last N")
    if not dates:
        raise SystemExit("No corpus days found")

    universe = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    cfg = BatchConfig(workers=args.workers, corpus_root=args.corpus_root)

    results = run_days(dates, universe, cfg)

    diffs: Optional[Dict[str, dict]] = None
    if args.diff_live:
        diffs = {}
        for r in results:
            live = _load_trade_log(r["date"], path=args.trade_log)
            diffs[r["date"]] = _diff_one_day(r, live)

    _print_replay_summary(results, diffs)

    if args.out_json:
        out = {"results": results, "diffs": diffs}
        with open(args.out_json, "w") as fh:
            json.dump(out, fh, indent=2, default=str)
        print(f"[replay] wrote {args.out_json}")

    has_err = any(r.get("error") for r in results)
    return 1 if has_err else 0


if __name__ == "__main__":
    sys.exit(_main())
