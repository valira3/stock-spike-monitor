import yfinance as yf
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
import pytz
import logging
from collections import deque
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

THRESHOLD           = 0.03
MIN_PRICE           = 5.0
COOLDOWN_MINUTES    = 5
CHECK_INTERVAL_MIN  = 1
VOLUME_SPIKE_MULT   = 2.0
LOG_FILE            = "stock_spike_monitor.log"

# -- Claude models ---------------------------------------------
# Sonnet  ? deep analysis, /ask, briefings, macro, compare
# Haiku   ? high-frequency: spike alerts, signal scores, dashboard one-liner
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
# AI CLIENTS ï¿½ Claude primary, Grok fallback
# ============================================================
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
grok_client   = OpenAI(api_key=GROK_API_KEY, base_url="https://api.x.ai/v1") if GROK_API_KEY else None

if claude_client:
    logger.info("AI: Claude (primary) initialised")
elif grok_client:
    logger.info("AI: Grok (fallback only ï¿½ no ANTHROPIC_API_KEY set)")
else:
    logger.warning("AI: No AI client available ï¿½ set ANTHROPIC_API_KEY in Railway")

# ============================================================
# BOT DESCRIPTION (used by /about and natural-language handler)
# ============================================================
BOT_DESCRIPTION = (
    "?? Stock Spike Monitor\n"
    "24/7 ï¿½ 60+ stocks ï¿½ =3% spike alerts ï¿½ Claude AI ï¿½ RSI/BB/Squeeze\n"
    "\n"
    "MARKET PULSE\n"
    "  /overview            indices ï¿½ sectors ï¿½ Fear & Greed ï¿½ AI outlook\n"
    "  /crypto              BTC ETH SOL DOGE XRP\n"
    "  /macro               CPI ï¿½ Fed ï¿½ NFP ï¿½ FOMC calendar\n"
    "  /earnings            next 7 days\n"
    "\n"
    "MOVERS\n"
    "  /movers              gainers + losers summary\n"
    "  /movers gainers      top 5 up\n"
    "  /movers losers       top 5 down\n"
    "  /movers volume       most active\n"
    "  /movers lowprice     $1ï¿½$10 rockets\n"
    "\n"
    "STOCK TOOLS\n"
    "  /price TICK          live quote + day range\n"
    "  /analyze TICK        AI deep dive: catalyst ï¿½ risk ï¿½ technicals\n"
    "  /compare TICK TICK   side-by-side AI verdict\n"
    "  /chart TICK          intraday sparkline + VWAP + volume\n"
    "  /rsi TICK            RSI(14) ï¿½ Bollinger Bands ï¿½ squeeze score\n"
    "  /news TICK           latest headlines\n"
    "\n"
    "ALERTS\n"
    "  /spikes              recent spikes (last 30 min)\n"
    "  /alerts              all alerts fired today\n"
    "  /squeeze             top squeeze candidates (0ï¿½100 score)\n"
    "  /setalert TICK $     custom price target\n"
    "  /watchlist           add ï¿½ remove ï¿½ scan your list\n"
    "\n"
    "PAPER TRADING  (simulated ï¿½ $100k ï¿½ bullish only)\n"
    "  /paper               portfolio value + open positions\n"
    "  /paper positions     live P&L on each position\n"
    "  /paper trades        today's buys & sells\n"
    "  /paper history       all-time win rate + summary\n"
    "  /paper signal TICK   7-factor signal breakdown\n"
    "  /paper log           download trade log\n"
    "  /paper reset         reset to $100k\n"
    "  /overnight           overnight gap risk on open positions\n"
    "\n"
    "OFF-HOURS & PREP\n"
    "  /prep                next session game plan (works anytime)\n"
    "  /wlprep              full watchlist technical scan + AI setup read\n"
    "  /ask <question>      chat with Claude AI (multi-turn memory)\n"
    "\n"
    "BOT\n"
    "  /dashboard           send visual dashboard now\n"
    "  /list                all monitored tickers\n"
    "  /monitoring          pause ï¿½ resume ï¿½ status\n"
    "  /help                this menu\n"
    "\n"
    "?? Auto: 8am pre-mkt ï¿½ 8:30 open ï¿½ 12pm mid-day ï¿½ 3pm close ï¿½ 6pm recap\n"
    "         Sat 9am watchlist prep ï¿½ Sun 6pm weekly digest  (all times CT)"
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
                    logger.warning(f"Telegram 429 ï¿½ sleeping {wait}s")
                    time.sleep(wait)
                    continue
                time.sleep(0.3)
                break
            except Exception as e:
                wait = 2 ** attempt
                logger.error(f"Telegram send error (attempt {attempt+1}): {e}. Retry in {wait}s")
                time.sleep(wait)

# ============================================================
# AI HELPERS ï¿½ Claude primary, Grok fallback, exponential backoff
# ============================================================

def _build_system(today_stamp: str) -> str:
    return (
        f"You are a concise stock market analyst assistant. "
        f"Today is {today_stamp}. "
        f"STRICT RULES: "
        f"(1) Only state facts you are confident are true as of this date. "
        f"(2) Do NOT invent specific events, earnings dates, economic reports, "
        f"strikes, executive statements, or price levels ï¿½ if uncertain, omit or say so. "
        f"(3) When live data is provided in the prompt, use it. "
        f"When it is not, give general analysis and clearly flag uncertainty. "
        f"(4) Never reference events, prices, or news from prior years unless asked. "
        f"Be direct and data-driven. No fluff. Max 3 sentences unless asked for more."
    )


def get_ai_response(prompt, system=None, max_tokens=300, fast=False):
    """
    Single-turn AI response.
    fast=True  ? Claude Haiku  (spike alerts, signals, dashboard ï¿½ high frequency)
    fast=False ? Claude Sonnet (analysis, briefings, /ask ï¿½ quality matters)
    Falls back to Grok if Claude is unavailable.
    """
    today_stamp = datetime.now(CT).strftime("%A %B %d, %Y  %I:%M %p CT")
    sys_msg     = system or _build_system(today_stamp)
    model       = CLAUDE_HAIKU if fast else CLAUDE_SONNET

    # -- Claude (primary) --------------------------------------
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
        logger.warning("Claude failed all retries ï¿½ falling back to Grok")

    # -- Grok (fallback) ---------------------------------------
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

    return "AI unavailable ï¿½ set ANTHROPIC_API_KEY in Railway"


def get_ai_conversation(chat_id: str, user_message: str) -> str:
    """
    Multi-turn conversational AI with per-chat memory (last 20 messages).
    Uses Claude Sonnet for quality. Falls back to Grok.
    """
    today_stamp = datetime.now(CT).strftime("%A %B %d %Y %I:%M %p CT")
    system = (
        f"You are a stock market assistant on Telegram. "
        f"Today is {today_stamp}. "
        f"STRICT RULES: "
        f"(1) Only state specific events, dates, or prices you are confident are accurate. "
        f"(2) Do NOT fabricate earnings dates, economic reports, executive statements, "
        f"strikes, mergers, or scheduled events ï¿½ if uncertain, say so explicitly. "
        f"(3) When the user's message contains live price data in [brackets], use it. "
        f"(4) Distinguish clearly between what you know vs. what is uncertain. "
        f"Be concise. Use plain text, no markdown."
    )

    history = conversation_history.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_message})
    if len(history) > 20:
        history = history[-20:]
        conversation_history[chat_id] = history

    # -- Claude (primary) --------------------------------------
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
            logger.warning("Claude rate limit in conversation ï¿½ trying Grok fallback")
        except Exception as e:
            logger.error(f"Claude conversation error: {e} ï¿½ trying Grok fallback")

    # -- Grok (fallback) ---------------------------------------
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

    return "AI unavailable ï¿½ set ANTHROPIC_API_KEY in Railway"

# ============================================================
# MARKET DATA HELPERS
# ============================================================
def fetch_finnhub_quote(ticker):
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_TOKEN}",
            timeout=10
        )
        data = r.json()
        return data.get('c'), data.get('v'), data.get('pc')  # current, volume, prev close
    except:
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
    except:
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
    except:
        return None

def get_ticker_data(ticker: str) -> dict:
    """
    Robust ticker data fetch with 3-tier fallback for fields that
    fast_info commonly returns as 0 (e.g. 52w high/low, avg volume).

    Returns a normalised dict with keys:
      price, chg, mcap, volume, high52, low52, avg_volume
    """
    t = yf.Ticker(ticker)
    d = {}

    # -- Tier 1: fast_info (fastest, least complete) -----------
    try:
        fi = t.fast_info
        d["price"]      = fi.get("lastPrice") or 0
        d["chg"]        = fi.get("regularMarketChangePercent") or 0
        d["mcap"]       = fi.get("marketCap") or 0
        d["volume"]     = fi.get("lastVolume") or 0
        d["high52"]     = fi.get("fiftyTwoWeekHigh") or 0
        d["low52"]      = fi.get("fiftyTwoWeekLow") or 0
        d["avg_volume"] = fi.get("threeMonthAverageVolume") or 0
    except Exception as e:
        logger.debug(f"fast_info failed for {ticker}: {e}")

    # -- Tier 2: full .info dict (slower but complete) ---------
    missing = [k for k in ("high52", "low52", "avg_volume") if not d.get(k)]
    if missing:
        try:
            info = t.info
            if not d.get("price"):
                d["price"]  = info.get("currentPrice") or info.get("regularMarketPrice") or 0
            if not d.get("chg"):
                prev = info.get("regularMarketPreviousClose") or 0
                curr = d["price"]
                d["chg"] = ((curr - prev) / prev * 100) if prev else 0
            if not d.get("mcap"):
                d["mcap"]  = info.get("marketCap") or 0
            if not d.get("volume"):
                d["volume"] = info.get("volume") or 0
            if not d.get("high52"):
                d["high52"] = info.get("fiftyTwoWeekHigh") or 0
            if not d.get("low52"):
                d["low52"]  = info.get("fiftyTwoWeekLow") or 0
            if not d.get("avg_volume"):
                d["avg_volume"] = info.get("averageVolume") or info.get("averageVolume10days") or 0
        except Exception as e:
            logger.debug(f".info failed for {ticker}: {e}")

    # -- Tier 3: compute from 1y history if still missing ------
    if not d.get("high52") or not d.get("low52"):
        try:
            hist = t.history(period="1y")
            if not hist.empty:
                d["high52"] = float(hist["High"].max())
                d["low52"]  = float(hist["Low"].min())
                if not d.get("price"):
                    d["price"] = float(hist["Close"].iloc[-1])
        except Exception as e:
            logger.debug(f"history fallback failed for {ticker}: {e}")

    # -- Ensure all keys exist ---------------------------------
    for k in ("price", "chg", "mcap", "volume", "high52", "low52", "avg_volume"):
        d.setdefault(k, 0)

    return d



# ============================================================
# TECHNICALS ï¿½ RSI (Wilder), Bollinger Bands, Squeeze Score
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
    pct_b = (price - lower) / (upper - lower)  ï¿½ 0=at lower, 1=at upper
    bandwidth = (upper - lower) / middle        ï¿½ squeeze proxy (lower=tighter)
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
    Squeeze score 0ï¿½100 combining:
      RSI distance from 40 (building momentum)  ï¿½ up to 30 pts
      Bollinger bandwidth squeeze (low = tight)  ï¿½ up to 25 pts
      %B near lower band (coiled spring)         ï¿½ up to 20 pts
      Volume trend (rising vs prior scans)       ï¿½ up to 15 pts
      Short interest ratio (Finnhub)             ï¿½ up to 10 pts
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

    try:
        r  = requests.get(
            f"https://finnhub.io/api/v1/stock/metric?symbol={ticker}"
            f"&metric=all&token={FINNHUB_TOKEN}",
            timeout=6
        )
        si     = r.json().get('metric', {}).get('shortRatioAnnual', 0) or 0
        si_pts = min(10, si / 2)
        score += si_pts
        components['short_ratio'] = si
        components['si_pts']      = round(si_pts, 1)
    except:
        pass

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
    except:
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
            info = yf.Ticker(sym).fast_info
            chg  = info.get('regularMarketChangePercent', 0)
            arrow = "?" if chg >= 0 else "?"
            lines.append(f"{arrow} {name}: {chg:+.2f}%")
        except:
            pass
    return lines

def get_crypto_prices():
    coins = ["BTC-USD","ETH-USD","SOL-USD","DOGE-USD","XRP-USD"]
    lines = []
    for coin in coins:
        try:
            info = yf.Ticker(coin).fast_info
            price = info.get('lastPrice', 0)
            chg   = info.get('regularMarketChangePercent', 0)
            name  = coin.replace("-USD","")
            arrow = "?" if chg >= 0 else "?"
            lines.append(f"{arrow} {name}: ${price:,.2f} ({chg:+.2f}%)")
        except:
            pass
    return lines


def fetch_market_snapshot() -> dict:
    """
    Fetch a comprehensive live market snapshot concurrently.
    Returns a dict with all data pre-formatted for both display
    and Claude prompt injection.

    Keys:
      indices_lines  ï¿½ list of formatted index strings
      indices_str    ï¿½ single-line summary for Claude prompts
      sector_lines   ï¿½ list of formatted sector strings
      sector_str     ï¿½ top 3 sectors for Claude prompts
      fg_val         ï¿½ Fear & Greed value (int)
      fg_label       ï¿½ Fear & Greed label (str)
      fg_str         ï¿½ formatted F&G string
      vix            ï¿½ VIX price (float)
      crypto_lines   ï¿½ list of formatted crypto strings
      crypto_str     ï¿½ top 3 crypto for Claude prompts
      movers_str     ï¿½ top gainers/losers from watchlist for Claude
      now_label      ï¿½ formatted date string
      session        ï¿½ trading session string
    """
    def _fetch_indices():
        syms = [("^GSPC","S&P 500"),("^IXIC","Nasdaq"),("^DJI","Dow"),
                ("^RUT","Russell"),("^VIX","VIX")]
        lines, summary_parts, vix = [], [], 0.0
        for sym, name in syms:
            try:
                fi    = yf.Ticker(sym).fast_info
                price = fi.get("lastPrice") or 0
                chg   = fi.get("regularMarketChangePercent") or 0
                arrow = "?" if chg >= 0 else "?"
                if name == "VIX":
                    vix = price
                    lines.append(f"{arrow} VIX: {price:.2f} ({chg:+.2f}%)")
                else:
                    lines.append(f"{arrow} {name}: {price:,.2f} ({chg:+.2f}%)")
                    summary_parts.append(f"{name} {chg:+.2f}%")
            except:
                pass
        return lines, " | ".join(summary_parts), vix

    def _fetch_sectors():
        syms = [("XLK","Tech"),("XLF","Fin"),("XLE","Energy"),
                ("XLV","Health"),("XLI","Indust"),("XLC","Comm"),
                ("XLY","Cons D"),("XLP","Cons S"),("XLB","Mat"),
                ("XLRE","RE"),("XLU","Util")]
        lines = []
        for sym, name in syms:
            try:
                chg   = yf.Ticker(sym).fast_info.get("regularMarketChangePercent") or 0
                arrow = "?" if chg >= 0 else "?"
                lines.append((name, chg, f"{arrow} {name}: {chg:+.2f}%"))
            except:
                pass
        lines.sort(key=lambda x: x[1], reverse=True)
        display = [l for _, _, l in lines]
        top3    = ", ".join(f"{n} {c:+.2f}%" for n, c, _ in lines[:3])
        bot3    = ", ".join(f"{n} {c:+.2f}%" for n, c, _ in lines[-3:])
        return display, f"Top: {top3} | Weak: {bot3}"

    def _fetch_crypto():
        coins = [("BTC-USD","BTC"),("ETH-USD","ETH"),("SOL-USD","SOL")]
        lines = []
        for sym, name in coins:
            try:
                fi    = yf.Ticker(sym).fast_info
                price = fi.get("lastPrice") or 0
                chg   = fi.get("regularMarketChangePercent") or 0
                arrow = "?" if chg >= 0 else "?"
                lines.append(f"{arrow} {name}: ${price:,.0f} ({chg:+.2f}%)")
            except:
                pass
        return lines

    def _fetch_movers():
        items = []
        for t in list(TICKERS)[:30]:
            try:
                fi    = yf.Ticker(t).fast_info
                chg   = fi.get("regularMarketChangePercent") or 0
                price = fi.get("lastPrice") or 0
                if price > 0:
                    items.append((t, chg, price))
            except:
                pass
        items.sort(key=lambda x: x[1])
        gainers = items[-3:][::-1]
        losers  = items[:3]
        g_str = " ".join(f"{t} {c:+.2f}%" for t, c, _ in gainers)
        l_str = " ".join(f"{t} {c:+.2f}%" for t, c, _ in losers)
        return f"Gainers: {g_str} | Losers: {l_str}"

    with ThreadPoolExecutor(max_workers=5) as pool:
        f_idx = pool.submit(_fetch_indices)
        f_sec = pool.submit(_fetch_sectors)
        f_cry = pool.submit(_fetch_crypto)
        f_mov = pool.submit(_fetch_movers)
        f_fg  = pool.submit(get_fear_greed)

    idx_lines, idx_str, vix = f_idx.result()
    sec_lines, sec_str      = f_sec.result()
    cry_lines               = f_cry.result()
    movers_str              = f_mov.result()
    fg_val, fg_label        = f_fg.result()
    fg_val   = int(fg_val) if fg_val else 50
    fg_label = fg_label or "Unknown"

    return {
        "indices_lines": idx_lines,
        "indices_str":   idx_str,
        "sector_lines":  sec_lines,
        "sector_str":    sec_str,
        "fg_val":        fg_val,
        "fg_label":      fg_label,
        "fg_str":        f"{fg_val} ï¿½ {fg_label}",
        "vix":           vix,
        "crypto_lines":  cry_lines,
        "crypto_str":    " | ".join(cry_lines),
        "movers_str":    movers_str,
        "now_label":     datetime.now(CT).strftime("%A %B %d, %Y"),
        "session":       get_trading_session(),
    }



# ============================================================
# DYNAMIC BULLISH LIST
# ============================================================
def get_dynamic_hot_stocks():
    logger.info("Fetching dynamic BULLISH candidates...")
    candidates, low_price = [], []
    try:
        r = requests.get(
            f"https://financialmodelingprep.com/api/v3/stock_market/actives?apikey={FMP_API_KEY}",
            timeout=10
        )
        data = r.json()
        if isinstance(data, list):
            candidates.extend([item.get('symbol') for item in data[:30] if isinstance(item, dict)])

        r = requests.get(
            f"https://financialmodelingprep.com/api/v3/stock_market/gainers?apikey={FMP_API_KEY}",
            timeout=10
        )
        data = r.json()
        if isinstance(data, list):
            candidates.extend([item.get('symbol') for item in data[:20] if isinstance(item, dict)])

        qqq_chg = yf.Ticker("^QQQ").fast_info.get('regularMarketChangePercent', 0)
        spy_chg = yf.Ticker("^GSPC").fast_info.get('regularMarketChangePercent', 0)
        index_up = qqq_chg > 0 or spy_chg > 0

        bullish = []
        for symbol in list(dict.fromkeys(candidates))[:50]:
            try:
                info        = yf.Ticker(symbol).fast_info
                mcap        = info.get('marketCap', 0)
                if mcap < 100_000_000_000:
                    continue
                stock_chg   = info.get('regularMarketChangePercent', 0)
                if stock_chg <= 0 or not index_up:
                    continue
                rel_strength = stock_chg / max(qqq_chg, spy_chg, 0.1)
                if rel_strength > 1.0:
                    bullish.append(symbol)
            except:
                continue

        low_price = [s for s in bullish
                     if 1 <= yf.Ticker(s).fast_info.get('lastPrice', 0) <= 10][:10]

    except Exception as e:
        logger.warning(f"FMP filter failed: {e}. Using core list.")

    combined = list(dict.fromkeys(CORE_TICKERS + bullish + low_price))[:60]
    logger.info(f"Watchlist updated ? {len(combined)} stocks ({len(low_price)} low-price rockets)")
    return combined

TICKERS = get_dynamic_hot_stocks()

# ============================================================
# ALERT ENGINE
# ============================================================
def send_alert(ticker, pct_change, current_price, volume_spike=False):
    global daily_alerts
    daily_alerts += 1
    news_items   = fetch_latest_news(ticker)
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
    news_text = "\n".join([f"ï¿½ {h[:80]}" for h, _ in news_items]) if news_items else "No news"

    message = (
        f"?? {ticker} {spike_label}\n"
        f"{pct_change:+.1f}% | ${current_price:.2f}"
        + (" | ?? Vol Spike" if volume_spike else "") + "\n"
        + (f"{tech_str}\n" if tech_str else "")
        + f"\nClaude: {ai}\n\n"
        f"News:\n{news_text}"
    )
    send_telegram(message)
    recent_alerts.append(f"{ticker} {pct_change:+.1f}% at {datetime.now(CT).strftime('%H:%M')}")

def check_custom_price_alerts(ticker, current_price):
    if ticker not in custom_price_alerts:
        return
    triggered = []
    for target in custom_price_alerts[ticker]:
        if abs(current_price - target) / target < 0.005:   # within 0.5%
            send_telegram(
                f"?? Price Alert Hit!\n{ticker} reached ${current_price:.2f}\n(Target: ${target:.2f})"
            )
            triggered.append(target)
    for t in triggered:
        custom_price_alerts[ticker].remove(t)

def _scan_ticker(ticker: str, now: datetime):
    """Scan a single ticker ï¿½ runs in thread pool."""
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
                        hist     = yf.Ticker(ticker).history(period="5d")
                        if not hist.empty:
                            avg_vol   = hist['Volume'].mean()
                            vol_spike = vol > avg_vol * VOLUME_SPIKE_MULT
                    except:
                        pass
                send_alert(ticker, change * 100, c, vol_spike)
                last_alert_time[ticker] = now

    last_prices[ticker] = c


def check_stocks():
    if monitoring_paused or get_trading_session() == "closed":
        return
    now = datetime.now(CT)
    logger.info(f"Scanning {len(TICKERS)} stocks (concurrent)...")

    with ThreadPoolExecutor(max_workers=min(32, len(TICKERS))) as pool:
        futures = {pool.submit(_scan_ticker, t, now): t for t in TICKERS}
        for future in as_completed(futures):
            t = futures[future]
            try:
                future.result()
            except Exception as e:
                logger.error(f"Scan error for {t}: {e}")

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
#   1. RSI Momentum      ï¿½ 50ï¿½65 trending zone = 20 pts
#   2. BB Breakout/Bounce ï¿½ price above mid or %B recovering = 15 pts
#   3. MACD Crossover    ï¿½ fast/slow EMA divergence direction = 15 pts
#   4. Volume Confirm    ï¿½ current vol vs 5-day avg = 15 pts
#   5. Squeeze Momentum  ï¿½ existing score (reused, scaled) = 10 pts
#   6. Price Slope       ï¿½ 5-min linear regression slope = 10 pts
#   7. Grok AI Signal    ï¿½ directional confidence 0-100, scaled = 15 pts
#
# Trade rules (bullish only, no shorts):
#   ï¿½ BUY  when composite = PAPER_MIN_SIGNAL and RSI < 72 and cash available
#   ï¿½ SELL on 8% take-profit, 4% stop-loss, or signal collapse (=30 + positive)
#   ï¿½ Max PAPER_MAX_ACTIONS actions per ticker per day
#   ï¿½ Max PAPER_MAX_POSITIONS open positions at once
#   ï¿½ Max PAPER_MAX_POS_PCT of total portfolio in a single name
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

# -- Live state (populated by load_paper_state on startup) -----
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
            logger.info(f"Paper state saved ? {PAPER_STATE_FILE}")
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
                f"State restored (new day ï¿½ intraday counters reset). "
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


# -- Dedicated investment logger -------------------------------
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
        except:
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
    Composite signal engine. Returns score 0-100 plus component breakdown.
    Caches for 60 seconds to avoid hammering Grok.
    """
    now = datetime.now(CT)

    # Return cached signal if fresh
    cached = paper_signals_cache.get(ticker)
    if cached and (now - cached["ts"]).total_seconds() < 60:
        return cached

    hist_raw = list(price_history.get(ticker, deque()))
    prices   = [p for _, p in hist_raw] if hist_raw and isinstance(hist_raw[0], tuple) else hist_raw

    score  = 0
    comps  = {}
    detail = []

    # -- 1. RSI Momentum (20 pts) ------------------------------
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

    # -- 2. Bollinger Band Position (15 pts) -------------------
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

    # -- 3. MACD Crossover (15 pts) ----------------------------
    macd_line, sig_line, hist_val = _compute_macd(prices)
    if macd_line is not None:
        if macd_line > 0 and (sig_line is None or macd_line > sig_line):
            pts = 15   # bullish: MACD above zero and above signal
        elif macd_line > 0:
            pts = 8    # above zero but below signal ï¿½ weakening
        elif hist_val is not None and hist_val > 0:
            pts = 5    # histogram turning positive ï¿½ early signal
        else:
            pts = 0
        score += pts
        comps["macd"] = macd_line
        comps["macd_pts"] = pts
        detail.append(f"MACD={macd_line:.4f}({pts}pts)")

    # -- 4. Volume Confirmation (15 pts) -----------------------
    try:
        _, vol, _ = fetch_finnhub_quote(ticker)
        hist_yf   = yf.Ticker(ticker).history(period="5d")
        if vol and not hist_yf.empty:
            avg_vol = hist_yf["Volume"].mean()
            ratio   = vol / avg_vol if avg_vol > 0 else 1
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
    except:
        pass

    # -- 5. Squeeze Score (10 pts, scaled from existing) -------
    sq = compute_squeeze_score(ticker)
    sq_pts = round(sq["score"] / 10, 1)   # 0-100 ? 0-10 pts
    score += sq_pts
    comps["squeeze"] = sq["score"]
    comps["sq_pts"]  = sq_pts
    detail.append(f"Squeeze={sq['score']:.0f}({sq_pts}pts)")

    # -- 6. Price Slope / Linear Momentum (10 pts) -------------
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

    # -- 7. Grok AI Directional Signal (15 pts) ----------------
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
            except:
                ai_score = 70
            pts = int(15 * ai_score / 100)
        elif "AVOID" in raw_ai.upper():
            ai_signal = "AVOID"
            pts = 0
        else:
            pts = 5
        try:
            ai_reason = raw_ai.split("REASON:")[-1].strip()[:60] if "REASON:" in raw_ai else raw_ai[:60]
        except:
            pass
        score += pts
        comps["grok_signal"]     = ai_signal
        comps["grok_confidence"] = ai_score
        comps["grok_reason"]     = ai_reason
        comps["grok_pts"]        = pts
        detail.append(f"Claude={ai_signal}@{ai_score}({pts}pts)")
    except Exception as e:
        logger.debug(f"Grok signal error for {ticker}: {e}")

    result = {
        "score":   round(min(score, 100), 1),
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

    # Scale position size with signal confidence (65?100 maps to 50%?100% of max)
    strength   = min(1.0, (signal_score - PAPER_MIN_SIGNAL) / (100 - PAPER_MIN_SIGNAL))
    dollars    = max_dollars * (0.5 + 0.5 * strength)
    dollars    = min(dollars, paper_cash * 0.95)   # never use more than 95% of cash

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

    # -- Check existing position: take-profit / stop-loss / signal exit --
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

            msg = (
                f"SELL | {ticker} | {shares} shares @ ${price:.2f} | "
                f"P&L: ${realized_pnl:+.2f} ({pnl_pct*100:+.1f}%) | "
                f"Reason: {sell_reason} | "
                f"Portfolio: ${paper_portfolio_value():,.0f}"
            )
            paper_log(msg)

            # -- Enriched SELL notification ---------------------
            hold_mins = ""
            try:
                entry_dt = datetime.strptime(
                    f"{pos.get('entry_date', today)} {pos.get('entry_time', '00:00:00')}",
                    "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=CT)
                held = int((now - entry_dt).total_seconds() / 60)
                hold_mins = f"{held}m" if held < 60 else f"{held//60}h {held%60}m"
            except:
                pass

            pnl_emoji = "??" if realized_pnl >= 0 else "??"
            reason_map = {
                "TAKE-PROFIT": "? Take-profit hit",
                "STOP-LOSS":   "?? Stop-loss triggered",
                "SIGNAL-COLLAPSE": "?? Signal deteriorated",
            }
            reason_label = next(
                (v for k, v in reason_map.items() if k in sell_reason), sell_reason
            )

            new_val     = paper_portfolio_value()
            lifetime_pct = (new_val - PAPER_STARTING_CAPITAL) / PAPER_STARTING_CAPITAL * 100

            send_telegram(
                f"{pnl_emoji} PAPER SELL ï¿½ {ticker}\n"
                f"{'-'*28}\n"
                f"Shares:    {shares} @ ${price:.2f}\n"
                f"Entry:     ${pos['avg_cost']:.2f}"
                + (f"  (held {hold_mins})" if hold_mins else "") + "\n"
                f"P&L:       ${realized_pnl:+.2f}  ({pnl_pct*100:+.1f}%)\n"
                f"Reason:    {reason_label}\n"
                f"{'-'*28}\n"
                f"Cash:      ${paper_cash:,.0f}\n"
                f"Positions: {len(paper_positions)}/{PAPER_MAX_POSITIONS}\n"
                f"Portfolio: ${new_val:,.0f}  ({lifetime_pct:+.2f}% all-time)\n"
                f"Trades today: {len(paper_trades_today)}"
            )
            save_paper_state()
        return  # one action per scan cycle per ticker

    # -- Check for new buy opportunity ------------------------
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

    msg = (
        f"BUY | {ticker} | {shares} shares @ ${price:.2f} | "
        f"Cost: ${cost:,.2f} | Signal: {sig['score']:.0f}/100 | "
        f"Detail: {sig['detail']} | "
        f"Portfolio: ${paper_portfolio_value():,.0f}"
    )
    paper_log(msg)

    # -- Enriched BUY notification ------------------------------
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

    send_telegram(
        f"?? PAPER BUY ï¿½ {ticker}\n"
        f"{'-'*28}\n"
        f"Shares:    {shares} @ ${price:.2f}\n"
        f"Cost:      ${cost:,.0f}\n"
        f"Target:    ${tp_price:.2f} (+{PAPER_TAKE_PROFIT_PCT*100:.0f}%)\n"
        f"Stop:      ${sl_price:.2f} (-{PAPER_STOP_LOSS_PCT*100:.0f}%)\n"
        f"{'-'*28}\n"
        f"Signal:    {sig['score']:.0f}/100\n"
        + "\n".join(f"  ï¿½ {l}" for l in sig_lines) + "\n"
        + (f"  ï¿½ {c.get('grok_reason','')}\n" if c.get("grok_reason") else "")
        + f"{'-'*28}\n"
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
        f"PAPER PORTFOLIO ï¿½ Market Open",
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
            arrow   = "?" if pnl >= 0 else "?"
            lines.append(
                f"  {arrow} {ticker:<6} {pos['shares']:>5} sh  "
                f"avg ${pos['avg_cost']:.2f} ? ${price:.2f}  "
                f"({pnl:+.1f}%)  ${mkt_val:,.0f}"
            )
    else:
        lines.append("No open positions ï¿½ scanning for entries.")

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
        f"PAPER PORTFOLIO ï¿½ Market Close",
        f"{datetime.now(CT).strftime('%A %B %d, %Y')}",
        f"",
        f"Total Value:     ${val:>12,.2f}",
        f"All-Time P&L:    ${total_pnl:>+12,.2f} ({total_pct:+.2f}%)",
        f"Today Realized:  ${day_realized:>+12,.2f}",
        f"Cash:            ${paper_cash:>12,.2f}",
        f"",
        f"TODAY'S TRADES ({len(paper_trades_today)} total  "
        f"?{len(buys)} buys  ?{len(sells)} sells):",
    ]

    for t in paper_trades_today:
        if t["action"] == "BUY":
            lines.append(
                f"  ? {t['time']}  BUY  {t['ticker']:<6} "
                f"{t['shares']} sh @ ${t['price']:.2f}  "
                f"(${t['cost']:,.0f})  sig={t.get('signal_score','?'):.0f}"
            )
        else:
            pnl_str = f"${t['pnl']:+.2f} ({t['pnl_pct']:+.1f}%)"
            lines.append(
                f"  ? {t['time']}  SELL {t['ticker']:<6} "
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
            arrow   = "?" if pnl >= 0 else "?"
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


# -- Paper Trading Telegram Commands ---------------------------

async def cmd_paper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /paper                  ï¿½ current portfolio snapshot
    /paper positions        ï¿½ open positions with live P&L
    /paper trades           ï¿½ today's trade log
    /paper history          ï¿½ all-time trade summary
    /paper signal TICK      ï¿½ show current signal breakdown for a ticker
    /paper log              ï¿½ send investment.log as a file download
    /paper reset            ï¿½ reset portfolio to $100k (with confirmation)
    """
    global paper_cash, paper_positions, paper_trades_today, paper_daily_counts
    global paper_all_trades, paper_signals_cache

    sub  = context.args[0].lower() if context.args else "portfolio"
    arg2 = context.args[1].upper() if len(context.args) > 1 else ""

    # -- /paper  or  /paper portfolio -------------------------
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
                arrow = "?" if pnl >= 0 else "?"
                lines.append(
                    f"  {arrow} {ticker:<6} {pos['shares']} sh  "
                    f"${pos['avg_cost']:.2f}?${price:.2f}  "
                    f"{pnl:+.1f}%  ${mkt:,.0f}"
                )
        else:
            lines.append("No open positions.")

        lines += [
            f"",
            f"Trades today: {len(paper_trades_today)}  |  "
            f"Lifetime: {len(paper_all_trades)}",
            f"Use /paper trades ï¿½ /paper signal TICK ï¿½ /paper log",
        ]
        await update.message.reply_text("\n".join(lines))

    # -- /paper positions -------------------------------------
    elif sub == "positions":
        if not paper_positions:
            await update.message.reply_text("No open positions.")
            return
        lines = [f"OPEN POSITIONS ï¿½ {datetime.now(CT).strftime('%H:%M CT')}"]
        for ticker, pos in paper_positions.items():
            price, _, _ = fetch_finnhub_quote(ticker)
            price = price or pos["avg_cost"]
            mkt   = pos["shares"] * price
            pnl   = (price - pos["avg_cost"]) / pos["avg_cost"] * 100
            unrealized = (price - pos["avg_cost"]) * pos["shares"]
            arrow = "?" if pnl >= 0 else "?"
            lines += [
                f"",
                f"{arrow} {ticker}",
                f"  Shares: {pos['shares']}  Entry: ${pos['avg_cost']:.2f}  Now: ${price:.2f}",
                f"  Unrealized: ${unrealized:+.2f} ({pnl:+.1f}%)",
                f"  Market value: ${mkt:,.2f}",
                f"  Entry: {pos['entry_date']} {pos['entry_time']}",
            ]
        await update.message.reply_text("\n".join(lines))

    # -- /paper trades -----------------------------------------
    elif sub == "trades":
        if not paper_trades_today:
            await update.message.reply_text("No trades today.")
            return
        buys   = [t for t in paper_trades_today if t["action"] == "BUY"]
        sells  = [t for t in paper_trades_today if t["action"] == "SELL"]
        real   = sum(t.get("pnl", 0) for t in sells)
        lines  = [
            f"TODAY'S TRADES",
            f"?{len(buys)} buys  ?{len(sells)} sells  "
            f"Realized: ${real:+.2f}",
            f"",
        ]
        for t in paper_trades_today:
            if t["action"] == "BUY":
                lines.append(
                    f"? {t['time']}  BUY  {t['ticker']} "
                    f"{t['shares']}sh @ ${t['price']:.2f}  "
                    f"sig={t.get('signal_score','?'):.0f}/100"
                )
            else:
                lines.append(
                    f"? {t['time']}  SELL {t['ticker']} "
                    f"{t['shares']}sh @ ${t['price']:.2f}  "
                    f"${t['pnl']:+.2f} ({t['pnl_pct']:+.1f}%)  "
                    f"[{t['reason']}]"
                )
        await update.message.reply_text("\n".join(lines))

    # -- /paper history ----------------------------------------
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
            f"PAPER TRADING ï¿½ ALL-TIME SUMMARY",
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

    # -- /paper signal TICK ------------------------------------
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
            f"Composite Score: {sig['score']:.0f}/100  ? {verdict}",
            f"",
            f"BREAKDOWN:",
            f"  RSI Momentum    {c.get('rsi','N/A')} ? {c.get('rsi_pts',0)} pts",
            f"  BB Position     %B={c.get('pct_b','N/A')} ? {c.get('bw_pts',0)} pts",
            f"  MACD            {c.get('macd','N/A')} ? {c.get('macd_pts',0)} pts",
            f"  Volume Ratio    {c.get('vol_ratio','N/A')}x ? {c.get('vol_pts',0)} pts",
            f"  Squeeze Score   {c.get('squeeze','N/A')}/100 ? {c.get('sq_pts',0)} pts",
            f"  Price Slope     {c.get('slope_pct','N/A')}%/tick ? {c.get('slope_pts',0)} pts",
            f"  Grok AI         {c.get('grok_signal','N/A')} "
            f"conf={c.get('grok_confidence','?')} ? {c.get('grok_pts',0)} pts",
            f"",
            f"Grok: {c.get('grok_reason', '')}",
            f"",
            f"Action threshold: {PAPER_MIN_SIGNAL}/100  "
            f"Daily actions today: "
            + str(paper_daily_counts.get(f'{arg2}:{datetime.now(CT).strftime("%Y-%m-%d")}', 0))
            + f"/{PAPER_MAX_ACTIONS}",
        ]
        await update.message.reply_text("\n".join(lines))

    # -- /paper log --------------------------------------------
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
                        caption=f"{fname} ï¿½ Portfolio: ${paper_portfolio_value():,.0f}"
                    )
                sent = True
        if not sent:
            await update.message.reply_text("No log files found yet ï¿½ no trades recorded.")

    # -- /paper reset ------------------------------------------
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
            "  /paper               ï¿½ portfolio snapshot\n"
            "  /paper positions     ï¿½ open positions + live P&L\n"
            "  /paper trades        ï¿½ today's actions\n"
            "  /paper history       ï¿½ all-time performance\n"
            "  /paper signal TICK   ï¿½ signal breakdown for any stock\n"
            "  /paper log           ï¿½ download investment.log\n"
            "  /paper reset         ï¿½ reset to $100,000"
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
    indices = {
        "^GSPC": "S&P 500", "^IXIC": "Nasdaq",
        "^DJI":  "Dow",     "^RUT":  "Russell 2000",
        "^VIX":  "VIX"
    }
    idx_lines = []
    for sym, name in indices.items():
        try:
            info  = yf.Ticker(sym).fast_info
            price = info.get('lastPrice', 0)
            chg   = info.get('regularMarketChangePercent', 0)
            arrow = "?" if chg >= 0 else "?"
            idx_lines.append(f"{arrow} {name}: {price:,.2f} ({chg:+.2f}%)")
        except:
            pass

    sec_lines = get_sector_performance()
    fg_val, fg_label = get_fear_greed()
    fg_str = f"{fg_val} ï¿½ {fg_label}" if fg_val else "unavailable"

    session = get_trading_session()
    summary = " | ".join(idx_lines[:4]) + f" | F&G {fg_val}"
    ai = get_ai_response(
        f"Market snapshot ({session}): {summary}. Top sectors: {', '.join(sec_lines[:3])}. "
        f"{'This is last-close data. ' if note else ''}"
        f"2-sentence outlook + one sector to watch."
    )
    header = f"Market Overview{f'  {note}' if note else ''}"
    await update.message.reply_text(
        f"{header}\n"
        "Indices:\n" + "\n".join(idx_lines) +
        f"\n\nFear & Greed: {fg_str}" +
        "\n\nSectors:\n" + "\n".join(sec_lines) +
        f"\n\nClaude: {ai}"
    )

async def cmd_spikes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not recent_alerts:
        await update.message.reply_text("No spikes in the last 30 minutes.")
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
            f"{'Market is closed ï¿½ focus on setup for next session. ' if note else ''}"
            f"Provide: (1) technical assessment (2) near-term catalyst (3) key risk. Be specific."
        )
        ai = get_ai_response(prompt, max_tokens=500)

        range_display = (f"${low52:.2f} ï¿½ ${high52:.2f}" if high52 and low52 else "n/a")
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
        # day high/low still best from fast_info
        fi      = yf.Ticker(ticker).fast_info
        day_hi  = fi.get("dayHigh") or 0
        day_lo  = fi.get("dayLow")  or 0
        chg_abs = price * chg / 100
        arrow   = "?" if chg >= 0 else "?"

        day_range = f"${day_lo:.2f} ï¿½ ${day_hi:.2f}" if day_hi and day_lo else "n/a"
        yr_range  = f"${low52:.2f} ï¿½ ${high52:.2f}"   if high52 and low52  else "n/a"

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
            f"52w range ${stats[t1]['low52']:.2f}ï¿½${stats[t1]['high52']:.2f} vs "
            f"{t2} ${stats[t2]['price']:.2f} ({stats[t2]['chg']:+.2f}%) "
            f"52w range ${stats[t2]['low52']:.2f}ï¿½${stats[t2]['high52']:.2f}. "
            f"Which is the better buy right now and why? Be specific."
        )
        ai = get_ai_response(summary)
        await update.message.reply_text("\n".join(lines) + f"\n\nClaude: {ai}")
    except Exception as e:
        await update.message.reply_text(f"Compare failed: {e}")

async def cmd_movers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /movers [gainers|losers|volume|lowprice]
    Defaults to showing a compact summary of all four if no arg.
    """
    mode = context.args[0].lower() if context.args else "all"

    async def _gainers():
        try:
            df = pd.read_html("https://finance.yahoo.com/screener/predefined/day_gainers")[0]
            return "Top Gainers:\n" + "\n".join(
                f"ï¿½ {r['Symbol']} +{r['% Change']:.1f}%"
                for _, r in df.head(5).iterrows()
            )
        except:
            return "Gainers unavailable."

    async def _losers():
        try:
            df = pd.read_html("https://finance.yahoo.com/screener/predefined/day_losers")[0]
            return "Top Losers:\n" + "\n".join(
                f"ï¿½ {r['Symbol']} {r['% Change']:.1f}%"
                for _, r in df.head(5).iterrows()
            )
        except:
            return "Losers unavailable."

    async def _volume():
        try:
            df = pd.read_html("https://finance.yahoo.com/screener/predefined/most_actives")[0]
            return "Most Active:\n" + "\n".join(
                f"ï¿½ {r['Symbol']}" for _, r in df.head(8).iterrows()
            )
        except:
            return "Volume data unavailable."

    async def _lowprice():
        try:
            df  = pd.read_html("https://finance.yahoo.com/screener/predefined/day_gainers")[0]
            col = "Price (Intraday)"
            low = df[(df.get(col, 0).astype(float, errors='ignore') >= 1) &
                     (df.get(col, 0).astype(float, errors='ignore') <= 10)]
            return "Low-Price Rockets ($1-$10):\n" + "\n".join(
                f"ï¿½ {r['Symbol']} +{r['% Change']:.1f}%"
                for _, r in low.head(8).iterrows()
            )
        except:
            return "Low-price data unavailable."

    if mode == "gainers":
        await update.message.reply_text(await _gainers())
    elif mode == "losers":
        await update.message.reply_text(await _losers())
    elif mode == "volume":
        await update.message.reply_text(await _volume())
    elif mode == "lowprice":
        await update.message.reply_text(await _lowprice())
    else:
        # compact all-four summary
        g = await _gainers()
        l = await _losers()
        await update.message.reply_text(
            g + "\n\n" + l +
            "\n\nFor more: /movers volume  or  /movers lowprice"
        )



async def cmd_earnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        today = datetime.now().date()
        end   = today + timedelta(days=7)
        r = requests.get(
            f"https://financialmodelingprep.com/api/v3/earning_calendar"
            f"?from={today}&to={end}&apikey={FMP_API_KEY}",
            timeout=10
        )
        data = r.json()
        if not isinstance(data, list) or not data:
            await update.message.reply_text("No upcoming earnings found.")
            return
        lines = ["Upcoming Earnings (7 days):"]
        for item in data[:15]:
            sym  = item.get('symbol','')
            date = item.get('date','')
            eps  = item.get('epsEstimated','?')
            lines.append(f"ï¿½ {sym} on {date} (EPS est: {eps})")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Unable to fetch earnings: {e}")

async def cmd_macro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Upcoming macro events ï¿½ sources in priority order:
      1. Finnhub economic calendar (free tier, live dates)
      2. FMP economic calendar
      3. Claude AI fallback with today's date explicitly injected
    """
    today     = datetime.now(CT).date()
    end       = today + timedelta(days=14)
    today_str = today.strftime("%Y-%m-%d")
    end_str   = end.strftime("%Y-%m-%d")
    now_label = datetime.now(CT).strftime("%A %B %d, %Y")

    HIGH_IMPACT_KEYWORDS = [
        "CPI", "PPI", "GDP", "NFP", "Nonfarm", "FOMC", "Federal Reserve",
        "Unemployment", "Jobless", "Retail Sales", "PCE", "ISM", "PMI",
        "Housing", "Consumer Confidence", "Durable Goods", "Trade Balance",
        "Interest Rate", "Inflation", "Payroll"
    ]

    def _is_relevant(name: str, impact: str) -> bool:
        if impact in ("High", "Medium", "3", "2"):
            return True
        name_up = name.upper()
        return any(k.upper() in name_up for k in HIGH_IMPACT_KEYWORDS)

    def _parse_finnhub(data) -> list:
        events = []
        if not isinstance(data, list):
            return events
        for item in data:
            name   = item.get("event", "") or ""
            impact = item.get("impact", "") or ""
            date   = (item.get("time", "") or "")[:10]
            if not date or date < today_str:        # skip past events
                continue
            if _is_relevant(name, impact):
                events.append({
                    "date":   date,
                    "event":  name,
                    "impact": impact,
                    "est":    str(item.get("estimate", "") or ""),
                    "prev":   str(item.get("prev", "") or ""),
                    "actual": str(item.get("actual", "") or ""),
                })
        return sorted(events, key=lambda x: x["date"])[:15]

    def _parse_fmp(data) -> list:
        events = []
        if not isinstance(data, list):
            return events
        for item in data:
            name   = item.get("event", "") or ""
            impact = item.get("impact", "") or ""
            date   = (item.get("date", "") or "")[:10]
            if not date or date < today_str:
                continue
            if _is_relevant(name, impact):
                events.append({
                    "date":   date,
                    "event":  name,
                    "impact": impact,
                    "est":    str(item.get("estimate", "") or ""),
                    "prev":   str(item.get("previous", "") or ""),
                    "actual": str(item.get("actual", "") or ""),
                })
        return sorted(events, key=lambda x: x["date"])[:15]

    def _format_events(events: list, source: str) -> str:
        lines = [f"Macro Calendar ï¿½ next 14 days (from {now_label})",
                 f"Source: {source}", ""]
        for e in events:
            tag = "[HIGH]" if e["impact"] in ("High", "3") else "[MED] "
            parts = [f"{tag} {e['date']}  {e['event']}"]
            if e["est"]:
                parts.append(f"est={e['est']}")
            if e["prev"]:
                parts.append(f"prev={e['prev']}")
            if e["actual"]:
                parts.append(f"actual={e['actual']}")
            lines.append("  ".join(parts))
        return "\n".join(lines)

    events = []
    source = ""

    # -- 1. Finnhub (free tier supports economic calendar) -----
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/calendar/economic"
            f"?from={today_str}&to={end_str}&token={FINNHUB_TOKEN}",
            timeout=10
        )
        raw = r.json()
        # Finnhub wraps results in {"economicCalendar": [...]}
        if isinstance(raw, dict):
            raw = raw.get("economicCalendar", raw)
        events = _parse_finnhub(raw)
        if events:
            source = "Finnhub"
            logger.info(f"Macro: {len(events)} events from Finnhub")
    except Exception as e:
        logger.warning(f"Finnhub macro failed: {e}")

    # -- 2. FMP fallback ---------------------------------------
    if not events and FMP_API_KEY:
        try:
            r = requests.get(
                f"https://financialmodelingprep.com/api/v3/economic_calendar"
                f"?from={today_str}&to={end_str}&apikey={FMP_API_KEY}",
                timeout=10
            )
            events = _parse_fmp(r.json())
            if events:
                source = "FMP"
                logger.info(f"Macro: {len(events)} events from FMP")
        except Exception as e:
            logger.warning(f"FMP macro failed: {e}")

    # -- 3. Grok fallback ï¿½ date anchored ---------------------
    if not events:
        logger.warning("Macro: both APIs failed, using date-anchored Grok fallback")
        ai = get_ai_response(
            f"Today is {now_label}. "
            f"List the actual scheduled US macro events for the next 14 days "
            f"(from {today_str} to {end_str}), including real scheduled dates. "
            f"Include: CPI, PPI, FOMC meetings, NFP, PCE, Retail Sales, GDP if applicable. "
            f"Format each line as: DATE  EVENT  (est: X). "
            f"Only include events actually scheduled in this window. "
            f"Do not reference any events from 2024.",
            max_tokens=500
        )
        await update.message.reply_text(
            f"Macro Calendar ï¿½ {now_label}\n"
            f"(Live calendar unavailable ï¿½ Grok estimate)\n\n"
            f"{ai}"
        )
        return

    # -- Format + Grok commentary ------------------------------
    body       = _format_events(events, source)
    event_names = ", ".join([e["event"] for e in events[:5]])
    ai = get_ai_response(
        f"Today is {now_label}. "
        f"Upcoming macro events this week: {event_names}. "
        f"Which is most market-moving and why? One sentence, current context only."
    )
    await update.message.reply_text(body + f"\n\nClaude: {ai}")



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
        lines.append(f"ï¿½ {headline[:100]}")
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
        await update.message.reply_text(f"Added {ticker} to your watchlist.")
    elif cmd == "remove" and len(context.args) > 1:
        ticker = context.args[1].upper()
        wl = user_watchlists.get(cid, [])
        if ticker in wl:
            wl.remove(ticker)
        await update.message.reply_text(f"Removed {ticker} from watchlist.")
    elif cmd == "scan":
        wl = user_watchlists.get(cid, [])
        if not wl:
            await update.message.reply_text("Your watchlist is empty.")
            return
        lines = [f"Watchlist snapshot:"]
        for t in wl:
            try:
                info  = yf.Ticker(t).fast_info
                price = info.get('lastPrice', 0)
                chg   = info.get('regularMarketChangePercent', 0)
                arrow = "?" if chg >= 0 else "?"
                lines.append(f"{arrow} {t}: ${price:.2f} ({chg:+.2f}%)")
            except:
                lines.append(f"? {t}: unavailable")
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
            "You'll be notified when the stock is within 0.5% of your target."
        )
        return
    ticker = context.args[0].upper()
    try:
        target = float(context.args[1])
    except:
        await update.message.reply_text("Invalid price. Example: /setalert NVDA 150.00")
        return
    custom_price_alerts.setdefault(ticker, []).append(target)
    await update.message.reply_text(
        f"Price alert set!\n{ticker} @ ${target:.2f}\nYou'll be alerted when within 0.5% of this target."
    )

async def cmd_squeeze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show top squeeze candidates ranked by squeeze score."""
    if not squeeze_scores:
        await update.message.reply_text(
            "No squeeze data yet ï¿½ scores build up after a few scan cycles.\n"
            "Try again in 2ï¿½3 minutes."
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
        bar      = "ï¿½" * int(score / 10) + "ï¿½" * (10 - int(score / 10))
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

    # Fetch fresh 1-day 5-min candles from yfinance for accurate indicators
    await update.message.reply_text(f"Calculating technicals for {ticker}...")
    try:
        hist   = yf.Ticker(ticker).history(period="5d", interval="5m")
        if hist.empty:
            await update.message.reply_text(f"No price history available for {ticker}.")
            return
        prices = hist['Close'].tolist()
        price  = prices[-1]

        rsi             = compute_rsi(prices)
        mid, upper, lower, pct_b, bw = compute_bollinger(prices)

        rsi_label = (
            "Overbought" if rsi and rsi > 70 else
            "Oversold"   if rsi and rsi < 30 else
            "Neutral"
        )
        bb_label = (
            "Above upper band ï¿½ extended" if pct_b and pct_b > 1 else
            "Below lower band ï¿½ oversold" if pct_b and pct_b < 0 else
            "Upper half ï¿½ bullish"        if pct_b and pct_b > 0.5 else
            "Lower half ï¿½ building"
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
            f"RSI (14): {rsi if rsi else 'n/a'} ï¿½ {rsi_label}\n\n"
            f"Bollinger Bands (20, 2s):\n"
            f"  Upper: ${upper}\n"
            f"  Middle: ${mid}\n"
            f"  Lower: ${lower}\n"
            f"  %B: {pct_b} ï¿½ {bb_label}\n"
            f"  Bandwidth: {bw} ï¿½ {squeeze_label}\n\n"
            f"Squeeze Score: {score}/100\n\n"
            f"Claude: {ai}"
        )
    except Exception as e:
        await update.message.reply_text(f"Error computing technicals for {ticker}: {e}")



# ============================================================
# CHART ï¿½ intraday sparkline with volume bars
# ============================================================

def build_chart_image(ticker: str) -> BytesIO:
    BG = "#0d1117"; PANEL = "#161b22"; TEXT = "#e6edf3"
    DIM = "#8b949e"; GREEN = "#2ecc71"; RED = "#e74c3c"; GOLD = "#f0b429"

    hist = yf.Ticker(ticker).history(period="1d", interval="5m")
    if hist.empty:
        raise ValueError(f"No intraday data for {ticker}")

    prices  = hist['Close'].tolist()
    volumes = hist['Volume'].tolist()
    times   = [t.strftime("%H:%M") for t in hist.index]
    open_p  = prices[0]
    color   = GREEN if prices[-1] >= open_p else RED

    # Tick labels ï¿½ show every ~60 min
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

    # VWAP line
    typical = [(h + l + c) / 3 for h, l, c in zip(hist['High'], hist['Low'], hist['Close'])]
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

        # Quick Grok read on the chart shape
        info    = yf.Ticker(ticker).fast_info
        price   = info.get('lastPrice', 0)
        chg     = info.get('regularMarketChangePercent', 0)
        sq      = compute_squeeze_score(ticker)
        rsi_str = f"RSI {sq['rsi']:.0f}" if sq.get('rsi') else ""
        ai = get_ai_response(
            f"{ticker} intraday: ${price:.2f} ({chg:+.2f}% today). "
            f"{rsi_str}. What does this intraday move suggest? One sentence.",
            fast=True
        )
        await update.message.reply_photo(
            photo=buf,
            caption=f"{ticker}  ${price:.2f}  ({chg:+.2f}%)\n{rsi_str}\nClaude: {ai}"
        )
    except Exception as e:
        logger.error(f"Chart error for {ticker}: {e}")
        await update.message.reply_text(f"Could not generate chart for {ticker}: {e}")


# ============================================================
# DASHBOARD ï¿½ visual market snapshot image
# ============================================================

def _clamp_color(val, lo, hi):
    """Map val in [lo,hi] to 0ï¿½1 for a red-white-green colormap."""
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

    # -- Fetch all data concurrently ---------------------------
    def _fetch_indices():
        syms = [("^GSPC","S&P 500"),("^IXIC","Nasdaq"),
                ("^DJI","Dow"),("^RUT","Russell"),("^VIX","VIX")]
        out = []
        for sym, name in syms:
            try:
                info = yf.Ticker(sym).fast_info
                out.append((name,
                             info.get("lastPrice", 0),
                             info.get("regularMarketChangePercent", 0)))
            except:
                out.append((name, 0, 0))
        return out

    def _fetch_sectors():
        syms = [("XLK","Tech"),("XLF","Fin"),("XLE","Energy"),
                ("XLV","Health"),("XLI","Indust"),("XLC","Comm"),
                ("XLY","Cons D"),("XLP","Cons S"),("XLB","Mat"),
                ("XLRE","RE"),("XLU","Util")]
        out = []
        for sym, name in syms:
            try:
                chg = yf.Ticker(sym).fast_info.get("regularMarketChangePercent", 0)
                out.append((name, round(chg, 2)))
            except:
                out.append((name, 0.0))
        return out

    def _fetch_movers():
        items = []
        for t in TICKERS:
            try:
                info  = yf.Ticker(t).fast_info
                chg   = info.get("regularMarketChangePercent", 0)
                price = info.get("lastPrice", 0)
                if price > 0:
                    items.append((t, chg, price))
            except:
                pass
        items.sort(key=lambda x: x[1])
        return items[-5:][::-1], items[:5]   # gainers, losers

    def _fetch_crypto():
        coins = [("BTC-USD","BTC"),("ETH-USD","ETH"),
                 ("SOL-USD","SOL"),("DOGE-USD","DOGE"),("XRP-USD","XRP")]
        out = []
        for sym, name in coins:
            try:
                info  = yf.Ticker(sym).fast_info
                out.append((name,
                             info.get("lastPrice", 0),
                             info.get("regularMarketChangePercent", 0)))
            except:
                pass
        return out

    with ThreadPoolExecutor(max_workers=5) as pool:
        f_idx = pool.submit(_fetch_indices)
        f_sec = pool.submit(_fetch_sectors)
        f_mov = pool.submit(_fetch_movers)
        f_cry = pool.submit(_fetch_crypto)
        f_fg  = pool.submit(get_fear_greed)

    indices          = f_idx.result()
    sectors          = f_sec.result()
    gainers, losers  = f_mov.result()
    crypto           = f_cry.result()
    fg_val, fg_label = f_fg.result()
    fg_val           = int(fg_val) if fg_val else 50

    top_squeeze = sorted(squeeze_scores.items(), key=lambda x: x[1], reverse=True)[:5]

    idx_summary = "  ".join(f"{n} {c:+.1f}%" for n, _, c in indices[:4])
    grok_line = get_ai_response(
        f"Market now: {idx_summary}. Fear&Greed={fg_val}({fg_label}). "
        f"One sentence market call.",
        max_tokens=80, fast=True
    )

    # -- Helpers -----------------------------------------------
    def _setup_panel(ax, title):
        ax.set_facecolor(PANEL)
        for sp in ax.spines.values():
            sp.set_edgecolor(EDGE)
            sp.set_linewidth(0.8)
        ax.set_title(title, color=DIM, fontsize=8.5,
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
        ax.set_yticklabels(names, color=TEXT, fontsize=9)
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
                        color=DIM, fontsize=7.5,
                        transform=ax.transAxes, clip_on=False)

    # -- Figure & grid -----------------------------------------
    fig = plt.figure(figsize=(22, 15), facecolor=BG)
    fig.patch.set_facecolor(BG)
    gs = gridspec.GridSpec(
        4, 4, figure=fig,
        hspace=0.60, wspace=0.45,
        top=0.91, bottom=0.04,
        left=0.05, right=0.95
    )

    # -- Header ------------------------------------------------
    fig.text(0.05, 0.957, "STOCK SPIKE MONITOR  //  LIVE DASHBOARD",
             color=TEXT, fontsize=15, fontweight="bold")
    fig.text(0.05, 0.934, now_str, color=DIM, fontsize=9)
    fig.text(0.32, 0.934,
             f"Market: {session.upper()}",
             color=session_color, fontsize=9, fontweight="bold")
    # Grok one-liner ï¿½ wrap manually to avoid matplotlib wrap quirks
    gl = grok_line[:120] + ("ï¿½" if len(grok_line) > 120 else "")
    fig.text(0.05, 0.918, f"Claude AI: {gl}",
             color=GOLD, fontsize=8, style="italic")

    # -- [A] Indices -------------------------------------------
    ax_idx = fig.add_subplot(gs[0, :2])
    _setup_panel(ax_idx, "MAJOR INDICES  (% change)")
    i_names  = [n for n, _, _ in indices]
    i_chgs   = [c for _, _, c in indices]
    i_prices = [f"${p:,.2f}" if p < 10000 else f"${p:,.0f}"
                for _, p, _ in indices]
    _barh_chart(ax_idx, i_names, i_chgs,
                [_bar_color(c) for c in i_chgs],
                price_strs=i_prices)

    # -- [B] Fear & Greed gauge --------------------------------
    ax_fg = fig.add_subplot(gs[0, 2])
    ax_fg.set_facecolor(PANEL)
    for sp in ax_fg.spines.values():
        sp.set_edgecolor(EDGE)
    ax_fg.set_title("FEAR & GREED", color=DIM, fontsize=8.5,
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
               fontsize=7.5, color=GOLD, zorder=5)

    # -- [C] Sector heatmap ------------------------------------
    ax_sec = fig.add_subplot(gs[0, 3])
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

    # -- [D] Top Gainers ---------------------------------------
    ax_gn = fig.add_subplot(gs[1, :2])
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

    # -- [E] Top Losers ----------------------------------------
    ax_ls = fig.add_subplot(gs[1, 2:])
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

    # -- [F] Squeeze Leaderboard -------------------------------
    ax_sq = fig.add_subplot(gs[2, :2])
    _setup_panel(ax_sq, "SQUEEZE LEADERBOARD  (score 0ï¿½100)")
    if top_squeeze:
        sq_names  = [t for t, _ in top_squeeze]
        sq_scores = [s for _, s in top_squeeze]
        ys_sq = list(range(len(sq_names)))
        sq_cols = [plt.cm.YlOrRd(s / 100) for s in sq_scores]
        ax_sq.barh(ys_sq, sq_scores, color=sq_cols, height=0.55, zorder=3)
        ax_sq.set_xlim(0, 115)
        ax_sq.set_yticks(ys_sq)
        ax_sq.set_yticklabels(sq_names, color=TEXT, fontsize=9)
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
        ax_sq.text(0.5, 0.5, "Buildingï¿½ (needs 2ï¿½3 scan cycles)",
                   ha="center", va="center", color=DIM, fontsize=9,
                   transform=ax_sq.transAxes)
        ax_sq.axis("off")

    # -- [G] Crypto --------------------------------------------
    ax_cr = fig.add_subplot(gs[2, 2:])
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

    # -- [H] Recent Spike Alerts -------------------------------
    ax_al = fig.add_subplot(gs[3, :3])
    ax_al.set_facecolor(PANEL)
    for sp in ax_al.spines.values():
        sp.set_edgecolor(EDGE)
    ax_al.set_title("RECENT SPIKE ALERTS", color=DIM, fontsize=8.5,
                    fontweight="bold", loc="left", pad=5)
    ax_al.axis("off")
    alerts_display = (recent_alerts[-12:] if recent_alerts
                      else ["No spikes yet today"])
    ncols_al = 3
    rows_al  = math.ceil(len(alerts_display) / ncols_al)
    row_h    = 1.0 / max(rows_al, 1)
    for idx, alert in enumerate(alerts_display):
        col = idx % ncols_al
        row = idx // ncols_al
        ax_al.text(col / ncols_al + 0.02,
                   0.92 - row * row_h * 0.85,
                   f"? {alert}",
                   ha="left", va="top",
                   color=GOLD if "%" in alert else DIM,
                   fontsize=8, transform=ax_al.transAxes,
                   clip_on=True)

    # -- [I] Bot Status ----------------------------------------
    ax_st = fig.add_subplot(gs[3, 3])
    ax_st.set_facecolor(PANEL)
    for sp in ax_st.spines.values():
        sp.set_edgecolor(EDGE)
    ax_st.set_title("BOT STATUS", color=DIM, fontsize=8.5,
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
                   fontsize=8.5, transform=ax_st.transAxes)

    # -- Save --------------------------------------------------
    buf = BytesIO()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor=BG, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf



async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate and send the full visual market dashboard."""
    await update.message.reply_text("Building dashboardï¿½ this takes ~10 seconds ?")
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        buf  = await loop.run_in_executor(None, build_dashboard_image)
        await update.message.reply_photo(
            photo=buf,
            caption=(
                f"?? Live Dashboard ï¿½ {datetime.now(CT).strftime('%I:%M %p CT')}\n"
                f"Indices ï¿½ Sectors ï¿½ Gainers/Losers ï¿½ Squeeze ï¿½ Crypto ï¿½ Alerts"
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
        caption = f"?? Dashboard ï¿½ {label}  {datetime.now(CT).strftime('%I:%M %p CT')}"
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
        await update.message.reply_text(
            "Monitoring PAUSED.\n"
            "Spike scanning stopped. Dashboards and scheduled messages continue.\n"
            "Resume with: /monitoring resume"
        )
    elif mode == "resume":
        monitoring_paused = False
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
# NATURAL LANGUAGE HANDLER ï¿½ the "ask anything" feature
# ============================================================
def _offhours_note() -> str:
    """Returns a contextual note string when market is closed, empty string otherwise."""
    session = get_trading_session()
    if session != "closed":
        return ""
    now = datetime.now(CT)
    if now.weekday() >= 5:
        days_to_open = 7 - now.weekday()   # Mon = 0
        return f"(Weekend ï¿½ market reopens Monday)"
    t = now.time()
    if t < datetime.strptime("07:00", "%H:%M").time():
        return "(Pre-market ï¿½ last close data)"
    return "(After hours ï¿½ last close data)"


async def cmd_prep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /prep ï¿½ Claude-powered game plan for the next trading session.
    Pulls last-close data for the watchlist and asks Claude what to watch.
    """
    await update.message.reply_text("Building tomorrow's game planï¿½")
    now_label = datetime.now(CT).strftime("%A %B %d, %Y  %I:%M %p CT")
    session   = get_trading_session()

    # Gather last-close snapshot for top watched tickers
    snap_lines = []
    for t in list(TICKERS)[:15]:
        try:
            info  = yf.Ticker(t).fast_info
            price = info.get("lastPrice", 0)
            chg   = info.get("regularMarketChangePercent", 0)
            high52 = info.get("fiftyTwoWeekHigh", 0)
            pct_off = ((price - high52) / high52 * 100) if high52 else 0
            if price > 0:
                snap_lines.append(
                    f"{t} ${price:.2f} ({chg:+.2f}%) {pct_off:+.1f}% from 52w high"
                )
        except:
            pass

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


async def cmd_overnight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /overnight ï¿½ overnight risk assessment for open paper positions.
    Checks for earnings, gap risk, macro exposure.
    """
    if not paper_positions:
        await update.message.reply_text("No open paper positions to assess.")
        return

    await update.message.reply_text("Checking overnight risk on open positionsï¿½")
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
        except:
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
        f"If uncertain about specific dates, say so ï¿½ don't invent events. "
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
    /wlprep ï¿½ deep dive on the full watchlist with technicals.
    Useful on weekends to rank stocks by setup quality going into Monday.
    """
    await update.message.reply_text(
        f"Running technical scan on {len(TICKERS)} watched stocksï¿½ "
        f"(this takes ~20 seconds)"
    )
    now_label = datetime.now(CT).strftime("%A %B %d, %Y")
    note      = _offhours_note()

    scored = []
    for ticker in list(TICKERS)[:20]:
        try:
            sq = compute_squeeze_score(ticker)
            if sq.get("score") is not None:
                info  = yf.Ticker(ticker).fast_info
                price = info.get("lastPrice", 0)
                chg   = info.get("regularMarketChangePercent", 0)
                scored.append((ticker, sq["score"], price, chg,
                                sq.get("rsi", 0), sq.get("bandwidth", 0)))
        except:
            pass

    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[:8]

    lines = [f"WATCHLIST TECHNICAL SCAN {note}", now_label, ""]
    for ticker, score, price, chg, rsi, bw in top:
        rsi_flag = "?? overbought" if rsi and rsi > 70 else ("?? oversold" if rsi and rsi < 35 else "")
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
    /ask <question> ï¿½ ask Claude AI anything about the market.
    Maintains multi-turn memory per chat (last 20 messages).
    """
    if not update.message:
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /ask <your question>\n\n"
            "Examples:\n"
            "  /ask What's happening with NVDA today?\n"
            "  /ask Is the market overbought right now?\n"
            "  /ask Explain RSI to me\n"
            "  /ask Should I be worried about the VIX spike?"
        )
        return

    user_msg = " ".join(context.args).strip()
    chat_id  = str(update.effective_chat.id)
    logger.info(f"/ask from {chat_id}: {user_msg[:80]}")

    await update.message.reply_text("Thinkingï¿½")

    # Enrich with live price if a watched ticker is mentioned
    enriched = user_msg
    words = user_msg.upper().split()
    for word in words:
        clean = ''.join(c for c in word if c.isalpha())
        if clean and clean in TICKERS:
            try:
                info  = yf.Ticker(clean).fast_info
                price = info.get('lastPrice', 0)
                chg   = info.get('regularMarketChangePercent', 0)
                if price > 0:
                    enriched += f" [Live: {clean}=${price:.2f}, {chg:+.2f}% today]"
            except:
                pass
            break

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
    """8:30 AM CT ï¿½ market open briefing with full live data."""
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
        [f"?? Morning Briefing ï¿½ {datetime.now(CT).strftime('%B %d, %Y')}",
         f"{datetime.now(CT).strftime('%I:%M %p CT')}", ""] +
        s["indices_lines"] +
        [f"", f"Fear & Greed: {s['fg_str']}  VIX: {s['vix']:.1f}",
         f"", "Sectors:"] + s["sector_lines"][:5] +
        [f"", f"Claude:\n{ai}"]
    )
    send_telegram("\n".join(msg_lines))
    send_dashboard_sync("Market Open")

def send_daily_close_summary():
    """3:00 PM CT ï¿½ closing bell summary with full live data."""
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
        [f"?? Market Close ï¿½ {datetime.now(CT).strftime('%I:%M %p CT')}",
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
        [f"?? STOCK SPIKE MONITOR STARTED",
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
    """Sunday 6 PM CT ï¿½ week-in-review with live snapshot."""
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
        [f"?? Weekly Digest ï¿½ {s['now_label']}",
         f"Total spike alerts this week: {len(recent_alerts)}", ""] +
        s["indices_lines"] +
        [f"", f"Fear & Greed: {s['fg_str']}  VIX: {s['vix']:.1f}",
         f"", "Most active tickers:"] +
        [f"  {t}: {n} alert{'s' if n > 1 else ''}" for t, n in ranked[:8]] +
        [f"", f"Claude: {ai}"]
    )
    send_telegram("\n".join(lines))


def send_premarket_dashboard():
    """8:00 AM CT ï¿½ pre-market snapshot with live data before regular open."""
    logger.info("Pre-market dashboard")
    s = fetch_market_snapshot()

    # Pre-market futures via yfinance extended hours
    futures_lines = []
    for sym, label in [("ES=F","S&P Fut"),("NQ=F","Nasdaq Fut"),("YM=F","Dow Fut")]:
        try:
            fi    = yf.Ticker(sym).fast_info
            price = fi.get("lastPrice") or 0
            chg   = fi.get("regularMarketChangePercent") or 0
            arrow = "?" if chg >= 0 else "?"
            futures_lines.append(f"{arrow} {label}: {price:,.0f} ({chg:+.2f}%)")
        except:
            pass

    futures_str = " | ".join(futures_lines) or "futures unavailable"

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
        f"Base your answer strictly on the numbers above. Plain text.",
        )
    ai = get_ai_response(prompt[0], max_tokens=200, fast=True)

    msg_lines = [
        f"?? Pre-Market Snapshot ï¿½ {datetime.now(CT).strftime('%I:%M %p CT')}",
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
        [f"?? Mid-Day Check-In ï¿½ {datetime.now(CT).strftime('%I:%M %p CT')}",
         s["now_label"], ""] +
        s["indices_lines"] +
        [f"", f"Fear & Greed: {s['fg_str']}  VIX: {s['vix']:.1f}",
         f"Alerts today: {daily_alerts}",
         f"", f"Claude: {ai}"]
    )
    send_telegram("\n".join(msg_lines))
    send_dashboard_sync("Mid-Day")


def send_evening_recap():
    """6:00 PM CT ï¿½ after-hours recap + tomorrow setup."""
    if get_trading_session() != "closed":
        return   # skip if still in extended hours
    now_label = datetime.now(CT).strftime("%A %B %d, %Y")
    snap = []
    for sym, name in [("^GSPC","S&P 500"),("^IXIC","Nasdaq"),("^DJI","Dow")]:
        try:
            info = yf.Ticker(sym).fast_info
            chg  = info.get("regularMarketChangePercent", 0)
            snap.append(f"{name} {chg:+.2f}%")
        except:
            pass
    snap_str = " | ".join(snap)
    fg_val, fg_label = get_fear_greed()
    ai = get_ai_response(
        f"Today is {now_label}. Market closed. Final: {snap_str}. "
        f"Fear & Greed: {fg_val} ({fg_label}). "
        f"(1) One sentence on today's key theme. "
        f"(2) Two things to watch for tomorrow's open. "
        f"Keep it to 3 sentences total. Plain text.",
        max_tokens=180
    )
    send_telegram(
        f"Evening Recap ï¿½ {datetime.now(CT).strftime('%I:%M %p CT')}\n"
        f"{snap_str}\n\n"
        f"Claude: {ai}\n\n"
        f"Use /prep for tomorrow's game plan  ï¿½  /overnight for position risk"
    )


def send_saturday_prep():
    """Saturday 9:00 AM CT ï¿½ weekend watchlist prep digest."""
    now_label = datetime.now(CT).strftime("%A %B %d, %Y")
    scored = []
    for ticker in list(TICKERS)[:20]:
        try:
            sq    = compute_squeeze_score(ticker)
            info  = yf.Ticker(ticker).fast_info
            price = info.get("lastPrice", 0)
            chg   = info.get("regularMarketChangePercent", 0)
            if sq.get("score") and price > 0:
                scored.append((ticker, sq["score"], price, chg, sq.get("rsi", 0)))
        except:
            pass
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
    lines = [f"Weekend Watchlist Prep ï¿½ {now_label}", f"Fear & Greed: {fg_val} ({fg_label})", ""]
    for ticker, score, price, chg, rsi in top:
        lines.append(f"  {ticker:6}  score={score:.0f}  RSI={rsi:.0f}  ${price:.2f} ({chg:+.2f}%)")
    lines += ["", f"Claude: {ai}", "", "Use /prep or /wlprep for deeper analysis"]
    send_telegram("\n".join(lines))


# ============================================================
# BACKGROUND SCANNER
# ============================================================
def scanner_thread():
    """
    Background thread ï¿½ timezone-independent scheduler.

    All job times are defined in CT (America/Chicago).
    The loop reads datetime.now(CT) directly, so it fires correctly
    regardless of the server's system timezone (UTC on Railway, local
    time on a dev machine, etc.).

    Job table format:
        (day, "HH:MM", function)
        day = "daily" | "monday"ï¿½"sunday"
    """

    # -- Define all scheduled jobs in CT -----------------------
    JOBS = [
        # day            CT time   function
        ("daily",        "08:00",  send_premarket_dashboard),
        ("daily",        "08:30",  lambda: globals().update(TICKERS=get_dynamic_hot_stocks())),
        ("daily",        "08:30",  send_morning_briefing),
        ("daily",        "08:31",  paper_morning_report),
        ("daily",        "12:00",  send_midday_dashboard),
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
    last_scan   = datetime.now(CT) - timedelta(minutes=CHECK_INTERVAL_MIN + 1)

    logger.info(
        f"Scheduler started ï¿½ all times in CT "
        f"(server local: {datetime.now().strftime('%Z %z') or 'unknown'})"
    )

    while True:
        now_ct    = datetime.now(CT)
        now_hhmm  = now_ct.strftime("%H:%M")
        now_day   = DAY_NAMES[now_ct.weekday()]
        fire_key  = f"{now_ct.strftime('%Y-%m-%d')}-{now_hhmm}"  # unique per day+minute

        # -- Timed jobs ----------------------------------------
        for day, hhmm, fn in JOBS:
            job_key = f"{fire_key}-{day}-{hhmm}"
            if now_hhmm == hhmm and (day == "daily" or day == now_day):
                if job_key not in fired:
                    fired.add(job_key)
                    logger.info(f"Firing scheduled job: {day} {hhmm} CT ? {fn.__name__ if hasattr(fn,'__name__') else 'lambda'}")
                    try:
                        fn()
                    except Exception as e:
                        logger.error(f"Scheduled job error ({day} {hhmm}): {e}", exc_info=True)

        # -- Prune fired set daily to avoid unbounded growth ---
        if len(fired) > 500:
            today_prefix = now_ct.strftime("%Y-%m-%d")
            fired = {k for k in fired if k.startswith(today_prefix)}

        # -- Stock scanner ï¿½ every CHECK_INTERVAL_MIN minutes --
        elapsed = (now_ct - last_scan).total_seconds() / 60
        if elapsed >= CHECK_INTERVAL_MIN:
            last_scan = now_ct
            try:
                check_stocks()
            except Exception as e:
                logger.error(f"check_stocks error: {e}", exc_info=True)

        time.sleep(30)   # check twice per minute ï¿½ plenty for minute-precision jobs


# ============================================================
# MAIN ï¿½ Telegram bot
# ============================================================
def run_telegram_bot():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # -- Market Pulse ------------------------------------------
    app.add_handler(CommandHandler("overview",    cmd_overview))
    app.add_handler(CommandHandler("crypto",      cmd_crypto))
    app.add_handler(CommandHandler("macro",       cmd_macro))
    app.add_handler(CommandHandler("earnings",    cmd_earnings))

    # -- Movers ------------------------------------------------
    app.add_handler(CommandHandler("movers",      cmd_movers))

    # -- Stock Tools -------------------------------------------
    app.add_handler(CommandHandler("price",       cmd_price))
    app.add_handler(CommandHandler("analyze",     cmd_analyze))
    app.add_handler(CommandHandler("compare",     cmd_compare))
    app.add_handler(CommandHandler("chart",       cmd_chart))
    app.add_handler(CommandHandler("rsi",         cmd_rsi))
    app.add_handler(CommandHandler("news",        cmd_news))

    # -- Alerts & Scanning -------------------------------------
    app.add_handler(CommandHandler("spikes",      cmd_spikes))
    app.add_handler(CommandHandler("alerts",      cmd_alerts))
    app.add_handler(CommandHandler("squeeze",     cmd_squeeze))
    app.add_handler(CommandHandler("setalert",    cmd_setalert))
    app.add_handler(CommandHandler("watchlist",   cmd_watchlist))

    # -- Bot Control -------------------------------------------
    app.add_handler(CommandHandler("dashboard",   cmd_dashboard))
    app.add_handler(CommandHandler("list",        cmd_list))
    app.add_handler(CommandHandler("monitoring",  cmd_monitoring))
    app.add_handler(CommandHandler("help",        cmd_help))

    # -- Paper Trading -----------------------------------------
    app.add_handler(CommandHandler("paper",       cmd_paper))
    app.add_handler(CommandHandler("overnight",   cmd_overnight))

    # -- Off-hours / prep --------------------------------------
    app.add_handler(CommandHandler("prep",        cmd_prep))
    app.add_handler(CommandHandler("wlprep",      cmd_watchlist_prep))

    app.add_handler(CommandHandler("ask",         cmd_ask))

    app.run_polling()

# ============================================================
# ENTRY POINT
# ============================================================
threading.Thread(target=scanner_thread, daemon=True).start()
logger.info("FULL INTERACTIVE MONITOR WITH BULLISH FILTER STARTED")
load_paper_state()   # restore paper trading state from disk
send_startup_message()
run_telegram_bot()