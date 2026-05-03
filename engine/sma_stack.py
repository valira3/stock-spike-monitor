# engine/sma_stack.py
# v6.0.1 / v6.9.0 \u2014 Daily SMA stack computation, restored after the v5.26.0
# v6.9.0: EMA series for backtest sweeps are available via
# ``backtest.indicator_cache.get_indicators`` (L2 cache). This module's
# ``compute_sma_stack`` is unchanged and used only for daily closes.
# Stage-1 spec-strict cut. Pure logic: takes a list of daily closes and
# returns the fully-formed dashboard payload that
# ``_pmtxSmaStackPanel`` in dashboard_static/app.js consumes. No
# network or process state \u2014 the daily-close fetch lives separately
# in trade_genius._daily_closes_for_sma so this module stays trivially
# testable.
#
# Output shape (matches the v5.21.0 frontend null-safe contract):
#   {
#     "daily_close": float | None,       # most recent close
#     "smas": {12: float|None, 22: float|None, 55: float|None,
#              100: float|None, 200: float|None},
#     "deltas_abs": {w: float|None, ...},  # close minus SMA(w)
#     "deltas_pct": {w: float|None, ...},  # (close/SMA(w) - 1) * 100
#     "above": {w: bool|None, ...},        # close > SMA(w)
#     "stack_classification": "bullish" | "bearish" | "mixed",
#     "stack_substate": "all_above" | "all_below"
#                       | "above_short_below_long"
#                       | "below_short_above_long"
#                       | "scrambled",
#     "order_chips": [{"window": int, "value": float|None}, ...],
#     "order_relations": [{"left": int, "op": ">"|"<"|"=", "right": int}, ...],
#   }
#
# A ``None`` return from ``compute_sma_stack`` means we did not have
# enough closes to compute even SMA(12); the frontend renders the
# "data not available" placeholder in that case.

from __future__ import annotations

from typing import Iterable, List, Optional

# Windows the dashboard renders. Order matters \u2014 it drives the
# left-to-right swatch order, the order-chip row, and the bullish-stack
# detection (12 > 22 > 55 long bias; 12 < 22 < 55 short bias).
WINDOWS: tuple[int, ...] = (12, 22, 55, 100, 200)


def _safe_mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    try:
        return float(sum(values)) / float(len(values))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _sma(closes: List[float], window: int) -> Optional[float]:
    """Simple moving average over the most recent ``window`` closes.

    Returns None if we do not have at least ``window`` closes. Closes
    must be ordered oldest-first; the SMA is the mean of the last
    ``window`` values.
    """
    if window <= 0:
        return None
    if len(closes) < window:
        return None
    tail = closes[-window:]
    return _safe_mean(tail)


def _classify_stack(smas: dict[int, Optional[float]],
                    daily_close: Optional[float]) -> tuple[str, str]:
    """Map the SMA values to (classification, substate).

    Bullish stack:   SMA(12) > SMA(22) > SMA(55) and close above all
                     three short windows. Strong long bias.
    Bearish stack:   SMA(12) < SMA(22) < SMA(55) and close below all
                     three short windows. Strong short bias.
    Otherwise mixed; the substate names where the price sits relative
    to the short and long ends of the stack so the operator can read
    intent at a glance.
    """
    s12 = smas.get(12)
    s22 = smas.get(22)
    s55 = smas.get(55)
    s100 = smas.get(100)
    s200 = smas.get(200)
    short_set = [v for v in (s12, s22, s55) if v is not None]
    long_set = [v for v in (s100, s200) if v is not None]

    # Need at least the three short windows + a daily close to claim a
    # full classification. Without them we fall back to "mixed /
    # scrambled" so the pill still has a sensible label.
    if (
        daily_close is not None
        and s12 is not None and s22 is not None and s55 is not None
    ):
        if s12 > s22 > s55 and daily_close > s12 and daily_close > s22 and daily_close > s55:
            return ("bullish", "all_above")
        if s12 < s22 < s55 and daily_close < s12 and daily_close < s22 and daily_close < s55:
            return ("bearish", "all_below")

    # Mixed substate naming \u2014 only meaningful when we have a close
    # plus at least one short and one long SMA.
    if daily_close is not None and short_set and long_set:
        above_short = all(daily_close > v for v in short_set)
        below_short = all(daily_close < v for v in short_set)
        above_long = all(daily_close > v for v in long_set)
        below_long = all(daily_close < v for v in long_set)
        if above_short and above_long:
            return ("mixed", "all_above")
        if below_short and below_long:
            return ("mixed", "all_below")
        if above_short and below_long:
            return ("mixed", "above_short_below_long")
        if below_short and above_long:
            return ("mixed", "below_short_above_long")

    return ("mixed", "scrambled")


def _order_relations(smas: dict[int, Optional[float]]) -> list[dict]:
    """Adjacent-pair relations across the SMA windows in canonical
    order. Used by the frontend to render an inline
    ``SMA12 > SMA22 > SMA55 > SMA100 > SMA200`` strip with proper
    operator chips. Missing values render as ``op="?"`` so the strip
    still preserves the position of the gap.
    """
    rel: list[dict] = []
    for i in range(len(WINDOWS) - 1):
        left = WINDOWS[i]
        right = WINDOWS[i + 1]
        lv = smas.get(left)
        rv = smas.get(right)
        if lv is None or rv is None:
            op = "?"
        elif lv > rv:
            op = ">"
        elif lv < rv:
            op = "<"
        else:
            op = "="
        rel.append({"left": left, "op": op, "right": right})
    return rel


def compute_sma_stack(closes: Iterable[float]) -> Optional[dict]:
    """Build the full SMA-stack payload from a list of daily closes.

    ``closes`` must be ordered oldest-first and contain real daily
    close prices. The most recent value is treated as ``daily_close``;
    each SMA(w) is the mean of the last ``w`` closes (so SMA(12)
    includes the most recent close).

    Returns ``None`` when we do not have enough closes to compute even
    the shortest SMA window (12). The frontend null-guard then renders
    "data not available" instead of a broken table.
    """
    try:
        cleaned: List[float] = [float(c) for c in closes if c is not None]
    except (TypeError, ValueError):
        return None
    if len(cleaned) < WINDOWS[0]:  # need at least 12
        return None

    daily_close: Optional[float] = cleaned[-1]
    smas: dict[int, Optional[float]] = {}
    for w in WINDOWS:
        smas[w] = _sma(cleaned, w)

    deltas_abs: dict[int, Optional[float]] = {}
    deltas_pct: dict[int, Optional[float]] = {}
    above: dict[int, Optional[bool]] = {}
    for w in WINDOWS:
        sv = smas[w]
        if sv is None or daily_close is None:
            deltas_abs[w] = None
            deltas_pct[w] = None
            above[w] = None
            continue
        deltas_abs[w] = daily_close - sv
        try:
            deltas_pct[w] = (daily_close / sv - 1.0) * 100.0 if sv else None
        except ZeroDivisionError:
            deltas_pct[w] = None
        above[w] = daily_close > sv

    classification, substate = _classify_stack(smas, daily_close)
    order_chips = [{"window": w, "value": smas[w]} for w in WINDOWS]
    order_relations = _order_relations(smas)

    return {
        "daily_close": daily_close,
        "smas": smas,
        "deltas_abs": deltas_abs,
        "deltas_pct": deltas_pct,
        "above": above,
        "stack_classification": classification,
        "stack_substate": substate,
        "order_chips": order_chips,
        "order_relations": order_relations,
    }
