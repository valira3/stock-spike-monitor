"""v5.11.0 \u2014 engine.scan: per-minute scan loop.

Extracted verbatim from `trade_genius.py` (v5.10.7 lines 7811\u20138200).
Behavior is unchanged; the scan loop body is the same instructions in
the same order, with broker / Telegram / clock / position-store calls
routed through the `EngineCallbacks` Protocol so replay (PR 6) can drop
in a record-only mock.

Module-level state from trade_genius referenced inside the loop
(`_scan_idle_hours`, `_last_scan_time`, `positions`,
`short_positions`, `pdc`, `TRADE_TICKERS`, `V561_INDEX_TICKER`,
`_QQQ_REGIME`, `_ws_consumer`, `_current_mode`, `_scan_paused`) and
helpers (`_clear_cycle_bar_cache`, `_v561_archive_qqq_bar`,
`_v512_archive_minute_bar`, `_v590_qqq_regime_tick`,
`_v561_maybe_persist_or_snapshots`, `_update_gate_snapshot`,
`_opening_avwap`, `_v512_archive_minute_bar`) remain owned by trade_genius.py
and accessed through the live module via `_tg()` (the same pattern
seeders.py / phase_machine.py use). This avoids circular imports
during the v5.11.0 staged extraction.

The public entrypoint is `scan_loop(callbacks)`. trade_genius.py keeps
a thin `def scan_loop(): engine.scan.scan_loop(_ProdCallbacks())`
shim so any importer that resolves `trade_genius.scan_loop` keeps
working.
"""

from __future__ import annotations

import logging
import sys as _sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import v5_10_1_integration as eot_glue
import volume_profile

from engine.callbacks import EngineCallbacks

logger = logging.getLogger("trade_genius")


def _tg():
    """Live trade_genius module (handles __main__ vs imported cases)."""
    return _sys.modules.get("trade_genius") or _sys.modules.get("__main__")


def scan_loop(callbacks: EngineCallbacks) -> None:
    """Main scan: manage positions, check new entries. Runs every 60s."""
    tg = _tg()
    now_et = callbacks.now_et()

    # v4.4.1 \u2014 Refresh the MarketMode banner BEFORE the after-hours
    # early returns. Without this, once the clock crosses 15:55 ET the
    # cached _current_mode / _current_mode_reason stayed frozen on the
    # last pre-close values (e.g. POWER "14:00-15:55 ET") and /api/state
    # kept serving them until the next open. Pure observation \u2014 safe to
    # fail silently; it cannot affect trading. Runs at idle cycles too,
    # not just during trading cycles.
    try:
        tg._refresh_market_mode()
    except Exception:
        logger.exception(
            "_refresh_market_mode failed (ignored \u2014 observation only, runs at idle cycles too)"
        )

    # Idle-state flag drives gates.scan_paused on the dashboard so the
    # UI can tell "scanner is not scanning right now" after hours without
    # reading internal mode globals.
    is_weekend = now_et.weekday() >= 5
    before_open = now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 35)
    after_close = now_et.hour >= 16 or (now_et.hour == 15 and now_et.minute >= 55)
    tg._scan_idle_hours = bool(is_weekend or before_open or after_close)

    # Skip weekends
    if is_weekend:
        return

    # v5.6.1 D2(a) \u2014 pre-9:35 ET writer warm-up. Between 9:29:30 ET and
    # 9:35:00 ET we run a stripped-down archive pass so the 9:30:00 first
    # tick is captured (closes the OR window backfill gap). We skip the
    # full entry/manage scan (gates aren't active until 9:35) but persist
    # the 1m bar for QQQ + every TRADE_TICKER. Failure-tolerant.
    _pre_open_window = (
        now_et.hour == 9
        and 29 <= now_et.minute < 35
        and (now_et.minute > 29 or now_et.second >= 30)
    )
    if before_open and _pre_open_window:
        try:
            tg._clear_cycle_bar_cache()
            _qqq_pre = callbacks.fetch_1min_bars(tg.V561_INDEX_TICKER)
            if _qqq_pre:
                tg._v561_archive_qqq_bar(_qqq_pre)
            for _t_pre in tg.TRADE_TICKERS:
                try:
                    _b_pre = callbacks.fetch_1min_bars(_t_pre)
                    if not _b_pre:
                        continue
                    _closes_pre = _b_pre.get("closes") or []
                    _ts_arr_pre = _b_pre.get("timestamps") or []
                    _idx_pre = None
                    if len(_closes_pre) >= 2 and _closes_pre[-2] is not None:
                        _idx_pre = -2
                    elif len(_closes_pre) >= 1 and _closes_pre[-1] is not None:
                        _idx_pre = -1
                    if _idx_pre is None:
                        continue
                    _opens_pre = _b_pre.get("opens") or []
                    _highs_pre = _b_pre.get("highs") or []
                    _lows_pre = _b_pre.get("lows") or []
                    _vols_pre = _b_pre.get("volumes") or []
                    _ts_val_pre = (
                        _ts_arr_pre[_idx_pre] if abs(_idx_pre) <= len(_ts_arr_pre) else None
                    )
                    try:
                        _ts_iso_pre = (
                            datetime.utcfromtimestamp(int(_ts_val_pre)).strftime(
                                "%Y-%m-%dT%H:%M:%SZ"
                            )
                            if _ts_val_pre is not None
                            else None
                        )
                    except Exception:
                        _ts_iso_pre = None
                    _bar_pre = {
                        "ts": _ts_iso_pre,
                        "et_bucket": None,
                        "open": _opens_pre[_idx_pre] if abs(_idx_pre) <= len(_opens_pre) else None,
                        "high": _highs_pre[_idx_pre] if abs(_idx_pre) <= len(_highs_pre) else None,
                        "low": _lows_pre[_idx_pre] if abs(_idx_pre) <= len(_lows_pre) else None,
                        "close": _closes_pre[_idx_pre],
                        "iex_volume": _vols_pre[_idx_pre]
                        if abs(_idx_pre) <= len(_vols_pre)
                        else None,
                        "iex_sip_ratio_used": None,
                        "bid": None,
                        "ask": None,
                        "last_trade_price": _b_pre.get("current_price"),
                    }
                    tg._v512_archive_minute_bar(_t_pre, _bar_pre)
                except Exception as _e_pre:
                    logger.warning("[V561-PREOPEN-BAR] %s: %s", _t_pre, _e_pre)
        except Exception as _e_pre_outer:
            logger.warning("[V561-PREOPEN] cycle hook error: %s", _e_pre_outer)
        # Pre-open: archive only, no entry/manage. Return after archive.
        return

    # Skip outside market hours (09:35 - 15:55 ET)
    if before_open or after_close:
        return

    cycle_start = time.time()
    tg._last_scan_time = datetime.now(timezone.utc)

    # Clear the per-cycle 1-min bar cache BEFORE anything else. Any call
    # to fetch_1min_bars inside this cycle will populate it on first hit
    # and reuse on subsequent hits. Observers read through the same cache.
    tg._clear_cycle_bar_cache()

    n_pos = len(tg.positions)
    n_short = len(tg.short_positions)
    logger.info(
        "Scanning %d stocks | pos=%d short=%d | mode=%s",
        len(tg.TRADE_TICKERS),
        n_pos,
        n_short,
        tg._current_mode,
    )

    # v5.13.9 \u2014 The PDC-anchored "REGIME: BULLISH/BEARISH" alert that
    # used to live here was retired. It compared SPY/QQQ 1m current
    # price to prior-day-close, which is not part of the Tiger Sovereign
    # spec (STEP 1 = QQQ 5m vs 9 EMA, STEP 2 = QQQ 5m vs 09:30 AVWAP)
    # and was decorative-only \u2014 no entry, exit, or sentinel path
    # consumed it. Removed alongside the matching `_update_gate_snapshot`
    # rewire so the dashboard `index`/`polarity` pills mirror the
    # actual gates the entry path uses.

    # v5.6.1 D1 \u2014 archive QQQ 1m bar each cycle so the index ticker is
    # persisted alongside the 8 trade tickers. Failure-tolerant; never
    # blocks the scan.
    try:
        _qqq_bars_archive = callbacks.fetch_1min_bars(tg.V561_INDEX_TICKER)
        if _qqq_bars_archive:
            tg._v561_archive_qqq_bar(_qqq_bars_archive)
    except Exception as _e:
        logger.warning("[V561-QQQ-BAR] cycle hook error: %s", _e)

    # v5.9.0 \u2014 advance QQQ Regime Shield on freshly closed 5m bars and
    # emit [V572-REGIME] log on each new bar. Seed-on-first-call behavior
    # is handled inside _v590_qqq_regime_tick / _v590_qqq_regime_seed_once.
    try:
        tg._v590_qqq_regime_tick()
    except Exception as _e:
        logger.warning("[V572-REGIME] cycle hook error: %s", _e)

    # v5.6.1 D2 \u2014 persist OR_High/OR_Low snapshots once per ticker per
    # session, after 9:35 ET when the OR window is closed and the gate
    # code's or_high/or_low dicts are seeded.
    try:
        tg._v561_maybe_persist_or_snapshots(now_et=now_et)
    except Exception as _e:
        logger.warning("[V561-OR-SNAP] cycle hook error: %s", _e)

    # v5.10.1 \u2014 Volume Bucket baseline refresh (once per session at
    # 9:29 ET) and Section I [V5100-PERMIT] state-change log emit.
    try:
        eot_glue.refresh_volume_baseline_if_needed(now_et)
    except Exception as _e:
        logger.warning("[V5100-VOLBUCKET] refresh hook error: %s", _e)
    try:
        _qqq_for_permit = callbacks.fetch_1min_bars(tg.V561_INDEX_TICKER)
        _qqq_cur = (_qqq_for_permit or {}).get("current_price") if _qqq_for_permit else None
        _qqq_5m_close = tg._QQQ_REGIME.last_close
        _qqq_5m_ema9 = tg._QQQ_REGIME.ema9
        _qqq_avwap = tg._opening_avwap("QQQ")
        eot_glue.maybe_log_permit_state(
            _qqq_5m_close,
            _qqq_5m_ema9,
            _qqq_cur,
            _qqq_avwap,
        )
    except Exception as _e:
        logger.warning("[V5100-PERMIT] log hook error: %s", _e)

    # Always manage existing positions (stops/trails) even when paused
    try:
        callbacks.manage_positions()
    except Exception as e:
        # v4.11.0 \u2014 report_error replaces the previous logger.error +
        # ad-hoc send_telegram pair. The Telegram message now follows
        # the unified \u226434-char-line health-pill format and is gated
        # by the per-(executor,code) 5-min dedup.
        callbacks.report_error(
            executor="main",
            code="MANAGE_POSITIONS_EXCEPTION",
            severity="error",
            summary="manage_positions crashed",
            detail=f"{type(e).__name__}: {str(e)[:200]}",
        )
    try:
        callbacks.manage_short_positions()
    except Exception as e:
        callbacks.report_error(
            executor="main",
            code="MANAGE_SHORT_POSITIONS_EXCEPTION",
            severity="error",
            summary="manage_short_positions crashed",
            detail=f"{type(e).__name__}: {str(e)[:200]}",
        )

    # v5.10.0 \u2014 Project Eye of the Tiger formally retires HARD_EJECT_TIGER
    # (legacy DI<25 hard eject). The Section V triple-lock stops
    # (Maffei 1-2-3 / Layered Shield + Two-Bar Lock / The Leash) plus
    # the Section IV tick overrides (sovereign_brake / velocity_fuse)
    # are the sole exit authority. Function is retained for backwards
    # compatibility but is no longer invoked.
    pass  # _tiger_hard_eject_check retired \u2014 v5.10.0 Section V owns exits

    # Feature 8: scan pause \u2014 only block NEW entries
    if tg._scan_paused:
        logger.info(
            "SCAN CYCLE done in %.2fs \u2014 paused (manage only)", time.time() - cycle_start
        )
        return

    # Check for new entries on tradable tickers (long + short).
    # v3.4.40 \u2014 paper and Robinhood are now evaluated INDEPENDENTLY.
    # check_entry() is the shared signal/indicator gate; the portfolio-
    # side decision (halt, cash, concurrency, per-ticker cap) is per-
    # book. A paper-held ticker no longer blocks RH from entering, and
    # vice versa.
    for ticker in tg.TRADE_TICKERS:
        _per_ticker_tick(callbacks, ticker)

    logger.info(
        "SCAN CYCLE done in %.2fs \u2014 %d tickers",
        time.time() - cycle_start,
        len(tg.TRADE_TICKERS),
    )


def _per_ticker_tick(callbacks: EngineCallbacks, ticker: str) -> None:
    """Per-ticker body of the scan loop.

    Extracted from the inline `for ticker in TRADE_TICKERS:` block so the
    structural seam (callbacks) is visible at function granularity.
    Behavior is byte-equal to the inline original.
    """
    tg = _tg()
    # Refresh the dashboard gate snapshot from the current OR
    # envelope before any entry gates run. Side + break are derived
    # purely from OR vs price each cycle (no latch).
    try:
        tg._update_gate_snapshot(ticker)
    except Exception as e:
        logger.error("_update_gate_snapshot error %s: %s", ticker, e)
    # Long entry check \u2014 run once per ticker and fan out to both books.
    try:
        # _bars_for_mtm is still
        # fetched here because the bar archive write below depends on
        # it; the fetch_1min_bars call is cached by _cycle_bar_cache so
        # the cost is essentially zero.
        try:
            _bars_for_mtm = callbacks.fetch_1min_bars(ticker)
        except Exception as e:
            logger.warning("[V510-BAR] fetch hook %s: %s", ticker, e)
            _bars_for_mtm = None
        # v5.5.2 \u2014 persist the most-recently-completed 1m bar to
        # /data/bars/YYYY-MM-DD/{TICKER}.jsonl so the offline backtest
        # CLI has something to replay. fetch_1min_bars already cached
        # by _cycle_bar_cache so this is free; we project the parallel
        # arrays onto the canonical bar_archive.BAR_SCHEMA_FIELDS dict
        # before passing. Wrapped in its own try/except: a write
        # failure must never disrupt the trading scan.
        try:
            if _bars_for_mtm:
                closes = _bars_for_mtm.get("closes") or []
                ts_arr = _bars_for_mtm.get("timestamps") or []
                # Prefer the second-to-last entry (last is often the
                # currently-forming bar); fall back to the last when
                # only one bar is available.
                idx = None
                if len(closes) >= 2 and closes[-2] is not None:
                    idx = -2
                elif len(closes) >= 1 and closes[-1] is not None:
                    idx = -1
                if idx is not None:
                    opens = _bars_for_mtm.get("opens") or []
                    highs = _bars_for_mtm.get("highs") or []
                    lows = _bars_for_mtm.get("lows") or []
                    vols = _bars_for_mtm.get("volumes") or []
                    ts_val = ts_arr[idx] if abs(idx) <= len(ts_arr) else None
                    try:
                        ts_iso = (
                            datetime.utcfromtimestamp(int(ts_val)).strftime("%Y-%m-%dT%H:%M:%SZ")
                            if ts_val is not None
                            else None
                        )
                    except Exception:
                        ts_iso = None
                    # v5.5.5 \u2014 prefer the WS consumer's IEX volume for
                    # the current bucket. Yahoo's intraday endpoint
                    # frequently returns volume=0/null on the leading-edge
                    # bar, leaving the offline backtest CLI replaying
                    # against zeroes. Fall back to the Yahoo value when
                    # the WS path is unavailable, outside RTH, or has
                    # not yet captured this bucket.
                    yahoo_vol = vols[idx] if abs(idx) <= len(vols) else None
                    iex_volume = yahoo_vol
                    et_bucket: str | None = None
                    try:
                        now_et = datetime.now(tz=ZoneInfo("America/New_York"))
                        et_bucket = volume_profile.session_bucket(now_et)
                        if et_bucket is not None and tg._ws_consumer is not None:
                            ws_vol = tg._ws_consumer.current_volume(
                                ticker,
                                et_bucket,
                            )
                            if ws_vol is not None:
                                iex_volume = int(ws_vol)
                    except Exception as _e:
                        # Never let observability break the trading scan.
                        logger.warning(
                            "[V510-BAR] ws-source switch %s: %s",
                            ticker,
                            _e,
                        )
                    canon_bar = {
                        "ts": ts_iso,
                        "et_bucket": et_bucket,
                        "open": opens[idx] if abs(idx) <= len(opens) else None,
                        "high": highs[idx] if abs(idx) <= len(highs) else None,
                        "low": lows[idx] if abs(idx) <= len(lows) else None,
                        "close": closes[idx],
                        "iex_volume": iex_volume,
                        "iex_sip_ratio_used": None,
                        "bid": None,
                        "ask": None,
                        "last_trade_price": _bars_for_mtm.get("current_price"),
                    }
                    tg._v512_archive_minute_bar(ticker, canon_bar)
        except Exception as e:
            logger.warning("[V510-BAR] archive hook %s: %s", ticker, e)
        # Fast path: if paper already holds this ticker, skip the
        # signal compute. Otherwise run check_entry so the signal
        # decision is made once for the scan cycle.
        paper_holds = callbacks.has_long(ticker)
        # v5.10.1 \u2014 record the just-closed 1m bar's close into the
        # rolling buffer used by Boundary Hold (Section II.2). This
        # is needed regardless of whether we currently hold the
        # ticker so the buffer keeps tracking through trade
        # lifecycles.
        # v5.20.4 \u2014 swap the inline ``closes[-2] is not None`` guard
        # for ``record_latest_1m_close``, which walks back from
        # ``[-2]`` to find the newest non-None close (Yahoo keeps
        # a forming-bar None at ``[-2]`` for nearly the whole RTH
        # session, which silently starved the boundary buffer for
        # every ticker every cycle prior to this fix).
        try:
            if _bars_for_mtm:
                eot_glue.record_latest_1m_close(ticker, _bars_for_mtm.get("closes") or [])
        except Exception as _e:
            logger.warning("[V5100-BOUNDARY] record_1m_close %s: %s", ticker, _e)
        if not paper_holds:
            ok, bars = callbacks.check_entry(ticker)
            if ok and bars:
                px = bars["current_price"]
                try:
                    callbacks.execute_entry(ticker, px)
                except Exception as e:
                    # v4.11.0 \u2014 report_error: paper-book entry
                    # exception. Operator should know why a long
                    # signal failed to execute.
                    callbacks.report_error(
                        executor="main",
                        code="PAPER_ENTRY_EXCEPTION",
                        severity="error",
                        summary=f"Paper entry exception: {ticker}",
                        detail=f"{type(e).__name__}: {str(e)[:200]}",
                    )
    except Exception as e:
        logger.error("Entry check error %s: %s", ticker, e)
    # Short entry check (Wounded Buffalo) \u2014 same call/execute pattern as long.
    try:
        paper_short_holds = callbacks.has_short(ticker)
        if not paper_short_holds:
            ok, bars = callbacks.check_short_entry(ticker)
            if ok and bars:
                px = bars["current_price"]
                try:
                    callbacks.execute_short_entry(ticker, px)
                except Exception as e:
                    # v4.11.0 \u2014 report_error: paper-book short
                    # entry exception.
                    callbacks.report_error(
                        executor="main",
                        code="PAPER_SHORT_ENTRY_EXCEPTION",
                        severity="error",
                        summary=f"Paper short entry exception: {ticker}",
                        detail=f"{type(e).__name__}: {str(e)[:200]}",
                    )
    except Exception as e:
        logger.error("Short entry check error %s: %s", ticker, e)
