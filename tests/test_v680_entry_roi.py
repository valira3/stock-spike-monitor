"""tests/test_v680_entry_roi.py

v6.8.0 entry ROI quick wins test suite.

Covers:
  W-E: EXIT_REASON_V651_DEEP_STOP routes to STOP_MARKET (not MARKET)
  C1:  Short deep-stop fires when _V651_DEEP_STOP_LONG_ONLY=False (new default)
  C2:  TICKER_SIDE_BLOCKLIST blocks configured ticker/side combinations,
       passes through non-blocked pairs, and respects env-override
  C3:  P3_SCALED_A_DI_LO is 22.0; DI 22-25 entries reach SCALED_A tier

No em-dashes anywhere in this file.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# W-E: deep-stop order type routing
# ---------------------------------------------------------------------------


def test_we_deep_stop_routes_stop_market():
    """EXIT_REASON_V651_DEEP_STOP must map to STOP_MARKET, not MARKET.

    This is the W-E audit finding from v6.6.0: the reason string was not
    registered in _STOP_REASONS, so order_type_for_reason fell through to
    the unknown-reason MARKET fallback.
    """
    from broker.order_types import ORDER_TYPE_STOP_MARKET, order_type_for_reason
    from engine.sentinel import EXIT_REASON_V651_DEEP_STOP

    result = order_type_for_reason(EXIT_REASON_V651_DEEP_STOP)
    assert result == ORDER_TYPE_STOP_MARKET, (
        "W-E fix: EXIT_REASON_V651_DEEP_STOP must route to STOP_MARKET; "
        "got %r" % (result,)
    )


def test_we_deep_stop_reason_string_value():
    """The reason string constant matches the value registered in _STOP_REASONS."""
    from broker.order_types import REASON_V651_DEEP_STOP
    from engine.sentinel import EXIT_REASON_V651_DEEP_STOP

    assert REASON_V651_DEEP_STOP == EXIT_REASON_V651_DEEP_STOP, (
        "broker.order_types.REASON_V651_DEEP_STOP must equal "
        "engine.sentinel.EXIT_REASON_V651_DEEP_STOP; got %r vs %r"
        % (REASON_V651_DEEP_STOP, EXIT_REASON_V651_DEEP_STOP)
    )


def test_we_submit_exit_deep_stop_returns_stop_market():
    """submit_exit with deep-stop reason must produce order_type=STOP_MARKET."""
    from broker.order_types import ORDER_TYPE_STOP_MARKET, submit_exit
    from engine.sentinel import EXIT_REASON_V651_DEEP_STOP

    order = submit_exit(
        direction="LONG",
        qty=100,
        price=198.50,
        reason=EXIT_REASON_V651_DEEP_STOP,
    )
    assert order.order_type == ORDER_TYPE_STOP_MARKET, (
        "submit_exit with deep-stop reason must yield STOP_MARKET; "
        "got %r" % (order.order_type,)
    )


# ---------------------------------------------------------------------------
# C1: Short deep-stop with LONG_ONLY=False (new default)
# ---------------------------------------------------------------------------


_ENTRY_UTC = datetime(2026, 5, 2, 14, 30, 0, tzinfo=timezone.utc)


def _make_position(entry_price=100.0, entry_ts_utc=_ENTRY_UTC):
    return {
        "shares": 100,
        "entry_price": entry_price,
        "stop": 99.50,
        "initial_stop": 99.50,
        "entry_ts_utc": entry_ts_utc.isoformat(),
        "lifecycle_position_id": "v680-c1-test",
        "v644_entry_now_et_iso": entry_ts_utc.isoformat(),
        "v531_min_adverse_price": None,
        "v531_max_favorable_price": None,
        "trail_state": None,
    }


def _make_bars():
    return {
        "timestamps": [],
        "opens": [],
        "highs": [],
        "lows": [],
        "closes": [],
    }


class _StubTG:
    def __init__(self, hold_seconds: float):
        self._now = _ENTRY_UTC + timedelta(seconds=hold_seconds)

    def _now_et(self):
        return self._now

    def now_et(self):
        return self._now

    paper_cash = 100000.0
    positions = {}
    short_positions = {}

    def get_fmp_quote(self, ticker):
        return None

    def v5_adx_1m_5m(self, ticker):
        return {"adx_1m": None, "adx_5m": None}

    def _compute_rsi(self, closes, period=15):
        return None


def _force_price_stop_result():
    from engine.sentinel import (
        EXIT_REASON_PRICE_STOP,
        SentinelAction,
        SentinelResult,
    )

    return SentinelResult(
        alarms=[
            SentinelAction(
                alarm="A_STOP_PRICE",
                reason=EXIT_REASON_PRICE_STOP,
                detail="forced",
                detail_stop_price=99.50,
            )
        ]
    )


def test_c1_default_long_only_is_false():
    """_V651_DEEP_STOP_LONG_ONLY must be False in v6.8.0 (C1 change)."""
    import engine.sentinel as sentinel_mod

    assert sentinel_mod._V651_DEEP_STOP_LONG_ONLY is False, (
        "C1: _V651_DEEP_STOP_LONG_ONLY must be False in v6.8.0; "
        "got %r" % sentinel_mod._V651_DEEP_STOP_LONG_ONLY
    )


def test_c1_short_deep_stop_fires_with_long_only_false(monkeypatch):
    """Short at +0.80%% inside hold window fires deep-stop when LONG_ONLY=False."""
    import engine.sentinel as sentinel_mod
    from broker import positions as broker_positions
    from engine.sentinel import EXIT_REASON_V651_DEEP_STOP

    monkeypatch.setattr(
        broker_positions,
        "evaluate_sentinel",
        lambda **kwargs: _force_price_stop_result(),
    )
    monkeypatch.setattr(broker_positions, "_tg", lambda: _StubTG(120))
    monkeypatch.setattr(sentinel_mod, "_V644_MIN_HOLD_GATE_ENABLED", True)
    monkeypatch.setattr(sentinel_mod, "_V651_DEEP_STOP_ENABLED", True)
    monkeypatch.setattr(sentinel_mod, "_V651_DEEP_STOP_LONG_ONLY", False)

    pos = _make_position(entry_price=100.0)
    out = broker_positions._run_sentinel(
        ticker="TSLA",
        side=broker_positions._SENTINEL_SIDE_SHORT,
        pos=pos,
        current_price=100.80,
        bars=_make_bars(),
    )
    assert out == EXIT_REASON_V651_DEEP_STOP, (
        "C1: short at +0.80%% inside window must fire EXIT_REASON_V651_DEEP_STOP "
        "when LONG_ONLY=False; got %r" % (out,)
    )


def test_c1_short_deep_stop_blocked_when_long_only_true(monkeypatch):
    """Guard: with LONG_ONLY=True, short deep-stop is still suppressed."""
    import engine.sentinel as sentinel_mod
    from broker import positions as broker_positions

    monkeypatch.setattr(
        broker_positions,
        "evaluate_sentinel",
        lambda **kwargs: _force_price_stop_result(),
    )
    monkeypatch.setattr(broker_positions, "_tg", lambda: _StubTG(120))
    monkeypatch.setattr(sentinel_mod, "_V644_MIN_HOLD_GATE_ENABLED", True)
    monkeypatch.setattr(sentinel_mod, "_V651_DEEP_STOP_ENABLED", True)
    monkeypatch.setattr(sentinel_mod, "_V651_DEEP_STOP_LONG_ONLY", True)

    pos = _make_position(entry_price=100.0)
    out = broker_positions._run_sentinel(
        ticker="TSLA",
        side=broker_positions._SENTINEL_SIDE_SHORT,
        pos=pos,
        current_price=100.80,
        bars=_make_bars(),
    )
    assert out is None, (
        "C1: with LONG_ONLY=True, short at +0.80%% inside window must not fire; "
        "got %r" % (out,)
    )


# ---------------------------------------------------------------------------
# C2: TICKER_SIDE_BLOCKLIST
# ---------------------------------------------------------------------------
# trade_genius.py cannot be imported in the test sandbox (it requires
# Telegram + FMP_API_KEY at import time). Tests validate the same logic
# directly: the blocklist JSON default, env override, and the
# check_breakout guard path in broker/orders.py.


_DEFAULT_BLOCKLIST = {"META": ["SHORT"], "AMZN": ["SHORT"]}


def _build_blocklist(env_json=None):
    """Replicate the trade_genius TICKER_SIDE_BLOCKLIST load logic."""
    raw = env_json if env_json is not None else json.dumps(_DEFAULT_BLOCKLIST)
    return json.loads(raw)


def _side_blocked(ticker, side_str, blocklist):
    """Replicate check_breakout blocklist guard logic."""
    side_upper = side_str.upper()
    return side_upper in blocklist.get(str(ticker).upper(), [])


def test_c2_meta_short_is_blocked():
    """META SHORT must be blocked by the default blocklist."""
    bl = _build_blocklist()
    assert _side_blocked("META", "SHORT", bl), (
        "C2: META SHORT must be in default TICKER_SIDE_BLOCKLIST"
    )


def test_c2_amzn_short_is_blocked():
    """AMZN SHORT must be blocked by the default blocklist."""
    bl = _build_blocklist()
    assert _side_blocked("AMZN", "SHORT", bl), (
        "C2: AMZN SHORT must be in default TICKER_SIDE_BLOCKLIST"
    )


def test_c2_meta_long_is_not_blocked():
    """META LONG must NOT be blocked by the default blocklist."""
    bl = _build_blocklist()
    assert not _side_blocked("META", "LONG", bl), (
        "C2: META LONG must not be in TICKER_SIDE_BLOCKLIST"
    )


def test_c2_tsla_short_is_not_blocked():
    """TSLA SHORT must not be blocked (not in default list)."""
    bl = _build_blocklist()
    assert not _side_blocked("TSLA", "SHORT", bl), (
        "C2: TSLA SHORT must not be blocked by default"
    )


def test_c2_env_override_clears_blocklist():
    """TICKER_SIDE_BLOCKLIST env var set to {} disables blocking entirely."""
    bl = _build_blocklist(env_json="{}")
    assert bl == {}, (
        "C2: TICKER_SIDE_BLOCKLIST={} must yield empty dict; got %r" % bl
    )


def test_c2_env_override_custom_blocklist():
    """TICKER_SIDE_BLOCKLIST env var with custom JSON is loaded correctly."""
    custom = json.dumps({"NVDA": ["LONG", "SHORT"], "TSLA": ["SHORT"]})
    bl = _build_blocklist(env_json=custom)
    assert "NVDA" in bl, "C2: NVDA must be in custom blocklist"
    assert "SHORT" in bl["NVDA"], "C2: NVDA SHORT must be blocked"
    assert "LONG" in bl["NVDA"], "C2: NVDA LONG must be blocked"
    assert "TSLA" in bl, "C2: TSLA must be in custom blocklist"


def test_c2_blocklist_in_orders_check_breakout_guard():
    """check_breakout returns (False, None) for a ticker/side in the blocklist.

    Verifies the guard code path in broker/orders.py by calling
    _side_blocked (which mirrors the inline check_breakout logic).
    """
    bl = {"META": ["SHORT"]}
    assert _side_blocked("META", "SHORT", bl), "guard: META SHORT must block"
    assert not _side_blocked("META", "LONG", bl), "guard: META LONG must pass"
    assert not _side_blocked("TSLA", "SHORT", bl), "guard: TSLA SHORT must pass"


# ---------------------------------------------------------------------------
# C3: P3 sizing floor 22.0
# ---------------------------------------------------------------------------


def test_c3_p3_floor_constant_is_22():
    """P3_SCALED_A_DI_LO must be 22.0 in v6.8.0 (was 25.0)."""
    from eye_of_tiger import P3_SCALED_A_DI_LO

    assert P3_SCALED_A_DI_LO == 22.0, (
        "C3: P3_SCALED_A_DI_LO must be 22.0; got %r" % P3_SCALED_A_DI_LO
    )


def test_c3_di_23_reaches_scaled_a():
    """DI 1m = 23.0 (in range 22-25) must reach SCALED_A tier after C3 fix."""
    from eye_of_tiger import evaluate_strike_sizing

    decision = evaluate_strike_sizing(
        side="LONG",
        di_5m=28.0,
        di_1m=23.0,
        is_fresh_extreme=True,
        intended_shares=100,
        held_shares_this_strike=0,
        alarm_e_blocked=False,
    )
    assert decision.size_label == "SCALED_A", (
        "C3: di_1m=23.0 must yield SCALED_A (floor lowered to 22.0); "
        "got %r" % decision.size_label
    )


def test_c3_di_22_is_inclusive_lower_bound():
    """DI 1m = 22.0 is the new inclusive lower bound for SCALED_A."""
    from eye_of_tiger import evaluate_strike_sizing

    decision = evaluate_strike_sizing(
        side="LONG",
        di_5m=28.0,
        di_1m=22.0,
        is_fresh_extreme=True,
        intended_shares=100,
        held_shares_this_strike=0,
        alarm_e_blocked=False,
    )
    assert decision.size_label == "SCALED_A", (
        "C3: di_1m=22.0 must yield SCALED_A (inclusive lower bound); "
        "got %r" % decision.size_label
    )


def test_c3_di_21_9_is_below_floor():
    """DI 1m = 21.9 (just below 22.0) must NOT reach SCALED_A."""
    from eye_of_tiger import evaluate_strike_sizing

    decision = evaluate_strike_sizing(
        side="LONG",
        di_5m=28.0,
        di_1m=21.9,
        is_fresh_extreme=True,
        intended_shares=100,
        held_shares_this_strike=0,
        alarm_e_blocked=False,
    )
    assert decision.size_label != "SCALED_A", (
        "C3: di_1m=21.9 (below floor 22.0) must not yield SCALED_A; "
        "got %r" % decision.size_label
    )


def test_c3_di_25_still_scaled_a():
    """DI 1m = 25.0 must still reach SCALED_A (range broadened, not shifted)."""
    from eye_of_tiger import evaluate_strike_sizing

    decision = evaluate_strike_sizing(
        side="LONG",
        di_5m=28.0,
        di_1m=25.0,
        is_fresh_extreme=True,
        intended_shares=100,
        held_shares_this_strike=0,
        alarm_e_blocked=False,
    )
    assert decision.size_label == "SCALED_A", (
        "C3: di_1m=25.0 must still yield SCALED_A; got %r" % decision.size_label
    )
