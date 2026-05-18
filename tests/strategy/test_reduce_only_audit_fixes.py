"""v9.1.128 audit fixes -- three regressions found during deep audit
of the v9.1.126/127/128 stack.

(1) Cooldown gate must not block reduce_only=True closes
    (executors/base.py:_submit_v10_entry).
(2) _v10_per_portfolio_exit_pass must record post-trade cooldown
    after a full exit, since the legacy bus-mirror handler that
    used to do this is now unreachable.
(3) MarketOrderRequest must forward reduce_only to Alpaca so the
    broker enforces close-only semantics (defends against local
    <-> broker position drift).
"""

from __future__ import annotations

import os
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


if "telegram" not in sys.modules:
    _tel = ModuleType("telegram")
    for _name in ("BotCommand", "BotCommandScopeAllPrivateChats", "Update"):
        setattr(_tel, _name, type(_name, (), {}))
    sys.modules["telegram"] = _tel
    _tel_ext = ModuleType("telegram.ext")
    for _name in (
        "Application",
        "ApplicationHandlerStop",
        "CommandHandler",
        "TypeHandler",
    ):
        setattr(_tel_ext, _name, type(_name, (), {}))
    sys.modules["telegram.ext"] = _tel_ext


# Other test modules (e.g. test_executor_partial_close.py) stub the
# alpaca.trading.enums module with a limited surface (OrderSide,
# TimeInForce) before our tests run. Their stub is missing
# PositionIntent, which our v9.1.128 fix consumes. Patch the stub
# defensively so this test file is robust to test-collection order.
_alpaca_enums = sys.modules.get("alpaca.trading.enums")
if _alpaca_enums is not None and not hasattr(_alpaca_enums, "PositionIntent"):

    class _PositionIntentStub:
        class _V:
            def __init__(self, name):
                self._n = name

            def __str__(self):
                return f"PositionIntent.{self._n}"

            def __repr__(self):
                return f"PositionIntent.{self._n}"

        BUY_TO_OPEN = _V("BUY_TO_OPEN")
        BUY_TO_CLOSE = _V("BUY_TO_CLOSE")
        SELL_TO_OPEN = _V("SELL_TO_OPEN")
        SELL_TO_CLOSE = _V("SELL_TO_CLOSE")

    _alpaca_enums.PositionIntent = _PositionIntentStub


def _account(equity: float, long_mv: float = 0.0, short_mv: float = 0.0):
    return SimpleNamespace(
        equity=str(equity),
        cash=str(equity - long_mv - abs(short_mv)),
        long_market_value=str(long_mv),
        short_market_value=str(-abs(short_mv)),
    )


def _build_executor(name: str = "Val"):
    from executors.base import TradeGeniusBase

    ex = TradeGeniusBase.__new__(TradeGeniusBase)
    ex.NAME = name
    ex.mode = "live"
    ex.client = MagicMock()
    ex._open_positions = {}
    ex._client_order_id_used = set()
    return ex


# ---------------------------------------------------------------------------
# Fix 1: cooldown gate must be bypassed when reduce_only=True
# ---------------------------------------------------------------------------


class TestCooldownBypassedByReduceOnly:
    def test_reduce_only_close_bypasses_active_cooldown(self):
        """A position-close (reduce_only=True) MUST submit even when
        (ticker, side) is in active post-trade cooldown. Pre-v9.1.128
        the cooldown gate was checked unconditionally, so an opposite-
        side close on a ticker whose same-side trade just stopped out
        was wrongly blocked."""
        from executors.base import TradeGeniusBase
        from engine.portfolio_book import PORTFOLIOS

        # The registry pre-registers val/gene/main; use the val book.
        pb = PORTFOLIOS.get("val")
        pb._post_trade_cooldown.clear()
        # Set up a "short" cooldown that's currently active (we'll close a
        # long position via fire_short, so the cooldown is checked on
        # (ticker, "short") inside _submit_v10_entry).
        with patch.dict(os.environ, {"ORB_POST_TRADE_COOLDOWN_MIN": "10"}):
            pb.record_post_trade("ORCL", "short")
        assert pb.is_in_post_trade_cooldown("ORCL", "short") is not None

        ex = _build_executor("Val")
        mock_client = MagicMock()
        mock_client.get_account.return_value = _account(30_000, 10_000, 0)
        mock_client.submit_order.return_value = SimpleNamespace(id="ord1")
        ex.client = mock_client

        # Close a long position via fire_short(reduce_only=True).
        # Without the audit fix this would BLOCK because the (ORCL, short)
        # cooldown is active.
        with patch.object(TradeGeniusBase, "_build_client_order_id", return_value="c"):
            ok = ex.fire_short(
                "ORCL", price=200.0, shares=50, error_callback=None, reduce_only=True
            )
        pb._post_trade_cooldown.clear()  # cleanup so other tests aren't polluted
        assert ok is True, "reduce_only=True close was incorrectly blocked by cooldown gate"

    def test_entry_still_blocked_by_cooldown(self):
        """Regression guard: reduce_only=False (entries) MUST still
        respect the cooldown. Don't accidentally turn cooldowns off
        for all paths."""
        from executors.base import TradeGeniusBase
        from engine.portfolio_book import PORTFOLIOS

        pb = PORTFOLIOS.get("val")
        pb._post_trade_cooldown.clear()
        with patch.dict(os.environ, {"ORB_POST_TRADE_COOLDOWN_MIN": "10"}):
            pb.record_post_trade("ORCL", "long")
        assert pb.is_in_post_trade_cooldown("ORCL", "long") is not None

        ex = _build_executor("Val")
        mock_client = MagicMock()
        mock_client.get_account.return_value = _account(30_000, 0, 0)
        mock_client.submit_order.return_value = SimpleNamespace(id="ord_e")
        ex.client = mock_client

        with patch.object(TradeGeniusBase, "_build_client_order_id", return_value="c"):
            # Default reduce_only=False -- entry path -- must be blocked.
            ok = ex.fire_long("ORCL", price=200.0, shares=50, error_callback=None)
        pb._post_trade_cooldown.clear()
        assert ok is False, "entry was NOT blocked by active cooldown (regression)"


# ---------------------------------------------------------------------------
# Fix 2: _v10_per_portfolio_exit_pass must record post-trade cooldown
# ---------------------------------------------------------------------------


def _fake_orb_position(ticker: str, side: str, shares: int):
    return SimpleNamespace(ticker=ticker, side=side, shares=shares)


def _exit_result(*, exit=False, partial=False, reason="", price=0.0):
    return SimpleNamespace(
        exit=exit,
        partial=partial,
        reason=reason,
        price=price,
        partial_shares=0,
        partial_price=0.0,
        partial_pnl_dollars=0.0,
    )


@pytest.fixture
def isolated_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("ORB_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ORB_LIVE_MODE", "1")
    yield monkeypatch


class TestExitPassRecordsCooldown:
    def test_full_exit_records_post_trade_cooldown(self, isolated_env):
        """On full exit, the new path MUST call record_post_trade on
        the portfolio's PortfolioBook so the Keystone cooldown lever
        still fires for Val/Gene. Pre-v9.1.128 (audit fix) this was
        missing -- ORB_POST_TRADE_COOLDOWN_MIN=10 was silently disabled
        for Val/Gene."""
        from engine import scan as _scan
        from engine.portfolio_book import PORTFOLIOS

        pos = _fake_orb_position("AAPL", side="long", shares=100)
        adapter = MagicMock()
        adapter.list_open_positions.return_value = [pos]

        engine = MagicMock()
        engine.portfolio_ids = ["val"]

        callbacks = MagicMock()
        callbacks.fetch_1min_bars.return_value = {
            "current_price": 105.5,
            "highs": [105.5],
            "lows": [105.0],
            "timestamps": [int(1700000000)],
        }

        exit_res = _exit_result(exit=True, reason="V10_STOP", price=102.0)

        # Use the registry's pre-registered val book; clear any cooldown
        # state so the assertion is meaningful.
        pb = PORTFOLIOS.get("val")
        pb._post_trade_cooldown.clear()
        assert pb.is_in_post_trade_cooldown("AAPL", "long") is None

        with patch.dict(os.environ, {"ORB_POST_TRADE_COOLDOWN_MIN": "10"}):
            with patch.object(_scan._orb_runtime, "get_engine", return_value=engine):
                with patch.object(_scan._orb_runtime, "get_adapter", return_value=adapter):
                    with patch.object(
                        _scan._orb_runtime,
                        "check_exit_by_ticker",
                        return_value=exit_res,
                    ):
                        with patch.object(_scan, "_v10_dispatch_executor_fire"):
                            _scan._v10_per_portfolio_exit_pass(callbacks=callbacks)

        # Cooldown MUST now be set on (AAPL, long) for the val book.
        cd = pb.is_in_post_trade_cooldown("AAPL", "long")
        pb._post_trade_cooldown.clear()  # cleanup
        assert cd is not None, "record_post_trade was not called by exit pass"

    def test_partial_exit_does_not_record_cooldown(self, isolated_env):
        """Partial exits leave the position open with a runner half.
        They are NOT terminal exits and must not start a cooldown
        (otherwise the runner's full-exit + cooldown cycle would be
        double-counted)."""
        from engine import scan as _scan
        from engine.portfolio_book import PORTFOLIOS

        pos = _fake_orb_position("AAPL", side="long", shares=100)
        adapter = MagicMock()
        adapter.list_open_positions.return_value = [pos]
        engine = MagicMock()
        engine.portfolio_ids = ["val"]
        callbacks = MagicMock()
        callbacks.fetch_1min_bars.return_value = {
            "current_price": 102.0,
            "highs": [102.5],
            "lows": [101.5],
            "timestamps": [int(1700000000)],
        }

        partial_res = SimpleNamespace(
            exit=False,
            partial=True,
            reason="",
            price=0.0,
            partial_shares=50,
            partial_price=102.0,
            partial_pnl_dollars=100.0,
        )
        pb = PORTFOLIOS.get("val")
        pb._post_trade_cooldown.clear()
        assert pb.is_in_post_trade_cooldown("AAPL", "long") is None

        with patch.dict(os.environ, {"ORB_POST_TRADE_COOLDOWN_MIN": "10"}):
            with patch.object(_scan._orb_runtime, "get_engine", return_value=engine):
                with patch.object(_scan._orb_runtime, "get_adapter", return_value=adapter):
                    with patch.object(
                        _scan._orb_runtime,
                        "check_exit_by_ticker",
                        return_value=partial_res,
                    ):
                        with patch.object(_scan, "_v10_dispatch_executor_partial_close"):
                            _scan._v10_per_portfolio_exit_pass(callbacks=callbacks)

        cd = pb.is_in_post_trade_cooldown("AAPL", "long")
        pb._post_trade_cooldown.clear()
        assert cd is None, "partial exit incorrectly set a cooldown"


# ---------------------------------------------------------------------------
# Fix 3: MarketOrderRequest must forward reduce_only to Alpaca
# ---------------------------------------------------------------------------


class TestMarketOrderRequestForwardsCloseIntent:
    """v9.1.128 (audit fix 3): the close-only intent forwards to Alpaca
    as `position_intent={SELL,BUY}_TO_CLOSE`. alpaca-py's MarketOrderRequest
    has no `reduce_only` field (pydantic v2 silently drops it), so the
    correct broker-side primitive is PositionIntent. Verifies the close
    path sets it and the entry path doesn't."""

    def test_close_short_carries_sell_to_close_intent(self):
        """fire_short(reduce_only=True) closes a long position with
        position_intent=SELL_TO_CLOSE."""
        from executors.base import TradeGeniusBase
        from alpaca.trading.enums import PositionIntent

        ex = _build_executor()
        mock_client = MagicMock()
        mock_client.get_account.return_value = _account(30_000, 36_000, 0)
        mock_client.submit_order.return_value = SimpleNamespace(id="ord_c")
        ex.client = mock_client

        with patch.object(TradeGeniusBase, "_build_client_order_id", return_value="c"):
            ok = ex.fire_short(
                "ORCL", price=200.0, shares=190, error_callback=None, reduce_only=True
            )
        assert ok is True
        req = mock_client.submit_order.call_args[0][0]
        assert getattr(req, "position_intent", None) == PositionIntent.SELL_TO_CLOSE, (
            "close-long (fire_short reduce_only) is missing SELL_TO_CLOSE intent"
        )

    def test_close_long_carries_buy_to_close_intent(self):
        """fire_long(reduce_only=True) closes a short position with
        position_intent=BUY_TO_CLOSE."""
        from executors.base import TradeGeniusBase
        from alpaca.trading.enums import PositionIntent

        ex = _build_executor()
        mock_client = MagicMock()
        mock_client.get_account.return_value = _account(30_000, 0, 36_000)
        mock_client.submit_order.return_value = SimpleNamespace(id="ord_c2")
        ex.client = mock_client

        with patch.object(TradeGeniusBase, "_build_client_order_id", return_value="c"):
            ok = ex.fire_long(
                "ORCL", price=200.0, shares=190, error_callback=None, reduce_only=True
            )
        assert ok is True
        req = mock_client.submit_order.call_args[0][0]
        assert getattr(req, "position_intent", None) == PositionIntent.BUY_TO_CLOSE, (
            "close-short (fire_long reduce_only) is missing BUY_TO_CLOSE intent"
        )

    def test_entry_request_does_not_carry_position_intent(self):
        """Entry orders (reduce_only=False) must NOT carry a *_TO_CLOSE
        intent -- an entry tagged with SELL_TO_CLOSE / BUY_TO_CLOSE
        would be rejected by Alpaca (no position to close yet)."""
        from executors.base import TradeGeniusBase

        ex = _build_executor()
        mock_client = MagicMock()
        mock_client.get_account.return_value = _account(30_000, 0, 0)
        mock_client.submit_order.return_value = SimpleNamespace(id="ord_e")
        ex.client = mock_client

        with patch.object(TradeGeniusBase, "_build_client_order_id", return_value="c"):
            ok = ex.fire_long("ORCL", price=200.0, shares=50, error_callback=None)
        assert ok is True
        req = mock_client.submit_order.call_args[0][0]
        assert getattr(req, "position_intent", None) is None, (
            "MarketOrderRequest wrongly carries a position_intent on entry path"
        )
