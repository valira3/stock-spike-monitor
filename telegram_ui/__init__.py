"""v5.11.1 \u2014 telegram_ui package.

Houses the Telegram-handler and presentation helpers extracted from
`trade_genius.py`. PR 1 introduces `charts` (chart and dayreport
helpers); PR 2 adds `commands` (sync command builders for /log,
/replay, /perf, /price, /proximity, /orb, /or_now, /ticker, and the
/reset confirmation keyboard). Subsequent PRs in v5.11.x will move
menu callbacks and runtime, then retire deprecation shims.

Boot log line `[TELEGRAM-UI] modules loaded: charts, commands` is
emitted at trade_genius startup so missed Dockerfile COPY lines
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

LOADED_MODULES = ("charts", "commands")

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
    "LOADED_MODULES",
]
