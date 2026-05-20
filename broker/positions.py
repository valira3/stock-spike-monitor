"""broker.positions -- per-tick position management.

Extracted from trade_genius.py in v5.11.2 PR 3. The legacy Tiger Sentinel
A/B/C `_run_sentinel` chain + Entry-2 (v5104) scale-in were deleted in
v10.0.1; the v10 ORB runtime owns all exits and entries are FULL-sized
at Entry-1. Surface kept stable for back-compat:

    * manage_positions / manage_short_positions: per-tick v10 exit dispatch
    * reset_session_state: no-op stub (session-reset hook binding)
    * _v5104_maybe_fire_entry_2: no-op stub (Entry-2 retired)
"""

from __future__ import annotations

import logging
import sys as _sys

import time as _time

from broker.orders import check_breakout  # noqa: F401

# v7.17.0: v10 ORB exit routing
import orb.live_runtime as _orb_rt
from engine.timing import minutes_since_et_midnight as _to_et_min

# v10.0.1 -- Tiger Sentinel A/B/C chain deleted (engine.sentinel +
# engine.alarm_f_trail + engine.momentum_state + engine.velocity_ratchet).
# v10 ORB runtime owns all exits; the legacy fallback is gone.
from engine.bars import compute_5m_ohlc_and_ema9  # noqa: F401

# Side labels (formerly imported from engine.sentinel).
_SENTINEL_SIDE_LONG = "LONG"
_SENTINEL_SIDE_SHORT = "SHORT"
def reset_session_state() -> None:
    """v10.0.1 -- no-op stub. The Tiger Sentinel state caches were deleted.
    Kept so trade_genius's session-reset hook still binds to a stable name.
    """
    return None


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





def _v5104_maybe_fire_entry_2(ticker, side, pos):
    """v10.0.1 -- stub. Tiger Sovereign Entry-2 scale-in retired.
    v10 positions are FULL-sized at Entry-1; no follow-on add ever fires.
    Stable name kept so broker/orders.py + broker/__init__.py + trade_genius.py
    imports still bind. Returns None unconditionally.
    """
    return None




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

        # v7.17.0: v10 ORB exit cutover.
        # When ORB_LIVE_MODE=1 (default), the v10 runtime owns exits for
        # positions it admitted. Legacy Tiger Sentinel A/B/C is the
        # fallback for legacy-held positions (those that exist in
        # tg.positions but have no v10 ticket -- e.g. opened before
        # ORB_LIVE_MODE flipped on, or opened via the legacy fallback
        # path when ORB_LIVE_MODE=0 was temporarily set).
        _v10_handled = False
        if _orb_rt.is_live_mode_on():
            try:
                _ts_arr = bars.get("timestamps") or []
                _bucket = _to_et_min(int(_ts_arr[-1])) if _ts_arr else 600
                _highs = bars.get("highs") or []
                _lows = bars.get("lows") or []
                _bar_h = float(_highs[-1] if _highs and _highs[-1] is not None else current_price)
                _bar_l = float(_lows[-1] if _lows and _lows[-1] is not None else current_price)
                _v10_res = _orb_rt.check_exit_by_ticker(
                    portfolio_id="main", ticker=ticker,
                    bar_high=_bar_h, bar_low=_bar_l,
                    bar_close=float(current_price),
                    bar_bucket_min=_bucket,
                )
                if _v10_res.exit:
                    logger.info(
                        "[V79-ORB-EXIT] long %s reason=%s exit_price=%.4f",
                        ticker, _v10_res.reason, _v10_res.price,
                    )
                    tickers_to_close.append(
                        (ticker, _v10_res.price, f"V10_{_v10_res.reason.upper()}"))
                    _v10_handled = True
                elif getattr(_v10_res, "partial", False):
                    # v8.1.0 -- partial-profit-at-1R fire. Apply the
                    # half-close on the paper book; position stays open
                    # with the runner half. NOT appended to
                    # tickers_to_close (full-close path).
                    try:
                        from broker.orders import partial_close_breakout
                        from side import Side as _Side
                        partial_close_breakout(
                            ticker=ticker,
                            shares_to_close=int(_v10_res.partial_shares),
                            price=float(_v10_res.partial_price),
                            side=_Side.LONG,
                            reason="PARTIAL_1R",
                        )
                    except Exception as _e:
                        logger.error(
                            "[V81-ORB-PARTIAL] long %s broker apply error: %s",
                            ticker, _e,
                        )
                    _v10_handled = True
                elif _v10_res.reason != "no_open_v10_position":
                    # v10 owns this position; v10 said "stay" -> skip Sentinel
                    _v10_handled = True
            except Exception as _e:
                logger.warning("[V79-ORB-EXIT] long %s error: %s", ticker, _e)
                # Fall through to legacy on exception (defensive)
        if _v10_handled:
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

        # v7.17.0: v10 ORB exit cutover (short side mirror).
        _v10_handled_s = False
        if _orb_rt.is_live_mode_on():
            try:
                _ts_arr = bars.get("timestamps") or []
                _bucket = _to_et_min(int(_ts_arr[-1])) if _ts_arr else 600
                _highs = bars.get("highs") or []
                _lows = bars.get("lows") or []
                _bar_h = float(_highs[-1] if _highs and _highs[-1] is not None else current_price)
                _bar_l = float(_lows[-1] if _lows and _lows[-1] is not None else current_price)
                _v10_res = _orb_rt.check_exit_by_ticker(
                    portfolio_id="main", ticker=ticker,
                    bar_high=_bar_h, bar_low=_bar_l,
                    bar_close=float(current_price),
                    bar_bucket_min=_bucket,
                )
                if _v10_res.exit:
                    logger.info(
                        "[V79-ORB-EXIT] short %s reason=%s exit_price=%.4f",
                        ticker, _v10_res.reason, _v10_res.price,
                    )
                    tg.close_short_position(
                        ticker, _v10_res.price,
                        reason=f"V10_{_v10_res.reason.upper()}",
                    )
                    _v10_handled_s = True
                elif getattr(_v10_res, "partial", False):
                    # v8.1.0 -- partial-profit-at-1R fire (short side).
                    try:
                        from broker.orders import partial_close_breakout
                        from side import Side as _Side
                        partial_close_breakout(
                            ticker=ticker,
                            shares_to_close=int(_v10_res.partial_shares),
                            price=float(_v10_res.partial_price),
                            side=_Side.SHORT,
                            reason="PARTIAL_1R",
                        )
                    except Exception as _e:
                        logger.error(
                            "[V81-ORB-PARTIAL] short %s broker apply error: %s",
                            ticker, _e,
                        )
                    _v10_handled_s = True
                elif _v10_res.reason != "no_open_v10_position":
                    _v10_handled_s = True
            except Exception as _e:
                logger.warning("[V79-ORB-EXIT] short %s error: %s", ticker, _e)
        if _v10_handled_s:
            continue

