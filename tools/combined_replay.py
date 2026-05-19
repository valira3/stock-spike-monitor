#!/usr/bin/env python3
"""combined_replay.py -- full-RTH morning+EOD backtest with cap interaction.

Replicates the staging methodology established in v9.1.131-137
(tools/synth_snapshots.py:synth_day._add_eod_with_interaction) but
extended to a full-year sweep with daily compounding and annualization.

Per-day flow:
  1. Load morning ORB pairs from <morning_out>/per_day/<DATE>.json
     (produced by tools/orb_backtest.py).
  2. Load EOD reversal pairs from <eod_out>/per_day/<DATE>.json
     (produced by tools/afternoon_backtest.py).
  3. Apply the cap-interaction at 15:00 ET:
     - held_over = sum of (entry_price * shares) for morning positions
       whose entry_min < EOD_ENTRY_MIN < exit_min.
     - cap = gross_notional_mult * equity_today.
     - admit EOD candidates FCFS (sorted by entry_bucket) until
       running_notional + nominal > cap; the rest are blocked.
  4. day_pnl = sum(morning_pnls) + sum(admitted_eod_pnls).
  5. Compound: equity_next = equity_now * (1 + day_pnl / equity_now).

Final summary:
  - net_pnl_combined (sum of day_pnls, ending_equity - starting_equity)
  - net_pnl_morning, net_pnl_eod_admitted, net_pnl_eod_blocked
  - days_with_block, total_blocked_count, lost_pnl_from_blocks
  - annualized_return = (ending / starting) ** (252 / N_days) - 1

The "cap interaction" is what makes morning <-> EOD trade-offs visible.
Independent runs of orb_backtest and afternoon_backtest both assume a
clean $100k account; this harness enforces the shared notional cap so
levers that free afternoon equity (R20) can be measured properly.

Usage:
    python tools/combined_replay.py \\
        --morning results/keystone_baseline/morning \\
        --eod     results/keystone_baseline/eod \\
        --equity 100000 \\
        --gross-cap 1.9 \\
        --out results/keystone_baseline/combined.json

For Val ($30k account):
    --equity 30185 --gross-cap 1.9   # cap = $57,351

NOTE: the morning P&L scales with equity (compound), but this script
uses the morning pairs AS-IS. To get accurate per-equity P&L, rerun
tools/orb_backtest.py with ORB_ACCOUNT=<equity> before this script.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


# ISO timestamp shape used by tools/orb_backtest.py:
# "2026-05-15T10:32:00-05:00" (no fractional seconds; DST-naive ET)
_ISO_HHMM = re.compile(r"T(\d{2}):(\d{2}):")

# Buckets are MINUTES SINCE ET MIDNIGHT.
# EOD reversal entry window opens at 15:00 ET = 900.
EOD_ENTRY_MIN = 900


def _iso_to_minutes(ts: str) -> int | None:
    """Extract minutes-since-midnight from an ET ISO timestamp."""
    m = _ISO_HHMM.search(ts or "")
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _load_morning_day(morning_dir: Path, date: str) -> list[dict]:
    """Load morning ORB pairs for one day. Returns [] if the day was
    filtered (no JSON file)."""
    f = morning_dir / "per_day" / f"{date}.json"
    if not f.exists():
        return []
    d = json.loads(f.read_text())
    pairs = d.get("pnl_pairs") or []
    # Normalize: add entry_min / exit_min derived from entry_ts / exit_ts.
    out = []
    for p in pairs:
        em = _iso_to_minutes(p.get("entry_ts", ""))
        xm = _iso_to_minutes(p.get("exit_ts", ""))
        if em is None or xm is None:
            continue
        out.append(
            {
                "ticker": p["ticker"],
                "side": p["side"],
                "entry_min": em,
                "exit_min": xm,
                "entry_price": float(p["entry_price"]),
                "exit_price": float(p["exit_price"]),
                "shares": int(p["shares"]),
                "pnl": float(p["pnl_dollars"]),
                "exit_reason": p.get("exit_reason", "?"),
                "leg": "morning",
            }
        )
    return out


def _load_eod_day(eod_dir: Path, date: str) -> list[dict]:
    """Load EOD reversal candidates for one day (afternoon_backtest output)."""
    f = eod_dir / "per_day" / f"{date}.json"
    if not f.exists():
        return []
    d = json.loads(f.read_text())
    pairs = d.get("pnl_pairs") or []
    out = []
    for p in pairs:
        out.append(
            {
                "ticker": p["ticker"],
                "side": p["side"],
                "entry_min": int(p["entry_bucket"]),
                "exit_min": int(p["exit_bucket"]),
                "entry_price": float(p["entry_price"]),
                "exit_price": float(p["exit_price"]),
                "shares": int(p["shares"]),
                "pnl": float(p["pnl_dollars"]),
                "exit_reason": p.get("exit_reason", "eod"),
                "leg": "eod",
            }
        )
    return out


def _apply_cap_interaction(
    morning_pairs: list[dict],
    eod_candidates: list[dict],
    equity: float,
    gross_notional_mult: float,
) -> tuple[list[dict], list[dict], dict]:
    """Replicates synth_day._add_eod_with_interaction.

    Returns (admitted_eod, blocked_eod, summary).
    """
    cap = gross_notional_mult * equity
    # Held-over morning notional at 15:00 ET (entry < 900 < exit).
    held_over = 0.0
    for p in morning_pairs:
        if p["entry_min"] < EOD_ENTRY_MIN < p["exit_min"]:
            held_over += p["entry_price"] * p["shares"]
    eod_sorted = sorted(eod_candidates, key=lambda x: x["entry_min"])
    admitted: list[dict] = []
    blocked: list[dict] = []
    running = held_over
    for p in eod_sorted:
        nominal = p["entry_price"] * p["shares"]
        if running + nominal > cap:
            blocked.append(p)
            continue
        admitted.append(p)
        running += nominal
    summary = {
        "cap": round(cap, 2),
        "held_over": round(held_over, 2),
        "eod_admitted": len(admitted),
        "eod_blocked": len(blocked),
        "blocked_pnl_lost": round(sum(p["pnl"] for p in blocked), 2),
    }
    return admitted, blocked, summary


def _list_dates(corpus: Path, year_prefix: str) -> list[str]:
    dates: list[str] = []
    for p in sorted(corpus.iterdir()):
        if p.is_dir() and p.name.startswith(year_prefix):
            dates.append(p.name)
    return dates


def run(
    morning_dir: Path,
    eod_dir: Path,
    corpus: Path,
    equity: float,
    gross_notional_mult: float,
    year_prefix: str = "20",
    cap_on_starting_equity: bool = False,
) -> dict:
    dates = _list_dates(corpus, year_prefix)
    rows = []
    starting_equity = equity
    current_equity = equity
    total_morning_pnl = 0.0
    total_eod_admitted_pnl = 0.0
    total_eod_blocked_pnl = 0.0
    total_blocked = 0
    days_with_block = 0
    days_morning_active = 0
    days_eod_active = 0
    days_both_active = 0

    for d in dates:
        morning = _load_morning_day(morning_dir, d)
        eod = _load_eod_day(eod_dir, d)
        if not morning and not eod:
            continue
        if morning:
            days_morning_active += 1
        if eod:
            days_eod_active += 1
        if morning and eod:
            days_both_active += 1
        # Cap reference: starting (matches staging's 5-day snapshot
        # methodology) or current compounded equity (more realistic for
        # full-year annualization since live broker caps grow with equity).
        cap_ref = starting_equity if cap_on_starting_equity else current_equity
        admitted, blocked, intr = _apply_cap_interaction(
            morning,
            eod,
            cap_ref,
            gross_notional_mult,
        )
        morning_pnl = sum(p["pnl"] for p in morning)
        admitted_pnl = sum(p["pnl"] for p in admitted)
        blocked_pnl = sum(p["pnl"] for p in blocked)
        day_pnl = morning_pnl + admitted_pnl

        total_morning_pnl += morning_pnl
        total_eod_admitted_pnl += admitted_pnl
        total_eod_blocked_pnl += blocked_pnl
        total_blocked += len(blocked)
        if len(blocked) > 0:
            days_with_block += 1

        # Daily compound.
        if current_equity > 0:
            current_equity = current_equity + day_pnl
        rows.append(
            {
                "date": d,
                "morning_pnl": round(morning_pnl, 2),
                "eod_pnl": round(admitted_pnl, 2),
                "eod_blocked_pnl": round(blocked_pnl, 2),
                "blocked_count": len(blocked),
                "day_pnl": round(day_pnl, 2),
                "equity_after": round(current_equity, 2),
                "held_over": intr["held_over"],
                "cap": intr["cap"],
            }
        )

    n = len(rows)
    total_return = (current_equity / starting_equity) - 1.0 if starting_equity else 0.0
    annualized = (current_equity / starting_equity) ** (252.0 / n) - 1.0 if n > 0 else 0.0
    annualized_dollars = starting_equity * annualized

    return {
        "starting_equity": starting_equity,
        "ending_equity": round(current_equity, 2),
        "gross_notional_mult": gross_notional_mult,
        "n_days": n,
        "days_morning_active": days_morning_active,
        "days_eod_active": days_eod_active,
        "days_both_active": days_both_active,
        "days_with_block": days_with_block,
        "total_blocked": total_blocked,
        "net_pnl_combined": round(current_equity - starting_equity, 2),
        "net_pnl_morning": round(total_morning_pnl, 2),
        "net_pnl_eod_admitted": round(total_eod_admitted_pnl, 2),
        "net_pnl_eod_blocked_lost": round(total_eod_blocked_pnl, 2),
        "total_return_pct": round(total_return * 100, 2),
        "annualized_return_pct": round(annualized * 100, 2),
        "annualized_pnl_dollars": round(annualized_dollars, 2),
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--morning", required=True, type=Path, help="orb_backtest output dir (contains per_day/)"
    )
    ap.add_argument(
        "--eod", required=True, type=Path, help="afternoon_backtest output dir (contains per_day/)"
    )
    ap.add_argument(
        "--corpus", required=True, type=Path, help="bar corpus dir (used only to enumerate dates)"
    )
    ap.add_argument(
        "--equity", type=float, default=100_000.0, help="starting equity for compound calc"
    )
    ap.add_argument(
        "--gross-cap",
        type=float,
        default=1.9,
        help="gross-notional cap multiplier (staging default 1.9)",
    )
    ap.add_argument("--year-prefix", default="20", help="filter dates by prefix (default '20')")
    ap.add_argument("--out", type=Path, help="write JSON result here (else stdout summary only)")
    ap.add_argument(
        "--cap-on-starting-equity",
        action="store_true",
        help="cap = mult * STARTING equity (no compounding) -- "
        "matches staging synth_day methodology; default is "
        "to compound the cap with the running equity for "
        "realistic annualization",
    )
    args = ap.parse_args()

    result = run(
        morning_dir=args.morning,
        eod_dir=args.eod,
        corpus=args.corpus,
        equity=args.equity,
        gross_notional_mult=args.gross_cap,
        year_prefix=args.year_prefix,
        cap_on_starting_equity=args.cap_on_starting_equity,
    )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2))

    # Compact stdout summary.
    print()
    print("=" * 78)
    print(f"COMBINED REPLAY  starting_equity=${args.equity:,.0f}  cap_mult={args.gross_cap}x")
    print("=" * 78)
    print(f"  N days replayed:               {result['n_days']}")
    print(f"  Days morning active:           {result['days_morning_active']}")
    print(f"  Days EOD active:               {result['days_eod_active']}")
    print(f"  Days both active:              {result['days_both_active']}")
    print(f"  Days with blocked EOD legs:    {result['days_with_block']}")
    print(f"  Total EOD legs blocked:        {result['total_blocked']}")
    print(f"  Lost P&L from blocked EOD:     ${result['net_pnl_eod_blocked_lost']:+,.2f}")
    print("-" * 78)
    print(f"  Net P&L morning:               ${result['net_pnl_morning']:+,.2f}")
    print(f"  Net P&L EOD admitted:          ${result['net_pnl_eod_admitted']:+,.2f}")
    print(f"  Net P&L combined:              ${result['net_pnl_combined']:+,.2f}")
    print(f"  Total return:                   {result['total_return_pct']:+.2f}%")
    print(f"  Annualized return:              {result['annualized_return_pct']:+.2f}%")
    print(f"  Annualized $:                  ${result['annualized_pnl_dollars']:+,.2f}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
