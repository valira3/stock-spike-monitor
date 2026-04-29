"""v5.11.1 \u2014 chart and dayreport helpers.

Extracted from trade_genius.py. Pure code motion \u2014 zero behavior
change. State (positions, short_positions, paper_trades, trade
history, REASON_LABELS, _SHORT_REASON, matplotlib state) still lives
in trade_genius; this module reaches it through the live-module
accessor `_tg()` so __main__ vs imported execution both work.
"""
from __future__ import annotations

import logging
import sys as _sys

# v5.11.1 \u2014 prod runs `python trade_genius.py`, so trade_genius is
# registered in sys.modules as `__main__`, NOT as `trade_genius`.
# Mirror the alias trick used by paper_state to make both names point
# at the same already-loaded module object.
if "trade_genius" not in _sys.modules and "__main__" in _sys.modules:
    _main = _sys.modules["__main__"]
    if getattr(_main, "BOT_NAME", None) == "TradeGenius":
        _sys.modules["trade_genius"] = _main


def _tg():
    """Live trade_genius module (handles __main__ vs imported cases)."""
    return _sys.modules.get("trade_genius") or _sys.modules.get("__main__")


logger = logging.getLogger(__name__)


def _dayreport_time(t, key):
    """Extract display time HH:MM from a trade record (CDT)."""
    iso = t.get(key + "_iso", "")
    if iso:
        return _tg()._parse_time_to_cdt(iso)
    raw = t.get(key, "")
    if raw and ":" in raw:
        return _tg()._parse_time_to_cdt(raw)
    return "..."


def _dayreport_sort_key(t):
    """Sort key for chronological ordering of trades."""
    iso = t.get("exit_time_iso", "")
    if iso:
        return iso
    return t.get("exit_time", "") or t.get("date", "")


def _short_reason(reason_key):
    """Map a reason key to short dayreport label."""
    tg = _tg()
    full = tg.REASON_LABELS.get(reason_key, reason_key)
    # Match by leading emoji character
    if full:
        first_char = full[0]
        if first_char in tg._SHORT_REASON:
            return tg._SHORT_REASON[first_char]
    return full


def _fmt_pnl(val):
    """Format P&L with unicode minus."""
    if val < 0:
        return "\u2212$%.2f" % abs(val)
    return "+$%.2f" % val


def _chart_dayreport(trades, day_label):
    """Generate trade P&L bar chart with cumulative line. Returns BytesIO or None."""
    tg = _tg()
    if not tg.MATPLOTLIB_AVAILABLE or not trades:
        return None
    plt = tg.plt
    _io = tg._io
    try:
        pnls = [(t.get("pnl") or 0) for t in trades]
        colors = ["#00cc66" if p >= 0 else "#ff4444" for p in pnls]
        fig, ax = plt.subplots(figsize=(8, 4))
        xs = list(range(1, len(pnls) + 1))
        ax.bar(xs, pnls, color=colors)
        ax.axhline(0, color="white", linewidth=0.5)
        ax.set_facecolor("#1a1a2e")
        fig.patch.set_facecolor("#1a1a2e")
        ax.tick_params(colors="white")
        ax.set_title("Trade P&L \u2014 %s" % day_label, color="white")
        ax.set_xlabel("Trade #", color="white")
        ax.set_ylabel("P&L ($)", color="white")
        for spine in ax.spines.values():
            spine.set_color("#444")
        # Cumulative line
        cum = []
        running = 0
        for p in pnls:
            running += p
            cum.append(running)
        ax2 = ax.twinx()
        ax2.plot(xs, cum, color="cyan", linewidth=2, label="Cumulative")
        ax2.tick_params(colors="white")
        ax2.set_ylabel("Cumulative ($)", color="white")
        for spine in ax2.spines.values():
            spine.set_color("#444")
        plt.tight_layout()
        buf = _io.BytesIO()
        plt.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor())
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as e:
        logger.warning("Chart generation failed: %s", e)
        return None


def _chart_equity_curve(history, label):
    """Generate equity curve line chart. Returns BytesIO or None."""
    tg = _tg()
    if not tg.MATPLOTLIB_AVAILABLE or not history:
        return None
    plt = tg.plt
    _io = tg._io
    try:
        # Group by date and compute daily P&L
        daily = {}
        for t in history:
            d = t.get("date", "")
            if d:
                daily[d] = daily.get(d, 0) + (t.get("pnl") or 0)
        if not daily:
            return None
        dates_sorted = sorted(daily.keys())
        daily_pnls = [daily[d] for d in dates_sorted]
        cum = []
        running = 0
        for p in daily_pnls:
            running += p
            cum.append(running)
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot(range(len(cum)), cum, color="cyan", linewidth=2)
        ax.fill_between(range(len(cum)), cum, alpha=0.15, color="cyan")
        ax.axhline(0, color="white", linewidth=0.5)
        ax.set_facecolor("#1a1a2e")
        fig.patch.set_facecolor("#1a1a2e")
        ax.tick_params(colors="white")
        ax.set_title("Equity Curve \u2014 %s" % label, color="white")
        ax.set_xlabel("Trading Day", color="white")
        ax.set_ylabel("Cumulative P&L ($)", color="white")
        for spine in ax.spines.values():
            spine.set_color("#444")
        # X-axis date labels
        if len(dates_sorted) <= 15:
            ax.set_xticks(range(len(dates_sorted)))
            short_labels = [d[5:] for d in dates_sorted]  # MM-DD
            ax.set_xticklabels(short_labels, rotation=45, ha="right", fontsize=8, color="white")
        plt.tight_layout()
        buf = _io.BytesIO()
        plt.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor())
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as e:
        logger.warning("Equity chart generation failed: %s", e)
        return None


def _chart_portfolio_pie(pos_dict, short_dict, cash):
    """Generate portfolio allocation pie chart. Returns BytesIO or None."""
    tg = _tg()
    if not tg.MATPLOTLIB_AVAILABLE:
        return None
    if not pos_dict and not short_dict:
        return None
    plt = tg.plt
    _io = tg._io
    fetch_1min_bars = tg.fetch_1min_bars
    try:
        from collections import OrderedDict
        slices = OrderedDict()
        for ticker, pos in pos_dict.items():
            bars = fetch_1min_bars(ticker)
            if bars:
                mkt_val = bars["current_price"] * pos["shares"]
            else:
                mkt_val = pos["entry_price"] * pos["shares"]
            lbl = "%s (L)" % ticker
            slices[lbl] = abs(mkt_val)
        for ticker, pos in short_dict.items():
            bars = fetch_1min_bars(ticker)
            if bars:
                mkt_val = bars["current_price"] * pos["shares"]
            else:
                mkt_val = pos["entry_price"] * pos["shares"]
            lbl = "%s (S)" % ticker
            slices[lbl] = abs(mkt_val)
        if cash > 0:
            slices["Cash"] = cash
        if not slices:
            return None
        labels = list(slices.keys())
        sizes = list(slices.values())
        # Color palette
        base_colors = ["#00cc66", "#ff4444", "#4488ff", "#ffaa00", "#cc44ff",
                       "#00cccc", "#ff6688", "#88cc00", "#ff8800", "#8844ff"]
        colors = []
        ci = 0
        for lbl in labels:
            if lbl == "Cash":
                colors.append("#666666")
            else:
                colors.append(base_colors[ci % len(base_colors)])
                ci += 1
        fig, ax = plt.subplots(figsize=(6, 6))
        fig.patch.set_facecolor("#1a1a2e")
        ax.set_facecolor("#1a1a2e")
        wedges, texts, autotexts = ax.pie(
            sizes, labels=labels, colors=colors, autopct="%.1f%%",
            startangle=90, textprops={"color": "white", "fontsize": 10}
        )
        for t in autotexts:
            t.set_color("white")
            t.set_fontsize(9)
        ax.set_title("Portfolio Allocation", color="white", fontsize=14)
        plt.tight_layout()
        buf = _io.BytesIO()
        plt.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor())
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as e:
        logger.warning("Pie chart generation failed: %s", e)
        return None


def _open_positions_as_pseudo_trades(target_date=None):
    """Build synthetic trade records for currently-open positions.

    v3.3.1: /perf and /dayreport historically only read
    `trade_history` / `short_trade_history`, which are populated on
    exit (sell / cover) \u2014 never on entry. An open-but-uncovered
    position was invisible to both commands even though /status showed
    it fine. This helper produces pseudo-trade records that slot into
    the same rendering pipeline (they have no exit_* fields, so
    _format_dayreport_section treats them as 'time \u2192 open').

    Unrealized P&L is computed from live 1-min bars; if bars are
    unavailable we fall back to 0 (fail-safe \u2014 we do NOT invent a
    price).

    Returns (long_opens, short_opens). Each list is date-filtered to
    `target_date` (YYYY-MM-DD) when provided; otherwise all opens.
    """
    tg = _tg()
    long_pos = tg.positions
    short_pos = tg.short_positions
    fetch_1min_bars = tg.fetch_1min_bars

    long_opens = []
    for ticker, pos in long_pos.items():
        date_str = pos.get("date", "")
        if target_date and date_str != target_date:
            continue
        entry_p = pos.get("entry_price", 0)
        shares = pos.get("shares", 0)
        bars = fetch_1min_bars(ticker)
        cur = bars["current_price"] if bars else None
        if cur is not None and entry_p:
            unreal = round((cur - entry_p) * shares, 2)
            unreal_pct = round((cur - entry_p) / entry_p * 100, 2)
        else:
            unreal = 0.0
            unreal_pct = 0.0
        long_opens.append({
            "ticker": ticker,
            "side": "long",
            "action": "OPEN",
            "shares": shares,
            "entry_price": entry_p,
            "exit_price": cur if cur is not None else entry_p,
            "pnl": unreal,
            "pnl_pct": unreal_pct,
            "unrealized": True,
            "reason": "OPEN",
            "entry_time": pos.get("entry_time", ""),
            "entry_time_iso": pos.get("entry_time", ""),
            "date": date_str,
            "entry_num": pos.get("entry_count", 1),
        })

    short_opens = []
    for ticker, pos in short_pos.items():
        date_str = pos.get("date", "")
        if target_date and date_str != target_date:
            continue
        entry_p = pos.get("entry_price", 0)
        shares = pos.get("shares", 0)
        bars = fetch_1min_bars(ticker)
        cur = bars["current_price"] if bars else None
        if cur is not None and entry_p:
            unreal = round((entry_p - cur) * shares, 2)
            unreal_pct = round((entry_p - cur) / entry_p * 100, 2)
        else:
            unreal = 0.0
            unreal_pct = 0.0
        short_opens.append({
            "ticker": ticker,
            "side": "short",
            "action": "OPEN",
            "shares": shares,
            "entry_price": entry_p,
            "exit_price": cur if cur is not None else entry_p,
            "pnl": unreal,
            "pnl_pct": unreal_pct,
            "unrealized": True,
            "reason": "OPEN",
            "entry_time": pos.get("entry_time", ""),
            "entry_time_iso": pos.get("entry_time", ""),
            "date": date_str,
        })

    return long_opens, short_opens


def _format_dayreport_section(trades, header, count_label):
    """Format one portfolio section for /dayreport (compact 2-line).

    header: e.g. '\U0001f4ca Day Report \u2014 Thu Apr 16' or '' for
        subsequent sections.
    count_label: e.g. 'Paper' or 'TP'.

    v3.3.1: Trades flagged `unrealized=True` (from
    _open_positions_as_pseudo_trades) are shown separately in the
    summary header so the 'closed P&L' number doesn't include live
    marks, and the trade list renders them as '\u2192open' via the
    existing has_exit branch below.
    """
    SEP = "\u2500" * 26
    lines = []
    if header:
        lines.append(header)

    trades_sorted = sorted(trades, key=_dayreport_sort_key) if trades else []
    realized = [t for t in trades_sorted if not t.get("unrealized")]
    unrealized = [t for t in trades_sorted if t.get("unrealized")]
    realized_pnl = sum(t.get("pnl", 0) for t in realized)
    unreal_pnl = sum(t.get("pnl", 0) for t in unrealized)

    lines.append(SEP)
    lines.append("%s: %d closed  P&L: %s"
                 % (count_label, len(realized), _fmt_pnl(realized_pnl)))
    if unrealized:
        lines.append("  Open: %d  Unreal: %s"
                     % (len(unrealized), _fmt_pnl(unreal_pnl)))
    lines.append(SEP)

    for idx, t in enumerate(trades_sorted, 1):
        ticker = t.get("ticker", "?")
        side = t.get("side", "long")
        arrow = "\u2191" if side == "long" else "\u2193"
        entry_p = t.get("entry_price", 0)
        exit_p = t.get("exit_price", t.get("price", 0))
        t_pnl = t.get("pnl", 0)
        reason = t.get("reason", "?")
        in_time = _dayreport_time(t, "entry_time")
        out_time = _dayreport_time(t, "exit_time")

        # Open position: no exit yet
        has_exit = bool(t.get("exit_time_iso") or t.get("exit_time"))
        if has_exit:
            time_span = "%s\u2192%s" % (in_time, out_time)
            price_str = "$%.2f\u2192$%.2f" % (entry_p, exit_p)
        else:
            time_span = "%s\u2192open" % in_time
            price_str = "$%.2f" % entry_p

        line1 = "%2d. %s %s  %s  %s" % (idx, ticker, arrow, time_span, _fmt_pnl(t_pnl))
        line2 = "    %s  %s" % (price_str, _short_reason(reason))
        lines.append(line1)
        lines.append(line2)

    return "\n".join(lines)


async def _reply_in_chunks(message, text, max_len=3800, reply_markup=None):
    """Send text in <=max_len-char chunks, splitting on newlines."""
    lines = text.split('\n')
    chunk = []
    length = 0
    for line in lines:
        line_len = len(line) + 1  # +1 for newline
        if length + line_len > max_len and chunk:
            await message.reply_text('\n'.join(chunk))
            chunk = []
            length = 0
        chunk.append(line)
        length += line_len
    if chunk:
        await message.reply_text('\n'.join(chunk), reply_markup=reply_markup)


def _collect_day_rows(target_str, today_str):
    """Collect all trade-log rows for one day, normalized.

    Returns a list of dicts:
      {"tm": "HH:MM", "ticker": str,
       "action": "BUY"|"SELL"|"SHORT"|"COVER",
       "shares": int, "price": float,
       "stop": float (BUY/SHORT only),
       "pnl": float (SELL/COVER only),
       "pnl_pct": float (SELL/COVER only)}

    v3.4.7: previously the same-day branch only pulled from paper_trades,
    which never contain shorts. Today's shorts (open or closed) were
    silently invisible. Now we pull from four sources for the today
    branch and synthesize rows from history for past dates.
    """
    tg = _tg()
    rows = []
    is_today = (target_str == today_str)

    live_long = tg.paper_trades
    long_hist = tg.trade_history
    short_hist = tg.short_trade_history
    open_shorts = tg.short_positions

    if is_today:
        # Long opens + closes are already in paper_trades
        for t in live_long:
            if t.get("date", "") != target_str:
                continue
            rows.append({
                "tm": t.get("time", "--:--") or "--:--",
                "ticker": t.get("ticker", "?"),
                "action": t.get("action", "?"),
                "shares": t.get("shares", 0) or 0,
                "price": t.get("price", 0) or 0,
                "stop": t.get("stop", 0) or 0,
                "pnl": t.get("pnl", 0) or 0,
                "pnl_pct": t.get("pnl_pct", 0) or 0,
            })
        # Closed shorts today \u2014 synthesize an OPEN row + a COVER row
        for t in short_hist:
            if t.get("date", "") != target_str:
                continue
            ticker = t.get("ticker", "?")
            shares = t.get("shares", 0) or 0
            rows.append({
                "tm": (t.get("entry_time") or "--:--")[:5],
                "ticker": ticker, "action": "SHORT", "shares": shares,
                "price": t.get("entry_price", 0) or 0,
                "stop": 0, "pnl": 0, "pnl_pct": 0,
            })
            rows.append({
                "tm": (t.get("exit_time") or "--:--")[:5],
                "ticker": ticker, "action": "COVER", "shares": shares,
                "price": t.get("exit_price", 0) or 0,
                "stop": 0,
                "pnl": t.get("pnl", 0) or 0,
                "pnl_pct": t.get("pnl_pct", 0) or 0,
            })
        # Currently-open shorts from today \u2014 add a SHORT open row only
        for ticker, pos in open_shorts.items():
            if pos.get("date", "") != target_str:
                continue
            rows.append({
                "tm": (pos.get("entry_time") or "--:--")[:5],
                "ticker": ticker, "action": "SHORT",
                "shares": pos.get("shares", 0) or 0,
                "price": pos.get("entry_price", 0) or 0,
                "stop": pos.get("stop", 0) or 0,
                "pnl": 0, "pnl_pct": 0,
            })
    else:
        # Past dates: synthesize from history
        for t in long_hist:
            if t.get("date", "") != target_str:
                continue
            ticker = t.get("ticker", "?")
            shares = t.get("shares", 0) or 0
            rows.append({
                "tm": (t.get("entry_time") or "--:--")[:5],
                "ticker": ticker, "action": "BUY", "shares": shares,
                "price": t.get("entry_price", 0) or 0,
                "stop": 0, "pnl": 0, "pnl_pct": 0,
            })
            rows.append({
                "tm": (t.get("exit_time") or "--:--")[:5],
                "ticker": ticker, "action": "SELL", "shares": shares,
                "price": t.get("exit_price", 0) or 0,
                "stop": 0,
                "pnl": t.get("pnl", 0) or 0,
                "pnl_pct": t.get("pnl_pct", 0) or 0,
            })
        for t in short_hist:
            if t.get("date", "") != target_str:
                continue
            ticker = t.get("ticker", "?")
            shares = t.get("shares", 0) or 0
            rows.append({
                "tm": (t.get("entry_time") or "--:--")[:5],
                "ticker": ticker, "action": "SHORT", "shares": shares,
                "price": t.get("entry_price", 0) or 0,
                "stop": 0, "pnl": 0, "pnl_pct": 0,
            })
            rows.append({
                "tm": (t.get("exit_time") or "--:--")[:5],
                "ticker": ticker, "action": "COVER", "shares": shares,
                "price": t.get("exit_price", 0) or 0,
                "stop": 0,
                "pnl": t.get("pnl", 0) or 0,
                "pnl_pct": t.get("pnl_pct", 0) or 0,
            })

    # Sort by time; "--:--" sinks to the end but keeps relative order.
    rows.sort(key=lambda r: (r["tm"] == "--:--", r["tm"]))
    return rows
