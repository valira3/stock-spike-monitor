"""telegram_ui.commands \u2014 synchronous command builders for Telegram screens.

Extracted from trade_genius.py in v5.11.1 PR 2. Pure code motion \u2014
zero behavior change. State (positions, short_positions, paper_trades,
trade history, OR/PDC dicts, MATPLOTLIB_AVAILABLE, etc.) still lives
in trade_genius; this module reaches it through the live-module
accessor `_tg()` so __main__ vs imported execution both work.
"""
from __future__ import annotations

import logging
import sys as _sys
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

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


def _log_sync(target_str, day_label):
    """Build trade log text (pure CPU \u2014 run in executor). Returns text or None."""
    tg = _tg()
    SEP = "\u2500" * 34
    today_str = tg._now_et().strftime("%Y-%m-%d")
    rows = tg._collect_day_rows(target_str, today_str)
    if not rows:
        return None

    lines = [
        "\U0001f4cb Trade Log \u2014 %s" % day_label,
        SEP,
    ]
    OPENS = ("BUY", "SHORT")
    CLOSES = ("SELL", "COVER")
    n_closed = 0
    day_pnl = 0.0
    for r in rows:
        tm = r["tm"]
        ticker = r["ticker"]
        action = r["action"]
        shares = r["shares"]
        price = r["price"]
        if action in OPENS:
            stop = r["stop"]
            lines.append(
                "%s  %-5s %s  %d @ $%.2f  stop $%.2f"
                % (tm, action, ticker, shares, price, stop)
            )
        else:
            n_closed += 1
            pnl_v = r["pnl"]
            pnl_p = r["pnl_pct"]
            day_pnl += pnl_v
            lines.append(
                "%s  %-5s %s  %d @ $%.2f  P&L: $%+.2f (%+.2f%%)"
                % (tm, action, ticker, shares, price, pnl_v, pnl_p)
            )

    n_open = len(tg.positions) + len(tg.short_positions)
    lines.append(SEP)
    lines.append("Completed: %d trades  Open: %d positions" % (n_closed, n_open))
    lines.append("Day P&L: ${:+,.2f}".format(day_pnl))
    return "\n".join(lines)


def _replay_sync(target_str, day_label):
    """Build replay text (pure CPU \u2014 run in executor). Returns text or None."""
    tg = _tg()
    SEP = "\u2500" * 34
    today_str = tg._now_et().strftime("%Y-%m-%d")

    # Normalize every source into a common row shape:
    #   {"tm": "HH:MM", "ticker": str, "action": "BUY"|"SELL"|"SHORT"|"COVER",
    #    "price": float, "pnl": float (0 for opens)}
    # Same-day source (paper_trades) already uses time/price/action.
    # Historical sources (trade_history / short_trade_history) store one
    # record per CLOSED trade with entry_time/entry_price and
    # exit_time/exit_price, so we synthesize both an open row and a
    # close row for each.
    rows = []

    def _push_live(src):
        for t in src:
            if t.get("date", "") != target_str:
                continue
            rows.append({
                "tm": t.get("time", "--:--"),
                "ticker": t.get("ticker", "?"),
                "action": t.get("action", "?"),
                "price": t.get("price", 0) or 0,
                "pnl": t.get("pnl", 0) or 0,
            })

    def _push_history(src, open_action, close_action):
        for t in src:
            if t.get("date", "") != target_str:
                continue
            ticker = t.get("ticker", "?")
            rows.append({
                "tm": t.get("entry_time", "--:--") or "--:--",
                "ticker": ticker,
                "action": open_action,
                "price": t.get("entry_price", 0) or 0,
                "pnl": 0,
            })
            rows.append({
                "tm": t.get("exit_time", "--:--") or "--:--",
                "ticker": ticker,
                "action": close_action,
                "price": t.get("exit_price", 0) or 0,
                "pnl": t.get("pnl", 0) or 0,
            })

    def _push_open_shorts(src):
        # Currently-open short positions on the target date \u2014 add a SHORT
        # row only (no close row yet). v3.4.7: replay missed today's open
        # shorts because paper_trades never holds shorts.
        for ticker, pos in src.items():
            if pos.get("date", "") != target_str:
                continue
            rows.append({
                "tm": (pos.get("entry_time") or "--:--")[:5],
                "ticker": ticker,
                "action": "SHORT",
                "price": pos.get("entry_price", 0) or 0,
                "pnl": 0,
            })

    prefix = ""
    if target_str == today_str:
        _push_live(tg.paper_trades)
        # v3.4.7: today's shorts (closed + open) live elsewhere
        _push_history(tg.short_trade_history, "SHORT", "COVER")
        _push_open_shorts(tg.short_positions)
    else:
        _push_history(tg.trade_history, "BUY", "SELL")
        _push_history(tg.short_trade_history, "SHORT", "COVER")

    # Sort by time; unknown "--:--" sinks to the end but keeps relative order.
    rows.sort(key=lambda r: (r["tm"] == "--:--", r["tm"]))
    if not rows:
        return None

    lines = [
        "\U0001f504 %sTrade Replay \u2014 %s" % (prefix, day_label),
        SEP,
    ]
    cum_pnl = 0.0
    open_count = 0
    wins = 0
    losses = 0
    OPENS = ("BUY", "SHORT")
    for r in rows:
        tm = r["tm"]
        ticker = r["ticker"]
        action = r["action"]
        price = r["price"]
        if action in OPENS:
            open_count += 1
            lines.append(
                "%s \u2192 %-5s %s  $%.2f  [positions: %d]"
                % (tm, action, ticker, price, open_count)
            )
        else:
            open_count = max(0, open_count - 1)
            pnl_val = r["pnl"]
            cum_pnl += pnl_val
            if pnl_val > 0:
                wins += 1
            else:
                losses += 1
            cum_fmt = "%+.2f" % cum_pnl
            lines.append(
                "%s \u2192 %-5s %s  $%.2f  $%+.2f   cumP&L: $%s"
                % (tm, action, ticker, price, pnl_val, cum_fmt)
            )
    lines.append(SEP)
    n_sells = wins + losses
    cum_pnl_fmt = "%+.2f" % cum_pnl
    lines.append(
        "Final P&L: $%s  |  Trades: %d  |  W: %d  L: %d"
        % (cum_pnl_fmt, n_sells, wins, losses)
    )
    return "\n".join(lines)


def _reset_buttons(action: str) -> InlineKeyboardMarkup:
    """Build a Confirm/Cancel keyboard where Confirm carries a fresh ts."""
    tg = _tg()
    ts = int(time.time())
    confirm_data = "reset_%s_confirm:%d" % (action, ts)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("\u2705 Confirm", callback_data=confirm_data),
        InlineKeyboardButton("\u274c Cancel", callback_data="reset_cancel"),
    ]])


def _perf_compute(long_history, short_hist, date_filter, single_day, today,
                  label, perf_label, long_opens=None, short_opens=None):
    """Synchronous helper: crunch all perf stats + chart. Runs in executor.

    v3.3.1: `long_opens` / `short_opens` are lists of pseudo-trades for
    currently-open positions (see `_open_positions_as_pseudo_trades`).
    They are NOT folded into the realized-performance math (would
    pollute win-rate / totals with live marks). They render as a
    dedicated 'Open Positions' section so the user can see unrealized
    P&L alongside historical stats.
    """
    tg = _tg()
    long_opens = long_opens or []
    short_opens = short_opens or []
    SEP = "\u2500" * 34

    if single_day:
        filt_long = [t for t in long_history if t.get("date", "") == date_filter]
        filt_short = [t for t in short_hist if t.get("date", "") == date_filter]
    elif date_filter:
        filt_long = [t for t in long_history if t.get("date", "") >= date_filter]
        filt_short = [t for t in short_hist if t.get("date", "") >= date_filter]
    else:
        filt_long = list(long_history)
        filt_short = list(short_hist)

    lines = [
        "\U0001f4c8 Performance \u2014 %s \u2014 %s" % (label, perf_label),
        SEP,
    ]

    # Open Positions section (v3.3.1)
    if long_opens or short_opens:
        lines.append("\U0001f4cc Open Positions")
        total_unreal = 0.0
        for p in long_opens:
            tk = p.get("ticker", "?")
            ep = p.get("entry_price", 0)
            sh = p.get("shares", 0)
            cp = p.get("exit_price", ep)
            pl = p.get("pnl", 0)
            pct = p.get("pnl_pct", 0)
            total_unreal += pl
            lines.append("  \u2191 %s  %d sh  $%.2f \u2192 $%.2f"
                         % (tk, sh, ep, cp))
            lines.append("      Unreal: $%+.2f (%+.2f%%)" % (pl, pct))
        for p in short_opens:
            tk = p.get("ticker", "?")
            ep = p.get("entry_price", 0)
            sh = p.get("shares", 0)
            cp = p.get("exit_price", ep)
            pl = p.get("pnl", 0)
            pct = p.get("pnl_pct", 0)
            total_unreal += pl
            lines.append("  \u2193 %s  %d sh  $%.2f \u2192 $%.2f"
                         % (tk, sh, ep, cp))
            lines.append("      Unreal: $%+.2f (%+.2f%%)" % (pl, pct))
        lines.append("  Total Unrealized: $%+.2f" % total_unreal)
        lines.append(SEP)

    # LONG Performance
    lines.append("\U0001f4c8 LONG Performance")
    all_stats = tg._compute_perf_stats(filt_long)
    if all_stats:
        best_tk = all_stats["best"].get("ticker", "?")
        best_pnl = all_stats["best"].get("pnl", 0)
        worst_tk = all_stats["worst"].get("ticker", "?")
        worst_pnl = all_stats["worst"].get("pnl", 0)
        lines.append("  Trades:    %d  (W:%d  L:%d)" % (
            all_stats["n"], all_stats["wins"], all_stats["losses"]))
        lines.append("  Win Rate:  %.1f%%" % all_stats["wr"])
        lines.append("  Total P&L: $%+.2f" % all_stats["total_pnl"])
        lines.append("  Avg Win:   $%+.2f  Avg Loss: $%+.2f"
                     % (all_stats["avg_win"], all_stats["avg_loss"]))
        lines.append("  Best:      %s $%+.2f" % (best_tk, best_pnl))
        lines.append("  Worst:     %s $%+.2f" % (worst_tk, worst_pnl))
    else:
        lines.append("  No long trades")
    lines.append(SEP)

    # SHORT Performance
    lines.append("\U0001f4c9 SHORT Performance")
    short_stats = tg._compute_perf_stats(filt_short)
    if short_stats:
        s_best_tk = short_stats["best"].get("ticker", "?")
        s_best_pnl = short_stats["best"].get("pnl", 0)
        s_worst_tk = short_stats["worst"].get("ticker", "?")
        s_worst_pnl = short_stats["worst"].get("pnl", 0)
        lines.append("  Trades:    %d  (W:%d  L:%d)" % (
            short_stats["n"], short_stats["wins"], short_stats["losses"]))
        lines.append("  Win Rate:  %.1f%%" % short_stats["wr"])
        lines.append("  Total P&L: $%+.2f" % short_stats["total_pnl"])
        lines.append("  Avg Win:   $%+.2f  Avg Loss: $%+.2f"
                     % (short_stats["avg_win"], short_stats["avg_loss"]))
        lines.append("  Best:      %s $%+.2f" % (s_best_tk, s_best_pnl))
        lines.append("  Worst:     %s $%+.2f" % (s_worst_tk, s_worst_pnl))
    else:
        lines.append("  No short trades")
    lines.append(SEP)

    # Combined today
    today_long = tg._compute_perf_stats(long_history, date_filter=today)
    today_short = tg._compute_perf_stats(short_hist, date_filter=today)
    lines.append("Today")
    if today_long:
        lines.append("  Long:  %d trades  P&L $%+.2f"
                     % (today_long["n"], today_long["total_pnl"]))
    if today_short:
        lines.append("  Short: %d trades  P&L $%+.2f"
                     % (today_short["n"], today_short["total_pnl"]))
    if not today_long and not today_short:
        lines.append("  No trades today")
    lines.append(SEP)

    # Streak (combined)
    combined = list(long_history) + list(short_hist)
    streak = tg._compute_streak(combined)
    lines.append("Streak: %s" % streak)

    msg = "\n".join(lines)

    # Chart: Equity curve
    chart_buf = None
    if tg.MATPLOTLIB_AVAILABLE:
        chart_hist = filt_long + filt_short
        if chart_hist:
            chart_buf = tg._chart_equity_curve(chart_hist, perf_label)

    return msg, chart_buf


def _price_sync(ticker):
    """Build price text (blocking I/O \u2014 run in executor). Returns text or None."""
    tg = _tg()
    SEP = "\u2500" * 34

    bars = tg.fetch_1min_bars(ticker)
    if not bars:
        return None

    cur_price = bars["current_price"]
    pdc_val = bars["pdc"]
    change = cur_price - pdc_val
    change_pct = (change / pdc_val * 100) if pdc_val else 0

    header = "\U0001f4b0 %s  $%.2f  $%+.2f (%+.2f%%)" % (ticker, cur_price, change, change_pct)

    if ticker not in tg.TRADE_TICKERS:
        return header

    lines = [header, SEP]

    # OR High
    orh = tg.or_high.get(ticker)
    if orh is not None:
        dist = cur_price - orh
        if cur_price > orh:
            or_status = "\u2705 Above (by $%.2f)" % dist
        else:
            or_status = "\u274c Below (by $%.2f)" % abs(dist)
        lines.append("OR High:  $%.2f  %s" % (orh, or_status))
    else:
        lines.append("OR High:  not collected")

    # OR Low
    orl = tg.or_low.get(ticker)
    if orl is not None:
        dist_low = cur_price - orl
        if cur_price < orl:
            orl_status = "\U0001fa78 Below (by $%.2f)" % abs(dist_low)
        else:
            orl_status = "\u2705 Above (by $%.2f)" % dist_low
        lines.append("OR Low:   $%.2f  %s" % (orl, orl_status))
    else:
        lines.append("OR Low:   not collected")

    # PDC
    pdc_strat = tg.pdc.get(ticker)
    if pdc_strat is not None:
        if cur_price > pdc_strat:
            pdc_status = "\u2705 Above (green)"
        else:
            pdc_status = "\u274c Below (red)"
        lines.append("PDC:      $%.2f  %s" % (pdc_strat, pdc_status))
    else:
        lines.append("PDC:      $%.2f" % pdc_val)

    # SPY/QQQ vs PDC (v3.4.34: swapped from AVWAP)
    spy_pdc_t = tg.pdc.get("SPY") or 0
    qqq_pdc_t = tg.pdc.get("QQQ") or 0
    spy_bars = tg.fetch_1min_bars("SPY")
    qqq_bars = tg.fetch_1min_bars("QQQ")
    spy_price_val = spy_bars["current_price"] if spy_bars else 0
    qqq_price_val = qqq_bars["current_price"] if qqq_bars else 0
    spy_ok = (spy_price_val > spy_pdc_t) if (spy_bars and spy_pdc_t > 0) else False
    qqq_ok = (qqq_price_val > qqq_pdc_t) if (qqq_bars and qqq_pdc_t > 0) else False
    spy_below = (spy_price_val < spy_pdc_t) if (spy_bars and spy_pdc_t > 0) else False
    qqq_below = (qqq_price_val < qqq_pdc_t) if (qqq_bars and qqq_pdc_t > 0) else False
    spy_icon = "\u2705" if spy_ok else "\u274c"
    qqq_icon = "\u2705" if qqq_ok else "\u274c"
    filter_status = "active" if (spy_ok and qqq_ok) else "inactive"
    lines.append("SPY/QQQ:  %s %s Index filters %s" % (spy_icon, qqq_icon, filter_status))
    lines.append(SEP)

    # Long entry eligible?
    in_position = ticker in tg.positions
    at_max_entries = tg.daily_entry_count.get(ticker, 0) >= 5
    index_ok = spy_ok and qqq_ok
    long_eligible = not in_position and not at_max_entries and index_ok and not tg._trading_halted

    if long_eligible:
        lines.append("Long eligible:  YES")
    else:
        reasons = []
        if in_position:
            reasons.append("in position")
        if at_max_entries:
            reasons.append("5 entries today")
        if not index_ok:
            reasons.append("index filter fails")
        if tg._trading_halted:
            reasons.append("trading halted")
        reason_str = ", ".join(reasons)
        lines.append("Long eligible:  NO (%s)" % reason_str)

    # Short entry eligible?
    in_short = ticker in tg.short_positions
    at_max_shorts = tg.daily_short_entry_count.get(ticker, 0) >= 5
    index_bearish = spy_below and qqq_below
    below_or_low = (orl is not None and cur_price < orl)
    below_pdc_short = (pdc_strat is not None and cur_price < pdc_strat)
    short_eligible = (not in_short and not at_max_shorts and index_bearish
                      and below_or_low and below_pdc_short and not tg._trading_halted)

    if short_eligible:
        lines.append("Short eligible: YES")
    else:
        s_reasons = []
        if in_short:
            s_reasons.append("in short position")
        if at_max_shorts:
            s_reasons.append("5 short entries today")
        if not index_bearish:
            s_reasons.append("index filter not bearish")
        if not below_or_low:
            s_reasons.append("above OR Low")
        if not below_pdc_short:
            s_reasons.append("above PDC")
        if tg._trading_halted:
            s_reasons.append("trading halted")
        s_reason_str = ", ".join(s_reasons)
        lines.append("Short eligible: NO (%s)" % s_reason_str)

    return "\n".join(lines)


def _proximity_sync():
    """Build proximity text (blocking I/O \u2014 run in executor).

    Shows how far each ticker is from its OR-breakout trigger, plus the
    SPY/QQQ vs PDC global gate. Read-only diagnostic view \u2014 does
    NOT change any trade logic or adaptive parameters.
    v3.4.34: anchor swapped from AVWAP to PDC.

    Every visible line is <= 34 chars incl. leading 2-space indent so it
    renders without wrap inside a Telegram mobile monospace block.

    Returns (text, None) on success or (None, err_msg) on no-data.
    """
    tg = _tg()
    SEP = "\u2500" * 34
    now_et = tg._now_et()
    today = now_et.strftime("%Y-%m-%d")

    if tg.or_collected_date != today:
        return None, "OR not collected yet \u2014 runs at 8:35 CT."

    # Pick the positions dicts for open-trade markers
    longs_dict = tg.positions
    shorts_dict = tg.short_positions

    # --- Global: SPY/QQQ vs PDC (the long gate, v3.4.34) ---
    spy_bars = tg.fetch_1min_bars("SPY")
    qqq_bars = tg.fetch_1min_bars("QQQ")
    spy_price = spy_bars["current_price"] if spy_bars else 0.0
    qqq_price = qqq_bars["current_price"] if qqq_bars else 0.0
    spy_pdc_p = tg.pdc.get("SPY") or 0
    qqq_pdc_p = tg.pdc.get("QQQ") or 0

    spy_have = spy_price > 0 and spy_pdc_p > 0
    qqq_have = qqq_price > 0 and qqq_pdc_p > 0
    spy_ok = spy_have and spy_price > spy_pdc_p
    qqq_ok = qqq_have and qqq_price > qqq_pdc_p
    spy_icon = "\u2705" if spy_ok else "\u274c"
    qqq_icon = "\u2705" if qqq_ok else "\u274c"

    long_ok = spy_ok and qqq_ok
    # Short anchor is the mirror: SPY AND QQQ both BELOW PDC enables shorts.
    short_ok = (spy_have and qqq_have
                and spy_price < spy_pdc_p
                and qqq_price < qqq_pdc_p)

    if long_ok:
        verdict = "LONGS enabled"
    elif short_ok:
        verdict = "SHORTS enabled"
    else:
        verdict = "NO NEW TRADES"

    now_ct = now_et.astimezone(tg.CDT)
    hdr_time = now_ct.strftime("%H:%M CT")

    lines = [
        "\U0001f3af PROXIMITY \u2014 %s" % hdr_time,
        SEP,
    ]

    # Index rows: "SPY $707.67 \u2705 vs $708.78"
    def _idx_row(tag, px, av, icon):
        if not (px > 0 and av > 0):
            return "%s  --" % tag
        return "%s $%.2f %s vs $%.2f" % (tag, px, icon, av)

    lines.append(_idx_row("SPY", spy_price, spy_pdc_p, spy_icon))
    lines.append(_idx_row("QQQ", qqq_price, qqq_pdc_p, qqq_icon))
    lines.append("Gate: %s" % verdict)
    lines.append(SEP)

    # --- Per-ticker rows ---
    # Build one snapshot per ticker: price, gap_long (px - OR_High),
    # gap_short (px - OR_Low), polarity vs PDC, open-position marker.
    rows = []  # list of dicts
    for t in tg.TRADE_TICKERS:
        orh = tg.or_high.get(t)
        orl = tg.or_low.get(t)
        pdc_val = tg.pdc.get(t)
        bars = tg.fetch_1min_bars(t)
        px = bars["current_price"] if bars else 0.0
        # Open-position marker: long takes precedence if somehow both
        # (shouldn't happen, but defensive).
        has_long = t in longs_dict
        has_short = t in shorts_dict
        if has_long:
            open_mark = "\U0001f7e2"  # green circle
        elif has_short:
            open_mark = "\U0001f534"  # red circle
        else:
            open_mark = ""
        if not (px > 0):
            rows.append({"t": t, "px": 0.0, "orh": orh, "orl": orl,
                         "pdc": pdc_val, "gl": None, "gs": None,
                         "pol": None, "mark": open_mark})
            continue
        gl = (px - orh) if (orh is not None) else None
        gs = (px - orl) if (orl is not None) else None
        pol = None
        if pdc_val is not None:
            pol = 1 if px > pdc_val else (-1 if px < pdc_val else 0)
        rows.append({"t": t, "px": px, "orh": orh, "orl": orl,
                     "pdc": pdc_val, "gl": gl, "gs": gs, "pol": pol,
                     "mark": open_mark})

    # ---- LONGS table: sorted by distance to OR High ----
    # Already above OR High (gl >= 0) first (closest to / past trigger),
    # then the rest ascending by |gl|. Unknowns go last.
    def _long_key(r):
        gl = r["gl"]
        if gl is None:
            return (2, 0.0)
        if gl >= 0:
            # Above trigger: rank by how far above (closer to trigger first)
            return (0, gl)
        return (1, -gl)  # below trigger: ascending gap

    longs_sorted = sorted(rows, key=_long_key)
    lines.append("LONGS \u2014 gap to OR High")
    for r in longs_sorted:
        t = r["t"]
        gl = r["gl"]
        orh = r["orh"]
        px = r["px"]
        om = r["mark"]
        # Open-marker replaces the 2-space indent when present (emoji
        # occupies ~2 monospace cells). Falls back to "  " otherwise so
        # tickers align cleanly.
        lead = om if om else "  "
        if gl is None or orh is None or px <= 0:
            lines.append("%s%-4s  --" % (lead, t))
            continue
        pct = (gl / orh) * 100.0 if orh else 0.0
        trig = "\u2705 " if gl >= 0 else "  "
        sign = "+" if gl >= 0 else "-"
        lines.append("%s%-4s %s%s$%.2f (%s%.2f%%)"
                     % (lead, t, trig, sign, abs(gl), sign, abs(pct)))
    lines.append(SEP)

    # ---- SHORTS table: sorted ascending by gap to OR Low ----
    # Most-negative first = already below OR Low (short trigger hit or past).
    def _short_key(r):
        gs = r["gs"]
        if gs is None:
            return (1, 0.0)
        return (0, gs)  # ascending: most negative first

    shorts_sorted = sorted(rows, key=_short_key)
    lines.append("SHORTS \u2014 gap to OR Low")
    for r in shorts_sorted:
        t = r["t"]
        gs = r["gs"]
        orl = r["orl"]
        px = r["px"]
        om = r["mark"]
        lead = om if om else "  "
        if gs is None or orl is None or px <= 0:
            lines.append("%s%-4s  --" % (lead, t))
            continue
        pct = (gs / orl) * 100.0 if orl else 0.0
        trig = "\u2705 " if gs <= 0 else "  "
        sign = "+" if gs >= 0 else "-"
        lines.append("%s%-4s %s%s$%.2f (%s%.2f%%)"
                     % (lead, t, trig, sign, abs(gs), sign, abs(pct)))
    lines.append(SEP)

    # ---- Prices & Polarity vs PDC (compact) ----
    # One cell = "<mark or 2sp><TICKER> $PRICE <arrow>" e.g.
    # "  AAPL $234.56 \u2191" or "\U0001f7e2NVDA $198.00 \u2193". Two
    # cells per row fit within 34ch mobile limit in the common case.
    # If a pair would exceed the budget (e.g. a 4-digit price on one
    # side and an emoji lead on the other), render that pair as two
    # separate rows instead of wrapping.
    lines.append("Prices & Polarity vs PDC")

    def _price_cell(r):
        pol = r["pol"]
        px = r["px"]
        om = r["mark"]
        lead = om if om else "  "
        if pol is None:
            arrow = "?"
        elif pol > 0:
            arrow = "\u2191"
        elif pol < 0:
            arrow = "\u2193"
        else:
            arrow = "="
        if px > 0:
            return "%s%-4s $%.2f %s" % (lead, r["t"], px, arrow)
        return "%s%-4s  --    %s" % (lead, r["t"], arrow)

    def _cell_width(cell):
        # Emoji in lead counts as 2 cells on mobile but 1 codepoint.
        w = len(cell)
        if cell.startswith(("\U0001f7e2", "\U0001f534")):
            w += 1
        return w

    chunk = []
    for r in rows:
        chunk.append(_price_cell(r))
        if len(chunk) == 2:
            combined = "  ".join(chunk)
            # 34 ch mobile budget; fall back to 1-per-row if over.
            if _cell_width(chunk[0]) + 2 + _cell_width(chunk[1]) <= 34:
                lines.append(combined)
            else:
                lines.append(chunk[0])
                lines.append(chunk[1])
            chunk = []
    if chunk:
        lines.append(chunk[0])

    # Legend if any open markers present
    any_long = any(r["mark"] == "\U0001f7e2" for r in rows)
    any_short = any(r["mark"] == "\U0001f534" for r in rows)
    if any_long or any_short:
        legend_bits = []
        if any_long:
            legend_bits.append("\U0001f7e2 long open")
        if any_short:
            legend_bits.append("\U0001f534 short open")
        lines.append(SEP)
        lines.append("  " + "  ".join(legend_bits))

    return "\n".join(lines), None


def _proximity_keyboard():
    """Inline keyboard for /proximity: Refresh + Menu."""
    tg = _tg()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f504 Refresh",
                              callback_data="proximity_refresh")],
        [InlineKeyboardButton("\U0001f3e0 Menu", callback_data="open_menu")],
    ])


def _orb_sync():
    """Build ORB text (blocking I/O \u2014 run in executor). Returns text or None."""
    tg = _tg()
    SEP = "\u2500" * 34
    now_et = tg._now_et()
    today = now_et.strftime("%Y-%m-%d")

    if tg.or_collected_date != today:
        return None

    lines = [
        "\U0001f4d0 TODAY'S OR LEVELS \u2014 %s" % today,
        SEP,
    ]

    for t in tg.TRADE_TICKERS:
        orh = tg.or_high.get(t)
        orl = tg.or_low.get(t)
        pdc_val = tg.pdc.get(t)
        if orh is None:
            lines.append("%s   --" % t)
            continue
        orl_str = "%.2f" % orl if orl is not None else "--"
        pdc_str = "%.2f" % pdc_val if pdc_val is not None else "--"
        lines.append(
            "%s   High $%.2f  Low $%s  PDC $%s"
            % (t, orh, orl_str, pdc_str)
        )

    lines.append(SEP)

    # SPY/QQQ vs PDC (v3.4.34: swapped from AVWAP)
    spy_bars = tg.fetch_1min_bars("SPY")
    qqq_bars = tg.fetch_1min_bars("QQQ")
    spy_price = spy_bars["current_price"] if spy_bars else 0
    qqq_price = qqq_bars["current_price"] if qqq_bars else 0
    spy_pdc_u = tg.pdc.get("SPY") or 0
    qqq_pdc_u = tg.pdc.get("QQQ") or 0
    spy_ok = spy_price > spy_pdc_u if spy_pdc_u > 0 else False
    qqq_ok = qqq_price > qqq_pdc_u if qqq_pdc_u > 0 else False
    spy_icon = "\u2705" if spy_ok else "\u274c"
    qqq_icon = "\u2705" if qqq_ok else "\u274c"

    spy_pdc_fmt = "%.2f" % spy_pdc_u if spy_pdc_u > 0 else "n/a"
    qqq_pdc_fmt = "%.2f" % qqq_pdc_u if qqq_pdc_u > 0 else "n/a"
    lines.append("SPY PDC: $%s  %s" % (spy_pdc_fmt, spy_icon))
    lines.append("QQQ PDC: $%s  %s" % (qqq_pdc_fmt, qqq_icon))

    # Entries today
    entry_parts = []
    for t in tg.TRADE_TICKERS:
        cnt = tg.daily_entry_count.get(t, 0)
        if cnt > 0:
            entry_parts.append("%sx%d" % (t, cnt))
    if entry_parts:
        entries_str = " ".join(entry_parts)
        lines.append("Entries today: %s" % entries_str)

    return "\n".join(lines)


def _fetch_or_for_ticker(ticker):
    """Try Yahoo then FMP to recover OR data for a single ticker. Returns dict or None."""
    tg = _tg()
    now_et = tg._now_et()
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    or_end = now_et.replace(hour=9, minute=35, second=0, microsecond=0)
    open_ts = int(market_open.timestamp())
    end_ts = int(or_end.timestamp())

    # Try Yahoo 1-min bars first
    try:
        bars = tg.fetch_1min_bars(ticker)
        if bars:
            max_high = None
            min_low = None
            for i, ts in enumerate(bars["timestamps"]):
                if open_ts <= ts < end_ts:
                    h = bars["highs"][i]
                    if h is None:
                        h = bars["closes"][i]
                    if h is not None:
                        if max_high is None or h > max_high:
                            max_high = h
                    lo = bars["lows"][i]
                    if lo is None:
                        lo = bars["closes"][i]
                    if lo is not None:
                        if min_low is None or lo < min_low:
                            min_low = lo
            if max_high is not None:
                tg.or_high[ticker] = max_high
                if min_low is not None:
                    tg.or_low[ticker] = min_low
                if bars.get("pdc") and bars["pdc"] > 0:
                    tg.pdc[ticker] = bars["pdc"]
                return {"high": max_high, "low": min_low if min_low else 0, "src": "Yahoo"}
    except Exception as e:
        logger.warning("or_now Yahoo failed for %s: %s", ticker, e)

    # FMP fallback
    try:
        fmp = tg.get_fmp_quote(ticker)
        if fmp and fmp.get("dayHigh") and fmp.get("dayLow"):
            tg.or_high[ticker] = fmp["dayHigh"]
            tg.or_low[ticker] = fmp["dayLow"]
            if fmp.get("previousClose") and fmp["previousClose"] > 0:
                tg.pdc[ticker] = fmp["previousClose"]
            return {"high": fmp["dayHigh"], "low": fmp["dayLow"], "src": "FMP"}
    except Exception as e:
        logger.warning("or_now FMP failed for %s: %s", ticker, e)

    return None


def _or_now_sync():
    """Re-collect missing OR data (blocking I/O \u2014 run in executor). Returns text or None."""
    tg = _tg()
    missing = [t for t in tg.TICKERS if t not in tg.or_high]
    if not missing:
        return None

    results = []
    recovered = 0
    still_fail = 0

    for ticker in missing:
        result = _fetch_or_for_ticker(ticker)
        if result is not None:
            recovered += 1
            results.append(
                "%s: \u2705 high=%.2f low=%.2f (%s)"
                % (ticker, result["high"], result["low"], result["src"])
            )
            logger.info("or_now recovered %s: high=%.2f low=%.2f (%s)",
                        ticker, result["high"], result["low"], result["src"])
        else:
            still_fail += 1
            results.append("%s: \u274c still missing" % ticker)
            logger.warning("or_now: %s still missing after Yahoo + FMP", ticker)

    if recovered > 0:
        tg.save_paper_state()

    SEP = "\u2500" * 34
    lines = ["\U0001f504 OR Recovery Complete", SEP]
    lines.extend(results)
    lines.append(SEP)
    lines.append("%d recovered | %d still missing" % (recovered, still_fail))
    return "\n".join(lines)


def _fmt_tickers_list() -> str:
    """Render the current ticker universe in a 34-char-safe table.
    Pinned tickers are flagged with an asterisk. Split into rows of
    5 symbols (≈ 30 chars at worst) so every line stays within the
    Telegram mobile code-block width.
    """
    tg = _tg()
    n_total = len(tg.TICKERS)
    n_trade = len(tg.TRADE_TICKERS)
    # Build rows of up to 5 symbols each \u2014 SPY and QQQ get a trailing
    # '*' to show they're pinned, so worst case per row is 5*(5+1)+4=34.
    def _tag(t):
        return t + "*" if t in tg.TICKERS_PINNED else t
    rows, row = [], []
    for t in tg.TICKERS:
        row.append(_tag(t))
        if len(row) == 5:
            rows.append(" ".join(row))
            row = []
    if row:
        rows.append(" ".join(row))
    body = "\n".join(rows) if rows else "(empty)"
    return (
        "\U0001f4cb Tracked Tickers\n"
        "%s\n%s\n%s\n"
        "%d total  \u00b7  %d tradable\n"
        "* = pinned (regime anchor)"
    ) % ("\u2500" * 26, body, "\u2500" * 26, n_total, n_trade)


def _fmt_add_reply(res: dict) -> str:
    """Format the reply for /ticker add. 34-char-safe."""
    tg = _tg()
    t = res.get("ticker", "?")
    if not res.get("ok"):
        return "\u274c Can't add %s\n%s" % (t, res.get("reason", "unknown"))
    if not res.get("added"):
        return "\u2139\ufe0f %s already tracked" % t
    metrics = res.get("metrics") or {}
    pdc_ok = metrics.get("pdc")
    pdc_src = metrics.get("pdc_src", "none")
    or_ok = metrics.get("or")
    or_pending = metrics.get("or_pending")
    rsi_ok = metrics.get("rsi")
    rsi_val = metrics.get("rsi_val")
    bars_ok = metrics.get("bars")
    pdc_val = tg.pdc.get(t)
    orh_val = tg.or_high.get(t)
    orl_val = tg.or_low.get(t)

    # Each metric gets one 34-char-safe status line.
    m_lines = []

    # Bars liveness probe \u2014 the foundation everything else depends on.
    m_lines.append(
        "Bars:  " + ("\u2705 reachable" if bars_ok
                     else "\u26a0 unreachable"))

    # PDC with source tag so the user knows which provider answered.
    if pdc_ok and pdc_val is not None:
        src_tag = " (%s)" % pdc_src if pdc_src in ("fmp", "bars") else ""
        m_lines.append("PDC:   $%.2f%s" % (pdc_val, src_tag))
    else:
        m_lines.append("PDC:   \u2014 (pending)")

    # OR high – low, or an explicit pending / missing reason.
    if or_ok and orh_val is not None and orl_val is not None:
        m_lines.append("OR:    $%.2f \u2013 $%.2f" % (orl_val, orh_val))
    elif or_pending:
        m_lines.append("OR:    pending 09:35 ET")
    else:
        m_lines.append("OR:    \u2014 (retry /or_now)")

    # RSI warm-up \u2014 proves bar history is deep enough.
    if rsi_ok and rsi_val is not None:
        m_lines.append("RSI:   %.1f (warm)" % rsi_val)
    else:
        m_lines.append("RSI:   \u2014 (warms on scan)")

    errs = [e for e in (metrics.get("errors") or []) if e]
    tail = ""
    if errs:
        # Truncate per-line to stay within the 34-char budget.
        tail = "\nnote: " + errs[0][:26]
    return (
        "\u2705 Added %s\n"
        "%s\n"
        "%s\n"
        "%s\n"
        "Next scan will trade it.%s"
    ) % (t, "\u2500" * 26, "\n".join(m_lines), "\u2500" * 26, tail)


def _fmt_remove_reply(res: dict) -> str:
    """Format the reply for /ticker remove. 34-char-safe."""
    tg = _tg()
    t = res.get("ticker", "?")
    if not res.get("ok"):
        return "\u274c Can't remove %s\n%s" % (t, res.get("reason", "unknown"))
    if not res.get("removed"):
        return "\u2139\ufe0f %s wasn't tracked" % t
    tail = ""
    if res.get("had_open"):
        tail = (
            "\nOpen position stays open\n"
            "and manages until close."
        )
    return (
        "\u2705 Removed %s\n"
        "%s\n"
        "No new entries on %s.%s"
    ) % (t, "\u2500" * 26, t, tail)


