"""v8.1.1 -- executor partial-close (live Alpaca) tests.

Covers:
  - _partial_close_position_idempotent submits a MARKET order to the
    mocked Alpaca client with the right qty and side.
  - Mutates self.positions[ticker]["qty"] only on success.
  - Refuses partial >= cur_qty (caller bug).
  - Refuses partial <= 0.
  - Treats 40410000 (position already flat) as a soft success that
    still decrements local qty.
  - Non-position-flat errors leave local qty UNCHANGED so caller can
    retry on next tick (no silent share leak).
  - _on_signal dispatches PARTIAL_EXIT_LONG / PARTIAL_EXIT_SHORT to
    the partial path with main_shares as the qty.
"""
import sys
from types import SimpleNamespace, ModuleType
from unittest.mock import MagicMock

# Sandbox doesn't ship python-telegram-bot OR alpaca-py; stub the
# imports referenced at executors/base.py module load and inside
# _partial_close_position_idempotent. Real packages are present in
# CI + production.
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

if "alpaca" not in sys.modules:
    _alpaca = ModuleType("alpaca")
    _alpaca_trading = ModuleType("alpaca.trading")
    _alpaca_trading_requests = ModuleType("alpaca.trading.requests")
    _alpaca_trading_enums = ModuleType("alpaca.trading.enums")

    # Minimal MarketOrderRequest stub: a SimpleNamespace-like that
    # records its kwargs so tests can inspect req.symbol, req.qty, etc.
    class _MarketOrderRequest:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    class _OrderSideStub:
        class _V:
            def __init__(self, name): self._n = name
            def __str__(self): return f"OrderSide.{self._n}"
        SELL = _V("SELL")
        BUY = _V("BUY")

    class _TimeInForceStub:
        DAY = "DAY"
        IOC = "IOC"

    class _LimitOrderRequest(_MarketOrderRequest): pass
    class _ClosePositionRequest(_MarketOrderRequest): pass
    class _StopOrderRequest(_MarketOrderRequest): pass
    class _StopLimitOrderRequest(_MarketOrderRequest): pass

    _alpaca_trading_requests.MarketOrderRequest = _MarketOrderRequest
    _alpaca_trading_requests.LimitOrderRequest = _LimitOrderRequest
    _alpaca_trading_requests.ClosePositionRequest = _ClosePositionRequest
    _alpaca_trading_requests.StopOrderRequest = _StopOrderRequest
    _alpaca_trading_requests.StopLimitOrderRequest = _StopLimitOrderRequest
    _alpaca_trading_enums.OrderSide = _OrderSideStub
    _alpaca_trading_enums.TimeInForce = _TimeInForceStub
    sys.modules["alpaca"] = _alpaca
    sys.modules["alpaca.trading"] = _alpaca_trading
    sys.modules["alpaca.trading.requests"] = _alpaca_trading_requests
    sys.modules["alpaca.trading.enums"] = _alpaca_trading_enums

# executors.base imports trade_genius lazily via `_tg()`. In sandbox
# the running script is __main__; _tg() returns sys.modules["__main__"]
# which lacks _utc_now_iso. Attach a stub so calls from _on_signal
# (e.g. event.get("timestamp_utc", _tg()._utc_now_iso())) don't raise.
sys.modules["__main__"]._utc_now_iso = lambda: "2026-05-12T00:00:00Z"

import pytest

from executors.base import TradeGeniusBase


# ----- test fixtures --------------------------------------------------


class _FakeExecutor(TradeGeniusBase):
    """Minimum-viable TradeGeniusBase subclass: provides the abstract
    bits without hitting telegram / persistence so unit tests can run
    in sandbox."""
    NAME = "TEST"
    mode = "paper"
    DEFAULT_OWNERS = set()

    def __init__(self):
        # Skip the heavy TradeGeniusBase.__init__ -- we only need the
        # attributes accessed by _partial_close_position_idempotent +
        # _build_client_order_id + _stamp_action + _persist_position +
        # _send_own_telegram.
        self.client = None
        self.positions = {}
        self.telegram_token = ""
        self._owner_chats = {}
        self._last_action_ts = {}
        # Used by _stamp_action / persistence -- stub out to a no-op
        # dict so the call works without a real state.db.
        self._persisted_positions = {}

    # Override persistence + telegram to avoid hitting state.db /
    # network in unit tests. The base class methods do nothing if
    # telegram_token is empty (already verified), so we just override
    # _persist_position which would try to touch state.db.
    def _persist_position(self, ticker):
        self._persisted_positions[ticker] = dict(self.positions.get(ticker) or {})


def _make_order(order_id="ord-42"):
    o = SimpleNamespace()
    o.id = order_id
    return o


# ----- 1. partial-close happy path ------------------------------------


class TestPartialCloseHappyPath:

    def test_long_partial_submits_market_sell_and_decrements_qty(self):
        ex = _FakeExecutor()
        ex.positions["AAPL"] = {"side": "LONG", "qty": 100, "stop": 99.0,
                                 "entry_price": 100.0}
        client = MagicMock()
        client.submit_order = MagicMock(return_value=_make_order("ord-1"))

        ex._partial_close_position_idempotent(
            client, "AAPL", shares_to_close=50,
            label="TEST", reason="PARTIAL_1R",
        )

        # Exactly one order submitted
        assert client.submit_order.call_count == 1
        # Inspect the request -- it's the first positional arg
        req = client.submit_order.call_args.args[0]
        # MarketOrderRequest fields (no need to deep-check the SDK
        # types -- just verify the public attributes the v8.1.1 spec
        # cares about)
        assert req.symbol == "AAPL"
        assert req.qty == 50
        # SELL on long-side partial-close
        assert str(req.side).endswith("SELL")
        # Local qty decremented to runner remainder
        assert ex.positions["AAPL"]["qty"] == 50
        # Persisted
        assert ex._persisted_positions["AAPL"]["qty"] == 50

    def test_short_partial_submits_market_buy(self):
        ex = _FakeExecutor()
        ex.positions["AAPL"] = {"side": "SHORT", "qty": 100, "stop": 101.0,
                                 "entry_price": 100.0}
        client = MagicMock()
        client.submit_order = MagicMock(return_value=_make_order("ord-2"))

        ex._partial_close_position_idempotent(
            client, "AAPL", shares_to_close=50,
            label="TEST", reason="PARTIAL_1R",
        )
        req = client.submit_order.call_args.args[0]
        assert req.symbol == "AAPL"
        assert req.qty == 50
        # BUY on short-side partial-close (buy-to-cover half)
        assert str(req.side).endswith("BUY")
        assert ex.positions["AAPL"]["qty"] == 50


# ----- 2. refuse bad partial ------------------------------------------


class TestPartialCloseRefuses:

    def test_refuses_partial_geq_current_qty(self):
        ex = _FakeExecutor()
        ex.positions["AAPL"] = {"side": "LONG", "qty": 100,
                                 "entry_price": 100.0}
        client = MagicMock()
        client.submit_order = MagicMock(return_value=_make_order())

        # partial = full -> refused
        ex._partial_close_position_idempotent(
            client, "AAPL", shares_to_close=100,
            label="TEST", reason="PARTIAL_1R",
        )
        # No order submitted; no mutation
        client.submit_order.assert_not_called()
        assert ex.positions["AAPL"]["qty"] == 100

    def test_refuses_partial_over_current_qty(self):
        ex = _FakeExecutor()
        ex.positions["AAPL"] = {"side": "LONG", "qty": 100,
                                 "entry_price": 100.0}
        client = MagicMock()
        ex._partial_close_position_idempotent(
            client, "AAPL", shares_to_close=150,
            label="TEST", reason="PARTIAL_1R",
        )
        client.submit_order.assert_not_called()
        assert ex.positions["AAPL"]["qty"] == 100

    def test_refuses_partial_zero_or_negative(self):
        ex = _FakeExecutor()
        ex.positions["AAPL"] = {"side": "LONG", "qty": 100,
                                 "entry_price": 100.0}
        client = MagicMock()
        ex._partial_close_position_idempotent(
            client, "AAPL", shares_to_close=0,
            label="TEST", reason="PARTIAL_1R",
        )
        ex._partial_close_position_idempotent(
            client, "AAPL", shares_to_close=-5,
            label="TEST", reason="PARTIAL_1R",
        )
        client.submit_order.assert_not_called()
        assert ex.positions["AAPL"]["qty"] == 100

    def test_no_position_tracked_is_noop(self):
        ex = _FakeExecutor()
        # AAPL not in positions
        client = MagicMock()
        ex._partial_close_position_idempotent(
            client, "AAPL", shares_to_close=50,
            label="TEST", reason="PARTIAL_1R",
        )
        client.submit_order.assert_not_called()


# ----- 3. error handling ----------------------------------------------


class _Already40410000(Exception):
    pass


class TestPartialCloseErrors:

    def test_40410000_treated_as_success_decrements_local(self):
        # Alpaca returns "position not found" for an already-flat
        # ticker (race condition). v8.1.1 treats this as a soft
        # success and decrements local qty anyway.
        ex = _FakeExecutor()
        ex.positions["AAPL"] = {"side": "LONG", "qty": 100,
                                 "entry_price": 100.0}
        client = MagicMock()
        client.submit_order = MagicMock(side_effect=Exception(
            "alpaca error code 40410000: position not found"
        ))
        ex._partial_close_position_idempotent(
            client, "AAPL", shares_to_close=50,
            label="TEST", reason="PARTIAL_1R",
        )
        # Local qty STILL decremented (so the engine state aligns
        # with Alpaca's already-flat view going forward)
        assert ex.positions["AAPL"]["qty"] == 50

    def test_other_error_leaves_local_qty_unchanged(self):
        # Any non-40410000 error must NOT mutate local qty -- caller
        # retries on next tick. Silent share leak is the worst case.
        ex = _FakeExecutor()
        ex.positions["AAPL"] = {"side": "LONG", "qty": 100,
                                 "entry_price": 100.0}
        client = MagicMock()
        client.submit_order = MagicMock(side_effect=Exception(
            "alpaca 500 server error"
        ))
        ex._partial_close_position_idempotent(
            client, "AAPL", shares_to_close=50,
            label="TEST", reason="PARTIAL_1R",
        )
        # qty unchanged so caller can retry
        assert ex.positions["AAPL"]["qty"] == 100


# ----- 4. _on_signal PARTIAL_EXIT_* dispatch --------------------------


class _DispatchExecutor(_FakeExecutor):
    """Capture _partial_close_position_idempotent calls so the
    _on_signal dispatch path can be verified without re-testing
    the partial code itself."""

    def __init__(self):
        super().__init__()
        self.partial_calls = []
        # Stub _ensure_client to skip the alpaca client builder.
        self._stub_client = MagicMock()

    def _ensure_client(self):
        return self._stub_client

    def _partial_close_position_idempotent(self, client, ticker,
                                            shares_to_close, label,
                                            reason):
        self.partial_calls.append({
            "ticker": ticker, "shares_to_close": shares_to_close,
            "reason": reason,
        })


class TestOnSignalPartialDispatch:

    def test_partial_exit_long_routes_to_partial_close(self):
        ex = _DispatchExecutor()
        ex.positions["AAPL"] = {"side": "LONG", "qty": 100,
                                 "entry_price": 100.0}
        event = {
            "kind": "PARTIAL_EXIT_LONG",
            "ticker": "AAPL",
            "price": 101.0,
            "reason": "PARTIAL_1R",
            "main_shares": 50, "timestamp_utc": "2026-05-12T00:00:00Z",
        }
        ex._on_signal(event)
        assert len(ex.partial_calls) == 1
        call = ex.partial_calls[0]
        assert call["ticker"] == "AAPL"
        assert call["shares_to_close"] == 50
        assert call["reason"] == "PARTIAL_1R"

    def test_partial_exit_short_routes_to_partial_close(self):
        ex = _DispatchExecutor()
        ex.positions["AAPL"] = {"side": "SHORT", "qty": 100,
                                 "entry_price": 100.0}
        event = {
            "kind": "PARTIAL_EXIT_SHORT",
            "ticker": "AAPL",
            "price": 99.0,
            "reason": "PARTIAL_1R",
            "main_shares": 50, "timestamp_utc": "2026-05-12T00:00:00Z",
        }
        ex._on_signal(event)
        assert len(ex.partial_calls) == 1
        assert ex.partial_calls[0]["shares_to_close"] == 50

    def test_partial_exit_no_position_skipped(self):
        ex = _DispatchExecutor()
        # AAPL not in positions
        event = {
            "kind": "PARTIAL_EXIT_LONG",
            "ticker": "AAPL",
            "price": 101.0,
            "reason": "PARTIAL_1R",
            "main_shares": 50, "timestamp_utc": "2026-05-12T00:00:00Z",
        }
        ex._on_signal(event)
        # No partial-close call
        assert len(ex.partial_calls) == 0

    def test_partial_exit_zero_main_shares_skipped(self):
        ex = _DispatchExecutor()
        ex.positions["AAPL"] = {"side": "LONG", "qty": 100,
                                 "entry_price": 100.0}
        event = {
            "kind": "PARTIAL_EXIT_LONG",
            "ticker": "AAPL",
            "price": 101.0,
            "reason": "PARTIAL_1R",
            "main_shares": 0, "timestamp_utc": "2026-05-12T00:00:00Z",
        }
        ex._on_signal(event)
        # Defensive guard: main_shares <= 0 -> skip without dispatching
        assert len(ex.partial_calls) == 0


# ----- 5. v8.1.7 -- executor records activity for cross-portfolio
# dashboard visibility ------------------------------------------------


class TestExecutorRecordsPartialActivity:
    """The Val + Gene executors don't route partials through
    orb.live_runtime.check_exit (that's Main-only via
    broker/positions.py:manage_positions). They receive a
    PARTIAL_EXIT_* signal-bus event and call
    _partial_close_position_idempotent directly. v8.1.7 wires that
    method to ALSO call orb.live_runtime._record_activity so the
    dashboard's v10 Activity Feed shows Val/Gene partials alongside
    Main's."""

    def test_successful_partial_records_activity_event(self):
        from orb import live_runtime
        live_runtime.clear_recent_activity()

        ex = _FakeExecutor()
        ex.NAME = "Val"
        ex.positions["AAPL"] = {"side": "LONG", "qty": 100,
                                 "entry_price": 100.0}
        client = MagicMock()
        client.submit_order = MagicMock(return_value=_make_order("ord-act-1"))

        ex._partial_close_position_idempotent(
            client, "AAPL", shares_to_close=50,
            label="VAL paper", reason="PARTIAL_1R",
        )

        # Activity event landed in the ring buffer
        events = live_runtime.get_recent_activity(limit=10)
        assert len(events) >= 1
        e = events[0]
        assert e["kind"] == "partial"
        assert e["ticker"] == "AAPL"
        assert e["pid"] == "val"
        # Detail includes the closed share count + reason
        assert "50 sh" in e["detail"]
        assert "PARTIAL_1R" in e["detail"]

    def test_failed_partial_does_not_record_activity(self):
        # When the partial-close errors out (non-40410000) and local
        # qty stays unchanged, no activity event should fire -- the
        # operator-facing feed should only show partials that
        # actually mutated state.
        from orb import live_runtime
        live_runtime.clear_recent_activity()

        ex = _FakeExecutor()
        ex.NAME = "Gene"
        ex.positions["AAPL"] = {"side": "LONG", "qty": 100,
                                 "entry_price": 100.0}
        client = MagicMock()
        client.submit_order = MagicMock(side_effect=Exception(
            "alpaca 500 server error"
        ))

        ex._partial_close_position_idempotent(
            client, "AAPL", shares_to_close=50,
            label="GENE paper", reason="PARTIAL_1R",
        )

        # No activity recorded (the function returned before the
        # _record_activity call site).
        events = live_runtime.get_recent_activity(limit=10)
        assert events == [] or all(e["kind"] != "partial" for e in events)
