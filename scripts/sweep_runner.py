"""Run a list of orb_backtest.py configs sequentially against the Keystone
base config + a per-variant override block. Emits a comparison table
with each variant's morning P&L, entry count, win rate, and delta vs
baseline.

Usage:
  python3 scripts/sweep_runner.py vwap     # runs the VWAP fence sweep
  python3 scripts/sweep_runner.py atr      # runs the ATR_STOP_MULT sweep
  python3 scripts/sweep_runner.py cooldown # runs the cooldown matrix
  python3 scripts/sweep_runner.py rr       # runs the RR sweep
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Keystone-canonical env block (matches the current production engine).
BASE_ENV = {
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
    "ORB_MIN_BREAK_BPS": "5.0",
    "ORB_MAX_CONCURRENT_PER_TICKER_SIDE": "1",
}
TICKERS = "AAPL,AMZN,AVGO,GOOG,META,MSFT,NFLX,NVDA,ORCL,QQQ,SPY,TSLA"
CORPUS = "data_pm_universe_dedup"


def run_one(name: str, overrides: dict[str, str], out_root: str) -> dict:
    out_dir = Path(out_root) / name
    if out_dir.exists():
        shutil.rmtree(out_dir)
    env = os.environ.copy()
    env.update(BASE_ENV)
    env.update(overrides)
    t0 = time.time()
    res = subprocess.run(
        [
            "/opt/homebrew/bin/python3.12",
            "tools/orb_backtest.py",
            "--corpus", CORPUS,
            "--out", str(out_dir),
            "--year-prefix", "20",
            "--tickers", TICKERS,
        ],
        env=env, capture_output=True, text=True, cwd=str(ROOT),
    )
    dt = time.time() - t0
    if res.returncode != 0:
        print(f"  FAILED: {res.stderr[-500:]}")
        return {"name": name, "error": res.stderr[-200:]}
    s = json.load(open(out_dir / "summary.json"))
    return {
        "name": name,
        "wall_s": dt,
        "days": s["days_ran"],
        "entries": s["entries"],
        "wins": s["wins"],
        "losses": s["losses"],
        "net_pnl": s["net_pnl"],
        "ann": s["net_pnl"] * 252 / s["days_ran"],
        "vix_skip": s.get("vix_days_skipped"),
        "gap_drop": s.get("gap_signals_dropped"),
    }


def sweep_vwap():
    """Sweep ORB_MAX_VWAP_DEV_BPS in [OFF, 5, 10, 15, 20, 25, 30] applied
    to all 12 tickers (vs default keystone which applies 15 to only 6
    mega-caps). Also explore per-ticker subsets."""
    variants = [
        # name, overrides dict
        ("baseline_kstone", {}),
        ("vwap_off", {"ORB_MAX_VWAP_DEV_BPS": "0", "ORB_MAX_VWAP_DEV_TICKERS": ""}),
        ("vwap_5_all", {"ORB_MAX_VWAP_DEV_BPS": "5.0",
                        "ORB_MAX_VWAP_DEV_TICKERS": TICKERS}),
        ("vwap_10_all", {"ORB_MAX_VWAP_DEV_BPS": "10.0",
                         "ORB_MAX_VWAP_DEV_TICKERS": TICKERS}),
        ("vwap_15_all", {"ORB_MAX_VWAP_DEV_BPS": "15.0",
                         "ORB_MAX_VWAP_DEV_TICKERS": TICKERS}),
        ("vwap_20_all", {"ORB_MAX_VWAP_DEV_BPS": "20.0",
                         "ORB_MAX_VWAP_DEV_TICKERS": TICKERS}),
        ("vwap_25_all", {"ORB_MAX_VWAP_DEV_BPS": "25.0",
                         "ORB_MAX_VWAP_DEV_TICKERS": TICKERS}),
        # Keystone fence + add losers (META is already in fence; add AVGO/GOOG are too).
        # The losers in fixed-sim attribution were META/AVGO/GOOG -- all already in fence.
        # So tighten fence on those 3 only.
        ("vwap_5_losers", {"ORB_MAX_VWAP_DEV_BPS": "5.0",
                           "ORB_MAX_VWAP_DEV_TICKERS": "META,AVGO,GOOG"}),
        # Keystone winners (TSLA/NVDA/NFLX/ORCL) added to the fence at 20bps.
        ("vwap_kstone_plus20_winners", {
            "ORB_MAX_VWAP_DEV_BPS": "15.0",  # 15 for original 6
            "ORB_MAX_VWAP_DEV_TICKERS": "META,MSFT,AAPL,AMZN,GOOG,AVGO",
            # 20bps fence on TSLA/NVDA/NFLX/ORCL needs ORB_MAX_VWAP_DEV_BPS_<TICKER>
            # which orb_backtest doesn't support per-ticker thresholds directly.
            # Workaround: extend the TICKERS list and set a single threshold of
            # 20 for all. Best we can do without per-ticker overrides.
        }),
        # Loosen fence on the 6 mega-caps to see if we're being too restrictive
        ("vwap_25_6mega", {"ORB_MAX_VWAP_DEV_BPS": "25.0",
                           "ORB_MAX_VWAP_DEV_TICKERS": "META,MSFT,AAPL,AMZN,GOOG,AVGO"}),
        # Tighten the keystone 6-ticker fence to 10bps
        ("vwap_10_6mega", {"ORB_MAX_VWAP_DEV_BPS": "10.0",
                           "ORB_MAX_VWAP_DEV_TICKERS": "META,MSFT,AAPL,AMZN,GOOG,AVGO"}),
    ]
    out_root = "/tmp/sweep_vwap"
    if Path(out_root).exists():
        shutil.rmtree(out_root)
    Path(out_root).mkdir(parents=True)

    print(f"VWAP fence sweep -- {len(variants)} variants")
    print(f"{'variant':32} | {'days':>4} | {'ent':>4} | {'wins':>4} | {'net':>10} | {'/yr':>10} | {'wall':>4}")
    print("-" * 90)
    baseline = None
    for name, overrides in variants:
        r = run_one(name, overrides, out_root)
        if "error" in r:
            print(f"  {name:30}: ERROR -- {r['error']}")
            continue
        if baseline is None:
            baseline = r["ann"]
        delta = r["ann"] - baseline
        print(f"{name:32} | {r['days']:>4} | {r['entries']:>4} | {r['wins']:>4} | "
              f"${r['net_pnl']:>+8.0f} | ${r['ann']:>+8.0f} | {r['wall_s']:.0f}s    "
              f"d=${delta:+.0f}")
    print(f"\\nResults at {out_root}/<variant>/summary.json")


def sweep_atr():
    variants = [
        ("atr_125", {"ORB_ATR_STOP_MULT": "1.25"}),
        ("atr_150", {"ORB_ATR_STOP_MULT": "1.5"}),
        ("baseline_175", {}),
        ("atr_200", {"ORB_ATR_STOP_MULT": "2.0"}),
        ("atr_250", {"ORB_ATR_STOP_MULT": "2.5"}),
    ]
    _sweep_loop("ATR_STOP_MULT", variants, "/tmp/sweep_atr")


def sweep_cooldown():
    variants = []
    for atr in ["7", "14", "21"]:
        for cd in ["5", "10", "15", "20"]:
            n = f"atr{atr}_cd{cd}"
            variants.append((n, {"ORB_ATR_LOOKBACK_5M": atr,
                                 "ORB_POST_TRADE_COOLDOWN_MIN": cd}))
    _sweep_loop("ATR_lookback x cooldown matrix", variants, "/tmp/sweep_atrcd")


def sweep_rr():
    variants = [
        ("rr_1_5", {"ORB_RR": "1.5"}),
        ("rr_2_0", {"ORB_RR": "2.0"}),
        ("baseline_rr_2_5", {}),
        ("rr_3_0", {"ORB_RR": "3.0"}),
    ]
    _sweep_loop("RR sweep", variants, "/tmp/sweep_rr")


def _sweep_loop(title, variants, out_root):
    if Path(out_root).exists():
        shutil.rmtree(out_root)
    Path(out_root).mkdir(parents=True)
    print(f"{title} -- {len(variants)} variants")
    print(f"{'variant':32} | {'days':>4} | {'ent':>4} | {'wins':>4} | {'net':>10} | {'/yr':>10} | {'wall':>4}")
    print("-" * 90)
    baseline = None
    for name, overrides in variants:
        r = run_one(name, overrides, out_root)
        if "error" in r:
            print(f"  {name:30}: ERROR")
            continue
        if baseline is None:
            baseline = r["ann"]
        delta = r["ann"] - baseline
        print(f"{name:32} | {r['days']:>4} | {r['entries']:>4} | {r['wins']:>4} | "
              f"${r['net_pnl']:>+8.0f} | ${r['ann']:>+8.0f} | {r['wall_s']:.0f}s    "
              f"d=${delta:+.0f}")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "vwap"
    {
        "vwap": sweep_vwap,
        "atr": sweep_atr,
        "cooldown": sweep_cooldown,
        "rr": sweep_rr,
    }[which]()
