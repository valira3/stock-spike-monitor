"""engine package (post-v10.0.1).

Per-tick decision pipeline. The Tiger Sentinel A/B/C chain
(engine.sentinel + engine.momentum_state + engine.velocity_ratchet)
was deleted in v10.0.1 when v10 ORB took over all exits.
engine.alarm_f_trail is reduced to just the TrailState dataclass
(used by engine.portfolio_book.record_entry on every new position).
"""

from __future__ import annotations

from engine.bars import compute_5m_ohlc_and_ema9
from engine.callbacks import EngineCallbacks
from engine.scan import scan_loop
from engine.seeders import (
    seed_opening_range,
    seed_opening_range_all,
)

LOADED_MODULES = ("bars", "seeders", "callbacks", "scan")

__all__ = [
    "compute_5m_ohlc_and_ema9",
    "EngineCallbacks",
    "scan_loop",
    "seed_opening_range",
    "seed_opening_range_all",
    "LOADED_MODULES",
]
