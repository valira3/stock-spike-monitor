"""broker.order_types — v5.13.0 PR 6 SHARED-ORDER-PROFIT / SHARED-ORDER-STOP.

Centralizes the spec-mandated order-type mapping for Tiger Sovereign
(STRATEGY.md §3 Shared rules). Every exit path that emits an Alpaca
order MUST go through ``order_type_for_reason`` so the LIMIT vs.
STOP MARKET vs. MARKET split is one source of truth.

Spec mapping (STRATEGY.md):
    Profit-taking (Stage 1, Stage 3 harvest, Stage 4 runner exit
    via positive-slippage limit) → LIMIT
    Defensive stops (Alarm A1 hard floor, Alarm A2 velocity, Alarm B
    9-EMA shield, Stage 2 ratchet trail) → STOP_MARKET
    EOD flush + Daily Circuit Breaker → MARKET

The runner exit (Stage 4) uses STOP_MARKET when the trail is hit
(spec: "All defensive stops...must be STOP MARKET orders to guarantee
immediate execution"). LIMIT-style runner exits are not in the spec
text — only Stages 1 and 3 are explicit Limit orders.
"""
from __future__ import annotations

from dataclasses import dataclass

# Spec-literal order-type tokens. Strings (not enums) so downstream
# code can pass them directly to the Alpaca SDK request constructors
# or to internal logging without an extra mapping layer.
ORDER_TYPE_LIMIT: str = "LIMIT"
ORDER_TYPE_STOP_MARKET: str = "STOP_MARKET"
ORDER_TYPE_MARKET: str = "MARKET"

# Stable reason codes — used as inputs to ``order_type_for_reason``.
# These match the strings emitted by engine.sentinel and engine.titan_grip.
REASON_STAGE1_HARVEST: str = "C1_STAGE1_HARVEST"
REASON_STAGE3_HARVEST: str = "C3_STAGE3_HARVEST"
REASON_RATCHET: str = "C2_RATCHET"
REASON_RUNNER_EXIT: str = "C4_RUNNER_EXIT"
REASON_ALARM_A: str = "sentinel_alarm_a"
REASON_ALARM_B: str = "sentinel_alarm_b"
REASON_EOD: str = "EOD"
REASON_CIRCUIT_BREAKER: str = "DAILY_LOSS_LIMIT"

# Bucket constants for callers that prefer a coarse classification.
ORDER_TYPE_HARVEST: str = ORDER_TYPE_LIMIT
ORDER_TYPE_STOP: str = ORDER_TYPE_STOP_MARKET

# Profit-taking reasons (LIMIT). Stage 1 + Stage 3 are explicit harvests
# in STRATEGY.md; both are spec-mandated LIMIT orders.
_HARVEST_REASONS = frozenset({
    REASON_STAGE1_HARVEST,
    REASON_STAGE3_HARVEST,
})

# Defensive-stop reasons (STOP MARKET). All of these are spec-mandated
# STOP MARKET in STRATEGY.md §3 ORDER TYPE SPECIFICATIONS:
# "All defensive stops...must be STOP MARKET orders".
_STOP_REASONS = frozenset({
    REASON_RATCHET,
    REASON_RUNNER_EXIT,
    REASON_ALARM_A,
    REASON_ALARM_B,
})

# Plain MARKET reasons. STRATEGY.md §3:
# "DAILY CIRCUIT BREAKER: ...close all open positions immediately
#  using MARKET orders."
# "END OF DAY (EOD) FLUSH: ...close all open positions using MARKET
#  orders."
_MARKET_REASONS = frozenset({
    REASON_EOD,
    REASON_CIRCUIT_BREAKER,
})


@dataclass(frozen=True)
class ExitOrder:
    """Order spec returned by ``submit_exit``.

    Pure data — no I/O. Callers translate this into an Alpaca request
    (MarketOrderRequest / LimitOrderRequest / StopOrderRequest) or an
    internal paper-broker entry. Keeping the type/qty/price decision
    pure makes the order-type mapping testable without an Alpaca stub.
    """

    direction: str   # "LONG" or "SHORT"
    qty: int
    price: float     # limit / stop price; 0.0 for MARKET
    reason: str
    order_type: str  # one of ORDER_TYPE_*


def order_type_for_reason(reason: str) -> str:
    """Return the spec-mandated order type for an exit reason.

    Unknown reasons fall back to MARKET (matching legacy behavior of
    ``client.close_position``). The fallback exists so partial
    instrumentation during the v5.13.0 rollout can't accidentally
    submit the wrong type — known reasons get the correct mapping;
    unknown reasons stay on the historic MARKET path.
    """
    if reason in _HARVEST_REASONS:
        return ORDER_TYPE_LIMIT
    if reason in _STOP_REASONS:
        return ORDER_TYPE_STOP_MARKET
    if reason in _MARKET_REASONS:
        return ORDER_TYPE_MARKET
    return ORDER_TYPE_MARKET


def submit_exit(direction: str, qty: int, price: float, reason: str) -> ExitOrder:
    """Build an ExitOrder with the spec-correct order type.

    The function is pure — it does NOT call Alpaca. It returns the
    structured order spec so callers can either submit it through
    a real client or record it as a paper-broker fill. Short-side
    mirroring is handled by ``direction``: a short harvest is a
    BUY-LIMIT below entry, a short stop is a BUY-STOP above entry.
    The direction string is preserved so the broker layer can map
    LONG → SELL / SHORT → BUY when it actually emits the order.

    Args:
        direction: "LONG" or "SHORT"
        qty: number of shares to close (must be > 0)
        price: limit/stop price; pass 0.0 for MARKET reasons
        reason: stable reason code (see REASON_* constants)
    """
    if qty <= 0:
        raise ValueError(f"submit_exit: qty must be > 0, got {qty}")
    if direction not in ("LONG", "SHORT"):
        raise ValueError(f"submit_exit: bad direction {direction!r}")
    return ExitOrder(
        direction=direction,
        qty=int(qty),
        price=float(price),
        reason=reason,
        order_type=order_type_for_reason(reason),
    )


__all__ = [
    "ExitOrder",
    "ORDER_TYPE_HARVEST",
    "ORDER_TYPE_LIMIT",
    "ORDER_TYPE_MARKET",
    "ORDER_TYPE_STOP",
    "ORDER_TYPE_STOP_MARKET",
    "REASON_ALARM_A",
    "REASON_ALARM_B",
    "REASON_CIRCUIT_BREAKER",
    "REASON_EOD",
    "REASON_RATCHET",
    "REASON_RUNNER_EXIT",
    "REASON_STAGE1_HARVEST",
    "REASON_STAGE3_HARVEST",
    "order_type_for_reason",
    "submit_exit",
]
