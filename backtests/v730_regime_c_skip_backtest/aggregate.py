#!/usr/bin/env python3
"""Aggregate v7.3.0 regime-C suppression backtest results.

Compares baseline vs skip_c variants. Crucially, breaks down per-regime
performance for the baseline so we can see what we're giving up by
skipping regime C.
"""
from __future__ import annotations
import json
from pathlib import Path
from collections import defaultdict
import statistics

ROOT = Path("/home/user/workspace/v730_regime_c_skip_backtest")
BARS = Path("/home/user/workspace/canonical_backtest_data_v707/replay_layout")


def classify_regime(date: str) -> str | None:
    spy_path = BARS / date / "SPY.jsonl"
    if not spy_path.exists():
        return None
    p_open = p_1000 = None
    for line in spy_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            b = json.loads(line)
        except Exception:
            continue
        bk = b.get("et_bucket")
        if bk == "0930" and p_open is None:
            p_open = b.get("close") or b.get("c")
        elif bk == "1000" and p_1000 is None:
            p_1000 = b.get("close") or b.get("c")
            break
    if not p_open or not p_1000:
        return None
    r = (p_1000 - p_open) / p_open * 100
    if r <= -0.50: return "A"
    if r < -0.15: return "B"
    if r <= 0.15: return "C"
    if r <= 0.50: return "D"
    return "E"


def regime_ret(date: str) -> float | None:
    spy_path = BARS / date / "SPY.jsonl"
    if not spy_path.exists():
        return None
    p_open = p_1000 = None
    for line in spy_path.read_text().splitlines():
        if not line.strip(): continue
        try: b = json.loads(line)
        except: continue
        bk = b.get("et_bucket")
        if bk == "0930" and p_open is None: p_open = b.get("close")
        elif bk == "1000" and p_1000 is None: p_1000 = b.get("close"); break
    return (p_1000 - p_open) / p_open * 100 if p_open and p_1000 else None


def load_day(per_day_dir: Path, date: str) -> dict:
    f = per_day_dir / f"{date}.json"
    if not f.exists():
        return {}
    return json.loads(f.read_text())


def aggregate(variant: str) -> dict:
    per_day_dir = ROOT / variant / "per_day"
    days = sorted(f.stem for f in per_day_dir.glob("*.json"))
    rows = []
    by_regime = defaultdict(lambda: {"days": 0, "entries": 0, "pairs": 0, "wins": 0, "losses": 0, "pnl": 0.0, "skipped_days": 0})
    by_ticker = defaultdict(lambda: {"pairs": 0, "wins": 0, "losses": 0, "pnl": 0.0, "long": 0, "short": 0})
    total = {"entries": 0, "pairs": 0, "wins": 0, "losses": 0, "pnl": 0.0}
    pos_days = neg_days = flat_days = 0
    pnls = []
    for date in days:
        d = load_day(per_day_dir, date)
        regime = classify_regime(date) or "?"
        was_skipped = bool(d.get("_v730_regime_c_skipped"))
        pp = d.get("pnl_pairs", [])
        day_pnl = sum(p.get("pnl_dollars", p.get("pnl", 0)) for p in pp)
        wins = sum(1 for p in pp if p.get("pnl_dollars", p.get("pnl", 0)) > 0)
        losses = sum(1 for p in pp if p.get("pnl_dollars", p.get("pnl", 0)) <= 0)
        entries = (d.get("summary") or {}).get("entries", 0) or len(d.get("entries", []))
        rows.append({
            "date": date, "regime": regime,
            "regime_ret_pct": round(regime_ret(date) or 0, 3),
            "skipped": was_skipped,
            "entries": entries, "pairs": len(pp),
            "wins": wins, "losses": losses,
            "pnl": round(day_pnl, 2),
        })
        total["entries"] += entries
        total["pairs"] += len(pp)
        total["wins"] += wins
        total["losses"] += losses
        total["pnl"] += day_pnl
        by_regime[regime]["days"] += 1
        by_regime[regime]["entries"] += entries
        by_regime[regime]["pairs"] += len(pp)
        by_regime[regime]["wins"] += wins
        by_regime[regime]["losses"] += losses
        by_regime[regime]["pnl"] += day_pnl
        if was_skipped:
            by_regime[regime]["skipped_days"] += 1
        pnls.append(day_pnl)
        if day_pnl > 0: pos_days += 1
        elif day_pnl < 0: neg_days += 1
        else: flat_days += 1
        for p in pp:
            tk = p.get("ticker", "?")
            side = p.get("side", "")
            pn = p.get("pnl_dollars", p.get("pnl", 0))
            by_ticker[tk]["pairs"] += 1
            by_ticker[tk]["pnl"] += pn
            if pn > 0: by_ticker[tk]["wins"] += 1
            else: by_ticker[tk]["losses"] += 1
            if str(side).upper() == "LONG": by_ticker[tk]["long"] += 1
            elif str(side).upper() == "SHORT": by_ticker[tk]["short"] += 1
    # max DD on cumulative
    cum = 0.0; peak = 0.0; max_dd = 0.0
    for p in pnls:
        cum += p; peak = max(peak, cum); max_dd = min(max_dd, cum - peak)
    return {
        "variant": variant,
        "rows": rows,
        "total": {**total, "pnl": round(total["pnl"], 2)},
        "by_regime": {k: {**v, "pnl": round(v["pnl"], 2)} for k, v in by_regime.items()},
        "by_ticker": {k: {**v, "pnl": round(v["pnl"], 2)} for k, v in by_ticker.items()},
        "pos_days": pos_days, "neg_days": neg_days, "flat_days": flat_days,
        "max_dd": round(max_dd, 2),
        "avg_per_day": round(total["pnl"] / max(1, len(days)), 2),
        "win_rate": round(total["wins"] / max(1, total["wins"] + total["losses"]) * 100, 2),
    }


def fmt_pnl(x): return f"${x:+,.2f}"


def main():
    base = aggregate("baseline")
    skip = aggregate("skip_c")
    Path(ROOT / "aggregate.json").write_text(json.dumps({"baseline": base, "skip_c": skip}, indent=2, default=str))

    # Build markdown report
    md = []
    md.append("# v7.3.0 — Regime-C Entry Suppression Backtest\n")
    md.append(f"**Corpus:** v7.0.7 SIP archive, 83 days (2026-01-02 → 2026-05-01), 12 prod tickers")
    md.append(f"**Bot:** v7.2.7 (commit 8f8908d)")
    md.append(f"**Production settings (Railway, 2026-05-07):** L=30 / S=30 / VOLUME_GATE=true / RATIO=0.85\n")

    md.append("## Headline\n")
    md.append("| Metric | Baseline | Skip Regime-C | Δ |")
    md.append("|---|---:|---:|---:|")
    md.append(f"| Net P&L (83d) | {fmt_pnl(base['total']['pnl'])} | {fmt_pnl(skip['total']['pnl'])} | {fmt_pnl(skip['total']['pnl']-base['total']['pnl'])} |")
    md.append(f"| Avg P&L / day | {fmt_pnl(base['avg_per_day'])} | {fmt_pnl(skip['avg_per_day'])} | {fmt_pnl(skip['avg_per_day']-base['avg_per_day'])} |")
    md.append(f"| Entries | {base['total']['entries']} | {skip['total']['entries']} | {skip['total']['entries']-base['total']['entries']:+d} |")
    md.append(f"| Closed pairs | {base['total']['pairs']} | {skip['total']['pairs']} | {skip['total']['pairs']-base['total']['pairs']:+d} |")
    md.append(f"| Wins | {base['total']['wins']} | {skip['total']['wins']} | {skip['total']['wins']-base['total']['wins']:+d} |")
    md.append(f"| Losses | {base['total']['losses']} | {skip['total']['losses']} | {skip['total']['losses']-base['total']['losses']:+d} |")
    md.append(f"| Win rate | {base['win_rate']}% | {skip['win_rate']}% | {skip['win_rate']-base['win_rate']:+.2f}pp |")
    md.append(f"| Pos / Neg / Flat days | {base['pos_days']}/{base['neg_days']}/{base['flat_days']} | {skip['pos_days']}/{skip['neg_days']}/{skip['flat_days']} | — |")
    md.append(f"| Max drawdown | {fmt_pnl(base['max_dd'])} | {fmt_pnl(skip['max_dd'])} | {fmt_pnl(skip['max_dd']-base['max_dd'])} |")
    md.append("")

    md.append("## Per-Regime Breakdown (BASELINE — what we'd be giving up)\n")
    md.append("| Regime | Days | Entries | Pairs | Wins | Losses | WR | P&L | Avg/Day |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for reg in ["A", "B", "C", "D", "E", "?"]:
        d = base["by_regime"].get(reg)
        if not d or d["days"] == 0: continue
        wr = d["wins"]/max(1,d["wins"]+d["losses"])*100
        avg = d["pnl"]/max(1,d["days"])
        md.append(f"| {reg} | {d['days']} | {d['entries']} | {d['pairs']} | {d['wins']} | {d['losses']} | {wr:.1f}% | {fmt_pnl(d['pnl'])} | {fmt_pnl(avg)} |")
    md.append("")
    md.append("**Regime band definitions** (SPY 09:30→10:00 ret):")
    md.append("- A: ≤ -0.50% (deep down)  •  B: -0.50% to -0.15%  •  C: -0.15% to +0.15% (chop)")
    md.append("- D: +0.15% to +0.50%  •  E: > +0.50%")
    md.append("")

    # Compare regime-C only between variants
    base_c = base["by_regime"].get("C", {})
    skip_c = skip["by_regime"].get("C", {})
    md.append("## Regime-C Days Only — Baseline vs Skip\n")
    md.append("| | Baseline (all C entries fire) | Skip Regime-C |")
    md.append("|---|---:|---:|")
    md.append(f"| Days | {base_c.get('days',0)} | {skip_c.get('days',0)} (synth-skipped: {skip_c.get('skipped_days',0)}) |")
    md.append(f"| Entries | {base_c.get('entries',0)} | {skip_c.get('entries',0)} |")
    md.append(f"| Pairs | {base_c.get('pairs',0)} | {skip_c.get('pairs',0)} |")
    md.append(f"| P&L | {fmt_pnl(base_c.get('pnl',0))} | {fmt_pnl(skip_c.get('pnl',0))} |")
    base_c_wr = base_c.get('wins',0)/max(1,base_c.get('wins',0)+base_c.get('losses',0))*100 if base_c else 0
    md.append(f"| Win rate | {base_c_wr:.1f}% | n/a (skipped) |")
    base_c_avg = base_c.get('pnl',0)/max(1,base_c.get('days',1)) if base_c else 0
    md.append(f"| Avg/Day | {fmt_pnl(base_c_avg)} | $0.00 |")
    md.append("")
    md.append(f"**The 35 regime-C days printed {fmt_pnl(base_c.get('pnl',0))} in the baseline.** Skipping them removes that contribution.")
    md.append("")

    # Per-ticker summary baseline
    md.append("## By Ticker (BASELINE)\n")
    md.append("| Ticker | Pairs | L/S | Wins | Losses | WR | P&L |")
    md.append("|---|---:|---|---:|---:|---:|---:|")
    for tk, t in sorted(base["by_ticker"].items(), key=lambda x: -x[1]["pnl"]):
        wr = t["wins"]/max(1, t["wins"]+t["losses"])*100
        md.append(f"| {tk} | {t['pairs']} | {t['long']}/{t['short']} | {t['wins']} | {t['losses']} | {wr:.1f}% | {fmt_pnl(t['pnl'])} |")
    md.append("")

    # Top/Bottom days baseline
    rows_sorted = sorted(base["rows"], key=lambda r: -r["pnl"])
    md.append("## Top 5 Baseline Days\n")
    md.append("| Date | Regime | SPY Ret | Entries | W/L | P&L |")
    md.append("|---|---|---:|---:|---|---:|")
    for r in rows_sorted[:5]:
        md.append(f"| {r['date']} | {r['regime']} | {r['regime_ret_pct']:+.3f}% | {r['entries']} | {r['wins']}/{r['losses']} | {fmt_pnl(r['pnl'])} |")
    md.append("\n## Worst 5 Baseline Days\n")
    md.append("| Date | Regime | SPY Ret | Entries | W/L | P&L |")
    md.append("|---|---|---:|---:|---|---:|")
    for r in rows_sorted[-5:]:
        md.append(f"| {r['date']} | {r['regime']} | {r['regime_ret_pct']:+.3f}% | {r['entries']} | {r['wins']}/{r['losses']} | {fmt_pnl(r['pnl'])} |")
    md.append("")

    # Daily detail
    md.append("## Daily Detail (Baseline | Skip)\n")
    md.append("| Date | Reg | SPY% | Base Entries | Base P&L | Skip Entries | Skip P&L | Δ |")
    md.append("|---|---|---:|---:|---:|---:|---:|---:|")
    skip_by_date = {r["date"]: r for r in skip["rows"]}
    for r in base["rows"]:
        s = skip_by_date.get(r["date"], {})
        delta = (s.get("pnl", 0) or 0) - r["pnl"]
        skip_marker = " (skip)" if s.get("skipped") else ""
        md.append(f"| {r['date']} | {r['regime']} | {r['regime_ret_pct']:+.3f}% | {r['entries']} | {fmt_pnl(r['pnl'])} | {s.get('entries',0)}{skip_marker} | {fmt_pnl(s.get('pnl',0))} | {fmt_pnl(delta)} |")

    md.append("\n---\n")
    md.append(f"Raw outputs: `{ROOT}/baseline/per_day/`, `{ROOT}/skip_c/per_day/`")
    md.append(f"Aggregate JSON: `{ROOT}/aggregate.json`")

    (ROOT / "REPORT.md").write_text("\n".join(md))
    print(f"Wrote {ROOT}/REPORT.md ({len(md)} lines)")
    print(f"\nHEADLINE:")
    print(f"  baseline:  pnl={fmt_pnl(base['total']['pnl'])}  pairs={base['total']['pairs']}  wr={base['win_rate']}%")
    print(f"  skip_c  :  pnl={fmt_pnl(skip['total']['pnl'])}  pairs={skip['total']['pairs']}  wr={skip['win_rate']}%")
    print(f"  delta   :  {fmt_pnl(skip['total']['pnl']-base['total']['pnl'])}")


if __name__ == "__main__":
    main()
