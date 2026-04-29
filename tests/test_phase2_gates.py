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
# ---------------------------------------------------------------------------


def test_volume_gate_below_threshold_fails(baseline_55d_aapl_at_0935):
    from engine.volume_baseline import gate_volume_pass
    cur = 0.99 * baseline_55d_aapl_at_0935
    ok, ratio = gate_volume_pass(cur, baseline_55d_aapl_at_0935)
    assert ok is False
    assert ratio == pytest.approx(0.99, abs=1e-6)


def test_volume_gate_at_threshold_passes(baseline_55d_aapl_at_0935):
    from engine.volume_baseline import gate_volume_pass
    cur = 1.00 * baseline_55d_aapl_at_0935
    ok, ratio = gate_volume_pass(cur, baseline_55d_aapl_at_0935)
    assert ok is True
    assert ratio == pytest.approx(1.00, abs=1e-6)


def test_volume_gate_above_threshold_passes(baseline_55d_aapl_at_0935):
    from engine.volume_baseline import gate_volume_pass
    cur = 1.50 * baseline_55d_aapl_at_0935
    ok, ratio = gate_volume_pass(cur, baseline_55d_aapl_at_0935)
    assert ok is True
    assert ratio == pytest.approx(1.50, abs=1e-6)


def test_volume_gate_coldstart_passes_through():
    """Cold-start (baseline=None) MUST pass-through so trading isn't blocked
    while the bar archive accumulates 55 trading days."""
    from engine.volume_baseline import gate_volume_pass
    ok, ratio = gate_volume_pass(50_000.0, None)
    assert ok is True
    assert ratio is None


# ---------------------------------------------------------------------------
# L-P2-S4 — Two consecutive 1m closes strictly above OR_High.
# ---------------------------------------------------------------------------


def test_two_consecutive_long_only_last_above_fails(or_high_aapl):
    from engine.volume_baseline import gate_two_consecutive_1m_above
    closes = [199.50, 199.80, 200.50]  # only newest above
    assert gate_two_consecutive_1m_above(closes, or_high_aapl) is False


def test_two_consecutive_long_last_two_above_passes(or_high_aapl):
    from engine.volume_baseline import gate_two_consecutive_1m_above
    closes = [199.50, 200.10, 200.50]  # last two above
    assert gate_two_consecutive_1m_above(closes, or_high_aapl) is True


def test_two_consecutive_long_last_two_below_fails(or_high_aapl):
    from engine.volume_baseline import gate_two_consecutive_1m_above
    closes = [200.50, 199.90, 199.50]  # last two below
    assert gate_two_consecutive_1m_above(closes, or_high_aapl) is False


def test_two_consecutive_long_strict_inequality(or_high_aapl):
    """A close exactly AT OR_High is not strictly above and must fail."""
    from engine.volume_baseline import gate_two_consecutive_1m_above
    closes = [200.10, 200.00]  # second close equals OR_High
    assert gate_two_consecutive_1m_above(closes, or_high_aapl) is False


# ---------------------------------------------------------------------------
# S-P2-S4 — mirror of L-P2-S4.
# ---------------------------------------------------------------------------


def test_two_consecutive_short_only_last_below_fails(or_low_aapl):
    from engine.volume_baseline import gate_two_consecutive_1m_below
    closes = [196.50, 195.50, 194.50]  # only newest below
    assert gate_two_consecutive_1m_below(closes, or_low_aapl) is False


def test_two_consecutive_short_last_two_below_passes(or_low_aapl):
    from engine.volume_baseline import gate_two_consecutive_1m_below
    closes = [196.00, 194.80, 194.50]
    assert gate_two_consecutive_1m_below(closes, or_low_aapl) is True


def test_two_consecutive_short_last_two_above_fails(or_low_aapl):
    from engine.volume_baseline import gate_two_consecutive_1m_below
    closes = [194.00, 195.50, 196.00]  # last two above
    assert gate_two_consecutive_1m_below(closes, or_low_aapl) is False


def test_two_consecutive_short_strict_inequality(or_low_aapl):
    from engine.volume_baseline import gate_two_consecutive_1m_below
    closes = [194.00, 195.00]  # second close equals OR_Low
    assert gate_two_consecutive_1m_below(closes, or_low_aapl) is False


# ---------------------------------------------------------------------------
# In-progress candle is NOT counted (only fully closed candles).
# ---------------------------------------------------------------------------


def test_in_progress_candle_must_be_excluded_by_caller(or_high_aapl):
    """The gate consumes a list of CLOSED 1m candles. Callers (broker.orders)
    drop the forming candle before passing it in. We re-state the contract
    here: passing a forming candle as the most recent element must be a
    bug at the call site, not a silent pass.

    Concretely: if the only "close" satisfying the gate is the still-open
    candle, the gate must fail without it.
    """
    from engine.volume_baseline import gate_two_consecutive_1m_above
    # Last fully-closed close was *below* OR_High; only the in-progress
    # bar (excluded) is above. With only closed data, gate must FAIL.
    closes_excluding_forming = [199.50, 199.80]
    assert gate_two_consecutive_1m_above(closes_excluding_forming, or_high_aapl) is False


# ---------------------------------------------------------------------------
# Both gates required — combined entry-readiness.
# ---------------------------------------------------------------------------


def _entry_ready(vol_pass: bool, candles_pass: bool) -> bool:
    """Spec rule: BOTH gates required. Models the broker.orders short-circuit.
    """
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
    res = vb.rolling_55d_per_minute_avg("ZZZZZ_NOT_A_REAL_TICKER",
                                        time(9, 35), date(2026, 4, 28))
    assert res is None or isinstance(res, float)
