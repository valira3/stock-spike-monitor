"""tests/test_v700_phase1_book_facade.py \u2014 v7.0.0 Phase 1 regression tests.

Verifies that _MAIN_BOOK's mutable attributes share identity with the
corresponding module-level globals in trade_genius, so mutations through
either path are immediately visible through the other.

Phase 1 spec reference: docs/specs/v7_0_0_spec.md, Migration Phase 1.

Audit fix \u2014 historically the module-level `import trade_genius as tg`
captured a stale reference whenever earlier tests (e.g. test_v700_aon)
re-executed `trade_genius.py`. The PORTFOLIOS singleton in
engine.portfolio_book is then rebound to the NEW module's dicts, leaving
the module-level `tg` here pointing at the OLD module whose `.positions`
no longer matches `_MAIN_BOOK.positions`. We fix it here by resolving
`tg` per-test through `sys.modules`, so identity assertions always
compare current-generation objects.
"""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("TELEGRAM_TOKEN", "0000000000:AAAA_smoke_placeholder_token_0000000")
os.environ.setdefault("CHAT_ID", "999999999")
os.environ.setdefault("DASHBOARD_PASSWORD", "smoketest1234")
os.environ.setdefault("FMP_API_KEY", "fake_fmp_key_for_tests")

import trade_genius  # noqa: E402,F401  (ensure import path warm)


@pytest.fixture
def tg():
    """Resolve trade_genius from sys.modules each test, so identity checks
    always compare against the current-generation module object.
    """
    mod = sys.modules.get("trade_genius")
    if mod is None:
        import trade_genius as _tg  # noqa: F401
        mod = sys.modules["trade_genius"]
    return mod


@pytest.fixture
def PortfolioBook():
    """Resolve PortfolioBook from sys.modules each test for the same reason.
    Cross-module re-imports can give us a stale class reference otherwise.
    """
    mod = sys.modules.get("engine.portfolio_book")
    if mod is None:
        from engine.portfolio_book import PortfolioBook as _PB  # noqa: F401
        mod = sys.modules["engine.portfolio_book"]
    return mod.PortfolioBook


def test_main_book_portfolio_id(tg) -> None:
    """_MAIN_BOOK.portfolio_id must equal 'main'."""
    assert tg._MAIN_BOOK.portfolio_id == "main"


def test_main_book_is_portfolio_book_instance(tg, PortfolioBook) -> None:
    """_MAIN_BOOK must be an instance of PortfolioBook."""
    assert isinstance(tg._MAIN_BOOK, PortfolioBook)


def test_positions_identity(tg) -> None:
    """_MAIN_BOOK.positions must be the identical object as tg.positions."""
    assert tg._MAIN_BOOK.positions is tg.positions


def test_short_positions_identity(tg) -> None:
    """_MAIN_BOOK.short_positions must be the identical object as tg.short_positions."""
    assert tg._MAIN_BOOK.short_positions is tg.short_positions


def test_trade_history_identity(tg) -> None:
    """_MAIN_BOOK.trade_history must be the identical object as tg.trade_history."""
    assert tg._MAIN_BOOK.trade_history is tg.trade_history


def test_short_trade_history_identity(tg) -> None:
    """_MAIN_BOOK.short_trade_history must be the identical object as tg.short_trade_history."""
    assert tg._MAIN_BOOK.short_trade_history is tg.short_trade_history


def test_v5_long_tracks_identity(tg) -> None:
    """_MAIN_BOOK.v5_long_tracks must be the identical object as tg.v5_long_tracks."""
    assert tg._MAIN_BOOK.v5_long_tracks is tg.v5_long_tracks


def test_v5_short_tracks_identity(tg) -> None:
    """_MAIN_BOOK.v5_short_tracks must be the identical object as tg.v5_short_tracks."""
    assert tg._MAIN_BOOK.v5_short_tracks is tg.v5_short_tracks


def test_v5_active_direction_identity(tg) -> None:
    """_MAIN_BOOK.v5_active_direction must be the identical object as tg.v5_active_direction."""
    assert tg._MAIN_BOOK.v5_active_direction is tg.v5_active_direction


def test_mutation_through_book_visible_on_module(tg) -> None:
    """Mutating a position through the book must be visible via tg.positions."""
    orig = dict(tg.positions)
    try:
        tg._MAIN_BOOK.positions["TEST"] = {"foo": 1}
        assert tg.positions["TEST"] == {"foo": 1}
    finally:
        tg.positions.pop("TEST", None)
        # Restore any entries that were present before the test
        tg.positions.clear()
        tg.positions.update(orig)


def test_mutation_through_module_visible_on_book(tg) -> None:
    """Mutating tg.positions directly must be visible via _MAIN_BOOK.positions."""
    orig = dict(tg.positions)
    try:
        tg.positions["TEST2"] = {"bar": 2}
        assert tg._MAIN_BOOK.positions["TEST2"] == {"bar": 2}
    finally:
        tg.positions.pop("TEST2", None)
        tg.positions.clear()
        tg.positions.update(orig)


def test_daily_entry_count_identity(tg) -> None:
    """_MAIN_BOOK.daily_entry_count must be the identical object as tg.daily_entry_count."""
    assert tg._MAIN_BOOK.daily_entry_count is tg.daily_entry_count


def test_daily_short_entry_count_identity(tg) -> None:
    """_MAIN_BOOK.daily_short_entry_count must be identical to tg.daily_short_entry_count."""
    assert tg._MAIN_BOOK.daily_short_entry_count is tg.daily_short_entry_count


def test_paper_trades_identity(tg) -> None:
    """_MAIN_BOOK.paper_trades must be the identical object as tg.paper_trades."""
    assert tg._MAIN_BOOK.paper_trades is tg.paper_trades


def test_paper_all_trades_identity(tg) -> None:
    """_MAIN_BOOK.paper_all_trades must be the identical object as tg.paper_all_trades."""
    assert tg._MAIN_BOOK.paper_all_trades is tg.paper_all_trades
