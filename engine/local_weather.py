"""v5.31.5 / v6.2.0 \u2014 Per-Stock Local Weather override.

When the global QQQ permit (Section I) is closed for a side, but the
ticker's own price action shows a clean directional move, we may
override the global block. This is the "local weather" gate.

Loose rule (chosen by Val 2026-05-01):

    (5m_close past EMA9 OR price past opening AVWAP
     OR ticker has cleared OR by k*ATR \u2014 v6.2.0)
    AND DI confirms

For a LONG override:
    (ticker_5m_close > ticker_5m_ema9
     OR ticker_last > ticker_avwap
     OR ticker_last > or_high + k*atr_pm     # v6.2.0 OR-break leg
    ) AND di_plus_1m > di_minus_1m

For a SHORT override:
    (ticker_5m_close < ticker_5m_ema9
     OR ticker_last < ticker_avwap
     OR ticker_last < or_low  - k*atr_pm     # v6.2.0 OR-break leg
    ) AND di_minus_1m > di_plus_1m

v6.2.0 \u2014 the OR-break leg is forensics-validated against the 5/1
run: 1,798 rejections at this gate showed 49.4% would-have-profited;
longs in AVGO/GOOG/AMZN clocked 64% / +$0.79 mean. The new leg fires
only when ATR is non-zero and the price has cleared OR by the spec
multiple. Gated by V620_LOCAL_OR_BREAK_ENABLED.

The dashboard also reads `weather_direction` to render the per-stock
Weather column glyph in the permit matrix:
    'up'   \u2192 long-aligned local weather
    'down' \u2192 short-aligned local weather
    'flat' \u2192 mixed / inconclusive / data-missing

`evaluate_local_override` returns a dict for both engine wiring and
dashboard surfacing:

    {
        "open": bool,          # True = override opens the gate
        "reason": str,         # short tag for [LOCAL_OVERRIDE] log
        "weather_direction": "up" | "down" | "flat",
        "ema9_aligned": bool | None,
        "avwap_aligned": bool | None,
        "di_aligned": bool | None,
    }

Fail-closed: any None input collapses to {open=False, reason=data_missing}.
"""

from __future__ import annotations

from typing import Optional

SIDE_LONG = "LONG"
SIDE_SHORT = "SHORT"

WEATHER_UP = "up"
WEATHER_DOWN = "down"
WEATHER_FLAT = "flat"


def classify_local_weather(
    ticker_5m_close: Optional[float],
    ticker_5m_ema9: Optional[float],
    ticker_last: Optional[float],
    ticker_avwap: Optional[float],
    di_plus_1m: Optional[float],
    di_minus_1m: Optional[float],
    or_high: Optional[float] = None,
    or_low: Optional[float] = None,
    atr_pm: Optional[float] = None,
) -> str:
    """Classify a ticker's local weather direction.

    Returns 'up', 'down', or 'flat'. 'flat' is the data-missing /
    mixed-signal default \u2014 the dashboard renders it as a neutral
    em-dash glyph.

    The rule mirrors the override rule but is direction-agnostic: we
    return whichever side has both a price-structure leg (EMA9 OR AVWAP
    OR OR-break) AND DI confirmation. v6.2.0 \u2014 OR-break leg is
    optional; callers that omit or_high/or_low/atr_pm get the legacy
    two-leg behaviour.
    """
    long_ok = _check_direction(
        SIDE_LONG,
        ticker_5m_close,
        ticker_5m_ema9,
        ticker_last,
        ticker_avwap,
        di_plus_1m,
        di_minus_1m,
        or_high=or_high,
        or_low=or_low,
        atr_pm=atr_pm,
    )
    short_ok = _check_direction(
        SIDE_SHORT,
        ticker_5m_close,
        ticker_5m_ema9,
        ticker_last,
        ticker_avwap,
        di_plus_1m,
        di_minus_1m,
        or_high=or_high,
        or_low=or_low,
        atr_pm=atr_pm,
    )
    if long_ok and not short_ok:
        return WEATHER_UP
    if short_ok and not long_ok:
        return WEATHER_DOWN
    return WEATHER_FLAT


# v6.2.0 \u2014 OR-break leg constants. Default ON; flip to disable the
# new leg without removing code. K matches V610_OR_BREAK_K (0.25) so the
# behaviour is consistent with the existing v6.1.0 ATR OR-break path.
V620_LOCAL_OR_BREAK_ENABLED: bool = True
V620_LOCAL_OR_BREAK_K: float = 0.25


def _or_break_leg(
    side: str,
    last: Optional[float],
    or_high: Optional[float],
    or_low: Optional[float],
    atr_pm: Optional[float],
) -> bool:
    """v6.2.0 OR-break leg. True when the ticker's last price has
    cleared the opening range by ``V620_LOCAL_OR_BREAK_K * atr_pm`` on
    the side-aligned edge. Any None input \u2014 or a non-positive ATR
    \u2014 collapses to False.
    """
    if not V620_LOCAL_OR_BREAK_ENABLED:
        return False
    if last is None or atr_pm is None or atr_pm <= 0:
        return False
    if side == SIDE_LONG:
        if or_high is None:
            return False
        return last > (or_high + V620_LOCAL_OR_BREAK_K * atr_pm)
    if side == SIDE_SHORT:
        if or_low is None:
            return False
        return last < (or_low - V620_LOCAL_OR_BREAK_K * atr_pm)
    return False


def _check_direction(
    side: str,
    close_5m: Optional[float],
    ema9_5m: Optional[float],
    last: Optional[float],
    avwap: Optional[float],
    di_plus_1m: Optional[float],
    di_minus_1m: Optional[float],
    or_high: Optional[float] = None,
    or_low: Optional[float] = None,
    atr_pm: Optional[float] = None,
) -> bool:
    """Return True if local weather is aligned with `side`.

    Loose: (close past EMA9 OR last past AVWAP OR OR-break) AND DI
    confirms. Any None input on a structure leg degrades to False for
    that leg. DI confirmation requires BOTH 1m DI values present.
    """
    if side not in (SIDE_LONG, SIDE_SHORT):
        return False
    if di_plus_1m is None or di_minus_1m is None:
        return False
    if side == SIDE_LONG:
        ema_leg = (
            close_5m is not None
            and ema9_5m is not None
            and close_5m > ema9_5m
        )
        avwap_leg = (
            last is not None
            and avwap is not None
            and last > avwap
        )
        di_ok = di_plus_1m > di_minus_1m
    else:
        ema_leg = (
            close_5m is not None
            and ema9_5m is not None
            and close_5m < ema9_5m
        )
        avwap_leg = (
            last is not None
            and avwap is not None
            and last < avwap
        )
        di_ok = di_minus_1m > di_plus_1m
    or_break = _or_break_leg(side, last, or_high, or_low, atr_pm)
    return (ema_leg or avwap_leg or or_break) and di_ok


def evaluate_local_override(
    side: str,
    ticker_5m_close: Optional[float],
    ticker_5m_ema9: Optional[float],
    ticker_last: Optional[float],
    ticker_avwap: Optional[float],
    di_plus_1m: Optional[float],
    di_minus_1m: Optional[float],
    or_high: Optional[float] = None,
    or_low: Optional[float] = None,
    atr_pm: Optional[float] = None,
) -> dict:
    """Evaluate the per-stock override for a given side.

    Called AFTER the global QQQ permit has rejected `side`. If this
    returns {open: True}, the entry gate proceeds. Otherwise the
    rejection from Section I stands.

    v6.2.0 \u2014 callers may pass or_high/or_low/atr_pm to enable the
    OR-break leg; legacy callers omitting these stay on the original
    two-leg behaviour.

    The returned dict is also surfaced to the dashboard's per-stock
    weather card so the operator can see why an override fired (or
    didn't) without combing the logs.
    """
    direction = classify_local_weather(
        ticker_5m_close,
        ticker_5m_ema9,
        ticker_last,
        ticker_avwap,
        di_plus_1m,
        di_minus_1m,
        or_high=or_high,
        or_low=or_low,
        atr_pm=atr_pm,
    )
    if side not in (SIDE_LONG, SIDE_SHORT):
        return {
            "open": False,
            "reason": "bad_side:%s" % side,
            "weather_direction": direction,
            "ema9_aligned": None,
            "avwap_aligned": None,
            "di_aligned": None,
        }
    if di_plus_1m is None or di_minus_1m is None:
        return {
            "open": False,
            "reason": "data_missing",
            "weather_direction": direction,
            "ema9_aligned": None,
            "avwap_aligned": None,
            "di_aligned": None,
        }
    if side == SIDE_LONG:
        ema_leg = (
            ticker_5m_close is not None
            and ticker_5m_ema9 is not None
            and ticker_5m_close > ticker_5m_ema9
        )
        avwap_leg = (
            ticker_last is not None
            and ticker_avwap is not None
            and ticker_last > ticker_avwap
        )
        di_ok = di_plus_1m > di_minus_1m
    else:
        ema_leg = (
            ticker_5m_close is not None
            and ticker_5m_ema9 is not None
            and ticker_5m_close < ticker_5m_ema9
        )
        avwap_leg = (
            ticker_last is not None
            and ticker_avwap is not None
            and ticker_last < ticker_avwap
        )
        di_ok = di_minus_1m > di_plus_1m
    or_break_leg = _or_break_leg(side, ticker_last, or_high, or_low, atr_pm)
    structure_ok = ema_leg or avwap_leg or or_break_leg
    if not structure_ok:
        return {
            "open": False,
            "reason": "structure_misaligned",
            "weather_direction": direction,
            "ema9_aligned": ema_leg,
            "avwap_aligned": avwap_leg,
            "or_break_aligned": or_break_leg,
            "di_aligned": di_ok,
        }
    if not di_ok:
        return {
            "open": False,
            "reason": "di_misaligned",
            "weather_direction": direction,
            "ema9_aligned": ema_leg,
            "avwap_aligned": avwap_leg,
            "or_break_aligned": or_break_leg,
            "di_aligned": di_ok,
        }
    # v6.2.0 \u2014 reason tags so we can audit which leg fired the override.
    if or_break_leg and not (ema_leg or avwap_leg):
        _open_reason = "open_or_break"
    else:
        _open_reason = "open"
    return {
        "open": True,
        "reason": _open_reason,
        "weather_direction": direction,
        "ema9_aligned": ema_leg,
        "avwap_aligned": avwap_leg,
        "or_break_aligned": or_break_leg,
        "di_aligned": di_ok,
    }


__all__ = [
    "classify_local_weather",
    "evaluate_local_override",
    "SIDE_LONG",
    "SIDE_SHORT",
    "WEATHER_UP",
    "WEATHER_DOWN",
    "WEATHER_FLAT",
]
