"""Round 21: partials-ladder + runner-trail sweep (2026-05-18).

Problem statement
=================

The Val morning EOD-exit forensic showed that 38 of 156 EOD-held
positions (24%) gave back >0R of unrealized P&L from the 1R peak to
the 15:55 ET close. Median giveback was 0.60R, max 0.91R. Total
dollars left on the table on Val: ~$3,796/yr.

The existing `partial_profit_at_1r` lever takes half off at exactly
1R and lets the runner ride to stop/target/BE/EOD with no further
profit protection. This sweep adds R21 levers that operate on the
RUNNER half only:

  - partial_at_2r          -- second half-close at 2R
  - partial_at_3r          -- third half-close at 3R (composes with 2R)
  - runner_mfe_trail_bps   -- after partial fires, trail stop = mfe -+ X bps
  - runner_eod_prep_minutes -- after partial fires, force-exit at this ET

Each variant is run through tools/orb_backtest.py (morning) and then
through tools/combined_replay.py (which pipes the per-day output
through the cap-interaction logic + daily compounding from
synth_snapshots staging methodology, annualized over 252 days).

Both Val ($30,185 starting equity) and Main ($100,000) are evaluated.

Anti-patterns avoided:
  - No new entry filters (we already explored R20 and it cost morning P&L).
  - Runner levers fire ONLY after partial-at-1R is taken, so losing trades
    aren't double-cut.
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

# Keystone v9.1.114 lever set, matches scripts/build_replay_week.sh BASE.
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
    "ORB_PARTIAL_PROFIT_AT_1R": "1",  # baseline -- R21 builds on top
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

# Keystone v9.1.114 r17 EOD reversal levers.
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

# R21 variants.
THEORIES = [
    ("R21_baseline_1R_only", {}),
    # Just add a 2R partial (no runner trail).
    ("R21_partial_2R_only", {"ORB_PARTIAL_AT_2R": "1"}),
    # 2R + 3R ladder.
    ("R21_partial_2R_3R", {"ORB_PARTIAL_AT_2R": "1", "ORB_PARTIAL_AT_3R": "1"}),
    # Runner MFE trail at various widths (no extra partials).
    ("R21_runner_trail_15bps", {"ORB_RUNNER_MFE_TRAIL_BPS": "15"}),
    ("R21_runner_trail_25bps", {"ORB_RUNNER_MFE_TRAIL_BPS": "25"}),
    ("R21_runner_trail_40bps", {"ORB_RUNNER_MFE_TRAIL_BPS": "40"}),
    ("R21_runner_trail_60bps", {"ORB_RUNNER_MFE_TRAIL_BPS": "60"}),
    # Runner EOD-prep (time-based runner exit).
    ("R21_runner_eod_1330", {"ORB_RUNNER_EOD_PREP_ET": "13:30"}),
    ("R21_runner_eod_1400", {"ORB_RUNNER_EOD_PREP_ET": "14:00"}),
    ("R21_runner_eod_1430", {"ORB_RUNNER_EOD_PREP_ET": "14:30"}),
    # Combinations.
    ("R21_2R_trail_25", {"ORB_PARTIAL_AT_2R": "1", "ORB_RUNNER_MFE_TRAIL_BPS": "25"}),
    ("R21_2R_trail_40", {"ORB_PARTIAL_AT_2R": "1", "ORB_RUNNER_MFE_TRAIL_BPS": "40"}),
    ("R21_2R_eod_1400", {"ORB_PARTIAL_AT_2R": "1", "ORB_RUNNER_EOD_PREP_ET": "14:00"}),
    (
        "R21_2R_3R_trail_40",
        {
            "ORB_PARTIAL_AT_2R": "1",
            "ORB_PARTIAL_AT_3R": "1",
            "ORB_RUNNER_MFE_TRAIL_BPS": "40",
        },
    ),
]


def _run_morning(tid: str, overrides: dict, account: int) -> Path:
    """Run orb_backtest for one variant + one account size. Returns out dir."""
    out = REPO / "results" / "r21" / f"{tid}_acct{account}" / "morning"
    if (out / "summary.json").exists():
        return out  # cache: skip if already done
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **BASE, **overrides, "ORB_ACCOUNT": str(account)}
    cmd = [
        sys.executable,
        "tools/orb_backtest.py",
        "--corpus",
        CORPUS,
        "--out",
        str(out),
        "--year-prefix",
        "202",
        "--tickers",
        UNIV,
    ]
    try:
        subprocess.run(cmd, env=env, check=True, capture_output=True, timeout=180, cwd=REPO)
    except subprocess.CalledProcessError as e:
        print(f"[ERR] {tid} morning acct={account} failed: {e.stderr[:200].decode()}", flush=True)
        return None
    return out


def _run_eod(account: int) -> Path:
    """Run afternoon_backtest once per account size (no R21 dependence)."""
    out = REPO / "results" / "r21" / f"eod_acct{account}"
    if (out / "summary.json").exists():
        return out
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **EOD_BASE, "AFT_ACCOUNT": str(account)}
    cmd = [
        sys.executable,
        "tools/afternoon_backtest.py",
        "--strategy",
        "eod_reversal",
        "--corpus",
        CORPUS,
        "--out",
        str(out),
        "--year-prefix",
        "20",
    ]
    try:
        subprocess.run(cmd, env=env, check=True, capture_output=True, timeout=180, cwd=REPO)
    except subprocess.CalledProcessError as e:
        print(f"[ERR] EOD acct={account} failed: {e.stderr[:200].decode()}", flush=True)
        return None
    return out


def _combined_replay(
    morning_dir: Path, eod_dir: Path, account: int, gross_cap: float = 1.9
) -> dict:
    """Run combined_replay and return its summary dict."""
    cmd = [
        sys.executable,
        "tools/combined_replay.py",
        "--morning",
        str(morning_dir),
        "--eod",
        str(eod_dir),
        "--corpus",
        CORPUS,
        "--equity",
        str(account),
        "--gross-cap",
        str(gross_cap),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, timeout=60)
    # Parse the stdout summary; combined_replay also writes to --out but we
    # didn't pass --out so we just extract what we need from stdout.
    out = proc.stdout

    def grab(label):
        for line in out.splitlines():
            if label in line:
                # Extract last numeric token, strip $ , %
                toks = [t for t in line.split() if any(c.isdigit() for c in t)]
                if not toks:
                    return None
                v = toks[-1].replace("$", "").replace(",", "").replace("%", "")
                v = v.lstrip("+")
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
        "blocked_lost": grab("Lost P&L from blocked EOD"),
    }


def main():
    print(f"R21: {len(THEORIES)} variants x 2 accounts (Val + Main)", flush=True)
    t0 = time.time()
    rows = []

    # Run EOD baselines once per account.
    eod_main = _run_eod(100_000)
    eod_val = _run_eod(30_185)

    # Run morning + combined-replay per variant in parallel.
    def _variant_row(tid, overrides):
        # Val
        morn_val = _run_morning(tid, overrides, 30_185)
        comb_val = _combined_replay(morn_val, eod_val, 30_185) if morn_val else {}
        # Main
        morn_main = _run_morning(tid, overrides, 100_000)
        comb_main = _combined_replay(morn_main, eod_main, 100_000) if morn_main else {}
        return {"id": tid, "val": comb_val, "main": comb_main}

    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_variant_row, tid, ov): tid for tid, ov in THEORIES}
        for f in as_completed(futs):
            row = f.result()
            rows.append(row)
            v, m = row["val"], row["main"]
            print(
                f"  {row['id']:<28} "
                f"VAL: ${v.get('combined_pnl', 0):>+8,.0f}  "
                f"({v.get('annualized_pct', 0):>+5.1f}%)  "
                f"morn=${v.get('morning_pnl', 0):>+7,.0f} eod=${v.get('eod_pnl', 0):>+6,.0f} "
                f"blkd={int(v.get('blocked_count', 0) or 0):>3} "
                f"| MAIN: ${m.get('combined_pnl', 0):>+8,.0f} "
                f"({m.get('annualized_pct', 0):>+5.1f}%)",
                flush=True,
            )

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.0f}s\n", flush=True)

    # Sort by Val combined annualized P&L.
    rows.sort(key=lambda r: -(r["val"].get("combined_pnl") or 0))
    print("=" * 110)
    print("R21 PARTIALS-LADDER RANKING (sorted by Val combined annualized $)")
    print("=" * 110)
    print(
        f"{'#':>2} {'variant':<28} "
        f"{'VAL combined':>13} {'VAL ann %':>10} "
        f"{'morn':>9} {'eod':>8} {'blkd':>5} "
        f"{'MAIN combined':>14} {'MAIN ann %':>11}"
    )
    print("-" * 110)
    for i, r in enumerate(rows, 1):
        v, m = r["val"], r["main"]
        print(
            f"{i:>2} {r['id']:<28} "
            f"${v.get('combined_pnl', 0):>+11,.0f} "
            f"{v.get('annualized_pct', 0):>+8.1f}% "
            f"${v.get('morning_pnl', 0):>+7,.0f} "
            f"${v.get('eod_pnl', 0):>+6,.0f} "
            f"{int(v.get('blocked_count', 0) or 0):>5} "
            f"${m.get('combined_pnl', 0):>+11,.0f} "
            f"{m.get('annualized_pct', 0):>+8.1f}%"
        )

    out_file = REPO / "results" / "r21" / "all.json"
    out_file.write_text(json.dumps(rows, indent=2))
    print(f"\nFull JSON: {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
