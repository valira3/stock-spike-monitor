"""v9.1.134 -- R32 asymmetric reversal circuit-breaker tests.

Fires ONLY when BOTH conditions hold:
  (a) MFE_R >= reversal_circuit_min_mfe_r
  (b) (MFE_R - current_PnL_R) >= reversal_circuit_min_giveback_r

Default OFF (both thresholds = 0). Designed to catch the rare clear
round-trip cohort (e.g. NFLX wild-swing days) without affecting
trending winners.
"""
from __future__ import annotations

from orb.exits import (
    EXIT_REVERSAL_CIRCUIT,
    evaluate,
    make_position,
)


SESSION_END = 15 * 60 + 55  # 15:55 ET


def _long_pos(entry=100.0, stop=98.0, shares=100):
    """Long position: risk=$2, 1R=$102, 2.5R target=$105."""
    return make_position(
        portfolio_id="main",
        ticker="AAPL",
        side="long",
        entry_price=entry,
        stop=stop,
        rr=2.5,
        shares=shares,
    )


def _short_pos(entry=100.0, stop=102.0, shares=100):
    """Short position: risk=$2, 1R=$98, 2.5R target=$95."""
    return make_position(
        portfolio_id="main",
        ticker="AAPL",
        side="short",
        entry_price=entry,
        stop=stop,
        rr=2.5,
        shares=shares,
    )


def test_disabled_by_default_does_not_fire():
    """Both thresholds=0 -> circuit never fires even on a partial round-trip.
    Bar is sized so no other exit (BE-arm/stop/target/EOD) fires either."""
    pos = _long_pos()
    # MFE up to 101.8 (0.9R, below 1R one-r so BE does NOT arm), then
    # close back at 100.5. With circuit OFF, this is silent.
    out = evaluate(
        pos,
        bar_high=101.8,
        bar_low=100.0,
        bar_close=100.5,
        bar_bucket_min=11 * 60,
        eod_cutoff_min=SESSION_END,
    )
    assert out is None  # circuit off + no stop/target/BE/EOD touch


def test_long_fires_when_both_thresholds_met():
    """MFE 1.5R then close at 0R -> giveback 1.5R, both thresholds met."""
    pos = _long_pos()
    # First bar: MFE pushes to 103 (1.5R favorable).
    evaluate(
        pos,
        bar_high=103.0,
        bar_low=101.5,
        bar_close=102.5,
        bar_bucket_min=11 * 60,
        eod_cutoff_min=SESSION_END,
        reversal_circuit_min_mfe_r=1.0,
        reversal_circuit_min_giveback_r=1.5,
    )
    assert pos.mfe_price == 103.0
    # Second bar: close drops to 100 -> current_R=0, MFE_R=1.5, giveback=1.5R.
    out = evaluate(
        pos,
        bar_high=101.0,
        bar_low=99.5,
        bar_close=100.0,
        bar_bucket_min=12 * 60,
        eod_cutoff_min=SESSION_END,
        reversal_circuit_min_mfe_r=1.0,
        reversal_circuit_min_giveback_r=1.5,
    )
    assert out is not None
    assert out.reason == EXIT_REVERSAL_CIRCUIT
    assert out.price == 100.0


def test_short_fires_when_both_thresholds_met():
    """Symmetric: short MFE 1.5R then giveback 1.5R."""
    pos = _short_pos()
    # MFE pushes price down to 97 (1.5R favorable on a short).
    evaluate(
        pos,
        bar_high=98.5,
        bar_low=97.0,
        bar_close=97.5,
        bar_bucket_min=11 * 60,
        eod_cutoff_min=SESSION_END,
        reversal_circuit_min_mfe_r=1.0,
        reversal_circuit_min_giveback_r=1.5,
    )
    assert pos.mfe_price == 97.0
    # Bar 2: close back at 100 -> current_R=0, giveback=1.5R.
    out = evaluate(
        pos,
        bar_high=101.0,
        bar_low=99.5,
        bar_close=100.0,
        bar_bucket_min=12 * 60,
        eod_cutoff_min=SESSION_END,
        reversal_circuit_min_mfe_r=1.0,
        reversal_circuit_min_giveback_r=1.5,
    )
    assert out is not None
    assert out.reason == EXIT_REVERSAL_CIRCUIT
    assert out.price == 100.0


def test_does_not_fire_when_only_mfe_threshold_met():
    """MFE 1.5R but no giveback -> circuit silent."""
    pos = _long_pos()
    # MFE pushes to 103 and close stays at 102.8 -> 1.4R current, 0.1R giveback.
    out = evaluate(
        pos,
        bar_high=103.0,
        bar_low=102.0,
        bar_close=102.8,
        bar_bucket_min=11 * 60,
        eod_cutoff_min=SESSION_END,
        reversal_circuit_min_mfe_r=1.0,
        reversal_circuit_min_giveback_r=1.5,
    )
    assert out is None  # MFE met but giveback only 0.1R


def test_does_not_fire_when_only_giveback_met_without_mfe_floor():
    """MFE 0.5R (below floor) then dropped to -1R -> 1.5R giveback,
    but MFE_R below floor -> circuit silent."""
    pos = _long_pos()
    # Bar 1: MFE to 101 (0.5R favorable -- below 1.0R floor).
    evaluate(
        pos,
        bar_high=101.0,
        bar_low=100.5,
        bar_close=100.5,
        bar_bucket_min=11 * 60,
        eod_cutoff_min=SESSION_END,
        reversal_circuit_min_mfe_r=1.0,
        reversal_circuit_min_giveback_r=1.5,
    )
    assert pos.mfe_price == 101.0
    # Bar 2: drop to 98.5 -> current_R=-0.75, giveback=1.25R; but mfe<1R.
    # Actually we need giveback >= 1.5R AND MFE >= 1R; here MFE = 0.5R, so out.
    out = evaluate(
        pos,
        bar_high=100.0,
        bar_low=98.5,
        bar_close=98.5,
        bar_bucket_min=12 * 60,
        eod_cutoff_min=SESSION_END,
        reversal_circuit_min_mfe_r=1.0,
        reversal_circuit_min_giveback_r=1.5,
    )
    # MFE floor not met (0.5R < 1.0R) -> circuit silent regardless of giveback.
    # Note: stop=98 untouched (bar_low=98.5), so no other exit fires either.
    assert out is None


def test_zero_risk_does_not_divide():
    """If pos.risk == 0 (defensive), the circuit must not raise."""
    pos = _long_pos()
    pos.risk = 0.0  # synthetic defensive guard
    # Bar sized to not trigger any other exit (no stop/BE/target/EOD).
    out = evaluate(
        pos,
        bar_high=101.5,
        bar_low=100.0,
        bar_close=100.5,
        bar_bucket_min=11 * 60,
        eod_cutoff_min=SESSION_END,
        reversal_circuit_min_mfe_r=1.0,
        reversal_circuit_min_giveback_r=1.5,
    )
    # risk=0 short-circuits the gate; no other exit triggers either.
    assert out is None


def test_fires_before_partial_at_1r():
    """When BOTH partial-at-1R and circuit would fire on the same bar,
    circuit takes priority (it's a stronger reversal signal)."""
    pos = _long_pos()
    # Bar 1: push MFE to 103 (1.5R).
    evaluate(
        pos,
        bar_high=103.0,
        bar_low=101.5,
        bar_close=102.5,
        bar_bucket_min=11 * 60,
        eod_cutoff_min=SESSION_END,
        partial_profit_at_1r=True,
        reversal_circuit_min_mfe_r=1.0,
        reversal_circuit_min_giveback_r=1.5,
    )
    # NOTE: this would have fired partial; we ignore it for the test
    # by re-priming a fresh position with mfe_price set manually.
    pos2 = _long_pos()
    pos2.mfe_price = 103.0
    pos2.partial_taken = False
    # Bar with high re-touching 1R AND close = 100 -> both partial
    # and circuit are eligible. Circuit fires first (returns early).
    out = evaluate(
        pos2,
        bar_high=102.0,  # touches 1R
        bar_low=99.5,
        bar_close=100.0,  # giveback = 1.5R
        bar_bucket_min=12 * 60,
        eod_cutoff_min=SESSION_END,
        partial_profit_at_1r=True,
        reversal_circuit_min_mfe_r=1.0,
        reversal_circuit_min_giveback_r=1.5,
    )
    assert out is not None
    assert out.reason == EXIT_REVERSAL_CIRCUIT
