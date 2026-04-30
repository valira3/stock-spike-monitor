"""v5.19.1 vAA-1 ULTIMATE Decision 1 \\u2014 STRIKE-CAP-3 unified to per-ticker.

Verifies that long and short entries on the same ticker share a single
per-day counter capped at 3 total. STRIKE-FLAT-GATE remains per-side.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("SSM_SMOKE_TEST", "1")

import trade_genius as tg  # noqa: E402


def _setup_clean_session():
    """Reset all session-scoped state to a known-empty starting point."""
    tg._v570_strike_counts.clear()
    tg._v570_strike_date = tg._v570_session_today_str()
    tg._v570_session_date = tg._v570_session_today_str()
    tg._v570_daily_pnl_date = tg._v570_session_today_str()
    tg._v570_kill_switch_latched = False
    tg._v570_kill_switch_logged = False


def test_strike_cap_3_unified_long_then_short_blocks_fourth():
    """Two LONG entries + one SHORT entry exhaust the per-ticker cap."""
    _setup_clean_session()
    assert tg._v570_record_entry("NVDA", "LONG") == 1
    assert tg._v570_record_entry("NVDA", "LONG") == 2
    assert tg._v570_record_entry("NVDA", "SHORT") == 3

    # Counter reads 3 from EITHER side perspective.
    assert tg._v570_strike_count("NVDA", "LONG") == 3
    assert tg._v570_strike_count("NVDA", "SHORT") == 3

    # 4th attempt blocked at the gate, regardless of side.
    assert tg.strike_entry_allowed("NVDA", "LONG") is False
    assert tg.strike_entry_allowed("NVDA", "SHORT") is False


def test_strike_cap_3_unified_record_raises_on_fourth():
    """The hot-path record_entry raises when the cap is exhausted."""
    import pytest

    _setup_clean_session()
    tg._v570_record_entry("NVDA", "LONG")
    tg._v570_record_entry("NVDA", "SHORT")
    tg._v570_record_entry("NVDA", "LONG")

    with pytest.raises(RuntimeError, match="STRIKE-CAP-3"):
        tg._v570_record_entry("NVDA", "SHORT")
    with pytest.raises(RuntimeError, match="STRIKE-CAP-3"):
        tg._v570_record_entry("NVDA", "LONG")


def test_strike_cap_3_unified_independent_across_tickers():
    """Per-ticker counter \\u2014 NVDA being capped does not affect AAPL."""
    _setup_clean_session()
    for _ in range(3):
        tg._v570_record_entry("NVDA", "LONG")
    assert tg.strike_entry_allowed("NVDA", "LONG") is False

    # AAPL still has full quota.
    assert tg._v570_strike_count("AAPL", "LONG") == 0
    assert tg.strike_entry_allowed("AAPL", "LONG") is True
    assert tg.strike_entry_allowed("AAPL", "SHORT") is True


def test_strike_flat_gate_remains_per_side():
    """STRIKE-FLAT-GATE stays per-side: flat long while holding short is OK."""
    _setup_clean_session()
    # Holding 100 SHORT shares of NVDA. The LONG side is flat.
    fake_positions = {"NVDA:SHORT": {"shares": 100}}

    # Flat-gate sees zero on LONG side \\u2014 returns True.
    assert tg._v570_strike_must_be_flat("NVDA", "LONG", positions=fake_positions) is True
    # Flat-gate sees 100 on SHORT side \\u2014 returns False.
    assert tg._v570_strike_must_be_flat("NVDA", "SHORT", positions=fake_positions) is False

    # Composite: LONG entry allowed (under cap, flat-gate True).
    assert tg.strike_entry_allowed("NVDA", "LONG", positions=fake_positions) is True
    # Composite: SHORT entry blocked (flat-gate False; can't stack).
    assert tg.strike_entry_allowed("NVDA", "SHORT", positions=fake_positions) is False


def test_strike_cap_3_session_reset_clears_counter():
    """Session roll resets the per-ticker counter."""
    _setup_clean_session()
    for _ in range(3):
        tg._v570_record_entry("NVDA", "LONG")
    assert tg._v570_strike_count("NVDA", "LONG") == 3

    # Force a session roll by mocking the strike_date.
    tg._v570_strike_date = "1900-01-01"
    tg._v570_session_date = "1900-01-01"
    tg._v570_daily_pnl_date = "1900-01-01"

    assert tg._v570_strike_count("NVDA", "LONG") == 0
    assert tg.strike_entry_allowed("NVDA", "LONG") is True
    assert tg.strike_entry_allowed("NVDA", "SHORT") is True
