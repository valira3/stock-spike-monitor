"""telegram_ui.runtime \u2014 bot lifecycle: auth guard, command registration, startup, run loop.

Extracted from trade_genius.py in v5.11.1 PR 4. Pure code motion \u2014
zero behavior change. State and helpers (MAIN_BOT_COMMANDS, CHAT_ID,
TELEGRAM_TOKEN, TRADEGENIUS_OWNER_IDS, BOT_NAME, BOT_VERSION,
positions, paper_cash, _now_et, send_telegram, etc.) still live in
trade_genius; this module reaches them through the live-module
accessor `_tg()` so __main__ vs imported execution both work.
"""

from __future__ import annotations

import logging
import sys as _sys

from telegram import (
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

from telegram_ui.menu import (
    _build_menu_keyboard,
    _cb_open_menu,
    menu_callback,
    monitoring_callback,
    positions_callback,
    proximity_callback,
)

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


async def _set_bot_commands(app: Application) -> None:
    """Register / menu commands on startup (all scopes) + send startup menu."""
    tg = _tg()
    try:
        # Clear default scope first (removes any stale commands from old versions)
        await app.bot.set_my_commands(tg.MAIN_BOT_COMMANDS, scope=BotCommandScopeDefault())
        await app.bot.set_my_commands(tg.MAIN_BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats())
        await app.bot.set_my_commands(tg.MAIN_BOT_COMMANDS, scope=BotCommandScopeAllGroupChats())
        logger.info("Registered %d bot commands (all scopes)", len(tg.MAIN_BOT_COMMANDS))
    except Exception as e:
        logger.warning("Failed to set bot commands: %s", e)
    # Send startup menu
    await _send_startup_menu(app.bot, tg.CHAT_ID)


async def _send_startup_menu(bot, chat_id):
    """Send the interactive menu to a chat on startup/deploy."""
    tg = _tg()
    reply_markup = InlineKeyboardMarkup(_build_menu_keyboard())
    startup_text = (
        "\U0001f7e2 %s v%s online\n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "\U0001f5c2 Menu"
    ) % (tg.BOT_NAME, tg.BOT_VERSION)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=startup_text,
            reply_markup=reply_markup,
        )
        logger.info("Startup menu sent to %s", chat_id)
    except Exception as e:
        logger.warning("Startup menu send failed for %s: %s", chat_id, e)


def send_startup_message():
    """Send tailored deployment card to main and TP bots.

    v3.4.16: main card stays paper-only (no TP cash/positions, no TP
    release notes). TP card shows TP portfolio + TP release notes.
    """
    tg = _tg()
    SEP = "\u2500" * 34
    now_et = tg._now_et()
    weekday = now_et.weekday() < 5
    in_hours = (
        weekday
        and now_et.hour >= 9
        and (now_et.hour < 15 or (now_et.hour == 15 and now_et.minute < 55))
    )
    market_status = "OPEN" if in_hours else "CLOSED"

    universe = " ".join(tg.TRADE_TICKERS)
    n_paper_pos = len(tg.positions)
    paper_cash_fmt = f"{tg.paper_cash:,.2f}"

    main_msg = (
        f"\U0001f680 v{tg.BOT_VERSION} deployed\n"
        f"{tg.CURRENT_MAIN_NOTE}\n"
        f"{SEP}\n"
        f"Universe: {universe}\n"
        f"Strategy: Tiger Sovereign | Phase 1-4\n"
        f"Phase 1: QQQ 9 EMA + 09:30 AVWAP\n"
        f"Scan:     every {tg.SCAN_INTERVAL}s\n"
        f"Stops:    entry \u00b1 0.75% (cap)\n"
        f"{SEP}\n"
        f"\U0001f4c4 Paper:  ${paper_cash_fmt} cash | {n_paper_pos} positions\n"
        f"Market:   {market_status}\n"
        f"{SEP}\n"
        f"/help for all commands"
    )
    tg.send_telegram(main_msg)


# v3.6.0 \u2014 Telegram owner auth guard.
# Installed as a group=-1 TypeHandler so it fires BEFORE any default
# group=0 handler. Non-owners are silently dropped: no reply is sent,
# the update is logged server-side, and ApplicationHandlerStop prevents
# any downstream handler (command, callback, etc.) from running.
#
# Edge cases (also silently dropped):
#   * update.effective_user is None \u2014 e.g. channel posts, edited
#     messages with no sender.
#   * user id not a string member of TRADEGENIUS_OWNER_IDS.
async def _auth_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Drop every Telegram update that isn't from a whitelisted owner."""
    tg = _tg()
    eff_user = getattr(update, "effective_user", None)
    user_id_str = str(eff_user.id) if eff_user and getattr(eff_user, "id", None) is not None else ""
    if user_id_str and user_id_str in tg.TRADEGENIUS_OWNER_IDS:
        return  # authorized \u2014 let downstream handlers run

    eff_chat = getattr(update, "effective_chat", None)
    chat_id_str = str(eff_chat.id) if eff_chat and getattr(eff_chat, "id", None) is not None else ""
    update_id = getattr(update, "update_id", None)
    logger.warning(
        "auth_guard: dropped non-owner update (update_id=%s user_id=%r chat_id=%r)",
        update_id,
        user_id_str or "(none)",
        chat_id_str or "(none)",
    )
    raise ApplicationHandlerStop


def run_telegram_bot():
    """Start Telegram bot (paper-only, single bot)."""
    tg = _tg()
    # Lazy import \u2014 telegram_commands does `from trade_genius import ...`,
    # so importing it at module top-level would create a circular import
    # when trade_genius first imports telegram_ui.runtime.
    import telegram_commands

    app = Application.builder().token(tg.TELEGRAM_TOKEN).post_init(_set_bot_commands).build()

    # v3.6.0 \u2014 Owner auth guard: every update is screened against
    # TRADEGENIUS_OWNER_IDS before any downstream handler sees it.
    # Must be installed FIRST (group=-1) so it runs before the default
    # group=0 command/callback handlers.
    app.add_handler(TypeHandler(Update, _auth_guard), group=-1)

    app.add_handler(CommandHandler("help", telegram_commands.cmd_help))
    app.add_handler(CommandHandler("dashboard", telegram_commands.cmd_dashboard))
    app.add_handler(CommandHandler("status", telegram_commands.cmd_status))
    app.add_handler(CommandHandler("log", telegram_commands.cmd_log))
    app.add_handler(CommandHandler("replay", telegram_commands.cmd_replay))
    app.add_handler(CommandHandler("dayreport", telegram_commands.cmd_dayreport))
    app.add_handler(CommandHandler("version", telegram_commands.cmd_version))
    app.add_handler(CommandHandler("near_misses", telegram_commands.cmd_near_misses))
    app.add_handler(CommandHandler("retighten", telegram_commands.cmd_retighten))
    app.add_handler(CommandHandler("trade_log", telegram_commands.cmd_trade_log))
    app.add_handler(CommandHandler("mode", telegram_commands.cmd_mode))
    app.add_handler(CommandHandler("reset", telegram_commands.cmd_reset))
    app.add_handler(CommandHandler("perf", telegram_commands.cmd_perf))
    app.add_handler(CommandHandler("price", telegram_commands.cmd_price))
    app.add_handler(CommandHandler("orb", telegram_commands.cmd_orb))
    app.add_handler(CommandHandler("proximity", telegram_commands.cmd_proximity))
    # v5.13.5 \u2014 Phase 1 (Section I) permit diagnostic.
    app.add_handler(CommandHandler("regime", telegram_commands.cmd_regime))
    app.add_handler(CommandHandler("monitoring", telegram_commands.cmd_monitoring))
    app.add_handler(CommandHandler("algo", telegram_commands.cmd_algo))
    app.add_handler(CommandHandler("strategy", telegram_commands.cmd_strategy))
    app.add_handler(CommandHandler("test", telegram_commands.cmd_test))
    app.add_handler(CommandHandler("menu", telegram_commands.cmd_menu))
    # v3.4.32 \u2014 runtime ticker universe management
    app.add_handler(CommandHandler("ticker", telegram_commands.cmd_ticker))
    # v6.18.0 \u2014 daily pre-open market expectations brief
    app.add_handler(CommandHandler("brief", telegram_commands.cmd_brief))

    # Callback query handlers
    app.add_handler(CallbackQueryHandler(monitoring_callback, pattern="^monitoring_"))
    app.add_handler(CallbackQueryHandler(telegram_commands.reset_callback, pattern="^reset_"))
    app.add_handler(CallbackQueryHandler(positions_callback, pattern="^positions_"))
    app.add_handler(CallbackQueryHandler(proximity_callback, pattern="^proximity_refresh$"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(_cb_open_menu, pattern="^open_menu$"))

    async def _error_handler(update, context):
        logger.error("Unhandled exception: %s", context.error, exc_info=context.error)
        if update and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "\u26a0\ufe0f Command failed: " + str(context.error)[:100]
                )
            except Exception:
                pass

    app.add_error_handler(_error_handler)

    app.run_polling()
