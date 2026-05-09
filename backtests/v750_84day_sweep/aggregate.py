#!/usr/bin/env python3
"""Aggregate v7.5.0 Filter #3 sweep (3 variants) vs v7.4.0 baseline.

Variants compared:
  v750_off       \\u2014 Filter OFF (mirrors v7.4.0; should match v740 baseline to penny)
  v750_w120_t5   \\u2014 120s window, $5 red threshold (aggressive)
  v750_w180_t10  \\u2014 180s window, $10 red threshold (conservative)
"""
from __future__ import annotations
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median

ROOT = Path("/home/user/workspace/v750_84day_sweep")
VARIANTS = {
    "v750_off":       ROOT / "v750_off" / "per_day",
    "v750_w120_t5":   ROOT / "v750_w120_t5" / "per_day",
    "v750_w180_t10":  ROOT / "v750_w180_t10" / "per_day",
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
            x = dict(x); x["_date"] = date
            pairs.append(x)
        daily[date] = sum(x.get("pnl_dollars", x.get("pnl", 0)) for x in pp)
    return pairs, daily


def load_exits(per_day_dir: Path):
    idx = {}
    for p in sorted(per_day_dir.glob("*.json")):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        for ex in d.get("exits", []) or []:
            key = (p.stem, ex.get("ticker"), ex.get("ts"))
            idx[key] = ex.get("reason") or "unknown"
    return idx


def hold_minutes(p):
    try:
        et = p.get("entry_ts") or p.get("entry_ts_utc")
        xt = p.get("exit_ts") or p.get("exit_ts_utc")
        if not (et and xt): return None
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
    gross_w = sum(wins); gross_l = abs(sum(losses))
    return {
        "n": len(pairs),
        "pnl": round(sum(pnls), 2),
        "wins": len(wins),
        "losses": len(losses),
        "wr": round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
        "avg": round(sum(pnls) / len(pnls), 2) if pnls else 0,
        "median": round(median(pnls), 2) if pnls else 0,
        "gross_win": round(gross_w, 2),
        "gross_loss": round(gross_l, 2),
        "profit_factor": round(gross_w / gross_l, 3) if gross_l > 0 else None,
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

    L = []; A = L.append
    A("# v7.5.0 Filter #3 (Early-Ditch) \\u2014 83-Day SIP Sweep")
    A("")
    A("**Branch:** `experiment/v7.5.0-early-ditch` @ `7176421`  ")
    A("**Corpus:** 83-day SIP (canonical_backtest_data_v707), 12 prod tickers  ")
    A("**Wall:** ~33.6 min total, 3 variants, 2 workers, slot-reuse")
    A("")
    A("Three variants run side-by-side, identical wiring on the v7.5.0 codebase:")
    A("- **v750_off**       \\u2014 Filter #3 disabled (mirrors v7.4.0 main behaviour)")
    A("- **v750_w120_t5**   \\u2014 120s window, $5 red threshold (aggressive)")
    A("- **v750_w180_t10**  \\u2014 180s window, $10 red threshold (conservative)")
    A("")
    A("All three keep V730 hysteresis ON (BARS=2, DEEP=0.0075) and V740 ratchet ON (frac=0.5).")
    A("")

    A("## Headline")
    A("")
    A("| Variant | Total P/L | \\u0394 vs v750_off | \\u0394 vs v7.4.0 frac_05 | Pairs | WR | Avg | PF |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|")
    base = data["v750_off"]["overall"]["pnl"]
    v740 = data["v740"]["overall"]["pnl"]
    v740_ov = data["v740"]["overall"]
    A(f"| v7.4.0 frac_05 (prod baseline) | ${v740:,.2f} | \\u2014 | \\u2014 | {v740_ov['n']} | {v740_ov['wr']}% | ${v740_ov['avg']:.2f} | {v740_ov['profit_factor']} |")
    for name in ("v750_off", "v750_w120_t5", "v750_w180_t10"):
        ov = data[name]["overall"]
        A(f"| {name} | ${ov['pnl']:,.2f} | ${ov['pnl']-base:+,.2f} | ${ov['pnl']-v740:+,.2f} | {ov['n']} | {ov['wr']}% | ${ov['avg']:.2f} | {ov['profit_factor']} |")
    A("")

    # Verdict
    pnls = {n: data[n]["overall"]["pnl"] for n in ("v750_off","v750_w120_t5","v750_w180_t10")}
    winner = max(pnls, key=pnls.get)
    delta_v740 = pnls[winner] - v740
    delta_off = pnls[winner] - base
    if winner == "v750_off":
        verdict = (f"**No-go: filter ON did not beat OFF.** "
                   f"v750_off={pnls['v750_off']:+.2f}, "
                   f"v750_w120_t5={pnls['v750_w120_t5']:+.2f} ({pnls['v750_w120_t5']-base:+.2f}), "
                   f"v750_w180_t10={pnls['v750_w180_t10']:+.2f} ({pnls['v750_w180_t10']-base:+.2f}). "
                   f"DO NOT SHIP.")
    else:
        verdict = (f"**Ship recommendation: `{winner}` wins.** "
                   f"+${delta_v740:,.2f} vs v7.4.0 prod baseline "
                   f"(+{delta_v740/abs(v740)*100:.1f}%), "
                   f"+${delta_off:,.2f} vs v750_off on the same codebase.")
    A(verdict)
    A("")

    # Conformance check
    drift = base - v740
    A(f"**Conformance check:** v750_off vs v7.4.0 prod baseline drift = ${drift:+,.2f}. "
      f"Expected ~$0 (the filter is OFF, code paths should be identical).")
    A("")

    A("## Long vs Short")
    A("")
    A("| Variant | LONG pairs | LONG P/L | LONG WR | SHORT pairs | SHORT P/L | SHORT WR |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    for name in ("v740", "v750_off", "v750_w120_t5", "v750_w180_t10"):
        Lg = data[name]["side"]["long"]; Sh = data[name]["side"]["short"]
        A(f"| {name} | {Lg['n']} | ${Lg['pnl']:,.2f} | {Lg['wr']}% | {Sh['n']} | ${Sh['pnl']:,.2f} | {Sh['wr']}% |")
    A("")

    A("## By Month")
    A("")
    A("| Month | v7.4.0 | v750_off | v750_w120_t5 | v750_w180_t10 | \\u0394 winner vs v7.4.0 |")
    A("|---|---:|---:|---:|---:|---:|")
    months = sorted(set().union(*[data[n]["month"].keys() for n in ("v740","v750_off","v750_w120_t5","v750_w180_t10")]))
    for m in months:
        v = {n: data[n]["month"].get(m, {"pnl":0,"n":0}) for n in ("v740","v750_off","v750_w120_t5","v750_w180_t10")}
        d_w = v[winner]["pnl"] - v["v740"]["pnl"]
        A(f"| {m} | ${v['v740']['pnl']:,.2f} (n={v['v740']['n']}) | ${v['v750_off']['pnl']:,.2f} | ${v['v750_w120_t5']['pnl']:,.2f} | ${v['v750_w180_t10']['pnl']:,.2f} | ${d_w:+,.2f} |")
    A("")

    A("## By Ticker")
    A("")
    A("| Ticker | v7.4.0 | v750_off | v750_w120_t5 | v750_w180_t10 | \\u0394 winner vs v7.4.0 |")
    A("|---|---:|---:|---:|---:|---:|")
    tickers = sorted(set().union(*[data[n]["ticker"].keys() for n in ("v740","v750_off","v750_w120_t5","v750_w180_t10")]))
    for t in tickers:
        v = {n: data[n]["ticker"].get(t, {"pnl":0,"n":0}) for n in ("v740","v750_off","v750_w120_t5","v750_w180_t10")}
        d_w = v[winner]["pnl"] - v["v740"]["pnl"]
        A(f"| {t} | ${v['v740']['pnl']:,.2f} (n={v['v740']['n']}) | ${v['v750_off']['pnl']:,.2f} (n={v['v750_off']['n']}) | ${v['v750_w120_t5']['pnl']:,.2f} (n={v['v750_w120_t5']['n']}) | ${v['v750_w180_t10']['pnl']:,.2f} (n={v['v750_w180_t10']['n']}) | ${d_w:+,.2f} |")
    A("")

    A("## Hold-time bucket")
    A("")
    A("| Bucket | v7.4.0 | v750_off | v750_w120_t5 | v750_w180_t10 |")
    A("|---|---:|---:|---:|---:|")
    for bkt in ["<5min","5-15min","15-30min","30-60min",">60min","unknown"]:
        cells = [bkt]
        for n in ("v740","v750_off","v750_w120_t5","v750_w180_t10"):
            v = data[n]["hold"][bkt]
            cells.append(f"${v['pnl']:,.2f} (n={v['n']}, WR={v['wr']}%)")
        A("| " + " | ".join(cells) + " |")
    A("")

    A("## Exit reason mix")
    A("")
    all_reasons = set()
    for n in ("v740","v750_off","v750_w120_t5","v750_w180_t10"):
        all_reasons |= set(data[n]["exit"].keys())
    A("| Reason | v7.4.0 | v750_off | v750_w120_t5 | v750_w180_t10 |")
    A("|---|---:|---:|---:|---:|")
    for r in sorted(all_reasons):
        cells = [r]
        for n in ("v740","v750_off","v750_w120_t5","v750_w180_t10"):
            v = data[n]["exit"].get(r, {"n":0,"pnl":0})
            cells.append(f"${v['pnl']:,.2f} (n={v['n']})")
        A("| " + " | ".join(cells) + " |")
    A("")

    # Top / worst days for the winner vs v740
    all_dates = sorted(set(data["v740"]["daily"]) | set(data[winner]["daily"]))
    diffs = [(d, data[winner]["daily"].get(d,0) - data["v740"]["daily"].get(d,0)) for d in all_dates]

    A(f"## Top 5 days where {winner} beat v7.4.0")
    A("")
    A("| Date | v7.4.0 | v750_off | v750_w120_t5 | v750_w180_t10 | \\u0394 winner vs v7.4.0 |")
    A("|---|---:|---:|---:|---:|---:|")
    for d, dl in sorted(diffs, key=lambda x: -x[1])[:5]:
        a = data["v740"]["daily"].get(d, 0)
        b = data["v750_off"]["daily"].get(d, 0)
        c = data["v750_w120_t5"]["daily"].get(d, 0)
        e = data["v750_w180_t10"]["daily"].get(d, 0)
        A(f"| {d} | ${a:,.2f} | ${b:,.2f} | ${c:,.2f} | ${e:,.2f} | ${dl:+,.2f} |")
    A("")
    A(f"## Top 5 days where {winner} underperformed v7.4.0")
    A("")
    A("| Date | v7.4.0 | v750_off | v750_w120_t5 | v750_w180_t10 | \\u0394 winner vs v7.4.0 |")
    A("|---|---:|---:|---:|---:|---:|")
    for d, dl in sorted(diffs, key=lambda x: x[1])[:5]:
        a = data["v740"]["daily"].get(d, 0)
        b = data["v750_off"]["daily"].get(d, 0)
        c = data["v750_w120_t5"]["daily"].get(d, 0)
        e = data["v750_w180_t10"]["daily"].get(d, 0)
        A(f"| {d} | ${a:,.2f} | ${b:,.2f} | ${c:,.2f} | ${e:,.2f} | ${dl:+,.2f} |")
    A("")

    A("## Daily detail")
    A("")
    A("| Date | v7.4.0 | v750_off | v750_w120_t5 | v750_w180_t10 |")
    A("|---|---:|---:|---:|---:|")
    for d in all_dates:
        a = data["v740"]["daily"].get(d, 0)
        b = data["v750_off"]["daily"].get(d, 0)
        c = data["v750_w120_t5"]["daily"].get(d, 0)
        e = data["v750_w180_t10"]["daily"].get(d, 0)
        A(f"| {d} | ${a:,.2f} | ${b:,.2f} | ${c:,.2f} | ${e:,.2f} |")
    A("")

    A("---")
    A("")
    A("**Notes**")
    A("- v7.5.0 implements Filter #3 (Early-Ditch) as a flag-gated guard at the top of `evaluate_sentinel`, before Alarm A. When fired, it short-circuits and emits `exit_reason='v750_early_ditch'`.")
    A("- The `v750_off` column is the apples-to-apples flags-off comparator on the v7.5.0 codebase; the small drift vs v7.4.0 baseline (see conformance check above) tells us whether the v7.5.0 fork has any non-flag-gated drift.")
    A("- Backtest cadence is 1 minute, so the filter window must be \\u2265 120s to cover at least 2 bar evaluations (age=60s and age=120s). 90s only catches age=60s and almost never fires.")
    A("- Run paths: per-day JSON in `<root>/<variant>/per_day/`; aggregate.json + REPORT.md at root.")
    A("")

    OUT_MD.write_text("\n".join(L))
    print(f"Wrote {OUT_MD}")
    for n in ("v740","v750_off","v750_w120_t5","v750_w180_t10"):
        ov = data[n]["overall"]
        print(f"  {n:14s} ${ov['pnl']:>9,.2f}  pairs={ov['n']:>4d}  WR={ov['wr']:>5.1f}%  PF={ov['profit_factor']}  avg=${ov['avg']:>6.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
