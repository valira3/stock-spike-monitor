"""v8.1.8 -- wash-sale risk tracker tests.

The tracker is operator-facing signaling (NOT tax-grade): when a
losing position closes and a new position opens on the same
(ticker, side) within 30 days, the engine logs [V81-WASH-RISK]
and increments a session-scoped counter. Entry is NEVER blocked.

Tests:
  - Losing close records (ticker, side, ts, pnl) into _recent_losses
  - Winning close does NOT record
  - Entry on same (ticker, side) within 30d -> counter increments
  - Entry on same ticker but OPPOSITE side -> counter unchanged
    (long vs short are not "substantially identical" per IRS)
  - Entry on DIFFERENT ticker -> counter unchanged
  - Entry after a >30d loss -> counter unchanged + buffer pruned
  - Session reset clears counter but PRESERVES the loss buffer
    (next-day re-entry on same (ticker, side) still counts)
  - Snapshot exposes wash_risk_count
"""
import time as _time

import pytest

from orb import exits as _exits
from orb.engine import OrbConfig, OrbEngine, BreakoutSignal


def _eng() -> OrbEngine:
    cfg = OrbConfig(
        rr=2.5, stop_buffer_bps=5.0,
        max_concurrent_risk_dollars=2000.0,
        max_concurrent_notional_mult=2.0,
        ticker_side_blocklist={},
        # v9.0.0 -- this test verifies wash-risk semantics, not chase
        # filters; disable the v9 filters so try_enter reaches the
        # wash-risk bookkeeping unconditionally.
        min_break_bps=0.0,
        max_vwap_dev_bps=0.0,
        skip_prior_spy_ret_lt_bps=0.0,
    )
    eng = OrbEngine(cfg, portfolio_ids=["main"])
    eng.start_new_session(
        date_iso="2026-01-15",
        tickers=["AAPL", "MSFT"],
        vix_close_d1=18.0,
        ticker_open_today={"AAPL": 100.0, "MSFT": 200.0},
        ticker_prev_close={"AAPL": 100.0, "MSFT": 200.0},
        equity_per_portfolio={"main": 100_000.0},
    )
    return eng


def _make_pos_and_admit(eng, *, ticker="AAPL", side="long",
                        entry=100.0, stop=99.0, shares=100):
    rb = eng._risk.get("main")
    risk_per_share = abs(entry - stop)
    risk_dollars = risk_per_share * shares
    ticket = rb.try_admit(risk_dollars=risk_dollars,
                          notional=entry * shares)
    assert ticket is not None
    pos = _exits.make_position(
        portfolio_id="main", ticker=ticker, side=side,
        entry_price=entry, stop=stop, rr=2.5, shares=shares,
        risk_ticket_id=ticket.ticket_id,
    )
    return pos


def _force_signal(eng, *, ticker="AAPL", side="long", entry=100.0):
    """Construct a BreakoutSignal directly + force the FSM to ARMED
    so try_enter passes can_enter(). Bypass _lock_and_arm because
    its bars_seen<or_minutes//2 guard would block a synthetic test
    OR window with zero accumulated bars."""
    from orb import state as _state
    w = eng._state.get_or_window(ticker, eng.cfg.or_minutes)
    w.lock(locked_at_iso="test")
    ds = eng._state.get_day_state("main", ticker)
    # If the FSM is in PHASE_CLOSED (after a prior on_exit in the
    # same test), transition back to ARMED so can_enter() allows
    # the new entry. If it's WARMUP (fresh test), also go ARMED.
    ds.transition(_state.PHASE_ARMED)
    return BreakoutSignal(
        portfolio_id="main", ticker=ticker, side=side,
        signal_bar_close_iso="test", signal_bar_close=entry,
        or_high=entry + 0.5, or_low=entry - 0.5,
        proposed_stop=entry - 1.0 if side == "long" else entry + 1.0,
        proposed_entry=entry,
    )


class TestRecentLossesRecording:

    def test_losing_close_records(self):
        eng = _eng()
        pos = _make_pos_and_admit(eng, ticker="AAPL", side="long",
                                   entry=100.0, stop=99.0, shares=100)
        # Exit at $98 -> loss
        dec = _exits.ExitDecision(reason=_exits.EXIT_STOP, price=98.0)
        eng.on_exit(pos, dec)
        assert ("AAPL", "long") in eng._recent_losses
        rec = eng._recent_losses[("AAPL", "long")]
        assert len(rec) == 1
        # pnl = (98 - 100) * 100 = -200
        assert rec[0]["pnl_dollars"] == pytest.approx(-200.0)

    def test_winning_close_does_not_record(self):
        eng = _eng()
        pos = _make_pos_and_admit(eng, ticker="AAPL", side="long",
                                   entry=100.0, stop=99.0, shares=100)
        dec = _exits.ExitDecision(reason=_exits.EXIT_TARGET, price=102.5)
        eng.on_exit(pos, dec)
        # No record -- pnl positive
        assert ("AAPL", "long") not in eng._recent_losses or \
               len(eng._recent_losses[("AAPL", "long")]) == 0

    def test_break_even_close_does_not_record(self):
        # Within 1¢ of entry -- threshold is strictly < -0.01
        eng = _eng()
        pos = _make_pos_and_admit(eng, ticker="AAPL", side="long",
                                   entry=100.0, stop=99.0, shares=100)
        # Exit at $99.9999 -> -1c on 100 shares = -$0.01 (boundary)
        dec = _exits.ExitDecision(reason=_exits.EXIT_BE_STOP, price=100.0)
        eng.on_exit(pos, dec)
        # P&L = 0 -- not recorded
        rec = eng._recent_losses.get(("AAPL", "long"), [])
        assert len(rec) == 0


class TestWashRiskCounter:

    def test_entry_after_loss_same_ticker_side_increments(self):
        eng = _eng()
        # Record a loss on AAPL long
        pos = _make_pos_and_admit(eng, ticker="AAPL", side="long",
                                   entry=100.0, stop=99.0, shares=100)
        eng.on_exit(pos, _exits.ExitDecision(
            reason=_exits.EXIT_STOP, price=98.0))
        assert eng.wash_risk_count == 0
        # New entry on AAPL long -> wash risk +1
        sig = _force_signal(eng, ticker="AAPL", side="long", entry=101.0)
        admission = eng.try_enter(sig, equity=100_000.0)
        assert admission is not None
        assert eng.wash_risk_count == 1

    def test_entry_after_loss_opposite_side_no_increment(self):
        eng = _eng()
        # Loss on AAPL long
        pos = _make_pos_and_admit(eng, ticker="AAPL", side="long",
                                   entry=100.0, stop=99.0, shares=100)
        eng.on_exit(pos, _exits.ExitDecision(
            reason=_exits.EXIT_STOP, price=98.0))
        # New entry on AAPL SHORT -> not substantially identical
        sig = _force_signal(eng, ticker="AAPL", side="short", entry=98.5)
        eng.try_enter(sig, equity=100_000.0)
        assert eng.wash_risk_count == 0

    def test_entry_on_different_ticker_no_increment(self):
        eng = _eng()
        pos = _make_pos_and_admit(eng, ticker="AAPL", side="long",
                                   entry=100.0, stop=99.0, shares=100)
        eng.on_exit(pos, _exits.ExitDecision(
            reason=_exits.EXIT_STOP, price=98.0))
        # New entry on MSFT long
        sig = _force_signal(eng, ticker="MSFT", side="long", entry=200.0)
        eng.try_enter(sig, equity=100_000.0)
        assert eng.wash_risk_count == 0

    def test_entry_after_winning_close_no_increment(self):
        eng = _eng()
        pos = _make_pos_and_admit(eng, ticker="AAPL", side="long",
                                   entry=100.0, stop=99.0, shares=100)
        # Win
        eng.on_exit(pos, _exits.ExitDecision(
            reason=_exits.EXIT_TARGET, price=102.5))
        sig = _force_signal(eng, ticker="AAPL", side="long", entry=101.0)
        eng.try_enter(sig, equity=100_000.0)
        assert eng.wash_risk_count == 0

    def test_old_loss_pruned_no_increment(self):
        eng = _eng()
        # Manually inject a loss that's >30 days old
        eng._recent_losses[("AAPL", "long")] = [{
            "ts_unix": _time.time() - 31 * 24 * 3600,
            "pnl_dollars": -200.0,
            "exit_iso": "2025-12-10T15:00:00Z",
        }]
        # New entry -> prune + no increment (loss is outside window)
        sig = _force_signal(eng, ticker="AAPL", side="long", entry=101.0)
        eng.try_enter(sig, equity=100_000.0)
        assert eng.wash_risk_count == 0
        # Buffer was pruned
        assert eng._recent_losses[("AAPL", "long")] == []


class TestSessionResetSemantics:

    def test_session_reset_clears_counter(self):
        eng = _eng()
        # Manually bump counter
        eng.wash_risk_count = 5
        eng.start_new_session(
            date_iso="2026-01-16",
            tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100_000.0},
        )
        assert eng.wash_risk_count == 0

    def test_session_reset_preserves_loss_buffer(self):
        """Critical: a losing close on Monday + new entry Tuesday is
        still a wash sale. The 30-day window is CROSS-DAY."""
        eng = _eng()
        # Inject a loss timestamp from "yesterday"
        eng._recent_losses[("AAPL", "long")] = [{
            "ts_unix": _time.time() - 24 * 3600,
            "pnl_dollars": -100.0,
            "exit_iso": "2026-01-14T15:00:00Z",
        }]
        # New session
        eng.start_new_session(
            date_iso="2026-01-15",
            tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100_000.0},
        )
        # Buffer survives session reset
        assert len(eng._recent_losses[("AAPL", "long")]) == 1
        # And the next entry triggers the counter
        sig = _force_signal(eng, ticker="AAPL", side="long", entry=101.0)
        eng.try_enter(sig, equity=100_000.0)
        assert eng.wash_risk_count == 1


class TestSnapshotExposes:

    def test_snapshot_includes_wash_risk_count(self):
        eng = _eng()
        snap = eng.snapshot()
        assert "wash_risk_count" in snap
        assert snap["wash_risk_count"] == 0
        eng.wash_risk_count = 3
        snap = eng.snapshot()
        assert snap["wash_risk_count"] == 3
