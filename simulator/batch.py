"""simulator.batch -- parallel day runner.

The foundation for:
  * simulator.replay   (last-7-days + live-trade-log diff)
  * simulator.annual   (full-year P&L)
  * simulator.anomaly  (correlate observed vs expected, day batch)

multiprocessing.Pool runs each day in its own Python process so the
module-level monkeypatches the simulator installs (alpaca client
classes, urllib.request.urlopen, datetime.datetime, time.*) stay
isolated per day. Threading is unsafe here -- the patches are global.

Per-day work:
    1. Build a "replay" scenario for (date, universe).
    2. Run the simulator end-to-end.
    3. Return a flat, picklable summary dict (no live runner state).

The result is a list of DayResult dicts; aggregation happens in the
parent process.
"""
from __future__ import annotations

import json
import multiprocessing as _mp
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# ----------------------------------------------------------------------
# Worker entry points (must be top-level for pickle).
# ----------------------------------------------------------------------


def _summarize_day(state: dict, scenario: dict, elapsed_s: float) -> dict:
    """Strip non-picklable references and project to a stable shape."""
    realized = dict(state.get("alpaca_realized_pl", {}) or {})
    open_at_eod = list((state.get("alpaca_positions", {}) or {}).keys())
    return {
        "date": scenario.get("date"),
        "name": scenario.get("name"),
        "universe": list(scenario.get("universe", [])),
        "entries": [_clone(e) for e in state.get("entries", [])],
        "exits": [_clone(x) for x in state.get("exits", [])],
        "alpaca_orders": [_clone(o) for o in state.get("alpaca_orders", [])],
        "realized_pl": realized,
        "realized_pl_total": float(sum(realized.values())),
        "open_at_eod": open_at_eod,
        "telegram_count": len(state.get("telegram_sends", []) or []),
        "fmp_count": len(state.get("fmp_calls", []) or []),
        "yahoo_count": len(state.get("yahoo_calls", []) or []),
        "elapsed_s": elapsed_s,
        "error": None,
    }


def _clone(item):
    """Shallow-copy a dict (or convert objects to dicts) so it pickles."""
    if isinstance(item, dict):
        return {k: _clone(v) for k, v in item.items()}
    if isinstance(item, (list, tuple)):
        return [_clone(v) for v in item]
    if hasattr(item, "__dict__"):
        return {k: _clone(v) for k, v in item.__dict__.items() if not k.startswith("_")}
    return item


def _run_one_day(args) -> dict:
    """Worker entry. args = (date, universe, config_overrides, corpus_root)."""
    date, universe, config_overrides, corpus_root = args
    t0 = time.time()
    try:
        # Set the corpus root for this worker BEFORE building the runner.
        if corpus_root:
            os.environ["SIMULATOR_CORPUS_ROOT"] = corpus_root

        # Import here -- workers under "spawn" do not inherit imports.
        from simulator.runner import SimulatorRunner

        scenario = {
            "name": f"batch-{date}",
            "description": f"Batch run for {date}",
            "date": date,
            "universe": list(universe),
            "bars": None,  # corpus mode
            "config_overrides": dict(config_overrides or {}),
            "expected": {},
        }
        runner = SimulatorRunner(scenario=scenario, quiet=True)
        state = runner.run()
        return _summarize_day(state, scenario, time.time() - t0)
    except Exception as exc:
        return {
            "date": date,
            "name": f"batch-{date}",
            "universe": list(universe),
            "entries": [],
            "exits": [],
            "alpaca_orders": [],
            "realized_pl": {},
            "realized_pl_total": 0.0,
            "open_at_eod": [],
            "telegram_count": 0,
            "fmp_count": 0,
            "yahoo_count": 0,
            "elapsed_s": time.time() - t0,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


@dataclass
class BatchConfig:
    """Knobs for run_days."""
    workers: int = 0   # 0 -> mp.cpu_count() // 2
    show_progress: bool = True
    progress_stream = sys.stderr
    config_overrides: Dict[str, str] = field(default_factory=dict)
    corpus_root: str = "data"


def run_days(
    dates: Sequence[str],
    universe: Sequence[str],
    cfg: Optional[BatchConfig] = None,
) -> List[dict]:
    """Run the simulator on each date in `dates` and return a list of
    per-day summary dicts in input order.

    Parallelism: multiprocessing.Pool, one worker per CPU half by default.
    Override via BatchConfig(workers=N).
    """
    cfg = cfg or BatchConfig()
    workers = cfg.workers or max(1, (_mp.cpu_count() // 2))
    dates = list(dates)
    universe = list(universe)
    args_list = [
        (d, universe, dict(cfg.config_overrides), cfg.corpus_root)
        for d in dates
    ]

    if not args_list:
        return []

    results: List[Optional[dict]] = [None] * len(args_list)
    completed = 0
    t_start = time.time()

    if workers == 1 or len(args_list) == 1:
        # Single-process path (easier debugging / smaller suites).
        for i, args in enumerate(args_list):
            results[i] = _run_one_day(args)
            completed += 1
            _emit_progress(cfg, completed, len(args_list), results[i], t_start)
    else:
        # multiprocessing pool. imap preserves input order.
        ctx = _mp.get_context("spawn")
        with ctx.Pool(processes=workers) as pool:
            for i, res in enumerate(pool.imap(_run_one_day, args_list, chunksize=1)):
                results[i] = res
                completed += 1
                _emit_progress(cfg, completed, len(args_list), res, t_start)

    if cfg.show_progress:
        cfg.progress_stream.write("\n")
        cfg.progress_stream.flush()

    return [r for r in results if r is not None]


def _emit_progress(cfg: BatchConfig, n: int, total: int, last: dict, t_start: float):
    if not cfg.show_progress:
        return
    elapsed = time.time() - t_start
    rate = n / elapsed if elapsed > 0 else 0.0
    eta = (total - n) / rate if rate > 0 else 0.0
    pct = n * 100 // total
    bar_width = 24
    filled = int(bar_width * n / total)
    bar = "#" * filled + "-" * (bar_width - filled)
    err_mark = "  ERR" if last.get("error") else ""
    line = (
        f"\r[batch] [{bar}] {n}/{total} ({pct}%)  "
        f"last={last.get('date')!s:<10}  "
        f"entries={len(last.get('entries', [])):>2d}  "
        f"P&L=${last.get('realized_pl_total', 0):+8.2f}  "
        f"rate={rate:.1f}/s  eta={eta:>4.0f}s{err_mark}"
    )
    cfg.progress_stream.write(line[:140])
    cfg.progress_stream.flush()


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def _main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="Simulator batch runner")
    p.add_argument("--dates", help="Comma-separated YYYY-MM-DD dates")
    p.add_argument("--from", dest="from_date", help="Inclusive start date YYYY-MM-DD")
    p.add_argument("--to", dest="to_date", help="Inclusive end date YYYY-MM-DD")
    p.add_argument("--tickers", default="AAPL,MSFT,NVDA,AMZN,META,GOOG,AVGO,NFLX,ORCL,TSLA,QQQ,SPY",
                   help="Comma-separated universe")
    p.add_argument("--workers", type=int, default=0,
                   help="Worker count (0 = cpu_count/2)")
    p.add_argument("--corpus-root", default="data")
    p.add_argument("--out", help="Write per-day JSON to this file")
    args = p.parse_args(argv)

    dates = _resolve_dates(args)
    universe = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    cfg = BatchConfig(workers=args.workers, corpus_root=args.corpus_root)

    results = run_days(dates, universe, cfg)

    total_entries = sum(len(r.get("entries", [])) for r in results)
    total_pl = sum(r.get("realized_pl_total", 0.0) for r in results)
    errors = [r for r in results if r.get("error")]

    print(f"\n[batch] complete: {len(results)} days, "
          f"{total_entries} total entries, "
          f"${total_pl:+.2f} total realized P&L, "
          f"{len(errors)} errors")
    if errors:
        print("Errors:")
        for e in errors[:10]:
            print(f"  {e['date']}: {e['error']}")

    if args.out:
        with open(args.out, "w") as fh:
            json.dump(results, fh, indent=2, default=str)
        print(f"[batch] wrote {len(results)} day results to {args.out}")
    return 0 if not errors else 1


def _resolve_dates(args) -> List[str]:
    if args.dates:
        return [d.strip() for d in args.dates.split(",") if d.strip()]
    if args.from_date and args.to_date:
        return _list_corpus_dates_between(args.from_date, args.to_date)
    raise SystemExit("Provide --dates A,B,C or --from YYYY-MM-DD --to YYYY-MM-DD")


def _list_corpus_dates_between(from_d: str, to_d: str, root: str = "data") -> List[str]:
    """Inclusive date list from on-disk corpus directories."""
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        if len(name) == 10 and name[4] == "-" and name[7] == "-":
            if from_d <= name <= to_d:
                out.append(name)
    return out


if __name__ == "__main__":
    sys.exit(_main())
