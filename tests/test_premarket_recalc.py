"""v5.19.0 \u2014 Tests for premarket_recalc() (vAA-1 ULTIMATE Decision 6).

The 09:29 ET recalc job must be:
  * Idempotent on warm caches (no Alpaca calls when seeded).
  * Per-ticker DI seed gating ("only seed if not yet seeded").
  * Always reload volume profile (picks up nightly rebuild output).
  * Always non-fatal \u2014 failures in any step must not raise.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("SSM_SMOKE_TEST", "1")


@pytest.fixture
def tg_module(monkeypatch):
    """Import trade_genius as `tg` with side-effect-free mocks."""
    import trade_genius as tg

    # Reset module state between tests
    tg._DI_SEED_CACHE.clear()
    tg._volume_profile_cache.clear()
    tg._QQQ_REGIME_SEEDED = False

    # Stub TICKERS / TRADE_TICKERS for deterministic iteration
    monkeypatch.setattr(tg, "TICKERS", ["AAPL", "MSFT", "QQQ"])
    monkeypatch.setattr(tg, "TRADE_TICKERS", ["AAPL", "MSFT"])

    return tg


def test_premarket_recalc_warm_cache_is_noop(tg_module, monkeypatch):
    """When DI seeded for all trade tickers and QQQ regime seeded,
    no DI seeds should be re-run. Volume profile reload always runs."""
    tg = tg_module
    # Pre-seed everything
    tg._DI_SEED_CACHE["AAPL"] = [{"bucket": 1, "high": 1, "low": 1, "close": 1}]
    tg._DI_SEED_CACHE["MSFT"] = [{"bucket": 1, "high": 1, "low": 1, "close": 1}]
    tg._QQQ_REGIME_SEEDED = True

    seed_one = MagicMock()
    qqq_seed = MagicMock()
    vp_load = MagicMock(return_value={"profile": "ok"})

    monkeypatch.setattr("engine.seeders.seed_di_buffer", seed_one)
    monkeypatch.setattr(tg, "_v590_qqq_regime_seed_once", qqq_seed)
    monkeypatch.setattr(tg.volume_profile, "load_profile", vp_load)

    tg.premarket_recalc()

    # No DI seeds for already-seeded tickers
    assert seed_one.call_count == 0
    # qqq_regime_seed_once still called (idempotent itself)
    assert qqq_seed.call_count == 1
    # Volume profile always reloaded
    assert vp_load.call_count == len(tg.TICKERS)


def test_premarket_recalc_cold_cache_seeds_all(tg_module, monkeypatch):
    """When DI cache empty, every trade ticker should be seeded."""
    tg = tg_module
    # Cache fully empty (the fixture cleared it)

    seed_one = MagicMock()
    qqq_seed = MagicMock()
    vp_load = MagicMock(return_value={"profile": "ok"})

    monkeypatch.setattr("engine.seeders.seed_di_buffer", seed_one)
    monkeypatch.setattr(tg, "_v590_qqq_regime_seed_once", qqq_seed)
    monkeypatch.setattr(tg.volume_profile, "load_profile", vp_load)

    tg.premarket_recalc()

    # Every trade ticker seeded
    assert seed_one.call_count == len(tg.TRADE_TICKERS)
    seeded_tickers = {call.args[0] for call in seed_one.call_args_list}
    assert seeded_tickers == set(tg.TRADE_TICKERS)
    # All volume profiles reloaded
    assert vp_load.call_count == len(tg.TICKERS)


def test_premarket_recalc_partial_cache_seeds_only_missing(tg_module, monkeypatch):
    """When only some tickers have DI seed, only the missing ones get seeded."""
    tg = tg_module
    tg._DI_SEED_CACHE["AAPL"] = [{"bucket": 1, "high": 1, "low": 1, "close": 1}]
    # MSFT not seeded

    seed_one = MagicMock()
    monkeypatch.setattr("engine.seeders.seed_di_buffer", seed_one)
    monkeypatch.setattr(tg, "_v590_qqq_regime_seed_once", MagicMock())
    monkeypatch.setattr(tg.volume_profile, "load_profile", MagicMock(return_value={"p": 1}))

    tg.premarket_recalc()

    # Only MSFT should be seeded
    assert seed_one.call_count == 1
    assert seed_one.call_args.args[0] == "MSFT"


def test_premarket_recalc_failures_non_fatal(tg_module, monkeypatch):
    """Any sub-step raising an exception must not propagate."""
    tg = tg_module

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr("engine.seeders.seed_di_buffer", boom)
    monkeypatch.setattr(tg, "_v590_qqq_regime_seed_once", boom)
    monkeypatch.setattr(tg.volume_profile, "load_profile", boom)

    # Must not raise
    tg.premarket_recalc()


def test_premarket_recalc_volprof_reload_overwrites_cache(tg_module, monkeypatch):
    """Volume profile reload should replace cached entries (the whole
    point of always-running step 4: pick up nightly rebuild output)."""
    tg = tg_module
    tg._volume_profile_cache["AAPL"] = {"profile": "stale"}

    fresh = {"profile": "fresh"}
    monkeypatch.setattr(tg.volume_profile, "load_profile", MagicMock(return_value=fresh))
    monkeypatch.setattr(tg, "_v590_qqq_regime_seed_once", MagicMock())
    tg._DI_SEED_CACHE["AAPL"] = [{"bucket": 1, "high": 1, "low": 1, "close": 1}]
    tg._DI_SEED_CACHE["MSFT"] = [{"bucket": 1, "high": 1, "low": 1, "close": 1}]

    tg.premarket_recalc()

    assert tg._volume_profile_cache["AAPL"] == fresh


def test_premarket_recalc_in_jobs_table(tg_module):
    """Confirm the 09:29 ET entry exists in the scheduler JOBS table.

    We can't easily run scheduler_thread (it's a while True loop), but we
    can read the source and confirm the JOBS literal includes 09:29 wired
    to premarket_recalc.
    """
    import inspect

    tg = tg_module
    src = inspect.getsource(tg.scheduler_thread)
    assert '"09:29"' in src
    assert "premarket_recalc" in src
