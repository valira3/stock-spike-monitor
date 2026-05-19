"""Round 20: afternoon-discipline sweep (2026-05-18).

Problem statement
=================

Morning ORB positions that haven't hit stop/target by ~14:30 ET carry
their notional into the 15:00-15:50 EOD reversal entry window. Val's
~$30k live account needs 35%*2 = ~$21k headroom for the EOD long+short
legs. A morning position holding $15-20k notional starves the EOD entry
via the cumulative-notional cap. Result: missed EOD trades, missed
the +$12,620/yr Keystone EOD contribution.

The reframe: the goal isn't max P&L on the morning leg in isolation --
it's max combined morning+EOD daily P&L. An exit that gives up $50 on
the morning leg but unblocks a $300 EOD entry is a +$250 trade.

Three theories tested (all backtest-only -- live wiring deferred until
a winner emerges):

1. **eod_prep_exit**: hard time exit at ET. Blunt but cheap.
2. **mfe_giveback**: exit when (MFE - current) > X bps after start_et.
   This is the literal "local maximum exit" -- the position has peaked
   and given back.
3. **afternoon_trail_pct**: chandelier trail from MFE using a fraction
   of initial_risk as the trail width. Time-gated. Lets the position
   ride further than mfe_giveback but tightens after the start time.

Levers can compose. e.g. mfe_giveback at 25bps from 14:00 + hard
fallback at 14:50.

Anti-patterns to avoid (per docs/pl_optimization_final_report_v12.md):
  - Don't add ORB_REQUIRE_RVOL_ABOVE -- kills +$27k/yr.
  - Don't reduce ORB_MAX_TRADES_PER_DAY below 5.
  - 2025-Q1 is the known weak quarter -- judge on multi-quarter avg.

Mirrors r5_per_ticker_cap.py structure: each theory runs against
full-year + quarterly slices, ranks by (neg_q ASC, fy_net DESC).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ---------------------------------------------------------------------
# Base config -- mirrors current Keystone production (2026-05-18).
# Source: CLAUDE.md "Keystone" section.
# ---------------------------------------------------------------------

UNIV = "AAPL,AMZN,AVGO,GOOG,META,MSFT,NFLX,NVDA,ORCL,QQQ,SPY,TSLA"

BASE = {
    # Keystone winner (v9.1.114).
    "ORB_OR_MINUTES": "30",
    "ORB_RR": "2.5",
    "ORB_RISK_PER_TRADE_PCT": "1.0",
    "ORB_RANGE_MIN_PCT": "0.008",
    "ORB_RANGE_MAX_PCT": "0.025",
    "ORB_MAX_TRADES_PER_DAY": "5",
    "ORB_MAX_CONCURRENT_RISK_DOLLARS": "2000",
    "ORB_DAILY_LOSS_KILL_PCT": "2.0",
    "ORB_ATR_STOP_MULT": "1.75",
    "ORB_ATR_LOOKBACK_5M": "14",
    "ORB_PARTIAL_PROFIT_AT_1R": "1",
    "ORB_MOVE_TO_BE_AFTER_1R": "1",
    "ORB_STOP_BUFFER_BPS": "5.0",
    "ORB_ENTRY_SLIPPAGE_BPS": "1.5",
    "ORB_EXIT_SLIPPAGE_BPS": "1.5",
    "ORB_STOP_KICK_BPS": "5.0",
    "ORB_SHORT_PENALTY_BPS": "1.0",
    "ORB_MAX_TRADE_NOTIONAL_PCT": "75",
    "ORB_SKIP_GAP_ABOVE_PCT": "1.5",
    "ORB_SKIP_VIX_ABOVE": "25.0",
    "ORB_SKIP_PRIOR_SPY_RET_LT_BPS": "-40.0",
    "ORB_SKIP_EARNINGS_WINDOW": "1",
    "ORB_TIME_CUTOFF_ET": "11:00",
    "ORB_EOD_CUTOFF_ET": "15:55",
    "ORB_ACCOUNT": "100000",
    "ORB_COMPOUND_DAILY": "1",
    "ORB_TICKER_SIDE_BLOCKLIST": "{}",
    "ORB_MAX_VWAP_DEV_BPS": "15.0",
    "ORB_MAX_VWAP_DEV_TICKERS": "META,MSFT,AAPL,AMZN,GOOG,AVGO",
    "ORB_POST_TRADE_COOLDOWN_MIN": "10",
}

# ---------------------------------------------------------------------
# Theories -- each adds afternoon-discipline overrides on top of BASE.
# ---------------------------------------------------------------------

THEORIES = [
    # === Hard time-exit (blunt) ===
    ("R20_time_1400", {"ORB_EOD_PREP_EXIT_ET": "14:00"}),
    ("R20_time_1430", {"ORB_EOD_PREP_EXIT_ET": "14:30"}),
    ("R20_time_1450", {"ORB_EOD_PREP_EXIT_ET": "14:50"}),
    # === MFE-giveback (local-max signal) ===
    # Active from 14:00 onward; exit when price gives back X bps from peak.
    (
        "R20_mfe_15bps",
        {
            "ORB_MFE_GIVEBACK_BPS": "15",
            "ORB_MFE_GIVEBACK_START_ET": "14:00",
        },
    ),
    (
        "R20_mfe_25bps",
        {
            "ORB_MFE_GIVEBACK_BPS": "25",
            "ORB_MFE_GIVEBACK_START_ET": "14:00",
        },
    ),
    (
        "R20_mfe_40bps",
        {
            "ORB_MFE_GIVEBACK_BPS": "40",
            "ORB_MFE_GIVEBACK_START_ET": "14:00",
        },
    ),
    (
        "R20_mfe_60bps",
        {
            "ORB_MFE_GIVEBACK_BPS": "60",
            "ORB_MFE_GIVEBACK_START_ET": "14:00",
        },
    ),
    (
        "R20_mfe_100bps",
        {
            "ORB_MFE_GIVEBACK_BPS": "100",
            "ORB_MFE_GIVEBACK_START_ET": "14:00",
        },
    ),
    # Start the giveback later -- give more room mid-afternoon.
    (
        "R20_mfe_30bps_1430",
        {
            "ORB_MFE_GIVEBACK_BPS": "30",
            "ORB_MFE_GIVEBACK_START_ET": "14:30",
        },
    ),
    # === Chandelier trail (gentler, time-stepped) ===
    # afternoon_trail_pct=0.5 -> stop ratchets to MFE - 0.5*initial_risk.
    # That's halfway between MFE and the original entry stop.
    (
        "R20_trail_0p50_1400",
        {
            "ORB_AFTERNOON_TRAIL_PCT": "0.5",
            "ORB_AFTERNOON_TRAIL_START_ET": "14:00",
        },
    ),
    (
        "R20_trail_0p30_1400",
        {
            "ORB_AFTERNOON_TRAIL_PCT": "0.3",
            "ORB_AFTERNOON_TRAIL_START_ET": "14:00",
        },
    ),
    (
        "R20_trail_0p20_1430",
        {
            "ORB_AFTERNOON_TRAIL_PCT": "0.2",
            "ORB_AFTERNOON_TRAIL_START_ET": "14:30",
        },
    ),
    # === Compositions -- belt + suspenders ===
    # The interesting question: do MFE+hard-fallback beat either alone?
    (
        "R20_mfe_25_then_1450",
        {
            "ORB_MFE_GIVEBACK_BPS": "25",
            "ORB_MFE_GIVEBACK_START_ET": "14:00",
            "ORB_EOD_PREP_EXIT_ET": "14:50",
        },
    ),
    (
        "R20_mfe_40_then_1450",
        {
            "ORB_MFE_GIVEBACK_BPS": "40",
            "ORB_MFE_GIVEBACK_START_ET": "14:00",
            "ORB_EOD_PREP_EXIT_ET": "14:50",
        },
    ),
    (
        "R20_trail_0p30_then_1450",
        {
            "ORB_AFTERNOON_TRAIL_PCT": "0.3",
            "ORB_AFTERNOON_TRAIL_START_ET": "14:00",
            "ORB_EOD_PREP_EXIT_ET": "14:50",
        },
    ),
    # === Baseline re-check (sanity) ===
    ("R20_baseline_no_changes", {}),
]


def run_corpus(tid, overrides, corpus_dir, label):
    """Run one theory against one corpus slice. Returns summary dict."""
    out = Path(f"/tmp/research_r20/{tid}_{label}")
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **BASE, **overrides}
    cmd = [
        "python3",
        "tools/orb_backtest.py",
        "--corpus",
        corpus_dir,
        "--out",
        str(out),
        "--year-prefix",
        "202",
        "--tickers",
        UNIV,
    ]
    try:
        subprocess.run(cmd, env=env, check=True, capture_output=True, timeout=300)
    except subprocess.CalledProcessError:
        return None
    s = json.load(open(out / "summary.json"))
    pos = neg = 0
    pnls = []
    # R20-specific: also count exits by reason so we can see how often
    # the new gates actually fired.
    exit_reasons: dict[str, int] = {}
    for jf in (out / "per_day").iterdir():
        d = json.load(open(jf))
        p = sum(x.get("pnl_dollars", 0) for x in d.get("pnl_pairs", []))
        pnls.append(p)
        if p > 0.01:
            pos += 1
        elif p < -0.01:
            neg += 1
        for pair in d.get("pnl_pairs", []):
            r = pair.get("exit_reason", "?")
            exit_reasons[r] = exit_reasons.get(r, 0) + 1
    return {
        "net": s["net_pnl"],
        "wr": s["win_rate_pct"],
        "entries": s["entries"],
        "days_pos": pos,
        "days_neg": neg,
        "days_pct": pos / (pos + neg) * 100 if (pos + neg) else 0,
        "best": max(pnls) if pnls else 0,
        "worst": min(pnls) if pnls else 0,
        "exit_reasons": exit_reasons,
    }


def evaluate(tid, overrides):
    """Run theory in parallel across the full-year + 4 quarterly slices."""
    targets = [
        ("fy", "/tmp/rth-data/data"),
        ("q2_2025", "/tmp/cv_q2_2025"),
        ("q3_2025", "/tmp/cv_q3_2025"),
        ("q4_2025", "/tmp/cv_q4_2025"),
        ("q1q2_2026", "/tmp/cv_q1q2_2026"),
    ]
    results = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(run_corpus, tid, overrides, c, label): label for label, c in targets}
        for f in as_completed(futs):
            try:
                results[futs[f]] = f.result()
            except Exception:
                results[futs[f]] = None
    return results


def main():
    Path("/tmp/research_r20").mkdir(exist_ok=True)
    print(f"R20: {len(THEORIES)} theories x 5 corpus splits", flush=True)
    rows = []
    t_start = time.time()
    for i, (tid, overrides) in enumerate(THEORIES, 1):
        res = evaluate(tid, overrides)
        fy = res.get("fy")
        if fy is None:
            print(f"[{i}/{len(THEORIES)}] {tid}: FAILED", flush=True)
            continue
        neg_q = sum(
            1
            for q in ["q2_2025", "q3_2025", "q4_2025", "q1q2_2026"]
            if res.get(q) and res[q]["net"] < 0
        )
        # Count how many of the new exit reasons fired (forensic).
        n_eod_prep = fy["exit_reasons"].get("eod_prep", 0)
        n_mfe = fy["exit_reasons"].get("mfe_giveback", 0)
        row = {
            "id": tid,
            "fy_net": fy["net"],
            "fy_wr": fy["wr"],
            "fy_days_pct": fy["days_pct"],
            "fy_worst": fy["worst"],
            "fy_best": fy["best"],
            "fy_entries": fy["entries"],
            "n_eod_prep": n_eod_prep,
            "n_mfe": n_mfe,
            "q2_2025_net": res.get("q2_2025", {}).get("net", 0) if res.get("q2_2025") else 0,
            "q3_2025_net": res.get("q3_2025", {}).get("net", 0) if res.get("q3_2025") else 0,
            "q4_2025_net": res.get("q4_2025", {}).get("net", 0) if res.get("q4_2025") else 0,
            "q1q2_2026_net": res.get("q1q2_2026", {}).get("net", 0) if res.get("q1q2_2026") else 0,
            "neg_q": neg_q,
        }
        rows.append(row)
        total = time.time() - t_start
        print(
            f"[{i}/{len(THEORIES)}] {tid}: FY=${fy['net']:+,.0f} WR={fy['wr']:.0f}% "
            f"neg_q={neg_q}/4 prep={n_eod_prep} mfe={n_mfe} "
            f"q2/3/4/1+2: ${row['q2_2025_net']:+,.0f}/"
            f"${row['q3_2025_net']:+,.0f}/${row['q4_2025_net']:+,.0f}/"
            f"${row['q1q2_2026_net']:+,.0f} [{total:.0f}s]",
            flush=True,
        )

    rows.sort(key=lambda r: (r["neg_q"], -r["fy_net"]))
    print()
    print("=" * 100)
    print("R20 STABILITY RANKING (sorted by neg_q ASC, fy_net DESC)")
    print("=" * 100)
    for i, r in enumerate(rows, 1):
        print(
            f"{i:2d} {r['id']:<32} neg_q={r['neg_q']}/4 FY=${r['fy_net']:+,.0f} "
            f"worst=${r['fy_worst']:+,.0f} best=${r['fy_best']:+,.0f} "
            f"WR={r['fy_wr']:.0f}% prep={r['n_eod_prep']} mfe={r['n_mfe']}"
        )

    with open("/tmp/research_r20/all.json", "w") as f:
        json.dump(rows, f, indent=2)

    # Caveat for the operator: this sweep measures ORB-morning P&L only.
    # The TRUE objective is morning+EOD combined. A winner here must
    # ALSO be validated by re-running the EOD reversal backtest
    # (tools/afternoon_backtest.py) on the same days and confirming the
    # combined P&L lifts. The hypothesis is: morning P&L drops slightly
    # but EOD P&L lifts more because more equity is free at 15:00.
    print()
    print(
        "NOTE: this measures morning-only P&L. After picking a top-3, "
        "rerun tools/afternoon_backtest.py against the FREED-equity days "
        "to validate the combined morning+EOD lift."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
