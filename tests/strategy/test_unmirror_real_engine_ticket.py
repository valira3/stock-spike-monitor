"""v9.1.26 -- real-engine-ticket cleanup tests.

The 2026-05-13 audit found Val admit_count=3 vs Main admit_count=17 on
the same OR data. Root cause: when Val's broker position closed via
the bus EXIT path, `executors.base._unmirror_position_from_engine`
only released the synthetic `recover-{pid}-{ticker}` ticket. The REAL
ticket created by `engine.try_enter` (uuid-style) leaked, FSM stayed
IN_POS until phantom-sweep ran (1+ scan cycle later) and `trades_today`
was never incremented.

v9.1.26 routes the unmirror path AND `engine.clear_phantom_in_pos`
through `engine.on_exit` when a real ticket is present. This drives:
  - FSM IN_POS -> CLOSED
  - trades_today += 1
  - real ticket release
  - adapter._open_positions / _ticker_to_ticket cleared

Tests verify the new path AND the legacy path (recover-* only, no
adapter ticket) still works.
"""
from __future__ import annotations

import pytest

import orb.live_runtime as _orb_runtime
from orb.engine import OrbConfig, OrbEngine
from orb.eod_reversal import EodReversalConfig, EodReversalEngine
from orb.live_adapter import LiveAdapter


# ---------- helpers ----------


def _build_engine_with_adapter() -> tuple[OrbEngine, dict]:
    """Build a real OrbEngine + LiveAdapter per portfolio, wired into
    `_orb_runtime._adapters` so the production code path can find them.
    """
    cfg = OrbConfig()
    engine = OrbEngine(cfg, portfolio_ids=["main", "val", "gene"])
    # Mirror what live_runtime.bootstrap() does: build a LiveAdapter
    # per portfolio and stash on the module-level dict.
    adapters = {pid: LiveAdapter(engine, portfolio_id=pid)
                for pid in engine.portfolio_ids}
    _orb_runtime._engine = engine
    _orb_runtime._adapters = adapters
    return engine, adapters


def _admit_position(engine: OrbEngine, adapter: LiveAdapter,
                    *, ticker: str, side: str = "long",
                    or_high: float = 100.0, or_low: float = 99.0,
                    entry_price: float = 100.5,
                    equity: float = 100_000.0) -> tuple[str, object]:
    """Open an engine position via the real try_enter path. Returns
    (ticket_id, position) so the test can verify cleanup.
    """
    # Seed an OR window so detect_breakout returns a signal.
    from orb.state import OrWindow, PHASE_ARMED
    engine._state.or_windows[ticker] = OrWindow(
        ticker=ticker, or_high=or_high, or_low=or_low, locked=True,
    )
    pid = adapter.portfolio_id
    # Transition the day state from default WARMUP -> ARMED so
    # ds.can_enter returns True. Production wires this via
    # ensure_session_started; the test stays local.
    ds_seed = engine._state.get_day_state(pid, ticker)
    ds_seed.transition(PHASE_ARMED)
    # Drive the full check_entry path so it lays down ticker_to_ticket
    # and _open_positions just like production.
    result = adapter.check_entry(
        ticker, side=side,
        five_min_close=entry_price + 0.05 if side == "long" else entry_price - 0.05,
        next_open=entry_price,
        equity=equity,
        signal_iso="2026-05-13T14:35:00Z",   # 10:35 ET, inside hunt window
    )
    assert result.ok, f"setup failed for {pid}/{ticker}: {result.reason_no}"
    ticket_id = result.ticket_id
    pos = adapter._open_positions[ticket_id]
    return ticket_id, pos


# ---------- 1. _unmirror_position_from_engine (executor-side) ----------


class _FakeExecutor:
    """Minimal stub mirroring the parts of executors.base.Executor that
    `_unmirror_position_from_engine` touches. We import the REAL method
    from the base class to test the production code, not a copy.
    """

    NAME = "Val"

    def __init__(self):
        from executors.base import TradeGeniusBase
        # Bind the real method.
        self._method = TradeGeniusBase._unmirror_position_from_engine.__get__(self)

    def unmirror(self, ticker: str) -> None:
        self._method(ticker)


class TestUnmirrorRealTicket:
    """The v9.1.26 fix: when a real engine ticket exists, unmirror must
    route through engine.on_exit so trades_today bumps + ticket releases.
    """

    def setup_method(self):
        self.engine, self.adapters = _build_engine_with_adapter()
        self.adapter = self.adapters["val"]
        self.executor = _FakeExecutor()  # NAME = "Val"

    def teardown_method(self):
        _orb_runtime._engine = None
        _orb_runtime._adapters = None

    def test_real_ticket_release_via_on_exit(self):
        ticket_id, pos = _admit_position(
            self.engine, self.adapter, ticker="TSLA",
        )
        rb = self.engine._risk.get("val")
        # Pre-unmirror invariants.
        assert rb.admit_count == 1
        assert rb.open_count == 1
        assert rb.open_risk > 0
        ds = self.engine._state.get_day_state("val", "TSLA")
        assert ds.in_position is True
        assert ds.phase == "in_pos"
        assert ds.trades_today == 0
        # Run the production unmirror (this is what bus-EXIT triggers
        # via Val's _on_signal path).
        self.executor.unmirror("TSLA")
        # Post-unmirror -- engine.on_exit should have fired.
        assert rb.open_count == 0, "real ticket must be released"
        assert rb.open_risk == pytest.approx(0.0, abs=1e-6), (
            "open_risk must be released (within FP residue)"
        )
        # FSM clean.
        assert ds.in_position is False
        assert ds.phase == "closed"
        # trades_today bumped (the headline 2026-05-13 regression).
        assert ds.trades_today == 1, (
            "v9.1.26: trades_today must increment on unmirror so the "
            "per-day cap accounting stays correct"
        )
        # admit_count is cumulative -- not decremented by on_exit.
        assert rb.admit_count == 1
        # Adapter maps cleaned up.
        assert ticket_id not in self.adapter._open_positions
        assert "TSLA" not in self.adapter._ticker_to_ticket

    def test_unmirror_then_readmit_succeeds(self):
        """Regression for the headline P&L finding: pre-v9.1.26 a
        ticker that got bus-exited couldn't re-admit on the next bar
        because FSM was stuck IN_POS. Post-fix, re-admit works.
        """
        _admit_position(self.engine, self.adapter, ticker="TSLA")
        self.executor.unmirror("TSLA")
        # Re-admit should succeed -- FSM is back to CLOSED, ticket
        # released, capacity available.
        ticket_id2, pos2 = _admit_position(
            self.engine, self.adapter, ticker="TSLA",
            # New OR + new entry price for the second breakout.
            or_high=101.0, or_low=100.0, entry_price=101.5,
        )
        assert ticket_id2 != "", "re-admit ticket id missing"
        rb = self.engine._risk.get("val")
        assert rb.admit_count == 2
        assert rb.open_count == 1

    def test_unmirror_no_position_is_noop(self):
        """If there's no engine ticket and no FSM in_position, unmirror
        is a no-op (legacy behaviour preserved)."""
        rb = self.engine._risk.get("val")
        before_admit = rb.admit_count
        self.executor.unmirror("TSLA")
        assert rb.admit_count == before_admit
        assert rb.open_count == 0

    def test_unmirror_releases_recover_ticket_only_when_no_real_ticket(self):
        """Legacy path: a recover-* ticket is in the risk book (from
        v8.3.6 mirror on startup) but no adapter._open_positions entry
        exists. Unmirror should release the recover ticket and
        transition FSM, just like pre-v9.1.26.
        """
        rb = self.engine._risk.get("val")
        # Simulate the v8.3.6 mirror state.
        from orb.risk_book import _Ticket
        ticket_id = "recover-val-TSLA"
        with rb._lock:
            rb._open_tickets[ticket_id] = _Ticket(
                ticket_id=ticket_id, risk_dollars=200.0, notional=20_000.0,
            )
            rb._open_risk = 200.0
            rb._open_notional = 20_000.0
        ds = self.engine._state.get_day_state("val", "TSLA")
        ds.in_position = True
        ds.transition("in_pos")
        # Run unmirror. No real ticket -> legacy path.
        self.executor.unmirror("TSLA")
        assert rb.open_count == 0
        assert rb.open_risk == 0.0
        assert ds.phase == "closed"
        assert ds.in_position is False


# ---------- 2. engine.clear_phantom_in_pos (Main's phantom-sweep path) ----------


class TestClearPhantomInPosWithRealTicket:
    """Mirror fix for the engine helper called by scan._orb_phantom_sweep
    when pid='main' (which has no executor instance). Pre-v9.1.26 this
    helper handled only the recover-* ticket + FSM transition; uuid
    tickets leaked. v9.1.26 routes through on_exit when possible.
    """

    def setup_method(self):
        self.engine, self.adapters = _build_engine_with_adapter()
        self.adapter = self.adapters["main"]

    def teardown_method(self):
        _orb_runtime._engine = None
        _orb_runtime._adapters = None

    def test_real_ticket_routes_through_on_exit(self):
        ticket_id, pos = _admit_position(
            self.engine, self.adapter, ticker="TSLA",
        )
        rb = self.engine._risk.get("main")
        ds = self.engine._state.get_day_state("main", "TSLA")
        # Sanity.
        assert rb.open_count == 1
        assert ds.in_position is True
        assert ds.trades_today == 0
        # Clear the phantom.
        result = self.engine.clear_phantom_in_pos("main", "TSLA")
        assert result is True
        # on_exit accounting.
        assert rb.open_count == 0
        assert rb.open_risk == pytest.approx(0.0, abs=1e-6)
        assert ds.in_position is False
        assert ds.phase == "closed"
        assert ds.trades_today == 1
        assert ticket_id not in self.adapter._open_positions

    def test_legacy_recover_only_path_unchanged(self):
        """When no real ticket exists, the legacy recover-* path must
        still work (no regression)."""
        from orb.risk_book import _Ticket
        rb = self.engine._risk.get("main")
        ticket_id = "recover-main-TSLA"
        with rb._lock:
            rb._open_tickets[ticket_id] = _Ticket(
                ticket_id=ticket_id, risk_dollars=300.0, notional=30_000.0,
            )
            rb._open_risk = 300.0
            rb._open_notional = 30_000.0
        ds = self.engine._state.get_day_state("main", "TSLA")
        ds.in_position = True
        ds.transition("in_pos")
        # No real ticket in adapter -- legacy path runs.
        result = self.engine.clear_phantom_in_pos("main", "TSLA")
        assert result is True
        assert rb.open_count == 0
        assert ds.phase == "closed"
