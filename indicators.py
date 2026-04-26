"""Pure indicator functions used by v5.1.2 candidate snapshots.

Each function takes a list of bars (newest last) and returns a float, or
None if there are not enough bars to compute the indicator. Callers MUST
treat None as "insufficient data" and emit `null` (not `0.0`) into log
lines.

Bars are dicts with at least `close` (float). ATR additionally requires
`high`, `low`. VWAP additionally requires `high`, `low`, `close`,
`volume`. None of these functions raise on bad input \u2014 they return
None instead.
"""
from __future__ import annotations

from typing import Sequence


def rsi14(closes: Sequence[float]) -> float | None:
    """14-period Wilder RSI on `closes`. Needs >= 15 closes.

    Returns rounded-to-4-decimals float, or None if insufficient bars.
    """
    if closes is None or len(closes) < 15:
        return None
    try:
        gains: list[float] = []
        losses: list[float] = []
        for i in range(1, 15):
            d = float(closes[i]) - float(closes[i - 1])
            gains.append(d if d > 0 else 0.0)
            losses.append(-d if d < 0 else 0.0)
        avg_g = sum(gains) / 14.0
        avg_l = sum(losses) / 14.0
        for i in range(15, len(closes)):
            d = float(closes[i]) - float(closes[i - 1])
            g = d if d > 0 else 0.0
            l = -d if d < 0 else 0.0
            avg_g = (avg_g * 13.0 + g) / 14.0
            avg_l = (avg_l * 13.0 + l) / 14.0
        if avg_l == 0.0:
            return 100.0
        rs = avg_g / avg_l
        return round(100.0 - (100.0 / (1.0 + rs)), 4)
    except (TypeError, ValueError, IndexError):
        return None


def ema(closes: Sequence[float], period: int) -> float | None:
    """Exponential moving average. Returns None if fewer than `period` bars."""
    if closes is None or period <= 0 or len(closes) < period:
        return None
    try:
        k = 2.0 / (period + 1.0)
        seed = sum(float(c) for c in closes[:period]) / period
        v = seed
        for c in closes[period:]:
            v = float(c) * k + v * (1.0 - k)
        return round(v, 4)
    except (TypeError, ValueError):
        return None


def ema9(closes: Sequence[float]) -> float | None:
    return ema(closes, 9)


def ema21(closes: Sequence[float]) -> float | None:
    return ema(closes, 21)


def atr14(bars: Sequence[dict]) -> float | None:
    """14-period Wilder ATR. Bars need `high`, `low`, `close`. Needs >= 15 bars.

    True range = max(high-low, |high-prev_close|, |low-prev_close|).
    """
    if bars is None or len(bars) < 15:
        return None
    try:
        trs: list[float] = []
        for i in range(1, len(bars)):
            h = float(bars[i]["high"])
            l = float(bars[i]["low"])
            pc = float(bars[i - 1]["close"])
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
        if len(trs) < 14:
            return None
        atr = sum(trs[:14]) / 14.0
        for tr in trs[14:]:
            atr = (atr * 13.0 + tr) / 14.0
        return round(atr, 4)
    except (TypeError, ValueError, KeyError):
        return None


def vwap_dist_pct(bars: Sequence[dict]) -> float | None:
    """% distance of last close from session VWAP, computed from the bar
    list (assumes session-bounded). Bars need `high`, `low`, `close`,
    `volume`. Returns None if no bars or zero volume.
    """
    if bars is None or len(bars) == 0:
        return None
    try:
        num = 0.0
        den = 0.0
        for b in bars:
            tp = (float(b["high"]) + float(b["low"]) + float(b["close"])) / 3.0
            v = float(b["volume"])
            num += tp * v
            den += v
        if den <= 0.0:
            return None
        vwap = num / den
        last_close = float(bars[-1]["close"])
        if vwap == 0.0:
            return None
        return round(((last_close - vwap) / vwap) * 100.0, 4)
    except (TypeError, ValueError, KeyError):
        return None


def _wilder_dx(bars: Sequence[dict], period: int = 14):
    """Compute (smoothed +DM, smoothed -DM, smoothed TR) using Wilder's
    RMA. Returns (None, None, None) if fewer than `period`+1 bars or on
    bad input.
    """
    if bars is None or period <= 0 or len(bars) < period + 1:
        return (None, None, None)
    try:
        plus_dms: list[float] = []
        minus_dms: list[float] = []
        trs: list[float] = []
        for i in range(1, len(bars)):
            h = float(bars[i]["high"])
            l = float(bars[i]["low"])
            ph = float(bars[i - 1]["high"])
            pl = float(bars[i - 1]["low"])
            pc = float(bars[i - 1]["close"])
            up = h - ph
            dn = pl - l
            plus_dm = up if (up > dn and up > 0) else 0.0
            minus_dm = dn if (dn > up and dn > 0) else 0.0
            tr = max(h - l, abs(h - pc), abs(l - pc))
            plus_dms.append(plus_dm)
            minus_dms.append(minus_dm)
            trs.append(tr)
        if len(trs) < period:
            return (None, None, None)
        sp = sum(plus_dms[:period])
        sm = sum(minus_dms[:period])
        st = sum(trs[:period])
        for i in range(period, len(trs)):
            sp = (sp * (period - 1) + plus_dms[i]) / period
            sm = (sm * (period - 1) + minus_dms[i]) / period
            st = (st * (period - 1) + trs[i]) / period
        return (sp, sm, st)
    except (TypeError, ValueError, KeyError):
        return (None, None, None)


def di_plus(bars: Sequence[dict], period: int = 14) -> float | None:
    """Wilder's +DI over `period` bars. Bars need `high`, `low`, `close`.
    Needs >= period+1 bars. Returns float in [0, 100] or None.
    """
    sp, _sm, st = _wilder_dx(bars, period=period)
    if sp is None or st is None or st <= 0.0:
        return None
    return round(100.0 * (sp / st), 4)


def di_minus(bars: Sequence[dict], period: int = 14) -> float | None:
    """Wilder's -DI over `period` bars. Bars need `high`, `low`, `close`.
    Needs >= period+1 bars. Returns float in [0, 100] or None.
    """
    _sp, sm, st = _wilder_dx(bars, period=period)
    if sm is None or st is None or st <= 0.0:
        return None
    return round(100.0 * (sm / st), 4)


def spread_bps(bid: float | None, ask: float | None) -> float | None:
    """Bid/ask spread in basis points relative to the mid. Returns None
    on missing or non-positive inputs."""
    if bid is None or ask is None:
        return None
    try:
        b = float(bid)
        a = float(ask)
        if b <= 0.0 or a <= 0.0 or a < b:
            return None
        mid = (a + b) / 2.0
        if mid <= 0.0:
            return None
        return round(((a - b) / mid) * 10000.0, 4)
    except (TypeError, ValueError):
        return None
