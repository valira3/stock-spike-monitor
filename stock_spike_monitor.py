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
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
import matplotlib
matplotlib.use(“Agg”)
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

FINNHUB_TOKEN   = os.getenv(“FINNHUB_TOKEN”)
GROK_API_KEY    = os.getenv(“GROK_API_KEY”)
TELEGRAM_TOKEN  = os.getenv(“TELEGRAM_TOKEN”)
CHAT_ID         = os.getenv(“CHAT_ID”)
FMP_API_KEY     = os.getenv(“FMP_API_KEY”)

THRESHOLD           = 0.03   # 3% spike
MIN_PRICE           = 5.0
COOLDOWN_MINUTES    = 5
CHECK_INTERVAL_MIN  = 1
VOLUME_SPIKE_MULT   = 2.0    # alert if volume 2× average
LOG_FILE            = “stock_spike_monitor.log”
GROK_MODEL          = “grok-4-1-fast-non-reasoning”

# ============================================================

# LOGGING

# ============================================================

logging.basicConfig(
level=logging.INFO,
format=’%(asctime)s [%(levelname)s] %(message)s’,
handlers=[
logging.FileHandler(LOG_FILE, encoding=‘utf-8’),
logging.StreamHandler()
]
)
logger = logging.getLogger(**name**)
CT = pytz.timezone(‘America/Chicago’)

# ============================================================

# GROK CLIENT

# ============================================================

grok_client = OpenAI(
api_key=GROK_API_KEY,
base_url=“https://api.x.ai/v1”
) if GROK_API_KEY else None

# ============================================================

# BOT DESCRIPTION (used by /about and natural-language handler)

# ============================================================

BOT_DESCRIPTION = “””
Stock Spike Monitor — 24/7 real-time market intelligence.

Scans 60+ stocks every minute for >=3% spikes. Alerts include Grok AI
analysis, live technicals (RSI, Bollinger Bands, squeeze score), and
latest news. Dashboards auto-send at pre-market, open, mid-day, and close.

── MARKET PULSE ──────────────────────────
/overview      — Indices + sectors + Fear & Greed + Grok read
/crypto        — BTC/ETH/SOL/DOGE/XRP live prices + AI outlook
/macro         — Upcoming macro events (CPI, Fed, jobs, FOMC)
/earnings      — Earnings calendar for next 7 days

── MOVERS ────────────────────────────────
/movers        — gainers · losers · volume · lowprice
e.g. /movers gainers   /movers volume

── STOCK TOOLS ───────────────────────────
/price TICK    — Quick price, day range, volume
/analyze TICK  — Deep AI: technicals + catalyst + risk
/compare A B   — Side-by-side with Grok verdict
/chart TICK    — Intraday sparkline + volume bars
/rsi TICK      — RSI(14) + Bollinger Bands breakdown
/news TICK     — Latest headlines

── ALERTS & SCANNING ─────────────────────
/spikes        — Spikes in last 30 min
/alerts        — All alerts fired today
/squeeze       — Top squeeze candidates (scored 0-100)
/setalert      — Custom price target alert
/watchlist     — Personal watchlist (add/remove/scan)

── BOT CONTROL ───────────────────────────
/dashboard     — Full visual dashboard image now
/list          — All monitored tickers
/monitoring    — pause · resume · status
e.g. /monitoring pause
/help          — This menu

Type any question in plain English — Grok AI will answer it.
Dashboards auto-send: 8:00 AM pre-market, 8:30 AM open,
12:00 PM mid-day, 3:00 PM close CT. Weekly digest: Sunday 6 PM.
“””

# ============================================================

# STATE

# ============================================================

CORE_TICKERS = [
“NVDA”,“TSLA”,“AMD”,“AAPL”,“AMZN”,“META”,“MSFT”,“GOOGL”,“SMCI”,“ARM”,
“MU”,“AVGO”,“QCOM”,“INTC”,“HIMS”,“PLTR”,“SOFI”,“RIVN”,“NIO”,“MARA”,
“AMC”,“GME”,“LCID”,“BYND”,“PFE”,“BAC”,“JPM”,“XOM”,“CVX”,“AAL”
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
parts, current = [], “”
for line in text.splitlines(keepends=True):
if len(current) + len(line) > 3800:
if current:
parts.append(current.rstrip())
current = line
else:
current += line
if current:
parts.append(current.rstrip())

```
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
```

# ============================================================

# GROK HELPERS — with exponential backoff

# ============================================================

def get_grok_response(prompt, system=None, max_tokens=300):
if not grok_client:
return “AI unavailable (no GROK_API_KEY)”
sys_msg = system or (
“You are a sharp, concise stock market analyst. “
“Give direct, data-driven insights. No fluff. Max 3 sentences unless asked for more.”
)
for attempt in range(4):
try:
resp = grok_client.chat.completions.create(
model=GROK_MODEL,
messages=[
{“role”: “system”, “content”: sys_msg},
{“role”: “user”,   “content”: prompt}
],
max_tokens=max_tokens,
temperature=0.4
)
return resp.choices[0].message.content.strip()
except Exception as e:
wait = 2 ** attempt
logger.error(f”Grok error (attempt {attempt+1}): {e}. Retry in {wait}s”)
time.sleep(wait)
return “Grok unavailable”

def get_grok_conversation(chat_id, user_message):
“”“Multi-turn conversational Grok with memory.”””
if not grok_client:
return “AI unavailable (no GROK_API_KEY)”
history = conversation_history.setdefault(chat_id, [])
history.append({“role”: “user”, “content”: user_message})
# Keep last 10 turns
if len(history) > 20:
history = history[-20:]
conversation_history[chat_id] = history

```
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
```

# ============================================================

# MARKET DATA HELPERS

# ============================================================

def fetch_finnhub_quote(ticker):
try:
r = requests.get(
f”https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_TOKEN}”,
timeout=10
)
data = r.json()
return data.get(‘c’), data.get(‘v’), data.get(‘pc’)  # current, volume, prev close
except:
return None, None, None

def fetch_latest_news(ticker, count=3):
try:
today     = datetime.now().date()
yesterday = today - timedelta(days=2)
r = requests.get(
f”https://finnhub.io/api/v1/company-news?symbol={ticker}”
f”&from={yesterday}&to={today}&token={FINNHUB_TOKEN}”,
timeout=10
)
news = r.json()[:count]
return [(item.get(‘headline’,’’), item.get(‘url’,’’)) for item in news]
except:
return []

def get_trading_session():
now     = datetime.now(CT)
if now.weekday() > 4:
return “closed”
current = now.time()
if datetime.strptime(“07:00”, “%H:%M”).time() <= current < datetime.strptime(“20:00”, “%H:%M”).time():
return “regular” if datetime.strptime(“08:30”, “%H:%M”).time() <= current < datetime.strptime(“15:00”, “%H:%M”).time() else “extended”
return “closed”

def get_yf_info(ticker):
try:
return yf.Ticker(ticker).fast_info
except:
return None

# ============================================================

# TECHNICALS — RSI (Wilder), Bollinger Bands, Squeeze Score

# ============================================================

def compute_rsi(prices: list, period: int = 14):
“””
Wilder’s Smoothed RSI from a list of closing prices.
Returns None if insufficient data.
“””
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
“””
Returns (middle, upper, lower, pct_b, bandwidth).
pct_b = (price - lower) / (upper - lower)  — 0=at lower, 1=at upper
bandwidth = (upper - lower) / middle        — squeeze proxy (lower=tighter)
“””
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
“””
Squeeze score 0–100 combining:
RSI distance from 40 (building momentum)  — up to 30 pts
Bollinger bandwidth squeeze (low = tight)  — up to 25 pts
%B near lower band (coiled spring)         — up to 20 pts
Volume trend (rising vs prior scans)       — up to 15 pts
Short interest ratio (Finnhub)             — up to 10 pts
Higher score = more squeeze-ready.
“””
hist_raw = list(price_history.get(ticker, deque()))
if not hist_raw:
return {“score”: 0, “rsi”: None, “pct_b”: None, “bandwidth”: None, “components”: {}}

```
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
```

def get_fear_greed():
try:
r = requests.get(“https://api.alternative.me/fng/?limit=1”, timeout=10)
d = r.json()[‘data’][0]
return d.get(‘value’), d.get(‘value_classification’)
except:
return None, None

def get_sector_performance():
sectors = {
“XLK”: “Technology”, “XLF”: “Financials”, “XLE”: “Energy”,
“XLV”: “Health Care”, “XLI”: “Industrials”, “XLC”: “Comm Services”,
“XLY”: “Cons Discret”, “XLP”: “Cons Staples”, “XLB”: “Materials”,
“XLRE”: “Real Estate”, “XLU”: “Utilities”
}
lines = []
for sym, name in sectors.items():
try:
info = yf.Ticker(sym).fast_info
chg  = info.get(‘regularMarketChangePercent’, 0)
arrow = “▲” if chg >= 0 else “▼”
lines.append(f”{arrow} {name}: {chg:+.2f}%”)
except:
pass
return lines

def get_crypto_prices():
coins = [“BTC-USD”,“ETH-USD”,“SOL-USD”,“DOGE-USD”,“XRP-USD”]
lines = []
for coin in coins:
try:
info = yf.Ticker(coin).fast_info
price = info.get(‘lastPrice’, 0)
chg   = info.get(‘regularMarketChangePercent’, 0)
name  = coin.replace(”-USD”,””)
arrow = “▲” if chg >= 0 else “▼”
lines.append(f”{arrow} {name}: ${price:,.2f} ({chg:+.2f}%)”)
except:
pass
return lines

# ============================================================

# DYNAMIC BULLISH LIST

# ============================================================

def get_dynamic_hot_stocks():
logger.info(“Fetching dynamic BULLISH candidates…”)
candidates, low_price = [], []
try:
r = requests.get(
f”https://financialmodelingprep.com/api/v3/stock_market/actives?apikey={FMP_API_KEY}”,
timeout=10
)
data = r.json()
if isinstance(data, list):
candidates.extend([item.get(‘symbol’) for item in data[:30] if isinstance(item, dict)])

```
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
```

TICKERS = get_dynamic_hot_stocks()

# ============================================================

# ALERT ENGINE

# ============================================================

def send_alert(ticker, pct_change, current_price, volume_spike=False):
global daily_alerts
daily_alerts += 1
news_items   = fetch_latest_news(ticker)
spike_label  = “VOLUME+PRICE SPIKE” if volume_spike else “SPIKE”

```
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
```

def check_custom_price_alerts(ticker, current_price):
if ticker not in custom_price_alerts:
return
triggered = []
for target in custom_price_alerts[ticker]:
if abs(current_price - target) / target < 0.005:   # within 0.5%
send_telegram(
f”🎯 Price Alert Hit!\n{ticker} reached ${current_price:.2f}\n(Target: ${target:.2f})”
)
triggered.append(target)
for t in triggered:
custom_price_alerts[ticker].remove(t)

def _scan_ticker(ticker: str, now: datetime):
“”“Scan a single ticker — runs in thread pool.”””
c, vol, pc = fetch_finnhub_quote(ticker)
if not c or c < MIN_PRICE:
return

```
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
```

def check_stocks():
if monitoring_paused or get_trading_session() == “closed”:
return
now = datetime.now(CT)
logger.info(f”Scanning {len(TICKERS)} stocks (concurrent)…”)

```
with ThreadPoolExecutor(max_workers=min(32, len(TICKERS))) as pool:
    futures = {pool.submit(_scan_ticker, t, now): t for t in TICKERS}
    for future in as_completed(futures):
        t = futures[future]
        try:
            future.result()
        except Exception as e:
            logger.error(f"Scan error for {t}: {e}")
```

# ============================================================

# TELEGRAM COMMANDS

# ============================================================

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text(BOT_DESCRIPTION)

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text(
f”Monitoring {len(TICKERS)} stocks:\n” + “  “.join(sorted(TICKERS))
)

async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not recent_alerts:
await update.message.reply_text(“No alerts fired yet today.”)
return
await update.message.reply_text(
f”Today’s alerts ({daily_alerts} total):\n” +
“\n”.join(recent_alerts[-20:])
)

async def cmd_overview(update: Update, context: ContextTypes.DEFAULT_TYPE):
“”“Indices + sectors + Fear & Greed in one shot.”””
indices = {
“^GSPC”: “S&P 500”, “^IXIC”: “Nasdaq”,
“^DJI”:  “Dow”,     “^RUT”:  “Russell 2000”,
“^VIX”:  “VIX”
}
idx_lines = []
for sym, name in indices.items():
try:
info  = yf.Ticker(sym).fast_info
price = info.get(‘lastPrice’, 0)
chg   = info.get(‘regularMarketChangePercent’, 0)
arrow = “▲” if chg >= 0 else “▼”
idx_lines.append(f”{arrow} {name}: {price:,.2f} ({chg:+.2f}%)”)
except:
pass

```
sec_lines = get_sector_performance()
fg_val, fg_label = get_fear_greed()
fg_str = f"{fg_val} — {fg_label}" if fg_val else "unavailable"

summary = " | ".join(idx_lines[:4]) + f" | F&G {fg_val}"
ai = get_grok_response(
    f"Market snapshot: {summary}. Top sectors: {', '.join(sec_lines[:3])}. "
    f"2-sentence outlook + one sector to watch."
)
await update.message.reply_text(
    "Indices:\n" + "\n".join(idx_lines) +
    f"\n\nFear & Greed: {fg_str}" +
    "\n\nSectors:\n" + "\n".join(sec_lines) +
    f"\n\nGrok: {ai}"
)
```

async def cmd_spikes(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not recent_alerts:
await update.message.reply_text(“No spikes in the last 30 minutes.”)
return
await update.message.reply_text(
“Recent spikes:\n” + “\n”.join(recent_alerts[-10:])
)

async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not context.args:
await update.message.reply_text(“Usage: /analyze TICKER (e.g. /analyze NVDA)”)
return
ticker = context.args[0].upper()
await update.message.reply_text(f”Analyzing {ticker}…”)
try:
info  = yf.Ticker(ticker).fast_info
price = info.get(‘lastPrice’, 0)
chg   = info.get(‘regularMarketChangePercent’, 0)
mcap  = info.get(‘marketCap’, 0) / 1e9
vol   = info.get(‘lastVolume’, 0)
high52 = info.get(‘fiftyTwoWeekHigh’, 0)
low52  = info.get(‘fiftyTwoWeekLow’, 0)
pct_from_high = ((price - high52) / high52 * 100) if high52 else 0

```
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
```

async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not context.args:
await update.message.reply_text(“Usage: /price TICKER (e.g. /price AAPL)”)
return
ticker = context.args[0].upper()
try:
info   = yf.Ticker(ticker).fast_info
price  = info.get(‘lastPrice’, 0)
chg    = info.get(‘regularMarketChangePercent’, 0)
chg_abs = info.get(‘regularMarketChange’, 0)
vol    = info.get(‘lastVolume’, 0)
high   = info.get(‘dayHigh’, 0)
low    = info.get(‘dayLow’, 0)
arrow  = “▲” if chg >= 0 else “▼”
await update.message.reply_text(
f”{arrow} {ticker}: ${price:.2f}\n”
f”Change: {chg_abs:+.2f} ({chg:+.2f}%)\n”
f”Day: ${low:.2f} – ${high:.2f}\n”
f”Volume: {vol:,}”
)
except Exception as e:
await update.message.reply_text(f”Could not fetch {ticker}: {e}”)

async def cmd_compare(update: Update, context: ContextTypes.DEFAULT_TYPE):
if len(context.args) < 2:
await update.message.reply_text(“Usage: /compare TICKER1 TICKER2 (e.g. /compare NVDA AMD)”)
return
t1, t2 = context.args[0].upper(), context.args[1].upper()
try:
rows = []
stats = {}
for t in [t1, t2]:
info = yf.Ticker(t).fast_info
stats[t] = {
“price”: info.get(‘lastPrice’, 0),
“chg”:   info.get(‘regularMarketChangePercent’, 0),
“mcap”:  info.get(‘marketCap’, 0) / 1e9,
“high52”: info.get(‘fiftyTwoWeekHigh’, 0),
“low52”:  info.get(‘fiftyTwoWeekLow’, 0),
}
def fmt(val, fmt_str):
return fmt_str.format(val)

```
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
```

async def cmd_movers(update: Update, context: ContextTypes.DEFAULT_TYPE):
“””
/movers [gainers|losers|volume|lowprice]
Defaults to showing a compact summary of all four if no arg.
“””
mode = context.args[0].lower() if context.args else “all”

```
async def _gainers():
    try:
        df = pd.read_html("https://finance.yahoo.com/screener/predefined/day_gainers")[0]
        return "Top Gainers:\n" + "\n".join(
            f"• {r['Symbol']} +{r['% Change']:.1f}%"
            for _, r in df.head(5).iterrows()
        )
    except:
        return "Gainers unavailable."

async def _losers():
    try:
        df = pd.read_html("https://finance.yahoo.com/screener/predefined/day_losers")[0]
        return "Top Losers:\n" + "\n".join(
            f"• {r['Symbol']} {r['% Change']:.1f}%"
            for _, r in df.head(5).iterrows()
        )
    except:
        return "Losers unavailable."

async def _volume():
    try:
        df = pd.read_html("https://finance.yahoo.com/screener/predefined/most_actives")[0]
        return "Most Active:\n" + "\n".join(
            f"• {r['Symbol']}" for _, r in df.head(8).iterrows()
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
            f"• {r['Symbol']} +{r['% Change']:.1f}%"
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
```

async def cmd_earnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
try:
today = datetime.now().date()
end   = today + timedelta(days=7)
r = requests.get(
f”https://financialmodelingprep.com/api/v3/earning_calendar”
f”?from={today}&to={end}&apikey={FMP_API_KEY}”,
timeout=10
)
data = r.json()
if not isinstance(data, list) or not data:
await update.message.reply_text(“No upcoming earnings found.”)
return
lines = [“Upcoming Earnings (7 days):”]
for item in data[:15]:
sym  = item.get(‘symbol’,’’)
date = item.get(‘date’,’’)
eps  = item.get(‘epsEstimated’,’?’)
lines.append(f”• {sym} on {date} (EPS est: {eps})”)
await update.message.reply_text(”\n”.join(lines))
except Exception as e:
await update.message.reply_text(f”Unable to fetch earnings: {e}”)

async def cmd_macro(update: Update, context: ContextTypes.DEFAULT_TYPE):
“””
Upcoming macro events via FMP economic calendar.
Falls back to a hardcoded awareness list if API unavailable.
“””
try:
today = datetime.now().date()
end   = today + timedelta(days=14)
r = requests.get(
f”https://financialmodelingprep.com/api/v3/economic_calendar”
f”?from={today}&to={end}&apikey={FMP_API_KEY}”,
timeout=10
)
data = r.json()
# Filter to high-impact events only
HIGH_IMPACT = [“CPI”,“PPI”,“GDP”,“NFP”,“Nonfarm”,“FOMC”,“Fed”,“Unemployment”,
“Retail Sales”,“PCE”,“ISM”,“PMI”,“Housing”,“Consumer Confidence”]
events = []
if isinstance(data, list):
for item in data:
name   = item.get(‘event’,’’)
impact = item.get(‘impact’,’’)
if impact in (‘High’,‘Medium’) or any(k.lower() in name.lower() for k in HIGH_IMPACT):
events.append({
“date”:    item.get(‘date’,’’)[:10],
“event”:   name,
“impact”:  impact,
“actual”:  item.get(‘actual’,’’),
“est”:     item.get(‘estimate’,’’),
})
events = events[:15]

```
    if not events:
        raise ValueError("no events returned")

    lines = ["Macro Calendar (14 days):"]
    for e in events:
        impact_tag = "[HIGH]" if e['impact'] == 'High' else "[MED] "
        actual_str = f"  actual={e['actual']}" if e['actual'] else ""
        est_str    = f"  est={e['est']}"       if e['est']    else ""
        lines.append(f"{impact_tag} {e['date']}  {e['event']}{est_str}{actual_str}")

    event_names = ", ".join([e['event'] for e in events[:4]])
    ai = get_grok_response(
        f"Upcoming macro events: {event_names}. "
        f"Which one is most market-moving right now and why? One sentence."
    )
    await update.message.reply_text("\n".join(lines) + f"\n\nGrok: {ai}")

except Exception as e:
    logger.warning(f"Macro calendar fetch failed: {e}")
    # Graceful fallback — static awareness message
    ai = get_grok_response(
        "What are the key macro events traders should watch this week "
        "(CPI, FOMC, jobs, etc.)? Be specific about timing."
    )
    await update.message.reply_text(f"Macro Events (Grok):\n{ai}")
```

async def cmd_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
lines = get_crypto_prices()
if not lines:
await update.message.reply_text(“Unable to fetch crypto prices.”)
return
summary = “ | “.join(lines[:3])
ai = get_grok_response(f”Crypto snapshot: {summary}. One-sentence crypto market outlook.”)
await update.message.reply_text(
“Crypto Prices:\n” + “\n”.join(lines) + f”\n\nGrok: {ai}”
)

async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not context.args:
await update.message.reply_text(“Usage: /news TICKER (e.g. /news NVDA)”)
return
ticker     = context.args[0].upper()
news_items = fetch_latest_news(ticker, 5)
if not news_items:
await update.message.reply_text(f”No recent news for {ticker}.”)
return
lines = [f”Latest news for {ticker}:”]
for headline, url in news_items:
lines.append(f”• {headline[:100]}”)
if url:
lines.append(f”  {url}”)
await update.message.reply_text(”\n”.join(lines))

async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
cid = str(update.effective_chat.id)
if not context.args:
wl = user_watchlists.get(cid, [])
if not wl:
await update.message.reply_text(
“Your watchlist is empty.\n”
“Usage:\n”
“/watchlist add TICKER\n”
“/watchlist remove TICKER\n”
“/watchlist show\n”
“/watchlist scan  (spike-scan your list now)”
)
return
await update.message.reply_text(“Your watchlist:\n” + “  “.join(wl))
return

```
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
```

async def cmd_setalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
if len(context.args) < 2:
await update.message.reply_text(
“Usage: /setalert TICKER PRICE\n”
“Example: /setalert NVDA 150.00\n\n”
“You’ll be notified when the stock is within 0.5% of your target.”
)
return
ticker = context.args[0].upper()
try:
target = float(context.args[1])
except:
await update.message.reply_text(“Invalid price. Example: /setalert NVDA 150.00”)
return
custom_price_alerts.setdefault(ticker, []).append(target)
await update.message.reply_text(
f”Price alert set!\n{ticker} @ ${target:.2f}\nYou’ll be alerted when within 0.5% of this target.”
)

async def cmd_squeeze(update: Update, context: ContextTypes.DEFAULT_TYPE):
“”“Show top squeeze candidates ranked by squeeze score.”””
if not squeeze_scores:
await update.message.reply_text(
“No squeeze data yet — scores build up after a few scan cycles.\n”
“Try again in 2–3 minutes.”
)
return

```
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
```

async def cmd_rsi(update: Update, context: ContextTypes.DEFAULT_TYPE):
“”“Show RSI + Bollinger Bands for a specific ticker.”””
if not context.args:
await update.message.reply_text(“Usage: /rsi TICKER (e.g. /rsi NVDA)”)
return
ticker = context.args[0].upper()

```
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
```

# ============================================================

# CHART — intraday sparkline with volume bars

# ============================================================

def build_chart_image(ticker: str) -> BytesIO:
BG = “#0d1117”; PANEL = “#161b22”; TEXT = “#e6edf3”
DIM = “#8b949e”; GREEN = “#2ecc71”; RED = “#e74c3c”; GOLD = “#f0b429”

```
hist = yf.Ticker(ticker).history(period="1d", interval="5m")
if hist.empty:
    raise ValueError(f"No intraday data for {ticker}")

prices  = hist['Close'].tolist()
volumes = hist['Volume'].tolist()
times   = [t.strftime("%H:%M") for t in hist.index]
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
```

async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
“”“Send intraday sparkline + volume chart for a ticker.”””
if not context.args:
await update.message.reply_text(“Usage: /chart TICKER  (e.g. /chart NVDA)”)
return
ticker = context.args[0].upper()
await update.message.reply_text(f”Fetching intraday chart for {ticker}…”)
try:
import asyncio
loop = asyncio.get_event_loop()
buf  = await loop.run_in_executor(None, build_chart_image, ticker)

```
    # Quick Grok read on the chart shape
    info    = yf.Ticker(ticker).fast_info
    price   = info.get('lastPrice', 0)
    chg     = info.get('regularMarketChangePercent', 0)
    sq      = compute_squeeze_score(ticker)
    rsi_str = f"RSI {sq['rsi']:.0f}" if sq.get('rsi') else ""
    ai = get_grok_response(
        f"{ticker} intraday: ${price:.2f} ({chg:+.2f}% today). "
        f"{rsi_str}. What does this intraday move suggest? One sentence."
    )
    await update.message.reply_photo(
        photo=buf,
        caption=f"{ticker}  ${price:.2f}  ({chg:+.2f}%)\n{rsi_str}\nGrok: {ai}"
    )
except Exception as e:
    logger.error(f"Chart error for {ticker}: {e}")
    await update.message.reply_text(f"Could not generate chart for {ticker}: {e}")
```

# ============================================================

# DASHBOARD — visual market snapshot image

# ============================================================

def _clamp_color(val, lo, hi):
“”“Map val in [lo,hi] to 0–1 for a red-white-green colormap.”””
span = hi - lo
if span == 0:
return 0.5
return max(0.0, min(1.0, (val - lo) / span))

def _rg_cmap():
return LinearSegmentedColormap.from_list(
“rg”, [”#e74c3c”, “#f5f5f5”, “#2ecc71”]
)

def _bar_color(val):
“”“Green for positive, red for negative.”””
return “#2ecc71” if val >= 0 else “#e74c3c”

def build_dashboard_image() -> BytesIO:
“””
Fetches live data across all bot dimensions and renders a
multi-panel PNG dashboard. Returns a BytesIO object.

```
Panels:
  [A] Major indices bar chart
  [B] Fear & Greed gauge
  [C] Sector heatmap
  [D] Top gainers / losers from monitored list
  [E] Squeeze leaderboard
  [F] Crypto prices
  [G] Recent spike alerts ticker
  [H] Grok AI one-liner sentiment
"""

BG    = "#0d1117"
PANEL = "#161b22"
TEXT  = "#e6edf3"
DIM   = "#8b949e"
GREEN = "#2ecc71"
RED   = "#e74c3c"
GOLD  = "#f0b429"
BLUE  = "#58a6ff"

now_str = datetime.now(CT).strftime("%a %b %d %Y  %I:%M %p CT")
session = get_trading_session()
session_color = GREEN if session == "regular" else GOLD if session == "extended" else RED

# ── Fetch all data concurrently ───────────────────────────
def _fetch_indices():
    syms = {"^GSPC":"S&P 500","^IXIC":"Nasdaq","^DJI":"Dow",
            "^RUT":"Russell","^VIX":"VIX"}
    out = {}
    for sym, name in syms.items():
        try:
            info = yf.Ticker(sym).fast_info
            out[name] = {
                "price": info.get("lastPrice", 0),
                "chg":   info.get("regularMarketChangePercent", 0),
            }
        except:
            out[name] = {"price": 0, "chg": 0}
    return out

def _fetch_sectors():
    sectors = {
        "XLK":"Tech","XLF":"Fin","XLE":"Energy","XLV":"Health",
        "XLI":"Indust","XLC":"Comm","XLY":"Cons D","XLP":"Cons S",
        "XLB":"Mat","XLRE":"RE","XLU":"Util"
    }
    out = {}
    for sym, name in sectors.items():
        try:
            chg = yf.Ticker(sym).fast_info.get("regularMarketChangePercent", 0)
            out[name] = round(chg, 2)
        except:
            out[name] = 0.0
    return out

def _fetch_movers():
    items = []
    for t in TICKERS:
        try:
            info = yf.Ticker(t).fast_info
            chg  = info.get("regularMarketChangePercent", 0)
            price = info.get("lastPrice", 0)
            if price > 0:
                items.append((t, chg, price))
        except:
            pass
    items.sort(key=lambda x: x[1])
    losers  = items[:5]
    gainers = items[-5:][::-1]
    return gainers, losers

def _fetch_crypto():
    coins = [("BTC-USD","BTC"),("ETH-USD","ETH"),
             ("SOL-USD","SOL"),("DOGE-USD","DOGE"),("XRP-USD","XRP")]
    out = []
    for sym, name in coins:
        try:
            info  = yf.Ticker(sym).fast_info
            price = info.get("lastPrice", 0)
            chg   = info.get("regularMarketChangePercent", 0)
            out.append((name, price, chg))
        except:
            pass
    return out

with ThreadPoolExecutor(max_workers=4) as pool:
    f_idx     = pool.submit(_fetch_indices)
    f_sec     = pool.submit(_fetch_sectors)
    f_mov     = pool.submit(_fetch_movers)
    f_cry     = pool.submit(_fetch_crypto)
    f_fg      = pool.submit(get_fear_greed)

indices  = f_idx.result()
sectors  = f_sec.result()
gainers, losers = f_mov.result()
crypto   = f_cry.result()
fg_val, fg_label = f_fg.result()
fg_val   = int(fg_val) if fg_val else 50

# Squeeze top 5
top_squeeze = sorted(squeeze_scores.items(), key=lambda x: x[1], reverse=True)[:5]

# Grok one-liner
idx_summary = "  ".join(
    [f"{n} {d['chg']:+.1f}%" for n, d in list(indices.items())[:4]]
)
grok_line = get_grok_response(
    f"Market now: {idx_summary}. Fear&Greed={fg_val}({fg_label}). "
    f"One sentence market call.",
    max_tokens=80
)

# ── Layout ────────────────────────────────────────────────
fig = plt.figure(figsize=(20, 14), facecolor=BG)
fig.patch.set_facecolor(BG)

gs = gridspec.GridSpec(
    4, 4,
    figure=fig,
    hspace=0.55,
    wspace=0.35,
    top=0.90, bottom=0.05,
    left=0.04, right=0.97
)

def panel(ax, title):
    ax.set_facecolor(PANEL)
    for spine in ax.spines.values():
        spine.set_edgecolor("#30363d")
        spine.set_linewidth(0.8)
    ax.set_title(title, color=DIM, fontsize=9,
                 fontweight="bold", loc="left", pad=6)
    ax.tick_params(colors=TEXT, labelsize=8)

# ── Header ────────────────────────────────────────────────
fig.text(0.04, 0.955, "STOCK SPIKE MONITOR  //  LIVE DASHBOARD",
         color=TEXT, fontsize=15, fontweight="bold")
fig.text(0.04, 0.930, now_str, color=DIM, fontsize=9)
fig.text(0.30, 0.930,
         f"Market: {session.upper()}",
         color=session_color, fontsize=9, fontweight="bold")
fig.text(0.04, 0.915,
         f"Grok AI: {grok_line}",
         color=GOLD, fontsize=8.5, style="italic",
         wrap=True)

# ── [A] Indices bar chart ─────────────────────────────────
ax_idx = fig.add_subplot(gs[0, :2])
panel(ax_idx, "MAJOR INDICES  (% change)")
names  = list(indices.keys())
chgs   = [indices[n]["chg"] for n in names]
colors = [_bar_color(c) for c in chgs]
bars   = ax_idx.barh(names, chgs, color=colors, height=0.55, zorder=3)
ax_idx.axvline(0, color=DIM, linewidth=0.7, zorder=2)
ax_idx.set_facecolor(PANEL)
ax_idx.xaxis.grid(True, color="#21262d", linewidth=0.5, zorder=1)
ax_idx.set_axisbelow(True)
for bar, chg, name in zip(bars, chgs, names):
    price = indices[name]["price"]
    label = f" {chg:+.2f}%  ${price:,.2f}" if name != "VIX" else f" {chg:+.2f}%  {price:.2f}"
    ax_idx.text(chg + (0.05 if chg >= 0 else -0.05),
                bar.get_y() + bar.get_height() / 2,
                label, va="center",
                ha="left" if chg >= 0 else "right",
                color=TEXT, fontsize=8)
ax_idx.tick_params(axis="y", colors=TEXT, labelsize=9)
ax_idx.tick_params(axis="x", colors=DIM,  labelsize=7)

# ── [B] Fear & Greed gauge ────────────────────────────────
ax_fg = fig.add_subplot(gs[0, 2])
ax_fg.set_facecolor(PANEL)
for spine in ax_fg.spines.values():
    spine.set_edgecolor("#30363d")
ax_fg.set_title("FEAR & GREED", color=DIM, fontsize=9, fontweight="bold", loc="left", pad=6)
ax_fg.set_aspect("equal")
ax_fg.set_xlim(-1.3, 1.3)
ax_fg.set_ylim(-0.3, 1.3)
ax_fg.axis("off")

import numpy as np
# Arc background segments: Extreme Fear → Greed
seg_colors = ["#c0392b","#e74c3c","#e67e22","#f1c40f","#2ecc71","#27ae60"]
seg_labels = ["Ext\nFear","Fear","Neutral","Greed","Ext\nGreed",""]
for i, (sc, sl) in enumerate(zip(seg_colors, seg_labels)):
    theta1 = 180 - i * 30
    theta2 = 180 - (i + 1) * 30
    theta  = np.linspace(np.radians(theta2), np.radians(theta1), 50)
    x_out  = np.cos(theta)
    y_out  = np.sin(theta)
    x_in   = 0.65 * np.cos(theta)
    y_in   = 0.65 * np.sin(theta)
    xs = np.concatenate([x_out, x_in[::-1]])
    ys = np.concatenate([y_out, y_in[::-1]])
    ax_fg.fill(xs, ys, color=sc, alpha=0.85, zorder=2)
    mid_theta = np.radians((theta1 + theta2) / 2)
    if sl:
        ax_fg.text(0.82 * np.cos(mid_theta), 0.82 * np.sin(mid_theta),
                   sl, ha="center", va="center", fontsize=5.5,
                   color="white", fontweight="bold", zorder=3)

# Needle
needle_angle = np.radians(180 - fg_val * 1.8)
ax_fg.annotate("",
    xy=(0.6 * np.cos(needle_angle), 0.6 * np.sin(needle_angle)),
    xytext=(0, 0),
    arrowprops=dict(arrowstyle="->, head_width=0.08, head_length=0.05",
                    color="white", lw=2),
    zorder=5
)
ax_fg.add_patch(plt.Circle((0, 0), 0.07, color=PANEL, zorder=4))

# Score + label
ax_fg.text(0, -0.18, str(fg_val), ha="center", va="center",
           fontsize=22, fontweight="bold", color=TEXT, zorder=5)
ax_fg.text(0, -0.28, fg_label or "", ha="center", va="center",
           fontsize=7.5, color=GOLD, zorder=5)

# ── [C] Sector heatmap ────────────────────────────────────
ax_sec = fig.add_subplot(gs[0, 3])
panel(ax_sec, "SECTOR HEATMAP")
ax_sec.axis("off")
sec_names = list(sectors.keys())
sec_vals  = list(sectors.values())
max_abs   = max(abs(v) for v in sec_vals) or 1
ncols, nrows = 3, 4
cmap = _rg_cmap()
for idx, (name, val) in enumerate(zip(sec_names, sec_vals)):
    row = idx // ncols
    col = idx % ncols
    cx  = col / ncols + 0.5 / ncols
    cy  = 1 - row / nrows - 0.5 / nrows
    norm_val = _clamp_color(val, -max_abs, max_abs)
    bg_color = cmap(norm_val)
    rect = FancyBboxPatch(
        (col / ncols + 0.01, 1 - (row + 1) / nrows + 0.01),
        1 / ncols - 0.02, 1 / nrows - 0.02,
        boxstyle="round,pad=0.01", facecolor=bg_color,
        edgecolor="#0d1117", linewidth=1, transform=ax_sec.transAxes
    )
    ax_sec.add_patch(rect)
    txt_color = "white" if abs(norm_val - 0.5) > 0.2 else "#0d1117"
    ax_sec.text(cx, cy + 0.05, name, ha="center", va="center",
                fontsize=7, fontweight="bold", color=txt_color,
                transform=ax_sec.transAxes)
    ax_sec.text(cx, cy - 0.05, f"{val:+.2f}%", ha="center", va="center",
                fontsize=6.5, color=txt_color, transform=ax_sec.transAxes)

# ── [D] Top Gainers ───────────────────────────────────────
ax_gn = fig.add_subplot(gs[1, :2])
panel(ax_gn, "TOP GAINERS  (monitored list)")
if gainers:
    g_names = [t for t, _, _ in gainers]
    g_vals  = [c for _, c, _ in gainers]
    g_bars  = ax_gn.barh(g_names, g_vals, color=GREEN, height=0.55, zorder=3)
    ax_gn.set_facecolor(PANEL)
    ax_gn.xaxis.grid(True, color="#21262d", linewidth=0.5, zorder=1)
    ax_gn.set_axisbelow(True)
    ax_gn.axvline(0, color=DIM, linewidth=0.7)
    for bar, (t, chg, price) in zip(g_bars, gainers):
        ax_gn.text(chg + 0.05, bar.get_y() + bar.get_height() / 2,
                   f" +{chg:.2f}%  ${price:.2f}",
                   va="center", color=TEXT, fontsize=8)
    ax_gn.tick_params(axis="y", colors=TEXT, labelsize=9)
    ax_gn.tick_params(axis="x", colors=DIM, labelsize=7)

# ── [E] Top Losers ────────────────────────────────────────
ax_ls = fig.add_subplot(gs[1, 2:])
panel(ax_ls, "TOP LOSERS  (monitored list)")
if losers:
    l_names = [t for t, _, _ in losers]
    l_vals  = [c for _, c, _ in losers]
    l_bars  = ax_ls.barh(l_names, l_vals, color=RED, height=0.55, zorder=3)
    ax_ls.set_facecolor(PANEL)
    ax_ls.xaxis.grid(True, color="#21262d", linewidth=0.5, zorder=1)
    ax_ls.set_axisbelow(True)
    ax_ls.axvline(0, color=DIM, linewidth=0.7)
    for bar, (t, chg, price) in zip(l_bars, losers):
        ax_ls.text(chg - 0.05, bar.get_y() + bar.get_height() / 2,
                   f"{chg:.2f}%  ${price:.2f}  ",
                   va="center", ha="right", color=TEXT, fontsize=8)
    ax_ls.tick_params(axis="y", colors=TEXT, labelsize=9)
    ax_ls.tick_params(axis="x", colors=DIM, labelsize=7)

# ── [F] Squeeze Leaderboard ───────────────────────────────
ax_sq = fig.add_subplot(gs[2, :2])
panel(ax_sq, "SQUEEZE LEADERBOARD  (score 0–100)")
if top_squeeze:
    sq_names  = [t for t, _ in top_squeeze]
    sq_scores = [s for _, s in top_squeeze]
    sq_colors = [plt.cm.YlOrRd(s / 100) for s in sq_scores]
    sq_bars   = ax_sq.barh(sq_names, sq_scores, color=sq_colors, height=0.55, zorder=3)
    ax_sq.set_xlim(0, 105)
    ax_sq.set_facecolor(PANEL)
    ax_sq.xaxis.grid(True, color="#21262d", linewidth=0.5, zorder=1)
    ax_sq.set_axisbelow(True)
    for bar, (t, score) in zip(sq_bars, top_squeeze):
        sq_data = compute_squeeze_score(t)
        rsi_s   = f"RSI {sq_data['rsi']:.0f}" if sq_data.get("rsi") else ""
        bw_s    = f"BW {sq_data['bandwidth']:.3f}" if sq_data.get("bandwidth") else ""
        detail  = "  ".join(filter(None, [rsi_s, bw_s]))
        ax_sq.text(score + 1, bar.get_y() + bar.get_height() / 2,
                   f" {score:.0f}  {detail}",
                   va="center", color=TEXT, fontsize=8)
    ax_sq.tick_params(axis="y", colors=TEXT, labelsize=9)
    ax_sq.tick_params(axis="x", colors=DIM, labelsize=7)
else:
    ax_sq.text(0.5, 0.5, "Building… (needs 2–3 scan cycles)",
               ha="center", va="center", color=DIM, fontsize=9,
               transform=ax_sq.transAxes)
    ax_sq.axis("off")

# ── [G] Crypto ────────────────────────────────────────────
ax_cr = fig.add_subplot(gs[2, 2:])
panel(ax_cr, "CRYPTO  (% change today)")
if crypto:
    cr_names  = [c[0] for c in crypto]
    cr_chgs   = [c[2] for c in crypto]
    cr_prices = [c[1] for c in crypto]
    cr_colors = [_bar_color(v) for v in cr_chgs]
    cr_bars   = ax_cr.barh(cr_names, cr_chgs, color=cr_colors, height=0.55, zorder=3)
    ax_cr.axvline(0, color=DIM, linewidth=0.7)
    ax_cr.set_facecolor(PANEL)
    ax_cr.xaxis.grid(True, color="#21262d", linewidth=0.5, zorder=1)
    ax_cr.set_axisbelow(True)
    for bar, (name, price, chg) in zip(cr_bars, crypto):
        label = f"  {chg:+.2f}%   ${price:,.2f}" if price < 1000 else f"  {chg:+.2f}%  ${price:,.0f}"
        ax_cr.text(chg + (0.05 if chg >= 0 else -0.05),
                   bar.get_y() + bar.get_height() / 2,
                   label, va="center",
                   ha="left" if chg >= 0 else "right",
                   color=TEXT, fontsize=8)
    ax_cr.tick_params(axis="y", colors=TEXT, labelsize=9)
    ax_cr.tick_params(axis="x", colors=DIM, labelsize=7)

# ── [H] Recent Spike Alerts timeline ─────────────────────
ax_al = fig.add_subplot(gs[3, :3])
ax_al.set_facecolor(PANEL)
for spine in ax_al.spines.values():
    spine.set_edgecolor("#30363d")
ax_al.set_title("RECENT SPIKE ALERTS", color=DIM, fontsize=9,
                fontweight="bold", loc="left", pad=6)
ax_al.axis("off")
alerts_display = recent_alerts[-12:] if recent_alerts else ["No spikes yet today"]
ncols_al = 3
for idx, alert in enumerate(alerts_display):
    col = idx % ncols_al
    row = idx // ncols_al
    ax_al.text(col / ncols_al + 0.02,
               0.88 - row * 0.28,
               f">> {alert}",
               ha="left", va="top",
               color=GOLD if "%" in alert else DIM,
               fontsize=8.5,
               transform=ax_al.transAxes)

# ── [I] Bot Stats ─────────────────────────────────────────
ax_st = fig.add_subplot(gs[3, 3])
ax_st.set_facecolor(PANEL)
for spine in ax_st.spines.values():
    spine.set_edgecolor("#30363d")
ax_st.set_title("BOT STATUS", color=DIM, fontsize=9,
                fontweight="bold", loc="left", pad=6)
ax_st.axis("off")
status_str = "[RUNNING]" if not monitoring_paused else "[PAUSED]"
status_color = GREEN if not monitoring_paused else GOLD
stats_lines = [
    (status_str,  status_color),
    (f"Watching: {len(TICKERS)} stocks", TEXT),
    (f"Alerts today: {daily_alerts}", GOLD if daily_alerts > 0 else DIM),
    (f"Threshold: {THRESHOLD*100:.0f}% spike", DIM),
    (f"Scan: every {CHECK_INTERVAL_MIN} min", DIM),
    (f"Session: {session.upper()}", session_color),
]
for i, (line, color) in enumerate(stats_lines):
    ax_st.text(0.05, 0.92 - i * 0.16, line,
               ha="left", va="top", color=color,
               fontsize=8.5, transform=ax_st.transAxes)

# ── Save ──────────────────────────────────────────────────
buf = BytesIO()
plt.savefig(buf, format="png", dpi=130, bbox_inches="tight",
            facecolor=BG, edgecolor="none")
plt.close(fig)
buf.seek(0)
return buf
```

async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
“”“Generate and send the full visual market dashboard.”””
await update.message.reply_text(“Building dashboard… this takes ~10 seconds ⏳”)
try:
import asyncio
loop = asyncio.get_event_loop()
buf  = await loop.run_in_executor(None, build_dashboard_image)
await update.message.reply_photo(
photo=buf,
caption=(
f”📊 Live Dashboard — {datetime.now(CT).strftime(’%I:%M %p CT’)}\n”
f”Indices • Sectors • Gainers/Losers • Squeeze • Crypto • Alerts”
)
)
except Exception as e:
logger.error(f”Dashboard error: {e}”, exc_info=True)
await update.message.reply_text(f”Dashboard error: {e}”)

def send_dashboard_sync(label: str = “”):
“””
Build the dashboard and push it to Telegram using the raw Bot API.
Safe to call from any background thread or scheduler (no async needed).
“””
try:
buf = build_dashboard_image()
caption = f”📊 Dashboard — {label}  {datetime.now(CT).strftime(’%I:%M %p CT’)}”
resp = requests.post(
f”https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto”,
data={“chat_id”: CHAT_ID, “caption”: caption},
files={“photo”: (“dashboard.png”, buf, “image/png”)},
timeout=30
)
if not resp.ok:
logger.error(f”Dashboard send failed ({label}): {resp.text}”)
else:
logger.info(f”Dashboard sent: {label}”)
except Exception as e:
logger.error(f”send_dashboard_sync error ({label}): {e}”, exc_info=True)

async def cmd_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE):
“””
/monitoring pause | resume | status
“””
global monitoring_paused
mode = context.args[0].lower() if context.args else “status”

```
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
```

# ============================================================

# NATURAL LANGUAGE HANDLER — the “ask anything” feature

# ============================================================

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
“”“Catch all plain-text messages and route to Grok AI for Q&A.”””
user_msg = update.message.text.strip()
if not user_msg:
return

```
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
```

# ============================================================

# SCHEDULED MESSAGES

# ============================================================

def send_morning_briefing():
global daily_alerts
daily_alerts = 0
logger.info(“Morning briefing”)

```
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
send_dashboard_sync("Market Open")
```

def send_daily_close_summary():
logger.info(“Daily close summary”)
ai = get_grok_response(
f”Market just closed. We had {daily_alerts} spike alerts today. “
f”Give a 2-sentence close recap and 1 thing to watch overnight.”
)
send_telegram(
f”🔔 Daily Close Summary\n”
f”Alerts today: {daily_alerts}\n\n”
f”Grok Recap: {ai}”
)
send_dashboard_sync(“Market Close”)

def send_startup_message():
session      = get_trading_session()
status_str   = “OPEN Regular” if session == “regular” else “OPEN Extended” if session == “extended” else “CLOSED”
ai_sentiment = get_grok_response(“Current market sentiment in 6 words.”)
send_telegram(
f”🚀 STOCK SPIKE MONITOR STARTED\n\n”
f”Watching {len(TICKERS)} stocks (dynamic BULLISH)\n”
f”Market: {status_str}\n”
f”Spike threshold: {THRESHOLD*100:.0f}%\n”
f”Check interval: {CHECK_INTERVAL_MIN} min\n\n”
f”Grok: {ai_sentiment}\n\n”
f”Dashboard auto-sends: startup, 8:30 AM, 12:00 PM, 3:00 PM CT\n”
f”Type any question to chat with Grok AI!\n”
f”Send /help for all commands.”
)
send_dashboard_sync(“Startup”)

def send_weekly_digest():
“”“Sunday 6 PM CT — week-in-review of all spike alerts.”””
if not recent_alerts:
logger.info(“Weekly digest: no alerts to report”)
return
logger.info(“Sending weekly digest”)

```
# Tally alerts by ticker
tally = {}
for alert in recent_alerts:
    ticker = alert.split()[0]
    tally[ticker] = tally.get(ticker, 0) + 1

ranked  = sorted(tally.items(), key=lambda x: x[1], reverse=True)
top_str = ", ".join([f"{t}({n})" for t, n in ranked[:5]])

ai = get_grok_response(
    f"Weekly spike recap: {len(recent_alerts)} total alerts. "
    f"Most active: {top_str}. "
    f"Give a 2-sentence week summary and one stock to watch next week."
)
lines = [f"Weekly Digest — {datetime.now(CT).strftime('%B %d, %Y')}",
         f"Total spike alerts: {len(recent_alerts)}",
         "",
         "Most active tickers:"]
for t, n in ranked[:8]:
    lines.append(f"  {t}: {n} alert{'s' if n > 1 else ''}")
lines += ["", f"Grok: {ai}"]
send_telegram("\n".join(lines))
```

def send_premarket_dashboard():
“”“8:00 AM CT — pre-market snapshot before regular open.”””
logger.info(“Pre-market dashboard”)
ai = get_grok_response(
“Pre-market trading has begun. Give a 1-sentence pre-market mood “
“and the one sector to watch at open.”
)
send_telegram(
f”Pre-Market Snapshot — {datetime.now(CT).strftime(’%I:%M %p CT’)}\n”
f”Grok: {ai}”
)
send_dashboard_sync(“Pre-Market”)

def send_midday_dashboard():
“”“12:00 PM CT mid-session dashboard snapshot.”””
if get_trading_session() == “closed”:
return
logger.info(“Mid-day dashboard”)
ai = get_grok_response(
f”Mid-session check: {daily_alerts} spike alerts so far today. “
f”One-sentence mid-day market read.”
)
send_telegram(f”Mid-Day Check-In\nAlerts so far: {daily_alerts}\nGrok: {ai}”)
send_dashboard_sync(“Mid-Day”)

# ============================================================

# BACKGROUND SCANNER

# ============================================================

def scanner_thread():
schedule.every(CHECK_INTERVAL_MIN).minutes.do(check_stocks)
# Watchlist refresh + open events
schedule.every().day.at(“08:00”).do(send_premarket_dashboard)
schedule.every().day.at(“08:30”).do(
lambda: globals().update(TICKERS=get_dynamic_hot_stocks())
)
schedule.every().day.at(“08:30”).do(send_morning_briefing)    # open + dashboard
schedule.every().day.at(“12:00”).do(send_midday_dashboard)    # mid-day dashboard
schedule.every().day.at(“15:00”).do(send_daily_close_summary) # close + dashboard
# Weekly digest — Sunday 18:00
schedule.every().sunday.at(“18:00”).do(send_weekly_digest)
while True:
schedule.run_pending()
time.sleep(10)

# ============================================================

# MAIN — Telegram bot

# ============================================================

def run_telegram_bot():
app = Application.builder().token(TELEGRAM_TOKEN).build()

```
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
app.add_handler(CommandHandler("watchlist",   cmd_watchlist))

# ── Bot Control ───────────────────────────────────────────
app.add_handler(CommandHandler("dashboard",   cmd_dashboard))
app.add_handler(CommandHandler("list",        cmd_list))
app.add_handler(CommandHandler("monitoring",  cmd_monitoring))
app.add_handler(CommandHandler("help",        cmd_help))

# Natural language Q&A — catches ALL non-command text messages
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

app.run_polling()
```

# ============================================================

# ENTRY POINT

# ============================================================

threading.Thread(target=scanner_thread, daemon=True).start()
logger.info(“FULL INTERACTIVE MONITOR WITH BULLISH FILTER STARTED”)
send_startup_message()
run_telegram_bot()