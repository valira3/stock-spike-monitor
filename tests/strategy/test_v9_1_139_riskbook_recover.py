"""v9.1.139 -- tests for the RiskBook auto-recover-from-held-positions path.

Closes the 2026-05-20 NVDA incident where:
  - NVDA admitted at 10:21 ET (RiskBook ticket created in-memory)
  - Railway restart fired BEFORE the next throttled persistence dump
  - Restart rehydrated tg.positions (paper_state.json had NVDA) but
    NOT the RiskBook ticket (orb_state_<date>.json hadn't been dumped)
  - Result: tg.positions ∋ NVDA but RiskBook ∌ NVDA → CRIT
    no_phantom_positions invariant

Fix: after ensure_session_started's rehydrate, scan tg.positions /
tg.short_positions / executor.positions and synthesize a recover-
RiskBook ticket for any held position not currently tracked.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def fresh_engine(monkeypatch):
    """Bootstrap a minimal engine + adapters with main portfolio_id only."""
    from orb import engine as _engine_mod
    from orb import live_adapter as _adapter_mod
    from orb import live_runtime as _rt

    cfg = _engine_mod.OrbConfig()
    eng = _engine_mod.OrbEngine(cfg, portfolio_ids=["main"])
    adapters = _adapter_mod.LiveAdapterRegistry(eng)

    monkeypatch.setattr(_rt, "_engine", eng, raising=False)
    monkeypatch.setattr(_rt, "_adapters", adapters, raising=False)
    monkeypatch.setattr(_rt, "_bootstrapped", True, raising=False)
    return _rt, eng, adapters


def _fake_tg_with_positions(positions=None, short_positions=None):
    """Build a fake trade_genius module shim with positions dicts."""
    mod = types.ModuleType("trade_genius")
    mod.positions = positions or {}
    mod.short_positions = short_positions or {}
    mod.BOT_NAME = "TradeGenius"
    return mod


def test_recover_synthesizes_ticket_for_main_long(monkeypatch, fresh_engine):
    """The 2026-05-20 NVDA scenario: tg.positions has NVDA LONG but
    RiskBook is empty. Auto-recover must synthesize a recover- ticket."""
    rt, eng, adapters = fresh_engine
    fake_tg = _fake_tg_with_positions(
        positions={
            "NVDA": {
                "side": "LONG",
                "shares": 343,
                "entry": 224.71,
                "entry_stop": 223.59,
                "stop": 223.59,
            }
        }
    )
    monkeypatch.setitem(sys.modules, "trade_genius", fake_tg)

    # Confirm pre-state
    rb = eng._risk.get("main")
    assert rb.snapshot()["open_count"] == 0

    rt._recover_riskbook_from_held_positions()

    # Confirm post-state: ticket synthesized
    assert rb.snapshot()["open_count"] == 1
    adapter = adapters.get("main")
    assert "recover-held-NVDA-main" in adapter._open_positions
    pos = adapter._open_positions["recover-held-NVDA-main"]
    assert pos.ticker == "NVDA"
    assert pos.side == "long"
    assert pos.shares == 343


def test_recover_idempotent_when_already_tracked(monkeypatch, fresh_engine):
    """If the ticker is ALREADY in the adapter's _open_positions, skip."""
    rt, eng, adapters = fresh_engine
    fake_tg = _fake_tg_with_positions(
        positions={
            "MSFT": {"side": "LONG", "shares": 100, "entry": 410.0, "stop": 408.0}
        }
    )
    monkeypatch.setitem(sys.modules, "trade_genius", fake_tg)

    # Pre-populate adapter so the recover should be a no-op.
    from orb.exits import make_position
    pre_pos = make_position(
        portfolio_id="main", ticker="MSFT", side="long",
        entry_price=410.0, stop=408.0, rr=2.5, shares=100,
        risk_ticket_id="existing-T-MSFT",
    )
    adapters.get("main")._open_positions["existing-T-MSFT"] = pre_pos

    rt._recover_riskbook_from_held_positions()

    # Adapter still has 1 entry, not 2.
    adapter = adapters.get("main")
    assert len(adapter._open_positions) == 1
    assert "existing-T-MSFT" in adapter._open_positions
    assert "recover-held-MSFT-main" not in adapter._open_positions


def test_recover_no_op_when_tg_positions_empty(monkeypatch, fresh_engine):
    """No held positions => no synthesis."""
    rt, eng, adapters = fresh_engine
    fake_tg = _fake_tg_with_positions()  # empty
    monkeypatch.setitem(sys.modules, "trade_genius", fake_tg)

    rt._recover_riskbook_from_held_positions()
    rb = eng._risk.get("main")
    assert rb.snapshot()["open_count"] == 0


def test_recover_handles_short_positions(monkeypatch, fresh_engine):
    """Short positions also get synthesized."""
    rt, eng, adapters = fresh_engine
    fake_tg = _fake_tg_with_positions(
        short_positions={
            "TSLA": {"side": "SHORT", "shares": 50, "entry": 400.0, "stop": 402.0}
        }
    )
    monkeypatch.setitem(sys.modules, "trade_genius", fake_tg)

    rt._recover_riskbook_from_held_positions()

    rb = eng._risk.get("main")
    assert rb.snapshot()["open_count"] == 1
    adapter = adapters.get("main")
    pos = adapter._open_positions["recover-held-TSLA-main"]
    assert pos.side == "short"
    assert pos.shares == 50


def test_recover_skips_invalid_geometry(monkeypatch, fresh_engine):
    """Entry == stop, or zero shares, or zero prices => skip safely."""
    rt, eng, adapters = fresh_engine
    fake_tg = _fake_tg_with_positions(
        positions={
            "BADENTRY": {"side": "LONG", "shares": 100, "entry": 0, "stop": 100},
            "BADSTOP": {"side": "LONG", "shares": 100, "entry": 100, "stop": 0},
            "BADSHARES": {"side": "LONG", "shares": 0, "entry": 100, "stop": 99},
            "ENTRYEQSTOP": {"side": "LONG", "shares": 100, "entry": 100, "stop": 100},
        }
    )
    monkeypatch.setitem(sys.modules, "trade_genius", fake_tg)
    rt._recover_riskbook_from_held_positions()
    rb = eng._risk.get("main")
    assert rb.snapshot()["open_count"] == 0  # all 4 rejected


def test_persist_force_bypasses_throttle(monkeypatch, fresh_engine, tmp_path):
    """v9.1.139 force=True bypasses the throttle window."""
    import datetime as _dt
    from orb import live_runtime as rt
    monkeypatch.setattr(rt, "_session_date", "2026-05-20", raising=False)
    monkeypatch.setattr(rt, "_persist_min_interval_s", 3600, raising=False)
    # Set last-persist to 1 second ago so throttle (3600s) will block.
    now = _dt.datetime.now(_dt.timezone.utc)
    monkeypatch.setattr(rt, "_persist_last_iso", now.isoformat(), raising=False)

    # Mock dump_state_to_disk to count calls without writing real files.
    call_count = {"n": 0}
    def _fake_dump(*a, **kw):
        call_count["n"] += 1
        return True

    import orb.persistence as _persist
    monkeypatch.setattr(_persist, "dump_state_to_disk", _fake_dump)

    # Without force: throttle blocks (last was now, threshold 3600s).
    result1 = rt.persist_engine_state(force=False)
    assert result1 is False, "throttle should block when within interval"
    assert call_count["n"] == 0

    # With force: bypasses throttle
    result2 = rt.persist_engine_state(force=True)
    assert result2 is True, "force should bypass throttle"
    assert call_count["n"] == 1
