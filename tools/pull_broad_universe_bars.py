"""Pull premarket + RTH 1-min bars for a broad universe (S&P 500) and
write them to data_pm_universe/<DATE>/<TICKER>.jsonl in the production
schema. Resume-friendly: a batch's day-files that already exist are
skipped on the next pass.

Window: 04:00 ET (premarket open) to 16:00 ET (RTH close).
Premarket bars: et_bucket < "0930"; RTH: et_bucket >= "0930".

Env: VAL_ALPACA_PAPER_KEY / VAL_ALPACA_PAPER_SECRET (SIP entitlement).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame


ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def _trading_days(start: date, end: date) -> list[date]:
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _et_bucket(ts: datetime) -> str:
    et = ts.astimezone(ET)
    return f"{et.hour:02d}{et.minute:02d}"


def _bars_for_day_exist(out_root: Path, d: date, tickers: list[str]) -> int:
    day = out_root / d.isoformat()
    if not day.exists():
        return 0
    existing = {p.stem for p in day.glob("*.jsonl")}
    return sum(1 for t in tickers if t in existing)


def _pull_chunk(
    client: StockHistoricalDataClient,
    batch: list[str],
    chunk_start: date,
    chunk_end: date,
    out_root: Path,
) -> int:
    """Pull one (batch, date-chunk) and write per-day-per-ticker files."""
    req = StockBarsRequest(
        symbol_or_symbols=batch,
        timeframe=TimeFrame.Minute,
        start=datetime.combine(chunk_start, datetime.min.time(), tzinfo=ET).replace(hour=4),
        end=datetime.combine(chunk_end, datetime.min.time(), tzinfo=ET).replace(hour=16),
        feed="sip",
    )
    resp = client.get_stock_bars(req)
    if resp.df is None or resp.df.empty:
        return 0

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    df = resp.df
    for idx, row in df.iterrows():
        sym, ts = idx
        if not hasattr(ts, "tzinfo") or ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        et_ts = ts.tz_convert("America/New_York")
        d_str = et_ts.date().isoformat()
        rec = {
            "ts": ts.to_pydatetime().isoformat(),
            "et_bucket": f"{et_ts.hour:02d}{et_ts.minute:02d}",
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "total_volume": float(row["volume"]),
            "iex_volume": None,
            "iex_sip_ratio_used": None,
            "bid": None,
            "ask": None,
            "last_trade_price": None,
            "trade_count": float(row.get("trade_count", 0)) if "trade_count" in row else 0.0,
            "bar_vwap": float(row.get("vwap", row["close"])) if "vwap" in row else float(row["close"]),
            "feed_source": "sip",
        }
        grouped[(d_str, sym)].append(rec)

    bars_total = 0
    for (d_str, sym), recs in grouped.items():
        day_dir = out_root / d_str
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"{sym}.jsonl"
        with path.open("w") as f:  # one chunk per calendar month, so each (date,ticker) is written once
            for r in recs:
                f.write(json.dumps(r) + "\n")
        bars_total += len(recs)
    return bars_total


def _month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Return list of (chunk_start, chunk_end) one per calendar month."""
    chunks = []
    cur = date(start.year, start.month, 1)
    while cur <= end:
        # Last day of this month
        if cur.month == 12:
            next_m = date(cur.year + 1, 1, 1)
        else:
            next_m = date(cur.year, cur.month + 1, 1)
        chunk_end = min(end, next_m - timedelta(days=1))
        chunk_start = max(start, cur)
        if chunk_start <= chunk_end:
            chunks.append((chunk_start, chunk_end))
        cur = next_m
    return chunks


def pull_batch(
    client: StockHistoricalDataClient,
    batch: list[str],
    start: date,
    end: date,
    out_root: Path,
) -> int:
    """Pull a batch over the date range in monthly chunks (memory + progress visibility)."""
    total = 0
    chunks = _month_chunks(start, end)
    for cs, ce in chunks:
        # Skip the chunk entirely if every (date, ticker) file in this
        # window already exists.
        days_in_chunk = _trading_days(cs, ce)
        expected = len(batch) * len(days_in_chunk)
        existing = sum(_bars_for_day_exist(out_root, d, batch) for d in days_in_chunk)
        if expected and existing >= expected:
            print(f"      chunk {cs.isoformat()}..{ce.isoformat()}  -- {existing}/{expected} files exist, skip",
                  flush=True)
            continue

        t0 = time.time()
        try:
            n = _pull_chunk(client, batch, cs, ce, out_root)
        except Exception as e:
            print(f"      chunk {cs.isoformat()} FAIL: {e}", flush=True, file=sys.stderr)
            continue
        total += n
        print(f"      chunk {cs.isoformat()}..{ce.isoformat()}  {n:>8,}b  {time.time()-t0:>5.1f}s",
              flush=True)
    return total


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--universe", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--out", default="data_pm_universe")
    p.add_argument("--batch-size", type=int, default=50)
    p.add_argument("--skip-frac", type=float, default=0.95,
                   help="Skip a batch if ≥ this fraction of its (date,ticker) files exist")
    p.add_argument("--limit-tickers", type=int, default=0)
    p.add_argument("--start-batch", type=int, default=0, help="Resume from batch N")
    args = p.parse_args(argv[1:])

    uni = json.loads(Path(args.universe).read_text())
    tickers = uni["tickers"]
    if args.limit_tickers > 0:
        tickers = tickers[: args.limit_tickers]

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    days = _trading_days(start, end)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    n_batches = (len(tickers) + args.batch_size - 1) // args.batch_size
    print(f"Universe: {len(tickers)} tickers, {len(days)} trading days, "
          f"{n_batches} batches × {args.batch_size}", flush=True)
    print(f"Output:   {out}", flush=True)

    key = os.environ.get("VAL_ALPACA_PAPER_KEY")
    sec = os.environ.get("VAL_ALPACA_PAPER_SECRET")
    if not key or not sec:
        print("FATAL: VAL_ALPACA_PAPER_KEY/SECRET not set", file=sys.stderr)
        return 1
    client = StockHistoricalDataClient(key, sec)

    total_bars = 0
    t_start = time.time()
    for i in range(args.start_batch * args.batch_size, len(tickers), args.batch_size):
        bi = i // args.batch_size + 1
        batch = tickers[i : i + args.batch_size]

        # Skip-check
        expected = len(batch) * len(days)
        existing = sum(_bars_for_day_exist(out, d, batch) for d in days)
        if expected and existing / expected >= args.skip_frac:
            print(f"[{bi:>3}/{n_batches}] {batch[0]:6}..{batch[-1]:6} ({len(batch):>2}tk) "
                  f"-- {existing}/{expected} files exist, skip", flush=True)
            continue

        t0 = time.time()
        try:
            bars = pull_batch(client, batch, start, end, out)
        except Exception as e:
            print(f"[{bi:>3}/{n_batches}] FAIL batch starting {batch[0]}: {e}", file=sys.stderr, flush=True)
            continue
        dt = time.time() - t0
        total_bars += bars
        done = bi - args.start_batch
        eta_min = (time.time() - t_start) / max(done, 1) * (n_batches - bi) / 60
        print(f"[{bi:>3}/{n_batches}] {batch[0]:6}..{batch[-1]:6} ({len(batch):>2}tk) "
              f"{bars:>9,}b  {dt:>5.1f}s  ETA {eta_min:>5.1f}m", flush=True)

    elapsed = (time.time() - t_start) / 60
    print(f"\nDone. {total_bars:,} bars in {elapsed:.1f} min", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
