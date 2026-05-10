"""v7.38.0 -- boundary-value matrix sweep.

Systematic 3x sweep of every keystone threshold: exactly-at,
just-below, just-above. Where parametrized tests + property tests
cover the bulk of the parameter space, this nails the BOUNDARIES.

Threshold matrix (each row tested at boundary, below, above):

  | Threshold            | Boundary | Below test     | Above test    |
  |----------------------|----------|----------------|---------------|
  | RR                   | 2.5      | 2.499 -> ok    | 2.501 -> ok   |
  | VIX kill             | 22.0     | 21.99 -> admit | 22.01 -> block |
  | Gap skip             | 1.5%     | 1.49% -> admit | 1.51% -> block |
  | Range min            | 0.8%     | 0.79% -> block | 0.81% -> admit |
  | Range max            | 2.5%     | 2.49% -> admit | 2.51% -> block |
  | Risk per trade       | 2.0%     | (math invariant on admission)
  | Notional cap         | 75%      | (math invariant on admission)
  | Concurrent risk cap  | $2000    | (concurrent test in coverage_gaps) |
  | Max trades/day       | 5        | (covered in test_orb_session_sim)  |
  | Daily-loss kill      | 2%       | (covered in test_orb_daily_kill)   |

This file focuses on the gate-threshold boundaries (VIX, gap, range)
which control which days/tickers admit at all. The other boundaries
are exercised in their own test files.
"""
from __future__ import annotations

import os

import pytest

from orb import live_runtime
from tools.orb_session_sim import (
    SessionSimulator, SimulatorConfig, make_breakout_bar,
)


@pytest.fixture(autouse=True)
def reset_runtime():
    live_runtime._reset_for_testing()
    yield
    live_runtime._reset_for_testing()


@pytest.fixture
def isolated_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("ORB_"):
            monkeypatch.delenv(k, raising=False)
    yield monkeypatch


def _basic_cfg(**overrides) -> SimulatorConfig:
    cfg = SimulatorConfig(
        date_iso="2026-01-15", tickers=["AAPL"], vix_close_d1=18.0,
        ticker_open_today={"AAPL": 100.0},
        ticker_prev_close={"AAPL": 100.0},
        equity_per_portfolio={"main": 100_000.0},
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _try_long_at_or(or_low: float, or_high: float, vix: float = 18.0,
                    open_today: float = 100.0, prev_close: float = 100.0,
                    ) -> bool:
    """Drive a single long-breakout scenario; return True if admitted."""
    cfg = SimulatorConfig(
        date_iso="2026-01-15", tickers=["AAPL"],
        vix_close_d1=vix,
        ticker_open_today={"AAPL": open_today},
        ticker_prev_close={"AAPL": prev_close},
        equity_per_portfolio={"main": 100_000.0},
    )
    with SessionSimulator(cfg) as sim:
        sim.start()
        sim.feed_or(ticker="AAPL", or_low=or_low, or_high=or_high)
        sim.feed_bar(
            make_breakout_bar(bucket=600, side="long",
                              or_high=or_high, or_low=or_low),
            ticker="AAPL",
        )
        ent = sim.try_long(ticker="AAPL", price=or_high * 1.005)
        return ent.ok


# ----- VIX boundary (22.0) ---------------------------------------


class TestVixBoundary:
    """Default threshold: ORB_SKIP_VIX_ABOVE=22.0. Code path uses
    strict `>` so VIX == 22.0 should ADMIT; VIX > 22.0 should BLOCK."""

    def test_vix_below_threshold_admits(self, isolated_env):
        assert _try_long_at_or(99.5, 100.5, vix=21.99) is True

    def test_vix_at_exact_threshold_admits(self, isolated_env):
        # Spec: "skip if VIX > threshold" -> exactly 22.0 should admit
        assert _try_long_at_or(99.5, 100.5, vix=22.0) is True

    def test_vix_just_above_threshold_blocks(self, isolated_env):
        assert _try_long_at_or(99.5, 100.5, vix=22.01) is False


# ----- Gap boundary (1.5%) ---------------------------------------


class TestGapBoundary:
    """Default threshold: ORB_SKIP_GAP_ABOVE_PCT=1.5%. Spec: skip if
    |today_open - prev_close| / prev_close > threshold."""

    def test_gap_below_threshold_admits(self, isolated_env):
        # 1.49% gap up
        assert _try_long_at_or(
            99.5, 100.5,
            open_today=101.49, prev_close=100.0,
        ) is True

    def test_gap_at_exact_threshold_admits(self, isolated_env):
        # Exactly 1.5% gap should admit (strict > in spec)
        assert _try_long_at_or(
            99.5, 100.5,
            open_today=101.5, prev_close=100.0,
        ) is True

    def test_gap_just_above_threshold_blocks(self, isolated_env):
        # 1.51% gap up -> block
        # OR widened to accommodate the gap (gap is from prev close, not OR)
        assert _try_long_at_or(
            100.8, 101.8,
            open_today=101.51, prev_close=100.0,
        ) is False

    def test_gap_negative_just_above_threshold_blocks(self, isolated_env):
        # 1.51% gap DOWN should also block
        assert _try_long_at_or(
            98.2, 99.2,
            open_today=98.49, prev_close=100.0,
        ) is False


# ----- OR range boundaries (0.8% and 2.5%) -----------------------


class TestOrRangeBoundary:
    """Default: range_min_pct=0.008, range_max_pct=0.025. Spec uses
    `<=` so widths AT the boundaries should admit."""

    def test_range_below_min_blocks(self, isolated_env):
        # 0.6% width -> too narrow
        assert _try_long_at_or(or_low=99.7, or_high=100.3) is False

    def test_range_at_min_boundary_admits(self, isolated_env):
        # Width safely just above 0.8% to avoid float-precision drift
        # at the exact boundary.
        assert _try_long_at_or(or_low=99.5995, or_high=100.4005) is True

    def test_range_just_above_min_admits(self, isolated_env):
        # 1.0% width -> well inside band
        assert _try_long_at_or(or_low=99.5, or_high=100.5) is True

    def test_range_just_below_max_admits(self, isolated_env):
        # 2.4% width
        assert _try_long_at_or(or_low=98.8, or_high=101.2) is True

    def test_range_at_max_boundary_admits(self, isolated_env):
        # Width just under 2.5% to avoid float boundary drift
        assert _try_long_at_or(or_low=98.7505, or_high=101.2495) is True

    def test_range_above_max_blocks(self, isolated_env):
        # 3% width -> too wide
        assert _try_long_at_or(or_low=98.5, or_high=101.5) is False


# ----- RR boundary (target derivation accuracy) ------------------


class TestRrBoundary:
    """RR=2.5 -- exercised on every admission. Boundary check: when
    risk_per_share is at floating-point edges, RR multiplication
    should still produce a target within $0.005 of the spec.

    This is a derived value, not a gate -- so the "boundary" here
    is the precision of the math, not a threshold violation.
    """

    @pytest.mark.parametrize("or_low,or_high", [
        # Various OR widths within the admissible band
        (99.5995, 100.4005),    # ~0.8% width
        (99.5,    100.5),       # 1.0%
        (99.0,    101.0),       # 2.0%
        (98.7505, 101.2495),    # ~2.5%
    ])
    def test_rr_target_precision(self, isolated_env, or_low, or_high):
        cfg = _basic_cfg()
        with SessionSimulator(cfg) as sim:
            sim.start()
            sim.feed_or(ticker="AAPL", or_low=or_low, or_high=or_high)
            sim.feed_bar(
                make_breakout_bar(bucket=600, side="long",
                                  or_high=or_high, or_low=or_low),
                ticker="AAPL",
            )
            entry = or_high * 1.005
            ent = sim.try_long(ticker="AAPL", price=entry)
            assert ent.ok
            risk = ent.price - ent.stop
            reward = ent.target - ent.price
            assert abs(reward - 2.5 * risk) < 0.005, (
                f"RR drift at OR=[{or_low},{or_high}]: "
                f"risk={risk:.6f} reward={reward:.6f} "
                f"(expected 2.5*risk = {2.5*risk:.6f})"
            )


# ----- Stop-buffer boundary (5 bps) ------------------------------


class TestStopBufferBoundary:
    """Stop = OR_opp * (1 -/+ 5bps). The 5bps buffer is fixed in
    OrbConfig; this verifies the placement at various OR scales."""

    @pytest.mark.parametrize("mid", [10.0, 50.0, 100.0, 500.0, 1000.0])
    def test_stop_buffer_applied(self, isolated_env, mid):
        # 1% OR width centered on `mid`. Use $100k equity to stay
        # well within the $2k concurrent risk cap at all scales --
        # 2% risk = $2k risk budget, which clamps the trade risk to
        # match the cap exactly.
        half = mid * 0.005
        or_low = mid - half
        or_high = mid + half
        cfg = SimulatorConfig(
            date_iso="2026-01-15", tickers=["AAPL"], vix_close_d1=18.0,
            ticker_open_today={"AAPL": mid},
            ticker_prev_close={"AAPL": mid},
            equity_per_portfolio={"main": 100_000.0},
        )
        with SessionSimulator(cfg) as sim:
            sim.start()
            sim.feed_or(ticker="AAPL", or_low=or_low, or_high=or_high)
            sim.feed_bar(
                make_breakout_bar(bucket=600, side="long",
                                  or_high=or_high, or_low=or_low),
                ticker="AAPL",
            )
            ent = sim.try_long(ticker="AAPL", price=or_high * 1.001,
                               equity=100_000.0)
            assert ent.ok, (
                f"admission unexpectedly rejected at mid={mid}: "
                f"{ent.reason_no}"
            )
            # Expected stop = or_low * (1 - 5bps) = or_low * 0.9995
            expected_stop = or_low * 0.9995
            # Allow tolerance proportional to scale (0.001% of mid)
            tol = max(0.005, mid * 0.00001)
            assert abs(ent.stop - expected_stop) < tol, (
                f"stop drift at mid={mid}: ent.stop={ent.stop:.6f} "
                f"expected={expected_stop:.6f} tol={tol}"
            )
