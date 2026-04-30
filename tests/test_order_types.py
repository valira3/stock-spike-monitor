"""v5.13.0 PR 6 — Order type tests.

Asserts the spec-mandated order-type mapping (STRATEGY.md §3 ORDER
TYPE SPECIFICATIONS) is implemented in ``broker.order_types`` and is
consistent with the action codes emitted by ``engine.titan_grip``
and ``engine.sentinel``. Long-side and short-side scenarios are both
covered.
"""

from __future__ import annotations

import pytest

from broker.order_types import (
    ExitOrder,
    ORDER_TYPE_HARVEST,
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    ORDER_TYPE_STOP,
    ORDER_TYPE_STOP_MARKET,
    REASON_ALARM_A,
    REASON_ALARM_B,
    REASON_CIRCUIT_BREAKER,
    REASON_EOD,
    REASON_RATCHET,
    REASON_RUNNER_EXIT,
    REASON_STAGE1_HARVEST,
    REASON_STAGE3_HARVEST,
    order_type_for_reason,
    submit_exit,
)


# ---------------------------------------------------------------------------
# Bucket constants
# ---------------------------------------------------------------------------


def test_bucket_constants_are_spec_literal():
    """ORDER_TYPE_HARVEST is LIMIT; ORDER_TYPE_STOP is STOP_MARKET."""
    assert ORDER_TYPE_HARVEST == ORDER_TYPE_LIMIT == "LIMIT"
    assert ORDER_TYPE_STOP == ORDER_TYPE_STOP_MARKET == "STOP_MARKET"
    assert ORDER_TYPE_MARKET == "MARKET"


# ---------------------------------------------------------------------------
# Reason → order-type mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", [REASON_STAGE1_HARVEST, REASON_STAGE3_HARVEST])
def test_harvest_reasons_map_to_limit(reason):
    """Stage 1 + Stage 3 harvests → LIMIT (spec §3)."""
    assert order_type_for_reason(reason) == ORDER_TYPE_LIMIT


@pytest.mark.parametrize(
    "reason",
    [REASON_ALARM_A, REASON_ALARM_B, REASON_RATCHET, REASON_RUNNER_EXIT],
)
def test_defensive_stop_reasons_map_to_stop_market(reason):
    """Alarms A/B + ratchet + runner exit → STOP_MARKET (spec §3)."""
    assert order_type_for_reason(reason) == ORDER_TYPE_STOP_MARKET


@pytest.mark.parametrize("reason", [REASON_EOD, REASON_CIRCUIT_BREAKER])
def test_eod_and_circuit_breaker_map_to_market(reason):
    """EOD flush + daily circuit breaker → MARKET (spec §3)."""
    assert order_type_for_reason(reason) == ORDER_TYPE_MARKET


def test_unknown_reason_falls_back_to_market():
    """Unknown reasons default to MARKET — historic close_position behavior."""
    assert order_type_for_reason("unknown_reason") == ORDER_TYPE_MARKET


# ---------------------------------------------------------------------------
# submit_exit — long and short
# ---------------------------------------------------------------------------


def test_submit_exit_long_stage1_harvest_is_limit():
    """Long Stage 1 harvest at OR_High + 0.93% target → LIMIT order."""
    order = submit_exit("LONG", qty=25, price=101.86, reason=REASON_STAGE1_HARVEST)
    assert order.direction == "LONG"
    assert order.qty == 25
    assert order.price == pytest.approx(101.86)
    assert order.order_type == ORDER_TYPE_LIMIT
    assert order.reason == REASON_STAGE1_HARVEST


def test_submit_exit_long_stage3_harvest_is_limit():
    """Long Stage 3 harvest at OR_High + 1.88% target → LIMIT order."""
    order = submit_exit("LONG", qty=25, price=102.81, reason=REASON_STAGE3_HARVEST)
    assert order.order_type == ORDER_TYPE_LIMIT


def test_submit_exit_long_alarm_a_is_stop_market():
    """Long Alarm A_LOSS (-$500 hard floor) → STOP MARKET, not plain MARKET."""
    order = submit_exit("LONG", qty=100, price=99.50, reason=REASON_ALARM_A)
    assert order.order_type == ORDER_TYPE_STOP_MARKET


def test_submit_exit_long_alarm_b_is_stop_market():
    """Long Alarm B (5m close < 9-EMA) → STOP MARKET."""
    order = submit_exit("LONG", qty=75, price=99.80, reason=REASON_ALARM_B)
    assert order.order_type == ORDER_TYPE_STOP_MARKET


def test_submit_exit_long_runner_is_stop_market():
    """Long Stage 4 runner exit on trail hit → STOP MARKET."""
    order = submit_exit("LONG", qty=50, price=101.30, reason=REASON_RUNNER_EXIT)
    assert order.order_type == ORDER_TYPE_STOP_MARKET


def test_submit_exit_short_stage1_harvest_is_limit():
    """Short Stage 1 harvest at OR_Low - 0.93% target → LIMIT (BUY-COVER)."""
    order = submit_exit("SHORT", qty=25, price=98.14, reason=REASON_STAGE1_HARVEST)
    assert order.direction == "SHORT"
    assert order.order_type == ORDER_TYPE_LIMIT


def test_submit_exit_short_alarm_a_is_stop_market():
    """Short Alarm A (e.g. +1%/min spike) → STOP MARKET (BUY-STOP above entry)."""
    order = submit_exit("SHORT", qty=100, price=100.50, reason=REASON_ALARM_A)
    assert order.direction == "SHORT"
    assert order.order_type == ORDER_TYPE_STOP_MARKET


def test_submit_exit_eod_is_market():
    """EOD flush → MARKET (spec §3 EOD)."""
    order = submit_exit("LONG", qty=50, price=0.0, reason=REASON_EOD)
    assert order.order_type == ORDER_TYPE_MARKET


def test_submit_exit_circuit_breaker_is_market():
    """Daily circuit breaker → MARKET (spec §3 DAILY CIRCUIT BREAKER)."""
    order = submit_exit("LONG", qty=50, price=0.0, reason=REASON_CIRCUIT_BREAKER)
    assert order.order_type == ORDER_TYPE_MARKET


def test_submit_exit_rejects_zero_qty():
    """qty must be > 0."""
    with pytest.raises(ValueError, match="qty"):
        submit_exit("LONG", qty=0, price=100.0, reason=REASON_EOD)


def test_submit_exit_rejects_bad_direction():
    """direction must be LONG or SHORT."""
    with pytest.raises(ValueError, match="direction"):
        submit_exit("LONGISH", qty=10, price=100.0, reason=REASON_EOD)


# v5.16.0: engine.titan_grip cross-check tests removed with the shim itself.
# The harvest reason constants live in broker.order_types only; the velocity
# ratchet's exit reason is sentinel_velocity_ratchet, not C2_RATCHET.


def test_exit_order_dataclass_is_frozen():
    """ExitOrder is immutable — callers can't mutate after construction."""
    order = submit_exit("LONG", qty=10, price=100.0, reason=REASON_EOD)
    with pytest.raises(Exception):  # FrozenInstanceError
        order.qty = 999  # type: ignore[misc]
    assert isinstance(order, ExitOrder)
