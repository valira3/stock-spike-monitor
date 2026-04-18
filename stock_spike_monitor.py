"""
Stock Spike Monitor v2.9.0 — ORB Momentum Breakout + Wounded Buffalo Short
===========================================================================
10-ticker universe, Opening Range breakout (long) + breakdown (short),
$1.00 stepped trail. Infrastructure: Telegram bot, paper trading,
TradersPost webhook, scheduler.
"""

import os
from pathlib import Path
import json
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
    Application, CommandHandler, ContextTypes, CallbackQueryHandler,
)

# ============================================================
# CONFIG FROM ENVIRONMENT VARIABLES
# ============================================================
TELEGRAM_TOKEN          = os.getenv("TELEGRAM_TOKEN")
CHAT_ID                 = os.getenv("CHAT_ID")
TRADERSPOST_WEBHOOK_URL = os.getenv("TRADERSPOST_WEBHOOK_URL")
TELEGRAM_TP_CHAT_ID     = "5165570192"
TELEGRAM_TP_TOKEN       = os.getenv("TELEGRAM_TP_TOKEN", "8612076951:AAGZXzVA4btFOMjYw-9VN1P4Iu9uggHWzQk")
TP_TOKEN                = TELEGRAM_TP_TOKEN  # alias for is_tp_update()

BOT_VERSION = "2.9.36"
RELEASE_NOTE = "v2.9.36 \u2014 Fix blocking calls in all async command handlers."

FMP_API_KEY = os.getenv("FMP_API_KEY", "VqYj2Jujrc8IvUOe4CR1g0tRf0qlB4AV")
FINNHUB_TOKEN = os.getenv("FINNHUB_TOKEN", "")

# Human-readable exit reason labels
REASON_LABELS = {
    "STOP": "\U0001f6d1 Hard Stop",
    "TRAIL": "\U0001f3af Trail Stop",
    "RED_CANDLE": "\U0001f56f Red Candle (lost daily polarity)",
    "LORDS_LEFT": "\U0001f451 Lords Left (SPY/QQQ < AVWAP)",
    "LORDS_LEFT[1m]": "\U0001f451 Lords Left (SPY/QQQ < AVWAP)",
    "POLARITY_SHIFT": "\U0001f504 Polarity Shift (price > PDC)",
    "BULL_VACUUM": "\U0001f300 Bull Vacuum (SPY/QQQ > AVWAP)",
    "BULL_VACUUM[1m]": "\U0001f300 Bull Vacuum (SPY/QQQ > AVWAP)",
    "EOD": "\U0001f514 End of Day",
}

# ============================================================
# LOGGING
# ============================================================
LOG_FILE = "stock_spike_monitor.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

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
            cdt_dt = dt.astimezone(timezone(timedelta(hours=-5)))
            return cdt_dt.strftime("%H:%M")
        except Exception:
            pass
    # HH:MM:SS or HH:MM — already local (CDT), just truncate
    parts = ts.split(":")
    if len(parts) >= 2:
        return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}"
    return ts[:5]


def _is_today(ts_str: str) -> bool:
    """Check if an ISO timestamp string is from today (UTC)."""
    if not ts_str:
        return False
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        today = datetime.utcnow().date()
        return dt.date() == today
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
TP_STATE_FILE          = os.getenv(
    "TP_STATE_FILE",
    os.path.join(os.path.dirname(PAPER_STATE_FILE) or ".", "tp_state.json")
)
PAPER_STARTING_CAPITAL = 100_000.0
PAPER_MODE             = True  # True = paper only, False = send webhook

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
TICKERS = [
    "AAPL", "MSFT", "NVDA", "TSLA", "META",
    "GOOG", "AMZN", "AVGO", "SPY", "QQQ",
]
TRADE_TICKERS = [t for t in TICKERS if t not in ("SPY", "QQQ")]

SHARES         = 10
STOP_OFFSET    = 0.50    # Initial stop: entry - $0.50
# Trail: +1.0% trigger, max(price*1.0%, $1.00) distance — see manage_positions()
TRAIL_TRIGGER  = 1.00    # Legacy constant (unused — trail is now percentage-based)
TRAIL_STEP     = 0.50    # Legacy constant (unused — trail is now percentage-based)

SCAN_INTERVAL  = 60      # seconds between scans
YAHOO_TIMEOUT  = 8       # seconds
YAHOO_HEADERS  = {"User-Agent": "Mozilla/5.0"}

# ============================================================
# GLOBAL STATE
# ============================================================

# OR data — populated at 09:35 ET
or_high: dict = {}                  # ticker -> OR high price
or_low: dict = {}                   # ticker -> OR low price (Wounded Buffalo)
pdc: dict = {}                      # ticker -> previous day close
or_collected_date: str = ""         # date string, prevents re-collection

# AVWAP state — SPY and QQQ only
avwap_data: dict = {
    "SPY": {"cum_pv": 0.0, "cum_vol": 0.0, "avwap": 0.0},
    "QQQ": {"cum_pv": 0.0, "cum_vol": 0.0, "avwap": 0.0},
}
avwap_last_ts: dict = {"SPY": 0, "QQQ": 0}

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

# TP Portfolio (independent, parallel tracking)
tp_positions: dict = {}
tp_paper_trades: list = []
tp_paper_cash: float = PAPER_STARTING_CAPITAL

# Trade history persistence (Feature 1)
trade_history: list = []        # ALL closed paper trades, max 500
tp_trade_history: list = []     # ALL closed TP trades, max 500
TRADE_HISTORY_MAX = 500

# Short positions (Wounded Buffalo strategy)
short_positions: dict = {}           # paper short: {ticker: {entry_price, shares, stop, trail_stop, trail_active, entry_time, date, side}}
tp_short_positions: dict = {}        # TP short positions
daily_short_entry_count: dict = {}   # {ticker: int} — resets daily, separate from long count
short_trade_history: list = []       # max 500 closed paper shorts
tp_short_trade_history: list = []    # max 500 closed TP shorts

# Daily loss limit (Feature 2)
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "-500"))
_trading_halted: bool = False
_trading_halted_reason: str = ""

# Scan pause (Feature 8)
_scan_paused: bool = False
_regime_bullish = None          # None=unknown, True/False tracks last known regime
_last_exit_time: dict = {}     # ticker -> datetime (UTC) of last exit
_last_scan_time = None           # datetime (UTC), updated each scan cycle

# TradersPost state
tp_state: dict = {
    "total_orders_sent": 0,
    "total_orders_success": 0,
    "total_orders_failed": 0,
    "last_order_time": None,
    "recent_orders": [],
}
tp_dm_chat_id = None

# User config
user_config: dict = {"trading_mode": "paper"}

# Thread safety
_paper_save_lock = threading.Lock()


# ============================================================
# NOTIFICATION ROUTING HELPER (Fix B)
# ============================================================
def is_tp_update(update) -> bool:
    """Check if the Telegram update came from the TP bot."""
    try:
        return update.get_bot().token == TP_TOKEN
    except Exception:
        return False


# ============================================================
# STATE PERSISTENCE
# ============================================================
def save_paper_state():
    """Persist paper trading + strategy state to disk. Thread-safe, atomic."""
    state = {
        "paper_cash": paper_cash,
        "positions": positions,
        "paper_trades": paper_trades,
        "paper_all_trades": paper_all_trades,
        "daily_entry_count": daily_entry_count,
        "daily_entry_date": daily_entry_date,
        "or_high": or_high,
        "or_low": or_low,
        "pdc": pdc,
        "or_collected_date": or_collected_date,
        "user_config": user_config,
        "tp_state": tp_state,
        "trade_history": trade_history,
        "short_positions": short_positions,
        "short_trade_history": short_trade_history[-500:],
        "avwap_data": avwap_data,
        "avwap_last_ts": avwap_last_ts,
        "daily_short_entry_count": daily_short_entry_count,
        "last_exit_time": {k: v.isoformat() for k, v in _last_exit_time.items()},
        "saved_at": _utc_now_iso(),
    }
    with _paper_save_lock:
        tmp = PAPER_STATE_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, default=str)
            os.replace(tmp, PAPER_STATE_FILE)
            logger.debug("Paper state saved -> %s", PAPER_STATE_FILE)
        except Exception as e:
            logger.error("save_paper_state failed: %s", e)


def load_paper_state():
    """Load paper trading state from disk on startup."""
    global paper_cash, positions, paper_trades, paper_all_trades
    global daily_entry_count, daily_entry_date
    global or_high, or_low, pdc, or_collected_date
    global user_config, tp_state, tp_dm_chat_id
    global trade_history
    global short_positions, short_trade_history
    global avwap_data, avwap_last_ts, daily_short_entry_count
    global _last_exit_time

    if not os.path.exists(PAPER_STATE_FILE):
        paper_log("No saved state at %s. Starting fresh $%.0f."
                  % (PAPER_STATE_FILE, PAPER_STARTING_CAPITAL))
        return

    try:
        with open(PAPER_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        paper_cash = float(state.get("paper_cash", PAPER_STARTING_CAPITAL))
        positions.update(state.get("positions", {}))
        paper_trades.extend(state.get("paper_trades", []))
        paper_all_trades.extend(state.get("paper_all_trades", []))
        daily_entry_count.update(state.get("daily_entry_count", {}))
        daily_entry_date = state.get("daily_entry_date", "")
        or_high.update(state.get("or_high", {}))
        or_low.update(state.get("or_low", {}))
        pdc.update(state.get("pdc", {}))
        or_collected_date = state.get("or_collected_date", "")
        user_config.update(state.get("user_config", {}))
        tp_state.update(state.get("tp_state", {}))
        trade_history.extend(state.get("trade_history", []))
        short_positions.update(state.get("short_positions", {}))
        short_trade_history.extend(state.get("short_trade_history", []))
        avwap_data.update(state.get("avwap_data", {}))
        avwap_last_ts.update(state.get("avwap_last_ts", {}))
        daily_short_entry_count.update(state.get("daily_short_entry_count", {}))
        raw_exit = state.get("last_exit_time", {})
        _last_exit_time = {k: datetime.fromisoformat(v) for k, v in raw_exit.items()}

        # Reset daily counts if saved on a different day
        today = _now_et().strftime("%Y-%m-%d")
        if daily_entry_date != today:
            daily_entry_count.clear()
            paper_trades.clear()

        logger.info("Loaded paper state: cash=$%.2f, %d positions",
                    paper_cash, len(positions))
    except Exception as e:
        logger.error("load_paper_state failed: %s — starting fresh", e)


# ============================================================
# TP STATE PERSISTENCE
# ============================================================
_tp_save_lock = threading.Lock()


def save_tp_state():
    """Persist TP portfolio state to disk. Thread-safe, atomic."""
    state = {
        "tp_paper_cash": tp_paper_cash,
        "tp_positions": tp_positions,
        "tp_paper_trades": tp_paper_trades,
        "tp_trade_history": tp_trade_history,
        "tp_short_positions": tp_short_positions,
        "tp_short_trade_history": tp_short_trade_history[-500:],
        "saved_at": _utc_now_iso(),
    }
    with _tp_save_lock:
        tmp = TP_STATE_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, default=str)
            os.replace(tmp, TP_STATE_FILE)
            logger.debug("TP state saved -> %s", TP_STATE_FILE)
        except Exception as e:
            logger.error("save_tp_state failed: %s", e)


def load_tp_state():
    """Load TP portfolio state from disk on startup."""
    global tp_paper_cash, tp_trade_history
    global tp_short_positions, tp_short_trade_history

    if not os.path.exists(TP_STATE_FILE):
        logger.info("No TP state at %s. Starting fresh $%.0f.",
                     TP_STATE_FILE, PAPER_STARTING_CAPITAL)
        return

    try:
        with open(TP_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        tp_paper_cash = float(state.get("tp_paper_cash", PAPER_STARTING_CAPITAL))
        tp_positions.update(state.get("tp_positions", {}))
        tp_paper_trades.extend(state.get("tp_paper_trades", []))
        tp_trade_history.extend(state.get("tp_trade_history", []))
        tp_short_positions.update(state.get("tp_short_positions", {}))
        tp_short_trade_history.extend(state.get("tp_short_trade_history", []))

        logger.info("Loaded TP state: cash=$%.2f, %d positions",
                    tp_paper_cash, len(tp_positions))
    except Exception as e:
        logger.error("load_tp_state failed: %s — starting fresh", e)


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


def send_tp_telegram(message):
    """Send to TP user's DM chat. Falls back to main channel. 3-attempt retry."""
    chat_id = tp_dm_chat_id or TELEGRAM_TP_CHAT_ID
    if not chat_id:
        send_telegram("[TP] %s" % message)
        return
    token = TELEGRAM_TP_TOKEN or TELEGRAM_TOKEN
    if not token:
        return
    for attempt in range(3):
        try:
            payload = json.dumps({"chat_id": chat_id, "text": message}).encode()
            url = "https://api.telegram.org/bot%s/sendMessage" % token
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
            return  # success
        except Exception as e:
            if attempt == 2:
                logger.warning("send_tp_telegram failed after 3 attempts: %s", e)
            else:
                time.sleep(1)


# ============================================================
# YAHOO FINANCE DATA
# ============================================================
def fetch_1min_bars(ticker):
    """Fetch 1-min intraday bars from Yahoo Finance.

    Returns dict with keys: timestamps, opens, highs, lows, closes,
    volumes, current_price, pdc.  Returns None on failure.
    """
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
            return None
        r = result[0]
        meta = r.get("meta", {})
        quote = r.get("indicators", {}).get("quote", [{}])[0]
        timestamps = r.get("timestamp", [])

        if not timestamps:
            return None

        return {
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
    except Exception as e:
        logger.debug("fetch_1min_bars %s failed: %s", ticker, e)
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
    try:
        url = (
            "https://financialmodelingprep.com/stable/quote"
            "?symbol=%s&apikey=%s" % (ticker, FMP_API_KEY)
        )
        req = urllib.request.Request(url, headers=YAHOO_HEADERS)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
    except Exception as e:
        logger.warning("FMP quote error for %s: %s", ticker, e)
    return None


def _or_price_sane(or_price, live_price, threshold=0.015):
    """Return True if OR price is within threshold of live price."""
    if not or_price or not live_price:
        return True  # can't validate, allow
    diff = abs(or_price - live_price) / live_price
    return diff <= threshold


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
    send_tp_telegram("\n".join(lines))


# ============================================================
# AVWAP (Anchored VWAP for SPY / QQQ)
# ============================================================
def update_avwap(ticker):
    """Update AVWAP for SPY or QQQ using new 1-min bars since last update."""
    if ticker not in avwap_data:
        return 0.0

    bars = fetch_1min_bars(ticker)
    if not bars:
        return avwap_data[ticker]["avwap"]

    last_ts = avwap_last_ts.get(ticker, 0)

    for i, ts in enumerate(bars["timestamps"]):
        if ts <= last_ts:
            continue
        h = bars["highs"][i]
        lo = bars["lows"][i]
        c = bars["closes"][i]
        v = bars["volumes"][i]
        if h is None or lo is None or c is None or v is None:
            continue
        if v == 0:
            continue
        typical_price = (h + lo + c) / 3.0
        avwap_data[ticker]["cum_pv"] += typical_price * v
        avwap_data[ticker]["cum_vol"] += v
        avwap_last_ts[ticker] = ts

    cum_vol = avwap_data[ticker]["cum_vol"]
    if cum_vol > 0:
        avwap_data[ticker]["avwap"] = avwap_data[ticker]["cum_pv"] / cum_vol

    return avwap_data[ticker]["avwap"]


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

    # Timing gate: after 09:45 ET (15-min buffer)
    market_open = now_et.replace(hour=9, minute=45, second=0, microsecond=0)
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
        elapsed = (datetime.utcnow() - last_exit).total_seconds()
        if elapsed < 900:
            mins_left = int((900 - elapsed) / 60) + 1
            logger.info("SKIP %s [COOLDOWN] %dm left", ticker, mins_left)
            return False, None

    # Per-ticker daily loss cap: skip if down > $50 on this ticker today
    ticker_pnl_today = sum(
        (t.get("pnl") or 0) for t in trade_history
        if t.get("ticker") == ticker and _is_today(t.get("exit_time_iso") or t.get("entry_time_iso", ""))
    )
    if ticker_pnl_today < -50.0:
        logger.info("SKIP %s [LOSS CAP] ticker P&L today: $%.2f", ticker, ticker_pnl_today)
        return False, None

    # Not already in position
    if ticker in positions:
        return False, None

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

    # OR sanity check: OR High must be within 1.5% of live price
    if not _or_price_sane(or_high[ticker], current_price):
        pct = abs(or_high[ticker] - current_price) / current_price * 100
        logger.warning(
            "SKIP %s long — OR High $%.2f is %.1f%% from live $%.2f (stale?)",
            ticker, or_high[ticker], pct, current_price
        )
        return False, None

    # Volume confirmation: entry bar volume >= 1.5x session average
    volumes = bars.get("volumes", [])
    if len(volumes) >= 5:
        valid_vols = [v for v in volumes[:-1] if v is not None and v > 0]
        avg_vol = sum(valid_vols) / len(valid_vols) if valid_vols else 0
        entry_bar_vol = volumes[-2] if volumes[-2] is not None else 0
        if avg_vol > 0 and entry_bar_vol < avg_vol * 1.5:
            logger.info("SKIP %s [LOW VOL] entry bar %.0f vs avg %.0f", ticker, entry_bar_vol, avg_vol)
            return False, None

    # Breakout: last 1-min bar close > OR_High
    if last_close <= or_high[ticker]:
        return False, None

    # Polarity: current price > PDC
    if current_price <= pdc[ticker]:
        return False, None

    # Index anchor: SPY > SPY_AVWAP and QQQ > QQQ_AVWAP
    spy_bars = fetch_1min_bars("SPY")
    qqq_bars = fetch_1min_bars("QQQ")
    if not spy_bars or not qqq_bars:
        return False, None

    update_avwap("SPY")
    update_avwap("QQQ")

    spy_avwap = avwap_data["SPY"]["avwap"]
    qqq_avwap = avwap_data["QQQ"]["avwap"]

    if spy_avwap == 0 or qqq_avwap == 0:
        return False, None

    if spy_bars["current_price"] <= spy_avwap:
        return False, None
    if qqq_bars["current_price"] <= qqq_avwap:
        return False, None

    return True, bars


# ============================================================
# TRADERSPOST WEBHOOK
# ============================================================
def send_traderspost_order(ticker, action, price, shares=SHARES):
    """Send a limit order to TradersPost via webhook.

    action: 'buy', 'sell', 'sell_short', or 'buy_to_cover'
    Returns response dict or None.
    """
    if PAPER_MODE or not TRADERSPOST_WEBHOOK_URL:
        if not TRADERSPOST_WEBHOOK_URL:
            logger.debug("[TP] No webhook URL configured")
        return None

    # Limit price: buy/buy_to_cover slightly above, sell/sell_short slightly below
    if action in ("buy", "buy_to_cover"):
        limit_price = round(price + 0.02, 2)
    else:
        limit_price = round(price - 0.01, 2)

    payload = {
        "ticker": ticker,
        "action": action,
        "orderType": "limit",
        "limitPrice": limit_price,
        "quantity": shares,
    }

    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            TRADERSPOST_WEBHOOK_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_data = json.loads(resp.read())

        logger.info("[TP] %s %s %d @ $%.2f limit $%.2f -> %s",
                    action.upper(), ticker, shares, price, limit_price, resp_data)

        tp_state["total_orders_sent"] = tp_state.get("total_orders_sent", 0) + 1
        if resp_data.get("success"):
            tp_state["total_orders_success"] = tp_state.get("total_orders_success", 0) + 1
        else:
            tp_state["total_orders_failed"] = tp_state.get("total_orders_failed", 0) + 1

        now_str = _utc_now_iso()
        tp_state["last_order_time"] = now_str
        recent = tp_state.get("recent_orders", [])
        recent.append({
            "ticker": ticker, "action": action.upper(),
            "price": price, "limit_price": limit_price,
            "shares": shares, "success": resp_data.get("success", False),
            "time": now_str,
        })
        if len(recent) > 20:
            recent[:] = recent[-20:]
        tp_state["recent_orders"] = recent
        save_paper_state()
        return resp_data

    except Exception as e:
        logger.error("[TP] Webhook failed for %s %s: %s", action, ticker, e)
        tp_state["total_orders_failed"] = tp_state.get("total_orders_failed", 0) + 1
        return None


# ============================================================
# EXECUTE ENTRY
# ============================================================
def execute_entry(ticker, current_price):
    """Place a limit buy for 10 shares. Record position + paper trade."""
    global paper_cash, tp_paper_cash, _trading_halted, _trading_halted_reason

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
    stop_price = round(or_high_val - 0.90, 2)
    entry_num = daily_entry_count.get(ticker, 0) + 1
    now_str = _now_cdt().strftime("%H:%M:%S")
    now_hhmm = _now_cdt().strftime("%H:%M CDT")
    now_date = now_et.strftime("%Y-%m-%d")

    positions[ticker] = {
        "entry_price": current_price,
        "shares": SHARES,
        "stop": stop_price,
        "trail_active": False,
        "trail_high": current_price,
        "entry_count": entry_num,
        "entry_time": now_str,
        "pdc": pdc.get(ticker, 0),
    }
    daily_entry_count[ticker] = entry_num

    # Paper accounting
    cost = current_price * SHARES
    paper_cash -= cost
    trade = {
        "action": "BUY",
        "ticker": ticker,
        "price": current_price,
        "limit_price": limit_price,
        "shares": SHARES,
        "cost": cost,
        "stop": stop_price,
        "entry_num": entry_num,
        "time": now_hhmm,
        "date": now_date,
    }
    paper_trades.append(trade)
    paper_all_trades.append(trade)

    paper_log("BUY %s %d @ $%.2f (limit $%.2f) stop=$%.2f entry#%d"
              % (ticker, SHARES, current_price, limit_price, stop_price, entry_num))

    # TradersPost webhook
    send_traderspost_order(ticker, "buy", current_price)

    # Fix B: Paper BUY notification → send_telegram() ONLY
    or_h = or_high.get(ticker, 0)
    pdc_e = pdc.get(ticker, 0)
    SEP_E = "\u2500" * 34
    sig_lines = "Signal : ORB Breakout \u2191\n"
    sig_lines += "  1m close > OR High \u2713\n"
    sig_lines += "  Price > PDC \u2713\n"
    sig_lines += "  SPY > AVWAP \u2713\n"
    sig_lines += "  QQQ > AVWAP \u2713\n"
    msg = (
        "\U0001f4c8 LONG ENTRY %s  #%d\n"
        "%s\n"
        "Price  : $%.2f  (limit $%.2f)\n"
        "Shares : %d   Cost: $%s\n"
        "Stop   : $%.2f  (OR_High-$0.90)\n"
        "OR High: $%.2f   PDC: $%.2f\n"
        "%s"
        "Time   : %s\n"
        "%s"
    ) % (ticker, entry_num, SEP_E,
         current_price, limit_price,
         SHARES, format(cost, ",.2f"),
         stop_price, or_h, pdc_e, sig_lines, now_hhmm, SEP_E)
    send_telegram(msg)

    # TP Portfolio — mirror entry
    tp_positions[ticker] = {
        "entry_price": current_price,
        "shares": SHARES,
        "stop": stop_price,
        "trail_active": False,
        "trail_high": current_price,
        "entry_count": entry_num,
        "entry_time": now_str,
        "pdc": pdc.get(ticker, 0),
    }
    tp_paper_cash -= cost
    tp_paper_trades.append(trade.copy())
    logger.info("[TP] BUY %s %d @ $%.2f (limit $%.2f) stop=$%.2f entry#%d",
                ticker, SHARES, current_price, limit_price, stop_price, entry_num)

    # Fix B: TP BUY notification → send_tp_telegram() ONLY
    tp_msg = (
        "[TP] \U0001f4c8 LONG ENTRY %s  #%d\n"
        "%s\n"
        "Price  : $%.2f  (limit $%.2f)\n"
        "Shares : %d   Cost: $%s\n"
        "Stop   : $%.2f  (OR_High-$0.90)\n"
        "OR High: $%.2f   PDC: $%.2f\n"
        "%s"
        "Time   : %s\n"
        "%s"
    ) % (ticker, entry_num, SEP_E,
         current_price, limit_price,
         SHARES, format(cost, ",.2f"),
         stop_price, or_h, pdc_e, sig_lines, now_hhmm, SEP_E)
    send_tp_telegram(tp_msg)
    save_tp_state()

    save_paper_state()


# ============================================================
# CLOSE POSITION
# ============================================================
def close_position(ticker, price, reason="STOP"):
    """Close position: remove, log P&L, send webhook + Telegram."""
    global paper_cash, tp_paper_cash

    if ticker not in positions:
        return

    _last_exit_time[ticker] = datetime.utcnow()

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

    paper_log("SELL %s %d @ $%.2f reason=%s pnl=$%.2f (%.1f%%)"
              % (ticker, shares, price, reason, pnl_val, pnl_pct))

    # TradersPost webhook
    send_traderspost_order(ticker, "sell", price, shares)

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

    # TP Portfolio — mirror close
    if ticker in tp_positions:
        tp_pos = tp_positions.pop(ticker)
        tp_entry = tp_pos["entry_price"]
        tp_shares = tp_pos["shares"]
        tp_pnl = (price - tp_entry) * tp_shares
        tp_pnl_pct = ((price - tp_entry) / tp_entry * 100) if tp_entry else 0
        tp_paper_cash += price * tp_shares
        logger.info("[TP] SELL %s %d @ $%.2f reason=%s pnl=$%.2f",
                    ticker, tp_shares, price, reason, tp_pnl)

        tp_entry_time_str = tp_pos.get("entry_time", "")
        tp_entry_hhmm = _to_cdt_hhmm(tp_entry_time_str) if tp_entry_time_str else ""

        tp_paper_trades.append({
            "action": "SELL",
            "ticker": ticker,
            "price": price,
            "shares": tp_shares,
            "pnl": round(tp_pnl, 2),
            "pnl_pct": round(tp_pnl_pct, 2),
            "reason": reason,
            "entry_price": tp_entry,
            "time": now_hhmm,
            "date": now_date,
        })

        # Feature 1: Append to tp_trade_history
        tp_entry_time_str = tp_pos.get("entry_time", "")
        tp_hist_record = {
            "ticker": ticker,
            "side": "long",
            "action": "SELL",
            "shares": tp_shares,
            "entry_price": tp_entry,
            "exit_price": price,
            "pnl": round(tp_pnl, 2),
            "pnl_pct": round(tp_pnl_pct, 2),
            "reason": reason,
            "entry_time": tp_entry_hhmm,
            "exit_time": now_hhmm,
            "entry_time_iso": tp_entry_time_str,
            "exit_time_iso": _utc_now_iso(),
            "entry_num": tp_pos.get("entry_count", 1),
            "date": now_date,
        }
        tp_trade_history.append(tp_hist_record)
        if len(tp_trade_history) > TRADE_HISTORY_MAX:
            tp_trade_history[:] = tp_trade_history[-TRADE_HISTORY_MAX:]

        # Fix B: TP EXIT → send_tp_telegram() ONLY
        tp_exit_emoji = "\u2705" if tp_pnl >= 0 else "\u274c"
        tp_entry_cost = round(tp_entry * tp_shares, 2)
        tp_proceeds = round(price * tp_shares, 2)
        tp_reason_label = REASON_LABELS.get(reason, reason)
        if reason == "TRAIL":
            tp_t_high = tp_pos.get("trail_high", price)
            tp_t_dist = max(round(tp_t_high * 0.010, 2), 1.00)
            tp_reason_label = "\U0001f3af Trail Stop (1.0%% / $%.2f)" % tp_t_dist
        tp_msg = (
            "[TP] %s EXIT %s\n"
            "%s\n"
            "Shares : %d\n"
            "Entry  : $%.2f  \u2192  $%.2f\n"
            "Cost   : $%s  \u2192  $%s\n"
            "P&L    : $%+.2f  (%+.1f%%)\n"
            "Reason : %s\n"
            "In: %s   Out: %s\n"
            "%s"
        ) % (tp_exit_emoji, ticker, SEP_X,
             tp_shares, tp_entry, price,
             format(tp_entry_cost, ",.2f"), format(tp_proceeds, ",.2f"),
             tp_pnl, tp_pnl_pct, tp_reason_label, tp_entry_hhmm, now_hhmm, SEP_X)
        send_tp_telegram(tp_msg)
        save_tp_state()

    save_paper_state()


# ============================================================
# MANAGE POSITIONS (stop + trail logic)
# ============================================================
def manage_positions():
    """Check stops and update trailing stops for all open positions."""
    tickers_to_close = []

    # ── Eye of the Tiger: "The Lords have left" ──────────────────────────────
    # Exit all longs if the last COMPLETED 1-min bar close of SPY or QQQ
    # crossed below its AVWAP.  Uses bar close (not live tick) to avoid
    # wick-out ejections.  (Index Regime Shield v2.9.8)
    spy_avwap = avwap_data["SPY"]["avwap"]
    qqq_avwap = avwap_data["QQQ"]["avwap"]
    lords_left = False
    if spy_avwap > 0 and qqq_avwap > 0:
        spy_1min = get_last_1min_close("SPY")
        qqq_1min = get_last_1min_close("QQQ")
        if spy_1min is not None and qqq_1min is not None:
            if spy_1min < spy_avwap or qqq_1min < qqq_avwap:
                lords_left = True
        else:
            logger.warning("SPY/QQQ 1min close unavailable — skipping Tiger check")

    for ticker in list(positions.keys()):
        bars = fetch_1min_bars(ticker)
        if not bars:
            continue

        current_price = bars["current_price"]
        pos = positions[ticker]

        # Check hard stop hit
        if current_price <= pos["stop"]:
            tickers_to_close.append((ticker, current_price, "STOP"))
            continue

        # ── Eye of the Tiger: "The Lords have left" — SPY or QQQ < AVWAP ────
        if lords_left:
            tickers_to_close.append((ticker, current_price, "LORDS_LEFT[1m]"))
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

        # Percentage trail: trigger +1.0%, trail max(price*1.0%, $1.00)
        trail_trigger_price = entry_price * 1.010

        if not pos["trail_active"] and current_price >= trail_trigger_price:
            pos["trail_active"] = True
            pos["trail_high"] = current_price
            logger.info("Trail activated for %s at $%.2f", ticker, current_price)

        if pos["trail_active"]:
            if current_price > pos.get("trail_high", current_price):
                pos["trail_high"] = current_price
            best = pos["trail_high"]
            trail_dist = max(round(best * 0.010, 2), 1.00)
            new_trail_stop = round(best - trail_dist, 2)
            if new_trail_stop > pos.get("trail_stop", 0):
                pos["trail_stop"] = new_trail_stop
            if current_price <= pos["trail_stop"]:
                tickers_to_close.append((ticker, current_price, "TRAIL"))
                continue

    # Close positions outside the loop to avoid mutation during iteration
    for ticker, price, reason in tickers_to_close:
        close_position(ticker, price, reason)


# ============================================================
# CLOSE TP POSITION (independent TP long close)
# ============================================================
def close_tp_position(ticker, price, reason="STOP"):
    """Close a TP long position independently (when paper already closed or diverged)."""
    global tp_paper_cash

    if ticker not in tp_positions:
        return

    _last_exit_time[ticker] = datetime.utcnow()

    tp_pos = tp_positions.pop(ticker)
    tp_entry = tp_pos["entry_price"]
    tp_shares = tp_pos["shares"]
    tp_pnl = (price - tp_entry) * tp_shares
    tp_pnl_pct = ((price - tp_entry) / tp_entry * 100) if tp_entry else 0
    tp_paper_cash += price * tp_shares

    now_et = _now_et()
    now_hhmm = _now_cdt().strftime("%H:%M CDT")
    now_date = now_et.strftime("%Y-%m-%d")
    tp_entry_time_str = tp_pos.get("entry_time", "")
    tp_entry_hhmm = _to_cdt_hhmm(tp_entry_time_str) if tp_entry_time_str else ""

    logger.info("[TP] SELL %s %d @ $%.2f reason=%s pnl=$%.2f",
                ticker, tp_shares, price, reason, tp_pnl)

    tp_paper_trades.append({
        "action": "SELL",
        "ticker": ticker,
        "price": price,
        "shares": tp_shares,
        "pnl": round(tp_pnl, 2),
        "pnl_pct": round(tp_pnl_pct, 2),
        "reason": reason,
        "entry_price": tp_entry,
        "time": now_hhmm,
        "date": now_date,
    })

    tp_trade_history.append({
        "ticker": ticker,
        "side": "long",
        "action": "SELL",
        "shares": tp_shares,
        "entry_price": tp_entry,
        "exit_price": price,
        "pnl": round(tp_pnl, 2),
        "pnl_pct": round(tp_pnl_pct, 2),
        "reason": reason,
        "entry_time": tp_entry_hhmm,
        "exit_time": now_hhmm,
        "entry_time_iso": tp_entry_time_str,
        "exit_time_iso": _utc_now_iso(),
        "entry_num": tp_pos.get("entry_count", 1),
        "date": now_date,
    })
    if len(tp_trade_history) > TRADE_HISTORY_MAX:
        tp_trade_history[:] = tp_trade_history[-TRADE_HISTORY_MAX:]

    SEP_X = "\u2500" * 34
    tp_exit_emoji = "\u2705" if tp_pnl >= 0 else "\u274c"
    tp_entry_cost = round(tp_entry * tp_shares, 2)
    tp_proceeds = round(price * tp_shares, 2)
    tp_reason_label = REASON_LABELS.get(reason, reason)
    tp_msg = (
        "[TP] %s EXIT %s\n"
        "%s\n"
        "Shares : %d\n"
        "Entry  : $%.2f  \u2192  $%.2f\n"
        "Cost   : $%s  \u2192  $%s\n"
        "P&L    : $%+.2f  (%+.1f%%)\n"
        "Reason : %s\n"
        "In: %s   Out: %s\n"
        "%s"
    ) % (tp_exit_emoji, ticker, SEP_X,
         tp_shares, tp_entry, price,
         format(tp_entry_cost, ",.2f"), format(tp_proceeds, ",.2f"),
         tp_pnl, tp_pnl_pct, tp_reason_label, tp_entry_hhmm, now_hhmm, SEP_X)
    send_tp_telegram(tp_msg)
    save_tp_state()


# ============================================================
# MANAGE TP POSITIONS (independent stop + trail logic)
# ============================================================
def manage_tp_positions():
    """Check stops and update trailing stops for all open TP positions."""
    tickers_to_close = []

    # ── Eye of the Tiger: "The Lords have left" ──────────────────────────────
    # Uses last completed 1-min bar close (Index Regime Shield v2.9.8)
    spy_avwap = avwap_data["SPY"]["avwap"]
    qqq_avwap = avwap_data["QQQ"]["avwap"]
    lords_left = False
    if spy_avwap > 0 and qqq_avwap > 0:
        spy_1min = get_last_1min_close("SPY")
        qqq_1min = get_last_1min_close("QQQ")
        if spy_1min is not None and qqq_1min is not None:
            if spy_1min < spy_avwap or qqq_1min < qqq_avwap:
                lords_left = True
        else:
            logger.warning("[TP] SPY/QQQ 1min close unavailable — skipping Tiger check")

    for ticker in list(tp_positions.keys()):
        bars = fetch_1min_bars(ticker)
        if not bars:
            continue

        current_price = bars["current_price"]
        pos = tp_positions[ticker]

        # Check hard stop hit
        if current_price <= pos["stop"]:
            tickers_to_close.append((ticker, current_price, "STOP"))
            continue

        # ── Eye of the Tiger: "The Lords have left" — SPY or QQQ < AVWAP ────
        if lords_left:
            tickers_to_close.append((ticker, current_price, "LORDS_LEFT[1m]"))
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

        # Percentage trail: trigger +1.0%, trail max(price*1.0%, $1.00)
        trail_trigger_price = entry_price * 1.010

        if not pos["trail_active"] and current_price >= trail_trigger_price:
            pos["trail_active"] = True
            pos["trail_high"] = current_price
            logger.info("[TP] Trail activated for %s at $%.2f", ticker, current_price)

        if pos["trail_active"]:
            if current_price > pos.get("trail_high", current_price):
                pos["trail_high"] = current_price
            best = pos["trail_high"]
            trail_dist = max(round(best * 0.010, 2), 1.00)
            new_trail_stop = round(best - trail_dist, 2)
            if new_trail_stop > pos.get("trail_stop", 0):
                pos["trail_stop"] = new_trail_stop
            if current_price <= pos["trail_stop"]:
                tickers_to_close.append((ticker, current_price, "TRAIL"))
                continue

    # Close TP positions outside the loop
    for ticker, price, reason in tickers_to_close:
        close_tp_position(ticker, price, reason)


# ============================================================
# SHORT ENTRY CHECK (Wounded Buffalo)
# ============================================================
def check_short_entry(ticker):
    """Wounded Buffalo: enter short if 1-min close breaks OR_Low with all filters valid."""
    global short_positions, tp_short_positions, daily_short_entry_count
    global paper_cash, tp_paper_cash

    if _trading_halted:
        return

    if _scan_paused:
        return

    now_et = _now_et()

    # Time gate: must be after 09:45 ET (15-min buffer)
    if now_et.hour < 9:
        return
    if now_et.hour == 9 and now_et.minute < 45:
        return

    # Max 5 short entries per ticker per day
    if daily_short_entry_count.get(ticker, 0) >= 5:
        return

    # Re-entry cooldown: 15 min after any exit on this ticker
    last_exit = _last_exit_time.get(ticker)
    if last_exit:
        elapsed = (datetime.utcnow() - last_exit).total_seconds()
        if elapsed < 900:
            mins_left = int((900 - elapsed) / 60) + 1
            logger.info("SKIP %s [COOLDOWN] %dm left", ticker, mins_left)
            return

    # Per-ticker daily loss cap: skip if down > $50 on this ticker today
    ticker_pnl_today = sum(
        (t.get("pnl") or 0) for t in short_trade_history
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
    current_close = closes[-1]
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

    # OR sanity check: OR Low must be within 1.5% of live price
    if not _or_price_sane(or_low_val, current_price):
        pct = abs(or_low_val - current_price) / current_price * 100
        logger.warning(
            "SKIP %s short — OR Low $%.2f is %.1f%% from live $%.2f (stale?)",
            ticker, or_low_val, pct, current_price
        )
        return

    # Volume confirmation: entry bar volume >= 1.5x session average
    volumes = bars.get("volumes", [])
    if len(volumes) >= 5:
        valid_vols = [v for v in volumes[:-1] if v is not None and v > 0]
        avg_vol = sum(valid_vols) / len(valid_vols) if valid_vols else 0
        entry_bar_vol = volumes[-2] if volumes[-2] is not None else 0
        if avg_vol > 0 and entry_bar_vol < avg_vol * 1.5:
            logger.info("SKIP %s [LOW VOL] entry bar %.0f vs avg %.0f", ticker, entry_bar_vol, avg_vol)
            return

    # Entry conditions — ALL must be true:
    # 1. Last 1-min close < OR_Low (breakdown)
    if current_close >= or_low_val:
        return
    # 2. Current price < PDC (polarity — "Red" stock only)
    if current_price >= pdc_val:
        return
    # 3. SPY < SPY_AVWAP
    spy_avwap = avwap_data["SPY"]["avwap"]
    if spy_avwap and spy_avwap > 0:
        spy_bars = fetch_1min_bars("SPY")
        if spy_bars:
            spy_price = spy_bars["current_price"]
            if spy_price >= spy_avwap:
                return
    # 4. QQQ < QQQ_AVWAP
    qqq_avwap = avwap_data["QQQ"]["avwap"]
    if qqq_avwap and qqq_avwap > 0:
        qqq_bars = fetch_1min_bars("QQQ")
        if qqq_bars:
            qqq_price = qqq_bars["current_price"]
            if qqq_price >= qqq_avwap:
                return

    # All checks passed — enter short
    execute_short_entry(ticker, current_price)


# ============================================================
# EXECUTE SHORT ENTRY (Wounded Buffalo)
# ============================================================
def execute_short_entry(ticker, price):
    """Open a paper short position (Wounded Buffalo)."""
    global short_positions, tp_short_positions
    global paper_cash, tp_paper_cash
    global daily_short_entry_count

    shares = 10
    entry_price = round(price, 2)
    pdc_val = pdc.get(ticker, entry_price)
    stop = round(pdc_val + 0.90, 2)   # hard stop: $0.90 ABOVE PDC
    now_et = _now_et()
    entry_time_cdt = _now_cdt().strftime("%H:%M:%S")
    entry_time_display = _now_cdt().strftime("%H:%M CDT")
    date_str = now_et.strftime("%Y-%m-%d")

    # Paper short
    short_positions[ticker] = {
        "entry_price": entry_price,
        "shares": shares,
        "stop": stop,
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

    # TP short (independent)
    tp_short_positions[ticker] = {
        "entry_price": entry_price,
        "shares": shares,
        "stop": stop,
        "trail_stop": None,
        "trail_active": False,
        "trail_low": entry_price,
        "entry_time": entry_time_cdt,
        "date": date_str,
        "side": "SHORT",
    }
    tp_paper_cash += entry_price * shares
    save_tp_state()

    # TradersPost webhook (short entry)
    send_traderspost_order(ticker, "sell_short", entry_price, shares)

    # Notification
    pdc_val = pdc.get(ticker, 0)
    or_low_val = or_low.get(ticker, 0)
    SEP = "\u2500" * 34
    entry_count = daily_short_entry_count.get(ticker, 1)
    short_proceeds = entry_price * shares
    short_sig = "Signal   : Wounded Buffalo \u2193\n"
    short_sig += "  1m close < OR Low \u2713\n"
    short_sig += "  Price < PDC \u2713\n"
    short_sig += "  SPY < AVWAP \u2713\n"
    short_sig += "  QQQ < AVWAP \u2713\n"
    msg = (
        "\U0001fa78 SHORT ENTRY #%d\n"
        "%s\n"
        "Ticker   : %s\n"
        "Entry    : $%.2f (limit)\n"
        "Shares   : %d   Proceeds: $%s\n"
        "Stop     : $%.2f (PDC+$0.90)\n"
        "OR Low   : $%.2f\n"
        "PDC      : $%.2f\n"
        "%s"
        "Time     : %s\n"
        "%s"
    ) % (entry_count, SEP, ticker, entry_price,
         shares, format(short_proceeds, ",.2f"),
         stop, or_low_val, pdc_val, short_sig, entry_time_display, SEP)
    send_telegram(msg)

    tp_msg = msg.replace("SHORT ENTRY", "TP SHORT ENTRY")
    send_tp_telegram(tp_msg)


# ============================================================
# MANAGE SHORT POSITIONS (stop + trail logic)
# ============================================================
def manage_short_positions():
    """Check stops and trailing stops for all open short positions."""
    global short_positions, tp_short_positions

    # ── Eye of the Tiger: "The Bullish Vacuum" ───────────────────────────────
    # Exit all shorts if the last COMPLETED 1-min bar close of SPY or QQQ
    # crossed above its AVWAP.  Uses bar close (not live tick) to avoid
    # wick-out ejections.  (Index Regime Shield v2.9.8)
    spy_avwap = avwap_data["SPY"]["avwap"]
    qqq_avwap = avwap_data["QQQ"]["avwap"]
    bull_vacuum = False
    if spy_avwap > 0 and qqq_avwap > 0:
        spy_1min = get_last_1min_close("SPY")
        qqq_1min = get_last_1min_close("QQQ")
        if spy_1min is not None and qqq_1min is not None:
            if spy_1min > spy_avwap or qqq_1min > qqq_avwap:
                bull_vacuum = True
        else:
            logger.warning("SPY/QQQ 1min close unavailable — skipping Tiger check")

    for ticker in list(short_positions.keys()):
        pos = short_positions[ticker]
        entry_price = pos["entry_price"]
        shares = pos["shares"]
        stop = pos["stop"]
        trail_stop = pos.get("trail_stop")
        trail_active = pos.get("trail_active", False)

        bars = fetch_1min_bars(ticker)
        if not bars:
            continue
        current_price = bars["current_price"]

        # Percentage trail: trigger -1.0%, trail max(price*1.0%, $1.00)
        trail_trigger_price = entry_price * 0.990

        if not trail_active and current_price <= trail_trigger_price:
            trail_active = True
            short_positions[ticker]["trail_active"] = True
            short_positions[ticker]["trail_low"] = current_price

        if trail_active:
            trail_low = short_positions[ticker].get("trail_low", current_price)
            if current_price < trail_low:
                trail_low = current_price
                short_positions[ticker]["trail_low"] = trail_low
            trail_dist = max(round(trail_low * 0.010, 2), 1.00)
            new_trail_stop = round(trail_low + trail_dist, 2)
            old_trail_stop = short_positions[ticker].get("trail_stop")
            if old_trail_stop is None or new_trail_stop < old_trail_stop:
                short_positions[ticker]["trail_stop"] = new_trail_stop
            trail_stop = short_positions[ticker]["trail_stop"]

        # Check stop conditions
        exit_reason = None
        if trail_active and trail_stop is not None:
            if current_price >= trail_stop:
                exit_reason = "TRAIL"
        else:
            if current_price >= stop:
                exit_reason = "STOP"

        # ── Eye of the Tiger: "The Bullish Vacuum" — SPY or QQQ > AVWAP ─────
        if not exit_reason and bull_vacuum:
            exit_reason = "BULL_VACUUM[1m]"

        # ── Eye of the Tiger: "The Polarity Shift" — Price > PDC ─────────────
        # Uses completed 1m bar close (same pattern as Lords Left / Bull Vacuum)
        if not exit_reason:
            ticker_pdc = pdc.get(ticker, 0)
            if ticker_pdc > 0:
                ps_closes = [c for c in bars.get("closes", []) if c is not None]
                ps_1min_close = ps_closes[-2] if len(ps_closes) >= 2 else (ps_closes[-1] if ps_closes else current_price)
                if ps_1min_close > ticker_pdc:
                    exit_reason = "POLARITY_SHIFT"

        if exit_reason:
            close_short_position(ticker, current_price, exit_reason, portfolio="paper")

    # Same logic for TP short positions
    for ticker in list(tp_short_positions.keys()):
        pos = tp_short_positions[ticker]
        entry_price = pos["entry_price"]
        shares = pos["shares"]
        stop = pos["stop"]
        trail_stop = pos.get("trail_stop")
        trail_active = pos.get("trail_active", False)

        bars = fetch_1min_bars(ticker)
        if not bars:
            continue
        current_price = bars["current_price"]

        # Percentage trail: trigger -1.0%, trail max(price*1.0%, $1.00)
        trail_trigger_price = entry_price * 0.990

        if not trail_active and current_price <= trail_trigger_price:
            trail_active = True
            tp_short_positions[ticker]["trail_active"] = True
            tp_short_positions[ticker]["trail_low"] = current_price

        if trail_active:
            trail_low = tp_short_positions[ticker].get("trail_low", current_price)
            if current_price < trail_low:
                trail_low = current_price
                tp_short_positions[ticker]["trail_low"] = trail_low
            trail_dist = max(round(trail_low * 0.010, 2), 1.00)
            new_trail_stop = round(trail_low + trail_dist, 2)
            old_trail_stop = tp_short_positions[ticker].get("trail_stop")
            if old_trail_stop is None or new_trail_stop < old_trail_stop:
                tp_short_positions[ticker]["trail_stop"] = new_trail_stop
            trail_stop = tp_short_positions[ticker]["trail_stop"]

        exit_reason = None
        if trail_active and trail_stop is not None:
            if current_price >= trail_stop:
                exit_reason = "TRAIL"
        else:
            if current_price >= stop:
                exit_reason = "STOP"

        # ── Eye of the Tiger: "The Bullish Vacuum" — SPY or QQQ > AVWAP ─────
        if not exit_reason and bull_vacuum:
            exit_reason = "BULL_VACUUM[1m]"

        # ── Eye of the Tiger: "The Polarity Shift" — Price > PDC ─────────────
        # Uses completed 1m bar close (same pattern as Lords Left / Bull Vacuum)
        if not exit_reason:
            ticker_pdc = pdc.get(ticker, 0)
            if ticker_pdc > 0:
                ps_closes = [c for c in bars.get("closes", []) if c is not None]
                ps_1min_close = ps_closes[-2] if len(ps_closes) >= 2 else (ps_closes[-1] if ps_closes else current_price)
                if ps_1min_close > ticker_pdc:
                    exit_reason = "POLARITY_SHIFT"

        if exit_reason:
            close_short_position(ticker, current_price, exit_reason, portfolio="tp")


# ============================================================
# CLOSE SHORT POSITION
# ============================================================
def close_short_position(ticker, price, reason, portfolio="paper"):
    """Cover a short position and record the trade."""
    global short_positions, tp_short_positions
    global paper_cash, tp_paper_cash
    global short_trade_history, tp_short_trade_history

    if portfolio == "paper":
        pos = short_positions.pop(ticker, None)
    else:
        pos = tp_short_positions.pop(ticker, None)

    if not pos:
        return

    _last_exit_time[ticker] = datetime.utcnow()

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

    if portfolio == "paper":
        paper_cash -= cover_price * shares
        short_trade_history.append(trade_record)
        if len(short_trade_history) > 500:
            short_trade_history.pop(0)
        save_paper_state()

        # TradersPost webhook
        send_traderspost_order(ticker, "buy_to_cover", cover_price, shares)

        # Notification
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

    else:  # TP
        tp_paper_cash -= cover_price * shares
        tp_short_trade_history.append(trade_record)
        if len(tp_short_trade_history) > 500:
            tp_short_trade_history.pop(0)
        save_tp_state()

        pnl_sign = "+" if pnl >= 0 else ""
        emoji = "\u2705" if pnl >= 0 else "\u274c"
        SEP = "\u2500" * 34
        tp_sc_entry_total = round(entry_price * shares, 2)
        tp_sc_cover_total = round(cover_price * shares, 2)
        tp_sc_in_time = _to_cdt_hhmm(pos.get("entry_time", ""))
        tp_sc_reason_label = REASON_LABELS.get(reason, reason)
        if reason == "TRAIL":
            tp_sc_t_low = pos.get("trail_low", cover_price)
            tp_sc_t_dist = max(round(tp_sc_t_low * 0.010, 2), 1.00)
            tp_sc_reason_label = "\U0001f3af Trail Stop (1.0%% / $%.2f)" % tp_sc_t_dist
        tp_msg = (
            "%s TP SHORT CLOSED\n"
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
             entry_price, format(tp_sc_entry_total, ",.2f"),
             cover_price, format(tp_sc_cover_total, ",.2f"),
             pnl_sign, pnl, pnl_sign, pnl_pct,
             tp_sc_reason_label, tp_sc_in_time, exit_time, SEP)
        send_tp_telegram(tp_msg)


# ============================================================
# EOD CLOSE
# ============================================================
def eod_close():
    """Force-close all open long AND short positions at 15:55 ET."""
    n_long = len(positions)
    n_short = len(short_positions)
    n_tp_short = len(tp_short_positions)

    if not positions and not tp_positions and not short_positions and not tp_short_positions:
        logger.info("EOD close: no open positions (long or short)")
        # Still fall through to show summary

    # Close long positions
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

    # Close any remaining TP long positions (orphaned if paper already closed)
    if tp_positions:
        logger.info("EOD close: closing %d TP long positions", len(tp_positions))
        tp_longs_to_close = []
        for ticker in list(tp_positions.keys()):
            bars = fetch_1min_bars(ticker)
            if bars:
                price = bars["current_price"]
            else:
                price = tp_positions[ticker]["entry_price"]
            tp_longs_to_close.append((ticker, price))
        for ticker, price in tp_longs_to_close:
            close_tp_position(ticker, price, reason="EOD")

    # Close paper short positions
    if short_positions:
        logger.info("EOD close: closing %d paper short positions", n_short)
        shorts_to_close = []
        for ticker in list(short_positions.keys()):
            bars = fetch_1min_bars(ticker)
            if bars:
                price = bars["current_price"]
            else:
                price = short_positions[ticker]["entry_price"]
            shorts_to_close.append((ticker, price))
        for ticker, price in shorts_to_close:
            close_short_position(ticker, price, "EOD", portfolio="paper")

    # Close TP short positions
    if tp_short_positions:
        logger.info("EOD close: closing %d TP short positions", n_tp_short)
        tp_shorts_to_close = []
        for ticker in list(tp_short_positions.keys()):
            bars = fetch_1min_bars(ticker)
            if bars:
                price = bars["current_price"]
            else:
                price = tp_short_positions[ticker]["entry_price"]
            tp_shorts_to_close.append((ticker, price))
        for ticker, price in tp_shorts_to_close:
            close_short_position(ticker, price, "EOD", portfolio="tp")

    # Fix B: Paper EOD summary → send_telegram(), TP EOD summary → send_tp_telegram()
    now_et = _now_et()
    today = now_et.strftime("%Y-%m-%d")
    today_sells = [t for t in paper_trades
                   if t.get("action") == "SELL" and t.get("date", "") == today]
    total_pnl = sum(t.get("pnl", 0) for t in today_sells)
    wins = sum(1 for t in today_sells if t.get("pnl", 0) >= 0)
    losses = sum(1 for t in today_sells if t.get("pnl", 0) < 0)

    msg = (
        f"EOD CLOSE Complete\n"
        f"  Trades: {len(today_sells)}  W/L: {wins}/{losses}\n"
        f"  Day P&L: ${total_pnl:+.2f}\n"
        f"  Cash: ${paper_cash:,.2f}"
    )
    send_telegram(msg)

    # TP EOD summary
    tp_today_sells = [
        t for t in tp_paper_trades
        if t.get("action") == "SELL" and t.get("date", "") == today
    ]
    tp_total_pnl = sum(t.get("pnl", 0) for t in tp_today_sells)
    tp_wins = sum(1 for t in tp_today_sells if t.get("pnl", 0) >= 0)
    tp_losses = sum(1 for t in tp_today_sells if t.get("pnl", 0) < 0)
    tp_msg = (
        f"[TP] EOD CLOSE Complete\n"
        f"  Trades: {len(tp_today_sells)}  W/L: {tp_wins}/{tp_losses}\n"
        f"  Day P&L: ${tp_total_pnl:+.2f}\n"
        f"  Cash: ${tp_paper_cash:,.2f}"
    )
    send_tp_telegram(tp_msg)
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

        # SPY/QQQ AVWAP
        spy_bars = fetch_1min_bars("SPY")
        qqq_bars = fetch_1min_bars("QQQ")
        spy_price = spy_bars["current_price"] if spy_bars else 0
        qqq_price = qqq_bars["current_price"] if qqq_bars else 0
        spy_avwap = avwap_data["SPY"]["avwap"]
        qqq_avwap = avwap_data["QQQ"]["avwap"]

        spy_above = spy_price > spy_avwap if spy_avwap > 0 else False
        qqq_above = qqq_price > qqq_avwap if qqq_avwap > 0 else False
        spy_icon = "\u2705 above" if spy_above else "\u274c below"
        qqq_icon = "\u2705 above" if qqq_above else "\u274c below"

        spy_avwap_fmt = "%.2f" % spy_avwap if spy_avwap > 0 else "n/a"
        qqq_avwap_fmt = "%.2f" % qqq_avwap if qqq_avwap > 0 else "n/a"

        lines.append("SPY AVWAP: $%s  %s" % (spy_avwap_fmt, spy_icon))
        lines.append("QQQ AVWAP: $%s  %s" % (qqq_avwap_fmt, qqq_icon))

        both_active = spy_above and qqq_above
        both_bearish = (not spy_above) and (not qqq_above)
        filter_status = "LONG ACTIVE" if both_active else ("SHORT ACTIVE" if both_bearish else "PARTIAL/INACTIVE")
        lines.append("Index filters: %s" % filter_status)
        lines.append(SEP)
        lines.append("Watching for breakouts (long) and breakdowns (short).")

        msg = "\n".join(lines)
        send_telegram(msg)
        send_tp_telegram(msg)

    threading.Thread(target=_do_send, daemon=True).start()


# ============================================================
# AUTO EOD REPORT (Feature 4)
# ============================================================
def send_eod_report():
    """Auto EOD report at 15:58 ET. Paper → send_telegram(), TP → send_tp_telegram()."""
    SEP = "\u2500" * 34
    now_et = _now_et()
    today = now_et.strftime("%Y-%m-%d")

    # --- Paper report ---
    today_sells = [
        t for t in paper_trades
        if t.get("action") == "SELL"
    ]
    n_trades = len(today_sells)
    wins = sum(1 for t in today_sells if t.get("pnl", 0) >= 0)
    losses = n_trades - wins
    win_rate = (wins / n_trades * 100) if n_trades > 0 else 0
    day_pnl = sum(t.get("pnl", 0) for t in today_sells)

    # All-time from trade_history
    all_time_pnl = sum(t.get("pnl", 0) for t in trade_history)
    all_wins = sum(1 for t in trade_history if t.get("pnl", 0) >= 0)
    all_losses = len(trade_history) - all_wins
    all_wr = (all_wins / len(trade_history) * 100) if trade_history else 0

    lines = [
        "\U0001f4ca EOD Report \u2014 %s" % today,
        SEP,
        "PAPER PORTFOLIO",
        "  Trades today:  %d" % n_trades,
        "  Wins / Losses: %d / %d" % (wins, losses),
        "  Win Rate:      %.1f%%" % win_rate,
        "  Day P&L:      $%+.2f" % day_pnl,
        SEP,
    ]
    for t in today_sells:
        tk = t.get("ticker", "?")
        sh = t.get("shares", 0)
        t_pnl = t.get("pnl", 0)
        t_pct = t.get("pnl_pct", 0)
        t_reason = t.get("reason", "?")
        lines.append("  %s  %dsh  $%+.2f (%+.1f%%)  %s" % (tk, sh, t_pnl, t_pct, t_reason))
    lines.append(SEP)
    lines.append("  All-time P&L:  $%+.2f" % all_time_pnl)
    lines.append("  All-time W/L:  %d / %d  (%.1f%%)" % (all_wins, all_losses, all_wr))

    paper_msg = "\n".join(lines)
    if len(paper_msg) > 4000:
        paper_msg = paper_msg[:3990] + "\n... (truncated)"
    send_telegram(paper_msg)

    # --- TP report ---
    tp_today_sells = [
        t for t in tp_paper_trades
        if t.get("action") == "SELL" and t.get("date", "") == today
    ]
    tp_n = len(tp_today_sells)
    tp_wins = sum(1 for t in tp_today_sells if t.get("pnl", 0) >= 0)
    tp_losses_n = tp_n - tp_wins
    tp_wr = (tp_wins / tp_n * 100) if tp_n > 0 else 0
    tp_day_pnl = sum(t.get("pnl", 0) for t in tp_today_sells)

    tp_all_pnl = sum(t.get("pnl", 0) for t in tp_trade_history)
    tp_all_wins = sum(1 for t in tp_trade_history if t.get("pnl", 0) >= 0)
    tp_all_losses = len(tp_trade_history) - tp_all_wins
    tp_all_wr = (tp_all_wins / len(tp_trade_history) * 100) if tp_trade_history else 0

    tp_lines = [
        "\U0001f4ca EOD Report \u2014 %s" % today,
        SEP,
        "TP PORTFOLIO",
        "  Trades today:  %d" % tp_n,
        "  Wins / Losses: %d / %d" % (tp_wins, tp_losses_n),
        "  Win Rate:      %.1f%%" % tp_wr,
        "  Day P&L:      $%+.2f" % tp_day_pnl,
        SEP,
    ]
    for t in tp_today_sells:
        tk = t.get("ticker", "?")
        sh = t.get("shares", 0)
        t_pnl = t.get("pnl", 0)
        t_pct = t.get("pnl_pct", 0)
        t_reason = t.get("reason", "?")
        tp_lines.append("  %s  %dsh  $%+.2f (%+.1f%%)  %s" % (tk, sh, t_pnl, t_pct, t_reason))
    tp_lines.append(SEP)
    tp_lines.append("  All-time P&L:  $%+.2f" % tp_all_pnl)
    tp_lines.append("  All-time W/L:  %d / %d  (%.1f%%)" % (tp_all_wins, tp_all_losses, tp_all_wr))

    tp_report_msg = "\n".join(tp_lines)
    if len(tp_report_msg) > 4000:
        tp_report_msg = tp_report_msg[:3990] + "\n... (truncated)"
    send_tp_telegram(tp_report_msg)


# ============================================================
# WEEKLY DIGEST (Feature 9)
# ============================================================
def send_weekly_digest():
    """Weekly digest — Sunday 18:00 ET. Paper → send_telegram(), TP → send_tp_telegram()."""
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

    paper_digest = _build_digest(trade_history, "PAPER PORTFOLIO")
    send_telegram(paper_digest)

    tp_digest = _build_digest(tp_trade_history, "TP PORTFOLIO")
    send_tp_telegram(tp_digest)


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
        with open(TP_STATE_FILE, "r", encoding="utf-8") as f:
            ts = json.load(f)
        p_cash = ps.get("paper_cash", 0)
        t_cash = ts.get("tp_paper_cash", 0)
        lines.append(
            "State: \u2705 paper $%s | TP $%s"
            % (format(int(p_cash), ","), format(int(t_cash), ","))
        )
    except Exception as exc:
        issues += 1
        lines.append("State: \u274c %s" % exc)

    # D. Positions count
    n_paper = len(positions) + len(short_positions)
    n_tp = len(tp_positions) + len(tp_short_positions)
    lines.append("Pos: %d paper | %d TP" % (n_paper, n_tp))

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
    send_tp_telegram(msg)


def _fire_system_test(label: str) -> None:
    """Sync wrapper to fire _run_system_test_sync from scheduler thread."""
    try:
        _run_system_test_sync(label)
    except Exception as exc:
        logger.error("System test (%s) failed: %s", label, exc, exc_info=True)


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /test command — run system health test."""
    await update.message.reply_chat_action(ChatAction.TYPING)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _run_system_test_sync, "Manual")


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

    global _last_scan_time
    _last_scan_time = datetime.now(timezone.utc)

    n_pos = len(positions)
    n_tp = len(tp_positions)
    n_short = len(short_positions)
    n_tp_short = len(tp_short_positions)
    logger.info("Scanning %d stocks | pos=%d tp=%d short=%d tp_short=%d",
                len(TRADE_TICKERS), n_pos, n_tp, n_short, n_tp_short)

    # Update AVWAP for index anchors
    update_avwap("SPY")
    update_avwap("QQQ")

    # ── Regime change alert ───────────────────────────────────────────────
    global _regime_bullish
    spy_avwap_r = avwap_data["SPY"]["avwap"]
    qqq_avwap_r = avwap_data["QQQ"]["avwap"]
    if spy_avwap_r > 0 and qqq_avwap_r > 0:
        spy_bars_r = fetch_1min_bars("SPY")
        qqq_bars_r = fetch_1min_bars("QQQ")
        if spy_bars_r and qqq_bars_r:
            spy_cur_r = spy_bars_r["current_price"]
            qqq_cur_r = qqq_bars_r["current_price"]
            now_bullish = (spy_cur_r > spy_avwap_r) and (qqq_cur_r > qqq_avwap_r)
            if _regime_bullish is None:
                _regime_bullish = now_bullish
            elif now_bullish != _regime_bullish:
                _regime_bullish = now_bullish
                now_hhmm_r = _now_cdt().strftime("%H:%M CDT")
                if now_bullish:
                    regime_msg = (
                        "\U0001f7e2 REGIME: BULLISH\n"
                        "SPY $%.2f > AVWAP $%.2f\n"
                        "QQQ $%.2f > AVWAP $%.2f\n"
                        "The Lords are back.  %s"
                    ) % (spy_cur_r, spy_avwap_r, qqq_cur_r, qqq_avwap_r, now_hhmm_r)
                else:
                    regime_msg = (
                        "\U0001f534 REGIME: BEARISH\n"
                        "SPY $%.2f < AVWAP $%.2f\n"
                        "QQQ $%.2f < AVWAP $%.2f\n"
                        "The Lords have left.  %s"
                    ) % (spy_cur_r, spy_avwap_r, qqq_cur_r, qqq_avwap_r, now_hhmm_r)
                send_telegram(regime_msg)
                send_tp_telegram(regime_msg)

    # Always manage existing positions (stops/trails) even when paused
    try:
        manage_positions()
    except Exception as e:
        logger.error("manage_positions crashed: %s", e, exc_info=True)
        err_msg = "⚠️ Bot error in manage_positions: %s" % str(e)[:200]
        send_telegram(err_msg)
        send_tp_telegram(err_msg)
    try:
        manage_tp_positions()
    except Exception as e:
        logger.error("manage_tp_positions crashed: %s", e, exc_info=True)
        err_msg = "⚠️ Bot error in manage_tp_positions: %s" % str(e)[:200]
        send_telegram(err_msg)
        send_tp_telegram(err_msg)
    try:
        manage_short_positions()
    except Exception as e:
        logger.error("manage_short_positions crashed: %s", e, exc_info=True)
        err_msg = "⚠️ Bot error in manage_short_positions: %s" % str(e)[:200]
        send_telegram(err_msg)
        send_tp_telegram(err_msg)

    # Feature 8: scan pause — only block NEW entries
    if _scan_paused:
        return

    # Check for new entries on tradable tickers (long + short)
    for ticker in TRADE_TICKERS:
        # Long entry check
        if ticker not in positions:
            try:
                ok, bars = check_entry(ticker)
                if ok and bars:
                    execute_entry(ticker, bars["current_price"])
            except Exception as e:
                logger.error("Entry check error %s: %s", ticker, e)
        # Short entry check (Wounded Buffalo)
        try:
            check_short_entry(ticker)
        except Exception as e:
            logger.error("Short entry check error %s: %s", ticker, e)


# ============================================================
# RESET DAILY STATE
# ============================================================
def reset_daily_state():
    """Reset AVWAP, OR data, and daily counts for new trading day."""
    global or_collected_date, daily_entry_date, _trading_halted, _trading_halted_reason, tp_paper_trades
    global daily_short_entry_count

    now_et = _now_et()
    today = now_et.strftime("%Y-%m-%d")

    if or_collected_date != today:
        or_high.clear()
        or_low.clear()
        pdc.clear()
        or_collected_date = ""

    if daily_entry_date != today:
        daily_entry_count.clear()
        daily_short_entry_count.clear()
        paper_trades.clear()
        tp_paper_trades.clear()
        save_tp_state()
        daily_entry_date = today

    # Reset AVWAP
    for t in ("SPY", "QQQ"):
        avwap_data[t] = {"cum_pv": 0.0, "cum_vol": 0.0, "avwap": 0.0}
        avwap_last_ts[t] = 0

    # Feature 2: Reset trading halt for new day
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
    """Show categorized command list."""
    SEP = "\u2500" * 30
    text = (
        "\U0001f4d6 Commands\n"
        f"{SEP}\n"
        "\U0001f4ca Portfolio\n"
        "  /dashboard   Full market snapshot\n"
        "  /positions   Open positions + live P&L\n"
        "  /perf [date] Performance stats (optional date)\n"
        "\n"
        "\U0001f4c8 Market Data\n"
        "  /price TICK  Live quote for a ticker\n"
        "  /orb         Today's OR levels\n"
        "\n"
        "\U0001f4c5 Reports\n"
        "  /dayreport [date]  Trades + P&L (optional date)\n"
        "  /log [date]        Trade log (optional date)\n"
        "  /replay [date]     Trade timeline (optional date)\n"
        "\n"
        "\u2699\ufe0f System\n"
        "  /monitoring  Pause/resume scanner\n"
        "  /test        System health check\n"
        "  /menu        Quick tap menu\n"
        "\n"
        "\U0001f4d8 Reference\n"
        "  /strategy    Strategy summary\n"
        "  /algo        Algorithm reference PDF\n"
        "  /version     Release notes\n"
        "\n"
        "\U0001f527 Admin\n"
        "  /reset       Reset portfolio\n"
        f"{SEP}"
    )
    await update.message.reply_text(text)


def _dashboard_sync(is_tp):
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
    spy_avwap = avwap_data["SPY"]["avwap"]
    qqq_avwap = avwap_data["QQQ"]["avwap"]
    spy_ok = spy_price > spy_avwap if spy_avwap > 0 else False
    qqq_ok = qqq_price > qqq_avwap if qqq_avwap > 0 else False
    spy_icon = "\u2705" if spy_ok else "\u274c"
    qqq_icon = "\u2705" if qqq_ok else "\u274c"

    lines = [
        "\U0001f4ca DASHBOARD  %s" % time_cdt,
        SEP,
    ]

    if is_tp:
        # TP portfolio only
        n_tp_pos = len(tp_positions)
        tp_today_sells = [t for t in tp_paper_trades
                          if t.get("action") == "SELL" and t.get("pnl") is not None
                          and t.get("date", "") == today]
        tp_day_pnl = sum(t.get("pnl", 0) for t in tp_today_sells)
        tp_cash_fmt = "%s" % format(tp_paper_cash, ",.2f")
        tp_day_pnl_fmt = "%s" % format(tp_day_pnl, "+,.2f")
        lines += [
            "\U0001f4cb TP PORTFOLIO",
            "  Cash:       $%s" % tp_cash_fmt,
            "  Positions:  %d open" % n_tp_pos,
            "  Today P&L:  $%s" % tp_day_pnl_fmt,
        ]
    else:
        # Paper portfolio only
        n_pos = len(positions)
        today_sells = [t for t in paper_trades
                       if t.get("action") == "SELL" and t.get("pnl") is not None]
        day_pnl = sum(t.get("pnl", 0) for t in today_sells)

        total_value = paper_cash
        for ticker, pos in positions.items():
            bars = fetch_1min_bars(ticker)
            if bars:
                total_value += bars["current_price"] * pos["shares"]
            else:
                total_value += pos["entry_price"] * pos["shares"]

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
        "  SPY  $%.2f  AVWAP $%.2f  %s" % (spy_price, spy_avwap, spy_icon),
        "  QQQ  $%.2f  AVWAP $%.2f  %s" % (qqq_price, qqq_avwap, qqq_icon),
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
    await update.message.reply_chat_action(ChatAction.TYPING)
    is_tp = is_tp_update(update)
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, _dashboard_sync, is_tp)
    await update.message.reply_text(text)


def _status_text_sync(is_tp):
    """Build full status text (blocking I/O — run in executor)."""
    now_et = _now_et()
    sep = "\u2500" * 34

    if is_tp:
        # Show TP portfolio
        n_pos = len(tp_positions)
        header = "[TP] Open Positions (%d)" % n_pos
        lines = [header, sep]

        total_unreal_pnl = 0.0
        total_market_value = 0.0

        if not tp_positions:
            lines.append("No open positions")
        else:
            for ticker, pos in tp_positions.items():
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
                    lines.append("  Entry:  $%.2f  ->  Now: $%.2f" % (entry_p, cur))
                    lines.append("  P&L:   $%+.2f (%+.1f%%)" % (pos_pnl, pos_pnl_pct))
                    mkt_val_fmt = format(mkt_val, ",.2f")
                    lines.append("  Value:  $%s" % mkt_val_fmt)
                    lines.append(stop_line)
                else:
                    mkt_val = entry_p * shares
                    total_market_value += mkt_val
                    lines.append("%s  %d shares" % (ticker, shares))
                    lines.append("  Entry:  $%.2f  ->  price unavailable" % entry_p)
                    lines.append("  Stop:   $%.2f" % pos["stop"])
                lines.append(sep)

        if tp_positions:
            lines.append("Total Unrealized P&L: $%+.2f" % total_unreal_pnl)
            tmv_fmt = format(total_market_value, ",.2f")
            lines.append("Total Market Value:   $%s" % tmv_fmt)

        today = now_et.strftime("%Y-%m-%d")
        tp_today_sells = [
            t for t in tp_paper_trades
            if t.get("action") == "SELL" and t.get("date") == today
        ]
        tp_short_today = [t for t in tp_short_trade_history if t.get("date") == today]
        tp_day_pnl = (sum(t.get("pnl", 0) for t in tp_today_sells)
                      + sum(t.get("pnl", 0) for t in tp_short_today))
        tp_day_trades = len(tp_today_sells) + len(tp_short_today)
        lines.append("Day P&L: $%+.2f  (%d trades)" % (tp_day_pnl, tp_day_trades))

        # TP Short positions
        lines.append(sep)
        lines.append("\U0001fa78 SHORT POSITIONS (Wounded Buffalo)")
        lines.append(sep)
        if not tp_short_positions:
            lines.append("No short positions open.")
        else:
            for s_ticker, s_pos in tp_short_positions.items():
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

        tp_cash_fmt = format(tp_paper_cash, ",.2f")
        lines.append("TP Cash: $%s" % tp_cash_fmt)

        # Portfolio equity summary
        tp_short_unreal = 0.0
        tp_short_mkt_val = 0.0
        for s_ticker, s_pos in tp_short_positions.items():
            s_bars = fetch_1min_bars(s_ticker)
            if s_bars:
                tp_short_unreal += (s_pos["entry_price"] - s_bars["current_price"]) * s_pos["shares"]
            tp_short_mkt_val += s_pos["entry_price"] * s_pos["shares"]
        tp_all_unreal = total_unreal_pnl + tp_short_unreal
        tp_all_mkt_val = total_market_value + tp_short_mkt_val
        tp_equity = tp_paper_cash + tp_all_mkt_val
        tp_vs_start = tp_equity - PAPER_STARTING_CAPITAL
        lines.append(sep)
        lines.append("\U0001f4bc Portfolio Snapshot")
        lines.append("  Cash:          $%s" % format(tp_paper_cash, ",.2f"))
        lines.append("  Market Value:  $%s" % format(tp_all_mkt_val, ",.2f"))
        lines.append("  Total Equity:  $%s" % format(tp_equity, ",.2f"))
        lines.append("  Unrealized P&L:    $%+.2f" % tp_all_unreal)
        lines.append("  vs Start:        $%+.2f  (started at $%s)"
                     % (tp_vs_start, format(PAPER_STARTING_CAPITAL, ",.0f")))
        lines.append(sep)
        return "\n".join(lines)

    # Paper portfolio (default)
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

    # Portfolio equity summary
    short_unreal = 0.0
    short_mkt_val = 0.0
    for s_ticker, s_pos in short_positions.items():
        s_bars = fetch_1min_bars(s_ticker)
        if s_bars:
            short_unreal += (s_pos["entry_price"] - s_bars["current_price"]) * s_pos["shares"]
        short_mkt_val += s_pos["entry_price"] * s_pos["shares"]
    all_unreal = total_unreal_pnl + short_unreal
    all_mkt_val = total_market_value + short_mkt_val
    equity = paper_cash + all_mkt_val
    vs_start = equity - PAPER_STARTING_CAPITAL
    lines.append(sep)
    lines.append("\U0001f4bc Portfolio Snapshot")
    lines.append("  Cash:          $%s" % format(paper_cash, ",.2f"))
    lines.append("  Market Value:  $%s" % format(all_mkt_val, ",.2f"))
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

    # AVWAP status
    spy_avwap = avwap_data["SPY"]["avwap"]
    qqq_avwap = avwap_data["QQQ"]["avwap"]
    if spy_avwap > 0:
        lines.append("SPY AVWAP: $%.2f" % spy_avwap)
    if qqq_avwap > 0:
        lines.append("QQQ AVWAP: $%.2f" % qqq_avwap)

    return "\n".join(lines)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show open positions with live prices, unrealized P&L, and TP summary."""
    await update.message.reply_chat_action(ChatAction.TYPING)
    is_tp = is_tp_update(update)
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, _status_text_sync, is_tp)

    refresh_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("\U0001f504 Refresh", callback_data="positions_refresh")
    ]])
    await update.message.reply_text(text, reply_markup=refresh_kb)

    # Portfolio pie chart (run in thread to avoid blocking event loop)
    if is_tp:
        if MATPLOTLIB_AVAILABLE and (tp_positions or tp_short_positions):
            buf = await loop.run_in_executor(None, _chart_portfolio_pie, tp_positions, tp_short_positions, tp_paper_cash)
            if buf:
                await update.message.reply_photo(photo=buf, caption="TP Portfolio Allocation")
    else:
        if MATPLOTLIB_AVAILABLE and (positions or short_positions):
            buf = await loop.run_in_executor(None, _chart_portfolio_pie, positions, short_positions, paper_cash)
            if buf:
                await update.message.reply_photo(photo=buf, caption="Portfolio Allocation")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias for /status."""
    await cmd_status(update, context)


def _build_positions_text(is_tp=False):
    """Build positions text for refresh callback."""
    now_et = _now_et()
    sep = "\u2500" * 34
    if is_tp:
        pos_dict = tp_positions
        short_dict = tp_short_positions
        trades_list = tp_paper_trades
        short_hist = tp_short_trade_history
        cash = tp_paper_cash
        label = "[TP] Open Positions"
        cash_label = "TP Cash"
    else:
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

    # Portfolio equity summary
    short_unreal = 0.0
    short_mkt_val = 0.0
    for s_ticker, s_pos in short_dict.items():
        s_bars = fetch_1min_bars(s_ticker)
        if s_bars:
            short_unreal += (s_pos["entry_price"] - s_bars["current_price"]) * s_pos["shares"]
        short_mkt_val += s_pos["entry_price"] * s_pos["shares"]
    all_unreal = total_unreal + short_unreal
    all_mkt_val = total_market_value + short_mkt_val
    equity = cash + all_mkt_val
    vs_start = equity - PAPER_STARTING_CAPITAL
    lines.append(sep)
    lines.append("\U0001f4bc Portfolio Snapshot")
    lines.append("  Cash:          $%s" % format(cash, ",.2f"))
    lines.append("  Market Value:  $%s" % format(all_mkt_val, ",.2f"))
    lines.append("  Total Equity:  $%s" % format(equity, ",.2f"))
    lines.append("  Unrealized P&L:    $%+.2f" % all_unreal)
    lines.append("  vs Start:        $%+.2f  (started at $%s)"
                 % (vs_start, format(PAPER_STARTING_CAPITAL, ",.0f")))
    lines.append(sep)

    return "\n".join(lines)


async def positions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle refresh button tap on /positions."""
    query = update.callback_query
    await query.answer("Refreshing...")
    is_tp = (str(query.message.chat_id) == TELEGRAM_TP_CHAT_ID)
    loop = asyncio.get_event_loop()
    msg = await loop.run_in_executor(None, _build_positions_text, is_tp)
    refresh_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("\U0001f504 Refresh", callback_data="positions_refresh")
    ]])
    await query.edit_message_text(msg, reply_markup=refresh_kb)


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


def _format_dayreport_section(trades, header, count_label):
    """Format one portfolio section for /dayreport (compact 2-line).

    header: e.g. '📊 Day Report — Thu Apr 16' or '' for subsequent sections.
    count_label: e.g. 'Paper' or 'TP'.
    """
    SEP = "\u2500" * 26
    lines = []
    if header:
        lines.append(header)

    trades_sorted = sorted(trades, key=_dayreport_sort_key) if trades else []
    total_pnl = sum(t.get("pnl", 0) for t in trades_sorted)

    lines.append(SEP)
    lines.append("%s: %d trades  P&L: %s" % (count_label, len(trades_sorted), _fmt_pnl(total_pnl)))
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


async def _reply_in_chunks(message, text, max_len=3800):
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
        await message.reply_text('\n'.join(chunk))


async def cmd_dayreport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show completed trades with P&L summary (optional date)."""
    await update.message.reply_chat_action(ChatAction.TYPING)
    target_date = _parse_date_arg(context.args)
    target_str = target_date.strftime("%Y-%m-%d")
    day_label = target_date.strftime("%a %b %d, %Y")
    header = "\U0001f4ca Day Report \u2014 %s" % day_label

    # Fix B: Route based on which bot
    if is_tp_update(update):
        tp_long = [
            t for t in tp_trade_history
            if t.get("date", "") == target_str
        ]
        tp_short = [
            t for t in tp_short_trade_history
            if t.get("date", "") == target_str
        ]
        all_tp = tp_long + tp_short
        body = _format_dayreport_section(all_tp, header, "TP")
        await _reply_in_chunks(update.message, body)
        # Chart: Trade P&L bar chart (run in thread to avoid blocking event loop)
        if MATPLOTLIB_AVAILABLE and all_tp:
            loop = asyncio.get_event_loop()
            buf = await loop.run_in_executor(None, _chart_dayreport, all_tp, day_label)
            if buf:
                await update.message.reply_photo(photo=buf, caption="Trade P&L chart")
        return

    # Paper portfolio
    paper_long = [
        t for t in trade_history
        if t.get("date", "") == target_str
    ]
    paper_short = [
        t for t in short_trade_history
        if t.get("date", "") == target_str
    ]
    all_paper = paper_long + paper_short

    paper_body = _format_dayreport_section(all_paper, header, "Paper")
    await _reply_in_chunks(update.message, paper_body)

    # Chart: Trade P&L bar chart (run in thread to avoid blocking event loop)
    if MATPLOTLIB_AVAILABLE and all_paper:
        loop = asyncio.get_event_loop()
        buf = await loop.run_in_executor(None, _chart_dayreport, all_paper, day_label)
        if buf:
            await update.message.reply_photo(photo=buf, caption="Trade P&L chart")


async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show completed trades (entries and exits) chronologically (optional date)."""
    await update.message.reply_chat_action(ChatAction.TYPING)
    SEP = "\u2500" * 34
    target_date = _parse_date_arg(context.args)
    target_str = target_date.strftime("%Y-%m-%d")
    day_label = target_date.strftime("%a %b %d, %Y")

    # Fix B: Route based on which bot
    if is_tp_update(update):
        today_trades = [t for t in tp_paper_trades if t.get("date", "") == target_str]
        today_trades.sort(key=lambda t: t.get("time", ""))
        if not today_trades:
            await update.message.reply_text("[TP] No trades on %s." % day_label)
            return
        lines = [
            "\U0001f4cb [TP] Trade Log \u2014 %s" % day_label,
            SEP,
        ]
        for t in today_trades:
            tm = t.get("time", "--:--")
            ticker = t.get("ticker", "?")
            action = t.get("action", "?")
            shares = t.get("shares", 0)
            price = t.get("price", 0)
            if action == "BUY":
                stop = t.get("stop", 0)
                lines.append(
                    f"{tm}  BUY   {ticker}  {shares} @ ${price:.2f}  stop ${stop:.2f}"
                )
            else:
                pnl_v = t.get("pnl", 0)
                pnl_p = t.get("pnl_pct", 0)
                lines.append(
                    f"{tm}  EXIT  {ticker}  {shares} @ ${price:.2f}"
                    f"  P&L: ${pnl_v:+.2f} ({pnl_p:+.2f}%)"
                )
        lines.append(SEP)
        n_closed = sum(1 for t in today_trades if t.get("action") == "SELL")
        n_open = len(tp_positions)
        day_pnl = sum(t.get("pnl", 0) for t in today_trades if t.get("action") == "SELL")
        day_pnl_fmt = f"{day_pnl:+,.2f}"
        lines.append(f"Completed: {n_closed} trades  Open: {n_open} positions")
        lines.append(f"Day P&L: ${day_pnl_fmt}")
        await _reply_in_chunks(update.message, "\n".join(lines))
        return

    # Paper portfolio
    today_trades = [t for t in paper_trades if t.get("date", "") == target_str]
    today_trades.sort(key=lambda t: t.get("time", ""))

    if not today_trades:
        await update.message.reply_text("No trades on %s." % day_label)
        return

    lines = [
        "\U0001f4cb Trade Log \u2014 %s" % day_label,
        SEP,
    ]

    for t in today_trades:
        tm = t.get("time", "--:--")
        ticker = t.get("ticker", "?")
        action = t.get("action", "?")
        shares = t.get("shares", 0)
        price = t.get("price", 0)
        if action == "BUY":
            stop = t.get("stop", 0)
            lines.append(
                "%s  BUY   %s  %d @ $%.2f  stop $%.2f"
                % (tm, ticker, shares, price, stop)
            )
        else:
            pnl_val = t.get("pnl", 0)
            pnl_pct = t.get("pnl_pct", 0)
            lines.append(
                "%s  EXIT  %s  %d @ $%.2f  P&L: $%+.2f (%+.2f%%)"
                % (tm, ticker, shares, price, pnl_val, pnl_pct)
            )

    lines.append(SEP)

    n_closed = sum(1 for t in today_trades if t.get("action") == "SELL")
    n_open = len(positions)
    day_pnl = sum(t.get("pnl", 0) for t in today_trades if t.get("action") == "SELL")
    lines.append("Completed: %d trades  Open: %d positions" % (n_closed, n_open))
    lines.append("Day P&L: $%+,.2f" % day_pnl)

    await _reply_in_chunks(update.message, "\n".join(lines))


async def cmd_replay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Timeline replay of trades with running cumulative P&L (optional date)."""
    await update.message.reply_chat_action(ChatAction.TYPING)
    SEP = "\u2500" * 34
    target_date = _parse_date_arg(context.args)
    target_str = target_date.strftime("%Y-%m-%d")
    day_label = target_date.strftime("%a %b %d, %Y")

    # Fix B: Route based on which bot
    if is_tp_update(update):
        today_trades = [t for t in tp_paper_trades if t.get("date", "") == target_str]
        today_trades.sort(key=lambda t: t.get("time", ""))
        if not today_trades:
            await update.message.reply_text("[TP] No trades on %s." % day_label)
            return
        lines = [
            "\U0001f504 [TP] Trade Replay \u2014 %s" % day_label,
            SEP,
        ]
        cum_pnl = 0.0
        open_count = 0
        wins = 0
        losses = 0
        for t in today_trades:
            tm = t.get("time", "--:--")
            ticker = t.get("ticker", "?")
            action = t.get("action", "?")
            price = t.get("price", 0)
            if action == "BUY":
                open_count += 1
                lines.append(
                    f"{tm} \u2192 BUY  {ticker}  ${price:.2f}  [positions: {open_count}]"
                )
            else:
                open_count = max(0, open_count - 1)
                pnl_val = t.get("pnl", 0)
                cum_pnl += pnl_val
                if pnl_val > 0:
                    wins += 1
                else:
                    losses += 1
                cum_fmt = f"{cum_pnl:+.2f}"
                lines.append(
                    f"{tm} \u2192 EXIT {ticker}  ${price:.2f}"
                    f"  ${pnl_val:+.2f}   cumP&L: ${cum_fmt}"
                )
        lines.append(SEP)
        n_sells = wins + losses
        cum_pnl_fmt = f"{cum_pnl:+.2f}"
        lines.append(
            f"Final P&L: ${cum_pnl_fmt}  |  Trades: {n_sells}  |  W: {wins}  L: {losses}"
        )
        msg = "\n".join(lines)
        await _reply_in_chunks(update.message, msg)
        return

    # Paper portfolio
    today_trades = [t for t in paper_trades if t.get("date", "") == target_str]
    today_trades.sort(key=lambda t: t.get("time", ""))

    if not today_trades:
        await update.message.reply_text("No trades on %s." % day_label)
        return

    lines = [
        "\U0001f504 Trade Replay \u2014 %s" % day_label,
        SEP,
    ]

    cum_pnl = 0.0
    open_count = 0
    wins = 0
    losses = 0

    for t in today_trades:
        tm = t.get("time", "--:--")
        ticker = t.get("ticker", "?")
        action = t.get("action", "?")
        price = t.get("price", 0)

        if action == "BUY":
            open_count += 1
            lines.append(
                f"{tm} \u2192 BUY  {ticker}  ${price:.2f}  [positions: {open_count}]"
            )
        else:
            open_count = max(0, open_count - 1)
            pnl_val = t.get("pnl", 0)
            cum_pnl += pnl_val
            if pnl_val > 0:
                wins += 1
            else:
                losses += 1
            cum_fmt = f"{cum_pnl:+.2f}"
            lines.append(
                f"{tm} \u2192 EXIT {ticker}  ${price:.2f}"
                f"  ${pnl_val:+.2f}   cumP&L: ${cum_fmt}"
            )

    lines.append(SEP)
    n_sells = wins + losses
    cum_pnl_fmt = f"{cum_pnl:+.2f}"
    lines.append(
        f"Final P&L: ${cum_pnl_fmt}  |  Trades: {n_sells}  |  W: {wins}  L: {losses}"
    )

    msg = "\n".join(lines)
    await _reply_in_chunks(update.message, msg)


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show version info."""
    await update.message.reply_text(
        "Stock Spike Monitor v%s\n%s" % (BOT_VERSION, RELEASE_NOTE))


# ============================================================
# /algo COMMAND
# ============================================================
async def cmd_algo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send algorithm summary + downloadable PDF reference."""
    SEP = "\u2500" * 34
    summary = (
        "\U0001f4d8 ALGORITHM REFERENCE v2.9\n"
        f"{SEP}\n"
        "Two independent strategies:\n\n"
        "\U0001f4c8 ORB LONG BREAKOUT\n"
        "  Entry: 1-min close > OR_High\n"
        "         + price > PDC (green stock)\n"
        "         + SPY & QQQ > AVWAP\n"
        "  Stop : OR_High \u2212 $0.90\n"
        "  Trail: +1.0% trigger | max(1.0%, $1.00) distance\n\n"
        "\U0001f9b7 WOUNDED BUFFALO SHORT\n"
        "  Entry: 1-min close < OR_Low\n"
        "         + price < PDC (red stock)\n"
        "         + SPY & QQQ < AVWAP\n"
        "  Stop : PDC + $0.90\n"
        "  Trail: +1.0% trigger | max(1.0%, $1.00) distance\n\n"
        f"{SEP}\n"
        "Size : 10 shares (limit orders only)\n"
        "Max  : 2 long + 2 short per ticker/day\n"
        "OR   : 8:30\u20138:35 CT (first 5 min)\n"
        "Scan : every 60s \u2192 8:35\u20142:55 CT\n"
        "EOD  : force-close all at 2:55 CT\n"
        f"{SEP}\n"
        "\U0001f6e1 INDEX REGIME SHIELD (v2.9.8)\n"
        "  Lords Left & Bull Vacuum exits now\n"
        "  use last completed 1-min bar close\n"
        "  instead of live tick \u2192 no wick-outs\n"
        f"{SEP}\n"
        "Full reference guide attached \u2193"
    )
    await update.message.reply_text(summary)

    # Send PDF — try local file first, fall back to GitHub raw download
    _ALGO_PDF_URL = (
        "https://raw.githubusercontent.com/valira3/"
        "stock-spike-monitor/main/stock_spike_monitor_algo.pdf"
    )
    pdf_path = Path("stock_spike_monitor_algo.pdf")
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
                    filename="StockSpikeMonitor_Algorithm_v2.9.pdf",
                    caption="Stock Spike Monitor \u2014 Algorithm Reference Manual v2.9",
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
        "  \u2022 SPY > AVWAP\n"
        "  \u2022 QQQ > AVWAP\n"
        "Stop: OR High \u2212 $0.90\n"
        "Trail: +1.0% trigger | max(1.0%, $1.00) distance\n"
        "Size: 10 shares \u00b7 limit order\n"
        "Max: 4 entries/ticker/day\n"
        "EOD: closes at 2:55 CT\n"
        "\n"
        "Eye of the Tiger exits:\n"
        "  \U0001f56f Red Candle\n"
        "     price < Open OR < PDC\n"
        "  \U0001f451 Lords Left\n"
        "     SPY or QQQ < AVWAP\n"
        "  (both confirmed on 1m close)\n"
        f"{SEP}\n"
        "\U0001f4c9 SHORT \u2014 Wounded Buffalo\n"
        "Entry after 8:45 CT (15-min buffer)\n"
        "Entry (all must be true):\n"
        "  \u2022 1m close < OR Low\n"
        "  \u2022 Price < PDC\n"
        "  \u2022 SPY < AVWAP\n"
        "  \u2022 QQQ < AVWAP\n"
        "Stop: PDC + $0.90\n"
        "Trail: +1.0% trigger | max(1.0%, $1.00) distance\n"
        "Size: 10 shares \u00b7 limit order\n"
        "Max: 4 entries/ticker/day\n"
        "EOD: closes at 2:55 CT\n"
        "\n"
        "Eye of the Tiger exits:\n"
        "  \U0001f300 Bull Vacuum\n"
        "     SPY or QQQ > AVWAP\n"
        "  \U0001f504 Polarity Shift\n"
        "     price > PDC\n"
        "  (both confirmed on 1m close)\n"
        f"{SEP}\n"
        "\U0001f6e1 Index Regime Shield\n"
        "  Tiger exits only fire on\n"
        "  completed 1m bar close\n"
        "  \u2014 no wick-outs\n"
        f"{SEP}"
    )
    await _reply_in_chunks(update.message, text)


# ============================================================
# /reset COMMAND (Fix C)
# ============================================================
async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/reset paper | /reset tp | /reset both — show confirmation before reset."""
    args = context.args
    target = args[0].lower() if args else ""

    if target == "paper":
        await update.message.reply_text(
            "\u26a0\ufe0f Reset paper portfolio to $100,000?\nAll trade history will be cleared.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("\u2705 Confirm", callback_data="reset_paper_confirm"),
                InlineKeyboardButton("\u274c Cancel", callback_data="reset_cancel")
            ]])
        )
    elif target == "tp":
        await update.message.reply_text(
            "\u26a0\ufe0f Reset TP portfolio to $100,000?\nAll trade history will be cleared.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("\u2705 Confirm", callback_data="reset_tp_confirm"),
                InlineKeyboardButton("\u274c Cancel", callback_data="reset_cancel")
            ]])
        )
    elif target == "both":
        await update.message.reply_text(
            "\u26a0\ufe0f Reset BOTH portfolios to $100,000?\nAll trade history will be cleared.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("\u2705 Confirm", callback_data="reset_both_confirm"),
                InlineKeyboardButton("\u274c Cancel", callback_data="reset_cancel")
            ]])
        )
    else:
        await update.message.reply_text(
            "Choose what to reset:",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("\U0001f4c4 Reset Paper", callback_data="reset_paper"),
                    InlineKeyboardButton("\U0001f4cb Reset TP", callback_data="reset_tp"),
                ],
                [
                    InlineKeyboardButton("\U0001f504 Reset Both", callback_data="reset_both"),
                ],
            ])
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


def _do_reset_tp():
    """Execute TP portfolio reset."""
    global tp_paper_cash
    tp_positions.clear()
    tp_short_positions.clear()
    tp_paper_trades.clear()
    tp_trade_history.clear()
    tp_short_trade_history.clear()
    tp_paper_cash = PAPER_STARTING_CAPITAL
    save_tp_state()


async def reset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard taps for /reset confirmation."""
    query = update.callback_query
    await query.answer()
    capital_fmt = format(PAPER_STARTING_CAPITAL, ",.0f")
    if query.data == "reset_paper_confirm":
        _do_reset_paper()
        await query.edit_message_text("\u2705 Paper portfolio reset to $%s." % capital_fmt)
    elif query.data == "reset_tp_confirm":
        _do_reset_tp()
        await query.edit_message_text("\u2705 TP portfolio reset to $%s." % capital_fmt)
    elif query.data == "reset_both_confirm":
        _do_reset_paper()
        _do_reset_tp()
        await query.edit_message_text("\u2705 Both portfolios reset to $%s." % capital_fmt)
    elif query.data == "reset_cancel":
        await query.edit_message_text("\u274c Reset cancelled.")
    elif query.data == "reset_paper":
        await query.edit_message_text(
            "\u26a0\ufe0f Reset paper portfolio to $100,000?\nAll trade history will be cleared.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("\u2705 Confirm", callback_data="reset_paper_confirm"),
                InlineKeyboardButton("\u274c Cancel", callback_data="reset_cancel")
            ]])
        )
    elif query.data == "reset_tp":
        await query.edit_message_text(
            "\u26a0\ufe0f Reset TP portfolio to $100,000?\nAll trade history will be cleared.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("\u2705 Confirm", callback_data="reset_tp_confirm"),
                InlineKeyboardButton("\u274c Cancel", callback_data="reset_cancel")
            ]])
        )
    elif query.data == "reset_both":
        await query.edit_message_text(
            "\u26a0\ufe0f Reset BOTH portfolios to $100,000?\nAll trade history will be cleared.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("\u2705 Confirm", callback_data="reset_both_confirm"),
                InlineKeyboardButton("\u274c Cancel", callback_data="reset_cancel")
            ]])
        )


# ============================================================
# /perf COMMAND (Feature 5)
# ============================================================
async def cmd_perf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show performance stats (optional date or N days)."""
    await update.message.reply_chat_action(ChatAction.TYPING)
    SEP = "\u2500" * 34
    now_et = _now_et()
    today = now_et.strftime("%Y-%m-%d")

    # Select history based on which bot
    if is_tp_update(update):
        long_history = tp_trade_history
        short_hist = tp_short_trade_history
        label = "TP Portfolio"
    else:
        long_history = trade_history
        short_hist = short_trade_history
        label = "Paper Portfolio"

    if not long_history and not short_hist:
        await update.message.reply_text("No completed trades yet.")
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

    if single_day:
        # Filter to exact date
        filt_long = [t for t in long_history if t.get("date", "") == date_filter]
        filt_short = [t for t in short_hist if t.get("date", "") == date_filter]
    elif date_filter:
        filt_long = [t for t in long_history if t.get("date", "") >= date_filter]
        filt_short = [t for t in short_hist if t.get("date", "") >= date_filter]
    else:
        filt_long = long_history
        filt_short = short_hist

    lines = [
        "\U0001f4c8 Performance \u2014 %s \u2014 %s" % (label, perf_label),
        SEP,
    ]

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
    combined = long_history + short_hist
    streak = _compute_streak(combined)
    lines.append("Streak: %s" % streak)

    msg = "\n".join(lines)
    await _reply_in_chunks(update.message, msg)

    # Chart: Equity curve (run in thread to avoid blocking event loop)
    if MATPLOTLIB_AVAILABLE:
        chart_hist = filt_long + filt_short
        if chart_hist:
            loop = asyncio.get_event_loop()
            buf = await loop.run_in_executor(None, _chart_equity_curve, chart_hist, perf_label)
            if buf:
                await update.message.reply_photo(photo=buf, caption="Equity Curve")


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

    # SPY/QQQ
    spy_avwap = avwap_data["SPY"]["avwap"]
    qqq_avwap = avwap_data["QQQ"]["avwap"]
    spy_bars = fetch_1min_bars("SPY")
    qqq_bars = fetch_1min_bars("QQQ")
    spy_price_val = spy_bars["current_price"] if spy_bars else 0
    qqq_price_val = qqq_bars["current_price"] if qqq_bars else 0
    spy_ok = (spy_price_val > spy_avwap) if (spy_bars and spy_avwap > 0) else False
    qqq_ok = (qqq_price_val > qqq_avwap) if (qqq_bars and qqq_avwap > 0) else False
    spy_below = (spy_price_val < spy_avwap) if (spy_bars and spy_avwap > 0) else False
    qqq_below = (qqq_price_val < qqq_avwap) if (qqq_bars and qqq_avwap > 0) else False
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
    await update.message.reply_chat_action(ChatAction.TYPING)
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /price AAPL")
        return

    ticker = args[0].upper()
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, _price_sync, ticker)
    if text is None:
        await update.message.reply_text("Could not fetch data for %s" % ticker)
        return
    await update.message.reply_text(text)


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

    # SPY/QQQ AVWAP
    spy_bars = fetch_1min_bars("SPY")
    qqq_bars = fetch_1min_bars("QQQ")
    spy_price = spy_bars["current_price"] if spy_bars else 0
    qqq_price = qqq_bars["current_price"] if qqq_bars else 0
    spy_avwap = avwap_data["SPY"]["avwap"]
    qqq_avwap = avwap_data["QQQ"]["avwap"]
    spy_ok = spy_price > spy_avwap if spy_avwap > 0 else False
    qqq_ok = qqq_price > qqq_avwap if qqq_avwap > 0 else False
    spy_icon = "\u2705" if spy_ok else "\u274c"
    qqq_icon = "\u2705" if qqq_ok else "\u274c"

    spy_avwap_fmt = "%.2f" % spy_avwap if spy_avwap > 0 else "n/a"
    qqq_avwap_fmt = "%.2f" % qqq_avwap if qqq_avwap > 0 else "n/a"
    lines.append("SPY AVWAP: $%s  %s" % (spy_avwap_fmt, spy_icon))
    lines.append("QQQ AVWAP: $%s  %s" % (qqq_avwap_fmt, qqq_icon))

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
    """Show today's OR levels and current price for all 8 trade tickers."""
    await update.message.reply_chat_action(ChatAction.TYPING)
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, _orb_sync)
    if text is None:
        await update.message.reply_text(
            "OR not collected yet \u2014 runs at 8:35 CT."
        )
        return
    await _reply_in_chunks(update.message, text)


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
# /menu COMMAND — Quick tap-grid
# ============================================================
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a quick tap-grid of all commands."""
    keyboard = [
        [
            InlineKeyboardButton("\U0001f4ca Dashboard", callback_data="menu_dashboard"),
            InlineKeyboardButton("\U0001f4c8 Positions", callback_data="menu_positions"),
        ],
        [
            InlineKeyboardButton("\U0001f4b0 Price", callback_data="menu_price_prompt"),
            InlineKeyboardButton("\U0001f4d0 ORs", callback_data="menu_orb"),
        ],
        [
            InlineKeyboardButton("\U0001f4c5 Day Report", callback_data="menu_dayreport"),
            InlineKeyboardButton("\U0001f4c8 Performance", callback_data="menu_perf"),
        ],
        [
            InlineKeyboardButton("\U0001f50d Monitor", callback_data="menu_monitoring"),
            InlineKeyboardButton("\U0001f9ea System Test", callback_data="menu_test"),
        ],
        [
            InlineKeyboardButton("\U0001f4d8 Strategy", callback_data="menu_strategy"),
            InlineKeyboardButton("\u2139\ufe0f Version", callback_data="menu_version"),
        ],
    ]
    await update.message.reply_text(
        "\U0001f4f1 Quick Menu\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle taps on /menu inline buttons."""
    query = update.callback_query
    await query.answer()
    is_tp = (str(query.message.chat_id) == TELEGRAM_TP_CHAT_ID)

    if query.data == "menu_price_prompt":
        await query.edit_message_text("Use /price TICKER (e.g. /price AAPL)")
        return

    if query.data == "menu_version":
        await query.edit_message_text(
            "Stock Spike Monitor v%s\n%s" % (BOT_VERSION, RELEASE_NOTE))
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

    await query.edit_message_text("\u23f3 Loading...")

    if query.data == "menu_dashboard":
        # Build a lightweight dashboard summary
        time_cdt = _now_cdt().strftime("%I:%M %p CDT")
        n_paper = len(positions)
        n_tp = len(tp_positions)
        msg = (
            "\U0001f4ca DASHBOARD  %s\n" % time_cdt
            + "Paper: %d positions | TP: %d positions\n" % (n_paper, n_tp)
            + "Use /dashboard for full snapshot"
        )
        await query.message.reply_text(msg)
    elif query.data == "menu_positions":
        loop = asyncio.get_event_loop()
        msg = await loop.run_in_executor(None, _build_positions_text, is_tp)
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
        await query.message.reply_text("Use /dayreport for today's trades + P&L")
    elif query.data == "menu_perf":
        await query.message.reply_text("Use /perf for all-time performance stats")
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


def _or_now_sync():
    """Re-collect missing OR data (blocking I/O — run in executor). Returns text or None."""
    now_et = _now_et()
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    or_end = now_et.replace(hour=9, minute=35, second=0, microsecond=0)
    open_ts = int(market_open.timestamp())
    end_ts = int(or_end.timestamp())

    missing = [t for t in TICKERS if t not in or_high]
    if not missing:
        return None

    results = []
    recovered = 0
    still_fail = 0

    for ticker in missing:
        source = None
        got_high = None
        got_low = None

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
                    got_high = max_high
                    got_low = min_low
                    source = "Yahoo"
        except Exception as e:
            logger.warning("or_now Yahoo failed for %s: %s", ticker, e)

        # FMP fallback if Yahoo didn't work
        if source is None:
            try:
                fmp = get_fmp_quote(ticker)
                if fmp and fmp.get("dayHigh") and fmp.get("dayLow"):
                    or_high[ticker] = fmp["dayHigh"]
                    or_low[ticker] = fmp["dayLow"]
                    if fmp.get("previousClose") and fmp["previousClose"] > 0:
                        pdc[ticker] = fmp["previousClose"]
                    got_high = fmp["dayHigh"]
                    got_low = fmp["dayLow"]
                    source = "FMP"
            except Exception as e:
                logger.warning("or_now FMP failed for %s: %s", ticker, e)

        if source is not None:
            recovered += 1
            results.append(
                "%s: \u2705 high=%.2f low=%.2f (%s)"
                % (ticker, got_high, got_low if got_low else 0, source)
            )
            logger.info("or_now recovered %s: high=%.2f low=%.2f (%s)",
                        ticker, got_high, got_low if got_low else 0, source)
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
    await update.message.reply_chat_action(ChatAction.TYPING)
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, _or_now_sync)
    if text is None:
        await update.message.reply_text("All tickers have OR data already.")
        return
    await update.message.reply_text(text)


# ============================================================
# TELEGRAM BOT SETUP
# ============================================================
MAIN_BOT_COMMANDS = [
    BotCommand("dashboard", "Full market snapshot"),
    BotCommand("help", "Command menu"),
    BotCommand("menu", "Quick command menu"),
    BotCommand("status", "Open positions + P&L"),
    BotCommand("positions", "Alias for /status"),
    BotCommand("orb", "Today's OR levels + status"),
    BotCommand("perf", "Performance stats (optional date)"),
    BotCommand("price", "Live quote for a ticker"),
    BotCommand("log", "Trade log (optional date)"),
    BotCommand("replay", "Trade timeline (optional date)"),
    BotCommand("dayreport", "Trades + P&L (optional date)"),
    BotCommand("monitoring", "Pause/resume scanner"),
    BotCommand("reset", "Reset portfolio"),
    BotCommand("algo", "Algorithm reference PDF"),
    BotCommand("strategy", "Strategy summary"),
    BotCommand("test", "Run system health test"),
    BotCommand("or_now", "Re-collect missing OR levels"),
    BotCommand("version", "Release notes"),
]

TP_BOT_COMMANDS = [
    BotCommand("dashboard", "Full market snapshot"),
    BotCommand("help", "Command menu"),
    BotCommand("menu", "Quick command menu"),
    BotCommand("status", "Open positions + P&L"),
    BotCommand("positions", "Alias for /status"),
    BotCommand("orb", "Today's OR levels + status"),
    BotCommand("perf", "Performance stats (optional date)"),
    BotCommand("price", "Live quote for a ticker"),
    BotCommand("log", "Trade log (optional date)"),
    BotCommand("replay", "Trade timeline (optional date)"),
    BotCommand("dayreport", "Trades + P&L (optional date)"),
    BotCommand("monitoring", "Pause/resume scanner"),
    BotCommand("reset", "Reset portfolio"),
    BotCommand("algo", "Algorithm reference PDF"),
    BotCommand("strategy", "Strategy summary"),
    BotCommand("test", "Run system health test"),
    BotCommand("or_now", "Re-collect missing OR levels"),
    BotCommand("version", "Release notes"),
]


async def _set_bot_commands(app: Application) -> None:
    """Register / menu commands on startup (all scopes)."""
    try:
        # Clear default scope first (removes any stale commands from old versions)
        await app.bot.set_my_commands(MAIN_BOT_COMMANDS, scope=BotCommandScopeDefault())
        await app.bot.set_my_commands(MAIN_BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats())
        await app.bot.set_my_commands(MAIN_BOT_COMMANDS, scope=BotCommandScopeAllGroupChats())
        logger.info("Registered %d bot commands (all scopes)", len(MAIN_BOT_COMMANDS))
    except Exception as e:
        logger.warning("Failed to set bot commands: %s", e)


async def _set_tp_bot_commands(app: Application) -> None:
    """Register TP bot commands (all scopes)."""
    try:
        # Clear default scope first (removes any stale commands from old versions)
        await app.bot.set_my_commands(TP_BOT_COMMANDS, scope=BotCommandScopeDefault())
        await app.bot.set_my_commands(TP_BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats())
        await app.bot.set_my_commands(TP_BOT_COMMANDS, scope=BotCommandScopeAllGroupChats())
        logger.info("Registered %d TP bot commands (all scopes)", len(TP_BOT_COMMANDS))
    except Exception as e:
        logger.warning("Failed to set TP bot commands: %s", e)


def send_startup_message():
    """Send rich deployment card to BOTH main and TP bots."""
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
    n_tp_pos = len(tp_positions)
    paper_cash_fmt = f"{paper_cash:,.2f}"
    tp_cash_fmt = f"{tp_paper_cash:,.2f}"

    msg = (
        f"\U0001f680 v{BOT_VERSION} deployed\n"
        f"{RELEASE_NOTE}\n"
        f"{SEP}\n"
        f"Universe: {universe}\n"
        f"Strategy: ORB Long + Wounded Buffalo Short | PDC | AVWAP\n"
        f"Scan:     every {SCAN_INTERVAL}s  |  Trail: Bison +1.0% / min $1.00\n"
        f"Stops:    Long OR_High\u2212$0.90  |  Short PDC+$0.90\n"
        f"{SEP}\n"
        f"\U0001f4c4 Paper:  ${paper_cash_fmt} cash | {n_paper_pos} positions\n"
        f"\U0001f4cb TP:     ${tp_cash_fmt} cash | {n_tp_pos} positions\n"
        f"Market:   {market_status}\n"
        f"{SEP}\n"
        f"/help for all commands"
    )
    send_telegram(msg)
    # Fix A: TP send failure → logger.debug (never raise/stop)
    try:
        send_tp_telegram(msg)
    except Exception as e:
        logger.debug("TP startup message failed: %s", e)


def run_telegram_bot():
    """Start main Telegram bot (and optional TP bot)."""
    app = (Application.builder()
           .token(TELEGRAM_TOKEN)
           .post_init(_set_bot_commands)
           .build())

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("replay", cmd_replay))
    app.add_handler(CommandHandler("dayreport", cmd_dayreport))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("perf", cmd_perf))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("orb", cmd_orb))
    app.add_handler(CommandHandler("monitoring", cmd_monitoring))
    app.add_handler(CommandHandler("algo", cmd_algo))
    app.add_handler(CommandHandler("strategy", cmd_strategy))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("or_now", cmd_or_now))
    app.add_handler(CommandHandler("menu", cmd_menu))

    # Callback query handlers
    app.add_handler(CallbackQueryHandler(monitoring_callback, pattern="^monitoring_"))
    app.add_handler(CallbackQueryHandler(reset_callback, pattern="^reset_"))
    app.add_handler(CallbackQueryHandler(positions_callback, pattern="^positions_"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu_"))

    # If no separate TP token, run single bot
    if not TELEGRAM_TP_TOKEN:
        app.run_polling()
        return

    # Dual bot mode
    tp_app = (Application.builder()
              .token(TELEGRAM_TP_TOKEN)
              .post_init(_set_tp_bot_commands)
              .build())

    tp_app.add_handler(CommandHandler("help", cmd_help))
    tp_app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    tp_app.add_handler(CommandHandler("status", cmd_status))
    tp_app.add_handler(CommandHandler("positions", cmd_positions))
    tp_app.add_handler(CommandHandler("log", cmd_log))
    tp_app.add_handler(CommandHandler("replay", cmd_replay))
    tp_app.add_handler(CommandHandler("dayreport", cmd_dayreport))
    tp_app.add_handler(CommandHandler("version", cmd_version))
    tp_app.add_handler(CommandHandler("reset", cmd_reset))
    tp_app.add_handler(CommandHandler("perf", cmd_perf))
    tp_app.add_handler(CommandHandler("price", cmd_price))
    tp_app.add_handler(CommandHandler("orb", cmd_orb))
    tp_app.add_handler(CommandHandler("monitoring", cmd_monitoring))
    tp_app.add_handler(CommandHandler("algo", cmd_algo))
    tp_app.add_handler(CommandHandler("strategy", cmd_strategy))
    tp_app.add_handler(CommandHandler("test", cmd_test))
    tp_app.add_handler(CommandHandler("or_now", cmd_or_now))
    tp_app.add_handler(CommandHandler("menu", cmd_menu))

    # Callback query handlers (TP bot)
    tp_app.add_handler(CallbackQueryHandler(monitoring_callback, pattern="^monitoring_"))
    tp_app.add_handler(CallbackQueryHandler(reset_callback, pattern="^reset_"))
    tp_app.add_handler(CallbackQueryHandler(positions_callback, pattern="^positions_"))
    tp_app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu_"))

    async def _run_both():
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        async with app:
            async with tp_app:
                # Explicitly register commands on all scopes (post_init does not
                # fire when using manual start/stop instead of run_polling)
                await _set_bot_commands(app)
                await _set_tp_bot_commands(tp_app)
                await app.updater.start_polling()
                await tp_app.updater.start_polling()
                await app.start()
                await tp_app.start()
                await stop.wait()
                await tp_app.updater.stop()
                await app.updater.stop()
                await tp_app.stop()
                await app.stop()

    asyncio.run(_run_both())


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
load_paper_state()
load_tp_state()

# Startup catch-up
startup_catchup()

# Background threads
threading.Thread(target=scheduler_thread, daemon=True).start()
threading.Thread(target=health_ping, daemon=True).start()

logger.info("Stock Spike Monitor v%s started", BOT_VERSION)
send_startup_message()
run_telegram_bot()
