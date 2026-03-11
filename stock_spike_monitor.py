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
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# === CONFIG FROM ENVIRONMENT VARIABLES ===
FINNHUB_TOKEN = os.getenv("FINNHUB_TOKEN")
GROK_API_KEY = os.getenv("GROK_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
FMP_API_KEY = os.getenv("FMP_API_KEY")

THRESHOLD = 0.03
MIN_PRICE = 5.0
COOLDOWN_MINUTES = 5
CHECK_INTERVAL_MIN = 1

LOG_FILE = "stock_spike_monitor.log"

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
                    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()])
logger = logging.getLogger(__name__)
CT = pytz.timezone('America/Chicago')

grok_client = OpenAI(api_key=GROK_API_KEY, base_url="https://api.x.ai/v1") if GROK_API_KEY else None

CORE_TICKERS = [
    "NVDA", "TSLA", "AMD", "AAPL", "AMZN", "META", "MSFT", "GOOGL", "SMCI", "ARM",
    "MU", "AVGO", "QCOM", "INTC", "HIMS", "PLTR", "SOFI", "RIVN", "NIO", "MARA",
    "AMC", "GME", "LCID", "BYND", "PFE", "BAC", "JPM", "XOM", "CVX", "AAL"
]

TICKERS = CORE_TICKERS.copy()
monitoring_paused = False
daily_alerts = 0
last_prices = {}
last_alert_time = {}
price_history = {t: deque(maxlen=10) for t in CORE_TICKERS}
recent_alerts = []

# ────────────────────────────────────────────────
# SAFE MULTI-PART TELEGRAM SENDER
# ────────────────────────────────────────────────
def send_telegram(text):
    if not text.strip(): return
    parts = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > 3800:
            if current: parts.append(current.rstrip())
            current = line
        else:
            current += line
    if current: parts.append(current.rstrip())

    total = len(parts)
    for i, part in enumerate(parts, 1):
        prefix = f"({i}/{total}) " if total > 1 else ""
        payload = {"chat_id": CHAT_ID, "text": prefix + part}
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)
            time.sleep(0.3)
        except Exception as e:
            logger.error(f"Telegram failed: {e}")

# ────────────────────────────────────────────────
# DYNAMIC STOCKS USING FMP (robust error handling)
# ────────────────────────────────────────────────
def get_dynamic_hot_stocks():
    logger.info("Fetching dynamic hot stocks via FMP...")
    hot = []
    low_price = []

    try:
        r = requests.get(f"https://financialmodelingprep.com/api/v3/stock_market/actives?apikey={FMP_API_KEY}", timeout=10)
        data = r.json()
        if isinstance(data, list):
            hot = [item.get('symbol') for item in data[:20] if isinstance(item, dict)]

        r = requests.get(f"https://financialmodelingprep.com/api/v3/stock_market/gainers?apikey={FMP_API_KEY}", timeout=10)
        data = r.json()
        if isinstance(data, list):
            hot.extend([item.get('symbol') for item in data[:15] if isinstance(item, dict)])

        low_price = [item.get('symbol') for item in data 
                     if isinstance(item, dict) and 1 <= item.get('price', 0) <= 10 and item.get('changesPercentage', 0) > 8][:10]

    except Exception as e:
        logger.warning(f"FMP fetch failed: {e}. Using core list only.")

    hot = [t.upper() for t in hot if isinstance(t, str) and 1 <= len(t) <= 6]
    combined = list(dict.fromkeys(CORE_TICKERS + hot + low_price))[:60]
    logger.info(f"Dynamic list updated → {len(combined)} stocks ({len(low_price)} low-priced rockets)")
    return combined

TICKERS = get_dynamic_hot_stocks()

# ────────────────────────────────────────────────
# Telegram Commands
# ────────────────────────────────────────────────
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""Commands:
/status   - Status + stock count
/list     - Monitored stocks
/alerts   - Alerts today
/market   - Current market snapshot
/spikes   - Recent spikes (last 30 min)
/pause    - Pause monitoring
/resume   - Resume monitoring
/help     - This help""")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = "PAUSED" if monitoring_paused else "RUNNING"
    await update.message.reply_text(f"Status: {status}\nStocks: {len(TICKERS)}\nAlerts today: {daily_alerts}")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Monitoring:\n" + "\n".join(sorted(TICKERS)))

async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Alerts today: {daily_alerts}")

async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    indices = {"^GSPC": "S&P 500", "^IXIC": "Nasdaq", "^DJI": "Dow"}
    lines = [f"{name}: {yf.Ticker(sym).fast_info.get('regularMarketChangePercent', 0):+.2f}%" for sym, name in indices.items()]
    summary = " | ".join(lines)
    grok_prompt = f"Market snapshot: {summary}. Short sentiment."
    ai = get_grok_response(grok_prompt)
    await update.message.reply_text(f"Market now:\n{summary}\n\nGrok: {ai}")

async def cmd_spikes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not recent_alerts:
        await update.message.reply_text("No spikes in last 30 minutes.")
        return
    await update.message.reply_text("Recent spikes:\n" + "\n".join(recent_alerts[-10:]))

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitoring_paused
    monitoring_paused = True
    await update.message.reply_text("✅ Monitoring PAUSED")

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitoring_paused
    monitoring_paused = False
    await update.message.reply_text("✅ Monitoring RESUMED")

# ────────────────────────────────────────────────
# Helper functions
# ────────────────────────────────────────────────
def get_trading_session():
    now = datetime.now(CT)
    if now.weekday() > 4: return "closed"
    current = now.time()
    if datetime.strptime("07:00", "%H:%M").time() <= current < datetime.strptime("20:00", "%H:%M").time():
        return "regular" if datetime.strptime("08:30", "%H:%M").time() <= current < datetime.strptime("15:00", "%H:%M").time() else "extended"
    return "closed"

def fetch_finnhub_quote(ticker):
    try:
        r = requests.get(f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_TOKEN}", timeout=10)
        return r.json().get('c')
    except:
        return None

def fetch_latest_news(ticker):
    try:
        today = datetime.now().date()
        yesterday = today - timedelta(days=1)
        r = requests.get(f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={yesterday}&to={today}&token={FINNHUB_TOKEN}", timeout=10)
        news = r.json()[:2]
        return [(item.get('headline', ''), item.get('url', '')) for item in news]
    except:
        return []

def get_grok_response(prompt):
    if not grok_client: return "AI disabled"
    try:
        resp = grok_client.chat.completions.create(model="grok-4-1-fast-non-reasoning", messages=[{"role": "user", "content": prompt}], max_tokens=140, temperature=0.4)
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Grok error: {e}")
        return "Grok unavailable"

def send_alert(ticker, pct_change, current_price):
    global daily_alerts
    daily_alerts += 1
    news_items = fetch_latest_news(ticker)
    grok_prompt = f"Analyze spike: {ticker} {pct_change:+.1f}% ~5 min. Price ${current_price:.2f}. Short analysis."
    ai = get_grok_response(grok_prompt)
    news_text = "\n".join([f"• {h[:80]}" for h,_ in news_items]) if news_items else "No news"

    message = f"""🚨 {ticker} SPIKE

{pct_change:+.1f}% | ${current_price:.2f}

Grok: {ai}

News:
{news_text}"""
    send_telegram(message)
    recent_alerts.append(f"{ticker} {pct_change:+.1f}% at {datetime.now(CT).strftime('%H:%M')}")

def check_stocks():
    if monitoring_paused or get_trading_session() == "closed":
        return
    now = datetime.now(CT)
    logger.info(f"Scanning {len(TICKERS)} stocks...")

    for ticker in TICKERS:
        c = fetch_finnhub_quote(ticker)
        if not c or c < MIN_PRICE: continue
        price_history.setdefault(ticker, deque(maxlen=10)).append((now, c))
        if ticker in last_prices:
            old_price = last_prices[ticker]
            for ts, p in list(price_history[ticker]):
                if (now - ts).total_seconds() > 280:
                    old_price = p
                    break
            change = (c - old_price) / old_price
            if abs(change) >= THRESHOLD:
                last_alert = last_alert_time.get(ticker, now - timedelta(days=1))
                if (now - last_alert).total_seconds() / 60 >= COOLDOWN_MINUTES:
                    send_alert(ticker, change * 100, c)
                    last_alert_time[ticker] = now
        last_prices[ticker] = c

# ────────────────────────────────────────────────
# Scheduler & startup – AFTER ALL FUNCTIONS
# ────────────────────────────────────────────────
schedule.every(CHECK_INTERVAL_MIN).minutes.do(check_stocks)
schedule.every().day.at("08:30").do(lambda: globals().update(TICKERS=get_dynamic_hot_stocks()))
schedule.every().day.at("08:30").do(send_morning_briefing)
schedule.every().day.at("15:00").do(send_daily_close_summary)

logger.info("✅ MONITOR STARTED WITH FMP + INTERACTIVE BOT")

def run_telegram_bot():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("market", cmd_market))
    app.add_handler(CommandHandler("spikes", cmd_spikes))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.run_polling()

threading.Thread(target=run_telegram_bot, daemon=True).start()

send_startup_message()
check_stocks()

while True:
    schedule.run_pending()
    time.sleep(10)