"""v8.3.30 -- compare ATR(5m, 14) computed from raw ticks vs from
1-min OHLC bars for a given (date, ticker).

The hypothesis under test (from v8.3.28 commit msg): production's
ATR is computed from streaming ticks (sub-second intra-bar
volatility) while the replay uses 1-min OHLC summaries that smooth
out the true range. If true, the tick-derived ATR should be
meaningfully tighter than the bar-derived ATR, explaining why the
replay's stops are ~1.5x wider than production's.

Method:
  1. Load N trading days of tick data from R2 (gzipped JSONL).
  2. Aggregate ticks into 5-min OHLC bars (true intra-minute
     high/low captured from every print).
  3. Compute ATR(5m, 14) at the 10:00 ET reference point (end of
     OR window, when entries fire) using `engine.alarm_f_trail.atr_from_bars`.
  4. Load the same date's 1-min bars from the data-extensions
     archive, aggregate to 5-min via the standard bucket boundary,
     compute ATR(5m, 14) the same way.
  5. Emit comparison row per (date, ticker) -- ratio, abs diff,
     verdict.

Output: JSON object suitable for committing to a results branch.

Usage:
    # In GHA runner where R2 creds + 1m bar archive are mounted:
    python -m tools.compare_atr \\
        --date 2026-05-12 \\
        --tickers AAPL,AMZN,...  \\
        --tick-r2-prefix tick-data \\
        --bar-archive ./bars/data \\
        --out summary.json

Environment (for R2):
    R2_ACCOUNT_ID, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ATR_PERIOD = 14

# v8.3.33 -- trade-condition filter so tick aggregation matches the
# SIP-consolidated 1-min bar high/low semantics.
#
# The first real validation (v8.3.32 chain) found tick ATR was 6-9x
# the 1m bar ATR on liquid mega-caps (SPY 9.06x, AAPL 6.00x, MSFT
# 4.66x). The cause is that raw trade ticks include conditions that
# the consolidated tape EXCLUDES from last-sale / high / low
# calculation (dark-pool prints, odd-lot trades, late prints, etc.).
# Filtering to tape-eligible conditions only should bring the tick
# aggregation in line with the 1m bar source.
#
# Sources: NYSE Trade Conditions, Nasdaq Plan SIP rules, CTA Plan.
# This is the canonical "exclude from consolidated last sale" list:
NON_LAST_SALE_ELIGIBLE_CONDITIONS = frozenset([
    "B",  # Average Price Trade
    "C",  # Cash Sale
    "G",  # Bunched Sold
    "H",  # Price Variation Trade
    "I",  # Odd Lot Trade -- the big one for ATR inflation on liquid names
    "K",  # Rule 127 Trade (NYSE)
    "L",  # Sold Last
    "M",  # Market Center Close Price
    "N",  # Next Day Trade
    "P",  # Prior Reference Price
    "Q",  # Market Center Official Open
    "R",  # Seller
    "T",  # Form T (extended-hours trade)
    "U",  # Extended Hours Sold
    "V",  # Contingent Trade
    "W",  # Average Price Trade
    "Z",  # Sold (Out of Sequence) -- late print
    "4",  # Derivatively Priced
    "5",  # Re-Opening Prints (sometimes)
    "6",  # Market Center Closing Price
    "7",  # Qualified Contingent Trade
    "9",  # Cross/Cross Trade
])


def is_tape_eligible(conditions) -> bool:
    """Return True if this trade contributes to the consolidated
    tape's last-sale / high / low.

    Empty conditions = regular trade = include.
    Any blacklisted condition present = exclude.
    """
    if not conditions:
        return True
    for c in conditions:
        if c in NON_LAST_SALE_ELIGIBLE_CONDITIONS:
            return False
    return True
# v8.3.31 -- anchor moved from 10:00 ET (OR end, when entries first
# fire) to 12:30 ET (midday) because the 1-min bar archive starts at
# 09:30 ET and ATR(14) needs >=14 prior 5-min bars. At 10:00 ET only
# ~6 5m bars exist from today; production has 14+ via prior-day
# accumulation, but we don't carry that across days in the archive.
# Mid-day (12:30 ET = bucket 750) gives us ~36 5m bars from the same
# session, plenty for ATR(14). The trade-off: this isn't the exact
# moment production fires its first entry, but the ATR-comparison
# question is bar-source-vs-tick-source, which is invariant to the
# anchor as long as both methods have the same prior history.
ANCHOR_BUCKET_ET = 12 * 60 + 30   # 12:30 ET
LOOKBACK_5M_BARS = 20


def _et_bucket(dt: datetime) -> int:
    """Minutes since ET midnight."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    et = dt.astimezone(ET)
    return et.hour * 60 + et.minute


def _load_ticks_from_r2(ticker: str, date_iso: str) -> list[dict]:
    """Read s3://$R2_BUCKET/$R2_PREFIX/<DATE>/<TK>.jsonl.gz into a list."""
    import boto3
    key_prefix = os.environ.get("R2_TICK_PREFIX", "tick-data")
    bucket = os.environ["R2_BUCKET"]
    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    key = f"{key_prefix}/{date_iso}/{ticker}.jsonl.gz"
    buf = io.BytesIO()
    s3.download_fileobj(bucket, key, buf)
    buf.seek(0)
    rows = []
    with gzip.GzipFile(fileobj=buf, mode="rb") as fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def aggregate_ticks_to_5m(ticks: list[dict]) -> list[tuple[int, float, float, float]]:
    """Bucket tape-eligible ticks into 5-min OHLC. Returns
    [(et_bucket_end, hi, lo, close), ...] ordered chronologically.
    Each 5-min bar's `et_bucket_end` is the last minute of that 5-min
    window (matches `bucket % 5 == 4` semantics elsewhere).

    v8.3.33 -- skips trades whose ``conditions`` field flags them as
    non-last-sale-eligible (dark-pool, odd-lot, late prints, etc.).
    Without this filter the tick-derived high/low is dominated by
    outlier prints that the SIP-consolidated 1-min bars correctly
    exclude.
    """
    per_5m: dict[int, dict] = {}
    for t in ticks:
        ts_str = t.get("ts")
        price = t.get("price")
        if not ts_str or price is None:
            continue
        # v8.3.33 -- filter to tape-eligible conditions only
        if not is_tape_eligible(t.get("conditions")):
            continue
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        minute = _et_bucket(ts)
        # 5-min window: 09:30-09:34 ends at 9:34 (bucket 574, %5==4)
        window_end = minute - (minute % 5) + 4
        d = per_5m.setdefault(window_end, {
            "hi": -float("inf"),
            "lo": float("inf"),
            "close": price,
            "close_ts": ts,
        })
        p = float(price)
        if p > d["hi"]:
            d["hi"] = p
        if p < d["lo"]:
            d["lo"] = p
        if ts > d["close_ts"]:
            d["close"] = p
            d["close_ts"] = ts
    out = []
    for bucket, v in sorted(per_5m.items()):
        if v["hi"] == -float("inf"):
            continue
        out.append((bucket, v["hi"], v["lo"], v["close"]))
    return out


def _load_1m_bars_from_archive(ticker: str, date_iso: str,
                                archive_root: Path) -> list[dict]:
    p = archive_root / date_iso / f"{ticker}.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def aggregate_1m_to_5m(bars_1m: list[dict]) -> list[tuple[int, float, float, float]]:
    """Same bucket convention as aggregate_ticks_to_5m."""
    per_5m: dict[int, dict] = {}
    for b in bars_1m:
        ts_str = b.get("ts") or ""
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            continue
        minute = _et_bucket(ts)
        window_end = minute - (minute % 5) + 4
        hi = float(b.get("high") or 0)
        lo = float(b.get("low") or 0)
        cl = float(b.get("close") or 0)
        d = per_5m.setdefault(window_end, {
            "hi": hi, "lo": lo, "close": cl, "close_ts": ts,
        })
        if hi > d["hi"]:
            d["hi"] = hi
        if lo < d["lo"]:
            d["lo"] = lo
        if ts > d["close_ts"]:
            d["close"] = cl
            d["close_ts"] = ts
    out = []
    for bucket, v in sorted(per_5m.items()):
        out.append((bucket, v["hi"], v["lo"], v["close"]))
    return out


def compute_atr(bars: list[tuple[int, float, float, float]],
                anchor_bucket: int,
                lookback: int = ATR_PERIOD) -> float | None:
    """ATR(lookback) computed across the `lookback` 5m bars whose
    window-end <= anchor_bucket.
    """
    eligible = [b for b in bars if b[0] <= anchor_bucket]
    if len(eligible) < lookback:
        return None
    series = eligible[-(lookback + 1):]  # need 1 prior bar for first TR
    trs = []
    for i in range(1, len(series)):
        _b, hi, lo, _cl = series[i]
        _, _, _, prev_close = series[i - 1]
        tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        trs.append(tr)
    use = trs[-lookback:]
    if not use:
        return None
    return sum(use) / len(use)


def compare_one(ticker: str, date_iso: str,
                bar_archive: Path) -> dict:
    """Compute ATR(5m, 14) from ticks vs from 1m bars at the OR-end
    reference (10:00 ET) for one (ticker, date) pair.
    """
    try:
        ticks = _load_ticks_from_r2(ticker, date_iso)
    except Exception as e:
        return {"ticker": ticker, "date": date_iso, "error": f"tick_load: {e}"}
    bars_1m = _load_1m_bars_from_archive(ticker, date_iso, bar_archive)
    if not bars_1m:
        return {"ticker": ticker, "date": date_iso,
                "error": f"1m bars missing at {bar_archive}/{date_iso}/{ticker}.jsonl"}

    bars_tick_5m = aggregate_ticks_to_5m(ticks)
    bars_1m_5m = aggregate_1m_to_5m(bars_1m)

    # v8.3.33 -- count how many ticks survived the tape-eligibility filter
    n_ticks_eligible = sum(
        1 for t in ticks
        if t.get("price") is not None and t.get("ts")
        and is_tape_eligible(t.get("conditions"))
    )

    # Anchor at 12:30 ET (mid-session). The 1-min bar archive starts
    # at 09:30 ET, so by 12:30 ET there are ~36 5m bars from the
    # session -- plenty for the 14-bar ATR window.
    anchor = ANCHOR_BUCKET_ET
    atr_tick = compute_atr(bars_tick_5m, anchor, lookback=ATR_PERIOD)
    atr_1m = compute_atr(bars_1m_5m, anchor, lookback=ATR_PERIOD)

    ratio = None
    abs_diff = None
    if atr_tick is not None and atr_1m is not None and atr_1m > 0:
        ratio = atr_tick / atr_1m
        abs_diff = atr_tick - atr_1m

    return {
        "ticker": ticker,
        "date": date_iso,
        "n_ticks": len(ticks),
        "n_ticks_eligible": n_ticks_eligible,
        "n_ticks_filtered_out": len(ticks) - n_ticks_eligible,
        "n_bars_1m": len(bars_1m),
        "n_bars_5m_from_ticks": len(bars_tick_5m),
        "n_bars_5m_from_1m": len(bars_1m_5m),
        "anchor_bucket_et": anchor,
        "atr_5m_14_from_ticks": atr_tick,
        "atr_5m_14_from_1m": atr_1m,
        "ratio_tick_over_1m": ratio,
        "abs_diff": abs_diff,
    }


def verdict_for(rows: list[dict]) -> dict:
    """Aggregate ratios across all (ticker, date) pairs and emit a
    qualitative verdict.

    Hypothesis: ratio < 0.8 -- tick ATR meaningfully tighter than 1m
    ATR -- justifies full corpus pull + replay integration.
    Ratio 0.8-1.0 -- marginal; integration cost may not be worth it.
    Ratio ~1.0 -- hypothesis falsified.
    """
    ratios = [r["ratio_tick_over_1m"] for r in rows
              if r.get("ratio_tick_over_1m") is not None]
    if not ratios:
        return {"verdict": "no_data", "n_compared": 0}
    median = statistics.median(ratios)
    p10 = statistics.quantiles(ratios, n=10)[0] if len(ratios) >= 10 else min(ratios)
    p90 = statistics.quantiles(ratios, n=10)[8] if len(ratios) >= 10 else max(ratios)
    if median < 0.8:
        v = "tick_atr_meaningfully_tighter"
        rec = "Pursue full FY tick pull + replay integration"
    elif median < 0.95:
        v = "tick_atr_modestly_tighter"
        rec = "Marginal -- evaluate integration cost vs expected lift"
    elif median < 1.05:
        v = "no_meaningful_difference"
        rec = "Hypothesis falsified -- fidelity is bounded elsewhere"
    else:
        v = "tick_atr_wider (unexpected)"
        rec = "Investigate -- aggregation likely wrong"
    return {
        "verdict": v,
        "recommendation": rec,
        "n_compared": len(ratios),
        "median_ratio": median,
        "p10_ratio": p10,
        "p90_ratio": p90,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument(
        "--tickers", default="AAPL,MSFT,NVDA,TSLA,META,GOOG,AMZN,AVGO,NFLX,ORCL,SPY,QQQ",
        help="Comma-separated tickers",
    )
    ap.add_argument(
        "--bar-archive", required=True, type=Path,
        help="Path to 1-min bar archive root (contains <DATE>/<TICKER>.jsonl)",
    )
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    rows = []
    for tk in tickers:
        row = compare_one(tk, args.date, args.bar_archive)
        rows.append(row)
        if row.get("error"):
            print(f"  {tk} {args.date}: ERROR {row['error']}", flush=True)
        else:
            r = row.get("ratio_tick_over_1m")
            r_disp = f"{r:.3f}" if r is not None else "n/a"
            print(f"  {tk} {args.date}: "
                  f"atr_tick={row['atr_5m_14_from_ticks']} "
                  f"atr_1m={row['atr_5m_14_from_1m']} "
                  f"ratio={r_disp}",
                  flush=True)

    v = verdict_for(rows)
    summary = {
        "schema_version": 1,
        "captured_at_utc": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": args.date,
        "tickers": tickers,
        "rows": rows,
        "verdict": v,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(f"\nWrote {args.out}", flush=True)
    print(f"Verdict: {v['verdict']} (median ratio {v.get('median_ratio')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
