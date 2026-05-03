"""v6.9.6 -- L1 Parquet bar cache: per-day files + in-process LRU.

Replaces the v6.9.0 single-file-per-ticker layout that forced a full
84-day Parquet scan for every 1-day request (12-15x regression vs JSONL).

v6.9.5 change: _cache_root() respects SSM_BAR_CACHE_DIR env var so sweep
workers can write cache files to a writable path even when bars_dir is a
read-only canonical SIP directory.

v6.9.6 change: _build_ticker_cache and _ensure_cache are confirmed to route
all cache path construction through _cache_root(). No write touches bars_dir
when SSM_BAR_CACHE_DIR is set.

Cache layout (v2)
-----------------
  <cache_root>/<TICKER>/<YYYY-MM-DD>.parquet   (cache_root = SSM_BAR_CACHE_DIR or bars_dir/.cache_v2)
  <cache_root>/<TICKER>.meta.json

Each Parquet contains ONLY the bars for that (ticker, date) pair
(pre-market + RTH combined, sorted by ts). A single-day read opens
exactly one ~30 KB file instead of a 2.1 MB all-dates file.

The .meta.json stores the SHA-256 cache key derived from all source
JSONL files for that ticker (path + mtime_ns + size). Any change to
a source file invalidates the whole ticker and triggers a rebuild of
all per-day Parquets for that ticker.

LRU (L3 in-process cache)
--------------------------
get_bars() is wrapped with functools.lru_cache(maxsize=4096).
Cache key: (str(bars_dir), ticker, date). After the first disk read
within a process, every subsequent call for the same (ticker, date)
is a ~microsecond dict lookup. Clear with get_bars.cache_clear().

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

import functools
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
# Cache version sentinel \u2014 bump to invalidate all prior caches
# ---------------------------------------------------------------------------

_CACHE_VERSION = "v2"
_CACHE_DIR_NAME = ".cache_v2"

# Process-level set of (bars_dir_str, ticker) pairs whose cache freshness
# has already been verified in this process. Eliminates repeated os.stat()
# calls on every get_bars() invocation after the first freshness check.
_CACHE_VERIFIED: set[tuple[str, str]] = set()


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
    override = os.environ.get("SSM_BAR_CACHE_DIR")
    if override:
        return Path(override)
    return bars_dir / _CACHE_DIR_NAME


def _parquet_path(bars_dir: Path, ticker: str, date: str) -> Path:
    """Per-day Parquet path: .cache_v2/<TICKER>/<YYYY-MM-DD>.parquet"""
    return _cache_root(bars_dir) / ticker.upper() / f"{date}.parquet"


def _meta_path(bars_dir: Path, ticker: str) -> Path:
    return _cache_root(bars_dir) / f"{ticker.upper()}.meta.json"


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
        json.dump({"key": key, "cache_version": _CACHE_VERSION}, fh)


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


def _bars_to_parquet_row(bar: dict, ts: datetime) -> dict[str, Any]:
    """Convert a normalised bar dict to a row ready for Parquet write."""
    b_ts = bar.get("ts")
    volume = bar.get("iex_volume") or bar.get("volume") or 0
    vw = bar.get("bar_vwap") or bar.get("vw") or 0.0
    n = bar.get("trade_count") or bar.get("n") or 0
    return {
        "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": ts.date().isoformat(),
        "session": _infer_session(b_ts, bar),
        "open": float(bar.get("open") or 0.0),
        "high": float(bar.get("high") or 0.0),
        "low": float(bar.get("low") or 0.0),
        "close": float(bar.get("close") or 0.0),
        "volume": int(volume),
        "vw": float(vw),
        "n": int(n),
        "_extras": json.dumps({k: bar.get(k) for k in _PASSTHROUGH_COLS}),
        "_dt": ts,
    }


def _build_ticker_cache(bars_dir: Path, ticker: str) -> None:
    """Parse all JSONL source files for ticker and write per-day Parquets.

    Layout: .cache_v2/<TICKER>/<YYYY-MM-DD>.parquet
    Each file contains only bars for that date (pre + RTH combined).
    This guarantees single-file reads for every get_bars() call.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    ticker_up = ticker.upper()
    source_files = _source_files_for_ticker(bars_dir, ticker)

    # Group bars by date
    by_date: dict[str, list[dict[str, Any]]] = {}
    for path in source_files:
        for bar in _load_jsonl(path):
            ts_str = bar.get("ts")
            dt = _parse_ts(ts_str)
            if dt is None:
                continue
            date_str = dt.date().isoformat()
            row = _bars_to_parquet_row(bar, dt)
            by_date.setdefault(date_str, []).append(row)

    if not by_date:
        logger.warning("[bar_cache] no bars found for ticker=%s", ticker_up)

    ticker_dir = _cache_root(bars_dir) / ticker_up
    ticker_dir.mkdir(parents=True, exist_ok=True)

    for date_str, rows in by_date.items():
        rows.sort(key=lambda r: r["_dt"])

        ts_col = pa.array([r["ts"] for r in rows], type=pa.string())
        date_col = pa.array([r["date"] for r in rows], type=pa.string())
        session_col = pa.array([r["session"] for r in rows], type=pa.string())
        open_col = pa.array([r["open"] for r in rows], type=pa.float64())
        high_col = pa.array([r["high"] for r in rows], type=pa.float64())
        low_col = pa.array([r["low"] for r in rows], type=pa.float64())
        close_col = pa.array([r["close"] for r in rows], type=pa.float64())
        volume_col = pa.array([r["volume"] for r in rows], type=pa.int64())
        vw_col = pa.array([r["vw"] for r in rows], type=pa.float64())
        n_col = pa.array([r["n"] for r in rows], type=pa.int64())
        extras_col = pa.array([r["_extras"] for r in rows], type=pa.string())

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

        pp = ticker_dir / f"{date_str}.parquet"
        pq.write_table(
            table,
            str(pp),
            compression=_COMPRESS,
            compression_level=_COMPRESS_LEVEL,
        )

    logger.debug(
        "[bar_cache] wrote %d per-day Parquets for ticker=%s",
        len(by_date),
        ticker_up,
    )


def _ensure_cache(bars_dir: Path, ticker: str) -> bool:
    """Ensure per-day Parquet caches exist and are fresh; rebuild if stale.

    Returns True if cache was already fresh (cache hit), False if it was
    rebuilt (cache miss).

    After the first successful freshness check for a (bars_dir, ticker) pair
    within a process, subsequent calls skip the os.stat() loop entirely by
    consulting _CACHE_VERIFIED. This eliminates the ~2.5 ms overhead that
    made every get_bars() call as slow as a fresh disk stat even when the LRU
    already held the bars in memory.
    """
    ck = (str(bars_dir), ticker)
    if ck in _CACHE_VERIFIED:
        return True  # already verified fresh in this process

    source_files = _source_files_for_ticker(bars_dir, ticker)
    key = _cache_key(source_files)
    meta = _read_meta(bars_dir, ticker)

    # Hit: key matches AND meta indicates same cache version
    if (
        meta.get("key") == key
        and meta.get("cache_version") == _CACHE_VERSION
    ):
        _CACHE_VERIFIED.add(ck)
        return True  # cache hit

    # Invalidate process-level verified set so next call re-checks
    _CACHE_VERIFIED.discard((str(bars_dir), ticker))
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


def _get_bars_uncached(bars_dir: Path, ticker_up: str, date: str) -> list[dict]:
    """Read a single per-day Parquet and return list-of-dicts.

    This is the raw disk path; wrapped by get_bars() which adds LRU.
    """
    import pyarrow.parquet as pq

    pp = _parquet_path(bars_dir, ticker_up, date)
    if not pp.is_file():
        return []

    try:
        table = pq.read_table(str(pp))
    except Exception as exc:
        logger.warning(
            "[bar_cache] read error ticker=%s date=%s: %s", ticker_up, date, exc
        )
        return []

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

    rows: list[dict] = []
    for i in range(table.num_rows):
        ts_str: str = ts_col[i].as_py()
        dt = _parse_ts(ts_str)
        if dt is None:
            continue
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

    rows.sort(key=lambda b: b["_dt"])
    return rows


@functools.lru_cache(maxsize=4096)
def _lru_read_bars(bars_dir_str: str, ticker_up: str, date: str) -> tuple[dict, ...]:
    """LRU-cached disk read for (bars_dir, ticker, date).

    Returns an immutable tuple of bar dicts so the result is hashable
    and safe to share across callers. Cache key is
    (str(bars_dir), ticker, date) \u2014 all hashable scalars.
    """
    rows = _get_bars_uncached(Path(bars_dir_str), ticker_up, date)
    # Convert to tuple for hashability (lru_cache requires hashable return
    # only when the function itself is the key, not the return; tuple is
    # fine here and lets callers convert back to list cheaply).
    return tuple(rows)


def get_bars(bars_dir: "Path | str", ticker: str, date: str) -> list[dict]:
    """Drop-in replacement for replay_v511_full.load_day_bars.

    Returns the same list-of-dicts shape (including _dt for sort
    compatibility) as the raw JSONL path. Reads from the per-day Parquet
    cache (.cache_v2/<TICKER>/<YYYY-MM-DD>.parquet), rebuilding if stale
    or missing.

    After the first call for a (bars_dir, ticker, date) triple within a
    process, subsequent calls return from an in-process LRU (no disk I/O).

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

    # _lru_read_bars caches by (str(bars_dir), ticker, date)
    bars_tuple = _lru_read_bars(str(bars_dir), ticker_up, date)
    return list(bars_tuple)


def build_all(bars_dir: "Path | str") -> None:
    """Build per-day Parquet caches for every ticker found under bars_dir.

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
    build_cmd = sub.add_parser("build", help="Pre-build all per-day Parquet caches")
    build_cmd.add_argument("--bars-dir", required=True, type=Path)
    args = ap.parse_args()
    if args.cmd == "build":
        build_all(args.bars_dir)
    else:
        ap.print_help()
