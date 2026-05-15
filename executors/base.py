"""v5.12.0 PR1 \u2014 TradeGeniusBase extracted from trade_genius.py.

Shared base class for Alpaca-backed executor sub-bots (Val, Gene). Two
cross-module symbols are required from `trade_genius`: the owner-id set
(`TRADEGENIUS_OWNER_IDS`, read at __init__ time) and the signal-bus
subscriber (`register_signal_listener`, called inside `start()`).

Circular-import avoidance: both symbols are looked up via the `_tg()`
shim at call time rather than imported at module top. trade_genius.py
imports this module BEFORE TradeGeniusVal/Gene class definitions (which
inherit from TradeGeniusBase), so any top-level `from trade_genius
import ...` here would deadlock when an external caller imports
`executors` first. Same `_tg()` pattern as broker/* and telegram_ui/*.

Verbatim move \u2014 zero behavior change. Sub-classes (TradeGeniusVal,
TradeGeniusGene) and the `val_executor` / `gene_executor` module-level
singletons remain in trade_genius.py through v5.12.0 PR 2 / PR 3.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys as _sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    TypeHandler,
)

import persistence

# Prod runs `python trade_genius.py`, so trade_genius is registered in
# sys.modules as `__main__`, NOT as `trade_genius`. Mirror the alias
# trick used by paper_state / broker / telegram_ui to make both names
# point at the same already-loaded module object.
if "trade_genius" not in _sys.modules and "__main__" in _sys.modules:
    _main = _sys.modules["__main__"]
    if getattr(_main, "BOT_NAME", None) == "TradeGenius":
        _sys.modules["trade_genius"] = _main


def _tg():
    """Live trade_genius module (handles __main__ vs imported cases)."""
    return _sys.modules.get("trade_genius") or _sys.modules.get("__main__")


logger = logging.getLogger(__name__)

# v6.0.7 \u2014 Alpaca's REST get_open_position is eventually consistent.
# After ENTRY submit, it can return 40410000 ("position not found") for
# ~1-2 s before the fill propagates. After EXIT submit, it can still
# return the pre-cover position for ~1-2 s. Pre-v6.0.7, the post-action
# reconcile took the first response at face value, which deleted rows
# the ENTRY just created (and grafted phantoms after EXIT covers).
# RECONCILE_GRACE_SECONDS bounds how long we will retry to confirm the
# expected outcome; RECONCILE_RETRY_SLEEP is the per-attempt backoff.
# Both are module-level so smoke tests can monkey-patch them to 0.
RECONCILE_GRACE_SECONDS = 4.0
RECONCILE_RETRY_SLEEP = 0.6


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

    NAME = "BASE"  # override: "Val", "Gene"
    ENV_PREFIX = ""  # override: "VAL_", "GENE_"

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
        # v5.0.3 \u2014 TELEGRAM_CHAT_ID is no longer required. Kept as an
        # optional seed for the auto-learned chat-map (back-compat: if an
        # operator had hand-set this previously, it still works on first
        # boot before any owner DMs the bot). See `_owner_chats` below.
        self.telegram_chat_id = os.getenv(p + "TELEGRAM_CHAT_ID", "").strip()
        # Unified owner list: all executor bots share the SAME owner set
        # as main (TRADEGENIUS_OWNER_IDS). One list to maintain on Railway.
        # No per-bot VAL_/GENE_TELEGRAM_OWNER_IDS — intentionally removed.
        self.owner_ids = set(_tg().TRADEGENIUS_OWNER_IDS)
        try:
            self.dollars_per_entry = float(os.getenv(p + "DOLLARS_PER_ENTRY", "10000"))
        except ValueError:
            self.dollars_per_entry = 10000.0
        # v5.1.4 \u2014 equity-aware sizing caps for the LIVE executor
        # path. Each entry is sized as
        #   min(dollars_per_entry,
        #       equity * max_pct_per_entry/100,
        #       cash - min_reserve_cash)
        # so a smaller account never blindly fires a fixed $10k entry
        # that Alpaca then rejects on the 4th signal. Paper book sizing
        # is unaffected.
        try:
            self.max_pct_per_entry = float(os.getenv(p + "MAX_PCT_PER_ENTRY", "10.0"))
        except ValueError:
            self.max_pct_per_entry = 10.0
        try:
            self.min_reserve_cash = float(os.getenv(p + "MIN_RESERVE_CASH", "500.0"))
        except ValueError:
            self.min_reserve_cash = 500.0
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
        # v5.0.3 \u2014 auto-learned owner chat-map. Each entry is
        # owner_id_str -> chat_id_int. Persisted on disk under
        # `<PREFIX>EXECUTOR_CHATS_PATH` (default
        # /data/executor_chats_<name>.json on Railway). Updated whenever
        # an owner DMs this executor bot (see _record_owner_chat hooked
        # into _auth_guard). Trade confirmations fan out to every entry.
        _tg_data_root = os.environ.get("TG_DATA_ROOT", "/data")
        default_chats_path = f"{_tg_data_root}/executor_chats_{self.NAME.lower()}.json"
        self._owner_chats_path = (
            os.getenv(p + "EXECUTOR_CHATS_PATH", "").strip() or default_chats_path
        )
        self._owner_chats: dict[str, int] = {}
        self._load_owner_chats()
        # Back-compat seed: if <PREFIX>TELEGRAM_CHAT_ID is set and we
        # don't yet have any learned chats, treat it as the seed value
        # for every owner. Once an owner DMs the bot, _record_owner_chat
        # will overwrite that owner's slot with their real chat_id.
        if self.telegram_chat_id and not self._owner_chats:
            try:
                seed = int(self.telegram_chat_id)
                for oid in self.owner_ids:
                    self._owner_chats[oid] = seed
            except ValueError:
                logger.warning(
                    "[%s] %sTELEGRAM_CHAT_ID is not an int (%r); ignoring as seed",
                    self.NAME,
                    p,
                    self.telegram_chat_id,
                )
        # Track whether we've already logged the "empty chat-map" warning
        # so the warning fires once per process, not on every signal.
        self._empty_chats_warned = False
        # v5.2.1 \u2014 executor-side view of broker positions, keyed by
        # ticker. Populated by _record_position on successful submit and
        # by _reconcile_broker_positions at boot. Used to detect orphans
        # the bot does not know about (broker accepted, client timed out).
        self.positions: dict = {}
        # v6.0.7 \u2014 wall-clock of the last ENTRY/EXIT submit per ticker,
        # used by _reconcile_position_with_broker to detect Alpaca's REST
        # eventual-consistency window. Within this grace, a 40410000 from
        # get_open_position right after an ENTRY (or a still-has-position
        # right after an EXIT) is the broker side lagging the local fill,
        # NOT a real divergence. Tracked here, not on the dict, so cleared
        # rows do not lose the timestamp.
        self._last_action_ts: dict = {}
        # v6.15.0 \u2014 last open_pnl snapshot wall-clock (monotonic).
        # Throttles broker.open_pnl.snapshot_open_pnl to one call per
        # OPEN_PNL_SNAPSHOT_MIN_INTERVAL seconds so a burst of signals
        # from a multi-ticker tick does not hammer get_all_positions.
        self._last_open_pnl_ts: float = 0.0
        # v7.0.0 Phase 5 \u2014 AON mode, set by _probe_aon_support() in start().
        # Defaulting to "software" here so tests that skip start() still
        # exercise the safe fallback path.
        self._aon_mode: str = "software"
        # v5.5.10 \u2014 rehydrate from state.db BEFORE
        # _reconcile_broker_positions runs (called from start()) so a
        # plain reboot during a live session sees persisted == broker
        # and stays silent. Wrapped: a bad load must never crash boot.
        try:
            self._load_persisted_positions()
        except Exception:
            logger.exception(
                "[%s] _load_persisted_positions failed \u2014 continuing with empty dict",
                self.NAME,
            )

    # ---------- state files ----------
    def _state_file(self, mode: str = None) -> str:
        # Primary: Railway persistent volume /data/ - checked directly so the
        # path is correct even when PAPER_STATE_FILE / PAPER_STATE_PATH env
        # vars are absent or point to the ephemeral /app/ directory.
        # Root cause of the persistent paper-reset bug: trade_genius.py reads
        # PAPER_STATE_PATH; executors read PAPER_STATE_FILE - mismatched env
        # var names meant Val/Gene state landed in /app/ and was wiped on
        # every redeploy.
        m = (mode or self.mode).strip().lower()
        fname = f"tradegenius_{self.NAME.lower()}_{m}.json"
        data_dir = "/data"
        if os.path.isdir(data_dir) and os.access(data_dir, os.W_OK):
            return os.path.join(data_dir, fname)
        # Fallback: derive directory from whichever state-path env var is set.
        # Check both names; PAPER_STATE_PATH is what Railway has configured.
        try:
            paper_state = (
                os.environ.get("PAPER_STATE_FILE")
                or os.environ.get("PAPER_STATE_PATH")
                or "paper_state.json"
            )
            d = os.path.dirname(paper_state) or "."
            return os.path.join(d, fname)
        except Exception:
            return fname

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
        # v9.1.80 -- "live file always wins" logic replaces the fragile
        # mtime comparison. The old approach: pick the most recently
        # modified file. Problem: any paper-mode state save during the
        # ~3-minute Railway startup window produces a newer paper file
        # than the live file, silently reverting live→paper on every
        # deploy. An explicit live state file means the operator
        # intentionally switched; only an explicit /mode paper command
        # (which now deletes the live file) should revert it.
        paper_path = self._state_file("paper")
        live_path = self._state_file("live")
        # Prefer live: if the live state file exists and records mode=live,
        # boot in live regardless of paper file mtime.
        if os.path.exists(live_path):
            try:
                with open(live_path, "r", encoding="utf-8") as _lf:
                    _live_state = json.load(_lf)
                if _live_state.get("mode") == "live":
                    self._state = _live_state
                    self.mode = "live"
                    logger.info(
                        "[%s] _load_state: live state file found -> mode=live",
                        self.NAME,
                    )
                    return
            except Exception:
                logger.exception(
                    "[%s] _load_state: live file parse failed, falling back",
                    self.NAME,
                )
        # Fall back to paper state file.
        chosen_path = paper_path if os.path.exists(paper_path) else None
        chosen_mode = "paper"
        if chosen_path is None:
            return
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
            self.NAME,
            account_number,
            status,
            cash,
            buying_power,
        )
        if "ACTIVE" not in status:
            return (False, f"account not ACTIVE (status={status})")
        return (
            True,
            f"live OK \u2014 acct={account_number} status={status} cash={cash} bp={buying_power}",
        )

    # ---------- mode control ----------
    def set_mode(self, new_mode: str, confirm_token: str = None):
        """Flip paper/live. Live requires confirm_token=='confirm' AND
        _live_sanity_check. Returns (ok, message)."""
        nm = (new_mode or "").strip().lower()
        if nm == "paper":
            self.mode = "paper"
            # v9.1.80 -- delete the live state file so the next Railway
            # restart boots in paper mode. Without this, _load_state's
            # "live file always wins" logic would re-enter live on restart.
            try:
                live_path = self._state_file("live")
                if os.path.exists(live_path):
                    os.remove(live_path)
                    logger.info("[%s] set_mode(paper): removed live state file", self.NAME)
            except Exception:
                logger.exception(
                    "[%s] set_mode(paper): failed to remove live state file",
                    self.NAME,
                )
            try:
                self.client = self._build_alpaca_client()
            except Exception:
                logger.exception("[%s] rebuild paper client failed", self.NAME)
                self.client = None
            self._save_state()
            # v5.5.10 \u2014 reload positions for the new mode bucket.
            self.positions = {}
            try:
                self._load_persisted_positions()
            except Exception:
                logger.exception(
                    "[%s] reload persisted positions on mode flip failed",
                    self.NAME,
                )
            return (True, "mode set to paper")
        if nm == "live":
            if confirm_token != "confirm":
                return (
                    False,
                    "live flip requires the literal 'confirm' token: /mode val live confirm",
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
            # v5.5.10 \u2014 reload positions for the new mode bucket.
            self.positions = {}
            try:
                self._load_persisted_positions()
            except Exception:
                logger.exception(
                    "[%s] reload persisted positions on mode flip failed",
                    self.NAME,
                )
            return (True, f"mode set to live \u2014 {msg}")
        return (False, f"unknown mode: {new_mode!r} (expected 'paper' or 'live')")

    # ---------- signal listener ----------
    def _scaled_signal_qty(self, signal_qty: int) -> tuple[int, float]:
        """v8.3.17 -- scale a Main-emitted signal_qty by this executor's
        equity-ratio so smaller Val/Gene accounts don't hit Main's
        notional cap.

        Returns (scaled_qty, ratio). When ex_equity >= main_equity OR
        either equity read fails, returns (signal_qty, 1.0) -- a no-op
        falls back to the legacy v5.24.0 1:1 mirror.

        Math:
          ratio = min(1.0, ex_equity / main_equity)
          scaled_qty = max(1, int(signal_qty * ratio))

        We clamp at >= 1 share when scaling down so a tiny-account
        executor still mirrors direction + ticker (even if just 1
        share); going to 0 would drop the trade entirely, which
        loses the side-tracking signal and the opposite-side guard.
        """
        if signal_qty <= 0:
            return (signal_qty, 1.0)
        # Read Main's equity. tg.paper_cash is the source of truth;
        # PortfolioBook's current_equity adds MTM but that introduces
        # noise into the scale factor and is mostly cancelled by the
        # symmetric MTM on Val's account.
        try:
            tg = _tg()
            main_equity = float(getattr(tg, "paper_cash", 0.0) or 0.0)
        except Exception:
            return (signal_qty, 1.0)
        if main_equity <= 0:
            return (signal_qty, 1.0)
        # Read this executor's equity (Alpaca account-equity).
        try:
            from engine.portfolio_equity import resolve_equity

            ex_equity = float(resolve_equity(self.NAME.lower()) or 0.0)
        except Exception:
            return (signal_qty, 1.0)
        if ex_equity <= 0:
            return (signal_qty, 1.0)
        if ex_equity >= main_equity:
            return (signal_qty, 1.0)
        ratio = ex_equity / main_equity
        scaled = max(1, int(signal_qty * ratio))
        return (scaled, ratio)

    def _shares_for(self, price: float, ticker: "str | None" = None) -> int:
        """v5.1.4 \u2014 equity-aware live sizing.

        Computes shares as
          floor(min(dollars_per_entry,
                    equity * max_pct_per_entry/100,
                    cash - min_reserve_cash) / price)
        and falls back to the legacy fixed-size path
        (`int(dollars_per_entry // price)`) if `get_account()` or the
        float casts raise. The bot must NEVER hard-fail on a network
        blip \u2014 always log and fall through.
        """
        if price is None or price <= 0:
            return 0
        legacy_qty = max(1, int(self.dollars_per_entry // price))
        client = self._ensure_client()
        if client is None:
            return legacy_qty
        try:
            acct = client.get_account()
            equity = float(getattr(acct, "equity", 0) or 0)
            cash = float(getattr(acct, "cash", 0) or 0)
            _bp = float(getattr(acct, "buying_power", 0) or 0)
        except Exception as e:
            logger.warning(
                "[%s] [SIZING_FALLBACK] get_account failed (%s) \u2014 "
                "using legacy fixed-size sizing $%.0f / $%.2f = %d sh",
                self.NAME,
                e,
                self.dollars_per_entry,
                price,
                legacy_qty,
            )
            return legacy_qty
        equity_cap = equity * (self.max_pct_per_entry / 100.0)
        cash_available = max(0.0, cash - self.min_reserve_cash)
        effective = min(self.dollars_per_entry, equity_cap, cash_available)
        if effective < price:
            logger.info(
                "[%s] [INSUFFICIENT_EQUITY] ticker=%s price=$%.2f "
                "cash=$%.2f reserve=$%.2f cap=$%.2f",
                self.NAME,
                ticker if ticker else "n/a",
                price,
                cash,
                self.min_reserve_cash,
                equity_cap,
            )
            return 0
        if effective < self.dollars_per_entry:
            logger.info(
                "[%s] [SIZE_CAPPED] %s requested=$%.0f effective=$%.0f "
                "equity=$%.0f cash=$%.0f cap=$%.0f reserve=$%.0f",
                self.NAME,
                ticker if ticker else "n/a",
                self.dollars_per_entry,
                effective,
                equity,
                cash,
                equity_cap,
                self.min_reserve_cash,
            )
        return max(1, int(effective // price))

    # ---------- chat-map persistence (v5.0.3) ----------
    def _load_owner_chats(self) -> None:
        """Load the persisted owner_id -> chat_id map from disk.

        Missing file is fine (first boot or volume reset) — leaves the
        map empty and we wait for an owner to DM the bot. Corrupted
        file logs and leaves the map empty (the next /start will
        rewrite it).
        """
        path = self._owner_chats_path
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            logger.exception(
                "[%s] failed to load owner-chats file (%s); starting empty",
                self.NAME,
                path,
            )
            return
        if not isinstance(raw, dict):
            logger.warning(
                "[%s] owner-chats file %s has unexpected shape %s; ignoring",
                self.NAME,
                path,
                type(raw).__name__,
            )
            return
        for k, v in raw.items():
            try:
                self._owner_chats[str(k)] = int(v)
            except (TypeError, ValueError):
                continue

    def _save_owner_chats(self) -> None:
        """Atomic write of the chat-map to disk."""
        path = self._owner_chats_path
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._owner_chats, f, indent=2)
            os.replace(tmp, path)
        except Exception:
            logger.exception("[%s] save owner-chats failed (%s)", self.NAME, path)

    def _record_owner_chat(self, owner_id: str, chat_id: int) -> None:
        """Update self._owner_chats and persist if the value changed.

        Called from _auth_guard on every inbound message from a verified
        owner so any DM (including /start) auto-registers the chat_id
        without the user needing to run a special command.
        """
        if not owner_id or chat_id is None:
            return
        owner_id = str(owner_id)
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            return
        if self._owner_chats.get(owner_id) == chat_id:
            return
        self._owner_chats[owner_id] = chat_id
        self._save_owner_chats()
        logger.info(
            "[%s] learned owner chat: owner_id=%s chat_id=%s (now %d entries)",
            self.NAME,
            owner_id,
            chat_id,
            len(self._owner_chats),
        )

    def _send_own_telegram(self, text: str) -> None:
        """Post to this executor's OWN Telegram chats.

        v5.0.3: fan out to every learned owner chat in self._owner_chats
        (auto-learned from inbound /start). If the map is empty, log
        once and bail — this surfaces the misconfiguration in startup
        logs instead of silently dropping every trade confirmation
        (which is what shipped pre-5.0.3).
        """
        if not self.telegram_token:
            return
        if not self._owner_chats:
            if not self._empty_chats_warned:
                logger.warning(
                    "[%s] notifications EMPTY \u2014 DM this executor's bot "
                    "/start to enable trade confirmations (chat-map at %s)",
                    self.NAME,
                    self._owner_chats_path,
                )
                self._empty_chats_warned = True
            return
        import urllib.parse

        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        # Iterate a snapshot so a concurrent _record_owner_chat can't
        # mutate the dict mid-loop.
        for owner_id, chat_id in list(self._owner_chats.items()):
            try:
                data = urllib.parse.urlencode(
                    {
                        "chat_id": chat_id,
                        "text": text,
                    }
                ).encode("utf-8")
                req = urllib.request.Request(url, data=data, method="POST")
                urllib.request.urlopen(req, timeout=10).read()
            except Exception:
                logger.exception(
                    "[%s] telegram send failed (owner_id=%s chat_id=%s)",
                    self.NAME,
                    owner_id,
                    chat_id,
                )

    # ---------- v5.2.1 idempotency + reconcile ----------
    def _build_client_order_id(self, ticker: str, direction: str) -> str:
        """Deterministic client_order_id for Alpaca submit_order.

        Format: f"{NAME}-{ticker}-{utc_iso_minute}-{direction}".
        Two signals for the same (executor, ticker, minute, direction)
        collapse to the same coid \u2014 Alpaca rejects the dup, the bot
        treats the rejection as success (broker has the original).
        """
        sym = "".join(c for c in (ticker or "").upper() if c.isalnum())
        utc_iso_minute = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")
        name = (self.NAME or "BASE").upper()
        return f"{name}-{sym}-{utc_iso_minute}-{direction}"

    def _summarize_req(self, req) -> str:
        """v7.77.0 -- compact one-line summary of a submit_order request
        for the [ALPACA-REQ] forensic log. Best-effort across alpaca-py
        request shapes (MarketOrderRequest, LimitOrderRequest, etc.).
        """
        try:
            sym = getattr(req, "symbol", "?")
            qty = getattr(req, "qty", None)
            notional = getattr(req, "notional", None)
            side = getattr(req, "side", None)
            side_v = getattr(side, "value", side)
            tif = getattr(req, "time_in_force", None)
            tif_v = getattr(tif, "value", tif)
            otype = getattr(req, "type", type(req).__name__)
            otype_v = getattr(otype, "value", otype)
            limit_price = getattr(req, "limit_price", None)
            stop_price = getattr(req, "stop_price", None)
            extended = getattr(req, "extended_hours", None)
            coid = getattr(req, "client_order_id", None)
            parts = [
                f"sym={sym}",
                f"side={side_v}",
                f"type={otype_v}",
                f"qty={qty}" if qty is not None else f"notional={notional}",
                f"tif={tif_v}",
            ]
            if limit_price is not None:
                parts.append(f"limit={limit_price}")
            if stop_price is not None:
                parts.append(f"stop={stop_price}")
            if extended:
                parts.append("extended_hours=true")
            if coid:
                parts.append(f"coid={coid}")
            return " ".join(parts)
        except Exception:
            return f"<{type(req).__name__} unsummarizable>"

    def _summarize_order(self, order) -> str:
        """v7.77.0 -- compact one-line summary of an Alpaca Order response
        for the [ALPACA-RESP] forensic log.
        """
        try:
            oid = getattr(order, "id", "?")
            status = getattr(order, "status", None)
            status_v = getattr(status, "value", status)
            filled_qty = getattr(order, "filled_qty", None)
            filled_avg_price = getattr(order, "filled_avg_price", None)
            sym = getattr(order, "symbol", "?")
            side = getattr(order, "side", None)
            side_v = getattr(side, "value", side)
            rejected_reason = getattr(order, "rejected_reason", None)
            failed_at = getattr(order, "failed_at", None)
            parts = [
                f"id={oid}",
                f"sym={sym}",
                f"side={side_v}",
                f"status={status_v}",
            ]
            if filled_qty:
                parts.append(f"filled_qty={filled_qty}")
            if filled_avg_price:
                parts.append(f"filled_avg_price={filled_avg_price}")
            if rejected_reason:
                parts.append(f"rejected_reason={rejected_reason!r}")
            if failed_at:
                parts.append(f"failed_at={failed_at}")
            return " ".join(parts)
        except Exception:
            return f"<{type(order).__name__} unsummarizable>"

    def _submit_order_idempotent(self, client, req, coid: str):
        """Wrap client.submit_order with duplicate-coid \u2192 success handling.

        On APIError whose message says the client_order_id must be unique
        (HTTP 422 from Alpaca), look up the existing order by coid and
        return it as if the submit had just succeeded. Re-raise anything
        else.

        v7.77.0 -- emits [ALPACA-REQ] / [ALPACA-RESP] / [ALPACA-ERR]
        forensic logs around every submit_order call so operator can
        diagnose broker-side rejects from Railway logs alone.
        """
        # [ALPACA-REQ] -- pre-flight request summary
        logger.info("[%s] [ALPACA-REQ] %s", self.NAME, self._summarize_req(req))
        try:
            order = client.submit_order(req)
            logger.info(
                "[%s] [ALPACA-RESP] %s",
                self.NAME,
                self._summarize_order(order),
            )
            return order
        except Exception as e:
            msg = str(e).lower()
            if "client order id" in msg and ("unique" in msg or "duplicate" in msg):
                try:
                    existing = client.get_order_by_client_id(coid)
                except Exception:
                    logger.exception(
                        "[%s] [IDEMPOTENCY] dup rejected but lookup failed coid=%s",
                        self.NAME,
                        coid,
                    )
                    raise
                logger.warning(
                    "[%s] [IDEMPOTENCY] submit_order duplicate rejected as expected: "
                    "coid=%s order_id=%s",
                    self.NAME,
                    coid,
                    getattr(existing, "id", "?"),
                )
                logger.info(
                    "[%s] [ALPACA-RESP] %s (via dup-lookup)",
                    self.NAME,
                    self._summarize_order(existing),
                )
                return existing
            # v7.77.0 -- non-duplicate broker error. Log the structured
            # [ALPACA-ERR] line so operator can grep Railway logs for
            # the exact rejection text (insufficient_buying_power,
            # pattern_day_trader, wash_sale, etc.) alongside the
            # request that triggered it.
            logger.warning(
                "[%s] [ALPACA-ERR] req=(%s) err=%s: %s",
                self.NAME,
                self._summarize_req(req),
                type(e).__name__,
                str(e)[:400],
            )
            raise

    # v5.5.10 \u2014 persistence helpers for self.positions. Backed by
    # the executor_positions table in state.db so a process restart
    # during a live session no longer looks like a divergence.
    def _load_persisted_positions(self) -> None:
        """Hydrate self.positions from state.db (executor_positions).

        Called from __init__ BEFORE _reconcile_broker_positions runs
        in start(). Silent no-op if the table is empty (first boot).
        """
        try:
            rows = persistence.load_executor_positions(self.NAME, self.mode)
        except Exception:
            logger.exception(
                "[%s] persistence.load_executor_positions failed",
                self.NAME,
            )
            return
        if not rows:
            return
        self.positions.update(rows)
        # v8.2.0 -- mirror each rehydrated row into the PortfolioBook
        # so the dashboard's per-pid positions feed isn't empty after
        # a Railway redeploy. Steady-state ENTRY rows are written to
        # the book by record_entry_with_fill; this is the BOOT path.
        # v8.3.6 -- ALSO mirror into the OrbEngine FSM + RiskBook so
        # day_states.in_position / trades_today and risk_book.open_risk
        # reflect the recovered positions (closes the "$0 / top ticker
        # 0/5" gap on the dashboard's RISK panel).
        for _tkr in rows.keys():
            self._mirror_position_into_book(_tkr)
            self._mirror_position_into_engine(_tkr)
        logger.info(
            "[%s] rehydrated %d persisted position(s) from state.db",
            self.NAME,
            len(rows),
        )

    def _persist_position(self, ticker: str) -> None:
        """INSERT OR REPLACE the row for ticker. Best-effort."""
        pos = self.positions.get(ticker)
        if not pos:
            return
        try:
            persistence.save_executor_position(
                self.NAME,
                self.mode,
                ticker,
                pos,
            )
        except Exception:
            logger.exception(
                "[%s] persistence.save_executor_position failed for %s",
                self.NAME,
                ticker,
            )

    def _mirror_position_into_book(self, ticker: str) -> None:
        """v8.2.0 -- mirror a position from self.positions into the
        per-portfolio PortfolioBook so the dashboard's
        /api/state.portfolios.<pid>.positions feed (which reads from
        book.positions, NOT self.positions) sees grafted/persisted
        rows. Closes the position_count_three_way phantom-at-broker
        watchdog alert.

        Idempotent: if the ticker is already in the book, no-op.
        Steady-state live entries flow through record_entry_with_fill
        which writes the book directly; this mirror only fires on the
        boot paths (state.db load + Alpaca reconcile) where the book
        would otherwise stay empty.
        """
        pos = self.positions.get(ticker)
        if not pos:
            return
        try:
            from engine.portfolio_book import PORTFOLIOS

            book = PORTFOLIOS.get(self.NAME.lower())
            if book is None:
                return
            side = str(pos.get("side", "LONG")).upper()
            target = book.short_positions if side == "SHORT" else book.positions
            if ticker in target:
                # Already mirrored (or live-entered through
                # record_entry_with_fill). Don't clobber a row that
                # may have richer state (trail, stop, etc.) than ours.
                return
            target[ticker] = {
                "ticker": ticker,
                "shares": int(pos.get("qty", 0) or 0),
                "entry_price": float(pos.get("entry_price", 0.0) or 0.0),
                "entry_ts_utc": pos.get("entry_ts_utc"),
                "source": pos.get("source", "BOOT_GRAFT"),
                "stop": pos.get("stop"),
            }
        except Exception:
            # Mirror is best-effort; never let it break a boot path.
            logger.exception(
                "[%s] _mirror_position_into_book failed for %s",
                self.NAME,
                ticker,
            )

    def _mirror_position_into_engine(self, ticker: str) -> None:
        """v8.3.6 -- mirror a self.positions row into the v10 OrbEngine
        FSM + RiskBook so the dashboard's CONCURRENT RISK panel,
        per-ticker trades_today counter ("top ticker N/5"), and the
        opposite-side guard all reflect reality after a boot/redeploy.

        Background: v8.2.0 covered the book.positions side. The engine
        layer is a separate state machine (`_state.day_states[*]` +
        `_risk[*]._open_tickets`) that doesn't watch executor.positions.
        Without this mirror, after a redeploy:
          - `day_states[pid, ticker].in_position` stays False
            -> opposite-side guard fails to block (operator's directive)
          - `day_states[pid, ticker].trades_today` stays 0
            -> "top ticker 0/5" even when this ticker just traded
          - `risk_book._open_tickets` is empty
            -> CONCURRENT RISK $0 even with live positions
            -> the daily-loss kill threshold's notional cap math is
               also wrong

        v8.3.4 prevents this for FUTURE redeploys via /data state
        persistence. v8.3.6 covers the case where state was lost
        BEFORE v8.3.4 was deployed -- a one-shot boot-time
        reconciliation from executor.positions to engine state.

        Idempotent: if the FSM is already IN_POS for this (pid, ticker)
        AND the RiskBook already has a matching ticket, no-op. Doesn't
        clobber a richer in-memory state.
        """
        pos = self.positions.get(ticker)
        if not pos:
            return
        try:
            import orb.live_runtime as _orb_runtime
            from orb import state as _orb_state
            from orb import risk_book as _orb_risk_book

            engine = _orb_runtime.get_engine()
            if engine is None:
                return
            pid = self.NAME.lower()
            # Bail early if the engine doesn't recognize this portfolio
            # (e.g. Val/Gene not registered, or pid mismatch).
            if pid not in engine.portfolio_ids:
                return
            # --- 1. FSM: mark in_position + transition to PHASE_IN_POS ---
            ds = engine._state.get_day_state(pid, ticker)
            if not ds.in_position:
                ds.in_position = True
                # Phase transition only if currently in a non-blocked,
                # non-in-pos phase. Don't clobber a BLOCKED_* row.
                if not ds.is_blocked() and ds.phase != _orb_state.PHASE_IN_POS:
                    ds.transition(_orb_state.PHASE_IN_POS)
                ds.last_entry_iso = pos.get("entry_ts_utc") or ds.last_entry_iso
            # --- 2. RiskBook: insert a synthetic ticket for the open risk ---
            rb = engine._risk.get(pid)
            if rb is None:
                return
            # Synthetic ticket id is deterministic so a re-mirror is idempotent.
            ticket_id = f"recover-{pid}-{ticker}"
            with rb._lock:
                if ticket_id in rb._open_tickets:
                    return  # already mirrored
                shares_n = int(pos.get("qty", 0) or 0)
                entry_p = float(pos.get("entry_price", 0.0) or 0.0)
                stop_p = pos.get("stop")
                # risk_dollars: prefer the actual stop distance when
                # available; otherwise fall back to risk_per_trade_pct
                # of the current equity (1% by default) as a safe
                # approximation of what the position would have been
                # admitted with.
                risk_d = 0.0
                if stop_p is not None and entry_p > 0 and shares_n > 0:
                    try:
                        risk_d = abs(entry_p - float(stop_p)) * shares_n
                    except (TypeError, ValueError):
                        risk_d = 0.0
                if risk_d <= 0.0:
                    try:
                        risk_d = rb._equity * engine.cfg.risk_per_trade_pct / 100.0
                    except Exception:
                        risk_d = 500.0  # conservative absolute fallback
                notional = entry_p * shares_n if (entry_p > 0 and shares_n > 0) else 0.0
                rb._open_tickets[ticket_id] = _orb_risk_book._Ticket(
                    ticket_id=ticket_id,
                    risk_dollars=float(risk_d),
                    notional=float(notional),
                )
                rb._open_risk += float(risk_d)
                rb._open_notional += float(notional)
            logger.info(
                "[%s] [V836-RECOVER] mirrored %s into engine FSM + RiskBook "
                "(shares=%d entry=%.2f stop=%s risk_d=%.2f notional=%.2f)",
                self.NAME,
                ticker,
                shares_n,
                entry_p,
                ("%.2f" % float(stop_p)) if stop_p is not None else "None",
                risk_d,
                notional,
            )
        except Exception:
            # Mirror is best-effort; never let it break a boot path.
            logger.exception(
                "[%s] _mirror_position_into_engine failed for %s",
                self.NAME,
                ticker,
            )

    def _delete_persisted_position(self, ticker: str) -> None:
        """DELETE the row for ticker. Best-effort."""
        try:
            persistence.delete_executor_position(
                self.NAME,
                self.mode,
                ticker,
            )
        except Exception:
            logger.exception(
                "[%s] persistence.delete_executor_position failed for %s",
                self.NAME,
                ticker,
            )

    def _remove_position(self, ticker: str) -> None:
        """Remove ticker from both self.positions and state.db.

        Single hook for every position-close path. The dict pop is
        defensive (a stale-then-gone case is fine); the DB delete
        always runs so a stray row never lingers.

        v8.3.12 -- also unmirror the engine FSM + RiskBook ticket that
        v8.3.6 may have created on boot graft. Without this, the
        engine stays IN_POS for a (pid, ticker) whose position has
        actually closed, surfacing as the watchdog's
        `v10_in_pos_has_internal_position` phantom alert.
        """
        self.positions.pop(ticker, None)
        self._delete_persisted_position(ticker)
        self._unmirror_position_from_engine(ticker)

    def _unmirror_position_from_engine(self, ticker: str) -> None:
        """v8.3.12 -- symmetric inverse of v8.3.6's
        _mirror_position_into_engine. Called when a position closes so
        the engine FSM transitions out of IN_POS and the synthetic
        RiskBook ticket releases its risk + notional reservations.

        Without this, the engine remembers the position as open
        forever, surfacing as:
          - dashboard CONCURRENT RISK that never drops back to $0
            after the last close
          - watchdog `v10_in_pos_has_internal_position` (FSM IN_POS
            but no matching position in book)
          - opposite-side guard incorrectly continuing to block
            opposite-side entries after the original closed

        Idempotent: if the FSM is not IN_POS and the synthetic
        ticket isn't present, no-op.

        v9.1.26 -- ALSO drive `engine.on_exit` for the REAL engine
        ticket (uuid-style from `try_admit`) when present. Pre-v9.1.26
        the bus-exit path released only the synthetic `recover-*`
        ticket, so real `try_admit` tickets leaked. This silently
        capped Val/Gene admits because their FSM stayed IN_POS until
        the next phantom-sweep cycle. Today's audit on 2026-05-13
        showed Val admit_count=3 vs Main 17 on the same bar data.
        """
        try:
            import orb.live_runtime as _orb_runtime
            from orb import state as _orb_state

            engine = _orb_runtime.get_engine()
            if engine is None:
                return
            pid = self.NAME.lower()
            if pid not in engine.portfolio_ids:
                return
            # --- 1. v9.1.26: release the REAL engine ticket via on_exit ---
            # This must run BEFORE the FSM transition below; on_exit
            # owns the FSM transition + trades_today increment + real
            # ticket release. If a real ticket isn't tracked (only a
            # synthetic recover-* ticket exists, e.g. on a phantom-
            # sweep clear before any try_enter), fall through to the
            # legacy FSM path.
            try:
                from orb.exits import ExitDecision

                adapter = _orb_runtime._adapters.get(pid) if _orb_runtime._adapters else None
                if adapter is not None:
                    real_ticket_id = adapter._ticker_to_ticket.get(ticker)
                    if real_ticket_id and not real_ticket_id.startswith("recover-"):
                        pos = adapter._open_positions.get(real_ticket_id)
                        if pos is not None:
                            # Synthesize ExitDecision: price defaults
                            # to entry_price (zero P&L on engine side);
                            # the real exit P&L was already booked by
                            # Alpaca + executor.todays_trades. Engine
                            # accounting just needs the ticket released
                            # and FSM cleaned up.
                            decision = ExitDecision(
                                reason="bus_exit_mirror",
                                price=float(pos.entry_price),
                            )
                            engine.on_exit(pos, decision)
                            # Match adapter.check_exit cleanup at
                            # live_adapter.py:272-277.
                            adapter._open_positions.pop(real_ticket_id, None)
                            if adapter._ticker_to_ticket.get(ticker) == real_ticket_id:
                                del adapter._ticker_to_ticket[ticker]
                            logger.info(
                                "[V9126-ENGINE-EXIT] %s/%s real ticket "
                                "released via on_exit (bus mirror path)",
                                self.NAME,
                                ticker,
                            )
                            # engine.on_exit already transitioned FSM
                            # and bumped trades_today. Skip the legacy
                            # FSM block below.
                            return
            except Exception:
                logger.exception(
                    "[V9126-ENGINE-EXIT] %s real-ticket release raised "
                    "(falling through to legacy unmirror) ticker=%s",
                    self.NAME,
                    ticker,
                )
            # --- 2. Legacy FSM transition (no real ticket, e.g. phantom) ---
            ds = engine._state.get_day_state(pid, ticker)
            if ds.in_position:
                ds.in_position = False
                if ds.phase == _orb_state.PHASE_IN_POS:
                    ds.transition(_orb_state.PHASE_CLOSED)
            # --- 3. RiskBook: release the synthetic ticket if present ---
            rb = engine._risk.get(pid)
            if rb is None:
                return
            ticket_id = f"recover-{pid}-{ticker}"
            with rb._lock:
                ticket = rb._open_tickets.pop(ticket_id, None)
                if ticket is not None:
                    rb._open_risk -= float(ticket.risk_dollars)
                    rb._open_notional -= float(ticket.notional)
                    # Defensive clamp: rounding could push these
                    # slightly negative on edge cases.
                    if rb._open_risk < 0:
                        rb._open_risk = 0.0
                    if rb._open_notional < 0:
                        rb._open_notional = 0.0
            if ticket is not None:
                logger.info(
                    "[%s] [V8312-UNRECOVER] released synthetic ticket %s (risk=%.2f notional=%.2f)",
                    self.NAME,
                    ticker,
                    ticket.risk_dollars,
                    ticket.notional,
                )
        except Exception:
            # Best-effort cleanup; never raise into the close path.
            logger.exception(
                "[%s] _unmirror_position_from_engine failed for %s",
                self.NAME,
                ticker,
            )

    def _stamp_action(self, ticker: str) -> None:
        """v6.0.7 \u2014 record wall-clock of the last ENTRY/EXIT submit
        so the post-action reconcile knows when Alpaca's REST eventual-
        consistency window started. See RECONCILE_GRACE_SECONDS.
        """
        self._last_action_ts[ticker] = time.monotonic()

    @staticmethod
    def _is_ioc_request(req) -> bool:
        """v6.15.2 \u2014 detect whether a submitted order request was IOC.

        Only IOC orders are terminal at the moment ``submit_order``
        returns; their ack carries the final fill state. MARKET/DAY,
        GTC, and other TIFs return an ``accepted`` / ``pending_new``
        ack with ``filled_qty=0`` and the fill propagates a beat
        later. Treating those as 'unfilled' would silently drop live
        orders from local tracking while leaving them open on Alpaca.

        Best-effort: if ``time_in_force`` isn't readable, return
        False so the caller falls back to the legacy 'trust the
        request' qty path.
        """
        if req is None:
            return False
        tif = getattr(req, "time_in_force", None)
        if tif is None:
            return False
        # alpaca-py exposes TimeInForce.IOC as the enum, but tests
        # may pass a plain string. Match either via .value or repr.
        val = getattr(tif, "value", None) or str(tif)
        return str(val).lower().endswith("ioc")

    @staticmethod
    def _extract_filled_qty(order, requested_qty: int, *, req=None) -> int:
        """v6.15.1 / v6.15.2 \u2014 realized fill qty off an Alpaca order ack.

        Used after entry submits to keep the local position row honest
        about what actually filled. Pre-v6.15.1 the entry path booked
        the REQUESTED qty regardless of actual fill, so a 24-share
        request that filled 18 left a stop sized against 24 (Alpaca
        then returned 40410000 on the missing 6 shares).

        v6.15.2 \u2014 ``filled_qty=0`` is only treated as terminal when
        the order's ``time_in_force`` is IOC. For MARKET/DAY/GTC/etc.,
        Alpaca's synchronous submit ack returns the order in
        ``accepted`` / ``pending_new`` with ``filled_qty=0`` and the
        fill propagates a beat later \u2014 trusting that 0 would
        silently abort live orders that fill milliseconds afterwards.
        Pass ``req`` to enable TIF detection; without it the helper
        keeps v6.15.1 semantics (legacy callers untouched).

        Behaviour:
          - ``order is None`` or no ``filled_qty`` attr \u2192 fall
            back to ``requested_qty`` (legacy mock compat).
          - non-numeric ``filled_qty`` \u2192 fall back to requested.
          - ``filled_qty == 0`` AND ``req`` looks IOC \u2192 return 0
            (true zero-fill, abort the entry).
          - ``filled_qty == 0`` AND ``req`` is NOT IOC (MARKET/DAY) or
            ``req`` is missing \u2192 fall back to ``requested_qty``
            (the MARKET fill is still pending; the post-action
            reconcile will sync to broker truth).
          - ``0 < filled_qty <= requested_qty`` \u2192 return that.
          - ``filled_qty > requested_qty`` \u2192 clamp to requested.
          - ``filled_qty < 0`` \u2192 clamp to 0.
        """
        if order is None:
            return int(requested_qty)
        raw = getattr(order, "filled_qty", None)
        if raw is None:
            return int(requested_qty)
        try:
            filled = int(float(raw))
        except (TypeError, ValueError):
            return int(requested_qty)
        if filled < 0:
            return 0
        if filled > int(requested_qty):
            return int(requested_qty)
        if filled == 0 and not TradeGeniusBase._is_ioc_request(req):
            # MARKET/DAY ack returns 0 before the fill propagates.
            # Trust the request; the post-action reconcile heals.
            return int(requested_qty)
        return filled

    # ---------- v7.26.0: v10 ORB direct fire surface ----------
    #
    # Per-portfolio v10 admissions in engine/scan.py call these methods
    # directly to fire broker orders -- bypassing the trade_genius._emit_signal
    # bus that the legacy ENTRY_LONG / ENTRY_SHORT events ride. This is the
    # piece that makes Val and Gene fire on their OWN admissions (different
    # equity, different RiskBook), not just mirror Main's signals.
    #
    # Production-safe rollout: gated behind ORB_PORTFOLIO_FIRE env flag in
    # engine/scan.py. Default OFF (0); set to "1" after the 5-day paper-fire
    # observation window confirms the per-portfolio admissions look right.
    # Until then, Val/Gene continue to mirror Main via _on_signal as before.
    #
    # Idempotency: coid is built with direction="V10LONG"/"V10SHORT" so v10
    # fires get a separate coid bucket from legacy ENTRY_LONG ("LONG") fires
    # within the same UTC minute. A double-fire (v10 admission + main bus
    # broadcast) would land as two separate orders, each idempotent against
    # itself -- _submit_order_idempotent handles within-bucket dupes.
    def fire_long(self, ticker: str, price: float, shares: int, *, error_callback=None) -> bool:
        """Submit a LONG v10 entry directly to Alpaca.

        Returns True if an order was submitted (or recognized as a coid
        duplicate); False on no-op (no client, no shares, exception).

        v7.30.0: optional `error_callback(name, side, ticker, shares,
        exc)` is invoked when the broker submit raises so callers can
        escalate via callbacks.report_error (Telegram / dashboard
        alert). Without it, the failure is still logged but only
        visible in the log file.
        """
        if shares <= 0:
            return False
        if not ticker:
            return False
        return self._submit_v10_entry(
            side="LONG",
            ticker=ticker,
            price=price,
            shares=int(shares),
            error_callback=error_callback,
        )

    def fire_short(self, ticker: str, price: float, shares: int, *, error_callback=None) -> bool:
        """Submit a SHORT v10 entry directly to Alpaca. Mirror of fire_long.

        v7.30.0: optional `error_callback` -- see `fire_long` docstring."""
        if shares <= 0:
            return False
        if not ticker:
            return False
        return self._submit_v10_entry(
            side="SHORT",
            ticker=ticker,
            price=price,
            shares=int(shares),
            error_callback=error_callback,
        )

    def _submit_v10_entry(
        self, *, side: str, ticker: str, price: float, shares: int, error_callback=None
    ) -> bool:
        """Shared body for fire_long / fire_short.

        Uses MARKET DAY orders rather than the legacy LIMIT IOC path because
        v10 sizing is risk-based (the entry price is the broker's fill, not
        the strategy's anchor); LIMIT IOC could short-fill and break the
        risk-cap accounting on the RiskBook. MARKET DAY ensures the full
        v10-computed quantity fills (or rejects cleanly).
        """
        # Post-loss cooldown gate -- mirrors the check in broker/orders.py for
        # Main. Blocks re-entry on (ticker, side) within POST_LOSS_COOLDOWN_MIN
        # minutes of a losing stop on this executor's portfolio book.
        try:
            from engine.portfolio_book import PORTFOLIOS

            _pb_entry = PORTFOLIOS.get(self.NAME.lower())
            if _pb_entry is not None:
                _cd = _pb_entry.is_in_post_loss_cooldown(ticker, side.lower())
                if _cd is not None:
                    logger.info(
                        "[%s] [V10-FIRE] COOLDOWN BLOCK %s %s -- post-loss "
                        "cooldown active until %s (loss=$%.2f)",
                        self.NAME,
                        side,
                        ticker,
                        _cd.get("until_utc", "?"),
                        _cd.get("loss_pnl", 0),
                    )
                    return False
        except Exception:
            logger.debug("[%s] cooldown pre-check skipped", self.NAME, exc_info=True)

        client = self._ensure_client()
        if client is None:
            logger.warning(
                "[%s] [V10-FIRE] skip %s %s -- no alpaca client",
                self.NAME,
                side,
                ticker,
            )
            return False
        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
        except Exception:
            logger.exception("[%s] [V10-FIRE] alpaca imports failed", self.NAME)
            return False
        direction = f"V10{side}"  # V10LONG / V10SHORT (own coid bucket)
        coid = self._build_client_order_id(ticker, direction)
        order_side = OrderSide.BUY if side == "LONG" else OrderSide.SELL
        req = MarketOrderRequest(
            symbol=ticker,
            qty=int(shares),
            side=order_side,
            time_in_force=TimeInForce.DAY,
            client_order_id=coid,
        )
        try:
            order = self._submit_order_idempotent(client, req, coid)
        except Exception as _exc:
            logger.exception(
                "[%s] [V10-FIRE] submit failed %s %s qty=%d",
                self.NAME,
                side,
                ticker,
                shares,
            )
            # v7.30.0: escalate broker errors (5xx / timeout / conn
            # drop) through the supplied callback. Failure of the
            # callback itself must NOT raise out of the fire path.
            if error_callback is not None:
                try:
                    error_callback(self.NAME, side, ticker, int(shares), _exc)
                except Exception:
                    logger.exception(
                        "[%s] [V10-FIRE] error_callback raised",
                        self.NAME,
                    )
            return False
        oid = getattr(order, "id", "?")
        logger.info(
            "[%s] [V10-FIRE] submitted %s %s qty=%d price=%.4f coid=%s order_id=%s",
            self.NAME,
            side,
            ticker,
            shares,
            float(price or 0.0),
            coid,
            oid,
        )
        # Track on the executor's own positions map so /status + dashboard
        # surface the v10 fire. Same shape as the legacy _on_signal path.
        try:
            self._record_position(ticker, side, int(shares), float(price or 0.0))
        except Exception:
            logger.exception(
                "[%s] [V10-FIRE] _record_position raised (non-fatal)",
                self.NAME,
            )
        # v9.1.7 -- mirror exit-side pattern: notify per-executor
        # Telegram channel on entry. Prior to v9.1.7 only exits fired
        # _send_own_telegram, so the operator's tg.val / tg.gene
        # channels showed closes but never opens. Format mirrors
        # _close_position_idempotent's "OK" string so they read as a
        # matched pair on the operator's phone.
        try:
            notional = float(price or 0.0) * int(shares)
            entry_msg = (
                f"\u2705 {self.NAME}: {ticker} {side} OPEN {shares}sh "
                f"@ ${float(price or 0.0):.2f} "
                f"(${notional:,.0f} notional) order_id={oid}"
            )
            self._send_own_telegram(entry_msg)
        except Exception:
            logger.exception(
                "[%s] [V10-FIRE] entry telegram raised (non-fatal)",
                self.NAME,
            )
        return True

    def _record_position(self, ticker: str, side: str, qty: int, entry_price: float) -> None:
        """Stamp an executor-side record after a successful submit."""
        self.positions[ticker] = {
            "ticker": ticker,
            "side": side,
            "qty": int(qty),
            "entry_price": float(entry_price) if entry_price else 0.0,
            "entry_ts_utc": datetime.now(timezone.utc).isoformat(),
            "source": "SIGNAL",
            "stop": None,
            "trail": None,
        }
        # v5.5.10 \u2014 mirror to state.db so a restart sees this row.
        self._persist_position(ticker)
        # v6.0.7 \u2014 mark the eventual-consistency window for this ticker.
        self._stamp_action(ticker)

    def _close_position_idempotent(self, client, ticker: str, label: str, reason: str) -> None:
        """Close a position on Alpaca with the spec-mandated order type.

        v6.15.0 \u2014 honour ``broker.order_types.order_type_for_reason``:
          - LIMIT (sentinel A-A / A-B / A-D / HVP / DIVERGENCE) IOC at
            +/- 0.5%% of bid/ask per RULING #1.
          - STOP_LIMIT (sentinel_a_stop_price) at the tracked stop with
            a 30bps slip cap (compute_stop_limit_price).
          - STOP_MARKET (R-2 hard stop, velocity ratchet, V651 deep
            stop) at the tracked stop.
          - MARKET (EOD, daily-loss circuit breaker) immediately.
        Falls through to the legacy market-close path on any builder
        failure or missing prerequisite (no quote, no stop, etc.) so a
        transient data outage never silently skips a real exit.

        v5.24.0 \u2014 Alpaca returns ``{"code":40410000}`` whenever you
        ask it to close a position that no longer exists (already sold,
        or never opened on this account). With three executors plus
        the paper book, harmless races (e.g. an executor lagging the
        EOD flatten) used to surface as red \u274c ticks on Telegram.
        We treat 40410000 as success: drop the local + persisted row
        and log a quiet info-level line. Any OTHER error still
        propagates so real Alpaca outages still page the operator.
        """
        from broker.order_types import (
            ORDER_TYPE_LIMIT,
            ORDER_TYPE_STOP_LIMIT,
            ORDER_TYPE_STOP_MARKET,
            ORDER_TYPE_MARKET,
            order_type_for_reason,
            compute_sentinel_limit_price,
            compute_stop_limit_price,
        )

        order_type = order_type_for_reason(reason)
        pos = self.positions.get(ticker) or {}
        side = pos.get("side")
        qty = int(pos.get("qty") or 0)
        stop_px = pos.get("stop")

        try:
            from alpaca.trading.requests import (
                MarketOrderRequest,
                LimitOrderRequest,
                StopOrderRequest,
                StopLimitOrderRequest,
            )
            from alpaca.trading.enums import OrderSide, TimeInForce
        except Exception:
            logger.exception("[%s] alpaca imports failed on close, falling back", self.NAME)
            return self._legacy_close_position_idempotent(client, ticker, label, reason)

        if qty <= 0 or side not in ("LONG", "SHORT"):
            return self._legacy_close_position_idempotent(client, ticker, label, reason)

        exit_side = OrderSide.SELL if side == "LONG" else OrderSide.BUY
        coid = self._build_client_order_id(ticker, f"EXIT_{side}")

        req = None
        descr = "market"
        try:
            if order_type == ORDER_TYPE_LIMIT:
                tg_mod = _tg()
                bid = ask = None
                if tg_mod is not None and hasattr(tg_mod, "_v512_quote_snapshot"):
                    bid, ask = tg_mod._v512_quote_snapshot(ticker)
                if bid and ask and bid > 0 and ask > 0:
                    limit_px = compute_sentinel_limit_price(side, float(bid), float(ask))
                    req = LimitOrderRequest(
                        symbol=ticker,
                        qty=qty,
                        side=exit_side,
                        time_in_force=TimeInForce.IOC,
                        client_order_id=coid,
                        limit_price=round(float(limit_px), 2),
                    )
                    descr = f"limit @ {round(float(limit_px), 2)} IOC (bid={bid:.4f},ask={ask:.4f})"
            elif order_type == ORDER_TYPE_STOP_LIMIT:
                if stop_px and float(stop_px) > 0:
                    lim_px = compute_stop_limit_price(side, float(stop_px), 30)
                    req = StopLimitOrderRequest(
                        symbol=ticker,
                        qty=qty,
                        side=exit_side,
                        time_in_force=TimeInForce.DAY,
                        client_order_id=coid,
                        stop_price=round(float(stop_px), 2),
                        limit_price=round(float(lim_px), 2),
                    )
                    descr = (
                        f"stop_limit stop={round(float(stop_px), 2)} "
                        f"lim={round(float(lim_px), 2)} (30bps slip cap)"
                    )
            elif order_type == ORDER_TYPE_STOP_MARKET:
                if stop_px and float(stop_px) > 0:
                    req = StopOrderRequest(
                        symbol=ticker,
                        qty=qty,
                        side=exit_side,
                        time_in_force=TimeInForce.DAY,
                        client_order_id=coid,
                        stop_price=round(float(stop_px), 2),
                    )
                    descr = f"stop_market stop={round(float(stop_px), 2)}"
        except Exception:
            logger.exception("[%s] v6.15.0 exit-build raised, falling back to MARKET", self.NAME)
            req = None

        if req is None:
            # MARKET reasons (EOD, circuit breaker) and any builder
            # failure (missing quote / stop / unknown side). Build an
            # explicit MARKET order so we still get an order_id back
            # for forensics rather than a fire-and-forget close_position.
            req = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=exit_side,
                time_in_force=TimeInForce.DAY,
                client_order_id=coid,
            )
            descr = "market"

        try:
            order = self._submit_order_idempotent(client, req, coid)
        except Exception as exc:
            msg = str(exc)
            if "40410000" in msg or "position not found" in msg.lower():
                logger.info(
                    "[%s] CLOSE %s \u2014 already flat on broker (%s)",
                    self.NAME,
                    ticker,
                    reason,
                )
                self._remove_position(ticker)
                self._stamp_action(ticker)
                return
            raise

        self._remove_position(ticker)
        # v6.0.7 \u2014 mark the eventual-consistency window so the
        # immediate post-EXIT reconcile does not graft a phantom row.
        self._stamp_action(ticker)
        oid = getattr(order, "id", "?")
        ok = f"\u2705 {label}: {ticker} CLOSE {qty}sh @ {descr} ({reason}) order_id={oid}"
        logger.info(ok)
        self._send_own_telegram(ok)

    def _partial_close_position_idempotent(
        self, client, ticker: str, shares_to_close: int, label: str, reason: str
    ) -> None:
        """v8.1.1 -- half-close `shares_to_close` shares of an open
        position on Alpaca WITHOUT teardown.

        Companion to _close_position_idempotent. Differences:
          * Always submits a MARKET order (partial fires when 1R is
            touched, momentum is favorable -- no need for STOP_LIMIT
            geometry).
          * Does NOT call _remove_position -- mutates
            self.positions[ticker]["qty"] to the remainder so the
            executor's view stays in sync with Alpaca's.
          * Records a PARTIAL_FILL log line instead of CLOSE.

        Returns None. On any submit failure, the local position state
        is NOT mutated (caller may retry on the next tick) -- this
        prevents the executor's view from drifting from Alpaca on a
        partial that didn't actually submit.

        40410000 (position already flat on Alpaca): treated as success
        path but with a warning -- the local position dict is still
        decremented so subsequent ticks see the remainder. Operator
        should inspect Alpaca for drift if 40410000 fires repeatedly.
        """
        if shares_to_close is None or int(shares_to_close) <= 0:
            return
        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
        except Exception:
            logger.exception(
                "[%s] alpaca imports failed on partial-close, skipping",
                self.NAME,
            )
            return

        pos = self.positions.get(ticker) or {}
        side = pos.get("side")
        cur_qty = int(pos.get("qty") or 0)
        closing = int(shares_to_close)
        if side not in ("LONG", "SHORT"):
            logger.warning(
                "[%s] [V81-ALPACA-PARTIAL] %s skipped -- unknown side=%s",
                self.NAME,
                ticker,
                side,
            )
            return
        if cur_qty <= 0:
            logger.info(
                "[%s] [V81-ALPACA-PARTIAL] %s skipped -- no position tracked",
                self.NAME,
                ticker,
            )
            return
        if closing >= cur_qty:
            # A "partial" that would close the full position is a
            # caller bug. Refuse so we never lose half the position
            # silently; caller should use EXIT_* for full close.
            logger.warning(
                "[%s] [V81-ALPACA-PARTIAL] %s REFUSED partial=%d "
                ">= current qty=%d (use EXIT_* for full close)",
                self.NAME,
                ticker,
                closing,
                cur_qty,
            )
            return

        exit_side = OrderSide.SELL if side == "LONG" else OrderSide.BUY
        coid = self._build_client_order_id(ticker, f"PARTIAL_{side}")
        req = MarketOrderRequest(
            symbol=ticker,
            qty=closing,
            side=exit_side,
            time_in_force=TimeInForce.DAY,
            client_order_id=coid,
        )
        try:
            order = self._submit_order_idempotent(client, req, coid)
        except Exception as exc:
            msg = str(exc)
            if "40410000" in msg or "position not found" in msg.lower():
                logger.warning(
                    "[%s] [V81-ALPACA-PARTIAL] %s already flat on broker "
                    "(%s) -- decrementing local qty %d -> %d",
                    self.NAME,
                    ticker,
                    reason,
                    cur_qty,
                    cur_qty - closing,
                )
                pos["qty"] = cur_qty - closing
                self._stamp_action(ticker)
                return
            # Non-position-flat error: leave local qty untouched so
            # the next tick can retry, and surface the error.
            logger.exception(
                "[%s] [V81-ALPACA-PARTIAL] %s FAILED submit -- local "
                "qty unchanged at %d so caller can retry",
                self.NAME,
                ticker,
                cur_qty,
            )
            return

        # Success: decrement local qty to the runner remainder.
        pos["qty"] = cur_qty - closing
        self._stamp_action(ticker)
        # Persist the mutated position row so a Railway redeploy mid-
        # session doesn't lose the partial state.
        try:
            self._persist_position(ticker)
        except Exception:
            logger.debug(
                "[%s] [V81-ALPACA-PARTIAL] %s persist skipped",
                self.NAME,
                ticker,
                exc_info=True,
            )
        oid = getattr(order, "id", "?")
        ok = (
            f"\U0001f504 {label}: {ticker} PARTIAL {closing}sh "
            f"@ market ({reason}) remaining={pos['qty']} order_id={oid}"
        )
        logger.info(ok)
        self._send_own_telegram(ok)

        # v8.1.7 -- record this partial in the v10 activity ring
        # buffer so the dashboard's v10 Activity Feed shows
        # cross-portfolio partials (Main was already covered via
        # orb.live_runtime.check_exit in v8.1.2; this adds Val/Gene
        # which dispatch through executors, NOT through check_exit).
        # The detail string mirrors the Main side's format so the
        # dashboard renders the two consistently.
        try:
            from orb.live_runtime import _record_activity

            _record_activity(
                kind="partial",
                ticker=ticker,
                pid=self.NAME.lower(),
                detail=f"{closing} sh @ market ({reason})",
            )
        except Exception:
            # Activity recording is best-effort -- never block the
            # broker path on a downstream import / lock issue.
            pass

    def _legacy_close_position_idempotent(
        self, client, ticker: str, label: str, reason: str
    ) -> None:
        """Pre-v6.15.0 fallback: blind ``client.close_position`` (MARKET).

        Used only when the v6.15.0 path cannot build a typed request \u2014
        e.g. position row missing side/qty, or alpaca request classes
        unimportable. Preserved verbatim from v6.14.10 so the rare
        fallback still flattens cleanly.
        """
        try:
            client.close_position(ticker)
        except Exception as exc:
            msg = str(exc)
            if "40410000" in msg or "position not found" in msg.lower():
                logger.info(
                    "[%s] CLOSE %s \u2014 already flat on broker (%s)",
                    self.NAME,
                    ticker,
                    reason,
                )
                self._remove_position(ticker)
                self._stamp_action(ticker)
                return
            raise
        self._remove_position(ticker)
        self._stamp_action(ticker)
        ok = f"\u2705 {label}: {ticker} CLOSE ({reason}) [legacy]"
        logger.info(ok)
        self._send_own_telegram(ok)

    def _within_action_grace(self, ticker: str) -> bool:
        """v6.0.7 \u2014 True iff a recent ENTRY/EXIT submit for ``ticker``
        is still inside Alpaca's REST eventual-consistency window.
        """
        ts = self._last_action_ts.get(ticker)
        if ts is None:
            return False
        return (time.monotonic() - ts) < RECONCILE_GRACE_SECONDS

    def _get_open_position_settled(self, client, ticker: str, expect: str):
        """v6.0.7 \u2014 poll get_open_position until the answer matches
        ``expect`` or the grace window expires.

        Returns ``(bp, status)`` where:
          - ``bp`` is the Alpaca position object (or None if broker is flat),
          - ``status`` is one of ``"present"``, ``"flat"``, or ``"error"``.

        ``expect`` is one of:
          - ``"present"``: post-ENTRY caller \u2014 retry while broker says
            flat (40410000) inside the grace window. After grace, accept
            \"flat\" as final.
          - ``"flat"``: post-EXIT caller \u2014 retry while broker still has
            the position inside the grace window. After grace, accept
            \"present\" as final.
          - ``"any"``: caller does not have a posted action (e.g., periodic
            sweep) \u2014 single shot, no retry.

        Other API errors short-circuit to ``status="error"`` so the caller
        can leave local state untouched (transient outage must not corrupt
        truth).
        """
        deadline = time.monotonic() + RECONCILE_GRACE_SECONDS
        last_err = None
        while True:
            try:
                bp = client.get_open_position(ticker)
                if expect == "flat" and time.monotonic() < deadline:
                    # Broker still has it; this is the post-EXIT eventual-
                    # consistency window. Wait and retry.
                    time.sleep(RECONCILE_RETRY_SLEEP)
                    continue
                return bp, "present"
            except Exception as exc:
                msg = str(exc)
                is_flat = "40410000" in msg or "position not found" in msg.lower()
                if not is_flat:
                    last_err = exc
                    return None, "error"
                if expect == "present" and time.monotonic() < deadline:
                    # Broker says flat; this is the post-ENTRY eventual-
                    # consistency window. Wait and retry.
                    time.sleep(RECONCILE_RETRY_SLEEP)
                    continue
                return None, "flat"
        # Unreachable but kept for static analysers.
        if last_err is not None:
            logger.warning("[%s] settled poll terminal err %s", self.NAME, last_err)
        return None, "error"

    def _reconcile_position_with_broker(self, ticker: str, expect: str = "any") -> None:
        """v5.25.0 / v6.0.7 \u2014 single-ticker post-action reconcile.

        Called immediately after every successful ENTRY/EXIT submit
        so the executor's local view of ``self.positions[ticker]`` is
        re-synced from the broker's authoritative book. Three outcomes:

          1. Broker has the position: overwrite local qty / entry_price
             with the broker's values (covers partial fills, prior
             stacking, executor / paper-book qty drift).
          2. Broker reports 40410000 \"position not found\": drop the
             local row \u2014 the position is flat on the broker, so
             we must reflect that even if a stale row lingered.
          3. Any other API failure: WARN log, leave state untouched.
             A transient outage must not corrupt local truth; the
             next signal or boot reconcile will heal it.

        v6.0.7 \u2014 the ``expect`` parameter says what outcome the
        caller expects so the helper can ride out Alpaca's REST
        eventual-consistency window:

          - ``"present"`` (post-ENTRY): retry on 40410000 inside grace.
            If the broker is still flat after grace, leave local state
            untouched (do not delete the row the ENTRY just created);
            the next periodic reconcile will catch a real divergence.
          - ``"flat"`` (post-EXIT): retry while broker still has the
            position inside grace. If broker still has it after grace,
            the EXIT really did fail \u2014 leave the row alone and let
            the next signal heal it (do not graft a phantom row from a
            position the bot just tried to close).
          - ``"any"`` (default, periodic sweep / pre-v6.0.7 callers):
            single-shot legacy behaviour preserved.

        Unlike ``_reconcile_broker_positions`` (the boot-time full
        sweep using ``get_all_positions``), this calls
        ``client.get_open_position(ticker)`` for a single symbol so
        the post-action path stays cheap. No Telegram fan-out \u2014
        the calling ENTRY/EXIT path already sent its own confirmation.
        """
        client = self._ensure_client()
        if client is None:
            logger.warning(
                "[%s] [POST-RECONCILE] no alpaca client \u2014 skipping %s",
                self.NAME,
                ticker,
            )
            return

        if expect == "any":
            # Legacy single-shot path (periodic sweep). Behaviour preserved.
            try:
                bp = client.get_open_position(ticker)
                status = "present"
            except Exception as exc:
                msg = str(exc)
                if "40410000" in msg or "position not found" in msg.lower():
                    bp, status = None, "flat"
                else:
                    logger.warning(
                        "[%s] [POST-RECONCILE] get_open_position(%s) failed: %s "
                        "\u2014 leaving local state untouched",
                        self.NAME,
                        ticker,
                        exc,
                    )
                    return
        else:
            bp, status = self._get_open_position_settled(client, ticker, expect)
            if status == "error":
                logger.warning(
                    "[%s] [POST-RECONCILE] get_open_position(%s) errored "
                    "\u2014 leaving local state untouched (expect=%s)",
                    self.NAME,
                    ticker,
                    expect,
                )
                return

        if status == "flat":
            if expect == "present":
                # Post-ENTRY: broker still flat after grace. Could be a
                # genuine reject the submit-path missed, but more often it
                # is a slow-fill paper account. DO NOT delete the local
                # row \u2014 next periodic reconcile will heal a real flat.
                logger.warning(
                    "[%s] [POST-RECONCILE] %s broker flat after %.1fs grace post-ENTRY "
                    "\u2014 leaving local row in place (next periodic sweep will heal)",
                    self.NAME,
                    ticker,
                    RECONCILE_GRACE_SECONDS,
                )
                return
            # expect in ("flat", "any"): legacy behaviour \u2014 broker says
            # flat, drop our row if it lingers.
            if ticker in self.positions:
                logger.info(
                    "[%s] [POST-RECONCILE] %s flat on broker, removing local row",
                    self.NAME,
                    ticker,
                )
                self._remove_position(ticker)
            else:
                logger.debug(
                    "[%s] [POST-RECONCILE] %s flat on broker, already untracked",
                    self.NAME,
                    ticker,
                )
            return

        # status == "present": broker has the position \u2014 sync qty + entry_price.
        try:
            qty_int = int(bp.qty)
        except Exception:
            logger.exception(
                "[%s] [POST-RECONCILE] bad qty on %s, skipping sync",
                self.NAME,
                ticker,
            )
            return
        side = "LONG" if qty_int > 0 else "SHORT"
        try:
            entry_px = float(bp.avg_entry_price)
        except Exception:
            entry_px = 0.0
        existing = self.positions.get(ticker)
        if existing is None:
            if expect == "flat":
                # Post-EXIT: broker still has the position after grace. The
                # close did not take. DO NOT graft a phantom row \u2014 next
                # signal or periodic reconcile will heal a real divergence.
                logger.warning(
                    "[%s] [POST-RECONCILE] %s broker still has position after "
                    "%.1fs grace post-EXIT \u2014 leaving untracked (next sweep heals)",
                    self.NAME,
                    ticker,
                    RECONCILE_GRACE_SECONDS,
                )
                return
            # Broker has it but we don't \u2014 graft the row.
            self.positions[ticker] = {
                "ticker": ticker,
                "side": side,
                "qty": abs(qty_int),
                "entry_price": entry_px,
                "entry_ts_utc": datetime.now(timezone.utc).isoformat(),
                "source": "POST_RECONCILE",
                "stop": None,
                "trail": None,
            }
            logger.warning(
                "[%s] [POST-RECONCILE] grafted untracked broker row %s side=%s qty=%d entry=%.2f",
                self.NAME,
                ticker,
                side,
                abs(qty_int),
                entry_px,
            )
        else:
            existing["qty"] = abs(qty_int)
            existing["side"] = side
            if entry_px:
                existing["entry_price"] = entry_px
            logger.info(
                "[%s] [POST-RECONCILE] %s synced from broker: side=%s qty=%d entry=%.2f",
                self.NAME,
                ticker,
                side,
                abs(qty_int),
                entry_px,
            )
        self._persist_position(ticker)

    def _probe_aon_support(self) -> str:
        """v7.0.0 \u2014 boot-time probe to detect native Alpaca AON support.

        Attempts to construct a LimitOrderRequest with all_or_none=True
        AND verify the flag survives model_dump() round-trip. On alpaca-py
        0.43.2, pydantic silently drops unknown kwargs at construction
        (model_fields has no all_or_none, model_config has no extra=allow),
        so a TypeError-only sentry returns a false positive that disables
        the software fallback. The round-trip check engages software AON
        until alpaca-py adds the field natively.

        Returns "native" only if the constructed request serializes the
        all_or_none flag; "software" otherwise (TypeError, silent drop,
        or any exception).
        """
        try:
            from alpaca.trading.requests import LimitOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce

            req = LimitOrderRequest(
                symbol="SPY",
                qty=1,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.IOC,
                limit_price=1.00,
                all_or_none=True,
            )
            dumped = req.model_dump()
            if not dumped.get("all_or_none"):
                return "software"
            return "native"
        except TypeError:
            return "software"
        except Exception:
            return "software"

    def _emit_zerofill_reconcile_followup(
        self,
        *,
        label: str,
        ticker: str,
        side: str,
        requested_qty: int,
        order_id: str,
        reconcile_raised: bool = False,
    ) -> None:
        """v7.0.0 (was v6.15.6) \u2014 single-message Telegram after a
        V6152-ZEROFILL reconcile. Collapses the prior two-ping pattern
        (initial \u26a0\ufe0f \"reconciling\" + follow-up outcome) into ONE
        final message per entry.

        Reads ``self.positions[ticker]`` immediately after
        ``_reconcile_position_with_broker`` returns and emits one of
        four outcomes:

          * RECONCILE-RAISED \u2014 reconcile raised an exception;
            cannot determine broker state.
          * GRAFTED \u2014 reconcile found a late fill on the broker
            book (synchronous IOC ack returned 0 but the fill
            propagated milliseconds later). \u2705 with (late fill) suffix.
          * SYNCED \u2014 a local row already existed and reconcile
            updated qty / entry from broker truth. Rare on the
            ZEROFILL path.
          * FLAT \u2014 reconcile confirmed the broker is flat. True
            zero-fill case (limit really did not cross).
        """
        try:
            existing = self.positions.get(ticker)
            if reconcile_raised:
                msg = (
                    f"\u26a0\ufe0f {label}: {ticker} {side} reconcile inconclusive "
                    f"\u2014 verify on broker (order_id={order_id})"
                )
            elif existing:
                qty = int(existing.get("qty") or 0)
                entry_px = float(existing.get("entry_price") or 0.0)
                src = str(existing.get("source") or "")
                if src == "POST_RECONCILE" and qty > 0:
                    msg = (
                        f"\u2705 {label}: {ticker} {side} {qty} @ ${entry_px:.2f} "
                        f"(late fill, order_id={order_id})"
                    )
                else:
                    # Synced pre-existing row (rare on ZEROFILL path)
                    msg = (
                        f"\u2705 {label}: {ticker} {side} synced "
                        f"(qty={qty} @ ${entry_px:.2f}, order_id={order_id})"
                    )
            else:
                msg = (
                    f"\u26a0\ufe0f {label}: {ticker} {side} rejected "
                    f"\u2014 limit did not cross "
                    f"(no broker fill, order_id={order_id})"
                )
            logger.warning("[%s] [V700-ZEROFILL-FOLLOWUP] %s", self.NAME, msg)
            self._send_own_telegram(msg)
        except Exception:
            # Follow-up is informational \u2014 must never raise into
            # the entry path. Swallow and rely on the existing
            # WARNING-level reconcile log lines for forensics.
            logger.exception(
                "[%s] [V700-ZEROFILL-FOLLOWUP] failed for %s %s",
                self.NAME,
                ticker,
                side,
            )

    def _reconcile_broker_positions(self) -> None:
        """Run once at boot. Pull broker positions, graft any orphans."

        v5.5.10 reframe: this runs AFTER _load_persisted_positions has
        rehydrated self.positions from state.db, so it becomes a true
        safety net rather than the primary state-bootstrap path. Three
        outcomes:

          1. Persisted set == broker set: clean reconcile, INFO log,
             no Telegram (the common reboot case).
          2. Broker has tickers persisted does not: true divergence \u2014
             graft + WARN log + Telegram with "(true divergence)".
          3. Persisted has tickers broker does not: stale local state.
             Quietly self-heal by removing the row. WARN log only,
             no Telegram, no close/exit path called.

        For grafted orphans we keep source='RECONCILE' and persist
        the new row so the next reboot stays silent.
        """
        client = self._ensure_client()
        if client is None:
            logger.warning(
                "[%s] [RECONCILE] no alpaca client \u2014 skipping",
                self.NAME,
            )
            return
        try:
            broker_positions = client.get_all_positions()
        except Exception as e:
            logger.error(
                "[%s] [RECONCILE] get_all_positions failed: %s",
                self.NAME,
                e,
            )
            return

        broker_by_ticker: dict = {}
        for bp in broker_positions or []:
            ticker = getattr(bp, "symbol", None)
            if not ticker:
                continue
            broker_by_ticker[ticker] = bp

        broker_tickers = set(broker_by_ticker.keys())
        persisted_tickers = set(self.positions.keys())

        # Outcome 3: stale local state \u2014 quiet self-heal.
        for ticker in sorted(persisted_tickers - broker_tickers):
            logger.warning(
                "[%s] [RECONCILE] stale local position: ticker=%s \u2014 "
                "broker says no position, removing",
                self.NAME,
                ticker,
            )
            self._remove_position(ticker)

        # Outcome 2: graft broker orphans (true divergence).
        grafted = 0
        for ticker in sorted(broker_tickers - persisted_tickers):
            bp = broker_by_ticker[ticker]
            try:
                qty_int = int(bp.qty)
            except Exception:
                logger.exception(
                    "[%s] [RECONCILE] bad qty on %s, skipping",
                    self.NAME,
                    ticker,
                )
                continue
            side = "LONG" if qty_int > 0 else "SHORT"
            try:
                entry_px = float(bp.avg_entry_price)
            except Exception:
                entry_px = 0.0
            self.positions[ticker] = {
                "ticker": ticker,
                "side": side,
                "qty": abs(qty_int),
                "entry_price": entry_px,
                "entry_ts_utc": datetime.now(timezone.utc).isoformat(),
                "source": "RECONCILE",
                "stop": None,
                "trail": None,
            }
            self._persist_position(ticker)
            # v8.2.0 -- mirror grafted row into PortfolioBook so the
            # dashboard's /api/state.portfolios.<pid>.positions feed
            # sees the recovered position. Closes the recurring
            # inv_position_count_three_way "phantom at broker" alert
            # that fires whenever broker has rows the book doesn't.
            self._mirror_position_into_book(ticker)
            # v8.3.6 -- ALSO mirror into the OrbEngine FSM + RiskBook so
            # the engine knows it's in_position on this ticker
            # (opposite-side guard) AND the RiskBook tracks the open
            # risk + notional (CONCURRENT RISK panel + notional-cap
            # math).
            self._mirror_position_into_engine(ticker)
            grafted += 1
            logger.warning(
                "[%s] [RECONCILE] grafted broker orphan: ticker=%s side=%s qty=%d entry=%.2f",
                self.NAME,
                ticker,
                side,
                abs(qty_int),
                entry_px,
            )

        # Outcome 1: clean reconcile \u2014 silent INFO log, no Telegram.
        if grafted == 0:
            logger.info(
                "[%s] [RECONCILE] clean: %d position(s) match broker",
                self.NAME,
                len(broker_tickers),
            )
            return

        try:
            self._send_own_telegram(
                f"\u26a0\ufe0f Reconcile: grafted {grafted} broker orphan(s) "
                f"on {self.NAME} boot (true divergence)"
            )
        except Exception:
            logger.exception(
                "[%s] [RECONCILE] telegram fan-out raised",
                self.NAME,
            )

    def _on_signal(self, event: dict) -> None:
        """Listener callback: dispatch on event['kind'].

        v7.83.0 -- emits [V79-MIRROR-*] forensic logs at every branch
        so the dashboard monitor can audit why the legacy signal-bus
        mirror produces val_gene_trades_match_main violations. Pre-
        v7.83.0 silent early-returns (qty<=0, exception in builder)
        gave no log trail post-receipt.

        v8.3.23 -- INDEPENDENT MODE entry guard. When
        ORB_PORTFOLIO_FIRE=1 (default since v8.3.23), entries are
        dispatched per-portfolio by engine/scan.py:_v10_dispatch_executor_fire
        -> executor.fire_long / fire_short. This listener still
        handles EXIT_LONG / EXIT_SHORT / PARTIAL_EXIT_* (the
        production code does not yet wire live_runtime.check_exit
        to a per-portfolio exit loop, so bus exits are still the
        canonical exit path). Skipping ONLY entries here prevents
        a double-fire on independent mode.
        """
        kind = event.get("kind", "")
        ticker = event.get("ticker", "")
        price = event.get("price", 0.0) or 0.0
        reason = event.get("reason", "")
        label = f"{self.NAME} {self.mode}"
        main_shares = event.get("main_shares")
        # [V79-MIRROR-RECV] -- proof that _on_signal fired at all.
        logger.info(
            "[V79-MIRROR-RECV] %s kind=%s ticker=%s price=%s main_shares=%s",
            self.NAME,
            kind,
            ticker,
            price,
            main_shares,
        )

        # v8.3.23 -- skip ENTRY signals in independent mode. Exits
        # still flow through here.
        if kind in ("ENTRY_LONG", "ENTRY_SHORT"):
            if os.environ.get("ORB_PORTFOLIO_FIRE", "1") == "1":
                logger.info(
                    "[V8323-INDEPENDENT-SKIP] %s %s %s -- independent "
                    "mode active; entry will fire via "
                    "_v10_dispatch_executor_fire (not legacy bus)",
                    self.NAME,
                    kind,
                    ticker,
                )
                return

        # v4.0.0-beta — remember the most recent event for the dashboard
        # (last_signal line on the per-executor tab). Captured before any
        # dispatch so we still record what was seen even if Alpaca errors.
        # v4.1.2: the old try/except here was unreachable: `price` is
        # normalised to 0.0-or-numeric at line 534 so `float(price)` can't
        # raise, and dict-literal assignment has no failure mode. Dropped.
        self.last_signal = {
            "kind": kind,
            "ticker": ticker,
            "price": float(price) if price else 0.0,
            "reason": reason,
            "timestamp_utc": event.get("timestamp_utc", _tg()._utc_now_iso()),
        }

        client = self._ensure_client()
        if client is None:
            # v7.83.0 -- [V79-MIRROR-SKIP] makes drop-path auditable.
            logger.warning(
                "[V79-MIRROR-SKIP] %s %s %s \u2014 no alpaca client",
                self.NAME,
                kind,
                ticker,
            )
            return

        # v6.15.0 \u2014 best-effort open-P/L snapshot to /data/open_pnl.jsonl
        # so the dashboard can show closed + open as a single number that
        # matches Alpaca's portfolio_value. Throttled to one call per
        # ~30 s; failures are silent (the dashboard tolerates a stale
        # or missing file).
        try:
            now_mono = time.monotonic()
            if (now_mono - self._last_open_pnl_ts) >= 30.0:
                self._last_open_pnl_ts = now_mono
                from broker.open_pnl import snapshot_open_pnl
                from bot_version import BOT_VERSION as _bv

                snapshot_open_pnl(client, _bv)
        except Exception:
            logger.debug("[%s] open_pnl snapshot raised (non-fatal)", self.NAME, exc_info=True)

        try:
            from alpaca.trading.requests import (
                MarketOrderRequest,
                LimitOrderRequest,
                ClosePositionRequest,
            )
            from alpaca.trading.enums import OrderSide, TimeInForce
        except Exception:
            logger.exception("[%s] alpaca imports failed", self.NAME)
            return

        # v5.15.1 vAA-1 \u2014 helper to build the Strike-entry order request.
        # Tries to fetch a live (bid, ask) quote and emit a LIMIT order at
        # ``compute_strike_limit_price(side, ask, bid)`` per spec rules
        # ORDER-LIMIT-PRICE-LONG / -SHORT. Falls back to MARKET on any
        # quote-fetch failure so a transient data outage never silently
        # skips a Strike fire \u2014 the entry signal must still fill.
        # v6.15.2 \u2014 when the real quote is null we synthesize a 5bps
        # spread anchored on the signal's last-trade price so we still
        # emit a marketable IOC LIMIT instead of an unbounded MARKET. The
        # AAPL incident showed the silent MARKET fallback combined with
        # v6.15.1's IOC-zero-fill abort can drop a live order from local
        # tracking; keeping the LIMIT path alive avoids both halves.
        def _build_entry_request(side_label: str, qty: int, coid: str, *, price: float = 0.0):
            order_side = OrderSide.BUY if side_label == "LONG" else OrderSide.SELL
            try:
                from broker.orders import compute_strike_limit_price

                tg_mod = _tg()
                bid = ask = None
                if tg_mod is not None and hasattr(tg_mod, "_v512_quote_snapshot"):
                    bid, ask = tg_mod._v512_quote_snapshot(ticker)
                # v6.15.2 \u2014 if the real quote is null/non-positive,
                # try a synthetic spread off the signal price BEFORE the
                # MARKET fallback, so we still ship an IOC LIMIT.
                if (bid is None or ask is None or bid <= 0 or ask <= 0) and price and price > 0:
                    if tg_mod is not None and hasattr(tg_mod, "_v512_synthetic_quote"):
                        try:
                            sb, sa = tg_mod._v512_synthetic_quote(ticker, float(price))
                            if sb and sa and sb > 0 and sa > 0:
                                logger.warning(
                                    "[%s] [V6152-QUOTE] %s synthetic quote anchor=%.4f bid=%.4f ask=%.4f",
                                    self.NAME,
                                    ticker,
                                    float(price),
                                    float(sb),
                                    float(sa),
                                )
                                bid, ask = sb, sa
                        except Exception as _qe:
                            logger.warning(
                                "[%s] [V6152-QUOTE] %s synthetic quote raised: %s",
                                self.NAME,
                                ticker,
                                _qe,
                            )
                if bid is not None and ask is not None and bid > 0 and ask > 0:
                    limit_px = compute_strike_limit_price(
                        side=side_label, ask=float(ask), bid=float(bid)
                    )
                    # v7.0.0 Phase 5 \u2014 native AON: pass all_or_none=True
                    # when the SDK probe at boot confirmed it accepts the kwarg.
                    # Software mode: construct without it (existing behavior).
                    _limit_kwargs: dict = dict(
                        symbol=ticker,
                        qty=qty,
                        side=order_side,
                        # v6.15.0 \u2014 marketable LIMITs use IOC so a
                        # stale or partially-filled order never sits
                        # on the book and fragments on liquidity hits.
                        time_in_force=TimeInForce.IOC,
                        client_order_id=coid,
                        limit_price=round(float(limit_px), 2),
                    )
                    if self._aon_mode == "native":
                        _limit_kwargs["all_or_none"] = True
                    return (
                        LimitOrderRequest(**_limit_kwargs),
                        f"limit @ {round(float(limit_px), 2)} IOC (bid={bid:.4f},ask={ask:.4f})",
                    )
                logger.warning(
                    "[%s] %s %s no bid/ask available, falling back to MARKET",
                    self.NAME,
                    kind,
                    ticker,
                )
            except Exception as _e:
                logger.warning(
                    "[%s] %s %s LIMIT build failed (%s), falling back to MARKET",
                    self.NAME,
                    kind,
                    ticker,
                    _e,
                )
            return (
                MarketOrderRequest(
                    symbol=ticker,
                    qty=qty,
                    side=order_side,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=coid,
                ),
                "market",
            )

        # v5.24.0 \u2014 honour the paper book's qty when present so each
        # executor mirrors the same share count the paper book booked.
        # Before: executors recomputed via ``_shares_for`` (uses the
        # executor's own dollars_per_entry, defaulting to $10k), while
        # the paper book sized Entry-1 at $10k * ENTRY_1_SIZE_PCT (0.5)
        # = $5k. Result was Val/Gene buying 2x the paper book's qty for
        # Entry-1 fires, then 4x on the rebalanced Entry-2. Honouring
        # ``main_shares`` keeps every executor's per-ticker quantity
        # aligned with the paper book and with each other.
        signal_qty = int(event.get("main_shares") or 0)

        # v8.3.17 -- proportional scaling. Val/Gene Alpaca accounts may
        # be smaller than Main's $100K paper book. Mirroring
        # main_shares 1:1 then puts AMZN ($75K notional) through Val's
        # ~$70K notional cap and gets risk_reject:notional_cap. Scale
        # the qty by ex_equity / main_equity so the executor stays
        # within its risk budget while still mirroring direction +
        # ticker. No-op when ex_equity >= main_equity.
        if signal_qty > 0:
            try:
                scaled_qty, scale_ratio = self._scaled_signal_qty(signal_qty)
            except Exception:
                # Best-effort -- never break the mirror path on a
                # scaling math error. Fall through to legacy 1:1.
                scaled_qty, scale_ratio = signal_qty, 1.0
            if scale_ratio < 1.0:
                logger.info(
                    "[V8317-SCALE] %s %s qty=%d -> %d (eq_ratio=%.3f)",
                    self.NAME,
                    ticker,
                    signal_qty,
                    scaled_qty,
                    scale_ratio,
                )
            signal_qty = scaled_qty

        try:
            if kind == "ENTRY_LONG":
                qty = signal_qty if signal_qty > 0 else self._shares_for(price, ticker=ticker)
                if qty <= 0:
                    # v7.83.0 -- forensic log so silent qty=0 drops are
                    # auditable. main_shares is the sender-supplied size;
                    # _shares_for is the local fallback.
                    logger.warning(
                        "[V79-MIRROR-SKIP] %s ENTRY_LONG %s qty=0 "
                        "(main_shares=%s, local_fallback_shares_for=%d)",
                        self.NAME,
                        ticker,
                        signal_qty,
                        self._shares_for(price, ticker=ticker),
                    )
                    return
                logger.info(
                    "[V79-MIRROR-DISPATCH] %s ENTRY_LONG %s qty=%d price=%s",
                    self.NAME,
                    ticker,
                    qty,
                    price,
                )
                coid = self._build_client_order_id(ticker, "LONG")
                req, order_descr = _build_entry_request("LONG", qty, coid, price=price)
                order = self._submit_order_idempotent(client, req, coid)
                oid = getattr(order, "id", "?")
                # v6.15.1 \u2014 IOC LIMIT acks carry filled_qty terminal.
                # v6.15.2 \u2014 pass req so MARKET/DAY filled_qty=0 (still
                # pending broker-side) is NOT mistaken for a true zero
                # fill; only IOC zeros are terminal.
                filled_qty = self._extract_filled_qty(order, qty, req=req)
                if filled_qty == 0:
                    # v7.0.0 Phase 5 \u2014 quiet ZEROFILL: suppress the
                    # initial \u26a0\ufe0f \"unfilled, reconciling...\" ping.
                    # Forensic WARNING log unchanged; only Telegram surface
                    # collapses to a single final message after reconcile.
                    msg = (
                        f"\u26a0\ufe0f {label}: {ticker} LONG IOC unfilled "
                        f"(requested={qty}, filled=0, order_id={oid}) "
                        f"\u2014 reconciling against broker"
                    )
                    logger.warning("[%s] [V6152-ZEROFILL] %s", self.NAME, msg)
                    # NOTE: first telegram suppressed per v7.0.0 spec D.
                    _reconcile_raised = False
                    try:
                        self._reconcile_position_with_broker(ticker, expect="present")
                    except Exception:
                        _reconcile_raised = True
                        logger.exception(
                            "[%s] [V6152-ZEROFILL] reconcile raised on %s LONG",
                            self.NAME,
                            ticker,
                        )
                    self._emit_zerofill_reconcile_followup(
                        label=label,
                        ticker=ticker,
                        side="LONG",
                        requested_qty=qty,
                        order_id=oid,
                        reconcile_raised=_reconcile_raised,
                    )
                    return
                _aon_partial_notified = False
                if self._aon_mode == "software" and 0 < filled_qty < qty:
                    # v7.0.0 Phase 5 \u2014 software AON partial fill.
                    # Per Val 2026-05-06: keep the partial alive and let
                    # normal sentinels manage it. Do NOT force-flatten.
                    # The \u26a0\ufe0f message here REPLACES the normal \u2705
                    # confirmation below (single-message contract).
                    logger.warning(
                        "[%s] [V700-AON-SOFTWARE] %s LONG partial %d/%d "
                        "\u2014 keeping partial, engine will manage",
                        self.NAME,
                        ticker,
                        filled_qty,
                        qty,
                    )
                    msg = (
                        f"\u26a0\ufe0f {label}: {ticker} LONG partial "
                        f"{filled_qty}/{qty} \u2014 keeping partial, "
                        f"sentinels engaged (order_id={oid})"
                    )
                    logger.warning("[%s] [V700-AON-SOFTWARE] %s", self.NAME, msg)
                    self._send_own_telegram(msg)
                    _aon_partial_notified = True
                    # Fall through to _record_position with filled_qty.
                elif filled_qty < qty:
                    logger.warning(
                        "[%s] [V6151-PARTIAL] %s LONG partial fill: "
                        "requested=%d filled=%d order_id=%s",
                        self.NAME,
                        ticker,
                        qty,
                        filled_qty,
                        oid,
                    )
                self._record_position(ticker, "LONG", filled_qty, price)
                if not _aon_partial_notified:
                    msg = f"\u2705 {label}: {ticker} BUY {filled_qty} shares @ {order_descr} (order_id={oid})"
                    logger.info(msg)
                    self._send_own_telegram(msg)
                # v5.25.0 / v6.0.7 \u2014 sync local row from broker book
                # with eventual-consistency-aware grace window.
                self._reconcile_position_with_broker(ticker, expect="present")
                # v7.0.0 Phase 4 \u2014 book confirmed fill into this executor's
                # PortfolioBook so val/gene track their own positions, ratchets,
                # and day-P&L independently of main. Best-effort: never block
                # trading on bookkeeping failure.
                try:
                    from engine.portfolio_book import PORTFOLIOS

                    _book_id = self.NAME.lower()
                    _pb = PORTFOLIOS.get(_book_id)
                    _pb.record_entry_with_fill(
                        ticker=ticker,
                        side="LONG",
                        fill_price=price,
                        shares=filled_qty,
                        entry_count=1,
                    )
                except Exception:
                    logger.debug(
                        "[%s] per-book record_entry_with_fill LONG skipped: %s",
                        self.NAME,
                        ticker,
                        exc_info=True,
                    )
            elif kind == "ENTRY_SHORT":
                qty = signal_qty if signal_qty > 0 else self._shares_for(price, ticker=ticker)
                if qty <= 0:
                    # v7.83.0 -- forensic log; see ENTRY_LONG branch.
                    logger.warning(
                        "[V79-MIRROR-SKIP] %s ENTRY_SHORT %s qty=0 "
                        "(main_shares=%s, local_fallback_shares_for=%d)",
                        self.NAME,
                        ticker,
                        signal_qty,
                        self._shares_for(price, ticker=ticker),
                    )
                    return
                logger.info(
                    "[V79-MIRROR-DISPATCH] %s ENTRY_SHORT %s qty=%d price=%s",
                    self.NAME,
                    ticker,
                    qty,
                    price,
                )
                coid = self._build_client_order_id(ticker, "SHORT")
                req, order_descr = _build_entry_request("SHORT", qty, coid, price=price)
                order = self._submit_order_idempotent(client, req, coid)
                oid = getattr(order, "id", "?")
                # v6.15.1 \u2014 same partial-fill handling as LONG above.
                # v6.15.2 \u2014 TIF-aware filled_qty + post-action reconcile.
                filled_qty = self._extract_filled_qty(order, qty, req=req)
                if filled_qty == 0:
                    # v7.0.0 Phase 5 \u2014 quiet ZEROFILL: suppress the
                    # initial \u26a0\ufe0f \"unfilled, reconciling...\" ping.
                    # Forensic WARNING log unchanged; only Telegram surface
                    # collapses to a single final message after reconcile.
                    msg = (
                        f"\u26a0\ufe0f {label}: {ticker} SHORT IOC unfilled "
                        f"(requested={qty}, filled=0, order_id={oid}) "
                        f"\u2014 reconciling against broker"
                    )
                    logger.warning("[%s] [V6152-ZEROFILL] %s", self.NAME, msg)
                    # NOTE: first telegram suppressed per v7.0.0 spec D.
                    _reconcile_raised = False
                    try:
                        self._reconcile_position_with_broker(ticker, expect="present")
                    except Exception:
                        _reconcile_raised = True
                        logger.exception(
                            "[%s] [V6152-ZEROFILL] reconcile raised on %s SHORT",
                            self.NAME,
                            ticker,
                        )
                    self._emit_zerofill_reconcile_followup(
                        label=label,
                        ticker=ticker,
                        side="SHORT",
                        requested_qty=qty,
                        order_id=oid,
                        reconcile_raised=_reconcile_raised,
                    )
                    return
                _aon_partial_notified = False
                if self._aon_mode == "software" and 0 < filled_qty < qty:
                    # v7.0.0 Phase 5 \u2014 software AON partial fill.
                    # Per Val 2026-05-06: keep the partial alive and let
                    # normal sentinels manage it. Do NOT force-flatten.
                    # The \u26a0\ufe0f message here REPLACES the normal \u2705
                    # confirmation below (single-message contract).
                    logger.warning(
                        "[%s] [V700-AON-SOFTWARE] %s SHORT partial %d/%d "
                        "\u2014 keeping partial, engine will manage",
                        self.NAME,
                        ticker,
                        filled_qty,
                        qty,
                    )
                    msg = (
                        f"\u26a0\ufe0f {label}: {ticker} SHORT partial "
                        f"{filled_qty}/{qty} \u2014 keeping partial, "
                        f"sentinels engaged (order_id={oid})"
                    )
                    logger.warning("[%s] [V700-AON-SOFTWARE] %s", self.NAME, msg)
                    self._send_own_telegram(msg)
                    _aon_partial_notified = True
                    # Fall through to _record_position with filled_qty.
                elif filled_qty < qty:
                    logger.warning(
                        "[%s] [V6151-PARTIAL] %s SHORT partial fill: "
                        "requested=%d filled=%d order_id=%s",
                        self.NAME,
                        ticker,
                        qty,
                        filled_qty,
                        oid,
                    )
                self._record_position(ticker, "SHORT", filled_qty, price)
                if not _aon_partial_notified:
                    msg = f"\u2705 {label}: {ticker} SELL {filled_qty} shares short @ {order_descr} (order_id={oid})"
                    logger.info(msg)
                    self._send_own_telegram(msg)
                # v5.25.0 / v6.0.7 \u2014 sync local row from broker book
                # with eventual-consistency-aware grace window.
                self._reconcile_position_with_broker(ticker, expect="present")
                # v7.0.0 Phase 4 \u2014 book confirmed fill into this executor's
                # PortfolioBook (symmetric with ENTRY_LONG path above).
                try:
                    from engine.portfolio_book import PORTFOLIOS

                    _book_id = self.NAME.lower()
                    _pb = PORTFOLIOS.get(_book_id)
                    _pb.record_entry_with_fill(
                        ticker=ticker,
                        side="SHORT",
                        fill_price=price,
                        shares=filled_qty,
                        entry_count=1,
                    )
                except Exception:
                    logger.debug(
                        "[%s] per-book record_entry_with_fill SHORT skipped: %s",
                        self.NAME,
                        ticker,
                        exc_info=True,
                    )
            elif kind in ("PARTIAL_EXIT_LONG", "PARTIAL_EXIT_SHORT"):
                # v8.1.1 -- partial-profit-at-1R mirror. main_shares
                # carries the partial-close share count (NOT the
                # remaining position size). Position stays open on
                # this executor with qty -= partial_shares.
                if ticker not in self.positions:
                    logger.info(
                        "[%s] %s %s skipped \u2014 no position tracked",
                        self.NAME,
                        kind,
                        ticker,
                    )
                    return
                partial_qty = 0
                try:
                    partial_qty = int(main_shares or 0)
                except Exception:
                    partial_qty = 0
                if partial_qty <= 0:
                    logger.warning(
                        "[%s] [V81-MIRROR-SKIP] %s partial_qty=0 (main_shares=%s)",
                        self.NAME,
                        kind,
                        main_shares,
                    )
                    return
                self._partial_close_position_idempotent(
                    client,
                    ticker,
                    partial_qty,
                    label,
                    reason,
                )
                return
            elif kind in ("EXIT_LONG", "EXIT_SHORT"):
                # v5.24.0 \u2014 ``state.db.executor_positions`` (loaded
                # into ``self.positions`` on boot, kept in sync via
                # ``_record_position`` / ``_remove_position``) is the
                # source of truth for whether THIS executor has the
                # ticker open. If it doesn't, skip silently \u2014 a
                # divergent paper book may have flagged an exit for
                # something this executor never opened, and calling
                # ``client.close_position`` against an already-flat
                # account just produces a 40410000 false alarm.
                if ticker not in self.positions:
                    logger.info(
                        "[%s] %s %s skipped \u2014 no position tracked",
                        self.NAME,
                        kind,
                        ticker,
                    )
                    return
                # v7.0.0 Phase 4 \u2014 capture pre-close ratchet extremes
                # from the PortfolioBook before the position is removed,
                # so record_exit can update the session ratchet correctly.
                _exit_side = "SHORT" if kind == "EXIT_SHORT" else "LONG"
                _exit_leg_extreme: float | None = None
                try:
                    from engine.portfolio_book import PORTFOLIOS

                    _book_id_exit = self.NAME.lower()
                    _pb_exit = PORTFOLIOS.get(_book_id_exit)
                    _bpos = (
                        _pb_exit.short_positions.get(ticker.upper())
                        if _exit_side == "SHORT"
                        else _pb_exit.positions.get(ticker.upper())
                    )
                    if _bpos is not None:
                        _exit_leg_extreme = _bpos.get("v531_max_favorable_price")
                except Exception:
                    logger.debug(
                        "[%s] per-book pre-exit capture skipped: %s",
                        self.NAME,
                        ticker,
                        exc_info=True,
                    )
                self._close_position_idempotent(client, ticker, label, reason)
                # v5.25.0 / v6.0.7 \u2014 confirm flat on broker, with grace
                # to ride out Alpaca's eventual-consistency window so we
                # do not graft a phantom row from a still-pending close.
                self._reconcile_position_with_broker(ticker, expect="flat")
                # v7.0.0 Phase 4 \u2014 update this executor's PortfolioBook
                # ratchet and remove the position row. Symmetric with
                # the ENTRY bookkeeping above; best-effort try/except.
                try:
                    from engine.portfolio_book import PORTFOLIOS

                    _book_id_exit2 = self.NAME.lower()
                    _pb_exit2 = PORTFOLIOS.get(_book_id_exit2)
                    if _exit_side == "LONG":
                        _pb_exit2.record_exit(ticker, "LONG", leg_high=_exit_leg_extreme)
                        _pb_exit2.positions.pop(ticker.upper(), None)
                    else:
                        _pb_exit2.record_exit(ticker, "SHORT", leg_low=_exit_leg_extreme)
                        _pb_exit2.short_positions.pop(ticker.upper(), None)
                    # Post-loss cooldown: record on this book so the next
                    # fire_long/fire_short call for (ticker, side) is blocked
                    # for POST_LOSS_COOLDOWN_MIN minutes. Mirrors Main's path
                    # in broker/orders.py. Only fires on stop exits (not EOD
                    # flushes or target exits -- those are not losses by design).
                    _stop_reasons = {
                        "stop",
                        "be_stop",
                        "sentinel_a_stop_price",
                        "sentinel_r2_hard_stop",
                        "sentinel_v651_deep_stop",
                        "v750_early_ditch",
                        "forensic_stop",
                        "per_trade_brake",
                        "velocity_fuse",
                        "ema_trail",
                    }
                    if reason.lower() in _stop_reasons or "stop" in reason.lower():
                        _pos_rec = self.positions.get(ticker, {})
                        _entry_px = float(_pos_rec.get("entry_price") or 0.0)
                        _qty = int(_pos_rec.get("qty") or 0)
                        if _entry_px > 0 and _qty > 0 and float(price) > 0:
                            if _exit_side == "LONG":
                                _exit_pnl = (float(price) - _entry_px) * _qty
                            else:
                                _exit_pnl = (_entry_px - float(price)) * _qty
                            _pb_exit2.record_post_loss_cooldown(
                                ticker, _exit_side.lower(), _exit_pnl
                            )
                except Exception:
                    logger.debug(
                        "[%s] per-book record_exit skipped: %s",
                        self.NAME,
                        ticker,
                        exc_info=True,
                    )
            elif kind == "EOD_CLOSE_ALL":
                client.close_all_positions(cancel_orders=True)
                # v5.5.10 \u2014 wipe every local + persisted row.
                for tkr in list(self.positions.keys()):
                    self._remove_position(tkr)
                msg = f"\u2705 {label}: EOD close_all_positions"
                logger.info(msg)
                self._send_own_telegram(msg)
                # v5.25.0 \u2014 full sweep so any laggard fills or stale
                # rows get reconciled against the now-flat broker book.
                try:
                    self._reconcile_broker_positions()
                except Exception:
                    logger.exception(
                        "[%s] EOD_CLOSE_ALL post-sweep reconcile raised",
                        self.NAME,
                    )
            else:
                logger.warning("[%s] unknown signal kind %r", self.NAME, kind)
        except Exception as e:
            err = f"\u274c {label}: {ticker or kind} failed: {e}"
            logger.exception("[%s] dispatch failed on %s", self.NAME, kind)
            self._send_own_telegram(err)

    # ---------- own Telegram bot ----------
    async def _auth_guard(self, update, context):
        """Owner-whitelist guard identical in pattern to main's guard.

        v5.0.3 \u2014 also auto-learns the owner's chat_id from any
        inbound message and persists it to disk via _record_owner_chat,
        so trade confirmations get fanned out to the right DM without
        the operator hand-setting <PREFIX>TELEGRAM_CHAT_ID on Railway.
        """
        eff_user = getattr(update, "effective_user", None)
        uid = str(eff_user.id) if eff_user and getattr(eff_user, "id", None) is not None else ""
        if uid and uid in self.owner_ids:
            # v5.0.3: capture the chat_id this owner is DMing us from.
            # effective_chat is the canonical PTB hook; fall back to
            # message.chat where present for older-style updates.
            chat = getattr(update, "effective_chat", None)
            chat_id = getattr(chat, "id", None) if chat is not None else None
            if chat_id is None:
                msg = getattr(update, "message", None)
                if msg is not None:
                    sub = getattr(msg, "chat", None)
                    chat_id = getattr(sub, "id", None) if sub is not None else None
            if chat_id is not None:
                try:
                    self._record_owner_chat(uid, int(chat_id))
                except Exception:
                    logger.exception("[%s] _record_owner_chat raised", self.NAME)
            return
        logger.warning(
            "[%s] auth_guard dropped non-owner (user_id=%r)",
            self.NAME,
            uid or "(none)",
        )
        raise ApplicationHandlerStop

    async def cmd_mode(self, update, context):
        """/mode paper  |  /mode live confirm"""
        args = context.args if context and hasattr(context, "args") else []
        if not args:
            await update.message.reply_text(
                f"{self.NAME} mode: {self.mode}\nUsage: /mode paper  |  /mode live confirm"
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
            await update.message.reply_text(f"\u274c {self.NAME}: no alpaca client")
            return
        try:
            client.close_all_positions(cancel_orders=True)
            # v5.5.10 \u2014 drop every local + persisted row so a
            # reboot does not see them as stale positions.
            for tkr in list(self.positions.keys()):
                self._remove_position(tkr)
            await update.message.reply_text(
                f"\u2705 {self.NAME}: HALT \u2014 close_all_positions fired"
            )
        except Exception as e:
            await update.message.reply_text(f"\u274c {self.NAME}: halt failed: {e}")

    async def cmd_version(self, update, context):
        await update.message.reply_text(f"{self.NAME} executor v{BOT_VERSION}\nmode: {self.mode}")

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
            await update.message.reply_text(f"\u274c {self.NAME}: no alpaca client")
            return
        try:
            acct = client.get_account()
            cash = float(getattr(acct, "cash", 0) or 0)
            bp = float(getattr(acct, "buying_power", 0) or 0)
            eq = float(getattr(acct, "equity", 0) or 0)
            # v5.1.4 \u2014 surface the equity-aware sizing caps so
            # operators can see what the next entry will be sized at.
            equity_cap = eq * (self.max_pct_per_entry / 100.0)
            cash_avail = max(0.0, cash - self.min_reserve_cash)
            next_entry = min(
                self.dollars_per_entry,
                equity_cap,
                cash_avail,
            )
            await update.message.reply_text(
                f"\U0001f4b0 {self.NAME} ({self.mode})\n"
                f"  cash:   ${cash:,.2f}\n"
                f"  equity: ${eq:,.2f}\n"
                f"  bp:     ${bp:,.2f}\n"
                f"  cap:    ${equity_cap:,.2f} "
                f"({self.max_pct_per_entry:.1f}% of equity)\n"
                f"  reserve:${self.min_reserve_cash:,.2f}\n"
                f"  next entry: ${next_entry:,.2f}"
            )
        except Exception as e:
            await update.message.reply_text(f"\u274c {self.NAME}: cash fetch failed: {e}")

    async def cmd_positions(self, update, context):
        """/positions — compact positions list only."""
        client = self._ensure_client()
        if client is None:
            await update.message.reply_text(f"\u274c {self.NAME}: no alpaca client")
            return
        try:
            positions = client.get_all_positions()
            if not positions:
                await update.message.reply_text(f"{self.NAME}: no open positions")
                return
            lines = [f"{self.NAME} positions ({len(positions)})"]
            for p in positions[:25]:
                sym = getattr(p, "symbol", "?")
                qty = getattr(p, "qty", "?")
                avg = getattr(p, "avg_entry_price", "?")
                try:
                    upl = float(getattr(p, "unrealized_pl", 0) or 0)
                    pct = float(getattr(p, "unrealized_plpc", 0) or 0) * 100
                    lines.append(f"  {sym}: {qty} @ {avg} pnl=${upl:+,.2f} ({pct:+.2f}%)")
                except Exception:
                    lines.append(f"  {sym}: {qty} @ {avg}")
            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(f"\u274c {self.NAME}: positions fetch failed: {e}")

    async def cmd_orders(self, update, context):
        """/orders — recent orders (last 10)."""
        client = self._ensure_client()
        if client is None:
            await update.message.reply_text(f"\u274c {self.NAME}: no alpaca client")
            return
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus

            req = GetOrdersRequest(
                status=QueryOrderStatus.ALL,
                limit=10,
            )
            orders = client.get_orders(filter=req)
            if not orders:
                await update.message.reply_text(f"{self.NAME}: no recent orders")
                return
            lines = [f"{self.NAME} recent orders ({len(orders)})"]
            for o in orders:
                sym = getattr(o, "symbol", "?")
                side = getattr(getattr(o, "side", None), "value", "?")
                qty = getattr(o, "qty", "?") or getattr(o, "notional", "?")
                stat = getattr(getattr(o, "status", None), "value", "?")
                filled = getattr(o, "filled_avg_price", None)
                tail = f" @ {filled}" if filled else ""
                lines.append(f"  {sym} {side} {qty} [{stat}]{tail}")
            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(f"\u274c {self.NAME}: orders fetch failed: {e}")

    async def cmd_signal(self, update, context):
        """/signal — show last signal received from main's bus."""
        sig = self.last_signal
        if not sig:
            await update.message.reply_text(f"{self.NAME}: no signals received yet")
            return
        try:
            import json as _json

            pretty = _json.dumps(sig, indent=2, default=str)[:1500]
        except Exception:
            pretty = str(sig)[:1500]
        await update.message.reply_text(f"{self.NAME} last signal:\n{pretty}")

    # -----------------------------------------------------------------

    # Commands shown in Telegram's BotFather / slash menu. Keep short
    # descriptions — Telegram truncates aggressively on mobile.
    TG_MENU_COMMANDS = [
        ("status", "Account, positions, and P&L"),
        ("positions", "Open positions only"),
        ("orders", "Recent orders (last 10)"),
        ("cash", "Account balance snapshot"),
        ("signal", "Last signal from main"),
        ("mode", "Show or change mode (paper / live)"),
        ("halt", "Emergency halt \u2014 flatten all"),
        ("ping", "Liveness check"),
        ("version", "Show running version"),
        ("help", "List available commands"),
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
            await app.bot.set_my_commands(cmds, scope=BotCommandScopeAllPrivateChats())
            logger.info("[%s] registered %d telegram menu commands", self.NAME, len(cmds))
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
        app = Application.builder().token(self.telegram_token).build()
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
        logger.info("[%s] telegram loop running (token=...%s)", self.NAME, self.telegram_token[-6:])
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
        _tg().register_signal_listener(self._on_signal)
        # Try to build the alpaca client eagerly so startup logs surface
        # missing/bad creds; failures are already caught + logged.
        self._ensure_client()
        # v7.0.0 Phase 5 \u2014 AON probe: detect whether the current Alpaca
        # SDK accepts all_or_none=True on LimitOrderRequest (native mode)
        # or falls back to software partial detection. Logged once per
        # executor at INFO level for ops visibility.
        self._aon_mode = self._probe_aon_support()
        logger.info("[V700-AON] %s mode=%s", self.NAME, self._aon_mode)
        # v5.2.1 \u2014 reconcile broker-side positions into self.positions
        # before the scan loop starts so orphan trades (broker accepted
        # but client timed out on a prior boot) get managed as normal.
        # Wrapped: a bad reconcile must not block scanner startup.
        try:
            self._reconcile_broker_positions()
        except Exception:
            logger.exception(
                "[%s] [RECONCILE] unexpected failure \u2014 continuing startup",
                self.NAME,
            )
        logger.info("[%s] started in %s mode", self.NAME, self.mode)
        # Own Telegram bot in a background thread so main.run_telegram_bot()
        # can still own the main-process asyncio loop.
        t = threading.Thread(target=self._run_tg_loop, daemon=True, name=f"{self.NAME}_tg")
        t.start()
