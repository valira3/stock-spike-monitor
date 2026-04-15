"""
Stock Spike Monitor v2.8.0 — Clean ORB Momentum Breakout
=========================================================
10-ticker universe, Opening Range breakout, $0.50 stepped trail.
Infrastructure: Telegram bot, paper trading, TradersPost webhook, scheduler.
"""

import os
import json
import time
import logging
import threading
import urllib.request
import asyncio
import signal
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import (
    BotCommand, BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats, Update,
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
)

# ============================================================
# CONFIG FROM ENVIRONMENT VARIABLES
# ============================================================
TELEGRAM_TOKEN          = os.getenv("TELEGRAM_TOKEN")
CHAT_ID                 = os.getenv("CHAT_ID")
TRADERSPOST_WEBHOOK_URL = os.getenv("TRADERSPOST_WEBHOOK_URL")
TELEGRAM_TP_CHAT_ID     = os.getenv("TELEGRAM_TP_CHAT_ID")
TELEGRAM_TP_TOKEN       = os.getenv("TELEGRAM_TP_TOKEN")

BOT_VERSION = "2.8.0"
RELEASE_NOTE = (
    "v2.8.0: Clean slate — ORB momentum breakout only, "
    "10-ticker universe, $0.50 stop, stepped trail"
)

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

# ============================================================
# PAPER TRADING CONFIG
# ============================================================
PAPER_LOG              = os.getenv("PAPER_LOG_PATH", "investment.log")
PAPER_STATE_FILE       = os.getenv("PAPER_STATE_PATH", "paper_state.json")
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
TRAIL_TRIGGER  = 1.00    # Activate trail at +$1.00/share above entry
TRAIL_STEP     = 0.50    # Ratchet step

SCAN_INTERVAL  = 60      # seconds between scans
YAHOO_TIMEOUT  = 8       # seconds
YAHOO_HEADERS  = {"User-Agent": "Mozilla/5.0"}

# ============================================================
# GLOBAL STATE
# ============================================================

# OR data — populated at 09:35 ET
or_high: dict = {}                  # ticker -> OR high price
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
daily_entry_count: dict = {}   # ticker -> count (max 2)
daily_entry_date: str = ""

# Paper trading log (today's trades)
paper_trades: list = []

# Paper cash and all-time trades
paper_cash: float = PAPER_STARTING_CAPITAL
paper_all_trades: list = []

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
        "pdc": pdc,
        "or_collected_date": or_collected_date,
        "user_config": user_config,
        "tp_state": tp_state,
        "saved_at": datetime.now(ET).isoformat(),
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
    global or_high, pdc, or_collected_date
    global user_config, tp_state, tp_dm_chat_id

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
        pdc.update(state.get("pdc", {}))
        or_collected_date = state.get("or_collected_date", "")
        user_config.update(state.get("user_config", {}))
        tp_state.update(state.get("tp_state", {}))

        # Reset daily counts if saved on a different day
        today = datetime.now(ET).strftime("%Y-%m-%d")
        if daily_entry_date != today:
            daily_entry_count.clear()
            paper_trades.clear()

        logger.info("Loaded paper state: cash=$%.2f, %d positions",
                    paper_cash, len(positions))
    except Exception as e:
        logger.error("load_paper_state failed: %s — starting fresh", e)


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
    """Send to TP user's DM chat. Falls back to main channel."""
    chat_id = tp_dm_chat_id or TELEGRAM_TP_CHAT_ID
    if not chat_id:
        send_telegram("[TP] %s" % message)
        return
    token = TELEGRAM_TP_TOKEN or TELEGRAM_TOKEN
    if not token:
        return
    try:
        payload = json.dumps({"chat_id": chat_id, "text": message}).encode()
        url = "https://api.telegram.org/bot%s/sendMessage" % token
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.error("[TP] Failed to send DM: %s", e)


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


# ============================================================
# OR COLLECTION (Opening Range)
# ============================================================
def collect_or():
    """Collect Opening Range data at 09:35 ET.

    For each ticker: find bars in [09:30, 09:35) ET, record max high as OR_High
    and previous day close as PDC.
    """
    global or_collected_date
    now_et = datetime.now(ET)
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
            for i, ts in enumerate(bars["timestamps"]):
                if open_ts <= ts < end_ts:
                    h = bars["highs"][i]
                    if h is None:
                        h = bars["closes"][i]
                    if h is not None:
                        if max_high is None or h > max_high:
                            max_high = h

            if max_high is None:
                logger.warning("OR: No bars in [09:30,09:35) for %s", ticker)
                continue

            or_high[ticker] = max_high
            pdc[ticker] = bars["pdc"]
            logger.info("OR collected: %s OR_high=%.2f PDC=%.2f",
                        ticker, or_high[ticker], pdc[ticker])
        except Exception as e:
            logger.error("OR collection error for %s: %s", ticker, e)

    or_collected_date = today
    save_paper_state()

    # Send summary
    lines = ["Opening Range Collected (%s):" % today]
    for t in TICKERS:
        if t in or_high:
            lines.append("  %s  OR_H=%.2f  PDC=%.2f" % (t, or_high[t], pdc.get(t, 0)))
        else:
            lines.append("  %s  MISSING" % t)
    send_telegram("\n".join(lines))


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
    now_et = datetime.now(ET)
    today = now_et.strftime("%Y-%m-%d")

    # Reset daily entry counts if new day
    global daily_entry_date
    if daily_entry_date != today:
        daily_entry_count.clear()
        daily_entry_date = today

    # Timing gate: after 09:35 ET
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

    # Daily entry cap (max 2)
    if daily_entry_count.get(ticker, 0) >= 2:
        return False, None

    # Not already in position
    if ticker in positions:
        return False, None

    # Fetch current bar
    bars = fetch_1min_bars(ticker)
    if not bars:
        return False, None

    current_price = bars["current_price"]
    closes = [c for c in bars["closes"] if c is not None]
    last_close = closes[-1] if closes else current_price

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

    action: 'buy' or 'sell'
    Returns response dict or None.
    """
    if PAPER_MODE or not TRADERSPOST_WEBHOOK_URL:
        if not TRADERSPOST_WEBHOOK_URL:
            logger.debug("[TP] No webhook URL configured")
        return None

    # Limit price: buy slightly above, sell slightly below
    if action == "buy":
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

        now_str = datetime.now(ET).isoformat()
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
    global paper_cash

    limit_price = round(current_price + 0.02, 2)
    stop_price = round(current_price - STOP_OFFSET, 2)
    entry_num = daily_entry_count.get(ticker, 0) + 1
    now_str = datetime.now(ET).isoformat()

    positions[ticker] = {
        "entry_price": current_price,
        "shares": SHARES,
        "stop": stop_price,
        "trail_active": False,
        "trail_high": current_price,
        "entry_count": entry_num,
        "entry_time": now_str,
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
        "time": now_str,
    }
    paper_trades.append(trade)
    paper_all_trades.append(trade)

    paper_log("BUY %s %d @ $%.2f (limit $%.2f) stop=$%.2f entry#%d"
              % (ticker, SHARES, current_price, limit_price, stop_price, entry_num))

    # TradersPost webhook
    send_traderspost_order(ticker, "buy", current_price)

    # Telegram notification
    or_h = or_high.get(ticker, 0)
    msg = (
        "ENTRY %s\n"
        "  Price:  $%.2f  (limit $%.2f)\n"
        "  Stop:   $%.2f\n"
        "  OR High: $%.2f\n"
        "  Entry #%d today"
    ) % (ticker, current_price, limit_price, stop_price, or_h, entry_num)
    send_telegram(msg)

    save_paper_state()


# ============================================================
# CLOSE POSITION
# ============================================================
def close_position(ticker, price, reason="STOP"):
    """Close position: remove, log P&L, send webhook + Telegram."""
    global paper_cash

    if ticker not in positions:
        return

    pos = positions.pop(ticker)
    entry_price = pos["entry_price"]
    shares = pos["shares"]
    pnl = (price - entry_price) * shares
    pnl_pct = ((price - entry_price) / entry_price * 100) if entry_price else 0
    now_str = datetime.now(ET).isoformat()

    # Paper accounting
    proceeds = price * shares
    paper_cash += proceeds

    trade = {
        "action": "SELL",
        "ticker": ticker,
        "price": price,
        "shares": shares,
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "reason": reason,
        "entry_price": entry_price,
        "time": now_str,
    }
    paper_trades.append(trade)
    paper_all_trades.append(trade)

    paper_log("SELL %s %d @ $%.2f reason=%s pnl=$%.2f (%.1f%%)"
              % (ticker, shares, price, reason, pnl, pnl_pct))

    # TradersPost webhook
    send_traderspost_order(ticker, "sell", price, shares)

    # Telegram
    msg = (
        "EXIT %s  [%s]\n"
        "  Entry:  $%.2f\n"
        "  Exit:   $%.2f\n"
        "  P&L:    $%+.2f  (%+.1f%%)"
    ) % (ticker, reason, entry_price, price, pnl, pnl_pct)
    send_telegram(msg)

    save_paper_state()


# ============================================================
# MANAGE POSITIONS (stop + trail logic)
# ============================================================
def manage_positions():
    """Check stops and update trailing stops for all open positions."""
    tickers_to_close = []

    for ticker in list(positions.keys()):
        bars = fetch_1min_bars(ticker)
        if not bars:
            continue

        current_price = bars["current_price"]
        pos = positions[ticker]

        # Check stop hit
        if current_price <= pos["stop"]:
            tickers_to_close.append((ticker, current_price, "STOP"))
            continue

        entry_price = pos["entry_price"]

        # Trail logic
        if not pos["trail_active"]:
            # Activate trail at +$1.00/share above entry
            if current_price >= entry_price + TRAIL_TRIGGER:
                pos["trail_active"] = True
                pos["stop"] = round(entry_price + TRAIL_STEP, 2)
                pos["trail_high"] = entry_price + TRAIL_TRIGGER
                logger.info("Trail activated for %s: stop raised to $%.2f",
                            ticker, pos["stop"])
        else:
            # Ratchet: for every $0.50 above trail_high, move stop up $0.50
            if current_price > pos["trail_high"] + TRAIL_STEP:
                steps = int((current_price - pos["trail_high"]) / TRAIL_STEP)
                pos["stop"] = round(pos["stop"] + steps * TRAIL_STEP, 2)
                pos["trail_high"] = round(
                    pos["trail_high"] + steps * TRAIL_STEP, 2)
                logger.info("Trail ratchet %s: stop=$%.2f trail_high=$%.2f",
                            ticker, pos["stop"], pos["trail_high"])

    # Close positions outside the loop to avoid mutation during iteration
    for ticker, price, reason in tickers_to_close:
        close_position(ticker, price, reason)


# ============================================================
# EOD CLOSE
# ============================================================
def eod_close():
    """Force-close all open positions at 15:55 ET."""
    if not positions:
        logger.info("EOD close: no open positions")
        return

    logger.info("EOD close: closing %d positions", len(positions))
    tickers_to_close = []

    for ticker in list(positions.keys()):
        bars = fetch_1min_bars(ticker)
        if bars:
            price = bars["current_price"]
        else:
            price = positions[ticker]["entry_price"]
        tickers_to_close.append((ticker, price))

    for ticker, price in tickers_to_close:
        close_position(ticker, price, reason="EOD")

    # Summary
    today_sells = [t for t in paper_trades if t.get("action") == "SELL"]
    total_pnl = sum(t.get("pnl", 0) for t in today_sells)
    wins = sum(1 for t in today_sells if t.get("pnl", 0) > 0)
    losses = sum(1 for t in today_sells if t.get("pnl", 0) <= 0)

    msg = (
        f"EOD CLOSE Complete\n"
        f"  Trades: {len(today_sells)}  W/L: {wins}/{losses}\n"
        f"  Day P&L: ${total_pnl:+.2f}\n"
        f"  Cash: ${paper_cash:,.2f}"
    )
    send_telegram(msg)
    save_paper_state()


# ============================================================
# SCAN LOOP
# ============================================================
def scan_loop():
    """Main scan: manage positions, check new entries. Runs every 60s."""
    now_et = datetime.now(ET)

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

    # Update AVWAP for index anchors
    update_avwap("SPY")
    update_avwap("QQQ")

    # Manage existing positions
    manage_positions()

    # Check for new entries on tradable tickers
    for ticker in TRADE_TICKERS:
        if ticker in positions:
            continue
        try:
            ok, bars = check_entry(ticker)
            if ok and bars:
                execute_entry(ticker, bars["current_price"])
        except Exception as e:
            logger.error("Entry check error %s: %s", ticker, e)


# ============================================================
# RESET DAILY STATE
# ============================================================
def reset_daily_state():
    """Reset AVWAP, OR data, and daily counts for new trading day."""
    global or_collected_date, daily_entry_date

    now_et = datetime.now(ET)
    today = now_et.strftime("%Y-%m-%d")

    if or_collected_date != today:
        or_high.clear()
        pdc.clear()
        or_collected_date = ""

    if daily_entry_date != today:
        daily_entry_count.clear()
        paper_trades.clear()
        daily_entry_date = today

    # Reset AVWAP
    for t in ("SPY", "QQQ"):
        avwap_data[t] = {"cum_pv": 0.0, "cum_vol": 0.0, "avwap": 0.0}
        avwap_last_ts[t] = 0


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
    last_scan = datetime.now(ET) - timedelta(seconds=SCAN_INTERVAL + 1)
    last_state_save = datetime.now(ET) - timedelta(minutes=6)

    # Job table: (day, "HH:MM", function)
    JOBS = [
        ("daily", "09:30", reset_daily_state),
        ("daily", "09:35",
         lambda: threading.Thread(target=collect_or, daemon=True).start()),
        ("daily", "15:55", eod_close),
    ]

    logger.info("Scheduler started — all times in ET (server: %s)",
                datetime.now().strftime("%Z %z") or "unknown")

    while True:
        now_et = datetime.now(ET)
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
# TELEGRAM COMMANDS
# ============================================================
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show available commands."""
    sep = "-" * 32
    text = (
        "Stock Spike Monitor v%s\n"
        "%s\n"
        "/help      — This menu\n"
        "/status    — Open positions + P&L\n"
        "/positions — Alias for /status\n"
        "/dayreport — Today's trades + P&L\n"
        "%s\n"
        "Strategy: ORB Momentum Breakout\n"
        "Universe: %s\n"
        "Scan: every %ds during market hours"
    ) % (BOT_VERSION, sep, sep,
         ", ".join(TRADE_TICKERS), SCAN_INTERVAL)
    await update.message.reply_text(text)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show open positions and today's P&L."""
    now_et = datetime.now(ET)
    sep = "-" * 32
    lines = [
        "STATUS — %s" % now_et.strftime("%I:%M %p ET"),
        sep,
    ]

    if not positions:
        lines.append("No open positions")
    else:
        for ticker, pos in positions.items():
            bars = fetch_1min_bars(ticker)
            cur = bars["current_price"] if bars else pos["entry_price"]
            pnl = (cur - pos["entry_price"]) * pos["shares"]
            trail_str = "ON" if pos["trail_active"] else "OFF"
            lines.append(
                "%s: entry=$%.2f cur=$%.2f pnl=$%+.2f stop=$%.2f trail=%s"
                % (ticker, pos["entry_price"], cur, pnl, pos["stop"], trail_str)
            )

    # Today's completed trades
    today_sells = [t for t in paper_trades if t.get("action") == "SELL"]
    if today_sells:
        total_pnl = sum(t.get("pnl", 0) for t in today_sells)
        lines.append(sep)
        lines.append("Today: %d trades, P&L=$%+.2f" % (len(today_sells), total_pnl))

    lines.append(sep)
    lines.append(f"Cash: ${paper_cash:,.2f}")

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

    await update.message.reply_text("\n".join(lines))


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias for /status."""
    await cmd_status(update, context)


async def cmd_dayreport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show today's completed trades with P&L summary."""
    now_et = datetime.now(ET)
    today = now_et.strftime("%Y-%m-%d")
    sep = "-" * 32

    today_sells = [t for t in paper_trades if t.get("action") == "SELL"]

    if not today_sells:
        await update.message.reply_text(
            "Day Report — %s\n%s\nNo completed trades today." % (today, sep))
        return

    lines = [
        "Day Report — %s" % today,
        sep,
    ]

    total_pnl = 0.0
    for t in today_sells:
        ticker = t.get("ticker", "?")
        pnl = t.get("pnl", 0)
        pnl_pct = t.get("pnl_pct", 0)
        reason = t.get("reason", "?")
        entry_p = t.get("entry_price", 0)
        exit_p = t.get("price", 0)
        total_pnl += pnl
        lines.append(
            "%s: $%.2f -> $%.2f  P&L=$%+.2f (%+.1f%%) [%s]"
            % (ticker, entry_p, exit_p, pnl, pnl_pct, reason)
        )

    wins = sum(1 for t in today_sells if t.get("pnl", 0) > 0)
    losses = len(today_sells) - wins
    win_rate = (wins / len(today_sells) * 100) if today_sells else 0

    lines.append(sep)
    lines.append("Total P&L: $%+.2f" % total_pnl)
    lines.append("Trades: %d  W/L: %d/%d  Win%%: %.0f%%" % (
        len(today_sells), wins, losses, win_rate))
    lines.append(f"Cash: ${paper_cash:,.2f}")

    await update.message.reply_text("\n".join(lines))


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show version info."""
    await update.message.reply_text(
        "Stock Spike Monitor v%s\n%s" % (BOT_VERSION, RELEASE_NOTE))


# ============================================================
# TELEGRAM BOT SETUP
# ============================================================
MAIN_BOT_COMMANDS = [
    BotCommand("help", "Command menu"),
    BotCommand("status", "Open positions + P&L"),
    BotCommand("positions", "Alias for /status"),
    BotCommand("dayreport", "Today's trades + P&L"),
    BotCommand("version", "Release notes"),
]

TP_BOT_COMMANDS = [
    BotCommand("help", "Command menu"),
    BotCommand("status", "Open positions + P&L"),
]


async def _set_bot_commands(app: Application) -> None:
    """Register / menu commands on startup."""
    try:
        await app.bot.set_my_commands(
            MAIN_BOT_COMMANDS,
            scope=BotCommandScopeAllPrivateChats(),
        )
        await app.bot.set_my_commands(
            MAIN_BOT_COMMANDS,
            scope=BotCommandScopeAllGroupChats(),
        )
        logger.info("Registered %d bot commands", len(MAIN_BOT_COMMANDS))
    except Exception as e:
        logger.warning("Failed to set bot commands: %s", e)


async def _set_tp_bot_commands(app: Application) -> None:
    """Register TP bot commands."""
    try:
        await app.bot.set_my_commands(
            TP_BOT_COMMANDS,
            scope=BotCommandScopeAllPrivateChats(),
        )
        await app.bot.set_my_commands(
            TP_BOT_COMMANDS,
            scope=BotCommandScopeAllGroupChats(),
        )
        logger.info("Registered %d TP bot commands", len(TP_BOT_COMMANDS))
    except Exception as e:
        logger.warning("Failed to set TP bot commands: %s", e)


def send_startup_message():
    """Send startup notification."""
    now_et = datetime.now(ET)
    weekday = now_et.weekday() < 5
    in_hours = (
        weekday
        and now_et.hour >= 9
        and (now_et.hour < 16 or (now_et.hour == 15 and now_et.minute < 55))
    )
    status = "OPEN" if in_hours else "CLOSED"

    sep = "-" * 32
    universe = ", ".join(TRADE_TICKERS)
    n_pos = len(positions)
    cash_fmt = f"{paper_cash:,.2f}"
    time_str = now_et.strftime("%Y-%m-%d %I:%M %p ET")
    msg = (
        f"Stock Spike Monitor v{BOT_VERSION}\n"
        f"{time_str}\n"
        f"{RELEASE_NOTE}\n"
        f"{sep}\n"
        f"Market: {status}\n"
        f"Universe: {universe}\n"
        f"Strategy: ORB Momentum Breakout\n"
        f"Scan: every {SCAN_INTERVAL}s\n"
        f"Positions: {n_pos} open\n"
        f"Cash: ${cash_fmt}\n"
        f"{sep}\n"
        f"/help for commands"
    )
    send_telegram(msg)


def run_telegram_bot():
    """Start main Telegram bot (and optional TP bot)."""
    app = (Application.builder()
           .token(TELEGRAM_TOKEN)
           .post_init(_set_bot_commands)
           .build())

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("dayreport", cmd_dayreport))
    app.add_handler(CommandHandler("version", cmd_version))

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
    tp_app.add_handler(CommandHandler("status", cmd_status))

    async def _run_both():
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        async with app:
            async with tp_app:
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
    now_et = datetime.now(ET)
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

# Startup catch-up
startup_catchup()

# Background threads
threading.Thread(target=scheduler_thread, daemon=True).start()
threading.Thread(target=health_ping, daemon=True).start()

logger.info("Stock Spike Monitor v%s started", BOT_VERSION)
send_startup_message()
run_telegram_bot()
