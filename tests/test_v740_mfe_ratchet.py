"""v7.4.0 \u2014 MFE-Ratchet Trail (Lever #3) unit tests.

Mechanism: once favorable >= V740_MFE_RATCHET_ARM_R * 1R, propose a stop
at entry +/- V740_MFE_RATCHET_FRAC * (peak_close - entry). Stacks on
the existing BE+pad / chandelier candidates via side-aware max/min;
one-way ratchet via state.last_proposed_stop ensures the floor never
decays.

Default OFF (V740_MFE_RATCHET_ENABLED=0) \u2014 these tests monkeypatch
the module flag to True. Verifies zero behavioural change when the
flag is off.
"""
from __future__ import annotations

from engine.alarm_f_trail import (
    STAGE_BREAKEVEN,
    STAGE_CHANDELIER_WIDE,
    STAGE_INACTIVE,
    TrailState,
    propose_stop,
    update_trail,
)
from engine.sentinel import SIDE_LONG, SIDE_SHORT
from engine import alarm_f_trail as _alarm_f_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enable_ratchet(monkeypatch, *, frac=0.5, arm_r=1.0):
    monkeypatch.setattr(_alarm_f_module, "V740_MFE_RATCHET_ENABLED", True)
    monkeypatch.setattr(_alarm_f_module, "V740_MFE_RATCHET_FRAC", frac)
    monkeypatch.setattr(_alarm_f_module, "V740_MFE_RATCHET_ARM_R", arm_r)


def _disable_ratchet(monkeypatch):
    monkeypatch.setattr(_alarm_f_module, "V740_MFE_RATCHET_ENABLED", False)


def _state_with_peak(side, entry, peak, stage=STAGE_BREAKEVEN):
    s = TrailState.fresh()
    s.peak_close = float(peak)
    s.stage = stage
    s.bars_seen = 5  # past MIN_BARS_BEFORE_ARM
    return s


# ---------------------------------------------------------------------------
# Off-by-default: zero behavioural change
# ---------------------------------------------------------------------------


def test_disabled_by_default_long(monkeypatch):
    """When V740_MFE_RATCHET_ENABLED is False, propose_stop returns the
    same value it always has (BE+pad once Stage 1 armed). Pinning the
    legacy contract."""
    _disable_ratchet(monkeypatch)
    s = _state_with_peak(SIDE_LONG, entry=100.0, peak=103.0)  # +3R favorable
    proposed = propose_stop(
        state=s, side=SIDE_LONG, entry_price=100.0,
        atr_value=None, current_stop_price=99.0, last_close=102.0,
        r_per_share=1.0,
    )
    # Stage 1 BE+pad on a $100 long = entry + max($0.01, 5bp*100) = 100.05
    assert proposed is not None
    assert abs(proposed - 100.05) < 1e-6, f"expected BE+pad 100.05, got {proposed}"


def test_legacy_caller_no_r_per_share(monkeypatch):
    """A legacy caller that does NOT pass r_per_share gets identical
    legacy behaviour even when the env flag is True. Belt and braces."""
    _enable_ratchet(monkeypatch, frac=0.5)
    s = _state_with_peak(SIDE_LONG, entry=100.0, peak=103.0)
    proposed = propose_stop(
        state=s, side=SIDE_LONG, entry_price=100.0,
        atr_value=None, current_stop_price=99.0, last_close=102.0,
        # r_per_share intentionally omitted
    )
    # No ratchet candidate \u2192 BE+pad wins
    assert proposed is not None
    assert abs(proposed - 100.05) < 1e-6


# ---------------------------------------------------------------------------
# Arming threshold
# ---------------------------------------------------------------------------


def test_no_ratchet_below_arm_threshold_long(monkeypatch):
    """At +0.5R favorable (below 1.0R arm), ratchet candidate is not
    added. BE+pad still wins."""
    _enable_ratchet(monkeypatch, frac=0.5, arm_r=1.0)
    s = _state_with_peak(SIDE_LONG, entry=100.0, peak=100.50)  # +0.5R
    # Force stage to allow BE+pad candidate (stage >= 1)
    proposed = propose_stop(
        state=s, side=SIDE_LONG, entry_price=100.0,
        atr_value=None, current_stop_price=99.0, last_close=100.30,
        r_per_share=1.0,
    )
    # Ratchet would propose 100 + 0.5*0.5 = 100.25 (below BE+pad of
    # 100.05 \u2014 actually higher than BE+pad). Wait: 100.25 > 100.05.
    # The ratchet should NOT have armed (favorable=0.5 < 1.0=arm).
    # If it had armed, stop would be 100.25 (max of [100.05, 100.25]).
    assert proposed is not None
    assert abs(proposed - 100.05) < 1e-6, (
        f"ratchet armed below threshold: got {proposed}, expected 100.05"
    )


def test_ratchet_arms_at_exactly_arm_threshold_long(monkeypatch):
    """At exactly +1R favorable, ratchet just barely arms. With
    frac=0.5, candidate = 100 + 0.5*1.0 = 100.50 which beats BE+pad
    of 100.05."""
    _enable_ratchet(monkeypatch, frac=0.5, arm_r=1.0)
    s = _state_with_peak(SIDE_LONG, entry=100.0, peak=101.0)  # +1R
    proposed = propose_stop(
        state=s, side=SIDE_LONG, entry_price=100.0,
        atr_value=None, current_stop_price=99.0, last_close=100.80,
        r_per_share=1.0,
    )
    # Ratchet at 100.50 beats BE+pad at 100.05
    assert proposed is not None
    assert abs(proposed - 100.50) < 1e-6, f"expected 100.50, got {proposed}"


# ---------------------------------------------------------------------------
# Ratchet math \u2014 long
# ---------------------------------------------------------------------------


def test_ratchet_locks_half_of_run_long_frac_05(monkeypatch):
    """Long entry $100, peak $103 (+3R), frac=0.5. Ratchet locks
    100 + 0.5*3 = 101.50. Wins vs BE+pad 100.05."""
    _enable_ratchet(monkeypatch, frac=0.5)
    s = _state_with_peak(SIDE_LONG, entry=100.0, peak=103.0)
    proposed = propose_stop(
        state=s, side=SIDE_LONG, entry_price=100.0,
        atr_value=None, current_stop_price=99.0, last_close=102.5,
        r_per_share=1.0,
    )
    assert proposed is not None
    assert abs(proposed - 101.50) < 1e-6, f"expected 101.50, got {proposed}"


def test_ratchet_tighter_with_frac_07_long(monkeypatch):
    """Same scenario, frac=0.7 \u2192 lock 100 + 0.7*3 = 102.10."""
    _enable_ratchet(monkeypatch, frac=0.7)
    s = _state_with_peak(SIDE_LONG, entry=100.0, peak=103.0)
    proposed = propose_stop(
        state=s, side=SIDE_LONG, entry_price=100.0,
        atr_value=None, current_stop_price=99.0, last_close=102.5,
        r_per_share=1.0,
    )
    assert proposed is not None
    assert abs(proposed - 102.10) < 1e-6, f"expected 102.10, got {proposed}"


def test_ratchet_looser_with_frac_03_long(monkeypatch):
    """Same scenario, frac=0.3 \u2192 lock 100 + 0.3*3 = 100.90."""
    _enable_ratchet(monkeypatch, frac=0.3)
    s = _state_with_peak(SIDE_LONG, entry=100.0, peak=103.0)
    proposed = propose_stop(
        state=s, side=SIDE_LONG, entry_price=100.0,
        atr_value=None, current_stop_price=99.0, last_close=102.5,
        r_per_share=1.0,
    )
    assert proposed is not None
    assert abs(proposed - 100.90) < 1e-6, f"expected 100.90, got {proposed}"


# ---------------------------------------------------------------------------
# Ratchet math \u2014 short (mirror)
# ---------------------------------------------------------------------------


def test_ratchet_locks_half_of_run_short_frac_05(monkeypatch):
    """Short entry $100, peak (low) $97 (+3R favorable), frac=0.5.
    Ratchet locks 100 - 0.5*3 = 98.50. Wins vs BE-pad 99.95."""
    _enable_ratchet(monkeypatch, frac=0.5)
    s = _state_with_peak(SIDE_SHORT, entry=100.0, peak=97.0)
    proposed = propose_stop(
        state=s, side=SIDE_SHORT, entry_price=100.0,
        atr_value=None, current_stop_price=101.0, last_close=97.5,
        r_per_share=1.0,
    )
    assert proposed is not None
    assert abs(proposed - 98.50) < 1e-6, f"expected 98.50, got {proposed}"


# ---------------------------------------------------------------------------
# One-way ratchet: floor never decays even as MFE pulls back
# ---------------------------------------------------------------------------


def test_pullback_does_not_decay_ratchet_long(monkeypatch):
    """Long peaks at $103 (ratchet locks 101.50), then pulls back to
    $101.20. peak_close stays at $103 (it's a high-water mark in
    update_trail), so ratchet candidate stays at 101.50. Even if it
    didn't, the last_proposed_stop one-way ratchet would hold."""
    _enable_ratchet(monkeypatch, frac=0.5)
    s = TrailState.fresh()
    s.bars_seen = 5
    s.stage = STAGE_BREAKEVEN
    # First call: walks peak to 103
    update_trail(
        state=s, side=SIDE_LONG, entry_price=100.0,
        last_close=103.0, atr_value=None, r_dollars=1.0, shares=1,
    )
    p1 = propose_stop(
        state=s, side=SIDE_LONG, entry_price=100.0,
        atr_value=None, current_stop_price=99.0, last_close=103.0,
        r_per_share=1.0,
    )
    assert p1 is not None
    assert abs(p1 - 101.50) < 1e-6

    # Pullback bar: peak_close stays at 103 (max-only)
    update_trail(
        state=s, side=SIDE_LONG, entry_price=100.0,
        last_close=101.20, atr_value=None, r_dollars=1.0, shares=1,
    )
    assert s.peak_close == 103.0, "peak_close decayed on pullback!"

    # propose_stop returns None because ratchet candidate (101.50) is
    # not strictly tighter than current stop (101.50 from p1 \u2014 the
    # caller would have installed it). Mirror that:
    p2 = propose_stop(
        state=s, side=SIDE_LONG, entry_price=100.0,
        atr_value=None, current_stop_price=101.50, last_close=101.20,
        r_per_share=1.0,
    )
    # Strictly-tighter gate: 101.50 not > 101.50, so None.
    assert p2 is None


def test_ratchet_advances_with_new_peak_long(monkeypatch):
    """Long peaks at $103 (lock 101.50), then climbs to $105 (lock
    102.50). One-way ratchet advances cleanly."""
    _enable_ratchet(monkeypatch, frac=0.5)
    s = TrailState.fresh()
    s.bars_seen = 5
    s.stage = STAGE_BREAKEVEN

    update_trail(
        state=s, side=SIDE_LONG, entry_price=100.0,
        last_close=103.0, atr_value=None, r_dollars=1.0, shares=1,
    )
    p1 = propose_stop(
        state=s, side=SIDE_LONG, entry_price=100.0,
        atr_value=None, current_stop_price=99.0, last_close=103.0,
        r_per_share=1.0,
    )
    assert abs(p1 - 101.50) < 1e-6

    update_trail(
        state=s, side=SIDE_LONG, entry_price=100.0,
        last_close=105.0, atr_value=None, r_dollars=1.0, shares=1,
    )
    p2 = propose_stop(
        state=s, side=SIDE_LONG, entry_price=100.0,
        atr_value=None, current_stop_price=101.50, last_close=105.0,
        r_per_share=1.0,
    )
    assert p2 is not None
    assert abs(p2 - 102.50) < 1e-6, f"expected 102.50, got {p2}"


# ---------------------------------------------------------------------------
# Interactions with chandelier
# ---------------------------------------------------------------------------


def test_chandelier_tighter_than_ratchet_wins_long(monkeypatch):
    """When the chandelier level is tighter than the ratchet (e.g.
    very low ATR, big run), chandelier candidate dominates. Verifies
    side-aware max picks the higher (tighter) of the two for long."""
    _enable_ratchet(monkeypatch, frac=0.3)  # loose ratchet
    s = TrailState.fresh()
    s.bars_seen = 5
    s.stage = STAGE_CHANDELIER_WIDE
    s.peak_close = 105.0  # +5R
    # WIDE_MULT default = 1.5, ATR=0.5 \u2192 chandelier = 105 - 1.5*0.5 = 104.25
    # Ratchet @ frac=0.3 = 100 + 0.3*5 = 101.50 (looser than 104.25)
    proposed = propose_stop(
        state=s, side=SIDE_LONG, entry_price=100.0,
        atr_value=0.5, current_stop_price=99.0, last_close=104.50,
        r_per_share=1.0,
    )
    assert proposed is not None
    assert abs(proposed - 104.25) < 1e-6, (
        f"chandelier (104.25) should beat ratchet (101.50); got {proposed}"
    )


def test_ratchet_tighter_than_chandelier_wins_long(monkeypatch):
    """When the ratchet is tighter than the chandelier (high ATR
    cushion, modest run), ratchet candidate wins."""
    _enable_ratchet(monkeypatch, frac=0.7)  # tight ratchet
    s = TrailState.fresh()
    s.bars_seen = 5
    s.stage = STAGE_CHANDELIER_WIDE
    s.peak_close = 103.0  # +3R
    # Chandelier with high ATR=2.0 \u2192 103 - 1.5*2.0 = 100.0 (loose)
    # Ratchet @ frac=0.7 = 100 + 0.7*3 = 102.10 (tighter)
    proposed = propose_stop(
        state=s, side=SIDE_LONG, entry_price=100.0,
        atr_value=2.0, current_stop_price=99.0, last_close=102.50,
        r_per_share=1.0,
    )
    assert proposed is not None
    assert abs(proposed - 102.10) < 1e-6, (
        f"ratchet (102.10) should beat chandelier (100.00); got {proposed}"
    )


# ---------------------------------------------------------------------------
# Safety / edge cases
# ---------------------------------------------------------------------------


def test_safety_floor_blocks_wrong_side_of_mark_long(monkeypatch):
    """v7.2.6 safety floor still applies. If ratchet would propose
    101.50 but mark is at 101.20, the proposal is rejected (would
    fire instantly)."""
    _enable_ratchet(monkeypatch, frac=0.5)
    s = _state_with_peak(SIDE_LONG, entry=100.0, peak=103.0)
    # Mark below ratchet level \u2192 safety floor rejects
    proposed = propose_stop(
        state=s, side=SIDE_LONG, entry_price=100.0,
        atr_value=None, current_stop_price=99.0, last_close=101.20,
        r_per_share=1.0,
    )
    # 101.50 >= 101.20 - 0.05 (safety_pad), so rejected \u2192 None
    assert proposed is None


def test_zero_r_per_share_no_op(monkeypatch):
    """Degenerate r_per_share=0 disables ratchet. BE+pad wins."""
    _enable_ratchet(monkeypatch, frac=0.5)
    s = _state_with_peak(SIDE_LONG, entry=100.0, peak=103.0)
    proposed = propose_stop(
        state=s, side=SIDE_LONG, entry_price=100.0,
        atr_value=None, current_stop_price=99.0, last_close=102.0,
        r_per_share=0.0,
    )
    assert proposed is not None
    assert abs(proposed - 100.05) < 1e-6, (
        f"r_per_share=0 should disable ratchet, got {proposed}"
    )


def test_inactive_stage_returns_none_long(monkeypatch):
    """Stage INACTIVE \u2192 propose_stop returns None regardless of
    ratchet state. Pre-existing contract; ratchet doesn't bypass it."""
    _enable_ratchet(monkeypatch, frac=0.5)
    s = _state_with_peak(SIDE_LONG, entry=100.0, peak=103.0,
                         stage=STAGE_INACTIVE)
    proposed = propose_stop(
        state=s, side=SIDE_LONG, entry_price=100.0,
        atr_value=None, current_stop_price=99.0, last_close=102.0,
        r_per_share=1.0,
    )
    assert proposed is None
