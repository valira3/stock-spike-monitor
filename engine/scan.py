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
from engine.extended_universe import effective_scan_tickers
from engine.timing import minutes_since_et_midnight

# v7.14.0: v10 ORB live-runtime shadow integration.
# The runtime sees every bar + builds OR window state + evaluates day
# gates IN PRODUCTION but does NOT execute trades yet. Entry routing
# stays on the legacy path. This lets us observe v10 state via the
# dashboard before flipping the live execution switch in PR7
# (v7.15.0). The kill-switch flag is ORB_LIVE_MODE (see
# orb/live_runtime.py).
import orb.live_runtime as _orb_runtime  # noqa: E402

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
            # v7.1.0: pre-open warm-up uses the dynamic extended-hours universe
            # so earnings reporters get bar archive seeded before the bell
            # if the feature flag is on. RTH branch falls back to TRADE_TICKERS.
            _pre_open_session = "extended"
            _pre_open_tickers = effective_scan_tickers(_pre_open_session)
            for _t_pre in _pre_open_tickers:
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
        # v7.0.7 \u2014 SPY regime tick + backfill before pre-open early
        # return. The 09:30 anchor capture window falls inside the pre-
        # open scan path (now_et.minute < 35) which historically returned
        # before any cycle hooks. The SPY tick hook is idempotent and
        # self-skips outside the 09:30 / 10:00 capture minutes; calling
        # it here means a 09:30 RTH-archive scan in production captures
        # spy_open_930 on the very first eligible cycle, and a backtest
        # replay starting at 09:30 ET sees the same anchor capture.
        try:
            tg._spy_regime_maybe_tick()
        except Exception as _e:
            logger.warning("[regime] preopen spy tick error: %s", _e)
        try:
            tg._spy_regime_maybe_backfill()
        except Exception as _e:
            logger.warning("[regime] preopen spy backfill error: %s", _e)
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

    # v7.0.7 \u2014 SPY regime tick on its own per-minute cadence. Same
    # rationale as the v7.0.6 backfill split: previously the SPY tick
    # was nested inside _qqq_weather_tick's QQQ-bucket-advance branch,
    # which fires only when a 5m bucket rolls. In production that
    # worked because pre-market QQQ buckets stream via websocket so the
    # 09:30 RTH bucket is a roll. In backtest replay the canonical
    # archive is RTH-only (390 bars), the first 5m bucket is still
    # forming at 09:30 (compute_5m_ohlc_and_ema9 drops the forming
    # bucket), so the QQQ-roll path's first tick lands at 09:35 \u2014
    # past the 09:30 anchor capture window. Wiring the tick onto its
    # own hook makes the live tick() path actually fire at 09:30
    # without changing live behavior (idempotent, self-skips once
    # classified).
    try:
        tg._spy_regime_maybe_tick()
    except Exception as _e:
        logger.warning("[regime] spy tick cycle hook error: %s", _e)

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

    # v7.1.0: session-aware ticker iteration. RTH unchanged (returns
    # TRADE_TICKERS); extended hours optionally returns the prod core
    # plus today's earnings reporters when the feature flag is on.
    try:
        _session = tg._market_session()
    except Exception:
        _session = "rth"
    _scan_universe = effective_scan_tickers(_session)

    # v7.14.0: bootstrap + session-start the v10 ORB runtime in shadow
    # mode. Failure-tolerant: a runtime exception cannot break the
    # legacy scan loop. The actual entry/exit routing stays on the
    # legacy path until PR7 (v7.15.0).
    try:
        if not _orb_runtime._bootstrapped:
            _orb_runtime.bootstrap()
            logger.info("[V79-ORB-WIRED] live=%s bootstrap=ok",
                        _orb_runtime.is_live_mode_on())
    except Exception as _e:
        logger.warning("[V79-ORB-WIRED] bootstrap failed: %s", _e)
    try:
        _date_iso = now_et.strftime("%Y-%m-%d")
        if _orb_runtime._session_date != _date_iso:
            # Build the inputs ensure_session_started needs. All values
            # are causally clean: VIX is the prior session's close,
            # ticker_open_today is the live 09:30 print, and pdc is
            # cached from the prior session's last bar.
            _vix_d1 = None
            try:
                from tools.orb_vix_loader import (
                    load_vix_closes, vix_close_for,
                )
                _vix_csv = "data/external/vix-daily.csv"
                _vix_dict = load_vix_closes(_vix_csv)
                _vix_d1 = vix_close_for(_vix_dict, [_date_iso], _date_iso)
            except Exception:
                _vix_d1 = None
            _opens = {tk: getattr(tg, "_session_open", {}).get(tk)
                      for tk in _scan_universe}
            _pdc = {tk: tg.pdc.get(tk) if hasattr(tg, "pdc") else None
                    for tk in _scan_universe}
            try:
                from engine.portfolio_book import (
                    PORTFOLIOS, ALL_PORTFOLIO_IDS,
                )
                _eq = {}
                for pid in ALL_PORTFOLIO_IDS:
                    book = PORTFOLIOS.get(pid)
                    if book is not None:
                        try:
                            _eq[pid] = book.current_equity()
                        except Exception:
                            _eq[pid] = float(getattr(book, "paper_cash", 0.0))
            except Exception:
                _eq = {"main": float(getattr(tg, "paper_cash", 100_000.0))}
            _orb_runtime.ensure_session_started(
                date_iso=_date_iso,
                tickers=list(_scan_universe),
                vix_close_d1=_vix_d1,
                ticker_open_today=_opens,
                ticker_prev_close=_pdc,
                equity_per_portfolio=_eq,
            )
    except Exception as _e:
        logger.warning("[V79-ORB-RESET] failed: %s", _e)

    # v7.24.0: intraday equity refresh. Pulls each PortfolioBook's
    # current_equity (paper_cash + MTM long_mv - short_liability) and
    # pushes into each per-portfolio RiskBook so notional caps track
    # reality across the session. Cheap, runs once per scan cycle.
    try:
        _refreshed = _orb_runtime.refresh_equity_from_books()
        if _refreshed:
            logger.debug(
                "[V79-ORB-EQUITY] refreshed %s",
                ",".join(f"{p}=${e:.0f}" for p, e in _refreshed.items()),
            )
    except Exception as _e:
        logger.debug("[V79-ORB-EQUITY] refresh failed: %s", _e)

    for ticker in _scan_universe:
        _per_ticker_tick(callbacks, ticker)

    logger.info(
        "SCAN CYCLE done in %.2fs, %d tickers (session=%s)",
        time.time() - cycle_start,
        len(_scan_universe),
        _session,
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
                        # v7.7.2: route through tg._now_et so backtest
                        # replay clock applies (was wall-clock leak).
                        now_et = tg._now_et()
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
                    # v7.14.0: shadow-mode feed to v10 ORB runtime.
                    # Failure-tolerant: a runtime exception must NOT
                    # break the legacy minute-bar archive path.
                    try:
                        _bar_open = canon_bar["open"]
                        _bar_high = canon_bar["high"]
                        _bar_low = canon_bar["low"]
                        _bar_close = canon_bar["close"]
                        _bar_vol = canon_bar.get("iex_volume") or 0.0
                        if (_bar_open is not None and _bar_high is not None
                                and _bar_low is not None
                                and _bar_close is not None and ts_val is not None):
                            _bucket_min = minutes_since_et_midnight(int(ts_val))
                            _orb_runtime.feed_bar(
                                ticker=ticker,
                                bar_high=float(_bar_high),
                                bar_low=float(_bar_low),
                                bar_open=float(_bar_open),
                                bar_close=float(_bar_close),
                                bar_volume=float(_bar_vol or 0.0),
                                bar_bucket_min=_bucket_min,
                            )
                    except Exception as _orb_e:
                        logger.warning(
                            "[V79-ORB-FEED] %s: %s", ticker, _orb_e,
                        )
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
        # v7.15.0: entry routing switch.
        #
        # When ORB_LIVE_MODE=1 (default), the v10 ORB runtime owns the
        # entry decision; the legacy Tiger Sovereign callbacks.check_entry
        # path is bypassed.
        #
        # When ORB_LIVE_MODE=0, the runtime is bootstrapped (so OR
        # window state still builds for dashboard observation) but the
        # legacy path takes over execution -- emergency rollback.
        paper_holds = callbacks.has_long(ticker)
        if not paper_holds:
            if _orb_runtime.is_live_mode_on():
                _orb_long_entry(callbacks, tg, ticker, _bars_for_mtm)
            else:
                # Legacy fallback path
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
            if _orb_runtime.is_live_mode_on():
                _orb_short_entry(callbacks, tg, ticker, _bars_for_mtm)
            else:
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


# v7.15.0: per-side ORB entry helpers. Factored out of _per_ticker_tick
# so each side is independently testable and the logic is symmetric.
def _resolve_portfolio_equity(tg, portfolio_id: str = "main") -> float:
    """Get current equity for `portfolio_id`. Falls back to paper_cash
    on PortfolioBook lookup failure."""
    try:
        from engine.portfolio_book import PORTFOLIOS
        book = PORTFOLIOS.get(portfolio_id)
        if book is None:
            return float(getattr(tg, "paper_cash", 100_000.0))
        return book.current_equity()
    except Exception:
        return float(getattr(tg, "paper_cash", 100_000.0))


def _orb_long_entry(callbacks: EngineCallbacks, tg, ticker: str,
                    bars_for_mtm: dict | None) -> None:
    """v10 ORB long-entry path -- per-portfolio fanout.

    v7.23.0: iterates ALL enabled portfolios (Main / Val / Gene) and
    runs check_entry independently for each. Each portfolio has its
    own RiskBook + FSM so admissions are isolated.

    Broker execution: only "main" routes through callbacks.execute_entry
    (which is main-bound). Val and Gene admissions are tracked on
    their LiveAdapter (position state, ticket id) and logged with
    [V79-ORB-ENTRY] tags so dashboard/Telegram show consistent state.
    Their actual broker orders are wired in a follow-up PR once the
    Val/Gene executors expose a `fire_long(ticker, price, shares)`
    surface; today their Alpaca keys are typically unset and the
    executors are skipped at boot.
    """
    try:
        from engine.bars import compute_5m_ohlc_and_ema9
        _5m = compute_5m_ohlc_and_ema9(bars_for_mtm)
        if not _5m or not _5m.get("closes"):
            return  # no closed 5m bar yet
        five_min_close = _5m["closes"][-1]
        next_open = (bars_for_mtm or {}).get("current_price") or five_min_close

        # Resolve all enabled portfolio_ids from the live runtime
        engine = _orb_runtime.get_engine()
        if engine is None:
            return
        portfolio_ids = list(engine.portfolio_ids)

        for pid in portfolio_ids:
            equity = _resolve_portfolio_equity(tg, pid)
            result = _orb_runtime.check_entry(
                portfolio_id=pid, ticker=ticker, side="long",
                five_min_close=float(five_min_close),
                next_open=float(next_open),
                equity=equity,
            )
            if result.ok:
                logger.info(
                    "[V79-ORB-ENTRY] long %s portfolio=%s "
                    "price=%.4f stop=%.4f target=%.4f shares=%d ticket=%s",
                    ticker, pid, result.price, result.stop, result.target,
                    result.shares, result.ticket_id[:8],
                )
                # Stash size for the broker sizing handoff
                try:
                    _orb_runtime.stash_v10_size(pid, ticker, result.shares)
                except Exception:
                    pass
                # Broker fire -- only main goes through the legacy
                # callbacks.execute_entry path today. Val/Gene execution
                # wiring is a follow-up PR.
                if pid == "main":
                    try:
                        callbacks.execute_entry(ticker, result.price)
                    except Exception as e:
                        callbacks.report_error(
                            executor="main",
                            code="ORB_LONG_ENTRY_EXCEPTION",
                            severity="error",
                            summary=f"ORB long entry exception: {ticker}",
                            detail=f"{type(e).__name__}: {str(e)[:200]}",
                        )
                else:
                    logger.info(
                        "[V79-ORB-ADMIT] %s long %s -- broker fire deferred "
                        "(awaiting per-portfolio executor wiring)",
                        pid, ticker,
                    )
            elif result.reason_no and result.reason_no != "no_signal":
                logger.debug(
                    "[V79-ORB-REJECT] long %s portfolio=%s reason=%s",
                    ticker, pid, result.reason_no,
                )
    except Exception as e:
        logger.warning("[V79-ORB] long entry error %s: %s", ticker, e)


def _orb_short_entry(callbacks: EngineCallbacks, tg, ticker: str,
                     bars_for_mtm: dict | None) -> None:
    """v10 ORB short-entry path -- per-portfolio fanout. Mirror of
    _orb_long_entry."""
    try:
        from engine.bars import compute_5m_ohlc_and_ema9
        _5m = compute_5m_ohlc_and_ema9(bars_for_mtm)
        if not _5m or not _5m.get("closes"):
            return
        five_min_close = _5m["closes"][-1]
        next_open = (bars_for_mtm or {}).get("current_price") or five_min_close
        engine = _orb_runtime.get_engine()
        if engine is None:
            return
        portfolio_ids = list(engine.portfolio_ids)
        for pid in portfolio_ids:
            equity = _resolve_portfolio_equity(tg, pid)
            result = _orb_runtime.check_entry(
                portfolio_id=pid, ticker=ticker, side="short",
                five_min_close=float(five_min_close),
                next_open=float(next_open),
                equity=equity,
            )
            if result.ok:
                logger.info(
                    "[V79-ORB-ENTRY] short %s portfolio=%s "
                    "price=%.4f stop=%.4f target=%.4f shares=%d ticket=%s",
                    ticker, pid, result.price, result.stop, result.target,
                    result.shares, result.ticket_id[:8],
                )
                try:
                    _orb_runtime.stash_v10_size(pid, ticker, result.shares)
                except Exception:
                    pass
                if pid == "main":
                    try:
                        callbacks.execute_short_entry(ticker, result.price)
                    except Exception as e:
                        callbacks.report_error(
                            executor="main",
                            code="ORB_SHORT_ENTRY_EXCEPTION",
                            severity="error",
                            summary=f"ORB short entry exception: {ticker}",
                            detail=f"{type(e).__name__}: {str(e)[:200]}",
                        )
                else:
                    logger.info(
                        "[V79-ORB-ADMIT] %s short %s -- broker fire deferred "
                        "(awaiting per-portfolio executor wiring)",
                        pid, ticker,
                    )
            elif result.reason_no and result.reason_no != "no_signal":
                logger.debug(
                    "[V79-ORB-REJECT] short %s portfolio=%s reason=%s",
                    ticker, pid, result.reason_no,
                )
    except Exception as e:
        logger.warning("[V79-ORB] short entry error %s: %s", ticker, e)
