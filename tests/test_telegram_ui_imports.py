"""v5.11.1 regression: telegram_ui package public surface is reachable.

Cheap import-time guard \u2014 ensures none of the four submodules has a
broken import chain after future refactors.
"""
def test_telegram_ui_charts_imports():
    from telegram_ui import charts
    assert charts._chart_dayreport is not None
    assert charts._chart_equity_curve is not None
    assert charts._chart_portfolio_pie is not None


def test_telegram_ui_commands_imports():
    from telegram_ui import commands
    assert commands._perf_compute is not None
    assert commands._price_sync is not None
    assert commands._proximity_sync is not None


def test_telegram_ui_menu_imports():
    from telegram_ui import menu
    assert menu._build_menu_keyboard is not None
    assert menu._CallbackUpdateShim is not None
    assert menu.menu_callback is not None


def test_telegram_ui_runtime_imports():
    from telegram_ui import runtime
    assert runtime._auth_guard is not None
    assert runtime.run_telegram_bot is not None
    assert runtime.send_startup_message is not None


def test_trade_genius_deprecation_aliases():
    """Deprecation aliases in trade_genius.py route to telegram_ui.* modules."""
    import trade_genius as tg
    assert tg._chart_dayreport.__module__ == "telegram_ui.charts"
    assert tg._perf_compute.__module__ == "telegram_ui.commands"
    assert tg.menu_callback.__module__ == "telegram_ui.menu"
    assert tg.run_telegram_bot.__module__ == "telegram_ui.runtime"
