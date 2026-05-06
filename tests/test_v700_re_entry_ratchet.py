"""tests/test_v700_re_entry_ratchet.py -- v7.0.0 Phase 2.5: re-entry HOD/LOD ratchet.

Verifies PortfolioBook.record_exit() and PortfolioBook.re_entry_ratchet_ok()
implement Eugene's rule:

    "2nd and 3rd strikes have to be on a new HOD (long) or LOD (short)."

Reproduces the AVGO SHORT stacking incident (2026-05-06): 6 entries at
431.72 -> 431.26 -> 431.05 -> 430.91 -> 430.19 -> 429.36 inside a $2.36
band where each was technically a fresh LOD vs. OR but did not push past
the prior leg's extreme.

Spec reference: docs/specs/v7_0_0_spec.md Section E.5.
"""

from __future__ import annotations

import os

# Minimal env so trade_genius imports cleanly in the test harness.
os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("TELEGRAM_TOKEN", "0000000000:AAAA_smoke_placeholder_token_0000000")
os.environ.setdefault("CHAT_ID", "999999999")
os.environ.setdefault("DASHBOARD_PASSWORD", "smoketest1234")
os.environ.setdefault("FMP_API_KEY", "fake_fmp_key_for_tests")

import pytest  # noqa: E402

from engine.portfolio_book import PortfolioBook  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def book_main():
    """PortfolioBook with portfolio_id='main'."""
    return PortfolioBook(portfolio_id="main")


@pytest.fixture()
def book_val():
    """Separate PortfolioBook with portfolio_id='val' -- isolation testing."""
    return PortfolioBook(portfolio_id="val")


# ---------------------------------------------------------------------------
# record_exit -- LONG ratchet
# ---------------------------------------------------------------------------


def test_record_exit_long_sets_initial_ratchet(book_main):
    """First record_exit for a LONG leg stores leg_high as the ratchet."""
    book_main.record_exit("AVGO", "LONG", leg_high=431.72)
    key = ("main", "AVGO")
    assert book_main.prior_legs_max_high_long[key] == 431.72


def test_record_exit_long_ratchets_monotonically(book_main):
    """Successive LONG leg closes ratchet max high upward only."""
    book_main.record_exit("AVGO", "LONG", leg_high=431.72)
    # Second leg had a lower intra-leg high -- ratchet stays at 431.72.
    book_main.record_exit("AVGO", "LONG", leg_high=430.50)
    key = ("main", "AVGO")
    assert book_main.prior_legs_max_high_long[key] == 431.72
    # Third leg with a new higher high -- ratchet updates.
    book_main.record_exit("AVGO", "LONG", leg_high=432.10)
    assert book_main.prior_legs_max_high_long[key] == 432.10


def test_record_exit_long_none_leg_high_is_noop(book_main):
    """record_exit with leg_high=None must not modify the ratchet dict."""
    book_main.record_exit("AVGO", "LONG", leg_high=None)
    assert ("main", "AVGO") not in book_main.prior_legs_max_high_long


# ---------------------------------------------------------------------------
# record_exit -- SHORT ratchet
# ---------------------------------------------------------------------------


def test_record_exit_short_sets_initial_ratchet(book_main):
    """First record_exit for a SHORT leg stores leg_low as the ratchet."""
    # AVGO SHORT leg 1: LOD reached 430.91 intra-leg.
    book_main.record_exit("AVGO", "SHORT", leg_low=430.91)
    key = ("main", "AVGO")
    assert book_main.prior_legs_min_low_short[key] == 430.91


def test_record_exit_short_ratchets_monotonically(book_main):
    """Successive SHORT leg closes ratchet min low downward only."""
    book_main.record_exit("AVGO", "SHORT", leg_low=430.91)
    # Second leg had a higher (worse) low -- ratchet stays at 430.91.
    book_main.record_exit("AVGO", "SHORT", leg_low=431.20)
    key = ("main", "AVGO")
    assert book_main.prior_legs_min_low_short[key] == 430.91
    # Third leg with a new lower low -- ratchet updates.
    book_main.record_exit("AVGO", "SHORT", leg_low=429.36)
    assert book_main.prior_legs_min_low_short[key] == 429.36


def test_record_exit_short_none_leg_low_is_noop(book_main):
    """record_exit with leg_low=None must not modify the ratchet dict."""
    book_main.record_exit("AVGO", "SHORT", leg_low=None)
    assert ("main", "AVGO") not in book_main.prior_legs_min_low_short


# ---------------------------------------------------------------------------
# re_entry_ratchet_ok -- first leg (no prior ratchet)
# ---------------------------------------------------------------------------


def test_re_entry_ratchet_ok_no_prior_long_passes(book_main):
    """First leg: no prior ratchet -> (True, None) for LONG."""
    ok, detail = book_main.re_entry_ratchet_ok("AVGO", "LONG", current_high=431.72)
    assert ok is True
    assert detail is None


def test_re_entry_ratchet_ok_no_prior_short_passes(book_main):
    """First leg: no prior ratchet -> (True, None) for SHORT."""
    ok, detail = book_main.re_entry_ratchet_ok("AVGO", "SHORT", current_low=430.91)
    assert ok is True
    assert detail is None


# ---------------------------------------------------------------------------
# re_entry_ratchet_ok -- LONG after a prior leg
# ---------------------------------------------------------------------------


def test_re_entry_ratchet_ok_long_above_ratchet_passes(book_main):
    """current_high strictly above prior max_high -> passes."""
    book_main.record_exit("AVGO", "LONG", leg_high=431.72)
    ok, detail = book_main.re_entry_ratchet_ok("AVGO", "LONG", current_high=432.00)
    assert ok is True
    assert detail is None


def test_re_entry_ratchet_ok_long_below_ratchet_rejected(book_main):
    """current_high below prior max_high -> rejected with detail string."""
    book_main.record_exit("AVGO", "LONG", leg_high=431.72)
    ok, detail = book_main.re_entry_ratchet_ok("AVGO", "LONG", current_high=431.20)
    assert ok is False
    assert detail is not None
    assert "prior_max_high" in detail


def test_re_entry_ratchet_ok_long_equal_to_ratchet_rejected(book_main):
    """current_high equal to ratchet (not strictly greater) -> rejected.

    Strict inequality: equal = stale HOD, not a new one.
    """
    book_main.record_exit("AVGO", "LONG", leg_high=431.72)
    ok, detail = book_main.re_entry_ratchet_ok("AVGO", "LONG", current_high=431.72)
    assert ok is False
    assert detail is not None


# ---------------------------------------------------------------------------
# re_entry_ratchet_ok -- SHORT after a prior leg
# ---------------------------------------------------------------------------


def test_re_entry_ratchet_ok_short_below_ratchet_passes(book_main):
    """current_low strictly below prior min_low -> passes."""
    book_main.record_exit("AVGO", "SHORT", leg_low=430.91)
    ok, detail = book_main.re_entry_ratchet_ok("AVGO", "SHORT", current_low=430.19)
    assert ok is True
    assert detail is None


def test_re_entry_ratchet_ok_short_above_ratchet_rejected(book_main):
    """current_low above prior min_low -> rejected with detail string.

    Reproduces the AVGO scenario: leg 2 current_low=430.19 would pass,
    but leg 3 attempt at current_low=430.91 (stale) should be rejected.
    """
    book_main.record_exit("AVGO", "SHORT", leg_low=430.19)
    ok, detail = book_main.re_entry_ratchet_ok("AVGO", "SHORT", current_low=430.91)
    assert ok is False
    assert detail is not None
    assert "prior_min_low" in detail


def test_re_entry_ratchet_ok_short_equal_to_ratchet_rejected(book_main):
    """current_low equal to ratchet (not strictly less) -> rejected."""
    book_main.record_exit("AVGO", "SHORT", leg_low=430.91)
    ok, detail = book_main.re_entry_ratchet_ok("AVGO", "SHORT", current_low=430.91)
    assert ok is False
    assert detail is not None


# ---------------------------------------------------------------------------
# Defensive: None current_high / current_low -> pass-through
# ---------------------------------------------------------------------------


def test_re_entry_ratchet_ok_long_none_current_high_passthrough(book_main):
    """If current_high is None the gate cannot evaluate -> pass through."""
    book_main.record_exit("AVGO", "LONG", leg_high=431.72)
    ok, detail = book_main.re_entry_ratchet_ok("AVGO", "LONG", current_high=None)
    assert ok is True
    assert detail is None


def test_re_entry_ratchet_ok_short_none_current_low_passthrough(book_main):
    """If current_low is None the gate cannot evaluate -> pass through."""
    book_main.record_exit("AVGO", "SHORT", leg_low=430.91)
    ok, detail = book_main.re_entry_ratchet_ok("AVGO", "SHORT", current_low=None)
    assert ok is True
    assert detail is None


# ---------------------------------------------------------------------------
# Per-book isolation: ratchet on 'main' does not affect 'val'
# ---------------------------------------------------------------------------


def test_per_book_isolation(book_main, book_val):
    """Ratchet set on book 'main' must not gate book 'val' first leg.

    Per spec: different portfolio books have separate ratchets.
    Val and main can each take Leg 1; only the same book's ratchet gates.
    """
    # Simulate a closed LONG leg on main with a high of 431.72.
    book_main.record_exit("AVGO", "LONG", leg_high=431.72)

    # Val book has no prior ratchet for AVGO LONG -- should pass.
    ok_val, detail_val = book_val.re_entry_ratchet_ok("AVGO", "LONG", current_high=430.00)
    assert ok_val is True, "val book should pass with no prior ratchet"
    assert detail_val is None

    # Main book has a ratchet -- same current_high=430.00 is rejected.
    ok_main, detail_main = book_main.re_entry_ratchet_ok("AVGO", "LONG", current_high=430.00)
    assert ok_main is False, "main book should reject below-ratchet entry"


# ---------------------------------------------------------------------------
# EOD daily reset via .clear()
# ---------------------------------------------------------------------------


def test_ratchet_clears_on_eod_reset(book_main):
    """Simulate EOD reset: .clear() on both dicts wipes the ratchet state."""
    book_main.record_exit("AVGO", "LONG", leg_high=431.72)
    book_main.record_exit("NFLX", "SHORT", leg_low=88.10)

    # Simulate paper_state EOD clear (same mechanism used by reset_daily_state).
    book_main.prior_legs_max_high_long.clear()
    book_main.prior_legs_min_low_short.clear()

    assert len(book_main.prior_legs_max_high_long) == 0
    assert len(book_main.prior_legs_min_low_short) == 0

    # After reset, the first leg of the new day must pass through.
    ok_long, _ = book_main.re_entry_ratchet_ok("AVGO", "LONG", current_high=431.00)
    assert ok_long is True
    ok_short, _ = book_main.re_entry_ratchet_ok("NFLX", "SHORT", current_low=87.00)
    assert ok_short is True


# ---------------------------------------------------------------------------
# Ticker normalisation (case-insensitive)
# ---------------------------------------------------------------------------


def test_record_exit_ticker_case_insensitive(book_main):
    """Ticker symbols are uppercased before keying the ratchet dict."""
    book_main.record_exit("avgo", "long", leg_high=431.72)
    key_upper = ("main", "AVGO")
    assert key_upper in book_main.prior_legs_max_high_long


def test_re_entry_ratchet_ok_ticker_case_insensitive(book_main):
    """re_entry_ratchet_ok normalizes ticker and side before lookup."""
    book_main.record_exit("AVGO", "SHORT", leg_low=430.91)
    ok, detail = book_main.re_entry_ratchet_ok("avgo", "short", current_low=430.50)
    assert ok is True  # 430.50 < 430.91 -> passes
