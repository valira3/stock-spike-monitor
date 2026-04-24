"""
TradeGenius v3.5.1 — Eye of the Tiger 2.0 (paper book)
===========================================================================
ORB Momentum Breakout + Wounded Buffalo Short on a user-defined ticker
universe. Paper book only; live execution arrives in v4.0.0 via the
Alpaca-backed TradeGenius executors (Val + Gene).
Infrastructure: Telegram bot, paper trading, dashboard, scheduler.
"""

import os
from pathlib import Path
import json
import re
import time
import logging
import threading
import urllib.request
import asyncio
import signal
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from telegram import (
    BotCommand, BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats, BotCommandScopeDefault, Update,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler,
    CommandHandler, ContextTypes, TypeHandler,
)

# ============================================================
# CONFIG FROM ENVIRONMENT VARIABLES
# ============================================================
TELEGRAM_TOKEN          = os.getenv("TELEGRAM_TOKEN")
CHAT_ID                 = os.getenv("CHAT_ID")
# v3.4.41 — treat empty string as unset so Railway vars left blank still
# fall back to the hardcoded owner ID.
_RH_OWNER_DEFAULT       = "5165570192"

# v3.6.0 — Telegram owner whitelist. Every Telegram update is checked
# against this set by a group=-1 TypeHandler before any other handler
# fires; non-owners are silently dropped (no reply, server-side log only).
# Comma-separated Telegram user ids (positive integers), NOT chat ids.
# Default includes Val so DM resets always work from the default deploy.
# v3.6.0 renamed from RH_OWNER_USER_IDS; the old env var is no longer read.
_TRADEGENIUS_OWNERS_RAW = os.getenv("TRADEGENIUS_OWNER_IDS", "").strip() or _RH_OWNER_DEFAULT
TRADEGENIUS_OWNER_IDS   = {
    u.strip() for u in _TRADEGENIUS_OWNERS_RAW.split(",") if u.strip()
}

BOT_NAME    = "TradeGenius"
BOT_VERSION = "4.0.3-beta"

# v3.4.21: release notes are split into two surfaces.
#
#  CURRENT_MAIN_NOTE
#    - Just the release that is actively being deployed.
#    - Used by the startup "deployed" card so each deploy shows only
#      what shipped this time (no accumulating carry-over list).
#    - MUST begin with the current BOT_VERSION string and MUST NOT
#      mention any prior version (enforced by smoke test).
#
#  MAIN_RELEASE_NOTE
#    - Rolling history: CURRENT note + previous few versions.
#    - Used by /version (typed and menu) so the history is still
#      available on demand.
#    - The Telegram 34-char mobile-width rule still applies to every
#      line of both surfaces.
CURRENT_MAIN_NOTE = (
    "v4.0.3-beta \u2014 OR seed fix:\n"
    "\u2022 Pull 9:30 ET OR from\n"
    "  Alpaca at boot (no more\n"
    "  stale/round-number OR)\n"
    "\u2022 Staleness guard 1.5%\n"
    "  \u2192 5% (OR_STALE_THRESHOLD)\n"
    "\u2022 or_stale_skip_count in\n"
    "  /api/state per ticker"
)

# Main-bot release note: short tail of recent releases.
_MAIN_HISTORY_TAIL = (
    "v4.0.2-beta \u2014 DI seed at boot:\n"
    "Pull 5m bars from Alpaca at\n"
    "scanner startup (#86); DI\n"
    "gate armed on first scan,\n"
    "not 70 min in.\n"
    "\n"
    "v4.0.1-beta \u2014 UI + gate fixes:\n"
    "dashboard row reorder, Val/\n"
    "Gene tabs mirror Main, shared\n"
    "market-state + per-exec\n"
    "trades, scanner OR latch\n"
    "fix, volume fiction removed,\n"
    "DI exposed as real gate.\n"
    "\n"
    "v4.0.0-beta \u2014 Gene + dashboard:\n"
    "second Alpaca executor Gene\n"
    "mirrors main signals, matches\n"
    "Val semantics. Dashboard now\n"
    "has 3 tabs (Main/Val/Gene)\n"
    "with paper/live badges, an\n"
    "index ticker strip, and the\n"
    "shorts P&L sign fix.\n"
    "\n"
    "v4.0.0-alpha \u2014 Val executor:\n"
    "main emits signals, Val\n"
    "mirrors to Alpaca paper.\n"
    "/mode val paper | live\n"
    "confirm. Strict paper/live\n"
    "segregation. Separate\n"
    "Val Telegram bot. Async\n"
    "fire-and-forget dispatch.\n"
    "\n"
    "v3.6.0 \u2014 Telegram auth guard:\n"
    "every update checked against\n"
    "TRADEGENIUS_OWNER_IDS before\n"
    "any handler fires. Non-owners\n"
    "silently dropped.\n"
    "\n"
    "v3.5.1 \u2014 TradeGenius rename:\n"
    "stock_spike_monitor.py \u2192\n"
    "trade_genius.py. Dashboard,\n"
    "Telegram startup card, and\n"
    "Docker/Railway/nixpacks\n"
    "entry points all updated.\n"
    "\n"
    "v3.5.0 \u2014 Deletion Pass:\n"
    "removed TP webhook, TP book,\n"
    "dual-bot wiring, RH IMAP +\n"
    "Gmail poll, /tp_sync +\n"
    "/rh_* commands. \u22122,110\n"
    "lines in the main file.\n"
    "\n"
    "v3.4.47 \u2014 Eye of the\n"
    "Tiger 2.0: 2-bar OR confirm\n"
    "+ DI+(5m,15) > 25 gate +\n"
    "Hard Eject exit."
)
MAIN_RELEASE_NOTE = CURRENT_MAIN_NOTE + "\n\n" + _MAIN_HISTORY_TAIL
# Backwards-compat alias — any remaining references default to main.
RELEASE_NOTE = MAIN_RELEASE_NOTE

FMP_API_KEY = os.getenv("FMP_API_KEY", "VqYj2Jujrc8IvUOe4CR1g0tRf0qlB4AV")
FINNHUB_TOKEN = os.getenv("FINNHUB_TOKEN", "")

# Human-readable exit reason labels
REASON_LABELS = {
    "STOP": "\U0001f6d1 Hard Stop",
    "TRAIL": "\U0001f3af Trail Stop",
    "RED_CANDLE": "\U0001f56f Red Candle (lost daily polarity)",
    # Long global eject — v3.4.28 Sovereign Regime Shield: SPY AND QQQ
    # 1m finalized close BELOW their PDC. Older labels retained so rows
    # in the persistent trade log from prior versions still render.
    "LORDS_LEFT":      "\U0001f451 Lords Left (SPY+QQQ 1m < PDC)",
    "LORDS_LEFT[1m]":  "\U0001f451 Lords Left (SPY/QQQ < AVWAP)",   # legacy v2.9.8
    "LORDS_LEFT[5m]":  "\U0001f451 Lords Left (SPY+QQQ 5m < AVWAP)",  # legacy v3.2.0–v3.4.27
    "POLARITY_SHIFT": "\U0001f504 Polarity Shift (price > PDC)",
    # Short global eject — v3.4.28 Sovereign Regime Shield mirror.
    "BULL_VACUUM":     "\U0001f300 Bull Vacuum (SPY+QQQ 1m > PDC)",
    "BULL_VACUUM[1m]": "\U0001f300 Bull Vacuum (SPY/QQQ > AVWAP)",  # legacy v2.9.8
    "BULL_VACUUM[5m]": "\U0001f300 Bull Vacuum (SPY+QQQ 5m > AVWAP)",  # legacy v3.2.0–v3.4.27
    "EOD": "\U0001f514 End of Day",
}

# ============================================================
# LOGGING
# ============================================================
LOG_FILE = "trade_genius.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ============================================================
# SIGNAL BUS (v4.0.0-alpha)
# ============================================================
# Main's paper book is the brain; executor bots (TradeGeniusVal, and
# in v4.0.0-beta TradeGeniusGene) subscribe to this bus and mirror
# signals onto Alpaca. Dispatch is async fire-and-forget: each listener
# runs in its own daemon thread so the main loop never blocks on an
# Alpaca round-trip and a single bad listener can't take the bus down.
#
# Event schema (dict):
#   {
#     "kind": "ENTRY_LONG" | "ENTRY_SHORT" | "EXIT_LONG" | "EXIT_SHORT" | "EOD_CLOSE_ALL",
#     "ticker": "AAPL",               # omitted on EOD_CLOSE_ALL
#     "price": 175.42,                # main's reference price
#     "reason": "BREAKOUT" | "STOP" | "TRAIL" | "RED_CANDLE" | ... ,
#     "timestamp_utc": "2026-04-24T13:45:12Z",
#     "main_shares": 57,              # audit-only: shares main paper book traded
#   }
_signal_listeners: list = []


def register_signal_listener(fn):
    """Subscribe a callable fn(event: dict) -> None to the signal bus."""
    _signal_listeners.append(fn)
    logger.info(
        "signal_bus: listener registered (%s) total=%d",
        getattr(fn, "__qualname__", repr(fn)), len(_signal_listeners),
    )


def _emit_signal(event: dict) -> None:
    """Fire an event to every listener in its own daemon thread.

    Async fire-and-forget: main's paper book never blocks on Alpaca.
    Per-listener exceptions are logged but never break the bus.
    """
    # Snapshot the listener list so a concurrent register/unregister can't
    # mutate what we iterate.
    listeners = list(_signal_listeners)
    if not listeners:
        return

    def _wrap(fn, ev):
        try:
            fn(ev)
        except Exception:
            logger.exception(
                "signal_bus: listener %s raised on event %r",
                getattr(fn, "__qualname__", repr(fn)),
                ev.get("kind"),
            )

    for fn in listeners:
        threading.Thread(
            target=_wrap, args=(fn, event), daemon=True,
        ).start()


# ============================================================
# TRADEGENIUS EXECUTOR BASE (v4.0.0-alpha)
# ============================================================
class TradeGeniusBase:
    """Shared base for Alpaca-backed executor bots.

    Subscribes to main's signal bus on startup, manages paper/live mode
    with its own Alpaca client, maintains its own state file, and runs
    its own Telegram bot with its own _auth_guard. Subclasses set NAME
    and ENV_PREFIX \u2014 all behavior lives here.

    Strict paper/live segregation: two state files are kept per bot,
    `tradegenius_{name_lower}_paper.json` and `..._live.json`; a mode
    flip reloads the correct file. A live flip additionally requires an
    explicit `confirm` token AND a sanity check (get_account on the live
    creds must succeed and report ACTIVE).
    """

    NAME = "BASE"        # override: "Val", "Gene"
    ENV_PREFIX = ""      # override: "VAL_", "GENE_"

    def __init__(self):
        p = self.ENV_PREFIX
        self.paper_key = os.getenv(p + "ALPACA_PAPER_KEY", "").strip()
        self.paper_secret = os.getenv(p + "ALPACA_PAPER_SECRET", "").strip()
        self.live_key = os.getenv(p + "ALPACA_LIVE_KEY", "").strip()
        self.live_secret = os.getenv(p + "ALPACA_LIVE_SECRET", "").strip()
        # Per-bot Telegram token env var: VAL_TELEGRAM_TG / GENE_TELEGRAM_TG
        # (matches what's provisioned on Railway). Note: this is distinct from
        # the main TradeGenius bot's TELEGRAM_TOKEN at module scope.
        self.telegram_token = os.getenv(p + "TELEGRAM_TG", "").strip()
        self.telegram_chat_id = os.getenv(p + "TELEGRAM_CHAT_ID", "").strip()
        # Unified owner list: all executor bots share the SAME owner set
        # as main (TRADEGENIUS_OWNER_IDS). One list to maintain on Railway.
        # No per-bot VAL_/GENE_TELEGRAM_OWNER_IDS — intentionally removed.
        self.owner_ids = set(TRADEGENIUS_OWNER_IDS)
        try:
            self.dollars_per_entry = float(
                os.getenv(p + "DOLLARS_PER_ENTRY", "10000")
            )
        except ValueError:
            self.dollars_per_entry = 10000.0
        self.mode = "paper"
        # Client is built lazily on first use so __init__ never touches
        # the network (smoke tests, missing keys, etc.).
        self.client = None
        self._state = {"mode": "paper", "last_updated": None}
        self._load_state()
        # Own Telegram Application instance, created in start().
        self._tg_app = None
        # v4.0.0-beta — last signal seen from the bus (for dashboard).
        # Populated by _on_signal; None until first event arrives.
        self.last_signal: "dict | None" = None

    # ---------- state files ----------
    def _state_file(self, mode: str = None) -> str:
        m = (mode or self.mode).strip().lower()
        return f"tradegenius_{self.NAME.lower()}_{m}.json"

    def _save_state(self) -> None:
        self._state["mode"] = self.mode
        self._state["last_updated"] = datetime.now(timezone.utc).isoformat()
        path = self._state_file()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2)
        except Exception:
            logger.exception("[%s] save state failed (%s)", self.NAME, path)

    def _load_state(self) -> None:
        # First load: if a persisted mode file exists for EITHER mode,
        # prefer the most recently written one so a live-mode restart
        # stays in live. If neither exists, default to paper.
        paper_path = self._state_file("paper")
        live_path = self._state_file("live")
        candidates = []
        for m, p in (("paper", paper_path), ("live", live_path)):
            if os.path.exists(p):
                try:
                    mtime = os.path.getmtime(p)
                except OSError:
                    mtime = 0.0
                candidates.append((mtime, m, p))
        if not candidates:
            return
        candidates.sort(reverse=True)
        _, chosen_mode, chosen_path = candidates[0]
        try:
            with open(chosen_path, "r", encoding="utf-8") as f:
                self._state = json.load(f)
            self.mode = self._state.get("mode", chosen_mode)
        except Exception:
            logger.exception("[%s] load state failed (%s)", self.NAME, chosen_path)

    # ---------- Alpaca client ----------
    def _build_alpaca_client(self, mode: str = None):
        """Return a TradingClient for the requested (or current) mode.

        Uses alpaca-py 0.43.2: `paper=True` routes to paper-api.alpaca.markets,
        `paper=False` to api.alpaca.markets. Optional env var overrides
        ALPACA_ENDPOINT_PAPER / ALPACA_ENDPOINT_TRADE are passed via
        url_override when set.

        IMPORTANT: alpaca-py's RESTClient builds the final URL as
            base_url + "/" + api_version + path
        i.e. it ALWAYS appends "/v2". So url_override must be the HOST
        (e.g. https://paper-api.alpaca.markets) and must NOT already
        include a trailing /v2. We defensively strip any trailing "/v2"
        or "/v2/" so a misconfigured Railway env var can't cause
        double-prefixed URLs (https://.../v2/v2/account -> 404).
        """
        from alpaca.trading.client import TradingClient  # lazy import
        m = (mode or self.mode).strip().lower()
        if m == "live":
            key, secret = self.live_key, self.live_secret
            url_override = os.getenv("ALPACA_ENDPOINT_TRADE", "").strip() or None
            paper = False
        else:
            key, secret = self.paper_key, self.paper_secret
            url_override = os.getenv("ALPACA_ENDPOINT_PAPER", "").strip() or None
            paper = True
        if url_override:
            # Strip any trailing /v2 or /v2/ the user may have included
            cleaned = url_override.rstrip("/")
            if cleaned.endswith("/v2"):
                cleaned = cleaned[:-3]
            url_override = cleaned or None
        kwargs = {"paper": paper}
        if url_override:
            kwargs["url_override"] = url_override
        return TradingClient(key, secret, **kwargs)

    def _ensure_client(self):
        if self.client is None:
            try:
                self.client = self._build_alpaca_client()
            except Exception:
                logger.exception("[%s] alpaca client build failed", self.NAME)
                self.client = None
        return self.client

    # ---------- sanity check before live flip ----------
    def _live_sanity_check(self):
        """Build a TEMP live client, verify it resolves to a non-paper,
        ACTIVE account, log account_number/cash/buying_power.

        Returns (ok: bool, message: str).
        """
        if not (self.live_key and self.live_secret):
            return (False, "live keys not set in env")
        try:
            tmp = self._build_alpaca_client(mode="live")
            acct = tmp.get_account()
        except Exception as e:
            logger.exception("[%s] live sanity check failed", self.NAME)
            return (False, f"get_account raised: {e}")
        status = str(getattr(acct, "status", "")).upper()
        account_number = getattr(acct, "account_number", "?")
        cash = getattr(acct, "cash", "?")
        buying_power = getattr(acct, "buying_power", "?")
        logger.info(
            "[%s] live sanity: account=%s status=%s cash=%s bp=%s",
            self.NAME, account_number, status, cash, buying_power,
        )
        if "ACTIVE" not in status:
            return (False, f"account not ACTIVE (status={status})")
        return (
            True,
            f"live OK \u2014 acct={account_number} status={status} "
            f"cash={cash} bp={buying_power}",
        )

    # ---------- mode control ----------
    def set_mode(self, new_mode: str, confirm_token: str = None):
        """Flip paper/live. Live requires confirm_token=='confirm' AND
        _live_sanity_check. Returns (ok, message)."""
        nm = (new_mode or "").strip().lower()
        if nm == "paper":
            self.mode = "paper"
            try:
                self.client = self._build_alpaca_client()
            except Exception:
                logger.exception("[%s] rebuild paper client failed", self.NAME)
                self.client = None
            self._save_state()
            return (True, "mode set to paper")
        if nm == "live":
            if confirm_token != "confirm":
                return (
                    False,
                    "live flip requires the literal 'confirm' token: "
                    "/mode val live confirm",
                )
            ok, msg = self._live_sanity_check()
            if not ok:
                return (False, f"live sanity failed: {msg}")
            self.mode = "live"
            try:
                self.client = self._build_alpaca_client()
            except Exception as e:
                logger.exception("[%s] rebuild live client failed", self.NAME)
                return (False, f"client rebuild after sanity failed: {e}")
            self._save_state()
            return (True, f"mode set to live \u2014 {msg}")
        return (False, f"unknown mode: {new_mode!r} (expected 'paper' or 'live')")

    # ---------- signal listener ----------
    def _shares_for(self, price: float) -> int:
        if price is None or price <= 0:
            return 0
        return max(1, int(self.dollars_per_entry // price))

    def _send_own_telegram(self, text: str) -> None:
        """Post to this executor's OWN Telegram chat (not main's)."""
        if not (self.telegram_token and self.telegram_chat_id):
            return
        try:
            import urllib.parse
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id": self.telegram_chat_id,
                "text": text,
            }).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST")
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            logger.exception("[%s] telegram send failed", self.NAME)

    def _on_signal(self, event: dict) -> None:
        """Listener callback: dispatch on event['kind']."""
        kind = event.get("kind", "")
        ticker = event.get("ticker", "")
        price = event.get("price", 0.0) or 0.0
        reason = event.get("reason", "")
        label = f"{self.NAME} {self.mode}"

        # v4.0.0-beta — remember the most recent event for the dashboard
        # (last_signal line on the per-executor tab). Captured before any
        # dispatch so we still record what was seen even if Alpaca errors.
        try:
            self.last_signal = {
                "kind": kind,
                "ticker": ticker,
                "price": float(price) if price else 0.0,
                "reason": reason,
                "timestamp_utc": event.get("timestamp_utc", _utc_now_iso()),
            }
        except Exception:
            pass

        client = self._ensure_client()
        if client is None:
            logger.warning(
                "[%s] skip %s %s \u2014 no alpaca client", self.NAME, kind, ticker,
            )
            return

        try:
            from alpaca.trading.requests import MarketOrderRequest, ClosePositionRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
        except Exception:
            logger.exception("[%s] alpaca imports failed", self.NAME)
            return

        try:
            if kind == "ENTRY_LONG":
                qty = self._shares_for(price)
                if qty <= 0:
                    return
                order = client.submit_order(MarketOrderRequest(
                    symbol=ticker, qty=qty,
                    side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                ))
                oid = getattr(order, "id", "?")
                msg = f"\u2705 {label}: {ticker} BUY {qty} shares @ market (order_id={oid})"
                logger.info(msg)
                self._send_own_telegram(msg)
            elif kind == "ENTRY_SHORT":
                qty = self._shares_for(price)
                if qty <= 0:
                    return
                order = client.submit_order(MarketOrderRequest(
                    symbol=ticker, qty=qty,
                    side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                ))
                oid = getattr(order, "id", "?")
                msg = f"\u2705 {label}: {ticker} SELL {qty} shares short @ market (order_id={oid})"
                logger.info(msg)
                self._send_own_telegram(msg)
            elif kind in ("EXIT_LONG", "EXIT_SHORT"):
                client.close_position(ticker)
                msg = f"\u2705 {label}: {ticker} CLOSE ({reason})"
                logger.info(msg)
                self._send_own_telegram(msg)
            elif kind == "EOD_CLOSE_ALL":
                client.close_all_positions(cancel_orders=True)
                msg = f"\u2705 {label}: EOD close_all_positions"
                logger.info(msg)
                self._send_own_telegram(msg)
            else:
                logger.warning("[%s] unknown signal kind %r", self.NAME, kind)
        except Exception as e:
            err = f"\u274c {label}: {ticker or kind} failed: {e}"
            logger.exception("[%s] dispatch failed on %s", self.NAME, kind)
            self._send_own_telegram(err)

    # ---------- own Telegram bot ----------
    async def _auth_guard(self, update, context):
        """Owner-whitelist guard identical in pattern to main's guard."""
        eff_user = getattr(update, "effective_user", None)
        uid = str(eff_user.id) if eff_user and getattr(eff_user, "id", None) is not None else ""
        if uid and uid in self.owner_ids:
            return
        logger.warning(
            "[%s] auth_guard dropped non-owner (user_id=%r)", self.NAME, uid or "(none)",
        )
        raise ApplicationHandlerStop

    async def cmd_mode(self, update, context):
        """/mode paper  |  /mode live confirm"""
        args = context.args if context and hasattr(context, "args") else []
        if not args:
            await update.message.reply_text(
                f"{self.NAME} mode: {self.mode}\n"
                f"Usage: /mode paper  |  /mode live confirm"
            )
            return
        new_mode = args[0]
        token = args[1] if len(args) > 1 else None
        ok, msg = self.set_mode(new_mode, confirm_token=token)
        marker = "\u2705" if ok else "\u274c"
        await update.message.reply_text(f"{marker} {self.NAME}: {msg}")

    async def cmd_status(self, update, context):
        client = self._ensure_client()
        lines = [f"{self.NAME} status", f"  mode: {self.mode}"]
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
                lines.append(f"  bp:   {getattr(acct, 'buying_power', '?')}")
                try:
                    positions = client.get_all_positions()
                    lines.append(f"  positions: {len(positions)}")
                    for p in positions[:10]:
                        lines.append(
                            f"    {getattr(p, 'symbol', '?')}: "
                            f"{getattr(p, 'qty', '?')} @ {getattr(p, 'avg_entry_price', '?')}"
                        )
                except Exception as e:
                    lines.append(f"  positions: (fetch failed: {e})")
            except Exception as e:
                lines.append(f"  alpaca error: {e}")
        await update.message.reply_text("\n".join(lines))

    async def cmd_halt(self, update, context):
        """Emergency close_all_positions."""
        client = self._ensure_client()
        if client is None:
            await update.message.reply_text(
                f"\u274c {self.NAME}: no alpaca client"
            )
            return
        try:
            client.close_all_positions(cancel_orders=True)
            await update.message.reply_text(
                f"\u2705 {self.NAME}: HALT \u2014 close_all_positions fired"
            )
        except Exception as e:
            await update.message.reply_text(
                f"\u274c {self.NAME}: halt failed: {e}"
            )

    async def cmd_version(self, update, context):
        await update.message.reply_text(
            f"{self.NAME} executor v{BOT_VERSION}\n"
            f"mode: {self.mode}"
        )

    # --- v4.0.1: expanded executor-bot command surface --------------

    async def cmd_ping(self, update, context):
        """/ping — liveness check (bot up + client reachable)."""
        client = self._ensure_client()
        alpaca_ok = client is not None
        await update.message.reply_text(
            f"\U0001f3d3 {self.NAME}: pong\n"
            f"  version: v{BOT_VERSION}\n"
            f"  mode: {self.mode}\n"
            f"  alpaca client: {'ok' if alpaca_ok else 'missing'}"
        )

    async def cmd_cash(self, update, context):
        """/cash — quick account balance glance."""
        client = self._ensure_client()
        if client is None:
            await update.message.reply_text(
                f"\u274c {self.NAME}: no alpaca client"
            )
            return
        try:
            acct = client.get_account()
            cash = float(getattr(acct, "cash", 0) or 0)
            bp   = float(getattr(acct, "buying_power", 0) or 0)
            eq   = float(getattr(acct, "equity", 0) or 0)
            await update.message.reply_text(
                f"\U0001f4b0 {self.NAME} ({self.mode})\n"
                f"  cash:   ${cash:,.2f}\n"
                f"  equity: ${eq:,.2f}\n"
                f"  bp:     ${bp:,.2f}"
            )
        except Exception as e:
            await update.message.reply_text(
                f"\u274c {self.NAME}: cash fetch failed: {e}"
            )

    async def cmd_positions(self, update, context):
        """/positions — compact positions list only."""
        client = self._ensure_client()
        if client is None:
            await update.message.reply_text(
                f"\u274c {self.NAME}: no alpaca client"
            )
            return
        try:
            positions = client.get_all_positions()
            if not positions:
                await update.message.reply_text(
                    f"{self.NAME}: no open positions"
                )
                return
            lines = [f"{self.NAME} positions ({len(positions)})"]
            for p in positions[:25]:
                sym = getattr(p, "symbol", "?")
                qty = getattr(p, "qty", "?")
                avg = getattr(p, "avg_entry_price", "?")
                try:
                    upl = float(getattr(p, "unrealized_pl", 0) or 0)
                    pct = float(getattr(p, "unrealized_plpc", 0) or 0) * 100
                    lines.append(
                        f"  {sym}: {qty} @ {avg} "
                        f"pnl=${upl:+,.2f} ({pct:+.2f}%)"
                    )
                except Exception:
                    lines.append(f"  {sym}: {qty} @ {avg}")
            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(
                f"\u274c {self.NAME}: positions fetch failed: {e}"
            )

    async def cmd_orders(self, update, context):
        """/orders — recent orders (last 10)."""
        client = self._ensure_client()
        if client is None:
            await update.message.reply_text(
                f"\u274c {self.NAME}: no alpaca client"
            )
            return
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            req = GetOrdersRequest(
                status=QueryOrderStatus.ALL, limit=10,
            )
            orders = client.get_orders(filter=req)
            if not orders:
                await update.message.reply_text(
                    f"{self.NAME}: no recent orders"
                )
                return
            lines = [f"{self.NAME} recent orders ({len(orders)})"]
            for o in orders:
                sym   = getattr(o, "symbol", "?")
                side  = getattr(getattr(o, "side", None), "value", "?")
                qty   = getattr(o, "qty", "?") or getattr(o, "notional", "?")
                stat  = getattr(getattr(o, "status", None), "value", "?")
                filled = getattr(o, "filled_avg_price", None)
                tail = f" @ {filled}" if filled else ""
                lines.append(f"  {sym} {side} {qty} [{stat}]{tail}")
            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(
                f"\u274c {self.NAME}: orders fetch failed: {e}"
            )

    async def cmd_signal(self, update, context):
        """/signal — show last signal received from main's bus."""
        sig = self.last_signal
        if not sig:
            await update.message.reply_text(
                f"{self.NAME}: no signals received yet"
            )
            return
        try:
            import json as _json
            pretty = _json.dumps(sig, indent=2, default=str)[:1500]
        except Exception:
            pretty = str(sig)[:1500]
        await update.message.reply_text(
            f"{self.NAME} last signal:\n{pretty}"
        )

    # -----------------------------------------------------------------

    # Commands shown in Telegram's BotFather / slash menu. Keep short
    # descriptions — Telegram truncates aggressively on mobile.
    TG_MENU_COMMANDS = [
        ("status",    "Account, positions, and P&L"),
        ("positions", "Open positions only"),
        ("orders",    "Recent orders (last 10)"),
        ("cash",      "Account balance snapshot"),
        ("signal",    "Last signal from main"),
        ("mode",      "Show or change mode (paper / live)"),
        ("halt",      "Emergency halt \u2014 flatten all"),
        ("ping",      "Liveness check"),
        ("version",   "Show running version"),
        ("help",      "List available commands"),
    ]

    async def cmd_help(self, update, context):
        """/help — list all available commands for this bot."""
        lines = [f"{self.NAME} commands:"]
        for cmd, desc in self.TG_MENU_COMMANDS:
            lines.append(f"/{cmd} \u2014 {desc}")
        await update.message.reply_text("\n".join(lines))

    async def _post_init_register_menu(self, app):
        """PTB post_init hook: register slash-command menu with Telegram
        so commands show up in the in-chat `/` picker automatically —
        no manual BotFather /setcommands needed."""
        try:
            cmds = [BotCommand(c, d) for c, d in self.TG_MENU_COMMANDS]
            await app.bot.set_my_commands(
                cmds, scope=BotCommandScopeAllPrivateChats()
            )
            logger.info("[%s] registered %d telegram menu commands",
                        self.NAME, len(cmds))
        except Exception:
            logger.exception("[%s] set_my_commands failed", self.NAME)

    async def _tg_main(self):
        """Async entry point for the executor's Telegram bot. Uses the
        low-level Application lifecycle (initialize/start/updater.start_polling)
        instead of app.run_polling() — because run_polling() tries to
        install OS signal handlers via loop.add_signal_handler(), which
        Python disallows outside the main thread (set_wakeup_fd only
        works in main thread of the main interpreter). Executor bots
        run on their own background threads, so we must drive the
        Application lifecycle manually."""
        # NOTE: we intentionally do NOT use .post_init() here — that hook
        # is only fired by Application.run_polling() / run_webhook(), and
        # we drive the lifecycle manually (initialize/start/updater) to
        # avoid the set_wakeup_fd main-thread restriction. Instead we
        # call _post_init_register_menu directly after initialize() below.
        app = (
            Application.builder()
            .token(self.telegram_token)
            .build()
        )
        self._tg_app = app
        app.add_handler(TypeHandler(Update, self._auth_guard), group=-1)
        app.add_handler(CommandHandler("mode", self.cmd_mode))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("positions", self.cmd_positions))
        app.add_handler(CommandHandler("orders", self.cmd_orders))
        app.add_handler(CommandHandler("cash", self.cmd_cash))
        app.add_handler(CommandHandler("signal", self.cmd_signal))
        app.add_handler(CommandHandler("halt", self.cmd_halt))
        app.add_handler(CommandHandler("ping", self.cmd_ping))
        app.add_handler(CommandHandler("version", self.cmd_version))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("start", self.cmd_help))
        await app.initialize()
        # Register the slash-command menu with Telegram now that the
        # Bot instance is usable (post app.initialize()). Failures are
        # logged inside the helper and never block startup.
        await self._post_init_register_menu(app)
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("[%s] telegram loop running (token=...%s)",
                    self.NAME, self.telegram_token[-6:])
        # Park forever — updater polls in its own task. Exits only when
        # the thread/process is torn down.
        try:
            await asyncio.Event().wait()
        finally:
            try:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
            except Exception:
                pass

    def _run_tg_loop(self):
        """Run this executor's own Telegram polling loop in its own
        thread. Creates its own asyncio event loop (PTB requires one)."""
        if not self.telegram_token:
            logger.info("[%s] telegram token unset \u2014 skipping tg loop", self.NAME)
            return
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            logger.info("[%s] telegram loop starting", self.NAME)
            loop.run_until_complete(self._tg_main())
        except Exception:
            logger.exception("[%s] telegram loop crashed", self.NAME)

    # ---------- startup ----------
    def start(self):
        """Subscribe to main's signal bus and start own Telegram loop."""
        register_signal_listener(self._on_signal)
        # Try to build the alpaca client eagerly so startup logs surface
        # missing/bad creds; failures are already caught + logged.
        self._ensure_client()
        logger.info("[%s] started in %s mode", self.NAME, self.mode)
        # Own Telegram bot in a background thread so main.run_telegram_bot()
        # can still own the main-process asyncio loop.
        t = threading.Thread(target=self._run_tg_loop, daemon=True, name=f"{self.NAME}_tg")
        t.start()


class TradeGeniusVal(TradeGeniusBase):
    """Val \u2014 first Genius executor. Alpaca paper by default; Val flips
    to live via `/mode live confirm` on Val's own Telegram bot, or via
    `/mode val live confirm` on main's bot."""
    NAME = "Val"
    ENV_PREFIX = "VAL_"


class TradeGeniusGene(TradeGeniusBase):
    """Gene \u2014 second Genius executor, identical in behavior to Val but
    with its own GENE_ env prefix, state files, and Telegram bot. Shipped
    in v4.0.0-beta alongside the 3-tab dashboard."""
    NAME = "Gene"
    ENV_PREFIX = "GENE_"


# Global executor instances (populated at startup if enabled). Referenced
# by main-bot's /mode {val,gene} router; left None when disabled / no keys.
val_executor: "TradeGeniusBase | None" = None
gene_executor: "TradeGeniusBase | None" = None


ET = ZoneInfo("America/New_York")
CDT = ZoneInfo("America/Chicago")   # user display timezone


def _now_et() -> datetime:
    """Current time in ET — for market-hour gate logic only."""
    return datetime.now(timezone.utc).astimezone(ET)


def _now_cdt() -> datetime:
    """Current time in CDT — for all user-facing display."""
    return datetime.now(timezone.utc).astimezone(CDT)


def _utc_now_iso() -> str:
    """UTC ISO timestamp string for internal storage."""
    return datetime.now(timezone.utc).isoformat()


def _to_cdt_hhmm(iso_str: str) -> str:
    """Decode a stored ISO timestamp to 'HH:MM CDT' for display.
    Handles both UTC-stored (new) and ET-stored (legacy) strings."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)   # legacy ET-stored fallback
        return dt.astimezone(CDT).strftime("%H:%M CDT")
    except Exception:
        return iso_str


def _to_cdt_hhmmss(iso_str: str) -> str:
    """Decode a stored ISO timestamp to 'HH:MM:SS' (CDT) for display."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)
        return dt.astimezone(CDT).strftime("%H:%M:%S")
    except Exception:
        return iso_str


def _parse_time_to_cdt(ts):
    """Normalise any stored timestamp format to HH:MM CDT."""
    if not ts:
        return "??:??"
    ts = str(ts).strip()
    # ISO format with timezone offset (stored as UTC)
    if "T" in ts and ("+" in ts or ts.endswith("Z")):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            cdt_dt = dt.astimezone(CDT)
            return cdt_dt.strftime("%H:%M")
        except Exception:
            pass
    # HH:MM:SS or HH:MM — already local (CDT), just truncate
    parts = ts.split(":")
    if len(parts) >= 2:
        return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}"
    return ts[:5]


def _is_today(ts_str: str) -> bool:
    """Check if an ISO timestamp string is from today (ET-based)."""
    if not ts_str:
        return False
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        today_et = _now_et().date()
        return dt.astimezone(ET).date() == today_et
    except Exception:
        return False


# ── Matplotlib (optional — graceful skip if not installed) ──────────────
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import io as _io
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

# Pre-warm matplotlib font manager in background thread so first chart
# call doesn't block the event loop for ~30-50 seconds.
if MATPLOTLIB_AVAILABLE:
    def _warm_matplotlib():
        try:
            fig, ax = plt.subplots()
            plt.close(fig)
        except Exception:
            pass
    threading.Thread(target=_warm_matplotlib, daemon=True).start()


def _parse_date_arg(args):
    """Parse optional date argument from command args. Returns date in ET."""
    import datetime as _dt
    today = _now_et().date()
    if not args:
        return today
    raw = " ".join(args).strip().lower()
    if raw == "yesterday":
        d = today - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d
    # Try YYYY-MM-DD
    try:
        return _dt.date.fromisoformat(raw)
    except ValueError:
        pass
    # Try integer = last N days (for /perf)
    try:
        n = int(raw)
        if 1 <= n <= 365:
            return today - timedelta(days=n)
    except ValueError:
        pass
    # Try "Apr 17" or "April 17"
    for fmt in ["%b %d", "%B %d"]:
        try:
            parsed = _dt.datetime.strptime(raw, fmt)
            return parsed.replace(year=today.year).date()
        except ValueError:
            pass
    # Try weekday names
    days_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    for abbr, num in days_map.items():
        if raw.startswith(abbr):
            delta = (today.weekday() - num) % 7
            if delta == 0:
                delta = 7
            return today - timedelta(days=delta)
    return today  # fallback


# Short reason labels for compact /dayreport display
_SHORT_REASON = {
    "\U0001f6d1": "\U0001f6d1 Stop",
    "\U0001f512": "\U0001f512 Trail",
    "\U0001f56f": "\U0001f56f Red Candle",
    "\U0001f451": "\U0001f451 Lords Left",
    "\U0001f504": "\U0001f504 Polarity Shift",
    "\U0001f300": "\U0001f300 Bull Vacuum",
    "\U0001f4c9": "\U0001f4c9 PDC Break",
    "\U0001f514": "\U0001f514 EOD",
}


# ============================================================
# PAPER TRADING CONFIG
# ============================================================
PAPER_LOG              = os.getenv("PAPER_LOG_PATH", "investment.log")
PAPER_STATE_FILE       = os.getenv("PAPER_STATE_PATH", "paper_state.json")
# v3.4.27 — persistent trade log. Default path is a sibling of the
# paper state file so it lands on the same volume automatically. The
# file is append-only JSONL — one closed trade per line. Survives
# redeploys when written to the mounted volume.
TRADE_LOG_FILE         = os.getenv(
    "TRADE_LOG_PATH",
    os.path.join(os.path.dirname(PAPER_STATE_FILE) or ".", "trade_log.jsonl"),
)
PAPER_STARTING_CAPITAL = 100_000.0

# Investment logger (separate file)
inv_logger = logging.getLogger("investment")
inv_logger.setLevel(logging.INFO)
_inv_fh = logging.FileHandler(PAPER_LOG, encoding="utf-8")
_inv_fh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
inv_logger.addHandler(_inv_fh)
inv_logger.propagate = False


def paper_log(msg: str):
    """Write a timestamped line to investment.log and the main logger."""
    inv_logger.info(msg)
    logger.info("[PAPER] %s", msg)


# ============================================================
# STRATEGY CONSTANTS
# ============================================================
# ------------------------------------------------------------
# v3.4.32: the ticker universe is now editable at runtime from
# Telegram via /add_ticker, /remove_ticker, /tickers. The list
# is persisted to TICKERS_FILE so edits survive restarts.
#
# DESIGN NOTES
#   - TICKERS and TRADE_TICKERS stay as module-level mutable
#     lists so every `for t in TICKERS` loop picks up changes
#     without plumbing a getter through ~25 call sites.
#   - SPY and QQQ are PINNED — they drive the Sovereign Regime
#     shield and the RSI regime classifier. They can be added
#     by the defaults but can never be removed via /remove.
#   - TRADE_TICKERS is kept in sync via _rebuild_trade_tickers()
#     which clears the list in place and re-extends from the
#     current TICKERS minus the pinned set.
#   - Persistence is fail-soft: if the JSON is missing, unreadable,
#     or empty, we fall back to TICKERS_DEFAULT. Callers never see
#     an exception.
#   - QBTS is included in the defaults so a fresh deploy (no
#     tickers.json yet) already tracks it.
# ------------------------------------------------------------
TICKERS_FILE = os.getenv("TICKERS_FILE", "tickers.json")
TICKERS_PINNED = ("SPY", "QQQ")   # always present, never removable
TICKERS_DEFAULT = [
    "AAPL", "MSFT", "NVDA", "TSLA", "META",
    "GOOG", "AMZN", "AVGO", "QBTS", "SPY", "QQQ",
]
TICKERS_MAX = 40            # sanity upper bound to protect cycle budget
TICKER_SYM_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,7}$")

TICKERS = list(TICKERS_DEFAULT)
TRADE_TICKERS = [t for t in TICKERS if t not in TICKERS_PINNED]


def _normalise_ticker(sym) -> str:
    """Uppercase + strip the common '$' / whitespace noise.
    Returns '' for anything that doesn't pass the symbol regex."""
    if not sym:
        return ""
    s = str(sym).strip().lstrip("$").upper()
    return s if TICKER_SYM_RE.match(s) else ""


def _rebuild_trade_tickers() -> None:
    """Sync TRADE_TICKERS with TICKERS — in place.
    Must run after every mutation of TICKERS so the scan loop,
    RSI regime classifier, and dashboard snapshot see the same
    tradable set.
    """
    TRADE_TICKERS.clear()
    for t in TICKERS:
        if t not in TICKERS_PINNED:
            TRADE_TICKERS.append(t)


def _load_tickers_file() -> list:
    """Read TICKERS_FILE and return a normalised, de-duplicated,
    order-preserving list. Fail-soft — any error returns [].
    """
    try:
        if not os.path.exists(TICKERS_FILE):
            return []
        with open(TICKERS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        items = raw.get("tickers") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return []
        seen, out = set(), []
        for sym in items:
            s = _normalise_ticker(sym)
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out
    except Exception as e:
        logger.warning("tickers.json load failed, using defaults: %s", e)
        return []


def _save_tickers_file() -> bool:
    """Atomically persist the current TICKERS list. Returns True on
    success. Uses a tmp+rename so a crash mid-write can never leave
    a half-written file.
    """
    try:
        payload = {
            "tickers": list(TICKERS),
            "updated_utc": _utc_now_iso(),
            "bot_version": BOT_VERSION,
        }
        tmp = TICKERS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=False)
        os.replace(tmp, TICKERS_FILE)
        return True
    except Exception as e:
        logger.error("tickers.json save failed: %s", e)
        return False


def _init_tickers() -> None:
    """Populate TICKERS from disk on startup; fall back to defaults
    (which include QBTS and the pinned SPY/QQQ). Always ensures the
    pinned symbols are present no matter what was on disk.
    """
    from_disk = _load_tickers_file()
    base = from_disk if from_disk else list(TICKERS_DEFAULT)
    # Ensure pinned symbols are always in the set.
    for p in TICKERS_PINNED:
        if p not in base:
            base.append(p)
    # Cap at TICKERS_MAX just in case a hand-edited file went wild.
    base = base[:TICKERS_MAX]
    TICKERS.clear()
    TICKERS.extend(base)
    _rebuild_trade_tickers()
    # If the file didn't exist or was empty, persist the seeded
    # defaults so the on-disk list matches memory immediately.
    if not from_disk:
        _save_tickers_file()
    logger.info("Ticker universe loaded: %d tickers (%s)",
                len(TICKERS), ", ".join(TICKERS))


def _fill_metrics_for_ticker(ticker: str) -> dict:
    """Populate every metric a newly-added ticker needs so the very
    next scan cycle can evaluate it without cold-starting any data.

    v3.4.33: thorough fill — primes PDC (dual source), OR high/low
    (post-09:35 ET), a warm-up RSI snapshot, and a liveness probe on
    1-minute bars. Returns a dict describing what was filled; the
    caller uses this to tell the user exactly what is ready and what
    is still pending.

    Keys in the returned dict:
      bars    : bool  — 1-minute bars are reachable for this symbol
      pdc     : bool  — previous-day close cached in pdc[ticker]
      pdc_src : str   — 'fmp' | 'bars' | 'none'
      or      : bool  — opening range populated (high and low)
      or_pending : bool — we're pre-09:35 ET; collect_or() will fill
      rsi     : bool  — RSI warm-up value computed (not cached, just
                        proves the bar history is long enough)
      rsi_val : float | None — the warm-up value, for display only
      errors  : list[str]    — human-readable problems, truncated
                               to short phrases by the caller
    """
    filled = {
        "bars": False,
        "pdc": False, "pdc_src": "none",
        "or": False, "or_pending": False,
        "rsi": False, "rsi_val": None,
        "errors": [],
    }
    now_et = _now_et()
    or_window_end = now_et.replace(hour=9, minute=35,
                                   second=0, microsecond=0)
    past_or_window = now_et >= or_window_end

    # 1) PDC via FMP quote — works any time of day, including pre-open.
    try:
        q = get_fmp_quote(ticker)
        if q and q.get("previousClose"):
            pdc[ticker] = float(q["previousClose"])
            filled["pdc"] = True
            filled["pdc_src"] = "fmp"
        else:
            filled["errors"].append("no PDC from FMP")
    except Exception as e:
        filled["errors"].append("FMP error: %s" % str(e)[:40])
        logger.warning("fill_metrics FMP %s failed: %s", ticker, e)

    # 2) Bars liveness probe + OR fill (if past 09:35) + RSI warm-up
    #    + PDC fallback (if FMP missed it). All three piggy-back on
    #    the same fetch so we only hit the data provider once.
    try:
        bars = fetch_1min_bars(ticker)
        if bars and bars.get("timestamps"):
            filled["bars"] = True

            # PDC fallback from bars snapshot.
            if not filled["pdc"] and bars.get("pdc"):
                pdc[ticker] = float(bars["pdc"])
                filled["pdc"] = True
                filled["pdc_src"] = "bars"

            # OR fill — only if we're past 09:35 ET.
            if past_or_window:
                open_ts = int(or_window_end.replace(hour=9, minute=30)
                              .timestamp())
                end_ts = int(or_window_end.timestamp())
                max_hi, min_lo = None, None
                for i, ts in enumerate(bars["timestamps"]):
                    if open_ts <= ts < end_ts:
                        h = bars["highs"][i] or bars["closes"][i]
                        lo = bars["lows"][i] or bars["closes"][i]
                        if h is not None:
                            max_hi = h if max_hi is None else max(max_hi, h)
                        if lo is not None:
                            min_lo = lo if min_lo is None else min(min_lo, lo)
                if max_hi is not None and min_lo is not None:
                    or_high[ticker] = max_hi
                    or_low[ticker] = min_lo
                    filled["or"] = True
                elif max_hi is not None:
                    or_high[ticker] = max_hi
                    filled["errors"].append("OR low missing")
                else:
                    filled["errors"].append(
                        "no bars in 09:30\u201309:35")
            else:
                # Pre-09:35 is not an error — explicitly flag pending.
                filled["or_pending"] = True

            # RSI warm-up — compute from available closes. This doesn't
            # cache anything (the scanner recomputes each cycle from
            # live bars), but it proves the bar history is deep enough
            # for _compute_rsi to return a real number on the next scan.
            closes = [c for c in (bars.get("closes") or []) if c is not None]
            if len(closes) >= RSI_PERIOD + 1:
                try:
                    rsi_val = _compute_rsi(closes)
                    if rsi_val is not None:
                        filled["rsi"] = True
                        filled["rsi_val"] = float(rsi_val)
                except Exception as e:
                    filled["errors"].append(
                        "RSI warm-up: %s" % str(e)[:30])
            else:
                filled["errors"].append(
                    "RSI needs %d closes" % (RSI_PERIOD + 1))
        else:
            filled["errors"].append("no 1m bars")
    except Exception as e:
        filled["errors"].append("bars error: %s" % str(e)[:40])
        logger.warning("fill_metrics bars %s failed: %s", ticker, e)

    return filled


def add_ticker(sym: str) -> dict:
    """Add a ticker to the live universe. Idempotent.

    Returns {ok, ticker, added, reason, metrics} where:
      - ok=False + reason=...   on validation failure
      - ok=True + added=False   if already present (no-op)
      - ok=True + added=True    on a fresh add (file saved, metrics filled)
    """
    t = _normalise_ticker(sym)
    if not t:
        return {"ok": False, "reason": "invalid symbol", "ticker": sym}
    if t in TICKERS:
        return {"ok": True, "added": False, "ticker": t,
                "reason": "already tracked"}
    if len(TICKERS) >= TICKERS_MAX:
        return {"ok": False, "ticker": t,
                "reason": "at max (%d) \u2014 remove one first" % TICKERS_MAX}
    TICKERS.append(t)
    _rebuild_trade_tickers()
    _save_tickers_file()
    metrics = _fill_metrics_for_ticker(t)
    logger.info("ticker added: %s (pdc=%s or=%s)",
                t, metrics["pdc"], metrics["or"])
    return {"ok": True, "added": True, "ticker": t, "metrics": metrics}


def remove_ticker(sym: str) -> dict:
    """Remove a ticker from the live universe. Idempotent.

    Pinned tickers (SPY, QQQ) are always refused.
    Open positions on the removed ticker keep managing until they
    close — this only stops new entries from being opened.
    """
    t = _normalise_ticker(sym)
    if not t:
        return {"ok": False, "reason": "invalid symbol", "ticker": sym}
    if t in TICKERS_PINNED:
        return {"ok": False, "ticker": t,
                "reason": "%s is pinned (regime anchor)" % t}
    if t not in TICKERS:
        return {"ok": True, "removed": False, "ticker": t,
                "reason": "not tracked"}
    TICKERS.remove(t)
    _rebuild_trade_tickers()
    _save_tickers_file()
    # Leave or_high/or_low/pdc entries behind — any still-open
    # position on this ticker relies on them to manage exits.
    logger.info("ticker removed: %s", t)
    open_long = t in positions
    open_short = t in short_positions
    return {"ok": True, "removed": True, "ticker": t,
            "had_open": bool(open_long or open_short)}

# v3.4.45 — paper sizing is now dollar-based like RH. SHARES is kept
# as a legacy fallback only (used when price is unavailable in test
# paths). Production entries call paper_shares_for(price) instead.
SHARES         = 10
PAPER_DOLLARS_PER_ENTRY = float(os.getenv("PAPER_DOLLARS_PER_ENTRY", "10000"))
STOP_OFFSET    = 0.50    # Initial stop: entry - $0.50
# Trail: +1.0% trigger, max(price*1.0%, $1.00) distance — see manage_positions()
TRAIL_TRIGGER  = 1.00    # Legacy constant (unused — trail is now percentage-based)
TRAIL_STEP     = 0.50    # Legacy constant (unused — trail is now percentage-based)

SCAN_INTERVAL  = 60      # seconds between scans
YAHOO_TIMEOUT  = 8       # seconds
YAHOO_HEADERS  = {"User-Agent": "Mozilla/5.0"}

# v3.4.47 — Eye of the Tiger 2.0 protocol configuration
TIGER_V2_DI_THRESHOLD = float(os.getenv("TIGER_V2_DI_THRESHOLD", "25"))
TIGER_V2_REQUIRE_VOL  = os.getenv("TIGER_V2_REQUIRE_VOL", "false").lower() in ("1", "true", "yes")

# ============================================================
# GLOBAL STATE
# ============================================================

# OR data — populated at 09:35 ET
or_high: dict = {}                  # ticker -> OR high price
or_low: dict = {}                   # ticker -> OR low price (Wounded Buffalo)
pdc: dict = {}                      # ticker -> previous day close
or_collected_date: str = ""         # date string, prevents re-collection
# v4.0.3-beta — per-ticker counter of OR staleness SKIPs this session.
# Exposed in /api/state so silent "OR vs live drift" failures are
# visible without tailing Railway logs.
or_stale_skip_count: dict = {}      # ticker -> int

# AVWAP state — REMOVED in v3.4.34. All AVWAP code paths (entry
# gates, regime alert, breadth observer) were migrated to PDC to
# match the v3.4.28 Sovereign Regime Shield ejector. Persisted
# state keys ("avwap_data", "avwap_last_ts") are silently ignored
# by load_paper_state for backwards compatibility with pre-v3.4.34
# state files — no migration required.

# Positions
positions: dict = {}
# positions[ticker] = {
#   "entry_price": float,
#   "shares": int,           # always 10
#   "stop": float,           # current stop price
#   "trail_active": bool,    # True once +$1.00 profit hit
#   "trail_high": float,     # highest price seen since trail activated
#   "entry_count": int,      # 1 or 2
#   "entry_time": str,       # ISO timestamp
# }

# Entry counts per day (reset daily)
daily_entry_count: dict = {}   # ticker -> count (max 5)
daily_entry_date: str = ""

# Paper trading log (today's trades)
paper_trades: list = []

# Paper cash and all-time trades
paper_cash: float = PAPER_STARTING_CAPITAL
paper_all_trades: list = []

# Trade history persistence (Feature 1)
trade_history: list = []        # ALL closed paper trades, max 500
TRADE_HISTORY_MAX = 500

# Guard: prevent scheduler from saving empty state before load completes
_state_loaded = False

# Short positions (Wounded Buffalo strategy)
short_positions: dict = {}           # paper short: {ticker: {entry_price, shares, stop, trail_stop, trail_active, entry_time, date, side}}
daily_short_entry_count: dict = {}   # {ticker: int} — resets daily, separate from long count
short_trade_history: list = []       # max 500 closed paper shorts

# Daily loss limit (Feature 2)
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "-500"))
_trading_halted: bool = False
_trading_halted_reason: str = ""

# ============================================================
# MARKET MODE (scaffolding — NO behavior change in this version)
# ============================================================
# Classifies each scan cycle into one of four behavioral regimes and
# exposes a corresponding (frozen, clamped) profile of parameters.
# This version ONLY logs the classification and exposes it via /mode;
# no entry/exit code reads the profile yet. The goal is to observe the
# classifier in production for a week before wiring any parameter to it.
#
# Design principles for when this is wired up:
#   1. Adaptive logic only makes things MORE conservative than baseline,
#      never looser. The baseline is the floor; profiles can raise it.
#   2. Every adaptive parameter is bounded — see CLAMP_* below. A runaway
#      classifier cannot push any value outside these bounds.
#   3. Hard floors (DAILY_LOSS_LIMIT, min trail distance $1.00, min 1
#      share) are constants outside the profile system. They never move.

class MarketMode:
    OPEN       = "OPEN"        # 09:35 - 11:00 ET — OR breakout window
    CHOP       = "CHOP"        # 11:00 - 14:00 ET — lunch chop
    POWER      = "POWER"       # 14:00 - 15:30 ET — power hour
    DEFENSIVE  = "DEFENSIVE"   # triggered by realized P&L <= half loss limit
    CLOSED     = "CLOSED"      # outside market hours / weekend

# Clamps: hard bounds any adaptive value must stay inside.
# baseline values (what the bot uses TODAY, before this scaffold):
#   trail_pct      = 0.010    max entries/ticker/day = 5
#   shares         = 10       min score gate         = (none, all signals pass)
CLAMP_TRAIL_PCT         = (0.006, 0.018)   # 0.6% - 1.8%
CLAMP_MAX_ENTRIES       = (1, 5)
CLAMP_SHARES            = (1, 10)
CLAMP_MIN_SCORE_DELTA   = (0.0, 0.15)      # added to baseline score gate

def _clamp(val, bounds):
    lo, hi = bounds
    return max(lo, min(hi, val))

# Each profile is a frozen dict of the tunables + the rationale.
# All numeric values are pre-clamped by construction via _clamp().
# If you edit these, keep every value inside its CLAMP_* range.
MODE_PROFILES = {
    MarketMode.OPEN: {
        "trail_pct":         _clamp(0.012, CLAMP_TRAIL_PCT),
        "max_entries":       _clamp(5,     CLAMP_MAX_ENTRIES),
        "shares":            _clamp(10,    CLAMP_SHARES),
        "min_score_delta":   _clamp(0.00,  CLAMP_MIN_SCORE_DELTA),
        "allow_shorts":      True,
        "note":              "OR breakout window — baseline risk",
    },
    MarketMode.CHOP: {
        "trail_pct":         _clamp(0.008, CLAMP_TRAIL_PCT),
        "max_entries":       _clamp(2,     CLAMP_MAX_ENTRIES),
        "shares":            _clamp(10,    CLAMP_SHARES),
        "min_score_delta":   _clamp(0.10,  CLAMP_MIN_SCORE_DELTA),
        "allow_shorts":      True,
        "note":              "Lunch chop — tighter trails, fewer re-entries",
    },
    MarketMode.POWER: {
        "trail_pct":         _clamp(0.010, CLAMP_TRAIL_PCT),
        "max_entries":       _clamp(3,     CLAMP_MAX_ENTRIES),
        "shares":            _clamp(10,    CLAMP_SHARES),
        "min_score_delta":   _clamp(0.05,  CLAMP_MIN_SCORE_DELTA),
        "allow_shorts":      True,
        "note":              "Power hour — baseline with entry cutoff at 15:30",
    },
    MarketMode.DEFENSIVE: {
        "trail_pct":         _clamp(0.006, CLAMP_TRAIL_PCT),
        "max_entries":       _clamp(1,     CLAMP_MAX_ENTRIES),
        "shares":            _clamp(5,     CLAMP_SHARES),
        "min_score_delta":   _clamp(0.15,  CLAMP_MIN_SCORE_DELTA),
        "allow_shorts":      False,
        "note":              "Down >=50% of daily loss limit — size down, shorts off",
    },
    MarketMode.CLOSED: {
        "trail_pct":         _clamp(0.010, CLAMP_TRAIL_PCT),
        "max_entries":       _clamp(5,     CLAMP_MAX_ENTRIES),
        "shares":            _clamp(10,    CLAMP_SHARES),
        "min_score_delta":   _clamp(0.00,  CLAMP_MIN_SCORE_DELTA),
        "allow_shorts":      True,
        "note":              "Market closed — no action",
    },
}

# Last-computed mode snapshot, refreshed each scan. Read by /mode.
_current_mode: str = MarketMode.CLOSED
_current_mode_reason: str = "not yet classified"
_current_mode_pnl: float = 0.0
_current_mode_ts = None

# ============================================================
# MARKET MODE OBSERVERS (v3.1 scaffolding — observation only)
# ============================================================
# Three independent observers that do NOT gate any trade. They run
# alongside the MarketMode classifier, are logged on transitions, and
# surface in /mode. After a week of observation we'll decide which
# (if any) deserve to actually influence trading behavior.
#
# 1) BREADTH   — SPY/QQQ vs their PDC → BULLISH/NEUTRAL/BEARISH
#    (v3.4.34: anchor swapped from AVWAP to PDC)
# 2) RSI       — 14-period on resampled 5-min bars, SPY+QQQ aggregate
#                  → OVERBOUGHT/NEUTRAL/OVERSOLD; plus a per-ticker dict
# 3) TICKER    — per-ticker today realized P&L + current per-ticker RSI
#    HEAT        → lists of tickers that are already red or already at
#                  extremes, surfaced in /mode for pattern-spotting
#
# Thresholds are deliberately conservative for the observation phase.
# If a knob is eventually wired, it'll use these same thresholds or
# tighter ones, never looser.

BREADTH_TOLERANCE_PCT    = 0.001   # ±0.1% around PDC counts as NEUTRAL
RSI_OVERBOUGHT           = 70.0
RSI_OVERSOLD             = 30.0
RSI_PERIOD               = 14
RSI_BAR_MINUTES          = 5
RSI_MIN_BARS_REQUIRED    = RSI_PERIOD + 1   # Wilder RSI needs P+1 closes
TICKER_RED_THRESHOLD_USD = -5.0    # tickers with today P&L <= this are "red"

# Observer snapshot — refreshed each scan.
_current_breadth: str = "UNKNOWN"
_current_breadth_detail: str = ""
_current_rsi_regime: str = "UNKNOWN"
_current_rsi_detail: str = ""
_current_rsi_per_ticker: dict = {}      # ticker -> float RSI
_current_ticker_pnl: dict = {}          # ticker -> realized P&L today
_current_ticker_red: list = []          # list of (ticker, pnl) sorted worst-first
_current_ticker_extremes: list = []     # list of (ticker, rsi, "OB"/"OS")

# v3.4.21 — per-ticker entry-gate snapshot for dashboard rendering.
# Populated by _update_gate_snapshot() on every scan cycle.
# Shape: {ticker: {
#     "side": "LONG"|"SHORT",
#     "break": bool,              # 1m close crossed OR (above/below)
#     "polarity": bool,           # price vs PDC on the right side
#     "index": bool,              # SPY/QQQ on the right side of PDC
#     "di": bool|None,            # DI+/DI- >= TIGER_V2_DI_THRESHOLD;
#                                 # None = warmup (DI not yet computable)
#     "ts": iso timestamp,
# }}
# v3.5.x: vol_pct / vol_ok removed — Tiger 2.0 replaced the volume gate
# with DI+/DI-, and TIGER_V2_REQUIRE_VOL defaults to False. Keeping the
# fields on the snapshot was decorative and misled diagnosis.
# Read-only from outside the scan loop; never cleared mid-scan.
_gate_snapshot: dict = {}

# v3.4.21 — near-miss ring buffer. Breakouts that cleared the price
# gate (1m close past OR) but were declined by volume confirmation.
# Bounded to last 20 entries. Exposed via /api/state and /near_misses.
# Records only — no effect on entry decisions (fail-closed stays).
_NEAR_MISS_MAX = 20
_near_miss_log: list = []


def _record_near_miss(**row):
    """Prepend a near-miss record. Trim to _NEAR_MISS_MAX.

    Expected keys: ticker, side, reason, close, level, vol_bar, vol_avg,
    vol_pct, ts. Missing keys are allowed — stored as-is.
    """
    global _near_miss_log
    row.setdefault("ts", datetime.now(timezone.utc).isoformat())
    _near_miss_log.insert(0, row)
    if len(_near_miss_log) > _NEAR_MISS_MAX:
        _near_miss_log = _near_miss_log[:_NEAR_MISS_MAX]


def _update_gate_snapshot(ticker):
    """Recompute the dashboard gate snapshot for ``ticker`` from the
    current OR envelope and live price.

    Side + break are derived purely from OR envelope each cycle (no
    latch). When inside the envelope, side falls back to the nearest
    edge for the polarity preview but break is False.

    Emits a structured ``GATE_EVAL`` log line for audit.
    """
    if ticker not in or_high or ticker not in or_low:
        return
    or_h = or_high[ticker]
    or_l = or_low[ticker]
    pdc_val = pdc.get(ticker)

    bars = fetch_1min_bars(ticker)
    if not bars:
        return
    price = bars.get("current_price")
    if price is None or price <= 0:
        return

    fmp_q = get_fmp_quote(ticker)
    if fmp_q:
        fmp_price = fmp_q.get("price")
        if fmp_price and fmp_price > 0:
            price = fmp_price
        fmp_pdc = fmp_q.get("previousClose")
        if fmp_pdc and fmp_pdc > 0:
            pdc_val = fmp_pdc

    if price > or_h:
        side = "LONG"
        break_ok = True
    elif price < or_l:
        side = "SHORT"
        break_ok = True
    else:
        side = "LONG" if abs(price - or_h) < abs(price - or_l) else "SHORT"
        break_ok = False

    if pdc_val and pdc_val > 0:
        polarity_ok = (price > pdc_val) if side == "LONG" else (price < pdc_val)
    else:
        polarity_ok = False

    spy_pdc_val = pdc.get("SPY")
    qqq_pdc_val = pdc.get("QQQ")
    index_ok = None
    if spy_pdc_val and qqq_pdc_val and spy_pdc_val > 0 and qqq_pdc_val > 0:
        spy_bars = fetch_1min_bars("SPY")
        qqq_bars = fetch_1min_bars("QQQ")
        if spy_bars and qqq_bars:
            spy_p = spy_bars.get("current_price")
            qqq_p = qqq_bars.get("current_price")
            if spy_p and qqq_p:
                if side == "LONG":
                    index_ok = (spy_p > spy_pdc_val) and (qqq_p > qqq_pdc_val)
                else:
                    index_ok = (spy_p < spy_pdc_val) and (qqq_p < qqq_pdc_val)

    di_plus, di_minus = tiger_di(ticker)
    if di_plus is None or di_minus is None:
        di_ok = None  # warmup
    elif side == "LONG":
        di_ok = di_plus >= TIGER_V2_DI_THRESHOLD
    else:
        di_ok = di_minus >= TIGER_V2_DI_THRESHOLD

    _gate_snapshot[ticker] = {
        "side": side,
        "break": bool(break_ok),
        "polarity": bool(polarity_ok),
        "index": index_ok,
        "di": di_ok,
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    idx_str = "None" if index_ok is None else str(bool(index_ok))
    di_str = "None" if di_ok is None else str(bool(di_ok))
    logger.info(
        "GATE_EVAL ticker=%s price=%.2f or_hi=%.2f or_lo=%.2f "
        "side=%s break=%s polarity=%s index=%s di=%s",
        ticker, price, or_h, or_l, side, bool(break_ok),
        bool(polarity_ok), idx_str, di_str,
    )


def _classify_breadth():
    """Observer 1: breadth from SPY/QQQ vs their PDC.
    Returns (label, detail). Never raises.
    v3.4.34: anchor swapped from AVWAP → PDC.
    """
    try:
        # fetch_1min_bars is cycle-cached — if the scan loop already
        # fetched SPY/QQQ this cycle we reuse; otherwise we fetch once.
        spy_bars = fetch_1min_bars("SPY")
        qqq_bars = fetch_1min_bars("QQQ")
        spy_px = spy_bars.get("current_price") if spy_bars else None
        qqq_px = qqq_bars.get("current_price") if qqq_bars else None
        spy_anchor = pdc.get("SPY") or 0
        qqq_anchor = pdc.get("QQQ") or 0
        if not (spy_px and qqq_px and spy_anchor and qqq_anchor):
            return ("UNKNOWN", "SPY/QQQ price or PDC not ready")

        spy_diff = (spy_px - spy_anchor) / spy_anchor
        qqq_diff = (qqq_px - qqq_anchor) / qqq_anchor
        tol = BREADTH_TOLERANCE_PCT

        def _side(d):
            if d >  tol: return "above"
            if d < -tol: return "below"
            return "at"

        spy_side = _side(spy_diff)
        qqq_side = _side(qqq_diff)
        detail = "SPY %+.2f%% %s PDC | QQQ %+.2f%% %s PDC" % (
            spy_diff * 100, spy_side, qqq_diff * 100, qqq_side)

        if spy_side == "above" and qqq_side == "above":
            return ("BULLISH", detail)
        if spy_side == "below" and qqq_side == "below":
            return ("BEARISH", detail)
        return ("NEUTRAL", detail)
    except Exception as e:
        logger.debug("_classify_breadth failed: %s", e)
        return ("UNKNOWN", "breadth computation failed")


def _resample_to_5min(timestamps, closes):
    """Resample a 1-min close series into 5-min bar closes.

    Each 5-min bar closes on the last 1-min close whose epoch second falls
    inside the [bar_start, bar_start+300) window aligned to UTC minute
    boundaries (9:30:00, 9:35:00, 9:40:00, …). Partial/forming bars are
    dropped — only complete 5-min intervals contribute.

    Returns a list of floats (oldest-first). Robust to None closes and to
    timestamps in any order (will sort).
    """
    if not timestamps or not closes or len(timestamps) != len(closes):
        return []
    # Pair and drop Nones, then sort by timestamp ascending.
    pairs = [(int(t), float(c)) for t, c in zip(timestamps, closes)
             if t is not None and c is not None]
    if not pairs:
        return []
    pairs.sort(key=lambda p: p[0])

    # Bucket by floor(ts / 300). Last close in each bucket is the bar close.
    buckets = {}
    for ts, c in pairs:
        bucket = ts // 300
        buckets[bucket] = c   # overwrites — last wins

    # Drop the most recent (possibly forming) bucket so we only return
    # closed bars. Safe heuristic: if the last pair's ts doesn't reach
    # (bucket+1)*300 - 60, the bar is still forming. We conservatively
    # drop the newest bucket always — partial bars are noisy for RSI.
    ordered = sorted(buckets.keys())
    if len(ordered) >= 1:
        ordered = ordered[:-1]   # drop newest (possibly partial)
    return [buckets[b] for b in ordered]


def _compute_rsi(closes, period=RSI_PERIOD):
    """Wilder's RSI on a list of closes (oldest-first).
    Returns float in [0, 100], or None if not enough data.
    """
    if not closes or len(closes) < period + 1:
        return None
    try:
        gains = 0.0
        losses = 0.0
        # Seed average gain/loss over the first `period` deltas.
        for i in range(1, period + 1):
            delta = closes[i] - closes[i - 1]
            if delta > 0: gains += delta
            else:         losses += -delta
        avg_gain = gains / period
        avg_loss = losses / period

        # Wilder smoothing for remaining deltas.
        for i in range(period + 1, len(closes)):
            delta = closes[i] - closes[i - 1]
            gain = delta if delta > 0 else 0.0
            loss = -delta if delta < 0 else 0.0
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))
    except Exception:
        return None


# ============================================================
# v3.4.47 — Eye of the Tiger 2.0 helpers
# ============================================================

def _resample_to_5min_ohlc(timestamps, opens, highs, lows, closes):
    """Resample 1m OHLC into 5m OHLC.

    Returns dict with lists 'highs', 'lows', 'closes'
    (oldest-first), only fully-closed bars.
    Uses floor(ts/300) bucketing like _resample_to_5min.
    Drops the newest bucket (may be forming).
    """
    if not timestamps or not closes:
        return None
    # Build per-bucket dicts: store max high, min low, last close.
    buckets_high = {}
    buckets_low = {}
    buckets_close = {}
    for i, ts in enumerate(timestamps):
        if ts is None:
            continue
        h = highs[i] if i < len(highs) else None
        lo = lows[i] if i < len(lows) else None
        c = closes[i] if i < len(closes) else None
        if h is None or lo is None or c is None:
            continue
        bucket = int(ts) // 300
        if bucket not in buckets_high:
            buckets_high[bucket] = h
            buckets_low[bucket] = lo
            buckets_close[bucket] = c
        else:
            buckets_high[bucket] = max(buckets_high[bucket], h)
            buckets_low[bucket] = min(buckets_low[bucket], lo)
            buckets_close[bucket] = c  # last close wins
    ordered = sorted(buckets_high.keys())
    if len(ordered) <= 1:
        return None
    # Drop newest bucket (may be forming)
    ordered = ordered[:-1]
    return {
        "highs":  [buckets_high[b]  for b in ordered],
        "lows":   [buckets_low[b]   for b in ordered],
        "closes": [buckets_close[b] for b in ordered],
    }


DI_PERIOD = 15  # Gene's spec: "DI+ (15 period, 5m)"


def _compute_di(highs, lows, closes, period=DI_PERIOD):
    """Wilder DI+ and DI-.

    Returns (di_plus, di_minus) as floats, or
    (None, None) if insufficient data.

    Wilder formula:
      +DM[i] = high[i]-high[i-1] if that > low[i-1]-low[i] AND >0 else 0
      -DM[i] = low[i-1]-low[i] if that > high[i]-high[i-1] AND >0 else 0
      TR[i]  = max(high[i]-low[i],
                   |high[i]-close[i-1]|, |low[i]-close[i-1]|)
    Smoothing (Wilder):
      first_val = sum of first `period` values
      new = prev - prev/period + current
    Needs at least period+1 bars.
    """
    n = len(closes)
    if n < period + 1 or len(highs) < period + 1 or len(lows) < period + 1:
        return None, None
    try:
        # Compute raw DM and TR for each bar i >= 1
        raw_pdm = []
        raw_ndm = []
        raw_tr  = []
        for i in range(1, n):
            up_move   = highs[i]  - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            pdm = up_move   if (up_move   > down_move and up_move   > 0) else 0.0
            ndm = down_move if (down_move > up_move   and down_move > 0) else 0.0
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i]  - closes[i - 1]),
            )
            raw_pdm.append(pdm)
            raw_ndm.append(ndm)
            raw_tr.append(tr)

        # Seed: sum of first `period` values
        smooth_pdm = sum(raw_pdm[:period])
        smooth_ndm = sum(raw_ndm[:period])
        smooth_tr  = sum(raw_tr[:period])

        # Wilder smoothing for remaining values
        for i in range(period, len(raw_tr)):
            smooth_pdm = smooth_pdm - smooth_pdm / period + raw_pdm[i]
            smooth_ndm = smooth_ndm - smooth_ndm / period + raw_ndm[i]
            smooth_tr  = smooth_tr  - smooth_tr  / period + raw_tr[i]

        if smooth_tr == 0:
            return None, None
        di_plus  = 100.0 * smooth_pdm / smooth_tr
        di_minus = 100.0 * smooth_ndm / smooth_tr
        return di_plus, di_minus
    except Exception:
        return None, None


# ------------------------------------------------------------
# DI seed buffer (v4.0.2-beta)
# ------------------------------------------------------------
# Without seeding, DI starts null on every boot and takes
# ~DI_PERIOD*2 = ~30 closed 5m bars (75 min of live data) before
# tiger_di() can return a non-null value. _seed_di_buffer() pulls
# historical 5m bars from Alpaca at scanner startup so DI is armed
# on the very first scan cycle.
#
# Storage: per-ticker list of closed 5m OHLC dicts, oldest-first.
#   { ticker: [ {"bucket": int, "high": f, "low": f, "close": f}, ... ] }
# tiger_di() merges these with live-resampled 5m bars, deduped by
# bucket (= ts // 300), so as the live session accumulates the
# seed is transparently superseded.
_DI_SEED_CACHE: dict = {}


def _alpaca_data_client():
    """Build a read-only StockHistoricalDataClient using whatever
    Alpaca paper credentials are in the environment. Tries Val first,
    then Gene. Returns None if no keys are set or alpaca-py import
    fails \u2014 caller must tolerate a None return.
    """
    key = os.getenv("VAL_ALPACA_PAPER_KEY", "").strip() \
          or os.getenv("GENE_ALPACA_PAPER_KEY", "").strip()
    secret = os.getenv("VAL_ALPACA_PAPER_SECRET", "").strip() \
             or os.getenv("GENE_ALPACA_PAPER_SECRET", "").strip()
    if not key or not secret:
        return None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        return StockHistoricalDataClient(key, secret)
    except Exception as e:
        logger.debug("alpaca data client build failed: %s", e)
        return None


def _seed_di_buffer(ticker):
    """Seed the DI 5m buffer for `ticker` from Alpaca historical bars.

    Priority stream (oldest \u2192 newest for DI math):
      today-RTH \u2192 today-premarket \u2192 prior-day-RTH
    but we feed oldest-first so the order inside the buffer is
    chronological: prior-day-RTH, then today-premarket, then today-RTH.
    The "priority" really means \u2014 if we already have enough
    today-RTH bars, we don't need to reach back further.

    If the DI_PREMARKET_SEED env flag is "0", premarket bars are
    skipped (kill switch for premarket-noise concerns).

    Safe to call on restart mid-session. Idempotent \u2014 overwrites
    any prior seed for the ticker. On any Alpaca failure logs a
    warning and continues; DI will warm up from live ticks.

    Returns dict {"bars_today_rth": N, "bars_premarket": N,
                  "bars_prior_day": N, "di_after_seed": float|None}.
    """
    result = {
        "bars_today_rth": 0, "bars_premarket": 0,
        "bars_prior_day": 0, "di_after_seed": None,
    }
    client = _alpaca_data_client()
    if client is None:
        logger.debug("DI_SEED %s skipped \u2014 no alpaca data client", ticker)
        return result

    premarket_on = os.getenv("DI_PREMARKET_SEED", "1").strip() not in (
        "0", "false", "False", "",
    )

    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
    except Exception as e:
        logger.debug("DI_SEED %s import failed: %s", ticker, e)
        return result

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    today_0400 = now_et.replace(hour=4,  minute=0,  second=0, microsecond=0)
    today_0930 = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    yday = now_et - timedelta(days=1)
    # Step back over weekend to last weekday
    while yday.weekday() >= 5:
        yday = yday - timedelta(days=1)
    yday_rth_end   = yday.replace(hour=16, minute=0, second=0, microsecond=0)
    yday_rth_start = yday.replace(hour=14, minute=50, second=0, microsecond=0)

    def _fetch(start, end):
        try:
            req = StockBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=TimeFrame.Minute,
                start=start.astimezone(timezone.utc),
                end=end.astimezone(timezone.utc),
                feed="iex",
            )
            resp = client.get_stock_bars(req)
            data = getattr(resp, "data", {}) or {}
            rows = data.get(ticker, []) or []
            return rows
        except Exception as e:
            logger.warning("DI_SEED %s alpaca fetch %s\u2192%s failed: %s",
                           ticker, start, end, e)
            return []

    # Fetch today 04:00 ET \u2192 now (premarket + whatever RTH has happened)
    today_rows = _fetch(today_0400, now_et)

    # Bucket 1m rows into 5m OHLC, tagged by classification.
    # today_0930_ts = unix seconds of today's 09:30 ET
    today_0930_ts = int(today_0930.timestamp())

    today_rth_buckets   = {}
    today_pre_buckets   = {}

    for row in today_rows:
        ts = getattr(row, "timestamp", None)
        if ts is None:
            continue
        try:
            # alpaca timestamps are tz-aware datetimes
            epoch = int(ts.timestamp())
        except Exception:
            continue
        h  = float(getattr(row, "high",  0) or 0)
        lo = float(getattr(row, "low",   0) or 0)
        c  = float(getattr(row, "close", 0) or 0)
        if h <= 0 or lo <= 0 or c <= 0:
            continue
        bucket = epoch // 300
        target = today_rth_buckets if epoch >= today_0930_ts else today_pre_buckets
        if bucket not in target:
            target[bucket] = {"bucket": bucket, "high": h, "low": lo, "close": c}
        else:
            target[bucket]["high"]  = max(target[bucket]["high"],  h)
            target[bucket]["low"]   = min(target[bucket]["low"],   lo)
            target[bucket]["close"] = c

    # Drop newest bucket if it could still be forming (now < bucket_end)
    def _finalize(buckets):
        ordered = sorted(buckets.keys())
        if not ordered:
            return []
        last = ordered[-1]
        last_end_ts = (last + 1) * 300
        if int(now_et.timestamp()) < last_end_ts:
            ordered = ordered[:-1]
        return [buckets[b] for b in ordered]

    today_rth_list = _finalize(today_rth_buckets)
    today_pre_list = _finalize(today_pre_buckets) if premarket_on else []
    result["bars_today_rth"]  = len(today_rth_list)
    result["bars_premarket"]  = len(today_pre_list)

    seeded_enough = len(today_rth_list) + len(today_pre_list) >= DI_PERIOD * 2
    prior_day_list = []
    if not seeded_enough:
        prior_rows = _fetch(yday_rth_start, yday_rth_end)
        prior_buckets = {}
        for row in prior_rows:
            ts = getattr(row, "timestamp", None)
            if ts is None:
                continue
            try:
                epoch = int(ts.timestamp())
            except Exception:
                continue
            h  = float(getattr(row, "high",  0) or 0)
            lo = float(getattr(row, "low",   0) or 0)
            c  = float(getattr(row, "close", 0) or 0)
            if h <= 0 or lo <= 0 or c <= 0:
                continue
            bucket = epoch // 300
            if bucket not in prior_buckets:
                prior_buckets[bucket] = {"bucket": bucket, "high": h,
                                          "low": lo, "close": c}
            else:
                prior_buckets[bucket]["high"]  = max(prior_buckets[bucket]["high"],  h)
                prior_buckets[bucket]["low"]   = min(prior_buckets[bucket]["low"],   lo)
                prior_buckets[bucket]["close"] = c
        prior_day_list = [prior_buckets[b] for b in sorted(prior_buckets.keys())]
        result["bars_prior_day"] = len(prior_day_list)

    # Combine chronologically: prior-day \u2192 today-premarket \u2192 today-RTH
    combined = prior_day_list + today_pre_list + today_rth_list
    # Dedup by bucket, keep last
    dedup = {}
    for b in combined:
        dedup[b["bucket"]] = b
    final_list = [dedup[k] for k in sorted(dedup.keys())]
    _DI_SEED_CACHE[ticker] = final_list

    # Compute DI on the seeded state for logging
    if len(final_list) >= DI_PERIOD + 1:
        highs  = [b["high"]  for b in final_list]
        lows   = [b["low"]   for b in final_list]
        closes = [b["close"] for b in final_list]
        dp, _dm = _compute_di(highs, lows, closes)
        result["di_after_seed"] = dp

    logger.info(
        "DI_SEED ticker=%s bars_today_rth=%d bars_premarket=%d "
        "bars_prior_day=%d di_after_seed=%s",
        ticker, result["bars_today_rth"], result["bars_premarket"],
        result["bars_prior_day"],
        ("%.2f" % result["di_after_seed"])
        if result["di_after_seed"] is not None else "None",
    )
    return result


def _seed_di_all(tickers):
    """Run _seed_di_buffer for every ticker and emit a summary line."""
    seeded = 0
    skipped = 0
    for t in tickers:
        try:
            r = _seed_di_buffer(t)
            if r.get("di_after_seed") is not None:
                seeded += 1
            else:
                skipped += 1
        except Exception as e:
            logger.warning("DI_SEED %s crashed: %s", t, e)
            skipped += 1
    logger.info(
        "DI_SEED_DONE tickers=%d seeded_with_nonnull_di=%d skipped=%d",
        len(tickers), seeded, skipped,
    )


# ------------------------------------------------------------
# Opening Range seed (v4.0.3-beta)
# ------------------------------------------------------------
# Mirrors the DI seeder: on startup (or mid-session restart), pull
# today's 9:30 ET +/- OR_WINDOW_MINUTES from Alpaca historical 1m
# bars and write or_high / or_low / pdc directly. Idempotent \u2014
# the scheduled 9:35 ET collect_or() still runs on fresh boots that
# happen before the open. On any Alpaca failure the seeder logs a
# warning and returns; the existing Yahoo+FMP path in collect_or()
# continues to work.
OR_WINDOW_MINUTES = int(os.getenv("OR_WINDOW_MINUTES", "5") or "5")


def _seed_opening_range(ticker):
    """Seed or_high[ticker]/or_low[ticker]/pdc[ticker] from Alpaca
    historical 1m bars covering today's 09:30 ET \u2192 09:30+OR_WINDOW_MINUTES
    ET window. Returns dict with keys: or_high, or_low, bars_used.

    Only seeds when the OR window is complete (now_et >= window end).
    Pre-open or pre-9:35-ET restarts return bars_used=0 so the
    scheduled 09:35 ET collect_or() can run cleanly.
    """
    result = {"or_high": None, "or_low": None, "bars_used": 0}
    client = _alpaca_data_client()
    if client is None:
        logger.debug("OR_SEED %s skipped \u2014 no alpaca data client", ticker)
        return result
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
    except Exception as e:
        logger.debug("OR_SEED %s import failed: %s", ticker, e)
        return result

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    window_start = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    window_end = window_start + timedelta(minutes=OR_WINDOW_MINUTES)
    if now_et < window_end:
        logger.debug("OR_SEED %s skipped \u2014 window not complete (now_et=%s < end=%s)",
                     ticker, now_et.strftime("%H:%M"),
                     window_end.strftime("%H:%M"))
        return result

    try:
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Minute,
            start=window_start.astimezone(timezone.utc),
            end=window_end.astimezone(timezone.utc),
            feed="iex",
        )
        resp = client.get_stock_bars(req)
        data = getattr(resp, "data", {}) or {}
        rows = data.get(ticker, []) or []
    except Exception as e:
        logger.warning("OR_SEED %s alpaca fetch failed: %s", ticker, e)
        return result

    max_hi = None
    min_lo = None
    bars_used = 0
    window_start_ts = int(window_start.timestamp())
    window_end_ts = int(window_end.timestamp())
    for row in rows:
        ts = getattr(row, "timestamp", None)
        if ts is None:
            continue
        try:
            epoch = int(ts.timestamp())
        except Exception:
            continue
        if epoch < window_start_ts or epoch >= window_end_ts:
            continue
        h = float(getattr(row, "high", 0) or 0)
        lo = float(getattr(row, "low", 0) or 0)
        if h <= 0 or lo <= 0:
            continue
        if max_hi is None or h > max_hi:
            max_hi = h
        if min_lo is None or lo < min_lo:
            min_lo = lo
        bars_used += 1

    if max_hi is None or min_lo is None:
        logger.warning("OR_SEED %s \u2014 no usable bars in window", ticker)
        return result

    or_high[ticker] = max_hi
    or_low[ticker] = min_lo
    result["or_high"] = max_hi
    result["or_low"] = min_lo
    result["bars_used"] = bars_used
    logger.info(
        "OR_SEED ticker=%s or_high=%.2f or_low=%.2f bars_used=%d "
        "window_et=%s-%s source=alpaca_historical",
        ticker, max_hi, min_lo, bars_used,
        window_start.strftime("%H:%M"), window_end.strftime("%H:%M"),
    )
    return result


def _seed_opening_range_all(tickers):
    """Run _seed_opening_range for every ticker and emit a summary.

    Marks or_collected_date=today once at least one ticker is seeded,
    so the scheduled 09:35 ET collect_or() does not overwrite the
    fresher Alpaca-sourced OR. Safe on a before-open restart \u2014
    returns immediately when the OR window is not yet complete.
    """
    global or_collected_date
    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    today = now_et.strftime("%Y-%m-%d")
    window_end = now_et.replace(hour=9, minute=30, second=0, microsecond=0) \
                    + timedelta(minutes=OR_WINDOW_MINUTES)
    if now_et < window_end:
        logger.info(
            "OR_SEED_DONE tickers=0 seeded=0 skipped=%d \u2014 pre-OR-window",
            len(tickers),
        )
        return
    seeded = 0
    skipped = 0
    for t in tickers:
        try:
            r = _seed_opening_range(t)
            if r.get("bars_used", 0) > 0:
                seeded += 1
            else:
                skipped += 1
        except Exception as e:
            logger.warning("OR_SEED %s crashed: %s", t, e)
            skipped += 1
    if seeded > 0:
        or_collected_date = today
    logger.info(
        "OR_SEED_DONE tickers=%d seeded=%d skipped=%d",
        len(tickers), seeded, skipped,
    )


def _resample_to_5min_ohlc_buckets(timestamps, highs, lows, closes):
    """Like _resample_to_5min_ohlc but returns list of bucket dicts.
    Oldest-first, newest (possibly forming) bucket dropped.
    Returns [] on empty input.
    """
    if not timestamps or not closes:
        return []
    buckets = {}
    for i, ts in enumerate(timestamps):
        if ts is None:
            continue
        h  = highs[i]  if i < len(highs)  else None
        lo = lows[i]   if i < len(lows)   else None
        c  = closes[i] if i < len(closes) else None
        if h is None or lo is None or c is None:
            continue
        bucket = int(ts) // 300
        if bucket not in buckets:
            buckets[bucket] = {"bucket": bucket, "high": h, "low": lo, "close": c}
        else:
            buckets[bucket]["high"]  = max(buckets[bucket]["high"],  h)
            buckets[bucket]["low"]   = min(buckets[bucket]["low"],   lo)
            buckets[bucket]["close"] = c
    ordered = sorted(buckets.keys())
    if len(ordered) <= 1:
        return []
    ordered = ordered[:-1]
    return [buckets[b] for b in ordered]


def tiger_di(ticker):
    """Return (di_plus, di_minus) for a ticker using 5m OHLC
    resampled from fetch_1min_bars, or (None, None) if not ready.

    Merges any pre-seeded 5m bars (_DI_SEED_CACHE) with live-resampled
    bars so DI is available from the first scan after boot. Both
    streams are keyed by real epoch buckets (ts // 300); overlapping
    buckets prefer the live value (last-write-wins).
    """
    bars = fetch_1min_bars(ticker)
    live_list = []
    if bars and bars.get("timestamps"):
        live_list = _resample_to_5min_ohlc_buckets(
            bars["timestamps"],
            bars.get("highs",  []),
            bars.get("lows",   []),
            bars.get("closes", []),
        )

    seed = _DI_SEED_CACHE.get(ticker) or []
    merged = {}
    for b in seed:
        merged[b["bucket"]] = (b["high"], b["low"], b["close"])
    for b in live_list:
        merged[b["bucket"]] = (b["high"], b["low"], b["close"])

    if not merged:
        return None, None
    keys = sorted(merged.keys())
    highs  = [merged[k][0] for k in keys]
    lows   = [merged[k][1] for k in keys]
    closes = [merged[k][2] for k in keys]
    if len(closes) < DI_PERIOD + 1:
        return None, None
    return _compute_di(highs, lows, closes)


def _tiger_two_bar_long(closes, or_h):
    """True if the last 2 closed 1m closes are both > OR high.

    Requires len(closes) >= 2. Fail-closed: missing data -> False.
    """
    if not closes or len(closes) < 2:
        return False
    return closes[-1] > or_h and closes[-2] > or_h


def _tiger_two_bar_short(closes, or_l):
    """True if the last 2 closed 1m closes are both < OR low.

    Requires len(closes) >= 2. Fail-closed: missing data -> False.
    """
    if not closes or len(closes) < 2:
        return False
    return closes[-1] < or_l and closes[-2] < or_l


def _rsi_for_ticker(ticker):
    """Compute current RSI(14) on 5-min bars for a ticker, using cached bars.
    Returns float or None. Never raises.
    """
    try:
        bars = fetch_1min_bars(ticker)   # cached per cycle
        if not bars:
            return None
        closes_5m = _resample_to_5min(bars.get("timestamps", []),
                                      bars.get("closes", []))
        if len(closes_5m) < RSI_MIN_BARS_REQUIRED:
            return None
        return _compute_rsi(closes_5m)
    except Exception as e:
        logger.debug("_rsi_for_ticker %s failed: %s", ticker, e)
        return None


def _classify_rsi_regime():
    """Observer 2: aggregate RSI regime from SPY+QQQ 5-min RSI.
    Returns (label, detail, per_ticker_dict). Never raises.
    """
    per_ticker = {}
    try:
        spy_rsi = _rsi_for_ticker("SPY")
        qqq_rsi = _rsi_for_ticker("QQQ")
        if spy_rsi is not None: per_ticker["SPY"] = spy_rsi
        if qqq_rsi is not None: per_ticker["QQQ"] = qqq_rsi

        # Per-ticker RSI for the trade universe. Uses the cycle cache,
        # so if scan_loop already fetched these bars this cycle there's
        # no extra network call.
        for t in TRADE_TICKERS:
            v = _rsi_for_ticker(t)
            if v is not None:
                per_ticker[t] = v

        if spy_rsi is None or qqq_rsi is None:
            return ("UNKNOWN", "SPY/QQQ RSI not ready (need %d closed 5m bars)" %
                    RSI_MIN_BARS_REQUIRED, per_ticker)

        avg = (spy_rsi + qqq_rsi) / 2.0
        detail = "SPY %.1f | QQQ %.1f | avg %.1f" % (spy_rsi, qqq_rsi, avg)
        if avg >= RSI_OVERBOUGHT: return ("OVERBOUGHT", detail, per_ticker)
        if avg <= RSI_OVERSOLD:   return ("OVERSOLD",   detail, per_ticker)
        return ("NEUTRAL", detail, per_ticker)
    except Exception as e:
        logger.debug("_classify_rsi_regime failed: %s", e)
        return ("UNKNOWN", "RSI regime computation failed", per_ticker)


def _per_ticker_today_pnl():
    """Observer 3a: realized P&L today, bucketed by ticker.
    Returns dict ticker -> float. Never raises.
    Reads paper_trades (long SELLs) AND short_trade_history (short COVERs).
    Short COVERs never appear in paper_trades — they live in short_trade_history.
    """
    try:
        today_str = _now_et().strftime("%Y-%m-%d")
        out = {}
        for t in paper_trades:
            if t.get("date") != today_str: continue
            if t.get("action") != "SELL": continue
            tk = t.get("ticker", "?")
            out[tk] = out.get(tk, 0.0) + (t.get("pnl", 0) or 0)
        for t in short_trade_history:
            if t.get("date") != today_str: continue
            tk = t.get("ticker", "?")
            out[tk] = out.get(tk, 0.0) + (t.get("pnl", 0) or 0)
        return out
    except Exception as e:
        logger.debug("_per_ticker_today_pnl failed: %s", e)
        return {}


def _classify_ticker_heat(per_ticker_pnl, per_ticker_rsi):
    """Observer 3b: build the ticker-heat lists for /mode and logs.
    Returns (red_list, extremes_list):
      red_list:       [(ticker, pnl), …] worst-first, pnl <= RED threshold
      extremes_list:  [(ticker, rsi, "OB"|"OS"), …] tickers in RSI extremes
    """
    try:
        red = [(tk, p) for tk, p in per_ticker_pnl.items()
               if p <= TICKER_RED_THRESHOLD_USD]
        red.sort(key=lambda x: x[1])   # most negative first

        extremes = []
        for tk, r in per_ticker_rsi.items():
            if r >= RSI_OVERBOUGHT: extremes.append((tk, r, "OB"))
            elif r <= RSI_OVERSOLD: extremes.append((tk, r, "OS"))
        extremes.sort(key=lambda x: x[1], reverse=True)   # highest RSI first
        return (red, extremes)
    except Exception as e:
        logger.debug("_classify_ticker_heat failed: %s", e)
        return ([], [])


def _compute_today_realized_pnl() -> float:
    """Realized P&L today across longs + shorts for the paper portfolio.
    Unrealized P&L is excluded on purpose — we want the number that
    drives the DAILY_LOSS_LIMIT halt, which is realized-only.

    Storage asymmetry (critical): long SELLs go to paper_trades with
    action="SELL"; short COVERs are written ONLY to short_trade_history
    (never to paper_trades). We must read both lists or short P&L is
    silently dropped from the DEFENSIVE-mode gate.
    """
    today_str = _now_et().strftime("%Y-%m-%d")
    pnl = 0.0
    for t in paper_trades:
        if t.get("date") == today_str and t.get("action") == "SELL":
            pnl += t.get("pnl", 0) or 0
    for t in short_trade_history:
        if t.get("date") == today_str:
            pnl += t.get("pnl", 0) or 0
    return pnl


def _today_pnl_breakdown() -> tuple:
    """Returns (sells_list, covers_list, total_pnl, wins, losses, n_trades)
    for today. Single source of truth used by EOD summaries, /dashboard,
    and weekly digest helpers.
    """
    today_str = _now_et().strftime("%Y-%m-%d")
    sells = [t for t in paper_trades
             if t.get("action") == "SELL" and t.get("date", "") == today_str]
    covers = [t for t in short_trade_history
              if t.get("date", "") == today_str]
    combined = list(sells) + list(covers)
    total = sum((t.get("pnl", 0) or 0) for t in combined)
    wins = sum(1 for t in combined if (t.get("pnl", 0) or 0) >= 0)
    losses = len(combined) - wins
    return (sells, covers, total, wins, losses, len(combined))


def get_current_mode(now_et=None) -> tuple:
    """Classify the current market mode. Returns (mode, reason, pnl_used).
    Priority: CLOSED > DEFENSIVE > time-of-day bucket.
    """
    if now_et is None:
        now_et = _now_et()

    # CLOSED: weekends and outside the same window scan_loop() skips.
    if now_et.weekday() >= 5:
        return (MarketMode.CLOSED, "weekend", 0.0)
    before_open = now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 35)
    after_close = now_et.hour >= 16 or (now_et.hour == 15 and now_et.minute >= 55)
    if before_open or after_close:
        return (MarketMode.CLOSED, "outside market hours", 0.0)

    # DEFENSIVE: realized P&L today is at or below half the daily loss limit.
    # Uses paper portfolio's P&L as the canonical risk signal (TP mirrors it).
    today_pnl = _compute_today_realized_pnl()
    half_limit = DAILY_LOSS_LIMIT / 2.0   # e.g. -500 / 2 = -250
    if today_pnl <= half_limit:
        reason = "realized P&L $%+.2f <= half limit $%+.2f" % (today_pnl, half_limit)
        return (MarketMode.DEFENSIVE, reason, today_pnl)

    # Time-of-day buckets.
    hm = now_et.hour * 60 + now_et.minute
    if hm < 11 * 60:
        return (MarketMode.OPEN,  "09:35-11:00 ET", today_pnl)
    if hm < 14 * 60:
        return (MarketMode.CHOP,  "11:00-14:00 ET", today_pnl)
    return (MarketMode.POWER,     "14:00-15:55 ET", today_pnl)


def _refresh_market_mode():
    """Recompute the cached mode + observers. Called at the top of every
    scan cycle. Pure observation — no side effects beyond updating module
    state and emitting log lines on transitions.
    """
    global _current_mode, _current_mode_reason, _current_mode_pnl, _current_mode_ts
    global _current_breadth, _current_breadth_detail
    global _current_rsi_regime, _current_rsi_detail, _current_rsi_per_ticker
    global _current_ticker_pnl, _current_ticker_red, _current_ticker_extremes

    prev_mode     = _current_mode
    prev_breadth  = _current_breadth
    prev_rsi      = _current_rsi_regime

    now_et = _now_et()

    # Core mode classifier.
    mode, reason, pnl = get_current_mode(now_et)
    _current_mode        = mode
    _current_mode_reason = reason
    _current_mode_pnl    = pnl
    _current_mode_ts     = now_et
    if mode != prev_mode:
        logger.info("MarketMode: %s -> %s (%s)", prev_mode, mode, reason)

    # Observers — each is individually safe and independent. A failure in
    # one never blocks the others or affects the core mode. All skipped
    # entirely when market is CLOSED (no meaningful data to classify).
    if mode == MarketMode.CLOSED:
        _current_breadth = "UNKNOWN"
        _current_breadth_detail = "market closed"
        _current_rsi_regime = "UNKNOWN"
        _current_rsi_detail = "market closed"
        _current_rsi_per_ticker = {}
        _current_ticker_pnl = {}
        _current_ticker_red = []
        _current_ticker_extremes = []
        return

    try:
        _current_breadth, _current_breadth_detail = _classify_breadth()
    except Exception:
        logger.exception("breadth observer failed (ignored)")
        _current_breadth, _current_breadth_detail = ("UNKNOWN", "observer crashed")
    if _current_breadth != prev_breadth:
        logger.info("MarketMode.breadth: %s -> %s (%s)",
                    prev_breadth, _current_breadth, _current_breadth_detail)

    try:
        rsi_label, rsi_detail, rsi_map = _classify_rsi_regime()
        _current_rsi_regime      = rsi_label
        _current_rsi_detail      = rsi_detail
        _current_rsi_per_ticker  = rsi_map
    except Exception:
        logger.exception("RSI observer failed (ignored)")
        _current_rsi_regime, _current_rsi_detail, _current_rsi_per_ticker = (
            "UNKNOWN", "observer crashed", {})
    if _current_rsi_regime != prev_rsi:
        logger.info("MarketMode.rsi: %s -> %s (%s)",
                    prev_rsi, _current_rsi_regime, _current_rsi_detail)

    try:
        _current_ticker_pnl = _per_ticker_today_pnl()
        red, extremes = _classify_ticker_heat(_current_ticker_pnl,
                                              _current_rsi_per_ticker)
        _current_ticker_red      = red
        _current_ticker_extremes = extremes
    except Exception:
        logger.exception("ticker-heat observer failed (ignored)")
        _current_ticker_pnl = {}
        _current_ticker_red = []
        _current_ticker_extremes = []


# Scan pause (Feature 8)
_scan_paused: bool = False
_regime_bullish = None          # None=unknown, True/False tracks last known regime
_last_exit_time: dict = {}     # ticker -> datetime (UTC) of last exit
_last_scan_time = None           # datetime (UTC), updated each scan cycle

# User config
user_config: dict = {"trading_mode": "paper"}

# Thread safety
_paper_save_lock = threading.Lock()


# ============================================================
# NOTIFICATION ROUTING HELPER (Fix B)
# ============================================================


# ============================================================
# STATE PERSISTENCE
# ============================================================
def save_paper_state():
    """Persist paper trading + strategy state to disk. Thread-safe, atomic."""
    t0 = time.time()
    if not _state_loaded:
        logger.warning("save_paper_state skipped — state not yet loaded")
        return
    # Data-loss guard: warn if history empty but cash changed (trades
    # happened then vanished). v3.3.1: also check for currently-open
    # positions — a short entry credits cash immediately but only
    # appends to short_trade_history on COVER, so an open-short session
    # is a legitimate state with empty history and moved cash. Only
    # warn when there's no record of ANY activity (no history AND no
    # open positions) yet cash has moved.
    has_any_activity = (
        bool(trade_history)
        or bool(short_trade_history)
        or bool(positions)
        or bool(short_positions)
    )
    if (not has_any_activity) and paper_cash != PAPER_STARTING_CAPITAL:
        logger.warning(
            "DATA LOSS GUARD: no trade history or open positions but "
            "cash=$%.2f (start=$%.0f) — possible trade history wipe!",
            paper_cash, PAPER_STARTING_CAPITAL,
        )
    state = {
        "paper_cash": paper_cash,
        "positions": positions,
        "paper_trades": paper_trades,
        "paper_all_trades": paper_all_trades[-500:],
        "daily_entry_count": daily_entry_count,
        "daily_entry_date": daily_entry_date,
        "or_high": or_high,
        "or_low": or_low,
        "pdc": pdc,
        "or_collected_date": or_collected_date,
        "user_config": user_config,
        "trade_history": trade_history,
        "short_positions": short_positions,
        "short_trade_history": short_trade_history[-500:],
        # v3.4.34: avwap_data / avwap_last_ts no longer persisted.
        "daily_short_entry_count": daily_short_entry_count,
        "last_exit_time": {k: v.isoformat() for k, v in _last_exit_time.items()},
        "_scan_paused": _scan_paused,
        "_trading_halted": _trading_halted,
        "_trading_halted_reason": _trading_halted_reason,
        "saved_at": _utc_now_iso(),
    }
    with _paper_save_lock:
        tmp = PAPER_STATE_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, default=str)
            os.replace(tmp, PAPER_STATE_FILE)
            logger.debug("Paper state saved -> %s (%.3fs)", PAPER_STATE_FILE, time.time() - t0)
        except Exception as e:
            logger.error("save_paper_state failed: %s", e)


def load_paper_state():
    """Load paper trading state from disk on startup."""
    global paper_cash, positions, paper_trades, paper_all_trades
    global daily_entry_count, daily_entry_date
    global or_high, or_low, pdc, or_collected_date
    global user_config
    global trade_history
    global short_positions, short_trade_history
    global daily_short_entry_count
    global _last_exit_time, _state_loaded
    global _scan_paused, _trading_halted, _trading_halted_reason

    if not os.path.exists(PAPER_STATE_FILE):
        paper_log("No saved state at %s. Starting fresh $%.0f."
                  % (PAPER_STATE_FILE, PAPER_STARTING_CAPITAL))
        _state_loaded = True
        return

    try:
        with open(PAPER_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        paper_cash = float(state.get("paper_cash", PAPER_STARTING_CAPITAL))
        positions.update(state.get("positions", {}))
        paper_trades.clear()
        paper_trades.extend(state.get("paper_trades", []))
        paper_all_trades.clear()
        paper_all_trades.extend(state.get("paper_all_trades", []))
        daily_entry_count.update(state.get("daily_entry_count", {}))
        daily_entry_date = state.get("daily_entry_date", "")
        or_high.update(state.get("or_high", {}))
        or_low.update(state.get("or_low", {}))
        pdc.update(state.get("pdc", {}))
        or_collected_date = state.get("or_collected_date", "")
        user_config.update(state.get("user_config", {}))
        trade_history.clear()
        trade_history.extend(state.get("trade_history", []))
        short_positions.update(state.get("short_positions", {}))
        short_trade_history.clear()
        short_trade_history.extend(state.get("short_trade_history", []))
        # v3.4.34: legacy "avwap_data"/"avwap_last_ts" keys in old
        # state files are silently ignored (no longer loaded).
        daily_short_entry_count.update(state.get("daily_short_entry_count", {}))
        raw_exit = state.get("last_exit_time", {})
        # Normalize to UTC-aware. Older persisted state may contain
        # tz-naive ISO strings; mixing those with tz-aware datetime.now
        # raises "can't subtract offset-naive and offset-aware" and kills
        # entry checks silently. Assume naive == UTC (the original write
        # site has always used datetime.now(timezone.utc)).
        def _parse_exit_ts(v):
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        _last_exit_time = {k: _parse_exit_ts(v) for k, v in raw_exit.items()}

        # Load persisted flags
        _scan_paused = state.get("_scan_paused", False)
        _trading_halted = state.get("_trading_halted", False)
        _trading_halted_reason = state.get("_trading_halted_reason", "")

        # Reset daily counts if saved on a different day
        today = _now_et().strftime("%Y-%m-%d")
        if daily_entry_date != today:
            daily_entry_count.clear()
            daily_short_entry_count.clear()
            paper_trades.clear()
            _trading_halted = False
            _trading_halted_reason = ""

        _state_loaded = True
        logger.info("Loaded paper state: cash=$%.2f, %d positions, %d trade_history",
                    paper_cash, len(positions), len(trade_history))
    except Exception as e:
        _state_loaded = True  # allow saves after failed load (fresh start)
        logger.error("load_paper_state failed: %s — starting fresh", e)




# ============================================================
# v3.4.27 — PERSISTENT TRADE LOG (append-only JSONL)
# ============================================================
# Every closed trade (longs via close_position, shorts via
# close_short_position, and their TP counterparts) writes one JSON
# line to TRADE_LOG_FILE. The file lives on the Railway volume so it
# survives redeploys. Append-only — never rewritten, never rotated
# (a year of typical volume is ~3 MB).
#
# Schema (v1):
#   schema_version: int       — 1
#   bot_version:    str       — BOT_VERSION at write time
#   date:           str       — YYYY-MM-DD (trade close date, ET)
#   portfolio:      str       — "paper" | "tp"
#   ticker:         str
#   side:           str       — "LONG" | "SHORT"
#   shares:         int
#   entry_price:    float
#   exit_price:     float
#   entry_time:     str       — HH:MM:SS or ISO (as stored)
#   exit_time:      str       — ISO-8601 UTC
#   hold_seconds:   float|null
#   pnl:            float     — signed dollars
#   pnl_pct:        float     — signed percent (0.23 = +0.23%)
#   reason:         str       — EOD | TRAIL | STOP | RETRO_CAP |
#                               BULL_VACUUM | LORDS_LEFT |
#                               BULL_VACUUM[5m] | LORDS_LEFT[5m] |
#                               ...
#   entry_num:      int       — add-on index (longs only; 1 for shorts)
#   trail_active_at_exit:   bool|null
#   trail_stop_at_exit:     float|null
#   trail_anchor_at_exit:   float|null  (trail_high for long, trail_low for short)
#   hard_stop_at_exit:      float|null
#   effective_stop_at_exit: float|null  (trail_stop if armed, else hard stop)
#
# All writes are best-effort: any IO error is logged and swallowed so
# a broken disk never breaks trade execution.
# ============================================================

TRADE_LOG_SCHEMA_VERSION = 1
_trade_log_lock = threading.Lock()
_trade_log_last_error = None  # surfaced via /api/state for visibility


def _trade_log_snapshot_pos(pos):
    """Extract trail + stop diagnostic fields from a position dict.

    Accepts both long (trail_high) and short (trail_low) shapes.
    Returns a dict of None-safe values. Used at close time so the
    row captures exactly what the exit decision saw.
    """
    if not isinstance(pos, dict):
        return {
            "trail_active_at_exit": None,
            "trail_stop_at_exit": None,
            "trail_anchor_at_exit": None,
            "hard_stop_at_exit": None,
            "effective_stop_at_exit": None,
        }
    trail_active = bool(pos.get("trail_active", False))
    trail_stop = pos.get("trail_stop")
    # Either long (trail_high) or short (trail_low) populates anchor.
    trail_anchor = pos.get("trail_high", pos.get("trail_low"))
    hard_stop = pos.get("stop")
    effective_stop = (
        trail_stop if (trail_active and trail_stop is not None) else hard_stop
    )
    def _as_float(v):
        return float(v) if v is not None else None
    return {
        "trail_active_at_exit": trail_active,
        "trail_stop_at_exit": _as_float(trail_stop),
        "trail_anchor_at_exit": _as_float(trail_anchor),
        "hard_stop_at_exit": _as_float(hard_stop),
        "effective_stop_at_exit": _as_float(effective_stop),
    }


def trade_log_append(row):
    """Append a single closed-trade row to the persistent trade log.

    Best-effort: failures are logged and swallowed, never raised. The
    lock guards against the (rare) case of two close paths firing at
    once — writes are atomic at the OS level for small lines on
    POSIX, but the lock keeps log order deterministic and protects
    the _trade_log_last_error surface from races.
    """
    global _trade_log_last_error
    # Defensive: never let a caller ship missing required fields.
    required = ("ticker", "side", "pnl", "reason")
    for f in required:
        if f not in row:
            _trade_log_last_error = f"missing field: {f}"
            logger.warning("[TRADE_LOG] skipping row missing %s: %s",
                           f, row)
            return False
    full = {
        "schema_version": TRADE_LOG_SCHEMA_VERSION,
        "bot_version": BOT_VERSION,
    }
    full.update(row)
    line = json.dumps(full, default=str, separators=(",", ":"))
    try:
        with _trade_log_lock:
            # Open append+ with explicit newline to keep JSONL clean.
            with open(TRADE_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        _trade_log_last_error = None
        return True
    except OSError as e:
        _trade_log_last_error = f"{type(e).__name__}: {e}"
        logger.error(
            "[TRADE_LOG] append failed (%s). Path=%s. Trade still "
            "executed — only persistence failed.",
            e, TRADE_LOG_FILE,
        )
        return False


def trade_log_read_tail(limit=500, since_date=None, portfolio=None):
    """Read the tail of the trade log, optionally filtered.

    Returns a list of dicts, newest-last (same order as on disk).
    Filtering is applied AFTER reading — trade log is small enough
    that this is fine. Failures return an empty list; never raises.

    Args:
      limit:       max rows to return (newest)
      since_date:  optional "YYYY-MM-DD"; only rows with date >= this
      portfolio:   optional "paper" or "tp" filter
    """
    if not os.path.exists(TRADE_LOG_FILE):
        return []
    try:
        with open(TRADE_LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        logger.error("[TRADE_LOG] read failed: %s", e)
        return []
    rows = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except json.JSONDecodeError:
            # Defensively skip corrupted lines rather than blowing up
            # the whole read.
            continue
    if since_date:
        rows = [r for r in rows if r.get("date", "") >= since_date]
    if portfolio:
        rows = [r for r in rows if r.get("portfolio") == portfolio]
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    return rows


# ============================================================
# TELEGRAM MESSAGING
# ============================================================
def send_telegram(text, chat_id=None):
    """Send text message to Telegram. Splits long messages. Retries on 429."""
    cid = chat_id or CHAT_ID
    if not text or not text.strip() or not TELEGRAM_TOKEN or not cid:
        return

    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > 3800:
            if current:
                parts.append(current.rstrip())
            current = line
        else:
            current += line
    if current:
        parts.append(current.rstrip())

    total = len(parts)
    for i, part in enumerate(parts, 1):
        prefix = "%d/%d " % (i, total) if total > 1 else ""
        payload = json.dumps({"chat_id": cid, "text": prefix + part}).encode()
        url = "https://api.telegram.org/bot%s/sendMessage" % TELEGRAM_TOKEN
        for attempt in range(5):
            try:
                req = urllib.request.Request(
                    url, data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    status = resp.status
                if status == 429:
                    wait = 2 ** attempt
                    logger.warning("Telegram 429 — sleeping %ds", wait)
                    time.sleep(wait)
                    continue
                time.sleep(0.3)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = 2 ** attempt
                    logger.warning("Telegram 429 — sleeping %ds", wait)
                    time.sleep(wait)
                    continue
                logger.error("Telegram send error (attempt %d): %s", attempt + 1, e)
                time.sleep(2 ** attempt)
            except Exception as e:
                logger.error("Telegram send error (attempt %d): %s", attempt + 1, e)
                time.sleep(2 ** attempt)




# ============================================================
# YAHOO FINANCE DATA
# ============================================================
# Per-scan-cycle cache for 1-min bars. scan_loop() calls
# _clear_cycle_bar_cache() at the start of each cycle; any call to
# fetch_1min_bars within the same cycle reuses the cached response.
# This lets observers (RSI, breadth) read the same bars the scan loop
# already fetched without doubling network calls.
_cycle_bar_cache: dict = {}


def _clear_cycle_bar_cache():
    """Reset the per-cycle bar cache. Called at the top of scan_loop()."""
    _cycle_bar_cache.clear()


def fetch_1min_bars(ticker):
    """Fetch 1-min intraday bars from Yahoo Finance.

    Returns dict with keys: timestamps, opens, highs, lows, closes,
    volumes, current_price, pdc.  Returns None on failure.

    Results are cached per scan cycle (see _cycle_bar_cache).
    """
    cached = _cycle_bar_cache.get(ticker)
    if cached is not None:
        # Sentinel for negative cache (prior fetch failed): keep returning
        # None for the rest of the cycle rather than retrying.
        return cached if cached != "__FAILED__" else None

    t0 = time.time()
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/%s"
        "?interval=1m&range=1d&includePrePost=false" % ticker
    )
    try:
        req = urllib.request.Request(url, headers=YAHOO_HEADERS)
        with urllib.request.urlopen(req, timeout=YAHOO_TIMEOUT) as resp:
            data = json.loads(resp.read())

        result = data.get("chart", {}).get("result")
        if not result:
            logger.debug("Yahoo %s: empty result (%.2fs)", ticker, time.time() - t0)
            _cycle_bar_cache[ticker] = "__FAILED__"
            return None
        r = result[0]
        meta = r.get("meta", {})
        quote = r.get("indicators", {}).get("quote", [{}])[0]
        timestamps = r.get("timestamp", [])

        if not timestamps:
            logger.debug("Yahoo %s: no timestamps (%.2fs)", ticker, time.time() - t0)
            _cycle_bar_cache[ticker] = "__FAILED__"
            return None

        logger.debug("Yahoo %s: %.2fs", ticker, time.time() - t0)
        out = {
            "timestamps": timestamps,
            "opens": quote.get("open", []),
            "highs": quote.get("high", []),
            "lows": quote.get("low", []),
            "closes": quote.get("close", []),
            "volumes": quote.get("volume", []),
            "current_price": meta.get("regularMarketPrice", 0),
            "pdc": (meta.get("previousClose")
                    or meta.get("chartPreviousClose")
                    or 0),
        }
        _cycle_bar_cache[ticker] = out
        return out
    except Exception as e:
        logger.debug("fetch_1min_bars %s failed: %s (%.2fs)", ticker, e, time.time() - t0)
        _cycle_bar_cache[ticker] = "__FAILED__"
        return None


def get_last_1min_close(ticker):
    """Return the close price of the most recently completed 1-min bar.

    Uses the existing Yahoo Finance fetch.  The last element in the closes
    array may be the currently-forming bar, so we prefer the second-to-last
    entry when available.  Returns None on any failure (fail-safe).
    """
    bars = fetch_1min_bars(ticker)
    if not bars:
        return None
    closes = [c for c in bars.get("closes", []) if c is not None]
    if len(closes) >= 2:
        return closes[-2]          # last completed bar
    if len(closes) == 1:
        return closes[-1]          # only one bar — best we have
    return None


# ============================================================
# FMP REAL-TIME QUOTES
# ============================================================
def get_fmp_quote(ticker):
    """Fetch real-time quote from FMP. Returns dict or None on error."""
    t0 = time.time()
    try:
        url = (
            "https://financialmodelingprep.com/stable/quote"
            "?symbol=%s&apikey=%s" % (ticker, FMP_API_KEY)
        )
        req = urllib.request.Request(url, headers=YAHOO_HEADERS)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        if data and isinstance(data, list) and len(data) > 0:
            logger.debug("FMP %s: %.2fs", ticker, time.time() - t0)
            return data[0]
    except Exception as e:
        logger.warning("FMP quote error for %s: %s (%.2fs)", ticker, e, time.time() - t0)
    return None


# v4.0.3-beta — env-tunable staleness guard threshold. The old 1.5%
# fired for routine intraday moves on volatile names (OKLO, QBTS,
# LEU regularly drift >5% within a session) which killed every
# signal. 5% is a real "something's broken" guard, not a "normal
# volatility" guard.
OR_STALE_THRESHOLD = float(os.getenv("OR_STALE_THRESHOLD", "0.05") or "0.05")


def _or_price_sane(or_price, live_price, threshold=None):
    """Return True if OR price is within threshold of live price.

    threshold defaults to OR_STALE_THRESHOLD (env-configurable,
    5% by default). Pass an explicit value to override.
    """
    if threshold is None:
        threshold = OR_STALE_THRESHOLD
    if not or_price or not live_price:
        return True  # can't validate, allow
    diff = abs(or_price - live_price) / live_price
    return diff <= threshold


def _entry_bar_volume(volumes, lookback=5):
    """Pick the most recent closed bar's volume, walking back through
    null/zero entries that indicate the data source hasn't populated
    the bar yet (seen when Yahoo returns a fresh series where the last
    closed bar is still settling).

    Convention: volumes[-1] is the in-progress bar, volumes[-2] is the
    most recently closed bar. Start there and walk back up to
    `lookback` bars, returning the first non-null, positive value.

    Returns (vol, ready):
      - (vol, True)  when a valid bar was found
      - (0,   False) when every candidate bar was null/zero — caller
                     must treat this as DATA NOT READY, NOT as low-vol.

    Failure-closed: a DATA NOT READY result must cause the caller to
    skip the entry attempt. This keeps behavior no looser than baseline
    (a missing-data bar never entered a trade before this fix either).
    """
    if not volumes or len(volumes) < 2:
        return 0, False
    # Walk back from volumes[-2] (last closed bar) through `lookback`
    # prior bars. Index range: [-2, -3, ..., -2-(lookback-1)].
    for offset in range(2, 2 + lookback):
        if offset > len(volumes):
            break
        v = volumes[-offset]
        if v is not None and v > 0:
            return v, True
    return 0, False


# v3.4.21 — Stop cap for late/extended entries.
#
# Baseline stop = OR_High − $0.90 (long) or PDC + $0.90 (short). That
# anchor is appropriate when price breaks at the OR trigger, but on a
# bar that closes well past the level the baseline stop sits far below
# (or above) the entry, inflating risk. Example from v3.4.20: MSFT long
# entered at $425.93 vs OR_High $420.16, baseline stop $419.26 = $6.67
# risk = −1.56% on entry.
#
# Cap: stop distance must not exceed MAX_STOP_PCT of the entry price.
# Final stop = tighter of {baseline, entry ± MAX_STOP_PCT}.
# Invariant (locked design principle): cap can only TIGHTEN the stop,
# never loosen it — a stop closer to entry than baseline is always
# more conservative for both long and short.
MAX_STOP_PCT = 0.0075  # 0.75% max from entry

# v3.4.25 — Breakeven ratchet (Stage 1)
# ----------------------------------------------------------------
# Once a position is in profit by BREAKEVEN_RATCHET_PCT, pull the
# stop to entry price (breakeven). This closes the gap between the
# fixed 0.75% entry cap and the 1% trail-arm threshold — without it,
# a short that moves +0.8% in our favor still has its stop pinned
# 0.75% above entry (i.e., 1.58% above current market), so a wick
# back would give back ~2x the current profit.
#
# Locked design preserved:
#   - MORE conservative than baseline, never looser. Breakeven is
#     always tighter than entry±0.75% by construction.
#   - Fail-closed: missing data → no ratchet, leave existing stop
#     alone.
#   - Trail interaction: if trail is already armed, ratchet is a
#     no-op (trail is ≥ as tight as breakeven already).
BREAKEVEN_RATCHET_PCT = 0.0050  # +0.50% profit arms breakeven


def _breakeven_long_stop(entry_price, current_price, current_stop,
                         arm_pct=BREAKEVEN_RATCHET_PCT):
    """Return the ratcheted long stop, or the existing stop unchanged.

    A long is in +arm_pct profit when current_price ≥ entry * (1+arm_pct).
    When armed, the stop pulls up to entry (breakeven). We return
    max(current_stop, entry) so the ratchet can only tighten.

    Returns (new_stop, armed). `armed` is True if the threshold is
    met, regardless of whether the stop actually moved (it may
    already be at or above entry).
    """
    arm_price = entry_price * (1.0 + arm_pct)
    if current_price < arm_price:
        return current_stop, False
    # Armed — stop can never go below entry (never looser).
    new_stop = round(max(current_stop, entry_price), 2)
    return new_stop, True


def _breakeven_short_stop(entry_price, current_price, current_stop,
                          arm_pct=BREAKEVEN_RATCHET_PCT):
    """Return the ratcheted short stop, or the existing stop unchanged.

    A short is in +arm_pct profit when current_price ≤ entry * (1−arm_pct).
    When armed, the stop pulls down to entry. We return
    min(current_stop, entry) so the ratchet can only tighten.
    """
    arm_price = entry_price * (1.0 - arm_pct)
    if current_price > arm_price:
        return current_stop, False
    new_stop = round(min(current_stop, entry_price), 2)
    return new_stop, True


def _capped_long_stop(or_high_val, entry_price, max_pct=MAX_STOP_PCT):
    """Compute long stop with 0.75%-from-entry cap.

    Returns (stop_price, capped, baseline_stop) — `capped` is True when
    the entry-relative floor was tighter than the OR baseline.
    """
    baseline = or_high_val - 0.90
    floor = entry_price * (1.0 - max_pct)
    # For longs, "tighter" = higher stop (closer to entry from below).
    final = max(baseline, floor)
    return round(final, 2), final > baseline, round(baseline, 2)


def _capped_short_stop(pdc_val, entry_price, max_pct=MAX_STOP_PCT):
    """Compute short stop with 0.75%-from-entry cap.

    Returns (stop_price, capped, baseline_stop). For shorts, "tighter"
    = lower stop (closer to entry from above).
    """
    baseline = pdc_val + 0.90
    ceiling = entry_price * (1.0 + max_pct)
    final = min(baseline, ceiling)
    return round(final, 2), final < baseline, round(baseline, 2)


# v3.4.36 — Profit-Lock Ladder (peak-anchored give-back)
# ----------------------------------------------------------------
# Six-tier ratchet driven by peak gain %. Peak is trail_high for
# long, trail_low for short. v3.4.35's gain-anchored tiers (entry +
# X%) made the gap between peak and stop WIDEN as peak grew — the
# opposite of the trailing-stop instinct. v3.4.36 inverts this: the
# stop sits a shrinking % below peak, so the tighter the trade
# works, the less give-back is allowed.
#
#   Peak gain %  Long give-back  Short give-back  Phase
#   -----------  --------------  ---------------  -------
#   < 1.0%       initial stop    initial stop     Bullet
#   ≥ 1.0%      peak − 0.50%    peak + 0.50%     Arm
#   ≥ 2.0%      peak − 0.40%    peak + 0.40%     Lock
#   ≥ 3.0%      peak − 0.30%    peak + 0.30%     Tight
#   ≥ 4.0%      peak − 0.20%    peak + 0.20%     Tighter
#   ≥ 5.0%      peak − 0.10%    peak + 0.10%     Harvest
#
# Design:
#   - PEAK-ANCHORED: stop is always defined as a % below peak (for
#     long) or above peak (for short). As peak ratchets up, the stop
#     ratchets up with it; the gap between them shrinks at higher
#     tiers.
#   - ONE-WAY: the returned stop is always max(existing_trail, tier)
#     for longs / min(existing_trail, tier) for shorts — never
#     looser. If a pullback happens, trail_high doesn't move and the
#     stop holds exactly where it was.
#   - SUB-1% TIER: returns `initial_stop` (the OR-based structural
#     stop). Legacy positions without initial_stop fall back to
#     pos["stop"].
#   - NEVER LOOSER THAN INITIAL: final result is clamped by
#     max(tier_stop, initial_stop) for long — the structural stop is
#     a permanent floor. Mirrors with min(...) for short.
LADDER_TIERS_LONG = [
    # (peak_gain_trigger, give_back_pct_below_peak)
    (0.05, 0.0010),   # ≥ 5% → peak − 0.10% (Harvest)
    (0.04, 0.0020),   # ≥ 4% → peak − 0.20% (Tighter)
    (0.03, 0.0030),   # ≥ 3% → peak − 0.30% (Tight)
    (0.02, 0.0040),   # ≥ 2% → peak − 0.40% (Lock)
    (0.01, 0.0050),   # ≥ 1% → peak − 0.50% (Arm)
]
# v3.4.35 had a separate LADDER_HARVEST_FRACTION; v3.4.36 rolls that
# concept into the tier table (the ≥5% tier is just the tightest
# give-back). Alias kept so any external readers don't crash; value is
# now the ≥5% give-back fraction itself.
LADDER_HARVEST_FRACTION = 0.0010


def _ladder_stop_long(pos):
    """Return the profit-lock ladder stop for a long position.

    Uses pos["trail_high"] as the peak. Stop is peak − give_back%
    where give_back shrinks as peak grows. Below +1% peak, returns
    `initial_stop` (structural stop only). Falls back to pos["stop"]
    when initial_stop is absent (legacy positions).

    Never looser than `initial_stop` — returns max(tier_stop,
    initial_stop) so the structural floor is permanent.
    """
    entry = pos.get("entry_price") or 0.0
    if entry <= 0:
        return pos.get("stop", 0)
    peak = pos.get("trail_high", entry) or entry
    peak_gain_pct = (peak - entry) / entry
    initial = pos.get("initial_stop", pos.get("stop", 0))
    # Iterate highest tier first so first match wins.
    for trigger, give_back_pct in LADDER_TIERS_LONG:
        if peak_gain_pct >= trigger:
            tier_stop = peak * (1.0 - give_back_pct)
            return round(max(tier_stop, initial), 2)
    # Below 1% gain — structural stop only.
    return initial


def _ladder_stop_short(pos):
    """Return the profit-lock ladder stop for a short position.

    Mirror of _ladder_stop_long. Uses pos["trail_low"] as the peak
    (lowest price reached). Peak gain % = (entry − low) / entry.
    Stop is peak + give_back% where give_back shrinks as peak
    deepens. Never looser (higher) than `initial_stop`.
    """
    entry = pos.get("entry_price") or 0.0
    if entry <= 0:
        return pos.get("stop", 0)
    peak = pos.get("trail_low", entry) or entry
    peak_gain_pct = (entry - peak) / entry
    initial = pos.get("initial_stop", pos.get("stop", 0))
    for trigger, give_back_pct in LADDER_TIERS_LONG:
        if peak_gain_pct >= trigger:
            tier_stop = peak * (1.0 + give_back_pct)
            # Tighter = lower for short, so take min with initial.
            return round(min(tier_stop, initial), 2)
    return initial


# ============================================================
# v3.4.23 — Retro-cap: retighten existing positions
# ------------------------------------------------------------
# The cap (v3.4.21) only fired at entry. Positions that were open
# before the cap shipped, or positions whose stop somehow got past
# the cap, still carried a potentially-wide baseline stop. This helper
# walks every open long/short position and enforces the 0.75% cap
# relative to entry. When the trail is already armed it is left alone
# (trail is always tighter than a fixed 0.75% cap by construction).
# When the newly-capped stop has already been breached by market
# price, we force the exit now with reason=RETRO_CAP rather than wait
# for the next scan — the cap is a hard risk ceiling, not a hint.
# Designed to be safe to call repeatedly: cycle-idempotent.
# ============================================================

def _retighten_long_stop(ticker, pos, current_price,
                         force_exit=True):
    """Retighten a single long position's stop.

    Two layers (cap + breakeven ratchet), applied based on trail state.

    When trail is NOT armed (v3.4.23 + v3.4.25 behavior):
      1. 0.75% cap: floor = entry * (1 − MAX_STOP_PCT).
      2. Breakeven ratchet: once current ≥ entry * (1+0.50%), pull
         pos["stop"] up to entry.

    When trail IS armed (v3.4.26 new behavior):
      Cap layer is skipped — trail was designed to replace it.
      Ratchet still runs but acts on pos["trail_stop"] instead of
      pos["stop"], because once trail is armed, manage_positions uses
      trail_stop for exit decisions. If the trail armed on an
      unfavorable dip (trail_low close to entry, trail_stop below
      entry), the ratchet pulls the effective exit stop up to entry.
      Pure tighten — never loosens.

    Returns one of:
      ("already_tight", stop, None) — nothing tightens further.
      ("tightened", old_stop, new)  — cap tightened pos["stop"].
      ("ratcheted", old_stop, new)  — ratchet tightened pos["stop"].
      ("ratcheted_trail", old_ts, new_ts)
                                    — ratchet tightened trail_stop
                                      while trail is armed.
      ("exit", new_stop, None)      — new stop breached; exited with
                                      reason=RETRO_CAP.
    """
    entry_price = pos["entry_price"]

    # v3.4.26 — trail-armed branch. Ratchet acts on trail_stop.
    if pos.get("trail_active"):
        current_trail = pos.get("trail_stop")
        if current_trail is None:
            # No trail_stop yet (shouldn't happen once armed, but
            # fail-safe) — leave it to manage_positions on next tick.
            return ("already_tight", pos["stop"], None)
        # Only fire ratchet if we're at or above the +0.50% arm.
        arm_price = entry_price * (1.0 + BREAKEVEN_RATCHET_PCT)
        if current_price < arm_price:
            return ("already_tight", current_trail, None)
        # Pure tighten: trail floor never falls below entry once armed.
        new_trail = round(max(current_trail, entry_price), 2)
        if new_trail <= current_trail:
            return ("already_tight", current_trail, None)
        old_trail = current_trail
        pos["trail_stop"] = new_trail
        logger.info(
            "[BREAKEVEN] %s LONG trail_stop ratcheted to entry: "
            "$%.2f → $%.2f (entry=$%.2f, current=$%.2f, "
            "trail_active=True)",
            ticker, old_trail, new_trail, entry_price, current_price,
        )
        return ("ratcheted_trail", old_trail, new_trail)

    current_stop = pos["stop"]

    # Layer 1: 0.75% cap (v3.4.23).
    floor = round(entry_price * (1.0 - MAX_STOP_PCT), 2)
    capped_stop = max(current_stop, floor)  # tighter = higher for long

    # Layer 2: breakeven ratchet (v3.4.25). Stacks on top of cap —
    # breakeven is always ≥ (entry − 0.75%), so this only tightens.
    ratcheted_stop, armed = _breakeven_long_stop(
        entry_price, current_price, capped_stop,
    )

    new_stop = ratcheted_stop
    if new_stop <= current_stop:
        return ("already_tight", current_stop, None)

    old_stop = current_stop
    pos["stop"] = new_stop
    # Classify which layer caused the tighten — informative logging.
    if armed and new_stop > floor:
        status = "ratcheted"
        logger.info(
            "[BREAKEVEN] %s LONG stop ratcheted to entry: $%.2f → $%.2f "
            "(entry=$%.2f, current=$%.2f)",
            ticker, old_stop, new_stop, entry_price, current_price,
        )
    else:
        status = "tightened"
        logger.info(
            "[RETRO_CAP] %s LONG stop tightened: $%.2f → $%.2f "
            "(entry=$%.2f, current=$%.2f)",
            ticker, old_stop, new_stop, entry_price, current_price,
        )
    # If the market has already broken the new stop, exit now.
    if force_exit and current_price <= new_stop:
        logger.warning(
            "[RETRO_CAP] %s LONG already breached at tighten time "
            "(current=$%.2f ≤ new_stop=$%.2f) — exiting immediately.",
            ticker, current_price, new_stop,
        )
        close_position(ticker, current_price, reason="RETRO_CAP")
        return ("exit", new_stop, None)
    return (status, old_stop, new_stop)


def _retighten_short_stop(ticker, pos, current_price,
                          force_exit=True):
    """Retighten a single short position's stop (cap + breakeven).

    Same return shape as _retighten_long_stop. For shorts, "tighter" =
    lower stop (closer to entry from above).

    v3.4.26: when trail_active=True, cap is skipped but the breakeven
    ratchet runs against pos["trail_stop"] — manage_short_positions
    uses trail_stop for exit decisions once armed.
    """
    entry_price = pos["entry_price"]

    # v3.4.26 — trail-armed branch. Ratchet acts on trail_stop.
    if pos.get("trail_active"):
        current_trail = pos.get("trail_stop")
        if current_trail is None:
            return ("already_tight", pos["stop"], None)
        arm_price = entry_price * (1.0 - BREAKEVEN_RATCHET_PCT)
        if current_price > arm_price:
            return ("already_tight", current_trail, None)
        # For shorts, tighter = lower. Cap at entry from above.
        new_trail = round(min(current_trail, entry_price), 2)
        if new_trail >= current_trail:
            return ("already_tight", current_trail, None)
        old_trail = current_trail
        pos["trail_stop"] = new_trail
        logger.info(
            "[BREAKEVEN] %s SHORT trail_stop ratcheted to entry: "
            "$%.2f → $%.2f (entry=$%.2f, current=$%.2f, "
            "trail_active=True)",
            ticker, old_trail, new_trail, entry_price, current_price,
        )
        return ("ratcheted_trail", old_trail, new_trail)

    current_stop = pos["stop"]

    # Layer 1: 0.75% cap (v3.4.23).
    ceiling = round(entry_price * (1.0 + MAX_STOP_PCT), 2)
    capped_stop = min(current_stop, ceiling)  # tighter = lower for short

    # Layer 2: breakeven ratchet (v3.4.25).
    ratcheted_stop, armed = _breakeven_short_stop(
        entry_price, current_price, capped_stop,
    )

    new_stop = ratcheted_stop
    if new_stop >= current_stop:
        return ("already_tight", current_stop, None)

    old_stop = current_stop
    pos["stop"] = new_stop
    if armed and new_stop < ceiling:
        status = "ratcheted"
        logger.info(
            "[BREAKEVEN] %s SHORT stop ratcheted to entry: $%.2f → $%.2f "
            "(entry=$%.2f, current=$%.2f)",
            ticker, old_stop, new_stop, entry_price, current_price,
        )
    else:
        status = "tightened"
        logger.info(
            "[RETRO_CAP] %s SHORT stop tightened: $%.2f → $%.2f "
            "(entry=$%.2f, current=$%.2f)",
            ticker, old_stop, new_stop, entry_price, current_price,
        )
    if force_exit and current_price >= new_stop:
        logger.warning(
            "[RETRO_CAP] %s SHORT already breached at tighten time "
            "(current=$%.2f ≥ new_stop=$%.2f) — exiting immediately.",
            ticker, current_price, new_stop,
        )
        close_short_position(ticker, current_price, "RETRO_CAP")
        return ("exit", new_stop, None)
    return (status, old_stop, new_stop)


def retighten_all_stops(force_exit=True, fetch_prices=True):
    """Retighten every open position's stop to the 0.75% cap.

    Returns a summary dict: {tightened: int, exited: int, no_op: int,
    already_tight: int, errors: int, details: list[dict]}

    Safe to call repeatedly — if all stops are already tight, it's a
    no-op. When fetch_prices is False, uses entry_price as a
    best-effort proxy for "current" (startup mode, before any scanner
    cycles have run).
    """
    # v3.4.25: separate counter for breakeven-ratchet tightenings, so
    # logging and /retighten output can distinguish cap vs ratchet.
    # v3.4.26: ratcheted_trail counts breakeven-ratchet tightenings
    # applied to trail_stop (when trail is armed).
    summary = {"tightened": 0, "ratcheted": 0, "ratcheted_trail": 0,
               "exited": 0, "no_op": 0, "already_tight": 0,
               "errors": 0, "details": []}

    def _current(ticker, fallback):
        if not fetch_prices:
            return fallback
        try:
            bars = fetch_1min_bars(ticker)
            if bars and bars.get("current_price"):
                return bars["current_price"]
        except Exception as e:
            logger.warning("[RETRO_CAP] %s fetch_1min_bars failed: %s",
                           ticker, e)
        return fallback

    # Longs (paper only)
    for ticker in list(positions.keys()):
        pos = positions.get(ticker)
        if not pos:
            continue
        try:
            cur = _current(ticker, pos["entry_price"])
            status, old, new = _retighten_long_stop(
                ticker, pos, cur, force_exit=force_exit,
            )
            key = "exited" if status == "exit" else status
            summary[key] = summary.get(key, 0) + 1
            summary["details"].append({
                "ticker": ticker, "side": "LONG",
                "status": status,
                "old_stop": old, "new_stop": new,
            })
        except Exception as e:
            summary["errors"] += 1
            logger.error("[RETRO_CAP] %s LONG failed: %s",
                         ticker, e, exc_info=True)

    # Shorts (paper only)
    for ticker in list(short_positions.keys()):
        pos = short_positions.get(ticker)
        if not pos:
            continue
        try:
            cur = _current(ticker, pos["entry_price"])
            status, old, new = _retighten_short_stop(
                ticker, pos, cur, force_exit=force_exit,
            )
            key = "exited" if status == "exit" else status
            summary[key] = summary.get(key, 0) + 1
            summary["details"].append({
                "ticker": ticker, "side": "SHORT",
                "status": status,
                "old_stop": old, "new_stop": new,
            })
        except Exception as e:
            summary["errors"] += 1
            logger.error("[RETRO_CAP] %s SHORT failed: %s",
                         ticker, e, exc_info=True)

    if (summary["tightened"] or summary["ratcheted"]
            or summary["ratcheted_trail"] or summary["exited"]):
        logger.info(
            "[RETRO_CAP] cycle summary: %d tightened, %d ratcheted, "
            "%d trail-ratcheted, %d exited, %d already-tight, "
            "%d no-op",
            summary["tightened"], summary["ratcheted"],
            summary["ratcheted_trail"], summary["exited"],
            summary["already_tight"], summary["no_op"],
        )
    return summary


# ============================================================
# OR COLLECTION (Opening Range)
# ============================================================
def collect_or():
    """Collect Opening Range data at 09:35 ET.

    For each ticker: find bars in [09:30, 09:35) ET, record max high as OR_High
    and previous day close as PDC.
    """
    global or_collected_date
    now_et = _now_et()
    today = now_et.strftime("%Y-%m-%d")

    if or_collected_date == today:
        logger.info("OR already collected for %s, skipping", today)
        return

    logger.info("Collecting Opening Range for %s ...", today)
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    or_end = now_et.replace(hour=9, minute=35, second=0, microsecond=0)
    open_ts = int(market_open.timestamp())
    end_ts = int(or_end.timestamp())

    for ticker in TICKERS:
        try:
            bars = fetch_1min_bars(ticker)
            if not bars:
                logger.warning("OR: No bars for %s", ticker)
                continue

            # Filter bars in [09:30, 09:35) window
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

            if max_high is None:
                logger.warning("OR: No bars in [09:30,09:35) for %s", ticker)
                continue

            or_high[ticker] = max_high
            if min_low is not None:
                or_low[ticker] = min_low
            pdc[ticker] = bars["pdc"]

            # FMP cross-check: prefer tighter (smaller) OR range
            fmp_q = get_fmp_quote(ticker)
            if fmp_q:
                fmp_high = fmp_q.get("dayHigh")
                fmp_low = fmp_q.get("dayLow")
                fmp_pdc = fmp_q.get("previousClose")
                if fmp_high and fmp_high < or_high[ticker]:
                    pct = abs(fmp_high - or_high[ticker]) / or_high[ticker] * 100
                    if pct > 2:
                        logger.info("OR FMP adj %s High: %.2f->%.2f (%.1f%%)",
                                    ticker, or_high[ticker], fmp_high, pct)
                        or_high[ticker] = fmp_high
                if fmp_low and ticker in or_low and fmp_low > or_low[ticker]:
                    pct = abs(fmp_low - or_low[ticker]) / or_low[ticker] * 100
                    if pct > 2:
                        logger.info("OR FMP adj %s Low: %.2f->%.2f (%.1f%%)",
                                    ticker, or_low[ticker], fmp_low, pct)
                        or_low[ticker] = fmp_low
                if fmp_pdc and fmp_pdc > 0:
                    pdc[ticker] = fmp_pdc

            or_low_val = or_low.get(ticker, 0)
            logger.info("OR collected: %s OR_high=%.2f OR_low=%.2f PDC=%.2f",
                        ticker, or_high[ticker], or_low_val, pdc[ticker])
        except Exception as e:
            logger.error("OR collection error for %s: %s", ticker, e)

    # ------ Retry missing tickers (3 attempts, 60s apart) ------
    OR_RETRY_MAX = 3
    for attempt in range(1, OR_RETRY_MAX + 1):
        missing = [t for t in TICKERS if t not in or_high]
        if not missing:
            break
        logger.info("OR retry %d/%d for: %s", attempt, OR_RETRY_MAX,
                     ", ".join(missing))
        time.sleep(60)
        for ticker in missing:
            try:
                bars = fetch_1min_bars(ticker)
                if not bars:
                    continue
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
                if max_high is None:
                    continue
                or_high[ticker] = max_high
                if min_low is not None:
                    or_low[ticker] = min_low
                pdc[ticker] = bars["pdc"]
                # FMP cross-check on retry too
                fmp_q = get_fmp_quote(ticker)
                if fmp_q:
                    fmp_high = fmp_q.get("dayHigh")
                    fmp_low = fmp_q.get("dayLow")
                    fmp_pdc = fmp_q.get("previousClose")
                    if fmp_high and fmp_high < or_high[ticker]:
                        or_high[ticker] = fmp_high
                    if fmp_low and ticker in or_low and fmp_low > or_low[ticker]:
                        or_low[ticker] = fmp_low
                    if fmp_pdc and fmp_pdc > 0:
                        pdc[ticker] = fmp_pdc
                logger.info("OR retry OK: %s OR_H=%.2f OR_L=%.2f",
                            ticker, or_high[ticker], or_low.get(ticker, 0))
            except Exception as e:
                logger.warning("OR retry failed for %s: %s", ticker, e)

    # ------ FMP fallback for anything still missing ------
    still_missing = [t for t in TICKERS if t not in or_high]
    for ticker in still_missing:
        try:
            fmp = get_fmp_quote(ticker)
            if fmp and fmp.get("dayHigh") and fmp.get("dayLow"):
                or_high[ticker] = fmp["dayHigh"]
                or_low[ticker] = fmp["dayLow"]
                if fmp.get("previousClose") and fmp["previousClose"] > 0:
                    pdc[ticker] = fmp["previousClose"]
                logger.warning("OR fallback to FMP for %s: high=%.2f low=%.2f",
                               ticker, fmp["dayHigh"], fmp["dayLow"])
        except Exception as e:
            logger.warning("OR FMP fallback failed for %s: %s", ticker, e)

    final_missing = [t for t in TICKERS if t not in or_high]
    if final_missing:
        logger.warning("OR FINAL: still missing after retries: %s",
                        ", ".join(final_missing))
        send_telegram("\u26a0\ufe0f OR missing after retries + FMP: %s"
                      % ", ".join(final_missing))

    or_collected_date = today
    save_paper_state()

    # Send summary
    lines = ["Opening Range Collected (%s):" % today]
    for t in TICKERS:
        if t in or_high:
            orl = or_low.get(t, 0)
            lines.append("  %s  OR_H=%.2f  OR_L=%.2f  PDC=%.2f"
                          % (t, or_high[t], orl, pdc.get(t, 0)))
        else:
            lines.append("  %s  MISSING" % t)
    send_telegram("\n".join(lines))


# ============================================================
# v3.4.34 — AVWAP fully removed
# ============================================================
# The AVWAP entry gates (check_entry, check_short_entry), the
# regime-change alert, the breadth observer (_classify_breadth),
# and the v3.2.0 dual-index 5-min AVWAP ejector (_dual_index_eject)
# were all superseded by the v3.4.28 Sovereign Regime Shield, which
# anchors on PDC via _sovereign_regime_eject. One anchor across
# entries, alerts, breadth, and ejects.
#
# Previously at this site: update_avwap(), _last_finalized_5min_close(),
# _dual_index_eject(). All callers migrated to pdc.get() + the 1-minute
# finalized-close helper below. Removed in v3.4.34.
# ============================================================


# ============================================================
# v3.4.28 — SOVEREIGN REGIME SHIELD (PDC-based eject)
# ============================================================
# Why: AVWAP is a rolling mean — it drifts intraday, so an AVWAP-
# cross eject can fire on slow sideways tape ("regime flim-flam")
# even though the true structural level (yesterday's close) is
# unchanged. PDC is a single static number per index per day, so
# a PDC cross is a hard structural break rather than a drift.
#
# Rule (same for both sides, mirrored):
#
#   Long  eject iff  SPY_1m_close  < SPY_PDC  AND QQQ_1m_close  < QQQ_PDC
#   Short eject iff  SPY_1m_close  > SPY_PDC  AND QQQ_1m_close  > QQQ_PDC
#
# Hysteresis (spec): divergence — one index above PDC, one below —
# means regime is UNCHANGED and no eject fires. We achieve this
# trivially by requiring the AND to hold on both closes.
#
# Bar cadence: previous FULLY-CLOSED 1-minute bar (the one ending
# at the most recent minute boundary), NOT the in-progress bar.
# Matches the spec: "wait for the 1-minute bar to finalize."
#
# Fail-closed: any missing input (no bars, no PDC, too few closes)
# → return False (do NOT eject). Locked design principle: fail-
# closed means stay in the trade; adaptive logic never loosens
# baseline, only tightens.
def _last_finalized_1min_close(ticker):
    """Close of the most recent FINALIZED 1-minute bar.

    fetch_1min_bars() returns the entire intraday series including
    the in-progress minute as the last element. We return the
    second-to-last close so the caller always sees a bar that is
    truly sealed (no more ticks can modify it).

    Returns None on insufficient data.
    """
    bars = fetch_1min_bars(ticker)
    if not bars:
        return None
    closes = [c for c in (bars.get("closes") or []) if c is not None]
    if len(closes) < 2:
        return None
    return closes[-2]


def _sovereign_regime_eject(side):
    """Dual-index 1m-close vs PDC eject gate with hysteresis.

    Args:
        side: 'long'  \u2192 True iff BOTH SPY_1m_close < SPY_PDC
                              AND QQQ_1m_close < QQQ_PDC
              'short' \u2192 True iff BOTH SPY_1m_close > SPY_PDC
                              AND QQQ_1m_close > QQQ_PDC

    Returns False (no eject) on ANY missing/ambiguous input,
    including the divergence case (SPY and QQQ on opposite sides
    of their respective PDCs). Both behaviors are intentional and
    enforce the hysteresis buffer from the spec.
    """
    if side not in ("long", "short"):
        return False

    spy_pdc = pdc.get("SPY")
    qqq_pdc = pdc.get("QQQ")
    if not spy_pdc or not qqq_pdc or spy_pdc <= 0 or qqq_pdc <= 0:
        # PDC not yet collected (pre-open cycle, or data fetch
        # failed). Stay-in-trade is the only safe default.
        return False

    spy_close = _last_finalized_1min_close("SPY")
    qqq_close = _last_finalized_1min_close("QQQ")
    if spy_close is None or qqq_close is None:
        return False  # <2 finalized 1-min bars yet

    if side == "long":
        # Eject longs only when BOTH indices close below PDC.
        # The AND naturally enforces the divergence hysteresis.
        return (spy_close < spy_pdc) and (qqq_close < qqq_pdc)
    else:
        # Mirror for shorts: BOTH above PDC.
        return (spy_close > spy_pdc) and (qqq_close > qqq_pdc)


# ============================================================
# ENTRY CHECK
# ============================================================
def check_entry(ticker):
    """Return (True, bars) if all entry conditions met, else (False, None)."""
    now_et = _now_et()
    today = now_et.strftime("%Y-%m-%d")

    # Reset daily entry counts if new day
    global daily_entry_date
    if daily_entry_date != today:
        daily_entry_count.clear()
        daily_entry_date = today

    # Timing gate: after 09:35 ET (OR window close + 2-bar confirm)
    market_open = now_et.replace(hour=9, minute=35, second=0, microsecond=0)
    if now_et < market_open:
        return False, None

    # Before EOD close (15:55)
    eod_time = now_et.replace(hour=15, minute=55, second=0, microsecond=0)
    if now_et >= eod_time:
        return False, None

    # OR data available
    if ticker not in or_high or ticker not in pdc:
        return False, None

    # Daily entry cap (max 5)
    if daily_entry_count.get(ticker, 0) >= 5:
        return False, None

    # Re-entry cooldown: 15 min after any exit on this ticker
    last_exit = _last_exit_time.get(ticker)
    if last_exit:
        elapsed = (datetime.now(timezone.utc) - last_exit).total_seconds()
        if elapsed < 900:
            mins_left = int((900 - elapsed) / 60) + 1
            logger.info("SKIP %s [COOLDOWN] %dm left", ticker, mins_left)
            return False, None

    # Per-ticker daily loss cap: skip if down > $50 on this ticker today (both sides)
    ticker_pnl_today = sum(
        (t.get("pnl") or 0) for t in trade_history
        if t.get("ticker") == ticker and _is_today(t.get("exit_time_iso") or t.get("entry_time_iso", ""))
    )
    ticker_pnl_today += sum(
        (t.get("pnl") or 0) for t in short_trade_history
        if t.get("ticker") == ticker and _is_today(t.get("exit_time_iso") or t.get("entry_time_iso", ""))
    )
    if ticker_pnl_today < -50.0:
        logger.info("SKIP %s [LOSS CAP] ticker P&L today: $%.2f", ticker, ticker_pnl_today)
        return False, None

    # v3.5.0 — paper-only. Per-ticker loss cap (-$50) stays as a signal
    # filter: if a ticker is structurally toxic today, the paper book
    # skips it.

    # Fetch current bar (Finnhub/Yahoo as fallback)
    bars = fetch_1min_bars(ticker)
    if not bars:
        return False, None

    current_price = bars["current_price"]
    closes = [c for c in bars["closes"] if c is not None]
    last_close = closes[-1] if closes else current_price

    # FMP primary quote — override price and PDC if available
    fmp_q = get_fmp_quote(ticker)
    if fmp_q:
        fmp_price = fmp_q.get("price")
        if fmp_price and fmp_price > 0:
            current_price = fmp_price
            last_close = fmp_price
        fmp_pdc = fmp_q.get("previousClose")
        if fmp_pdc and fmp_pdc > 0:
            pdc[ticker] = fmp_pdc

    # OR sanity check: OR High must be within OR_STALE_THRESHOLD of live price.
    if not _or_price_sane(or_high[ticker], current_price):
        pct = abs(or_high[ticker] - current_price) / current_price * 100
        or_stale_skip_count[ticker] = or_stale_skip_count.get(ticker, 0) + 1
        logger.warning(
            "SKIP %s long \u2014 OR High $%.2f is %.1f%% from live $%.2f (stale?)",
            ticker, or_high[ticker], pct, current_price
        )
        return False, None

    # v3.4.21 — compute gate values first, then record a dashboard
    # snapshot and check each gate. This preserves fail-closed semantics
    # (all returns remain as-is) while giving the UI a read-only view
    # of where each ticker currently stands.
    or_h_val = or_high[ticker]
    pdc_val_e = pdc[ticker]
    # v3.4.47 — 2-bar OR breakout confirmation (Tiger 2.0).
    # Both of the last two closed 1m closes must be above OR high.
    price_break = _tiger_two_bar_long(closes, or_h_val)
    polarity_ok = current_price > pdc_val_e

    volumes = bars.get("volumes", [])
    vol_pct = None
    vol_ok = False
    vol_ready_flag = True
    entry_bar_vol = 0.0
    avg_vol = 0.0
    if len(volumes) >= 5:
        valid_vols = [v for v in volumes[:-1] if v is not None and v > 0]
        avg_vol = sum(valid_vols) / len(valid_vols) if valid_vols else 0
        entry_bar_vol, vol_ready = _entry_bar_volume(volumes)
        vol_ready_flag = vol_ready
        if vol_ready and avg_vol > 0:
            vol_pct = (entry_bar_vol / avg_vol) * 100.0
            vol_ok = vol_pct >= 150.0

    # v3.5.x: dashboard gate snapshot is now written by
    # _update_gate_snapshot() at the top of each scan cycle, using the
    # canonical OR-envelope -> side mapping. The per-entry writes that
    # used to live here caused a last-writer-wins latch between the
    # LONG and SHORT paths.

    # Volume confirmation: entry bar volume >= 1.5x session average.
    # v3.4.20: walk back through null/zero bars before failing. Yahoo
    # sometimes returns the most-recent closed bar with volume not yet
    # populated; treating that as low-vol blocks every breakout. If no
    # valid bar is found in the lookback window, log DATA NOT READY
    # (distinct from LOW VOL) and skip — fail-closed, never enter on
    # missing data.
    # v3.4.47 — gated by TIGER_V2_REQUIRE_VOL (default False).
    # Gene's 2.0 protocol replaces the vol filter with DI+.
    if TIGER_V2_REQUIRE_VOL and len(volumes) >= 5:
        if not vol_ready_flag:
            logger.info("SKIP %s [DATA NOT READY] no closed bar with volume in last 5", ticker)
            # v3.4.21 — if price had already cleared OR High, note it.
            if price_break:
                _record_near_miss(
                    ticker=ticker, side="LONG", reason="DATA_NOT_READY",
                    close=round(last_close, 2), level=round(or_h_val, 2),
                    vol_bar=None, vol_avg=None, vol_pct=None,
                )
            return False, None
        if avg_vol > 0 and entry_bar_vol < avg_vol * 1.5:
            logger.info("SKIP %s [LOW VOL] entry bar %.0f vs avg %.0f", ticker, entry_bar_vol, avg_vol)
            # v3.4.21 — near-miss only if the price gate actually cleared.
            if price_break:
                _record_near_miss(
                    ticker=ticker, side="LONG", reason="LOW_VOL",
                    close=round(last_close, 2), level=round(or_h_val, 2),
                    vol_bar=int(entry_bar_vol), vol_avg=int(avg_vol),
                    vol_pct=round(vol_pct, 1) if vol_pct is not None else None,
                )
            return False, None

    # Breakout: 2 consecutive 1m closes above OR_High (Tiger 2.0)
    if not price_break:
        return False, None

    # Polarity: current price > PDC
    if not polarity_ok:
        return False, None

    # Index anchor: SPY > SPY_PDC and QQQ > QQQ_PDC
    # v3.4.34 — migrated from AVWAP to PDC so the entry gate matches
    # the v3.4.28 Sovereign Regime Shield ejector (same anchor on
    # both sides of the trade). Fail-closed on missing PDC.
    spy_bars = fetch_1min_bars("SPY")
    qqq_bars = fetch_1min_bars("QQQ")
    if not spy_bars or not qqq_bars:
        return False, None

    spy_pdc_val = pdc.get("SPY")
    qqq_pdc_val = pdc.get("QQQ")
    if not spy_pdc_val or not qqq_pdc_val or spy_pdc_val <= 0 or qqq_pdc_val <= 0:
        return False, None

    index_ok = (spy_bars["current_price"] > spy_pdc_val
                and qqq_bars["current_price"] > qqq_pdc_val)
    # v3.4.21 — update long index flag on the snapshot.
    snap = _gate_snapshot.get(ticker)
    if snap is not None and snap.get("side") == "LONG":
        snap["index"] = bool(index_ok)

    if spy_bars["current_price"] <= spy_pdc_val:
        return False, None
    if qqq_bars["current_price"] <= qqq_pdc_val:
        return False, None

    # v3.4.47 — DI+ gate (Tiger 2.0): DI+(5m,15) must exceed threshold.
    # Fail-closed: if DI data is not ready, skip (warmup in progress).
    di_plus, di_minus = tiger_di(ticker)
    if di_plus is None:
        logger.info(
            "SKIP %s [DI WARMUP] need %d+1 5m bars",
            ticker, DI_PERIOD,
        )
        return False, None
    if di_plus < TIGER_V2_DI_THRESHOLD:
        logger.info(
            "SKIP %s [DI+] %.1f < %d",
            ticker, di_plus, TIGER_V2_DI_THRESHOLD,
        )
        return False, None

    return True, bars


def paper_shares_for(price: float) -> int:
    """Dollar-sized paper order: floor(PAPER_DOLLARS_PER_ENTRY / price),
    min 1. Returns 0 only when price <= 0 (invalid).

    v3.4.45 — paper now sizes by notional like RH does, scaled to the
    $100k paper book (default $10k/entry vs RH's $1.5k/$25k). This
    fixes the old flat 10-share behavior that made $400 NVDA cost 80x
    more risk per entry than $5 QBTS.
    """
    if price <= 0:
        return 0
    return max(1, int(PAPER_DOLLARS_PER_ENTRY // price))




# ============================================================
# v3.5.0 — Paper-only entry path (RH path removed).
# ============================================================
# Paper remains on _trading_halted / execute_entry.


# ============================================================
# EXECUTE ENTRY (paper)
# ============================================================
def execute_entry(ticker, current_price):
    """Place a limit buy on the PAPER book only.

    v3.5.0: paper-only. Robinhood/TradersPost mirror has been removed.

    v3.4.45: share size is now dollar-based via paper_shares_for(price)
    — floor(PAPER_DOLLARS_PER_ENTRY / price), min 1 — instead of a flat
    10 shares. Entry is also gated on paper_cash so the book can't go
    negative.
    """
    global paper_cash, _trading_halted, _trading_halted_reason

    # Feature 2: Check daily loss limit
    now_et = _now_et()
    today_str = now_et.strftime("%Y-%m-%d")

    if _trading_halted:
        logger.info("Trading halted — skipping entry for %s", ticker)
        return

    today_pnl = sum(
        t["pnl"] for t in paper_trades
        if t.get("date") == today_str and t.get("action") == "SELL"
    )
    # Include closed short P&L in daily loss check
    today_pnl += sum(
        t["pnl"] for t in short_trade_history
        if t.get("date") == today_str
    )

    # Add unrealized P&L from open long positions
    for pos_ticker, pos in list(positions.items()):
        fmp = get_fmp_quote(pos_ticker)
        live_px = fmp.get("price", 0) if fmp else 0
        if live_px > 0:
            today_pnl += (live_px - pos["entry_price"]) * pos.get("shares", 10)

    # Add unrealized P&L from open short positions
    for pos_ticker, pos in list(short_positions.items()):
        fmp = get_fmp_quote(pos_ticker)
        live_px = fmp.get("price", 0) if fmp else 0
        if live_px > 0:
            today_pnl += (pos["entry_price"] - live_px) * pos.get("shares", 10)

    logger.info("Daily P&L check: $%.2f (limit $%.2f)", today_pnl, DAILY_LOSS_LIMIT)
    if today_pnl <= DAILY_LOSS_LIMIT:
        _trading_halted = True
        pnl_fmt = "%+.2f" % today_pnl
        limit_fmt = "%.2f" % DAILY_LOSS_LIMIT
        _trading_halted_reason = "Daily loss limit hit: $%s" % pnl_fmt
        halt_msg = (
            "STOP Trading halted — daily loss limit hit\n"
            "Today P&L: $%s\n"
            "Limit: $%s\n"
            "No new entries until tomorrow."
        ) % (pnl_fmt, limit_fmt)
        send_telegram(halt_msg)
        return

    limit_price = round(current_price + 0.02, 2)
    or_high_val = or_high.get(ticker, current_price)
    # v3.4.21 — cap stop at 0.75% below entry when OR baseline would
    # imply a looser stop (late/extended breakout bar).
    stop_price, _stop_capped, _stop_baseline = _capped_long_stop(
        or_high_val, current_price
    )
    if _stop_capped:
        logger.info(
            "%s stop capped: baseline=$%.2f -> capped=$%.2f (entry=$%.2f, %.2f%% cap)",
            ticker, _stop_baseline, stop_price, current_price, MAX_STOP_PCT * 100,
        )
    # v3.4.45 — dollar-sized paper entry. Shares scale with price so a
    # $400 NVDA and a $5 QBTS both put ~$10k at risk per fill (vs the
    # old flat 10 shares). Paper cash gate skips the entry if we can't
    # afford it; on a $100k book at $10k/entry this only trips after
    # ~10 concurrent fills, but it makes paper_cash a real ceiling.
    shares = paper_shares_for(current_price)
    cost = current_price * shares
    if shares <= 0:
        logger.warning("[paper] skip %s — invalid price $%.2f",
                       ticker, current_price)
        return
    if cost > paper_cash:
        logger.info(
            "[paper] skip %s — insufficient cash (need $%.2f, have $%.2f)",
            ticker, cost, paper_cash,
        )
        return

    entry_num = daily_entry_count.get(ticker, 0) + 1
    now_str = _now_cdt().strftime("%H:%M:%S")
    now_hhmm = _now_cdt().strftime("%H:%M CDT")
    now_date = now_et.strftime("%Y-%m-%d")

    positions[ticker] = {
        "entry_price": current_price,
        "shares": shares,
        "stop": stop_price,
        "initial_stop": stop_price,  # v3.4.35 — frozen; used for R fallback only
        "trail_active": False,
        "trail_high": current_price,
        "entry_count": entry_num,
        "entry_time": now_str,
        "date": now_date,
        "pdc": pdc.get(ticker, 0),
    }
    daily_entry_count[ticker] = entry_num

    # Paper accounting
    paper_cash -= cost
    trade = {
        "action": "BUY",
        "ticker": ticker,
        "price": current_price,
        "limit_price": limit_price,
        "shares": shares,
        "cost": cost,
        "stop": stop_price,
        "entry_num": entry_num,
        "time": now_hhmm,
        "date": now_date,
    }
    paper_trades.append(trade)
    paper_all_trades.append(trade)

    paper_log("BUY %s %d @ $%.2f (limit $%.2f) stop=$%.2f entry#%d"
              % (ticker, shares, current_price, limit_price, stop_price, entry_num))

    # Fix B: Paper BUY notification → send_telegram() ONLY
    or_h = or_high.get(ticker, 0)
    pdc_e = pdc.get(ticker, 0)
    SEP_E = "\u2500" * 34
    sig_lines = "Signal : ORB Breakout \u2191\n"
    sig_lines += "  1m close > OR High \u2713\n"
    sig_lines += "  Price > PDC \u2713\n"
    sig_lines += "  SPY > PDC \u2713\n"
    sig_lines += "  QQQ > PDC \u2713\n"
    # v3.4.21 — when stop is capped at entry-0.75%, label it so.
    stop_label = (
        "entry \u22120.75%" if _stop_capped else "OR_High-$0.90"
    )
    msg = (
        "\U0001f4c8 LONG ENTRY %s  #%d\n"
        "%s\n"
        "Price  : $%.2f  (limit $%.2f)\n"
        "Shares : %d   Cost: $%s\n"
        "Stop   : $%.2f  (%s)\n"
        "OR High: $%.2f   PDC: $%.2f\n"
        "%s"
        "Time   : %s\n"
        "%s"
    ) % (ticker, entry_num, SEP_E,
         current_price, limit_price,
         shares, format(cost, ",.2f"),
         stop_price, stop_label, or_h, pdc_e, sig_lines, now_hhmm, SEP_E)
    send_telegram(msg)

    save_paper_state()

    # v4.0.0-alpha — notify executor bots (Val, Gene) of the paper entry.
    _emit_signal({
        "kind": "ENTRY_LONG",
        "ticker": ticker,
        "price": float(current_price),
        "reason": "BREAKOUT",
        "timestamp_utc": _utc_now_iso(),
        "main_shares": int(shares),
    })


# ============================================================
# CLOSE POSITION
# ============================================================
def close_position(ticker, price, reason="STOP"):
    """Close position: remove, log P&L, send Telegram."""
    global paper_cash

    if ticker not in positions:
        return

    _last_exit_time[ticker] = datetime.now(timezone.utc)

    pos = positions.pop(ticker)
    entry_price = pos["entry_price"]
    shares = pos["shares"]
    pnl_val = (price - entry_price) * shares
    pnl_pct = ((price - entry_price) / entry_price * 100) if entry_price else 0
    now_et = _now_et()
    now_hhmm = _now_cdt().strftime("%H:%M CDT")
    now_date = now_et.strftime("%Y-%m-%d")

    # Compute entry time from position
    entry_time_str = pos.get("entry_time", "")
    entry_hhmm = _to_cdt_hhmm(entry_time_str) if entry_time_str else ""

    # Paper accounting
    proceeds = price * shares
    paper_cash += proceeds

    trade = {
        "action": "SELL",
        "ticker": ticker,
        "price": price,
        "shares": shares,
        "pnl": round(pnl_val, 2),
        "pnl_pct": round(pnl_pct, 2),
        "reason": reason,
        "entry_price": entry_price,
        "time": now_hhmm,
        "date": now_date,
    }
    paper_trades.append(trade)
    paper_all_trades.append(trade)

    # Feature 1: Append to trade_history
    history_record = {
        "ticker": ticker,
        "side": "long",
        "action": "SELL",
        "shares": shares,
        "entry_price": entry_price,
        "exit_price": price,
        "pnl": round(pnl_val, 2),
        "pnl_pct": round(pnl_pct, 2),
        "reason": reason,
        "entry_time": entry_hhmm,
        "exit_time": now_hhmm,
        "entry_time_iso": entry_time_str,
        "exit_time_iso": _utc_now_iso(),
        "entry_num": pos.get("entry_count", 1),
        "date": now_date,
    }
    trade_history.append(history_record)
    if len(trade_history) > TRADE_HISTORY_MAX:
        trade_history[:] = trade_history[-TRADE_HISTORY_MAX:]

    # v3.4.27 — persistent trade log (paper long close).
    _entry_iso = entry_time_str or ""
    _hold_s = None
    try:
        if _entry_iso:
            _ent_dt = datetime.fromisoformat(_entry_iso)
            if _ent_dt.tzinfo is None:
                _ent_dt = _ent_dt.replace(tzinfo=timezone.utc)
            _hold_s = (datetime.now(timezone.utc) - _ent_dt).total_seconds()
    except (TypeError, ValueError):
        _hold_s = None
    _log_row = {
        "date": now_date,
        "portfolio": "paper",
        "ticker": ticker,
        "side": "LONG",
        "shares": int(shares),
        "entry_price": float(entry_price),
        "exit_price": float(price),
        "entry_time": entry_time_str,
        "exit_time": _utc_now_iso(),
        "hold_seconds": _hold_s,
        "pnl": round(pnl_val, 2),
        "pnl_pct": round(pnl_pct, 2),
        "reason": reason,
        "entry_num": int(pos.get("entry_count", 1)),
    }
    _log_row.update(_trade_log_snapshot_pos(pos))
    trade_log_append(_log_row)

    paper_log("SELL %s %d @ $%.2f reason=%s pnl=$%.2f (%.1f%%)"
              % (ticker, shares, price, reason, pnl_val, pnl_pct))

    # Fix B: Paper EXIT → send_telegram() ONLY
    exit_emoji = "\u2705" if pnl_val >= 0 else "\u274c"
    entry_cost_val = round(entry_price * shares, 2)
    SEP_X = "\u2500" * 34
    reason_label = REASON_LABELS.get(reason, reason)
    if reason == "TRAIL":
        t_high = pos.get("trail_high", price)
        t_dist = max(round(t_high * 0.010, 2), 1.00)
        reason_label = "\U0001f3af Trail Stop (1.0%% / $%.2f)" % t_dist
    msg = (
        "%s EXIT %s\n"
        "%s\n"
        "Shares : %d\n"
        "Entry  : $%.2f  \u2192  $%.2f\n"
        "Cost   : $%s  \u2192  $%s\n"
        "P&L    : $%+.2f  (%+.1f%%)\n"
        "Reason : %s\n"
        "In: %s   Out: %s\n"
        "%s"
    ) % (exit_emoji, ticker, SEP_X,
         shares, entry_price, price,
         format(entry_cost_val, ",.2f"), format(proceeds, ",.2f"),
         pnl_val, pnl_pct, reason_label, entry_hhmm, now_hhmm, SEP_X)
    send_telegram(msg)

    save_paper_state()

    # v4.0.0-alpha — notify executor bots (Val, Gene) of the paper exit.
    _emit_signal({
        "kind": "EXIT_LONG",
        "ticker": ticker,
        "price": float(price),
        "reason": reason,
        "timestamp_utc": _utc_now_iso(),
        "main_shares": int(shares),
    })


# ============================================================
# MANAGE POSITIONS (stop + trail logic)
# ============================================================
def manage_positions():
    """Check stops and update trailing stops for all open positions."""
    tickers_to_close = []

    # v3.4.23 — enforce 0.75% entry cap on every open long position
    # before the regular stop/trail pass. This catches pre-cap positions
    # and any position whose stored stop has drifted wider than the cap.
    # Also fires immediate exit on positions that have already breached
    # the retro-tightened stop. Idempotent — fast when everything is
    # already tight.
    retighten_all_stops(force_exit=True, fetch_prices=True)

    # ── Sovereign Regime Shield (v3.4.28) ────────────────────────────────────
    # Exit all longs ONLY when BOTH SPY and QQQ have a finalized 1-min close
    # BELOW their respective Prior Day Close (PDC). PDC is one static price
    # per day — a cross of it is a structural break, not intraday drift.
    # AND-logic enforces divergence hysteresis: if only one index is below
    # PDC (or data is missing), regime is UNCHANGED. See v3.4.28 CHANGELOG.
    lords_left = _sovereign_regime_eject("long")

    for ticker in list(positions.keys()):
        bars = fetch_1min_bars(ticker)
        if not bars:
            continue

        current_price = bars["current_price"]
        pos = positions[ticker]

        # v3.4.35 — Stop hit. "TRAIL" when the ladder has ratcheted past
        # the initial structural stop (capital already safe), else "STOP"
        # (initial structural stop hit with no profit locked).
        if current_price <= pos["stop"]:
            reason = "TRAIL" if pos.get("trail_active") else "STOP"
            tickers_to_close.append((ticker, current_price, reason))
            continue

        # ── Sovereign Regime Shield: BOTH SPY+QQQ 1m_close < PDC ─────────────
        if lords_left:
            tickers_to_close.append((ticker, current_price, "LORDS_LEFT"))
            continue

        # ── Eye of the Tiger: "The Red Candle" — lost Daily Polarity ─────────
        # Fires when 1-min confirmed close < day open OR < PDC
        closes = [c for c in bars.get("closes", []) if c is not None]
        ticker_1min_close = closes[-2] if len(closes) >= 2 else (closes[-1] if closes else current_price)
        opens = [o for o in bars.get("opens", []) if o is not None]
        day_open = opens[0] if opens else None
        pos_pdc = pos.get("pdc") or pos.get("prev_close")
        lost_polarity = False
        if day_open is not None and ticker_1min_close < day_open:
            lost_polarity = True
        if pos_pdc and ticker_1min_close < pos_pdc:
            lost_polarity = True
        if lost_polarity:
            tickers_to_close.append((ticker, current_price, "RED_CANDLE"))
            continue

        entry_price = pos["entry_price"]

        # v3.4.35 — Profit-Lock Ladder replaces the 1%/$1 armed-trail.
        # Update peak (trail_high) every tick — ladder reads this.
        if current_price > pos.get("trail_high", entry_price):
            pos["trail_high"] = current_price
        peak = pos["trail_high"]
        peak_gain_pct = (peak - entry_price) / entry_price if entry_price > 0 else 0.0

        # Compute ladder stop; ratchet pos["stop"] upward only.
        ladder_stop = _ladder_stop_long(pos)
        if ladder_stop > pos.get("stop", 0):
            old_stop = pos.get("stop", 0)
            pos["stop"] = ladder_stop
            logger.info(
                "[LADDER] %s LONG stop ratcheted $%.2f \u2192 $%.2f "
                "(peak=$%.2f, +%.2f%%)",
                ticker, old_stop, ladder_stop, peak, peak_gain_pct * 100,
            )

        # Arm cosmetic trail_active / trail_stop once past the 1% gate
        # (Bullet phase ends). Keeps /api/state + exit-reason attribution
        # (TRAIL vs STOP in _finalize_pos) working.
        if peak_gain_pct >= 0.01:
            if not pos.get("trail_active"):
                pos["trail_active"] = True
                logger.info(
                    "Trail armed for %s at $%.2f (+%.2f%% peak) — ladder active",
                    ticker, current_price, peak_gain_pct * 100,
                )
            pos["trail_stop"] = pos["stop"]

        # Exit when current price crosses the ladder stop.
        if current_price <= pos["stop"]:
            reason = "TRAIL" if pos.get("trail_active") else "STOP"
            tickers_to_close.append((ticker, current_price, reason))
            continue

    # Close positions outside the loop to avoid mutation during iteration
    for ticker, price, reason in tickers_to_close:
        close_position(ticker, price, reason)


# ============================================================
# CLOSE TP POSITION (independent TP long close)
# ============================================================


# ============================================================
# MANAGE TP POSITIONS (independent stop + trail logic)
# ============================================================


# ============================================================
# SHORT ENTRY CHECK (Wounded Buffalo)
# ============================================================
def check_short_entry(ticker):
    """Wounded Buffalo: enter short if 1-min close breaks OR_Low with all filters valid.

    v3.5.0: paper-only. RH mirror removed.
    """
    global short_positions, daily_short_entry_count
    global paper_cash

    if _trading_halted:
        return

    if _scan_paused:
        return

    now_et = _now_et()

    # Time gate: must be after 09:35 ET (OR window close + 2-bar confirm)
    if now_et.hour < 9:
        return
    if now_et.hour == 9 and now_et.minute < 35:
        return

    # EOD gate: no new shorts after 15:55 ET
    eod_time = now_et.replace(hour=15, minute=55, second=0, microsecond=0)
    if now_et >= eod_time:
        return

    # Max 5 short entries per ticker per day
    if daily_short_entry_count.get(ticker, 0) >= 5:
        return

    # Re-entry cooldown: 15 min after any exit on this ticker
    last_exit = _last_exit_time.get(ticker)
    if last_exit:
        elapsed = (datetime.now(timezone.utc) - last_exit).total_seconds()
        if elapsed < 900:
            mins_left = int((900 - elapsed) / 60) + 1
            logger.info("SKIP %s [COOLDOWN] %dm left", ticker, mins_left)
            return

    # Per-ticker daily loss cap: skip if down > $50 on this ticker today (both sides)
    ticker_pnl_today = sum(
        (t.get("pnl") or 0) for t in short_trade_history
        if t.get("ticker") == ticker and _is_today(t.get("exit_time_iso") or t.get("entry_time_iso", ""))
    )
    ticker_pnl_today += sum(
        (t.get("pnl") or 0) for t in trade_history
        if t.get("ticker") == ticker and _is_today(t.get("exit_time_iso") or t.get("entry_time_iso", ""))
    )
    if ticker_pnl_today < -50.0:
        logger.info("SKIP %s [LOSS CAP] ticker P&L today: $%.2f", ticker, ticker_pnl_today)
        return

    # Already in a short position for this ticker (paper)
    if ticker in short_positions:
        return

    # OR data must be available (need or_low)
    if ticker not in or_low or ticker not in pdc:
        return

    or_low_val = or_low[ticker]
    pdc_val = pdc[ticker]

    # Fetch current quote (sync — no await)
    bars = fetch_1min_bars(ticker)
    if not bars:
        return
    closes = [c for c in bars["closes"] if c is not None]
    if not closes:
        return
    current_close = closes[-2] if len(closes) >= 2 else closes[-1]
    current_price = current_close  # use close as limit price proxy

    # FMP primary quote — override price and PDC if available
    fmp_q = get_fmp_quote(ticker)
    if fmp_q:
        fmp_price = fmp_q.get("price")
        if fmp_price and fmp_price > 0:
            current_close = fmp_price
            current_price = fmp_price
        fmp_pdc = fmp_q.get("previousClose")
        if fmp_pdc and fmp_pdc > 0:
            pdc_val = fmp_pdc
            pdc[ticker] = fmp_pdc

    # OR sanity check: OR Low must be within OR_STALE_THRESHOLD of live price.
    if not _or_price_sane(or_low_val, current_price):
        pct = abs(or_low_val - current_price) / current_price * 100
        or_stale_skip_count[ticker] = or_stale_skip_count.get(ticker, 0) + 1
        logger.warning(
            "SKIP %s short \u2014 OR Low $%.2f is %.1f%% from live $%.2f (stale?)",
            ticker, or_low_val, pct, current_price
        )
        return

    # v3.4.21 — pre-compute gate values for snapshot + near-miss logging.
    # v3.4.47 — 2-bar OR breakdown confirmation (Tiger 2.0).
    price_break = _tiger_two_bar_short(closes, or_low_val)
    polarity_ok = current_price < pdc_val

    volumes = bars.get("volumes", [])
    vol_pct = None
    vol_ok = False
    vol_ready_flag = True
    entry_bar_vol = 0.0
    avg_vol = 0.0
    if len(volumes) >= 5:
        valid_vols = [v for v in volumes[:-1] if v is not None and v > 0]
        avg_vol = sum(valid_vols) / len(valid_vols) if valid_vols else 0
        entry_bar_vol, vol_ready = _entry_bar_volume(volumes)
        vol_ready_flag = vol_ready
        if vol_ready and avg_vol > 0:
            vol_pct = (entry_bar_vol / avg_vol) * 100.0
            vol_ok = vol_pct >= 150.0

    # v3.5.x: dashboard gate snapshot is now written by
    # _update_gate_snapshot() at the top of each scan cycle. The
    # per-entry writes that used to live here were the latch:
    # SHORT ran after LONG and always clobbered the side field.

    # Volume confirmation: entry bar volume >= 1.5x session average.
    # v3.4.20: walk back through null/zero bars before failing (see
    # _entry_bar_volume docstring). DATA NOT READY is distinct from
    # LOW VOL and still fail-closed.
    # v3.4.47 — gated by TIGER_V2_REQUIRE_VOL (default False).
    if TIGER_V2_REQUIRE_VOL and len(volumes) >= 5:
        if not vol_ready_flag:
            logger.info("SKIP %s [DATA NOT READY] no closed bar with volume in last 5", ticker)
            if price_break:
                _record_near_miss(
                    ticker=ticker, side="SHORT", reason="DATA_NOT_READY",
                    close=round(current_close, 2), level=round(or_low_val, 2),
                    vol_bar=None, vol_avg=None, vol_pct=None,
                )
            return
        if avg_vol > 0 and entry_bar_vol < avg_vol * 1.5:
            logger.info("SKIP %s [LOW VOL] entry bar %.0f vs avg %.0f", ticker, entry_bar_vol, avg_vol)
            if price_break:
                _record_near_miss(
                    ticker=ticker, side="SHORT", reason="LOW_VOL",
                    close=round(current_close, 2), level=round(or_low_val, 2),
                    vol_bar=int(entry_bar_vol), vol_avg=int(avg_vol),
                    vol_pct=round(vol_pct, 1) if vol_pct is not None else None,
                )
            return

    # Entry conditions — ALL must be true:
    # 1. Last 1-min close < OR_Low (breakdown)
    if not price_break:
        return
    # 2. Current price < PDC (polarity — "Red" stock only)
    if not polarity_ok:
        return
    # 3. SPY < SPY_PDC   (v3.4.34: migrated from AVWAP to PDC)
    # 4. QQQ < QQQ_PDC
    # Short gate now mirrors the Sovereign Regime Shield ejector
    # (v3.4.28) — one anchor across entries and ejects. Fail-closed
    # on missing PDC: if either index PDC is unseeded, do NOT enter.
    spy_below = False
    qqq_below = False
    spy_pdc_val = pdc.get("SPY")
    qqq_pdc_val = pdc.get("QQQ")
    if spy_pdc_val and spy_pdc_val > 0:
        spy_bars = fetch_1min_bars("SPY")
        if spy_bars:
            spy_price = spy_bars["current_price"]
            if spy_price < spy_pdc_val:
                spy_below = True
    if qqq_pdc_val and qqq_pdc_val > 0:
        qqq_bars = fetch_1min_bars("QQQ")
        if qqq_bars:
            qqq_price = qqq_bars["current_price"]
            if qqq_price < qqq_pdc_val:
                qqq_below = True

    # v3.4.21 — update short index flag on snapshot before the early return.
    snap = _gate_snapshot.get(ticker)
    if snap is not None and snap.get("side") == "SHORT":
        snap["index"] = bool(spy_below and qqq_below)

    if not spy_below or not qqq_below:
        return

    # v3.4.47 — DI- gate (Tiger 2.0 short mirror).
    # DI-(5m,15) must exceed threshold. Fail-closed on warmup.
    _di_plus_s, di_minus_s = tiger_di(ticker)
    if di_minus_s is None:
        logger.info(
            "SKIP %s [DI WARMUP] need %d+1 5m bars",
            ticker, DI_PERIOD,
        )
        return
    if di_minus_s < TIGER_V2_DI_THRESHOLD:
        logger.info(
            "SKIP %s [DI-] %.1f < %d",
            ticker, di_minus_s, TIGER_V2_DI_THRESHOLD,
        )
        return

    # All checks passed — enter short
    execute_short_entry(ticker, current_price)


# ============================================================
# EXECUTE SHORT ENTRY (Wounded Buffalo)
# ============================================================
def execute_short_entry(ticker, price):
    """Open a paper short position (Wounded Buffalo).

    v3.4.45 — size is dollar-based via paper_shares_for(price) for
    consistency with long entries. Short proceeds still credit
    paper_cash, so no cash gate is needed on the open.
    """
    global short_positions
    global paper_cash
    global daily_short_entry_count

    entry_price = round(price, 2)
    shares = paper_shares_for(entry_price)
    if shares <= 0:
        logger.warning("[paper] skip short %s — invalid price $%.2f",
                       ticker, entry_price)
        return
    pdc_val = pdc.get(ticker, entry_price)
    # v3.4.21 — cap stop at 0.75% above entry when PDC baseline would
    # imply a looser stop (late/extended breakdown bar).
    stop, _stop_capped, _stop_baseline = _capped_short_stop(pdc_val, entry_price)
    if _stop_capped:
        logger.info(
            "%s short stop capped: baseline=$%.2f -> capped=$%.2f (entry=$%.2f, %.2f%% cap)",
            ticker, _stop_baseline, stop, entry_price, MAX_STOP_PCT * 100,
        )
    now_et = _now_et()
    entry_time_cdt = _now_cdt().strftime("%H:%M:%S")
    entry_time_display = _now_cdt().strftime("%H:%M CDT")
    date_str = now_et.strftime("%Y-%m-%d")

    # Paper short
    short_positions[ticker] = {
        "entry_price": entry_price,
        "shares": shares,
        "stop": stop,
        "initial_stop": stop,  # v3.4.35 — frozen
        "trail_stop": None,
        "trail_active": False,
        "trail_low": entry_price,
        "entry_time": entry_time_cdt,
        "date": date_str,
        "side": "SHORT",
    }
    paper_cash += entry_price * shares  # short sale proceeds credited
    daily_short_entry_count[ticker] = daily_short_entry_count.get(ticker, 0) + 1
    save_paper_state()

    # Paper notification always fires (paper side already mutated)
    pdc_val = pdc.get(ticker, 0)
    or_low_val = or_low.get(ticker, 0)
    SEP = "\u2500" * 34
    entry_count = daily_short_entry_count.get(ticker, 1)
    short_proceeds = entry_price * shares
    short_sig = "Signal   : Wounded Buffalo \u2193\n"
    short_sig += "  1m close < OR Low \u2713\n"
    short_sig += "  Price < PDC \u2713\n"
    short_sig += "  SPY < PDC \u2713\n"
    short_sig += "  QQQ < PDC \u2713\n"
    # v3.4.21 — label stop source: baseline PDC+$0.90 or entry-relative cap.
    short_stop_label = (
        "entry +0.75%" if _stop_capped else "PDC+$0.90"
    )
    msg = (
        "\U0001fa78 SHORT ENTRY #%d\n"
        "%s\n"
        "Ticker   : %s\n"
        "Entry    : $%.2f (limit)\n"
        "Shares   : %d   Proceeds: $%s\n"
        "Stop     : $%.2f (%s)\n"
        "OR Low   : $%.2f\n"
        "PDC      : $%.2f\n"
        "%s"
        "Time     : %s\n"
        "%s"
    ) % (entry_count, SEP, ticker, entry_price,
         shares, format(short_proceeds, ",.2f"),
         stop, short_stop_label, or_low_val, pdc_val, short_sig, entry_time_display, SEP)
    send_telegram(msg)

    # v4.0.0-alpha — notify executor bots of the paper short entry.
    _emit_signal({
        "kind": "ENTRY_SHORT",
        "ticker": ticker,
        "price": float(entry_price),
        "reason": "WOUNDED_BUFFALO",
        "timestamp_utc": _utc_now_iso(),
        "main_shares": int(shares),
    })


# ============================================================
# MANAGE SHORT POSITIONS (stop + trail logic)
# ============================================================
def manage_short_positions():
    """Check stops and trailing stops for all open short positions."""
    global short_positions

    # v3.4.23 — enforce 0.75% entry cap retroactively on every open
    # short (see manage_positions for rationale). Note: manage_positions
    # and manage_short_positions are called back-to-back by the scan
    # loop, so calling retighten_all_stops from both is redundant-but-
    # cheap. Kept in both for defensive symmetry: if a future refactor
    # reorders or skips one manager, the cap still holds for the other
    # book.
    retighten_all_stops(force_exit=True, fetch_prices=True)

    # ── Sovereign Regime Shield (v3.4.28) ────────────────────────────────────
    # Exit all shorts ONLY when BOTH SPY and QQQ have a finalized 1-min close
    # ABOVE their respective Prior Day Close (PDC). Mirror of the long-side
    # Sovereign Regime Shield — a PDC cross is structural, not drift. AND-
    # logic suppresses ejects on SPY/QQQ divergence. See v3.4.28 CHANGELOG.
    bull_vacuum = _sovereign_regime_eject("short")

    for ticker in list(short_positions.keys()):
        pos = short_positions[ticker]
        entry_price = pos["entry_price"]
        shares = pos["shares"]

        bars = fetch_1min_bars(ticker)
        if not bars:
            continue
        current_price = bars["current_price"]

        # v3.4.35 — Profit-Lock Ladder replaces the 1%/$1 armed-trail.
        # Track trail_low every tick (peak = deepest price reached).
        trail_low = pos.get("trail_low", entry_price)
        if current_price < trail_low:
            trail_low = current_price
            pos["trail_low"] = trail_low
        peak_gain_pct = (entry_price - trail_low) / entry_price if entry_price > 0 else 0.0

        # Compute ladder stop; ratchet pos["stop"] downward only (tighter).
        ladder_stop = _ladder_stop_short(pos)
        if ladder_stop < pos.get("stop", float("inf")):
            old_stop = pos.get("stop", 0)
            pos["stop"] = ladder_stop
            logger.info(
                "[LADDER] %s SHORT stop ratcheted $%.2f \u2192 $%.2f "
                "(trail_low=$%.2f, +%.2f%%)",
                ticker, old_stop, ladder_stop, trail_low, peak_gain_pct * 100,
            )

        # Arm cosmetic trail_active / trail_stop past the 1% gate.
        if peak_gain_pct >= 0.01:
            if not pos.get("trail_active"):
                pos["trail_active"] = True
                logger.info(
                    "Trail armed for %s SHORT at $%.2f (+%.2f%% peak) — ladder active",
                    ticker, current_price, peak_gain_pct * 100,
                )
            pos["trail_stop"] = pos["stop"]

        stop = pos["stop"]
        trail_active = pos.get("trail_active", False)

        # Exit on stop hit. TRAIL vs STOP per ladder-armed state.
        exit_reason = None
        if current_price >= stop:
            exit_reason = "TRAIL" if trail_active else "STOP"


        # ── Sovereign Regime Shield: BOTH SPY+QQQ 1m_close > PDC ─────────────
        if not exit_reason and bull_vacuum:
            exit_reason = "BULL_VACUUM"

        # ── Eye of the Tiger: "The Polarity Shift" — Price > PDC ─────────────
        # Uses completed 1m bar close (per-ticker; not part of the index shield)
        if not exit_reason:
            ticker_pdc = pdc.get(ticker, 0)
            if ticker_pdc > 0:
                ps_closes = [c for c in bars.get("closes", []) if c is not None]
                ps_1min_close = ps_closes[-2] if len(ps_closes) >= 2 else (ps_closes[-1] if ps_closes else current_price)
                if ps_1min_close > ticker_pdc:
                    exit_reason = "POLARITY_SHIFT"

        if exit_reason:
            close_short_position(ticker, current_price, exit_reason)


# ============================================================
# CLOSE SHORT POSITION
# ============================================================
def close_short_position(ticker, price, reason):
    """Cover a short position and record the trade."""
    global short_positions
    global paper_cash
    global short_trade_history

    pos = short_positions.pop(ticker, None)

    if not pos:
        return

    _last_exit_time[ticker] = datetime.now(timezone.utc)

    entry_price = pos["entry_price"]
    shares = pos["shares"]
    cover_price = round(price, 2)

    pnl = round((entry_price - cover_price) * shares, 2)
    pnl_pct = round((entry_price - cover_price) / entry_price * 100, 2) if entry_price else 0
    now_et = _now_et()
    exit_time = _now_cdt().strftime("%H:%M CDT")
    date_str = pos.get("date", now_et.strftime("%Y-%m-%d"))

    trade_record = {
        "ticker": ticker,
        "side": "short",
        "action": "COVER",
        "shares": shares,
        "entry_price": entry_price,
        "exit_price": cover_price,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "reason": reason,
        "entry_time": pos.get("entry_time", "?"),
        "exit_time": exit_time,
        "entry_time_iso": pos.get("entry_time", ""),
        "exit_time_iso": _utc_now_iso(),
        "entry_num": pos.get("entry_count", 1),
        "date": date_str,
    }

    # v3.4.27 — persistent trade log (shorts, both paper and TP).
    # Written BEFORE portfolio branch so either path gets a row.
    _sh_entry_iso = pos.get("entry_time", "") or ""
    _sh_hold_s = None
    try:
        if _sh_entry_iso:
            _ent_dt = datetime.fromisoformat(_sh_entry_iso)
            if _ent_dt.tzinfo is None:
                _ent_dt = _ent_dt.replace(tzinfo=timezone.utc)
            _sh_hold_s = (datetime.now(timezone.utc) - _ent_dt).total_seconds()
    except (TypeError, ValueError):
        _sh_hold_s = None
    _sh_log_row = {
        "date": date_str,
        "portfolio": "paper",
        "ticker": ticker,
        "side": "SHORT",
        "shares": int(shares),
        "entry_price": float(entry_price),
        "exit_price": float(cover_price),
        "entry_time": _sh_entry_iso,
        "exit_time": _utc_now_iso(),
        "hold_seconds": _sh_hold_s,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "reason": reason,
        "entry_num": int(pos.get("entry_count", 1)),
    }
    _sh_log_row.update(_trade_log_snapshot_pos(pos))
    trade_log_append(_sh_log_row)

    paper_cash -= cover_price * shares
    short_trade_history.append(trade_record)
    if len(short_trade_history) > 500:
        short_trade_history.pop(0)
    save_paper_state()

    # Notification (paper cover)
    pnl_sign = "+" if pnl >= 0 else ""
    emoji = "\u2705" if pnl >= 0 else "\u274c"
    SEP = "\u2500" * 34
    sc_entry_total = round(entry_price * shares, 2)
    sc_cover_total = round(cover_price * shares, 2)
    sc_in_time = _to_cdt_hhmm(pos.get("entry_time", ""))
    sc_reason_label = REASON_LABELS.get(reason, reason)
    if reason == "TRAIL":
        sc_t_low = pos.get("trail_low", cover_price)
        sc_t_dist = max(round(sc_t_low * 0.010, 2), 1.00)
        sc_reason_label = "\U0001f3af Trail Stop (1.0%% / $%.2f)" % sc_t_dist
    msg = (
        "%s SHORT CLOSED\n"
        "%s\n"
        "Ticker : %s\n"
        "Shares : %d\n"
        "Entry  : $%.2f  (total $%s)\n"
        "Cover  : $%.2f  (total $%s)\n"
        "P&L    : %s$%.2f  (%s%.1f%%)\n"
        "Reason : %s\n"
        "In: %s   Out: %s\n"
        "%s"
    ) % (emoji, SEP, ticker, shares,
         entry_price, format(sc_entry_total, ",.2f"),
         cover_price, format(sc_cover_total, ",.2f"),
         pnl_sign, pnl, pnl_sign, pnl_pct,
         sc_reason_label, sc_in_time, exit_time, SEP)
    send_telegram(msg)

    # v4.0.0-alpha — notify executor bots of the paper short cover.
    _emit_signal({
        "kind": "EXIT_SHORT",
        "ticker": ticker,
        "price": float(cover_price),
        "reason": reason,
        "timestamp_utc": _utc_now_iso(),
        "main_shares": int(shares),
    })



# ============================================================
# EOD CLOSE
# ============================================================
def eod_close():
    """Force-close all open long AND short positions at 15:55 ET."""
    # v4.0.0-alpha — notify executors to flatten everything on Alpaca.
    # Per-position close events still fire from close_position /
    # close_short_position below; this event lets executors shortcut with
    # a single close_all_positions call if they prefer.
    _emit_signal({
        "kind": "EOD_CLOSE_ALL",
        "ticker": "",
        "price": 0.0,
        "reason": "EOD",
        "timestamp_utc": _utc_now_iso(),
        "main_shares": 0,
    })
    n_long = len(positions)
    n_short = len(short_positions)

    if not positions and not short_positions:
        logger.info("EOD close: no open positions (long or short)")

    if positions:
        logger.info("EOD close: closing %d long positions", n_long)
        longs_to_close = []
        for ticker in list(positions.keys()):
            bars = fetch_1min_bars(ticker)
            if bars:
                price = bars["current_price"]
            else:
                price = positions[ticker]["entry_price"]
            longs_to_close.append((ticker, price))
        for ticker, price in longs_to_close:
            close_position(ticker, price, reason="EOD")

    if short_positions:
        logger.info("EOD close: closing %d short positions", n_short)
        shorts_to_close = []
        for ticker in list(short_positions.keys()):
            bars = fetch_1min_bars(ticker)
            if bars:
                price = bars["current_price"]
            else:
                price = short_positions[ticker]["entry_price"]
            shorts_to_close.append((ticker, price))
        for ticker, price in shorts_to_close:
            close_short_position(ticker, price, "EOD")

    _, _, total_pnl, wins, losses, n_trades = _today_pnl_breakdown()
    msg = (
        f"EOD CLOSE Complete\n"
        f"  Trades: {n_trades}  W/L: {wins}/{losses}\n"
        f"  Day P&L: ${total_pnl:+.2f}\n"
        f"  Cash: ${paper_cash:,.2f}"
    )
    send_telegram(msg)
    save_paper_state()


# ============================================================
# MORNING OR NOTIFICATION (Feature 3)
# ============================================================
def send_or_notification():
    """Send morning OR card at 09:36 ET. Retry if OR data not ready."""
    def _do_send():
        now_et = _now_et()
        today = now_et.strftime("%Y-%m-%d")

        for attempt in range(3):
            if or_collected_date == today and len(or_high) > 0:
                break
            if attempt < 2:
                logger.info("OR notification: data not ready, retry %d/3 in 30s", attempt + 1)
                time.sleep(30)

        if or_collected_date != today:
            logger.warning("OR notification: OR data not ready after retries, skipping")
            return

        SEP = "\u2500" * 34
        lines = [
            "\U0001f4d0 OR LEVELS \u2014 8:36 CT",
            SEP,
        ]

        for t in TRADE_TICKERS:
            orh = or_high.get(t)
            orl = or_low.get(t)
            pdc_val = pdc.get(t)
            if orh is None or pdc_val is None:
                lines.append("%s   --" % t)
                continue
            # Fetch current price for status
            bars = fetch_1min_bars(t)
            cur_price = bars["current_price"] if bars else 0
            if cur_price > pdc_val:
                status_icon = "\U0001f7e2"
            elif cur_price < pdc_val:
                status_icon = "\U0001f534"
            else:
                status_icon = "\u2b1c"
            orl_str = "%.2f" % orl if orl is not None else "--"
            lines.append(
                "%s  H:$%.2f  L:$%s  PDC:$%.2f  %s"
                % (t, orh, orl_str, pdc_val, status_icon)
            )

        lines.append(SEP)

        # SPY/QQQ vs PDC (v3.4.34: swapped from AVWAP)
        spy_bars = fetch_1min_bars("SPY")
        qqq_bars = fetch_1min_bars("QQQ")
        spy_price = spy_bars["current_price"] if spy_bars else 0
        qqq_price = qqq_bars["current_price"] if qqq_bars else 0
        spy_pdc_d = pdc.get("SPY") or 0
        qqq_pdc_d = pdc.get("QQQ") or 0

        spy_above = spy_price > spy_pdc_d if spy_pdc_d > 0 else False
        qqq_above = qqq_price > qqq_pdc_d if qqq_pdc_d > 0 else False
        spy_icon = "\u2705 above" if spy_above else "\u274c below"
        qqq_icon = "\u2705 above" if qqq_above else "\u274c below"

        spy_pdc_fmt = "%.2f" % spy_pdc_d if spy_pdc_d > 0 else "n/a"
        qqq_pdc_fmt = "%.2f" % qqq_pdc_d if qqq_pdc_d > 0 else "n/a"

        lines.append("SPY PDC: $%s  %s" % (spy_pdc_fmt, spy_icon))
        lines.append("QQQ PDC: $%s  %s" % (qqq_pdc_fmt, qqq_icon))

        both_active = spy_above and qqq_above
        both_bearish = (not spy_above) and (not qqq_above)
        filter_status = "LONG ACTIVE" if both_active else ("SHORT ACTIVE" if both_bearish else "PARTIAL/INACTIVE")
        lines.append("Index filters: %s" % filter_status)
        lines.append(SEP)
        lines.append("Watching for breakouts (long) and breakdowns (short).")

        msg = "\n".join(lines)
        send_telegram(msg)

    threading.Thread(target=_do_send, daemon=True).start()


# ============================================================
# AUTO EOD REPORT (Feature 4)
# ============================================================
def _build_eod_report(today: str) -> str:
    """Build EOD report text for the paper portfolio.

    v3.4.6: includes shorts. Previously only counted long SELLs (action='SELL'
    in paper_trades), so paper short COVERs (logged to short_trade_history
    with action='COVER') were silently dropped. All-time totals also excluded
    short P&L. This rebuilds the report from trade_history + short_trade_history
    so longs and shorts are both counted, with a per-trade label.
    """
    SEP = "\u2500" * 34
    long_hist = trade_history
    short_hist = short_trade_history
    title = "PAPER PORTFOLIO"

    # Today's closed trades (longs + shorts), filtered by date
    today_longs = [t for t in long_hist if t.get("date", "") == today]
    today_shorts = [t for t in short_hist if t.get("date", "") == today]
    today_all = today_longs + today_shorts

    n_trades = len(today_all)
    n_long = len(today_longs)
    n_short = len(today_shorts)
    wins = sum(1 for t in today_all if (t.get("pnl") or 0) >= 0)
    losses = n_trades - wins
    win_rate = (wins / n_trades * 100) if n_trades > 0 else 0
    day_pnl = sum((t.get("pnl") or 0) for t in today_all)

    # All-time across longs + shorts
    all_long_pnl = sum((t.get("pnl") or 0) for t in long_hist)
    all_short_pnl = sum((t.get("pnl") or 0) for t in short_hist)
    all_time_pnl = all_long_pnl + all_short_pnl
    all_wins = (
        sum(1 for t in long_hist if (t.get("pnl") or 0) >= 0)
        + sum(1 for t in short_hist if (t.get("pnl") or 0) >= 0)
    )
    all_n = len(long_hist) + len(short_hist)
    all_losses = all_n - all_wins
    all_wr = (all_wins / all_n * 100) if all_n else 0

    lines = [
        "\U0001f4ca EOD Report \u2014 %s" % today,
        SEP,
        title,
        "  Trades today:  %d  (L:%d S:%d)" % (n_trades, n_long, n_short),
        "  Wins / Losses: %d / %d" % (wins, losses),
        "  Win Rate:      %.1f%%" % win_rate,
        "  Day P&L:      $%+.2f" % day_pnl,
        SEP,
    ]
    # Sort by exit time so the per-trade list reads chronologically
    today_all_sorted = sorted(
        today_all,
        key=lambda t: t.get("exit_time_iso") or t.get("exit_time") or "",
    )
    for t in today_all_sorted:
        tk = t.get("ticker", "?")
        sh = t.get("shares", 0)
        t_pnl = t.get("pnl") or 0
        t_pct = t.get("pnl_pct") or 0
        t_reason = t.get("reason", "?")
        side = (t.get("side") or "long").upper()
        side_tag = "S" if side == "SHORT" else "L"
        lines.append("  [%s] %s  %dsh  $%+.2f (%+.1f%%)  %s"
                     % (side_tag, tk, sh, t_pnl, t_pct, t_reason))
    lines.append(SEP)
    lines.append("  All-time P&L:  $%+.2f" % all_time_pnl)
    lines.append("  All-time W/L:  %d / %d  (%.1f%%)"
                 % (all_wins, all_losses, all_wr))

    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:3990] + "\n... (truncated)"
    return msg


def send_eod_report():
    """Auto EOD report at 15:58 ET. Paper only.

    v3.4.6: includes paper shorts (previously dropped because the report
    filtered paper_trades for action='SELL', which excludes COVER records).
    """
    now_et = _now_et()
    today = now_et.strftime("%Y-%m-%d")
    send_telegram(_build_eod_report(today))


# ============================================================
# WEEKLY DIGEST (Feature 9)
# ============================================================
def send_weekly_digest():
    """Weekly digest — Sunday 18:00 ET. Paper only."""
    SEP = "\u2500" * 34
    now_et = _now_et()
    cutoff = now_et - timedelta(days=7)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    week_label = _now_cdt().strftime("Week of %b %d")

    def _build_digest(history, label):
        week_trades = [
            t for t in history
            if t.get("date", "") >= cutoff_str
        ]
        if not week_trades:
            return "\U0001f4c5 Weekly Digest \u2014 %s\n%s\n%s\nNo trades this week." % (
                week_label, SEP, label
            )

        n = len(week_trades)
        wins = sum(1 for t in week_trades if t.get("pnl", 0) >= 0)
        losses = n - wins
        wr = (wins / n * 100) if n > 0 else 0
        week_pnl = sum(t.get("pnl", 0) for t in week_trades)

        # Best day
        day_pnls = {}
        for t in week_trades:
            d = t.get("date", "")
            day_pnls[d] = day_pnls.get(d, 0) + t.get("pnl", 0)
        best_day_date = max(day_pnls, key=day_pnls.get)
        # Convert date to day name
        try:
            best_day_dt = datetime.strptime(best_day_date, "%Y-%m-%d")
            best_day_name = best_day_dt.strftime("%a")
        except Exception:
            best_day_name = best_day_date
        best_day_pnl = day_pnls[best_day_date]

        # Best trade
        best_trade = max(week_trades, key=lambda t: t.get("pnl", 0))
        best_ticker = best_trade.get("ticker", "?")
        best_pnl = best_trade.get("pnl", 0)

        # Top performers by ticker
        ticker_pnls = {}
        ticker_counts = {}
        for t in week_trades:
            tk = t.get("ticker", "?")
            ticker_pnls[tk] = ticker_pnls.get(tk, 0) + t.get("pnl", 0)
            ticker_counts[tk] = ticker_counts.get(tk, 0) + 1
        sorted_tickers = sorted(ticker_pnls.keys(), key=lambda k: ticker_pnls[k], reverse=True)
        top3 = sorted_tickers[:3]

        lines = [
            "\U0001f4c5 Weekly Digest \u2014 %s" % week_label,
            SEP,
            label,
            "  Trades:    %d  (W:%d  L:%d)" % (n, wins, losses),
            "  Win Rate:  %.1f%%" % wr,
            "  Week P&L: $%+.2f" % week_pnl,
            "  Best day:  %s $%+.2f" % (best_day_name, best_day_pnl),
            "  Best trade: %s $%+.2f" % (best_ticker, best_pnl),
            SEP,
            "Top performers this week:",
        ]
        for tk in top3:
            lines.append("  %s  %d trades  $%+.2f" % (tk, ticker_counts[tk], ticker_pnls[tk]))
        lines.append(SEP)
        lines.append("Next week: OR strategy continues.")
        lines.append("All 8 tickers monitored from 8:45 CT.")

        msg = "\n".join(lines)
        if len(msg) > 4000:
            msg = msg[:3990] + "\n... (truncated)"
        return msg

    # Merge long + short history so weekly digest covers all closed trades.
    # Long closes live in trade_history; short COVERs live in short_trade_history.
    paper_combined = list(trade_history) + list(short_trade_history)
    paper_digest = _build_digest(paper_combined, "PAPER PORTFOLIO")
    send_telegram(paper_digest)


# ============================================================
# SYSTEM HEALTH TEST
# ============================================================
def _run_system_test_sync(label: str) -> None:
    """Run system health checks and send report (blocking I/O — run in executor)."""
    SEP = "\u2500" * 30
    issues = 0
    lines = []

    # A. FMP API check
    try:
        spy_q = get_fmp_quote("SPY")
        qqq_q = get_fmp_quote("QQQ")
        spy_price = float(spy_q.get("price", 0)) if spy_q else 0
        qqq_price = float(qqq_q.get("price", 0)) if qqq_q else 0
        if spy_price > 0 and qqq_price > 0:
            lines.append(
                "FMP: \u2705 SPY $%.2f | QQQ $%.2f" % (spy_price, qqq_price)
            )
        else:
            issues += 1
            lines.append("FMP: \u274c no price data")
    except Exception as exc:
        issues += 1
        lines.append("FMP: \u274c %s" % exc)

    # B. Finnhub fallback check
    try:
        fhb_url = (
            "https://finnhub.io/api/v1/quote?symbol=SPY&token=%s"
            % FINNHUB_TOKEN
        )
        req = urllib.request.Request(fhb_url, headers=YAHOO_HEADERS)
        with urllib.request.urlopen(req, timeout=5) as resp:
            fhb_data = json.loads(resp.read())
        fhb_price = float(fhb_data.get("c", 0))
        if fhb_price > 0:
            lines.append("FHB: \u2705 SPY $%.2f" % fhb_price)
        else:
            issues += 1
            lines.append("FHB: \u274c no price data")
    except Exception as exc:
        issues += 1
        lines.append("FHB: \u274c %s" % exc)

    # C. State health check
    try:
        with open(PAPER_STATE_FILE, "r", encoding="utf-8") as f:
            ps = json.load(f)
        p_cash = ps.get("paper_cash", 0)
        lines.append(
            "State: \u2705 paper $%s"
            % format(int(p_cash), ",")
        )
    except Exception as exc:
        issues += 1
        lines.append("State: \u274c %s" % exc)

    # D. Positions count
    n_paper = len(positions) + len(short_positions)
    lines.append("Pos: %d paper" % n_paper)

    # E. Scanner health
    if _last_scan_time is None:
        lines.append("Scanner: \u23f8 Not started")
    else:
        age = (datetime.now(timezone.utc) - _last_scan_time).total_seconds()
        if age < 90:
            lines.append("Scanner: \u2705 Active (%ds ago)" % int(age))
        else:
            mins = int(age) // 60
            secs = int(age) % 60
            issues += 1
            lines.append(
                "Scanner: \u274c STALLED (%dm %ds ago)" % (mins, secs)
            )

    # F. OR status — only for 8:31 CT label
    if label == "8:31 CT":
        n_or = sum(1 for t in TRADE_TICKERS if t in or_high)
        lines.append("ORs set: %d / %d tickers" % (n_or, len(TRADE_TICKERS)))

    # Build message
    if issues == 0:
        footer = "\u2705 All systems GO"
    else:
        footer = "\u26a0\ufe0f %d issue(s) found \u2014 check logs" % issues

    body = "\n".join(lines)
    msg = (
        "\U0001f9ea System Test [%s]\n"
        "%s\n"
        "%s\n"
        "%s\n"
        "%s"
    ) % (label, SEP, body, SEP, footer)

    send_telegram(msg)


def _fire_system_test(label: str) -> None:
    """Sync wrapper to fire _run_system_test_sync from scheduler thread."""
    try:
        _run_system_test_sync(label)
    except Exception as exc:
        logger.error("System test (%s) failed: %s", label, exc, exc_info=True)


def _test_fmp():
    """Test FMP API — returns status string."""
    try:
        spy_q = get_fmp_quote("SPY")
        qqq_q = get_fmp_quote("QQQ")
        spy_price = float(spy_q.get("price", 0)) if spy_q else 0
        qqq_price = float(qqq_q.get("price", 0)) if qqq_q else 0
        if spy_price > 0 and qqq_price > 0:
            return "\u2705 SPY $%.2f | QQQ $%.2f" % (spy_price, qqq_price)
        return "\u274c no price data"
    except Exception as exc:
        return "\u274c %s" % exc


def _test_finnhub():
    """Test Finnhub API — returns status string."""
    try:
        fhb_url = (
            "https://finnhub.io/api/v1/quote?symbol=SPY&token=%s"
            % FINNHUB_TOKEN
        )
        req = urllib.request.Request(fhb_url, headers=YAHOO_HEADERS)
        with urllib.request.urlopen(req, timeout=5) as resp:
            fhb_data = json.loads(resp.read())
        fhb_price = float(fhb_data.get("c", 0))
        if fhb_price > 0:
            return "\u2705 SPY $%.2f" % fhb_price
        return "\u274c no price data"
    except Exception as exc:
        return "\u274c %s" % exc


def _test_state():
    """Test state files — returns status string."""
    try:
        with open(PAPER_STATE_FILE, "r", encoding="utf-8") as f:
            ps = json.load(f)
        p_cash = ps.get("paper_cash", 0)
        return "\u2705 paper $%s" % format(int(p_cash), ",")
    except Exception as exc:
        return "\u274c %s" % exc


def _test_positions():
    """Test positions — returns status string."""
    n_paper = len(positions) + len(short_positions)
    return "%d paper" % n_paper


def _test_scanner():
    """Test scanner health — returns status string."""
    if _last_scan_time is None:
        return "\u23f8 Not started"
    age = (datetime.now(timezone.utc) - _last_scan_time).total_seconds()
    if age < 90:
        return "\u2705 Active (%ds ago)" % int(age)
    mins = int(age) // 60
    secs = int(age) % 60
    return "\u274c STALLED (%dm %ds ago)" % (mins, secs)


def _build_test_progress(results):
    """Format the interactive test progress message."""
    SEP = "\u2500" * 30
    steps = [
        ("FMP API", "fmp"),
        ("Finnhub", "fhb"),
        ("State files", "state"),
        ("Positions", "pos"),
        ("Scanner", "scanner"),
    ]
    body_lines = []
    for label, key in steps:
        status = results.get(key, "\u23f3")
        body_lines.append("  %-12s %s" % (label + ":", status))
    body = "\n".join(body_lines)

    issues = 0
    for key in ("fmp", "fhb", "state", "scanner"):
        val = results.get(key, "")
        if val.startswith("\u274c"):
            issues += 1

    if all(k in results for _, k in steps):
        if issues == 0:
            footer = "\u2705 All systems GO"
        else:
            footer = "\u26a0\ufe0f %d issue(s) found \u2014 check logs" % issues
        return "\U0001f9ea System Test [Manual]\n%s\n%s\n%s\n%s" % (SEP, body, SEP, footer)

    return "\U0001f9ea System Test [Manual]\n%s\n%s" % (SEP, body)


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /test command — run system health test with live progress."""
    t0 = asyncio.get_event_loop().time()
    await update.message.reply_chat_action(ChatAction.TYPING)
    results = {}
    prog = await update.message.reply_text(_build_test_progress(results))

    loop = asyncio.get_event_loop()

    # Step 1 — FMP
    results["fmp"] = await loop.run_in_executor(None, _test_fmp)
    try:
        await prog.edit_text(_build_test_progress(results))
    except Exception:
        pass

    # Step 2 — Finnhub
    results["fhb"] = await loop.run_in_executor(None, _test_finnhub)
    try:
        await prog.edit_text(_build_test_progress(results))
    except Exception:
        pass

    # Step 3 — State files
    results["state"] = await loop.run_in_executor(None, _test_state)
    try:
        await prog.edit_text(_build_test_progress(results))
    except Exception:
        pass

    # Step 4 — Positions
    results["pos"] = _test_positions()
    try:
        await prog.edit_text(_build_test_progress(results))
    except Exception:
        pass

    # Step 5 — Scanner
    results["scanner"] = _test_scanner()
    try:
        await prog.edit_text(_build_test_progress(results))
    except Exception:
        pass

    # Final edit with menu button
    try:
        await prog.edit_text(_build_test_progress(results), reply_markup=_menu_button())
    except Exception:
        pass
    logger.info("CMD test completed in %.2fs", asyncio.get_event_loop().time() - t0)






# ============================================================
# v3.4.47 — HARD EJECT (Eye of the Tiger 2.0)
# ============================================================
def _tiger_hard_eject_check():
    """Hard Eject: close any open position whose DI or index
    regime has flipped against it.

    Called once per scan cycle BEFORE the new-entry scan.
    Longs: eject if DI+ < threshold OR both indices < PDC.
    Shorts: eject if DI- < threshold OR both indices > PDC.
    Applies to paper (positions, short_positions).
    """
    # Index regime flags (reuse cached bars from this cycle)
    spy_bars = fetch_1min_bars("SPY")
    qqq_bars = fetch_1min_bars("QQQ")
    spy_pdc_v = pdc.get("SPY")
    qqq_pdc_v = pdc.get("QQQ")

    index_flip_down = False  # both indices below PDC -> eject longs
    index_flip_up   = False  # both indices above PDC -> eject shorts
    if (spy_bars and qqq_bars
            and spy_pdc_v and qqq_pdc_v
            and spy_pdc_v > 0 and qqq_pdc_v > 0):
        spy_cur = spy_bars["current_price"]
        qqq_cur = qqq_bars["current_price"]
        index_flip_down = (spy_cur < spy_pdc_v
                           and qqq_cur < qqq_pdc_v)
        index_flip_up   = (spy_cur > spy_pdc_v
                           and qqq_cur > qqq_pdc_v)

    # -- Long positions (paper) --
    for ticker in list(positions):
        di_plus, _di_m = tiger_di(ticker)
        di_weak = (di_plus is not None
                   and di_plus < TIGER_V2_DI_THRESHOLD)
        if di_weak or index_flip_down:
            price = positions[ticker].get("entry_price", 0)
            bars_t = fetch_1min_bars(ticker)
            if bars_t:
                price = bars_t["current_price"] or price
            logger.info(
                "HARD_EJECT_TIGER long %s di+=%s idx_flip=%s",
                ticker, di_plus, index_flip_down,
            )
            close_position(ticker, price,
                           reason="HARD_EJECT_TIGER")


    # -- Short positions (paper) --
    for ticker in list(short_positions):
        _di_p, di_minus = tiger_di(ticker)
        di_weak = (di_minus is not None
                   and di_minus < TIGER_V2_DI_THRESHOLD)
        if di_weak or index_flip_up:
            price = short_positions[ticker].get("entry_price", 0)
            bars_t = fetch_1min_bars(ticker)
            if bars_t:
                price = bars_t["current_price"] or price
            logger.info(
                "HARD_EJECT_TIGER short %s di-=%s idx_flip=%s",
                ticker, di_minus, index_flip_up,
            )
            close_short_position(ticker, price, reason="HARD_EJECT_TIGER")



# ============================================================
# SCAN LOOP
# ============================================================
def scan_loop():
    """Main scan: manage positions, check new entries. Runs every 60s."""
    now_et = _now_et()

    # Skip weekends
    if now_et.weekday() >= 5:
        return

    # Skip outside market hours (09:35 - 15:55 ET)
    if now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 35):
        return
    if now_et.hour >= 16:
        return
    if now_et.hour == 15 and now_et.minute >= 55:
        return

    cycle_start = time.time()
    global _last_scan_time
    _last_scan_time = datetime.now(timezone.utc)

    # Clear the per-cycle 1-min bar cache BEFORE anything else. Any call
    # to fetch_1min_bars inside this cycle will populate it on first hit
    # and reuse on subsequent hits. Observers read through the same cache.
    _clear_cycle_bar_cache()

    # MarketMode + observers: observation only — no parameter is read from
    # this yet. Safe to fail silently; it cannot affect trading.
    try:
        _refresh_market_mode()
    except Exception:
        logger.exception("_refresh_market_mode failed (ignored — observation only)")

    n_pos = len(positions)
    n_short = len(short_positions)
    logger.info("Scanning %d stocks | pos=%d short=%d | mode=%s",
                len(TRADE_TICKERS), n_pos, n_short, _current_mode)

    # ── Regime change alert ───────────────────────────────────────────────
    # v3.4.34: anchor swapped from AVWAP → PDC to match the
    # v3.4.28 Sovereign Regime Shield ejector. One anchor across
    # the whole system; no more divergent alerts vs. real ejects.
    # Fail-closed on missing PDC: no alert fires if either index
    # PDC is unseeded (same semantics as _sovereign_regime_eject).
    global _regime_bullish
    spy_pdc_r = pdc.get("SPY")
    qqq_pdc_r = pdc.get("QQQ")
    if spy_pdc_r and qqq_pdc_r and spy_pdc_r > 0 and qqq_pdc_r > 0:
        spy_bars_r = fetch_1min_bars("SPY")
        qqq_bars_r = fetch_1min_bars("QQQ")
        if spy_bars_r and qqq_bars_r:
            spy_cur_r = spy_bars_r["current_price"]
            qqq_cur_r = qqq_bars_r["current_price"]
            now_bullish = (spy_cur_r > spy_pdc_r) and (qqq_cur_r > qqq_pdc_r)
            if _regime_bullish is None:
                _regime_bullish = now_bullish
            elif now_bullish != _regime_bullish:
                _regime_bullish = now_bullish
                now_hhmm_r = _now_cdt().strftime("%H:%M CDT")
                if now_bullish:
                    regime_msg = (
                        "\U0001f7e2 REGIME: BULLISH\n"
                        "SPY $%.2f > PDC $%.2f\n"
                        "QQQ $%.2f > PDC $%.2f\n"
                        "The Lords are back.  %s"
                    ) % (spy_cur_r, spy_pdc_r, qqq_cur_r, qqq_pdc_r, now_hhmm_r)
                else:
                    regime_msg = (
                        "\U0001f534 REGIME: BEARISH\n"
                        "SPY $%.2f < PDC $%.2f\n"
                        "QQQ $%.2f < PDC $%.2f\n"
                        "The Lords have left.  %s"
                    ) % (spy_cur_r, spy_pdc_r, qqq_cur_r, qqq_pdc_r, now_hhmm_r)
                send_telegram(regime_msg)

    # Always manage existing positions (stops/trails) even when paused
    try:
        manage_positions()
    except Exception as e:
        logger.error("manage_positions crashed: %s", e, exc_info=True)
        err_msg = "⚠️ Bot error in manage_positions: %s" % str(e)[:200]
        send_telegram(err_msg)
    try:
        manage_short_positions()
    except Exception as e:
        logger.error("manage_short_positions crashed: %s", e, exc_info=True)
        err_msg = "⚠️ Bot error in manage_short_positions: %s" % str(e)[:200]
        send_telegram(err_msg)

    # v3.4.47 — Hard Eject: close positions whose DI or regime
    # has flipped against them (runs before new-entry scan).
    try:
        _tiger_hard_eject_check()
    except Exception as e:
        logger.error("_tiger_hard_eject_check crashed: %s", e,
                     exc_info=True)

    # Feature 8: scan pause — only block NEW entries
    if _scan_paused:
        logger.info("SCAN CYCLE done in %.2fs — paused (manage only)", time.time() - cycle_start)
        return

    # Check for new entries on tradable tickers (long + short).
    # v3.4.40 — paper and Robinhood are now evaluated INDEPENDENTLY.
    # check_entry() is the shared signal/indicator gate; the portfolio-
    # side decision (halt, cash, concurrency, per-ticker cap) is per-
    # book. A paper-held ticker no longer blocks RH from entering, and
    # vice versa.
    for ticker in TRADE_TICKERS:
        # Refresh the dashboard gate snapshot from the current OR
        # envelope before any entry gates run. Side + break are derived
        # purely from OR vs price each cycle (no latch).
        try:
            _update_gate_snapshot(ticker)
        except Exception as e:
            logger.error("_update_gate_snapshot error %s: %s", ticker, e)
        # Long entry check — run once per ticker and fan out to both books.
        try:
            # Fast path: if both books already hold this ticker, skip the
            # signal compute. Otherwise run check_entry so the signal
            # decision is made once for the scan cycle.
            paper_holds = ticker in positions
            if not paper_holds:
                ok, bars = check_entry(ticker)
                if ok and bars:
                    px = bars["current_price"]
                    try:
                        execute_entry(ticker, px)
                    except Exception as e:
                        logger.error("Paper entry error %s: %s", ticker, e)
        except Exception as e:
            logger.error("Entry check error %s: %s", ticker, e)
        # Short entry check (Wounded Buffalo) — paper + RH handled inside.
        try:
            check_short_entry(ticker)
        except Exception as e:
            logger.error("Short entry check error %s: %s", ticker, e)

    logger.info("SCAN CYCLE done in %.2fs — %d tickers", time.time() - cycle_start, len(TRADE_TICKERS))


# ============================================================
# RESET DAILY STATE
# ============================================================
def reset_daily_state():
    """Reset OR data and daily counts for new trading day.
    (v3.4.34: AVWAP reset removed — AVWAP state no longer tracked.)
    """
    global or_collected_date, daily_entry_date, _trading_halted, _trading_halted_reason
    global daily_short_entry_count

    now_et = _now_et()
    today = now_et.strftime("%Y-%m-%d")

    if or_collected_date != today:
        or_high.clear()
        or_low.clear()
        pdc.clear()
        or_stale_skip_count.clear()
        or_collected_date = ""

    if daily_entry_date != today:
        daily_entry_count.clear()
        daily_short_entry_count.clear()
        paper_trades.clear()
        daily_entry_date = today

    _trading_halted = False
    _trading_halted_reason = ""


# ============================================================
# SCHEDULER THREAD
# ============================================================
def scheduler_thread():
    """Background scheduler — all times in ET."""
    DAY_NAMES = [
        "monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday",
    ]

    fired = set()
    last_scan = _now_et() - timedelta(seconds=SCAN_INTERVAL + 1)
    last_state_save = _now_et() - timedelta(minutes=6)

    # Job table: (day, "HH:MM", function)
    # Note: times are ET.  8:20 CT = 9:20 ET, 8:31 CT = 9:31 ET
    JOBS = [
        ("daily", "09:20", lambda: _fire_system_test("8:20 CT")),
        ("daily", "09:30", reset_daily_state),
        ("daily", "09:31", lambda: _fire_system_test("8:31 CT")),
        ("daily", "09:35",
         lambda: threading.Thread(target=collect_or, daemon=True).start()),
        ("daily", "09:36", send_or_notification),
        ("daily", "15:55", eod_close),
        ("daily", "15:58", send_eod_report),
        ("sunday", "18:00", send_weekly_digest),
    ]

    logger.info("Scheduler started — market times ET, display CDT (UTC offset: %s)",
                datetime.now(timezone.utc).strftime("%z"))

    while True:
        now_et = _now_et()
        now_hhmm = now_et.strftime("%H:%M")
        now_day = DAY_NAMES[now_et.weekday()]
        fire_key = now_et.strftime("%Y-%m-%d") + "-" + now_hhmm

        # Timed jobs
        for day, hhmm, fn in JOBS:
            job_key = fire_key + "-" + day + "-" + hhmm
            if now_hhmm != hhmm:
                continue
            match = (
                (day == "daily" and now_et.weekday() < 5)
                or day == "everyday"
                or day == now_day
            )
            if match and job_key not in fired:
                fired.add(job_key)
                fn_name = getattr(fn, "__name__", "lambda")
                logger.info("Firing scheduled job: %s %s ET -> %s",
                            day, hhmm, fn_name)
                try:
                    fn()
                except Exception as e:
                    logger.error("Scheduled job error (%s %s): %s",
                                 day, hhmm, e, exc_info=True)

        # Prune fired set daily
        if len(fired) > 200:
            today_prefix = now_et.strftime("%Y-%m-%d")
            fired = {k for k in fired if k.startswith(today_prefix)}

        # Scan loop — every SCAN_INTERVAL seconds
        elapsed = (now_et - last_scan).total_seconds()
        if elapsed >= SCAN_INTERVAL:
            last_scan = now_et
            try:
                scan_loop()
            except Exception as e:
                logger.error("scan_loop error: %s", e, exc_info=True)

        # Periodic state save — every 5 minutes
        state_elapsed = (now_et - last_state_save).total_seconds() / 60
        if state_elapsed >= 5:
            last_state_save = now_et
            threading.Thread(target=save_paper_state, daemon=True).start()

        time.sleep(30)


# ============================================================
# HEALTH CHECK (keep Railway deployment alive)
# ============================================================
def health_ping():
    """Periodic health check log line — keeps the process visible."""
    while True:
        logger.debug("Health ping — alive")
        time.sleep(300)


# ============================================================
# PERFORMANCE STATS HELPER
# ============================================================
def _compute_perf_stats(history, date_filter=None):
    """Compute performance stats from a trade history list.
    If date_filter is given, only include trades on/after that date string.
    Returns dict with stats or None if no trades.
    """
    trades = history
    if date_filter:
        trades = [t for t in history if t.get("date", "") >= date_filter]
    if not trades:
        return None
    n = len(trades)
    wins = sum(1 for t in trades if t.get("pnl", 0) >= 0)
    losses = n - wins
    wr = (wins / n * 100) if n > 0 else 0
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    win_trades = [t for t in trades if t.get("pnl", 0) >= 0]
    loss_trades = [t for t in trades if t.get("pnl", 0) < 0]
    avg_win = (sum(t["pnl"] for t in win_trades) / len(win_trades)) if win_trades else 0
    avg_loss = (sum(t["pnl"] for t in loss_trades) / len(loss_trades)) if loss_trades else 0
    best = max(trades, key=lambda t: t.get("pnl", 0))
    worst = min(trades, key=lambda t: t.get("pnl", 0))
    return {
        "n": n, "wins": wins, "losses": losses, "wr": wr,
        "total_pnl": total_pnl, "avg_win": avg_win, "avg_loss": avg_loss,
        "best": best, "worst": worst,
    }


def _compute_streak(history):
    """Compute current consecutive win/loss streak from most recent trade backward."""
    if not history:
        return "N/A"
    sorted_h = sorted(history, key=lambda t: (t.get("date", ""), t.get("exit_time", "")))
    last = sorted_h[-1]
    is_win = last.get("pnl", 0) >= 0
    count = 0
    for t in reversed(sorted_h):
        t_win = t.get("pnl", 0) >= 0
        if t_win == is_win:
            count += 1
        else:
            break
    label = "W" if is_win else "L"
    return "%d%s (current)" % (count, label)


# ============================================================
# TELEGRAM COMMANDS
# ============================================================
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


def _dashboard_sync():
    """Build dashboard text (blocking I/O — run in executor)."""
    SEP = "\u2500" * 34
    now_et = _now_et()
    time_cdt = _now_cdt().strftime("%I:%M %p CDT")
    today = now_et.strftime("%Y-%m-%d")

    weekday = now_et.weekday() < 5
    in_hours = (
        weekday
        and now_et.hour >= 9
        and (now_et.hour < 15 or (now_et.hour == 15 and now_et.minute < 55))
    )
    market_status = "OPEN" if in_hours else "CLOSED"

    # Index filters — fetch live prices
    spy_bars = fetch_1min_bars("SPY")
    qqq_bars = fetch_1min_bars("QQQ")
    spy_price = spy_bars["current_price"] if spy_bars else 0.0
    qqq_price = qqq_bars["current_price"] if qqq_bars else 0.0
    spy_pdc_d = pdc.get("SPY") or 0
    qqq_pdc_d = pdc.get("QQQ") or 0
    spy_ok = spy_price > spy_pdc_d if spy_pdc_d > 0 else False
    qqq_ok = qqq_price > qqq_pdc_d if qqq_pdc_d > 0 else False
    spy_icon = "\u2705" if spy_ok else "\u274c"
    qqq_icon = "\u2705" if qqq_ok else "\u274c"

    lines = [
        "\U0001f4ca DASHBOARD  %s" % time_cdt,
        SEP,
    ]

    # Paper portfolio only — Day P&L includes long SELLs + short COVERs
    n_pos = len(positions) + len(short_positions)
    _, _, day_pnl, _, _, _ = _today_pnl_breakdown()

    total_value = paper_cash
    for ticker, pos in positions.items():
        bars = fetch_1min_bars(ticker)
        if bars:
            total_value += bars["current_price"] * pos["shares"]
        else:
            total_value += pos["entry_price"] * pos["shares"]
    # Shorts: subtract current buy-back liability (the proceeds are
    # already in paper_cash). See short-accounting note on /positions.
    for s_ticker, s_pos in short_positions.items():
        s_bars = fetch_1min_bars(s_ticker)
        s_cur = s_bars["current_price"] if s_bars else s_pos["entry_price"]
        total_value -= s_cur * s_pos["shares"]

    paper_cash_fmt = format(paper_cash, ",.2f")
    total_value_fmt = format(total_value, ",.2f")
    day_pnl_fmt = format(day_pnl, "+,.2f")
    lines += [
        "\U0001f4c4 PAPER PORTFOLIO",
        "  Cash:       $%s" % paper_cash_fmt,
        "  Positions:  %d open" % n_pos,
        "  Today P&L:  $%s" % day_pnl_fmt,
        "  Est. Value: $%s" % total_value_fmt,
    ]

    lines += [
        SEP,
        "\U0001f4c8 INDEX FILTERS",
        "  SPY  $%.2f  PDC $%.2f  %s" % (spy_price, spy_pdc_d, spy_icon),
        "  QQQ  $%.2f  PDC $%.2f  %s" % (qqq_price, qqq_pdc_d, qqq_icon),
        "  Market: %s" % market_status,
        SEP,
        "\U0001f4d0 TODAY'S OR LEVELS",
    ]

    # OR levels (High + Low)
    or_ready = or_collected_date == today
    if or_ready:
        for t in TRADE_TICKERS:
            orh_val = or_high.get(t)
            orl_val = or_low.get(t)
            if orh_val is not None:
                orl_str = "%.2f" % orl_val if orl_val is not None else "--"
                lines.append("  %s  H:$%.2f  L:$%s" % (t, orh_val, orl_str))
            else:
                lines.append("  %s --" % t)
    else:
        lines.append("  (OR not collected yet)")

    return "\n".join(lines)


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


def _status_text_sync():
    """Build full status text (blocking I/O — run in executor)."""
    now_et = _now_et()
    sep = "\u2500" * 34

    # Paper portfolio
    n_pos = len(positions)
    header = "Open Positions (%d)" % n_pos
    lines = [header, sep]

    total_unreal_pnl = 0.0
    total_market_value = 0.0

    if not positions:
        lines.append("No open positions")
    else:
        for ticker, pos in positions.items():
            bars = fetch_1min_bars(ticker)
            entry_p = pos["entry_price"]
            shares = pos["shares"]
            if bars:
                cur = bars["current_price"]
                pos_pnl = (cur - entry_p) * shares
                pos_pnl_pct = ((cur - entry_p) / entry_p * 100) if entry_p else 0
                mkt_val = cur * shares
                total_unreal_pnl += pos_pnl
                total_market_value += mkt_val
                if pos.get("trail_active") and pos.get("trail_stop") and pos["trail_stop"] > 0:
                    peak = pos.get("trail_high", 0)
                    stop_line = "  Stop:   $%.2f [\U0001f3af trail | peak $%.2f]" % (pos["trail_stop"], peak)
                else:
                    stop_line = "  Stop:   $%.2f [stop]" % pos["stop"]
                lines.append("%s  %d shares" % (ticker, shares))
                lines.append(
                    "  Entry:  $%.2f  ->  Now: $%.2f" % (entry_p, cur)
                )
                lines.append(
                    "  P&L:   $%+.2f (%+.1f%%)" % (pos_pnl, pos_pnl_pct)
                )
                lines.append(
                    "  Value:  $%s" % format(mkt_val, ",.2f")
                )
                lines.append(stop_line)
            else:
                mkt_val = entry_p * shares
                total_market_value += mkt_val
                lines.append("%s  %d shares" % (ticker, shares))
                lines.append("  Entry:  $%.2f  ->  price unavailable" % entry_p)
                lines.append("  Stop:   $%.2f" % pos["stop"])
            lines.append(sep)

    # Totals
    if positions:
        lines.append("Total Unrealized P&L: $%+.2f" % total_unreal_pnl)
        lines.append("Total Market Value:   $%s" % format(total_market_value, ",.2f"))

    # Today's completed trades (always show, date-filtered, includes shorts)
    today_date = now_et.strftime("%Y-%m-%d")
    today_sells = [t for t in paper_trades
                   if t.get("action") == "SELL" and t.get("date") == today_date]
    short_today = [t for t in short_trade_history if t.get("date") == today_date]
    day_pnl = (sum(t.get("pnl", 0) for t in today_sells)
               + sum(t.get("pnl", 0) for t in short_today))
    day_trades = len(today_sells) + len(short_today)
    lines.append("Day P&L: $%+.2f  (%d trades)" % (day_pnl, day_trades))

    # Short positions (paper)
    lines.append(sep)
    lines.append("\U0001fa78 SHORT POSITIONS (Wounded Buffalo)")
    lines.append(sep)
    if not short_positions:
        lines.append("No short positions open.")
    else:
        for s_ticker, s_pos in short_positions.items():
            s_entry = s_pos["entry_price"]
            s_shares = s_pos["shares"]
            s_bars = fetch_1min_bars(s_ticker)
            if s_bars:
                s_cur = s_bars["current_price"]
                s_pnl = (s_entry - s_cur) * s_shares
                if s_pos.get("trail_active") and s_pos.get("trail_stop") and s_pos["trail_stop"] > 0:
                    s_low = s_pos.get("trail_low", 0)
                    s_stop_txt = "$%.2f [\U0001f3af trail | low  $%.2f]" % (s_pos["trail_stop"], s_low)
                else:
                    s_stop_txt = "$%.2f [stop]" % s_pos["stop"]
                lines.append("%s  Entry $%.2f  Stop %s"
                             % (s_ticker, s_entry, s_stop_txt))
                lines.append("      Current $%.2f  P&L $%+.2f"
                             % (s_cur, s_pnl))
            else:
                lines.append("%s  Entry $%.2f  Stop $%.2f  (price unavailable)"
                             % (s_ticker, s_entry, s_pos["stop"]))

    lines.append("Paper Cash:           $%s" % format(paper_cash, ",.2f"))

    # Portfolio equity summary. See note in the TP branch above for the
    # short accounting fix (v3.3.3 hotfix).
    short_unreal = 0.0
    short_liability = 0.0
    for s_ticker, s_pos in short_positions.items():
        s_bars = fetch_1min_bars(s_ticker)
        cur_px = s_bars["current_price"] if s_bars else s_pos["entry_price"]
        short_unreal += (s_pos["entry_price"] - cur_px) * s_pos["shares"]
        short_liability += cur_px * s_pos["shares"]
    all_unreal = total_unreal_pnl + short_unreal
    equity = paper_cash + total_market_value - short_liability
    vs_start = equity - PAPER_STARTING_CAPITAL
    lines.append(sep)
    lines.append("\U0001f4bc Portfolio Snapshot")
    lines.append("  Cash:          $%s" % format(paper_cash, ",.2f"))
    lines.append("  Long MV:       $%s" % format(total_market_value, ",.2f"))
    if short_liability > 0:
        lines.append("  Short Liab:    $%s" % format(short_liability, ",.2f"))
    lines.append("  Total Equity:  $%s" % format(equity, ",.2f"))
    lines.append("  Unrealized P&L:    $%+.2f" % all_unreal)
    lines.append("  vs Start:        $%+.2f  (started at $%s)"
                 % (vs_start, format(PAPER_STARTING_CAPITAL, ",.0f")))
    lines.append(sep)

    # OR status
    if or_collected_date == now_et.strftime("%Y-%m-%d"):
        lines.append("OR: collected")
    else:
        lines.append("OR: not yet collected")

    # PDC status (v3.4.34: swapped from AVWAP)
    spy_pdc_s = pdc.get("SPY") or 0
    qqq_pdc_s = pdc.get("QQQ") or 0
    if spy_pdc_s > 0:
        lines.append("SPY PDC: $%.2f" % spy_pdc_s)
    if qqq_pdc_s > 0:
        lines.append("QQQ PDC: $%.2f" % qqq_pdc_s)

    return "\n".join(lines)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show open positions with live prices, unrealized P&L, and TP summary."""
    t0 = asyncio.get_event_loop().time()
    await update.message.reply_chat_action(ChatAction.TYPING)
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, _status_text_sync)

    refresh_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("\U0001f504 Refresh", callback_data="positions_refresh")
    ]])
    await update.message.reply_text(text, reply_markup=refresh_kb)

    # Portfolio pie chart (run in thread to avoid blocking event loop)
    sent_photo = False
    if MATPLOTLIB_AVAILABLE and (positions or short_positions):
        buf = await loop.run_in_executor(None, _chart_portfolio_pie, positions, short_positions, paper_cash)
        if buf:
            await update.message.reply_photo(photo=buf, caption="Portfolio Allocation", reply_markup=_menu_button())
            sent_photo = True

    if not sent_photo:
        await update.effective_message.reply_text("\u2500", reply_markup=_menu_button())
    logger.info("CMD status completed in %.2fs", asyncio.get_event_loop().time() - t0)


def _build_positions_text():
    """Build positions text for refresh callback."""
    now_et = _now_et()
    sep = "\u2500" * 34
    pos_dict = positions
    short_dict = short_positions
    trades_list = paper_trades
    short_hist = short_trade_history
    cash = paper_cash
    label = "Open Positions"
    cash_label = "Paper Cash"

    n_pos = len(pos_dict)
    lines = ["%s (%d)" % (label, n_pos), sep]
    total_unreal = 0.0
    total_market_value = 0.0
    if not pos_dict:
        lines.append("No open positions")
    else:
        for ticker, pos in pos_dict.items():
            bars = fetch_1min_bars(ticker)
            ep = pos["entry_price"]
            sh = pos["shares"]
            if bars:
                cur = bars["current_price"]
                pnl = (cur - ep) * sh
                pct = ((cur - ep) / ep * 100) if ep else 0
                mkt_val = cur * sh
                total_unreal += pnl
                total_market_value += mkt_val
                if pos.get("trail_active") and pos.get("trail_stop") and pos["trail_stop"] > 0:
                    peak = pos.get("trail_high", 0)
                    stop_line = "  Stop:   $%.2f [\U0001f3af trail | peak $%.2f]" % (pos["trail_stop"], peak)
                else:
                    stop_line = "  Stop:   $%.2f [stop]" % pos["stop"]
                lines.append("%s  %d shares" % (ticker, sh))
                lines.append("  Entry:  $%.2f  ->  Now: $%.2f" % (ep, cur))
                lines.append("  P&L:   $%+.2f (%+.1f%%)" % (pnl, pct))
                lines.append("  Value:  $%s" % format(mkt_val, ",.2f"))
                lines.append(stop_line)
            else:
                mkt_val = ep * sh
                total_market_value += mkt_val
                lines.append("%s  %d shares" % (ticker, sh))
                lines.append("  Entry:  $%.2f  ->  price unavailable" % ep)
            lines.append(sep)
    if pos_dict:
        lines.append("Total Unrealized P&L: $%+.2f" % total_unreal)
        lines.append("Total Market Value:   $%s" % format(total_market_value, ",.2f"))
    today = now_et.strftime("%Y-%m-%d")
    today_sells = [t for t in trades_list if t.get("action") == "SELL" and t.get("date") == today]
    short_today = [t for t in short_hist if t.get("date") == today]
    day_pnl = sum(t.get("pnl", 0) for t in today_sells) + sum(t.get("pnl", 0) for t in short_today)
    day_trades = len(today_sells) + len(short_today)
    lines.append("Day P&L: $%+.2f  (%d trades)" % (day_pnl, day_trades))
    lines.append(sep)
    lines.append("\U0001fa78 SHORT POSITIONS (Wounded Buffalo)")
    lines.append(sep)
    if not short_dict:
        lines.append("No short positions open.")
    else:
        for s_ticker, s_pos in short_dict.items():
            s_entry = s_pos["entry_price"]
            s_shares = s_pos["shares"]
            s_bars = fetch_1min_bars(s_ticker)
            if s_bars:
                s_cur = s_bars["current_price"]
                s_pnl = (s_entry - s_cur) * s_shares
                if s_pos.get("trail_active") and s_pos.get("trail_stop") and s_pos["trail_stop"] > 0:
                    s_low = s_pos.get("trail_low", 0)
                    s_stop_txt = "$%.2f [\U0001f3af trail | low  $%.2f]" % (s_pos["trail_stop"], s_low)
                else:
                    s_stop_txt = "$%.2f [stop]" % s_pos["stop"]
                lines.append("%s  Entry $%.2f  Stop %s" % (s_ticker, s_entry, s_stop_txt))
                lines.append("      Current $%.2f  P&L $%+.2f" % (s_cur, s_pnl))
            else:
                lines.append("%s  Entry $%.2f  Stop $%.2f  (price unavailable)"
                             % (s_ticker, s_entry, s_pos["stop"]))
    lines.append("%s:           $%s" % (cash_label, format(cash, ",.2f")))

    # Portfolio equity summary. Short accounting fix (v3.3.3): the
    # short-sale proceeds are already in cash; the short contributes
    # only its unrealized P&L to equity. Previously we added
    # entry_price * shares as "market value", which double-counted
    # the proceeds and inflated equity by roughly the short principal.
    short_unreal = 0.0
    short_liability = 0.0
    for s_ticker, s_pos in short_dict.items():
        s_bars = fetch_1min_bars(s_ticker)
        cur_px = s_bars["current_price"] if s_bars else s_pos["entry_price"]
        short_unreal += (s_pos["entry_price"] - cur_px) * s_pos["shares"]
        short_liability += cur_px * s_pos["shares"]
    all_unreal = total_unreal + short_unreal
    equity = cash + total_market_value - short_liability
    _start_cap = PAPER_STARTING_CAPITAL
    vs_start = equity - _start_cap
    snap_label = "\U0001f4bc Portfolio Snapshot"
    lines.append(sep)
    lines.append(snap_label)
    lines.append("  Cash:          $%s" % format(cash, ",.2f"))
    lines.append("  Long MV:       $%s" % format(total_market_value, ",.2f"))
    if short_liability > 0:
        lines.append("  Short Liab:    $%s" % format(short_liability, ",.2f"))
    lines.append("  Total Equity:  $%s" % format(equity, ",.2f"))
    lines.append("  Unrealized P&L:    $%+.2f" % all_unreal)
    lines.append("  vs Start:        $%+.2f  (started at $%s)"
                 % (vs_start, format(_start_cap, ",.0f")))
    lines.append(sep)

    return "\n".join(lines)


async def positions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle refresh button tap on /status and /positions.

    Appends a 'Refreshed HH:MM:SS CDT' footer so each tap produces a
    visibly different message \u2014 Telegram rejects edits whose body
    and markup are identical to the current message with
    'Message is not modified'. If that race still wins (rapid double
    tap in the same second), we swallow the error silently; the user
    already got the button-tap acknowledgment via query.answer().
    """
    query = update.callback_query
    await query.answer("Refreshing...")
    loop = asyncio.get_event_loop()
    msg = await loop.run_in_executor(None, _build_positions_text)
    # Ensure content changes between taps even if prices and positions
    # are momentarily identical (common outside market hours).
    stamp = _now_cdt().strftime("%H:%M:%S CDT")
    msg = "%s\n\u21bb Refreshed %s" % (msg, stamp)
    refresh_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("\U0001f504 Refresh", callback_data="positions_refresh")
    ]])
    try:
        await query.edit_message_text(msg, reply_markup=refresh_kb)
    except Exception as e:
        # Harmless race ("Message is not modified") \u2014 don't surface.
        logger.debug("positions_callback edit failed: %s", e)


def _dayreport_time(t, key):
    """Extract display time HH:MM from a trade record (CDT)."""
    iso = t.get(key + "_iso", "")
    if iso:
        return _parse_time_to_cdt(iso)
    raw = t.get(key, "")
    if raw and ":" in raw:
        return _parse_time_to_cdt(raw)
    return "..."


def _dayreport_sort_key(t):
    """Sort key for chronological ordering of trades."""
    iso = t.get("exit_time_iso", "")
    if iso:
        return iso
    return t.get("exit_time", "") or t.get("date", "")


def _short_reason(reason_key):
    """Map a reason key to short dayreport label."""
    full = REASON_LABELS.get(reason_key, reason_key)
    # Match by leading emoji character
    if full:
        first_char = full[0]
        if first_char in _SHORT_REASON:
            return _SHORT_REASON[first_char]
    return full


def _fmt_pnl(val):
    """Format P&L with unicode minus."""
    if val < 0:
        return "\u2212$%.2f" % abs(val)
    return "+$%.2f" % val


def _chart_dayreport(trades, day_label):
    """Generate trade P&L bar chart with cumulative line. Returns BytesIO or None."""
    if not MATPLOTLIB_AVAILABLE or not trades:
        return None
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
    if not MATPLOTLIB_AVAILABLE or not history:
        return None
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
    if not MATPLOTLIB_AVAILABLE:
        return None
    if not pos_dict and not short_dict:
        return None
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
    long_pos = positions
    short_pos = short_positions

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
    """Send text in ≤max_len-char chunks, splitting on newlines."""
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
    paper_long = [
        t for t in trade_history
        if t.get("date", "") == target_str
    ]
    paper_short = [
        t for t in short_trade_history
        if t.get("date", "") == target_str
    ]
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
            "No trades on {date}.".format(date=target_str),
            reply_markup=_menu_button()
        )
        logger.info("CMD dayreport completed in %.2fs (no trades)", asyncio.get_event_loop().time() - t0)
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
            await update.message.reply_photo(photo=buf, caption="Trade P&L \u2014 %s" % day_label, reply_markup=_menu_button())
        else:
            try:
                await chart_msg.edit_text("\U0001f4ca Chart unavailable (no trades or matplotlib missing)", reply_markup=_menu_button())
            except Exception:
                pass
    else:
        await update.effective_message.reply_text("\u2500", reply_markup=_menu_button())
    logger.info("CMD dayreport completed in %.2fs", asyncio.get_event_loop().time() - t0)


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
    rows = []
    is_today = (target_str == today_str)

    live_long = paper_trades
    long_hist = trade_history
    short_hist = short_trade_history
    open_shorts = short_positions

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
        # Closed shorts today — synthesize an OPEN row + a COVER row
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
        # Currently-open shorts from today — add a SHORT open row only
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


def _log_sync(target_str, day_label):
    """Build trade log text (pure CPU — run in executor). Returns text or None."""
    SEP = "\u2500" * 34
    today_str = _now_et().strftime("%Y-%m-%d")
    rows = _collect_day_rows(target_str, today_str)
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

    n_open = len(positions) + len(short_positions)
    lines.append(SEP)
    lines.append("Completed: %d trades  Open: %d positions" % (n_closed, n_open))
    lines.append("Day P&L: ${:+,.2f}".format(day_pnl))
    return "\n".join(lines)


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
            await prog.edit_text("\u26a0\ufe0f Trade log timed out. Try again.", reply_markup=_menu_button())
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


def _replay_sync(target_str, day_label):
    """Build replay text (pure CPU — run in executor). Returns text or None."""
    SEP = "\u2500" * 34
    today_str = _now_et().strftime("%Y-%m-%d")

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
        # Currently-open short positions on the target date — add a SHORT
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
        _push_live(paper_trades)
        # v3.4.7: today's shorts (closed + open) live elsewhere
        _push_history(short_trade_history, "SHORT", "COVER")
        _push_open_shorts(short_positions)
    else:
        _push_history(trade_history, "BUY", "SELL")
        _push_history(short_trade_history, "SHORT", "COVER")

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
        await update.message.reply_text("\u26a0\ufe0f Replay timed out. Try again.", reply_markup=_menu_button())
        return

    if text is None:
        await update.message.reply_text("No trades on %s." % day_label, reply_markup=_menu_button())
        logger.info("CMD replay completed in %.2fs (no trades)", asyncio.get_event_loop().time() - t0)
        return

    await _reply_in_chunks(update.message, text, reply_markup=_menu_button())
    logger.info("CMD replay completed in %.2fs", asyncio.get_event_loop().time() - t0)


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show version info."""
    await update.message.reply_text(
        "%s v%s\n%s" % (BOT_NAME, BOT_VERSION, MAIN_RELEASE_NOTE),
        reply_markup=_menu_button())


# ============================================================
# /near_misses COMMAND (v3.4.21)
# ============================================================
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
            "that was declined by the volume gate."
            % SEP,
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


# ============================================================
# /retighten COMMAND (v3.4.23)
# ============================================================
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
            force_exit=True, fetch_prices=True,
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
                lines.append("  stop $%.2f \u2192 $%.2f" % (old, new))
                any_change = True
            elif status == "ratcheted":
                lines.append("%s %s [%s] breakeven" % (tkr, side, port))
                lines.append("  stop $%.2f \u2192 $%.2f" % (old, new))
                any_change = True
            elif status == "ratcheted_trail":
                lines.append(
                    "%s %s [%s] trail\u2192entry" % (tkr, side, port)
                )
                lines.append("  trail $%.2f \u2192 $%.2f" % (old, new))
                any_change = True
            elif status == "exit":
                lines.append("%s %s [%s] EXITED" % (tkr, side, port))
                lines.append("  breached at cap $%.2f" % (new if new is not None else 0.0))
                any_change = True
            elif status == "no_op":
                lines.append("%s %s [%s] trail armed" % (tkr, side, port))
            elif status == "already_tight":
                lines.append("%s %s [%s] already tight" % (tkr, side, port))
                if old is not None:
                    lines.append("  stop $%.2f" % old)
        if not any_change:
            lines.append("")
            lines.append("No changes \u2014 stops already optimal.")
    lines.append(SEP)
    lines.append(
        "Summary: %d cap, %d ratchet,"
        % (result.get("tightened", 0),
           result.get("ratcheted", 0))
    )
    lines.append(
        "%d trail\u2192entry, %d exited,"
        % (result.get("ratcheted_trail", 0),
           result.get("exited", 0))
    )
    lines.append(
        "%d no-op, %d tight"
        % (result.get("no_op", 0),
           result.get("already_tight", 0))
    )
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=_menu_button(),
    )


# ============================================================
# /trade_log COMMAND — last 10 persistent-log entries (v3.4.27)
# ============================================================
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
            lines.append("err: %s" % str(
                _trade_log_last_error)[:28])
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
        lines.append("%s %-5s %s[%s] $%+.2f" % (
            date, tkr, side, port, pnl,
        ))
        # Line 2: reason + entry→exit
        entry = r.get("entry_price")
        exit_ = r.get("exit_price")
        if entry is not None and exit_ is not None:
            lines.append("  %s  $%.2f\u2192$%.2f" % (
                reason, float(entry), float(exit_),
            ))
        else:
            lines.append("  %s" % reason)

    lines.append(SEP)
    lines.append("By reason:")
    for reason, (n, p) in sorted(
        by_reason.items(), key=lambda kv: -kv[1][1]
    ):
        lines.append("  %-10s %d  $%+.2f" % (reason[:10], n, p))
    if _trade_log_last_error:
        lines.append(SEP)
        lines.append("err: %s" % str(_trade_log_last_error)[:28])

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=_menu_button(),
    )


# ============================================================
# /tp_sync COMMAND — TradersPost broker sync status (v3.4.15)
# ============================================================




# ============================================================
# /rh_enable /rh_disable /rh_status \u2014 live-trading kill switch
# ============================================================








# ============================================================
# /mode COMMAND — market mode classifier (observation only)
# ============================================================
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
        if which == "val":
            executor = val_executor
            label = "Val"
        else:
            executor = gene_executor
            label = "Gene"
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
        confirm_token = args[2] if len(args) > 2 else None
        ok, msg = executor.set_mode(sub, confirm_token=confirm_token)
        marker = "\u2705" if ok else "\u274c"
        await update.message.reply_text(f"{marker} {label}: {msg}")
        return

    SEP = "\u2500" * 34
    # Refresh once on demand so manual checks outside scan cadence are fresh.
    try:
        _refresh_market_mode()
    except Exception:
        logger.exception("/mode: refresh failed")

    mode    = _current_mode
    reason  = _current_mode_reason
    pnl     = _current_mode_pnl
    ts      = _current_mode_ts
    profile = MODE_PROFILES.get(mode, {})

    ts_str = ts.strftime("%H:%M ET") if ts else "—"
    shorts = "ON" if profile.get("allow_shorts") else "OFF"
    trail_bps = int(round(profile.get("trail_pct", 0) * 10000))

    # Build compact per-ticker RSI preview (top 6 by value, highest first)
    if _current_rsi_per_ticker:
        sorted_rsis = sorted(_current_rsi_per_ticker.items(),
                             key=lambda kv: kv[1], reverse=True)
        rsi_preview = " | ".join("%s %.0f" % (tk, r) for tk, r in sorted_rsis[:6])
    else:
        rsi_preview = "—"

    if _current_ticker_red:
        red_preview = ", ".join("%s $%+.0f" % (tk, p)
                                for tk, p in _current_ticker_red[:5])
    else:
        red_preview = "none"

    if _current_ticker_extremes:
        ext_preview = ", ".join("%s %.0f %s" % (tk, r, tag)
                                for tk, r, tag in _current_ticker_extremes[:5])
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
        "Bounds: trail %.1f-%.1f%% | entries %d-%d | shares %d-%d | score +%.2f-+%.2f" % (
            CLAMP_TRAIL_PCT[0]*100, CLAMP_TRAIL_PCT[1]*100,
            CLAMP_MAX_ENTRIES[0],   CLAMP_MAX_ENTRIES[1],
            CLAMP_SHARES[0],        CLAMP_SHARES[1],
            CLAMP_MIN_SCORE_DELTA[0], CLAMP_MIN_SCORE_DELTA[1],
        ),
        "",
        "(v%s — observation only, no parameter is adaptive yet)" % BOT_VERSION,
    ]
    await update.message.reply_text("\n".join(lines), reply_markup=_menu_button())


# ============================================================
# /algo COMMAND
# ============================================================
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
        "\U0001f6e1 SOVEREIGN REGIME SHIELD (v3.4.28)\n"
        "  Lords Left & Bull Vacuum require\n"
        "  BOTH SPY AND QQQ to cross PDC on a\n"
        "  finalized 1-min bar close \u2192 one\n"
        "  anchor across entries and ejects;\n"
        "  no sector divergence ejects.\n"
        "  (v3.4.34: AVWAP fully removed)\n"
        f"{SEP}\n"
        "Full reference guide attached \u2193"
    )
    await update.message.reply_text(summary)

    # Send PDF — try local file first, fall back to GitHub raw download
    _ALGO_PDF_URL = (
        "https://raw.githubusercontent.com/valira3/"
        "stock-spike-monitor/main/trade_genius_algo.pdf"
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


# ============================================================
# /strategy COMMAND
# ============================================================
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
        "  \U0001f451 Lords Left\n"
        "     SPY AND QQQ < PDC\n"
        "     on finalized 1m close\n"
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
        "  \U0001f300 Bull Vacuum\n"
        "     SPY AND QQQ > PDC\n"
        "     on finalized 1m close\n"
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


# ============================================================
# /reset COMMAND (Fix C)
# ============================================================

# Window in seconds during which a "Confirm" tap is accepted after the
# /reset command was issued. Beyond this, the callback is rejected — this
# prevents scrolling up to an old /reset message tomorrow and tapping
# Confirm by accident.
RESET_CONFIRM_WINDOW_SEC = 60


def _reset_authorized(query, context=None) -> tuple:
    """Gatekeeper for /reset callbacks.

    v3.5.0: paper-only single-bot. Returns (allowed: bool, reason: str).
    Checks:
      1. Owner check — the chat is one of the configured owner chats,
         OR the tapping user id is in TRADEGENIUS_OWNER_IDS. The user-id
         path lets the owner /reset from a direct message when CHAT_ID
         is a group.
      2. Freshness check — confirm callbacks carry ':<unix_ts>' suffix
         and must be within RESET_CONFIRM_WINDOW_SEC. Prevents stale
         replays.
    """
    data = query.data or ""
    chat_id_str = str(query.message.chat_id)
    try:
        user_id_str = str(query.from_user.id) if query.from_user else ""
    except Exception:
        user_id_str = ""

    # (1) Owner check
    owner_ids = {str(CHAT_ID or "")}
    owner_ids.discard("")
    is_owner_chat = chat_id_str in owner_ids or user_id_str in owner_ids
    is_owner_user = user_id_str in TRADEGENIUS_OWNER_IDS
    if not (is_owner_chat or is_owner_user):
        return (False, "unauthorized chat")

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


def _reset_buttons(action: str) -> InlineKeyboardMarkup:
    """Build a Confirm/Cancel keyboard where Confirm carries a fresh ts."""
    ts = int(time.time())
    confirm_data = "reset_%s_confirm:%d" % (action, ts)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("\u2705 Confirm", callback_data=confirm_data),
        InlineKeyboardButton("\u274c Cancel", callback_data="reset_cancel"),
    ]])


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/reset — show confirmation before resetting the paper portfolio.

    v3.5.0: paper-only. TP/Robinhood reset path removed.
    """
    await update.message.reply_text(
        "\u26a0\ufe0f Reset paper portfolio to $100,000?\nAll trade history will be cleared.\n(Confirm within 60s.)",
        reply_markup=_reset_buttons("paper"),
    )


def _do_reset_paper():
    """Execute paper portfolio reset."""
    global paper_cash, daily_entry_date
    global _trading_halted, _trading_halted_reason
    positions.clear()
    short_positions.clear()
    paper_trades.clear()
    paper_all_trades.clear()
    trade_history.clear()
    short_trade_history.clear()
    daily_entry_count.clear()
    daily_short_entry_count.clear()
    daily_entry_date = ""
    paper_cash = PAPER_STARTING_CAPITAL
    _trading_halted = False
    _trading_halted_reason = ""
    save_paper_state()


async def reset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard taps for /reset confirmation.

    Confirm callbacks carry a ':<ts>' suffix. _reset_authorized() enforces
    chat-ownership, bot/action match, and freshness. The 'reset_*'
    (non-confirm) and 'reset_cancel' variants carry no state change and
    only need the owner check.
    """
    query = update.callback_query
    await query.answer()
    paper_fmt = format(PAPER_STARTING_CAPITAL, ",.0f")

    allowed, reason = _reset_authorized(query, context)
    if not allowed:
        # v3.4.42 surface chat/user ids and configured owner env vars
        # directly in the Telegram message so the owner can diagnose
        # auth mismatches without Railway logs.
        try:
            _user = query.from_user.id if query.from_user else "?"
        except Exception:
            _user = "?"
        logger.warning(
            "reset_callback blocked: data=%s chat_id=%s user_id=%s reason=%s CHAT_ID=%r",
            query.data, query.message.chat_id, _user, reason, CHAT_ID,
        )
        owner_users_fmt = ",".join(sorted(TRADEGENIUS_OWNER_IDS)) or "(unset)"
        diag = (
            "\u274c Reset blocked: %s.\n"
            "chat_id: %s\n"
            "user_id: %s\n"
            "allowed paper: %s\n"
            "owner users: %s"
        ) % (
            reason,
            query.message.chat_id,
            _user,
            CHAT_ID or "(unset)",
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
            "\u26a0\ufe0f Reset paper portfolio to $%s?\nAll trade history will be cleared.\n(Confirm within 60s.)" % paper_fmt,
            reply_markup=_reset_buttons("paper"),
        )


# ============================================================
# /perf COMMAND (Feature 5)
# ============================================================
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
    all_stats = _compute_perf_stats(filt_long)
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
    short_stats = _compute_perf_stats(filt_short)
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
    today_long = _compute_perf_stats(long_history, date_filter=today)
    today_short = _compute_perf_stats(short_hist, date_filter=today)
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
    streak = _compute_streak(combined)
    lines.append("Streak: %s" % streak)

    msg = "\n".join(lines)

    # Chart: Equity curve
    chart_buf = None
    if MATPLOTLIB_AVAILABLE:
        chart_hist = filt_long + filt_short
        if chart_hist:
            chart_buf = _chart_equity_curve(chart_hist, perf_label)

    return msg, chart_buf


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
                None, _perf_compute,
                long_history, short_hist, date_filter, single_day,
                today, label, perf_label, long_opens, short_opens,
            ),
            timeout=10.0,
        )
    except asyncio.TimeoutError:
        logger.warning("cmd_perf: executor timed out after 10s")
        await update.message.reply_text("\u26a0\ufe0f Performance report timed out. Try again.", reply_markup=_menu_button())
        return

    await _reply_in_chunks(update.message, msg)

    if chart_buf:
        await update.message.reply_photo(photo=chart_buf, caption="Equity Curve", reply_markup=_menu_button())
    elif MATPLOTLIB_AVAILABLE and (long_history or short_hist):
        await update.message.reply_text("\U0001f4ca Chart unavailable (timeout or no data)", reply_markup=_menu_button())
    else:
        await update.effective_message.reply_text("\u2500", reply_markup=_menu_button())
    logger.info("CMD perf completed in %.2fs", asyncio.get_event_loop().time() - t0)


# ============================================================
# /price COMMAND (Feature 6)
# ============================================================
def _price_sync(ticker):
    """Build price text (blocking I/O — run in executor). Returns text or None."""
    SEP = "\u2500" * 34

    bars = fetch_1min_bars(ticker)
    if not bars:
        return None

    cur_price = bars["current_price"]
    pdc_val = bars["pdc"]
    change = cur_price - pdc_val
    change_pct = (change / pdc_val * 100) if pdc_val else 0

    header = "\U0001f4b0 %s  $%.2f  $%+.2f (%+.2f%%)" % (ticker, cur_price, change, change_pct)

    if ticker not in TRADE_TICKERS:
        return header

    lines = [header, SEP]

    # OR High
    orh = or_high.get(ticker)
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
    orl = or_low.get(ticker)
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
    pdc_strat = pdc.get(ticker)
    if pdc_strat is not None:
        if cur_price > pdc_strat:
            pdc_status = "\u2705 Above (green)"
        else:
            pdc_status = "\u274c Below (red)"
        lines.append("PDC:      $%.2f  %s" % (pdc_strat, pdc_status))
    else:
        lines.append("PDC:      $%.2f" % pdc_val)

    # SPY/QQQ vs PDC (v3.4.34: swapped from AVWAP)
    spy_pdc_t = pdc.get("SPY") or 0
    qqq_pdc_t = pdc.get("QQQ") or 0
    spy_bars = fetch_1min_bars("SPY")
    qqq_bars = fetch_1min_bars("QQQ")
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
    in_position = ticker in positions
    at_max_entries = daily_entry_count.get(ticker, 0) >= 5
    index_ok = spy_ok and qqq_ok
    long_eligible = not in_position and not at_max_entries and index_ok and not _trading_halted

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
        if _trading_halted:
            reasons.append("trading halted")
        reason_str = ", ".join(reasons)
        lines.append("Long eligible:  NO (%s)" % reason_str)

    # Short entry eligible?
    in_short = ticker in short_positions
    at_max_shorts = daily_short_entry_count.get(ticker, 0) >= 5
    index_bearish = spy_below and qqq_below
    below_or_low = (orl is not None and cur_price < orl)
    below_pdc_short = (pdc_strat is not None and cur_price < pdc_strat)
    short_eligible = (not in_short and not at_max_shorts and index_bearish
                      and below_or_low and below_pdc_short and not _trading_halted)

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
        if _trading_halted:
            s_reasons.append("trading halted")
        s_reason_str = ", ".join(s_reasons)
        lines.append("Short eligible: NO (%s)" % s_reason_str)

    return "\n".join(lines)


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
            await prog.edit_text("Could not fetch data for %s" % ticker, reply_markup=_menu_button())
        elif len(text) > 3800:
            await prog.delete()
            await _reply_in_chunks(update.message, text, reply_markup=_menu_button())
        else:
            await prog.edit_text(text, reply_markup=_menu_button())
    except Exception:
        pass
    logger.info("CMD price completed in %.2fs", asyncio.get_event_loop().time() - t0)


# ============================================================
# /proximity COMMAND (v3.3.0)
# ============================================================
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
    SEP = "\u2500" * 34
    now_et = _now_et()
    today = now_et.strftime("%Y-%m-%d")

    if or_collected_date != today:
        return None, "OR not collected yet \u2014 runs at 8:35 CT."

    # Pick the positions dicts for open-trade markers
    longs_dict = positions
    shorts_dict = short_positions

    # --- Global: SPY/QQQ vs PDC (the long gate, v3.4.34) ---
    spy_bars = fetch_1min_bars("SPY")
    qqq_bars = fetch_1min_bars("QQQ")
    spy_price = spy_bars["current_price"] if spy_bars else 0.0
    qqq_price = qqq_bars["current_price"] if qqq_bars else 0.0
    spy_pdc_p = pdc.get("SPY") or 0
    qqq_pdc_p = pdc.get("QQQ") or 0

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

    now_ct = now_et.astimezone(CDT)
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
    for t in TRADE_TICKERS:
        orh = or_high.get(t)
        orl = or_low.get(t)
        pdc_val = pdc.get(t)
        bars = fetch_1min_bars(t)
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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f504 Refresh",
                              callback_data="proximity_refresh")],
        [InlineKeyboardButton("\U0001f3e0 Menu", callback_data="open_menu")],
    ])


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
        logger.info("CMD proximity completed in %.2fs (no data)",
                    asyncio.get_event_loop().time() - t0)
        return
    body = "```\n" + text + "\n```"
    await update.message.reply_text(
        body,
        parse_mode="Markdown",
        reply_markup=_proximity_keyboard(),
    )
    logger.info("CMD proximity completed in %.2fs",
                asyncio.get_event_loop().time() - t0)


async def proximity_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle refresh button tap on /proximity."""
    query = update.callback_query
    await query.answer("Refreshing...")
    loop = asyncio.get_event_loop()
    text, err = await loop.run_in_executor(None, _proximity_sync)
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
            reply_markup=_proximity_keyboard(),
        )
    except Exception as e:
        # Common case: "Message is not modified" when nothing changed
        # between ticks. Swallow silently \u2014 the user got their ack.
        logger.debug("proximity_callback edit failed: %s", e)


# ============================================================
# /orb COMMAND (Feature 7)
# ============================================================
def _orb_sync():
    """Build ORB text (blocking I/O — run in executor). Returns text or None."""
    SEP = "\u2500" * 34
    now_et = _now_et()
    today = now_et.strftime("%Y-%m-%d")

    if or_collected_date != today:
        return None

    lines = [
        "\U0001f4d0 TODAY'S OR LEVELS \u2014 %s" % today,
        SEP,
    ]

    for t in TRADE_TICKERS:
        orh = or_high.get(t)
        orl = or_low.get(t)
        pdc_val = pdc.get(t)
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
    spy_bars = fetch_1min_bars("SPY")
    qqq_bars = fetch_1min_bars("QQQ")
    spy_price = spy_bars["current_price"] if spy_bars else 0
    qqq_price = qqq_bars["current_price"] if qqq_bars else 0
    spy_pdc_u = pdc.get("SPY") or 0
    qqq_pdc_u = pdc.get("QQQ") or 0
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
    for t in TRADE_TICKERS:
        cnt = daily_entry_count.get(t, 0)
        if cnt > 0:
            entry_parts.append("%sx%d" % (t, cnt))
    if entry_parts:
        entries_str = " ".join(entry_parts)
        lines.append("Entries today: %s" % entries_str)

    return "\n".join(lines)


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
            "OR not collected yet \u2014 runs at 8:35 CT.",
            reply_markup=_menu_button()
        )
        logger.info("CMD orb completed in %.2fs (no data)", asyncio.get_event_loop().time() - t0)
        return
    await _reply_in_chunks(update.message, text, reply_markup=_menu_button())
    logger.info("CMD orb completed in %.2fs", asyncio.get_event_loop().time() - t0)


# ============================================================
# /monitoring COMMAND (Feature 8)
# ============================================================
async def cmd_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause/resume scanner. /monitoring pause | resume | (no arg = show status)."""
    global _scan_paused
    args = context.args
    action = args[0].lower() if args else ""

    if action == "pause":
        _scan_paused = True
        await update.message.reply_text(
            "\U0001f50d Scanner: PAUSED\n"
            "  Tap below to resume.\n"
            "  Existing positions still managed.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("\u25b6\ufe0f Resume Scanner", callback_data="monitoring_resume")
            ]])
        )
    elif action == "resume":
        _scan_paused = False
        await update.message.reply_text(
            "\U0001f50d Scanner: ACTIVE\n"
            "  Watching for breakouts.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("\u23f8 Pause Scanner", callback_data="monitoring_pause")
            ]])
        )
    else:
        status = "PAUSED" if _scan_paused else "ACTIVE"
        if _scan_paused:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("\u25b6\ufe0f Resume Scanner", callback_data="monitoring_resume")
            ]])
        else:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("\u23f8 Pause Scanner", callback_data="monitoring_pause")
            ]])
        await update.message.reply_text(
            "\U0001f50d Scanner: %s\n"
            "  Existing positions still managed." % status,
            reply_markup=kb
        )
    await update.effective_message.reply_text("\u2500", reply_markup=_menu_button())


async def monitoring_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard taps for /monitoring."""
    global _scan_paused
    query = update.callback_query
    await query.answer()
    if query.data == "monitoring_pause":
        _scan_paused = True
        await query.edit_message_text(
            "\U0001f50d Scanner: PAUSED\n  Tap below to resume.\n  Existing positions still managed.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("\u25b6\ufe0f Resume Scanner", callback_data="monitoring_resume")
            ]])
        )
    elif query.data == "monitoring_resume":
        _scan_paused = False
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


# ============================================================
# /menu COMMAND — Quick tap-grid
# ============================================================
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a quick tap-grid of all commands."""
    keyboard = _build_menu_keyboard()
    await update.message.reply_text(
        "\U0001f4f1 Quick Menu\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def _cb_open_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the single Menu button tap — show full menu."""
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
        note = MAIN_RELEASE_NOTE
        await query.edit_message_text(
            "%s v%s\n%s" % (BOT_NAME, BOT_VERSION, note))
        return

    if query.data == "menu_strategy":
        await query.edit_message_text("\u23f3 Loading...")
        SEP = "\u2500" * 26
        text = (
            "Strategy v%s\n%s\n" % (BOT_VERSION, SEP)
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
        await _invoke_from_callback(query, context, cmd_help)
        return
    if query.data == "menu_algo":
        await _invoke_from_callback(query, context, cmd_algo)
        return
    if query.data == "menu_mode":
        await _invoke_from_callback(query, context, cmd_mode)
        return
    if query.data == "menu_log":
        await _invoke_from_callback(query, context, cmd_log)
        return
    if query.data == "menu_replay":
        await _invoke_from_callback(query, context, cmd_replay)
        return
    if query.data == "menu_or_recover":
        await _invoke_from_callback(query, context, cmd_or_now)
        return
    if query.data == "menu_reset":
        # /reset is a two-step confirm flow; delegate to its handler and let
        # it show the same confirmation keyboard it shows on the typed command.
        await _invoke_from_callback(query, context, cmd_reset)
        return

    await query.edit_message_text("\u23f3 Loading...")

    if query.data == "menu_dashboard":
        # Show the same full dashboard that /dashboard produces.
        # The menu message itself has already been edited to "\u23f3 Loading..."
        # above, so we just swap it out with the real dashboard text.
        loop = asyncio.get_event_loop()
        try:
            text = await loop.run_in_executor(None, _dashboard_sync)
        except Exception:
            logger.exception("menu_dashboard: _dashboard_sync failed")
            await query.message.reply_text(
                "\u26a0\ufe0f Dashboard failed. Try again.",
                reply_markup=_menu_button(),
            )
            return
        try:
            if len(text) > 3800:
                await _reply_in_chunks(query.message, text, reply_markup=_menu_button())
            else:
                await query.message.reply_text(text, reply_markup=_menu_button())
        except Exception:
            logger.exception("menu_dashboard: send failed")
    elif query.data == "menu_positions":
        loop = asyncio.get_event_loop()
        msg = await loop.run_in_executor(None, _build_positions_text)
        refresh_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("\U0001f504 Refresh", callback_data="positions_refresh")
        ]])
        await query.message.reply_text(msg, reply_markup=refresh_kb)
    elif query.data == "menu_orb":
        now_et = _now_et()
        today = now_et.strftime("%Y-%m-%d")
        if or_collected_date != today:
            await query.message.reply_text("OR not collected yet \u2014 runs at 8:35 CT.")
        else:
            orb_lines = ["\U0001f4d0 TODAY'S OR LEVELS \u2014 %s" % today]
            for t in TRADE_TICKERS:
                orh = or_high.get(t)
                if orh is None:
                    orb_lines.append("%s   --" % t)
                else:
                    orl = or_low.get(t)
                    pdc_val = pdc.get(t)
                    orl_s = "%.2f" % orl if orl else "--"
                    pdc_s = "%.2f" % pdc_val if pdc_val else "--"
                    orb_lines.append("%s  H:$%.2f  L:$%s  PDC:$%s" % (t, orh, orl_s, pdc_s))
            await query.message.reply_text("\n".join(orb_lines))
    elif query.data == "menu_dayreport":
        await _invoke_from_callback(query, context, cmd_dayreport)
    elif query.data == "menu_proximity":
        await _invoke_from_callback(query, context, cmd_proximity)
    elif query.data == "menu_perf":
        await _invoke_from_callback(query, context, cmd_perf)
    elif query.data == "menu_monitoring":
        status = "PAUSED" if _scan_paused else "ACTIVE"
        if _scan_paused:
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
        await loop.run_in_executor(None, _run_system_test_sync, "Manual")


def _fetch_or_for_ticker(ticker):
    """Try Yahoo then FMP to recover OR data for a single ticker. Returns dict or None."""
    now_et = _now_et()
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    or_end = now_et.replace(hour=9, minute=35, second=0, microsecond=0)
    open_ts = int(market_open.timestamp())
    end_ts = int(or_end.timestamp())

    # Try Yahoo 1-min bars first
    try:
        bars = fetch_1min_bars(ticker)
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
                or_high[ticker] = max_high
                if min_low is not None:
                    or_low[ticker] = min_low
                if bars.get("pdc") and bars["pdc"] > 0:
                    pdc[ticker] = bars["pdc"]
                return {"high": max_high, "low": min_low if min_low else 0, "src": "Yahoo"}
    except Exception as e:
        logger.warning("or_now Yahoo failed for %s: %s", ticker, e)

    # FMP fallback
    try:
        fmp = get_fmp_quote(ticker)
        if fmp and fmp.get("dayHigh") and fmp.get("dayLow"):
            or_high[ticker] = fmp["dayHigh"]
            or_low[ticker] = fmp["dayLow"]
            if fmp.get("previousClose") and fmp["previousClose"] > 0:
                pdc[ticker] = fmp["previousClose"]
            return {"high": fmp["dayHigh"], "low": fmp["dayLow"], "src": "FMP"}
    except Exception as e:
        logger.warning("or_now FMP failed for %s: %s", ticker, e)

    return None


def _or_now_sync():
    """Re-collect missing OR data (blocking I/O — run in executor). Returns text or None."""
    missing = [t for t in TICKERS if t not in or_high]
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
        save_paper_state()

    SEP = "\u2500" * 34
    lines = ["\U0001f504 OR Recovery Complete", SEP]
    lines.extend(results)
    lines.append(SEP)
    lines.append("%d recovered | %d still missing" % (recovered, still_fail))
    return "\n".join(lines)


async def cmd_or_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually re-collect OR data for tickers missing or_high."""
    t0 = asyncio.get_event_loop().time()
    await update.message.reply_chat_action(ChatAction.TYPING)

    missing = [t for t in TICKERS if t not in or_high]
    if not missing:
        await update.message.reply_text("\u2705 All ORs already collected.", reply_markup=_menu_button())
        logger.info("CMD or_now completed in %.2fs (none missing)", asyncio.get_event_loop().time() - t0)
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
            lines[ticker] = "\u2705 $%.2f\u2013$%.2f (%s)" % (result["high"], result["low"], result["src"])
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


# ============================================================
# /ticker COMMAND  (v3.4.33 — unified add/remove/list)
# ============================================================
# One command with sub-switches:
#   /ticker list         — show the tracked universe
#   /ticker add SYM      — add + prime PDC/OR/RSI/bars
#   /ticker remove SYM   — drop (SPY/QQQ are pinned, refused)
#
# Back-compat aliases registered as hidden handlers so any saved
# shortcuts still work:
#   /tickers          → /ticker list
#   /add_ticker SYM   → /ticker add SYM
#   /remove_ticker    → /ticker remove SYM
#
# All replies stay within the 34-char Telegram mobile-width budget.
# Mutation and persistence live in add_ticker() / remove_ticker()
# above; these handlers format the response.

_TICKER_USAGE = (
    "Usage: /ticker <sub> [SYM]\n"
    "\n"
    "  /ticker list\n"
    "  /ticker add SYM\n"
    "  /ticker remove SYM\n"
    "\n"
    "Example: /ticker add QBTS"
)


def _fmt_tickers_list() -> str:
    """Render the current ticker universe in a 34-char-safe table.
    Pinned tickers are flagged with an asterisk. Split into rows of
    5 symbols (≈ 30 chars at worst) so every line stays within the
    Telegram mobile code-block width.
    """
    n_total = len(TICKERS)
    n_trade = len(TRADE_TICKERS)
    # Build rows of up to 5 symbols each — SPY and QQQ get a trailing
    # '*' to show they're pinned, so worst case per row is 5*(5+1)+4=34.
    def _tag(t):
        return t + "*" if t in TICKERS_PINNED else t
    rows, row = [], []
    for t in TICKERS:
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
    pdc_val = pdc.get(t)
    orh_val = or_high.get(t)
    orl_val = or_low.get(t)

    # Each metric gets one 34-char-safe status line.
    m_lines = []

    # Bars liveness probe — the foundation everything else depends on.
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

    # RSI warm-up — proves bar history is deep enough.
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
        await update.message.reply_text(
            _fmt_tickers_list(), reply_markup=_menu_button())
        return
    sub = (args[0] or "").strip().lower()

    if sub in ("list", "ls", "show"):
        await update.message.reply_text(
            _fmt_tickers_list(), reply_markup=_menu_button())
        return

    if sub in ("add", "+"):
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /ticker add SYM\nExample: /ticker add QBTS",
                reply_markup=_menu_button())
            return
        await update.message.reply_chat_action(ChatAction.TYPING)
        # Run in executor — add_ticker does blocking HTTP (FMP + bars).
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, add_ticker, args[1])
        await update.message.reply_text(
            _fmt_add_reply(res), reply_markup=_menu_button())
        return

    if sub in ("remove", "rm", "del", "delete", "-"):
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /ticker remove SYM\n"
                "Example: /ticker remove QBTS",
                reply_markup=_menu_button())
            return
        res = remove_ticker(args[1])
        await update.message.reply_text(
            _fmt_remove_reply(res), reply_markup=_menu_button())
        return

    # Unknown sub-command — show usage.
    await update.message.reply_text(
        _TICKER_USAGE, reply_markup=_menu_button())


# v3.4.44: former /tickers, /add_ticker, /remove_ticker back-compat
# aliases were removed. Use /ticker list | add SYM | remove SYM instead.


# ============================================================
# TELEGRAM BOT SETUP
# ============================================================
# Commands shown in the Telegram / menu (user-facing).
#
# v3.4.44 menu cleanup: the popup is scoped to everyday-use commands.
# These typed commands still work but are intentionally hidden from
# the popup to keep it tight:
#   - /help, /test, /near_misses (advanced / rarely used)
#   - /tp_sync on the TP bot (duplicate of /rh_sync)
# These aliases were removed entirely (no handler, no popup):
#   /positions, /eod, /or_now, /tickers, /add_ticker, /remove_ticker.
MAIN_BOT_COMMANDS = [
    BotCommand("dashboard", "Full market snapshot"),
    BotCommand("status", "Open positions + P&L"),
    BotCommand("perf", "Performance stats (optional date)"),
    BotCommand("price", "Live quote for a ticker"),
    BotCommand("orb", "OR levels (add 'recover' to recollect)"),
    BotCommand("proximity", "Gap to breakout (long/short)"),
    BotCommand("mode", "Current market mode (observation)"),
    BotCommand("dayreport", "Trades + P&L (optional date)"),
    BotCommand("log", "Trade log (optional date)"),
    BotCommand("replay", "Trade timeline (optional date)"),
    BotCommand("monitoring", "Pause/resume scanner"),
    BotCommand("menu", "Quick command menu"),
    BotCommand("strategy", "Strategy summary"),
    BotCommand("algo", "Algorithm reference PDF"),
    BotCommand("version", "Release notes"),
    BotCommand("retighten", "Retighten stops to 0.75% cap"),
    BotCommand("trade_log", "Last 10 closed trades (persistent)"),
    BotCommand("ticker", "Ticker: list | add SYM | remove SYM"),
    # v3.4.38 — Robinhood live-trading kill switch.
    BotCommand("rh_status", "Robinhood kill-switch state"),
    BotCommand("rh_enable", "Enable Robinhood live trading"),
    BotCommand("rh_disable", "Disable Robinhood live trading"),
    BotCommand("reset", "Reset portfolio"),
]

# TP bot: main bot's commands plus /rh_sync (Robinhood-only).
# v3.4.38 — kill-switch commands (rh_enable/disable/status) are main-bot
# only, so strip them from the TP menu.
# v3.4.44 — /tp_sync popup entry removed (duplicate of /rh_sync); the
# typed /tp_sync handler stays as a silent alias so saved shortcuts work.
_RH_KILL_SWITCH_CMDS = {"rh_enable", "rh_disable", "rh_status"}
TP_BOT_COMMANDS = [
    bc for bc in MAIN_BOT_COMMANDS if bc.command not in _RH_KILL_SWITCH_CMDS
] + [
    BotCommand("rh_sync", "Robinhood broker sync status"),
]


async def _set_bot_commands(app: Application) -> None:
    """Register / menu commands on startup (all scopes) + send startup menu."""
    try:
        # Clear default scope first (removes any stale commands from old versions)
        await app.bot.set_my_commands(MAIN_BOT_COMMANDS, scope=BotCommandScopeDefault())
        await app.bot.set_my_commands(MAIN_BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats())
        await app.bot.set_my_commands(MAIN_BOT_COMMANDS, scope=BotCommandScopeAllGroupChats())
        logger.info("Registered %d bot commands (all scopes)", len(MAIN_BOT_COMMANDS))
    except Exception as e:
        logger.warning("Failed to set bot commands: %s", e)
    # Send startup menu
    await _send_startup_menu(app.bot, CHAT_ID)




async def _send_startup_menu(bot, chat_id):
    """Send the interactive menu to a chat on startup/deploy."""
    reply_markup = InlineKeyboardMarkup(_build_menu_keyboard())
    startup_text = (
        "\U0001f7e2 %s v%s online\n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "\U0001f5c2 Menu"
    ) % (BOT_NAME, BOT_VERSION)
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
    SEP = "\u2500" * 34
    now_et = _now_et()
    weekday = now_et.weekday() < 5
    in_hours = (
        weekday
        and now_et.hour >= 9
        and (now_et.hour < 15 or (now_et.hour == 15 and now_et.minute < 55))
    )
    market_status = "OPEN" if in_hours else "CLOSED"

    universe = " ".join(TRADE_TICKERS)
    n_paper_pos = len(positions)
    paper_cash_fmt = f"{paper_cash:,.2f}"

    main_msg = (
        f"\U0001f680 v{BOT_VERSION} deployed\n"
        f"{CURRENT_MAIN_NOTE}\n"
        f"{SEP}\n"
        f"Universe: {universe}\n"
        f"Strategy: ORB Long + Wounded Buffalo Short | PDC anchor\n"
        f"Scan:     every {SCAN_INTERVAL}s  |  Trail: Bison +1.0% / min $1.00\n"
        f"Stops:    Long OR_High\u2212$0.90  |  Short PDC+$0.90\n"
        f"{SEP}\n"
        f"\U0001f4c4 Paper:  ${paper_cash_fmt} cash | {n_paper_pos} positions\n"
        f"Market:   {market_status}\n"
        f"{SEP}\n"
        f"/help for all commands"
    )
    send_telegram(main_msg)


# v3.6.0 — Telegram owner auth guard.
# Installed as a group=-1 TypeHandler so it fires BEFORE any default
# group=0 handler. Non-owners are silently dropped: no reply is sent,
# the update is logged server-side, and ApplicationHandlerStop prevents
# any downstream handler (command, callback, etc.) from running.
#
# Edge cases (also silently dropped):
#   * update.effective_user is None — e.g. channel posts, edited
#     messages with no sender.
#   * user id not a string member of TRADEGENIUS_OWNER_IDS.
async def _auth_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Drop every Telegram update that isn't from a whitelisted owner."""
    eff_user = getattr(update, "effective_user", None)
    user_id_str = str(eff_user.id) if eff_user and getattr(eff_user, "id", None) is not None else ""
    if user_id_str and user_id_str in TRADEGENIUS_OWNER_IDS:
        return  # authorized — let downstream handlers run

    eff_chat = getattr(update, "effective_chat", None)
    chat_id_str = str(eff_chat.id) if eff_chat and getattr(eff_chat, "id", None) is not None else ""
    update_id = getattr(update, "update_id", None)
    logger.warning(
        "auth_guard: dropped non-owner update (update_id=%s user_id=%r chat_id=%r)",
        update_id, user_id_str or "(none)", chat_id_str or "(none)",
    )
    raise ApplicationHandlerStop


def run_telegram_bot():
    """Start Telegram bot (paper-only, single bot)."""
    app = (Application.builder()
           .token(TELEGRAM_TOKEN)
           .post_init(_set_bot_commands)
           .build())

    # v3.6.0 — Owner auth guard: every update is screened against
    # TRADEGENIUS_OWNER_IDS before any downstream handler sees it.
    # Must be installed FIRST (group=-1) so it runs before the default
    # group=0 command/callback handlers.
    app.add_handler(TypeHandler(Update, _auth_guard), group=-1)

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("replay", cmd_replay))
    app.add_handler(CommandHandler("dayreport", cmd_dayreport))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(CommandHandler("near_misses", cmd_near_misses))
    app.add_handler(CommandHandler("retighten", cmd_retighten))
    app.add_handler(CommandHandler("trade_log", cmd_trade_log))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("perf", cmd_perf))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("orb", cmd_orb))
    app.add_handler(CommandHandler("proximity", cmd_proximity))
    app.add_handler(CommandHandler("monitoring", cmd_monitoring))
    app.add_handler(CommandHandler("algo", cmd_algo))
    app.add_handler(CommandHandler("strategy", cmd_strategy))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("menu", cmd_menu))
    # v3.4.32 — runtime ticker universe management
    app.add_handler(CommandHandler("ticker", cmd_ticker))

    # Callback query handlers
    app.add_handler(CallbackQueryHandler(monitoring_callback, pattern="^monitoring_"))
    app.add_handler(CallbackQueryHandler(reset_callback, pattern="^reset_"))
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


# ============================================================
# STARTUP CATCH-UP
# ============================================================
def startup_catchup():
    """If restarting after 09:35 ET on a weekday, collect OR immediately."""
    now_et = _now_et()
    if now_et.weekday() >= 5:
        return
    today = now_et.strftime("%Y-%m-%d")

    # OR catch-up
    past_or_time = (now_et.hour > 9 or (now_et.hour == 9 and now_et.minute >= 35))
    if past_or_time and or_collected_date != today:
        logger.info("Catch-up: OR data stale, collecting now")
        threading.Thread(target=collect_or, daemon=True).start()


# ============================================================
# ENTRY POINT
# ============================================================
# v3.4.32 — load the editable ticker universe from tickers.json
# before anything else so load_paper_state() and retighten see the
# right TICKERS list (e.g. if a newly-added QBTS already has an
# open paper position persisted from a previous session).
_init_tickers()

load_paper_state()

# v3.4.23 — on startup, retighten every open position's stop to the
# 0.75% cap. Positions that were opened before the cap shipped (or
# that somehow have a drifted stop) get tightened here. force_exit is
# ON but fetch_prices is OFF: at process start the scanner loop
# hasn't run yet, so we'd hit Yahoo cold and probably get stale quotes
# anyway. Use entry_price as the "current" proxy — by construction
# the new capped stop can't be breached at entry_price (entry ±0.75%
# never equals entry), so force_exit is effectively silent on startup.
# The immediate-exit path fires from the first manage cycle instead,
# where real quotes are available.
try:
    _retro = retighten_all_stops(force_exit=True, fetch_prices=False)
    if _retro.get("tightened") or _retro.get("exited"):
        logger.info("[RETRO_CAP] startup: tightened %d, exited %d",
                    _retro.get("tightened", 0),
                    _retro.get("exited", 0))
except Exception as _e:
    logger.error("[RETRO_CAP] startup retighten failed: %s",
                 _e, exc_info=True)

# Live dashboard (read-only web UI). Env-gated: off unless DASHBOARD_PASSWORD is set.
# Runs in its own thread with its own asyncio loop — never touches PTB's loop.
try:
    import dashboard_server
    dashboard_server.start_in_thread()
except Exception as _dash_err:
    logger.warning("Dashboard failed to start (bot continues): %s", _dash_err)

# Startup summary
logger.info(
    "=== STARTUP SUMMARY === v%s | paper: $%.2f cash, %d pos, %d trades",
    BOT_VERSION, paper_cash, len(positions), len(trade_history),
)

# Smoke-test guard — lets smoke_test.py import this module without booting
# the Telegram client, scheduler, OR-collector, or dashboard. The test
# script sets SSM_SMOKE_TEST=1 before import. This is the ONLY place
# where that env var is read.
if os.getenv("SSM_SMOKE_TEST", "").strip() == "1":
    logger.info("SSM_SMOKE_TEST=1 \u2014 skipping catch-up, scheduler, and Telegram loop")
else:
    # v4.0.3-beta \u2014 OR seed from Alpaca historical bars BEFORE the
    # catch-up hook, so a mid-session restart lands with correct OR
    # values rather than yesterday's persisted or_high/or_low or a
    # wrong-window fallback from collect_or()'s Yahoo/FMP path.
    # Failures are non-fatal: startup_catchup() still runs and will
    # invoke collect_or() via the existing Yahoo+FMP chain.
    try:
        _seed_opening_range_all(list(TICKERS))
    except Exception:
        logger.exception("OR_SEED startup failed \u2014 continuing without seed")

    # Startup catch-up
    startup_catchup()

    # v4.0.2-beta \u2014 DI seed from Alpaca historical bars so the DI gate
    # is armed on the first scan cycle rather than waiting ~70 min
    # of live RTH. Failures here are non-fatal: DI simply warms up
    # naturally from live ticks as before.
    try:
        _seed_di_all(list(TRADE_TICKERS))
    except Exception:
        logger.exception("DI_SEED startup failed \u2014 continuing without seed")

    # Background threads
    threading.Thread(target=scheduler_thread, daemon=True).start()
    threading.Thread(target=health_ping, daemon=True).start()

    # v4.0.0-alpha — TradeGeniusVal executor (opt-in via env).
    # Enabled by default if paper keys are present; VAL_ENABLED=0 force-disables.
    # Silently skipped if disabled or creds missing so deploys without Alpaca
    # keys still boot cleanly.
    _val_enabled = os.getenv("VAL_ENABLED", "1").strip() not in ("0", "false", "False", "")
    _val_has_keys = bool(os.getenv("VAL_ALPACA_PAPER_KEY", "").strip())
    if _val_enabled and _val_has_keys:
        try:
            val_executor = TradeGeniusVal()
            val_executor.start()
            logger.info("[Val] started in %s mode", val_executor.mode)
        except Exception:
            logger.exception("[Val] startup failed \u2014 main continues")
            val_executor = None
    else:
        logger.info(
            "[Val] skipped (VAL_ENABLED=%s, VAL_ALPACA_PAPER_KEY set=%s)",
            os.getenv("VAL_ENABLED", "1"), _val_has_keys,
        )

    # v4.0.0-beta — TradeGeniusGene executor (opt-in via env, same pattern).
    _gene_enabled = os.getenv("GENE_ENABLED", "1").strip() not in ("0", "false", "False", "")
    _gene_has_keys = bool(os.getenv("GENE_ALPACA_PAPER_KEY", "").strip())
    if _gene_enabled and _gene_has_keys:
        try:
            gene_executor = TradeGeniusGene()
            gene_executor.start()
            logger.info("[Gene] started in %s mode", gene_executor.mode)
        except Exception:
            logger.exception("[Gene] startup failed \u2014 main continues")
            gene_executor = None
    else:
        logger.info(
            "[Gene] skipped (GENE_ENABLED=%s, GENE_ALPACA_PAPER_KEY set=%s)",
            os.getenv("GENE_ENABLED", "1"), _gene_has_keys,
        )

    logger.info("%s v%s started", BOT_NAME, BOT_VERSION)
    send_startup_message()
    run_telegram_bot()
