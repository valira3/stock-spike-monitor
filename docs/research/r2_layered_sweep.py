"""Round 2: layer the winning lever (T5 block top losers) with other
filters. Find the BEST combination."""
import json, os, subprocess, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

UNIV = "AAPL,MSFT,NVDA,TSLA,META,GOOG,AMZN,AVGO,NFLX,ORCL,SPY,QQQ"

# T5 baseline: META/MSFT/AAPL/AMZN/GOOG/AVGO blocked
T5_BLOCK = '{"META":["LONG","SHORT"],"MSFT":["LONG","SHORT"],"AAPL":["LONG","SHORT"],"AMZN":["LONG","SHORT"],"GOOG":["LONG","SHORT"],"AVGO":["LONG","SHORT"]}'

ANCHOR = {
    "ORB_COMPOUND_DAILY":"1","ORB_STOP_BUFFER_BPS":"5",
    "ORB_OR_MINUTES":"30","ORB_RR":"2.5",
    "ORB_RANGE_MIN_PCT":"0.008","ORB_RANGE_MAX_PCT":"0.025",
    "ORB_MAX_TRADES_PER_DAY":"5","ORB_RISK_PER_TRADE_PCT":"2.00",
    "ORB_MAX_CONCURRENT_RISK_DOLLARS":"2000","ORB_DAILY_LOSS_KILL_PCT":"2.0",
    "ORB_MAX_TRADE_NOTIONAL_PCT":"75","ORB_MOVE_TO_BE_AFTER_1R":"1",
    "ORB_TICKER_SIDE_BLOCKLIST": T5_BLOCK,
    "ORB_SKIP_GAP_ABOVE_PCT":"1.5","ORB_SKIP_EARNINGS_WINDOW":"1",
    "ORB_EARNINGS_DAYS_BEFORE":"1","ORB_SKIP_VIX_ABOVE":"22",
    "ORB_TIME_CUTOFF_ET":"15:55","ORB_EOD_CUTOFF_ET":"15:55",
    "ORB_ACCOUNT":"100000",
    "ORB_ENTRY_SLIPPAGE_BPS":"1.5","ORB_EXIT_SLIPPAGE_BPS":"1.5",
    "ORB_STOP_KICK_BPS":"5.0","ORB_SHORT_PENALTY_BPS":"1.0",
}

THEORIES = [
    ("R2_T5_only", {}),
    ("R2_T5_vix20", {"ORB_SKIP_VIX_ABOVE":"20"}),
    ("R2_T5_vix18", {"ORB_SKIP_VIX_ABOVE":"18"}),
    ("R2_T5_cut1100", {"ORB_TIME_CUTOFF_ET":"11:00"}),
    ("R2_T5_cut1200", {"ORB_TIME_CUTOFF_ET":"12:00"}),
    ("R2_T5_cut1100_vix20", {
        "ORB_TIME_CUTOFF_ET":"11:00","ORB_SKIP_VIX_ABOVE":"20"}),
    ("R2_T5_rr20", {"ORB_RR":"2.0"}),
    ("R2_T5_rr30", {"ORB_RR":"3.0"}),
    ("R2_T5_or45", {"ORB_OR_MINUTES":"45"}),
    ("R2_T5_or15", {"ORB_OR_MINUTES":"15"}),
    ("R2_T5_or45_cut1100", {
        "ORB_OR_MINUTES":"45","ORB_TIME_CUTOFF_ET":"11:00"}),
    ("R2_T5_no_be", {"ORB_MOVE_TO_BE_AFTER_1R":"0"}),
    ("R2_T5_be_no_meta_msft", {  # T5 minus META/MSFT block (only 4 blocked)
        "ORB_TICKER_SIDE_BLOCKLIST":
            '{"AAPL":["LONG","SHORT"],"AMZN":["LONG","SHORT"],"GOOG":["LONG","SHORT"],"AVGO":["LONG","SHORT"]}'}),
    ("R2_T5_plus_TSLA", {  # block TSLA too
        "ORB_TICKER_SIDE_BLOCKLIST":
            '{"META":["LONG","SHORT"],"MSFT":["LONG","SHORT"],"AAPL":["LONG","SHORT"],"AMZN":["LONG","SHORT"],"GOOG":["LONG","SHORT"],"AVGO":["LONG","SHORT"],"TSLA":["LONG","SHORT"]}'}),
    ("R2_T5_plus_NFLX", {  # NFLX shorts were bad
        "ORB_TICKER_SIDE_BLOCKLIST":
            '{"META":["LONG","SHORT"],"MSFT":["LONG","SHORT"],"AAPL":["LONG","SHORT"],"AMZN":["LONG","SHORT"],"GOOG":["LONG","SHORT"],"AVGO":["LONG","SHORT"],"NFLX":["SHORT"]}'}),
    ("R2_T5_drop_AAPL_only", {  # MINIMAL block: just AAPL added to existing
        "ORB_TICKER_SIDE_BLOCKLIST":
            '{"META":["LONG","SHORT"],"MSFT":["LONG","SHORT"],"AAPL":["LONG","SHORT"]}'}),
    ("R2_T5_drop_AAPL_AMZN", {
        "ORB_TICKER_SIDE_BLOCKLIST":
            '{"META":["LONG","SHORT"],"MSFT":["LONG","SHORT"],"AAPL":["LONG","SHORT"],"AMZN":["LONG","SHORT"]}'}),
    ("R2_T5_drop_3", {  # block AAPL, AMZN, GOOG only (not AVGO)
        "ORB_TICKER_SIDE_BLOCKLIST":
            '{"META":["LONG","SHORT"],"MSFT":["LONG","SHORT"],"AAPL":["LONG","SHORT"],"AMZN":["LONG","SHORT"],"GOOG":["LONG","SHORT"]}'}),
]


def run_corpus(theory_id, overrides, corpus_dir, label):
    out = Path(f"/tmp/research_r2/{theory_id}_{label}")
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **ANCHOR, **overrides}
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


def evaluate(theory_id, overrides):
    targets = [("fy", "/tmp/rth-data/data"),
               ("q2_2025", "/tmp/cv_q2_2025"),
               ("q3_2025", "/tmp/cv_q3_2025"),
               ("q4_2025", "/tmp/cv_q4_2025"),
               ("q1q2_2026", "/tmp/cv_q1q2_2026")]
    results = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(run_corpus, theory_id, overrides, c, l): l for l, c in targets}
        for f in as_completed(futs):
            try: results[futs[f]] = f.result()
            except Exception: results[futs[f]] = None
    return results


Path("/tmp/research_r2").mkdir(exist_ok=True)
print(f"R2: {len(THEORIES)} layered theories", flush=True)
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
           "fy_entries": fy["entries"],
           "q2_2025_net": res.get("q2_2025",{}).get("net", 0),
           "q3_2025_net": res.get("q3_2025",{}).get("net", 0),
           "q4_2025_net": res.get("q4_2025",{}).get("net", 0),
           "q1q2_2026_net": res.get("q1q2_2026",{}).get("net", 0),
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
print("R2 RANKED BY FULL-YEAR NET P&L")
print("="*100)
for i, r in enumerate(rows[:10], 1):
    print(f"{i:<3} {r['id']:<26} FY=${r['fy_net']:>+8,.0f} "
          f"WR={r['fy_wr']:.0f}% %+={r['fy_days_pct']:.0f}% "
          f"neg_q={r['neg_q']}/4 "
          f"Q2-25=${r['q2_2025_net']:+,.0f} "
          f"Q3-25=${r['q3_2025_net']:+,.0f} "
          f"Q4-25=${r['q4_2025_net']:+,.0f} "
          f"Q12-26=${r['q1q2_2026_net']:+,.0f}")

print()
print("="*100)
print("R2 RANKED BY STABILITY (fewest neg_q, then highest fy_net)")
print("="*100)
rows.sort(key=lambda r: (r["neg_q"], -r["fy_net"]))
for i, r in enumerate(rows[:5], 1):
    print(f"{i} {r['id']:<26} neg_q={r['neg_q']}/4 FY=${r['fy_net']:+,.0f} "
          f"worst-day=${r['fy_worst']:+,.0f}")

with open("/tmp/research_r2/all.json","w") as f:
    json.dump(rows, f, indent=2)
