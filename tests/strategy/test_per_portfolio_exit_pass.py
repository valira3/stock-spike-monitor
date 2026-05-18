"""v9.1.127 -- per-portfolio EXIT pass tests.

Closes the v8.3.23 limitation flagged in CLAUDE.md: until this PR, Val/Gene
mirrored Main's bus EXIT_LONG/EXIT_SHORT signals to close their positions.
That left positions Val/Gene admitted (that Main rejected on independent
RiskBook fanout) with no intraday exit signal -- they could only flush at
the 15:57 ET safety-net EOD sweep.

This module covers `engine/scan.py:_v10_per_portfolio_exit_pass`:
  - Skipped in mirror mode (ORB_PORTFOLIO_FIRE=0)
  - Skipped when live mode is off
  - Skips the "main" portfolio (Main exits via broker/positions.py)
  - On full exit: dispatches close via _v10_dispatch_executor_fire(reduce_only=True)
  - On partial: dispatches via _v10_dispatch_executor_partial_close
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


def _fake_orb_position(ticker: str, side: str, shares: int):
    """Minimal stand-in for orb.exits.OrbPosition. The exit pass only
    reads .ticker, .side, .shares so duck-typing is fine."""
    return SimpleNamespace(ticker=ticker, side=side, shares=shares)


def _exit_result(
    *,
    exit=False,
    partial=False,
    reason="",
    price=0.0,
    partial_shares=0,
    partial_price=0.0,
    partial_pnl_dollars=0.0,
):
    return SimpleNamespace(
        exit=exit,
        partial=partial,
        reason=reason,
        price=price,
        partial_shares=partial_shares,
        partial_price=partial_price,
        partial_pnl_dollars=partial_pnl_dollars,
    )


@pytest.fixture
def isolated_env(monkeypatch):
    """Default: independent mode ON, live mode ON. Tests override as needed."""
    for k in list(os.environ):
        if k.startswith("ORB_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ORB_PORTFOLIO_FIRE", "1")
    monkeypatch.setenv("ORB_LIVE_MODE", "1")
    yield monkeypatch


class TestExitPassGuards:
    def test_skipped_when_mirror_mode(self, isolated_env):
        """ORB_PORTFOLIO_FIRE=0 -> bus mirror path owns exits, pass is a no-op."""
        isolated_env.setenv("ORB_PORTFOLIO_FIRE", "0")
        from engine import scan as _scan

        with patch.object(_scan._orb_runtime, "get_engine") as mock_get:
            _scan._v10_per_portfolio_exit_pass(callbacks=MagicMock())
        # get_engine should NEVER be called -- we bail on the env check
        mock_get.assert_not_called()

    def test_skipped_when_live_mode_off(self, isolated_env):
        """ORB_LIVE_MODE=0 -> kill switch active, no exit fires."""
        isolated_env.setenv("ORB_LIVE_MODE", "0")
        from engine import scan as _scan

        with patch.object(_scan._orb_runtime, "get_engine") as mock_get:
            _scan._v10_per_portfolio_exit_pass(callbacks=MagicMock())
        mock_get.assert_not_called()

    def test_skipped_when_no_engine(self, isolated_env):
        """No bootstrapped engine -> no-op (idempotent on pre-bootstrap state)."""
        from engine import scan as _scan

        with patch.object(_scan._orb_runtime, "get_engine", return_value=None):
            _scan._v10_per_portfolio_exit_pass(callbacks=MagicMock())
        # No exception means pass


class TestMainIsSkipped:
    def test_main_pid_skipped(self, isolated_env):
        """Main exits via broker/positions.py:manage_positions, not this pass."""
        from engine import scan as _scan

        engine = MagicMock()
        engine.portfolio_ids = ["main"]

        with patch.object(_scan._orb_runtime, "get_engine", return_value=engine):
            with patch.object(_scan._orb_runtime, "get_adapter") as mock_adapter:
                _scan._v10_per_portfolio_exit_pass(callbacks=MagicMock())
        # get_adapter should NEVER be called for main
        mock_adapter.assert_not_called()


class TestFullExitDispatch:
    def test_full_exit_fires_close_via_dispatch_with_reduce_only(self, isolated_env):
        """On a full exit decision, the per-portfolio pass must call
        _v10_dispatch_executor_fire with the opposite side and reduce_only=True."""
        from engine import scan as _scan

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

        with patch.object(_scan._orb_runtime, "get_engine", return_value=engine):
            with patch.object(_scan._orb_runtime, "get_adapter", return_value=adapter):
                with patch.object(
                    _scan._orb_runtime, "check_exit_by_ticker", return_value=exit_res
                ):
                    with patch.object(_scan, "_v10_dispatch_executor_fire") as mock_fire:
                        _scan._v10_per_portfolio_exit_pass(callbacks=callbacks)

        mock_fire.assert_called_once()
        kwargs = mock_fire.call_args.kwargs
        assert kwargs["pid"] == "val"
        assert kwargs["side"] == "short"  # long -> sell to close
        assert kwargs["ticker"] == "AAPL"
        assert kwargs["shares"] == 100
        assert kwargs["reduce_only"] is True

    def test_full_exit_short_fires_buy_to_cover(self, isolated_env):
        """Short position -> close_side='long' (buy to cover)."""
        from engine import scan as _scan

        pos = _fake_orb_position("AAPL", side="short", shares=50)
        adapter = MagicMock()
        adapter.list_open_positions.return_value = [pos]

        engine = MagicMock()
        engine.portfolio_ids = ["gene"]

        callbacks = MagicMock()
        callbacks.fetch_1min_bars.return_value = {
            "current_price": 95.0,
            "highs": [96.0],
            "lows": [94.5],
            "timestamps": [int(1700000000)],
        }

        exit_res = _exit_result(exit=True, reason="V10_TARGET", price=95.0)

        with patch.object(_scan._orb_runtime, "get_engine", return_value=engine):
            with patch.object(_scan._orb_runtime, "get_adapter", return_value=adapter):
                with patch.object(
                    _scan._orb_runtime, "check_exit_by_ticker", return_value=exit_res
                ):
                    with patch.object(_scan, "_v10_dispatch_executor_fire") as mock_fire:
                        _scan._v10_per_portfolio_exit_pass(callbacks=callbacks)

        mock_fire.assert_called_once()
        kwargs = mock_fire.call_args.kwargs
        assert kwargs["side"] == "long"
        assert kwargs["reduce_only"] is True

    def test_no_exit_no_dispatch(self, isolated_env):
        """When check_exit_by_ticker returns exit=False / partial=False,
        the pass must NOT call dispatch."""
        from engine import scan as _scan

        pos = _fake_orb_position("AAPL", side="long", shares=100)
        adapter = MagicMock()
        adapter.list_open_positions.return_value = [pos]

        engine = MagicMock()
        engine.portfolio_ids = ["val"]

        callbacks = MagicMock()
        callbacks.fetch_1min_bars.return_value = {
            "current_price": 100.5,
            "highs": [100.5],
            "lows": [100.0],
            "timestamps": [int(1700000000)],
        }

        no_exit = _exit_result(exit=False, partial=False)

        with patch.object(_scan._orb_runtime, "get_engine", return_value=engine):
            with patch.object(_scan._orb_runtime, "get_adapter", return_value=adapter):
                with patch.object(_scan._orb_runtime, "check_exit_by_ticker", return_value=no_exit):
                    with patch.object(_scan, "_v10_dispatch_executor_fire") as mock_fire:
                        _scan._v10_per_portfolio_exit_pass(callbacks=callbacks)

        mock_fire.assert_not_called()


class TestPartialExitDispatch:
    def test_partial_fires_partial_close_dispatch(self, isolated_env):
        """On partial=True, the pass must call _v10_dispatch_executor_partial_close
        with partial_shares + partial_price from the ExitResult."""
        from engine import scan as _scan

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

        partial_res = _exit_result(
            partial=True,
            partial_shares=50,
            partial_price=102.0,
            partial_pnl_dollars=100.0,
        )

        with patch.object(_scan._orb_runtime, "get_engine", return_value=engine):
            with patch.object(_scan._orb_runtime, "get_adapter", return_value=adapter):
                with patch.object(
                    _scan._orb_runtime, "check_exit_by_ticker", return_value=partial_res
                ):
                    with patch.object(
                        _scan, "_v10_dispatch_executor_partial_close"
                    ) as mock_partial:
                        with patch.object(_scan, "_v10_dispatch_executor_fire") as mock_fire:
                            _scan._v10_per_portfolio_exit_pass(callbacks=callbacks)

        mock_partial.assert_called_once()
        kwargs = mock_partial.call_args.kwargs
        assert kwargs["pid"] == "val"
        assert kwargs["ticker"] == "AAPL"
        assert kwargs["shares"] == 50
        assert kwargs["price"] == 102.0
        # Full-exit dispatch must NOT fire on a partial decision
        mock_fire.assert_not_called()


class TestMultiplePortfolios:
    def test_iterates_all_non_main_portfolios(self, isolated_env):
        """The pass must iterate Val + Gene independently."""
        from engine import scan as _scan

        pos_val = _fake_orb_position("AAPL", side="long", shares=100)
        pos_gene = _fake_orb_position("MSFT", side="short", shares=50)

        adapter_val = MagicMock()
        adapter_val.list_open_positions.return_value = [pos_val]
        adapter_gene = MagicMock()
        adapter_gene.list_open_positions.return_value = [pos_gene]

        def _get_adapter(pid):
            return {"val": adapter_val, "gene": adapter_gene}.get(pid)

        engine = MagicMock()
        engine.portfolio_ids = ["main", "val", "gene"]

        callbacks = MagicMock()
        callbacks.fetch_1min_bars.return_value = {
            "current_price": 100.0,
            "highs": [101.0],
            "lows": [99.0],
            "timestamps": [int(1700000000)],
        }

        no_exit = _exit_result(exit=False)

        with patch.object(_scan._orb_runtime, "get_engine", return_value=engine):
            with patch.object(_scan._orb_runtime, "get_adapter", side_effect=_get_adapter):
                with patch.object(
                    _scan._orb_runtime, "check_exit_by_ticker", return_value=no_exit
                ) as mock_check:
                    _scan._v10_per_portfolio_exit_pass(callbacks=callbacks)

        # check_exit_by_ticker called once for val/AAPL and once for gene/MSFT
        called_pids = [c.kwargs["portfolio_id"] for c in mock_check.call_args_list]
        called_tickers = [c.kwargs["ticker"] for c in mock_check.call_args_list]
        assert sorted(called_pids) == ["gene", "val"]
        assert sorted(called_tickers) == ["AAPL", "MSFT"]


class TestBarFetchFailure:
    def test_skips_ticker_when_bars_missing(self, isolated_env):
        """A failed fetch must NOT crash the pass -- continue to next ticker."""
        from engine import scan as _scan

        pos = _fake_orb_position("AAPL", side="long", shares=100)
        adapter = MagicMock()
        adapter.list_open_positions.return_value = [pos]

        engine = MagicMock()
        engine.portfolio_ids = ["val"]

        callbacks = MagicMock()
        callbacks.fetch_1min_bars.return_value = None  # missing bars

        with patch.object(_scan._orb_runtime, "get_engine", return_value=engine):
            with patch.object(_scan._orb_runtime, "get_adapter", return_value=adapter):
                with patch.object(_scan._orb_runtime, "check_exit_by_ticker") as mock_check:
                    with patch.object(_scan, "_v10_dispatch_executor_fire") as mock_fire:
                        _scan._v10_per_portfolio_exit_pass(callbacks=callbacks)

        # check_exit not called -- we bailed on missing bars
        mock_check.assert_not_called()
        mock_fire.assert_not_called()
