"""v6.16.1 \u2014 earnings_watcher.data_sources: live data access layer.

Provides:
  - get_earnings_calendar(date_iso)     -> FMP earnings calendar for a date
  - fetch_minute_bars(ticker, start_utc, end_utc) -> Alpaca 1-min bars
  - get_account_equity()               -> Alpaca paper account equity
  - get_today_earnings_universe()      -> (bmo_tickers, amc_tickers) filtered by cap+vol

Hard boundary: MUST NOT import from eye_of_tiger or trade_genius.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("earnings_watcher")

# ---------------------------------------------------------------------------
# Cache path (falls back to /tmp if /data not writable)
# ---------------------------------------------------------------------------

_CACHE_BASE_CANDIDATES = ["/data/earnings_watcher", "/tmp/earnings_watcher"]


def _cache_dir() -> Path:
    for candidate in _CACHE_BASE_CANDIDATES:
        p = Path(candidate)
        try:
            p.mkdir(parents=True, exist_ok=True)
            # Probe writability
            probe = p / ".write_probe"
            probe.write_text("ok")
            probe.unlink()
            return p
        except OSError:
            continue
    return Path("/tmp/earnings_watcher")


_UNIVERSE_CACHE_FILE = "universe_cache.json"
_UNIVERSE_CACHE_TTL_S = 86_400  # 24 h


# ---------------------------------------------------------------------------
# FMP helpers
# ---------------------------------------------------------------------------

def _fmp_key() -> str:
    key = os.getenv("FMP_API_KEY", "")
    if not key:
        raise EnvironmentError("[EW-DATA] FMP_API_KEY env var not set")
    return key


def get_earnings_calendar(date_iso: str) -> List[Dict[str, Any]]:
    """Pull FMP /stable/earnings-calendar for a single date.

    Parameters
    ----------
    date_iso : str
        Date in YYYY-MM-DD format.

    Returns
    -------
    List of dicts with keys: ticker, date, time (BMO/AMC),
    epsActual, epsEstimated, revActual, revEstimated.
    Returns empty list on any error.
    """
    try:
        key = _fmp_key()
    except EnvironmentError as exc:
        logger.warning("[EW-DATA] get_earnings_calendar skipped: %s", exc)
        return []

    url = (
        "https://financialmodelingprep.com/stable/earnings-calendar"
        f"?from={date_iso}&to={date_iso}&apikey={key}"
    )
    logger.info("[EW-DATA] get_earnings_calendar date=%s", date_iso)
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as exc:
        logger.warning("[EW-DATA] FMP earnings-calendar error: %s", exc)
        return []

    out: List[Dict[str, Any]] = []
    for item in (raw if isinstance(raw, list) else []):
        out.append(
            {
                "ticker": item.get("symbol", ""),
                "date": item.get("date", date_iso),
                "time": item.get("time", ""),         # "BMO" | "AMC" | ""
                "epsActual": item.get("epsActual"),
                "epsEstimated": item.get("epsEstimated"),
                "revActual": item.get("revenueActual"),
                "revEstimated": item.get("revenueEstimated"),
            }
        )

    logger.info("[EW-DATA] get_earnings_calendar date=%s returned %d events",
                date_iso, len(out))
    return out


# ---------------------------------------------------------------------------
# Alpaca bar fetch
# ---------------------------------------------------------------------------

def _alpaca_credentials() -> Tuple[str, str]:
    key = os.getenv("VAL_ALPACA_PAPER_KEY", "")
    secret = os.getenv("VAL_ALPACA_PAPER_SECRET", "")
    if not key or not secret:
        raise EnvironmentError("[EW-DATA] VAL_ALPACA_PAPER_KEY/SECRET not set")
    return key, secret


def fetch_minute_bars(
    ticker: str,
    start_utc: datetime,
    end_utc: datetime,
) -> List[Dict[str, Any]]:
    """Fetch 1-min bars from Alpaca for ticker in [start_utc, end_utc].

    Tries SIP feed first, falls back to IEX on any error.
    Returns list of dicts: {timestamp, open, high, low, close, volume}.
    timestamp is ISO 8601 string (as decision_engine.py expects).
    Returns empty list on credential or API error.
    """
    try:
        key, secret = _alpaca_credentials()
    except EnvironmentError as exc:
        logger.warning("[EW-DATA] fetch_minute_bars skipped: %s", exc)
        return []

    from alpaca.data.historical import StockHistoricalDataClient  # type: ignore
    from alpaca.data.requests import StockBarsRequest  # type: ignore
    from alpaca.data.timeframe import TimeFrame  # type: ignore
    from alpaca.data.enums import DataFeed  # type: ignore

    logger.info("[EW-DATA] fetch_minute_bars ticker=%s start=%s end=%s",
                ticker, start_utc.isoformat(), end_utc.isoformat())

    def _pull(feed: str) -> List[Dict[str, Any]]:
        client = StockHistoricalDataClient(key, secret)
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Minute,
            start=start_utc,
            end=end_utc,
            feed=feed,
        )
        bars_df = client.get_stock_bars(req)
        # bars_df is a BarSet; iterate via .df or direct
        try:
            df = bars_df.df
        except AttributeError:
            df = bars_df[ticker].df if hasattr(bars_df, "__getitem__") else None

        if df is None or df.empty:
            return []

        result: List[Dict[str, Any]] = []
        for ts, row in df.iterrows():
            # ts may be a DatetimeTZDtype index or (symbol, ts) MultiIndex
            if hasattr(ts, "__len__") and len(ts) == 2:
                ts = ts[1]
            # Normalise to ISO 8601 string
            if hasattr(ts, "isoformat"):
                ts_str = ts.isoformat()
            else:
                ts_str = str(ts)
            result.append(
                {
                    "timestamp": ts_str,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(row["volume"]),
                }
            )
        return result

    for feed in ("sip", "iex"):
        try:
            bars = _pull(feed)
            logger.info("[EW-DATA] fetch_minute_bars ticker=%s feed=%s bars=%d",
                        ticker, feed, len(bars))
            return bars
        except Exception as exc:
            logger.warning("[EW-DATA] fetch_minute_bars ticker=%s feed=%s error: %s",
                           ticker, feed, exc)

    return []


# ---------------------------------------------------------------------------
# Account equity
# ---------------------------------------------------------------------------

def get_account_equity() -> Optional[float]:
    """Return Alpaca paper account equity as float, or None on any error."""
    try:
        key, secret = _alpaca_credentials()
    except EnvironmentError as exc:
        logger.warning("[EW-DATA] get_account_equity skipped: %s", exc)
        return None

    try:
        from alpaca.trading.client import TradingClient  # type: ignore
        client = TradingClient(key, secret, paper=True)
        acct = client.get_account()
        equity = float(acct.equity)
        logger.info("[EW-DATA] get_account_equity equity=%.2f", equity)
        return equity
    except Exception as exc:
        logger.warning("[EW-DATA] get_account_equity error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Universe filter
# ---------------------------------------------------------------------------

def _load_universe_cache() -> Dict[str, Any]:
    p = _cache_dir() / _UNIVERSE_CACHE_FILE
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        # Expire if older than 24 h
        saved_at = data.get("_saved_at", 0)
        if time.time() - saved_at > _UNIVERSE_CACHE_TTL_S:
            return {}
        return data
    except Exception:
        return {}


def _save_universe_cache(data: Dict[str, Any]) -> None:
    data["_saved_at"] = time.time()
    p = _cache_dir() / _UNIVERSE_CACHE_FILE
    tmp = str(p) + ".tmp"
    try:
        Path(tmp).write_text(json.dumps(data))
        os.replace(tmp, str(p))
    except Exception as exc:
        logger.warning("[EW-DATA] cache save error: %s", exc)


def _get_fmp_market_cap(ticker: str, cache: Dict[str, Any]) -> Optional[float]:
    """Return market cap for ticker via FMP company profile. Uses in-memory cache."""
    if ticker in cache:
        return cache[ticker].get("market_cap")

    try:
        key = _fmp_key()
    except EnvironmentError:
        return None

    url = (
        f"https://financialmodelingprep.com/stable/profile?symbol={ticker}&apikey={key}"
    )
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        raw = resp.json()
        items = raw if isinstance(raw, list) else [raw]
        if items:
            cap = items[0].get("mktCap") or items[0].get("marketCap") or items[0].get("mktcap")
            cache[ticker] = {"market_cap": float(cap) if cap else None}
            return cache[ticker]["market_cap"]
    except Exception as exc:
        logger.warning("[EW-DATA] FMP profile error ticker=%s: %s", ticker, exc)
    cache[ticker] = {"market_cap": None}
    return None


def _get_alpaca_avg_volume(ticker: str) -> Optional[float]:
    """Return 30-day average daily volume via Alpaca daily bars."""
    try:
        key, secret = _alpaca_credentials()
    except EnvironmentError:
        return None

    try:
        from alpaca.data.historical import StockHistoricalDataClient  # type: ignore
        from alpaca.data.requests import StockBarsRequest  # type: ignore
        from alpaca.data.timeframe import TimeFrame  # type: ignore
        from datetime import timedelta

        client = StockHistoricalDataClient(key, secret)
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=35)
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=start_dt,
            end=end_dt,
        )
        bars_obj = client.get_stock_bars(req)
        try:
            df = bars_obj.df
        except AttributeError:
            df = bars_obj[ticker].df if hasattr(bars_obj, "__getitem__") else None

        if df is None or df.empty:
            return None

        vols = [float(row["volume"]) for _, row in df.iterrows()]
        return sum(vols) / len(vols) if vols else None
    except Exception as exc:
        logger.warning("[EW-DATA] Alpaca avg_vol error ticker=%s: %s", ticker, exc)
        return None


def get_today_earnings_universe(
    date_iso: Optional[str] = None,
) -> Tuple[List[str], List[str]]:
    """Return (bmo_tickers, amc_tickers) for today filtered by:

    - market_cap >= $10B (FMP profile lookup, cached 24 h)
    - average daily volume >= 100K (Alpaca 30-day daily bars)

    Parameters
    ----------
    date_iso : str, optional
        Date to pull calendar for. Defaults to today UTC.

    Returns
    -------
    (bmo_tickers, amc_tickers) as lists of ticker strings.
    """
    if date_iso is None:
        date_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    logger.info("[EW-DATA] get_today_earnings_universe date=%s", date_iso)
    events = get_earnings_calendar(date_iso)
    if not events:
        return [], []

    profile_cache = _load_universe_cache()

    bmo: List[str] = []
    amc: List[str] = []
    cap_min = 10_000_000_000  # $10B
    vol_min = 100_000

    for ev in events:
        ticker = ev.get("ticker", "").strip().upper()
        timing = (ev.get("time") or "").upper()
        if not ticker:
            continue

        # Market cap gate
        cap = _get_fmp_market_cap(ticker, profile_cache)
        if cap is None or cap < cap_min:
            logger.debug("[EW-DATA] universe skip ticker=%s reason=cap cap=%s",
                         ticker, cap)
            continue

        # Average volume gate
        avg_vol = _get_alpaca_avg_volume(ticker)
        if avg_vol is None or avg_vol < vol_min:
            logger.debug("[EW-DATA] universe skip ticker=%s reason=vol avg_vol=%s",
                         ticker, avg_vol)
            continue

        if timing in ("BMO", "BEFORE MARKET OPEN"):
            bmo.append(ticker)
        elif timing in ("AMC", "AFTER MARKET CLOSE"):
            amc.append(ticker)
        else:
            # Unknown timing: add to both; session will be determined by bars
            bmo.append(ticker)
            amc.append(ticker)

    _save_universe_cache(profile_cache)
    logger.info("[EW-DATA] universe date=%s bmo=%d amc=%d", date_iso, len(bmo), len(amc))
    return bmo, amc
