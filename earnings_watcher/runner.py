"""v6.16.1 \u2014 earnings_watcher.runner: orchestration loop.

Entry points called from trade_genius.py (behind EARNINGS_WATCHER_ENABLED flag):
  - run_premarket_cycle()   -> Dict   (04:00 ET, BMO tickers)
  - run_afterhours_cycle()  -> Dict   (16:00 ET, AMC tickers)
  - run_exit_cycle()        -> Dict   (every 60 s during active windows)

Internal helpers (also importable for tests):
  - evaluate_and_size(equity, ticker, bars, event_meta, open_dmi_exposure) -> Optional[Dict]
  - submit_dmi_order(intent, paper=True) -> Dict
  - manage_open_positions(open_intents) -> int

Hard boundary: MUST NOT import from eye_of_tiger or trade_genius.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("earnings_watcher")

# ---------------------------------------------------------------------------
# Local imports (earnings_watcher package only)
# ---------------------------------------------------------------------------
from earnings_watcher.signals import (
    DMI_MAX_ENTRY_IDX,
    find_nhod_dmi_breakout,
    filter_bars_for_session,
    determine_session,
    quality_score,
)
from earnings_watcher.sizing import (
    DMI_HARD_STOP,
    DMI_TRAIL_PCT,
    DMI_TRAIL_TRIGGER,
    DMI_TIME_STOP_MIN,
    dmi_sized_notional,
    dmi_conviction_multiplier,
)
from earnings_watcher.exits import evaluate_exit, compute_elapsed_minutes
from earnings_watcher.data_sources import (
    fetch_minute_bars,
    get_account_equity,
    get_today_earnings_universe,
    get_earnings_calendar,
)
from earnings_watcher.state import (
    load_open_positions,
    save_open_positions,
    add_position,
    remove_position,
    update_position,
)


# ---------------------------------------------------------------------------
# ET timezone helper
# ---------------------------------------------------------------------------

def _et_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # Fallback: approximate ET as UTC-4 (EDT)
        return datetime.now(timezone.utc) - timedelta(hours=4)


def _is_extended_hours(dt: datetime) -> bool:
    """True if dt is pre-market (04:00-09:29 ET) or after-hours (16:00-20:00 ET)."""
    h = dt.hour
    m = dt.minute
    total = h * 60 + m
    pre_market = (4 * 60) <= total < (9 * 60 + 30)
    after_hours = (16 * 60) <= total < (20 * 60)
    return pre_market or after_hours


# ---------------------------------------------------------------------------
# evaluate_and_size
# ---------------------------------------------------------------------------

def evaluate_and_size(
    equity: Optional[float],
    ticker: str,
    bars: List[Dict[str, Any]],
    event_meta: Dict[str, Any],
    open_dmi_exposure: float,
) -> Optional[Dict[str, Any]]:
    """Run the full DMI signal pipeline on one ticker's bars.

    Pipeline:
      1. Determine session from bars (or from event_meta['session'] if mixed)
      2. Filter bars to session window
      3. find_nhod_dmi_breakout (requires >= 25 sess bars)
      4. Cap entry index at DMI_MAX_ENTRY_IDX
      5. quality_score bias check (long requires bullish)
      6. dmi_sized_notional with open_dmi_exposure
      7. Build and return order intent dict

    Parameters
    ----------
    equity : float or None
        Current account equity. None causes sizing to return 0 (no_equity).
    ticker : str
        Ticker symbol.
    bars : list
        Raw 1-min bars as returned by fetch_minute_bars.
    event_meta : dict
        Event record from get_earnings_calendar. Must have epsActual,
        epsEstimated, revActual, revEstimated (may be None). May have 'session'.
    open_dmi_exposure : float
        Total dollar notional currently held in DMI positions.

    Returns
    -------
    Dict with keys:
      ticker, side, notional, qty, limit_price, conv, di_plus, adx, reason
    or None if no valid signal.
    """
    logger.info("[EW-RUNNER] evaluate_and_size ticker=%s bars=%d equity=%s",
                ticker, len(bars), equity)

    if not bars:
        logger.debug("[EW-RUNNER] skip ticker=%s reason=no_bars", ticker)
        return None

    # Step 1: session determination
    session = determine_session(bars)
    if session == "mixed" and event_meta.get("session") in ("bmo", "amc"):
        session = event_meta["session"]
    if session in ("unknown", "mixed"):
        logger.debug("[EW-RUNNER] skip ticker=%s reason=no_session session=%s", ticker, session)
        return None

    # Step 2: filter to session window
    sess_bars = filter_bars_for_session(bars, session)
    if len(sess_bars) < 25:
        logger.debug("[EW-RUNNER] skip ticker=%s reason=too_few_bars sess_bars=%d",
                     ticker, len(sess_bars))
        return None

    # Step 3: NHOD DMI breakout detection
    bo = find_nhod_dmi_breakout(sess_bars)
    if bo is None:
        logger.debug("[EW-RUNNER] skip ticker=%s reason=no_dmi_breakout", ticker)
        return None

    # Step 4: cap on late-session entry index
    if DMI_MAX_ENTRY_IDX is not None and bo["idx"] > DMI_MAX_ENTRY_IDX:
        logger.debug("[EW-RUNNER] skip ticker=%s reason=idx_too_late idx=%d", ticker, bo["idx"])
        return None

    direction = bo["direction"]

    # Step 5: quality score bias check
    qs = quality_score(event_meta)
    if direction == "long" and qs["bias"] != "bullish":
        logger.debug("[EW-RUNNER] skip ticker=%s reason=bias_misaligned_long bias=%s",
                     ticker, qs["bias"])
        return None
    if direction == "short" and qs["bias"] != "bearish":
        logger.debug("[EW-RUNNER] skip ticker=%s reason=bias_misaligned_short bias=%s",
                     ticker, qs["bias"])
        return None

    # Step 6: sizing
    conv = bo["conviction"]
    notional, sizing_reason = dmi_sized_notional(equity, conv, open_dmi_exposure)
    if notional <= 0:
        logger.info("[EW-RUNNER] skip ticker=%s reason=sizing reason=%s equity=%s",
                    ticker, sizing_reason, equity)
        return None

    # Step 7: build order intent
    entry_bar = sess_bars[bo["idx"]]
    limit_price = round(float(entry_bar["close"]) * 1.005, 4)  # allow 0.5% slippage
    qty = max(1, math.floor(notional / limit_price))

    intent: Dict[str, Any] = {
        "ticker": ticker,
        "side": "BUY" if direction == "long" else "SELL",
        "notional": round(notional, 2),
        "qty": qty,
        "limit_price": limit_price,
        "conv": round(conv, 4),
        "di_plus": round(bo["di_plus"], 2) if bo.get("di_plus") is not None else None,
        "adx": round(bo["adx"], 2) if bo.get("adx") is not None else None,
        "reason": sizing_reason,
        "direction": direction,
        "entry_ts": bo.get("entry_ts", ""),
        "quality_score": qs["score"],
        "bias": qs["bias"],
        "session": session,
    }
    logger.info("[EW-RUNNER] signal ticker=%s side=%s notional=%.0f qty=%d "
                "limit=%.4f conv=%.2f adx=%s sizing=%s",
                ticker, intent["side"], notional, qty, limit_price,
                conv, intent["adx"], sizing_reason)
    return intent


# ---------------------------------------------------------------------------
# submit_dmi_order
# ---------------------------------------------------------------------------

def submit_dmi_order(intent: Dict[str, Any], paper: bool = True) -> Dict[str, Any]:
    """Submit a limit order to Alpaca for an earnings_watcher DMI signal.

    Parameters
    ----------
    intent : dict
        Order intent from evaluate_and_size. Must have:
        ticker, side, qty, limit_price, notional.
    paper : bool
        If True, use Alpaca paper trading endpoint.

    Returns
    -------
    Dict with order_id, status, filled_qty.
    On error returns {order_id: '', status: 'error', filled_qty: 0}.
    """
    ticker = intent["ticker"]
    qty = intent["qty"]
    limit_price = intent["limit_price"]
    side = intent["side"]

    logger.info("[EW-ORDER] submit ticker=%s side=%s qty=%d limit=%.4f paper=%s",
                ticker, side, qty, limit_price, paper)

    if qty <= 0:
        logger.warning("[EW-ORDER] skip ticker=%s reason=zero_qty", ticker)
        return {"order_id": "", "status": "skipped_zero_qty", "filled_qty": 0}

    try:
        from alpaca.trading.client import TradingClient  # type: ignore
        from alpaca.trading.requests import LimitOrderRequest  # type: ignore
        from alpaca.trading.enums import OrderSide, TimeInForce  # type: ignore

        key = os.getenv("VAL_ALPACA_PAPER_KEY", "")
        secret = os.getenv("VAL_ALPACA_PAPER_SECRET", "")
        if not key or not secret:
            raise EnvironmentError("VAL_ALPACA_PAPER_KEY/SECRET not set")

        client = TradingClient(key, secret, paper=paper)

        now_et = _et_now()
        ext_hours = _is_extended_hours(now_et)

        order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
        req = LimitOrderRequest(
            symbol=ticker,
            qty=qty,
            side=order_side,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price,
            extended_hours=ext_hours,
        )
        order = client.submit_order(req)
        order_id = str(order.id) if hasattr(order, "id") else ""
        status = str(order.status) if hasattr(order, "status") else "submitted"
        filled_qty = int(order.filled_qty) if hasattr(order, "filled_qty") and order.filled_qty else 0
        logger.info("[EW-ORDER] submitted ticker=%s order_id=%s status=%s filled_qty=%d",
                    ticker, order_id, status, filled_qty)
        return {"order_id": order_id, "status": status, "filled_qty": filled_qty}

    except Exception as exc:
        logger.warning("[EW-ORDER-ERROR] ticker=%s error: %s", ticker, exc)
        return {"order_id": "", "status": "error", "filled_qty": 0, "error": str(exc)}


# ---------------------------------------------------------------------------
# manage_open_positions
# ---------------------------------------------------------------------------

def manage_open_positions(open_intents: Optional[Dict[str, Any]] = None) -> int:
    """Check each open earnings_watcher position for exit conditions.

    For each position:
      1. Fetch latest 1-min bar from Alpaca
      2. call evaluate_exit
      3. If should_exit: submit market sell, remove from state

    Parameters
    ----------
    open_intents : dict, optional
        Ignored (reserved for future caller overrides). Uses state file.

    Returns
    -------
    int: count of positions exited this cycle.
    """
    positions = load_open_positions()
    if not positions:
        return 0

    exits = 0
    now_utc = datetime.now(timezone.utc)

    for ticker, pos in list(positions.items()):
        entry_ts_str = pos.get("entry_ts_utc", "")
        try:
            entry_dt = datetime.fromisoformat(entry_ts_str)
        except ValueError:
            logger.warning("[EW-RUNNER] manage bad entry_ts ticker=%s ts=%s",
                           ticker, entry_ts_str)
            continue

        # Fetch a single recent bar (last 2 minutes)
        start_utc = now_utc - timedelta(minutes=3)
        bars = fetch_minute_bars(ticker, start_utc, now_utc)
        if not bars:
            logger.debug("[EW-RUNNER] manage no bars for ticker=%s", ticker)
            continue

        latest_bar = bars[-1]

        # elapsed_minutes: count of 1-min bars since entry
        elapsed = max(0, int((now_utc - entry_dt).total_seconds() / 60))

        should_exit, reason = evaluate_exit(pos, latest_bar, elapsed)
        if should_exit:
            logger.info("[EW-RUNNER] exit ticker=%s reason=%s", ticker, reason)
            _submit_exit_order(ticker, pos)
            remove_position(ticker)
            exits += 1
        else:
            # Persist updated peak/trough/trail state back to disk
            update_position(
                ticker,
                peak_pct=pos.get("peak_pct", 0.0),
                trough_pct=pos.get("trough_pct", 0.0),
                trail_active=pos.get("trail_active", False),
                trail_stop=pos.get("trail_stop", 0.0),
            )

    logger.info("[EW-RUNNER] manage_open_positions checked=%d exits=%d",
                len(positions), exits)
    return exits


def _submit_exit_order(ticker: str, pos: Dict[str, Any]) -> None:
    """Submit a market SELL (or BUY for short) to close a position."""
    qty = int(pos.get("qty", 0))
    direction = pos.get("side", "long")
    side = "SELL" if direction == "long" else "BUY"

    if qty <= 0:
        logger.warning("[EW-ORDER] exit skip ticker=%s reason=zero_qty", ticker)
        return

    try:
        from alpaca.trading.client import TradingClient  # type: ignore
        from alpaca.trading.requests import MarketOrderRequest  # type: ignore
        from alpaca.trading.enums import OrderSide, TimeInForce  # type: ignore

        key = os.getenv("VAL_ALPACA_PAPER_KEY", "")
        secret = os.getenv("VAL_ALPACA_PAPER_SECRET", "")
        if not key or not secret:
            raise EnvironmentError("VAL_ALPACA_PAPER_KEY/SECRET not set")

        client = TradingClient(key, secret, paper=True)
        now_et = _et_now()
        ext_hours = _is_extended_hours(now_et)

        order_side = OrderSide.SELL if side == "SELL" else OrderSide.BUY
        req = MarketOrderRequest(
            symbol=ticker,
            qty=qty,
            side=order_side,
            time_in_force=TimeInForce.DAY,
        )
        order = client.submit_order(req)
        order_id = str(order.id) if hasattr(order, "id") else ""
        logger.info("[EW-ORDER] exit submitted ticker=%s side=%s qty=%d order_id=%s",
                    ticker, side, qty, order_id)
    except Exception as exc:
        logger.warning("[EW-ORDER-ERROR] exit ticker=%s error: %s", ticker, exc)


# ---------------------------------------------------------------------------
# run_premarket_cycle
# ---------------------------------------------------------------------------

def run_premarket_cycle() -> Dict[str, Any]:
    """Top-level entry for the 04:00 ET pre-market BMO cycle.

    For each BMO ticker:
      1. fetch bars from 04:00 ET to now
      2. evaluate_and_size
      3. submit order, track in state

    Returns summary dict.
    """
    logger.info("[EW-RUNNER] run_premarket_cycle start")
    now_utc = datetime.now(timezone.utc)
    # 04:00 ET = approx 08:00 UTC (EDT); use a fixed offset here
    today_str = now_utc.strftime("%Y-%m-%d")
    start_utc = datetime.fromisoformat(f"{today_str}T08:00:00+00:00")

    equity = get_account_equity()
    positions = load_open_positions()
    open_exposure = sum(
        float(p.get("notional", 0)) for p in positions.values()
    )

    bmo_tickers, _ = get_today_earnings_universe(today_str)
    calendar = get_earnings_calendar(today_str)
    event_map: Dict[str, Dict[str, Any]] = {
        ev["ticker"]: ev for ev in calendar
    }

    evaluated = signals = orders_submitted = orders_filled = 0

    for ticker in bmo_tickers:
        if ticker in positions:
            logger.debug("[EW-RUNNER] premarket skip ticker=%s reason=already_open", ticker)
            continue
        evaluated += 1
        bars = fetch_minute_bars(ticker, start_utc, now_utc)
        event_meta = event_map.get(ticker, {"ticker": ticker})
        event_meta.setdefault("session", "bmo")

        intent = evaluate_and_size(equity, ticker, bars, event_meta, open_exposure)
        if intent is None:
            continue
        signals += 1

        result = submit_dmi_order(intent, paper=True)
        orders_submitted += 1
        if result.get("filled_qty", 0) > 0:
            orders_filled += 1

        if result.get("status") not in ("error", "skipped_zero_qty"):
            add_position(
                ticker,
                entry_px=intent["limit_price"],
                entry_ts_utc=now_utc.isoformat(),
                qty=intent["qty"],
                side=intent["direction"],
                notional=intent["notional"],
                conv=intent["conv"],
                order_id=result.get("order_id", ""),
            )
            open_exposure += intent["notional"]

    exits = manage_open_positions()
    summary = {
        "cycle": "premarket",
        "evaluated": evaluated,
        "signals": signals,
        "orders_submitted": orders_submitted,
        "orders_filled": orders_filled,
        "exits": exits,
    }
    logger.info("[EW-RUNNER] run_premarket_cycle done: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# run_afterhours_cycle
# ---------------------------------------------------------------------------

def run_afterhours_cycle() -> Dict[str, Any]:
    """Top-level entry for the 16:00 ET after-hours AMC cycle.

    For each AMC ticker:
      1. fetch bars from 16:00 ET to now (approx 20:00 UTC)
      2. evaluate_and_size
      3. submit order, track in state

    Returns summary dict.
    """
    logger.info("[EW-RUNNER] run_afterhours_cycle start")
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    # 16:00 ET = 20:00 UTC (EDT)
    start_utc = datetime.fromisoformat(f"{today_str}T20:00:00+00:00")

    equity = get_account_equity()
    positions = load_open_positions()
    open_exposure = sum(
        float(p.get("notional", 0)) for p in positions.values()
    )

    _, amc_tickers = get_today_earnings_universe(today_str)
    calendar = get_earnings_calendar(today_str)
    event_map: Dict[str, Dict[str, Any]] = {
        ev["ticker"]: ev for ev in calendar
    }

    evaluated = signals = orders_submitted = orders_filled = 0

    for ticker in amc_tickers:
        if ticker in positions:
            logger.debug("[EW-RUNNER] ah skip ticker=%s reason=already_open", ticker)
            continue
        evaluated += 1
        bars = fetch_minute_bars(ticker, start_utc, now_utc)
        event_meta = event_map.get(ticker, {"ticker": ticker})
        event_meta.setdefault("session", "amc")

        intent = evaluate_and_size(equity, ticker, bars, event_meta, open_exposure)
        if intent is None:
            continue
        signals += 1

        result = submit_dmi_order(intent, paper=True)
        orders_submitted += 1
        if result.get("filled_qty", 0) > 0:
            orders_filled += 1

        if result.get("status") not in ("error", "skipped_zero_qty"):
            add_position(
                ticker,
                entry_px=intent["limit_price"],
                entry_ts_utc=now_utc.isoformat(),
                qty=intent["qty"],
                side=intent["direction"],
                notional=intent["notional"],
                conv=intent["conv"],
                order_id=result.get("order_id", ""),
            )
            open_exposure += intent["notional"]

    exits = manage_open_positions()
    summary = {
        "cycle": "afterhours",
        "evaluated": evaluated,
        "signals": signals,
        "orders_submitted": orders_submitted,
        "orders_filled": orders_filled,
        "exits": exits,
    }
    logger.info("[EW-RUNNER] run_afterhours_cycle done: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# run_exit_cycle
# ---------------------------------------------------------------------------

def run_exit_cycle() -> Dict[str, Any]:
    """Called every 60 s from the scheduler. Checks all open positions for exits.

    Returns summary dict with exits count.
    """
    logger.debug("[EW-RUNNER] run_exit_cycle")
    exits = manage_open_positions()
    return {"cycle": "exit", "exits": exits}
