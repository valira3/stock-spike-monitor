"""v5.26.0 \u2014 broker package.

v5.26.0 spec-strict cut: the standalone `broker.stops` module is
deleted (capped/ladder/retighten helpers were non-spec). The R-2
hard stop now flows through the sentinel exit path. The remaining
re-exports below are the surface trade_genius.py + tests still pull
through ``broker.<symbol>``.
"""

from __future__ import annotations

from broker.orders import (
    check_breakout,
    paper_shares_for,
    execute_breakout,
    close_breakout,
)
from broker.positions import (
    _v5104_maybe_fire_entry_2,
    manage_positions,
    manage_short_positions,
)
from broker.lifecycle import (
    check_entry,
    check_short_entry,
    execute_entry,
    execute_short_entry,
    close_position,
    close_short_position,
    eod_close,
)

LOADED_MODULES = ("orders", "positions", "lifecycle")

__all__ = [
    "check_breakout",
    "paper_shares_for",
    "execute_breakout",
    "close_breakout",
    "_v5104_maybe_fire_entry_2",
    "manage_positions",
    "manage_short_positions",
    "check_entry",
    "check_short_entry",
    "execute_entry",
    "execute_short_entry",
    "close_position",
    "close_short_position",
    "eod_close",
    "LOADED_MODULES",
]
