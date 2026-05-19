"""Round 24: dial partial-profit trigger below 1R (2026-05-18). FALSIFIED.

Result (FY Val + Main combined replay, 14 variants):
  - Coupled (partial + BE both move below 1R): all variants neutral
    to -$2,757/yr vs baseline. Lower partial booking costs more on
    trades that DO reach 1R than it captures on trades that don't.
  - Decoupled (partial below 1R, BE stays at 1R): same conclusion.
    Runner staying un-BE'd doesn't compensate.
  - Stacked with R21 14:00 fallback: still no improvement.

Conclusion: 1R is the right partial trigger. The $43k/yr give-back
on un-partialed trades is captured by R26 (`stale_full_exit`) at the
position level, not by dialing the partial threshold.

Status: NOT shipped. R26 (v9.1.130) ships as the answer to the
un-partialed give-back problem instead.

NOTE: references ORBConfig fields (partial_at_r_multiple,
be_arm_at_r_multiple) that are NOT in main. Re-running requires
re-adding those fields to tools/orb_backtest.py first.

Original hypothesis
===================

Val MFE forensic found 219/393 morning trades (56%) peaked below 1R
and never fired partial, leaving ~$43k/yr of unrealized profit
unprotected. Smoke test of partial-at-0.7R (BE also at 0.7R) on Val
morning showed -$2,757/yr -- the cost of lower partial booking on
trades that DO hit 1R outweighs the capture on trades that don't.

This sweep tests:
  1. Coupled: partial AND BE move together to X*R for X in {0.5,...,0.95}
  2. Decoupled: partial moves to X*R, BE stays at 1.0R
     (lets runners stay un-BE'd longer so they can reach target)
  3. Top candidates run through combined_replay (Val + Main).
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
    "ORB_OR_MINUTES": "30", "ORB_RR": "2.5", "ORB_RISK_PER_TRADE_PCT": "1.0",
    "ORB_RANGE_MIN_PCT": "0.008", "ORB_RANGE_MAX_PCT": "0.025",
    "ORB_MAX_TRADES_PER_DAY": "5", "ORB_MAX_CONCURRENT_RISK_DOLLARS": "2000",
    "ORB_MAX_CONCURRENT_NOTIONAL_MULT": "1.9", "ORB_DAILY_LOSS_KILL_PCT": "2.0",
    "ORB_ATR_STOP_MULT": "1.75", "ORB_ATR_LOOKBACK_5M": "14",
    "ORB_PARTIAL_PROFIT_AT_1R": "1", "ORB_MOVE_TO_BE_AFTER_1R": "1",
    "ORB_STOP_BUFFER_BPS": "5.0", "ORB_ENTRY_SLIPPAGE_BPS": "1.5",
    "ORB_EXIT_SLIPPAGE_BPS": "1.5", "ORB_STOP_KICK_BPS": "5.0",
    "ORB_SHORT_PENALTY_BPS": "1.0", "ORB_MAX_TRADE_NOTIONAL_PCT": "75",
    "ORB_SKIP_GAP_ABOVE_PCT": "1.5", "ORB_SKIP_VIX_ABOVE": "25.0",
    "ORB_SKIP_PRIOR_SPY_RET_LT_BPS": "-40.0", "ORB_SKIP_EARNINGS_WINDOW": "1",
    "ORB_TIME_CUTOFF_ET": "11:00", "ORB_EOD_CUTOFF_ET": "15:55",
    "ORB_COMPOUND_DAILY": "1", "ORB_TICKER_SIDE_BLOCKLIST": "{}",
    "ORB_MAX_VWAP_DEV_BPS": "15.0",
    "ORB_MAX_VWAP_DEV_TICKERS": "META,MSFT,AAPL,AMZN,GOOG,AVGO",
    "ORB_POST_TRADE_COOLDOWN_MIN": "10",
}

EOD_BASE = {
    "AFT_STRATEGY": "eod_reversal",
    "AFT_EOD_UNIVERSE": "ORCL,AAPL,MSFT,AVGO,NFLX,TSLA",
    "AFT_EOD_LONG_TICKERS": "ORCL,AAPL,MSFT,AVGO,TSLA",
    "AFT_EOD_SHORT_TICKERS": "ORCL,NFLX,AAPL,MSFT,TSLA",
    "AFT_EOD_TOP_N": "1", "AFT_NOTIONAL_PCT": "35",
    "AFT_SIZING_MODE": "fixed_notional",
    "AFT_ENTRY_BUCKET": "900", "AFT_EXIT_BUCKET": "958",
    "AFT_ENTRY_SLIP_BPS": "1.5", "AFT_EXIT_SLIP_BPS": "1.5",
    "AFT_COMPOUND_DAILY": "1",
}

THEORIES = [
    ("R24_baseline_1R", {}),
    # Coupled: partial + BE both at the lower level.
    ("R24_coupled_095R", {"ORB_PARTIAL_AT_R_MULTIPLE": "0.95"}),
    ("R24_coupled_090R", {"ORB_PARTIAL_AT_R_MULTIPLE": "0.90"}),
    ("R24_coupled_085R", {"ORB_PARTIAL_AT_R_MULTIPLE": "0.85"}),
    ("R24_coupled_080R", {"ORB_PARTIAL_AT_R_MULTIPLE": "0.80"}),
    ("R24_coupled_070R", {"ORB_PARTIAL_AT_R_MULTIPLE": "0.70"}),
    ("R24_coupled_050R", {"ORB_PARTIAL_AT_R_MULTIPLE": "0.50"}),
    # Decoupled: partial fires earlier, BE stays at 1R (runner is
    # un-BE'd longer; may reach target more often).
    ("R24_decoupled_partial_095_be_1R", {
        "ORB_PARTIAL_AT_R_MULTIPLE": "0.95", "ORB_BE_ARM_AT_R_MULTIPLE": "1.0",
    }),
    ("R24_decoupled_partial_090_be_1R", {
        "ORB_PARTIAL_AT_R_MULTIPLE": "0.90", "ORB_BE_ARM_AT_R_MULTIPLE": "1.0",
    }),
    ("R24_decoupled_partial_085_be_1R", {
        "ORB_PARTIAL_AT_R_MULTIPLE": "0.85", "ORB_BE_ARM_AT_R_MULTIPLE": "1.0",
    }),
    ("R24_decoupled_partial_080_be_1R", {
        "ORB_PARTIAL_AT_R_MULTIPLE": "0.80", "ORB_BE_ARM_AT_R_MULTIPLE": "1.0",
    }),
    ("R24_decoupled_partial_070_be_1R", {
        "ORB_PARTIAL_AT_R_MULTIPLE": "0.70", "ORB_BE_ARM_AT_R_MULTIPLE": "1.0",
    }),
    ("R24_decoupled_partial_050_be_1R", {
        "ORB_PARTIAL_AT_R_MULTIPLE": "0.50", "ORB_BE_ARM_AT_R_MULTIPLE": "1.0",
    }),
    # Stack winners on top of R21 time-14:00 fallback (composition test).
    ("R24_coupled_085_eod_1400", {
        "ORB_PARTIAL_AT_R_MULTIPLE": "0.85", "ORB_RUNNER_EOD_PREP_ET": "14:00",
    }),
    ("R24_decoupled_085_be_1R_eod_1400", {
        "ORB_PARTIAL_AT_R_MULTIPLE": "0.85", "ORB_BE_ARM_AT_R_MULTIPLE": "1.0",
        "ORB_RUNNER_EOD_PREP_ET": "14:00",
    }),
]


def _run_morning(tid, overrides, account):
    out = REPO / "results" / "r24" / f"{tid}_acct{account}" / "morning"
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
         "eod_reversal", "--corpus", CORPUS, "--out", str(out), "--year-prefix", "20"],
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
    print(f"R24: {len(THEORIES)} variants x 2 accounts (Val + Main)", flush=True)
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
                f"  {row['id']:<40} "
                f"VAL: ${v.get('combined_pnl', 0):>+8,.0f} ({v.get('annualized_pct', 0):>+5.1f}%) "
                f"morn=${v.get('morning_pnl', 0):>+7,.0f} eod=${v.get('eod_pnl', 0):>+6,.0f} "
                f"| MAIN: ${m.get('combined_pnl', 0):>+8,.0f}", flush=True,
            )
            rows.append(row)

    elapsed = time.time() - t0
    rows.sort(key=lambda r: -(r["val"].get("combined_pnl") or 0))
    print(f"\nDone in {elapsed:.0f}s\n" + "=" * 130)
    print("R24 PARTIAL-AT-X-R RANKING (sorted by Val combined annualized $)")
    print("=" * 130)
    print(f"{'#':>2} {'variant':<42} {'VAL combined':>14} {'VAL ann %':>10} {'morn':>9} {'eod':>8} {'MAIN combined':>15} {'MAIN ann %':>11}")
    print("-" * 130)
    for i, r in enumerate(rows, 1):
        v, m = r["val"], r["main"]
        print(f"{i:>2} {r['id']:<42} "
              f"${v.get('combined_pnl', 0):>+12,.0f} {v.get('annualized_pct', 0):>+8.1f}% "
              f"${v.get('morning_pnl', 0):>+7,.0f} ${v.get('eod_pnl', 0):>+6,.0f} "
              f"${m.get('combined_pnl', 0):>+12,.0f} {m.get('annualized_pct', 0):>+8.1f}%")
    out = REPO / "results" / "r24" / "all.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2))
    print(f"\nFull JSON: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
