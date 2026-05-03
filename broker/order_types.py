"""broker.order_types \u2014 v5.26.0 spec-strict order-type mapping.

Tiger Sovereign v15.0 mapping per RULING #1: sentinel A-A / A-B / A-D
exits emit LIMIT orders at +/- 0.5% from current. A_LOSS (R-2 hard
stop, -$500) is a STOP MARKET. EOD flush + Daily Circuit Breaker emit
MARKET orders. Stage-1 / Stage-3 Titan-Grip harvest constants from
v5.16.0 are deleted (dead code).

v6.8.0 W-E fix: EXIT_REASON_V651_DEEP_STOP routes to STOP_MARKET.
Previously the unknown-reason fallback returned MARKET (audit finding
W-E, v6.6.0). Added to _STOP_REASONS alongside R-2 and price-rail.
"""

from __future__ import annotations

from dataclasses import dataclass

ORDER_TYPE_LIMIT: str = "LIMIT"
ORDER_TYPE_STOP_MARKET: str = "STOP_MARKET"
ORDER_TYPE_MARKET: str = "MARKET"

# v5.26.0 reason codes. Sentinel-driven exits (A-A flash loss, A-B 5m
# EMA cross, A-D ADX decline) route through LIMIT per RULING #1. The
# R-2 hard stop (-$500) routes through STOP MARKET. EOD + circuit
# breaker route through MARKET.
REASON_ALARM_A: str = "sentinel_a_flash_loss"
REASON_ALARM_B: str = "sentinel_b_ema_cross"
REASON_ALARM_D: str = "sentinel_d_adx_decline"
REASON_R2_HARD_STOP: str = "sentinel_r2_hard_stop"
# v5.31.4 \u2014 price-rail STOP MARKET when mark crosses protective stop.
REASON_PRICE_STOP: str = "sentinel_a_stop_price"
REASON_VELOCITY_RATCHET: str = "sentinel_velocity_ratchet"
REASON_HVP_LOCK: str = "HVP_LOCK"
REASON_DIVERGENCE_TRAP: str = "DIVERGENCE_TRAP"
REASON_EOD: str = "EOD"
REASON_CIRCUIT_BREAKER: str = "DAILY_LOSS_LIMIT"
# v6.8.0 W-E fix — deep-stop blow-through rail (V651) must route to
# STOP_MARKET, not fall through to the unknown-reason MARKET fallback.
REASON_V651_DEEP_STOP: str = "sentinel_v651_deep_stop"

# RULING #1: sentinel A-A / A-B / A-D defensive exits use LIMIT (not
# STOP MARKET, not MARKET). HVP_LOCK and DIVERGENCE_TRAP are also
# limit-routed harvests.
_LIMIT_REASONS = frozenset(
    {
        REASON_ALARM_A,
        REASON_ALARM_B,
        REASON_ALARM_D,
        REASON_HVP_LOCK,
        REASON_DIVERGENCE_TRAP,
    }
)

# R-2 hard stop and the velocity ratchet stay STOP MARKET. v5.31.4
# adds the price-rail protective stop here \u2014 same order type, same
# semantics (immediate market-close on cross).
_STOP_REASONS = frozenset(
    {
        REASON_R2_HARD_STOP,
        REASON_PRICE_STOP,
        REASON_VELOCITY_RATCHET,
        REASON_V651_DEEP_STOP,  # v6.8.0 W-E fix
    }
)

# EOD flush and the daily-loss circuit breaker close at MARKET.
_MARKET_REASONS = frozenset(
    {
        REASON_EOD,
        REASON_CIRCUIT_BREAKER,
    }
)


@dataclass(frozen=True)
class ExitOrder:
    """Order spec returned by ``submit_exit``.

    Pure data \u2014 no I/O. Callers translate this into a broker request
    or an internal paper-broker entry.
    """

    direction: str  # "LONG" or "SHORT"
    qty: int
    price: float  # limit / stop price; 0.0 for MARKET
    reason: str
    order_type: str  # one of ORDER_TYPE_*


def order_type_for_reason(reason: str) -> str:
    """Return the spec-mandated order type for an exit reason.

    Unknown reasons fall back to MARKET (matching legacy behavior of
    ``client.close_position``). RULING #1 routes A-A / A-B / A-D
    through LIMIT.
    """
    if reason in _LIMIT_REASONS:
        return ORDER_TYPE_LIMIT
    if reason in _STOP_REASONS:
        return ORDER_TYPE_STOP_MARKET
    if reason in _MARKET_REASONS:
        return ORDER_TYPE_MARKET
    return ORDER_TYPE_MARKET


def compute_sentinel_limit_price(side: str, bid: float, ask: float) -> float:
    """Per RULING #1, sentinel-driven LIMIT exits are placed at:

    LONG  exit \u2192 Bid * 0.995  (0.5% below bid, sells out cleanly)
    SHORT exit \u2192 Ask * 1.005  (0.5% above ask, covers cleanly)
    """
    s = (side or "").strip().upper()
    if s == "LONG":
        return float(bid) * 0.995
    if s == "SHORT":
        return float(ask) * 1.005
    raise ValueError(f"compute_sentinel_limit_price: unknown side {side!r}")


def submit_exit(direction: str, qty: int, price: float, reason: str) -> ExitOrder:
    """Build an ExitOrder with the spec-correct order type."""
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
    "ORDER_TYPE_LIMIT",
    "ORDER_TYPE_MARKET",
    "ORDER_TYPE_STOP_MARKET",
    "REASON_ALARM_A",
    "REASON_ALARM_B",
    "REASON_ALARM_D",
    "REASON_R2_HARD_STOP",
    "REASON_PRICE_STOP",
    "REASON_VELOCITY_RATCHET",
    "REASON_HVP_LOCK",
    "REASON_DIVERGENCE_TRAP",
    "REASON_EOD",
    "REASON_CIRCUIT_BREAKER",
    "REASON_V651_DEEP_STOP",
    "compute_sentinel_limit_price",
    "order_type_for_reason",
    "submit_exit",
]
