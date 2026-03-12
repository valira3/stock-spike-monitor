import yfinance as yf
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
import pytz
import logging
from collections import defaultdict, deque
import anthropic
from openai import OpenAI   # kept for Grok fallback only
import os
import threading
import json
import math
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch
from matplotlib.colors import LinearSegmentedColormap
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    MessageHandler, filters
)

# ============================================================
# CONFIG FROM ENVIRONMENT VARIABLES
# ============================================================
FINNHUB_TOKEN     = os.getenv("FINNHUB_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GROK_API_KEY      = os.getenv("GROK_API_KEY")        # fallback only
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
CHAT_ID           = os.getenv("CHAT_ID")
FMP_API_KEY       = os.getenv("FMP_API_KEY")

# FMP stable API endpoints (v3 is deprecated for newer accounts)
FMP_ENDPOINTS = {
    "actives": "https://financialmodelingprep.com/stable/most-actives",
    "gainers": "https://financialmodelingprep.com/stable/biggest-gainers",
    "losers":  "https://financialmodelingprep.com/stable/biggest-losers",
}

THRESHOLD           = 0.03
MIN_PRICE           = 5.0
COOLDOWN_MINUTES    = 5
CHECK_INTERVAL_MIN  = 1
VOLUME_SPIKE_MULT   = 2.0
LOG_FILE            = "stock_spike_monitor.log"

# ── Claude models ─────────────────────────────────────────────
# Sonnet  -> deep analysis, /ask, briefings, macro, compare
# Haiku   -> high-frequency: spike alerts, signal scores, dashboard one-liner
CLAUDE_SONNET = "claude-sonnet-4-5"
CLAUDE_HAIKU  = "claude-haiku-4-5-20251001"
GROK_MODEL    = "grok-4-1-fast-non-reasoning"   # fallback

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
CT = pytz.timezone('America/Chicago')

# ============================================================
# AI CLIENTS — Claude primary, Grok fallback
# ============================================================
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
grok_client   = OpenAI(api_key=GROK_API_KEY, base_url="https://api.x.ai/v1") if GROK_API_KEY else None

if claude_client:
    logger.info("AI: Claude (primary) initialised")
elif grok_client:
    logger.info("AI: Grok (fallback only — no ANTHROPIC_API_KEY set)")
else:
    logger.warning("AI: No AI client available — set ANTHROPIC_API_KEY in Railway")

# ============================================================
# FINNHUB RATE LIMITER + RESPONSE CACHE
# ============================================================
# Token-bucket rate limiter: 55 calls/min safety margin (API limit: 60/min)
# All Finnhub API calls MUST go through finnhub_rate_limit() before making
# the HTTP request.  The cache avoids duplicate calls for the same data.

class _FinnhubRateLimiter:
    """Thread-safe token-bucket rate limiter for Finnhub API."""
    def __init__(self, max_calls: int = 55, period: float = 60.0):
        self._max_calls = max_calls
        self._period = period
        self._lock = threading.Lock()
        self._tokens = float(max_calls)
        self._last_refill = time.monotonic()
        self._total_calls = 0
        self._limited_calls = 0

    def acquire(self, timeout: float = 30.0) -> bool:
        """Block until a token is available or timeout. Returns True if acquired."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    self._total_calls += 1
                    return True
            # No token — wait a bit and retry
            if time.monotonic() >= deadline:
                with self._lock:
                    self._limited_calls += 1
                logger.warning("Finnhub rate limiter: timeout waiting for token")
                return False
            time.sleep(0.5)

    def _refill(self):
        """Refill tokens based on elapsed time (must hold lock)."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        new_tokens = elapsed * (self._max_calls / self._period)
        self._tokens = min(self._max_calls, self._tokens + new_tokens)
        self._last_refill = now

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "total_calls": self._total_calls,
                "limited_calls": self._limited_calls,
                "tokens_available": round(self._tokens, 1),
            }

_finnhub_limiter = _FinnhubRateLimiter(max_calls=55, period=60.0)


class _TTLCache:
    """Thread-safe TTL cache for API responses."""
    def __init__(self, ttl_seconds: float = 45.0, max_size: int = 500):
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._lock = threading.Lock()
        self._store: dict = {}  # key -> (timestamp, value)
        self._hits = 0
        self._misses = 0

    def get(self, key: str):
        """Return cached value or None if missing/expired."""
        with self._lock:
            entry = self._store.get(key)
            if entry:
                ts, val = entry
                if time.monotonic() - ts < self._ttl:
                    self._hits += 1
                    return val
                del self._store[key]
            self._misses += 1
        return None

    def put(self, key: str, value):
        with self._lock:
            # Evict oldest entries if over max size
            if len(self._store) >= self._max_size:
                oldest_keys = sorted(self._store, key=lambda k: self._store[k][0])
                for k in oldest_keys[:self._max_size // 4]:
                    del self._store[k]
            self._store[key] = (time.monotonic(), value)

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "size": len(self._store),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": f"{self._hits / max(1, self._hits + self._misses) * 100:.0f}%",
            }

_quote_cache = _TTLCache(ttl_seconds=55.0, max_size=500)
_metrics_cache = _TTLCache(ttl_seconds=300.0, max_size=300)

# ============================================================
# BOT DESCRIPTION (used by /about and natural-language handler)
# ============================================================
BOT_DESCRIPTION = (
    "STOCK SPIKE MONITOR\n"
    "60+ stocks | 3% alerts | AI-driven\n"
    "\n"
    "MARKET\n"
    " /overview    indices+sectors+AI read\n"
    " /movers      gainers, losers, active\n"
    " /crypto      BTC ETH SOL DOGE XRP\n"
    " /macro       CPI, Fed, NFP, FOMC\n"
    " /earnings    next 7 days calendar\n"
    " /dashboard   visual market snapshot\n"
    "\n"
    "STOCKS\n"
    " /price TICK  live quote + range\n"
    " /chart TICK  intraday + volume\n"
    " /analyze T   AI catalyst/risk/setup\n"
    " /compare A B side-by-side AI\n"
    " /rsi TICK    RSI, BB, squeeze score\n"
    " /news TICK   latest headlines\n"
    "\n"
    "ALERTS\n"
    " /spikes      recent spike alerts\n"
    " /alerts      all alerts today\n"
    " /squeeze     top squeeze candidates\n"
    " /setalert T $  set price alert\n"
    " /myalerts    view active alerts\n"
    " /delalert T  remove alert(s)\n"
    " /watchlist   add|remove|show|scan\n"
    "\n"
    "PAPER TRADING  ($100k sim)\n"
    " /paper       portfolio overview\n"
    " /paper positions  live P&L\n"
    " /paper trades     today's trades\n"
    " /paper history    win rate stats\n"
    " /paper signal T   9-factor score\n"
    " /paper log   download trade log\n"
    " /paper reset start over at $100k\n"
    " /overnight   gap risk on holdings\n"
    "\n"
    "AI & TOOLS\n"
    " /aistocks    AI picks + conviction\n"
    " /ask <q>     chat with Claude\n"
    " /prep        next session plan\n"
    " /wlprep      watchlist deep scan\n"
    "\n"
    "BOT\n"
    " /list        monitored tickers\n"
    " /monitoring  pause|resume|status\n"
    " /help        this menu\n"
    "\n"
    "Auto: 7am AI | 8am dash | 8:30 open\n"
    " 10:30/12:30/2:30 AI | 3pm close\n"
    " 6pm recap | Sat 9am | Sun 6pm"
)

# ============================================================
# STATE
# ============================================================
CORE_TICKERS = [
    "NVDA","TSLA","AMD","AAPL","AMZN","META","MSFT","GOOGL","SMCI","ARM",
    "MU","AVGO","QCOM","INTC","HIMS","PLTR","SOFI","RIVN","NIO","MARA",
    "AMC","GME","LCID","BYND","PFE","BAC","JPM","XOM","CVX","AAL"
]
TICKERS             = CORE_TICKERS.copy()
monitoring_paused   = False
daily_alerts        = 0
last_prices         = {}
last_alert_time     = {}
price_history       = {t: deque(maxlen=60) for t in CORE_TICKERS}  # 60 ticks for RSI(14)
recent_alerts       = []
custom_price_alerts = {}   # {ticker: [target_prices]}
user_watchlists     = {}   # {chat_id: [tickers]}
conversation_history= {}   # {chat_id: [messages]} for multi-turn Q&A
squeeze_scores      = {}   # {ticker: score} updated each scan cycle
ai_watchlist_suggestions = {}  # {ticker: {"conviction": int, "thesis": str, "category": str, "added_at": str}}
ai_watchlist_last_refresh = ""  # e.g. "10:30 AM CT (intraday)"
daily_candles = {}  # {ticker: list of dicts [{date, open, high, low, close, volume}, ...]}  — ephemeral, NOT persisted

# ============================================================
# TELEGRAM: SAFE MULTI-PART SENDER WITH EXPONENTIAL BACKOFF
# ============================================================
def send_telegram(text, chat_id=None):
    cid = chat_id or CHAT_ID
    if not text or not text.strip():
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
        prefix  = f"{i}/{total} " if total > 1 else ""
        payload = {"chat_id": cid, "text": prefix + part}
        for attempt in range(5):
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json=payload, timeout=10
                )
                if r.status_code == 429:          # rate-limited
                    wait = int(r.json().get("parameters", {}).get("retry_after", 2 ** attempt))
                    logger.warning(f"Telegram 429 — sleeping {wait}s")
                    time.sleep(wait)
                    continue
                time.sleep(0.3)
                break
            except Exception as e:
                wait = 2 ** attempt
                logger.error(f"Telegram send error (attempt {attempt+1}): {e}. Retry in {wait}s")
                time.sleep(wait)

# ============================================================
# AI HELPERS — Claude primary, Grok fallback, exponential backoff
# ============================================================

def _build_system(today_stamp: str) -> str:
    return (
        f"You are a concise stock market analyst assistant. "
        f"Today is {today_stamp}. "
        f"STRICT RULES: "
        f"(1) Only state facts you are confident are true as of this date. "
        f"(2) Do NOT invent specific events, earnings dates, economic reports, "
        f"strikes, executive statements, or price levels — if uncertain, omit or say so. "
        f"(3) When live data is provided in the prompt, use it. "
        f"When it is not, give general analysis and clearly flag uncertainty. "
        f"(4) Never reference events, prices, or news from prior years unless asked. "
        f"Be direct and data-driven. No fluff. Max 3 sentences unless asked for more."
    )


def get_ai_response(prompt, system=None, max_tokens=300, fast=False):
    """
    Single-turn AI response.
    fast=True  -> Claude Haiku  (spike alerts, signals, dashboard — high frequency)
    fast=False -> Claude Sonnet (analysis, briefings, /ask — quality matters)
    Falls back to Grok if Claude is unavailable.
    """
    today_stamp = datetime.now(CT).strftime("%A %B %d, %Y  %I:%M %p CT")
    sys_msg     = system or _build_system(today_stamp)
    model       = CLAUDE_HAIKU if fast else CLAUDE_SONNET

    # ── Claude (primary) ──────────────────────────────────────
    if claude_client:
        for attempt in range(4):
            try:
                resp = claude_client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=sys_msg,
                    messages=[{"role": "user", "content": prompt}]
                )
                return resp.content[0].text.strip()
            except anthropic.RateLimitError:
                wait = 2 ** attempt
                logger.warning(f"Claude rate limit (attempt {attempt+1}), retry in {wait}s")
                time.sleep(wait)
            except anthropic.APIStatusError as e:
                logger.error(f"Claude API error (attempt {attempt+1}): {e.status_code} {e.message}")
                if e.status_code < 500:
                    break   # 4xx won't fix on retry
                time.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"Claude error (attempt {attempt+1}): {e}")
                time.sleep(2 ** attempt)
        logger.warning("Claude failed all retries — falling back to Grok")

    # ── Grok (fallback) ───────────────────────────────────────
    if grok_client:
        for attempt in range(3):
            try:
                resp = grok_client.chat.completions.create(
                    model=GROK_MODEL,
                    messages=[
                        {"role": "system", "content": sys_msg},
                        {"role": "user",   "content": prompt}
                    ],
                    max_tokens=max_tokens,
                    temperature=0.4
                )
                logger.info("Used Grok fallback")
                return resp.choices[0].message.content.strip()
            except Exception as e:
                logger.error(f"Grok fallback error (attempt {attempt+1}): {e}")
                time.sleep(2 ** attempt)

    return "AI unavailable — set ANTHROPIC_API_KEY in Railway"


def get_ai_conversation(chat_id: str, user_message: str) -> str:
    """
    Multi-turn conversational AI with per-chat memory (last 20 messages).
    Uses Claude Sonnet for quality. Falls back to Grok.
    """
    today_stamp = datetime.now(CT).strftime("%A %B %d %Y %I:%M %p CT")
    system = (
        f"You are a live stock market assistant on Telegram. "
        f"Today is {today_stamp}. "
        f"IMPORTANT: The user's message includes LIVE MARKET DATA, NEWS HEADLINES, and "
        f"LIVE PRICES fetched right now from Finnhub and other real-time sources. "
        f"This data is current as of this moment — use it to answer questions directly "
        f"and specifically. Do NOT say you lack real-time data or news access. "
        f"The data in the message IS the real-time data. "
        f"RULES: "
        f"(1) Always base your answer on the live data provided in the message. "
        f"(2) Do NOT fabricate earnings dates, analyst targets, or executive statements. "
        f"(3) If a specific fact is not in the provided data, say so briefly then reason "
        f"from what IS provided. "
        f"(4) Be concise and direct. No markdown, plain text only."
    )

    history = conversation_history.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_message})
    if len(history) > 20:
        conversation_history[chat_id] = history[-20:]
        history = conversation_history[chat_id]
    # Cap total conversations to prevent unbounded memory growth
    if len(conversation_history) > 50:
        oldest_keys = sorted(conversation_history.keys())[:-50]
        for k in oldest_keys:
            del conversation_history[k]

    # ── Claude (primary) ──────────────────────────────────────
    if claude_client:
        try:
            resp = claude_client.messages.create(
                model=CLAUDE_SONNET,
                max_tokens=500,
                system=system,
                messages=history
            )
            reply = resp.content[0].text.strip()
            history.append({"role": "assistant", "content": reply})
            return reply
        except anthropic.RateLimitError:
            logger.warning("Claude rate limit in conversation — trying Grok fallback")
        except Exception as e:
            logger.error(f"Claude conversation error: {e} — trying Grok fallback")

    # ── Grok (fallback) ───────────────────────────────────────
    if grok_client:
        try:
            resp = grok_client.chat.completions.create(
                model=GROK_MODEL,
                messages=[{"role": "system", "content": system}] + history,
                max_tokens=500,
                temperature=0.5
            )
            reply = resp.choices[0].message.content.strip()
            history.append({"role": "assistant", "content": reply})
            logger.info("Used Grok fallback for conversation")
            return reply
        except Exception as e:
            logger.error(f"Grok conversation fallback error: {e}")

    return "AI unavailable — set ANTHROPIC_API_KEY in Railway"

# ============================================================
# MARKET DATA HELPERS
# ============================================================
def fetch_finnhub_quote(ticker):
    """Legacy wrapper — returns (current, volume, prev_close) tuple.
    Now uses the rate-limited + cached _finnhub_quote() internally."""
    q = _finnhub_quote(ticker)
    if q:
        return q.get('c'), q.get('v'), q.get('pc')
    return None, None, None

def fetch_latest_news(ticker, count=3):
    try:
        today     = datetime.now().date()
        yesterday = today - timedelta(days=2)
        r = requests.get(
            f"https://finnhub.io/api/v1/company-news?symbol={ticker}"
            f"&from={yesterday}&to={today}&token={FINNHUB_TOKEN}",
            timeout=10
        )
        news = r.json()[:count]
        return [(item.get('headline',''), item.get('url','')) for item in news]
    except Exception as e:
        logger.debug(f"fetch_latest_news {ticker}: {e}")
        return []

def get_trading_session():
    now     = datetime.now(CT)
    if now.weekday() > 4:
        return "closed"
    current = now.time()
    if datetime.strptime("07:00", "%H:%M").time() <= current < datetime.strptime("20:00", "%H:%M").time():
        return "regular" if datetime.strptime("08:30", "%H:%M").time() <= current < datetime.strptime("15:00", "%H:%M").time() else "extended"
    return "closed"

def get_yf_info(ticker):
    try:
        return yf.Ticker(ticker).fast_info
    except Exception as e:
        logger.debug(f"get_yf_info {ticker}: {e}")
        return None

def _finnhub_quote(ticker: str) -> dict:
    """
    Rate-limited + cached Finnhub quote. Returns dict with keys:
      c (current), pc (prev close), h (day high), l (day low), v (volume)
    NOTE: During off-hours Finnhub sets c=0 but pc still holds the last close price.
          We return the dict as long as pc > 0 so off-hours callers can use pc.
    Returns {} on failure.
    """
    # Check cache first
    cached = _quote_cache.get(f"quote:{ticker}")
    if cached is not None:
        return cached
    # Rate limit
    if not _finnhub_limiter.acquire(timeout=5):
        logger.warning(f"Finnhub rate limit: skipping quote for {ticker}")
        return {}
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_TOKEN}",
            timeout=8
        )
        if r.status_code == 429:
            logger.warning(f"Finnhub 429 on quote {ticker}")
            return {}
        d = r.json()
        # Accept quote if current price OR previous close is valid
        if d.get("c", 0) > 0 or d.get("pc", 0) > 0:
            _quote_cache.put(f"quote:{ticker}", d)
            return d
    except Exception as e:
        logger.debug(f"Finnhub quote {ticker}: {e}")
    return {}


def _finnhub_metrics(ticker: str) -> dict:
    """
    Rate-limited + cached Finnhub fundamental metrics — 52w high/low, market cap, avg volume.
    Returns {} on failure.
    """
    cached = _metrics_cache.get(f"metrics:{ticker}")
    if cached is not None:
        return cached
    if not _finnhub_limiter.acquire(timeout=5):
        logger.warning(f"Finnhub rate limit: skipping metrics for {ticker}")
        return {}
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/stock/metric?symbol={ticker}&metric=all&token={FINNHUB_TOKEN}",
            timeout=8
        )
        if r.status_code == 429:
            logger.warning(f"Finnhub 429 on metrics {ticker}")
            return {}
        result = r.json().get("metric", {})
        if result:
            _metrics_cache.put(f"metrics:{ticker}", result)
        return result
    except Exception as e:
        logger.debug(f"Finnhub metrics {ticker}: {e}")
    return {}


def _finnhub_candles(ticker: str, resolution: str = "5", count: int = 300) -> list:
    """
    Rate-limited Finnhub OHLCV candles.
    resolution: "1","5","15","30","60","D","W","M"
    Returns list of dicts: [{t, o, h, l, c, v}, ...] sorted oldest->newest.
    Returns [] on failure.
    """
    if not _finnhub_limiter.acquire(timeout=5):
        logger.warning(f"Finnhub rate limit: skipping candles for {ticker}")
        return []
    try:
        now_ts   = int(time.time())
        # Go back far enough to get `count` candles
        mins_map = {"1":1,"5":5,"15":15,"30":30,"60":60,"D":1440,"W":10080,"M":43200}
        lookback = mins_map.get(resolution, 5) * count * 60
        from_ts  = now_ts - lookback
        r = requests.get(
            f"https://finnhub.io/api/v1/stock/candle"
            f"?symbol={ticker}&resolution={resolution}"
            f"&from={from_ts}&to={now_ts}&token={FINNHUB_TOKEN}",
            timeout=10
        )
        if r.status_code == 429:
            logger.warning(f"Finnhub 429 on candles {ticker}")
            return []
        d = r.json()
        if d.get("s") != "ok":
            return []
        closes  = d.get("c", [])
        opens   = d.get("o", [])
        highs   = d.get("h", [])
        lows    = d.get("l", [])
        vols    = d.get("v", [])
        stamps  = d.get("t", [])
        return [
            {"t": stamps[i], "o": opens[i], "h": highs[i],
             "l": lows[i],   "c": closes[i], "v": vols[i]}
            for i in range(len(closes))
        ]
    except Exception as e:
        logger.debug(f"Finnhub candles {ticker} {resolution}: {e}")
    return []


def get_ticker_data(ticker: str) -> dict:
    """
    Robust ticker data with Finnhub as primary source, yfinance as fallback.

    Returns normalised dict:
      price, chg, mcap, volume, high52, low52, avg_volume,
      day_high, day_low, prev_close
    """
    d = {k: 0 for k in ("price","chg","mcap","volume","high52","low52",
                         "avg_volume","day_high","day_low","prev_close")}

    # ── Tier 1: Finnhub quote (primary — works on Railway) ────
    q = _finnhub_quote(ticker)
    if q:
        c  = q.get("c") or 0
        pc = q.get("pc") or 0
        # Off-hours: c==0 but pc has last close — use pc as price
        d["price"]      = c if c > 0 else pc
        d["prev_close"] = pc
        d["day_high"]   = q.get("h") or 0
        d["day_low"]    = q.get("l") or 0
        d["volume"]     = q.get("v") or 0
        if c > 0 and pc > 0:
            d["chg"] = (c - pc) / pc * 100

    # ── Tier 2: Finnhub metrics (52w range, mcap) ─────────────
    m = _finnhub_metrics(ticker)
    if m:
        d["high52"]     = m.get("52WeekHigh") or 0
        d["low52"]      = m.get("52WeekLow")  or 0
        d["avg_volume"] = (m.get("10DayAverageTradingVolume") or 0) * 1_000_000
        mcap_m          = m.get("marketCapitalization") or 0   # in millions
        d["mcap"]       = mcap_m * 1_000_000

    # ── Tier 3: yfinance fallback (if Finnhub returned nothing) ──
    if not d["price"]:
        try:
            t  = yf.Ticker(ticker)
            fi = t.fast_info
            d["price"]  = fi.get("lastPrice")    or fi.get("previousClose") or 0
            d["volume"] = fi.get("lastVolume")   or 0
            d["mcap"]   = fi.get("marketCap")    or 0
            d["high52"] = fi.get("yearHigh")     or 0
            d["low52"]  = fi.get("yearLow")      or 0
            pc          = fi.get("regularMarketPreviousClose") or fi.get("previousClose") or 0
            if d["price"] and pc:
                d["chg"] = (d["price"] - pc) / pc * 100
        except Exception as e:
            logger.debug(f"yfinance fallback {ticker}: {e}")

    return d



# ============================================================
# TECHNICALS — RSI (Wilder), Bollinger Bands, Squeeze Score
# ============================================================

def compute_rsi(prices: list, period: int = 14):
    """
    Wilder's Smoothed RSI from a list of closing prices.
    Returns None if insufficient data.
    """
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def compute_bollinger(prices: list, period: int = 20, num_std: float = 2.0):
    """
    Returns (middle, upper, lower, pct_b, bandwidth).
    pct_b = (price - lower) / (upper - lower)  — 0=at lower, 1=at upper
    bandwidth = (upper - lower) / middle        — squeeze proxy (lower=tighter)
    """
    if len(prices) < period:
        return None, None, None, None, None
    window = prices[-period:]
    mid    = sum(window) / period
    var    = sum((p - mid) ** 2 for p in window) / period
    std    = math.sqrt(var)
    upper  = mid + num_std * std
    lower  = mid - num_std * std
    price  = prices[-1]
    pct_b  = (price - lower) / (upper - lower) if (upper - lower) != 0 else 0.5
    bw     = (upper - lower) / mid if mid != 0 else 0
    return round(mid, 2), round(upper, 2), round(lower, 2), round(pct_b, 3), round(bw, 4)


def compute_squeeze_score(ticker: str) -> dict:
    """
    Squeeze score 0-100 combining:
      RSI distance from 40 (building momentum)  — up to 30 pts
      Bollinger bandwidth squeeze (low = tight)  — up to 25 pts
      %B near lower band (coiled spring)         — up to 20 pts
      Volume trend (rising vs prior scans)       — up to 15 pts
      Short interest ratio (Finnhub)             — up to 10 pts
    Higher score = more squeeze-ready.
    """
    hist_raw = list(price_history.get(ticker, deque()))
    if not hist_raw:
        return {"score": 0, "rsi": None, "pct_b": None, "bandwidth": None, "components": {}}

    # price_history stores (datetime, price) tuples
    prices = [p for _, p in hist_raw] if isinstance(hist_raw[0], tuple) else hist_raw

    rsi            = compute_rsi(prices)
    _, _, _, pct_b, bw = compute_bollinger(prices)

    score      = 0
    components = {}

    if rsi is not None:
        rsi_pts = max(0, 30 - abs(rsi - 40))
        score  += rsi_pts
        components['rsi']     = round(rsi, 1)
        components['rsi_pts'] = round(rsi_pts, 1)

    if bw is not None:
        bw_pts = max(0, 25 * (1 - bw / 0.1)) if bw < 0.1 else 0
        score += bw_pts
        components['bandwidth'] = bw
        components['bw_pts']    = round(bw_pts, 1)

    if pct_b is not None:
        pb_pts = max(0, 20 * (1 - pct_b)) if pct_b < 0.5 else 0
        score += pb_pts
        components['pct_b']  = pct_b
        components['pb_pts'] = round(pb_pts, 1)

    if len(prices) >= 10:
        recent_avg = sum(prices[-5:]) / 5
        prior_avg  = sum(prices[-10:-5]) / 5
        if prior_avg > 0:
            vol_trend = (recent_avg - prior_avg) / prior_avg
            vt_pts    = min(15, max(0, vol_trend * 100))
            score    += vt_pts
            components['price_trend_pct'] = round(vol_trend * 100, 1)
            components['vt_pts']          = round(vt_pts, 1)

    # Use cached metrics only for short interest — don't burn an API call for 10 pts
    cached_metrics = _metrics_cache.get(f"metrics:{ticker}")
    if cached_metrics is not None:
        si     = (cached_metrics.get('shortRatioAnnual') or 0)
        si_pts = min(10, si / 2)
        score += si_pts
        components['short_ratio'] = si
        components['si_pts']      = round(si_pts, 1)

    return {
        "score":     round(min(score, 100), 1),
        "rsi":       components.get('rsi'),
        "pct_b":     components.get('pct_b'),
        "bandwidth": components.get('bandwidth'),
        "components": components,
    }


def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        d = r.json()['data'][0]
        return d.get('value'), d.get('value_classification')
    except Exception as e:
        logger.debug(f"get_fear_greed: {e}")
        return None, None

def get_sector_performance():
    sectors = {
        "XLK": "Technology", "XLF": "Financials", "XLE": "Energy",
        "XLV": "Health Care", "XLI": "Industrials", "XLC": "Comm Services",
        "XLY": "Cons Discret", "XLP": "Cons Staples", "XLB": "Materials",
        "XLRE": "Real Estate", "XLU": "Utilities"
    }
    lines = []
    for sym, name in sectors.items():
        try:
            q = _finnhub_quote(sym)
            if q:
                c  = q.get("c") or 0
                pc = q.get("pc") or 0
                chg = (c - pc) / pc * 100 if c > 0 and pc > 0 else 0
            else:
                fi  = yf.Ticker(sym).fast_info
                price = fi.get("lastPrice") or 0
                pc    = fi.get("regularMarketPreviousClose") or fi.get("previousClose") or 0
                chg   = (price - pc) / pc * 100 if price and pc else 0
            sign = "+" if chg >= 0 else ""
            lines.append(f"{sign}{chg:.2f}% {name}")
        except Exception as e:
            logger.debug(f"Sector perf {sym}: {e}")
    return lines

def _finnhub_crypto_candle(fsym: str) -> dict:
    """DEPRECATED: Use _finnhub_quote() with crypto symbols instead. Kept for reference."""
    if not _finnhub_limiter.acquire(timeout=5):
        return {}
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/crypto/candle?symbol={fsym}"
            f"&resolution=D&count=2&token={FINNHUB_TOKEN}",
            timeout=8
        )
        if r.status_code == 429:
            logger.warning(f"Finnhub 429 on crypto candle {fsym}")
            return {}
        return r.json()
    except Exception as e:
        logger.debug(f"Finnhub crypto candle {fsym}: {e}")
    return {}


def get_crypto_prices():
    coins = [
        ("BINANCE:BTCUSDT", "BTC"), ("BINANCE:ETHUSDT", "ETH"),
        ("BINANCE:SOLUSDT", "SOL"), ("BINANCE:DOGEUSDT", "DOGE"),
        ("BINANCE:XRPUSDT", "XRP"),
    ]
    lines = []
    for fsym, name in coins:
        try:
            q = _finnhub_quote(fsym)
            if q:
                price = q.get("c") or 0
                pc = q.get("pc") or 0
                if price > 0 and pc > 0:
                    chg = (price - pc) / pc * 100
                    sign = "+" if chg >= 0 else ""
                    if price >= 1000:
                        p_fmt = f"${price:,.0f}"
                    elif price >= 1:
                        p_fmt = f"${price:,.2f}"
                    else:
                        p_fmt = f"${price:.4f}"
                    lines.append(f"{name}: {p_fmt} ({sign}{chg:.2f}%)")
        except Exception as e:
            logger.debug(f"Crypto price {name}: {e}")
    return lines


def fetch_market_snapshot() -> dict:
    """
    Live market snapshot using Finnhub as primary data source.
    ETF proxies used for indices (Finnhub handles ETFs reliably):
      SPY -> S&P 500 | QQQ -> Nasdaq | DIA -> Dow | IWM -> Russell

    Returns dict with display-ready strings and Claude prompt strings.
    """
    INDEX_MAP = [
        ("SPY",  "S&P 500"),
        ("QQQ",  "Nasdaq"),
        ("DIA",  "Dow"),
        ("IWM",  "Russell 2K"),
        ("VXX",  "VIX ETF"),
    ]
    SECTOR_MAP = [
        ("XLK","Tech"), ("XLF","Fin"), ("XLE","Energy"),
        ("XLV","Health"), ("XLI","Indust"), ("XLC","Comm"),
        ("XLY","Cons D"), ("XLP","Cons S"), ("XLB","Mat"),
        ("XLRE","RE"), ("XLU","Util"),
    ]
    FUTURES_MAP = [
        # Finnhub free tier doesn't serve futures symbols (ES=F etc.)
        # Use liquid ETFs as pre-market proxies instead
        ("SPY",  "S&P ETF"),
        ("QQQ",  "Ndaq ETF"),
        ("DIA",  "Dow ETF"),
    ]

    def _q(sym):
        """Finnhub quote -> (price, chg_pct). Off-hours uses prev close as price."""
        q = _finnhub_quote(sym)
        if q:
            c  = q.get("c") or 0
            pc = q.get("pc") or 0
            price = c if c > 0 else pc   # off-hours: c==0, use pc
            chg   = (c - pc) / pc * 100 if c > 0 and pc > 0 else 0
            if price:
                return price, chg
        try:
            fi    = yf.Ticker(sym).fast_info
            price = fi.get("lastPrice") or fi.get("previousClose") or 0
            pc    = fi.get("regularMarketPreviousClose") or fi.get("previousClose") or 0
            chg   = (price - pc) / pc * 100 if price and pc else 0
            return price, chg
        except Exception as e:
            logger.debug(f"fetch_market_snapshot _q yfinance fallback {sym}: {e}")
            return 0, 0

    def _fmt(price, chg, name, is_index=True):
        sign = "+" if chg >= 0 else ""
        if is_index:
            return f"  {name}: ${price:,.2f} ({sign}{chg:.2f}%)"
        return f"  {name}: {sign}{chg:.2f}%"

    def _fetch_indices():
        lines, summary, vix = [], [], 0.0
        for sym, name in INDEX_MAP:
            price, chg = _q(sym)
            if not price:
                continue
            sign = "+" if chg >= 0 else ""
            if "VIX" in name:
                vix = price
                lines.append(f"  {name}: ${price:.2f} ({sign}{chg:.2f}%)")
            else:
                lines.append(f"  {name}: ${price:,.2f} ({sign}{chg:.2f}%)")
                summary.append(f"{name} {sign}{chg:.2f}%")
        return lines, " | ".join(summary) or "indices unavailable", vix

    def _fetch_sectors():
        rows = []
        for sym, name in SECTOR_MAP:
            price, chg = _q(sym)
            if not price:
                continue
            rows.append((name, chg))
        rows.sort(key=lambda x: x[1], reverse=True)
        lines = [f"  {n}: {'+'if c>=0 else ''}{c:.2f}%" for n, c in rows]
        top3  = ", ".join(f"{n} {'+'if c>=0 else ''}{c:.2f}%" for n, c in rows[:3])
        bot3  = ", ".join(f"{n} {'+'if c>=0 else ''}{c:.2f}%" for n, c in rows[-3:])
        return lines, (f"Top: {top3} | Weak: {bot3}" if rows else "sectors unavailable")

    def _fetch_futures():
        lines = []
        for sym, name in FUTURES_MAP:
            price, chg = _q(sym)
            if not price:
                continue
            sign = "+" if chg >= 0 else ""
            lines.append(f"  {name}: {price:,.0f} ({sign}{chg:.2f}%)")
        return lines

    def _fetch_crypto():
        coins = [
            ("BINANCE:BTCUSDT", "BTC"), ("BINANCE:ETHUSDT", "ETH"),
            ("BINANCE:SOLUSDT", "SOL"),
        ]
        lines = []
        for fsym, name in coins:
            try:
                q = _finnhub_quote(fsym)
                if q:
                    price = q.get("c") or 0
                    pc = q.get("pc") or 0
                    if price > 0 and pc > 0:
                        chg = (price - pc) / pc * 100
                        sign = "+" if chg >= 0 else ""
                        p_fmt = f"${price:,.0f}" if price >= 1000 else f"${price:,.4f}"
                        lines.append(f"  {name}: {p_fmt} ({sign}{chg:.2f}%)")
            except Exception as e:
                logger.debug(f"Snapshot crypto {name}: {e}")
        return lines

    def _fetch_movers():
        tickers_to_scan = list(TICKERS)  # scan full watchlist
        items = []
        def _q_one(t):
            q = _finnhub_quote(t)
            if not q:
                return None
            c  = q.get("c") or 0
            pc = q.get("pc") or 0
            price = c if c > 0 else pc
            if not price:
                return None
            chg = (c - pc) / pc * 100 if c > 0 and pc > 0 else 0
            return (t, chg, price)
        with ThreadPoolExecutor(max_workers=5) as p:
            results = list(p.map(_q_one, tickers_to_scan[:40]))
        items = [r for r in results if r]
        if not items:
            return "movers unavailable"
        items.sort(key=lambda x: x[1])
        gainers = items[-3:][::-1]
        losers  = items[:3]
        g = " ".join(f"{t} {'+'if c>=0 else ''}{c:.2f}%" for t, c, _ in gainers)
        l = " ".join(f"{t} {c:.2f}%" for t, c, _ in losers)
        return f"Gainers: {g} | Losers: {l}"

    with ThreadPoolExecutor(max_workers=6) as pool:
        f_idx = pool.submit(_fetch_indices)
        f_sec = pool.submit(_fetch_sectors)
        f_fut = pool.submit(_fetch_futures)
        f_cry = pool.submit(_fetch_crypto)
        f_mov = pool.submit(_fetch_movers)
        f_fg  = pool.submit(get_fear_greed)

    idx_lines, idx_str, vix = f_idx.result()
    sec_lines, sec_str      = f_sec.result()
    futures_lines           = f_fut.result()
    cry_lines               = f_cry.result()
    movers_str              = f_mov.result()
    fg_val, fg_label        = f_fg.result()
    fg_val   = int(fg_val) if fg_val else 50
    fg_label = fg_label or "Unknown"

    return {
        "indices_lines":  idx_lines,
        "indices_str":    idx_str,
        "sector_lines":   sec_lines,
        "sector_str":     sec_str,
        "futures_lines":  futures_lines,
        "futures_str":    " | ".join(futures_lines) or "futures unavailable",
        "fg_val":         fg_val,
        "fg_label":       fg_label,
        "fg_str":         f"{fg_val} - {fg_label}",
        "vix":            vix,
        "crypto_lines":   cry_lines,
        "crypto_str":     " | ".join(cry_lines),
        "movers_str":     movers_str,
        "now_label":      datetime.now(CT).strftime("%A %B %d, %Y"),
        "session":        get_trading_session(),
    }


def get_dynamic_hot_stocks():
    """
    Build the live scan watchlist from multiple real-time signals:
      1. FMP most-active, gainers, losers (broad market universe)
      2. Recent spike alerts fired by the bot
      3. Top squeeze-score candidates from last scan cycle
      4. CORE_TICKERS anchor (always included)
    Then verify each candidate via Finnhub live quote and keep only
    liquid stocks (price >= $1, volume > 50k).  No arbitrary mcap wall.
    Returns up to 80 symbols, deduplicated.
    """
    def _fq_simple(sym):
        q = _finnhub_quote(sym)
        if q:
            c  = q.get("c") or 0
            pc = q.get("pc") or 0
            price = c if c > 0 else pc
            chg   = (c - pc) / pc * 100 if c > 0 and pc > 0 else 0
            return price, chg
        return 0, 0

    logger.info("Fetching dynamic watchlist candidates...")
    candidates = list(CORE_TICKERS)  # always anchor with core 30

    # ── Pool 1: FMP market data (actives + gainers + losers) ──────
    for name, url in FMP_ENDPOINTS.items():
        if not FMP_API_KEY:
            break
        try:
            r = requests.get(f"{url}?apikey={FMP_API_KEY}", timeout=10)
            data = r.json()
            if isinstance(data, list):
                candidates.extend(
                    item.get("symbol") for item in data[:50]
                    if isinstance(item, dict) and item.get("symbol")
                )
        except Exception as e:
            logger.debug(f"FMP {name}: {e}")

    # ── Pool 2: Recent spike alerts (tickers that already moved) ──
    for alert_str in list(recent_alerts)[-50:]:
        sym = alert_str.split()[0]
        if sym:
            candidates.append(sym)

    # ── Pool 3: Top squeeze candidates from last scan ─────────────
    if squeeze_scores:
        top_squeeze = sorted(squeeze_scores, key=squeeze_scores.get, reverse=True)[:20]
        candidates.extend(top_squeeze)

    # ── Deduplicate preserving order ──────────────────────────────
    seen = set()
    unique = []
    for s in candidates:
        if s and s not in seen:
            seen.add(s)
            unique.append(s)

    # ── Live-verify via Finnhub: require price >= $0.50 ──────────
    verified = []
    def _verify(sym):
        try:
            q = _finnhub_quote(sym)
            if not q:
                return None
            c  = q.get("c") or 0
            pc = q.get("pc") or 0
            price = c if c > 0 else pc   # off-hours: c==0, use prev close
            if price >= 0.50:
                return sym
        except Exception as e:
            logger.debug(f"Ticker verify {sym}: {e}")
        return None

    # Run verification concurrently (cap at 60 candidates to stay within rate limits)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_verify, unique[:60]))

    verified = [s for s in results if s]

    # Always guarantee CORE_TICKERS are included even if all external calls fail
    combined = list(dict.fromkeys(verified + CORE_TICKERS))

    logger.info(f"Watchlist updated -> {len(combined)} stocks "
                f"(from {len(unique)} candidates)")
    return combined[:80]


def _merge_dynamic_stocks():
    """Refresh dynamic stocks without losing AI picks or paper positions."""
    global TICKERS
    dynamic = get_dynamic_hot_stocks()
    # Start with core + paper positions + AI picks
    merged = list(CORE_TICKERS)
    for t in paper_positions:
        if t not in merged:
            merged.append(t)
    # Add AI picks sorted by conviction
    if ai_watchlist_suggestions:
        ai_sorted = sorted(ai_watchlist_suggestions.keys(),
                          key=lambda t: ai_watchlist_suggestions[t].get("conviction", 0),
                          reverse=True)
        for t in ai_sorted:
            if t not in merged and len(merged) < 80:
                merged.append(t)
    # Fill with dynamic/FMP stocks
    for t in dynamic:
        if t not in merged and len(merged) < 80:
            merged.append(t)
    TICKERS = merged
    for t in TICKERS:
        if t not in price_history:
            price_history[t] = deque(maxlen=60)
    save_bot_state()


def ai_refresh_watchlist(mode="premarket"):
    """
    AI-driven watchlist rotation.
    mode="premarket" — 7:00 AM CT, uses Claude Sonnet, suggests 10-15 tickers.
    mode="intraday"  — 10:30/12:30/14:30 CT, uses Claude Haiku, suggests 3-5 tickers.
    Falls back to get_dynamic_hot_stocks() on failure.
    """
    global TICKERS, ai_watchlist_suggestions, ai_watchlist_last_refresh

    now_ct = datetime.now(CT)
    is_premarket = (mode == "premarket")
    logger.info(f"AI watchlist refresh starting — mode={mode}")

    # ── 1. Gather context ──────────────────────────────────────
    context_parts = []

    # Core tickers
    context_parts.append(f"Core tickers (always kept): {', '.join(CORE_TICKERS)}")

    # FMP market data — yesterday's movers
    fmp_labels = {"actives": "Most Active", "gainers": "Top Gainers", "losers": "Top Losers"}
    for name, url in FMP_ENDPOINTS.items():
        if not FMP_API_KEY:
            break
        try:
            r = requests.get(f"{url}?apikey={FMP_API_KEY}", timeout=10)
            data = r.json()
            if isinstance(data, list):
                items = []
                for item in data[:15]:
                    if isinstance(item, dict) and item.get("symbol"):
                        sym = item["symbol"]
                        chg = item.get("changesPercentage", 0)
                        items.append(f"{sym} ({chg:+.1f}%)" if chg else sym)
                if items:
                    context_parts.append(f"{fmp_labels[name]}: {', '.join(items)}")
        except Exception as e:
            logger.debug(f"AI watchlist FMP {name}: {e}")

    # Top squeeze scores
    if squeeze_scores:
        top_sq = sorted(squeeze_scores, key=squeeze_scores.get, reverse=True)[:20]
        sq_strs = [f"{t}({squeeze_scores[t]:.0f})" for t in top_sq]
        context_parts.append(f"Top squeeze scores: {', '.join(sq_strs)}")

    # Current paper positions
    if paper_positions:
        pos_strs = list(paper_positions.keys())
        context_parts.append(f"Current paper positions (don't suggest these): {', '.join(pos_strs)}")

    # Recent alerts (last 24h)
    if recent_alerts:
        recent = list(recent_alerts)[-30:]
        context_parts.append(f"Recent alerts (last 24h): {'; '.join(recent[-10:])}")

    # Current AI suggestions (for intraday context)
    if ai_watchlist_suggestions and mode == "intraday":
        current_ai = [f"{t}({ai_watchlist_suggestions[t]['conviction']}/10)"
                      for t in list(ai_watchlist_suggestions.keys())[:15]]
        context_parts.append(f"Current AI picks: {', '.join(current_ai)}")

    context_str = "\n".join(context_parts)

    # ── 2. Build prompt and ask AI ──────────────────────────────
    if is_premarket:
        count_hint = "10-15"
        prompt = (
            "You are a stock scanner assistant. Based on the current market context below, "
            f"suggest {count_hint} tickers to ADD to today's watchlist beyond the core 30. Focus on:\n"
            "- Stocks with upcoming catalysts (earnings, FDA, product launches this week)\n"
            "- Sector sympathy plays based on yesterday's movers\n"
            "- Momentum setups showing volume/price breakout patterns\n"
            "- Small/mid-cap names that institutional flows suggest are building positions\n\n"
            "For each ticker, provide:\n"
            "- Symbol\n"
            "- Conviction (1-10): how confident you are this will move today\n"
            "- Thesis: one sentence on why\n"
            "- Category: one of [earnings_catalyst, sympathy_play, momentum, sector_rotation, breakout, news_driven]\n\n"
            f"Context:\n{context_str}\n\n"
            'Respond ONLY with valid JSON: [{"symbol": "TICKER", "conviction": 8, "thesis": "...", "category": "..."}]'
        )
    else:
        count_hint = "3-5"
        prompt = (
            "You are a stock scanner assistant. Based on current market activity, "
            f"suggest {count_hint} NEW tickers to add to the intraday watchlist. Focus on:\n"
            "- What's moving RIGHT NOW with unusual volume\n"
            "- Sector rotation plays happening today\n"
            "- Breaking news catalysts\n"
            "Also suggest up to 3 tickers to DROP (not in core list, not moving) to free API budget.\n\n"
            "For each ADD, provide: symbol, conviction (1-10), thesis, category.\n"
            "For each DROP, provide: symbol, reason.\n\n"
            f"Context:\n{context_str}\n\n"
            'Respond ONLY with valid JSON: {"add": [{"symbol": "TICKER", "conviction": 8, "thesis": "...", "category": "..."}], '
            '"drop": [{"symbol": "TICKER", "reason": "..."}]}'
        )

    # Use Sonnet for premarket (quality), Haiku for intraday (speed)
    try:
        raw = get_ai_response(prompt, max_tokens=1500, fast=not is_premarket)
    except Exception as e:
        logger.error(f"AI watchlist call failed: {e}")
        raw = None

    # ── 3. Parse response ───────────────────────────────────────
    added_tickers = []
    dropped_tickers = []

    if raw:
        # Try JSON parsing first
        try:
            # Strip markdown code fences if present
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = "\n".join(cleaned.split("\n")[1:])
            if cleaned.endswith("```"):
                cleaned = cleaned.rsplit("```", 1)[0]
            cleaned = cleaned.strip()

            parsed = json.loads(cleaned)

            # Handle both response formats
            if isinstance(parsed, list):
                suggestions = parsed
                drops = []
            elif isinstance(parsed, dict):
                suggestions = parsed.get("add", [])
                drops = parsed.get("drop", [])
            else:
                suggestions = []
                drops = []

            for item in suggestions:
                if not isinstance(item, dict):
                    continue
                sym = str(item.get("symbol", "")).upper().strip()
                if not sym or len(sym) > 5:
                    continue
                conviction = int(item.get("conviction", 5))
                conviction = max(1, min(10, conviction))
                thesis = str(item.get("thesis", ""))[:120]
                category = str(item.get("category", "momentum"))

                ai_watchlist_suggestions[sym] = {
                    "conviction": conviction,
                    "thesis": thesis,
                    "category": category,
                    "added_at": now_ct.isoformat(),
                }
                added_tickers.append(sym)

            for item in drops:
                if isinstance(item, dict):
                    sym = str(item.get("symbol", "")).upper().strip()
                    if sym and sym not in CORE_TICKERS and sym not in paper_positions:
                        dropped_tickers.append(sym)

        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning(f"AI watchlist JSON parse failed, trying regex: {e}")
            # Fallback: extract ticker-like symbols from response
            import re
            matches = re.findall(r'"symbol"\s*:\s*"([A-Z]{1,5})"', raw)
            for sym in matches[:15 if is_premarket else 5]:
                if sym not in ai_watchlist_suggestions:
                    ai_watchlist_suggestions[sym] = {
                        "conviction": 6,
                        "thesis": "AI suggested",
                        "category": "momentum",
                        "added_at": now_ct.isoformat(),
                    }
                    added_tickers.append(sym)

    if not added_tickers:
        # Fallback: use existing dynamic hot stocks
        logger.warning("AI watchlist returned no tickers — falling back to FMP dynamic stocks")
        try:
            TICKERS = get_dynamic_hot_stocks()
            for t in TICKERS:
                if t not in price_history:
                    price_history[t] = deque(maxlen=60)
            save_bot_state()
        except Exception as e:
            logger.error(f"Fallback get_dynamic_hot_stocks failed: {e}")
        return

    # ── 4. Rebuild TICKERS list ──────────────────────────────────
    new_tickers = list(CORE_TICKERS)  # always start with core 30

    # Keep tickers with open paper positions
    for t in paper_positions:
        if t not in new_tickers:
            new_tickers.append(t)

    # Add AI-suggested tickers (sorted by conviction, highest first)
    ai_sorted = sorted(ai_watchlist_suggestions.keys(),
                       key=lambda t: ai_watchlist_suggestions[t].get("conviction", 0),
                       reverse=True)
    for t in ai_sorted:
        if t not in new_tickers and len(new_tickers) < 80:
            new_tickers.append(t)

    # Add top squeeze score tickers not already included (up to 10)
    if squeeze_scores:
        top_sq = sorted(squeeze_scores, key=squeeze_scores.get, reverse=True)[:10]
        for t in top_sq:
            if t not in new_tickers and len(new_tickers) < 80:
                new_tickers.append(t)

    # Fill remaining with FMP dynamic stocks
    try:
        fmp_stocks = get_dynamic_hot_stocks()
        for t in fmp_stocks:
            if t not in new_tickers and len(new_tickers) < 80:
                new_tickers.append(t)
    except Exception as e:
        logger.debug(f"FMP fill failed: {e}")

    # Drop tickers AI suggested removing
    for t in dropped_tickers:
        if t in new_tickers and t not in CORE_TICKERS and t not in paper_positions:
            new_tickers.remove(t)
            ai_watchlist_suggestions.pop(t, None)

    # Cap at 80
    new_tickers = new_tickers[:80]

    old_set = set(TICKERS)
    TICKERS = new_tickers

    # Initialize price_history for new tickers
    for t in TICKERS:
        if t not in price_history:
            price_history[t] = deque(maxlen=60)

    # ── 5. Send Telegram summary ─────────────────────────────────
    time_label = now_ct.strftime("%I:%M %p CT")
    mode_label = "Premarket" if is_premarket else "Intraday"
    ai_watchlist_last_refresh = f"{time_label} ({mode_label.lower()})"

    added_strs = []
    for t in added_tickers[:10]:
        info = ai_watchlist_suggestions.get(t, {})
        conv = info.get("conviction", "?")
        cat = info.get("category", "")
        short_cat = cat.replace("_", " ")[:12]
        added_strs.append(f"{t} ({conv}/10 - {short_cat})")

    removed = [t for t in old_set if t not in set(TICKERS) and t not in CORE_TICKERS]

    msg_lines = [f"AI WATCHLIST UPDATE ({mode_label})"]
    if added_strs:
        msg_lines.append(f"Added: {', '.join(added_strs)}")
        if len(added_tickers) > 10:
            msg_lines.append(f"  +{len(added_tickers) - 10} more")
    if removed:
        msg_lines.append(f"Dropped: {', '.join(removed[:10])}")
    if dropped_tickers:
        msg_lines.append(f"AI suggested drop: {', '.join(dropped_tickers[:5])}")

    core_count = len([t for t in TICKERS if t in CORE_TICKERS])
    dynamic_count = len(TICKERS) - core_count
    msg_lines.append(f"Now monitoring: {len(TICKERS)} stocks ({core_count} core + {dynamic_count} dynamic)")

    send_telegram("\n".join(msg_lines))
    save_bot_state()
    logger.info(f"AI watchlist refresh complete — mode={mode}, added={len(added_tickers)}, "
                f"dropped={len(dropped_tickers)}, total={len(TICKERS)}")


TICKERS = list(CORE_TICKERS)   # seed immediately; refreshed at startup and 8:30 AM daily

# ============================================================
# ALERT ENGINE
# ============================================================
def send_alert(ticker, pct_change, current_price, volume_spike=False, alert_type="spike"):
    global daily_alerts
    daily_alerts += 1
    news_items   = fetch_latest_news(ticker)

    if alert_type == "day":
        # Day-change mover alert — simpler format
        spike_label = "MOVER"
        direction   = "up" if pct_change > 0 else "down"
        grok_prompt = (
            f"Analyze daily mover: {ticker} {pct_change:+.1f}% today. "
            f"Price ${current_price:.2f}. "
            f"Stock is {direction} from previous close. Short analysis."
        )
        ai        = get_ai_response(grok_prompt)
        news_text = "\n".join([f"• {h[:80]}" for h, _ in news_items]) if news_items else "No news"

        message = (
            f"📊 MOVER ALERT: {ticker} {pct_change:+.1f}% (${current_price:.2f})"
            f" — daily move from prev close"
            + (" | 🔊 Vol Spike" if volume_spike else "") + "\n"
            + f"\nClaude: {ai}\n\n"
            f"News:\n{news_text}"
        )
        send_telegram(message)
        recent_alerts.append(f"{ticker} {pct_change:+.1f}% day at {datetime.now(CT).strftime('%H:%M')}")
    else:
        # Original spike alert — unchanged
        spike_label  = "VOLUME+PRICE SPIKE" if volume_spike else "SPIKE"

        # Pull live technicals from accumulated price history
        sq       = compute_squeeze_score(ticker)
        rsi_str  = f"RSI {sq['rsi']:.0f}" if sq['rsi'] is not None else ""
        pb_str   = f"%B {sq['pct_b']:.2f}" if sq['pct_b'] is not None else ""
        tech_str = "  ".join(filter(None, [rsi_str, pb_str, f"Squeeze {sq['score']:.0f}/100"]))

        grok_prompt = (
            f"Analyze {spike_label}: {ticker} {pct_change:+.1f}% in ~5 min. "
            f"Price ${current_price:.2f}. {tech_str}. "
            + ("HIGH VOLUME detected. " if volume_spike else "")
            + "Short analysis."
        )
        ai        = get_ai_response(grok_prompt)
        news_text = "\n".join([f"• {h[:80]}" for h, _ in news_items]) if news_items else "No news"

        message = (
            f"🚨 {ticker} {spike_label}\n"
            f"{pct_change:+.1f}% | ${current_price:.2f}"
            + (" | 🔊 Vol Spike" if volume_spike else "") + "\n"
            + (f"{tech_str}\n" if tech_str else "")
            + f"\nClaude: {ai}\n\n"
            f"News:\n{news_text}"
        )
        send_telegram(message)
        recent_alerts.append(f"{ticker} {pct_change:+.1f}% at {datetime.now(CT).strftime('%H:%M')}")

    # Cap recent_alerts at runtime to prevent unbounded memory growth
    while len(recent_alerts) > 200:
        recent_alerts.pop(0)
    # Persist alert history (non-blocking — best-effort)
    threading.Thread(target=save_bot_state, daemon=True).start()

def check_custom_price_alerts(ticker, current_price):
    if ticker not in custom_price_alerts or not custom_price_alerts[ticker]:
        return
    if current_price <= 0:
        return
    triggered = []
    for target in custom_price_alerts[ticker]:
        if abs(current_price - target) / max(target, 0.01) < 0.005:   # within 0.5%
            send_telegram(
                f"Price Alert Hit!\n{ticker} reached ${current_price:.2f}\n(Target: ${target:.2f})"
            )
            triggered.append(target)
    for t in triggered:
        if t in custom_price_alerts.get(ticker, []):
            custom_price_alerts[ticker].remove(t)
    # Clean up empty ticker entries
    if ticker in custom_price_alerts and not custom_price_alerts[ticker]:
        del custom_price_alerts[ticker]
    if triggered:
        threading.Thread(target=save_bot_state, daemon=True).start()

def _scan_ticker(ticker: str, now: datetime):
    """Scan a single ticker — runs in thread pool."""
    c, vol, pc = fetch_finnhub_quote(ticker)
    if not c or c < MIN_PRICE:
        return

    price_history.setdefault(ticker, deque(maxlen=60)).append((now, c))
    check_custom_price_alerts(ticker, c)

    # Update squeeze score on every scan
    sq = compute_squeeze_score(ticker)
    squeeze_scores[ticker] = sq["score"]

    if ticker in last_prices:
        old_price = last_prices[ticker]
        for ts, p in list(price_history[ticker]):
            if (now - ts).total_seconds() > 280:
                old_price = p

        change = (c - old_price) / old_price
        if abs(change) >= THRESHOLD:
            last_alert = last_alert_time.get(ticker, now - timedelta(days=1))
            if (now - last_alert).total_seconds() / 60 >= COOLDOWN_MINUTES:
                vol_spike = False
                if vol and pc:
                    try:
                        m = _finnhub_metrics(ticker)
                        avg_vol = (m.get("10DayAverageTradingVolume") or 0) * 1_000_000
                        if avg_vol > 0:
                            vol_spike = vol > avg_vol * VOLUME_SPIKE_MULT
                    except Exception as e:
                        logger.debug(f"Vol spike check {ticker}: {e}")
                send_alert(ticker, change * 100, c, vol_spike)
                last_alert_time[ticker] = now

    # ── Day-change alerts: catch sustained moves vs prev close ──
    if pc and pc > 0:
        day_change = (c - pc) / pc
        if abs(day_change) >= THRESHOLD:
            day_alert_key = f"{ticker}:day:{datetime.now(CT).strftime('%Y-%m-%d')}"
            if day_alert_key not in last_alert_time:
                # Only fire once per ticker per day for day-change alerts
                last_alert_time[day_alert_key] = now
                # Check volume spike
                vol_spike = False
                if vol:
                    cached_m = _metrics_cache.get(f"metrics:{ticker}")
                    if cached_m:
                        avg_vol = (cached_m.get("10DayAverageTradingVolume") or 0) * 1_000_000
                        if avg_vol > 0:
                            vol_spike = vol > avg_vol * VOLUME_SPIKE_MULT
                send_alert(ticker, day_change * 100, c, vol_spike, alert_type="day")

    last_prices[ticker] = c


def check_stocks():
    if monitoring_paused or get_trading_session() == "closed":
        return
    now = datetime.now(CT)
    tickers = list(TICKERS)
    logger.info(f"Scanning {len(tickers)} stocks (batched)...")

    BATCH = 20
    for i in range(0, len(tickers), BATCH):
        batch = tickers[i:i+BATCH]
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_scan_ticker, t, now): t for t in batch}
            for future in as_completed(futures):
                t = futures[future]
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"Scan error for {t}: {e}")
        if i + BATCH < len(tickers):
            time.sleep(2)  # Let rate limiter replenish between batches

    # Paper trading evaluation runs after every scan cycle
    try:
        paper_scan()
    except Exception as e:
        logger.error(f"paper_scan error: {e}")

# ============================================================
# PAPER TRADING ENGINE
# ============================================================
#
# Signal stack (research-backed, bullish-only):
#   1. RSI Momentum      — 50-65 trending zone = 20 pts
#   2. BB Breakout/Bounce — price above mid or %B recovering = 15 pts
#   3. MACD Crossover    — fast/slow EMA divergence direction = 15 pts
#   4. Volume Confirm    — current vol vs 5-day avg = 15 pts
#   5. Squeeze Momentum  — existing score (reused, scaled) = 10 pts
#   6. Price Slope       — 5-min linear regression slope = 10 pts
#   7. Grok AI Signal    — directional confidence 0-100, scaled = 15 pts
#
# Trade rules (bullish only, no shorts):
#   • BUY  when composite ≥ PAPER_MIN_SIGNAL and RSI < 72 and cash available
#   • SELL on 8% take-profit, 4% stop-loss, or signal collapse (≤30 + positive)
#   • Max PAPER_MAX_ACTIONS actions per ticker per day
#   • Max PAPER_MAX_POSITIONS open positions at once
#   • Max PAPER_MAX_POS_PCT of total portfolio in a single name
# ============================================================

PAPER_STARTING_CAPITAL = 100_000.0
PAPER_LOG              = os.getenv("PAPER_LOG_PATH", "investment.log")
PAPER_STATE_FILE       = os.getenv("PAPER_STATE_PATH", "paper_state.json")
PAPER_MAX_ACTIONS      = 3        # per ticker per trading day
PAPER_MAX_POSITIONS    = 8        # simultaneous open positions
PAPER_MAX_POS_PCT      = 0.20     # 20% of portfolio per position
PAPER_TAKE_PROFIT_PCT  = 0.08     # 8% take-profit
PAPER_STOP_LOSS_PCT    = 0.04     # 4% stop-loss
PAPER_MIN_SIGNAL       = 65       # min composite score (0-100) to open a position

# Bot-wide persistence (watchlists, alerts, tickers, conversation history …)
# Set BOT_STATE_PATH env var to a Railway Volume path, e.g. /data/bot_state.json
_data_dir = os.path.dirname(os.getenv("PAPER_STATE_PATH", "paper_state.json"))
BOT_STATE_FILE = os.getenv(
    "BOT_STATE_PATH",
    os.path.join(_data_dir, "bot_state.json") if _data_dir else "bot_state.json"
)

# ── Live state (populated by load_paper_state on startup) ─────
paper_cash          = PAPER_STARTING_CAPITAL
paper_positions     = {}
paper_trades_today  = []
paper_daily_counts  = {}
paper_all_trades    = []
paper_signals_cache = {}

_paper_save_lock = threading.Lock()


def save_paper_state():
    """
    Persist all paper trading state to PAPER_STATE_FILE (JSON).
    Thread-safe. Called after every buy/sell and at EOD.
    Point PAPER_STATE_PATH env var at a Railway Volume mount for
    true cross-deploy persistence (e.g. /data/paper_state.json).
    """
    state = {
        "paper_cash":         paper_cash,
        "paper_positions":    paper_positions,
        "paper_all_trades":   paper_all_trades,
        "paper_trades_today": paper_trades_today,
        "paper_daily_counts": paper_daily_counts,
        "saved_at":           datetime.now(CT).isoformat(),
    }
    with _paper_save_lock:
        tmp = PAPER_STATE_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, default=str)
            os.replace(tmp, PAPER_STATE_FILE)   # atomic rename
            logger.info(f"Paper state saved -> {PAPER_STATE_FILE}")
        except Exception as e:
            logger.error(f"save_paper_state failed: {e}")


def load_paper_state():
    """
    Load paper trading state from disk on startup.
    Falls back to clean $100k state if file missing or corrupt.
    Skips paper_trades_today and paper_daily_counts if the saved
    date is not today (i.e. restarted on a new trading day).
    """
    global paper_cash, paper_positions, paper_all_trades
    global paper_trades_today, paper_daily_counts

    if not os.path.exists(PAPER_STATE_FILE):
        paper_log(
            f"No saved state found at {PAPER_STATE_FILE}. "
            f"Starting fresh with ${PAPER_STARTING_CAPITAL:,.0f}."
        )
        return

    try:
        with open(PAPER_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        paper_cash       = float(state.get("paper_cash", PAPER_STARTING_CAPITAL))
        paper_positions  = state.get("paper_positions", {})
        paper_all_trades = state.get("paper_all_trades", [])

        # Only restore intraday state if saved today
        saved_at   = state.get("saved_at", "")
        saved_date = saved_at[:10] if saved_at else ""
        today      = datetime.now(CT).strftime("%Y-%m-%d")

        if saved_date == today:
            paper_trades_today = state.get("paper_trades_today", [])
            paper_daily_counts = state.get("paper_daily_counts", {})
            paper_log(
                f"State restored (same day). "
                f"Cash: ${paper_cash:,.2f} | "
                f"Positions: {len(paper_positions)} | "
                f"Trades today: {len(paper_trades_today)} | "
                f"Lifetime trades: {len(paper_all_trades)}"
            )
        else:
            paper_trades_today = []
            paper_daily_counts = {}
            paper_log(
                f"State restored (new day — intraday counters reset). "
                f"Cash: ${paper_cash:,.2f} | "
                f"Positions: {len(paper_positions)} | "
                f"Lifetime trades: {len(paper_all_trades)}"
            )

    except Exception as e:
        logger.error(f"load_paper_state failed: {e}. Starting fresh.")
        paper_cash       = PAPER_STARTING_CAPITAL
        paper_positions  = {}
        paper_all_trades = []
        paper_trades_today = []
        paper_daily_counts = {}



_bot_save_lock = threading.Lock()

def save_bot_state():
    """
    Persist all non-paper bot state to BOT_STATE_FILE (JSON).
    Saves: TICKERS, user_watchlists, custom_price_alerts,
           recent_alerts, conversation_history, squeeze_scores,
           daily_alerts, monitoring_paused.

    Called automatically after every mutation of the above.
    Set BOT_STATE_PATH=/data/bot_state.json in Railway to use the Volume.
    """
    state = {
        "tickers":              list(TICKERS),
        "user_watchlists":      user_watchlists,
        "custom_price_alerts":  custom_price_alerts,
        "recent_alerts":        list(recent_alerts)[-200:],  # cap at 200
        "conversation_history": {
            cid: msgs[-20:]                                  # cap at 20 per chat
            for cid, msgs in conversation_history.items()
        },
        "squeeze_scores":       squeeze_scores,
        "daily_alerts":         daily_alerts,
        "monitoring_paused":    monitoring_paused,
        "ai_watchlist_suggestions": ai_watchlist_suggestions,
        "ai_watchlist_last_refresh": ai_watchlist_last_refresh,
        "last_prices": {k: v for k, v in last_prices.items()},
        "saved_at":             datetime.now(CT).isoformat(),
    }
    with _bot_save_lock:
        tmp = BOT_STATE_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, default=str)
            os.replace(tmp, BOT_STATE_FILE)
            logger.debug(f"Bot state saved -> {BOT_STATE_FILE}")
        except Exception as e:
            logger.error(f"save_bot_state failed: {e}")


def load_bot_state():
    """
    Restore bot state from disk on startup.
    Safely skips missing keys — graceful forward/backward compat.
    daily_alerts is only restored if saved_at is today (intraday counter).
    """
    global TICKERS, user_watchlists, custom_price_alerts
    global recent_alerts, conversation_history, squeeze_scores
    global daily_alerts, monitoring_paused
    global ai_watchlist_suggestions, ai_watchlist_last_refresh

    if not os.path.exists(BOT_STATE_FILE):
        logger.info(f"No bot state file at {BOT_STATE_FILE} — starting fresh.")
        return

    try:
        with open(BOT_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        saved_tickers = state.get("tickers", [])
        if saved_tickers:
            # Merge saved tickers with CORE_TICKERS — never drop the anchor list
            TICKERS = list(dict.fromkeys(saved_tickers + CORE_TICKERS))
            logger.info(f"Restored {len(TICKERS)} tickers")

        user_watchlists     = state.get("user_watchlists", {})
        raw_alerts          = state.get("custom_price_alerts", {})
        # Clean up: remove empty alert lists and ensure values are lists of floats
        custom_price_alerts = {}
        for ticker, targets in raw_alerts.items():
            if isinstance(targets, list) and targets:
                clean_targets = []
                for t in targets:
                    try:
                        clean_targets.append(float(t))
                    except (ValueError, TypeError):
                        pass
                if clean_targets:
                    custom_price_alerts[ticker] = clean_targets
        recent_alerts       = state.get("recent_alerts", [])[-200:]  # cap on load
        conversation_history= state.get("conversation_history", {})
        squeeze_scores      = state.get("squeeze_scores", {})
        monitoring_paused   = state.get("monitoring_paused", False)
        ai_watchlist_suggestions = state.get("ai_watchlist_suggestions", {})
        ai_watchlist_last_refresh = state.get("ai_watchlist_last_refresh", "")
        last_prices.update(state.get("last_prices", {}))

        # Only restore daily_alerts if saved today
        saved_at   = state.get("saved_at", "")
        saved_date = saved_at[:10] if saved_at else ""
        today      = datetime.now(CT).strftime("%Y-%m-%d")
        daily_alerts = state.get("daily_alerts", 0) if saved_date == today else 0

        logger.info(
            f"Bot state restored from {BOT_STATE_FILE} | "
            f"tickers={len(TICKERS)} | watchlists={len(user_watchlists)} | "
            f"alerts={len(custom_price_alerts)} | "
            f"conversations={len(conversation_history)} | "
            f"monitoring_paused={monitoring_paused}"
        )

    except Exception as e:
        logger.error(f"load_bot_state failed: {e} — using defaults.")


# ── Dedicated investment logger ───────────────────────────────
inv_logger = logging.getLogger("investment")
inv_logger.setLevel(logging.INFO)
_inv_fh = logging.FileHandler(PAPER_LOG, encoding="utf-8")
_inv_fh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
inv_logger.addHandler(_inv_fh)
inv_logger.propagate = False


def paper_log(msg: str):
    """Write a timestamped line to investment.log and the main logger."""
    inv_logger.info(msg)
    logger.info(f"[PAPER] {msg}")


def paper_portfolio_value() -> float:
    """Total portfolio value: cash + market value of all open positions."""
    total = paper_cash
    for ticker, pos in paper_positions.items():
        try:
            price, _, _ = fetch_finnhub_quote(ticker)
            if price:
                total += pos["shares"] * price
        except Exception as e:
            logger.debug(f"paper_portfolio_value {ticker}: {e}")
            total += pos["shares"] * pos["avg_cost"]  # fallback to cost
    return total


def _compute_ema(prices: list, period: int) -> float | None:
    """Exponential moving average."""
    if len(prices) < period:
        return None
    k   = 2.0 / (period + 1)
    ema = sum(prices[:period]) / period   # SMA seed
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema


def _compute_macd(prices: list) -> tuple:
    """
    Returns (macd_line, signal_line, histogram).
    Uses standard 12/26/9 parameters on the available price_history.
    Returns (None, None, None) if insufficient data.
    """
    if len(prices) < 26:
        return None, None, None
    ema12 = _compute_ema(prices, 12)
    ema26 = _compute_ema(prices, 26)
    if ema12 is None or ema26 is None:
        return None, None, None
    macd = ema12 - ema26
    # Approximate signal as EMA(9) of last few MACD values
    # Build a mini MACD series from rolling windows
    macd_series = []
    for i in range(26, len(prices) + 1):
        e12 = _compute_ema(prices[:i], 12)
        e26 = _compute_ema(prices[:i], 26)
        if e12 and e26:
            macd_series.append(e12 - e26)
    if len(macd_series) >= 9:
        sig = _compute_ema(macd_series, 9)
        hist = macd - sig if sig else 0
        return round(macd, 4), round(sig, 4), round(hist, 4)
    return round(macd, 4), None, None


def compute_paper_signal(ticker: str) -> dict:
    """
    Composite signal engine (9 components, max 125 pts).
    Components: RSI(20) + BB(15) + MACD(15) + Volume(15) + Squeeze(10) +
    Slope(10) + AI Direction(15) + AI Watchlist(10) + Multi-Day Trend(15).
    Caches for 60 seconds to avoid hammering AI.
    """
    now = datetime.now(CT)

    # Return cached signal if fresh
    cached = paper_signals_cache.get(ticker)
    if cached and (now - cached["ts"]).total_seconds() < 120:
        return cached

    hist_raw = list(price_history.get(ticker, deque()))
    prices   = [p for _, p in hist_raw] if hist_raw and isinstance(hist_raw[0], tuple) else hist_raw

    score  = 0
    comps  = {}
    detail = []

    # ── 1. RSI Momentum (20 pts) ──────────────────────────────
    rsi = compute_rsi(prices) if len(prices) >= 15 else None
    if rsi is not None:
        if 50 <= rsi <= 65:
            pts = 20                                   # sweet spot
        elif 65 < rsi <= 72:
            pts = 10                                   # still bullish but overbought warning
        elif 40 <= rsi < 50:
            pts = 8                                    # recovering
        else:
            pts = 0
        score += pts
        comps["rsi"] = round(rsi, 1)
        comps["rsi_pts"] = pts
        detail.append(f"RSI={rsi:.1f}({pts}pts)")

    # ── 2. Bollinger Band Position (15 pts) ───────────────────
    _, _, _, pct_b, bw = compute_bollinger(prices) if len(prices) >= 20 else (None,)*5
    if pct_b is not None:
        if 0.5 <= pct_b <= 0.85:
            pts = 15                                   # above mid, not at extreme
        elif 0.85 < pct_b <= 1.0:
            pts = 8                                    # near upper (slightly extended)
        elif 0.3 <= pct_b < 0.5:
            pts = 10                                   # just below mid, potential bounce
        else:
            pts = max(0, int(pct_b * 10))
        score += pts
        comps["pct_b"] = pct_b
        comps["bw_pts"] = pts
        detail.append(f"%B={pct_b:.2f}({pts}pts)")

    # ── 3. MACD Crossover (15 pts) ────────────────────────────
    macd_line, sig_line, hist_val = _compute_macd(prices)
    if macd_line is not None:
        if macd_line > 0 and (sig_line is None or macd_line > sig_line):
            pts = 15   # bullish: MACD above zero and above signal
        elif macd_line > 0:
            pts = 8    # above zero but below signal — weakening
        elif hist_val is not None and hist_val > 0:
            pts = 5    # histogram turning positive — early signal
        else:
            pts = 0
        score += pts
        comps["macd"] = macd_line
        comps["macd_pts"] = pts
        detail.append(f"MACD={macd_line:.4f}({pts}pts)")

    # ── 4. Volume Confirmation (15 pts) ───────────────────────
    try:
        _, vol, _ = fetch_finnhub_quote(ticker)
        m = _finnhub_metrics(ticker)
        avg_vol = (m.get("10DayAverageTradingVolume") or 0) * 1_000_000
        if vol and avg_vol > 0:
            ratio   = vol / avg_vol
            if ratio >= 2.0:
                pts = 15
            elif ratio >= 1.5:
                pts = 10
            elif ratio >= 1.0:
                pts = 5
            else:
                pts = 0
            score += pts
            comps["vol_ratio"] = round(ratio, 2)
            comps["vol_pts"]   = pts
            detail.append(f"VolRatio={ratio:.1f}x({pts}pts)")
    except Exception as e:
        logger.debug(f"Signal score vol ratio {ticker}: {e}")

    # ── 5. Squeeze Score (10 pts, scaled from existing) ───────
    sq = compute_squeeze_score(ticker)
    sq_pts = round(sq["score"] / 10, 1)   # 0-100 -> 0-10 pts
    score += sq_pts
    comps["squeeze"] = sq["score"]
    comps["sq_pts"]  = sq_pts
    detail.append(f"Squeeze={sq['score']:.0f}({sq_pts}pts)")

    # ── 6. Price Slope / Linear Momentum (10 pts) ─────────────
    if len(prices) >= 10:
        xs      = list(range(len(prices[-10:])))
        ys      = prices[-10:]
        n       = len(xs)
        x_mean  = sum(xs) / n
        y_mean  = sum(ys) / n
        num     = sum((xs[i] - x_mean) * (ys[i] - y_mean) for i in range(n))
        den     = sum((xs[i] - x_mean) ** 2 for i in range(n))
        slope   = num / den if den != 0 else 0
        slope_pct = slope / y_mean * 100 if y_mean else 0
        if slope_pct >= 0.3:
            pts = 10
        elif slope_pct >= 0.1:
            pts = 6
        elif slope_pct >= 0:
            pts = 2
        else:
            pts = 0
        score += pts
        comps["slope_pct"] = round(slope_pct, 3)
        comps["slope_pts"] = pts
        detail.append(f"Slope={slope_pct:.3f}%({pts}pts)")

    # ── 7. Grok AI Directional Signal (15 pts) ────────────────
    try:
        price_now = prices[-1] if prices else 0
        chg_5m    = ((prices[-1] - prices[-6]) / prices[-6] * 100
                     if len(prices) >= 6 and prices[-6] else 0)
        grok_prompt = (
            f"Paper trading signal for {ticker}: "
            f"price ${price_now:.2f}, 5-min change {chg_5m:+.2f}%, "
            f"RSI={rsi:.1f if rsi else 'N/A'}, "
            f"MACD={macd_line:.4f if macd_line else 'N/A'}, "
            f"squeeze={sq['score']:.0f}/100. "
            f"Bullish strategies only. "
            f"Respond ONLY with: SIGNAL:<BUY|HOLD|AVOID> CONFIDENCE:<0-100> REASON:<10 words max>"
        )
        raw_ai = get_ai_response(grok_prompt, max_tokens=60, fast=True)
        ai_score = 50   # default neutral
        ai_signal = "HOLD"
        ai_reason = ""
        if "BUY" in raw_ai.upper():
            ai_signal = "BUY"
            try:
                ai_score = int([w for w in raw_ai.split() if w.startswith("CONFIDENCE:")][0].split(":")[1])
            except (IndexError, ValueError, TypeError):
                ai_score = 70
            pts = int(15 * ai_score / 100)
        elif "AVOID" in raw_ai.upper():
            ai_signal = "AVOID"
            pts = 0
        else:
            pts = 5
        try:
            ai_reason = raw_ai.split("REASON:")[-1].strip()[:60] if "REASON:" in raw_ai else raw_ai[:60]
        except (IndexError, AttributeError):
            pass
        score += pts
        comps["grok_signal"]     = ai_signal
        comps["grok_confidence"] = ai_score
        comps["grok_reason"]     = ai_reason
        comps["grok_pts"]        = pts
        detail.append(f"Claude={ai_signal}@{ai_score}({pts}pts)")
    except Exception as e:
        logger.debug(f"Grok signal error for {ticker}: {e}")

    # ── 8. AI Watchlist Conviction (10 pts bonus) ──────────────
    ai_info = ai_watchlist_suggestions.get(ticker)
    if ai_info and ai_info.get("conviction", 0) >= 7:
        pts = min(10, ai_info["conviction"])  # 7->7pts, 8->8pts, 9->9pts, 10->10pts
        score += pts
        comps["ai_conviction"] = ai_info["conviction"]
        comps["ai_category"] = ai_info.get("category", "")
        detail.append(f"AI={ai_info['conviction']}({pts}pts)")

    # ── 9. Multi-Day Trend (15 pts) ───────────────────────────
    daily = daily_candles.get(ticker)
    if daily and len(daily) >= 10:
        closes = [d["close"] for d in daily]
        volumes = [d["volume"] for d in daily]

        # 9a. SMA Trend (6 pts)
        sma5 = sum(closes[-5:]) / 5
        sma20 = sum(closes[-20:]) / min(20, len(closes))
        current_close = closes[-1]

        sma_pts = 0
        if current_close > sma5 > sma20:
            sma_pts = 6  # Strong uptrend alignment
        elif current_close > sma5 and sma5 <= sma20:
            sma_pts = 3  # Short-term bounce
        elif current_close < sma5 and sma5 > sma20:
            sma_pts = 1  # Pullback in uptrend

        score += sma_pts
        comps["sma5"] = round(sma5, 2)
        comps["sma20"] = round(sma20, 2)
        comps["sma_pts"] = sma_pts

        # 9b. Multi-Day Momentum (5 pts)
        mom_pts = 0
        if len(closes) >= 6:
            ret_5d = (closes[-1] - closes[-6]) / closes[-6]
            if -0.08 <= ret_5d < -0.03:
                mom_pts = 5  # Oversold bounce setup
            elif 0.01 <= ret_5d <= 0.05:
                mom_pts = 4  # Steady uptrend
            elif 0.05 < ret_5d <= 0.10:
                mom_pts = 2  # Hot but extended
            elif -0.03 <= ret_5d < 0.01:
                mom_pts = 1  # Flat
            # >10% or <-8%: 0 pts

            score += mom_pts
            comps["ret_5d"] = round(ret_5d * 100, 2)
            comps["mom_pts"] = mom_pts

        # 9c. Daily Volume Trend (4 pts)
        avg_daily_vol = sum(volumes[-10:]) / min(10, len(volumes[-10:]))
        today_vol = volumes[-1]
        vol_d_pts = 0
        if avg_daily_vol > 0:
            vol_d_ratio = today_vol / avg_daily_vol
            if vol_d_ratio >= 1.5:
                vol_d_pts = 4
            elif vol_d_ratio >= 1.2:
                vol_d_pts = 2
            comps["daily_vol_ratio"] = round(vol_d_ratio, 2)

        score += vol_d_pts
        comps["vol_d_pts"] = vol_d_pts

        total_multi = sma_pts + mom_pts + vol_d_pts if len(closes) >= 6 else sma_pts + vol_d_pts
        detail.append(f"MultiDay={total_multi}pts(SMA{sma_pts}+Mom{mom_pts if len(closes) >= 6 else '?'}+DVol{vol_d_pts})")

    result = {
        "score":   round(min(score, 125), 1),  # max 125 with multi-day + AI bonus
        "detail":  " | ".join(detail),
        "comps":   comps,
        "rsi":     rsi,
        "macd":    macd_line,
        "ts":      now,
    }
    paper_signals_cache[ticker] = result
    return result


def _paper_position_size(ticker: str, signal_score: float) -> int:
    """
    Calculate shares to buy based on signal strength and portfolio rules.
    Returns 0 if no trade should be made.
    """
    portfolio_val = paper_portfolio_value()
    max_dollars   = portfolio_val * PAPER_MAX_POS_PCT

    # Scale position size with signal confidence (65->100 maps to 50%->100% of max)
    strength   = min(1.0, (signal_score - PAPER_MIN_SIGNAL) / (100 - PAPER_MIN_SIGNAL))
    dollars    = max_dollars * (0.5 + 0.5 * strength)
    dollars    = min(dollars, paper_cash * 0.95)   # never use more than 95% of cash

    # AI conviction boost: 15% larger position for high-conviction AI picks
    ai_info = ai_watchlist_suggestions.get(ticker)
    if ai_info and ai_info.get("conviction", 0) >= 8:
        dollars *= 1.15
        dollars = min(dollars, portfolio_val * PAPER_MAX_POS_PCT)  # still respect max

    if dollars < 100:
        return 0

    price, _, _ = fetch_finnhub_quote(ticker)
    if not price or price <= 0:
        return 0

    return max(1, int(dollars / price))


def paper_evaluate_ticker(ticker: str):
    """
    Evaluate a single ticker for paper trading actions.
    Called from within the scanner thread for every scan cycle.
    """
    global paper_cash

    if get_trading_session() not in ("regular", "extended"):
        return

    now   = datetime.now(CT)
    today = now.strftime("%Y-%m-%d")

    # Respect daily action limit
    count_key = f"{ticker}:{today}"
    if paper_daily_counts.get(count_key, 0) >= PAPER_MAX_ACTIONS:
        return

    price, _, _ = fetch_finnhub_quote(ticker)
    if not price or price < MIN_PRICE:
        return

    # ── Check existing position: take-profit / stop-loss / signal exit ──
    if ticker in paper_positions:
        pos       = paper_positions[ticker]
        cost      = pos["avg_cost"]
        pnl_pct   = (price - cost) / cost

        # Update high-water mark for trailing context
        if price > pos.get("high", cost):
            paper_positions[ticker]["high"] = price

        should_sell = False
        sell_reason = ""

        if pnl_pct >= PAPER_TAKE_PROFIT_PCT:
            should_sell = True
            sell_reason = f"TAKE-PROFIT +{pnl_pct*100:.1f}%"
        elif pnl_pct <= -PAPER_STOP_LOSS_PCT:
            should_sell = True
            sell_reason = f"STOP-LOSS {pnl_pct*100:.1f}%"
        else:
            sig = compute_paper_signal(ticker)
            if sig["score"] <= 30 and pnl_pct > 0:
                should_sell = True
                sell_reason = f"SIGNAL-COLLAPSE score={sig['score']:.0f} pnl={pnl_pct*100:+.1f}%"

        if should_sell:
            shares    = pos["shares"]
            proceeds  = shares * price
            cost_basis = shares * cost
            realized_pnl = proceeds - cost_basis

            paper_cash += proceeds
            del paper_positions[ticker]
            paper_daily_counts[count_key] = paper_daily_counts.get(count_key, 0) + 1

            trade = {
                "action": "SELL", "ticker": ticker, "shares": shares,
                "price": price, "proceeds": proceeds,
                "cost": cost_basis, "pnl": realized_pnl,
                "pnl_pct": pnl_pct * 100, "reason": sell_reason,
                "time": now.strftime("%H:%M:%S"), "date": today,
                "portfolio_value": paper_portfolio_value(),
            }
            paper_trades_today.append(trade)
            paper_all_trades.append(trade)
            if len(paper_all_trades) > 5000:
                paper_all_trades[:] = paper_all_trades[-4000:]

            msg = (
                f"SELL | {ticker} | {shares} shares @ ${price:.2f} | "
                f"P&L: ${realized_pnl:+.2f} ({pnl_pct*100:+.1f}%) | "
                f"Reason: {sell_reason} | "
                f"Portfolio: ${paper_portfolio_value():,.0f}"
            )
            paper_log(msg)

            # ── Enriched SELL notification ─────────────────────
            hold_mins = ""
            try:
                entry_dt = datetime.strptime(
                    f"{pos.get('entry_date', today)} {pos.get('entry_time', '00:00:00')}",
                    "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=CT)
                held = int((now - entry_dt).total_seconds() / 60)
                hold_mins = f"{held}m" if held < 60 else f"{held//60}h {held%60}m"
            except (ValueError, TypeError, KeyError) as e:
                logger.debug(f"Hold time calc: {e}")

            pnl_emoji = "🟢" if realized_pnl >= 0 else "🔴"
            reason_map = {
                "TAKE-PROFIT": "✅ Take-profit hit",
                "STOP-LOSS":   "🛑 Stop-loss triggered",
                "SIGNAL-COLLAPSE": "📉 Signal deteriorated",
            }
            reason_label = next(
                (v for k, v in reason_map.items() if k in sell_reason), sell_reason
            )

            new_val     = paper_portfolio_value()
            lifetime_pct = (new_val - PAPER_STARTING_CAPITAL) / PAPER_STARTING_CAPITAL * 100

            send_telegram(
                f"{pnl_emoji} PAPER SELL — {ticker}\n"
                f"{'─'*28}\n"
                f"Shares:    {shares} @ ${price:.2f}\n"
                f"Entry:     ${pos['avg_cost']:.2f}"
                + (f"  (held {hold_mins})" if hold_mins else "") + "\n"
                f"P&L:       ${realized_pnl:+.2f}  ({pnl_pct*100:+.1f}%)\n"
                f"Reason:    {reason_label}\n"
                f"{'─'*28}\n"
                f"Cash:      ${paper_cash:,.0f}\n"
                f"Positions: {len(paper_positions)}/{PAPER_MAX_POSITIONS}\n"
                f"Portfolio: ${new_val:,.0f}  ({lifetime_pct:+.2f}% all-time)\n"
                f"Trades today: {len(paper_trades_today)}"
            )
            save_paper_state()
        return  # one action per scan cycle per ticker

    # ── Check for new buy opportunity ────────────────────────
    if len(paper_positions) >= PAPER_MAX_POSITIONS:
        return
    if paper_cash < 200:
        return

    sig = compute_paper_signal(ticker)
    if sig["score"] < PAPER_MIN_SIGNAL:
        return

    rsi = sig.get("rsi")
    if rsi and rsi > 72:   # avoid chasing overbought
        return

    shares = _paper_position_size(ticker, sig["score"])
    if shares <= 0:
        return

    cost         = shares * price
    paper_cash  -= cost
    paper_positions[ticker] = {
        "shares":     shares,
        "avg_cost":   price,
        "entry_price": price,
        "entry_time": now.strftime("%H:%M:%S"),
        "entry_date": today,
        "high":       price,
    }
    paper_daily_counts[count_key] = paper_daily_counts.get(count_key, 0) + 1

    trade = {
        "action": "BUY", "ticker": ticker, "shares": shares,
        "price": price, "cost": cost,
        "signal_score": sig["score"], "signal_detail": sig["detail"],
        "time": now.strftime("%H:%M:%S"), "date": today,
        "portfolio_value": paper_portfolio_value(),
    }
    paper_trades_today.append(trade)
    paper_all_trades.append(trade)
    if len(paper_all_trades) > 5000:
        paper_all_trades[:] = paper_all_trades[-4000:]

    msg = (
        f"BUY | {ticker} | {shares} shares @ ${price:.2f} | "
        f"Cost: ${cost:,.2f} | Signal: {sig['score']:.0f}/100 | "
        f"Detail: {sig['detail']} | "
        f"Portfolio: ${paper_portfolio_value():,.0f}"
    )
    paper_log(msg)

    # ── Enriched BUY notification ──────────────────────────────
    c            = sig["comps"]
    new_val      = paper_portfolio_value()
    lifetime_pct = (new_val - PAPER_STARTING_CAPITAL) / PAPER_STARTING_CAPITAL * 100
    tp_price     = price * (1 + PAPER_TAKE_PROFIT_PCT)
    sl_price     = price * (1 - PAPER_STOP_LOSS_PCT)

    # Readable signal summary
    sig_lines = []
    if c.get("rsi"):
        sig_lines.append(f"RSI {c['rsi']:.0f} ({c.get('rsi_pts',0)}pts)")
    if c.get("macd") is not None:
        sig_lines.append(f"MACD {c['macd']:+.4f} ({c.get('macd_pts',0)}pts)")
    if c.get("vol_ratio"):
        sig_lines.append(f"Vol {c['vol_ratio']:.1f}x avg ({c.get('vol_pts',0)}pts)")
    if c.get("grok_signal"):
        sig_lines.append(
            f"Grok {c['grok_signal']} conf={c.get('grok_confidence','?')} ({c.get('grok_pts',0)}pts)"
        )
    if c.get("ai_conviction"):
        sig_lines.append(
            f"AI Pick {c['ai_conviction']}/10 ({c.get('ai_category','')}) ({c['ai_conviction']}pts)"
        )

    # Multi-day context line for buy notification
    multi_day_line = ""
    daily = daily_candles.get(ticker)
    if daily and len(daily) >= 6:
        d_closes = [d["close"] for d in daily]
        ret_5d = (d_closes[-1] - d_closes[-6]) / d_closes[-6] * 100
        d_sma5 = sum(d_closes[-5:]) / 5
        multi_day_line = f"  5d: {ret_5d:+.1f}%  SMA5: ${d_sma5:.2f}\n"

    # AI thesis line for buy notification
    ai_thesis_line = ""
    ai_info = ai_watchlist_suggestions.get(ticker)
    if ai_info:
        ai_thesis_line = f"  AI: {ai_info['thesis']} (conviction {ai_info['conviction']}/10)\n"

    send_telegram(
        f"📈 PAPER BUY — {ticker}\n"
        f"{'─'*28}\n"
        f"Shares:    {shares} @ ${price:.2f}\n"
        f"Cost:      ${cost:,.0f}\n"
        f"Target:    ${tp_price:.2f} (+{PAPER_TAKE_PROFIT_PCT*100:.0f}%)\n"
        f"Stop:      ${sl_price:.2f} (-{PAPER_STOP_LOSS_PCT*100:.0f}%)\n"
        f"{'─'*28}\n"
        f"Signal:    {sig['score']:.0f}/100\n"
        + "\n".join(f"  | {l}" for l in sig_lines) + "\n"
        + (f"  | {c.get('grok_reason','')}\n" if c.get("grok_reason") else "")
        + multi_day_line
        + ai_thesis_line
        + f"{'─'*28}\n"
        f"Cash left: ${paper_cash:,.0f}\n"
        f"Positions: {len(paper_positions)}/{PAPER_MAX_POSITIONS}\n"
        f"Portfolio: ${new_val:,.0f}  ({lifetime_pct:+.2f}% all-time)\n"
        f"Trades today: {len(paper_trades_today)}"
    )
    save_paper_state()


def paper_scan():
    """
    Run paper trading evaluation for all monitored tickers.
    Plugged into check_stocks() cadence via scheduler.
    """
    if get_trading_session() not in ("regular", "extended"):
        return
    for ticker in list(TICKERS):
        # Skip tickers without enough history for meaningful signals
        hist = price_history.get(ticker, deque())
        if len(hist) < 10:
            continue
        try:
            paper_evaluate_ticker(ticker)
        except Exception as e:
            logger.error(f"paper_evaluate_ticker({ticker}): {e}")


def paper_morning_report():
    """Send portfolio snapshot at market open."""
    global paper_trades_today, paper_daily_counts
    paper_trades_today = []
    paper_daily_counts = {}

    val      = paper_portfolio_value()
    starting = PAPER_STARTING_CAPITAL
    total_pnl = val - starting
    total_pct = total_pnl / starting * 100

    lines = [
        f"PAPER PORTFOLIO — Market Open",
        f"{datetime.now(CT).strftime('%A %B %d, %Y')}",
        f"",
        f"Total Value:  ${val:>12,.2f}",
        f"Starting Cap: ${starting:>12,.2f}",
        f"All-Time P&L: ${total_pnl:>+12,.2f} ({total_pct:+.2f}%)",
        f"Cash:         ${paper_cash:>12,.2f}",
        f"Positions:    {len(paper_positions)}",
        f"",
    ]

    if paper_positions:
        lines.append("OPEN POSITIONS:")
        for ticker, pos in paper_positions.items():
            price, _, _ = fetch_finnhub_quote(ticker)
            price = price or pos["avg_cost"]
            mkt_val = pos["shares"] * price
            pnl     = (price - pos["avg_cost"]) / pos["avg_cost"] * 100
            arrow   = "+" if pnl >= 0 else "-"
            lines.append(
                f"  {arrow} {ticker:<6} {pos['shares']:>5} sh  "
                f"avg ${pos['avg_cost']:.2f} -> ${price:.2f}  "
                f"({pnl:+.1f}%)  ${mkt_val:,.0f}"
            )
    else:
        lines.append("No open positions — scanning for entries.")

    report = "\n".join(lines)
    paper_log(f"=== MORNING REPORT ===\n{report}")
    send_telegram(report)
    save_paper_state()   # persist the daily reset


def paper_eod_report():
    """Send end-of-day P&L report with all actions taken."""
    val       = paper_portfolio_value()
    starting  = PAPER_STARTING_CAPITAL
    total_pnl = val - starting
    total_pct = total_pnl / starting * 100

    buys  = [t for t in paper_trades_today if t["action"] == "BUY"]
    sells = [t for t in paper_trades_today if t["action"] == "SELL"]
    day_realized = sum(t.get("pnl", 0) for t in sells)

    lines = [
        f"PAPER PORTFOLIO — Market Close",
        f"{datetime.now(CT).strftime('%A %B %d, %Y')}",
        f"",
        f"Total Value:     ${val:>12,.2f}",
        f"All-Time P&L:    ${total_pnl:>+12,.2f} ({total_pct:+.2f}%)",
        f"Today Realized:  ${day_realized:>+12,.2f}",
        f"Cash:            ${paper_cash:>12,.2f}",
        f"",
        f"TODAY'S TRADES ({len(paper_trades_today)} total  "
        f"↑{len(buys)} buys  ↓{len(sells)} sells):",
    ]

    for t in paper_trades_today:
        if t["action"] == "BUY":
            lines.append(
                f"  ↑ {t['time']}  BUY  {t['ticker']:<6} "
                f"{t['shares']} sh @ ${t['price']:.2f}  "
                f"(${t['cost']:,.0f})  sig={t.get('signal_score','?'):.0f}"
            )
        else:
            pnl_str = f"${t['pnl']:+.2f} ({t['pnl_pct']:+.1f}%)"
            lines.append(
                f"  ↓ {t['time']}  SELL {t['ticker']:<6} "
                f"{t['shares']} sh @ ${t['price']:.2f}  "
                f"{pnl_str}  [{t['reason']}]"
            )

    lines.append("")
    lines.append("REMAINING POSITIONS:")
    if paper_positions:
        for ticker, pos in paper_positions.items():
            price, _, _ = fetch_finnhub_quote(ticker)
            price = price or pos["avg_cost"]
            mkt_val = pos["shares"] * price
            pnl     = (price - pos["avg_cost"]) / pos["avg_cost"] * 100
            arrow   = "+" if pnl >= 0 else "-"
            lines.append(
                f"  {arrow} {ticker:<6} {pos['shares']:>5} sh  "
                f"cost ${pos['avg_cost']:.2f}  now ${price:.2f}  "
                f"({pnl:+.1f}%)  ${mkt_val:,.0f}"
            )
    else:
        lines.append("  (all positions closed)")

    report = "\n".join(lines)
    paper_log(f"=== EOD REPORT ===\n{report}")
    send_telegram(report)
    save_paper_state()   # persist end-of-day snapshot


# ── Paper Trading Telegram Commands ───────────────────────────

async def cmd_paper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /paper                  — current portfolio snapshot
    /paper positions        — open positions with live P&L
    /paper trades           — today's trade log
    /paper history          — all-time trade summary
    /paper signal TICK      — show current signal breakdown for a ticker
    /paper log              — send investment.log as a file download
    /paper reset            — reset portfolio to $100k (with confirmation)
    """
    global paper_cash, paper_positions, paper_trades_today, paper_daily_counts
    global paper_all_trades, paper_signals_cache

    sub  = context.args[0].lower() if context.args else "portfolio"
    arg2 = context.args[1].upper() if len(context.args) > 1 else ""

    # ── /paper  or  /paper portfolio ─────────────────────────
    if sub in ("portfolio", "p"):
        val       = paper_portfolio_value()
        total_pnl = val - PAPER_STARTING_CAPITAL
        total_pct = total_pnl / PAPER_STARTING_CAPITAL * 100

        lines = [
            f"PAPER PORTFOLIO",
            f"",
            f"Value:    ${val:>12,.2f}",
            f"Start:    ${PAPER_STARTING_CAPITAL:>12,.2f}",
            f"P&L:      ${total_pnl:>+12,.2f} ({total_pct:+.2f}%)",
            f"Cash:     ${paper_cash:>12,.2f}",
            f"Invested: ${val - paper_cash:>12,.2f}",
            f"",
        ]
        if paper_positions:
            lines.append(f"POSITIONS ({len(paper_positions)}/{PAPER_MAX_POSITIONS}):")
            for ticker, pos in paper_positions.items():
                price, _, _ = fetch_finnhub_quote(ticker)
                price = price or pos["avg_cost"]
                mkt   = pos["shares"] * price
                pnl   = (price - pos["avg_cost"]) / pos["avg_cost"] * 100
                arrow = "+" if pnl >= 0 else "-"
                lines.append(
                    f"  {arrow} {ticker:<6} {pos['shares']} sh  "
                    f"${pos['avg_cost']:.2f}->${price:.2f}  "
                    f"{pnl:+.1f}%  ${mkt:,.0f}"
                )
        else:
            lines.append("No open positions.")

        lines += [
            f"",
            f"Trades today: {len(paper_trades_today)}  |  "
            f"Lifetime: {len(paper_all_trades)}",
            f"Use /paper trades | /paper signal TICK | /paper log",
        ]
        await update.message.reply_text("\n".join(lines))

    # ── /paper positions ─────────────────────────────────────
    elif sub == "positions":
        if not paper_positions:
            await update.message.reply_text("No open positions.")
            return
        lines = [f"OPEN POSITIONS — {datetime.now(CT).strftime('%H:%M CT')}"]
        for ticker, pos in paper_positions.items():
            price, _, _ = fetch_finnhub_quote(ticker)
            price = price or pos["avg_cost"]
            mkt   = pos["shares"] * price
            pnl   = (price - pos["avg_cost"]) / pos["avg_cost"] * 100
            unrealized = (price - pos["avg_cost"]) * pos["shares"]
            arrow = "+" if pnl >= 0 else "-"
            lines += [
                f"",
                f"{arrow} {ticker}",
                f"  Shares: {pos['shares']}  Entry: ${pos['avg_cost']:.2f}  Now: ${price:.2f}",
                f"  Unrealized: ${unrealized:+.2f} ({pnl:+.1f}%)",
                f"  Market value: ${mkt:,.2f}",
                f"  Entry: {pos['entry_date']} {pos['entry_time']}",
            ]
        await update.message.reply_text("\n".join(lines))

    # ── /paper trades ─────────────────────────────────────────
    elif sub == "trades":
        if not paper_trades_today:
            await update.message.reply_text("No trades today.")
            return
        buys   = [t for t in paper_trades_today if t["action"] == "BUY"]
        sells  = [t for t in paper_trades_today if t["action"] == "SELL"]
        real   = sum(t.get("pnl", 0) for t in sells)
        lines  = [
            f"TODAY'S TRADES",
            f"↑{len(buys)} buys  ↓{len(sells)} sells  "
            f"Realized: ${real:+.2f}",
            f"",
        ]
        for t in paper_trades_today:
            if t["action"] == "BUY":
                lines.append(
                    f"↑ {t['time']}  BUY  {t['ticker']} "
                    f"{t['shares']}sh @ ${t['price']:.2f}  "
                    f"sig={t.get('signal_score','?'):.0f}/100"
                )
            else:
                lines.append(
                    f"↓ {t['time']}  SELL {t['ticker']} "
                    f"{t['shares']}sh @ ${t['price']:.2f}  "
                    f"${t['pnl']:+.2f} ({t['pnl_pct']:+.1f}%)  "
                    f"[{t['reason']}]"
                )
        await update.message.reply_text("\n".join(lines))

    # ── /paper history ────────────────────────────────────────
    elif sub == "history":
        if not paper_all_trades:
            await update.message.reply_text("No trades on record yet.")
            return
        sells    = [t for t in paper_all_trades if t["action"] == "SELL"]
        buys     = [t for t in paper_all_trades if t["action"] == "BUY"]
        winners  = [t for t in sells if t.get("pnl", 0) > 0]
        losers   = [t for t in sells if t.get("pnl", 0) <= 0]
        total_pl = sum(t.get("pnl", 0) for t in sells)
        win_rate = len(winners) / len(sells) * 100 if sells else 0
        avg_win  = sum(t["pnl"] for t in winners) / len(winners) if winners else 0
        avg_loss = sum(t["pnl"] for t in losers) / len(losers) if losers else 0
        val      = paper_portfolio_value()

        lines = [
            f"PAPER TRADING — ALL-TIME SUMMARY",
            f"",
            f"Portfolio value: ${val:,.2f}",
            f"P&L: ${val - PAPER_STARTING_CAPITAL:+,.2f} "
            f"({(val - PAPER_STARTING_CAPITAL)/PAPER_STARTING_CAPITAL*100:+.2f}%)",
            f"",
            f"Total trades:  {len(paper_all_trades)}",
            f"  Buys:  {len(buys)}",
            f"  Sells: {len(sells)}",
            f"Win rate:      {win_rate:.1f}%",
            f"Avg winner:    ${avg_win:+.2f}",
            f"Avg loser:     ${avg_loss:+.2f}",
            f"Total realized:${total_pl:+.2f}",
        ]

        # Top 5 best trades
        if winners:
            lines += ["", "TOP TRADES:"]
            for t in sorted(winners, key=lambda x: x["pnl"], reverse=True)[:5]:
                lines.append(
                    f"  {t['ticker']} {t['date']}  "
                    f"+${t['pnl']:.2f} ({t['pnl_pct']:+.1f}%)"
                )
        await update.message.reply_text("\n".join(lines))

    # ── /paper signal TICK ────────────────────────────────────
    elif sub == "signal":
        if not arg2:
            await update.message.reply_text("Usage: /paper signal TICKER  (e.g. /paper signal NVDA)")
            return
        await update.message.reply_text(f"Computing signal for {arg2}...")
        # Force refresh by clearing cache
        paper_signals_cache.pop(arg2, None)
        sig = compute_paper_signal(arg2)
        price, _, _ = fetch_finnhub_quote(arg2)
        verdict = "BUY" if sig["score"] >= PAPER_MIN_SIGNAL else \
                  "WATCH" if sig["score"] >= 50 else "AVOID"
        c = sig["comps"]
        lines = [
            f"SIGNAL: {arg2}  @${price:.2f}" if price else f"SIGNAL: {arg2}",
            f"",
            f"Composite Score: {sig['score']:.0f}/125  -> {verdict}",
            f"",
            f"BREAKDOWN:",
            f"  RSI Momentum    {c.get('rsi','N/A')} -> {c.get('rsi_pts',0)} pts",
            f"  BB Position     %B={c.get('pct_b','N/A')} -> {c.get('bw_pts',0)} pts",
            f"  MACD            {c.get('macd','N/A')} -> {c.get('macd_pts',0)} pts",
            f"  Volume Ratio    {c.get('vol_ratio','N/A')}x -> {c.get('vol_pts',0)} pts",
            f"  Squeeze Score   {c.get('squeeze','N/A')}/100 -> {c.get('sq_pts',0)} pts",
            f"  Price Slope     {c.get('slope_pct','N/A')}%/tick -> {c.get('slope_pts',0)} pts",
            f"  Grok AI         {c.get('grok_signal','N/A')} "
            f"conf={c.get('grok_confidence','?')} -> {c.get('grok_pts',0)} pts",
            f"  AI Conviction   {c.get('ai_conviction','N/A')}/10 "
            f"({c.get('ai_category','')}) -> {c.get('ai_conviction',0) if c.get('ai_conviction') else 0} pts"
            if c.get('ai_conviction') else
            f"  AI Conviction   N/A",
        ]
        # Multi-day trend breakdown
        if c.get("sma5") is not None:
            lines.append(f"  Multi-Day Trend:")
            lines.append(f"    SMA5=${c['sma5']:.2f}  SMA20=${c.get('sma20','N/A')} -> {c.get('sma_pts',0)} pts")
            if c.get("ret_5d") is not None:
                lines.append(f"    5d Return: {c['ret_5d']:+.2f}% -> {c.get('mom_pts',0)} pts")
            if c.get("daily_vol_ratio") is not None:
                lines.append(f"    Daily Vol: {c['daily_vol_ratio']:.2f}x avg -> {c.get('vol_d_pts',0)} pts")
        else:
            lines.append(f"  Multi-Day Trend  N/A (no daily candles)")
        lines += [
            f"",
            f"Grok: {c.get('grok_reason', '')}",
        ]
        # Add AI thesis if available
        ai_sig_info = ai_watchlist_suggestions.get(arg2)
        if ai_sig_info:
            lines.append(f"AI: {ai_sig_info['thesis']} (conviction {ai_sig_info['conviction']}/10)")
        lines += [
            f"",
            f"Action threshold: {PAPER_MIN_SIGNAL}/100  "
            f"Daily actions today: "
            + str(paper_daily_counts.get(f'{arg2}:{datetime.now(CT).strftime("%Y-%m-%d")}', 0))
            + f"/{PAPER_MAX_ACTIONS}",
        ]
        await update.message.reply_text("\n".join(lines))

    # ── /paper log ────────────────────────────────────────────
    elif sub == "log":
        sent = False
        for path, fname in [
            (PAPER_LOG,        "investment.log"),
            (PAPER_STATE_FILE, "paper_state.json"),
        ]:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                with open(path, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename=fname,
                        caption=f"{fname} — Portfolio: ${paper_portfolio_value():,.0f}"
                    )
                sent = True
        if not sent:
            await update.message.reply_text("No log files found yet — no trades recorded.")

    # ── /paper reset ──────────────────────────────────────────
    elif sub == "reset":
        if arg2 == "CONFIRM":
            paper_cash          = PAPER_STARTING_CAPITAL
            paper_positions     = {}
            paper_trades_today  = []
            paper_daily_counts  = {}
            paper_all_trades    = []
            paper_signals_cache = {}
            paper_log("=== PORTFOLIO RESET TO $100,000 ===")
            save_paper_state()
            await update.message.reply_text(
                f"Portfolio reset to ${PAPER_STARTING_CAPITAL:,.0f}.\n"
                f"All positions and history cleared.\n"
                f"trade log preserved in investment.log."
            )
        else:
            await update.message.reply_text(
                f"This will reset the portfolio to ${PAPER_STARTING_CAPITAL:,.0f} "
                f"and clear all positions and history.\n\n"
                f"Type /paper reset CONFIRM to proceed."
            )

    else:
        await update.message.reply_text(
            "Paper Trading commands:\n"
            "  /paper               — portfolio snapshot\n"
            "  /paper positions     — open positions + live P&L\n"
            "  /paper trades        — today's actions\n"
            "  /paper history       — all-time performance\n"
            "  /paper signal TICK   — signal breakdown for any stock\n"
            "  /paper log           — download investment.log\n"
            "  /paper reset         — reset to $100,000"
        )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(BOT_DESCRIPTION)

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Monitoring {len(TICKERS)} stocks:\n" + "  ".join(sorted(TICKERS))
    )

async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not recent_alerts:
        await update.message.reply_text("No alerts fired yet today.")
        return
    await update.message.reply_text(
        f"Today's alerts ({daily_alerts} total):\n" +
        "\n".join(recent_alerts[-20:])
    )

async def cmd_overview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Indices + sectors + Fear & Greed in one shot."""
    note = _offhours_note()
    s = fetch_market_snapshot()
    fg_str = f"{s['fg_val']} - {s['fg_label']}" if s["fg_val"] else "unavailable"

    summary = s["indices_str"] + f" | F&G {s['fg_val']}"
    ai = get_ai_response(
        f"Market snapshot ({s['session']}): {summary}. "
        f"Sectors: {s['sector_str']}. "
        f"VIX: {s['vix']:.1f}. "
        f"{'This is last-close data. ' if note else ''}"
        f"2-sentence outlook + one sector to watch. Be specific to the numbers."
    )
    header = f"Market Overview{f'  {note}' if note else ''}"
    msg_lines = (
        [header, "", "Indices (ETF proxies):"] +
        s["indices_lines"] +
        [f"", f"Fear & Greed: {fg_str}",
         f"", "Sectors:"] +
        s["sector_lines"] +
        [f"", f"Claude: {ai}"]
    )
    await update.message.reply_text("\n".join(msg_lines))

async def cmd_spikes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not recent_alerts:
        await update.message.reply_text(
            "No spikes detected yet.\n"
            f"Monitoring {len(TICKERS)} stocks, threshold {THRESHOLD*100:.0f}%.\n"
            "Spikes trigger when a stock moves ≥3% within ~5 min."
        )
        return
    await update.message.reply_text(
        "Recent spikes:\n" + "\n".join(recent_alerts[-10:])
    )

async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /analyze TICKER (e.g. /analyze NVDA)")
        return
    ticker = context.args[0].upper()
    await update.message.reply_text(f"Analyzing {ticker}...")
    note = _offhours_note()
    try:
        d      = get_ticker_data(ticker)
        price  = d["price"]
        chg    = d["chg"]
        mcap   = d["mcap"] / 1e9
        vol    = d["volume"]
        high52 = d["high52"]
        low52  = d["low52"]
        pct_from_high = ((price - high52) / high52 * 100) if high52 else 0

        news_items = fetch_latest_news(ticker, 3)
        news_str   = "; ".join([h[:60] for h, _ in news_items]) if news_items else "no recent news"

        range_str = (f"52w High ${high52:.2f} ({pct_from_high:+.1f}% from high), 52w Low ${low52:.2f}"
                     if high52 and low52 else "52w range unavailable")

        prompt = (
            f"{'Last-close analysis' if note else 'Analysis'} of {ticker}: "
            f"Price ${price:.2f} ({chg:+.2f}%), "
            f"Mkt Cap ${mcap:.1f}B, Volume {vol:,}, "
            f"{range_str}. "
            f"Recent news: {news_str}. "
            f"{'Market is closed — focus on setup for next session. ' if note else ''}"
            f"Provide: (1) technical assessment (2) near-term catalyst (3) key risk. Be specific."
        )
        ai = get_ai_response(prompt, max_tokens=500)

        range_display = (f"${low52:.2f} - ${high52:.2f}" if high52 and low52 else "n/a")
        pct_display   = (f"{pct_from_high:+.1f}%" if high52 else "n/a")

        await update.message.reply_text(
            f"{ticker} Analysis{f'  {note}' if note else ''}\n"
            f"Price:        ${price:.2f} ({chg:+.2f}%)\n"
            f"Mkt Cap:      ${mcap:.1f}B\n"
            f"Volume:       {vol:,}\n"
            f"52w Range:    {range_display}\n"
            f"From 52w High: {pct_display}\n\n"
            f"Claude Analysis:\n{ai}"
        )
    except Exception as e:
        await update.message.reply_text(f"Unable to analyze {ticker}: {e}")

async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /price TICKER (e.g. /price AAPL)")
        return
    ticker = context.args[0].upper()
    note = _offhours_note()
    try:
        d       = get_ticker_data(ticker)
        price   = d["price"]
        chg     = d["chg"]
        vol     = d["volume"]
        high52  = d["high52"]
        low52   = d["low52"]
        # day high/low from Finnhub quote
        q      = _finnhub_quote(ticker)
        day_hi = q.get("h") or 0
        day_lo = q.get("l") or 0
        chg_abs = price * chg / 100
        arrow   = "+" if chg >= 0 else "-"

        day_range = f"${day_lo:.2f} - ${day_hi:.2f}" if day_hi and day_lo else "n/a"
        yr_range  = f"${low52:.2f} - ${high52:.2f}"   if high52 and low52  else "n/a"

        await update.message.reply_text(
            f"{arrow} {ticker}: ${price:.2f}{f'  {note}' if note else ''}\n"
            f"Change:    {chg_abs:+.2f} ({chg:+.2f}%)\n"
            f"Day range: {day_range}\n"
            f"52w range: {yr_range}\n"
            f"Volume:    {vol:,}"
        )
    except Exception as e:
        await update.message.reply_text(f"Could not fetch {ticker}: {e}")

async def cmd_compare(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /compare TICKER1 TICKER2 (e.g. /compare NVDA AMD)")
        return
    t1, t2 = context.args[0].upper(), context.args[1].upper()
    try:
        stats = {}
        for t in [t1, t2]:
            d = get_ticker_data(t)
            stats[t] = d

        def _r(val, prefix="$", suffix=""):
            return f"{prefix}{val:.2f}{suffix}" if val else "n/a"

        lines = [
            f"{'Metric':<16} {t1:>8} {t2:>8}",
            f"{'-'*34}",
            f"{'Price':<16} {_r(stats[t1]['price']):>8} {_r(stats[t2]['price']):>8}",
            f"{'Change %':<16} {stats[t1]['chg']:>+7.2f}% {stats[t2]['chg']:>+7.2f}%",
            f"{'Mkt Cap $B':<16} {stats[t1]['mcap']/1e9:>7.1f} {stats[t2]['mcap']/1e9:>7.1f}",
            f"{'52w High':<16} {_r(stats[t1]['high52']):>8} {_r(stats[t2]['high52']):>8}",
            f"{'52w Low':<16}  {_r(stats[t1]['low52']):>8} {_r(stats[t2]['low52']):>8}",
        ]
        summary = (
            f"{t1} ${stats[t1]['price']:.2f} ({stats[t1]['chg']:+.2f}%) "
            f"52w range ${stats[t1]['low52']:.2f}-${stats[t1]['high52']:.2f} vs "
            f"{t2} ${stats[t2]['price']:.2f} ({stats[t2]['chg']:+.2f}%) "
            f"52w range ${stats[t2]['low52']:.2f}-${stats[t2]['high52']:.2f}. "
            f"Which is the better buy right now and why? Be specific."
        )
        ai = get_ai_response(summary)
        await update.message.reply_text("\n".join(lines) + f"\n\nClaude: {ai}")
    except Exception as e:
        await update.message.reply_text(f"Compare failed: {e}")

async def cmd_movers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /movers — top gainers, losers, most active, low-price rockets.
    Strategy:
      1. Fetch symbol universe from FMP (gainers + losers + actives)
      2. Re-fetch LIVE prices from Finnhub for every symbol (rate-limited)
      3. Re-sort by actual live % change — not FMP's (possibly stale) ranking
      4. Falls back to FMP-only data if Finnhub rate-limited
      5. Falls back to yfinance if both FMP and Finnhub unavailable
    """
    await update.message.reply_text("Fetching live movers...")

    def _fmp_symbols_with_data(url):
        """Get symbol list WITH price data from FMP stable endpoint."""
        if not FMP_API_KEY:
            return [], []
        try:
            r = requests.get(f"{url}?apikey={FMP_API_KEY}", timeout=10)
            data = r.json()
            if isinstance(data, list) and data:
                syms = [item.get("symbol") for item in data[:40] if item.get("symbol")]
                # Also extract FMP's own price/change data as fallback
                fmp_items = []
                for item in data[:40]:
                    sym = item.get("symbol")
                    price = item.get("price", 0) or 0
                    chg = item.get("changesPercentage", 0) or 0
                    if sym and price > 0.10:
                        fmp_items.append({
                            "symbol": sym, "price": float(price),
                            "chg": float(chg), "volume": int(item.get("volume", 0) or 0),
                            "source": "FMP"
                        })
                return syms, fmp_items
        except Exception as e:
            logger.debug(f"FMP movers {url}: {e}")
        return [], []

    def _live_quote(sym):
        """Return live-verified dict for a symbol via Finnhub."""
        try:
            q = _finnhub_quote(sym)
            if not q:
                return None
            c  = float(q.get("c") or 0)
            pc = float(q.get("pc") or 0)
            price = c if c > 0 else pc
            if price < 0.10:
                return None
            vol = int(q.get("v") or 0)
            chg = (c - pc) / pc * 100 if c > 0 and pc > 0 else 0.0
            return {"symbol": sym, "price": price, "chg": chg, "volume": vol, "source": "Finnhub"}
        except Exception as e:
            logger.debug(f"Movers live quote {sym}: {e}")
            return None

    def _yf_quote(sym):
        """yfinance fallback for a single symbol."""
        try:
            fi = yf.Ticker(sym).fast_info
            price = fi.get("lastPrice") or fi.get("previousClose") or 0
            pc = fi.get("regularMarketPreviousClose") or fi.get("previousClose") or 0
            if not price or price < 0.10:
                return None
            chg = (price - pc) / pc * 100 if price and pc else 0
            vol = int(fi.get("lastVolume") or 0)
            return {"symbol": sym, "price": float(price), "chg": float(chg), "volume": vol, "source": "yfinance"}
        except Exception as e:
            logger.debug(f"yfinance movers {sym}: {e}")
            return None

    # ── Step 1: collect symbol universe from FMP (with fallback data) ─
    fmp_data_items = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_gain = pool.submit(_fmp_symbols_with_data, FMP_ENDPOINTS["gainers"])
        f_lose = pool.submit(_fmp_symbols_with_data, FMP_ENDPOINTS["losers"])
        f_act  = pool.submit(_fmp_symbols_with_data, FMP_ENDPOINTS["actives"])

    gain_syms, gain_data = f_gain.result()
    lose_syms, lose_data = f_lose.result()
    act_syms, act_data   = f_act.result()
    fmp_data_items = gain_data + lose_data + act_data

    all_syms = list(dict.fromkeys(gain_syms + lose_syms + act_syms))

    if not all_syms:
        logger.warning("FMP returned no symbols — falling back to TICKERS watchlist")
        all_syms = list(TICKERS)

    # Include TICKERS but cap total to avoid overwhelming the rate limiter
    all_syms = list(dict.fromkeys(all_syms + list(TICKERS)))[:80]

    # ── Step 2: live-verify via Finnhub (rate-limited, max 4 workers) ─
    with ThreadPoolExecutor(max_workers=4) as pool:
        live_results = list(pool.map(_live_quote, all_syms))

    live = [r for r in live_results if r is not None]

    # ── Step 2b: if Finnhub returned too few, use FMP data directly ─
    data_source = "Finnhub"
    if len(live) < 5 and fmp_data_items:
        logger.info(f"Finnhub returned only {len(live)} quotes — using FMP data as primary")
        # Merge: FMP data for symbols not already in live
        live_syms = {r["symbol"] for r in live}
        for item in fmp_data_items:
            if item["symbol"] not in live_syms:
                live.append(item)
                live_syms.add(item["symbol"])
        data_source = "FMP + Finnhub"

    # ── Step 2c: if still empty, try yfinance on core tickers ─
    if len(live) < 5:
        logger.info("Falling back to yfinance for movers data")
        yf_syms = list(TICKERS)[:30]
        with ThreadPoolExecutor(max_workers=3) as pool:
            yf_results = list(pool.map(_yf_quote, yf_syms))
        yf_live = [r for r in yf_results if r is not None]
        live_syms = {r["symbol"] for r in live}
        for item in yf_live:
            if item["symbol"] not in live_syms:
                live.append(item)
        data_source = "yfinance fallback"

    if not live:
        await update.message.reply_text(
            "Unable to fetch market data right now.\n"
            "All data sources (Finnhub, FMP, yfinance) returned no results.\n"
            "This usually means the market is closed or APIs are temporarily down."
        )
        return

    # ── Step 3: sort into categories by live data ─────────────────
    by_chg    = sorted(live, key=lambda x: x["chg"], reverse=True)
    gainers   = [x for x in by_chg if x["chg"] > 0][:10]
    losers    = [x for x in reversed(by_chg) if x["chg"] < 0][:10]
    actives   = sorted(live, key=lambda x: x["volume"], reverse=True)[:10]
    rockets   = [x for x in gainers if 1.0 <= x["price"] <= 15.0][:6]

    # ── Step 4: format ────────────────────────────────────────────
    def _row(item):
        sym   = item["symbol"]
        price = item["price"]
        chg   = item["chg"]
        sign  = "+" if chg >= 0 else ""
        return f"  {sym:<6} ${price:>8.2f}  {sign}{chg:.2f}%"

    def _vol_str(v):
        if v >= 1_000_000: return f"{v/1e6:.1f}M"
        if v >= 1_000:     return f"{v/1e3:.0f}K"
        return str(v)

    now_str = datetime.now(CT).strftime("%I:%M %p CT")

    lines = [f"Market Movers — {now_str}", f"({len(live)} stocks via {data_source})", ""]

    lines += ["TOP GAINERS", "  Ticker    Price      Chg%", "  " + "-"*28]
    lines += [_row(r) for r in gainers] or ["  (none)"]

    lines += ["", "TOP LOSERS", "  Ticker    Price      Chg%", "  " + "-"*28]
    lines += [_row(r) for r in losers] or ["  (none)"]

    lines += ["", "MOST ACTIVE (volume)", "  Ticker    Price      Chg%     Vol", "  " + "-"*36]
    for r in actives:
        sign = "+" if r["chg"] >= 0 else ""
        lines.append(f"  {r['symbol']:<6} ${r['price']:>8.2f}  {sign}{r['chg']:.2f}%  {_vol_str(r['volume'])}")

    if rockets:
        lines += ["", "LOW-PRICE ROCKETS ($1-$15)", "  Ticker    Price      Chg%", "  " + "-"*28]
        lines += [_row(r) for r in rockets]

    await update.message.reply_text("\n".join(lines))



async def cmd_earnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(CT).date()
    end = today + timedelta(days=7)
    today_str = today.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    earnings = []
    source = ""

    # ── 1. Primary: Finnhub earnings calendar ──────────────────
    if FINNHUB_TOKEN:
        try:
            if not _finnhub_limiter.acquire(timeout=5):
                logger.warning("Finnhub rate limit: skipping earnings calendar")
            else:
                r = requests.get(
                    f"https://finnhub.io/api/v1/calendar/earnings"
                    f"?from={today_str}&to={end_str}&token={FINNHUB_TOKEN}",
                    timeout=10
                )
                if r.status_code == 200:
                    raw = r.json()
                    cal = raw.get("earningsCalendar", []) if isinstance(raw, dict) else []
                    if cal:
                        total_count = len(cal)
                        # Prioritize well-known names from TICKERS
                        known = set(TICKERS) | set(CORE_TICKERS)
                        priority = [e for e in cal if e.get("symbol") in known]
                        others = [e for e in cal if e.get("symbol") not in known]
                        combined = priority + others
                        earnings = combined[:20]
                        source = f"Finnhub ({total_count} companies reporting)"
                        logger.info(f"Earnings: {len(earnings)} shown from {total_count} via Finnhub")
        except Exception as e:
            logger.warning(f"Finnhub earnings failed: {e}")

    # ── 2. Fallback: FMP stable earnings calendar ──────────────
    if not earnings and FMP_API_KEY:
        try:
            r = requests.get(
                f"https://financialmodelingprep.com/stable/earnings-calendar"
                f"?from={today_str}&to={end_str}&apikey={FMP_API_KEY}",
                timeout=10
            )
            data = r.json()
            if isinstance(data, list) and data:
                earnings = data[:20]
                source = "FMP"
                logger.info(f"Earnings: {len(earnings)} from FMP stable")
        except Exception as e:
            logger.warning(f"FMP earnings failed: {e}")

    # ── 3. Fallback: AI ────────────────────────────────────────
    if not earnings:
        now_label = datetime.now(CT).strftime("%A %B %d, %Y")
        ai = get_ai_response(
            f"Today is {now_label}. List the most notable US stock earnings "
            f"reports scheduled from {today_str} to {end_str}. "
            f"For each, show: symbol, date, before/after market, EPS estimate. "
            f"Focus on large-cap and well-known companies. Max 20 entries.",
            max_tokens=600
        )
        await update.message.reply_text(
            f"EARNINGS — Next 7 Days\n\n{ai}\n\n"
            f"Source: Claude AI"
        )
        return

    # ── Format grouped by date ─────────────────────────────────
    by_date = defaultdict(list)
    for item in earnings:
        sym = item.get("symbol", "")
        date_str = item.get("date", "")
        if not sym or not date_str:
            continue
        # Finnhub uses epsEstimate, FMP uses epsEstimated
        eps = item.get("epsEstimate") or item.get("epsEstimated") or ""
        hour = item.get("hour", "")
        if hour == "bmo":
            hour_label = "Before Open"
        elif hour == "amc":
            hour_label = "After Close"
        else:
            hour_label = ""
        by_date[date_str].append((sym, hour_label, eps))

    lines = ["EARNINGS — Next 7 Days", ""]
    for date_str in sorted(by_date.keys()):
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            day_label = dt.strftime("%b %d (%a)")
        except ValueError:
            day_label = date_str
        lines.append(day_label)
        for sym, hour_label, eps in by_date[date_str]:
            parts = [f"  {sym:<6}"]
            if hour_label:
                parts.append(f"{hour_label:<13}")
            if eps:
                try:
                    parts.append(f"EPS est ${float(eps):.2f}")
                except (ValueError, TypeError):
                    parts.append(f"EPS est {eps}")
            lines.append(" ".join(parts))
    lines.append(f"\nSource: {source}")

    await update.message.reply_text("\n".join(lines))

async def cmd_macro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Upcoming macro events — Claude AI primary source.
    (Finnhub economic calendar requires paid tier; FMP economic calendar
    returns 402 on this plan. Both removed to avoid wasted API calls.)
    """
    today = datetime.now(CT)
    today_str = today.strftime("%Y-%m-%d")
    now_label = today.strftime("%A %B %d, %Y")
    end = today + timedelta(days=14)
    end_str = end.strftime("%Y-%m-%d")

    await update.message.reply_text("Fetching macro calendar...")

    prompt = (
        f"Today is {now_label}. List the scheduled US macroeconomic "
        f"events from {today_str} to {end_str}. Include ONLY events "
        f"that are actually on the official economic calendar:\n"
        f"- FOMC meetings/minutes/rate decisions\n"
        f"- CPI, Core CPI\n"
        f"- PPI, Core PPI\n"
        f"- Nonfarm Payrolls (NFP)\n"
        f"- Unemployment Rate\n"
        f"- PCE Price Index\n"
        f"- GDP (advance/preliminary/final)\n"
        f"- Retail Sales\n"
        f"- ISM Manufacturing/Services PMI\n"
        f"- Consumer Confidence\n"
        f"- Durable Goods Orders\n"
        f"- Housing Starts/Existing Home Sales\n"
        f"- Initial Jobless Claims (weekly, Thursdays)\n\n"
        f"Format EXACTLY as:\n"
        f"DATE (Day)\n"
        f"  TIME EVENT [Impact]\n\n"
        f"Example:\n"
        f"Mar 14 (Fri)\n"
        f"  8:30am  CPI (Feb) [HIGH]\n"
        f"  10:00am Consumer Sentiment [MED]\n\n"
        f"Only include events you are confident about. "
        f"Mark impact as [HIGH] or [MED]. "
        f"If you're not sure about a date, skip it."
    )

    ai = get_ai_response(prompt, max_tokens=800)

    header = f"MACRO CALENDAR\n{now_label} — next 14 days\n"
    footer = "\nSource: Claude AI"
    await update.message.reply_text(header + "\n" + ai + footer)



async def cmd_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = get_crypto_prices()
    if not lines:
        await update.message.reply_text("Unable to fetch crypto prices.")
        return
    summary = " | ".join(lines[:3])
    ai = get_ai_response(f"Crypto snapshot: {summary}. One-sentence crypto market outlook.", fast=True)
    await update.message.reply_text(
        "Crypto Prices:\n" + "\n".join(lines) + f"\n\nClaude: {ai}"
    )

async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /news TICKER (e.g. /news NVDA)")
        return
    ticker     = context.args[0].upper()
    news_items = fetch_latest_news(ticker, 5)
    if not news_items:
        await update.message.reply_text(f"No recent news for {ticker}.")
        return
    lines = [f"Latest news for {ticker}:"]
    for headline, url in news_items:
        lines.append(f"• {headline[:100]}")
        if url:
            lines.append(f"  {url}")
    await update.message.reply_text("\n".join(lines))

async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    if not context.args:
        wl = user_watchlists.get(cid, [])
        if not wl:
            await update.message.reply_text(
                "Your watchlist is empty.\n"
                "Usage:\n"
                "/watchlist add TICKER\n"
                "/watchlist remove TICKER\n"
                "/watchlist show\n"
                "/watchlist scan  (spike-scan your list now)"
            )
            return
        await update.message.reply_text("Your watchlist:\n" + "  ".join(wl))
        return

    cmd = context.args[0].lower()
    if cmd == "show":
        wl = user_watchlists.get(cid, [])
        await update.message.reply_text(
            "Your watchlist:\n" + ("  ".join(wl) if wl else "(empty)")
        )
    elif cmd == "add" and len(context.args) > 1:
        ticker = context.args[1].upper()
        wl = user_watchlists.setdefault(cid, [])
        if ticker not in wl:
            wl.append(ticker)
        save_bot_state()
        await update.message.reply_text(f"Added {ticker} to your watchlist.")
    elif cmd == "remove" and len(context.args) > 1:
        ticker = context.args[1].upper()
        wl = user_watchlists.get(cid, [])
        if ticker in wl:
            wl.remove(ticker)
        save_bot_state()
        await update.message.reply_text(f"Removed {ticker} from watchlist.")
    elif cmd == "scan":
        wl = user_watchlists.get(cid, [])
        if not wl:
            await update.message.reply_text("Your watchlist is empty.")
            return
        lines = [f"Watchlist snapshot:"]
        for t in wl:
            try:
                d     = get_ticker_data(t)
                price = d["price"]
                chg   = d["chg"]
                sign  = "+" if chg >= 0 else ""
                lines.append(f"  {t}: ${price:.2f} ({sign}{chg:.2f}%)")
            except Exception as e:
                logger.debug(f"Watchlist show {t}: {e}")
                lines.append(f"  {t}: unavailable")
        await update.message.reply_text("\n".join(lines))
    else:
        await update.message.reply_text(
            "Usage:\n"
            "/watchlist add TICKER\n"
            "/watchlist remove TICKER\n"
            "/watchlist show\n"
            "/watchlist scan"
        )

async def cmd_setalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /setalert TICKER PRICE\n"
            "Example: /setalert NVDA 150.00\n\n"
            "You'll be notified when the stock is within 0.5% of your target.\n"
            "View active alerts: /myalerts\n"
            "Remove an alert: /delalert TICKER PRICE"
        )
        return
    ticker = context.args[0].upper()
    try:
        target = float(context.args[1])
    except (ValueError, IndexError):
        await update.message.reply_text("Invalid price. Example: /setalert NVDA 150.00")
        return
    if target <= 0:
        await update.message.reply_text("Price must be positive.")
        return
    existing = custom_price_alerts.get(ticker, [])
    # Prevent duplicate alerts (within 0.1% of an existing target)
    for ex_target in existing:
        if abs(ex_target - target) / max(target, 0.01) < 0.001:
            await update.message.reply_text(
                f"Alert already exists for {ticker} @ ${ex_target:.2f}"
            )
            return
    # Cap at 10 alerts per ticker
    if len(existing) >= 10:
        await update.message.reply_text(
            f"Max 10 alerts per ticker. Remove one first with /delalert {ticker} PRICE"
        )
        return
    custom_price_alerts.setdefault(ticker, []).append(target)
    save_bot_state()
    total_alerts = sum(len(v) for v in custom_price_alerts.values())
    await update.message.reply_text(
        f"Price alert set!\n{ticker} @ ${target:.2f}\n"
        f"You'll be alerted when within 0.5% of this target.\n"
        f"Active alerts: {total_alerts} total  |  /myalerts to view all"
    )

async def cmd_myalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all active custom price alerts."""
    if not custom_price_alerts:
        await update.message.reply_text(
            "No active price alerts.\n"
            "Set one with: /setalert TICKER PRICE"
        )
        return
    lines = ["Active Price Alerts:"]
    total = 0
    for ticker in sorted(custom_price_alerts.keys()):
        targets = custom_price_alerts[ticker]
        if not targets:
            continue
        # Get live price for context
        try:
            price, _, _ = fetch_finnhub_quote(ticker)
            price_str = f"  (now ${price:.2f})" if price else ""
        except Exception as e:
            logger.debug(f"myalerts price fetch {ticker}: {e}")
            price_str = ""
        for target in sorted(targets):
            direction = "above" if price and target > price else "below" if price and target < price else ""
            dist = ""
            if price and price > 0:
                dist_pct = abs(target - price) / price * 100
                dist = f"  [{dist_pct:.1f}% {direction}]" if direction else ""
            lines.append(f"  {ticker} @ ${target:.2f}{dist}")
            total += 1
    lines.append(f"\nTotal: {total} alerts")
    lines.append("Remove with: /delalert TICKER PRICE")
    lines.append("Remove all for ticker: /delalert TICKER all")
    await update.message.reply_text("\n".join(lines))


async def cmd_delalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete a custom price alert."""
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage:\n"
            "  /delalert TICKER PRICE   — remove specific alert\n"
            "  /delalert TICKER all     — remove all alerts for ticker\n"
            "Example: /delalert NVDA 150.00"
        )
        return
    ticker = context.args[0].upper()
    if ticker not in custom_price_alerts or not custom_price_alerts[ticker]:
        await update.message.reply_text(f"No active alerts for {ticker}.")
        return

    arg2 = context.args[1].lower()
    if arg2 == "all":
        count = len(custom_price_alerts[ticker])
        del custom_price_alerts[ticker]
        save_bot_state()
        await update.message.reply_text(f"Removed all {count} alerts for {ticker}.")
        return

    try:
        target = float(context.args[1])
    except (ValueError, IndexError):
        await update.message.reply_text("Invalid price. Example: /delalert NVDA 150.00")
        return

    # Find and remove the closest matching alert (within 0.5%)
    closest = None
    closest_dist = float('inf')
    for existing in custom_price_alerts[ticker]:
        dist = abs(existing - target)
        if dist < closest_dist:
            closest_dist = dist
            closest = existing

    if closest is not None and closest_dist / max(target, 0.01) < 0.005:
        custom_price_alerts[ticker].remove(closest)
        if not custom_price_alerts[ticker]:
            del custom_price_alerts[ticker]
        save_bot_state()
        await update.message.reply_text(
            f"Removed alert: {ticker} @ ${closest:.2f}\n"
            f"Use /myalerts to see remaining alerts."
        )
    else:
        targets_str = ", ".join(f"${t:.2f}" for t in sorted(custom_price_alerts[ticker]))
        await update.message.reply_text(
            f"No alert found for {ticker} @ ${target:.2f}.\n"
            f"Active alerts for {ticker}: {targets_str}"
        )


async def cmd_squeeze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show top squeeze candidates ranked by squeeze score."""
    if not squeeze_scores:
        await update.message.reply_text(
            "No squeeze data yet — scores build up after a few scan cycles.\n"
            "Try again in 2-3 minutes."
        )
        return

    # Sort descending by score, take top 10
    ranked = sorted(squeeze_scores.items(), key=lambda x: x[1], reverse=True)[:10]
    lines  = ["Top Squeeze Candidates (score 0-100):"]
    for ticker, score in ranked:
        sq = compute_squeeze_score(ticker)
        rsi_str  = f"RSI {sq['rsi']:.0f}" if sq['rsi'] is not None else "RSI n/a"
        bw_str   = f"BW {sq['bandwidth']:.3f}" if sq['bandwidth'] is not None else ""
        pb_str   = f"%B {sq['pct_b']:.2f}" if sq['pct_b'] is not None else ""
        bar      = "█" * int(score / 10) + "░" * (10 - int(score / 10))
        lines.append(f"{score:>5.1f} [{bar}] {ticker:<6} {rsi_str} {bw_str} {pb_str}")

    top_names = ", ".join([t for t, _ in ranked[:3]])
    ai = get_ai_response(
        f"These stocks have the highest squeeze scores right now: {top_names}. "
        f"Are any of them actual short-squeeze or momentum candidates? Be specific."
    )
    await update.message.reply_text("\n".join(lines) + f"\n\nClaude: {ai}")


async def cmd_rsi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show RSI + Bollinger Bands for a specific ticker."""
    if not context.args:
        await update.message.reply_text("Usage: /rsi TICKER (e.g. /rsi NVDA)")
        return
    ticker = context.args[0].upper()

    # Fetch 5-min candles via yfinance (Finnhub candles require paid plan)
    await update.message.reply_text(f"Calculating technicals for {ticker}...")
    try:
        tk = yf.Ticker(ticker)
        hist = tk.history(period="5d", interval="5m")
        if hist is None or hist.empty:
            await update.message.reply_text(f"No price history available for {ticker}.")
            return
        candles = []
        for idx, row in hist.iterrows():
            ts = idx.to_pydatetime()
            if ts.tzinfo:
                ts = ts.astimezone(CT)
            candles.append({
                "t": int(ts.timestamp()),
                "o": float(row["Open"]),
                "h": float(row["High"]),
                "l": float(row["Low"]),
                "c": float(row["Close"]),
                "v": int(row["Volume"]),
            })
        if not candles:
            await update.message.reply_text(f"No price history available for {ticker}.")
            return
        prices = [c["c"] for c in candles]
        price  = prices[-1]

        rsi             = compute_rsi(prices)
        mid, upper, lower, pct_b, bw = compute_bollinger(prices)

        rsi_label = (
            "Overbought" if rsi and rsi > 70 else
            "Oversold"   if rsi and rsi < 30 else
            "Neutral"
        )
        bb_label = (
            "Above upper band — extended" if pct_b and pct_b > 1 else
            "Below lower band — oversold" if pct_b and pct_b < 0 else
            "Upper half — bullish"        if pct_b and pct_b > 0.5 else
            "Lower half — building"
        )
        squeeze_label = "TIGHT SQUEEZE" if bw and bw < 0.04 else ("Moderate" if bw and bw < 0.08 else "Wide")

        sq    = compute_squeeze_score(ticker)
        score = sq.get("score", 0)

        ai = get_ai_response(
            f"{ticker} technicals: RSI={rsi} ({rsi_label}), "
            f"BB %B={pct_b} ({bb_label}), bandwidth={bw} ({squeeze_label}), "
            f"squeeze score={score}/100. "
            f"Current price ${price:.2f}. What's your read on momentum and next move?"
        )

        await update.message.reply_text(
            f"{ticker} Technicals (5-min candles)\n"
            f"Price: ${price:.2f}\n\n"
            f"RSI (14): {rsi if rsi else 'n/a'} — {rsi_label}\n\n"
            f"Bollinger Bands (20, 2σ):\n"
            f"  Upper: ${upper}\n"
            f"  Middle: ${mid}\n"
            f"  Lower: ${lower}\n"
            f"  %B: {pct_b} — {bb_label}\n"
            f"  Bandwidth: {bw} — {squeeze_label}\n\n"
            f"Squeeze Score: {score}/100\n\n"
            f"Claude: {ai}"
        )
    except Exception as e:
        await update.message.reply_text(f"Error computing technicals for {ticker}: {e}")



# ============================================================
# CHART — intraday sparkline with volume bars
# ============================================================

def build_chart_image(ticker: str) -> BytesIO:
    BG = "#0d1117"; PANEL = "#161b22"; TEXT = "#e6edf3"
    DIM = "#8b949e"; GREEN = "#2ecc71"; RED = "#e74c3c"; GOLD = "#f0b429"

    try:
        tk = yf.Ticker(ticker)
        hist = tk.history(period="1d", interval="5m")
        if hist is None or hist.empty:
            raise ValueError(f"No intraday data for {ticker}")
        candles = []
        for idx, row in hist.iterrows():
            ts = idx.to_pydatetime()
            if ts.tzinfo:
                ts = ts.astimezone(CT)
            candles.append({
                "t": int(ts.timestamp()),
                "o": float(row["Open"]),
                "h": float(row["High"]),
                "l": float(row["Low"]),
                "c": float(row["Close"]),
                "v": int(row["Volume"]),
            })
        if not candles:
            raise ValueError(f"No intraday data for {ticker}")
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"No intraday data for {ticker}: {e}")

    prices  = [c["c"] for c in candles]
    volumes = [c["v"] for c in candles]
    times   = [datetime.fromtimestamp(c["t"], tz=CT).strftime("%H:%M") for c in candles]
    open_p  = prices[0]
    color   = GREEN if prices[-1] >= open_p else RED

    # Tick labels — show every ~60 min
    n = len(times)
    step = max(1, n // 6)
    tick_pos    = list(range(0, n, step))
    tick_labels = [times[i] for i in tick_pos]

    fig, (ax_p, ax_v) = plt.subplots(
        2, 1, figsize=(12, 6),
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.08},
        facecolor=BG
    )
    fig.patch.set_facecolor(BG)

    # Price panel
    ax_p.set_facecolor(PANEL)
    xs = list(range(n))
    ax_p.plot(xs, prices, color=color, linewidth=1.8, zorder=3)
    ax_p.fill_between(xs, prices, min(prices), alpha=0.15, color=color, zorder=2)
    ax_p.axhline(open_p, color=DIM, linewidth=0.8, linestyle="--", zorder=1)

    # VWAP line — compute from candle OHLCV data
    highs_c  = [c["h"] for c in candles]
    lows_c   = [c["l"] for c in candles]
    closes_c = [c["c"] for c in candles]
    typical = [(h + l + c) / 3 for h, l, c in zip(highs_c, lows_c, closes_c)]
    cum_tp_vol = [tp * v for tp, v in zip(typical, volumes)]
    vwap = []
    cum_vol = 0; cum_tpv = 0
    for tpv, v in zip(cum_tp_vol, volumes):
        cum_tpv += tpv; cum_vol += v
        vwap.append(cum_tpv / cum_vol if cum_vol else 0)
    ax_p.plot(xs, vwap, color=GOLD, linewidth=1.0, linestyle="--", alpha=0.8, zorder=3, label="VWAP")
    ax_p.legend(loc="upper left", fontsize=7, facecolor=PANEL, labelcolor=GOLD, framealpha=0.7)

    chg     = ((prices[-1] - open_p) / open_p * 100) if open_p else 0
    chg_str = f"{chg:+.2f}%"
    ax_p.set_title(
        f"{ticker}  ${prices[-1]:.2f}  {chg_str}  (5-min intraday)",
        color=TEXT, fontsize=12, fontweight="bold", loc="left", pad=8
    )
    ax_p.set_xticks(tick_pos)
    ax_p.set_xticklabels(tick_labels, color=DIM, fontsize=7)
    ax_p.tick_params(axis="y", colors=TEXT, labelsize=8)
    for spine in ax_p.spines.values():
        spine.set_edgecolor("#30363d")
    ax_p.xaxis.grid(True, color="#21262d", linewidth=0.5)
    ax_p.yaxis.grid(True, color="#21262d", linewidth=0.5)
    ax_p.set_axisbelow(True)

    # Volume panel
    ax_v.set_facecolor(PANEL)
    bar_colors = [GREEN if p >= open_p else RED for p in prices]
    ax_v.bar(xs, volumes, color=bar_colors, alpha=0.7, width=0.8, zorder=2)
    ax_v.set_xticks(tick_pos)
    ax_v.set_xticklabels(tick_labels, color=DIM, fontsize=7)
    ax_v.tick_params(axis="y", colors=DIM, labelsize=6)
    ax_v.set_ylabel("Vol", color=DIM, fontsize=7)
    for spine in ax_v.spines.values():
        spine.set_edgecolor("#30363d")
    ax_v.yaxis.grid(True, color="#21262d", linewidth=0.4)
    ax_v.set_axisbelow(True)

    plt.tight_layout(pad=0.5)
    buf = BytesIO()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor=BG, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf


async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send intraday sparkline + volume chart for a ticker."""
    if not context.args:
        await update.message.reply_text("Usage: /chart TICKER  (e.g. /chart NVDA)")
        return
    ticker = context.args[0].upper()
    await update.message.reply_text(f"Fetching intraday chart for {ticker}...")
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        buf  = await loop.run_in_executor(None, build_chart_image, ticker)

        # Quick read on the chart shape
        d       = get_ticker_data(ticker)
        price   = d["price"]
        chg     = d["chg"]
        sq      = compute_squeeze_score(ticker)
        rsi_str = f"RSI {sq['rsi']:.0f}" if sq.get('rsi') else ""
        sign    = "+" if chg >= 0 else ""
        ai = get_ai_response(
            f"{ticker} intraday: ${price:.2f} ({sign}{chg:.2f}% today). "
            f"{rsi_str}. What does this intraday move suggest? One sentence.",
            fast=True
        )
        await update.message.reply_photo(
            photo=buf,
            caption=f"{ticker}  ${price:.2f}  ({sign}{chg:.2f}%)\n{rsi_str}\nClaude: {ai}"
        )
    except Exception as e:
        logger.error(f"Chart error for {ticker}: {e}")
        await update.message.reply_text(f"Could not generate chart for {ticker}: {e}")


# ============================================================
# DASHBOARD — visual market snapshot image
# ============================================================

def _clamp_color(val, lo, hi):
    """Map val in [lo,hi] to 0-1 for a red-white-green colormap."""
    span = hi - lo
    if span == 0:
        return 0.5
    return max(0.0, min(1.0, (val - lo) / span))

def _rg_cmap():
    return LinearSegmentedColormap.from_list(
        "rg", ["#e74c3c", "#f5f5f5", "#2ecc71"]
    )

def _bar_color(val):
    """Green for positive, red for negative."""
    return "#2ecc71" if val >= 0 else "#e74c3c"

def build_dashboard_image() -> BytesIO:
    import numpy as np

    BG    = "#0d1117"
    PANEL = "#161b22"
    TEXT  = "#e6edf3"
    DIM   = "#8b949e"
    GREEN = "#2ecc71"
    RED   = "#e74c3c"
    GOLD  = "#f0b429"
    BLUE  = "#58a6ff"
    GRID  = "#21262d"
    EDGE  = "#30363d"

    now_str = datetime.now(CT).strftime("%a %b %d %Y  %I:%M %p CT")
    session = get_trading_session()
    session_color = GREEN if session == "regular" else GOLD if session == "extended" else RED

    # ── Fetch all data concurrently using Finnhub as primary ─────
    INDEX_SYMS = [("SPY","S&P 500"),("QQQ","Nasdaq"),
                  ("DIA","Dow"),("IWM","Russell"),("VXX","VIX ETF")]
    SECTOR_SYMS = [("XLK","Tech"),("XLF","Fin"),("XLE","Energy"),
                   ("XLV","Health"),("XLI","Indust"),("XLC","Comm"),
                   ("XLY","Cons D"),("XLP","Cons S"),("XLB","Mat"),
                   ("XLRE","RE"),("XLU","Util")]
    CRYPTO_SYMS = [
        ("BINANCE:BTCUSDT","BTC"),
        ("BINANCE:ETHUSDT","ETH"),
        ("BINANCE:SOLUSDT","SOL"),
        ("BINANCE:DOGEUSDT","DOGE"),
        ("BINANCE:XRPUSDT","XRP"),
    ]

    def _fq(sym):
        """Finnhub -> (price, chg%). Off-hours returns prev close as price with 0% chg."""
        q = _finnhub_quote(sym)
        if q:
            c  = q.get("c") or 0
            pc = q.get("pc") or 0
            price = c if c > 0 else pc
            chg   = (c - pc) / pc * 100 if c > 0 and pc > 0 else 0
            if price:
                return price, chg
        try:
            fi    = yf.Ticker(sym).fast_info
            price = fi.get("lastPrice") or fi.get("previousClose") or 0
            pc    = fi.get("regularMarketPreviousClose") or fi.get("previousClose") or 0
            chg   = (price - pc) / pc * 100 if price and pc else 0
            return price, chg
        except Exception as e:
            logger.debug(f"Dashboard _fq yfinance fallback {sym}: {e}")
            return 0, 0

    def _fetch_indices():
        out = []
        for sym, name in INDEX_SYMS:
            price, chg = _fq(sym)
            out.append((name, price, chg))
        return out

    def _fetch_sectors():
        out = []
        for sym, name in SECTOR_SYMS:
            _, chg = _fq(sym)
            out.append((name, round(chg, 2)))
        return out

    def _fetch_movers():
        # Build a broader scan pool: TICKERS + FMP actives
        scan_pool = list(dict.fromkeys(list(TICKERS)))
        if len(scan_pool) < 20 and FMP_API_KEY:
            # Supplement with FMP actives if watchlist is thin
            try:
                r = requests.get(
                    f"{FMP_ENDPOINTS['actives']}?apikey={FMP_API_KEY}",
                    timeout=8
                )
                fmp_data = r.json()
                if isinstance(fmp_data, list):
                    scan_pool += [d.get("symbol") for d in fmp_data[:30] if d.get("symbol")]
                    scan_pool = list(dict.fromkeys(scan_pool))
            except Exception as e:
                logger.debug(f"Dashboard FMP actives: {e}")

        items = []
        def _quote_one(t):
            q = _finnhub_quote(t)
            if not q:
                return None
            c  = q.get("c") or 0
            pc = q.get("pc") or 0
            price = c if c > 0 else pc
            if not price:
                return None
            chg = (c - pc) / pc * 100 if c > 0 and pc > 0 else 0
            return (t, chg, price)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(_quote_one, scan_pool[:30]))

        items = [r for r in results if r]
        items.sort(key=lambda x: x[1])
        return items[-5:][::-1], items[:5]   # gainers, losers

    def _fetch_crypto():
        out = []
        for fsym, name in CRYPTO_SYMS:
            try:
                q = _finnhub_quote(fsym)
                if q:
                    price = q.get("c") or 0
                    pc = q.get("pc") or 0
                    if price > 0 and pc > 0:
                        chg = (price - pc) / pc * 100
                        out.append((name, price, chg))
            except Exception as e:
                logger.debug(f"Dashboard crypto {name}: {e}")
        return out


    with ThreadPoolExecutor(max_workers=5) as pool:
        f_idx = pool.submit(_fetch_indices)
        f_sec = pool.submit(_fetch_sectors)
        f_mov = pool.submit(_fetch_movers)
        f_cry = pool.submit(_fetch_crypto)
        f_fg  = pool.submit(get_fear_greed)

    try:
        indices = f_idx.result()
    except Exception as e:
        logger.error(f"Dashboard indices fetch failed: {e}")
        indices = [(n, 0, 0) for _, n in INDEX_SYMS]

    try:
        sectors = f_sec.result()
    except Exception as e:
        logger.error(f"Dashboard sectors fetch failed: {e}")
        sectors = [(n, 0) for _, n in SECTOR_SYMS]

    try:
        gainers, losers = f_mov.result()
    except Exception as e:
        logger.error(f"Dashboard movers fetch failed: {e}")
        gainers, losers = [], []

    try:
        crypto = f_cry.result()
    except Exception as e:
        logger.error(f"Dashboard crypto fetch failed: {e}")
        crypto = []

    try:
        fg_val, fg_label = f_fg.result()
    except Exception as e:
        logger.error(f"Dashboard fear&greed fetch failed: {e}")
        fg_val, fg_label = 50, "N/A"
    fg_val = int(fg_val) if fg_val else 50

    top_squeeze = sorted(squeeze_scores.items(), key=lambda x: x[1], reverse=True)[:5]
    # If squeeze_scores empty (first run), compute on-demand from CORE_TICKERS
    if not top_squeeze:
        def _seed_sq(t):
            try:
                sq = compute_squeeze_score(t)
                if sq.get("score") is not None:
                    return (t, sq["score"])
            except Exception as e:
                logger.debug(f"Squeeze seed {t}: {e}")
            return None
        with ThreadPoolExecutor(max_workers=3) as pool:
            seed_results = list(pool.map(_seed_sq, CORE_TICKERS[:10]))
        seeded = [r for r in seed_results if r]
        top_squeeze = sorted(seeded, key=lambda x: x[1], reverse=True)[:5]

    idx_summary = "  ".join(f"{n} {c:+.1f}%" for n, _, c in indices[:4])
    grok_line = get_ai_response(
        f"Market now: {idx_summary}. Fear&Greed={fg_val}({fg_label}). "
        f"One sentence market call.",
        max_tokens=80, fast=True
    )

    # ── Helpers ───────────────────────────────────────────────
    def _setup_panel(ax, title):
        ax.set_facecolor(PANEL)
        for sp in ax.spines.values():
            sp.set_edgecolor(EDGE)
            sp.set_linewidth(0.8)
        ax.set_title(title, color=DIM, fontsize=10,
                     fontweight="bold", loc="left", pad=5)

    def _barh_chart(ax, names, values, bar_colors, *, price_strs=None):
        """
        Draw a clean horizontal bar chart.
        - Labels placed at a FIXED right-edge position (axes coords) so they
          never collide with the y-axis or each other regardless of value.
        - X axis always has a minimum range so bars are visible near-zero.
        """
        ys = list(range(len(names)))
        ax.barh(ys, values, color=bar_colors, height=0.55, zorder=3)
        ax.set_yticks(ys)
        ax.set_yticklabels(names, color=TEXT, fontsize=10)
        ax.axvline(0, color=DIM, linewidth=0.7, zorder=2)
        ax.xaxis.grid(True, color=GRID, linewidth=0.5, zorder=1)
        ax.set_axisbelow(True)
        ax.tick_params(axis="x", colors=DIM, labelsize=7)
        ax.tick_params(axis="y", length=0)

        # Ensure a sensible x range so tiny/zero bars are still visible
        abs_max = max((abs(v) for v in values), default=0.1)
        pad     = max(abs_max * 0.15, 0.05)
        ax.set_xlim(-abs_max - pad, abs_max + pad)

        # Pct label: just right of bar end, min distance from zero line
        for i, (v, name) in enumerate(zip(values, names)):
            pct_label = f"{v:+.2f}%"
            # Place slightly beyond bar end; flip side if negative
            x_offset = pad * 0.4
            if v >= 0:
                ax.text(v + x_offset, i, pct_label,
                        va="center", ha="left", color=TEXT, fontsize=8,
                        clip_on=False)
            else:
                ax.text(v - x_offset, i, pct_label,
                        va="center", ha="right", color=TEXT, fontsize=8,
                        clip_on=False)

        # Price label: always at fixed right edge in axes coords
        if price_strs:
            for i, ps in enumerate(price_strs):
                ax.text(1.01, (i + 0.5) / len(names),
                        ps, va="center", ha="left",
                        color=DIM, fontsize=9,
                        transform=ax.transAxes, clip_on=False)

    # ── Figure & grid (portrait/mobile layout) ──────────────
    fig = plt.figure(figsize=(14, 24), facecolor=BG)
    fig.patch.set_facecolor(BG)
    gs = gridspec.GridSpec(
        8, 2, figure=fig,
        hspace=0.45, wspace=0.35,
        top=0.96, bottom=0.02,
        left=0.08, right=0.95
    )

    # ── Header ────────────────────────────────────────────────
    fig.text(0.08, 0.982, "STOCK SPIKE MONITOR  //  LIVE DASHBOARD",
             color=TEXT, fontsize=14, fontweight="bold")
    fig.text(0.08, 0.974, now_str, color=DIM, fontsize=10)
    fig.text(0.55, 0.974,
             f"Market: {session.upper()}",
             color=session_color, fontsize=10, fontweight="bold")
    # Claude AI one-liner — truncate to ~80 chars for narrow layout
    gl = grok_line[:80] + ("\u2026" if len(grok_line) > 80 else "")
    fig.text(0.08, 0.966, f"Claude AI: {gl}",
             color=GOLD, fontsize=9, style="italic")

    # ── [A] Indices (full width) ──────────────────────────────
    ax_idx = fig.add_subplot(gs[0, :])
    _setup_panel(ax_idx, "MAJOR INDICES  (% change)")
    i_names  = [n for n, _, _ in indices]
    i_chgs   = [c for _, _, c in indices]
    i_prices = [f"${p:,.2f}" if p < 10000 else f"${p:,.0f}"
                for _, p, _ in indices]
    _barh_chart(ax_idx, i_names, i_chgs,
                [_bar_color(c) for c in i_chgs],
                price_strs=i_prices)

    # ── [B] Fear & Greed gauge ────────────────────────────────
    ax_fg = fig.add_subplot(gs[1, 0])
    ax_fg.set_facecolor(PANEL)
    for sp in ax_fg.spines.values():
        sp.set_edgecolor(EDGE)
    ax_fg.set_title("FEAR & GREED", color=DIM, fontsize=10,
                    fontweight="bold", loc="left", pad=5)
    ax_fg.set_aspect("equal")
    ax_fg.set_xlim(-1.3, 1.3)
    ax_fg.set_ylim(-0.35, 1.3)
    ax_fg.axis("off")

    seg_colors = ["#c0392b","#e74c3c","#e67e22","#f1c40f","#2ecc71","#27ae60"]
    seg_labels = ["Ext\nFear","Fear","Neutral","Greed","Ext\nGreed",""]
    for i, (sc, sl) in enumerate(zip(seg_colors, seg_labels)):
        t1  = 180 - i * 30
        t2  = 180 - (i + 1) * 30
        th  = np.linspace(np.radians(t2), np.radians(t1), 50)
        xo, yo = np.cos(th), np.sin(th)
        xi, yi = 0.65 * np.cos(th), 0.65 * np.sin(th)
        ax_fg.fill(np.concatenate([xo, xi[::-1]]),
                   np.concatenate([yo, yi[::-1]]),
                   color=sc, alpha=0.85, zorder=2)
        if sl:
            mt = np.radians((t1 + t2) / 2)
            ax_fg.text(0.82 * np.cos(mt), 0.82 * np.sin(mt), sl,
                       ha="center", va="center", fontsize=5.5,
                       color="white", fontweight="bold", zorder=3)

    na = np.radians(180 - fg_val * 1.8)
    ax_fg.annotate("",
        xy=(0.6 * np.cos(na), 0.6 * np.sin(na)), xytext=(0, 0),
        arrowprops=dict(arrowstyle="->,head_width=0.08,head_length=0.05",
                        color="white", lw=2), zorder=5)
    ax_fg.add_patch(plt.Circle((0, 0), 0.07, color=PANEL, zorder=4))
    ax_fg.text(0, -0.18, str(fg_val), ha="center", va="center",
               fontsize=22, fontweight="bold", color=TEXT, zorder=5)
    ax_fg.text(0, -0.29, fg_label or "", ha="center", va="center",
               fontsize=8, color=GOLD, zorder=5)

    # ── [C] Sector heatmap ────────────────────────────────────
    ax_sec = fig.add_subplot(gs[1, 1])
    _setup_panel(ax_sec, "SECTOR HEATMAP")
    ax_sec.axis("off")
    ncols_s, nrows_s = 3, 4
    max_abs_s = max((abs(v) for _, v in sectors), default=1) or 1
    cmap = _rg_cmap()
    for idx, (name, val) in enumerate(sectors):
        row = idx // ncols_s
        col = idx % ncols_s
        x0  = col / ncols_s + 0.01
        y0  = 1 - (row + 1) / nrows_s + 0.01
        w   = 1 / ncols_s - 0.02
        h   = 1 / nrows_s - 0.025
        cx, cy = x0 + w / 2, y0 + h / 2
        norm_v = _clamp_color(val, -max_abs_s, max_abs_s)
        bg     = cmap(norm_v)
        ax_sec.add_patch(FancyBboxPatch(
            (x0, y0), w, h,
            boxstyle="round,pad=0.01", facecolor=bg,
            edgecolor=BG, linewidth=1.2,
            transform=ax_sec.transAxes
        ))
        tc = "white" if abs(norm_v - 0.5) > 0.18 else "#111111"
        ax_sec.text(cx, cy + 0.045, name, ha="center", va="center",
                    fontsize=7, fontweight="bold", color=tc,
                    transform=ax_sec.transAxes)
        ax_sec.text(cx, cy - 0.045, f"{val:+.2f}%", ha="center", va="center",
                    fontsize=6.5, color=tc, transform=ax_sec.transAxes)

    # ── [D] Top Gainers (full width) ──────────────────────────
    ax_gn = fig.add_subplot(gs[2, :])
    _setup_panel(ax_gn, "TOP GAINERS  (monitored list)")
    if gainers:
        _barh_chart(ax_gn,
                    [t for t, _, _ in gainers],
                    [c for _, c, _ in gainers],
                    [GREEN] * len(gainers),
                    price_strs=[f"${p:.2f}" for _, _, p in gainers])
    else:
        ax_gn.text(0.5, 0.5, "No data", ha="center", va="center",
                   color=DIM, fontsize=9, transform=ax_gn.transAxes)
        ax_gn.axis("off")

    # ── [E] Top Losers (full width) ───────────────────────────
    ax_ls = fig.add_subplot(gs[3, :])
    _setup_panel(ax_ls, "TOP LOSERS  (monitored list)")
    if losers:
        _barh_chart(ax_ls,
                    [t for t, _, _ in losers],
                    [c for _, c, _ in losers],
                    [RED] * len(losers),
                    price_strs=[f"${p:.2f}" for _, _, p in losers])
    else:
        ax_ls.text(0.5, 0.5, "No data", ha="center", va="center",
                   color=DIM, fontsize=9, transform=ax_ls.transAxes)
        ax_ls.axis("off")

    # ── [F] Squeeze Leaderboard (full width) ──────────────────
    ax_sq = fig.add_subplot(gs[4, :])
    _setup_panel(ax_sq, "SQUEEZE LEADERBOARD  (score 0-100)")
    if top_squeeze:
        sq_names  = [t for t, _ in top_squeeze]
        sq_scores = [s for _, s in top_squeeze]
        ys_sq = list(range(len(sq_names)))
        sq_cols = [plt.cm.YlOrRd(s / 100) for s in sq_scores]
        ax_sq.barh(ys_sq, sq_scores, color=sq_cols, height=0.55, zorder=3)
        ax_sq.set_xlim(0, 115)
        ax_sq.set_yticks(ys_sq)
        ax_sq.set_yticklabels(sq_names, color=TEXT, fontsize=10)
        ax_sq.tick_params(axis="x", colors=DIM, labelsize=7)
        ax_sq.tick_params(axis="y", length=0)
        ax_sq.xaxis.grid(True, color=GRID, linewidth=0.5, zorder=1)
        ax_sq.set_axisbelow(True)
        for i, (t, sc) in enumerate(top_squeeze):
            sd    = compute_squeeze_score(t)
            parts = []
            if sd.get("rsi"):
                parts.append(f"RSI {sd['rsi']:.0f}")
            if sd.get("bandwidth"):
                parts.append(f"BW {sd['bandwidth']:.3f}")
            detail = "  ".join(parts)
            ax_sq.text(sc + 1.5, i, f"{sc:.0f}  {detail}",
                       va="center", ha="left", color=TEXT, fontsize=8)
    else:
        ax_sq.text(0.5, 0.5, "Building\u2026 (needs 2-3 scan cycles)",
                   ha="center", va="center", color=DIM, fontsize=9,
                   transform=ax_sq.transAxes)
        ax_sq.axis("off")

    # ── [G] Crypto (full width) ───────────────────────────────
    ax_cr = fig.add_subplot(gs[5, :])
    _setup_panel(ax_cr, "CRYPTO  (% change today)")
    if crypto:
        def _fmt_crypto_price(p):
            if p >= 10000:  return f"${p:,.0f}"
            if p >= 1:      return f"${p:,.2f}"
            return f"${p:.4f}"
        _barh_chart(ax_cr,
                    [c[0] for c in crypto],
                    [c[2] for c in crypto],
                    [_bar_color(c[2]) for c in crypto],
                    price_strs=[_fmt_crypto_price(c[1]) for c in crypto])
    else:
        ax_cr.text(0.5, 0.5, "No data", ha="center", va="center",
                   color=DIM, fontsize=9, transform=ax_cr.transAxes)
        ax_cr.axis("off")

    # ── [H] Recent Spike Alerts (full width) ──────────────────
    ax_al = fig.add_subplot(gs[6, :])
    ax_al.set_facecolor(PANEL)
    for sp in ax_al.spines.values():
        sp.set_edgecolor(EDGE)
    ax_al.set_title("RECENT SPIKE ALERTS", color=DIM, fontsize=10,
                    fontweight="bold", loc="left", pad=5)
    ax_al.axis("off")
    alerts_display = (recent_alerts[-12:] if recent_alerts
                      else ["No spikes yet today"])
    ncols_al = 2
    rows_al  = math.ceil(len(alerts_display) / ncols_al)
    row_h    = 1.0 / max(rows_al, 1)
    for idx, alert in enumerate(alerts_display):
        col = idx % ncols_al
        row = idx // ncols_al
        ax_al.text(col / ncols_al + 0.02,
                   0.92 - row * row_h * 0.85,
                   f"\u25b8 {alert}",
                   ha="left", va="top",
                   color=GOLD if "%" in alert else DIM,
                   fontsize=9, transform=ax_al.transAxes,
                   clip_on=True)

    # ── [I] Bot Status ────────────────────────────────────────
    ax_st = fig.add_subplot(gs[7, 0])
    ax_st.set_facecolor(PANEL)
    for sp in ax_st.spines.values():
        sp.set_edgecolor(EDGE)
    ax_st.set_title("BOT STATUS", color=DIM, fontsize=10,
                    fontweight="bold", loc="left", pad=5)
    ax_st.axis("off")
    status_str   = "[RUNNING]" if not monitoring_paused else "[PAUSED]"
    status_color = GREEN if not monitoring_paused else GOLD
    pv = paper_portfolio_value()
    paper_pnl = pv - PAPER_STARTING_CAPITAL
    stats = [
        (status_str,                                   status_color),
        (f"Watching: {len(TICKERS)} stocks",           TEXT),
        (f"Alerts today: {daily_alerts}",              GOLD if daily_alerts > 0 else DIM),
        (f"Threshold: {THRESHOLD*100:.0f}%  "
         f"Scan: {CHECK_INTERVAL_MIN}m",               DIM),
        (f"Session: {session.upper()}",                session_color),
        ("",                                           DIM),
        (f"Paper: ${pv:,.0f}",                        GREEN if paper_pnl >= 0 else RED),
        (f"  P&L: ${paper_pnl:+,.0f}",               GREEN if paper_pnl >= 0 else RED),
        (f"  Positions: {len(paper_positions)}",       DIM),
    ]
    for i, (line, color) in enumerate(stats):
        ax_st.text(0.05, 0.96 - i * 0.11, line,
                   ha="left", va="top", color=color,
                   fontsize=9.5, transform=ax_st.transAxes)

    # ── [J] AI Picks Summary (NEW panel) ──────────────────────
    ax_ai = fig.add_subplot(gs[7, 1])
    ax_ai.set_facecolor(PANEL)
    for sp in ax_ai.spines.values():
        sp.set_edgecolor(EDGE)
    ax_ai.set_title("AI PICKS", color=DIM, fontsize=10,
                    fontweight="bold", loc="left", pad=5)
    ax_ai.axis("off")

    if ai_watchlist_suggestions:
        top_ai = sorted(ai_watchlist_suggestions.items(),
                        key=lambda x: x[1].get("conviction", 0),
                        reverse=True)[:6]
        for i, (ticker, info) in enumerate(top_ai):
            conv = info.get("conviction", 0)
            cat = info.get("category", "")[:8]
            color = GREEN if conv >= 8 else GOLD if conv >= 6 else DIM
            ax_ai.text(0.05, 0.90 - i * 0.15,
                       f"{ticker} ({conv}/10) {cat}",
                       ha="left", va="top", color=color, fontsize=9,
                       transform=ax_ai.transAxes)
    else:
        ax_ai.text(0.5, 0.5, "No AI picks yet", ha="center", va="center",
                   color=DIM, fontsize=9, transform=ax_ai.transAxes)

    # ── Save ──────────────────────────────────────────────────
    buf = BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=BG, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf



async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate and send the full visual market dashboard."""
    await update.message.reply_text("Building dashboard… this takes ~10 seconds ⏳")
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        buf  = await loop.run_in_executor(None, build_dashboard_image)
        await update.message.reply_photo(
            photo=buf,
            caption=(
                f"📊 Live Dashboard — {datetime.now(CT).strftime('%I:%M %p CT')}\n"
                f"Indices • Sectors • Gainers/Losers • Squeeze • Crypto • Alerts"
            )
        )
    except Exception as e:
        logger.error(f"Dashboard error: {e}", exc_info=True)
        await update.message.reply_text(f"Dashboard error: {e}")


def send_dashboard_sync(label: str = ""):
    """
    Build the dashboard and push it to Telegram using the raw Bot API.
    Safe to call from any background thread or scheduler (no async needed).
    """
    try:
        buf = build_dashboard_image()
        caption = f"📊 Dashboard — {label}  {datetime.now(CT).strftime('%I:%M %p CT')}"
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            data={"chat_id": CHAT_ID, "caption": caption},
            files={"photo": ("dashboard.png", buf, "image/png")},
            timeout=30
        )
        if not resp.ok:
            logger.error(f"Dashboard send failed ({label}): {resp.text}")
        else:
            logger.info(f"Dashboard sent: {label}")
    except Exception as e:
        logger.error(f"send_dashboard_sync error ({label}): {e}", exc_info=True)


async def cmd_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /monitoring pause | resume | status
    """
    global monitoring_paused
    mode = context.args[0].lower() if context.args else "status"

    if mode == "pause":
        monitoring_paused = True
        save_bot_state()
        await update.message.reply_text(
            "Monitoring PAUSED.\n"
            "Spike scanning stopped. Dashboards and scheduled messages continue.\n"
            "Resume with: /monitoring resume"
        )
    elif mode == "resume":
        monitoring_paused = False
        save_bot_state()
        await update.message.reply_text(
            "Monitoring RESUMED.\n"
            f"Scanning {len(TICKERS)} stocks every {CHECK_INTERVAL_MIN} min."
        )
    else:
        session = get_trading_session()
        state   = "PAUSED" if monitoring_paused else "RUNNING"
        await update.message.reply_text(
            f"Monitoring: {state}\n"
            f"Session: {session.upper()}\n"
            f"Stocks watched: {len(TICKERS)}\n"
            f"Alerts today: {daily_alerts}\n"
            f"Threshold: {THRESHOLD*100:.0f}% | Cooldown: {COOLDOWN_MINUTES} min\n\n"
            f"Commands: /monitoring pause  |  /monitoring resume"
        )


# ============================================================
# NATURAL LANGUAGE HANDLER — the "ask anything" feature
# ============================================================
def _offhours_note() -> str:
    """Returns a contextual note string when market is closed, empty string otherwise."""
    session = get_trading_session()
    if session != "closed":
        return ""
    now = datetime.now(CT)
    if now.weekday() >= 5:
        days_to_open = 7 - now.weekday()   # Mon = 0
        return f"(Weekend — market reopens Monday)"
    t = now.time()
    if t < datetime.strptime("07:00", "%H:%M").time():
        return "(Pre-market — last close data)"
    return "(After hours — last close data)"


async def cmd_prep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /prep — Claude-powered game plan for the next trading session.
    Pulls last-close data for the watchlist and asks Claude what to watch.
    """
    await update.message.reply_text("Building tomorrow's game plan…")
    now_label = datetime.now(CT).strftime("%A %B %d, %Y  %I:%M %p CT")
    session   = get_trading_session()

    # Gather last-close snapshot for top watched tickers
    snap_lines = []
    for t in list(TICKERS)[:15]:
        try:
            d      = get_ticker_data(t)
            price  = d["price"]
            chg    = d["chg"]
            high52 = d["high52"]
            pct_off = ((price - high52) / high52 * 100) if high52 else 0
            if price > 0:
                sign = "+" if chg >= 0 else ""
                snap_lines.append(
                    f"{t} ${price:.2f} ({sign}{chg:.2f}%) {pct_off:+.1f}% from 52w high"
                )
        except Exception as e:
            logger.debug(f"Premarket snap {t}: {e}")

    fg_val, fg_label = get_fear_greed()
    snap_str = " | ".join(snap_lines[:10])

    prompt = (
        f"Today is {now_label}. Market is currently {session}. "
        f"Last-close snapshot of key watchlist stocks: {snap_str}. "
        f"Fear & Greed Index: {fg_val} ({fg_label}). "
        f"Give me: "
        f"(1) 3 specific stocks from this list with the most interesting setups for the next session and why. "
        f"(2) Key price levels to watch (support/resistance based on 52w range). "
        f"(3) One macro factor that could drive direction tomorrow. "
        f"Be specific and concise. Plain text, no markdown."
    )
    ai = get_ai_response(prompt, max_tokens=500)

    note = _offhours_note()
    await update.message.reply_text(
        f"NEXT SESSION GAME PLAN {note}\n"
        f"{datetime.now(CT).strftime('%a %b %d  %I:%M %p CT')}\n\n"
        f"{ai}\n\n"
        f"Use /analyze TICK or /chart TICK to dig deeper."
    )


async def cmd_aistocks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /aistocks — show current AI watchlist suggestions, conviction levels,
    categories, and paper trading performance on AI picks.
    /aistocks refresh — trigger an on-demand AI watchlist refresh.
    """
    # Handle /aistocks refresh sub-command
    if context.args and context.args[0].lower() == "refresh":
        await update.message.reply_text("Running AI watchlist refresh...")
        try:
            ai_refresh_watchlist(mode="intraday")
            await update.message.reply_text("AI watchlist refreshed. Run /aistocks to see results.")
        except Exception as e:
            await update.message.reply_text(f"Refresh failed: {e}")
        return

    # Calculate next refresh time
    refresh_times = ["07:00", "10:30", "12:30", "14:30"]
    now_ct = datetime.now(CT)
    now_hhmm = now_ct.strftime("%H:%M")
    next_refresh = next((t for t in refresh_times if t > now_hhmm), refresh_times[0] + " (tomorrow)")
    # Format for display (e.g. "07:00" -> "7:00 AM", "14:30" -> "2:30 PM")
    def _fmt_refresh(t_str):
        raw = t_str.replace(" (tomorrow)", "")
        suffix = " (tomorrow)" if "(tomorrow)" in t_str else ""
        h, m = int(raw.split(":")[0]), raw.split(":")[1]
        ampm = "AM" if h < 12 else "PM"
        h12 = h if h <= 12 else h - 12
        if h12 == 0:
            h12 = 12
        return f"{h12}:{m} {ampm} CT{suffix}"

    if not ai_watchlist_suggestions:
        await update.message.reply_text(
            "AI WATCHLIST STATUS\n\n"
            "No AI picks yet.\n"
            "Refresh schedule: 7am \u00b7 10:30am \u00b7 12:30pm \u00b7 2:30pm CT"
        )
        return

    lines = ["AI WATCHLIST STATUS"]
    if ai_watchlist_last_refresh:
        lines.append(f"Last: {ai_watchlist_last_refresh}  |  Next: {_fmt_refresh(next_refresh)}")
    else:
        lines.append(f"Next: {_fmt_refresh(next_refresh)}")
    lines.append("")

    # Sort by conviction
    sorted_picks = sorted(
        ai_watchlist_suggestions.items(),
        key=lambda x: x[1].get("conviction", 0),
        reverse=True,
    )

    # High conviction (8-10)
    high = [(t, info) for t, info in sorted_picks if info.get("conviction", 0) >= 8]
    if high:
        lines.append("HIGH CONVICTION (8-10):")
        for t, info in high:
            cat = info.get("category", "").replace("_", " ")
            thesis = info.get("thesis", "")[:60]
            lines.append(f"  {t} ({info['conviction']}/10) - {thesis}")

    # Moderate (6-7)
    moderate = [(t, info) for t, info in sorted_picks if 6 <= info.get("conviction", 0) <= 7]
    if moderate:
        lines.append("")
        lines.append("MODERATE (6-7):")
        for t, info in moderate:
            thesis = info.get("thesis", "")[:60]
            lines.append(f"  {t} ({info['conviction']}/10) - {thesis}")

    # Lower (1-5)
    lower = [(t, info) for t, info in sorted_picks if info.get("conviction", 0) <= 5]
    if lower:
        lines.append("")
        lines.append(f"LOWER (1-5): {len(lower)} picks")

    # Category breakdown
    cats = {}
    for _, info in sorted_picks:
        cat = info.get("category", "other").replace("_", " ")
        cats[cat] = cats.get(cat, 0) + 1
    cat_strs = [f"{v} {k}" for k, v in sorted(cats.items(), key=lambda x: -x[1])]
    lines.append("")
    lines.append(f"Categories: {', '.join(cat_strs)}")

    # Paper positions on AI picks
    ai_positions = []
    for t in ai_watchlist_suggestions:
        if t in paper_positions:
            pos = paper_positions[t]
            price, _, _ = fetch_finnhub_quote(t)
            price = price or pos["avg_cost"]
            pnl_pct = (price - pos["avg_cost"]) / pos["avg_cost"] * 100
            ai_positions.append(f"{t} ({pnl_pct:+.1f}%)")

    if ai_positions:
        lines.append("")
        lines.append(f"Paper positions on AI picks: {', '.join(ai_positions)}")

    # Count AI picks in TICKERS
    ai_in_tickers = sum(1 for t in ai_watchlist_suggestions if t in TICKERS)
    lines.append("")
    lines.append(f"AI picks in scan list: {ai_in_tickers}/{len(ai_watchlist_suggestions)}")
    lines.append(f"Total monitoring: {len(TICKERS)} tickers")

    await update.message.reply_text("\n".join(lines))


async def cmd_overnight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /overnight — overnight risk assessment for open paper positions.
    Checks for earnings, gap risk, macro exposure.
    """
    if not paper_positions:
        await update.message.reply_text("No open paper positions to assess.")
        return

    await update.message.reply_text("Checking overnight risk on open positions…")
    now_label = datetime.now(CT).strftime("%A %B %d, %Y")

    pos_lines = []
    for ticker, pos in paper_positions.items():
        try:
            price, _, _ = fetch_finnhub_quote(ticker)
            price = price or pos["avg_cost"]
            pnl_pct = (price - pos["avg_cost"]) / pos["avg_cost"] * 100
            pos_lines.append(
                f"{ticker}: entry ${pos['avg_cost']:.2f}, now ${price:.2f}, "
                f"P&L {pnl_pct:+.1f}%, held since {pos['entry_date']}"
            )
        except Exception as e:
            logger.debug(f"Overnight review {ticker}: {e}")
            pos_lines.append(f"{ticker}: entry ${pos['avg_cost']:.2f}")

    pos_str   = " | ".join(pos_lines)
    fg_val, fg_label = get_fear_greed()

    prompt = (
        f"Today is {now_label}. These are open paper trading positions held overnight: {pos_str}. "
        f"Fear & Greed: {fg_val} ({fg_label}). "
        f"For each position assess: "
        f"(1) Overnight gap risk (high/medium/low and why). "
        f"(2) Whether to hold, tighten stop, or consider trimming before close. "
        f"(3) Any known catalysts overnight (earnings, macro). "
        f"If uncertain about specific dates, say so — don't invent events. "
        f"Be direct, one paragraph per position. Plain text."
    )
    ai = get_ai_response(prompt, max_tokens=600)

    header_lines = [
        f"OVERNIGHT RISK ASSESSMENT",
        f"{now_label}",
        f"Open positions: {len(paper_positions)}",
        "",
    ]
    for line in pos_lines:
        header_lines.append(f"  {line}")

    await update.message.reply_text(
        "\n".join(header_lines) + f"\n\nClaude Assessment:\n{ai}"
    )


async def cmd_watchlist_prep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /wlprep — deep dive on the full watchlist with technicals.
    Useful on weekends to rank stocks by setup quality going into Monday.
    """
    await update.message.reply_text(
        f"Running technical scan on {len(TICKERS)} watched stocks… "
        f"(this takes ~20 seconds)"
    )
    now_label = datetime.now(CT).strftime("%A %B %d, %Y")
    note      = _offhours_note()

    scored = []
    for ticker in list(TICKERS)[:20]:
        try:
            sq = compute_squeeze_score(ticker)
            if sq.get("score") is not None:
                d     = get_ticker_data(ticker)
                price = d["price"]
                chg   = d["chg"]
                scored.append((ticker, sq["score"], price, chg,
                                sq.get("rsi", 0), sq.get("bandwidth", 0)))
        except Exception as e:
            logger.debug(f"Watchlist scan {ticker}: {e}")

    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[:8]

    lines = [f"WATCHLIST TECHNICAL SCAN {note}", now_label, ""]
    for ticker, score, price, chg, rsi, bw in top:
        rsi_flag = "⚠️ overbought" if rsi and rsi > 70 else ("🟢 oversold" if rsi and rsi < 35 else "")
        lines.append(
            f"{ticker:6}  score={score:.0f}  RSI={rsi:.0f}  BW={bw:.3f}  "
            f"${price:.2f} ({chg:+.2f}%)  {rsi_flag}"
        )

    summary_str = " | ".join(
        f"{t} score={sc:.0f} RSI={r:.0f}" for t, sc, _, _, r, _ in top[:5]
    )
    ai = get_ai_response(
        f"Today is {now_label}. Weekend watchlist technical scan results: {summary_str}. "
        f"Which 2-3 look most ready for a move next week and what setup are they forming? "
        f"Be specific about the pattern. Plain text.",
        max_tokens=400
    )

    await update.message.reply_text(
        "\n".join(lines) + f"\n\nClaude Setup Read:\n{ai}\n\n"
        f"Top pick: /analyze {top[0][0] if top else '?'}"
    )


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /ask <question> — ask Claude AI anything about the market.
    Automatically injects live prices, market snapshot, and news headlines
    so Claude always has current data to answer with.
    """
    if not update.message:
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /ask <your question>\n\n"
            "Examples:\n"
            "  /ask What's happening with NVDA today?\n"
            "  /ask Is the market overbought right now?\n"
            "  /ask What's driving the market today?\n"
            "  /ask Should I be worried about the VIX spike?"
        )
        return

    user_msg = " ".join(context.args).strip()
    chat_id  = str(update.effective_chat.id)
    logger.info(f"/ask from {chat_id}: {user_msg[:80]}")
    await update.message.reply_text("Thinking...")

    # ── Gather live context concurrently ─────────────────────
    def _get_snapshot():
        try:
            s = fetch_market_snapshot()
            return (
                f"LIVE MARKET DATA:\n"
                f"Indices: {s['indices_str']}\n"
                f"Sectors: {s['sector_str']}\n"
                f"Fear & Greed: {s['fg_str']}\n"
                f"VIX: {s['vix']:.1f}\n"
                f"Crypto: {s['crypto_str']}\n"
                f"Watchlist movers: {s['movers_str']}"
            )
        except Exception as e:
            logger.debug(f"snapshot in /ask failed: {e}")
            return ""

    def _get_market_news():
        """Fetch general market news headlines from Finnhub."""
        try:
            r = requests.get(
                f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_TOKEN}",
                timeout=8
            )
            items = r.json()[:8]
            headlines = [item.get("headline", "") for item in items if item.get("headline")]
            return "TODAY'S NEWS HEADLINES:\n" + "\n".join(f"- {h[:100]}" for h in headlines[:6])
        except Exception as e:
            logger.debug(f"market news fetch failed: {e}")
            return ""

    def _get_ticker_news(ticker):
        """Fetch company-specific news if a ticker was mentioned."""
        try:
            news = fetch_latest_news(ticker, 4)
            if news:
                return f"\n{ticker} NEWS:\n" + "\n".join(f"- {h[:100]}" for h, _ in news)
        except Exception as e:
            logger.debug(f"Chat ticker news {ticker}: {e}")
        return ""

    def _get_ticker_price(ticker):
        """Fetch live price for a mentioned ticker."""
        try:
            d = get_ticker_data(ticker)
            if d["price"]:
                sign = "+" if d["chg"] >= 0 else ""
                parts = [f"{ticker}: ${d['price']:.2f} ({sign}{d['chg']:.2f}%)"]
                if d["high52"] and d["low52"]:
                    parts.append(f"52w range: ${d['low52']:.2f}-${d['high52']:.2f}")
                if d["volume"]:
                    parts.append(f"vol: {d['volume']:,}")
                return " | ".join(parts)
        except Exception as e:
            logger.debug(f"Chat ticker price {ticker}: {e}")
        return ""

    # Detect mentioned tickers
    words = user_msg.upper().split()
    mentioned_tickers = []
    for word in words:
        clean = ''.join(c for c in word if c.isalpha())
        if clean and (clean in TICKERS or len(clean) <= 5):
            mentioned_tickers.append(clean)
            if len(mentioned_tickers) >= 3:
                break

    with ThreadPoolExecutor(max_workers=4) as pool:
        f_snap = pool.submit(_get_snapshot)
        f_news = pool.submit(_get_market_news)
        f_tick_news  = [pool.submit(_get_ticker_news, t)  for t in mentioned_tickers[:2]]
        f_tick_price = [pool.submit(_get_ticker_price, t) for t in mentioned_tickers[:2]]

    snapshot_str   = f_snap.result()
    market_news    = f_news.result()
    ticker_news    = "\n".join(f.result() for f in f_tick_news  if f.result())
    ticker_prices  = "\n".join(f.result() for f in f_tick_price if f.result())

    # Build enriched message with all live data prepended
    context_block = "\n\n".join(filter(None, [
        snapshot_str,
        f"LIVE PRICES:\n{ticker_prices}" if ticker_prices else "",
        ticker_news,
        market_news,
    ]))

    enriched = (
        f"{context_block}\n\n"
        f"USER QUESTION: {user_msg}"
        if context_block else user_msg
    )

    try:
        reply = get_ai_conversation(chat_id, enriched)
        if not reply or reply.strip() in ("", "AI unavailable", "AI unavailable right now."):
            await update.message.reply_text(
                "Claude AI is not responding. "
                "Check that ANTHROPIC_API_KEY is set in Railway and try again."
            )
        else:
            await update.message.reply_text(reply)
    except Exception as e:
        logger.error(f"cmd_ask error: {e}", exc_info=True)
        await update.message.reply_text(f"Error reaching Claude AI: {e}")



# ============================================================
# SCHEDULED MESSAGES
# ============================================================
def send_morning_briefing():
    """8:30 AM CT — market open briefing with full live data."""
    global daily_alerts
    daily_alerts = 0
    logger.info("Morning briefing")
    s = fetch_market_snapshot()

    prompt = (
        f"Today is {s['now_label']}. Market just opened at 8:30 AM CT. "
        f"Indices: {s['indices_str']}. "
        f"Sectors: {s['sector_str']}. "
        f"Fear & Greed: {s['fg_val']} ({s['fg_label']}). "
        f"VIX: {s['vix']:.1f}. "
        f"Watchlist movers: {s['movers_str']}. "
        f"Crypto: {s['crypto_str']}. "
        f"Give 3 specific things to watch today based strictly on this data. Plain text."
    )
    ai = get_ai_response(prompt, max_tokens=350)

    msg_lines = (
        [f"🌅 Morning Briefing — {datetime.now(CT).strftime('%B %d, %Y')}",
         f"{datetime.now(CT).strftime('%I:%M %p CT')}", ""] +
        s["indices_lines"] +
        [f"", f"Fear & Greed: {s['fg_str']}  VIX: {s['vix']:.1f}",
         f"", "Sectors:"] + s["sector_lines"][:5] +
        [f"", f"Claude:\n{ai}"]
    )
    send_telegram("\n".join(msg_lines))
    send_dashboard_sync("Market Open")

def send_daily_close_summary():
    """3:00 PM CT — closing bell summary with full live data."""
    logger.info("Daily close summary")
    s = fetch_market_snapshot()

    # Top squeeze candidates at close
    top_sq = sorted(squeeze_scores.items(), key=lambda x: x[1], reverse=True)[:3]
    sq_str = " | ".join(f"{t} score={sc:.0f}" for t, sc in top_sq) or "none"

    prompt = (
        f"Today is {s['now_label']}. Market just closed at 3:00 PM CT. "
        f"Final: {s['indices_str']}. "
        f"Sectors: {s['sector_str']}. "
        f"Fear & Greed: {s['fg_val']} ({s['fg_label']}). "
        f"VIX: {s['vix']:.1f}. "
        f"Today's spike alerts: {daily_alerts}. "
        f"Top squeeze candidates: {sq_str}. "
        f"Watchlist movers: {s['movers_str']}. "
        f"Give: (1) one sentence on what drove today's action based on this data, "
        f"(2) one overnight risk or catalyst to watch. "
        f"Be specific to the numbers. Plain text."
    )
    ai = get_ai_response(prompt, max_tokens=200)

    msg_lines = (
        [f"🔔 Market Close — {datetime.now(CT).strftime('%I:%M %p CT')}",
         s["now_label"], ""] +
        s["indices_lines"] +
        [f"", f"Fear & Greed: {s['fg_str']}  VIX: {s['vix']:.1f}",
         f"Spike alerts today: {daily_alerts}",
         f"", "Sectors:"] + s["sector_lines"][:5] +
        [f"", f"Claude: {ai}"]
    )
    send_telegram("\n".join(msg_lines))
    send_dashboard_sync("Market Close")

def send_startup_message():
    """Send startup notification with live market snapshot."""
    s          = fetch_market_snapshot()
    status_str = ("OPEN Regular" if s["session"] == "regular"
                  else "OPEN Extended" if s["session"] == "extended"
                  else "CLOSED")

    prompt = (
        f"Today is {s['now_label']}. Stock monitor just started. "
        f"Market is {status_str}. "
        f"Indices: {s['indices_str']}. "
        f"Fear & Greed: {s['fg_val']} ({s['fg_label']}). "
        f"VIX: {s['vix']:.1f}. "
        f"Give current market sentiment in 6 words based on this data."
    )
    ai = get_ai_response(prompt, fast=True, max_tokens=30)

    msg_lines = (
        [f"🚀 STOCK SPIKE MONITOR STARTED",
         f"{s['now_label']}",
         f"Market: {status_str}",
         f"Watching: {len(TICKERS)} stocks",
         f"Spike threshold: {THRESHOLD*100:.0f}%  Scan: every {CHECK_INTERVAL_MIN} min",
         f""] +
        s["indices_lines"] +
        [f"", f"Fear & Greed: {s['fg_str']}  VIX: {s['vix']:.1f}",
         f"", f"Claude: {ai}",
         f"", f"Use /help for all commands."]
    )
    send_telegram("\n".join(msg_lines))
    send_dashboard_sync("Startup")

def send_weekly_digest():
    """Sunday 6 PM CT — week-in-review with live snapshot."""
    if not recent_alerts:
        logger.info("Weekly digest: no alerts to report")
        return
    logger.info("Sending weekly digest")
    s = fetch_market_snapshot()

    tally  = {}
    for alert in recent_alerts:
        ticker = alert.split()[0]
        tally[ticker] = tally.get(ticker, 0) + 1
    ranked  = sorted(tally.items(), key=lambda x: x[1], reverse=True)
    top_str = ", ".join(f"{t}({n})" for t, n in ranked[:5])

    prompt = (
        f"Today is {s['now_label']} (Sunday). Weekly market recap. "
        f"Indices: {s['indices_str']}. "
        f"Sectors: {s['sector_str']}. "
        f"Fear & Greed: {s['fg_val']} ({s['fg_label']}). "
        f"VIX: {s['vix']:.1f}. "
        f"This week's spike alerts: {len(recent_alerts)} total. "
        f"Most active tickers: {top_str}. "
        f"Give: (1) one sentence summarising this week's market theme based on this data, "
        f"(2) one stock or sector to watch next week and why. "
        f"Be specific to the numbers. Plain text."
    )
    ai = get_ai_response(prompt, max_tokens=250)

    lines = (
        [f"📅 Weekly Digest — {s['now_label']}",
         f"Total spike alerts this week: {len(recent_alerts)}", ""] +
        s["indices_lines"] +
        [f"", f"Fear & Greed: {s['fg_str']}  VIX: {s['vix']:.1f}",
         f"", "Most active tickers:"] +
        [f"  {t}: {n} alert{'s' if n > 1 else ''}" for t, n in ranked[:8]] +
        [f"", f"Claude: {ai}"]
    )
    send_telegram("\n".join(lines))


def send_premarket_dashboard():
    """8:00 AM CT — pre-market snapshot with live data before regular open."""
    logger.info("Pre-market dashboard")
    s = fetch_market_snapshot()

    # Pre-market futures — use ETF proxies via Finnhub (ES=F not available on free tier)
    # SPY/QQQ pre-market quotes serve as reliable proxies for S&P/Nasdaq futures
    futures_lines = s["futures_lines"]   # already fetched in fetch_market_snapshot
    futures_str   = s["futures_str"]

    prompt = (
        f"Today is {s['now_label']}. Pre-market 8:00 AM CT. "
        f"Futures: {futures_str}. "
        f"Indices last close: {s['indices_str']}. "
        f"Sectors: {s['sector_str']}. "
        f"Fear & Greed: {s['fg_val']} ({s['fg_label']}). "
        f"VIX: {s['vix']:.1f}. "
        f"Crypto: {s['crypto_str']}. "
        f"Watchlist movers: {s['movers_str']}. "
        f"Give: (1) one-sentence pre-market mood based on this data, "
        f"(2) the one sector or theme most likely to lead at open and why. "
        f"Base your answer strictly on the numbers above. Plain text."
    )
    ai = get_ai_response(prompt, max_tokens=200, fast=True)

    msg_lines = [
        f"🌅 Pre-Market Snapshot — {datetime.now(CT).strftime('%I:%M %p CT')}",
        f"{s['now_label']}",
        "",
    ]
    if futures_lines:
        msg_lines += ["Futures:"] + futures_lines + [""]
    msg_lines += (
        ["Indices (last close):"] + s["indices_lines"] +
        ["", f"Fear & Greed: {s['fg_str']}", f"VIX: {s['vix']:.1f}"] +
        ["", "Sectors:"] + s["sector_lines"][:5] +
        ["", f"Claude: {ai}"]
    )
    send_telegram("\n".join(msg_lines))
    send_dashboard_sync("Pre-Market")


def send_midday_dashboard():
    """12:00 PM CT mid-session check with live data."""
    if get_trading_session() == "closed":
        return
    logger.info("Mid-day dashboard")
    s = fetch_market_snapshot()

    prompt = (
        f"Today is {s['now_label']}. Mid-session 12:00 PM CT. "
        f"Indices: {s['indices_str']}. "
        f"Sectors: {s['sector_str']}. "
        f"Fear & Greed: {s['fg_val']} ({s['fg_label']}). "
        f"VIX: {s['vix']:.1f}. "
        f"Watchlist movers: {s['movers_str']}. "
        f"Spike alerts so far today: {daily_alerts}. "
        f"One sentence: what is the market doing right now and what should traders watch "
        f"into the close? Base it on the data above. Plain text."
    )
    ai = get_ai_response(prompt, max_tokens=150, fast=True)

    msg_lines = (
        [f"📊 Mid-Day Check-In — {datetime.now(CT).strftime('%I:%M %p CT')}",
         s["now_label"], ""] +
        s["indices_lines"] +
        [f"", f"Fear & Greed: {s['fg_str']}  VIX: {s['vix']:.1f}",
         f"Alerts today: {daily_alerts}",
         f"", f"Claude: {ai}"]
    )
    send_telegram("\n".join(msg_lines))
    send_dashboard_sync("Mid-Day")


def send_evening_recap():
    """6:00 PM CT — after-hours recap + tomorrow setup."""
    if get_trading_session() != "closed":
        return   # skip if still in extended hours
    s = fetch_market_snapshot()
    ai = get_ai_response(
        f"Today is {s['now_label']}. Market closed. Final: {s['indices_str']}. "
        f"Sectors: {s['sector_str']}. "
        f"Fear & Greed: {s['fg_val']} ({s['fg_label']}). "
        f"VIX: {s['vix']:.1f}. "
        f"(1) One sentence on today's key theme based on this data. "
        f"(2) Two things to watch for tomorrow's open. "
        f"Keep it to 3 sentences total. Plain text.",
        max_tokens=180
    )
    send_telegram(
        f"Evening Recap — {datetime.now(CT).strftime('%I:%M %p CT')}\n"
        f"{s['indices_str']}\n\n"
        f"Fear & Greed: {s['fg_str']}  VIX: {s['vix']:.1f}\n\n"
        f"Claude: {ai}\n\n"
        f"Use /prep for tomorrow's game plan  |  /overnight for position risk"
    )


def send_saturday_prep():
    """Saturday 9:00 AM CT — weekend watchlist prep digest."""
    now_label = datetime.now(CT).strftime("%A %B %d, %Y")
    scored = []
    for ticker in list(TICKERS)[:20]:
        try:
            sq    = compute_squeeze_score(ticker)
            d     = get_ticker_data(ticker)
            price = d["price"]
            chg   = d["chg"]
            if sq.get("score") and price > 0:
                scored.append((ticker, sq["score"], price, chg, sq.get("rsi", 0)))
        except Exception as e:
            logger.debug(f"Weekend scan {ticker}: {e}")
    scored.sort(key=lambda x: x[1], reverse=True)
    top     = scored[:6]
    top_str = " | ".join(f"{t} score={sc:.0f} RSI={r:.0f}" for t, sc, _, _, r in top)
    fg_val, fg_label = get_fear_greed()
    ai = get_ai_response(
        f"Today is {now_label} (weekend). Watchlist technical scores: {top_str}. "
        f"Fear & Greed: {fg_val} ({fg_label}). "
        f"Top 3 setups to watch Monday open and what each needs to confirm the move. "
        f"Plain text, be specific.",
        max_tokens=400
    )
    lines = [f"Weekend Watchlist Prep — {now_label}", f"Fear & Greed: {fg_val} ({fg_label})", ""]
    for ticker, score, price, chg, rsi in top:
        lines.append(f"  {ticker:6}  score={score:.0f}  RSI={rsi:.0f}  ${price:.2f} ({chg:+.2f}%)")
    lines += ["", f"Claude: {ai}", "", "Use /prep or /wlprep for deeper analysis"]
    send_telegram("\n".join(lines))


# ============================================================
# STARTUP: PRIME PRICE HISTORY WITH YFINANCE
# ============================================================
def _prime_price_history():
    """Bootstrap price_history with recent intraday data from yfinance so
    technical indicators work immediately instead of waiting 20+ minutes."""
    logger.info("Priming price_history with yfinance intraday data...")
    count = 0
    for ticker in list(TICKERS)[:50]:  # Cap at 50 to not overload yfinance
        try:
            if len(price_history.get(ticker, deque())) >= 15:
                continue  # Already has enough data
            tk = yf.Ticker(ticker)
            hist = tk.history(period="1d", interval="1m")
            if hist is not None and len(hist) > 0:
                dq = price_history.setdefault(ticker, deque(maxlen=60))
                for idx, row in hist.tail(30).iterrows():
                    ts = idx.to_pydatetime()
                    if ts.tzinfo:
                        ts = ts.astimezone(CT)
                    else:
                        ts = CT.localize(ts)
                    dq.append((ts, float(row['Close'])))
                count += 1
                # Also prime last_prices
                if ticker not in last_prices and len(dq) > 0:
                    last_prices[ticker] = dq[-1][1]
        except Exception as e:
            logger.debug(f"Prime {ticker}: {e}")
    logger.info(f"Primed price_history for {count} tickers")


def _load_daily_candles():
    """Load 30 trading days of daily candles from yfinance for all monitored tickers.
    Called on startup and once daily (at 07:05 CT before market open)."""
    global daily_candles
    logger.info("Loading daily candles from yfinance...")
    count = 0
    for ticker in list(TICKERS)[:60]:
        try:
            tk = yf.Ticker(ticker)
            hist = tk.history(period="2mo", interval="1d")
            if hist is not None and len(hist) >= 5:
                candles = []
                for idx, row in hist.iterrows():
                    candles.append({
                        "date": idx.strftime("%Y-%m-%d"),
                        "open": float(row["Open"]),
                        "high": float(row["High"]),
                        "low": float(row["Low"]),
                        "close": float(row["Close"]),
                        "volume": int(row["Volume"]),
                    })
                daily_candles[ticker] = candles[-30:]
                count += 1
        except Exception as e:
            logger.debug(f"Daily candles {ticker}: {e}")
    logger.info(f"Loaded daily candles for {count} tickers")


# ============================================================
# BACKGROUND SCANNER
# ============================================================
def scanner_thread():
    """
    Background thread — timezone-independent scheduler.

    All job times are defined in CT (America/Chicago).
    The loop reads datetime.now(CT) directly, so it fires correctly
    regardless of the server's system timezone (UTC on Railway, local
    time on a dev machine, etc.).

    Job table format:
        (day, "HH:MM", function)
        day = "daily" | "monday"…"sunday"
    """

    # ── Define all scheduled jobs in CT ───────────────────────
    JOBS = [
        # day            CT time   function
        ("daily",        "07:00",  lambda: ai_refresh_watchlist(mode="premarket")),
        ("daily",        "07:05",  _load_daily_candles),  # Refresh daily candles before market open
        ("daily",        "08:00",  send_premarket_dashboard),
        ("daily",        "08:30",  _merge_dynamic_stocks),
        ("daily",        "08:30",  send_morning_briefing),
        ("daily",        "08:31",  paper_morning_report),
        ("daily",        "10:30",  lambda: ai_refresh_watchlist(mode="intraday")),
        ("daily",        "12:00",  send_midday_dashboard),
        ("daily",        "12:30",  lambda: ai_refresh_watchlist(mode="intraday")),
        ("daily",        "14:30",  lambda: ai_refresh_watchlist(mode="intraday")),
        ("daily",        "15:00",  send_daily_close_summary),
        ("daily",        "15:01",  paper_eod_report),
        ("daily",        "18:00",  send_evening_recap),
        ("sunday",       "18:00",  send_weekly_digest),
        ("saturday",     "09:00",  send_saturday_prep),
    ]

    DAY_NAMES = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]

    # Track which (day, time) combos have already fired to prevent
    # double-firing within the same minute
    fired: set = set()
    last_scan      = datetime.now(CT) - timedelta(minutes=CHECK_INTERVAL_MIN + 1)
    last_state_save = datetime.now(CT) - timedelta(minutes=6)  # save every 5 min

    logger.info(
        f"Scheduler started — all times in CT "
        f"(server local: {datetime.now().strftime('%Z %z') or 'unknown'})"
    )

    while True:
        now_ct    = datetime.now(CT)
        now_hhmm  = now_ct.strftime("%H:%M")
        now_day   = DAY_NAMES[now_ct.weekday()]
        fire_key  = f"{now_ct.strftime('%Y-%m-%d')}-{now_hhmm}"  # unique per day+minute

        # ── Timed jobs ────────────────────────────────────────
        for day, hhmm, fn in JOBS:
            job_key = f"{fire_key}-{day}-{hhmm}"
            if now_hhmm == hhmm and (day == "daily" or day == now_day):
                if job_key not in fired:
                    fired.add(job_key)
                    logger.info(f"Firing scheduled job: {day} {hhmm} CT -> {fn.__name__ if hasattr(fn,'__name__') else 'lambda'}")
                    try:
                        fn()
                        # Save state after ticker refresh so new TICKERS list persists
                        if hhmm in ("08:30", "07:00", "10:30", "12:30", "14:30"):
                            save_bot_state()
                    except Exception as e:
                        logger.error(f"Scheduled job error ({day} {hhmm}): {e}", exc_info=True)

        # ── Prune fired set daily to avoid unbounded growth ───
        if len(fired) > 500:
            today_prefix = now_ct.strftime("%Y-%m-%d")
            fired = {k for k in fired if k.startswith(today_prefix)}

        # ── Stock scanner — every CHECK_INTERVAL_MIN minutes ──
        elapsed = (now_ct - last_scan).total_seconds() / 60
        if elapsed >= CHECK_INTERVAL_MIN:
            last_scan = now_ct
            try:
                check_stocks()
            except Exception as e:
                logger.error(f"check_stocks error: {e}", exc_info=True)

        # ── Periodic state persistence — every 5 minutes ─────
        state_elapsed = (now_ct - last_state_save).total_seconds() / 60
        if state_elapsed >= 5:
            last_state_save = now_ct
            threading.Thread(target=save_bot_state, daemon=True).start()

        time.sleep(30)   # check twice per minute — plenty for minute-precision jobs


# ============================================================
# MAIN — Telegram bot
# ============================================================
def run_telegram_bot():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # ── Market Pulse ──────────────────────────────────────────
    app.add_handler(CommandHandler("overview",    cmd_overview))
    app.add_handler(CommandHandler("crypto",      cmd_crypto))
    app.add_handler(CommandHandler("macro",       cmd_macro))
    app.add_handler(CommandHandler("earnings",    cmd_earnings))

    # ── Movers ────────────────────────────────────────────────
    app.add_handler(CommandHandler("movers",      cmd_movers))

    # ── Stock Tools ───────────────────────────────────────────
    app.add_handler(CommandHandler("price",       cmd_price))
    app.add_handler(CommandHandler("analyze",     cmd_analyze))
    app.add_handler(CommandHandler("compare",     cmd_compare))
    app.add_handler(CommandHandler("chart",       cmd_chart))
    app.add_handler(CommandHandler("rsi",         cmd_rsi))
    app.add_handler(CommandHandler("news",        cmd_news))

    # ── Alerts & Scanning ─────────────────────────────────────
    app.add_handler(CommandHandler("spikes",      cmd_spikes))
    app.add_handler(CommandHandler("alerts",      cmd_alerts))
    app.add_handler(CommandHandler("squeeze",     cmd_squeeze))
    app.add_handler(CommandHandler("setalert",    cmd_setalert))
    app.add_handler(CommandHandler("myalerts",    cmd_myalerts))
    app.add_handler(CommandHandler("delalert",    cmd_delalert))
    app.add_handler(CommandHandler("watchlist",   cmd_watchlist))

    # ── Bot Control ───────────────────────────────────────────
    app.add_handler(CommandHandler("dashboard",   cmd_dashboard))
    app.add_handler(CommandHandler("list",        cmd_list))
    app.add_handler(CommandHandler("monitoring",  cmd_monitoring))
    app.add_handler(CommandHandler("help",        cmd_help))

    # ── Paper Trading ─────────────────────────────────────────
    app.add_handler(CommandHandler("paper",       cmd_paper))
    app.add_handler(CommandHandler("overnight",   cmd_overnight))
    app.add_handler(CommandHandler("aistocks",    cmd_aistocks))

    # ── Off-hours / prep ──────────────────────────────────────
    app.add_handler(CommandHandler("prep",        cmd_prep))
    app.add_handler(CommandHandler("wlprep",      cmd_watchlist_prep))

    app.add_handler(CommandHandler("ask",         cmd_ask))

    app.run_polling()

# ============================================================
# ENTRY POINT
# ============================================================
# Load saved state FIRST so we immediately have a working ticker list
load_paper_state()   # restore paper trading state from disk
load_bot_state()     # restore watchlists, alerts, tickers, conversations

# If TICKERS ended up empty for any reason, fall back to CORE_TICKERS immediately
if not TICKERS:
    TICKERS = list(CORE_TICKERS)
    logger.warning("TICKERS was empty after state load — reset to CORE_TICKERS")

# Background ticker refresh — runs get_dynamic_hot_stocks() without blocking startup
def _refresh_tickers_bg():
    global TICKERS
    try:
        fresh = get_dynamic_hot_stocks()
        if fresh:
            TICKERS = fresh
            save_bot_state()
            logger.info(f"Background ticker refresh complete: {len(TICKERS)} stocks")
    except Exception as e:
        logger.error(f"Background ticker refresh failed: {e}")

threading.Thread(target=_refresh_tickers_bg, daemon=True).start()

# Startup: prime price_history so technical indicators work immediately
if get_trading_session() != "closed":
    threading.Thread(target=_prime_price_history, daemon=True).start()
    threading.Thread(target=_load_daily_candles, daemon=True).start()

# Startup: prime AI watchlist if market is open (scanner_thread handles first scan)
if get_trading_session() != "closed":
    logger.info("Startup: market is open, priming AI watchlist...")
    if not ai_watchlist_suggestions:
        threading.Thread(target=lambda: ai_refresh_watchlist(mode="intraday"), daemon=True).start()

threading.Thread(target=scanner_thread, daemon=True).start()
logger.info("STOCK SPIKE MONITOR STARTED")
send_startup_message()
run_telegram_bot()
