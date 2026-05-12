"""v8.1.3 -- shared autouse fixture pinning ORB_PARTIAL_PROFIT_AT_1R=0
for the strategy test suite.

Why:
  In v8.1.3 the env-fallback default in orb/live_runtime.py was
  flipped False -> True. That means a fresh OrbConfig (no ORB_*
  env vars set) now constructs with partial_profit_at_1r=True --
  the engine emits EXIT_PARTIAL on first 1R touch instead of
  arming BE and continuing.

  ~20 strategy tests pre-date v8.1.0 and codify the LEGACY
  non-partial path (full target/stop/eod on first 1R touch, BE
  arm without partial fire). Their per-file `isolated_env`
  fixtures already wipe ORB_* env vars, but post-v8.1.3 that
  wipe leaves partial=True, breaking those tests.

  Rather than touching every per-file isolated_env fixture, this
  autouse fixture runs BEFORE each test in tests/strategy/ and
  forces ORB_PARTIAL_PROFIT_AT_1R=0. Tests that DO want partial
  behavior (test_orb_partial_profit.py) set
  ORB_PARTIAL_PROFIT_AT_1R=1 in their own per-test fixture which
  monkeypatches AFTER this autouse runs and wins.

  Net effect: legacy tests keep testing legacy behavior; new
  partial tests keep testing partial behavior; CI stays green
  through the v8.1.3 default flip.
"""
import os

import pytest


@pytest.fixture(autouse=True)
def _strategy_default_partial_off(monkeypatch):
    # Run BEFORE per-file `isolated_env` fixtures (which wipe ORB_*
    # vars). After their wipe, the env lookup falls back to the
    # process env where this autouse fixture has already set
    # PARTIAL=0. Tests that want partial-on call
    # monkeypatch.setenv(..., "1") AFTER this fixture runs.
    monkeypatch.setenv("ORB_PARTIAL_PROFIT_AT_1R", "0")
    yield
