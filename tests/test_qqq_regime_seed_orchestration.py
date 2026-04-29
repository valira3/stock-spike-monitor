"""v5.13.8 \u2014 unit tests for engine.seeders.qqq_regime_seed_once orchestration.

Pre-v5.13.8 the orchestration short-circuited on any non-empty archive
return, which on cold restarts stranded the regime with bars=1 and
ema9=None for the first ~25-45 minutes of the session. The fix
introduces MIN_ARCHIVE_BARS=9 and a fall-through to Alpaca when the
archive read is below threshold.

These tests cover the full fall-through matrix without touching the
filesystem or the network \u2014 we monkeypatch the three internal source
helpers and a stub `tg` module supplying `_QQQ_REGIME` / `_QQQ_REGIME_SEEDED`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import qqq_regime  # noqa: E402
from engine import seeders  # noqa: E402


def _fresh_tg_stub():
    """Build a minimal stub for the live trade_genius module."""
    return SimpleNamespace(
        _QQQ_REGIME=qqq_regime.QQQRegime(),
        _QQQ_REGIME_SEEDED=False,
    )


@pytest.fixture
def patched_seeders(monkeypatch):
    """Patch _tg() and the three source helpers; default to all empty."""
    stub = _fresh_tg_stub()
    monkeypatch.setattr(seeders, "_tg", lambda: stub)
    monkeypatch.setattr(seeders, "_qqq_seed_from_archive", lambda s, e: [])
    monkeypatch.setattr(seeders, "_qqq_seed_from_alpaca", lambda s, e: [])
    monkeypatch.setattr(seeders, "_qqq_seed_from_prior_session", lambda n: [])
    return stub


def test_full_archive_uses_archive_path(patched_seeders, monkeypatch):
    """Archive returning >=9 bars short-circuits as before (fast path)."""
    full = [200.0 + 0.1 * i for i in range(15)]
    monkeypatch.setattr(seeders, "_qqq_seed_from_archive", lambda s, e: full)

    seeders.qqq_regime_seed_once()

    qr = patched_seeders._QQQ_REGIME
    assert qr.seed_source == "archive"
    assert qr.seed_bar_count == 15
    assert qr.ema9 is not None
    assert patched_seeders._QQQ_REGIME_SEEDED is True


def test_sparse_archive_falls_through_to_alpaca(patched_seeders, monkeypatch):
    """v5.13.8 fix: archive with <9 bars must NOT short-circuit."""
    sparse = [200.0]  # 1 bar \u2014 what we saw in prod 2026-04-29
    alpaca_full = [199.5 + 0.05 * i for i in range(60)]  # ~60 pre-mkt bars
    monkeypatch.setattr(seeders, "_qqq_seed_from_archive", lambda s, e: sparse)
    monkeypatch.setattr(seeders, "_qqq_seed_from_alpaca", lambda s, e: alpaca_full)

    seeders.qqq_regime_seed_once()

    qr = patched_seeders._QQQ_REGIME
    assert qr.seed_source == "alpaca"
    assert qr.seed_bar_count == 60
    assert qr.ema9 is not None  # the whole point \u2014 ema9 defined at open


def test_threshold_boundary_8_bars_falls_through(patched_seeders, monkeypatch):
    """8 bars is below MIN_ARCHIVE_BARS=9 \u2014 must fall through."""
    eight = [200.0 + 0.1 * i for i in range(8)]
    alpaca_full = [199.0 + 0.05 * i for i in range(50)]
    monkeypatch.setattr(seeders, "_qqq_seed_from_archive", lambda s, e: eight)
    monkeypatch.setattr(seeders, "_qqq_seed_from_alpaca", lambda s, e: alpaca_full)

    seeders.qqq_regime_seed_once()

    assert patched_seeders._QQQ_REGIME.seed_source == "alpaca"


def test_threshold_boundary_9_bars_uses_archive(patched_seeders, monkeypatch):
    """Exactly 9 bars meets MIN_ARCHIVE_BARS \u2014 must use archive."""
    nine = [200.0 + 0.1 * i for i in range(9)]
    alpaca_full = [100.0] * 50  # different values so we can tell them apart
    monkeypatch.setattr(seeders, "_qqq_seed_from_archive", lambda s, e: nine)
    monkeypatch.setattr(seeders, "_qqq_seed_from_alpaca", lambda s, e: alpaca_full)

    seeders.qqq_regime_seed_once()

    qr = patched_seeders._QQQ_REGIME
    assert qr.seed_source == "archive"
    assert qr.seed_bar_count == 9


def test_sparse_archive_alpaca_empty_uses_prior_session(
    patched_seeders,
    monkeypatch,
):
    """Sparse archive + empty Alpaca should fall through to prior session."""
    sparse = [200.0, 200.1]
    prior = [195.0 + 0.1 * i for i in range(12)]
    monkeypatch.setattr(seeders, "_qqq_seed_from_archive", lambda s, e: sparse)
    monkeypatch.setattr(seeders, "_qqq_seed_from_alpaca", lambda s, e: [])
    monkeypatch.setattr(seeders, "_qqq_seed_from_prior_session", lambda n: prior)

    seeders.qqq_regime_seed_once()

    qr = patched_seeders._QQQ_REGIME
    assert qr.seed_source == "prior_session"
    assert qr.seed_bar_count == 12


def test_sparse_archive_all_fallbacks_empty_uses_archive_partial(
    patched_seeders,
    monkeypatch,
):
    """Final last-resort: use whatever the archive did give us, labeled
    archive_partial. Better than nothing \u2014 at least it tracks the live
    bars seen so far."""
    sparse = [200.0, 200.1, 200.2]
    monkeypatch.setattr(seeders, "_qqq_seed_from_archive", lambda s, e: sparse)
    monkeypatch.setattr(seeders, "_qqq_seed_from_alpaca", lambda s, e: [])
    monkeypatch.setattr(seeders, "_qqq_seed_from_prior_session", lambda n: [])

    seeders.qqq_regime_seed_once()

    qr = patched_seeders._QQQ_REGIME
    assert qr.seed_source == "archive_partial"
    assert qr.seed_bar_count == 3
    # ema9 will still be None (only 3 bars) but that's fine \u2014 caller
    # knows it's a partial; live ticks finish the warmup.
    assert qr.ema9 is None


def test_all_sources_empty_marks_seeded_no_crash(patched_seeders):
    """No bars from any source \u2014 must not crash, must mark seeded so
    we don't retry every tick."""
    seeders.qqq_regime_seed_once()
    qr = patched_seeders._QQQ_REGIME
    assert patched_seeders._QQQ_REGIME_SEEDED is True
    assert qr.seed_source is None  # never assigned
    assert qr.bars_seen == 0


def test_idempotent_already_seeded_returns_immediately(
    patched_seeders,
    monkeypatch,
):
    """Calling twice must be a no-op; second call should not even invoke
    the source helpers."""
    full = [200.0 + 0.1 * i for i in range(12)]
    monkeypatch.setattr(seeders, "_qqq_seed_from_archive", lambda s, e: full)
    seeders.qqq_regime_seed_once()
    assert patched_seeders._QQQ_REGIME.seed_bar_count == 12

    # Replace archive with one that would trip a different path; if the
    # second call ran the orchestration we'd see the seed_source change.
    monkeypatch.setattr(
        seeders,
        "_qqq_seed_from_archive",
        lambda s, e: pytest.fail("archive helper called on idempotent re-run"),
    )
    seeders.qqq_regime_seed_once()
    assert patched_seeders._QQQ_REGIME.seed_source == "archive"


def test_fall_through_logs_threshold_message(
    patched_seeders,
    monkeypatch,
    caplog,
):
    """The info-level fall-through log helps prod forensics."""
    import logging

    sparse = [200.0]
    alpaca_full = [199.0] * 12
    monkeypatch.setattr(seeders, "_qqq_seed_from_archive", lambda s, e: sparse)
    monkeypatch.setattr(seeders, "_qqq_seed_from_alpaca", lambda s, e: alpaca_full)

    with caplog.at_level(logging.INFO, logger="trade_genius"):
        seeders.qqq_regime_seed_once()

    fall_through_msgs = [
        rec.message
        for rec in caplog.records
        if "falling through to Alpaca historical" in rec.message
    ]
    assert len(fall_through_msgs) == 1
    assert "1 bars" in fall_through_msgs[0]
    assert "9 minimum" in fall_through_msgs[0]
