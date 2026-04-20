# Changelog

All notable changes to Stock Spike Monitor.

---

## v3.2.0 — Dual-Index Confluence Shield (2026-04-20)

Tightens the global eject signal (`LORDS_LEFT` / `BULL_VACUUM`) to fire only
on a **market-systemic** move, not a sector-specific wick. Historically a
1-minute close on either SPY *or* QQQ below AVWAP was enough to flip the
trigger — that produced Flim-Flam noise during sector divergence and
sub-5-min liquidity probes ("Hormuz wicks"). This release requires
Confluence (AND) across both indices and confirmation on a finalized 5-min
bar close before abandonment.

**Rule change**
- Old (v2.9.8 → v3.1.4): `SPY_1m < AVWAP` **OR** `QQQ_1m < AVWAP` → eject longs.
- New (v3.2.0): `SPY_5m_close < SPY_AVWAP` **AND** `QQQ_5m_close < QQQ_AVWAP`
  on the most recently **finalized** 5-min bar → eject longs.
- Mirror for shorts (both indices' 5m close *above* AVWAP).
- If either index reclaims its AVWAP before the 5m bar finalizes, the
  eject is suppressed for that bar.

**Fail-safe**
- Any missing data (fetch failure, < 5 min elapsed, AVWAP not seeded) →
  helper returns `False` → **stay in the trade**. Ambiguity never forces an
  exit.

**Implementation**
- New `_last_finalized_5min_close(ticker)` — reuses `_resample_to_5min`,
  which already drops the in-progress (newest) bucket.
- New `_dual_index_eject(side)` — 'long' / 'short' gate returning bool.
- Four call sites switched: `manage_positions`, `manage_tp_positions`
  (long side + TP loop), `manage_short_positions` (main + TP loop).
- Exit reason keys now emit `LORDS_LEFT[5m]` and `BULL_VACUUM[5m]`.
  Legacy `[1m]` keys preserved in `REASON_LABELS` so historical `/replay`
  and `/log` renders still format correctly.
- `/algo` and `/strategy` text updated to describe AND + 5m confluence.
- 6 new deterministic unit tests in `/tmp/test_observers.py` covering:
  (1) long both below → True, (2) long only SPY below → False,
  (3) short both above → True, (4) short only QQQ above → False,
  (5) missing AVWAP / bar data → False, (6) invalid `side` → False.

**Unchanged**
- Hard stops, trailing stops (min $1.00), RED_CANDLE, POLARITY_SHIFT,
  DAILY_LOSS_LIMIT, min-1-share floor, entry logic, position sizing.
- MarketMode observers still observation-only (no adaptive param yet).

---

## v3.1.4 — /menu Main + Advanced Submenu (2026-04-18)

v3.1.3's 17-button grid felt cluttered and some labels truncated on mobile.
Split into a lean main menu and an Advanced submenu so the daily-use stuff
is one tap and everything else is two.

**Main /menu (10 tiles, 2 columns)**
- Dashboard, Status
- Perf, Price
- OR, Day Report
- Mode, Help
- Monitor (full width)
- Advanced (full width, opens submenu)

**Advanced submenu (8 tiles + Back)**
- Log, Replay
- OR Recover, Test
- Strategy, Algo
- Version, Reset
- ⬅️ Back (returns to main)

**Implementation**
- New `_build_advanced_menu_keyboard()` alongside `_build_menu_keyboard()`.
- `menu_advanced` callback edits the existing menu message in place to swap
  keyboards (no new messages, clean UX).
- `menu_back` callback does the reverse.
- All nine command-executing callbacks from v3.1.3 still work; they're just
  reachable from either menu depending on placement.
- No button callbacks removed — only regrouped.

No behavior changes to scanning, entries, exits, sizing, or observers.

---

## v3.1.3 — /menu Covers Every /help Command (2026-04-18)

Makes the `/help` ↔ `/menu` split useful: `/help` is the polished reference
(non-tappable monospace), `/menu` is the tap grid that covers **every single
command** listed in `/help`.

**New buttons** (in addition to the 10 that were already there):
- Perf, Mode, Log, Replay, OR Recover, Algo, Help, Reset — 8 new taps.
- Total grid: 17 buttons across 7 rows, grouped portfolio → market data →
  reports → system → reference → admin.

**Taps now execute the command**
- Previously `menu_dayreport` and `menu_perf` just echoed "Use /dayreport"
  instead of running the command. Now they actually run it.
- New `_CallbackUpdateShim` + `_invoke_from_callback` helper forwards a
  callback_query through any `cmd_*` handler by faking the Update fields the
  handlers touch (`message`, `effective_message`, `effective_user`,
  `effective_chat`). Keeps the helpers reusable for future tap-button work.
- `context.args` is scoped per invocation and restored after, so passing a
  date through the shim wouldn't leak across taps.
- `/reset` tap delegates to `cmd_reset`, which runs the same two-step
  confirmation flow as the typed command — no accidental resets from a tap.

**/help footer**
- Added one-line tip: `Tip: /menu for tap buttons`. Still within the 33-char
  mobile-code-block width limit.

No behavior changes to scanning, entries, exits, sizing, or observers.

---

## v3.1.2 — /help Rendering Fix (2026-04-18)

Cosmetic fix. Telegram renders regular text in a proportional font, so the
column alignment in v3.1.1's `/help` didn't line up on mobile and several
descriptions wrapped awkwardly onto a second line.

- Help body is now wrapped in a Markdown code block, so Telegram renders it
  in monospace and space-padded columns actually align.
- Descriptions trimmed so every line stays ≤ 33 chars and nothing wraps at
  phone widths. Section headers simplified (no emoji, single word per row).
- Removed the horizontal rule separators (the code block provides its own
  visual frame).

No behavior changes to scanning, entries, exits, sizing, or observers.

---

## v3.1.1 — Help Menu Cleanup + Command Consolidation (2026-04-18)

Small UX release. No behavior changes to scanning, entries, exits, sizing, or
observers — purely command surface cleanup.

**/help additions**
- `/status` now listed (was registered but missing from `/help`).
- `/mode` now listed under Market Data (was missing from `/help`).
- `/orb recover` documented — folds in the old `/or_now` as a subcommand.

**Consolidation (backward compatible)**
- `/positions` stays as a silent alias of `/status`. Removed from `/help` and
  from the Telegram / menu to tighten the surface; the command itself still
  works for anyone who has it in muscle memory.
- `/or_now` stays as a silent alias of `/orb recover`. Same treatment —
  removed from `/help` and the Telegram / menu, command still works.
- `/orb` gains `recover` / `recollect` / `refresh` subcommand that dispatches
  to the existing OR-recovery flow.

**Telegram / menu reorganized**
- Grouped by use: portfolio → market data → reports → system → reference →
  admin. Aliases (`/positions`, `/or_now`) dropped from the menu.
- `TP_BOT_COMMANDS = list(MAIN_BOT_COMMANDS)` — single source of truth for
  both bots.

---

## v3.1.0 — MarketMode Observers (2026-04-18)

Adds three observation-only signals on top of v3.0.0 scaffolding.

- **Breadth observer** — SPY/QQQ vs AVWAP with ±0.1% tolerance →
  BULLISH / NEUTRAL / BEARISH.
- **RSI observer** — Wilder RSI(14) on 5-min bars resampled from the existing
  1-min Yahoo feed. Aggregate = mean(SPY, QQQ) → OVERBOUGHT (≥70) /
  NEUTRAL / OVERSOLD (≤30). Plus per-ticker RSI map for all TRADE_TICKERS.
- **Ticker heat** — per-ticker realized P&L today + per-ticker RSI extremes;
  red list (P&L ≤ -$5) and extremes list surfaced in `/mode`.
- **Per-cycle 1-min bar cache** — `fetch_1min_bars` dedupes within a scan
  cycle with a `__FAILED__` negative-cache sentinel, so observers add ~0
  network calls over v3.0.0.
- Each observer lives in its own try/except and short-circuits when
  `mode=CLOSED`. Nothing reads observer state for trading decisions.
- 8 unit tests for `_resample_to_5min` and `_compute_rsi` (Wilder 1978
  reference sample verified at 74.21).

---

## v3.0.0 — MarketMode Scaffolding + Platform Hardening (2026-04-18)

Milestone release rolling up the significant work of the past week. No breaking
changes; all behavior at the trading layer is backward compatible with v2.9.x.

**MarketMode scaffolding (new)**
- Classifier tags each scan cycle as `OPEN` / `CHOP` / `POWER` / `DEFENSIVE` / `CLOSED`.
- Frozen per-mode advisory profiles with hard clamp bounds on every adaptive
  parameter: `trail_pct` ∈ [0.6%, 1.8%], `max_entries` ∈ [1, 5], `shares` ∈ [1, 10],
  `min_score_delta` ∈ [0.00, 0.15]. `_clamp()` is applied at profile construction
  so out-of-range values are impossible.
- Hard floors (`DAILY_LOSS_LIMIT`, min trail distance $1.00, min 1 share) remain
  constants outside the profile system.
- `scan_loop()` logs `mode=<X>` each cycle and `MarketMode: X -> Y (reason)` on
  transitions.
- `/mode` command shows current classification, advisory profile, and bounds.
- **Observation only in v3.0.0** — no entry, exit, sizing, score, or trail code
  reads the profile yet. Observe in production before wiring the first knob.

**Reliability & UX fixes**
- `/replay` historical view: normalize all four sources (`paper_trades`,
  `trade_history`, `short_trade_history`, TP variants) into a common row shape;
  synthesize both open and close rows from each closed-trade record using
  `entry_time`/`entry_price` + `exit_time`/`exit_price`. Past-date replays no
  longer show `--:--` / `$0.00` placeholders.
- `/dayreport`: threaded chart generation + empty-trades guard (was hanging on
  no-trade days); inline `Text must be non-empty` path fixed.
- `/log` and `/replay`: moved sync work to the executor with 15s timeout and a
  loading message. Historical queries read the correct source (`trade_history`,
  not `paper_trades`).
- `/positions`: full equity snapshot (cash, unrealized P&L, total equity,
  vs-start performance) on paper + TP; refresh action with live-price updates;
  trail-stop details for active trails.
- Menu → Dashboard now renders the same full snapshot `/dashboard` produces (was
  a 2-line summary).
- Menu UX: removed auto-menu after every command; replaced with an opt-in
  `[🗂 Menu]` button.
- Silent-crash fix: replaced Python `%` formatting of `+,.2f` with `.format()`;
  added a global error handler so Telegram surfaces failures instead of freezing.

**Platform**
- TP Portfolio independence hardened (shares signals, separate tracking/UI).
- Multi-instance deployment: env-var-driven config for a second `valstradebot`
  instance on a separate Railway service.

---

## v2.7.0 — Full Gap Analysis Implementation (2026-03-17)

Comprehensive upgrade based on deep industry research across quantitative finance
literature and professional systematic trading practices. Implements all 7
recommendations from the gap analysis report.

### 1. ATR-Based Dynamic Stops (Rec #1 — CRITICAL)
- Replaced fixed 3–6% trailing and 6% hard stops with ATR(14)-based dynamic stops.
- Initial hard stop: entry − (ATR × 2.5).
- Trailing stop: highest high − (ATR × multiplier), where multiplier tightens with profit:
  - At entry: 3.0× ATR → At +5%: 2.5× → At +10%: 2.0× → At +15%: 1.5×
- Market regime multiplier applied to stop distances.
- Backward compatible: positions without ATR data fall back to fixed % stops.

### 2. Volatility-Normalized Position Sizing (Rec #2 — CRITICAL)
- Position sizes now based on equal-risk contribution using ATR.
- Risk budget: 1% of portfolio per trade.
- Position size = risk_budget / (ATR × 2.5 stop distance).
- Still applies signal-strength scaling (50–100%), ToD multiplier, and AI boost.
- Falls back to dollar-based sizing if ATR unavailable.

### 3. Portfolio Heat Limit (Rec #3 — HIGH)
- New `_calculate_portfolio_heat()` tracks total risk if all stops hit simultaneously.
- New buys blocked if portfolio heat ≥ 6% of total portfolio value.
- Prevents catastrophic drawdowns in correlated selloffs.
- Heat logged in scan status messages.

### 4. Per-Ticker Re-Entry Cooldown (Rec #4 — HIGH)
- After any SELL, the same ticker is blocked from re-entry:
  - 4 hours after a winning sell.
  - 8 hours after a losing sell.
- Prevents buy→stop→rebuy→stop churn cycle.
- Cooldown tracked per-ticker with `_record_cooldown()` / `_check_cooldown()`.

### 5. Multi-Regime Market Classification (Rec #5 — MEDIUM-HIGH)
- Replaces binary Fear & Greed model with 4-regime system:
  - **trending_up**: SPY > SMA20 > SMA50, VIX < 22 → easier entry, larger positions.
  - **trending_down**: SPY < SMA20 < SMA50 → +10 threshold, smaller positions, tighter stops.
  - **range_bound**: SMAs converged → +5 threshold, slightly smaller.
  - **crisis**: VIX > 30 or SPY < SMA50 by >3% → +15 threshold, half size, very tight stops.
- Regime cached for 15 minutes. Adjusts threshold, max positions, stop multiplier, and sizing.

### 6. Signal Decay / Dynamic Weighting (Rec #6 — MEDIUM)
- New `_recalculate_signal_weights()` analyzes signal_log.jsonl trade outcomes.
- Correlates each signal component (RSI, MACD, etc.) with winning vs losing trades.
- Components that predict winners get up to 1.5× weight; losers down to 0.5×.
- Requires 10+ wins and 5+ losses to activate (defaults to 1.0× until then).
- Recalculated daily during morning reset.

### 7. Correlation-Aware Position Limits (Rec #7 — MEDIUM)
- New `_check_correlation()` calculates 20-day Pearson correlation between
  a candidate ticker and all held positions.
- Blocks entry if 2+ existing positions have correlation > 0.7 with the candidate.
- Catches crypto clustering (BITO + IBIT + MARA) that sector labels miss.
- Daily returns cached for 1 hour to reduce API calls.

### Integration & Infrastructure
- `get_atr()`: New ATR(14) calculation using Finnhub daily candles, 5-min cache.
- Regime + heat + cooldown info logged in scan cycle messages.
- BUY notifications now show ATR-based stop/trail levels.
- SELL notifications include ATR-HARD-STOP and ATR-TRAIL reason types.
- All changes backward compatible with existing position data.

---

## v2.6 — Intraday Time-of-Day Awareness (2026-03-16)

### Signal Score Modifier (Component #12, ±8 pts)
- New `Time-of-Day` component added to the 12-component signal engine (max score now 158).
- Based on the well-documented U-shaped intraday volume/volatility pattern:
  - **Power Open** (9:30–10:30 AM ET): +8 pts — highest volume and volatility, most reliable signals.
  - **Morning** (10:30–11:30 AM ET): +3 pts — still elevated activity.
  - **Transition** (11:30 AM–12:00 PM ET): 0 pts — neutral.
  - **Lunch Lull** (12:00–2:00 PM ET): -8 pts — lowest volume, more false breakouts, less conviction.
  - **Transition** (2:00–3:00 PM ET): -3 pts — volume recovering.
  - **Afternoon** (3:00–3:30 PM ET): +3 pts — building toward close.
  - **Power Close** (3:30–4:00 PM ET): +6 pts — strong close activity, rebalancing flows.
- Naturally raises the effective threshold during lunch and lowers it during power hours.

### Position Sizing by Time Zone
- Position size now scaled by intraday zone:
  - **Power hours** (open/close): 100% of calculated size.
  - **Morning/Afternoon**: 90%.
  - **Transition**: 80–85%.
  - **Lunch Lull**: 65% — even if a signal passes threshold, trade smaller during low-conviction periods.
  - **Extended hours**: 85%.

### Signal Log & BUY Notification
- `signal_log.jsonl` now captures `tod_zone`, `tod_pts`, `tod_size_mult` for backtesting.
- BUY notification shows the time-of-day zone, point adjustment, and size multiplier.

---

## v2.5.1 — TP Portfolio Independence (2026-03-16)

### TP Portfolio is fully independent from Paper
- `/tpsync reset` now wipes all TP positions and restores starting cash ($100k). Previously it cloned the paper portfolio.
- `/tpsync status` shows TP portfolio snapshot on its own (no paper comparison).
- Removed all "shadow" and "mirror" terminology from user-facing messages and comments.
- `/shadow` command now shows "TP Trading: ON/OFF" instead of "Shadow Mode".
- `/tp` mode label now shows "Active" / "Disabled" instead of "Shadow (Paper Mirror)".

---

## v2.5 — TP Portfolio Sync Fix (2026-03-16)

### Cash Guard on BUY
- TP portfolio BUY path now checks available cash before deducting.
- If cost exceeds cash, shares are capped to 95% of available cash.
- If less than 1 share is affordable, the BUY is skipped entirely.
- Prevents TP cash from ever going negative on new buys.

### Failed EXIT Webhook Sync
- When a TradersPost EXIT webhook fails, the TP portfolio now still removes the position and returns proceeds to cash.
- Previously, a failed EXIT left the position in TP while the scanner had already exited — causing cash drift on subsequent buy cycles.

### Negative Cash Warning
- `/tppos` now displays a warning if TP cash is negative, with instructions to fix via `/tpsync reset` or `/tpedit cash`.

---

## v2.4 — Robinhood Hours + Limit Orders (2026-03-16)

### Trading Hours Fix
- Extended session now correctly matches Robinhood: **7:00 AM – 8:00 PM ET**.
- Previous: bot ran 8:00 AM – 9:00 PM ET — missed 1 hour of pre-market and traded 1 hour past Robinhood's close.
- `get_trading_session()` updated: extended = 6:00–19:00 CT (= 7:00 AM–8:00 PM ET).

### All Orders Now Use Limit Pricing
- Every TradersPost order is now a **limit order** instead of market.
- BUY orders: limit price = current price + 0.5% buffer.
- EXIT orders: limit price = current price − 0.5% buffer.
- Eliminates slippage risk and complies with Robinhood's extended-hours rule (market orders rejected during pre/post-market).
- Constants `LIMIT_ORDER_BUY_BUFFER` and `LIMIT_ORDER_SELL_BUFFER` (default 0.5%) are tunable.
- TP notifications now show "LIMIT BUY" / "LIMIT EXIT" with the limit price.
- Order records include `limit_price` for audit trail.

---

## v2.3 — AI Reasoning in Signal Log (2026-03-15)

### Enhanced Signal Logger
- Signal log (`signal_log.jsonl`) now captures `grok_reason` — Claude's text explanation for BUY/HOLD/AVOID calls.
- Signal log now captures `news_catalyst` — the key news catalyst identified by AI sentiment analysis.
- BUY action log entries now include full AI context: `grok_signal`, `grok_reason`, `news_sentiment`, `news_catalyst`, `fg_index`.
- These fields enable future backtests to analyze why AI recommended or avoided specific trades, and to filter by AI sentiment in replay mode.

### Existing Backtest Engine
- `/backtest` already replays from `signal_log.jsonl` with the full AI-scored composite signals.
- Adaptive thresholds (F&G + VIX) are replayed from logged values.
- AVWAP gates, RSI overbought guards, and signal-collapse exits all use logged data.

---

## v2.2 — Graduated Trailing Stop (2026-03-15)

### Exit Strategy Overhaul
- Removed fixed 10% take-profit exit. Winners now ride with a graduated trailing stop that widens as profit grows:
  - `<5% profit`: 3% trail (base)
  - `5–10%`: 4% trail
  - `10–15%`: 5% trail
  - `15%+`: 6% trail (wide, let runners run)
- Hard stop (-6% from entry) remains as a safety net.
- Applied to both paper trading and backtest engine.

### Updated Notifications & Config
- BUY notifications now show graduated trail zones instead of a fixed target price.
- `/set` display shows the graduated trail table. `/set take_profit` now explains the new system.
- Adaptive config no longer adjusts `PAPER_TAKE_PROFIT_PCT` (graduated trail replaces it).

---

## v2.1 — Portfolio Value Fix, Command Menu & TP Bot Cleanup (2026-03-15)

### Bug Fixes
- `/tp` portfolio value now uses live market prices instead of cost basis (avg_price). Previously always showed ~$100,000 regardless of actual market value.
- `post_init` callback for `set_my_commands` wasn't firing in dual-bot mode. Moved command registration inline into `_run_both()`.

### Improvements
- Command menus registered for both private and group chat scopes (`BotCommandScopeAllPrivateChats` + `BotCommandScopeAllGroupChats`).
- Removed `/paper` command from TP bot — TP bot now focuses exclusively on TradersPost trading.
- Renamed all user-visible "Shadow Portfolio" references to "TP Portfolio" throughout the TP bot.
- Updated TP bot welcome, help, and command descriptions to reflect independent trading (not shadow/mirror).

---

## v2.0 — AVWAP, Backtesting & Cash Account (2026-03-15)

Major version bump reflecting three significant feature additions.

### AVWAP Integration
- Added Anchored VWAP (session-anchored to 9:30 AM ET open) as signal component 11/11 (up to 10 pts, or -5 penalty if below)
- AVWAP entry gate: during regular hours, only opens new positions when price is above AVWAP
- AVWAP stop-loss: exits position if price drops below AVWAP after having reclaimed it
- Signal scoring raised from 140 to 150 max points
- BUY notifications now show AVWAP price, % distance, points, and AVWAP stop level

### Backtesting Engine
- Persistent signal logger: every signal evaluation is appended to `signal_log.jsonl` with all 20+ indicator values, composite score, market context (F&G, VIX), and trade actions
- `/backtest` Telegram command: replays logged signal data with custom parameters (tp, sl, trail, threshold, max_pos), generates and sends a dark-themed PDF report
- Report includes: equity curve, KPIs, trade statistics, exit reason breakdown, drawdown chart, per-ticker P&L, best/worst trades
- Signal log auto-trimmed to 30 days on morning reset (~3 MB/day)
- Standalone `backtest.py` script also available for historical backtests using API data

### Cash Account
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
