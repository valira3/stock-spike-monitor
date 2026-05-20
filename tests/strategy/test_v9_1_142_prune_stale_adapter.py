"""v9.1.142 -- tests for _prune_stale_adapter_positions.

Closes the 2026-05-20 ORCL incident layered on top of the NVDA one:
  - ORCL admitted in the morning, both v10 adapter + tg.positions
    track it
  - ORCL closed at 11:18 ET via legacy `chandelier_exit` in bison_v5,
    which removed it from `tg.positions` but NOT from
    `adapter._open_positions`
  - Stale adapter ORCL was captured by every subsequent persistence
    dump and rehydrated by V834-PERSIST on every redeploy
  - v9.1.141's adapter-aware V8322 then correctly preserved the
    RiskBook ticket (the adapter STILL had the matching position),
    producing the persistent open_count=2 vs len(positions)=1
    `no_phantom_positions` CRIT

The fix is the symmetric counterpart to V9139-RECOVER: where
V9139-RECOVER catches a position in tg.positions that's missing
from the adapter, V9142-PRUNE catches a position in the adapter
that's missing from tg.positions.
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
    """Bootstrap a minimal engine + adapters covering main/val/gene."""
    from orb import engine as _engine_mod
    from orb import live_adapter as _adapter_mod
    from orb import live_runtime as _rt

    cfg = _engine_mod.OrbConfig()
    eng = _engine_mod.OrbEngine(cfg, portfolio_ids=["main", "val", "gene"])
    adapters = _adapter_mod.LiveAdapterRegistry(eng)

    monkeypatch.setattr(_rt, "_engine", eng, raising=False)
    monkeypatch.setattr(_rt, "_adapters", adapters, raising=False)
    monkeypatch.setattr(_rt, "_bootstrapped", True, raising=False)
    return _rt, eng, adapters


def _seed_adapter_position(eng, adapters, pid, ticker, side="long", shares=100,
                           entry=100.0, stop=99.0, tid_prefix="recover-recover-"):
    """Inject a position directly into adapter._open_positions + RiskBook
    to simulate the post-V834-PERSIST state without going through
    `try_admit`. Returns the synthesized tid."""
    from orb import risk_book as _rb_mod
    from orb.exits import make_position as _make_pos

    rr = float(getattr(eng.cfg, "rr", 2.5) or 2.5)
    tid = f"{tid_prefix}{ticker.lower()}-{pid}-uuid"
    pos = _make_pos(
        portfolio_id=pid,
        ticker=ticker,
        side=side,
        entry_price=entry,
        stop=stop,
        rr=rr,
        shares=shares,
        risk_ticket_id=tid,
    )
    rb = eng._risk.get(pid)
    adapter = adapters.get(pid)
    with rb._lock:
        rb._open_tickets[tid] = _rb_mod._Ticket(
            ticket_id=tid,
            risk_dollars=pos.risk_dollars,
            notional=pos.notional,
        )
        rb._open_risk += pos.risk_dollars
        rb._open_notional += pos.notional
    adapter._open_positions[tid] = pos
    adapter._ticker_to_ticket[ticker] = tid
    return tid


def _fake_tg(positions=None, short_positions=None):
    mod = types.ModuleType("trade_genius")
    mod.positions = positions or {}
    mod.short_positions = short_positions or {}
    return mod


def _fake_executor(positions_by_ticker):
    """Stand-in for executors.bootstrap.get_executor return shape."""
    class _Ex:
        positions = positions_by_ticker
    return _Ex()


def test_main_stale_long_pruned(monkeypatch, fresh_engine):
    """The exact 2026-05-20 ORCL scenario: adapter has ORCL long but
    tg.positions does not. Prune removes adapter entry + releases
    RiskBook ticket. NVDA (genuinely held) stays untouched."""
    rt, eng, adapters = fresh_engine
    main_adapter = adapters.get("main")
    main_rb = eng._risk.get("main")
    # Genuine NVDA (in both adapter AND tg.positions)
    nvda_tid = _seed_adapter_position(eng, adapters, "main", "NVDA",
                                       shares=343, entry=224.71, stop=222.73)
    # Stale ORCL (in adapter but NOT in tg.positions)
    orcl_tid = _seed_adapter_position(eng, adapters, "main", "ORCL",
                                       shares=106, entry=182.39, stop=180.10)
    assert main_rb.open_count == 2
    pre_risk = main_rb.open_risk
    pre_notional = main_rb.open_notional

    monkeypatch.setitem(sys.modules, "trade_genius",
                        _fake_tg(positions={"NVDA": {"side": "LONG"}}))
    # Stub executor lookup so val/gene paths don't error out.
    monkeypatch.setattr("executors.bootstrap.get_executor",
                        lambda pid: None, raising=False)

    rt._prune_stale_adapter_positions()

    # ORCL gone, NVDA kept.
    assert nvda_tid in main_adapter._open_positions
    assert orcl_tid not in main_adapter._open_positions
    assert main_rb.open_count == 1
    # RiskBook _open_risk/_open_notional decremented by exactly the
    # ORCL ticket's contribution (and stays non-negative).
    assert main_rb.open_risk < pre_risk
    assert main_rb.open_notional < pre_notional
    assert main_rb.open_risk >= 0
    assert main_rb.open_notional >= 0


def test_main_stale_short_pruned(monkeypatch, fresh_engine):
    """Same pattern but on the short side. tg.short_positions is the
    source of truth, not tg.positions."""
    rt, eng, adapters = fresh_engine
    _seed_adapter_position(eng, adapters, "main", "TSLA", side="short",
                           shares=50, entry=400.0, stop=410.0)
    monkeypatch.setitem(sys.modules, "trade_genius",
                        _fake_tg(positions={}, short_positions={}))
    monkeypatch.setattr("executors.bootstrap.get_executor",
                        lambda pid: None, raising=False)

    rt._prune_stale_adapter_positions()

    assert eng._risk.get("main").open_count == 0
    assert adapters.get("main")._open_positions == {}


def test_short_in_truth_set_kept(monkeypatch, fresh_engine):
    """tg.short_positions[TSLA] exists -> the TSLA short adapter
    entry must NOT be pruned."""
    rt, eng, adapters = fresh_engine
    tsla_tid = _seed_adapter_position(eng, adapters, "main", "TSLA",
                                       side="short", shares=50,
                                       entry=400.0, stop=410.0)
    monkeypatch.setitem(sys.modules, "trade_genius",
                        _fake_tg(short_positions={"TSLA": {"side": "SHORT"}}))
    monkeypatch.setattr("executors.bootstrap.get_executor",
                        lambda pid: None, raising=False)

    rt._prune_stale_adapter_positions()

    assert tsla_tid in adapters.get("main")._open_positions
    assert eng._risk.get("main").open_count == 1


def test_val_stale_pruned_via_executor(monkeypatch, fresh_engine):
    """Val side: adapter has AVGO but executor.positions doesn't.
    AVGO must be pruned."""
    rt, eng, adapters = fresh_engine
    _seed_adapter_position(eng, adapters, "val", "AVGO",
                           shares=10, entry=200.0, stop=195.0)
    _seed_adapter_position(eng, adapters, "val", "NVDA",
                           shares=20, entry=224.0, stop=222.0)
    monkeypatch.setitem(sys.modules, "trade_genius", _fake_tg())

    def _get_ex(pid):
        if pid == "val":
            return _fake_executor({"NVDA": {"side": "LONG"}})
        return None
    monkeypatch.setattr("executors.bootstrap.get_executor", _get_ex,
                        raising=False)

    rt._prune_stale_adapter_positions()

    val_adapter = adapters.get("val")
    tickers = {p.ticker for p in val_adapter._open_positions.values()}
    assert tickers == {"NVDA"}


def test_executor_disabled_pid_is_skipped(monkeypatch, fresh_engine):
    """When the executor returns None (e.g. ALPACA_SKIP_PORTFOLIOS=gene),
    the pid is skipped rather than pruned against an empty truth set
    -- otherwise a transient executor outage would nuke real
    positions."""
    rt, eng, adapters = fresh_engine
    gene_tid = _seed_adapter_position(eng, adapters, "gene", "QQQ",
                                       shares=5, entry=400.0, stop=395.0)
    monkeypatch.setitem(sys.modules, "trade_genius", _fake_tg())
    monkeypatch.setattr("executors.bootstrap.get_executor",
                        lambda pid: None, raising=False)

    rt._prune_stale_adapter_positions()

    # Gene's adapter position UNTOUCHED because executor is None.
    assert gene_tid in adapters.get("gene")._open_positions
    assert eng._risk.get("gene").open_count == 1


def test_clean_state_is_noop(monkeypatch, fresh_engine):
    """A consistent state (adapter ⊆ truth) should be left alone."""
    rt, eng, adapters = fresh_engine
    nvda_tid = _seed_adapter_position(eng, adapters, "main", "NVDA",
                                       shares=343, entry=224.71, stop=222.73)
    monkeypatch.setitem(sys.modules, "trade_genius",
                        _fake_tg(positions={"NVDA": {"side": "LONG"}}))
    monkeypatch.setattr("executors.bootstrap.get_executor",
                        lambda pid: None, raising=False)

    pre_risk = eng._risk.get("main").open_risk

    rt._prune_stale_adapter_positions()

    assert nvda_tid in adapters.get("main")._open_positions
    assert eng._risk.get("main").open_count == 1
    assert eng._risk.get("main").open_risk == pytest.approx(pre_risk)


def test_riskbook_clamp_non_negative(monkeypatch, fresh_engine):
    """Even if floating-point drift would push _open_risk negative,
    the prune must clamp it to 0 -- same invariant as
    purge_non_recover_tickets."""
    rt, eng, adapters = fresh_engine
    tid = _seed_adapter_position(eng, adapters, "main", "AAPL",
                                  shares=10, entry=200.0, stop=199.0)
    rb = eng._risk.get("main")
    # Force drift: shave open_risk to just below the ticket's
    # risk_dollars so the subtract goes negative.
    with rb._lock:
        ticket_risk = rb._open_tickets[tid].risk_dollars
        rb._open_risk = ticket_risk - 0.5
    monkeypatch.setitem(sys.modules, "trade_genius", _fake_tg())
    monkeypatch.setattr("executors.bootstrap.get_executor",
                        lambda pid: None, raising=False)

    rt._prune_stale_adapter_positions()

    assert rb.open_risk == 0.0
    assert rb.open_notional == 0.0
