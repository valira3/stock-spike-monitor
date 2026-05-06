"""tests/test_v7_0_7_spy_regime_tick_wiring.py \u2014 wiring tests for the
v7.0.7 SPY regime tick decoupling.

Background:
- Prior to v7.0.7, ``_SPY_REGIME.tick(now_et, spy_price)`` was called
  ONLY inside ``_qqq_weather_tick``'s QQQ-bucket-advance branch (nested
  inside a forensic_capture try/except), which fires only when a fresh
  5m QQQ bucket closes and a new one starts.
- In production this worked because pre-market QQQ buckets stream via
  websocket, so by 09:30 ET the QQQ 5m bucket roll fires and pulls
  the SPY tick along with it (capturing ``spy_open_930``).
- In backtest replay the canonical archive is RTH-only (390 bars,
  09:30 to 16:00). At 09:30 the first 5m QQQ bucket is still forming
  and ``compute_5m_ohlc_and_ema9`` returns None (it drops the newest
  forming bucket and needs >=2 closed buckets). So the QQQ-roll path's
  first tick lands at 09:35 \u2014 too late to capture the 09:30 anchor.
- Additionally, ``engine.scan.scan_loop`` early-returns before 09:35
  ET via the ``before_open`` guard, so even after the QQQ-roll
  decoupling, the cycle hooks placed AFTER the early return never
  fire during the 09:30 capture minute.

The v7.0.7 fix:
1. Extract ``_spy_regime_maybe_tick()`` as a standalone module-level
   function in trade_genius (mirrors v7.0.6's _spy_regime_maybe_backfill).
2. Wire it from engine.scan.scan_loop in TWO places:
   a) Inside the pre-open branch BEFORE its early return, so the
      09:30 RTH-archive scan tick still calls it.
   b) After the post-RTH guards, so RTH cycles also tick it (no-op
      after first capture, idempotent).
3. The function is fail-closed and idempotent: returns immediately
   if regime is already classified, swallows fetch/tick exceptions.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# Hermetic env so trade_genius imports cleanly in tests.
os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:fake")
os.environ.setdefault("FMP_API_KEY", "test_dummy")
os.environ.setdefault("LOG_LEVEL", "WARNING")


@pytest.fixture()
def tg(monkeypatch, tmp_path):
    monkeypatch.setenv("TG_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("BAR_ARCHIVE_BASE", str(tmp_path / "bars"))
    if "trade_genius" in sys.modules:
        importlib.reload(sys.modules["trade_genius"])
    import trade_genius as _tg
    return _tg


def test_maybe_tick_function_exists(tg):
    """Module-level ``_spy_regime_maybe_tick`` must be defined."""
    assert callable(getattr(tg, "_spy_regime_maybe_tick", None)), (
        "trade_genius._spy_regime_maybe_tick must exist as a module-level "
        "callable so engine.scan.scan_loop can wire it as a per-cycle hook."
    )


def test_maybe_tick_self_skips_when_classified(tg):
    """When regime is already classified, the hook must be a near-no-op
    \u2014 specifically it must NOT call fetch_1min_bars or _SPY_REGIME.tick.
    """
    tg._SPY_REGIME.regime = "A"  # pretend already classified
    tg._SPY_REGIME.spy_open_930 = 100.0
    tg._SPY_REGIME.spy_close_1000 = 100.0

    fetch_calls = []
    tick_calls = []
    orig_fetch = tg.fetch_1min_bars
    orig_tick = tg._SPY_REGIME.tick

    def trace_fetch(t):
        fetch_calls.append(t)
        return orig_fetch(t)

    def trace_tick(now_et, price):
        tick_calls.append((now_et, price))
        return orig_tick(now_et, price)

    tg.fetch_1min_bars = trace_fetch
    tg._SPY_REGIME.tick = trace_tick

    try:
        tg._spy_regime_maybe_tick()
    finally:
        tg.fetch_1min_bars = orig_fetch
        tg._SPY_REGIME.tick = orig_tick
        tg._SPY_REGIME.daily_reset()

    assert fetch_calls == [], (
        "_spy_regime_maybe_tick must self-skip when regime is set; "
        "it called fetch_1min_bars %r" % (fetch_calls,)
    )
    assert tick_calls == [], (
        "_spy_regime_maybe_tick must self-skip when regime is set; "
        "it called _SPY_REGIME.tick %r" % (tick_calls,)
    )


def test_maybe_tick_swallows_exceptions(tg, monkeypatch):
    """Any exception inside the hook must be logged and swallowed so a
    transient SPY fetch error can't crash the scan cycle.
    """
    tg._SPY_REGIME.daily_reset()
    monkeypatch.setattr(tg, "fetch_1min_bars",
                        lambda t: (_ for _ in ()).throw(RuntimeError("boom")))
    # Must not raise.
    tg._spy_regime_maybe_tick()


def test_maybe_tick_handles_missing_bars(tg, monkeypatch):
    """If fetch_1min_bars returns None / {} / a dict with no
    current_price, the hook must gracefully no-op without calling
    tick().
    """
    tg._SPY_REGIME.daily_reset()
    tick_calls = []
    orig_tick = tg._SPY_REGIME.tick
    tg._SPY_REGIME.tick = lambda *a, **k: tick_calls.append(a)

    try:
        for fake in (None, {}, {"current_price": None}):
            monkeypatch.setattr(tg, "fetch_1min_bars", lambda t, _f=fake: _f)
            tg._spy_regime_maybe_tick()
    finally:
        tg._SPY_REGIME.tick = orig_tick

    assert tick_calls == [], (
        "_spy_regime_maybe_tick must not call tick() when SPY bars are "
        "missing or have no current_price; saw %r" % (tick_calls,)
    )


def test_scan_loop_calls_maybe_tick_in_preopen_branch():
    """``engine.scan.scan_loop`` must call ``tg._spy_regime_maybe_tick``
    inside the pre-open branch BEFORE its early return. Otherwise the
    09:30 anchor capture window passes without the hook ever firing
    during a backtest replay started at 09:30 ET.
    """
    src = (_REPO / "engine" / "scan.py").read_text(encoding="utf-8")
    # The new pre-open hook must appear before the "if before_open or
    # after_close: return" guard.
    pre_idx = src.find("[scan] preopen cycle hook error")
    rth_guard_idx = src.find("if before_open or after_close:\n        return")
    tick_idx = src.find("tg._spy_regime_maybe_tick()")
    assert pre_idx > 0, "preopen branch marker missing in engine/scan.py"
    assert rth_guard_idx > 0, "RTH early-return guard missing in engine/scan.py"
    assert tick_idx > 0, "tg._spy_regime_maybe_tick() call missing in engine/scan.py"
    # The first occurrence of the tick call must come BEFORE the RTH
    # guard (i.e. inside the pre-open branch).
    assert tick_idx < rth_guard_idx, (
        "tg._spy_regime_maybe_tick() must be wired inside the pre-open "
        "branch before the 'if before_open or after_close: return' guard "
        "so the 09:30 capture minute actually fires the hook."
    )


def test_scan_loop_also_calls_maybe_tick_after_rth_guards():
    """A second call to ``tg._spy_regime_maybe_tick()`` must exist
    after the RTH guards so that in steady-state RTH the hook also
    fires (the pre-open path returns at 09:35 ET, so RTH cycles need
    their own wire-in too).
    """
    src = (_REPO / "engine" / "scan.py").read_text(encoding="utf-8")
    n_calls = src.count("tg._spy_regime_maybe_tick()")
    assert n_calls >= 2, (
        "Expected >=2 calls to tg._spy_regime_maybe_tick() in "
        "engine/scan.py (one inside the pre-open branch, one after "
        "the RTH guards). Found %d." % n_calls
    )


def test_scan_loop_also_calls_maybe_backfill_in_preopen_branch():
    """Symmetric guard: the v7.0.6 backfill hook must also be wired
    into the pre-open branch so a mid-session restart at 09:32 (e.g.,
    Railway redeploy) can recover the 09:30 anchor on the next scan.
    Without this, the backfill hook only fires after 09:35 ET.
    """
    src = (_REPO / "engine" / "scan.py").read_text(encoding="utf-8")
    n_calls = src.count("tg._spy_regime_maybe_backfill()")
    assert n_calls >= 2, (
        "Expected >=2 calls to tg._spy_regime_maybe_backfill() in "
        "engine/scan.py (one inside the pre-open branch, one after "
        "the RTH guards). Found %d." % n_calls
    )


def test_replay_default_start_is_0930():
    """``backtest.replay_v511_full.run_replay``'s default ``start_hhmm``
    must be (9, 30) so the live SPY regime tick path (which gates on
    ``hh==9 mm==30``) can actually fire during replay.
    """
    src = (_REPO / "backtest" / "replay_v511_full.py").read_text(encoding="utf-8")
    # The default arg in the run_replay signature.
    assert "start_hhmm: tuple[int, int] = (9, 30)" in src, (
        "backtest.replay_v511_full.run_replay default start_hhmm must "
        "be (9, 30); the v7.0.7 fix moved it from (9, 35) so live "
        "tick() path can capture spy_open_930."
    )


def test_canonical_backfill_tool_exists():
    """The one-shot et_bucket backfill tool must be present so the
    v7.0.6 backfill path also works in replay (in case of mid-session
    restart and the live tick missed the 09:30 capture window).
    """
    p = _REPO / "tools" / "backfill_canonical_et_bucket.py"
    assert p.exists(), (
        "tools/backfill_canonical_et_bucket.py must exist; without "
        "et_bucket strings on the canonical archive the v7.0.6 backfill "
        "path returns False during replay."
    )
    src = p.read_text(encoding="utf-8")
    assert "_compute_et_bucket" in src, (
        "backfill tool must reuse ingest.algo_plus._compute_et_bucket "
        "so backfilled buckets match production semantics exactly."
    )
