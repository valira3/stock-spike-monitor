"""broker.lifecycle \u2014 entry/exit dispatchers and EOD close.

Extracted from trade_genius.py in v5.11.2 PR 4.
"""
from __future__ import annotations

import logging
import sys as _sys

from broker.orders import check_breakout, execute_breakout, close_breakout
from side import Side

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


# ============================================================
# v4.9.0 \u2014 Public entry/close API \u2014 thin wrappers
# ============================================================
# check_breakout / execute_breakout / close_breakout are the canonical
# unified bodies in broker.orders. The public names below preserve the
# call sites that scan_loop, manage_positions, manage_short_positions,
# eod_close, and the dashboard server use. They forward to the unified
# bodies via Side.LONG / Side.SHORT.
def check_entry(ticker):
    return check_breakout(ticker, Side.LONG)


def check_short_entry(ticker):
    return check_breakout(ticker, Side.SHORT)


def execute_entry(ticker, current_price):
    return execute_breakout(ticker, current_price, Side.LONG)


def execute_short_entry(ticker, current_price):
    return execute_breakout(ticker, current_price, Side.SHORT)


def close_position(ticker, price, reason="STOP"):
    return close_breakout(ticker, price, Side.LONG, reason)


def close_short_position(ticker, price, reason="STOP"):
    return close_breakout(ticker, price, Side.SHORT, reason)


# ============================================================
# EOD CLOSE
# ============================================================
def eod_close():
    """Force-close all open long AND short positions at 15:59:50 ET.

    v5.10.0 Section VI: EOD flush moved from 15:55 to 15:59:50 ET.
    """
    tg = _tg()
    # v4.0.0-alpha \u2014 notify executors to flatten everything on Alpaca.
    # Per-position close events still fire from close_position /
    # close_short_position below; this event lets executors shortcut with
    # a single close_all_positions call if they prefer.
    tg._emit_signal({
        "kind": "EOD_CLOSE_ALL",
        "ticker": "",
        "price": 0.0,
        "reason": "EOD",
        "timestamp_utc": tg._utc_now_iso(),
        "main_shares": 0,
    })
    positions = tg.positions
    short_positions = tg.short_positions
    n_long = len(positions)
    n_short = len(short_positions)

    if not positions and not short_positions:
        logger.info("EOD close: no open positions (long or short)")

    if positions:
        logger.info("EOD close: closing %d long positions", n_long)
        longs_to_close = []
        for ticker in list(positions.keys()):
            bars = tg.fetch_1min_bars(ticker)
            if bars:
                price = bars["current_price"]
            else:
                price = positions[ticker]["entry_price"]
            longs_to_close.append((ticker, price))
        for ticker, price in longs_to_close:
            close_position(ticker, price, reason="EOD")

    if short_positions:
        logger.info("EOD close: closing %d short positions", n_short)
        shorts_to_close = []
        for ticker in list(short_positions.keys()):
            bars = tg.fetch_1min_bars(ticker)
            if bars:
                price = bars["current_price"]
            else:
                price = short_positions[ticker]["entry_price"]
            shorts_to_close.append((ticker, price))
        for ticker, price in shorts_to_close:
            close_short_position(ticker, price, "EOD")

    # v5.2.0 \u2014 close any orphan shadow positions (configs whose
    # would-have-entered ticker is not held live) at EOD.
    # v5.2.1 H2: last_marks now falls back to entry_price when
    # last_mark_price is missing, mirroring the live long/short EOD
    # pattern above (price = entry_price when bars unavailable). The
    # tracker's close_all_for_eod additionally force-closes any
    # remaining orphan with EOD_NO_MARK + entry_price as the exit so
    # nothing is silently left open.
    try:
        last_marks: dict[str, float] = {}
        tr = tg.shadow_pnl.tracker()
        with tr._lock:
            for cfg_positions in tr._open.values():
                for sp in cfg_positions:
                    if sp.last_mark_price is not None:
                        last_marks[sp.ticker] = sp.last_mark_price
                    elif sp.ticker not in last_marks:
                        last_marks[sp.ticker] = float(sp.entry_price)
        tr.close_all_for_eod(last_marks)
    except Exception as e:
        logger.warning("[V520-SHADOW-PNL] EOD shadow close failed: %s", e)

    _, _, total_pnl, wins, losses, n_trades = tg._today_pnl_breakdown()
    msg = (
        f"EOD CLOSE Complete\n"
        f"  Trades: {n_trades}  W/L: {wins}/{losses}\n"
        f"  Day P&L: ${total_pnl:+.2f}\n"
        f"  Cash: ${tg.paper_cash:,.2f}"
    )
    tg.send_telegram(msg)
    # C-R5: EOD force-close flattens any open v5 position regardless of
    # state \u2014 we lock every track so the next session starts fresh
    # rather than resuming a half-mid-state machine.
    try:
        tg.v5_lock_all_tracks("eod")
    except Exception:
        logger.exception("v5_lock_all_tracks failed (eod)")
    # v5.5.2 \u2014 enforce 90-day retention on the bar archive once per
    # day at EOD. Failure-tolerant; never raises.
    try:
        deleted = tg.bar_archive.cleanup_old_dirs(retain_days=90)
        if deleted:
            logger.info("[V510-BAR] retention cleanup removed %d dated dirs",
                        len(deleted))
    except Exception as e:
        logger.warning("[V510-BAR] retention cleanup failed: %s", e)
    tg.save_paper_state()
