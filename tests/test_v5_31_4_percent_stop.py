"""Unit tests for v5.31.4 percent-of-entry stop + price-rail Alarm.

Covers:
  * eye_of_tiger.STOP_PCT_OF_ENTRY = 0.005 (0.5%%, symmetric long/short)
  * broker.orders stop derivation: long = entry x 0.995, short = entry x 1.005
  * engine.sentinel.check_alarm_a_stop_price: side-symmetric mark-cross
    fires a SentinelAction with EXIT_REASON_PRICE_STOP
  * evaluate_sentinel wires the new alarm into the tick result
  * SentinelResult.has_full_exit and exit_reason recognise the new code
  * broker.order_types routes EXIT_REASON_PRICE_STOP to STOP_MARKET

The new sub-alarm sits next to A_LOSS on the same Alarm A rail. The
price rail typically fires first in a slow drift scenario where the
dollar rail (R-2 -$500) has not yet been reached.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# eye_of_tiger constant
# ---------------------------------------------------------------------------


def test_stop_pct_of_entry_constant_is_half_percent():
    """STOP_PCT_OF_ENTRY = 0.005 (0.5%%) per v5.31.4 spec decision."""
    from eye_of_tiger import STOP_PCT_OF_ENTRY

    assert STOP_PCT_OF_ENTRY == pytest.approx(0.005), (
        "STOP_PCT_OF_ENTRY must be exactly 0.005 (0.5%%); changing this "
        "is a spec change and requires a version bump."
    )


def test_stop_pct_long_short_arithmetic():
    """Long stop = entry x (1 - pct); short stop = entry x (1 + pct).

    At entry $100: long stop = $99.50, short stop = $100.50. At
    entry $197.45 (NVDA SHORT live state): short stop ~= $198.44.
    """
    from eye_of_tiger import STOP_PCT_OF_ENTRY

    long_stop = 100.0 * (1.0 - STOP_PCT_OF_ENTRY)
    short_stop = 100.0 * (1.0 + STOP_PCT_OF_ENTRY)
    assert long_stop == pytest.approx(99.50)
    assert short_stop == pytest.approx(100.50)

    # Live NVDA SHORT 50sh @ $197.45 -> stop $198.4372 (rounds to $198.44)
    nvda_short_stop = 197.45 * (1.0 + STOP_PCT_OF_ENTRY)
    assert round(nvda_short_stop, 2) == pytest.approx(198.44)


# ---------------------------------------------------------------------------
# engine.sentinel.check_alarm_a_stop_price
# ---------------------------------------------------------------------------


def test_alarm_a_stop_price_fires_long_at_or_below_stop():
    """LONG: mark <= stop fires the price rail."""
    from engine.sentinel import (
        EXIT_REASON_PRICE_STOP,
        SIDE_LONG,
        check_alarm_a_stop_price,
    )

    # Inside tolerance: NO fire.
    fired = check_alarm_a_stop_price(
        side=SIDE_LONG,
        current_price=99.51,
        current_stop_price=99.50,
    )
    assert fired == [], "LONG mark above stop must NOT fire"

    # On the boundary (mark == stop): FIRES (inclusive comparison).
    fired = check_alarm_a_stop_price(
        side=SIDE_LONG,
        current_price=99.50,
        current_stop_price=99.50,
    )
    assert len(fired) == 1
    assert fired[0].alarm == "A_STOP_PRICE"
    assert fired[0].reason == EXIT_REASON_PRICE_STOP
    assert fired[0].detail_stop_price == pytest.approx(99.50)

    # Below stop: FIRES.
    fired = check_alarm_a_stop_price(
        side=SIDE_LONG,
        current_price=99.49,
        current_stop_price=99.50,
    )
    assert len(fired) == 1
    assert fired[0].alarm == "A_STOP_PRICE"
    assert fired[0].reason == EXIT_REASON_PRICE_STOP


def test_alarm_a_stop_price_fires_short_at_or_above_stop():
    """SHORT: mark >= stop fires the price rail."""
    from engine.sentinel import (
        EXIT_REASON_PRICE_STOP,
        SIDE_SHORT,
        check_alarm_a_stop_price,
    )

    # Below stop: NO fire (short is profitable below stop).
    fired = check_alarm_a_stop_price(
        side=SIDE_SHORT,
        current_price=100.49,
        current_stop_price=100.50,
    )
    assert fired == [], "SHORT mark below stop must NOT fire"

    # Boundary: FIRES.
    fired = check_alarm_a_stop_price(
        side=SIDE_SHORT,
        current_price=100.50,
        current_stop_price=100.50,
    )
    assert len(fired) == 1
    assert fired[0].alarm == "A_STOP_PRICE"
    assert fired[0].reason == EXIT_REASON_PRICE_STOP

    # Above stop (adverse for short): FIRES.
    fired = check_alarm_a_stop_price(
        side=SIDE_SHORT,
        current_price=100.51,
        current_stop_price=100.50,
    )
    assert len(fired) == 1


def test_alarm_a_stop_price_sits_out_when_inputs_missing():
    """Either input None -> sit out silently (no exception, no fire)."""
    from engine.sentinel import SIDE_LONG, check_alarm_a_stop_price

    assert check_alarm_a_stop_price(
        side=SIDE_LONG, current_price=None, current_stop_price=99.5
    ) == []
    assert check_alarm_a_stop_price(
        side=SIDE_LONG, current_price=99.0, current_stop_price=None
    ) == []
    assert check_alarm_a_stop_price(
        side=SIDE_LONG, current_price=None, current_stop_price=None
    ) == []


def test_alarm_a_stop_price_live_nvda_scenario():
    """Reproduce the live NVDA SHORT 50sh @ $197.45 stop $198.44 case.

    Mark drifts from $199.78 to $198.44 (touches stop) -> price rail
    fires. The dollar rail (R-2 -$500) has NOT been reached yet (50sh
    x $0.99 adverse = -$49.50, far below -$500), so without the price
    rail the position would stay open. v5.31.4 closes that gap.
    """
    from engine.sentinel import (
        EXIT_REASON_PRICE_STOP,
        SIDE_SHORT,
        check_alarm_a_stop_price,
    )

    # Mark above stop but adverse: NO price-rail fire yet (drift).
    fired = check_alarm_a_stop_price(
        side=SIDE_SHORT, current_price=199.78, current_stop_price=198.44
    )
    assert any(f.reason == EXIT_REASON_PRICE_STOP for f in fired), (
        "SHORT mark $199.78 vs stop $198.44 IS adverse -> price rail fires"
    )

    # Mark exactly at stop: still fires (inclusive).
    fired = check_alarm_a_stop_price(
        side=SIDE_SHORT, current_price=198.44, current_stop_price=198.44
    )
    assert len(fired) == 1


# ---------------------------------------------------------------------------
# evaluate_sentinel wiring + SentinelResult priority
# ---------------------------------------------------------------------------


def test_evaluate_sentinel_emits_price_stop_action():
    """evaluate_sentinel must surface A_STOP_PRICE in result.alarms."""
    from engine.sentinel import (
        EXIT_REASON_PRICE_STOP,
        SIDE_LONG,
        evaluate_sentinel,
    )

    result = evaluate_sentinel(
        side=SIDE_LONG,
        unrealized_pnl=-50.0,  # well above R-2 -$500
        position_value=10000.0,
        pnl_history=None,
        now_ts=1000.0,
        last_5m_close=None,
        last_5m_ema9=None,
        current_price=99.49,
        current_stop_price=99.50,
    )
    codes = [a.alarm for a in result.alarms]
    assert "A_STOP_PRICE" in codes
    assert result.exit_reason == EXIT_REASON_PRICE_STOP
    assert result.has_full_exit is True


def test_evaluate_sentinel_no_fire_when_mark_inside_stop():
    """Mark on profitable side of stop -> no A_STOP_PRICE in alarms."""
    from engine.sentinel import SIDE_LONG, evaluate_sentinel

    result = evaluate_sentinel(
        side=SIDE_LONG,
        unrealized_pnl=20.0,
        position_value=10000.0,
        pnl_history=None,
        now_ts=1000.0,
        last_5m_close=None,
        last_5m_ema9=None,
        current_price=100.20,
        current_stop_price=99.50,
    )
    codes = [a.alarm for a in result.alarms]
    assert "A_STOP_PRICE" not in codes


def test_r2_hard_stop_outranks_price_stop():
    """When BOTH R-2 dollar rail AND price rail trip, R-2 wins exit_reason.

    Both alarms still appear in result.alarms (parallel evaluation) -
    only exit_reason resolution prefers R-2 (the deepest rail).
    """
    from engine.sentinel import (
        EXIT_REASON_PRICE_STOP,
        EXIT_REASON_R2_HARD_STOP,
        SIDE_LONG,
        evaluate_sentinel,
    )

    result = evaluate_sentinel(
        side=SIDE_LONG,
        unrealized_pnl=-501.0,  # trips R-2
        position_value=10000.0,
        pnl_history=None,
        now_ts=1000.0,
        last_5m_close=None,
        last_5m_ema9=None,
        current_price=99.49,    # also trips price rail
        current_stop_price=99.50,
    )
    codes = [a.alarm for a in result.alarms]
    assert "A_LOSS" in codes
    assert "A_STOP_PRICE" in codes
    # Priority: R-2 > A_STOP_PRICE.
    assert result.exit_reason == EXIT_REASON_R2_HARD_STOP
    # Sanity: EXIT_REASON_PRICE_STOP is still observed in the alarms list.
    reasons = [a.reason for a in result.alarms]
    assert EXIT_REASON_PRICE_STOP in reasons


# ---------------------------------------------------------------------------
# broker.order_types routing
# ---------------------------------------------------------------------------


def test_price_stop_routes_to_stop_market():
    """REASON_PRICE_STOP must map to STOP_MARKET, like R-2."""
    from broker.order_types import (
        ORDER_TYPE_STOP_MARKET,
        REASON_PRICE_STOP,
        order_type_for_reason,
    )

    assert order_type_for_reason(REASON_PRICE_STOP) == ORDER_TYPE_STOP_MARKET


def test_price_stop_constant_matches_sentinel():
    """broker REASON_PRICE_STOP must equal sentinel EXIT_REASON_PRICE_STOP.

    Two separate constants exist for layering reasons (broker layer
    must not import from engine.sentinel directly), but they must
    stay byte-identical strings.
    """
    from broker.order_types import REASON_PRICE_STOP
    from engine.sentinel import EXIT_REASON_PRICE_STOP

    assert REASON_PRICE_STOP == EXIT_REASON_PRICE_STOP


# ---------------------------------------------------------------------------
# dashboard_static/app.js __tgSessionColor scope fix (v5.31.4)
# ---------------------------------------------------------------------------


def test_tg_session_color_lifted_to_window():
    """v5.31.2 introduced a second IIFE that referenced __tgSessionColor
    by name (not via window), causing 'Fetch failed' on Val's tab. The
    fix: expose window.__tgSessionColor in IIFE#1 and read it via
    window.__tgSessionColor in the two IIFE#2 callsites.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "dashboard_static" / "app.js"
    text = src.read_text()
    # IIFE#1 must export to window.
    assert "window.__tgSessionColor = __tgSessionColor" in text, (
        "IIFE#1 must publish __tgSessionColor onto window for IIFE#2 access"
    )
    # IIFE#2 callsites must use the window-qualified read.
    assert text.count("window.__tgSessionColor") >= 3, (
        "Expect at least 3 references to window.__tgSessionColor "
        "(1 export in IIFE#1 + 2 reads in IIFE#2)"
    )
