"""Main-bot Telegram command handlers.

Extracted from trade_genius.py in v4.5.0 for maintainability.
Pure code motion — no behavior change.
"""

from __future__ import annotations

# v4.5.4 — prod runs `python trade_genius.py`, so trade_genius is registered
# in sys.modules as `__main__`, NOT as `trade_genius`. Without the alias
# below, `from trade_genius import (...)` would re-execute trade_genius.py
# from disk under a second module name, which re-enters run_telegram_bot()
# while this module is still partially initialized — AttributeError on
# cmd_help. Aliasing __main__ as `trade_genius` makes both names point at
# the same already-loaded module object.
import sys as _sys

if "trade_genius" not in _sys.modules and "__main__" in _sys.modules:
    _main = _sys.modules["__main__"]
    if getattr(_main, "BOT_NAME", None) == "TradeGenius":
        _sys.modules["trade_genius"] = _main


def _tg_module():
    """Return the live trade_genius module, whether it's running as __main__
    (production) or imported as 'trade_genius' (smoke tests, REPL)."""
    return _sys.modules.get("trade_genius") or _sys.modules.get("__main__")


from trade_genius import (
    BOT_NAME,
    BOT_VERSION,
    CDT,
    CLAMP_MAX_ENTRIES,
    CLAMP_MIN_SCORE_DELTA,
    CLAMP_SHARES,
    CLAMP_TRAIL_PCT,
    ChatAction,
    ContextTypes,
    DAILY_LOSS_LIMIT,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MAIN_RELEASE_NOTE,
    MODE_PROFILES,
    PAPER_STARTING_CAPITAL,
    Path,
    RESET_CONFIRM_WINDOW_SEC,
    TICKERS,
    TRADEGENIUS_OWNER_IDS,
    TelegramBadRequest,
    Update,
    _TICKER_USAGE,
    _build_menu_keyboard,
    _build_test_progress,
    _chart_dayreport,
    _chart_portfolio_pie,
    _current_breadth,
    _current_breadth_detail,
    _current_mode,
    _current_mode_pnl,
    _current_mode_reason,
    _current_mode_ts,
    _current_rsi_detail,
    _current_rsi_per_ticker,
    _current_rsi_regime,
    _current_ticker_extremes,
    _current_ticker_red,
    _dashboard_sync,
    _do_reset_paper,
    _fetch_or_for_ticker,
    _fmt_add_reply,
    _fmt_remove_reply,
    _fmt_tickers_list,
    _format_dayreport_section,
    _log_sync,
    _menu_button,
    _near_miss_log,
    _now_et,
    _open_positions_as_pseudo_trades,
    _orb_sync,
    _parse_date_arg,
    _perf_compute,
    _price_sync,
    _proximity_keyboard,
    _proximity_sync,
    _refresh_market_mode,
    _replay_sync,
    _reply_in_chunks,
    _reset_buttons,
    _status_text_sync,
    _test_fmp,
    _test_positions,
    _test_scanner,
    _test_state,
    _trade_log_last_error,
    add_ticker,
    asyncio,
    datetime,
    logger,
    or_high,
    os,
    paper_cash,
    positions,
    remove_ticker,
    retighten_all_stops,
    save_paper_state,
    short_positions,
    short_trade_history,
    time,
    timedelta,
    trade_history,
    trade_log_read_tail,
    urllib,
)


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /test command \u2014 run system health test, render once at end.

    v5.1.5: previously this function called prog.edit_text() after every
    one of the 4 _test_* steps plus a final 5th edit. With 5 edits within
    ~1 second, Telegram's per-chat editMessageText rate limit fired the
    httpx ~5 s read-timeout on the last edit, surfacing to the user as a
    cosmetic "\u26a0\ufe0f Command failed: Timed out" chip even though every
    underlying _test_* step had completed cleanly. The fix is a single
    edit at the end of the loop with a reply_text fallback if that final
    edit still races the limit.
    """
    t0 = asyncio.get_event_loop().time()
    await update.message.reply_chat_action(ChatAction.TYPING)
    results: dict = {}
    prog = await update.message.reply_text(_build_test_progress(results))

    loop = asyncio.get_event_loop()

    results["fmp"] = await loop.run_in_executor(None, _test_fmp)
    results["state"] = await loop.run_in_executor(None, _test_state)
    results["pos"] = _test_positions()
    results["scanner"] = _test_scanner()

    # Single final edit. TelegramBadRequest covers "message is not
    # modified" no-ops; any other failure (httpx ReadTimeout from the
    # per-chat edit rate limit, transient network) falls back to a
    # fresh reply_text so the user always sees the result.
    try:
        await prog.edit_text(_build_test_progress(results), reply_markup=_menu_button())
    except TelegramBadRequest as e:
        logger.debug("cmd_test: edit_text final: %s", e)
    except Exception as e:
        logger.info("cmd_test: final edit_text fell back to reply_text: %s", e)
        try:
            await update.message.reply_text(
                _build_test_progress(results),
                reply_markup=_menu_button(),
            )
        except Exception as e2:
            logger.warning("cmd_test: reply_text fallback also failed: %s", e2)
    logger.info("CMD test completed in %.2fs", asyncio.get_event_loop().time() - t0)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show categorized command list.

    Body is wrapped in a Markdown code block so Telegram renders it in
    monospace. This makes space-padded columns actually align and keeps
    each line short enough to avoid wrapping on phone widths.
    """
    # Keep every line <= 34 chars including the leading 2-space indent so
    # the content fits Telegram's mobile code-block width without wrapping.
    body = (
        "\U0001f4d6 Commands\n"
        "```\n"
        "Portfolio\n"
        "  /dashboard   Full snapshot\n"
        "  /status      Positions + P&L\n"
        "  /perf [date] Performance stats\n"
        "\n"
        "Market Data\n"
        "  /price TICK  Live quote\n"
        "  /orb         Today's OR levels\n"
        "  /orb recover Recollect missing\n"
        "  /proximity   Gap to breakout\n"
        "  /mode        Market regime\n"
        "\n"
        "Reports\n"
        "  /dayreport [date]  Trades + P&L\n"
        "  /log [date]        Trade log\n"
        "  /replay [date]     Timeline\n"
        "\n"
        "System\n"
        "  /monitoring  Pause/resume scan\n"
        "  /test        Health check\n"
        "  /menu        Quick tap menu\n"
        "\n"
        "Reference\n"
        "  /strategy    Strategy summary\n"
        "  /algo        Algorithm PDF\n"
        "  /version     Release notes\n"
        "\n"
        "Admin\n"
        "  /reset       Reset portfolio\n"
        "  /ticker list       Show list\n"
        "  /ticker add SYM    Track\n"
        "  /ticker remove SYM Drop\n"
        "\n"
        "Tip: /menu for tap buttons\n"
        "```"
    )
    await update.message.reply_text(
        body,
        parse_mode="Markdown",
        reply_markup=_menu_button(),
    )


async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Full market snapshot: portfolio, index filters, OR levels."""
    t0 = asyncio.get_event_loop().time()
    await update.message.reply_chat_action(ChatAction.TYPING)
    prog = await update.message.reply_text("\u23f3 Loading dashboard (~3s)...")
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, _dashboard_sync)
    try:
        if len(text) > 3800:
            await prog.delete()
            await _reply_in_chunks(update.message, text, reply_markup=_menu_button())
        else:
            await prog.edit_text(text, reply_markup=_menu_button())
    except Exception:
        pass
    logger.info("CMD dashboard completed in %.2fs", asyncio.get_event_loop().time() - t0)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show open positions with live prices, unrealized P&L, and TP summary."""
    t0 = asyncio.get_event_loop().time()
    await update.message.reply_chat_action(ChatAction.TYPING)
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, _status_text_sync)

    refresh_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("\U0001f504 Refresh", callback_data="positions_refresh")]]
    )
    await update.message.reply_text(text, reply_markup=refresh_kb)

    # Portfolio pie chart (run in thread to avoid blocking event loop)
    sent_photo = False
    if MATPLOTLIB_AVAILABLE and (positions or short_positions):
        buf = await loop.run_in_executor(
            None, _chart_portfolio_pie, positions, short_positions, paper_cash
        )
        if buf:
            await update.message.reply_photo(
                photo=buf, caption="Portfolio Allocation", reply_markup=_menu_button()
            )
            sent_photo = True

    if not sent_photo:
        await update.effective_message.reply_text("\u2500", reply_markup=_menu_button())
    logger.info("CMD status completed in %.2fs", asyncio.get_event_loop().time() - t0)


async def cmd_dayreport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show completed trades with P&L summary (optional date)."""
    t0 = asyncio.get_event_loop().time()
    await update.message.reply_chat_action(ChatAction.TYPING)
    target_date = _parse_date_arg(context.args)
    target_str = target_date.strftime("%Y-%m-%d")
    day_label = target_date.strftime("%a %b %d, %Y")
    header = "\U0001f4ca Day Report \u2014 %s" % day_label

    # Fix B: Route based on which bot
    today_str = _now_et().strftime("%Y-%m-%d")

    # Paper portfolio
    paper_long = [t for t in trade_history if t.get("date", "") == target_str]
    paper_short = [t for t in short_trade_history if t.get("date", "") == target_str]
    # v3.3.1: include currently-open positions as pseudo-trades when
    # target date matches today. Past-date reports stay history-only.
    if target_str == today_str:
        paper_long_open, paper_short_open = _open_positions_as_pseudo_trades(
            target_date=target_str,
        )
    else:
        paper_long_open, paper_short_open = [], []
    all_paper = paper_long + paper_short + paper_long_open + paper_short_open

    if not all_paper:
        await update.effective_message.reply_text(
            "No trades on {date}.".format(date=target_str), reply_markup=_menu_button()
        )
        logger.info(
            "CMD dayreport completed in %.2fs (no trades)", asyncio.get_event_loop().time() - t0
        )
        return

    paper_body = _format_dayreport_section(all_paper, header, "Paper")
    await _reply_in_chunks(update.message, paper_body)

    # Chart: Trade P&L bar chart
    if MATPLOTLIB_AVAILABLE:
        chart_msg = await update.message.reply_text("\U0001f4ca Generating chart...")
        loop = asyncio.get_event_loop()
        buf = await loop.run_in_executor(None, _chart_dayreport, all_paper, day_label)
        if buf:
            try:
                await chart_msg.delete()
            except Exception:
                pass
            await update.message.reply_photo(
                photo=buf, caption="Trade P&L \u2014 %s" % day_label, reply_markup=_menu_button()
            )
        else:
            try:
                await chart_msg.edit_text(
                    "\U0001f4ca Chart unavailable (no trades or matplotlib missing)",
                    reply_markup=_menu_button(),
                )
            except Exception:
                pass
    else:
        await update.effective_message.reply_text("\u2500", reply_markup=_menu_button())
    logger.info("CMD dayreport completed in %.2fs", asyncio.get_event_loop().time() - t0)


async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show completed trades (entries and exits) chronologically (optional date)."""
    t0 = asyncio.get_event_loop().time()
    await update.message.reply_chat_action(ChatAction.TYPING)
    prog = await update.message.reply_text("\u23f3 Loading log...")
    target_date = _parse_date_arg(context.args)
    target_str = target_date.strftime("%Y-%m-%d")
    day_label = target_date.strftime("%a %b %d, %Y")

    loop = asyncio.get_event_loop()
    try:
        text = await asyncio.wait_for(
            loop.run_in_executor(None, _log_sync, target_str, day_label),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        logger.warning("cmd_log: executor timed out after 15s")
        try:
            await prog.edit_text(
                "\u26a0\ufe0f Trade log timed out. Try again.", reply_markup=_menu_button()
            )
        except Exception:
            pass
        return

    if text is None:
        try:
            await prog.edit_text("No trades on %s." % day_label, reply_markup=_menu_button())
        except Exception:
            pass
        logger.info("CMD log completed in %.2fs (no trades)", asyncio.get_event_loop().time() - t0)
        return

    try:
        await prog.delete()
    except Exception:
        pass
    await _reply_in_chunks(update.message, text, reply_markup=_menu_button())
    logger.info("CMD log completed in %.2fs", asyncio.get_event_loop().time() - t0)


async def cmd_replay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Timeline replay of trades with running cumulative P&L (optional date)."""
    t0 = asyncio.get_event_loop().time()
    await update.message.reply_chat_action(ChatAction.TYPING)
    target_date = _parse_date_arg(context.args)
    target_str = target_date.strftime("%Y-%m-%d")
    day_label = target_date.strftime("%a %b %d, %Y")

    loop = asyncio.get_event_loop()
    try:
        text = await asyncio.wait_for(
            loop.run_in_executor(None, _replay_sync, target_str, day_label),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        logger.warning("cmd_replay: executor timed out after 15s")
        await update.message.reply_text(
            "\u26a0\ufe0f Replay timed out. Try again.", reply_markup=_menu_button()
        )
        return

    if text is None:
        await update.message.reply_text("No trades on %s." % day_label, reply_markup=_menu_button())
        logger.info(
            "CMD replay completed in %.2fs (no trades)", asyncio.get_event_loop().time() - t0
        )
        return

    await _reply_in_chunks(update.message, text, reply_markup=_menu_button())
    logger.info("CMD replay completed in %.2fs", asyncio.get_event_loop().time() - t0)


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show version info."""
    await update.message.reply_text(
        "%s v%s\n%s" % (BOT_NAME, BOT_VERSION, MAIN_RELEASE_NOTE), reply_markup=_menu_button()
    )


async def cmd_near_misses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent near-miss entries — breakouts that cleared price
    but were declined by the volume gate. Read-only diagnostic;
    fail-closed behavior is unchanged.
    """
    log = list(_near_miss_log)
    SEP = "\u2500" * 34
    if not log:
        await update.message.reply_text(
            "\U0001f50d Near-misses\n%s\nNone recorded yet today.\n"
            "A near-miss is a 1m close past OR\n"
            "that was declined by the volume gate." % SEP,
            reply_markup=_menu_button(),
        )
        return
    lines = ["\U0001f50d Near-misses (last %d)" % len(log), SEP]
    for row in log[:10]:
        # Each row: "09:47 META LONG LOW_VOL 48%"
        ts = row.get("ts", "")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            hhmm = dt.astimezone(CDT).strftime("%H:%M")
        except Exception:
            hhmm = "--:--"
        tkr = row.get("ticker", "?")
        side = row.get("side", "?")
        reason = row.get("reason", "?")
        vp = row.get("vol_pct")
        vp_str = ("%d%%" % int(vp)) if isinstance(vp, (int, float)) else "n/a"
        close_v = row.get("close")
        level_v = row.get("level")
        head = "%s %s %s %s" % (hhmm, tkr, side, reason)
        if close_v is not None and level_v is not None:
            lines.append(head)
            lines.append("  close $%.2f vs $%.2f  vol %s" % (close_v, level_v, vp_str))
        else:
            lines.append("%s  vol %s" % (head, vp_str))
    lines.append(SEP)
    lines.append("Diagnostic only \u2014 no entries made.")
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=_menu_button(),
    )


async def cmd_retighten(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually run the 0.75% retro-cap across every open position.

    The cap already runs automatically on startup and every manage
    cycle, so this is mostly a transparency tool — it shows what the
    cap would do right now. force_exit=True is ON: a position whose
    retightened stop is already breached will be exited immediately,
    same as the automatic pass.
    """
    SEP = "\u2500" * 34
    try:
        result = retighten_all_stops(
            force_exit=True,
            fetch_prices=True,
        )
    except Exception as e:
        logger.error("cmd_retighten failed: %s", e, exc_info=True)
        await update.message.reply_text(
            "\u26a0\ufe0f retighten failed: %s" % str(e)[:200],
            reply_markup=_menu_button(),
        )
        return

    lines = ["\U0001f527 Stop retighten", SEP]
    details = result.get("details", [])
    if not details:
        lines.append("No open positions.")
    else:
        # Audit: the tightened / ratcheted / ratcheted_trail branches
        # previously fed `old`/`new` straight into "%.2f" which raises
        # TypeError when retighten_all_stops returns None for either
        # (happens when the caller skipped the price-fetch step or a
        # mid-cycle state change blanked the value). Coerce with
        # `or 0.0` so the command returns a readable message instead
        # of dying with an unhandled handler exception.
        def _fn(v):
            return float(v) if v is not None else 0.0

        any_change = False
        for d in details:
            tkr = d.get("ticker", "?")
            side = d.get("side", "?")
            port = d.get("portfolio", "?")
            status = d.get("status", "?")
            old = d.get("old_stop")
            new = d.get("new_stop")
            if status == "tightened":
                lines.append("%s %s [%s] cap" % (tkr, side, port))
                lines.append("  stop $%.2f \u2192 $%.2f" % (_fn(old), _fn(new)))
                any_change = True
            elif status == "ratcheted":
                lines.append("%s %s [%s] breakeven" % (tkr, side, port))
                lines.append("  stop $%.2f \u2192 $%.2f" % (_fn(old), _fn(new)))
                any_change = True
            elif status == "ratcheted_trail":
                lines.append("%s %s [%s] trail\u2192entry" % (tkr, side, port))
                lines.append("  trail $%.2f \u2192 $%.2f" % (_fn(old), _fn(new)))
                any_change = True
            elif status == "exit":
                lines.append("%s %s [%s] EXITED" % (tkr, side, port))
                lines.append("  breached at cap $%.2f" % _fn(new))
                any_change = True
            elif status == "no_op":
                lines.append("%s %s [%s] trail armed" % (tkr, side, port))
            elif status == "already_tight":
                lines.append("%s %s [%s] already tight" % (tkr, side, port))
                if old is not None:
                    lines.append("  stop $%.2f" % _fn(old))
        if not any_change:
            lines.append("")
            lines.append("No changes \u2014 stops already optimal.")
    lines.append(SEP)
    lines.append(
        "Summary: %d cap, %d ratchet," % (result.get("tightened", 0), result.get("ratcheted", 0))
    )
    lines.append(
        "%d trail\u2192entry, %d exited,"
        % (result.get("ratcheted_trail", 0), result.get("exited", 0))
    )
    lines.append("%d no-op, %d tight" % (result.get("no_op", 0), result.get("already_tight", 0)))
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=_menu_button(),
    )


async def cmd_trade_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the last 10 rows from the persistent trade log.

    Reads the same append-only JSONL file that the dashboard
    /api/trade_log endpoint serves. Output is width-safe for
    Telegram mobile (≤34 chars per line). Errors are surfaced
    so Val can catch disk issues early.
    """
    SEP = "\u2500" * 34
    # v3.4.39: scope by originating bot so the Robinhood bot never shows paper rows.
    portfolio = "tp" if False else "paper"
    try:
        rows = trade_log_read_tail(limit=10, portfolio=portfolio)
    except Exception as e:
        logger.error("cmd_trade_log failed: %s", e, exc_info=True)
        await update.message.reply_text(
            "\u26a0\ufe0f trade_log failed: %s" % str(e)[:200],
            reply_markup=_menu_button(),
        )
        return

    scope = "Robinhood" if portfolio == "tp" else "Paper"
    lines = ["\U0001f4d2 Trade log \u2014 %s (last 10)" % scope, SEP]
    if not rows:
        lines.append("No trades logged yet.")
        if _trade_log_last_error:
            lines.append("err: %s" % str(_trade_log_last_error)[:28])
        lines.append(SEP)
        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=_menu_button(),
        )
        return

    # Summary first — wins/losses, total P&L, by-reason bucket.
    wins = sum(1 for r in rows if (r.get("pnl") or 0) > 0)
    losses = sum(1 for r in rows if (r.get("pnl") or 0) < 0)
    total = sum(float(r.get("pnl") or 0) for r in rows)
    by_reason = {}
    for r in rows:
        # Strip [5m]/[1h] suffixes so reasons bucket.
        reason = str(r.get("reason", "?")).split("[")[0]
        b = by_reason.setdefault(reason, [0, 0.0])
        b[0] += 1
        b[1] += float(r.get("pnl") or 0)

    lines.append("W%d L%d  P&L $%+.2f" % (wins, losses, total))
    lines.append(SEP)

    for r in rows:
        tkr = str(r.get("ticker", "?"))[:5]
        side = "L" if r.get("side") == "LONG" else "S"
        port = str(r.get("portfolio", "?"))[0].upper()
        pnl = float(r.get("pnl") or 0)
        reason = str(r.get("reason", "?")).split("[")[0][:10]
        date = str(r.get("date", ""))[-5:]  # MM-DD
        # Line 1: date ticker side[port]  +/-P&L
        lines.append(
            "%s %-5s %s[%s] $%+.2f"
            % (
                date,
                tkr,
                side,
                port,
                pnl,
            )
        )
        # Line 2: reason + entry→exit
        entry = r.get("entry_price")
        exit_ = r.get("exit_price")
        if entry is not None and exit_ is not None:
            lines.append(
                "  %s  $%.2f\u2192$%.2f"
                % (
                    reason,
                    float(entry),
                    float(exit_),
                )
            )
        else:
            lines.append("  %s" % reason)

    lines.append(SEP)
    lines.append("By reason:")
    for reason, (n, p) in sorted(by_reason.items(), key=lambda kv: -kv[1][1]):
        lines.append("  %-10s %d  $%+.2f" % (reason[:10], n, p))
    if _trade_log_last_error:
        lines.append(SEP)
        lines.append("err: %s" % str(_trade_log_last_error)[:28])

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=_menu_button(),
    )


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the current MarketMode classification and its profile.
    OBSERVATION ONLY in this version — no trading parameter reads from it yet.

    v4.0.0-alpha — also routes to executor bots:
      /mode val                     → show Val's current mode + account
      /mode val paper               → flip Val to paper
      /mode val live confirm        → flip Val to live (sanity-checked)

    v4.0.0-beta — same routing for /mode gene (second executor).
    """
    args = context.args if context and hasattr(context, "args") else []
    if args and args[0].lower() in ("val", "gene"):
        which = args[0].lower()
        # Use globals().get so a missing-keys boot (where val_executor /
        # gene_executor were never assigned at module scope) returns a
        # friendly reply instead of raising NameError.
        executor = globals().get(f"{which}_executor")
        label = "Val" if which == "val" else "Gene"
        if executor is None:
            await update.message.reply_text(f"{label} executor not enabled")
            return
        sub = args[1].lower() if len(args) > 1 else ""
        if not sub:
            client = executor._ensure_client()
            lines = [f"{label} mode: {executor.mode}"]
            if client is None:
                lines.append("  alpaca: (no client \u2014 keys missing?)")
            else:
                try:
                    acct = client.get_account()
                    lines.append(
                        f"  acct: {getattr(acct, 'account_number', '?')} "
                        f"status={getattr(acct, 'status', '?')}"
                    )
                    lines.append(f"  cash: {getattr(acct, 'cash', '?')}")
                except Exception as e:
                    lines.append(f"  alpaca error: {e}")
            await update.message.reply_text("\n".join(lines))
            return
        # Reject anything outside {"paper","live"} up front so the
        # handler doesn't die inside set_mode on unknown strings.
        if sub not in ("paper", "live"):
            await update.message.reply_text(
                f"\u274c {label}: unknown mode '{sub}' (expected paper|live)"
            )
            return
        confirm_token = args[2] if len(args) > 2 else None
        try:
            ok, msg = executor.set_mode(sub, confirm_token=confirm_token)
        except Exception as e:
            logger.exception("cmd_mode: executor.set_mode raised")
            await update.message.reply_text(f"\u274c {label}: set_mode error: {str(e)[:200]}")
            return
        marker = "\u2705" if ok else "\u274c"
        await update.message.reply_text(f"{marker} {label}: {msg}")
        return

    SEP = "\u2500" * 34
    # Refresh once on demand so manual checks outside scan cadence are fresh.
    try:
        _refresh_market_mode()
    except Exception:
        logger.exception("/mode: refresh failed")

    mode = _current_mode
    reason = _current_mode_reason
    pnl = _current_mode_pnl
    ts = _current_mode_ts
    profile = MODE_PROFILES.get(mode, {})

    ts_str = ts.strftime("%H:%M ET") if ts else "—"
    shorts = "ON" if profile.get("allow_shorts") else "OFF"
    trail_bps = int(round(profile.get("trail_pct", 0) * 10000))

    # Build compact per-ticker RSI preview (top 6 by value, highest first)
    if _current_rsi_per_ticker:
        sorted_rsis = sorted(_current_rsi_per_ticker.items(), key=lambda kv: kv[1], reverse=True)
        rsi_preview = " | ".join("%s %.0f" % (tk, r) for tk, r in sorted_rsis[:6])
    else:
        rsi_preview = "—"

    if _current_ticker_red:
        red_preview = ", ".join("%s $%+.0f" % (tk, p) for tk, p in _current_ticker_red[:5])
    else:
        red_preview = "none"

    if _current_ticker_extremes:
        ext_preview = ", ".join(
            "%s %.0f %s" % (tk, r, tag) for tk, r, tag in _current_ticker_extremes[:5]
        )
    else:
        ext_preview = "none"

    lines = [
        "\U0001f9ed MARKET MODE  %s" % ts_str,
        SEP,
        "Mode:       %s" % mode,
        "Reason:     %s" % reason,
        "Realized:   $%+.2f  (loss limit $%+.2f)" % (pnl, DAILY_LOSS_LIMIT),
        SEP,
        "Observers (advisory — not yet applied):",
        "  Breadth:  %s" % _current_breadth,
        "            %s" % (_current_breadth_detail or "—"),
        "  RSI:      %s" % _current_rsi_regime,
        "            %s" % (_current_rsi_detail or "—"),
        "  Per-tkr:  %s" % rsi_preview,
        "  Red:      %s" % red_preview,
        "  Extremes: %s" % ext_preview,
        SEP,
        "Profile (advisory — not yet applied):",
        "  trail_pct       %.3f%%  (%d bps)" % (profile.get("trail_pct", 0) * 100, trail_bps),
        "  max_entries     %d / ticker / day" % profile.get("max_entries", 0),
        "  shares          %d" % profile.get("shares", 0),
        "  min_score_delta +%.2f" % profile.get("min_score_delta", 0),
        "  allow_shorts    %s" % shorts,
        SEP,
        profile.get("note", ""),
        "",
        "Bounds: trail %.1f-%.1f%% | entries %d-%d | shares %d-%d | score +%.2f-+%.2f"
        % (
            CLAMP_TRAIL_PCT[0] * 100,
            CLAMP_TRAIL_PCT[1] * 100,
            CLAMP_MAX_ENTRIES[0],
            CLAMP_MAX_ENTRIES[1],
            CLAMP_SHARES[0],
            CLAMP_SHARES[1],
            CLAMP_MIN_SCORE_DELTA[0],
            CLAMP_MIN_SCORE_DELTA[1],
        ),
        "",
        "(v%s — observation only, no parameter is adaptive yet)" % BOT_VERSION,
    ]
    await update.message.reply_text("\n".join(lines), reply_markup=_menu_button())


async def cmd_algo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send algorithm summary + downloadable PDF reference."""
    SEP = "\u2500" * 34
    summary = (
        "\U0001f4d8 ALGORITHM REFERENCE v3.4.40\n"
        f"{SEP}\n"
        "Two independent strategies:\n\n"
        "\U0001f4c8 ORB LONG BREAKOUT\n"
        "  Entry: 1-min close > OR_High\n"
        "         + price > PDC (green stock)\n"
        "         + SPY & QQQ > PDC\n"
        "  Stop : OR_High \u2212 $0.90\n"
        "  Ladder (peak \u2192 stop):\n"
        "    +1% \u2192 peak \u2212 0.50%\n"
        "    +2% \u2192 peak \u2212 0.40%\n"
        "    +3% \u2192 peak \u2212 0.30%\n"
        "    +4% \u2192 peak \u2212 0.20%\n"
        "    +5%+ \u2192 peak \u2212 0.10%\n\n"
        "\U0001f9b7 WOUNDED BUFFALO SHORT\n"
        "  Entry: 1-min close < OR_Low\n"
        "         + price < PDC (red stock)\n"
        "         + SPY & QQQ < PDC\n"
        "  Stop : PDC + $0.90\n"
        "  Ladder (peak \u2192 stop):\n"
        "    +1% \u2192 peak + 0.50%\n"
        "    +2% \u2192 peak + 0.40%\n"
        "    +3% \u2192 peak + 0.30%\n"
        "    +4% \u2192 peak + 0.20%\n"
        "    +5%+ \u2192 peak + 0.10%\n\n"
        f"{SEP}\n"
        "Size : 10 shares (limit orders only)\n"
        "Max  : 5 entries per ticker/day (long + short combined)\n"
        "OR   : 8:30\u20138:35 CT (first 5 min)\n"
        "Scan : every 60s \u2192 8:35\u20142:55 CT\n"
        "EOD  : force-close all at 2:55 CT\n"
        f"{SEP}\n"
        "\U0001f6e1 INDEX REGIME (v5.9.0+)\n"
        "  Entry-side QQQ Regime Shield uses\n"
        "  a 5-min EMA compass (EMA3 vs EMA9)\n"
        "  at G1. Exit-side dual-PDC eject\n"
        "  retired in v5.9.1; HARD_EJECT_TIGER\n"
        "  is DI-only (v5.9.2). v5.9.3 cleaned\n"
        "  up the last residual labels.\n"
        f"{SEP}\n"
        "Full reference guide attached \u2193"
    )
    await update.message.reply_text(summary)

    # Send PDF — try local file first, fall back to GitHub raw download
    _ALGO_PDF_URL = (
        "https://raw.githubusercontent.com/valira3/stock-spike-monitor/main/trade_genius_algo.pdf"
    )
    pdf_path = Path("trade_genius_algo.pdf")
    tmp_path = None
    if not pdf_path.exists():
        logger.info("/algo: PDF not found locally — downloading from GitHub")
        try:
            import tempfile

            tmp_fd, tmp_name = tempfile.mkstemp(suffix=".pdf")
            os.close(tmp_fd)
            await asyncio.to_thread(urllib.request.urlretrieve, _ALGO_PDF_URL, tmp_name)
            pdf_path = Path(tmp_name)
            tmp_path = tmp_name
        except Exception as e:
            logger.warning("/algo: GitHub PDF download failed: %s", e)
            pdf_path = None
    if pdf_path and pdf_path.exists():
        try:
            with open(pdf_path, "rb") as pdf_file:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=pdf_file,
                    filename="TradeGenius_Algorithm_v%s.pdf" % BOT_VERSION,
                    caption="%s \u2014 Algorithm Reference Manual v%s" % (BOT_NAME, BOT_VERSION),
                )
        except Exception as e:
            logger.warning("Failed to send algo PDF: %s", e)
            await update.message.reply_text("(PDF unavailable \u2014 contact admin)")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
    else:
        await update.message.reply_text("(PDF unavailable \u2014 contact admin)")
    await update.effective_message.reply_text("\u2500", reply_markup=_menu_button())


async def cmd_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show compact strategy summary."""
    SEP = "\u2500" * 26
    text = (
        f"\U0001f4d8 Strategy v{BOT_VERSION}\n"
        f"{SEP}\n"
        "\U0001f4c8 LONG \u2014 ORB Breakout\n"
        "Entry after 8:45 CT (15-min buffer)\n"
        "Entry (all must be true):\n"
        "  \u2022 1m close > OR High\n"
        "  \u2022 Price > PDC\n"
        "  \u2022 SPY > PDC\n"
        "  \u2022 QQQ > PDC\n"
        "Stop: OR High \u2212 $0.90\n"
        "Ladder (peak \u2192 stop):\n"
        "  +1% \u2192 peak \u2212 0.50%\n"
        "  +2% \u2192 peak \u2212 0.40%\n"
        "  +3% \u2192 peak \u2212 0.30%\n"
        "  +4% \u2192 peak \u2212 0.20%\n"
        "  +5%+ \u2192 peak \u2212 0.10%\n"
        "Size: 10 shares \u00b7 limit order\n"
        "Max: 5 entries/ticker/day\n"
        "EOD: closes at 2:55 CT\n"
        "\n"
        "Eye of the Tiger exits:\n"
        "  \U0001f56f Red Candle\n"
        "     price < Open OR < PDC\n"
        "  HARD_EJECT_TIGER (DI<25)\n"
        "     non-Titan tickers only;\n"
        "     Titans use the FSM\n"
        f"{SEP}\n"
        "\U0001f4c9 SHORT \u2014 Wounded Buffalo\n"
        "Entry after 8:45 CT (15-min buffer)\n"
        "Entry (all must be true):\n"
        "  \u2022 1m close < OR Low\n"
        "  \u2022 Price < PDC\n"
        "  \u2022 SPY < PDC\n"
        "  \u2022 QQQ < PDC\n"
        "Stop: PDC + $0.90\n"
        "Ladder (peak \u2192 stop):\n"
        "  +1% \u2192 peak + 0.50%\n"
        "  +2% \u2192 peak + 0.40%\n"
        "  +3% \u2192 peak + 0.30%\n"
        "  +4% \u2192 peak + 0.20%\n"
        "  +5%+ \u2192 peak + 0.10%\n"
        "Size: 10 shares \u00b7 limit order\n"
        "Max: 5 entries/ticker/day\n"
        "EOD: closes at 2:55 CT\n"
        "\n"
        "Eye of the Tiger exits:\n"
        "  HARD_EJECT_TIGER (DI<25)\n"
        "     non-Titan tickers only\n"
        "  \U0001f504 Polarity Shift\n"
        "     price > PDC (1m close)\n"
        f"{SEP}\n"
        "\U0001f6e1 Regime Shield (v3.4.28)\n"
        "  Global eject requires BOTH\n"
        "  SPY and QQQ to cross PDC on\n"
        "  a finalized 1m close. One\n"
        "  anchor across entries and\n"
        "  ejects (v3.4.34: AVWAP gone)\n"
        f"{SEP}"
    )
    await _reply_in_chunks(update.message, text, reply_markup=_menu_button())


def _reset_authorized(query, context=None) -> tuple:
    """Gatekeeper for /reset callbacks.

    v4.4.0: chat-based fallback removed \u2014 owner user_id required
    regardless of chat context. Returns (allowed: bool, reason: str).
    Checks:
      1. Owner check \u2014 tapping user's id MUST be in TRADEGENIUS_OWNER_IDS.
         If user_id cannot be determined (channel post, edited message
         with no sender) the callback is denied.
      2. Freshness check \u2014 confirm callbacks carry ':<unix_ts>' suffix
         and must be within RESET_CONFIRM_WINDOW_SEC. Prevents stale
         replays.
    """
    data = query.data or ""
    try:
        user_id_str = str(query.from_user.id) if query.from_user else ""
    except Exception:
        user_id_str = ""

    # (1) Owner check \u2014 user_id in TRADEGENIUS_OWNER_IDS is the ONLY gate.
    if not user_id_str:
        return (False, "no user_id")
    is_owner_user = user_id_str in TRADEGENIUS_OWNER_IDS
    if not is_owner_user:
        return (False, "unauthorized user")

    # (2) Freshness check — confirm callbacks carry ':<unix_ts>' suffix.
    if "_confirm" in data and ":" in data:
        try:
            ts = int(data.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            return (False, "malformed timestamp")
        age = time.time() - ts
        if age < -5:
            return (False, "future-dated confirm")
        if age > RESET_CONFIRM_WINDOW_SEC:
            return (False, "expired confirm (%.0fs old)" % age)

    return (True, "")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/reset — show confirmation before resetting the paper portfolio.

    v3.5.0: paper-only. TP/Robinhood reset path removed.
    """
    await update.message.reply_text(
        "\u26a0\ufe0f Reset paper portfolio to $100,000?\nAll trade history will be cleared.\n(Confirm within 60s.)",
        reply_markup=_reset_buttons("paper"),
    )


async def reset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard taps for /reset confirmation.

    Confirm callbacks carry a ':<ts>' suffix. _reset_authorized() enforces
    owner user_id match and freshness (v4.4.0: chat-based fallback
    removed \u2014 owner user_id required regardless of chat context).
    """
    query = update.callback_query
    await query.answer()
    paper_fmt = format(PAPER_STARTING_CAPITAL, ",.0f")

    allowed, reason = _reset_authorized(query, context)
    if not allowed:
        # v4.4.0 surface user id + owner env var in the Telegram message
        # so the owner can diagnose auth mismatches without Railway logs.
        # CHAT_ID is no longer printed as an auth input \u2014 it's routing only.
        try:
            _user = query.from_user.id if query.from_user else "?"
        except Exception:
            _user = "?"
        logger.warning(
            "reset_callback blocked: data=%s chat_id=%s user_id=%s reason=%s",
            query.data,
            query.message.chat_id,
            _user,
            reason,
        )
        owner_users_fmt = ",".join(sorted(TRADEGENIUS_OWNER_IDS)) or "(unset)"
        diag = ("\u274c Reset blocked: %s.\nchat_id: %s\nuser_id: %s\nowner users: %s") % (
            reason,
            query.message.chat_id,
            _user,
            owner_users_fmt,
        )
        await query.edit_message_text(diag)
        return

    # Confirm variants carry ':<ts>' — strip before dispatching.
    action = query.data.split(":", 1)[0]

    if action == "reset_paper_confirm":
        _do_reset_paper()
        await query.edit_message_text("\u2705 Paper portfolio reset to $%s." % paper_fmt)
    elif action == "reset_cancel":
        await query.edit_message_text("\u274c Reset cancelled.")
    elif action == "reset_paper":
        await query.edit_message_text(
            "\u26a0\ufe0f Reset paper portfolio to $%s?\nAll trade history will be cleared.\n(Confirm within 60s.)"
            % paper_fmt,
            reply_markup=_reset_buttons("paper"),
        )


async def cmd_perf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show performance stats (optional date or N days)."""
    t0 = asyncio.get_event_loop().time()
    await update.message.reply_chat_action(ChatAction.TYPING)
    now_et = _now_et()
    today = now_et.strftime("%Y-%m-%d")

    long_history = trade_history
    short_hist = short_trade_history
    label = "Paper Portfolio"

    # v3.3.1: also consider currently-open positions so an open-but-
    # uncovered entry (which is invisible in trade_history until exit)
    # doesn't make /perf claim there's nothing to show.
    long_opens, short_opens = _open_positions_as_pseudo_trades()

    if not long_history and not short_hist and not long_opens and not short_opens:
        await update.message.reply_text("No completed trades yet.", reply_markup=_menu_button())
        return

    # Date filtering: /perf = all time, /perf 7 = last 7 days, /perf Apr 17 = single day
    date_filter = None
    single_day = False
    perf_label = "All Time"
    if context.args:
        raw = " ".join(context.args).strip()
        try:
            n = int(raw)
            if 1 <= n <= 365:
                date_filter = (now_et - timedelta(days=n)).strftime("%Y-%m-%d")
                perf_label = "Last %d days" % n
        except ValueError:
            target_date = _parse_date_arg(context.args)
            date_filter = target_date.strftime("%Y-%m-%d")
            single_day = True
            perf_label = target_date.strftime("%a %b %d, %Y")

    # Run ALL data processing + chart generation in executor (non-blocking)
    loop = asyncio.get_event_loop()
    try:
        msg, chart_buf = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                _perf_compute,
                long_history,
                short_hist,
                date_filter,
                single_day,
                today,
                label,
                perf_label,
                long_opens,
                short_opens,
            ),
            timeout=10.0,
        )
    except asyncio.TimeoutError:
        logger.warning("cmd_perf: executor timed out after 10s")
        await update.message.reply_text(
            "\u26a0\ufe0f Performance report timed out. Try again.", reply_markup=_menu_button()
        )
        return

    await _reply_in_chunks(update.message, msg)

    if chart_buf:
        await update.message.reply_photo(
            photo=chart_buf, caption="Equity Curve", reply_markup=_menu_button()
        )
    elif MATPLOTLIB_AVAILABLE and (long_history or short_hist):
        await update.message.reply_text(
            "\U0001f4ca Chart unavailable (timeout or no data)", reply_markup=_menu_button()
        )
    else:
        await update.effective_message.reply_text("\u2500", reply_markup=_menu_button())
    logger.info("CMD perf completed in %.2fs", asyncio.get_event_loop().time() - t0)


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/price AAPL — live quote from Yahoo Finance."""
    t0 = asyncio.get_event_loop().time()
    await update.message.reply_chat_action(ChatAction.TYPING)
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /price AAPL", reply_markup=_menu_button())
        return

    ticker = args[0].upper()
    prog = await update.message.reply_text("\u23f3 Fetching %s..." % ticker)
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, _price_sync, ticker)
    try:
        if text is None:
            await prog.edit_text(
                "Could not fetch data for %s" % ticker, reply_markup=_menu_button()
            )
        elif len(text) > 3800:
            await prog.delete()
            await _reply_in_chunks(update.message, text, reply_markup=_menu_button())
        else:
            await prog.edit_text(text, reply_markup=_menu_button())
    except Exception:
        # Log and try a plain reply as a fallback so the user sees
        # *something* instead of being stuck on "Fetching…". The
        # previous bare `except Exception: pass` swallowed every
        # telegram.error.BadRequest (stale prog msg, network) and left
        # the user staring at the loading placeholder forever.
        logger.debug("cmd_price: edit/chunk reply failed", exc_info=True)
        try:
            if text is not None:
                await update.message.reply_text(
                    text[:3800],
                    reply_markup=_menu_button(),
                )
        except Exception:
            logger.debug("cmd_price: fallback reply failed", exc_info=True)
    logger.info("CMD price completed in %.2fs", asyncio.get_event_loop().time() - t0)


async def cmd_proximity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show breakout proximity: SPY/QQQ gate + per-ticker gap to OR.

    Read-only diagnostic view. Does not change any trade logic.
    """
    t0 = asyncio.get_event_loop().time()
    await update.message.reply_chat_action(ChatAction.TYPING)
    loop = asyncio.get_event_loop()
    text, err = await loop.run_in_executor(None, _proximity_sync)
    if text is None:
        await update.message.reply_text(
            err or "Proximity unavailable.",
            reply_markup=_menu_button(),
        )
        logger.info(
            "CMD proximity completed in %.2fs (no data)", asyncio.get_event_loop().time() - t0
        )
        return
    body = "```\n" + text + "\n```"
    await update.message.reply_text(
        body,
        parse_mode="Markdown",
        reply_markup=_proximity_keyboard(),
    )
    logger.info("CMD proximity completed in %.2fs", asyncio.get_event_loop().time() - t0)


async def cmd_orb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show today's OR levels. `/orb recover` re-collects any missing ORs."""
    # Subcommand: /orb recover (folds in legacy /or_now)
    args = context.args if context.args else []
    if args and args[0].lower() in ("recover", "recollect", "refresh"):
        await cmd_or_now(update, context)
        return
    t0 = asyncio.get_event_loop().time()
    await update.message.reply_chat_action(ChatAction.TYPING)
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, _orb_sync)
    if text is None:
        await update.message.reply_text(
            "OR not collected yet \u2014 runs at 8:35 CT.", reply_markup=_menu_button()
        )
        logger.info("CMD orb completed in %.2fs (no data)", asyncio.get_event_loop().time() - t0)
        return
    await _reply_in_chunks(update.message, text, reply_markup=_menu_button())
    logger.info("CMD orb completed in %.2fs", asyncio.get_event_loop().time() - t0)


async def cmd_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause/resume scanner. /monitoring pause | resume | (no arg = show status)."""
    args = context.args
    action = args[0].lower() if args else ""

    if action == "pause":
        _tg_module()._scan_paused = True
        await update.message.reply_text(
            "\U0001f50d Scanner: PAUSED\n"
            "  Tap below to resume.\n"
            "  Existing positions still managed.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "\u25b6\ufe0f Resume Scanner", callback_data="monitoring_resume"
                        )
                    ]
                ]
            ),
        )
    elif action == "resume":
        _tg_module()._scan_paused = False
        await update.message.reply_text(
            "\U0001f50d Scanner: ACTIVE\n  Watching for breakouts.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("\u23f8 Pause Scanner", callback_data="monitoring_pause")]]
            ),
        )
    else:
        _tg = _tg_module()
        status = "PAUSED" if _tg._scan_paused else "ACTIVE"
        if _tg._scan_paused:
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "\u25b6\ufe0f Resume Scanner", callback_data="monitoring_resume"
                        )
                    ]
                ]
            )
        else:
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("\u23f8 Pause Scanner", callback_data="monitoring_pause")]]
            )
        await update.message.reply_text(
            "\U0001f50d Scanner: %s\n  Existing positions still managed." % status, reply_markup=kb
        )
    await update.effective_message.reply_text("\u2500", reply_markup=_menu_button())


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a quick tap-grid of all commands."""
    keyboard = _build_menu_keyboard()
    await update.message.reply_text(
        "\U0001f4f1 Quick Menu\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_or_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually re-collect OR data for tickers missing or_high."""
    t0 = asyncio.get_event_loop().time()
    await update.message.reply_chat_action(ChatAction.TYPING)

    missing = [t for t in TICKERS if t not in or_high]
    if not missing:
        await update.message.reply_text(
            "\u2705 All ORs already collected.", reply_markup=_menu_button()
        )
        logger.info(
            "CMD or_now completed in %.2fs (none missing)", asyncio.get_event_loop().time() - t0
        )
        return

    lines = {t: "\u23f3" for t in missing}

    def _fmt():
        body = "\n".join("  %-6s %s" % (t, lines[t]) for t in missing)
        return "\U0001f504 OR Recovery (%d tickers)\n%s\n%s" % (len(missing), "\u2500" * 26, body)

    prog = await update.message.reply_text(_fmt())

    loop = asyncio.get_event_loop()
    recovered = 0
    for ticker in missing:
        result = await loop.run_in_executor(None, _fetch_or_for_ticker, ticker)
        if result:
            recovered += 1
            lines[ticker] = "\u2705 $%.2f\u2013$%.2f (%s)" % (
                result["high"],
                result["low"],
                result["src"],
            )
        else:
            lines[ticker] = "\u274c failed"
        try:
            await prog.edit_text(_fmt())
        except Exception:
            pass

    if recovered > 0:
        save_paper_state()

    failed = len(missing) - recovered
    summary = _fmt() + "\n%s\n%d recovered | %d failed" % ("\u2500" * 26, recovered, failed)
    try:
        await prog.edit_text(summary, reply_markup=_menu_button())
    except Exception:
        pass
    logger.info("CMD or_now completed in %.2fs", asyncio.get_event_loop().time() - t0)


async def cmd_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ticker — unified add/remove/list for the tracked universe.

    Sub-commands (case-insensitive, several aliases each):
      list   | ls | show       — show the current watchlist
      add    | +              — add SYM; primes PDC/OR/RSI/bars
      remove | rm | del | -   — drop SYM (SPY/QQQ are pinned)
    """
    args = context.args or []
    if not args:
        # Bare /ticker defaults to list — most common case.
        await update.message.reply_text(_fmt_tickers_list(), reply_markup=_menu_button())
        return
    sub = (args[0] or "").strip().lower()

    if sub in ("list", "ls", "show"):
        await update.message.reply_text(_fmt_tickers_list(), reply_markup=_menu_button())
        return

    if sub in ("add", "+"):
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /ticker add SYM\nExample: /ticker add QBTS", reply_markup=_menu_button()
            )
            return
        await update.message.reply_chat_action(ChatAction.TYPING)
        # Run in executor — add_ticker does blocking HTTP (FMP + bars).
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, add_ticker, args[1])
        await update.message.reply_text(_fmt_add_reply(res), reply_markup=_menu_button())
        return

    if sub in ("remove", "rm", "del", "delete", "-"):
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /ticker remove SYM\nExample: /ticker remove QBTS",
                reply_markup=_menu_button(),
            )
            return
        res = remove_ticker(args[1])
        await update.message.reply_text(_fmt_remove_reply(res), reply_markup=_menu_button())
        return

    # Unknown sub-command — show usage.
    await update.message.reply_text(_TICKER_USAGE, reply_markup=_menu_button())
