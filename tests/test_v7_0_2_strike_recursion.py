"""v7.0.2 -- recursive strike unlock when all closed strikes are positive.

Verifies the new rule layered on top of v5.19.1's STRIKE-CAP-3:
  * Strikes 1, 2, 3 still cap by default (same as before).
  * Strike 4 (and beyond) becomes legal IFF every closed strike on
    the ticker has net P/L strictly > 0.
  * Any non-positive close (loss OR breakeven $0.00) re-anchors the
    cap at the current strike count for the rest of the session.
  * STRIKE-FLAT-GATE still applies independently of the unlock.
  * Per-strike P/L is recorded across multiple partial exits within
    the same strike (sum, not last write).
  * Per-ticker isolation: AAPL's strike history does not affect
    NVDA's unlock state.

No em-dashes in this file (forbidden chars constraint for new .py).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:fake")
os.environ.setdefault("FMP_API_KEY", "test_dummy_key")
os.environ.setdefault("LOG_LEVEL", "WARNING")

import trade_genius as tg  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_session():
    """Reset all session-scoped strike state between tests so a prior
    test never leaves a partially-populated _v570_strike_pnl that
    leaks into the next one's unlock decision."""
    tg._v570_strike_counts.clear()
    tg._v570_strike_pnl.clear()
    tg._v570_strike_date = tg._v570_session_today_str()
    tg._v570_session_date = tg._v570_session_today_str()
    tg._v570_daily_pnl_date = tg._v570_session_today_str()
    tg._v570_kill_switch_latched = False
    tg._v570_kill_switch_logged = False
    yield
    tg._v570_strike_counts.clear()
    tg._v570_strike_pnl.clear()


def _close_strike(ticker, strike_num, pnl):
    """Helper: simulate a TRADE_CLOSED for a strike."""
    tg._v702_record_strike_pnl(ticker, strike_num, pnl)


# ----- baseline: pre-v7.0.2 behavior preserved -----

def test_cap_3_still_blocks_when_no_closes_recorded():
    """No P/L recorded yet -> strike 4 blocked (recursive rule
    requires evidence of positive closes, not their absence)."""
    tg._v570_record_entry("NVDA", "LONG")
    tg._v570_record_entry("NVDA", "LONG")
    tg._v570_record_entry("NVDA", "SHORT")
    assert tg._v570_strike_count("NVDA") == 3
    # FLAT-GATE satisfied (no positions dict provided -> treated flat).
    assert tg.strike_entry_allowed("NVDA", "LONG") is False


def test_cap_3_blocks_when_any_closed_strike_negative():
    """Strikes 1+2+3 fired; strike 2 closed at a loss -> blocked."""
    for _ in range(3):
        tg._v570_record_entry("AAPL", "LONG")
    _close_strike("AAPL", 1, +50.0)
    _close_strike("AAPL", 2, -25.0)
    _close_strike("AAPL", 3, +100.0)
    assert tg.strike_entry_allowed("AAPL", "LONG") is False


def test_cap_3_blocks_when_any_closed_strike_zero():
    """Breakeven $0.00 close does NOT count as positive (strict >)."""
    for _ in range(3):
        tg._v570_record_entry("MSFT", "LONG")
    _close_strike("MSFT", 1, +50.0)
    _close_strike("MSFT", 2, 0.0)        # exactly breakeven
    _close_strike("MSFT", 3, +100.0)
    assert tg.strike_entry_allowed("MSFT", "LONG") is False


# ----- recursive unlock -----

def test_strike_4_unlocks_when_strikes_1_2_3_all_positive():
    """The headline path: three winners in a row -> strike 4 fires."""
    for _ in range(3):
        tg._v570_record_entry("NVDA", "LONG")
    _close_strike("NVDA", 1, +75.0)
    _close_strike("NVDA", 2, +50.0)
    _close_strike("NVDA", 3, +25.0)
    assert tg.strike_entry_allowed("NVDA", "LONG") is True
    # _v570_record_entry no longer raises when the unlock is active.
    assert tg._v570_record_entry("NVDA", "LONG") == 4


def test_recursive_strike_5_unlocks_after_strike_4_positive():
    """After 4 wins, strike 5 also unlocks (recursive)."""
    for _ in range(3):
        tg._v570_record_entry("META", "LONG")
    _close_strike("META", 1, +10.0)
    _close_strike("META", 2, +20.0)
    _close_strike("META", 3, +30.0)
    # Cap relaxed -> strike 4 fires.
    tg._v570_record_entry("META", "LONG")
    _close_strike("META", 4, +40.0)
    # Strike 5 also unlocks because strikes 1..4 are all positive.
    assert tg.strike_entry_allowed("META", "LONG") is True
    assert tg._v570_record_entry("META", "LONG") == 5


def test_recursion_breaks_on_first_negative_after_unlock():
    """Strike 4 was a loser -> strike 5 blocked even though 1..3
    were winners."""
    for _ in range(3):
        tg._v570_record_entry("TSLA", "LONG")
    _close_strike("TSLA", 1, +50.0)
    _close_strike("TSLA", 2, +50.0)
    _close_strike("TSLA", 3, +50.0)
    tg._v570_record_entry("TSLA", "LONG")  # strike 4
    _close_strike("TSLA", 4, -100.0)       # losing strike 4
    assert tg.strike_entry_allowed("TSLA", "LONG") is False


# ----- partial exits accumulate per strike -----

def test_partial_exits_sum_into_strike_pnl():
    """Multiple exits on the same strike sum to the strike's net P/L.
    A small loss + a bigger gain -> net positive -> strike unlocks."""
    for _ in range(3):
        tg._v570_record_entry("AVGO", "LONG")
    # Strike 1: trim at -$10, then full out at +$60 -> net +$50
    _close_strike("AVGO", 1, -10.0)
    _close_strike("AVGO", 1, +60.0)
    _close_strike("AVGO", 2, +20.0)
    _close_strike("AVGO", 3, +30.0)
    assert tg._v570_strike_pnl[("AVGO", 1)] == pytest.approx(50.0)
    assert tg.strike_entry_allowed("AVGO", "LONG") is True


def test_partial_exits_can_finish_negative():
    """Partial gain followed by larger loss closes the strike negative
    even though one leg was positive."""
    for _ in range(3):
        tg._v570_record_entry("ORCL", "LONG")
    _close_strike("ORCL", 1, +20.0)
    _close_strike("ORCL", 1, -50.0)        # net -30
    _close_strike("ORCL", 2, +10.0)
    _close_strike("ORCL", 3, +10.0)
    assert tg._v570_strike_pnl[("ORCL", 1)] == pytest.approx(-30.0)
    assert tg.strike_entry_allowed("ORCL", "LONG") is False


# ----- isolation -----

def test_per_ticker_isolation():
    """AAPL's recursive unlock state does not leak to NVDA."""
    for _ in range(3):
        tg._v570_record_entry("AAPL", "LONG")
    _close_strike("AAPL", 1, +50.0)
    _close_strike("AAPL", 2, +50.0)
    _close_strike("AAPL", 3, +50.0)
    # NVDA never traded today -> still capped at strike 1 entry path
    # (no strikes recorded yet, count==0). Just make sure NVDA's
    # zero-count path does not borrow AAPL's positive history.
    tg._v570_record_entry("NVDA", "LONG")
    tg._v570_record_entry("NVDA", "LONG")
    tg._v570_record_entry("NVDA", "LONG")
    # NVDA has no closes recorded -> strike 4 blocked.
    assert tg.strike_entry_allowed("NVDA", "LONG") is False
    # AAPL still allowed.
    assert tg.strike_entry_allowed("AAPL", "LONG") is True


def test_long_short_share_same_ticker_history():
    """STRIKE-CAP is per-ticker (long+short combined), so the unlock
    predicate also looks at long+short closes together."""
    tg._v570_record_entry("NFLX", "LONG")
    tg._v570_record_entry("NFLX", "SHORT")
    tg._v570_record_entry("NFLX", "LONG")
    _close_strike("NFLX", 1, +25.0)        # long
    _close_strike("NFLX", 2, +25.0)        # short
    _close_strike("NFLX", 3, +25.0)        # long
    # Either side can fire strike 4 (FLAT-GATE permitting).
    assert tg.strike_entry_allowed("NFLX", "LONG") is True
    assert tg.strike_entry_allowed("NFLX", "SHORT") is True


# ----- session reset -----

def test_session_boundary_clears_strike_pnl():
    """A new ET session wipes both _v570_strike_counts AND
    _v570_strike_pnl so yesterday's positive run does not seed
    today's unlock state."""
    for _ in range(3):
        tg._v570_record_entry("GOOG", "LONG")
    _close_strike("GOOG", 1, +50.0)
    _close_strike("GOOG", 2, +50.0)
    _close_strike("GOOG", 3, +50.0)
    assert tg._v570_strike_pnl[("GOOG", 1)] == pytest.approx(50.0)

    # Force a session rollover by faking a stale strike_date.
    tg._v570_strike_date = "2000-01-01"
    tg._v570_reset_if_new_session()

    # Both dicts cleared; even though "today" started empty for GOOG,
    # it has zero strikes/closes, so strike 4 is NOT unlocked.
    assert tg._v570_strike_counts == {}
    assert tg._v570_strike_pnl == {}
    assert tg.strike_entry_allowed("GOOG", "LONG") is True  # count=0 -> normal path


# ----- defensive: missing strike_num handled -----

def test_record_strike_pnl_returns_zero_for_invalid_inputs():
    """Bad ticker / strike_num must never raise."""
    assert tg._v702_record_strike_pnl("", 1, 50.0) == 0.0
    assert tg._v702_record_strike_pnl("AAPL", 0, 50.0) == 0.0
    assert tg._v702_record_strike_pnl("AAPL", -1, 50.0) == 0.0


def test_unlock_predicate_false_when_strike_n_has_no_pnl():
    """If counter says N=3 but only strikes 1+2 have P/L recorded,
    treat strike 3 as not-yet-finalized and keep the cap."""
    for _ in range(3):
        tg._v570_record_entry("QQQ", "LONG")
    _close_strike("QQQ", 1, +50.0)
    _close_strike("QQQ", 2, +50.0)
    # strike 3 P/L missing
    assert tg._v702_all_closed_strikes_positive("QQQ") is False
    assert tg.strike_entry_allowed("QQQ", "LONG") is False
