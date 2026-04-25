# TradeGenius Strategy — v5

> **Status:** Canonical specification, derived from Gene's "Strategy - v5" memo (2026-04-25).
> **Authoritative for:** `BOT_VERSION >= 5.0.0`.
> **Replaces:** the v4.x ORB Breakout (long) and Wounded Buffalo (short) entries, the 4-layer stop chain, and the v4 sizing-stage logic.
>
> This document is the source of truth. The implementation in `trade_genius.py` MUST cite section IDs from this file in code comments. When the strategy needs to change in the future, update this document first, then patch the code. Tests reference rule IDs (e.g. `L-P2-R3`) so a spec change can be traced through to test failures.

## Overview

v5 reframes both the long and short workflows as a **two-stage entry, structural-ratchet exit** state machine:

```
                ┌──────────┐   gates pass    ┌──────────┐   2× DI confirm   ┌──────────┐
                │   IDLE   │ ──────────────► │  ARMED   │ ───────────────►  │  STAGE_1 │
                └──────────┘                 └──────────┘   (50% on)        └────┬─────┘
                                                                                  │
                                       in profit + DI accel + 2× confirm          ▼
                                       ┌────────────────────────────────────────────────┐
                                       │                       STAGE_2                  │
                                       │            (full size, stop = entry)           │
                                       └─────────────────────┬──────────────────────────┘
                                                              │ structural ratchet active
                                                              ▼
                                                       ┌────────────┐
                                          stop hit OR  │  TRAILING  │
                                          DI < 25  ─►  └─────┬──────┘
                                                              │
                                                              ▼
                                                       ┌────────────┐
                                          one chance   │   EXITED   │
                                          to RE_HUNT ─►└────────────┘
```

The **long protocol** ("The Tiger Hunts the Bison") and the **short protocol** ("The Wounded Buffalo / Gravity Trade") share this state machine; they differ in:
- direction of inequalities (above vs. below)
- which DMI line is used (DI+ vs. DI−)
- where the structural stop sits (5m candle low vs. 5m candle high)
- which structural pivot drives the ratchet (Higher Low vs. Lower High)

Risk infrastructure that is **NOT** redefined here continues from v4 unchanged:
- Unit sizing (whatever `trade_genius.py` computes today is "100% of unit")
- Daily-loss-limit (incl. the v4.7.0 short-side cap)
- 9-ticker spike universe + SPY/QQQ pinned filter rows
- EOD force-close
- Sovereign Regime Shield (Eye of the Tiger) global kill

In v5, the 50/50 staging means "50% of the v4 unit, then add the other 50%." The unit calculation itself is untouched.

---

## L — Long Protocol ("The Tiger")

### L-P1 — Permission Gates (IDLE → ARMED)

A long is only **eligible** when ALL of the following are true at the moment of evaluation:

| ID         | Rule |
|------------|------|
| **L-P1-G1** | `QQQ.last > QQQ.previous_day_close` (PDC). |
| **L-P1-G2** | `SPY.last > SPY.previous_day_close` (PDC). |
| **L-P1-G3** | `ticker.last > ticker.previous_day_close`. |
| **L-P1-G4** | `ticker.last > ticker.first_hour_high`. The "first hour" is 09:30–10:30 ET on the current session. |

If any gate fails, the bot stays in `IDLE` for that ticker. Gates are re-evaluated on each scan tick.

The dashboard MUST surface these as four green/red lights per ticker. If any light is not green, the bot is "Off" for that name.

### L-P2 — Stage 1: The Jab (ARMED → STAGE_1, 50% on)

| ID         | Rule |
|------------|------|
| **L-P2-R1** | `DI+(1m) > 25` AND `DI+(5m) > 25` simultaneously. ADX/DMI period = 14, computed from the live 1m and 5m candle streams. |
| **L-P2-R2** | Confirmation: rule L-P2-R1 must hold on **two consecutive closed 1-minute candles** (the "double-tap"). Entry fires on the close of the second confirming candle. |
| **L-P2-R3** | On entry, place **50% of unit size** as a market or marketable-limit order. |
| **L-P2-R4** | Initial stop ("Emergency Exit") = **low of the previous closed 5-minute candle**. This is a hard stop, not a trailing one — it does not move during STAGE_1. |
| **L-P2-R5** | Record `original_entry_price` = fill price of this Stage-1 order. This value is referenced by L-P3-R5, L-P4-R3, and L-P5-R1. |

### L-P3 — Stage 2: The Strike (STAGE_1 → STAGE_2, full size)

| ID         | Rule |
|------------|------|
| **L-P3-R1** | `DI+(1m) > 30`. |
| **L-P3-R2** | Confirmation: rule L-P3-R1 must hold on **two consecutive closed 1-minute candles** *after* L-P2 fired. |
| **L-P3-R3** | "Winning Rule": at the moment of the second confirming candle's close, `ticker.last > original_entry_price` (Stage-1 fills are in profit). If price has slipped to or below `original_entry_price`, Stage 2 does NOT fire — the bot stays in STAGE_1 with the original stop. |
| **L-P3-R4** | On entry, add the **remaining 50% of unit size**. The position is now 100% ("Full Port"). |
| **L-P3-R5** | "Safety Lock": at the instant the Stage-2 fill confirms, move the stop for the **entire 100% position** to `original_entry_price`. The trade is now risk-free vs. its original cost basis ("House Money"). |

### L-P4 — The Guardrail (STAGE_2 → TRAILING, 5m structural ratchet)

| ID         | Rule |
|------------|------|
| **L-P4-R1** | Every 5 minutes (on the close of each 5m candle), compute the most recent **Higher Low (HL)** in the post-entry 5m series. Definition of HL: the low of a 5m candle that is higher than the low of the immediately preceding 5m candle. |
| **L-P4-R2** | "Ratchet Up Only": if `new_HL > current_stop`, set `current_stop = new_HL`. If `new_HL <= current_stop`, do nothing. The stop NEVER moves down. |
| **L-P4-R3** | Hard exit on EITHER trigger: (a) `ticker.last < current_stop` at any tick, OR (b) `DI+(1m) < 25` on a closed 1m candle. Either trigger flattens 100% of the position immediately. |
| **L-P4-R4** | Once exited, transition to `EXITED`. The Re-Hunt branch (L-P5) becomes available exactly once for the rest of the session. |

### L-P5 — The Re-Hunt (one shot)

| ID         | Rule |
|------------|------|
| **L-P5-R1** | After an L-P4 exit, the ticker is dormant until `ticker.last > original_entry_price`. ("Reclamation" — price climbs back above where the original Stage-1 fill happened.) |
| **L-P5-R2** | When reclamation is true, the state machine returns to `ARMED` and the full L-P2 → L-P3 → L-P4 sequence runs again with **fresh** values (new `original_entry_price`, fresh stops, fresh DMI confirmations). |
| **L-P5-R3** | Maximum **one** Re-Hunt per ticker per session. After a second L-P4 exit, the ticker is `LOCKED_FOR_DAY` regardless of subsequent reclamation. |

---

## S — Short Protocol ("The Wounded Buffalo / Gravity Trade")

The short side is the structural mirror of the long side. Because "fear moves faster than greed," the **Hard Eject (S-P4-R3) is priority #1** — momentum decay on the short side is treated as imminent squeeze risk and triggers immediate cover, ahead of any structural-stop check.

### S-P1 — Permission Gates (IDLE → ARMED)

| ID         | Rule |
|------------|------|
| **S-P1-G1** | `QQQ.last < QQQ.previous_day_close`. |
| **S-P1-G2** | `SPY.last < SPY.previous_day_close`. |
| **S-P1-G3** | `ticker.last < ticker.previous_day_close`. |
| **S-P1-G4** | `ticker.last < ticker.opening_range_low_5m`. Opening Range Low = lowest low of the 09:30–09:35 ET 5-minute candle. |

If any gate fails, no short is taken. Critically: if the indices are green (S-P1-G1 or S-P1-G2 fails), shorts are forbidden regardless of the ticker's own weakness.

### S-P2 — Stage 1: The Jab (ARMED → STAGE_1, 50% short)

| ID         | Rule |
|------------|------|
| **S-P2-R1** | `DI−(1m) > 25` AND `DI−(5m) > 25` simultaneously. |
| **S-P2-R2** | Confirmation: rule S-P2-R1 must hold on **two consecutive closed 1-minute candles**. |
| **S-P2-R3** | On entry, short **50% of unit size**. |
| **S-P2-R4** | Initial stop = **high of the previous closed 5-minute candle**. Hard stop, does not move during STAGE_1. |
| **S-P2-R5** | Record `original_entry_price` = fill price of this Stage-1 short. |

### S-P3 — Stage 2: The Strike (STAGE_1 → STAGE_2, full short)

| ID         | Rule |
|------------|------|
| **S-P3-R1** | `DI−(1m) > 30`. |
| **S-P3-R2** | Confirmation: rule S-P3-R1 must hold on **two consecutive closed 1-minute candles** *after* S-P2 fired. |
| **S-P3-R3** | "Winning Rule" (inverted): at the second confirming candle's close, `ticker.last < original_entry_price` (the short is in profit — we "fund the fall"). If price has rallied to or above the original entry, Stage 2 does NOT fire. |
| **S-P3-R4** | On entry, add the remaining 50%. Position is 100% short. |
| **S-P3-R5** | "Safety Lock": stop for the entire 100% position moves to `original_entry_price`. The trade is now a risk-free Gravity Trade. |

### S-P4 — The Guardrail (STAGE_2 → TRAILING, structural Lower Highs)

| ID         | Rule |
|------------|------|
| **S-P4-R1** | Every 5 minutes (on the close of each 5m candle), compute the most recent **Lower High (LH)** in the post-entry 5m series. Definition of LH: the high of a 5m candle that is lower than the high of the immediately preceding 5m candle. |
| **S-P4-R2** | "Ratchet Down Only": if `new_LH < current_stop`, set `current_stop = new_LH`. If `new_LH >= current_stop`, do nothing. The stop NEVER moves up. |
| **S-P4-R3** | **Priority-1 Hard Eject:** `DI−(1m) < 25` on any closed 1m candle ⇒ immediate flatten. This check fires BEFORE the structural-stop check on every tick. Rationale: short-side momentum decay typically precedes a squeeze. |
| **S-P4-R4** | Structural exit (priority 2): `ticker.last > current_stop` ⇒ flatten. |
| **S-P4-R5** | Once exited, transition to `EXITED`. Re-Hunt (S-P5) becomes available once. |

### S-P5 — The Re-Hunt (one shot)

| ID         | Rule |
|------------|------|
| **S-P5-R1** | After an S-P4 exit, the ticker is dormant until `ticker.last < original_entry_price` (price falls back below the original short entry). |
| **S-P5-R2** | On reclamation, return to `ARMED` and run the full S-P2 → S-P3 → S-P4 sequence with fresh values. |
| **S-P5-R3** | Maximum one Re-Hunt per ticker per session. After a second S-P4 exit the ticker is `LOCKED_FOR_DAY`. |

---

## C — Cross-cutting Rules

| ID        | Rule |
|-----------|------|
| **C-R1**  | Long and short on the same ticker are mutually exclusive within a session. Entering one direction means the other direction's gates are ignored until EOD. |
| **C-R2**  | All DMI/ADX values use period **14** on the relevant timeframe. |
| **C-R3**  | "Closed candle" means the candle's wall-clock period has fully elapsed. Real-time intra-candle prints do NOT trigger entries — only confirmed closes do. The hard-stop *exits* (L-P4-R3, S-P4-R3, S-P4-R4) are the exception: they evaluate on every live tick because exits prioritize speed over confirmation. |
| **C-R4**  | The v4 daily-loss-limit (incl. v4.7.0 short-side cap) remains the portfolio-level brake on top of v5's per-trade risk. If the daily-loss-limit fires, all v5 state machines transition to `LOCKED_FOR_DAY` regardless of current state. |
| **C-R5**  | EOD force-close (15:55 ET) flattens any open v5 position regardless of state. |
| **C-R6**  | Sovereign Regime Shield (Eye of the Tiger) override remains a global kill — when active, all gates are forced false and any open position is flattened. |
| **C-R7**  | The v5 universe is identical to v4: the existing 9-ticker spike list. SPY and QQQ remain pinned filter rows on the dashboard and serve as the L-P1-G1/G2 and S-P1-G1/G2 permission inputs — they are NEVER traded directly. |

---

## D — Developer Summary (canonical state-machine spec)

> Code a state machine for a trading bot. Each ticker has independent state.
>
> States: `IDLE`, `ARMED`, `STAGE_1`, `STAGE_2`, `TRAILING`, `EXITED`, `RE_HUNT_PENDING`, `LOCKED_FOR_DAY`.
>
> Two parallel direction tracks (long, short) — at most one is active per ticker per session (C-R1).
>
> - `IDLE → ARMED`: all four permission gates true (L-P1 / S-P1).
> - `ARMED → STAGE_1`: DMI gate at 25 confirmed across two consecutive closed 1m candles (L-P2-R1+R2 / S-P2-R1+R2). Enter 50% of unit, stop at prior 5m candle low (long) or high (short).
> - `STAGE_1 → STAGE_2`: DMI gate at 30 confirmed across two more consecutive closed 1m candles, AND stage-1 fills are in profit vs. `original_entry_price` (L-P3-R3 / S-P3-R3). Add remaining 50%; immediately move stop on full 100% position to `original_entry_price`.
> - `STAGE_2 → TRAILING`: implicit on the next 5m close. Ratchet stop to most recent Higher Low (long) or Lower High (short). Stop is monotonic in the favorable direction only.
> - `TRAILING → EXITED`: hard stop hit OR DMI < 25. On the short side, the DMI < 25 check is priority-1 over the structural stop (S-P4-R3).
> - `EXITED → RE_HUNT_PENDING`: stays here until price reclaims `original_entry_price` (above for longs, below for shorts).
> - `RE_HUNT_PENDING → ARMED`: on reclamation, exactly once per ticker per session.
> - `EXITED → LOCKED_FOR_DAY`: on the second exit (re-hunt also stopped out). Also forced by daily-loss-limit (C-R4), Sovereign Regime Shield (C-R6), or EOD (C-R5).

---

## H — Change History

| Version | Date       | Author    | Notes |
|---------|------------|-----------|-------|
| v5.0    | 2026-04-25 | Gene → Val → TradeGenius dev | Initial canonical spec. Replaces v4.x ORB Breakout + Wounded Buffalo + 4-layer stop chain. |

---

## Appendix — Vocabulary mapping (Gene's metaphor → spec terms)

For future readers: Gene's memo uses hunting metaphors. The mapping is preserved here so the prose and the spec stay aligned.

| Memo term                  | Spec term                                                  |
|----------------------------|------------------------------------------------------------|
| The Jungle Check / Storm Watch | Phase 1 Permission Gates                                |
| The Heartbeat / Sell Pulse | DI+ / DI− crossing the 25 threshold                        |
| The Stalk / The Jab        | Stage 1 entry (50%)                                        |
| The Pounce / The Strike    | Stage 2 entry (full size)                                  |
| House Money / Gravity Trade | Stop moved to `original_entry_price` after Stage 2        |
| The Guardrail / Ratchet    | 5m structural-stop ratchet (HL up / LH down)               |
| Hard Exit / V-Eject        | Priority-1 DMI < 25 flatten (short side: S-P4-R3)          |
| Re-Hunt / Strike 2         | Single re-entry on price reclamation                       |
| The Tiger is finished      | `LOCKED_FOR_DAY`                                           |
| The Bison                  | A long candidate (uptrending ticker)                       |
| The Wounded Buffalo        | A short candidate (downtrending ticker)                    |
