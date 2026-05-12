"""v8.3.6 -- executor -> OrbEngine FSM + RiskBook mirror tests.

Operator surfaced post-redeploy: TRADES TODAY = 2 (correct, from
trade_log.jsonl) but "top ticker 0/5" and CONCURRENT RISK = $0. Root
cause: v8.2.0 mirrors executor.positions to book.positions, but the
v10 OrbEngine FSM + RiskBook are separate state and weren't touched
by the boot reconciliation. v8.3.6 adds _mirror_position_into_engine
to close that gap.

These tests cover the engine-side mirror in isolation: given a
populated executor.positions, _mirror_position_into_engine should:
  - Set day_states[(pid, ticker)].in_position = True
  - Transition phase to PHASE_IN_POS (when not currently blocked)
  - Insert a synthetic _Ticket into RiskBook._open_tickets with the
    right risk_dollars + notional
  - Be idempotent on re-call (same ticket_id, no double-counting)
"""
from __future__ import annotations

import sys
from types import ModuleType

import pytest


# Stub the telegram modules used by executors.base, exactly like
# test_executor_book_mirror.py does.
if "telegram" not in sys.modules:
    _tel = ModuleType("telegram")
    for _name in ("BotCommand", "BotCommandScopeAllPrivateChats", "Update"):
        setattr(_tel, _name, type(_name, (), {}))
    sys.modules["telegram"] = _tel
    _tel_ext = ModuleType("telegram.ext")
    for _name in ("Application", "ApplicationHandlerStop", "CommandHandler",
                  "TypeHandler"):
        setattr(_tel_ext, _name, type(_name, (), {}))
    sys.modules["telegram.ext"] = _tel_ext

from executors.base import TradeGeniusBase
import orb.live_runtime as _orb_runtime
from orb import state as _orb_state


class _FakeExec(TradeGeniusBase):
    NAME = "Val"
    mode = "paper"
    DEFAULT_OWNERS = set()

    def __init__(self):
        self.client = None
        self.positions = {}
        self.telegram_token = ""
        self._owner_chats = {}
        self._last_action_ts = {}
        self._persisted_positions = {}

    def _persist_position(self, ticker):
        pass


@pytest.fixture(autouse=True)
def fresh_runtime():
    """Reset the live_runtime singleton + bootstrap a clean engine
    with main+val+gene portfolios. Clear the PortfolioBook side too."""
    _orb_runtime._reset_for_testing()
    _orb_runtime.bootstrap()
    from engine.portfolio_book import PORTFOLIOS
    for pid in ("main", "val", "gene"):
        book = PORTFOLIOS.get(pid)
        if book is not None:
            book.positions.clear()
            book.short_positions.clear()
    _orb_runtime.ensure_session_started(
        date_iso="2026-05-12",
        tickers=["AAPL", "NVDA"], vix_close_d1=18.0,
        ticker_open_today={"AAPL": 150.0, "NVDA": 200.0},
        ticker_prev_close={"AAPL": 149.0, "NVDA": 199.0},
        equity_per_portfolio={"main": 100000.0, "val": 50000.0,
                              "gene": 25000.0},
    )
    yield
    _orb_runtime._reset_for_testing()


class TestMirrorPositionIntoEngine:

    def test_long_position_marks_in_position(self):
        ex = _FakeExec()
        ex.positions["AAPL"] = {
            "ticker": "AAPL", "side": "LONG", "qty": 100,
            "entry_price": 150.25, "stop": 149.0,
            "entry_ts_utc": "2026-05-12T14:00:00Z",
            "source": "RECONCILE",
        }
        ex._mirror_position_into_engine("AAPL")
        engine = _orb_runtime.get_engine()
        ds = engine._state.get_day_state("val", "AAPL")
        assert ds.in_position is True
        assert ds.phase == _orb_state.PHASE_IN_POS
        assert ds.last_entry_iso == "2026-05-12T14:00:00Z"

    def test_short_position_marks_in_position(self):
        ex = _FakeExec()
        ex.positions["NVDA"] = {
            "ticker": "NVDA", "side": "SHORT", "qty": 50,
            "entry_price": 200.0, "stop": 202.0,
            "entry_ts_utc": "2026-05-12T14:00:00Z",
            "source": "RECONCILE",
        }
        ex._mirror_position_into_engine("NVDA")
        engine = _orb_runtime.get_engine()
        ds = engine._state.get_day_state("val", "NVDA")
        assert ds.in_position is True
        assert ds.phase == _orb_state.PHASE_IN_POS

    def test_risk_book_ticket_inserted_with_stop_distance(self):
        ex = _FakeExec()
        ex.positions["AAPL"] = {
            "ticker": "AAPL", "side": "LONG", "qty": 100,
            "entry_price": 150.0, "stop": 148.5,
        }
        ex._mirror_position_into_engine("AAPL")
        engine = _orb_runtime.get_engine()
        rb = engine._risk.get("val")
        # 1 open ticket
        assert rb.open_count == 1
        # risk_dollars = |entry - stop| * shares = 1.5 * 100 = 150
        assert rb.open_risk == pytest.approx(150.0)
        # notional = entry * shares = 150 * 100 = 15000
        assert rb.open_notional == pytest.approx(15000.0)

    def test_risk_book_falls_back_to_risk_per_trade_pct_when_no_stop(self):
        """When stop is None (some boot paths don't carry it), the
        risk_dollars approximation falls back to risk_per_trade_pct
        of equity (1% by default)."""
        ex = _FakeExec()
        ex.positions["AAPL"] = {
            "ticker": "AAPL", "side": "LONG", "qty": 100,
            "entry_price": 150.0, "stop": None,
        }
        ex._mirror_position_into_engine("AAPL")
        engine = _orb_runtime.get_engine()
        rb = engine._risk.get("val")
        # equity for val is 50000, risk_per_trade_pct default 1% -> $500
        assert rb.open_risk == pytest.approx(500.0)
        # notional still correct
        assert rb.open_notional == pytest.approx(15000.0)

    def test_idempotent_on_re_call(self):
        """A second call with the same (pid, ticker) MUST NOT double-
        insert into _open_tickets."""
        ex = _FakeExec()
        ex.positions["AAPL"] = {
            "ticker": "AAPL", "side": "LONG", "qty": 100,
            "entry_price": 150.0, "stop": 148.5,
        }
        ex._mirror_position_into_engine("AAPL")
        ex._mirror_position_into_engine("AAPL")  # again
        ex._mirror_position_into_engine("AAPL")  # again
        engine = _orb_runtime.get_engine()
        rb = engine._risk.get("val")
        assert rb.open_count == 1
        assert rb.open_risk == pytest.approx(150.0)

    def test_doesnt_clobber_blocked_phase(self):
        """If the DayState is in a BLOCKED_* phase (e.g. blocklist),
        the mirror sets in_position=True but does NOT transition the
        phase. Defensive against weird-config recoveries."""
        engine = _orb_runtime.get_engine()
        ds = engine._state.get_day_state("val", "AAPL")
        ds.transition(_orb_state.PHASE_BLOCKED_BLOCKLIST,
                      reason="test setup")
        ex = _FakeExec()
        ex.positions["AAPL"] = {
            "ticker": "AAPL", "side": "LONG", "qty": 100,
            "entry_price": 150.0, "stop": 148.5,
        }
        ex._mirror_position_into_engine("AAPL")
        # in_position flips True; phase stays BLOCKED_BLOCKLIST
        ds_after = engine._state.get_day_state("val", "AAPL")
        assert ds_after.in_position is True
        assert ds_after.phase == _orb_state.PHASE_BLOCKED_BLOCKLIST

    def test_unknown_pid_no_op(self):
        """If self.NAME.lower() isn't a registered portfolio (e.g.
        a future fourth executor that's not yet wired into the
        engine's portfolio_ids), the mirror silently no-ops."""
        ex = _FakeExec()
        ex.NAME = "UnknownPid"
        ex.positions["AAPL"] = {
            "ticker": "AAPL", "side": "LONG", "qty": 100,
            "entry_price": 150.0, "stop": 148.5,
        }
        # Must not raise
        ex._mirror_position_into_engine("AAPL")

    def test_no_pos_no_op(self):
        ex = _FakeExec()
        # AAPL not in self.positions
        ex._mirror_position_into_engine("AAPL")
        engine = _orb_runtime.get_engine()
        rb = engine._risk.get("val")
        assert rb.open_count == 0


class TestUnmirrorPositionFromEngine:
    """v8.3.12 -- the inverse of TestMirrorPositionIntoEngine. When a
    position closes (_remove_position fires), the engine FSM should
    flip out of IN_POS and the synthetic RiskBook ticket should
    release its risk + notional reservations."""

    def _setup_mirrored(self):
        """Mirror a position in, return executor + engine."""
        ex = _FakeExec()
        ex.positions["AAPL"] = {
            "ticker": "AAPL", "side": "LONG", "qty": 100,
            "entry_price": 150.0, "stop": 148.5,
            "entry_ts_utc": "2026-05-12T14:00:00Z",
            "source": "RECONCILE",
        }
        ex._mirror_position_into_engine("AAPL")
        engine = _orb_runtime.get_engine()
        return ex, engine

    def test_unmirror_flips_in_position_off(self):
        ex, engine = self._setup_mirrored()
        ds = engine._state.get_day_state("val", "AAPL")
        assert ds.in_position is True
        assert ds.phase == _orb_state.PHASE_IN_POS
        ex._unmirror_position_from_engine("AAPL")
        ds_after = engine._state.get_day_state("val", "AAPL")
        assert ds_after.in_position is False
        assert ds_after.phase == _orb_state.PHASE_CLOSED

    def test_unmirror_releases_risk_book_ticket(self):
        ex, engine = self._setup_mirrored()
        rb = engine._risk.get("val")
        assert rb.open_count == 1
        assert rb.open_risk == pytest.approx(150.0)
        assert rb.open_notional == pytest.approx(15000.0)
        ex._unmirror_position_from_engine("AAPL")
        assert rb.open_count == 0
        assert rb.open_risk == pytest.approx(0.0)
        assert rb.open_notional == pytest.approx(0.0)

    def test_remove_position_fires_unmirror_end_to_end(self):
        """The real close path: _remove_position() should call
        _unmirror_position_from_engine() so engine state matches
        executor reality."""
        ex, engine = self._setup_mirrored()
        # Sanity
        ds_before = engine._state.get_day_state("val", "AAPL")
        rb = engine._risk.get("val")
        assert ds_before.in_position is True
        assert rb.open_count == 1
        # Close
        ex._remove_position("AAPL")
        # Executor side empty
        assert "AAPL" not in ex.positions
        # Engine FSM flipped + RiskBook released
        ds_after = engine._state.get_day_state("val", "AAPL")
        assert ds_after.in_position is False
        assert ds_after.phase == _orb_state.PHASE_CLOSED
        assert rb.open_count == 0

    def test_unmirror_idempotent_no_position_open(self):
        """Calling unmirror when nothing was mirrored is safe."""
        ex = _FakeExec()
        # AAPL never mirrored; FSM row doesn't exist (or is at WARMUP)
        ex._unmirror_position_from_engine("AAPL")
        engine = _orb_runtime.get_engine()
        rb = engine._risk.get("val")
        assert rb.open_count == 0

    def test_unmirror_doesnt_clobber_blocked_phase(self):
        """If FSM was BLOCKED_*, the unmirror sets in_position=False
        but does NOT transition the phase (it's only allowed to
        flip the IN_POS phase)."""
        ex = _FakeExec()
        engine = _orb_runtime.get_engine()
        ds = engine._state.get_day_state("val", "AAPL")
        ds.transition(_orb_state.PHASE_BLOCKED_BLOCKLIST,
                      reason="test setup")
        # Pretend in_position got set (e.g. legacy bug)
        ds.in_position = True
        ex._unmirror_position_from_engine("AAPL")
        ds_after = engine._state.get_day_state("val", "AAPL")
        assert ds_after.in_position is False
        # Phase unchanged
        assert ds_after.phase == _orb_state.PHASE_BLOCKED_BLOCKLIST

    def test_double_unmirror_is_safe(self):
        """Calling unmirror twice doesn't double-decrement RiskBook
        risk / notional (the dict pop in _open_tickets guards this)."""
        ex, engine = self._setup_mirrored()
        ex._unmirror_position_from_engine("AAPL")
        ex._unmirror_position_from_engine("AAPL")
        rb = engine._risk.get("val")
        assert rb.open_count == 0
        assert rb.open_risk == pytest.approx(0.0)


class TestLoadPersistedPositionsCallsEngineMirror:
    """v8.3.6 -- the state.db rehydrate path (boot, after Railway
    redeploy) should mirror each loaded row into BOTH the
    PortfolioBook AND the OrbEngine FSM + RiskBook."""

    def test_load_persisted_mirrors_into_engine(self, monkeypatch):
        import persistence as _p
        fake_rows = {
            "AAPL": {
                "ticker": "AAPL", "side": "LONG", "qty": 75,
                "entry_price": 150.0, "stop": 148.0,
                "source": "RECONCILE",
            },
        }
        monkeypatch.setattr(_p, "load_executor_positions",
                            lambda name, mode: fake_rows)
        ex = _FakeExec()
        ex._load_persisted_positions()
        # Executor side populated
        assert "AAPL" in ex.positions
        # Engine FSM + RiskBook populated
        engine = _orb_runtime.get_engine()
        ds = engine._state.get_day_state("val", "AAPL")
        assert ds.in_position is True
        assert ds.phase == _orb_state.PHASE_IN_POS
        rb = engine._risk.get("val")
        assert rb.open_count == 1
        # risk_dollars = |150 - 148| * 75 = 150
        assert rb.open_risk == pytest.approx(150.0)
