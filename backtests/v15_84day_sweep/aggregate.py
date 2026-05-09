#!/usr/bin/env python3
"""Aggregate v15.0.0-experimental sweep (v15_full vs v15_baseline) vs v7.4.0 baseline."""
from __future__ import annotations
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median

ROOT = Path("/home/user/workspace/v15_84day_sweep")
VARIANTS = {
    "v15_full":     ROOT / "v15_full" / "per_day",
    "v15_baseline": ROOT / "v15_baseline" / "per_day",
}
V740_BASELINE = Path("/home/user/workspace/v740_mfe_ratchet_84d/v740/frac_05/per_day")
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


def load_exits(per_day_dir: Path):
    """Index exits by (date, ticker, exit_ts) so we can join exit_reason onto pairs."""
    idx = {}
    for p in sorted(per_day_dir.glob("*.json")):
        d = json.loads(p.read_text())
        for ex in d.get("exits", []) or []:
            key = (p.stem, ex.get("ticker"), ex.get("ts"))
            idx[key] = ex.get("reason") or "unknown"
    return idx


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


def by_exit(pairs, exit_idx):
    buckets = defaultdict(list)
    for p in pairs:
        key = (p.get("_date"), p.get("ticker"), p.get("exit_ts"))
        reason = exit_idx.get(key, "unknown")
        buckets[reason].append(p)
    return {r: summarize(v) for r, v in buckets.items()}


def by_ticker(pairs):
    buckets = defaultdict(list)
    for p in pairs:
        buckets[p.get("ticker") or "unk"].append(p)
    return {t: summarize(v) for t, v in buckets.items()}


def by_month(pairs):
    buckets = defaultdict(list)
    for p in pairs:
        d = p.get("_date") or ""
        ym = d[:7] if len(d) >= 7 else "unk"
        buckets[ym].append(p)
    return {m: summarize(v) for m, v in sorted(buckets.items())}


def main():
    data = {}
    for name, path in VARIANTS.items():
        pairs, daily = load_pairs(path)
        exit_idx = load_exits(path)
        data[name] = {
            "overall":  summarize(pairs),
            "side":     by_side(pairs),
            "hold":     by_hold(pairs),
            "exit":     by_exit(pairs, exit_idx),
            "ticker":   by_ticker(pairs),
            "month":    by_month(pairs),
            "daily":    daily,
        }

    v740_pairs, v740_daily = load_pairs(V740_BASELINE)
    v740_exit_idx = load_exits(V740_BASELINE)
    data["v740"] = {
        "overall":  summarize(v740_pairs),
        "side":     by_side(v740_pairs),
        "hold":     by_hold(v740_pairs),
        "exit":     by_exit(v740_pairs, v740_exit_idx),
        "ticker":   by_ticker(v740_pairs),
        "month":    by_month(v740_pairs),
        "daily":    v740_daily,
    }

    OUT_JSON.write_text(json.dumps(data, indent=2, default=str))

    # ---- REPORT ----
    L = []
    A = L.append
    A("# Tiger Sovereign v15.0.0-experimental \u2014 83-Day SIP Sweep")
    A("")
    A("**Spec:** `tiger-sovereign-spec-v15-1.md` (uploaded 2026-05-07)  ")
    A("**Branch:** `experiment/tiger-sovereign-v15` @ `ce4a62f`  ")
    A("**Corpus:** 83-day SIP (canonical_backtest_data_v707), 12 prod tickers")
    A("")
    A("Two variants run side-by-side, identical wiring:")
    A("- **v15_full**     \u2014 all four v15 flags ON (hard strike cap, DI floor=25, 5m ADX>20, Alarm E post-entry)")
    A("- **v15_baseline** \u2014 all four v15 flags OFF (mimics v7.4.0 logic on the v15.0.0 codebase)")
    A("")
    A("Both run with V730 hysteresis ON (BARS=2, DEEP=0.0075) and V740 ratchet ON (frac=0.5).")
    A("")

    A("## Headline")
    A("")
    A("| Variant | Total P/L | \u0394 vs v15_baseline | \u0394 vs v7.4.0 frac_05 | Pairs | WR | Avg |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    base = data["v15_baseline"]["overall"]["pnl"]
    v740 = data["v740"]["overall"]["pnl"]
    A(f"| v7.4.0 frac_05 (prod baseline) | ${v740:,.2f} | \u2014 | \u2014 | {data['v740']['overall']['n']} | {data['v740']['overall']['wr']}% | ${data['v740']['overall']['avg']:.2f} |")
    for name in ("v15_baseline", "v15_full"):
        ov = data[name]["overall"]
        A(f"| {name} | ${ov['pnl']:,.2f} | ${ov['pnl']-base:+,.2f} | ${ov['pnl']-v740:+,.2f} | {ov['n']} | {ov['wr']}% | ${ov['avg']:.2f} |")
    A("")

    full_pnl = data["v15_full"]["overall"]["pnl"]
    if full_pnl > base and full_pnl > v740:
        verdict = f"**v15_full WINS** \u2014 ${full_pnl-v740:+,.2f} vs prod v7.4.0, ${full_pnl-base:+,.2f} vs v15_baseline."
    elif full_pnl > base:
        verdict = f"**v15_full beats its own baseline** by ${full_pnl-base:+,.2f}, but underperforms prod v7.4.0 by ${full_pnl-v740:+,.2f}."
    elif full_pnl > v740:
        verdict = f"**v15_full beats v7.4.0** by ${full_pnl-v740:+,.2f} but underperforms its own v15_baseline by ${full_pnl-base:+,.2f} (the v15 flags hurt vs flags-off)."
    else:
        verdict = f"**v15_full underperforms** \u2014 ${full_pnl-v740:+,.2f} vs prod v7.4.0, ${full_pnl-base:+,.2f} vs v15_baseline."
    A(verdict)
    A("")

    A("## Long vs Short")
    A("")
    A("| Variant | LONG pairs | LONG P/L | LONG WR | SHORT pairs | SHORT P/L | SHORT WR |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    for name in ("v740", "v15_baseline", "v15_full"):
        Lg = data[name]["side"]["long"]; Sh = data[name]["side"]["short"]
        A(f"| {name} | {Lg['n']} | ${Lg['pnl']:,.2f} | {Lg['wr']}% | {Sh['n']} | ${Sh['pnl']:,.2f} | {Sh['wr']}% |")
    A("")

    A("## By Month")
    A("")
    A("| Month | v7.4.0 | v15_baseline | v15_full | \u0394 v15_full vs v7.4.0 |")
    A("|---|---:|---:|---:|---:|")
    months = sorted(set(data["v740"]["month"]) | set(data["v15_baseline"]["month"]) | set(data["v15_full"]["month"]))
    for m in months:
        a = data["v740"]["month"].get(m, {"pnl": 0, "n": 0})
        b = data["v15_baseline"]["month"].get(m, {"pnl": 0, "n": 0})
        c = data["v15_full"]["month"].get(m, {"pnl": 0, "n": 0})
        A(f"| {m} | ${a['pnl']:,.2f} (n={a['n']}) | ${b['pnl']:,.2f} (n={b['n']}) | ${c['pnl']:,.2f} (n={c['n']}) | ${c['pnl']-a['pnl']:+,.2f} |")
    A("")

    A("## By Ticker")
    A("")
    tickers = sorted(set(data["v740"]["ticker"]) | set(data["v15_baseline"]["ticker"]) | set(data["v15_full"]["ticker"]))
    A("| Ticker | v7.4.0 | v15_baseline | v15_full |")
    A("|---|---:|---:|---:|")
    for t in tickers:
        a = data["v740"]["ticker"].get(t, {"pnl": 0, "n": 0})
        b = data["v15_baseline"]["ticker"].get(t, {"pnl": 0, "n": 0})
        c = data["v15_full"]["ticker"].get(t, {"pnl": 0, "n": 0})
        A(f"| {t} | ${a['pnl']:,.2f} (n={a['n']}) | ${b['pnl']:,.2f} (n={b['n']}) | ${c['pnl']:,.2f} (n={c['n']}) |")
    A("")

    A("## Exit reason mix")
    A("")
    all_reasons = set()
    for n in ("v740", "v15_baseline", "v15_full"):
        all_reasons |= set(data[n]["exit"].keys())
    A("| Reason | v7.4.0 | v15_baseline | v15_full |")
    A("|---|---:|---:|---:|")
    for r in sorted(all_reasons):
        cells = [r]
        for n in ("v740", "v15_baseline", "v15_full"):
            v = data[n]["exit"].get(r, {"n": 0, "pnl": 0})
            cells.append(f"${v['pnl']:,.2f} (n={v['n']})")
        A("| " + " | ".join(cells) + " |")
    A("")

    A("## Hold-time bucket")
    A("")
    A("| Bucket | v7.4.0 | v15_baseline | v15_full |")
    A("|---|---:|---:|---:|")
    for bkt in ["<5min", "5-15min", "15-30min", "30-60min", ">60min", "unknown"]:
        cells = [bkt]
        for n in ("v740", "v15_baseline", "v15_full"):
            v = data[n]["hold"][bkt]
            cells.append(f"${v['pnl']:,.2f} (n={v['n']})")
        A("| " + " | ".join(cells) + " |")
    A("")

    # Top / worst days (v15_full delta vs v740)
    all_dates = sorted(set(data["v740"]["daily"]) | set(data["v15_full"]["daily"]) | set(data["v15_baseline"]["daily"]))
    diffs = [(d, data["v15_full"]["daily"].get(d, 0) - data["v740"]["daily"].get(d, 0)) for d in all_dates]

    A("## Top 5 days where v15_full beat v7.4.0")
    A("")
    A("| Date | v7.4.0 | v15_baseline | v15_full | \u0394 vs v7.4.0 |")
    A("|---|---:|---:|---:|---:|")
    for d, dl in sorted(diffs, key=lambda x: -x[1])[:5]:
        a = data["v740"]["daily"].get(d, 0); b = data["v15_baseline"]["daily"].get(d, 0); c = data["v15_full"]["daily"].get(d, 0)
        A(f"| {d} | ${a:,.2f} | ${b:,.2f} | ${c:,.2f} | ${dl:+,.2f} |")
    A("")
    A("## Top 5 days where v15_full underperformed v7.4.0")
    A("")
    A("| Date | v7.4.0 | v15_baseline | v15_full | \u0394 vs v7.4.0 |")
    A("|---|---:|---:|---:|---:|")
    for d, dl in sorted(diffs, key=lambda x: x[1])[:5]:
        a = data["v740"]["daily"].get(d, 0); b = data["v15_baseline"]["daily"].get(d, 0); c = data["v15_full"]["daily"].get(d, 0)
        A(f"| {d} | ${a:,.2f} | ${b:,.2f} | ${c:,.2f} | ${dl:+,.2f} |")
    A("")

    A("## Daily detail")
    A("")
    A("| Date | v7.4.0 | v15_baseline | v15_full | \u0394 v15_full vs v7.4.0 |")
    A("|---|---:|---:|---:|---:|")
    for d in all_dates:
        a = data["v740"]["daily"].get(d, 0); b = data["v15_baseline"]["daily"].get(d, 0); c = data["v15_full"]["daily"].get(d, 0)
        A(f"| {d} | ${a:,.2f} | ${b:,.2f} | ${c:,.2f} | ${c-a:+,.2f} |")
    A("")

    A("---")
    A("")
    A("**Notes**")
    A("- v15 spec changes implemented as four flag-gated guards on the v15.0.0-experimental fork; baseline run flips them all OFF.")
    A("- Strike-distribution and Alarm E fire counts not surfaced because per-day JSON does not currently emit those fields. Exit-reason mix is the closest proxy for behavior shifts.")
    A("- Both v15 variants use V730 hysteresis + V740 ratchet (frac=0.5), so v15_baseline is an apples-to-apples flags-off comparator on the same code.")
    A("- v7.4.0 frac_05 column comes from `/home/user/workspace/v740_mfe_ratchet_84d/v740/frac_05/per_day/`.")
    A("")

    OUT_MD.write_text("\n".join(L))
    print(f"Wrote {OUT_MD}")
    for n in ("v740", "v15_baseline", "v15_full"):
        ov = data[n]["overall"]
        print(f"  {n:14s} ${ov['pnl']:>9,.2f}  pairs={ov['n']:>4d}  WR={ov['wr']:>5.1f}%  avg=${ov['avg']:>6.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
