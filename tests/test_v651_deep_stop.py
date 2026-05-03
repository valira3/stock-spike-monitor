"""tests/test_v651_deep_stop.py

v6.5.1 deep-stop during min_hold window test suite.

Covers:
  1. deep_stop_long_breach_inside_window
  2. deep_stop_short_breach_inside_window
  3. deep_stop_threshold_not_breached
  4. deep_stop_outside_window
  5. deep_stop_disabled_flag
  6. deep_stop_priority_below_r2
  7. deep_stop_dashboard_log_emitted
  8. deep_stop_default_pct_value

No em-dashes anywhere in this file.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

import engine.sentinel as sentinel_mod
from engine.sentinel import (
    EXIT_REASON_PRICE_STOP,
    EXIT_REASON_R2_HARD_STOP,
    EXIT_REASON_V651_DEEP_STOP,
    SentinelAction,
    SentinelResult,
)


_ENTRY_UTC = datetime(2026, 5, 2, 14, 30, 0, tzinfo=timezone.utc)


def _make_position(entry_price=100.0, entry_ts_utc=_ENTRY_UTC):
    """Build a minimal position dict suitable for _run_sentinel."""
    return {
        "shares": 100,
        "entry_price": entry_price,
        "stop": 99.50,
        "initial_stop": 99.50,
        "entry_ts_utc": entry_ts_utc.isoformat(),
        "lifecycle_position_id": "v651-test-pid",
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
    """Minimal trade_genius stub; only _now_et() is needed by the gate."""

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


def _force_full_exit_result(reason: str) -> SentinelResult:
    """Build a SentinelResult with has_full_exit=True for the given reason."""
    alarm_code = "A_STOP_PRICE" if reason == EXIT_REASON_PRICE_STOP else "A_LOSS"
    return SentinelResult(
        alarms=[
            SentinelAction(
                alarm=alarm_code,
                reason=reason,
                detail="forced",
                detail_stop_price=99.50,
            )
        ]
    )


@pytest.fixture
def patched_broker(monkeypatch):
    """Patch broker.positions so tests can drive _run_sentinel directly.

    Returns a callable _install(reason, hold_seconds, ...) that configures
    the mocks and returns the broker_positions module.
    """
    from broker import positions as broker_positions

    def _install(
        reason: str,
        hold_seconds: float,
        gate_enabled: bool = True,
        deep_stop_enabled: bool = True,
        entry_ts=_ENTRY_UTC,
    ):
        monkeypatch.setattr(
            broker_positions,
            "evaluate_sentinel",
            lambda **kwargs: _force_full_exit_result(reason),
        )
        monkeypatch.setattr(broker_positions, "_tg", lambda: _StubTG(hold_seconds))
        monkeypatch.setattr(sentinel_mod, "_V644_MIN_HOLD_GATE_ENABLED", gate_enabled)
        monkeypatch.setattr(
            sentinel_mod, "_V651_DEEP_STOP_ENABLED", deep_stop_enabled
        )
        return broker_positions

    return _install


# ---------------------------------------------------------------------------
# Test 1: long breach inside window fires deep-stop
# ---------------------------------------------------------------------------


def test_deep_stop_long_breach_inside_window(patched_broker):
    """Long entry @100, mark=99.20 (-0.80%) at hold=120s.

    Mark is below entry * (1 - 0.0075) = 99.25, so deep-stop fires.
    """
    bp = patched_broker(EXIT_REASON_PRICE_STOP, hold_seconds=120)
    pos = _make_position(entry_price=100.0)
    out = bp._run_sentinel(
        ticker="TEST",
        side=bp._SENTINEL_SIDE_LONG,
        pos=pos,
        current_price=99.20,
        bars=_make_bars(),
    )
    assert out == EXIT_REASON_V651_DEEP_STOP, (
        "v6.5.1: long at -0.80%% inside window must return EXIT_REASON_V651_DEEP_STOP; got %r" % (out,)
    )


# ---------------------------------------------------------------------------
# Test 2: short breach inside window fires deep-stop
# ---------------------------------------------------------------------------


def test_deep_stop_short_breach_inside_window_blocked_long_only(patched_broker):
    """Short entry @100, mark=100.80 (+0.80%) at hold=120s.

    With _V651_DEEP_STOP_LONG_ONLY=True (default), shorts do NOT fire the
    deep-stop, so the v6.4.4 gate still blocks the 50bp PRICE_STOP and
    returns None. The 84-day backtest showed deep-stop hurt shorts (-$455
    via early cuts on mean-reverting trades), hence the long-only default.
    """
    bp = patched_broker(EXIT_REASON_PRICE_STOP, hold_seconds=120)
    pos = _make_position(entry_price=100.0)
    out = bp._run_sentinel(
        ticker="TEST",
        side=bp._SENTINEL_SIDE_SHORT,
        pos=pos,
        current_price=100.80,
        bars=_make_bars(),
    )
    assert out is None, (
        "v6.5.1 long-only: short at +0.80%% inside window must NOT fire "
        "deep-stop (gate blocks as normal); got %r" % (out,)
    )


def test_deep_stop_short_breach_when_long_only_disabled(patched_broker):
    """With _V651_DEEP_STOP_LONG_ONLY=False, shorts also fire deep-stop.

    This guards the override path so we can flip back if a future
    refinement adds asymmetric short handling.
    """
    import engine.sentinel as sentinel_mod
    orig = sentinel_mod._V651_DEEP_STOP_LONG_ONLY
    sentinel_mod._V651_DEEP_STOP_LONG_ONLY = False
    try:
        bp = patched_broker(EXIT_REASON_PRICE_STOP, hold_seconds=120)
        pos = _make_position(entry_price=100.0)
        out = bp._run_sentinel(
            ticker="TEST",
            side=bp._SENTINEL_SIDE_SHORT,
            pos=pos,
            current_price=100.80,
            bars=_make_bars(),
        )
        assert out == EXIT_REASON_V651_DEEP_STOP, (
            "With LONG_ONLY=False, short at +0.80%% must fire deep-stop; got %r" % (out,)
        )
    finally:
        sentinel_mod._V651_DEEP_STOP_LONG_ONLY = orig


# ---------------------------------------------------------------------------
# Test 3: threshold not breached - gate blocks as normal
# ---------------------------------------------------------------------------


def test_deep_stop_threshold_not_breached(patched_broker):
    """Long entry @100, mark=99.50 (-0.50%) at hold=120s.

    Mark is above deep-stop level (99.25), so gate blocks PRICE_STOP -> None.
    """
    bp = patched_broker(EXIT_REASON_PRICE_STOP, hold_seconds=120)
    pos = _make_position(entry_price=100.0)
    out = bp._run_sentinel(
        ticker="TEST",
        side=bp._SENTINEL_SIDE_LONG,
        pos=pos,
        current_price=99.50,
        bars=_make_bars(),
    )
    assert out is None, (
        "v6.5.1: long at -0.50%% (no deep breach) inside window must be blocked -> None; got %r" % (out,)
    )


# ---------------------------------------------------------------------------
# Test 4: outside min_hold window - PRICE_STOP fires normally
# ---------------------------------------------------------------------------


def test_deep_stop_outside_window(patched_broker):
    """Long entry @100, mark=99.20 at hold=700s (past min_hold=600s).

    Deep-stop check does not run; PRICE_STOP fires as usual.
    """
    bp = patched_broker(EXIT_REASON_PRICE_STOP, hold_seconds=700)
    pos = _make_position(entry_price=100.0)
    out = bp._run_sentinel(
        ticker="TEST",
        side=bp._SENTINEL_SIDE_LONG,
        pos=pos,
        current_price=99.20,
        bars=_make_bars(),
    )
    assert out == EXIT_REASON_PRICE_STOP, (
        "v6.5.1: at hold=700s (outside window) PRICE_STOP must fire; got %r" % (out,)
    )


# ---------------------------------------------------------------------------
# Test 5: deep-stop disabled flag restores gate-block behavior
# ---------------------------------------------------------------------------


def test_deep_stop_disabled_flag(patched_broker):
    """With _V651_DEEP_STOP_ENABLED=False, a deep breach is still blocked."""
    bp = patched_broker(
        EXIT_REASON_PRICE_STOP, hold_seconds=120, deep_stop_enabled=False
    )
    pos = _make_position(entry_price=100.0)
    out = bp._run_sentinel(
        ticker="TEST",
        side=bp._SENTINEL_SIDE_LONG,
        pos=pos,
        current_price=99.20,
        bars=_make_bars(),
    )
    assert out is None, (
        "v6.5.1: with deep_stop disabled, breach inside window must be blocked -> None; got %r" % (out,)
    )


# ---------------------------------------------------------------------------
# Test 6: R-2 hard stop wins over deep-stop in priority
# ---------------------------------------------------------------------------


def test_deep_stop_priority_below_r2(patched_broker):
    """R-2 hard stop fires in parallel with a deep-stop scenario.

    When result.exit_reason is EXIT_REASON_R2_HARD_STOP, the gate block
    does not run (R-2 is a different reason), so R-2 fires through.
    This confirms R-2 always beats deep-stop.
    """
    bp = patched_broker(EXIT_REASON_R2_HARD_STOP, hold_seconds=120)
    pos = _make_position(entry_price=100.0)
    out = bp._run_sentinel(
        ticker="TEST",
        side=bp._SENTINEL_SIDE_LONG,
        pos=pos,
        current_price=95.00,
        bars=_make_bars(),
    )
    assert out == EXIT_REASON_R2_HARD_STOP, (
        "v6.5.1: R-2 must win over deep-stop; got %r" % (out,)
    )


# ---------------------------------------------------------------------------
# Test 7: warning log line is emitted on breach
# ---------------------------------------------------------------------------


def test_deep_stop_dashboard_log_emitted(patched_broker, caplog):
    """On a deep-stop breach, a WARNING containing '[V651-DEEP-STOP]' is logged."""
    bp = patched_broker(EXIT_REASON_PRICE_STOP, hold_seconds=120)
    pos = _make_position(entry_price=100.0)
    with caplog.at_level(logging.WARNING):
        bp._run_sentinel(
            ticker="TSLA",
            side=bp._SENTINEL_SIDE_LONG,
            pos=pos,
            current_price=99.20,
            bars=_make_bars(),
        )
    assert any("[V651-DEEP-STOP]" in record.message for record in caplog.records), (
        "v6.5.1: expected [V651-DEEP-STOP] warning in caplog; records: %r"
        % [r.message for r in caplog.records]
    )


# ---------------------------------------------------------------------------
# Test 8: default PCT constant value
# ---------------------------------------------------------------------------


def test_deep_stop_default_pct_value():
    """_V651_DEEP_STOP_PCT must equal 0.0075 (75 bp)."""
    assert sentinel_mod._V651_DEEP_STOP_PCT == 0.0075, (
        "v6.5.1: _V651_DEEP_STOP_PCT must be 0.0075; got %r" % sentinel_mod._V651_DEEP_STOP_PCT
    )
