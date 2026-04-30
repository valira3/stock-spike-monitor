"""v5.21.0 -- engine.sma_stack: daily SMA stack computation.

Provides a pure function `compute_sma_stack` that takes a chronological
list of daily close prices (most-recent-last) and returns a structured
dict describing the SMA values, relative positions, stack classification,
and order-line chips for the Daily SMA Stack panel in the Permit Matrix
expanded row.

This module is pure computation -- no I/O, no state, no imports from
the rest of the repo. Data fetching lives in engine.daily_bars.

Public API:
    SMA_WINDOWS -- tuple of window sizes (12, 22, 55, 100, 200)
    compute_sma_stack(daily_closes) -> dict
"""

from __future__ import annotations

from typing import Union

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SMA_WINDOWS: tuple[int, ...] = (12, 22, 55, 100, 200)

# Shorthand aliases used internally for readability.
_W12, _W22, _W55, _W100, _W200 = SMA_WINDOWS


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _simple_mean(values: list[float]) -> float:
    """Return the arithmetic mean of a non-empty list of floats."""
    return sum(values) / len(values)


def _sma(closes: list[float], window: int) -> Union[float, None]:
    """Compute the simple moving average of the last `window` closes.

    Returns None when len(closes) < window (insufficient history).
    """
    if len(closes) < window:
        return None
    return _simple_mean(closes[-window:])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_sma_stack(daily_closes: list[float]) -> dict:
    """Compute the full daily SMA stack state from a list of closes.

    Parameters
    ----------
    daily_closes:
        Chronological list of daily close prices, most-recent-last.
        An empty list is valid and returns all-None values.

    Returns
    -------
    dict with keys:
        daily_close     -- float | None   last close in the list
        smas            -- {window: float|None, ...}
        deltas_abs      -- {window: float|None, ...}  close - sma
        deltas_pct      -- {window: float|None, ...}  (close - sma) / sma
        above           -- {window: bool, ...}         close > sma
        stack_classification -- "bullish" | "bearish" | "mixed"
        stack_substate  -- one of five substate strings (see below)
        order_chips     -- list of {window, value} for windows 12/22/55
        order_relations -- two-element list: 12-vs-22, 22-vs-55 relation
                           each element is "gt"|"lt"|"eq"|"unknown"
    """
    closes = list(daily_closes or [])

    # -- daily_close ---------------------------------------------------------
    daily_close: Union[float, None] = closes[-1] if closes else None

    # -- smas ----------------------------------------------------------------
    smas: dict[int, Union[float, None]] = {}
    for w in SMA_WINDOWS:
        smas[w] = _sma(closes, w)

    # -- deltas_abs / deltas_pct / above -------------------------------------
    deltas_abs: dict[int, Union[float, None]] = {}
    deltas_pct: dict[int, Union[float, None]] = {}
    above: dict[int, bool] = {}

    for w in SMA_WINDOWS:
        sma_val = smas[w]
        if daily_close is None or sma_val is None:
            deltas_abs[w] = None
            deltas_pct[w] = None
            above[w] = False
        else:
            d_abs = daily_close - sma_val
            deltas_abs[w] = d_abs
            deltas_pct[w] = d_abs / sma_val
            above[w] = daily_close > sma_val

    # -- stack_classification ------------------------------------------------
    # "bullish"  iff sma_12 > sma_22 > sma_55 (all three present, strict)
    # "bearish"  iff sma_12 < sma_22 < sma_55 (all three present, strict)
    # "mixed"    otherwise (including any of the three being None)
    sma12 = smas[_W12]
    sma22 = smas[_W22]
    sma55 = smas[_W55]

    if sma12 is not None and sma22 is not None and sma55 is not None and sma12 > sma22 > sma55:
        stack_classification = "bullish"
    elif sma12 is not None and sma22 is not None and sma55 is not None and sma12 < sma22 < sma55:
        stack_classification = "bearish"
    else:
        stack_classification = "mixed"

    # -- stack_substate -------------------------------------------------------
    # Uses all five SMAs for maximum granularity.
    sma100 = smas[_W100]
    sma200 = smas[_W200]

    all_five_present = all(smas[w] is not None for w in SMA_WINDOWS)

    if daily_close is None:
        stack_substate = "scrambled"
    elif (
        all_five_present
        and daily_close > smas[_W12]  # type: ignore[operator]
        and daily_close > smas[_W22]  # type: ignore[operator]
        and daily_close > smas[_W55]  # type: ignore[operator]
        and daily_close > smas[_W100]  # type: ignore[operator]
        and daily_close > smas[_W200]
    ):  # type: ignore[operator]
        stack_substate = "all_above"
    elif (
        all_five_present
        and daily_close < smas[_W12]  # type: ignore[operator]
        and daily_close < smas[_W22]  # type: ignore[operator]
        and daily_close < smas[_W55]  # type: ignore[operator]
        and daily_close < smas[_W100]  # type: ignore[operator]
        and daily_close < smas[_W200]
    ):  # type: ignore[operator]
        stack_substate = "all_below"
    elif (
        sma12 is not None
        and sma22 is not None
        and sma100 is not None
        and sma200 is not None
        and daily_close > sma12
        and daily_close > sma22
        and daily_close < sma100
        and daily_close < sma200
    ):
        stack_substate = "above_short_below_long"
    elif (
        sma12 is not None
        and sma22 is not None
        and sma100 is not None
        and sma200 is not None
        and daily_close < sma12
        and daily_close < sma22
        and daily_close > sma100
        and daily_close > sma200
    ):
        stack_substate = "below_short_above_long"
    else:
        stack_substate = "scrambled"

    # -- order_chips ----------------------------------------------------------
    order_chips = [
        {"window": _W12, "value": smas[_W12]},
        {"window": _W22, "value": smas[_W22]},
        {"window": _W55, "value": smas[_W55]},
    ]

    # -- order_relations ------------------------------------------------------
    # Two relations: [12-vs-22, 22-vs-55]
    def _relation(a: Union[float, None], b: Union[float, None]) -> str:
        if a is None or b is None:
            return "unknown"
        if a > b:
            return "gt"
        if a < b:
            return "lt"
        return "eq"

    order_relations = [
        _relation(smas[_W12], smas[_W22]),
        _relation(smas[_W22], smas[_W55]),
    ]

    return {
        "daily_close": daily_close,
        "smas": smas,
        "deltas_abs": deltas_abs,
        "deltas_pct": deltas_pct,
        "above": above,
        "stack_classification": stack_classification,
        "stack_substate": stack_substate,
        "order_chips": order_chips,
        "order_relations": order_relations,
    }


__all__ = ["SMA_WINDOWS", "compute_sma_stack"]
