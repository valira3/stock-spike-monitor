"""v5.26.0 \u2014 engine.seeders (spec-strict).

Stage 3 of the Tiger Sovereign v15.0 spec-strict cut deleted all
non-spec seeder helpers: QQQ Regime Shield seed/tick, DI buffer seed,
DI all-ticker seed, archive/Alpaca prior-session fallbacks, and the
recompute-on-unwarm paths. Per RULING #5, the QQQ 5m + EMA9 walk is
now maintained by the live scan via `_qqq_weather_tick` in
trade_genius.py and does not need a pre-market seed.

What remains: `seed_opening_range` (G-1 / S-3 freeze) and its all-
ticker driver `seed_opening_range_all`.
"""

from __future__ import annotations

import logging
import sys as _sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger("trade_genius")


def _tg():
    """Live trade_genius module (handles __main__ vs imported cases)."""
    return _sys.modules.get("trade_genius") or _sys.modules.get("__main__")


def seed_opening_range(ticker):
    """Seed or_high[ticker]/or_low[ticker] from Alpaca historical 1m bars
    covering today's 09:30 ET to 09:30+OR_WINDOW_MINUTES ET window.

    Only seeds when the OR window is complete (now_et >= window end).
    Pre-open or pre-9:35 ET restarts return bars_used=0 so the scheduled
    09:35 ET collect_or() can run cleanly.
    """
    tg = _tg()
    result = {"or_high": None, "or_low": None, "bars_used": 0}
    client = tg._alpaca_data_client()
    if client is None:
        logger.debug("OR_SEED %s skipped, no alpaca data client", ticker)
        return result
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
    except Exception as e:
        logger.debug("OR_SEED %s import failed: %s", ticker, e)
        return result

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    window_start = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    window_end = window_start + timedelta(minutes=tg.OR_WINDOW_MINUTES)
    if now_et < window_end:
        logger.debug(
            "OR_SEED %s skipped, window not complete (now_et=%s < end=%s)",
            ticker,
            now_et.strftime("%H:%M"),
            window_end.strftime("%H:%M"),
        )
        return result

    try:
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Minute,
            start=window_start.astimezone(timezone.utc),
            end=window_end.astimezone(timezone.utc),
            feed="iex",
        )
        resp = client.get_stock_bars(req)
        data = getattr(resp, "data", {}) or {}
        rows = data.get(ticker, []) or []
    except Exception as e:
        logger.warning("OR_SEED %s alpaca fetch failed: %s", ticker, e)
        return result

    max_hi = None
    min_lo = None
    bars_used = 0
    window_start_ts = int(window_start.timestamp())
    window_end_ts = int(window_end.timestamp())
    for row in rows:
        ts = getattr(row, "timestamp", None)
        if ts is None:
            continue
        try:
            epoch = int(ts.timestamp())
        except Exception:
            continue
        if epoch < window_start_ts or epoch >= window_end_ts:
            continue
        h = float(getattr(row, "high", 0) or 0)
        lo = float(getattr(row, "low", 0) or 0)
        if h <= 0 or lo <= 0:
            continue
        if max_hi is None or h > max_hi:
            max_hi = h
        if min_lo is None or lo < min_lo:
            min_lo = lo
        bars_used += 1

    if max_hi is None or min_lo is None:
        logger.warning("OR_SEED %s, no usable bars in window", ticker)
        return result

    tg.or_high[ticker] = max_hi
    tg.or_low[ticker] = min_lo
    result["or_high"] = max_hi
    result["or_low"] = min_lo
    result["bars_used"] = bars_used
    logger.info(
        "OR_SEED ticker=%s or_high=%.2f or_low=%.2f bars_used=%d "
        "window_et=%s-%s source=alpaca_historical",
        ticker,
        max_hi,
        min_lo,
        bars_used,
        window_start.strftime("%H:%M"),
        window_end.strftime("%H:%M"),
    )
    return result


def seed_opening_range_all(tickers):
    """Run seed_opening_range for every ticker and emit a summary.

    Marks or_collected_date=today once at least one ticker is seeded,
    so the scheduled 09:35 ET collect_or() does not overwrite the
    fresher Alpaca-sourced OR.
    """
    tg = _tg()
    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    today = now_et.strftime("%Y-%m-%d")
    window_end = now_et.replace(hour=9, minute=30, second=0, microsecond=0) + timedelta(
        minutes=tg.OR_WINDOW_MINUTES
    )
    if now_et < window_end:
        logger.info(
            "OR_SEED_DONE tickers=0 seeded=0 skipped=%d, pre-OR-window",
            len(tickers),
        )
        return
    seeded = 0
    skipped = 0
    for t in tickers:
        try:
            r = seed_opening_range(t)
            if r.get("bars_used", 0) > 0:
                seeded += 1
            else:
                skipped += 1
        except Exception as e:
            logger.warning("OR_SEED %s crashed: %s", t, e)
            skipped += 1
    if seeded > 0:
        tg.or_collected_date = today
    logger.info(
        "OR_SEED_DONE tickers=%d seeded=%d skipped=%d",
        len(tickers),
        seeded,
        skipped,
    )
