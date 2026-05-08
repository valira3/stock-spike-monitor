"""tools/fetch_alpaca_bars.py — download 1m bars from Alpaca and write
them to the production bar archive layout.

Mirrors the schema and code path used by ingest/algo_plus.py:_backfill so
the resulting JSONL files are byte-compatible with what the live engine
writes. Uses bar_archive.write_bar() for the actual append.

Credentials (env):
    VAL_ALPACA_PAPER_KEY / VAL_ALPACA_PAPER_SECRET   (preferred)
    GENE_ALPACA_PAPER_KEY / GENE_ALPACA_PAPER_SECRET (fallback)

Output:
    Writes to ${TG_DATA_ROOT}/bars/YYYY-MM-DD/<TICKER>.jsonl
    (TG_DATA_ROOT defaults to /data; override with --base-dir.)

Feed:
    --feed sip   full SIP (requires Alpaca Algo Trader Plus subscription)
    --feed iex   IEX-only (free paper tier)
    Defaults to sip; falls back to iex automatically on a 403/permission
    error from the first ticker.

Usage:
    export VAL_ALPACA_PAPER_KEY=...
    export VAL_ALPACA_PAPER_SECRET=...
    python3 tools/fetch_alpaca_bars.py \\
        --start 2026-04-20 --end 2026-04-30 \\
        --tickers AAPL,MSFT,NVDA,QQQ,SPY \\
        --base-dir ./data/bars
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import bar_archive  # noqa: E402

ET = ZoneInfo("America/New_York")

DEFAULT_TICKERS = (
    "AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOG", "AMZN",
    "AVGO", "NFLX", "ORCL", "SPY", "QQQ",
)


def _resolve_creds() -> tuple[str, str]:
    k = os.environ.get("VAL_ALPACA_PAPER_KEY", "").strip()
    s = os.environ.get("VAL_ALPACA_PAPER_SECRET", "").strip()
    if k and s:
        return k, s
    k = os.environ.get("GENE_ALPACA_PAPER_KEY", "").strip()
    s = os.environ.get("GENE_ALPACA_PAPER_SECRET", "").strip()
    if k and s:
        return k, s
    print(
        "[FATAL] no Alpaca credentials in env. Set VAL_ALPACA_PAPER_KEY + "
        "VAL_ALPACA_PAPER_SECRET (or the GENE_ equivalents).",
        file=sys.stderr,
    )
    sys.exit(2)


def _trading_days(start: date, end: date) -> list[date]:
    """Mon–Fri only. Skips obvious US-equity holidays we know about; the
    Alpaca API will simply return zero bars for any closed day, which is
    fine — we just won't write anything."""
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _et_window(d: date, premarket: bool) -> tuple[datetime, datetime]:
    if premarket:
        start_et = datetime(d.year, d.month, d.day, 4, 0, tzinfo=ET)
    else:
        start_et = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
    end_et = datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET)
    return start_et.astimezone(timezone.utc), end_et.astimezone(timezone.utc)


def _fetch_one_day(client, ticker: str, day: date, feed: str, premarket: bool):
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    start_utc, end_utc = _et_window(day, premarket)
    req = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Minute,
        start=start_utc,
        end=end_utc,
        feed=feed,
        limit=10000,
    )
    resp = client.get_stock_bars(req)
    rows = []
    if hasattr(resp, "data"):
        rows = resp.data.get(ticker, []) or []
    return rows


def _bar_to_dict(b, feed: str) -> dict | None:
    ts_obj = getattr(b, "timestamp", None)
    if ts_obj is None:
        return None
    if ts_obj.tzinfo is None:
        ts_obj = ts_obj.replace(tzinfo=timezone.utc)
    ts_str = ts_obj.isoformat()
    vol_raw = getattr(b, "volume", None)
    try:
        vol = float(vol_raw) if vol_raw is not None else None
    except (TypeError, ValueError):
        vol = None
    et = ts_obj.astimezone(ET)
    in_rth = (
        (et.hour == 9 and et.minute >= 30)
        or (10 <= et.hour <= 15)
        or (et.hour == 16 and et.minute == 0)
    )
    et_bucket = f"{et.hour:02d}{et.minute:02d}" if in_rth else None
    return {
        "ts": ts_str,
        "et_bucket": et_bucket,
        "open": float(getattr(b, "open", 0) or 0),
        "high": float(getattr(b, "high", 0) or 0),
        "low": float(getattr(b, "low", 0) or 0),
        "close": float(getattr(b, "close", 0) or 0),
        "total_volume": vol,
        "iex_volume": None,
        "iex_sip_ratio_used": None,
        "bid": None,
        "ask": None,
        "last_trade_price": None,
        "trade_count": getattr(b, "trade_count", None),
        "bar_vwap": getattr(b, "vwap", None),
        "feed_source": feed,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Download 1m Alpaca bars into the production /data/bars layout."
    )
    ap.add_argument("--start", required=True, help="YYYY-MM-DD inclusive")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD inclusive")
    ap.add_argument(
        "--tickers",
        default=",".join(DEFAULT_TICKERS),
        help="Comma-separated tickers (default: 12 prod universe)",
    )
    ap.add_argument(
        "--feed",
        default="sip",
        choices=("sip", "iex"),
        help="Alpaca data feed (sip requires paid sub; falls back to iex on 403)",
    )
    ap.add_argument(
        "--base-dir",
        default=None,
        help="Output base dir (default: $TG_DATA_ROOT/bars or /data/bars)",
    )
    ap.add_argument(
        "--premarket",
        action="store_true",
        help="Include 04:00–09:30 ET premarket bars (default: RTH only)",
    )
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end < start:
        print("[FATAL] --end before --start", file=sys.stderr)
        return 2

    tickers = tuple(t.strip().upper() for t in args.tickers.split(",") if t.strip())
    if not tickers:
        print("[FATAL] no tickers", file=sys.stderr)
        return 2

    base_dir = args.base_dir or bar_archive.DEFAULT_BASE_DIR
    Path(base_dir).mkdir(parents=True, exist_ok=True)

    key, secret = _resolve_creds()

    from alpaca.data.historical import StockHistoricalDataClient
    client = StockHistoricalDataClient(api_key=key, secret_key=secret)

    feed = args.feed
    days = _trading_days(start, end)
    print(
        f"[FETCH] tickers={len(tickers)} days={len(days)} feed={feed} "
        f"base_dir={base_dir} premarket={args.premarket}"
    )

    total_written = 0
    for day in days:
        for ticker in tickers:
            try:
                rows = _fetch_one_day(client, ticker, day, feed, args.premarket)
            except Exception as e:
                msg = str(e)
                if feed == "sip" and ("subscription" in msg.lower() or "403" in msg):
                    print(
                        f"[FETCH] SIP denied ({e}). Falling back to iex feed.",
                        file=sys.stderr,
                    )
                    feed = "iex"
                    try:
                        rows = _fetch_one_day(client, ticker, day, feed, args.premarket)
                    except Exception as e2:
                        print(f"[WARN] {ticker} {day}: {e2}", file=sys.stderr)
                        continue
                else:
                    print(f"[WARN] {ticker} {day}: {e}", file=sys.stderr)
                    continue

            wrote = 0
            for b in rows:
                bd = _bar_to_dict(b, feed)
                if bd is None:
                    continue
                try:
                    bar_archive.write_bar(
                        ticker, bd, base_dir=base_dir, today=day
                    )
                    wrote += 1
                except Exception as e:
                    print(f"[WARN] write {ticker} {day}: {e}", file=sys.stderr)
            total_written += wrote
            print(f"[FETCH] {day} {ticker:6s} bars={wrote} feed={feed}")

    print(f"[FETCH] DONE total_bars_written={total_written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
