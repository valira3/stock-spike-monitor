"""simulator.annual -- full-year P&L via the simulator.

Runs every corpus day in parallel, aggregates per-day outcomes into an
annualized P&L + trading report. Output schema mirrors what
tools/orb_backtest.py emits so existing dashboard analysis tools still
parse it.

NOTE: This is an INTEGRATION-level backtest (full bot decision path
incl. broker fire, telegram, persistence). For pure-strategy sweeps
across thousands of lever permutations, tools/orb_backtest.py is still
faster.

CLI:
    python -m simulator.annual --from 2025-01-02 --to 2025-12-31
    python -m simulator.annual --all
    python -m simulator.annual --year 2026
    python -m simulator.annual --all --workers 8 --out results/sim/2026.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from typing import Dict, List, Optional

from simulator.batch import BatchConfig, _list_corpus_dates_between, run_days
from simulator.diff import diff_one_day, load_trade_log


# ----------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------


def aggregate_live_divergence(
    results: List[dict],
    trade_log_path: str = "data/trade_log.jsonl",
) -> Optional[dict]:
    """For every day in `results`, load the live trade log and diff
    simulator entries against live trades. Return None if the trade
    log file is absent (e.g. CI / sandbox); otherwise return a dict
    with per-day diffs + roll-up counters."""
    import os as _os
    if not _os.path.isfile(trade_log_path):
        return None

    per_day = {}
    total_matched = 0
    total_drift = 0
    total_sim_only = 0
    total_live_only = 0
    days_with_divergence = 0
    days_with_live_trades = 0

    for r in results:
        date = r["date"]
        live = load_trade_log(date, path=trade_log_path)
        if not live and not r.get("entries"):
            continue  # nothing to diff on either side
        if live:
            days_with_live_trades += 1
        d = diff_one_day(r, live)
        per_day[date] = d
        total_matched += len(d["matched"]) - d["drift_count"]
        total_drift += d["drift_count"]
        total_sim_only += len(d["sim_only"])
        total_live_only += len(d["live_only"])
        if d["verdict"] == "DIVERGE":
            days_with_divergence += 1

    return {
        "trade_log_path": trade_log_path,
        "days_with_live_trades": days_with_live_trades,
        "days_with_divergence": days_with_divergence,
        "totals": {
            "matched": total_matched,
            "drift": total_drift,
            "sim_only": total_sim_only,
            "live_only": total_live_only,
        },
        "per_day": per_day,
    }


def aggregate(results: List[dict], starting_equity: float = 100_000.0,
              compound_daily: bool = True) -> dict:
    """Roll up per-day results into annualized metrics."""
    days = sorted(results, key=lambda r: r["date"])
    n_days = len(days)
    if n_days == 0:
        return {"error": "no days"}

    equity = starting_equity
    daily_equity: List[dict] = []
    total_entries = 0
    total_exits = 0
    pnl_by_symbol: Dict[str, float] = defaultdict(float)
    trades_by_symbol: Dict[str, int] = defaultdict(int)
    wins = 0
    losses = 0
    win_pnl_sum = 0.0
    loss_pnl_sum = 0.0
    biggest_win = 0.0
    biggest_loss = 0.0

    # Compute per-day P&L sequence (for max-drawdown + Sharpe).
    pnl_series: List[float] = []
    pnl_pct_series: List[float] = []

    for d in days:
        d_pl = float(d.get("realized_pl_total", 0.0))
        total_entries += len(d.get("entries", []))
        total_exits += len(d.get("exits", []))
        for sym, pl in d.get("realized_pl", {}).items():
            pnl_by_symbol[sym] += float(pl)
            trades_by_symbol[sym] += 1
            if pl > 0:
                wins += 1
                win_pnl_sum += pl
                biggest_win = max(biggest_win, pl)
            elif pl < 0:
                losses += 1
                loss_pnl_sum += pl
                biggest_loss = min(biggest_loss, pl)

        prev_equity = equity
        if compound_daily:
            # Compound the day's P&L on a fresh equity base.
            equity = equity + d_pl
        else:
            equity = starting_equity + sum(float(x.get("realized_pl_total", 0))
                                           for x in days[: days.index(d) + 1])
        pnl_series.append(d_pl)
        pnl_pct_series.append(d_pl / prev_equity if prev_equity else 0.0)
        daily_equity.append({"date": d["date"], "equity": round(equity, 2),
                             "day_pl": round(d_pl, 2),
                             "entries": len(d.get("entries", []))})

    total_pl = equity - starting_equity
    total_pct = (total_pl / starting_equity) * 100.0

    # Max drawdown.
    peak = starting_equity
    max_dd = 0.0
    max_dd_pct = 0.0
    for row in daily_equity:
        e = row["equity"]
        if e > peak:
            peak = e
        dd = peak - e
        dd_pct = (dd / peak) * 100.0 if peak else 0.0
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd_pct

    # Sharpe (daily, annualized via sqrt(252)).
    sharpe = 0.0
    if len(pnl_pct_series) > 1:
        mean = sum(pnl_pct_series) / len(pnl_pct_series)
        var = sum((x - mean) ** 2 for x in pnl_pct_series) / len(pnl_pct_series)
        std = var ** 0.5
        if std > 0:
            sharpe = (mean / std) * (252 ** 0.5)

    n_trades = wins + losses
    win_rate = (wins / n_trades * 100.0) if n_trades else 0.0
    avg_win = (win_pnl_sum / wins) if wins else 0.0
    avg_loss = (loss_pnl_sum / losses) if losses else 0.0
    profit_factor = (win_pnl_sum / abs(loss_pnl_sum)) if loss_pnl_sum < 0 else 0.0

    # Quarterly breakdown.
    by_quarter: Dict[str, float] = defaultdict(float)
    for d in days:
        date = d["date"]
        q = f"{date[:4]}-Q{((int(date[5:7]) - 1) // 3) + 1}"
        by_quarter[q] += float(d.get("realized_pl_total", 0.0))

    # Top-5 symbol contributors.
    top_symbols = sorted(pnl_by_symbol.items(), key=lambda kv: -abs(kv[1]))[:8]

    return {
        "schema": "simulator.annual v1",
        "n_days": n_days,
        "n_entries": total_entries,
        "n_exits": total_exits,
        "n_trades_closed": n_trades,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "biggest_win": round(biggest_win, 2),
        "biggest_loss": round(biggest_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "starting_equity": round(starting_equity, 2),
        "ending_equity": round(equity, 2),
        "total_pl": round(total_pl, 2),
        "total_pct": round(total_pct, 2),
        "max_drawdown_dollars": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "sharpe_annualized": round(sharpe, 2),
        "by_quarter": {k: round(v, 2) for k, v in sorted(by_quarter.items())},
        "top_symbols": [
            {"symbol": s, "pl": round(p, 2), "trades": trades_by_symbol[s]}
            for s, p in top_symbols
        ],
        "daily_equity_curve": daily_equity,
    }


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------


def _print_annual_report(agg: dict, dates_range: str):
    print()
    print("=" * 78)
    print(f" Annual Backtest Report  ({dates_range})")
    print("=" * 78)
    print()
    print(f"  Days traded:           {agg['n_days']}")
    print(f"  Total entries:         {agg['n_entries']}")
    print(f"  Total trades closed:   {agg['n_trades_closed']}")
    print()
    print(f"  Starting equity:       ${agg['starting_equity']:>12,.2f}")
    print(f"  Ending equity:         ${agg['ending_equity']:>12,.2f}")
    print(f"  Total P&L:             ${agg['total_pl']:>+12,.2f}  ({agg['total_pct']:+.2f}%)")
    print(f"  Max drawdown:          ${agg['max_drawdown_dollars']:>12,.2f}  "
          f"({agg['max_drawdown_pct']:+.2f}%)")
    print()
    print(f"  Win rate:              {agg['win_rate_pct']:>6.1f}%   "
          f"({agg['wins']} wins / {agg['losses']} losses)")
    print(f"  Avg win:               ${agg['avg_win']:>+10.2f}")
    print(f"  Avg loss:              ${agg['avg_loss']:>+10.2f}")
    print(f"  Biggest win:           ${agg['biggest_win']:>+10.2f}")
    print(f"  Biggest loss:          ${agg['biggest_loss']:>+10.2f}")
    print(f"  Profit factor:         {agg['profit_factor']:>6.2f}")
    print(f"  Sharpe (annualized):   {agg['sharpe_annualized']:>6.2f}")
    print()

    if agg.get("by_quarter"):
        print("  Quarterly P&L")
        for q, pl in agg["by_quarter"].items():
            print(f"    {q}      ${pl:>+12,.2f}")
        print()

    if agg.get("top_symbols"):
        print("  Top symbol contributors")
        for row in agg["top_symbols"]:
            print(f"    {row['symbol']:6s}  ${row['pl']:>+10,.2f}   "
                  f"({row['trades']} trades)")
        print()


def _print_divergence_block(div: dict):
    """Pretty-print the live-vs-sim divergence summary."""
    print("  Live-vs-sim divergence")
    print(f"    trade log:               {div['trade_log_path']}")
    print(f"    days with live trades:   {div['days_with_live_trades']}")
    print(f"    days with divergence:    {div['days_with_divergence']}")
    t = div["totals"]
    print(f"    MATCH:    {t['matched']:>4d}")
    print(f"    DRIFT:    {t['drift']:>4d}   "
          f"(same key, entry price > $0.10 apart)")
    print(f"    SIM-ONLY: {t['sim_only']:>4d}   "
          f"(simulator fired, live did not)")
    print(f"    LIVE-ONLY:{t['live_only']:>4d}   "
          f"(live fired, simulator did not)")
    if div["days_with_divergence"] > 0:
        print()
        print("  Days flagged for review:")
        for date, d in sorted(div["per_day"].items()):
            if d["verdict"] != "DIVERGE":
                continue
            issues = []
            if d["sim_only"]:
                issues.append(f"sim-only {','.join(d['sim_only'])}")
            if d["live_only"]:
                issues.append(f"live-only {','.join(d['live_only'])}")
            if d["drift_count"]:
                issues.append(f"drift x{d['drift_count']}")
            print(f"    {date}  {' | '.join(issues)}")
    print()


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def _main(argv=None):
    p = argparse.ArgumentParser(description="Simulator-based annual P&L")
    p.add_argument("--from", dest="from_date")
    p.add_argument("--to", dest="to_date")
    p.add_argument("--year", help="Convenience: --year 2025")
    p.add_argument("--all", action="store_true")
    p.add_argument("--corpus-root", default="data")
    p.add_argument("--tickers", default="AAPL,MSFT,NVDA,AMZN,META,GOOG,AVGO,NFLX,ORCL,TSLA,QQQ,SPY")
    p.add_argument("--workers", type=int, default=0)
    p.add_argument("--starting-equity", type=float, default=100_000.0)
    p.add_argument("--no-compound", action="store_true")
    p.add_argument("--diff-live", action="store_true",
                   help="Also diff sim outcomes vs data/trade_log.jsonl per day")
    p.add_argument("--trade-log", default="data/trade_log.jsonl",
                   help="Path to live trade log JSONL (when --diff-live)")
    p.add_argument("--out-json", default="results/simulator/annual.json",
                   help="Where to write the aggregated report")
    args = p.parse_args(argv)

    # Resolve date range.
    if args.year:
        from_d = f"{args.year}-01-01"
        to_d = f"{args.year}-12-31"
    elif args.all:
        from_d = "1900-01-01"
        to_d = "9999-12-31"
    else:
        from_d = args.from_date
        to_d = args.to_date
    if not (from_d and to_d):
        raise SystemExit("Provide --year, --all, or --from --to")

    dates = _list_corpus_dates_between(from_d, to_d, root=args.corpus_root)
    if not dates:
        raise SystemExit(f"No corpus days in [{from_d}, {to_d}] under {args.corpus_root}")
    print(f"[annual] running {len(dates)} days  workers={args.workers or 'auto'}")
    universe = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    cfg = BatchConfig(workers=args.workers, corpus_root=args.corpus_root)

    results = run_days(dates, universe, cfg)
    agg = aggregate(
        results,
        starting_equity=args.starting_equity,
        compound_daily=not args.no_compound,
    )

    range_str = f"{dates[0]} -> {dates[-1]}, {len(dates)} days"
    _print_annual_report(agg, range_str)

    # Optional live-vs-sim divergence block.
    divergence = None
    if args.diff_live:
        divergence = aggregate_live_divergence(results, trade_log_path=args.trade_log)
        if divergence is None:
            print(f"  (--diff-live skipped: trade log not found at {args.trade_log})")
            print()
        else:
            _print_divergence_block(divergence)

    if args.out_json:
        os.makedirs(os.path.dirname(args.out_json), exist_ok=True)
        agg_full = {"meta": {"range": range_str, "universe": universe,
                             "from": from_d, "to": to_d},
                    "summary": agg,
                    "per_day": results}
        if divergence is not None:
            agg_full["divergence"] = divergence
        with open(args.out_json, "w") as fh:
            json.dump(agg_full, fh, indent=2, default=str)
        print(f"[annual] wrote {args.out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
