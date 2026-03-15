# Changelog

All notable changes to Stock Spike Monitor.

---

## v1.19 — Cash Account (2026-03-14)

- Removed PDT (Pattern Day Trader) tracker — no longer needed with cash account
- Removed drift detection between paper and shadow portfolios
- Added T+1 settlement tracking for cash account
  - `record_settlement()` tracks unsettled funds from sells
  - `get_settled_cash()` returns settled vs. unsettled balances
  - `/settlement` command shows settlement status
- Replaced `/pdt` command with `/settlement`
- Updated `/start`, `/shadow`, and `/tp` displays to show settlement info

## v1.18 — VIX Put-Selling Alert (2026-03-14)

- Added automatic VIX put-selling alerts when VIX crosses threshold (default: 33)
- Estimates put premiums on GOOG, NVDA, AMZN, META using Black-Scholes approximation
- Suggests OTM strikes (~3% below current price) with 3-week expiry
- New `/vixalert` command to view status and configuration
- New `/vixalert check` to manually trigger a scan regardless of VIX level
- Runs automatically every scan cycle during market hours

## v1.17 — Full Channel Separation (2026-03-13)

- TradersPost commands exclusive to the TP bot — no longer registered on main bot when TP token is set
- Cleaner command separation between market analysis (main bot) and trade management (TP bot)

## v1.16 — Separate Telegram Channel (2026-03-12)

- Added support for a separate Telegram bot token for TradersPost notifications
- TP bot runs alongside main bot in the same process
- Both bots share state and paper trading engine

## v1.15 — Shadow Portfolio Tracker (2026-03-11)

- Shadow portfolio tracks what TradersPost/Robinhood should hold
- `/tpsync reset` resets shadow to match paper portfolio
- `/tpsync status` shows side-by-side comparison of paper vs. shadow positions
- `/tpedit` command for manual shadow portfolio adjustments (add, remove, shares, cash, clear)

## v1.14 — Shadow Mode (2026-03-10)

- TradersPost webhook integration for live trade mirroring
- Shadow mode toggle (`/shadow`) to enable/disable trade forwarding
- `/tp` status command showing orders sent, success rate, portfolio summary
- Webhook sends BUY and EXIT signals with ticker, action, and signal metadata

## v1.13 — Adaptive Trading (2026-03-09)

- All trading parameters auto-adjust to market conditions
- Fear & Greed Index + VIX drive adaptive rebalancing every 30 minutes
- Parameters widen in calm markets, tighten in volatile markets
- `/set` command for manual overrides that persist across deploys
- User config saved to paper_state.json

## v1.12 — Extended Hours Paper Trading (2026-03-08)

- Portfolio, positions, and sell logic now use live pre-market and after-hours prices from yfinance
- Trailing stops and take-profit evaluated against extended-hours prices
- More accurate portfolio valuation outside regular trading hours

## v1.11 — Smart Trading (2026-03-07)

- Trailing stops (3% from high-water mark)
- Adaptive thresholds based on market conditions
- Sector guards to limit exposure
- Earnings filter — avoids buying stocks reporting earnings within 2 days
- `/perf` performance dashboard with win rate, avg gain/loss, Sharpe-like metric
- `/set` command to view and change trading configuration
- Signal learning: tracks signal effectiveness over time
- Support/resistance level awareness
- `/paper chart` for intraday portfolio value visualization
- Daily P&L summary at 4:05 PM CT

## v1.10 — News Sentiment Scoring (2026-03-06)

- AI-powered news sentiment analysis (component 10/10 in signal engine, up to 15 pts)
- `/news TICK` shows sentiment scores and source timestamps
- Claude Haiku scores headlines as bullish/neutral/bearish with confidence
- Integrated into the composite trading signal

## v1.9 — Extended Hours Pricing (2026-03-05)

- Pre-market and after-hours prices from yfinance
- Dashboard and `/price` quotes show live extended session data
- Trading session detection (pre-market, regular, after-hours, closed)

## v1.8 — Dashboard Sharpness (2026-03-04)

- 220 DPI rendering for crisp charts on mobile
- Larger fonts throughout dashboard
- Sent as Telegram document (not compressed photo) for full resolution

## v1.7 — Alert Spam Fix (2026-03-03)

- 15-minute cooldown between alerts for the same ticker
- 1% escalation threshold — re-alerts only if move increases by 1%+ beyond last alert
- Startup grace period (300 seconds) prevents false alerts on boot

## v1.6 — Chart & RSI (2026-03-02)

- `/chart TICK` command using yfinance data (replaced Finnhub candles)
- `/rsi TICK` command showing RSI, Bollinger Bands, bandwidth, squeeze score
- VWAP crash fix

## v1.5 — Startup Rate Fix (2026-03-01)

- Removed duplicate scan on boot
- Eliminated 75+ Finnhub 429 errors that occurred at startup

## v1.4 — Multi-Day Trends (2026-02-28)

- 5-day SMA trend + momentum + volume component (15 pts)
- Signal component 9/10 for longer-term trend confirmation
- Daily candle data loaded from yfinance

## v1.3 — Paper Trading Boost (2026-02-27)

- Day-change MOVER alerts for significant overnight gaps
- Price history primed on startup (fills deques before first scan)
- Signal cache TTL increased to 120 seconds

## v1.2 — Crypto & Batching (2026-02-26)

- Rewritten `/crypto` command with live BTC, ETH, SOL, DOGE, XRP
- TTL caching layer for all API responses
- Batch scanning for efficient ticker processing
- Wider dashboard layout

## v1.1 — Mobile & AI Watchlist (2026-02-25)

- Compact `/help` menu optimized for mobile (64-char width)
- Mobile-friendly dashboard layout
- AI-driven watchlist rotation with conviction scores
- `/aistocks` command for AI picks

## v1.0 — Initial Release (2026-02-24)

- 30-stock scanner polling Finnhub every 60 seconds
- 3%+ spike alerts via Telegram
- $100,000 paper trading portfolio
- Automated buy/sell based on signal scoring
- Claude AI integration for stock analysis
- `/overview`, `/price`, `/analyze`, `/compare`, `/movers`, `/earnings`, `/macro`
- `/paper` portfolio management commands
- `/ask` free-form AI chat
- Morning briefing, close summary, weekly digest
