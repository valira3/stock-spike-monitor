"""v5.15.0 PR-4 \u2014 Tiger Sovereign vAA-1 Velocity Ratchet.

Replaces the deleted Titan Grip Harvest (Stage 1 0.93%, micro-ratchet
0.25%, Stage 3 1.88%, runner) with a single unified rule:

* When the 1m ADX trend window prints three strictly-decreasing values
  (oldest > middle > newest), tighten the protective stop to
  ``current_price * (1 - 0.0025)`` for LONG (and the mirror for
  SHORT). Never loosen \u2014 if the existing stop is already tighter
  than the proposed new one, emit nothing.

Spec rule IDs:
* SENT-C velocity ratchet
* SENT-C strictly monotone
* SENT-C ratchet does not loosen
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Optional

if TYPE_CHECKING:
    from engine.momentum_state import ADXTrendWindow

# Spec literal: protective offset is 0.25% from current price.
RATCHET_STOP_PCT: float = 0.0025

SIDE_LONG: str = "LONG"
SIDE_SHORT: str = "SHORT"

EXIT_REASON_VELOCITY_RATCHET: str = "sentinel_velocity_ratchet"


@dataclass(frozen=True)
class RatchetDecision:
    """Outcome of one Velocity Ratchet evaluation.

    ``should_emit_stop`` is True iff the ratchet trigger fired AND the
    new stop is strictly tighter than ``existing_stop_price``. When
    True, ``new_stop_price`` carries the protective price the caller
    should send as a STOP MARKET modify. When False, ``new_stop_price``
    may still carry the proposed value (for telemetry) but callers must
    not act on it.
    """

    should_emit_stop: bool
    new_stop_price: Optional[float]
    reason: str


def _proposed_stop(side: str, current_price: float) -> float:
    """Compute the proposed protective stop for a given side."""
    if side == SIDE_LONG:
        return float(current_price) * (1.0 - RATCHET_STOP_PCT)
    if side == SIDE_SHORT:
        return float(current_price) * (1.0 + RATCHET_STOP_PCT)
    raise ValueError(f"evaluate_velocity_ratchet: bad side {side!r}")


def _is_tighter(side: str, new_stop: float, existing: Optional[float]) -> bool:
    """Tighter means closer to current price in the protective direction.

    For a LONG, a tighter stop is HIGHER (less room to fall). For a
    SHORT, a tighter stop is LOWER (less room to rise). If no existing
    stop is set, any proposal is considered tighter.
    """
    if existing is None:
        return True
    if side == SIDE_LONG:
        return new_stop > existing
    return new_stop < existing


def evaluate_velocity_ratchet(
    side: Literal["LONG", "SHORT"],
    adx_window: "ADXTrendWindow",
    current_price: float,
    existing_stop_price: Optional[float],
) -> RatchetDecision:
    """Evaluate Alarm C / Velocity Ratchet for one position on one tick.

    Trigger: ``adx_window`` holds three samples that are strictly
    monotone-decreasing (oldest > middle > newest). Equality on either
    pair fails the trigger. A window with fewer than three samples
    never fires.

    On trigger, propose ``current_price \u00b1 0.25%`` in the protective
    direction. Emit only if strictly tighter than the existing stop;
    the ratchet never loosens.
    """
    if not adx_window.is_strictly_decreasing():
        return RatchetDecision(
            should_emit_stop=False,
            new_stop_price=None,
            reason="adx_not_strictly_decreasing",
        )

    new_stop = _proposed_stop(side, current_price)
    if not _is_tighter(side, new_stop, existing_stop_price):
        return RatchetDecision(
            should_emit_stop=False,
            new_stop_price=new_stop,
            reason="not_tighter_than_existing_stop",
        )

    return RatchetDecision(
        should_emit_stop=True,
        new_stop_price=new_stop,
        reason="velocity_ratchet_fired",
    )


__all__ = [
    "EXIT_REASON_VELOCITY_RATCHET",
    "RATCHET_STOP_PCT",
    "RatchetDecision",
    "SIDE_LONG",
    "SIDE_SHORT",
    "evaluate_velocity_ratchet",
]
