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

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)
CT = pytz.timezone('America/Chicago')

grok_client = OpenAI(api_key=GROK_API_KEY, base_url="https://api.x.ai/v1") if GROK_API_KEY else None

TICKERS = [
    "NVDA", "TSLA", "AMD", "AAPL", "AMZN", "META", "MSFT", "GOOGL", "SMCI", "ARM",
    "MU", "AVGO", "QCOM", "INTC", "HIMS", "PLTR", "SOFI", "RIVN", "NIO", "MARA",
    "AMC", "GME", "LCID", "BYND", "PFE", "BAC", "JPM", "XOM", "CVX", "AAL"
]

daily_alerts = 0
last_prices = {}
last_alert_time = {}
price_history = {t: deque(maxlen=10) for t in TICKERS}

# ────────────────────────────────────────────────
# Safe multi-part Telegram sender
# ────────────────────────────────────────────────

def send_telegram(text, max_chunk=3800):
    """
    Send text, splitting into multiple messages if necessary.
    Adds (1/N) numbering for multi-part messages.
    """
    if not text.strip():
        return

    parts = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > max_chunk:
            if current:
                parts.append(current.rstrip())
            current = line
        else:
            current += line

    if current:
        parts.append(current.rstrip())

    total_parts = len(parts)

    for i, part in enumerate(parts, 1):
        prefix = f"({i}/{total_parts}) " if total_parts > 1 else ""
        payload = {
            "chat_id": CHAT_ID,
            "text": prefix + part,
            "parse_mode": "HTML"
        }
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json=payload,
                timeout=10
            )
            r.raise_for_status()
            logger.info(f"Telegram part {i}/{total_parts} sent")
            time.sleep(0.4)  # polite rate limit avoidance
        except Exception as e:
            logger.error(f"Telegram part {i} failed: {e}")


# ────────────────────────────────────────────────
# Helper functions (unchanged except for send_telegram call)
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
        return data.get('c'), data.get('pc')
    except:
        return None, None

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

def get_market_summary():
    indices = {"^GSPC": "S&P 500", "^IXIC": "Nasdaq", "^DJI": "Dow"}
    lines = []
    for sym, name in indices.items():
        try:
            info = yf.Ticker(sym).fast_info
            chg = info.get('regularMarketChangePercent', 0) or 0
            lines.append(f"{name}: {chg:+.2f}%")
        except:
            lines.append(f"{name}: N/A")
    return " | ".join(lines)

# ────────────────────────────────────────────────
# Messages – now using send_telegram (safe splitting)
# ────────────────────────────────────────────────

def send_startup_message():
    session = get_trading_session()
    status = "OPEN (Regular)" if session == "regular" else "OPEN (Extended)" if session == "extended" else "CLOSED"
    market_summary = get_market_summary()

    grok_prompt = "Current market sentiment in 6 words."
    ai_sentiment = get_grok_response(grok_prompt)

    message = f"""🚀 MONITOR STARTED

Watching {len(TICKERS)} stocks | 3% spikes + Grok AI + news

Market now: {market_summary}
Grok view: {ai_sentiment}

Status: {status}
Morning brief: 8:30 AM CT
Daily summary: 3:00 PM CT

Live scanning now."""

    send_telegram(message)

def send_morning_briefing():
    global daily_alerts
    daily_alerts = 0
    logger.info("Morning briefing")

    gaps = []
    for t in TICKERS:
        c, pc = fetch_finnhub_quote(t)
        if c and pc:
            gap_pct = (c - pc) / pc * 100
            gaps.append((t, gap_pct, c))
    gaps.sort(key=lambda x: abs(x[1]), reverse=True)

    gap_text = "\n".join([f"• {t} {g:+.1f}% → ${p:.2f}" for t,g,p in gaps[:4]])

    grok_prompt = f"Pre-market outlook for {datetime.now(CT).strftime('%A')}. List: {', '.join(TICKERS[:10])}. Give sentiment + 1-2 ideas."
    ai_outlook = get_grok_response(grok_prompt)

    message = f"""🌅 MARKET OPEN – {datetime.now(CT).strftime('%b %d')}

Pre-market:
{gap_text or "No big gaps"}

Grok outlook:
{ai_outlook}

Monitor active."""

    send_telegram(message)

def send_daily_close_summary():
    global daily_alerts
    logger.info("Daily close summary")

    performance = []
    for t in TICKERS:
        try:
            hist = yf.Ticker(t).history(period="2d")
            if len(hist) >= 2:
                change = (hist['Close'][-1] / hist['Close'][-2] - 1) * 100
                performance.append((t, change))
        except:
            pass
    performance.sort(key=lambda x: x[1], reverse=True)

    gainers = "\n".join([f"• {t} +{c:.1f}%" for t,c in performance[:4]])
    losers  = "\n".join([f"• {t} {c:.1f}%" for t,c in performance[-4:]])

    grok_prompt = f"Daily recap: {daily_alerts} alerts. Top movers: {', '.join([t for t,c in performance[:6]])}. Summarize + 1 lesson."
    ai_recap = get_grok_response(grok_prompt)

    message = f"""📉 CLOSE – {datetime.now(CT).strftime('%b %d')}

Gainers:
{gainers}

Losers:
{losers}

Alerts: {daily_alerts}

Grok recap:
{ai_recap}"""

    send_telegram(message)

def send_alert(ticker, pct_change, current_price):
    global daily_alerts
    daily_alerts += 1
    news_items = fetch_latest_news(ticker)
    grok_prompt = f"Analyze spike: {ticker} {pct_change:+.1f}% ~5 min. Price ${current_price:.2f}. News: {[h for h,_ in news_items]}. Short: Why / Pred / Risk / Action."
    ai_analysis = get_grok_response(grok_prompt)

    news_text = "\n".join([f"• {h[:100]}" for h,_ in news_items]) if news_items else "No news"

    message = f"""🚨 {ticker} SPIKE

{pct_change:+.1f}% | ${current_price:.2f}

Grok:
{ai_analysis}

News:
{news_text}"""

    send_telegram(message)

def check_stocks():
    if get_trading_session() == "closed":
        return

    now = datetime.now(CT)
    logger.info(f"Scanning {len(TICKERS)} stocks at {now.strftime('%H:%M:%S %Z')}")

    for ticker in TICKERS:
        c, _ = fetch_finnhub_quote(ticker)
        if not c or c < MIN_PRICE: continue
        price_history[ticker].append((now, c))
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
# Scheduler & startup
# ────────────────────────────────────────────────

schedule.every(CHECK_INTERVAL_MIN).minutes.do(check_stocks)
schedule.every().day.at("08:30").do(send_morning_briefing)
schedule.every().day.at("15:00").do(send_daily_close_summary)

logger.info("✅ FULL AI TRADING CO-PILOT STARTED")
send_startup_message()
check_stocks()

while True:
    schedule.run_pending()
    time.sleep(10)