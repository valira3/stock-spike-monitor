# Architecture

Stock Spike Monitor is a single-file Python application (`stock_spike_monitor.py`, ~8,600 lines) that combines real-time market scanning, paper trading, AI analysis, and Telegram bot interaction into one process.

---

## High-Level Overview

```
┌──────────────────────────────────────────────────────────┐
│                    Main Process                          │
│                                                          │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │  Scheduler   │  │  Main Bot    │  │   TP Bot       │  │
│  │  (Thread)    │  │  (Async)     │  │   (Async)      │  │
│  │             │  │              │  │                │  │
│  │ • 1-min scan │  │ • 40+ cmds   │  │ • Shadow cmds  │  │
│  │ • Scheduled  │  │ • Charts     │  │ • Settlement   │  │
│  │   reports    │  │ • AI queries │  │ • Sync tools   │  │
│  │ • Adaptive   │  │              │  │                │  │
│  │   rebalance  │  │              │  │                │  │
│  └──────┬──────┘  └──────┬───────┘  └───────┬────────┘  │
│         │                │                   │           │
│         ▼                ▼                   ▼           │
│  ┌──────────────────────────────────────────────────┐    │
│  │              Shared State (in-memory)             │    │
│  │  paper_positions, paper_cash, tp_state,           │    │
│  │  price_history, squeeze_scores, ai_watchlist      │    │
│  └────────────────────┬─────────────────────────────┘    │
│                       │                                  │
│                       ▼                                  │
│  ┌──────────────────────────────────────────────────┐    │
│  │         paper_state.json (Railway Volume)         │    │
│  └──────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────┘
         │              │              │
         ▼              ▼              ▼
   ┌──────────┐  ┌───────────┐  ┌──────────────┐
   │ Finnhub  │  │ Claude AI │  │ TradersPost  │
   │ (quotes) │  │ (Sonnet/  │  │ (webhook)    │
   │          │  │  Haiku)   │  │              │
   │ FMP      │  │           │  │ Robinhood    │
   │ (movers) │  │ Grok      │  │ (via TP)     │
   │          │  │ (fallback)│  │              │
   │ yfinance │  │           │  │              │
   │ (charts) │  │           │  │              │
   └──────────┘  └───────────┘  └──────────────┘
```

---

## Core Components

### 1. Scheduler Thread

A background thread that wakes every 30 seconds and manages two types of work:

**Continuous scanning (every ~60 seconds during market hours):**
- `check_stocks()` — Fetches quotes for all monitored tickers via Finnhub, checks for spike alerts, runs paper trading evaluations, and checks VIX put-selling conditions.

**Scheduled jobs (timezone-aware, CT):**
- Defined as `(day, "HH:MM", function)` tuples
- `"daily"` = weekdays only (Mon–Fri)
- `"everyday"` = all 7 days
- `"saturday"`, `"sunday"` = specific days
- See [COMMANDS.md](COMMANDS.md#scheduled-automated-messages) for the full schedule

### 2. Finnhub Rate Limiter

All Finnhub API calls go through a thread-safe token-bucket rate limiter:

- **Capacity:** 55 calls/minute (API limit is 60; 5-call safety margin)
- **Behavior:** Blocks until a token is available, with a 30-second timeout
- **429 handling:** Routine Finnhub 429 errors (a few per cycle) are normal and logged at debug level

A TTL cache sits in front of the rate limiter:
- **Quote cache:** 55-second TTL, 500 entries
- **Metrics cache:** 300-second TTL, 300 entries

### 3. AI Integration

Two-tier AI setup for cost optimization:

| Model | Use Case | Token Limit |
|-------|----------|-------------|
| Claude Sonnet | Deep analysis (`/analyze`, `/compare`, `/ask`, briefings, macro) | 300–2000 |
| Claude Haiku | High-frequency scoring (signal direction, spike alerts, dashboard one-liners, news sentiment) | 60–300 |
| Grok (fallback) | Only used if `ANTHROPIC_API_KEY` is not set | Varies |

The `get_ai_response()` function handles model selection, retries, and fallback:
```
get_ai_response(prompt, system=None, max_tokens=300, fast=False)
  fast=False → Claude Sonnet (deep)
  fast=True  → Claude Haiku (quick)
  Both fail  → Grok fallback
```

### 4. Paper Trading Engine

**Capital:** $100,000 simulated starting balance

**Buy Logic:**
1. `compute_paper_signal(ticker)` generates an 11-factor composite score (0–150)
2. If score ≥ adaptive threshold (default 65) and RSI < 72 and cash available → BUY
3. Position size scales with signal strength: stronger signals get larger allocations
4. Max 20% of portfolio per position, max 8 simultaneous positions

**AVWAP Entry Gate:**
During regular trading hours, the bot only opens new positions when price is above the Anchored VWAP (session-anchored to 9:30 AM ET open). This filters out entries into overhead supply zones. Gate is skipped in extended hours or when AVWAP data is unavailable.

**Sell Logic (checked every scan cycle):**
- **Take Profit:** +10% from entry (adaptive: widens in low-fear markets)
- **Trailing Stop:** -3% from high-water mark (adaptive)
- **Hard Stop:** -6% from entry (adaptive: tightens in high-fear markets)
- **AVWAP Stop:** Exit if price drops below AVWAP after having reclaimed it ("price lost AVWAP — overhead supply")

**Adaptive Rebalancing:**
Every 30 minutes, the bot adjusts trading parameters based on:
- Fear & Greed Index (0–100)
- VIX level
- Thresholds widen in calm markets (let winners run), tighten in volatile markets (protect capital)

### 5. 11-Factor Signal Scoring

Maximum score: 150 points. Components:

| # | Component | Max Pts | Source |
|---|-----------|---------|--------|
| 1 | RSI Momentum | 20 | Intraday price history (14-period RSI) |
| 2 | Bollinger Band Position | 15 | %B position within bands |
| 3 | MACD Crossover | 15 | MACD line vs. signal line |
| 4 | Volume Confirmation | 15 | Current volume vs. 10-day average |
| 5 | Squeeze Score | 10 | Bollinger bandwidth + price trend + short interest |
| 6 | Price Slope | 10 | Linear regression slope of last 10 ticks |
| 7 | AI Direction | 15 | Claude Haiku BUY/HOLD/AVOID + confidence |
| 8 | AI Watchlist Conviction | 10 | Bonus if ticker is on AI watchlist with conviction ≥ 7 |
| 9 | Multi-Day Trend | 15 | SMA alignment (6) + 5-day momentum (5) + daily volume trend (4) |
| 10 | News Sentiment | 15 | AI-scored headline sentiment |
| 11 | AVWAP | 10 (-5) | Anchored VWAP: +10 if price comfortably above, +6 if just above, -5 if below (overhead supply penalty) |

### 6. Persistent Signal Logger

Every time `compute_paper_signal()` runs (each scan cycle per ticker), a complete snapshot is appended to `signal_log.jsonl`:

```json
{"type": "signal", "ts": "2026-03-14T10:31:22", "ticker": "NVDA",
 "score": 87.5, "rsi": 58.2, "pct_b": 0.72, "macd": 0.0034,
 "vol_ratio": 1.8, "squeeze": 45, "slope_pct": 0.21,
 "avwap": 142.50, "pct_from_avwap": 1.2, "reclaimed": true,
 "ai_signal": "BUY", "ai_confidence": 75,
 "news_sentiment": "bullish", "fg_index": 35, "vix": 22.1,
 "threshold": 65, "session": "regular", ...}
```

BUY and SELL actions are also logged with full trade details (shares, cost, P&L, exit reason).

- **Storage:** JSONL (append-only), ~3 MB/day
- **Retention:** Auto-trimmed to 30 days on morning reset
- **Location:** Same Railway Volume mount as `paper_state.json`

### 7. Backtesting Engine

**In-bot (`/backtest` command):**
Replays logged signal data from `signal_log.jsonl` with custom trading parameters. No API calls needed — uses the exact scores and prices that were recorded live. Generates a 2-page dark-themed PDF report with equity curve, KPIs, trade statistics, exit reason breakdown, drawdown chart, per-ticker P&L, and best/worst trades.

**Standalone (`backtest.py` script):**
Fetches historical data from yfinance/Finnhub and simulates the full signal engine from scratch. Useful for backtesting periods before signal logging was enabled. Outputs `backtest_report.pdf`.

Both engines support custom parameters: take-profit, stop-loss, trailing stop, signal threshold, and max positions.

### 8. Shadow Trading (TradersPost)

When shadow mode is ON, every paper trade triggers a webhook POST to TradersPost:

```
Paper BUY  → POST webhook { ticker, action: "buy", ... }
Paper SELL → POST webhook { ticker, action: "exit", ... }
```

The shadow portfolio tracks what TradersPost/Robinhood should hold, independent of paper positions. The `/tpsync` command can reconcile the two.

**T+1 Settlement Tracking:**
Since the linked Robinhood account is a cash account (no margin), sells don't settle until the next business day. The bot tracks:
- Pending settlements with expected settle dates
- Settled vs. unsettled cash
- Available buying power (settled cash only)

### 9. Telegram Bot Architecture

Two bot instances run in the same process:

| Bot | Purpose | Commands |
|-----|---------|----------|
| Main Bot | All market/paper/analysis commands | 40+ commands |
| TP Bot | TradersPost-specific commands via DM | `/shadow`, `/tp`, `/tppos`, `/settlement`, `/tpsync`, `/tpedit`, `/paper`, `/set`, `/start`, `/help` |

Both bots use `python-telegram-bot`'s async polling. They share the same in-memory state and paper_state.json file.

**Message handling:**
- `send_telegram()` splits messages >3,800 chars into multiple parts
- Exponential backoff on Telegram API rate limits
- Charts sent as documents (not photos) for crisp rendering on mobile

---

## Data Flow

### Scan Cycle (every ~60 seconds)

```
check_stocks()
  │
  ├── For each ticker:
  │     ├── fetch_finnhub_quote(ticker) → (price, volume, change%)
  │     ├── Update price_history deque
  │     ├── Check spike threshold (3%+ change)
  │     │     └── If spike → AI analysis → send_telegram() alert
  │     ├── Check custom price alerts
  │     └── paper_evaluate_ticker(ticker)
  │           ├── compute_paper_signal() → 11-factor score
  │           ├── log_signal_data() → append to signal_log.jsonl
  │           ├── Check sell conditions (TP/SL/trailing/AVWAP-stop)
  │           │     └── If sell → update positions, log, notify, webhook
  │           └── Check buy conditions (signal ≥ threshold + AVWAP gate)
  │                 └── If buy → size position → execute
  │
  ├── check_vix_put_alert()
  │     └── If VIX > 33 → estimate put premiums → alert
  │
  └── Update squeeze_scores for all tickers
```

### State Persistence

All mutable state is stored in `paper_state.json`:

```json
{
  "paper_cash": 97543.21,
  "paper_positions": { "NVDA": { "shares": 5, "cost": 142.50, ... } },
  "paper_all_trades": [ ... ],
  "paper_trades_today": [ ... ],
  "paper_daily_counts": { "2026-03-14_NVDA_buy": 1 },
  "user_config": {
    "stop_loss": 0.06,
    "take_profit": 0.10,
    "trailing": 0.03,
    "max_positions": 8,
    "threshold": 65,
    "trading_mode": "shadow"
  },
  "tp_state": {
    "pending_settlements": [ ... ],
    "total_orders_sent": 42,
    "shadow_portfolio": { ... },
    "recent_orders": [ ... ]
  },
  "custom_alerts": { "NVDA": [150.0, 160.0] },
  "watchlists": { "12345": ["AAPL", "TSLA"] }
}
```

**signal_log.jsonl** (append-only, auto-trimmed to 30 days):
```json
{"type": "signal", "ts": "...", "ticker": "NVDA", "score": 87.5, ...}
{"type": "buy", "ts": "...", "ticker": "NVDA", "shares": 5, "cost": 142.50, ...}
{"type": "sell", "ts": "...", "ticker": "NVDA", "pnl": 215.00, "reason": "trailing_stop", ...}
```

State is saved atomically (write to `.tmp` then `os.replace()`) after every trade, config change, or significant state mutation.

---

## External APIs

| API | Purpose | Rate Limit | Auth |
|-----|---------|------------|------|
| Finnhub | Real-time quotes, metrics, short interest | 60/min (55 used) | API key |
| FMP | Movers, gainers, losers, earnings calendar | Varies by plan | API key |
| yfinance | Historical candles, chart data | No hard limit | None |
| Anthropic | Claude Sonnet + Haiku for AI analysis | Token-based billing | API key |
| xAI | Grok (fallback) | Token-based billing | API key |
| TradersPost | Trade mirroring via webhook | N/A | Webhook URL |
| Telegram | Bot commands + notifications | ~30 msg/sec | Bot token |

---

## Monitoring & Observability

- **Logging:** Dual handler — file (`stock_spike_monitor.log`) + stdout
- **Investment log:** Separate file (`investment.log`) for all paper trades
- **Railway logs:** All stdout/stderr visible in Railway dashboard
- **Health indicators in logs:**
  - `"Scanning X stocks"` every ~60 seconds = healthy
  - Finnhub 429 errors (a few per cycle) = normal
  - Missing scan messages for 5+ minutes = problem
  - Python tracebacks or ERROR-level messages = investigate
