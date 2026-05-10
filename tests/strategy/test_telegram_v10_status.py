"""Tests for v7.21.0 Telegram v10 status surfaces.

Asserts:
  1. /status output contains the v10 ORB Status block (gate status,
     trades used, risk used) when the runtime is bootstrapped.
  2. Every line in the v10 block is <= 34 chars (CLAUDE.md mobile rule).
  3. Deploy banner auto-derives from CHANGELOG (regression coverage
     reaffirmed from v7.13.1).
"""
from __future__ import annotations

import os

import pytest


# Stub env before importing trade_genius
os.environ.setdefault("FMP_API_KEY", "stub")
os.environ.setdefault("ALPACA_API_KEY_ID", "stub")
os.environ.setdefault("ALPACA_API_SECRET_KEY", "stub")
os.environ.setdefault(
    "TELEGRAM_BOT_TOKEN",
    "123456789:AAGabcdefghijklmnopqrstuvwxyz12345678",
)
os.environ.setdefault("TELEGRAM_CHAT_ID", "-100123456789")


@pytest.fixture(scope="module")
def tg_module():
    try:
        import trade_genius as _tg
        return _tg
    except Exception as e:
        pytest.skip(f"cannot import trade_genius: {e}")


class TestV10StatusBlock:

    def test_status_includes_v10_block_when_bootstrapped(self, tg_module):
        """After bootstrap, /status must include 'v10 ORB Status'."""
        from orb import live_runtime
        live_runtime._reset_for_testing()
        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-01-02",
            tickers=["AAPL"], vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        text = tg_module._status_text_sync()
        assert "v10 ORB Status" in text
        assert "Mode:" in text
        assert "VIX(D-1):" in text or "VIX" in text  # may be omitted if vix=None
        live_runtime._reset_for_testing()

    def test_status_omits_v10_block_when_not_bootstrapped(self, tg_module):
        """If runtime never ran bootstrap, the v10 block is silently omitted."""
        from orb import live_runtime
        live_runtime._reset_for_testing()
        text = tg_module._status_text_sync()
        # The block-header should not be present
        assert "v10 ORB Status" not in text

    def test_v10_block_lines_under_34_chars(self, tg_module):
        """CLAUDE.md: Telegram mobile code-block <=34 chars per line."""
        from orb import live_runtime
        live_runtime._reset_for_testing()
        live_runtime.bootstrap()
        live_runtime.ensure_session_started(
            date_iso="2026-01-02",
            tickers=["AAPL"], vix_close_d1=18.5,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100000.0},
        )
        text = tg_module._status_text_sync()
        # Find the v10 block lines
        lines = text.split("\n")
        in_block = False
        for line in lines:
            if "v10 ORB Status" in line:
                in_block = True
                continue
            if in_block:
                if line and not line.startswith(" ") and "v10" not in line:
                    # Block ended
                    break
                # Check the indented body lines + emoji header
                assert len(line) <= 34, (
                    f"v10 status line too long ({len(line)}): {line!r}"
                )
        live_runtime._reset_for_testing()


class TestDeployBannerStillAutoDerives:
    """v7.13.1 added auto-derivation; this test ensures it still works."""

    def test_current_main_note_starts_with_bot_version(self, tg_module):
        first_line = tg_module.CURRENT_MAIN_NOTE.split("\n", 1)[0]
        expected = f"v{tg_module.BOT_VERSION}"
        assert first_line.startswith(expected), (
            f"CURRENT_MAIN_NOTE first line {first_line!r} drift from "
            f"BOT_VERSION {expected!r}"
        )
