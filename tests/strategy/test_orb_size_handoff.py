"""Tests for v7.18.0 v10-to-broker sizing handoff.

orb.live_runtime exposes stash_v10_size + consume_v10_size, which
broker/orders.paper_shares_for now consults when ORB_LIVE_MODE=1.

These tests verify the stash mechanics + the paper_shares_for
override path. The full integration (engine/scan.py:_orb_long_entry
stashes -> execute_breakout reads) is tested in scan-integration.
"""
from __future__ import annotations

import os

import pytest

from orb import live_runtime


@pytest.fixture(autouse=True)
def reset_runtime():
    live_runtime._reset_for_testing()
    live_runtime._pending_v10_sizes.clear()
    yield
    live_runtime._pending_v10_sizes.clear()
    live_runtime._reset_for_testing()


@pytest.fixture
def isolated_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("ORB_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ORB_PARTIAL_PROFIT_AT_1R", "0")  # v8.1.3 legacy default
    yield monkeypatch


class TestSizeStash:

    def test_stash_then_consume(self, isolated_env):
        live_runtime.stash_v10_size("main", "AAPL", 250)
        assert live_runtime.peek_v10_size("main", "AAPL") == 250
        assert live_runtime.consume_v10_size("main", "AAPL") == 250
        assert live_runtime.consume_v10_size("main", "AAPL") is None

    def test_consume_unknown_returns_none(self, isolated_env):
        assert live_runtime.consume_v10_size("main", "ZZZZ") is None

    def test_independent_per_portfolio(self, isolated_env):
        live_runtime.stash_v10_size("main", "AAPL", 100)
        live_runtime.stash_v10_size("val", "AAPL", 50)
        live_runtime.stash_v10_size("gene", "AAPL", 25)
        assert live_runtime.consume_v10_size("main", "AAPL") == 100
        assert live_runtime.consume_v10_size("val", "AAPL") == 50
        assert live_runtime.consume_v10_size("gene", "AAPL") == 25

    def test_independent_per_ticker(self, isolated_env):
        live_runtime.stash_v10_size("main", "AAPL", 100)
        live_runtime.stash_v10_size("main", "NVDA", 50)
        assert live_runtime.consume_v10_size("main", "AAPL") == 100
        assert live_runtime.consume_v10_size("main", "NVDA") == 50

    def test_stash_overwrites(self, isolated_env):
        live_runtime.stash_v10_size("main", "AAPL", 100)
        live_runtime.stash_v10_size("main", "AAPL", 200)
        assert live_runtime.consume_v10_size("main", "AAPL") == 200


class TestPaperSharesForHandoff:

    def test_uses_v10_stash_when_live_mode_on(self, isolated_env):
        from broker.orders import paper_shares_for
        live_runtime.stash_v10_size("main", "AAPL", 250)
        result = paper_shares_for(price=100.0, ticker="AAPL")
        assert result == 250

    def test_consumes_stash_one_shot(self, isolated_env):
        from broker.orders import paper_shares_for
        live_runtime.stash_v10_size("main", "AAPL", 250)
        first = paper_shares_for(price=100.0, ticker="AAPL")
        assert first == 250
        second = paper_shares_for(price=100.0, ticker="AAPL")
        legacy_no_ticker = paper_shares_for(price=100.0)
        assert second == legacy_no_ticker

    def test_skips_v10_when_kill_switch_off(self, isolated_env):
        isolated_env.setenv("ORB_LIVE_MODE", "0")
        from broker.orders import paper_shares_for
        live_runtime.stash_v10_size("main", "AAPL", 250)
        result = paper_shares_for(price=100.0, ticker="AAPL")
        legacy = paper_shares_for(price=100.0)
        assert result == legacy
        assert live_runtime.peek_v10_size("main", "AAPL") == 250

    def test_no_ticker_uses_legacy(self, isolated_env):
        from broker.orders import paper_shares_for
        live_runtime.stash_v10_size("main", "AAPL", 250)
        result = paper_shares_for(price=100.0)
        assert live_runtime.peek_v10_size("main", "AAPL") == 250
        assert isinstance(result, int)
        assert result >= 1

    def test_invalid_price_returns_zero(self, isolated_env):
        from broker.orders import paper_shares_for
        assert paper_shares_for(price=0.0, ticker="AAPL") == 0
        assert paper_shares_for(price=-1.0, ticker="AAPL") == 0
