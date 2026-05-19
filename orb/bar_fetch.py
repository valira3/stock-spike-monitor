"""Alpaca + Yahoo 1-minute bar fetch helpers.

History. Lived in trade_genius.py from v3.4.x through v9.1.140. Carved
out to its own module in v10.0.1 as part of the post-architectural-
review monolith reduction. trade_genius.py keeps back-compat re-exports
for the orchestrator path (`fetch_1min_bars`) and every internal helper
(`_alpaca_data_client`, `_alpaca_pdc`, `_daily_closes_for_sma`,
`_fetch_1min_bars_alpaca`, `_fetch_1min_bars_yahoo`), plus the cache
state dicts (`_cycle_bar_cache`, `_alpaca_pdc_cache`,
`_dual_source_critical_emitted`). Existing callers and test
monkey-patches against `trade_genius.<name>` keep working unchanged.

What stayed in trade_genius.py: the public `fetch_1min_bars` orchestrator
(it sits on the telegram coupling boundary because the "both sources
failed" branch sends a CRITICAL Telegram alert via
`_notify_dual_source_failure`), plus `_notify_dual_source_failure`
itself. The dependency direction stays clean: orb.bar_fetch has no
imports from trade_genius at module top -- the only crossover is a
lazy `from trade_genius import get_fmp_quote` inside
`_fetch_1min_bars_alpaca` so the FMP-live-quote path resolves to
whatever tg's namespace currently has (including pytest monkey-patches).

Module-level state owned here:
  - _cycle_bar_cache              per-scan-cycle 1m bar memo
  - _alpaca_pdc_cache             per-(ticker, ET-date) previous-day close
  - _dual_source_critical_emitted one-shot ticker set for CRITICAL alert

All three are mutated by `fetch_1min_bars` (in trade_genius) via the
re-exported references, so they remain shared across the trade_genius
and orb.bar_fetch namespaces.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo


logger = logging.getLogger(__name__)


# Yahoo HTTP config (legacy fallback path).
YAHOO_TIMEOUT = 8
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}

# Per-scan-cycle cache for 1-min bars. scan_loop() calls
# _clear_cycle_bar_cache() at the start of each cycle; any call to
# fetch_1min_bars within the same cycle reuses the cached response.
# This lets observers (RSI, breadth) read the same bars the scan loop
# already fetched without doubling network calls.
_cycle_bar_cache: dict = {}

# v6.0.5 -- pdc cache keyed by (ticker_upper, et_date_iso). Alpaca's daily
# previous-close is yesterday's RTH close which doesn't change intra-session,
# so we look it up once per ticker per ET trading day instead of on every
# scan cycle. Value is float (success) or None (lookup failed; we'll retry
# next cycle in case the daily endpoint was transient).
_alpaca_pdc_cache: dict = {}

# v6.0.5 -- one-shot guard so the dual-source-failure CRITICAL notification
# only fires once per ticker per process lifetime. Without this, a sustained
# outage (e.g. Alpaca + Yahoo both down for an hour) would spam a
# notification every scan cycle (~12/min). The flag resets on process
# restart, which is the right reset semantics: a redeploy means we want to
# know if it's still broken.
_dual_source_critical_emitted: set = set()


def _clear_cycle_bar_cache():
    """Reset the per-cycle bar cache. Called at the top of scan_loop()."""
    _cycle_bar_cache.clear()


def _alpaca_data_client():
    """Build a read-only StockHistoricalDataClient using whatever
    Alpaca paper credentials are in the environment. Tries Val first,
    then Gene. Returns None if no keys are set or alpaca-py import
    fails -- caller must tolerate a None return.
    """
    key = os.getenv("VAL_ALPACA_PAPER_KEY", "").strip() \
          or os.getenv("GENE_ALPACA_PAPER_KEY", "").strip()
    secret = os.getenv("VAL_ALPACA_PAPER_SECRET", "").strip() \
             or os.getenv("GENE_ALPACA_PAPER_SECRET", "").strip()
    if not key or not secret:
        return None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        return StockHistoricalDataClient(key, secret)
    except Exception as e:
        logger.debug("alpaca data client build failed: %s", e)
        return None


def _daily_closes_for_sma(ticker: str, needed: int = 210) -> Optional[list]:
    """v6.0.1 -- fetch the most recent ``needed`` daily closes for
    ``ticker``, oldest-first. Used by the Daily SMA stack panel
    (``v5_13_2_snapshot._compute_sma_stack_safe``) which caches the
    result once per RTH calendar day so this only runs once per ticker
    per day in steady state.

    Returns ``None`` on any failure (no Alpaca client, alpaca-py
    missing, network error, ticker symbol unknown). Caller must treat
    ``None`` as "not available" and the frontend renders the fallback.
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        return None
    client = _alpaca_data_client()
    if client is None:
        return None
    try:
        from alpaca.data.requests import StockBarsRequest  # type: ignore
        from alpaca.data.timeframe import TimeFrame  # type: ignore
    except Exception as e:
        logger.debug("alpaca StockBarsRequest import failed: %s", e)
        return None
    # Pull a generous calendar window (need ``needed`` trading days; ~252
    # trading days per year, so 1.6x covers weekends/holidays comfortably).
    end = datetime.now(timezone.utc)
    # Roughly 1.7 calendar days per trading day handles weekends + holidays.
    lookback_days = max(int(needed * 1.7), 60)
    start = end - timedelta(days=lookback_days)
    try:
        # v6.5.0 P-5 -- promoted to SIP feed (Algo Plus unlocks consolidated
        # tape). Falls back to IEX if SIP returns empty (defense-in-depth
        # per spec section 5 risk register).
        req = StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed="sip",
        )
        resp = client.get_stock_bars(req)
        _daily_sma_bars_tmp = None
        try:
            _d = getattr(resp, "data", None)
            if isinstance(_d, dict):
                _daily_sma_bars_tmp = _d.get(sym)
        except Exception:
            _daily_sma_bars_tmp = None
        if not _daily_sma_bars_tmp:
            logger.debug("daily-bars SIP empty for %s, retrying IEX", sym)
            req_iex = StockBarsRequest(
                symbol_or_symbols=sym,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed="iex",
            )
            resp = client.get_stock_bars(req_iex)
    except Exception as e:
        logger.debug("daily-bars fetch failed for %s: %s", sym, e)
        return None
    bars = None
    try:
        # alpaca-py BarSet has a ``data`` dict[symbol, list[Bar]] and
        # also indexes via __getitem__; both shapes have shown up across
        # versions, so try both.
        data = getattr(resp, "data", None)
        if isinstance(data, dict):
            bars = data.get(sym)
        if bars is None:
            try:
                bars = resp[sym]
            except Exception:
                bars = None
    except Exception as e:
        logger.debug("daily-bars unpack failed for %s: %s", sym, e)
        return None
    if not bars:
        return None
    closes: list[float] = []
    for b in bars:
        c = getattr(b, "close", None)
        if c is None:
            continue
        try:
            closes.append(float(c))
        except (TypeError, ValueError):
            continue
    if not closes:
        return None
    # Trim to the most recent ``needed`` values.
    if len(closes) > needed:
        closes = closes[-needed:]
    return closes


def _alpaca_pdc(ticker: str, client) -> Optional[float]:
    """Return previous-day RTH close for ``ticker`` from Alpaca daily bars.

    Cached per ticker per ET date. ``None`` on any failure (caller must
    tolerate -- downstream code reads bars["pdc"] with a 0-fallback).
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        return None
    et = ZoneInfo("America/New_York")
    today_et = datetime.now(et).date().isoformat()
    ckey = (sym, today_et)
    if ckey in _alpaca_pdc_cache:
        return _alpaca_pdc_cache[ckey]
    if client is None:
        return None
    try:
        from alpaca.data.requests import StockBarsRequest  # type: ignore
        from alpaca.data.timeframe import TimeFrame  # type: ignore
        from alpaca.data.enums import DataFeed  # type: ignore
    except Exception as e:
        logger.debug("alpaca pdc import failed for %s: %s", sym, e)
        return None
    # Pull a 10 calendar-day window so we always have at least one prior
    # trading day even across long weekends / market holidays.
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=10)
    try:
        # v6.5.0 P-5 -- promoted to SIP feed; falls back to IEX if SIP
        # returns empty (defense-in-depth per spec section 5 risk register).
        req = StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed=DataFeed.SIP,
        )
        resp = client.get_stock_bars(req)
        rows = []
        if hasattr(resp, "data"):
            rows = resp.data.get(sym, []) or []
        if not rows:
            logger.debug("pdc SIP empty for %s, retrying IEX", sym)
            req_iex = StockBarsRequest(
                symbol_or_symbols=sym,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed=DataFeed.IEX,
            )
            resp_iex = client.get_stock_bars(req_iex)
            if hasattr(resp_iex, "data"):
                rows = resp_iex.data.get(sym, []) or []
        # Alpaca's daily bars come oldest-first; the LAST bar with a
        # timestamp strictly before today's ET date is yesterday's RTH
        # close (Alpaca closes the daily bar at 16:00 ET so today's bar,
        # if present mid-session, is still forming and must be skipped).
        prev_close = None
        for b in rows:
            ts = getattr(b, "timestamp", None)
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            bar_date_et = ts.astimezone(et).date().isoformat()
            if bar_date_et >= today_et:
                continue
            c = getattr(b, "close", None)
            if c is None:
                continue
            try:
                prev_close = float(c)
            except (TypeError, ValueError):
                continue
        _alpaca_pdc_cache[ckey] = prev_close
        return prev_close
    except Exception as e:
        logger.debug("alpaca pdc fetch failed for %s: %s", sym, e)
        # Negative-cache for this ET day so we don't retry every cycle
        # if the call is structurally broken (e.g. delisted symbol).
        # Yahoo fallback path will still supply pdc when it runs.
        return None


# v8.3.2 -- SIP completeness helpers live in engine/data_completeness.py
# so they're independently importable + unit-testable without dragging
# in trade_genius's telegram / alpaca / FMP deps.
from engine.data_completeness import (
    _or_expected_bars,
    _count_alpaca_rows_in_or_window,
    _is_or_coverage_thin,
    _merge_alpaca_rows_by_timestamp,
)


def _fetch_1min_bars_alpaca(ticker: str) -> Optional[dict]:
    """v6.0.5 -- Alpaca-IEX 1m bar fetch in the same dict shape as the
    legacy Yahoo path. Covers 04:00-20:00 ET so the premarket warm-up
    loop and the bar archive keep working exactly like they did under
    Yahoo's ``includePrePost=true``.

    Returns the same dict shape as ``_fetch_1min_bars_yahoo`` on success,
    or ``None`` on any failure (no creds, alpaca-py missing, network
    error, empty response). Caller is responsible for falling back to
    Yahoo on ``None``.

    Lists are oldest-first to match Yahoo's ordering. Unlike Yahoo,
    Alpaca only emits a bar when at least one trade prints in that
    minute, so closes/highs/lows are guaranteed non-None -- which is the
    whole reason for this swap. The trailing-None walk-back in
    broker/positions.py stays in place as defense-in-depth for the
    Yahoo fallback case.
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        return None
    t0 = time.time()
    client = _alpaca_data_client()
    if client is None:
        logger.debug("Alpaca %s: no data client", sym)
        return None
    try:
        from alpaca.data.requests import StockBarsRequest  # type: ignore
        from alpaca.data.timeframe import TimeFrame  # type: ignore
        from alpaca.data.enums import DataFeed  # type: ignore
    except Exception as e:
        logger.debug("alpaca 1m import failed for %s: %s", sym, e)
        return None
    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    # v6.5.0 P-4 -- expanded window from 08:00-18:00 to 04:00-20:00 ET
    # to capture full premarket (04:00-09:30) and after-hours (16:00-20:00)
    # sessions now available via Algo Plus SIP feed.
    start_et = now_et.replace(hour=4, minute=0, second=0, microsecond=0)
    end_et = now_et.replace(hour=20, minute=0, second=0, microsecond=0) + timedelta(minutes=1)
    try:
        # v6.5.0 P-5 -- promoted to SIP feed; falls back to IEX if SIP
        # returns empty (defense-in-depth per spec section 5 risk register).
        req = StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame.Minute,
            start=start_et.astimezone(timezone.utc),
            end=end_et.astimezone(timezone.utc),
            feed=DataFeed.SIP,
        )
        resp = client.get_stock_bars(req)
    except Exception as e:
        logger.debug("Alpaca %s: fetch failed: %s (%.2fs)", sym, e, time.time() - t0)
        return None
    rows = []
    try:
        if hasattr(resp, "data"):
            rows = resp.data.get(sym, []) or resp.data.get(ticker, []) or []
    except Exception:
        rows = []
    sip_count = len(rows)
    # v8.3.2 -- SIP->IEX merge fallback on thin result, not just empty.
    # Why: pre-v8.3.2, the IEX retry only fired when SIP returned ZERO
    # bars. If SIP returned a partial result (e.g. 9 of an expected 30
    # OR-window bars due to a transient feed glitch), we accepted the
    # thin data, the OR window locked with bars_seen<15, and the FSM
    # transitioned to PHASE_BLOCKED_OR_INSUFFICIENT for the rest of
    # the day. v8.3.2 counts in-OR-window bars (09:30-09:59 ET) and
    # retries IEX + merges by timestamp if SIP's coverage is thin.
    # Yahoo via _fetch_1min_bars_yahoo() remains the last-ditch
    # fallback when even SIP+IEX merged comes up short.
    or_expected = _or_expected_bars(now_et)
    sip_or_count = _count_alpaca_rows_in_or_window(rows, et)
    iex_count = 0
    if not rows or _is_or_coverage_thin(sip_or_count, or_expected):
        try:
            req_iex = StockBarsRequest(
                symbol_or_symbols=sym,
                timeframe=TimeFrame.Minute,
                start=start_et.astimezone(timezone.utc),
                end=end_et.astimezone(timezone.utc),
                feed=DataFeed.IEX,
            )
            resp_iex = client.get_stock_bars(req_iex)
            iex_rows = []
            if hasattr(resp_iex, "data"):
                iex_rows = (resp_iex.data.get(sym, [])
                            or resp_iex.data.get(ticker, []) or [])
            iex_count = len(iex_rows)
            if iex_rows:
                rows = _merge_alpaca_rows_by_timestamp(rows, iex_rows)
        except Exception as e_iex:
            logger.debug("Alpaca %s: IEX fallback failed: %s", sym, e_iex)
    merged_count = len(rows)
    merged_or_count = _count_alpaca_rows_in_or_window(rows, et)
    # Forensic: one log line per fetch when we had to fire the IEX
    # retry. Silent on the steady-state happy path (SIP full coverage).
    if sip_or_count != merged_or_count or sip_count != merged_count:
        logger.info(
            "[V83-SIP-COMPLETENESS] sym=%s sip=%d iex=%d merged=%d "
            "sip_or=%d merged_or=%d or_expected=%d",
            sym, sip_count, iex_count, merged_count,
            sip_or_count, merged_or_count, or_expected,
        )
    if not rows:
        logger.debug("Alpaca %s: empty rows after SIP+IEX (%.2fs)", sym, time.time() - t0)
        return None
    # If merged result is STILL thin on the OR window, return None so
    # the caller falls through to the Yahoo branch (last-ditch).
    if _is_or_coverage_thin(merged_or_count, or_expected, hard=True):
        logger.warning(
            "[V83-SIP-COMPLETENESS] sym=%s thin-after-merge "
            "sip=%d iex=%d merged_or=%d/%d -- falling back to Yahoo",
            sym, sip_count, iex_count, merged_or_count, or_expected,
        )
        return None
    timestamps: list[int] = []
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    volumes: list[int] = []
    for b in rows:
        ts = getattr(b, "timestamp", None)
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        try:
            timestamps.append(int(ts.timestamp()))
            opens.append(float(getattr(b, "open", 0) or 0))
            highs.append(float(getattr(b, "high", 0) or 0))
            lows.append(float(getattr(b, "low", 0) or 0))
            closes.append(float(getattr(b, "close", 0) or 0))
            volumes.append(int(getattr(b, "volume", 0) or 0))
        except (TypeError, ValueError):
            continue
    if not timestamps or not closes:
        logger.debug("Alpaca %s: no usable bars after parse (%.2fs)", sym, time.time() - t0)
        return None
    # current_price MUST be near-real-time: engine/scan.py uses it as the
    # entry execution price (px = bars["current_price"]). Yahoo's
    # ``regularMarketPrice`` was tick-current; Alpaca's last 1m bar close
    # is up to ~60s stale. To preserve entry-pricing semantics on the
    # Alpaca path we ask FMP for the live quote (already the bot's
    # canonical realtime source -- see get_fmp_quote use sites). Last
    # bar close is the fallback if FMP is down so we never regress to 0.
    # Lazy import of get_fmp_quote so pytest monkey-patches against
    # trade_genius.get_fmp_quote are visible to this code path.
    current_price = 0
    try:
        from trade_genius import get_fmp_quote as _get_fmp_quote
        _fmp_q = _get_fmp_quote(sym) or {}
        _fmp_px = _fmp_q.get("price")
        if _fmp_px is not None:
            current_price = float(_fmp_px) or 0
    except Exception:
        current_price = 0
    if not current_price and closes:
        current_price = closes[-1]
    pdc_val = _alpaca_pdc(sym, client) or 0
    out = {
        "timestamps": timestamps,
        "opens": opens,
        "highs": highs,
        "lows": lows,
        "closes": closes,
        "volumes": volumes,
        "current_price": current_price,
        "pdc": pdc_val,
    }
    logger.debug("Alpaca %s: %d bars, %.2fs", sym, len(timestamps), time.time() - t0)
    return out


def _fetch_1min_bars_yahoo(ticker):
    """Legacy Yahoo Finance 1m fetch. Kept as a fallback when the
    Alpaca primary path returns None.

    Returns dict with keys: timestamps, opens, highs, lows, closes,
    volumes, current_price, pdc.  Returns None on failure.
    """
    t0 = time.time()
    # v5.30.1 -- includePrePost=true so the 08:00-09:30 ET premarket
    # warm-up loop in engine.scan actually receives bars to archive into
    # /data/bars/<today>/<ticker>.jsonl. Prior to this the loop ran every
    # minute starting at 08:00 ET but Yahoo only returned RTH bars, so
    # the bar archive (and the dashboard charts that read from it) stayed
    # frozen at yesterday's 19:59 close until 09:30. Including premarket
    # bars does not affect entry / OR / sentinel logic: callers downstream
    # filter by ts (e.g. opening-range collection bounds bars to
    # [09:30, 09:36) ET) so premarket bars only flow where they should
    # -- the bar archive and the dashboard chart panel.
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/%s"
        "?interval=1m&range=1d&includePrePost=true" % ticker
    )
    try:
        req = urllib.request.Request(url, headers=YAHOO_HEADERS)
        with urllib.request.urlopen(req, timeout=YAHOO_TIMEOUT) as resp:
            data = json.loads(resp.read())

        result = data.get("chart", {}).get("result")
        if not result:
            logger.debug("Yahoo %s: empty result (%.2fs)", ticker, time.time() - t0)
            return None
        r = result[0]
        meta = r.get("meta", {})
        quote = r.get("indicators", {}).get("quote", [{}])[0]
        timestamps = r.get("timestamp", [])

        if not timestamps:
            logger.debug("Yahoo %s: no timestamps (%.2fs)", ticker, time.time() - t0)
            return None

        logger.debug("Yahoo %s: %.2fs", ticker, time.time() - t0)
        out = {
            "timestamps": timestamps,
            "opens": quote.get("open", []),
            "highs": quote.get("high", []),
            "lows": quote.get("low", []),
            "closes": quote.get("close", []),
            "volumes": quote.get("volume", []),
            "current_price": meta.get("regularMarketPrice", 0),
            "pdc": (meta.get("previousClose")
                    or meta.get("chartPreviousClose")
                    or 0),
        }
        return out
    except Exception as e:
        logger.debug("Yahoo %s: fetch failed: %s (%.2fs)", ticker, e, time.time() - t0)
        return None
