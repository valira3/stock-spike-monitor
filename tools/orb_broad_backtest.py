"""Broad-universe ORB backtest harness.

Per day: scan premarket for breakout setups → take top-K → run the
existing orb_backtest.run_ticker_day engine on each pick. Writes
per-day JSON files in the same schema as tools/orb_backtest.py so
tools/combined_replay.py consumes the output without modification.

Why a separate tool: tools/orb_backtest.py has a strict fixed-universe
loader (discover_dates requires every ticker to have a file on every
day) and pre-builds a pkl cache for all tickers. A 504-ticker run
through that path would blow memory and OOM-style stall. This harness
trades the pkl cache for lazy per-ticker loading at the cost of slower
runs — fine for a research sweep against the broad universe.

Usage:
    python tools/orb_broad_backtest.py \\
        --pm-corpus data_pm_universe \\
        --universe data/universe/sp500.json \\
        --signal composite --top-k 10 \\
        --start 2025-01-02 --end 2026-05-15 \\
        --out results/broad_universe/baseline

Levers (env):
    ORB_DYNAMIC_UNIVERSE_SIGNAL    overrides --signal
    ORB_DYNAMIC_UNIVERSE_TOP_K     overrides --top-k
    ORB_DYNAMIC_UNIVERSE_MIN_PM_BARS  default 10
    ORB_DYNAMIC_UNIVERSE_MIN_DOLLAR_VOL  default 100_000

Plus every existing ORB_* env in tools/orb_backtest.py:ORBConfig.from_env.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# orb_backtest is a big module; import the bits we need.
from tools.orb_backtest import (  # type: ignore
    Bar1m,
    ORBConfig,
    SESSION_START_ET,
    _bucket_to_minutes,
    _ts_to_et_bucket_minutes,
    run_ticker_day,
)
from orb.premarket_scanner import scan_day
from orb.scanner_cache import load_cache


def _trading_days(start: date, end: date) -> list[date]:
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _load_bars(pm_corpus: Path, date_str: str, ticker: str) -> list[Bar1m]:
    """Load 1-min bars (premarket + RTH) for one (date, ticker) into Bar1m."""
    fp = pm_corpus / date_str / f"{ticker}.jsonl"
    if not fp.is_file():
        return []
    bars: list[Bar1m] = []
    try:
        for line in fp.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ts_str = rec.get("ts", "")
            bkt = _ts_to_et_bucket_minutes(ts_str) if ts_str else -1
            if bkt < 0:
                bkt = _bucket_to_minutes(rec.get("et_bucket", ""))
            if bkt < 0:
                continue
            bars.append(
                Bar1m(
                    bucket=bkt,
                    open=float(rec.get("open", 0)),
                    high=float(rec.get("high", 0)),
                    low=float(rec.get("low", 0)),
                    close=float(rec.get("close", 0)),
                    volume=float(rec.get("total_volume") or rec.get("volume") or 0),
                )
            )
    except Exception:
        return []
    bars.sort(key=lambda b: b.bucket)
    return bars


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pm-corpus", default="data_pm_universe",
                   help="Root with full-day bars (premarket+RTH) for the broad universe")
    p.add_argument("--universe", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--signal", default=os.environ.get("ORB_DYNAMIC_UNIVERSE_SIGNAL", "composite"),
                   choices=["gap", "volume", "range", "composite"])
    p.add_argument("--top-k", type=int,
                   default=int(os.environ.get("ORB_DYNAMIC_UNIVERSE_TOP_K", "10")))
    p.add_argument("--min-pm-bars", type=int,
                   default=int(os.environ.get("ORB_DYNAMIC_UNIVERSE_MIN_PM_BARS", "10")))
    p.add_argument("--min-dollar-vol", type=float,
                   default=float(os.environ.get("ORB_DYNAMIC_UNIVERSE_MIN_DOLLAR_VOL", "100000")))
    p.add_argument("--vid", default="orb_broad_universe")
    args = p.parse_args(argv[1:])

    pm = Path(args.pm_corpus)
    if not pm.is_dir():
        print(f"ERROR: pm-corpus {pm} not found", file=sys.stderr)
        return 1

    uni = json.loads(Path(args.universe).read_text())
    tickers = uni["tickers"]

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    all_days = _trading_days(start, end)
    # Filter to days that exist on disk
    days = [d for d in all_days if (pm / d.isoformat()).is_dir()]

    cfg = ORBConfig.from_env()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "per_day").mkdir(parents=True, exist_ok=True)

    # Load the feature cache once (built by tools/build_scanner_cache.py).
    feature_cache = load_cache(pm)
    cache_note = f"feature_cache={len(feature_cache):,} rows" if feature_cache else "feature_cache=MISS (slow path)"

    print(f"Broad backtest: signal={args.signal} top_k={args.top_k} "
          f"universe={len(tickers)} days={len(days)}/{len(all_days)} "
          f"corpus={pm} {cache_note}", flush=True)

    # Compounding state (mirrors orb_backtest.run)
    starting_account = cfg.account
    running_account = starting_account

    grand_pairs: list[dict] = []
    grand_picks: list[dict] = []
    summary_per_day: list[dict] = []
    t0 = time.time()

    for d_idx, d in enumerate(days):
        date_str = d.isoformat()

        # Pre-day scan (uses feature cache when present; otherwise reads JSONLs)
        picks = scan_day(
            pm, date_str, tickers,
            signal=args.signal, top_k=args.top_k,
            min_pm_bars=args.min_pm_bars,
            min_dollar_volume=args.min_dollar_vol,
            feature_cache=feature_cache,
        )
        if not picks:
            continue

        picks_record = {
            "date": date_str,
            "picks": [
                {"ticker": r.ticker, "score": round(r.score, 4),
                 "gap_pct": round(r.gap_pct * 100, 3),
                 "pm_dollar_volume": round(r.pm_dollar_volume, 0),
                 "pm_range_pct": round(r.pm_range_pct * 100, 3)}
                for r in picks
            ],
        }
        grand_picks.append(picks_record)

        day_pairs: list[dict] = []
        for r in picks:
            bars = _load_bars(pm, date_str, r.ticker)
            if not bars:
                continue
            pairs = run_ticker_day(date_str, r.ticker, bars, cfg, current_account=running_account)
            day_pairs.extend(pairs)

        # Compound daily P&L into running_account
        day_pnl = sum(p.get("pnl_dollars", 0.0) for p in day_pairs)
        if cfg.compound_daily:
            running_account = max(running_account + day_pnl, 1.0)

        # Per-day JSON file
        with open(out_dir / "per_day" / f"{date_str}.json", "w") as f:
            json.dump(
                {
                    "date": date_str,
                    "n_picks": len(picks),
                    "scanner_picks": picks_record["picks"],
                    "pnl_pairs": day_pairs,
                    "day_pnl": day_pnl,
                    "running_account": running_account,
                },
                f,
            )

        grand_pairs.extend(day_pairs)
        summary_per_day.append({
            "date": date_str,
            "picks": len(picks),
            "trades": len(day_pairs),
            "day_pnl": day_pnl,
            "running_account": running_account,
        })

        if (d_idx + 1) % 25 == 0 or d_idx == len(days) - 1:
            elapsed = time.time() - t0
            print(f"  [{d_idx+1:>3}/{len(days)}] {date_str}  "
                  f"day_pnl=${day_pnl:>+9,.0f}  acct=${running_account:>10,.0f}  "
                  f"t={elapsed:>5.0f}s", flush=True)

    total_pnl = running_account - starting_account
    wins = sum(1 for p in grand_pairs if p.get("pnl_dollars", 0) > 0)
    losses = sum(1 for p in grand_pairs if p.get("pnl_dollars", 0) < 0)

    # Per-quarter breakdown so we can spot in-sample fits / time-period overfitting
    by_quarter: dict[str, float] = {}
    quarter_trades: dict[str, int] = {}
    for row in summary_per_day:
        d = date.fromisoformat(row["date"])
        q = f"{d.year}-Q{(d.month - 1) // 3 + 1}"
        by_quarter[q] = by_quarter.get(q, 0.0) + row["day_pnl"]
        quarter_trades[q] = quarter_trades.get(q, 0) + row["trades"]

    summary = {
        "variant": args.vid,
        "signal": args.signal,
        "top_k": args.top_k,
        "universe_size": len(tickers),
        "days_ran": len(summary_per_day),
        "trades": len(grand_pairs),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(100 * wins / max(wins + losses, 1), 2),
        "starting_account": starting_account,
        "ending_account": running_account,
        "net_pnl": round(total_pnl, 2),
        "compound_daily": cfg.compound_daily,
        "wall_seconds": round(time.time() - t0, 1),
        "per_quarter_pnl": {q: round(by_quarter[q], 2) for q in sorted(by_quarter)},
        "per_quarter_trades": {q: quarter_trades[q] for q in sorted(quarter_trades)},
    }
    with open(out_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    # Also dump the scanner picks list (useful for diagnostics)
    with open(out_dir / "scanner_picks.json", "w") as f:
        json.dump({"picks_by_day": grand_picks}, f)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
