"""Side-by-side per-day diff between sim batch JSON and backtest per_day dir.

Usage:
  python scripts/diff_sim_vs_backtest.py /tmp/sim_dedup.json /tmp/keystone_dedup
"""

import glob
import json
import os
import sys


def main():
    if len(sys.argv) != 3:
        sys.exit("Usage: diff_sim_vs_backtest.py <sim.json> <backtest_out_dir>")
    sim_path, bt_dir = sys.argv[1], sys.argv[2]

    sim = json.load(open(sim_path))
    sim_by_date = {x["date"]: x for x in sim}

    bt_data = {}
    for f in glob.glob(os.path.join(bt_dir, "per_day", "*.json")):
        d = json.load(open(f))
        bt_data[d["date"]] = d

    sim_dates = set(sim_by_date)
    bt_dates = set(bt_data)
    common = sorted(sim_dates & bt_dates)
    print(f"Sim days:      {len(sim_dates)}")
    print(f"Backtest days: {len(bt_dates)}")
    print(f"Common days:   {len(common)}")
    print(f"Sim-only:      {len(sim_dates - bt_dates)}")
    print(f"Backtest-only: {len(bt_dates - sim_dates)}")
    if not common:
        return

    # Sum morning P&L per side
    def sim_morn_pl(d):
        s = sim_by_date[d]
        return sum(
            (e.get("pnl", 0) or 0)
            for e in (s.get("exits") or [])
            if not e["reason"].startswith("EOD")
        )

    def bt_pl(d):
        return (bt_data[d].get("summary") or {}).get("total_pnl", 0.0)

    sim_total = sum(sim_morn_pl(d) for d in common)
    bt_total = sum(bt_pl(d) for d in common)
    print()
    print("Common-day MORNING P&L:")
    n = len(common)
    print(f"  Sim:      ${sim_total:8.0f}  ({sim_total * 252 / n:+.0f}/yr)")
    print(f"  Backtest: ${bt_total:8.0f}  ({bt_total * 252 / n:+.0f}/yr)")
    print(f"  Diff (bt - sim): ${bt_total - sim_total:+.0f}")

    # Match-quality
    diffs = sorted(((bt_pl(d) - sim_morn_pl(d), d) for d in common), key=lambda x: abs(x[0]), reverse=True)
    abs_diffs = [abs(x[0]) for x in diffs]
    within = lambda thr: sum(1 for x in abs_diffs if x < thr)
    print()
    print("Match quality:")
    for thr in (100, 250, 500, 1000):
        c = within(thr)
        print(f"  within ${thr}: {c:3d}/{n} ({100*c/n:.0f}%)")

    # Day classification
    both_fire = 0
    both_zero = 0
    bt_only_a = 0
    sim_only_a = 0
    bt_only_pl = 0
    sim_only_pl = 0
    for d in common:
        s_morn = [e for e in (sim_by_date[d].get("entries") or [])
                  if e.get("bucket", 9999) < 900]
        b_ent = bt_data[d].get("entries") or []
        if s_morn and b_ent:
            both_fire += 1
        elif not s_morn and not b_ent:
            both_zero += 1
        elif b_ent:
            bt_only_a += 1
            bt_only_pl += bt_pl(d)
        else:
            sim_only_a += 1
            sim_only_pl += sim_morn_pl(d)

    print()
    print(f"Day classification ({n} common days):")
    print(f"  Both fire:      {both_fire}")
    print(f"  Neither fires:  {both_zero}")
    print(f"  Backtest-only:  {bt_only_a}  (bt pnl ${bt_only_pl:+.0f})")
    print(f"  Sim-only:       {sim_only_a}  (sim pnl ${sim_only_pl:+.0f})")

    # Same-day-both-fire residual
    both_dates = []
    for d in common:
        s_morn = [e for e in (sim_by_date[d].get("entries") or [])
                  if e.get("bucket", 9999) < 900]
        if s_morn and (bt_data[d].get("entries") or []):
            both_dates.append(d)
    both_bt = sum(bt_pl(d) for d in both_dates)
    both_sim = sum(sim_morn_pl(d) for d in both_dates)
    print(f"\nBoth-fire residual ({len(both_dates)} days):")
    print(f"  Backtest sum: ${both_bt:+.0f}")
    print(f"  Sim sum:      ${both_sim:+.0f}")
    print(f"  Diff:         ${both_bt - both_sim:+.0f}")

    print()
    print("Top 10 biggest single-day diffs (bt - sim):")
    for diff, d in diffs[:10]:
        print(f"  {d}: bt=${bt_pl(d):+8.0f}  sim=${sim_morn_pl(d):+8.0f}  diff=${diff:+.0f}")


if __name__ == "__main__":
    main()
