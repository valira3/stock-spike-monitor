# Tiger Sovereign System — Authoritative Spec (vAA-1, dated 2026-04-29)

**Status:** Authoritative going forward. Supersedes `tiger_sovereign_spec_v2026-04-28h.md`.
**Source:** Migration spec `tiger-sovereign-morphing-spec-vAA-1.md`, expanded to executable form.
**Anchor PR:** v5.15.0 (planned).

This document is the single source of truth for the trading model. Code comments cite rule IDs (e.g. `L-P3-S5`, `SENT-D`); tests in `tests/test_tiger_sovereign_spec.py` cite the same IDs. When the spec changes, update **doc → tests → code** in that order.

---

## SECTION 0 — Glossary

| Term | Definition |
|---|---|
| **ORH / ORL** | Opening Range High / Low. Frozen at 09:35:59 ET on the 5m bar that closes at 09:35. |
| **NHOD / NLOD** | Dynamic New High / New Low of Day. Updates real-time on every 1m bar close from 09:30 ET onward. |
| **HWM / HVP** | High Water Mark / High Value Point. Peak recorded indicator value during the active session window. |
| **Trade_HVP** | Per-Strike variable: peak 5m ADX observed during the lifetime of the active Strike. **Resets at Strike entry.** |
| **Stored_Peak_Price** | Per-(ticker, side) variable for Alarm E. Holds the most-recent extreme price (NHOD for longs, NLOD for shorts) at the moment the corresponding RSI(15) peak was recorded. |
| **Stored_Peak_RSI** | Per-(ticker, side) variable for Alarm E. Holds the RSI(15) value at the moment Stored_Peak_Price was set. |
| **Strike** | A complete trade lifecycle on one (ticker, side): Entry → Exit. |
| **Alarm** | A parallel-evaluated exit / sizing-block trigger in the Sentinel Loop. |
| **Velocity Ratchet** | The Alarm-C mechanism: three consecutive declining 1m ADX values trigger a tight stop. |
| **HVP Lock** | The Alarm-D mechanism: 5m ADX falling below 75% of Trade_HVP triggers a market exit. |
| **Divergence Trap** | The Alarm-E mechanism: a new price extreme combined with a lower (long) / higher (short) RSI(15) blocks new strikes pre-entry and ratchets the stop post-entry. |

---

## SECTION 1 — System Concepts

### 1.1 The Strike Model (`STRIKE-1` … `STRIKE-3`)
- **`STRIKE-CAP-3`**: Maximum **3 Strikes per (ticker, side) per trading day**.
- **`STRIKE-FLAT-GATE`**: Strike `N+1` may not initiate until the position from Strike `N` is fully flat (`Position == 0`).
- **`STRIKE-RESET`**: All Strike counters and per-Strike state reset at 09:30:00 ET.

### 1.2 Alarm E — Divergence Trap (cross-cutting)
- **`SENT-E-PRE`** (pre-entry filter): If the current price prints a new extreme (NHOD for longs / NLOD for shorts) **AND** RSI(15) is diverging (`current_rsi < Stored_Peak_RSI` for longs; `current_rsi > Stored_Peak_RSI` for shorts), the bot is **prohibited** from opening **Strike 2 or Strike 3** positions. Strike 1 is unaffected.
- **`SENT-E-POST`** (in-trade): If divergence is detected on a held position, ratchet a **STOP MARKET** order to `current_price × (1 − 0.0025)` for longs / `current_price × (1 + 0.0025)` for shorts. The ratchet only moves in the protective direction; never loosens.

---

## SECTION 2 — The Bison (Long Strategy)

### Phase 1 — Weather (`L-P1-S1`, `L-P1-S2`)
- **`L-P1-S1`**: QQQ (5m) close > QQQ 5m 9-EMA. If FALSE → STOP.
- **`L-P1-S2`**: QQQ (current price) > QQQ 09:30 anchor VWAP. If FALSE → STOP.
- Both TRUE → `PERMIT_LONG = TRUE`.

### Phase 2 — Permits (`L-P2-S3`, `L-P2-S4`)
- **`L-P2-S3` Volume Gate** *(time-conditional)*:
  - If `now_et < 10:00:00 ET` → **gate auto-passes (TRUE)**.
  - If `now_et ≥ 10:00:00 ET` → require `ticker_1m_volume ≥ 1.00 × rolling_avg_55_bar(same_minute_of_day)`.
- **`L-P2-S4` ORH Boundary**: Two consecutive 1m candles must close strictly above the 5m ORH. The breakout permit fires on the close of the second qualifying 1m bar.

### Phase 3 — The Strike (Sizing & Execution)

Master anchor (gate): `L-P3-AUTH`: `5m_DI+ > 25`. If FALSE → no entry, regardless of 1m DI+.

When the master anchor is TRUE, sizing is **momentum-sensitive**:

- **`L-P3-FULL`** (Full Strike, 100%): If `1m_DI+ > 30` → enter **100%** of intended size immediately.
  - Order: `LIMIT @ ask × 1.001`.
- **`L-P3-SCALED-A`** (Scaled-A, 50%): If `25 ≤ 1m_DI+ ≤ 30` → enter **50%** of intended size.
  - Order: `LIMIT @ ask × 1.001`.
- **`L-P3-SCALED-B`** (Scaled-B add-on, +50%): While holding a Scaled-A position, add the remaining 50% iff:
  1. `1m_DI+ > 30`, **AND**
  2. price prints a fresh NHOD, **AND**
  3. `Alarm E (SENT-E-PRE) == False`.
  - Order: `LIMIT @ ask × 1.001`.

For all sizing decisions, the active Strike's `Trade_HVP` initializes to the current 5m ADX value at fill time and updates on every 5m bar close.

---

## SECTION 3 — The Wounded Buffalo (Short Strategy)

Mirror of Section 2 with inequality direction flipped, DI− replacing DI+, ORL replacing ORH, NLOD replacing NHOD.

### Phase 1 — Weather (`S-P1-S1`, `S-P1-S2`)
- **`S-P1-S1`**: QQQ (5m) close < QQQ 5m 9-EMA.
- **`S-P1-S2`**: QQQ (current) < QQQ 09:30 anchor VWAP.

### Phase 2 — Permits (`S-P2-S3`, `S-P2-S4`)
- **`S-P2-S3` Volume Gate**: same time-conditional logic as `L-P2-S3` (auto-pass before 10:00 ET).
- **`S-P2-S4` ORL Boundary**: two consecutive 1m closes strictly below 5m ORL.

### Phase 3 — The Strike

Master anchor: `S-P3-AUTH`: `5m_DI- > 25`.

- **`S-P3-FULL`**: `1m_DI- > 30` → 100% size, `LIMIT @ bid × 0.999`.
- **`S-P3-SCALED-A`**: `25 ≤ 1m_DI- ≤ 30` → 50%, `LIMIT @ bid × 0.999`.
- **`S-P3-SCALED-B`**: add-on requires `1m_DI- > 30` AND fresh NLOD AND `Alarm E == False`, `LIMIT @ bid × 0.999`.

---

## SECTION 4 — Shared Risk & Timing

| Rule ID | Rule | Value |
|---|---|---|
| `SHARED-HARD-STOP` | Per-trade hard stop | `unrealized_pnl ≤ -$500` → MARKET EXIT |
| `SHARED-CB` | Daily circuit breaker | `realized_pnl_today ≤ -$1,500` → halt + flatten |
| `SHARED-CUTOFF` | New-position cutoff | `15:44:59 ET` |
| `SHARED-EOD` | EOD flush | `15:49:59 ET` (MARKET) |
| `SHARED-ORDER-PROFIT` | Profit-taking exits use **LIMIT** orders |
| `SHARED-ORDER-STOP` | Defensive stops + ratchets use **STOP MARKET** orders |

---

## SECTION 5 — The Sentinel Loop (parallel monitor)

> **Architectural rule:** All five alarms (A, B, C, D, E) are evaluated in **parallel** on every per-tick or per-bar update. They are NOT a sequence. Caller resolves priority: any of {A, B, D} → full MARKET EXIT; otherwise apply {C, E} ratchets and any C harvest events. Alarm E pre-entry filter is consulted by the Strike sizing logic, not the per-tick exit loop.

### `SENT-A` — Emergency Shield (split, codes renamed in vAA-1)
- **`SENT-A_LOSS`** (Hard Loss): `unrealized_pnl ≤ -$500` → MARKET EXIT. Code: `A_LOSS` (legacy `A1` deleted).
- **`SENT-A_FLASH`** (Flash Move): single-minute price move > 1.0% against the position → MARKET EXIT. Code: `A_FLASH` (legacy `A2` deleted).
  - Window: 60 seconds. Threshold: `(pnl_now - pnl_60s_ago) / position_value ≤ -0.01`.
- **Migration note:** v5.15.0 deletes the legacy code strings `"A1"` / `"A2"` from `engine/sentinel.py`, dashboard surfaces, forensic capture filters, and log line formatters. No dual-emit window; clean break.

### `SENT-B` — Trend Death (5m EMA9 cross)
- A 5m bar **closes** below 5m 9-EMA (long) / above 5m 9-EMA (short) → MARKET EXIT.
- Triggered on 5m bar close, not intra-bar.

### `SENT-C` — Velocity Ratchet (REPLACES Titan Grip Harvest)
- Maintain a sliding window of the last three 1m ADX values: `[adx_1m_t-2, adx_1m_t-1, adx_1m_t]`.
- Trigger condition: `adx_1m_t < adx_1m_t-1 < adx_1m_t-2` (strictly decreasing for 3 consecutive 1m bars).
- Action: ratchet a **STOP MARKET** order to:
  - Long: `current_price × (1 − 0.0025)`
  - Short: `current_price × (1 + 0.0025)`
- Ratchet rule: only move in the protective direction; never loosen an existing stop. If a stop is already tighter, leave it.

### `SENT-D` — HVP Lock (NEW)
- Track `Trade_HVP = max(Trade_HVP, current_5m_adx)` on every 5m bar close during the active Strike.
- Trigger condition: `current_5m_adx < 0.75 × Trade_HVP`.
- Action: MARKET EXIT.
- `Trade_HVP` is per-Strike state and resets when the Strike closes (full flat).

### `SENT-E` — Divergence Trap (NEW)

State (per ticker, per side, persists across Strikes within the day):
- `Stored_Peak_Price`: extreme price (NHOD/NLOD) at last RSI(15) peak.
- `Stored_Peak_RSI`: RSI(15) value at that moment.

Update on every 1m bar close:
- **Long**: if `current_price > Stored_Peak_Price` AND `current_rsi_15 ≥ Stored_Peak_RSI` → update both.
- **Short**: if `current_price < Stored_Peak_Price` AND `current_rsi_15 ≤ Stored_Peak_RSI` → update both.

Trigger conditions:
- **`SENT-E-PRE`** (sizing gate; consulted by Strike-2/3 entry only):
  - Long: `current_price > Stored_Peak_Price` AND `current_rsi_15 < Stored_Peak_RSI` → **block Strike 2/3 entry**.
  - Short: `current_price < Stored_Peak_Price` AND `current_rsi_15 > Stored_Peak_RSI` → **block Strike 2/3 entry**.
- **`SENT-E-POST`** (in-trade):
  - Same condition as PRE → ratchet **STOP MARKET** to `current_price × (1 ∓ 0.0025)` (in the protective direction). Never loosens.

`Stored_Peak_*` does not reset between Strikes within the day; it resets at 09:30 ET each session.

---

## Spec → code mapping (rule IDs → modules)

| Rule ID | Module |
|---|---|
| `L-P1-*`, `S-P1-*` | `eye_of_tiger.evaluate_global_permit` |
| `L-P2-S3`, `S-P2-S3` | `eye_of_tiger.evaluate_volume_bucket` (extend with time gate) |
| `L-P2-S4`, `S-P2-S4` | `eye_of_tiger.evaluate_boundary_hold` |
| `L-P3-*`, `S-P3-*` | `eye_of_tiger.evaluate_strike_sizing` (NEW; replaces Entry-1 / Entry-2) |
| `STRIKE-CAP-3`, `STRIKE-FLAT-GATE` | `trade_genius.py` (extends `_v570_strike_*`) |
| `SHARED-HARD-STOP` | `engine/sentinel.check_alarm_a` (subsumed under `SENT-A_LOSS`) |
| `SHARED-CB`, `SHARED-CUTOFF`, `SHARED-EOD` | `engine/timing.py` |
| `SHARED-ORDER-*` | `broker/orders.py` |
| `SENT-A_LOSS`, `SENT-A_FLASH` | `engine/sentinel.check_alarm_a` |
| `SENT-B` | `engine/sentinel.check_alarm_b` |
| `SENT-C` | `engine/sentinel.check_alarm_c` (REWRITE; delegates to new `engine/velocity_ratchet.py`) |
| `SENT-D` | `engine/sentinel.check_alarm_d` (NEW) |
| `SENT-E-PRE`, `SENT-E-POST` | `engine/sentinel.check_alarm_e` (NEW) + `eye_of_tiger.evaluate_strike_2_3_gate` (NEW) |
| `Trade_HVP`, `Stored_Peak_*` | `engine/momentum_state.py` (NEW) |

---

## Removed / deprecated

- **Titan Grip Harvest** (`Stage 1 0.93%`, `0.40% stop`, `0.25% micro-ratchet`, `Stage 3 1.88%`, `Stage 4 runner`) — entirely deleted in vAA-1. Module `engine/titan_grip.py` is repurposed (or deleted in favor of `engine/velocity_ratchet.py`).
- **Fixed 50/50 entry sequence** (`evaluate_entry_1` / `evaluate_entry_2`) — replaced by `evaluate_strike_sizing`. Kept as deprecated thin wrappers for one release to avoid wide-blast renames.
- **`ENABLE_UNLIMITED_TITAN_STRIKES = True`** — incompatible with `STRIKE-CAP-3`. Default flips to `False`; Titans now obey the 3-strike cap unless explicitly overridden by env var (which is itself flagged for retirement).

---

## Outstanding interpretation questions (locked unless user revises)

These were called out in `tiger_sovereign_vAA_understanding.md` and are locked here pending confirmation:

1. RSI(15) = **15-period RSI on 1m bars**.
2. "55-bar rolling average" = 55 same-minute bars across prior trading days.
3. Velocity Ratchet trigger = **strictly monotone-decreasing** 1m ADX over 3 bars.
4. `STRIKE-CAP-3` overrides `ENABLE_UNLIMITED_TITAN_STRIKES`.
5. All profit-taking happens via stop-ratchet trips, Alarm D, or EOD. No fixed harvests.
6. Alarm A code names **renamed**: `A1` → `A_LOSS`, `A2` → `A_FLASH`. Legacy strings deleted everywhere (engine, dashboard, forensic, log formatters).
