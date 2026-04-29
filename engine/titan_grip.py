"""v5.13.0 PR 3 — Titan Grip Harvest ratchet state machine.

Implements rules L-P4-C-S1..S4 (long) and S-P4-C-S1..S4 (short) from
the Tiger Sovereign spec (STRATEGY.md § Phase 4 Alarm C). The ratchet
is the third parallel sentinel arm: alongside Alarm A (Emergency)
and Alarm B (9-EMA Shield), it monitors every tick and may harvest
partial profits or trail-stop the runner.

Sizing — spec-literal:
* Stage 1 (anchor at OR_High + 0.93%): SELL 25% LIMIT, move stop to
  OR_High + 0.40%.
* Stage 2 (micro-ratchet, every +0.25% above 0.93%): move stop +0.25%.
* Stage 3 (second harvest at OR_High + 1.88%): SELL 25% LIMIT.
* Stage 4 (runner, final 50%): continued +0.25% / +0.25% ratchet
  until stop is hit or EOD flush.

Short side mirrors with OR_Low minus the same offsets and price
inequalities flipped.

Order types:
* Profit-taking (Stage 1, Stage 3) → LIMIT (positive slippage).
* Stops (Stage 1 stop, Stage 2/4 ratchet) → STOP MARKET.
PR 6 owns the actual order-type submission switch; this module
records the spec-mandated `order_type` on each emitted action so
the executor swap is mechanical when PR 6 lands.

Parallel-not-sequential semantics: every tick the bot evaluates
Alarms A, B, AND C independently. If A and C fire on the same tick,
A wins (full exit overrides partial harvest), but BOTH alarms appear
in the SentinelResult.fired list for observability. This module
returns its own list of TitanGripActions; the merge happens in
engine.sentinel.evaluate_sentinel.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Constants — spec-literal thresholds (STRATEGY.md § Phase 4 Alarm C)
# ---------------------------------------------------------------------------

# Stage 1 anchor — first harvest fires at this offset above OR_High
# (long) / below OR_Low (short). Long: +0.93%. Short: -0.93%.
TITAN_GRIP_STAGE1_ANCHOR_PCT: float = 0.0093  # 0.93%

# Stage 1 stop — move stop to OR_High + 0.40% (long) / OR_Low - 0.40%
# (short) the moment Stage 1 fires.
TITAN_GRIP_STAGE1_STOP_PCT: float = 0.0040  # 0.40%

# Stage 2 micro-ratchet step. Every additional +0.25% above 0.93%
# (long) advances the stop by another +0.25%. Symmetric on short.
TITAN_GRIP_RATCHET_STEP_PCT: float = 0.0025  # 0.25%

# Stage 3 second-harvest threshold — OR_High + 1.88% (long) /
# OR_Low - 1.88% (short).
TITAN_GRIP_STAGE3_TARGET_PCT: float = 0.0188  # 1.88%

# Stage 1 / Stage 3 harvest sizes as fractions of the ORIGINAL
# position. 25% + 25% + 50% runner = 100%.
TITAN_GRIP_STAGE1_HARVEST_FRAC: float = 0.25
TITAN_GRIP_STAGE3_HARVEST_FRAC: float = 0.25
TITAN_GRIP_RUNNER_FRAC: float = 0.50

# Action codes emitted by check_titan_grip on tick events.
ACTION_STAGE1_HARVEST: str = "C1_STAGE1_HARVEST"  # SELL 25% LIMIT
ACTION_RATCHET: str = "C2_RATCHET"                # move stop only
ACTION_STAGE3_HARVEST: str = "C3_STAGE3_HARVEST"  # SELL 25% LIMIT
ACTION_RUNNER_EXIT: str = "C4_RUNNER_EXIT"        # final 50% STOP MARKET

ORDER_TYPE_LIMIT: str = "LIMIT"
ORDER_TYPE_STOP_MARKET: str = "STOP_MARKET"

EXIT_REASON_ALARM_C: str = "sentinel_alarm_c"

SIDE_LONG = "LONG"
SIDE_SHORT = "SHORT"


# ---------------------------------------------------------------------------
# State + actions
# ---------------------------------------------------------------------------


@dataclass
class TitanGripAction:
    """Single Titan Grip event emitted on a tick.

    `code` is one of ACTION_*. `shares` is the number of shares the
    action affects (0 for ACTION_RATCHET, which is stop-only).
    `order_type` is LIMIT for harvests, STOP_MARKET for the runner
    exit; ACTION_RATCHET carries STOP_MARKET (stop modify, not a fill).
    """

    code: str
    shares: int
    price: float
    order_type: str
    detail: str = ""


@dataclass
class TitanGripState:
    """Per-position ratchet state. One instance lives for the lifetime
    of a position; broker.positions stores it as a sidecar on the
    position dict (key ``titan_grip_state``).

    Stage values:
        0 = pre-anchor (price has not yet hit Stage 1 target)
        1 = anchored (Stage 1 harvest done; ratcheting toward Stage 3)
        2 = post-Stage-3 (runner phase; final 50% trailing)
        3 = exited (runner stop hit; terminal)
    """

    position_id: str
    direction: str  # SIDE_LONG or SIDE_SHORT
    entry_price: float
    or_high: float        # used for long-side targets
    or_low: float         # used for short-side targets
    original_shares: int

    stage: int = 0
    current_stop_anchor: Optional[float] = None
    first_harvest_done: bool = False
    second_harvest_done: bool = False

    # Spec-literal precomputed targets. Long uses or_high + offset;
    # short uses or_low - offset. Stored once at construction so we
    # never re-derive the boundary mid-trade.
    stage1_harvest_target: float = field(init=False)
    stage3_harvest_target: float = field(init=False)

    def __post_init__(self) -> None:
        if self.direction == SIDE_LONG:
            self.stage1_harvest_target = self.or_high * (1.0 + TITAN_GRIP_STAGE1_ANCHOR_PCT)
            self.stage3_harvest_target = self.or_high * (1.0 + TITAN_GRIP_STAGE3_TARGET_PCT)
        elif self.direction == SIDE_SHORT:
            self.stage1_harvest_target = self.or_low * (1.0 - TITAN_GRIP_STAGE1_ANCHOR_PCT)
            self.stage3_harvest_target = self.or_low * (1.0 - TITAN_GRIP_STAGE3_TARGET_PCT)
        else:
            raise ValueError(f"TitanGripState: bad direction {self.direction!r}")

    # Convenience — number of shares a Stage 1 / Stage 3 harvest cuts
    # from the ORIGINAL position (per STRATEGY.md spec sizing). int()
    # truncates toward zero, matching how _shares_for_partial in the
    # executor side already behaves.
    def stage1_harvest_shares(self) -> int:
        return int(self.original_shares * TITAN_GRIP_STAGE1_HARVEST_FRAC)

    def stage3_harvest_shares(self) -> int:
        return int(self.original_shares * TITAN_GRIP_STAGE3_HARVEST_FRAC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stage1_stop_for(state: TitanGripState) -> float:
    """OR_High + 0.40% (long) / OR_Low - 0.40% (short) — the Stage 1
    stop placement that fires the moment Stage 1 harvest happens.
    """
    if state.direction == SIDE_LONG:
        return state.or_high * (1.0 + TITAN_GRIP_STAGE1_STOP_PCT)
    return state.or_low * (1.0 - TITAN_GRIP_STAGE1_STOP_PCT)


def _ratcheted_anchor(state: TitanGripState, current_price: float) -> Optional[float]:
    """Return a NEW anchor value if the price has advanced past the
    last anchor + RATCHET_STEP. Else None.

    The ratchet step is computed off OR_High (long) / OR_Low (short)
    so the absolute delta is stable across a session. Implementation
    is monotone: never moves anchor backward — the caller only adopts
    the result when it's strictly tighter than the existing stop.
    """
    if state.current_stop_anchor is None:
        return None
    if state.direction == SIDE_LONG:
        step = state.or_high * TITAN_GRIP_RATCHET_STEP_PCT
        # advance only if price has moved one full step past the
        # current anchor, then snap to the next grid point.
        steps_above = int((current_price - state.current_stop_anchor) // step)
        if steps_above >= 1:
            return state.current_stop_anchor + steps_above * step
    else:
        step = state.or_low * TITAN_GRIP_RATCHET_STEP_PCT
        steps_below = int((state.current_stop_anchor - current_price) // step)
        if steps_below >= 1:
            return state.current_stop_anchor - steps_below * step
    return None


def _hit_stage1_target(state: TitanGripState, current_price: float) -> bool:
    if state.direction == SIDE_LONG:
        return current_price >= state.stage1_harvest_target
    return current_price <= state.stage1_harvest_target


def _hit_stage3_target(state: TitanGripState, current_price: float) -> bool:
    if state.direction == SIDE_LONG:
        return current_price >= state.stage3_harvest_target
    return current_price <= state.stage3_harvest_target


def _hit_stop_anchor(state: TitanGripState, current_price: float) -> bool:
    """True when price has fallen back to the current ratchet anchor.
    Long: price <= anchor. Short: price >= anchor.
    """
    if state.current_stop_anchor is None:
        return False
    if state.direction == SIDE_LONG:
        return current_price <= state.current_stop_anchor
    return current_price >= state.current_stop_anchor


# ---------------------------------------------------------------------------
# Tick evaluator
# ---------------------------------------------------------------------------


def check_titan_grip(
    *, state: TitanGripState, current_price: float, current_shares: int
) -> list[TitanGripAction]:
    """Evaluate the Titan Grip ratchet for one tick. Mutates `state`.

    Returns the list of actions the caller should execute this tick.
    Each action is independent; the caller emits one order per action.

    The state machine fires AT MOST one stage transition per tick
    (Stage 1 → Stage 1 stop placement, Stage 3 transition, or runner
    exit), but a single tick can produce multiple ACTION_RATCHET
    events if price has jumped multiple steps; the implementation
    coalesces them into one ACTION_RATCHET that records the new
    anchor.
    """
    if state.stage >= 3:
        return []  # already exited

    actions: list[TitanGripAction] = []

    # --- Stage 0 → Stage 1: first harvest ---
    if state.stage == 0:
        if _hit_stage1_target(state, current_price):
            harvest_n = state.stage1_harvest_shares()
            if harvest_n > 0 and harvest_n <= current_shares:
                actions.append(
                    TitanGripAction(
                        code=ACTION_STAGE1_HARVEST,
                        shares=harvest_n,
                        price=current_price,
                        order_type=ORDER_TYPE_LIMIT,
                        detail=(
                            f"side={state.direction} stage1_target="
                            f"{state.stage1_harvest_target:.4f} "
                            f"price={current_price:.4f}"
                        ),
                    )
                )
                state.current_stop_anchor = _stage1_stop_for(state)
                state.first_harvest_done = True
                state.stage = 1
        return actions

    # --- Stage 1: micro-ratchet OR Stage 3 trigger OR stop hit ---
    if state.stage == 1:
        # Stage 3 second harvest (highest priority within Stage 1
        # because crossing 1.88% is a discrete profit-taking event).
        if _hit_stage3_target(state, current_price):
            harvest_n = state.stage3_harvest_shares()
            if harvest_n > 0 and harvest_n <= current_shares:
                actions.append(
                    TitanGripAction(
                        code=ACTION_STAGE3_HARVEST,
                        shares=harvest_n,
                        price=current_price,
                        order_type=ORDER_TYPE_LIMIT,
                        detail=(
                            f"side={state.direction} stage3_target="
                            f"{state.stage3_harvest_target:.4f} "
                            f"price={current_price:.4f}"
                        ),
                    )
                )
                state.second_harvest_done = True
                state.stage = 2
                # Continue evaluation in Stage 2 below — a single tick
                # can both trigger Stage 3 and ratchet. Recompute the
                # ratchet anchor after the harvest so the runner has
                # tight protection from the new threshold.
                new_anchor = _ratcheted_anchor(state, current_price)
                if new_anchor is not None:
                    state.current_stop_anchor = new_anchor
                    actions.append(
                        TitanGripAction(
                            code=ACTION_RATCHET,
                            shares=0,
                            price=current_price,
                            order_type=ORDER_TYPE_STOP_MARKET,
                            detail=(
                                f"side={state.direction} new_anchor="
                                f"{new_anchor:.4f}"
                            ),
                        )
                    )
                return actions

        # Stop anchor hit — fire ACTION_STAGE1_HARVEST exit (caller
        # exits remaining position). This is the "drop to anchor"
        # path: Stage 1 already harvested 25%, the remaining 75%
        # exits at the trailed anchor.
        if _hit_stop_anchor(state, current_price):
            remaining = current_shares
            actions.append(
                TitanGripAction(
                    code=ACTION_RUNNER_EXIT,
                    shares=remaining,
                    price=current_price,
                    order_type=ORDER_TYPE_STOP_MARKET,
                    detail=(
                        f"side={state.direction} stop_hit_in_stage1 "
                        f"anchor={state.current_stop_anchor:.4f} "
                        f"price={current_price:.4f}"
                    ),
                )
            )
            state.stage = 3
            return actions

        # Pure micro-ratchet — advance the stop only, no harvest.
        new_anchor = _ratcheted_anchor(state, current_price)
        if new_anchor is not None:
            state.current_stop_anchor = new_anchor
            actions.append(
                TitanGripAction(
                    code=ACTION_RATCHET,
                    shares=0,
                    price=current_price,
                    order_type=ORDER_TYPE_STOP_MARKET,
                    detail=(
                        f"side={state.direction} new_anchor="
                        f"{new_anchor:.4f}"
                    ),
                )
            )
        return actions

    # --- Stage 2 (runner): continued ratchet, or runner stop exit ---
    if state.stage == 2:
        if _hit_stop_anchor(state, current_price):
            remaining = current_shares
            actions.append(
                TitanGripAction(
                    code=ACTION_RUNNER_EXIT,
                    shares=remaining,
                    price=current_price,
                    order_type=ORDER_TYPE_STOP_MARKET,
                    detail=(
                        f"side={state.direction} runner_stop_hit "
                        f"anchor={state.current_stop_anchor:.4f} "
                        f"price={current_price:.4f}"
                    ),
                )
            )
            state.stage = 3
            return actions

        new_anchor = _ratcheted_anchor(state, current_price)
        if new_anchor is not None:
            state.current_stop_anchor = new_anchor
            actions.append(
                TitanGripAction(
                    code=ACTION_RATCHET,
                    shares=0,
                    price=current_price,
                    order_type=ORDER_TYPE_STOP_MARKET,
                    detail=(
                        f"side={state.direction} runner_anchor="
                        f"{new_anchor:.4f}"
                    ),
                )
            )

    return actions


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
    "TITAN_GRIP_RATCHET_STEP_PCT",
    "TITAN_GRIP_RUNNER_FRAC",
    "TITAN_GRIP_STAGE1_ANCHOR_PCT",
    "TITAN_GRIP_STAGE1_HARVEST_FRAC",
    "TITAN_GRIP_STAGE1_STOP_PCT",
    "TITAN_GRIP_STAGE3_HARVEST_FRAC",
    "TITAN_GRIP_STAGE3_TARGET_PCT",
    "TitanGripAction",
    "TitanGripState",
    "check_titan_grip",
]
