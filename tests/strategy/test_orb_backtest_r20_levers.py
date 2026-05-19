"""R20 (2026-05-18) -- env-var plumbing for the afternoon-discipline
levers added to tools/orb_backtest.py.

The behavior these levers gate is tested by the sweep itself
(docs/research/r20_afternoon_discipline.py). These tests only cover the
config field defaults + env parsing so the sweep harness can dispatch
variants without surprises.

Levers:
  ORB_EOD_PREP_EXIT_ET           -- hard time exit, "HH:MM"
  ORB_MFE_GIVEBACK_BPS           -- bps giveback from MFE
  ORB_MFE_GIVEBACK_START_ET      -- when the giveback check activates
  ORB_AFTERNOON_TRAIL_PCT        -- chandelier-trail width as fraction
                                    of initial_risk
  ORB_AFTERNOON_TRAIL_START_ET   -- when the trail activates

Backtest-only -- live engine wiring deferred until the sweep validates
a winner.
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


class TestR20Defaults:
    def test_defaults_off(self, isolated_env):
        from tools.orb_backtest import ORBConfig

        cfg = ORBConfig.from_env()
        assert cfg.eod_prep_exit_minutes == 0
        assert cfg.mfe_giveback_bps == 0.0
        assert cfg.mfe_giveback_start_minutes == 0
        assert cfg.afternoon_trail_pct == 0.0
        # afternoon_trail_start_minutes defaults to 14:00 = 840 even when
        # the pct lever is off (only the pct=0 disables the behavior).
        assert cfg.afternoon_trail_start_minutes == 14 * 60


class TestR20EnvParsing:
    def test_eod_prep_exit_et_parses(self, isolated_env):
        isolated_env.setenv("ORB_EOD_PREP_EXIT_ET", "14:30")
        from tools.orb_backtest import ORBConfig

        cfg = ORBConfig.from_env()
        assert cfg.eod_prep_exit_minutes == 14 * 60 + 30

    def test_eod_prep_exit_empty_is_off(self, isolated_env):
        isolated_env.setenv("ORB_EOD_PREP_EXIT_ET", "")
        from tools.orb_backtest import ORBConfig

        cfg = ORBConfig.from_env()
        assert cfg.eod_prep_exit_minutes == 0

    def test_mfe_giveback_parses(self, isolated_env):
        isolated_env.setenv("ORB_MFE_GIVEBACK_BPS", "25")
        isolated_env.setenv("ORB_MFE_GIVEBACK_START_ET", "14:00")
        from tools.orb_backtest import ORBConfig

        cfg = ORBConfig.from_env()
        assert cfg.mfe_giveback_bps == 25.0
        assert cfg.mfe_giveback_start_minutes == 14 * 60

    def test_mfe_start_empty_means_always(self, isolated_env):
        """When the start ET is empty but bps>0, the giveback check
        activates from bar 0 (minutes=0)."""
        isolated_env.setenv("ORB_MFE_GIVEBACK_BPS", "40")
        isolated_env.setenv("ORB_MFE_GIVEBACK_START_ET", "")
        from tools.orb_backtest import ORBConfig

        cfg = ORBConfig.from_env()
        assert cfg.mfe_giveback_bps == 40.0
        assert cfg.mfe_giveback_start_minutes == 0

    def test_afternoon_trail_parses(self, isolated_env):
        isolated_env.setenv("ORB_AFTERNOON_TRAIL_PCT", "0.3")
        isolated_env.setenv("ORB_AFTERNOON_TRAIL_START_ET", "14:30")
        from tools.orb_backtest import ORBConfig

        cfg = ORBConfig.from_env()
        assert cfg.afternoon_trail_pct == 0.3
        assert cfg.afternoon_trail_start_minutes == 14 * 60 + 30


class TestR20Composition:
    """A theory may set multiple R20 levers simultaneously
    (mfe_giveback + eod_prep_exit). The fields must coexist without
    interference -- each lever is independently active when its
    primary threshold (>0 / non-empty) is set."""

    def test_all_three_set_simultaneously(self, isolated_env):
        isolated_env.setenv("ORB_EOD_PREP_EXIT_ET", "14:50")
        isolated_env.setenv("ORB_MFE_GIVEBACK_BPS", "25")
        isolated_env.setenv("ORB_MFE_GIVEBACK_START_ET", "14:00")
        isolated_env.setenv("ORB_AFTERNOON_TRAIL_PCT", "0.3")
        isolated_env.setenv("ORB_AFTERNOON_TRAIL_START_ET", "14:00")
        from tools.orb_backtest import ORBConfig

        cfg = ORBConfig.from_env()
        assert cfg.eod_prep_exit_minutes == 14 * 60 + 50
        assert cfg.mfe_giveback_bps == 25.0
        assert cfg.mfe_giveback_start_minutes == 14 * 60
        assert cfg.afternoon_trail_pct == 0.3
        assert cfg.afternoon_trail_start_minutes == 14 * 60
