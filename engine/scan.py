"""v5.26.0 / v6.9.0 \u2014 engine.scan: per-minute scan loop (spec-strict).

v6.9.0 note: backtest-mode callers may pre-warm indicator state using
``backtest.indicator_cache.get_indicators`` (L2 cache) before driving
the scan loop. The live code path is unchanged; ``get_indicators``
is a pure-read call that returns cached Parquet data and does not
affect process state.

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


def _v531_build_permit_state(tg, ticker: str) -> dict | None:
    """v5.31.0 \u2014 assemble a per-minute permit_state blob for the
    indicator snapshot stream. Captures boundary-hold (LONG + SHORT) and
    the live trail-stop / stage of any open position so the lifecycle
    overlay's per-minute trail-stop staircase has data.

    Failure-tolerant \u2014 returns None on any error so the caller can
    pass it through cleanly. Boundary-hold reads use the same
    ``eot_glue.evaluate_boundary_hold_gate`` call the gate stack uses;
    trail-stop snapshots read off ``pos['trail_state']`` (the TrailState
    dataclass attached lazily by ``_run_sentinel``).
    """
    try:
        sym = (ticker or "").upper()
        or_h = None
        or_l = None
        try:
            or_h = tg.or_high.get(sym) if hasattr(tg, "or_high") else None
            or_l = tg.or_low.get(sym) if hasattr(tg, "or_low") else None
        except Exception:
            pass

        bh_long = None
        bh_short = None
        try:
            if or_h is not None and or_l is not None:
                _r_l = eot_glue.evaluate_boundary_hold_gate(sym, "LONG", or_h, or_l)
                bh_long = bool(_r_l.get("hold")) if isinstance(_r_l, dict) else None
                _r_s = eot_glue.evaluate_boundary_hold_gate(sym, "SHORT", or_h, or_l)
                bh_short = bool(_r_s.get("hold")) if isinstance(_r_s, dict) else None
        except Exception:
            pass

        # Open-position trail-state snapshot for the lifecycle overlay.
        # Captures the per-minute trail-stop ladder + stage transitions
        # so a backtest can reconstruct the exact stop the engine would
        # have proposed at any minute the position was alive.
        trail = None
        try:
            for _attr, _label in (("positions", "LONG"), ("short_positions", "SHORT")):
                _book = getattr(tg, _attr, None) or {}
                _pos = _book.get(sym)
                if _pos is None:
                    continue
                _ts = _pos.get("trail_state")
                if _ts is None:
                    trail = {
                        "side": _label,
                        "stage": 0,
                        "last_proposed_stop": _pos.get("stop"),
                        "peak_close": None,
                        "bars_seen": None,
                    }
                else:
                    trail = {
                        "side": _label,
                        "stage": getattr(_ts, "stage", 0),
                        "last_proposed_stop": getattr(_ts, "last_proposed_stop", None)
                        or _pos.get("stop"),
                        "peak_close": getattr(_ts, "peak_close", None),
                        "bars_seen": getattr(_ts, "bars_seen", None),
                    }
                break
        except Exception:
            trail = None

        return {
            "boundary_hold_long": bh_long,
            "boundary_hold_short": bh_short,
            "or_high": or_h,
            "or_low": or_l,
            "trail": trail,
        }
    except Exception:
        return None


def scan_loop(callbacks: EngineCallbacks) -> None:
    """Main scan: manage positions, check new entries. Runs every 60s."""
    tg = _tg()
    now_et = callbacks.now_et()

    try:
        tg._refresh_market_mode()
    except Exception:
        logger.exception("_refresh_market_mode failed (observation only)")

    # v6.14.4 \u2014 wire the volume_bucket baseline refresh into the live
    # scan loop. The hook itself self-guards (no-op before 09:29 ET, and
    # idempotent within a single session via _baseline_refreshed_for_date).
    # Prior to this release the function was exported but never invoked,
    # so the baseline stayed empty and the dashboard sat in COLDSTART.
    try:
        eot_glue.refresh_volume_baseline_if_needed(now_et)
    except Exception:
        logger.exception("refresh_volume_baseline_if_needed failed")

    is_weekend = now_et.weekday() >= 5
    before_open = now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 35)
    # v6.3.2 \u2014 hard cutoff moved 15:55 \u2192 16:00 ET so the engine
    # keeps managing positions and accepting candidates through the full
    # final 5-minute bucket. The 15:55 ceiling was clipping ~5 minutes
    # of legitimate exit/entry activity per day in backtest and prod.
    after_close = now_et.hour >= 16
    tg._scan_idle_hours = bool(is_weekend or before_open or after_close)

    if is_weekend:
        return

    # v5.26.1 \u2014 premarket warm-up window: 08:00 ET (1.5h before RTH
    # open) through 09:35 ET. During this window we archive every
    # minute's 1m bar for QQQ and each TRADE_TICKER so the bar archive
    # is fully populated before the entry engine activates at 09:35.
    # No entries / no manage_positions in this window. Replaces the
    # previous narrow 09:29:30\u201309:35 single-tick warm-up.
    _pre_open_window = now_et.hour == 8 or (now_et.hour == 9 and now_et.minute < 35)
    if before_open and _pre_open_window:
        # v6.11.9 — also stamp _last_scan_time during the pre-open
        # warm-up window so the dashboard's "next scan in Ns" countdown
        # ticks instead of showing "♻ --" all morning. The scan loop
        # genuinely runs every SCAN_INTERVAL seconds during this window
        # to archive QQQ + per-ticker premarket bars; the live pill
        # should reflect that.
        tg._last_scan_time = datetime.now(timezone.utc)
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
                    # v5.26.2 \u2014 populate bid/ask from Alpaca latest quote
                    # in the premarket warm-up window too. Failure-tolerant.
                    try:
                        _q_bid_pre, _q_ask_pre = tg._v512_quote_snapshot(_t_pre)
                    except Exception:
                        _q_bid_pre, _q_ask_pre = (None, None)
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
                        "bid": _q_bid_pre,
                        "ask": _q_ask_pre,
                        "last_trade_price": _b_pre.get("current_price"),
                        # v5.31.0 \u2014 Yahoo source has no trade_count / vwap.
                        "trade_count": None,
                        "bar_vwap": None,
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

    # v7.0.6 \u2014 SPY regime backfill on its own cadence. Idempotent
    # (no-op once classified) and decoupled from the QQQ bucket-advance
    # branch so a mid-session restart can recover the 09:30/10:00 anchors
    # on the very next scan cycle. The previous nesting inside
    # _qqq_weather_tick's macro-snapshot try silently dropped the backfill
    # whenever forensic_capture or a wrapping condition failed.
    try:
        tg._spy_regime_maybe_backfill()
    except Exception as _e:
        logger.warning("[regime] spy backfill cycle hook error: %s", _e)

    # v5.31.5 \u2014 per-stock local weather cache for the local-override
    # gate and the dashboard's per-stock Weather card. Walks active
    # tickers (TRADE_TICKERS plus open positions) and refreshes each
    # one's 5m close + EMA9 + last + AVWAP. Fail-closed inside the
    # helper so a single bad ticker can't break the cycle.
    try:
        tg._ticker_weather_tick_all()
    except Exception as _e:
        logger.warning("[regime] ticker weather cycle hook error: %s", _e)

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
        logger.info("SCAN CYCLE done in %.2fs (paused, manage only)", time.time() - cycle_start)
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
                    # v5.26.2 \u2014 populate bid/ask from the Alpaca latest
                    # quote snapshot. Failure-tolerant; (None, None) on miss.
                    try:
                        _q_bid, _q_ask = tg._v512_quote_snapshot(ticker)
                    except Exception:
                        _q_bid, _q_ask = (None, None)
                    _vols_rth = _bars_for_mtm.get("volumes") or []
                    canon_bar = {
                        "ts": ts_iso,
                        "et_bucket": et_bucket,
                        "open": opens[idx] if abs(idx) <= len(opens) else None,
                        "high": highs[idx] if abs(idx) <= len(highs) else None,
                        "low": lows[idx] if abs(idx) <= len(lows) else None,
                        "close": closes[idx],
                        "iex_volume": _vols_rth[idx] if abs(idx) <= len(_vols_rth) else None,
                        "iex_sip_ratio_used": None,
                        "bid": _q_bid,
                        "ask": _q_ask,
                        "last_trade_price": _bars_for_mtm.get("current_price"),
                        # v5.31.0 \u2014 Yahoo source has no trade_count / vwap.
                        "trade_count": None,
                        "bar_vwap": None,
                    }
                    tg._v512_archive_minute_bar(ticker, canon_bar)
                    # v5.26.2 \u2014 per-minute forensic indicator snapshot.
                    # Pulls the same DI/ADX/RSI streams the gate stack will
                    # read on this tick; written to /data/forensics/<date>/
                    # indicators/<TICKER>.jsonl. Failure-tolerant.
                    try:
                        from forensic_capture import (
                            write_indicator_snapshot as _write_ind,
                        )

                        _di_s = tg.v5_di_1m_5m(ticker) if hasattr(tg, "v5_di_1m_5m") else None
                        _adx_s = tg.v5_adx_1m_5m(ticker) if hasattr(tg, "v5_adx_1m_5m") else None
                        _closes_for_rsi = [c for c in (closes or []) if c is not None]
                        _rsi15 = (
                            tg._compute_rsi(_closes_for_rsi, period=15)
                            if _closes_for_rsi and hasattr(tg, "_compute_rsi")
                            else None
                        )
                        _vol_idx = (
                            (_bars_for_mtm.get("volumes") or [])[idx]
                            if abs(idx) <= len(_bars_for_mtm.get("volumes") or [])
                            else None
                        )
                        _write_ind(
                            ticker=ticker,
                            ts_utc=ts_iso,
                            bar_close=closes[idx],
                            bar_open=opens[idx] if abs(idx) <= len(opens) else None,
                            bar_high=highs[idx] if abs(idx) <= len(highs) else None,
                            bar_low=lows[idx] if abs(idx) <= len(lows) else None,
                            bar_volume=_vol_idx,
                            bid=_q_bid,
                            ask=_q_ask,
                            last_trade_price=_bars_for_mtm.get("current_price"),
                            or_high=tg.or_high.get(ticker) if hasattr(tg, "or_high") else None,
                            or_low=tg.or_low.get(ticker) if hasattr(tg, "or_low") else None,
                            pdc=(tg.pdc.get(ticker) if hasattr(tg, "pdc") else None),
                            sess_hod=(
                                tg._v570_session_hod.get(ticker.upper())
                                if hasattr(tg, "_v570_session_hod")
                                else None
                            ),
                            sess_lod=(
                                tg._v570_session_lod.get(ticker.upper())
                                if hasattr(tg, "_v570_session_lod")
                                else None
                            ),
                            di_plus_1m=(_di_s or {}).get("di_plus_1m"),
                            di_minus_1m=(_di_s or {}).get("di_minus_1m"),
                            di_plus_5m=(_di_s or {}).get("di_plus_5m"),
                            di_minus_5m=(_di_s or {}).get("di_minus_5m"),
                            adx_1m=(_adx_s or {}).get("adx_1m"),
                            adx_5m=(_adx_s or {}).get("adx_5m"),
                            rsi_15=_rsi15,
                            strike_count=(
                                tg._v570_strike_count(ticker)
                                if hasattr(tg, "_v570_strike_count")
                                else None
                            ),
                            sentinel_state=None,
                            permit_state=_v531_build_permit_state(tg, ticker),
                        )
                    except Exception as _e_ind:
                        logger.warning(
                            "[V526-FORENSIC] indicator snapshot %s: %s",
                            ticker,
                            _e_ind,
                        )
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
                eot_glue.record_latest_1m_close(ticker, _bars_for_mtm.get("closes") or [])
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
