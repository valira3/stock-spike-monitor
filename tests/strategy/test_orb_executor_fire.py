"""Tests for v7.26.0 -- TradeGeniusBase.fire_long/fire_short surface +
get_executor lookup + engine/scan.py dispatch.

These verify the wiring without requiring a live Alpaca client or
broker. We construct a minimal TradeGeniusBase subclass with stubs and
confirm:

  1. fire_long / fire_short call _submit_order_idempotent with the right
     symbol, qty, side, and a V10LONG/V10SHORT-tagged client_order_id.
  2. fire_long no-ops on shares <= 0, missing client, missing ticker.
  3. get_executor("main") returns None; get_executor("val") returns the
     registered executor instance; get_executor("nonexistent") returns
     None.
  4. _v10_dispatch_executor_fire honors ORB_PORTFOLIO_FIRE env flag:
     off -> deferred log, no fire; on -> fire_* called.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


# Skip the whole module gracefully if alpaca isn't importable in the
# sandbox; the production CI image has alpaca installed.
alpaca_trading = pytest.importorskip("alpaca.trading.requests")


@pytest.fixture
def stub_client():
    """A mock Alpaca trading client that records submit_order calls."""
    c = MagicMock()
    # Return an order with an id when submit_order is called
    fake_order = MagicMock()
    fake_order.id = "order-abc-123"
    c.submit_order.return_value = fake_order
    return c


def _make_stub_base(name: str = "VAL", env_prefix: str = "VAL_"):
    """Build a TradeGeniusBase instance with all I/O stubbed.

    Skips the heavy __init__ via __new__ + minimal attribute hand-set.
    """
    from executors.base import TradeGeniusBase

    inst = TradeGeniusBase.__new__(TradeGeniusBase)
    inst.NAME = name
    inst.ENV_PREFIX = env_prefix
    inst.mode = "paper"
    inst.client = None
    inst.positions = {}
    inst.short_positions = {}
    inst._aon_mode = "software"
    return inst


# ----- fire_long / fire_short ------------------------------------


class TestFireLong:

    def test_submits_market_buy_with_v10_coid(self, stub_client):
        inst = _make_stub_base()
        inst._ensure_client = MagicMock(return_value=stub_client)
        inst._submit_order_idempotent = MagicMock(
            wraps=lambda c, req, coid: stub_client.submit_order(req))
        inst._record_position = MagicMock()
        inst._build_client_order_id = MagicMock(
            return_value="VAL-AAPL-20260510T1430-V10LONG")

        ok = inst.fire_long("AAPL", price=100.50, shares=50)
        assert ok is True
        inst._build_client_order_id.assert_called_once_with(
            "AAPL", "V10LONG")
        # Verify submit_order_idempotent received a MarketOrderRequest
        # for AAPL/50/BUY with the v10 coid.
        args, _ = inst._submit_order_idempotent.call_args
        client, req, coid = args
        assert client is stub_client
        assert coid == "VAL-AAPL-20260510T1430-V10LONG"
        assert req.symbol == "AAPL"
        assert int(req.qty) == 50
        # Direction enum -> string
        assert "BUY" in str(req.side).upper()
        inst._record_position.assert_called_once_with(
            "AAPL", "LONG", 50, 100.50)

    def test_returns_false_on_zero_shares(self):
        inst = _make_stub_base()
        inst._ensure_client = MagicMock()  # should not be called
        ok = inst.fire_long("AAPL", price=100.0, shares=0)
        assert ok is False
        inst._ensure_client.assert_not_called()

    def test_returns_false_on_no_client(self):
        inst = _make_stub_base()
        inst._ensure_client = MagicMock(return_value=None)
        ok = inst.fire_long("AAPL", price=100.0, shares=10)
        assert ok is False

    def test_returns_false_on_empty_ticker(self):
        inst = _make_stub_base()
        inst._ensure_client = MagicMock()
        ok = inst.fire_long("", price=100.0, shares=10)
        assert ok is False
        inst._ensure_client.assert_not_called()

    def test_swallows_submit_exception(self, stub_client):
        inst = _make_stub_base()
        inst._ensure_client = MagicMock(return_value=stub_client)
        inst._submit_order_idempotent = MagicMock(
            side_effect=RuntimeError("simulated alpaca failure"))
        inst._build_client_order_id = MagicMock(return_value="VAL-COID")
        inst._record_position = MagicMock()
        ok = inst.fire_long("AAPL", price=100.0, shares=10)
        assert ok is False
        inst._record_position.assert_not_called()


class TestFireShort:

    def test_submits_market_sell_with_v10_coid(self, stub_client):
        inst = _make_stub_base()
        inst._ensure_client = MagicMock(return_value=stub_client)
        inst._submit_order_idempotent = MagicMock(
            wraps=lambda c, req, coid: stub_client.submit_order(req))
        inst._record_position = MagicMock()
        inst._build_client_order_id = MagicMock(
            return_value="VAL-AAPL-20260510T1430-V10SHORT")

        ok = inst.fire_short("AAPL", price=99.50, shares=25)
        assert ok is True
        inst._build_client_order_id.assert_called_once_with(
            "AAPL", "V10SHORT")
        args, _ = inst._submit_order_idempotent.call_args
        _client, req, coid = args
        assert coid == "VAL-AAPL-20260510T1430-V10SHORT"
        assert req.symbol == "AAPL"
        assert int(req.qty) == 25
        assert "SELL" in str(req.side).upper()
        inst._record_position.assert_called_once_with(
            "AAPL", "SHORT", 25, 99.50)


# ----- get_executor lookup ---------------------------------------


class TestGetExecutor:

    def test_main_returns_none(self):
        from executors.bootstrap import get_executor
        assert get_executor("main") is None

    def test_empty_returns_none(self):
        from executors.bootstrap import get_executor
        assert get_executor("") is None

    def test_returns_registered_val(self, monkeypatch):
        from executors.bootstrap import get_executor
        sentinel = object()
        # Stub trade_genius module attribute lookup
        import sys
        fake = MagicMock()
        fake.val_executor = sentinel
        fake.gene_executor = None
        monkeypatch.setitem(sys.modules, "trade_genius", fake)
        assert get_executor("val") is sentinel

    def test_unregistered_returns_none(self, monkeypatch):
        from executors.bootstrap import get_executor
        import sys
        fake = MagicMock(spec=[])  # no val_executor / gene_executor attrs
        monkeypatch.setitem(sys.modules, "trade_genius", fake)
        assert get_executor("val") is None
        assert get_executor("gene") is None
        assert get_executor("nonexistent") is None


# ----- engine/scan.py dispatch ----------------------------------


class TestV10DispatchExecutorFire:

    def _import_dispatch(self):
        # Importing engine.scan transitively imports trade_genius (via
        # callbacks). In sandbox without telegram, that fails. Skip
        # gracefully but exercise the dispatch via direct import in CI
        # (where telegram is installed).
        try:
            from engine.scan import _v10_dispatch_executor_fire
            return _v10_dispatch_executor_fire
        except (ModuleNotFoundError, ImportError) as e:
            if "telegram" in str(e):
                pytest.skip("telegram unavailable in sandbox")
            raise

    def test_off_by_default_no_fire(self, monkeypatch):
        dispatch = self._import_dispatch()
        monkeypatch.delenv("ORB_PORTFOLIO_FIRE", raising=False)
        fake_ex = MagicMock()
        with patch("executors.bootstrap.get_executor",
                   return_value=fake_ex):
            dispatch(pid="val", side="long", ticker="AAPL",
                     price=100.0, shares=10)
        fake_ex.fire_long.assert_not_called()
        fake_ex.fire_short.assert_not_called()

    def test_on_calls_fire_long(self, monkeypatch):
        # v7.30.0: dispatch now requires ORB_LIVE_MODE=1 too.
        dispatch = self._import_dispatch()
        monkeypatch.setenv("ORB_LIVE_MODE", "1")
        monkeypatch.setenv("ORB_PORTFOLIO_FIRE", "1")
        fake_ex = MagicMock()
        fake_ex.fire_long.return_value = True
        with patch("executors.bootstrap.get_executor",
                   return_value=fake_ex):
            dispatch(pid="val", side="long", ticker="AAPL",
                     price=100.0, shares=10)
        # v7.30.0: dispatch now passes error_callback=None when no
        # callbacks supplied.
        fake_ex.fire_long.assert_called_once()
        args, kwargs = fake_ex.fire_long.call_args
        assert args == ("AAPL", 100.0, 10)
        assert "error_callback" in kwargs

    def test_on_calls_fire_short(self, monkeypatch):
        dispatch = self._import_dispatch()
        monkeypatch.setenv("ORB_LIVE_MODE", "1")
        monkeypatch.setenv("ORB_PORTFOLIO_FIRE", "1")
        fake_ex = MagicMock()
        fake_ex.fire_short.return_value = True
        with patch("executors.bootstrap.get_executor",
                   return_value=fake_ex):
            dispatch(pid="gene", side="short", ticker="MSFT",
                     price=200.0, shares=5)
        fake_ex.fire_short.assert_called_once()
        args, kwargs = fake_ex.fire_short.call_args
        assert args == ("MSFT", 200.0, 5)
        assert "error_callback" in kwargs

    def test_on_no_executor_no_fire(self, monkeypatch):
        dispatch = self._import_dispatch()
        monkeypatch.setenv("ORB_PORTFOLIO_FIRE", "1")
        with patch("executors.bootstrap.get_executor",
                   return_value=None):
            # Should not raise
            dispatch(pid="val", side="long", ticker="AAPL",
                     price=100.0, shares=10)

    def test_on_swallows_executor_exception(self, monkeypatch):
        dispatch = self._import_dispatch()
        monkeypatch.setenv("ORB_PORTFOLIO_FIRE", "1")
        fake_ex = MagicMock()
        fake_ex.fire_long.side_effect = RuntimeError("boom")
        with patch("executors.bootstrap.get_executor",
                   return_value=fake_ex):
            # Should not propagate
            dispatch(pid="val", side="long", ticker="AAPL",
                     price=100.0, shares=10)
