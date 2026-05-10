"""Tests for orb.exits -- RR + move-to-BE exit evaluator."""
from __future__ import annotations

import pytest

from orb.exits import (
    OrbPosition,
    ExitDecision,
    evaluate,
    make_position,
    maybe_arm_be,
    EXIT_TARGET,
    EXIT_STOP,
    EXIT_BE_STOP,
    EXIT_EOD,
)


# Helpers
SESSION_END = 15 * 60 + 55  # 15:55 ET


# -------------------- make_position --------------------


class TestMakePosition:

    def test_long_geometry(self):
        # entry $100, stop $98 (risk = $2), RR=2.5
        pos = make_position(portfolio_id="main", ticker="AAPL", side="long",
                            entry_price=100.0, stop=98.0, rr=2.5, shares=100)
        assert pos.entry_price == 100.0
        assert pos.stop == 98.0
        assert pos.target == 105.0       # 100 + 2.5 * 2
        assert pos.one_r == 102.0        # 100 + 1 * 2
        assert pos.risk == 2.0
        assert pos.shares == 100
        assert pos.risk_dollars == 200.0  # 2 * 100
        assert pos.notional == 10000.0
        assert not pos.be_moved

    def test_short_geometry(self):
        # entry $100, stop $102 (risk = $2 short), RR=2.5
        pos = make_position(portfolio_id="main", ticker="AAPL", side="short",
                            entry_price=100.0, stop=102.0, rr=2.5, shares=100)
        assert pos.target == 95.0        # 100 - 2.5 * 2
        assert pos.one_r == 98.0         # 100 - 1 * 2
        assert pos.risk == 2.0

    def test_zero_risk_raises(self):
        with pytest.raises(ValueError):
            make_position(portfolio_id="main", ticker="AAPL", side="long",
                          entry_price=100.0, stop=100.0, rr=2.5)

    def test_invalid_side_raises(self):
        with pytest.raises(ValueError):
            make_position(portfolio_id="main", ticker="AAPL", side="up",
                          entry_price=100.0, stop=98.0, rr=2.5)


# -------------------- maybe_arm_be --------------------


class TestMaybeArmBe:

    def test_long_arms_when_high_reaches_one_r(self):
        pos = make_position(portfolio_id="main", ticker="AAPL", side="long",
                            entry_price=100.0, stop=98.0, rr=2.5)
        # one_r = 102; bar.high = 102.5 -> arms BE
        armed = maybe_arm_be(pos, bar_high=102.5, bar_low=101.0)
        assert armed
        assert pos.be_moved
        assert pos.stop == 100.0  # bumped to entry

    def test_long_does_not_arm_below_one_r(self):
        pos = make_position(portfolio_id="main", ticker="AAPL", side="long",
                            entry_price=100.0, stop=98.0, rr=2.5)
        armed = maybe_arm_be(pos, bar_high=101.5, bar_low=99.5)
        assert not armed
        assert not pos.be_moved
        assert pos.stop == 98.0

    def test_short_arms_when_low_reaches_one_r(self):
        pos = make_position(portfolio_id="main", ticker="AAPL", side="short",
                            entry_price=100.0, stop=102.0, rr=2.5)
        # one_r = 98; bar.low = 97.8 -> arms BE
        armed = maybe_arm_be(pos, bar_high=99.5, bar_low=97.8)
        assert armed
        assert pos.be_moved
        assert pos.stop == 100.0

    def test_arm_idempotent(self):
        pos = make_position(portfolio_id="main", ticker="AAPL", side="long",
                            entry_price=100.0, stop=98.0, rr=2.5)
        maybe_arm_be(pos, bar_high=102.5, bar_low=101.0)
        # Second call returns False
        again = maybe_arm_be(pos, bar_high=103.0, bar_low=101.5)
        assert not again
        assert pos.stop == 100.0  # unchanged


# -------------------- evaluate (long) --------------------


class TestEvaluateLong:

    def test_no_exit_in_range(self):
        pos = make_position(portfolio_id="main", ticker="AAPL", side="long",
                            entry_price=100.0, stop=98.0, rr=2.5)
        # Bar inside [stop, target]: stop=98, target=105
        d = evaluate(pos, bar_high=101.5, bar_low=99.5, bar_close=100.5,
                     bar_bucket_min=600, eod_cutoff_min=SESSION_END)
        assert d is None
        assert not pos.be_moved

    def test_stop_hit(self):
        pos = make_position(portfolio_id="main", ticker="AAPL", side="long",
                            entry_price=100.0, stop=98.0, rr=2.5)
        d = evaluate(pos, bar_high=99.5, bar_low=97.5, bar_close=97.8,
                     bar_bucket_min=600, eod_cutoff_min=SESSION_END)
        assert d is not None
        assert d.reason == EXIT_STOP
        assert d.price == 98.0

    def test_target_hit(self):
        pos = make_position(portfolio_id="main", ticker="AAPL", side="long",
                            entry_price=100.0, stop=98.0, rr=2.5)
        # high reaches target $105 (also crosses one_r)
        d = evaluate(pos, bar_high=105.5, bar_low=103.0, bar_close=105.2,
                     bar_bucket_min=600, eod_cutoff_min=SESSION_END)
        assert d is not None
        assert d.reason == EXIT_TARGET
        assert d.price == 105.0

    def test_be_arms_then_be_stop(self):
        """Bar 1: high crosses one_r, arms BE. Bar 2: low crosses entry, be_stop fires."""
        pos = make_position(portfolio_id="main", ticker="AAPL", side="long",
                            entry_price=100.0, stop=98.0, rr=2.5)
        # Bar 1: high=$103 (above one_r=$102), low=$101.5 (above entry); arms BE, no exit
        d1 = evaluate(pos, bar_high=103.0, bar_low=101.5, bar_close=102.5,
                      bar_bucket_min=600, eod_cutoff_min=SESSION_END)
        assert d1 is None
        assert pos.be_moved
        assert pos.stop == 100.0
        # Bar 2: low=$99.8 -> below the bumped stop $100; be_stop fires
        d2 = evaluate(pos, bar_high=101.0, bar_low=99.8, bar_close=100.5,
                      bar_bucket_min=601, eod_cutoff_min=SESSION_END)
        assert d2 is not None
        assert d2.reason == EXIT_BE_STOP
        assert d2.price == 100.0

    def test_eod_flush(self):
        pos = make_position(portfolio_id="main", ticker="AAPL", side="long",
                            entry_price=100.0, stop=98.0, rr=2.5)
        # Bar at eod cutoff with neutral price: closes at bar_close
        d = evaluate(pos, bar_high=101.0, bar_low=99.0, bar_close=100.5,
                     bar_bucket_min=SESSION_END, eod_cutoff_min=SESSION_END)
        assert d is not None
        assert d.reason == EXIT_EOD
        assert d.price == 100.5

    def test_simultaneous_target_and_stop_long_pessimistic(self):
        """If both target and stop are touched in same bar, stop wins (pessimistic)."""
        pos = make_position(portfolio_id="main", ticker="AAPL", side="long",
                            entry_price=100.0, stop=98.0, rr=2.5)
        # bar.high=$105.5 (crosses target $105 AND one_r $102 -> arms BE);
        # bar.low=$97.5 (below original stop $98 BUT BE was just armed,
        # so stop becomes $100; low $97.5 is below new stop too).
        # After arming, stop=$100; bar.low=$97.5 <= $100 -> be_stop fires.
        d = evaluate(pos, bar_high=105.5, bar_low=97.5, bar_close=100.0,
                     bar_bucket_min=600, eod_cutoff_min=SESSION_END)
        assert d is not None
        assert d.reason == EXIT_BE_STOP
        assert d.price == 100.0


# -------------------- evaluate (short) --------------------


class TestEvaluateShort:

    def test_no_exit_in_range(self):
        pos = make_position(portfolio_id="main", ticker="AAPL", side="short",
                            entry_price=100.0, stop=102.0, rr=2.5)
        # target=$95, stop=$102
        d = evaluate(pos, bar_high=100.5, bar_low=99.5, bar_close=99.8,
                     bar_bucket_min=600, eod_cutoff_min=SESSION_END)
        assert d is None

    def test_stop_hit_short(self):
        pos = make_position(portfolio_id="main", ticker="AAPL", side="short",
                            entry_price=100.0, stop=102.0, rr=2.5)
        d = evaluate(pos, bar_high=102.5, bar_low=101.0, bar_close=102.2,
                     bar_bucket_min=600, eod_cutoff_min=SESSION_END)
        assert d is not None
        assert d.reason == EXIT_STOP
        assert d.price == 102.0

    def test_target_hit_short(self):
        pos = make_position(portfolio_id="main", ticker="AAPL", side="short",
                            entry_price=100.0, stop=102.0, rr=2.5)
        # bar.low reaches $95 (target); also crosses one_r=$98
        d = evaluate(pos, bar_high=99.0, bar_low=94.5, bar_close=95.0,
                     bar_bucket_min=600, eod_cutoff_min=SESSION_END)
        assert d is not None
        assert d.reason == EXIT_TARGET
        assert d.price == 95.0

    def test_be_arms_then_be_stop_short(self):
        pos = make_position(portfolio_id="main", ticker="AAPL", side="short",
                            entry_price=100.0, stop=102.0, rr=2.5)
        # one_r=$98. Bar 1: low=$97 arms BE
        d1 = evaluate(pos, bar_high=99.0, bar_low=97.0, bar_close=97.5,
                      bar_bucket_min=600, eod_cutoff_min=SESSION_END)
        assert d1 is None
        assert pos.be_moved
        assert pos.stop == 100.0
        # Bar 2: high=$100.5 -> above bumped stop $100 -> be_stop
        d2 = evaluate(pos, bar_high=100.5, bar_low=99.0, bar_close=100.0,
                      bar_bucket_min=601, eod_cutoff_min=SESSION_END)
        assert d2 is not None
        assert d2.reason == EXIT_BE_STOP
        assert d2.price == 100.0
