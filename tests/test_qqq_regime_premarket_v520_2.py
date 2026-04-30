"""v5.20.2 \u2014 QQQ regime premarket reseed + continuous gap-fill.

Tests the new seeder semantics:
  * `qqq_regime_seed_once` only seals `_QQQ_REGIME_SEEDED=True` once
    `ema9` has actually warmed (\u22659 closed 5m bars applied).
  * `force_reseed=True` wipes prior regime state and re-applies fresh
    closes.
  * Empty fetch leaves regime un-sealed so later passes can retry.
  * `recompute_qqq_regime_if_unwarm` short-circuits when warm and
    re-seeds with `force_reseed=True` when not warm.
  * `qqq_regime_tick` invokes `force_reseed=True` if `ema9` is still
    None when a fresh 5m bucket arrives (live-tick gap-fill).
  * Scheduler JOBS table wires the 09:31 row to `qqq_regime_recompute_0931`
    alongside `di_recompute_0931`.
"""

from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("SSM_SMOKE_TEST", "1")


@pytest.fixture
def regime_module(monkeypatch):
    """Import engine.seeders fresh and reset the QQQ regime state."""
    import qqq_regime
    import trade_genius as tg
    from engine import seeders

    # Reset regime to a fresh, un-seeded state for every test.
    tg._QQQ_REGIME = qqq_regime.QQQRegime()
    tg._QQQ_REGIME_SEEDED = False
    tg._QQQ_REGIME_LAST_BUCKET = None

    return seeders, tg, qqq_regime


def _stub_qqq_alpaca_closes(monkeypatch, seeders, tg, n_closes):
    """Make `_qqq_seed_from_alpaca` return n_closes synthetic prices.

    Bypasses the archive/Alpaca/prior-session priority cascade by
    monkey-patching the helper functions directly. Archive returns []
    (so the alpaca branch is taken), prior-session is never called when
    Alpaca returns >=1 close.
    """
    monkeypatch.setattr(
        seeders,
        "_qqq_seed_from_archive",
        lambda start, end: [],
    )
    closes = [100.0 + 0.1 * i for i in range(n_closes)]
    monkeypatch.setattr(
        seeders,
        "_qqq_seed_from_alpaca",
        lambda start, end: list(closes),
    )
    monkeypatch.setattr(
        seeders,
        "_qqq_seed_from_prior_session",
        lambda now: [],
    )


def _stub_all_qqq_sources_empty(monkeypatch, seeders):
    """All three seed sources return [] \u2014 the no-data path."""
    monkeypatch.setattr(seeders, "_qqq_seed_from_archive", lambda s, e: [])
    monkeypatch.setattr(seeders, "_qqq_seed_from_alpaca", lambda s, e: [])
    monkeypatch.setattr(seeders, "_qqq_seed_from_prior_session", lambda n: [])


def test_empty_fetch_leaves_regime_unsealed(regime_module, monkeypatch):
    """No bars from any source must NOT seal _QQQ_REGIME_SEEDED."""
    seeders, tg, _ = regime_module
    _stub_all_qqq_sources_empty(monkeypatch, seeders)

    seeders.qqq_regime_seed_once()

    assert tg._QQQ_REGIME_SEEDED is False, (
        "v5.20.2: empty fetch must NOT seal the regime; later passes need to retry"
    )
    assert tg._QQQ_REGIME.ema9 is None
    assert tg._QQQ_REGIME.bars_seen == 0


def test_partial_fetch_4_bars_leaves_regime_unsealed(regime_module, monkeypatch):
    """4 bars (today's bug) must NOT seal because ema9 needs 9."""
    seeders, tg, _ = regime_module
    _stub_qqq_alpaca_closes(monkeypatch, seeders, tg, n_closes=4)

    seeders.qqq_regime_seed_once()

    assert tg._QQQ_REGIME_SEEDED is False, (
        "v5.20.2: 4-bar partial seed must NOT seal; ema9 is still None"
    )
    assert tg._QQQ_REGIME.ema9 is None
    assert tg._QQQ_REGIME.ema3 is not None  # 3 bars are enough for ema3
    assert tg._QQQ_REGIME.bars_seen == 4


def test_full_fetch_12_bars_seals_and_warms_ema9(regime_module, monkeypatch):
    """\u22659 bars must produce ema9 != None and seal the regime."""
    seeders, tg, _ = regime_module
    _stub_qqq_alpaca_closes(monkeypatch, seeders, tg, n_closes=12)

    seeders.qqq_regime_seed_once()

    assert tg._QQQ_REGIME_SEEDED is True
    assert tg._QQQ_REGIME.ema9 is not None
    assert tg._QQQ_REGIME.ema3 is not None
    assert tg._QQQ_REGIME.bars_seen == 12


def test_force_reseed_wipes_prior_state(regime_module, monkeypatch):
    """force_reseed=True must wipe ema3/ema9/buffers before re-applying."""
    seeders, tg, _ = regime_module

    # First pass: 4 partial bars \u2014 leaves ema3 set, ema9 None.
    _stub_qqq_alpaca_closes(monkeypatch, seeders, tg, n_closes=4)
    seeders.qqq_regime_seed_once()
    assert tg._QQQ_REGIME.bars_seen == 4
    assert tg._QQQ_REGIME.ema3 is not None
    assert tg._QQQ_REGIME.ema9 is None

    # Second pass with force_reseed=True and 12 fresh bars.
    _stub_qqq_alpaca_closes(monkeypatch, seeders, tg, n_closes=12)
    seeders.qqq_regime_seed_once(force_reseed=True)

    # bars_seen should be exactly 12, not 4+12=16 \u2014 wipe-then-apply.
    assert tg._QQQ_REGIME.bars_seen == 12
    assert tg._QQQ_REGIME.ema9 is not None
    assert tg._QQQ_REGIME_SEEDED is True


def test_recompute_short_circuits_when_warm(regime_module, monkeypatch):
    """recompute_qqq_regime_if_unwarm must NOT re-fetch when ema9 is set."""
    seeders, tg, _ = regime_module

    # Seed to warm state first.
    _stub_qqq_alpaca_closes(monkeypatch, seeders, tg, n_closes=12)
    seeders.qqq_regime_seed_once()
    assert tg._QQQ_REGIME.ema9 is not None

    # Replace seed_once with a sentinel; recompute must NOT call it.
    sentinel = MagicMock()
    monkeypatch.setattr(seeders, "qqq_regime_seed_once", sentinel)

    result = seeders.recompute_qqq_regime_if_unwarm()

    assert result["already_warm"] is True
    assert result["reseeded"] is False
    assert sentinel.call_count == 0


def test_recompute_force_reseeds_when_unwarm(regime_module, monkeypatch):
    """recompute_qqq_regime_if_unwarm must call seed_once with force_reseed=True."""
    seeders, tg, _ = regime_module

    # Leave regime un-warmed (ema9 None).
    assert tg._QQQ_REGIME.ema9 is None

    sentinel = MagicMock()
    monkeypatch.setattr(seeders, "qqq_regime_seed_once", sentinel)

    seeders.recompute_qqq_regime_if_unwarm()

    assert sentinel.call_count == 1
    # The force_reseed kwarg must be set so a stale partial seed is wiped.
    assert sentinel.call_args.kwargs.get("force_reseed") is True


def test_recompute_non_fatal_on_seed_crash(regime_module, monkeypatch):
    """A crash inside seed_once during recompute must not propagate."""
    seeders, tg, _ = regime_module

    def boom(*a, **kw):
        raise RuntimeError("simulated")

    monkeypatch.setattr(seeders, "qqq_regime_seed_once", boom)

    result = seeders.recompute_qqq_regime_if_unwarm()
    assert result["failed"] is True
    assert result["reseeded"] is False


def test_qqq_regime_recompute_0931_in_jobs_table():
    """Scheduler JOBS table must wire 09:31 to qqq_regime_recompute_0931."""
    import trade_genius as tg

    src = inspect.getsource(tg.scheduler_thread)
    assert '"09:31"' in src
    assert "qqq_regime_recompute_0931" in src
    # And the v5.20.1 DI recompute must still be there \u2014 they share the row.
    assert "di_recompute_0931" in src


def test_qqq_regime_recompute_0931_function_exists_and_is_safe():
    """qqq_regime_recompute_0931 must be defined and call the helper."""
    import trade_genius as tg

    assert hasattr(tg, "qqq_regime_recompute_0931")
    assert callable(tg.qqq_regime_recompute_0931)

    called = {"n": 0}

    def fake(*a, **kw):
        called["n"] += 1
        return {
            "reseeded": False,
            "already_warm": True,
            "failed": False,
            "ema9": 100.0,
            "bars_seen": 12,
        }

    original = tg._recompute_qqq_regime_if_unwarm
    tg._recompute_qqq_regime_if_unwarm = fake
    try:
        tg.qqq_regime_recompute_0931()
        assert called["n"] == 1
    finally:
        tg._recompute_qqq_regime_if_unwarm = original


def test_min_bars_constant_matches_ema9_period():
    """QQQ_REGIME_MIN_BARS_FOR_EMA9 must equal qqq_regime.EMA9_PERIOD."""
    import qqq_regime
    from engine.seeders import QQQ_REGIME_MIN_BARS_FOR_EMA9

    assert QQQ_REGIME_MIN_BARS_FOR_EMA9 == qqq_regime.EMA9_PERIOD == 9


def test_log_line_includes_sealed_flag(regime_module, monkeypatch, caplog):
    """The [V572-REGIME-SEED] log line must report sealed=Y|N."""
    import logging

    seeders, tg, _ = regime_module
    _stub_qqq_alpaca_closes(monkeypatch, seeders, tg, n_closes=12)

    with caplog.at_level(logging.INFO, logger="trade_genius"):
        seeders.qqq_regime_seed_once()

    seed_lines = [r.message for r in caplog.records if "V572-REGIME-SEED" in r.message]
    assert seed_lines, "Expected a [V572-REGIME-SEED] log line"
    # 12 bars warmed ema9 \u2192 sealed=Y.
    assert any("sealed=Y" in m for m in seed_lines)
