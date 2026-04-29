"""engine.volume_baseline — Tiger Sovereign Phase 2 volume gate (L-P2-S3 / S-P2-S3).

Provides the canonical accessor for the **55-day rolling per-minute average
volume** baseline that the Phase 2 entry-readiness gate consumes. The actual
multi-day archive sweep lives in ``volume_bucket.VolumeBucketBaseline`` (added
in v5.10.0); this module is the explicit, spec-named surface area introduced by
v5.13.0 PR 4 so the rule mapping in ``STRATEGY.md`` lines up 1:1 with code.

Spec rule:
    L-P2-S3 / S-P2-S3 — current 1m volume must be at least 100% of the rolling
    55-day per-minute baseline for the same minute-of-day (HH:MM ET).

Cold-start:
    If the bar archive holds fewer than 55 trading days for a symbol, callers
    receive ``None`` and emit a ``[VOLPROFILE]`` warning. Trading is NOT
    blocked — Unlimited Hunting requires the gate to pass-through during the
    archive warmup.
"""
from __future__ import annotations

import logging
from datetime import date, time
from functools import lru_cache
from typing import Optional

import volume_bucket as _vb
from engine import feature_flags as _ff

logger = logging.getLogger(__name__)

# Spec literal — keep the explicit "55-day rolling minute baseline" string
# next to the constant so STRATEGY.md ↔ code grep stays aligned.
LOOKBACK_DAYS_55 = _vb.VOLUME_BUCKET_LOOKBACK_DAYS  # 55-day rolling minute baseline
THRESHOLD_RATIO = _vb.VOLUME_BUCKET_THRESHOLD_RATIO  # 1.00 (== 100%)

_BASELINE_SINGLETON: Optional[_vb.VolumeBucketBaseline] = None


def _baseline() -> _vb.VolumeBucketBaseline:
    """Lazy singleton for the per-process 55-day baseline cache.

    The underlying baseline already deduplicates per-(symbol, minute) reads
    inside its own dict. We just wrap construction so callers don't need to
    know about the storage layer.
    """
    global _BASELINE_SINGLETON
    if _BASELINE_SINGLETON is None:
        _BASELINE_SINGLETON = _vb.VolumeBucketBaseline()
    return _BASELINE_SINGLETON


def reset_cache() -> None:
    """Drop the cached baseline. Used by tests; not by the live loop."""
    global _BASELINE_SINGLETON
    _BASELINE_SINGLETON = None
    rolling_55d_per_minute_avg.cache_clear()


def _hhmm(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


@lru_cache(maxsize=4096)
def rolling_55d_per_minute_avg(
    symbol: str,
    time_of_day: time,
    as_of: date,
) -> Optional[float]:
    """Return the 55-day rolling per-minute average volume for ``symbol``
    at ``time_of_day`` (RTH minute, ET) computed against the trailing 55
    trading days strictly before ``as_of``.

    Returns ``None`` (with a single ``[VOLPROFILE]`` warning per
    (symbol, minute, date) tuple) when the archive holds fewer than the
    required 55 trading days. Callers MUST treat ``None`` as "gate
    pass-through" so cold-start doesn't block trading.
    """
    sym = (symbol or "").upper()
    if not sym:
        return None
    bb = _baseline()
    # Force a refresh aligned to ``as_of`` so unit tests can pin the
    # baseline date deterministically.
    try:
        bb.refresh(today=as_of)
    except Exception as e:  # archive missing entirely
        logger.warning("[VOLPROFILE] %s baseline refresh failed: %s", sym, e)
        return None

    # The baseline stores per-(symbol, HH:MM) averages keyed off the
    # archive sweep. Read it directly via .check() with current_volume=0
    # so the dict computation is performed and we get the baseline back.
    res = bb.check(sym, _hhmm(time_of_day), 0.0)
    days = int(res.get("days_available", 0) or 0)
    if days < LOOKBACK_DAYS_55:
        logger.warning(
            "[VOLPROFILE] %s @ %s — only %d/%d trading days available; "
            "gate pass-through (cold-start)",
            sym, _hhmm(time_of_day), days, LOOKBACK_DAYS_55,
        )
        return None
    base = res.get("baseline")
    if base is None:
        return None
    return float(base)


def gate_volume_pass(
    current_volume: float,
    baseline: Optional[float],
) -> tuple[bool, Optional[float]]:
    """Apply the L-P2-S3 / S-P2-S3 100% threshold.

    Returns ``(pass, ratio)``. ``baseline=None`` (cold-start) returns
    ``(True, None)`` so trading is not blocked while the archive warms up.
    Otherwise pass = current_volume / baseline >= 1.00.

    Runtime override: when ``feature_flags.VOLUME_GATE_ENABLED`` is False
    (the production default as of v5.13.1) the gate auto-passes with
    ``ratio=None`` and reason ``DISABLED_BY_FLAG``. The 2-consecutive-1m
    candle gate (L-P2-S4 / S-P2-S4) is unaffected.
    """
    if not _ff.VOLUME_GATE_ENABLED:
        return (True, None)
    if baseline is None or baseline <= 0:
        return (True, None)
    if current_volume is None or current_volume < 0:
        return (False, 0.0)
    ratio = float(current_volume) / float(baseline)
    return (ratio >= THRESHOLD_RATIO, ratio)


def gate_two_consecutive_1m_above(
    last_n_closes: list[float],
    or_high: Optional[float],
) -> bool:
    """L-P2-S4 — TWO consecutive completed 1m candles closed strictly ABOVE
    the 5m Opening Range High. The in-progress candle MUST NOT be counted;
    callers are responsible for only passing fully closed bars.
    """
    if or_high is None or not last_n_closes or len(last_n_closes) < 2:
        return False
    c1, c0 = float(last_n_closes[-2]), float(last_n_closes[-1])
    return c1 > or_high and c0 > or_high


def gate_two_consecutive_1m_below(
    last_n_closes: list[float],
    or_low: Optional[float],
) -> bool:
    """S-P2-S4 — mirror of :func:`gate_two_consecutive_1m_above` for shorts."""
    if or_low is None or not last_n_closes or len(last_n_closes) < 2:
        return False
    c1, c0 = float(last_n_closes[-2]), float(last_n_closes[-1])
    return c1 < or_low and c0 < or_low


__all__ = [
    "LOOKBACK_DAYS_55",
    "THRESHOLD_RATIO",
    "rolling_55d_per_minute_avg",
    "gate_volume_pass",
    "gate_two_consecutive_1m_above",
    "gate_two_consecutive_1m_below",
    "reset_cache",
]
