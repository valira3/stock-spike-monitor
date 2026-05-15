# TradeGenius — Trading Strategy Overview

**As of v9.1.83 / 2026-05-14**

Two independent strategies run each trading day: a morning ORB session and an afternoon EOD reversal addon.

---

## 1. Daily Schedule

```
06:00 ET  Monitor wakes (pre-market checks, 15-min cadence)
07:00 ET  RTH monitoring begins (5-min cadence)
09:25 ET  Bot pre-arms: loads prior closes, seeds OR windows
09:30 ET  Market open — OR accumulation begins
10:00 ET  OR window closes (30-min ORB)
10:00 ET  Entry window opens (breakout entries allowed)
11:00 ET  Entry cutoff — no new ORB entries after this
15:00 ET  EOD reversal entry window opens
15:59 ET  EOD reversal exits (flat all positions)
16:00 ET  Market close
19:00 ET  RTH monitoring ends
```

---

## 2. Morning Strategy — Opening Range Breakout (ORB)

### 2.1 Concept

The first 30 minutes of trading (09:30–10:00 ET) establishes the **Opening Range (OR)**. A breakout above OR High signals a long entry; a breakdown below OR Low signals a short entry. The trade rides momentum with a defined risk/reward.

### 2.2 Entry Flow

```
09:30 ──────────────────── 10:00 ─────────────────── 11:00
  │   OR ACCUMULATION         │   ENTRY WINDOW          │
  │   High/Low building       │   (breakout hunting)    │ cutoff
  │                           │                         │
  └── OR_High, OR_Low locked ─┘                         └── no new entries
```

```
Price breaks OR_High or OR_Low?
            │
            ▼
    Day Gates (all must pass)
    ┌─────────────────────────┐
    │ VIX < 22.0              │
    │ SPY prior day > -40bps  │
    │ No earnings this week   │
    │ Gap < 1.5% at open      │
    │ Ticker not blocklisted  │
    └─────────────────────────┘
            │ pass
            ▼
    VWAP Chase Gate
    (6 mega-caps only: META/MSFT/AAPL/AMZN/GOOG/AVGO)
    Price within 25bps of session VWAP?
            │ yes → admit
            ▼
    Risk Book checks
    ┌─────────────────────────────────────┐
    │ Daily loss < 2% of account          │
    │ Concurrent risk < $2,000            │
    │ Trade notional < 75% of account     │
    │ Not in 30-min post-loss cooldown    │
    └─────────────────────────────────────┘
            │ pass
            ▼
         ENTRY FIRES
```

### 2.3 Position Sizing

```
Stop distance = ATR(14, 5-min) x 1.75
Risk per trade = 1.0% of account equity
Shares = (account x 0.01) / stop_distance
```

### 2.4 Exit Flow

```
ENTRY
  │
  ├── Stop hit (OR edge ± 5bps buffer)  ──────────────► EXIT (full loss)
  │
  ├── Price reaches +1R (1x stop distance)
  │     └── Close 50% of position (partial profit)
  │         Move stop to break-even
  │
  ├── Price reaches +2.5R (target)  ─────────────────► EXIT (remaining 50%)
  │
  └── 11:00 ET cutoff  ──────────────────────────────► EXIT (time stop)
```

### 2.5 Key Parameters

| Parameter | Value | Purpose |
|---|---|---|
| OR window | 30 min (09:30–10:00 ET) | Defines the range |
| Entry cutoff | 11:00 ET | No late chasing |
| RR target | 2.5R | Profit target |
| ATR multiplier | 1.75 | Volatility-adaptive stop |
| Risk per trade | 1.0% | Position sizing |
| Max concurrent risk | $2,000 | Portfolio cap |
| Daily loss kill | 2.0% | Circuit breaker |
| Post-loss cooldown | 30 min | Same ticker re-entry block |
| VWAP gate | 25bps (6 mega-caps) | Blocks chased entries |
| VIX gate | < 22.0 | Avoids extreme vol days |

### 2.6 Per-Portfolio Architecture

Three portfolios run independently (Main, Val, Gene), each with its own RiskBook and FSM:

```
Scan Loop (every ~30s)
        │
        ├── Main portfolio ──► RiskBook_Main ──► callbacks.execute_entry
        │
        ├── Val portfolio  ──► RiskBook_Val  ──► executor_val.fire_long/short
        │                                        (Alpaca paper → live)
        │
        └── Gene portfolio ──► RiskBook_Gene ──► executor_gene.fire_long/short
                                                  (disabled, ALPACA_SKIP_PORTFOLIOS=gene)
```

---

## 3. Afternoon Strategy — EOD Reversal Addon (r17)

### 3.1 Concept

At 15:00 ET, after the main trading session has defined the day's direction, a mean-reversion trade is placed on a narrow universe of liquid, non-momentum stocks. The thesis: large-cap stocks that have moved with intraday momentum tend to partially reverse in the final hour as institutions rebalance.

### 3.2 Entry Flow

```
15:00 ET ───────────────────────────── 15:59 ET
    │   EOD ENTRY WINDOW                   │ FLAT
    │                                      │
    ▼                                      ▼
Select top-1 LONG from fence:         Exit ALL positions
  ORCL, AAPL, MSFT, AVGO              (market orders)

Select top-1 SHORT from fence:
  ORCL, NFLX, AAPL, MSFT

(Excluded: META, GOOG, TSLA, AMZN, NVDA
 — retail momentum names that fail the reversal pattern)
```

### 3.3 Selection Logic

```
For each side (LONG / SHORT):
  1. Rank tickers in the fence by intraday move (price vs prior close)
  2. Pick the ticker with the largest move against the reversal direction
     (largest down-move for LONG candidate,
      largest up-move for SHORT candidate)
  3. Fire top-1 only
```

### 3.4 Sizing & Exit

```
Position size = 35% of account notional (fixed, not stop-based)
Entry: 15:00 ET market order (1.5bps slippage model)
Exit:  15:59 ET market order (1.5bps slippage model)
Hold time: ~59 minutes
```

### 3.5 Performance (Backtest, Jan 2025–May 2026)

```
EOD addon alone:     +$10,036/yr  on $100k account
Combined with ORB:   +$41,485/yr  (+58.8% / 17 months)

Quarter    Combined P&L
─────────────────────────
2025-Q1    -$5,183   (weak — pre-live, NFLX-driven)
2025-Q2    +$11,430
2025-Q3    +$8,479
2025-Q4    +$15,208
2026-Q1    +$8,316
2026-Q2    +$20,521
```

---

## 4. Full Day Timeline Diagram

```
TIME (ET)   EVENT
─────────────────────────────────────────────────────────────
06:00       Monitor pre-market ticks begin (15-min cadence)
07:00       RTH monitoring (5-min cadence)
09:25       Bot seeds prior closes, arms OR windows
            ┌────────────────────────────────────────────────
09:30       │ MARKET OPEN
            │ OR accumulation: tracking High/Low every bar
            │
10:00       │ OR LOCKED — entry window opens
            │ Breakout above OR_High → LONG signal
            │ Breakdown below OR_Low → SHORT signal
            │ Day gates + VWAP gate + RiskBook evaluated
            │ Partial profit at +1R, move stop to BE
            │
11:00       │ ENTRY CUTOFF — no new ORB trades
            │ Existing positions still managed (stop/target)
            │
            │           [quiet period]
            │
15:00       │ EOD ENTRY — top-1 LONG + top-1 SHORT
            │ 35% notional per leg, market order
            │
15:59       │ EOD EXIT — all EOD positions flattened
            └────────────────────────────────────────────────
16:00       Market close
19:00       RTH monitoring ends
```

---

## 5. Risk Architecture

```
                    ACCOUNT EQUITY ($100k)
                           │
          ┌────────────────┼────────────────┐
          │                │                │
    Daily Loss Kill   Concurrent Risk   Per-Trade Max
      (2% = $2,000)    ($2,000 cap)     (75% notional)
          │
          ▼
    If triggered: no new entries for rest of day
          │
          ▼
    30-min post-loss cooldown per (ticker, side)
    prevents double-firing on the same losing setup
```

---

## 6. Glossary

**ATR (Average True Range)**
A volatility measure computed over the prior N bars. Used here as `ATR(14, 5-min)` — the 14-period ATR on 5-minute bars. Wider ATR = wider stop = fewer shares (keeps dollar risk constant).

**BE (Break-Even)**
Moving the stop loss to the entry price after a trade reaches +1R profit. Eliminates the possibility of a winning trade turning into a loss.

**Circuit Breaker (Daily Loss Kill)**
When cumulative realized losses in a day exceed 2% of account equity, the bot stops taking new entries for the remainder of the session.

**Cooldown**
A 30-minute block on re-entering the same (ticker, side) after a stop-out. Prevents re-entering a broken setup repeatedly on a bad day.

**EOD Reversal**
End-of-day mean-reversion strategy. Bets that stocks that moved sharply intraday will partially retrace in the final 59 minutes before close.

**FSM (Finite State Machine)**
The per-portfolio state tracker (`orb/state.py`). Tracks whether a portfolio is in IDLE, OR_BUILDING, ENTRY_PENDING, IN_TRADE, or CLOSED state for each ticker.

**OR / Opening Range**
The price range (High to Low) established in the first 30 minutes of trading (09:30–10:00 ET). The breakout above OR_High or below OR_Low triggers an entry signal.

**ORB (Opening Range Breakout)**
The core morning strategy. Enter long on a break above the OR_High, short on a break below OR_Low.

**Partial Profit (1R)**
When a trade reaches 1x the initial risk distance (stop distance), half the position is closed to lock in profit. The remaining half rides toward the 2.5R target with a break-even stop.

**RiskBook**
The per-portfolio risk ledger (`orb/risk_book.py`). Tracks open risk, daily P&L, and enforces position limits. Each of Main, Val, and Gene has its own independent RiskBook.

**RR (Risk/Reward)**
The ratio of potential profit to risk. An RR of 2.5 means the target is 2.5x the stop distance away from entry.

**RTH (Regular Trading Hours)**
09:30–16:00 ET. The monitor uses a broader definition (07:00–19:00 ET) to cover pre-market and after-hours checks.

**SEV-1**
A severity-1 incident — a bug causing live P&L loss or blocking entries/exits during RTH. Requires immediate hotfix bypassing the staging gate.

**Slippage**
The difference between the expected fill price and the actual price. Modeled in backtest as 1.5bps on entry and 1.5bps on exit.

**VWAP (Volume-Weighted Average Price)**
The average price weighted by volume since market open. Used as a gate for 6 mega-caps: if price has already moved more than 25bps from VWAP, the entry is skipped (the move has already chased too far).

**VIX Gate**
Skips all entries on days when the VIX (CBOE Volatility Index) opens above 22.0. High VIX days have erratic intraday behavior that breaks the ORB pattern.

**1R, 2.5R**
Multiples of the initial risk (stop distance). If the stop is $0.50 away, 1R = $0.50 profit, 2.5R = $1.25 profit.
