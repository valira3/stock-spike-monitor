"""v6.15.0 broker fidelity: order_types routing + STOP_LIMIT math.

Covers:
  - REASON_PRICE_STOP (sentinel_a_stop_price) now routes to STOP_LIMIT
  - R-2 / velocity / V651 deep-stop stay STOP_MARKET
  - LIMIT reasons (A-A / A-B / A-D / HVP / DIVERGENCE) unchanged
  - MARKET reasons (EOD / circuit breaker) unchanged
  - compute_stop_limit_price: LONG = stop * (1 - bps/10_000),
    SHORT = stop * (1 + bps/10_000), default 30 bps, configurable.
  - bad inputs raise ValueError.
"""

from __future__ import annotations

import pytest


def test_reason_price_stop_routes_stop_limit():
    """v6.15.0 \u2014 sentinel_a_stop_price -> STOP_LIMIT."""
    from broker.order_types import (
        ORDER_TYPE_STOP_LIMIT,
        REASON_PRICE_STOP,
        order_type_for_reason,
    )

    assert order_type_for_reason(REASON_PRICE_STOP) == ORDER_TYPE_STOP_LIMIT


def test_r2_hard_stop_still_stop_market():
    """R-2 (-$500 hard floor) still STOP_MARKET. v6.15.0 only moved
    the protective price-rail; the dollar-loss hard stop must always
    cross the book at any price (the loss is already capped)."""
    from broker.order_types import (
        ORDER_TYPE_STOP_MARKET,
        REASON_R2_HARD_STOP,
        order_type_for_reason,
    )

    assert order_type_for_reason(REASON_R2_HARD_STOP) == ORDER_TYPE_STOP_MARKET


def test_velocity_ratchet_still_stop_market():
    from broker.order_types import (
        ORDER_TYPE_STOP_MARKET,
        REASON_VELOCITY_RATCHET,
        order_type_for_reason,
    )

    assert order_type_for_reason(REASON_VELOCITY_RATCHET) == ORDER_TYPE_STOP_MARKET


def test_v651_deep_stop_still_stop_market():
    from broker.order_types import (
        ORDER_TYPE_STOP_MARKET,
        REASON_V651_DEEP_STOP,
        order_type_for_reason,
    )

    assert order_type_for_reason(REASON_V651_DEEP_STOP) == ORDER_TYPE_STOP_MARKET


def test_alarm_a_b_d_still_limit():
    """Sentinel-driven A-A / A-B / A-D defensive exits stay LIMIT."""
    from broker.order_types import (
        ORDER_TYPE_LIMIT,
        REASON_ALARM_A,
        REASON_ALARM_B,
        REASON_ALARM_D,
        order_type_for_reason,
    )

    assert order_type_for_reason(REASON_ALARM_A) == ORDER_TYPE_LIMIT
    assert order_type_for_reason(REASON_ALARM_B) == ORDER_TYPE_LIMIT
    assert order_type_for_reason(REASON_ALARM_D) == ORDER_TYPE_LIMIT


def test_eod_and_circuit_breaker_still_market():
    from broker.order_types import (
        ORDER_TYPE_MARKET,
        REASON_CIRCUIT_BREAKER,
        REASON_EOD,
        order_type_for_reason,
    )

    assert order_type_for_reason(REASON_EOD) == ORDER_TYPE_MARKET
    assert order_type_for_reason(REASON_CIRCUIT_BREAKER) == ORDER_TYPE_MARKET


def test_unknown_reason_falls_back_to_market():
    """Spec preserves v5.13.7 unknown-reason fallback (MARKET)."""
    from broker.order_types import ORDER_TYPE_MARKET, order_type_for_reason

    assert order_type_for_reason("totally_made_up_reason") == ORDER_TYPE_MARKET


# ---------------------------------------------------------------------------
# compute_stop_limit_price math
# ---------------------------------------------------------------------------


def test_compute_stop_limit_price_long_default_30bps():
    """LONG exit: limit BELOW stop. 30bps == 0.30%."""
    from broker.order_types import compute_stop_limit_price

    # stop = $100.00, 30bps slip -> $99.70
    assert compute_stop_limit_price("LONG", 100.0) == pytest.approx(99.70)
    # stop = $263.41 (live AVGO-like) -> $262.6197
    assert compute_stop_limit_price("LONG", 263.41) == pytest.approx(263.41 * 0.997)


def test_compute_stop_limit_price_short_default_30bps():
    """SHORT exit: limit ABOVE stop."""
    from broker.order_types import compute_stop_limit_price

    assert compute_stop_limit_price("SHORT", 100.0) == pytest.approx(100.30)
    assert compute_stop_limit_price("SHORT", 198.44) == pytest.approx(198.44 * 1.003)


def test_compute_stop_limit_price_custom_slip_bps():
    """slip_bps is configurable; 50bps == 0.50% slip cap."""
    from broker.order_types import compute_stop_limit_price

    assert compute_stop_limit_price("LONG", 100.0, slip_bps=50) == pytest.approx(99.50)
    assert compute_stop_limit_price("SHORT", 100.0, slip_bps=50) == pytest.approx(100.50)
    # 0bps degenerates to stop == limit (effectively STOP at LIMIT)
    assert compute_stop_limit_price("LONG", 100.0, slip_bps=0) == pytest.approx(100.0)


def test_compute_stop_limit_price_rejects_zero_or_negative():
    from broker.order_types import compute_stop_limit_price

    with pytest.raises(ValueError):
        compute_stop_limit_price("LONG", 0.0)
    with pytest.raises(ValueError):
        compute_stop_limit_price("LONG", -1.5)


def test_compute_stop_limit_price_rejects_unknown_side():
    from broker.order_types import compute_stop_limit_price

    with pytest.raises(ValueError):
        compute_stop_limit_price("BOTH", 100.0)
    with pytest.raises(ValueError):
        compute_stop_limit_price("", 100.0)


def test_stop_limit_constant_string():
    """ORDER_TYPE_STOP_LIMIT must be the literal string 'STOP_LIMIT'."""
    from broker.order_types import ORDER_TYPE_STOP_LIMIT

    assert ORDER_TYPE_STOP_LIMIT == "STOP_LIMIT"
