"""v6.16.1 \u2014 earnings_watcher.exits: live-position exit logic.

Implements evaluate_exit() which mirrors simulate_runaway() from
earnings_watcher_spec/replay/decision_engine.py using the same
v4-locked risk constants:
  hard_stop=DMI_HARD_STOP, trail_trigger=DMI_TRAIL_TRIGGER,
  trail_pct=DMI_TRAIL_PCT, time_stop_min=DMI_TIME_STOP_MIN.

Hard boundary: MUST NOT import from eye_of_tiger or trade_genius.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

from earnings_watcher.sizing import (
    DMI_HARD_STOP,
    DMI_TRAIL_PCT,
    DMI_TRAIL_TRIGGER,
    DMI_TIME_STOP_MIN,
)
from earnings_watcher.signals import filter_bars_for_session

logger = logging.getLogger("earnings_watcher")


def evaluate_exit(
    position_state: Dict[str, Any],
    current_bar: Dict[str, Any],
    elapsed_minutes: int,
) -> Tuple[bool, str]:
    """Decide whether to exit an open earnings_watcher position.

    Parameters
    ----------
    position_state : dict
        Live position record. Expected keys:
          entry_px        float   \u2014 fill price at entry
          entry_idx       int     \u2014 bar index at entry (within session bars)
          entry_ts        str     \u2014 ISO 8601 timestamp of entry bar
          peak_pct        float   \u2014 running maximum gain (fraction, e.g. 0.04)
          trough_pct      float   \u2014 running minimum gain (signed, e.g. -0.02)
          trail_active    bool    \u2014 True once trail has been armed
          trail_stop      float   \u2014 current trail stop level (fraction gain)
          direction       str     \u2014 'long' or 'short'
    current_bar : dict
        Latest 1-min bar: {timestamp, open, high, low, close, volume}
    elapsed_minutes : int
        Number of 1-min bars elapsed since entry (computed by caller by
        counting minute bars since entry_ts). Used for the time stop.

    Returns
    -------
    (should_exit, reason)
        should_exit: True if the position should be closed now
        reason: one of 'hard_stop', 'trail', 'time', 'session_end', '' (no exit)

    Notes
    -----
    - hard_stop  : price moved DMI_HARD_STOP (3%) against entry
    - trail      : trail armed at +DMI_TRAIL_TRIGGER (2%), stop at peak - DMI_TRAIL_PCT (5%)
    - time        : elapsed_minutes >= DMI_TIME_STOP_MIN (90)
    - session_end: bar timestamp falls outside the declared session window
                   (caller must pass a bar from the correct session; if the
                    bar hour is outside session bounds we exit defensively)
    """
    entry_px: float = float(position_state["entry_px"])
    direction: str = position_state.get("direction", "long")
    peak_pct: float = float(position_state.get("peak_pct", 0.0))
    trough_pct: float = float(position_state.get("trough_pct", 0.0))
    trail_active: bool = bool(position_state.get("trail_active", False))
    trail_stop: float = float(position_state.get("trail_stop", 0.0))

    sign = 1 if direction == "long" else -1
    close_px = float(current_bar["close"])
    chg = (close_px - entry_px) / entry_px * sign

    # ---- update running peaks in-place so caller can persist them ----
    if chg > peak_pct:
        position_state["peak_pct"] = chg
        peak_pct = chg
    if chg < trough_pct:
        position_state["trough_pct"] = chg
        trough_pct = chg

    # ---- session_end: defensive check via bar hour ----
    ts: str = current_bar.get("timestamp", "")
    if len(ts) >= 16:
        bar_h = int(ts[11:13])
        bar_m = int(ts[14:16])
        bar_mins = bar_h * 60 + bar_m
        # BMO window: 08:00-13:25 UTC; AMC window: 19:00-23:55 UTC
        # If neither window matches, fire session_end
        in_bmo = (8 * 60) <= bar_mins <= (13 * 60 + 25)
        in_amc = (19 * 60) <= bar_mins <= (23 * 60 + 55)
        if not (in_bmo or in_amc):
            logger.info("[EW-EXIT] session_end ticker=%s ts=%s chg=%.4f",
                        position_state.get("ticker", "?"), ts, chg)
            return True, "session_end"

    # ---- hard stop ----
    if chg <= -DMI_HARD_STOP:
        logger.info("[EW-EXIT] hard_stop ticker=%s chg=%.4f threshold=%.4f",
                    position_state.get("ticker", "?"), chg, -DMI_HARD_STOP)
        return True, "hard_stop"

    # ---- trail logic ----
    if chg >= DMI_TRAIL_TRIGGER:
        trail_active = True
        position_state["trail_active"] = True
        candidate = chg - DMI_TRAIL_PCT
        if candidate > trail_stop:
            trail_stop = candidate
            position_state["trail_stop"] = trail_stop
    if trail_active and chg <= trail_stop:
        logger.info("[EW-EXIT] trail ticker=%s chg=%.4f trail_stop=%.4f",
                    position_state.get("ticker", "?"), chg, trail_stop)
        return True, "trail"

    # ---- time stop ----
    if elapsed_minutes >= DMI_TIME_STOP_MIN:
        logger.info("[EW-EXIT] time ticker=%s elapsed=%d chg=%.4f",
                    position_state.get("ticker", "?"), elapsed_minutes, chg)
        return True, "time"

    return False, ""


def compute_elapsed_minutes(
    bars_since_entry: int,
) -> int:
    """Convert bar count to elapsed minutes (1 bar = 1 minute for 1-min bars).

    This is a trivial identity at 1-min resolution, but provided as a named
    helper so callers are explicit and the conversion is easy to change if
    we ever switch bar resolution.
    """
    return bars_since_entry
