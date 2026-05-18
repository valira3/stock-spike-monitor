"""v8.3.23 -- _on_signal always-independent skip guard.

Val/Gene fire their own entries + exits + partials per-portfolio via
engine/scan.py:_v10_dispatch_executor_fire + _v10_per_portfolio_exit_pass
+ _v10_dispatch_executor_partial_close. The legacy bus listener
(_on_signal) still receives those events but MUST skip them to avoid
double-firing on the Val/Gene Alpaca accounts.

EOD_CLOSE_ALL stays through the bus listener as the v9.1.126
safety-net sweep at 15:57 ET.

v9.1.128: the ORB_PORTFOLIO_FIRE env flag (mirror-mode escape hatch)
was removed; this skip is now unconditional.
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
    for _name in ("Application", "ApplicationHandlerStop", "CommandHandler", "TypeHandler"):
        setattr(_tel_ext, _name, type(_name, (), {}))
    sys.modules["telegram.ext"] = _tel_ext

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
        self.last_signal = None
        self._last_open_pnl_ts = 0.0
        self._aon_mode = "software"

    def _ensure_client(self):
        return MagicMock()

    def _send_own_telegram(self, msg):
        pass


@pytest.fixture
def isolated_env(monkeypatch):
    """Clear ORB_* env and patch _tg() so _utc_now_iso doesn't blow up."""
    for k in list(os.environ):
        if k.startswith("ORB_"):
            monkeypatch.delenv(k, raising=False)
    import executors.base as exec_base

    fake_tg = MagicMock()
    fake_tg._utc_now_iso = MagicMock(return_value="2026-05-12T15:00:00Z")
    monkeypatch.setattr(exec_base, "_tg", lambda: fake_tg)
    yield monkeypatch


class TestAlwaysIndependentSkipGuard:
    """Each kind in the v9.1.128 unconditional skip set must short-circuit
    before _ensure_client. EOD_CLOSE_ALL must NOT short-circuit."""

    def test_entry_long_skipped(self, isolated_env):
        ex = _FakeExec()
        ex._ensure_client = MagicMock(
            side_effect=AssertionError("guard should fire before _ensure_client")
        )
        ex._on_signal({"kind": "ENTRY_LONG", "ticker": "AAPL", "price": 150.0, "main_shares": 100})

    def test_entry_short_skipped(self, isolated_env):
        ex = _FakeExec()
        ex._ensure_client = MagicMock(side_effect=AssertionError("not reached"))
        ex._on_signal({"kind": "ENTRY_SHORT", "ticker": "AAPL", "price": 150})

    def test_exit_long_skipped(self, isolated_env):
        ex = _FakeExec()
        ex._ensure_client = MagicMock(side_effect=AssertionError("not reached"))
        ex._on_signal(
            {
                "kind": "EXIT_LONG",
                "ticker": "AAPL",
                "price": 150,
                "timestamp_utc": "2026-05-12T15:00:00Z",
            }
        )

    def test_exit_short_skipped(self, isolated_env):
        ex = _FakeExec()
        ex._ensure_client = MagicMock(side_effect=AssertionError("not reached"))
        ex._on_signal(
            {
                "kind": "EXIT_SHORT",
                "ticker": "AAPL",
                "price": 150,
                "timestamp_utc": "2026-05-12T15:00:00Z",
            }
        )

    def test_partial_exit_long_skipped(self, isolated_env):
        ex = _FakeExec()
        ex._ensure_client = MagicMock(side_effect=AssertionError("not reached"))
        ex._on_signal(
            {
                "kind": "PARTIAL_EXIT_LONG",
                "ticker": "AAPL",
                "price": 150,
                "timestamp_utc": "2026-05-12T15:00:00Z",
            }
        )

    def test_partial_exit_short_skipped(self, isolated_env):
        ex = _FakeExec()
        ex._ensure_client = MagicMock(side_effect=AssertionError("not reached"))
        ex._on_signal(
            {
                "kind": "PARTIAL_EXIT_SHORT",
                "ticker": "AAPL",
                "price": 150,
                "timestamp_utc": "2026-05-12T15:00:00Z",
            }
        )

    def test_eod_close_all_passes_guard(self, isolated_env):
        """EOD_CLOSE_ALL is NOT in the skip list -- it still arrives at
        15:57 ET as the v9.1.126 safety-net sweep."""
        ex = _FakeExec()
        called = {"n": 0}

        def _spy():
            called["n"] += 1
            return None

        ex._ensure_client = _spy
        ex._on_signal(
            {
                "kind": "EOD_CLOSE_ALL",
                "ticker": "",
                "timestamp_utc": "2026-05-12T15:00:00Z",
            }
        )
        assert called["n"] == 1

    def test_skip_is_unconditional_even_if_flag_set_to_zero(self, isolated_env):
        """v9.1.128: the ORB_PORTFOLIO_FIRE env flag was removed.
        Setting it to '0' has no effect -- ENTRY/EXIT/PARTIAL still skip."""
        isolated_env.setenv("ORB_PORTFOLIO_FIRE", "0")
        ex = _FakeExec()
        ex._ensure_client = MagicMock(side_effect=AssertionError("not reached"))
        ex._on_signal({"kind": "ENTRY_LONG", "ticker": "AAPL", "price": 150})
