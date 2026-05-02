"""broker.positions \u2014 per-tick position management.

Extracted from trade_genius.py in v5.11.2 PR 3.
"""

from __future__ import annotations

import logging
import sys as _sys

import time as _time

from broker.orders import check_breakout  # noqa: F401

# v5.26.0 \u2014 broker.stops module deleted. Imports were unused in this
# file's body and the surviving R-2 hard stop flows through the
# sentinel exit path.
from engine.alarm_f_trail import TrailState, atr_from_bars
from engine.bars import compute_5m_ohlc_and_ema9
from engine.momentum_state import ADXTrendWindow, DivergenceMemory, TradeHVP
from engine import sentinel as _sentinel_mod
from engine.sentinel import (
    ALARM_B_CONFIRM_BARS,
    EXIT_REASON_PRICE_STOP,
    SIDE_LONG as _SENTINEL_SIDE_LONG,
    SIDE_SHORT as _SENTINEL_SIDE_SHORT,
    evaluate_sentinel,
    format_sentinel_log,
    maybe_reset_pnl_baseline_on_shares_change,
    new_pnl_history,
    record_pnl,
)

# v5.15.1 vAA-1 \u2014 Sentinel-loop momentum state caches.
#
# These three module-level holders are the live in-memory state that
# Alarms C, D, and E read on every sentinel tick:
#
#   _adx_window_per_position : per-(ticker, side) 3-element 1m ADX ring.
#       Seeded lazily on the first _run_sentinel call for a position;
#       cleared at session reset and on close.
#   _divergence_memory       : single global DivergenceMemory keyed by
#       (ticker, side). session_reset() is called at 09:30 ET via the
#       same hook that clears _v570_strike_counts.
#   _trade_hvp_per_position  : per-(ticker, side) TradeHVP. Instantiated
#       and ``on_strike_open(initial_adx_5m)``-seeded at Strike fill
#       time (broker.orders.execute_breakout). Updated on every
#       _run_sentinel tick with the current 5m ADX.
#
# All three are intentionally module-level (not attached to ``pos``)
# because the sentinel loop runs across positions and we want a single
# mutation point. The session_reset hook calls reset_session_state()
# below, which clears all three and also fires DivergenceMemory's
# session_reset() so the underlying dict is wiped.
_adx_window_per_position: dict = {}
_divergence_memory: DivergenceMemory = DivergenceMemory()
_trade_hvp_per_position: dict = {}

# v6.0.4 \u2014 once-per-(ticker, side, error-class) escalation flags. The
# Sentinel hot path is wrapped in a broad ``try/except Exception`` to keep
# a single bad tick from killing the scan loop, but a swallowed warning
# every cycle is easy to miss. The set below tracks which (ticker, side,
# exc_type) tuples have already been escalated to ``[SENTINEL][CRITICAL]``
# so we log the loud version exactly once per process while still
# preserving the existing per-tick warning trail.
_sentinel_critical_seen: set = set()


def get_divergence_memory() -> DivergenceMemory:
    """Return the singleton DivergenceMemory used by Alarm E.

    Exposed so trade_genius.py session reset hook (and tests) can
    call session_reset() without importing the private name.
    """
    return _divergence_memory


def get_trade_hvp(ticker: str, side: str) -> TradeHVP | None:
    """Return the live TradeHVP for (ticker, side) or None.

    The position-fill site (broker.orders) calls
    ``ensure_trade_hvp(ticker, side, initial_adx_5m)`` to install one;
    sentinel ticks read it via this getter to feed Alarm D.
    """
    key = (str(ticker).upper(), str(side).upper())
    return _trade_hvp_per_position.get(key)


def ensure_trade_hvp(ticker: str, side: str, initial_adx_5m: float | None) -> TradeHVP:
    """Install / re-seed the TradeHVP for (ticker, side).

    Called at Strike fill time. ``initial_adx_5m`` may be None when
    warmup is incomplete; in that case we seed the peak with 0.0 and
    the safety-floor branch in check_alarm_d keeps the alarm dormant
    until a real reading arrives.
    """
    key = (str(ticker).upper(), str(side).upper())
    hvp = _trade_hvp_per_position.get(key)
    if hvp is None:
        hvp = TradeHVP()
        _trade_hvp_per_position[key] = hvp
    seed = float(initial_adx_5m) if initial_adx_5m is not None else 0.0
    hvp.on_strike_open(seed)
    return hvp


def clear_trade_hvp(ticker: str, side: str) -> None:
    """Drop the TradeHVP for (ticker, side) on position close."""
    key = (str(ticker).upper(), str(side).upper())
    _trade_hvp_per_position.pop(key, None)
    _adx_window_per_position.pop(key, None)


def reset_session_state() -> None:
    """Clear all sentinel-loop momentum state at session boundary.

    Called from trade_genius._v570_reset_if_new_session at 09:30 ET
    alongside _v570_strike_counts.clear(). DivergenceMemory's stored
    peaks are wiped via session_reset(); per-position ADX windows and
    TradeHVPs are dropped wholesale (positions don't survive EOD).
    """
    _adx_window_per_position.clear()
    _trade_hvp_per_position.clear()
    _divergence_memory.session_reset()


# v5.15.0 PR-4 \u2014 Titan Grip Harvest deleted in vAA-1; Velocity Ratchet
# replaces it (engine.sentinel.check_alarm_c \u2192 engine.velocity_ratchet).
# The position no longer carries a TitanGripState sidecar; Alarm C, when
# wired in a follow-up PR, will read an ADXTrendWindow from
# engine.momentum_state instead.

# v5.13.6 \u2014 best-effort import of lifecycle logger.
try:
    import lifecycle_logger as _lifecycle  # noqa: F401
except Exception:  # pragma: no cover
    _lifecycle = None


def _lifecycle_logger():
    if _lifecycle is None:
        return None
    try:
        tg = _tg()
        ver = getattr(tg, "BOT_VERSION", "") if tg else ""
        return _lifecycle.get_default_logger(bot_version=ver)
    except Exception:
        return None


def _lifecycle_log_phase4_change(ticker, side, pos, result, current_price):
    """Emit PHASE4_SENTINEL / TITAN_GRIP_STAGE events when state changes
    vs the prior tick. Best-effort: any exception swallowed.
    """
    try:
        ll = _lifecycle_logger()
        if ll is None:
            return
        position_id = pos.get("lifecycle_position_id")
        if not position_id:
            return
        side_lbl = "LONG" if side == _SENTINEL_SIDE_LONG else "SHORT"
        # Sentinel state summary - the codes that fired this tick.
        codes = list(getattr(result, "alarm_codes", None) or [])
        prior = pos.get("_lifecycle_prev_alarm_codes")
        if prior != codes:
            pos["_lifecycle_prev_alarm_codes"] = list(codes)
            ll.log_event(
                position_id,
                "PHASE4_SENTINEL",
                {
                    "alarm_codes": codes,
                    "fired": bool(getattr(result, "fired", False)),
                    "exit_reason": getattr(result, "exit_reason", None),
                    "current_price": float(current_price),
                    "state": ",".join(codes) if codes else "OK",
                },
                ticker=ticker,
                side=side_lbl,
                entry_ts_utc=pos.get("entry_ts_utc"),
                reason_text=(f"sentinel {','.join(codes)}" if codes else "sentinel ok"),
            )
    except Exception as e:
        try:
            logger.debug("[lifecycle] phase4 change %s: %s", ticker, e)
        except Exception:
            pass


# v5.11.2 \u2014 prod runs `python trade_genius.py`, so trade_genius is
# registered in sys.modules as `__main__`, NOT as `trade_genius`.
# Mirror the alias trick used by paper_state / telegram_ui to make
# both names point at the same already-loaded module object.
if "trade_genius" not in _sys.modules and "__main__" in _sys.modules:
    _main = _sys.modules["__main__"]
    if getattr(_main, "BOT_NAME", None) == "TradeGenius":
        _sys.modules["trade_genius"] = _main


def _tg():
    """Live trade_genius module (handles __main__ vs imported cases)."""
    return _sys.modules.get("trade_genius") or _sys.modules.get("__main__")


logger = logging.getLogger(__name__)


def _v644_position_hold_seconds(pos) -> float | None:
    """Seconds since position entry, harness-clock aware.

    Prefers ``pos["v644_entry_now_et_iso"]`` (set in broker/orders.py at
    fill time using ``tg._now_et()``) so backtests on a monkey-patched
    BacktestClock return deterministic values. Falls back to
    ``pos["entry_ts_utc"]`` when the v6.4.4 field is absent (old
    positions hydrated from a pre-v6.4.4 paper-state snapshot, or any
    code path that creates a position without going through the
    standard fill site). In prod the two fields are within microseconds
    of each other; in backtest only the v6.4.4 field is harness-aware.

    Returns None when no entry timestamp is available, parsing fails,
    or the resulting delta is negative (clock skew / wallclock-vs-
    simulated mismatch). Callers must treat None as "do not gate" so a
    clock outage cannot silently disable a real protective stop.
    """
    from datetime import datetime as _dt

    def _parse(s):
        if not s:
            return None
        s = str(s)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            return _dt.fromisoformat(s)
        except Exception:
            return None

    entry_dt = _parse(pos.get("v644_entry_now_et_iso")) or _parse(pos.get("entry_ts_utc"))
    if entry_dt is None:
        return None
    try:
        now_dt = _tg()._now_et()
    except Exception:
        return None
    try:
        delta = (now_dt - entry_dt).total_seconds()
    except Exception:
        return None
    return float(delta) if delta >= 0 else None


def _run_sentinel(ticker, side, pos, current_price, bars):
    """v5.13.0 PR 2-3 / v5.15.0 PR-4 \u2014 evaluate Tiger Sovereign Sentinel Loop.

    Runs Alarm A (-$500 / -1%/min), Alarm B (5m close vs 9-EMA), AND
    Alarm C (Velocity Ratchet) INDEPENDENTLY \u2014 not short-circuited.
    Per the spec: "These Alarms are NOT a sequence."

    Priority on multi-fire (returned exit reason):
      A wins over B and C \u2014 -$500 / velocity is an emergency stop.
      B wins over C \u2014 9-EMA shield is a full close.
      A and B both fired: A's reason wins; both appear in the log.
    The log line lists every fired alarm regardless.

    Returns the sentinel EXIT reason string if any FULL-EXIT alarm
    fires (A or B), else None. Alarm C never returns an exit reason
    \u2014 it tightens the protective stop in place; the runner exits
    through the existing manage_positions stop-cross branch when
    the new stop is hit.

    Side: ``"LONG"`` or ``"SHORT"`` matching the sentinel SIDE_*
    constants.
    """
    try:
        entry_p = pos.get("entry_price")
        shares = int(pos.get("shares") or 0)
        if not entry_p or shares <= 0:
            return None
        if side == _SENTINEL_SIDE_LONG:
            unrealized = (current_price - entry_p) * shares
        else:
            unrealized = (entry_p - current_price) * shares
        position_value = float(entry_p) * shares

        # v5.31.0 \u2014 maintain per-position MAE / MFE trackers. The
        # exit-record forensic writer reads these on close to compute
        # max-adverse and max-favorable excursion in bps. Tracked in raw
        # price space so a backtest can also recompute against entry_price.
        try:
            _cur_min = pos.get("v531_min_adverse_price")
            _cur_max = pos.get("v531_max_favorable_price")
            if side == _SENTINEL_SIDE_LONG:
                if _cur_min is None or current_price < _cur_min:
                    pos["v531_min_adverse_price"] = float(current_price)
                if _cur_max is None or current_price > _cur_max:
                    pos["v531_max_favorable_price"] = float(current_price)
            else:
                if _cur_min is None or current_price > _cur_min:
                    pos["v531_min_adverse_price"] = float(current_price)
                if _cur_max is None or current_price < _cur_max:
                    pos["v531_max_favorable_price"] = float(current_price)
        except Exception:
            pass

        history = pos.get("pnl_history")
        if history is None:
            history = new_pnl_history()
            pos["pnl_history"] = history
        # v6.3.2 \u2014 derive now_ts from the harness clock instead of
        # _time.time(). In backtests, _now_et is monkey-patched to the
        # BacktestClock, so its timestamp drives Alarm A's 1m velocity
        # tracker deterministically. In prod, _now_et() and time.time()
        # are within microseconds of each other (verified). Falls back
        # to wallclock if the clock accessor ever raises.
        try:
            now_ts = _tg()._now_et().timestamp()
        except Exception:
            now_ts = _time.time()

        # v5.13.2 P1 #4 \u2014 Alarm A velocity baseline reset on Entry-2 fill.
        # When share count changes (Entry-2 fills, partial harvests), the
        # cached pnl_history holds samples computed against pre-change
        # notional. Computing velocity against new notional produces an
        # artificial spike. Detect the change and rebuild baseline.
        maybe_reset_pnl_baseline_on_shares_change(
            pos,
            history,
            now_ts,
            unrealized,
        )
        record_pnl(history, now_ts, unrealized)

        last_5m_close = None
        last_5m_ema9 = None
        # v5.27.0 \u2014 prev (bucket -2) close + EMA9 to feed Alarm B
        # 2-bar confirmation. None when the EMA9 history hasn't seeded
        # at the prior bucket yet; check_alarm_b silently sits out in
        # that case (insufficient history).
        prev_5m_close: float | None = None
        prev_5m_ema9: float | None = None
        try:
            # v6.0.0 \u2014 pass PDC so the EMA9 has a synthetic seed
            # when fewer than 9 closed 5m bars exist (e.g. positions
            # opened in the first 45 min of the session).
            five = compute_5m_ohlc_and_ema9(bars, pdc=bars.get("pdc"))
            if five and five.get("seeded"):
                closes_5m = five.get("closes") or []
                if closes_5m:
                    last_5m_close = closes_5m[-1]
                last_5m_ema9 = five.get("ema9")
                ema9_series = five.get("ema9_series") or []
                if len(closes_5m) >= 2 and len(ema9_series) >= 2 and ema9_series[-2] is not None:
                    prev_5m_close = closes_5m[-2]
                    prev_5m_ema9 = ema9_series[-2]
        except Exception:
            last_5m_close = None
            last_5m_ema9 = None
            prev_5m_close = None
            prev_5m_ema9 = None

        # v5.15.1 vAA-1 \u2014 wire ADXTrendWindow / TradeHVP /
        # DivergenceMemory live into the sentinel evaluator. Each
        # branch silently degrades if the underlying ADX/RSI helper
        # is missing or warmup is incomplete (returns None), so the
        # call site never raises and partial-warmup positions match
        # the v5.15.0 dormant behaviour.
        tg = _tg()
        side_key = (str(ticker).upper(), str(side).upper())

        # ADX (1m, 5m). Either may be None during warmup.
        adx_1m = None
        adx_5m = None
        try:
            if tg is not None and hasattr(tg, "v5_adx_1m_5m"):
                adx_streams = tg.v5_adx_1m_5m(ticker)
                adx_1m = adx_streams.get("adx_1m")
                adx_5m = adx_streams.get("adx_5m")
        except Exception as _e:
            logger.debug("[SENTINEL] ADX compute failed %s: %s", ticker, _e)

        # ADXTrendWindow \u2014 Alarm C. Push only when we have a fresh
        # 1m reading; otherwise keep the existing buffer state.
        adx_window = _adx_window_per_position.get(side_key)
        if adx_window is None:
            adx_window = ADXTrendWindow()
            _adx_window_per_position[side_key] = adx_window
        if adx_1m is not None:
            adx_window.push(float(adx_1m))

        # TradeHVP \u2014 Alarm D. Installed at Strike fill via
        # broker.orders.ensure_trade_hvp; we just look it up and
        # forward the live 5m ADX. If the position predates the
        # fill-hook (mid-session restart, paper replay), the lookup
        # may return None and Alarm D silently sits out.
        trade_hvp = _trade_hvp_per_position.get(side_key)
        if trade_hvp is not None and adx_5m is not None:
            try:
                trade_hvp.update(float(adx_5m))
            except Exception as _e:
                logger.debug("[SENTINEL] HVP update failed %s: %s", ticker, _e)

        # RSI(15) on 1m closes \u2014 Alarm E. The existing _compute_rsi
        # helper accepts an explicit period kwarg; pass 15 so the
        # divergence detector reads on the spec timeframe.
        rsi_15 = None
        try:
            if tg is not None and hasattr(tg, "_compute_rsi"):
                closes_1m = (bars or {}).get("closes") or []
                if closes_1m:
                    rsi_15 = tg._compute_rsi(closes_1m, period=15)
        except Exception as _e:
            logger.debug("[SENTINEL] RSI(15) compute failed %s: %s", ticker, _e)

        # DivergenceMemory \u2014 update the stored peak BEFORE the
        # sentinel evaluation so the in-trade ratchet (Alarm E POST)
        # reads the same epoch's view of the world. update() is
        # max-monotone and direction-aware, so duplicate ticks are
        # safe.
        if rsi_15 is not None:
            try:
                _divergence_memory.update(
                    ticker=ticker,
                    side=side,
                    price=float(current_price),
                    rsi=float(rsi_15),
                )
            except Exception as _e:
                logger.debug("[SENTINEL] divergence update failed %s: %s", ticker, _e)

        # v5.27.0 \u2014 portfolio-scaled Alarm A. Mirrors the
        # ``_check_daily_loss_limit`` computation: paper_cash + open
        # long market value \u2212 open short liability. Quote lookups
        # are best-effort; on failure the value falls back to None
        # and ``evaluate_sentinel`` uses the spec-default -$500.
        portfolio_value = None
        try:
            if tg is not None and hasattr(tg, "paper_cash"):
                pv = float(tg.paper_cash)
                long_pos = getattr(tg, "positions", {}) or {}
                short_pos = getattr(tg, "short_positions", {}) or {}
                _get_q = getattr(tg, "get_fmp_quote", None)
                for _pt, _pp in long_pos.items():
                    _qq = (_get_q(_pt) if _get_q else None) or {}
                    _px = float(_qq.get("price") or 0.0) or float(_pp.get("entry_price") or 0.0)
                    pv += _px * float(_pp.get("shares") or 0)
                for _pt, _pp in short_pos.items():
                    _qq = (_get_q(_pt) if _get_q else None) or {}
                    _px = float(_qq.get("price") or 0.0) or float(_pp.get("entry_price") or 0.0)
                    pv -= _px * float(_pp.get("shares") or 0)
                portfolio_value = pv if pv > 0 else None
        except Exception:
            portfolio_value = None

        # v5.28.0 \u2014 Alarm F state + ATR(14) on 1m closes. State is
        # lazily attached to the position dict so existing positions
        # carried across a process restart pick it up on their next
        # sentinel tick. ATR may be None during the first 14 bars; the
        # alarm silently waits before arming Stage 2/3.
        trail_state = pos.get("trail_state")
        if trail_state is None:
            trail_state = TrailState.fresh()
            pos["trail_state"] = trail_state
        last_1m_close = None
        last_1m_atr = None
        try:
            highs_1m_raw = (bars or {}).get("highs") or []
            lows_1m_raw = (bars or {}).get("lows") or []
            closes_1m_raw = (bars or {}).get("closes") or []
            # v6.0.5 \u2014 Yahoo's 1m series trails a None for the still-
            # forming current minute (sometimes for empty premarket bars
            # too). Naive ``closes[-1]`` then yields None and float(None)
            # raises, killing Alarm F's last_1m_close every cycle. Walk
            # backward to the most recent finite close, and align ATR on
            # the matching i-prefix where ALL three series are finite.
            n_close = len(closes_1m_raw)
            for _i in range(n_close - 1, -1, -1):
                _c = closes_1m_raw[_i]
                if _c is not None:
                    try:
                        last_1m_close = float(_c)
                    except (TypeError, ValueError):
                        last_1m_close = None
                    break
            # Build aligned finite-only H/L/C lists for ATR. Only keep
            # bars where high, low, AND close are all non-None / finite.
            highs_1m: list[float] = []
            lows_1m: list[float] = []
            closes_1m: list[float] = []
            n_align = min(len(highs_1m_raw), len(lows_1m_raw), n_close)
            for _i in range(n_align):
                _h = highs_1m_raw[_i]
                _l = lows_1m_raw[_i]
                _c = closes_1m_raw[_i]
                if _h is None or _l is None or _c is None:
                    continue
                try:
                    highs_1m.append(float(_h))
                    lows_1m.append(float(_l))
                    closes_1m.append(float(_c))
                except (TypeError, ValueError):
                    # Drop any non-numeric stragglers to keep ATR clean.
                    if highs_1m and len(highs_1m) > len(closes_1m):
                        highs_1m.pop()
                    if lows_1m and len(lows_1m) > len(closes_1m):
                        lows_1m.pop()
                    continue
            if highs_1m and lows_1m and closes_1m:
                last_1m_atr = atr_from_bars(highs_1m, lows_1m, closes_1m, period=14)
        except Exception:
            last_1m_close = None
            last_1m_atr = None

        # v6.3.1 \u2014 wire position_id + now_et into the sentinel.
        # Without these, the v6.1.0 stateful EMA-cross path and the v6.3.0
        # noise-cross filter inside it (engine/sentinel.py:697 gates on
        # ``position_id is not None``) silently fall through to the legacy
        # 2-bar confirm path, leaving both features as dead code in prod
        # and backtest. The lifecycle position_id is already resolved at
        # broker/positions.py:157 for PHASE4 logging; reuse it here.
        # v6.3.2 \u2014 fix v6.3.1 typo: was _tg().now_et() (no such
        # attribute), always raised AttributeError so _v631_now_et was
        # always None. The v6.1.0 lunch-chop suppression branch in
        # engine/sentinel.py:718 silently no-ops when now_et is None,
        # so that feature was still dead code under v6.3.1. Use the
        # canonical _now_et() accessor so the harness override flows
        # through in backtests and the lunch-chop branch activates.
        _v631_position_id = pos.get("lifecycle_position_id")
        try:
            _v631_now_et = _tg()._now_et()
        except Exception:
            _v631_now_et = None

        result = evaluate_sentinel(
            side=side,
            unrealized_pnl=unrealized,
            position_value=position_value,
            pnl_history=history,
            now_ts=now_ts,
            last_5m_close=last_5m_close,
            last_5m_ema9=last_5m_ema9,
            prev_5m_close=prev_5m_close,
            prev_5m_ema9=prev_5m_ema9,
            alarm_b_confirm_bars=ALARM_B_CONFIRM_BARS,
            portfolio_value=portfolio_value,
            adx_window=adx_window,
            current_price=current_price,
            current_shares=shares,
            trade_hvp=trade_hvp,
            current_adx_5m=adx_5m,
            current_stop_price=pos.get("stop"),
            divergence_memory=_divergence_memory,
            current_rsi_15=rsi_15,
            ticker=ticker,
            trail_state=trail_state,
            entry_price=entry_p,
            last_1m_close=last_1m_close,
            last_1m_atr=last_1m_atr,
            initial_stop_price=pos.get("initial_stop"),
            position_id=_v631_position_id,
            now_et=_v631_now_et,
        )
        # v5.13.6 \u2014 emit lifecycle PHASE4 events on state changes
        # (best-effort, no-op when logger absent).
        _lifecycle_log_phase4_change(ticker, side, pos, result, current_price)
        # v5.31.0 \u2014 record sentinel arm/trip events for chart overlay.
        # Append to trade_genius._sentinel_arm_events on either:
        #   * result.fired (any alarm tripped this tick)
        #   * armed-code set changed vs previous tick (state transition)
        # Bounded to ~500 entries to avoid unbounded growth.
        try:
            _codes = sorted({a.alarm for a in (result.alarms or []) if a and a.alarm})
            _prior = pos.get("v531_prior_alarm_codes") or []
            if result.fired or _codes != list(_prior):
                _tg_mod = _sys.modules.get("trade_genius") or _sys.modules.get("__main__")
                if _tg_mod is not None:
                    _ev_list = getattr(_tg_mod, "_sentinel_arm_events", None)
                    if isinstance(_ev_list, list):
                        _ts_iso_fn = getattr(_tg_mod, "_utc_now_iso", None)
                        _ts_iso = _ts_iso_fn() if callable(_ts_iso_fn) else None
                        _ev_list.append(
                            {
                                "ticker": ticker,
                                "side": side,
                                "ts_utc": _ts_iso,
                                "codes": _codes,
                                "price": float(current_price)
                                if current_price is not None
                                else None,
                                "fired": bool(result.fired),
                            }
                        )
                        if len(_ev_list) > 500:
                            del _ev_list[0 : len(_ev_list) - 500]
            pos["v531_prior_alarm_codes"] = _codes
        except Exception:
            pass
        if not result.fired:
            return None
        # Always log every fired alarm \u2014 multi-fire trips include
        # all A/B/C codes for observability.
        logger.warning(
            "%s",
            format_sentinel_log(ticker, pos.get("position_id"), result),
        )

        # Priority: if A or B fired, full exit overrides any C
        # stop-tighten on the same tick (don't ratchet before closing).
        if result.has_full_exit:
            # v6.4.4 \u2014 min-hold gate. Block the 50 bp Alarm-A protective
            # stop (EXIT_REASON_PRICE_STOP) under 10 minutes from entry.
            # Devi 84day_2026_sip: 266/269 under-10min pairs exit on this
            # alarm for -$6,649. Deeper rails (R-2 -$500, daily circuit
            # -$1,500, Alarm-A flash >1%/min, Alarm-B EMA, Alarm-D HVP,
            # Alarm-F chandelier) emit different exit reasons and still
            # fire normally, so the suppression is targeted.
            if (
                getattr(_sentinel_mod, "_V644_MIN_HOLD_GATE_ENABLED", True)
                and result.exit_reason == EXIT_REASON_PRICE_STOP
            ):
                hold_seconds = _v644_position_hold_seconds(pos)
                min_hold = int(getattr(_sentinel_mod, "_V644_MIN_HOLD_SECONDS", 600))
                if hold_seconds is not None and hold_seconds < min_hold:
                    logger.info(
                        "[V644-MIN-HOLD] %s %s blocked PRICE_STOP "
                        "hold=%ds<%ds; deeper rails still armed",
                        ticker,
                        side,
                        int(hold_seconds),
                        min_hold,
                    )
                    return None
            return result.exit_reason

        # Alarm C / Alarm F stop-tighten path \u2014 merge by side-aware
        # best (long: max, short: min) so the broker stop is the
        # tightest of the two reactive trails. Each alarm logs its
        # own line for observability.
        for action in result.alarms:
            if action.detail_stop_price is None:
                continue
            if action.alarm not in ("C", "F"):
                continue
            new_stop = float(action.detail_stop_price)
            old_stop = pos.get("stop") or 0.0
            if side == _SENTINEL_SIDE_LONG:
                if new_stop > old_stop:
                    pos["stop"] = new_stop
                    if action.alarm == "C":
                        logger.info(
                            "[VELOCITY-RATCHET] %s LONG stop %.4f -> %.4f",
                            ticker,
                            old_stop,
                            new_stop,
                        )
                    else:
                        logger.info(
                            "[ALARM-F-TRAIL] %s LONG stage=%d stop %.4f -> %.4f",
                            ticker,
                            getattr(trail_state, "stage", 0),
                            old_stop,
                            new_stop,
                        )
            else:
                if old_stop == 0.0 or new_stop < old_stop:
                    pos["stop"] = new_stop
                    if action.alarm == "C":
                        logger.info(
                            "[VELOCITY-RATCHET] %s SHORT stop %.4f -> %.4f",
                            ticker,
                            old_stop,
                            new_stop,
                        )
                    else:
                        logger.info(
                            "[ALARM-F-TRAIL] %s SHORT stage=%d stop %.4f -> %.4f",
                            ticker,
                            getattr(trail_state, "stage", 0),
                            old_stop,
                            new_stop,
                        )
        return None
    except Exception as e:
        # v6.0.4 \u2014 keep the per-tick warning for trail-style debugging,
        # but escalate the FIRST occurrence of each (ticker, side, exc_type)
        # to CRITICAL with a stack trace. A swallowed AttributeError used
        # to silently disable Alarms A/B/C/F for the rest of the process
        # (see paper_state.py header comment for the v6.0.4 root cause).
        logger.warning("[SENTINEL] error ticker=%s side=%s: %s", ticker, side, e)
        _key = (ticker, side, type(e).__name__)
        if _key not in _sentinel_critical_seen:
            _sentinel_critical_seen.add(_key)
            logger.critical(
                "[SENTINEL][CRITICAL] first %s on ticker=%s side=%s \u2014 "
                "sentinel evaluation aborted, no Alarms A/B/C/F will fire "
                "for this position until the underlying error is fixed: %s",
                type(e).__name__,
                ticker,
                side,
                e,
                exc_info=True,
            )
        return None


def _v5104_maybe_fire_entry_2(ticker, side, pos):
    """Per-tick Entry 2 evaluator. Mutates ``pos`` in place on fire.
    Always returns ``None``; check_breakout discards the return value.
    """
    tg = _tg()
    if pos.get("v5104_entry2_fired"):
        return
    cfg = tg.CONFIGS[side]
    side_label = "LONG" if cfg.side.is_long else "SHORT"

    bars = tg.fetch_1min_bars(ticker)
    if not bars:
        return
    current_price = bars.get("current_price")
    if not current_price or current_price <= 0:
        return
    fmp_q = tg.get_fmp_quote(ticker)
    if fmp_q:
        fmp_price = fmp_q.get("price")
        if fmp_price and fmp_price > 0:
            current_price = fmp_price

    # Track the running HWM (long) / LWM (short) for Entry 1 since
    # last fill; needed for the "fresh NHOD/NLOD past Entry 1" check.
    e1_hwm = pos.get("v5104_entry1_hwm")
    if e1_hwm is None:
        e1_hwm = pos.get("v5104_entry1_price", current_price)
    if cfg.side.is_long:
        if current_price > e1_hwm:
            pos["v5104_entry1_hwm"] = float(current_price)
            fresh_extreme = True
        else:
            fresh_extreme = False
    else:
        if current_price < e1_hwm:
            pos["v5104_entry1_hwm"] = float(current_price)
            fresh_extreme = True
        else:
            fresh_extreme = False

    # Re-evaluate Section I fresh at trigger time (spec XIV.3).
    qqq_bars = tg.fetch_1min_bars("QQQ")
    if not qqq_bars:
        return
    qqq_last = qqq_bars.get("current_price")
    qqq_avwap = tg._opening_avwap("QQQ")
    qqq_5m_close = tg._QQQ_REGIME.last_close
    qqq_ema9 = tg._QQQ_REGIME.ema9
    permit = tg.eot_glue.evaluate_section_i(
        side_label,
        qqq_5m_close,
        qqq_ema9,
        qqq_last,
        qqq_avwap,
    )
    permit_open = bool(permit.get("open"))

    # 1m DI for the appropriate polarity.
    di_streams = tg.v5_di_1m_5m(ticker)
    di_1m_now = di_streams.get("di_plus_1m") if cfg.side.is_long else di_streams.get("di_minus_1m")

    decision = tg.eot_glue.evaluate_entry_2_decision(
        ticker,
        side_label,
        entry_1_active=True,
        permit_open_at_trigger=permit_open,
        di_1m_now=di_1m_now,
        fresh_nhod_or_nlod=fresh_extreme,
        entry_2_already_fired=False,
    )
    if not decision.get("fire"):
        return

    # v5.15.1 vAA-1 \u2014 SENT-E-PRE: block Strike 2/3 entries when the
    # current tick prints a divergence vs the stored peak. Strike 1
    # (Entry-1) is never blocked because the memory has no peak yet.
    # We compute current RSI(15) on 1m closes and consult
    # check_alarm_e_pre against the singleton DivergenceMemory.
    try:
        from engine.sentinel import check_alarm_e_pre as _check_alarm_e_pre

        _strike_num = int(pos.get("strike_num") or 1) + 1  # Entry-2 \u2192 Strike 2
        _closes_1m = (bars or {}).get("closes") or []
        _rsi15 = (
            tg._compute_rsi(_closes_1m, period=15)
            if (_closes_1m and hasattr(tg, "_compute_rsi"))
            else None
        )
        if _rsi15 is not None:
            _blocked = _check_alarm_e_pre(
                memory=_divergence_memory,
                ticker=ticker,
                side=side_label,
                current_price=float(current_price),
                current_rsi_15=float(_rsi15),
                strike_num=_strike_num,
            )
            if _blocked:
                logger.info(
                    "[SENT-E-PRE] %s strike_num=%d %s blocked: divergence vs stored peak "
                    "price=%.4f rsi15=%.2f",
                    ticker,
                    _strike_num,
                    side_label,
                    float(current_price),
                    float(_rsi15),
                )
                return
    except Exception as _e:
        logger.debug("[SENT-E-PRE] %s eval skipped: %s", ticker, _e)

    # Entry-1 ts must precede now (spec III.2).
    e1_ts = pos.get("v5104_entry1_ts_utc")
    now_iso = tg._utc_now_iso()
    if e1_ts and e1_ts >= now_iso:
        return

    # v5.13.7 \u2014 N1: spec L-P3-S6 / S-P3-S6 mandates a 50/50 split by
    # SHARE COUNT, not by dollar notional. Pre-v5.13.7 we computed
    # target_full = floor(PAPER_DOLLARS_PER_ENTRY / current_price) and
    # then E2 = target_full - e1_shares; that produced an asymmetric
    # share split whenever the price drifted between Entry-1 fill and
    # Entry-2 trigger. The spec says "BUY remaining 50%" of a 50/50
    # split, which means E2 == E1 in the typical full-fill case.
    # Defensive fallback: if e1_shares is missing/zero (Entry-1 didn't
    # actually fire \u2014 shouldn't happen), preserve the old dollar-parity
    # behavior so we never silently size to 1 share.
    from eye_of_tiger import ENTRY_1_SIZE_PCT, ENTRY_2_SIZE_PCT  # noqa: F401

    e1_shares = int(pos.get("v5104_entry1_shares") or pos.get("shares") or 0)
    # ENTRY_2_SIZE_PCT participates in the sanity check: full = E1 + E2,
    # so E1+E2 \u2248 1.0. If somebody changes the constants in eye_of_tiger
    # the assertion below catches it before we ship a non-spec sizing.
    assert abs((ENTRY_1_SIZE_PCT + ENTRY_2_SIZE_PCT) - 1.0) < 1e-6, (
        "ENTRY_1_SIZE_PCT + ENTRY_2_SIZE_PCT must sum to 1.0"
    )
    if e1_shares > 0:
        e2_shares = e1_shares
    else:
        target_full = max(1, int(tg.PAPER_DOLLARS_PER_ENTRY // float(current_price)))
        e2_shares = max(1, target_full)
    if e2_shares <= 0:
        return

    # Paper cash: long debits, short credits.
    notional = float(current_price) * e2_shares
    if cfg.side.is_long and notional > tg.paper_cash:
        logger.info(
            "[V5100-ENTRY] %s skip entry_2 \u2014 insufficient cash (need $%.2f, have $%.2f)",
            ticker,
            notional,
            tg.paper_cash,
        )
        return
    tg.paper_cash += cfg.entry_cash_delta(e2_shares, current_price)

    # Average down/up the entry price; grow share count.
    e1_price = float(pos.get("v5104_entry1_price") or pos.get("entry_price"))
    total_shares = e1_shares + e2_shares
    new_avg = (e1_price * e1_shares + float(current_price) * e2_shares) / total_shares
    pos["entry_price"] = new_avg
    pos["shares"] = total_shares
    pos["v5104_entry2_price"] = float(current_price)
    pos["v5104_entry2_shares"] = int(e2_shares)
    pos["v5104_entry2_ts_utc"] = now_iso
    pos["v5104_entry2_fired"] = True

    try:
        logger.info(
            "[V5100-ENTRY] ticker=%s side=%s entry_num=2 di_1m=%s "
            "fresh_extreme=%s fill_price=%.4f shares=%d new_avg=%.4f",
            ticker,
            side_label,
            ("%.2f" % di_1m_now) if di_1m_now is not None else "None",
            fresh_extreme,
            float(current_price),
            e2_shares,
            new_avg,
        )
    except Exception:
        pass

    try:
        tg.save_paper_state()
    except Exception:
        pass


# ============================================================
# MANAGE POSITIONS (stop + trail logic)
# ============================================================
def manage_positions():
    """Check stops and update trailing stops for all open positions."""
    tg = _tg()
    positions = tg.positions
    tickers_to_close = []

    # v5.26.0 \u2014 retighten_all_stops removed. The R-2 hard stop is set
    # at entry and is fixed for the life of the position; periodic
    # cap-retightening is non-spec.

    for ticker in list(positions.keys()):
        bars = tg.fetch_1min_bars(ticker)
        if not bars:
            continue

        current_price = bars["current_price"]
        pos = positions[ticker]

        # v5.13.0 PR 2 \u2014 Tiger Sovereign Sentinel Loop (parallel
        # alarms A & B & C). Spec-literal: A_LOSS=-$500 hard floor,
        # A2=-1% over 60s, B=closed 5m close < 9-EMA, C=Titan Grip
        # Harvest. Alarms are evaluated INDEPENDENTLY (not
        # short-circuited). Sole exit decision-maker as of v5.13.10
        # \u2014 the legacy phase-machine / ladder / RED_CANDLE path
        # was removed when LEGACY_EXITS_ENABLED retired.
        _sentinel_reason = _run_sentinel(
            ticker,
            _SENTINEL_SIDE_LONG,
            pos,
            current_price,
            bars,
        )
        if _sentinel_reason is not None:
            tickers_to_close.append((ticker, current_price, _sentinel_reason))
            continue

    # Close positions outside the loop to avoid mutation during iteration
    for ticker, price, reason in tickers_to_close:
        tg.close_position(ticker, price, reason)


# ============================================================
# MANAGE SHORT POSITIONS (stop + trail logic)
# ============================================================
def manage_short_positions():
    """Check stops and trailing stops for all open short positions."""
    tg = _tg()
    short_positions = tg.short_positions

    # v5.26.0 \u2014 retighten_all_stops removed (mirrors manage_positions).

    # v5.9.1: Sovereign Regime Shield (PDC eject) retired on the short
    # side too. v5.13.10: per-ticker POLARITY_SHIFT exit also retired
    # along with the rest of the legacy phase-machine path.

    for ticker in list(short_positions.keys()):
        pos = short_positions[ticker]

        bars = tg.fetch_1min_bars(ticker)
        if not bars:
            continue
        current_price = bars["current_price"]

        # v5.13.0 PR 2 \u2014 Tiger Sovereign Sentinel Loop (short side
        # mirror). Alarm A: -$500 / -1%/min. Alarm B: 5m close ABOVE
        # 9-EMA fires. Alarms run in parallel; sole exit path as of
        # v5.13.10.
        _sentinel_reason_s = _run_sentinel(
            ticker,
            _SENTINEL_SIDE_SHORT,
            pos,
            current_price,
            bars,
        )
        if _sentinel_reason_s is not None:
            tg.close_short_position(ticker, current_price, reason=_sentinel_reason_s)
            continue
