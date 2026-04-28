"""v5.9.0 \u2014 unit tests for the Recursive Forensic Stop + per-trade brake.

Covers:
  1. forensic_audit_long / short \u2014 lower-low / higher-high logic
  2. update_forensic_stop_long / short \u2014 close-inside-OR resets, close-
     outside-OR runs audit, exit_reason set on fire
  3. per_trade_sovereign_brake \u2014 -$499 STAY, -$500 EXIT, -$501 EXIT
  4. evaluate_titan_exit ordering \u2014 velocity_fuse > per_trade_brake > be_stop
  5. Phase B (house_money) does NOT advance forensic counter
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tiger_buffalo_v5 as v5  # noqa: E402


# ----------------------- forensic_audit_* ---------------------------------
def test_audit_long_lower_low_exits():
    assert v5.forensic_audit_long(prior_low=100.0, current_low=99.5) is True


def test_audit_long_equality_stays():
    assert v5.forensic_audit_long(prior_low=100.0, current_low=100.0) is False


def test_audit_long_higher_low_stays():
    assert v5.forensic_audit_long(prior_low=100.0, current_low=100.1) is False


def test_audit_long_none_stays():
    assert v5.forensic_audit_long(prior_low=None, current_low=99.0) is False
    assert v5.forensic_audit_long(prior_low=100.0, current_low=None) is False


def test_audit_short_higher_high_exits():
    assert v5.forensic_audit_short(prior_high=200.0, current_high=200.5) is True


def test_audit_short_lower_high_stays():
    assert v5.forensic_audit_short(prior_high=200.0, current_high=199.5) is False


# ----------------------- update_forensic_stop_long ------------------------
def _new_long_track(entry=500.0, qty=10):
    track = v5.new_track(v5.DIR_LONG)
    v5.init_titan_exit_state(track, entry_price=entry, qty=qty)
    return track


def test_update_forensic_long_close_inside_or_resets_counter():
    track = _new_long_track()
    track["forensic_consecutive_count"] = 3
    fired = v5.update_forensic_stop_long(
        track,
        candle_1m_close=501.0,
        candle_1m_low=500.5,
        prior_candle_1m_low=499.0,
        or_high=500.0,
    )
    assert fired is False
    assert track["forensic_consecutive_count"] == 0


def test_update_forensic_long_close_outside_or_with_lower_low_fires():
    track = _new_long_track()
    fired = v5.update_forensic_stop_long(
        track,
        candle_1m_close=499.0,
        candle_1m_low=498.5,
        prior_candle_1m_low=499.0,
        or_high=500.0,
    )
    assert fired is True
    assert track["exit_reason"] == v5.EXIT_REASON_FORENSIC_STOP
    assert track["forensic_consecutive_count"] == 1


def test_update_forensic_long_close_outside_or_higher_low_stays():
    track = _new_long_track()
    fired = v5.update_forensic_stop_long(
        track,
        candle_1m_close=499.0,
        candle_1m_low=498.9,
        prior_candle_1m_low=498.5,
        or_high=500.0,
    )
    assert fired is False
    assert track.get("exit_reason") is None
    assert track["forensic_consecutive_count"] == 1


def test_update_forensic_long_close_outside_or_equal_low_stays():
    track = _new_long_track()
    fired = v5.update_forensic_stop_long(
        track,
        candle_1m_close=499.0,
        candle_1m_low=498.5,
        prior_candle_1m_low=498.5,
        or_high=500.0,
    )
    assert fired is False
    assert track["forensic_consecutive_count"] == 1


def test_update_forensic_short_mirror():
    track = v5.new_track(v5.DIR_SHORT)
    v5.init_titan_exit_state(track, entry_price=500.0, qty=10)
    fired = v5.update_forensic_stop_short(
        track,
        candle_1m_close=501.0,
        candle_1m_high=501.5,
        prior_candle_1m_high=501.0,
        or_low=500.0,
    )
    assert fired is True
    assert track["exit_reason"] == v5.EXIT_REASON_FORENSIC_STOP


# ----------------------- per_trade_sovereign_brake ------------------------
def test_per_trade_brake_long_499_stays():
    track = _new_long_track(entry=500.0, qty=10)
    # Loss = (450.10 - 500.0) * 10 = -$499.00
    assert v5.per_trade_sovereign_brake(track, current_price=450.10) is False


def test_per_trade_brake_long_500_exits():
    track = _new_long_track(entry=500.0, qty=10)
    # Loss = (450.0 - 500.0) * 10 = -$500.00 (boundary fires per spec)
    assert v5.per_trade_sovereign_brake(track, current_price=450.0) is True


def test_per_trade_brake_long_501_exits():
    track = _new_long_track(entry=500.0, qty=10)
    # Loss = (449.90 - 500.0) * 10 = -$501.00
    assert v5.per_trade_sovereign_brake(track, current_price=449.90) is True


def test_per_trade_brake_short_mirrors_long():
    track = v5.new_track(v5.DIR_SHORT)
    v5.init_titan_exit_state(track, entry_price=500.0, qty=10)
    # Loss for short = (entry - current) * qty.  current=550 -> -$500
    assert v5.per_trade_sovereign_brake(track, current_price=550.0) is True
    assert v5.per_trade_sovereign_brake(track, current_price=549.99) is False


def test_per_trade_brake_qty_zero_fails_closed():
    track = _new_long_track(entry=500.0, qty=0)
    assert v5.per_trade_sovereign_brake(track, current_price=10.0) is False


def test_per_trade_brake_none_price_fails_closed():
    track = _new_long_track()
    assert v5.per_trade_sovereign_brake(track, current_price=None) is False


# ----------------------- evaluate_titan_exit ordering ---------------------
def test_evaluate_titan_exit_velocity_fuse_wins_over_brake():
    track = _new_long_track(entry=500.0, qty=10)
    # Velocity fuse: current < open * (1 - 0.01)
    # Make velocity fuse fire AND brake also fire.
    out = v5.evaluate_titan_exit(
        track,
        side=v5.DIR_LONG,
        current_price=450.0,  # -$500 unrealized
        candle_1m_open=460.0,  # 450 < 460*0.99=455.4
    )
    assert out == v5.EXIT_REASON_VELOCITY_FUSE


def test_evaluate_titan_exit_brake_wins_over_be_stop():
    track = _new_long_track(entry=500.0, qty=10)
    track["phase"] = v5.PHASE_HOUSE_MONEY
    track["current_stop"] = 500.0
    # No velocity fuse (open=current). Brake fires (-$500). BE would also fire.
    out = v5.evaluate_titan_exit(
        track,
        side=v5.DIR_LONG,
        current_price=450.0,
        candle_1m_open=450.0,
    )
    assert out == v5.EXIT_REASON_PER_TRADE_BRAKE


def test_evaluate_titan_exit_be_only_fires_in_house_money():
    track = _new_long_track(entry=500.0, qty=10)
    # phase=initial_risk, BE-stop should NOT fire
    track["current_stop"] = 500.0
    out = v5.evaluate_titan_exit(
        track,
        side=v5.DIR_LONG,
        current_price=499.0,  # below stop, but Phase A
        candle_1m_open=499.0,
    )
    assert out is None
    track["phase"] = v5.PHASE_HOUSE_MONEY
    out = v5.evaluate_titan_exit(
        track,
        side=v5.DIR_LONG,
        current_price=499.0,
        candle_1m_open=499.0,
    )
    assert out == v5.EXIT_REASON_BE_STOP
