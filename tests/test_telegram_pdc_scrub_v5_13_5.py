"""v5.13.5 \u2014 Telegram surface vocabulary cleanup tests.

Asserts that PDC-anchor / dual-PDC vocabulary is scrubbed from the
user-facing Telegram surfaces (release banner, /strategy help text,
/proximity diagnostic) and replaced with Tiger Sovereign Phase 1-4
language. String-only checks \u2014 no algorithm coverage.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def telegram_modules(monkeypatch):
    """Boot trade_genius in smoke-test mode so we can introspect the
    /strategy, /proximity, deploy-banner, and runtime sources without
    triggering the live Telegram bot startup.
    """
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    sys.path.insert(0, str(REPO_ROOT))
    for mod in (
        "trade_genius",
        "telegram_commands",
        "telegram_ui",
        "telegram_ui.commands",
        "telegram_ui.runtime",
    ):
        if mod in sys.modules:
            del sys.modules[mod]
    import trade_genius  # noqa: F401
    import telegram_commands
    from telegram_ui import commands as ui_commands
    from telegram_ui import runtime as ui_runtime

    return telegram_commands, ui_commands, ui_runtime


def _src(obj) -> str:
    import inspect

    return inspect.getsource(obj)


def test_strategy_no_legacy_pdc_phrasing(telegram_modules):
    tc, _ui, _rt = telegram_modules
    body = _src(tc.cmd_strategy)
    forbidden = [
        "SPY > PDC",
        "SPY < PDC",
        "QQQ > PDC",
        "QQQ < PDC",
        "PDC + $0.90",
        "OR High \u2212 $0.90",
        "Price > PDC",
        "Price < PDC",
    ]
    for phrase in forbidden:
        assert phrase not in body, "v5.13.5: %r should be scrubbed from /strategy text" % phrase


def test_strategy_mentions_phase1_and_permit(telegram_modules):
    tc, _ui, _rt = telegram_modules
    body = _src(tc.cmd_strategy)
    assert "Phase 1" in body, "/strategy must surface Phase 1 vocabulary"
    assert re.search(r"[Pp]ermit", body), "/strategy must mention the Phase 1 Permit concept"


def test_strategy_mentions_tiger_sovereign_phases(telegram_modules):
    tc, _ui, _rt = telegram_modules
    body = _src(tc.cmd_strategy)
    for phase in ("Phase 1", "Phase 2", "Phase 3", "Phase 4"):
        assert phase in body, "/strategy must surface %s" % phase


def test_proximity_no_pdc_polarity_phrases(telegram_modules):
    _tc, ui, _rt = telegram_modules
    body = _src(ui._proximity_sync)
    forbidden = ["above PDC", "below PDC", "Polarity vs PDC"]
    for phrase in forbidden:
        assert phrase not in body, "v5.13.5: %r should be scrubbed from /proximity output" % phrase


def test_proximity_renders_permit_booleans(telegram_modules):
    _tc, ui, _rt = telegram_modules
    body = _src(ui._proximity_sync)
    assert "Long permit" in body, "/proximity must render the Phase 1 long permit boolean"
    assert "Short permit" in body, "/proximity must render the Phase 1 short permit boolean"


def test_deploy_banner_no_pdc_anchor(telegram_modules):
    """v5.13.5: 'PDC anchor' was the literal the user complained about
    in Telegram release notes. It lived in the deploy banner constructed
    by send_startup_message.
    """
    _tc, _ui, rt = telegram_modules
    src = _src(rt.send_startup_message)
    assert "PDC anchor" not in src, (
        "Deploy banner must not advertise 'PDC anchor' \u2014 retired in v5.9.0"
    )
    assert "Tiger Sovereign" in src, "Deploy banner must surface Tiger Sovereign vocabulary"


def test_regime_command_exists(telegram_modules):
    """v5.13.5: /regime is the new Phase 1 permit diagnostic."""
    tc, _ui, _rt = telegram_modules
    assert hasattr(tc, "cmd_regime"), "/regime command must be defined"


def test_regime_command_wired_in_runtime(telegram_modules):
    """The /regime CommandHandler must be registered."""
    _tc, _ui, rt = telegram_modules
    src = _src(rt)
    assert 'CommandHandler("regime"' in src, "/regime must be registered as a CommandHandler"


def test_bot_version_bumped():
    if "bot_version" in sys.modules:
        del sys.modules["bot_version"]
    sys.path.insert(0, str(REPO_ROOT))
    import bot_version

    # v5.13.6 superseded v5.13.5 in the same release window; assert >= 5.13.5
    # so this test stays valid through future patch bumps without churn.
    parts = tuple(int(p) for p in bot_version.BOT_VERSION.split("."))
    assert parts >= (5, 13, 5), "bot_version.BOT_VERSION must be at least 5.13.5"


def test_entry_telegram_message_no_pdc_lines():
    """v5.13.10 \u2014 the LONG ENTRY / SHORT ENTRY message emitted from
    ``broker/orders.py::execute_breakout`` must not advertise the legacy
    dual-PDC gate vocabulary. v5.13.9 retired the PDC compute on the
    dashboard; v5.13.10 retires the matching Telegram surface and replaces
    it with the actual Section I (QQQ 5m vs 9-EMA + 09:30 AVWAP) gates.
    """
    sys.path.insert(0, str(REPO_ROOT))
    if "broker.orders" in sys.modules:
        del sys.modules["broker.orders"]
    from broker import orders as bo  # noqa: WPS433 \u2014 import inside test by design

    src = _src(bo.execute_breakout)
    forbidden = [
        "Price > PDC",
        "Price < PDC",
        "SPY > PDC",
        "SPY < PDC",
        "QQQ > PDC",
        "QQQ < PDC",
        "PDC: $%.2f",
        "PDC      : $%.2f",
    ]
    for phrase in forbidden:
        assert phrase not in src, (
            "v5.13.10: %r should be scrubbed from the LONG/SHORT ENTRY "
            "Telegram message in broker/orders.py::execute_breakout" % phrase
        )


def test_entry_telegram_message_uses_section_i_gates():
    """v5.13.10 \u2014 the entry message must surface the actual Section I
    gates the entry path enforces: QQQ 5m close vs 9-EMA AND vs 09:30
    AVWAP, plus the boundary_hold (two consecutive 1m closes outside OR).
    """
    sys.path.insert(0, str(REPO_ROOT))
    if "broker.orders" in sys.modules:
        del sys.modules["broker.orders"]
    from broker import orders as bo  # noqa: WPS433

    src = _src(bo.execute_breakout)
    required = [
        "QQQ 5m close > 9-EMA",
        "QQQ 5m close > 09:30 AVWAP",
        "QQQ 5m close < 9-EMA",
        "QQQ 5m close < 09:30 AVWAP",
        "2nd 1m close > OR High",
        "2nd 1m close < OR Low",
    ]
    for phrase in required:
        assert phrase in src, (
            "v5.13.10: %r must appear in the LONG/SHORT ENTRY Telegram "
            "message in broker/orders.py::execute_breakout" % phrase
        )
