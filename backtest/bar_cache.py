"""v6.9.0 \u2014 L1 Parquet bar cache for the backtest data layer.

Replaces per-day JSONL reads with a single Parquet file per ticker so
the 84-day SIP corpus is parsed once and served \u22651\u00d7 faster on every
subsequent sweep run.

Cache layout
------------
  <bars_dir>/.cache_v1/<TICKER>.parquet
  <bars_dir>/.cache_v1/<TICKER>.parquet.meta.json

The meta JSON stores the SHA-256 cache key derived from all source
JSONL files for that ticker (path + mtime_ns + size). Any change to
a source file invalidates the cache and triggers a rebuild.

Public API
----------
  get_bars(bars_dir, ticker, date) -> list[dict]
    Drop-in replacement for replay_v511_full.load_day_bars.

CLI
---
  python -m backtest.bar_cache build --bars-dir <dir>
    Pre-builds all ticker Parquets (optional; first sweep auto-builds).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("trade_genius")

# Parquet compression: ZSTD level 3 (fast decode, good ratio)
_COMPRESS = "zstd"
_COMPRESS_LEVEL = 3

# Schema column order \u2014 must match what write_cache and read_cache produce.
_COLUMNS = [
    "ts",
    "date",
    "session",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "vw",
    "n",
]

# Extra pass-through columns stored verbatim from the source JSONL
# (not included in the canonical Parquet schema but preserved in the
# returned dict so callers see the same shape as the raw load path).
_PASSTHROUGH_COLS = [
    "et_bucket",
    "iex_volume",
    "iex_sip_ratio_used",
    "bid",
    "ask",
    "last_trade_price",
    "trade_count",
    "bar_vwap",
    "epoch",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file; malformed lines are silently skipped."""
    if not path.is_file():
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _parse_ts(ts: str | None) -> datetime | None:
    """Parse ISO-8601 timestamp string to a UTC-aware datetime."""
    if not ts:
        return None
    s = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _source_files_for_ticker(bars_dir: Path, ticker: str) -> list[Path]:
    """Return all JSONL source files for ticker, across all dates."""
    ticker_up = ticker.upper()
    paths: list[Path] = []
    if not bars_dir.is_dir():
        return paths
    for day_dir in sorted(bars_dir.iterdir()):
        if not day_dir.is_dir() or day_dir.name.startswith("."):
            continue
        rth = day_dir / f"{ticker_up}.jsonl"
        if rth.is_file():
            paths.append(rth)
        pre = day_dir / "premarket" / f"{ticker_up}.jsonl"
        if pre.is_file():
            paths.append(pre)
    return paths


def _cache_key(paths: list[Path]) -> str:
    """SHA-256 over (path, mtime_ns, size) for each source file."""
    h = hashlib.sha256()
    for p in sorted(str(p) for p in paths):
        try:
            st = os.stat(p)
            h.update(f"{p}\x00{st.st_mtime_ns}\x00{st.st_size}\n".encode())
        except OSError:
            h.update(f"{p}\x00missing\n".encode())
    return h.hexdigest()


def _cache_root(bars_dir: Path) -> Path:
    return bars_dir / ".cache_v1"


def _parquet_path(bars_dir: Path, ticker: str) -> Path:
    return _cache_root(bars_dir) / f"{ticker.upper()}.parquet"


def _meta_path(bars_dir: Path, ticker: str) -> Path:
    return _cache_root(bars_dir) / f"{ticker.upper()}.parquet.meta.json"


def _read_meta(bars_dir: Path, ticker: str) -> dict:
    mp = _meta_path(bars_dir, ticker)
    if not mp.is_file():
        return {}
    try:
        with open(mp, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def _write_meta(bars_dir: Path, ticker: str, key: str) -> None:
    mp = _meta_path(bars_dir, ticker)
    mp.parent.mkdir(parents=True, exist_ok=True)
    with open(mp, "w", encoding="utf-8") as fh:
        json.dump({"key": key}, fh)


def _infer_session(ts_str: str | None, bar: dict) -> str:
    """Infer session label (pre/rth) from bar data."""
    # Explicit session field wins (premarket files carry this)
    sess = bar.get("session")
    if sess:
        return str(sess).lower()
    # Fall back to timestamp hour in UTC (premarket = before 13:30 UTC)
    dt = _parse_ts(ts_str)
    if dt is None:
        return "rth"
    # 09:30 ET = 13:30 UTC (EST) or 14:30 UTC (EDT); use 13:30 as threshold
    return "pre" if dt.hour < 13 or (dt.hour == 13 and dt.minute < 30) else "rth"


def _bars_from_parquet(bars_dir: Path, ticker: str) -> list[dict]:
    """Read all bars from the Parquet cache and return list-of-dicts."""
    import pyarrow.parquet as pq  # lazy import \u2014 optional dep

    pp = _parquet_path(bars_dir, ticker)
    table = pq.read_table(str(pp))
    rows: list[dict] = []
    col_names = table.schema.names
    for i in range(table.num_rows):
        row: dict[str, Any] = {}
        for col in col_names:
            val = table.column(col)[i].as_py()
            row[col] = val
        rows.append(row)
    return rows


def _build_ticker_cache(bars_dir: Path, ticker: str) -> None:
    """Parse all JSONL source files for ticker and write Parquet cache."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    ticker_up = ticker.upper()
    source_files = _source_files_for_ticker(bars_dir, ticker)

    # Collect all bars from source JSONL files
    all_bars: list[dict] = []
    for path in source_files:
        date_str = path.parts[-2] if path.parent.name == "premarket" else path.parent.name
        for bar in _load_jsonl(path):
            # Normalise: add date and session
            ts = bar.get("ts")
            dt = _parse_ts(ts)
            if dt is None:
                continue
            # iex_volume alias \u2014 RTH files use iex_volume; premarket files use volume
            volume = bar.get("iex_volume") or bar.get("volume") or 0
            # vw (VWAP) and n (trade count) may be absent
            vw = bar.get("bar_vwap") or bar.get("vw") or 0.0
            n = bar.get("trade_count") or bar.get("n") or 0

            row: dict[str, Any] = {
                "ts": dt,
                "date": dt.date().isoformat(),
                "session": _infer_session(ts, bar),
                "open": float(bar.get("open") or 0.0),
                "high": float(bar.get("high") or 0.0),
                "low": float(bar.get("low") or 0.0),
                "close": float(bar.get("close") or 0.0),
                "volume": int(volume),
                "vw": float(vw),
                "n": int(n),
                # Passthrough extras \u2014 stored as JSON string to keep schema simple
                "_extras": json.dumps({k: bar.get(k) for k in _PASSTHROUGH_COLS}),
            }
            all_bars.append(row)

    # Sort by timestamp
    all_bars.sort(key=lambda b: b["ts"])

    if not all_bars:
        logger.warning("[bar_cache] no bars found for ticker=%s", ticker_up)

    # Build PyArrow table (pyarrow already imported above)
    # Normalise ts to Z-suffix format for round-trip fidelity
    ts_col = pa.array([b["ts"].strftime("%Y-%m-%dT%H:%M:%SZ") for b in all_bars], type=pa.string())
    date_col = pa.array([b["date"] for b in all_bars], type=pa.string())
    session_col = pa.array([b["session"] for b in all_bars], type=pa.string())
    open_col = pa.array([b["open"] for b in all_bars], type=pa.float64())
    high_col = pa.array([b["high"] for b in all_bars], type=pa.float64())
    low_col = pa.array([b["low"] for b in all_bars], type=pa.float64())
    close_col = pa.array([b["close"] for b in all_bars], type=pa.float64())
    volume_col = pa.array([b["volume"] for b in all_bars], type=pa.int64())
    vw_col = pa.array([b["vw"] for b in all_bars], type=pa.float64())
    n_col = pa.array([b["n"] for b in all_bars], type=pa.int64())
    extras_col = pa.array([b["_extras"] for b in all_bars], type=pa.string())

    table = pa.table(
        {
            "ts": ts_col,
            "date": date_col,
            "session": session_col,
            "open": open_col,
            "high": high_col,
            "low": low_col,
            "close": close_col,
            "volume": volume_col,
            "vw": vw_col,
            "n": n_col,
            "_extras": extras_col,
        }
    )

    pp = _parquet_path(bars_dir, ticker)
    pp.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table,
        str(pp),
        compression=_COMPRESS,
        compression_level=_COMPRESS_LEVEL,
    )
    logger.debug("[bar_cache] wrote %d rows to %s", len(all_bars), pp)


def _ensure_cache(bars_dir: Path, ticker: str) -> bool:
    """Ensure Parquet cache exists and is fresh; rebuild if stale.

    Returns True if cache was already fresh (cache hit), False if it was
    rebuilt (cache miss).
    """
    source_files = _source_files_for_ticker(bars_dir, ticker)
    key = _cache_key(source_files)
    meta = _read_meta(bars_dir, ticker)
    pp = _parquet_path(bars_dir, ticker)

    if meta.get("key") == key and pp.is_file():
        return True  # cache hit

    t0 = time.perf_counter()
    _build_ticker_cache(bars_dir, ticker)
    _write_meta(bars_dir, ticker, key)
    elapsed = time.perf_counter() - t0
    logger.info(
        "[bar_cache] rebuilt ticker=%s in %.3fs (%d source files)",
        ticker.upper(),
        elapsed,
        len(source_files),
    )
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_bars(bars_dir: Path | str, ticker: str, date: str) -> list[dict]:
    """Drop-in replacement for replay_v511_full.load_day_bars.

    Returns the same list-of-dicts shape (including _dt for sort
    compatibility) as the raw JSONL path. Reads from Parquet cache,
    rebuilds cache file if stale or missing.

    Args:
        bars_dir: root directory containing per-date subdirectories.
        ticker:   ticker symbol (case-insensitive).
        date:     YYYY-MM-DD date string.

    Returns:
        List of bar dicts sorted by timestamp, each carrying at minimum:
        ts, open, high, low, close, iex_volume, session, _dt.
    """
    bars_dir = Path(bars_dir)
    ticker_up = ticker.upper()
    _ensure_cache(bars_dir, ticker_up)

    pp = _parquet_path(bars_dir, ticker_up)
    if not pp.is_file():
        return []

    import pyarrow.parquet as pq

    # Read only rows for the requested date \u2014 use predicate pushdown
    filters = [("date", "=", date)]
    try:
        table = pq.read_table(str(pp), filters=filters)
    except Exception as exc:
        logger.warning("[bar_cache] read error ticker=%s date=%s: %s", ticker_up, date, exc)
        return []

    rows: list[dict] = []
    ts_col = table.column("ts")
    date_col = table.column("date")
    session_col = table.column("session")
    open_col = table.column("open")
    high_col = table.column("high")
    low_col = table.column("low")
    close_col = table.column("close")
    volume_col = table.column("volume")
    vw_col = table.column("vw")
    n_col = table.column("n")
    extras_col = table.column("_extras")

    for i in range(table.num_rows):
        ts_str: str = ts_col[i].as_py()
        dt = _parse_ts(ts_str)
        if dt is None:
            continue
        # Normalise ts to the canonical Z-suffix format that source JSONL uses
        ts_canonical = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        extras: dict = {}
        try:
            extras = json.loads(extras_col[i].as_py() or "{}")
        except (json.JSONDecodeError, TypeError):
            pass

        bar: dict[str, Any] = {
            "ts": ts_canonical,
            "date": date_col[i].as_py(),
            "session": session_col[i].as_py(),
            "open": open_col[i].as_py(),
            "high": high_col[i].as_py(),
            "low": low_col[i].as_py(),
            "close": close_col[i].as_py(),
            # iex_volume alias: replay harness reads iex_volume
            "iex_volume": extras.get("iex_volume"),
            "volume": volume_col[i].as_py(),
            "vw": vw_col[i].as_py(),
            "n": n_col[i].as_py(),
            "_dt": dt,
        }
        # Restore passthrough extras
        for k, v in extras.items():
            if k not in bar:
                bar[k] = v

        rows.append(bar)

    # Already sorted by ts in the Parquet (written sorted), but confirm
    rows.sort(key=lambda b: b["_dt"])
    return rows


def build_all(bars_dir: Path | str) -> None:
    """Build Parquet caches for every ticker found under bars_dir.

    Suitable for:  python -m backtest.bar_cache build --bars-dir <dir>
    """
    bars_dir = Path(bars_dir)
    tickers: set[str] = set()
    if bars_dir.is_dir():
        for day_dir in bars_dir.iterdir():
            if not day_dir.is_dir() or day_dir.name.startswith("."):
                continue
            for f in day_dir.iterdir():
                if f.is_file() and f.suffix == ".jsonl":
                    tickers.add(f.stem.upper())
            pre_dir = day_dir / "premarket"
            if pre_dir.is_dir():
                for f in pre_dir.iterdir():
                    if f.is_file() and f.suffix == ".jsonl":
                        tickers.add(f.stem.upper())

    t0 = time.perf_counter()
    for tk in sorted(tickers):
        _ensure_cache(bars_dir, tk)
    elapsed = time.perf_counter() - t0
    logger.info("[bar_cache] build_all: %d tickers in %.2fs", len(tickers), elapsed)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="bar_cache CLI")
    sub = ap.add_subparsers(dest="cmd")
    build_cmd = sub.add_parser("build", help="Pre-build all Parquet caches")
    build_cmd.add_argument("--bars-dir", required=True, type=Path)
    args = ap.parse_args()
    if args.cmd == "build":
        build_all(args.bars_dir)
    else:
        ap.print_help()
