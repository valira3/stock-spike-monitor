"""tests/test_v700_dashboard_shape.py

v7.0.0 Phase 6 \u2014 /api/state portfolios map + strip + TRAIL pill state.

Spec H: /api/state carries a top-level \"portfolios\" key with 3 books.
Spec G: each book carries a \"strip\" sub-object (cooldowns, errors, state).
Spec F: each serialized position carries a \"trail_pill\" field.

Tests (15):
  1.  /api/state returns \"portfolios\" top-level key
  2.  portfolios contains exactly 3 keys: main, val, gene
  3.  Each book has required fields: portfolio_id, equity, day_pnl,
      positions, trades_today, strip
  4.  strip has required fields: cooldowns, errors, positions, day_pnl, state
  5.  strip.state for main book is one of valid states
  6.  strip.state is \"disabled\" when book.config.enabled = False
  7.  strip.state is \"halted_daily_loss\" when book.daily_halted() returns True
  8.  strip.state is \"paused\" for main when tg._scan_paused is True
  9.  Legacy \"portfolio\" key still present (back-compat)
  10. Legacy portfolio.equity matches portfolios.main.equity
  11. _build_portfolio_block handles None executor without raising
  12. _compute_trail_pill_state returns None for position with no stop
  13. _compute_trail_pill_state returns \"armed\" (LONG, mark above stop)
  14. _compute_trail_pill_state returns \"armed\" (SHORT, mark below stop)
  15. _compute_trail_pill_state returns \"breached_hold\" (hold > 0)
  16. _compute_trail_pill_state returns \"breached_firing\" (hold == 0)
  17. Best-effort: exception in one book does not prevent other books
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Minimal env so trade_genius imports cleanly.
os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("TELEGRAM_TOKEN", "0000000000:AAAA_smoke_placeholder_token_0000000")
os.environ.setdefault("CHAT_ID", "999999999")
os.environ.setdefault("DASHBOARD_PASSWORD", "smoketest1234")
os.environ.setdefault("FMP_API_KEY", "fake_fmp_key_for_tests")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_modules():
    """Ensure each test starts with a fresh dashboard_server import.

    Scoped to this module only (autouse fixtures in non-conftest files
    are module-scoped by default in pytest).  Teardown removes only the
    modules this file loaded so sibling test files’ module caches are
    not disturbed.
    """
    # Pre-test: drop any stale dashboard_server (not trade_genius —
    # other test files in the same session may rely on the live tg module).
    sys.modules.pop("dashboard_server", None)
    yield
    # Post-test: drop dashboard_server only; leave trade_genius alone.
    sys.modules.pop("dashboard_server", None)


@pytest.fixture
def smoke_modules():
    """Import trade_genius + dashboard_server in smoke mode."""
    import trade_genius
    import dashboard_server
    return trade_genius, dashboard_server


# ---------------------------------------------------------------------------
# Tests 1\u20132: top-level portfolios key presence and shape
# ---------------------------------------------------------------------------

def test_portfolios_key_present(smoke_modules):
    """snapshot() response must carry a top-level 'portfolios' key."""
    tg, ds = smoke_modules
    snap = ds.snapshot()
    assert snap.get("ok") is True, f"snapshot failed: {snap}"
    assert "portfolios" in snap, "missing 'portfolios' key in /api/state"


def test_portfolios_has_exactly_three_books(smoke_modules):
    """portfolios must contain exactly main, val, gene and nothing else."""
    tg, ds = smoke_modules
    snap = ds.snapshot()
    port_map = snap["portfolios"]
    assert isinstance(port_map, dict), "portfolios must be a dict"
    assert set(port_map.keys()) == {"main", "val", "gene"}, (
        f"portfolios keys mismatch: {set(port_map.keys())}"
    )


# ---------------------------------------------------------------------------
# Test 3: required fields on each book
# ---------------------------------------------------------------------------

def test_each_book_has_required_fields(smoke_modules):
    """Each book block must carry portfolio_id, equity, day_pnl,
    positions, trades_today, strip."""
    tg, ds = smoke_modules
    snap = ds.snapshot()
    required = {"portfolio_id", "equity", "day_pnl", "positions", "trades_today", "strip"}
    for pid in ("main", "val", "gene"):
        block = snap["portfolios"][pid]
        missing = required - set(block.keys())
        assert not missing, f"portfolios[{pid}] missing fields: {missing}"


# ---------------------------------------------------------------------------
# Test 4: strip sub-object shape
# ---------------------------------------------------------------------------

def test_strip_has_required_fields(smoke_modules):
    """strip must carry cooldowns, errors, positions, day_pnl, state."""
    tg, ds = smoke_modules
    snap = ds.snapshot()
    strip_required = {"cooldowns", "errors", "positions", "day_pnl", "state"}
    for pid in ("main", "val", "gene"):
        strip = snap["portfolios"][pid]["strip"]
        missing = strip_required - set(strip.keys())
        assert not missing, f"portfolios[{pid}].strip missing fields: {missing}"


# ---------------------------------------------------------------------------
# Test 5: strip.state valid value set
# ---------------------------------------------------------------------------

def test_strip_state_valid_values(smoke_modules):
    """strip.state must be one of the four defined states."""
    tg, ds = smoke_modules
    snap = ds.snapshot()
    valid = {"active", "halted_daily_loss", "paused", "disabled", "unknown"}
    for pid in ("main", "val", "gene"):
        state = snap["portfolios"][pid]["strip"]["state"]
        assert state in valid, f"portfolios[{pid}].strip.state invalid: {state!r}"


# ---------------------------------------------------------------------------
# Tests 6\u20138: strip.state logic via direct helper calls
# ---------------------------------------------------------------------------

def _make_book(pid="main", enabled=True, halted=False):
    """Build a minimal PortfolioBook-like object for helper tests."""
    from engine.portfolio_book import PortfolioBook, PortfolioConfig
    book = PortfolioBook(portfolio_id=pid)
    book.config = PortfolioConfig(enabled=enabled)
    if halted:
        if pid == "main":
            pass  # main reads tg._trading_halted; we test val/gene instead
        else:
            book._trading_halted = True
    return book


def test_strip_state_disabled(smoke_modules):
    """_build_portfolio_strip returns 'disabled' when config.enabled=False."""
    tg, ds = smoke_modules
    book = _make_book(pid="val", enabled=False)
    strip = ds._build_portfolio_strip(book, executor=None)
    assert strip["state"] == "disabled", f"expected disabled, got {strip['state']!r}"


def test_strip_state_halted_daily_loss(smoke_modules):
    """_build_portfolio_strip returns 'halted_daily_loss' when book.daily_halted()."""
    tg, ds = smoke_modules
    book = _make_book(pid="gene", enabled=True)
    # Force daily halt by setting the scalar on the book.
    book._trading_halted = True
    strip = ds._build_portfolio_strip(book, executor=None)
    assert strip["state"] == "halted_daily_loss", (
        f"expected halted_daily_loss, got {strip['state']!r}"
    )


def test_strip_state_paused_for_main(smoke_modules, monkeypatch):
    """_build_portfolio_strip returns 'paused' for main when tg._scan_paused."""
    tg, ds = smoke_modules
    monkeypatch.setattr(tg, "_scan_paused", True, raising=False)
    book = _make_book(pid="main", enabled=True)
    strip = ds._build_portfolio_strip(book, executor=None)
    assert strip["state"] == "paused", f"expected paused, got {strip['state']!r}"


# ---------------------------------------------------------------------------
# Tests 9\u201310: back-compat legacy \"portfolio\" key
# ---------------------------------------------------------------------------

def test_legacy_portfolio_key_present(smoke_modules):
    """Legacy 'portfolio' key must still exist at top-level for back-compat."""
    tg, ds = smoke_modules
    snap = ds.snapshot()
    assert "portfolio" in snap, "missing legacy 'portfolio' key in /api/state"


def test_legacy_portfolio_equity_matches_portfolios_main(smoke_modules):
    """Legacy portfolio.equity must equal portfolios.main.equity."""
    tg, ds = smoke_modules
    snap = ds.snapshot()
    legacy_equity = snap["portfolio"]["equity"]
    main_equity = snap["portfolios"]["main"]["equity"]
    assert legacy_equity == main_equity, (
        f"equity mismatch: legacy={legacy_equity}, portfolios.main={main_equity}"
    )


# ---------------------------------------------------------------------------
# Test 11: _build_portfolio_block handles None executor
# ---------------------------------------------------------------------------

def test_build_portfolio_block_none_executor(smoke_modules):
    """_build_portfolio_block(val_book, executor=None) must not raise."""
    tg, ds = smoke_modules
    from engine.portfolio_book import PortfolioBook
    book = PortfolioBook(portfolio_id="val")
    block = ds._build_portfolio_block(book, executor=None, prices={})
    assert block["portfolio_id"] == "val"
    assert "strip" in block
    assert "equity" in block


# ---------------------------------------------------------------------------
# Tests 12\u201316: _compute_trail_pill_state
# ---------------------------------------------------------------------------

def test_trail_pill_none_no_stop(smoke_modules):
    """_compute_trail_pill_state returns None when no effective_stop."""
    tg, ds = smoke_modules
    result = ds._compute_trail_pill_state({"mark": 150.0, "side": "LONG"})
    assert result is None, f"expected None, got {result}"


def test_trail_pill_armed_long(smoke_modules):
    """_compute_trail_pill_state returns 'armed' for LONG when mark > stop."""
    tg, ds = smoke_modules
    result = ds._compute_trail_pill_state({
        "effective_stop": 148.0,
        "mark": 152.0,
        "side": "LONG",
        "v644_hold_seconds": 0,
    })
    assert result is not None
    assert result["status"] == "armed", f"expected armed, got {result}"
    assert result["hold_remaining_sec"] is None


def test_trail_pill_armed_short(smoke_modules):
    """_compute_trail_pill_state returns 'armed' for SHORT when mark < stop."""
    tg, ds = smoke_modules
    result = ds._compute_trail_pill_state({
        "effective_stop": 505.0,
        "mark": 498.0,
        "side": "SHORT",
        "v644_hold_seconds": 0,
    })
    assert result is not None
    assert result["status"] == "armed", f"expected armed for SHORT below stop, got {result}"


def test_trail_pill_breached_hold(smoke_modules):
    """_compute_trail_pill_state returns 'breached_hold' when hold > 0."""
    tg, ds = smoke_modules
    result = ds._compute_trail_pill_state({
        "effective_stop": 150.0,
        "mark": 147.0,   # below stop \u2014 breached
        "side": "LONG",
        "v644_hold_seconds": 180,
    })
    assert result is not None
    assert result["status"] == "breached_hold", f"expected breached_hold, got {result}"
    assert result["hold_remaining_sec"] == 180


def test_trail_pill_breached_firing(smoke_modules):
    """_compute_trail_pill_state returns 'breached_firing' when hold == 0."""
    tg, ds = smoke_modules
    result = ds._compute_trail_pill_state({
        "effective_stop": 150.0,
        "mark": 148.5,   # below stop \u2014 breached
        "side": "LONG",
        "v644_hold_seconds": 0,
    })
    assert result is not None
    assert result["status"] == "breached_firing", f"expected breached_firing, got {result}"
    assert result["hold_remaining_sec"] == 0


# ---------------------------------------------------------------------------
# Test 17: best-effort \u2014 bad book doesn't crash the map
# ---------------------------------------------------------------------------

def test_portfolios_map_best_effort(smoke_modules, monkeypatch):
    """A crash inside one book's block must not prevent others from building."""
    tg, ds = smoke_modules

    original_build = ds._build_portfolio_block

    call_count = [0]
    def _boom(book, executor=None, prices=None):
        call_count[0] += 1
        pid = getattr(book, "portfolio_id", "?")
        if pid == "val":
            raise RuntimeError("simulated val executor failure")
        return original_build(book, executor=executor, prices=prices)

    monkeypatch.setattr(ds, "_build_portfolio_block", _boom)
    result = ds._build_portfolios_map(prices={})
    # All three keys must still exist.
    assert set(result.keys()) == {"main", "val", "gene"}, (
        f"missing portfolios keys after best-effort: {set(result.keys())}"
    )
    # val block should be the stub.
    val_block = result["val"]
    assert val_block["portfolio_id"] == "val"
    # main and gene should be normally built (no stub state).
    assert result["main"]["portfolio_id"] == "main"
    assert result["gene"]["portfolio_id"] == "gene"
