"""Multi-cell sweep harness for the broad-universe ORB backtest.

Runs N variants of (signal × top_k × concurrent_risk_dollars × min_dollar_vol)
in sequence, capturing each cell's summary into one comparison table.

Usage:
    python tools/orb_broad_sweep.py \\
        --pm-corpus data_pm_universe \\
        --universe data/universe/sp500.json \\
        --start 2025-01-02 --end 2026-05-15 \\
        --out results/broad_universe/sweeps/run-001

Cells specified via --cells (JSON list) or fall back to the default sweep
documented in __main__.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

DEFAULT_CELLS = [
    # Phase 1: signal comparison at top_k=10, cap=2000, mdv=1M
    {"signal": "gap",       "top_k": 10, "cap": 2000, "mdv": 1_000_000},
    {"signal": "volume",    "top_k": 10, "cap": 2000, "mdv": 1_000_000},
    {"signal": "range",     "top_k": 10, "cap": 2000, "mdv": 1_000_000},
    {"signal": "composite", "top_k": 10, "cap": 2000, "mdv": 1_000_000},
    # Phase 2: top_k sweep at the strongest single signal (will need re-run if winner != composite)
    {"signal": "composite", "top_k":  5, "cap": 2000, "mdv": 1_000_000},
    {"signal": "composite", "top_k": 20, "cap": 2000, "mdv": 1_000_000},
    # Phase 3: concurrent-cap relief at top_k=10 composite
    {"signal": "composite", "top_k": 10, "cap": 3000, "mdv": 1_000_000},
    {"signal": "composite", "top_k": 10, "cap": 4000, "mdv": 1_000_000},
    # Phase 4: min-dollar-volume filter (higher = bias to large caps)
    {"signal": "composite", "top_k": 10, "cap": 2000, "mdv": 10_000_000},
    {"signal": "composite", "top_k": 10, "cap": 2000, "mdv":    100_000},
]


def _cell_id(cell: dict) -> str:
    base = f"sig={cell['signal']}_topk={cell['top_k']}_cap={cell['cap']}_mdv={cell['mdv']}"
    eo = cell.get("env_overrides") or {}
    if eo:
        # short suffix for env overrides so cell dirs are unique
        suffix = "_".join(f"{k.replace('ORB_','')}={v}" for k, v in sorted(eo.items()))
        base += f"__{suffix}"
    return base


def _run_one_cell(args: tuple) -> dict:
    """Module-level worker so ProcessPoolExecutor can pickle it."""
    cell, base_env, repo_str, out_root_str, pm_corpus, universe, start, end = args
    cid = _cell_id(cell)
    out_root = Path(out_root_str)
    cell_out = out_root / cid
    env = dict(base_env)
    env["ORB_MAX_CONCURRENT_RISK_DOLLARS"] = str(cell["cap"])
    for k, v in (cell.get("env_overrides") or {}).items():
        env[k] = str(v)
    cmd = [
        sys.executable, "tools/orb_broad_backtest.py",
        "--pm-corpus", pm_corpus,
        "--universe", universe,
        "--start", start,
        "--end", end,
        "--out", str(cell_out),
        "--signal", str(cell["signal"]),
        "--top-k", str(cell["top_k"]),
        "--min-dollar-vol", str(cell["mdv"]),
        "--vid", cid,
    ]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=repo_str, env=env, capture_output=True, text=True)
    dt = time.time() - t0
    if proc.returncode != 0:
        return {"cid": cid, **cell, "status": "FAIL",
                "stderr": proc.stderr[-300:], "wall_seconds": round(dt, 1)}
    try:
        summary = json.loads((cell_out / "summary.json").read_text())
    except Exception as e:
        return {"cid": cid, **cell, "status": "FAIL",
                "stderr": str(e), "wall_seconds": round(dt, 1)}
    return {
        "cid": cid,
        **cell,
        "status": "OK",
        "days_ran": summary["days_ran"],
        "trades": summary["trades"],
        "win_rate_pct": summary["win_rate_pct"],
        "net_pnl": summary["net_pnl"],
        "ending_account": summary["ending_account"],
        "per_quarter_pnl": summary.get("per_quarter_pnl", {}),
        "per_quarter_trades": summary.get("per_quarter_trades", {}),
        "wall_seconds": round(dt, 1),
    }


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pm-corpus", default="data_pm_universe")
    p.add_argument("--universe", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--cells", default="",
                   help="JSON list overriding DEFAULT_CELLS")
    p.add_argument("--keystone-env", action="store_true",
                   help="Inject Keystone + R21 + R26 + cap=1.9 levers (same as production)")
    p.add_argument("--workers", type=int, default=8,
                   help="Parallel sweep cells (this machine has 18 logical cores)")
    args = p.parse_args(argv[1:])

    cells = json.loads(args.cells) if args.cells else DEFAULT_CELLS
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    repo = Path(__file__).resolve().parent.parent
    base_env = os.environ.copy()
    if args.keystone_env:
        keystone = {
            "ORB_OR_MINUTES": "30", "ORB_RR": "2.5", "ORB_RISK_PER_TRADE_PCT": "1.0",
            "ORB_RANGE_MIN_PCT": "0.008", "ORB_RANGE_MAX_PCT": "0.025",
            "ORB_MAX_TRADES_PER_DAY": "5",
            "ORB_DAILY_LOSS_KILL_PCT": "2.0",
            "ORB_ATR_STOP_MULT": "1.75", "ORB_ATR_LOOKBACK_5M": "14",
            "ORB_PARTIAL_PROFIT_AT_1R": "1", "ORB_MOVE_TO_BE_AFTER_1R": "1",
            "ORB_STOP_BUFFER_BPS": "5.0", "ORB_ENTRY_SLIPPAGE_BPS": "1.5",
            "ORB_EXIT_SLIPPAGE_BPS": "1.5", "ORB_STOP_KICK_BPS": "5.0",
            "ORB_SHORT_PENALTY_BPS": "1.0", "ORB_MAX_TRADE_NOTIONAL_PCT": "75",
            "ORB_SKIP_GAP_ABOVE_PCT": "1.5",
            "ORB_SKIP_PRIOR_SPY_RET_LT_BPS": "-40.0",
            "ORB_SKIP_EARNINGS_WINDOW": "1",
            "ORB_TIME_CUTOFF_ET": "11:00", "ORB_EOD_CUTOFF_ET": "15:55",
            "ORB_ACCOUNT": "100000", "ORB_COMPOUND_DAILY": "1",
            "ORB_TICKER_SIDE_BLOCKLIST": "{}",
            "ORB_MAX_VWAP_DEV_BPS": "15.0",
            "ORB_MAX_VWAP_DEV_TICKERS":
                "META,MSFT,AAPL,AMZN,GOOG,AVGO",
            "ORB_SKIP_VIX_ABOVE": "25.0",
            "ORB_POST_TRADE_COOLDOWN_MIN": "10",
            "ORB_RUNNER_EOD_PREP_ET": "14:00",
            "ORB_STALE_FULL_EXIT_ET": "14:30",
            "ORB_MAX_CONCURRENT_NOTIONAL_MULT": "1.9",
        }
        base_env.update(keystone)

    rows: list[dict] = []
    t_overall = time.time()
    n_cells = len(cells)
    print(f"Running {n_cells} cells with {args.workers}-way parallelism", flush=True)

    worker_args = [
        (cell, base_env, str(repo), str(out_root), args.pm_corpus,
         args.universe, args.start, args.end)
        for cell in cells
    ]

    if args.workers <= 1:
        for i, wa in enumerate(worker_args, 1):
            cid = _cell_id(wa[0])
            print(f"\n=== [{i}/{n_cells}] {cid} ===", flush=True)
            r = _run_one_cell(wa)
            rows.append(r)
            net = r.get("net_pnl", float("nan"))
            tr = r.get("trades", 0)
            wr = r.get("win_rate_pct", float("nan"))
            print(f"  net=${net:>+10,.0f}  trades={tr:>5}  WR={wr:.1f}%  "
                  f"{r['wall_seconds']:.1f}s  [{r['status']}]", flush=True)
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(_run_one_cell, wa): wa for wa in worker_args}
            done = 0
            for fut in as_completed(futs):
                done += 1
                r = fut.result()
                rows.append(r)
                net = r.get("net_pnl", float("nan"))
                tr = r.get("trades", 0)
                wr = r.get("win_rate_pct", float("nan"))
                print(f"[{done:>2}/{n_cells}] {r['cid']:60s}  "
                      f"net=${net:>+10,.0f}  trades={tr:>5}  WR={wr:.1f}%  "
                      f"{r['wall_seconds']:.1f}s  [{r['status']}]", flush=True)

    # Comparison table
    rows.sort(key=lambda r: r.get("net_pnl", -1e9), reverse=True)
    out_root.mkdir(parents=True, exist_ok=True)
    with open(out_root / "comparison.json", "w") as f:
        json.dump({"cells": rows, "wall_minutes": round((time.time()-t_overall)/60, 1)}, f, indent=2)

    print("\n=== SWEEP COMPARISON (ranked by net P&L) ===")
    print(f"{'#':>2}  {'signal':>10}  {'top_k':>5}  {'cap':>5}  "
          f"{'mdv':>12}  {'trades':>7}  {'WR%':>5}  {'net $':>12}")
    print("-" * 80)
    for i, r in enumerate(rows, 1):
        if r["status"] != "OK":
            print(f"{i:>2}  {r['signal']:>10}  {r['top_k']:>5}  {r['cap']:>5}  "
                  f"{r['mdv']:>12,}  -- FAIL --")
            continue
        print(f"{i:>2}  {r['signal']:>10}  {r['top_k']:>5}  {r['cap']:>5}  "
              f"{r['mdv']:>12,}  {r['trades']:>7}  {r['win_rate_pct']:>5.1f}  "
              f"{r['net_pnl']:>+12,.0f}")

    print(f"\nWrote {out_root / 'comparison.json'} ({len(rows)} cells)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
