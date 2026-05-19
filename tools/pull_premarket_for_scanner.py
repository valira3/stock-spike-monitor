"""Pull premarket bars (04:00-09:29 ET) for the S&P 500 into the bar
archive. Intended to run at ~09:24 ET each trading morning so the v10
broad-universe scanner has fresh data when `ensure_session_started`
fires shortly after.

Writes to `${TG_DATA_ROOT}/bars/<DATE>/<TICKER>.jsonl` in the SAME
schema as bar_archive.py \u2014 the live scanner reads from the same
directory.

Resume-friendly: per-(date, ticker) file skip means re-running mid-
window is safe.

Usage:
    VAL_ALPACA_PAPER_KEY=...   # SIP entitlement
    VAL_ALPACA_PAPER_SECRET=...
    python tools/pull_premarket_for_scanner.py \\
        [--universe data/universe/sp500.json] \\
        [--date 2026-05-20] \\
        [--out /data/bars] \\
        [--batch-size 50]

Default --date is today's ET date. --out defaults to
`${TG_DATA_ROOT:/data}/bars` to match the production bar archive
layout written by bar_archive.py.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
DEFAULT_BATCH_SIZE = 50


def _today_et() -> date:
    return datetime.now(ET).date()


def _exists_and_nonempty(p: Path) -> bool:
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def _pull(client, batch: list[str], target_date: date, out_root: Path) -> int:
    """Pull 04:00-09:30 ET premarket bars for one batch of tickers on
    one date. Writes JSONL per (date, ticker). Returns bars written.
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    start = datetime.combine(target_date, dt_time(4, 0), tzinfo=ET)
    end = datetime.combine(target_date, dt_time(9, 30), tzinfo=ET)
    req = StockBarsRequest(
        symbol_or_symbols=batch,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        feed="sip",
    )
    resp = client.get_stock_bars(req)
    if resp.df is None or resp.df.empty:
        return 0
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for idx, row in resp.df.iterrows():
        sym, ts = idx
        if not hasattr(ts, "tz_convert"):
            continue
        ts_utc = ts.tz_convert("UTC")
        et_ts = ts.tz_convert("America/New_York")
        d_str = et_ts.date().isoformat()
        rec = {
            "ts": ts_utc.to_pydatetime().isoformat(),
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
    bars = 0
    for (d_str, sym), recs in grouped.items():
        day_dir = out_root / d_str
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"{sym}.jsonl"
        if _exists_and_nonempty(path):
            # Skip already-written tickers for this date (resume-friendly).
            continue
        with path.open("w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        bars += len(recs)
    return bars


def rebuild_premarket_bars_for_date(
    *,
    target_date: date,
    out_root: Path,
    universe_tickers: list[str],
    batch_size: int = DEFAULT_BATCH_SIZE,
    skip_frac: float = 0.95,
) -> int:
    """In-process rebuild used by the live runtime when the 09:24 ET GHA
    cron didn't deliver. Same logic as `main()` but importable; bubbles
    only the bar count (returns 0 on missing creds or import failure).
    """
    key = os.environ.get("VAL_ALPACA_PAPER_KEY")
    sec = os.environ.get("VAL_ALPACA_PAPER_SECRET")
    if not key or not sec:
        return 0
    try:
        from alpaca.data.historical import StockHistoricalDataClient
    except ImportError:
        return 0
    out_root.mkdir(parents=True, exist_ok=True)
    client = StockHistoricalDataClient(key, sec)
    total = 0
    for i in range(0, len(universe_tickers), batch_size):
        batch = universe_tickers[i : i + batch_size]
        existing = sum(
            1 for t in batch
            if _exists_and_nonempty(out_root / target_date.isoformat() / f"{t}.jsonl")
        )
        if existing / max(len(batch), 1) >= skip_frac:
            continue
        try:
            total += _pull(client, batch, target_date, out_root)
        except Exception as e:
            import re as _re
            bad = _re.findall(r"invalid symbol:\s*([A-Z./-]+)", str(e))
            if bad:
                retry = [t for t in batch if t not in bad]
                if len(retry) < len(batch):
                    try:
                        total += _pull(client, retry, target_date, out_root)
                    except Exception:
                        pass
    return total


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--universe", default="data/universe/sp500.json")
    p.add_argument("--date", default="", help="ET date YYYY-MM-DD (default: today)")
    default_out = Path(os.environ.get("TG_DATA_ROOT", "/data")) / "bars"
    p.add_argument("--out", default=str(default_out))
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument("--skip-frac", type=float, default=0.95,
                   help="Skip the whole batch if >= this fraction of "
                        "(date,ticker) files already exist (resume-friendly).")
    args = p.parse_args(argv[1:])

    target_date = (
        date.fromisoformat(args.date)
        if args.date
        else _today_et()
    )

    uni = json.loads(Path(args.universe).read_text())
    tickers = list(uni.get("tickers") or [])
    if not tickers:
        print("ERROR: empty universe", file=sys.stderr)
        return 1

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    key = os.environ.get("VAL_ALPACA_PAPER_KEY")
    sec = os.environ.get("VAL_ALPACA_PAPER_SECRET")
    if not key or not sec:
        print("ERROR: VAL_ALPACA_PAPER_KEY / VAL_ALPACA_PAPER_SECRET not set",
              file=sys.stderr)
        return 1
    try:
        from alpaca.data.historical import StockHistoricalDataClient
    except ImportError:
        print("ERROR: alpaca-py not installed (pip install alpaca-py)",
              file=sys.stderr)
        return 1
    client = StockHistoricalDataClient(key, sec)

    n_batches = (len(tickers) + args.batch_size - 1) // args.batch_size
    print(f"Pulling premarket bars for {len(tickers)} S&P 500 tickers on "
          f"{target_date.isoformat()} -- {n_batches} batches × {args.batch_size}",
          flush=True)

    total_bars = 0
    for i in range(0, len(tickers), args.batch_size):
        bi = i // args.batch_size + 1
        batch = tickers[i : i + args.batch_size]
        existing = sum(
            1 for t in batch
            if _exists_and_nonempty(out_root / target_date.isoformat() / f"{t}.jsonl")
        )
        if existing / max(len(batch), 1) >= args.skip_frac:
            print(f"  [{bi:>3}/{n_batches}] {batch[0]:6}..{batch[-1]:6} -- "
                  f"{existing}/{len(batch)} files exist, skip", flush=True)
            continue
        try:
            bars = _pull(client, batch, target_date, out_root)
        except Exception as e:
            msg = str(e)
            # Retry once dropping any invalid symbols reported by Alpaca.
            import re as _re
            bad = _re.findall(r"invalid symbol:\s*([A-Z./-]+)", msg)
            if bad:
                retry = [t for t in batch if t not in bad]
                if len(retry) < len(batch):
                    print(f"  [{bi}] retry: dropping {bad}", flush=True)
                    try:
                        bars = _pull(client, retry, target_date, out_root)
                    except Exception as e2:
                        print(f"  [{bi}] retry FAIL: {e2}", file=sys.stderr, flush=True)
                        continue
                else:
                    print(f"  [{bi}] FAIL: {msg[:200]}", file=sys.stderr, flush=True)
                    continue
            else:
                print(f"  [{bi}] FAIL: {msg[:200]}", file=sys.stderr, flush=True)
                continue
        total_bars += bars
        print(f"  [{bi:>3}/{n_batches}] {batch[0]:6}..{batch[-1]:6} ({len(batch):>2}tk) "
              f"{bars:>8,} bars", flush=True)

    print(f"\nDone. Wrote {total_bars:,} bars under {out_root}/{target_date.isoformat()}/",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
