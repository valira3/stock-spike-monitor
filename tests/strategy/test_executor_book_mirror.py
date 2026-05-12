"""v8.2.0 -- executor PortfolioBook mirror tests.

Closes the inv_position_count_three_way "phantom at broker" alert:
boot paths (state.db rehydrate + Alpaca reconcile) now mirror each
populated executor.positions row into PORTFOLIOS[<pid>].positions
so the dashboard's per-portfolio positions feed isn't empty after
a redeploy.
"""
import sys
from types import ModuleType
from unittest.mock import MagicMock

# Reuse the telegram/alpaca module stubs from test_executor_partial_close.
if "telegram" not in sys.modules:
    _tel = ModuleType("telegram")
    for _name in ("BotCommand", "BotCommandScopeAllPrivateChats", "Update"):
        setattr(_tel, _name, type(_name, (), {}))
    sys.modules["telegram"] = _tel
    _tel_ext = ModuleType("telegram.ext")
    for _name in ("Application", "ApplicationHandlerStop", "CommandHandler",
                  "TypeHandler"):
        setattr(_tel_ext, _name, type(_name, (), {}))
    sys.modules["telegram.ext"] = _tel_ext

import pytest

from executors.base import TradeGeniusBase


class _FakeExec(TradeGeniusBase):
    NAME = "Val"
    mode = "paper"
    DEFAULT_OWNERS = set()

    def __init__(self):
        self.client = None
        self.positions = {}
        self.telegram_token = ""
        self._owner_chats = {}
        self._last_action_ts = {}
        self._persisted_positions = {}

    def _persist_position(self, ticker):
        # Stub: don't hit state.db in tests.
        pass


@pytest.fixture(autouse=True)
def reset_books():
    """PORTFOLIOS is a module-level singleton across tests; clear the
    Val and Gene books before each test so previous test state can't
    leak."""
    from engine.portfolio_book import PORTFOLIOS
    for pid in ("val", "gene"):
        book = PORTFOLIOS.get(pid)
        if book is not None:
            book.positions.clear()
            book.short_positions.clear()
    yield


class TestMirrorPositionIntoBook:

    def test_mirror_long_into_book(self):
        from engine.portfolio_book import PORTFOLIOS
        ex = _FakeExec()
        ex.positions["AAPL"] = {
            "ticker": "AAPL", "side": "LONG", "qty": 100,
            "entry_price": 150.25, "entry_ts_utc": "2026-05-12T10:00:00Z",
            "source": "RECONCILE", "stop": 149.0,
        }
        ex._mirror_position_into_book("AAPL")
        book = PORTFOLIOS.get("val")
        assert "AAPL" in book.positions
        row = book.positions["AAPL"]
        # Field mapping: qty -> shares
        assert row["shares"] == 100
        assert row["entry_price"] == 150.25
        assert row["source"] == "RECONCILE"
        assert row["stop"] == 149.0

    def test_mirror_short_into_short_book(self):
        from engine.portfolio_book import PORTFOLIOS
        ex = _FakeExec()
        ex.positions["AAPL"] = {
            "ticker": "AAPL", "side": "SHORT", "qty": 50,
            "entry_price": 150.0, "source": "RECONCILE",
        }
        ex._mirror_position_into_book("AAPL")
        book = PORTFOLIOS.get("val")
        # Goes into short_positions, NOT positions
        assert "AAPL" in book.short_positions
        assert "AAPL" not in book.positions
        assert book.short_positions["AAPL"]["shares"] == 50

    def test_mirror_idempotent_does_not_clobber_existing_row(self):
        """If the book already has a richer row (e.g. from a live
        record_entry_with_fill), mirror should NOT overwrite it."""
        from engine.portfolio_book import PORTFOLIOS
        ex = _FakeExec()
        # Pre-seed the book with a "live" row carrying state we
        # shouldn't lose.
        book = PORTFOLIOS.get("val")
        book.positions["AAPL"] = {
            "ticker": "AAPL", "shares": 200, "entry_price": 145.0,
            "source": "LIVE_FILL", "stop": 144.0,
            "v531_max_favorable_price": 155.0,
        }
        # Executor has a stale RECONCILE-shaped row.
        ex.positions["AAPL"] = {
            "ticker": "AAPL", "side": "LONG", "qty": 50,
            "entry_price": 100.0, "source": "RECONCILE", "stop": None,
        }
        ex._mirror_position_into_book("AAPL")
        # Book row preserved -- shares=200, source=LIVE_FILL,
        # tracking state intact.
        assert book.positions["AAPL"]["shares"] == 200
        assert book.positions["AAPL"]["source"] == "LIVE_FILL"
        assert book.positions["AAPL"]["v531_max_favorable_price"] == 155.0

    def test_mirror_no_pos_no_op(self):
        ex = _FakeExec()
        # AAPL not in self.positions
        ex._mirror_position_into_book("AAPL")
        from engine.portfolio_book import PORTFOLIOS
        book = PORTFOLIOS.get("val")
        assert "AAPL" not in book.positions
        assert "AAPL" not in book.short_positions

    def test_mirror_unknown_pid_no_crash(self):
        """If self.NAME.lower() isn't a known portfolio, mirror should
        silently no-op rather than raise."""
        ex = _FakeExec()
        ex.NAME = "UnknownPid"
        ex.positions["AAPL"] = {
            "ticker": "AAPL", "side": "LONG", "qty": 100,
            "entry_price": 150.0, "source": "RECONCILE",
        }
        # No exception
        ex._mirror_position_into_book("AAPL")


class TestLoadPersistedPositionsCallsMirror:
    """v8.2.0 -- the state.db rehydrate path (boot, after Railway
    redeploy) should mirror each loaded row into the PortfolioBook."""

    def test_load_persisted_mirrors_into_book(self, monkeypatch):
        from engine.portfolio_book import PORTFOLIOS
        import persistence as _p
        # Stub persistence.load_executor_positions to return a row.
        fake_rows = {
            "AAPL": {
                "ticker": "AAPL", "side": "LONG", "qty": 75,
                "entry_price": 150.0, "source": "RECONCILE",
            },
        }
        monkeypatch.setattr(_p, "load_executor_positions",
                            lambda name, mode: fake_rows)
        ex = _FakeExec()
        ex._load_persisted_positions()
        # self.positions populated AND book mirrored
        assert "AAPL" in ex.positions
        book = PORTFOLIOS.get("val")
        assert "AAPL" in book.positions
        assert book.positions["AAPL"]["shares"] == 75
