"""v6.16.1 \u2014 earnings_watcher.signals: pure DMI signal functions.

Ported from earnings_watcher_spec/replay/decision_engine.py (Phase 0 v4
locked config). No I/O, no external imports beyond statistics stdlib.

Hard boundary: MUST NOT import from eye_of_tiger or trade_genius.
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# DMI constants (mirrors sizing.py risk fields + decision_engine.py signal cfg)
# ---------------------------------------------------------------------------

DMI_PERIOD = 10                 # Wilder period (10 fits 150-240 bar AH/PM windows)
DMI_DI_MIN = 30.0               # DI+ (long) or DI- (short) must clear this
DMI_ADX_MIN = 20.0              # ADX confirms trend has body, not just one bar
DMI_LOOKBACK = 10               # bars used to compute prior-high for NHOD test
DMI_VOL_MULT = 3.0              # breakout-bar volume vs trailing median
DMI_MIN_VOL = 100_000           # absolute floor on breakout-bar volume
DMI_LONG_ONLY = True            # short-side hit rate too low on Phase 0 corpus
DMI_MAX_ENTRY_IDX = 90          # skip late-session breakouts


# ---------------------------------------------------------------------------
# Wilder DMI (ADX / DI+ / DI-)
# ---------------------------------------------------------------------------

def wilder_dmi(bars: List[Dict[str, Any]], period: int = DMI_PERIOD) -> List[Optional[tuple]]:
    """Return list of (di_plus, di_minus, adx) per bar. None until warmup completes.

    Uses Wilder's RMA smoothing per the standard ADX/DMI definition.
    Identical to decision_engine.wilder_dmi \u2014 preserved for exact replay parity.
    """
    n = len(bars)
    out: List[Optional[tuple]] = [None] * n
    if n < period + 2:
        return out

    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up = bars[i]["high"] - bars[i - 1]["high"]
        dn = bars[i - 1]["low"] - bars[i]["low"]
        plus_dm[i] = up if (up > dn and up > 0) else 0.0
        minus_dm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(
            bars[i]["high"] - bars[i]["low"],
            abs(bars[i]["high"] - bars[i - 1]["close"]),
            abs(bars[i]["low"] - bars[i - 1]["close"]),
        )

    def rma(series: List[float]) -> List[Optional[float]]:
        smoothed: List[Optional[float]] = [None] * n
        if n < period + 1:
            return smoothed
        first = sum(series[1: period + 1])
        smoothed[period] = first
        for i in range(period + 1, n):
            smoothed[i] = smoothed[i - 1] - (smoothed[i - 1] / period) + series[i]  # type: ignore[operator]
        return smoothed

    s_pdm = rma(plus_dm)
    s_mdm = rma(minus_dm)
    s_tr = rma(tr)

    di_plus: List[Optional[float]] = [None] * n
    di_minus: List[Optional[float]] = [None] * n
    dx: List[Optional[float]] = [None] * n
    for i in range(n):
        if s_tr[i] is None or s_tr[i] == 0:
            continue
        di_plus[i] = 100.0 * s_pdm[i] / s_tr[i]  # type: ignore[operator]
        di_minus[i] = 100.0 * s_mdm[i] / s_tr[i]  # type: ignore[operator]
        denom = di_plus[i] + di_minus[i]  # type: ignore[operator]
        if denom > 0:
            dx[i] = 100.0 * abs(di_plus[i] - di_minus[i]) / denom  # type: ignore[operator]

    adx: List[Optional[float]] = [None] * n
    valid_dx = [(i, v) for i, v in enumerate(dx) if v is not None]
    if len(valid_dx) >= period:
        first_idx = valid_dx[period - 1][0]
        first_adx = sum(v for _, v in valid_dx[:period]) / period
        adx[first_idx] = first_adx
        prev = first_adx
        for j in range(period, len(valid_dx)):
            idx, v = valid_dx[j]
            new_adx = (prev * (period - 1) + v) / period
            adx[idx] = new_adx
            prev = new_adx

    for i in range(n):
        out[i] = (di_plus[i], di_minus[i], adx[i])
    return out


# ---------------------------------------------------------------------------
# NHOD/NLOD + DMI breakout detector
# ---------------------------------------------------------------------------

def find_nhod_dmi_breakout(
    sess_bars: List[Dict[str, Any]],
    *,
    dmi_period: int = DMI_PERIOD,
    lookback: int = DMI_LOOKBACK,
    vol_mult: float = DMI_VOL_MULT,
    min_vol: int = DMI_MIN_VOL,
    di_min: float = DMI_DI_MIN,
    adx_min: Optional[float] = DMI_ADX_MIN,
    long_only: bool = DMI_LONG_ONLY,
) -> Optional[Dict[str, Any]]:
    """Detect first NHOD/NLOD bar with sustained follow-through AND DMI confirmation.

    Returns dict with idx, direction, conviction, di_plus, di_minus, adx,
    entry_ts \u2014 or None if no qualifying bar found.

    Identical to decision_engine.find_nhod_dmi_breakout for replay parity.
    """
    if len(sess_bars) < max(lookback, dmi_period) + 3:
        return None

    dmi = wilder_dmi(sess_bars, period=dmi_period)

    for i in range(max(lookback, dmi_period + 1), len(sess_bars) - 1):
        prior = sess_bars[:i]
        b = sess_bars[i]
        nxt = sess_bars[i + 1]
        prior_high = max(p["high"] for p in prior)
        prior_low = min(p["low"] for p in prior)
        recent_vols = [p["volume"] for p in sess_bars[max(0, i - lookback): i]]
        med_vol = statistics.median(recent_vols) if recent_vols else 0
        d_plus, d_minus, adx = dmi[i] if dmi[i] else (None, None, None)
        if d_plus is None:
            continue
        adx_ok = (adx_min is None) or (adx is not None and adx >= adx_min)
        vol_ok = (b["volume"] >= vol_mult * max(med_vol, 1)) and (b["volume"] >= min_vol)

        if (
            b["high"] > prior_high
            and vol_ok
            and nxt["close"] > b["close"]
            and d_plus >= di_min
            and adx_ok
        ):
            return {
                "idx": i,
                "direction": "long",
                "conviction": b["volume"] / max(med_vol, 1),
                "di_plus": d_plus,
                "di_minus": d_minus,
                "adx": adx,
                "entry_ts": b["timestamp"],
            }

        if (not long_only) and (
            b["low"] < prior_low
            and vol_ok
            and nxt["close"] < b["close"]
            and d_minus >= di_min
            and adx_ok
        ):
            return {
                "idx": i,
                "direction": "short",
                "conviction": b["volume"] / max(med_vol, 1),
                "di_plus": d_plus,
                "di_minus": d_minus,
                "adx": adx,
                "entry_ts": b["timestamp"],
            }

    return None


# ---------------------------------------------------------------------------
# Quality score (EPS + revenue surprise)
# ---------------------------------------------------------------------------

def quality_score(event: Dict[str, Any]) -> Dict[str, Any]:
    """Score the report from FMP earnings calendar fields.

    Inputs (in event dict): epsActual, epsEstimated, revActual, revEstimated.

    Returns dict with score (int), bias ('bullish'/'bearish'/'neutral'),
    and components breakdown.

    Identical to decision_engine.quality_score for replay parity.
    """
    eps_a = event.get("epsActual")
    eps_e = event.get("epsEstimated")
    rev_a = event.get("revActual")
    rev_e = event.get("revEstimated")

    components: Dict[str, Any] = {}
    score = 0

    # +2 beat revenue (>1%)
    if rev_a is not None and rev_e is not None and rev_e > 0:
        rev_surp = (rev_a - rev_e) / rev_e
        components["rev_surp"] = round(rev_surp, 4)
        if rev_surp > 0.01:
            score += 2
            components["beat_revenue"] = True
        elif rev_surp < -0.01:
            score -= 2
            components["miss_revenue"] = True
    else:
        components["rev_surp"] = None

    # +2 beat EPS (>1%)
    if eps_a is not None and eps_e is not None:
        if eps_e > 0:
            eps_surp = (eps_a - eps_e) / abs(eps_e)
        elif eps_e < 0:
            eps_surp = (eps_a - eps_e) / abs(eps_e)
        else:
            eps_surp = 0
        components["eps_surp"] = round(eps_surp, 4)
        if eps_surp > 0.01:
            score += 2
            components["beat_eps"] = True
        elif eps_surp < -0.01:
            score -= 2
            components["miss_eps"] = True
    else:
        components["eps_surp"] = None

    # Phase 0 v1: guidance + margins not in our data; skip
    components["guidance_status"] = "not_available_v1"
    components["margin_status"] = "not_available_v1"

    if score >= 3:
        bias = "bullish"
    elif score <= -2:
        bias = "bearish"
    else:
        bias = "neutral"

    return {"score": score, "bias": bias, "components": components}


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def determine_session(bars: List[Dict[str, Any]]) -> str:
    """Decide AMC vs BMO from where the volume actually lives.

    Returns 'amc' if >70% of total volume in 19:00-00:00 UTC;
    'bmo' if >70% in 08:00-13:30 UTC; 'mixed' otherwise.
    """
    if not bars:
        return "unknown"
    amc_vol = bmo_vol = 0
    for b in bars:
        h = int(b["timestamp"][11:13])
        v = b.get("volume", 0)
        if 19 <= h or h < 1:
            amc_vol += v
        elif 8 <= h < 14:
            bmo_vol += v
    total = amc_vol + bmo_vol
    if total == 0:
        return "unknown"
    if amc_vol / total > 0.7:
        return "amc"
    if bmo_vol / total > 0.7:
        return "bmo"
    return "mixed"


def filter_bars_for_session(
    bars: List[Dict[str, Any]], session: str
) -> List[Dict[str, Any]]:
    """Filter bars to the working session window.

    AMC: 19:00 UTC -> 23:55 UTC
    BMO: 08:00 UTC -> 13:25 UTC
    """
    out = []
    for b in bars:
        h = int(b["timestamp"][11:13])
        m = int(b["timestamp"][14:16])
        mins = h * 60 + m
        if session == "amc":
            if (19 * 60) <= mins <= (23 * 60 + 55):
                out.append(b)
        elif session == "bmo":
            if (8 * 60) <= mins <= (13 * 60 + 25):
                out.append(b)
    return out
