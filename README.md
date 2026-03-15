# Stock Spike Monitor

A real-time stock scanner and paper trading bot for Telegram, powered by Claude AI. Monitors 60+ stocks every minute, fires spike alerts, runs a $100k paper portfolio with 10-factor signal scoring, and mirrors trades to a live brokerage via TradersPost.

## Features

- **Real-Time Scanner** — Polls Finnhub every 60 seconds for price/volume changes across 30 core tickers + dynamically added movers
- **Spike Alerts** — Notifies on 3%+ moves with 15-min cooldown and 1% escalation threshold
- **Paper Trading** — Fully automated $100k simulated portfolio with trailing stops, take-profit, and adaptive thresholds
- **11-Factor Signal Scoring** — RSI, Bollinger Bands, MACD, volume, squeeze, slope, AI direction, AI watchlist conviction, multi-day trend, news sentiment, and AVWAP (max 150 pts)
- **Shadow Trading** — Mirrors paper trades to a real brokerage account via TradersPost webhooks
- **T+1 Settlement Tracking** — Cash account aware; tracks settled vs. unsettled funds
- **AI Integration** — Claude Sonnet for deep analysis, Claude Haiku for high-frequency scoring; Grok as fallback
- **VIX Put-Selling Alerts** — Auto-alerts when VIX crosses 33 with estimated put premiums on GOOG/NVDA/AMZN/META
- **Backtesting** — `/backtest` command replays logged signal data with custom parameters and generates a PDF report. Standalone `backtest.py` script for historical backtests against API data
- **Persistent Signal Logger** — Every signal evaluation is logged to `signal_log.jsonl` (all indicators, scores, market context, trades) for future backtesting
- **AVWAP Integration** — Anchored VWAP as entry gate (only buy above AVWAP) and stop-loss trigger (exit if price loses AVWAP)
- **Scheduled Reports** — Morning briefing, pre-market dashboard, midday update, close summary, evening recap, weekly digest, Saturday prep
- **Market Data** — Macro calendar, earnings, crypto, movers, sector overview, news sentiment
- **Charts** — Intraday price/volume charts, RSI/Bollinger charts, portfolio value charts, performance dashboards

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.11+ |
| Bot Framework | python-telegram-bot |
| AI (primary) | Anthropic Claude (Sonnet + Haiku) |
| AI (fallback) | xAI Grok |
| Market Data | Finnhub (real-time quotes), FMP (movers/gainers), yfinance (charts/candles) |
| Brokerage Bridge | TradersPost (webhook-based) |
| Charts | matplotlib |
| Hosting | Railway (auto-deploys from `main`) |
| State | JSON file on Railway Volume mount |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `FINNHUB_TOKEN` | Yes | Finnhub API key for real-time quotes |
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key for Claude |
| `TELEGRAM_TOKEN` | Yes | Main Telegram bot token |
| `CHAT_ID` | Yes | Main Telegram chat/group ID |
| `FMP_API_KEY` | Yes | Financial Modeling Prep API key |
| `TRADERSPOST_WEBHOOK_URL` | No | TradersPost webhook for live trade mirroring |
| `TELEGRAM_TP_TOKEN` | No | Separate Telegram bot token for TradersPost commands |
| `TELEGRAM_TP_CHAT_ID` | No | TradersPost Telegram chat ID |
| `GROK_API_KEY` | No | xAI Grok API key (fallback only) |
| `PAPER_STATE_PATH` | No | Path for paper trading state file (default: `paper_state.json`) |
| `PAPER_LOG_PATH` | No | Path for investment log (default: `investment.log`) |

## Deployment

The bot is deployed on [Railway](https://railway.app) and auto-deploys on every push to `main`.

### Railway Setup

1. Connect GitHub repo to Railway
2. Set all environment variables above
3. Attach a **Volume** and set `PAPER_STATE_PATH` to a path on the volume (e.g., `/data/paper_state.json`) so state persists across deploys
4. Railway auto-builds and deploys on push

### Local Development

```bash
# Install dependencies
pip install yfinance requests pandas pytz anthropic openai python-telegram-bot matplotlib

# Set environment variables
export FINNHUB_TOKEN="..."
export ANTHROPIC_API_KEY="..."
export TELEGRAM_TOKEN="..."
export CHAT_ID="..."
export FMP_API_KEY="..."

# Run
python stock_spike_monitor.py
```

## Architecture

The bot is a single-file Python application (~7,700 lines). See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed internals.

**High-level flow:**

```
Scheduler (1-min loop)
  ├── check_stocks() → scan all tickers → spike alerts + paper trading + signal logging
  ├── paper_scan() → evaluate buy/sell for each position (AVWAP gate + AVWAP stop)
  ├── check_vix_put_alert() → VIX threshold monitoring
  └── Scheduled reports (morning, midday, close, evening, weekly)

Telegram Bot (async, polling)
  ├── Main bot: all market/paper/analysis commands
  └── TP bot: shadow trading commands via DM
```

## Commands

See [COMMANDS.md](COMMANDS.md) for the full command reference.

**Quick overview:**

- `/overview` — Market indices, sectors, AI read
- `/paper` — Paper portfolio overview
- `/analyze TICK` — AI catalyst/risk/setup analysis
- `/chart TICK` — Intraday price + volume chart
- `/backtest 10 tp=8 sl=5` — Replay logged signals with custom parameters, generates PDF
- `/dashboard` — Visual market snapshot (220 DPI)
- `/help` — Full command menu

## Documentation

- [COMMANDS.md](COMMANDS.md) — Full command reference with usage examples
- [ARCHITECTURE.md](ARCHITECTURE.md) — Internal architecture, signal scoring, scheduler, data flow
- [CHANGELOG.md](CHANGELOG.md) — Version history from v1.0 to v1.19

## License

Private repository. Not licensed for redistribution.
