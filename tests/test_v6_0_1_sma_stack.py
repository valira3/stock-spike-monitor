# tests/test_v6_0_1_sma_stack.py
# v6.0.1 -- daily SMA stack computation. The frontend always rendered
# "data not available" because the backend stub returned None; this
# suite locks down the restored compute path so a regression cannot
# silently put the panel back to the placeholder state.
# No em-dashes in this file (constraint for .py test files).
from __future__ import annotations

import pytest

from engine.sma_stack import WINDOWS, compute_sma_stack


# ---------------------------------------------------------------------------
# 1. Too few closes -> None (frontend renders "data not available")
# ---------------------------------------------------------------------------
def test_returns_none_when_under_smallest_window():
    # Smallest window is 12. With 11 closes we cannot even compute SMA(12),
    # so the helper must return None.
    closes = [100.0 + i for i in range(11)]
    assert compute_sma_stack(closes) is None


def test_returns_none_for_empty_input():
    assert compute_sma_stack([]) is None


# ---------------------------------------------------------------------------
# 2. Shape: payload contains every key the frontend reads
# ---------------------------------------------------------------------------
def test_payload_shape_contains_all_dashboard_keys():
    closes = [float(100 + i) for i in range(210)]
    out = compute_sma_stack(closes)
    assert isinstance(out, dict)
    required_keys = {
        "daily_close",
        "smas",
        "deltas_abs",
        "deltas_pct",
        "above",
        "stack_classification",
        "stack_substate",
        "order_chips",
        "order_relations",
    }
    missing = required_keys - set(out.keys())
    assert not missing, "missing keys: " + repr(missing)


def test_smas_keys_match_canonical_windows():
    closes = [float(100 + i) for i in range(210)]
    out = compute_sma_stack(closes)
    assert set(out["smas"].keys()) == set(WINDOWS)
    assert set(out["deltas_abs"].keys()) == set(WINDOWS)
    assert set(out["deltas_pct"].keys()) == set(WINDOWS)
    assert set(out["above"].keys()) == set(WINDOWS)


# ---------------------------------------------------------------------------
# 3. Numerical correctness: SMA(12) on a known sequence
# ---------------------------------------------------------------------------
def test_sma_12_value_is_arithmetic_mean_of_last_12():
    # Closes 1..50, SMA(12) = mean(39..50) = (39+50)/2 = 44.5
    closes = [float(i) for i in range(1, 51)]
    out = compute_sma_stack(closes)
    assert out is not None
    assert out["smas"][12] == pytest.approx(44.5)
    assert out["daily_close"] == pytest.approx(50.0)


def test_only_smas_with_enough_data_are_populated():
    # 30 closes -> SMA(12) and SMA(22) defined; 55/100/200 must be None.
    closes = [float(i) for i in range(1, 31)]
    out = compute_sma_stack(closes)
    assert out is not None
    assert out["smas"][12] is not None
    assert out["smas"][22] is not None
    assert out["smas"][55] is None
    assert out["smas"][100] is None
    assert out["smas"][200] is None
    # Deltas / above must mirror the None pattern.
    assert out["above"][55] is None
    assert out["deltas_abs"][200] is None


# ---------------------------------------------------------------------------
# 4. Classification: bullish / bearish / mixed
# ---------------------------------------------------------------------------
def test_monotonic_uptrend_is_bullish_stack():
    # Strictly rising closes guarantee SMA(12) > SMA(22) > SMA(55) and
    # the daily close is above all three.
    closes = [float(i) for i in range(1, 211)]
    out = compute_sma_stack(closes)
    assert out is not None
    assert out["stack_classification"] == "bullish"
    assert out["stack_substate"] == "all_above"
    assert all(out["above"][w] for w in (12, 22, 55, 100, 200))


def test_monotonic_downtrend_is_bearish_stack():
    closes = [float(500 - i) for i in range(210)]
    out = compute_sma_stack(closes)
    assert out is not None
    assert out["stack_classification"] == "bearish"
    assert out["stack_substate"] == "all_below"
    assert not any(out["above"][w] for w in (12, 22, 55, 100, 200))


# ---------------------------------------------------------------------------
# 5. Order relations: adjacent pairs always reported
# ---------------------------------------------------------------------------
def test_order_relations_always_has_n_minus_1_entries():
    closes = [float(100 + i) for i in range(210)]
    out = compute_sma_stack(closes)
    assert out is not None
    assert len(out["order_relations"]) == len(WINDOWS) - 1
    # On the strict-uptrend payload every pair must read "left > right":
    # SMA(12) is computed from the highest 12 values, so it is the
    # largest of the SMAs.
    for rel in out["order_relations"]:
        assert rel["op"] == ">", "uptrend should give all-> ops, got " + repr(rel)


# ---------------------------------------------------------------------------
# 6. Caller wiring: snapshot helper consumes the engine output verbatim
# ---------------------------------------------------------------------------
def test_compute_sma_stack_safe_uses_injected_fetcher(monkeypatch):
    """``v5_13_2_snapshot._compute_sma_stack_safe`` must (a) call
    ``trade_genius._daily_closes_for_sma`` for the closes, (b) feed
    them through ``engine.sma_stack.compute_sma_stack``, and (c)
    return the resulting dict. We patch both ends so the test does
    not need real Alpaca credentials.
    """
    import v5_13_2_snapshot as snap

    fake_closes = [float(100 + i) for i in range(210)]

    # Inject the fetcher into trade_genius without importing it for real
    # (it pulls in the entire bot runtime). The snapshot helper imports
    # trade_genius lazily and reads _daily_closes_for_sma via getattr.
    import sys
    import types

    fake_tg = sys.modules.get("trade_genius")
    if fake_tg is None:
        fake_tg = types.ModuleType("trade_genius")
        sys.modules["trade_genius"] = fake_tg
    monkeypatch.setattr(
        fake_tg,
        "_daily_closes_for_sma",
        lambda ticker, needed=210: list(fake_closes),
        raising=False,
    )
    # Clear any prior cache so the test sees a fresh fetch.
    snap._DAILY_CLOSES_CACHE.clear()

    out = snap._compute_sma_stack_safe("AAPL")
    assert isinstance(out, dict)
    assert out["daily_close"] == pytest.approx(309.0)  # last value
    assert out["stack_classification"] == "bullish"


def test_compute_sma_stack_safe_returns_none_when_fetcher_missing(monkeypatch):
    import sys
    import types

    import v5_13_2_snapshot as snap

    # Replace trade_genius with a stub that has NO _daily_closes_for_sma.
    stub = types.ModuleType("trade_genius")
    monkeypatch.setitem(sys.modules, "trade_genius", stub)
    snap._DAILY_CLOSES_CACHE.clear()

    assert snap._compute_sma_stack_safe("AAPL") is None
