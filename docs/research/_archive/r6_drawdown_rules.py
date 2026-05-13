"""Round 6: day-end-giveback defenses.

Test the v18 rules added in v8.3.26:
  - ORB_LOSS_LOCK_THRESHOLD_USD (Rule #1, per-(ticker, side) lock
    after losing leg).
  - ORB_PEAK_DD_HALT_USD (Rule #2, peak-drawdown halt).

Motivation (operator request 2026-05-12): on the v8.3.x production
config Main gave back 97% of peak realized PnL by EOD ($1,080 ->
$35). The two rules above are surgical, env-only defenses against
the re-entry-after-stop pattern that drove the giveback.

Layered on top of v12's winning config (Config A: risk=1.0% +
v10-keystone baseline). r2-r5 already falsified the alternatives
(NFLX block, cut11:00, VIX<=20, etc.) so this round isolates
exactly the new lever.

Run pattern (foreground, mirrors r2/r3/r4/r5):
  python3 docs/research/r6_drawdown_rules.py
  -> /tmp/research_r6/<tid>_<label>/summary.json + per_day/*.json

GHA-driven equivalent (preferred, see CLAUDE.md "GHA-driven
backtest via lever-sweep"):
  Actions tab -> Lever Sweep -> Run workflow -> paste the JSON
  variants tuple printed by `python3 -m docs.research.r6_drawdown_rules --print-variants`
"""
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

UNIV = "AAPL,MSFT,NVDA,TSLA,META,GOOG,AMZN,AVGO,NFLX,ORCL,SPY,QQQ"

# v12-winning Config A baseline (risk_per_trade_pct=1.0 on the v10
# keystone). Everything else matches the production v10 config.
BASE = {
    "ORB_COMPOUND_DAILY": "1",
    "ORB_STOP_BUFFER_BPS": "5",
    "ORB_OR_MINUTES": "30",
    "ORB_RR": "2.5",
    "ORB_RANGE_MIN_PCT": "0.008",
    "ORB_RANGE_MAX_PCT": "0.025",
    "ORB_MAX_TRADES_PER_DAY": "5",
    "ORB_RISK_PER_TRADE_PCT": "1.00",          # Config A winner
    "ORB_MAX_CONCURRENT_RISK_DOLLARS": "2000",
    "ORB_DAILY_LOSS_KILL_PCT": "2.0",
    # v8.3.20 production change: notional cap 2.0 -> 0.95.
    "ORB_MAX_CONCURRENT_NOTIONAL_MULT": "0.95",
    "ORB_MAX_TRADE_NOTIONAL_PCT": "75",
    "ORB_MOVE_TO_BE_AFTER_1R": "1",
    "ORB_PARTIAL_PROFIT_AT_1R": "1",           # v8.1.3 production default
    "ORB_TICKER_SIDE_BLOCKLIST": "",           # no static block
    "ORB_SKIP_GAP_ABOVE_PCT": "1.5",
    "ORB_SKIP_EARNINGS_WINDOW": "1",
    "ORB_EARNINGS_DAYS_BEFORE": "1",
    "ORB_SKIP_VIX_ABOVE": "22",
    "ORB_TIME_CUTOFF_ET": "15:55",
    "ORB_EOD_CUTOFF_ET": "15:55",
    "ORB_ACCOUNT": "100000",
    "ORB_ENTRY_SLIPPAGE_BPS": "5.0",           # v8 realism
    "ORB_EXIT_SLIPPAGE_BPS": "5.0",
    "ORB_STOP_KICK_BPS": "5.0",
    "ORB_SHORT_PENALTY_BPS": "1.0",
}


THEORIES: list[tuple[str, dict[str, str]]] = [
    # Control: v12 Config A + v8.3.20 cap, no v18 rules.
    ("R6_baseline_v12A", {}),
    # Rule #1 sweep at thresholds from aggressive ($0 = any loss locks)
    # to conservative ($100 = only meaningful losses lock).
    ("R6_lock_0",   {"ORB_LOSS_LOCK_THRESHOLD_USD": "0.01"}),  # ~any loss
    ("R6_lock_25",  {"ORB_LOSS_LOCK_THRESHOLD_USD": "25"}),
    ("R6_lock_50",  {"ORB_LOSS_LOCK_THRESHOLD_USD": "50"}),
    ("R6_lock_100", {"ORB_LOSS_LOCK_THRESHOLD_USD": "100"}),
    ("R6_lock_150", {"ORB_LOSS_LOCK_THRESHOLD_USD": "150"}),
    # Rule #2 sweep.
    ("R6_dd_300",  {"ORB_PEAK_DD_HALT_USD": "300"}),
    ("R6_dd_500",  {"ORB_PEAK_DD_HALT_USD": "500"}),
    ("R6_dd_750",  {"ORB_PEAK_DD_HALT_USD": "750"}),
    ("R6_dd_1000", {"ORB_PEAK_DD_HALT_USD": "1000"}),
    # Combined (Rule #1 winners x Rule #2 winners).
    ("R6_combo_lock25_dd500",  {"ORB_LOSS_LOCK_THRESHOLD_USD": "25",
                                "ORB_PEAK_DD_HALT_USD": "500"}),
    ("R6_combo_lock50_dd500",  {"ORB_LOSS_LOCK_THRESHOLD_USD": "50",
                                "ORB_PEAK_DD_HALT_USD": "500"}),
    ("R6_combo_lock100_dd500", {"ORB_LOSS_LOCK_THRESHOLD_USD": "100",
                                "ORB_PEAK_DD_HALT_USD": "500"}),
    ("R6_combo_lock100_dd750", {"ORB_LOSS_LOCK_THRESHOLD_USD": "100",
                                "ORB_PEAK_DD_HALT_USD": "750"}),
]


def run_corpus(tid: str, overrides: dict[str, str],
               corpus_dir: str, label: str) -> dict | None:
    """Run orb_backtest.py against `corpus_dir` with BASE + overrides.
    Returns a summary dict, or None on failure."""
    out = Path(f"/tmp/research_r6/{tid}_{label}")
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **BASE, **overrides}
    cmd = [
        "python3", "tools/orb_backtest.py",
        "--corpus", corpus_dir, "--out", str(out),
        "--year-prefix", "202",
        "--tickers", UNIV,
    ]
    try:
        subprocess.run(cmd, env=env, check=True,
                       capture_output=True, timeout=600)
    except subprocess.CalledProcessError as e:
        print(f"  {tid}_{label}: FAIL ({e.returncode})")
        return None
    except subprocess.TimeoutExpired:
        print(f"  {tid}_{label}: TIMEOUT")
        return None

    summary_path = out / "summary.json"
    if not summary_path.exists():
        return None
    s = json.loads(summary_path.read_text())
    pos = neg = 0
    pnls: list[float] = []
    r18_lock_total = 0
    kill_fired_days = 0
    per_day_dir = out / "per_day"
    if per_day_dir.exists():
        for jf in per_day_dir.iterdir():
            d = json.loads(jf.read_text())
            p = sum(x.get("pnl_dollars", 0) for x in d.get("pnl_pairs", []))
            pnls.append(p)
            if p > 0.01:
                pos += 1
            elif p < -0.01:
                neg += 1
            r18_lock_total += int(d.get("r18_lock_rejects", 0))
            if d.get("kill_switch_fired"):
                kill_fired_days += 1
    return {
        "net": s.get("net_pnl"),
        "wr": s.get("win_rate_pct"),
        "entries": s.get("entries"),
        "days_pos": pos, "days_neg": neg,
        "days_pct": (pos / (pos + neg) * 100) if (pos + neg) else 0.0,
        "best": max(pnls) if pnls else 0,
        "worst": min(pnls) if pnls else 0,
        # v18-specific diagnostics
        "r18_lock_rejects_total": r18_lock_total,
        "kill_fired_days": kill_fired_days,
    }


def evaluate(tid: str, overrides: dict[str, str]) -> dict:
    """Evaluate THEORY (tid, overrides) on the full-year corpus AND
    each quarterly slice. Quarterly cross-validation is how r3-r5
    distinguished in-sample wins from real edges."""
    targets = [
        ("fy",          "/tmp/rth-data/data"),
        ("q2_2025",     "/tmp/cv_q2_2025"),
        ("q3_2025",     "/tmp/cv_q3_2025"),
        ("q4_2025",     "/tmp/cv_q4_2025"),
        ("q1q2_2026",   "/tmp/cv_q1q2_2026"),
    ]
    results: dict[str, dict | None] = {}
    for label, corpus_dir in targets:
        if not Path(corpus_dir).exists():
            results[label] = None
            continue
        results[label] = run_corpus(tid, overrides, corpus_dir, label)
    return results


def print_variants_for_lever_sweep() -> None:
    """Emit the JSON tuple for the Lever Sweep workflow's
    `variants` input. Run with --print-variants and paste into the
    GHA dispatch dialog."""
    variants = []
    for vid, overrides in THEORIES:
        env = {**BASE, **overrides}
        variants.append({"vid": vid, "env": env, "stride": "1"})
    print(json.dumps(variants, indent=2))


def main() -> int:
    import sys
    if "--print-variants" in sys.argv:
        print_variants_for_lever_sweep()
        return 0

    print(f"R6 -- {len(THEORIES)} theories, "
          f"5 corpus slices each, parallel by theory")
    print()
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(evaluate, tid, ov): tid
                   for tid, ov in THEORIES}
        results: dict[str, dict] = {}
        for fut in as_completed(futures):
            tid = futures[fut]
            results[tid] = fut.result()
            r = results[tid]
            fy = r.get("fy") or {}
            print(f"  {tid:<28} FY net=${fy.get('net','--'):>+8} "
                  f"days={fy.get('days_pct','--'):>5.1f}%pos "
                  f"locks={fy.get('r18_lock_rejects_total','-'):>4} "
                  f"kills={fy.get('kill_fired_days','-'):>2}d")

    out_path = Path("docs/research/r6_results.json")
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print()
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
