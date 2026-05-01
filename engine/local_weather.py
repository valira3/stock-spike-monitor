"""v5.31.5 \u2014 Per-Stock Local Weather override.

When the global QQQ permit (Section I) is closed for a side, but the
ticker's own price action shows a clean directional move, we may
override the global block. This is the "local weather" gate.

Loose rule (chosen by Val 2026-05-01):

    (5m_close past EMA9 OR price past opening AVWAP) AND DI confirms

For a LONG override:
    (ticker_5m_close > ticker_5m_ema9 OR ticker_last > ticker_avwap)
    AND di_plus_1m > di_minus_1m

For a SHORT override:
    (ticker_5m_close < ticker_5m_ema9 OR ticker_last < ticker_avwap)
    AND di_minus_1m > di_plus_1m

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
) -> str:
    """Classify a ticker's local weather direction.

    Returns 'up', 'down', or 'flat'. 'flat' is the data-missing /
    mixed-signal default \u2014 the dashboard renders it as a neutral
    em-dash glyph.

    The rule mirrors the override rule but is direction-agnostic: we
    return whichever side has both a price-structure leg (EMA9 OR AVWAP)
    AND DI confirmation. If both legs disagree, we fall back to flat.
    """
    long_ok = _check_direction(
        SIDE_LONG,
        ticker_5m_close,
        ticker_5m_ema9,
        ticker_last,
        ticker_avwap,
        di_plus_1m,
        di_minus_1m,
    )
    short_ok = _check_direction(
        SIDE_SHORT,
        ticker_5m_close,
        ticker_5m_ema9,
        ticker_last,
        ticker_avwap,
        di_plus_1m,
        di_minus_1m,
    )
    if long_ok and not short_ok:
        return WEATHER_UP
    if short_ok and not long_ok:
        return WEATHER_DOWN
    return WEATHER_FLAT


def _check_direction(
    side: str,
    close_5m: Optional[float],
    ema9_5m: Optional[float],
    last: Optional[float],
    avwap: Optional[float],
    di_plus_1m: Optional[float],
    di_minus_1m: Optional[float],
) -> bool:
    """Return True if local weather is aligned with `side`.

    Loose: (close past EMA9 OR last past AVWAP) AND DI confirms.
    Any None input on the structure leg degrades to False for that
    leg. DI confirmation requires BOTH 1m DI values present.
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
    return (ema_leg or avwap_leg) and di_ok


def evaluate_local_override(
    side: str,
    ticker_5m_close: Optional[float],
    ticker_5m_ema9: Optional[float],
    ticker_last: Optional[float],
    ticker_avwap: Optional[float],
    di_plus_1m: Optional[float],
    di_minus_1m: Optional[float],
) -> dict:
    """Evaluate the per-stock override for a given side.

    Called AFTER the global QQQ permit has rejected `side`. If this
    returns {open: True}, the entry gate proceeds. Otherwise the
    rejection from Section I stands.

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
    structure_ok = ema_leg or avwap_leg
    if not structure_ok:
        return {
            "open": False,
            "reason": "structure_misaligned",
            "weather_direction": direction,
            "ema9_aligned": ema_leg,
            "avwap_aligned": avwap_leg,
            "di_aligned": di_ok,
        }
    if not di_ok:
        return {
            "open": False,
            "reason": "di_misaligned",
            "weather_direction": direction,
            "ema9_aligned": ema_leg,
            "avwap_aligned": avwap_leg,
            "di_aligned": di_ok,
        }
    return {
        "open": True,
        "reason": "open",
        "weather_direction": direction,
        "ema9_aligned": ema_leg,
        "avwap_aligned": avwap_leg,
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
