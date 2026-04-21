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

BOT_VERSION = "3.4.12"
RELEASE_NOTE = (
    "v3.4.12 \u2014 proximity row fix.\n"
    "Dashboard proximity card no\n"
    "longer wraps the pct column\n"
    "onto a second line.\n"
    "\u2022 Widen .prox-pct so\n"
    "  '0.02% \u00b7 OR-low' fits\n"
    "  on one line at mobile and\n"
    "  desktop widths.\n"
    "\u2022 Progress bar shrinks\n"
    "  slightly (flex: 1) to\n"
    "  make room.\n"
    "\u2022 Belt-and-suspenders:\n"
    "  white-space: nowrap on\n"
    "  the pct column.\n"
    "CSS only. No trade-logic\n"
    "or backend changes."
)

FMP_API_KEY = os.getenv("FMP_API_KEY", "VqYj2Jujrc8IvUOe4CR1g0tRf0qlB4AV")
FINNHUB_TOKEN = os.getenv("FINNHUB_TOKEN", "")

# Human-readable exit reason labels
REASON_LABELS = {
    "STOP": "\U0001f6d1 Hard Stop",
    "TRAIL": "\U0001f3af Trail Stop",
    "RED_CANDLE": "\U0001f56f Red Candle (lost daily polarity)",
    # Long global eject (Confluence Shield, v3.2.0+: SPY AND QQQ, 5m close)
    "LORDS_LEFT":      "\U0001f451 Lords Left (SPY/QQQ < AVWAP)",
    "LORDS_LEFT[1m]":  "\U0001f451 Lords Left (SPY/QQQ < AVWAP)",   # legacy v2.9.8
    "LORDS_LEFT[5m]":  "\U0001f451 Lords Left (SPY+QQQ 5m < AVWAP)",
    "POLARITY_SHIFT": "\U0001f504 Polarity Shift (price > PDC)",
    # Short global eject (Confluence Shield, v3.2.0+: SPY AND QQQ, 5m close)
    "BULL_VACUUM":     "\U0001f300 Bull Vacuum (SPY/QQQ > AVWAP)",
    "BULL_VACUUM[1m]": "\U0001f300 Bull Vacuum (SPY/QQQ > AVWAP)",  # legacy v2.9.8
    "BULL_VACUUM[5m]": "\U0001f300 Bull Vacuum (SPY+QQQ 5m > AVWAP)",
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

# Guard: prevent scheduler from saving empty state before load completes
_state_loaded = False

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
# 1) BREADTH   — SPY/QQQ vs their AVWAP → BULLISH/NEUTRAL/BEARISH
# 2) RSI       — 14-period on resampled 5-min bars, SPY+QQQ aggregate
#                  → OVERBOUGHT/NEUTRAL/OVERSOLD; plus a per-ticker dict
# 3) TICKER    — per-ticker today realized P&L + current per-ticker RSI
#    HEAT        → lists of tickers that are already red or already at
#                  extremes, surfaced in /mode for pattern-spotting
#
# Thresholds are deliberately conservative for the observation phase.
# If a knob is eventually wired, it'll use these same thresholds or
# tighter ones, never looser.

BREADTH_TOLERANCE_PCT    = 0.001   # ±0.1% around AVWAP counts as NEUTRAL
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


def _classify_breadth():
    """Observer 1: breadth from SPY/QQQ vs their AVWAP.
    Returns (label, detail). Never raises.
    """
    try:
        # fetch_1min_bars is cycle-cached — if the scan loop already
        # fetched SPY/QQQ this cycle we reuse; otherwise we fetch once.
        spy_bars = fetch_1min_bars("SPY")
        qqq_bars = fetch_1min_bars("QQQ")
        spy_px = spy_bars.get("current_price") if spy_bars else None
        qqq_px = qqq_bars.get("current_price") if qqq_bars else None
        spy_av = avwap_data.get("SPY", {}).get("avwap", 0) or 0
        qqq_av = avwap_data.get("QQQ", {}).get("avwap", 0) or 0
        if not (spy_px and qqq_px and spy_av and qqq_av):
            return ("UNKNOWN", "SPY/QQQ price or AVWAP not ready")

        spy_diff = (spy_px - spy_av) / spy_av
        qqq_diff = (qqq_px - qqq_av) / qqq_av
        tol = BREADTH_TOLERANCE_PCT

        def _side(d):
            if d >  tol: return "above"
            if d < -tol: return "below"
            return "at"

        spy_side = _side(spy_diff)
        qqq_side = _side(qqq_diff)
        detail = "SPY %+.2f%% %s AVWAP | QQQ %+.2f%% %s AVWAP" % (
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


def _compute_today_realized_pnl(is_tp: bool = False) -> float:
    """Realized P&L today across longs + shorts for the given portfolio.
    Unrealized P&L is excluded on purpose — we want the number that
    drives the DAILY_LOSS_LIMIT halt, which is realized-only.

    Storage asymmetry (critical): long SELLs go to paper_trades with
    action="SELL"; short COVERs are written ONLY to short_trade_history
    (never to paper_trades). We must read both lists or short P&L is
    silently dropped from the DEFENSIVE-mode gate.
    """
    today_str = _now_et().strftime("%Y-%m-%d")
    pnl = 0.0
    if is_tp:
        for t in tp_paper_trades:
            if t.get("date") == today_str and t.get("action") == "SELL":
                pnl += t.get("pnl", 0) or 0
        for t in tp_short_trade_history:
            if t.get("date") == today_str:
                pnl += t.get("pnl", 0) or 0
    else:
        for t in paper_trades:
            if t.get("date") == today_str and t.get("action") == "SELL":
                pnl += t.get("pnl", 0) or 0
        for t in short_trade_history:
            if t.get("date") == today_str:
                pnl += t.get("pnl", 0) or 0
    return pnl


def _today_pnl_breakdown(is_tp: bool = False) -> tuple:
    """Returns (sells_list, covers_list, total_pnl, wins, losses, n_trades)
    for today, for the given portfolio. Single source of truth used by
    EOD summaries, /dashboard, and weekly digest helpers.
    """
    today_str = _now_et().strftime("%Y-%m-%d")
    if is_tp:
        sells = [t for t in tp_paper_trades
                 if t.get("action") == "SELL" and t.get("date", "") == today_str]
        covers = [t for t in tp_short_trade_history
                  if t.get("date", "") == today_str]
    else:
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
    today_pnl = _compute_today_realized_pnl(is_tp=False)
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
        "tp_state": tp_state,
        "trade_history": trade_history,
        "short_positions": short_positions,
        "short_trade_history": short_trade_history[-500:],
        "avwap_data": avwap_data,
        "avwap_last_ts": avwap_last_ts,
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
    global user_config, tp_state, tp_dm_chat_id
    global trade_history
    global short_positions, short_trade_history
    global avwap_data, avwap_last_ts, daily_short_entry_count
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
        tp_state.update(state.get("tp_state", {}))
        trade_history.clear()
        trade_history.extend(state.get("trade_history", []))
        short_positions.update(state.get("short_positions", {}))
        short_trade_history.clear()
        short_trade_history.extend(state.get("short_trade_history", []))
        avwap_data.update(state.get("avwap_data", {}))
        avwap_last_ts.update(state.get("avwap_last_ts", {}))
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
# TP STATE PERSISTENCE
# ============================================================
_tp_save_lock = threading.Lock()
_tp_state_loaded = False


def save_tp_state():
    """Persist TP portfolio state to disk. Thread-safe, atomic."""
    t0 = time.time()
    if not _tp_state_loaded:
        logger.warning("save_tp_state skipped — state not yet loaded")
        return
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
            logger.debug("TP state saved -> %s (%.3fs)", TP_STATE_FILE, time.time() - t0)
        except Exception as e:
            logger.error("save_tp_state failed: %s", e)


def load_tp_state():
    """Load TP portfolio state from disk on startup."""
    global tp_paper_cash, tp_trade_history
    global tp_short_positions, tp_short_trade_history
    global _tp_state_loaded

    if not os.path.exists(TP_STATE_FILE):
        logger.info("No TP state at %s. Starting fresh $%.0f.",
                     TP_STATE_FILE, PAPER_STARTING_CAPITAL)
        _tp_state_loaded = True
        return

    try:
        with open(TP_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        tp_paper_cash = float(state.get("tp_paper_cash", PAPER_STARTING_CAPITAL))
        tp_positions.update(state.get("tp_positions", {}))
        tp_paper_trades.clear()
        tp_paper_trades.extend(state.get("tp_paper_trades", []))
        tp_trade_history.clear()
        tp_trade_history.extend(state.get("tp_trade_history", []))
        tp_short_positions.update(state.get("tp_short_positions", {}))
        tp_short_trade_history.clear()
        tp_short_trade_history.extend(state.get("tp_short_trade_history", []))

        _tp_state_loaded = True
        logger.info("Loaded TP state: cash=$%.2f, %d positions",
                    tp_paper_cash, len(tp_positions))
    except Exception as e:
        _tp_state_loaded = True
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
# DUAL-INDEX CONFLUENCE SHIELD (v3.2.0)
# ============================================================
# Replaces the v2.9.8 "Lords Left / Bull Vacuum" 1-minute, OR-based eject
# with a market-systemic eject that requires:
#   1. AND confluence \u2014 BOTH SPY and QQQ must agree.
#   2. A FINALIZED 5-minute bar close as confirmation.
#
# Goal: filter out sub-5-min liquidity probes ("Hormuz" wicks) and sector
# divergence (e.g. semis strong while energy/defense drag the S&P).
#
# Fail-safe: any missing data \u2192 returns False (do NOT eject). The whole
# point of the change is to do nothing in ambiguous conditions.
# ============================================================
def _last_finalized_5min_close(ticker):
    """Return the close of the most recently FINALIZED 5-min bar for a ticker.

    Reuses _resample_to_5min, which already drops the in-progress (newest)
    bucket. Returns None when bars are unavailable or fewer than one full
    5-min bar has elapsed.
    """
    bars = fetch_1min_bars(ticker)
    if not bars:
        return None
    timestamps = bars.get("timestamps") or []
    closes = bars.get("closes") or []
    if not timestamps or not closes:
        return None
    five_min_closes = _resample_to_5min(timestamps, closes)
    if not five_min_closes:
        return None
    return five_min_closes[-1]


def _dual_index_eject(side):
    """Confluence + 5-min finalized close gate for global eject signals.

    Args:
        side: 'long'  \u2192 True iff BOTH SPY_5m_close < SPY_AVWAP
                              AND QQQ_5m_close < QQQ_AVWAP
              'short' \u2192 True iff BOTH SPY_5m_close > SPY_AVWAP
                              AND QQQ_5m_close > QQQ_AVWAP

    Returns False (no eject) on ANY missing/ambiguous input.
    """
    if side not in ("long", "short"):
        return False

    # Refresh AVWAPs (idempotent; no-op if already current).
    try:
        update_avwap("SPY")
        update_avwap("QQQ")
    except Exception as e:
        logger.debug("_dual_index_eject: avwap refresh failed: %s", e)
        return False

    spy_avwap = avwap_data.get("SPY", {}).get("avwap", 0) or 0
    qqq_avwap = avwap_data.get("QQQ", {}).get("avwap", 0) or 0
    if spy_avwap <= 0 or qqq_avwap <= 0:
        return False  # AVWAPs not seeded yet

    spy_5m = _last_finalized_5min_close("SPY")
    qqq_5m = _last_finalized_5min_close("QQQ")
    if spy_5m is None or qqq_5m is None:
        return False  # < 5 mins of data, or fetch failed

    if side == "long":
        # Both indices must close 5m below their AVWAPs.
        return (spy_5m < spy_avwap) and (qqq_5m < qqq_avwap)
    else:
        # 'short' \u2014 both must close 5m above their AVWAPs.
        return (spy_5m > spy_avwap) and (qqq_5m > qqq_avwap)


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
        "date": now_date,
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
        "date": now_date,
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

    # ── Dual-Index Confluence Shield (v3.2.0) ────────────────────────────────
    # Exit all longs ONLY when BOTH SPY and QQQ have a finalized 5-min close
    # below their respective AVWAPs. Filters sub-5-min liquidity probes and
    # sector divergence ("Hormuz" wicks). Replaces v2.9.8's 1-min OR test.
    lords_left = _dual_index_eject("long")

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

        # ── Confluence Shield: BOTH SPY+QQQ 5m_close < AVWAP ─────────────────
        if lords_left:
            tickers_to_close.append((ticker, current_price, "LORDS_LEFT[5m]"))
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

    _last_exit_time[ticker] = datetime.now(timezone.utc)

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

    # ── Dual-Index Confluence Shield (v3.2.0) ────────────────────────────────
    # Same shield as the main bot: BOTH SPY+QQQ 5m close < AVWAP required.
    lords_left = _dual_index_eject("long")

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

        # ── Confluence Shield: BOTH SPY+QQQ 5m_close < AVWAP ─────────────────
        if lords_left:
            tickers_to_close.append((ticker, current_price, "LORDS_LEFT[5m]"))
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

    # ── Dual-Index Confluence Shield (v3.2.0) ────────────────────────────────
    # Exit all shorts ONLY when BOTH SPY and QQQ have a finalized 5-min close
    # above their respective AVWAPs. Filters sub-5-min liquidity probes and
    # sector divergence ("Hormuz" wicks). Replaces v2.9.8's 1-min OR test.
    bull_vacuum = _dual_index_eject("short")

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

        # ── Confluence Shield: BOTH SPY+QQQ 5m_close > AVWAP ─────────────────
        if not exit_reason and bull_vacuum:
            exit_reason = "BULL_VACUUM[5m]"

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

        # ── Confluence Shield: BOTH SPY+QQQ 5m_close > AVWAP ─────────────────
        if not exit_reason and bull_vacuum:
            exit_reason = "BULL_VACUUM[5m]"

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

    # Paper EOD summary — includes longs (paper_trades SELLs) AND shorts
    # (short_trade_history COVERs). Same for TP. See _today_pnl_breakdown().
    _, _, total_pnl, wins, losses, n_trades = _today_pnl_breakdown(is_tp=False)
    msg = (
        f"EOD CLOSE Complete\n"
        f"  Trades: {n_trades}  W/L: {wins}/{losses}\n"
        f"  Day P&L: ${total_pnl:+.2f}\n"
        f"  Cash: ${paper_cash:,.2f}"
    )
    send_telegram(msg)

    # TP EOD summary
    _, _, tp_total_pnl, tp_wins, tp_losses, tp_n_trades = _today_pnl_breakdown(is_tp=True)
    tp_msg = (
        f"[TP] EOD CLOSE Complete\n"
        f"  Trades: {tp_n_trades}  W/L: {tp_wins}/{tp_losses}\n"
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
def _build_eod_report(today: str, portfolio: str) -> str:
    """Build EOD report text for one portfolio. portfolio in {'paper','tp'}.

    v3.4.6: includes shorts. Previously only counted long SELLs (action='SELL'
    in paper_trades), so paper short COVERs (logged to short_trade_history
    with action='COVER') were silently dropped. All-time totals also excluded
    short P&L. This rebuilds the report from trade_history + short_trade_history
    so longs and shorts are both counted, with a per-trade label.
    """
    SEP = "\u2500" * 34
    if portfolio == "paper":
        long_hist = trade_history
        short_hist = short_trade_history
        title = "PAPER PORTFOLIO"
    else:
        long_hist = tp_trade_history
        short_hist = tp_short_trade_history
        title = "TP PORTFOLIO"

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
    """Auto EOD report at 15:58 ET. Paper → send_telegram(), TP → send_tp_telegram().

    v3.4.6: includes paper + TP shorts (previously dropped because the report
    filtered paper_trades for action='SELL', which excludes COVER records).
    """
    now_et = _now_et()
    today = now_et.strftime("%Y-%m-%d")
    send_telegram(_build_eod_report(today, "paper"))
    send_tp_telegram(_build_eod_report(today, "tp"))


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

    # Merge long + short history so weekly digest covers all closed trades.
    # Long closes live in trade_history; short COVERs live in short_trade_history.
    paper_combined = list(trade_history) + list(short_trade_history)
    tp_combined = list(tp_trade_history) + list(tp_short_trade_history)
    paper_digest = _build_digest(paper_combined, "PAPER PORTFOLIO")
    send_telegram(paper_digest)

    tp_digest = _build_digest(tp_combined, "TP PORTFOLIO")
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
        with open(TP_STATE_FILE, "r", encoding="utf-8") as f:
            ts = json.load(f)
        p_cash = ps.get("paper_cash", 0)
        t_cash = ts.get("tp_paper_cash", 0)
        return "\u2705 paper $%s | TP $%s" % (format(int(p_cash), ","), format(int(t_cash), ","))
    except Exception as exc:
        return "\u274c %s" % exc


def _test_positions():
    """Test positions — returns status string."""
    n_paper = len(positions) + len(short_positions)
    n_tp = len(tp_positions) + len(tp_short_positions)
    return "%d paper | %d TP" % (n_paper, n_tp)


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
    n_tp = len(tp_positions)
    n_short = len(short_positions)
    n_tp_short = len(tp_short_positions)
    logger.info("Scanning %d stocks | pos=%d tp=%d short=%d tp_short=%d | mode=%s",
                len(TRADE_TICKERS), n_pos, n_tp, n_short, n_tp_short, _current_mode)

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
        logger.info("SCAN CYCLE done in %.2fs — paused (manage only)", time.time() - cycle_start)
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

    logger.info("SCAN CYCLE done in %.2fs — %d tickers", time.time() - cycle_start, len(TRADE_TICKERS))


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
        "\n"
        "Tip: /menu for tap buttons\n"
        "```"
    )
    await update.message.reply_text(
        body,
        parse_mode="Markdown",
        reply_markup=_menu_button(),
    )


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
        # TP portfolio only — Day P&L includes long SELLs + short COVERs
        n_tp_pos = len(tp_positions) + len(tp_short_positions)
        _, _, tp_day_pnl, _, _, _ = _today_pnl_breakdown(is_tp=True)
        tp_cash_fmt = "%s" % format(tp_paper_cash, ",.2f")
        tp_day_pnl_fmt = "%s" % format(tp_day_pnl, "+,.2f")
        lines += [
            "\U0001f4cb TP PORTFOLIO",
            "  Cash:       $%s" % tp_cash_fmt,
            "  Positions:  %d open" % n_tp_pos,
            "  Today P&L:  $%s" % tp_day_pnl_fmt,
        ]
    else:
        # Paper portfolio only — Day P&L includes long SELLs + short COVERs
        n_pos = len(positions) + len(short_positions)
        _, _, day_pnl, _, _, _ = _today_pnl_breakdown(is_tp=False)

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
    t0 = asyncio.get_event_loop().time()
    await update.message.reply_chat_action(ChatAction.TYPING)
    prog = await update.message.reply_text("\u23f3 Loading dashboard (~3s)...")
    is_tp = is_tp_update(update)
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, _dashboard_sync, is_tp)
    try:
        if len(text) > 3800:
            await prog.delete()
            await _reply_in_chunks(update.message, text, reply_markup=_menu_button())
        else:
            await prog.edit_text(text, reply_markup=_menu_button())
    except Exception:
        pass
    logger.info("CMD dashboard completed in %.2fs", asyncio.get_event_loop().time() - t0)


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

        # Portfolio equity summary.
        # Short accounting: on entry, proceeds (entry_px * shares) are
        # credited to cash AND we owe a liability equal to the current
        # buy-back cost (current_px * shares). The equity contribution
        # of a short is therefore short_unreal = (entry_px - current_px)
        # * shares, NOT entry_px * shares. Previously we added
        # entry_px * shares to market value, which double-counted the
        # short-sale proceeds and inflated equity by roughly the short
        # principal.
        tp_short_unreal = 0.0
        tp_short_liability = 0.0  # current buy-back cost
        for s_ticker, s_pos in tp_short_positions.items():
            s_bars = fetch_1min_bars(s_ticker)
            cur_px = s_bars["current_price"] if s_bars else s_pos["entry_price"]
            tp_short_unreal += (s_pos["entry_price"] - cur_px) * s_pos["shares"]
            tp_short_liability += cur_px * s_pos["shares"]
        tp_all_unreal = total_unreal_pnl + tp_short_unreal
        tp_equity = tp_paper_cash + total_market_value - tp_short_liability
        tp_vs_start = tp_equity - PAPER_STARTING_CAPITAL
        lines.append(sep)
        lines.append("\U0001f4bc Portfolio Snapshot")
        lines.append("  Cash:          $%s" % format(tp_paper_cash, ",.2f"))
        lines.append("  Long MV:       $%s" % format(total_market_value, ",.2f"))
        if tp_short_liability > 0:
            lines.append("  Short Liab:    $%s" % format(tp_short_liability, ",.2f"))
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
    t0 = asyncio.get_event_loop().time()
    await update.message.reply_chat_action(ChatAction.TYPING)
    is_tp = is_tp_update(update)
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, _status_text_sync, is_tp)

    refresh_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("\U0001f504 Refresh", callback_data="positions_refresh")
    ]])
    await update.message.reply_text(text, reply_markup=refresh_kb)

    # Portfolio pie chart (run in thread to avoid blocking event loop)
    sent_photo = False
    if is_tp:
        if MATPLOTLIB_AVAILABLE and (tp_positions or tp_short_positions):
            buf = await loop.run_in_executor(None, _chart_portfolio_pie, tp_positions, tp_short_positions, tp_paper_cash)
            if buf:
                await update.message.reply_photo(photo=buf, caption="TP Portfolio Allocation", reply_markup=_menu_button())
                sent_photo = True
    else:
        if MATPLOTLIB_AVAILABLE and (positions or short_positions):
            buf = await loop.run_in_executor(None, _chart_portfolio_pie, positions, short_positions, paper_cash)
            if buf:
                await update.message.reply_photo(photo=buf, caption="Portfolio Allocation", reply_markup=_menu_button())
                sent_photo = True

    if not sent_photo:
        await update.effective_message.reply_text("\u2500", reply_markup=_menu_button())
    logger.info("CMD status completed in %.2fs", asyncio.get_event_loop().time() - t0)


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
    vs_start = equity - PAPER_STARTING_CAPITAL
    lines.append(sep)
    lines.append("\U0001f4bc Portfolio Snapshot")
    lines.append("  Cash:          $%s" % format(cash, ",.2f"))
    lines.append("  Long MV:       $%s" % format(total_market_value, ",.2f"))
    if short_liability > 0:
        lines.append("  Short Liab:    $%s" % format(short_liability, ",.2f"))
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


def _open_positions_as_pseudo_trades(is_tp=False, target_date=None):
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
    long_pos = tp_positions if is_tp else positions
    short_pos = tp_short_positions if is_tp else short_positions

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


async def cmd_eod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-send the EOD report for today on demand (paper or TP, based on chat)."""
    today = _now_et().strftime("%Y-%m-%d")
    portfolio = "tp" if is_tp_update(update) else "paper"
    await update.message.reply_text(_build_eod_report(today, portfolio))


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
    if is_tp_update(update):
        tp_long = [
            t for t in tp_trade_history
            if t.get("date", "") == target_str
        ]
        tp_short = [
            t for t in tp_short_trade_history
            if t.get("date", "") == target_str
        ]
        # v3.3.1: include currently-open positions as pseudo-trades
        # when the target date matches today. Past-date reports only
        # show completed history.
        if target_str == today_str:
            tp_long_open, tp_short_open = _open_positions_as_pseudo_trades(
                is_tp=True, target_date=target_str,
            )
        else:
            tp_long_open, tp_short_open = [], []
        all_tp = tp_long + tp_short + tp_long_open + tp_short_open
        if not all_tp:
            await update.effective_message.reply_text(
                "No trades on {date}.".format(date=target_str),
                reply_markup=_menu_button()
            )
            logger.info("CMD dayreport completed in %.2fs (no trades)", asyncio.get_event_loop().time() - t0)
            return
        body = _format_dayreport_section(all_tp, header, "TP")
        await _reply_in_chunks(update.message, body)
        # Chart: Trade P&L bar chart
        if MATPLOTLIB_AVAILABLE:
            chart_msg = await update.message.reply_text("\U0001f4ca Generating chart...")
            loop = asyncio.get_event_loop()
            buf = await loop.run_in_executor(None, _chart_dayreport, all_tp, day_label)
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
    # v3.3.1: include currently-open positions as pseudo-trades when
    # target date matches today. Past-date reports stay history-only.
    if target_str == today_str:
        paper_long_open, paper_short_open = _open_positions_as_pseudo_trades(
            is_tp=False, target_date=target_str,
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


def _collect_day_rows(target_str, today_str, is_tp):
    """Collect all trade-log rows for one day, normalized.

    Returns a list of dicts:
      {"tm": "HH:MM", "ticker": str,
       "action": "BUY"|"SELL"|"SHORT"|"COVER",
       "shares": int, "price": float,
       "stop": float (BUY/SHORT only),
       "pnl": float (SELL/COVER only),
       "pnl_pct": float (SELL/COVER only)}

    v3.4.7: previously the same-day branch only pulled from paper_trades /
    tp_paper_trades, which never contain shorts. Today's shorts (open or
    closed) were silently invisible. Now we pull from four sources for the
    today branch and synthesize rows from history for past dates.
    """
    rows = []
    is_today = (target_str == today_str)

    if is_tp:
        live_long = tp_paper_trades
        long_hist = tp_trade_history
        short_hist = tp_short_trade_history
        open_shorts = tp_short_positions
    else:
        live_long = paper_trades
        long_hist = trade_history
        short_hist = short_trade_history
        open_shorts = short_positions

    if is_today:
        # Long opens + closes are already in paper_trades / tp_paper_trades
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


def _log_sync(target_str, day_label, is_tp):
    """Build trade log text (pure CPU — run in executor). Returns text or None."""
    SEP = "\u2500" * 34
    today_str = _now_et().strftime("%Y-%m-%d")
    rows = _collect_day_rows(target_str, today_str, is_tp)
    if not rows:
        return None

    prefix = "[TP] " if is_tp else ""
    lines = [
        "\U0001f4cb %sTrade Log \u2014 %s" % (prefix, day_label),
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

    n_open = (len(tp_positions) + len(tp_short_positions)) if is_tp \
             else (len(positions) + len(short_positions))
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
    is_tp = is_tp_update(update)

    loop = asyncio.get_event_loop()
    try:
        text = await asyncio.wait_for(
            loop.run_in_executor(None, _log_sync, target_str, day_label, is_tp),
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
        prefix = "[TP] " if is_tp else ""
        try:
            await prog.edit_text("%sNo trades on %s." % (prefix, day_label), reply_markup=_menu_button())
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


def _replay_sync(target_str, day_label, is_tp):
    """Build replay text (pure CPU — run in executor). Returns text or None."""
    SEP = "\u2500" * 34
    today_str = _now_et().strftime("%Y-%m-%d")

    # Normalize every source into a common row shape:
    #   {"tm": "HH:MM", "ticker": str, "action": "BUY"|"SELL"|"SHORT"|"COVER",
    #    "price": float, "pnl": float (0 for opens)}
    # Same-day sources (paper_trades / tp_paper_trades) already use
    # time/price/action. Historical sources (trade_history /
    # short_trade_history) store one record per CLOSED trade with
    # entry_time/entry_price and exit_time/exit_price, so we synthesize
    # both an open row and a close row for each.
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

    if is_tp:
        prefix = "[TP] "
        if target_str == today_str:
            _push_live(tp_paper_trades)
            # v3.4.7: today's shorts (closed + open) live elsewhere
            _push_history(tp_short_trade_history, "SHORT", "COVER")
            _push_open_shorts(tp_short_positions)
        else:
            _push_history(tp_trade_history, "BUY", "SELL")
            _push_history(tp_short_trade_history, "SHORT", "COVER")
    else:
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
    is_tp = is_tp_update(update)

    loop = asyncio.get_event_loop()
    try:
        text = await asyncio.wait_for(
            loop.run_in_executor(None, _replay_sync, target_str, day_label, is_tp),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        logger.warning("cmd_replay: executor timed out after 15s")
        await update.message.reply_text("\u26a0\ufe0f Replay timed out. Try again.", reply_markup=_menu_button())
        return

    if text is None:
        prefix = "[TP] " if is_tp else ""
        await update.message.reply_text("%sNo trades on %s." % (prefix, day_label), reply_markup=_menu_button())
        logger.info("CMD replay completed in %.2fs (no trades)", asyncio.get_event_loop().time() - t0)
        return

    await _reply_in_chunks(update.message, text, reply_markup=_menu_button())
    logger.info("CMD replay completed in %.2fs", asyncio.get_event_loop().time() - t0)


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show version info."""
    await update.message.reply_text(
        "Stock Spike Monitor v%s\n%s" % (BOT_VERSION, RELEASE_NOTE),
        reply_markup=_menu_button())


# ============================================================
# /mode COMMAND — market mode classifier (observation only)
# ============================================================
async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the current MarketMode classification and its profile.
    OBSERVATION ONLY in this version — no trading parameter reads from it yet.
    """
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
        "Max  : 5 entries per ticker/day (long + short combined)\n"
        "OR   : 8:30\u20138:35 CT (first 5 min)\n"
        "Scan : every 60s \u2192 8:35\u20142:55 CT\n"
        "EOD  : force-close all at 2:55 CT\n"
        f"{SEP}\n"
        "\U0001f6e1 DUAL-INDEX CONFLUENCE SHIELD (v3.2.0)\n"
        "  Lords Left & Bull Vacuum require\n"
        "  BOTH SPY AND QQQ to confirm on a\n"
        "  finalized 5-min bar close \u2192 no\n"
        "  wick-outs, no sector divergence\n"
        "  ejects (\"Hormuz\" wick filter)\n"
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
        "  \u2022 SPY > AVWAP\n"
        "  \u2022 QQQ > AVWAP\n"
        "Stop: OR High \u2212 $0.90\n"
        "Trail: +1.0% trigger | max(1.0%, $1.00) distance\n"
        "Size: 10 shares \u00b7 limit order\n"
        "Max: 5 entries/ticker/day\n"
        "EOD: closes at 2:55 CT\n"
        "\n"
        "Eye of the Tiger exits:\n"
        "  \U0001f56f Red Candle\n"
        "     price < Open OR < PDC\n"
        "  \U0001f451 Lords Left\n"
        "     SPY AND QQQ < AVWAP\n"
        "     on finalized 5m close\n"
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
        "Max: 5 entries/ticker/day\n"
        "EOD: closes at 2:55 CT\n"
        "\n"
        "Eye of the Tiger exits:\n"
        "  \U0001f300 Bull Vacuum\n"
        "     SPY AND QQQ > AVWAP\n"
        "     on finalized 5m close\n"
        "  \U0001f504 Polarity Shift\n"
        "     price > PDC (1m close)\n"
        f"{SEP}\n"
        "\U0001f6e1 Confluence Shield (v3.2.0)\n"
        "  Global eject requires BOTH\n"
        "  indices on a finalized 5m\n"
        "  close \u2014 filters wicks &\n"
        "  sector divergence\n"
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


def _reset_authorized(query) -> tuple:
    """Gatekeeper for /reset callbacks.

    Returns (allowed: bool, reason: str). Checks:
      1. The tap came from the bot owner (chat_id matches the paper or TP
         chat). Prevents anyone else added to the chat from wiping state.
      2. The tap was routed to a bot whose portfolio the action matches
         (paper reset must come from paper bot, TP reset from TP bot;
         'both' may come from either).
      3. The confirm button has a timestamp within
         RESET_CONFIRM_WINDOW_SEC. Prevents stale-message replay.
    """
    data = query.data or ""
    chat_id_str = str(query.message.chat_id)
    from_bot_is_tp = (str(query.message.chat_id) == TELEGRAM_TP_CHAT_ID)

    # (1) Owner check — chat_id must be one of the two known chats.
    if chat_id_str != TELEGRAM_TP_CHAT_ID and chat_id_str != str(CHAT_ID or ""):
        return (False, "unauthorized chat")

    # (2) Bot/action match — only the confirm variants carry an action.
    if data.startswith("reset_paper_confirm") and from_bot_is_tp:
        return (False, "paper reset must be confirmed from paper bot")
    if data.startswith("reset_tp_confirm") and not from_bot_is_tp:
        return (False, "TP reset must be confirmed from TP bot")

    # (3) Freshness check — confirm callbacks carry ':<unix_ts>' suffix.
    if "_confirm" in data and ":" in data:
        try:
            ts = int(data.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            return (False, "malformed timestamp")
        age = time.time() - ts
        if age < -5:   # future-dated beyond clock-skew tolerance
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
    """/reset paper | /reset tp | /reset both — show confirmation before reset."""
    args = context.args
    target = args[0].lower() if args else ""

    if target == "paper":
        await update.message.reply_text(
            "\u26a0\ufe0f Reset paper portfolio to $100,000?\nAll trade history will be cleared.\n(Confirm within 60s.)",
            reply_markup=_reset_buttons("paper"),
        )
    elif target == "tp":
        await update.message.reply_text(
            "\u26a0\ufe0f Reset TP portfolio to $100,000?\nAll trade history will be cleared.\n(Confirm within 60s.)",
            reply_markup=_reset_buttons("tp"),
        )
    elif target == "both":
        await update.message.reply_text(
            "\u26a0\ufe0f Reset BOTH portfolios to $100,000?\nAll trade history will be cleared.\n(Confirm within 60s.)",
            reply_markup=_reset_buttons("both"),
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
    """Handle inline keyboard taps for /reset confirmation.

    Confirm callbacks carry a ':<ts>' suffix. _reset_authorized() enforces
    chat-ownership, bot/action match, and freshness. The 'reset_*'
    (non-confirm) and 'reset_cancel' variants carry no state change and
    only need the owner check.
    """
    query = update.callback_query
    await query.answer()
    capital_fmt = format(PAPER_STARTING_CAPITAL, ",.0f")

    allowed, reason = _reset_authorized(query)
    if not allowed:
        logger.warning(
            "reset_callback blocked: data=%s chat_id=%s reason=%s",
            query.data, query.message.chat_id, reason,
        )
        await query.edit_message_text("\u274c Reset blocked: %s." % reason)
        return

    # Confirm variants carry ':<ts>' — strip before dispatching.
    action = query.data.split(":", 1)[0]

    if action == "reset_paper_confirm":
        _do_reset_paper()
        await query.edit_message_text("\u2705 Paper portfolio reset to $%s." % capital_fmt)
    elif action == "reset_tp_confirm":
        _do_reset_tp()
        await query.edit_message_text("\u2705 TP portfolio reset to $%s." % capital_fmt)
    elif action == "reset_both_confirm":
        _do_reset_paper()
        _do_reset_tp()
        await query.edit_message_text("\u2705 Both portfolios reset to $%s." % capital_fmt)
    elif action == "reset_cancel":
        await query.edit_message_text("\u274c Reset cancelled.")
    elif action == "reset_paper":
        await query.edit_message_text(
            "\u26a0\ufe0f Reset paper portfolio to $100,000?\nAll trade history will be cleared.\n(Confirm within 60s.)",
            reply_markup=_reset_buttons("paper"),
        )
    elif action == "reset_tp":
        await query.edit_message_text(
            "\u26a0\ufe0f Reset TP portfolio to $100,000?\nAll trade history will be cleared.\n(Confirm within 60s.)",
            reply_markup=_reset_buttons("tp"),
        )
    elif action == "reset_both":
        await query.edit_message_text(
            "\u26a0\ufe0f Reset BOTH portfolios to $100,000?\nAll trade history will be cleared.\n(Confirm within 60s.)",
            reply_markup=_reset_buttons("both"),
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

    # Select history based on which bot
    if is_tp_update(update):
        long_history = tp_trade_history
        short_hist = tp_short_trade_history
        label = "TP Portfolio"
    else:
        long_history = trade_history
        short_hist = short_trade_history
        label = "Paper Portfolio"

    # v3.3.1: also consider currently-open positions so an open-but-
    # uncovered entry (which is invisible in trade_history until exit)
    # doesn't make /perf claim there's nothing to show.
    long_opens, short_opens = _open_positions_as_pseudo_trades(
        is_tp=is_tp_update(update),
    )

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
def _proximity_sync(is_tp: bool = False):
    """Build proximity text (blocking I/O \u2014 run in executor).

    Shows how far each ticker is from its OR-breakout trigger, plus the
    SPY/QQQ vs AVWAP global gate. Read-only diagnostic view \u2014 does
    NOT change any trade logic or adaptive parameters.

    Every visible line is <= 34 chars incl. leading 2-space indent so it
    renders without wrap inside a Telegram mobile monospace block.

    is_tp selects which positions dicts are consulted for open-trade
    markers (\U0001f7e2 long / \U0001f534 short) \u2014 global market
    state is the same either way.

    Returns (text, None) on success or (None, err_msg) on no-data.
    """
    SEP = "\u2500" * 34
    now_et = _now_et()
    today = now_et.strftime("%Y-%m-%d")

    if or_collected_date != today:
        return None, "OR not collected yet \u2014 runs at 8:35 CT."

    # Pick the right positions dicts for open-trade markers
    longs_dict = tp_positions if is_tp else positions
    shorts_dict = tp_short_positions if is_tp else short_positions

    # --- Global: SPY/QQQ vs AVWAP (the long gate) ---
    spy_bars = fetch_1min_bars("SPY")
    qqq_bars = fetch_1min_bars("QQQ")
    spy_price = spy_bars["current_price"] if spy_bars else 0.0
    qqq_price = qqq_bars["current_price"] if qqq_bars else 0.0
    spy_avwap = avwap_data["SPY"]["avwap"]
    qqq_avwap = avwap_data["QQQ"]["avwap"]

    spy_have = spy_price > 0 and spy_avwap > 0
    qqq_have = qqq_price > 0 and qqq_avwap > 0
    spy_ok = spy_have and spy_price > spy_avwap
    qqq_ok = qqq_have and qqq_price > qqq_avwap
    spy_icon = "\u2705" if spy_ok else "\u274c"
    qqq_icon = "\u2705" if qqq_ok else "\u274c"

    long_ok = spy_ok and qqq_ok
    # Short anchor is the mirror: SPY AND QQQ both BELOW AVWAP enables shorts.
    short_ok = (spy_have and qqq_have
                and spy_price < spy_avwap
                and qqq_price < qqq_avwap)

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

    lines.append(_idx_row("SPY", spy_price, spy_avwap, spy_icon))
    lines.append(_idx_row("QQQ", qqq_price, qqq_avwap, qqq_icon))
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
    is_tp = is_tp_update(update)
    loop = asyncio.get_event_loop()
    text, err = await loop.run_in_executor(None, _proximity_sync, is_tp)
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
    is_tp = (str(query.message.chat_id) == TELEGRAM_TP_CHAT_ID)
    loop = asyncio.get_event_loop()
    text, err = await loop.run_in_executor(None, _proximity_sync, is_tp)
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
    is_tp = (str(query.message.chat_id) == TELEGRAM_TP_CHAT_ID)

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
            text = await loop.run_in_executor(None, _dashboard_sync, is_tp)
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
# TELEGRAM BOT SETUP
# ============================================================
# Commands shown in the Telegram / menu (user-facing). /positions and /or_now
# remain registered as silent aliases in add_handler() but are intentionally
# omitted here to keep the menu tight.
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
    BotCommand("test", "Run system health test"),
    BotCommand("menu", "Quick command menu"),
    BotCommand("strategy", "Strategy summary"),
    BotCommand("algo", "Algorithm reference PDF"),
    BotCommand("version", "Release notes"),
    BotCommand("help", "Command menu"),
    BotCommand("reset", "Reset portfolio"),
]

TP_BOT_COMMANDS = list(MAIN_BOT_COMMANDS)


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
    # Send startup menu (only for single-bot mode; dual-bot sends from _run_both)
    if not TELEGRAM_TP_TOKEN:
        await _send_startup_menu(app.bot, CHAT_ID)


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


async def _send_startup_menu(bot, chat_id):
    """Send the interactive menu to a chat on startup/deploy."""
    reply_markup = InlineKeyboardMarkup(_build_menu_keyboard())
    startup_text = (
        "\U0001f7e2 Stock Spike Monitor v%s online\n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "\U0001f5c2 Menu"
    ) % BOT_VERSION
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
    app.add_handler(CommandHandler("eod", cmd_eod))
    app.add_handler(CommandHandler("version", cmd_version))
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
    app.add_handler(CommandHandler("or_now", cmd_or_now))
    app.add_handler(CommandHandler("menu", cmd_menu))

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
    tp_app.add_handler(CommandHandler("mode", cmd_mode))
    tp_app.add_handler(CommandHandler("reset", cmd_reset))
    tp_app.add_handler(CommandHandler("perf", cmd_perf))
    tp_app.add_handler(CommandHandler("price", cmd_price))
    tp_app.add_handler(CommandHandler("orb", cmd_orb))
    tp_app.add_handler(CommandHandler("proximity", cmd_proximity))
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
    tp_app.add_handler(CallbackQueryHandler(proximity_callback, pattern="^proximity_refresh$"))
    tp_app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu_"))
    tp_app.add_handler(CallbackQueryHandler(_cb_open_menu, pattern="^open_menu$"))

    tp_app.add_error_handler(_error_handler)

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
                # Send startup menu to both chats
                await _send_startup_menu(app.bot, CHAT_ID)
                await _send_startup_menu(tp_app.bot, TELEGRAM_TP_CHAT_ID)
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

# Live dashboard (read-only web UI). Env-gated: off unless DASHBOARD_PASSWORD is set.
# Runs in its own thread with its own asyncio loop — never touches PTB's loop.
try:
    import dashboard_server
    dashboard_server.start_in_thread()
except Exception as _dash_err:
    logger.warning("Dashboard failed to start (bot continues): %s", _dash_err)

# Startup summary
logger.info(
    "=== STARTUP SUMMARY === v%s | paper: $%.2f cash, %d pos, %d trades | TP: $%.2f cash, %d pos",
    BOT_VERSION, paper_cash, len(positions), len(trade_history),
    tp_paper_cash, len(tp_positions),
)

# Smoke-test guard — lets smoke_test.py import this module without booting
# the Telegram client, scheduler, OR-collector, or dashboard. The test
# script sets SSM_SMOKE_TEST=1 before import. This is the ONLY place
# where that env var is read.
if os.getenv("SSM_SMOKE_TEST", "").strip() == "1":
    logger.info("SSM_SMOKE_TEST=1 \u2014 skipping catch-up, scheduler, and Telegram loop")
else:
    # Startup catch-up
    startup_catchup()

    # Background threads
    threading.Thread(target=scheduler_thread, daemon=True).start()
    threading.Thread(target=health_ping, daemon=True).start()

    logger.info("Stock Spike Monitor v%s started", BOT_VERSION)
    send_startup_message()
    run_telegram_bot()
