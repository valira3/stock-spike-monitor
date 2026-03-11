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

# === CONFIG FROM ENVIRONMENT VARIABLES ===
FINNHUB_TOKEN = os.getenv("FINNHUB_TOKEN")
GROK_API_KEY = os.getenv("GROK_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

THRESHOLD = 0.03
MIN_PRICE = 5.0
COOLDOWN_MINUTES = 5
CHECK_INTERVAL_MIN = 1

LOG_FILE = "stock_spike_monitor.log"

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)
CT = pytz.timezone('America/Chicago')

grok_client = OpenAI(api_key=GROK_API_KEY, base_url="https://api.x.ai/v1") if GROK_API_KEY else None

# Core stable stocks
CORE_TICKERS = [
    "NVDA", "TSLA", "AMD", "AAPL", "AMZN", "META", "MSFT", "GOOGL", "SMCI", "ARM",
    "MU", "AVGO", "QCOM", "INTC", "HIMS", "PLTR", "SOFI", "RIVN", "NIO", "MARA",
    "AMC", "GME", "LCID", "BYND", "PFE", "BAC", "JPM", "XOM", "CVX", "AAL"
]

TICKERS = CORE_TICKERS.copy()

daily_alerts = 0
last_prices = {}
last_alert_time = {}
price_history = {t: deque(maxlen=10) for t in CORE_TICKERS}

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
            r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)
            r.raise_for_status()
            logger.info(f"Telegram part {i}/{total} sent")
            time.sleep(0.3)
        except Exception as e:
            logger.error(f"Telegram part {i} failed: {e}")

# ────────────────────────────────────────────────
# DYNAMIC HOT + LOW-PRICED STOCKS (FIXED)
# ────────────────────────────────────────────────
def get_dynamic_hot_stocks():
    logger.info("Fetching dynamic hot stocks + low-priced rockets...")
    hot = []
    low_price = []  # ← FIXED: always defined

    try:
        # Most Active
        df_active = pd.read_html("https://finance.yahoo.com/screener/predefined/most_actives")[0]
        hot.extend(df_active["Symbol"].head(20).tolist())

        # Top Gainers
        df_gainers = pd.read_html("https://finance.yahoo.com/screener/predefined/day_gainers")[0]
        hot.extend(df_gainers["Symbol"].head(15).tolist())

        # Low-priced explosion candidates ($1–$10 with strong momentum)
        low_price = df_gainers[
            (df_gainers.get("Price (Intraday)", pd.Series(0)).astype(float, errors='ignore') >= 1) &
            (df_gainers.get("Price (Intraday)", pd.Series(0)).astype(float, errors='ignore') <= 10) &
            (df_gainers.get("% Change", pd.Series(0)).astype(float, errors='ignore') > 8)
        ]["Symbol"].head(10).tolist()

    except Exception as e:
        logger.warning(f"Dynamic fetch failed: {e}. Using core list only.")

    # Clean + dedupe
    hot = [t.upper() for t in hot if isinstance(t, str) and 1 <= len(t) <= 6]
    combined = list(dict.fromkeys(CORE_TICKERS + hot + low_price))[:60]

    logger.info(f"Dynamic list updated → {len(combined)} stocks ({len(low_price)} low-priced rockets)")
    return combined

# Update list at startup and every morning
TICKERS = get_dynamic_hot_stocks()

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
        data = r.json()
        return data.get('c')
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
        resp = grok_client.chat.completions.create(
            model="grok-4-1-fast-non-reasoning",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=140,
            temperature=0.4
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Grok error: {e}")
        return "Grok unavailable"

# ────────────────────────────────────────────────
# Messages
# ────────────────────────────────────────────────
def send_startup_message():
    session = get_trading_session()
    status = "OPEN Regular" if session == "regular" else "OPEN Extended" if session == "extended" else "CLOSED"
    grok_prompt = "Current market sentiment in 6 words."
    ai_sentiment = get_grok_response(grok_prompt)

    message = f"""🚀 MONITOR STARTED

Watching {len(TICKERS)} stocks (dynamic hot + low-priced rockets) | 3% spikes + Grok AI

Status: {status}
Grok: {ai_sentiment}

Morning brief: 8:30 AM CT
Daily summary: 3:00 PM CT

Live scanning now."""

    send_telegram(message)

def send_morning_briefing():
    global daily_alerts
    daily_alerts = 0
    logger.info("Morning briefing")
    send_telegram("🌅 Morning briefing coming soon...")

def send_daily_close_summary():
    global daily_alerts
    logger.info("Daily close summary")
    send_telegram(f"📉 Daily close - {daily_alerts} alerts today")

def send_alert(ticker, pct_change, current_price):
    global daily_alerts
    daily_alerts += 1
    news_items = fetch_latest_news(ticker)
    grok_prompt = f"Analyze spike: {ticker} {pct_change:+.1f}% ~5 min. Price ${current_price:.2f}. Short analysis."
    ai_analysis = get_grok_response(grok_prompt)
    news_text = "\n".join([f"• {h[:80]}" for h,_ in news_items]) if news_items else "No news"

    message = f"""🚨 {ticker} SPIKE

{pct_change:+.1f}% | ${current_price:.2f}

Grok: {ai_analysis}

News:
{news_text}"""

    send_telegram(message)

def check_stocks():
    if get_trading_session() == "closed":
        return
    now = datetime.now(CT)
    logger.info(f"Scanning {len(TICKERS)} stocks at {now.strftime('%H:%M:%S %Z')}")

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
# Scheduler & startup – AFTER all definitions
# ────────────────────────────────────────────────
schedule.every(CHECK_INTERVAL_MIN).minutes.do(check_stocks)
schedule.every().day.at("08:30").do(lambda: globals().update(TICKERS=get_dynamic_hot_stocks()))
schedule.every().day.at("08:30").do(send_morning_briefing)
schedule.every().day.at("15:00").do(send_daily_close_summary)

logger.info("✅ DYNAMIC HOT + LOW-PRICED ROCKET MONITOR STARTED")
send_startup_message()
check_stocks()

while True:
    schedule.run_pending()
    time.sleep(10)