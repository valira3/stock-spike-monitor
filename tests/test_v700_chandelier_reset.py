"""tests/test_v700_chandelier_reset.py -- v7.0.0 Phase 2A: chandelier reset on entry.

Verifies that PortfolioBook.record_entry() resets all TrailState fields
to a fresh baseline on a new entry, so a re-entry on the same (ticker, side)
never inherits peak_close / stage from a prior leg.

Reproduces the AVGO incident (2026-05-06): third SHORT entry at $419.57 had
peak_close=$418.18 from the second leg, causing the chandelier trail to snap
to $419.56 within 3 minutes. After record_entry(), peak_close is $419.57 and
stage is STAGE_INACTIVE.

Spec reference: docs/specs/v7_0_0_spec.md Section E.
"""

from __future__ import annotations

import logging
import os

# Minimal env so trade_genius imports cleanly in the test harness.
os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("TELEGRAM_TOKEN", "0000000000:AAAA_smoke_placeholder_token_0000000")
os.environ.setdefault("CHAT_ID", "999999999")
os.environ.setdefault("DASHBOARD_PASSWORD", "smoketest1234")
os.environ.setdefault("FMP_API_KEY", "fake_fmp_key_for_tests")

import pytest  # noqa: E402

from engine.alarm_f_trail import (  # noqa: E402
    STAGE_CHANDELIER_TIGHT,
    STAGE_INACTIVE,
    TrailState,
)
from engine.portfolio_book import PortfolioBook  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def book():
    """A standalone PortfolioBook with isolated position dicts."""
    b = PortfolioBook(portfolio_id="test")
    return b


def _avgo_stale_trail() -> TrailState:
    """Simulate the stale TrailState AVGO carried into its 3rd entry.

    Prior leg values: SHORT, peak_close=418.18, stage=CHANDELIER_TIGHT (3).
    """
    ts = TrailState(
        stage=STAGE_CHANDELIER_TIGHT,
        peak_close=418.18,
        stage2_arm_favorable=1.39,
        stage2_arm_atr=0.87,
        last_proposed_stop=419.00,
        bars_seen=37,
        last_atr=0.91,
        last_mult=0.7,
    )
    return ts


# ---------------------------------------------------------------------------
# Core correctness tests
# ---------------------------------------------------------------------------


def test_record_entry_resets_peak_close_to_entry_price(book):
    """peak_close must equal entry_price after record_entry, not the prior leg's HWM."""
    stale = _avgo_stale_trail()
    book.short_positions["AVGO"] = {
        "entry_price": 419.57,
        "shares": 24,
        "trail_state": stale,
        "entry_count": 3,
    }

    book.record_entry(ticker="AVGO", side="SHORT", entry_price=419.57, entry_count=3)

    ts = book.short_positions["AVGO"]["trail_state"]
    assert ts.peak_close == pytest.approx(419.57), (
        f"peak_close should be 419.57 (new entry), got {ts.peak_close}"
    )


def test_record_entry_resets_stage_to_inactive(book):
    """Stage must be STAGE_INACTIVE (0) after record_entry."""
    stale = _avgo_stale_trail()
    book.short_positions["AVGO"] = {
        "entry_price": 419.57,
        "shares": 24,
        "trail_state": stale,
        "entry_count": 3,
    }

    book.record_entry(ticker="AVGO", side="SHORT", entry_price=419.57, entry_count=3)

    ts = book.short_positions["AVGO"]["trail_state"]
    assert ts.stage == STAGE_INACTIVE, (
        f"stage should be STAGE_INACTIVE ({STAGE_INACTIVE}), got {ts.stage}"
    )


def test_record_entry_resets_bars_seen_to_zero(book):
    """bars_seen must be 0 after record_entry."""
    book.short_positions["AVGO"] = {
        "entry_price": 419.57,
        "shares": 24,
        "trail_state": _avgo_stale_trail(),
        "entry_count": 3,
    }

    book.record_entry(ticker="AVGO", side="SHORT", entry_price=419.57, entry_count=3)

    ts = book.short_positions["AVGO"]["trail_state"]
    assert ts.bars_seen == 0


def test_record_entry_clears_atr_and_stop_fields(book):
    """last_atr, last_proposed_stop, stage2_arm_* must all be None after record_entry."""
    book.short_positions["AVGO"] = {
        "entry_price": 419.57,
        "shares": 24,
        "trail_state": _avgo_stale_trail(),
        "entry_count": 3,
    }

    book.record_entry(ticker="AVGO", side="SHORT", entry_price=419.57, entry_count=3)

    ts = book.short_positions["AVGO"]["trail_state"]
    assert ts.last_atr is None
    assert ts.last_proposed_stop is None
    assert ts.stage2_arm_favorable is None
    assert ts.stage2_arm_atr is None


def test_record_entry_last_mult_reset_to_zero(book):
    """last_mult must be 0.0 (Stage < 2, no chandelier active) after record_entry."""
    book.short_positions["AVGO"] = {
        "entry_price": 419.57,
        "shares": 24,
        "trail_state": _avgo_stale_trail(),
        "entry_count": 3,
    }

    book.record_entry(ticker="AVGO", side="SHORT", entry_price=419.57, entry_count=3)

    ts = book.short_positions["AVGO"]["trail_state"]
    assert ts.last_mult == 0.0


def test_record_entry_long_side(book):
    """record_entry works for LONG positions too."""
    book.positions["AAPL"] = {
        "entry_price": 285.07,
        "shares": 35,
        "trail_state": TrailState(
            stage=STAGE_CHANDELIER_TIGHT,
            peak_close=289.50,
            bars_seen=20,
            last_atr=1.2,
            last_mult=0.7,
        ),
        "entry_count": 2,
    }

    book.record_entry(ticker="AAPL", side="LONG", entry_price=285.07, entry_count=2)

    ts = book.positions["AAPL"]["trail_state"]
    assert ts.peak_close == pytest.approx(285.07)
    assert ts.stage == STAGE_INACTIVE
    assert ts.bars_seen == 0
    assert ts.last_atr is None


def test_record_entry_when_no_existing_trail_state(book):
    """record_entry must work even when trail_state key is missing on the pos dict."""
    book.positions["NVDA"] = {
        "entry_price": 900.00,
        "shares": 11,
        # No trail_state key -- simulates very first entry after a fresh dict
        "entry_count": 1,
    }

    book.record_entry(ticker="NVDA", side="LONG", entry_price=900.00, entry_count=1)

    ts = book.positions["NVDA"]["trail_state"]
    assert isinstance(ts, TrailState)
    assert ts.peak_close == pytest.approx(900.00)
    assert ts.stage == STAGE_INACTIVE


def test_record_entry_when_position_not_yet_in_dict(book):
    """record_entry must not raise when the position dict doesn't have the ticker yet."""
    # This is the edge case where record_entry is called before the pos dict is
    # inserted (should not happen in prod, but must be safe).
    result = book.record_entry(
        ticker="MSFT", side="SHORT", entry_price=420.00, entry_count=1
    )
    assert isinstance(result, TrailState)
    assert result.peak_close == pytest.approx(420.00)
    assert result.stage == STAGE_INACTIVE


# ---------------------------------------------------------------------------
# Log emission test
# ---------------------------------------------------------------------------


def test_record_entry_emits_v700_chandelier_reset_log(book, caplog):
    """record_entry must emit a [V700-CHANDELIER-RESET] log line."""
    book.short_positions["AVGO"] = {
        "entry_price": 419.57,
        "shares": 24,
        "trail_state": _avgo_stale_trail(),
        "entry_count": 3,
    }

    with caplog.at_level(logging.INFO, logger="engine.portfolio_book"):
        book.record_entry(
            ticker="AVGO", side="SHORT", entry_price=419.57, entry_count=3
        )

    v700_lines = [r for r in caplog.records if "V700-CHANDELIER-RESET" in r.message]
    assert len(v700_lines) >= 1, "Expected at least one [V700-CHANDELIER-RESET] log line"

    msg = v700_lines[0].message
    assert "AVGO" in msg
    assert "SHORT" in msg
    assert "entry#3" in msg
    # Old peak (418.18) and new peak (419.57) must both appear
    assert "418.18" in msg
    assert "419.57" in msg
    # Old stage (3) and new stage (0 = STAGE_INACTIVE) must both appear
    assert "stage" in msg.lower()


def test_record_entry_emits_log_for_first_entry_no_prior_state(book, caplog):
    """Log line must also be emitted on the very first entry (no stale state)."""
    book.positions["TSLA"] = {
        "entry_price": 250.00,
        "shares": 40,
        "entry_count": 1,
        # No trail_state -- simulates a brand-new position
    }

    with caplog.at_level(logging.INFO, logger="engine.portfolio_book"):
        book.record_entry(ticker="TSLA", side="LONG", entry_price=250.00, entry_count=1)

    v700_lines = [r for r in caplog.records if "V700-CHANDELIER-RESET" in r.message]
    assert len(v700_lines) >= 1
    msg = v700_lines[0].message
    assert "TSLA" in msg
    assert "LONG" in msg
    assert "entry#1" in msg


# ---------------------------------------------------------------------------
# fresh() equivalence
# ---------------------------------------------------------------------------


def test_record_entry_trail_state_equivalent_to_fresh(book):
    """The stamped trail_state after record_entry should match TrailState.fresh()
    except that peak_close is seeded to entry_price (not None)."""
    book.short_positions["AVGO"] = {
        "entry_price": 419.57,
        "shares": 24,
        "trail_state": _avgo_stale_trail(),
        "entry_count": 3,
    }

    book.record_entry(ticker="AVGO", side="SHORT", entry_price=419.57, entry_count=3)

    ts = book.short_positions["AVGO"]["trail_state"]
    fresh = TrailState.fresh()

    # Everything except peak_close (seeded) must match TrailState.fresh() defaults.
    assert ts.stage == fresh.stage
    assert ts.stage2_arm_favorable == fresh.stage2_arm_favorable
    assert ts.stage2_arm_atr == fresh.stage2_arm_atr
    assert ts.last_proposed_stop == fresh.last_proposed_stop
    assert ts.bars_seen == fresh.bars_seen
    assert ts.last_atr == fresh.last_atr
    assert ts.last_mult == fresh.last_mult
    # peak_close is the one intentional deviation from fresh().
    assert ts.peak_close == pytest.approx(419.57)
    assert fresh.peak_close is None
