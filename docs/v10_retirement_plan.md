# v10 ORB — Dead-Code Retirement Plan

> Companion to `docs/v10_strategy_keystone.md`. This doc enumerates the
> legacy code that v10 ORB has replaced and lays out the criteria + plan
> for removing it.

**Status as of v7.21.0**: v10 ORB is the live trading path on all 3
portfolios (Main / Val / Gene). Tiger Sovereign + Eye-of-Tiger remain
as a fallback when `ORB_LIVE_MODE=0`. This doc is the bridge to "v10
only" — fully removing the legacy fallback in a future PR series.

---

## Why retire?

1. **Strategy-level dead code**: Tiger Sovereign V570/V560 gates,
   Sentinel A/B/C alarms, V730 cooldowns, Bison ratchet — none of these
   fire in production once `ORB_LIVE_MODE=1`.
2. **Architectural drift**: every PR that touches these modules pays
   tax for code that's behind a kill switch.
3. **Operator confusion**: `/strategy` and `/algo` Telegram commands
   still describe Tiger Sovereign as the live strategy. They mislead.
4. **CLAUDE.md staleness**: references `entry_gate_v5.py`, `bison_v5.py`,
   `config.py` — none of which exist in the current codebase.

---

## Retirement criteria (all must be met before PR14 ships)

- [x] v10 entry path live in production (PR7 v7.15.0)
- [x] v10 exit path live in production (PR9 v7.17.0)
- [x] v10 sizing path live in production (PR10 v7.18.0)
- [x] Multi-portfolio support validated (Main / Val / Gene)
- [x] 3 cross-validation splits show v10 stable (per keystone)
- [ ] **At least 5 paper-trading days observed in production with
      ORB_LIVE_MODE=1** before retirement begins. *(NOT YET MET — this
      is the gating criterion. Retirement should NOT proceed until
      live data confirms v10 behaves as backtested.)*
- [ ] Operator (Val) explicit go-ahead on the retirement plan

---

## What gets deleted

### Phase 1: top-level legacy modules

| file | LOC | reason | imported by |
|---|---:|---|---|
| `tiger_buffalo_v5.py` | ~600 | Tiger/Buffalo FSM; replaced by `orb/exits.py` + `orb/state.py` | `trade_genius.py`, `engine/sentinel.py` |
| `eye_of_tiger.py` | ~350 | DI/ADX evaluators; replaced by v10 day_gates | `trade_genius.py`, `engine/scan.py`, `broker/orders.py` |
| `qqq_regime.py` | ~200 | retired per v5.9.1 (still in tree) | `trade_genius.py` |
| `v5_10_1_integration.py` | ~400 | live-engine glue for v5.10; replaced by `orb/live_runtime.py` | `engine/scan.py`, `trade_genius.py` |
| `v5_10_6_snapshot.py` | ~150 | spec snapshot for retired version | `paper_state.py` |
| `v5_13_2_snapshot.py` | ~150 | spec snapshot for retired version | `paper_state.py` |

### Phase 2: in-file legacy paths

After Phase 1 deletes the modules, these in-file paths can be cut:

- `engine/scan.py:_per_ticker_tick`: the `else:` branch for legacy
  callbacks.check_entry / check_short_entry (run when `ORB_LIVE_MODE=0`)
- `broker/positions.py:manage_positions`: the legacy `_run_sentinel`
  fallback for non-v10 positions (keeping defensive code for legacy-held
  positions during the transition window is fine; deletion is the v10-only
  end state)
- `broker/orders.py:execute_breakout`: the Tiger Sovereign sizing-label
  + V730 cooldown branches
- `broker/orders.py:check_breakout`: V570 strike count, V560 gate,
  QQQ permit logic — all bypassed by v10's check_entry path

### Phase 3: trade_genius.py legacy globals

The following globals in `trade_genius.py` are no longer mutated by any
live code path once Phase 1+2 ships:

- `_v570_session_hod` / `_v570_session_lod` / `_v570_strike_count`
- `_post_loss_cooldown` / `_post_exit_cooldown`
- `V730_*` / `V740_*` / `V750_*` / `V770_*` / `V780_*` env-bridged toggles
- `LOCAL_OVERRIDE_*` flags
- `_qqq_weather_tick` and the QQQ Phase 1 permit machinery

### Phase 4: Telegram surfaces

Update or delete:

- `cmd_strategy` (`telegram_commands.py:685`): rewrite to describe v10
  ORB
- `cmd_algo` (`telegram_commands.py:595`): rewrite to describe v10
- `cmd_mode` (`telegram_commands.py:524`): legacy mode-switching;
  retire (v10 has no mode toggle)
- `market_brief.py`: legacy daily brief; rewrite to use v10 state

### Phase 5: dashboard surfaces

Update or delete:

- `weather-check` banner in `index.html` (depends on tiger_sovereign
  data that won't emit after Phase 3)
- `Permit Matrix` in `index.html` + `renderPermitMatrix()` in `app.js`
  (~600 LOC of complex JS) — replaced by v10 Day Status banner +
  Projection card
- `Earnings Watcher` card (replaced by v10's per-ticker earnings gate)
- `Lifecycle` tab — repurpose or remove

### Phase 6: tests

Many tests under `tests/` reference legacy modules. After deletion they
need to be either updated to reference v10 or deleted as obsolete:

- `tests/test_strike_cap_unified.py`
- `tests/test_v6*` files (24 of them)
- `tests/test_v7_0_2_strike_recursion.py`
- `tests/test_universe_guard.py` (may need update only)
- `tests/test_v610_atr_or_break.py`
- `tests/earnings_watcher/test_runner.py`
- All tests that import `tiger_buffalo_v5` / `eye_of_tiger`

---

## Retirement order (proposed)

1. **PR15 v7.23.0** — *Phase 4 + 5 deletions*: Telegram + dashboard
   legacy surfaces. Lowest risk because these are presentation-only;
   no trade decisions affected.
2. **PR16 v7.24.0** — *Phase 2 + 3*: in-file legacy paths in
   scan/positions/orders, plus the no-longer-mutated trade_genius
   globals. Moderate risk; full smoke + paper-fire verification.
3. **PR17 v7.25.0** — *Phase 1 module deletion*: actually delete the 6
   top-level files. Easy after PR16 because no live code path
   references them.
4. **PR18 v7.26.0** — *Phase 6*: legacy test cleanup. Pure cleanup;
   smallest risk.
5. **PR19 v7.27.0** — *rename pass*: drop `_v5` suffix where modules
   stay (e.g. `trade_genius_v5.py` → `trade_genius.py` if applicable),
   `bot_version.py` already named cleanly.

---

## Operational guardrails (during retirement)

- **Each PR ships behind same kill-switch**: `ORB_LIVE_MODE=0` should
  remain functional through PR15 (presentation-only). After PR16 the
  kill switch is moot since legacy paths are gone.
- **Paper-trade between each PR**: deploy, watch 1 trading day, verify
  forensic logs (`[V79-ORB-*]`) match expected entries/exits.
- **Roll-back plan**: each PR is a single commit on `main`. Any PR
  can be reverted via `git revert <sha>` and a re-deploy.

---

## What stays (do NOT delete)

- `engine/portfolio_book.py` — multi-portfolio container, used by v10
- `engine/scan.py` — core scan loop (with v10 wiring)
- `engine/timing.py` — timezone helpers
- `engine/bars.py` — bar aggregation
- `engine/callbacks.py` — Protocol surface for engine
- `engine/extended_universe.py` — universe resolution
- `engine/feature_flags.py` — env-flag bridge
- `engine/sentinel.py` — verify after Phase 2; may go
- `broker/orders.py`, `broker/positions.py`, `broker/lifecycle.py` —
  core broker glue (with v10 wiring)
- `executors/` — per-portfolio Alpaca clients
- `paper_state.py` — persistence layer
- `bar_archive.py` — forensic capture
- `volume_profile.py` — used by RTH session detection
- `forensic_capture.py` — preserved for incident triage
- `dashboard_server.py` — needs frontend update later

---

## Live-mode validation checklist (gating criterion)

Before starting PR15, verify on production paper account:

- [ ] At least 5 trading days with `ORB_LIVE_MODE=1`
- [ ] At least 10 v10 entries observed (per `[V79-ORB-ENTRY]` log)
- [ ] At least 5 v10 exits with each reason: `V10_TARGET`, `V10_STOP`,
      `V10_BE_STOP`
- [ ] Daily P&L distribution shape matches backtest expectation
      (Sharpe > 1.5 over the observation window)
- [ ] No `V79-ORB-FEED`/`V79-ORB-EXIT` errors in logs
- [ ] Dashboard `/api/state.v10` shows sensible state on each session
- [ ] Telegram `/status` shows correct v10 block

---

**Last updated**: 2026-05-10 (v7.21.0 release)
**Owner**: Val
**Plan author**: Manager Agent (rule #0) + assistant
