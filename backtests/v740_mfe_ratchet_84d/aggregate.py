#!/usr/bin/env python3
"""Aggregate v7.4.0 MFE-ratchet sweep (3 fracs) vs v7.3.0 baseline."""
from __future__ import annotations
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median

ROOT = Path("/home/user/workspace/v740_mfe_ratchet_84d")
VARIANTS = {
    "frac_03": ROOT / "v740/frac_03/per_day",
    "frac_05": ROOT / "v740/frac_05/per_day",
    "frac_07": ROOT / "v740/frac_07/per_day",
}
V730_BASELINE = Path("/home/user/workspace/v730_stop_hysteresis_84d/v730/stop_hysteresis/per_day")
CLEAN_BASELINE = Path("/home/user/workspace/v730_first15_block_backtest/baseline_clean/per_day")
OUT_MD = ROOT / "REPORT.md"
OUT_JSON = ROOT / "aggregate.json"


def load_pairs(per_day_dir: Path):
    pairs = []
    daily = {}
    for p in sorted(per_day_dir.glob("*.json")):
        date = p.stem
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        pp = d.get("pnl_pairs", [])
        for x in pp:
            x = dict(x)
            x["_date"] = date
            pairs.append(x)
        daily[date] = sum(x.get("pnl_dollars", x.get("pnl", 0)) for x in pp)
    return pairs, daily


def hold_minutes(p):
    try:
        et = p.get("entry_ts") or p.get("entry_ts_utc")
        xt = p.get("exit_ts") or p.get("exit_ts_utc")
        if not (et and xt):
            return None
        ed = datetime.fromisoformat(et.replace("Z", "+00:00"))
        xd = datetime.fromisoformat(xt.replace("Z", "+00:00"))
        return (xd - ed).total_seconds() / 60.0
    except Exception:
        return None


def bucket_hold(m):
    if m is None: return "unknown"
    if m < 5: return "<5min"
    if m < 15: return "5-15min"
    if m < 30: return "15-30min"
    if m < 60: return "30-60min"
    return ">60min"


def summarize(pairs):
    pnls = [p.get("pnl_dollars", p.get("pnl", 0)) for p in pairs]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    return {
        "n": len(pairs),
        "pnl": round(sum(pnls), 2),
        "wins": len(wins),
        "losses": len(losses),
        "wr": round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
        "avg": round(sum(pnls) / len(pnls), 2) if pnls else 0,
        "median": round(median(pnls), 2) if pnls else 0,
    }


def by_side(pairs):
    return {side: summarize([p for p in pairs if (p.get("side") or "").lower() == side])
            for side in ("long", "short")}


def by_hold(pairs):
    buckets = defaultdict(list)
    for p in pairs:
        buckets[bucket_hold(hold_minutes(p))].append(p)
    order = ["<5min", "5-15min", "15-30min", "30-60min", ">60min", "unknown"]
    return {b: summarize(buckets.get(b, [])) for b in order}


def by_exit(pairs):
    buckets = defaultdict(list)
    for p in pairs:
        buckets[p.get("exit_reason") or "unknown"].append(p)
    return {r: summarize(v) for r, v in buckets.items()}


def main():
    data = {}
    for name, path in VARIANTS.items():
        pairs, daily = load_pairs(path)
        data[name] = {
            "overall": summarize(pairs),
            "side": by_side(pairs),
            "hold": by_hold(pairs),
            "exit": by_exit(pairs),
            "daily": daily,
        }
    v730_pairs, v730_daily = load_pairs(V730_BASELINE)
    base_pairs, base_daily = load_pairs(CLEAN_BASELINE)
    data["v730"] = {
        "overall": summarize(v730_pairs),
        "side": by_side(v730_pairs),
        "hold": by_hold(v730_pairs),
        "exit": by_exit(v730_pairs),
        "daily": v730_daily,
    }
    data["clean"] = {
        "overall": summarize(base_pairs),
        "side": by_side(base_pairs),
        "hold": by_hold(base_pairs),
        "exit": by_exit(base_pairs),
        "daily": base_daily,
    }

    OUT_JSON.write_text(json.dumps(data, indent=2, default=str))

    # Build report
    L = []
    A = L.append
    A("# v7.4.0 MFE-Ratchet Trail \u2014 83-Day SIP Sweep")
    A("")
    A("**3 fracs (0.3 / 0.5 / 0.7) stacked on v7.3.0 stop-hysteresis**")
    A("")
    A(f"- Corpus: 83-day SIP (canonical_backtest_data_v707), 12 prod tickers")
    A(f"- Settings: V730 hysteresis (BARS=2, DEEP=0.0075) + V740 ratchet (ARM_R=1.0) + prod parity (L=30/S=30, vol gate off)")
    A(f"- Wall: 43.7 min (3 variants, 2 workers each)")
    A("")
    A("## Headline")
    A("")
    A("| Variant | Total P/L | Δ vs v7.3.0 | Δ vs clean | Pairs | WR | Avg |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    v730_pnl = data["v730"]["overall"]["pnl"]
    clean_pnl = data["clean"]["overall"]["pnl"]
    A(f"| Clean baseline | ${clean_pnl:,.2f} | \u2014 | \u2014 | {data['clean']['overall']['n']} | {data['clean']['overall']['wr']}% | ${data['clean']['overall']['avg']:.2f} |")
    A(f"| v7.3.0 hysteresis | ${v730_pnl:,.2f} | \u2014 | ${v730_pnl-clean_pnl:+,.2f} | {data['v730']['overall']['n']} | {data['v730']['overall']['wr']}% | ${data['v730']['overall']['avg']:.2f} |")
    for name in ("frac_03", "frac_05", "frac_07"):
        ov = data[name]["overall"]
        A(f"| v7.4.0 {name} | ${ov['pnl']:,.2f} | ${ov['pnl']-v730_pnl:+,.2f} | ${ov['pnl']-clean_pnl:+,.2f} | {ov['n']} | {ov['wr']}% | ${ov['avg']:.2f} |")
    A("")

    # Best variant
    best = max(("frac_03","frac_05","frac_07"), key=lambda n: data[n]["overall"]["pnl"])
    A(f"**Winner: v7.4.0 {best}** (${data[best]['overall']['pnl']:,.2f}, ${data[best]['overall']['pnl']-v730_pnl:+,.2f} vs v7.3.0)")
    A("")

    A("## Long vs Short")
    A("")
    A("| Variant | LONG pairs | LONG P/L | LONG WR | SHORT pairs | SHORT P/L | SHORT WR |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    for name in ("clean", "v730", "frac_03", "frac_05", "frac_07"):
        L_ = data[name]["side"]["long"]; S_ = data[name]["side"]["short"]
        A(f"| {name} | {L_['n']} | ${L_['pnl']:,.2f} | {L_['wr']}% | {S_['n']} | ${S_['pnl']:,.2f} | {S_['wr']}% |")
    A("")

    A("## Hold-time bucket (P/L by bucket)")
    A("")
    A("| Bucket | clean | v7.3.0 | frac_03 | frac_05 | frac_07 |")
    A("|---|---:|---:|---:|---:|---:|")
    for bkt in ["<5min", "5-15min", "15-30min", "30-60min", ">60min", "unknown"]:
        cells = [bkt]
        for n in ("clean", "v730", "frac_03", "frac_05", "frac_07"):
            v = data[n]["hold"][bkt]
            cells.append(f"${v['pnl']:,.2f} (n={v['n']})")
        A("| " + " | ".join(cells) + " |")
    A("")

    A("## Exit reason mix")
    A("")
    all_reasons = set()
    for n in ("clean", "v730", "frac_03", "frac_05", "frac_07"):
        all_reasons |= set(data[n]["exit"].keys())
    A("| Reason | clean | v7.3.0 | frac_03 | frac_05 | frac_07 |")
    A("|---|---:|---:|---:|---:|---:|")
    for r in sorted(all_reasons):
        cells = [r]
        for n in ("clean", "v730", "frac_03", "frac_05", "frac_07"):
            v = data[n]["exit"].get(r, {"n": 0, "pnl": 0})
            cells.append(f"${v['pnl']:,.2f} (n={v['n']})")
        A("| " + " | ".join(cells) + " |")
    A("")

    # Daily detail (best variant vs v730)
    all_dates = sorted(set(data[best]["daily"]) | set(data["v730"]["daily"]))
    A(f"## Daily detail \u2014 {best} vs v7.3.0")
    A("")
    A("| Date | clean | v7.3.0 | " + best + " | Δ vs v7.3.0 |")
    A("|---|---:|---:|---:|---:|")
    for d in all_dates:
        c = data["clean"]["daily"].get(d, 0)
        v = data["v730"]["daily"].get(d, 0)
        b = data[best]["daily"].get(d, 0)
        A(f"| {d} | ${c:,.2f} | ${v:,.2f} | ${b:,.2f} | ${b-v:+,.2f} |")
    A("")

    # Days where ratchet helped most / hurt most
    diffs = [(d, data[best]["daily"].get(d,0) - data["v730"]["daily"].get(d,0))
             for d in all_dates]
    A(f"### Top 5 days where {best} beat v7.3.0")
    A("")
    A("| Date | v7.3.0 | " + best + " | Δ |")
    A("|---|---:|---:|---:|")
    for d, dl in sorted(diffs, key=lambda x: -x[1])[:5]:
        A(f"| {d} | ${data['v730']['daily'].get(d,0):,.2f} | ${data[best]['daily'].get(d,0):,.2f} | ${dl:+,.2f} |")
    A("")
    A(f"### Top 5 days where {best} underperformed v7.3.0")
    A("")
    A("| Date | v7.3.0 | " + best + " | Δ |")
    A("|---|---:|---:|---:|")
    for d, dl in sorted(diffs, key=lambda x: x[1])[:5]:
        A(f"| {d} | ${data['v730']['daily'].get(d,0):,.2f} | ${data[best]['daily'].get(d,0):,.2f} | ${dl:+,.2f} |")
    A("")
    A("---")
    A("")
    A("Raw outputs: `v740/frac_{03,05,07}/per_day/`, `aggregate.json`, per-variant `summary.json`, `run_sweep.py`")

    OUT_MD.write_text("\n".join(L))
    print(f"Wrote {OUT_MD}")
    for n in ("clean","v730","frac_03","frac_05","frac_07"):
        ov = data[n]["overall"]
        print(f"  {n:8s} ${ov['pnl']:>9,.2f}  pairs={ov['n']:>4d}  WR={ov['wr']:>5.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
