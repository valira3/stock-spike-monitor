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
    ChatAction,
    ContextTypes,
    DAILY_LOSS_LIMIT,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MAIN_RELEASE_NOTE,
    PAPER_STARTING_CAPITAL,
    Path,
    RESET_CONFIRM_WINDOW_SEC,
    TICKERS,
    TRADEGENIUS_OWNER_IDS,
    TelegramBadRequest,
    Update,
    _TICKER_USAGE,
    _dashboard_sync,
    _do_reset_paper,
    _now_et,
    _parse_date_arg,
    _run_system_test_sync_v2,
    _status_text_sync,
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
    save_paper_state,
    short_positions,
    short_trade_history,
    time,
    timedelta,
    trade_history,
    trade_log_read_tail,
    urllib,
)

# v5.12.0 \u2014 telegram_ui helpers imported directly from their canonical
# modules (deprecation aliases in trade_genius removed in v5.12.0 PR 5).
from telegram_ui.charts import (
    _chart_dayreport,
    _chart_portfolio_pie,
    _format_dayreport_section,
    _open_positions_as_pseudo_trades,
    _reply_in_chunks,
)
from telegram_ui.commands import (
    _fetch_or_for_ticker,
    _fmt_add_reply,
    _fmt_remove_reply,
    _fmt_tickers_list,
    _log_sync,
    _orb_sync,
    _perf_compute,
    _price_sync,
    _proximity_keyboard,
    _proximity_sync,
    _replay_sync,
    _reset_buttons,
)
from telegram_ui.menu import _build_menu_keyboard, _menu_button  # v5.11.1 PR 3


# v6.11.1 — pre-market check integration. Optional: if import fails,
# cmd_test continues with the existing 15-check body only.
_premarket_check_available = False
try:
    import sys as _sys
    import os as _os
    _tg_root = _os.path.dirname(_os.path.abspath(__file__))
    if _tg_root not in _sys.path:
        _sys.path.insert(0, _tg_root)
    from scripts.premarket_check import format_for_telegram as _fmt_premarket
    from scripts.premarket_check import run_all_checks as _run_premarket
    _premarket_check_available = True
except Exception as _pmc_import_err:
    logger.debug("cmd_test: pre-market check import failed (non-fatal): %s", _pmc_import_err)


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /test command — v6.7.0 15-check system health test plus v6.11.1
    pre-market readiness check (14 checks, appended after a separator).

    Single final edit pattern preserved from v5.1.5 to avoid Telegram
    editMessageText rate-limit races. Both suites run in executor threads
    (blocking I/O). Pre-market check failure never breaks the existing output.
    """
    t0 = asyncio.get_event_loop().time()
    await update.message.reply_chat_action(ChatAction.TYPING)
    prog = await update.message.reply_text(
        "\U0001f9ea Running system test\u2026 (up to 25s)"
    )

    loop = asyncio.get_event_loop()
    body = await loop.run_in_executor(None, lambda: _run_system_test_sync_v2("manual"))

    # v6.11.1 — append pre-market check results. write_artifact=False preserves
    # the daily /data/preflight/<date>.json written by the 04:30 ET cron.
    if _premarket_check_available:
        try:
            pmc_result = await loop.run_in_executor(
                None,
                lambda: _run_premarket(in_container=True, write_artifact=False),
            )
            pmc_body = _fmt_premarket(pmc_result)
            body = body + "\n\n---\n\n" + pmc_body
        except Exception as _pmc_err:
            body = body + "\n\n---\n\n[ERR] Pre-market check raised: %s: %s" % (
                type(_pmc_err).__name__, str(_pmc_err)[:120]
            )
            logger.warning("cmd_test: pre-market check raised: %s", _pmc_err)

    # Single final edit. TelegramBadRequest covers "message is not
    # modified" no-ops; any other failure falls back to reply_text.
    try:
        await prog.edit_text(body, reply_markup=_menu_button())
    except TelegramBadRequest as e:
        logger.debug("cmd_test: edit_text final: %s", e)
    except Exception as e:
        logger.info("cmd_test: final edit_text fell back to reply_text: %s", e)
        try:
            await update.message.reply_text(body, reply_markup=_menu_button())
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
        "  /regime      Phase 1 permit\n"
        "  /mode        Market regime\n"
        "\n"
        "Reports\n"
        "  /dayreport [date]  Trades + P&L\n"
        "  /log [date]        Trade log\n"
        "  /replay [date]     Timeline\n"
        "\n"
        "System\n"
        "  /monitoring  Pause/resume scan\n"
        "  /test        Health + premkt\n"
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
    """v5.26.0 \u2014 near-miss tracker removed (volume gate is bypassed)."""
    await update.message.reply_text(
        "\U0001f50d Near-misses removed in v5.26.0\n(volume gate bypassed; no near-miss feed).",
        reply_markup=_menu_button(),
    )


async def cmd_retighten(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """v5.26.0 \u2014 stop-retighten machinery removed.
    Spec only has the R-2 hard stop and per-tick A-C / A-E ratchets,
    both wired through the sentinel.
    """
    await update.message.reply_text(
        "\U0001f527 /retighten removed in v5.26.0\n(stop ladder + breakeven cap deleted per spec).",
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
    lines = [
        "\U0001f9ed MARKET MODE",
        SEP,
        "MarketMode classifier removed in v5.26.0",
        "per Tiger Sovereign v15.0 spec-strict pass.",
        SEP,
        "Loss limit: $%+.2f" % DAILY_LOSS_LIMIT,
        "(v%s)" % BOT_VERSION,
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
    """Show compact strategy summary (Tiger Sovereign vAA-1)."""
    SEP = "\u2500" * 26
    text = (
        f"\U0001f4d8 Strategy v{BOT_VERSION}\n"
        f"  Tiger Sovereign (vAA-1)\n"
        f"{SEP}\n"
        "\U0001f4c8 LONG - The Bison\n"
        "\n"
        "Phase 1 - Weather (L-P1-S1/S2)\n"
        "  \u2022 QQQ 5m close > 9 EMA\n"
        "  \u2022 QQQ price > 09:30 AVWAP\n"
        "  Both true -> LONG permit ON\n"
        "\n"
        "Phase 2 - Permits\n"
        "  \u2022 Volume Bucket (L-P2-S3):\n"
        "    auto-pass before 10:00 ET;\n"
        "    else vol >= 1x 55-bar avg\n"
        "  \u2022 ORH Hold (L-P2-S4):\n"
        "    2 consec 1m closes > ORH\n"
        "\n"
        "Phase 3 - Strike Sizing\n"
        "  Max 3 strikes/ticker/day\n"
        "  (STRIKE-CAP-3)\n"
        "  \u2022 FULL (L-P3-FULL):\n"
        "    1m DI+ > 30 -> BUY 100%\n"
        "  \u2022 Scaled-A (L-P3-SCALED-A):\n"
        "    1m DI+ 25-30 -> BUY 50%\n"
        "  \u2022 Scaled-B (L-P3-SCALED-B):\n"
        "    add-on +50% if DI+>30\n"
        "    + fresh NHOD + Alarm E=F\n"
        "\n"
        "Sentinel Loop (all parallel):\n"
        "  A/B/D -> MARKET EXIT;\n"
        "  C/E -> ratchet stop only\n"
        "  \u2022 A1 Loss (SENT-A_LOSS):\n"
        "    unrealized <= -$500\n"
        "    -> EXIT 100%\n"
        "  \u2022 A2 Flash (SENT-A_FLASH):\n"
        "    1m move > -1% vs pos\n"
        "    -> EXIT 100%\n"
        "  \u2022 B Trend Death (SENT-B):\n"
        "    5m close < 5m 9-EMA\n"
        "    -> EXIT 100%\n"
        "  \u2022 C Vel. Ratchet (SENT-C):\n"
        "    3 declining 1m ADX\n"
        "    -> STOP @ price-0.25%\n"
        "  \u2022 D HVP Lock (SENT-D):\n"
        "    5m ADX < 75% Trade_HVP\n"
        "    -> EXIT 100%\n"
        "  \u2022 E Div. Trap (SENT-E):\n"
        "    price NHOD + RSI div\n"
        "    -> block S2/S3 entry\n"
        "    or ratchet stop\n"
        "\n"
        "Size: dollar-based paper sizing\n"
        "New entries cutoff: 14:44:59 CT\n"
        "EOD flush: 14:49:59 CT\n"
        "Daily breaker: -$1,500 -> halt\n"
        f"{SEP}\n"
        "\U0001f4c9 SHORT - Wounded Buffalo\n"
        "\n"
        "Phase 1 - Weather (S-P1-S1/S2)\n"
        "  \u2022 QQQ 5m close < 9 EMA\n"
        "  \u2022 QQQ price < 09:30 AVWAP\n"
        "  Both true -> SHORT permit ON\n"
        "\n"
        "Phase 2 - Permits\n"
        "  \u2022 Volume Bucket (S-P2-S3):\n"
        "    auto-pass before 10:00 ET;\n"
        "    else vol >= 1x 55-bar avg\n"
        "  \u2022 ORL Hold (S-P2-S4):\n"
        "    2 consec 1m closes < ORL\n"
        "\n"
        "Phase 3 - Strike Sizing\n"
        "  Max 3 strikes/ticker/day\n"
        "  (STRIKE-CAP-3)\n"
        "  \u2022 FULL (S-P3-FULL):\n"
        "    1m DI- > 30 -> SHORT 100%\n"
        "  \u2022 Scaled-A (S-P3-SCALED-A):\n"
        "    1m DI- 25-30 -> SHORT 50%\n"
        "  \u2022 Scaled-B (S-P3-SCALED-B):\n"
        "    add-on +50% if DI->30\n"
        "    + fresh NLOD + Alarm E=F\n"
        "\n"
        "Sentinel Loop (all parallel):\n"
        "  A/B/D -> MARKET EXIT;\n"
        "  C/E -> ratchet stop only\n"
        "  \u2022 A1 Loss (SENT-A_LOSS):\n"
        "    unrealized <= -$500\n"
        "    -> COVER 100%\n"
        "  \u2022 A2 Flash (SENT-A_FLASH):\n"
        "    1m move > +1% vs pos\n"
        "    -> COVER 100%\n"
        "  \u2022 B Trend Death (SENT-B):\n"
        "    5m close > 5m 9-EMA\n"
        "    -> COVER 100%\n"
        "  \u2022 C Vel. Ratchet (SENT-C):\n"
        "    3 declining 1m ADX\n"
        "    -> STOP @ price+0.25%\n"
        "  \u2022 D HVP Lock (SENT-D):\n"
        "    5m ADX < 75% Trade_HVP\n"
        "    -> COVER 100%\n"
        "  \u2022 E Div. Trap (SENT-E):\n"
        "    price NLOD + RSI div\n"
        "    -> block S2/S3 entry\n"
        "    or ratchet stop\n"
        "\n"
        "Size: dollar-based paper sizing\n"
        "New entries cutoff: 14:44:59 CT\n"
        "EOD flush: 14:49:59 CT\n"
        "Daily breaker: -$1,500 -> halt\n"
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


async def cmd_regime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the current Phase 1 (Section I) QQQ permit state.

    v5.13.5: replaces the legacy SPY/QQQ-vs-PDC global gate diagnostic.
    Reads from v5_10_6_snapshot._section_i_permit so this view agrees
    with the dashboard.
    """
    SEP = "\u2500" * 26
    long_open = False
    short_open = False
    qqq_close = qqq_ema9 = qqq_avwap = qqq_last = None
    try:
        import v5_10_6_snapshot as _v510
        import trade_genius as _tg_mod

        sip = _v510._section_i_permit(_tg_mod)
        long_open = bool(sip.get("long_open"))
        short_open = bool(sip.get("short_open"))
        qqq_close = sip.get("qqq_5m_close")
        qqq_ema9 = sip.get("qqq_5m_ema9")
        qqq_avwap = sip.get("qqq_avwap_0930")
        qqq_last = sip.get("qqq_current_price")
    except Exception:
        pass

    def _fmt(v):
        return ("$%.2f" % v) if isinstance(v, (int, float)) and v else "--"

    long_icon = "\u2705" if long_open else "\u274c"
    short_icon = "\u2705" if short_open else "\u274c"

    text = (
        f"\U0001f6e1 REGIME \u2014 Phase 1 (Section I)\n"
        f"{SEP}\n"
        f"QQQ last:   {_fmt(qqq_last)}\n"
        f"QQQ 5m cl:  {_fmt(qqq_close)}\n"
        f"QQQ 9 EMA:  {_fmt(qqq_ema9)}\n"
        f"QQQ AVWAP:  {_fmt(qqq_avwap)} (09:30)\n"
        f"{SEP}\n"
        f"Long permit:  {long_icon}\n"
        f"Short permit: {short_icon}\n"
        f"{SEP}\n"
        f"Long  = QQQ 5m close > 9 EMA\n"
        f"        AND QQQ > 09:30 AVWAP\n"
        f"Short = QQQ 5m close < 9 EMA\n"
        f"        AND QQQ < 09:30 AVWAP"
    )
    await update.message.reply_text(text, reply_markup=_menu_button())


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


# v6.18.0 \u2014 daily market-expectations brief
async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pre-open market brief: EW universe, macro snapshot, movers, catalysts.

    Builder lives in market_brief.py so the same payload can be fired from
    the daily 7:00 CT scheduler entry without going through the Telegram
    command surface.
    """
    t0 = asyncio.get_event_loop().time()
    await update.message.reply_chat_action(ChatAction.TYPING)
    prog = await update.message.reply_text("\u23f3 Building brief...")
    loop = asyncio.get_event_loop()
    try:
        from market_brief import build_market_brief

        text = await loop.run_in_executor(
            None, build_market_brief, BOT_VERSION, os.environ.get("FMP_API_KEY", "")
        )
    except Exception as exc:
        logger.exception("cmd_brief: build failed")
        try:
            await prog.edit_text("\u26a0\ufe0f Brief failed: %s" % str(exc)[:120])
        except Exception:
            pass
        return
    try:
        if len(text) > 3800:
            await prog.delete()
            await _reply_in_chunks(update.message, text, reply_markup=_menu_button())
        else:
            await prog.edit_text(text, reply_markup=_menu_button())
    except Exception:
        try:
            await update.message.reply_text(text, reply_markup=_menu_button())
        except Exception:
            pass
    logger.info("CMD brief completed in %.2fs", asyncio.get_event_loop().time() - t0)
