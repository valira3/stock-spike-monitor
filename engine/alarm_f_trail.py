"""engine.alarm_f_trail -- slim survivor (v10.0.1).

Reduced from the full v6.0.6 chandelier-trail implementation (507 LOC)
to just the dataclass + stage constants. The Tiger Sentinel A/B/C
chain plus the chandelier exit logic were deleted when v10 ORB took
over all exits; only the per-position `TrailState` dataclass survives,
because engine.portfolio_book.record_entry still stamps one on every
new position dict (back-compat for the position serialization schema).

The trail logic (`update_trail`, `propose_stop`, `true_range`,
`atr_from_bars`, `_favorable`, and the STAGE3 transition rules) is
gone. STAGE_BREAKEVEN is the only stage v10-managed positions ever
reach -- they are armed at entry by record_entry and never advance
because no caller ticks them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

STAGE_INACTIVE: int = 0
STAGE_BREAKEVEN: int = 1
STAGE_CHANDELIER_WIDE: int = 2
STAGE_CHANDELIER_TIGHT: int = 3

SIDE_LONG = "LONG"
SIDE_SHORT = "SHORT"


@dataclass
class TrailState:
    """Per-position Alarm F state. Persisted across ticks via the position dict.

    Surfaced by engine.portfolio_book.record_entry on every new entry; the
    live tick loop never reads it back because v10 ORB owns exits.
    """

    stage: int = STAGE_INACTIVE
    peak_close: Optional[float] = None
    stage2_arm_favorable: Optional[float] = None
    stage2_arm_atr: Optional[float] = None
    last_proposed_stop: Optional[float] = None
    bars_seen: int = 0
    last_atr: Optional[float] = None
    last_mult: float = 0.0

    @classmethod
    def fresh(cls) -> "TrailState":
        return cls()


__all__ = [
    "STAGE_INACTIVE",
    "STAGE_BREAKEVEN",
    "STAGE_CHANDELIER_WIDE",
    "STAGE_CHANDELIER_TIGHT",
    "SIDE_LONG",
    "SIDE_SHORT",
    "TrailState",
]
