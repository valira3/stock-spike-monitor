"""v8.3.23 -- _on_signal entry guard in independent mode.

When ORB_PORTFOLIO_FIRE=1 (default since v8.3.23), entries are
dispatched per-portfolio by engine/scan.py:_v10_dispatch_executor_fire
-> executor.fire_long/fire_short. The legacy bus listener
(_on_signal) still receives entry events but must SKIP them to avoid
double-firing on Val/Gene Alpaca accounts.

EXIT signals (EXIT_LONG / EXIT_SHORT / PARTIAL_EXIT_*) still flow
through _on_signal so bus-driven exits keep working (Main's sentinel
fires exits, Val mirrors). A future per-portfolio exit loop will
replace this in v8.3.24+.
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
    for _name in ("Application", "ApplicationHandlerStop", "CommandHandler",
                  "TypeHandler"):
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
        # Return a mock so _on_signal proceeds past the None-check
        return MagicMock()

    def _send_own_telegram(self, msg):
        pass


@pytest.fixture
def isolated_env(monkeypatch):
    """Clear ORB_* env so tests have a clean slate AND patch the
    executors.base._tg() reference so the last_signal write (which
    calls _tg()._utc_now_iso() eagerly via dict.get default) doesn't
    blow up in sandbox."""
    for k in list(os.environ):
        if k.startswith("ORB_"):
            monkeypatch.delenv(k, raising=False)
    import executors.base as exec_base
    fake_tg = MagicMock()
    fake_tg._utc_now_iso = MagicMock(return_value="2026-05-12T15:00:00Z")
    monkeypatch.setattr(exec_base, "_tg", lambda: fake_tg)
    yield monkeypatch


class TestIndependentModeEntryGuard:

    def test_default_is_independent_mode(self, isolated_env):
        """v8.3.23 changed the default. With no ORB_PORTFOLIO_FIRE
        env set, the guard should activate (skip entries)."""
        ex = _FakeExec()
        # Send ENTRY_LONG -- should be guarded out before any client call
        ex._ensure_client = MagicMock(side_effect=AssertionError(
            "Should not reach _ensure_client when guard fires"
        ))
        ex._on_signal({
            "kind": "ENTRY_LONG",
            "ticker": "AAPL",
            "price": 150.0,
            "main_shares": 100,
        })
        # No exception means guard fired correctly.

    def test_explicit_one_skips_entry_long(self, isolated_env):
        isolated_env.setenv("ORB_PORTFOLIO_FIRE", "1")
        ex = _FakeExec()
        ex._ensure_client = MagicMock(side_effect=AssertionError("not reached"))
        ex._on_signal({"kind": "ENTRY_LONG", "ticker": "AAPL", "price": 150})

    def test_explicit_one_skips_entry_short(self, isolated_env):
        isolated_env.setenv("ORB_PORTFOLIO_FIRE", "1")
        ex = _FakeExec()
        ex._ensure_client = MagicMock(side_effect=AssertionError("not reached"))
        ex._on_signal({"kind": "ENTRY_SHORT", "ticker": "AAPL", "price": 150})

    def test_explicit_zero_allows_entry(self, isolated_env, monkeypatch):
        """Mirror-mode override: ORB_PORTFOLIO_FIRE=0 keeps the legacy
        bus listener active. Used when operator wants pre-v8.3.23
        behavior."""
        isolated_env.setenv("ORB_PORTFOLIO_FIRE", "0")
        ex = _FakeExec()
        # Stub alpaca-py imports + client so _on_signal can run further.
        # We just need to see that the guard DIDN'T short-circuit.
        # Easiest: assert the in-function _ensure_client gets called.
        called = {"n": 0}
        orig_ensure = ex._ensure_client
        def _spy():
            called["n"] += 1
            return None  # returning None will short-circuit later but
                         # only AFTER the guard check we're testing
        ex._ensure_client = _spy
        ex._on_signal({
            "kind": "ENTRY_LONG", "ticker": "AAPL", "price": 150,
            "timestamp_utc": "2026-05-12T15:00:00Z",
        })
        assert called["n"] == 1, "guard incorrectly fired in mirror mode"

    def test_exit_long_always_passes_guard(self, isolated_env):
        """EXIT signals must NOT be guarded -- they still flow through
        the bus path even in independent mode (no per-portfolio exit
        loop exists yet)."""
        isolated_env.setenv("ORB_PORTFOLIO_FIRE", "1")
        ex = _FakeExec()
        called = {"n": 0}
        def _spy():
            called["n"] += 1
            return None
        ex._ensure_client = _spy
        ex._on_signal({"kind": "EXIT_LONG", "ticker": "AAPL", "price": 150, "timestamp_utc": "2026-05-12T15:00:00Z"})
        assert called["n"] == 1, "EXIT_LONG was incorrectly guarded"

    def test_exit_short_always_passes_guard(self, isolated_env):
        isolated_env.setenv("ORB_PORTFOLIO_FIRE", "1")
        ex = _FakeExec()
        called = {"n": 0}
        def _spy():
            called["n"] += 1
            return None
        ex._ensure_client = _spy
        ex._on_signal({"kind": "EXIT_SHORT", "ticker": "AAPL", "price": 150, "timestamp_utc": "2026-05-12T15:00:00Z"})
        assert called["n"] == 1

    def test_partial_exit_always_passes_guard(self, isolated_env):
        """PARTIAL_EXIT_LONG / PARTIAL_EXIT_SHORT (v8.1.1) also flow
        through in independent mode."""
        isolated_env.setenv("ORB_PORTFOLIO_FIRE", "1")
        ex = _FakeExec()
        called = {"n": 0}
        def _spy():
            called["n"] += 1
            return None
        ex._ensure_client = _spy
        ex._on_signal({
            "kind": "PARTIAL_EXIT_LONG",
            "ticker": "AAPL",
            "price": 150,
            "timestamp_utc": "2026-05-12T15:00:00Z",
        })
        assert called["n"] == 1

    def test_unknown_kind_always_passes_guard(self, isolated_env):
        """Future event kinds (or no-op kinds) shouldn't be guarded
        -- guard is entry-specific."""
        isolated_env.setenv("ORB_PORTFOLIO_FIRE", "1")
        ex = _FakeExec()
        called = {"n": 0}
        def _spy():
            called["n"] += 1
            return None
        ex._ensure_client = _spy
        ex._on_signal({"kind": "EOD_CLOSE_ALL", "ticker": "", "timestamp_utc": "2026-05-12T15:00:00Z"})
        # EOD_CLOSE_ALL isn't an entry so guard doesn't fire.
        assert called["n"] == 1
