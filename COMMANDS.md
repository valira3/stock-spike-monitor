# Command Reference

All commands are registered on both the main bot and the TP bot unless noted. The main bot routes to the paper portfolio; the TP bot routes to the Robinhood mirror portfolio. `/dashboard` shows both portfolios on either bot.

Aliases `/positions` and `/or_now` remain registered but are not shown in the Telegram `/` menu.

---

## Status and Monitoring

| Command | Args | Description |
|---------|------|-------------|
| `/dashboard` | — | Full snapshot: portfolio (cash, positions, day P&L), index filters (SPY/QQQ vs PDC), today's OR levels for every ticker. |
| `/status` | — | Open long and short positions with live prices, unrealized P&L per position, stop levels, day P&L, portfolio allocation pie chart. |
| `/positions` | — | Alias for `/status`. |
| `/perf` | `[date \| N]` | Performance stats: win rate, avg win/loss, streak, long vs short breakdown. No arg = all-time. `7` = last 7 days. `Apr 17` = single day. |
| `/mode` | — | Current MarketMode classification (OPEN, MOMENTUM, CHOP, DEFENSIVE, etc.), breadth/RSI observer readings, and mode profile. Observation-only in v3.4.37 — no parameters are read from it yet. |
| `/monitoring` | `[pause \| resume]` | Show scanner status, or pause/resume new-entry scanning. Position management (stops, trailing) continues while paused. No arg = show status. |
| `/proximity` | — | Read-only diagnostic showing each ticker's current price gap to OR_High (long), OR_Low (short), and PDC. Includes SPY/QQQ polarity check. Refreshable via inline button. |

---

## Positions and Trades

| Command | Args | Description |
|---------|------|-------------|
| `/log` | `[date]` | Completed trades (entries and exits) in chronological order for the given date. Default = today. Accepts `YYYY-MM-DD` or natural formats like `Apr 17`. |
| `/replay` | `[date]` | Trade timeline with running cumulative P&L. Same date parsing as `/log`. |
| `/dayreport` | `[date]` | Completed trades with P&L summary and a per-trade bar chart. Default = today. |
| `/eod` | — | Re-send the EOD report for today on demand. |
| `/trade_log` | — | Last 10 rows from the persistent append-only `trade_log.jsonl` file. |
| `/near_misses` | — | Recent breakouts that cleared the price condition but were declined by the volume gate. Diagnostic only — no entries were made. |

---

## Configuration and Control

| Command | Args | Description |
|---------|------|-------------|
| `/reset` | — | Interactive reset with an inline confirm button (60-second expiry). Resets portfolio to $100,000. |
| `/retighten` | — | Force-run the 0.75% stop cap and breakeven ratchet across every open position right now. Positions with stops already breached by the retightened level are exited immediately (`RETRO_CAP`). |
| `/rh_sync` | — | **TP bot only.** Robinhood broker sync status: webhook enabled/disabled, orders sent/OK/failed, open Robinhood long and short positions, recent webhook outcomes, unsynced exits needing manual reconciliation. On the main bot, `/rh_sync` redirects to the TP bot. Also available as `/tp_sync` (alias). |

---

## Ticker Management

The primary interface is `/ticker`. The standalone commands are back-compat aliases — they still work but are not in the Telegram menu.

| Command | Args | Description |
|---------|------|-------------|
| `/ticker` | — | Show the current tracked universe (same as `list`). |
| `/ticker list` | — | Show tracked tickers and pinned index tickers (SPY, QQQ). |
| `/ticker add SYM` | `SYM` | Add a ticker to the tradeable universe. Primes PDC, OR, RSI, and 1-minute bars. |
| `/ticker remove SYM` | `SYM` | Remove a ticker. SPY and QQQ are pinned and cannot be removed. |
| `/tickers` | — | Alias: show current ticker list. |
| `/add_ticker SYM` | `SYM` | Alias for `/ticker add SYM`. |
| `/remove_ticker SYM` | `SYM` | Alias for `/ticker remove SYM`. |

---

## Market Data

| Command | Args | Description |
|---------|------|-------------|
| `/price TICK` | `TICK` | Live quote from Yahoo Finance for any ticker. Shows current price, long/short entry eligibility (vs PDC and OR levels). |
| `/orb` | `[recover]` | Today's OR_High, OR_Low, and PDC for every tracked ticker. `/orb recover` re-collects any missing ORs (equivalent to `/or_now`). |
| `/or_now` | — | Manually re-collect OR data for tickers missing `or_high`. Alias: `/orb recover`. |

---

## Strategy Reference

| Command | Description |
|---------|-------------|
| `/strategy` | Compact inline strategy summary: long and short entry conditions, stop and ladder for both sides, Eye-of-the-Tiger exits, Regime Shield. |
| `/algo` | Algorithm summary (same content as `/strategy`) plus the full `StockSpikeMonitor_Algorithm_v3.4.37.pdf` sent as a document. PDF is fetched from the repo if not present locally. |
| `/version` | Current bot version (`v3.4.37`) and release notes. |

---

## System and Debug

| Command | Description |
|---------|-------------|
| `/help` | Full command menu grouped by category, rendered in monospace code block. |
| `/menu` | Quick tap-grid of daily-use commands as inline buttons. Includes an "Advanced" button for less-common commands. |
| `/test` | Health check: runs `_fire_system_test()`, checks OR data, PDC data, 1-minute bars for all tickers, and reports any gaps. |

---

## Command Groups Summary

```
Portfolio
  /dashboard        Full snapshot
  /status           Positions + P&L
  /perf [date]      Performance stats

Market Data
  /price TICK       Live quote
  /orb              Today's OR levels
  /orb recover      Recollect missing ORs
  /proximity        Gap to breakout

Reports
  /dayreport [date] Trades + P&L
  /log [date]       Trade log
  /replay [date]    Timeline
  /eod              EOD report

System
  /monitoring       Pause/resume scan
  /test             Health check
  /mode             Market regime
  /menu             Quick tap menu

Reference
  /strategy         Strategy summary
  /algo             Algorithm PDF
  /version          Release notes

Admin
  /reset            Reset portfolio
  /retighten        Force-cap all stops
  /near_misses      Recent declined breakouts
  /trade_log        Last 10 persistent log rows
  /rh_sync          Robinhood sync (TP bot)
  /tp_sync          (alias: /rh_sync)
  /ticker list/add/remove   Ticker universe
```

---

## Scheduled Automated Messages

All times ET. Display in CDT where noted.

| Time ET | Day | Message |
|---------|-----|---------|
| 09:20 | Weekdays | System test (8:20 CT) |
| 09:31 | Weekdays | System test (8:31 CT) |
| 09:35 | Weekdays | OR collection (`collect_or()`) |
| 09:36 | Weekdays | OR morning card — OR_High, OR_Low, PDC for all tickers |
| 15:55 | Weekdays | EOD force-close all positions |
| 15:58 | Weekdays | EOD report |
| 18:00 | Sunday | Weekly digest |
