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
TRADERSPOST_WEBHOOK_URL = os.getenv("TRADERSPOST_WEBHOOK_URL")
TELEGRAM_TP_CHAT_ID     = os.getenv("TELEGRAM_TP_CHAT_ID")
TELEGRAM_TP_TOKEN       = os.getenv("TELEGRAM_TP_TOKEN")

# FMP stable API endpoints (v3 is deprecated for newer accounts)
FMP_ENDPOINTS = {
    "actives": "https://financialmodelingprep.com/stable/most-actives",
    "gainers": "https://financialmodelingprep.com/stable/biggest-gainers",
    "losers":  "https://financialmodelingprep.com/stable/biggest-losers",
}

BOT_VERSION = "1.19"
RELEASE_NOTES = [
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