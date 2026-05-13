"""v8.3.34 -- day-end-giveback defenses on the live RiskBook.

Two rules ported from the R6 sweep (tools/orb_backtest.py) to
orb.risk_book:

  Rule #1: ``loss_lock_threshold_usd`` -- after a closed leg with
    pnl below -threshold, lock that (ticker, side) pair for the
    rest of the day. Future ``try_admit`` calls for that pair are
    rejected with reason ``pair_locked``.

  Rule #2: ``peak_dd_halt_usd`` -- when intraday realized PnL drops
    this many $ below today's running peak, halt all new entries.
    Mirrors the existing ``daily_kill_triggered`` rejection mechanism.

Both default 0 = off (no behavior change for existing callers).
Both daily-scoped (cleared in ``reset_session``).
"""
from __future__ import annotations

import pytest

from orb.risk_book import RiskBook


def _make_book(**kwargs) -> RiskBook:
    defaults = dict(
        portfolio_id="main",
        equity=100_000.0,
        max_concurrent_risk_dollars=2000.0,
        max_concurrent_notional_mult=2.0,
        daily_loss_kill_pct=2.0,
    )
    defaults.update(kwargs)
    return RiskBook(**defaults)


class TestDefaultsOff:
    """When both env-derived knobs are 0, behavior must be identical
    to pre-v8.3.34. Existing tests cover this implicitly; these
    re-state it for documentation."""

    def test_loss_lock_off_by_default(self):
        rb = _make_book()
        # A 1000-loss leg shouldn't lock anything.
        rb.record_realized_pnl(-1000.0, ticker="AMZN", side="short")
        # Next admit for AMZN short succeeds.
        ticket = rb.try_admit(
            risk_dollars=100.0, notional=10_000.0,
            ticker="AMZN", side="short",
        )
        assert ticket is not None

    def test_peak_dd_off_by_default(self):
        rb = _make_book()
        rb.record_realized_pnl(+500.0)   # peak = 500
        rb.record_realized_pnl(-1500.0)  # cum = -1000, dd from peak = 1500
        # No halt -- both knobs default 0.
        ticket = rb.try_admit(
            risk_dollars=100.0, notional=10_000.0,
            ticker="META", side="long",
        )
        assert ticket is not None


class TestLossLockRule1:

    def test_lock_after_loss_above_threshold(self):
        rb = _make_book(loss_lock_threshold_usd=150.0)
        rb.record_realized_pnl(-200.0, ticker="AMZN", side="short")
        ticket = rb.try_admit(
            risk_dollars=100.0, notional=10_000.0,
            ticker="AMZN", side="short",
        )
        assert ticket is None
        assert "pair_locked" in rb.last_reject_reason
        assert "AMZN" in rb.last_reject_reason
        assert "short" in rb.last_reject_reason

    def test_no_lock_when_loss_below_threshold(self):
        rb = _make_book(loss_lock_threshold_usd=150.0)
        rb.record_realized_pnl(-100.0, ticker="AMZN", side="short")
        ticket = rb.try_admit(
            risk_dollars=100.0, notional=10_000.0,
            ticker="AMZN", side="short",
        )
        assert ticket is not None, "small loss should NOT lock the pair"

    def test_lock_does_not_block_other_pairs(self):
        rb = _make_book(loss_lock_threshold_usd=150.0)
        rb.record_realized_pnl(-200.0, ticker="AMZN", side="short")
        # AMZN short is locked. AMZN LONG should still admit.
        t1 = rb.try_admit(
            risk_dollars=100.0, notional=10_000.0,
            ticker="AMZN", side="long",
        )
        assert t1 is not None
        # META short should also admit.
        rb.release(t1)
        t2 = rb.try_admit(
            risk_dollars=100.0, notional=10_000.0,
            ticker="META", side="short",
        )
        assert t2 is not None

    def test_lock_does_not_block_winners(self):
        """A winning leg (pnl > 0) must NOT lock the pair."""
        rb = _make_book(loss_lock_threshold_usd=150.0)
        rb.record_realized_pnl(+500.0, ticker="NFLX", side="long")
        ticket = rb.try_admit(
            risk_dollars=100.0, notional=10_000.0,
            ticker="NFLX", side="long",
        )
        assert ticket is not None

    def test_record_without_ticker_does_not_lock(self):
        """Legacy callers that don't pass ticker/side shouldn't trigger
        a lock (silent no-op for backwards compat)."""
        rb = _make_book(loss_lock_threshold_usd=150.0)
        rb.record_realized_pnl(-200.0)   # no ticker/side
        ticket = rb.try_admit(
            risk_dollars=100.0, notional=10_000.0,
            ticker="AMZN", side="short",
        )
        assert ticket is not None

    def test_session_reset_clears_locks(self):
        rb = _make_book(loss_lock_threshold_usd=150.0)
        rb.record_realized_pnl(-200.0, ticker="AMZN", side="short")
        # Confirm locked.
        t1 = rb.try_admit(risk_dollars=100.0, notional=10_000.0,
                          ticker="AMZN", side="short")
        assert t1 is None
        rb.reset_session()
        t2 = rb.try_admit(risk_dollars=100.0, notional=10_000.0,
                          ticker="AMZN", side="short")
        assert t2 is not None, "session reset must clear locked pairs"


class TestPeakDDHaltRule2:

    def test_halt_when_dd_at_threshold(self):
        rb = _make_book(peak_dd_halt_usd=500.0)
        rb.record_realized_pnl(+1000.0)   # peak = 1000
        rb.record_realized_pnl(-600.0)    # cum = 400, dd = 600
        ticket = rb.try_admit(
            risk_dollars=100.0, notional=10_000.0,
            ticker="AMZN", side="short",
        )
        assert ticket is None
        assert "peak_dd_halt" in rb.last_reject_reason

    def test_halt_at_exact_threshold(self):
        rb = _make_book(peak_dd_halt_usd=500.0)
        rb.record_realized_pnl(+1000.0)
        rb.record_realized_pnl(-500.0)    # dd = 500 exactly (>=)
        ticket = rb.try_admit(risk_dollars=100.0, notional=10_000.0,
                              ticker="X", side="long")
        assert ticket is None

    def test_no_halt_when_dd_below_threshold(self):
        rb = _make_book(peak_dd_halt_usd=500.0)
        rb.record_realized_pnl(+1000.0)
        rb.record_realized_pnl(-400.0)    # dd = 400 (< 500)
        ticket = rb.try_admit(risk_dollars=100.0, notional=10_000.0,
                              ticker="X", side="long")
        assert ticket is not None

    def test_session_reset_clears_peak(self):
        rb = _make_book(peak_dd_halt_usd=500.0)
        rb.record_realized_pnl(+1000.0)
        rb.record_realized_pnl(-700.0)
        # Halted now.
        t1 = rb.try_admit(risk_dollars=100.0, notional=10_000.0,
                          ticker="X", side="long")
        assert t1 is None
        rb.reset_session()
        t2 = rb.try_admit(risk_dollars=100.0, notional=10_000.0,
                          ticker="X", side="long")
        assert t2 is not None

    def test_peak_only_goes_up(self):
        """Peak ratchets monotonically; losses don't lower it."""
        rb = _make_book(peak_dd_halt_usd=500.0)
        rb.record_realized_pnl(+1000.0)   # peak = 1000
        rb.record_realized_pnl(-300.0)    # peak still 1000, cum = 700
        snap = rb.snapshot()
        assert snap["peak_pnl_today"] == 1000.0
        assert snap["current_dd_from_peak"] == 300.0
        # Next gain doesn't change the dd calc until peak grows.
        rb.record_realized_pnl(+100.0)    # cum = 800
        snap = rb.snapshot()
        assert snap["peak_pnl_today"] == 1000.0


class TestCombined:

    def test_both_rules_active(self):
        rb = _make_book(loss_lock_threshold_usd=150.0,
                        peak_dd_halt_usd=500.0)
        # First leg: AMZN -200 -- triggers lock on AMZN/short
        rb.record_realized_pnl(-200.0, ticker="AMZN", side="short")
        assert ("AMZN", "short") in rb._locked_pairs
        # cum = -200, peak = 0, dd = 200 (< 500, no halt yet)
        t1 = rb.try_admit(risk_dollars=100.0, notional=10_000.0,
                          ticker="META", side="long")
        assert t1 is not None, "dd not yet at halt threshold"
        # Second leg: META -400 -- pushes cum to -600, dd = 600
        rb.record_realized_pnl(-400.0, ticker="META", side="long")
        t2 = rb.try_admit(risk_dollars=100.0, notional=10_000.0,
                          ticker="NFLX", side="long")
        assert t2 is None, "peak-DD halt should fire"
        assert "peak_dd_halt" in rb.last_reject_reason


class TestSnapshotExposure:
    """The /api/state snapshot must expose the new state so the
    dashboard + watchdog can read the rules' status."""

    def test_snapshot_includes_v8334_fields(self):
        rb = _make_book(loss_lock_threshold_usd=150.0,
                        peak_dd_halt_usd=500.0)
        snap = rb.snapshot()
        for f in ("loss_lock_threshold_usd", "peak_dd_halt_usd",
                  "locked_pairs", "peak_pnl_today",
                  "current_dd_from_peak"):
            assert f in snap, f"snapshot missing {f}"

    def test_snapshot_locked_pairs_format(self):
        rb = _make_book(loss_lock_threshold_usd=150.0)
        rb.record_realized_pnl(-200.0, ticker="AMZN", side="short")
        rb.record_realized_pnl(-200.0, ticker="META", side="long")
        snap = rb.snapshot()
        # JSON-serializable: list of [ticker, side] pairs
        assert isinstance(snap["locked_pairs"], list)
        assert ["AMZN", "short"] in snap["locked_pairs"]
        assert ["META", "long"] in snap["locked_pairs"]
