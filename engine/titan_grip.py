"""DEPRECATED \u2014 module removed in v5.16.0.

v5.15.0 PR-4 replaced Titan Grip Harvest with the Velocity Ratchet
(``engine.velocity_ratchet``). The old staircase logic (Stage 1 0.93%,
micro-ratchet 0.25%, Stage 3 1.88%, runner) is gone; this shim exists
solely so legacy imports do not crash before the v5.16.0 cleanup PR.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Optional

from engine.velocity_ratchet import (
    EXIT_REASON_VELOCITY_RATCHET,
    evaluate_velocity_ratchet,
)

warnings.warn(
    "engine.titan_grip is deprecated; use engine.velocity_ratchet",
    DeprecationWarning,
    stacklevel=2,
)

# Spec alias \u2014 the new evaluator under the old name.
evaluate_titan_grip = evaluate_velocity_ratchet

# Stable string codes preserved so broker.order_types and the
# v5.13.7 close-order-type wiring tests keep their reason \u2192
# order-type mapping. The behaviour these codes used to drive is
# gone; the strings now serve only as identifiers in logs and tests.
ACTION_STAGE1_HARVEST: str = "C1_STAGE1_HARVEST"
ACTION_RATCHET: str = "C2_RATCHET"
ACTION_STAGE3_HARVEST: str = "C3_STAGE3_HARVEST"
ACTION_RUNNER_EXIT: str = "C4_RUNNER_EXIT"

ORDER_TYPE_LIMIT: str = "LIMIT"
ORDER_TYPE_STOP_MARKET: str = "STOP_MARKET"

EXIT_REASON_ALARM_C: str = EXIT_REASON_VELOCITY_RATCHET

SIDE_LONG: str = "LONG"
SIDE_SHORT: str = "SHORT"


@dataclass
class TitanGripAction:
    """DEPRECATED legacy struct kept so old imports resolve. The
    Velocity Ratchet does not emit these; production code should
    consume ``engine.sentinel.SentinelAction`` instead.
    """

    code: str = ""
    shares: int = 0
    price: float = 0.0
    order_type: str = ""
    detail: str = ""


@dataclass
class TitanGripState:
    """DEPRECATED no-op state object kept so legacy imports resolve.

    The Velocity Ratchet is stateless beyond the ``ADXTrendWindow``;
    callers should attach ``engine.momentum_state.ADXTrendWindow`` to
    the position instead.
    """

    position_id: str = ""
    direction: str = SIDE_LONG
    entry_price: float = 0.0
    or_high: float = 0.0
    or_low: float = 0.0
    original_shares: int = 0
    stage: int = 0
    current_stop_anchor: Optional[float] = None
    first_harvest_done: bool = False
    second_harvest_done: bool = False


def check_titan_grip(*args, **kwargs) -> list:
    """DEPRECATED \u2014 always returns ``[]``.

    The Titan Grip staircase was deleted in v5.15.0 PR-4. The
    Velocity Ratchet replacement lives in ``engine.velocity_ratchet``
    and is dispatched by ``engine.sentinel.check_alarm_c``.
    """
    return []


__all__ = [
    "ACTION_RATCHET",
    "ACTION_RUNNER_EXIT",
    "ACTION_STAGE1_HARVEST",
    "ACTION_STAGE3_HARVEST",
    "EXIT_REASON_ALARM_C",
    "ORDER_TYPE_LIMIT",
    "ORDER_TYPE_STOP_MARKET",
    "SIDE_LONG",
    "SIDE_SHORT",
    "TitanGripAction",
    "TitanGripState",
    "check_titan_grip",
    "evaluate_titan_grip",
]
