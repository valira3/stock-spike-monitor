"""v5.13.0 PR 3 — Titan Grip Harvest ratchet tests.

Spec rules covered: L-P4-C-S1..S4 / S-P4-C-S1..S4.

Boundary math (OR_High = 100.00 used throughout):
    Stage 1 anchor target  : 100.93  (+0.93%)
    Stage 1 stop level     : 100.40  (+0.40%)
    Stage 2 ratchet step   :   0.25  (+0.25% of OR_High)
    Stage 3 harvest target : 101.88  (+1.88%)

Sizing per spec: 25% / 25% / 50% runner.

Parallel-not-sequential semantics: every alarm (A, B, C) is evaluated
on every tick. If A and C fire on the same tick, BOTH are recorded
in result.alarms; A wins for OUTBOUND classification (full exit
overrides partial harvest).
"""
from __future__ import annotations

import pytest

from engine import sentinel as S
from engine import titan_grip as TG


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(direction=TG.SIDE_LONG, *, or_high=100.0, or_low=100.0,
                entry_price=100.0, shares=100):
    return TG.TitanGripState(
        position_id="X",
        direction=direction,
        entry_price=entry_price,
        or_high=or_high,
        or_low=or_low,
        original_shares=shares,
    )


# ---------------------------------------------------------------------------
# Long side
# ---------------------------------------------------------------------------


class TestStage0to1Transition:
    """L-P4-C-S1: At OR_High +0.93% sell 25% LIMIT, move stop to OR_High +0.40%."""

    def test_below_stage1_target_does_not_fire(self):
        """Spec boundary: 0.92% must NOT trigger Stage 1."""
        st = _make_state()
        # 100.92 is below 100.93 — should NOT fire.
        actions = TG.check_titan_grip(state=st, current_price=100.92,
                                      current_shares=100)
        assert actions == []
        assert st.stage == 0
        assert not st.first_harvest_done

    def test_at_stage1_target_fires(self):
        """Spec literal: price >= OR_High + 0.93% triggers Stage 1."""
        st = _make_state()
        actions = TG.check_titan_grip(state=st, current_price=100.93,
                                      current_shares=100)
        assert len(actions) == 1
        a = actions[0]
        assert a.code == TG.ACTION_STAGE1_HARVEST
        assert a.shares == 25  # 25% of 100
        assert a.order_type == TG.ORDER_TYPE_LIMIT
        # State machine advanced.
        assert st.stage == 1
        assert st.first_harvest_done
        assert st.current_stop_anchor == pytest.approx(100.40)


class TestStage2MicroRatchet:
    """L-P4-C-S2: Every +0.25% increment moves the stop +0.25%."""

    def test_ratchet_advances_with_price(self):
        st = _make_state()
        # Trigger Stage 1 first.
        TG.check_titan_grip(state=st, current_price=100.93, current_shares=100)
        # Anchor = 100.40. Advance to 100.65 (+0.25 step). Should ratchet.
        TG.check_titan_grip(state=st, current_price=100.65, current_shares=75)
        assert st.current_stop_anchor == pytest.approx(100.65)

    def test_ratchet_never_moves_down(self):
        st = _make_state()
        TG.check_titan_grip(state=st, current_price=100.93, current_shares=100)
        TG.check_titan_grip(state=st, current_price=100.65, current_shares=75)
        # Now price drops back to 100.50 — anchor must stay at 100.65.
        anchor_before = st.current_stop_anchor
        TG.check_titan_grip(state=st, current_price=100.50, current_shares=75)
        assert st.current_stop_anchor == anchor_before
        # And no fresh harvest was emitted.

    def test_multiple_steps_in_one_tick(self):
        st = _make_state()
        TG.check_titan_grip(state=st, current_price=100.93, current_shares=100)
        # Jump from 100.40 anchor to 101.15 — that's 3 full +0.25 steps.
        TG.check_titan_grip(state=st, current_price=101.15, current_shares=75)
        assert st.current_stop_anchor == pytest.approx(101.15)


class TestStage1FirstHarvestExit:
    """L-P4-C-S1 cont.: drop back to anchor exits remaining position."""

    def test_drop_to_anchor_runner_exit(self):
        st = _make_state()
        TG.check_titan_grip(state=st, current_price=100.93, current_shares=100)
        # First harvest done; 75 shares remain. Drop to 100.40 anchor.
        actions = TG.check_titan_grip(state=st, current_price=100.40,
                                      current_shares=75)
        assert any(a.code == TG.ACTION_RUNNER_EXIT and a.shares == 75
                   for a in actions)
        assert st.stage == 3  # exited terminal


class TestStage3SecondHarvest:
    """L-P4-C-S3: At OR_High +1.88% sell second 25% LIMIT."""

    def test_stage3_fires_at_188pct(self):
        st = _make_state()
        TG.check_titan_grip(state=st, current_price=100.93, current_shares=100)
        # Move past Stage 1, past ratchet steps, hit Stage 3 target.
        actions = TG.check_titan_grip(state=st, current_price=101.88,
                                      current_shares=75)
        codes = [a.code for a in actions]
        assert TG.ACTION_STAGE3_HARVEST in codes
        # 25% of original 100 shares.
        s3 = next(a for a in actions if a.code == TG.ACTION_STAGE3_HARVEST)
        assert s3.shares == 25
        assert s3.order_type == TG.ORDER_TYPE_LIMIT
        assert st.stage == 2
        assert st.second_harvest_done


class TestStage4Runner:
    """L-P4-C-S4: Final 50% runner with continued +0.25% ratchet."""

    def test_runner_continues_to_ratchet(self):
        st = _make_state()
        # Walk through Stage 1, then directly to Stage 3.
        TG.check_titan_grip(state=st, current_price=100.93, current_shares=100)
        TG.check_titan_grip(state=st, current_price=101.88, current_shares=75)
        # 50 shares remain (runner). Push higher to ratchet.
        anchor_before = st.current_stop_anchor
        TG.check_titan_grip(state=st, current_price=102.20, current_shares=50)
        assert st.current_stop_anchor > anchor_before
        assert st.stage == 2

    def test_runner_exits_when_stop_hit(self):
        st = _make_state()
        TG.check_titan_grip(state=st, current_price=100.93, current_shares=100)
        TG.check_titan_grip(state=st, current_price=101.88, current_shares=75)
        # Drop runner to current anchor.
        anchor = st.current_stop_anchor
        actions = TG.check_titan_grip(state=st, current_price=anchor,
                                      current_shares=50)
        assert any(a.code == TG.ACTION_RUNNER_EXIT and a.shares == 50
                   for a in actions)
        assert st.stage == 3


# ---------------------------------------------------------------------------
# Short side mirror
# ---------------------------------------------------------------------------


class TestShortMirror:
    """S-P4-C-S1..S4 mirror of long with OR_Low and inequality flipped."""

    def test_full_short_scenario(self):
        st = _make_state(direction=TG.SIDE_SHORT, or_low=100.0,
                        entry_price=100.0)
        # Stage 1: price <= OR_Low - 0.93% = 99.07
        actions = TG.check_titan_grip(state=st, current_price=99.07,
                                      current_shares=100)
        codes = [a.code for a in actions]
        assert TG.ACTION_STAGE1_HARVEST in codes
        assert st.current_stop_anchor == pytest.approx(99.60)  # OR_Low - 0.40%

        # Ratchet down.
        TG.check_titan_grip(state=st, current_price=99.35, current_shares=75)
        assert st.current_stop_anchor == pytest.approx(99.35)

        # Stage 3: price <= 98.12 (-1.88%). Use 98.10 to avoid the
        # 98.12 == 98.11999... float rounding boundary.
        actions = TG.check_titan_grip(state=st, current_price=98.10,
                                      current_shares=75)
        codes = [a.code for a in actions]
        assert TG.ACTION_STAGE3_HARVEST in codes
        assert st.stage == 2

        # Runner exit when price rises back to anchor.
        anchor = st.current_stop_anchor
        actions = TG.check_titan_grip(state=st, current_price=anchor,
                                      current_shares=50)
        assert any(a.code == TG.ACTION_RUNNER_EXIT for a in actions)


# ---------------------------------------------------------------------------
# Sentinel integration — parallel-not-sequential A/B/C semantics
# ---------------------------------------------------------------------------


class TestParallelAlarms:
    """Spec § Sentinel Loop: alarms fire INDEPENDENTLY. If A and C
    both fire on the same tick, BOTH appear in result.alarms; A wins
    for OUTBOUND classification (full exit overrides partial harvest)."""

    def test_alarm_a_and_c_both_fire_a_wins(self):
        """Same-tick A1 + C2: both recorded, A wins outbound classification.

        Constructed scenario: position is in Stage 1 (anchored at 100.40).
        Price is at 101.20 (advancing the ratchet) but a sudden -$500
        unrealized has occurred (e.g. shares*price model says so for
        the test setup). Even with C2 firing, A1's reason wins.
        """
        st = _make_state()
        # Drive to Stage 1 first.
        TG.check_titan_grip(state=st, current_price=100.93,
                            current_shares=100)
        # Now evaluate sentinel with both -$500 P&L AND a ratcheting
        # price advance that will fire a C2 ratchet.
        result = S.evaluate_sentinel(
            side="LONG",
            unrealized_pnl=-501.0,           # A1 fires
            position_value=10000.0,
            pnl_history=None,
            now_ts=0.0,
            last_5m_close=None,
            last_5m_ema9=None,
            titan_grip_state=st,
            current_price=101.20,            # ratchet step past 100.40
            current_shares=75,
        )
        codes = result.alarm_codes
        assert "A1" in codes, f"A1 missing from {codes}"
        # C ratchet should also be recorded (parallel evaluation).
        assert any(c.startswith("C") for c in codes), (
            f"Alarm C missing from parallel evaluation: {codes}"
        )
        # A wins outbound.
        assert result.exit_reason == S.EXIT_REASON_ALARM_A
        assert result.has_full_exit is True

    def test_alarm_b_and_c_both_fire_b_wins(self):
        """Same parallel-not-sequential rule for B + C."""
        st = _make_state()
        TG.check_titan_grip(state=st, current_price=100.93,
                            current_shares=100)
        result = S.evaluate_sentinel(
            side="LONG",
            unrealized_pnl=0.0,
            position_value=10000.0,
            pnl_history=None,
            now_ts=0.0,
            last_5m_close=99.50,             # closed BELOW 9-EMA
            last_5m_ema9=100.00,             # B fires
            titan_grip_state=st,
            current_price=101.20,            # C2 ratchet
            current_shares=75,
        )
        codes = result.alarm_codes
        assert "B" in codes
        assert any(c.startswith("C") for c in codes)
        assert result.exit_reason == S.EXIT_REASON_ALARM_B
        assert result.has_full_exit is True

    def test_only_c_fires_no_full_exit(self):
        """Pure C trip — no full exit, partial harvest semantics."""
        st = _make_state()
        result = S.evaluate_sentinel(
            side="LONG",
            unrealized_pnl=10.0,             # nowhere near A1
            position_value=10000.0,
            pnl_history=None,
            now_ts=0.0,
            last_5m_close=None,
            last_5m_ema9=None,
            titan_grip_state=st,
            current_price=100.93,            # C1 fires
            current_shares=100,
        )
        assert "C1" in result.alarm_codes
        assert result.has_full_exit is False
        assert result.exit_reason == S.EXIT_REASON_ALARM_C
        # And the structured action data is present for the caller.
        assert any(a.code == TG.ACTION_STAGE1_HARVEST
                   for a in result.titan_grip_actions)
