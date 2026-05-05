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

    def _submit_order_idempotent(self, client, req, coid: str):
        """Wrap client.submit_order with duplicate-coid \u2192 success handling.

        On APIError whose message says the client_order_id must be unique
        (HTTP 422 from Alpaca), look up the existing order by coid and
        return it as if the submit had just succeeded. Re-raise anything
        else.
        """
        try:
            return client.submit_order(req)
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
                return existing
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
        """
        self.positions.pop(ticker, None)
        self._delete_persisted_position(ticker)

    def _stamp_action(self, ticker: str) -> None:
        """v6.0.7 \u2014 record wall-clock of the last ENTRY/EXIT submit
        so the post-action reconcile knows when Alpaca's REST eventual-
        consistency window started. See RECONCILE_GRACE_SECONDS.
        """
        self._last_action_ts[ticker] = time.monotonic()

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

    def _reconcile_broker_positions(self) -> None:
        """Run once at boot. Pull broker positions, graft any orphans.

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
        """Listener callback: dispatch on event['kind']."""
        kind = event.get("kind", "")
        ticker = event.get("ticker", "")
        price = event.get("price", 0.0) or 0.0
        reason = event.get("reason", "")
        label = f"{self.NAME} {self.mode}"

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
            logger.warning(
                "[%s] skip %s %s \u2014 no alpaca client",
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
        def _build_entry_request(side_label: str, qty: int, coid: str):
            order_side = OrderSide.BUY if side_label == "LONG" else OrderSide.SELL
            try:
                from broker.orders import compute_strike_limit_price

                tg_mod = _tg()
                bid = ask = None
                if tg_mod is not None and hasattr(tg_mod, "_v512_quote_snapshot"):
                    bid, ask = tg_mod._v512_quote_snapshot(ticker)
                if bid is not None and ask is not None and bid > 0 and ask > 0:
                    limit_px = compute_strike_limit_price(
                        side=side_label, ask=float(ask), bid=float(bid)
                    )
                    return (
                        LimitOrderRequest(
                            symbol=ticker,
                            qty=qty,
                            side=order_side,
                            # v6.15.0 \u2014 marketable LIMITs use IOC so a
                            # stale or partially-filled order never sits
                            # on the book and fragments on liquidity hits.
                            time_in_force=TimeInForce.IOC,
                            client_order_id=coid,
                            limit_price=round(float(limit_px), 2),
                        ),
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

        try:
            if kind == "ENTRY_LONG":
                qty = signal_qty if signal_qty > 0 else self._shares_for(price, ticker=ticker)
                if qty <= 0:
                    return
                coid = self._build_client_order_id(ticker, "LONG")
                req, order_descr = _build_entry_request("LONG", qty, coid)
                order = self._submit_order_idempotent(client, req, coid)
                oid = getattr(order, "id", "?")
                self._record_position(ticker, "LONG", qty, price)
                msg = f"\u2705 {label}: {ticker} BUY {qty} shares @ {order_descr} (order_id={oid})"
                logger.info(msg)
                self._send_own_telegram(msg)
                # v5.25.0 / v6.0.7 \u2014 sync local row from broker book
                # with eventual-consistency-aware grace window.
                self._reconcile_position_with_broker(ticker, expect="present")
            elif kind == "ENTRY_SHORT":
                qty = signal_qty if signal_qty > 0 else self._shares_for(price, ticker=ticker)
                if qty <= 0:
                    return
                coid = self._build_client_order_id(ticker, "SHORT")
                req, order_descr = _build_entry_request("SHORT", qty, coid)
                order = self._submit_order_idempotent(client, req, coid)
                oid = getattr(order, "id", "?")
                self._record_position(ticker, "SHORT", qty, price)
                msg = f"\u2705 {label}: {ticker} SELL {qty} shares short @ {order_descr} (order_id={oid})"
                logger.info(msg)
                self._send_own_telegram(msg)
                # v5.25.0 / v6.0.7 \u2014 sync local row from broker book
                # with eventual-consistency-aware grace window.
                self._reconcile_position_with_broker(ticker, expect="present")
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
                self._close_position_idempotent(client, ticker, label, reason)
                # v5.25.0 / v6.0.7 \u2014 confirm flat on broker, with grace
                # to ride out Alpaca's eventual-consistency window so we
                # do not graft a phantom row from a still-pending close.
                self._reconcile_position_with_broker(ticker, expect="flat")
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
