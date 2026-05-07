"""v7.2.0 \u2014 earnings_watcher.signals_pmr: Pre-Market Range Break signal.

Strategy summary
----------------
Build phase  : 04:00\u201308:00 ET (08:00\u201312:00 UTC) \u2014 track running pre-market
               high/low and average per-minute volume.
Freeze       : at 08:00 ET (12:00 UTC) capture range_high, range_low, range_width.
Quality gates: range_width / range_low >= PMR_MIN_RANGE_PCT,
               range_width / atr_5min >= 1.0,
               at least PMR_MIN_BUILD_BARS bars in build phase.
Scan phase   : 08:00\u201309:25 ET (12:00\u201313:25 UTC) \u2014 fire on volume-confirmed
               break of frozen range.
Hard exit    : 09:25 ET enforced by exit policy (atr_trail) and the existing
               session_end logic in earnings_watcher.exits.

Hard boundary: MUST NOT import from eye_of_tiger or trade_genius. Reuses
sizing helpers from earnings_watcher.sizing for portfolio-relative notional
math.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from earnings_watcher.sizing import (
    DMI_MAX_PORTFOLIO_EXPOSURE_PCT,
    DMI_MAX_POSITION_PCT,
    DMI_MIN_POSITION_PCT,
)

logger = logging.getLogger("earnings_watcher")


# ---------------------------------------------------------------------------
# Tunables (env-overridable)
# ---------------------------------------------------------------------------

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


# Build phase boundary (UTC minutes): 04:00 ET == 08:00 UTC == 480.
PMR_BUILD_START_UTC_MIN = _i("PMR_BUILD_START_UTC_MIN", 8 * 60)
# Freeze (== scan start): 08:00 ET == 12:00 UTC == 720.
PMR_RANGE_FREEZE_UTC_MIN = _i("PMR_RANGE_FREEZE_UTC_MIN", 12 * 60)
# Hard exit (== scan end): 09:25 ET == 13:25 UTC == 805.
PMR_HARD_EXIT_UTC_MIN = _i("PMR_HARD_EXIT_UTC_MIN", 13 * 60 + 25)

PMR_VOLUME_MULT = _f("PMR_VOLUME_MULT", 1.5)
PMR_MIN_RANGE_PCT = _f("PMR_MIN_RANGE_PCT", 0.005)
PMR_MIN_BUILD_BARS = _i("PMR_MIN_BUILD_BARS", 5)

PMR_BASE_NOTIONAL_PCT = _f("PMR_BASE_NOTIONAL_PCT", 0.05)
PMR_CONVICTION_SIZE_MAX = _f("PMR_CONVICTION_SIZE_MAX", 2.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bar_utc_min(bar: Dict[str, Any]) -> int:
    """Return minutes-from-UTC-midnight for a bar timestamp like 2026-05-07T12:34:00Z."""
    ts = bar.get("timestamp", "")
    if len(ts) < 16:
        return -1
    return int(ts[11:13]) * 60 + int(ts[14:16])


def _atr_5min(bars: List[Dict[str, Any]], period: int = 14) -> Optional[float]:
    """Wilder-style ATR over the most recent `period` bars.

    Bars must contain high, low, close. Returns None if fewer than period+1 bars.
    """
    if len(bars) < period + 1:
        return None
    trs = []
    prev_close = float(bars[-period - 1]["close"])
    for b in bars[-period:]:
        h = float(b["high"])
        lo = float(b["low"])
        cl = float(b["close"])
        tr = max(h - lo, abs(h - prev_close), abs(lo - prev_close))
        trs.append(tr)
        prev_close = cl
    return sum(trs) / period


def pmr_range_quality_score(
    range_width: float,
    range_low: float,
    atr5: Optional[float],
) -> float:
    """Conviction score: range as % of price (capped at 5).

    Mirrors the spec: conv = (range_width / range_low) * 100, capped at 5.
    Higher = more meaningful pre-market action.
    """
    if range_low <= 0:
        return 0.0
    pct = (range_width / range_low) * 100.0
    return max(0.0, min(pct, 5.0))


def pmr_sized_notional(
    equity: Optional[float],
    conviction: float,
    open_exposure: float,
) -> tuple:
    """PMR portfolio-relative notional sizing.

    Mirrors dmi_sized_notional but uses PMR base/cap constants. Returns
    (notional, reason) where reason is one of: ok, exposure_cap,
    exposure_minimal, no_equity.
    """
    if equity is None or equity <= 0:
        return 0.0, "no_equity"

    # Piecewise: 1x at conv<2, then linear up to PMR_CONVICTION_SIZE_MAX at conv>=5.
    if conviction < 2.0:
        size_mult = 1.0
    else:
        size_mult = min(
            1.0 + (conviction - 2.0) / 3.0 * (PMR_CONVICTION_SIZE_MAX - 1.0),
            PMR_CONVICTION_SIZE_MAX,
        )

    proposed = equity * PMR_BASE_NOTIONAL_PCT * size_mult
    proposed = min(proposed, equity * DMI_MAX_POSITION_PCT)

    max_total = equity * DMI_MAX_PORTFOLIO_EXPOSURE_PCT
    if open_exposure + proposed > max_total:
        scaled = max(0.0, max_total - open_exposure)
        if scaled < equity * DMI_MIN_POSITION_PCT:
            return 0.0, "exposure_minimal"
        return scaled, "exposure_cap"

    return proposed, "ok"


# ---------------------------------------------------------------------------
# Build / freeze / scan
# ---------------------------------------------------------------------------

def _build_phase_bars(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Bars between 04:00 ET (build start) and 08:00 ET (freeze) inclusive."""
    out = []
    for b in bars:
        m = _bar_utc_min(b)
        if PMR_BUILD_START_UTC_MIN <= m < PMR_RANGE_FREEZE_UTC_MIN:
            out.append(b)
    return out


def _scan_phase_bars(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Bars between 08:00 ET (freeze) and 09:25 ET (hard exit)."""
    out = []
    for b in bars:
        m = _bar_utc_min(b)
        if PMR_RANGE_FREEZE_UTC_MIN <= m <= PMR_HARD_EXIT_UTC_MIN:
            out.append(b)
    return out


def find_pmr_breakout(bars: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Scan for the first volume-confirmed break of the frozen pre-market range.

    Parameters
    ----------
    bars : list of dict
        Minute bars from 04:00 ET onward (timestamps in UTC). Must include
        at least the build phase. Bars after 09:25 ET are ignored.

    Returns
    -------
    dict or None
        On signal:
          {direction, entry_idx, entry_ts, entry_px, range_high, range_low,
           range_width, vol_ratio, atr_5min}
        None when no signal (range invalid, no break, or insufficient data).
    """
    if not bars:
        return None

    build_bars = _build_phase_bars(bars)
    if len(build_bars) < PMR_MIN_BUILD_BARS:
        return None

    range_high = max(float(b["high"]) for b in build_bars)
    range_low = min(float(b["low"]) for b in build_bars)
    range_width = range_high - range_low
    if range_low <= 0:
        return None
    if (range_width / range_low) < PMR_MIN_RANGE_PCT:
        return None

    pre_volumes = [float(b.get("volume", 0)) for b in build_bars]
    pre_vol_avg = sum(pre_volumes) / len(pre_volumes) if pre_volumes else 0.0
    if pre_vol_avg <= 0:
        return None

    # ATR over the build phase (final 14 bars). Range must be >= 1x ATR.
    atr5 = _atr_5min(build_bars, period=14)
    if atr5 is None or atr5 <= 0:
        return None
    if range_width < atr5:
        return None

    scan_bars = _scan_phase_bars(bars)
    for idx, b in enumerate(scan_bars):
        cl = float(b["close"])
        vol = float(b.get("volume", 0))
        if vol < pre_vol_avg * PMR_VOLUME_MULT:
            continue
        if cl > range_high:
            return {
                "direction": "long",
                "entry_idx": idx,
                "entry_ts": b.get("timestamp", ""),
                "entry_px": cl,
                "range_high": range_high,
                "range_low": range_low,
                "range_width": range_width,
                "vol_ratio": vol / pre_vol_avg,
                "atr_5min": atr5,
            }
        if cl < range_low:
            return {
                "direction": "short",
                "entry_idx": idx,
                "entry_ts": b.get("timestamp", ""),
                "entry_px": cl,
                "range_high": range_high,
                "range_low": range_low,
                "range_width": range_width,
                "vol_ratio": vol / pre_vol_avg,
                "atr_5min": atr5,
            }
    return None


# ---------------------------------------------------------------------------
# evaluate_and_size_pmr (runner-compatible interface)
# ---------------------------------------------------------------------------

def evaluate_and_size_pmr(
    equity: Optional[float],
    ticker: str,
    bars: List[Dict[str, Any]],
    event_meta: Dict[str, Any],
    open_exposure: float,
) -> Optional[Dict[str, Any]]:
    """Top-level PMR evaluation. Returns an Intent dict or None.

    Intent schema (compatible with submit_dmi_order, with strategy/exit_policy
    additions):
      {
        ticker, side, qty, limit_price, notional, conv, vol_ratio, atr_5min,
        range_high, range_low, range_width, direction, entry_ts,
        strategy="pmr", exit_policy="atr_trail", session="bmo",
      }
    """
    bo = find_pmr_breakout(bars)
    if bo is None:
        return None

    conv = pmr_range_quality_score(bo["range_width"], bo["range_low"], bo["atr_5min"])
    notional, reason = pmr_sized_notional(equity, conv, open_exposure)
    if notional <= 0:
        logger.debug("[EW-PMR] sized_zero ticker=%s reason=%s conv=%.2f",
                     ticker, reason, conv)
        return None

    limit_price = float(bo["entry_px"])
    qty = int(notional / limit_price) if limit_price > 0 else 0
    if qty <= 0:
        logger.debug("[EW-PMR] zero_qty ticker=%s notional=%.2f price=%.4f",
                     ticker, notional, limit_price)
        return None

    direction = bo["direction"]
    side = "BUY" if direction == "long" else "SELL"

    intent = {
        "ticker": ticker,
        "side": side,
        "qty": qty,
        "limit_price": round(limit_price, 4),
        "notional": round(notional, 2),
        "conv": round(conv, 4),
        "vol_ratio": round(bo["vol_ratio"], 2),
        "atr_5min": round(bo["atr_5min"], 4),
        "range_high": round(bo["range_high"], 4),
        "range_low": round(bo["range_low"], 4),
        "range_width": round(bo["range_width"], 4),
        "direction": direction,
        "entry_ts": bo["entry_ts"],
        "reason": reason,
        "strategy": "pmr",
        "exit_policy": "atr_trail",
        "session": "bmo",
    }
    logger.info(
        "[EW-PMR] signal ticker=%s side=%s notional=%.0f qty=%d limit=%.4f "
        "range=%.4f-%.4f conv=%.2f vol_ratio=%.2fx",
        ticker, side, notional, qty, limit_price,
        bo["range_low"], bo["range_high"], conv, bo["vol_ratio"],
    )
    return intent


# ---------------------------------------------------------------------------
# Skip classification (parallel to runner._classify_skip_reason for telemetry)
# ---------------------------------------------------------------------------

def classify_pmr_skip(
    bars: List[Dict[str, Any]],
) -> tuple:
    """Re-derive why find_pmr_breakout returned None. Returns (kind, reason)."""
    if not bars:
        return ("retry", "no_bars")
    build_bars = _build_phase_bars(bars)
    if len(build_bars) < PMR_MIN_BUILD_BARS:
        return ("retry", "build_phase_incomplete")

    range_high = max(float(b["high"]) for b in build_bars)
    range_low = min(float(b["low"]) for b in build_bars)
    if range_low <= 0:
        return ("terminal", "invalid_range")
    range_width = range_high - range_low
    if (range_width / range_low) < PMR_MIN_RANGE_PCT:
        return ("terminal", "range_too_narrow")

    atr5 = _atr_5min(build_bars, period=14)
    if atr5 is None or atr5 <= 0:
        return ("terminal", "atr_unavailable")
    if range_width < atr5:
        return ("terminal", "range_below_atr")

    scan_bars = _scan_phase_bars(bars)
    if not scan_bars:
        return ("retry", "scan_phase_empty")
    return ("retry", "no_break_yet")
