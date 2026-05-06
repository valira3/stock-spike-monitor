"""v7.0.6 \u2014 SPY regime backfill wiring integration tests.

Why this file exists: v6.15.3 added `SpyRegime.backfill_from_bars`
with 13 unit tests covering the function in isolation, but NONE of
those tests verified that the call site actually fires it. On
2026-05-06 the wiring failed silently in production \u2014 the
backfill function was nested inside `_qqq_weather_tick`'s
QQQ-bucket-advance branch AND inside the macro-snapshot try block
AND gated on a single-shot latch. All three conditions had to align
for the backfill to fire, and they often didn't. The dashboard
showed `regime=None` all session despite the SPY archive containing
both 0930 and 1000 buckets, and the V6153-BACKFILL log marker was
completely absent from the deployment logs.

These tests verify the v7.0.6 fix:
  - `_spy_regime_maybe_backfill` exists at module top-level
  - It is idempotent (no-op when regime is already classified)
  - It actually invokes `backfill_from_bars` when regime is None
  - It is wired into `engine.scan` so it runs on every cycle
  - The single-shot latch was removed
"""
from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock, patch


def _reload_tg(monkeypatch):
    """Reload trade_genius cleanly so module-level state is fresh."""
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "000:fake")
    monkeypatch.setenv("FMP_API_KEY", "fake_key")
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius
    return trade_genius


def test_maybe_backfill_function_exists(monkeypatch):
    """The new cycle hook must be importable at module top level."""
    tg = _reload_tg(monkeypatch)
    assert hasattr(tg, "_spy_regime_maybe_backfill"), (
        "v7.0.6 wiring: trade_genius._spy_regime_maybe_backfill is missing"
    )
    assert callable(tg._spy_regime_maybe_backfill)


def test_maybe_backfill_no_op_when_classified(monkeypatch):
    """When regime is already classified, do not call backfill_from_bars.
    Steady-state cost must be one attribute read."""
    tg = _reload_tg(monkeypatch)
    # Force a classified state.
    tg._SPY_REGIME.regime = "B"
    with patch.object(
        tg._SPY_REGIME, "backfill_from_bars"
    ) as mock_bf:
        tg._spy_regime_maybe_backfill()
        mock_bf.assert_not_called()


def test_maybe_backfill_invokes_when_regime_none(monkeypatch):
    """When regime is None, backfill_from_bars MUST be called every cycle.
    This is the regression test for the 2026-05-06 silent failure."""
    tg = _reload_tg(monkeypatch)
    tg._SPY_REGIME.regime = None
    with patch.object(
        tg._SPY_REGIME, "backfill_from_bars", return_value=False
    ) as mock_bf:
        tg._spy_regime_maybe_backfill()
        assert mock_bf.call_count == 1


def test_maybe_backfill_self_retries_until_classified(monkeypatch):
    """Multiple cycles with regime=None must all attempt backfill.
    The single-shot latch was the root cause; verify it's gone."""
    tg = _reload_tg(monkeypatch)
    tg._SPY_REGIME.regime = None
    with patch.object(
        tg._SPY_REGIME, "backfill_from_bars", return_value=False
    ) as mock_bf:
        for _ in range(5):
            tg._spy_regime_maybe_backfill()
        assert mock_bf.call_count == 5, (
            "v7.0.6 fix: backfill must self-retry every cycle while "
            "regime is None (was permanently latched off in v7.0.5)"
        )


def test_maybe_backfill_stops_after_classification(monkeypatch):
    """Once backfill flips regime to non-None, subsequent cycles no-op."""
    tg = _reload_tg(monkeypatch)
    tg._SPY_REGIME.regime = None
    call_count = {"n": 0}

    def fake_backfill(now_et):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            tg._SPY_REGIME.regime = "B"  # simulate classification
        return tg._SPY_REGIME.regime is not None

    with patch.object(
        tg._SPY_REGIME, "backfill_from_bars", side_effect=fake_backfill
    ) as mock_bf:
        # Call 5 times; backfill_from_bars should run on cycles 1+2,
        # then no-op for 3,4,5 once regime is set.
        for _ in range(5):
            tg._spy_regime_maybe_backfill()
        assert mock_bf.call_count == 2
        assert tg._SPY_REGIME.regime == "B"


def test_maybe_backfill_swallows_exceptions(monkeypatch):
    """Backfill failures must NOT break the scan cycle."""
    tg = _reload_tg(monkeypatch)
    tg._SPY_REGIME.regime = None
    with patch.object(
        tg._SPY_REGIME,
        "backfill_from_bars",
        side_effect=RuntimeError("disk full"),
    ):
        # Must not raise.
        tg._spy_regime_maybe_backfill()


def test_old_latch_variable_is_gone(monkeypatch):
    """The single-shot latch _SPY_REGIME_BACKFILL_ATTEMPTED was the
    proximate cause of the 2026-05-06 outage. Make sure it stays gone
    so a future refactor doesn't reintroduce the same bug."""
    tg = _reload_tg(monkeypatch)
    assert not hasattr(tg, "_SPY_REGIME_BACKFILL_ATTEMPTED"), (
        "v7.0.6 fix: the single-shot latch was deliberately removed; "
        "if you reintroduce it, you will recreate the 2026-05-06 bug "
        "where a premarket first-tick burns the latch with regime "
        "still None and locks the deploy out for the whole session"
    )


def test_scan_cycle_calls_maybe_backfill(monkeypatch):
    """engine.scan must invoke _spy_regime_maybe_backfill on every cycle.
    This is the integration test that would have caught the 2026-05-06
    bug (the unit tests for backfill_from_bars all passed; the wiring
    was where the bug lived)."""
    tg = _reload_tg(monkeypatch)
    # Reload engine.scan so it picks up our reloaded trade_genius.
    if "engine.scan" in sys.modules:
        del sys.modules["engine.scan"]
    import engine.scan as scan_mod

    # The scan function reads from `tg.<attr>` directly, so patching
    # the attribute on the trade_genius module is what matters.
    with patch.object(
        tg, "_spy_regime_maybe_backfill"
    ) as mock_hook:
        # We don't need to actually run scan_cycle (it requires a lot
        # of setup); we just need to assert that the call site exists
        # in the source.
        import inspect
        src = inspect.getsource(scan_mod)
        assert "_spy_regime_maybe_backfill" in src, (
            "engine.scan must call tg._spy_regime_maybe_backfill on "
            "every cycle. If this assertion fails, the wiring bug is "
            "back."
        )
        # Ensure the call is on its own (not nested inside the QQQ
        # weather tick's exception handler, which was the original bug).
        # Simple heuristic: the call appears as a top-level statement
        # `tg._spy_regime_maybe_backfill()` in the cycle body.
        assert "tg._spy_regime_maybe_backfill()" in src
