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
| **L-P2-R1** | `DI+(1m) > 25` AND `DI+(5m) > 25` simultaneously. ADX/DMI period = 15 (per Gene's spec; matches the canonical `DI_PERIOD = 15` in v4 `trade_genius.py`). Computed from the live 1m and 5m candle streams. |
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
| **C-R2**  | All DMI/ADX values use period **15** on the relevant timeframe. This matches Gene's original spec ("DI+ (15 period, 5m)") and the canonical `DI_PERIOD = 15` constant that has been in `trade_genius.py` since v4. Wilder's classical default of 14 is **not** used here. |
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
| v5.0.1  | 2026-04-25 | Gene flagged | DMI/ADX period corrected from 14 → 15 in C-R2 and L-P2-R1 to match Gene's spec and the canonical `DI_PERIOD` already in `trade_genius.py`. No state-machine logic changed. |
| v5.10.6 | 2026-04-28 | Project Eye of the Tiger closeout | Strategy regenerated to reflect the six-section v5.10 algorithm (Section I–VI below). The v5.0 two-stage state machine remains the historical reference; the live bot at `BOT_VERSION >= 5.10.0` follows the Eye-of-the-Tiger pipeline instead. |

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

---

# v5.10 Eye of the Tiger — current canonical algorithm

> **Status:** Live for `BOT_VERSION >= 5.10.0`.
> **Authored:** Project Eye of the Tiger, shipped across v5.10.1 → v5.10.6.
> **Replaces:** the v5.0 IDLE → ARMED → STAGE_1 → STAGE_2 → TRAILING state machine and every legacy v5.1.x — v5.9.x emitter (REHUNT, OOMPH, V512-CAND, V570-STRIKE, V572-ABORT). Those forensic shadows have been retired.

The v5.10 pipeline is six discrete sections. Each section is a pure function of recent state; the live bot evaluates them top-down each minute, and a position is opened only if every gate in Sections I–III passes on the same bar.

## Section I — Global Permit (QQQ Market Shield + Sovereign Anchor)

Two top-of-book gates that decide whether the bot is allowed to trade at all this minute.

| ID    | Gate | Pass condition |
|-------|------|----------------|
| I-LONG  | QQQ Market Shield | last 5-minute QQQ close > 9-period EMA of 5m closes |
| I-SHORT | QQQ Market Shield | last 5-minute QQQ close < 9-period EMA of 5m closes |
| I-ANCHOR | Sovereign Anchor | QQQ current price > QQQ 9:30 ET opening AVWAP (LONG); inverse for SHORT |

If `I-LONG` is False, the LONG rail is dark (no LONG entries can fire); same for `I-SHORT`. The ANCHOR rail is shared between sides; live state surfaces on the dashboard as the `ANCHOR` pill in the Eye of the Tiger card.

Implementation: `v5_10_1_integration.evaluate_section_i(side, qqq_close_5m, qqq_ema9, qqq_current_price, qqq_avwap_open) -> {open: bool, ...}`.

## Section II — Volume Bucket + Boundary Hold

Per-ticker, per-side gates that confirm there's a real move underway.

### Volume Bucket
The 55-day rolling baseline holds, for each minute of the trading day, the median 1-minute volume. The current 1-minute volume is graded against that baseline:

| State      | Condition |
|------------|-----------|
| `PASS`      | current 1m volume ≥ baseline median for the just-closed minute |
| `FAIL`      | current 1m volume < baseline median |
| `COLDSTART` | baseline not yet seeded (first 55 trading days of any new ticker) |

The bot allows entries during `COLDSTART` (the 55-day fail-open is intentional — the alternative is no new tickers ever); `FAIL` blocks the entry.

### Boundary Hold
Using the 9:30–10:00 ET opening range (`OR_HIGH`, `OR_LOW`):

| State        | Condition |
|--------------|-----------|
| `ARMED`      | fewer than 2 consecutive 1m closes outside the OR window |
| `SATISFIED`  | 2+ consecutive 1m closes outside the window (above OR_HIGH for LONG, below OR_LOW for SHORT) |
| `BROKEN`     | a previous SATISFIED then a single close back inside the window |

`SATISFIED` is the trigger for Section III's Entry 1; `BROKEN` arms a re-evaluation but does not auto-fire.

## Section III — Entry 1 + Entry 2 (scaled in)

Once Section I-LONG (or I-SHORT) is open AND the ticker's Section II gates are both green, **Entry 1** fires at the next 1-minute close: 50% of full size at the current price.

**Entry 2** scales in to full size when, after Entry 1 has been held at least three 1-minute bars:

- Position is currently in profit (`unrealized > $100`), AND
- Sovereign Anchor still on (`I-ANCHOR` open), AND
- QQQ Market Shield still on for this side.

Entry 2 raises the position's `phase` to phase B and stamps `v5104_entry2_fired = True`. The dashboard's per-position row carries this flag.

## Section IV — Sovereign Brake + Velocity Fuse

Two non-negotiable exit triggers, evaluated every minute:

- **Sovereign Brake.** If unrealized P&L on a single position drops to **≤ −$500**, the position is force-closed at the current 1m close. This is the dollar-stop floor and supersedes phase logic. The dashboard surfaces *distance until brake* per position as `sovereign_brake_distance_dollars` (= `unrealized + 500`); negative or near-zero values mean an imminent or already-tripped brake.
- **Velocity Fuse.** If the 1-minute candle moves ≥ 1.0% against the position (the candle's open-to-close drop for a LONG, or open-to-close rise for a SHORT), the position closes at the current 1m close.

Both triggers fire independently of phase. The brake and the fuse are the only "dollar-driven" exits in v5.10 — every other exit is structural (phase-based).

## Section V — Phase A/B/C Triple-Lock

Each open position progresses through three phases. The phase governs the **structural** stop and the take-profit ratchet.

| Phase | Entry condition                                                       | Behavior |
|-------|-----------------------------------------------------------------------|----------|
| `A`   | Just opened (Entry 1 or Entry 2 has fired).                            | "Survival" — original stop. No ratchet. |
| `B`   | Entry 2 has fired AND 5+ bars held.                                    | "Neutrality layered" — stop moves to entry price (gravity trade). |
| `C`   | Phase B held 15+ bars AND position currently in profit.                | "Neutrality locked" / "Extraction" — stop moves to the last 5m structural pivot, locking gains. |

Phase progression is monotonic: a phase B/C never demotes back to A. EOD flush (Section VI) closes regardless of phase.

## Section VI — EOD Flush

At 15:55 ET (or earlier per `is_eod_flush_time`), every open position is flattened at the current 1m close. Realized P&L is folded into the daily P&L counter; the daily-circuit-breaker check (`daily_circuit_breaker_tripped(cumulative_realized_pnl)`) blocks any further new entries when the day's cumulative realized P&L drops below the tripwire.

---

## Vocabulary mapping (Eye of the Tiger metaphor → spec terms)

| Eye of the Tiger memo term | v5.10 spec term |
|----------------------------|----------------|
| Sovereign Anchor           | Section I-ANCHOR (QQQ vs 9:30 AVWAP)            |
| Market Shield              | Section I QQQ 5m close vs 9-EMA                  |
| The Eye                    | Section II Volume Bucket gate                    |
| The Tiger's stalk          | Section II Boundary Hold (2+ closes outside OR)  |
| First strike / Pounce      | Entry 1                                          |
| Second strike / Triple Lock | Entry 2 + Phase B                               |
| The Sovereign Brake        | −$500 unrealized force-close                    |
| Velocity Fuse              | 1% adverse 1m candle move                        |
| Phase A / B / C            | Survival / Neutrality layered / Extraction      |
| Sundown                    | EOD flush at 15:55 ET                           |
