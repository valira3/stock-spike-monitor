import yfinance as yf
import time
import schedule
import requests
import pandas as pd
from datetime import datetime, timedelta
import pytz
import logging
from collections import deque
from openai import OpenAI
import os
import threading
import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    MessageHandler, filters
)

# ============================================================
# CONFIG FROM ENVIRONMENT VARIABLES
# ============================================================
FINNHUB_TOKEN   = os.getenv("FINNHUB_TOKEN")
GROK_API_KEY    = os.getenv("GROK_API_KEY")
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
CHAT_ID         = os.getenv("CHAT_ID")
FMP_API_KEY     = os.getenv("FMP_API_KEY")

THRESHOLD           = 0.03   # 3% spike
MIN_PRICE           = 5.0
COOLDOWN_MINUTES    = 5
CHECK_INTERVAL_MIN  = 1
VOLUME_SPIKE_MULT   = 2.0    # alert if volume 2× average
LOG_FILE            = "stock_spike_monitor.log"
GROK_MODEL          = "grok-4-1-fast-non-reasoning"

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
# GROK CLIENT
# ============================================================
grok_client = OpenAI(
    api_key=GROK_API_KEY,
    base_url="https://api.x.ai/v1"
) if GROK_API_KEY else None

# ============================================================
# BOT DESCRIPTION (used by /about and natural-language handler)
# ============================================================
BOT_DESCRIPTION = """
Stock Spike Monitor — a 24/7 real-time market intelligence bot.

What it does:
• Scans 60+ BULLISH stocks every minute for ≥3% price spikes
• Sends instant alerts with Grok AI analysis + latest news
• Dynamically refreshes its watchlist each morning using FMP screener
• Delivers a morning briefing (8:30 AM CT) and daily close recap (3:00 PM CT)
• Lets you query the market in plain English via Grok AI

Commands:
/about         — What this bot does
/status        — Running status + alert count
/list          — All monitored tickers
/alerts        — Alerts fired today
/market        — Live index snapshot + AI sentiment
/spikes        — Spikes in last 30 minutes
/topgainers    — Top 5 gainers today
/toplosers     — Top 5 losers today
/highvolume    — Most active stocks
/lowprice      — Low-priced rockets ($1–$10)
/sectors       — S&P sector performance
/analyze TICK  — Deep AI analysis of any stock
/price TICK    — Quick price + stats
/compare A B   — Compare two stocks side-by-side
/earnings      — Upcoming earnings this week
/fear          — Fear & Greed Index
/crypto        — Top crypto prices
/news TICK     — Latest headlines for a ticker
/watchlist     — Manage your personal watchlist
/setalert      — Set a custom price alert
/squeeze       — Top squeeze candidates (RSI + BB + volume score)
/rsi TICK      — RSI + Bollinger Bands for any ticker
/pause         — Pause spike monitoring
/resume        — Resume spike monitoring
/help          — This help menu

You can also just type any question in plain English and Grok AI will answer it!
"""

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
# GROK HELPERS — with exponential backoff
# ============================================================
def get_grok_response(prompt, system=None, max_tokens=300):
    if not grok_client:
        return "AI unavailable (no GROK_API_KEY)"
    sys_msg = system or (
        "You are a sharp, concise stock market analyst. "
        "Give direct, data-driven insights. No fluff. Max 3 sentences unless asked for more."
    )
    for attempt in range(4):
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
            return resp.choices[0].message.content.strip()
        except Exception as e:
            wait = 2 ** attempt
            logger.error(f"Grok error (attempt {attempt+1}): {e}. Retry in {wait}s")
            time.sleep(wait)
    return "Grok unavailable"

def get_grok_conversation(chat_id, user_message):
    """Multi-turn conversational Grok with memory."""
    if not grok_client:
        return "AI unavailable (no GROK_API_KEY)"
    history = conversation_history.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_message})
    # Keep last 10 turns
    if len(history) > 20:
        history = history[-20:]
        conversation_history[chat_id] = history

    system = (
        "You are a real-time stock market assistant bot on Telegram. "
        "You have access to live market data and Grok AI. "
        "Answer questions about stocks, markets, investing, and finance. "
        "Be concise and direct. Use plain text (no markdown). "
        f"Today is {datetime.now(CT).strftime('%A %B %d %Y %I:%M %p CT')}."
    )
    try:
        resp = grok_client.chat.completions.create(
            model=GROK_MODEL,
            messages=[{"role": "system", "content": system}] + history,
            max_tokens=400,
            temperature=0.5
        )
        reply = resp.choices[0].message.content.strip()
        history.append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        logger.error(f"Grok conversation error: {e}")
        return "Grok unavailable right now."

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
    Squeeze score 0–100 combining:
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
            arrow = "▲" if chg >= 0 else "▼"
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
            arrow = "▲" if chg >= 0 else "▼"
            lines.append(f"{arrow} {name}: ${price:,.2f} ({chg:+.2f}%)")
        except:
            pass
    return lines

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
    logger.info(f"Watchlist updated → {len(combined)} stocks ({len(low_price)} low-price rockets)")
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
    ai        = get_grok_response(grok_prompt)
    news_text = "\n".join([f"• {h[:80]}" for h, _ in news_items]) if news_items else "No news"

    message = (
        f"🚨 {ticker} {spike_label}\n"
        f"{pct_change:+.1f}% | ${current_price:.2f}"
        + (" | 🔊 Vol Spike" if volume_spike else "") + "\n"
        + (f"{tech_str}\n" if tech_str else "")
        + f"\nGrok: {ai}\n\n"
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
                f"🎯 Price Alert Hit!\n{ticker} reached ${current_price:.2f}\n(Target: ${target:.2f})"
            )
            triggered.append(target)
    for t in triggered:
        custom_price_alerts[ticker].remove(t)

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

# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(BOT_DESCRIPTION)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(BOT_DESCRIPTION)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_trading_session()
    status  = "PAUSED" if monitoring_paused else "RUNNING"
    fg_val, fg_label = get_fear_greed()
    fear_str = f"Fear & Greed: {fg_val} ({fg_label})" if fg_val else ""
    await update.message.reply_text(
        f"Status: {status}\n"
        f"Market: {session.upper()}\n"
        f"Stocks watched: {len(TICKERS)}\n"
        f"Alerts today: {daily_alerts}\n"
        f"Spike threshold: {THRESHOLD*100:.0f}%\n"
        + (f"{fear_str}" if fear_str else "")
    )

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

async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    indices = {
        "^GSPC": "S&P 500", "^IXIC": "Nasdaq",
        "^DJI":  "Dow",     "^RUT":  "Russell 2000",
        "^VIX":  "VIX"
    }
    lines = []
    for sym, name in indices.items():
        try:
            info  = yf.Ticker(sym).fast_info
            price = info.get('lastPrice', 0)
            chg   = info.get('regularMarketChangePercent', 0)
            arrow = "▲" if chg >= 0 else "▼"
            lines.append(f"{arrow} {name}: {price:,.2f} ({chg:+.2f}%)")
        except:
            pass
    fg_val, fg_label = get_fear_greed()
    if fg_val:
        lines.append(f"\nFear & Greed: {fg_val} — {fg_label}")

    summary    = " | ".join(lines[:4])
    grok_prompt = f"Market snapshot: {summary}. Give 2-sentence market outlook."
    ai = get_grok_response(grok_prompt)
    await update.message.reply_text(
        "Market Now:\n" + "\n".join(lines) + f"\n\nGrok: {ai}"
    )

async def cmd_spikes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not recent_alerts:
        await update.message.reply_text("No spikes in the last 30 minutes.")
        return
    await update.message.reply_text(
        "Recent spikes:\n" + "\n".join(recent_alerts[-10:])
    )

async def cmd_sectors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = get_sector_performance()
    if not lines:
        await update.message.reply_text("Unable to fetch sector data.")
        return
    summary = ", ".join(lines[:5])
    ai = get_grok_response(f"Sector snapshot: {summary}. Which sector looks strongest today and why?")
    await update.message.reply_text(
        "Sector Performance:\n" + "\n".join(lines) + f"\n\nGrok: {ai}"
    )

async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /analyze TICKER (e.g. /analyze NVDA)")
        return
    ticker = context.args[0].upper()
    await update.message.reply_text(f"Analyzing {ticker}...")
    try:
        info  = yf.Ticker(ticker).fast_info
        price = info.get('lastPrice', 0)
        chg   = info.get('regularMarketChangePercent', 0)
        mcap  = info.get('marketCap', 0) / 1e9
        vol   = info.get('lastVolume', 0)
        high52 = info.get('fiftyTwoWeekHigh', 0)
        low52  = info.get('fiftyTwoWeekLow', 0)
        pct_from_high = ((price - high52) / high52 * 100) if high52 else 0

        news_items = fetch_latest_news(ticker, 3)
        news_str   = "; ".join([h[:60] for h, _ in news_items]) if news_items else "no recent news"

        prompt = (
            f"Deep analysis of {ticker}: Price ${price:.2f} ({chg:+.2f}%), "
            f"Mkt Cap ${mcap:.1f}B, Volume {vol:,}, "
            f"52w High ${high52:.2f} ({pct_from_high:+.1f}% from high), "
            f"52w Low ${low52:.2f}. "
            f"Recent news: {news_str}. "
            f"Provide: (1) technical assessment (2) near-term catalyst (3) key risk. Be specific."
        )
        ai = get_grok_response(prompt, max_tokens=500)

        await update.message.reply_text(
            f"{ticker} Analysis\n"
            f"Price: ${price:.2f} ({chg:+.2f}%)\n"
            f"Mkt Cap: ${mcap:.1f}B\n"
            f"Volume: {vol:,}\n"
            f"52w Range: ${low52:.2f} – ${high52:.2f}\n"
            f"From 52w High: {pct_from_high:+.1f}%\n\n"
            f"Grok Analysis:\n{ai}"
        )
    except Exception as e:
        await update.message.reply_text(f"Unable to analyze {ticker}: {e}")

async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /price TICKER (e.g. /price AAPL)")
        return
    ticker = context.args[0].upper()
    try:
        info   = yf.Ticker(ticker).fast_info
        price  = info.get('lastPrice', 0)
        chg    = info.get('regularMarketChangePercent', 0)
        chg_abs = info.get('regularMarketChange', 0)
        vol    = info.get('lastVolume', 0)
        high   = info.get('dayHigh', 0)
        low    = info.get('dayLow', 0)
        arrow  = "▲" if chg >= 0 else "▼"
        await update.message.reply_text(
            f"{arrow} {ticker}: ${price:.2f}\n"
            f"Change: {chg_abs:+.2f} ({chg:+.2f}%)\n"
            f"Day: ${low:.2f} – ${high:.2f}\n"
            f"Volume: {vol:,}"
        )
    except Exception as e:
        await update.message.reply_text(f"Could not fetch {ticker}: {e}")

async def cmd_compare(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /compare TICKER1 TICKER2 (e.g. /compare NVDA AMD)")
        return
    t1, t2 = context.args[0].upper(), context.args[1].upper()
    try:
        rows = []
        stats = {}
        for t in [t1, t2]:
            info = yf.Ticker(t).fast_info
            stats[t] = {
                "price": info.get('lastPrice', 0),
                "chg":   info.get('regularMarketChangePercent', 0),
                "mcap":  info.get('marketCap', 0) / 1e9,
                "high52": info.get('fiftyTwoWeekHigh', 0),
                "low52":  info.get('fiftyTwoWeekLow', 0),
            }
        def fmt(val, fmt_str):
            return fmt_str.format(val)

        lines = [
            f"{'Metric':<16} {t1:>8} {t2:>8}",
            f"{'-'*34}",
            f"{'Price':<16} ${stats[t1]['price']:>7.2f} ${stats[t2]['price']:>7.2f}",
            f"{'Change %':<16} {stats[t1]['chg']:>+7.2f}% {stats[t2]['chg']:>+7.2f}%",
            f"{'Mkt Cap $B':<16} {stats[t1]['mcap']:>7.1f} {stats[t2]['mcap']:>7.1f}",
            f"{'52w High':<16} ${stats[t1]['high52']:>7.2f} ${stats[t2]['high52']:>7.2f}",
            f"{'52w Low':<16} ${stats[t1]['low52']:>7.2f} ${stats[t2]['low52']:>7.2f}",
        ]
        summary = (
            f"{t1} ${stats[t1]['price']:.2f} ({stats[t1]['chg']:+.2f}%) vs "
            f"{t2} ${stats[t2]['price']:.2f} ({stats[t2]['chg']:+.2f}%). "
            f"Which is the better buy right now and why?"
        )
        ai = get_grok_response(summary)
        await update.message.reply_text("\n".join(lines) + f"\n\nGrok: {ai}")
    except Exception as e:
        await update.message.reply_text(f"Compare failed: {e}")

async def cmd_topgainers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        df   = pd.read_html("https://finance.yahoo.com/screener/predefined/day_gainers")[0]
        text = "Top Gainers:\n" + "\n".join(
            [f"• {row['Symbol']} +{row['% Change']:.1f}%" for _, row in df.head(5).iterrows()]
        )
        await update.message.reply_text(text)
    except:
        await update.message.reply_text("Unable to fetch top gainers right now.")

async def cmd_toplosers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        df   = pd.read_html("https://finance.yahoo.com/screener/predefined/day_losers")[0]
        text = "Top Losers:\n" + "\n".join(
            [f"• {row['Symbol']} {row['% Change']:.1f}%" for _, row in df.head(5).iterrows()]
        )
        await update.message.reply_text(text)
    except:
        await update.message.reply_text("Unable to fetch top losers right now.")

async def cmd_highvolume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        df   = pd.read_html("https://finance.yahoo.com/screener/predefined/most_actives")[0]
        text = "Most Active:\n" + "\n".join(
            [f"• {row['Symbol']}" for _, row in df.head(8).iterrows()]
        )
        await update.message.reply_text(text)
    except:
        await update.message.reply_text("Unable to fetch most active stocks.")

async def cmd_lowprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        df   = pd.read_html("https://finance.yahoo.com/screener/predefined/day_gainers")[0]
        low  = df[
            (df.get("Price (Intraday)", 0).astype(float, errors='ignore') >= 1) &
            (df.get("Price (Intraday)", 0).astype(float, errors='ignore') <= 10)
        ]
        text = "Low-Priced Rockets ($1–$10):\n" + "\n".join(
            [f"• {row['Symbol']} +{row['% Change']:.1f}%" for _, row in low.head(8).iterrows()]
        )
        await update.message.reply_text(text)
    except:
        await update.message.reply_text("Unable to fetch low-priced rockets.")

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
            lines.append(f"• {sym} on {date} (EPS est: {eps})")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Unable to fetch earnings: {e}")

async def cmd_fear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val, label = get_fear_greed()
    if val is None:
        await update.message.reply_text("Unable to fetch Fear & Greed index.")
        return
    emoji = "😱" if int(val) < 25 else "😨" if int(val) < 45 else "😐" if int(val) < 55 else "😀" if int(val) < 75 else "🤑"
    ai = get_grok_response(
        f"Fear & Greed Index is {val} ({label}). "
        f"What does this mean for short-term traders right now?"
    )
    await update.message.reply_text(
        f"{emoji} Fear & Greed Index\n"
        f"Value: {val} / 100\n"
        f"Sentiment: {label}\n\n"
        f"Grok: {ai}"
    )

async def cmd_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = get_crypto_prices()
    if not lines:
        await update.message.reply_text("Unable to fetch crypto prices.")
        return
    summary = " | ".join(lines[:3])
    ai = get_grok_response(f"Crypto snapshot: {summary}. One-sentence crypto market outlook.")
    await update.message.reply_text(
        "Crypto Prices:\n" + "\n".join(lines) + f"\n\nGrok: {ai}"
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
                arrow = "▲" if chg >= 0 else "▼"
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
            "No squeeze data yet — scores build up after a few scan cycles.\n"
            "Try again in 2–3 minutes."
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
    ai = get_grok_response(
        f"These stocks have the highest squeeze scores right now: {top_names}. "
        f"Are any of them actual short-squeeze or momentum candidates? Be specific."
    )
    await update.message.reply_text("\n".join(lines) + f"\n\nGrok: {ai}")


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
            "Above upper band — extended" if pct_b and pct_b > 1 else
            "Below lower band — oversold" if pct_b and pct_b < 0 else
            "Upper half — bullish"        if pct_b and pct_b > 0.5 else
            "Lower half — building"
        )
        squeeze_label = "TIGHT SQUEEZE" if bw and bw < 0.04 else ("Moderate" if bw and bw < 0.08 else "Wide")

        sq    = compute_squeeze_score(ticker)
        score = sq.get("score", 0)

        ai = get_grok_response(
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
            f"Grok: {ai}"
        )
    except Exception as e:
        await update.message.reply_text(f"Error computing technicals for {ticker}: {e}")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitoring_paused
    monitoring_paused = True
    await update.message.reply_text("Monitoring PAUSED.")

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitoring_paused
    monitoring_paused = False
    await update.message.reply_text("Monitoring RESUMED.")

# ============================================================
# NATURAL LANGUAGE HANDLER — the "ask anything" feature
# ============================================================
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch all plain-text messages and route to Grok AI for Q&A."""
    user_msg = update.message.text.strip()
    if not user_msg:
        return

    chat_id = str(update.effective_chat.id)
    logger.info(f"Q&A from {chat_id}: {user_msg[:80]}")

    # Enrich with live data if user asks about a known ticker
    enriched = user_msg
    words = user_msg.upper().split()
    for word in words:
        if word in TICKERS or (len(word) <= 5 and word.isalpha()):
            try:
                info  = yf.Ticker(word).fast_info
                price = info.get('lastPrice', 0)
                chg   = info.get('regularMarketChangePercent', 0)
                if price > 0:
                    enriched += f" [Live data: {word} = ${price:.2f}, {chg:+.2f}% today]"
                    break
            except:
                pass

    await update.message.reply_text("Thinking...")
    reply = get_grok_conversation(chat_id, enriched)
    await update.message.reply_text(reply)

# ============================================================
# SCHEDULED MESSAGES
# ============================================================
def send_morning_briefing():
    global daily_alerts
    daily_alerts = 0
    logger.info("Morning briefing")

    indices = {"^GSPC": "S&P 500", "^IXIC": "Nasdaq", "^DJI": "Dow"}
    lines   = []
    for sym, name in indices.items():
        try:
            info = yf.Ticker(sym).fast_info
            chg  = info.get('regularMarketChangePercent', 0)
            lines.append(f"{name}: {chg:+.2f}%")
        except:
            pass

    fg_val, fg_label = get_fear_greed()
    sector_lines     = get_sector_performance()[:3]

    summary = " | ".join(lines)
    ai = get_grok_response(
        f"Morning market brief: {summary}. Fear&Greed={fg_val}({fg_label}). "
        f"Top sectors: {', '.join(sector_lines)}. "
        f"Give 3 key things to watch today.",
        max_tokens=400
    )
    send_telegram(
        f"🌅 Morning Briefing — {datetime.now(CT).strftime('%B %d, %Y')}\n\n"
        f"Indices:\n" + "\n".join(lines) + "\n\n"
        f"Fear & Greed: {fg_val} ({fg_label})\n\n"
        f"Top Sectors:\n" + "\n".join(sector_lines) + "\n\n"
        f"Grok:\n{ai}"
    )

def send_daily_close_summary():
    logger.info("Daily close summary")
    ai = get_grok_response(
        f"Market just closed. We had {daily_alerts} spike alerts today. "
        f"Give a 2-sentence close recap and 1 thing to watch overnight."
    )
    send_telegram(
        f"🔔 Daily Close Summary\n"
        f"Alerts today: {daily_alerts}\n\n"
        f"Grok Recap: {ai}"
    )

def send_startup_message():
    session      = get_trading_session()
    status_str   = "OPEN Regular" if session == "regular" else "OPEN Extended" if session == "extended" else "CLOSED"
    ai_sentiment = get_grok_response("Current market sentiment in 6 words.")
    send_telegram(
        f"🚀 STOCK SPIKE MONITOR STARTED\n\n"
        f"Watching {len(TICKERS)} stocks (dynamic BULLISH)\n"
        f"Market: {status_str}\n"
        f"Spike threshold: {THRESHOLD*100:.0f}%\n"
        f"Check interval: {CHECK_INTERVAL_MIN} min\n\n"
        f"Grok: {ai_sentiment}\n\n"
        f"Morning brief: 8:30 AM CT\n"
        f"Daily close: 3:00 PM CT\n\n"
        f"Type any question to chat with Grok AI!\n"
        f"Send /help for all commands."
    )

# ============================================================
# BACKGROUND SCANNER
# ============================================================
def scanner_thread():
    schedule.every(CHECK_INTERVAL_MIN).minutes.do(check_stocks)
    schedule.every().day.at("08:30").do(
        lambda: globals().update(TICKERS=get_dynamic_hot_stocks())
    )
    schedule.every().day.at("08:30").do(send_morning_briefing)
    schedule.every().day.at("15:00").do(send_daily_close_summary)
    while True:
        schedule.run_pending()
        time.sleep(10)

# ============================================================
# MAIN — Telegram bot
# ============================================================
def run_telegram_bot():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("about",       cmd_about))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(CommandHandler("list",        cmd_list))
    app.add_handler(CommandHandler("alerts",      cmd_alerts))
    app.add_handler(CommandHandler("market",      cmd_market))
    app.add_handler(CommandHandler("spikes",      cmd_spikes))
    app.add_handler(CommandHandler("sectors",     cmd_sectors))
    app.add_handler(CommandHandler("analyze",     cmd_analyze))
    app.add_handler(CommandHandler("price",       cmd_price))
    app.add_handler(CommandHandler("compare",     cmd_compare))
    app.add_handler(CommandHandler("topgainers",  cmd_topgainers))
    app.add_handler(CommandHandler("toplosers",   cmd_toplosers))
    app.add_handler(CommandHandler("highvolume",  cmd_highvolume))
    app.add_handler(CommandHandler("lowprice",    cmd_lowprice))
    app.add_handler(CommandHandler("earnings",    cmd_earnings))
    app.add_handler(CommandHandler("fear",        cmd_fear))
    app.add_handler(CommandHandler("crypto",      cmd_crypto))
    app.add_handler(CommandHandler("news",        cmd_news))
    app.add_handler(CommandHandler("watchlist",   cmd_watchlist))
    app.add_handler(CommandHandler("setalert",    cmd_setalert))
    app.add_handler(CommandHandler("squeeze",     cmd_squeeze))
    app.add_handler(CommandHandler("rsi",         cmd_rsi))
    app.add_handler(CommandHandler("pause",       cmd_pause))
    app.add_handler(CommandHandler("resume",      cmd_resume))

    # Natural language Q&A — catches ALL non-command text messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    app.run_polling()

# ============================================================
# ENTRY POINT
# ============================================================
threading.Thread(target=scanner_thread, daemon=True).start()
logger.info("FULL INTERACTIVE MONITOR WITH BULLISH FILTER STARTED")
send_startup_message()
run_telegram_bot()