# Command Reference

All commands are available via the main Telegram bot. TradersPost-specific commands are also available on the separate TP bot (`@valstradebot_bot`).

---

## Market Overview

| Command | Description |
|---------|-------------|
| `/overview` | Major indices (SPY, QQQ, DIA, IWM), sector ETFs, Fear & Greed Index, VIX, and an AI market read |
| `/movers` | Top gainers, losers, and most active stocks from FMP |
| `/crypto` | Live prices for BTC, ETH, SOL, DOGE, XRP |
| `/macro` | Upcoming US macroeconomic events (CPI, Fed, NFP, FOMC) via AI |
| `/earnings` | Earnings calendar for the next 7 days |
| `/dashboard` | High-DPI (220 DPI) visual market snapshot — indices, paper portfolio, positions, AI commentary. Sent as a document for crisp mobile viewing |

---

## Stock Analysis

| Command | Description |
|---------|-------------|
| `/price TICK` | Live quote with day range, 52-week range, volume, market cap |
| `/chart TICK` | Intraday price + volume chart (yfinance data) |
| `/chart TICK 5d` | Multi-day chart (supports `1d`, `5d`, `1mo`, `3mo`, `6mo`, `1y`) |
| `/analyze TICK` | AI-powered deep analysis: catalysts, risks, entry/exit levels, setup quality |
| `/compare TICK1 TICK2` | Side-by-side AI comparison of two stocks |
| `/rsi TICK` | RSI, Bollinger Bands, bandwidth, squeeze score |
| `/news TICK` | Latest headlines with sentiment scoring and source timestamps |

---

## Alerts & Watchlist

| Command | Description |
|---------|-------------|
| `/spikes` | Recent spike alerts (3%+ moves) |
| `/alerts` | All alerts fired today |
| `/squeeze` | Top squeeze candidates across monitored tickers |
| `/setalert TICK $PRICE` | Set a custom price alert (e.g., `/setalert NVDA 150`) |
| `/myalerts` | View all active custom price alerts |
| `/delalert TICK` | Remove all price alerts for a ticker |
| `/watchlist show` | Show your personal watchlist |
| `/watchlist add TICK` | Add a ticker to your watchlist |
| `/watchlist remove TICK` | Remove a ticker from your watchlist |
| `/watchlist scan` | Scan all watchlist tickers for signals |

---

## Paper Trading ($100k Simulated Portfolio)

The paper trading engine runs automatically every scan cycle (~1 minute). It uses an 11-factor composite signal (max 150 pts) to decide buys, and trailing stops / take-profit / hard stops / AVWAP stops for exits.

AVWAP (Anchored VWAP) acts as both an entry gate (only buys when price is above AVWAP) and a stop-loss trigger (exits if price drops below AVWAP after reclaiming it).

| Command | Description |
|---------|-------------|
| `/paper` | Portfolio overview: cash, positions, total value, P&L |
| `/paper positions` | All open positions with live P&L per ticker |
| `/paper trades` | Trades executed today |
| `/paper history` | Historical win rate, average P&L, total trades |
| `/paper signal TICK` | Show the full 11-factor signal breakdown for a ticker |
| `/paper chart` | Intraday portfolio value chart |
| `/paper log` | Download the full trade log file |
| `/paper reset` | Reset portfolio to $100k (requires confirmation) |
| `/perf` | Performance dashboard: win rate, avg gain/loss, Sharpe-like stats |
| `/overnight` | Gap risk analysis on current holdings |

### Configurable Parameters

Use `/set` to view and adjust trading parameters:

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `threshold` | 65 | 40–100 | Minimum signal score to open a position |
| `stop_loss` | 6% | 3–12% | Hard stop loss from entry price |
| `take_profit` | 10% | 5–20% | Take profit target |
| `trailing` | 3% | 2–8% | Trailing stop from high-water mark |
| `max_positions` | 8 | 3–15 | Maximum simultaneous open positions |

Example: `/set threshold 70` or `/set trailing 4`

Parameters auto-adjust based on market conditions (Fear & Greed Index + VIX) via adaptive rebalancing. Manual overrides via `/set` persist across deploys.

---

## Options

| Command | Description |
|---------|-------------|
| `/vixalert` | Show VIX put-selling alert status and configuration |
| `/vixalert check` | Manually scan for put premiums now (regardless of VIX level) |

When VIX crosses the threshold (default: 33), the bot automatically sends put-selling setups for GOOG, NVDA, AMZN, and META — including strike, expiry, and estimated premium.

---

## Backtesting

The bot continuously logs all signal evaluations to `signal_log.jsonl` during market hours. This data powers the backtest engine.

### In-Bot Command

| Command | Description |
|---------|-------------|
| `/backtest` | Run a 10-day backtest with current bot parameters |
| `/backtest 30` | Specify look-back period (1–60 days) |
| `/backtest 10 tp=8 sl=5` | Custom take-profit (8%) and stop-loss (5%) |
| `/backtest 10 trail=2.5 threshold=80` | Custom trailing stop and signal threshold |
| `/backtest 10 max_pos=5` | Limit maximum simultaneous positions |

Overridable parameters: `tp`, `sl`, `trail`, `threshold`, `max_pos`. Values > 1 are treated as percentages (e.g., `tp=10` = 10%).

The replay engine uses logged signal scores (no API calls needed) and applies your custom trading rules. Generates a dark-themed PDF report with equity curve, KPIs, trade stats, exit reasons, drawdown, per-ticker P&L, and best/worst trades.

### Standalone Script

```bash
python backtest.py                    # default 30 trading days
python backtest.py --days 60          # custom look-back period
python backtest.py --capital 50000    # custom starting capital
```

The standalone script fetches historical data from APIs (yfinance, Finnhub) and simulates the full signal engine. Outputs `backtest_report.pdf`.

### Signal Logger

Every scan cycle, the bot logs a complete snapshot per ticker to `signal_log.jsonl`:
- All indicator values (RSI, Bollinger, MACD, volume, squeeze, slope, SMA5/20, AVWAP, news sentiment, AI signals)
- Composite score and signal detail
- Market context (Fear & Greed, VIX, active threshold, trading session)
- BUY and SELL actions with full trade details

Auto-trimmed to 30 days on morning reset. At ~3 MB/day, 30 days of data provides ~60,000 signal snapshots.

---

## AI & Research Tools

| Command | Description |
|---------|-------------|
| `/aistocks` | AI-curated stock picks with conviction scores (1–10) and category tags. Refreshes 4x daily |
| `/ask <question>` | Free-form chat with Claude. Supports multi-turn conversation |
| `/prep` | AI-generated prep for the next trading session: key levels, events, strategy |
| `/wlprep` | Deep scan of your watchlist with AI analysis per ticker |

---

## TradersPost / Shadow Trading

These commands are available on the separate TP bot. If no separate TP bot token is set, they fall back to the main bot.

| Command | Description |
|---------|-------------|
| `/shadow` | Toggle shadow mode on/off. When ON, paper trades are mirrored to TradersPost |
| `/tp` | TradersPost status: mode, webhook, orders sent/success/failed, shadow portfolio summary |
| `/tppos` | Shadow portfolio positions with P&L |
| `/settlement` | T+1 settlement status: settled cash, unsettled funds, pending items |
| `/tpsync reset` | Reset shadow portfolio to match paper portfolio |
| `/tpsync status` | Side-by-side comparison of paper vs. shadow positions |
| `/tpedit add TICK QTY PRICE` | Manually add a position to the shadow portfolio |
| `/tpedit remove TICK` | Remove a position from the shadow portfolio |
| `/tpedit shares TICK QTY` | Adjust share count for a shadow position |
| `/tpedit cash AMOUNT` | Set shadow portfolio cash balance |
| `/tpedit clear` | Clear the entire shadow portfolio |

---

## Bot Management

| Command | Description |
|---------|-------------|
| `/list` | Show all monitored tickers (core + dynamically added) |
| `/set` | View or adjust trading configuration |
| `/monitoring pause` | Pause spike scanning (scheduled reports continue) |
| `/monitoring resume` | Resume spike scanning |
| `/monitoring status` | Show scanner status |
| `/version` | Show current version and release notes |
| `/help` | Full command menu |

---

## Scheduled Automated Messages

All times are Central Time (CT).

| Time | Day | Message |
|------|-----|---------|
| 7:00 AM | Weekdays | AI watchlist refresh (pre-market mode) |
| 7:05 AM | Weekdays | Daily candle data refresh |
| 8:00 AM | Weekdays | Pre-market dashboard |
| 8:30 AM | Weekdays | Morning briefing + dynamic ticker merge |
| 8:31 AM | Weekdays | Paper trading morning report |
| 10:30 AM | Weekdays | AI watchlist refresh (intraday) |
| 12:00 PM | Weekdays | Midday dashboard |
| 12:30 PM | Weekdays | AI watchlist refresh (intraday) |
| 2:30 PM | Weekdays | AI watchlist refresh (intraday) |
| 3:00 PM | Weekdays | Daily close summary |
| 3:01 PM | Weekdays | Paper trading end-of-day report |
| 3:05 PM | Weekdays | Signal effectiveness analysis |
| 4:05 PM | Weekdays | Daily P&L summary |
| 6:00 PM | Weekdays | Evening recap |
| 9:00 AM | Saturday | Weekend prep session |
| 6:00 PM | Sunday | Weekly digest |
