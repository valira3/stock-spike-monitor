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
import os
import sys as _sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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


# v10.0.0 -- broad-universe scanner pre-warm. One-shot per session,
# triggered from scan_loop's pre-open window at ~09:24 ET. Fires
# in a daemon thread so it doesn't block the 60s scan cadence.
_v10_prewarm_done_for: set = set()
_v10_prewarm_lock = __import__("threading").RLock()


def _v10_prewarm_dynamic_universe(now_et) -> None:
    """Fire the in-process premarket pull at ~09:24 ET so the
    broad-universe scanner's data is ready when ensure_session_started
    runs at the 09:30 ET RTH open. Idempotent: one fire per ET date.
    Background thread; safe to call from inside scan_loop.

    Skips when ORB_DYNAMIC_UNIVERSE=0 or when the bar archive directory
    can't be resolved. On failure, the auto-rebuild path inside
    orb.live_premarket_scanner.compute_universe is the safety net.
    """
    import os as _os
    if _os.environ.get("ORB_DYNAMIC_UNIVERSE", "1") != "1":
        return
    # Only fire in the 09:20-09:29 ET window (one minute of slack
    # before the cron-style 09:24 target so scan-cadence drift can't
    # miss it; the inner once-per-date guard prevents double-fire).
    if not (now_et.hour == 9 and 20 <= now_et.minute <= 29):
        return
    date_iso = now_et.strftime("%Y-%m-%d")
    with _v10_prewarm_lock:
        if date_iso in _v10_prewarm_done_for:
            return
        _v10_prewarm_done_for.add(date_iso)

    def _worker():
        try:
            import json as _json
            from datetime import date as _date
            from pathlib import Path as _Path
            from tools.pull_premarket_for_scanner import (
                rebuild_premarket_bars_for_date,
            )
            from orb.live_premarket_scanner import default_bar_archive_root

            uni_path = _Path("data/universe/sp500.json")
            if not uni_path.is_file():
                uni_path = _Path(
                    _os.environ.get("TG_DATA_ROOT", "/data")
                ) / "universe" / "sp500.json"
            try:
                uni_doc = _json.loads(uni_path.read_text())
                tickers = list(uni_doc.get("tickers") or [])
            except Exception:
                tickers = []
            if not tickers:
                logger.warning("[V100-SCANNER-PREWARM] no universe; skip")
                return
            n = rebuild_premarket_bars_for_date(
                target_date=_date.fromisoformat(date_iso),
                out_root=default_bar_archive_root(),
                universe_tickers=tickers,
            )
            logger.info(
                "[V100-SCANNER-PREWARM] date=%s pulled %d bars across %d tickers",
                date_iso, n, len(tickers),
            )
        except Exception:
            logger.exception("[V100-SCANNER-PREWARM] worker failed")

    import threading as _th
    _th.Thread(
        target=_worker, name="v10-scanner-prewarm", daemon=True,
    ).start()


def _v531_build_permit_state(tg, ticker: str) -> dict | None:
    """v5.31.0 \u2014 per-minute permit_state blob for the indicator snapshot
    stream.

    v10.0.1: boundary-hold gate deleted along with the rest of the
    eot_glue surface. The blob still emits trail-stop / stage data for
    the lifecycle overlay's per-minute trail-stop staircase.
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

        # Open-position trail-state snapshot for the lifecycle overlay.
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
            "boundary_hold_long": None,
            "boundary_hold_short": None,
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
    # v10.0.1 -- eot_glue.refresh_volume_baseline_if_needed call deleted
    # along with the rest of the v5_10_1_integration surface.

    is_weekend = now_et.weekday() >= 5
    # v7.72.0 -- boundary moved from 09:35 to 09:30 ET. The 09:30-09:34
    # slot is the OR (opening range) lock window; the engine is actively
    # collecting OR bounds during it, not idle. Pre-v7.72.0 the dashboard
    # banner flashed "OUTSIDE MARKET HOURS" for those 5 minutes each
    # session because _scan_idle_hours leaked True through the OR window.
    # The "no entries during OR" gate is enforced separately in orb/day_gates.
    before_open = now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 30)
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
        # v10.0.0 -- pre-warm the broad-universe scanner data at 09:24 ET
        # in a background thread. Triggers the same in-process Alpaca pull
        # that orb/live_premarket_scanner.compute_universe would otherwise
        # run on-demand at ensure_session_started, but doing it ~6 min
        # ahead means the 09:30 ET first-scan cycle finds data ready and
        # doesn't pay the ~30s pull latency right at the RTH open.
        # Fully self-healing: if it fails or skipped, the scanner's
        # on-demand auto-rebuild still kicks in at session start.
        try:
            _v10_prewarm_dynamic_universe(now_et)
        except Exception:
            logger.debug("[V100-SCANNER-PREWARM] failed (non-fatal)", exc_info=True)
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
            logger.info("[V79-ORB-WIRED] live=%s bootstrap=ok", _orb_runtime.is_live_mode_on())
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
                    load_vix_closes,
                    vix_close_for,
                )

                _vix_csv = "data/external/vix-daily.csv"
                _vix_dict = load_vix_closes(_vix_csv)
                _vix_d1 = vix_close_for(_vix_dict, [_date_iso], _date_iso)
            except Exception:
                _vix_d1 = None
            _opens = {tk: getattr(tg, "_session_open", {}).get(tk) for tk in _scan_universe}
            _pdc = {tk: tg.pdc.get(tk) if hasattr(tg, "pdc") else None for tk in _scan_universe}
            try:
                from engine.portfolio_book import (
                    PORTFOLIOS,
                    ALL_PORTFOLIO_IDS,
                )
                from engine.portfolio_equity import resolve_equity

                _eq = {}
                for pid in ALL_PORTFOLIO_IDS:
                    book = PORTFOLIOS.get(pid)
                    if book is None:
                        continue
                    # v7.76.0 -- route through resolve_equity so Val/Gene
                    # books pick up their Alpaca account equity instead
                    # of book.current_equity() returning 0 (paper_cash
                    # defaults to 0 and is never bridged for non-main
                    # books). Pre-v7.76.0 Val/Gene RiskBook.equity was
                    # always 0, which produced a notional cap of 0 and
                    # rejected every entry with risk_reject:notional_cap.
                    try:
                        _eq[pid] = resolve_equity(pid)
                    except Exception:
                        try:
                            _eq[pid] = book.current_equity()
                        except Exception:
                            _eq[pid] = float(getattr(book, "paper_cash", 0.0))
            except Exception:
                _eq = {"main": float(getattr(tg, "paper_cash", 100_000.0))}
            _fresh = _orb_runtime.ensure_session_started(
                date_iso=_date_iso,
                tickers=list(_scan_universe),
                vix_close_d1=_vix_d1,
                ticker_open_today=_opens,
                ticker_prev_close=_pdc,
                equity_per_portfolio=_eq,
            )
            # v7.74.0 -- if the session just got initialized AND we're
            # already past OR end (i.e. the bot started up mid-session,
            # post-OR), replay the 09:30-09:59 ET 1m bars into the ORB
            # engine so it can still trade today. Pre-v7.74.0 this case
            # left bars_seen=0 forever -> WARMUP forever -> 0 trades.
            if _fresh:
                _maybe_backfill_or_window(callbacks, now_et, _scan_universe)
    except Exception as _e:
        logger.warning("[V79-ORB-RESET] failed: %s", _e)

    # v8.3.0 -- automatic OR backfill on EVERY scan cycle (not just
    # the first one after ensure_session_started). The v7.74.0 hook
    # above only fired when `_fresh==True`, which is one-shot per
    # date. If that single attempt failed silently (Alpaca returned
    # empty bars, fetch raised, etc.) the OR stayed empty for the
    # rest of the day. v8.3.0 retries on every cycle so a transient
    # bar-source glitch can't cook today's trading.
    #
    # Cheap when all ORs are locked: the engine snapshot is consulted
    # first and the per-ticker loop short-circuits on locked rows.
    try:
        _orb_post_or_backfill_sweep(callbacks, now_et, _scan_universe)
    except Exception as _e:
        logger.debug("[V83-OR-BACKFILL] sweep failed: %s", _e)

    # v8.3.15 -- phantom-IN_POS consistency sweep. After v8.3.4
    # rehydrate + executor.positions load, reconcile any engine FSM
    # rows that say in_position=True but the ticker isn't actually
    # held by the corresponding executor / main book. Self-heals
    # stale orb_state_<date>.json snapshots written by an older
    # process where the close path missed the unmirror.
    #
    # Idempotent: runs every cycle but a clean state returns 0
    # phantoms (cheap O(N day_states) scan).
    try:
        _orb_phantom_sweep(tg)
    except Exception as _e:
        logger.debug("[V8315-PHANTOM-SWEEP] failed: %s", _e)

    # v8.3.4 -- engine state persistence. Snapshot OR windows +
    # DayState FSM + RiskBook + Activity feed + Wash-sale tracker +
    # Pending v10 sizes to /data/orb_state_<date>.json after each
    # scan cycle. Rehydrated on next bootstrap so a Railway redeploy
    # mid-day no longer loses in-memory state.
    #
    # Cheap: ~5 KB JSON, atomic write (tempfile + os.replace). Errors
    # are swallowed and debug-logged; never blocks the trading path.
    try:
        _orb_runtime.dump_engine_state_now()
    except Exception as _e:
        logger.debug("[V834-PERSIST] dump failed: %s", _e)

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

    # v9.1.127 -- per-portfolio EXIT pass for non-main portfolios.
    # Val/Gene evaluate their OWN open positions against the v10 engine's
    # exit logic and fire their own broker closes. No longer hooked to
    # Main's bus EXIT_LONG/EXIT_SHORT (those are skipped in _on_signal
    # when independent mode is on). Main exits via the legacy
    # broker/positions.py:manage_positions path on Main's scan loop.
    try:
        _v10_per_portfolio_exit_pass(callbacks)
    except Exception:
        logger.exception("[V9127-EXIT] pass failed; engine state unchanged")

    # v9.1.0 -- EOD reversal pass. Single hook per cycle that:
    #   - at 15:30 ET ranks the EOD universe + admits top-1/top-1
    #     long/short legs per portfolio (idempotent: only fires once
    #     per session via entry_attempted flag)
    #   - at 15:59 ET flattens all open EOD positions
    # The engine TRACKS the positions for the dashboard regardless;
    # actual broker fire is gated by ORB_EOD_FIRE_BROKER (default OFF
    # for v9.1.0 paper-fire-observation per the v8.3.23 pattern).
    #
    # v9.1.20 HOTFIX -- cur_min was never defined in scan_loop's
    # scope. The pre-v9.1.20 call below raised NameError every cycle;
    # the wrapper try/except caught + logged but the engine never
    # admitted EOD signals because the function bailed before any
    # work. Today's operator caught this when the EOD window opened
    # at 15:00 ET and entry_attempted stayed False across all books.
    # Computed identically to scan.py:581 + 705's other cur_min sites.
    cur_min = now_et.hour * 60 + now_et.minute
    try:
        _eod_reversal_pass(callbacks, cur_min)
    except Exception:
        logger.exception("[V910-EOD] pass failed; engine state unchanged")

    # v9.1.8 -- per-cycle engine state dump (throttled to 30s by default
    # via orb.live_runtime._persist_min_interval_s) so day_states +
    # risk_books + or_windows + recent_activity survive a Railway
    # redeploy. Pre-v9.1.8 the dump function existed but had no
    # production caller; every deploy reset the per-ticker trade
    # counter to 0 even when the broker had real trades that day.
    try:
        from orb import live_runtime as _lr_persist

        _lr_persist.persist_engine_state()
    except Exception:
        logger.debug("[V834-PERSIST] cycle dump raised (non-fatal)")

    logger.info(
        "SCAN CYCLE done in %.2fs, %d tickers (session=%s)",
        time.time() - cycle_start,
        len(_scan_universe),
        _session,
    )


def _read_bars_from_archive(
    date_iso: str, ticker: str, bucket_start: int, bucket_end: int
) -> list[tuple]:
    """Read 1m bars from the local bar archive for bucket range [start, end).

    Returns list of (bucket, high, low, open, close, volume) tuples sorted
    by bucket. Used for OR backfill on redeploy so the bot can always trade
    regardless of when Railway deploys relative to the OR window.
    """
    import json as _json
    import os as _os

    bars_dir = _os.environ.get("BARS_BASE_DIR", "/data/bars")
    path = _os.path.join(bars_dir, date_iso, f"{ticker}.jsonl")
    if not _os.path.exists(path):
        return []
    rows: list[tuple] = []
    try:
        with open(path, encoding="utf-8") as _f:
            for line in _f:
                try:
                    bar = _json.loads(line)
                except Exception:
                    continue
                bucket = bar.get("et_bucket")
                if bucket is None or not isinstance(bucket, int):
                    continue
                if bucket < bucket_start or bucket >= bucket_end:
                    continue
                h = bar.get("high")
                lo = bar.get("low")
                o = bar.get("open")
                c = bar.get("close")
                if None in (h, lo, o, c):
                    continue
                v = float(bar.get("total_volume") or bar.get("iex_volume") or 0.0)
                rows.append((bucket, float(h), float(lo), float(o), float(c), v))
    except Exception:
        pass
    return sorted(rows, key=lambda x: x[0])


def _maybe_backfill_or_window(
    callbacks: EngineCallbacks, now_et: datetime, scan_universe: list
) -> None:
    """v7.74.0 -- replay historical 09:30-09:59 ET 1m bars into the
    ORB engine for each ticker, then feed one post-OR bar so the
    window locks.

    Why: when the bot starts up mid-session (post-OR-end), the live
    scan loop only feeds the LATEST bar per cycle -- never replays
    history. Result: bars_seen stays at 0, OR never locks, FSMs
    stuck in WARMUP, zero trades for the rest of the day.

    Triggered only on the FIRST scan cycle of a fresh session
    (`ensure_session_started` returned True).

    Auto-heal design: works at any deploy time:
    - Pre-OR (before 09:30 ET): no-op, live scan covers it normally
    - Mid-OR (09:30-10:00 ET): replays missed bars from /data/bars archive
    - Post-OR (after 10:00 ET): replays full OR from archive + API fallback

    The fetch_1min_bars hook returns 1m bars from 08:00 ET onward
    (Alpaca IEX feed window). We filter to the OR window
    [09:30, 09:59] and feed in chronological order. The lock
    triggers either on the 09:59 bar (normal path) or via the
    v7.73.0 post-window-bar fallback if 09:59 is missing.
    """
    from orb.engine import OrbConfig

    cfg = OrbConfig()  # default; or_start=570 (09:30), or_end=600 (10:00)
    or_start = cfg.session_start_minutes
    or_end = cfg.or_end_minutes
    cur_min = now_et.hour * 60 + now_et.minute
    date_iso = now_et.strftime("%Y-%m-%d")

    if cur_min < or_start:
        # Before OR window; live scan will cover it normally.
        return

    if cur_min < or_end:
        # Mid-OR: bot deployed during the opening range window.
        # Replay all bars from [or_start, cur_min) from the local archive
        # so OR windows are populated with the data we missed.
        backfilled = 0
        for ticker in scan_universe:
            rows = _read_bars_from_archive(date_iso, ticker, or_start, cur_min)
            if not rows:
                continue
            for bucket, h, lo, o, c, v in rows:
                try:
                    _orb_runtime.feed_bar(
                        ticker=ticker,
                        bar_high=h,
                        bar_low=lo,
                        bar_open=o,
                        bar_close=c,
                        bar_volume=v,
                        bar_bucket_min=bucket,
                    )
                except Exception as _fe:
                    logger.debug("[V79-ORB-MID-OR] feed_bar %s b=%d: %s", ticker, bucket, _fe)
            backfilled += 1
        logger.info(
            "[V79-ORB-MID-OR-BACKFILL] archive mid-OR cur_min=%d backfilled=%d tickers",
            cur_min,
            backfilled,
        )
        return
    logger.info(
        "[V79-ORB-BACKFILL] start cur_min=%d or_start=%d or_end=%d tickers=%d",
        cur_min,
        or_start,
        or_end,
        len(scan_universe),
    )
    backfilled = 0
    locked = 0
    for ticker in scan_universe:
        try:
            bars = callbacks.fetch_1min_bars(ticker)
        except Exception as _e:
            logger.warning("[V79-ORB-BACKFILL] fetch failed %s: %s", ticker, _e)
            continue
        if not bars:
            continue
        timestamps = bars.get("timestamps") or []
        opens = bars.get("opens") or []
        highs = bars.get("highs") or []
        lows = bars.get("lows") or []
        closes = bars.get("closes") or []
        volumes = bars.get("volumes") or []
        # Two-pass: (1) in-window bars in chronological order;
        # (2) one post-window bar to force lock via v7.73.0 fallback
        # if the 09:59 bar was missing.
        fed_in_window = 0
        for i, ts in enumerate(timestamps):
            if ts is None:
                continue
            try:
                bucket = minutes_since_et_midnight(int(ts))
            except Exception:
                continue
            if bucket < or_start or bucket >= or_end:
                continue
            if not (i < len(opens) and i < len(highs) and i < len(lows) and i < len(closes)):
                continue
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            if None in (o, h, l, c):
                continue
            v = volumes[i] if i < len(volumes) else 0.0
            try:
                _orb_runtime.feed_bar(
                    ticker=ticker,
                    bar_high=float(h),
                    bar_low=float(l),
                    bar_open=float(o),
                    bar_close=float(c),
                    bar_volume=float(v or 0.0),
                    bar_bucket_min=bucket,
                )
                fed_in_window += 1
            except Exception as _fe:
                logger.warning("[V79-ORB-BACKFILL] feed_bar %s b=%d: %s", ticker, bucket, _fe)
        if fed_in_window == 0:
            continue
        backfilled += 1
        # Force lock via post-window bar (v7.73.0 fallback) in case
        # the 09:59 bucket was missing from the source.
        for i, ts in enumerate(timestamps):
            if ts is None:
                continue
            try:
                bucket = minutes_since_et_midnight(int(ts))
            except Exception:
                continue
            if bucket < or_end:
                continue
            if not (i < len(opens) and i < len(highs) and i < len(lows) and i < len(closes)):
                continue
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            if None in (o, h, l, c):
                continue
            v = volumes[i] if i < len(volumes) else 0.0
            try:
                _orb_runtime.feed_bar(
                    ticker=ticker,
                    bar_high=float(h),
                    bar_low=float(l),
                    bar_open=float(o),
                    bar_close=float(c),
                    bar_volume=float(v or 0.0),
                    bar_bucket_min=bucket,
                )
                locked += 1
            except Exception as _fe:
                logger.warning("[V79-ORB-BACKFILL] post-bar %s b=%d: %s", ticker, bucket, _fe)
            break  # one post-window bar is enough to trigger lock
    logger.info(
        "[V79-ORB-BACKFILL] done backfilled_tickers=%d locked_tickers=%d",
        backfilled,
        locked,
    )


def _orb_post_or_backfill_sweep(
    callbacks: EngineCallbacks, now_et: datetime, scan_universe: list
) -> None:
    """v8.3.0 -- per-cycle automatic OR backfill.

    Runs on every scan cycle (not just the first after a fresh
    session_start). For each ticker whose OR window isn't locked
    AND we're past OR end, fetch today's 1m bars and feed them
    through orb.live_runtime.backfill_or_windows. The engine handles
    idempotency: locked windows reject bars silently, already-locked
    tickers are skipped fast.

    Why this exists in addition to _maybe_backfill_or_window:
    the v7.74.0 hook fires once per date (when _fresh==True from
    ensure_session_started). If that single attempt silently
    failed -- Alpaca bar feed glitched, fetch raised, the bot
    crashed between ensure_session_started and the backfill call --
    the OR window stayed empty for the rest of the day. v8.3.0
    retries on every cycle so we can't miss a trading day to a
    transient glitch.

    Fast-path: when current_et_minutes < or_end_minutes, the live
    scan covers the active OR normally; the engine method returns
    immediately. When all tickers are locked, the engine method
    counts them as skipped without doing any fetch work.
    """
    from orb.engine import OrbConfig

    cfg = OrbConfig()
    cur_min = now_et.hour * 60 + now_et.minute
    if cur_min < cfg.or_end_minutes:
        return  # active OR; live scan handles it
    engine = _orb_runtime.get_engine()
    if engine is None:
        return
    # Identify tickers whose OR isn't yet locked. If everyone is
    # locked, we're done -- skip the fetch entirely.
    snap = engine.snapshot().get("or_windows", {})
    unlocked = []
    for tk in scan_universe:
        row = snap.get(tk) or {}
        if not row.get("locked"):
            unlocked.append(tk)
        else:
            # Pre-locked: nothing to do for this ticker.
            pass
    if not unlocked:
        return
    date_iso = now_et.strftime("%Y-%m-%d")
    or_start = cfg.or_start_minutes if hasattr(cfg, "or_start_minutes") else 570
    bars_by_ticker: dict = {}
    for ticker in unlocked:
        # Primary: local bar archive (fast, no API dependency, survives redeploy).
        rows = _read_bars_from_archive(date_iso, ticker, or_start, cur_min + 5)
        if rows:
            bars_by_ticker[ticker] = rows
            continue
        # Fallback: Alpaca API fetch when archive is empty (e.g. first day on new volume).
        try:
            bars = callbacks.fetch_1min_bars(ticker)
        except Exception as _e:
            logger.debug("[V83-OR-BACKFILL] fetch failed %s: %s", ticker, _e)
            continue
        if not bars:
            continue
        timestamps = bars.get("timestamps") or []
        opens = bars.get("opens") or []
        highs = bars.get("highs") or []
        lows = bars.get("lows") or []
        closes = bars.get("closes") or []
        volumes = bars.get("volumes") or []
        api_rows: list = []
        for i, ts in enumerate(timestamps):
            if ts is None:
                continue
            try:
                bucket = minutes_since_et_midnight(int(ts))
            except Exception:
                continue
            if not (i < len(opens) and i < len(highs) and i < len(lows) and i < len(closes)):
                continue
            o, h, lo, c = opens[i], highs[i], lows[i], closes[i]
            if None in (o, h, lo, c):
                continue
            v = volumes[i] if i < len(volumes) else 0.0
            api_rows.append((bucket, float(h), float(lo), float(o), float(c), float(v or 0.0)))
        if api_rows:
            bars_by_ticker[ticker] = api_rows
    if not bars_by_ticker:
        return
    result = _orb_runtime.backfill_or_windows(
        bars_by_ticker=bars_by_ticker,
        current_et_minutes=cur_min,
    )
    if result and (result.get("backfilled") or result.get("locked")):
        logger.info(
            "[V83-OR-BACKFILL] sweep cur_min=%d backfilled=%d locked=%d skipped=%d failed=%d",
            cur_min,
            result.get("backfilled", 0),
            result.get("locked", 0),
            result.get("skipped", 0),
            result.get("failed", 0),
        )


def _orb_phantom_sweep(tg) -> None:
    """v8.3.15 -- find + clear engine FSM rows that say in_position=True
    but the ticker isn't actually held in the corresponding portfolio's
    positions map.

    Symptom: watchdog `v10_in_pos_has_internal_position` invariant fires
    with main/AMZN phase='in_pos' in_position=True last_entry='', but
    the dashboard shows AMZN closed. Root cause: stale
    `/data/orb_state_<date>.json` written by a pre-v8.3.12 process where
    a close path missed the unmirror; v8.3.4 rehydrate then reloads
    that row on every subsequent bootstrap.

    Self-heals on every scan cycle. When state is clean (no phantoms),
    the engine snapshot returns an empty list and we return without
    touching anything. When phantoms exist, we clear them via
    `OrbEngine.clear_phantom_in_pos` for main, or the executor's
    `_unmirror_position_from_engine` for val/gene (different paths
    because main has no executor instance -- it IS the tg module).
    """
    engine = _orb_runtime.get_engine()
    if engine is None:
        return
    # Build held-tickers per pid.
    held: dict = {}
    try:
        # Main: tg.positions (longs) + tg.short_positions (shorts).
        main_longs = set(getattr(tg, "positions", {}).keys() or [])
        main_shorts = set(getattr(tg, "short_positions", {}).keys() or [])
        held["main"] = main_longs | main_shorts
    except Exception:
        held["main"] = set()
    # Val/Gene: executor.positions (executor only stores one side; v10
    # path mirrors long+short into the same dict per v7.0.0 design).
    try:
        from executors.bootstrap import get_executor

        for pid in ("val", "gene"):
            ex = get_executor(pid)
            if ex is not None:
                held[pid] = set(getattr(ex, "positions", {}).keys() or [])
            else:
                # No executor instance -- we have no data to determine
                # phantoms. Skip rather than wipe (leaves the FSM as-is).
                pass
    except Exception:
        pass
    phantoms = engine.find_phantom_in_pos(held_tickers_by_pid=held)
    # Clear each phantom via the right path (v8.3.15 FSM-side path).
    cleared: list = []
    for pid, ticker in phantoms:
        if pid == "main":
            if engine.clear_phantom_in_pos(pid, ticker):
                cleared.append((pid, ticker))
        else:
            try:
                from executors.bootstrap import get_executor

                ex = get_executor(pid)
                if ex is not None:
                    ex._unmirror_position_from_engine(ticker)
                    cleared.append((pid, ticker))
            except Exception:
                logger.debug(
                    "[V8315-PHANTOM-SWEEP] could not unmirror %s/%s",
                    pid,
                    ticker,
                )
    if cleared:
        logger.warning(
            "[V8315-PHANTOM-SWEEP] cleared %d phantom IN_POS row(s): %s",
            len(cleared),
            cleared,
        )
    # v9.1.96/v9.1.97/v9.1.99 -- bi-directional engine ↔ broker reconciliation.
    # Key fix: held["val"] = ex.positions (paper dict, empty in live mode), NOT
    # Alpaca positions. Using it for the purge caused every injected position to
    # be re-purged on the next cycle. Now we fetch Alpaca positions first and use
    # them as the authoritative broker truth for BOTH purge and inject.
    try:
        from orb.live_runtime import (
            purge_phantom_engine_positions,
            inject_missing_engine_positions,
        )
        from executors.bootstrap import get_executor as _get_ex

        for _pid in ("val", "gene"):
            if held.get(_pid) is None:
                continue  # executor unavailable (get_executor returned None)
            _ex = _get_ex(_pid)
            if _ex is None:
                continue
            try:
                # Use ex.positions (pre-reconciled at startup) instead of
                # get_all_positions() which returns empty in scan context
                # for unknown reasons (possibly auth/mode issue). ex.positions
                # is populated by _reconcile_broker_positions() at boot and
                # updated on every new entry -- always accurate.
                _ex_pos = getattr(_ex, "positions", {}) or {}
                _broker_tuples = []
                _alpaca_tickers: set[str] = set()
                for _sym, _pos in _ex_pos.items():
                    _sym_u = (_sym or "").upper()
                    _side = str(_pos.get("side") or "long").lower()
                    _entry = float(_pos.get("entry_price") or 0)
                    _qty = int(_pos.get("qty") or 0)
                    if _sym_u and _qty > 0:
                        _broker_tuples.append((_sym_u, _side, _entry, _qty))
                        _alpaca_tickers.add(_sym_u)
                # Purge uses ALPACA tickers as truth (not paper positions).
                _purged = purge_phantom_engine_positions(_pid, frozenset(_alpaca_tickers))
                if _purged:
                    logger.warning(
                        "[V9196-RECONCILE] %s: purged %d phantom adapter position(s): %s",
                        _pid,
                        len(_purged),
                        _purged,
                    )
                # Inject Alpaca positions missing from engine.
                _injected = inject_missing_engine_positions(_pid, _broker_tuples)
                if _injected:
                    logger.warning(
                        "[V9197-INJECT] %s: injected %d missing engine position(s): %s",
                        _pid,
                        len(_injected),
                        _injected,
                    )
            except Exception as _ie:
                logger.warning("[V9199-RECONCILE] %s inner: %s", _pid, _ie)
        # v9.1.123 -- extend engine↔broker reconciliation to Main.
        # Pre-v9.1.123 Main was excluded from the inject path because the
        # function docstring restricted it to val/gene; a redeploy after
        # Main acquired a position via the legacy callbacks.execute_entry
        # path left the v10 RiskBook's _open_tickets empty for Main,
        # tripping the no_phantom_positions invariant. (Observed
        # 2026-05-18 10:10 ET AVGO short entry → 11:20 ET earnings-feed-
        # refresh redeploy → 11:25 ET monitor CRIT.) Main's broker truth
        # is tg.positions (longs) + tg.short_positions (shorts), not an
        # executor, so the tuple construction is slightly different from
        # the val/gene path above.
        try:
            _main_longs = getattr(tg, "positions", {}) or {}
            _main_shorts = getattr(tg, "short_positions", {}) or {}
            _main_broker_tuples: list = []
            for _ticker, _pos in _main_longs.items():
                _ticker_u = (_ticker or "").upper()
                _entry = float(_pos.get("entry_price") or 0)
                _qty = int(_pos.get("shares") or 0)
                if _ticker_u and _qty > 0:
                    _main_broker_tuples.append((_ticker_u, "long", _entry, _qty))
            for _ticker, _pos in _main_shorts.items():
                _ticker_u = (_ticker or "").upper()
                _entry = float(_pos.get("entry_price") or 0)
                _qty = int(_pos.get("shares") or 0)
                if _ticker_u and _qty > 0:
                    _main_broker_tuples.append((_ticker_u, "short", _entry, _qty))
            if _main_broker_tuples:
                _injected_main = inject_missing_engine_positions("main", _main_broker_tuples)
                if _injected_main:
                    logger.warning(
                        "[V9197-INJECT] main: injected %d missing engine position(s): %s",
                        len(_injected_main),
                        _injected_main,
                    )
        except Exception as _me:
            logger.warning("[V9199-RECONCILE] main: %s", _me)
    except Exception as _rce:
        logger.warning("[V9199-RECONCILE] outer: %s", _rce)
    # v8.3.20 -- second-level sweep: orphan recover-* tickets in
    # RiskBook._open_tickets where the FSM in_position is False but
    # the ticket still consumes open_risk/open_notional budget. v8.3.15
    # only catches in_position=True rows; this catches the in_position=
    # False ones from partial v8.3.12 unmirrors / mid-write v8.3.4
    # rehydrate snapshots. Without this sweep, leaked tickets blocked
    # new entries with risk_reject:notional_cap because the cap was
    # already consumed by ghost tickets.
    try:
        ticket_phantoms = engine.find_phantom_recover_tickets(
            held_tickers_by_pid=held,
        )
    except Exception:
        ticket_phantoms = []
    ticket_cleared: list = []
    for pid, tid, ticker in ticket_phantoms:
        try:
            if engine.release_recover_ticket(pid, tid):
                ticket_cleared.append((pid, ticker))
        except Exception:
            logger.debug(
                "[V8320-TICKET-SWEEP] release failed %s/%s",
                pid,
                tid,
            )
    if ticket_cleared:
        logger.warning(
            "[V8320-TICKET-SWEEP] released %d phantom recover-* ticket(s): %s",
            len(ticket_cleared),
            ticket_cleared,
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
                        if (
                            _bar_open is not None
                            and _bar_high is not None
                            and _bar_low is not None
                            and _bar_close is not None
                            and ts_val is not None
                        ):
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
                            "[V79-ORB-FEED] %s: %s",
                            ticker,
                            _orb_e,
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
        # v10.0.1 \u2014 Section II.2 Boundary Hold gate retired; the rolling
        # closed-1m buffer (eot_glue.record_latest_1m_close) is no longer
        # needed and the call is deleted.
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
    """Get current equity for `portfolio_id`.

    v8.1.9 -- routes Val/Gene through engine.portfolio_equity.resolve_equity
    FIRST so Alpaca's authoritative account equity wins over
    book.current_equity() (which returns 0 when paper_cash is the
    default 0.0 -- the root cause of the recurring [Gene equity=0]
    watchdog alert). Falls back through book MTM and then paper_cash.

    Why a layered fallback rather than just resolve_equity:
      - resolve_equity returns 0 when Alpaca keys aren't configured
        for that portfolio (silent fail by design); we want to recover
        to book.current_equity() rather than return 0 and reject every
        entry on the notional_cap.
      - book.current_equity() is correct for Main (where paper_cash
        is the source of truth) but returns 0 for Val/Gene with
        un-seeded books.
      - Final fallback to tg.paper_cash maintains v7.x semantics
        for callers that pass an unknown portfolio_id.
    """
    # Tier 1: portfolio_equity (Alpaca-first for Val/Gene)
    try:
        from engine.portfolio_equity import resolve_equity

        eq = float(resolve_equity(portfolio_id))
        if eq > 0:
            return eq
    except Exception:
        pass
    # Tier 2: PortfolioBook current_equity (correct for Main)
    try:
        from engine.portfolio_book import PORTFOLIOS

        book = PORTFOLIOS.get(portfolio_id)
        if book is not None:
            eq = float(book.current_equity())
            if eq > 0:
                return eq
    except Exception:
        pass
    # Tier 3: legacy tg.paper_cash fallback
    return float(getattr(tg, "paper_cash", 100_000.0))


def _v10_dispatch_executor_fire(
    *,
    pid: str,
    side: str,
    ticker: str,
    price: float,
    shares: int,
    callbacks: "EngineCallbacks | None" = None,
    reduce_only: bool = False,
) -> bool:
    """v7.26.0 -- route a non-main v10 admission to its executor's fire_*.

    v9.1.128: always-independent. The only kill switch is ORB_LIVE_MODE;
    the old ORB_PORTFOLIO_FIRE escape hatch was removed.

    v7.30.0: also routes broker submit failures (e.g. Alpaca 5xx) to
    callbacks.report_error so Telegram/dashboard see them instead of
    only the log file.

    v7.81.0: returns True iff a broker order was actually submitted.
    Callers use this to know whether to roll back the v10 admit on
    the FSM + RiskBook -- otherwise a deferred / suppressed / failed
    fire leaves the admit dangling as a phantom IN_POS.

    v9.1.125: `reduce_only=True` forwards to fire_long/fire_short so
    close orders (e.g. _eod_fire_broker_close) bypass the notional
    cap. The cap is for new-exposure entries; closes shrink exposure
    and must not be blocked. Default False preserves entry semantics.
    """
    if not _orb_runtime.is_live_mode_on():
        logger.info(
            "[V79-ORB-ADMIT] %s %s %s -- broker fire suppressed "
            "(ORB_LIVE_MODE=0; kill switch active)",
            pid,
            side,
            ticker,
        )
        return False
    # v9.1.128 -- always-independent. The ORB_PORTFOLIO_FIRE escape
    # hatch was removed; every non-main portfolio fires its own
    # broker order via the executor. The only kill switch remaining
    # is ORB_LIVE_MODE (checked above).
    try:
        from executors.bootstrap import get_executor

        ex = get_executor(pid)
    except Exception as e:
        logger.warning(
            "[V79-ORB-FIRE] %s %s %s get_executor raised: %s",
            pid,
            side,
            ticker,
            e,
        )
        if callbacks is not None:
            try:
                callbacks.report_error(
                    executor=pid,
                    code="V10_FIRE_DISPATCH_LOOKUP",
                    severity="error",
                    summary=f"v10 fire dispatch lookup failed: {pid}",
                    detail=f"{type(e).__name__}: {str(e)[:200]}",
                )
            except Exception:
                logger.exception("[V79-ORB-FIRE] report_error failed")
        return False
    if ex is None:
        logger.info(
            "[V79-ORB-FIRE] %s %s %s -- no executor instance (keys unset); admission tracked only",
            pid,
            side,
            ticker,
        )
        return False

    # v7.30.0: error callback escalates broker submit failures (5xx,
    # timeouts, connection drops) through the standard report_error
    # pipeline so Telegram/dashboard see them.
    _err_cb = None
    if callbacks is not None:

        def _err_cb(name: str, side2: str, ticker2: str, shares2: int, exc: Exception) -> None:
            try:
                callbacks.report_error(
                    executor=pid,
                    code="V10_BROKER_FIRE_FAILED",
                    severity="error",
                    summary=f"v10 broker fire failed: {name} {side2} {ticker2}",
                    detail=(f"{type(exc).__name__}: {str(exc)[:200]} (qty={shares2}, pid={pid})"),
                )
            except Exception:
                logger.exception("[V79-ORB-FIRE] report_error failed")

    try:
        if side == "long":
            ok = ex.fire_long(
                ticker,
                float(price),
                int(shares),
                error_callback=_err_cb,
                reduce_only=reduce_only,
            )
        else:
            ok = ex.fire_short(
                ticker,
                float(price),
                int(shares),
                error_callback=_err_cb,
                reduce_only=reduce_only,
            )
        logger.info(
            "[V79-ORB-FIRE] %s %s %s qty=%d submitted=%s",
            pid,
            side,
            ticker,
            int(shares),
            ok,
        )
        return bool(ok)
    except Exception as e:
        # v7.32.0: ERROR-level (was WARNING) since this is an executor
        # bug (not a broker error -- those are escalated via the
        # error_callback path above) and the position is in flight but
        # unexecuted. include_exc_info=True so the traceback hits the
        # log even when callbacks is None.
        logger.error(
            "[V79-ORB-FIRE] %s %s %s fire raised %s: %s",
            pid,
            side,
            ticker,
            type(e).__name__,
            e,
            exc_info=True,
        )
        if callbacks is not None:
            try:
                callbacks.report_error(
                    executor=pid,
                    code="V10_FIRE_DISPATCH_EXCEPTION",
                    severity="error",
                    summary=f"v10 fire dispatch raised: {pid} {side} {ticker}",
                    detail=f"{type(e).__name__}: {str(e)[:200]}",
                )
            except Exception:
                logger.exception("[V79-ORB-FIRE] report_error failed")
        return False


def _load_eod_prior_closes(
    date_iso: str,
    universe: tuple[str, ...] | list[str],
) -> dict[str, float]:
    """v9.1.25 -- extracted helper for the EOD reversal prior-close
    lookup. Reads the bar archive at /data/bars/<D-n>/<TICKER>.jsonl
    and returns the most-recent RTH close (et_bucket in [930, 1559])
    per ticker. Walks back up to 10 calendar days to skip weekends
    and holidays. Fail-open: missing data leaves the ticker out of
    the returned dict; the caller's `select_signals` skips it cleanly.

    Pulled out of `_eod_reversal_pass` so the runtime integration
    test in tests/strategy/test_eod_reversal_scan_integration.py can
    monkeypatch this single function rather than patching
    `pathlib.Path` globally (which breaks pytest's own Path usage).

    Look-ahead audit: only reads bars whose et_bucket falls in the
    prior session's RTH window. No future-leaking data path.
    """
    prior_closes: dict[str, float] = {}
    if not date_iso:
        return prior_closes
    try:
        from pathlib import Path as _Path
        import json as _json
        from datetime import timedelta as _td
        from datetime import datetime as _dt

        root = _Path("/data/bars")
        for tk in universe:
            if tk in prior_closes:
                continue
            found = None
            dt = _dt.strptime(date_iso, "%Y-%m-%d")
            for off in range(1, 11):
                cand = (dt - _td(days=off)).strftime("%Y-%m-%d")
                fp = root / cand / f"{tk}.jsonl"
                if not fp.is_file():
                    continue
                last_close = None
                for line in fp.read_text().splitlines():
                    if not line.strip():
                        continue
                    try:
                        b = _json.loads(line)
                    except Exception:
                        continue
                    bucket = b.get("et_bucket", "")
                    try:
                        bi = int(str(bucket))
                    except (ValueError, TypeError):
                        continue
                    if 930 <= bi <= 1559:
                        c = b.get("close")
                        if isinstance(c, (int, float)) and c > 0:
                            last_close = float(c)
                if last_close is not None:
                    found = last_close
                    break
            if found is not None:
                prior_closes[tk] = found
    except Exception:
        logger.exception("[V910-EOD] prior-close lookup failed")
    return prior_closes


def _eod_reversal_pass(callbacks: EngineCallbacks, cur_min: int) -> None:
    """v9.1.0 -- per-cycle hook for the EOD reversal addon strategy.

    No-op outside the [entry_et, exit_et] window. Inside the entry
    minute (default 15:30 ET) ranks the EOD universe and admits the
    top-1 long + top-1 short per portfolio. At/after the exit minute
    (default 15:59) flattens any open EOD positions.

    Idempotent within a session via per-portfolio `entry_attempted`
    flag (admission) and per-portfolio open_positions dict (exits).

    Tracks positions for dashboard regardless of broker mode. When
    cfg.fire_broker is True, also dispatches real broker orders via
    callbacks.execute_entry (Main) and _v10_dispatch_executor_fire
    (Val/Gene). Paper-fire-observation default: fire_broker=False.
    """
    eod = _orb_runtime.get_eod_engine()
    if eod is None:
        return
    # Outside the trading window (RTH).
    if cur_min < eod.cfg.entry_et_minutes - 1:
        return
    # Ensure session reset has fired for today's date.
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        date_iso = datetime.now(ZoneInfo("US/Eastern")).strftime("%Y-%m-%d")
        eod.reset_for_session(date_iso)
    except Exception:
        date_iso = ""

    # --- entry window ---
    if eod.is_entry_window(cur_min):
        # Gather current prices via callbacks for the EOD universe.
        current_prices: dict[str, float] = {}
        for tk in eod.cfg.universe:
            try:
                bars = callbacks.fetch_1min_bars(tk)
            except Exception:
                bars = None
            if not bars:
                continue
            px = bars.get("current_price") or (bars.get("closes") or [None])[-1]
            if isinstance(px, (int, float)) and px > 0:
                current_prices[tk] = float(px)
        # Prior-session closes from the production bar archive.
        # v9.1.25 -- pulled out into module-level _load_eod_prior_closes
        # so tests/strategy/test_eod_reversal_scan_integration.py can
        # monkeypatch this single seam instead of patching pathlib.Path
        # globally (which breaks pytest's own internal Path usage).
        prior_closes = _load_eod_prior_closes(date_iso, eod.cfg.universe)

        long_picks, short_picks = eod.select_signals(
            current_prices=current_prices,
            prior_closes=prior_closes,
        )
        if not long_picks and not short_picks:
            logger.info(
                "[V910-EOD-NO-SIGNAL] date=%s cur_prices=%d prior_closes=%d "
                "long_picks=0 short_picks=0",
                date_iso,
                len(current_prices),
                len(prior_closes),
            )
            return

        from datetime import datetime as _dt2, timezone as _tz

        entry_iso = _dt2.now(_tz.utc).isoformat()

        # Fire per-portfolio. Each portfolio admits independently with
        # its own equity for sizing.
        try:
            from engine.portfolio_book import ALL_PORTFOLIO_IDS, PORTFOLIOS
        except Exception:
            ALL_PORTFOLIO_IDS = ["main"]
            PORTFOLIOS = {"main": None}
        for pid in ALL_PORTFOLIO_IDS:
            if eod.has_attempted(pid):
                continue
            book = PORTFOLIOS.get(pid)
            # v9.1.21 SEV-1 HOTFIX -- current_equity is a METHOD on
            # PortfolioBook (def current_equity(self, prices=None) -> float),
            # not an attribute. Pre-v9.1.21 used
            # `getattr(book, "current_equity", 100_000.0)` which returned
            # the bound method (truthy), then `float(<bound_method>)`
            # raised TypeError. Same crash class as the v9.1.20 cur_min
            # NameError: silently caught by the outer wrapper, never
            # admitted. Call it as a method.
            try:
                equity = float(book.current_equity()) if book else 100_000.0
            except Exception:
                equity = 100_000.0
            if equity <= 0:
                equity = 100_000.0
            for ticker, rod3 in long_picks:
                price = current_prices.get(ticker)
                if price is None or price <= 0:
                    continue
                pos = eod.admit(
                    portfolio_id=pid,
                    ticker=ticker,
                    side="long",
                    entry_price=price,
                    equity=equity,
                    rod3_bps=rod3,
                    entry_iso=entry_iso,
                )
                if pos is not None and eod.cfg.fire_broker:
                    _eod_fire_broker(callbacks, pid, ticker, "long", price, pos.shares)
            for ticker, rod3 in short_picks:
                price = current_prices.get(ticker)
                if price is None or price <= 0:
                    continue
                pos = eod.admit(
                    portfolio_id=pid,
                    ticker=ticker,
                    side="short",
                    entry_price=price,
                    equity=equity,
                    rod3_bps=rod3,
                    entry_iso=entry_iso,
                )
                if pos is not None and eod.cfg.fire_broker:
                    _eod_fire_broker(callbacks, pid, ticker, "short", price, pos.shares)
            eod.mark_attempted(pid)

    # --- v9.1.104: intraday stop check (runs every scan cycle during hold) ---
    if not eod.is_exit_window(cur_min):
        from datetime import datetime as _dt_s, timezone as _tz_s

        _stop_iso = _dt_s.now(_tz_s.utc).isoformat()
        for _spid, _sst in list(eod._states.items()):
            for _stk, _spos in list(_sst.open_positions.items()):
                if _spos.stop_price <= 0:
                    continue
                try:
                    _sbars = callbacks.fetch_1min_bars(_stk)
                    _spx = _sbars.get("current_price") if _sbars else None
                    if not isinstance(_spx, (int, float)) or _spx <= 0:
                        continue
                    _hit = (_spos.side == "long" and _spx <= _spos.stop_price) or (
                        _spos.side == "short" and _spx >= _spos.stop_price
                    )
                    if _hit:
                        logger.warning(
                            "[V910-EOD-STOP] %s %s %s stop=%.4f mark=%.4f",
                            _spid,
                            _stk,
                            _spos.side,
                            _spos.stop_price,
                            _spx,
                        )
                        _sleg = eod.close(
                            portfolio_id=_spid,
                            ticker=_stk,
                            exit_price=float(_spx),
                            exit_iso=_stop_iso,
                            exit_reason="stop",
                        )
                        if _sleg is not None:
                            _eod_append_trade_log(_sleg)
                        if _sleg is not None and eod.cfg.fire_broker:
                            _eod_fire_broker_close(
                                callbacks, _spid, _stk, _spos.side, float(_spx), _spos.shares
                            )
                except Exception as _se:
                    logger.debug("[V910-EOD-STOP] %s/%s check failed: %s", _spid, _stk, _se)

    # --- exit window ---
    if eod.is_exit_window(cur_min):
        from datetime import datetime as _dt3, timezone as _tz3

        exit_iso = _dt3.now(_tz3.utc).isoformat()
        for pid, st in list(eod._states.items()):
            if not st.open_positions:
                continue
            for ticker, pos in list(st.open_positions.items()):
                try:
                    bars = callbacks.fetch_1min_bars(ticker)
                except Exception:
                    bars = None
                if not bars:
                    continue
                px = bars.get("current_price") or (bars.get("closes") or [None])[-1]
                if not isinstance(px, (int, float)) or px <= 0:
                    continue
                leg = eod.close(
                    portfolio_id=pid,
                    ticker=ticker,
                    exit_price=float(px),
                    exit_iso=exit_iso,
                    exit_reason="eod_window",
                )
                if leg is not None:
                    # v9.1.69 -- persist to /data/eod_trade_log.jsonl so
                    # Today's Trades survives Railway redeploys. In-memory
                    # closed_legs is lost on each new instance boot; writing
                    # to the persistent volume is the only durable record for
                    # Main (Val/Gene also write here for symmetry, though
                    # they can fall back to Alpaca order history).
                    _eod_append_trade_log(leg)
                if leg is not None and eod.cfg.fire_broker:
                    _eod_fire_broker_close(callbacks, pid, ticker, pos.side, float(px), pos.shares)


def _eod_append_trade_log(leg: dict) -> None:
    """v9.1.69 -- append a closed EOD leg to /data/eod_trade_log.jsonl.

    Each line is a JSON object. The dashboard reads this file as a
    fallback when EodReversalEngine._states has empty closed_legs (e.g.
    after a Railway redeploy mid-session or post-15:59 ET restart).
    Never raises -- a write failure is logged and silently ignored.
    """
    try:
        import json as _json
        import os as _os

        path = _os.environ.get("EOD_TRADE_LOG_FILE", "/data/eod_trade_log.jsonl")
        _os.makedirs(_os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as _f:
            _f.write(_json.dumps(leg) + "\n")
    except Exception:
        logger.exception("[V910-EOD] eod_trade_log append failed")


def _eod_fire_broker(
    callbacks: EngineCallbacks, pid: str, ticker: str, side: str, price: float, shares: int
) -> None:
    """v9.1.0 -- dispatch a real broker fire for an EOD entry.

    Reuses the existing v10 fanout: Main goes through
    callbacks.execute_entry (legacy path), Val/Gene via
    _v10_dispatch_executor_fire. Failures are logged but do not
    propagate -- tracking continues even if broker rejects.
    """
    try:
        if pid == "main":
            # The legacy callback fires LONG by default. For short side
            # we rely on _v10_dispatch_executor_fire which is side-aware.
            if side == "long":
                callbacks.execute_entry(ticker, float(price))
            else:
                _v10_dispatch_executor_fire(
                    pid=pid,
                    side=side,
                    ticker=ticker,
                    price=float(price),
                    shares=int(shares),
                )
        else:
            _v10_dispatch_executor_fire(
                pid=pid,
                side=side,
                ticker=ticker,
                price=float(price),
                shares=int(shares),
            )
        logger.info(
            "[V910-EOD-FIRE] %s/%s %s %d shares @ %.4f",
            pid,
            ticker,
            side,
            shares,
            price,
        )
    except Exception:
        logger.exception(
            "[V910-EOD-FIRE-FAIL] %s/%s %s shares=%d price=%.4f",
            pid,
            ticker,
            side,
            shares,
            price,
        )


def _eod_fire_broker_close(
    callbacks: EngineCallbacks, pid: str, ticker: str, side: str, price: float, shares: int
) -> None:
    """v9.1.0 -- broker close for an EOD position. Uses the inverse
    side on the same executor surface (long -> sell, short -> cover).

    v9.1.125: routes with reduce_only=True so the cumulative-notional
    cap in fire_long/fire_short does NOT block the close. The 2026-05-18
    incident showed the cap silently rejected the close (submitted=False)
    when account was at 95% notional from the entry pair; only Alpaca's
    broker-side EOD auto-flush kept the live position from staying open.
    """
    try:
        close_side = "short" if side == "long" else "long"
        _v10_dispatch_executor_fire(
            pid=pid,
            side=close_side,
            ticker=ticker,
            price=float(price),
            shares=int(shares),
            reduce_only=True,
        )
        logger.info(
            "[V910-EOD-CLOSE-FIRE] %s/%s closing %s %d shares @ %.4f (reduce_only)",
            pid,
            ticker,
            side,
            shares,
            price,
        )
    except Exception:
        logger.exception(
            "[V910-EOD-CLOSE-FIRE-FAIL] %s/%s %s shares=%d price=%.4f",
            pid,
            ticker,
            side,
            shares,
            price,
        )


def _v10_dispatch_executor_partial_close(
    *,
    pid: str,
    ticker: str,
    shares: int,
    price: float,
    reason: str,
) -> bool:
    """v9.1.127 -- route a per-portfolio partial-close to its executor.

    Mirrors _v10_dispatch_executor_fire but uses the executor's
    _partial_close_position_idempotent. Used by the per-portfolio
    exit pass when the v10 engine returns a partial-profit decision
    so Val/Gene take their 1R partials independently of Main's bus.

    Returns True if the executor was found and the partial was
    dispatched; False if the executor is missing or the keys are
    unset.
    """
    if not _orb_runtime.is_live_mode_on():
        return False
    try:
        from executors.bootstrap import get_executor

        ex = get_executor(pid)
    except Exception as e:
        logger.warning("[V9127-PARTIAL-FIRE] %s %s get_executor raised: %s", pid, ticker, e)
        return False
    if ex is None:
        return False
    try:
        client = ex._ensure_client()
        if client is None:
            return False
        ex._partial_close_position_idempotent(
            client,
            ticker,
            int(shares),
            f"{ex.NAME} {ex.mode}",
            reason,
        )
        logger.info(
            "[V9127-PARTIAL-FIRE] %s %s shares=%d @ %.4f reason=%s",
            pid,
            ticker,
            int(shares),
            float(price),
            reason,
        )
        return True
    except Exception:
        logger.exception("[V9127-PARTIAL-FIRE] %s %s partial-close raised", pid, ticker)
        return False


def _v10_per_portfolio_exit_pass(callbacks: EngineCallbacks) -> None:
    """v9.1.127 -- per-portfolio EXIT pass for non-main portfolios.

    Each non-main portfolio (Val, Gene) evaluates its OWN open positions
    against the v10 engine's exit logic and fires its own broker close.
    No longer hooked to Main's bus EXIT_LONG/EXIT_SHORT signals.

    Mirrors what broker/positions.py:manage_positions does for Main
    (calls _orb_runtime.check_exit_by_ticker, then fires the close)
    but routes broker fires through executors/base.py:fire_*(reduce_only=True)
    instead of the legacy close_breakout pipeline.

    Companion: executors/base.py:_on_signal skips EXIT_LONG/EXIT_SHORT
    and PARTIAL_EXIT_* in independent mode so this loop owns ALL exit
    firing for Val/Gene.

    v9.1.128: always runs. The only kill switch is ORB_LIVE_MODE.
    """
    if not _orb_runtime.is_live_mode_on():
        return

    engine = _orb_runtime.get_engine()
    if engine is None:
        return

    for pid in list(engine.portfolio_ids):
        if pid == "main":
            # Main exits via broker/positions.py:manage_positions
            # (called from trade_genius.py scan_loop -> callbacks.manage_positions).
            # Same v10 check_exit_by_ticker logic, different broker pipeline.
            continue

        adapter = _orb_runtime.get_adapter(pid)
        if adapter is None:
            continue

        open_positions = adapter.list_open_positions()
        if not open_positions:
            continue

        for pos in open_positions:
            ticker = pos.ticker
            try:
                bars = callbacks.fetch_1min_bars(ticker)
            except Exception as e:
                logger.warning("[V9127-EXIT-BAR] %s/%s fetch failed: %s", pid, ticker, e)
                continue
            if not bars:
                continue

            current_price = bars.get("current_price")
            if not isinstance(current_price, (int, float)) or current_price <= 0:
                continue

            try:
                _ts_arr = bars.get("timestamps") or []
                _bucket = minutes_since_et_midnight(int(_ts_arr[-1])) if _ts_arr else 600
            except Exception:
                _bucket = 600
            _highs = bars.get("highs") or []
            _lows = bars.get("lows") or []
            _bar_h = float(_highs[-1] if _highs and _highs[-1] is not None else current_price)
            _bar_l = float(_lows[-1] if _lows and _lows[-1] is not None else current_price)

            try:
                result = _orb_runtime.check_exit_by_ticker(
                    portfolio_id=pid,
                    ticker=ticker,
                    bar_high=_bar_h,
                    bar_low=_bar_l,
                    bar_close=float(current_price),
                    bar_bucket_min=_bucket,
                )
            except Exception:
                logger.exception("[V9127-EXIT] %s/%s check_exit_by_ticker raised", pid, ticker)
                continue

            if result.exit:
                logger.info(
                    "[V9127-EXIT] %s/%s %s exit reason=%s price=%.4f shares=%d",
                    pid,
                    ticker,
                    pos.side,
                    result.reason,
                    float(result.price or 0.0),
                    int(pos.shares),
                )
                close_side = "short" if pos.side == "long" else "long"
                _v10_dispatch_executor_fire(
                    pid=pid,
                    side=close_side,
                    ticker=ticker,
                    price=float(result.price or current_price),
                    shares=int(pos.shares),
                    callbacks=callbacks,
                    reduce_only=True,
                )
                # v9.1.128 (audit fix): record post-trade cooldown on
                # the portfolio's PortfolioBook. Pre-v9.1.127 this was
                # set inside executors/base.py:_on_signal's EXIT_LONG/
                # EXIT_SHORT handler -- now unreachable in always-
                # independent mode. Without this call the Keystone
                # cooldown lever (ORB_POST_TRADE_COOLDOWN_MIN=10,
                # +$42,573/yr) was silently disabled for Val/Gene.
                try:
                    from engine.portfolio_book import PORTFOLIOS as _pb_map

                    _pb_pid = _pb_map.get(pid)
                    if _pb_pid is not None:
                        _pb_pid.record_post_trade(ticker, pos.side.lower())
                except Exception:
                    logger.debug(
                        "[V9128-COOLDOWN] %s/%s record_post_trade skipped",
                        pid,
                        ticker,
                        exc_info=True,
                    )
            elif getattr(result, "partial", False):
                logger.info(
                    "[V9127-EXIT-PARTIAL] %s/%s %s shares=%d @ %.4f booked=$%.2f",
                    pid,
                    ticker,
                    pos.side,
                    int(getattr(result, "partial_shares", 0) or 0),
                    float(getattr(result, "partial_price", 0) or 0.0),
                    float(getattr(result, "partial_pnl_dollars", 0) or 0.0),
                )
                _v10_dispatch_executor_partial_close(
                    pid=pid,
                    ticker=ticker,
                    shares=int(getattr(result, "partial_shares", 0) or 0),
                    price=float(getattr(result, "partial_price", 0) or 0.0),
                    reason="V10_PARTIAL_1R",
                )


def _compute_session_vwap_from_bars(bars_for_mtm: dict | None) -> float:
    """v9.0.0 -- session-cumulative VWAP from the 1m bar series.

    `bars_for_mtm` is the per-ticker 1m bar dict supplied by
    `callbacks.fetch_1min_bars`. It carries parallel arrays
    `opens / highs / lows / closes / volumes / timestamps`.

    Returns 0.0 when bars are missing or the session has not opened
    yet (no RTH bars in the series). Caller treats 0.0 as "no VWAP
    available" and the v9 chase filter fails OPEN in that case so
    a fresh-bootstrap session is never stranded.

    Look-ahead audit: only consumes bars whose timestamp resolves to
    a RTH minute-of-day in [09:30, 15:59] ET; no future data.
    """
    if not bars_for_mtm:
        return 0.0
    highs = bars_for_mtm.get("highs") or []
    lows = bars_for_mtm.get("lows") or []
    closes = bars_for_mtm.get("closes") or []
    vols = bars_for_mtm.get("volumes") or []
    ts_arr = bars_for_mtm.get("timestamps") or []
    n = min(len(highs), len(lows), len(closes), len(vols), len(ts_arr))
    if n == 0:
        return 0.0
    try:
        from engine.timing import minutes_since_et_midnight
    except Exception:
        return 0.0
    cum_pv = 0.0
    cum_v = 0.0
    for i in range(n):
        ts = ts_arr[i]
        try:
            if isinstance(ts, (int, float)):
                import datetime as _dt

                bucket_min = minutes_since_et_midnight(
                    _dt.datetime.fromtimestamp(int(ts), tz=_dt.timezone.utc)
                )
            elif isinstance(ts, str):
                import datetime as _dt

                dt = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                bucket_min = minutes_since_et_midnight(dt)
            else:
                continue
        except Exception:
            continue
        if bucket_min is None or bucket_min < 9 * 60 + 30 or bucket_min > 15 * 60 + 59:
            continue
        h = highs[i]
        lo = lows[i]
        c = closes[i]
        v = vols[i]
        if not (
            isinstance(h, (int, float))
            and isinstance(lo, (int, float))
            and isinstance(c, (int, float))
            and isinstance(v, (int, float))
            and v > 0
        ):
            continue
        typical = (float(h) + float(lo) + float(c)) / 3.0
        cum_pv += typical * float(v)
        cum_v += float(v)
    return cum_pv / cum_v if cum_v > 0 else 0.0


def _orb_long_entry(callbacks: EngineCallbacks, tg, ticker: str, bars_for_mtm: dict | None) -> None:
    """v10 ORB long-entry path -- per-portfolio fanout.

    v7.23.0: iterates ALL enabled portfolios (Main / Val / Gene) and
    runs check_entry independently for each. Each portfolio has its
    own RiskBook + FSM so admissions are isolated.

    Broker execution: "main" routes through callbacks.execute_entry
    (legacy pipeline). Val/Gene route through
    _v10_dispatch_executor_fire -> executor.fire_long (v8.3.23+).
    Exits route through _v10_per_portfolio_exit_pass for non-main
    (v9.1.127+) and broker/positions.py:manage_positions for main.
    """
    try:
        from engine.bars import compute_5m_ohlc_and_ema9

        _5m = compute_5m_ohlc_and_ema9(bars_for_mtm)
        if not _5m or not _5m.get("closes"):
            return  # no closed 5m bar yet
        five_min_close = _5m["closes"][-1]
        next_open = (bars_for_mtm or {}).get("current_price") or five_min_close
        # v8.0.0 -- pass recent 5m HLC so the engine can compute ATR
        # when ORB_ATR_STOP_MULT > 0. Cap to last (lookback+1) bars to
        # keep payload small; engine averages internally.
        _h5 = list(_5m.get("highs") or [])[-20:]
        _l5 = list(_5m.get("lows") or [])[-20:]
        _c5 = list(_5m.get("closes") or [])[-20:]
        # v9.0.0: session-cumulative VWAP for the chase-prevention
        # filter. Computed once per side per tick; 0.0 = fail-open.
        _session_vwap = _compute_session_vwap_from_bars(bars_for_mtm)

        # Resolve all enabled portfolio_ids from the live runtime
        engine = _orb_runtime.get_engine()
        if engine is None:
            return
        portfolio_ids = list(engine.portfolio_ids)

        # v9.1.8 HOTFIX -- compute signal_iso from current UTC so the
        # v9.1.7 time_cutoff check (which parses signal_bar_close_iso
        # to ET minutes) actually fires. Pre-v9.1.8 scan.py defaulted
        # signal_iso to "" -> _utc_iso_to_et_minutes returns None ->
        # cutoff fails-open -> v9.1.7 was effectively dead in
        # production. The signal bar is by definition the bar that
        # just closed within the past few seconds of this scan cycle,
        # so wall-clock UTC is a tight approximation of the bar's
        # close time and good to the minute (which is the cutoff
        # comparison's granularity).
        _signal_iso = datetime.now(timezone.utc).isoformat()
        for pid in portfolio_ids:
            equity = _resolve_portfolio_equity(tg, pid)
            result = _orb_runtime.check_entry(
                portfolio_id=pid,
                ticker=ticker,
                side="long",
                five_min_close=float(five_min_close),
                next_open=float(next_open),
                equity=equity,
                signal_iso=_signal_iso,
                recent_5m_highs=_h5,
                recent_5m_lows=_l5,
                recent_5m_closes=_c5,
                session_vwap=_session_vwap,
            )
            if result.ok:
                logger.info(
                    "[V79-ORB-ENTRY] long %s portfolio=%s "
                    "price=%.4f stop=%.4f target=%.4f shares=%d ticket=%s",
                    ticker,
                    pid,
                    result.price,
                    result.stop,
                    result.target,
                    result.shares,
                    result.ticket_id[:8],
                )
                # Stash size for the broker sizing handoff
                try:
                    _orb_runtime.stash_v10_size(pid, ticker, result.shares)
                except Exception:
                    pass
                # Broker fire: main -> legacy callbacks.execute_entry;
                # val/gene -> _v10_dispatch_executor_fire (v8.3.23+).
                if pid == "main":
                    _fired_main = False
                    try:
                        callbacks.execute_entry(ticker, result.price)
                        # v7.81.0 -- verify the broker call actually
                        # populated tg.positions. execute_breakout has
                        # ~10 early-return gates (daily loss, cutoff,
                        # cooldown, insufficient cash, ...) that each
                        # skip the tg.positions write without raising.
                        # If the write didn't land, roll back the v10
                        # admit so the FSM doesn't stick at IN_POS.
                        _fired_main = ticker in getattr(tg, "positions", {})
                    except Exception as e:
                        callbacks.report_error(
                            executor="main",
                            code="ORB_LONG_ENTRY_EXCEPTION",
                            severity="error",
                            summary=f"ORB long entry exception: {ticker}",
                            detail=f"{type(e).__name__}: {str(e)[:200]}",
                        )
                    if not _fired_main:
                        try:
                            _orb_runtime.rollback_admit(
                                pid,
                                ticker,
                                result.ticket_id,
                                reason="execute_entry early-returned without populating tg.positions",
                                side="long",
                            )
                        except Exception:
                            pass
                else:
                    _fired_other = _v10_dispatch_executor_fire(
                        pid=pid,
                        side="long",
                        ticker=ticker,
                        price=result.price,
                        shares=result.shares,
                        callbacks=callbacks,
                    )
                    # v7.81.0 -- if the executor wasn't available
                    # (no keys, kill switch), roll back the admit so
                    # Val/Gene FSM doesn't stick IN_POS.
                    if not _fired_other:
                        try:
                            _orb_runtime.rollback_admit(
                                pid,
                                ticker,
                                result.ticket_id,
                                reason="executor broker-fire unavailable",
                                side="long",
                            )
                        except Exception:
                            pass
            elif result.reason_no and result.reason_no != "no_signal":
                logger.debug(
                    "[V79-ORB-REJECT] long %s portfolio=%s reason=%s",
                    ticker,
                    pid,
                    result.reason_no,
                )
    except Exception as e:
        logger.warning("[V79-ORB] long entry error %s: %s", ticker, e)


def _orb_short_entry(
    callbacks: EngineCallbacks, tg, ticker: str, bars_for_mtm: dict | None
) -> None:
    """v10 ORB short-entry path -- per-portfolio fanout. Mirror of
    _orb_long_entry."""
    try:
        from engine.bars import compute_5m_ohlc_and_ema9

        _5m = compute_5m_ohlc_and_ema9(bars_for_mtm)
        if not _5m or not _5m.get("closes"):
            return
        five_min_close = _5m["closes"][-1]
        next_open = (bars_for_mtm or {}).get("current_price") or five_min_close
        # v8.0.0 -- 5m HLC for ATR (engine averages internally)
        _h5 = list(_5m.get("highs") or [])[-20:]
        _l5 = list(_5m.get("lows") or [])[-20:]
        _c5 = list(_5m.get("closes") or [])[-20:]
        # v9.0.0: session-cumulative VWAP for the chase-prevention
        # filter. Computed once per side per tick; 0.0 = fail-open.
        _session_vwap = _compute_session_vwap_from_bars(bars_for_mtm)
        engine = _orb_runtime.get_engine()
        if engine is None:
            return
        portfolio_ids = list(engine.portfolio_ids)
        # v9.1.8 HOTFIX -- same signal_iso wiring as the long path; see
        # _orb_long_entry comment.
        _signal_iso = datetime.now(timezone.utc).isoformat()
        for pid in portfolio_ids:
            equity = _resolve_portfolio_equity(tg, pid)
            result = _orb_runtime.check_entry(
                portfolio_id=pid,
                ticker=ticker,
                side="short",
                five_min_close=float(five_min_close),
                next_open=float(next_open),
                equity=equity,
                signal_iso=_signal_iso,
                recent_5m_highs=_h5,
                recent_5m_lows=_l5,
                recent_5m_closes=_c5,
                session_vwap=_session_vwap,
            )
            if result.ok:
                logger.info(
                    "[V79-ORB-ENTRY] short %s portfolio=%s "
                    "price=%.4f stop=%.4f target=%.4f shares=%d ticket=%s",
                    ticker,
                    pid,
                    result.price,
                    result.stop,
                    result.target,
                    result.shares,
                    result.ticket_id[:8],
                )
                try:
                    _orb_runtime.stash_v10_size(pid, ticker, result.shares)
                except Exception:
                    pass
                if pid == "main":
                    _fired_main_s = False
                    try:
                        callbacks.execute_short_entry(ticker, result.price)
                        # v7.81.0 -- post-check tg.short_positions
                        _fired_main_s = ticker in getattr(tg, "short_positions", {})
                    except Exception as e:
                        callbacks.report_error(
                            executor="main",
                            code="ORB_SHORT_ENTRY_EXCEPTION",
                            severity="error",
                            summary=f"ORB short entry exception: {ticker}",
                            detail=f"{type(e).__name__}: {str(e)[:200]}",
                        )
                    if not _fired_main_s:
                        try:
                            _orb_runtime.rollback_admit(
                                pid,
                                ticker,
                                result.ticket_id,
                                reason="execute_short_entry early-returned without populating tg.short_positions",
                                side="short",
                            )
                        except Exception:
                            pass
                else:
                    _fired_other_s = _v10_dispatch_executor_fire(
                        pid=pid,
                        side="short",
                        ticker=ticker,
                        price=result.price,
                        shares=result.shares,
                        callbacks=callbacks,
                    )
                    if not _fired_other_s:
                        try:
                            _orb_runtime.rollback_admit(
                                pid,
                                ticker,
                                result.ticket_id,
                                reason="executor broker-fire deferred or unavailable",
                                side="short",
                            )
                        except Exception:
                            pass
            elif result.reason_no and result.reason_no != "no_signal":
                logger.debug(
                    "[V79-ORB-REJECT] short %s portfolio=%s reason=%s",
                    ticker,
                    pid,
                    result.reason_no,
                )
    except Exception as e:
        logger.warning("[V79-ORB] short entry error %s: %s", ticker, e)
