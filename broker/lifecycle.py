"""broker.lifecycle \u2014 entry/exit dispatchers and EOD close.

Extracted from trade_genius.py in v5.11.2 PR 4.
"""

from __future__ import annotations

import logging
import sys as _sys
import time as _time
from datetime import datetime as _datetime

from broker.orders import check_breakout, execute_breakout, close_breakout
from engine.timing import EOD_FLUSH_ET, ET as _ET, is_after_eod_et
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
def _eod_align_to_spec(now: _datetime | None = None, sleep_fn=_time.sleep) -> float:
    """Sleep until EOD_FLUSH_ET wall-clock if called before it.

    v5.13.1 prod-verify finding: scheduler fires eod_close at 15:49:00 ET
    (HH:MM precision), but spec EOD_FLUSH_ET is 15:49:59 ET. This helper
    blocks the calling thread until the spec wall-clock so positions are
    flushed at exactly 15:49:59 ET, never earlier.

    Returns the number of seconds slept (for tests/visibility). Pass a
    fake ``sleep_fn`` and ``now`` to test without waiting.
    """
    if now is None:
        now = _datetime.now(tz=_ET)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_ET)
    else:
        now = now.astimezone(_ET)
    if is_after_eod_et(now):
        return 0.0
    target = now.replace(
        hour=EOD_FLUSH_ET.hour,
        minute=EOD_FLUSH_ET.minute,
        second=EOD_FLUSH_ET.second,
        microsecond=0,
    )
    delay = (target - now).total_seconds()
    if delay <= 0:
        return 0.0
    # Cap defensively: if for any reason we're called more than 5 min
    # before EOD, just return rather than block the scheduler thread.
    if delay > 300:
        logger.warning(
            "[EOD FLUSH] _eod_align_to_spec: called %.0fs before EOD; "
            "skipping align-to-spec sleep (scheduler misfire?)",
            delay,
        )
        return 0.0
    logger.info(
        "[EOD FLUSH] aligning to spec wall-clock 15:49:59 ET (sleep %.1fs)",
        delay,
    )
    sleep_fn(delay)
    return delay


def eod_close():
    """Force-close all open long AND short positions at 15:49:59 ET.

    v5.13.0 PR-5 SHARED-EOD: EOD flush moved from 15:59:50 ET to 15:49:59 ET
    per Tiger Sovereign §3. Order types are unchanged in this PR (PR 6 owns
    the LIMIT/STOP MARKET split). All positions exit regardless of sentinel
    or ratchet state. One ``[EOD FLUSH]`` line is logged per position.

    v5.13.1 prod-verify guard: the scheduler fires this at 15:49:00 ET (HH:MM
    precision only). The spec wall-clock is 15:49:59 ET. If we are called
    before EOD_FLUSH_ET, sleep until that wall-clock so positions are not
    flushed 59 seconds early. If we are called at/after EOD_FLUSH_ET, run
    immediately.
    """
    _eod_align_to_spec()
    tg = _tg()
    # v4.0.0-alpha \u2014 notify executors to flatten everything on Alpaca.
    # Per-position close events still fire from close_position /
    # close_short_position below; this event lets executors shortcut with
    # a single close_all_positions call if they prefer.
    tg._emit_signal(
        {
            "kind": "EOD_CLOSE_ALL",
            "ticker": "",
            "price": 0.0,
            "reason": "EOD",
            "timestamp_utc": tg._utc_now_iso(),
            "main_shares": 0,
        }
    )
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
            logger.info(
                "[EOD FLUSH] side=LONG ticker=%s price=%.2f reason=EOD",
                ticker,
                float(price),
            )
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
            logger.info(
                "[EOD FLUSH] side=SHORT ticker=%s price=%.2f reason=EOD",
                ticker,
                float(price),
            )
            close_short_position(ticker, price, "EOD")

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
            logger.info("[V510-BAR] retention cleanup removed %d dated dirs", len(deleted))
    except Exception as e:
        logger.warning("[V510-BAR] retention cleanup failed: %s", e)
    tg.save_paper_state()
