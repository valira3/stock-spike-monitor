"""v7.2.6 \u2014 Entry-2 trail-inheritance bug fix.

NVDA flushed -$22.77 on 2026-05-07: Entry-2 top-up at $213.66 (averaged
from Entry-1) inherited the Entry-1 TrailState (stage=1, peak_close set,
bars_seen>0). propose_stop produced BE+pad = $213.77, which sat ABOVE
the current mark $213.53 \u2014 the broker filled the sentinel_a stop
instantly.

Two fixes this release:

A. broker/positions.py:_v5104_maybe_fire_entry_2 resets ``trail_state``
   to a fresh dataclass when Entry-2 fills, so the trail re-arms naturally
   against the new averaged entry price.

B. engine/alarm_f_trail.py:propose_stop accepts an optional ``last_close``
   and refuses to propose a stop sitting on the wrong side of the mark
   (LONG: stop \u2265 mark - pad; SHORT: stop \u2264 mark + pad). This is
   a defensive belt for any future stale-trail bug, not just Entry-2.
"""
from __future__ import annotations

from engine.alarm_f_trail import (
    BE_PAD_FLOOR,
    BE_PAD_PCT,
    STAGE_BREAKEVEN,
    STAGE_INACTIVE,
    TrailState,
    propose_stop,
)


# ---------------------------------------------------------------------------
# Fix B: propose_stop safety floor
# ---------------------------------------------------------------------------


def test_long_safety_floor_blocks_stop_above_mark():
    # NVDA replay: entry 213.66, peak 213.66, mark 213.53.
    # pad = max(0.01, 0.0005*213.66) = 0.107 -> BE candidate = 213.767.
    # mark - pad = 213.423; proposed >= 213.423 -> BLOCK.
    state = TrailState(stage=STAGE_BREAKEVEN, peak_close=213.66, bars_seen=10)
    proposed = propose_stop(
        state=state,
        side="LONG",
        entry_price=213.66,
        atr_value=None,
        current_stop_price=212.59,
        last_close=213.53,
    )
    assert proposed is None


def test_long_safety_floor_allows_stop_well_below_mark():
    # Mark $214.50 well above stop $213.77 -> proposes normally.
    state = TrailState(stage=STAGE_BREAKEVEN, peak_close=214.50, bars_seen=10)
    proposed = propose_stop(
        state=state,
        side="LONG",
        entry_price=213.66,
        atr_value=None,
        current_stop_price=212.59,
        last_close=214.50,
    )
    assert proposed is not None
    assert abs(proposed - 213.767) < 0.01


def test_short_safety_floor_blocks_stop_below_mark():
    # SHORT entry 100, pad = max(0.01, 0.05) = 0.05; BE = 99.95.
    # Mark 100.10 is ABOVE the BE; proposed <= mark + pad -> BLOCK.
    state = TrailState(stage=STAGE_BREAKEVEN, peak_close=100.0, bars_seen=10)
    proposed = propose_stop(
        state=state,
        side="SHORT",
        entry_price=100.0,
        atr_value=None,
        current_stop_price=101.0,
        last_close=100.10,
    )
    assert proposed is None


def test_short_safety_floor_allows_stop_well_above_mark():
    state = TrailState(stage=STAGE_BREAKEVEN, peak_close=99.5, bars_seen=10)
    proposed = propose_stop(
        state=state,
        side="SHORT",
        entry_price=100.0,
        atr_value=None,
        current_stop_price=101.0,
        last_close=99.5,
    )
    assert proposed is not None
    assert abs(proposed - 99.95) < 0.01


def test_legacy_callers_without_last_close_still_propose():
    # Backwards compat: omitting last_close skips the new floor. Existing
    # call sites in tests and any third-party code must keep working.
    state = TrailState(stage=STAGE_BREAKEVEN, peak_close=105.0, bars_seen=10)
    proposed = propose_stop(
        state=state,
        side="LONG",
        entry_price=100.0,
        atr_value=None,
        current_stop_price=99.0,
    )
    assert proposed == 100.05  # entry 100 + pad 0.05


# ---------------------------------------------------------------------------
# Fix A: TrailState.fresh() invariants relied on by Entry-2 reset
# ---------------------------------------------------------------------------


def test_fresh_trailstate_is_fully_neutral():
    s = TrailState.fresh()
    assert s.stage == STAGE_INACTIVE
    assert s.peak_close is None
    assert s.bars_seen == 0
    assert s.last_proposed_stop is None
    assert s.stage2_arm_favorable is None
    assert s.stage2_arm_atr is None
    assert s.last_atr is None
    assert s.last_mult == 0.0


def test_fresh_state_proposes_no_stop_at_stage_inactive():
    # After Entry-2 reset, the trail re-arms naturally; until BE-arm fires
    # again, propose_stop must return None even with adverse mark.
    fresh = TrailState.fresh()
    proposed = propose_stop(
        state=fresh,
        side="LONG",
        entry_price=213.66,
        atr_value=None,
        current_stop_price=212.59,
        last_close=213.53,
    )
    assert proposed is None


# ---------------------------------------------------------------------------
# Pad scaling sanity (cross-check with v7.2.4 semantics)
# ---------------------------------------------------------------------------


def test_pad_scales_with_entry_price():
    # Cheap stock: floor wins.
    assert max(BE_PAD_FLOOR, BE_PAD_PCT * 5.0) == 0.01
    # Expensive stock: 5bp wins.
    pad_213 = max(BE_PAD_FLOOR, BE_PAD_PCT * 213.66)
    assert abs(pad_213 - 0.10683) < 1e-4
    pad_1500 = max(BE_PAD_FLOOR, BE_PAD_PCT * 1500.0)
    assert pad_1500 == 0.75
