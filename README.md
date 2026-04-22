# Stock Spike Monitor

ORB + Wounded Buffalo Telegram trading bot. Runs two independent intraday strategies — a long Opening Range Breakout and a short Wounded Buffalo breakdown — across a 9-ticker universe. Trades a $100k paper portfolio, with an optional Robinhood live-trading mirror ($25k, long-only, routed through TradersPost) and IMAP reconciliation against TradersPost fill/reject emails.

Current version: **v3.4.37**

---

## Strategies

**Long — ORB Breakout:** enters when the first 1-minute bar to close above the 5-minute Opening Range high occurs, the stock is above its Previous Day Close, and both SPY and QQQ are above their PDC.

**Short — Wounded Buffalo:** enters when the first 1-minute bar to close below the OR low occurs, the stock is below its PDC (the "wounded" condition), and both SPY and QQQ are below their PDC.

Both strategies use limit orders, 10 shares per entry, and share a 5-entry-per-ticker daily cap.

**Robinhood mode (v3.4.37):** The TradersPost mirror bot runs in long-only mode against a $25k starting capital, sizing $1,500 per entry, capping at 1 entry per ticker and 6 concurrent positions, with IMAP email reconciliation against TradersPost fill/reject emails.

---

## Ticker Universe

```
AAPL  MSFT  NVDA  TSLA  META  GOOG  AMZN  AVGO  QBTS
```

SPY and QQQ are index filters only (never traded). The universe is editable at runtime via `/ticker add` and `/ticker remove`.

---

## Data Source

All 1-minute OHLCV bars come from **Yahoo Finance**. PDC (Previous Day Close) is fetched from FMP. No Finnhub, no AVWAP, no VIX put-selling.

---

## PDC Polarity Anchor

PDC is the single price anchor across every decision in the bot: stock entry gates (price vs PDC), index filters (SPY/QQQ vs PDC), and the Sovereign Regime Shield ejects. As of v3.4.34, Anchored VWAP has been fully removed.

---

## 4-Layer Stop Chain

Every open position is protected by four stacking layers, long or short. Each layer can only tighten the stop — never loosen it.

> **Adaptive logic only makes things MORE conservative than baseline, never looser.**

| Layer | When | Long action |
|-------|------|-------------|
| 1 — Structural baseline | Entry time | `OR_High − $0.90` — permanent floor |
| 2 — 0.75% cap (v3.4.21) | Every scan | `max(baseline, entry × 0.9925)` |
| 3 — Breakeven ratchet (v3.4.25) | Every scan | At +0.50% peak, pull stop to entry |
| 4 — Profit-lock ladder (v3.4.36) | Every scan | `peak × (1 − give_back%)`, shrinking: |

```
Peak gain    Long stop          Phase
< 1.0%       initial hard stop  Bullet
>= 1.0%      peak − 0.50%       Arm
>= 2.0%      peak − 0.40%       Lock
>= 3.0%      peak − 0.30%       Tight
>= 4.0%      peak − 0.20%       Tighter
>= 5.0%      peak − 0.10%       Harvest
```

Shorts mirror with `PDC + $0.90` baseline and `min(tier_stop, initial_stop)` clamping.

---

## Sovereign Regime Shield

Four "Eye of the Tiger" exits fire on macro reversals:

| Exit | Side | Trigger |
|------|------|---------|
| Red Candle | Longs | 1-min finalized close < session open or < PDC |
| Lords Left | Longs | BOTH SPY AND QQQ 1-min finalized close < PDC |
| Bull Vacuum | Shorts | BOTH SPY AND QQQ 1-min finalized close > PDC |
| Polarity Shift | Shorts | 1-min finalized close > PDC |

Lords Left and Bull Vacuum require **both** indices to cross simultaneously. Single-index divergence does not trigger an eject.

---

## Paper Portfolio + Robinhood Mirror

The bot maintains a **paper portfolio** ($100k starting capital) and a parallel **Robinhood mirror portfolio** (via TradersPost webhook, $25k starting capital). Both are tracked side by side:

- Paper trades report to the main Telegram group.
- Robinhood trades report privately via a separate TP bot.
- Every paper trade fires a TradersPost webhook when `TRADERSPOST_ENABLED=true`.
- Robinhood mode is long-only; short entries are gated by `RH_LONG_ONLY=true` (default).

---

## Timing

| Parameter | Value |
|-----------|-------|
| OR window | 09:30–09:35 ET (first 5 min) |
| Entry window | 09:45 ET+ (15-min buffer) |
| Scan interval | Every 60s, 09:35–15:55 ET |
| EOD force-close | 15:55 ET |
| State save | Every 5 min |

---

## Deployment

Hosted on [Railway](https://railway.app). Auto-deploys on every push to `main`.

```bash
# Local development
pip install yfinance requests pandas pytz python-telegram-bot matplotlib reportlab

export TELEGRAM_TOKEN="..."
export CHAT_ID="..."
export FMP_API_KEY="..."           # PDC + quote data
export TRADERSPOST_WEBHOOK_URL="..." # optional — live mirror
export TRADERSPOST_ENABLED="true"    # optional — activates webhook sends
export PAPER_STATE_PATH="/data/paper_state.json"  # Railway Volume path

python stock_spike_monitor.py
```

**Railway setup:**
1. Connect GitHub repo to Railway.
2. Set all environment variables.
3. Attach a Volume and set `PAPER_STATE_PATH` to a path on the volume so state persists across deploys.
4. Railway auto-builds and deploys on push to `main`.

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.11+ |
| Bot framework | python-telegram-bot |
| Market data | Yahoo Finance (1-min bars), FMP (PDC/quotes) |
| Brokerage bridge | TradersPost (webhook) |
| Charts | matplotlib |
| Hosting | Railway |
| State | JSON files on Railway Volume |

---

## Documentation

- [COMMANDS.md](COMMANDS.md) — Full command reference
- [ARCHITECTURE.md](ARCHITECTURE.md) — Internal architecture: scan loop, stop chain, regime shield, state persistence
- [stock_spike_monitor_algo.pdf](stock_spike_monitor_algo.pdf) — Algorithm Reference Manual v3.4.37 (also available via `/algo` in the bot)

---

## License

Private repository. Not licensed for redistribution.
