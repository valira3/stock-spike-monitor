#!/usr/bin/env python3
"""Aggregate v7.3.0 stop-hysteresis sweep vs $1,665 clean baseline."""
from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path
from statistics import median

ROOT = Path("/home/user/workspace/v730_stop_hysteresis_84d")
V730 = ROOT / "v730/stop_hysteresis/per_day"
BASE = Path("/home/user/workspace/v730_first15_block_backtest/baseline_clean/per_day")
OUT_MD = ROOT / "REPORT.md"
OUT_JSON = ROOT / "aggregate.json"


def load_pairs(per_day_dir: Path) -> tuple[list[dict], dict]:
    """Return (all_pairs, daily_pnl_by_date)."""
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


def hold_minutes(p: dict) -> float | None:
    try:
        from datetime import datetime
        et = p.get("entry_ts") or p.get("entry_ts_utc")
        xt = p.get("exit_ts") or p.get("exit_ts_utc")
        if not (et and xt):
            return None
        ed = datetime.fromisoformat(et.replace("Z", "+00:00"))
        xd = datetime.fromisoformat(xt.replace("Z", "+00:00"))
        return (xd - ed).total_seconds() / 60.0
    except Exception:
        return None


def bucket_hold(m: float | None) -> str:
    if m is None:
        return "unknown"
    if m < 5: return "<5min"
    if m < 15: return "5-15min"
    if m < 30: return "15-30min"
    if m < 60: return "30-60min"
    return ">60min"


def summarize_pairs(pairs: list[dict]) -> dict:
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


def by_side(pairs: list[dict]) -> dict[str, dict]:
    out = {}
    for side in ("long", "short"):
        sub = [p for p in pairs if (p.get("side") or "").lower() == side]
        out[side] = summarize_pairs(sub)
    return out


def by_hold_bucket(pairs: list[dict]) -> dict[str, dict]:
    buckets = defaultdict(list)
    for p in pairs:
        buckets[bucket_hold(hold_minutes(p))].append(p)
    order = ["<5min", "5-15min", "15-30min", "30-60min", ">60min", "unknown"]
    return {b: summarize_pairs(buckets.get(b, [])) for b in order}


def by_exit_reason(pairs: list[dict]) -> dict[str, dict]:
    buckets = defaultdict(list)
    for p in pairs:
        buckets[p.get("exit_reason") or "unknown"].append(p)
    return {r: summarize_pairs(v) for r, v in buckets.items()}


def main() -> int:
    v730_pairs, v730_daily = load_pairs(V730)
    base_pairs, base_daily = load_pairs(BASE)

    overall_v730 = summarize_pairs(v730_pairs)
    overall_base = summarize_pairs(base_pairs)
    delta = round(overall_v730["pnl"] - overall_base["pnl"], 2)

    side_v730 = by_side(v730_pairs)
    side_base = by_side(base_pairs)

    hold_v730 = by_hold_bucket(v730_pairs)
    hold_base = by_hold_bucket(base_pairs)

    exit_v730 = by_exit_reason(v730_pairs)
    exit_base = by_exit_reason(base_pairs)

    # Daily delta
    all_dates = sorted(set(v730_daily) | set(base_daily))
    daily_rows = []
    for d in all_dates:
        v = v730_daily.get(d, 0)
        b = base_daily.get(d, 0)
        daily_rows.append((d, v, b, v - b))
    pos_days_v = sum(1 for _, v, _, _ in daily_rows if v > 0)
    neg_days_v = sum(1 for _, v, _, _ in daily_rows if v < 0)
    pos_days_b = sum(1 for _, _, b, _ in daily_rows if b > 0)
    neg_days_b = sum(1 for _, _, b, _ in daily_rows if b < 0)

    agg = {
        "v730": overall_v730,
        "baseline": overall_base,
        "delta_pnl": delta,
        "by_side": {"v730": side_v730, "baseline": side_base},
        "by_hold_bucket": {"v730": hold_v730, "baseline": hold_base},
        "by_exit_reason": {"v730": exit_v730, "baseline": exit_base},
        "daily": [{"date": d, "v730": v, "base": b, "delta": dl} for d, v, b, dl in daily_rows],
        "days_v730": {"pos": pos_days_v, "neg": neg_days_v, "flat": len(daily_rows) - pos_days_v - neg_days_v},
        "days_base": {"pos": pos_days_b, "neg": neg_days_b, "flat": len(daily_rows) - pos_days_b - neg_days_b},
    }
    OUT_JSON.write_text(json.dumps(agg, indent=2, default=str))

    # Build report
    lines = []
    L = lines.append
    L("# v7.3.0 Stop-Hysteresis 83-Day SIP Sweep")
    L("")
    L(f"**vs $1,665 clean baseline (PR #407 base)**")
    L("")
    L(f"- Corpus: 83-day SIP (canonical_backtest_data_v707), 12 prod tickers")
    L(f"- Settings: V730 hysteresis (BARS=2, DEEP_FRAC=0.0075) + prod parity (L=30/S=30, vol gate off)")
    L(f"- Wall time: 12.0 min (2 workers)")
    L("")
    L("## Headline")
    L("")
    L("| Metric | v7.3.0 | Baseline | Δ |")
    L("|---|---:|---:|---:|")
    L(f"| Total P/L | ${overall_v730['pnl']:,.2f} | ${overall_base['pnl']:,.2f} | **${delta:+,.2f}** |")
    L(f"| Pairs | {overall_v730['n']} | {overall_base['n']} | {overall_v730['n']-overall_base['n']:+d} |")
    L(f"| Win rate | {overall_v730['wr']}% | {overall_base['wr']}% | {overall_v730['wr']-overall_base['wr']:+.1f}pp |")
    L(f"| Avg/trade | ${overall_v730['avg']:,.2f} | ${overall_base['avg']:,.2f} | ${overall_v730['avg']-overall_base['avg']:+,.2f} |")
    L(f"| Median/trade | ${overall_v730['median']:,.2f} | ${overall_base['median']:,.2f} | — |")
    L(f"| Pos days | {pos_days_v}/83 | {pos_days_b}/83 | {pos_days_v-pos_days_b:+d} |")
    L(f"| Neg days | {neg_days_v}/83 | {neg_days_b}/83 | {neg_days_v-neg_days_b:+d} |")
    L("")
    L("## Long vs Short")
    L("")
    L("| Side | Version | Pairs | P/L | WR | Avg |")
    L("|---|---|---:|---:|---:|---:|")
    for side in ("long", "short"):
        v = side_v730[side]; b = side_base[side]
        L(f"| {side.upper()} | v7.3.0   | {v['n']} | ${v['pnl']:,.2f} | {v['wr']}% | ${v['avg']:,.2f} |")
        L(f"| {side.upper()} | baseline | {b['n']} | ${b['pnl']:,.2f} | {b['wr']}% | ${b['avg']:,.2f} |")
    L("")
    L("## Hold-time bucket comparison")
    L("")
    L("| Bucket | v7.3.0 pairs | v7.3.0 P/L | base pairs | base P/L | Δ P/L |")
    L("|---|---:|---:|---:|---:|---:|")
    for bkt in ["<5min", "5-15min", "15-30min", "30-60min", ">60min", "unknown"]:
        v = hold_v730[bkt]; b = hold_base[bkt]
        if v["n"] == 0 and b["n"] == 0:
            continue
        L(f"| {bkt} | {v['n']} | ${v['pnl']:,.2f} | {b['n']} | ${b['pnl']:,.2f} | ${v['pnl']-b['pnl']:+,.2f} |")
    L("")
    L("## Exit reason mix")
    L("")
    L("| Reason | v7.3.0 pairs | v7.3.0 P/L | base pairs | base P/L |")
    L("|---|---:|---:|---:|---:|")
    all_reasons = sorted(set(exit_v730) | set(exit_base))
    for r in all_reasons:
        v = exit_v730.get(r, {"n": 0, "pnl": 0})
        b = exit_base.get(r, {"n": 0, "pnl": 0})
        L(f"| {r} | {v['n']} | ${v['pnl']:,.2f} | {b['n']} | ${b['pnl']:,.2f} |")
    L("")
    L("## Daily detail (chronological)")
    L("")
    L("| Date | v7.3.0 | Baseline | Δ |")
    L("|---|---:|---:|---:|")
    for d, v, b, dl in daily_rows:
        L(f"| {d} | ${v:,.2f} | ${b:,.2f} | ${dl:+,.2f} |")
    L("")
    L("## Top 5 wins for v7.3.0")
    L("")
    L("| Date | v7.3.0 | Δ vs base |")
    L("|---|---:|---:|")
    for d, v, b, dl in sorted(daily_rows, key=lambda r: -r[1])[:5]:
        L(f"| {d} | ${v:,.2f} | ${dl:+,.2f} |")
    L("")
    L("## Worst 5 days for v7.3.0")
    L("")
    L("| Date | v7.3.0 | Δ vs base |")
    L("|---|---:|---:|")
    for d, v, b, dl in sorted(daily_rows, key=lambda r: r[1])[:5]:
        L(f"| {d} | ${v:,.2f} | ${dl:+,.2f} |")
    L("")
    L("## Top 5 days by delta (where hysteresis helped most)")
    L("")
    L("| Date | v7.3.0 | Baseline | Δ |")
    L("|---|---:|---:|---:|")
    for d, v, b, dl in sorted(daily_rows, key=lambda r: -r[3])[:5]:
        L(f"| {d} | ${v:,.2f} | ${b:,.2f} | ${dl:+,.2f} |")
    L("")
    L("## Worst 5 days by delta (where hysteresis hurt)")
    L("")
    L("| Date | v7.3.0 | Baseline | Δ |")
    L("|---|---:|---:|---:|")
    for d, v, b, dl in sorted(daily_rows, key=lambda r: r[3])[:5]:
        L(f"| {d} | ${v:,.2f} | ${b:,.2f} | ${dl:+,.2f} |")
    L("")
    L("---")
    L("")
    L("Raw outputs: `v730/stop_hysteresis/per_day/` (83 day JSONs), `aggregate.json`, `summary.json`, `run_sweep.py`")

    OUT_MD.write_text("\n".join(lines))
    print(f"Wrote {OUT_MD}")
    print(f"Wrote {OUT_JSON}")
    print(f"\nHEADLINE: v7.3.0 ${overall_v730['pnl']:,.2f} vs baseline ${overall_base['pnl']:,.2f} = Δ ${delta:+,.2f}")
    print(f"  pairs {overall_v730['n']} vs {overall_base['n']}, WR {overall_v730['wr']}% vs {overall_base['wr']}%")
    print(f"  pos days {pos_days_v} vs {pos_days_b}, neg days {neg_days_v} vs {neg_days_b}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
