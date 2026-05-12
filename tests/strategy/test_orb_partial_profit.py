"""v8.1.0 -- partial-profit-at-1R live engine tests.

Coverage:
  1. orb.exits.evaluate() partial-fire ordering + idempotence
  2. orb.exits.apply_partial_fill() math + idempotence + edge cases
  3. orb.risk_book.RiskBook.release_partial() math + bounds + idempotence
  4. orb.engine.OrbEngine.on_partial_exit() releases half risk-book budget
  5. orb.engine.on_exit() includes partial_pnl in daily-loss-kill accounting
  6. orb.live_adapter.LiveAdapter.check_exit() returns partial=True envelope
     correctly + position stays in open map
  7. Full lifecycle: entry -> partial -> stop on runner -> on_exit credits
     full pnl (partial + runner) to kill-gate
  8. partial_profit_at_1r=False (default) is a strict no-op vs v8.0.x
"""
import pytest

from orb import exits as _exits
from orb import risk_book as _rb
from orb.engine import OrbConfig, OrbEngine
from orb import live_runtime
from orb.live_adapter import LiveAdapter


# ----- 1. exits.evaluate() partial-fire ordering ----------------------


def _make_long(entry=100.0, stop=99.0, rr=2.5, shares=100):
    return _exits.make_position(
        portfolio_id="main", ticker="AAPL", side="long",
        entry_price=entry, stop=stop, rr=rr, shares=shares,
        risk_ticket_id="tkt-1",
    )


def _make_short(entry=100.0, stop=101.0, rr=2.5, shares=100):
    return _exits.make_position(
        portfolio_id="main", ticker="AAPL", side="short",
        entry_price=entry, stop=stop, rr=rr, shares=shares,
        risk_ticket_id="tkt-2",
    )


class TestEvaluatePartialFire:

    def test_partial_off_no_emit(self):
        # Default behavior unchanged: partial_profit_at_1r=False (default)
        pos = _make_long(entry=100.0, stop=99.0, shares=100)
        # bar.high crosses 1R (=101.0) but partial flag is off.
        # bar.low=100.5 stays above the (about-to-arm) BE stop = entry=100,
        # so no exit fires.
        dec = _exits.evaluate(
            pos, bar_high=101.5, bar_low=100.5, bar_close=101.3,
            bar_bucket_min=600, eod_cutoff_min=955,
            partial_profit_at_1r=False,
        )
        # BE arms (1R touched) but no exit (bar_low > new BE stop).
        assert dec is None
        assert pos.be_moved is True
        assert pos.partial_taken is False

    def test_long_partial_fires_at_1r(self):
        pos = _make_long(entry=100.0, stop=99.0, shares=100)
        dec = _exits.evaluate(
            pos, bar_high=101.0, bar_low=100.0, bar_close=101.0,
            bar_bucket_min=600, eod_cutoff_min=955,
            partial_profit_at_1r=True,
        )
        assert dec is not None
        assert dec.reason == _exits.EXIT_PARTIAL
        assert dec.price == 101.0  # entry + risk = 100 + 1.0

    def test_short_partial_fires_at_1r(self):
        pos = _make_short(entry=100.0, stop=101.0, shares=100)
        dec = _exits.evaluate(
            pos, bar_high=99.5, bar_low=99.0, bar_close=99.2,
            bar_bucket_min=600, eod_cutoff_min=955,
            partial_profit_at_1r=True,
        )
        assert dec is not None
        assert dec.reason == _exits.EXIT_PARTIAL
        assert dec.price == 99.0  # entry - risk = 100 - 1.0

    def test_partial_fires_before_stop_on_same_bar(self):
        # Bar pierces both 1R and the original stop. Partial fires
        # FIRST; BE arms on the same bar (matching backtest semantics
        # where partial and be-arm happen in lockstep on a 1R touch).
        # The original stop pierce is irrelevant because the
        # partial-fire short-circuits the rest of the evaluator AND
        # bumps the stop to entry; the runner's stop is now the BE
        # stop, not the original OR-edge stop.
        pos = _make_long(entry=100.0, stop=99.0, shares=100)
        dec = _exits.evaluate(
            pos, bar_high=101.1, bar_low=98.5, bar_close=99.0,
            bar_bucket_min=600, eod_cutoff_min=955,
            partial_profit_at_1r=True,
        )
        assert dec is not None
        assert dec.reason == _exits.EXIT_PARTIAL
        # BE armed on the same bar -- stop bumped to entry.
        assert pos.be_moved is True
        assert pos.stop == pos.entry_price

    def test_partial_only_fires_once(self):
        pos = _make_long(entry=100.0, stop=99.0, shares=100)
        # First call -> partial fires
        dec1 = _exits.evaluate(
            pos, bar_high=101.0, bar_low=100.0, bar_close=101.0,
            bar_bucket_min=600, eod_cutoff_min=955,
            partial_profit_at_1r=True,
        )
        assert dec1.reason == _exits.EXIT_PARTIAL
        # Caller applies the partial:
        _exits.apply_partial_fill(pos, partial_price=101.0)
        assert pos.partial_taken is True
        # Second call on a bar that ALSO touches 1R -> NO partial
        # (partial_taken=True). Normal evaluation continues.
        dec2 = _exits.evaluate(
            pos, bar_high=101.2, bar_low=100.5, bar_close=101.1,
            bar_bucket_min=600, eod_cutoff_min=955,
            partial_profit_at_1r=True,
        )
        # BE just armed -> stop is at entry (100); bar_low 100.5 > stop,
        # bar_high 101.2 < target 102.5. So None.
        assert dec2 is None

    def test_partial_skipped_when_shares_lt_2(self):
        # Tiny position (1 share) -- no partial possible.
        # Use bar_low=100.5 to stay above the BE stop that arms on
        # this bar's 1R touch.
        pos = _make_long(entry=100.0, stop=99.0, shares=1)
        dec = _exits.evaluate(
            pos, bar_high=101.0, bar_low=100.5, bar_close=101.0,
            bar_bucket_min=600, eod_cutoff_min=955,
            partial_profit_at_1r=True,
        )
        # Partial branch falls through (shares < 2); BE-arm + stop/target
        # logic runs. bar_low > BE stop -> no exit.
        assert dec is None
        assert pos.partial_taken is False
        assert pos.be_moved is True  # BE armed by the normal path


# ----- 2. apply_partial_fill math -------------------------------------


class TestApplyPartialFill:

    def test_long_books_half_pnl(self):
        pos = _make_long(entry=100.0, stop=99.0, shares=100)
        shares_closed, pnl = _exits.apply_partial_fill(pos, partial_price=101.0)
        # half = 50, pnl = (101 - 100) * 50 = 50.0
        assert shares_closed == 50
        assert pnl == 50.0
        assert pos.shares == 50  # mutated to remainder
        assert pos.partial_taken is True
        assert pos.partial_pnl_dollars == 50.0
        assert pos.original_shares == 100  # preserved

    def test_short_books_half_pnl(self):
        pos = _make_short(entry=100.0, stop=101.0, shares=100)
        shares_closed, pnl = _exits.apply_partial_fill(pos, partial_price=99.0)
        # half = 50, pnl = (100 - 99) * 50 = 50.0
        assert shares_closed == 50
        assert pnl == 50.0
        assert pos.shares == 50
        assert pos.partial_taken is True
        assert pos.partial_pnl_dollars == 50.0

    def test_odd_shares_rounds_down(self):
        pos = _make_long(entry=100.0, stop=99.0, shares=101)
        shares_closed, _ = _exits.apply_partial_fill(pos, partial_price=101.0)
        assert shares_closed == 50  # 101 // 2 = 50, runner = 51
        assert pos.shares == 51

    def test_second_call_is_noop(self):
        pos = _make_long(entry=100.0, stop=99.0, shares=100)
        _exits.apply_partial_fill(pos, partial_price=101.0)
        # Second call after partial_taken=True -> no-op
        shares_closed, pnl = _exits.apply_partial_fill(pos, partial_price=101.5)
        assert shares_closed == 0
        assert pnl == 0.0
        assert pos.shares == 50  # unchanged from first partial
        assert pos.partial_pnl_dollars == 50.0  # unchanged

    def test_too_few_shares_noop(self):
        pos = _make_long(entry=100.0, stop=99.0, shares=1)
        shares_closed, pnl = _exits.apply_partial_fill(pos, partial_price=101.0)
        assert (shares_closed, pnl) == (0, 0.0)
        assert pos.partial_taken is False


# ----- 3. risk_book.release_partial -----------------------------------


class TestReleasePartial:

    def _book(self):
        book = _rb.RiskBook(
            "main",
            max_concurrent_risk_dollars=2000.0,
            max_concurrent_notional_mult=2.0,
            equity=100_000.0,
            daily_loss_kill_pct=2.0,
        )
        # admit a ticket
        ticket = book.try_admit(risk_dollars=1000.0, notional=50000.0)
        return book, ticket

    def test_release_half_reduces_open_risk(self):
        book, ticket = self._book()
        assert book._open_risk == 1000.0
        assert book.release_partial(ticket, frac=0.5) is True
        assert book._open_risk == 500.0
        assert book._open_notional == 25000.0
        # ticket itself was mutated
        assert ticket.risk_dollars == 500.0
        assert ticket.notional == 25000.0
        # ticket is STILL in open_tickets (not popped)
        assert ticket.ticket_id in book._open_tickets

    def test_release_with_frac_quarter(self):
        book, ticket = self._book()
        assert book.release_partial(ticket, frac=0.25) is True
        assert book._open_risk == 750.0  # 1000 * 0.75
        assert ticket.risk_dollars == 750.0

    def test_release_partial_invalid_frac_rejected(self):
        book, ticket = self._book()
        for bad in (0.0, 1.0, -0.1, 1.5, 2.0):
            assert book.release_partial(ticket, frac=bad) is False
        # No mutation
        assert book._open_risk == 1000.0

    def test_release_partial_none_ticket(self):
        book, _ = self._book()
        assert book.release_partial(None, frac=0.5) is False

    def test_release_partial_then_full_release(self):
        book, ticket = self._book()
        book.release_partial(ticket, frac=0.5)
        # Full release removes the remaining budget
        assert book.release(ticket) is True
        assert book._open_risk == 0.0
        assert book._open_notional == 0.0
        assert ticket.ticket_id not in book._open_tickets

    def test_release_partial_after_full_release_noop(self):
        book, ticket = self._book()
        book.release(ticket)
        # ticket is gone from book; partial-release should return False
        assert book.release_partial(ticket, frac=0.5) is False


# ----- 4. engine.on_partial_exit --------------------------------------


def _eng(*, partial_on=True):
    cfg = OrbConfig(
        rr=2.5, stop_buffer_bps=5.0,
        max_concurrent_risk_dollars=2000.0,
        max_concurrent_notional_mult=2.0,
        partial_profit_at_1r=partial_on,
        ticker_side_blocklist={},
    )
    eng = OrbEngine(cfg, portfolio_ids=["main"])
    eng.start_new_session(
        date_iso="2026-01-15",
        tickers=["AAPL"],
        vix_close_d1=18.0,
        ticker_open_today={"AAPL": 100.0},
        ticker_prev_close={"AAPL": 100.0},
        equity_per_portfolio={"main": 100_000.0},
    )
    return eng


class TestEngineOnPartialExit:

    def test_releases_half_risk(self):
        eng = _eng()
        # Hand-build a position + admit through risk-book to populate
        # _open_tickets so on_partial_exit can find the ticket.
        rb = eng._risk.get("main")
        ticket = rb.try_admit(risk_dollars=1000.0, notional=50000.0)
        assert ticket is not None
        pos = _exits.make_position(
            portfolio_id="main", ticker="AAPL", side="long",
            entry_price=100.0, stop=99.0, rr=2.5, shares=100,
            risk_ticket_id=ticket.ticket_id,
        )
        shares_closed, pnl = eng.on_partial_exit(pos, partial_price=101.0)
        assert shares_closed == 50
        assert pnl == 50.0
        # Risk book budget halved
        assert rb._open_risk == 500.0
        assert rb._open_notional == 25000.0

    def test_idempotent_second_call_noop(self):
        eng = _eng()
        rb = eng._risk.get("main")
        ticket = rb.try_admit(risk_dollars=1000.0, notional=50000.0)
        pos = _exits.make_position(
            portfolio_id="main", ticker="AAPL", side="long",
            entry_price=100.0, stop=99.0, rr=2.5, shares=100,
            risk_ticket_id=ticket.ticket_id,
        )
        eng.on_partial_exit(pos, partial_price=101.0)
        # Second call after partial_taken=True
        shares_closed, pnl = eng.on_partial_exit(pos, partial_price=101.5)
        assert (shares_closed, pnl) == (0, 0.0)
        # Risk-book not double-released
        assert rb._open_risk == 500.0


# ----- 5. on_exit credits partial_pnl to kill-gate --------------------


class TestOnExitCreditsPartialPnl:

    def test_partial_pnl_added_to_realized(self):
        eng = _eng()
        rb = eng._risk.get("main")
        ticket = rb.try_admit(risk_dollars=1000.0, notional=50000.0)
        pos = _exits.make_position(
            portfolio_id="main", ticker="AAPL", side="long",
            entry_price=100.0, stop=99.0, rr=2.5, shares=100,
            risk_ticket_id=ticket.ticket_id,
        )
        # Partial fires
        eng.on_partial_exit(pos, partial_price=101.0)
        assert pos.partial_pnl_dollars == 50.0
        assert pos.shares == 50
        # Final exit at stop on remaining 50 (pnl = (99-100)*50 = -50)
        final_dec = _exits.ExitDecision(reason=_exits.EXIT_STOP, price=99.0)
        eng.on_exit(pos, final_dec)
        # Realized pnl for kill-gate should be: runner -50 + partial +50 = 0
        assert abs(rb.realized_pnl_today - 0.0) < 1e-6


# ----- 6. adapter.check_exit() partial envelope -----------------------


@pytest.fixture(autouse=True)
def reset_runtime():
    live_runtime._reset_for_testing()
    yield
    live_runtime._reset_for_testing()


@pytest.fixture
def isolated_env(monkeypatch):
    import os as _os
    for k in list(_os.environ):
        if k.startswith("ORB_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ORB_LIVE_MODE", "1")
    monkeypatch.setenv("ORB_PARTIAL_PROFIT_AT_1R", "1")
    yield monkeypatch


class TestAdapterPartialEnvelope:

    def test_partial_keeps_position_open(self, isolated_env):
        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-01-15",
            tickers=["AAPL"], vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100_000.0},
        )
        eng = live_runtime.get_engine()
        adapter = live_runtime.get_adapter("main")
        # Force-lock an OR window so detect_breakout can fire (we don't
        # actually use it -- we manually build a position via the
        # adapter to test the exit path).
        w = eng._state.get_or_window("AAPL", 30)
        w.lock(locked_at_iso="t")
        # Manually inject a position via the engine's risk book
        rb = eng._risk.get("main")
        ticket = rb.try_admit(risk_dollars=1000.0, notional=50000.0)
        pos = _exits.make_position(
            portfolio_id="main", ticker="AAPL", side="long",
            entry_price=100.0, stop=99.0, rr=2.5, shares=100,
            risk_ticket_id=ticket.ticket_id,
        )
        adapter._open_positions[ticket.ticket_id] = pos
        adapter._ticker_to_ticket["AAPL"] = ticket.ticket_id

        # Bar high crosses 1R -> partial fires.
        result = adapter.check_exit(
            "AAPL", ticket.ticket_id,
            bar_high=101.0, bar_low=100.0, bar_close=101.0,
            bar_bucket_min=600,
        )
        assert result.exit is False
        assert result.partial is True
        assert result.partial_shares == 50
        assert result.partial_price == 101.0
        assert result.remaining_shares == 50
        assert result.partial_pnl_dollars == 50.0
        # Position MUST still be in the open map
        assert ticket.ticket_id in adapter._open_positions
        assert adapter._open_positions[ticket.ticket_id].partial_taken is True
        assert adapter._open_positions[ticket.ticket_id].shares == 50


# ----- 7. Full lifecycle: entry -> partial -> stop on runner ---------


class TestFullLifecycle:

    def test_partial_then_be_stop_credits_full_pnl(self, isolated_env):
        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-01-15",
            tickers=["AAPL"], vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100_000.0},
        )
        eng = live_runtime.get_engine()
        adapter = live_runtime.get_adapter("main")
        rb = eng._risk.get("main")
        ticket = rb.try_admit(risk_dollars=1000.0, notional=50000.0)
        pos = _exits.make_position(
            portfolio_id="main", ticker="AAPL", side="long",
            entry_price=100.0, stop=99.0, rr=2.5, shares=100,
            risk_ticket_id=ticket.ticket_id,
        )
        adapter._open_positions[ticket.ticket_id] = pos
        adapter._ticker_to_ticket["AAPL"] = ticket.ticket_id

        # Bar 1: 1R touch -> partial fires at 101.0 (booked pnl +$50)
        r1 = adapter.check_exit(
            "AAPL", ticket.ticket_id,
            bar_high=101.0, bar_low=100.0, bar_close=101.0,
            bar_bucket_min=600,
        )
        assert r1.partial is True
        # Bar 2: BE arm + BE stop touch (pos.stop is now 100.0 after BE)
        r2 = adapter.check_exit(
            "AAPL", ticket.ticket_id,
            bar_high=100.5, bar_low=99.8, bar_close=99.9,
            bar_bucket_min=605,
        )
        assert r2.exit is True
        assert r2.reason == _exits.EXIT_BE_STOP
        # Realized pnl for kill-gate = runner (50 sh * (100 - 100) = 0)
        # + partial $50 = $50
        assert abs(rb.realized_pnl_today - 50.0) < 1e-6

    def test_partial_then_target_credits_both(self, isolated_env):
        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-01-15",
            tickers=["AAPL"], vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100_000.0},
        )
        eng = live_runtime.get_engine()
        adapter = live_runtime.get_adapter("main")
        rb = eng._risk.get("main")
        ticket = rb.try_admit(risk_dollars=1000.0, notional=50000.0)
        pos = _exits.make_position(
            portfolio_id="main", ticker="AAPL", side="long",
            entry_price=100.0, stop=99.0, rr=2.5, shares=100,
            risk_ticket_id=ticket.ticket_id,
        )
        adapter._open_positions[ticket.ticket_id] = pos
        adapter._ticker_to_ticket["AAPL"] = ticket.ticket_id

        # 1R -> partial booking $50
        adapter.check_exit(
            "AAPL", ticket.ticket_id,
            bar_high=101.0, bar_low=100.0, bar_close=101.0,
            bar_bucket_min=600,
        )
        # Target 102.5 hit on remaining 50 shares
        r = adapter.check_exit(
            "AAPL", ticket.ticket_id,
            bar_high=102.5, bar_low=101.5, bar_close=102.5,
            bar_bucket_min=605,
        )
        assert r.exit is True
        assert r.reason == _exits.EXIT_TARGET
        # Runner pnl = 50 * (102.5 - 100) = 125; partial = 50; total 175
        assert abs(rb.realized_pnl_today - 175.0) < 1e-6


# ----- 8. partial-off is a strict no-op vs v8.0.x --------------------


class TestPartialOffIsNoOp:

    def test_off_disables_partial_branch(self, monkeypatch):
        # v8.1.3 -- env-fallback default flipped to True. To test the
        # "off" branch we now must explicitly set =0 in env.
        import os as _os
        for k in list(_os.environ):
            if k.startswith("ORB_"):
                monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("ORB_LIVE_MODE", "1")
        monkeypatch.setenv("ORB_PARTIAL_PROFIT_AT_1R", "0")
        live_runtime._reset_for_testing()
        live_runtime.bootstrap()
        eng = live_runtime.get_engine()
        # Config sees partial_profit_at_1r = False because env=0
        assert eng.cfg.partial_profit_at_1r is False
        # And the snapshot exposes it
        snap = eng.snapshot()
        assert snap["config"]["partial_profit_at_1r"] is False
