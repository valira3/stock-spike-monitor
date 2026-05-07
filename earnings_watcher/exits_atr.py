"""v7.2.0 \u2014 earnings_watcher.exits_atr: ATR-based exit policy for PMR/PMC.

Strategy summary
----------------
Hard stop : -ATR_HARD_STOP_MULT * atr_5min from entry (default 1.5x ATR).
Trail arm : when chg >= ATR_TRAIL_TRIGGER_MULT * atr_5min (default 1.0x ATR).
Trail stop: peak - ATR_TRAIL_PCT_MULT * atr_5min (default 1.5x ATR).
Time stop : ATR_TIME_STOP_MIN bars elapsed (default 60 min).
Session   : hard exit at PMR_HARD_EXIT_UTC_MIN (09:25 ET) for premarket;
            at PMC_HARD_EXIT_UTC_MIN (19:55 ET) for afterhours; falls back
            to defensive bar-window check if not provided.

Hard boundary: MUST NOT import from eye_of_tiger or trade_genius. Position
records are expected to carry an `atr_5min` field captured at entry time
(populated by signals_pmr.evaluate_and_size_pmr / signals_pmc).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Tuple

logger = logging.getLogger("earnings_watcher")


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ATR multiples (in units of atr_5min / entry_px, i.e. fraction-of-price moves).
ATR_HARD_STOP_MULT = _f("ATR_HARD_STOP_MULT", 1.5)
ATR_TRAIL_TRIGGER_MULT = _f("ATR_TRAIL_TRIGGER_MULT", 1.0)
ATR_TRAIL_PCT_MULT = _f("ATR_TRAIL_PCT_MULT", 1.5)
ATR_TIME_STOP_MIN = _i("ATR_TIME_STOP_MIN", 60)

# Bar elapsed before bar-low/high is considered tradeable (mirrors DMI exit).
ATR_BAR_LOW_STOP_MIN_ELAPSED = 2

# Session windows for atr_trail exits (UTC minutes).
# PMR runs 04:00-09:25 ET == 08:00-13:25 UTC (480-805) in EDT.
# PMC runs 16:00-19:55 ET == 20:00-23:55 UTC (1200-1435) in EDT.
PMR_SESSION_START_UTC_MIN = _i("PMR_BUILD_START_UTC_MIN", 8 * 60)
PMR_SESSION_END_UTC_MIN = _i("PMR_HARD_EXIT_UTC_MIN", 13 * 60 + 25)
PMC_SESSION_START_UTC_MIN = _i("PMC_BUILD_START_UTC_MIN", 20 * 60)
PMC_SESSION_END_UTC_MIN = _i("PMC_HARD_EXIT_UTC_MIN", 23 * 60 + 55)


def _bar_utc_min(bar: Dict[str, Any]) -> int:
    ts = bar.get("timestamp", "")
    if len(ts) < 16:
        return -1
    try:
        return int(ts[11:13]) * 60 + int(ts[14:16])
    except ValueError:
        return -1


def evaluate_exit_atr(
    position_state: Dict[str, Any],
    current_bar: Dict[str, Any],
    elapsed_minutes: int,
) -> Tuple[bool, str]:
    """ATR-trail exit decision for PMR/PMC positions.

    Parameters mirror earnings_watcher.exits.evaluate_exit. Position record
    MUST carry `atr_5min` (captured at entry by the signal module). If
    absent or non-positive, falls back to a conservative -3% hard stop and
    skips trail logic (logs a warning).

    Returns
    -------
    (should_exit, reason) where reason is one of
      'hard_stop' | 'trail' | 'time' | 'session_end' | ''
    """
    entry_px: float = float(position_state["entry_px"])
    direction: str = position_state.get("side", position_state.get("direction", "long"))
    peak_pct: float = float(position_state.get("peak_pct", 0.0))
    trough_pct: float = float(position_state.get("trough_pct", 0.0))
    trail_active: bool = bool(position_state.get("trail_active", False))
    trail_stop: float = float(position_state.get("trail_stop", 0.0))
    atr: float = float(position_state.get("atr_5min", 0.0))
    strategy: str = position_state.get("strategy", "pmr")

    if entry_px <= 0:
        return False, ""

    # Convert ATR ($) to a fraction-of-entry-price for symmetric long/short math.
    if atr > 0:
        atr_frac = atr / entry_px
    else:
        # Fallback: emulate a 3% hard stop with no trail. We log once.
        atr_frac = 0.03 / ATR_HARD_STOP_MULT
        logger.warning(
            "[EW-EXIT-ATR] missing atr_5min ticker=%s strategy=%s falling back to 3pct stop",
            position_state.get("ticker", "?"), strategy,
        )

    sign = 1 if direction == "long" else -1
    close_px = float(current_bar["close"])
    chg = (close_px - entry_px) / entry_px * sign

    # Bar-worst (low for long, high for short) used only for hard stop after warmup.
    use_bar_low = elapsed_minutes >= ATR_BAR_LOW_STOP_MIN_ELAPSED
    if use_bar_low:
        if direction == "long":
            adverse_px = float(current_bar.get("low", close_px))
        else:
            adverse_px = float(current_bar.get("high", close_px))
        worst_chg = (adverse_px - entry_px) / entry_px * sign
    else:
        worst_chg = chg

    # Update running peak/trough in-place.
    if chg > peak_pct:
        position_state["peak_pct"] = chg
        peak_pct = chg
    if chg < trough_pct:
        position_state["trough_pct"] = chg
        trough_pct = chg

    # ---- session_end (atr_trail enforces strategy-specific window) ----
    bar_min = _bar_utc_min(current_bar)
    if bar_min >= 0:
        if strategy == "pmr":
            in_window = PMR_SESSION_START_UTC_MIN <= bar_min <= PMR_SESSION_END_UTC_MIN
        elif strategy == "pmc":
            in_window = PMC_SESSION_START_UTC_MIN <= bar_min <= PMC_SESSION_END_UTC_MIN
        else:
            in_window = True  # unknown strategy: don't force-close
        if not in_window:
            logger.info(
                "[EW-EXIT-ATR] session_end ticker=%s strategy=%s ts=%s chg=%.4f",
                position_state.get("ticker", "?"), strategy,
                current_bar.get("timestamp", "?"), chg,
            )
            return True, "session_end"

    # ---- hard stop ----
    hard_threshold = ATR_HARD_STOP_MULT * atr_frac
    if worst_chg <= -hard_threshold:
        logger.info(
            "[EW-EXIT-ATR] hard_stop ticker=%s strategy=%s worst_chg=%.4f close_chg=%.4f threshold=%.4f atr_frac=%.4f",
            position_state.get("ticker", "?"), strategy,
            worst_chg, chg, -hard_threshold, atr_frac,
        )
        position_state["hard_stop_intrabar"] = True
        return True, "hard_stop"

    # ---- trail logic ----
    trigger = ATR_TRAIL_TRIGGER_MULT * atr_frac
    trail_dist = ATR_TRAIL_PCT_MULT * atr_frac
    if chg >= trigger:
        trail_active = True
        position_state["trail_active"] = True
        candidate = chg - trail_dist
        if candidate > trail_stop:
            trail_stop = candidate
            position_state["trail_stop"] = trail_stop
    if trail_active and chg <= trail_stop:
        logger.info(
            "[EW-EXIT-ATR] trail ticker=%s strategy=%s chg=%.4f trail_stop=%.4f atr_frac=%.4f",
            position_state.get("ticker", "?"), strategy, chg, trail_stop, atr_frac,
        )
        return True, "trail"

    # ---- time stop ----
    if elapsed_minutes >= ATR_TIME_STOP_MIN:
        logger.info(
            "[EW-EXIT-ATR] time ticker=%s strategy=%s elapsed=%d chg=%.4f",
            position_state.get("ticker", "?"), strategy, elapsed_minutes, chg,
        )
        return True, "time"

    return False, ""
