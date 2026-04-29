"""telegram_ui.menu \u2014 keyboards and callback dispatch.

Extracted from trade_genius.py in v5.11.1 PR 3. Pure code motion \u2014
zero behavior change. State (positions, OR/PDC dicts, _scan_paused,
TRADE_TICKERS, BOT_NAME/BOT_VERSION, etc.) and helper functions
(_now_cdt, _now_et, _build_positions_text, _dashboard_sync,
_proximity_sync, _proximity_keyboard, _reply_in_chunks,
_run_system_test_sync) still live in trade_genius; this module reaches
them through the live-module accessor `_tg()` so __main__ vs imported
execution both work.
"""
from __future__ import annotations

import asyncio
import logging
import sys as _sys

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

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


async def positions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle refresh button tap on /status and /positions.

    Appends a 'Refreshed HH:MM:SS CDT' footer so each tap produces a
    visibly different message \u2014 Telegram rejects edits whose body
    and markup are identical to the current message with
    'Message is not modified'. If that race still wins (rapid double
    tap in the same second), we swallow the error silently; the user
    already got the button-tap acknowledgment via query.answer().
    """
    tg = _tg()
    query = update.callback_query
    await query.answer("Refreshing...")
    loop = asyncio.get_event_loop()
    msg = await loop.run_in_executor(None, tg._build_positions_text)
    # Ensure content changes between taps even if prices and positions
    # are momentarily identical (common outside market hours).
    stamp = tg._now_cdt().strftime("%H:%M:%S CDT")
    msg = "%s\n\u21bb Refreshed %s" % (msg, stamp)
    refresh_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("\U0001f504 Refresh", callback_data="positions_refresh")
    ]])
    try:
        await query.edit_message_text(msg, reply_markup=refresh_kb)
    except Exception as e:
        # Harmless race ("Message is not modified") \u2014 don't surface.
        logger.debug("positions_callback edit failed: %s", e)


async def proximity_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle refresh button tap on /proximity."""
    tg = _tg()
    query = update.callback_query
    await query.answer("Refreshing...")
    loop = asyncio.get_event_loop()
    text, err = await loop.run_in_executor(None, tg._proximity_sync)
    if text is None:
        # Edit to show the error and drop refresh button (no data to refresh)
        try:
            await query.edit_message_text(
                err or "Proximity unavailable.",
                reply_markup=_menu_button(),
            )
        except Exception as e:
            logger.debug("proximity_callback edit (no-data) failed: %s", e)
        return
    body = "```\n" + text + "\n```"
    try:
        await query.edit_message_text(
            body,
            parse_mode="Markdown",
            reply_markup=tg._proximity_keyboard(),
        )
    except Exception as e:
        # Common case: "Message is not modified" when nothing changed
        # between ticks. Swallow silently \u2014 the user got their ack.
        logger.debug("proximity_callback edit failed: %s", e)


async def monitoring_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard taps for /monitoring."""
    tg = _tg()
    query = update.callback_query
    await query.answer()
    if query.data == "monitoring_pause":
        tg._scan_paused = True
        await query.edit_message_text(
            "\U0001f50d Scanner: PAUSED\n  Tap below to resume.\n  Existing positions still managed.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("\u25b6\ufe0f Resume Scanner", callback_data="monitoring_resume")
            ]])
        )
    elif query.data == "monitoring_resume":
        tg._scan_paused = False
        await query.edit_message_text(
            "\U0001f50d Scanner: ACTIVE\n  Watching for breakouts.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("\u23f8 Pause Scanner", callback_data="monitoring_pause")
            ]])
        )


# ============================================================
# MENU KEYBOARD BUILDER + MENU BUTTON HELPER
# ============================================================
def _build_menu_keyboard():
    """Main /menu keyboard \u2014 daily-use commands only.

    Ten tiles in a 2-column grid plus a full-width Advanced button that
    opens the secondary keyboard built by `_build_advanced_menu_keyboard`.
    """
    return [
        [
            InlineKeyboardButton("\U0001f4ca Dashboard", callback_data="menu_dashboard"),
            InlineKeyboardButton("\U0001f4c8 Status", callback_data="menu_positions"),
        ],
        [
            InlineKeyboardButton("\U0001f4c9 Perf", callback_data="menu_perf"),
            InlineKeyboardButton("\U0001f4b0 Price", callback_data="menu_price_prompt"),
        ],
        [
            InlineKeyboardButton("\U0001f4d0 OR", callback_data="menu_orb"),
            InlineKeyboardButton("\U0001f3af Proximity", callback_data="menu_proximity"),
        ],
        [
            InlineKeyboardButton("\U0001f39b\ufe0f Mode", callback_data="menu_mode"),
            InlineKeyboardButton("\u2753 Help", callback_data="menu_help"),
        ],
        [
            InlineKeyboardButton("\U0001f50d Monitor", callback_data="menu_monitoring"),
        ],
        [
            InlineKeyboardButton("\u2699\ufe0f Advanced", callback_data="menu_advanced"),
        ],
    ]


def _build_advanced_menu_keyboard():
    """Advanced /menu keyboard \u2014 rarely-needed commands.

    Accessible via the 'Advanced' button on the main menu. Includes a
    Back button to return to the main keyboard.
    """
    return [
        # Reports
        [
            InlineKeyboardButton("\U0001f4c5 Day Report", callback_data="menu_dayreport"),
            InlineKeyboardButton("\U0001f4dc Log", callback_data="menu_log"),
        ],
        [
            InlineKeyboardButton("\U0001f3ac Replay", callback_data="menu_replay"),
        ],
        # Market data recovery / system
        [
            InlineKeyboardButton("\U0001f504 OR Recover", callback_data="menu_or_recover"),
            InlineKeyboardButton("\U0001f9ea Test", callback_data="menu_test"),
        ],
        # Reference
        [
            InlineKeyboardButton("\U0001f4d8 Strategy", callback_data="menu_strategy"),
            InlineKeyboardButton("\U0001f4d6 Algo", callback_data="menu_algo"),
        ],
        [
            InlineKeyboardButton("\u2139\ufe0f Version", callback_data="menu_version"),
            InlineKeyboardButton("\u26a0\ufe0f Reset", callback_data="menu_reset"),
        ],
        # Nav
        [
            InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="menu_back"),
        ],
    ]


def _menu_button():
    """Return a one-button InlineKeyboardMarkup with a Menu tap."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f5c2 Menu", callback_data="open_menu")]])


async def _cb_open_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the single Menu button tap \u2014 show full menu."""
    await update.callback_query.answer()
    keyboard = _build_menu_keyboard()
    await update.callback_query.message.reply_text(
        "\U0001f4f1 Quick Menu\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


class _CallbackUpdateShim:
    """Minimal Update-like wrapper that lets cmd_* handlers be invoked from
    an inline-button callback. The handlers only touch update.message.*
    (reply_text / reply_photo / reply_chat_action / reply_document) and
    update.effective_message / update.effective_user, so we forward those
    to the callback_query's message/user.
    """
    __slots__ = ("_query",)

    def __init__(self, query):
        self._query = query

    def get_bot(self):
        return self._query.get_bot()

    @property
    def message(self):
        return self._query.message

    @property
    def effective_message(self):
        return self._query.message

    @property
    def effective_user(self):
        return self._query.from_user

    @property
    def effective_chat(self):
        return self._query.message.chat

    @property
    def callback_query(self):
        # Some code paths may still want the raw query; preserve it.
        return self._query


async def _invoke_from_callback(query, context, handler, *, args=None):
    """Run a cmd_* handler as if it came from a regular message.

    `args` optionally overrides context.args (e.g. to inject a date). The
    override is scoped to this call only; context.args is restored after.
    """
    shim = _CallbackUpdateShim(query)
    saved_args = context.args
    try:
        context.args = list(args) if args is not None else []
        await handler(shim, context)
    finally:
        context.args = saved_args


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle taps on /menu inline buttons."""
    # Lazy import \u2014 telegram_commands imports trade_genius, which would
    # cause a circular import at module load time if hoisted to the top.
    import telegram_commands

    tg = _tg()
    query = update.callback_query
    await query.answer()

    # --- Navigation between main and advanced submenus ---
    if query.data == "menu_advanced":
        try:
            await query.edit_message_text(
                "\u2699\ufe0f Advanced\n" + "\u2500" * 30,
                reply_markup=InlineKeyboardMarkup(_build_advanced_menu_keyboard()),
            )
        except Exception:
            await query.message.reply_text(
                "\u2699\ufe0f Advanced",
                reply_markup=InlineKeyboardMarkup(_build_advanced_menu_keyboard()),
            )
        return
    if query.data == "menu_back":
        try:
            await query.edit_message_text(
                "\U0001f4f1 Quick Menu\n" + "\u2500" * 30,
                reply_markup=InlineKeyboardMarkup(_build_menu_keyboard()),
            )
        except Exception:
            await query.message.reply_text(
                "\U0001f4f1 Quick Menu",
                reply_markup=InlineKeyboardMarkup(_build_menu_keyboard()),
            )
        return

    # --- Lightweight callbacks that replace the menu message in-place ---
    if query.data == "menu_price_prompt":
        await query.edit_message_text("Use /price TICKER (e.g. /price AAPL)")
        return

    if query.data == "menu_version":
        note = tg.MAIN_RELEASE_NOTE
        await query.edit_message_text(
            "%s v%s\n%s" % (tg.BOT_NAME, tg.BOT_VERSION, note))
        return

    if query.data == "menu_strategy":
        await query.edit_message_text("\u23f3 Loading...")
        SEP = "\u2500" * 26
        text = (
            "Strategy v%s\n%s\n" % (tg.BOT_VERSION, SEP)
            + "Long: ORB Breakout after 8:45 CT\n"
            "Short: Wounded Buffalo after 8:45 CT\n"
            "Trail: +1.0%% trigger | min $1.00\n"
            "Size: 10 shares | Max 5/ticker/day\n"
            "%s\nUse /strategy for full details" % SEP
        )
        await query.message.reply_text(text)
        return

    # --- Handlers that execute a real command via the shim ---
    # These don't edit the menu message; they reply with the command's output.
    if query.data == "menu_help":
        await _invoke_from_callback(query, context, telegram_commands.cmd_help)
        return
    if query.data == "menu_algo":
        await _invoke_from_callback(query, context, telegram_commands.cmd_algo)
        return
    if query.data == "menu_mode":
        await _invoke_from_callback(query, context, telegram_commands.cmd_mode)
        return
    if query.data == "menu_log":
        await _invoke_from_callback(query, context, telegram_commands.cmd_log)
        return
    if query.data == "menu_replay":
        await _invoke_from_callback(query, context, telegram_commands.cmd_replay)
        return
    if query.data == "menu_or_recover":
        await _invoke_from_callback(query, context, telegram_commands.cmd_or_now)
        return
    if query.data == "menu_reset":
        # /reset is a two-step confirm flow; delegate to its handler and let
        # it show the same confirmation keyboard it shows on the typed command.
        await _invoke_from_callback(query, context, telegram_commands.cmd_reset)
        return

    await query.edit_message_text("\u23f3 Loading...")

    if query.data == "menu_dashboard":
        # Show the same full dashboard that /dashboard produces.
        # The menu message itself has already been edited to "\u23f3 Loading..."
        # above, so we just swap it out with the real dashboard text.
        loop = asyncio.get_event_loop()
        try:
            text = await loop.run_in_executor(None, tg._dashboard_sync)
        except Exception:
            logger.exception("menu_dashboard: _dashboard_sync failed")
            await query.message.reply_text(
                "\u26a0\ufe0f Dashboard failed. Try again.",
                reply_markup=_menu_button(),
            )
            return
        try:
            if len(text) > 3800:
                await tg._reply_in_chunks(query.message, text, reply_markup=_menu_button())
            else:
                await query.message.reply_text(text, reply_markup=_menu_button())
        except Exception:
            logger.exception("menu_dashboard: send failed")
    elif query.data == "menu_positions":
        loop = asyncio.get_event_loop()
        msg = await loop.run_in_executor(None, tg._build_positions_text)
        refresh_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("\U0001f504 Refresh", callback_data="positions_refresh")
        ]])
        await query.message.reply_text(msg, reply_markup=refresh_kb)
    elif query.data == "menu_orb":
        now_et = tg._now_et()
        today = now_et.strftime("%Y-%m-%d")
        if tg.or_collected_date != today:
            await query.message.reply_text("OR not collected yet \u2014 runs at 8:35 CT.")
        else:
            orb_lines = ["\U0001f4d0 TODAY'S OR LEVELS \u2014 %s" % today]
            for t in tg.TRADE_TICKERS:
                orh = tg.or_high.get(t)
                if orh is None:
                    orb_lines.append("%s   --" % t)
                else:
                    orl = tg.or_low.get(t)
                    pdc_val = tg.pdc.get(t)
                    orl_s = "%.2f" % orl if orl else "--"
                    pdc_s = "%.2f" % pdc_val if pdc_val else "--"
                    orb_lines.append("%s  H:$%.2f  L:$%s  PDC:$%s" % (t, orh, orl_s, pdc_s))
            await query.message.reply_text("\n".join(orb_lines))
    elif query.data == "menu_dayreport":
        await _invoke_from_callback(query, context, telegram_commands.cmd_dayreport)
    elif query.data == "menu_proximity":
        await _invoke_from_callback(query, context, telegram_commands.cmd_proximity)
    elif query.data == "menu_perf":
        await _invoke_from_callback(query, context, telegram_commands.cmd_perf)
    elif query.data == "menu_monitoring":
        status = "PAUSED" if tg._scan_paused else "ACTIVE"
        if tg._scan_paused:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("\u25b6\ufe0f Resume Scanner", callback_data="monitoring_resume")
            ]])
        else:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("\u23f8 Pause Scanner", callback_data="monitoring_pause")
            ]])
        await query.message.reply_text(
            "\U0001f50d Scanner: %s" % status, reply_markup=kb)
    elif query.data == "menu_test":
        await query.message.reply_text("Running /test ...")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, tg._run_system_test_sync, "Manual")
