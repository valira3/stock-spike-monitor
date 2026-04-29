<!-- Adopted in v5.13.0 series, supersedes v5 strategy archived at docs/spec_archive/v5_strategy_pre_tiger_sovereign.md. -->

# Tiger Sovereign System: Automated Trading Specification (v2026.4.28h)

## Operational Overrides (Runtime Flags)

* **VOLUME_GATE_ENABLED** (env var) — controls L-P2-S3 / S-P2-S3 volume rule. Default: `false` (DISABLED). Set to `true` on Railway to re-enable spec-strict behavior. Rationale: backtest analysis on 2026-04-28 showed the gate filtered out trades that, when allowed with full Sentinel exit logic, returned net positive (+$251 cohort P&L). Operating with gate OFF until multi-day analysis confirms direction.

## Architectural Note for Developer
The **Sentinel Loop (Phase 4)** is a parallel monitoring system. These "Alarms" are **NOT** a sequence. The bot must check all Alarms (A, B, and C) on every single price tick. If **ANY** alarm triggers, the bot must act immediately. Alarm A (Emergency) or Alarm B (9-EMA Shield) can terminate a trade at any time, even if the "Harvest" targets in Alarm C have not been reached.

---

## SECTION 1: THE BISON (LONG STRATEGY)

### PHASE 1: GLOBAL MARKET SHIELD (THE WEATHER)
* **STEP 1:** Is QQQ (5m) Price ABOVE the 9 EMA?
    * [NO] --> STOP. (Permit = OFF)
    * [YES] --> Proceed to Step 2.
* **STEP 2:** Is QQQ (Current) Price ABOVE the 9:30 AM Anchor VWAP?
    * [NO] --> STOP. (Permit = OFF)
    * [YES] --> **PERMIT LONG TRADING = TRUE.** Proceed to Ticker Scan.

### PHASE 2: TICKER-SPECIFIC PERMITS (THE SCENT)
* **STEP 3:** Does the Ticker have HIGH VOLUME? (>= 100% of 55-day rolling average for this minute)\*
    * [NO] --> WAIT.
    * [YES] --> Proceed to Step 4.
* **STEP 4:** Did the Ticker close TWO (2) consecutive 1m candles ABOVE its 5m Opening Range High?
    * [NO] --> WAIT.
    * [YES] --> **TICKER IS "HOT."** Proceed to Entry Trigger.

\* Subject to `VOLUME_GATE_ENABLED` env var; default off.

### PHASE 3: THE STRIKE (THE ENTRY)
* **STEP 5 (ENTRY 1):** Is 5m DI+ > 25 AND 1m DI+ > 25 AND Price at New High of Day (NHOD)?
    * [YES] --> **BUY 50% POSITION.**
* **STEP 6 (ENTRY 2):** While holding Entry 1, did 1m DI+ cross ABOVE 30 AND Price print a FRESH NHOD?
    * [YES] --> **BUY REMAINING 50% (FULL LOAD).**

### PHASE 4: THE SENTINEL LOOP (ACTIVE PROTECTION)
* **ALARM A (EMERGENCY - STOP MARKET ORDER):** Has the trade lost $500 OR has price dropped 1% in a single minute?
    * [ACTION] --> **EXIT 100% IMMEDIATELY.**
* **ALARM B (THE 9-EMA SHIELD - STOP MARKET ORDER):** Did a 5-minute candle close BELOW the 5m 9-EMA?
    * [ACTION] --> **EXIT 100% IMMEDIATELY.**
* **ALARM C (THE TITAN GRIP HARVEST):**
    * **STAGE 1 (THE ANCHOR):** IF Price >= (OR High + 0.93%) --> **SELL 25%** (Limit Order). Move STOP MARKET for remaining 75% to **(OR High + 0.40%)**.
    * **STAGE 2 (THE MICRO-RATCHET):** For every **+0.25%** increment above 0.93% (e.g., 1.18%, 1.43%, 1.68%...):
        * [ACTION] --> Move the existing STOP MARKET order UP by **+0.25%**.
    * **STAGE 3 (THE SECOND HARVEST):** IF Price >= (OR High + 1.88%) --> **SELL 25%** (Limit Order). 
    * **STAGE 4 (THE RUNNER):** Final 50% remains in trade, continuing the **+0.25% / +0.25%** ratchet until stopped out or EOD Flush.

---

## SECTION 2: THE WOUNDED BUFFALO (SHORT STRATEGY)

### PHASE 1: GLOBAL MARKET SHIELD (THE WEATHER)
* **STEP 1:** Is QQQ (5m) Price BELOW the 9 EMA?
    * [NO] --> STOP. (Short Permit = OFF)
    * [YES] --> Proceed to Step 2.
* **STEP 2:** Is QQQ (Current) Price BELOW the 9:30 AM Anchor VWAP?
    * [NO] --> STOP. (Short Permit = OFF)
    * [YES] --> **PERMIT SHORT TRADING = TRUE.** Proceed to Ticker Scan.

### PHASE 2: TICKER-SPECIFIC PERMITS (THE SCENT)
* **STEP 3:** Does the Ticker have HIGH VOLUME? (>= 100% of 55-day rolling average for this minute)\*
    * [NO] --> WAIT.
    * [YES] --> Proceed to Step 4.
* **STEP 4:** Did the Ticker close TWO (2) consecutive 1m candles BELOW its 5m Opening Range Low?
    * [NO] --> WAIT.
    * [YES] --> **TICKER IS "BLEEDING."** Proceed to Entry Trigger.

\* Subject to `VOLUME_GATE_ENABLED` env var; default off.

### PHASE 3: THE STRIKE (THE ENTRY)
* **STEP 5 (ENTRY 1):** Is 5m DI- > 25 AND 1m DI- > 25 AND Price at New Low of Day (NLOD)?
    * [YES] --> **SELL SHORT 50% POSITION.**
* **STEP 6 (ENTRY 2):** While holding Entry 1, did 1m DI- cross ABOVE 30 AND Price print a FRESH NLOD?
    * [YES] --> **SELL SHORT REMAINING 50% (FULL LOAD).**

### PHASE 4: THE SENTINEL LOOP (ACTIVE PROTECTION)
* **ALARM A (EMERGENCY - STOP MARKET ORDER):** Has the trade lost $500 OR has price spiked 1% in a single minute?
    * [ACTION] --> **EXIT 100% IMMEDIATELY (BUY TO COVER).**
* **ALARM B (THE 9-EMA SHIELD - STOP MARKET ORDER):** Did a 5-minute candle close ABOVE the 5m 9-EMA?
    * [ACTION] --> **EXIT 100% IMMEDIATELY.**
* **ALARM C (THE TITAN GRIP HARVEST):**
    * **STAGE 1 (THE ANCHOR):** IF Price <= (OR Low - 0.93%) --> **BUY COVER 25%** (Limit Order). Move STOP MARKET for remaining 75% to **(OR Low - 0.40%)**.
    * **STAGE 2 (THE MICRO-RATCHET):** For every **-0.25%** increment below -0.93% (e.g., -1.18%, -1.43%, -1.68%...):
        * [ACTION] --> Move the existing STOP MARKET order DOWN by **-0.25%**.
    * **STAGE 3 (THE SECOND HARVEST):** IF Price <= (OR Low - 1.88%) --> **BUY COVER 25%** (Limit Order).
    * **STAGE 4 (THE RUNNER):** Final 50% remains in trade, continuing the **-0.25% / -0.25%** ratchet until stopped out or EOD Flush.

---

## SECTION 3: SHARED SYSTEM RULES

* **NEW POSITION CUTOFF:** The bot is prohibited from opening any NEW positions after **15:44:59 EST**.
* **DAILY CIRCUIT BREAKER:** If total losses for the day reach -$1,500, the bot must shut down all trading and close all open positions immediately using MARKET orders.
* **END OF DAY (EOD) FLUSH:** Every day at exactly **15:49:59 EST**, the bot must close all open positions using MARKET orders.
* **UNLIMITED HUNTING:** The bot should continue to look for new trades until the 15:44:59 cutoff, provided the Global Market Shield is active and the Daily Circuit Breaker has not been hit.
* **ORDER TYPE SPECIFICATIONS:**
    * **Profit Taking (Harvest):** All profit-taking exits must be executed via **LIMIT orders** to capture positive slippage.
    * **Stop Losses:** All defensive stops (Initial Stop, Sovereign Brake, 9-EMA Shield, and Trailing Ratchets) must be **STOP MARKET orders** to guarantee immediate execution.
