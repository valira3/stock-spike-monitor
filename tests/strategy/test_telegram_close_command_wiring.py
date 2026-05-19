"""v9.1.135 -- /close <ticker> Telegram command wiring tests.

Source-inspection only (no telegram-python-bot dependency). Verifies
the three integration touchpoints are wired correctly:

  1. `cmd_close` handler exists in `telegram_commands.py` with the
     expected signature and forensic-log signature.
  2. `CommandHandler("close", telegram_commands.cmd_close)` is
     registered in `telegram_ui/runtime.py`.
  3. `BotCommand("close", ...)` appears in `MAIN_BOT_COMMANDS` in
     `trade_genius.py` so the Telegram client surfaces it in the /
     menu popup.

Behavioral coverage (mocked Update/Context, dispatch routing) lives
under `tests/test_telegram_close_behavior.py` and requires the
`telegram` package; that suite is run only in CI sandboxes that
already have the dep.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _read(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def test_cmd_close_defined_in_telegram_commands():
    src = _read("telegram_commands.py")
    assert "async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):" in src
    # Forensic log signature -- log scrapers depend on the exact prefix.
    assert "[V79-MANUAL-CLOSE]" in src
    # Usage line -- the message users see when they invoke /close with no arg.
    assert "Usage: /close <TICKER>" in src


def test_cmd_close_registered_in_runtime():
    src = _read("telegram_ui/runtime.py")
    assert 'CommandHandler("close", telegram_commands.cmd_close)' in src


def test_close_in_main_bot_commands():
    src = _read("trade_genius.py")
    # Single source of truth for the / menu popup.
    assert 'BotCommand("close",' in src


def test_cmd_close_uses_owner_guard_via_runtime():
    """The /close handler does NOT add a per-handler owner check --
    it relies on the global `_auth_guard` (group=-1) in
    telegram_ui/runtime.py. Verify that guard exists and references
    TRADEGENIUS_OWNER_IDS so a future refactor doesn't silently drop
    owner-gating on this state-mutating command."""
    src = _read("telegram_ui/runtime.py")
    assert "_auth_guard" in src
    assert "TRADEGENIUS_OWNER_IDS" in src
    assert "group=-1" in src


def test_cmd_close_routes_main_to_close_breakout():
    """Main close path uses broker.orders.close_breakout (legacy)."""
    src = _read("telegram_commands.py")
    assert "from broker.orders import close_breakout" in src
    assert 'reason="manual_close"' in src


def test_cmd_close_routes_valgene_to_executor_with_reduce_only():
    """Val/Gene close path uses _v10_dispatch_executor_fire with
    reduce_only=True so the v9.1.125 cumulative-notional cap is
    bypassed (cap is for new exposure; close shrinks exposure)."""
    src = _read("telegram_commands.py")
    assert "from engine.scan import _v10_dispatch_executor_fire" in src
    assert "reduce_only=True" in src


def test_cmd_close_records_post_trade_cooldown_for_valgene():
    """v9.1.128: after a successful close, the per-portfolio
    post-trade cooldown must be recorded so the Keystone cooldown
    lever (+$42,573/yr) stays active for Val/Gene after a manual close."""
    src = _read("telegram_commands.py")
    assert "record_post_trade" in src
