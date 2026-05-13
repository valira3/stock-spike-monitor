"""v8.3.26 -- tests for the v18 day-end-giveback defense env vars.

Two new env vars added to ORBConfig in v8.3.26:
  ORB_LOSS_LOCK_THRESHOLD_USD -- when a closed leg's pnl is below
      -threshold, lock that (ticker, side) for the rest of the day.
  ORB_PEAK_DD_HALT_USD -- when intraday realized PnL drops $X below
      the day's running peak, halt all new entries.

These are SUBTRACTIVE rules layered on top of the existing daily-loss
kill switch, blocklist, and concurrent-risk-cap. They live ONLY in
tools/orb_backtest.py (the research harness). The live engine
(orb.live_runtime / orb.risk_book) is NOT modified by this PR -- if
the sweep validates the rules, a follow-up PR will port them to the
live engine.

Behavioral validation runs via docs/research/r6_drawdown_rules.py
sweep against the production bar corpus on data-extensions/rth-expand.
These tests only cover the env-var plumbing + config defaults.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def isolated_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("ORB_"):
            monkeypatch.delenv(k, raising=False)
    yield monkeypatch


class TestNewEnvVars:

    def test_defaults_off(self, isolated_env):
        from tools.orb_backtest import ORBConfig
        cfg = ORBConfig.from_env()
        assert cfg.loss_lock_threshold_usd == 0.0
        assert cfg.peak_dd_halt_usd == 0.0

    def test_loss_lock_parses(self, isolated_env):
        isolated_env.setenv("ORB_LOSS_LOCK_THRESHOLD_USD", "25")
        from tools.orb_backtest import ORBConfig
        cfg = ORBConfig.from_env()
        assert cfg.loss_lock_threshold_usd == 25.0

    def test_peak_dd_parses(self, isolated_env):
        isolated_env.setenv("ORB_PEAK_DD_HALT_USD", "500")
        from tools.orb_backtest import ORBConfig
        cfg = ORBConfig.from_env()
        assert cfg.peak_dd_halt_usd == 500.0

    def test_both_parse_together(self, isolated_env):
        isolated_env.setenv("ORB_LOSS_LOCK_THRESHOLD_USD", "100.5")
        isolated_env.setenv("ORB_PEAK_DD_HALT_USD", "750.0")
        from tools.orb_backtest import ORBConfig
        cfg = ORBConfig.from_env()
        assert cfg.loss_lock_threshold_usd == 100.5
        assert cfg.peak_dd_halt_usd == 750.0

    def test_zero_value_treated_as_off(self, isolated_env):
        isolated_env.setenv("ORB_LOSS_LOCK_THRESHOLD_USD", "0")
        isolated_env.setenv("ORB_PEAK_DD_HALT_USD", "0")
        from tools.orb_backtest import ORBConfig
        cfg = ORBConfig.from_env()
        assert cfg.loss_lock_threshold_usd == 0.0
        assert cfg.peak_dd_halt_usd == 0.0

    def test_negative_value_clamped_to_off_semantically(self, isolated_env):
        # The simulate loop guards with `if cfg.X > 0`, so any
        # non-positive value (including negatives) is OFF. The
        # parser does not reject negative inputs.
        isolated_env.setenv("ORB_LOSS_LOCK_THRESHOLD_USD", "-50")
        from tools.orb_backtest import ORBConfig
        cfg = ORBConfig.from_env()
        # parser preserves the value; downstream `> 0` guards
        # interpret negative as off.
        assert cfg.loss_lock_threshold_usd == -50.0


class TestConfigDataclassFields:
    """Schema-level guards so a future refactor that drops or
    renames a field surfaces immediately as a test failure rather
    than a silent skip."""

    def test_field_names(self):
        from tools.orb_backtest import ORBConfig
        cfg = ORBConfig()
        assert hasattr(cfg, "loss_lock_threshold_usd")
        assert hasattr(cfg, "peak_dd_halt_usd")

    def test_field_types(self):
        from tools.orb_backtest import ORBConfig
        cfg = ORBConfig()
        assert isinstance(cfg.loss_lock_threshold_usd, float)
        assert isinstance(cfg.peak_dd_halt_usd, float)
