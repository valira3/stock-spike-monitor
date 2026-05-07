# v7.2.0 — PMR + PMC + ATR-trail Extended-Hours Strategies

**Status:** SPEC v1
**Date:** 2026-05-07
**Author:** Computer/Val
**Scope:** Extended-hours only (pre-market 04:00-08:00 ET, post-market 16:15-19:55 ET). Zero RTH impact.

---

## Goals

Add two new extended-hours signals running in parallel with existing earnings_watcher DMI breakout (`signals.py`):

1. **PMR (Pre-Market Range break)** — fires on volume-confirmed break of frozen pre-market range during BMO window.
2. **PMC (Post-Market post-print Continuation)** — fires on volume-confirmed break of post-print 15-minute range during AMC window.

Both share a new ATR-trail exit overlay that runs alongside the existing DMI exit logic. ATR-trail tightens stops dynamically once the trade moves favorably 1× ATR.

## Non-Goals

- Touch RTH code (no edits to `engine/scan.py`, `engine/extended_universe.py`, or `trade_genius.py` scan loop).
- Add new universe gating (use existing `get_today_earnings_universe()`).
- Change DMI strategy behavior in any way.

---

## Architecture

### Strategy registry (new pattern)

`earnings_watcher/runner.py` currently runs ONE signal (`evaluate_and_size` calling DMI). v7.2.0 introduces a strategy registry:

```python
STRATEGIES = {
    "dmi": (evaluate_and_size_dmi, "EW_STRATEGY_DMI_ENABLED"),    # default ON
    "pmr": (evaluate_and_size_pmr, "EW_STRATEGY_PMR_ENABLED"),    # default OFF, opt-in
    "pmc": (evaluate_and_size_pmc, "EW_STRATEGY_PMC_ENABLED"),    # default OFF, opt-in
}
```

Each strategy is a pure function with the same signature:
```python
def evaluate_and_size_X(equity, ticker, bars, event_meta, open_exposure) -> Optional[Intent]
```

`Intent` adds two fields to the existing schema:
- `strategy`: str — one of "dmi", "pmr", "pmc". Tagged on every position record for telemetry.
- `exit_policy`: str — "dmi_legacy" (existing) or "atr_trail" (new). Each strategy chooses.

### Conflict resolution

In a single cycle, all enabled strategies are evaluated for every ticker. Resolution order:
1. If only one fires → take it.
2. If multiple fire same direction → take the one with highest `conviction_score` (DMI's existing `qs["score"]` for DMI; new `range_quality_score` for PMR/PMC).
3. If multiple fire opposite directions → skip the ticker entirely (log "conflict_opposite_direction").

### Window mapping

| Window | Strategies eligible | Notes |
|---|---|---|
| `premarket` (04:00-09:25 ET) | `dmi`, `pmr` | PMR freezes range at 08:00 ET, scans for break 08:00-09:25 ET |
| `afterhours` (16:00-19:55 ET) | `dmi`, `pmc` | PMC waits 15 min post-print (16:15 ET), scans for break 16:15-19:55 ET |

---

## PMR — Pre-Market Range Break

### Signal logic

1. **Build phase (04:00-08:00 ET / 08:00-12:00 UTC):** every minute, update running `pmr_high` and `pmr_low` from session bars.
2. **Freeze (at 12:00 UTC = 08:00 ET):** capture frozen `range_high`, `range_low`, `range_width = range_high - range_low`, `pre_volume_avg = mean(volume[04:00-08:00])`.
3. **Quality gates:**
   - `range_width / range_low >= 0.005` (range ≥ 0.5% of price; below this is noise)
   - `range_width / atr_5min >= 1.0` (range is ≥ 1× ATR; below means range hasn't expressed real vol)
   - At least 60 minute-bars in build phase (≥ 60 min of data; rejects illiquid names with sparse pre-market quotes)
4. **Scan phase (08:00-09:25 ET / 12:00-13:25 UTC):**
   - Long entry: bar close > `range_high` AND bar volume ≥ 1.5× `pre_volume_avg`
   - Short entry: bar close < `range_low` AND bar volume ≥ 1.5× `pre_volume_avg`
   - First confirmed break only; subsequent same-direction breaks ignored (idempotent via evaluated cache).
5. **Hard exit at 13:25 UTC (09:25 ET):** never carries into RTH.

### Sizing

Reuse `dmi_sized_notional()` with one adjustment:
- `PMR_BASE_NOTIONAL_PCT = 0.05` (half DMI's 10%, since signal frequency is higher)
- `PMR_CONVICTION_SIZE_MAX = 2.0` (cap at 2x for higher-frequency strategy)

`conviction = range_width / range_low * 100` (range as % of price, capped at 5).

### Exit policy

Use new `atr_trail` exit (see ATR-trail section). Why ATR-trail not DMI exits:
- PMR signals on range expansion, so ATR is a natural stop unit.
- DMI's 3% hard stop is too wide for typical 0.5-1.5% pre-market ranges.

### Env flags

- `EW_STRATEGY_PMR_ENABLED=0` (default OFF)
- `PMR_RANGE_FREEZE_UTC_MIN=720` (08:00 ET = 720 min from UTC midnight; configurable)
- `PMR_VOLUME_MULT=1.5`
- `PMR_MIN_RANGE_PCT=0.005`
- `PMR_BASE_NOTIONAL_PCT=0.05`
- `PMR_HARD_EXIT_UTC_MIN=805` (13:25 UTC = 09:25 ET)

---

## PMC — Post-Market Continuation

### Signal logic

1. **Wait phase (16:00-16:15 ET / 20:00-20:15 UTC):** ignore all bars (initial print volatility / noise).
2. **Build phase (16:15-16:30 ET / 20:15-20:30 UTC = 15 minutes):** track `pmc_high`, `pmc_low` over these 15 minutes.
3. **Freeze (at 20:30 UTC):** capture `range_high`, `range_low`, `range_width`, `print_volume_avg = mean(volume[16:15-16:30])`.
4. **Quality gates:**
   - `range_width / range_low >= 0.01` (post-print ranges are wider; require ≥ 1% range)
   - `range_width / atr_5min >= 1.0`
   - At least 12 minute-bars in build phase (≥ 12 of 15 min covered; rejects sparse-quote names)
5. **Scan phase (20:30-23:55 UTC = 16:30-19:55 ET):**
   - Long entry: bar close > `range_high` AND volume ≥ 1.5× `print_volume_avg`
   - Short entry: bar close < `range_low` AND volume ≥ 1.5× `print_volume_avg`
6. **Hard exit at 23:55 UTC (19:55 ET):** session_end already enforced by existing exit logic.

### Sizing

- `PMC_BASE_NOTIONAL_PCT = 0.07` (slightly above PMR; AMC is where most EW alpha lives historically)
- `PMC_CONVICTION_SIZE_MAX = 2.5`

`conviction = range_width / range_low * 100` (range as % of price, capped at 8).

### Exit policy

ATR-trail (same as PMR).

### Env flags

- `EW_STRATEGY_PMC_ENABLED=0` (default OFF)
- `PMC_WAIT_MIN=15`
- `PMC_BUILD_MIN=15`
- `PMC_VOLUME_MULT=1.5`
- `PMC_MIN_RANGE_PCT=0.01`
- `PMC_BASE_NOTIONAL_PCT=0.07`

---

## ATR-Trail Exit

New exit policy for PMR/PMC (DMI keeps its existing exits unchanged).

### Logic

Per cycle, after computing `chg = (close - entry) / entry * sign`:

1. Compute `atr_5min` from the most recent 14 bars (Wilder ATR, 5-min average true range).
2. If position is in a loss (`chg < 0`): hard stop at `-1.5 * atr_5min / entry_px` (atr-relative hard stop).
3. If position is in a profit ≥ `1.0 * atr_5min / entry_px` (1 ATR above entry):
   - Arm trail: `trail_active = True`
   - `trail_stop = peak_pct - 1.5 * atr_5min / entry_px`
4. If trail armed and `chg <= trail_stop`: exit "atr_trail".
5. Time stop: `elapsed_minutes >= 60` (tighter than DMI's 90; PMR/PMC are higher-frequency).
6. Session_end (defensive): existing logic in `evaluate_exit` handles this.

### Why ATR-trail

- Range-break strategies have variable risk (range_width is the natural stop unit). Hard 3% can be 6× the actual range.
- Empirically, range-break setups that work tend to expand 2-3× their breakout range; ATR-trail captures this without over-fitting fixed percentages.

### Public API

```python
def evaluate_atr_trail_exit(position_state, current_bar, recent_bars, elapsed_minutes) -> Tuple[bool, str]
```

Lives in new file `earnings_watcher/exits_atr.py` (does NOT modify existing `exits.py`).

---

## State namespace

Per-strategy idempotency cache: `evaluated_today.json` schema extends:

```json
{
  "2026-05-07": {
    "premarket": {
      "dmi": ["DDOG", "COIN", ...],
      "pmr": ["MCHP", ...]
    },
    "afterhours": {
      "dmi": [...],
      "pmc": [...]
    }
  }
}
```

State helpers updated:
- `get_evaluated_tickers(date, window, strategy)` — backward compat: defaults to "dmi" when called without strategy.
- `mark_ticker_evaluated(date, window, ticker, strategy)` — same default.
- `clear_window_evaluated(date, window, strategy=None)` — None means clear all strategies for that window.

Migration: existing list-shaped data at `[date][window]` is auto-promoted to `[date][window]["dmi"]` on first read.

---

## Telemetry / observability

### last_cycle.json schema additions

```json
{
  "cycle": "window_premarket",
  "strategies_run": ["dmi", "pmr"],
  "per_strategy": {
    "dmi":  {"signals": 0, "skip_reasons": {...}, "evaluated": 18, ...},
    "pmr":  {"signals": 1, "skip_reasons": {"range_too_narrow": 12}, "evaluated": 18, ...}
  },
  ...existing fields aggregated across strategies...
}
```

### Telegram alert template

For each new fire:
```
🚀 EW PMR FIRE [premarket] DDOG LONG
range: $145.20-$148.80 (+2.5%)
break: $149.04 vol=2.3M (1.8x avg)
conv=2.5 → notional $5,000 qty=33
```

### Dashboard tile

Add to `dashboard_static/index.html` near the EW tile:
- "PMR" and "PMC" status pills (gray/green/red), bar count, recent fires (last 5 with PnL).

---

## File manifest

| File | Status | LoC |
|---|---|---|
| `earnings_watcher/signals_pmr.py` | NEW | ~150 |
| `earnings_watcher/signals_pmc.py` | NEW | ~140 |
| `earnings_watcher/exits_atr.py` | NEW | ~100 |
| `earnings_watcher/runner.py` | MOD | ~80 changed (add registry + per-strategy dispatch) |
| `earnings_watcher/state.py` | MOD | ~40 changed (add strategy namespace) |
| `earnings_watcher/__init__.py` | MOD | ~5 (export new symbols) |
| `bot_version.py` | MOD | 1 (7.1.0 → 7.2.0) |
| `trade_genius.py` | MOD | 1 (BOT_VERSION = 7.2.0) |
| `dashboard_static/index.html` | MOD | ~20 (add tiles) |
| `dashboard_static/app.js` | MOD | ~30 (render new fields) |
| `v720_pmr_pmc_replay/replay.py` | NEW | ~180 (fork v6180_ew_replay) |

**Total: ~570 new + ~177 modified ≈ 750 LoC**

Aligns with Option 3 estimate (740 LoC).

---

## Backtest plan

1. Fork `v6180_ew_replay/replay.py` to `v720_pmr_pmc_replay/replay.py`.
2. Run on Phase 0 corpus (48 days × 313 tickers × 400 events).
3. Three runs:
   - DMI-only (control, expect $18,814 / 19 trades / 52.6% WR — byte-identical to v6.18.0)
   - DMI + PMR
   - DMI + PMR + PMC
4. Compare net PnL, max drawdown, trade frequency, win rate.
5. Ship if PMR/PMC additions show >$2k incremental PnL with <30% drawdown vs. control.

---

## Risk register

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| PMR fires too often, churns capital | Medium | Med | `EW_STRATEGY_PMR_ENABLED=0` default; opt-in only after backtest |
| ATR computation fails on sparse pre-market | Low | High | Fall back to DMI hard_stop logic if `atr_5min` is None |
| State migration corrupts existing DMI cache | Low | High | Auto-promote with explicit list/dict type check; backwards-compat |
| Conflict resolution drops genuinely good signals | Medium | Low | Log every conflict to last_cycle.json; tune after 2 weeks |
| Telegram spam from PMR fires | Low | Low | Rate-limit alerts to ≤5/hour per strategy |

---

## Rollout plan

1. Ship v7.2.0 with both flags OFF (`EW_STRATEGY_PMR_ENABLED=0`, `EW_STRATEGY_PMC_ENABLED=0`).
2. Run 48-day backtest. If PnL clears bar, enable PMR for 1 week shadow (paper) on Railway.
3. Monitor signal frequency, win rate, drawdown.
4. If shadow looks healthy, enable PMC for 1 week shadow.
5. After 2 weeks of clean shadow, both stay ON in paper.
