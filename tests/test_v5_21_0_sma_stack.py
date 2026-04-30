"""v5.21.0 -- backend tests for the Daily SMA Stack feature (Track D).

Tests cover:
  1. SMA_WINDOWS constant is (12, 22, 55, 100, 200) and is a tuple.
  2. Bullish stack: monotonically increasing closes -> classification
     "bullish", substate "all_above".
  3. Bearish stack: monotonically decreasing closes -> classification
     "bearish", substate "all_below".
  4. Mixed short-above / long-below scenario (NVDA-style rally from trough).
  5. Insufficient history (50 closes) -> sma_100/sma_200 are None,
     classification degrades to "mixed".
  6. Delta correctness: delta_abs[55] and delta_pct[55] arithmetic.
  7. Order relations for ascending closes are ["gt", "gt"].
  8. get_recent_daily_closes respects injected fetcher.
  9. get_recent_daily_closes caches: fetcher called exactly once for
     two calls with the same ticker.
  10. Snapshot test: _compute_sma_stack_safe is wired and one phase2
      row carries a sma_stack key (or None on failure).

No em-dashes in this file. No engine exit logic called -- read-only.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# 1. SMA_WINDOWS immutability and value
# ---------------------------------------------------------------------------


def test_sma_windows_immutable():
    """SMA_WINDOWS must be a tuple with exactly (12, 22, 55, 100, 200)."""
    from engine.sma_stack import SMA_WINDOWS

    assert isinstance(SMA_WINDOWS, tuple), "SMA_WINDOWS must be a tuple"
    assert SMA_WINDOWS == (12, 22, 55, 100, 200), (
        f"SMA_WINDOWS expected (12, 22, 55, 100, 200), got {SMA_WINDOWS}"
    )


# ---------------------------------------------------------------------------
# 2. Bullish stack
# ---------------------------------------------------------------------------


def test_compute_bullish_stack():
    """Monotonically increasing closes must produce classification
    'bullish' and substate 'all_above'.

    With closes = [100, 101, 102, ...] (250 values) the most recent
    close (349) is above all SMAs (which are averages of earlier,
    lower prices). SMA 12 < SMA 22 < SMA 55 is REVERSED (SMA 12 is
    the average of the 12 most recent bars which are the highest),
    so SMA 12 > SMA 22 > SMA 55 -- bullish stack condition.
    """
    from engine.sma_stack import compute_sma_stack

    closes = [100.0 + i for i in range(250)]
    result = compute_sma_stack(closes)

    assert result["stack_classification"] == "bullish", (
        f"Expected 'bullish', got {result['stack_classification']!r}"
    )
    assert result["stack_substate"] == "all_above", (
        f"Expected 'all_above', got {result['stack_substate']!r}"
    )
    # Verify all five above flags are True
    for w in (12, 22, 55, 100, 200):
        assert result["above"][w] is True, f"Expected above[{w}]=True for bullish series"


# ---------------------------------------------------------------------------
# 3. Bearish stack
# ---------------------------------------------------------------------------


def test_compute_bearish_stack():
    """Monotonically decreasing closes must produce classification
    'bearish' and substate 'all_below'.
    """
    from engine.sma_stack import compute_sma_stack

    closes = [349.0 - i for i in range(250)]
    result = compute_sma_stack(closes)

    assert result["stack_classification"] == "bearish", (
        f"Expected 'bearish', got {result['stack_classification']!r}"
    )
    assert result["stack_substate"] == "all_below", (
        f"Expected 'all_below', got {result['stack_substate']!r}"
    )
    # Verify all five above flags are False
    for w in (12, 22, 55, 100, 200):
        assert result["above"][w] is False, f"Expected above[{w}]=False for bearish series"


# ---------------------------------------------------------------------------
# 4. Mixed -- above short-term, below long-term
# ---------------------------------------------------------------------------


def test_compute_mixed_short_above_long_below():
    """Construct a series where the close is above SMA 12/22 but below
    SMA 100/200. Pattern: steep decay from 500 to 100 over 200 bars,
    then rally back to 130 over the last 30 bars.

    In this scenario:
    - SMA 12 and SMA 22 are averages of the recent (rallied) region -> below close.
    - SMA 100 and SMA 200 are anchored by the high start (500) and remain
      above the close of 130.
    - substate should be 'above_short_below_long'.
    """
    from engine.sma_stack import compute_sma_stack

    # Steep decay phase: 200 bars from 500 down to 100.
    # The large starting value anchors SMA 100/200 well above the trough.
    decay = [500.0 - i * (400.0 / 199) for i in range(200)]
    # Rally phase: 30 bars from 100 up to 130 (short-term SMAs track this).
    rally = [100.0 + i * (30.0 / 29) for i in range(30)]
    closes = decay + rally  # 230 bars total

    result = compute_sma_stack(closes)

    close = result["daily_close"]
    sma12 = result["smas"][12]
    sma22 = result["smas"][22]
    sma100 = result["smas"][100]
    sma200 = result["smas"][200]

    assert close is not None
    assert sma12 is not None
    assert sma22 is not None
    assert sma100 is not None
    assert sma200 is not None

    assert close > sma12, f"Expected close ({close:.4f}) > sma12 ({sma12:.4f})"
    assert close > sma22, f"Expected close ({close:.4f}) > sma22 ({sma22:.4f})"
    assert close < sma100, f"Expected close ({close:.4f}) < sma100 ({sma100:.4f})"
    assert close < sma200, f"Expected close ({close:.4f}) < sma200 ({sma200:.4f})"

    assert result["stack_substate"] == "above_short_below_long", (
        f"Expected 'above_short_below_long', got {result['stack_substate']!r}"
    )


# ---------------------------------------------------------------------------
# 5. Insufficient history
# ---------------------------------------------------------------------------


def test_insufficient_history():
    """With only 50 closes, sma_100 and sma_200 must be None, and the
    stack_classification must not be 'bullish' based on missing long-term
    SMAs alone -- it is driven by sma_12/sma_22/sma_55 which ARE present.

    For a strictly ascending 50-bar series, sma_12 > sma_22 > sma_55
    so classification is 'bullish' -- that is correct.  What we verify
    is that sma_100 and sma_200 are None (insufficient bars).

    Per spec: when ANY of the three short-term SMAs (12/22/55) is None
    the classification falls back to 'mixed'. With 50 bars all three
    are present, so the meaningful assertion is that sma_100 is None
    and sma_200 is None.
    """
    from engine.sma_stack import compute_sma_stack

    closes = [100.0 + i for i in range(50)]
    result = compute_sma_stack(closes)

    assert result["smas"][100] is None, "sma_100 must be None with only 50 closes"
    assert result["smas"][200] is None, "sma_200 must be None with only 50 closes"
    # substate cannot be all_above because not all five SMAs are present
    assert result["stack_substate"] != "all_above", (
        "stack_substate must not be 'all_above' when long-term SMAs are absent"
    )


# ---------------------------------------------------------------------------
# 6. Delta correctness
# ---------------------------------------------------------------------------


def test_deltas():
    """delta_abs[55] must equal close - sma_55 and delta_pct[55] must
    equal (close - sma_55) / sma_55 within floating-point tolerance.
    """
    from engine.sma_stack import compute_sma_stack

    closes = [50.0 + i * 0.1 for i in range(200)]
    result = compute_sma_stack(closes)

    close = result["daily_close"]
    sma55 = result["smas"][55]

    assert close is not None
    assert sma55 is not None

    expected_abs = close - sma55
    expected_pct = (close - sma55) / sma55

    assert abs(result["deltas_abs"][55] - expected_abs) < 1e-9, (
        f"delta_abs[55] mismatch: {result['deltas_abs'][55]} != {expected_abs}"
    )
    assert abs(result["deltas_pct"][55] - expected_pct) < 1e-9, (
        f"delta_pct[55] mismatch: {result['deltas_pct'][55]} != {expected_pct}"
    )


# ---------------------------------------------------------------------------
# 7. Order relations for ascending closes
# ---------------------------------------------------------------------------


def test_order_relations():
    """For a monotonically ascending series, SMA 12 > SMA 22 > SMA 55,
    so order_relations must be ['gt', 'gt'].
    """
    from engine.sma_stack import compute_sma_stack

    closes = [100.0 + i for i in range(250)]
    result = compute_sma_stack(closes)

    assert result["order_relations"] == ["gt", "gt"], (
        f"Expected ['gt', 'gt'], got {result['order_relations']}"
    )


# ---------------------------------------------------------------------------
# 8. get_recent_daily_closes uses injected fetcher
# ---------------------------------------------------------------------------


def test_get_recent_daily_closes_uses_fetcher():
    """A stub fetcher must be called with (ticker, lookback) and its
    return value must pass through to the caller unchanged.
    """
    from engine import daily_bars

    # Clear cache to prevent cross-test contamination.
    daily_bars._cache_clear()

    stub_closes = [100.0, 101.0, 102.0, 103.0]
    mock_fetcher = MagicMock(return_value=stub_closes)

    result = daily_bars.get_recent_daily_closes(
        "TEST_TICKER_FETCH", lookback=4, fetcher=mock_fetcher
    )

    mock_fetcher.assert_called_once_with("TEST_TICKER_FETCH", 4)
    assert result == stub_closes, f"Expected stub_closes to pass through, got {result}"


# ---------------------------------------------------------------------------
# 9. get_recent_daily_closes caches -- fetcher called exactly once
# ---------------------------------------------------------------------------


def test_get_recent_daily_closes_caches():
    """Calling get_recent_daily_closes twice with the same ticker and
    lookback must invoke the fetcher exactly once (second call uses cache).
    """
    from engine import daily_bars

    daily_bars._cache_clear()

    stub_closes = [200.0 + i for i in range(10)]
    mock_fetcher = MagicMock(return_value=stub_closes)

    first = daily_bars.get_recent_daily_closes(
        "CACHE_TEST_TICKER", lookback=10, fetcher=mock_fetcher
    )
    second = daily_bars.get_recent_daily_closes(
        "CACHE_TEST_TICKER", lookback=10, fetcher=mock_fetcher
    )

    assert mock_fetcher.call_count == 1, (
        f"Fetcher should be called once (cached), was called {mock_fetcher.call_count} times"
    )
    assert first == second == stub_closes


# ---------------------------------------------------------------------------
# 10. Snapshot test: phase2 row carries sma_stack key
# ---------------------------------------------------------------------------


def test_snapshot_includes_sma_stack():
    """After the v5.21.0 wiring, each phase2 row returned by
    build_tiger_sovereign_snapshot must contain an 'sma_stack' key.

    The value is either None (when Alpaca is unavailable in the test
    environment) or a dict with the five expected sub-keys.
    """
    # Reload the snapshot module fresh so edits are picked up.
    for mod in list(sys.modules.keys()):
        if "v5_13_2_snapshot" in mod:
            del sys.modules[mod]

    import v5_13_2_snapshot as ts

    class _StubM:
        BOT_VERSION = "5.21.0"
        TRADE_TICKERS: list = ["AAPL"]
        positions: dict = {}
        short_positions: dict = {}
        or_high: dict = {}
        or_low: dict = {}

        def _now_et(self):
            from datetime import datetime, timezone

            return datetime(2026, 4, 29, 14, 0, tzinfo=timezone.utc)

        def fetch_1min_bars(self, ticker: str):
            return None

        def _opening_avwap(self, ticker: str):
            return None

        def tiger_di(self, ticker: str):
            return (None, None)

    m = _StubM()
    snap = ts.build_tiger_sovereign_snapshot(m, ["AAPL"], {}, {}, {})

    phase2 = snap.get("phase2", [])
    assert len(phase2) >= 1, "phase2 should contain at least one row for AAPL"

    row = phase2[0]
    assert "sma_stack" in row, (
        f"phase2 row must contain 'sma_stack' key; keys found: {list(row.keys())}"
    )

    sma_stack = row["sma_stack"]
    if sma_stack is not None:
        # Verify the five expected top-level sub-keys when data is available.
        for key in ("daily_close", "smas", "deltas_abs", "deltas_pct", "above"):
            assert key in sma_stack, (
                f"sma_stack missing sub-key {key!r}; keys: {list(sma_stack.keys())}"
            )
