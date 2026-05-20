"""Prior-day SPY return loader for the v9.0.0 regime-skip gate.

The gate (`orb.day_gates.evaluate_day` with
`skip_prior_spy_ret_lt_bps` set) consumes the prior session's
SPY close-to-close return in bps to decide whether to block the
day. R12 research showed the strategy bleeds ~$208/day on days
where prior SPY return was in the (-1.0%, -0.5%) band; the gate
filters those out.

Three data sources, tried in order:
  1. `/data/bars/<YYYY-MM-DD>/SPY.jsonl` (production bar archive
     written by bar_archive.py). Used in live.
  2. `data/external/spy-daily.csv` (datahub-style CSV). Fallback
     for backtests + local dev when the bar archive is missing.
  3. v9.1.3: Alpaca historical daily bars. Self-heal fallback for
     fresh deploys / wiped volumes / missed overnight writes -- no
     operator CSV maintenance needed. Reuses the standard
     `VAL_ALPACA_PAPER_KEY` -> `GENE_ALPACA_PAPER_KEY` credential
     pool already wired for other data calls. Result is cached
     in-process per `decision_date` so repeated calls in the same
     session are free.

Fail behavior: returns None when all three sources are unavailable.
Callers (engine.start_new_session via day_gates.evaluate_day)
should treat None as "fail-open" unless
`fail_closed_on_missing_spy=True` is set in DayGateConfig.

Look-ahead audit (rule #7b): all three sources expose only closes
at or before the decision date. The function explicitly walks
BACKWARD from `decision_date`; same-day or future data is never
consulted.
"""

from __future__ import annotations

import csv
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


import os as _os

# Honor BARS_BASE_DIR for sim / non-Railway environments (mirrors
# engine/scan._load_eod_prior_closes and bar_archive's DEFAULT_BASE_DIR).
# Pre-2026-05-20 this was hardcoded /data/bars, so the SPY-D1 regime
# gate fail-opened in any environment where the volume wasn't mounted.
DEFAULT_BAR_ARCHIVE_ROOT = _os.environ.get("BARS_BASE_DIR", "/data/bars")
DEFAULT_CSV_PATH = "data/external/spy-daily.csv"

# v9.1.3 -- module-level cache keyed on decision_date so a single
# session reset triggers at most one Alpaca REST call. Reset on
# process restart; that's fine since each new deploy gets a fresh
# session anyway.
_alpaca_cache: dict[str, Optional[tuple[str, float, str, float]]] = {}


def _last_rth_close_from_jsonl(jsonl_path: Path) -> Optional[float]:
    """Scan a SPY.jsonl bar file, return the last RTH bar's close
    (et_bucket in [0930, 1559]). Returns None on missing/empty file.
    """
    if not jsonl_path.is_file():
        return None
    last_close: Optional[float] = None
    try:
        with jsonl_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    bar = json.loads(line)
                except json.JSONDecodeError:
                    continue
                bucket = bar.get("et_bucket")
                # Accept string ("0930") or int (570 minute-of-day).
                if isinstance(bucket, str):
                    try:
                        b_int = int(bucket)
                    except ValueError:
                        continue
                    if not (930 <= b_int <= 1559):
                        continue
                elif isinstance(bucket, int):
                    if not (9 * 60 + 30 <= bucket <= 15 * 60 + 59):
                        continue
                else:
                    continue
                close = bar.get("close")
                if isinstance(close, (int, float)) and close > 0:
                    last_close = float(close)
    except OSError:
        return None
    return last_close


def _prior_close_from_bar_archive(
    bar_archive_root: str,
    decision_date: str,
    *,
    max_lookback: int = 10,
) -> Optional[tuple[str, float]]:
    """Walk backward from `decision_date` to find the most recent
    SPY.jsonl with a valid RTH last-close. Returns (date_iso, close)
    or None if no archive file is found within `max_lookback` days.
    """
    try:
        dt = datetime.strptime(decision_date, "%Y-%m-%d")
    except ValueError:
        return None
    root = Path(bar_archive_root)
    for offset in range(1, max_lookback + 1):
        cand = (dt - timedelta(days=offset)).strftime("%Y-%m-%d")
        close = _last_rth_close_from_jsonl(root / cand / "SPY.jsonl")
        if close is not None:
            return (cand, close)
    return None


def _load_spy_csv_closes(csv_path: str) -> dict[str, float]:
    """Mirror of orb_vix_loader.load_vix_closes for SPY.
    Returns {YYYY-MM-DD: close}. Accepts datahub MM/DD/YYYY format and
    yfinance-style YYYY-MM-DD.
    """
    path = Path(csv_path)
    if not path.is_file():
        return {}
    out: dict[str, float] = {}
    try:
        with path.open() as fh:
            for row in csv.DictReader(fh):
                d = row.get("DATE") or row.get("Date") or row.get("date")
                c = row.get("CLOSE") or row.get("Close") or row.get("close")
                if not d or not c:
                    continue
                try:
                    try:
                        iso = datetime.strptime(d, "%m/%d/%Y").strftime("%Y-%m-%d")
                    except ValueError:
                        iso = datetime.strptime(d, "%Y-%m-%d").strftime("%Y-%m-%d")
                    out[iso] = float(c)
                except (ValueError, TypeError):
                    continue
    except OSError:
        return {}
    return out


def _walk_back_two_closes(
    closes: dict[str, float],
    decision_date: str,
    *,
    max_lookback: int = 10,
) -> Optional[tuple[str, float, str, float]]:
    """Find the two most recent CSV closes strictly before `decision_date`.
    Returns (d1_date, d1_close, d2_date, d2_close) where d1 is more recent
    than d2. Returns None if fewer than 2 prior closes available.
    """
    try:
        dt = datetime.strptime(decision_date, "%Y-%m-%d")
    except ValueError:
        return None
    found: list[tuple[str, float]] = []
    for offset in range(1, max_lookback + 1):
        cand = (dt - timedelta(days=offset)).strftime("%Y-%m-%d")
        if cand in closes:
            found.append((cand, closes[cand]))
            if len(found) == 2:
                d1_date, d1_close = found[0]
                d2_date, d2_close = found[1]
                return (d1_date, d1_close, d2_date, d2_close)
    return None


def _alpaca_credentials() -> tuple[Optional[str], Optional[str]]:
    """Return (key, secret) using the standard pool fallback chain.
    Matches trade_genius.py's data-call lookup so we don't introduce
    a new env var. Returns (None, None) if no credentials available.
    """
    key = (
        (os.getenv("VAL_ALPACA_PAPER_KEY") or "").strip()
        or (os.getenv("GENE_ALPACA_PAPER_KEY") or "").strip()
    )
    secret = (
        (os.getenv("VAL_ALPACA_PAPER_SECRET") or "").strip()
        or (os.getenv("GENE_ALPACA_PAPER_SECRET") or "").strip()
    )
    if not key or not secret:
        return (None, None)
    return (key, secret)


def _prior_two_closes_from_alpaca(
    decision_date: str,
    *,
    max_lookback: int = 14,
) -> Optional[tuple[str, float, str, float]]:
    """v9.1.3 -- third-tier fallback. Fetches the most recent two daily
    SPY closes strictly before `decision_date` via Alpaca historical
    bars. Returns (d1_date, d1_close, d2_date, d2_close) or None when
    credentials are missing, the SDK is unavailable, the REST call
    fails, or fewer than 2 closes come back. Cached per
    `decision_date` to keep this to one network call per session.

    `max_lookback` is in calendar days; 14 covers the longest holiday
    weekend (Thanksgiving / Christmas) with margin.
    """
    if decision_date in _alpaca_cache:
        return _alpaca_cache[decision_date]
    key, secret = _alpaca_credentials()
    if key is None or secret is None:
        logger.debug(
            "[V900-SPY-LOADER] alpaca: no credentials in env, skipping fallback"
        )
        _alpaca_cache[decision_date] = None
        return None
    try:
        dt = datetime.strptime(decision_date, "%Y-%m-%d")
    except ValueError:
        _alpaca_cache[decision_date] = None
        return None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
    except ImportError as e:
        logger.debug("[V900-SPY-LOADER] alpaca SDK not importable: %s", e)
        _alpaca_cache[decision_date] = None
        return None
    start = (dt - timedelta(days=max_lookback)).strftime("%Y-%m-%d")
    # End is decision_date - 1 day so we never read same-day or
    # future data (look-ahead rule #7b).
    end = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        client = StockHistoricalDataClient(key, secret)
        req = StockBarsRequest(
            symbol_or_symbols="SPY",
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        resp = client.get_stock_bars(req)
    except Exception as e:
        logger.warning("[V900-SPY-LOADER] alpaca REST failed: %s", e)
        _alpaca_cache[decision_date] = None
        return None
    raw = resp.data.get("SPY", []) if hasattr(resp, "data") else []
    # alpaca-py returns bars in chronological order; we want the two
    # most recent.
    closes: list[tuple[str, float]] = []
    for b in raw:
        ts = getattr(b, "timestamp", None)
        close = getattr(b, "close", None)
        if ts is None or close is None:
            continue
        try:
            iso = ts.strftime("%Y-%m-%d")
        except Exception:
            continue
        # Defensive: even with end=decision_date-1, drop any bar at
        # or after decision_date.
        if iso >= decision_date:
            continue
        if close > 0:
            closes.append((iso, float(close)))
    if len(closes) < 2:
        logger.warning(
            "[V900-SPY-LOADER] alpaca: only %d valid closes returned for %s",
            len(closes),
            decision_date,
        )
        _alpaca_cache[decision_date] = None
        return None
    d1_date, d1_close = closes[-1]
    d2_date, d2_close = closes[-2]
    result = (d1_date, d1_close, d2_date, d2_close)
    _alpaca_cache[decision_date] = result
    return result


def prior_spy_return_bps(
    decision_date: str,
    *,
    bar_archive_root: str = DEFAULT_BAR_ARCHIVE_ROOT,
    csv_path: str = DEFAULT_CSV_PATH,
    max_lookback: int = 10,
) -> Optional[float]:
    """Return prior-session SPY close-to-close return in bps.

    Computed as (SPY[D-1] - SPY[D-2]) / SPY[D-2] * 10000 where D-1 and
    D-2 are the two most recent trading days strictly before
    `decision_date`. Returns None when fewer than 2 prior closes are
    available from any source.

    Source priority: bar archive (live) -> CSV (backtest) -> Alpaca
    REST (self-heal fallback for fresh deploys / wiped volumes).
    """
    # Try bar archive first.
    d1 = _prior_close_from_bar_archive(
        bar_archive_root,
        decision_date,
        max_lookback=max_lookback,
    )
    if d1 is not None:
        d1_date, d1_close = d1
        # Walk back from d1 to find d2.
        d2 = _prior_close_from_bar_archive(
            bar_archive_root,
            d1_date,
            max_lookback=max_lookback,
        )
        if d2 is not None:
            d2_date, d2_close = d2
            if d2_close > 0:
                bps = (d1_close - d2_close) / d2_close * 10000.0
                logger.debug(
                    "[V900-SPY-LOADER] bar archive: D-1 %s close=%.2f, "
                    "D-2 %s close=%.2f -> %.1fbps",
                    d1_date,
                    d1_close,
                    d2_date,
                    d2_close,
                    bps,
                )
                return bps
    # Fallback to CSV.
    closes = _load_spy_csv_closes(csv_path)
    if closes:
        pair = _walk_back_two_closes(closes, decision_date, max_lookback=max_lookback)
        if pair is not None:
            d1_date, d1_close, d2_date, d2_close = pair
            if d2_close > 0:
                bps = (d1_close - d2_close) / d2_close * 10000.0
                logger.debug(
                    "[V900-SPY-LOADER] CSV fallback: D-1 %s close=%.2f, "
                    "D-2 %s close=%.2f -> %.1fbps",
                    d1_date,
                    d1_close,
                    d2_date,
                    d2_close,
                    bps,
                )
                return bps
    # v9.1.3 -- third-tier Alpaca REST fallback. Self-heals on fresh
    # deploys / wiped volumes / overnight-restart misses.
    pair = _prior_two_closes_from_alpaca(decision_date)
    if pair is not None:
        d1_date, d1_close, d2_date, d2_close = pair
        if d2_close > 0:
            bps = (d1_close - d2_close) / d2_close * 10000.0
            logger.info(
                "[V900-SPY-LOADER] alpaca rebuild: D-1 %s close=%.2f, "
                "D-2 %s close=%.2f -> %.1fbps",
                d1_date,
                d1_close,
                d2_date,
                d2_close,
                bps,
            )
            return bps
    logger.warning(
        "[V900-SPY-LOADER] no prior SPY close found for %s in any of "
        "bar archive (%s), CSV (%s), or alpaca",
        decision_date,
        bar_archive_root,
        csv_path,
    )
    return None
