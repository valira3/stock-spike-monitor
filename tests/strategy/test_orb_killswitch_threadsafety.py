"""Tests for v7.30.0 -- kill-switch consistency, thread safety, and
broker fire error escalation.

Three audit findings addressed:

1. ORB_LIVE_MODE gate on _v10_dispatch_executor_fire: previously the
   dispatch only gated on ORB_PORTFOLIO_FIRE, so an operator setting
   LIVE_MODE=0 to disable v10 strategy could still see Val/Gene fire
   broker orders if PORTFOLIO_FIRE was on.

2. Atomic bootstrap + thread-safe _pending_v10_sizes: bootstrap()
   uses local-then-swap so partial init doesn't leak; stash/consume/
   peek serialize on _sizes_lock so dict-mutation races can't corrupt
   state.

3. Broker fire error escalation: fire_long/fire_short accept an
   error_callback that is invoked on broker submit failures (5xx,
   timeout, etc.). Previously these were logged but never surfaced
   to Telegram/dashboard. The scan-side dispatch wires this to
   callbacks.report_error.
"""

from __future__ import annotations

import os
import threading
from unittest.mock import MagicMock, patch

import pytest

from orb import live_runtime


@pytest.fixture(autouse=True)
def reset_runtime():
    live_runtime._reset_for_testing()
    yield
    live_runtime._reset_for_testing()


@pytest.fixture
def isolated_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("ORB_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ORB_PARTIAL_PROFIT_AT_1R", "0")  # v8.1.3 legacy default
    yield monkeypatch


alpaca_trading = pytest.importorskip("alpaca.trading.requests")


# ----- Kill-switch consistency (ORB_LIVE_MODE gate on dispatch) -----


class TestKillSwitchOnDispatch:
    def _import_dispatch(self):
        try:
            from engine.scan import _v10_dispatch_executor_fire

            return _v10_dispatch_executor_fire
        except (ModuleNotFoundError, ImportError) as e:
            if "telegram" in str(e):
                pytest.skip("telegram unavailable in sandbox")
            raise

    def test_live_mode_off_suppresses_fire(self, isolated_env):
        dispatch = self._import_dispatch()
        isolated_env.setenv("ORB_LIVE_MODE", "0")
        isolated_env.setenv("ORB_PORTFOLIO_FIRE", "1")
        fake_ex = MagicMock()
        with patch("executors.bootstrap.get_executor", return_value=fake_ex):
            dispatch(pid="val", side="long", ticker="AAPL", price=100.0, shares=10)
        fake_ex.fire_long.assert_not_called()
        fake_ex.fire_short.assert_not_called()

    def test_live_mode_on_and_portfolio_fire_on_fires(self, isolated_env):
        dispatch = self._import_dispatch()
        isolated_env.setenv("ORB_LIVE_MODE", "1")
        isolated_env.setenv("ORB_PORTFOLIO_FIRE", "1")
        fake_ex = MagicMock()
        fake_ex.fire_long.return_value = True
        with patch("executors.bootstrap.get_executor", return_value=fake_ex):
            dispatch(pid="val", side="long", ticker="AAPL", price=100.0, shares=10)
        fake_ex.fire_long.assert_called_once()

    def test_portfolio_fire_off_overrides_live_mode_on(self, isolated_env):
        dispatch = self._import_dispatch()
        isolated_env.setenv("ORB_LIVE_MODE", "1")
        isolated_env.setenv("ORB_PORTFOLIO_FIRE", "0")
        fake_ex = MagicMock()
        with patch("executors.bootstrap.get_executor", return_value=fake_ex):
            dispatch(pid="val", side="long", ticker="AAPL", price=100.0, shares=10)
        fake_ex.fire_long.assert_not_called()


# ----- Atomic bootstrap ----------------------------------------------


class TestAtomicBootstrap:
    def test_partial_init_does_not_leak_engine(self, isolated_env, monkeypatch):
        """If LiveAdapterRegistry constructor raises after OrbEngine
        succeeds, the module should NOT have _engine set with no
        adapters -- _bootstrapped stays False and _engine is None.
        """
        from orb import live_runtime as lr
        from orb import live_adapter as la

        original_registry = la.LiveAdapterRegistry

        class _BoomRegistry:
            def __init__(self, *a, **kw):
                raise RuntimeError("simulated adapter init failure")

        monkeypatch.setattr(la, "LiveAdapterRegistry", _BoomRegistry)
        # Also patch the import inside live_runtime so bootstrap sees
        # the boom version.
        monkeypatch.setattr(lr, "LiveAdapterRegistry", _BoomRegistry)

        with pytest.raises(RuntimeError, match="simulated"):
            lr.bootstrap()

        # Atomic: nothing committed
        assert lr._bootstrapped is False
        assert lr._engine is None
        assert lr._adapters is None

    def test_concurrent_bootstrap_calls_serialize(self, isolated_env):
        """Two threads calling bootstrap() simultaneously should both
        complete; subsequent state is consistent (_bootstrapped True,
        single _engine instance)."""
        from orb import live_runtime as lr

        barrier = threading.Barrier(2)
        results = []

        def _go():
            barrier.wait()
            lr.bootstrap()
            results.append(id(lr._engine))

        t1 = threading.Thread(target=_go)
        t2 = threading.Thread(target=_go)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert lr._bootstrapped is True
        assert lr._engine is not None
        # Both threads see the same engine
        assert results[0] == results[1]


# ----- Thread-safe _pending_v10_sizes ------------------------------


class TestSizesLock:
    def test_stash_and_consume_basic(self, isolated_env):
        live_runtime.bootstrap()
        live_runtime.stash_v10_size("main", "AAPL", 100)
        assert live_runtime.peek_v10_size("main", "AAPL") == 100
        assert live_runtime.consume_v10_size("main", "AAPL") == 100
        # Pop was atomic; second consume returns None
        assert live_runtime.consume_v10_size("main", "AAPL") is None

    def test_concurrent_stash_consume_does_not_crash(self, isolated_env):
        """Pound the stash/consume API from many threads. With the lock
        in place there should be no RuntimeError from dict-mutation
        and each consumed value (if any) should be an int."""
        live_runtime.bootstrap()
        errors = []

        def stasher():
            for i in range(200):
                try:
                    live_runtime.stash_v10_size("main", f"T{i}", i + 1)
                except Exception as e:
                    errors.append(("stash", e))

        def consumer():
            for i in range(200):
                try:
                    v = live_runtime.consume_v10_size("main", f"T{i}")
                    if v is not None and not isinstance(v, int):
                        errors.append(("type", v))
                except Exception as e:
                    errors.append(("consume", e))

        threads = [threading.Thread(target=stasher) for _ in range(3)] + [
            threading.Thread(target=consumer) for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"unexpected errors under concurrent load: {errors}"


# ----- Error escalation -------------------------------------------


def _make_stub_base():
    from executors.base import TradeGeniusBase

    inst = TradeGeniusBase.__new__(TradeGeniusBase)
    inst.NAME = "VAL"
    inst.ENV_PREFIX = "VAL_"
    inst.mode = "paper"
    inst.client = None
    inst.positions = {}
    inst.short_positions = {}
    inst._aon_mode = "software"
    return inst


def _stub_client_with_cash(cash: float = 200_000.0) -> MagicMock:
    """Return a mock Alpaca client whose get_account() has enough cash to
    pass the 95%-of-cash notional cap for small test orders."""
    c = MagicMock()
    fake_order = MagicMock()
    fake_order.id = "order-abc-123"
    c.submit_order.return_value = fake_order
    fake_acct = MagicMock()
    fake_acct.cash = cash
    fake_acct.equity = cash
    fake_acct.buying_power = cash * 2
    fake_acct.long_market_value = 0.0
    fake_acct.short_market_value = 0.0
    c.get_account.return_value = fake_acct
    return c


class TestFireErrorCallback:
    def test_error_callback_invoked_on_broker_exception(self):
        inst = _make_stub_base()
        fake_client = _stub_client_with_cash()
        inst._ensure_client = MagicMock(return_value=fake_client)
        inst._submit_order_idempotent = MagicMock(side_effect=RuntimeError("alpaca 503"))
        inst._build_client_order_id = MagicMock(return_value="VAL-COID")
        inst._record_position = MagicMock()

        captured = []

        def cb(name, side, ticker, shares, exc):
            captured.append((name, side, ticker, shares, type(exc).__name__))

        ok = inst.fire_long("AAPL", price=100.0, shares=50, error_callback=cb)
        assert ok is False
        assert captured == [("VAL", "LONG", "AAPL", 50, "RuntimeError")]

    def test_error_callback_failure_does_not_propagate(self):
        inst = _make_stub_base()
        fake_client = _stub_client_with_cash()
        inst._ensure_client = MagicMock(return_value=fake_client)
        inst._submit_order_idempotent = MagicMock(side_effect=RuntimeError("alpaca 503"))
        inst._build_client_order_id = MagicMock(return_value="VAL-COID")
        inst._record_position = MagicMock()

        def bad_cb(*a, **kw):
            raise ValueError("error-callback bug")

        # Should not propagate the bad_cb error; fire_long still returns False
        ok = inst.fire_long("AAPL", price=100.0, shares=50, error_callback=bad_cb)
        assert ok is False

    def test_no_callback_is_safe(self):
        inst = _make_stub_base()
        fake_client = _stub_client_with_cash()
        inst._ensure_client = MagicMock(return_value=fake_client)
        inst._submit_order_idempotent = MagicMock(side_effect=RuntimeError("alpaca 503"))
        inst._build_client_order_id = MagicMock(return_value="VAL-COID")

        ok = inst.fire_long("AAPL", price=100.0, shares=50)
        assert ok is False  # no crash without the callback

    def test_dispatch_routes_broker_error_to_report_error(self, isolated_env):
        """End-to-end: dispatch passes a callback that escalates broker
        failures via callbacks.report_error."""
        try:
            from engine.scan import _v10_dispatch_executor_fire
        except (ModuleNotFoundError, ImportError) as e:
            if "telegram" in str(e):
                pytest.skip("telegram unavailable in sandbox")
            raise
        isolated_env.setenv("ORB_LIVE_MODE", "1")
        isolated_env.setenv("ORB_PORTFOLIO_FIRE", "1")

        # Real executor stub that "fails" the submit and invokes the
        # error_callback the dispatch supplies.
        def fake_fire_long(ticker, price, shares, *, error_callback=None):
            if error_callback:
                error_callback("VAL", "LONG", ticker, shares, RuntimeError("alpaca 503"))
            return False

        fake_ex = MagicMock()
        fake_ex.fire_long.side_effect = fake_fire_long

        callbacks = MagicMock()
        with patch("executors.bootstrap.get_executor", return_value=fake_ex):
            _v10_dispatch_executor_fire(
                pid="val",
                side="long",
                ticker="AAPL",
                price=100.0,
                shares=10,
                callbacks=callbacks,
            )

        callbacks.report_error.assert_called_once()
        kwargs = callbacks.report_error.call_args.kwargs
        assert kwargs["executor"] == "val"
        assert kwargs["code"] == "V10_BROKER_FIRE_FAILED"
        assert "RuntimeError" in kwargs["detail"]
        assert "503" in kwargs["detail"]
