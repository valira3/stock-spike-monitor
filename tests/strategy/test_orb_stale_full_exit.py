"""R26 (v9.1.130) -- live-engine wiring of the stale_full_exit lever.

Mirror of test_orb_runner_eod_prep.py for the un-partialed cohort.

Pins the behavior:
  - default 0 = OFF -- no behavior change vs Keystone v9.1.114 baseline.
  - When set, fires ONLY when partial_taken=False (i.e. position never
    hit 1R) AND bar_bucket_min >= stale_full_exit_min.
  - Optional MFE-in-R floor (stale_full_exit_mfe_floor_r): if > 0,
    fires ONLY when mfe-in-R < floor (trades that came close to 1R
    are spared). floor <= 0 (default) = always fire at cutoff.
  - Fires AFTER stop/target checks but BEFORE EOD cutoff.
  - Emits EXIT_STALE_FULL_EXIT with price=bar_close.
  - Mutually exclusive with EXIT_RUNNER_EOD_PREP (one needs
    partial_taken=True, the other =False).

Reference: docs/research/r26_stale_full_exit.py sweep + r26_quarterly.py.
Production winner: 14:30 ET (870 min), no floor. Catches afternoon
driftback on positions that never hit 1R -- the legacy sentinel A
safety net that v9.1.128 portfolio independence removed for Val/Gene.
"""

from __future__ import annotations

from orb import exits as _exits
from orb.exits import (
    EXIT_EOD,
    EXIT_RUNNER_EOD_PREP,
    EXIT_STALE_FULL_EXIT,
    EXIT_STOP,
    EXIT_TARGET,
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
    mfe_price=None,
) -> OrbPosition:
    """Build an OrbPosition with explicit kwargs. Mirrors the shape
    the engine produces in production (see orb/exits.py:make_position).
    """
    risk = abs(entry - stop)
    one_r = entry + risk if side == "long" else entry - risk
    return OrbPosition(
        portfolio_id="val",
        ticker="AVGO",
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
        mfe_price=mfe_price if mfe_price is not None else entry,
    )


class TestStaleFullExitDefaultOff:
    def test_default_zero_does_not_fire(self):
        """stale_full_exit_min=0 (default) MUST NOT change behavior --
        an un-partialed position at 14:30 just stays open."""
        pos = _make_pos(partial_taken=False)
        result = _exits.evaluate(
            pos,
            bar_high=100.3,
            bar_low=99.7,
            bar_close=100.1,
            bar_bucket_min=14 * 60 + 30,
            eod_cutoff_min=15 * 60 + 55,
            partial_profit_at_1r=True,
            stale_full_exit_min=0,
        )
        assert result is None


class TestStaleFullExitFires:
    def test_fires_on_un_partialed_at_cutoff(self):
        """Long un-partialed position, time >= cutoff, no floor --
        force-close at bar_close."""
        pos = _make_pos(partial_taken=False)
        result = _exits.evaluate(
            pos,
            bar_high=100.5,
            bar_low=99.7,
            bar_close=100.2,
            bar_bucket_min=14 * 60 + 30,
            eod_cutoff_min=15 * 60 + 55,
            partial_profit_at_1r=True,
            stale_full_exit_min=14 * 60 + 30,
        )
        assert result is not None
        assert result.reason == EXIT_STALE_FULL_EXIT
        assert result.price == 100.2

    def test_fires_on_short_un_partialed_at_cutoff(self):
        """Mirror for SHORT side."""
        pos = _make_pos(side="short", entry=100.0, stop=101.0, target=97.5, partial_taken=False)
        result = _exits.evaluate(
            pos,
            bar_high=100.5,
            bar_low=99.7,
            bar_close=99.8,
            bar_bucket_min=14 * 60 + 30,
            eod_cutoff_min=15 * 60 + 55,
            stale_full_exit_min=14 * 60 + 30,
        )
        assert result is not None
        assert result.reason == EXIT_STALE_FULL_EXIT

    def test_does_not_fire_before_cutoff(self):
        """Before bar_bucket_min reaches cutoff, lever is dormant."""
        pos = _make_pos(partial_taken=False)
        result = _exits.evaluate(
            pos,
            bar_high=100.3,
            bar_low=99.7,
            bar_close=100.0,
            bar_bucket_min=13 * 60 + 30,  # before 14:30
            eod_cutoff_min=15 * 60 + 55,
            stale_full_exit_min=14 * 60 + 30,
        )
        assert result is None


class TestStaleFullExitPartialGate:
    def test_does_not_fire_when_partial_taken(self):
        """If partial_at_1r already fired, this lever is dormant; R21
        runner_eod_prep handles the runner instead."""
        pos = _make_pos(partial_taken=True, be_moved=True, stop=100.0)
        result = _exits.evaluate(
            pos,
            bar_high=100.6,
            bar_low=100.1,
            bar_close=100.4,
            bar_bucket_min=14 * 60 + 30,
            eod_cutoff_min=15 * 60 + 55,
            stale_full_exit_min=14 * 60 + 30,
        )
        # The stale_full_exit lever should NOT fire; the runner_eod_prep
        # lever is what handles partialed runners (not enabled in this
        # test). Position stays open.
        assert result is None

    def test_mutually_exclusive_with_runner_eod_prep(self):
        """When BOTH levers are set: partialed position triggers R21,
        un-partialed triggers R26. They never both fire."""
        # Partialed: runner_eod_prep wins.
        pos1 = _make_pos(partial_taken=True, be_moved=True, stop=100.0)
        r1 = _exits.evaluate(
            pos1,
            bar_high=100.6,
            bar_low=100.1,
            bar_close=100.4,
            bar_bucket_min=14 * 60 + 30,
            eod_cutoff_min=15 * 60 + 55,
            runner_eod_prep_min=14 * 60,
            stale_full_exit_min=14 * 60 + 30,
        )
        assert r1 is not None and r1.reason == EXIT_RUNNER_EOD_PREP

        # Un-partialed: stale_full_exit wins.
        pos2 = _make_pos(partial_taken=False)
        r2 = _exits.evaluate(
            pos2,
            bar_high=100.3,
            bar_low=99.7,
            bar_close=100.0,
            bar_bucket_min=14 * 60 + 30,
            eod_cutoff_min=15 * 60 + 55,
            runner_eod_prep_min=14 * 60,
            stale_full_exit_min=14 * 60 + 30,
        )
        assert r2 is not None and r2.reason == EXIT_STALE_FULL_EXIT


class TestStaleFullExitStopAndTargetWin:
    def test_stop_fires_first_on_same_bar(self):
        """If the same bar pierces the stop AND time >= cutoff, stop
        wins (pessimistic ordering)."""
        pos = _make_pos(partial_taken=False, stop=99.0)
        result = _exits.evaluate(
            pos,
            bar_high=100.3,
            bar_low=98.5,  # pierces stop
            bar_close=99.5,
            bar_bucket_min=14 * 60 + 30,
            eod_cutoff_min=15 * 60 + 55,
            stale_full_exit_min=14 * 60 + 30,
        )
        assert result is not None
        assert result.reason == EXIT_STOP
        assert result.price == 99.0

    def test_target_fires_first_on_same_bar(self):
        """If the same bar pierces the target AND time >= cutoff,
        target wins. Bar_low kept above entry so BE-armed stop (which
        moves to entry when bar_high crosses 1R) is NOT touched."""
        pos = _make_pos(partial_taken=False, target=102.5)
        result = _exits.evaluate(
            pos,
            bar_high=103.0,  # pierces target (and crosses 1R, arms BE)
            bar_low=101.0,   # above entry=100 so BE-stop doesn't trigger
            bar_close=102.8,
            bar_bucket_min=14 * 60 + 30,
            eod_cutoff_min=15 * 60 + 55,
            stale_full_exit_min=14 * 60 + 30,
        )
        assert result is not None
        assert result.reason == EXIT_TARGET
        assert result.price == 102.5


class TestStaleFullExitMfeFloor:
    def test_floor_spares_trade_that_came_close_to_1R(self):
        """With floor=0.5: a position whose MFE was 0.7R should NOT
        be force-closed (still ride to stop/target/EOD)."""
        pos = _make_pos(partial_taken=False, mfe_price=100.7)  # 0.7R favorable
        result = _exits.evaluate(
            pos,
            bar_high=100.3,
            bar_low=99.7,
            bar_close=100.0,
            bar_bucket_min=14 * 60 + 30,
            eod_cutoff_min=15 * 60 + 55,
            stale_full_exit_min=14 * 60 + 30,
            stale_full_exit_mfe_floor_r=0.5,
        )
        assert result is None

    def test_floor_force_closes_low_mfe_trade(self):
        """With floor=0.5: a position whose MFE was 0.3R IS closed."""
        pos = _make_pos(partial_taken=False, mfe_price=100.3)  # 0.3R favorable
        result = _exits.evaluate(
            pos,
            bar_high=100.3,
            bar_low=99.7,
            bar_close=100.0,
            bar_bucket_min=14 * 60 + 30,
            eod_cutoff_min=15 * 60 + 55,
            stale_full_exit_min=14 * 60 + 30,
            stale_full_exit_mfe_floor_r=0.5,
        )
        assert result is not None
        assert result.reason == EXIT_STALE_FULL_EXIT

    def test_floor_zero_disables_gate(self):
        """floor=0 (default) means MFE check is bypassed -- ANY
        un-partialed position at cutoff is closed."""
        pos = _make_pos(partial_taken=False, mfe_price=100.9)  # 0.9R, very close
        result = _exits.evaluate(
            pos,
            bar_high=100.3,
            bar_low=99.7,
            bar_close=100.0,
            bar_bucket_min=14 * 60 + 30,
            eod_cutoff_min=15 * 60 + 55,
            stale_full_exit_min=14 * 60 + 30,
            stale_full_exit_mfe_floor_r=0.0,
        )
        assert result is not None
        assert result.reason == EXIT_STALE_FULL_EXIT

    def test_mfe_updates_on_each_bar_long(self):
        """evaluate() updates pos.mfe_price for LONG with bar_high."""
        pos = _make_pos(partial_taken=False)
        assert pos.mfe_price == 100.0
        # Bar pushes high to 100.4 -- mfe should update.
        _exits.evaluate(
            pos,
            bar_high=100.4,
            bar_low=99.7,
            bar_close=100.1,
            bar_bucket_min=10 * 60,
            eod_cutoff_min=15 * 60 + 55,
        )
        assert pos.mfe_price == 100.4

    def test_mfe_updates_on_each_bar_short(self):
        """evaluate() updates pos.mfe_price for SHORT with bar_low."""
        pos = _make_pos(side="short", entry=100.0, stop=101.0, target=97.5, partial_taken=False)
        # SHORT make_position sets mfe_price = entry_price = 100.0; the
        # eval below should drop it to 99.6.
        assert pos.mfe_price == 100.0
        _exits.evaluate(
            pos,
            bar_high=100.2,
            bar_low=99.6,
            bar_close=99.9,
            bar_bucket_min=10 * 60,
            eod_cutoff_min=15 * 60 + 55,
        )
        assert pos.mfe_price == 99.6


class TestStaleFullExitBeforeEod:
    def test_fires_before_eod_cutoff_on_same_bar(self):
        """If time matches both stale_full_exit AND eod_cutoff,
        stale_full_exit wins (precedes EOD in evaluate ordering)."""
        pos = _make_pos(partial_taken=False)
        result = _exits.evaluate(
            pos,
            bar_high=100.3,
            bar_low=99.7,
            bar_close=100.1,
            bar_bucket_min=15 * 60 + 55,  # at EOD cutoff
            eod_cutoff_min=15 * 60 + 55,
            stale_full_exit_min=14 * 60 + 30,
        )
        assert result is not None
        assert result.reason == EXIT_STALE_FULL_EXIT

    def test_eod_wins_when_stale_full_exit_disabled(self):
        """If stale_full_exit_min=0 and EOD reached, EXIT_EOD fires."""
        pos = _make_pos(partial_taken=False)
        result = _exits.evaluate(
            pos,
            bar_high=100.3,
            bar_low=99.7,
            bar_close=100.1,
            bar_bucket_min=15 * 60 + 55,
            eod_cutoff_min=15 * 60 + 55,
            stale_full_exit_min=0,
        )
        assert result is not None
        assert result.reason == EXIT_EOD
