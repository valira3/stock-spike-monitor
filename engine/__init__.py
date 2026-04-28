"""v5.11.0 \u2014 engine package.

Houses the per-tick decision pipeline extracted from
`trade_genius.py`. Subsequent PRs in v5.11.x will add
`phase_machine`, `scan`, and `callbacks`.

Boot log line `[ENGINE] modules loaded: bars, seeders` is emitted
at trade_genius startup so missed Dockerfile COPY lines surface
as ImportError on boot rather than mid-session.
"""
from __future__ import annotations

from engine.bars import compute_5m_ohlc_and_ema9
from engine.seeders import (
    qqq_regime_seed_once,
    qqq_regime_tick,
    seed_di_buffer,
    seed_di_all,
    seed_opening_range,
    seed_opening_range_all,
)

LOADED_MODULES = ("bars", "seeders")

__all__ = [
    "compute_5m_ohlc_and_ema9",
    "qqq_regime_seed_once",
    "qqq_regime_tick",
    "seed_di_buffer",
    "seed_di_all",
    "seed_opening_range",
    "seed_opening_range_all",
    "LOADED_MODULES",
]
