"""tests/test_phase2_gates.py — v5.13.0 PR 4.

Unit tests for the Tiger Sovereign Phase 2 entry gates:

* L-P2-S3 / S-P2-S3: 100% of 55-day rolling per-minute volume baseline
* L-P2-S4 / S-P2-S4: TWO consecutive 1m candles strictly outside the OR

Both gates must PASS for the entry decision to proceed; either failing
keeps the FSM in Phase 2.

Tests run in isolation against the pure-function gate primitives in
``engine.volume_baseline``. The full ``check_breakout`` pipeline is exercised
indirectly via ``tests/test_tiger_sovereign_spec.py`` source-grep tests.
"""

from __future__ import annotations

from datetime import date, time

import pytest


# ---------------------------------------------------------------------------
# Fixtures — fake 55-day baseline so tests are deterministic.
# ---------------------------------------------------------------------------


@pytest.fixture
def baseline_55d_aapl_at_0935():
    """Pretend the 55-day rolling per-minute baseline for AAPL at 09:35 ET
    is exactly 100,000 shares. Multiplying current_volume by the desired
    ratio gives us deterministic gate inputs.
    """
    return 100_000.0


@pytest.fixture
def or_high_aapl():
    return 200.00


@pytest.fixture
def or_low_aapl():
    return 195.00


# ---------------------------------------------------------------------------
# L-P2-S3 / S-P2-S3 — Volume gate.
#
# v5.13.1 — gate is flag-controlled via ``feature_flags.VOLUME_GATE_ENABLED``.
# Production default is False (gate DISABLED, auto-pass). The spec-strict
# threshold tests below pin the flag ON via monkeypatch so the original
# v5.13.0 contract still has explicit coverage.
# ---------------------------------------------------------------------------


@pytest.fixture
def volume_gate_on(monkeypatch):
    """Monkeypatch the runtime flag ON so spec-strict assertions exercise
    the threshold logic instead of the DISABLED_BY_FLAG short-circuit."""
    from engine import feature_flags as ff

    monkeypatch.setattr(ff, "VOLUME_GATE_ENABLED", True)
    return ff


@pytest.fixture
def volume_gate_off(monkeypatch):
    """Monkeypatch the runtime flag OFF (production default, made explicit)."""
    from engine import feature_flags as ff

    monkeypatch.setattr(ff, "VOLUME_GATE_ENABLED", False)
    return ff


def test_volume_gate_below_threshold_fails(volume_gate_on, baseline_55d_aapl_at_0935):
    from engine.volume_baseline import gate_volume_pass

    cur = 0.99 * baseline_55d_aapl_at_0935
    ok, ratio = gate_volume_pass(cur, baseline_55d_aapl_at_0935)
    assert ok is False
    assert ratio == pytest.approx(0.99, abs=1e-6)


def test_volume_gate_at_threshold_passes(volume_gate_on, baseline_55d_aapl_at_0935):
    from engine.volume_baseline import gate_volume_pass

    cur = 1.00 * baseline_55d_aapl_at_0935
    ok, ratio = gate_volume_pass(cur, baseline_55d_aapl_at_0935)
    assert ok is True
    assert ratio == pytest.approx(1.00, abs=1e-6)


def test_volume_gate_above_threshold_passes(volume_gate_on, baseline_55d_aapl_at_0935):
    from engine.volume_baseline import gate_volume_pass

    cur = 1.50 * baseline_55d_aapl_at_0935
    ok, ratio = gate_volume_pass(cur, baseline_55d_aapl_at_0935)
    assert ok is True
    assert ratio == pytest.approx(1.50, abs=1e-6)


def test_volume_gate_coldstart_passes_through(volume_gate_on):
    """Cold-start (baseline=None) MUST pass-through so trading isn't blocked
    while the bar archive accumulates 55 trading days."""
    from engine.volume_baseline import gate_volume_pass

    ok, ratio = gate_volume_pass(50_000.0, None)
    assert ok is True
    assert ratio is None


# ---------------------------------------------------------------------------
# v5.13.1 — runtime flag default OFF (DISABLED_BY_FLAG path).
# ---------------------------------------------------------------------------


def test_volume_gate_disabled_by_default_auto_passes_below_threshold(
    volume_gate_off, baseline_55d_aapl_at_0935
):
    """With VOLUME_GATE_ENABLED=False (production default), the volume gate
    auto-passes regardless of how far below 100% threshold the current
    minute's volume sits."""
    from engine.volume_baseline import gate_volume_pass

    cur = 0.05 * baseline_55d_aapl_at_0935
    ok, ratio = gate_volume_pass(cur, baseline_55d_aapl_at_0935)
    assert ok is True
    assert ratio is None


def test_volume_gate_disabled_by_default_auto_passes_zero_volume(
    volume_gate_off, baseline_55d_aapl_at_0935
):
    """Even literal zero current volume must auto-pass when the flag is OFF."""
    from engine.volume_baseline import gate_volume_pass

    ok, ratio = gate_volume_pass(0.0, baseline_55d_aapl_at_0935)
    assert ok is True
    assert ratio is None


def test_volume_gate_default_module_constant_is_false():
    """Pin the production default — when the env var is unset at import
    time, the constant must resolve to False."""
    from engine import feature_flags as ff
    import os

    if "VOLUME_GATE_ENABLED" not in os.environ:
        assert ff.VOLUME_GATE_ENABLED is False


def test_two_consecutive_gate_unaffected_by_volume_flag_off(volume_gate_off, or_high_aapl):
    """The 2-consecutive-1m candle gate (L-P2-S4 / S-P2-S4) remains fully
    enforced when the volume flag is OFF — only the volume gate is
    short-circuited. v5.13.9: the L-P2-S4 contract is owned by
    eye_of_tiger.evaluate_boundary_hold; this test pins the same
    behavior against that surface."""
    import eye_of_tiger as eot

    or_low_aapl_local = 199.0  # opposite edge stub for the helper signature
    closes_only_one_above = [199.50, 199.80, 200.50]
    res = eot.evaluate_boundary_hold("LONG", or_high_aapl, or_low_aapl_local, closes_only_one_above)
    assert bool(res.get("hold")) is False


def test_eye_of_tiger_volume_bucket_disabled_by_flag(volume_gate_off):
    """The live caller (eye_of_tiger.evaluate_volume_bucket) must return
    True regardless of the bucket .check() result when the flag is OFF."""
    import eye_of_tiger

    fail_result = {"gate": "FAIL", "ratio": 0.42, "days_available": 55}
    assert eye_of_tiger.evaluate_volume_bucket(fail_result) is True
    assert eye_of_tiger.evaluate_volume_bucket(None) is True


def test_eye_of_tiger_volume_bucket_enforced_when_flag_on(volume_gate_on):
    """Mirror: when the flag is ON, the bucket result is honored."""
    import eye_of_tiger

    assert (
        eye_of_tiger.evaluate_volume_bucket({"gate": "FAIL", "ratio": 0.42, "days_available": 55})
        is False
    )
    assert (
        eye_of_tiger.evaluate_volume_bucket({"gate": "PASS", "ratio": 1.20, "days_available": 55})
        is True
    )
    assert (
        eye_of_tiger.evaluate_volume_bucket(
            {"gate": "COLDSTART", "ratio": None, "days_available": 12}
        )
        is True
    )
    assert eye_of_tiger.evaluate_volume_bucket(None) is False


# ---------------------------------------------------------------------------
# L-P2-S4 — Two consecutive 1m closes strictly above OR_High.
# v5.13.9: the L-P2-S4 contract lives in eye_of_tiger.evaluate_boundary_hold.
# These tests pin the spec rule against the live evaluator that the entry
# path actually consumes.
# ---------------------------------------------------------------------------


def test_two_consecutive_long_only_last_above_fails(or_high_aapl, or_low_aapl):
    import eye_of_tiger as eot

    closes = [199.50, 199.80, 200.50]  # only newest above
    res = eot.evaluate_boundary_hold("LONG", or_high_aapl, or_low_aapl, closes)
    assert bool(res.get("hold")) is False


def test_two_consecutive_long_last_two_above_passes(or_high_aapl, or_low_aapl):
    import eye_of_tiger as eot

    closes = [199.50, 200.10, 200.50]  # last two above
    res = eot.evaluate_boundary_hold("LONG", or_high_aapl, or_low_aapl, closes)
    assert bool(res.get("hold")) is True


def test_two_consecutive_long_last_two_below_fails(or_high_aapl, or_low_aapl):
    import eye_of_tiger as eot

    closes = [200.50, 199.90, 199.50]  # last two below
    res = eot.evaluate_boundary_hold("LONG", or_high_aapl, or_low_aapl, closes)
    assert bool(res.get("hold")) is False


def test_two_consecutive_long_strict_inequality(or_high_aapl, or_low_aapl):
    """A close exactly AT OR_High is not strictly above and must fail."""
    import eye_of_tiger as eot

    closes = [200.10, 200.00]  # second close equals OR_High
    res = eot.evaluate_boundary_hold("LONG", or_high_aapl, or_low_aapl, closes)
    assert bool(res.get("hold")) is False


# ---------------------------------------------------------------------------
# S-P2-S4 — mirror of L-P2-S4.
# ---------------------------------------------------------------------------


def test_two_consecutive_short_only_last_below_fails(or_high_aapl, or_low_aapl):
    import eye_of_tiger as eot

    closes = [196.50, 195.50, 194.50]  # only newest below
    res = eot.evaluate_boundary_hold("SHORT", or_high_aapl, or_low_aapl, closes)
    assert bool(res.get("hold")) is False


def test_two_consecutive_short_last_two_below_passes(or_high_aapl, or_low_aapl):
    import eye_of_tiger as eot

    closes = [196.00, 194.80, 194.50]
    res = eot.evaluate_boundary_hold("SHORT", or_high_aapl, or_low_aapl, closes)
    assert bool(res.get("hold")) is True


def test_two_consecutive_short_last_two_above_fails(or_high_aapl, or_low_aapl):
    import eye_of_tiger as eot

    closes = [194.00, 195.50, 196.00]  # last two above
    res = eot.evaluate_boundary_hold("SHORT", or_high_aapl, or_low_aapl, closes)
    assert bool(res.get("hold")) is False


def test_two_consecutive_short_strict_inequality(or_high_aapl, or_low_aapl):
    import eye_of_tiger as eot

    closes = [194.00, 195.00]  # second close equals OR_Low
    res = eot.evaluate_boundary_hold("SHORT", or_high_aapl, or_low_aapl, closes)
    assert bool(res.get("hold")) is False


# ---------------------------------------------------------------------------
# In-progress candle is NOT counted (only fully closed candles).
# ---------------------------------------------------------------------------


def test_in_progress_candle_must_be_excluded_by_caller(or_high_aapl, or_low_aapl):
    """The gate consumes a list of CLOSED 1m candles. Callers (engine.scan)
    drop the forming candle before passing it in via record_1m_close.
    We re-state the contract here: passing a forming candle as the most
    recent element must be a bug at the call site, not a silent pass.

    Concretely: if the only "close" satisfying the gate is the still-open
    candle, the gate must fail without it.
    """
    import eye_of_tiger as eot

    # Last fully-closed close was *below* OR_High; only the in-progress
    # bar (excluded) is above. With only closed data, gate must FAIL.
    closes_excluding_forming = [199.50, 199.80]
    res = eot.evaluate_boundary_hold("LONG", or_high_aapl, or_low_aapl, closes_excluding_forming)
    assert bool(res.get("hold")) is False


# ---------------------------------------------------------------------------
# Both gates required — combined entry-readiness.
# ---------------------------------------------------------------------------


def _entry_ready(vol_pass: bool, candles_pass: bool) -> bool:
    """Spec rule: BOTH gates required. Models the broker.orders short-circuit."""
    return bool(vol_pass) and bool(candles_pass)


def test_combined_volume_fail_candles_pass_no_entry():
    assert _entry_ready(False, True) is False


def test_combined_volume_pass_candles_fail_no_entry():
    assert _entry_ready(True, False) is False


def test_combined_both_pass_entry_allowed():
    """With Phase 1 already satisfied (covered by separate tests), both
    Phase 2 gates passing means the entry is allowed to proceed.
    """
    assert _entry_ready(True, True) is True


def test_combined_both_fail_no_entry():
    assert _entry_ready(False, False) is False


# ---------------------------------------------------------------------------
# Public surface area: signature contract for the rolling baseline accessor.
# ---------------------------------------------------------------------------


def test_rolling_55d_per_minute_avg_signature():
    """The accessor required by the spec must accept (symbol, time, date)
    and return a float-or-None. We don't exercise the real archive here
    (that's covered by the volume_bucket integration tests); we just
    pin the contract.
    """
    from engine import volume_baseline as vb

    assert callable(vb.rolling_55d_per_minute_avg)
    res = vb.rolling_55d_per_minute_avg("ZZZZZ_NOT_A_REAL_TICKER", time(9, 35), date(2026, 4, 28))
    assert res is None or isinstance(res, float)
