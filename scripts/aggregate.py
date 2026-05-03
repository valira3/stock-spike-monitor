"""Aggregate a backtest run directory into aggregate.json and pairs.json.

Usage
-----
    python scripts/aggregate.py                  # run from a backtest dir
    python /path/to/scripts/aggregate.py         # run from anywhere \u2014 ROOT resolves to script\u2019s dir

ROOT is derived from the script\u2019s own location so the file can be copied
into any backtest directory without manual path surgery.
"""
import json
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "raw"
DAYS_FILE = "/home/user/workspace/canonical_backtest_data/84day_2026_sip/days_84.txt"


def load_days():
    with open(DAYS_FILE) as f:
        return [d.strip() for d in f if d.strip()]


def _build_exit_reason_lookup(raw: dict) -> dict:
    """Return a dict mapping (ticker, side, exit_price, shares) -> exit_reason.

    closes_raw entries carry the canonical exit reason emitted by each
    sentinel.  The lookup key omits entry_price because the pnl_pairs
    entry_price occasionally differs from the closes_raw entry_price when
    a position is partially re-entered before the exit fires.
    """
    lookup: dict = {}
    for c in raw.get("closes_raw", []):
        key = (
            c["ticker"],
            (c.get("side") or "long").lower(),
            c["exit_price"],
            c["shares"],
        )
        lookup[key] = c.get("reason") or "unknown"
    return lookup


def main():
    days = load_days()
    days_summary = []
    all_pairs = []
    for d in days:
        path = RAW_DIR / f"{d}.json"
        with open(path) as f:
            r = json.load(f)
        s = r.get("summary", {})
        exit_reason_lookup = _build_exit_reason_lookup(r)
        for p in r.get("pnl_pairs", []):
            p["date"] = d
            key = (
                p["ticker"],
                (p.get("side") or "long").lower(),
                p["exit_price"],
                p["shares"],
            )
            p["exit_reason"] = exit_reason_lookup.get(key, "unknown")
            all_pairs.append(p)
        days_summary.append({
            "date": d,
            "entries": s.get("entries", 0),
            "exits": s.get("exits", 0),
            "wins": s.get("wins", 0),
            "losses": s.get("losses", 0),
            "pnl": round(s.get("total_pnl", 0), 4),
            "minutes": r.get("minutes_processed"),
            "tickers": len(r.get("tickers", [])),
        })

    totals = {
        "entries": sum(d["entries"] for d in days_summary),
        "exits":   sum(d["exits"] for d in days_summary),
        "wins":    sum(d["wins"] for d in days_summary),
        "losses":  sum(d["losses"] for d in days_summary),
        "total_pnl": round(sum(d["pnl"] for d in days_summary), 4),
        "minutes": sum(d["minutes"] or 0 for d in days_summary),
    }
    wr = totals["wins"] / max(1, totals["wins"] + totals["losses"]) * 100

    # Side splits
    long_pairs  = [p for p in all_pairs if (p.get("side") or "long").lower() == "long"]
    short_pairs = [p for p in all_pairs if (p.get("side") or "long").lower() == "short"]
    def split(pairs):
        pnl = round(sum(p.get("pnl_dollars", 0) for p in pairs), 4)
        wins = sum(1 for p in pairs if p.get("pnl_dollars", 0) > 0)
        wr_ = wins / max(1, len(pairs)) * 100
        return {"pairs": len(pairs), "pnl": pnl, "wins": wins, "wr_pct": round(wr_, 2)}
    long_stats  = split(long_pairs)
    short_stats = split(short_pairs)

    # By ticker
    by_ticker = defaultdict(lambda: {"pairs": 0, "pnl": 0.0, "wins": 0, "long_pnl": 0.0, "short_pnl": 0.0,
                                     "long_pairs": 0, "short_pairs": 0})
    for p in all_pairs:
        t = p.get("ticker") or "?"
        side = (p.get("side") or "long").lower()
        pnl = p.get("pnl_dollars", 0)
        bt = by_ticker[t]
        bt["pairs"] += 1
        bt["pnl"] += pnl
        if pnl > 0:
            bt["wins"] += 1
        if side == "long":
            bt["long_pnl"] += pnl
            bt["long_pairs"] += 1
        else:
            bt["short_pnl"] += pnl
            bt["short_pairs"] += 1
    by_ticker_out = {
        t: {
            "pairs": v["pairs"], "pnl": round(v["pnl"], 4),
            "wins": v["wins"], "wr_pct": round(v["wins"]/max(1,v["pairs"])*100, 1),
            "long_pairs": v["long_pairs"], "long_pnl": round(v["long_pnl"], 4),
            "short_pairs": v["short_pairs"], "short_pnl": round(v["short_pnl"], 4),
        }
        for t, v in sorted(by_ticker.items(), key=lambda kv: -kv[1]["pnl"])
    }

    # Weekly buckets (ISO week) for variance analysis
    from datetime import date as _date
    weekly = defaultdict(lambda: {"days": 0, "pnl": 0.0, "pairs": 0, "wins": 0, "losses": 0,
                                  "long_pnl": 0.0, "short_pnl": 0.0})
    for d in days_summary:
        y, m, dd = map(int, d["date"].split("-"))
        iso_year, iso_wk, _ = _date(y, m, dd).isocalendar()
        key = f"{iso_year}-W{iso_wk:02d}"
        w = weekly[key]
        w["days"] += 1
        w["pnl"] += d["pnl"]
        w["wins"] += d["wins"]
        w["losses"] += d["losses"]
    # Add per-week side pnl
    for p in all_pairs:
        y, m, dd = map(int, p["date"].split("-"))
        iso_year, iso_wk, _ = _date(y, m, dd).isocalendar()
        key = f"{iso_year}-W{iso_wk:02d}"
        side = (p.get("side") or "long").lower()
        if side == "long":
            weekly[key]["long_pnl"] += p.get("pnl_dollars", 0)
        else:
            weekly[key]["short_pnl"] += p.get("pnl_dollars", 0)
        weekly[key]["pairs"] += 1
    weekly_out = {
        k: {"days": v["days"], "pnl": round(v["pnl"], 2),
            "pairs": v["pairs"], "wins": v["wins"], "losses": v["losses"],
            "long_pnl": round(v["long_pnl"], 2),
            "short_pnl": round(v["short_pnl"], 2)}
        for k, v in sorted(weekly.items())
    }

    # Derive run version from the raw data so copies of this script
    # in different backtest dirs report the correct version automatically.
    run_version = "unknown"
    for d in days_summary:
        raw_path = RAW_DIR / f"{d['date']}.json"
        if raw_path.exists():
            with open(raw_path) as _f:
                _r = json.load(_f)
            run_version = _r.get("version", "unknown")
            break

    out = {
        "version": run_version,
        "data_source": "fresh_alpaca_iex",
        "config": "L0/S30 (default v6.4.3)",
        "days": days_summary,
        "totals": totals,
        "totals_win_rate": round(wr, 4),
        "by_side": {"long": long_stats, "short": short_stats},
        "by_ticker": by_ticker_out,
        "by_week": weekly_out,
        "pairs_count": len(all_pairs),
        "days_count": len(days),
    }
    with open(ROOT / "aggregate.json", "w") as f:
        json.dump(out, f, indent=2)
    with open(ROOT / "pairs.json", "w") as f:
        json.dump(all_pairs, f, indent=2, default=str)

    # Print summary
    print("=== v6.4.3 30-DAY BACKTEST (FRESH ALPACA, default L0/S30) ===")
    print(f"Days: {len(days)}  Pairs: {len(all_pairs)}  Total P&L: ${totals['total_pnl']:+,.2f}  WR: {wr:.1f}%\n")
    print(f"{'Date':<12} {'Entries':>8} {'Exits':>6} {'Wins':>5} {'Loss':>5} {'P&L':>12}")
    for d in days_summary:
        print(f"{d['date']:<12} {d['entries']:>8} {d['exits']:>6} {d['wins']:>5} {d['losses']:>5} ${d['pnl']:>+11,.2f}")
    print(f"{'TOTAL':<12} {totals['entries']:>8} {totals['exits']:>6} {totals['wins']:>5} {totals['losses']:>5} ${totals['total_pnl']:>+11,.2f}\n")
    print(f"Longs:  {long_stats['pairs']:>3} pairs, ${long_stats['pnl']:>+10,.2f}, WR {long_stats['wr_pct']:.1f}%")
    print(f"Shorts: {short_stats['pairs']:>3} pairs, ${short_stats['pnl']:>+10,.2f}, WR {short_stats['wr_pct']:.1f}%\n")
    print("By ticker (P&L desc):")
    for t, v in by_ticker_out.items():
        print(f"  {t:<6} pairs={v['pairs']:>3} pnl=${v['pnl']:>+10,.2f} WR={v['wr_pct']:>5.1f}%   "
              f"L:{v['long_pairs']:>3}/${v['long_pnl']:>+10,.2f}  S:{v['short_pairs']:>3}/${v['short_pnl']:>+10,.2f}")
    print("\nBy week:")
    print(f"{'Week':<10} {'Days':>5} {'Pairs':>6} {'Wins':>5} {'Loss':>5} {'P&L':>12} {'Long':>10} {'Short':>10}")
    for k, v in weekly_out.items():
        print(f"{k:<10} {v['days']:>5} {v['pairs']:>6} {v['wins']:>5} {v['losses']:>5} ${v['pnl']:>+11,.2f} ${v['long_pnl']:>+9,.2f} ${v['short_pnl']:>+9,.2f}")

    # Exit-reason summary
    from collections import Counter
    reason_counts = Counter(p.get("exit_reason", "unknown") for p in all_pairs)
    print("\nExit reasons:")
    for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
        print(f"  {reason:<40} {count:>5}")


if __name__ == "__main__":
    main()
