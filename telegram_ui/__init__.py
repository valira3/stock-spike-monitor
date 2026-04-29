"""v5.11.1 \u2014 telegram_ui package.

Houses the Telegram-handler and presentation helpers extracted from
`trade_genius.py`. PR 1 introduces `charts` (chart and dayreport
helpers). Subsequent PRs in v5.11.x will move sync commands, menu,
and runtime, then retire deprecation shims.

Boot log line `[TELEGRAM-UI] modules loaded: charts` is emitted at
trade_genius startup so missed Dockerfile COPY lines surface as
ImportError on boot rather than mid-session.
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

LOADED_MODULES = ("charts",)

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
    "LOADED_MODULES",
]
