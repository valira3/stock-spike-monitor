"""v7.2.0 \u2014 earnings_watcher.signals_pmc: Post-Market Continuation signal.

Strategy summary
----------------
Wait phase    : 16:00\u201316:15 ET (20:00\u201320:15 UTC) \u2014 ignore initial print noise.
Build phase   : 16:15\u201316:30 ET (20:15\u201320:30 UTC) \u2014 track 15-min post-print
                range and per-minute volume.
Freeze        : at 16:30 ET (20:30 UTC) capture range_high, range_low, range_width.
Quality gates : range_width / range_low >= PMC_MIN_RANGE_PCT,
                range_width / atr_5min >= 1.0,
                at least PMC_MIN_BUILD_BARS bars in build phase.
Scan phase    : 16:30\u201319:55 ET (20:30\u201323:55 UTC) \u2014 fire on volume-confirmed
                break of frozen range.
Hard exit     : 19:55 ET enforced by exit policy and existing session_end logic.

Hard boundary: MUST NOT import from eye_of_tiger or trade_genius.
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


# UTC minute boundaries (16:00 ET == 20:00 UTC == 1200).
PMC_PRINT_OPEN_UTC_MIN = _i("PMC_PRINT_OPEN_UTC_MIN", 20 * 60)
PMC_BUILD_START_UTC_MIN = _i("PMC_BUILD_START_UTC_MIN", 20 * 60 + 15)
PMC_BUILD_END_UTC_MIN = _i("PMC_BUILD_END_UTC_MIN", 20 * 60 + 30)
PMC_HARD_EXIT_UTC_MIN = _i("PMC_HARD_EXIT_UTC_MIN", 23 * 60 + 55)

PMC_VOLUME_MULT = _f("PMC_VOLUME_MULT", 1.5)
PMC_MIN_RANGE_PCT = _f("PMC_MIN_RANGE_PCT", 0.01)
PMC_MIN_BUILD_BARS = _i("PMC_MIN_BUILD_BARS", 5)

PMC_BASE_NOTIONAL_PCT = _f("PMC_BASE_NOTIONAL_PCT", 0.07)
PMC_CONVICTION_SIZE_MAX = _f("PMC_CONVICTION_SIZE_MAX", 2.5)


def _bar_utc_min(bar: Dict[str, Any]) -> int:
    ts = bar.get("timestamp", "")
    if len(ts) < 16:
        return -1
    return int(ts[11:13]) * 60 + int(ts[14:16])


def _atr_5min(bars: List[Dict[str, Any]], period: int = 14) -> Optional[float]:
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


def pmc_range_quality_score(range_width: float, range_low: float) -> float:
    """conv = range_width / range_low * 100, capped at 8."""
    if range_low <= 0:
        return 0.0
    pct = (range_width / range_low) * 100.0
    return max(0.0, min(pct, 8.0))


def pmc_sized_notional(
    equity: Optional[float],
    conviction: float,
    open_exposure: float,
) -> tuple:
    if equity is None or equity <= 0:
        return 0.0, "no_equity"

    if conviction < 2.0:
        size_mult = 1.0
    else:
        size_mult = min(
            1.0 + (conviction - 2.0) / 6.0 * (PMC_CONVICTION_SIZE_MAX - 1.0),
            PMC_CONVICTION_SIZE_MAX,
        )

    proposed = equity * PMC_BASE_NOTIONAL_PCT * size_mult
    proposed = min(proposed, equity * DMI_MAX_POSITION_PCT)

    max_total = equity * DMI_MAX_PORTFOLIO_EXPOSURE_PCT
    if open_exposure + proposed > max_total:
        scaled = max(0.0, max_total - open_exposure)
        if scaled < equity * DMI_MIN_POSITION_PCT:
            return 0.0, "exposure_minimal"
        return scaled, "exposure_cap"

    return proposed, "ok"


def _build_phase_bars(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for b in bars:
        m = _bar_utc_min(b)
        if PMC_BUILD_START_UTC_MIN <= m < PMC_BUILD_END_UTC_MIN:
            out.append(b)
    return out


def _scan_phase_bars(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for b in bars:
        m = _bar_utc_min(b)
        if PMC_BUILD_END_UTC_MIN <= m <= PMC_HARD_EXIT_UTC_MIN:
            out.append(b)
    return out


def find_pmc_breakout(bars: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """First volume-confirmed break of the 15-min post-print range."""
    if not bars:
        return None

    build_bars = _build_phase_bars(bars)
    if len(build_bars) < PMC_MIN_BUILD_BARS:
        return None

    range_high = max(float(b["high"]) for b in build_bars)
    range_low = min(float(b["low"]) for b in build_bars)
    range_width = range_high - range_low
    if range_low <= 0:
        return None
    if (range_width / range_low) < PMC_MIN_RANGE_PCT:
        return None

    pre_volumes = [float(b.get("volume", 0)) for b in build_bars]
    pre_vol_avg = sum(pre_volumes) / len(pre_volumes) if pre_volumes else 0.0
    if pre_vol_avg <= 0:
        return None

    atr5 = _atr_5min(build_bars, period=min(14, len(build_bars) - 1))
    if atr5 is None or atr5 <= 0:
        return None
    if range_width < atr5:
        return None

    scan_bars = _scan_phase_bars(bars)
    for idx, b in enumerate(scan_bars):
        cl = float(b["close"])
        vol = float(b.get("volume", 0))
        if vol < pre_vol_avg * PMC_VOLUME_MULT:
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


def evaluate_and_size_pmc(
    equity: Optional[float],
    ticker: str,
    bars: List[Dict[str, Any]],
    event_meta: Dict[str, Any],
    open_exposure: float,
) -> Optional[Dict[str, Any]]:
    bo = find_pmc_breakout(bars)
    if bo is None:
        return None

    conv = pmc_range_quality_score(bo["range_width"], bo["range_low"])
    notional, reason = pmc_sized_notional(equity, conv, open_exposure)
    if notional <= 0:
        logger.debug("[EW-PMC] sized_zero ticker=%s reason=%s conv=%.2f",
                     ticker, reason, conv)
        return None

    limit_price = float(bo["entry_px"])
    qty = int(notional / limit_price) if limit_price > 0 else 0
    if qty <= 0:
        logger.debug("[EW-PMC] zero_qty ticker=%s notional=%.2f price=%.4f",
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
        "strategy": "pmc",
        "exit_policy": "atr_trail",
        "session": "amc",
    }
    logger.info(
        "[EW-PMC] signal ticker=%s side=%s notional=%.0f qty=%d limit=%.4f "
        "range=%.4f-%.4f conv=%.2f vol_ratio=%.2fx",
        ticker, side, notional, qty, limit_price,
        bo["range_low"], bo["range_high"], conv, bo["vol_ratio"],
    )
    return intent


def classify_pmc_skip(bars: List[Dict[str, Any]]) -> tuple:
    if not bars:
        return ("retry", "no_bars")
    build_bars = _build_phase_bars(bars)
    if len(build_bars) < PMC_MIN_BUILD_BARS:
        return ("retry", "build_phase_incomplete")

    range_high = max(float(b["high"]) for b in build_bars)
    range_low = min(float(b["low"]) for b in build_bars)
    if range_low <= 0:
        return ("terminal", "invalid_range")
    range_width = range_high - range_low
    if (range_width / range_low) < PMC_MIN_RANGE_PCT:
        return ("terminal", "range_too_narrow")

    atr5 = _atr_5min(build_bars, period=min(14, len(build_bars) - 1))
    if atr5 is None or atr5 <= 0:
        return ("terminal", "atr_unavailable")
    if range_width < atr5:
        return ("terminal", "range_below_atr")

    scan_bars = _scan_phase_bars(bars)
    if not scan_bars:
        return ("retry", "scan_phase_empty")
    return ("retry", "no_break_yet")
