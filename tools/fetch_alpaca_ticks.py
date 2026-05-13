"""v8.3.28 -- pull historical trade ticks from Alpaca + write to the
tick-data archive layout.

Mirror of tools/fetch_alpaca_bars.py but for the tick-level
StockTradesRequest endpoint. Each trade record is one print on the
exchange; ATR and stop-distance computations from ticks are much
tighter than from 1-min OHLC summaries, closing the data-fidelity
gap between the orb_replay_day backtest and the live engine.

Output layout (deliberately separate from /bars/<DATE>/<TICKER>.jsonl):
    <base-dir>/<YYYY-MM-DD>/<TICKER>.jsonl.gz

Each row is:
    {
      "ts": "2026-05-12T13:30:00.123456+00:00",
      "price": 264.41,
      "size": 100,
      "exchange": "Q",
      "conditions": ["@"],
      "tape": "C",
      "feed_source": "sip"
    }

Files are gzip-compressed in-place because a single liquid-stock
ticker-day can hit 100K-500K trades (~10 MB uncompressed, ~3 MB
compressed). Without compression the FY corpus would be ~30 GB.

Credentials (env, mirror of fetch_alpaca_bars.py):
    VAL_ALPACA_PAPER_KEY / VAL_ALPACA_PAPER_SECRET   (preferred)
    GENE_ALPACA_PAPER_KEY / GENE_ALPACA_PAPER_SECRET (fallback)

Feed:
    --feed sip (default; requires Alpaca Algo Trader Plus)
    --feed iex (free paper tier, only IEX prints)

Usage:
    python3 tools/fetch_alpaca_ticks.py \\
        --start 2026-05-12 --end 2026-05-12 \\
        --tickers AAPL,MSFT,NVDA \\
        --base-dir /tmp/tick-data

Rate limit: Alpaca's Algo Trader Plus tier allows 200 req/min. Each
ticker-day is one paginated request (typically 5-50 pages), so a
single FY-corpus pull would hit rate limits. Use --sleep-ms-between-tickers
to throttle (default 100ms = comfortable).
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

DEFAULT_TICKERS = [
    "AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOG",
    "AMZN", "AVGO", "NFLX", "ORCL", "SPY", "QQQ",
]


def _key_secret() -> tuple[str, str]:
    """Resolve Alpaca credentials, matching fetch_alpaca_bars.py order."""
    for prefix in ("VAL", "GENE"):
        k = os.environ.get(f"{prefix}_ALPACA_PAPER_KEY", "").strip()
        s = os.environ.get(f"{prefix}_ALPACA_PAPER_SECRET", "").strip()
        if k and s:
            return k, s
    # Fall back to the unbranded env vars some workflows use
    k = os.environ.get("ALPACA_KEY", "").strip()
    s = os.environ.get("ALPACA_SECRET", "").strip()
    if k and s:
        return k, s
    raise RuntimeError(
        "Need VAL_ALPACA_PAPER_KEY/SECRET (or GENE_*/ALPACA_KEY) in env."
    )


def _is_weekday(d: date) -> bool:
    return d.weekday() < 5


def _enum_days(start: date, end: date) -> list[date]:
    out = []
    cur = start
    while cur <= end:
        if _is_weekday(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


def _et_window(d: date, premarket: bool) -> tuple[datetime, datetime]:
    if premarket:
        start_et = datetime(d.year, d.month, d.day, 4, 0, tzinfo=ET)
    else:
        start_et = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
    end_et = datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET)
    return start_et.astimezone(timezone.utc), end_et.astimezone(timezone.utc)


def _fetch_trades_one_day(client, ticker: str, day: date,
                          feed: str, premarket: bool):
    """Paginated fetch of trade ticks for one ticker-day.

    v8.3.31 -- fixed pagination. Alpaca returns up to 10000 trades
    per page; on liquid mega-caps a single day can have 100K+ trades.
    We iterate ``next_page_token`` until exhausted.
    """
    from alpaca.data.requests import StockTradesRequest
    start_utc, end_utc = _et_window(day, premarket)
    all_trades: list = []
    page_token = None
    while True:
        kwargs = dict(
            symbol_or_symbols=ticker,
            start=start_utc,
            end=end_utc,
            feed=feed,
            limit=10000,
        )
        if page_token is not None:
            kwargs["page_token"] = page_token
        req = StockTradesRequest(**kwargs)
        resp = client.get_stock_trades(req)
        # Page trades for this symbol
        page_trades: list = []
        if hasattr(resp, "data"):
            page_trades = resp.data.get(ticker, []) or []
        all_trades.extend(page_trades)
        # Continue if there's a next page token. Different alpaca-py
        # versions surface it differently; check common shapes.
        next_token = None
        if hasattr(resp, "next_page_token") and resp.next_page_token:
            next_token = resp.next_page_token
        elif hasattr(resp, "raw") and isinstance(resp.raw, dict):
            next_token = resp.raw.get("next_page_token")
        if not next_token:
            break
        page_token = next_token
    return all_trades


def _trade_to_dict(t, feed: str) -> dict | None:
    """Normalize an Alpaca Trade into the compact JSONL row."""
    ts_obj = getattr(t, "timestamp", None)
    if ts_obj is None:
        return None
    if ts_obj.tzinfo is None:
        ts_obj = ts_obj.replace(tzinfo=timezone.utc)
    price = getattr(t, "price", None)
    size = getattr(t, "size", None)
    if price is None or size is None:
        return None
    return {
        "ts": ts_obj.isoformat(),
        "price": float(price),
        "size": int(size),
        "exchange": getattr(t, "exchange", None),
        "conditions": getattr(t, "conditions", None) or [],
        "tape": getattr(t, "tape", None),
        "id": getattr(t, "id", None),
        "feed_source": feed,
    }


def _write_jsonl_gz(rows: list[dict], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")))
            fh.write("\n")
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Download Alpaca trade ticks into a per-ticker-day JSONL.gz archive.",
    )
    ap.add_argument("--start", required=True, help="YYYY-MM-DD inclusive")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD inclusive")
    ap.add_argument(
        "--tickers", default=",".join(DEFAULT_TICKERS),
        help="Comma-separated tickers (default: 12-ticker v10 universe)",
    )
    ap.add_argument(
        "--feed", default="sip", choices=("sip", "iex"),
        help="Alpaca data feed",
    )
    ap.add_argument(
        "--base-dir", default=None,
        help="Output base dir. Default: $TG_TICKDATA_ROOT or ./tick-data",
    )
    ap.add_argument(
        "--premarket", action="store_true",
        help="Include 04:00-09:30 ET premarket trades (default: RTH only)",
    )
    ap.add_argument(
        "--sleep-ms-between-tickers", type=int, default=100,
        help="Throttle between API calls (default 100ms = 600/min, well "
             "under the 200/min limit assuming bursts)",
    )
    ap.add_argument(
        "--skip-existing", action="store_true", default=True,
        help="Skip <date>/<ticker>.jsonl.gz files that already exist",
    )
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if start > end:
        print(f"::error::start {start} > end {end}", file=sys.stderr)
        return 1
    days = _enum_days(start, end)
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    base = Path(args.base_dir or os.environ.get(
        "TG_TICKDATA_ROOT", "./tick-data"
    )).resolve()

    print(f"[fetch-ticks] {len(days)} days x {len(tickers)} tickers "
          f"-> {base} feed={args.feed}", flush=True)

    # Resolve creds + client (deferred so --help works without env set)
    key, secret = _key_secret()
    from alpaca.data.historical import StockHistoricalDataClient
    client = StockHistoricalDataClient(key, secret)

    total_trades = 0
    total_files = 0
    t0 = time.time()
    feed = args.feed
    for d in days:
        for tk in tickers:
            fp = base / d.strftime("%Y-%m-%d") / f"{tk}.jsonl.gz"
            if args.skip_existing and fp.exists():
                continue
            try:
                trades = _fetch_trades_one_day(
                    client, tk, d, feed=feed, premarket=args.premarket,
                )
            except Exception as e:
                msg = str(e)
                # Auto-fall-back to IEX on SIP permission errors
                if feed == "sip" and ("403" in msg or "permission" in msg.lower()):
                    print(f"  {tk} {d}: SIP forbidden, falling back to IEX",
                          flush=True)
                    feed = "iex"
                    try:
                        trades = _fetch_trades_one_day(
                            client, tk, d, feed=feed,
                            premarket=args.premarket,
                        )
                    except Exception as e2:
                        print(f"  {tk} {d}: IEX FAIL {e2}", file=sys.stderr)
                        continue
                else:
                    print(f"  {tk} {d}: FAIL {e}", file=sys.stderr)
                    continue
            rows = []
            for t in trades:
                row = _trade_to_dict(t, feed)
                if row is not None:
                    rows.append(row)
            if not rows:
                continue
            n = _write_jsonl_gz(rows, fp)
            total_trades += n
            total_files += 1
            if total_files % 5 == 0:
                print(f"  {d} {tk}: {n} trades  "
                      f"(running: {total_files} files, "
                      f"{total_trades:,} trades, "
                      f"{time.time() - t0:.1f}s)",
                      flush=True)
            if args.sleep_ms_between_tickers > 0:
                time.sleep(args.sleep_ms_between_tickers / 1000.0)

    elapsed = time.time() - t0
    print(f"[fetch-ticks] DONE {total_files} files, "
          f"{total_trades:,} trades in {elapsed:.1f}s",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
