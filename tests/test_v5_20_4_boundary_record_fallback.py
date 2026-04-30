"""v5.20.4 \u2014 Boundary Hold close recorder fallback contract tests.

Covers ``v5_10_1_integration.record_latest_1m_close`` and the new
``engine/scan.py`` call site that consumes it. Yahoo's intraday minute
response keeps a forming bar at ``closes[-2]`` whose value is ``None``
during RTH; the prior implementation guarded ``[-2] is not None`` and
silently never fired, leaving every Phase 2 boundary check starved.
The helper walks back up to 4 slots to find the newest non-None close,
falls back to ``[-1]`` only as a last resort, and de-dups against the
last buffered value.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import v5_10_1_integration as glue  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_buffer():
    """Each test gets a fresh per-ticker buffer."""
    glue._last_1m_closes.clear()
    yield
    glue._last_1m_closes.clear()


# ---------------------------------------------------------------------
# record_latest_1m_close \u2014 happy paths
# ---------------------------------------------------------------------


def test_records_minus_2_when_full_and_non_none():
    closes = [100.0, 100.5, 101.0, 101.5]
    assert glue.record_latest_1m_close("AAPL", closes) is True
    assert glue._last_1m_closes["AAPL"] == [101.0]


def test_falls_back_to_minus_3_when_minus_2_is_none():
    # Yahoo's dominant RTH shape: forming bar at [-2] is None, the
    # last fully-closed minute lives at [-3], and [-1] is a stale
    # snapshot.
    closes = [99.0, 99.5, 100.5, None, 101.5]
    assert glue.record_latest_1m_close("NVDA", closes) is True
    assert glue._last_1m_closes["NVDA"] == [100.5]


def test_walks_back_through_multiple_nones():
    closes = [98.0, 100.0, None, None, None, 101.5]
    # [-2]=None, [-3]=None, [-4]=None, [-5]=100.0 \u2192 picks 100.0
    assert glue.record_latest_1m_close("MSFT", closes) is True
    assert glue._last_1m_closes["MSFT"] == [100.0]


def test_last_resort_fallback_to_minus_1():
    # Only the snapshot has a value; no earlier slot does.
    closes = [None, None, None, 199.59]
    assert glue.record_latest_1m_close("NVDA", closes) is True
    assert glue._last_1m_closes["NVDA"] == [199.59]


def test_single_element_records_minus_1():
    closes = [123.45]
    assert glue.record_latest_1m_close("AVGO", closes) is True
    assert glue._last_1m_closes["AVGO"] == [123.45]


# ---------------------------------------------------------------------
# record_latest_1m_close \u2014 edge cases / negative paths
# ---------------------------------------------------------------------


def test_returns_false_on_empty_list():
    assert glue.record_latest_1m_close("AMZN", []) is False
    assert "AMZN" not in glue._last_1m_closes


def test_returns_false_when_all_none():
    assert glue.record_latest_1m_close("META", [None, None, None]) is False
    assert "META" not in glue._last_1m_closes


def test_walk_back_capped_at_4_slots_from_end():
    # 6-element list, only [-6] (= [0]) has a value. The walk back
    # iterates ``range(2, min(len, 5)+1)`` = 2..5, so it inspects
    # [-2], [-3], [-4], [-5]. Index [-6]=99.0 is out of reach. The
    # last-resort ``[-1]`` is also None, so the helper gives up.
    closes = [99.0, None, None, None, None, None]
    assert glue.record_latest_1m_close("GOOG", closes) is False
    assert "GOOG" not in glue._last_1m_closes


# ---------------------------------------------------------------------
# de-dup behavior across cycles
# ---------------------------------------------------------------------


def test_dedups_identical_repeat():
    closes = [100.0, 100.5, 101.0, 101.5]
    assert glue.record_latest_1m_close("TSLA", closes) is True
    # Second call with the same slice should NOT re-record.
    assert glue.record_latest_1m_close("TSLA", closes) is False
    assert glue._last_1m_closes["TSLA"] == [101.0]


def test_appends_when_value_changes():
    glue.record_latest_1m_close("ORCL", [100.0, 101.0])
    glue.record_latest_1m_close("ORCL", [101.0, 102.0])
    glue.record_latest_1m_close("ORCL", [102.0, 103.0])
    assert glue._last_1m_closes["ORCL"] == [100.0, 101.0, 102.0]


def test_buffer_trimmed_to_last_4():
    for v in [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]:
        # Wrap each value at idx=-2 so the helper picks it.
        glue.record_latest_1m_close("NFLX", [None, v, None])
    # record_1m_close keeps the rolling window at 4 entries.
    assert glue._last_1m_closes["NFLX"] == [12.0, 13.0, 14.0, 15.0]


# ---------------------------------------------------------------------
# Yahoo-shape integration \u2014 simulate the real bug pattern
# ---------------------------------------------------------------------


def test_simulated_yahoo_session_populates_buffer():
    """Five successive ``fetch_1min_bars`` returns where every cycle
    has a forming None at ``[-2]``. The pre-fix path would have
    skipped all of them; the helper must register at least one
    distinct close.
    """
    cycles = [
        # cycle 1: [-2]=None, [-3]=200.0
        [199.0, 199.5, 200.0, None, 200.1],
        # cycle 2: [-2]=None, [-3]=200.5 (new fully-closed minute)
        [199.5, 200.0, 200.5, None, 200.4],
        # cycle 3: same shape, new closed value 201.0
        [200.0, 200.5, 201.0, None, 200.9],
        # cycle 4: same closed value 201.0 (mid-minute fetch \u2192 dedup)
        [200.0, 200.5, 201.0, None, 201.05],
        # cycle 5: closed value advances to 201.5
        [200.5, 201.0, 201.5, None, 201.4],
    ]
    for c in cycles:
        glue.record_latest_1m_close("NVDA", c)
    # 200.0, 200.5, 201.0 (deduped on cycle 4), 201.5 \u2192 4 entries
    assert glue._last_1m_closes["NVDA"] == [200.0, 200.5, 201.0, 201.5]


def test_engine_scan_callsite_uses_helper(monkeypatch):
    """Smoke test that ``engine/scan.py`` imports and exposes the
    helper through ``eot_glue``. The full per-ticker tick is heavy
    to set up, so we assert the symbol wiring directly.
    """
    import engine.scan as scan  # noqa: F401  (import side effects)
    import v5_10_1_integration as glue_mod

    # The fix replaces the inline guard with a helper call.
    assert hasattr(glue_mod, "record_latest_1m_close")
    assert callable(glue_mod.record_latest_1m_close)
    # And it MUST be exported \u2014 otherwise the engine/scan import
    # surface is fragile.
    assert "record_latest_1m_close" in glue_mod.__all__


def test_helper_is_idempotent_when_closes_value_repeats():
    """Same value at the head, called many times in a row, should
    leave the buffer at length 1. Defends against minute-aligned
    scan storms re-registering the identical closed bar.
    """
    closes = [None, 250.75, None]
    for _ in range(20):
        glue.record_latest_1m_close("AAPL", closes)
    assert glue._last_1m_closes["AAPL"] == [250.75]
