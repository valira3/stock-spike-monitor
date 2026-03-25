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
from telegram import (
    BotCommand, BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats, Update,
)
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
TRADERSPOST_WEBHOOK_URL = os.getenv("TRADERSPOST_WEBHOOK_URL")
TELEGRAM_TP_CHAT_ID     = os.getenv("TELEGRAM_TP_CHAT_ID")
TELEGRAM_TP_TOKEN       = os.getenv("TELEGRAM_TP_TOKEN")

# FMP stable API endpoints (v3 is deprecated for newer accounts)
FMP_ENDPOINTS = {
    "actives": "https://financialmodelingprep.com/stable/most-actives",
    "gainers": "https://financialmodelingprep.com/stable/biggest-gainers",
    "losers":  "https://financialmodelingprep.com/stable/biggest-losers",
}

BOT_VERSION = "2.7.10"
RELEASE_NOTES = [
    "2.7.10 — /buzz command (Reddit buzz leaderboard), morning cool-off (block first 15min entries).",
    "2.7.9 — Social buzz (Reddit/ApeWisdom), compact mover alerts, fear override for high-conviction viral stocks.",
    "2.7.8 — Real-time F&G: switched to CNN intraday endpoint (updates every few minutes) with alternative.me fallback.",
    "2.7.7 — Regime-aware pause (F&G<20), wider ATR stops (4.0/3.5/3.0/2.5), hard stop ATR×3.0, signal-collapse 2% min, position caps by F&G.",
    "2.7.6 — Fix asymmetric P&L: threshold floor 70 (was 60), signal-collapse ≤20 with 1% min profit gate.",
    "2.7.5 — Risk appetite 5% per trade (was 1%), portfolio heat limit 30% (was 6%).",
    "2.7.4 — Falling-knife guard: block buys on stocks that surged 15%+ in 5d and are now declining.",
    "2.7.3 — Speculative buys ($1-5 viral/volume stocks, 5% cap, max 2), hold duration on all SELL messages.",
    "2.7.2 — Anti-churn: trough-buying bias (RSI/BB mean-reversion), 30-min min hold, wider ATR trails, TP portfolio stats.",
    "2.7.1 — /strategy command: full end-to-end trading strategy overview with live parameters.",
    "2.7.0 — Full gap analysis implementation: ATR-based dynamic stops, volatility-normalized position sizing, portfolio heat limit (6%), per-ticker re-entry cooldown (4h/8h), multi-regime market classification (4-regime), signal decay weighting, correlation-aware position limits.",
    "2.6.3 — Performance tuning: adaptive threshold floor 60 (was 45), 30-min hold before signal-collapse exit, 429 cache to cut Finnhub rate-limit storms.",
    "2.6.2 — TP notifications now include exit reason, P&L, and signal score/ToD zone on BUY.",
    "2.6.1 — Settlement cleanup on startup: purges stale T+1 entries, logs what was cleared.",
    "2.6 — Intraday time-of-day awareness: signal score modifier (±8 pts) and position sizing (65-100%) based on U-shaped volume pattern. Power hours boosted, lunch lull penalized.",
    "2.5.1 — TP Portfolio fully independent. /tpsync reset wipes to clean $100k.",
    "2.5 — TP Portfolio sync fix: cash guard on BUY, forced EXIT sync on webhook failure.",
    "2.4 — Robinhood hours fix: extended session now 7 AM–8 PM ET. All TradersPost orders use limit pricing (±0.5% buffer) for safety and extended-hours compliance.",
    "2.3 — Signal logger now captures AI reasoning (grok_reason, news_catalyst) for richer backtesting. BUY log entries include full AI context.",
    "2.2 — Graduated trailing stop replaces fixed take-profit. Winners now run with widening trail (3%/4%/5%/6% by profit zone).",
    "2.1 — Fix: /tp portfolio value uses live prices. Command menu for groups. Removed /paper from TP bot. Renamed shadow→TP portfolio.",
    "2.0 — Major: AVWAP entry gate & stop, backtesting engine (/backtest), persistent signal logger, 11-factor scoring (150 pts).",
    "1.19 — Cash Account: removed PDT tracker & drift detection, added T+1 settlement tracking.",
    "1.18 — VIX Put-Selling Alert: auto-alerts when VIX crosses 33 with put premiums on GOOG/NVDA/AMZN/META.",
    "1.17 — Full channel separation: TP commands exclusive to TradersPost bot.",
    "1.16 — Separate Telegram channel for TradersPost/shadow trading.",
    "1.15 — Shadow portfolio tracker with /tpsync command.",
    "1.14 — Shadow Mode: TradersPost webhook integration, /shadow /tp commands.",
    "1.13 — Adaptive Trading: all params auto-adjust to market conditions (F&G + VIX). /set persists across deploys.",
    "1.12 — Extended Hours Paper Trading: portfolio, positions, and sell logic now use live pre-market/after-hours prices.",
    "1.11 — Smart Trading: trailing stops, adaptive thresholds, sector guards, earnings filter, /perf dashboard, /set config, signal learning, support/resistance, /paper chart, daily P&L.",
    "1.10 — News Sentiment Scoring: AI-powered news analysis now feeds into trading signals (component 10/10, up to 15 pts). /news shows sentiment + source timestamps.",
    "1.9 — Extended Hours Pricing: pre-market and after-hours prices from yfinance. Dashboard and quotes now show live extended session data.",
    "1.8 — Dashboard Sharpness: 220 DPI rendering, larger fonts, sent as document for crisp mobile viewing.",
    "1.7 — Alert Spam Fix: 15-min cooldown with 1% escalation threshold. Startup grace period prevents false alerts.",
    "1.6 — Chart & RSI: yfinance-based /chart and /rsi commands (replaced Finnhub candles). VWAP crash fix.",
    "1.5 — Startup Rate Fix: removed duplicate scan on boot, eliminated 75+ Finnhub 429 errors.",
    "1.4 — Multi-Day Trends: 5-day SMA trend + momentum + volume component (15 pts) for longer-term signals.",
    "1.3 — Paper Trading Boost: day-change MOVER alerts, price history primed on startup, signal cache 120s.",
    "1.2 — Crypto & Batching: rewritten /crypto, TTL caching, batch scanning, wider dashboard.",
    "1.1 — Mobile & AI Watchlist: compact /help, mobile dashboard, AI-driven watchlist rotation.",
    "1.0 — Initial Release: 30-stock scanner, paper trading, spike alerts, Claude AI integration.",
]

THRESHOLD           = 0.03
MIN_PRICE           = 5.0
MIN_PRICE_SPECULATIVE = 1.0    # v2.7.3: speculative low-price stocks
SPEC_MAX_POS_PCT    = 0.05     # v2.7.3: max 5% of portfolio per speculative position
SPEC_MAX_POSITIONS  = 2        # v2.7.3: max 2 speculative positions at once
SPEC_MIN_VOL_RATIO  = 3.0      # v2.7.3: require 3x avg volume for speculative buys
COOLDOWN_MINUTES    = 15
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

_quote_cache = _TTLCache(ttl_seconds=90.0, max_size=500)  # 90s: survive across scan cycles
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
    " /buzz        Reddit social buzz leaderboard\n"
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
    " /paper signal T   11-factor score\n"
    " /paper chart intraday value chart\n"
    " /paper log   download trade log\n"
    " /paper reset start over at $100k\n"
    " /perf        performance dashboard\n"
    " /overnight   gap risk on holdings\n"
    " /backtest [days] [tp=X sl=X] replay backtest\n"
    "\n"
    "OPTIONS\n"
    " /vixalert    VIX put-selling setup\n"
    " /vixalert check  scan puts now\n"
    "\n"
    "AI & TOOLS\n"
    " /aistocks    AI picks + conviction\n"
    " /ask <q>     chat with Claude\n"
    " /prep        next session plan\n"
    " /wlprep      watchlist deep scan\n"
    "\n"
    "BOT\n"
    " /list        monitored tickers\n"
    " /set         adjust thresholds\n"
    " /monitoring  pause|resume|status\n"
    " /strategy    full trading strategy\n"
    " /version     release notes\n"
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
last_alert_pct      = {}   # {ticker: last_pct_change_alerted} for smart spike suppression
_startup_time       = datetime.now(CT)  # grace period: skip spike alerts for 300s after startup
price_history       = {t: deque(maxlen=60) for t in CORE_TICKERS}  # 60 ticks for RSI(14)
recent_alerts       = []
_pending_mover_alerts = []  # v2.7.9: batch day-change mover alerts
custom_price_alerts = {}   # {ticker: [target_prices]}
user_watchlists     = {}   # {chat_id: [tickers]}
conversation_history= {}   # {chat_id: [messages]} for multi-turn Q&A
squeeze_scores      = {}   # {ticker: score} updated each scan cycle
ai_watchlist_suggestions = {}  # {ticker: {"conviction": int, "thesis": str, "category": str, "added_at": str}}
ai_watchlist_last_refresh = ""  # e.g. "10:30 AM CT (intraday)"
daily_candles = {}  # {ticker: list of dicts [{date, open, high, low, close, volume}, ...]}  — ephemeral, NOT persisted
avwap_cache   = {}  # {ticker: {"avwap": float, "reclaimed": bool, "ts": datetime}} — intraday AVWAP state

# ── Signal data logger for future backtesting ──────────────────
_signal_log_lock = threading.Lock()
SIGNAL_LOG_FILE = os.path.join(
    os.path.dirname(os.getenv("PAPER_STATE_PATH", "paper_state.json")),
    "signal_log.jsonl"
) if os.path.dirname(os.getenv("PAPER_STATE_PATH", "paper_state.json")) else "signal_log.jsonl"

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


def _capture_tp_chat(update):
    """Auto-capture the TP bot DM chat ID from any command."""
    global tp_dm_chat_id
    if update.effective_chat and update.effective_chat.type == "private":
        new_id = update.effective_chat.id
        if new_id != tp_dm_chat_id:
            tp_dm_chat_id = new_id
            tp_state["dm_chat_id"] = new_id
            save_paper_state()
            logger.info(f"[TP] Captured DM chat ID: {new_id}")


def send_tp_telegram(message):
    """Send to TP user's DM chat.
    Falls back to TP channel, then main channel."""
    chat_id = tp_dm_chat_id or TELEGRAM_TP_CHAT_ID
    if not chat_id:
        send_telegram(f"📡 [TP] {message}")
        return
    token = TELEGRAM_TP_TOKEN or TELEGRAM_TOKEN
    try:
        url = (f"https://api.telegram.org/bot"
               f"{token}/sendMessage")
        payload = {
            "chat_id": chat_id,
            "text": message,
        }
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"[TP] Failed to send DM: {e}")

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

def fetch_news_with_details(ticker, count=5):
    """Fetch news with full details: headline, summary, source, datetime, url."""
    try:
        today     = datetime.now().date()
        yesterday = today - timedelta(days=2)
        if not _finnhub_limiter.acquire(timeout=5):
            logger.warning(f"Finnhub rate limit: skipping news for {ticker}")
            return []
        r = requests.get(
            f"https://finnhub.io/api/v1/company-news?symbol={ticker}"
            f"&from={yesterday}&to={today}&token={FINNHUB_TOKEN}",
            timeout=10
        )
        news = r.json()[:count]
        return [
            {
                "headline": item.get("headline", ""),
                "summary":  item.get("summary", ""),
                "source":   item.get("source", ""),
                "datetime": item.get("datetime", 0),
                "url":      item.get("url", ""),
            }
            for item in news
        ]
    except Exception as e:
        logger.debug(f"fetch_news_with_details {ticker}: {e}")
        return []


# ── News sentiment cache (5-min TTL) ─────────────────────────
news_sentiment_cache = {}


def _score_news_sentiment(ticker: str) -> dict:
    """
    AI-powered news sentiment scoring (Component 10 of signal engine).
    Fetches recent news, sends to Claude Haiku for sentiment analysis.
    Returns {"sentiment": int, "pts": int, "catalyst": str}.
    Cached for 5 minutes.
    """
    now = time.time()
    cached = news_sentiment_cache.get(ticker)
    if cached and (now - cached["ts"]) < 300:
        return cached

    result = {"sentiment": 0, "pts": 5, "catalyst": "", "ts": now}

    articles = fetch_news_with_details(ticker, count=5)
    if not articles:
        news_sentiment_cache[ticker] = result
        return result

    # Format news as bullet points
    bullets = []
    for a in articles:
        line = f"- {a['headline']}"
        if a["summary"]:
            line += f": {a['summary'][:200]}"
        bullets.append(line)
    news_block = "\n".join(bullets)

    prompt = (
        f"Analyze these news headlines+summaries for {ticker} and score "
        f"the overall sentiment for short-term trading.\n\n"
        f"{news_block}\n\n"
        f"Score from -100 (extremely bearish) to +100 (extremely bullish). "
        f"0 is neutral.\n"
        f"Consider: earnings surprises, analyst upgrades/downgrades, "
        f"product launches, regulatory actions, insider buying/selling, "
        f"sector catalysts.\n\n"
        f"Respond ONLY with: SENTIMENT:<score> CATALYST:<one-line reason>"
    )

    try:
        raw = get_ai_response(prompt, max_tokens=80, fast=True)
        sentiment = 0
        catalyst = ""

        # Parse SENTIMENT:<score>
        if "SENTIMENT:" in raw:
            try:
                sent_part = raw.split("SENTIMENT:")[1].split()[0]
                sentiment = int(sent_part)
                sentiment = max(-100, min(100, sentiment))
            except (ValueError, IndexError):
                pass

        # Parse CATALYST:<reason>
        if "CATALYST:" in raw:
            catalyst = raw.split("CATALYST:")[-1].strip()[:100]

        # Map -100..+100 to 0-15 pts
        if sentiment >= 50:
            pts = 15
        elif sentiment >= 25:
            pts = 12
        elif sentiment >= 10:
            pts = 8
        elif sentiment >= -10:
            pts = 5
        elif sentiment >= -25:
            pts = 2
        else:
            pts = 0

        result = {"sentiment": sentiment, "pts": pts, "catalyst": catalyst, "ts": now}
        logger.info(f"NewsSentiment {ticker}: score={sentiment}, pts={pts}, catalyst={catalyst}")

    except Exception as e:
        logger.debug(f"_score_news_sentiment {ticker}: {e}")

    news_sentiment_cache[ticker] = result
    return result


def get_trading_session():
    """Return current trading session aligned with Robinhood hours.
    All times in CT (Central Time = ET - 1 hour).
      Robinhood regular:  9:30 AM - 4:00 PM ET  →  8:30 - 15:00 CT
      Robinhood extended: 7:00 AM - 8:00 PM ET  →  6:00 - 19:00 CT
    """
    now     = datetime.now(CT)
    if now.weekday() > 4:
        return "closed"
    current = now.time()
    # Robinhood extended hours: 7 AM - 8 PM ET = 6 AM - 7 PM CT
    if datetime.strptime("06:00", "%H:%M").time() <= current < datetime.strptime("19:00", "%H:%M").time():
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
            _quote_cache.put(f"quote:{ticker}", {})  # cache empty for TTL to avoid retry storm
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
            _metrics_cache.put(f"metrics:{ticker}", {})  # cache empty to avoid retry storm
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

    # ── Extended hours: overlay post/pre-market price if available ──
    session = get_trading_session()
    if session in ("extended", "closed"):
        ext = _get_extended_price(ticker)
        if ext and ext.get("price"):
            d["ext_price"] = ext["price"]
            d["ext_change"] = ext.get("change", 0)
            d["ext_change_pct"] = ext.get("change_pct", 0)
            d["ext_session"] = ext.get("session", "")
            d["ext_regular_close"] = ext.get("regular_close", 0)

    return d


def _get_extended_price(ticker: str) -> dict:
    """Get extended hours price data from yfinance.
    Returns dict with keys: price, change, change_pct, regular_close, session, source.
    Returns {} if no extended data available."""
    try:
        info = yf.Ticker(ticker).info
        state = (info.get("marketState") or "").upper()

        if state == "POST" and info.get("postMarketPrice"):
            pm_price = info["postMarketPrice"]
            reg_close = info.get("regularMarketPrice") or info.get("currentPrice") or 0
            return {
                "price": pm_price,
                "change": info.get("postMarketChange", 0),
                "change_pct": info.get("postMarketChangePercent", 0),
                "regular_close": reg_close,
                "session": "After Hours",
                "source": "yfinance",
            }
        elif state == "PRE" and info.get("preMarketPrice"):
            pm_price = info["preMarketPrice"]
            prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose") or 0
            return {
                "price": pm_price,
                "change": info.get("preMarketChange", 0),
                "change_pct": info.get("preMarketChangePercent", 0),
                "regular_close": prev_close,
                "session": "Pre-Market",
                "source": "yfinance",
            }
    except Exception as e:
        logger.debug(f"Extended price {ticker}: {e}")
    return {}


def _get_best_price(ticker: str) -> tuple:
    """Get the best available price: extended hours if available, else Finnhub quote.
    Returns (price, volume, prev_close) like fetch_finnhub_quote."""
    session = get_trading_session()

    # During extended/closed sessions, try extended price first
    if session in ("extended", "closed"):
        ext = _get_extended_price(ticker)
        if ext and ext.get("price"):
            return ext["price"], None, ext.get("regular_close")

    # Fall back to Finnhub (works best during regular hours)
    return fetch_finnhub_quote(ticker)


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
    """Fetch Fear & Greed Index. Primary: CNN real-time (intraday updates).
    Fallback: alternative.me (daily updates, can lag)."""
    # Primary: CNN real-time endpoint (updates every few minutes during market hours)
    try:
        cnn_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.cnn.com/markets/fear-and-greed",
            "Origin": "https://www.cnn.com",
        }
        r = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers=cnn_headers, timeout=10,
        )
        if r.status_code == 200:
            fg = r.json().get("fear_and_greed", {})
            score = fg.get("score")
            rating = fg.get("rating", "")
            if score is not None:
                val = int(round(float(score)))
                label = rating.replace("_", " ").title() if rating else "Unknown"
                logger.debug("F&G from CNN: %s (%s)", val, label)
                return val, label
    except Exception as e:
        logger.debug("CNN F&G failed, trying fallback: %s", e)
    # Fallback: alternative.me (updates once daily)
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        d = r.json()["data"][0]
        return d.get("value"), d.get("value_classification")
    except Exception as e:
        logger.debug("get_fear_greed fallback failed: %s", e)
        return None, None

# Cache for social buzz data (refresh every 5 minutes)
_social_buzz_cache = {"data": {}, "ts": None}

def get_social_buzz(ticker=None):
    """Fetch Reddit social buzz data from ApeWisdom.
    Returns dict mapping ticker -> {mentions, mentions_24h_ago, velocity, rank}.
    If ticker provided, returns that ticker's data or None.
    Caches for 5 minutes."""
    now = datetime.now(CT)
    cache = _social_buzz_cache
    if cache["ts"] and (now - cache["ts"]).total_seconds() < 300:
        if ticker:
            return cache["data"].get(ticker)
        return cache["data"]

    try:
        # Fetch first 2 pages (~200 tickers) - covers all meaningful mentions
        buzz = {}
        for page in range(1, 3):
            r = requests.get(
                f"https://apewisdom.io/api/v1.0/filter/all-stocks/page/{page}",
                timeout=10
            )
            if r.status_code != 200:
                break
            results = r.json().get("results", [])
            for s in results:
                t = s.get("ticker", "")
                m = s.get("mentions", 0)
                m24 = s.get("mentions_24h_ago", 0)
                if m24 > 0:
                    velocity = ((m - m24) / m24) * 100
                elif m > 0:
                    velocity = 999
                else:
                    velocity = 0
                buzz[t] = {
                    "mentions": m,
                    "mentions_24h_ago": m24,
                    "velocity": round(velocity, 1),
                    "rank": s.get("rank", 999),
                    "upvotes": s.get("upvotes", 0),
                }
        cache["data"] = buzz
        cache["ts"] = now
        logger.debug("Social buzz updated: %d tickers", len(buzz))
    except Exception as e:
        logger.debug("Social buzz fetch failed: %s", e)

    if ticker:
        return cache["data"].get(ticker)
    return cache["data"]

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
        # Fix 4: Skip spike detection during startup grace period (primed data → false spikes)
        if (now - _startup_time).total_seconds() < 300:
            last_prices[ticker] = c
            # Fall through to day-change alerts below (they use prev close, not history)
        else:
            old_price = last_prices[ticker]
            for ts, p in list(price_history[ticker]):
                if (now - ts).total_seconds() > 280:
                    old_price = p

            change = (c - old_price) / old_price
            if abs(change) >= THRESHOLD:
                last_alert = last_alert_time.get(ticker, now - timedelta(days=1))
                if (now - last_alert).total_seconds() / 60 >= COOLDOWN_MINUTES:
                    # Fix 3: Suppress spike if day-change alert already covers this direction
                    day_alert_key = f"{ticker}:day:{datetime.now(CT).strftime('%Y-%m-%d')}"
                    if day_alert_key in last_alert_time:
                        day_pct = last_alert_pct.get(day_alert_key, 0)
                        day_direction = "up" if day_pct > 0 else "down"
                        spike_direction = "up" if change > 0 else "down"
                        if day_direction == spike_direction:
                            last_prices[ticker] = c
                            return  # Already covered by MOVER alert

                    # Fix 2: Suppress if move hasn't grown by at least 1% since last alert
                    prev_pct = last_alert_pct.get(ticker, 0)
                    if abs(change * 100) < abs(prev_pct) + 1.0:
                        pass  # No new info — skip alert
                    else:
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
                        last_alert_pct[ticker] = change * 100

    # ── Day-change alerts: catch sustained moves vs prev close ──
    # During extended hours, use extended price to catch big AH/PM moves
    day_c = c
    if get_trading_session() == "extended":
        ext = _get_extended_price(ticker)
        if ext and ext.get("price"):
            day_c = ext["price"]

    if pc and pc > 0:
        day_change = (day_c - pc) / pc
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
                # v2.7.9: batch day-change movers instead of individual alerts
                _pending_mover_alerts.append({
                    "ticker": ticker,
                    "pct": day_change * 100,
                    "price": day_c,
                    "vol_spike": vol_spike,
                })
                last_alert_pct[day_alert_key] = day_change * 100

    last_prices[ticker] = c


def _flush_mover_alerts():
    """Send batched mover alerts as compact messages."""
    alerts = list(_pending_mover_alerts)
    if not alerts:
        return

    if len(alerts) == 1:
        # Single alert: use existing format but shorter
        a = alerts[0]
        vol_tag = " Vol" if a["vol_spike"] else ""
        message = (
            f"MOVER: {a['ticker']} {a['pct']:+.1f}%"
            f" (${a['price']:.2f}){vol_tag}"
        )
        send_telegram(message)
    else:
        # Multiple alerts: compact table
        # Sort by absolute change descending
        alerts.sort(key=lambda x: abs(x["pct"]), reverse=True)
        header = f"MOVERS ({len(alerts)} stocks):"
        lines = [header]
        for a in alerts[:10]:  # cap at 10 in one message
            vol_tag = " V" if a["vol_spike"] else ""
            lines.append(
                f"  {a['ticker']:>6} {a['pct']:+6.1f}%"
                f" ${a['price']:>8.2f}{vol_tag}"
            )
        if len(alerts) > 10:
            remaining = len(alerts) - 10
            lines.append(f"  ... +{remaining} more")
        message = "\n".join(lines)
        send_telegram(message)

    # Update recent_alerts for all
    for a in alerts:
        ts_str = datetime.now(CT).strftime('%H:%M')
        recent_alerts.append(
            f"{a['ticker']} {a['pct']:+.1f}% day at {ts_str}"
        )

    global daily_alerts
    daily_alerts += len(alerts)


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

    # v2.7.9: Flush batched mover alerts as compact message
    if _pending_mover_alerts:
        try:
            _flush_mover_alerts()
        except Exception as e:
            logger.error(f"_flush_mover_alerts error: {e}")
        _pending_mover_alerts.clear()

    # Paper trading evaluation runs after every scan cycle
    try:
        paper_scan()
    except Exception as e:
        logger.error(f"paper_scan error: {e}")

    # VIX put-selling alert check
    try:
        check_vix_put_alert()
    except Exception as e:
        logger.error(f"check_vix_put_alert error: {e}")

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
#   • BUY  when composite ≥ adaptive threshold and RSI < 72 and cash available
#   • SELL on 3% trailing stop from high-water, 6% hard stop from entry,
#     10% take-profit, or signal collapse (≤30 + positive)
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
PAPER_TRAILING_STOP_PCT = 0.03    # 3% base trailing stop from high-water mark
PAPER_STOP_LOSS_PCT     = 0.06    # 6% hard stop from entry (safety net)
PAPER_TAKE_PROFIT_PCT   = 0.10    # legacy — used as graduated trail reference

# ── Graduated trailing stop zones ─────────────────────────────
# Instead of a fixed take-profit exit, the trail widens as
# profit grows so big winners can run while locking in gains.
GRADUATED_TRAIL_ZONES = [
    # (min_pnl_pct, trail_pct)  — checked top-down
    (0.15, 0.06),   # 15%+ profit → 6% trail (wide, let it run)
    (0.10, 0.05),   # 10-15%      → 5% trail
    (0.05, 0.04),   # 5-10%       → 4% trail (lock some gains)
]
# Below 5% profit: use PAPER_TRAILING_STOP_PCT (default 3%)


def _graduated_trail_pct(pnl_pct: float) -> float:
    """Return the trailing stop % based on current profit zone.
    Higher profits get a wider trail to let winners run."""
    for threshold, trail in GRADUATED_TRAIL_ZONES:
        if pnl_pct >= threshold:
            return trail
    return PAPER_TRAILING_STOP_PCT  # base trail for <5%

PAPER_MIN_SIGNAL       = 65       # min composite score (0-140) to open a position
PAPER_MIN_HOLD_MINUTES = 30       # v2.7.2: minimum hold before non-hard-stop exits

# ── Intraday time-of-day adjustments ─────────────────────────
# U-shaped volume/volatility pattern: high at open & close,
# low during lunch. Signals during high-volume windows are
# more reliable; midday signals carry more false-breakout risk.
# All times in CT (Central Time = ET - 1 hour).
INTRADAY_ZONES = {
    # (CT start, CT end): (signal_pts, pos_size_mult, label)
    "cool_off":    ("08:30", "08:45", -99, 0.00, "CoolOff"),    # v2.7.10: block entries first 15 min
    "power_open":  ("08:45", "09:30", 5, 0.90, "PowerOpen"),    # v2.7.10: reduced from +8/1.00
    "morning":     ("09:30", "10:30", 3, 0.90, "Morning"),
    "transition1": ("10:30", "11:00", 0, 0.85, "Transition"),
    "lunch":       ("11:00", "13:00",-8, 0.65, "LunchLull"),
    "transition2": ("13:00", "14:00",-3, 0.80, "Transition"),
    "afternoon":   ("14:00", "14:30", 3, 0.90, "Afternoon"),
    "power_close": ("14:30", "15:00", 6, 1.00, "PowerClose"),
}
# Extended hours (pre/post market): no modifier — volume is
# naturally thin but we already gate on session type elsewhere.


def _get_intraday_zone() -> tuple:
    """Return (signal_pts, size_mult, label) for current
    time of day. Returns (0, 0.85, 'Extended') outside
    regular hours."""
    now = datetime.now(CT)
    ct = now.strftime("%H:%M")
    for _name, (start, end, pts, mult, label) in (
        INTRADAY_ZONES.items()
    ):
        if start <= ct < end:
            return pts, mult, label
    return 0, 0.85, "Extended"

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

# ── Adaptive threshold cache (Feature #2) ──────────────────
_adaptive_threshold_cache = {"val": 65, "ts": datetime.min.replace(tzinfo=CT)}

# ── Persistent user config (restored from paper_state.json) ──
DEFAULT_CONFIG = {
    "stop_loss": 0.06,
    "take_profit": 0.10,
    "trailing": 0.03,
    "max_positions": 8,
    "threshold": 65,
    "auto_adjust": True,
    "trading_mode": "paper",
}
user_config = dict(DEFAULT_CONFIG)

# ── TradersPost (TP) portfolio state ─────────────────────────

def _default_shadow_portfolio():
    """Return a fresh TP portfolio dict."""
    return {
        "cash": PAPER_STARTING_CAPITAL,
        "starting_cash": PAPER_STARTING_CAPITAL,
        "positions": {},
        "closed_trades": [],
        "total_value_estimate": PAPER_STARTING_CAPITAL,
        "last_sync_check": None,
    }

tp_state = {
    "pending_settlements": [],
    "total_orders_sent": 0,
    "total_orders_success": 0,
    "total_orders_failed": 0,
    "last_order_time": None,
    "recent_orders": [],
    "shadow_portfolio": _default_shadow_portfolio(),
}

# ── TP DM chat ID (auto-captured from user's first command) ──
tp_dm_chat_id = None

# ── Portfolio snapshots for intraday chart (Feature #7) ────
_portfolio_snapshots = []  # [(datetime, value), ...]
_last_snapshot_time = datetime.min.replace(tzinfo=CT)

# ── Morning value capture for daily P&L (Feature #6) ──────
_paper_morning_value = None  # captured at 8:31 AM CT


# ── v2.7.0: Gap Analysis State ──────────────────────────────
# Per-ticker re-entry cooldown (Rec #4)
_ticker_cooldowns = {}  # {ticker: {"last_sell": datetime, "was_loss": bool}}

# Market regime cache (Rec #5) — refreshed every 15 min
_market_regime_cache = {
    "regime": "unknown", "confidence": 0.0,
    "params": {"threshold_adjust": 0, "max_positions_adjust": 0,
               "stop_multiplier": 1.0, "size_multiplier": 1.0},
    "ts": None,
    "vix": None, "spy": None, "sma_20": None, "sma_50": None,
}

# Signal component weights (Rec #6) — refreshed every 24h
_signal_weights = {}  # component -> multiplier (0.5-1.5)
_signal_weights_ts = None  # last recalc time

# ATR cache (Rec #1, #2, #3) — 5-min TTL
_atr_cache = {}  # {ticker: {"atr": float, "ts": float}}
_ATR_CACHE_TTL = 300  # 5 minutes

# Daily returns cache for correlation (Rec #7) — 1-hour TTL
_daily_returns_cache = {}  # {ticker: {"returns": list, "ts": float}}
_RETURNS_CACHE_TTL = 3600  # 1 hour

# Portfolio heat constant (Rec #3)
PORTFOLIO_HEAT_LIMIT = 30.0  # v2.7.5: was 6% — scaled for 5% risk per trade

# Re-entry cooldown hours (Rec #4)
COOLDOWN_HOURS_LOSS = 12  # v2.7.2: was 8h — reduce churn
COOLDOWN_HOURS_WIN = 6   # v2.7.2: was 4h — reduce churn

# ── Earnings cache (Feature #9) ──────────────────────────
_earnings_cache = {}  # {ticker: {"has_earnings": bool, "ts": float}}

# ── User config overrides (Feature #12) ──────────────────
_user_config = {}

_paper_save_lock = threading.Lock()


# ── Signal Data Logger ────────────────────────────────────────────────
# Records every signal evaluation for future backtesting replay.
# Format: JSONL (one JSON object per line), append-only.
# Each entry captures: price, all indicator values, composite score,
# adaptive params (F&G, VIX, threshold), action taken, and OHLCV.
# Future backtests can replay with different parameters using this data
# instead of re-fetching from APIs.

def log_signal_data(entry: dict):
    """Append a signal data entry to the JSONL log file."""
    try:
        line = json.dumps(entry, default=str) + "\n"
        with _signal_log_lock:
            with open(SIGNAL_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:
        logger.debug(f"Signal log write error: {e}")


def trim_signal_log(keep_days: int = 30):
    """Remove signal log entries older than keep_days. Called in morning reset."""
    try:
        if not os.path.exists(SIGNAL_LOG_FILE):
            return
        cutoff = (datetime.now(CT) - timedelta(days=keep_days)).isoformat()
        kept = []
        with open(SIGNAL_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("ts", "") >= cutoff:
                        kept.append(line)
                except json.JSONDecodeError:
                    continue
        with _signal_log_lock:
            with open(SIGNAL_LOG_FILE, "w", encoding="utf-8") as f:
                for line in kept:
                    f.write(line + "\n")
        logger.info(f"Signal log trimmed: kept {len(kept)} entries (cutoff {keep_days}d)")
    except Exception as e:
        logger.error(f"trim_signal_log error: {e}")


def load_signal_log(days: int = None) -> list:
    """Load signal log entries, optionally filtered to last N days."""
    entries = []
    try:
        if not os.path.exists(SIGNAL_LOG_FILE):
            return entries
        cutoff = None
        if days:
            cutoff = (datetime.now(CT) - timedelta(days=days)).isoformat()
        with open(SIGNAL_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if cutoff and entry.get("ts", "") < cutoff:
                        continue
                    entries.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.error(f"load_signal_log error: {e}")
    return entries


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
        "user_config":        user_config,
        "tp_state":           tp_state,
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
    global user_config, tp_state, tp_dm_chat_id
    global PAPER_STOP_LOSS_PCT, PAPER_TAKE_PROFIT_PCT, PAPER_TRAILING_STOP_PCT
    global PAPER_MAX_POSITIONS, PAPER_MIN_SIGNAL

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

        # Migrate existing positions: ensure each has a "high" key for trailing stop
        for _t, _pos in paper_positions.items():
            if "high" not in _pos:
                _pos["high"] = max(_pos.get("avg_cost", 0), _pos.get("entry_price", _pos.get("avg_cost", 0)))

        # Restore user config (persisted /set values)
        user_config = state.get("user_config", dict(DEFAULT_CONFIG))
        # Migrate: ensure trading_mode exists in restored config
        if "trading_mode" not in user_config:
            user_config["trading_mode"] = "paper"
        PAPER_STOP_LOSS_PCT = user_config["stop_loss"]
        PAPER_TAKE_PROFIT_PCT = user_config["take_profit"]
        PAPER_TRAILING_STOP_PCT = user_config["trailing"]
        PAPER_MAX_POSITIONS = user_config["max_positions"]
        PAPER_MIN_SIGNAL = user_config["threshold"]

        # Restore TradersPost state
        tp_state = state.get("tp_state", {
            "pending_settlements": [],
            "total_orders_sent": 0,
            "total_orders_success": 0,
            "total_orders_failed": 0,
            "last_order_time": None,
            "recent_orders": [],
            "shadow_portfolio": _default_shadow_portfolio(),
        })
        # Migrate: ensure TP portfolio exists
        if "shadow_portfolio" not in tp_state:
            tp_state["shadow_portfolio"] = _default_shadow_portfolio()

        # Restore TP DM chat ID
        if tp_state.get("dm_chat_id"):
            tp_dm_chat_id = tp_state["dm_chat_id"]
            logger.info(f"[TP] Restored DM chat ID: {tp_dm_chat_id}")

        # Purge stale settlements on startup
        _today_str = datetime.now(CT).strftime("%Y-%m-%d")
        _old_pending = tp_state.get("pending_settlements", [])
        _still = [p for p in _old_pending
                  if p.get("settles_on", "") > _today_str]
        _purged = len(_old_pending) - len(_still)
        if _purged > 0:
            _purged_amt = sum(
                p["amount"] for p in _old_pending
                if p.get("settles_on", "") <= _today_str
            )
            tp_state["pending_settlements"] = _still
            logger.info(
                f"[TP] Settlement cleanup: purged "
                f"{_purged} settled entries "
                f"(${_purged_amt:,.0f}), "
                f"{len(_still)} still pending"
            )

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
        tp_state = {
            "pending_settlements": [],
            "total_orders_sent": 0,
            "total_orders_success": 0,
            "total_orders_failed": 0,
            "last_order_time": None,
            "recent_orders": [],
            "shadow_portfolio": _default_shadow_portfolio(),
        }



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
        "last_alert_time": {k: v.isoformat() if isinstance(v, datetime) else str(v) for k, v in last_alert_time.items()},
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

        raw_lat = state.get("last_alert_time", {})
        for k, v in raw_lat.items():
            try:
                last_alert_time[k] = datetime.fromisoformat(v)
            except (ValueError, TypeError):
                pass

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


# ============================================================
# TRADERSPOST PORTFOLIO TRACKING
# ============================================================

def tp_log(message: str):
    """Log TradersPost events to TP channel."""
    logger.info(f"[TP] {message}")
    send_tp_telegram(f"📡 {message}")



def _tp_portfolio_stats_msg() -> str:
    """Build a compact TP portfolio stats summary."""
    sp = tp_state.get("shadow_portfolio", _default_shadow_portfolio())
    positions = sp.get("positions", {})
    cash = sp.get("cash", 0)
    # Calculate total value with current prices where possible
    pos_value = 0
    pos_lines = []
    for tk, p in sorted(positions.items()):
        shares = p.get("shares", 0)
        entry_px = p.get("avg_price", 0)
        cost = shares * entry_px
        # Try to get current price
        try:
            cur_px, _, _ = fetch_finnhub_quote(tk)
        except Exception:
            cur_px = entry_px
        if not cur_px:
            cur_px = entry_px
        mkt_val = shares * cur_px
        pos_value += mkt_val
        pnl = mkt_val - cost
        pnl_pct = (pnl / cost * 100) if cost > 0 else 0
        sign = "+" if pnl >= 0 else ""
        pos_lines.append(
            f"  {tk}: {shares}sh ${mkt_val:,.0f}"
            f" ({sign}{pnl_pct:.1f}%)"
        )
    total = cash + pos_value
    starting = sp.get("starting_capital", 100000)
    total_pnl = total - starting
    total_pct = (total_pnl / starting * 100) if starting > 0 else 0
    closed = sp.get("closed_trades", [])
    wins = sum(1 for t in closed if t.get("pnl", 0) > 0)
    losses = sum(1 for t in closed if t.get("pnl", 0) <= 0)
    SEP = chr(9472) * 28
    lines = [
        f"{SEP}",
        f"TP Portfolio Stats",
        f"{SEP}",
    ]
    if pos_lines:
        lines.extend(pos_lines)
        lines.append(f"{SEP}")
    lines.extend([
        f"Positions: {len(positions)}",
        f"Cash:      ${cash:,.0f}",
        f"Value:     ${total:,.0f}",
        f"P&L:       ${total_pnl:+,.0f} ({total_pct:+.1f}%)",
        f"W/L:       {wins}/{losses}",
    ])
    return "\n".join(lines)

def update_shadow_portfolio(ticker, action, price, quantity_dollars, success):
    """Update the TP portfolio after a TradersPost webhook call.

    - On success: update TP positions/cash to reflect the trade.
    - On failure: log a warning; still update on EXIT to keep
      TP portfolio accurate (the exit intent was acted on).
    """
    sp = tp_state.setdefault("shadow_portfolio",
                             _default_shadow_portfolio())
    now = datetime.now(CT)
    today = now.strftime("%Y-%m-%d")
    now_hm = now.strftime("%H:%M")

    if not success:
        tp_log(
            f"⚠️ {action.upper()} {ticker} sent but "
            "webhook failed — paper has position, "
            "TradersPost may not"
        )
        # Still update TP on EXIT failures to keep portfolio
        # accurate — the exit intent was acted on by the scanner.
        if action == "exit":
            positions = sp.get("positions", {})
            pos = positions.pop(ticker, None)
            if pos:
                shares = pos.get("shares", 0)
                proceeds = round(shares * price, 2)
                sp["cash"] = round(sp.get("cash", 0) + proceeds, 2)
                tp_log(
                    f"TP EXIT (forced): {ticker} "
                    f"${proceeds:,.0f} returned to cash"
                )
        save_paper_state()
        return

    if action == "buy" and quantity_dollars and price > 0:
        shares = math.floor(quantity_dollars / price)
        if shares < 1:
            tp_log(
                f"TP BUY skipped: {ticker} — "
                f"${quantity_dollars:,.0f} not enough for 1 share @ ${price:,.2f}"
            )
            return
        actual_cost = round(shares * price, 2)

        # Guard: don't let TP cash go negative
        current_cash = sp.get("cash", 0)
        if actual_cost > current_cash:
            tp_log(
                f"⚠️ TP BUY {ticker} capped: "
                f"cost ${actual_cost:,.0f} > cash ${current_cash:,.0f}"
            )
            # Scale down to what cash allows
            shares = math.floor(current_cash * 0.95 / price)
            if shares < 1:
                tp_log(f"TP BUY {ticker} skipped — insufficient cash")
                return
            actual_cost = round(shares * price, 2)

        sp["cash"] = round(sp.get("cash", 0) - actual_cost, 2)
        sp.setdefault("positions", {})[ticker] = {
            "shares": shares,
            "avg_price": round(price, 2),
            "entry_date": today,
            "entry_time": now_hm,
            "dollar_amount": actual_cost,
        }
        tp_log(
            f"TP BUY: {shares} shares of "
            f"{ticker} @ ${price:,.2f} "
            f"(${actual_cost:,.0f} allocated)"
        )
        # v2.7.2: Print portfolio stats after every action
        try:
            tp_log(_tp_portfolio_stats_msg())
        except Exception as e:
            logger.debug(f"TP stats after BUY: {e}")

    elif action == "exit":
        positions = sp.get("positions", {})
        pos = positions.pop(ticker, None)
        if pos:
            shares = pos.get("shares", 0)
            entry_px = pos.get("avg_price", price)
            proceeds = round(shares * price, 2)
            pnl = round(proceeds - pos.get("dollar_amount",
                                           shares * entry_px), 2)
            sp["cash"] = sp.get("cash", 0) + proceeds
            closed = {
                "ticker": ticker,
                "shares": shares,
                "entry_price": entry_px,
                "exit_price": round(price, 2),
                "pnl": pnl,
                "entry_date": pos.get("entry_date", ""),
                "exit_date": today,
                "exit_time": now_hm,
            }
            sp.setdefault("closed_trades", []).append(closed)
            if len(sp["closed_trades"]) > 50:
                sp["closed_trades"] = sp["closed_trades"][-50:]
            sign = "+" if pnl >= 0 else ""
            # v2.7.3: Calculate hold duration for TP EXIT
            _tp_hold_str = ""
            _tp_entry_d = pos.get("entry_date", "")
            _tp_entry_t = pos.get("entry_time", "")
            if _tp_entry_d and _tp_entry_t:
                try:
                    _tp_entry_dt = datetime.strptime(
                        f"{_tp_entry_d} {_tp_entry_t}",
                        "%Y-%m-%d %H:%M"
                    ).replace(tzinfo=CT)
                    _tp_held_s = int((now - _tp_entry_dt).total_seconds())
                    if _tp_held_s < 3600:
                        _tp_hold_str = f" held {_tp_held_s // 60}m"
                    elif _tp_held_s < 86400:
                        _tp_hold_str = f" held {_tp_held_s // 3600}h{(_tp_held_s % 3600) // 60}m"
                    else:
                        _tp_hold_str = f" held {_tp_held_s // 86400}d{(_tp_held_s % 86400) // 3600}h"
                except Exception:
                    pass
            tp_log(
                f"TP EXIT: {ticker} ~{shares:.2f} "
                f"shares @ ${price:,.2f} "
                f"(est P&L: {sign}${pnl:,.2f}){_tp_hold_str}"
            )
            # v2.7.2: Print portfolio stats after every action
            try:
                tp_log(_tp_portfolio_stats_msg())
            except Exception as e:
                logger.debug(f"TP stats after EXIT: {e}")
        else:
            tp_log(
                f"TP EXIT: {ticker} — no TP "
                "position found (already synced?)"
            )

    # Update total value estimate
    pos_value = sum(
        p.get("shares", 0) * p.get("avg_price", 0)
        for p in sp.get("positions", {}).values()
    )
    sp["total_value_estimate"] = round(
        sp.get("cash", 0) + pos_value, 2
    )
    sp["last_sync_check"] = now.isoformat()
    save_paper_state()


# Limit order buffer: how much above/below current price to set limit
# 0.5% buffer gives room for normal spread while preventing runaway fills
LIMIT_ORDER_BUY_BUFFER  = 0.005   # +0.5% above current price for buys
LIMIT_ORDER_SELL_BUFFER = 0.005   # -0.5% below current price for sells


def send_traderspost_order(ticker, action, signal_score, price, quantity_dollars=None):
    """
    Send a LIMIT order to TradersPost via webhook POST.
    All orders use limit pricing to prevent slippage and comply with
    Robinhood's extended-hours requirement (no market orders).
    action: "buy" or "exit"
    Returns response dict or None on failure.
    """
    if not TRADERSPOST_WEBHOOK_URL:
        logger.warning("[TP] No TRADERSPOST_WEBHOOK_URL configured")
        return None

    now = datetime.now(CT)
    tp_state["total_orders_sent"] = tp_state.get("total_orders_sent", 0) + 1

    # Calculate limit price with buffer
    # BUY:  limit slightly above current price (willing to pay up to +0.5%)
    # EXIT: limit slightly below current price (willing to sell down to -0.5%)
    if action == "buy":
        limit_price = round(price * (1 + LIMIT_ORDER_BUY_BUFFER), 2)
    else:
        limit_price = round(price * (1 - LIMIT_ORDER_SELL_BUFFER), 2)

    # Build payload — always use limit orders
    payload = {
        "ticker": ticker,
        "action": action,
        "orderType": "limit",
        "limitPrice": limit_price,
    }

    if action == "buy" and quantity_dollars:
        shares = math.floor(quantity_dollars / limit_price) if limit_price > 0 else 0
        if shares < 1:
            logger.warning(
                f"[TP] Skipping BUY {ticker}: calculated {shares} shares "
                f"(${quantity_dollars:,.0f} / ${limit_price:,.2f} = {quantity_dollars/limit_price:.2f})"
            )
            return None
        payload["quantityType"] = "fixed_quantity"
        payload["quantity"] = shares

    # Extended hours during pre/post-market
    session = get_trading_session()
    if session == "extended":
        payload["extendedHours"] = True

    try:
        logger.info(f"[TP] Sending LIMIT {action.upper()} {ticker} ({payload.get('quantity', '')} shares) @ ${limit_price:.2f} (mkt ${price:.2f}): {json.dumps(payload)}")
        r = requests.post(
            TRADERSPOST_WEBHOOK_URL,
            json=payload,
            timeout=10,
        )
        resp = r.json()
        logger.info(f"[TP] Response {r.status_code}: {resp}")

        if resp.get("success"):
            tp_state["total_orders_success"] = tp_state.get("total_orders_success", 0) + 1
        else:
            tp_state["total_orders_failed"] = tp_state.get("total_orders_failed", 0) + 1

        tp_state["last_order_time"] = now.isoformat()

        # Store in recent orders (keep last 20)
        order_record = {
            "ticker": ticker,
            "action": action.upper(),
            "price": price,
            "limit_price": limit_price,
            "dollars": quantity_dollars,
            "success": resp.get("success", False),
            "time": now.isoformat(),
            "response_id": resp.get("id"),
        }
        recent = tp_state.get("recent_orders", [])
        recent.append(order_record)
        if len(recent) > 20:
            recent[:] = recent[-20:]
        tp_state["recent_orders"] = recent

        save_paper_state()
        return resp

    except Exception as e:
        logger.error(f"[TP] Webhook failed for {action} {ticker}: {e}")
        tp_state["total_orders_failed"] = tp_state.get("total_orders_failed", 0) + 1
        tp_state["last_order_time"] = now.isoformat()

        order_record = {
            "ticker": ticker,
            "action": action.upper(),
            "price": price,
            "limit_price": limit_price,
            "dollars": quantity_dollars,
            "success": False,
            "time": now.isoformat(),
            "error": str(e),
        }
        recent = tp_state.get("recent_orders", [])
        recent.append(order_record)
        if len(recent) > 20:
            recent[:] = recent[-20:]
        tp_state["recent_orders"] = recent

        save_paper_state()
        return None


# ── T+1 Settlement Tracker (Cash Account) ────────────────────
# Cash account: no PDT restrictions, but can't trade with
# unsettled funds.  Stock/option sales settle T+1.

def _next_business_day(from_date=None):
    """Return the next business day after from_date."""
    d = from_date or datetime.now(CT).date()
    d += timedelta(days=1)
    while d.weekday() >= 5:  # skip weekends
        d += timedelta(days=1)
    return d


def record_settlement(ticker, amount):
    """Record unsettled funds from a sell. Settles T+1."""
    now = datetime.now(CT)
    settles_on = _next_business_day(now.date())
    pending = tp_state.setdefault("pending_settlements", [])
    pending.append({
        "ticker": ticker,
        "amount": round(amount, 2),
        "sell_date": now.strftime("%Y-%m-%d"),
        "sell_time": now.strftime("%H:%M"),
        "settles_on": settles_on.strftime("%Y-%m-%d"),
    })
    save_paper_state()


def get_settled_cash():
    """Return (settled_cash, unsettled_total, pending_items).
    Cleans up already-settled entries."""
    today = datetime.now(CT).strftime("%Y-%m-%d")
    pending = tp_state.get("pending_settlements", [])
    # Separate settled vs still pending
    still_pending = []
    for p in pending:
        if p.get("settles_on", "") <= today:
            pass  # settled — drop from list
        else:
            still_pending.append(p)
    tp_state["pending_settlements"] = still_pending
    unsettled = sum(p["amount"] for p in still_pending)
    sp = tp_state.get("shadow_portfolio",
                       _default_shadow_portfolio())
    total_cash = sp.get("cash", 0)
    settled = max(0, total_cash - unsettled)
    return settled, unsettled, still_pending


def paper_portfolio_value() -> float:
    """Total portfolio value: cash + market value of all open positions."""
    total = paper_cash
    for ticker, pos in paper_positions.items():
        try:
            price, _, _ = _get_best_price(ticker)
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


# ── Feature #2: Adaptive config (threshold + all trading params) ──
def _apply_adaptive_config() -> int:
    """Auto-adjust trading parameters based on market regime (F&G + VIX).
    Called every 5 minutes during market hours (cached).
    Only adjusts if user_config["auto_adjust"] is True.
    Returns the adaptive threshold value (same interface as old _get_adaptive_threshold).
    """
    global PAPER_STOP_LOSS_PCT, PAPER_TAKE_PROFIT_PCT, PAPER_TRAILING_STOP_PCT
    global PAPER_MAX_POSITIONS, PAPER_MIN_SIGNAL
    global _adaptive_threshold_cache

    if not user_config.get("auto_adjust", True):
        return _adaptive_threshold_cache["val"]

    now = datetime.now(CT)
    if (now - _adaptive_threshold_cache["ts"]).total_seconds() < 300:
        return _adaptive_threshold_cache["val"]

    fg_val, _ = get_fear_greed()
    fg = int(fg_val) if fg_val else 50

    try:
        vix_q = _finnhub_quote("^VIX") or {}
        vix = vix_q.get("c", 20) or 20
    except Exception:
        vix = 20

    # ── Threshold ─────────────────────────────────────
    base_threshold = user_config["threshold"]
    threshold = base_threshold
    if fg >= 75:    threshold += 10
    elif fg >= 60:  threshold += 5
    elif fg <= 25:  threshold -= 10
    elif fg <= 40:  threshold -= 5
    if vix >= 30:   threshold += 5
    elif vix <= 15: threshold -= 3
    threshold = max(70, min(85, threshold))  # v2.7.6: floor 70 (was 60) — stop marginal entries in fear

    # v2.7.0: Multi-regime adjustment
    regime = _classify_market_regime()
    regime_adj = regime["params"].get("threshold_adjust", 0)
    threshold = max(70, min(90, threshold + regime_adj))  # v2.7.6: floor 70 here too

    # ── Take Profit (legacy — graduated trail replaces)
    # No longer adjusts TP; kept for backcompat config.

    # ── Stop Loss ─────────────────────────────────────
    base_sl = user_config["stop_loss"]
    if vix >= 30:     sl = base_sl * 1.3
    elif vix >= 25:   sl = base_sl * 1.15
    elif vix <= 15:   sl = base_sl * 0.85
    else:             sl = base_sl
    PAPER_STOP_LOSS_PCT = round(max(0.03, min(0.12, sl)), 3)

    # ── Trailing Stop ─────────────────────────────────
    base_trail = user_config["trailing"]
    if vix >= 30:     trail = base_trail * 1.3
    elif vix >= 25:   trail = base_trail * 1.15
    elif vix <= 15:   trail = base_trail * 0.85
    else:             trail = base_trail
    PAPER_TRAILING_STOP_PCT = round(max(0.02, min(0.08, trail)), 3)

    # ── Max Positions ─────────────────────────────────
    base_max = user_config["max_positions"]
    if fg <= 25:      max_pos = min(base_max + 3, 15)
    elif fg <= 40:    max_pos = min(base_max + 1, 12)
    elif fg >= 75:    max_pos = max(base_max - 2, 4)
    elif fg >= 60:    max_pos = max(base_max - 1, 5)
    else:             max_pos = base_max
    # v2.7.0: Regime-adjusted max positions
    regime_pos_adj = regime["params"].get("max_positions_adjust", 0)
    max_pos = max(3, min(15, max_pos + regime_pos_adj))
    PAPER_MAX_POSITIONS = max_pos

    PAPER_MIN_SIGNAL = threshold

    _adaptive_threshold_cache = {"val": threshold, "ts": now}

    logger.info(
        f"Adaptive config: thresh={threshold} "
        f"SL={PAPER_STOP_LOSS_PCT*100:.1f}% trail={PAPER_TRAILING_STOP_PCT*100:.1f}% "
        f"max_pos={PAPER_MAX_POSITIONS} (F&G={fg} VIX={vix:.1f})"
    )

    return threshold


# ── Feature #4: Support/Resistance ──────────────────────────
def _compute_support_resistance(ticker: str) -> dict:
    """Compute basic support/resistance from daily candles."""
    candles = daily_candles.get(ticker)
    if not candles or len(candles) < 10:
        return {"support": None, "resistance": None, "pivot": None}

    highs = [c["high"] for c in candles[-20:]]
    lows = [c["low"] for c in candles[-20:]]

    resistance = max(highs)
    support = min(lows)

    last = candles[-1]
    pivot = (last["high"] + last["low"] + last["close"]) / 3

    return {
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "pivot": round(pivot, 2),
    }


# ── Feature #5: Sector maps ────────────────────────────────
TICKER_SECTORS = {
    "AAPL": "Technology", "MSFT": "Technology", "GOOGL": "Technology",
    "AMZN": "Consumer Discretionary", "META": "Technology", "NVDA": "Technology",
    "TSLA": "Consumer Discretionary", "AMD": "Technology", "INTC": "Technology",
    "NFLX": "Communication", "DIS": "Communication",
    "JPM": "Financials", "BAC": "Financials", "GS": "Financials",
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy",
    "JNJ": "Healthcare", "UNH": "Healthcare", "PFE": "Healthcare",
    "BA": "Industrials", "CAT": "Industrials", "GE": "Industrials",
    "WMT": "Consumer Staples", "PG": "Consumer Staples", "KO": "Consumer Staples",
    "AVGO": "Technology", "QCOM": "Technology", "MU": "Technology",
    "ARM": "Technology", "SMCI": "Technology", "PLTR": "Technology",
    "SOFI": "Financials", "HIMS": "Healthcare",
    "RIVN": "Consumer Discretionary", "NIO": "Consumer Discretionary",
    "LCID": "Consumer Discretionary", "MARA": "Financials",
    "AMC": "Communication", "GME": "Consumer Discretionary",
    "BYND": "Consumer Staples", "AAL": "Industrials",
}

SECTOR_ETF = {
    "Technology": "XLK", "Financials": "XLF", "Energy": "XLE",
    "Healthcare": "XLV", "Industrials": "XLI", "Communication": "XLC",
    "Consumer Discretionary": "XLY", "Consumer Staples": "XLP",
    "Materials": "XLB", "Real Estate": "XLRE", "Utilities": "XLU",
}


# ── Feature #9: Earnings proximity guard ────────────────────
def _has_upcoming_earnings(ticker: str, days: int = 2) -> bool:
    """Check if ticker has earnings within N days. Cached 12 hours."""
    now = time.time()
    cached = _earnings_cache.get(ticker)
    if cached and (now - cached["ts"]) < 43200:  # 12 hours
        return cached["has_earnings"]

    result = False
    try:
        today = datetime.now(CT).date()
        end = today + timedelta(days=days)
        if FINNHUB_TOKEN and _finnhub_limiter.acquire(timeout=2):
            r = requests.get(
                f"https://finnhub.io/api/v1/calendar/earnings"
                f"?from={today}&to={end}&symbol={ticker}&token={FINNHUB_TOKEN}",
                timeout=5
            )
            if r.status_code == 200:
                cal = r.json().get("earningsCalendar", [])
                result = any(e.get("symbol") == ticker for e in cal)
    except Exception as e:
        logger.debug(f"Earnings check {ticker}: {e}")

    _earnings_cache[ticker] = {"has_earnings": result, "ts": now}
    return result


# ── Feature #11: Anchored VWAP (open-session VWAP) ─────────────────
def compute_avwap(ticker: str) -> dict:
    """
    Compute the Anchored VWAP (volume-weighted average price) for the current
    session, anchored to the 9:30 AM ET market open.

    Uses Finnhub 5-minute candles from today's session.  Each candle's
    typical price = (high + low + close) / 3, weighted by its volume.

    Returns:
        {"avwap": float,            # the VWAP value
         "price": float,            # current price
         "reclaimed": bool,         # True if price > AVWAP
         "pct_from_avwap": float,   # (price - avwap) / avwap * 100
         "ok": bool}                # True if enough data to compute
    """
    result = {"avwap": 0, "price": 0, "reclaimed": False,
              "pct_from_avwap": 0, "ok": False}
    try:
        # Fetch today's 5-min candles (enough for a full session)
        candles = _finnhub_candles(ticker, resolution="5", count=80)
        if not candles or len(candles) < 3:
            return result

        # Filter to today's session only (candles anchored to today 9:30 ET)
        et = pytz.timezone("US/Eastern")
        today_et = datetime.now(et).date()
        market_open_ts = int(
            et.localize(datetime(today_et.year, today_et.month, today_et.day, 9, 30))
            .timestamp()
        )

        session_candles = [c for c in candles if c["t"] >= market_open_ts]
        if len(session_candles) < 2:
            return result

        # Compute VWAP:  sum(typical_price * volume) / sum(volume)
        cum_tp_vol = 0.0
        cum_vol    = 0.0
        for c in session_candles:
            tp = (c["h"] + c["l"] + c["c"]) / 3.0
            v  = c["v"]
            if v > 0:
                cum_tp_vol += tp * v
                cum_vol    += v

        if cum_vol <= 0:
            return result

        avwap = cum_tp_vol / cum_vol
        price = session_candles[-1]["c"]

        pct = (price - avwap) / avwap * 100 if avwap > 0 else 0

        result["avwap"]          = round(avwap, 4)
        result["price"]          = price
        result["reclaimed"]      = price > avwap
        result["pct_from_avwap"] = round(pct, 2)
        result["ok"]             = True

        # Update global cache
        avwap_cache[ticker] = {
            "avwap":     avwap,
            "price":     price,
            "reclaimed": price > avwap,
            "pct_from_avwap": pct,
            "ts":        datetime.now(CT),
        }

    except Exception as e:
        logger.debug(f"AVWAP calc {ticker}: {e}")

    return result



# ── v2.7.0: ATR Calculation (Foundation for Recs #1, #2, #3) ───
def get_atr(ticker: str, period: int = 14) -> float:
    """Calculate ATR(period) using Finnhub daily candles. 5-min cache."""
    now = time.time()
    cached = _atr_cache.get(ticker)
    if cached and (now - cached["ts"]) < _ATR_CACHE_TTL:
        return cached["atr"]

    try:
        candles = _finnhub_candles(ticker, resolution="D", count=period + 10)
        if not candles or len(candles) < period + 1:
            return None
        # True Range calculation
        true_ranges = []
        for i in range(1, len(candles)):
            tr = max(
                candles[i]["h"] - candles[i]["l"],
                abs(candles[i]["h"] - candles[i-1]["c"]),
                abs(candles[i]["l"] - candles[i-1]["c"])
            )
            true_ranges.append(tr)
        if len(true_ranges) < period:
            return None
        atr = sum(true_ranges[-period:]) / period
        atr = round(atr, 4)
        _atr_cache[ticker] = {"atr": atr, "ts": now}
        return atr
    except Exception as e:
        logger.error(f"ATR calc error {ticker}: {e}")
        return None


# ── v2.7.0: Portfolio Heat (Rec #3) ────────────────────────────
def _calculate_portfolio_heat() -> float:
    """Calculate total portfolio heat = sum of position risk if all stops hit.
    Returns heat as % of total portfolio value."""
    portfolio_val = paper_portfolio_value()
    if portfolio_val <= 0:
        return 0.0
    total_risk = 0.0
    for ticker, pos in paper_positions.items():
        shares = pos.get("shares", 0)
        entry = pos.get("entry_price", pos.get("avg_cost", 0))
        if entry <= 0 or shares <= 0:
            continue
        atr = pos.get("atr_at_entry")
        if atr and atr > 0:
            stop_distance = atr * 3.0  # v2.7.7: match widened hard stop
            risk_per_share = min(stop_distance, entry * 0.06)  # cap at 6%
        else:
            risk_per_share = entry * PAPER_STOP_LOSS_PCT
        total_risk += shares * risk_per_share
    return (total_risk / portfolio_val) * 100


# ── v2.7.0: Re-entry Cooldown (Rec #4) ────────────────────────
def _check_cooldown(ticker: str) -> tuple:
    """Check if ticker is in re-entry cooldown.
    Returns (is_blocked: bool, remaining_hours: float)."""
    cd = _ticker_cooldowns.get(ticker)
    if not cd:
        return (False, 0.0)
    now = datetime.now(CT)
    elapsed_sec = (now - cd["last_sell"]).total_seconds()
    cooldown_h = COOLDOWN_HOURS_LOSS if cd["was_loss"] else COOLDOWN_HOURS_WIN
    cooldown_sec = cooldown_h * 3600
    if elapsed_sec < cooldown_sec:
        remaining = (cooldown_sec - elapsed_sec) / 3600
        return (True, round(remaining, 1))
    return (False, 0.0)


def _record_cooldown(ticker: str, was_loss: bool):
    """Record a sell event for re-entry cooldown tracking."""
    _ticker_cooldowns[ticker] = {
        "last_sell": datetime.now(CT),
        "was_loss": was_loss,
    }


# ── v2.7.0: Multi-Regime Market Classification (Rec #5) ───────
def _classify_market_regime() -> dict:
    """Classify market into 4 regimes using SPY SMAs + VIX.
    Cached for 15 minutes.
    Returns: {"regime": str, "confidence": float, "params": dict}
    Regimes: trending_up, trending_down, range_bound, crisis
    """
    global _market_regime_cache
    now = datetime.now(CT)
    if (_market_regime_cache["ts"] and
            (now - _market_regime_cache["ts"]).total_seconds() < 900):
        return _market_regime_cache

    try:
        # Get SPY daily candles for SMA calculation
        spy_candles = _finnhub_candles("SPY", resolution="D", count=55)
        if not spy_candles or len(spy_candles) < 20:
            return _market_regime_cache

        closes = [c["c"] for c in spy_candles]
        current_spy = closes[-1]

        # Calculate SMAs
        sma_20 = sum(closes[-20:]) / 20
        sma_50 = (sum(closes[-50:]) / 50
                  if len(closes) >= 50
                  else sum(closes) / len(closes))

        # Get VIX
        try:
            vix_q = _finnhub_quote("^VIX") or {}
            vix = vix_q.get("c", 20) or 20
        except Exception:
            vix = 20

        # Spread between SMAs
        sma_spread = ((sma_20 - sma_50) / sma_50 * 100
                      if sma_50 > 0 else 0)
        spy_vs_50 = ((current_spy - sma_50) / sma_50 * 100
                     if sma_50 > 0 else 0)

        # Classification
        if vix > 30 or spy_vs_50 < -3:
            regime = "crisis"
            confidence = min(0.9, max(0.5, (vix - 25) / 15))
        elif (current_spy > sma_20 > sma_50
              and (vix is None or vix < 22)):
            regime = "trending_up"
            confidence = min(0.9, max(0.4, sma_spread / 3))
        elif current_spy < sma_20 and sma_20 < sma_50:
            regime = "trending_down"
            confidence = min(0.9, max(0.4, abs(sma_spread) / 3))
        else:
            regime = "range_bound"
            confidence = 0.6

        # Map to parameters
        REGIME_PARAMS = {
            "trending_up": {
                "threshold_adjust": -5,
                "max_positions_adjust": 2,
                "stop_multiplier": 1.0,
                "size_multiplier": 1.1,
            },
            "trending_down": {
                "threshold_adjust": 10,
                "max_positions_adjust": -3,
                "stop_multiplier": 0.8,
                "size_multiplier": 0.7,
            },
            "crisis": {
                "threshold_adjust": 15,
                "max_positions_adjust": -5,
                "stop_multiplier": 0.6,
                "size_multiplier": 0.5,
            },
            "range_bound": {
                "threshold_adjust": 5,
                "max_positions_adjust": 0,
                "stop_multiplier": 0.9,
                "size_multiplier": 0.85,
            },
        }
        params = REGIME_PARAMS.get(regime, REGIME_PARAMS["range_bound"])
        _market_regime_cache = {
            "regime": regime,
            "confidence": round(confidence, 2),
            "params": params,
            "ts": now,
            "vix": vix,
            "sma_20": round(sma_20, 2),
            "sma_50": round(sma_50, 2),
            "spy": round(current_spy, 2),
        }
        logger.info(
            f"Market regime: {regime} (conf={confidence:.0%},"
            f" VIX={vix:.1f}, SPY={current_spy:.2f},"
            f" SMA20={sma_20:.2f}, SMA50={sma_50:.2f})"
        )
        return _market_regime_cache
    except Exception as e:
        logger.error(f"Regime classification error: {e}")
        return _market_regime_cache


# ── v2.7.0: Signal Decay / Dynamic Weighting (Rec #6) ─────────
def _recalculate_signal_weights():
    """Analyze signal_log.jsonl + trade outcomes to weight components.
    Components that predicted winners get higher weights (up to 1.5x).
    Components that predicted losers get lower weights (down to 0.5x).
    Recalculated every 24 hours or on startup.
    """
    global _signal_weights, _signal_weights_ts
    default = {
        "rsi_pts": 1.0, "bw_pts": 1.0, "macd_pts": 1.0,
        "vol_pts": 1.0, "sq_pts": 1.0, "slope_pts": 1.0,
        "grok_pts": 1.0, "news_pts": 1.0, "avwap_pts": 1.0,
    }

    try:
        if not os.path.exists(SIGNAL_LOG_FILE):
            _signal_weights = default
            _signal_weights_ts = datetime.now(CT)
            return

        # Gather BUY and SELL pairs from signal log
        buys = {}   # ticker -> list of {ts, score, components...}
        sells = {}  # ticker -> list of {ts, pnl_pct}

        cutoff = (datetime.now(CT) - timedelta(days=30)).isoformat()
        with open(SIGNAL_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if entry.get("ts", "") < cutoff:
                    continue
                t = entry.get("ticker", "")
                if entry.get("type") == "BUY":
                    buys.setdefault(t, []).append(entry)
                elif entry.get("type") == "SELL":
                    sells.setdefault(t, []).append(entry)

        # Match buys to sells (chronological order)
        wins = []
        losses = []
        for ticker in buys:
            b_list = sorted(buys[ticker], key=lambda x: x.get("ts", ""))
            s_list = sorted(sells.get(ticker, []),
                            key=lambda x: x.get("ts", ""))
            s_idx = 0
            for b in b_list:
                # Find next sell after this buy
                while (s_idx < len(s_list)
                       and s_list[s_idx].get("ts", "") <= b.get("ts", "")):
                    s_idx += 1
                if s_idx < len(s_list):
                    pnl = s_list[s_idx].get("pnl_pct", 0)
                    if pnl > 0:
                        wins.append(b)
                    else:
                        losses.append(b)
                    s_idx += 1

        if len(wins) < 10 or len(losses) < 5:
            # Not enough data — use defaults
            _signal_weights = default
            _signal_weights_ts = datetime.now(CT)
            logger.info(
                f"Signal weights: insufficient data "
                f"(wins={len(wins)}, losses={len(losses)}), "
                f"using defaults"
            )
            return

        weights = {}
        for comp in default:
            w_avg = (sum(e.get(comp, 0) for e in wins)
                     / len(wins)) if wins else 0
            l_avg = (sum(e.get(comp, 0) for e in losses)
                     / len(losses)) if losses else 0
            if l_avg > 0:
                ratio = w_avg / l_avg
                weights[comp] = max(0.5, min(1.5, ratio))
            elif w_avg > 0:
                weights[comp] = 1.3  # component only in wins
            else:
                weights[comp] = 1.0

        _signal_weights = weights
        _signal_weights_ts = datetime.now(CT)
        # Log significant deviations
        deviations = {k: v for k, v in weights.items()
                      if abs(v - 1.0) > 0.15}
        if deviations:
            logger.info(
                f"Signal weights adjusted: "
                + ", ".join(f"{k}={v:.2f}" for k, v in deviations.items())
                + f" (from {len(wins)}W/{len(losses)}L trades)"
            )
        else:
            logger.info(
                f"Signal weights: all near 1.0 "
                f"({len(wins)}W/{len(losses)}L trades)"
            )
    except Exception as e:
        logger.error(f"Signal weight calc error: {e}")
        _signal_weights = default
        _signal_weights_ts = datetime.now(CT)


# ── v2.7.0: Correlation-Aware Position Limits (Rec #7) ────────
def _get_daily_returns(ticker: str, days: int = 25):
    """Get daily returns for correlation. 1-hour cache."""
    now = time.time()
    cached = _daily_returns_cache.get(ticker)
    if cached and (now - cached["ts"]) < _RETURNS_CACHE_TTL:
        return cached["returns"]

    try:
        candles = _finnhub_candles(ticker, resolution="D", count=days)
        if not candles or len(candles) < 10:
            return None
        closes = [c["c"] for c in candles]
        returns = [(closes[i] - closes[i-1]) / closes[i-1]
                   for i in range(1, len(closes))
                   if closes[i-1] != 0]
        _daily_returns_cache[ticker] = {"returns": returns, "ts": now}
        return returns
    except Exception:
        return None


def _pearson_corr(x: list, y: list) -> float:
    """Pearson correlation coefficient. Returns None if insufficient data."""
    n = min(len(x), len(y))
    if n < 8:
        return None
    x, y = x[-n:], y[-n:]
    mx = sum(x) / n
    my = sum(y) / n
    cov = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    sx = sum((xi - mx) ** 2 for xi in x) ** 0.5
    sy = sum((yi - my) ** 2 for yi in y) ** 0.5
    if sx * sy == 0:
        return None
    return cov / (sx * sy)


def _check_correlation(new_ticker: str, threshold: float = 0.7) -> tuple:
    """Check if new_ticker is too correlated with existing positions.
    Returns (is_ok, blocking_tickers, max_corr)."""
    if not paper_positions:
        return (True, [], 0.0)

    new_ret = _get_daily_returns(new_ticker)
    if not new_ret:
        return (True, [], 0.0)  # can't calc, allow

    highly_correlated = []
    max_corr = 0.0

    for held in paper_positions:
        held_ret = _get_daily_returns(held)
        if not held_ret:
            continue
        corr = _pearson_corr(new_ret, held_ret)
        if corr is not None:
            max_corr = max(max_corr, corr)
            if corr > threshold:
                highly_correlated.append((held, round(corr, 2)))

    # Block if 2+ highly correlated positions already held
    if len(highly_correlated) >= 2:
        return (False, [t for t, c in highly_correlated], round(max_corr, 2))
    return (True, [], round(max_corr, 2))



def compute_paper_signal(ticker: str) -> dict:
    """
    Composite signal engine (13 components, max 168 pts).
    Components: RSI(20) + BB(15) + MACD(15) + Volume(15) + Squeeze(10) +
    Slope(10) + AI Direction(15) + AI Watchlist(10) + Multi-Day Trend(15) +
    News Sentiment(15) + AVWAP(10) + Time-of-Day(±8) + Social Buzz(10).
    S/R modifier: ±5 pts.
    AVWAP can also go -5 if price is below VWAP (overhead supply penalty).
    ToD boosts power hours (open/close), penalizes lunch lull.
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

    # ── 1. RSI Mean-Reversion Entry (20 pts) ─────────────────
    # v2.7.2: Reward trough entries (oversold pullbacks), penalize peaks
    rsi = compute_rsi(prices) if len(prices) >= 15 else None
    if rsi is not None:
        if 30 <= rsi <= 45:
            pts = 20                                   # trough / oversold pullback
        elif 45 < rsi <= 55:
            pts = 15                                   # recovery zone
        elif 55 < rsi <= 65:
            pts = 10                                   # momentum (still OK)
        elif 65 < rsi <= 68:
            pts = 5                                    # getting hot, reduced reward
        else:
            pts = 0                                    # overbought or deeply oversold
        score += pts
        comps["rsi"] = round(rsi, 1)
        comps["rsi_pts"] = pts
        detail.append(f"RSI={rsi:.1f}({pts}pts)")

    # ── 2. Bollinger Band Mean-Reversion (15 pts) ────────────
    # v2.7.2: Reward trough entries (low %B), penalize peak entries
    _, _, _, pct_b, bw = compute_bollinger(prices) if len(prices) >= 20 else (None,)*5
    if pct_b is not None:
        if 0.15 <= pct_b <= 0.40:
            pts = 15                                   # trough / pullback to lower band
        elif 0.40 < pct_b <= 0.60:
            pts = 12                                   # mid-band, decent entry
        elif 0.60 < pct_b <= 0.80:
            pts = 8                                    # upper-mid, less ideal
        elif 0.80 < pct_b <= 0.92:
            pts = 4                                    # extended, caution
        else:
            pts = 0                                    # at/above upper band = peak
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
        # Grab top headline for AI context (cheap — uses existing function)
        _top_news = fetch_latest_news(ticker, 1)
        _headline_ctx = f"Latest news: {_top_news[0][0]}. " if _top_news else ""
        grok_prompt = (
            f"Paper trading signal for {ticker}: "
            f"price ${price_now:.2f}, 5-min change {chg_5m:+.2f}%, "
            f"RSI={rsi:.1f if rsi else 'N/A'}, "
            f"MACD={macd_line:.4f if macd_line else 'N/A'}, "
            f"squeeze={sq['score']:.0f}/100. "
            f"{_headline_ctx}"
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

    # ── 10. News Sentiment (15 pts) ──────────────────────────────
    try:
        news_sent = _score_news_sentiment(ticker)
        n_pts = news_sent["pts"]
        score += n_pts
        comps["news_sentiment"] = news_sent["sentiment"]
        comps["news_pts"] = n_pts
        comps["news_catalyst"] = news_sent.get("catalyst", "")
        detail.append(f"News={news_sent['sentiment']}({n_pts}pts)")
    except Exception as e:
        logger.debug(f"News sentiment error for {ticker}: {e}")

    # ── Feature #4: Support/Resistance modifier (±5 pts) ────────
    try:
        sr = _compute_support_resistance(ticker)
        cur_price, _, _ = fetch_finnhub_quote(ticker)
        if sr["resistance"] and sr["support"] and cur_price:
            comps["support"] = sr["support"]
            comps["resistance"] = sr["resistance"]
            comps["pivot"] = sr.get("pivot")
            if sr["resistance"] > 0 and abs(cur_price - sr["resistance"]) / sr["resistance"] <= 0.01:
                score -= 5
                detail.append(f"NearResist(-5)")
            elif sr["support"] > 0 and abs(cur_price - sr["support"]) / sr["support"] <= 0.01 and cur_price > sr["support"]:
                score += 5
                detail.append(f"NearSupport(+5)")
    except Exception as e:
        logger.debug(f"S/R modifier {ticker}: {e}")

    # ── Feature #11: Anchored VWAP (10 pts + entry gate) ─────────
    # The moment price reclaims AVWAP (breaks above and stays over) = long signal.
    # If buyers are underwater (price < AVWAP) = overhead supply, dangerous to go long.
    try:
        av = compute_avwap(ticker)
        if av["ok"]:
            comps["avwap"]          = av["avwap"]
            comps["avwap_reclaimed"] = av["reclaimed"]
            comps["pct_from_avwap"] = av["pct_from_avwap"]
            if av["reclaimed"] and av["pct_from_avwap"] >= 0.15:
                avwap_pts = 10  # Strong reclaim: price comfortably above AVWAP
            elif av["reclaimed"]:
                avwap_pts = 6   # Just above AVWAP
            elif av["pct_from_avwap"] >= -0.3:
                avwap_pts = 2   # Slightly below — approaching reclaim
            else:
                avwap_pts = -5  # Below AVWAP: overhead supply (buyers dumping at break-even)
            score += avwap_pts
            comps["avwap_pts"] = avwap_pts
            detail.append(f"AVWAP={'ABOVE' if av['reclaimed'] else 'BELOW'}({av['pct_from_avwap']:+.1f}%,{avwap_pts}pts)")
    except Exception as e:
        logger.debug(f"AVWAP signal {ticker}: {e}")

    # ── 12. Intraday time-of-day modifier (±8 pts) ──────────
    # U-shaped volume pattern: boost signals during power
    # hours (open/close), penalize during lunch lull where
    # false breakouts are more common.
    tod_pts, tod_mult, tod_label = _get_intraday_zone()
    if tod_pts != 0:
        score += tod_pts
        detail.append(f"ToD={tod_label}({tod_pts:+d}pts)")
    comps["tod_zone"] = tod_label
    comps["tod_pts"] = tod_pts
    comps["tod_size_mult"] = tod_mult

    # ── 13. Social Buzz / Reddit Mentions (10 pts) ──────────
    # Measures Reddit mention velocity (growth rate) from ApeWisdom.
    # High velocity = stock going viral = momentum indicator.
    try:
        buzz = get_social_buzz(ticker)
        if buzz:
            velocity = buzz["velocity"]
            mentions = buzz["mentions"]
            rank = buzz["rank"]
            comps["social_mentions"] = mentions
            comps["social_velocity"] = velocity
            comps["social_rank"] = rank

            # Score based on velocity AND absolute mentions
            # Need both: high velocity on 2 mentions is noise
            if mentions >= 20 and velocity >= 200:
                buzz_pts = 10  # Viral breakout
            elif mentions >= 15 and velocity >= 100:
                buzz_pts = 7   # Strong buzz
            elif mentions >= 10 and velocity >= 50:
                buzz_pts = 5   # Notable interest
            elif mentions >= 5 and velocity >= 25:
                buzz_pts = 3   # Mild buzz
            else:
                buzz_pts = 0   # Normal/no buzz

            # Declining mentions = fading interest (warning)
            if velocity < -30 and mentions >= 10:
                buzz_pts = -3  # Fading stock

            score += buzz_pts
            comps["social_pts"] = buzz_pts
            if buzz_pts != 0:
                _vel_str = f"{velocity:+.0f}"
                detail.append(f"Social={_vel_str}%vel({buzz_pts}pts,rank#{rank})")
    except Exception as e:
        logger.debug("Social buzz signal %s: %s", ticker, e)

    result = {
        "score":   round(min(score, 168), 1),   # cap: 160 base + 8 ToD
        "detail":  " | ".join(detail),
        "comps":   comps,
        "rsi":     rsi,
        "macd":    macd_line,
        "ts":      now,
    }
    # ── Log signal data for future backtesting ──
    try:
        _fg_val, _ = get_fear_greed()
        _fg_int = int(_fg_val) if _fg_val else None
        try:
            _vix_q = _finnhub_quote("^VIX") or {}
            _vix_val = _vix_q.get("c") or None
        except Exception:
            _vix_val = None
        _thresh = _adaptive_threshold_cache.get("val", 65)
        _daily_today = daily_candles.get(ticker, [{}])[-1] if daily_candles.get(ticker) else {}
        log_signal_data({
            "ts": now.isoformat(),
            "ticker": ticker,
            "price": prices[-1] if prices else 0,
            "rsi": comps.get("rsi"),
            "pct_b": comps.get("pct_b"),
            "macd": comps.get("macd"),
            "macd_pts": comps.get("macd_pts"),
            "vol_ratio": comps.get("vol_ratio"),
            "squeeze": comps.get("squeeze"),
            "slope_pct": comps.get("slope_pct"),
            "sma5": comps.get("sma5"),
            "sma20": comps.get("sma20"),
            "ret_5d": comps.get("ret_5d"),
            "daily_vol_ratio": comps.get("daily_vol_ratio"),
            "avwap": comps.get("avwap"),
            "avwap_reclaimed": comps.get("avwap_reclaimed"),
            "pct_from_avwap": comps.get("pct_from_avwap"),
            "news_sentiment": comps.get("news_sentiment"),
            "news_pts": comps.get("news_pts"),
            "grok_signal": comps.get("grok_signal"),
            "grok_confidence": comps.get("grok_confidence"),
            "grok_pts": comps.get("grok_pts"),
            "grok_reason": comps.get("grok_reason"),
            "news_catalyst": comps.get("news_catalyst"),
            "ai_conviction": comps.get("ai_conviction"),
            "support": comps.get("support"),
            "resistance": comps.get("resistance"),
            "composite_score": result["score"],
            "detail": result["detail"],
            "fg_index": _fg_int,
            "vix": _vix_val,
            "threshold": _thresh,
            "session": get_trading_session(),
            "tod_zone": comps.get("tod_zone"),
            "tod_pts": comps.get("tod_pts"),
            "tod_size_mult": comps.get("tod_size_mult"),
            "social_mentions": comps.get("social_mentions"),
            "social_velocity": comps.get("social_velocity"),
            "social_rank": comps.get("social_rank"),
            "social_pts": comps.get("social_pts"),
            "daily_ohlcv": {
                "open": _daily_today.get("open"),
                "high": _daily_today.get("high"),
                "low": _daily_today.get("low"),
                "close": _daily_today.get("close"),
                "volume": _daily_today.get("volume"),
            } if _daily_today else None,
            "type": "signal",
        })
    except Exception as e:
        logger.debug(f"Signal data log error {ticker}: {e}")
    paper_signals_cache[ticker] = result
    return result


def _paper_position_size(ticker: str, signal_score: float) -> int:
    """
    Calculate shares to buy based on ATR-normalized risk,
    signal strength, market regime, and time-of-day zone.
    v2.7.0: Volatility-normalized sizing (equal-risk per trade).
    Returns 0 if no trade should be made.
    """
    portfolio_val = paper_portfolio_value()

    price, _, _ = _get_best_price(ticker)
    if not price or price <= 0:
        return 0

    # v2.7.0: ATR-based volatility-normalized sizing
    atr = get_atr(ticker)
    if atr and atr > 0:
        # v2.7.5: Risk budget: 5% of portfolio per trade (was 1%)
        risk_per_trade = portfolio_val * 0.05
        stop_distance = atr * 3.0  # v2.7.7: match widened hard stop
        # Position size = risk / stop distance
        ideal_shares = risk_per_trade / stop_distance
        dollars = ideal_shares * price
    else:
        # Fallback: old dollar-based sizing
        max_dollars = portfolio_val * PAPER_MAX_POS_PCT
        strength = min(1.0, (signal_score - PAPER_MIN_SIGNAL)
                       / (100 - PAPER_MIN_SIGNAL))
        dollars = max_dollars * (0.5 + 0.5 * strength)

    # Signal strength scaling (50%-100% of computed size)
    strength = min(1.0, (signal_score - PAPER_MIN_SIGNAL)
                   / (100 - PAPER_MIN_SIGNAL))
    dollars *= (0.5 + 0.5 * strength)

    # Cap at max 20% of portfolio and 95% of cash
    dollars = min(dollars, portfolio_val * PAPER_MAX_POS_PCT)
    dollars = min(dollars, paper_cash * 0.95)

    # AI conviction boost: 15% larger position for high-conviction
    ai_info = ai_watchlist_suggestions.get(ticker)
    if ai_info and ai_info.get("conviction", 0) >= 8:
        dollars *= 1.15
        dollars = min(dollars, portfolio_val * PAPER_MAX_POS_PCT)

    # Time-of-day sizing
    _, tod_mult, _ = _get_intraday_zone()
    dollars *= tod_mult

    # v2.7.0: Market regime sizing multiplier
    regime = _classify_market_regime()
    dollars *= regime["params"].get("size_multiplier", 1.0)

    if dollars < 100:
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

    price, _, _ = _get_best_price(ticker)
    if not price or price < MIN_PRICE_SPECULATIVE:
        return

    # v2.7.3: Classify as speculative if below normal MIN_PRICE
    is_speculative = price < MIN_PRICE

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

        # v2.7.0: ATR-based dynamic stops (Rec #1)
        atr_entry = pos.get("atr_at_entry")
        if atr_entry and atr_entry > 0:
            # ATR-based hard stop — v2.7.7: widened to ATR×3.0 (was 2.5)
            # v2.7.9: tighter ATR×2.0 for fear override positions
            _hard_stop_mult = 2.0 if pos.get("fear_override", False) else 3.0
            atr_hard_stop = cost - (atr_entry * _hard_stop_mult)

            # Dynamic trailing: multiplier tightens with profit
            # v2.7.7: Wider trails for better win ratio (was 3.5/3.0/2.5/2.0)
            profit_pct_raw = pnl_pct * 100
            if profit_pct_raw >= 10:
                atr_mult = 2.5
            elif profit_pct_raw >= 6:
                atr_mult = 3.0
            elif profit_pct_raw >= 3:
                atr_mult = 3.5
            else:
                atr_mult = 4.0

            # Apply regime stop multiplier
            regime = _classify_market_regime()
            atr_mult *= regime["params"].get("stop_multiplier", 1.0)

            high = pos.get("high", cost)
            atr_trail_stop = high - (atr_entry * atr_mult)
            effective_stop = max(atr_trail_stop, atr_hard_stop)

            if price <= effective_stop:
                should_sell = True
                if price <= atr_hard_stop:
                    sell_reason = (
                        f"ATR-HARD-STOP {pnl_pct*100:.1f}%"
                        f" (stop=${atr_hard_stop:.2f},"
                        f" ATR=${atr_entry:.2f})"
                    )
                else:
                    # v2.7.2: Minimum hold period for trailing exits
                    _et_str = f"{pos.get('entry_date', '')} {pos.get('entry_time', '00:00:00')}"
                    try:
                        _et_dt = datetime.strptime(_et_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CT)
                        _held_min = (datetime.now(CT) - _et_dt).total_seconds() / 60
                    except Exception:
                        _held_min = 999
                    if _held_min >= PAPER_MIN_HOLD_MINUTES:
                        peak_pnl = (high - cost) / cost * 100
                        sell_reason = (
                            f"ATR-TRAIL {pnl_pct*100:+.1f}%"
                            f" (peak +{peak_pnl:.1f}%,"
                            f" mult={atr_mult:.1f},"
                            f" stop=${atr_trail_stop:.2f})"
                        )
                    else:
                        logger.debug(f"{ticker}: ATR trail triggered but held only {_held_min:.0f}m < {PAPER_MIN_HOLD_MINUTES}m min")
        else:
            # Fallback: fixed % stops (pre-2.7.0 positions)
            if pnl_pct <= -PAPER_STOP_LOSS_PCT:
                should_sell = True
                sell_reason = f"HARD-STOP {pnl_pct*100:.1f}%"
            else:
                high = pos.get("high", cost)
                peak_pnl_pct = (high - cost) / cost
                trail = _graduated_trail_pct(peak_pnl_pct)
                if price <= high * (1 - trail):
                    # v2.7.2: Minimum hold period for trailing exits
                    _et_str2 = f"{pos.get('entry_date', '')} {pos.get('entry_time', '00:00:00')}"
                    try:
                        _et_dt2 = datetime.strptime(_et_str2, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CT)
                        _held_min2 = (datetime.now(CT) - _et_dt2).total_seconds() / 60
                    except Exception:
                        _held_min2 = 999
                    if _held_min2 >= PAPER_MIN_HOLD_MINUTES:
                        should_sell = True
                        peak_pnl = peak_pnl_pct * 100
                        sell_reason = (
                            f"TRAILING-STOP {pnl_pct*100:+.1f}%"
                            f" (peak +{peak_pnl:.1f}%,"
                            f" trail {trail*100:.0f}%)"
                        )
                    else:
                        logger.debug(f"{ticker}: trail triggered but held only {_held_min2:.0f}m < {PAPER_MIN_HOLD_MINUTES}m min")

        if not should_sell:
            sig = compute_paper_signal(ticker)
            # v2.7.6: Signal-collapse exit — tighter threshold (≤20 vs ≤30)
            # and require at least +1% profit to exit on collapse.
            # If score is low but P&L < 1%, keep holding — stops protect downside.
            # This prevents exiting winners too early on signal noise.
            _sc_score_thresh = 20
            _sc_min_profit = 0.02  # v2.7.7: 2% minimum profit to trigger collapse exit (was 1%)
            if sig["score"] <= _sc_score_thresh and pnl_pct >= _sc_min_profit:
                # Minimum hold before signal-collapse exit
                entry_dt_str = f"{pos.get('entry_date', '')} {pos.get('entry_time', '00:00:00')}"
                try:
                    entry_dt = datetime.strptime(entry_dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CT)
                    hold_minutes = (datetime.now(CT) - entry_dt).total_seconds() / 60
                except Exception:
                    hold_minutes = 999
                if hold_minutes >= PAPER_MIN_HOLD_MINUTES:
                    should_sell = True
                    sell_reason = f"SIGNAL-COLLAPSE score={sig['score']:.0f} pnl={pnl_pct*100:+.1f}% held={hold_minutes:.0f}m"

        # Feature #11: AVWAP stop — if price drops below AVWAP, exit
        # "AVWAP as a stop — if price drops back below it, exit."
        # Only applies during regular session and only for same-day entries
        # (positions entered on a prior day use normal trailing/hard stops)
        if not should_sell and get_trading_session() == "regular":
            if pos.get("entry_date") == today:
                av_cached = avwap_cache.get(ticker)
                if av_cached and av_cached.get("avwap", 0) > 0:
                    avwap_val = av_cached["avwap"]
                    if price < avwap_val * 0.998 and pos.get("high", cost) > avwap_val:
                        # v2.7.2: Minimum hold period for AVWAP exits
                        _et_avwap = f"{pos.get('entry_date', '')} {pos.get('entry_time', '00:00:00')}"
                        try:
                            _et_avwap_dt = datetime.strptime(_et_avwap, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CT)
                            _held_avwap = (datetime.now(CT) - _et_avwap_dt).total_seconds() / 60
                        except Exception:
                            _held_avwap = 999
                        if _held_avwap >= PAPER_MIN_HOLD_MINUTES:
                            should_sell = True
                            sell_reason = (f"AVWAP-STOP: price ${price:.2f} < AVWAP ${avwap_val:.2f} "
                                           f"({pnl_pct*100:+.1f}%)")
                        else:
                            logger.debug(f"{ticker}: AVWAP stop triggered but held only {_held_avwap:.0f}m < {PAPER_MIN_HOLD_MINUTES}m")

        if should_sell:
            shares    = pos["shares"]
            proceeds  = shares * price
            cost_basis = shares * cost
            realized_pnl = proceeds - cost_basis

            # v2.7.3: Compute hold duration early for all SELL messages
            hold_mins = ""
            try:
                _sell_entry_dt = datetime.strptime(
                    f"{pos.get('entry_date', today)} {pos.get('entry_time', '00:00:00')}",
                    "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=CT)
                _sell_held_sec = int((now - _sell_entry_dt).total_seconds())
                if _sell_held_sec < 3600:
                    hold_mins = f"{_sell_held_sec // 60}m"
                elif _sell_held_sec < 86400:
                    hold_mins = f"{_sell_held_sec // 3600}h{(_sell_held_sec % 3600) // 60}m"
                else:
                    hold_mins = f"{_sell_held_sec // 86400}d{(_sell_held_sec % 86400) // 3600}h"
            except (ValueError, TypeError, KeyError):
                pass

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

            # v2.7.0: Record re-entry cooldown (Rec #4)
            _record_cooldown(ticker, was_loss=(realized_pnl < 0))

            # Log SELL action for backtesting
            log_signal_data({
                "ts": now.isoformat(),
                "ticker": ticker,
                "price": price,
                "type": "SELL",
                "shares": shares,
                "proceeds": proceeds,
                "pnl": realized_pnl,
                "pnl_pct": pnl_pct * 100,
                "reason": sell_reason,
                "session": get_trading_session(),
            })

            _sell_held = hold_mins if hold_mins else ""
            msg = (
                f"SELL | {ticker} | {shares} shares @ ${price:.2f} | "
                f"P&L: ${realized_pnl:+.2f} ({pnl_pct*100:+.1f}%) | "
                f"Held: {_sell_held} | "
                f"Reason: {sell_reason} | "
                f"Portfolio: ${paper_portfolio_value():,.0f}"
            )
            paper_log(msg)

            # ── Enriched SELL notification ─────────────────────

            pnl_emoji = "🟢" if realized_pnl >= 0 else "🔴"
            reason_map = {
                "TAKE-PROFIT": "✅ Take-profit hit",
                "HARD-STOP":   "🛑 Hard stop triggered",
                "TRAILING-STOP": "📉 Trailing stop hit",
                "ATR-HARD-STOP": "🛑 ATR hard stop",
                "ATR-TRAIL":   "📉 ATR trailing stop",
                "SIGNAL-COLLAPSE": "📉 Signal deteriorated",
                "AVWAP-STOP": "📉 Price lost AVWAP (overhead supply)",
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

            # ── TP mode: send EXIT to TradersPost ───────────
            if user_config.get("trading_mode") == "shadow":
                try:
                    tp_result = send_traderspost_order(
                        ticker=ticker,
                        action="exit",
                        signal_score=0,
                        price=price,
                    )
                    success = bool(
                        tp_result
                        and tp_result.get("success")
                    )
                    update_shadow_portfolio(
                        ticker, "exit", price,
                        None, success,
                    )
                    if tp_result:
                        _lp = round(price * (1 - LIMIT_ORDER_SELL_BUFFER), 2)
                        _tp_held = hold_mins if hold_mins else ""
                        tp_log(
                            f"LIMIT EXIT {ticker} "
                            f"{shares} shares @ ${_lp:.2f}\n"
                            f"  P&L: ${realized_pnl:+,.0f} "
                            f"({pnl_pct*100:+.1f}%)"
                            f"  Held: {_tp_held}\n"
                            f"  Reason: {sell_reason}"
                        )
                        # Record settlement (T+1)
                        sell_amount = shares * price
                        record_settlement(ticker, sell_amount)
                    else:
                        tp_log(
                            f"LIMIT EXIT {ticker} FAILED\n"
                            f"  Reason: {sell_reason}"
                        )

                except Exception as e:
                    logger.error(f"[TP] EXIT error: {e}")

        return  # one action per scan cycle per ticker

    # ── Check for new buy opportunity ────────────────────────

    # v2.7.7: Regime-aware pause & position cap by F&G
    _fg_val_raw, _ = get_fear_greed()
    _fg_int = int(_fg_val_raw) if _fg_val_raw else 50
    _fear_override_active = False  # v2.7.9: track fear override for this entry
    if _fg_int < 20:
        # v2.7.9: Check for fear override — high-conviction entries allowed
        # even in extreme fear if signal is very strong + social buzz/catalyst
        _override = False
        _override_reason = ""
        _sig_early = compute_paper_signal(ticker)
        if _sig_early["score"] >= 85:
            _buzz = get_social_buzz(ticker)
            _has_buzz = (
                _buzz is not None
                and _buzz.get("velocity", 0) >= 100
                and _buzz.get("mentions", 0) >= 15
            )
            _has_catalyst = _sig_early.get("comps", {}).get("news_pts", 0) >= 10
            if _has_buzz:
                _bv = _buzz["velocity"]
                _br = _buzz["rank"]
                _override = True
                _override_reason = f"viral Reddit buzz (vel={_bv:+.0f}%, rank#{_br})"
            elif _has_catalyst:
                _np = _sig_early["comps"].get("news_pts", 0)
                _override = True
                _override_reason = f"strong news catalyst (news={_np}pts)"

        if _override:
            # Check max 1 fear override position at a time
            _fear_override_count = sum(
                1 for pos in paper_positions.values()
                if pos.get("fear_override", False)
            )
            if _fear_override_count >= 1:
                logger.info(
                    f"FEAR OVERRIDE CAP: already have 1 fear-override "
                    f"position, skipping {ticker}"
                )
                return
            logger.info(
                f"FEAR OVERRIDE: F&G={_fg_int}, allowing {ticker} "
                f"(score={_sig_early['score']}, reason={_override_reason})"
            )
            _fear_override_active = True
            # Continue to entry logic with reduced position size (applied below)
        else:
            logger.info(
                f"REGIME PAUSE: F&G={_fg_int} < 20, "
                f"skipping entry for {ticker}"
            )
            return
    # v2.7.7: Position cap by regime
    if _fg_int < 30:
        _regime_max_pos = 3
    elif _fg_int <= 50:
        _regime_max_pos = 5
    else:
        _regime_max_pos = 10
    _n_open = len(paper_positions)
    if _n_open >= _regime_max_pos:
        logger.info(
            f"POSITION CAP: {_n_open}/{_regime_max_pos} "
            f"positions (F&G={_fg_int}), skipping {ticker}"
        )
        return

    if len(paper_positions) >= PAPER_MAX_POSITIONS:
        return
    if paper_cash < 200:
        return

    # v2.7.0: Re-entry cooldown check (Rec #4)
    is_blocked, cd_remaining = _check_cooldown(ticker)
    if is_blocked:
        logger.debug(f"Skip {ticker}: re-entry cooldown ({cd_remaining:.1f}h remaining)")
        return

    # v2.7.0: Portfolio heat check (Rec #3)
    heat = _calculate_portfolio_heat()
    if heat >= PORTFOLIO_HEAT_LIMIT:
        logger.debug(f"Skip {ticker}: portfolio heat {heat:.1f}% >= {PORTFOLIO_HEAT_LIMIT}% limit")
        return

    # v2.7.10: Cool-off period — block entries in first 15 min
    _ct_now = datetime.now(CT).strftime("%H:%M")
    if "08:30" <= _ct_now < "08:45":
        logger.info(f"COOL-OFF: blocking {ticker} (first 15min after open)")
        return

    sig = compute_paper_signal(ticker)
    threshold = _apply_adaptive_config()
    if sig["score"] < threshold:
        return

    rsi = sig.get("rsi")
    if rsi and rsi > 68:   # v2.7.2: tighter — avoid buying peaks
        return

    # v2.7.2: Block buys at Bollinger Band peaks
    if sig.get("comps", {}).get("pct_b") and sig["comps"]["pct_b"] > 0.92:
        return

    # v2.7.4: Falling-knife guard — block stocks that surged then reversed
    # If a stock ran up 15%+ over 5 days but is now declining (below SMA5
    # AND today is red), it's distributing — don't catch the knife.
    _fk_daily = daily_candles.get(ticker)
    if _fk_daily and len(_fk_daily) >= 6:
        _fk_closes = [d["close"] for d in _fk_daily]
        _fk_opens = [d["open"] for d in _fk_daily]
        _fk_ret_5d = (_fk_closes[-1] - _fk_closes[-6]) / _fk_closes[-6]
        _fk_sma5 = sum(_fk_closes[-5:]) / 5
        _fk_today_red = _fk_closes[-1] < _fk_opens[-1]
        _fk_below_sma5 = _fk_closes[-1] < _fk_sma5
        # How far off the recent 5-day peak
        _fk_peak = max(_fk_closes[-5:])
        _fk_off_peak = (_fk_peak - _fk_closes[-1]) / _fk_peak if _fk_peak > 0 else 0
        # Block: surged 15%+ in 5d, now below SMA5 and today is red
        if _fk_ret_5d >= 0.15 and _fk_below_sma5 and _fk_today_red:
            logger.info(
                f"Skip {ticker}: falling-knife "
                f"(5d +{_fk_ret_5d*100:.1f}%, "
                f"off peak -{_fk_off_peak*100:.1f}%, "
                f"below SMA5, today red)"
            )
            return
        # Block: 10%+ run-up AND already 5%+ off the peak (sharp reversal)
        if _fk_ret_5d >= 0.10 and _fk_off_peak >= 0.05:
            logger.info(
                f"Skip {ticker}: sharp reversal "
                f"(5d +{_fk_ret_5d*100:.1f}%, "
                f"off peak -{_fk_off_peak*100:.1f}%)"
            )
            return

    # Feature #8: Sector concentration guard — max 2 per sector
    ticker_sector = TICKER_SECTORS.get(ticker)
    if ticker_sector:
        same_sector = sum(1 for t in paper_positions
                          if TICKER_SECTORS.get(t) == ticker_sector)
        if same_sector >= 2:
            logger.debug(f"Skip {ticker}: already 2 positions in {ticker_sector}")
            return

    # Feature #5: Sector rotation — check sector ETF performance
    if ticker_sector:
        etf = SECTOR_ETF.get(ticker_sector)
        if etf:
            try:
                etf_price, _, _ = fetch_finnhub_quote(etf)
                etf_prev = _finnhub_quote(etf)
                if etf_price and etf_prev:
                    pc = etf_prev.get("pc") or etf_prev.get("c")
                    if pc and pc > 0:
                        sector_chg = (etf_price - pc) / pc * 100
                        if sector_chg < -1.5:
                            sig["score"] = sig["score"] - 5
                            logger.debug(f"{ticker}: sector {ticker_sector} down {sector_chg:.1f}%, -5pts")
                        elif sector_chg > 1.0:
                            sig["score"] = sig["score"] + 3
                            logger.debug(f"{ticker}: sector {ticker_sector} up {sector_chg:.1f}%, +3pts")
            except Exception as e:
                logger.debug(f"Sector check {ticker}: {e}")
        # Re-check threshold after sector adjustment
        if sig["score"] < threshold:
            return

    # Feature #9: Earnings proximity guard
    if _has_upcoming_earnings(ticker):
        logger.info(f"Skip BUY {ticker}: earnings within 2 days")
        return

    # v2.7.0: Correlation check (Rec #7)
    corr_ok, corr_blockers, max_corr = _check_correlation(ticker)
    if not corr_ok:
        logger.info(f"Skip {ticker}: high correlation ({max_corr:.2f}) with {corr_blockers}")
        return

    # Feature #11: AVWAP entry gate — only enter if price has reclaimed AVWAP
    # "The moment price reclaims AVWAP, long entry with AVWAP as stop."
    # During regular session, require AVWAP reclaim. Skip gate in extended hours
    # or if AVWAP data isn't available (Finnhub rate limit, pre-market, etc.)
    avwap_data = sig["comps"].get("avwap_reclaimed")
    if avwap_data is not None and get_trading_session() == "regular":
        if not avwap_data:
            logger.debug(f"Skip BUY {ticker}: price below AVWAP (overhead supply)")
            return

    # v2.7.3: Speculative buy gates
    if is_speculative:
        # Count current speculative positions
        spec_count = sum(
            1 for t, p in paper_positions.items()
            if p.get("speculative", False)
        )
        if spec_count >= SPEC_MAX_POSITIONS:
            logger.debug(f"Skip {ticker}: max {SPEC_MAX_POSITIONS} speculative positions reached")
            return
        # Require high volume ratio for speculative
        vol_ratio = sig.get("comps", {}).get("vol_ratio", 0)
        has_news = sig.get("comps", {}).get("news_pts", 0) >= 5
        has_ai = sig.get("comps", {}).get("grok_signal") == "BUY"
        if vol_ratio < SPEC_MIN_VOL_RATIO and not has_news and not has_ai:
            logger.debug(f"Skip speculative {ticker}: vol {vol_ratio:.1f}x < {SPEC_MIN_VOL_RATIO}x and no catalyst")
            return

    shares = _paper_position_size(ticker, sig["score"])
    if shares <= 0:
        return

    # v2.7.9: Fear override — half position size
    if _fear_override_active:
        shares = max(1, shares // 2)
        logger.info(f"Fear override half-size: {ticker} {shares} shares")

    # v2.7.3: Cap speculative position size at SPEC_MAX_POS_PCT
    cost         = shares * price
    if is_speculative:
        max_spec_dollars = paper_portfolio_value() * SPEC_MAX_POS_PCT
        if cost > max_spec_dollars:
            shares = max(1, int(max_spec_dollars / price))
            cost = shares * price
    paper_cash  -= cost
    # v2.7.0: Store ATR for dynamic stops (Rec #1)
    _entry_atr = get_atr(ticker)
    paper_positions[ticker] = {
        "shares":     shares,
        "avg_cost":   price,
        "entry_price": price,
        "entry_time": now.strftime("%H:%M:%S"),
        "entry_date": today,
        "high":       price,
        "atr_at_entry": _entry_atr,
        "speculative": is_speculative,
        "fear_override": _fear_override_active,  # v2.7.9
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

    # Log BUY action for backtesting
    log_signal_data({
        "ts": now.isoformat(),
        "ticker": ticker,
        "price": price,
        "type": "BUY",
        "shares": shares,
        "cost": cost,
        "signal_score": sig["score"],
        "signal_detail": sig["detail"],
        "grok_signal": sig["comps"].get("grok_signal"),
        "grok_reason": sig["comps"].get("grok_reason"),
        "news_sentiment": sig["comps"].get("news_sentiment"),
        "news_catalyst": sig["comps"].get("news_catalyst"),
        "fg_index": sig["comps"].get("fg_index"),
        "session": get_trading_session(),
    })

    # Include news catalyst in log if available
    _catalyst = sig["comps"].get("news_catalyst", "")
    _catalyst_str = f" | catalyst={_catalyst}" if _catalyst else ""
    _spec_log = " [SPEC]" if is_speculative else ""
    msg = (
        f"BUY{_spec_log} | {ticker} | {shares} shares @ ${price:.2f} | "
        f"Cost: ${cost:,.2f} | Signal: {sig['score']:.0f}/168 | "
        f"Detail: {sig['detail']}{_catalyst_str} | "
        f"Portfolio: ${paper_portfolio_value():,.0f}"
    )
    paper_log(msg)

    # ── Enriched BUY notification ──────────────────────────────
    c            = sig["comps"]
    new_val      = paper_portfolio_value()
    lifetime_pct = (new_val - PAPER_STARTING_CAPITAL) / PAPER_STARTING_CAPITAL * 100
    # v2.7.0: ATR-based stop levels
    _buy_atr = get_atr(ticker)
    if _buy_atr and _buy_atr > 0:
        # v2.7.9: tighter hard stop for fear override entries
        _hard_mult = 2.0 if _fear_override_active else 3.0
        sl_price = price - (_buy_atr * _hard_mult)
        trail_price = price - (_buy_atr * 4.0)  # v2.7.7: wider initial trail
        _mult_label = f"{_hard_mult:.1f}"
        _stop_label = f"ATR x{_mult_label} (ATR=${_buy_atr:.2f})"
    else:
        sl_price = price * (1 - PAPER_STOP_LOSS_PCT)
        trail_price = price * (1 - PAPER_TRAILING_STOP_PCT)
        _stop_label = f"-{PAPER_STOP_LOSS_PCT*100:.0f}%"

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
    if c.get("news_pts") is not None:
        sig_lines.append(f"News {c.get('news_sentiment', 0):+d} ({c['news_pts']}pts)")
    if c.get("avwap"):
        sig_lines.append(f"AVWAP ${c['avwap']:.2f} ({c.get('pct_from_avwap', 0):+.1f}%) ({c.get('avwap_pts', 0)}pts)")
    if c.get("tod_zone"):
        _tp = c.get('tod_pts', 0)
        _tm = c.get('tod_size_mult', 1.0)
        sig_lines.append(
            f"ToD {c['tod_zone']} ({_tp:+d}pts,"
            f" size {_tm:.0%})"
        )

    # Social buzz line for buy notification
    if c.get("social_pts") is not None and c.get("social_pts", 0) != 0:
        _sv = c.get("social_velocity", 0)
        _sr = c.get("social_rank", "?")
        _sp = c["social_pts"]
        sig_lines.append(f"Reddit {_sv:+.0f}% buzz ({_sp}pts, rank#{_sr})")

    # News catalyst line for buy notification
    news_catalyst_line = ""
    if c.get("news_catalyst"):
        news_catalyst_line = f"  Catalyst: {c['news_catalyst']}\n"

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

    _spec_tag = " [SPEC]" if is_speculative else ""
    send_telegram(
        f"📈 PAPER BUY{_spec_tag} — {ticker}\n"
        f"{'─'*28}\n"
        f"Shares:    {shares} @ ${price:.2f}\n"
        f"Cost:      ${cost:,.0f}\n"
        f"Stop:      ${sl_price:.2f} ({_stop_label})\n"
        f"Trail:     ${trail_price:.2f} (tightens)\n"
        + (f"AVWAP Stop: ${c.get('avwap', 0):.2f} (exit if lost)\n" if c.get('avwap') else "")
        + f"{'─'*28}\n"
        f"Signal:    {sig['score']:.0f}/168 (thresh={threshold})\n"
        + "\n".join(f"  | {l}" for l in sig_lines) + "\n"
        + (f"  | {c.get('grok_reason','')}\n" if c.get("grok_reason") else "")
        + news_catalyst_line
        + multi_day_line
        + ai_thesis_line
        + f"{'─'*28}\n"
        f"Cash left: ${paper_cash:,.0f}\n"
        f"Positions: {len(paper_positions)}/{PAPER_MAX_POSITIONS}\n"
        f"Portfolio: ${new_val:,.0f}  ({lifetime_pct:+.2f}% all-time)\n"
        f"Trades today: {len(paper_trades_today)}"
    )
    save_paper_state()

    # ── TP mode: send BUY to TradersPost ────────────────────
    if user_config.get("trading_mode") == "shadow":
        try:
            tp_result = send_traderspost_order(
                ticker=ticker,
                action="buy",
                signal_score=sig["score"],
                price=price,
                quantity_dollars=cost,
            )
            success = bool(
                tp_result and tp_result.get("success")
            )
            update_shadow_portfolio(
                ticker, "buy", price, cost, success,
            )
            if tp_result:
                _shares = math.floor(cost / price) if price > 0 else 0
                _lp = round(price * (1 + LIMIT_ORDER_BUY_BUFFER), 2)
                _tod = sig.get("comps", {}).get("tod_zone", "")
                _tod_str = f" [{_tod}]" if _tod else ""
                _spec_tp = " [SPEC]" if is_speculative else ""
                tp_log(
                    f"LIMIT BUY{_spec_tp} {ticker} "
                    f"{_shares} shares @ ${_lp:.2f}"
                    f" (${cost:,.0f})\n"
                    f"  Signal: {sig['score']:.0f}/168"
                    f"{_tod_str}"
                )
            else:
                tp_log(f"LIMIT BUY {ticker} FAILED")
        except Exception as e:
            logger.error(f"[TP] BUY error: {e}")


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


def _analyze_signal_effectiveness():
    """Analyze which signal components predicted winning trades."""
    sells = [t for t in paper_all_trades if t["action"] == "SELL"]
    if len(sells) < 5:
        return  # not enough data

    winners = [t for t in sells if t.get("pnl", 0) > 0]
    losers = [t for t in sells if t.get("pnl", 0) <= 0]

    if not winners and not losers:
        return

    win_rate = len(winners) / len(sells) * 100
    avg_win = sum(t["pnl"] for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t["pnl"] for t in losers) / len(losers) if losers else 0

    trade_summary = f"Win rate: {win_rate:.0f}%, Avg win: ${avg_win:.2f}, Avg loss: ${avg_loss:.2f}, Total sells: {len(sells)}"

    recent = paper_all_trades[-20:]
    trade_details = []
    for t in recent:
        detail = t.get("signal_detail", "")
        pnl = t.get("pnl", t.get("pnl_pct", ""))
        trade_details.append(f"{t['action']} {t['ticker']} sig={t.get('signal_score','?')} pnl={pnl} {detail}")

    prompt = (
        f"Analyze these paper trading results and identify patterns.\n"
        f"Overall: {trade_summary}\n"
        f"Recent trades:\n" + "\n".join(trade_details[-15:]) + "\n\n"
        f"In 3-4 bullet points, identify:\n"
        f"1. Which signal components (RSI, MACD, Volume, News, etc) correlate with winners?\n"
        f"2. Any patterns in losing trades (time of day, specific conditions)?\n"
        f"3. One specific actionable suggestion to improve.\n"
        f"Be concise, data-driven."
    )
    analysis = get_ai_response(prompt, max_tokens=300)

    logger.info(f"Signal analysis: {analysis[:200]}")

    # Only send to Telegram on Fridays (weekly learning report)
    if datetime.now(CT).weekday() == 4:  # Friday
        send_telegram(f"WEEKLY SIGNAL ANALYSIS\n\n{trade_summary}\n\n{analysis}")


def paper_morning_report():
    """Send portfolio snapshot at market open."""
    global paper_trades_today, paper_daily_counts, _paper_morning_value, _portfolio_snapshots
    paper_trades_today = []
    _portfolio_snapshots = []  # clear daily snapshots
    paper_daily_counts = {}

    # Trim old signal log entries (keep 30 days)
    trim_signal_log(30)
    # v2.7.0: Recalculate signal weights daily
    _recalculate_signal_weights()

    val      = paper_portfolio_value()
    _paper_morning_value = val  # capture for daily P&L calc
    starting = PAPER_STARTING_CAPITAL
    total_pnl = val - starting
    total_pct = total_pnl / starting * 100

    _session_label = {"regular": "Market Open", "extended": "Pre/Post Market", "closed": "Market Closed"}
    lines = [
        f"PAPER PORTFOLIO — {_session_label[get_trading_session()]}",
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

    # Feature #11: Stale position warnings
    stale = []
    for _ticker, _pos in paper_positions.items():
        entry_date = _pos.get("entry_date")
        if entry_date:
            try:
                days_held = (datetime.now(CT).date() - datetime.strptime(entry_date, "%Y-%m-%d").date()).days
                if days_held >= 3:
                    _sig = compute_paper_signal(_ticker)
                    if _sig["score"] < 50:
                        stale.append(f"  {_ticker} held {days_held}d, sig={_sig['score']:.0f}")
            except (ValueError, TypeError):
                pass
    if stale:
        lines.append("")
        lines.append("STALE POSITIONS:")
        lines.extend(stale)

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

    # Feature #6: Today's portfolio change from morning
    today_change_line = ""
    if _paper_morning_value and _paper_morning_value > 0:
        day_chg = val - _paper_morning_value
        day_chg_pct = day_chg / _paper_morning_value * 100
        today_change_line = f"Today's Change:  ${day_chg:>+12,.2f} ({day_chg_pct:+.2f}%)"

    # Best/worst position of the day (unrealized)
    best_pos = worst_pos = ""
    if paper_positions:
        pos_pnls = []
        for _t, _p in paper_positions.items():
            _pr, _, _ = _get_best_price(_t)
            if _pr:
                _pnl_pct = (_pr - _p["avg_cost"]) / _p["avg_cost"] * 100
                pos_pnls.append((_t, _pnl_pct))
        if pos_pnls:
            pos_pnls.sort(key=lambda x: x[1])
            best_pos = f"Best:  {pos_pnls[-1][0]} {pos_pnls[-1][1]:+.1f}%"
            worst_pos = f"Worst: {pos_pnls[0][0]} {pos_pnls[0][1]:+.1f}%"

    lines = [
        f"PAPER PORTFOLIO — Market Close",
        f"{datetime.now(CT).strftime('%A %B %d, %Y')}",
        f"",
        f"Total Value:     ${val:>12,.2f}",
        f"All-Time P&L:    ${total_pnl:>+12,.2f} ({total_pct:+.2f}%)",
        f"Today Realized:  ${day_realized:>+12,.2f}",
    ]
    if today_change_line:
        lines.append(today_change_line)
    lines.extend([
        f"Cash:            ${paper_cash:>12,.2f}",
    ])
    if best_pos:
        lines.append(f"{best_pos} | {worst_pos}")
    lines.extend([
        f"",
        f"TODAY'S TRADES ({len(paper_trades_today)} total  "
        f"↑{len(buys)} buys  ↓{len(sells)} sells):",
    ])

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
            price, _, _ = _get_best_price(ticker)
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

    # Feature #11: Stale position warnings in EOD
    stale = []
    for _ticker, _pos in paper_positions.items():
        entry_date = _pos.get("entry_date")
        if entry_date:
            try:
                days_held = (datetime.now(CT).date() - datetime.strptime(entry_date, "%Y-%m-%d").date()).days
                if days_held >= 3:
                    _sig = compute_paper_signal(_ticker)
                    if _sig["score"] < 50:
                        stale.append(f"  {_ticker} held {days_held}d, sig={_sig['score']:.0f}")
            except (ValueError, TypeError):
                pass
    if stale:
        lines.append("")
        lines.append("STALE POSITIONS:")
        lines.extend(stale)

    report = "\n".join(lines)
    paper_log(f"=== EOD REPORT ===\n{report}")
    send_telegram(report)
    save_paper_state()   # persist end-of-day snapshot


def send_daily_pnl_summary():
    """Send compact daily P&L summary at 16:05 CT."""
    val = paper_portfolio_value()
    sells = [t for t in paper_trades_today if t["action"] == "SELL"]
    buys = [t for t in paper_trades_today if t["action"] == "BUY"]
    today_realized = sum(t.get("pnl", 0) for t in sells)
    today_winners = [t for t in sells if t.get("pnl", 0) > 0]
    today_win_rate = len(today_winners) / len(sells) * 100 if sells else 0

    # Today's change
    today_chg = ""
    if _paper_morning_value and _paper_morning_value > 0:
        chg = val - _paper_morning_value
        today_chg = f" ({chg:+$,.0f} today)"

    # Best/worst unrealized
    best = worst = ""
    if paper_positions:
        pos_pnls = []
        for _t, _p in paper_positions.items():
            _pr, _, _ = _get_best_price(_t)
            if _pr:
                _pnl_pct = (_pr - _p["avg_cost"]) / _p["avg_cost"] * 100
                pos_pnls.append((_t, _pnl_pct))
        if pos_pnls:
            pos_pnls.sort(key=lambda x: x[1])
            best = f"{pos_pnls[-1][0]} {pos_pnls[-1][1]:+.1f}%"
            worst = f"{pos_pnls[0][0]} {pos_pnls[0][1]:+.1f}%"

    lines = [
        f"TODAY'S P&L SUMMARY",
        f"Portfolio: ${val:,.0f}{today_chg}",
        f"Trades: {len(buys)} buys, {len(sells)} sells",
        f"Realized: ${today_realized:+,.2f}",
    ]
    if best and worst:
        lines.append(f"Best: {best} | Worst: {worst}")
    if sells:
        lines.append(f"Win rate (today): {today_win_rate:.1f}%")

    send_telegram("\n".join(lines))


# ── Paper Trading Telegram Commands ───────────────────────────

async def cmd_paper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /paper                  — current portfolio snapshot
    /paper positions        — open positions with live P&L
    /paper trades           — today's trade log
    /paper history          — all-time trade summary
    /paper signal TICK      — show current signal breakdown for a ticker
    /paper chart            — intraday portfolio value chart
    /paper log              — send investment.log as a file download
    /paper reset            — reset portfolio to $100k (with confirmation)
    """
    _capture_tp_chat(update)
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
                price, _, _ = _get_best_price(ticker)
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
        session = get_trading_session()
        session_label = ""
        if session == "extended":
            # Determine pre/post from yfinance market state
            try:
                sample_ticker = next(iter(paper_positions))
                info = yf.Ticker(sample_ticker).info
                state = (info.get("marketState") or "").upper()
                if state == "PRE":
                    session_label = " (Pre-Market)"
                elif state == "POST":
                    session_label = " (After Hours)"
                else:
                    session_label = " (Extended)"
            except Exception:
                session_label = " (Extended)"
        elif session == "closed":
            session_label = " (Closed)"
        lines = [f"OPEN POSITIONS — {datetime.now(CT).strftime('%H:%M CT')}{session_label}"]
        for ticker, pos in paper_positions.items():
            price, _, _ = _get_best_price(ticker)
            price = price or pos["avg_cost"]
            # Determine if this is an extended-hours price
            price_tag = ""
            if session in ("extended", "closed"):
                ext = _get_extended_price(ticker)
                if ext and ext.get("price"):
                    if ext.get("session") == "Pre-Market":
                        price_tag = " (pre)"
                    elif ext.get("session") == "After Hours":
                        price_tag = " (post)"
            mkt   = pos["shares"] * price
            pnl   = (price - pos["avg_cost"]) / pos["avg_cost"] * 100
            unrealized = (price - pos["avg_cost"]) * pos["shares"]
            arrow = "+" if pnl >= 0 else "-"
            days_held = ""
            try:
                dh = (datetime.now(CT).date() - datetime.strptime(pos.get("entry_date", ""), "%Y-%m-%d").date()).days
                days_held = f"  Held: {dh}d"
            except (ValueError, TypeError):
                pass
            lines += [
                f"",
                f"{arrow} {ticker}",
                f"  Shares: {pos['shares']}  Entry: ${pos['avg_cost']:.2f}  Now: ${price:.2f}{price_tag}",
                f"  Unrealized: ${unrealized:+.2f} ({pnl:+.1f}%)",
                f"  Market value: ${mkt:,.2f}",
                f"  Entry: {pos['entry_date']} {pos['entry_time']}{days_held}",
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
                    f"sig={t.get('signal_score','?'):.0f}/140"
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
        price, _, _ = _get_best_price(arg2)
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
            f"Action threshold: {PAPER_MIN_SIGNAL}/140  "
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

    # ── /paper chart ──────────────────────────────────────────
    elif sub == "chart":
        if len(_portfolio_snapshots) < 2:
            await update.message.reply_text("Not enough data yet — snapshots collected every 5 min during market hours.")
            return
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates

            times = [s[0] for s in _portfolio_snapshots]
            values = [s[1] for s in _portfolio_snapshots]

            fig, ax = plt.subplots(figsize=(10, 5))
            fig.patch.set_facecolor("#1a1a2e")
            ax.set_facecolor("#16213e")

            ax.plot(times, values, color="#00d4ff", linewidth=2)
            ax.fill_between(times, values, alpha=0.15, color="#00d4ff")

            # Starting capital line
            ax.axhline(y=PAPER_STARTING_CAPITAL, color="#555555", linestyle="--", linewidth=1, label="$100k start")

            # Mark BUY/SELL points
            for t in paper_trades_today:
                try:
                    t_time = datetime.strptime(f"{t['date']} {t['time']}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=CT)
                    t_val = t.get("portfolio_value", PAPER_STARTING_CAPITAL)
                    color = "#00ff88" if t["action"] == "BUY" else "#ff4444"
                    ax.plot(t_time, t_val, "o", color=color, markersize=6, zorder=5)
                except (ValueError, KeyError):
                    pass

            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f"${x:,.0f}"))
            ax.tick_params(colors="white")
            ax.set_xlabel("Time (CT)", color="white")
            ax.set_ylabel("Portfolio Value", color="white")
            ax.set_title(f"Paper Portfolio — {datetime.now(CT).strftime('%Y-%m-%d')}", color="white", fontsize=14)
            ax.spines["bottom"].set_color("#444")
            ax.spines["left"].set_color("#444")
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.grid(True, alpha=0.2, color="#444")

            buf = BytesIO()
            fig.savefig(buf, format="png", dpi=220, bbox_inches="tight", facecolor=fig.get_facecolor())
            plt.close(fig)
            buf.seek(0)

            await update.message.reply_document(
                document=buf,
                filename=f"paper_portfolio_{datetime.now(CT).strftime('%Y%m%d')}.png",
                caption=f"Portfolio: ${values[-1]:,.0f} | Snapshots: {len(values)}"
            )
        except Exception as e:
            await update.message.reply_text(f"Chart error: {e}")

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
            "  /paper chart         — intraday portfolio chart\n"
            "  /paper log           — download investment.log\n"
            "  /paper reset         — reset to $100,000\n"
            "  /perf                — performance dashboard\n"
            "  /set                 — adjust thresholds"
        )


async def cmd_perf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One-screen performance dashboard."""
    val = paper_portfolio_value()
    starting = PAPER_STARTING_CAPITAL
    total_pnl = val - starting
    total_pct = total_pnl / starting * 100

    sells = [t for t in paper_all_trades if t["action"] == "SELL"]
    winners = [t for t in sells if t.get("pnl", 0) > 0]
    win_rate = len(winners) / len(sells) * 100 if sells else 0

    today_sells = [t for t in paper_trades_today if t["action"] == "SELL"]
    today_buys = [t for t in paper_trades_today if t["action"] == "BUY"]
    today_pnl = sum(t.get("pnl", 0) for t in today_sells)

    pos_lines = []
    for t, pos in paper_positions.items():
        price, _, _ = _get_best_price(t)
        if price:
            pnl = (price - pos["avg_cost"]) / pos["avg_cost"] * 100
            icon = "+" if pnl >= 0 else "-"
            pos_lines.append(f"{icon}{t} {pnl:+.1f}%")

    threshold = _apply_adaptive_config()

    lines = [
        f"PERFORMANCE DASHBOARD",
        f"{'─'*31}",
        f"Portfolio:  ${val:>10,.0f}",
        f"All-Time:   {total_pct:>+9.2f}%",
        f"Today P&L:  ${today_pnl:>+10,.2f}",
        f"{'─'*31}",
        f"Win Rate:   {win_rate:>9.1f}%",
        f"Trades:     {len(paper_all_trades):>9}",
        f"Open:       {len(paper_positions):>9}/{PAPER_MAX_POSITIONS}",
        f"Cash:       ${paper_cash:>10,.0f}",
        f"Threshold:  {threshold:>9}/140",
        f"{'─'*31}",
    ]

    if pos_lines:
        lines.append("POSITIONS:")
        lines.append(" ".join(pos_lines[:8]))

    await update.message.reply_text("\n".join(lines))


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Adjust key trading parameters via Telegram. Values persist across deploys."""
    _capture_tp_chat(update)
    global PAPER_STOP_LOSS_PCT, PAPER_TAKE_PROFIT_PCT, PAPER_TRAILING_STOP_PCT
    global PAPER_MAX_POSITIONS, PAPER_MIN_SIGNAL

    args = context.args
    if not args:
        aa = "ON (adapts to market)" if user_config.get("auto_adjust", True) else "OFF (using base values)"
        # Build graduated trail display
        z = GRADUATED_TRAIL_ZONES
        trail_str = (
            f"{PAPER_TRAILING_STOP_PCT*100:.0f}%"
            f"/{z[2][1]*100:.0f}%"
            f"/{z[1][1]*100:.0f}%"
            f"/{z[0][1]*100:.0f}%"
        )
        lines = [
            "CONFIGURABLE SETTINGS",
            f"Auto-adjust: {aa}",
            f"",
            f"{'':14s} Base   Active",
            f"stop_loss    {user_config['stop_loss']*100:5.0f}%  {PAPER_STOP_LOSS_PCT*100:5.1f}%",
            f"trailing     {user_config['trailing']*100:5.0f}%  {PAPER_TRAILING_STOP_PCT*100:5.1f}%",
            f"max_positions {user_config['max_positions']:4}   {PAPER_MAX_POSITIONS:5}",
            f"threshold    {user_config['threshold']:5}   {PAPER_MIN_SIGNAL:5}",
            f"",
            f"Graduated Trail: {trail_str}",
            f"  <5%: {PAPER_TRAILING_STOP_PCT*100:.0f}%"
            f"  5-10%: {z[2][1]*100:.0f}%"
            f"  10-15%: {z[1][1]*100:.0f}%"
            f"  15%+: {z[0][1]*100:.0f}%",
            f"  (no fixed take-profit)",
            f"",
            f"Usage: /set <param> <value>",
            f"       /set auto_adjust on|off",
        ]
        await update.message.reply_text("\n".join(lines))
        return

    if len(args) < 2:
        await update.message.reply_text("Usage: /set <param> <value>")
        return

    param = args[0].lower()

    # Handle auto_adjust toggle (non-numeric)
    if param == "auto_adjust":
        val = args[1].lower()
        user_config["auto_adjust"] = val in ("1", "on", "true", "yes")
        save_paper_state()
        status = "ON" if user_config["auto_adjust"] else "OFF"
        await update.message.reply_text(f"Auto-adjust set to {status} (persisted)")
        return

    try:
        value = float(args[1])
    except ValueError:
        await update.message.reply_text("Value must be a number.")
        return

    if param == "stop_loss" and 1 <= value <= 20:
        PAPER_STOP_LOSS_PCT = value / 100
        user_config["stop_loss"] = value / 100
        save_paper_state()
        await update.message.reply_text(f"Stop loss set to {value:.0f}% (persisted)")
    elif param == "take_profit":
        await update.message.reply_text(
            "Fixed take-profit removed in v2.2.\n"
            "Now using graduated trailing stop:\n"
            f"  <5%: {PAPER_TRAILING_STOP_PCT*100:.0f}% trail\n"
            f"  5-10%: {GRADUATED_TRAIL_ZONES[2][1]*100:.0f}% trail\n"
            f"  10-15%: {GRADUATED_TRAIL_ZONES[1][1]*100:.0f}% trail\n"
            f"  15%+: {GRADUATED_TRAIL_ZONES[0][1]*100:.0f}% trail\n"
            "Use /set trailing to change base."
        )
    elif param == "trailing" and 1 <= value <= 15:
        PAPER_TRAILING_STOP_PCT = value / 100
        user_config["trailing"] = value / 100
        save_paper_state()
        await update.message.reply_text(f"Trailing stop set to {value:.0f}% (persisted)")
    elif param == "max_positions" and 1 <= value <= 20:
        PAPER_MAX_POSITIONS = int(value)
        user_config["max_positions"] = int(value)
        save_paper_state()
        await update.message.reply_text(f"Max positions set to {int(value)} (persisted)")
    elif param == "threshold" and 30 <= value <= 100:
        PAPER_MIN_SIGNAL = int(value)
        user_config["threshold"] = int(value)
        save_paper_state()
        await update.message.reply_text(f"Base threshold set to {int(value)} (persisted)")
    else:
        await update.message.reply_text(f"Unknown param or invalid range: {param}={value}")


# ============================================================
# VIX PUT-SELLING ALERT
# ============================================================
# When VIX spikes above threshold, fetch put option premiums
# on favorite stocks and alert the user with a put-selling setup.

VIX_ALERT_TICKERS = ["GOOG", "NVDA", "AMZN", "META"]
VIX_ALERT_THRESHOLD = 33.0
_vix_alert_last_above = False   # was VIX above threshold on last check?
_vix_alert_last_date  = ""      # date string of last alert sent


def _find_best_put(ticker: str, target_otm_pct: float = 0.035,
                   min_days: int = 14, max_days: int = 28) -> dict:
    """
    Find the best OTM put to sell for a given ticker.
    Looks for ~3-4% OTM puts expiring in 2-4 weeks.
    Returns dict with strike, expiry, bid, ask, iv, or {} on failure.
    """
    try:
        tk = yf.Ticker(ticker)
        expirations = tk.options  # tuple of 'YYYY-MM-DD' strings
        if not expirations:
            return {}

        today = datetime.now(CT).date()
        # Find expiry closest to 3 weeks out (within min_days..max_days)
        best_exp = None
        best_diff = 999
        for exp_str in expirations:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            days_out = (exp_date - today).days
            if min_days <= days_out <= max_days:
                diff = abs(days_out - 21)  # prefer ~21 days
                if diff < best_diff:
                    best_diff = diff
                    best_exp = exp_str

        if not best_exp:
            # Fallback: first expiry beyond min_days
            for exp_str in expirations:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                if (exp_date - today).days >= min_days:
                    best_exp = exp_str
                    break

        if not best_exp:
            return {}

        # Get current stock price
        price, _, _ = _get_best_price(ticker)
        if not price or not isinstance(price, (int, float)):
            q = _finnhub_quote(ticker)
            price = q.get("c", 0) or q.get("pc", 0)
        if not price:
            return {}

        target_strike = price * (1 - target_otm_pct)

        # Fetch puts for that expiry
        chain = tk.option_chain(best_exp)
        puts = chain.puts
        if puts.empty:
            return {}

        # Find the put closest to our target strike (OTM)
        otm_puts = puts[puts["strike"] <= price].copy()
        if otm_puts.empty:
            return {}

        otm_puts["dist"] = abs(otm_puts["strike"] - target_strike)
        best = otm_puts.loc[otm_puts["dist"].idxmin()]

        return {
            "ticker":  ticker,
            "price":   round(float(price), 2),
            "strike":  float(best["strike"]),
            "expiry":  best_exp,
            "bid":     float(best.get("bid", 0) or 0),
            "ask":     float(best.get("ask", 0) or 0),
            "iv":      float(best.get("impliedVolatility", 0) or 0),
            "volume":  int(best.get("volume", 0) or 0),
            "oi":      int(best.get("openInterest", 0) or 0),
        }

    except Exception as e:
        logger.warning(f"_find_best_put {ticker}: {e}")
        return {}


def _format_vix_put_alert(vix: float, puts: list) -> str:
    """Format the VIX put-selling alert message (64-char width)."""
    now = datetime.now(CT)
    lines = [
        "VIX SPIKE — PUT SELLING SETUP",
        f"VIX: {vix:.1f}  (threshold: {VIX_ALERT_THRESHOLD})",
        now.strftime("%A %B %d, %Y  %I:%M %p CT"),
        "",
        "PUT OPPORTUNITIES (~3-4% OTM, ~3wk):",
        "" + "─" * 36,
    ]

    for p in puts:
        if not p:
            continue
        exp_short = datetime.strptime(
            p["expiry"], "%Y-%m-%d"
        ).strftime("%-m/%-d")
        otm_pct = (p["price"] - p["strike"]) / p["price"] * 100

        lines.append(
            f"{p['ticker']:<5} ${p['price']:>7.2f} "
            f"-> Sell {p['strike']:.0f}p {exp_short}"
        )
        if p["bid"] > 0:
            lines.append(
                f"  Bid ${p['bid']:.2f}  Ask ${p['ask']:.2f}"
                f"  IV {p['iv']*100:.0f}%"
            )
            cost_basis = p["strike"] - p["bid"]
            lines.append(
                f"  Assigned: ${p['strike']:.0f} - "
                f"${p['bid']:.2f} = ${cost_basis:.2f}"
                f"  ({otm_pct:.1f}% OTM)"
            )
        else:
            lines.append(
                f"  IV {p['iv']*100:.0f}%  "
                f"(mkt closed — check bid Mon)"
            )
            lines.append(f"  ~{otm_pct:.1f}% OTM")
        lines.append("")

    lines.append("─" * 36)
    lines.append("Premium inflated — vol crush expected")
    return "\n".join(lines)


def check_vix_put_alert():
    """Check VIX level and send put-selling alert if threshold crossed.
    Called from check_stocks() each scan cycle."""
    global _vix_alert_last_above, _vix_alert_last_date

    try:
        vix_q = _finnhub_quote("^VIX") or {}
        vix = vix_q.get("c", 0) or vix_q.get("pc", 0)
        if not vix or vix <= 0:
            return
    except Exception:
        return

    today_str = datetime.now(CT).strftime("%Y-%m-%d")
    above = vix >= VIX_ALERT_THRESHOLD

    # Only alert on crossing UP (was below, now above)
    # and only once per calendar day
    if above and not _vix_alert_last_above and _vix_alert_last_date != today_str:
        logger.info(
            f"VIX crossed {VIX_ALERT_THRESHOLD}: "
            f"{vix:.1f} — fetching put options"
        )
        _vix_alert_last_date = today_str

        # Fetch put data for each ticker
        put_data = []
        for ticker in VIX_ALERT_TICKERS:
            p = _find_best_put(ticker)
            if p:
                put_data.append(p)
            time.sleep(0.3)  # gentle with Yahoo

        if put_data:
            msg = _format_vix_put_alert(vix, put_data)
            send_telegram(msg)
            logger.info(
                f"VIX put alert sent: {len(put_data)} tickers"
            )
        else:
            send_telegram(
                f"VIX SPIKE: {vix:.1f} (>{VIX_ALERT_THRESHOLD})\n"
                f"Options data unavailable — check manually"
            )

    _vix_alert_last_above = above


async def cmd_vixalert(update: Update,
                       context: ContextTypes.DEFAULT_TYPE):
    """Show VIX put-selling alert status or trigger manual check."""
    args = context.args

    # /vixalert check — manual trigger
    if args and args[0].lower() == "check":
        try:
            vix_q = _finnhub_quote("^VIX") or {}
            vix = vix_q.get("c", 0) or vix_q.get("pc", 0)
        except Exception:
            vix = 0

        if not vix:
            await update.message.reply_text(
                "Could not fetch VIX data."
            )
            return

        put_data = []
        await update.message.reply_text(
            f"VIX: {vix:.1f} — fetching put options..."
        )
        for ticker in VIX_ALERT_TICKERS:
            p = _find_best_put(ticker)
            if p:
                put_data.append(p)
            time.sleep(0.3)

        if put_data:
            msg = _format_vix_put_alert(vix, put_data)
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text(
                f"VIX: {vix:.1f}\n"
                f"Options data unavailable (market may be closed)."
            )
        return

    # Default: show status
    try:
        vix_q = _finnhub_quote("^VIX") or {}
        vix = vix_q.get("c", 0) or vix_q.get("pc", 0)
    except Exception:
        vix = 0

    status = "ABOVE" if vix >= VIX_ALERT_THRESHOLD else "below"
    tickers_str = ", ".join(VIX_ALERT_TICKERS)

    lines = [
        "VIX PUT-SELLING ALERT",
        "",
        f"VIX:       {vix:.1f} ({status} {VIX_ALERT_THRESHOLD})",
        f"Tickers:   {tickers_str}",
        f"Threshold: {VIX_ALERT_THRESHOLD}",
        f"Last alert: {_vix_alert_last_date or 'never'}",
        "",
        "Auto-alerts when VIX crosses above",
        f"{VIX_ALERT_THRESHOLD} during market hours.",
        "",
        "Commands:",
        " /vixalert        this status",
        " /vixalert check  manual scan now",
    ]
    await update.message.reply_text("\n".join(lines))


# ============================================================
# TRADERSPOST COMMANDS
# ============================================================

async def cmd_shadow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle TP trading mode on/off."""
    _capture_tp_chat(update)
    mode = user_config.get("trading_mode", "paper")
    if mode == "shadow":
        user_config["trading_mode"] = "paper"
        save_paper_state()
        await update.message.reply_text(
            "📡 TP Trading: OFF\n"
            f"{'─'*28}\n"
            "TradersPost sends disabled.\n"
            "Paper trading continues."
        )
    else:
        if not TRADERSPOST_WEBHOOK_URL:
            await update.message.reply_text(
                "📡 TP Trading: ERROR\n"
                f"{'─'*28}\n"
                "TRADERSPOST_WEBHOOK_URL not set.\n"
                "Add it as a Railway env var first."
            )
            return
        user_config["trading_mode"] = "shadow"
        save_paper_state()
        settled, unsettled, _ = get_settled_cash()
        ts = tp_state.get("total_orders_sent", 0)
        tok = tp_state.get("total_orders_success", 0)
        tfl = tp_state.get("total_orders_failed", 0)
        settle_str = (
            f"${unsettled:,.0f} unsettled"
            if unsettled > 0 else "all settled"
        )
        await update.message.reply_text(
            "📡 TP Trading: ON\n"
            f"{'─'*28}\n"
            "TradersPost: Connected ✓\n"
            "Account: Cash (no PDT limits)\n"
            f"Settlement: {settle_str}\n"
            f"Orders Sent: {ts} "
            f"({tok} success, {tfl} failed)"
        )


async def cmd_settlement(update: Update,
                          context: ContextTypes.DEFAULT_TYPE):
    """Show T+1 settlement status for cash account."""
    _capture_tp_chat(update)
    settled, unsettled, pending = get_settled_cash()

    sep = "━" * 31
    lines = [
        "💰 Settlement Tracker (T+1)",
        sep,
        f"Settled Cash:   ${settled:>10,.2f}",
        f"Unsettled:      ${unsettled:>10,.2f}",
    ]

    if pending:
        lines.append("")
        lines.append("Pending Settlements:")
        for p in pending[-8:]:
            try:
                s_date = datetime.strptime(
                    p["settles_on"], "%Y-%m-%d"
                ).strftime("%b %d")
            except (ValueError, KeyError):
                s_date = p.get("settles_on", "?")
            lines.append(
                f"  {p['ticker']:<6} ${p['amount']:>8,.2f}"
                f"  settles {s_date}"
            )
    else:
        lines.append("")
        lines.append("All funds settled.")

    lines.append(sep)
    lines.append("Cash acct: no PDT limits")
    lines.append("Sells settle next business day")

    await update.message.reply_text("\n".join(lines))


async def cmd_tp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show TradersPost status and TP Portfolio."""
    _capture_tp_chat(update)
    mode = user_config.get("trading_mode", "paper")
    mode_label = (
        "Active"
        if mode == "shadow" else "Disabled"
    )
    wh = "Connected ✓" if TRADERSPOST_WEBHOOK_URL else "Not Set ✗"
    ts = tp_state.get("total_orders_sent", 0)
    tok = tp_state.get("total_orders_success", 0)
    tfl = tp_state.get("total_orders_failed", 0)

    # Last order time
    last_str = "None"
    lot = tp_state.get("last_order_time")
    if lot:
        try:
            lt = datetime.fromisoformat(lot)
            ago = datetime.now(CT) - lt.replace(tzinfo=CT)
            mins = int(ago.total_seconds() / 60)
            if mins < 60:
                last_str = f"{mins}m ago"
            elif mins < 1440:
                last_str = f"{mins // 60}h ago"
            else:
                last_str = f"{mins // 1440}d ago"
        except (ValueError, TypeError):
            last_str = str(lot)[:16]

    lines = [
        "📡 TradersPost Status",
        "━" * 31,
        f"Mode: {mode_label}",
        f"Webhook: {wh}",
        f"Orders Sent: {ts}",
        f" ✅ Success: {tok}",
        f" ❌ Failed: {tfl}",
        f"Last Order: {last_str}",
    ]

    # ── TP Portfolio summary ─────────────────────────
    sp = tp_state.get("shadow_portfolio",
                       _default_shadow_portfolio())
    sp_cash = sp.get("cash", 0)
    sp_positions = sp.get("positions", {})
    sp_start = sp.get("starting_cash", PAPER_STARTING_CAPITAL)
    pos_value = 0
    for tick, p in sp_positions.items():
        shares = p.get("shares", 0)
        avg = p.get("avg_price", 0)
        cur_price = avg  # fallback to cost basis
        try:
            result = _get_best_price(tick)
            if isinstance(result, tuple):
                cur_price = result[0] or avg
            elif result:
                cur_price = result
        except Exception:
            pass
        pos_value += shares * cur_price
    est_value = sp_cash + pos_value
    est_pnl = est_value - sp_start
    pnl_pct = (est_pnl / sp_start * 100) if sp_start else 0
    sign = "+" if est_pnl >= 0 else ""

    lines.append("")
    lines.append("TP Portfolio:")
    lines.append(f" Cash: ${sp_cash:,.0f}")
    lines.append(f" Positions: {len(sp_positions)}")
    lines.append(f" Est. Value: ${est_value:,.0f}")
    lines.append(
        f" Est. P&L: {sign}${est_pnl:,.0f} "
        f"({sign}{pnl_pct:.2f}%)"
    )

    # ── Recent orders ────────────────────────────────
    recent = tp_state.get("recent_orders", [])
    if recent:
        lines.append("")
        lines.append("Recent Orders:")
        for o in reversed(recent[-10:]):
            tick = o.get("ticker", "?")
            act = o.get("action", "?")
            ok = "✅" if o.get("success") else "❌"
            t_ago = ""
            try:
                ot = datetime.fromisoformat(o["time"])
                delta = datetime.now(CT) - ot.replace(
                    tzinfo=CT
                )
                m = int(delta.total_seconds() / 60)
                if m < 60:
                    t_ago = f"{m}m ago"
                elif m < 1440:
                    t_ago = f"{m // 60}h ago"
                else:
                    t_ago = f"{m // 1440}d ago"
            except (ValueError, TypeError, KeyError):
                pass
            dl = o.get("dollars")
            dl_str = f" ${dl:,.0f}" if dl else ""
            lines.append(
                f"• {tick} {act}{dl_str} — {ok} {t_ago}"
            )
    else:
        lines.append("")
        lines.append("No orders sent yet.")

    await update.message.reply_text("\n".join(lines))


async def cmd_tppos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all TP Portfolio positions."""
    _capture_tp_chat(update)
    sp = tp_state.get("shadow_portfolio",
                       _default_shadow_portfolio())
    positions = sp.get("positions", {})
    sp_cash = sp.get("cash", 0)
    sp_start = sp.get("starting_cash", PAPER_STARTING_CAPITAL)

    if not positions:
        await update.message.reply_text(
            "📡 TP Portfolio Positions\n"
            f"{'━' * 31}\n"
            "No positions.\n"
            f"Cash: ${sp_cash:,.0f}"
        )
        return

    lines = [
        "📡 TP Portfolio Positions",
        "━" * 31,
    ]

    total_value = 0
    total_cost = 0
    for tick in sorted(positions.keys()):
        p = positions[tick]
        shares = p.get("shares", 0)
        avg = p.get("avg_price", 0)
        cost = shares * avg
        # Try to get current price for P&L
        cur_price = avg  # fallback
        try:
            result = _get_best_price(tick)
            if isinstance(result, tuple):
                cur_price = result[0] or avg
            elif result:
                cur_price = result
        except Exception:
            pass
        mkt_val = shares * cur_price
        pnl = mkt_val - cost
        pnl_pct = (pnl / cost * 100) if cost else 0
        sign = "+" if pnl >= 0 else ""

        lines.append(
            f"{tick}: {shares} @ ${avg:,.2f}"
        )
        lines.append(
            f"  ${mkt_val:,.0f} ({sign}{pnl_pct:.1f}%)"
        )
        total_value += mkt_val
        total_cost += cost

    port_value = sp_cash + total_value
    port_pnl = port_value - sp_start
    port_sign = "+" if port_pnl >= 0 else ""

    lines.append("━" * 31)
    lines.append(f"Positions: {len(positions)}")
    lines.append(f"Cash: ${sp_cash:,.0f}")
    if sp_cash < 0:
        lines.append("⚠️ NEGATIVE CASH")
        lines.append("Fix: /tpsync reset")
        lines.append(" or /tpedit cash AMOUNT")
    lines.append(
        f"Total: ${port_value:,.0f} "
        f"({port_sign}{port_pnl / sp_start * 100:.2f}%)"
    )

    # Split into chunks if too long (Telegram 4096 limit)
    text = "\n".join(lines)
    if len(text) <= 4000:
        await update.message.reply_text(text)
    else:
        # Send in chunks
        chunk = []
        chunk_len = 0
        for line in lines:
            if chunk_len + len(line) + 1 > 3900 and chunk:
                await update.message.reply_text(
                    "\n".join(chunk)
                )
                chunk = []
                chunk_len = 0
            chunk.append(line)
            chunk_len += len(line) + 1
        if chunk:
            await update.message.reply_text(
                "\n".join(chunk)
            )


async def cmd_tpsync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual reset/status for the TP Portfolio."""
    _capture_tp_chat(update)
    args = context.args
    sub = (args[0].lower() if args else "").strip()

    if sub == "reset":
        # Full reset: wipe positions, restore starting cash
        sp = tp_state.setdefault(
            "shadow_portfolio", _default_shadow_portfolio()
        )
        old_pos_count = len(sp.get("positions", {}))
        old_cash = sp.get("cash", 0)
        sp["positions"] = {}
        sp["cash"] = PAPER_STARTING_CAPITAL
        sp["starting_cash"] = PAPER_STARTING_CAPITAL
        sp["total_value_estimate"] = PAPER_STARTING_CAPITAL
        sp["last_sync_check"] = (
            datetime.now(CT).isoformat()
        )
        sp["trade_history"] = []
        save_paper_state()
        tp_log(
            f"TP portfolio reset: "
            f"{old_pos_count} positions cleared, "
            f"cash ${old_cash:,.0f} → "
            f"${PAPER_STARTING_CAPITAL:,.0f}"
        )
        await update.message.reply_text(
            "📡 TP Portfolio Reset\n"
            f"{'━' * 28}\n"
            f"Positions cleared: {old_pos_count}\n"
            f"Cash: ${PAPER_STARTING_CAPITAL:,.0f}\n"
            "Ready for fresh trades."
        )

    elif sub == "status":
        sp = tp_state.get(
            "shadow_portfolio", _default_shadow_portfolio()
        )
        tp_pos = sp.get("positions", {})
        sp_cash = sp.get("cash", 0)
        sp_start = sp.get(
            "starting_cash", PAPER_STARTING_CAPITAL
        )
        lines = [
            "📡 TP Portfolio Status",
            "━" * 28,
        ]
        total_val = 0
        for t in sorted(tp_pos.keys()):
            p = tp_pos[t]
            shares = p.get("shares", 0)
            avg = p.get("avg_price", 0)
            val = shares * avg
            total_val += val
            lines.append(
                f"{t}: {shares} @ ${avg:,.2f}"
                f" (${val:,.0f})"
            )
        if not tp_pos:
            lines.append("No open positions.")
        port_val = sp_cash + total_val
        pnl = port_val - sp_start
        sign = "+" if pnl >= 0 else ""
        lines.append("━" * 28)
        lines.append(f"Positions: {len(tp_pos)}")
        lines.append(f"Cash: ${sp_cash:,.0f}")
        if sp_cash < 0:
            lines.append("⚠️ NEGATIVE — /tpsync reset")
        lines.append(
            f"Total: ${port_val:,.0f} "
            f"({sign}{pnl / sp_start * 100:.2f}%)"
        )
        await update.message.reply_text(
            "\n".join(lines)
        )

    else:
        await update.message.reply_text(
            "📡 /tpsync commands:\n"
            f"{'─' * 28}\n"
            "/tpsync reset — Wipe all TP\n"
            "  positions, restore starting cash\n"
            "/tpsync status — Current TP\n"
            "  portfolio snapshot"
        )


async def cmd_tpedit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual shadow portfolio editor."""
    _capture_tp_chat(update)
    args = context.args
    sub = (args[0].lower() if args else "").strip()
    sp = tp_state.setdefault(
        "shadow_portfolio", _default_shadow_portfolio()
    )

    # ── add TICKER SHARES PRICE ─────────────────────────
    if sub == "add":
        if len(args) < 4:
            await update.message.reply_text(
                "Usage: /tpedit add TICK QTY PRICE"
            )
            return
        ticker = args[1].upper()
        try:
            shares = float(args[2])
        except ValueError:
            await update.message.reply_text(
                f"Invalid number: {args[2]}"
            )
            return
        try:
            price = float(args[3])
        except ValueError:
            await update.message.reply_text(
                f"Invalid number: {args[3]}"
            )
            return
        dollar_amt = round(shares * price, 2)
        # If replacing, refund old position first
        if ticker in sp["positions"]:
            old = sp["positions"][ticker]
            old_val = round(
                old["shares"] * old["avg_price"], 2
            )
            sp["cash"] = round(sp["cash"] + old_val, 2)
        sp["positions"][ticker] = {
            "shares": shares,
            "avg_price": round(price, 2),
            "entry_date": datetime.now(CT).strftime(
                "%Y-%m-%d"
            ),
            "entry_time": datetime.now(CT).strftime(
                "%H:%M"
            ),
            "dollar_amount": dollar_amt,
        }
        sp["cash"] = round(sp["cash"] - dollar_amt, 2)
        save_paper_state()
        await update.message.reply_text(
            f"📡 Added: {ticker} {shares:g} shares"
            f" @ ${price:,.2f} (${dollar_amt:,.0f})"
        )

    # ── remove TICKER ───────────────────────────────────
    elif sub == "remove":
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /tpedit remove TICK"
            )
            return
        ticker = args[1].upper()
        if ticker not in sp["positions"]:
            await update.message.reply_text(
                f"Position {ticker} not found"
                " in TP portfolio"
            )
            return
        pos = sp["positions"].pop(ticker)
        refund = round(
            pos["shares"] * pos["avg_price"], 2
        )
        sp["cash"] = round(sp["cash"] + refund, 2)
        save_paper_state()
        await update.message.reply_text(
            f"📡 Removed: {ticker}"
            f" ({pos['shares']:g} shares,"
            f" +${refund:,.0f} to cash)"
        )

    # ── cash AMOUNT ─────────────────────────────────────
    elif sub == "cash":
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /tpedit cash AMOUNT"
            )
            return
        try:
            amount = float(args[1])
        except ValueError:
            await update.message.reply_text(
                f"Invalid number: {args[1]}"
            )
            return
        sp["cash"] = round(amount, 2)
        save_paper_state()
        await update.message.reply_text(
            f"📡 Shadow cash set to ${amount:,.2f}"
        )

    # ── shares TICKER SHARES ────────────────────────────
    elif sub == "shares":
        if len(args) < 3:
            await update.message.reply_text(
                "Usage: /tpedit shares TICK QTY"
            )
            return
        ticker = args[1].upper()
        if ticker not in sp["positions"]:
            await update.message.reply_text(
                f"Position {ticker} not found"
                " in TP portfolio"
            )
            return
        try:
            new_shares = float(args[2])
        except ValueError:
            await update.message.reply_text(
                f"Invalid number: {args[2]}"
            )
            return
        pos = sp["positions"][ticker]
        old_shares = pos["shares"]
        diff = new_shares - old_shares
        cash_adj = round(diff * pos["avg_price"], 2)
        sp["cash"] = round(sp["cash"] - cash_adj, 2)
        pos["shares"] = new_shares
        pos["dollar_amount"] = round(
            new_shares * pos["avg_price"], 2
        )
        save_paper_state()
        await update.message.reply_text(
            f"📡 {ticker} shares updated:"
            f" {old_shares:g} → {new_shares:g}"
        )

    # ── clear ───────────────────────────────────────────
    elif sub == "clear":
        sp["positions"] = {}
        sp["cash"] = PAPER_STARTING_CAPITAL
        sp["starting_cash"] = PAPER_STARTING_CAPITAL
        sp["closed_trades"] = []
        sp["total_value_estimate"] = PAPER_STARTING_CAPITAL
        save_paper_state()
        await update.message.reply_text(
            "📡 TP portfolio cleared."
            f" Cash: ${PAPER_STARTING_CAPITAL:,.0f}"
        )

    # ── help (no args / unknown subcommand) ─────────────
    else:
        await update.message.reply_text(
            "📡 TP Portfolio Editor\n"
            "━" * 27 + "\n"
            "/tpedit add TICK QTY PRICE\n"
            "  Add/replace a position\n"
            "/tpedit remove TICK\n"
            "  Remove a position\n"
            "/tpedit shares TICK QTY\n"
            "  Adjust share count\n"
            "/tpedit cash AMOUNT\n"
            "  Set cash balance\n"
            "/tpedit clear\n"
            "  Reset to empty portfolio\n"
            "━" * 27
        )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def cmd_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Full end-to-end trading strategy summary with live params."""
    # Gather live values
    regime = _classify_market_regime()
    regime_name = regime.get("regime", "unknown")
    regime_conf = regime.get("confidence", 0)
    _rv = regime.get("vix")
    regime_vix = f"{_rv:.1f}" if _rv else "N/A (market closed)"
    _rs = regime.get("spy")
    regime_spy = f"${_rs:.2f}" if _rs else "N/A"
    _r20 = regime.get("sma_20")
    regime_sma20 = f"${_r20:.2f}" if _r20 else "N/A"
    _r50 = regime.get("sma_50")
    regime_sma50 = f"${_r50:.2f}" if _r50 else "N/A"
    r_params = regime.get("params", {})
    heat = _calculate_portfolio_heat()
    fg_val, fg_label = get_fear_greed()
    fg_str = f"{fg_val} ({fg_label})" if fg_val else "N/A"
    thresh = _adaptive_threshold_cache.get("val", 65)
    n_pos = len(paper_positions)
    n_cool = sum(1 for t in _ticker_cooldowns
                 if _check_cooldown(t)[0])
    _, tod_mult, tod_label = _get_intraday_zone()
    # Separator line (pre-computed to avoid backslash-in-fstring)
    SEP = "\u2500" * 32
    # Signal weights summary
    wt_deviations = []
    if _signal_weights:
        for k, v in _signal_weights.items():
            if abs(v - 1.0) > 0.1:
                name = k.replace("_pts", "")
                wt_deviations.append(f"{name}={v:.2f}")
    wt_str = (", ".join(wt_deviations)
              if wt_deviations else "all 1.0x (default)")

    msg1 = (
        f"\U0001f9e0 TRADING STRATEGY v{BOT_VERSION}\n"
        f"{SEP}\n"
        f"\n"
        f"\U0001f3af SIGNAL ENGINE (13 components)\n"
        f"Max score: 168 pts\n"
        f" 1. RSI Mean-Revert   0-20 pts\n"
        f" 2. BB Mean-Revert    0-15 pts\n"
        f" 3. MACD Crossover    0-15 pts\n"
        f" 4. Volume Confirm    0-15 pts\n"
        f" 5. Squeeze Score     0-10 pts\n"
        f" 6. Price Slope       0-10 pts\n"
        f" 7. AI Direction      0-15 pts\n"
        f" 8. AI Watchlist      0-10 pts\n"
        f" 9. Multi-Day Trend   0-15 pts\n"
        f"10. News Sentiment    0-15 pts\n"
        f"11. AVWAP             -5 to +10\n"
        f"12. Time-of-Day       -8 to +8\n"
        f"13. Social Buzz       0-10 pts\n"
        f"    Reddit mention velocity (ApeWisdom)\n"
        f"    S/R modifier      \u00b15 pts\n"
        f"Signal weights: {wt_str}\n"
    )

    msg2 = (
        f"\n\U0001f6a8 ENTRY GATES (all must pass)\n"
        f"{SEP}\n"
        f" 1. Score \u2265 adaptive threshold\n"
        f"    Current: {thresh}\n"
        f" 2. RSI < 68 (no overbought)\n"
        f"    + %%B < 0.92 (no BB peak)\n"
        f" 3. Max 2 per sector\n"
        f" 4. Sector ETF momentum\n"
        f" 5. No earnings within 2 days\n"
        f" 6. AVWAP reclaimed (reg hrs)\n"
        f" 7. Cooldown clear\n"
        f"    Win: {COOLDOWN_HOURS_WIN}h, Loss: {COOLDOWN_HOURS_LOSS}h\n"
        f"    Active: {n_cool} tickers blocked\n"
        f" 8. Portfolio heat < {PORTFOLIO_HEAT_LIMIT:.0f}%\n"
        f"    Current: {heat:.1f}%\n"
        f" 9. Correlation < 0.7 with 2+\n"
        f"    held positions\n"
        f"10. Falling-knife guard\n"
        f"    No 15%+ surge + decline\n"
        f"    No 10%+ surge + 5% off peak\n"
    )

    msg3 = (
        f"\n\U0001f4b0 POSITION SIZING (ATR-based)\n"
        f"{SEP}\n"
        f"Risk budget: 5% of portfolio\n"
        f"Size = risk / (ATR\u00d73.0 stop)\n"
        f"Then scaled by:\n"
        f" \u2022 Signal strength  50-100%\n"
        f" \u2022 ToD zone: {tod_label} ({tod_mult:.0%})\n"
        f" \u2022 AI conviction  +15% if \u22658\n"
        f" \u2022 Regime: {regime_name} (\u00d7{r_params.get('size_multiplier', 1.0):.2f})\n"
        f"Max per position: {PAPER_MAX_POS_PCT*100:.0f}%\n"
        f"Max positions: {PAPER_MAX_POSITIONS}\n"
        f"Fallback: dollar-based if no ATR\n"
    )

    msg4 = (
        f"\n\U0001f6d1 EXIT STRATEGY\n"
        f"{SEP}\n"
        f"ATR-based dynamic stops:\n"
        f" Hard: entry \u2212 (ATR\u00d73.0)\n"
        f" Trail: high \u2212 (ATR\u00d7mult)\n"
        f"   0-3%:   4.0\u00d7 ATR\n"
        f"   3-6%:   3.5\u00d7 ATR\n"
        f"   6-10%:  3.0\u00d7 ATR\n"
        f"   10%+:   2.5\u00d7 ATR\n"
        f" Regime stop mult: \u00d7{r_params.get('stop_multiplier', 1.0):.2f}\n"
        f"\n"
        f"Other exits:\n"
        f" \u2022 Signal collapse (\u226420, +2% min, 30m)\n"
        f" \u2022 AVWAP lost (same-day only)\n"
        f" \u2022 Fallback: {PAPER_STOP_LOSS_PCT*100:.0f}% hard /"
        f" {PAPER_TRAILING_STOP_PCT*100:.0f}% trail\n"
    )

    msg5 = (
        f"\n\U0001f30d MARKET REGIME\n"
        f"{SEP}\n"
        f"Current: {regime_name.upper()}"
        + (" (market closed)" if regime_name == "unknown" else "")
        + "\n"
        f"Confidence: {regime_conf:.0%}\n"
        f"SPY: {regime_spy}\n"
        f"SMA20: {regime_sma20}  SMA50: {regime_sma50}\n"
        f"VIX: {regime_vix}\n"
        f"\n"
        f"Regime effects:\n"
        f" Threshold: {r_params.get('threshold_adjust', 0):+d}\n"
        f" Max pos:   {r_params.get('max_positions_adjust', 0):+d}\n"
        f" Stop mult: \u00d7{r_params.get('stop_multiplier', 1.0):.2f}\n"
        f" Size mult: \u00d7{r_params.get('size_multiplier', 1.0):.2f}\n"
        f"\n"
        f"Regimes: trending_up (-5 thresh,\n"
        f" +2 pos, \u00d71.1 size) | trending_down\n"
        f" (+10, -3 pos, \u00d70.7) | range_bound\n"
        f" (+5, \u00d70.85) | crisis (+15, -5 pos,\n"
        f" \u00d70.5 size, \u00d70.6 stops)\n"
    )

    # v2.7.7: Compute regime position cap for display
    _strat_fg = int(fg_val) if fg_val else 50
    if _strat_fg < 20:
        _strat_cap = 0
        _strat_cap_label = "PAUSED"
    elif _strat_fg < 30:
        _strat_cap = 3
        _strat_cap_label = str(_strat_cap)
    elif _strat_fg <= 50:
        _strat_cap = 5
        _strat_cap_label = str(_strat_cap)
    else:
        _strat_cap = 10
        _strat_cap_label = str(_strat_cap)

    msg6 = (
        f"\n\U0001f6e1 RISK MANAGEMENT\n"
        f"{SEP}\n"
        f"Portfolio heat: {heat:.1f}% / {PORTFOLIO_HEAT_LIMIT:.0f}% max\n"
        f"  (total risk if all stops hit)\n"
        f"Positions: {n_pos}/{_strat_cap_label}\n"
        f"\n"
        f"v2.7.7 Regime position caps:\n"
        f" F&G < 20:  PAUSED (fear override*)\n"
        f" F&G 20-30: max 3 positions\n"
        f" F&G 30-50: max 5 positions\n"
        f" F&G > 50:  max 10 positions\n"
        f" Current: F&G={_strat_fg} cap={_strat_cap_label}\n"
        f"\n"
        f"Sector guard: max 2 per sector\n"
        f"Correlation: block if 2+ held\n"
        f"  positions corr > 0.7\n"
        f"Cooldowns: {n_cool} active\n"
        f"  Win sell: {COOLDOWN_HOURS_WIN}h block\n"
        f"  Loss sell: {COOLDOWN_HOURS_LOSS}h block\n"
        f"Max actions: {PAPER_MAX_ACTIONS}/ticker/day\n"
        f"\n"
        f"\U0001f4c8 ADAPTIVE CONFIG\n"
        f"{SEP}\n"
        f"F&G: {fg_str}\n"
        f"VIX: {regime_vix}\n"
        f"Threshold: {thresh} (floor 70, cap 90)\n"
        f"Adjusts: threshold, SL, trail,\n"
        f"  max positions every 5 min\n"
    )

    msg7 = (
        f"\n\U0001f4e1 EXECUTION\n"
        f"{SEP}\n"
        f"Order type: LIMIT only\n"
        f"Buy buffer:  +{LIMIT_ORDER_BUY_BUFFER*100:.1f}%\n"
        f"Sell buffer: -{LIMIT_ORDER_SELL_BUFFER*100:.1f}%\n"
        f"Min price: ${MIN_PRICE:.0f} (${MIN_PRICE_SPECULATIVE:.0f} spec)\n"
        f"Speculative: ${MIN_PRICE_SPECULATIVE}-${MIN_PRICE}\n"
        f"  Max {SPEC_MAX_POSITIONS} positions, {SPEC_MAX_POS_PCT*100:.0f}% cap\n"
        f"  Req: {SPEC_MIN_VOL_RATIO:.0f}x vol OR catalyst\n"
        f"\n"
        f"\u23f0 SCHEDULE\n"
        f"{SEP}\n"
        f"Scanner: every ~60s during market\n"
        f"7am AI prep | 8am dashboard\n"
        f"8:30 open | 10:30/12:30/2:30 AI\n"
        f"3pm close | 6pm recap\n"
        f"Sat 9am weekly | Sun 6pm prep\n"
    )

    msg8 = (
        f"\n\U0001f525 v2.7.9 ADDITIONS\n"
        f"{SEP}\n"
        f"Fear Override:\n"
        f" Entries in F&G<20 IF signal>=85\n"
        f" + viral Reddit buzz (vel>=100%,\n"
        f"   mentions>=15) OR news>=10pts\n"
        f" Half position, ATR x2.0 stop\n"
        f" Max 1 fear-override at a time\n"
        f"\n"
        f"Compact Alerts:\n"
        f" Mover alerts batched into one\n"
        f" message when multiple trigger\n"
        f" Spike alerts still individual\n"
    )

    msg9 = (
        f"\n\U0001f195 v2.7.10 ADDITIONS\n"
        f"{SEP}\n"
        f"Morning cool-off: No entries first\n"
        f"15 min after open (9:30-9:45 ET).\n"
        f"Avoids opening pop/deflation traps.\n"
        f"\n"
        f"/buzz command: Reddit social buzz\n"
        f"leaderboard from ApeWisdom.\n"
    )

    full_msg = msg1 + msg2 + msg3 + msg4 + msg5 + msg6 + msg7 + msg8 + msg9
    # send_telegram handles splitting if > 4096 chars
    cid = update.effective_chat.id
    send_telegram(full_msg, chat_id=str(cid))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(BOT_DESCRIPTION)

async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show full release history."""
    lines = [f"📋 Stock Spike Monitor v{BOT_VERSION}", ""]
    lines.append("Recent Changes:")
    for note in RELEASE_NOTES:
        # Extract "X.Y — Feature Name" (title only)
        parts = note.split(": ", 1)
        title = parts[0]  # "X.Y — Feature Name"
        lines.append(title)
    await update.message.reply_text("\n".join(lines))

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

        # Feature #4: Include support/resistance levels
        sr = _compute_support_resistance(ticker)
        sr_str = ""
        if sr.get("support") and sr.get("resistance"):
            sr_str = f" Support ${sr['support']:.2f}, Resistance ${sr['resistance']:.2f}, Pivot ${sr.get('pivot', 0):.2f}."

        prompt = (
            f"{'Last-close analysis' if note else 'Analysis'} of {ticker}: "
            f"Price ${price:.2f} ({chg:+.2f}%), "
            f"Mkt Cap ${mcap:.1f}B, Volume {vol:,}, "
            f"{range_str}.{sr_str} "
            f"Recent news: {news_str}. "
            f"{'Market is closed — focus on setup for next session. ' if note else ''}"
            f"Provide: (1) technical assessment (2) near-term catalyst (3) key risk. Be specific."
        )
        ai = get_ai_response(prompt, max_tokens=500)

        range_display = (f"${low52:.2f} - ${high52:.2f}" if high52 and low52 else "n/a")
        pct_display   = (f"{pct_from_high:+.1f}%" if high52 else "n/a")
        sr_display = ""
        if sr.get("support") and sr.get("resistance"):
            sr_display = f"\nSupport:      ${sr['support']:.2f}\nResistance:   ${sr['resistance']:.2f}"

        await update.message.reply_text(
            f"{ticker} Analysis{f'  {note}' if note else ''}\n"
            f"Price:        ${price:.2f} ({chg:+.2f}%)\n"
            f"Mkt Cap:      ${mcap:.1f}B\n"
            f"Volume:       {vol:,}\n"
            f"52w Range:    {range_display}\n"
            f"From 52w High: {pct_display}"
            f"{sr_display}\n\n"
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

        # Extended hours: show real-time AH/PM price as main price
        display_price = d.get("ext_price") or price
        display_chg_pct = d.get("ext_change_pct") or chg
        session_tag = f"  ({d['ext_session']})" if d.get("ext_session") else ""
        chg_abs = display_price * display_chg_pct / 100 if display_chg_pct else price * chg / 100
        arrow   = "+" if display_chg_pct >= 0 else "-"

        day_range = f"${day_lo:.2f} - ${day_hi:.2f}" if day_hi and day_lo else "n/a"
        yr_range  = f"${low52:.2f} - ${high52:.2f}"   if high52 and low52  else "n/a"

        ext_line = ""
        if d.get("ext_price"):
            ext_p = d["ext_price"]
            ext_chg = d.get("ext_change_pct", 0)
            ext_session = d.get("ext_session", "Extended")
            ext_reg = d.get("ext_regular_close", price)
            moon = "\U0001f319" if ext_session == "After Hours" else "\U0001f305"
            ext_line = (
                f"\n\n{moon} {ext_session}:\n"
                f"  Price: ${ext_p:.2f} ({ext_chg:+.2f}%)\n"
                f"  vs Close: ${ext_reg:.2f} \u2192 ${ext_p:.2f}"
            )

        await update.message.reply_text(
            f"{arrow} {ticker}: ${display_price:.2f}{session_tag}{f'  {note}' if note else ''}\n"
            f"Change:    {chg_abs:+.2f} ({display_chg_pct:+.2f}%)\n"
            f"Day range: {day_range}\n"
            f"52w range: {yr_range}\n"
            f"Volume:    {vol:,}"
            f"{ext_line}"
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



async def cmd_buzz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Reddit social buzz leaderboard from ApeWisdom."""
    await update.message.reply_text("Fetching Reddit buzz...")

    buzz_data = get_social_buzz()
    if not buzz_data:
        await update.message.reply_text("No social buzz data available.")
        return

    # Sort by mentions descending, take top 15
    sorted_buzz = sorted(
        buzz_data.items(),
        key=lambda x: x[1]["mentions"],
        reverse=True
    )[:15]

    SEP = "\u2500" * 32
    lines = [
        "REDDIT BUZZ (ApeWisdom)",
        SEP,
        f"{'#':>2} {'Ticker':<6} {'Ment':>5} {'Vel':>7} {'Rank':>4}",
        SEP,
    ]

    for i, (ticker, d) in enumerate(sorted_buzz, 1):
        vel = d["velocity"]
        if vel >= 100:
            flag = " \U0001f525"  # fire emoji for viral
        elif vel >= 50:
            flag = " \u2B06"  # up arrow
        elif vel < -30:
            flag = " \u2B07"  # down arrow
        else:
            flag = ""
        mention_ct = d["mentions"]
        rank_val = d["rank"]
        lines.append(
            f"{i:>2} {ticker:<6} {mention_ct:>5}"
            f" {vel:>+6.0f}% #{rank_val:<3}{flag}"
        )

    lines.append(SEP)
    lines.append("Vel = 24h mention change %")
    lines.append("\U0001f525 = viral (100%+)")

    # Show which of our watchlist tickers have buzz
    our_tickers = set(TICKERS)
    buzzing_ours = []
    for ticker, d in buzz_data.items():
        if ticker in our_tickers and d["velocity"] >= 25 and d["mentions"] >= 5:
            buzzing_ours.append((ticker, d))

    if buzzing_ours:
        buzzing_ours.sort(key=lambda x: x[1]["velocity"], reverse=True)
        lines.append("")
        lines.append("IN OUR WATCHLIST:")
        for ticker, d in buzzing_ours[:5]:
            bm = d["mentions"]
            bv = d["velocity"]
            lines.append(
                f"  {ticker}: {bm} mentions"
                f" ({bv:+.0f}%)"
            )

    msg = "\n".join(lines)
    await update.message.reply_text(msg, parse_mode=None)

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
    ticker = context.args[0].upper()
    articles = fetch_news_with_details(ticker, count=5)
    if not articles:
        await update.message.reply_text(f"No recent news for {ticker}.")
        return
    now_ts = time.time()
    lines = [f"Latest news for {ticker}:"]
    for a in articles:
        # Relative time
        age = ""
        if a["datetime"]:
            diff = now_ts - a["datetime"]
            if diff < 3600:
                age = f"{int(diff/60)}m ago"
            elif diff < 86400:
                age = f"{int(diff/3600)}h ago"
            else:
                age = f"{int(diff/86400)}d ago"
        src = a["source"][:12] if a["source"] else ""
        tag = f"[{src}] " if src else ""
        time_tag = f" ({age})" if age else ""
        lines.append(f"• {tag}{a['headline'][:80]}{time_tag}")
        if a["url"]:
            lines.append(f"  {a['url']}")
    # AI sentiment summary at the bottom
    try:
        sent = _score_news_sentiment(ticker)
        if sent["catalyst"]:
            lines.append(f"\nSentiment: {sent['sentiment']:+d}/100 — {sent['catalyst']}")
        else:
            lines.append(f"\nSentiment: {sent['sentiment']:+d}/100")
    except Exception:
        pass
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
    plt.savefig(buf, format="png", dpi=200, bbox_inches="tight",
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
        await update.message.reply_document(
            document=buf,
            filename="chart.png",
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
        _ext_session = get_trading_session() != "regular"

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
            # During extended/closed hours, try real extended price
            if _ext_session:
                ext = _get_extended_price(t)
                if ext and ext.get("price"):
                    ext_p = ext["price"]
                    reg_p = ext.get("regular_close") or price
                    if reg_p:
                        chg = (ext_p - reg_p) / reg_p * 100
                    price = ext_p
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
        ax.set_title(title, color=DIM, fontsize=14,
                     fontweight="bold", loc="left", pad=5)

    def _barh_chart(ax, names, values, bar_colors, *, price_strs=None):
        """
        Draw a clean horizontal bar chart.
        - Labels placed at a FIXED right-edge position (axes coords) so they
          never collide with the y-axis or each other regardless of value.
        - X axis always has a minimum range so bars are visible near-zero.
        """
        ys = list(range(len(names)))
        ax.barh(ys, values, color=bar_colors, height=0.65, zorder=3)
        ax.set_yticks(ys)
        ax.set_yticklabels(names, color=TEXT, fontsize=14)
        ax.axvline(0, color=DIM, linewidth=0.7, zorder=2)
        ax.xaxis.grid(True, color=GRID, linewidth=0.7, zorder=1)
        ax.set_axisbelow(True)
        ax.tick_params(axis="x", colors=DIM, labelsize=10)
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
                        va="center", ha="left", color=TEXT, fontsize=12,
                        clip_on=False)
            else:
                ax.text(v - x_offset, i, pct_label,
                        va="center", ha="right", color=TEXT, fontsize=12,
                        clip_on=False)

        # Price label: always at fixed right edge in axes coords
        if price_strs:
            for i, ps in enumerate(price_strs):
                ax.text(1.01, (i + 0.5) / len(names),
                        ps, va="center", ha="left",
                        color=DIM, fontsize=13,
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
             color=TEXT, fontsize=20, fontweight="bold")
    fig.text(0.08, 0.974, now_str, color=DIM, fontsize=14)
    fig.text(0.55, 0.974,
             f"Market: {session.upper()}",
             color=session_color, fontsize=14, fontweight="bold")
    # Claude AI one-liner — truncate to ~80 chars for narrow layout
    gl = grok_line[:80] + ("\u2026" if len(grok_line) > 80 else "")
    fig.text(0.08, 0.966, f"Claude AI: {gl}",
             color=GOLD, fontsize=13, style="italic")

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
    ax_fg.set_title("FEAR & GREED", color=DIM, fontsize=14,
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
                       ha="center", va="center", fontsize=8,
                       color="white", fontweight="bold", zorder=3)

    na = np.radians(180 - fg_val * 1.8)
    ax_fg.annotate("",
        xy=(0.6 * np.cos(na), 0.6 * np.sin(na)), xytext=(0, 0),
        arrowprops=dict(arrowstyle="->,head_width=0.08,head_length=0.05",
                        color="white", lw=2), zorder=5)
    ax_fg.add_patch(plt.Circle((0, 0), 0.07, color=PANEL, zorder=4))
    ax_fg.text(0, -0.18, str(fg_val), ha="center", va="center",
               fontsize=30, fontweight="bold", color=TEXT, zorder=5)
    ax_fg.text(0, -0.29, fg_label or "", ha="center", va="center",
               fontsize=12, color=GOLD, zorder=5)

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
                    fontsize=11, fontweight="bold", color=tc,
                    transform=ax_sec.transAxes)
        ax_sec.text(cx, cy - 0.045, f"{val:+.2f}%", ha="center", va="center",
                    fontsize=10, color=tc, transform=ax_sec.transAxes)

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
                   color=DIM, fontsize=13, transform=ax_gn.transAxes)
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
                   color=DIM, fontsize=13, transform=ax_ls.transAxes)
        ax_ls.axis("off")

    # ── [F] Squeeze Leaderboard (full width) ──────────────────
    ax_sq = fig.add_subplot(gs[4, :])
    _setup_panel(ax_sq, "SQUEEZE LEADERBOARD  (score 0-100)")
    if top_squeeze:
        sq_names  = [t for t, _ in top_squeeze]
        sq_scores = [s for _, s in top_squeeze]
        ys_sq = list(range(len(sq_names)))
        sq_cols = [plt.cm.YlOrRd(s / 100) for s in sq_scores]
        ax_sq.barh(ys_sq, sq_scores, color=sq_cols, height=0.65, zorder=3)
        ax_sq.set_xlim(0, 115)
        ax_sq.set_yticks(ys_sq)
        ax_sq.set_yticklabels(sq_names, color=TEXT, fontsize=14)
        ax_sq.tick_params(axis="x", colors=DIM, labelsize=10)
        ax_sq.tick_params(axis="y", length=0)
        ax_sq.xaxis.grid(True, color=GRID, linewidth=0.7, zorder=1)
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
                       va="center", ha="left", color=TEXT, fontsize=12)
    else:
        ax_sq.text(0.5, 0.5, "Building\u2026 (needs 2-3 scan cycles)",
                   ha="center", va="center", color=DIM, fontsize=13,
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
                   color=DIM, fontsize=13, transform=ax_cr.transAxes)
        ax_cr.axis("off")

    # ── [H] Recent Spike Alerts (full width) ──────────────────
    ax_al = fig.add_subplot(gs[6, :])
    ax_al.set_facecolor(PANEL)
    for sp in ax_al.spines.values():
        sp.set_edgecolor(EDGE)
    ax_al.set_title("RECENT SPIKE ALERTS", color=DIM, fontsize=14,
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
                   fontsize=13, transform=ax_al.transAxes,
                   clip_on=True)

    # ── [I] Bot Status ────────────────────────────────────────
    ax_st = fig.add_subplot(gs[7, 0])
    ax_st.set_facecolor(PANEL)
    for sp in ax_st.spines.values():
        sp.set_edgecolor(EDGE)
    ax_st.set_title("BOT STATUS", color=DIM, fontsize=14,
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
                   fontsize=13, transform=ax_st.transAxes)

    # ── [J] AI Picks Summary (NEW panel) ──────────────────────
    ax_ai = fig.add_subplot(gs[7, 1])
    ax_ai.set_facecolor(PANEL)
    for sp in ax_ai.spines.values():
        sp.set_edgecolor(EDGE)
    ax_ai.set_title("AI PICKS", color=DIM, fontsize=14,
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
                       ha="left", va="top", color=color, fontsize=13,
                       transform=ax_ai.transAxes)
    else:
        ax_ai.text(0.5, 0.5, "No AI picks yet", ha="center", va="center",
                   color=DIM, fontsize=13, transform=ax_ai.transAxes)

    # ── Save ──────────────────────────────────────────────────
    buf = BytesIO()
    plt.savefig(buf, format="png", dpi=220, bbox_inches="tight",
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
        await update.message.reply_document(
            document=buf,
            filename="dashboard.png",
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
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
            data={"chat_id": CHAT_ID, "caption": caption},
            files={"document": ("dashboard.png", buf, "image/png")},
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
    """Returns a contextual note string when market is closed or in extended hours."""
    session = get_trading_session()
    if session == "regular":
        return ""
    if session == "extended":
        return "(Extended hours \u2014 live)"
    # closed
    now = datetime.now(CT)
    if now.weekday() >= 5:
        return "(Weekend \u2014 market reopens Monday)"
    t = now.time()
    if t < datetime.strptime("07:00", "%H:%M").time():
        return "(Pre-market \u2014 updating)"
    return "(After hours \u2014 updating)"


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
# /backtest COMMAND
# ============================================================
async def cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /backtest [days] [tp=X sl=X trail=X threshold=X max_pos=X]
    Run a backtest on logged signal data with custom parameters.
    Falls back to API data if signal log is insufficient.
    """
    args = context.args or []

    # Parse arguments
    days = 10
    custom_params = {}
    for arg in args:
        if "=" in arg:
            key, val = arg.split("=", 1)
            try:
                custom_params[key.lower().strip()] = float(val)
            except ValueError:
                pass
        else:
            try:
                days = int(arg)
            except ValueError:
                pass

    days = max(1, min(60, days))

    # Trading parameters (defaults from bot config, overridable)
    bt_tp = custom_params.get("tp", user_config.get("take_profit", 0.10) * 100) / 100
    bt_sl = custom_params.get("sl", user_config.get("stop_loss", 0.06) * 100) / 100
    bt_trail = custom_params.get("trail", user_config.get("trailing", 0.03) * 100) / 100
    bt_threshold = custom_params.get("threshold", user_config.get("threshold", 65))
    bt_max_pos = int(custom_params.get("max_pos", user_config.get("max_positions", 8)))

    # Handle percentage inputs (if user passes tp=10 meaning 10%, convert to 0.10)
    if bt_tp > 1: bt_tp /= 100
    if bt_sl > 1: bt_sl /= 100
    if bt_trail > 1: bt_trail /= 100

    param_str = (f"TP={bt_tp*100:.1f}% | SL={bt_sl*100:.1f}% | "
                 f"Trail={bt_trail*100:.1f}% | Thresh={bt_threshold:.0f} | "
                 f"MaxPos={bt_max_pos}")

    await update.message.reply_text(
        f"⏳ Running {days}-day backtest...\n"
        f"Parameters: {param_str}\n"
        f"Loading signal data..."
    )

    # Load logged signal data
    entries = load_signal_log(days=days)
    signal_entries = [e for e in entries if e.get("type") == "signal"]

    if len(signal_entries) < 10:
        await update.message.reply_text(
            f"⚠️ Only {len(signal_entries)} signal entries in last {days} days.\n"
            f"Need at least a few days of logged data for meaningful backtest.\n"
            f"The bot logs data during market hours \u2014 try again after more data accumulates."
        )
        return

    # Run replay backtest
    try:
        results = _run_replay_backtest(
            signal_entries, bt_tp, bt_sl, bt_trail, bt_threshold, bt_max_pos
        )
    except Exception as e:
        logger.error(f"Backtest replay error: {e}")
        await update.message.reply_text(f"\u274c Backtest error: {e}")
        return

    # Generate PDF report
    try:
        report_path = os.path.join(
            os.path.dirname(SIGNAL_LOG_FILE) or ".",
            "backtest_replay_report.pdf"
        )
        _generate_replay_report(results, report_path, days, param_str,
                                 bt_tp, bt_sl, bt_trail, bt_threshold, bt_max_pos)

        # Send PDF via Telegram
        _sep = '\u2500' * 28
        with open(report_path, "rb") as pdf_file:
            await update.message.reply_document(
                document=pdf_file,
                filename=f"backtest_{days}d_{datetime.now(CT).strftime('%Y%m%d_%H%M')}.pdf",
                caption=(
                    f"\U0001f4ca {days}-Day Backtest Report\n"
                    f"{_sep}\n"
                    f"Return: {results['total_return_pct']:+.2f}%\n"
                    f"Max DD: -{results['max_drawdown']:.2f}%\n"
                    f"Trades: {results['total_trades']} ({results['win_rate']:.0f}% win)\n"
                    f"{_sep}\n"
                    f"{param_str}"
                )
            )
    except Exception as e:
        logger.error(f"Backtest report generation error: {e}")
        await update.message.reply_text(f"\u274c Report generation error: {e}")


def _run_replay_backtest(entries, tp, sl, trail, threshold, max_pos):
    """
    Replay backtest using logged signal data.
    Uses logged composite scores and prices, applies custom trading rules.
    """
    starting_capital = PAPER_STARTING_CAPITAL
    cash = starting_capital
    positions = {}  # {ticker: {shares, avg_cost, high, entry_date}}
    trades = []
    daily_values = []

    # Group entries by date
    from collections import OrderedDict
    daily_entries = OrderedDict()
    for e in sorted(entries, key=lambda x: x.get("ts", "")):
        date = e.get("ts", "")[:10]
        if date not in daily_entries:
            daily_entries[date] = {}
        ticker = e.get("ticker", "")
        if ticker:
            # Keep the latest entry per ticker per day (most recent signal)
            daily_entries[date][ticker] = e

    # Track latest prices per ticker across days
    latest_prices = {}

    for date_str, ticker_signals in daily_entries.items():
        # Update latest prices from today's signals
        for ticker, sig in ticker_signals.items():
            p = sig.get("price")
            if p and p > 0:
                latest_prices[ticker] = p

        # ── Check existing positions for exits ──
        tickers_to_sell = []
        for ticker in list(positions.keys()):
            pos = positions[ticker]
            # Use today's logged price if available, else last known
            price = None
            if ticker in ticker_signals:
                price = ticker_signals[ticker].get("price")
            if not price:
                price = latest_prices.get(ticker)
            if not price:
                continue

            cost = pos["avg_cost"]
            pnl_pct = (price - cost) / cost

            if price > pos.get("high", cost):
                positions[ticker]["high"] = price

            sell_reason = ""
            if pnl_pct <= -sl:
                sell_reason = f"HARD-STOP {pnl_pct*100:.1f}%"
            else:
                # Graduated trailing stop
                high = pos.get("high", cost)
                peak_pnl_pct = (high - cost) / cost
                g_trail = _graduated_trail_pct(peak_pnl_pct)
                if price <= high * (1 - g_trail):
                    peak_pnl = peak_pnl_pct * 100
                    sell_reason = (
                        f"TRAILING-STOP {pnl_pct*100:+.1f}%"
                        f" (peak +{peak_pnl:.1f}%,"
                        f" trail {g_trail*100:.0f}%)"
                    )

            if not sell_reason:
                # Check signal collapse using logged score
                # Skip same-day entries (min hold ~1 day in backtester)
                if ticker in ticker_signals and pos.get("entry_date") != date_str:
                    sig_score = ticker_signals[ticker].get("composite_score", 50)
                    if sig_score <= 20 and pnl_pct >= 0.02:  # v2.7.7: match live params (2% min profit)
                        sell_reason = f"SIGNAL-COLLAPSE score={sig_score:.0f}"

            # Check AVWAP stop for same-day entries
            if not sell_reason and pos.get("entry_date") == date_str:
                if ticker in ticker_signals:
                    avwap_reclaimed = ticker_signals[ticker].get("avwap_reclaimed")
                    if avwap_reclaimed is False and pos.get("high", cost) > (ticker_signals[ticker].get("avwap") or 0):
                        avwap_val = ticker_signals[ticker].get("avwap", 0)
                        if avwap_val and price < avwap_val * 0.998:
                            sell_reason = f"AVWAP-STOP ${price:.2f} < ${avwap_val:.2f}"

            if sell_reason:
                tickers_to_sell.append((ticker, price, sell_reason, pnl_pct))

        for ticker, price, reason, pnl_pct in tickers_to_sell:
            pos = positions[ticker]
            shares = pos["shares"]
            proceeds = shares * price
            cost_b = shares * pos["avg_cost"]
            realized = proceeds - cost_b
            cash += proceeds
            entry_date = pos.get("entry_date", "")
            del positions[ticker]
            trades.append({
                "action": "SELL", "ticker": ticker, "shares": shares,
                "price": price, "pnl": realized, "pnl_pct": pnl_pct * 100,
                "reason": reason, "date": date_str, "entry_date": entry_date,
            })

        # ── Check for new buys ──
        scored_tickers = sorted(
            ticker_signals.items(),
            key=lambda x: x[1].get("composite_score", 0),
            reverse=True
        )

        for ticker, sig in scored_tickers:
            if ticker in positions:
                continue
            if len(positions) >= max_pos:
                break
            if cash < 200:
                break

            sig_score = sig.get("composite_score", 0)

            # Apply adaptive threshold if F&G and VIX are logged
            effective_threshold = threshold
            fg = sig.get("fg_index")
            vix = sig.get("vix")
            if fg is not None and vix is not None:
                # Simplified adaptive: adjust threshold based on F&G/VIX
                adj = 0
                if fg and fg <= 25: adj -= 8
                elif fg and fg <= 40: adj -= 4
                elif fg and fg >= 75: adj += 10
                elif fg and fg >= 60: adj += 5
                if vix and vix >= 30: adj += 5
                elif vix and vix <= 15: adj -= 3
                effective_threshold = max(48, min(85, threshold + adj))

            if sig_score < effective_threshold:
                continue

            # AVWAP gate: skip if price below AVWAP
            avwap_reclaimed = sig.get("avwap_reclaimed")
            if avwap_reclaimed is not None and avwap_reclaimed is False:
                session = sig.get("session", "regular")
                if session == "regular":
                    continue

            price = sig.get("price", 0)
            if not price or price <= 0:
                continue

            # RSI overbought guard
            rsi = sig.get("rsi")
            if rsi and rsi > 72:
                continue

            # Position sizing (simplified)
            pv = cash + sum(
                latest_prices.get(t, pos["avg_cost"]) * pos["shares"]
                for t, pos in positions.items()
            )
            max_dollars = pv * 0.20
            strength = min(1.0, (sig_score - effective_threshold) / max(1, (100 - effective_threshold)))
            dollars = max_dollars * (0.5 + 0.5 * strength)
            dollars = min(dollars, cash * 0.95)
            if dollars < 100:
                continue
            shares = max(1, int(dollars / price))
            cost = shares * price

            cash -= cost
            positions[ticker] = {
                "shares": shares, "avg_cost": price,
                "high": price, "entry_date": date_str,
            }
            trades.append({
                "action": "BUY", "ticker": ticker, "shares": shares,
                "price": price, "cost": cost, "signal_score": sig_score,
                "date": date_str,
            })

        # End-of-day portfolio value
        pv = cash
        for t, pos in positions.items():
            p = latest_prices.get(t, pos["avg_cost"])
            pv += pos["shares"] * p
        daily_values.append((date_str, pv))

    # Close remaining positions at last known prices
    if daily_values:
        final_date = daily_values[-1][0]
        for ticker in list(positions.keys()):
            pos = positions[ticker]
            price = latest_prices.get(ticker, pos["avg_cost"])
            shares = pos["shares"]
            proceeds = shares * price
            cost_b = shares * pos["avg_cost"]
            realized = proceeds - cost_b
            pnl_pct = (price - pos["avg_cost"]) / pos["avg_cost"] * 100
            cash += proceeds
            trades.append({
                "action": "SELL", "ticker": ticker, "shares": shares,
                "price": price, "pnl": realized, "pnl_pct": pnl_pct,
                "reason": "END-OF-BACKTEST", "date": final_date,
                "entry_date": pos.get("entry_date", ""),
            })
        positions.clear()

    # Compute results
    sells = [t for t in trades if t["action"] == "SELL"]
    buys = [t for t in trades if t["action"] == "BUY"]
    winners = [t for t in sells if t.get("pnl", 0) > 0]
    losers = [t for t in sells if t.get("pnl", 0) <= 0]

    final_val = daily_values[-1][1] if daily_values else starting_capital
    total_pnl = sum(t.get("pnl", 0) for t in sells)

    # Drawdown
    peak = starting_capital
    max_dd = 0
    max_dd_date = ""
    for d, v in daily_values:
        if v > peak: peak = v
        dd = (peak - v) / peak
        if dd > max_dd:
            max_dd = dd
            max_dd_date = d

    # Per-ticker P&L
    ticker_pnl = defaultdict(lambda: {"trades": 0, "pnl": 0, "wins": 0})
    for t in sells:
        tk = t["ticker"]
        ticker_pnl[tk]["trades"] += 1
        ticker_pnl[tk]["pnl"] += t.get("pnl", 0)
        if t.get("pnl", 0) > 0:
            ticker_pnl[tk]["wins"] += 1

    # Exit reason breakdown
    reason_counts = defaultdict(int)
    for t in sells:
        r = t.get("reason", "")
        for key in ["TAKE-PROFIT", "HARD-STOP", "TRAILING-STOP", "SIGNAL-COLLAPSE", "AVWAP-STOP", "END-OF-BACKTEST"]:
            if key in r:
                reason_counts[key] += 1
                break

    # Hold time
    hold_days = []
    for t in sells:
        ed, sd = t.get("entry_date"), t.get("date")
        if ed and sd:
            try:
                d1 = datetime.strptime(ed, "%Y-%m-%d")
                d2 = datetime.strptime(sd, "%Y-%m-%d")
                hold_days.append((d2 - d1).days)
            except ValueError:
                pass

    return {
        "starting_capital": starting_capital,
        "final_value": final_val,
        "total_return_pct": (final_val - starting_capital) / starting_capital * 100,
        "total_pnl": total_pnl,
        "total_trades": len(buys) + len(sells),
        "total_buys": len(buys),
        "total_sells": len(sells),
        "winners": len(winners),
        "losers": len(losers),
        "win_rate": len(winners) / len(sells) * 100 if sells else 0,
        "avg_win": (sum(t["pnl"] for t in winners) / len(winners)) if winners else 0,
        "avg_loss": (sum(t["pnl"] for t in losers) / len(losers)) if losers else 0,
        "best_trade": max(sells, key=lambda t: t.get("pnl", 0)) if sells else None,
        "worst_trade": min(sells, key=lambda t: t.get("pnl", 0)) if sells else None,
        "max_drawdown": max_dd * 100,
        "max_drawdown_date": max_dd_date,
        "ticker_pnl": dict(ticker_pnl),
        "reason_counts": dict(reason_counts),
        "daily_values": daily_values,
        "avg_hold_days": (sum(hold_days) / len(hold_days)) if hold_days else 0,
        "trades": trades,
        "data_entries": len(entries),
    }


def _generate_replay_report(results, output_path, num_days, param_str,
                             tp, sl, trail, threshold, max_pos):
    """Generate a dark-themed PDF backtest report from replay results."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import matplotlib.ticker as mticker
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.gridspec import GridSpec

    # Dark theme colors (same as backtest.py)
    BG       = "#0f1117"
    PANEL_BG = "#1a1d27"
    TEXT     = "#e0e0e0"
    ACCENT   = "#4fc3f7"
    GREEN    = "#66bb6a"
    RED      = "#ef5350"
    AMBER    = "#ffa726"
    GRID     = "#2a2d37"
    MUTED    = "#888888"

    def _style_ax(ax, title=""):
        ax.set_facecolor(PANEL_BG)
        ax.tick_params(colors=TEXT, labelsize=8)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["left"].set_color(GRID)
        ax.spines["bottom"].set_color(GRID)
        ax.grid(True, color=GRID, linewidth=0.5, alpha=0.5)
        if title:
            ax.set_title(title, fontsize=11, color=TEXT, fontweight="bold", pad=8, loc="left")

    dates = [r[0] for r in results["daily_values"]]
    values = [r[1] for r in results["daily_values"]]
    x_dates = [datetime.strptime(d, "%Y-%m-%d") for d in dates]

    with PdfPages(output_path) as pdf:
        # PAGE 1: Summary + Equity Curve
        fig = plt.figure(figsize=(11, 8.5), facecolor=BG)
        gs = GridSpec(3, 2, figure=fig, hspace=0.45, wspace=0.35,
                      left=0.08, right=0.95, top=0.88, bottom=0.08)

        fig.text(0.08, 0.95, "BACKTEST REPLAY REPORT", fontsize=18, color=ACCENT,
                 fontweight="bold", fontfamily="monospace")
        fig.text(0.08, 0.91,
                 f"{num_days}d logged data  |  {param_str}  |  {datetime.now(CT).strftime('%Y-%m-%d %H:%M')}",
                 fontsize=8, color=MUTED, fontfamily="monospace")

        # Equity curve
        ax1 = fig.add_subplot(gs[0, :])
        _style_ax(ax1, "Portfolio Value")
        if x_dates:
            ax1.plot(x_dates, values, color=ACCENT, linewidth=1.5, zorder=3)
            ax1.fill_between(x_dates, results["starting_capital"], values,
                             where=[v >= results["starting_capital"] for v in values],
                             color=GREEN, alpha=0.15, interpolate=True)
            ax1.fill_between(x_dates, results["starting_capital"], values,
                             where=[v < results["starting_capital"] for v in values],
                             color=RED, alpha=0.15, interpolate=True)
            ax1.axhline(results["starting_capital"], color=MUTED, linewidth=0.8, linestyle="--", zorder=2)
            ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"${x:,.0f}"))
            ax1.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
            ax1.tick_params(axis="x", rotation=30)

        # Trade markers
        for t in results["trades"]:
            try:
                td = datetime.strptime(t["date"], "%Y-%m-%d")
                if t["action"] == "BUY":
                    ax1.scatter(td, results["starting_capital"], marker="^", color=GREEN, s=18, zorder=5, alpha=0.7)
                elif t["action"] == "SELL" and "END-OF-BACKTEST" not in t.get("reason", ""):
                    ax1.scatter(td, results["starting_capital"], marker="v", color=RED, s=18, zorder=5, alpha=0.7)
            except (ValueError, KeyError):
                pass

        # KPI panel
        ax2 = fig.add_subplot(gs[1, 0])
        ax2.set_facecolor(PANEL_BG)
        ax2.axis("off")
        ret = results["total_return_pct"]
        ret_color = GREEN if ret >= 0 else RED
        kpis = [
            ("Starting Capital",  f"${results['starting_capital']:,.0f}", TEXT),
            ("Final Value",       f"${results['final_value']:,.0f}", ret_color),
            ("Total Return",      f"{ret:+.2f}%", ret_color),
            ("Total P&L",         f"${results['total_pnl']:+,.0f}", ret_color),
            ("Max Drawdown",      f"-{results['max_drawdown']:.2f}%", AMBER),
            ("Avg Hold (days)",   f"{results['avg_hold_days']:.1f}", TEXT),
        ]
        for i, (label, val, col) in enumerate(kpis):
            y = 0.92 - i * 0.155
            ax2.text(0.05, y, label, fontsize=9, color=MUTED, transform=ax2.transAxes)
            ax2.text(0.95, y, val, fontsize=10, color=col, fontweight="bold",
                     transform=ax2.transAxes, ha="right")
        ax2.set_title("Key Metrics", fontsize=11, color=TEXT, fontweight="bold", pad=8, loc="left")

        # Trade stats
        ax3 = fig.add_subplot(gs[1, 1])
        ax3.set_facecolor(PANEL_BG)
        ax3.axis("off")
        tstats = [
            ("Total Buys",     f"{results['total_buys']}", ACCENT),
            ("Total Sells",    f"{results['total_sells']}", ACCENT),
            ("Winners",        f"{results['winners']}", GREEN),
            ("Losers",         f"{results['losers']}", RED),
            ("Win Rate",       f"{results['win_rate']:.1f}%", GREEN if results['win_rate'] >= 50 else RED),
            ("Avg Win / Loss", f"${results['avg_win']:+,.0f} / ${results['avg_loss']:+,.0f}", TEXT),
        ]
        for i, (label, val, col) in enumerate(tstats):
            y = 0.92 - i * 0.155
            ax3.text(0.05, y, label, fontsize=9, color=MUTED, transform=ax3.transAxes)
            ax3.text(0.95, y, val, fontsize=10, color=col, fontweight="bold",
                     transform=ax3.transAxes, ha="right")
        ax3.set_title("Trade Statistics", fontsize=11, color=TEXT, fontweight="bold", pad=8, loc="left")

        # Exit reasons
        ax4 = fig.add_subplot(gs[2, 0])
        _style_ax(ax4, "Exit Reasons")
        rc = results["reason_counts"]
        if rc:
            label_map = {
                "TAKE-PROFIT": "Take-Profit", "HARD-STOP": "Hard-Stop",
                "TRAILING-STOP": "Trailing", "SIGNAL-COLLAPSE": "Sig Collapse",
                "AVWAP-STOP": "AVWAP Stop", "END-OF-BACKTEST": "End Close",
            }
            labels = [label_map.get(k, k) for k in rc.keys()]
            vals = list(rc.values())
            colors_list = []
            for lbl_key in rc.keys():
                if "TAKE-PROFIT" in lbl_key: colors_list.append(GREEN)
                elif "HARD-STOP" in lbl_key: colors_list.append(RED)
                elif "TRAILING" in lbl_key: colors_list.append(AMBER)
                elif "AVWAP" in lbl_key: colors_list.append("#ce93d8")
                elif "SIGNAL" in lbl_key: colors_list.append("#9575cd")
                else: colors_list.append(MUTED)
            bars = ax4.barh(labels, vals, color=colors_list, height=0.6)
            ax4.set_xlabel("Count", fontsize=8, color=MUTED)
            for bar, v in zip(bars, vals):
                ax4.text(bar.get_width() + 0.3, bar.get_y() + bar.get_height()/2,
                         str(v), va="center", fontsize=8, color=TEXT)
        else:
            ax4.text(0.5, 0.5, "No exits", ha="center", va="center", color=MUTED, fontsize=10,
                     transform=ax4.transAxes)

        # Drawdown
        ax5 = fig.add_subplot(gs[2, 1])
        _style_ax(ax5, "Drawdown from Peak")
        peak = results["starting_capital"]
        drawdowns = []
        for d, v in results["daily_values"]:
            if v > peak: peak = v
            drawdowns.append(-(peak - v) / peak * 100)
        if drawdowns and x_dates:
            ax5.fill_between(x_dates, 0, drawdowns, color=RED, alpha=0.35)
            ax5.plot(x_dates, drawdowns, color=RED, linewidth=0.8)
        ax5.set_ylabel("%", fontsize=8, color=MUTED)
        if x_dates:
            ax5.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
            ax5.tick_params(axis="x", rotation=30)

        pdf.savefig(fig, facecolor=BG)
        plt.close(fig)

        # PAGE 2: Per-Ticker + Best/Worst
        fig2 = plt.figure(figsize=(11, 8.5), facecolor=BG)
        gs2 = GridSpec(2, 2, figure=fig2, hspace=0.4, wspace=0.35,
                       left=0.08, right=0.95, top=0.92, bottom=0.08)

        fig2.text(0.08, 0.96, "DETAILED ANALYSIS", fontsize=14, color=ACCENT,
                  fontweight="bold", fontfamily="monospace")

        # Per-ticker P&L
        ax6 = fig2.add_subplot(gs2[0, :])
        _style_ax(ax6, "P&L by Ticker")
        tp_data = results["ticker_pnl"]
        if tp_data:
            sorted_tickers = sorted(tp_data.items(), key=lambda x: x[1]["pnl"])
            t_labels = [t[0] for t in sorted_tickers]
            t_pnls = [t[1]["pnl"] for t in sorted_tickers]
            t_colors = [GREEN if p >= 0 else RED for p in t_pnls]
            ax6.barh(t_labels, t_pnls, color=t_colors, height=0.6)
            ax6.axvline(0, color=MUTED, linewidth=0.5)
            ax6.set_xlabel("P&L ($)", fontsize=8, color=MUTED)
            for i, (lbl, pnl) in enumerate(zip(t_labels, t_pnls)):
                ax6.text(pnl + (50 if pnl >= 0 else -50), i,
                         f"${pnl:+,.0f}", va="center", fontsize=7, color=TEXT,
                         ha="left" if pnl >= 0 else "right")

        # Best & worst trades
        ax7 = fig2.add_subplot(gs2[1, 0])
        ax7.set_facecolor(PANEL_BG)
        ax7.axis("off")
        ax7.set_title("Best & Worst Trades", fontsize=11, color=TEXT, fontweight="bold", pad=8, loc="left")
        best = results.get("best_trade")
        worst = results.get("worst_trade")
        y_pos = 0.85
        if best:
            ax7.text(0.05, y_pos, "Best:", fontsize=9, color=MUTED, transform=ax7.transAxes)
            ax7.text(0.05, y_pos-0.12, f"  {best['ticker']} ${best.get('pnl',0):+,.0f} ({best.get('pnl_pct',0):+.1f}%)",
                     fontsize=10, color=GREEN, transform=ax7.transAxes)
            ax7.text(0.05, y_pos-0.24, f"  {best.get('reason', '')}",
                     fontsize=8, color=MUTED, transform=ax7.transAxes)
        if worst:
            ax7.text(0.05, y_pos-0.42, "Worst:", fontsize=9, color=MUTED, transform=ax7.transAxes)
            ax7.text(0.05, y_pos-0.54, f"  {worst['ticker']} ${worst.get('pnl',0):+,.0f} ({worst.get('pnl_pct',0):+.1f}%)",
                     fontsize=10, color=RED, transform=ax7.transAxes)
            ax7.text(0.05, y_pos-0.66, f"  {worst.get('reason', '')}",
                     fontsize=8, color=MUTED, transform=ax7.transAxes)

        # Parameters used
        ax8 = fig2.add_subplot(gs2[1, 1])
        ax8.set_facecolor(PANEL_BG)
        ax8.axis("off")
        ax8.set_title("Backtest Parameters", fontsize=11, color=TEXT, fontweight="bold", pad=8, loc="left")
        params_display = [
            ("Data Source", "Signal Log (JSONL)", ACCENT),
            ("Period", f"{num_days} days", TEXT),
            ("Take Profit", f"{tp*100:.1f}%", TEXT),
            ("Stop Loss", f"{sl*100:.1f}%", TEXT),
            ("Trailing Stop", f"{trail*100:.1f}%", TEXT),
            ("Threshold", f"{threshold:.0f}", TEXT),
            ("Max Positions", f"{max_pos}", TEXT),
        ]
        for i, (label, val, col) in enumerate(params_display):
            y = 0.88 - i * 0.12
            ax8.text(0.05, y, label, fontsize=9, color=MUTED, transform=ax8.transAxes)
            ax8.text(0.95, y, val, fontsize=10, color=col, fontweight="bold",
                     transform=ax8.transAxes, ha="right")

        pdf.savefig(fig2, facecolor=BG)
        plt.close(fig2)

    logger.info(f"Replay backtest report saved to {output_path}")


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

    # Build compact What's New from latest 2 release notes
    whats_new = []
    for note in RELEASE_NOTES[:2]:
        # Extract "Feature Name: brief..." after the "X.Y — "
        parts = note.split(" — ", 1)
        if len(parts) == 2:
            # Take title before colon, add short summary
            title_rest = parts[1].split(": ", 1)
            title = title_rest[0]
            summary = title_rest[1][:30] if len(title_rest) > 1 else ""
            line = f"• {title}: {summary}" if summary else f"• {title}"
            # Trim to 64 chars for mobile
            if len(line) > 64:
                line = line[:61] + "..."
            whats_new.append(line)

    separator = "─" * 31

    msg_lines = (
        [f"🚀 STOCK SPIKE MONITOR v{BOT_VERSION}",
         f"{s['now_label']}",
         separator,
         f"📋 What's New:"] +
        whats_new +
        [separator,
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


def send_tp_startup_message():
    """Send startup message to TP user's DM (or channel fallback)."""
    chat_id = tp_dm_chat_id or TELEGRAM_TP_CHAT_ID
    if not chat_id:
        return
    mode = user_config.get("trading_mode", "paper")
    settled, unsettled, pending = get_settled_cash()
    sp = tp_state.get("shadow_portfolio",
                       _default_shadow_portfolio())
    sp_cash = sp.get("cash", 0)
    sp_positions = sp.get("positions", {})
    # Compute live TP portfolio value
    pos_value = 0
    for tick, p in sp_positions.items():
        s = p.get("shares", 0)
        a = p.get("avg_price", 0)
        try:
            r = _get_best_price(tick)
            cp = (r[0] if isinstance(r, tuple) else r) or a
        except Exception:
            cp = a
        pos_value += s * cp
    sp_val = sp_cash + pos_value
    sep = "━" * 31
    dest = "DM" if tp_dm_chat_id else "Channel"
    if unsettled > 0:
        settle_str = (
            f"${unsettled:,.0f} unsettled"
            f" ({len(pending)} sells)"
        )
    else:
        settle_str = "all settled"
    mode_label = (
        "Active" if mode == "shadow" else "Disabled"
    )
    lines = [
        f"📡 Stock Spike Monitor v{BOT_VERSION}",
        f"TradersPost {dest} Active",
        sep,
        f"TP Trading: {mode_label}",
        f"Account: Cash (no PDT limits)",
        f"Settlement: {settle_str}",
        f"TP Portfolio: ${sp_val:,.0f}",
    ]
    send_tp_telegram("\n".join(lines))


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
        day = "daily" (weekdays only) | "everyday" | "monday"…"sunday"
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
        ("daily",        "15:05",  _analyze_signal_effectiveness),
        ("daily",        "16:05",  send_daily_pnl_summary),
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
            if now_hhmm == hhmm and (day == "daily" and now_ct.weekday() < 5 or day == "everyday" or day == now_day):
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

        # ── Feature #7: Portfolio snapshot every 5 minutes during market ──
        if get_trading_session() in ("regular", "extended"):
            snap_elapsed = (now_ct - _last_snapshot_time).total_seconds() / 60
            if snap_elapsed >= 5:
                globals()["_last_snapshot_time"] = now_ct
                try:
                    _portfolio_snapshots.append((now_ct, paper_portfolio_value()))
                except Exception:
                    pass

        # ── Periodic state persistence — every 5 minutes ─────
        state_elapsed = (now_ct - last_state_save).total_seconds() / 60
        if state_elapsed >= 5:
            last_state_save = now_ct
            threading.Thread(target=save_bot_state, daemon=True).start()

        time.sleep(30)   # check twice per minute — plenty for minute-precision jobs


# ============================================================
# MAIN — Telegram bot
# ============================================================
async def cmd_tp_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command for the TP bot — TP commands only."""
    _capture_tp_chat(update)
    mode = user_config.get("trading_mode", "paper")
    sep = "━" * 29
    await update.message.reply_text(
        f"📡 TradersPost Bot — Help\n"
        f"{sep}\n"
        f"/shadow  Toggle live trading\n"
        f"/tp      Status & recent orders\n"
        f"/tppos   Portfolio positions\n"
        f"/settlement  T+1 settlement\n"
        f"/tpsync  Sync portfolio\n"
        f"  reset \u2014 reset to starting cash\n"
        f"  status \u2014 current breakdown\n"
        f"/tpedit  Edit portfolio\n"
        f"  add TICK QTY PRICE\n"
        f"  remove TICK\n"
        f"  shares TICK QTY\n"
        f"  cash AMOUNT | clear\n"
        f"/set     View/change trading config\n"
        f"/help    Show this menu\n"
        f"{sep}\n"
        f"Mode: {mode}"
    )


async def cmd_tp_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message for the TP bot."""
    _capture_tp_chat(update)
    mode = user_config.get("trading_mode", "paper")
    shadow_str = "ON" if mode == "shadow" else "OFF"
    webhook_str = ("connected" if TRADERSPOST_WEBHOOK_URL
                   else "not set")
    settled, unsettled, _ = get_settled_cash()
    sep = "━" * 29
    await update.message.reply_text(
        f"📡 TradersPost Trading Bot\n"
        f"{sep}\n"
        f"Executes trades via TradersPost\n"
        f"using signals from the main bot.\n"
        f"\n"
        f"Live trading: {shadow_str}\n"
        f"Webhook: {webhook_str}\n"
        f"Cash: ${settled:,.2f} settled\n"
        f"      ${unsettled:,.2f} pending\n"
        f"{sep}\n"
        f"/shadow  Toggle live trading\n"
        f"/tp      Status & recent orders\n"
        f"/tppos   Portfolio positions\n"
        f"/settlement  Settlement status\n"
        f"/tpsync  Sync portfolio\n"
        f"/tpedit  Edit portfolio\n"
        f"/set     Trading config\n"
        f"/help    Full help menu"
    )


# ── Telegram command menus (/ autocomplete) ──────────────────
MAIN_BOT_COMMANDS = [
    BotCommand("help",       "Full command menu"),
    BotCommand("overview",   "Market indices + AI read"),
    BotCommand("dashboard",  "Visual market snapshot"),
    BotCommand("paper",      "Paper portfolio overview"),
    BotCommand("perf",       "Performance dashboard"),
    BotCommand("price",      "Live quote — /price TICK"),
    BotCommand("chart",      "Price chart — /chart TICK"),
    BotCommand("analyze",    "AI analysis — /analyze TICK"),
    BotCommand("compare",    "Side-by-side — /compare A B"),
    BotCommand("rsi",        "RSI + Bollinger — /rsi TICK"),
    BotCommand("news",       "Headlines — /news TICK"),
    BotCommand("movers",     "Gainers, losers, active"),
    BotCommand("crypto",     "BTC ETH SOL DOGE XRP"),
    BotCommand("macro",      "CPI, Fed, NFP, FOMC"),
    BotCommand("earnings",   "Earnings calendar"),
    BotCommand("spikes",     "Recent spike alerts"),
    BotCommand("alerts",     "All alerts today"),
    BotCommand("squeeze",    "Top squeeze candidates"),
    BotCommand("setalert",   "Set price alert"),
    BotCommand("myalerts",   "View active alerts"),
    BotCommand("delalert",   "Remove alert"),
    BotCommand("watchlist",  "Manage watchlist"),
    BotCommand("backtest",   "Replay backtest"),
    BotCommand("aistocks",   "AI picks + conviction"),
    BotCommand("ask",        "Chat with Claude"),
    BotCommand("prep",       "Next session plan"),
    BotCommand("wlprep",     "Watchlist deep scan"),
    BotCommand("overnight",  "Gap risk on holdings"),
    BotCommand("vixalert",   "VIX put-selling setup"),
    BotCommand("set",        "Adjust trading config"),
    BotCommand("list",       "Monitored tickers"),
    BotCommand("monitoring", "Pause/resume scanner"),
    BotCommand("version",    "Release notes"),
]

TP_BOT_COMMANDS = [
    BotCommand("help",       "Full help menu"),
    BotCommand("shadow",     "Toggle live trading"),
    BotCommand("tp",         "Status + recent orders"),
    BotCommand("tppos",      "Portfolio positions"),
    BotCommand("settlement", "T+1 settlement status"),
    BotCommand("tpsync",     "Sync portfolio"),
    BotCommand("tpedit",     "Edit portfolio"),
    BotCommand("set",        "Trading config"),
]


async def _set_bot_commands(app: Application) -> None:
    """Register / menu commands on startup (post_init callback)."""
    try:
        await app.bot.set_my_commands(
            MAIN_BOT_COMMANDS,
            scope=BotCommandScopeAllPrivateChats(),
        )
        await app.bot.set_my_commands(
            MAIN_BOT_COMMANDS,
            scope=BotCommandScopeAllGroupChats(),
        )
        logger.info(
            f"Registered {len(MAIN_BOT_COMMANDS)} main "
            f"bot commands (private + group scope)"
        )
    except Exception as e:
        logger.warning(f"Failed to set main bot commands: {e}")


async def _set_tp_bot_commands(app: Application) -> None:
    """Register / menu commands for TP bot on startup."""
    try:
        await app.bot.set_my_commands(
            TP_BOT_COMMANDS,
            scope=BotCommandScopeAllPrivateChats(),
        )
        await app.bot.set_my_commands(
            TP_BOT_COMMANDS,
            scope=BotCommandScopeAllGroupChats(),
        )
        logger.info(
            f"Registered {len(TP_BOT_COMMANDS)} TP "
            f"bot commands (private + group scope)"
        )
    except Exception as e:
        logger.warning(f"Failed to set TP bot commands: {e}")


def run_telegram_bot():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(_set_bot_commands).build()

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
    app.add_handler(CommandHandler("version",     cmd_version))
    app.add_handler(CommandHandler("strategy",    cmd_strategy))

    # ── Paper Trading ─────────────────────────────────────────
    app.add_handler(CommandHandler("paper",       cmd_paper))
    app.add_handler(CommandHandler("perf",        cmd_perf))
    app.add_handler(CommandHandler("set",         cmd_set))
    app.add_handler(CommandHandler("overnight",   cmd_overnight))
    app.add_handler(CommandHandler("aistocks",    cmd_aistocks))
    app.add_handler(CommandHandler("vixalert",    cmd_vixalert))

    # ── TradersPost (main bot only if no separate TP token) ──
    if not TELEGRAM_TP_TOKEN:
        app.add_handler(CommandHandler("shadow",  cmd_shadow))
        app.add_handler(CommandHandler("tp",      cmd_tp))
        app.add_handler(CommandHandler("tppos",   cmd_tppos))
        app.add_handler(CommandHandler("settlement", cmd_settlement))
        app.add_handler(CommandHandler("tpsync",  cmd_tpsync))

    # ── Off-hours / prep ──────────────────────────────────────
    app.add_handler(CommandHandler("prep",        cmd_prep))
    app.add_handler(CommandHandler("wlprep",      cmd_watchlist_prep))

    app.add_handler(CommandHandler("ask",         cmd_ask))
    app.add_handler(CommandHandler("backtest",    cmd_backtest))
    app.add_handler(CommandHandler("buzz",        cmd_buzz))

    # ── Second bot for TP channel (separate token) ───────────
    if not TELEGRAM_TP_TOKEN:
        app.run_polling()
        return

    tp_app = Application.builder().token(TELEGRAM_TP_TOKEN).post_init(_set_tp_bot_commands).build()
    tp_app.add_handler(CommandHandler("shadow",  cmd_shadow))
    tp_app.add_handler(CommandHandler("tp",      cmd_tp))
    tp_app.add_handler(CommandHandler("settlement", cmd_settlement))
    tp_app.add_handler(CommandHandler("tpsync",  cmd_tpsync))
    tp_app.add_handler(CommandHandler("tpedit",  cmd_tpedit))
    tp_app.add_handler(CommandHandler("tppos",   cmd_tppos))
    tp_app.add_handler(CommandHandler("set",     cmd_set))
    tp_app.add_handler(CommandHandler("start",   cmd_tp_start))
    tp_app.add_handler(CommandHandler("help",    cmd_tp_help))

    import asyncio, signal

    async def _run_both():
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        async with app:
            async with tp_app:
                # Register / menus after init
                try:
                    await app.bot.set_my_commands(
                        MAIN_BOT_COMMANDS,
                        scope=BotCommandScopeAllPrivateChats(),
                    )
                    await app.bot.set_my_commands(
                        MAIN_BOT_COMMANDS,
                        scope=BotCommandScopeAllGroupChats(),
                    )
                    logger.info(
                        f"Registered {len(MAIN_BOT_COMMANDS)}"
                        f" main bot cmds (priv + group)"
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to set main cmds: {e}"
                    )
                try:
                    await tp_app.bot.set_my_commands(
                        TP_BOT_COMMANDS,
                        scope=BotCommandScopeAllPrivateChats(),
                    )
                    await tp_app.bot.set_my_commands(
                        TP_BOT_COMMANDS,
                        scope=BotCommandScopeAllGroupChats(),
                    )
                    logger.info(
                        f"Registered {len(TP_BOT_COMMANDS)}"
                        f" TP bot cmds (priv + group)"
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to set TP cmds: {e}"
                    )
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
logger.info(f"Stock Spike Monitor v{BOT_VERSION} started")
send_startup_message()
send_tp_startup_message()
run_telegram_bot()
