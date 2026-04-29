"""broker.stops \u2014 stop-management helpers (breakeven, capped, ladder, retighten).

Extracted from trade_genius.py in v5.11.2 PR 1.
"""
from __future__ import annotations

import logging
import sys as _sys

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


def _breakeven_long_stop(entry_price, current_price, current_stop,
                         arm_pct=None):
    """Return the ratcheted long stop, or the existing stop unchanged.

    A long is in +arm_pct profit when current_price \u2265 entry * (1+arm_pct).
    When armed, the stop pulls up to entry (breakeven). We return
    max(current_stop, entry) so the ratchet can only tighten.

    Returns (new_stop, armed). `armed` is True if the threshold is
    met, regardless of whether the stop actually moved (it may
    already be at or above entry).
    """
    if arm_pct is None:
        arm_pct = _tg().BREAKEVEN_RATCHET_PCT
    arm_price = entry_price * (1.0 + arm_pct)
    if current_price < arm_price:
        return current_stop, False
    # Armed \u2014 stop can never go below entry (never looser).
    new_stop = round(max(current_stop, entry_price), 2)
    return new_stop, True


def _breakeven_short_stop(entry_price, current_price, current_stop,
                          arm_pct=None):
    """Return the ratcheted short stop, or the existing stop unchanged.

    A short is in +arm_pct profit when current_price \u2264 entry * (1\u2212arm_pct).
    When armed, the stop pulls down to entry. We return
    min(current_stop, entry) so the ratchet can only tighten.
    """
    if arm_pct is None:
        arm_pct = _tg().BREAKEVEN_RATCHET_PCT
    arm_price = entry_price * (1.0 - arm_pct)
    if current_price > arm_price:
        return current_stop, False
    new_stop = round(min(current_stop, entry_price), 2)
    return new_stop, True


def _capped_long_stop(or_high_val, entry_price, max_pct=None):
    """Compute long stop with 0.75%-from-entry cap.

    Returns (stop_price, capped, baseline_stop) \u2014 `capped` is True when
    the entry-relative floor was tighter than the OR baseline.
    """
    if max_pct is None:
        max_pct = _tg().MAX_STOP_PCT
    baseline = or_high_val - 0.90
    floor = entry_price * (1.0 - max_pct)
    # For longs, "tighter" = higher stop (closer to entry from below).
    final = max(baseline, floor)
    return round(final, 2), final > baseline, round(baseline, 2)


def _capped_short_stop(pdc_val, entry_price, max_pct=None):
    """Compute short stop with 0.75%-from-entry cap.

    Returns (stop_price, capped, baseline_stop). For shorts, "tighter"
    = lower stop (closer to entry from above).
    """
    if max_pct is None:
        max_pct = _tg().MAX_STOP_PCT
    baseline = pdc_val + 0.90
    ceiling = entry_price * (1.0 + max_pct)
    final = min(baseline, ceiling)
    return round(final, 2), final < baseline, round(baseline, 2)


def _ladder_stop_long(pos):
    """Return the profit-lock ladder stop for a long position.

    Uses pos["trail_high"] as the peak. Stop is peak \u2212 give_back%
    where give_back shrinks as peak grows. Below +1% peak, returns
    `initial_stop` (structural stop only). Falls back to pos["stop"]
    when initial_stop is absent (legacy positions).

    Never looser than `initial_stop` \u2014 returns max(tier_stop,
    initial_stop) so the structural floor is permanent.
    """
    entry = pos.get("entry_price") or 0.0
    if entry <= 0:
        return pos.get("stop", 0)
    peak = pos.get("trail_high", entry) or entry
    peak_gain_pct = (peak - entry) / entry
    initial = pos.get("initial_stop", pos.get("stop", 0))
    # Iterate highest tier first so first match wins.
    for trigger, give_back_pct in _tg().LADDER_TIERS_LONG:
        if peak_gain_pct >= trigger:
            tier_stop = peak * (1.0 - give_back_pct)
            return round(max(tier_stop, initial), 2)
    # Below 1% gain \u2014 structural stop only.
    return initial


def _ladder_stop_short(pos):
    """Return the profit-lock ladder stop for a short position.

    Mirror of _ladder_stop_long. Uses pos["trail_low"] as the peak
    (lowest price reached). Peak gain % = (entry \u2212 low) / entry.
    Stop is peak + give_back% where give_back shrinks as peak
    deepens. Never looser (higher) than `initial_stop`.
    """
    entry = pos.get("entry_price") or 0.0
    if entry <= 0:
        return pos.get("stop", 0)
    peak = pos.get("trail_low", entry) or entry
    peak_gain_pct = (entry - peak) / entry
    initial = pos.get("initial_stop", pos.get("stop", 0))
    for trigger, give_back_pct in _tg().LADDER_TIERS_LONG:
        if peak_gain_pct >= trigger:
            tier_stop = peak * (1.0 + give_back_pct)
            # Tighter = lower for short, so take min with initial.
            return round(min(tier_stop, initial), 2)
    return initial


def _retighten_long_stop(ticker, pos, current_price,
                         force_exit=True):
    """Retighten a single long position's stop.

    Two layers (cap + breakeven ratchet), applied based on trail state.

    When trail is NOT armed (v3.4.23 + v3.4.25 behavior):
      1. 0.75% cap: floor = entry * (1 \u2212 MAX_STOP_PCT).
      2. Breakeven ratchet: once current \u2265 entry * (1+0.50%), pull
         pos["stop"] up to entry.

    When trail IS armed (v3.4.26 new behavior):
      Cap layer is skipped \u2014 trail was designed to replace it.
      Ratchet still runs but acts on pos["trail_stop"] instead of
      pos["stop"], because once trail is armed, manage_positions uses
      trail_stop for exit decisions. If the trail armed on an
      unfavorable dip (trail_low close to entry, trail_stop below
      entry), the ratchet pulls the effective exit stop up to entry.
      Pure tighten \u2014 never loosens.

    Returns one of:
      ("already_tight", stop, None) \u2014 nothing tightens further.
      ("tightened", old_stop, new)  \u2014 cap tightened pos["stop"].
      ("ratcheted", old_stop, new)  \u2014 ratchet tightened pos["stop"].
      ("ratcheted_trail", old_ts, new_ts)
                                    \u2014 ratchet tightened trail_stop
                                      while trail is armed.
      ("exit", new_stop, None)      \u2014 new stop breached; exited with
                                      reason=RETRO_CAP.
    """
    tg = _tg()
    entry_price = pos["entry_price"]

    # v3.4.26 \u2014 trail-armed branch. Ratchet acts on trail_stop.
    if pos.get("trail_active"):
        current_trail = pos.get("trail_stop")
        if current_trail is None:
            # No trail_stop yet (shouldn't happen once armed, but
            # fail-safe) \u2014 leave it to manage_positions on next tick.
            return ("already_tight", pos["stop"], None)
        # Only fire ratchet if we're at or above the +0.50% arm.
        arm_price = entry_price * (1.0 + tg.BREAKEVEN_RATCHET_PCT)
        if current_price < arm_price:
            return ("already_tight", current_trail, None)
        # Pure tighten: trail floor never falls below entry once armed.
        new_trail = round(max(current_trail, entry_price), 2)
        if new_trail <= current_trail:
            return ("already_tight", current_trail, None)
        old_trail = current_trail
        pos["trail_stop"] = new_trail
        tg.logger.info(
            "[BREAKEVEN] %s LONG trail_stop ratcheted to entry: "
            "$%.2f \u2192 $%.2f (entry=$%.2f, current=$%.2f, "
            "trail_active=True)",
            ticker, old_trail, new_trail, entry_price, current_price,
        )
        return ("ratcheted_trail", old_trail, new_trail)

    current_stop = pos["stop"]

    # Layer 1: 0.75% cap (v3.4.23).
    floor = round(entry_price * (1.0 - tg.MAX_STOP_PCT), 2)
    capped_stop = max(current_stop, floor)  # tighter = higher for long

    # Layer 2: breakeven ratchet (v3.4.25). Stacks on top of cap \u2014
    # breakeven is always \u2265 (entry \u2212 0.75%), so this only tightens.
    ratcheted_stop, armed = _breakeven_long_stop(
        entry_price, current_price, capped_stop,
    )

    new_stop = ratcheted_stop
    if new_stop <= current_stop:
        return ("already_tight", current_stop, None)

    old_stop = current_stop
    pos["stop"] = new_stop
    # Classify which layer caused the tighten \u2014 informative logging.
    if armed and new_stop > floor:
        status = "ratcheted"
        tg.logger.info(
            "[BREAKEVEN] %s LONG stop ratcheted to entry: $%.2f \u2192 $%.2f "
            "(entry=$%.2f, current=$%.2f)",
            ticker, old_stop, new_stop, entry_price, current_price,
        )
    else:
        status = "tightened"
        tg.logger.info(
            "[RETRO_CAP] %s LONG stop tightened: $%.2f \u2192 $%.2f "
            "(entry=$%.2f, current=$%.2f)",
            ticker, old_stop, new_stop, entry_price, current_price,
        )
    # If the market has already broken the new stop, exit now.
    if force_exit and current_price <= new_stop:
        tg.logger.warning(
            "[RETRO_CAP] %s LONG already breached at tighten time "
            "(current=$%.2f \u2264 new_stop=$%.2f) \u2014 exiting immediately.",
            ticker, current_price, new_stop,
        )
        tg.close_position(ticker, current_price, reason="RETRO_CAP")
        return ("exit", new_stop, None)
    return (status, old_stop, new_stop)


def _retighten_short_stop(ticker, pos, current_price,
                          force_exit=True):
    """Retighten a single short position's stop (cap + breakeven).

    Same return shape as _retighten_long_stop. For shorts, "tighter" =
    lower stop (closer to entry from above).

    v3.4.26: when trail_active=True, cap is skipped but the breakeven
    ratchet runs against pos["trail_stop"] \u2014 manage_short_positions
    uses trail_stop for exit decisions once armed.
    """
    tg = _tg()
    entry_price = pos["entry_price"]

    # v3.4.26 \u2014 trail-armed branch. Ratchet acts on trail_stop.
    if pos.get("trail_active"):
        current_trail = pos.get("trail_stop")
        if current_trail is None:
            return ("already_tight", pos["stop"], None)
        arm_price = entry_price * (1.0 - tg.BREAKEVEN_RATCHET_PCT)
        if current_price > arm_price:
            return ("already_tight", current_trail, None)
        # For shorts, tighter = lower. Cap at entry from above.
        new_trail = round(min(current_trail, entry_price), 2)
        if new_trail >= current_trail:
            return ("already_tight", current_trail, None)
        old_trail = current_trail
        pos["trail_stop"] = new_trail
        tg.logger.info(
            "[BREAKEVEN] %s SHORT trail_stop ratcheted to entry: "
            "$%.2f \u2192 $%.2f (entry=$%.2f, current=$%.2f, "
            "trail_active=True)",
            ticker, old_trail, new_trail, entry_price, current_price,
        )
        return ("ratcheted_trail", old_trail, new_trail)

    current_stop = pos["stop"]

    # Layer 1: 0.75% cap (v3.4.23).
    ceiling = round(entry_price * (1.0 + tg.MAX_STOP_PCT), 2)
    capped_stop = min(current_stop, ceiling)  # tighter = lower for short

    # Layer 2: breakeven ratchet (v3.4.25).
    ratcheted_stop, armed = _breakeven_short_stop(
        entry_price, current_price, capped_stop,
    )

    new_stop = ratcheted_stop
    if new_stop >= current_stop:
        return ("already_tight", current_stop, None)

    old_stop = current_stop
    pos["stop"] = new_stop
    if armed and new_stop < ceiling:
        status = "ratcheted"
        tg.logger.info(
            "[BREAKEVEN] %s SHORT stop ratcheted to entry: $%.2f \u2192 $%.2f "
            "(entry=$%.2f, current=$%.2f)",
            ticker, old_stop, new_stop, entry_price, current_price,
        )
    else:
        status = "tightened"
        tg.logger.info(
            "[RETRO_CAP] %s SHORT stop tightened: $%.2f \u2192 $%.2f "
            "(entry=$%.2f, current=$%.2f)",
            ticker, old_stop, new_stop, entry_price, current_price,
        )
    if force_exit and current_price >= new_stop:
        tg.logger.warning(
            "[RETRO_CAP] %s SHORT already breached at tighten time "
            "(current=$%.2f \u2265 new_stop=$%.2f) \u2014 exiting immediately.",
            ticker, current_price, new_stop,
        )
        tg.close_short_position(ticker, current_price, "RETRO_CAP")
        return ("exit", new_stop, None)
    return (status, old_stop, new_stop)


def retighten_all_stops(force_exit=True, fetch_prices=True):
    """Retighten every open position's stop to the 0.75% cap.

    Returns a summary dict: {tightened: int, exited: int, no_op: int,
    already_tight: int, errors: int, details: list[dict]}

    Safe to call repeatedly \u2014 if all stops are already tight, it's a
    no-op. When fetch_prices is False, uses entry_price as a
    best-effort proxy for "current" (startup mode, before any scanner
    cycles have run).
    """
    tg = _tg()
    # v3.4.25: separate counter for breakeven-ratchet tightenings, so
    # logging and /retighten output can distinguish cap vs ratchet.
    # v3.4.26: ratcheted_trail counts breakeven-ratchet tightenings
    # applied to trail_stop (when trail is armed).
    summary = {"tightened": 0, "ratcheted": 0, "ratcheted_trail": 0,
               "exited": 0, "no_op": 0, "already_tight": 0,
               "errors": 0, "details": []}

    def _current(ticker, fallback):
        if not fetch_prices:
            return fallback
        try:
            bars = tg.fetch_1min_bars(ticker)
            if bars and bars.get("current_price"):
                return bars["current_price"]
        except Exception as e:
            tg.logger.warning("[RETRO_CAP] %s fetch_1min_bars failed: %s",
                              ticker, e)
        return fallback

    # Longs (paper only)
    for ticker in list(tg.positions.keys()):
        pos = tg.positions.get(ticker)
        if not pos:
            continue
        try:
            cur = _current(ticker, pos["entry_price"])
            status, old, new = _retighten_long_stop(
                ticker, pos, cur, force_exit=force_exit,
            )
            key = "exited" if status == "exit" else status
            summary[key] = summary.get(key, 0) + 1
            summary["details"].append({
                "ticker": ticker, "side": "LONG",
                "status": status,
                "old_stop": old, "new_stop": new,
            })
        except Exception as e:
            summary["errors"] += 1
            # v4.11.0 \u2014 report_error: trading-path retighten failure.
            tg.report_error(
                executor="main",
                code="RETRO_CAP_LONG_FAILED",
                severity="error",
                summary=f"Retro cap LONG failed: {ticker}",
                detail=f"{type(e).__name__}: {e}",
            )

    # Shorts (paper only)
    for ticker in list(tg.short_positions.keys()):
        pos = tg.short_positions.get(ticker)
        if not pos:
            continue
        try:
            cur = _current(ticker, pos["entry_price"])
            status, old, new = _retighten_short_stop(
                ticker, pos, cur, force_exit=force_exit,
            )
            key = "exited" if status == "exit" else status
            summary[key] = summary.get(key, 0) + 1
            summary["details"].append({
                "ticker": ticker, "side": "SHORT",
                "status": status,
                "old_stop": old, "new_stop": new,
            })
        except Exception as e:
            summary["errors"] += 1
            # v4.11.0 \u2014 report_error: trading-path retighten failure.
            tg.report_error(
                executor="main",
                code="RETRO_CAP_SHORT_FAILED",
                severity="error",
                summary=f"Retro cap SHORT failed: {ticker}",
                detail=f"{type(e).__name__}: {e}",
            )

    if (summary["tightened"] or summary["ratcheted"]
            or summary["ratcheted_trail"] or summary["exited"]):
        tg.logger.info(
            "[RETRO_CAP] cycle summary: %d tightened, %d ratcheted, "
            "%d trail-ratcheted, %d exited, %d already-tight, "
            "%d no-op",
            summary["tightened"], summary["ratcheted"],
            summary["ratcheted_trail"], summary["exited"],
            summary["already_tight"], summary["no_op"],
        )
    return summary
