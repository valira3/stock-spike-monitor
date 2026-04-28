"""v5.11.0 \u2014 engine.seeders: pre-market QQQ regime + DI + OR seeders.

Extracted verbatim from `trade_genius.py` (v5.10.7 lines 2894\u20134692,
shifted slightly after PR 198 history-tail cleanup). Public names drop
the `_v590_` / `_seed_` prefixes per the v5.11.0 refactor convention;
private aliases are kept in trade_genius.py for one release as
deprecation shims.

Zero behavior change. Validated byte-equal pre/post the move via
`tests/golden/verify.py`.

Module-level state (`_QQQ_REGIME`, `_QQQ_REGIME_SEEDED`,
`_QQQ_REGIME_LAST_BUCKET`, `_DI_SEED_CACHE`, `or_high`, `or_low`,
`or_collected_date`, `OR_WINDOW_MINUTES`) and helpers (`_alpaca_data_client`,
`fetch_1min_bars`, `_resample_to_5min`, `_compute_di`, `V561_INDEX_TICKER`,
`DI_PERIOD`) remain owned by trade_genius.py to avoid circular imports
during the v5.11.0 staged extraction. They are accessed via the live
trade_genius module through `_tg()` (the same pattern paper_state.py
and telegram_commands.py use).
"""
from __future__ import annotations

import json
import logging
import os
import sys as _sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger("trade_genius")


def _tg():
    """Live trade_genius module (handles __main__ vs imported cases)."""
    return _sys.modules.get("trade_genius") or _sys.modules.get("__main__")


def qqq_regime_seed_once() -> None:
    """v5.9.0 \u2014 Seed the QQQ Regime EMAs from pre-market 5m bars.

    Source priority (per spec):
      1. /data/bars/<today>/QQQ.jsonl bar archive
      2. Alpaca historical 5m bars (IEX feed) for today 04:00 ET \u2192 now
      3. Prior session's last 5m bars

    Idempotent: subsequent calls are no-ops. Failure-tolerant: any
    crash leaves the regime un-seeded, in which case the live tick
    feed will warm up the EMAs naturally (compass returns None until
    9 closed bars accumulate).
    """
    tg = _tg()
    if tg._QQQ_REGIME_SEEDED:
        return

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    today_0400 = now_et.replace(hour=4, minute=0, second=0, microsecond=0)

    closes = _qqq_seed_from_archive(today_0400, now_et)
    source = None
    if closes:
        source = "archive"
    else:
        closes = _qqq_seed_from_alpaca(today_0400, now_et)
        if closes:
            source = "alpaca"
        else:
            closes = _qqq_seed_from_prior_session(now_et)
            if closes:
                source = "prior_session"

    if not closes:
        logger.warning("[V572-REGIME-SEED] no source returned bars; "
                       "compass will warm up from live ticks")
        tg._QQQ_REGIME_SEEDED = True
        return

    n = tg._QQQ_REGIME.seed(closes, source)
    tg._QQQ_REGIME_SEEDED = True
    compass = tg._QQQ_REGIME.current_compass()
    logger.info(
        "[V572-REGIME-SEED] source=%s bars=%d ema3=%s ema9=%s compass=%s",
        source, n,
        ("%.4f" % tg._QQQ_REGIME.ema3) if tg._QQQ_REGIME.ema3 is not None else "None",
        ("%.4f" % tg._QQQ_REGIME.ema9) if tg._QQQ_REGIME.ema9 is not None else "None",
        compass if compass is not None else "None",
    )


def _qqq_seed_from_archive(start_et, end_et):
    """Try to read today's QQQ bar archive, resample to 5m closes.

    Returns chronological list of finalized 5m closes from
    [start_et, end_et). Returns [] on any failure or if archive
    has no entries.
    """
    tg = _tg()
    try:
        date_str = start_et.strftime("%Y-%m-%d")
        path = "/data/bars/%s/QQQ.jsonl" % date_str
        if not os.path.exists(path):
            return []
        timestamps = []
        closes = []
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get("ts")
                close = rec.get("close")
                if ts is None or close is None:
                    continue
                try:
                    if isinstance(ts, str):
                        epoch = int(datetime.strptime(
                            ts.replace("Z", ""), "%Y-%m-%dT%H:%M:%S",
                        ).replace(tzinfo=timezone.utc).timestamp())
                    else:
                        epoch = int(ts)
                except Exception:
                    continue
                if (epoch < int(start_et.timestamp())
                        or epoch >= int(end_et.timestamp())):
                    continue
                timestamps.append(epoch)
                closes.append(float(close))
        return tg._resample_to_5min(timestamps, closes)
    except Exception as e:
        logger.debug("[V572-REGIME-SEED] archive read failed: %s", e)
        return []


def _qqq_seed_from_alpaca(start_et, end_et):
    """Pull today's pre-market 5m QQQ bars via Alpaca historical IEX.

    Returns chronological list of finalized 5m closes. [] on failure.
    """
    tg = _tg()
    try:
        client = tg._alpaca_data_client()
        if client is None:
            return []
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        req = StockBarsRequest(
            symbol_or_symbols="QQQ",
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start_et.astimezone(timezone.utc),
            end=end_et.astimezone(timezone.utc),
            feed="iex",
        )
        resp = client.get_stock_bars(req)
        data = getattr(resp, "data", {}) or {}
        rows = data.get("QQQ", []) or []
        closes = []
        end_ts = int(end_et.timestamp())
        for row in rows:
            ts = getattr(row, "timestamp", None)
            c = getattr(row, "close", None)
            if ts is None or c is None:
                continue
            try:
                epoch = int(ts.timestamp())
            except Exception:
                continue
            # Drop the still-forming bar (one whose end > now).
            if epoch + 300 > end_ts:
                continue
            closes.append(float(c))
        return closes
    except Exception as e:
        logger.debug("[V572-REGIME-SEED] alpaca fetch failed: %s", e)
        return []


def _qqq_seed_from_prior_session(now_et):
    """Final fallback \u2014 prior session's last few 5m QQQ bars (Alpaca)."""
    tg = _tg()
    try:
        client = tg._alpaca_data_client()
        if client is None:
            return []
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        yday = now_et - timedelta(days=1)
        while yday.weekday() >= 5:
            yday = yday - timedelta(days=1)
        start = yday.replace(hour=14, minute=0, second=0, microsecond=0)
        end = yday.replace(hour=16, minute=0, second=0, microsecond=0)
        req = StockBarsRequest(
            symbol_or_symbols="QQQ",
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start.astimezone(timezone.utc),
            end=end.astimezone(timezone.utc),
            feed="iex",
        )
        resp = client.get_stock_bars(req)
        data = getattr(resp, "data", {}) or {}
        rows = data.get("QQQ", []) or []
        closes = []
        for row in rows:
            c = getattr(row, "close", None)
            if c is None:
                continue
            closes.append(float(c))
        return closes
    except Exception as e:
        logger.debug("[V572-REGIME-SEED] prior session fetch failed: %s", e)
        return []


def qqq_regime_tick():
    """v5.9.0 \u2014 Advance the QQQ regime on a freshly closed 5m bar.

    Called every scan cycle. Pulls the latest QQQ 1m bars, resamples
    to 5m closes, and advances the regime state by exactly one bar
    when a new finalized bucket is observed (deduped by epoch//300).
    On each new closed bar emits [V572-REGIME].
    """
    tg = _tg()
    try:
        bars = tg.fetch_1min_bars(tg.V561_INDEX_TICKER)
        if not bars:
            return
        timestamps = bars.get("timestamps") or []
        closes = bars.get("closes") or []
        # Pair valid (ts, close) and bucket by floor(ts/300); drop newest
        # (forming) bucket via the existing resampler logic.
        pairs = [(int(t), float(c)) for t, c in zip(timestamps, closes)
                 if t is not None and c is not None]
        if not pairs:
            return
        pairs.sort(key=lambda p: p[0])
        buckets = {}
        for ts, c in pairs:
            buckets[ts // 300] = c
        ordered = sorted(buckets.keys())
        if len(ordered) < 2:
            return
        finalized = ordered[:-1]   # drop newest, possibly forming
        last_bucket = finalized[-1]
        if (tg._QQQ_REGIME_LAST_BUCKET is not None
                and last_bucket <= tg._QQQ_REGIME_LAST_BUCKET):
            return
        # Seed before applying the first live bar so seed math runs first.
        qqq_regime_seed_once()
        # Apply only the new bucket (even after a long gap, fast-forward
        # at most one bar per cycle keeps the math monotonic).
        new_close = buckets[last_bucket]
        tg._QQQ_REGIME.update(new_close)
        tg._QQQ_REGIME_LAST_BUCKET = last_bucket
        compass = tg._QQQ_REGIME.current_compass()
        logger.info(
            "[V572-REGIME] qqq_5m_close=%.4f ema3=%s ema9=%s compass=%s",
            new_close,
            ("%.4f" % tg._QQQ_REGIME.ema3) if tg._QQQ_REGIME.ema3 is not None else "None",
            ("%.4f" % tg._QQQ_REGIME.ema9) if tg._QQQ_REGIME.ema9 is not None else "None",
            compass if compass is not None else "None",
        )
    except Exception as e:
        logger.warning("[V572-REGIME] tick error: %s", e)


def seed_di_buffer(ticker):
    """Seed the DI 5m buffer for `ticker` from Alpaca historical bars.

    Priority stream (oldest \u2192 newest for DI math):
      today-RTH \u2192 today-premarket \u2192 prior-day-RTH
    but we feed oldest-first so the order inside the buffer is
    chronological: prior-day-RTH, then today-premarket, then today-RTH.
    The "priority" really means \u2014 if we already have enough
    today-RTH bars, we don't need to reach back further.

    If the DI_PREMARKET_SEED env flag is "0", premarket bars are
    skipped (kill switch for premarket-noise concerns).

    Safe to call on restart mid-session. Idempotent \u2014 overwrites
    any prior seed for the ticker. On any Alpaca failure logs a
    warning and continues; DI will warm up from live ticks.

    Returns dict {"bars_today_rth": N, "bars_premarket": N,
                  "bars_prior_day": N, "di_after_seed": float|None}.
    """
    tg = _tg()
    result = {
        "bars_today_rth": 0, "bars_premarket": 0,
        "bars_prior_day": 0, "di_after_seed": None,
    }
    client = tg._alpaca_data_client()
    if client is None:
        logger.debug("DI_SEED %s skipped \u2014 no alpaca data client", ticker)
        return result

    premarket_on = os.getenv("DI_PREMARKET_SEED", "1").strip() not in (
        "0", "false", "False", "",
    )

    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
    except Exception as e:
        logger.debug("DI_SEED %s import failed: %s", ticker, e)
        return result

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    today_0400 = now_et.replace(hour=4,  minute=0,  second=0, microsecond=0)
    today_0930 = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    yday = now_et - timedelta(days=1)
    # Step back over weekend to last weekday
    while yday.weekday() >= 5:
        yday = yday - timedelta(days=1)
    yday_rth_end   = yday.replace(hour=16, minute=0, second=0, microsecond=0)
    yday_rth_start = yday.replace(hour=14, minute=50, second=0, microsecond=0)

    def _fetch(start, end):
        try:
            req = StockBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=TimeFrame.Minute,
                start=start.astimezone(timezone.utc),
                end=end.astimezone(timezone.utc),
                feed="iex",
            )
            resp = client.get_stock_bars(req)
            data = getattr(resp, "data", {}) or {}
            rows = data.get(ticker, []) or []
            return rows
        except Exception as e:
            logger.warning("DI_SEED %s alpaca fetch %s\u2192%s failed: %s",
                           ticker, start, end, e)
            return []

    # Fetch today 04:00 ET \u2192 now (premarket + whatever RTH has happened)
    today_rows = _fetch(today_0400, now_et)

    # Bucket 1m rows into 5m OHLC, tagged by classification.
    # today_0930_ts = unix seconds of today's 09:30 ET
    today_0930_ts = int(today_0930.timestamp())

    today_rth_buckets   = {}
    today_pre_buckets   = {}

    for row in today_rows:
        ts = getattr(row, "timestamp", None)
        if ts is None:
            continue
        try:
            # alpaca timestamps are tz-aware datetimes
            epoch = int(ts.timestamp())
        except Exception:
            continue
        h  = float(getattr(row, "high",  0) or 0)
        lo = float(getattr(row, "low",   0) or 0)
        c  = float(getattr(row, "close", 0) or 0)
        if h <= 0 or lo <= 0 or c <= 0:
            continue
        bucket = epoch // 300
        target = today_rth_buckets if epoch >= today_0930_ts else today_pre_buckets
        if bucket not in target:
            target[bucket] = {"bucket": bucket, "high": h, "low": lo, "close": c}
        else:
            target[bucket]["high"]  = max(target[bucket]["high"],  h)
            target[bucket]["low"]   = min(target[bucket]["low"],   lo)
            target[bucket]["close"] = c

    # Drop newest bucket if it could still be forming (now < bucket_end)
    def _finalize(buckets):
        ordered = sorted(buckets.keys())
        if not ordered:
            return []
        last = ordered[-1]
        last_end_ts = (last + 1) * 300
        if int(now_et.timestamp()) < last_end_ts:
            ordered = ordered[:-1]
        return [buckets[b] for b in ordered]

    today_rth_list = _finalize(today_rth_buckets)
    today_pre_list = _finalize(today_pre_buckets) if premarket_on else []
    result["bars_today_rth"]  = len(today_rth_list)
    result["bars_premarket"]  = len(today_pre_list)

    seeded_enough = len(today_rth_list) + len(today_pre_list) >= tg.DI_PERIOD * 2
    prior_day_list = []
    if not seeded_enough:
        prior_rows = _fetch(yday_rth_start, yday_rth_end)
        prior_buckets = {}
        for row in prior_rows:
            ts = getattr(row, "timestamp", None)
            if ts is None:
                continue
            try:
                epoch = int(ts.timestamp())
            except Exception:
                continue
            h  = float(getattr(row, "high",  0) or 0)
            lo = float(getattr(row, "low",   0) or 0)
            c  = float(getattr(row, "close", 0) or 0)
            if h <= 0 or lo <= 0 or c <= 0:
                continue
            bucket = epoch // 300
            if bucket not in prior_buckets:
                prior_buckets[bucket] = {"bucket": bucket, "high": h,
                                          "low": lo, "close": c}
            else:
                prior_buckets[bucket]["high"]  = max(prior_buckets[bucket]["high"],  h)
                prior_buckets[bucket]["low"]   = min(prior_buckets[bucket]["low"],   lo)
                prior_buckets[bucket]["close"] = c
        prior_day_list = [prior_buckets[b] for b in sorted(prior_buckets.keys())]
        result["bars_prior_day"] = len(prior_day_list)

    # Combine chronologically: prior-day \u2192 today-premarket \u2192 today-RTH
    combined = prior_day_list + today_pre_list + today_rth_list
    # Dedup by bucket, keep last
    dedup = {}
    for b in combined:
        dedup[b["bucket"]] = b
    final_list = [dedup[k] for k in sorted(dedup.keys())]
    tg._DI_SEED_CACHE[ticker] = final_list

    # Compute DI on the seeded state for logging
    if len(final_list) >= tg.DI_PERIOD + 1:
        highs  = [b["high"]  for b in final_list]
        lows   = [b["low"]   for b in final_list]
        closes = [b["close"] for b in final_list]
        dp, _dm = tg._compute_di(highs, lows, closes)
        result["di_after_seed"] = dp

    logger.info(
        "DI_SEED ticker=%s bars_today_rth=%d bars_premarket=%d "
        "bars_prior_day=%d di_after_seed=%s",
        ticker, result["bars_today_rth"], result["bars_premarket"],
        result["bars_prior_day"],
        ("%.2f" % result["di_after_seed"])
        if result["di_after_seed"] is not None else "None",
    )
    return result


def seed_di_all(tickers):
    """Run seed_di_buffer for every ticker and emit a summary line."""
    seeded = 0
    skipped = 0
    for t in tickers:
        try:
            r = seed_di_buffer(t)
            if r.get("di_after_seed") is not None:
                seeded += 1
            else:
                skipped += 1
        except Exception as e:
            logger.warning("DI_SEED %s crashed: %s", t, e)
            skipped += 1
    logger.info(
        "DI_SEED_DONE tickers=%d seeded_with_nonnull_di=%d skipped=%d",
        len(tickers), seeded, skipped,
    )


def seed_opening_range(ticker):
    """Seed or_high[ticker]/or_low[ticker]/pdc[ticker] from Alpaca
    historical 1m bars covering today's 09:30 ET \u2192 09:30+OR_WINDOW_MINUTES
    ET window. Returns dict with keys: or_high, or_low, bars_used.

    Only seeds when the OR window is complete (now_et >= window end).
    Pre-open or pre-9:35-ET restarts return bars_used=0 so the
    scheduled 09:35 ET collect_or() can run cleanly.
    """
    tg = _tg()
    result = {"or_high": None, "or_low": None, "bars_used": 0}
    client = tg._alpaca_data_client()
    if client is None:
        logger.debug("OR_SEED %s skipped \u2014 no alpaca data client", ticker)
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
        logger.debug("OR_SEED %s skipped \u2014 window not complete (now_et=%s < end=%s)",
                     ticker, now_et.strftime("%H:%M"),
                     window_end.strftime("%H:%M"))
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
        logger.warning("OR_SEED %s \u2014 no usable bars in window", ticker)
        return result

    tg.or_high[ticker] = max_hi
    tg.or_low[ticker] = min_lo
    result["or_high"] = max_hi
    result["or_low"] = min_lo
    result["bars_used"] = bars_used
    logger.info(
        "OR_SEED ticker=%s or_high=%.2f or_low=%.2f bars_used=%d "
        "window_et=%s-%s source=alpaca_historical",
        ticker, max_hi, min_lo, bars_used,
        window_start.strftime("%H:%M"), window_end.strftime("%H:%M"),
    )
    return result


def seed_opening_range_all(tickers):
    """Run seed_opening_range for every ticker and emit a summary.

    Marks or_collected_date=today once at least one ticker is seeded,
    so the scheduled 09:35 ET collect_or() does not overwrite the
    fresher Alpaca-sourced OR. Safe on a before-open restart \u2014
    returns immediately when the OR window is not yet complete.
    """
    tg = _tg()
    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    today = now_et.strftime("%Y-%m-%d")
    window_end = now_et.replace(hour=9, minute=30, second=0, microsecond=0) \
                    + timedelta(minutes=tg.OR_WINDOW_MINUTES)
    if now_et < window_end:
        logger.info(
            "OR_SEED_DONE tickers=0 seeded=0 skipped=%d \u2014 pre-OR-window",
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
        len(tickers), seeded, skipped,
    )


__all__ = [
    "qqq_regime_seed_once",
    "qqq_regime_tick",
    "seed_di_buffer",
    "seed_di_all",
    "seed_opening_range",
    "seed_opening_range_all",
]
