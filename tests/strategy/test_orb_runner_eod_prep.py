"""R21 (v9.1.x) -- live-engine wiring of the runner_eod_prep lever.

Pins the behavior:
  - default 0 = OFF -- no behavior change vs Keystone v9.1.114 baseline.
  - When set, fires ONLY after partial_taken=True (so losing trades that
    never reached 1R are unaffected).
  - Fires AFTER stop/target checks (a clean stop/target on the same bar
    wins) but BEFORE the whole-session EOD cutoff.
  - Emits EXIT_RUNNER_EOD_PREP with price=bar_close.

Reference: docs/research/r21_partials_ladder.py sweep; quarterly stability
table in the v9.1.x changelog/PR. Production winner = 14:00 ET (840
minutes).
"""

from __future__ import annotations

from orb import exits as _exits
from orb.exits import (
    EXIT_EOD,
    EXIT_PARTIAL,
    EXIT_RUNNER_EOD_PREP,
    EXIT_STOP,
    EXIT_TARGET,
    ExitDecision,
    OrbPosition,
)


def _make_pos(
    *,
    side="long",
    entry=100.0,
    stop=99.0,
    target=102.5,
    shares=100,
    partial_taken=False,
    be_moved=False,
) -> OrbPosition:
    """Build an OrbPosition with explicit kwargs. Mirrors the shape the
    engine produces in production (see orb/exits.py:make_position)."""
    risk = abs(entry - stop)
    one_r = entry + risk if side == "long" else entry - risk
    return OrbPosition(
        portfolio_id="main",
        ticker="AAPL",
        side=side,
        entry_price=entry,
        stop=stop,
        target=target,
        risk=risk,
        one_r=one_r,
        shares=shares,
        risk_dollars=risk * shares,
        risk_ticket_id="t1",
        be_moved=be_moved,
        partial_taken=partial_taken,
        partial_pnl_dollars=0.0,
    )


class TestRunnerEodPrepDefaultOff:
    def test_default_zero_does_not_fire(self):
        """runner_eod_prep_min=0 (default) MUST NOT change anything --
        position with partial_taken=True at 14:00 just stays open."""
        pos = _make_pos(partial_taken=True, be_moved=True, stop=100.0)
        # Bar at 14:00 ET, no stop/target/EOD hit.
        result = _exits.evaluate(
            pos,
            bar_high=101.5,
            bar_low=100.5,
            bar_close=101.0,
            bar_bucket_min=14 * 60,
            eod_cutoff_min=15 * 60 + 55,
            partial_profit_at_1r=True,
            runner_eod_prep_min=0,
        )
        assert result is None, "default-off lever should not fire"


class TestRunnerEodPrepFires:
    def test_fires_at_threshold_when_partial_taken(self):
        """Set runner_eod_prep_min=14:00. Position is partial_taken,
        BE-moved, and the bar is at exactly 14:00 ET. Should emit
        EXIT_RUNNER_EOD_PREP at bar_close (not at the stop)."""
        pos = _make_pos(partial_taken=True, be_moved=True, stop=100.0)
        result = _exits.evaluate(
            pos,
            bar_high=101.5,
            bar_low=100.5,
            bar_close=101.0,
            bar_bucket_min=14 * 60,
            eod_cutoff_min=15 * 60 + 55,
            partial_profit_at_1r=True,
            runner_eod_prep_min=14 * 60,
        )
        assert result is not None
        assert result.reason == EXIT_RUNNER_EOD_PREP
        assert result.price == 101.0  # bar_close

    def test_fires_at_threshold_short_side(self):
        pos = _make_pos(
            side="short", entry=100.0, stop=101.0, target=97.5, partial_taken=True, be_moved=True
        )
        pos.stop = 100.0  # BE
        result = _exits.evaluate(
            pos,
            bar_high=99.5,
            bar_low=98.5,
            bar_close=99.0,
            bar_bucket_min=14 * 60,
            eod_cutoff_min=15 * 60 + 55,
            partial_profit_at_1r=True,
            runner_eod_prep_min=14 * 60,
        )
        assert result is not None
        assert result.reason == EXIT_RUNNER_EOD_PREP
        assert result.price == 99.0

    def test_does_not_fire_before_threshold(self):
        pos = _make_pos(partial_taken=True, be_moved=True, stop=100.0)
        result = _exits.evaluate(
            pos,
            bar_high=101.5,
            bar_low=100.5,
            bar_close=101.0,
            bar_bucket_min=13 * 60 + 59,  # 13:59 < 14:00
            eod_cutoff_min=15 * 60 + 55,
            partial_profit_at_1r=True,
            runner_eod_prep_min=14 * 60,
        )
        assert result is None


class TestRunnerEodPrepGatedByPartialTaken:
    def test_does_not_fire_when_partial_not_taken(self):
        """Losing trades (never hit 1R, so partial_taken=False) MUST NOT
        be force-exited by this lever. Otherwise we'd lock in losses we'd
        otherwise let ride to their stop. This is the safety guard."""
        pos = _make_pos(partial_taken=False, be_moved=False, stop=99.0)
        result = _exits.evaluate(
            pos,
            bar_high=99.7,
            bar_low=99.2,
            bar_close=99.5,  # below 1R
            bar_bucket_min=14 * 60 + 30,
            eod_cutoff_min=15 * 60 + 55,
            partial_profit_at_1r=True,
            runner_eod_prep_min=14 * 60,
        )
        assert result is None, "runner_eod_prep fired on a non-partialed position"


class TestRunnerEodPrepOrdering:
    def test_stop_wins_on_same_bar(self):
        """If the same bar hits the stop AND the runner_eod_prep time
        window, the stop should win (it's a cleaner price). EXIT_STOP
        or EXIT_BE_STOP, not EXIT_RUNNER_EOD_PREP."""
        pos = _make_pos(partial_taken=True, be_moved=True, stop=100.0)
        result = _exits.evaluate(
            pos,
            bar_high=100.5,
            bar_low=99.8,
            bar_close=100.1,  # low pierces stop
            bar_bucket_min=14 * 60 + 5,
            eod_cutoff_min=15 * 60 + 55,
            partial_profit_at_1r=True,
            runner_eod_prep_min=14 * 60,
        )
        assert result is not None
        assert result.reason in ("be_stop", "stop"), (
            f"expected stop to win on same-bar hit, got {result.reason}"
        )
        assert result.price == 100.0  # stop, not bar_close

    def test_target_wins_on_same_bar(self):
        """Same for target -- a clean hit > a time-based exit."""
        pos = _make_pos(partial_taken=True, be_moved=True, stop=100.0, target=102.5)
        result = _exits.evaluate(
            pos,
            bar_high=102.7,
            bar_low=101.5,
            bar_close=102.0,
            bar_bucket_min=14 * 60 + 5,
            eod_cutoff_min=15 * 60 + 55,
            partial_profit_at_1r=True,
            runner_eod_prep_min=14 * 60,
        )
        assert result is not None
        assert result.reason == EXIT_TARGET
        assert result.price == 102.5

    def test_fires_before_eod_cutoff(self):
        """A bar at 14:30 should fire runner_eod_prep, not wait for
        15:55 EOD. Confirms the new lever sits BEFORE the EOD check."""
        pos = _make_pos(partial_taken=True, be_moved=True, stop=100.0)
        result = _exits.evaluate(
            pos,
            bar_high=101.5,
            bar_low=100.5,
            bar_close=101.0,
            bar_bucket_min=14 * 60 + 30,
            eod_cutoff_min=15 * 60 + 55,
            partial_profit_at_1r=True,
            runner_eod_prep_min=14 * 60,
        )
        assert result is not None
        assert result.reason == EXIT_RUNNER_EOD_PREP, (
            f"expected runner_eod_prep to fire before EOD cutoff, got {result.reason}"
        )
