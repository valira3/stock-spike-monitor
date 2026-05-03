"""v6.9.0 \u2014 L2 indicator precompute cache for the backtest data layer.

Caches per-ticker per-date indicator values so sweep variants that
change only entry/exit logic (not indicator params) skip recomputation
entirely.

Cache layout
------------
  <bars_dir>/.indcache_v1/<TICKER>__<params_hash>.parquet

One Parquet per (ticker, indicator-set). The params_hash is a
SHA-256 over:
  (bar_cache_key, indicator_set_version, canonical params JSON)

Cached indicators (pure functions of bars + static params)
----------------------------------------------------------
  ATR(14), ATR(20)
  EMA9, EMA20, EMA50
  VWAP rolling (intraday, reset per session)
  Opening range high/low (9:30\u201309:35 ET, 9:30\u201310:00 ET)
  Premarket high/low/range
  Session boundary markers (pre\u2192RTH transition bar index)

Public API
----------
  get_indicators(bars_dir, ticker, date, indicators, params) -> dict
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backtest.bar_cache import _cache_key, _source_files_for_ticker, get_bars

logger = logging.getLogger("trade_genius")

# Increment this version string to invalidate ALL indicator caches
# whenever indicator computation logic changes.
INDICATOR_SET_VERSION = "v1"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _indcache_root(bars_dir: Path) -> Path:
    return bars_dir / ".indcache_v1"


def _ind_parquet_path(bars_dir: Path, ticker: str, params_hash: str) -> Path:
    return _indcache_root(bars_dir) / f"{ticker.upper()}__{params_hash}.parquet"


def _ind_meta_path(bars_dir: Path, ticker: str, params_hash: str) -> Path:
    return _indcache_root(bars_dir) / f"{ticker.upper()}__{params_hash}.meta.json"


def _compute_params_hash(
    bars_dir: Path,
    ticker: str,
    indicators: list[str],
    params: dict,
) -> str:
    """SHA-256 cache key: bar_cache_key + indicator set version + params."""
    source_files = _source_files_for_ticker(bars_dir, ticker)
    bar_key = _cache_key(source_files)
    canonical = json.dumps(
        {
            "bar_key": bar_key,
            "ind_version": INDICATOR_SET_VERSION,
            "indicators": sorted(indicators),
            "params": params,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _read_ind_meta(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def _write_ind_meta(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"key": key}, fh)


# ---------------------------------------------------------------------------
# Pure indicator computations (operate on list[dict] bars)
# ---------------------------------------------------------------------------

_ET = "America/New_York"


def _parse_ts(ts: str | None) -> datetime | None:
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


def _to_et(dt: datetime) -> datetime:
    from zoneinfo import ZoneInfo

    return dt.astimezone(ZoneInfo(_ET))


def _compute_ema(closes: list[float], period: int) -> list[float | None]:
    """Exponential moving average; returns aligned list (None until warm)."""
    out: list[float | None] = [None] * len(closes)
    if period <= 0 or not closes:
        return out
    k = 2.0 / (period + 1)
    ema = None
    for i, c in enumerate(closes):
        if c is None:
            out[i] = ema
            continue
        if ema is None:
            ema = c
        else:
            ema = c * k + ema * (1 - k)
        out[i] = ema
    return out


def _compute_atr(bars: list[dict], period: int) -> list[float | None]:
    """Average True Range (Wilder / RMA smoothing)."""
    n = len(bars)
    out: list[float | None] = [None] * n
    if period <= 0 or n == 0:
        return out
    trs: list[float] = []
    prev_close = None
    for b in bars:
        h = b.get("high") or 0.0
        lo = b.get("low") or 0.0
        c = b.get("close") or 0.0
        if prev_close is None:
            tr = h - lo
        else:
            tr = max(h - lo, abs(h - prev_close), abs(lo - prev_close))
        trs.append(tr)
        prev_close = c

    # Wilder smoothing
    atr = None
    for i, tr in enumerate(trs):
        if atr is None:
            if i >= period - 1:
                atr = sum(trs[i - period + 1 : i + 1]) / period
        else:
            atr = (atr * (period - 1) + tr) / period
        out[i] = atr
    return out


def _compute_vwap(bars: list[dict]) -> list[float | None]:
    """Rolling intraday VWAP; resets at session start (pre\u2192RTH)."""
    out: list[float | None] = [None] * len(bars)
    cum_pv = 0.0
    cum_vol = 0.0
    prev_session = None
    for i, b in enumerate(bars):
        sess = b.get("session", "rth")
        if sess != prev_session and prev_session is not None:
            # Session boundary \u2014 reset accumulators
            cum_pv = 0.0
            cum_vol = 0.0
        prev_session = sess
        h = b.get("high") or 0.0
        lo = b.get("low") or 0.0
        c = b.get("close") or 0.0
        typical = (h + lo + c) / 3.0
        vol = b.get("volume") or b.get("iex_volume") or 0
        cum_pv += typical * vol
        cum_vol += vol
        out[i] = cum_pv / cum_vol if cum_vol > 0 else None
    return out


def _compute_or(
    bars: list[dict],
    or_start_h: int,
    or_start_m: int,
    or_end_h: int,
    or_end_m: int,
) -> tuple[list[float | None], list[float | None]]:
    """Opening range high/low aligned to bar count.

    Returns (or_high_series, or_low_series) where each element holds the
    running OR high/low up to that bar, or None before the OR window.
    """
    n = len(bars)
    or_high_out: list[float | None] = [None] * n
    or_low_out: list[float | None] = [None] * n
    or_h: float | None = None
    or_l: float | None = None
    for i, b in enumerate(bars):
        dt = _parse_ts(b.get("ts"))
        if dt is None:
            continue
        et = _to_et(dt)
        in_window = (
            (et.hour, et.minute) >= (or_start_h, or_start_m)
            and (et.hour, et.minute) < (or_end_h, or_end_m)
        )
        h = b.get("high") or 0.0
        lo = b.get("low") or 0.0
        if in_window:
            if h > 0:
                or_h = max(or_h, h) if or_h is not None else h
            if lo > 0:
                or_l = min(or_l, lo) if or_l is not None else lo
        if or_h is not None:
            or_high_out[i] = or_h
        if or_l is not None:
            or_low_out[i] = or_l
    return or_high_out, or_low_out


def _compute_premarket(bars: list[dict]) -> tuple[list[float | None], list[float | None]]:
    """Premarket high/low aligned to bar count (pre-session bars only)."""
    n = len(bars)
    pm_h_out: list[float | None] = [None] * n
    pm_l_out: list[float | None] = [None] * n
    pm_h: float | None = None
    pm_l: float | None = None
    for i, b in enumerate(bars):
        sess = b.get("session", "rth")
        if sess == "pre":
            h = b.get("high") or 0.0
            lo = b.get("low") or 0.0
            if h > 0:
                pm_h = max(pm_h, h) if pm_h is not None else h
            if lo > 0:
                pm_l = min(pm_l, lo) if pm_l is not None else lo
        if pm_h is not None:
            pm_h_out[i] = pm_h
        if pm_l is not None:
            pm_l_out[i] = pm_l
    return pm_h_out, pm_l_out


def _compute_session_boundary(bars: list[dict]) -> list[int | None]:
    """Index of first RTH bar per date; None for pre-market bars."""
    n = len(bars)
    out: list[int | None] = [None] * n
    seen_date: dict[str, int] = {}
    for i, b in enumerate(bars):
        sess = b.get("session", "rth")
        date = b.get("date") or ""
        if sess == "rth" and date not in seen_date:
            seen_date[date] = i
        if date in seen_date:
            out[i] = seen_date[date]
    return out


# ---------------------------------------------------------------------------
# Indicator dispatch
# ---------------------------------------------------------------------------

# Default params \u2014 callers can override via the params dict
_DEFAULT_PARAMS: dict[str, Any] = {
    "atr_period_14": 14,
    "atr_period_20": 20,
    "ema_period_9": 9,
    "ema_period_20": 20,
    "ema_period_50": 50,
    "or_short_start": (9, 30),
    "or_short_end": (9, 35),
    "or_long_start": (9, 30),
    "or_long_end": (10, 0),
}


def _compute_all_indicators(
    bars: list[dict],
    indicators: list[str],
    params: dict,
) -> dict[str, list[Any]]:
    """Compute requested indicators over the full bar list.

    Args:
        bars:       list of bar dicts (all sessions, sorted by ts).
        indicators: list of indicator names to compute.
        params:     override params (merged with defaults).

    Returns:
        dict mapping indicator name \u2192 aligned list (length = len(bars)).
    """
    p = {**_DEFAULT_PARAMS, **params}
    closes = [b.get("close") or 0.0 for b in bars]
    result: dict[str, list[Any]] = {}

    if "atr14" in indicators:
        result["atr14"] = _compute_atr(bars, int(p.get("atr_period_14", 14)))
    if "atr20" in indicators:
        result["atr20"] = _compute_atr(bars, int(p.get("atr_period_20", 20)))
    if "ema9" in indicators:
        result["ema9"] = _compute_ema(closes, int(p.get("ema_period_9", 9)))
    if "ema20" in indicators:
        result["ema20"] = _compute_ema(closes, int(p.get("ema_period_20", 20)))
    if "ema50" in indicators:
        result["ema50"] = _compute_ema(closes, int(p.get("ema_period_50", 50)))
    if "vwap" in indicators:
        result["vwap"] = _compute_vwap(bars)
    if "or5_high" in indicators or "or5_low" in indicators:
        sh, sm = p.get("or_short_start", (9, 30))
        eh, em = p.get("or_short_end", (9, 35))
        or5_h, or5_l = _compute_or(bars, sh, sm, eh, em)
        if "or5_high" in indicators:
            result["or5_high"] = or5_h
        if "or5_low" in indicators:
            result["or5_low"] = or5_l
    if "or30_high" in indicators or "or30_low" in indicators:
        sh, sm = p.get("or_long_start", (9, 30))
        eh, em = p.get("or_long_end", (10, 0))
        or30_h, or30_l = _compute_or(bars, sh, sm, eh, em)
        if "or30_high" in indicators:
            result["or30_high"] = or30_h
        if "or30_low" in indicators:
            result["or30_low"] = or30_l
    if "pm_high" in indicators or "pm_low" in indicators or "pm_range" in indicators:
        pm_h, pm_l = _compute_premarket(bars)
        if "pm_high" in indicators:
            result["pm_high"] = pm_h
        if "pm_low" in indicators:
            result["pm_low"] = pm_l
        if "pm_range" in indicators:
            result["pm_range"] = [
                (h - l) if (h is not None and l is not None) else None
                for h, l in zip(pm_h, pm_l)
            ]
    if "session_boundary" in indicators:
        result["session_boundary"] = _compute_session_boundary(bars)

    return result


# ---------------------------------------------------------------------------
# Cache build + read
# ---------------------------------------------------------------------------


def _build_indicator_cache(
    bars_dir: Path,
    ticker: str,
    date: str,
    indicators: list[str],
    params: dict,
    params_hash: str,
) -> dict[str, list[Any]]:
    """Compute indicators from bars and persist to Parquet."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    bars = get_bars(bars_dir, ticker, date)
    if not bars:
        return {ind: [] for ind in indicators}

    computed = _compute_all_indicators(bars, indicators, params)

    # Build table: ts + one column per indicator
    ts_vals = [b.get("ts") for b in bars]
    col_data: dict[str, pa.Array] = {
        "ts": pa.array(ts_vals, type=pa.string()),
        "date": pa.array([date] * len(bars), type=pa.string()),
    }
    for ind in sorted(set(computed.keys())):
        vals = computed[ind]
        # Use float64 for numeric; int64 for session_boundary; None-safe
        if ind == "session_boundary":
            col_data[ind] = pa.array(
                [int(v) if v is not None else None for v in vals],
                type=pa.int64(),
            )
        else:
            col_data[ind] = pa.array(
                [float(v) if v is not None else None for v in vals],
                type=pa.float64(),
            )

    table = pa.table(col_data)
    pp = _ind_parquet_path(bars_dir, ticker, params_hash)
    pp.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table,
        str(pp),
        compression="zstd",
        compression_level=3,
    )
    logger.debug(
        "[ind_cache] wrote ticker=%s date=%s params_hash=%s rows=%d",
        ticker.upper(),
        date,
        params_hash,
        len(bars),
    )

    # Filter to requested date (entire ticker cache is stored per-file)
    return {ind: computed.get(ind, [None] * len(bars)) for ind in indicators}


def _read_indicator_cache(
    bars_dir: Path,
    ticker: str,
    date: str,
    indicators: list[str],
    params_hash: str,
) -> dict[str, list[Any]] | None:
    """Read cached indicators for (ticker, date, params_hash).

    Returns None if file missing or date has no rows.
    """
    import pyarrow.parquet as pq

    pp = _ind_parquet_path(bars_dir, ticker, params_hash)
    if not pp.is_file():
        return None

    try:
        table = pq.read_table(str(pp), filters=[("date", "=", date)])
    except Exception as exc:
        logger.warning("[ind_cache] read error %s %s: %s", ticker, date, exc)
        return None

    if table.num_rows == 0:
        return None

    result: dict[str, list[Any]] = {}
    for ind in indicators:
        if ind in table.schema.names:
            col = table.column(ind)
            result[ind] = [col[i].as_py() for i in range(table.num_rows)]
        else:
            result[ind] = [None] * table.num_rows
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_indicators(
    bars_dir: Path | str,
    ticker: str,
    date: str,
    indicators: list[str],
    params: dict | None = None,
) -> dict[str, list[float]]:
    """Return aligned per-bar indicator values for (ticker, date).

    Length matches bar count for that ticker/date. Cache miss triggers
    computation + persistence; subsequent calls are Parquet reads.

    Args:
        bars_dir:   root bars directory.
        ticker:     ticker symbol (case-insensitive).
        date:       YYYY-MM-DD date string.
        indicators: names from the supported set (atr14, atr20, ema9,
                    ema20, ema50, vwap, or5_high, or5_low, or30_high,
                    or30_low, pm_high, pm_low, pm_range,
                    session_boundary).
        params:     optional override params dict.

    Returns:
        dict[indicator_name, list[value]] \u2014 aligned to bar order.
    """
    bars_dir = Path(bars_dir)
    ticker_up = ticker.upper()
    params = params or {}
    params_hash = _compute_params_hash(bars_dir, ticker_up, indicators, params)

    # Try cache hit first
    cached = _read_indicator_cache(bars_dir, ticker_up, date, indicators, params_hash)
    if cached is not None:
        return cached

    # Cache miss \u2014 compute + persist
    t0 = time.perf_counter()
    result = _build_indicator_cache(
        bars_dir, ticker_up, date, indicators, params, params_hash
    )
    elapsed = time.perf_counter() - t0
    logger.debug(
        "[ind_cache] computed ticker=%s date=%s in %.3fs",
        ticker_up,
        date,
        elapsed,
    )
    return result
