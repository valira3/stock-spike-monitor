"""v6.3.1 regression: broker._run_sentinel must thread position_id and
now_et into evaluate_sentinel.

The v6.1.0 stateful EMA-cross path (engine/sentinel.py around line 697) is
gated on ``position_id is not None and _V610_EMA_CONFIRM_ENABLED``. Before
v6.3.1, broker/positions.py:458 omitted both arguments, so the path never
executed in production or backtest. The v6.3.0 noise-cross filter and the
v6.1.0 lunch-chop suppression both lived inside that gated path and were
silently dead code.

This test forces a Sentinel B cross condition and asserts the per-position
``_ema_cross_pending`` counter increments. If the wiring regresses, the
counter stays at zero and the assertion fails.
"""

from __future__ import annotations

import time as _time

import pytest

import engine.sentinel as sentinel_mod
from engine.sentinel import (
    SIDE_LONG,
    reset_ema_cross_pending,
)


@pytest.fixture(autouse=True)
def _clear_ema_state():
    """Clear the module-level pending counter between tests."""
    reset_ema_cross_pending()
    yield
    reset_ema_cross_pending()


def _build_position(lifecycle_id: str = "test-pid-v631") -> dict:
    """Construct a minimal long position with a lifecycle_position_id.

    Mirrors the post-fill shape produced by broker/orders.py: shares,
    entry_price, stop, lifecycle_position_id, and a few v5.x fields the
    sentinel reads defensively.
    """
    return {
        "shares": 100,
        "entry_price": 100.00,
        "stop": 99.00,
        "initial_stop": 99.00,
        "lifecycle_position_id": lifecycle_id,
        "v531_min_adverse_price": None,
        "v531_max_favorable_price": None,
        "trail_state": None,
    }


def _stub_bars_with_cross() -> dict:
    """Construct a 1m bars dict that produces a 5m close BELOW 5m 9-EMA.

    Shape matches the engine.bars contract: 'timestamps' (epoch seconds),
    'opens', 'highs', 'lows', 'closes'. Synthetic series stays flat at
    100 for ~75 minutes (15 closed 5m buckets) then tilts down by 0.5/m
    in the last 5 minutes so the most recent closed 5m bucket has a
    close around 99.0 while EMA9 is still near 99.9, producing the LONG
    cross condition (close < ema9).
    """
    n = 80
    base = 100.0
    closes = [base] * (n - 5) + [99.5, 99.0, 98.5, 98.0, 97.5]
    opens = [base] * (n - 5) + [100.0, 99.5, 99.0, 98.5, 98.0]
    highs = [c + 0.1 for c in closes]
    lows = [c - 0.1 for c in closes]
    # Anchor timestamps on a clean 5m boundary so bucket arithmetic is
    # deterministic. Start 80 minutes ago at the start of the minute.
    base_ts = int(_time.time()) // 60 * 60 - n * 60
    timestamps = [base_ts + i * 60 for i in range(n)]
    return {
        "timestamps": timestamps,
        "opens": opens,
        "highs": highs,
        "lows": lows,
        "closes": closes,
    }


def test_v631_position_id_threads_into_evaluate_sentinel(monkeypatch):
    """The v6.1.0 stateful counter must increment when broker calls run_sentinel.

    This is the v6.3.1 wiring regression: the only way the counter bumps is
    if check_alarm_b enters the position_id path, which requires
    broker/positions.py to forward position_id to evaluate_sentinel.
    """
    # v6.4.0: B is disabled by default; this test asserts B path wiring,
    # so force-enable for the test.
    monkeypatch.setattr(sentinel_mod, "ALARM_B_ENABLED", True)
    from broker import positions as broker_positions

    pos = _build_position("v631-test-pid-A")
    bars = _stub_bars_with_cross()

    # Stub the trade_genius module surface that _run_sentinel touches via
    # _tg(). We only need now_et and the live positions store.
    class _StubTG:
        def now_et(self):
            from datetime import datetime
            from engine.timing import ET
            # Pick 14:00 ET to avoid the 11:30-13:00 lunch-chop window so
            # this test isolates the wiring regression rather than the
            # lunch-suppression behavior.
            return datetime.now(tz=ET).replace(hour=14, minute=0, second=0, microsecond=0)

    monkeypatch.setattr(broker_positions, "_tg", lambda: _StubTG())

    # Counter starts clean from the fixture.
    assert sentinel_mod._ema_cross_pending.get("v631-test-pid-A", 0) == 0

    # Drive the sentinel: a LONG position with a 5m close below the 5m 9-EMA
    # should bump the counter by exactly 1 on the first call.
    broker_positions._run_sentinel(
        ticker="TEST",
        side=broker_positions._SENTINEL_SIDE_LONG,
        pos=pos,
        current_price=98.0,
        bars=bars,
    )

    count = sentinel_mod._ema_cross_pending.get("v631-test-pid-A", 0)
    assert count == 1, (
        f"v6.3.1 wiring regression: _ema_cross_pending should be 1 after one "
        f"cross tick when position_id is forwarded; got {count}. This means "
        f"broker/positions.py is not threading position_id into "
        f"evaluate_sentinel and the v6.1.0 stateful path is dead code again."
    )


def test_v631_no_position_id_falls_through_to_legacy(monkeypatch):
    """Back-compat: if a position somehow lacks a lifecycle_position_id, the
    sentinel must still run (legacy 2-bar path) without raising.
    """
    from broker import positions as broker_positions

    pos = _build_position(lifecycle_id="")
    pos.pop("lifecycle_position_id", None)
    bars = _stub_bars_with_cross()

    class _StubTG:
        def now_et(self):
            from datetime import datetime
            from engine.timing import ET
            return datetime.now(tz=ET).replace(hour=14, minute=0, second=0, microsecond=0)

    monkeypatch.setattr(broker_positions, "_tg", lambda: _StubTG())

    # Should not raise; nothing to assert beyond clean execution.
    broker_positions._run_sentinel(
        ticker="TEST",
        side=broker_positions._SENTINEL_SIDE_LONG,
        pos=pos,
        current_price=98.0,
        bars=bars,
    )
