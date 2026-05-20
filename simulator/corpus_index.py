"""simulator.corpus_index -- classify each corpus day into categories.

Scans `data/YYYY-MM-DD/SPY.jsonl` (or any pivot ticker) and tags days
that match each of the following categories:

    gap_up_1_5pct        SPY open is >=1.5% above prior-day close
    gap_down_1_5pct      SPY open is <=-1.5% below prior-day close
    vix_high             VIX close (data/external/vix-daily.csv) > 25
    range_compression    SPY 09:30-10:00 range < 0.4%
    range_expansion      SPY 09:30-10:00 range > 1.6%
    eod_winner           AAPL 15:30-15:58 close > 15:30 open by > 0.5%
    eod_loser            AAPL 15:30-15:58 close < 15:30 open by > 0.5%
    halt_present         a one-minute bar has zero volume mid-RTH
    flash_crash          5-min drawdown > 2% in RTH
    regime_shift         SPY EMA9-cross at 5m within RTH

Output schema (written to simulator/corpus/day_index.json):

    [
      {
        "date": "2025-04-22",
        "categories": ["gap_up_1_5pct", "range_compression"],
        "spy_open": 538.10,
        "spy_pdc": 528.30,
        "spy_gap_pct": 1.85,
        "spy_or_range_pct": 0.32,
        "vix_close_d1": null
      },
      ...
    ]

Used by simulator.batch + simulator.anomaly to drive a curated
"interesting days" portfolio: pick N days from each category,
expect specific behavior per category, flag anomalies on mismatch.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")
_GAP_THRESHOLD = 1.5
_RANGE_COMPRESSION_PCT = 0.4
_RANGE_EXPANSION_PCT = 1.6
_EOD_MOVE_PCT = 0.5
_VIX_HIGH = 25.0
_FLASH_CRASH_PCT = 2.0


def classify_day(date: str, corpus_root: str = "data",
                 vix_csv: str = "data/external/vix-daily.csv") -> Optional[dict]:
    """Compute the category set for one day. Returns None if the
    pivot data (SPY bars) is missing."""
    spy = _load_bars(date, "SPY", corpus_root)
    if not spy:
        return None

    # First and last bars define open / EOD close.
    first = spy[0]
    or_window_end = _bucket_min(9, 60)  # 10:00 ET = bucket 600
    or_window = [b for b in spy if _bar_bucket(b) <= or_window_end]
    if not or_window:
        return None
    or_high = max(float(b["high"]) for b in or_window)
    or_low = min(float(b["low"]) for b in or_window)
    or_mid = (or_high + or_low) / 2.0 or 1.0
    or_range_pct = ((or_high - or_low) / or_mid) * 100.0

    spy_open = float(first["open"])
    spy_pdc = _previous_close(date, "SPY", corpus_root) or spy_open
    spy_gap_pct = ((spy_open - spy_pdc) / spy_pdc) * 100.0 if spy_pdc else 0.0

    categories: List[str] = []
    if spy_gap_pct >= _GAP_THRESHOLD:
        categories.append("gap_up_1_5pct")
    elif spy_gap_pct <= -_GAP_THRESHOLD:
        categories.append("gap_down_1_5pct")

    if or_range_pct < _RANGE_COMPRESSION_PCT:
        categories.append("range_compression")
    elif or_range_pct > _RANGE_EXPANSION_PCT:
        categories.append("range_expansion")

    # VIX (optional)
    vix_close_d1 = _vix_close(date, vix_csv)
    if vix_close_d1 is not None and vix_close_d1 > _VIX_HIGH:
        categories.append("vix_high")

    # Halts: any RTH bar with zero volume (9:30..15:55).
    halt = False
    for b in spy:
        if _is_rth(b) and float(b.get("total_volume") or b.get("iex_volume") or 0) == 0:
            halt = True
            break
    if halt:
        categories.append("halt_present")

    # Flash crash: max 5-bar drawdown > 2%.
    closes = [float(b["close"]) for b in spy if _is_rth(b)]
    if len(closes) > 5:
        peak = closes[0]
        max_dd = 0.0
        for c in closes:
            if c > peak:
                peak = c
            dd = (peak - c) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
        if max_dd > _FLASH_CRASH_PCT:
            categories.append("flash_crash")

    # EOD move on AAPL (proxy for the EOD-reversal strategy regime).
    aapl = _load_bars(date, "AAPL", corpus_root)
    if aapl:
        eod_start = _bucket_min(15, 30)
        eod_end = _bucket_min(15, 58)
        eod_window = [b for b in aapl
                      if eod_start <= _bar_bucket(b) <= eod_end]
        if len(eod_window) >= 2:
            a_open = float(eod_window[0]["open"])
            a_close = float(eod_window[-1]["close"])
            move_pct = ((a_close - a_open) / a_open) * 100.0 if a_open else 0.0
            if move_pct > _EOD_MOVE_PCT:
                categories.append("eod_winner")
            elif move_pct < -_EOD_MOVE_PCT:
                categories.append("eod_loser")

    if not categories:
        categories.append("baseline")

    return {
        "date": date,
        "categories": categories,
        "spy_open": round(spy_open, 2),
        "spy_pdc": round(spy_pdc, 2) if spy_pdc else None,
        "spy_gap_pct": round(spy_gap_pct, 3),
        "spy_or_range_pct": round(or_range_pct, 3),
        "vix_close_d1": vix_close_d1,
    }


def build_index(corpus_root: str = "data",
                vix_csv: str = "data/external/vix-daily.csv",
                out_path: str = "simulator/corpus/day_index.json"
                ) -> List[dict]:
    """Scan every YYYY-MM-DD directory under corpus_root and classify
    each. Writes the JSON index. Returns the in-memory list."""
    if not os.path.isdir(corpus_root):
        raise SystemExit(f"corpus root not found: {corpus_root}")
    dates = sorted(d for d in os.listdir(corpus_root)
                   if len(d) == 10 and d[4] == "-" and d[7] == "-"
                   and os.path.isdir(os.path.join(corpus_root, d)))
    out: List[dict] = []
    for d in dates:
        row = classify_day(d, corpus_root=corpus_root, vix_csv=vix_csv)
        if row is not None:
            out.append(row)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(out, fh, indent=2)
    return out


def load_index(path: str = "simulator/corpus/day_index.json") -> List[dict]:
    if not os.path.isfile(path):
        return []
    with open(path) as fh:
        return json.load(fh)


def pick_representative(
    index: List[dict],
    per_category: int = 3,
    categories: Optional[List[str]] = None,
) -> List[str]:
    """Return up to `per_category` dates from each category.

    Useful for the anomaly batch: ~30 days covering a wide spread of
    market regimes without running the full year.
    """
    seen: Dict[str, List[str]] = {}
    for row in index:
        for cat in row.get("categories", []):
            if categories and cat not in categories:
                continue
            seen.setdefault(cat, []).append(row["date"])

    out: List[str] = []
    for cat, dates in seen.items():
        # Sample evenly across the year by striding the sorted list.
        dates = sorted(set(dates))
        if len(dates) <= per_category:
            out.extend(dates)
        else:
            step = max(1, len(dates) // per_category)
            out.extend(dates[::step][:per_category])
    return sorted(set(out))


# ----- helpers ----------------------------------------------------------


def _load_bars(date: str, ticker: str, root: str) -> List[dict]:
    path = os.path.join(root, date, f"{ticker}.jsonl")
    if not os.path.isfile(path):
        return []
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _bar_bucket(bar: dict) -> int:
    """ET minutes-since-midnight from the bar timestamp."""
    raw = bar.get("timestamp_utc") or bar.get("timestamp") or ""
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return -1
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    et = dt.astimezone(_NY)
    return et.hour * 60 + et.minute


def _bucket_min(hh: int, mm: int) -> int:
    return hh * 60 + mm


def _is_rth(bar: dict) -> bool:
    b = _bar_bucket(bar)
    return _bucket_min(9, 30) <= b <= _bucket_min(15, 55)


def _previous_close(date: str, ticker: str, root: str) -> Optional[float]:
    """Look at preceding corpus day's last bar."""
    if not os.path.isdir(root):
        return None
    days = sorted(d for d in os.listdir(root)
                  if len(d) == 10 and d[4] == "-" and d[7] == "-")
    if date not in days:
        return None
    idx = days.index(date)
    for prior_date in reversed(days[:idx]):
        bars = _load_bars(prior_date, ticker, root)
        if bars:
            return float(bars[-1].get("close", 0) or 0)
    return None


def _vix_close(date: str, csv_path: str) -> Optional[float]:
    """Look up VIX close from the daily CSV (date column = closing day)."""
    if not os.path.isfile(csv_path):
        return None
    try:
        with open(csv_path) as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                if row.get("date") == date or row.get("Date") == date:
                    return float(row.get("close") or row.get("Close") or 0) or None
    except Exception:
        pass
    return None


# ----- CLI --------------------------------------------------------------


def _main(argv=None):
    p = argparse.ArgumentParser(description="Build the simulator corpus index")
    p.add_argument("--corpus-root", default="data")
    p.add_argument("--vix-csv", default="data/external/vix-daily.csv")
    p.add_argument("--out", default="simulator/corpus/day_index.json")
    p.add_argument("--summary", action="store_true",
                   help="Print per-category counts after building")
    args = p.parse_args(argv)

    rows = build_index(args.corpus_root, args.vix_csv, args.out)
    print(f"[corpus_index] indexed {len(rows)} days -> {args.out}")

    if args.summary:
        counts: Dict[str, int] = {}
        for r in rows:
            for c in r.get("categories", []):
                counts[c] = counts.get(c, 0) + 1
        print("\nCategory distribution:")
        for cat, n in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"  {cat:24s}  {n:>4d} days")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
