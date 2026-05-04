# Tiger Sovereign System \u2014 Strategy (v15.0, finalized 2026-04-30)

**Status:** Finalized production-ready specification. Supersedes every
prior version including the vAA-1 ULTIMATE morphing doc.
**Source:** `/home/user/workspace/tiger-sovereign-spec-v15-1.md`
(developer-issued canonical text).
**Anchor PR:** v5.20.0 \u2014 spec-v15-conformance.

This document is the in-repo strategy reference. The full canonical
spec lives in the workspace path above; this file mirrors it with the
fields each code surface needs to enforce. When the canonical spec
changes, update the canonical file, then this file, then tests, then
code \u2014 in that order.

---

## SECTION 0 \u2014 Glossary of terms

* **ORH / ORL.** Opening Range High / Low. Fixed price levels
  established at exactly **09:35:59**.
* **NHOD / NLOD.** New High / Low of Day. Dynamic price levels
  updating in real time as the ticker prints fresh session extremes.
* **HWM / HVP.** High Water Mark / High Value Point. The highest
  recorded value of an indicator (typically 5m ADX) during the trade
  lifecycle.
* **Trade_HVP.** Variable tracking peak 5m ADX during an active
  Strike. Resets to zero at the start of each new Strike.
* **Stored_Peak_Price / Stored_Peak_RSI.** Memory variables for
  Alarm E. Stored at the exact tick of every new NHOD/NLOD,
  unconditionally on the RSI relationship (the divergence signal is
  produced at query time by comparing live RSI to stored RSI).

---

## SECTION 1 \u2014 System concepts

### 1.1 Strike sequence
A Strike is one full trade lifecycle (Entry to Exit). **Maximum
3 Strikes per ticker per day.**

* **Sequential requirement.** A subsequent Strike cannot initiate
  until the previous position is fully flat (Position = 0).
* **Permission ladder:**
  * **Strike 1.** Triggered by 2x 1m close above ORH (long) /
    below ORL (short).
  * **Strike 2 & 3.** Triggered by 2x 1m close above the running
    NHOD (long) / below the running NLOD (short).

### 1.2 Alarm E \u2014 the divergence trap
The bot monitors the relationship between price and RSI(15) at every
new NHOD / NLOD.

* **Pre-entry filter.** If a price prints a new extreme but RSI(15)
  is diverging (lower for longs, higher for shorts), the bot is
  prohibited from opening new Strike 2 or Strike 3 positions.
* **Post-entry sentinel.** If divergence is detected while a
  position is open, immediately ratchet the resting STOP MARKET to
  Current Price \u00b1 0.25%.

---

## SECTION 2 \u2014 The Bison (long strategy)

### Phase 1 & 2 \u2014 Weather and permits
* **Weather.** QQQ(5m) > 9-EMA AND QQQ > 9:30 AM Anchor VWAP.
* **Permit.** Two consecutive 1m closes above the target level
  (ORH for Strike 1, NHOD for Strikes 2 & 3).
* **Volume gate.** 1m volume \u2265 100% of the 55-bar rolling
  average. **Required after 10:00 AM ET.**

### Phase 3 \u2014 The Strike (sizing & execution)
* **Authority check.** 5m DI+ MUST be > 25.
* **Momentum check.** 5m ADX > 20 AND **Alarm E = FALSE**.
* **Full Strike (100% size).** Trigger: 1m DI+ > 30.
  * **Order:** LIMIT at Ask \u00d7 1.001.
* **Scaled Strike (50% starter).** Trigger: 1m DI+ in [25, 30].
  * **Order:** LIMIT at Ask \u00d7 1.001.
  * **Scale-in.** Add the remaining 50% only if (1m DI+ > 30) AND
    (new NHOD) AND (Alarm E = FALSE).

---

## SECTION 3 \u2014 The Wounded Buffalo (short strategy)

Mirror of Section 2 with sign flips on every comparison:

### Phase 1 & 2 \u2014 Weather and permits
* **Weather.** QQQ(5m) < 9-EMA AND QQQ < 9:30 AM Anchor VWAP.
* **Permit.** Two consecutive 1m closes below the target level
  (ORL for Strike 1, NLOD for Strikes 2 & 3).
* **Volume gate.** Same 100% / 55-bar / required-after-10:00 contract.

### Phase 3 \u2014 The Strike (sizing & execution)
* **Authority check.** 5m DI\u2212 MUST be > 25.
* **Momentum check.** 5m ADX > 20 AND **Alarm E = FALSE**.
* **Full Strike (100% size).** Trigger: 1m DI\u2212 > 30.
  * **Order:** LIMIT at Bid \u00d7 0.999.
* **Scaled Strike (50% starter).** Trigger: 1m DI\u2212 in [25, 30].
  * **Order:** LIMIT at Bid \u00d7 0.999.
  * **Scale-in.** Add the remaining 50% only if (1m DI\u2212 > 30) AND
    (new NLOD) AND (Alarm E = FALSE).

---

## SECTION 4 \u2014 Shared rules and risk management

* **Entry window.** 09:36:00 to 15:44:59 EST. No new entries after
  15:44:59.
* **Hard protection.** Immediate resting STOP MARKET at \u2212$500
  (per position).
* **Daily circuit breaker.** Halt all trading and flatten all
  positions if session P&L reaches \u2212$1,500.
* **EOD flush.** Absolute market close at 15:49:59 EST.

---

## ADDENDUM \u2014 The Sentinels (exit protection)

| Alarm | Trigger | Action |
| ----- | ------- | ------ |
| **A. Flash Move** | 1m price move > 1% against position | MARKET EXIT |
| **B. Trend Death** | 5m candle closes across the 5m 9-EMA | MARKET EXIT |
| **C. Tiger Grip** | 3 consecutive 1m ADX declines | RATCHET STOP \u00b1 0.25% |
| **D. HVP Lock** | 5m ADX falls below 75% of session peak (HWM) | MARKET EXIT |
| **E. Divergence** | New extreme printed on lower (long) / higher (short) RSI(15) | RATCHET STOP \u00b1 0.25% (also blocks new S2/S3) |

---

## Implementation map (where each rule lives)

| Rule | Source surface |
| ---- | --------------- |
| Entry window 09:36\u201315:44:59 | `engine/timing.py` (`HUNT_START_ET`, `NEW_POSITION_CUTOFF_ET`); enforced in `broker/orders.py.check_entry`. |
| ORH/ORL freeze 09:35:59 | OR aggregation: `eye_of_tiger.py.OR_WINDOW_END_HHMM_ET = "09:36"` (half-open bound including the 09:35 candle); `trade_genius.py.collect_or` and `_fill_metrics_for_ticker.or_window_end` use the same `09:36` upper bound. |
| Strike 1 boundary (2x close above ORH / below ORL) | `eye_of_tiger.py.evaluate_boundary_hold` + `BOUNDARY_HOLD_REQUIRED_CLOSES = 2`. |
| Strike 2/3 boundary (NHOD/NLOD) | `broker/orders.py.check_entry` strike-aware boundary path; reads `tg._v570_session_hod[ticker]` / `tg._v570_session_lod[ticker]`. |
| Volume gate (\u2265 100% / 55-bar, required after 10:00 ET) | `volume_bucket.py.VolumeBucketBaseline.check`; live caller passes `now_et=ZoneInfo("America/New_York")` so the spec-mandatory time-conditional path activates. Default ON via `engine/feature_flags.py.VOLUME_GATE_ENABLED = True`. |
| Authority (5m DI\u00b1 > 25) | `broker/orders.py.check_entry` reads `tg.v5_adx_1m_5m(ticker)`. |
| Momentum (5m ADX > 20) | Hard gate in `broker/orders.py.check_entry`; fails closed if `adx_5m` is unavailable. |
| Alarm E pre-entry filter (S2/S3) | `engine/sentinel.py.check_alarm_e_pre`, called from `broker/orders.py.check_entry`; reads `broker/positions.py.get_divergence_memory()`. |
| Sizing (Full / Scaled) | `eye_of_tiger.py.evaluate_strike_sizing` wired into `broker/orders.py.execute_breakout`. FULL (1m DI\u00b1 > 30) fills 100% in one fill and pre-sets `v5104_entry2_fired=True` so the legacy Entry-2 add-on does not double-fill. SCALED_A (1m DI\u00b1 in [25, 30]) fills 50% starter; Entry-2 may top up under spec scale-in conditions. WAIT defensively aborts entry. Helper exceptions fall back to the legacy 50% starter so a sizing bug never blocks a trade. |
| Strike cap 3/day + sequential requirement | `tg.strike_entry_allowed(ticker, side, view)` invoked from `broker/orders.py.check_entry`. |
| Hard stop \u2212$500 | Polling loop + Sentinel backstop in `broker/positions.py`. |
| Daily circuit breaker \u2212$1,500 | `eye_of_tiger.py.DAILY_CIRCUIT_BREAKER_DOLLARS`. |
| EOD flush 15:49:59 | `engine/timing.py.is_after_eod_et`. |
| Alarm A (flash > 1%) | `engine/sentinel.py.check_alarm_a` with strict `<` velocity threshold. |
| Alarm B (5m EMA cross) | `engine/sentinel.py.check_alarm_b`. |
| Alarm C (3 ADX declines) | `engine/sentinel.py.check_alarm_c`. |
| Alarm D (HVP lock) | `engine/sentinel.py.check_alarm_d`. |
| Alarm E (divergence post-entry) | `engine/sentinel.py.check_alarm_e` + `engine/momentum_state.py.DivergenceMemory`. |

---

## Operator dashboard \u2014 spec surface

The Permit Matrix on Main and on the Val/Gene exec panels shows the
live verdict for each gate. v5.20.0 wires the **expanded detail row**
to print the verbatim v15.0 spec for each gate
(`renderPermitMatrix._pmtxBuildRow` in `dashboard_static/app.js`)
so operators can compare \"what the engine says\" to \"what the spec
says\" without leaving the UI.

---

## ADDENDUM C25 (v6.11.0) -- SPY Regime-B Short Amplification

### Motivation

When SPY's first-30-minute return falls in the moderately-down band
(-0.50% to -0.15%, exclusive on both ends), short-side entries show
a disproportionate edge: 60.78% WR / +$14.43/pair across 232 pairs in
the 84d v6.10.0 SIP corpus vs 46-55% WR on other bands. The intraday
stability finding confirms the edge concentrates in the first hour
post-classification (61.11% WR, 108 pairs, [10:00, 11:00) ET) and
collapses after 11:00 ET as regime-B days mean-revert.

### Short-side sizing branch (C25)

After the v15.0 tier decision (FULL / SCALED_A / WAIT) and before
`notional = current_price * shares`, the helper
`_maybe_apply_regime_b_short_amp` in `broker/orders.py` applies:

```
if V611_REGIME_B_ENABLED
   AND side == SHORT
   AND regime == B
   AND now_et in [arm, disarm):    # half-open; arm=10:00, disarm=11:00 ET
    shares = max(1, round(shares * V611_REGIME_B_SHORT_SCALE_MULT))
```

Default scale is 1.5x. Long entries are never affected. Regime=None
(feed gap or pre-classification) fails closed (no amplification).

### Regime classification

`spy_regime.SpyRegime` captures SPY price at 09:30 and 10:00 ET each
session and classifies into one of five bands:

- **A** (deep down): ret <= -0.50%
- **B** (moderately down): -0.50% < ret < -0.15%  [amp target]
- **C** (flat): -0.15% <= ret <= +0.15%
- **D** (moderately up): +0.15% < ret <= +0.50%
- **E** (deep up): ret > +0.50%

Log tag `[V611-REGIME-B]` fires once at 10:00 ET classification.
Log tag `[V611-AMP]` fires at every amplified entry.

### Rollback

`V611_REGIME_B_ENABLED=0` -- complete no-op, no code revert needed.

### Implementation map

| Rule | Source |
|---|---|
| Regime classification | `spy_regime.SpyRegime.tick()` / `classify()` |
| Env-var defaults | `eye_of_tiger.py` V611_REGIME_B_* block |
| Sizing amplifier | `broker/orders.py._maybe_apply_regime_b_short_amp` |
| Daily reset | `trade_genius.py.reset_daily_state` -> `_SPY_REGIME.daily_reset()` |
| SPY price feed | `trade_genius.py._qqq_weather_tick` existing SPY 1m fetch |
| Dashboard fields | `dashboard_server.py._v611_regime_snapshot()` |
| Frontend rows | `dashboard_static/app.js` _p3aRows SPY regime block |
