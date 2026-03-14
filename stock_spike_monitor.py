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