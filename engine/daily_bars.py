"""v5.21.0 -- engine.daily_bars: daily close price fetcher.

Provides `get_recent_daily_closes` which returns a chronological list
of recent daily close prices (most-recent-last) for a given ticker.

Design principles:
    - Inject a `fetcher` callable for full testability.
    - Default fetcher uses the existing Alpaca client pattern from
      trade_genius._alpaca_data_client (VAL_ALPACA_PAPER_KEY, etc.).
    - Per-process cache with 30-minute TTL so daily bars are not
      re-fetched on every snapshot tick (daily bars change at most
      once per market day).
    - NEVER pulls at module import time.

Public API:
    get_recent_daily_closes(ticker, lookback=250, *, fetcher=None) -> list[float]
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, List, Optional

logger = logging.getLogger("trade_genius")

# ---------------------------------------------------------------------------
# Module-level TTL cache
# ---------------------------------------------------------------------------
# Keyed by (ticker, lookback). Value is (timestamp_float, list[float]).
# TTL of 30 minutes (1800 seconds) -- daily bars do not change intraday.

_CACHE: dict[tuple[str, int], tuple[float, list[float]]] = {}
_CACHE_TTL_SECONDS: float = 1800.0


def _cache_get(ticker: str, lookback: int) -> Optional[list[float]]:
    """Return cached closes if fresh, else None."""
    key = (ticker, lookback)
    entry = _CACHE.get(key)
    if entry is None:
        return None
    ts, closes = entry
    if time.monotonic() - ts < _CACHE_TTL_SECONDS:
        return closes
    return None


def _cache_set(ticker: str, lookback: int, closes: list[float]) -> None:
    """Store closes in cache with the current monotonic timestamp."""
    _CACHE[(ticker, lookback)] = (time.monotonic(), closes)


def _cache_clear() -> None:
    """Clear the entire cache. Intended for test use only."""
    _CACHE.clear()


# ---------------------------------------------------------------------------
# Default Alpaca fetcher
# ---------------------------------------------------------------------------


def _build_alpaca_client():
    """Build a read-only StockHistoricalDataClient using Alpaca paper
    credentials from the environment. Mirrors the pattern used by
    trade_genius._alpaca_data_client.

    Tries VAL keys first, then GENE keys. Returns None if no credentials
    are set or the alpaca-py package is unavailable.
    """
    key = (
        os.getenv("VAL_ALPACA_PAPER_KEY", "").strip()
        or os.getenv("GENE_ALPACA_PAPER_KEY", "").strip()
    )
    secret = (
        os.getenv("VAL_ALPACA_PAPER_SECRET", "").strip()
        or os.getenv("GENE_ALPACA_PAPER_SECRET", "").strip()
    )
    if not key or not secret:
        return None
    try:
        from alpaca.data.historical import StockHistoricalDataClient  # type: ignore

        return StockHistoricalDataClient(key, secret)
    except Exception as exc:
        logger.debug("daily_bars: alpaca client build failed: %s", exc)
        return None


def _default_fetcher(ticker: str, lookback: int) -> list[float]:
    """Fetch daily closes via the Alpaca StockHistoricalDataClient.

    Pulls `lookback` calendar days ending today. Returns a
    chronological list of close prices (most-recent-last).

    Raises RuntimeError if the Alpaca client cannot be constructed
    (missing credentials or package not installed). Callers that
    need silent failure should use try/except around
    get_recent_daily_closes.
    """
    import datetime

    client = _build_alpaca_client()
    if client is None:
        raise RuntimeError(
            "daily_bars: no Alpaca credentials found -- "
            "set VAL_ALPACA_PAPER_KEY/VAL_ALPACA_PAPER_SECRET (or GENE_) "
            "to enable daily SMA computation"
        )

    from alpaca.data.requests import StockBarsRequest  # type: ignore
    from alpaca.data.timeframe import TimeFrame  # type: ignore

    end_dt = datetime.datetime.now(datetime.timezone.utc)
    start_dt = end_dt - datetime.timedelta(days=lookback + 10)  # small buffer

    request = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Day,
        start=start_dt,
        end=end_dt,
        limit=lookback + 10,
    )
    bars = client.get_stock_bars(request)
    # bars is a BarSet; iterate over ticker bars
    ticker_bars = bars[ticker] if hasattr(bars, "__getitem__") else list(bars)
    closes = [float(bar.close) for bar in ticker_bars]
    # Keep only the most-recent `lookback` bars
    return closes[-lookback:]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_recent_daily_closes(
    ticker: str,
    lookback: int = 250,
    *,
    fetcher: Optional[Callable[[str, int], List[float]]] = None,
) -> list[float]:
    """Return a chronological list of up to `lookback` daily close prices.

    Parameters
    ----------
    ticker:
        Ticker symbol (e.g. "AAPL").
    lookback:
        Maximum number of daily closes to return. Defaults to 250
        (enough to compute all SMA windows including SMA 200).
    fetcher:
        Optional injectable callable with signature
        ``(ticker: str, lookback: int) -> list[float]``.
        Defaults to the Alpaca-based implementation. Pass a stub in
        tests to avoid live network calls.

    Returns
    -------
    list[float]
        Chronological list of close prices, most-recent-last.
        May be shorter than `lookback` when the ticker has limited
        history. Never returns None -- callers receive an empty list
        on failure if they catch exceptions upstream.

    Raises
    ------
    RuntimeError
        When no fetcher is supplied and the Alpaca client cannot be
        constructed (missing credentials or alpaca-py not installed).
        Propagated so the caller (snapshot writer) can log and fall
        back to sma_stack=None rather than silently using stale data.
    """
    cached = _cache_get(ticker, lookback)
    if cached is not None:
        return cached

    fetch_fn = fetcher if fetcher is not None else _default_fetcher
    closes = fetch_fn(ticker, lookback)
    closes_list = list(closes)
    _cache_set(ticker, lookback, closes_list)
    return closes_list


__all__ = ["get_recent_daily_closes"]
