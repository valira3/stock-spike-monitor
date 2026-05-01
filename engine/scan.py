"""v5.26.0 \u2014 engine.scan: per-minute scan loop (spec-strict).

Stage 3 of the Tiger Sovereign v15.0 spec-strict cut deleted Volume
Bucket / Volume-Baseline / Permit-state observability, regime-shield
gating telemetry, and the V510/V5100/V561/V572 log-tag clusters. What
remains: the QQQ 5m bar walk + EMA9 emission required by BL-1 / BU-1
Weather (per RULING #5), per-cycle 1m bar archival, position
management calls, and the per-ticker entry tick.

State owned by trade_genius referenced inside the loop: `positions`,
`short_positions`, `TRADE_TICKERS`, `V561_INDEX_TICKER`,
`_QQQ_REGIME`, `_ws_consumer`, `_scan_paused`, `_scan_idle_hours`,
`_last_scan_time`, `_current_mode`. Helpers: `_clear_cycle_bar_cache`,
`_v561_archive_qqq_bar`, `_v512_archive_minute_bar`,
`_qqq_weather_tick`, `_v561_maybe_persist_or_snapshots`,
`_update_gate_snapshot`. All accessed via `_tg()`.
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

    try:
        tg._refresh_market_mode()
    except Exception:
        logger.exception("_refresh_market_mode failed (observation only)")

    is_weekend = now_et.weekday() >= 5
    before_open = now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 35)
    after_close = now_et.hour >= 16 or (now_et.hour == 15 and now_et.minute >= 55)
    tg._scan_idle_hours = bool(is_weekend or before_open or after_close)

    if is_weekend:
        return

    # Pre-9:35 ET writer warm-up: archive the 09:30 first tick so the OR
    # window backfill gap is closed. No entries / no manage in this window.
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
                    logger.warning("[bar] preopen %s: %s", _t_pre, _e_pre)
        except Exception as _e_pre_outer:
            logger.warning("[scan] preopen cycle hook error: %s", _e_pre_outer)
        return

    if before_open or after_close:
        return

    cycle_start = time.time()
    tg._last_scan_time = datetime.now(timezone.utc)

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

    try:
        _qqq_bars_archive = callbacks.fetch_1min_bars(tg.V561_INDEX_TICKER)
        if _qqq_bars_archive:
            tg._v561_archive_qqq_bar(_qqq_bars_archive)
    except Exception as _e:
        logger.warning("[bar] qqq cycle hook error: %s", _e)

    # RULING #5: keep QQQ 5m bar walk + EMA9 emission for BL-1 / BU-1
    # Weather. Renamed neutrally; non-spec regime-shield gating deleted.
    try:
        tg._qqq_weather_tick()
    except Exception as _e:
        logger.warning("[regime] cycle hook error: %s", _e)

    try:
        tg._v561_maybe_persist_or_snapshots(now_et=now_et)
    except Exception as _e:
        logger.warning("[scan] OR-snap cycle hook error: %s", _e)

    try:
        callbacks.manage_positions()
    except Exception as e:
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

    if tg._scan_paused:
        logger.info(
            "SCAN CYCLE done in %.2fs (paused, manage only)", time.time() - cycle_start
        )
        return

    for ticker in tg.TRADE_TICKERS:
        _per_ticker_tick(callbacks, ticker)

    logger.info(
        "SCAN CYCLE done in %.2fs, %d tickers",
        time.time() - cycle_start,
        len(tg.TRADE_TICKERS),
    )


def _per_ticker_tick(callbacks: EngineCallbacks, ticker: str) -> None:
    """Per-ticker body of the scan loop."""
    tg = _tg()
    # RULING #8 defers _update_gate_snapshot cleanup to the cascade-fix
    # pass; left in place for now.
    try:
        tg._update_gate_snapshot(ticker)
    except Exception as e:
        logger.error("_update_gate_snapshot error %s: %s", ticker, e)
    try:
        try:
            _bars_for_mtm = callbacks.fetch_1min_bars(ticker)
        except Exception as e:
            logger.warning("[bar] fetch hook %s: %s", ticker, e)
            _bars_for_mtm = None
        try:
            if _bars_for_mtm:
                closes = _bars_for_mtm.get("closes") or []
                ts_arr = _bars_for_mtm.get("timestamps") or []
                idx = None
                if len(closes) >= 2 and closes[-2] is not None:
                    idx = -2
                elif len(closes) >= 1 and closes[-1] is not None:
                    idx = -1
                if idx is not None:
                    opens = _bars_for_mtm.get("opens") or []
                    highs = _bars_for_mtm.get("highs") or []
                    lows = _bars_for_mtm.get("lows") or []
                    ts_val = ts_arr[idx] if abs(idx) <= len(ts_arr) else None
                    try:
                        ts_iso = (
                            datetime.utcfromtimestamp(int(ts_val)).strftime("%Y-%m-%dT%H:%M:%SZ")
                            if ts_val is not None
                            else None
                        )
                    except Exception:
                        ts_iso = None
                    et_bucket: str | None = None
                    try:
                        now_et = datetime.now(tz=ZoneInfo("America/New_York"))
                        et_bucket = volume_profile.session_bucket(now_et)
                    except Exception:
                        pass
                    canon_bar = {
                        "ts": ts_iso,
                        "et_bucket": et_bucket,
                        "open": opens[idx] if abs(idx) <= len(opens) else None,
                        "high": highs[idx] if abs(idx) <= len(highs) else None,
                        "low": lows[idx] if abs(idx) <= len(lows) else None,
                        "close": closes[idx],
                        "iex_sip_ratio_used": None,
                        "bid": None,
                        "ask": None,
                        "last_trade_price": _bars_for_mtm.get("current_price"),
                    }
                    tg._v512_archive_minute_bar(ticker, canon_bar)
        except Exception as e:
            logger.warning("[bar] archive hook %s: %s", ticker, e)
        # Spec Section II.2 (Boundary Hold) requires a rolling buffer of
        # the most recent closed 1m closes. `record_latest_1m_close` walks
        # back from [-2] to find the newest non-None close (Yahoo keeps a
        # forming-bar None at [-2] for most of RTH); without this hook
        # `_last_1m_closes` stays empty and `evaluate_boundary_hold_gate`
        # returns insufficient_closes \u2192 polarity=None forever.
        try:
            if _bars_for_mtm:
                eot_glue.record_latest_1m_close(
                    ticker, _bars_for_mtm.get("closes") or []
                )
        except Exception as _e:
            logger.warning("[V5100-BOUNDARY] record_1m_close %s: %s", ticker, _e)
        paper_holds = callbacks.has_long(ticker)
        if not paper_holds:
            ok, bars = callbacks.check_entry(ticker)
            if ok and bars:
                px = bars["current_price"]
                try:
                    callbacks.execute_entry(ticker, px)
                except Exception as e:
                    callbacks.report_error(
                        executor="main",
                        code="PAPER_ENTRY_EXCEPTION",
                        severity="error",
                        summary=f"Paper entry exception: {ticker}",
                        detail=f"{type(e).__name__}: {str(e)[:200]}",
                    )
    except Exception as e:
        logger.error("Entry check error %s: %s", ticker, e)
    try:
        paper_short_holds = callbacks.has_short(ticker)
        if not paper_short_holds:
            ok, bars = callbacks.check_short_entry(ticker)
            if ok and bars:
                px = bars["current_price"]
                try:
                    callbacks.execute_short_entry(ticker, px)
                except Exception as e:
                    callbacks.report_error(
                        executor="main",
                        code="PAPER_SHORT_ENTRY_EXCEPTION",
                        severity="error",
                        summary=f"Paper short entry exception: {ticker}",
                        detail=f"{type(e).__name__}: {str(e)[:200]}",
                    )
    except Exception as e:
        logger.error("Short entry check error %s: %s", ticker, e)
