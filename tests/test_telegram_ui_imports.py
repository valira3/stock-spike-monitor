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


def test_trade_genius_no_telegram_ui_deprecation_aliases():
    """v5.12.0: telegram_ui chart/commands/menu deprecation aliases removed.

    `_chart_dayreport`, `_perf_compute`, `menu_callback` no longer live in
    trade_genius's namespace; callers must import them directly from
    telegram_ui.charts / .commands / .menu. Only the runtime entry points
    (`run_telegram_bot`, `_auth_guard`, etc.) are still re-exported for the
    `__main__` block.
    """
    import trade_genius as tg
    assert not hasattr(tg, "_chart_dayreport"), \
        "telegram_ui.charts deprecation alias should be removed in v5.12.0"
    assert not hasattr(tg, "_perf_compute"), \
        "telegram_ui.commands deprecation alias should be removed in v5.12.0"
    assert not hasattr(tg, "menu_callback"), \
        "telegram_ui.menu deprecation alias should be removed in v5.12.0"
    # runtime entry point is canonical, not a deprecation alias.
    assert tg.run_telegram_bot.__module__ == "telegram_ui.runtime"
