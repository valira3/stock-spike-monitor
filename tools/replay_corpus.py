"""v8.3.27 -- live-engine corpus sweep.

Drives orb.live_runtime end-to-end across a date range and applies
"what-if" risk rule variants as POST-PROCESSING on the resulting
admit/exit ledger. This is the missing complement to
tools/orb_backtest.py (the classical ORB engine) -- the live engine
has fundamentally different re-entry behavior (multi-fire same-side
on signal flips) that the classical backtest doesn't capture, so
Rule #1 (per-(ticker, side) lock after losing leg) shows zero value
in orb_backtest but ~$665 value on the live engine's behavior
(measured against the actual production trade log on 2026-05-12).

This tool answers "if we run today's live engine + these defensive
rules across the full year, what's the PnL?"

Architecture:
  1. For each date, call orb_replay_day.replay() to get the event
     ledger (admit / exit / reject / summary).
  2. Pair admits with their subsequent exits into closed legs with
     entry_bucket, exit_bucket, entry_price, exit_price, pnl.
  3. For each rule variant, walk legs chronologically per day:
       - Rule #1: lock (ticker, side) after a leg with pnl below
         -loss_threshold. Future entries on the locked pair within
         the same day are dropped.
       - Rule #2: track running peak realized PnL; once intraday
         realized drops dd_threshold below peak, halt all future
         entries for the day.
     Sum surviving legs' pnl per day, then per corpus.

Rule application is purely subtractive -- it never INTRODUCES new
trades, only BLOCKS existing ones. This is sound because both Rule
#1 and Rule #2 are admission-time gates; their effect is purely to
short-circuit `live_runtime.check_entry` for a given (portfolio,
ticker, side) under the same conditions. The live engine in
production with these rules wired in would produce the same closed-
leg subset.

Usage:
    python -m tools.replay_corpus \\
        --start-date 2025-05-12 --end-date 2026-05-12 \\
        --tickers AAPL,MSFT,NVDA,TSLA,META,GOOG,AMZN,AVGO,NFLX,ORCL,SPY,QQQ \\
        --base-dir /tmp/rth-data/data \\
        --out-dir /tmp/replay_corpus_run

Output:
    summary.json -- per-variant aggregate stats
    legs/<YYYY-MM-DD>.json -- per-day pair list (baseline only)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Leg:
    """One closed round-trip from the replay event stream."""
    date: str
    ticker: str
    side: str  # "long" | "short"
    shares: int
    entry_price: float
    exit_price: float
    entry_bucket: int
    exit_bucket: int
    exit_reason: str
    pnl: float

    def asdict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class RuleConfig:
    """Subtractive risk rule applied post-replay.

    loss_lock_threshold_usd > 0  ==> Rule #1 active: after a closed
        leg with pnl < -threshold, lock that (ticker, side) for the
        rest of the day.

    peak_dd_halt_usd > 0  ==> Rule #2 active: when intraday realized
        PnL drops this many $ below the day's running peak, halt
        future entries for the day.
    """
    name: str
    loss_lock_threshold_usd: float = 0.0
    peak_dd_halt_usd: float = 0.0


def pair_legs(date_iso: str, events: list[dict]) -> list[Leg]:
    """Walk one day's events and pair admits with subsequent exits.

    `events` is the raw output of `orb_replay_day.replay()` after the
    write_ledger flattening (each event is `{kind, ...fields}`).
    """
    open_admits: dict[str, dict] = {}
    legs: list[Leg] = []
    for ev in events:
        kind = ev.get("kind")
        if kind == "admit" and "ticker" in ev:
            open_admits[ev["ticker"]] = ev
        elif kind == "exit" and "ticker" in ev:
            tk = ev["ticker"]
            admit = open_admits.pop(tk, None)
            if not admit:
                continue
            entry_price = float(admit.get("price", 0) or 0)
            exit_price = float(ev.get("price", 0) or 0)
            shares = int(admit.get("shares", 0) or 0)
            side = str(admit.get("side", "long")).lower()
            sign = 1 if side == "long" else -1
            pnl = (exit_price - entry_price) * shares * sign
            legs.append(Leg(
                date=date_iso,
                ticker=str(tk),
                side=side,
                shares=shares,
                entry_price=entry_price,
                exit_price=exit_price,
                entry_bucket=int(admit.get("bucket", 0) or 0),
                exit_bucket=int(ev.get("bucket", 0) or 0),
                exit_reason=str(ev.get("reason", "")),
                pnl=pnl,
            ))
    return legs


def simulate(legs_by_day: dict[str, list[Leg]],
             rule: RuleConfig) -> dict[str, Any]:
    """Apply `rule` to baseline legs subtractively."""
    total_pnl = 0.0
    kept = skipped_r1 = skipped_r2 = 0
    per_day: dict[str, dict[str, Any]] = {}
    for d in sorted(legs_by_day):
        day_legs = sorted(
            legs_by_day[d],
            key=lambda L: (L.entry_bucket, L.exit_bucket),
        )
        locked: dict[tuple[str, str], int] = {}  # (ticker,side) -> lock_bucket
        peak = cum = 0.0
        halted_at: int | None = None
        day_kept = 0
        for leg in day_legs:
            key = (leg.ticker, leg.side)
            # Rule #1 -- skip if locked before this leg's entry.
            if (rule.loss_lock_threshold_usd > 0
                    and key in locked
                    and locked[key] < leg.entry_bucket):
                skipped_r1 += 1
                continue
            # Rule #2 -- skip if halted before this leg's entry.
            if (rule.peak_dd_halt_usd > 0
                    and halted_at is not None
                    and halted_at < leg.entry_bucket):
                skipped_r2 += 1
                continue
            # Accept this leg.
            total_pnl += leg.pnl
            cum += leg.pnl
            kept += 1
            day_kept += 1
            if cum > peak:
                peak = cum
            if (rule.peak_dd_halt_usd > 0
                    and halted_at is None
                    and cum <= peak - rule.peak_dd_halt_usd):
                halted_at = leg.exit_bucket
            # Update Rule #1 lock based on this leg's outcome.
            if (rule.loss_lock_threshold_usd > 0
                    and leg.pnl < -rule.loss_lock_threshold_usd):
                locked[key] = leg.exit_bucket
        per_day[d] = {
            "pnl": cum, "kept": day_kept,
            "halted": halted_at is not None,
        }
    return {
        "name": rule.name,
        "total_pnl": total_pnl,
        "kept_count": kept,
        "skipped_r1_count": skipped_r1,
        "skipped_r2_count": skipped_r2,
        "per_day": per_day,
    }


def default_variants() -> list[RuleConfig]:
    return [
        RuleConfig(name="baseline"),
        RuleConfig(name="lock_25", loss_lock_threshold_usd=25.0),
        RuleConfig(name="lock_50", loss_lock_threshold_usd=50.0),
        RuleConfig(name="lock_100", loss_lock_threshold_usd=100.0),
        RuleConfig(name="lock_150", loss_lock_threshold_usd=150.0),
        RuleConfig(name="dd_500", peak_dd_halt_usd=500.0),
        RuleConfig(name="dd_1000", peak_dd_halt_usd=1000.0),
        RuleConfig(name="combo_25_500",
                   loss_lock_threshold_usd=25.0, peak_dd_halt_usd=500.0),
        RuleConfig(name="combo_100_500",
                   loss_lock_threshold_usd=100.0, peak_dd_halt_usd=500.0),
        RuleConfig(name="combo_100_1000",
                   loss_lock_threshold_usd=100.0, peak_dd_halt_usd=1000.0),
    ]


def _trading_days(start: str, end: str) -> list[str]:
    s = datetime.strptime(start, "%Y-%m-%d").date()
    e = datetime.strptime(end, "%Y-%m-%d").date()
    out = []
    d = s
    while d <= e:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def _replay_one(date_iso: str, tickers: list[str], base_dir: str,
                portfolio_id: str = "main",
                equity: float = 100_000.0) -> list[dict]:
    """Programmatic single-day replay. Returns flattened event list."""
    from tools.orb_replay_day import ReplayConfig, replay
    cfg = ReplayConfig(
        date_iso=date_iso,
        tickers=tickers,
        base_dir=base_dir,
        portfolio_id=portfolio_id,
        equity=equity,
    )
    events = replay(cfg)
    # orb_replay_day's _Event has .kind and .data; flatten to dict for
    # consistency with the JSONL write_ledger() format.
    out = []
    for ev in events:
        if hasattr(ev, "payload") and hasattr(ev, "kind"):
            d = {"kind": ev.kind, **ev.payload}
            out.append(d)
        elif isinstance(ev, dict):
            out.append(ev)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--tickers", required=True,
                        help="Comma-separated ticker list")
    parser.add_argument("--base-dir", default="data",
                        help="Bar archive root (base_dir/<DATE>/<TICKER>.jsonl)")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--equity", type=float, default=100_000.0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # VIX gate forces day-blocked unless fail_closed is OFF. Backtest
    # parity flag for replays.
    os.environ.setdefault("ORB_FAIL_CLOSED_VIX", "0")

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    dates = _trading_days(args.start_date, args.end_date)
    out_dir = Path(args.out_dir) if args.out_dir else Path(
        f"replays/run-{datetime.utcnow():%Y%m%dT%H%M%SZ}"
    )

    print(f"[replay-corpus] {args.start_date} -> {args.end_date} "
          f"({len(dates)} trading days), {len(tickers)} tickers",
          flush=True)

    legs_by_day: dict[str, list[Leg]] = {}
    days_with_no_data = 0
    days_failed = 0
    t_start = time.time()
    for i, d in enumerate(dates):
        if not (Path(args.base_dir) / d).exists():
            days_with_no_data += 1
            continue
        try:
            events = _replay_one(date_iso=d, tickers=tickers,
                                 base_dir=args.base_dir,
                                 equity=args.equity)
            legs = pair_legs(d, events)
            legs_by_day[d] = legs
        except Exception as e:
            print(f"  {d}: FAIL ({type(e).__name__}: {e})", flush=True)
            days_failed += 1
            continue
    elapsed = time.time() - t_start

    print(f"[replay-corpus] replayed {len(legs_by_day)} days "
          f"({days_with_no_data} no-data, {days_failed} failed) "
          f"in {elapsed:.1f}s",
          flush=True)

    variants_results = [simulate(legs_by_day, v)
                        for v in default_variants()]

    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema_version": 1,
        "meta": {
            "start_date": args.start_date,
            "end_date": args.end_date,
            "tickers": tickers,
            "base_dir": args.base_dir,
            "equity": args.equity,
            "captured_at_utc": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "elapsed_seconds": round(elapsed, 1),
            "days_replayed": len(legs_by_day),
            "days_no_data": days_with_no_data,
            "days_failed": days_failed,
        },
        "variants": variants_results,
        "legs_count": sum(len(L) for L in legs_by_day.values()),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True))

    # Per-day legs (baseline only) for spot-checking.
    legs_dir = out_dir / "legs"
    legs_dir.mkdir(exist_ok=True)
    for d, legs in legs_by_day.items():
        (legs_dir / f"{d}.json").write_text(
            json.dumps([L.asdict() for L in legs], indent=2,
                       sort_keys=True))

    print()
    print(f"{'VARIANT':<22} {'PnL':>12} {'Δ baseline':>14} {'kept':>7} "
          f"{'sk-R1':>7} {'sk-R2':>7}")
    print("-" * 75)
    baseline_pnl = variants_results[0]["total_pnl"]
    for r in variants_results:
        delta = r["total_pnl"] - baseline_pnl
        print(f"{r['name']:<22} ${r['total_pnl']:>+11.2f} "
              f"${delta:>+13.2f} {r['kept_count']:>7} "
              f"{r['skipped_r1_count']:>7} {r['skipped_r2_count']:>7}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
