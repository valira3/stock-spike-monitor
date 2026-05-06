# v6.13.0 — SPY Undecided-Regime Auto-Pause Gate

**Status:** SPEC. No code changes in this version.
**Owner:** Val
**Targets:** v6.13.0 (bundles with cancel-first-then-enter and 09:50 long lockout)
**Option:** Operator chop-day diagnosis Option 3

---

## Summary

Add a pre-permit entry gate that detects intraday whipsaw/chop conditions and
pauses ALL new entries for a configurable window. The gate fires on either of two
independent criteria: (1) SPY price sitting near its prior-day close (PDC) while
its 5m EMA9 has been crossed repeatedly in the recent lookback window, or (2) the
bot's own `section_i_permit` (long\_open / short\_open) having flipped sides
multiple times in the past hour — a self-referential chop detector that catches
regimes SPY's absolute delta misses.

Existing positions are **not** affected. This is an entry-only gate.

---

## Problem Statement

On **2026-05-04**, the bot whipsawed through 3 permit flips in a 100-minute
window (08:44–10:30 CDT), recording 11 trades (4W / 7L) for **−$139.59
realized** plus 4 open positions all underwater at session end.

Root cause: SPY and QQQ swung ±0.30% intraday with no follow-through. The
OR-breakout and DI-confirmation strategies that drive Section I/II are
specifically calibrated for trending regimes. In a non-trending, mean-reverting
(chopping) regime those same signals become noise: the 5m close repeatedly
crosses EMA9 in both directions, the global permit toggles long↔short, and the
bot re-enters on each toggle.

The existing gate stack has no "this is choppy, dial back" governor:

| Existing gate | Why it doesn't help |
|---|---|
| Daily circuit breaker (`_v570_kill_switch_active`) | Activates only after −$1,500 realized; chop days often land at −$150 |
| Post-exit same-ticker cooldown (v6.11.13, 10 s) | Per-ticker, not regime-aware; churn can be across different tickers |
| Sovereign Brake (−$500 unrealized) | Position-level, not entry-level |
| Global permit (`evaluate_global_permit`) | Gives the signal — does not count how often the signal has been wrong today |

**The fix** is a pre-permit gate, evaluated before the global-then-local permit
ladder, that measures regime indecision directly and pauses entry while the
condition persists.

---

## Design

### 3.1 Gate criteria

The gate fires — pausing all new entries for `UNDECIDED_PAUSE_MINUTES` — if
**either** of the following independent criteria is true at decision time.

#### PRIMARY: SPY undecided

Both sub-conditions must hold simultaneously:

```
|SPY_price - SPY_PDC| / SPY_PDC  <  UNDECIDED_SPY_DELTA_PCT   (A)
AND
count(5m EMA9 crosses by SPY in last UNDECIDED_LOOKBACK_MINUTES)
                                 >=  UNDECIDED_FLIP_THRESHOLD  (B)
```

**Sub-condition A — Near-PDC anchor.** Compares the most recent SPY price
(same source as `qqq_last` in the existing gate stack; use FMP quote if
available, Yahoo 1m bar `current_price` otherwise) against SPY's prior-day
close retrieved from `tg.pdc["SPY"]` or `tg.get_fmp_quote("SPY")`. A ratio
below 0.50% (default) means SPY has not yet established a directional bias
for the day.

**Sub-condition B — EMA9 cross count.** Count the number of times the SPY 5m
close line has crossed its 5m EMA9 (either direction: above→below or
below→above) in the past `UNDECIDED_LOOKBACK_MINUTES` minutes. SPY 5m data is
already maintained in `tg._SPY_REGIME` (same pattern as `tg._QQQ_REGIME` used
at line 274 of `broker/orders.py`). If `tg._SPY_REGIME` does not exist at
ship time, the gate falls back to fetching SPY 5m closes via the existing
`tg.fetch_1min_bars("SPY")` path and resampling — see **§3.4 Implementation
notes**.

> Rationale for gating on BOTH A and B: A trending SPY that happens to retrace
> through EMA9 once (e.g., +1.5% off PDC, 1 cross) should NOT be blocked.
> A SPY that is +0.1% off PDC and has crossed EMA9 three times in 30 minutes
> absolutely should be. Requiring both prevents blocking healthy trending days
> with a single reactive candle.

#### SECONDARY: Permit churn

```
count(section_i_permit direction flips in last PERMIT_FLIP_LOOKBACK_MIN minutes)
                                 >=  PERMIT_FLIP_THRESHOLD
```

A "flip" is a transition in `section_i_permit` from `long_open=True,
short_open=False` to the inverse (or vice versa). The gate module maintains a
timestamped flip log: each time `check_breakout` is called and `permit_res`
changes direction vs. the previous call, the timestamp is appended to a bounded
deque `tg._undecided_permit_flip_log` (max 200 entries). At gate evaluation
time, count entries within the lookback window.

> Rationale: permit churn is the bot's own revealed observation that the market
> is indecisive. Two long→short→long transitions in 60 minutes are
> statistically a reliable chop indicator even when SPY's PDC delta is moderate.
> This criterion catches mid-session chop that begins after SPY has already moved
> 0.6–0.8% off PDC (clearing threshold A) but then stalls and reverses.

### 3.2 Pause mechanics

When the gate fires:

- Set `tg._undecided_gate_paused_until` (UTC datetime) =
  `utcnow() + timedelta(minutes=UNDECIDED_PAUSE_MINUTES)`.
- Set `tg._undecided_gate_reason` = `"spy_chop"` or `"permit_churn"` (primary
  criterion that fired; if both fired simultaneously, use `"spy_chop"` as
  canonical reason since it is the more observable signal).
- On every subsequent call to `check_breakout`, re-evaluate the gate at the top
  of the function. If `utcnow() < tg._undecided_gate_paused_until`, return
  `(False, None)` immediately and emit a concise repeat-block log line (rate-
  limited to once per 60 s to avoid log spam).
- When `utcnow() >= tg._undecided_gate_paused_until`, clear the pause state and
  emit a CLEAR log line.

**The pause is NOT auto-extended.** At expiry, the gate re-evaluates criteria
fresh on the next `check_breakout` call. If conditions still hold, a new pause
window starts (and a new BLOCK log line is emitted). This prevents silent
indefinite blocking while still handling sustained chop.

**The pause does NOT block:**
- `manage_positions` / `manage_short_positions` exit logic
- Sentinel stop triggers
- Trailing-stop overrides
- EOD position flush
- `_v5104_maybe_fire_entry_2` (Entry 2 on an existing position is an add, not a
  new entry — it may optionally be gated; see **§6 Open questions**)

### 3.3 Hook location in `broker/orders.py`

The gate must be evaluated **before** the existing global-then-local permit
ladder so the local-weather override cannot punch through it (same design
rationale as the 09:50 long lockout documented in
`/home/user/workspace/v6_12_0_sweep/FOLLOWUPS.md`).

**Full hook order inside `check_breakout` after the existing early-exit checks
(`_trading_halted`, `_scan_paused`, blocklist, kill-switch, OR-data, strike cap,
daily count):**

```
1. [NEW v6.13.0] Undecided-regime gate           ← this spec
2. [NEW v6.13.0] 09:50 long lockout              ← FOLLOWUPS.md
3. Existing Section I global permit              ← lines 280-287
4. Existing local-weather override               ← lines 289-353
5. Volume bucket / Boundary hold                 ← lines 355+
6. Post-loss cooldown                            ← upstream of execute_breakout
7. Post-exit cooldown (v6.11.13)                 ← upstream of execute_breakout
```

Concretely, insert the gate block immediately **after** the existing daily-count
cap check at line ~180 (after `if daily_count.get(ticker, 0) >= 3: return
False, None`) and **before** the `fetch_1min_bars(ticker)` call at line ~203.
This position is after all cheap guard-rails (halted, paused, blocklist,
kill-switch, strike cap, daily count) but before any I/O or signal evaluation:

```python
# v6.13.0 — undecided-regime gate. Evaluated BEFORE the permit ladder so
# the local-weather override cannot punch through. Entry-only; does not
# affect exit logic.
if getattr(tg, "UNDECIDED_REGIME_GATE_ENABLED", True):
    _undecided_block, _undecided_reason = tg._v613_undecided_gate_check(now_et)
    if _undecided_block:
        tg.logger.info(
            "[V613-UNDECIDED] BLOCK ticker=%s reason=%s paused_until=%s",
            ticker,
            _undecided_reason,
            tg._undecided_gate_paused_until.strftime("%H:%M:%S") if tg._undecided_gate_paused_until else "?",
        )
        return False, None
```

The helper `tg._v613_undecided_gate_check(now_et)` lives in `trade_genius.py`
(see §3.4). It returns `(bool blocked, str reason)`.

### 3.4 Implementation notes

**SPY data source.** The gate needs SPY 5m closes for the past
`UNDECIDED_LOOKBACK_MINUTES` minutes. Preferred: extend `tg._SPY_REGIME` (same
rolling-bar structure as `tg._QQQ_REGIME`) with a bounded deque of the last N
5m bars. If `_SPY_REGIME` does not exist at implementation time, use
`tg.fetch_1min_bars("SPY")` to get 1m closes and compute a synthetic 5m EMA9
crossing count from the 1m series. Either approach must tolerate None/missing
bars gracefully — if SPY data is unavailable, the PRIMARY criterion is treated
as **not firing** (fail-open for SPY data unavailability).

**EMA9 crossing count algorithm.** Given a sequence of 5m closes `c[0..n]` and
corresponding EMA9 values `e[0..n]`, a cross at index `i` is defined as
`sign(c[i] - e[i]) != sign(c[i-1] - e[i-1])`. Count the number of such sign
changes within the lookback window. Ties (c == e) are treated as no-change.

**SPY PDC.** Use `tg.pdc.get("SPY")`. If not present, attempt
`tg.get_fmp_quote("SPY").get("previousClose")`. If neither is available,
sub-condition A is treated as **not firing** for that evaluation.

**Permit flip log.** `tg._undecided_permit_flip_log` is a
`collections.deque(maxlen=200)` of `datetime` objects (UTC). Each time
`check_breakout` observes a direction change in `permit_res` relative to
`tg._undecided_last_permit_direction`, append `utcnow()` and update
`tg._undecided_last_permit_direction`. "Direction" is encoded as `"long"` when
`permit_res["long_open"]` is True, `"short"` when `permit_res["short_open"]` is
True, and `"none"` when both are False. A flip is any transition that changes
this label.

**Pause state initialization.** On bot startup (and on replay init),
`tg._undecided_gate_paused_until = None` and
`tg._undecided_gate_reason = None`. A None paused\_until means no active pause.

**Replay / backtest parity.** The gate uses `utcnow()` for the pause-until
comparison. In replay, `tg._utc_now()` must return the replayed bar timestamp,
not wall clock. Confirm that `tg._now_et()` (already used at line 117 of
`orders.py`) is replay-aware; if so, derive UTC from it. The gate should be
disableable via `UNDECIDED_REGIME_GATE_ENABLED=false` for byte-identical replay
comparison against v6.12.0.

---

## Environment Variables

| Variable | Type | Default | Range / Notes |
|---|---|---|---|
| `UNDECIDED_REGIME_GATE_ENABLED` | bool | `true` | Set `false` to fully disable (no-op); use for replay parity and Phase 1 shadow mode requires code path enabled but enforcement flag below |
| `UNDECIDED_SPY_DELTA_PCT` | float | `0.50` | 0.20–1.00. Percent of PDC. SPY within this band triggers sub-condition A. |
| `UNDECIDED_LOOKBACK_MINUTES` | int | `30` | Minutes of 5m SPY bar history to scan for EMA9 crosses. |
| `UNDECIDED_FLIP_THRESHOLD` | int | `2` | Minimum EMA9 cross count in lookback window to satisfy sub-condition B. |
| `PERMIT_FLIP_THRESHOLD` | int | `2` | Minimum permit direction changes in lookback window to trigger SECONDARY criterion. |
| `PERMIT_FLIP_LOOKBACK_MIN` | int | `60` | Minutes of permit flip history to evaluate for SECONDARY criterion. |
| `UNDECIDED_PAUSE_MINUTES` | int | `30` | Duration of entry pause when gate fires. |
| `UNDECIDED_ENFORCE` | bool | `false` | **Phase 1 shadow switch.** When `true`, the gate actually blocks entries. When `false` (Phase 1 default), gate evaluates and logs BLOCK tags but returns `(False gate_fired, ...)` so entry proceeds. Both require `UNDECIDED_REGIME_GATE_ENABLED=true`. |

> Note: `UNDECIDED_REGIME_GATE_ENABLED=false` is a hard kill-switch that skips
> all evaluation (no logs, no state mutation). `UNDECIDED_ENFORCE=false` runs
> evaluation and logging in shadow mode. Use `UNDECIDED_ENFORCE=true` for Phase 2
> enforcement.

---

## Affected Files

| File | Change |
|---|---|
| `broker/orders.py` | Add undecided-regime gate block in `check_breakout` (before `fetch_1min_bars(ticker)`) |
| `broker/orders.py` | Add permit-flip logging after `permit_res` is evaluated (lines ~288, ~329) |
| `trade_genius.py` | Add `_v613_undecided_gate_check(now_et)` helper |
| `trade_genius.py` | Add `_undecided_permit_flip_log`, `_undecided_gate_paused_until`, `_undecided_gate_reason`, `_undecided_last_permit_direction` instance attrs |
| `eye_of_tiger.py` | Add env var declarations: `UNDECIDED_REGIME_GATE_ENABLED`, `UNDECIDED_SPY_DELTA_PCT`, `UNDECIDED_LOOKBACK_MINUTES`, `UNDECIDED_FLIP_THRESHOLD`, `PERMIT_FLIP_THRESHOLD`, `PERMIT_FLIP_LOOKBACK_MIN`, `UNDECIDED_PAUSE_MINUTES`, `UNDECIDED_ENFORCE` |
| `api/state.py` (or equivalent) | Extend `/api/state.gates` to expose `undecided_regime` sub-object |
| `bot_version.py` / `trade_genius.py` | Bump to 6.13.0 (shared with cancel-first and 09:50 lockout) |
| `ARCHITECTURE.md` | Section on regime-aware pre-permit gates |
| `trade_genius_algo.pdf` | Mandatory update — v6.13.0 is a minor version |

---

## Code Hooks

### Hook A — `check_breakout` entry point (`broker/orders.py`)

Insert immediately after `if daily_count.get(ticker, 0) >= 3: return False, None`
and before `bars = tg.fetch_1min_bars(ticker)`:

```python
# v6.13.0 — undecided-regime gate (option 3, chop-day diagnosis).
# Must be BEFORE the permit ladder so local-weather override cannot bypass it.
# See specs/v6_13_0_undecided_regime_gate.md.
if getattr(tg, "UNDECIDED_REGIME_GATE_ENABLED", True):
    _urg_blocked, _urg_reason = tg._v613_undecided_gate_check(now_et)
    if _urg_blocked:
        _urg_until = tg._undecided_gate_paused_until
        tg.logger.info(
            "[V613-UNDECIDED] BLOCK ticker=%s reason=%s paused_until=%s",
            ticker,
            _urg_reason,
            _urg_until.strftime("%H:%M:%S") if _urg_until else "?",
        )
        if getattr(tg, "UNDECIDED_ENFORCE", False):
            return False, None
        # Phase 1 shadow mode: log but allow entry to continue.
```

### Hook B — Permit flip tracking (`broker/orders.py`)

After `permit_res` is obtained (line ~288) and again after the local-weather
override result is determined (line ~329), call:

```python
tg._v613_record_permit_direction(permit_res, now_et)
```

This helper updates `tg._undecided_last_permit_direction` and appends to
`tg._undecided_permit_flip_log` when direction changes.

### Hook C — `tg._v613_undecided_gate_check` (`trade_genius.py`)

```python
def _v613_undecided_gate_check(self, now_et) -> tuple[bool, str]:
    """Evaluate undecided-regime gate. Returns (blocked, reason).

    reason is one of: 'spy_chop', 'permit_churn', or '' (not blocked).
    If currently in an active pause window, returns (True, cached_reason)
    without re-evaluating criteria.
    """
    import os
    from datetime import timedelta, timezone

    now_utc = now_et.astimezone(timezone.utc).replace(tzinfo=None)

    # Check active pause window first (cheapest path).
    if self._undecided_gate_paused_until is not None:
        if now_utc < self._undecided_gate_paused_until:
            return True, self._undecided_gate_reason or "active_pause"
        else:
            # Pause expired.
            self.logger.info("[V613-UNDECIDED] CLEAR resumed scanning")
            self._undecided_gate_paused_until = None
            self._undecided_gate_reason = None

    pause_min = int(os.environ.get("UNDECIDED_PAUSE_MINUTES", 30))

    # --- PRIMARY: SPY undecided ---
    spy_chop = self._v613_spy_chop_check()

    if spy_chop:
        self._undecided_gate_paused_until = now_utc + timedelta(minutes=pause_min)
        self._undecided_gate_reason = "spy_chop"
        return True, "spy_chop"

    # --- SECONDARY: permit churn ---
    permit_churn = self._v613_permit_churn_check(now_utc)

    if permit_churn:
        self._undecided_gate_paused_until = now_utc + timedelta(minutes=pause_min)
        self._undecided_gate_reason = "permit_churn"
        return True, "permit_churn"

    return False, ""
```

---

## Logging

### New log tags

| Tag | When emitted | Fields |
|---|---|---|
| `[V613-UNDECIDED] BLOCK` | Gate fires (new pause window started) or active pause is hit | `ticker=`, `reason=spy_chop\|permit_churn\|active_pause`, `paused_until=HH:MM:SS` |
| `[V613-UNDECIDED] CLEAR` | Pause window expires and scanning resumes | _(no additional fields)_ |
| `[V613-UNDECIDED] SPY_CHOP_CHECK` | Debug-level; emitted each evaluation | `spy_delta_pct=`, `ema9_crosses=`, `threshold_pct=`, `cross_threshold=`, `fire=True\|False` |
| `[V613-UNDECIDED] PERMIT_CHURN_CHECK` | Debug-level; emitted each evaluation | `flips_in_window=`, `threshold=`, `lookback_min=`, `fire=True\|False` |

**Example log lines:**

```
[V613-UNDECIDED] BLOCK ticker=AAPL reason=spy_chop paused_until=10:22:30
[V613-UNDECIDED] BLOCK ticker=TSLA reason=active_pause paused_until=10:22:30
[V613-UNDECIDED] CLEAR resumed scanning
```

### Rate-limiting

During an active pause, repeat BLOCK lines for individual tickers are
rate-limited to **one log line per ticker per 60 seconds** to prevent flooding
in high-frequency scan loops.

---

## Dashboard / API

### `/api/state.gates` extension

Add a new `undecided_regime` sub-object to the existing gates payload:

```json
{
  "undecided_regime": {
    "active": true,
    "reason": "spy_chop",
    "paused_until_utc": "2026-05-04T15:22:30Z",
    "spy_flips_30m": 3,
    "permit_flips_60m": 2,
    "enforce_mode": true
  }
}
```

| Field | Type | Description |
|---|---|---|
| `active` | bool | True when a pause window is currently in effect |
| `reason` | str | `"spy_chop"`, `"permit_churn"`, or `""` |
| `paused_until_utc` | str (ISO 8601) | UTC expiry of current pause; `null` when inactive |
| `spy_flips_30m` | int | Current EMA9 cross count in the last 30m (always populated, even when gate is inactive) |
| `permit_flips_60m` | int | Current permit flip count in the last 60m (always populated) |
| `enforce_mode` | bool | Mirror of `UNDECIDED_ENFORCE` env var |

`spy_flips_30m` and `permit_flips_60m` must be returned even when `active` is
false, so operators can monitor proximity to thresholds in real time without
waiting for a block event.

---

## Tests

The following test cases must be implemented (in the project's existing test
framework) before Phase 2 enforcement is enabled. Test code is not written here;
this section enumerates coverage requirements.

| Test name | Setup | Expected result |
|---|---|---|
| `test_spy_chop_blocks_entry` | SPY at +0.10% off PDC; 3 EMA9 crosses in the 30m lookback window; `UNDECIDED_ENFORCE=true` | `check_breakout` returns `(False, None)`; `[V613-UNDECIDED] BLOCK ... reason=spy_chop` is logged |
| `test_spy_trending_allows_entry` | SPY at +1.20% off PDC; 3 EMA9 crosses in the 30m lookback window | Gate PRIMARY does not fire (sub-condition A clears it); `check_breakout` proceeds to permit ladder |
| `test_permit_churn_blocks_entry` | 2 long→short→long permit flips in 45 minutes; `UNDECIDED_ENFORCE=true` | `check_breakout` returns `(False, None)`; `[V613-UNDECIDED] BLOCK ... reason=permit_churn` is logged |
| `test_pause_expires_after_30min` | Gate fires at T=0 (30-minute pause); subsequent call at T=31m | At T=31m: `_undecided_gate_paused_until` is cleared; `[V613-UNDECIDED] CLEAR` is logged; `check_breakout` re-evaluates fresh criteria |
| `test_existing_positions_unaffected` | Gate active (paused\_until in future); `manage_positions` / exit logic invoked | Exit logic runs to completion; no gate interference; only new entry path is blocked |
| `test_env_disable` | `UNDECIDED_REGIME_GATE_ENABLED=false` | Gate block is `_v613_undecided_gate_check(...)` is never called; no state mutation; `check_breakout` proceeds normally regardless of SPY or permit conditions |
| `test_shadow_mode_no_block` | `UNDECIDED_REGIME_GATE_ENABLED=true`, `UNDECIDED_ENFORCE=false`; SPY chop conditions met | `[V613-UNDECIDED] BLOCK` log line emitted but `check_breakout` does NOT return early; entry proceeds to permit ladder |
| `test_replay_parity` | 84-day SIP corpus; v6.13.0 with `UNDECIDED_REGIME_GATE_ENABLED=false` vs v6.12.0 | Total P/L delta ≤ $0.01 (byte-identical replay output) |

---

## Rollout Plan

### Phase 1 — Shadow / calibration (5 RTH sessions)

Deploy with:
```
UNDECIDED_REGIME_GATE_ENABLED=true
UNDECIDED_ENFORCE=false
```

Gate evaluates criteria and emits `[V613-UNDECIDED] BLOCK` / `CLEAR` log lines
on every `check_breakout` call but **does not block any entries**. Measure:

- How many times per session does the gate fire (spy\_chop vs permit\_churn)?
- What is the P/L of entries that would have been blocked?
- Does the gate fire on trending days (false-positive rate)?
- Are the default thresholds (`UNDECIDED_SPY_DELTA_PCT=0.50`,
  `UNDECIDED_FLIP_THRESHOLD=2`) calibrated appropriately, or do they over-fire?

Outcome threshold: gate should **not** fire on days where SPY moves >0.8% off
PDC with a clean trend. If it does, raise `UNDECIDED_SPY_DELTA_PCT` or
`UNDECIDED_FLIP_THRESHOLD` before Phase 2.

### Phase 2 — Enforcement

After Phase 1 calibration confirms false-positive rate is acceptable, enable
enforcement:
```
UNDECIDED_ENFORCE=true
```

Monitor for 5 additional RTH sessions. Compare daily P/L against the pre-gate
baseline. Expected improvement: reduction in chop-day losses of the order of the
2026-05-04 session (−$139.59 + underwater positions).

### Phase 3 — Threshold tuning

Using Phase 1 flag-rate data:
- If gate fired 0–1× on trending days but 3–8× on chop days: thresholds are
  well-calibrated; no change needed.
- If gate fired on trending days: increase `UNDECIDED_SPY_DELTA_PCT` (e.g., to
  0.65–0.80%) or increase `UNDECIDED_FLIP_THRESHOLD` to 3.
- If gate missed obvious chop days: decrease `UNDECIDED_SPY_DELTA_PCT` (to
  0.30%) or decrease `UNDECIDED_FLIP_THRESHOLD` to 2 (already default; consider
  1 for the most conservative setting).

All threshold changes are env-var only; no code changes required.

---

## Open Questions

1. **Entry 2 scoping.** Should `_v5104_maybe_fire_entry_2` (adding to an
   existing position) be blocked by the undecided-regime gate? Arguments for:
   adding to a position during chop amplifies losses. Arguments against:
   Entry 2 requires a fresh NHOD/NLOD + DI alignment on a position already open
   — blocking it is more disruptive. Recommendation: exclude Entry 2 from the
   gate for v6.13.0; revisit in v6.14.0 if chop-day add-on losses are material.

2. **SPY REGIME tracker.** If `tg._SPY_REGIME` does not yet exist (it is QQQ
   today), should v6.13.0 also introduce it, or should the gate use the 1m-bar
   resampling fallback? Introducing `_SPY_REGIME` is cleaner but more invasive.
   Use the 1m fallback for v6.13.0; track `_SPY_REGIME` as a follow-up.

3. **Pause auto-extend.** Should a second gate evaluation during an active pause
   window reset the timer? Current spec says no (pause is fixed-duration, not
   auto-extending). Revisit if Phase 2 data shows chop persisting well beyond
   30 minutes regularly.

4. **Dashboard polling.** The `/api/state.gates` endpoint is currently polled
   on a fixed interval. The undecided-regime sub-object values (`spy_flips_30m`,
   `permit_flips_60m`) will be stale between polls. Acceptable for Phase 1–2;
   consider a WebSocket push for Phase 3 if operators want real-time gate
   proximity monitoring.

---

## Sister Specs Cross-Reference (v6.13.0 bundle)

| Spec | Description | Status |
|---|---|---|
| `specs/v6_13_0_cancel_first_entry.md` | Cancel open protective stop before new entry to prevent Alpaca wash-trade reject (error 40310000) | SPEC — ready |
| `specs/v6_13_0_undecided_regime_gate.md` | This spec — SPY undecided-regime auto-pause gate | SPEC — ready |
| `FOLLOWUPS.md` (v6.13.0 section) | 09:50 long lockout — hard pre-permit time gate for LONG entries before 09:50 ET | DECIDED, awaiting implementation spec |

All three ship in the same v6.13.0 minor bump. Hook order in `check_breakout`:
undecided-regime gate → 09:50 long lockout → global permit → local-weather
override → downstream gate stack.
