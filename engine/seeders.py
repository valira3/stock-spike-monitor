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


# v5.13.8 \u2014 minimum bar count for the archive fast path. EMA9 needs
# 9 closed 5m bars to be defined; if the archive has fewer than that,
# fall through to the Alpaca historical fetch (which can pull the full
# pre-market window) before settling on the partial archive read.
MIN_ARCHIVE_BARS = 9

# v5.20.2 \u2014 minimum closed 5m bars required to fully warm EMA9. Used by
# qqq_regime_seed_once and the 09:31 ET / live-tick recompute paths to
# decide whether the regime is "hot" (idempotent seal allowed) or still
# "warming" (callers should retry).
QQQ_REGIME_MIN_BARS_FOR_EMA9 = 9


def qqq_regime_seed_once(force_reseed: bool = False) -> None:
    """v5.9.0 \u2014 Seed the QQQ Regime EMAs from pre-market 5m bars.

    Source priority (per spec):
      1. /data/bars/<today>/QQQ.jsonl bar archive  (≥ MIN_ARCHIVE_BARS)
      2. Alpaca historical 5m bars (IEX feed) for today 04:00 ET \u2192 now
      3. Prior session's last 5m bars
      4. Partial archive (< MIN_ARCHIVE_BARS) \u2014 last-resort, only if
         Alpaca and prior-session fallbacks both fail

    v5.13.8 fix: previously any non-empty archive return short-circuited
    the orchestration. On cold starts where the archive only contained
    a handful of RTH-open bars, this prevented Alpaca from supplying
    the pre-market 04:00 ET window and left ema9 \u201cwarming up\u201d for
    the first \u224825 minutes of the session \u2014 exactly the volatile
    window the permit gate needs. We now require \u22659 bars from archive
    before treating it as authoritative.

    v5.20.2 fix: idempotency is no longer permanent on first call. The
    seal flag (_QQQ_REGIME_SEEDED=True) is now set ONLY after ema9 has
    actually warmed (≥9 closed 5m bars applied). If the first attempt
    pulled <9 bars, the regime stays unsealed and subsequent callers
    (premarket_recalc 09:29, the new 09:31 recompute, and the live tick
    gap-fill in qqq_regime_tick) will retry until ema9 is non-None.
    Pass `force_reseed=True` to bypass the seal even when set \u2014 used
    by the recompute paths so a partial seed can be replaced by a
    later, larger one.

    Failure-tolerant: any crash leaves the regime un-seeded, in which
    case the live tick feed will warm up the EMAs naturally and the
    next gap-fill tick will retry the seed.
    """
    tg = _tg()
    # v5.20.2: only honor the seal when ema9 actually warmed. A stale
    # half-warm seal (set by old behavior) can still be unblocked via
    # force_reseed; otherwise we fall through and try again every call.
    already_warm = tg._QQQ_REGIME.ema9 is not None
    if tg._QQQ_REGIME_SEEDED and already_warm and not force_reseed:
        return

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    today_0400 = now_et.replace(hour=4, minute=0, second=0, microsecond=0)

    archive_closes = _qqq_seed_from_archive(today_0400, now_et)
    closes = None
    source = None
    if archive_closes and len(archive_closes) >= MIN_ARCHIVE_BARS:
        closes = archive_closes
        source = "archive"
    else:
        if archive_closes:
            logger.info(
                "[V572-REGIME-SEED] archive has %d bars (< %d minimum); "
                "falling through to Alpaca historical",
                len(archive_closes),
                MIN_ARCHIVE_BARS,
            )
        closes = _qqq_seed_from_alpaca(today_0400, now_et)
        if closes:
            source = "alpaca"
        else:
            closes = _qqq_seed_from_prior_session(now_et)
            if closes:
                source = "prior_session"
            elif archive_closes:
                # Last resort: use the partial archive read.
                closes = archive_closes
                source = "archive_partial"

    if not closes:
        logger.warning(
            "[V572-REGIME-SEED] no source returned bars; compass will warm up from live ticks"
        )
        # v5.20.2: do NOT seal on empty fetch; let later passes retry.
        return

    # v5.20.2: when force_reseed is set we wipe regime state first so the
    # fresh seed is authoritative and not blended into a half-warm one.
    if force_reseed:
        tg._QQQ_REGIME.ema3 = None
        tg._QQQ_REGIME.ema9 = None
        tg._QQQ_REGIME._seed_buf3 = []
        tg._QQQ_REGIME._seed_buf9 = []
        tg._QQQ_REGIME.bars_seen = 0

    n = tg._QQQ_REGIME.seed(closes, source)
    # v5.20.2: only seal when ema9 actually warmed (≥9 bars applied).
    if tg._QQQ_REGIME.ema9 is not None:
        tg._QQQ_REGIME_SEEDED = True
    compass = tg._QQQ_REGIME.current_compass()
    logger.info(
        "[V572-REGIME-SEED] source=%s bars=%d ema3=%s ema9=%s compass=%s sealed=%s",
        source,
        n,
        ("%.4f" % tg._QQQ_REGIME.ema3) if tg._QQQ_REGIME.ema3 is not None else "None",
        ("%.4f" % tg._QQQ_REGIME.ema9) if tg._QQQ_REGIME.ema9 is not None else "None",
        compass if compass is not None else "None",
        "Y" if tg._QQQ_REGIME_SEEDED else "N",
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
                        epoch = int(
                            datetime.strptime(
                                ts.replace("Z", ""),
                                "%Y-%m-%dT%H:%M:%S",
                            )
                            .replace(tzinfo=timezone.utc)
                            .timestamp()
                        )
                    else:
                        epoch = int(ts)
                except Exception:
                    continue
                if epoch < int(start_et.timestamp()) or epoch >= int(end_et.timestamp()):
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
        pairs = [
            (int(t), float(c))
            for t, c in zip(timestamps, closes)
            if t is not None and c is not None
        ]
        if not pairs:
            return
        pairs.sort(key=lambda p: p[0])
        buckets = {}
        for ts, c in pairs:
            buckets[ts // 300] = c
        ordered = sorted(buckets.keys())
        if len(ordered) < 2:
            return
        finalized = ordered[:-1]  # drop newest, possibly forming
        last_bucket = finalized[-1]
        if tg._QQQ_REGIME_LAST_BUCKET is not None and last_bucket <= tg._QQQ_REGIME_LAST_BUCKET:
            return
        # Seed before applying the first live bar so seed math runs first.
        qqq_regime_seed_once()
        # v5.20.2 gap-fill: if ema9 is still None at this point (premarket
        # source had <9 bars on every prior pass), force-reseed using
        # whatever data is now available before applying today's live
        # close. This lets the regime self-heal mid-session without
        # waiting for live ticks to organically accumulate 9 bars.
        if tg._QQQ_REGIME.ema9 is None:
            try:
                qqq_regime_seed_once(force_reseed=True)
            except Exception as _e:
                logger.warning("[V572-REGIME] gap-fill reseed failed: %s", _e)
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


# v5.20.1 \u2014 premarket DI seed window. Spec: collect \u226515 5m bars from
# today's premarket so DI is armed BEFORE the 09:36 ET entry window opens.
# Window is the last 90 minutes before the open (08:00\u201309:30 ET = 18
# 5m buckets), giving 3 bars of headroom above the 15-bar DI threshold.
# The prior-day 14:50\u219216:00 ET tail-seed path was removed: it polluted
# DI with stale momentum from the previous session and produced di=None
# anyway when premarket was empty (only 14 buckets < the 16-bar minimum
# tiger_di() needs internally).
PREMARKET_DI_WINDOW_START_HHMM = (8, 0)  # 08:00 ET
PREMARKET_DI_WINDOW_END_HHMM = (9, 30)  # 09:30 ET
PREMARKET_DI_MIN_BARS = 15  # must equal DI_PERIOD


def seed_di_buffer(ticker):
    """Seed the DI 5m buffer for `ticker` from today's premarket bars.

    v5.20.1 (premarket-only): fetches Alpaca 1m bars in the
    08:00\u219209:30 ET window, buckets to 5m, and writes
    `_DI_SEED_CACHE[ticker]` chronologically. No prior-day fallback;
    if premarket has <15 bars the cache is left UNSET so the 09:31 ET
    recompute can retry once today's first 5m RTH bar (09:30:00\u219209:34:59)
    is available. tiger_di() merges seed + live 5m buckets, so the first
    RTH bar lifts seed-bar count from N to N+1, and DI starts producing
    values once that combined count crosses 15.

    If the DI_PREMARKET_SEED env flag is "0", DI seeding is fully
    disabled (DI warms up from live ticks only \u2014 ~75 min of RTH).

    Safe to call on restart mid-session. Idempotent within a session
    once \u226515 bars are present (writes the same buffer). On any Alpaca
    failure logs a warning and continues.

    Returns dict {"bars_premarket": N, "di_after_seed": float|None,
                  "window_et": "HH:MM-HH:MM", "sufficient": bool}.
    """
    tg = _tg()
    result = {
        "bars_premarket": 0,
        "di_after_seed": None,
        "window_et": "08:00-09:30",
        "sufficient": False,
    }
    client = tg._alpaca_data_client()
    if client is None:
        logger.debug("DI_SEED %s skipped \u2014 no alpaca data client", ticker)
        return result

    premarket_on = os.getenv("DI_PREMARKET_SEED", "1").strip() not in (
        "0",
        "false",
        "False",
        "",
    )
    if not premarket_on:
        logger.debug("DI_SEED %s skipped \u2014 DI_PREMARKET_SEED disabled", ticker)
        return result

    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
    except Exception as e:
        logger.debug("DI_SEED %s import failed: %s", ticker, e)
        return result

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    win_start = now_et.replace(
        hour=PREMARKET_DI_WINDOW_START_HHMM[0],
        minute=PREMARKET_DI_WINDOW_START_HHMM[1],
        second=0,
        microsecond=0,
    )
    win_end = now_et.replace(
        hour=PREMARKET_DI_WINDOW_END_HHMM[0],
        minute=PREMARKET_DI_WINDOW_END_HHMM[1],
        second=0,
        microsecond=0,
    )
    # Fetch upper bound is min(now, 09:30 ET) so a pre-08:00 boot logs
    # bars_premarket=0 cleanly instead of pulling RTH bars by accident.
    fetch_end = min(now_et, win_end)

    try:
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Minute,
            start=win_start.astimezone(timezone.utc),
            end=fetch_end.astimezone(timezone.utc),
            feed="iex",
        )
        resp = client.get_stock_bars(req)
        data = getattr(resp, "data", {}) or {}
        rows = data.get(ticker, []) or []
    except Exception as e:
        logger.warning(
            "DI_SEED %s alpaca premarket fetch %s\u2192%s failed: %s",
            ticker,
            win_start,
            fetch_end,
            e,
        )
        return result

    # Bucket 1m rows into 5m OHLC.
    win_start_ts = int(win_start.timestamp())
    win_end_ts = int(win_end.timestamp())
    pre_buckets = {}
    for row in rows:
        ts = getattr(row, "timestamp", None)
        if ts is None:
            continue
        try:
            epoch = int(ts.timestamp())
        except Exception:
            continue
        if epoch < win_start_ts or epoch >= win_end_ts:
            continue
        h = float(getattr(row, "high", 0) or 0)
        lo = float(getattr(row, "low", 0) or 0)
        c = float(getattr(row, "close", 0) or 0)
        if h <= 0 or lo <= 0 or c <= 0:
            continue
        bucket = epoch // 300
        if bucket not in pre_buckets:
            pre_buckets[bucket] = {"bucket": bucket, "high": h, "low": lo, "close": c}
        else:
            pre_buckets[bucket]["high"] = max(pre_buckets[bucket]["high"], h)
            pre_buckets[bucket]["low"] = min(pre_buckets[bucket]["low"], lo)
            pre_buckets[bucket]["close"] = c

    # Drop newest bucket if it could still be forming.
    ordered = sorted(pre_buckets.keys())
    if ordered:
        last_end_ts = (ordered[-1] + 1) * 300
        if int(now_et.timestamp()) < last_end_ts:
            ordered = ordered[:-1]
    final_list = [pre_buckets[b] for b in ordered]
    result["bars_premarket"] = len(final_list)

    # Only commit to cache if we have enough bars to actually arm DI.
    # Otherwise leave _DI_SEED_CACHE[ticker] unset so a later recompute
    # (or live-tick warmup) can supersede this attempt cleanly.
    sufficient = len(final_list) >= PREMARKET_DI_MIN_BARS
    if sufficient:
        tg._DI_SEED_CACHE[ticker] = final_list
        result["sufficient"] = True
        if len(final_list) >= tg.DI_PERIOD + 1:
            highs = [b["high"] for b in final_list]
            lows = [b["low"] for b in final_list]
            closes = [b["close"] for b in final_list]
            dp, _dm = tg._compute_di(highs, lows, closes)
            result["di_after_seed"] = dp

    logger.info(
        "DI_SEED ticker=%s window_et=%s bars_premarket=%d sufficient=%s di_after_seed=%s",
        ticker,
        result["window_et"],
        result["bars_premarket"],
        "Y" if sufficient else "N",
        ("%.2f" % result["di_after_seed"]) if result["di_after_seed"] is not None else "None",
    )
    return result


def seed_di_all(tickers):
    """Run seed_di_buffer for every ticker and emit a summary line.

    v5.20.1: a ticker is counted as `seeded_with_sufficient_premarket`
    when premarket yielded \u226515 5m bars (the threshold for arming DI
    immediately at 09:30 ET). Tickers with insufficient premarket
    will be re-tried by the 09:31 ET recompute job, which can include
    today's first RTH 5m bar and typically clears the threshold.
    """
    seeded = 0
    insufficient = 0
    for t in tickers:
        try:
            r = seed_di_buffer(t)
            if r.get("sufficient"):
                seeded += 1
            else:
                insufficient += 1
        except Exception as e:
            logger.warning("DI_SEED %s crashed: %s", t, e)
            insufficient += 1
    logger.info(
        "DI_SEED_DONE tickers=%d seeded_with_sufficient_premarket=%d insufficient=%d",
        len(tickers),
        seeded,
        insufficient,
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
        logger.debug(
            "OR_SEED %s skipped \u2014 window not complete (now_et=%s < end=%s)",
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
    fresher Alpaca-sourced OR. Safe on a before-open restart \u2014
    returns immediately when the OR window is not yet complete.
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
        len(tickers),
        seeded,
        skipped,
    )


def recompute_di_for_unseeded(tickers):
    """v5.20.1 \u2014 09:31 ET recompute pass.

    Re-runs seed_di_buffer ONLY for tickers whose _DI_SEED_CACHE entry is
    still missing or empty (i.e. premarket-only seed didn't reach the
    \u226515 bar threshold at 09:29 ET). At 09:31 ET the seeder fetch window
    has closed (we never read past 09:30 ET), but tiger_di() will see
    today's freshly-arrived 5m RTH bars from the live tick path on its
    next call \u2014 the seed itself is just one half of the merged buffer.

    Idempotent: tickers already seeded are left alone. Non-fatal: per-
    ticker exceptions are logged and skipped.

    Returns dict {"recomputed": N, "already_seeded": N, "failed": N}.
    """
    tg = _tg()
    cache = getattr(tg, "_DI_SEED_CACHE", {}) or {}
    recomputed = 0
    already_seeded = 0
    failed = 0
    for t in tickers:
        existing = cache.get(t) or []
        if len(existing) >= PREMARKET_DI_MIN_BARS:
            already_seeded += 1
            continue
        try:
            seed_di_buffer(t)
            recomputed += 1
        except Exception:
            logger.exception("DI_RECOMPUTE %s crashed", t)
            failed += 1
    logger.info(
        "[DI-RECOMPUTE-0931] tickers=%d recomputed=%d already_seeded=%d failed=%d",
        len(tickers),
        recomputed,
        already_seeded,
        failed,
    )
    return {"recomputed": recomputed, "already_seeded": already_seeded, "failed": failed}


def recompute_qqq_regime_if_unwarm():
    """v5.20.2 \u2014 09:31 ET safety net for the QQQ regime EMA9.

    Mirror of recompute_di_for_unseeded but for the QQQ regime. Fires
    at 09:31 ET (1 minute after the bell) and re-runs qqq_regime_seed_once
    with force_reseed=True if ema9 is still None. By 09:31 ET today's
    first 5m bar (09:30:00→09:34:59) is forming, but the seeder's
    fetch window covers 04:00→now ET so this pass picks up any
    premarket bars Alpaca had not yet aggregated when the previous
    boot/recalc ran (cold-start within ~30s of the bell is the typical
    scenario where bars=4 from premarket leaves ema9=None).

    Idempotent: a fully-warm regime (ema9 non-None) short-circuits.
    Non-fatal: per-attempt exceptions are logged and swallowed.

    Returns dict {"reseeded": bool, "already_warm": bool, "failed": bool,
                  "ema9": float|None, "bars_seen": int}.
    """
    tg = _tg()
    if tg._QQQ_REGIME.ema9 is not None:
        logger.info(
            "[QQQ-REGIME-RECOMPUTE-0931] already warm bars_seen=%d ema9=%.4f",
            tg._QQQ_REGIME.bars_seen,
            tg._QQQ_REGIME.ema9,
        )
        return {
            "reseeded": False,
            "already_warm": True,
            "failed": False,
            "ema9": tg._QQQ_REGIME.ema9,
            "bars_seen": tg._QQQ_REGIME.bars_seen,
        }
    try:
        qqq_regime_seed_once(force_reseed=True)
    except Exception:
        logger.exception("[QQQ-REGIME-RECOMPUTE-0931] reseed crashed")
        return {
            "reseeded": False,
            "already_warm": False,
            "failed": True,
            "ema9": tg._QQQ_REGIME.ema9,
            "bars_seen": tg._QQQ_REGIME.bars_seen,
        }
    logger.info(
        "[QQQ-REGIME-RECOMPUTE-0931] reseed bars_seen=%d ema9=%s",
        tg._QQQ_REGIME.bars_seen,
        ("%.4f" % tg._QQQ_REGIME.ema9) if tg._QQQ_REGIME.ema9 is not None else "None",
    )
    return {
        "reseeded": True,
        "already_warm": False,
        "failed": False,
        "ema9": tg._QQQ_REGIME.ema9,
        "bars_seen": tg._QQQ_REGIME.bars_seen,
    }


__all__ = [
    "qqq_regime_seed_once",
    "qqq_regime_tick",
    "recompute_qqq_regime_if_unwarm",
    "QQQ_REGIME_MIN_BARS_FOR_EMA9",
    "seed_di_buffer",
    "seed_di_all",
    "recompute_di_for_unseeded",
    "seed_opening_range",
    "seed_opening_range_all",
    "PREMARKET_DI_WINDOW_START_HHMM",
    "PREMARKET_DI_WINDOW_END_HHMM",
    "PREMARKET_DI_MIN_BARS",
]
