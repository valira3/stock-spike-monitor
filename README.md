# Stock Spike Monitor 📈

A real-time stock monitoring Telegram bot with AI analysis, paper trading, live market dashboards, and squeeze detection.

---

## Features

| Category | What it does |
|---|---|
| **Real-time scanning** | Scans 60-80 tickers every 60s, fires spike alerts on ≥3% moves |
| **AI analysis** | Claude Sonnet (primary) + Grok (fallback) explain every alert and answer questions |
| **Paper trading** | Automated buy/sell engine with RSI, Bollinger, MACD, squeeze, and AI signals |
| **Market dashboards** | Pre-market, midday, close, and evening recaps sent automatically |
| **25 commands** | `/movers`, `/overview`, `/analyze`, `/chart`, `/rsi`, `/squeeze`, `/ask`, and more |
| **Off-hours aware** | All commands work 24/7 — uses previous close when live price unavailable |
| **Persistence** | Watchlists, alerts, and paper positions survive restarts via JSON state files |

---

## Quick Deploy to Railway

### 1 — Fork / upload to GitHub
Push this entire folder to a GitHub repo.

### 2 — Create Railway project
1. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
2. Select your repo

### 3 — Add a Volume (required for persistence)
1. In your Railway project → **+ New** → **Volume**
2. Mount path: `/data`

### 4 — Set environment variables
In Railway → your service → **Variables**, add:

| Variable | Required | Notes |
|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | From @BotFather |
| `CHAT_ID` | ✅ | Your group chat ID (negative number) |
| `FINNHUB_TOKEN` | ✅ | Free at [finnhub.io](https://finnhub.io) |
| `ANTHROPIC_API_KEY` | ✅ | From [console.anthropic.com](https://console.anthropic.com) |
| `GROK_API_KEY` | recommended | From [console.x.ai](https://console.x.ai) |
| `FMP_API_KEY` | recommended | Free at [financialmodelingprep.com](https://financialmodelingprep.com) |
| `PAPER_STATE_PATH` | ✅ | `/data/paper_state.json` |
| `PAPER_LOG_PATH` | ✅ | `/data/investment.log` |
| `BOT_STATE_PATH` | ✅ | `/data/bot_state.json` |

> ⚠️ **Do NOT set `TZ`** — the bot manages timezones internally via pytz (US/Central).

### 5 — Deploy
Railway auto-deploys on every push. Check the logs tab for startup confirmation.

---

## Deploy to Docker / Fly.io / Render

```bash
# Build
docker build -t stock-spike-monitor .

# Run (mount a volume for persistence)
docker run -d \
  --name spike-bot \
  -v $(pwd)/data:/data \
  -e TELEGRAM_TOKEN=your_token \
  -e CHAT_ID=your_chat_id \
  -e FINNHUB_TOKEN=your_key \
  -e ANTHROPIC_API_KEY=your_key \
  -e FMP_API_KEY=your_key \
  -e GROK_API_KEY=your_key \
  stock-spike-monitor
```

---

## Local Development

```bash
# 1. Clone / download this folder
cd stock_spike_package

# 2. Create virtual environment
python3.11 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# 3. Install deps
pip install -r requirements.txt

# 4. Copy and fill env vars
cp .env.example .env
# Edit .env with your keys

# 5. Load env and run
export $(cat .env | grep -v '^#' | xargs)
python stock_spike_monitor.py
```

---

## Telegram Commands Reference

### Market Pulse
| Command | Description |
|---|---|
| `/overview` | Indices + sectors + Fear & Greed with AI commentary |
| `/crypto` | Live BTC, ETH, SOL, DOGE, XRP prices |
| `/macro` | Upcoming economic events (Fed, CPI, GDP, etc.) |
| `/earnings` | Upcoming earnings calendar (7-day window) |

### Movers
| Command | Description |
|---|---|
| `/movers` | Top gainers, losers, most active, low-price rockets |

### Stock Tools
| Command | Description |
|---|---|
| `/price AAPL` | Live quote with day range, volume, 52w range |
| `/analyze NVDA` | AI-powered fundamental + technical analysis |
| `/compare TSLA RIVN` | Side-by-side AI comparison of two stocks |
| `/chart MSFT` | ASCII price chart (last 78 candles) |
| `/rsi AAPL` | RSI indicator with AI interpretation |
| `/news TSLA` | Latest headlines for a ticker |

### Alerts & Watchlist
| Command | Description |
|---|---|
| `/spikes` | Show all spike alerts fired today |
| `/alerts` | Show today's alert count and recent triggers |
| `/squeeze` | Top squeeze candidates ranked by score |
| `/setalert AAPL 200` | Set a price alert for any ticker |
| `/watchlist add NVDA` | Add ticker to personal watchlist |
| `/watchlist remove NVDA` | Remove ticker from watchlist |
| `/watchlist` | Show your watchlist |

### Paper Trading
| Command | Description |
|---|---|
| `/paper` | Portfolio value, P&L, positions summary |
| `/paper positions` | All open positions with entry price and P&L |
| `/paper trades` | Today's executed trades |
| `/paper history` | All-time trade log |
| `/paper signal AAPL` | Compute buy/sell signal score for any ticker |
| `/paper log` | Full investment journal |
| `/paper reset` | Reset paper portfolio to $100k |
| `/overnight` | After-hours AI briefing on your positions |

### Off-Hours & AI
| Command | Description |
|---|---|
| `/prep` | Pre-market prep for tomorrow (macro, technicals, AI picks) |
| `/wlprep` | Watchlist-specific prep report |
| `/ask What will NVDA do tomorrow?` | Direct AI chat with live market context |

### Bot Management
| Command | Description |
|---|---|
| `/dashboard` | Full real-time dashboard (indices + sectors + movers + crypto) |
| `/list` | Show all monitored tickers |
| `/monitoring pause` | Pause spike scanning |
| `/monitoring resume` | Resume spike scanning |
| `/monitoring status` | Show current scan state |
| `/help` | Command reference |

---

## Architecture

```
stock_spike_monitor.py  (single file, ~4,200 lines)
│
├── Background thread: scanner_thread()
│     CT-aware scheduler — checks/fires jobs every 30s
│     check_stocks() — concurrent Finnhub scan of all TICKERS
│     paper_scan()   — automated paper trade execution
│
├── Main thread: run_telegram_bot()
│     python-telegram-bot async polling
│     25 command handlers
│
└── Shared state (module globals)
      TICKERS           — dynamic watchlist (refreshed daily at 8:30 AM CT)
      price_history     — deque of (datetime, price) per ticker
      squeeze_scores    — latest squeeze score per ticker
      paper_positions   — open paper trade positions
      conversation_history — per-chat AI conversation history
```

### Data Flow
```
Market Data:  Finnhub (primary) → yfinance (fallback, rarely fires on Railway)
              FMP actives/gainers/losers → /movers universe building

AI:           Claude Sonnet (primary) → Claude Haiku (fast/bulk) → Grok (fallback)

Persistence:  bot_state.json   → watchlists, alerts, tickers, conversations
              paper_state.json → positions, cash, trade history
```

### Scheduled Messages (all CT)
| Time | Message |
|---|---|
| 8:00 AM | Pre-market dashboard |
| 8:30 AM | Morning briefing + paper open report + ticker refresh |
| 12:00 PM | Midday dashboard |
| 3:00 PM | Daily close summary + paper EOD report |
| 6:00 PM | Evening recap |
| Sunday 6:00 PM | Weekly digest |
| Saturday 9:00 AM | Next-week prep |

---

## Configuration Constants

Edit these at the top of `stock_spike_monitor.py`:

```python
THRESHOLD          = 0.03    # 3% move triggers a spike alert
MIN_PRICE          = 5.0     # ignore stocks below this price in scanner
COOLDOWN_MINUTES   = 5       # minimum minutes between alerts per ticker
CHECK_INTERVAL_MIN = 1       # scan interval (minutes)
VOLUME_SPIKE_MULT  = 2.0     # volume must be 2x avg to count as volume spike

# AI models
CLAUDE_SONNET = "claude-sonnet-4-5"
CLAUDE_HAIKU  = "claude-haiku-4-5-20251001"
GROK_MODEL    = "grok-4-1-fast-non-reasoning"

# Paper trading
PAPER_STARTING_CAPITAL = 100_000.0
PAPER_MAX_POSITIONS    = 8       # max simultaneous open positions
PAPER_MAX_POS_PCT      = 0.20    # max 20% of portfolio per position
PAPER_TAKE_PROFIT_PCT  = 0.08    # take profit at +8%
PAPER_STOP_LOSS_PCT    = 0.04    # stop loss at -4%
PAPER_MIN_SIGNAL       = 65      # min composite score to trigger buy
```

---

## API Keys — Where to Get Them

| Service | URL | Free Tier |
|---|---|---|
| Telegram Bot | t.me/BotFather | Free |
| Finnhub | finnhub.io/register | 60 calls/min free |
| Anthropic | console.anthropic.com | Pay-per-token |
| Grok (xAI) | console.x.ai | Credit-based |
| FMP | financialmodelingprep.com/register | 250 calls/day free |

---

## Troubleshooting

**"Unable to fetch live market data"**  
→ Check `FINNHUB_TOKEN` is set correctly in Railway variables. Test: `curl "https://finnhub.io/api/v1/quote?symbol=AAPL&token=YOUR_TOKEN"`

**Bot not responding to commands**  
→ Check `TELEGRAM_TOKEN` and `CHAT_ID`. Make sure the bot is an admin in the group.

**Paper state resets on redeploy**  
→ Railway Volume is not mounted. Verify Volume is attached at `/data` in Railway dashboard.

**Scheduled messages not sending**  
→ Do NOT set `TZ` env var. The bot uses pytz US/Central internally.

**FMP commands returning errors**  
→ Add `FMP_API_KEY` to Railway variables. Get a free key at financialmodelingprep.com.

**AI responses slow or failing**  
→ Check `ANTHROPIC_API_KEY`. Bot falls back to Grok if Anthropic fails, and to basic text if both fail.

---

## Platform Compatibility

| Platform | Status | Notes |
|---|---|---|
| Railway | ✅ Recommended | Set Volume for persistence |
| Docker | ✅ | Mount `/data` volume |
| Fly.io | ✅ | Use persistent volumes, set env vars |
| Render | ✅ | Use worker dyno type |
| Heroku | ✅ | Use worker Procfile entry |
| Local / VPS | ✅ | Use `.env` file or export vars |

---

## File Structure

```
stock_spike_package/
├── stock_spike_monitor.py   # Main bot (single file, all logic)
├── requirements.txt          # Pinned Python dependencies
├── .env.example              # Environment variable template
├── railway.json              # Railway deployment config
├── nixpacks.toml             # Railway build config
├── Dockerfile                # Docker / container deployment
├── Procfile                  # Heroku / Render deployment
└── README.md                 # This file
```
