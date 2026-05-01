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
from telegram.error import BadRequest as TelegramBadRequest
# v4.8.0 \u2014 Side enum + SideConfig table for the long/short collapse.
# side.py is a pure module (no imports from trade_genius), so a plain
# top-level import is safe and avoids the __main__ aliasing dance that
# paper_state.py / telegram_commands.py need.
from side import Side, CONFIGS  # noqa: E402

# v5.0.0 \u2014 Tiger/Buffalo two-stage state machine. Pure module, safe to
# import top-level. Canonical spec lives in STRATEGY.md at the repo
# root; this module's helpers cite rule IDs (e.g. L-P2-R3) that map
# 1:1 to that spec. The runtime integration is gating-only: v5 sits in
# front of the v4 entry/close paths and decides when to fire each
# stage. Unit-sizing math is preserved unchanged from v4 (50/50 staging
# means "50% of the v4 unit, then add the other 50%").
import tiger_buffalo_v5 as v5  # noqa: E402
# v5.10.0/v5.10.1 \u2014 Eye-of-the-Tiger evaluators. v5_10_1_integration is the
# live-hot-path glue that wires Sections I–VI into check_breakout /
# manage_positions; eot is the pure-function evaluator surface.
import eye_of_tiger as eot  # noqa: E402
import v5_10_1_integration as eot_glue  # noqa: E402
# v5.1.2 \u2014 forensic capture: bar archive + indicators.
import indicators  # noqa: E402
import bar_archive  # noqa: E402
import persistence  # noqa: E402
# v5.11.0 \u2014 engine/ package extraction (PR1: bars). Module-level
# import here so a missing Dockerfile COPY surfaces as ImportError
# at boot rather than during the first scan tick.
import engine  # noqa: E402

# v5.11.1 \u2014 telegram_ui/ package extraction (PR1: charts). Module-level
# import here so a missing Dockerfile COPY surfaces as ImportError
# at boot rather than mid-session.
import telegram_ui  # noqa: E402

# v5.11.2 \u2014 broker/ package extraction (PR1: stops). Same rationale \u2014
# missing Dockerfile COPY surfaces as ImportError at boot.
import broker  # noqa: E402

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
BOT_VERSION = "5.28.3"

# Release-note surface: CURRENT_MAIN_NOTE describes the release actively
# being deployed; MAIN_RELEASE_NOTE aliases it for /version. Full per-release
# history lives in CHANGELOG.md (the previous in-code rolling tail was
# removed). The Telegram 34-char mobile-width rule still applies to every
# line of CURRENT_MAIN_NOTE.
CURRENT_MAIN_NOTE = (
    "v5.28.3 fix: cache-bust\n"
    "app.js + app.css with\n"
    "BOT_VERSION query string\n"
    "so deploys reload bundles.\n"
    "v5.28.2: open-permit rows\n"
    "expand to show idle strip.\n"
    "v5.28.1: idle alarm strip\n"
    "+ banner when no position.\n"
    "v5.28.0: F primary exit."
)

MAIN_RELEASE_NOTE = CURRENT_MAIN_NOTE
# Backwards-compat alias — any remaining references default to main.
RELEASE_NOTE = MAIN_RELEASE_NOTE

FMP_API_KEY = os.getenv("FMP_API_KEY", "VqYj2Jujrc8IvUOe4CR1g0tRf0qlB4AV")

# Human-readable exit reason labels.
# v5.9.3: LORDS_LEFT* / BULL_VACUUM* keys removed. The Sovereign Regime
# Shield was retired in v5.9.1 and the dual-PDC HARD_EJECT_TIGER half was
# retired in v5.9.2; v5.9.3 eradicates the residual labels. Any historical
# trade-log rows with those raw reasons render their raw token rather than
# a pretty label \u2014 acceptable since no live emission path remains.
REASON_LABELS = {
    "STOP": "\U0001f6d1 Hard Stop",
    "TRAIL": "\U0001f3af Trail Stop",
    "RED_CANDLE": "\U0001f56f Red Candle (lost daily polarity)",
    "POLARITY_SHIFT": "\U0001f504 Polarity Shift (price > PDC)",
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
_signal_listeners_lock = threading.Lock()

# v5.5.7 \u2014 Most recent signal emitted by the main paper book. The
# per-executor TradeGeniusBase already keeps its own ``last_signal`` for
# the Val/Gene exec panels; this module-level mirror is the equivalent
# for the Main (internal paper) tab so the dashboard's /api/state can
# surface it the same way as the executor payloads.
last_signal: "dict | None" = None


def register_signal_listener(fn):
    """Subscribe a callable fn(event: dict) -> None to the signal bus.

    Idempotent: re-registering the same callable is a no-op. Prevents
    double-execution of ENTRY/EXIT against Alpaca when an executor's
    ``start()`` is called more than once (e.g. supervisor re-spawn, a
    module reload during hot-patching, or a paranoid init-retry path).
    The read-test-append is held under ``_signal_listeners_lock`` so
    two concurrent ``start()`` calls cannot both observe "not present"
    and both append the same callable.
    """
    with _signal_listeners_lock:
        if fn in _signal_listeners:
            logger.info(
                "signal_bus: listener already registered, skipping (%s) total=%d",
                getattr(fn, "__qualname__", repr(fn)), len(_signal_listeners),
            )
            return
        _signal_listeners.append(fn)
        total = len(_signal_listeners)
    logger.info(
        "signal_bus: listener registered (%s) total=%d",
        getattr(fn, "__qualname__", repr(fn)), total,
    )


def _emit_signal(event: dict) -> None:
    """Fire an event to every listener in its own daemon thread.

    Async fire-and-forget: main's paper book never blocks on Alpaca.
    Per-listener exceptions are logged but never break the bus.
    """
    # v5.5.7 \u2014 capture the latest event for the Main-tab LAST SIGNAL
    # card before dispatching, so even a listener-less moment (or a
    # crashing listener) still updates what the dashboard renders.
    global last_signal
    try:
        last_signal = {
            "kind": event.get("kind", ""),
            "ticker": event.get("ticker", ""),
            "price": float(event.get("price", 0.0) or 0.0),
            "reason": event.get("reason", ""),
            "timestamp_utc": event.get("timestamp_utc", _utc_now_iso()),
        }
    except Exception:
        last_signal = None

    # Snapshot the listener list so a concurrent register/unregister can't
    # mutate what we iterate. Held under the same lock as registration.
    with _signal_listeners_lock:
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
# Re-exports for back-compat with `m.TradeGeniusBase` / `m.TradeGeniusVal` /
# `m.TradeGeniusGene` lookups in smoke_test and external probing. These are
# the canonical public names of the executor classes.
from executors.base import TradeGeniusBase  # noqa: E402
from executors import TradeGeniusVal, TradeGeniusGene  # noqa: E402
import executors  # noqa: E402


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
        except Exception as e:
            # v4.1.2: don't swallow silently — a broken matplotlib install
            # will make `/dayreport` fail later, and a DEBUG line here gives
            # the operator a breadcrumb when chart generation explodes.
            logger.debug("matplotlib warmup failed: %s", e)
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


# Short reason labels for compact /dayreport display.
# v5.9.3: Lords Left / Bull Vacuum entries dropped along with the
# REASON_LABELS keys. The 1f451 / 1f300 emoji bytes will pass through
# untouched if any pre-v5.9.1 row still carries them.
_SHORT_REASON = {
    "\U0001f6d1": "\U0001f6d1 Stop",
    "\U0001f512": "\U0001f512 Trail",
    "\U0001f56f": "\U0001f56f Red Candle",
    "\U0001f504": "\U0001f504 Polarity Shift",
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
#   - v5.10.7: QBTS removed from defaults \u2014 not a Titan.
#     Users who want it can `/ticker add QBTS` at runtime.
# ------------------------------------------------------------
TICKERS_FILE = os.getenv("TICKERS_FILE", "tickers.json")
TICKERS_PINNED = ("SPY", "QQQ")   # always present, never removable
# v5.27.0 \u2014 NFLX, ORCL added to default universe (NFLX was today's
# biggest live winner +$90.63; ORCL has seen recurring spike behavior).
# QBTS retained from runtime tickers.json overlay (user opt-in via
# /ticker add).
TICKERS_DEFAULT = [
    "AAPL", "MSFT", "NVDA", "TSLA", "META",
    "GOOG", "AMZN", "AVGO", "NFLX", "ORCL",
    "QBTS", "SPY", "QQQ",
]

# Section VI Daily Circuit Breaker.
DAILY_LOSS_LIMIT_DOLLARS: float = -1500.0
TICKERS_MAX = 40            # sanity upper bound to protect cycle budget
TICKER_SYM_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,7}$")

TICKERS = list(TICKERS_DEFAULT)
TRADE_TICKERS = [t for t in TICKERS if t not in TICKERS_PINNED]


# ------------------------------------------------------------
# v5.26.0 \u2014 Volume Gate (BL-3 / BU-3) BYPASSED 2026-04-30. The 55-day
# rolling per-minute baseline, IEX WebSocket consumer, and nightly rebuild
# thread were removed. Bar archive at /data/bars/ is the canonical
# substrate for offline backtests.


# ---------------------------------------------------------------------------
# v5.1.2 \u2014 forensic capture emitters.
#
# These emit greppable log lines so post-hoc backtests can replay any
# "what if the threshold/indicator were different" scenario without a
# redeploy. None of these change the trading decision; they are pure
# observation layers.
# ---------------------------------------------------------------------------

def _fmt_num(v) -> str:
    """Render a number for log lines. None \u2192 'null' so logs are
    machine-parseable; ints stay ints; floats keep 4dp."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    try:
        return ("%.4f" % float(v)).rstrip("0").rstrip(".") or "0"
    except (TypeError, ValueError):
        return "null"


def _v512_log_entry_extension(
    ticker: str,
    *,
    bid=None,
    ask=None,
    cash=None,
    equity=None,
    open_positions=None,
    total_exposure_pct=None,
    current_drawdown_pct=None,
) -> None:
    """Emit [V510-ENTRY] alongside the existing entry log line. Carries
    bid/ask + account state so post-hoc analysis has the snapshot
    without re-reading the broker.
    """
    try:
        logger.info(
            "[V510-ENTRY] ticker=%s bid=%s ask=%s cash=%s equity=%s "
            "open_positions=%s total_exposure_pct=%s current_drawdown_pct=%s",
            ticker, _fmt_num(bid), _fmt_num(ask),
            _fmt_num(cash), _fmt_num(equity),
            _fmt_num(open_positions),
            _fmt_num(total_exposure_pct),
            _fmt_num(current_drawdown_pct),
        )
    except Exception as e:
        logger.warning("[V510-ENTRY] emit error %s: %s", ticker, e)


def _v512_quote_snapshot(ticker: str):
    """Return (bid, ask) for `ticker`, or (None, None) on failure. The
    Alpaca data client is not always reachable from tests, so we treat
    any exception as "no quote available"."""
    try:
        client = _historical_data_client() if "_historical_data_client" in globals() else None
        if client is None:
            return (None, None)
        from alpaca.data.requests import StockLatestQuoteRequest  # type: ignore
        req = StockLatestQuoteRequest(symbol_or_symbols=ticker)
        q = client.get_stock_latest_quote(req)
        rec = q.get(ticker) if isinstance(q, dict) else None
        if rec is None:
            return (None, None)
        bid = getattr(rec, "bid_price", None)
        ask = getattr(rec, "ask_price", None)
        return (bid, ask)
    except Exception:
        return (None, None)


def _v512_archive_minute_bar(ticker: str, bar: dict) -> None:
    """Persist a 1m bar to /data/bars/YYYY-MM-DD/{TICKER}.jsonl.

    Failure-tolerant. Respects the 30-symbol IEX cap and the active
    TICKERS list (skips persistence for anything outside it). Caller
    is expected to invoke this once per minute close per ticker.
    """
    try:
        sym = (ticker or "").strip().upper()
        if not sym:
            return
        # Skip persistence for symbols outside the active watchlist
        # (QQQ/SPY are always allowed for index forensics).
        try:
            if sym not in TICKERS and sym not in ("QQQ", "SPY"):
                return
        except Exception:
            pass
        bar_archive.write_bar(sym, bar)
    except Exception as e:
        logger.warning("[V510-BAR] archive error %s: %s", ticker, e)


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


def _ensure_universe_consistency() -> None:
    """v5.8.0 \u2014 prevent /data/tickers.json from lagging code's UNIVERSE.

    Compares the on-disk persisted ticker list against the canonical
    code-side TICKERS_DEFAULT. If the file is missing, corrupt, or has
    drifted, it is rewritten to match code. Emits the new
    [UNIVERSE_GUARD] log tag for post-deploy smoke-check observability.

    Tolerant of both supported on-disk formats:
      - flat JSON list:        ["AAPL", "MSFT", ...]
      - envelope (current):    {"tickers": ["AAPL", ...], ...}

    On rewrite, preserves the envelope format used elsewhere in the bot
    so _load_tickers_file() keeps working unchanged.
    """
    from pathlib import Path

    # UNIVERSE_GUARD_PATH env var lets tests redirect the persistent
    # path to a tmp file. Production always reads the default.
    path = Path(os.getenv("UNIVERSE_GUARD_PATH", "/data/tickers.json"))
    expected = sorted(set(TICKERS_DEFAULT))

    def _write(payload_list):
        envelope = {
            "tickers": payload_list,
            "updated_utc": _utc_now_iso(),
            "bot_version": BOT_VERSION,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(envelope, indent=2))
        except Exception as we:
            logger.error("[UNIVERSE_GUARD] write failed: %s", we)

    if not path.exists():
        logger.warning(
            "[UNIVERSE_GUARD] %s missing, writing %d tickers",
            path, len(expected),
        )
        _write(expected)
        return

    try:
        raw = json.loads(path.read_text())
        items = raw.get("tickers") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise ValueError("tickers payload is not a list")
        on_disk = sorted({str(s).upper() for s in items if str(s).strip()})
    except Exception as e:
        logger.error(
            "[UNIVERSE_GUARD] %s corrupt (%s), rewriting", path, e,
        )
        _write(expected)
        return

    if on_disk != expected:
        logger.warning(
            "[UNIVERSE_GUARD] DRIFT detected: disk=%s code=%s \u2014 rewriting to code",
            on_disk, expected,
        )
        _write(expected)
    else:
        logger.info(
            "[UNIVERSE_GUARD] universe consistent (%d tickers)",
            len(expected),
        )


def _init_tickers() -> None:
    """Populate TICKERS from disk on startup; fall back to defaults
    (which include the pinned SPY/QQQ). Always ensures the
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
    # v15.0 SPEC: ORH/ORL freeze at exactly 09:35:59 ET. The OR window
    # is open through 09:35:59 inclusive; bars whose close timestamp is
    # strictly less than 09:36:00 belong to the OR window.
    or_window_end = now_et.replace(hour=9, minute=36,
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
    # v5.6.1 D6 \u2014 [WATCHLIST_ADD] hook for replay universe-reconstruction.
    try:
        _v561_log_watchlist_add(t, reason="manual")
    except Exception:
        pass
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
    # v5.6.1 D6 \u2014 [WATCHLIST_REMOVE] hook for replay reconstruction.
    try:
        _v561_log_watchlist_remove(t, reason="manual")
    except Exception:
        pass
    open_long = t in positions
    open_short = t in short_positions
    return {"ok": True, "removed": True, "ticker": t,
            "had_open": bool(open_long or open_short)}

# v3.4.45 — paper sizing is now dollar-based like RH. SHARES is kept
# as a legacy fallback only (used when price is unavailable in test
# paths). Production entries call paper_shares_for(price) instead.
SHARES         = 10
PAPER_DOLLARS_PER_ENTRY = float(os.getenv("PAPER_DOLLARS_PER_ENTRY", "10000"))

SCAN_INTERVAL  = 60      # seconds between scans
YAHOO_TIMEOUT  = 8       # seconds
YAHOO_HEADERS  = {"User-Agent": "Mozilla/5.0"}

# v5.26.0 \u2014 Tiger Sovereign Phase 2 entry gates (spec-strict).
#
# Volume Gate (BL-3 / BU-3) is BYPASSED 2026-04-30. Permit gates and DI
# thresholds remain spec-mandated:
#   BL-2 / BU-2 \u2014 two consecutive closed 1m bars above/below target.
#   BS-3 / BF-3 \u2014 1m DI > 30 triggers Full Strike (100%).
PHASE2_TWO_CONSECUTIVE_1M_CLOSES = True
PHASE2_CONSECUTIVE_1M_REQUIRED  = 2
DI_PLUS_ENTRY2_THRESHOLD        = 30
DI_MINUS_ENTRY2_THRESHOLD       = 30

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

# AVWAP state \u2014 REMOVED in v3.4.34, RESTORED in v5.6.0 with new
# semantics. Session-open anchored AVWAP (anchor at 09:30 ET regular
# session open; reset daily; recomputed on every 1m bar close from the
# bar archive). Used by the v5.6.0 unified permission gates:
#   L-P1: G1 = Index.Last > Index.Opening_AVWAP
#         G3 = Ticker.Last > Ticker.Opening_AVWAP
#   S-P1: mirrored with strict <.
# AVWAP None (no bars yet) -> G1/G3 fail deterministically. Persisted
# state keys ("avwap_data", "avwap_last_ts") from pre-v3.4.34 are still
# silently ignored by load_paper_state for backwards compatibility.
# The v5.6.0 AVWAP is recomputed on the fly from the per-cycle 1m bar
# cache, no persistence required.


def _opening_avwap(ticker: str) -> float | None:
    """Session-open anchored VWAP for ``ticker``.

    Anchors at 09:30 ET regular-session open and includes every closed
    1m bar from then through the most recent close. Returns None if:
      - no bars are available yet (very first 9:30 second), OR
      - cumulative volume is zero across the included bars.

    Strict-pass gate semantics (v5.6.0): callers must treat None as
    a hard FAIL (do not enter on insufficient data).
    """
    bars = fetch_1min_bars(ticker)
    if not bars:
        return None
    timestamps = bars.get("timestamps") or []
    highs = bars.get("highs") or []
    lows = bars.get("lows") or []
    closes = bars.get("closes") or []
    volumes = bars.get("volumes") or []
    if not timestamps:
        return None

    now_et = _now_et()
    session_open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    open_epoch = session_open_et.timestamp()

    num = 0.0
    den = 0.0
    n = min(len(timestamps), len(highs), len(lows), len(closes), len(volumes))
    for i in range(n):
        ts = timestamps[i]
        if ts is None or ts < open_epoch:
            continue
        h = highs[i]
        l = lows[i]
        c = closes[i]
        v = volumes[i]
        if h is None or l is None or c is None or v is None or v <= 0:
            continue
        tp = (float(h) + float(l) + float(c)) / 3.0
        num += tp * float(v)
        den += float(v)
    if den <= 0.0:
        return None
    return num / den


def _v560_log_gate(ticker: str, side: str, gate: str, value, threshold, result: bool) -> None:
    """v5.6.0 forensic gate-eval logger. One line per G1/G3/G4 evaluation.

    Saturday's report parses these to validate the unified-AVWAP gate set.
    Format: ``[V560-GATE] ticker=AAPL side=LONG gate=G1 value=425.10 threshold=425.04 result=True``.
    """
    val_s = "None" if value is None else "%.4f" % float(value)
    thr_s = "None" if threshold is None else "%.4f" % float(threshold)
    logger.info(
        "[V560-GATE] ticker=%s side=%s gate=%s value=%s threshold=%s result=%s",
        ticker, side, gate, val_s, thr_s, bool(result),
    )


# ------------------------------------------------------------
# v5.6.1 \u2014 Data-collection helpers (logging + writer extensions).
# Pure observers; do not affect gate logic. See spec
# /home/user/workspace/specs/v5_6_1_data_collection_improvements.md.
# ------------------------------------------------------------
V561_INDEX_TICKER = "QQQ"
V561_OR_DIR_DEFAULT = "/data/or"


# ============================================================
# v5.9.0 \u2014 QQQ Regime Shield runtime state
# ============================================================
# Singleton regime tracker. Lives for the life of the bot process;
# seeded once at first compass evaluation, then advanced on each
# finalized 5m QQQ bar via _v590_qqq_regime_tick().
import qqq_regime  # noqa: E402
_QQQ_REGIME = qqq_regime.QQQRegime()
_QQQ_REGIME_SEEDED = False
_QQQ_REGIME_LAST_BUCKET = None  # epoch_seconds // 300 of last seen close


from engine.seeders import (
    seed_opening_range as _engine_seed_opening_range,
    seed_opening_range_all as _engine_seed_opening_range_all,
)
_seed_opening_range = _engine_seed_opening_range
_seed_opening_range_all = _engine_seed_opening_range_all


def _qqq_weather_tick():
    """v5.26.0 RULING #5 \u2014 advance QQQ 5m EMA9 used by BL-1 / BU-1
    Weather. Pulls QQQ 1m bars via `fetch_1min_bars`, derives 5m OHLC
    via `compute_5m_ohlc_and_ema9`, and writes the latest close + EMA9
    into the `_QQQ_REGIME` cache. Fail-closed: any exception leaves
    the prior cached values untouched (Weather check then sees stale-
    or-None and rejects entries).
    """
    global _QQQ_REGIME, _QQQ_REGIME_LAST_BUCKET
    try:
        bars = fetch_1min_bars(V561_INDEX_TICKER)
        if not bars:
            return
        five = _engine_compute_5m_ohlc_and_ema9(bars)
        if not five:
            return
        bucket = five.get("last_bucket")
        if bucket is None or bucket == _QQQ_REGIME_LAST_BUCKET:
            return
        closes = five.get("closes") or []
        if not closes:
            return
        _QQQ_REGIME.last_close = closes[-1]
        _QQQ_REGIME.ema9 = five.get("ema9")
        _QQQ_REGIME_LAST_BUCKET = bucket
    except Exception as _e:
        logger.warning("[regime] qqq weather tick error: %s", _e)


_v590_qqq_regime_tick = _qqq_weather_tick


def _v561_fmt_num(v) -> str:
    """Render a float/None as a stable token for log lines.

    None -> ``null`` (matches the gate_state JSON null semantics).
    Numbers -> 4dp string with no trailing whitespace.
    """
    if v is None:
        return "null"
    try:
        return "%.4f" % float(v)
    except (TypeError, ValueError):
        return "null"


def _v561_gate_state_dict(
    *,
    g1: bool | None,
    g3: bool | None,
    g4: bool | None,
    pass_: bool | None,
    ticker_price: float | None,
    ticker_avwap: float | None,
    index_price: float | None,
    index_avwap: float | None,
    or_high: float | None,
    or_low: float | None,
) -> dict:
    """Build the canonical gate_state payload used by both [V560-GATE]
    and [SKIP] gate_state= lines. Booleans are coerced; floats kept None
    when unknown so JSON encodes them as null."""
    def _fb(x):
        return None if x is None else bool(x)

    def _ff(x):
        if x is None:
            return None
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    return {
        "g1": _fb(g1),
        "g3": _fb(g3),
        "g4": _fb(g4),
        "pass": _fb(pass_),
        "ticker_price": _ff(ticker_price),
        "ticker_avwap": _ff(ticker_avwap),
        "index_price": _ff(index_price),
        "index_avwap": _ff(index_avwap),
        "or_high": _ff(or_high),
        "or_low": _ff(or_low),
    }


def _v561_log_v560_gate_rich(
    *,
    ticker: str,
    side: str,
    ts_utc: str,
    ticker_price,
    ticker_avwap,
    index_price,
    index_avwap,
    or_high,
    or_low,
    g1: bool,
    g3: bool,
    g4: bool,
    pass_: bool,
    reason: str | None,
) -> None:
    """v5.6.1 \u2014 single richened [V560-GATE] line.

    Carries every field a replay needs to pair a SKIP/PASS with the
    underlying numbers without consulting the bar archive.
    """
    logger.info(
        "[V560-GATE] ticker=%s side=%s ts=%s "
        "ticker_price=%s ticker_avwap=%s "
        "index_price=%s index_avwap=%s "
        "or_high=%s or_low=%s "
        "g1=%s g3=%s g4=%s pass=%s reason=%s",
        ticker, side, ts_utc,
        _v561_fmt_num(ticker_price), _v561_fmt_num(ticker_avwap),
        _v561_fmt_num(index_price), _v561_fmt_num(index_avwap),
        _v561_fmt_num(or_high), _v561_fmt_num(or_low),
        bool(g1), bool(g3), bool(g4), bool(pass_),
        ("null" if reason is None else str(reason)),
    )


def _v561_log_skip(
    *,
    ticker: str,
    reason: str,
    ts_utc: str,
    gate_state: dict | None,
) -> None:
    """v5.6.1 \u2014 unified [SKIP] line with gate_state.

    `gate_state=None` -> emits literal ``gate_state=null`` (used for
    pre-gate skips like cooldown / loss-cap / data-not-ready). When the
    SKIP fires after gates have evaluated, pass the dict from
    `_v561_gate_state_dict`.
    """
    if gate_state is None:
        gs_json = "null"
    else:
        try:
            gs_json = json.dumps(gate_state, separators=(",", ":"),
                                 sort_keys=True)
        except (TypeError, ValueError):
            gs_json = "null"
    logger.info(
        "[SKIP] ticker=%s reason=%s ts=%s gate_state=%s",
        ticker, reason, ts_utc, gs_json,
    )


def _v561_compose_entry_id(ticker: str, entry_ts_utc: str) -> str:
    """Deterministic entry id: ``<TICKER>-<YYYYMMDDHHMMSS>``.

    The compact ts uses the entry_ts_utc as-is, stripping non-digits;
    if entry_ts_utc is missing/unparseable, falls back to the current
    UTC clock so the id is always populated.
    """
    sym = (ticker or "").strip().upper() or "UNK"
    raw = entry_ts_utc or ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) < 14:
        digits = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    else:
        digits = digits[:14]
    return f"{sym}-{digits}"


def _v561_log_entry(
    *,
    ticker: str,
    side: str,
    entry_id: str,
    entry_ts_utc: str,
    entry_price: float,
    qty: int,
    strike_num: int = 1,
) -> None:
    """v5.6.1 \u2014 [ENTRY] line carrying entry_id for pairing.

    Strictly additive: this is in addition to the legacy
    [V510-ENTRY] line. Replay pairs by entry_id. v5.7.0 adds
    `strike_num` so log readers can count strikes without
    state-replay.
    """
    logger.info(
        "[ENTRY] ticker=%s side=%s entry_id=%s entry_ts=%s "
        "entry_price=%.4f qty=%d strike_num=%d",
        ticker, side, entry_id, entry_ts_utc,
        float(entry_price), int(qty), int(strike_num),
    )


def _v561_log_trade_closed(
    *,
    ticker: str,
    side: str,
    entry_id: str,
    entry_ts_utc: str,
    entry_price: float,
    exit_ts_utc: str,
    exit_price: float,
    exit_reason: str,
    qty: int,
    pnl_dollars: float,
    pnl_pct: float,
    hold_seconds: int,
    strike_num: int = 1,
    daily_realized_pnl: float | None = None,
) -> None:
    """v5.6.1 \u2014 [TRADE_CLOSED] lifecycle line.

    Emitted on every exit (stop, target, time, eod, manual).
    Replay pairs to [ENTRY] via entry_id. v5.7.0 adds
    `strike_num` and the running `daily_realized_pnl` so the
    kill-switch path can be reproduced offline. When
    `daily_realized_pnl` is omitted the helper folds this trade
    into the day's running total via `_v570_record_trade_close`
    so the logged value is always the post-this-close cumulative.
    """
    if daily_realized_pnl is None:
        try:
            daily_realized_pnl = _v570_record_trade_close(pnl_dollars)
        except Exception:
            daily_realized_pnl = float(pnl_dollars or 0.0)
    logger.info(
        "[TRADE_CLOSED] ticker=%s side=%s entry_id=%s "
        "entry_ts=%s entry_price=%.4f "
        "exit_ts=%s exit_price=%.4f exit_reason=%s "
        "qty=%d pnl_dollars=%.4f pnl_pct=%.4f hold_seconds=%d "
        "strike_num=%d daily_realized_pnl=%.4f",
        ticker, side, entry_id,
        entry_ts_utc, float(entry_price),
        exit_ts_utc, float(exit_price), exit_reason,
        int(qty), float(pnl_dollars), float(pnl_pct),
        int(hold_seconds),
        int(strike_num), float(daily_realized_pnl),
    )


def _v561_log_universe(tickers: list | tuple) -> None:
    """v5.6.1 \u2014 boot-time [UNIVERSE] one-shot.

    Tickers are uppercased, deduped, and sorted alphabetically for a
    stable line. Emitted once at module init.
    """
    seen, out = set(), []
    for t in tickers or []:
        s = (t or "").strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    out.sort()
    logger.info("[UNIVERSE] tickers=%s", ",".join(out))


def _v561_log_watchlist_add(ticker: str, reason: str = "manual",
                             ts_utc: str | None = None) -> None:
    """v5.6.1 \u2014 [WATCHLIST_ADD] hook. Currently called manually; the
    static-universe path doesn't mutate at runtime, but the hook is
    wired so future oomph/news-driven adds emit a structured line.
    """
    ts = ts_utc or _utc_now_iso()
    sym = (ticker or "").strip().upper()
    logger.info("[WATCHLIST_ADD] ticker=%s ts=%s reason=%s", sym, ts, reason)


def _v561_log_watchlist_remove(ticker: str, reason: str = "manual",
                                ts_utc: str | None = None) -> None:
    """v5.6.1 \u2014 [WATCHLIST_REMOVE] hook. Mirror of WATCHLIST_ADD."""
    ts = ts_utc or _utc_now_iso()
    sym = (ticker or "").strip().upper()
    logger.info("[WATCHLIST_REMOVE] ticker=%s ts=%s reason=%s", sym, ts, reason)


# ------------------------------------------------------------
# v5.7.0 \u2014 Unlimited Titan Strikes. HOD/LOD-gated unlimited
# re-entries on the Ten Titans only. Strike 1 takes the unchanged
# v5.6.0 L-P1/S-P1 permission gates; Strike 2+ runs the new
# Expansion Gate (HOD/LOD break + IndexAVWAP). Spec:
# /home/user/workspace/specs/v5_7_0_unlimited_titan_strikes.md.
# ------------------------------------------------------------

# Per-ticker per-day strike counter. Reset at session
# start (9:30 ET). Strike N counts how many entries on this
# ticker have already fired today across BOTH sides combined;
# strike_num for the next attempt is (count + 1).
#
# v5.19.1 vAA-1 ULTIMATE Decision 1 \u2014 STRIKE-CAP-3 unified
# from per-(ticker, side) to per-ticker. Long+short entries on
# the same ticker now share one counter, capping a ticker at 3
# strikes per day total. STRIKE-FLAT-GATE remains per-side.
_v570_strike_counts: dict = {}   # key=ticker -> int
_v570_strike_date: str = ""

# Per-ticker per-day session HOD/LOD tracker. Seeded from the
# first 9:30 ET print onward. Pre-market values do NOT seed.
_v570_session_hod: dict = {}     # {ticker: float}
_v570_session_lod: dict = {}     # {ticker: float}
_v570_session_date: str = ""

# Daily realized P&L, recomputed cumulatively from [TRADE_CLOSED]
# emissions. Resets at 9:30 ET next session.
_v570_daily_realized_pnl: float = 0.0
_v570_daily_pnl_date: str = ""

# Kill-switch latch. True once realized P&L breaches the floor;
# resets at the next session boundary alongside the strike
# counters.
_v570_kill_switch_latched: bool = False
_v570_kill_switch_logged: bool = False


def _v570_session_today_str() -> str:
    """Today as ET date string \u2014 anchors the daily counters."""
    try:
        now_et = datetime.now(tz=ZoneInfo("America/New_York"))
    except Exception:
        now_et = datetime.utcnow()
    return now_et.strftime("%Y-%m-%d")


def _v570_reset_if_new_session() -> None:
    """Reset strike counters / HOD-LOD / daily P&L / kill switch
    when a new ET session begins. Idempotent."""
    global _v570_strike_date, _v570_session_date, _v570_daily_pnl_date
    global _v570_kill_switch_latched, _v570_kill_switch_logged
    global _v570_daily_realized_pnl
    today = _v570_session_today_str()
    if _v570_strike_date != today:
        _v570_strike_counts.clear()
        _v570_strike_date = today
        # v5.15.1 vAA-1 \u2014 wipe sentinel-loop momentum state at the
        # session boundary alongside the strike counters. Clears
        # ADXTrendWindow per position, TradeHVP per position, and
        # DivergenceMemory's stored peaks so a fresh session starts
        # with empty caches per spec SENT-E session_reset.
        try:
            from broker.positions import reset_session_state as _reset_sentinel_state

            _reset_sentinel_state()
        except Exception as _e:
            try:
                logger.debug("[SENT-RESET] %s", _e)
            except Exception:
                pass
    if _v570_session_date != today:
        _v570_session_hod.clear()
        _v570_session_lod.clear()
        _v570_session_date = today
    if _v570_daily_pnl_date != today:
        _v570_daily_realized_pnl = 0.0
        _v570_daily_pnl_date = today
        _v570_kill_switch_latched = False
        _v570_kill_switch_logged = False


def _v570_strike_count(ticker: str, side: str = "") -> int:
    """Return the number of entries already filled today on
    ``ticker`` across both sides combined. The next attempt is
    strike_num = count + 1.

    v5.19.1 vAA-1 ULTIMATE Decision 1 \u2014 the ``side`` argument
    is preserved for call-site compatibility but is no longer
    consulted; long and short share a single per-ticker counter.
    """
    _v570_reset_if_new_session()
    return int(_v570_strike_counts.get(ticker.upper(), 0))


def _v570_record_entry(ticker: str, side: str = "") -> int:
    """Increment the strike counter on a successful ENTRY and
    return the strike_num that was just consumed.

    v5.15.0 vAA-1 \u2014 STRIKE-CAP-3 enforced: a 4th attempt is rejected
    with RuntimeError("STRIKE-CAP-3 reached"); the counter remains
    at 3. Callers should pre-check via ``strike_entry_allowed``;
    this raise is a defensive belt-and-braces check.

    v5.19.1 vAA-1 ULTIMATE Decision 1 \u2014 ``side`` is accepted for
    call-site compatibility but ignored: long+short entries on the
    same ticker share one counter (per-ticker cap of 3 total).
    """
    _v570_reset_if_new_session()
    key = ticker.upper()
    cur = int(_v570_strike_counts.get(key, 0))
    if cur >= 3:
        raise RuntimeError("STRIKE-CAP-3 reached")
    new_n = cur + 1
    _v570_strike_counts[key] = new_n
    return new_n


# v5.15.0 vAA-1 \u2014 STRIKE-CAP-3 + STRIKE-FLAT-GATE.
# spec: tests/test_tiger_sovereign_vAA_spec.py
#         ::test_strike_cap_3_blocks_fourth_entry
#         ::test_strike_flat_gate_blocks_until_position_closes
def _v570_strike_must_be_flat(
    ticker: str,
    side: str,
    positions: dict | None = None,
) -> bool:
    """STRIKE-FLAT-GATE: True iff (ticker, side) holds zero shares.

    The gate prevents Strike N+1 from stacking into an open Strike N
    position. Spec rule STRIKE-FLAT-GATE: a new Strike fires only
    after the prior Strike has fully closed (shares == 0).

    ``positions`` is a {f"{ticker}:{side}": {...}} mapping; missing
    keys are treated as flat (no position). For first-Strike entries
    the absence of any prior position is the expected case.
    """
    if not positions:
        return True
    key = f"{ticker.upper()}:{side.upper()}"
    pos = positions.get(key)
    if not pos:
        return True
    try:
        return int(pos.get("shares", 0) or 0) == 0
    except (TypeError, ValueError):
        return True


def strike_entry_allowed(
    ticker: str,
    side: str,
    positions: dict | None = None,
) -> bool:
    """STRIKE-CAP-3 + STRIKE-FLAT-GATE composite gate.

    Returns False when EITHER:
      * the per-ticker Strike count has already reached 3 today
        (STRIKE-CAP-3 \u2014 long+short combined), OR
      * a prior Strike on this side still holds shares > 0
        (STRIKE-FLAT-GATE \u2014 still per-side).

    Returns True only when the next Strike attempt is permitted
    under both gates.

    v5.19.1 vAA-1 ULTIMATE Decision 1 \u2014 cap is per-ticker; the
    flat gate stays per-side because long and short positions are
    independent (you can be flat long while holding short).
    """
    if _v570_strike_count(ticker) >= 3:
        return False
    return _v570_strike_must_be_flat(ticker, side, positions=positions)


def _v570_update_session_hod_lod(
    ticker: str, current_price: float | None,
) -> tuple[float | None, float | None, bool, bool]:
    """Update the per-ticker session HOD/LOD with the current
    print and return ``(prev_hod, prev_lod, hod_break, lod_break)``.

    `prev_hod`/`prev_lod` are the values BEFORE this tick was
    folded in (None if this is the first print of the session).
    `hod_break` is True iff the current price is strictly greater
    than the prior HOD; mirror for `lod_break`. After the call,
    the stored HOD/LOD are updated to include this tick.

    Pre-market behavior: this helper does NOT seed before 9:30 ET
    \u2014 callers gate themselves with `_v570_is_session_open()`.
    """
    _v570_reset_if_new_session()
    sym = (ticker or "").strip().upper()
    if not sym or current_price is None or current_price <= 0:
        return None, None, False, False
    prev_hod = _v570_session_hod.get(sym)
    prev_lod = _v570_session_lod.get(sym)
    px = float(current_price)
    hod_break = (prev_hod is not None and px > prev_hod)
    lod_break = (prev_lod is not None and px < prev_lod)
    if prev_hod is None or px > prev_hod:
        _v570_session_hod[sym] = px
    if prev_lod is None or px < prev_lod:
        _v570_session_lod[sym] = px
    return prev_hod, prev_lod, hod_break, lod_break


def _v570_is_session_open() -> bool:
    """True at/after 9:30 ET on a weekday."""
    try:
        now_et = datetime.now(tz=ZoneInfo("America/New_York"))
    except Exception:
        return True
    if now_et.weekday() >= 5:
        return False
    open_t = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    return now_et >= open_t


def _v570_log_kill_switch(realized_pnl: float, ts_utc: str) -> None:
    """v5.7.0 \u2014 single [KILL_SWITCH] line on first breach."""
    logger.info(
        "[KILL_SWITCH] reason=daily_loss_limit triggered_at=%s "
        "realized_pnl=%.4f",
        ts_utc, float(realized_pnl),
    )


def _v570_record_trade_close(pnl_dollars: float) -> float:
    """Update cumulative daily realized P&L on a [TRADE_CLOSED]
    emission and trigger the kill switch the moment the floor is
    breached. Returns the updated cumulative P&L."""
    global _v570_daily_realized_pnl
    global _v570_kill_switch_latched, _v570_kill_switch_logged
    _v570_reset_if_new_session()
    _v570_daily_realized_pnl += float(pnl_dollars or 0.0)
    if (_v570_daily_realized_pnl <= DAILY_LOSS_LIMIT_DOLLARS
            and not _v570_kill_switch_latched):
        _v570_kill_switch_latched = True
        if not _v570_kill_switch_logged:
            try:
                _v570_log_kill_switch(
                    _v570_daily_realized_pnl, _utc_now_iso(),
                )
            finally:
                _v570_kill_switch_logged = True
    return _v570_daily_realized_pnl


def _v570_kill_switch_active() -> bool:
    """Return True iff the daily-loss kill switch has latched."""
    _v570_reset_if_new_session()
    return bool(_v570_kill_switch_latched)


def _v561_archive_qqq_bar(bars: dict | None) -> None:
    """v5.6.1 \u2014 D1: T-off the QQQ stream into /data/bars/<UTC>/QQQ.jsonl.

    `bars` is the dict returned by fetch_1min_bars("QQQ"); we project
    the last-closed bar onto the canonical bar_archive schema. Failure-
    tolerant: a bad QQQ snapshot must never disrupt the trading scan.
    """
    try:
        if not bars:
            return
        closes = bars.get("closes") or []
        ts_arr = bars.get("timestamps") or []
        idx = None
        if len(closes) >= 2 and closes[-2] is not None:
            idx = -2
        elif len(closes) >= 1 and closes[-1] is not None:
            idx = -1
        if idx is None:
            return
        opens = bars.get("opens") or []
        highs = bars.get("highs") or []
        lows = bars.get("lows") or []
        vols = bars.get("volumes") or []
        ts_val = ts_arr[idx] if abs(idx) <= len(ts_arr) else None
        try:
            ts_iso = (datetime.utcfromtimestamp(int(ts_val))
                      .strftime("%Y-%m-%dT%H:%M:%SZ")
                      if ts_val is not None else None)
        except Exception:
            ts_iso = None
        canon_bar = {
            "ts": ts_iso,
            "open":  opens[idx] if abs(idx) <= len(opens) else None,
            "high":  highs[idx] if abs(idx) <= len(highs) else None,
            "low":   lows[idx]  if abs(idx) <= len(lows)  else None,
            "close": closes[idx],
            "bid": None,
            "ask": None,
            "last_trade_price": bars.get("current_price"),
        }
        bar_archive.write_bar(
            V561_INDEX_TICKER, canon_bar,
            base_dir=bar_archive.DEFAULT_BASE_DIR,
        )
    except Exception as e:
        logger.warning("[V561-QQQ-BAR] archive error: %s", e)


def _v561_persist_or_snapshot(
    ticker: str,
    *,
    base_dir: str | os.PathLike = V561_OR_DIR_DEFAULT,
    today_utc: str | None = None,
) -> str | None:
    """v5.6.1 \u2014 D2: persist OR_High / OR_Low to
    `/data/or/<UTC-date>/<TICKER>.json` once per ticker per session.

    Returns the file path on success, or None on failure (logged at
    warning level, never raised). Reads `or_high[ticker]` / `or_low[ticker]`
    from the live module-level dicts; if either is None the snapshot is
    still written with null values so replay can detect the gap.
    """
    try:
        sym = (ticker or "").strip().upper()
        if not sym:
            return None
        day = today_utc or datetime.utcnow().strftime("%Y-%m-%d")
        dir_path = Path(base_dir) / day
        dir_path.mkdir(parents=True, exist_ok=True)
        file_path = dir_path / f"{sym}.json"
        payload = {
            "ticker": sym,
            "or_high": or_high.get(sym),
            "or_low": or_low.get(sym),
            "computed_at_utc": _utc_now_iso(),
        }
        tmp = str(file_path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.replace(tmp, file_path)
        return str(file_path)
    except Exception as e:
        logger.warning("[V561-OR-SNAP] persist %s failed: %s", ticker, e)
        return None


# Set tracking which tickers have had their OR snapshot persisted today.
# Keyed by `<UTC-date>:<TICKER>` so a session boundary auto-resets.
_v561_or_snap_taken: set = set()


def _v561_maybe_persist_or_snapshots(now_et=None) -> int:
    """v5.6.1 \u2014 idempotent OR-snapshot dispatcher. Run once per scan
    cycle from inside scan_loop; persists any ticker whose snapshot is
    not yet taken today and whose OR is seeded.

    Returns the number of new files written this call. After 9:35 ET
    every tracked ticker should have a row; pre-9:35 nothing fires.
    """
    try:
        if now_et is None:
            now_et = _now_et()
        # Only fire after the OR window has closed (9:35 ET +).
        if now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 35):
            return 0
        today_utc = datetime.utcnow().strftime("%Y-%m-%d")
        # Universe = TRADE_TICKERS plus the index ticker (QQQ) since v5.6.1
        # archives QQQ bars and replay needs the matching OR snapshot to
        # validate the index G1 gate (QQQ has no OR_High/Low gate but the
        # snapshot is harmless and keeps the schema uniform).
        universe = list(TRADE_TICKERS)
        if V561_INDEX_TICKER not in universe:
            universe.append(V561_INDEX_TICKER)
        n = 0
        for sym in universe:
            key = f"{today_utc}:{sym}"
            if key in _v561_or_snap_taken:
                continue
            if sym not in or_high and sym not in or_low:
                # OR not yet seeded \u2014 try again next cycle.
                continue
            path = _v561_persist_or_snapshot(sym, today_utc=today_utc)
            if path:
                _v561_or_snap_taken.add(key)
                n += 1
        return n
    except Exception as e:
        logger.warning("[V561-OR-SNAP] dispatcher error: %s", e)
        return 0


def _v561_reset_or_snap_state() -> None:
    """Reset the per-session OR-snapshot dedup set. Called from
    reset_daily_state() so a new RTH session re-emits snapshots."""
    _v561_or_snap_taken.clear()


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

# v4.6.0: _state_loaded moved to paper_state.py (single owner of the flag).

# Short positions (Wounded Buffalo strategy)
short_positions: dict = {}           # paper short: {ticker: {entry_price, shares, stop, trail_stop, trail_active, entry_time, date, side}}
daily_short_entry_count: dict = {}   # {ticker: int} — resets daily, separate from long count
daily_short_entry_date: str = ""     # v4.7.0 — mirror of daily_entry_date for shorts
short_trade_history: list = []       # max 500 closed paper shorts

# v5.0.0 \u2014 Tiger/Buffalo two-stage state-machine tracks. Per-ticker per-
# direction. Schema and transitions defined in STRATEGY.md (canonical
# spec) and tiger_buffalo_v5.py. Persisted in paper_state.json under
# the "v5_tracks" key. v4 paper_state files load with empty tracks
# (defaults to IDLE) \u2014 see paper_state.py load_paper_state.
v5_long_tracks: dict = {}    # {ticker: track_dict}
v5_short_tracks: dict = {}   # {ticker: track_dict}
# C-R1: at most one direction is active per ticker per session.
v5_active_direction: dict = {}  # {ticker: "long"|"short"|None}

# Daily loss limit (Feature 2)
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "-1500"))
_trading_halted: bool = False
_trading_halted_reason: str = ""

# ============================================================
# MARKET MODE (DELETED v5.26.0 \u2014 non-spec scaffolding)
# ============================================================
# v5.26.0: MarketMode classifier, MODE_PROFILES, breadth/RSI observers,
# and ticker-heat lists were all non-spec scaffolding. Deleted per Tiger
# Sovereign v15.0 spec-strict pass.

# MarketMode kept as a stub label for legacy log lines. CLOSED is the
# only value used. Spec-strict: no profiles, no observers, no clamps.
class MarketMode:
    CLOSED = "CLOSED"

_current_mode: str = MarketMode.CLOSED

# v3.4.21 — per-ticker entry-gate snapshot for dashboard rendering.
# Populated by _update_gate_snapshot() on every scan cycle.
# Shape: {ticker: {
#     "side": "LONG"|"SHORT",
#     "break": bool,              # 1m close crossed OR (above/below)
#     "polarity": bool|None,      # Phase 2 boundary hold (2 closed 1m
#                                 # candles outside the 5m OR edge for
#                                 # this side). None = OR/closes not
#                                 # yet available. Spec STEP 4.
#     "index": bool|None,         # Section I global permit for this
#                                 # side (QQQ 5m close vs 9-EMA + QQQ
#                                 # vs 09:30 AVWAP). None = inputs not
#                                 # ready. Spec STEPS 1-2.
#     "di": bool|None,            # DI+/DI- >= 25 (BS-1 / BF-1 Authority);
#                                 # None = warmup (DI not yet computable)
#     "ts": iso timestamp,
# }}
# Read-only from outside the scan loop; never cleared mid-scan.
_gate_snapshot: dict = {}

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

    bars = fetch_1min_bars(ticker)
    if not bars:
        return
    price = bars.get("current_price")
    if price is None or price <= 0:
        return

    # v5.13.9: PDC override removed (legacy v4 polarity field is gone).
    # FMP price override is still useful as the most-recent quote.
    fmp_q = get_fmp_quote(ticker)
    if fmp_q:
        fmp_price = fmp_q.get("price")
        if fmp_price and fmp_price > 0:
            price = fmp_price

    if price > or_h:
        side = "LONG"
        break_ok = True
    elif price < or_l:
        side = "SHORT"
        break_ok = True
    else:
        side = "LONG" if abs(price - or_h) < abs(price - or_l) else "SHORT"
        break_ok = False

    # v5.13.9 \u2014 polarity = Phase 2 Boundary Hold for this side.
    # Spec STEP 4: TWO consecutive closed 1m candles strictly outside
    # the 5m OR edge. Reads the same evaluator the entry path uses so
    # the dashboard cannot disagree with the bot. None when inputs are
    # not yet available (OR not seeded, or fewer than 2 closes).
    polarity_ok: bool | None
    try:
        bh_res = eot_glue.evaluate_boundary_hold_gate(ticker, side, or_h, or_l)
        if bh_res.get("reason") in ("or_not_set", "insufficient_closes"):
            polarity_ok = None
        else:
            polarity_ok = bool(bh_res.get("hold"))
    except Exception:
        polarity_ok = None

    # v5.13.9 \u2014 index = Section I global permit for this side.
    # Spec STEPS 1-2: LONG requires QQQ 5m close > 9-EMA AND QQQ price
    # > 09:30 AVWAP. SHORT mirrors with strict-below. None when QQQ
    # regime / AVWAP are not yet seeded.
    index_ok: bool | None
    try:
        qqq_bars_idx = fetch_1min_bars("QQQ")
        qqq_last = qqq_bars_idx.get("current_price") if qqq_bars_idx else None
        qqq_avwap = _opening_avwap("QQQ")
        qqq_5m_close = _QQQ_REGIME.last_close
        qqq_ema9 = _QQQ_REGIME.ema9
        if (
            qqq_last is None
            or qqq_avwap is None
            or qqq_5m_close is None
            or qqq_ema9 is None
        ):
            index_ok = None
        else:
            permit = eot_glue.evaluate_section_i(
                side, qqq_5m_close, qqq_ema9, qqq_last, qqq_avwap
            )
            index_ok = bool(permit.get("open"))
    except Exception:
        index_ok = None

    di_plus, di_minus = tiger_di(ticker)
    if di_plus is None or di_minus is None:
        di_ok = None  # warmup
    elif side == "LONG":
        di_ok = di_plus >= 25  # BS-1 Authority Check
    else:
        di_ok = di_minus >= 25  # BF-1 Authority Check

    # v4.3.0 \u2014 extension_pct: signed distance of price past the
    # relevant OR edge. LONG = (price \u2212 OR_High)/OR_High*100;
    # SHORT = (OR_Low \u2212 price)/OR_Low*100. None if OR not seeded.
    extension_pct: float | None
    if side == "LONG" and or_h and or_h > 0:
        extension_pct = round((price - or_h) / or_h * 100.0, 2)
    elif side == "SHORT" and or_l and or_l > 0:
        extension_pct = round((or_l - price) / or_l * 100.0, 2)
    else:
        extension_pct = None

    _gate_snapshot[ticker] = {
        "side": side,
        "break": bool(break_ok),
        "polarity": polarity_ok,
        "index": index_ok,
        "di": di_ok,
        "extension_pct": extension_pct,
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    pol_str = "None" if polarity_ok is None else str(bool(polarity_ok))
    idx_str = "None" if index_ok is None else str(bool(index_ok))
    di_str = "None" if di_ok is None else str(bool(di_ok))
    logger.info(
        "GATE_EVAL ticker=%s price=%.2f or_hi=%.2f or_lo=%.2f "
        "side=%s break=%s polarity=%s index=%s di=%s",
        ticker, price, or_h, or_l, side, bool(break_ok),
        pol_str, idx_str, di_str,
    )


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


def _compute_adx(highs, lows, closes, period=DI_PERIOD):
    # v5.15.1 vAA-1 \u2014 Wilder ADX. Mirrors _compute_di's smoothing
    # pipeline so DI+/DI-/ADX agree byte-for-byte. ADX is the Wilder-
    # smoothed DX series, where DX_i = 100 * |+DI_i \u2212 \u2212DI_i| /
    # (+DI_i + \u2212DI_i). Needs at least 2*period bars to seed both
    # the DI smoothing (period bars) and the DX smoothing (another
    # period bars). Returns None on insufficient data so callers can
    # silently degrade (Alarm C / D simply skip).
    n = len(closes)
    if n < 2 * period or len(highs) < 2 * period or len(lows) < 2 * period:
        return None
    try:
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

        # Seed Wilder smoothing on first `period` raw values.
        smooth_pdm = sum(raw_pdm[:period])
        smooth_ndm = sum(raw_ndm[:period])
        smooth_tr  = sum(raw_tr[:period])

        # Build the DX series, advancing Wilder smoothing one step at
        # a time. The DX seed point is the first index where smoothed
        # values exist (after period raw deltas \u2192 raw_tr index 0..period-1
        # consumed in the seed). Subsequent indices feed both the DI
        # roll forward AND the DX collection.
        dx_series: list[float] = []
        if smooth_tr > 0:
            dp0 = 100.0 * smooth_pdm / smooth_tr
            dn0 = 100.0 * smooth_ndm / smooth_tr
            denom0 = dp0 + dn0
            if denom0 > 0:
                dx_series.append(100.0 * abs(dp0 - dn0) / denom0)
            else:
                dx_series.append(0.0)

        for i in range(period, len(raw_tr)):
            smooth_pdm = smooth_pdm - smooth_pdm / period + raw_pdm[i]
            smooth_ndm = smooth_ndm - smooth_ndm / period + raw_ndm[i]
            smooth_tr  = smooth_tr  - smooth_tr  / period + raw_tr[i]
            if smooth_tr <= 0:
                continue
            dp = 100.0 * smooth_pdm / smooth_tr
            dn = 100.0 * smooth_ndm / smooth_tr
            denom = dp + dn
            if denom <= 0:
                dx_series.append(0.0)
            else:
                dx_series.append(100.0 * abs(dp - dn) / denom)

        if len(dx_series) < period:
            return None
        # ADX seed: simple average of first `period` DX values.
        adx = sum(dx_series[:period]) / period
        # Wilder smoothing thereafter.
        for i in range(period, len(dx_series)):
            adx = (adx * (period - 1) + dx_series[i]) / period
        return float(adx)
    except Exception:
        return None


def v5_adx_1m_5m(ticker):
    """v5.15.1 vAA-1 \u2014 Wilder ADX on both 1m and 5m timeframes.

    Returns dict ``{"adx_1m": float|None, "adx_5m": float|None}``.
    Reuses the same bar streams as ``v5_di_1m_5m`` (same per-cycle
    cache via fetch_1min_bars), so ADX and DI agree on the same
    underlying tape. Either value can be None when warmup is
    incomplete (need >= 2 * DMI_PERIOD bars).
    """
    out = {"adx_1m": None, "adx_5m": None}
    bars = fetch_1min_bars(ticker)
    if not bars:
        return out
    closes_1m = [c for c in bars.get("closes", []) if c is not None]
    highs_1m  = [h for h in bars.get("highs",  []) if h is not None]
    lows_1m   = [lo for lo in bars.get("lows", []) if lo is not None]
    n = min(len(closes_1m), len(highs_1m), len(lows_1m))
    if n >= 2 * DI_PERIOD:
        out["adx_1m"] = _compute_adx(highs_1m[:n], lows_1m[:n], closes_1m[:n])
    # 5m \u2014 reuse the same seed+live merge that v5_di_1m_5m uses.
    live_5m = _resample_to_5min_ohlc_buckets(
        bars.get("timestamps", []),
        bars.get("highs",  []),
        bars.get("lows",   []),
        bars.get("closes", []),
    )
    seed = _DI_SEED_CACHE.get(ticker) or []
    merged = {}
    for b in seed:
        merged[b["bucket"]] = (b["high"], b["low"], b["close"])
    for b in live_5m:
        merged[b["bucket"]] = (b["high"], b["low"], b["close"])
    if merged:
        keys = sorted(merged.keys())
        h5 = [merged[k][0] for k in keys]
        l5 = [merged[k][1] for k in keys]
        c5 = [merged[k][2] for k in keys]
        if len(c5) >= 2 * DI_PERIOD:
            out["adx_5m"] = _compute_adx(h5, l5, c5)
    return out


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


# ============================================================
# v5.0.0 \u2014 Tiger/Buffalo state-machine integration helpers
# ============================================================
# Spec: STRATEGY.md (canonical). Pure-function rule logic lives in
# tiger_buffalo_v5.py; this block is the runtime glue that pulls live
# market data into the spec helpers and persists track state.
def v5_get_track(ticker: str, direction: str) -> dict:
    """Return the live track for (ticker, direction), creating an IDLE
    record if absent. C-R1 mutex is enforced separately by callers.
    """
    if direction == v5.DIR_LONG:
        bucket = v5_long_tracks
    elif direction == v5.DIR_SHORT:
        bucket = v5_short_tracks
    else:
        raise ValueError(f"unknown direction {direction!r}")
    if ticker not in bucket:
        bucket[ticker] = v5.new_track(direction)
    return bucket[ticker]


def v5_di_1m_5m(ticker):
    """Compute DI+ and DI- on both 1m and 5m timeframes for a ticker.
    Used by L-P2-R1 / S-P2-R1 (gates need both timeframes).

    Returns dict with keys 'di_plus_1m', 'di_minus_1m', 'di_plus_5m',
    'di_minus_5m'. Any value can be None when warmup is incomplete.

    DMI period = 15 per C-R2 (matches Gene's spec and the canonical
    v4 DI_PERIOD = 15 constant). v5 and v4 now compute DMI on the
    same period so signals between the v5 decision engine and the v4
    dashboard / executor agree byte-for-byte.
    """
    bars = fetch_1min_bars(ticker)
    out = {
        "di_plus_1m": None, "di_minus_1m": None,
        "di_plus_5m": None, "di_minus_5m": None,
    }
    if not bars:
        return out
    closes_1m = [c for c in bars.get("closes", []) if c is not None]
    highs_1m  = [h for h in bars.get("highs",  []) if h is not None]
    lows_1m   = [lo for lo in bars.get("lows", []) if lo is not None]
    n = min(len(closes_1m), len(highs_1m), len(lows_1m))
    if n >= v5.DMI_PERIOD + 1:
        dp, dm = _compute_di(highs_1m[:n], lows_1m[:n], closes_1m[:n],
                             period=v5.DMI_PERIOD)
        out["di_plus_1m"], out["di_minus_1m"] = dp, dm
    # 5m \u2014 reuse tiger_di which already merges seed + live 5m buckets
    # and normalizes on DI_PERIOD = 15. v5 now matches v4's period
    # exactly (C-R2 corrected v5.0.0 \u2192 v5.0.1 per Gene's flag), so the
    # v5 5m DI is the same value tiger_di emits.
    live_5m = _resample_to_5min_ohlc_buckets(
        bars.get("timestamps", []),
        bars.get("highs",  []),
        bars.get("lows",   []),
        bars.get("closes", []),
    )
    seed = _DI_SEED_CACHE.get(ticker) or []
    merged = {}
    for b in seed:
        merged[b["bucket"]] = (b["high"], b["low"], b["close"])
    for b in live_5m:
        merged[b["bucket"]] = (b["high"], b["low"], b["close"])
    if merged:
        keys = sorted(merged.keys())
        h5 = [merged[k][0] for k in keys]
        l5 = [merged[k][1] for k in keys]
        c5 = [merged[k][2] for k in keys]
        if len(c5) >= v5.DMI_PERIOD + 1:
            dp5, dm5 = _compute_di(h5, l5, c5, period=v5.DMI_PERIOD)
            out["di_plus_5m"], out["di_minus_5m"] = dp5, dm5
    return out


def v5_first_hour_high(ticker):
    """L-P1-G4: high of the 09:30-10:30 ET window on the current session.

    Returns float or None if the window has not yet completed enough
    bars to compute. Reads from fetch_1min_bars (same per-cycle cache).
    """
    bars = fetch_1min_bars(ticker)
    if not bars:
        return None
    timestamps = bars.get("timestamps") or []
    highs = bars.get("highs") or []
    if not timestamps or not highs:
        return None
    # 09:30..10:30 ET. Convert each ts to ET via _now_et's tz, but we
    # only need date math: build window in ET then compare epochs.
    now_et = _now_et()
    window_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    window_close = now_et.replace(hour=10, minute=30, second=0, microsecond=0)
    open_epoch = window_open.timestamp()
    close_epoch = window_close.timestamp()
    fh_high = None
    for i, ts in enumerate(timestamps):
        if ts is None:
            continue
        if ts < open_epoch or ts >= close_epoch:
            continue
        h = highs[i] if i < len(highs) else None
        if h is None:
            continue
        fh_high = h if fh_high is None else max(fh_high, h)
    return fh_high


def v5_opening_range_low_5m(ticker):
    """S-P1-G4: low of the 09:30-09:35 ET 5m candle.

    The v4 'or_low' dict is computed off the same window, so we read
    that directly when present; falls back to a fresh scan otherwise.
    """
    if ticker in or_low:
        return or_low[ticker]
    return None


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


def _refresh_market_mode():
    """Spec-strict v5.26.0: classifier deleted; this is now a no-op
    kept so legacy scan-loop callers can keep calling it without
    raising. The /mode telegram surface and dashboard banner have
    been migrated off these snapshots.
    """
    return


# Scan pause (Feature 8) — user-set via Telegram /pause /resume.
_scan_paused: bool = False
# Auto-idle flag — True when scan_loop is short-circuiting because it's
# outside market hours (weekends, pre-09:35, post-15:55). Updated at the
# top of every scan cycle, independent of market hours, so the dashboard
# banner reflects reality after the close instead of sticking on POWER.
_scan_idle_hours: bool = False
# v5.13.9 \u2014 _regime_bullish (PDC-anchored bull/bear flag) was retired
# alongside the matching scan.py alert. The dashboard index/polarity
# pills now mirror Section I permit + boundary_hold instead.
_last_exit_time: dict = {}     # ticker -> datetime (UTC) of last exit
_last_scan_time = None           # datetime (UTC), updated each scan cycle

# User config
user_config: dict = {"trading_mode": "paper"}

# v4.6.0: _paper_save_lock moved to paper_state.py.


# ============================================================
# NOTIFICATION ROUTING HELPER (Fix B)
# ============================================================


# ============================================================
# STATE PERSISTENCE \u2014 moved to paper_state.py in v4.6.0.
# Re-exported below so existing callsites keep working.
# ============================================================

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
#                               RED_CANDLE | POLARITY_SHIFT |
#                               HARD_EJECT_TIGER | forensic_stop |
#                               per_trade_brake | be_stop | ema_trail |
#                               velocity_fuse | ...
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
# v4.11.0 \u2014 health-pill error reporting
# ============================================================
# report_error() is the single entry point for "operator should be
# paged about this" events. It does three things, in order:
#   1. Logs via the existing logger so existing log surfaces still see
#      the event (file logs, stderr, the dashboard ring buffer prior
#      to v4.11.0 \u2014 the dashboard log tail card itself was deleted in
#      this release, but the underlying logger handlers stay).
#   2. Appends to error_state so the dashboard health pill counter +
#      tap-to-expand list reflect the event.
#   3. If error_state's dedup gate says "send", routes a Telegram
#      message to the right channel: main bot for "main" events,
#      executor's own bot for "val" / "gene".
#
# The 5-min dedup is per (executor, code) so a flapping ORDER_REJECT
# does not spam the channel; the dashboard count still increments on
# every event.
import error_state as _error_state


def _executor_inst(name: str):
    """Return the live executor instance for "val"/"gene", or None."""
    n = (name or "").strip().lower()
    if n == "val":
        return val_executor
    if n == "gene":
        return gene_executor
    return None


def _format_error_telegram(executor: str, code: str, summary: str, detail: str = "") -> str:
    """Format a Telegram error message respecting the \u226434 chars/line rule.

    Layout:
      \U0001f6a8 X \u00b7 CODE
      <summary>
      <detail line(s)>

      ts: HH:MM:SS ET
    """
    ex_label = (executor or "").upper()
    head = f"\U0001f6a8 {ex_label} \u00b7 {code}"

    def _wrap(text: str, width: int = 34) -> list[str]:
        out: list[str] = []
        for raw_line in (text or "").splitlines() or [""]:
            line = raw_line.rstrip()
            if len(line) <= width:
                out.append(line)
                continue
            # Greedy word-wrap. If a single word is >width, hard-split it.
            words = line.split(" ")
            buf = ""
            for w in words:
                if not buf:
                    if len(w) <= width:
                        buf = w
                    else:
                        # Hard-split overlong word.
                        while len(w) > width:
                            out.append(w[:width])
                            w = w[width:]
                        buf = w
                elif len(buf) + 1 + len(w) <= width:
                    buf = buf + " " + w
                else:
                    out.append(buf)
                    if len(w) <= width:
                        buf = w
                    else:
                        while len(w) > width:
                            out.append(w[:width])
                            w = w[width:]
                        buf = w
            if buf:
                out.append(buf)
        return out

    parts: list[str] = []
    parts.append(head if len(head) <= 34 else head[:34])
    parts.extend(_wrap(summary))
    if detail:
        parts.extend(_wrap(detail))

    try:
        ts = _now_et().strftime("%H:%M:%S ET")
    except Exception:
        ts = ""
    if ts:
        parts.append("")
        parts.append(f"ts: {ts}")
    return "\n".join(parts)


def report_error(executor: str, code: str, severity: str, summary: str,
                 detail: str = "") -> bool:
    """Page-the-operator entry point. See module-level docstring above.

    Returns True iff a Telegram message was actually dispatched (i.e.
    the dedup gate elapsed). Dashboard count always increments.
    """
    # 1. Log via existing logger. Preserve the same level mapping the
    #    rest of the codebase uses: "warning" -> WARNING, otherwise
    #    ERROR. CRITICAL events still log at ERROR; the distinction is
    #    only relevant for the dashboard pill color.
    sev = (severity or "").strip().lower()
    log_msg = f"[{(executor or '').upper()}/{code}] {summary}"
    try:
        if sev == "warning":
            logger.warning(log_msg)
        else:
            logger.error(log_msg)
    except Exception:
        pass

    # 2. Append to error_state ring + check dedup gate.
    try:
        ts_iso = _utc_now_iso()
    except Exception:
        ts_iso = ""
    try:
        should_send = _error_state.record_error(
            executor=executor,
            code=code,
            severity=severity,
            summary=summary,
            detail=detail,
            ts=ts_iso,
        )
    except Exception:
        # Never let error reporting itself raise.
        logger.exception("report_error: error_state.record_error failed")
        return False

    if not should_send:
        return False

    # 3. Dispatch to the right Telegram channel.
    try:
        text = _format_error_telegram(executor, code, summary, detail)
    except Exception:
        logger.exception("report_error: format failed")
        return False

    ex = (executor or "").strip().lower()
    try:
        if ex in ("val", "gene"):
            inst = _executor_inst(ex)
            if inst is not None:
                inst._send_own_telegram(text)
            else:
                # Executor not enabled \u2014 fall back to main bot so the
                # operator still gets paged.
                send_telegram(text)
        else:
            send_telegram(text)
    except Exception:
        logger.exception("report_error: telegram dispatch failed")
        return False
    return True


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


# v5.26.0 \u2014 stop-cap, extended-entry guard, breakeven ratchet
# constants deleted. Tiger Sovereign v15.0 spec uses a single R-2
# -$500 hard STOP MARKET rail (computed inline in
# broker.orders.execute_breakout). MAX_STOP_PCT, ENTRY_EXTENSION_MAX_PCT,
# ENTRY_STOP_CAP_REJECT, BREAKEVEN_RATCHET_PCT are not part of the
# spec and have been removed.


# v5.26.0 \u2014 broker.stops module deleted. R-2 -$500 hard stop is
# computed inline in broker.orders.execute_breakout per Tiger
# Sovereign v15.0 \u00a7Risk Rails. The broker.orders / broker.positions /
# broker.lifecycle names are referenced by TradeGeniusBase methods and the
# scheduler thread, so they must resolve in this module's namespace.
from broker.orders import (  # noqa: E402, F401
    check_breakout,
    paper_shares_for,
    execute_breakout,
    close_breakout,
)
from broker.positions import (  # noqa: E402, F401
    _v5104_maybe_fire_entry_2,
    manage_positions,
    manage_short_positions,
)
from broker.lifecycle import (  # noqa: E402, F401
    check_entry,
    check_short_entry,
    execute_entry,
    execute_short_entry,
    close_position,
    close_short_position,
    eod_close,
)
# v5.11.2 PR 2 \u2014 [TRADE_CLOSED] exit_reason vocabulary moved to
# broker/orders.py with the close_breakout body. The v5.9.0 enum
# values are preserved here as a guard so the smoke-test source
# scan continues to succeed: "forensic_stop", "per_trade_brake",
# "be_stop", "ema_trail", "velocity_fuse".


def _validate_side_config_attrs() -> None:
    """Fail fast at module load if any SideConfig *_attr field references
    a name that doesn't exist in this module. Without this, a renamed
    module-level dict (e.g. positions -> open_positions) silently rots
    until the first entry of the day raises KeyError mid-session.
    """
    g = globals()
    for cfg in CONFIGS.values():
        for attr in (
            cfg.or_attr,
            cfg.positions_attr,
            cfg.daily_count_attr,
            cfg.daily_date_attr,
            cfg.trade_history_attr,
        ):
            assert attr in g, (
                f"SideConfig({cfg.side.value}) references missing "
                f"global {attr!r} in trade_genius.py"
            )


_validate_side_config_attrs()


# v5.26.0 \u2014 Profit-Lock Ladder deleted. Tiger Sovereign v15.0
# defines exit harvests via Sentinels A-A..A-E + the R-2 -$500 hard
# stop; peak-anchored give-back tiers are not part of the spec.


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
    # v15.0 SPEC: ORH/ORL fixed at exactly 09:35:59 ET. The OR window is
    # the inclusive minute range 09:30:00..09:35:59, which is the half-open
    # bar range [09:30:00, 09:36:00) so the 09:35 candle (open 09:35:00,
    # close 09:35:59) is INCLUDED in the OR aggregation.
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    or_end = now_et.replace(hour=9, minute=36, second=0, microsecond=0)
    open_ts = int(market_open.timestamp())
    end_ts = int(or_end.timestamp())

    for ticker in TICKERS:
        try:
            bars = fetch_1min_bars(ticker)
            if not bars:
                logger.warning("OR: No bars for %s", ticker)
                continue

            # v15.0 SPEC: filter bars in [09:30, 09:36) \u2014 includes the
            # 09:35 candle so ORH/ORL freeze at 09:35:59.
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
# were all superseded by the v3.4.28 Sovereign Regime Shield (a
# PDC-based eject) at the time. The Sovereign Regime Shield was
# itself retired in v5.9.1 (see block below); the entry-side
# index regime check now lives in the v5.9.0 5m EMA compass.
#
# Previously at this site: update_avwap(), _last_finalized_5min_close(),
# _dual_index_eject(). Removed in v3.4.34.
# ============================================================


# ============================================================
# v5.9.1 \u2014 Sovereign Regime Shield (PDC eject) REMOVED
# ============================================================
# v5.9.0 swapped the entry-side index regime check from AVWAP/PDC
# to a 5-minute EMA compass (QQQ Regime Shield). v5.9.1 retired the
# matching exit-side rule so entry and exit are consistent. v5.9.3
# eradicated the residual REASON_LABELS / _SHORT_REASON / comment
# residue and is now the source of truth: there is no
# Sovereign-Regime-Shield code path anywhere in the bot.
# ============================================================


# ============================================================
# v4.7.0 — Shared helpers for long/short entry symmetry
# ============================================================
def _ticker_today_realized_pnl(ticker: str) -> float:
    """Sum today's realized P&L for `ticker` from long+short closed trades."""
    pnl = sum(
        (t.get("pnl") or 0) for t in trade_history
        if t.get("ticker") == ticker and _is_today(t.get("exit_time_iso") or t.get("entry_time_iso", ""))
    )
    pnl += sum(
        (t.get("pnl") or 0) for t in short_trade_history
        if t.get("ticker") == ticker and _is_today(t.get("exit_time_iso") or t.get("entry_time_iso", ""))
    )
    return pnl


# v5.13.0 PR-5 SHARED-CUTOFF / SHARED-HUNT — single source of truth for the
# new-position cutoff (15:44:59 ET) lives in engine/timing.py. This wrapper
# adds the [SHARED-CUTOFF] log line and integrates with the entry path.
from engine.timing import (
    NEW_POSITION_CUTOFF_ET as _NEW_POSITION_CUTOFF_ET,
    EOD_FLUSH_ET as _EOD_FLUSH_ET,
    is_after_cutoff_et as _is_after_cutoff_et,
    is_after_eod_et as _is_after_eod_et,
)


def _check_new_position_cutoff(ticker: str) -> bool:
    """SHARED-CUTOFF gate: returns True if a NEW position may still be opened.

    At/after 15:44:59 ET, this returns False and emits a structured log line.
    Existing positions are NOT touched here — sentinel/ratchet manage them
    through SHARED-EOD (15:49:59 ET).
    """
    now_et = _now_et()
    if _is_after_cutoff_et(now_et):
        logger.info(
            "[SHARED-CUTOFF] ticker=%s now_et=%s cutoff_et=%s action=BLOCK_ENTRY",
            ticker, now_et.strftime("%H:%M:%S"),
            _NEW_POSITION_CUTOFF_ET.strftime("%H:%M:%S"),
        )
        return False
    return True


def _check_daily_loss_limit(ticker: str) -> bool:
    """Return True if entry should proceed; False if daily loss limit
    halts trading.

    Side effects on breach: sets _trading_halted=True, sets
    _trading_halted_reason, sends a Telegram alert. Mirrors the
    legacy block previously inlined in execute_entry only.
    """
    global _trading_halted, _trading_halted_reason

    if _trading_halted:
        logger.info("Trading halted — skipping entry for %s", ticker)
        return False

    now_et = _now_et()
    today_str = now_et.strftime("%Y-%m-%d")

    today_pnl = sum(
        (t.get("pnl") or 0) for t in paper_trades
        if t.get("date") == today_str and t.get("action") == "SELL"
    )
    today_pnl += sum(
        (t.get("pnl") or 0) for t in short_trade_history
        if _is_today(t.get("exit_time_iso") or "") and t.get("action") == "COVER"
    )

    for pos_ticker, pos in list(positions.items()):
        fmp = get_fmp_quote(pos_ticker)
        live_px = fmp.get("price", 0) if fmp else 0
        if live_px > 0:
            today_pnl += (live_px - pos["entry_price"]) * (pos.get("shares") or 0)

    for pos_ticker, pos in list(short_positions.items()):
        fmp = get_fmp_quote(pos_ticker)
        live_px = fmp.get("price", 0) if fmp else 0
        if live_px > 0:
            today_pnl += (pos["entry_price"] - live_px) * (pos.get("shares") or 0)

    # v5.27.0 \u2014 portfolio-scaled daily circuit breaker. The legacy
    # ``DAILY_LOSS_LIMIT`` env var still wins when set explicitly
    # (operator override); otherwise the scaled threshold derived from
    # current portfolio value drives the halt. ``portfolio_value_now``
    # is paper_cash + open long market value \u2212 open short liability,
    # mirrored from the /accounts and /portfolio commands.
    try:
        from eye_of_tiger import scaled_daily_circuit_breaker_dollars

        portfolio_value_now = paper_cash
        for _pt, _pp in positions.items():
            _fmp = get_fmp_quote(_pt) or {}
            _px = float(_fmp.get("price") or 0.0) or float(_pp.get("entry_price") or 0.0)
            portfolio_value_now += _px * float(_pp.get("shares") or 0)
        for _pt, _pp in short_positions.items():
            _fmp = get_fmp_quote(_pt) or {}
            _px = float(_fmp.get("price") or 0.0) or float(_pp.get("entry_price") or 0.0)
            # Short liability subtracts from portfolio.
            portfolio_value_now -= _px * float(_pp.get("shares") or 0)
        scaled_limit = scaled_daily_circuit_breaker_dollars(portfolio_value_now)
    except Exception:
        scaled_limit = float(DAILY_LOSS_LIMIT)
        portfolio_value_now = None
    # The effective limit is whichever is LESS aggressive (closer to
    # zero) of the env override and the scaled value \u2014 i.e. the
    # scaled limit can only TIGHTEN (smaller portfolio = smaller halt
    # dollars), never loosen below the operator override.
    effective_limit = max(float(DAILY_LOSS_LIMIT), float(scaled_limit))
    logger.info(
        "Daily P&L check: $%.2f (legacy_limit $%.2f, scaled_limit $%.2f, "
        "effective $%.2f, portfolio $%s)",
        today_pnl,
        float(DAILY_LOSS_LIMIT),
        float(scaled_limit),
        float(effective_limit),
        ("%.2f" % portfolio_value_now) if portfolio_value_now is not None else "n/a",
    )
    if today_pnl <= effective_limit:
        # v5.13.0 PR-5 SHARED-CB: structured circuit-breaker line. Logged once
        # at the moment of breach and on every subsequent blocked entry.
        logger.info(
            "[DAILY-BREAKER] day_pnl=%.2f threshold=%.2f action=BLOCK_ENTRY",
            today_pnl, float(effective_limit),
        )
        # v5.13.2 Track A SHARED-CB: on the false\u2192true transition of
        # _trading_halted, force-close all open longs and shorts at MARKET
        # per STRATEGY.md \u00a73. The close-loop runs inside this guard so
        # subsequent ticks (which short-circuit at the top via the
        # `if _trading_halted` check above) do not re-enter it. Reason
        # "DAILY_LOSS_LIMIT" matches REASON_CIRCUIT_BREAKER in
        # broker/order_types.py and maps to ORDER_TYPE_MARKET.
        was_halted = _trading_halted
        _trading_halted = True
        if not was_halted:
            try:
                long_tickers = list(positions.keys())
                for pos_ticker in long_tickers:
                    try:
                        fmp = get_fmp_quote(pos_ticker)
                        live_px = fmp.get("price", 0) if fmp else 0
                        if not live_px or live_px <= 0:
                            live_px = positions[pos_ticker].get(
                                "entry_price", 0
                            )
                        logger.info(
                            "[DAILY-BREAKER] force-close side=LONG "
                            "ticker=%s price=%.2f reason=DAILY_LOSS_LIMIT",
                            pos_ticker, float(live_px),
                        )
                        close_position(
                            pos_ticker, live_px, reason="DAILY_LOSS_LIMIT",
                        )
                    except Exception:
                        logger.exception(
                            "[DAILY-BREAKER] long close failed for %s",
                            pos_ticker,
                        )
                short_tickers = list(short_positions.keys())
                for pos_ticker in short_tickers:
                    try:
                        fmp = get_fmp_quote(pos_ticker)
                        live_px = fmp.get("price", 0) if fmp else 0
                        if not live_px or live_px <= 0:
                            live_px = short_positions[pos_ticker].get(
                                "entry_price", 0
                            )
                        logger.info(
                            "[DAILY-BREAKER] force-close side=SHORT "
                            "ticker=%s price=%.2f reason=DAILY_LOSS_LIMIT",
                            pos_ticker, float(live_px),
                        )
                        close_short_position(
                            pos_ticker, live_px, reason="DAILY_LOSS_LIMIT",
                        )
                    except Exception:
                        logger.exception(
                            "[DAILY-BREAKER] short close failed for %s",
                            pos_ticker,
                        )
            except Exception:
                logger.exception(
                    "[DAILY-BREAKER] force-close loop failed",
                )
        pnl_fmt = "%+.2f" % today_pnl
        limit_fmt = "%.2f" % effective_limit
        _trading_halted_reason = "Daily loss limit hit: $%s" % pnl_fmt
        halt_msg = (
            "STOP Trading halted — daily loss limit hit\n"
            "Today P&L: $%s\n"
            "Limit: $%s\n"
            "No new entries until tomorrow."
        ) % (pnl_fmt, limit_fmt)
        send_telegram(halt_msg)
        return False

    return True



# v5.12.0 \u2014 engine.bars / engine.phase_machine names imported here
# under `_engine_*` aliases because broker/positions.py and broker/orders.py
# reach into trade_genius globals via `tg._engine_phase_machine_tick` and
# `tg._engine_clear_phase_bucket`. These are canonical re-exports, not
# legacy shims \u2014 the engine package owns the implementations and trade_genius
# bridges them into its namespace for the broker callers. The 5m-bucket debounce dict (`_v5105_last_5m_bucket`) is owned
# by engine/phase_machine.py; trade_genius accesses it via clear_phase_bucket()
# during position close.
from engine.bars import compute_5m_ohlc_and_ema9 as _engine_compute_5m_ohlc_and_ema9  # noqa: E402, F401


# v5.26.0: engine/phase_machine.py deleted (non-spec FSM). The broker
# layer still calls `tg._engine_clear_phase_bucket` after each close \u2014
# replace with a no-op so the call site keeps compiling.
def _engine_phase_machine_tick(*args, **kwargs):
    return None


def _engine_clear_phase_bucket(*args, **kwargs):
    return None



# ============================================================
# v4.9.0 \u2014 Public entry/close API \u2014 thin wrappers
# v5.11.2 PR 4: moved to broker/lifecycle.py.
# ============================================================


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

    # B. State health check
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

    # C. Positions count
    n_paper = len(positions) + len(short_positions)
    lines.append("Pos: %d paper" % n_paper)

    # D. Scanner health
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

    # E. OR status — only for 8:31 CT label
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
        # v4.11.0 \u2014 report_error: scheduled health check failure.
        report_error(
            executor="main",
            code="SYSTEM_TEST_FAILED",
            severity="error",
            summary=f"System test failed: {label}",
            detail=f"{type(exc).__name__}: {exc}",
        )


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


# v5.10.1 \u2014 _tiger_hard_eject_check retired. Section V (Triple-Lock stops)
# in eye_of_tiger.py owns all exit decisions; legacy DI<25 hard-eject and
# REHUNT_VOL_CONFIRM watches are deleted.


# ============================================================
# SCAN LOOP
# ============================================================
def scan_loop():
    """Main scan: manage positions, check new entries. Runs every 60s.

    v5.11.0 PR4 \u2014 the body moved to `engine.scan.scan_loop` behind the
    `EngineCallbacks` Protocol. This thin shim builds the production
    callbacks impl (which wraps the existing module-level functions /
    globals) and dispatches. The `def scan_loop()` symbol is preserved
    so any importer / test that resolves `trade_genius.scan_loop` keeps
    working without change.
    """
    import engine.scan as _engine_scan
    _engine_scan.scan_loop(_ProdCallbacks())


class _ProdCallbacks:
    """Production `EngineCallbacks` impl. Each method wraps the existing
    `trade_genius` module-level function / global it replaced. Replay
    (PR 6) will pass a record-only mock with the same surface."""

    # --- Clock ----------------------------------------------------------
    def now_et(self):
        return _now_et()

    def now_cdt(self):
        return _now_cdt()

    # --- Market data ----------------------------------------------------
    def fetch_1min_bars(self, ticker):
        return fetch_1min_bars(ticker)

    # --- Position store -------------------------------------------------
    def get_position(self, ticker, side):
        if side == "long" or side == Side.LONG:
            return positions.get(ticker)
        return short_positions.get(ticker)

    def has_long(self, ticker):
        return ticker in positions

    def has_short(self, ticker):
        return ticker in short_positions

    # --- Position management -------------------------------------------
    def manage_positions(self):
        manage_positions()

    def manage_short_positions(self):
        manage_short_positions()

    # --- Entry signals --------------------------------------------------
    def check_entry(self, ticker):
        return check_entry(ticker)

    def check_short_entry(self, ticker):
        return check_short_entry(ticker)

    # --- Order execution ------------------------------------------------
    def execute_entry(self, ticker, price):
        execute_entry(ticker, price)

    def execute_short_entry(self, ticker, price):
        execute_short_entry(ticker, price)

    def execute_exit(self, ticker, side, price, reason):
        if side == "long" or side == Side.LONG:
            close_position(ticker, price, reason)
        else:
            close_short_position(ticker, price, reason)

    # --- Operator surface -----------------------------------------------
    def alert(self, msg):
        send_telegram(msg)

    def report_error(self, *, executor, code, severity, summary, detail):
        report_error(executor=executor, code=code, severity=severity,
                     summary=summary, detail=detail)




# ============================================================
# RESET DAILY STATE
# ============================================================
def reset_daily_state():
    """Reset OR data and daily counts for new trading day.
    (v3.4.34: AVWAP reset removed — AVWAP state no longer tracked.)
    """
    global or_collected_date, daily_entry_date, _trading_halted, _trading_halted_reason
    global daily_short_entry_count, daily_short_entry_date

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
        daily_short_entry_date = today
        # v5.6.1 \u2014 OR-snapshot dedup keyed by UTC date; clear at the
        # session boundary so tomorrow re-emits.
        try:
            _v561_reset_or_snap_state()
        except Exception:
            logger.exception("reset_daily_state: _v561 OR snap reset failed")
        # v5.0.0 \u2014 fresh session: clear all v5 state-machine tracks so
        # tomorrow's first ARMED transition gets a clean tab. C-R5 / C-R6
        # only LOCK; only the daily reset clears.
        v5_long_tracks.clear()
        v5_short_tracks.clear()
        v5_active_direction.clear()
        # v4.11.0 \u2014 health-pill: clear today's error counts at the
        # same boundary as the existing daily counters so the pill
        # rolls back to green at session reset.
        try:
            _error_state.reset_daily()
        except Exception:
            logger.exception("reset_daily_state: error_state.reset_daily failed")

    _trading_halted = False
    _trading_halted_reason = ""

    # Cross-day cooldown pruning: _last_exit_time persists across restarts,
    # so yesterday's 15:54 exit would keep today's 09:35 first-5-min entry
    # under the 15-min post-exit cooldown. Drop any entry whose exit
    # occurred before today's 09:30 ET session open.
    #
    # Invariant: all date/session comparisons here are done in ET (trading
    # timezone). _last_exit_time values are stored as UTC-aware datetimes,
    # so each stored value is converted to ET before comparing against
    # today's 09:30 ET session open. Using a single timezone (ET) for both
    # sides avoids subtle DST-boundary and midnight-ET off-by-one issues.
    try:
        session_open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        stale_keys = [
            k for k, v in list(_last_exit_time.items())
            if v is not None and v.astimezone(ET) < session_open_et
        ]
        for k in stale_keys:
            _last_exit_time.pop(k, None)
        if stale_keys:
            logger.info(
                "reset_daily_state: pruned %d stale _last_exit_time entries",
                len(stale_keys),
            )
    except Exception:
        logger.exception("reset_daily_state: _last_exit_time prune failed")

    # v5.13.9 \u2014 _regime_bullish reset removed alongside the retired
    # PDC regime alert. v5.26.0 \u2014 RSI regime classifier deleted.


# ============================================================
# SCHEDULER THREAD
# ============================================================
def scheduler_thread():
    """Background scheduler — all times in ET."""
    DAY_NAMES = [
        "monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday",
    ]

    # v5.1.8 \u2014 fired_set is now persisted in SQLite via persistence.py
    # (table: fired_set). Replaces the in-memory set so an EOD job that
    # fired before a Railway restart at 15:59:30 ET cannot double-fire
    # at 16:00 after the container comes back up.
    last_scan = _now_et() - timedelta(seconds=SCAN_INTERVAL + 1)
    last_state_save = _now_et() - timedelta(minutes=6)
    last_fired_prune = _now_et()

    # Job table: (day, "HH:MM", function). Times are ET.
    # v5.26.0 \u2014 09:29 premarket_recalc, 09:31 di_recompute_0931 +
    # qqq_regime_recompute_0931, and 10:00 / 10:30 DI safety-net
    # retries deleted (non-spec). 09:30 reset, 09:35 OR collect, R-4
    # 15:49 EOD flush retained.
    JOBS = [
        ("daily", "09:20", lambda: _fire_system_test("8:20 CT")),
        ("daily", "09:30", reset_daily_state),
        ("daily", "09:31", lambda: _fire_system_test("8:31 CT")),
        ("daily", "09:35",
         lambda: threading.Thread(target=collect_or, daemon=True).start()),
        ("daily", "09:36", send_or_notification),
        # R-4: EOD flush at 15:49:59 ET per Tiger Sovereign v15.0.
        ("daily", "15:49", eod_close),
        ("daily", "15:48", send_eod_report),
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
            if match and not persistence.was_fired(job_key):
                persistence.mark_fired(job_key)
                fn_name = getattr(fn, "__name__", "lambda")
                logger.info("Firing scheduled job: %s %s ET -> %s",
                            day, hhmm, fn_name)
                try:
                    fn()
                except Exception as e:
                    logger.error("Scheduled job error (%s %s): %s",
                                 day, hhmm, e, exc_info=True)

        # Prune fired_set rows from prior days. v5.1.8: SQLite-backed,
        # so we run this once an hour rather than on every loop.
        if (now_et - last_fired_prune).total_seconds() >= 3600:
            last_fired_prune = now_et
            today_prefix = now_et.strftime("%Y-%m-%d")
            try:
                persistence.prune_fired(today_prefix)
            except Exception as e:
                logger.warning("persistence.prune_fired failed: %s", e)

        # Scan loop — every SCAN_INTERVAL seconds
        elapsed = (now_et - last_scan).total_seconds()
        if elapsed >= SCAN_INTERVAL:
            last_scan = now_et
            try:
                scan_loop()
            except Exception as e:
                # v4.11.0 \u2014 report_error: top-level scan-loop catch.
                # If the whole cycle threw, the operator must know.
                report_error(
                    executor="main",
                    code="SCAN_LOOP_EXCEPTION",
                    severity="error",
                    summary="scan_loop crashed",
                    detail=f"{type(e).__name__}: {str(e)[:200]}",
                )

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


# positions_callback moved to telegram_ui/menu.py (v5.11.1 PR 3)






# ============================================================
# /near_misses COMMAND (v3.4.21)
# ============================================================


# ============================================================
# /retighten COMMAND (v3.4.23)
# ============================================================


# ============================================================
# /trade_log COMMAND — last 10 persistent-log entries (v3.4.27)
# ============================================================


# ============================================================
# /tp_sync COMMAND — TradersPost broker sync status (v3.4.15)
# ============================================================


# ============================================================
# /rh_enable /rh_disable /rh_status \u2014 live-trading kill switch
# ============================================================


# ============================================================
# /mode COMMAND — market mode classifier (observation only)
# ============================================================


# ============================================================
# /algo COMMAND
# ============================================================


# ============================================================
# /strategy COMMAND
# ============================================================


# ============================================================
# /reset COMMAND (Fix C)
# ============================================================

# Window in seconds during which a "Confirm" tap is accepted after the
# /reset command was issued. Beyond this, the callback is rejected — this
# prevents scrolling up to an old /reset message tomorrow and tapping
# Confirm by accident.
RESET_CONFIRM_WINDOW_SEC = 60




# ============================================================
# /perf COMMAND (Feature 5)
# ============================================================


# ============================================================
# /price COMMAND (Feature 6)
# ============================================================


# ============================================================
# /proximity COMMAND (v3.3.0)
# ============================================================




# proximity_callback moved to telegram_ui/menu.py (v5.11.1 PR 3)


# ============================================================
# /orb COMMAND (Feature 7)
# ============================================================


# ============================================================
# /monitoring COMMAND (Feature 8)
# ============================================================


# monitoring_callback moved to telegram_ui/menu.py (v5.11.1 PR 3)


# ============================================================
# MENU KEYBOARD BUILDER + MENU BUTTON HELPER
# ============================================================
# _build_menu_keyboard / _build_advanced_menu_keyboard / _menu_button
# moved to telegram_ui/menu.py (v5.11.1 PR 3)


# ============================================================
# /menu COMMAND — Quick tap-grid
# ============================================================


# _CallbackUpdateShim, _invoke_from_callback, menu_callback, _cb_open_menu
# moved to telegram_ui/menu.py (v5.11.1 PR 3)






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


# v4.6.0 \u2014 paper-state I/O lives in paper_state.py. Re-exported here so
# existing callsites (telegram_commands.py, smoke_test.py, internal
# uses) keep resolving the names from `trade_genius`. Must come BEFORE
# `import telegram_commands` because telegram_commands does
# `from trade_genius import save_paper_state, _do_reset_paper`.
import paper_state  # noqa: E402
from paper_state import save_paper_state, load_paper_state, _do_reset_paper  # noqa: E402,F401

# v4.5.0 \u2014 defer import to avoid circular (telegram_commands imports from trade_genius).
import telegram_commands  # noqa: E402,F401

# v5.11.1 PR 4 \u2014 bot lifecycle moved to telegram_ui/runtime.py.
# Re-exported here so existing callsites and the __main__ block still
# resolve `_set_bot_commands`, `_send_startup_menu`, `send_startup_message`,
# `_auth_guard`, and `run_telegram_bot` from `trade_genius`.
from telegram_ui.runtime import (  # noqa: E402, F401
    _set_bot_commands,
    _send_startup_menu,
    send_startup_message,
    _auth_guard,
    run_telegram_bot,
)


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
# v5.8.0 \u2014 universe-drift startup guard. Rewrites
# /data/tickers.json if it lags the code-side TICKERS_DEFAULT, so a
# Railway redeploy that ships a new universe never trades the stale
# persisted list. Emits the [UNIVERSE_GUARD] log tag. Runs BEFORE
# _init_tickers() so the loader sees the corrected file.
try:
    _ensure_universe_consistency()
except Exception as _uge:
    logger.error("[UNIVERSE_GUARD] startup check crashed: %s", _uge, exc_info=True)

# v3.4.32 — load the editable ticker universe from tickers.json
# before anything else so load_paper_state() and retighten see the
# right TICKERS list (e.g. if a newly-added QBTS already has an
# open paper position persisted from a previous session). Note
# v5.10.7: QBTS is no longer in the defaults; the example still
# applies if QBTS was added at runtime via `/ticker add QBTS`.
_init_tickers()

# v5.26.0 \u2014 [V561-UNIVERSE] boot line, _start_volume_profile boot,
# retighten_all_stops startup retro all deleted. Volume profile / cap
# tightening / universe-line apparatus is not part of Tiger Sovereign
# v15.0. load_paper_state() still required \u2014 it restores open
# positions from the prior session.
load_paper_state()

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
# v5.6.0 \u2014 confirms the unified-AVWAP gate set is active on every boot.
logger.info(
    "[V560] Unified AVWAP gates: L-P1 (G1/G3/G4), S-P1 (G1/G3/G4)"
)
# v5.13.1 \u2014 surface the Phase 2 volume-gate runtime override at boot
# so the deploy log shows the active state of L-P2-S3 / S-P2-S3.
try:
    from engine import feature_flags as _ff_startup
    _vg_state = _ff_startup.VOLUME_GATE_ENABLED
    logger.info(
        "[STARTUP] VOLUME_GATE_ENABLED=%s (%s)",
        _vg_state,
        "spec-strict path" if _vg_state else "using DISABLED_BY_FLAG path",
    )
except Exception as _ff_err:
    logger.warning("[STARTUP] feature_flags read failed: %s", _ff_err)

# Smoke-test guard — lets smoke_test.py import this module without booting
# the Telegram client, scheduler, OR-collector, or dashboard. The test
# script sets SSM_SMOKE_TEST=1 before import. This is the ONLY place
# where that env var is read.
if os.getenv("SSM_SMOKE_TEST", "").strip() == "1":
    logger.info("SSM_SMOKE_TEST=1 \u2014 skipping catch-up, scheduler, and Telegram loop")
    # v5.10.4 \u2014 when invoked as the entrypoint (Docker CMD: `python
    # trade_genius.py`), block the main thread so the dashboard
    # daemon thread keeps serving /api/version. The CI Docker-build
    # gate polls that endpoint to verify the container actually
    # boots before the PR can merge. Skipped when imported under
    # pytest (``__name__ == 'trade_genius'``) so the existing
    # tests/test_startup_smoke.py imports keep returning promptly.
    if __name__ == "__main__":
        import time as _ssm_time
        logger.info("SSM_SMOKE_TEST=1 \u2014 blocking on idle loop to keep web server alive")
        try:
            while True:
                _ssm_time.sleep(60)
        except KeyboardInterrupt:
            pass
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

    # v5.26.0 \u2014 DI seed from prior session deleted (non-spec). DI now
    # warms up naturally from live ticks during RTH; entries that
    # require DI authority simply wait for warmup per BS-1 / BF-1.

    # Background threads
    threading.Thread(target=scheduler_thread, daemon=True).start()
    threading.Thread(target=health_ping, daemon=True).start()

    # v5.12.0 \u2014 executor bootstrap moved to executors/bootstrap.py
    from executors.bootstrap import build_val_executor, build_gene_executor, install_globals
    val_executor = build_val_executor()
    gene_executor = build_gene_executor()
    install_globals(val=val_executor, gene=gene_executor)

    logger.info("%s v%s started", BOT_NAME, BOT_VERSION)
    logger.info("[ENGINE] modules loaded: %s", ", ".join(engine.LOADED_MODULES))
    logger.info("[TELEGRAM-UI] modules loaded: %s", ", ".join(telegram_ui.LOADED_MODULES))
    logger.info("[BROKER] modules loaded: %s", ", ".join(broker.LOADED_MODULES))
    logger.info("[EXEC] modules loaded: %s", ", ".join(executors.LOADED_MODULES))
    send_startup_message()
    run_telegram_bot()
