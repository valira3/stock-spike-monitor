"""Tests for engine.portfolio_equity -- the v7.76.0 single-source-of-
truth equity helper used by RiskBook seeding for Val/Gene."""
from __future__ import annotations

import os

import pytest

from engine import portfolio_equity as pe


@pytest.fixture(autouse=True)
def reset_cache():
    pe._ALPACA_ACCT_CACHE.clear()
    yield
    pe._ALPACA_ACCT_CACHE.clear()


class TestAlpacaAccountForBook:

    def test_returns_none_for_empty_pid(self):
        assert pe.alpaca_account_for_book("") is None
        assert pe.alpaca_account_for_book(None) is None  # type: ignore

    def test_returns_none_for_main(self):
        """main path reads tg.paper_cash directly; alpaca lookup not applicable."""
        assert pe.alpaca_account_for_book("main") is None
        assert pe.alpaca_account_for_book("MAIN") is None

    def test_returns_none_when_env_missing(self, monkeypatch):
        for k in list(os.environ):
            if "VAL_ALPACA" in k or "GENE_ALPACA" in k:
                monkeypatch.delenv(k, raising=False)
        assert pe.alpaca_account_for_book("val") is None

    def test_returns_none_when_creds_trivially_short(self, monkeypatch):
        monkeypatch.setenv("VAL_ALPACA_PAPER_KEY", "abc")
        monkeypatch.setenv("VAL_ALPACA_PAPER_SECRET", "short")
        assert pe.alpaca_account_for_book("val") is None


class TestResolveEquity:

    def test_main_returns_tg_paper_cash(self, monkeypatch):
        """Main pulls from tg.paper_cash. We patch the import to a stub."""
        import sys
        import types
        stub = types.ModuleType("trade_genius")
        stub.paper_cash = 88_888.0
        monkeypatch.setitem(sys.modules, "trade_genius", stub)
        assert pe.resolve_equity("main") == 88_888.0
        assert pe.resolve_equity("MAIN") == 88_888.0
        assert pe.resolve_equity("") == 88_888.0

    def test_main_falls_back_to_default_on_import_error(self, monkeypatch):
        import sys
        # Make `import trade_genius` raise inside resolve_equity by
        # putting a falsy sentinel that triggers AttributeError.
        broken = type("brk", (), {})()  # has no paper_cash attr
        monkeypatch.setitem(sys.modules, "trade_genius", broken)
        # AttributeError path goes through the except -> default
        assert pe.resolve_equity("main", default_main_equity=12345.0) == 12345.0

    def test_val_uses_alpaca_account_when_available(self, monkeypatch):
        # Stub the alpaca_account_for_book to return a known equity.
        monkeypatch.setattr(
            pe, "alpaca_account_for_book",
            lambda pid: {"equity": 99273.10} if pid == "val" else None,
        )
        assert pe.resolve_equity("val") == 99273.10

    def test_val_falls_back_to_book_when_alpaca_missing(self, monkeypatch):
        """When Alpaca creds are missing/broken, fall back to
        PortfolioBook.current_equity()."""
        monkeypatch.setattr(pe, "alpaca_account_for_book", lambda pid: None)

        class FakeBook:
            def current_equity(self):
                return 50_000.0

        import sys
        import types
        stub_pb = types.ModuleType("engine.portfolio_book")
        stub_pb.PORTFOLIOS = {"val": FakeBook()}
        monkeypatch.setitem(sys.modules, "engine.portfolio_book", stub_pb)
        assert pe.resolve_equity("val") == 50_000.0

    def test_returns_zero_when_alpaca_returns_zero_and_book_unavailable(self, monkeypatch):
        monkeypatch.setattr(
            pe, "alpaca_account_for_book",
            lambda pid: {"equity": 0.0},
        )
        import sys
        import types
        stub_pb = types.ModuleType("engine.portfolio_book")
        stub_pb.PORTFOLIOS = {}  # val not registered
        monkeypatch.setitem(sys.modules, "engine.portfolio_book", stub_pb)
        # Alpaca says 0, book.PORTFOLIOS.get('val') is None
        # -> falls through to return 0.0
        assert pe.resolve_equity("val") == 0.0
