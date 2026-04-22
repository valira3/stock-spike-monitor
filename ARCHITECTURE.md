# Architecture

Stock Spike Monitor is a single-file Python application (`stock_spike_monitor.py`, ~9,400 lines) that runs an ORB (Opening Range Breakout) long strategy and a Wounded Buffalo short strategy in parallel, manages a paper portfolio and a TradersPost mirror portfolio, and delivers everything through a Telegram bot. The process is hosted on Railway and auto-deploys on every push to `main`.

---

## High-Level Overview

```
┌──────────────────────────────────────────────────────────┐
│                     Main Process                         │
│                                                          │
│  ┌───────────────────────────┐  ┌──────────────────────┐ │
│  │  Scheduler Thread         │  │  Telegram Bots       │ │
│  │                           │  │  (Async, Polling)    │ │
│  │  • scan_loop() every 60s  │  │                      │ │
│  │  • manage_positions()     │  │  Main Bot            │ │
│  │  • manage_short_          │  │  TP Bot (mirror)     │ │
│  │    positions()            │  │                      │ │
│  │  • check_entry()          │  │  Both share in-      │ │
│  │  • check_short_entry()    │  │  memory state        │ │
│  │  • Scheduled jobs         │  │                      │ │
│  │  • State save / 5 min     │  │                      │ │
│  └──────────────┬────────────┘  └──────────┬───────────┘ │
│                 │                           │             │
│                 ▼                           ▼             │
│  ┌─────────────────────────────────────────────────────┐  │
│  │                 Shared State (in-memory)             │  │
│  │  positions, short_positions, tp_positions,           │  │
│  │  tp_short_positions, paper_cash, tp_paper_cash,      │  │
│  │  or_high, or_low, pdc, _near_miss_log               │  │
│  └───────────────────────┬─────────────────────────────┘  │
│                           │                               │
│                           ▼                               │
│  ┌─────────────────────────────────────────────────────┐  │
│  │     paper_state.json / tp_state.json                 │  │
│  │     trade_log.jsonl  (Railway Volume)                │  │
│  └─────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
         │                │                 │
         ▼                ▼                 ▼
  ┌──────────┐  ┌─────────────────┐  ┌──────────────┐
  │  Yahoo   │  │  FMP            │  │ TradersPost  │
  │  Finance │  │  (PDC + quotes) │  │ (webhook)    │
  │ (1m bars)│  │                 │  │              │
  └──────────┘  └─────────────────┘  └──────────────┘
```

Two Telegram bots run in the same process. The **main bot** reports the paper portfolio to the main group. The **TP bot** reports the TradersPost mirror portfolio privately. Both expose the same command surface; the bot that receives the message routes to its own portfolio view.

---

## Ticker Universe

Nine tradeable tickers (editable via `/ticker`):

```
AAPL  MSFT  NVDA  TSLA  META  GOOG  AMZN  AVGO  QBTS
```

`SPY` and `QQQ` are pinned index-filter tickers — they are never entered as positions and cannot be removed.

---

## Scheduler Thread

`scheduler_thread()` runs in a background thread and loops every 30 seconds (ET clock).

**Timed jobs (ET):**

| Time  | Day     | Job |
|-------|---------|-----|
| 09:20 | Weekdays | System test (8:20 CT pre-open) |
| 09:30 | Weekdays | `reset_daily_state()` — clears OR data, entry counts |
| 09:31 | Weekdays | System test (8:31 CT) |
| 09:35 | Weekdays | `collect_or()` — collect Opening Range data |
| 09:36 | Weekdays | Send OR notification card to both bots |
| 15:55 | Weekdays | `eod_close()` — force-close all open positions |
| 15:58 | Weekdays | Send EOD report |
| 18:00 | Sunday  | Send weekly digest |

**Continuous scan loop:** fires every 60 seconds while `elapsed >= SCAN_INTERVAL`.

**Periodic state save:** every 5 minutes, `save_paper_state()` runs in a daemon thread.

---

## Scan Loop Execution Order

`scan_loop()` runs only during market hours (09:35–15:55 ET, Mon–Fri):

```
1. Gate check          — weekday, 09:35–15:55 ET
2. PDC / polarity      — refresh SPY/QQQ vs PDC for regime alert
3. manage_positions()  — long stop chain + ladder + Red Candle + Lords Left
4. manage_tp_positions()  — TP mirror long positions (same logic)
5. manage_short_positions()  — short stop chain + ladder + Bull Vacuum + Polarity Shift
6. Pause check         — if /monitoring paused → skip steps 7–8 (protection still runs)
7. check_entry(ticker) — for each ticker: evaluate all long entry conditions
8. check_short_entry(ticker) — for each ticker: evaluate all short entry conditions
```

Position management (steps 3–5) runs unconditionally — open positions are never left unprotected during a scanner pause.

---

## Opening Range Collection

`collect_or()` fires at 09:35 ET:

- **OR window:** 09:30:00–09:34:59 ET (five 1-minute bars from Yahoo Finance).
- **OR_High:** maximum high across all bars in the window.
- **OR_Low:** minimum low across all bars in the window.
- **PDC (Previous Day Close):** official 4:00 PM ET close, fetched from FMP. Single static price per index/ticker per day.
- Retries up to 3× at 30-second intervals if Yahoo data is delayed.
- No entries fire until OR + PDC are confirmed for all tickers.

---

## Entry Logic

**Long (ORB Breakout)** — all conditions must be simultaneously true:

1. Time >= 09:45 ET (15-minute buffer after OR window closes)
2. Most recent completed 1-minute bar closes above `OR_High`
3. Current price > PDC (bullish polarity)
4. SPY current price > SPY PDC
5. QQQ current price > QQQ PDC
6. Daily entry count < 5 for this ticker (long + short combined)
7. No existing long open for this ticker
8. Daily loss limit not triggered

**Short (Wounded Buffalo)** — mirror conditions:

1. Time >= 09:45 ET
2. Most recent completed 1-minute bar closes below `OR_Low`
3. Current price < PDC (bearish polarity — the "Wounded Buffalo")
4. SPY current price < SPY PDC
5. QQQ current price < QQQ PDC
6. Daily entry count < 5 for this ticker
7. No existing short open for this ticker
8. Daily loss limit not triggered

**Timing:** scan runs every 60 seconds, 09:35–15:55 ET. EOD force-close at 15:55 ET.

---

## Position Sizing

| Parameter | Value |
|-----------|-------|
| Shares per entry | 10 (fixed, limit orders only) |
| Max entries per ticker per day | 5 (long + short combined) |
| Starting paper capital | $100,000 |
| Order type | Limit at current market price |

---

## 4-Layer Stop Chain (Long Side)

Every long position is protected by four stacking layers. Each layer can only tighten the stop — never loosen it.

> **Adaptive logic only makes things MORE conservative than baseline, never looser.**

```
final_stop = max(
  initial_stop,             # (1) structural baseline — permanent floor
  cap_floor,                # (2) entry × (1 − 0.75%)
  breakeven_ratchet_stop,   # (3) entry, armed at +0.50% peak
  ladder_stop(pos),         # (4) peak × (1 − give_back%)
)
```

### Layer 1 — Structural Baseline

Set at entry time: `OR_High − $0.90`. Frozen as `initial_stop` and never modified. This is the permanent floor for all subsequent layers.

### Layer 2 — 0.75% Cap (v3.4.21 + v3.4.23 retro)

When the OR_High baseline would place the stop more than 0.75% below entry (e.g., entry far above OR_High on a wide-range bar), the stop is capped:

```
cap_floor = entry × (1 − 0.0075)
stop = max(baseline, cap_floor)
```

`retighten_all_stops()` runs on every scan cycle to enforce this cap on all open positions, including positions opened before the cap shipped.

### Layer 3 — Breakeven Ratchet (v3.4.25)

Once peak gain >= +0.50%, the stop is pulled up to entry price (breakeven):

```
if current_price >= entry × 1.0050:
    stop = max(stop, entry)
```

This closes the gap between the 0.75% cap and the 1% ladder arm threshold.

### Layer 4 — Peak-Anchored Profit-Lock Ladder (v3.4.36)

Once peak gain >= +1%, the stop is defined as a shrinking percentage below peak. As peak climbs, give-back shrinks:

| Peak gain | Long stop | Phase |
|-----------|-----------|-------|
| < 1.0% | `initial_stop` | Bullet |
| >= 1.0% | `peak − 0.50%` | Arm |
| >= 2.0% | `peak − 0.40%` | Lock |
| >= 3.0% | `peak − 0.30%` | Tight |
| >= 4.0% | `peak − 0.20%` | Tighter |
| >= 5.0% | `peak − 0.10%` | Harvest |

Result is always clamped: `max(tier_stop, initial_stop)` so the structural floor is permanent.

---

## 4-Layer Stop Chain (Short Side)

Mirror of the long chain with inverted arithmetic:

- **Layer 1 baseline:** `PDC + $0.90` (stop is above entry for shorts).
- **Layer 2 cap:** `entry × (1 + 0.0075)` — stop can be no more than 0.75% above entry.
- **Layer 3 breakeven:** once peak gain (price decline) >= +0.50%, pull stop down to entry.
- **Layer 4 ladder:** `peak + give_back%` with the same shrinking tier table.

Result clamped: `min(tier_stop, initial_stop)`.

---

## Sovereign Regime Shield

The regime shield (v3.4.28) guards against macro tape reversals that flip every open position into an immediate exit candidate.

Four exit triggers ("Eye of the Tiger"):

| Exit | Applies to | Trigger |
|------|-----------|---------|
| Red Candle | Longs only | 1-min finalized close < session open OR < PDC |
| Lords Left | Longs only | BOTH SPY AND QQQ 1-min finalized close < their PDC |
| Bull Vacuum | Shorts only | BOTH SPY AND QQQ 1-min finalized close > their PDC |
| Polarity Shift | Shorts only | 1-min finalized close > PDC |

**Key design rules:**

- Lords Left and Bull Vacuum require **both** SPY **and** QQQ to cross PDC simultaneously. If only one index crosses (divergence), no eject fires — this is the hysteresis buffer.
- Uses the most recent **finalized** 1-minute bar (second-to-last close), not the in-progress bar.
- Fail-closed: missing bars or missing PDC → no eject. Stay in the trade.
- v3.4.34: AVWAP fully removed; PDC is the single anchor across entries, filters, and ejects.

```python
# Eject longs iff BOTH SPY and QQQ finalized 1m close < PDC
def _sovereign_regime_eject(side):
    ...
    if side == "long":
        return (spy_close < spy_pdc) and (qqq_close < qqq_pdc)
    else:  # short
        return (spy_close > spy_pdc) and (qqq_close > qqq_pdc)
```

---

## State Persistence

All mutable state is stored in `paper_state.json` (and `tp_state.json` for the TP portfolio), written to Railway Volume storage.

Saves occur:
- Every 5 minutes during the scan loop.
- Atomically: write to `.tmp` then `os.replace()` — no partial writes.

Key fields in `paper_state.json`:

```json
{
  "paper_cash": 97543.21,
  "positions": { "NVDA": { "entry_price": 142.50, "shares": 10,
                            "stop": 141.44, "initial_stop": 141.23,
                            "trail_high": 143.80, "trail_active": true } },
  "short_positions": { ... },
  "paper_trades": [ ... ],
  "paper_all_trades": [ ... ],
  "daily_entry_count": { "NVDA": 1 },
  "_trading_halted": false,
  "bot_version": "3.4.36"
}
```

`trade_log.jsonl` is an append-only file logging every entry and exit with full context.

---

## TradersPost Mirror

When `TRADERSPOST_ENABLED=true`, every paper trade fires a webhook POST to the configured TradersPost URL:

```
Paper long BUY  → POST { ticker, action: "buy",        shares: 10, price }
Paper long SELL → POST { ticker, action: "exit",        shares: 10, price }
Paper short     → POST { ticker, action: "sell_short",  shares: 10, price }
Paper cover     → POST { ticker, action: "buy_to_cover",shares: 10, price }
```

The TP portfolio (`tp_positions`, `tp_short_positions`) tracks what TradersPost should hold, independent of paper positions.

---

## Data Sources

| Source | Purpose | Auth |
|--------|---------|------|
| Yahoo Finance (yfinance) | 1-minute OHLCV bars — entries, stop management, OR collection | None |
| FMP | Real-time quotes, PDC data | `FMP_API_KEY` |
| TradersPost | Live trade mirroring via webhook | `TRADERSPOST_WEBHOOK_URL` |
| Telegram | Bot commands + notifications | `TELEGRAM_TOKEN`, `TELEGRAM_TP_TOKEN` |

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_TOKEN` | Yes | Main Telegram bot token |
| `CHAT_ID` | Yes | Main Telegram chat/group ID |
| `TELEGRAM_TP_TOKEN` | No | Separate bot token for TP bot |
| `TELEGRAM_TP_CHAT_ID` | No | TP bot chat ID |
| `TRADERSPOST_WEBHOOK_URL` | No | TradersPost webhook URL |
| `TRADERSPOST_ENABLED` | No | `true` to activate webhook sends (default: `false`) |
| `FMP_API_KEY` | No | FMP API key for PDC/quote data |
| `PAPER_STATE_PATH` | No | Path for paper state file (default: `paper_state.json`) |
| `DAILY_LOSS_LIMIT` | No | Realized P&L circuit breaker (default: `-500`) |

---

## MarketMode Classifier

`_refresh_market_mode()` runs each scan cycle and classifies the session into a mode (OPEN, MOMENTUM, CHOP, DEFENSIVE, etc.) based on day P&L, breadth, and RSI observations. **This is observation-only in v3.4.37 — no trading parameters are read from it yet.** See `/mode` for the live output.

---

## Dashboard

`dashboard_server.py` is imported at startup and runs an HTTP server in a background thread, serving a live status dashboard at the Railway URL. The main Telegram `/dashboard` command sends a text snapshot directly to the chat.

---

## Deployment

The bot runs on [Railway](https://railway.app):

1. Connect the GitHub repo to Railway.
2. Set all required environment variables.
3. Attach a Volume mount and set `PAPER_STATE_PATH` to a path on the volume (e.g., `/data/paper_state.json`) so state persists across deploys.
4. Railway auto-builds and deploys on every push to `main`.

**Logging:** dual handler — file (`stock_spike_monitor.log`) + stdout. All stdout/stderr visible in the Railway dashboard.

---

## Command Surface

See [COMMANDS.md](COMMANDS.md) for the full reference.

---

## Robinhood Bot (Live Trading) — v3.4.37

The TP (TradersPost mirror) bot doubles as the **Robinhood bot** for live trading. It uses TradersPost as a routing layer to send orders to Robinhood.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `RH_STARTING_CAPITAL` | `25000` | Robinhood account starting balance used for equity tracking |
| `RH_DOLLARS_PER_ENTRY` | `1500` | Dollar allocation per entry (replaces fixed 10-share paper sizing) |
| `RH_MAX_ENTRIES_PER_TICKER` | `1` | Maximum concurrent Robinhood positions in a single ticker |
| `RH_MAX_CONCURRENT_POSITIONS` | `6` | Maximum total concurrent Robinhood positions across all tickers |
| `RH_LONG_ONLY` | `true` | When true, all TP/Robinhood short entries are suppressed (paper short continues) |
| `GMAIL_ADDRESS` | `""` | Gmail address for IMAP reconciliation (optional) |
| `GMAIL_APP_PASSWORD` | `""` | Gmail app password (not account password) for IMAP |
| `RH_IMAP_POLL_SEC` | `120` | How often to poll Gmail for TradersPost fill/reject emails |

### Share Sizing

Robinhood orders use dynamic dollar-based sizing instead of the paper bot's fixed 10 shares:

```python
rh_shares = max(1, int(RH_DOLLARS_PER_ENTRY // price))
# Example: $1500 / $145.20 = 10 shares
```

This size is computed at entry and stored in `tp_positions[ticker]["shares"]`. Exits use the stored share count.

### Long-Only Gate

When `RH_LONG_ONLY=true` (default), the TP bot skips all short-entry webhook calls:

- Paper short positions continue to open and manage normally.
- `tp_short_positions` remains empty.
- No buy-to-cover order is ever sent for a short that was never opened.

### Concurrency Caps

Before any TP long entry fires, two gates are checked:

1. **Per-ticker cap:** if `ticker in tp_positions`, skip (max 1 position per ticker).
2. **Concurrent cap:** if `len(tp_positions) >= RH_MAX_CONCURRENT_POSITIONS`, skip.

The paper bot is entirely unaffected by these caps.

### IMAP Reconciliation

When `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` are set, `rh_imap_poll_once()` runs every `RH_IMAP_POLL_SEC` seconds in a daemon thread. It:

1. Connects to `imap.gmail.com` via SSL.
2. Searches for emails from `support@traderspost.io`.
3. Parses each unprocessed email with `_rh_parse_tp_email()` to classify it as `failed`, `filled`, or `unknown`.
4. Sends a Telegram message to the Robinhood chat for each email:
   - Failed: rejection reason extracted from `Status:` and `Payload:` fields.
   - Filled: confirmation with ticker, quantity, and fill price (best-effort parsing).
   - Unknown: raw subject forwarded for manual review.

UIDs of processed emails are tracked in `_rh_reconcile_seen` (in-memory; resets on restart).

### Paper Bot Unchanged

The paper portfolio remains completely unchanged:
- Starting capital: `$100,000`
- Shares per entry: `10` (fixed)
- Max 5 entries per ticker per day (long + short combined)
- No concurrency cap
- Short selling enabled
