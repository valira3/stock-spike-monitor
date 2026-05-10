"""Stability-aware ranking of GHA sweep variants.

Reads from origin/sweep-results: sweeps/<run-id>/<vid>/{summary.json, per_day/*.json}.

Computes per-variant:
  - net_pnl, entries, win_rate (from summary.json)
  - daily_sharpe (annualized): mean(daily_pnl) / std(daily_pnl) * sqrt(252)
  - max_drawdown (peak-to-trough cumulative P&L, in dollars)
  - pct_profitable_days
  - worst_day_pnl
  - per_ticker_top_share: top ticker's share of net P&L (lower = more diverse)
  - per_side_balance: |long_pnl - short_pnl| / |long_pnl + short_pnl| (lower = more balanced)
  - per_month_cv: coefficient of variation across months (lower = more consistent)

Stability score (higher = more stable):
  stability = sigmoid(daily_sharpe) * (1 - per_ticker_top_share) * (1 - per_side_balance) * pct_profitable_days

Combined rank score:
  combined = net_pnl * stability_multiplier  (stability_multiplier in [0.5, 1.5])

Usage:
  python tools/sweep_stability_analyzer.py <run-id>             # all variants in run
  python tools/sweep_stability_analyzer.py <run-id> --top 5     # top-5 by combined score
  python tools/sweep_stability_analyzer.py --runs run-A run-B   # combine multiple runs
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _git(*args: str) -> tuple[str, int]:
    p = subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True,
    )
    return p.stdout, p.returncode


def list_variants(run_id: str) -> list[str]:
    out, _ = _git("ls-tree", "-r", "--name-only",
                  "origin/sweep-results", f"sweeps/{run_id}/")
    vids = set()
    for line in out.splitlines():
        # sweeps/<run-id>/<vid>/summary.json
        if line.endswith("/summary.json"):
            parts = line.split("/")
            if len(parts) >= 4:
                vids.add(parts[2])
    return sorted(vids)


def load_summary(run_id: str, vid: str) -> dict | None:
    out, rc = _git("show",
                   f"origin/sweep-results:sweeps/{run_id}/{vid}/summary.json")
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def list_per_day_files(run_id: str, vid: str) -> list[str]:
    out, _ = _git("ls-tree", "-r", "--name-only",
                  "origin/sweep-results",
                  f"sweeps/{run_id}/{vid}/per_day/")
    return [l for l in out.splitlines() if l.endswith(".json")]


def load_per_day(run_id: str, vid: str) -> list[dict]:
    files = list_per_day_files(run_id, vid)
    days = []
    for f in files:
        out, rc = _git("show", f"origin/sweep-results:{f}")
        if rc != 0:
            continue
        try:
            days.append(json.loads(out))
        except Exception:
            pass
    days.sort(key=lambda d: d.get("date", ""))
    return days


def compute_stability(days: list[dict]) -> dict:
    """Per-variant stability metrics from per_day data."""
    if not days:
        return {}

    daily_pnl = []
    pairs_all = []
    for d in days:
        pairs = d.get("pnl_pairs") or []
        day_pnl = sum(float(p.get("pnl_dollars", 0)) for p in pairs)
        daily_pnl.append((d.get("date"), day_pnl))
        for p in pairs:
            pairs_all.append({
                "date": d.get("date"),
                "ticker": p.get("ticker"),
                "side": p.get("side"),
                "pnl": float(p.get("pnl_dollars", 0)),
            })

    pnls = [p for _, p in daily_pnl]
    if not pnls:
        return {}

    mean = statistics.mean(pnls)
    std = statistics.stdev(pnls) if len(pnls) > 1 else 0.0
    sharpe = (mean / std * math.sqrt(252)) if std > 0 else 0.0

    # Drawdown
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for _, p in daily_pnl:
        cum += p
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > mdd:
            mdd = dd

    profitable_days = sum(1 for p in pnls if p > 0)
    pct_profit_days = profitable_days / len(pnls)
    worst_day = min(pnls)
    best_day = max(pnls)
    net = sum(pnls)

    # Per-ticker concentration
    by_ticker: dict[str, float] = defaultdict(float)
    for p in pairs_all:
        by_ticker[p["ticker"]] += p["pnl"]
    if net != 0:
        # contribution to NET P&L (signed): a ticker that loses while
        # variant is overall negative still "concentrates" the loss.
        # We use absolute share of total absolute P&L flow as the
        # diversification metric (less manipulable than signed share).
        total_abs = sum(abs(v) for v in by_ticker.values())
        if total_abs > 0:
            shares = sorted(
                (abs(v) / total_abs for v in by_ticker.values()), reverse=True
            )
            top_share = shares[0]
        else:
            top_share = 1.0
    else:
        top_share = 1.0

    # Per-side balance (in absolute dollars)
    long_pnl = sum(p["pnl"] for p in pairs_all if p["side"] == "long")
    short_pnl = sum(p["pnl"] for p in pairs_all if p["side"] == "short")
    s_abs = abs(long_pnl) + abs(short_pnl)
    if s_abs > 0:
        side_imbalance = abs(abs(long_pnl) - abs(short_pnl)) / s_abs
    else:
        side_imbalance = 0.0

    # Per-month CV
    by_month: dict[str, float] = defaultdict(float)
    for date, p in daily_pnl:
        if date and len(date) >= 7:
            by_month[date[:7]] += p
    monthly = list(by_month.values())
    if len(monthly) >= 2 and abs(statistics.mean(monthly)) > 0.01:
        cv = statistics.stdev(monthly) / abs(statistics.mean(monthly))
    else:
        cv = float("nan")

    return {
        "n_days": len(daily_pnl),
        "n_trades": len(pairs_all),
        "daily_sharpe": round(sharpe, 3),
        "max_drawdown": round(mdd, 2),
        "pct_profitable_days": round(pct_profit_days, 3),
        "worst_day_pnl": round(worst_day, 2),
        "best_day_pnl": round(best_day, 2),
        "top_ticker_share": round(top_share, 3),
        "side_imbalance": round(side_imbalance, 3),
        "monthly_cv": round(cv, 3) if not math.isnan(cv) else None,
        "long_pnl": round(long_pnl, 2),
        "short_pnl": round(short_pnl, 2),
        "by_ticker": {t: round(v, 2) for t, v in
                      sorted(by_ticker.items(), key=lambda x: x[1])},
    }


def stability_score(s: dict) -> float:
    """Combine into a 0..1 score. Higher = more stable.

    Components (each 0..1):
      sigmoid(sharpe / 1.5)     -- Sharpe normalized; |1.5| ~> 0.82
      1 - top_ticker_share      -- diversification (1.0 ticker dominates -> 0)
      1 - side_imbalance        -- L/S balance
      pct_profitable_days       -- direct
    Geometric mean to penalize any one weak axis.
    """
    sharpe = s.get("daily_sharpe", 0) or 0.0
    sharpe_norm = 1.0 / (1.0 + math.exp(-sharpe / 1.5))
    top = s.get("top_ticker_share", 1.0) or 1.0
    div_norm = max(0.0, 1.0 - top)
    side = s.get("side_imbalance", 1.0) or 1.0
    bal_norm = max(0.0, 1.0 - side)
    pct = s.get("pct_profitable_days", 0.0) or 0.0
    parts = [sharpe_norm, div_norm, bal_norm, pct]
    parts = [max(0.001, p) for p in parts]
    return round(math.exp(sum(math.log(p) for p in parts) / len(parts)), 4)


def combined_score(net_pnl: float, stability: float) -> float:
    """A simple combined score for ranking.

    Stability acts as a multiplier in [0.5, 1.5] of the net P&L.
    Negative P&L: stability still acts positively (stable losses
    are "better than" volatile ones since they're predictable).
    """
    multiplier = 0.5 + stability  # 0..1 -> 0.5..1.5
    return round(net_pnl * multiplier, 2)


def render(rows: list[dict], top_n: int | None = None) -> None:
    rows = sorted(rows, key=lambda r: r["combined"], reverse=True)
    if top_n:
        rows = rows[:top_n]

    headers = [
        ("vid", 50), ("net_pnl", 11), ("entries", 7), ("wr%", 6),
        ("sharpe", 7), ("mdd", 8), ("pct_profit_d", 6),
        ("top_tk", 6), ("L/S_imb", 7), ("monthly_cv", 6),
        ("stab", 6), ("score", 11),
    ]
    line = " ".join(f"{h:>{w}}" if w > 7 else f"{h:<{w}}"
                    for h, w in headers)
    print(line)
    print("-" * len(line))
    for r in rows:
        s = r["stability"]
        cells = [
            f"{r['vid'][:50]:<50}",
            f"${r['net_pnl']:>9.2f}",
            f"{r['entries']:>7}",
            f"{r['wr']:>5.1f}%",
            f"{s.get('daily_sharpe', 0):>7.3f}",
            f"{s.get('max_drawdown', 0):>8.2f}",
            f"{s.get('pct_profitable_days', 0):>6.2f}",
            f"{s.get('top_ticker_share', 0):>6.2f}",
            f"{s.get('side_imbalance', 0):>7.2f}",
            f"{(s.get('monthly_cv') or 0):>6.2f}",
            f"{r['stab']:>6.3f}",
            f"${r['combined']:>9.2f}",
        ]
        print(" ".join(cells))


def gather(run_ids: list[str]) -> list[dict]:
    rows = []
    for run_id in run_ids:
        for vid in list_variants(run_id):
            summary = load_summary(run_id, vid)
            if not summary:
                continue
            days = load_per_day(run_id, vid)
            stab = compute_stability(days)
            stab_score = stability_score(stab) if stab else 0.0
            net = float(summary.get("net_pnl", 0))
            row = {
                "run_id": run_id,
                "vid": vid,
                "net_pnl": net,
                "entries": summary.get("entries"),
                "wr": summary.get("win_rate_pct") or 0.0,
                "stability": stab,
                "stab": stab_score,
                "combined": combined_score(net, stab_score),
            }
            rows.append(row)
    return rows


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("run_id", nargs="?")
    p.add_argument("--runs", nargs="+", default=None)
    p.add_argument("--top", type=int, default=None)
    p.add_argument("--per-ticker", action="store_true",
                   help="Print per-ticker breakdown for each row")
    args = p.parse_args(argv[1:])

    _git("fetch", "-q", "origin", "sweep-results")

    runs = args.runs or ([args.run_id] if args.run_id else None)
    if not runs:
        # list recent runs
        out, _ = _git("ls-tree", "-r", "--name-only",
                      "origin/sweep-results", "sweeps/")
        seen = set()
        for line in out.splitlines():
            if line.startswith("sweeps/run-"):
                seen.add(line.split("/")[1])
        for r in sorted(seen, reverse=True)[:10]:
            print(r)
        return 0

    rows = gather(runs)
    if not rows:
        print("no variants found", file=sys.stderr)
        return 1
    render(rows, top_n=args.top)

    if args.per_ticker:
        print("\n--- per-ticker breakdown (top 5 by combined score) ---")
        for r in sorted(rows, key=lambda x: x["combined"], reverse=True)[:5]:
            print(f"\n{r['vid']}  net=${r['net_pnl']:.2f}")
            for tk, pnl in (r["stability"].get("by_ticker") or {}).items():
                print(f"  {tk:<8} ${pnl:>+8.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
