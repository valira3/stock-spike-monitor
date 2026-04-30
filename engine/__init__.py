"""v5.11.0 \u2014 engine package.

Houses the per-tick decision pipeline extracted from
`trade_genius.py`. PR 4 adds `callbacks` (the `EngineCallbacks`
Protocol) and `scan` (the per-minute scan loop). Subsequent PRs
in v5.11.x will retire deprecation shims.

Boot log line `[ENGINE] modules loaded: bars, seeders, phase_machine,
callbacks, scan` is emitted at trade_genius startup so missed
Dockerfile COPY lines surface as ImportError on boot rather than
mid-session.
"""

from __future__ import annotations

from engine.bars import compute_5m_ohlc_and_ema9
from engine.callbacks import EngineCallbacks
from engine.scan import scan_loop
from engine.seeders import (
    qqq_regime_seed_once,
    qqq_regime_tick,
    recompute_di_for_unseeded,
    recompute_qqq_regime_if_unwarm,
    seed_di_buffer,
    seed_di_all,
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
    "qqq_regime_seed_once",
    "qqq_regime_tick",
    "recompute_qqq_regime_if_unwarm",
    "seed_di_buffer",
    "seed_di_all",
    "recompute_di_for_unseeded",
    "seed_opening_range",
    "seed_opening_range_all",
    "SentinelAction",
    "SentinelResult",
    "check_alarm_a",
    "check_alarm_b",
    "evaluate_sentinel",
    "LOADED_MODULES",
]
