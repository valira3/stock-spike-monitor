"""v10.0.1 -- per-portfolio env overrides for OrbEngine admission caps.

Covers:
  - orb.portfolio_env.resolve_* helpers: precedence (per-PID > global >
    default), type coercion failures fall back to default, empty string
    is treated as "not set".
  - OrbEngine actually applies the per-portfolio override when
    registering each portfolio's RiskBook.
"""
from __future__ import annotations

import os
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helper precedence
# ---------------------------------------------------------------------------


def test_resolve_str_per_pid_wins(monkeypatch):
    from orb.portfolio_env import resolve_str
    monkeypatch.setenv("ORB_FOO", "global")
    monkeypatch.setenv("VAL_ORB_FOO", "val-specific")
    assert resolve_str("val", "ORB_FOO", "default") == "val-specific"


def test_resolve_str_falls_back_to_global(monkeypatch):
    from orb.portfolio_env import resolve_str
    monkeypatch.delenv("VAL_ORB_FOO", raising=False)
    monkeypatch.setenv("ORB_FOO", "global")
    assert resolve_str("val", "ORB_FOO", "default") == "global"


def test_resolve_str_falls_back_to_default(monkeypatch):
    from orb.portfolio_env import resolve_str
    monkeypatch.delenv("VAL_ORB_FOO", raising=False)
    monkeypatch.delenv("ORB_FOO", raising=False)
    assert resolve_str("val", "ORB_FOO", "default") == "default"


def test_resolve_str_no_portfolio_skips_per_pid(monkeypatch):
    from orb.portfolio_env import resolve_str
    monkeypatch.setenv("VAL_ORB_FOO", "val-only")
    monkeypatch.setenv("ORB_FOO", "global")
    # portfolio_id=None must NOT consult VAL_ prefix
    assert resolve_str(None, "ORB_FOO", "default") == "global"
    assert resolve_str("", "ORB_FOO", "default") == "global"


def test_resolve_str_empty_per_pid_falls_through(monkeypatch):
    """Empty-string per-PID env should NOT mask the global setting."""
    from orb.portfolio_env import resolve_str
    monkeypatch.setenv("VAL_ORB_FOO", "")
    monkeypatch.setenv("ORB_FOO", "global")
    assert resolve_str("val", "ORB_FOO", "default") == "global"


def test_resolve_float_per_pid(monkeypatch):
    from orb.portfolio_env import resolve_float
    monkeypatch.setenv("VAL_ORB_CAP", "1500")
    monkeypatch.setenv("ORB_CAP", "2000")
    assert resolve_float("val", "ORB_CAP", 999.0) == 1500.0


def test_resolve_float_bad_value_falls_to_default(monkeypatch):
    from orb.portfolio_env import resolve_float
    monkeypatch.setenv("VAL_ORB_CAP", "not-a-number")
    monkeypatch.setenv("ORB_CAP", "2000")
    # Bad per-PID falls through to default (NOT to global) -- the helper
    # only attempts ONE numeric parse per call.
    out = resolve_float("val", "ORB_CAP", 999.0)
    assert out == 999.0


def test_resolve_int_uses_per_pid(monkeypatch):
    from orb.portfolio_env import resolve_int
    monkeypatch.setenv("MAIN_ORB_TRADES", "10")
    monkeypatch.setenv("ORB_TRADES", "5")
    assert resolve_int("main", "ORB_TRADES", 1) == 10


def test_resolve_bool_truthy_per_pid(monkeypatch):
    from orb.portfolio_env import resolve_bool
    monkeypatch.setenv("GENE_ORB_PARTIAL_PROFIT_AT_1R", "1")
    monkeypatch.setenv("ORB_PARTIAL_PROFIT_AT_1R", "0")
    assert resolve_bool("gene", "ORB_PARTIAL_PROFIT_AT_1R", False) is True


def test_resolve_bool_global_falsy(monkeypatch):
    from orb.portfolio_env import resolve_bool
    monkeypatch.delenv("VAL_ORB_FOO", raising=False)
    monkeypatch.setenv("ORB_FOO", "0")
    assert resolve_bool("val", "ORB_FOO", True) is False


# ---------------------------------------------------------------------------
# OrbEngine.__init__ applies the per-portfolio overrides
# ---------------------------------------------------------------------------


def _make_engine(portfolio_ids=("main", "val", "gene")):
    from orb.engine import OrbConfig, OrbEngine
    cfg = OrbConfig(
        max_concurrent_risk_dollars=2000.0,
        max_concurrent_notional_mult=0.95,
        daily_loss_kill_pct=2.0,
    )
    return OrbEngine(cfg, portfolio_ids=list(portfolio_ids))


def test_engine_applies_per_pid_max_risk(monkeypatch):
    """Val's RiskBook should end up with the per-PID cap, Main with the global."""
    # Strip any cross-test bleed
    for k in ("VAL_ORB_MAX_CONCURRENT_RISK_DOLLARS",
              "MAIN_ORB_MAX_CONCURRENT_RISK_DOLLARS",
              "GENE_ORB_MAX_CONCURRENT_RISK_DOLLARS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VAL_ORB_MAX_CONCURRENT_RISK_DOLLARS", "1500")

    eng = _make_engine()
    assert eng._risk.get("val")._max_risk == 1500.0
    assert eng._risk.get("main")._max_risk == 2000.0  # global default
    assert eng._risk.get("gene")._max_risk == 2000.0


def test_engine_applies_per_pid_notional_mult(monkeypatch):
    for k in ("VAL_ORB_MAX_CONCURRENT_NOTIONAL_MULT",
              "MAIN_ORB_MAX_CONCURRENT_NOTIONAL_MULT",
              "GENE_ORB_MAX_CONCURRENT_NOTIONAL_MULT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MAIN_ORB_MAX_CONCURRENT_NOTIONAL_MULT", "0.5")

    eng = _make_engine()
    assert eng._risk.get("main")._max_notional_mult == 0.5
    assert eng._risk.get("val")._max_notional_mult == 0.95  # global


def test_engine_applies_per_pid_daily_kill(monkeypatch):
    for k in ("VAL_ORB_DAILY_LOSS_KILL_PCT",
              "MAIN_ORB_DAILY_LOSS_KILL_PCT",
              "GENE_ORB_DAILY_LOSS_KILL_PCT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GENE_ORB_DAILY_LOSS_KILL_PCT", "1.0")

    eng = _make_engine()
    assert eng._risk.get("gene")._daily_loss_kill_pct == 1.0
    assert eng._risk.get("main")._daily_loss_kill_pct == 2.0  # global


def test_engine_no_per_pid_env_uses_global(monkeypatch):
    """All three portfolios get the cfg defaults when no per-PID env is set."""
    for pid in ("MAIN", "VAL", "GENE"):
        for k in (f"{pid}_ORB_MAX_CONCURRENT_RISK_DOLLARS",
                  f"{pid}_ORB_MAX_CONCURRENT_NOTIONAL_MULT",
                  f"{pid}_ORB_DAILY_LOSS_KILL_PCT"):
            monkeypatch.delenv(k, raising=False)
    for k in ("ORB_MAX_CONCURRENT_RISK_DOLLARS",
              "ORB_MAX_CONCURRENT_NOTIONAL_MULT",
              "ORB_DAILY_LOSS_KILL_PCT"):
        monkeypatch.delenv(k, raising=False)

    eng = _make_engine()
    for pid in ("main", "val", "gene"):
        assert eng._risk.get(pid)._max_risk == 2000.0
        assert eng._risk.get(pid)._max_notional_mult == 0.95
        assert eng._risk.get(pid)._daily_loss_kill_pct == 2.0


def test_engine_global_orb_env_still_applies_uniformly(monkeypatch):
    """If only the global ORB_* env is set (no per-PID), every portfolio
    should see it."""
    for pid in ("MAIN", "VAL", "GENE"):
        monkeypatch.delenv(f"{pid}_ORB_MAX_CONCURRENT_RISK_DOLLARS", raising=False)
    monkeypatch.setenv("ORB_MAX_CONCURRENT_RISK_DOLLARS", "2500")
    eng = _make_engine()
    for pid in ("main", "val", "gene"):
        assert eng._risk.get(pid)._max_risk == 2500.0


def test_engine_mixed_overrides(monkeypatch):
    """Val tighter, Gene looser, Main on global default -- all coexist."""
    for pid in ("MAIN", "VAL", "GENE"):
        monkeypatch.delenv(f"{pid}_ORB_MAX_CONCURRENT_RISK_DOLLARS", raising=False)
    monkeypatch.setenv("ORB_MAX_CONCURRENT_RISK_DOLLARS", "2000")
    monkeypatch.setenv("VAL_ORB_MAX_CONCURRENT_RISK_DOLLARS", "1000")
    monkeypatch.setenv("GENE_ORB_MAX_CONCURRENT_RISK_DOLLARS", "3500")
    eng = _make_engine()
    assert eng._risk.get("main")._max_risk == 2000.0
    assert eng._risk.get("val")._max_risk == 1000.0
    assert eng._risk.get("gene")._max_risk == 3500.0
