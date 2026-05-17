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


@pytest.fixture(autouse=True)
def _strategy_isolate_orb_persistence(tmp_path, monkeypatch):
    # v9.1.119 -- pin orb.persistence file location to a per-test tmp
    # dir. Without this, dump_state_to_disk writes orb_state_<date>.json
    # to the repo root (orb.persistence._default_path_template falls
    # back to "." when PAPER_STATE_PATH is unset). A later test's
    # ensure_session_started -> _try_rehydrate_engine_state then finds
    # the stale file and overlays its equity/state on top of the fresh
    # test fixture. The 3 tests using date_iso="2026-01-02"
    # (test_bootstrap_compounding_default_on,
    #  test_bootstrap_then_session_then_feed,
    #  test_three_portfolios_independent) silently failed with
    # rb.equity == 100_000.0 instead of 105_000.0 whenever an earlier
    # test in the run (e.g. test_orb_entry_route) had persisted state
    # for that date via engine.scan.persist_engine_state.
    #
    # NOTE: per-file isolated_env fixtures iterate os.environ and
    # delenv every ORB_* key, which would wipe an env-var-based patch.
    # Patch the resolver function directly so the override survives
    # those wipes.
    import orb.persistence as _orb_persistence

    template = str(tmp_path / "orb_state_{date}.json")
    monkeypatch.setattr(_orb_persistence, "_default_path_template", lambda: template)
    yield
