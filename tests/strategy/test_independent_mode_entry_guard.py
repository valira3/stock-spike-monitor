"""v8.3.23 -- _on_signal independent-mode skip guard.

When ORB_PORTFOLIO_FIRE=1 (default since v8.3.23), entries are
dispatched per-portfolio by engine/scan.py:_v10_dispatch_executor_fire
-> executor.fire_long/fire_short. The legacy bus listener
(_on_signal) still receives entry events but must SKIP them to avoid
double-firing on Val/Gene Alpaca accounts.

v9.1.127: extended to EXIT_LONG / EXIT_SHORT / PARTIAL_EXIT_LONG /
PARTIAL_EXIT_SHORT. The v8.3.24+ per-portfolio exit loop now exists
as engine/scan.py:_v10_per_portfolio_exit_pass. Val/Gene are
completely independent of Main's bus -- entries and exits both fire
per-portfolio. EOD_CLOSE_ALL remains in the legacy bus listener as
the v9.1.126 safety-net sweep at 15:57 ET.

In mirror mode (ORB_PORTFOLIO_FIRE=0) the guards stand down and
both entries + exits flow through _on_signal as before.
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
        ex._ensure_client = MagicMock(
            side_effect=AssertionError("Should not reach _ensure_client when guard fires")
        )
        ex._on_signal(
            {
                "kind": "ENTRY_LONG",
                "ticker": "AAPL",
                "price": 150.0,
                "main_shares": 100,
            }
        )
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
        ex._on_signal(
            {
                "kind": "ENTRY_LONG",
                "ticker": "AAPL",
                "price": 150,
                "timestamp_utc": "2026-05-12T15:00:00Z",
            }
        )
        assert called["n"] == 1, "guard incorrectly fired in mirror mode"

    def test_exit_long_skipped_in_independent_mode(self, isolated_env):
        """v9.1.127 -- EXIT_LONG is now skipped in independent mode.
        The per-portfolio exit pass in engine/scan.py owns exit firing
        for Val/Gene. Pre-v9.1.127 this was the bus mirror path."""
        isolated_env.setenv("ORB_PORTFOLIO_FIRE", "1")
        ex = _FakeExec()
        ex._ensure_client = MagicMock(
            side_effect=AssertionError("Should not reach _ensure_client when EXIT guard fires")
        )
        ex._on_signal(
            {
                "kind": "EXIT_LONG",
                "ticker": "AAPL",
                "price": 150,
                "timestamp_utc": "2026-05-12T15:00:00Z",
            }
        )

    def test_exit_short_skipped_in_independent_mode(self, isolated_env):
        """v9.1.127 -- EXIT_SHORT is now skipped in independent mode."""
        isolated_env.setenv("ORB_PORTFOLIO_FIRE", "1")
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

    def test_partial_exit_skipped_in_independent_mode(self, isolated_env):
        """v9.1.127 -- PARTIAL_EXIT_LONG / PARTIAL_EXIT_SHORT are now
        skipped in independent mode. _v10_dispatch_executor_partial_close
        in engine/scan.py fires partials for Val/Gene directly."""
        isolated_env.setenv("ORB_PORTFOLIO_FIRE", "1")
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

    def test_exit_long_passes_in_mirror_mode(self, isolated_env):
        """ORB_PORTFOLIO_FIRE=0 (mirror mode) keeps the legacy bus
        listener active for EXIT signals -- Val/Gene mirror Main's
        exits, just like pre-v9.1.127."""
        isolated_env.setenv("ORB_PORTFOLIO_FIRE", "0")
        ex = _FakeExec()
        called = {"n": 0}

        def _spy():
            called["n"] += 1
            return None

        ex._ensure_client = _spy
        ex._on_signal(
            {
                "kind": "EXIT_LONG",
                "ticker": "AAPL",
                "price": 150,
                "timestamp_utc": "2026-05-12T15:00:00Z",
            }
        )
        assert called["n"] == 1, "EXIT_LONG was incorrectly guarded in mirror mode"

    def test_eod_close_all_passes_in_independent_mode(self, isolated_env):
        """EOD_CLOSE_ALL is NOT in the v9.1.127 skip list -- it still
        arrives at 15:57 ET as the v9.1.126 safety-net sweep."""
        isolated_env.setenv("ORB_PORTFOLIO_FIRE", "1")
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
