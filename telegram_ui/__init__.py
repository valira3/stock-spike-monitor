"""v5.11.1 \u2014 telegram_ui package.

Houses the Telegram-handler and presentation helpers extracted from
`trade_genius.py`. PR 1 introduced `charts` (chart and dayreport
helpers); PR 2 added `commands` (sync command builders for /log,
/replay, /perf, /price, /proximity, /orb, /or_now, /ticker, and the
/reset confirmation keyboard); PR 3 added `menu` (keyboards, callback
handlers, and the _CallbackUpdateShim dispatcher); PR 4 adds
`runtime` (bot lifecycle: auth guard, command registration, startup,
and the run_polling loop). `send_telegram` intentionally stays in
`trade_genius` because it's the broker-side notification entry used
by paper_state, error_state, and the scheduler.

Boot log line `[TELEGRAM-UI] modules loaded: charts, commands, menu, runtime`
is emitted at trade_genius startup so missed Dockerfile COPY lines
surface as ImportError on boot rather than mid-session.
"""
from __future__ import annotations

from telegram_ui.charts import (
    _dayreport_time,
    _dayreport_sort_key,
    _short_reason,
    _fmt_pnl,
    _chart_dayreport,
    _chart_equity_curve,
    _chart_portfolio_pie,
    _open_positions_as_pseudo_trades,
    _format_dayreport_section,
    _reply_in_chunks,
    _collect_day_rows,
)
from telegram_ui.commands import (
    _log_sync,
    _replay_sync,
    _reset_buttons,
    _perf_compute,
    _price_sync,
    _proximity_sync,
    _proximity_keyboard,
    _orb_sync,
    _fetch_or_for_ticker,
    _or_now_sync,
    _fmt_tickers_list,
    _fmt_add_reply,
    _fmt_remove_reply,
)
from telegram_ui.menu import (
    positions_callback,
    proximity_callback,
    monitoring_callback,
    _build_menu_keyboard,
    _build_advanced_menu_keyboard,
    _menu_button,
    _cb_open_menu,
    _CallbackUpdateShim,
    _invoke_from_callback,
    menu_callback,
)
from telegram_ui.runtime import (
    _set_bot_commands,
    _send_startup_menu,
    send_startup_message,
    _auth_guard,
    run_telegram_bot,
)

LOADED_MODULES = ("charts", "commands", "menu", "runtime")

__all__ = [
    "_dayreport_time",
    "_dayreport_sort_key",
    "_short_reason",
    "_fmt_pnl",
    "_chart_dayreport",
    "_chart_equity_curve",
    "_chart_portfolio_pie",
    "_open_positions_as_pseudo_trades",
    "_format_dayreport_section",
    "_reply_in_chunks",
    "_collect_day_rows",
    "_log_sync",
    "_replay_sync",
    "_reset_buttons",
    "_perf_compute",
    "_price_sync",
    "_proximity_sync",
    "_proximity_keyboard",
    "_orb_sync",
    "_fetch_or_for_ticker",
    "_or_now_sync",
    "_fmt_tickers_list",
    "_fmt_add_reply",
    "_fmt_remove_reply",
    "positions_callback",
    "proximity_callback",
    "monitoring_callback",
    "_build_menu_keyboard",
    "_build_advanced_menu_keyboard",
    "_menu_button",
    "_cb_open_menu",
    "_CallbackUpdateShim",
    "_invoke_from_callback",
    "menu_callback",
    "_set_bot_commands",
    "_send_startup_menu",
    "send_startup_message",
    "_auth_guard",
    "run_telegram_bot",
    "LOADED_MODULES",
]
