"""v9.1.126 -- EOD_CLOSE_ALL always sweeps positions, regardless of mode.

Pre-v9.1.126: in independent mode (ORB_PORTFOLIO_FIRE=1, the default
since v8.3.23) the _on_signal EOD_CLOSE_ALL branch SKIPPED
client.close_all_positions on the "engines own their exits" rationale.
That was unsafe -- a position Val/Gene admitted that Main rejected got
no exit signal (orb.live_runtime.check_exit has no production caller),
and the executor wiped local tracking without closing the Alpaca leg.

v9.1.126: always sweep. Safe because v9.1.125 moved the EOD reversal
exit window from 15:59 to 15:56, leaving a 1-min buffer before the
15:57 EOD_CLOSE_ALL arrives.
"""

from __future__ import annotations

import os
import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest


# Stub telegram modules so executors.base imports cleanly.
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

from executors.base import TradeGeniusBase


class _FakeExec(TradeGeniusBase):
    NAME = "Val"
    mode = "paper"
    DEFAULT_OWNERS = set()

    def __init__(self, positions: dict | None = None):
        self.client = None
        self.positions = positions or {}
        self.telegram_token = ""
        self._owner_chats = {}
        self._last_action_ts = {}
        self._persisted_positions = {}
        self.last_signal = None
        self._last_open_pnl_ts = 0.0
        self._aon_mode = "software"
        self._mock_client = MagicMock()

    def _ensure_client(self):
        return self._mock_client

    def _send_own_telegram(self, msg):
        pass

    def _remove_position(self, ticker):
        self.positions.pop(ticker, None)

    def _reconcile_broker_positions(self):
        pass


@pytest.fixture
def isolated_env(monkeypatch):
    """Clear ORB_* env and patch _tg() so _utc_now_iso doesn't blow up."""
    for k in list(os.environ):
        if k.startswith("ORB_"):
            monkeypatch.delenv(k, raising=False)
    import executors.base as exec_base

    fake_tg = MagicMock()
    fake_tg._utc_now_iso = MagicMock(return_value="2026-05-18T19:57:00Z")
    monkeypatch.setattr(exec_base, "_tg", lambda: fake_tg)
    yield monkeypatch


class TestEodSweep:
    def test_independent_mode_calls_close_all_positions(self, isolated_env):
        """ORB_PORTFOLIO_FIRE=1 (default) MUST call close_all_positions.

        Pre-v9.1.126 this was the broken path: skip set, position lingered.
        """
        isolated_env.setenv("ORB_PORTFOLIO_FIRE", "1")
        ex = _FakeExec(positions={"ORCL": {"shares": 190}, "NFLX": {"shares": 401}})

        ex._on_signal(
            {
                "kind": "EOD_CLOSE_ALL",
                "ticker": "",
                "price": 0.0,
                "timestamp_utc": "2026-05-18T19:57:00Z",
            }
        )

        ex._mock_client.close_all_positions.assert_called_once_with(cancel_orders=True)
        # Local tracking must also be wiped.
        assert ex.positions == {}

    def test_mirror_mode_also_calls_close_all_positions(self, isolated_env):
        """ORB_PORTFOLIO_FIRE=0 (legacy mirror mode) still sweeps. Behavior
        is now identical in both modes -- the v9.1.106 env-gated skip is
        gone."""
        isolated_env.setenv("ORB_PORTFOLIO_FIRE", "0")
        ex = _FakeExec(positions={"AAPL": {"shares": 100}})

        ex._on_signal(
            {
                "kind": "EOD_CLOSE_ALL",
                "ticker": "",
                "price": 0.0,
                "timestamp_utc": "2026-05-18T19:57:00Z",
            }
        )

        ex._mock_client.close_all_positions.assert_called_once_with(cancel_orders=True)
        assert ex.positions == {}

    def test_default_env_calls_close_all_positions(self, isolated_env):
        """No ORB_PORTFOLIO_FIRE set at all (independent mode by default
        since v8.3.23). The sweep MUST still fire -- this is the prod
        configuration."""
        # isolated_env already cleared ORB_PORTFOLIO_FIRE.
        ex = _FakeExec(positions={"AVGO": {"shares": 175}})

        ex._on_signal(
            {
                "kind": "EOD_CLOSE_ALL",
                "ticker": "",
                "price": 0.0,
                "timestamp_utc": "2026-05-18T19:57:00Z",
            }
        )

        ex._mock_client.close_all_positions.assert_called_once_with(cancel_orders=True)
        assert ex.positions == {}
