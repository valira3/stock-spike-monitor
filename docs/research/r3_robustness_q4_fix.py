"""Round 3: refine R2 winner (T5+cut1100+vix20) -- target Q4 2025
which still bled -$1,634 even with the best Round-2 settings.

Q4 forensic: NFLX in Oct stacked 4 trades on 10/27 (-$2,771), 2 more on
10/30 (-$2,040), 1945 on 10/02. TSLA Dec longs took stops. So tests:

A. Robustness grid around the winner (fine cutoff/VIX)
B. Block NFLX (both sides) -- direct Q4 fix candidate
C. Cap stacking (max_trades_per_day 2 or 3)
D. Tighter range cap (skip widest 1% of days)
E. Lower per-trade risk %
F. Combinations
"""
import json, os, subprocess, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

UNIV = "AAPL,MSFT,NVDA,TSLA,META,GOOG,AMZN,AVGO,NFLX,ORCL,SPY,QQQ"

# T5 baseline blocklist
T5_BLOCK = '{"META":["LONG","SHORT"],"MSFT":["LONG","SHORT"],"AAPL":["LONG","SHORT"],"AMZN":["LONG","SHORT"],"GOOG":["LONG","SHORT"],"AVGO":["LONG","SHORT"]}'
T5_PLUS_NFLX = '{"META":["LONG","SHORT"],"MSFT":["LONG","SHORT"],"AAPL":["LONG","SHORT"],"AMZN":["LONG","SHORT"],"GOOG":["LONG","SHORT"],"AVGO":["LONG","SHORT"],"NFLX":["LONG","SHORT"]}'
T5_PLUS_NFLX_S = '{"META":["LONG","SHORT"],"MSFT":["LONG","SHORT"],"AAPL":["LONG","SHORT"],"AMZN":["LONG","SHORT"],"GOOG":["LONG","SHORT"],"AVGO":["LONG","SHORT"],"NFLX":["SHORT"]}'

# Baseline = R2 winner config
BASE = {
    "ORB_COMPOUND_DAILY":"1","ORB_STOP_BUFFER_BPS":"5",
    "ORB_OR_MINUTES":"30","ORB_RR":"2.5",
    "ORB_RANGE_MIN_PCT":"0.008","ORB_RANGE_MAX_PCT":"0.025",
    "ORB_MAX_TRADES_PER_DAY":"5","ORB_RISK_PER_TRADE_PCT":"2.00",
    "ORB_MAX_CONCURRENT_RISK_DOLLARS":"2000","ORB_DAILY_LOSS_KILL_PCT":"2.0",
    "ORB_MAX_TRADE_NOTIONAL_PCT":"75","ORB_MOVE_TO_BE_AFTER_1R":"1",
    "ORB_TICKER_SIDE_BLOCKLIST": T5_BLOCK,
    "ORB_SKIP_GAP_ABOVE_PCT":"1.5","ORB_SKIP_EARNINGS_WINDOW":"1",
    "ORB_EARNINGS_DAYS_BEFORE":"1","ORB_SKIP_VIX_ABOVE":"20",
    "ORB_TIME_CUTOFF_ET":"11:00","ORB_EOD_CUTOFF_ET":"15:55",
    "ORB_ACCOUNT":"100000",
    "ORB_ENTRY_SLIPPAGE_BPS":"1.5","ORB_EXIT_SLIPPAGE_BPS":"1.5",
    "ORB_STOP_KICK_BPS":"5.0","ORB_SHORT_PENALTY_BPS":"1.0",
}

THEORIES = [
    # A: robustness grid around the R2 winner
    ("R3_baseline_recheck", {}),                                       # sanity
    ("R3_cut1030_vix20", {"ORB_TIME_CUTOFF_ET":"10:30"}),
    ("R3_cut1130_vix20", {"ORB_TIME_CUTOFF_ET":"11:30"}),
    ("R3_cut1200_vix20", {"ORB_TIME_CUTOFF_ET":"12:00"}),
    ("R3_cut1100_vix19", {"ORB_SKIP_VIX_ABOVE":"19"}),
    ("R3_cut1100_vix21", {"ORB_SKIP_VIX_ABOVE":"21"}),
    ("R3_cut1100_vix22", {"ORB_SKIP_VIX_ABOVE":"22"}),

    # B: block NFLX (the Q4 bleeder)
    ("R3_nflx_full_block",  {"ORB_TICKER_SIDE_BLOCKLIST": T5_PLUS_NFLX}),
    ("R3_nflx_short_block", {"ORB_TICKER_SIDE_BLOCKLIST": T5_PLUS_NFLX_S}),

    # C: cap stacking
    ("R3_max3_per_day", {"ORB_MAX_TRADES_PER_DAY":"3"}),
    ("R3_max2_per_day", {"ORB_MAX_TRADES_PER_DAY":"2"}),

    # D: tighter range cap (skip the widest-OR days, NFLX 10/27 was wide)
    ("R3_range_max_022", {"ORB_RANGE_MAX_PCT":"0.022"}),
    ("R3_range_max_020", {"ORB_RANGE_MAX_PCT":"0.020"}),
    ("R3_range_max_018", {"ORB_RANGE_MAX_PCT":"0.018"}),

    # E: lower per-trade risk
    ("R3_risk_1pt5", {"ORB_RISK_PER_TRADE_PCT":"1.5"}),
    ("R3_risk_1pt0", {"ORB_RISK_PER_TRADE_PCT":"1.0"}),

    # F: combinations targeting Q4
    ("R3_nflx_block_max3", {
        "ORB_TICKER_SIDE_BLOCKLIST": T5_PLUS_NFLX,
        "ORB_MAX_TRADES_PER_DAY":"3"}),
    ("R3_nflx_block_range020", {
        "ORB_TICKER_SIDE_BLOCKLIST": T5_PLUS_NFLX,
        "ORB_RANGE_MAX_PCT":"0.020"}),
    ("R3_nflx_block_risk1pt5", {
        "ORB_TICKER_SIDE_BLOCKLIST": T5_PLUS_NFLX,
        "ORB_RISK_PER_TRADE_PCT":"1.5"}),
    ("R3_nflx_block_cut1030", {
        "ORB_TICKER_SIDE_BLOCKLIST": T5_PLUS_NFLX,
        "ORB_TIME_CUTOFF_ET":"10:30"}),
    ("R3_nflx_block_max3_range020", {
        "ORB_TICKER_SIDE_BLOCKLIST": T5_PLUS_NFLX,
        "ORB_MAX_TRADES_PER_DAY":"3",
        "ORB_RANGE_MAX_PCT":"0.020"}),
]


def run_corpus(tid, overrides, corpus_dir, label):
    out = Path(f"/tmp/research_r3/{tid}_{label}")
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **BASE, **overrides}
    cmd = ["python3","tools/orb_backtest.py",
           "--corpus", corpus_dir, "--out", str(out),
           "--year-prefix","202","--tickers", UNIV]
    try:
        subprocess.run(cmd, env=env, check=True, capture_output=True, timeout=300)
    except subprocess.CalledProcessError:
        return None
    s = json.load(open(out/"summary.json"))
    pos = neg = 0; pnls=[]
    for jf in (out/"per_day").iterdir():
        d = json.load(open(jf))
        p = sum(x.get("pnl_dollars",0) for x in d.get("pnl_pairs",[]))
        pnls.append(p)
        if p > 0.01: pos += 1
        elif p < -0.01: neg += 1
    return {"net": s["net_pnl"], "wr": s["win_rate_pct"], "entries": s["entries"],
            "days_pos": pos, "days_neg": neg,
            "days_pct": pos/(pos+neg)*100 if (pos+neg) else 0,
            "best": max(pnls) if pnls else 0,
            "worst": min(pnls) if pnls else 0}


def evaluate(tid, overrides):
    targets = [("fy", "/tmp/rth-data/data"),
               ("q2_2025", "/tmp/cv_q2_2025"),
               ("q3_2025", "/tmp/cv_q3_2025"),
               ("q4_2025", "/tmp/cv_q4_2025"),
               ("q1q2_2026", "/tmp/cv_q1q2_2026")]
    results = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(run_corpus, tid, overrides, c, l): l for l, c in targets}
        for f in as_completed(futs):
            try: results[futs[f]] = f.result()
            except Exception: results[futs[f]] = None
    return results


Path("/tmp/research_r3").mkdir(exist_ok=True)
print(f"R3: {len(THEORIES)} theories x 5 corpus splits", flush=True)
rows = []
t_start = time.time()
for i, (tid, overrides) in enumerate(THEORIES, 1):
    res = evaluate(tid, overrides)
    fy = res.get("fy")
    if fy is None:
        print(f"[{i}/{len(THEORIES)}] {tid}: FAILED", flush=True)
        continue
    neg_q = sum(1 for q in ["q2_2025","q3_2025","q4_2025","q1q2_2026"]
                if res.get(q) and res[q]["net"] < 0)
    row = {"id": tid, "fy_net": fy["net"], "fy_wr": fy["wr"],
           "fy_days_pct": fy["days_pct"], "fy_worst": fy["worst"],
           "fy_best": fy["best"], "fy_entries": fy["entries"],
           "q2_2025_net": res.get("q2_2025",{}).get("net", 0) if res.get("q2_2025") else 0,
           "q3_2025_net": res.get("q3_2025",{}).get("net", 0) if res.get("q3_2025") else 0,
           "q4_2025_net": res.get("q4_2025",{}).get("net", 0) if res.get("q4_2025") else 0,
           "q1q2_2026_net": res.get("q1q2_2026",{}).get("net", 0) if res.get("q1q2_2026") else 0,
           "neg_q": neg_q}
    rows.append(row)
    total = time.time() - t_start
    print(f"[{i}/{len(THEORIES)}] {tid}: FY=${fy['net']:+,.0f} WR={fy['wr']:.0f}% "
          f"neg_q={neg_q}/4 q2/3/4/1+2: ${row['q2_2025_net']:+,.0f}/"
          f"${row['q3_2025_net']:+,.0f}/${row['q4_2025_net']:+,.0f}/"
          f"${row['q1q2_2026_net']:+,.0f} [{total:.0f}s]", flush=True)

rows.sort(key=lambda r: r["fy_net"], reverse=True)
print()
print("="*100)
print("R3 RANKED BY FULL-YEAR NET P&L")
print("="*100)
for i, r in enumerate(rows[:12], 1):
    print(f"{i:<3} {r['id']:<32} FY=${r['fy_net']:>+8,.0f} "
          f"WR={r['fy_wr']:.0f}% %+={r['fy_days_pct']:.0f}% "
          f"neg_q={r['neg_q']}/4 "
          f"Q2-25=${r['q2_2025_net']:+,.0f} "
          f"Q3-25=${r['q3_2025_net']:+,.0f} "
          f"Q4-25=${r['q4_2025_net']:+,.0f} "
          f"Q12-26=${r['q1q2_2026_net']:+,.0f}")

print()
print("="*100)
print("R3 RANKED BY STABILITY (fewest neg_q, then highest fy_net)")
print("="*100)
rows.sort(key=lambda r: (r["neg_q"], -r["fy_net"]))
for i, r in enumerate(rows[:8], 1):
    print(f"{i} {r['id']:<32} neg_q={r['neg_q']}/4 FY=${r['fy_net']:+,.0f} "
          f"worst-day=${r['fy_worst']:+,.0f} best-day=${r['fy_best']:+,.0f}")

with open("/tmp/research_r3/all.json","w") as f:
    json.dump(rows, f, indent=2)
