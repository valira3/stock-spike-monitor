"""v5.26.0 \u2014 engine package (spec-strict).

Per-tick decision pipeline. Stage 1 + Stage 3 spec-strict cuts of
v5.26.0 deleted daily_bars, feature_flags, volume_baseline, sma_stack,
phase_machine modules + the non-spec seeder helpers (DI seed, QQQ
regime seed/tick, archive/Alpaca prior-session fallbacks). What
remains: bars (5m OHLC + EMA9), seeders (OR freeze only), callbacks,
scan, sentinel.
"""

from __future__ import annotations

from engine.bars import compute_5m_ohlc_and_ema9
from engine.callbacks import EngineCallbacks
from engine.scan import scan_loop
from engine.seeders import (
    seed_opening_range,
    seed_opening_range_all,
)
from engine.sentinel import (
    SentinelAction,
    SentinelResult,
    check_alarm_a,
    check_alarm_b,
    evaluate_sentinel,
)

LOADED_MODULES = ("bars", "seeders", "callbacks", "scan", "sentinel")

__all__ = [
    "compute_5m_ohlc_and_ema9",
    "EngineCallbacks",
    "scan_loop",
    "seed_opening_range",
    "seed_opening_range_all",
    "SentinelAction",
    "SentinelResult",
    "check_alarm_a",
    "check_alarm_b",
    "evaluate_sentinel",
    "LOADED_MODULES",
]
