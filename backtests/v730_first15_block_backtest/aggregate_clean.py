#!/usr/bin/env python3
"""Aggregate v7.3.0 first-15min entry-block backtest vs baseline.

Reuses the baseline at v730_regime_c_skip_backtest/baseline/per_day/
(same v7.2.7, prod settings, same corpus) and compares against the
new block15 results. Per-regime + headline comparison.
"""
from __future__ import annotations
import json
from pathlib import Path
from collections import defaultdict

BASELINE_DIR = Path("/home/user/workspace/v730_first15_block_backtest/baseline_clean/per_day")
BLOCK15_DIR  = Path("/home/user/workspace/v730_first15_block_backtest/block15_clean/per_day")
BARS = Path("/home/user/workspace/canonical_backtest_data_v707/replay_layout")
OUT = Path("/home/user/workspace/v730_first15_block_backtest")


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
    if r < -0.15:  return "B"
    if r <= 0.15:  return "C"
    if r <= 0.50:  return "D"
    return "E"


def load_variant(d: Path) -> dict[str, dict]:
    out = {}
    for p in sorted(d.glob("*.json")):
        try:
            j = json.loads(p.read_text())
        except Exception:
            continue
        date = p.stem
        pairs = j.get("pnl_pairs") or []
        summary = j.get("summary") or {}
        out[date] = {
            "pairs": pairs,
            "entries": int(summary.get("entries", 0) or 0),
            "exits":   int(summary.get("exits", 0) or 0),
        }
    return out


def _pnl(p):
    return float(p.get("pnl_dollars") or p.get("pnl") or 0)


def stats(pairs: list) -> dict:
    n = len(pairs)
    pnl = sum(_pnl(p) for p in pairs)
    wins = sum(1 for p in pairs if _pnl(p) > 0)
    losses = sum(1 for p in pairs if _pnl(p) < 0)
    flats = n - wins - losses
    wr = (wins / n * 100) if n else 0.0
    avg = (pnl / n) if n else 0.0
    return dict(n=n, pnl=pnl, wins=wins, losses=losses, flats=flats, wr=wr, avg=avg)


def by_side(pairs: list, side: str) -> list:
    return [p for p in pairs if str(p.get("side", "")).upper() == side]


def main():
    baseline = load_variant(BASELINE_DIR)
    block15  = load_variant(BLOCK15_DIR)

    common_dates = sorted(set(baseline) & set(block15))

    base_pairs = [p for d in common_dates for p in baseline[d]["pairs"]]
    blk_pairs  = [p for d in common_dates for p in block15[d]["pairs"]]

    base_s = stats(base_pairs)
    blk_s  = stats(blk_pairs)

    base_long  = stats(by_side(base_pairs, "LONG"))
    base_short = stats(by_side(base_pairs, "SHORT"))
    blk_long   = stats(by_side(blk_pairs, "LONG"))
    blk_short  = stats(by_side(blk_pairs, "SHORT"))
    # also accept lowercase
    base_long  = stats([p for p in base_pairs if str(p.get('side','')).upper()=='LONG'])
    base_short = stats([p for p in base_pairs if str(p.get('side','')).upper()=='SHORT'])
    blk_long   = stats([p for p in blk_pairs if str(p.get('side','')).upper()=='LONG'])
    blk_short  = stats([p for p in blk_pairs if str(p.get('side','')).upper()=='SHORT'])

    # Per-regime
    regimes = defaultdict(lambda: {"baseline": [], "block15": []})
    for d in common_dates:
        r = classify_regime(d) or "?"
        regimes[r]["baseline"].extend(baseline[d]["pairs"])
        regimes[r]["block15"].extend(block15[d]["pairs"])

    # Daily P&L deltas
    daily_deltas = []
    for d in common_dates:
        bp = sum(_pnl(p) for p in baseline[d]["pairs"])
        kp = sum(_pnl(p) for p in block15[d]["pairs"])
        be = baseline[d]["entries"]
        ke = block15[d]["entries"]
        daily_deltas.append((d, classify_regime(d) or "?", be, ke, bp, kp, kp - bp))

    # Days where block15 outperformed by big margin
    daily_deltas_sorted = sorted(daily_deltas, key=lambda x: x[6])

    # Build report
    lines = []
    a = lines.append
    a("# v7.3.0 First-15min Entry Block — 83-day Backtest")
    a("")
    a("**Hypothesis**: blocking new entries within 15 min of 09:30 ET (i.e., no entries before 09:45 ET) avoids the worst-WR window across all regimes.")
    a("")
    a("**Setup**")
    a("- Bot version: v7.2.7 (current prod)")
    a("- Settings: L=30 / S=30 / VOLUME_GATE_ENABLED=true / RATIO=0.85 (live prod as of 2026-05-07)")
    a("- Corpus: 83 days, 2026-01-02 → 2026-05-01, v7.0.7 SIP archive")
    a("- Universe: 12 prod tickers + warmup seeded (55 days of pre-corpus history)")
    a("- Variant: `V730_FIRST_N_MIN_BLOCK=15` env-gated guard in `broker/orders.py`")
    a("")
    a("## Headline")
    a("")
    a("| Metric | Baseline | +15min Block | Δ |")
    a("|---|---:|---:|---:|")
    a(f"| Net P&L (83d) | ${base_s['pnl']:,.2f} | ${blk_s['pnl']:,.2f} | **${blk_s['pnl']-base_s['pnl']:+,.2f}** |")
    a(f"| Pairs | {base_s['n']} | {blk_s['n']} | {blk_s['n']-base_s['n']:+d} |")
    a(f"| Win Rate | {base_s['wr']:.1f}% | {blk_s['wr']:.1f}% | {blk_s['wr']-base_s['wr']:+.1f}pp |")
    a(f"| Avg / Trade | ${base_s['avg']:.2f} | ${blk_s['avg']:.2f} | ${blk_s['avg']-base_s['avg']:+.2f} |")
    a(f"| Avg / Day | ${base_s['pnl']/len(common_dates):.2f} | ${blk_s['pnl']/len(common_dates):.2f} | ${(blk_s['pnl']-base_s['pnl'])/len(common_dates):+.2f} |")
    a(f"| Wins / Losses / Flats | {base_s['wins']}/{base_s['losses']}/{base_s['flats']} | {blk_s['wins']}/{blk_s['losses']}/{blk_s['flats']} | |")
    a("")
    a(f"**Offline projection was +$484 / +1.1pp WR. Replay-mode result: ${blk_s['pnl']-base_s['pnl']:+,.0f} / {blk_s['wr']-base_s['wr']:+.1f}pp.**")
    a("")
    a("## Long vs Short")
    a("")
    a("| Side | Variant | Pairs | P&L | WR | Avg |")
    a("|---|---|---:|---:|---:|---:|")
    a(f"| LONG | Baseline | {base_long['n']} | ${base_long['pnl']:,.2f} | {base_long['wr']:.1f}% | ${base_long['avg']:.2f} |")
    a(f"| LONG | +15min   | {blk_long['n']} | ${blk_long['pnl']:,.2f} | {blk_long['wr']:.1f}% | ${blk_long['avg']:.2f} |")
    a(f"| SHORT | Baseline | {base_short['n']} | ${base_short['pnl']:,.2f} | {base_short['wr']:.1f}% | ${base_short['avg']:.2f} |")
    a(f"| SHORT | +15min   | {blk_short['n']} | ${blk_short['pnl']:,.2f} | {blk_short['wr']:.1f}% | ${blk_short['avg']:.2f} |")
    a("")
    a("## Per-Regime Breakdown")
    a("")
    a("| Regime | Days | Base Pairs | Base P&L | Base WR | Blk Pairs | Blk P&L | Blk WR | ΔP&L | ΔWR |")
    a("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in ["A", "B", "C", "D", "E", "?"]:
        if r not in regimes: continue
        bp = stats(regimes[r]["baseline"])
        kp = stats(regimes[r]["block15"])
        n_days = sum(1 for d in common_dates if (classify_regime(d) or "?") == r)
        a(f"| {r} | {n_days} | {bp['n']} | ${bp['pnl']:,.0f} | {bp['wr']:.1f}% | {kp['n']} | ${kp['pnl']:,.0f} | {kp['wr']:.1f}% | ${kp['pnl']-bp['pnl']:+,.0f} | {kp['wr']-bp['wr']:+.1f}pp |")
    a("")
    a("## Top 5 Gain Days (block15 vs baseline)")
    a("")
    a("| Date | Regime | Base Entries | Blk Entries | Base P&L | Blk P&L | ΔP&L |")
    a("|---|---|---:|---:|---:|---:|---:|")
    for row in daily_deltas_sorted[-5:][::-1]:
        d, r, be, ke, bp, kp, delta = row
        a(f"| {d} | {r} | {be} | {ke} | ${bp:,.2f} | ${kp:,.2f} | **${delta:+,.2f}** |")
    a("")
    a("## Worst 5 Days (block15 vs baseline)")
    a("")
    a("| Date | Regime | Base Entries | Blk Entries | Base P&L | Blk P&L | ΔP&L |")
    a("|---|---|---:|---:|---:|---:|---:|")
    for row in daily_deltas_sorted[:5]:
        d, r, be, ke, bp, kp, delta = row
        a(f"| {d} | {r} | {be} | {ke} | ${bp:,.2f} | ${kp:,.2f} | **${delta:+,.2f}** |")
    a("")
    n_better = sum(1 for x in daily_deltas if x[6] > 0)
    n_worse  = sum(1 for x in daily_deltas if x[6] < 0)
    n_same   = sum(1 for x in daily_deltas if x[6] == 0)
    a(f"**Day-level**: block15 better on **{n_better}** days, worse on **{n_worse}**, same on **{n_same}**.")
    a("")
    a("## Recommendation")
    a("")
    if blk_s['pnl'] - base_s['pnl'] > 5000:
        a("**SHIP.** First-15min entry block is a clean, large win. Add to v7.3.0 production env (`V730_FIRST_N_MIN_BLOCK=15`).")
    elif blk_s['pnl'] - base_s['pnl'] > 0:
        a("**Conditional ship.** Positive but smaller than top-band — review per-regime to confirm no regime is hurt materially.")
    else:
        a("**Don't ship.** Replay-mode test failed to reproduce the offline projection.")
    a("")
    a("---")
    a(f"_Generated from `{BASELINE_DIR}` and `{BLOCK15_DIR}`. Common dates: {len(common_dates)}._")

    rpt = OUT / "REPORT_clean.md"
    rpt.write_text("\n".join(lines))
    print(f"Wrote {rpt}")
    print(f"Headline: ΔP&L=${blk_s['pnl']-base_s['pnl']:+,.0f}, ΔWR={blk_s['wr']-base_s['wr']:+.1f}pp")


if __name__ == "__main__":
    main()
