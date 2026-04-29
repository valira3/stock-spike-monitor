"""v5.11.2 \u2014 broker package.

Houses the broker / position-management code extracted from
`trade_genius.py`. PR 1 introduces `stops` (breakeven, capped, ladder,
and retighten helpers). PRs 2\u20134 will add orders, positions, and
lifecycle modules.

Boot log line `[BROKER] modules loaded: stops` is emitted at
trade_genius startup so missed Dockerfile COPY lines surface as
ImportError on boot rather than mid-session.
"""
from __future__ import annotations

from broker.stops import (
    _breakeven_long_stop,
    _breakeven_short_stop,
    _capped_long_stop,
    _capped_short_stop,
    _ladder_stop_long,
    _ladder_stop_short,
    _retighten_long_stop,
    _retighten_short_stop,
    retighten_all_stops,
)

LOADED_MODULES = ("stops",)

__all__ = [
    "_breakeven_long_stop",
    "_breakeven_short_stop",
    "_capped_long_stop",
    "_capped_short_stop",
    "_ladder_stop_long",
    "_ladder_stop_short",
    "_retighten_long_stop",
    "_retighten_short_stop",
    "retighten_all_stops",
    "LOADED_MODULES",
]
