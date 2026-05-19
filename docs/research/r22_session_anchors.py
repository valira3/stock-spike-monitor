"""Round 22: session-relative anchor runner exits (2026-05-18).

Problem statement
=================

R21's runner_eod_prep=14:00 won the FY sweep but lost the volatile
Q4'25 to no-lever (i.e., 14:00 was right on average, but a fixed
time doesn't adapt to regime). Hypothesis: a session-relative anchor
(VWAP, 5m EMA-9, day HOD) captures the trending-vs-choppy distinction
natively, beating 14:00 in BOTH FY and Q4'25.

Three indicator-based levers, all firing only AFTER partial_taken=True:

  runner_vwap_cross_after_et    -- exit when 1m close pierces session VWAP
  runner_ema9_cross_after_et    -- exit when 1m close pierces 5m EMA-9
  runner_hod_trail_bps          -- exit when close X bps below day HOD/LOD

Each lever requires a time-gate (earliest ET minute it activates) to
suppress noise from intraday dips. R21 forensic showed VWAP cross fires
at 12:00 are too aggressive (catastrophic -$11k vs baseline on smoke);
the right gate is empirical -- this sweep finds it.

For each lever variant, the existing 14:00 runner_eod_prep can be
ADDED as a safety-net fallback (belt-and-suspenders), or left off so
each signal is judged on its own.

Anti-patterns avoided:
  - No position-relative trails (R21 sweep proved they underperform).
  - All levers gated on partial_taken so losing trades stay untouched.
  - Time-gates required; no variant exits earlier than ~12:00 ET.

Run locally: `python docs/research/r22_session_anchors.py`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
CORPUS = "/tmp/rth-data/data"

UNIV = "AAPL,AMZN,AVGO,GOOG,META,MSFT,NFLX,NVDA,ORCL,QQQ,SPY,TSLA"

BASE = {
    "ORB_OR_MINUTES": "30",
    "ORB_RR": "2.5",
    "ORB_RISK_PER_TRADE_PCT": "1.0",
    "ORB_RANGE_MIN_PCT": "0.008",
    "ORB_RANGE_MAX_PCT": "0.025",
    "ORB_MAX_TRADES_PER_DAY": "5",
    "ORB_MAX_CONCURRENT_RISK_DOLLARS": "2000",
    "ORB_MAX_CONCURRENT_NOTIONAL_MULT": "1.9",
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
    "ORB_COMPOUND_DAILY": "1",
    "ORB_TICKER_SIDE_BLOCKLIST": "{}",
    "ORB_MAX_VWAP_DEV_BPS": "15.0",
    "ORB_MAX_VWAP_DEV_TICKERS": "META,MSFT,AAPL,AMZN,GOOG,AVGO",
    "ORB_POST_TRADE_COOLDOWN_MIN": "10",
}

EOD_BASE = {
    "AFT_STRATEGY": "eod_reversal",
    "AFT_EOD_UNIVERSE": "ORCL,AAPL,MSFT,AVGO,NFLX,TSLA",
    "AFT_EOD_LONG_TICKERS": "ORCL,AAPL,MSFT,AVGO,TSLA",
    "AFT_EOD_SHORT_TICKERS": "ORCL,NFLX,AAPL,MSFT,TSLA",
    "AFT_EOD_TOP_N": "1",
    "AFT_NOTIONAL_PCT": "35",
    "AFT_SIZING_MODE": "fixed_notional",
    "AFT_ENTRY_BUCKET": "900",
    "AFT_EXIT_BUCKET": "958",
    "AFT_ENTRY_SLIP_BPS": "1.5",
    "AFT_EXIT_SLIP_BPS": "1.5",
    "AFT_COMPOUND_DAILY": "1",
}

# R22 variants. Each grouped by anchor type + time-gate sweep.
THEORIES = [
    # Reference points (already evaluated in R21, included for direct compare).
    ("R22_baseline_no_runner_lever", {}),
    ("R22_R21_winner_eod_1400", {"ORB_RUNNER_EOD_PREP_ET": "14:00"}),
    # VWAP-cross at various time gates.
    ("R22_vwap_cross_1230", {"ORB_RUNNER_VWAP_CROSS_AFTER_ET": "12:30"}),
    ("R22_vwap_cross_1300", {"ORB_RUNNER_VWAP_CROSS_AFTER_ET": "13:00"}),
    ("R22_vwap_cross_1330", {"ORB_RUNNER_VWAP_CROSS_AFTER_ET": "13:30"}),
    ("R22_vwap_cross_1400", {"ORB_RUNNER_VWAP_CROSS_AFTER_ET": "14:00"}),
    ("R22_vwap_cross_1430", {"ORB_RUNNER_VWAP_CROSS_AFTER_ET": "14:30"}),
    # 5m EMA-9 cross.
    ("R22_ema9_cross_1230", {"ORB_RUNNER_EMA9_CROSS_AFTER_ET": "12:30"}),
    ("R22_ema9_cross_1300", {"ORB_RUNNER_EMA9_CROSS_AFTER_ET": "13:00"}),
    ("R22_ema9_cross_1330", {"ORB_RUNNER_EMA9_CROSS_AFTER_ET": "13:30"}),
    ("R22_ema9_cross_1400", {"ORB_RUNNER_EMA9_CROSS_AFTER_ET": "14:00"}),
    # HOD/LOD trail at various widths + time gates.
    ("R22_hod_trail_40_1200", {
        "ORB_RUNNER_HOD_TRAIL_BPS": "40",
        "ORB_RUNNER_HOD_TRAIL_AFTER_ET": "12:00",
    }),
    ("R22_hod_trail_60_1200", {
        "ORB_RUNNER_HOD_TRAIL_BPS": "60",
        "ORB_RUNNER_HOD_TRAIL_AFTER_ET": "12:00",
    }),
    ("R22_hod_trail_60_1300", {
        "ORB_RUNNER_HOD_TRAIL_BPS": "60",
        "ORB_RUNNER_HOD_TRAIL_AFTER_ET": "13:00",
    }),
    ("R22_hod_trail_100_1200", {
        "ORB_RUNNER_HOD_TRAIL_BPS": "100",
        "ORB_RUNNER_HOD_TRAIL_AFTER_ET": "12:00",
    }),
    # Composition: VWAP-cross primary + 14:00 safety net.
    ("R22_vwap_1300_then_eod_1430", {
        "ORB_RUNNER_VWAP_CROSS_AFTER_ET": "13:00",
        "ORB_RUNNER_EOD_PREP_ET": "14:30",
    }),
    ("R22_ema9_1300_then_eod_1430", {
        "ORB_RUNNER_EMA9_CROSS_AFTER_ET": "13:00",
        "ORB_RUNNER_EOD_PREP_ET": "14:30",
    }),
    ("R22_hod_60_1200_then_eod_1430", {
        "ORB_RUNNER_HOD_TRAIL_BPS": "60",
        "ORB_RUNNER_HOD_TRAIL_AFTER_ET": "12:00",
        "ORB_RUNNER_EOD_PREP_ET": "14:30",
    }),
]


def _run_morning(tid, overrides, account):
    out = REPO / "results" / "r22" / f"{tid}_acct{account}" / "morning"
    if (out / "summary.json").exists():
        return out
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **BASE, **overrides, "ORB_ACCOUNT": str(account)}
    try:
        subprocess.run(
            [sys.executable, "tools/orb_backtest.py", "--corpus", CORPUS,
             "--out", str(out), "--year-prefix", "202", "--tickers", UNIV],
            env=env, check=True, capture_output=True, timeout=180, cwd=REPO,
        )
    except subprocess.CalledProcessError as e:
        print(f"[ERR] {tid} acct={account} failed: {e.stderr[:200].decode()}", flush=True)
        return None
    return out


def _run_eod(account):
    out = REPO / "results" / "r22" / f"eod_acct{account}"
    if (out / "summary.json").exists():
        return out
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **EOD_BASE, "AFT_ACCOUNT": str(account)}
    subprocess.run(
        [sys.executable, "tools/afternoon_backtest.py", "--strategy",
         "eod_reversal", "--corpus", CORPUS, "--out", str(out),
         "--year-prefix", "20"],
        env=env, check=True, capture_output=True, timeout=180, cwd=REPO,
    )
    return out


def _replay(morning_dir, eod_dir, account):
    proc = subprocess.run(
        [sys.executable, "tools/combined_replay.py", "--morning", str(morning_dir),
         "--eod", str(eod_dir), "--corpus", CORPUS, "--equity", str(account),
         "--gross-cap", "1.9"],
        capture_output=True, text=True, cwd=REPO, timeout=60,
    )

    def grab(label):
        for line in proc.stdout.splitlines():
            if label in line:
                toks = [t for t in line.split() if any(c.isdigit() for c in t)]
                if toks:
                    v = toks[-1].replace("$", "").replace(",", "").replace("%", "").lstrip("+")
                    try:
                        return float(v)
                    except ValueError:
                        return None
        return None

    return {
        "morning_pnl": grab("Net P&L morning"),
        "eod_pnl": grab("Net P&L EOD admitted"),
        "combined_pnl": grab("Net P&L combined"),
        "annualized_pct": grab("Annualized return"),
        "blocked_count": grab("Total EOD legs blocked"),
    }


def main():
    print(f"R22: {len(THEORIES)} variants x 2 accounts (Val + Main)", flush=True)
    t0 = time.time()

    eod_main = _run_eod(100_000)
    eod_val = _run_eod(30_185)

    def variant_row(tid, overrides):
        mval = _run_morning(tid, overrides, 30_185)
        mmain = _run_morning(tid, overrides, 100_000)
        return {
            "id": tid,
            "val": _replay(mval, eod_val, 30_185) if mval else {},
            "main": _replay(mmain, eod_main, 100_000) if mmain else {},
        }

    rows = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(variant_row, t, o): t for t, o in THEORIES}
        for f in as_completed(futs):
            row = f.result()
            v, m = row["val"], row["main"]
            print(
                f"  {row['id']:<33} "
                f"VAL: ${v.get('combined_pnl', 0):>+8,.0f} ({v.get('annualized_pct', 0):>+5.1f}%) "
                f"morn=${v.get('morning_pnl', 0):>+7,.0f} eod=${v.get('eod_pnl', 0):>+6,.0f} "
                f"blkd={int(v.get('blocked_count', 0) or 0):>3} "
                f"| MAIN: ${m.get('combined_pnl', 0):>+8,.0f} ({m.get('annualized_pct', 0):>+5.1f}%)",
                flush=True,
            )
            rows.append(row)

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.0f}s\n", flush=True)

    rows.sort(key=lambda r: -(r["val"].get("combined_pnl") or 0))
    print("=" * 130)
    print("R22 SESSION-ANCHOR RANKING (sorted by Val combined annualized $)")
    print("=" * 130)
    print(
        f"{'#':>2} {'variant':<33} "
        f"{'VAL combined':>14} {'VAL ann %':>10} {'morn':>9} {'eod':>8} {'blkd':>5} "
        f"{'MAIN combined':>15} {'MAIN ann %':>11}"
    )
    print("-" * 130)
    for i, r in enumerate(rows, 1):
        v, m = r["val"], r["main"]
        print(
            f"{i:>2} {r['id']:<33} "
            f"${v.get('combined_pnl', 0):>+12,.0f} "
            f"{v.get('annualized_pct', 0):>+8.1f}% "
            f"${v.get('morning_pnl', 0):>+7,.0f} "
            f"${v.get('eod_pnl', 0):>+6,.0f} "
            f"{int(v.get('blocked_count', 0) or 0):>5} "
            f"${m.get('combined_pnl', 0):>+12,.0f} "
            f"{m.get('annualized_pct', 0):>+8.1f}%"
        )

    out = REPO / "results" / "r22" / "all.json"
    out.write_text(json.dumps(rows, indent=2))
    print(f"\nFull JSON: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
