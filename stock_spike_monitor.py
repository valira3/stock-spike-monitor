import yfinance as yf
import time
import schedule
import requests
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
COOLDOWN_MINUTES = 30
CHECK_INTERVAL_MIN = 1

LOG_FILE = "stock_spike_monitor.log"

# Logging + Grok client
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
                    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()])
logger = logging.getLogger(__name__)
CT = pytz.timezone('America/Chicago')
grok_client = OpenAI(api_key=GROK_API_KEY, base_url="https://api.x.ai/v1")

TICKERS = ["NVDA","TSLA","AMD","AAPL","AMZN","META","MSFT","GOOGL","SMCI","ARM","MU","AVGO",
           "QCOM","INTC","HIMS","PLTR","SOFI","RIVN","NIO","MARA","AMC","GME","LCID","BYND",
           "PFE","BAC","JPM","XOM","CVX","AAL"]

daily_alerts = 0
last_prices = {}
last_alert_time = {}
price_history = {t: deque(maxlen=10) for t in TICKERS}

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
        return data.get('c'), data.get('pc')
    except:
        return None, None

def fetch_latest_news(ticker):
    try:
        today = datetime.now().date()
        yesterday = today - timedelta(days=1)
        r = requests.get(f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={yesterday}&to={today}&token={FINNHUB_TOKEN}", timeout=10)
        news = r.json()[:2]
        return [(item.get('headline',''), item.get('url','')) for item in news]
    except:
        return []

def get_grok_response(prompt):
    try:
        resp = grok_client.chat.completions.create(
            model="grok-4-1-fast-non-reasoning",   # ← FIXED: correct current model
            messages=[{"role": "user", "content": prompt}],
            max_tokens=220, temperature=0.4
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Grok error: {e}")
        return "Grok analysis temporarily unavailable (using fallback sentiment)"

# ================== STARTUP MESSAGE (with market summary) ==================
def send_startup_message():
    session = get_trading_session()
    status = "OPEN (Regular Hours)" if session == "regular" else "OPEN (Extended Hours)" if session == "extended" else "CLOSED"
    # market summary code (same as before) ...
    # ... (keep the rest of your startup function exactly as it was)

# (All other functions — morning briefing, daily close, send_alert, check_stocks, scheduler — are unchanged)

# ================== SCHEDULER ==================
schedule.every(CHECK_INTERVAL_MIN).minutes.do(check_stocks)
schedule.every().day.at("08:30").do(send_morning_briefing)
schedule.every().day.at("15:00").do(send_daily_close_summary)

logger.info("✅ FULL AI TRADING CO-PILOT STARTED")
send_startup_message()
check_stocks()

while True:
    schedule.run_pending()
    time.sleep(10)