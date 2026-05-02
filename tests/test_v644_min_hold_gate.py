"""v6.4.4 min-hold gate on Alarm-A protective stop (PRICE_STOP).

Devi 84day_2026_sip backtest: 266 of 269 under-10min pairs exit on
sentinel_a_stop_price for -$6,649. This module gates that single
exit reason under a 10-minute hold floor; deeper rails (R-2, daily
circuit, Alarm-A flash velocity, B/D/F) still fire.

Coverage:
  1. PRICE_STOP fires under 10min      -> blocked, returns None
  2. PRICE_STOP fires at exactly 10min -> NOT blocked, returns reason
  3. PRICE_STOP fires after 10min      -> NOT blocked, returns reason
  4. R-2 hard stop under 10min         -> NOT blocked (different reason)
  5. SHORT side under 10min            -> blocked
  6. Kill-switch off, under 10min      -> NOT blocked
  7. Missing entry_ts_utc, under 10min -> NOT blocked (fail-open)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import engine.sentinel as sentinel_mod
from engine.sentinel import (
    EXIT_REASON_PRICE_STOP,
    EXIT_REASON_R2_HARD_STOP,
    SentinelAction,
    SentinelResult,
)


_ENTRY_UTC = datetime(2026, 5, 2, 14, 30, 0, tzinfo=timezone.utc)


def _make_position(entry_ts_utc=_ENTRY_UTC, lifecycle_id="v644-test-pid", include_v644_field=True):
    """v6.4.4 fills carry both ``entry_ts_utc`` (wallclock, legacy field)
    and ``v644_entry_now_et_iso`` (harness-aware, new in v6.4.4).
    The helper prefers the v6.4.4 field; tests that need to verify the
    fallback can pass include_v644_field=False.
    """
    pos = {
        "shares": 100,
        "entry_price": 100.00,
        "stop": 99.50,
        "initial_stop": 99.50,
        "entry_ts_utc": entry_ts_utc.isoformat(),
        "lifecycle_position_id": lifecycle_id,
        "v531_min_adverse_price": None,
        "v531_max_favorable_price": None,
        "trail_state": None,
    }
    if include_v644_field:
        pos["v644_entry_now_et_iso"] = entry_ts_utc.isoformat()
    return pos


def _make_bars():
    return {
        "timestamps": [],
        "opens": [],
        "highs": [],
        "lows": [],
        "closes": [],
    }


class _StubTG:
    """Minimal trade_genius surface: only _now_et() is consulted by
    _v644_position_hold_seconds and the rest of _run_sentinel.

    Returns a tz-aware UTC datetime so isoformat parsing in the helper
    stays consistent across tests.
    """

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
    """Build a SentinelResult that has_full_exit returns True with the
    given reason. Bypasses evaluate_sentinel so the unit test isolates
    the broker.positions gate logic.
    """
    return SentinelResult(
        alarms=[
            SentinelAction(
                alarm="A_STOP_PRICE" if reason == EXIT_REASON_PRICE_STOP else "A_LOSS",
                reason=reason,
                detail="forced",
                detail_stop_price=99.50,
            )
        ]
    )


@pytest.fixture
def patched_broker(monkeypatch):
    """Patch broker.positions to return a forced full-exit result and a
    stub trade_genius. Returns the broker_positions module so each test
    can call _run_sentinel directly.
    """
    from broker import positions as broker_positions

    def _install(reason: str, hold_seconds: float, gate_enabled: bool = True, entry_ts=_ENTRY_UTC):
        monkeypatch.setattr(
            broker_positions,
            "evaluate_sentinel",
            lambda **kwargs: _force_full_exit_result(reason),
        )
        monkeypatch.setattr(broker_positions, "_tg", lambda: _StubTG(hold_seconds))
        monkeypatch.setattr(sentinel_mod, "_V644_MIN_HOLD_GATE_ENABLED", gate_enabled)
        return broker_positions

    return _install


def test_v644_price_stop_under_10min_blocked_long(patched_broker):
    bp = patched_broker(EXIT_REASON_PRICE_STOP, hold_seconds=300)
    pos = _make_position()
    out = bp._run_sentinel(
        ticker="TEST",
        side=bp._SENTINEL_SIDE_LONG,
        pos=pos,
        current_price=99.40,
        bars=_make_bars(),
    )
    assert out is None, "v6.4.4: PRICE_STOP at hold=300s must be blocked; got %r" % (out,)


def test_v644_price_stop_under_10min_blocked_short(patched_broker):
    bp = patched_broker(EXIT_REASON_PRICE_STOP, hold_seconds=120)
    pos = _make_position()
    out = bp._run_sentinel(
        ticker="TEST",
        side=bp._SENTINEL_SIDE_SHORT,
        pos=pos,
        current_price=100.60,
        bars=_make_bars(),
    )
    assert out is None, "v6.4.4: SHORT PRICE_STOP at hold=120s must be blocked; got %r" % (out,)


def test_v644_price_stop_at_10min_boundary_fires(patched_broker):
    """At exactly 600s the gate uses < not <=, so the stop fires."""
    bp = patched_broker(EXIT_REASON_PRICE_STOP, hold_seconds=600)
    pos = _make_position()
    out = bp._run_sentinel(
        ticker="TEST",
        side=bp._SENTINEL_SIDE_LONG,
        pos=pos,
        current_price=99.40,
        bars=_make_bars(),
    )
    assert out == EXIT_REASON_PRICE_STOP, (
        "v6.4.4 boundary: at hold=600s PRICE_STOP must fire; got %r" % (out,)
    )


def test_v644_price_stop_after_10min_fires_unchanged(patched_broker):
    bp = patched_broker(EXIT_REASON_PRICE_STOP, hold_seconds=900)
    pos = _make_position()
    out = bp._run_sentinel(
        ticker="TEST",
        side=bp._SENTINEL_SIDE_LONG,
        pos=pos,
        current_price=99.40,
        bars=_make_bars(),
    )
    assert out == EXIT_REASON_PRICE_STOP, "v6.4.4: at hold=900s PRICE_STOP must fire; got %r" % (
        out,
    )


def test_v644_r2_hard_stop_under_10min_still_fires(patched_broker):
    """R-2 (-$500) is the deep risk rail and emits a different reason.
    The gate must NOT touch it, regardless of hold time.
    """
    bp = patched_broker(EXIT_REASON_R2_HARD_STOP, hold_seconds=180)
    pos = _make_position()
    out = bp._run_sentinel(
        ticker="TEST",
        side=bp._SENTINEL_SIDE_LONG,
        pos=pos,
        current_price=95.00,
        bars=_make_bars(),
    )
    assert out == EXIT_REASON_R2_HARD_STOP, (
        "v6.4.4: R-2 hard stop must fire under 10min as a deep rail; got %r" % (out,)
    )


def test_v644_kill_switch_off_under_10min_fires(patched_broker):
    """When _V644_MIN_HOLD_GATE_ENABLED is flipped off, behavior reverts
    to v6.4.3 (PRICE_STOP fires under 10min).
    """
    bp = patched_broker(EXIT_REASON_PRICE_STOP, hold_seconds=120, gate_enabled=False)
    pos = _make_position()
    out = bp._run_sentinel(
        ticker="TEST",
        side=bp._SENTINEL_SIDE_LONG,
        pos=pos,
        current_price=99.40,
        bars=_make_bars(),
    )
    assert out == EXIT_REASON_PRICE_STOP, (
        "v6.4.4: kill-switch off must restore v6.4.3 behavior; got %r" % (out,)
    )


def test_v644_fallback_to_entry_ts_utc_when_new_field_missing(patched_broker):
    """Positions hydrated from a pre-v6.4.4 paper-state snapshot will
    not have ``v644_entry_now_et_iso``. The helper must fall back to
    ``entry_ts_utc`` so the gate still works in prod (where wallclock
    and harness-clock agree to microseconds). In backtest the fallback
    fails open, but that's the prior behavior and is acceptable.
    """
    bp = patched_broker(EXIT_REASON_PRICE_STOP, hold_seconds=300)
    pos = _make_position(include_v644_field=False)
    assert "v644_entry_now_et_iso" not in pos
    out = bp._run_sentinel(
        ticker="TEST",
        side=bp._SENTINEL_SIDE_LONG,
        pos=pos,
        current_price=99.40,
        bars=_make_bars(),
    )
    assert out is None, (
        "v6.4.4 fallback: entry_ts_utc must drive the gate when the new "
        "field is absent; got %r" % (out,)
    )


def test_v644_missing_entry_ts_under_10min_fail_open(patched_broker):
    """If entry_ts_utc is missing or unparseable, the helper returns None
    and the gate sits out (fail-open). A real stop must never be silently
    disabled by a clock outage.
    """
    bp = patched_broker(EXIT_REASON_PRICE_STOP, hold_seconds=120)
    pos = _make_position()
    pos.pop("entry_ts_utc", None)
    pos.pop("v644_entry_now_et_iso", None)
    out = bp._run_sentinel(
        ticker="TEST",
        side=bp._SENTINEL_SIDE_LONG,
        pos=pos,
        current_price=99.40,
        bars=_make_bars(),
    )
    assert out == EXIT_REASON_PRICE_STOP, (
        "v6.4.4: missing entry timestamp fields must fail open and let "
        "PRICE_STOP fire; got %r" % (out,)
    )
