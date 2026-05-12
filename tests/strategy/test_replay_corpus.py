"""v8.3.27 -- tests for tools/replay_corpus.py.

Covers the post-processing surface (rule application on a fixed
synthetic leg list). The replay-driver side (orb_replay_day.replay()
invocation) is tested indirectly by tests/strategy/test_orb_replay_day.py
in the existing suite.
"""
from __future__ import annotations

import pytest

from tools.replay_corpus import Leg, RuleConfig, pair_legs, simulate


# ---------------------------------------------------------------- #
#  pair_legs                                                       #
# ---------------------------------------------------------------- #


class TestPairLegs:

    def test_pairs_admit_with_following_exit(self):
        events = [
            {"kind": "admit", "ticker": "AAPL", "side": "long",
             "shares": 100, "price": 150.0, "bucket": 600},
            {"kind": "exit", "ticker": "AAPL", "price": 152.0,
             "reason": "target", "bucket": 700},
        ]
        legs = pair_legs("2026-05-12", events)
        assert len(legs) == 1
        assert legs[0].ticker == "AAPL"
        assert legs[0].side == "long"
        assert legs[0].pnl == pytest.approx(200.0)  # (152-150)*100
        assert legs[0].entry_bucket == 600
        assert legs[0].exit_bucket == 700
        assert legs[0].exit_reason == "target"

    def test_short_pnl_inverts(self):
        events = [
            {"kind": "admit", "ticker": "AMZN", "side": "short",
             "shares": 50, "price": 100.0, "bucket": 600},
            {"kind": "exit", "ticker": "AMZN", "price": 98.0,
             "reason": "target", "bucket": 700},
        ]
        legs = pair_legs("2026-05-12", events)
        # Short profit when price drops: (100-98)*50 = 100
        assert legs[0].pnl == pytest.approx(100.0)

    def test_exit_without_admit_ignored(self):
        events = [{"kind": "exit", "ticker": "MSFT", "price": 100.0}]
        legs = pair_legs("2026-05-12", events)
        assert legs == []

    def test_non_admit_exit_events_skipped(self):
        events = [
            {"kind": "session_start", "date": "2026-05-12"},
            {"kind": "reject", "ticker": "AAPL", "reason": "no_signal"},
            {"kind": "summary", "admits": 0, "exits": 0},
        ]
        legs = pair_legs("2026-05-12", events)
        assert legs == []

    def test_multi_leg_same_ticker_different_admits(self):
        """The live engine fires the same (ticker, side) multiple
        times per day (signal flips). Each admit/exit pair becomes
        its own leg."""
        events = [
            {"kind": "admit", "ticker": "AMZN", "side": "short",
             "shares": 50, "price": 100.0, "bucket": 600},
            {"kind": "exit", "ticker": "AMZN", "price": 102.0,
             "reason": "sentinel_a_stop_price", "bucket": 615},
            {"kind": "admit", "ticker": "AMZN", "side": "short",
             "shares": 50, "price": 99.0, "bucket": 700},
            {"kind": "exit", "ticker": "AMZN", "price": 101.0,
             "reason": "sentinel_a_stop_price", "bucket": 750},
        ]
        legs = pair_legs("2026-05-12", events)
        assert len(legs) == 2
        assert legs[0].pnl == pytest.approx(-100.0)
        assert legs[1].pnl == pytest.approx(-100.0)


# ---------------------------------------------------------------- #
#  simulate                                                        #
# ---------------------------------------------------------------- #


def _make_leg(date, ticker, side, entry_b, exit_b, pnl, reason="x"):
    """Build a Leg with minimal fields for rule-testing."""
    return Leg(
        date=date, ticker=ticker, side=side, shares=100,
        entry_price=100.0, exit_price=100.0 + (pnl / 100.0),
        entry_bucket=entry_b, exit_bucket=exit_b,
        exit_reason=reason, pnl=pnl,
    )


class TestRule1LossLock:

    def test_off_when_threshold_zero(self):
        # 3 legs same (AAPL, long): -$50, +$100, +$100.
        legs = {
            "2026-05-12": [
                _make_leg("2026-05-12", "AAPL", "long", 600, 650, -50),
                _make_leg("2026-05-12", "AAPL", "long", 700, 750, +100),
                _make_leg("2026-05-12", "AAPL", "long", 800, 850, +100),
            ],
        }
        result = simulate(legs, RuleConfig(name="off"))
        assert result["total_pnl"] == pytest.approx(150.0)
        assert result["skipped_r1_count"] == 0

    def test_locks_after_loss_above_threshold(self):
        # Same legs; with threshold $25, the -$50 first leg locks
        # AAPL,long; the next 2 winning legs are blocked.
        legs = {
            "2026-05-12": [
                _make_leg("2026-05-12", "AAPL", "long", 600, 650, -50),
                _make_leg("2026-05-12", "AAPL", "long", 700, 750, +100),
                _make_leg("2026-05-12", "AAPL", "long", 800, 850, +100),
            ],
        }
        result = simulate(legs, RuleConfig(
            name="lock25", loss_lock_threshold_usd=25.0,
        ))
        assert result["total_pnl"] == pytest.approx(-50.0)
        assert result["skipped_r1_count"] == 2

    def test_loss_below_threshold_does_not_lock(self):
        # Threshold $100, first loss is -$50 -- does NOT lock.
        legs = {
            "2026-05-12": [
                _make_leg("2026-05-12", "AAPL", "long", 600, 650, -50),
                _make_leg("2026-05-12", "AAPL", "long", 700, 750, +100),
            ],
        }
        result = simulate(legs, RuleConfig(
            name="lock100", loss_lock_threshold_usd=100.0,
        ))
        # No lock fired -> both legs kept -> $50 net
        assert result["total_pnl"] == pytest.approx(50.0)
        assert result["skipped_r1_count"] == 0

    def test_lock_is_per_ticker_side(self):
        """Locking AAPL,long does NOT lock AAPL,short or MSFT,long."""
        legs = {
            "2026-05-12": [
                _make_leg("2026-05-12", "AAPL", "long", 600, 650, -150),
                _make_leg("2026-05-12", "AAPL", "long", 700, 750, +100),
                _make_leg("2026-05-12", "AAPL", "short", 700, 750, +100),
                _make_leg("2026-05-12", "MSFT", "long", 700, 750, +100),
            ],
        }
        result = simulate(legs, RuleConfig(
            name="lock100", loss_lock_threshold_usd=100.0,
        ))
        # AAPL long #2 blocked; AAPL short + MSFT long pass through.
        assert result["total_pnl"] == pytest.approx(-150 + 100 + 100)
        assert result["skipped_r1_count"] == 1


class TestRule2PeakDdHalt:

    def test_halts_after_peak_drawdown(self):
        # Build a day with peak +$1000 then drop to +$400 (DD $600).
        # With threshold $500, halt triggers; subsequent +$100 legs
        # skipped.
        legs = {
            "2026-05-12": [
                _make_leg("2026-05-12", "AAPL", "long", 600, 610, +1000),
                _make_leg("2026-05-12", "AMZN", "short", 700, 710, -600),
                _make_leg("2026-05-12", "MSFT", "long", 800, 810, +100),
                _make_leg("2026-05-12", "NVDA", "long", 900, 910, +100),
            ],
        }
        result = simulate(legs, RuleConfig(
            name="dd500", peak_dd_halt_usd=500.0,
        ))
        # After the -600 leg: peak=1000, cum=400, drawdown=600 > 500.
        # The two subsequent +$100 legs are blocked.
        assert result["total_pnl"] == pytest.approx(400.0)
        assert result["skipped_r2_count"] == 2

    def test_no_halt_if_dd_below_threshold(self):
        # Peak +$500, drop to +$200 (DD $300). Threshold $500 -> no halt.
        legs = {
            "2026-05-12": [
                _make_leg("2026-05-12", "AAPL", "long", 600, 610, +500),
                _make_leg("2026-05-12", "AMZN", "short", 700, 710, -300),
                _make_leg("2026-05-12", "MSFT", "long", 800, 810, +100),
            ],
        }
        result = simulate(legs, RuleConfig(
            name="dd500", peak_dd_halt_usd=500.0,
        ))
        assert result["total_pnl"] == pytest.approx(300.0)
        assert result["skipped_r2_count"] == 0


class TestCombinedRules:

    def test_combo_skips_via_both_paths(self):
        # AAPL,long stops big -> Rule #1 locks AAPL,long.
        # Then a big drawdown via AMZN -> Rule #2 halts.
        # Anything after both events should be blocked.
        legs = {
            "2026-05-12": [
                _make_leg("2026-05-12", "AAPL", "long", 600, 610, -200),
                _make_leg("2026-05-12", "AMZN", "short", 700, 710, -400),
                _make_leg("2026-05-12", "AAPL", "long", 800, 810, +100),
                _make_leg("2026-05-12", "MSFT", "long", 900, 910, +100),
            ],
        }
        result = simulate(legs, RuleConfig(
            name="combo", loss_lock_threshold_usd=100.0,
            peak_dd_halt_usd=500.0,
        ))
        # First leg: -$200 (taken, locks AAPL,long, no DD halt yet)
        # Second leg: -$400 (taken, peak=0, cum=-600, DD vs peak=600 > 500 -> halt)
        # Third leg: AAPL,long locked by Rule #1 -> skipped (counted as r1)
        # Fourth leg: halted by Rule #2 -> skipped (counted as r2)
        assert result["total_pnl"] == pytest.approx(-600.0)
        assert result["skipped_r1_count"] == 1
        assert result["skipped_r2_count"] == 1
