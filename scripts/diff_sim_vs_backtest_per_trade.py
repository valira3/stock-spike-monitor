"""Per-trade detail diff -- for days where both sim and backtest fire on
the SAME ticker+side, compare entry price, entry time, exit price,
exit reason. Surfaces residual divergence (slippage, cooldown timing,
ATR-stop placement, signal-bar selection).

Usage:
  python scripts/diff_sim_vs_backtest_per_trade.py /tmp/sim_dedup.json /tmp/keystone_dedup
"""

import glob
import json
import os
import sys
from statistics import median


def fmt_bucket(b):
    return f"{b // 60:02d}:{b % 60:02d}"


def parse_ts_to_bucket(ts):
    """ts like '2025-09-11T10:05:00-05:00' -> minutes-since-midnight ET."""
    hh, mm = ts[11:13], ts[14:16]
    return int(hh) * 60 + int(mm)


def main():
    if len(sys.argv) != 3:
        sys.exit("Usage: diff_sim_vs_backtest_per_trade.py <sim.json> <backtest_out_dir>")
    sim_path, bt_dir = sys.argv[1], sys.argv[2]

    sim = json.load(open(sim_path))
    sim_by_date = {x["date"]: x for x in sim}
    bt_data = {}
    for f in glob.glob(os.path.join(bt_dir, "per_day", "*.json")):
        d = json.load(open(f))
        bt_data[d["date"]] = d

    common = sorted(set(sim_by_date) & set(bt_data))

    # Build a list of matched trades by (date, ticker, side)
    matched = []  # (date, ticker, side, sim_trade, bt_trade)
    sim_only_trades = []
    bt_only_trades = []
    for d in common:
        s = sim_by_date[d]
        b = bt_data[d]
        # Sim morning trades: pair entry+exit by ticker
        sim_entries = [e for e in (s.get("entries") or [])
                       if e.get("bucket", 9999) < 900]
        sim_exits = [e for e in (s.get("exits") or [])
                     if not e["reason"].startswith("EOD")]
        # Best-effort pair sim entries to exits by ticker+bucket order
        sim_trades = []
        for ent in sim_entries:
            tk = ent["ticker"]
            side = ent["side"].lower()
            ent_b = ent["bucket"]
            # find earliest matching exit on same ticker with bucket >= ent_b
            ex = None
            for e in sim_exits:
                if e["ticker"] != tk:
                    continue
                if e["bucket"] < ent_b:
                    continue
                if e.get("_paired"):
                    continue
                ex = e
                ex["_paired"] = True
                break
            sim_trades.append({
                "ticker": tk,
                "side": side,
                "entry_bucket": ent_b,
                "entry_price": ent["price"],
                "exit_bucket": ex["bucket"] if ex else None,
                "exit_price": ex["price"] if ex else None,
                "exit_reason": ex["reason"] if ex else "open",
                "pnl": ex.get("pnl", 0) if ex else 0,
            })

        # Backtest pnl_pairs
        bt_trades = []
        for p in (b.get("pnl_pairs") or []):
            bt_trades.append({
                "ticker": p["ticker"],
                "side": p["side"],
                "entry_bucket": parse_ts_to_bucket(p["entry_ts"]),
                "entry_price": p["entry_price"],
                "exit_bucket": parse_ts_to_bucket(p["exit_ts"]),
                "exit_price": p["exit_price"],
                "exit_reason": p["exit_reason"],
                "pnl": p["pnl_dollars"],
            })

        # Match by (ticker, side) -- first match
        s_keys = [(t["ticker"], t["side"]) for t in sim_trades]
        for bt in bt_trades:
            key = (bt["ticker"], bt["side"])
            if key in s_keys:
                idx = s_keys.index(key)
                matched.append((d, bt["ticker"], bt["side"], sim_trades[idx], bt))
                s_keys[idx] = None  # consume
            else:
                bt_only_trades.append((d, bt))
        for i, st in enumerate(sim_trades):
            if s_keys[i] is not None:
                sim_only_trades.append((d, st))

    print(f"Matched trades (same date+ticker+side): {len(matched)}")
    print(f"Sim-only trades:      {len(sim_only_trades)}")
    print(f"Backtest-only trades: {len(bt_only_trades)}")

    if matched:
        # Entry price diff distribution
        entry_diffs_bps = []
        same_entry_bucket = 0
        for d, tk, side, st, bt in matched:
            ent_diff = (bt["entry_price"] - st["entry_price"]) / st["entry_price"] * 10000
            entry_diffs_bps.append(ent_diff)
            if st["entry_bucket"] == bt["entry_bucket"]:
                same_entry_bucket += 1
        abs_bps = sorted(abs(x) for x in entry_diffs_bps)
        n = len(matched)
        print(f"\nEntry price |diff| in bps (across {n} matched trades):")
        print(f"  median: {abs_bps[n // 2]:.2f}")
        print(f"  p75:    {abs_bps[n * 3 // 4]:.2f}")
        print(f"  p90:    {abs_bps[n * 9 // 10]:.2f}")
        print(f"  p99:    {abs_bps[min(n - 1, n * 99 // 100)]:.2f}")
        print(f"  max:    {abs_bps[-1]:.2f}")
        print(f"  Same entry bucket: {same_entry_bucket}/{n} ({100*same_entry_bucket/n:.0f}%)")

        # P&L attribution
        sum_bt = sum(bt["pnl"] for _, _, _, _, bt in matched)
        sum_sim = sum(st["pnl"] for _, _, _, st, _ in matched)
        print(f"\nMatched-trade P&L:")
        print(f"  Sim total:      ${sum_sim:+.0f}")
        print(f"  Backtest total: ${sum_bt:+.0f}")
        print(f"  Diff:           ${sum_bt - sum_sim:+.0f}")

        # Worst per-trade divergences
        ranked = sorted(matched, key=lambda x: abs(x[4]["pnl"] - x[3]["pnl"]), reverse=True)
        print(f"\nTop 8 per-trade pnl divergences:")
        for d, tk, side, st, bt in ranked[:8]:
            ent_diff_bps = (bt["entry_price"] - st["entry_price"]) / st["entry_price"] * 10000
            print(
                f"  {d} {tk:5} {side:5} "
                f"sim_ent={fmt_bucket(st['entry_bucket'])}@${st['entry_price']:.2f} "
                f"(exit {st['exit_reason']:15} ${st['pnl']:+7.0f})  "
                f"bt_ent={fmt_bucket(bt['entry_bucket'])}@${bt['entry_price']:.2f} "
                f"(exit {bt['exit_reason']:8} ${bt['pnl']:+7.0f})  "
                f"ent_bps={ent_diff_bps:+.1f}"
            )

    # Single-side trades (admission divergence)
    bt_only_pl = sum(t["pnl"] for _, t in bt_only_trades)
    sim_only_pl = sum(t["pnl"] for _, t in sim_only_trades)
    print(f"\nAdmission divergence:")
    print(f"  Backtest-only trades P&L: ${bt_only_pl:+.0f}")
    print(f"  Sim-only trades P&L:      ${sim_only_pl:+.0f}")


if __name__ == "__main__":
    main()
