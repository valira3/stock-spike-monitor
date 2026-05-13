"""Round 5: per-ticker cap=1 test. Q4 forensic showed duplicate
same-ticker stops (NFLX x2 on 10/27, NVDA x2 on 10/10, etc).
ORB_MAX_TRADES_PER_DAY is per-ticker. Setting =1 suppresses dupes.

Foreground run to dodge harness auto-restart bug.
"""
import json, os, subprocess, time, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

UNIV = "AAPL,MSFT,NVDA,TSLA,META,GOOG,AMZN,AVGO,NFLX,ORCL,SPY,QQQ"
T5 = '{"META":["LONG","SHORT"],"MSFT":["LONG","SHORT"],"AAPL":["LONG","SHORT"],"AMZN":["LONG","SHORT"],"GOOG":["LONG","SHORT"],"AVGO":["LONG","SHORT"]}'
T5_NFLX_SHORT = '{"META":["LONG","SHORT"],"MSFT":["LONG","SHORT"],"AAPL":["LONG","SHORT"],"AMZN":["LONG","SHORT"],"GOOG":["LONG","SHORT"],"AVGO":["LONG","SHORT"],"NFLX":["SHORT"]}'

BASE = {
    "ORB_COMPOUND_DAILY":"1","ORB_STOP_BUFFER_BPS":"5",
    "ORB_OR_MINUTES":"30","ORB_RR":"2.5",
    "ORB_RANGE_MIN_PCT":"0.008","ORB_RANGE_MAX_PCT":"0.025",
    "ORB_MAX_TRADES_PER_DAY":"5","ORB_RISK_PER_TRADE_PCT":"2.00",
    "ORB_MAX_CONCURRENT_RISK_DOLLARS":"2000","ORB_DAILY_LOSS_KILL_PCT":"2.0",
    "ORB_MAX_TRADE_NOTIONAL_PCT":"75","ORB_MOVE_TO_BE_AFTER_1R":"1",
    "ORB_TICKER_SIDE_BLOCKLIST": T5,
    "ORB_SKIP_GAP_ABOVE_PCT":"1.5","ORB_SKIP_EARNINGS_WINDOW":"1",
    "ORB_EARNINGS_DAYS_BEFORE":"1","ORB_SKIP_VIX_ABOVE":"20",
    "ORB_TIME_CUTOFF_ET":"11:00","ORB_EOD_CUTOFF_ET":"15:55",
    "ORB_ACCOUNT":"100000",
    "ORB_ENTRY_SLIPPAGE_BPS":"1.5","ORB_EXIT_SLIPPAGE_BPS":"1.5",
    "ORB_STOP_KICK_BPS":"5.0","ORB_SHORT_PENALTY_BPS":"1.0",
}

THEORIES = [
    ("R5_max1",        {"ORB_MAX_TRADES_PER_DAY":"1"}),
    ("R5_max1_nflxS",  {"ORB_MAX_TRADES_PER_DAY":"1",
                        "ORB_TICKER_SIDE_BLOCKLIST": T5_NFLX_SHORT}),
    ("R5_max1_risk1",  {"ORB_MAX_TRADES_PER_DAY":"1",
                        "ORB_RISK_PER_TRADE_PCT":"1.0"}),
    ("R5_max1_nflxS_risk1", {"ORB_MAX_TRADES_PER_DAY":"1",
                             "ORB_TICKER_SIDE_BLOCKLIST": T5_NFLX_SHORT,
                             "ORB_RISK_PER_TRADE_PCT":"1.0"}),
    # also: final re-confirm of R3 winners under identical (current) corpus
    ("R5_recheck_baseline",     {}),
    ("R5_recheck_nflxS",        {"ORB_TICKER_SIDE_BLOCKLIST": T5_NFLX_SHORT}),
    ("R5_recheck_risk1pt0",     {"ORB_RISK_PER_TRADE_PCT":"1.0"}),
    ("R5_recheck_risk1pt5",     {"ORB_RISK_PER_TRADE_PCT":"1.5"}),
]


def run_corpus(tid, overrides, corpus_dir, label):
    out = Path(f"/tmp/research_r5/{tid}_{label}")
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


def main():
    Path("/tmp/research_r5").mkdir(exist_ok=True)
    print(f"R5: {len(THEORIES)} theories x 5 corpus splits", flush=True)
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

    rows.sort(key=lambda r: (r["neg_q"], -r["fy_net"]))
    print()
    print("="*100)
    print("R5 STABILITY RANKING")
    print("="*100)
    for i, r in enumerate(rows, 1):
        print(f"{i} {r['id']:<30} neg_q={r['neg_q']}/4 FY=${r['fy_net']:+,.0f} "
              f"worst=${r['fy_worst']:+,.0f} best=${r['fy_best']:+,.0f} "
              f"WR={r['fy_wr']:.0f}% Q4=${r['q4_2025_net']:+,.0f}")

    with open("/tmp/research_r5/all.json","w") as f:
        json.dump(rows, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
