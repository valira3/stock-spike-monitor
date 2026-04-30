# Changelog

All notable changes to TradeGenius (formerly Stock Spike Monitor, renamed in v3.5.1).

---

## v5.20.5 — 2026-04-30 — Volume gate WS source + DI RTH fallback + expanded card metrics

### Why

Live forensics on Apr 30 (post-v5.20.4 deploy) revealed two distinct
health issues that were keeping the bot from firing trades even after
the Boundary Hold buffer started populating:

1. **Volume Bucket gate starved.** The dashboard showed `volume_feed_status: "live"`
   and `[VOLPROFILE]` log lines confirmed the WebSocket consumer was
   capturing real bucket volumes (e.g. NVDA 20206 @ 14:26 UTC), yet
   every single `[V5100-VOLBUCKET]` log line read `current_vol=0`. The
   entry gate at `broker/orders.py:347-352` was reading
   `bars["volumes"][-2]` from Yahoo, which ships `volume=0/None` on the
   trailing-edge bar for ~30-60s after each minute close.
2. **DI seed insufficient for the entire opening hour.** Alpaca IEX
   premarket data is too thin on most names (1-9 5m bars vs the
   required 15). At 09:36 ET, 0/10 tickers had cleared the
   `PREMARKET_DI_MIN_BARS=15` threshold; the existing
   `recompute_di_for_unseeded` job at 09:31 ET re-ran the same
   premarket-only seeder, which never produced more bars on its own.
   `tiger_di()` therefore returned `None` for every gate evaluation
   until enough RTH 5m bars accumulated organically (~10:30 ET).

A third workstream was already queued: the v5.20.3 component-state
card grid surfaced PASS/FAIL/PEND badges but no underlying numbers,
so operators couldn't tell *how close* a gate was to passing.

### Changes

**Change 1 — Volume gate WS-first lookup** (`broker/orders.py`)

- Extracted volume-source resolution into
  `_resolve_last_completed_volume(tg, ticker, now_et, bars)`.
- Precedence: WS consumer's `current_volume(ticker, previous_session_bucket(now_et))`
  → Yahoo `volumes[-2]` → Yahoo `volumes[-1]`. WS values <= 0 are
  treated as not-yet-captured and fall through to Yahoo.
- Mirrors the v5.5.5 swap that engine/scan.py already applied for the
  bar archive writer; the entry gate now uses the same source of truth.
- Smoke-test guard added: `broker/orders.py` source must contain
  `_ws_consumer.current_volume(`, `_resolve_last_completed_volume`,
  and `previous_session_bucket` (mirrors smoke_test.py:3859 for v5.5.5).

**Change 2 — DI seed RTH fallback** (`engine/seeders.py`, `trade_genius.py`)

- New `seed_di_buffer_with_rth_fallback(ticker)` in `engine/seeders.py`
  (line 523). Calls `seed_di_buffer(ticker)` first; if insufficient AND
  we are at/past 09:30 ET, fetches Alpaca IEX 1m bars from 09:30 ET to
  the most recently closed 5m boundary, buckets them into 5m OHLC,
  merges with the premarket cache (re-fetched and deduped by bucket),
  and commits to `_DI_SEED_CACHE` only when combined ≥ 15.
- `recompute_di_for_unseeded` (line 951) now dispatches to the new
  helper instead of `seed_di_buffer`.
- New scheduler entries at 10:00 ET and 10:30 ET retry the recompute
  for any ticker still missing a sufficient seed (in addition to the
  existing 09:31 ET job).
- Logs emit `DI_SEED_RTH ticker=X premarket_bars=N rth_bars=M combined=K
  sufficient=Y/N` so the cron health check can confirm progress.

**Change 3 — Dashboard expanded card metrics** (`v5_10_6_snapshot.py`,
`dashboard_static/app.js`, `dashboard_static/app.css`)

- `_per_position_v510` now ships three additional blocks per
  `TICKER:SIDE` entry:
  - `sovereign_brake`: `{unrealized_pct, brake_threshold_pct,
    brake_threshold_dollars: -500.0, time_in_position_min}`
  - `velocity_fuse`: `{last_5m_move_pct, fuse_threshold_pct: 1.0}`
  - `strikes`: `{strikes_count, strike_history}` (history is a stub
    list until a per-ticker event log is wired separately)
- `_vol_bucket_per_ticker` accepts a new `prev_minute_hhmm` param so
  the dashboard reads from the same just-closed bucket the entry gate
  uses; new fields: `current_1m_vol`, `baseline_at_minute`,
  `ratio_to_55bar_avg`, `days_available`, `lookup_bucket`.
- `_boundary_hold_per_ticker` adds `long_consecutive_outside` and
  `short_consecutive_outside` so the Boundary Hold card can show
  progress toward the 2-of-2 trigger.
- New `_di_per_ticker` block: `{di_plus_1m, di_minus_1m, di_plus_5m,
  di_minus_5m, threshold, seed_bars, sufficient}`.
- Dashboard `_pmtxComponentGrid()` now renders a key/value
  `.pmtx-comp-metrics` stack beneath each card state badge. Eight
  cards: P1 Weather, P2 Boundary, P2 Volume, P3 Authority, P3 Momentum,
  AL Sov.Brake, AL Velocity Fuse, POS Strikes. Null values render as a
  dimmed em dash so layout stays stable while data is warming up.
- CSS: `.pmtx-comp-metric-row` is a flex key/value line; metric stack
  is capped at 132px (≈8 rows) with overflow scroll so cards never
  stretch unbounded.

### Tests

- `tests/test_v5_20_5_vol_source_orders.py` — 9 tests covering WS-vs-Yahoo
  precedence, previous-vs-current bucket lookup, exception swallowing,
  premarket / weekend skips, and Yahoo-only edge cases.
- `tests/test_v5_20_5_di_seed_rth_fallback.py` — 5 tests covering the
  before-RTH skip, RTH bars insufficient, RTH bars sufficient, the
  idempotency short-circuit when premarket already passed, and the
  source-level routing of `recompute_di_for_unseeded` through the new
  helper.
- `tests/test_v5_20_5_card_metrics.py` — 8 tests covering all metric
  blocks present, SHORT pnl inversion, null-safety with missing data,
  malformed entry_time, legacy field preservation, and helper-level
  null-safety.
- Smoke-test guards added at `smoke_test.py:3863-3892` (volume gate
  WS check, DI seeder RTH-fallback wiring check).

### Live validation (post-deploy)

1. Pull recent prod logs and confirm `[V5100-VOLBUCKET]` lines now
   show `current_vol > 0` for all tickers during RTH.
2. After 09:35 / 10:00 / 10:30 ET retry jobs run, confirm
   `[GATE_EVAL]` lines stop showing `di=None` for tickers whose
   `seed_bars >= 16`.
3. Pull `/api/state` and inspect `per_ticker_v510.<t>.di`,
   `per_ticker_v510.<t>.vol_bucket.days_available`, and
   `per_position_v510.<key>.sovereign_brake` blocks.
4. Visually confirm dashboard expanded cards render the new metric
   rows.

---

## v5.20.4 \u2014 2026-04-30 \u2014 Boundary Hold close recorder fallback (Yahoo forming-bar fix)

### Why

During the Apr 30 morning session every short setup the bot saw was
being skipped with `V5100_BOUNDARY:insufficient_closes` and
`gate_2candle=FAIL last2=n=0`. NVDA, AMZN, GOOG, MSFT, and AVGO were all
deeply below their OR_low (NVDA 34/40 closes, AMZN 35/40, GOOG 32/40)
and the bar archive on disk had clean 1m data, but `/api/state.per_ticker_v510`
showed `boundary_hold.last_two_closes: []` for every single trade ticker.

### Root cause

`engine/scan.py` fed the boundary buffer with:

```python
if len(_closes_b) >= 2 and _closes_b[-2] is not None:
    eot_glue.record_1m_close(ticker, float(_closes_b[-2]))
```

Yahoo's intraday minute response keeps a forming bar at `closes[-2]`
whose value is `None` until the minute boundary fully passes; by then a
new forming bar has shifted everything down a slot. So `closes[-2] is None`
is the dominant case during RTH, the guard silently never fires, and
`record_1m_close` is essentially never called. The bug has been latent
since v5.10.1; it only became visible after the v5.20.2 EMA9 fix cleared
Phase 1 enough for the empty Phase 2 buffer to matter.

The bar-archive writer at `engine/scan.py:317-330` already handled the
same Yahoo quirk by walking back from `[-2]` to find a non-None close,
which is why disk had 40 bars per ticker even though the in-memory
tracker was empty.

### Fix

- New helper `record_latest_1m_close(ticker, closes)` in
  `v5_10_1_integration.py`: walks back up to 4 slots from `[-2]` for the
  newest non-None close, falls back to `[-1]` only when nothing earlier
  qualifies, and de-dups against the last value already in the buffer
  so successive scan cycles within the same minute do not register the
  same closed bar twice.
- `engine/scan.py:396-407` now calls the helper instead of the inline
  guarded block. One-line caller, identical behavior on the happy path,
  resilient to Yahoo's forming-bar `None`s.

### Validation

Within a few scan cycles after deploy, every active ticker's
`/api/state.per_ticker_v510.<TKR>.boundary_hold.last_two_closes` should
populate, and `[V5100-BOUNDARY]` log lines should start emitting real
`prior_close=` and `current_close=` values instead of `None`. NVDA at
200-201 vs OR_low 208.50 should flip the SHORT boundary to SATISFIED
after 2 consecutive 1m closes.

### Tests

12 new tests in `tests/test_v5_20_4_boundary_record_fallback.py` cover:
full-array happy path, `[-2]` None fallback to `[-3]`, `[-2]/[-3]` None
fallback to last-resort `[-1]`, all-None gives up, empty list, single
element, de-dup against last buffered value, walk-back capped at 4,
buffer trimmed to 4, return value semantics, and an end-to-end engine
harness simulating 5 successive Yahoo-shaped responses.

### Out of scope

- `V5100_PERMIT:shield_and_anchor` is still blocking Phase 1 short
  permits. Likely cascades from the same starved buffer; reassess after
  this lands.
- SPY missing from `/data/bars/<date>/` (subscribed but no archive
  file). Observability gap, not a trading-path bug.
- The Mon-Fri pipeline cron still queries the dropped `shadow_positions`
  table and errors on the row-count check; harmless but noisy.

---

## v5.20.3 \u2014 2026-04-30 \u2014 Permit Matrix expanded view: pipeline component card grid

### Why

The expanded permit-matrix row dumped the verbatim Tiger Sovereign v15.0
spec text inside every ticker (16 `<dt>/<dd>` pairs covering every gate,
alarm, and risk rule). That worked as a reference card during the v15.0
rollout but it buried the live state \u2014 operators had to mentally pair
the per-ticker indicator at the top of the matrix with the spec text
below to know what was actually happening.

v5.20.3 replaces the verbose `<dl>` with a responsive component card grid.
Each card is one pipeline component (Phase 1/2/3, an alarm, or the strike
counter) and surfaces:

- phase chip (P1 / P2 / P3 / AL / POS)
- component name and one-line short description
- live state: status badge (PASS / FAIL / COLD / PEND / OFF / SAFE / TRIP /
  IN POS / LOCKED / IDLE / USED) + numeric value

The verbatim spec lives in `tiger_sovereign-spec-v15-1.md`; the dashboard
is no longer a redundant copy of it.

### What changed

- `dashboard_static/app.js` \u2014 new `_pmtxComponentGrid(d)` helper that
  emits 8 cards: P1 Weather, P2 Boundary, P2 Volume, P3 Authority,
  P3 Momentum, AL Sov.Brake, AL Velocity Fuse, POS Strikes. The expanded
  detail panel calls it instead of inlining the v15.0 `<dl>`.
- `dashboard_static/app.css` \u2014 retired `.pmtx-spec-defs*` classes;
  added `.pmtx-comp-grid`, `.pmtx-comp-cards`, `.pmtx-comp-card`,
  `.pmtx-comp-chip`, `.pmtx-comp-name`, `.pmtx-comp-desc`,
  `.pmtx-comp-state`, `.pmtx-comp-badge`, `.pmtx-comp-val`, plus state
  tints `.pmtx-comp-{pass,fail,warn,pend,off,safe,trip,inpos,locked,used,idle}`.
  Grid auto-flows 4 cards/row at desktop, 3 at \u22641024px, 2 at \u2264720px,
  1 at \u2264480px.
- `bot_version.py` + `trade_genius.py` \u2014 BOT_VERSION 5.20.2 \u2192 5.20.3.
- `CURRENT_MAIN_NOTE` rewritten for the new release (\u226434 chars/line).
- `tests/test_dashboard_pmtx_expand_v5_20_3.py` \u2014 new string-level
  audits pinning the helper, the grid markup, the 8 component cards,
  and the absence of the retired `pmtx-spec-defs` block.

### Behavior parity

The verbatim v15.0 spec is no longer rendered inside the expanded row.
The spec source of truth (`tiger_sovereign-spec-v15-1.md`) is unchanged.
All prior live data (`pmtx-detail-grid` stats, `pmtx-sentinel-strip`,
and the per-row gate cells) renders identically. The expand/collapse
click handler (`body.__pmtxExpandWired`) and the `hasDetail` gate are
unchanged.

---

## v5.20.2 \u2014 2026-04-30 \u2014 QQQ regime EMA9 premarket reseed + continuous gap-fill

### Why

v5.20.1 fixed the DI premarket seed but the **QQQ regime EMA9** seeder
(`engine/seeders.py:qqq_regime_seed_once`) had the same shape of bug
in a different stream. On 2026-04-30 the dashboard returned
`section_i_permit.qqq_5m_ema9 = null` and the prod log showed
`[V572-REGIME-SEED] source=alpaca bars=4 ema3=665.97 ema9=None` at
13:29:38 UTC \u2014 60 seconds before market open. Result: the Phase 1
L-P1-G1 / S-P1-G1 permit gate was fail-closed for the first \u224825
minutes of RTH because EMA9 needs 9 closed 5m bars to seed and the
seeder only got 4 from Alpaca's premarket aggregation.

The seeder set `_QQQ_REGIME_SEEDED = True` after that single attempt
regardless of whether ema9 was actually computed, so subsequent
callers (`_v590_compass_for_gate`, `qqq_regime_tick`, `premarket_recalc`)
short-circuited and the half-warm state persisted until live ticks
organically accumulated 5 more bars.

The operator directive (`val@ 2026-04-30`): "Need do those premarket
and make sure that we always have them. If anything missed at any
point of time, recompute."

### What changed

#### `engine/seeders.py:qqq_regime_seed_once` \u2014 deferred seal + force_reseed

- **Idempotency seal deferred**: `_QQQ_REGIME_SEEDED = True` is set
  ONLY after `tg._QQQ_REGIME.ema9 is not None`. A partial seed (e.g.
  bars=4 from premarket) leaves the flag False so subsequent passes
  retry instead of trusting a half-warm state.
- **`force_reseed=True` argument**: bypasses the seal even when set,
  wipes regime state (`ema3`, `ema9`, seed buffers, `bars_seen`)
  before re-applying the fresh fetch. Used by the 09:31 recompute
  path and the live-tick gap-fill so a partial seed can be replaced
  by a later, larger one without blending the math.
- **Empty-fetch path no longer seals**: when no source returned bars
  the function now returns without writing the seal flag, letting
  later passes try again.
- **New constant**: `QQQ_REGIME_MIN_BARS_FOR_EMA9 = 9` (documents the
  threshold; matches `qqq_regime.EMA9_PERIOD`).
- **Log line gains `sealed=Y|N`** so the operator can see whether a
  given pass actually warmed EMA9.

#### `engine/seeders.py:qqq_regime_tick` \u2014 live-tick gap-fill

- After the existing `qqq_regime_seed_once()` call, if `ema9` is
  still None we now invoke `qqq_regime_seed_once(force_reseed=True)`
  before applying the fresh live close. The regime self-heals on
  every closed 5m bar until ema9 arms, even if every prior pass
  came up short.

#### `engine/seeders.py:recompute_qqq_regime_if_unwarm` \u2014 09:31 ET safety net

- New helper, mirror of `recompute_di_for_unseeded`. Short-circuits
  when ema9 is already non-None; otherwise calls
  `qqq_regime_seed_once(force_reseed=True)`. Logs
  `[QQQ-REGIME-RECOMPUTE-0931]` either way for cron forensics.
- Returns `{reseeded, already_warm, failed, ema9, bars_seen}`.

#### `trade_genius.py:qqq_regime_recompute_0931`

- New scheduler wrapper for the helper above. Wired into the existing
  `09:31 ET` job tuple alongside `di_recompute_0931`; both run on
  daemon threads so the system-test ping fires immediately on
  schedule and the two safety nets run in parallel.

#### `trade_genius.py:premarket_recalc` step 2 \u2014 ema9 gate

- The 09:29 ET recalc previously gated on `_QQQ_REGIME_SEEDED`,
  which became permanent after the first (possibly partial) attempt.
  It now gates on `_QQQ_REGIME.ema9 is None` and passes
  `force_reseed=True` whenever the regime is not yet warm. Result:
  the 09:29 pass is meaningful even when boot saw only a handful of
  premarket bars.

#### `trade_genius.py:_v590_compass_for_gate` \u2014 ema9 gate

- Gate flipped from `not _QQQ_REGIME_SEEDED` to `_QQQ_REGIME.ema9 is None`.
  Even if a stale partial seal exists from an old build, every
  permit-eval call retries the seed until ema9 arms.

### Tests

New file `tests/test_qqq_regime_premarket_v520_2.py`:

1. Empty fetch leaves `_QQQ_REGIME_SEEDED=False`.
2. Partial fetch (bars=4) leaves `_QQQ_REGIME_SEEDED=False` and
   ema9=None.
3. Full fetch (bars=12) seals and ema9 is non-None.
4. `force_reseed=True` wipes prior state and re-applies fresh closes.
5. `recompute_qqq_regime_if_unwarm` is a no-op when already warm and
   re-seeds when not warm.
6. `qqq_regime_tick` gap-fill: after a partial pre-seed, a fresh tick
   triggers force_reseed and warms ema9.
7. Scheduler `JOBS` table contains a `09:31` entry whose lambda
   reaches `qqq_regime_recompute_0931`.

### Behavior at 09:30 ET on next deploy

- Boot: seed pulls Alpaca premarket; if bars≥9, seal + log
  `sealed=Y`. If bars<9, seal=N, retry on next tick.
- 09:29 ET: `premarket_recalc` re-fetches premarket; same gate.
- 09:30 ET: live-tick path observes the first finalized 5m bucket,
  invokes `qqq_regime_seed_once`; if ema9 still None, force_reseed.
- 09:31 ET: `qqq_regime_recompute_0931` runs on a daemon thread; if
  ema9 still None, force_reseed picks up any premarket bars Alpaca
  newly aggregated since boot.
- Continuous: every closed 5m tick re-runs the gap-fill until ema9
  is non-None.

No change to permit semantics, no change to QQQ regime math.

---

## v5.20.1 \u2014 2026-04-30 \u2014 Premarket-only DI seed + 09:31 ET recompute

### Why

v5.20.0 inherited a DI 5m seeder that pulled prior-day 14:50\u219216:00 ET
bars (14 5m buckets) as a tail seed when premarket bars were
unavailable. This produced two distinct failure modes:

1. **`di_after_seed=None` for every ticker on a fresh boot** \u2014 the
   prior-day fetch yields 14 buckets but `tiger_di()` requires \u226516
   (`DI_PERIOD + 1`), so DI returned None until today's RTH bars
   accumulated to lift the merged buffer over the threshold.
2. **Stale momentum in the seed** \u2014 yesterday's late-session DI
   contributed to today's first calculation, biasing the very first
   entry decisions toward yesterday's regime.

The operator directive (`val@ 2026-04-30`): "The 15 bars in question
for DI should be collected from premarket. If missing, recompute at
the time of market entry \u2014 09:31 ET."

### What changed

#### `engine/seeders.py.seed_di_buffer` \u2014 premarket-only window

- **Fetch window**: 08:00\u219209:30 ET (last 90 minutes before the open).
  Yields up to 18\u00d7 5m buckets when premarket is liquid \u2014 3 bars of
  headroom above the `PREMARKET_DI_MIN_BARS = 15` threshold.
- **Removed**: prior-day 14:50\u219216:00 ET tail-seed code path. No
  cross-session contamination of DI.
- **Removed**: today's RTH fetch path inside the seeder. The seeder
  is now strictly a premarket primer; once 09:30 ET passes, today's
  5m RTH buckets arrive via the live tick feed and `tiger_di()` merges
  them with the seed at read time.
- **Conditional cache write**: `_DI_SEED_CACHE[ticker]` is only set
  when premarket yielded \u226515 bars. Insufficient runs leave the cache
  unset so a later recompute (or live RTH bars) can supersede cleanly
  without partial-buffer interference.
- **New return-key**: `sufficient: bool` \u2014 truthy iff \u226515 bars
  cached. The legacy keys `bars_today_rth` and `bars_prior_day` are
  removed (always 0/0 in the new design); `bars_premarket` is the
  only count that matters. `seed_di_all` summary line now reports
  `seeded_with_sufficient_premarket=N insufficient=N` instead of
  `seeded_with_nonnull_di=N skipped=N`.
- **New log line shape**:
  `DI_SEED ticker=X window_et=08:00-09:30 bars_premarket=N
  sufficient=Y/N di_after_seed=...`

#### `engine/seeders.py.recompute_di_for_unseeded` (new)

- Iterates the trade ticker list and re-runs `seed_di_buffer` ONLY for
  tickers whose `_DI_SEED_CACHE` entry is missing or has <15 bars.
- Idempotent for already-seeded tickers; non-fatal on per-ticker errors.
- Emits `[DI-RECOMPUTE-0931]` summary line with
  `recomputed=N already_seeded=N failed=N`.

#### `trade_genius.py.di_recompute_0931` (new)

- Wrapper called by the scheduler at 09:31 ET. Hands off to
  `recompute_di_for_unseeded(TRADE_TICKERS)` and swallows any orchestration
  exception with a logged traceback.

#### Scheduler JOBS table

- **09:31 ET row** now fires both the system-test ping (`_fire_system_test("8:31 CT")`)
  AND the DI recompute job. The system test fires synchronously; the
  recompute runs on a daemon thread so it cannot delay the test.
- The previous 09:31 single-handler row was unsafe to duplicate \u2014 the
  scheduler builds `job_key = fire_key + day + hhmm` so two rows with
  the same `(day, hhmm)` would deduplicate via the `fired_set` SQLite
  table. Combining into one lambda preserves the existing fired-set
  semantics.

#### Constants exposed by `engine.seeders`

- `PREMARKET_DI_WINDOW_START_HHMM = (8, 0)`
- `PREMARKET_DI_WINDOW_END_HHMM = (9, 30)`
- `PREMARKET_DI_MIN_BARS = 15` \u2014 must equal `trade_genius.DI_PERIOD`

### Operator-visible behavior

- Liquid premarket day: DI armed at 09:29 ET (premarket_recalc tick).
  First entry at 09:36 ET sees a fully-seeded buffer.
- Illiquid premarket day: DI seed insufficient. 09:31 ET recompute
  re-tries; usually still insufficient on its own. `tiger_di()` adds
  today's 5m RTH bars as they close, so DI typically arms by ~09:35\u201309:40 ET.
  Phase 3 entry path correctly fails closed on `5m DI > 25` when DI
  is None, so no bad trades fire during warmup.
- DI no longer contaminated by yesterday's late-session momentum.

### Files

- `engine/seeders.py` \u2014 rewrite of `seed_di_buffer`, new
  `recompute_di_for_unseeded`, new constants, updated `seed_di_all`
  summary message.
- `engine/__init__.py` \u2014 export `recompute_di_for_unseeded`.
- `trade_genius.py` \u2014 new `di_recompute_0931`; scheduler 09:31 row
  combined with system-test fire; import of new seeder symbol;
  `BOT_VERSION = "5.20.1"`; `CURRENT_MAIN_NOTE` updated.
- `bot_version.py` \u2014 `BOT_VERSION = "5.20.1"`.
- `tests/test_di_seed_premarket_only.py` \u2014 new file: premarket-window
  bucket math, conditional cache write, recompute idempotency, 09:31
  scheduler-row presence.

---

## v5.20.0 \u2014 2026-04-30 \u2014 Tiger Sovereign v15.0 spec conformance (engine + UI)

### Why

v15.0 of the Tiger Sovereign spec was issued as the finalized
production-ready specification, deprecating every prior version
(including the vAA-1 ULTIMATE doc that v5.19.x was tracking). A full
conformance audit (`/home/user/workspace/specs/v15_conformance_audit.md`,
14 findings, 9 critical) showed the live engine had drifted in nine
places relative to v15.0:

1. **Entry window** \u2014 v15.0 \u00a74: "09:36:00 to 15:44:59 EST".
   Engine had `09:35:00` as the hunt-window start.
2. **OR window end** \u2014 v15.0 \u00a70: "ORH / ORL: Fixed price
   levels established at exactly 09:35:59." Engine was freezing OR at
   09:34:59 (excluding the 09:35 candle).
3. **Strike-cap** \u2014 v15.0 \u00a71: "Maximum 3 Strikes per ticker
   per day." Engine still used a daily cap of 5.
4. **Strike 2/3 boundary target** \u2014 v15.0 \u00a71/\u00a72/\u00a73:
   strikes 2 & 3 must hunt the running NHOD/NLOD, not the original
   ORH/ORL. Engine was still using ORH/ORL for every strike.
5. **Phase-3 momentum gate** \u2014 v15.0 \u00a72/\u00a73: "5m ADX > 20
   AND Alarm E = FALSE." Engine had no 5m ADX>20 hard gate; the closest
   surface was an Alarm-D safety floor at ADX 25.
6. **Alarm E pre-entry filter on S2/S3** \u2014 v15.0 \u00a71.2:
   "Pre-Entry Filter: If a price prints a new extreme but RSI(15) is
   diverging \u2026, the bot is prohibited from opening new Strike 2 or
   Strike 3 positions." Engine had no pre-entry Alarm E check.
7. **Volume gate** \u2014 v15.0 \u00a72/\u00a73 makes the volume gate
   a primary Phase-2 permit (1m volume \u2265 100% of 55-bar avg,
   REQUIRED after 10:00 AM). Engine default for `VOLUME_GATE_ENABLED`
   was OFF; the spec-mandatory time-conditional path was also
   conditioned on a `now_et` argument that the live caller never passed,
   silently bypassing the gate.
8. **Divergence memory storage** \u2014 v15.0 \u00a70 glossary requires
   storing `(price, RSI)` at the exact tick of every new NHOD/NLOD;
   engine was conditionally storing only when RSI also confirmed,
   silently dropping the very ticks that produce the Alarm E signal.
9. **Alarm A flash-move threshold** \u2014 v15.0 \u00a7Addendum: "1m
   price move > 1% against position". Engine used `<=` (\u22651%);
   tightened to strict `<` so exactly 1% does not fire the alarm.

The dashboard surface had also drifted: matrix tooltips still cited
old rule IDs (`L-P2-S4`, `STRIKE-CAP-3`, etc.) and called the ADX
column "not a primary spec gate" when v15.0 makes ADX>20 the primary
momentum gate. Operators also asked for the Val/Gene panel pair to
adopt the same Open-positions-above-Weather order shipped on Main in
v5.19.4.

### What \u2014 engine

#### `engine/timing.py`
- `HUNT_START_ET` 09:35:00 \u2192 09:36:00 (v15.0 \u00a74).

#### `eye_of_tiger.py`
- `OR_WINDOW_END_HHMM_ET` 09:35 \u2192 09:36 with comment citing v15.0
  \u00a70: ORH/ORL frozen at 09:35:59, so OR is the half-open minute
  range `[09:30, 09:36)` and the 09:35 candle is INCLUDED.

#### `trade_genius.py`
- `_fill_metrics_for_ticker.or_window_end` 09:35 \u2192 09:36 (matches
  the `OR_WINDOW_END_HHMM_ET` change in `eye_of_tiger.py`).
- `collect_or` window upper bound 09:35 \u2192 09:36 with comment
  documenting the v15.0 alignment.

#### `broker/orders.py.check_entry`
- Entry-window guard: `time(9, 35)` \u2192 `time(9, 36)`.
- EOD short-circuit: `time(15, 55)` \u2192 `time(15, 44, 59)` (matches
  v15.0 \u00a74: no new entries after 15:44:59).
- Wired the unified `tg.strike_entry_allowed(ticker, side, view)`
  helper through a `_flat_gate_view` projection of `tg.positions` and
  `tg.short_positions` into `f"{ticker}:{SIDE}"` keys, so STRIKE-CAP-3
  + STRIKE-FLAT-GATE are enforced verbatim.
- Daily count cap 5 \u2192 3 (v15.0 \u00a71).
- Strike-aware boundary: strikes 2 & 3 hunt
  `tg._v570_session_hod[ticker]` (long) /
  `tg._v570_session_lod[ticker]` (short) instead of OR levels.
- 5m ADX>20 hard gate via `tg.v5_adx_1m_5m(ticker)["adx_5m"]`. Fails
  closed (no entry) if the indicator can't be computed.
- Pre-entry Alarm E filter for strikes 2 & 3 via
  `engine.sentinel.check_alarm_e_pre` reading
  `broker.positions.get_divergence_memory()`.
- `now_et=ZoneInfo("America/New_York")` plumbed through to
  `tg.eot.evaluate_volume_bucket(\u2026)` so the spec-mandatory
  time-conditional path activates (auto-pass before 10:00 ET; require
  bucket pass after).

#### `volume_bucket.py`
- `VolumeBucketBaseline.check` now also returns `ratio_to_55bar_avg`
  (alias of the existing `ratio` field) so the v15.0 spec name resolves
  in downstream callers.

#### `engine/feature_flags.py`
- `VOLUME_GATE_ENABLED` default: `False` \u2192 `True` (v15.0 \u00a72
  / \u00a73 makes the gate a primary Phase-2 permit). Operator override
  via env var still supported.

#### `engine/sentinel.py.check_alarm_a`
- Velocity threshold tightened from `<=` to strict `<` so a price move
  of exactly 1% against position does not trigger the flash-move alarm
  (v15.0 \u00a7Addendum: "> 1%").

#### `engine/momentum_state.py.DivergenceMemory.update`
- Storage is now unconditional on the RSI relationship: every new
  price extreme overwrites the prior `(price, rsi)` peak. The
  divergence test (`is_diverging`) compares current RSI vs stored RSI
  but does NOT influence storage. Pre-v5.20.0 the LONG path required
  `rsi >= stored_rsi` to store, which silently dropped the very NHOD
  ticks v15.0 wants captured (the ones that subsequently form the
  Alarm E divergence signal).

#### `broker/orders.py.execute_breakout` \u2014 sizing wire-in
- `eye_of_tiger.evaluate_strike_sizing` is now invoked on every
  Strike-1 fill in the live entry path. The helper has existed since
  v5.15.0 but was previously only exercised by unit tests; the live
  path always fired the legacy 50% Entry-1 starter regardless of the
  spec-tier 1m DI value. The wire-in maps:
  * **FULL** (1m DI\u00b1 > 30) \u2192 fills 100% in a single fill
    (2 \u00d7 starter shares) and pre-sets `v5104_entry2_fired=True`
    on the position dict so the legacy Entry-2 add-on does NOT
    double-fill.
  * **SCALED_A** (1m DI\u00b1 in [25, 30]) \u2192 fills 50% starter
    (existing behavior); leaves `v5104_entry2_fired=False` so
    Entry-2 may add the remaining 50% under the spec scale-in
    conditions (`_v5104_maybe_fire_entry_2`).
  * **WAIT** \u2192 defensive abort (the L-P3-AUTH master-anchor
    gate in `check_breakout` should already have caught this; this
    is a backstop).
  Each fire emits `[V15-SIZING] <ticker> side=<...> tier=<...>
  shares=<N> (1m DI=<...>, 5m DI=<...>)`. The position dict is
  stamped with `v15_size_label` and `v15_size_reason` for forensic
  capture in `[TRADE_CLOSED]`. Wrapped in try/except: any helper
  exception falls back to the legacy 50% starter so a sizing-helper
  bug never blocks a trade. New conformance tests in
  `tests/test_spec_v15_conformance.py` (9 tests) pin the contract
  the wire-in depends on (FULL doubles starter, SCALED_A equals
  starter, boundary cases at 25.0 and 30.0, anchor-fail and missing
  DI both yield WAIT, and the `v5104_entry2_fired` flag mapping).

### What \u2014 UI

#### `dashboard_static/app.js`
- `execSkeleton(...)` (Val + Gene panel render path): the Open
  positions `<section class="grid">` is now emitted BEFORE the
  `pmtx-weather-section` Weather Check banner, mirroring the swap
  shipped on Main in v5.19.4.
- `renderPermitMatrix(...)` column tooltips rewritten to v15.0 wording.
  Rule-ID references (`L-P2-S4`, `S-P2-S4`, `L-P3-AUTH`, `L-P2-S3`,
  `STRIKE-CAP-3`, `STRIKE-FLAT-GATE`) are dropped; ADX is now described
  as the primary momentum gate ("5m ADX > 20 AND Alarm E = FALSE")
  rather than "not a primary spec gate".
- `_pmtxBuildRow(...)` detail panel: each gate row now carries the
  full v15.0 spec definition (Weather, Permit, Volume Gate, Authority,
  Momentum, Sizing, Strike Sequence, Hard Stop, Circuit Breaker,
  Alarms A\u2013E, Entry Window, EOD Flush) so the operator can
  cross-check the live verdict against the verbatim rule.

### What \u2014 spec & docs

- `STRATEGY.md` rewritten as the v15.0-aligned in-repo strategy doc.
  vAA-1 morphing notes deleted; the new doc is the single source of
  truth and links to `/home/user/workspace/tiger-sovereign-spec-v15-1.md`.

### What \u2014 tests

Nine pre-existing tests asserted pre-v15.0 behavior and have been
rewritten to pin the new contract:

- `tests/test_timing_rules.py::test_hunt_window` \u2014 09:35 cases
  flipped to 09:36; the 09:35:30 case is now "before window".
- `tests/test_timing_rules.py::test_hunt_end_aligns_with_cutoff`
  \u2014 start time 09:35 \u2192 09:36.
- `tests/test_eye_of_tiger.py::test_boundary_hold_earliest_satisfaction_time_is_0936`
  \u2014 expected satisfaction time 09:36 \u2192 09:37 (with OR end at
  09:35:59 and `BOUNDARY_HOLD_REQUIRED_CLOSES = 2`, the second
  qualifying close is the 09:37 candle).
- `tests/test_momentum_state.py::test_divergence_memory_long_update_only_when_both_conditions_met`
  and `..._short_mirrors_long` \u2014 rewritten to assert
  unconditional-on-RSI storage.
- `tests/test_phase2_gates.py::test_volume_gate_default_module_constant_is_true`
  (renamed from `_is_false`) \u2014 default ON pin.
- `tests/test_startup_smoke.py::test_volume_gate_enabled_default_on_when_env_unset`
  (renamed from `_off_`) \u2014 default ON pin via env-deletion path.
- `tests/test_startup_smoke.py::test_scan_loop_no_blocking_at_first_call_with_empty_state`
  \u2014 `BOT_VERSION` prefix `5.19.` \u2192 `5.20.`.
- `tests/test_dashboard_state_v5_13_2.py::test_build_tiger_sovereign_snapshot_volume_gate_off_flag`
  \u2014 now sets `VOLUME_GATE_ENABLED=0` explicitly to exercise the
  operator-override path.
- `tests/test_tiger_sovereign_spec.py::test_SHARED_HUNT` \u2014
  `HUNT_START_ET` 09:35 \u2192 09:36.

All three reload-the-feature-flags helpers also drop the cached
attribute on the `engine` parent package, otherwise
`from engine import feature_flags` resolves through the still-bound
parent attribute and returns the previously-loaded module.

### What \u2014 versioning

- `bot_version.py.BOT_VERSION` and `trade_genius.py.BOT_VERSION`
  bumped to `5.20.0`.
- `CURRENT_MAIN_NOTE` rewritten for v5.20.0 (10 lines, all
  \u2264 34 chars wide).

---

## v5.19.4 \u2014 2026-04-30 \u2014 Sticky expand, panel reorder, spec-cited matrix headers

### Why

Three follow-up issues from operator review of v5.19.3:

1. **Permit Matrix rows expanded on click and then "immediately collapsed."** v5.19.3 widened the `hasDetail` gate so the detail row gets emitted, and the click handler does flip `pmtx-row-expanded` / `pmtx-detail-open`. The regression is upstream of the toggle: every `/api/state` SSE push (every 1\u20132s) calls `body.innerHTML = \u2026`, wiping the live class. The user perceives a click that flashes open and snaps shut. Sticky state must live OUTSIDE the rendered DOM so the next render can re-apply it.

2. **Open positions sat below the Weather Check banner.** Operator request: currently-held risk should be the first thing visible on Main; the conditional "can I take a new entry?" verdict comes second.

3. **Permit Matrix column headers and tooltips drifted from the Tiger Sovereign vAA-1 spec.** The `ADX>20` column was labeled like a primary spec gate even though the spec only uses ADX in Sentinel alarms (SENT-C, SENT-D); `5m ORB` glossed over the exact "two consecutive 1m closes strictly above/below" rule and the 09:35:59 ET freeze; tooltips never cited rule IDs (`L-P2-S4`, `L-P3-AUTH`, `STRIKE-CAP-3`, etc.) so the dashboard was orphaned from the spec.

### What

#### `dashboard_static/app.js` \u2014 Bug #1 (sticky expand + outside-click)

- `body.__pmtxExpandedSet` (Set, lives across renders): the source of truth for "which ticker(s) are expanded".
- `_pmtxApplyExpanded()` reads the Set and toggles `pmtx-row-expanded` on every `tr.pmtx-row` and `pmtx-detail-open` on every `tr.pmtx-detail-row`. Called at the end of `renderPermitMatrix(\u2026)` after the `innerHTML` write, so each /api/state push restores the operator's choice.
- Click handler: clears the Set, then re-adds the clicked ticker iff it wasn't already expanded. **Single-open semantics:** clicking a different row replaces the prior expansion; re-clicking the same row collapses it.
- Document-level click listener: when the click target is outside the matrix body, clears the Set and re-applies. The matrix's own click handler still wins for in-matrix clicks because it short-circuits via `body.contains(ev.target)`.

#### `dashboard_static/index.html` \u2014 Bug #2 (panel reorder)

- `<section class="grid">` containing the Open positions card is now BEFORE the `<section class="pmtx-weather-section">` Weather Check banner. Both sections kept their full inner markup; only the order swapped.
- Comments updated on both sections to record the swap and why.

#### `dashboard_static/app.js` \u2014 Bug #3 (spec-cited headers)

- `5m ORB` \u2192 `ORB`, tooltip rewritten: "L-P2-S4 / S-P2-S4 \u2014 ORH/ORL Boundary. Two consecutive 1m candles must close strictly above the 5m ORH (long) or strictly below the 5m ORL (short). ORH/ORL frozen at 09:35:59 ET on the 5m bar that closes at 09:35."
- `ADX>20` \u2192 `Trend`, tooltip rewritten: "Trend strength proxy (not a primary spec gate). Lights up once the Phase 3 master anchor fires (5m DI\u00b1 > 25), which empirically requires 5m ADX > 20." Honest about what the column actually measures.
- `DI\u00b1 5m>25` \u2192 `5m DI\u00b1`, tooltip rewritten: "L-P3-AUTH / S-P3-AUTH \u2014 Phase 3 master anchor. 5m DI+ > 25 (long) or 5m DI\u2212 > 25 (short). If FALSE \u2192 no entry, regardless of 1m DI."
- `Vol` tooltip rewritten: "L-P2-S3 / S-P2-S3 \u2014 Volume gate. Auto-passes before 10:00 ET. After 10:00 ET, requires 1m volume \u2265 1.00\u00d7 rolling 55-bar same-minute average."
- `Strikes` tooltip rewritten: "STRIKE-CAP-3 \u2014 maximum 3 Strikes per ticker per session. STRIKE-FLAT-GATE: next strike requires position fully flat. Counters reset at 09:30:00 ET."
- `State` tooltip rewritten: "Per-ticker FSM \u2014 IDLE \u00b7 ARMED (P1+P2 satisfied, awaiting P3) \u00b7 IN POS \u00b7 LOCKED (3-of-3 used)."
- `Dist` tooltip references L-P2-S4 / S-P2-S4.

### Tests

- `tests/test_dashboard_pmtx_sticky_expand_v5_19_4.py` \u2014 string-level audit confirming `__pmtxExpandedSet`, `_pmtxApplyExpanded`, single-open semantics, and outside-click handler are all in `app.js`.
- `tests/test_dashboard_panel_order_v5_19_4.py` \u2014 reads `index.html` and asserts the Open positions section appears before the Weather Check section.
- `tests/test_dashboard_pmtx_spec_headers_v5_19_4.py` \u2014 string-level audit confirming the new headers (`ORB`, `Trend`, `5m DI\u00b1`) and rule IDs (`L-P2-S4`, `L-P3-AUTH`, `STRIKE-CAP-3`, `L-P2-S3`) are in tooltips.

Manual verify: Playwright harness with proximity-only mock state \u2014 click expand, simulate three /api/state re-renders, row stays open. Click a different row, prior collapses, new opens. Click outside, all collapse.

### Deploy

Standard squash + delete-branch. No env / volume changes; dashboard JS + HTML only. Railway picks up the new image automatically.

---

## v5.19.3 \u2014 2026-04-30 \u2014 Dashboard fixes: row expand, tab + login persistence

### Why

Three usability bugs reported against v5.19.2:

1. **Permit Matrix rows weren't expanding on click** anymore. The
   click handler is fine \u2014 the regression is upstream: `hasDetail`
   was `pos || lastFill`, and pre-market sessions have neither, so no
   `.pmtx-detail-row` was ever emitted. Bug shipped in v5.18.0 when
   the standalone Proximity card folded into the matrix detail panel
   without `hasDetail` being widened to include proximity payload. It
   only surfaced now because Val tested during pre-market.
2. **Active tab snapped back to Main on every page reload / redeploy**
   even when the user had Val or Gene open. The active-tab state
   lived only in a body data-attribute set in-memory; a fresh fetch
   wiped it.
3. **Login session expired sooner than necessary**. The signing key
   has been persistent (`/data/dashboard_secret.key`) since v3.4.29,
   so the cookie does survive across redeploys, but `SESSION_DAYS=7`
   meant Val had to re-enter the password every week regardless.

### What

- **Row expand fix** (`dashboard_static/app.js`, `_pmtxBuildRow`):
  `hasDetail` is now `!!(pos || lastFill || proxHasDetail)`, where
  `proxHasDetail` is true when proximity carries any of `price`,
  `nearest_label`, `or_high`, `or_low`. Pre-market and quiet RTH
  sessions can now expand every row that has live price + boundary
  info, which is what the detail panel was already designed to show.
  The toggle handler at `body.__pmtxExpandWired` was already correct
  (it is delegate-bound on the parent body, which survives innerHTML
  swaps; `classList.toggle` already implements click-to-collapse on
  the second click) \u2014 no change there.
- **Tab persistence** (`dashboard_static/app.js`, `selectTab` + boot):
  every `selectTab(name)` call writes the chosen tab to
  `localStorage["tg-active-tab"]`. After click handlers wire up on
  page load, a small bootstrap reads that value and re-invokes
  `selectTab` if it's not Main, so panel visibility, tab chrome
  highlight, and per-tab activation hooks (executor poll, lifecycle
  activate) all run through the same code path. localStorage failures
  (private browsing, disabled storage) fall through to Main without
  raising.
- **Login lifetime** (`dashboard_server.py`): `SESSION_DAYS = 7` \u2192
  `SESSION_DAYS = 90`. Cookie behavior, signing, and CSRF guards are
  unchanged \u2014 just a longer expiry window. The stale `_make_token`
  docstring ("process-local random bytes, so restarts invalidate all
  sessions") was rewritten to reflect the persistent-key reality
  introduced in v3.4.29.

### Tests

- `tests/test_dashboard_pmtx_expand_v5_19_3.py` \u2014 calls
  `_pmtxBuildRow` (via the inlined dashboard preview harness) with
  the four input shapes and asserts the table-rows contain a
  `pmtx-detail-row` exactly when proximity, position, or fill is
  present.
- `tests/test_dashboard_session_days_v5_19_3.py` \u2014 imports
  `dashboard_server` and pins `SESSION_DAYS == 90` so the value can
  only change with a deliberate test update.
- `tests/test_dashboard_tab_persistence_v5_19_3.py` \u2014 string-level
  audit of `dashboard_static/app.js` confirming the localStorage save
  call inside `selectTab` and the boot-time read.

Full preflight (6/6 stages) green; smoke and version-consistency
guards still pass.

### Deploy

No migration. New cookies issued after deploy will have `Max-Age` of
90 days; existing 7-day cookies remain valid until their original
expiry. To force-rotate the signing key (and invalidate every active
session), set `DASHBOARD_SESSION_SECRET` on Railway to a new 64-hex
value and redeploy.

---

## v5.19.2 \u2014 2026-04-30 \u2014 Permit Matrix mobile parity + condensed headers

### Why

On phones ≤720px CSS, the Permit Matrix was hiding three of its most
informative columns (ADX>20, DI+ 5m>25, Vol confirm). To see those
gates the operator had to expand each row — a friction the desktop
view doesn't have. The DI+ header was also misleading on SHORT
permits (the underlying snapshot is DI− for short side, not DI+).

### What

- **Mobile parity** (`dashboard_static/app.css`): the ≤720px
  `display:none` rule on `.pmtx-col-adx / -diplus / -vol` is removed.
  All 9 columns render on every viewport. The `.pmtx-table-wrap`
  already has `overflow-x: auto` and `min-width: 760px`, so phones
  narrower than ~760px CSS now horizontal-scroll the row instead of
  hiding columns.
- **Header rename** (`dashboard_static/app.js`):
  - `Vol confirm` → `Vol` (compact)
  - `Price · Distance` → `Dist` (compact)
  - `DI+ 5m>25` → `DI± 5m>25` (acknowledges both sides; the
    `entry1_di` snapshot from `v5_13_2_snapshot._phase3_row` is
    already side-correct — DI+ for LONG, DI− for SHORT)
- **Side-aware tooltip** (`_pmtxDiTooltip`): when only LONG permits
  are open the tooltip reads `DI+ on 5m bars above 25 — last reading
  X.X`; only SHORT → `DI−`; both / neither → `DI±`.
- **Boundary abbreviation** (`_pmtxAbbrevBoundary`): `OR-high` →
  `ORH`, `OR-low` → `ORL` rendered in the Dist cell and the detail
  row's "Nearest boundary" stat. Hover tooltip preserves the full
  unambiguous name. Server-side `nearest_label` contract is
  unchanged (still emits `OR-high` / `OR-low`, pinned by
  `tests/test_dashboard_state_v5_13_2.py:285`).

### Tests

No behavioral changes to the Python surface. Existing
`test_proximity_rows_drops_pdc_and_carries_permit_side` still
passes (server contract unchanged). Front-end is render-only;
spot-checked at 360 / 390 / 720 / 1024 / 1280px.

### Risk / mitigation

None for the live trading path. Pure dashboard render. Worst case
the horizontal scroll feels unfamiliar on a 360px phone; the
operator can pinch-zoom or rotate to landscape.

---

## v5.19.1 \u2014 2026-04-30 \u2014 STRIKE-CAP-3 unified to per-ticker (vAA-1 ULTIMATE Decision 1)

### Why

Under v5.15.0+, `STRIKE-CAP-3` capped strikes at 3 per **(ticker, side)**
per day, allowing up to 6 entries on the same ticker (3 long + 3 short).
The vAA-1 ULTIMATE spec (Decision 1) tightens this to 3 strikes per
**ticker** per day, long+short combined. This caps daily exposure on a
single name and prevents whipsaw entries from burning through 6 strikes
in a chop.

### What

- `_v570_strike_counts` key changed from `(ticker, side)` to `ticker`.
  Long and short entries on the same ticker now share one counter.
- `_v570_strike_count(ticker, side)` and `_v570_record_entry(ticker, side)`
  keep their signatures for call-site compatibility but ignore `side` in
  the dict lookup. Hot-path callers (`broker/orders.py:520`) require no
  changes.
- `strike_entry_allowed(ticker, side, positions)` now consults the
  per-ticker counter. The fourth combined entry on a ticker is blocked.
- `_v570_strike_must_be_flat` (STRIKE-FLAT-GATE) stays **per-side**.
  You can be flat long while holding short — the flat gate is about
  not stacking into an open same-side strike, not about cross-side
  exclusivity.

### Tests

- `tests/test_strike_cap_unified.py` (NEW) — long-2 + short-1 fills the
  cap; the fourth attempt (any side) is blocked.
- `tests/test_tiger_sovereign_vAA_spec.py::test_strike_cap_3_blocks_fourth_entry`
  updated to assert the per-ticker semantics.
- `smoke_test.py` D3/D4 fixtures (lines 4546, 4671) updated for the
  unified counter — the legacy "independent SHORT counter" assertion is
  removed, and the 25-strikes-on-NVDA fixture is rewritten as a 3-strike
  cap exhaustion test.

### Risk / mitigation

Low. Rolling state is in-memory only (`_v570_strike_counts: dict`); the
first session boundary after deploy resets it cleanly. No persisted
state migration is required because the counter is regenerated each day
at 09:30 ET via `_v570_reset_if_new_session()`.

### Closes

- #251

---

## v5.19.0 \u2014 2026-04-30 \u2014 Premarket data recalc at 09:29 ET (vAA-1 ULTIMATE Decision 6)

### Why

All four premarket seed paths (DI seed, QQQ Regime EMAs, volume profile
cache, prior-day bar archive) run **once at process startup**. On long-
running containers (Railway keeps the bot alive overnight via
`health_ping`), seeded caches reflect yesterday's startup state —
premarket data accumulated overnight is never re-seeded into them. The
first few entries of the day were operating against stale caches.

### What

New scheduler entry fires weekdays at **09:29 ET** (1 minute before
market open). The orchestrator (`premarket_recalc()` in
`trade_genius.py`) runs four steps, all non-fatal:

1. **Prior-day bar archive existence check** — logs warning if
   `/data/bars/<yesterday>/` is missing files. (Backfill itself is
   scoped to a future PR; this surfaces gaps.)
2. **`qqq_regime_seed_once()`** — already idempotent via
   `_QQQ_REGIME_SEEDED` flag; no-op if seeded.
3. **DI seed (per-ticker idempotency)** — wrapped with `if t in
   _DI_SEED_CACHE: continue` at the orchestrator level, honoring user
   direction "only seed if not yet seeded".
4. **Volume profile cache reload** — always reloads from disk per
   ticker; this is the one exception to idempotency, because reloading
   reflects the previous night's 21:00 ET nightly rebuild output.

### Operational

Look for `[PREMARKET-RECALC] complete in Xs — di_seeded=N qqq_seeded=Y
volprof_reloaded=N archive_warnings=N archive_filled=N` in logs at
09:29 ET each weekday. A fully-warm cache produces a no-op (no Alpaca
calls fire); a cold container populates everything.

New rule `SHARED-PREMARKET-RECALC` in STRATEGY.md SECTION 4.

Closing GitHub issue: #252.

---

## v5.18.1 — 2026-04-29 — Mobile responsive matrix + Val/Gene Permit Matrix + release-note fix

Three-issue follow-up patch on top of v5.18.0:

### 1. Mobile Permit Matrix — drop the cards-stack, render the same table

v5.18.0 swapped to a per-Titan card stack under 720px so the gates could
be shown as label-per-chip rows. Operator feedback: the per-gate label
in every card is clutter and visually dissimilar from the desktop
layout. v5.18.1 makes the same `<table class="pmtx-table">` responsive
instead — the ADX, DI+ 5m, and Vol-confirm columns hide on ≤720px
(their values are inside the click-to-expand detail row anyway), and
the remaining six columns (Titan / 5m ORB / Strikes / State / Price ·
Distance / chevron) tighten font-size + padding to fit a 390px iPhone
viewport. Under 400px we trim further (5m ORB → 32px, prox-bar → 28px)
so 360px Android viewports still read clean. The `.pmtx-cards` CSS
class remains as a `display:none` stub for deploy-race safety against
clients holding stale JS that still emits the cards markup.

### 2. Val / Gene tabs — replace Proximity card with Weather Check + Permit Matrix

The v5.17/v5.18 Val and Gene tabs still showed the legacy standalone
Proximity card (price + bar + nearest-OR strip per ticker) under their
portfolio mirror. Operator feedback: the gates these executors are
actually subject to are exactly the gates Main shows in the Permit
Matrix, so the Proximity card is stale info and a divergent UI. v5.18.1
replaces the per-executor Proximity card with the same Weather Check
banner + Permit Matrix card rendered on Main. Data is market-wide
(reads `window.__tgLastState`, the most-recent `/api/state` payload
republished by the Main IIFE), so Val/Gene see the exact same Phase 1
verdict and per-Titan gate rows as Main. `renderWeatherCheck` and
`renderPermitMatrix` now accept an optional `panel` second arg that
switches their DOM lookups from `getElementById` to a `data-f="..."`
attribute query inside the panel root, so a single renderer feeds all
three tabs. The dead `renderExecProximity` and
`execRenderPermitSideChip` helpers are removed.

### 3. Telegram release notes — fix `CURRENT_MAIN_NOTE` drift + add preflight guard

v5.18.0 (and v5.17.0 before it) shipped with `trade_genius.CURRENT_MAIN_NOTE`
still stuck at the v5.16.0 "Legacy purge" body, so every `/version`
response and every `🚀 vX.Y.Z deployed` Telegram banner trailed a stale
release description. Root cause: `smoke_test.py` already enforces
`CURRENT_MAIN_NOTE` first-line == `f"v{BOT_VERSION}"`, but `smoke_test.py`
lives at the repo root (not under `tests/`) and the filename does not
match `test_*.py`, so `pytest tests/ test_*.py` (the preflight invocation)
never picks it up. v5.18.1 (a) updates `CURRENT_MAIN_NOTE` to the v5.18.1
body, and (b) adds an explicit shell assertion to `scripts/preflight.sh`
step [3/6] that imports `trade_genius` and `bot_version` and checks that
`CURRENT_MAIN_NOTE.split("\n")[0]` starts with `f"v{BOT_VERSION}"`. So
the release-note miss is now a preflight failure rather than a
production post-mortem.

### Files touched

- `dashboard_static/app.js` — `renderWeatherCheck`/`renderPermitMatrix`
  gain optional `panel` arg via new `_pmtxEl` lookup helper; `execSkeleton`
  swaps Proximity card for Weather Check + Permit Matrix sections;
  `renderExecMarketState` now calls `renderWeatherCheck` +
  `renderPermitMatrix` with the panel root; `renderExecProximity` and
  `execRenderPermitSideChip` removed.
- `dashboard_static/app.css` — already swapped to responsive-table
  block in v5.18.1 mobile fix (no further change here).
- `trade_genius.py` — `BOT_VERSION` 5.18.0 → 5.18.1; `CURRENT_MAIN_NOTE`
  rewritten to the v5.18.1 body.
- `bot_version.py` — `BOT_VERSION` 5.18.0 → 5.18.1.
- `scripts/preflight.sh` — step [3/6] grows a release-note guard.
- `tests/test_startup_smoke.py` — already pinned to `5.18.` prefix; no
  change required.

---

## v5.18.0 — 2026-04-29 — Permit Matrix row collapse + Proximity merge

Dashboard density refactor: the v5.17.0 Permit Matrix shipped with ~70–90px
tall rows because each row carried a multi-line Titan cell (name + side meta)
plus a permanently-rendered sentinel-strip subrow under every IN-POS Titan.
For a 4-Titan layout that meant ~280–360px of vertical real estate just for
the matrix on Main, which pushed the Open positions grid below the fold on
13" laptop viewports. v5.18.0 collapses each Titan to a single ~38px row
matching the original Tiger Sovereign mockup
(`titan_permits_mockup/permit_matrix_only.html`), and folds the standalone
Proximity card into a new Price · Distance column inside the matrix.

No engine changes — pure presentation refactor. `/api/state` shape is
unchanged (still emits `proximity[]`, `tiger_sovereign.phase4[].sentinel`,
etc.); only the Main-tab dashboard JS/CSS/HTML changes.

### Main tab — what's gone

- **Proximity card** (the dedicated 4-row price + bar + %·label panel that
  sat under the KPI strip in v5.17). Its data is now rendered inline as a
  single Price · Distance cell per Titan inside the Permit Matrix. The
  underlying `_proximity_rows()` payload is still emitted by
  `dashboard_server.py` and is still consumed by the Val/Gene exec tabs
  via `renderExecProximity` (those tabs do not have a Permit Matrix and
  rely on the standalone strip).
- **Permanent sentinel sub-row** under every IN-POS Titan. Sentinel A/B/C
  detail is now hidden by default and revealed by clicking the row
  (chevron rotates 90°), matching the mockup's `tr.detail` pattern.
- **Two-line Titan cell** with "long side / awaiting permit" meta text
  under the Titan name. The same information is conveyed by the row tint
  (green/red bar on permit-go/permit-block) plus the existing State pill.
- **DI+ 1m column** — was always pending (`null`) since v5.16+ and not
  surfaced by `/api/state`. Removed entirely from the table; the
  per-Titan column count is unchanged because the new Price · Distance
  column replaces it.

### Main tab — what's new

- **Price · Distance column** in Permit Matrix. Each Titan row shows
  live last price + a thin proximity bar + `% · OR-high|OR-low` label.
  Bar fills as price approaches the entry-relevant boundary; tints amber
  when within 0.5%. Tooltip carries the verbose `Last NNN.NN — N.NNN%
  from OR-high` form for hover inspection.
- **Click-to-expand sentinel detail** for IN-POS Titans. A new chevron
  in the rightmost column toggles a `tr.pmtx-detail-row` carrying the
  existing 5-cell sentinel strip (A1 P&L, A2 Velocity, B Velocity Fuse,
  C Ratchet, D ADX Collapse, E Divergence Trap) plus a stat grid (Last
  trade timestamp, Last trade price, Nearest boundary, OR range). Static
  Titans (no open position, no trade today) show no chevron.
- **Row tint** for at-a-glance permit verdict: green left-bar for Titans
  whose long-side gates are GO with `long_permit` open, red for short.
  Replaces the deleted Titan-meta line.
- **Open positions panel** is now full-width single-column on Main since
  it no longer shares its row with the retired Proximity card.

### Mobile (≤720px)

The `.pmtx-cards` mobile card stack is unchanged structurally — each card
still folds the per-Titan gate stages into a vertical layout. The new
Price · Distance cell is appended to each card's stat grid alongside
Strikes, so the mobile view also surfaces proximity inline.

### Files touched

- `dashboard_static/index.html` — removed Proximity `<section>`; Open
  positions section dropped its grid wrapper.
- `dashboard_static/app.js` — deleted `renderProximity` and
  `renderPermitSideChip` (IIFE-1, ~lines 314–355 in v5.17); rewrote
  `renderPermitMatrix` and `_pmtxBuildRow` with the new column layout
  and click-to-expand machinery; added `_pmtxProxCell` helper. Delegated
  click handler is wired idempotently via `body.__pmtxExpandWired`.
- `dashboard_static/app.css` — tightened `.pmtx-table` cell padding from
  the v5.17 default to 8×8px to match mockup; added `.pmtx-row-permit-go`
  / `.pmtx-row-permit-block` row tints; added `.pmtx-expand-chev`
  rotate-on-open animation; added `.pmtx-detail-row` collapsed-by-default
  styles; added `.pmtx-prox` cell layout (price + bar + pct grid with
  warn tint <0.5%); added column-width helpers for prox/strike/expand.

### Compatibility

No backend changes. No CI changes. `/api/state` consumers (Telegram
bots, alarm sentinels, downstream replays) all see identical payloads.

---

## v5.17.0 — 2026-04-29 — Dashboard redesign: Permit Matrix + Weather Check

Full dashboard redesign for Tiger Sovereign vAA-1. The legacy v5.13.2 Phase
1–4 vertical panel, Observer panel, Gates·entry-checks panel, and Volume
Gate flag pill are all retired; their content is now folded into a new
**Weather Check** banner (single-line market-wide permit verdict) plus a
**Permit Matrix** card (per-Titan row × gate-stage table on desktop, per-
Titan card stack on mobile, with an inline A/B/C/D/E sentinel detail strip
that unfolds for Titans currently in a position).

No engine changes — purely a presentation refactor. The `/api/state` shape
is unchanged from v5.16.0; the new dashboard reads `tiger_sovereign.phase1`
(Weather Check verdict), `phase2` + `phase3` + `tickers` (matrix rows), and
`phase4[].sentinel` + `phase4[].titan_grip` (sentinel detail strip).

### Main tab — what's gone

- **GATE + REGIME KPI tiles** — collapsed into the Weather Check banner.
  KPI row trimmed from 6 tiles to 4 (Equity / Day P&L / Open / Session).
- **Volume Gate flag pill** — folded into the per-Titan VOL CONFIRM column
  in the Permit Matrix.
- **Tiger Sovereign Phase 1–4 panel** — replaced by the matrix rows.
  Per-Titan state (5M ORB, ADX>20, DI+ 5M, DI+ 1M, VOL CONFIRM, STRIKE CAP,
  STATE, LAST TRADE) is now in a single readable table.
- **Observer panel** — never carried operational signal; deleted.
- **Gates · entry-checks panel** — duplicated info now in the matrix.

### Val/Gene tabs — portfolio-only redesign

These tabs are scoped to per-executor portfolio state, so the market-wide
Permit Matrix and Weather Check live only on Main. The exec tabs lose:

- **Sovereign Regime Shield card** — was a relic of the v5.9.1 dual-index
  PDC eject rule (decommissioned; only chip skeletons remained).
- **Gates · entry-checks card** — duplicate of the Main matrix.
- **Gate + Regime KPI tiles** — same redundancy logic.

KPI row matches Main's (Equity / Day P&L / Open / Session). Open positions,
Proximity, Last signal, Today's trades, and Account diagnostics all stay.

### Code purge (no production behavior change)

Dead JS removed (~640 lines net): `_tsRender*`, `renderTigerSovereign`,
`renderObserver`, `renderFeatureFlags`, `renderGates` (kept only the
next-scan-countdown shim), `tGateRow`/`permitChip`/`volChip`/`boundaryChip`/
`entryFiredChips`/`gateRow`, plus the executor-side `renderExecSovereign` /
`renderExecGates` and their chip helpers, plus the `applyGateTriState`
helper + window bridge (no remaining callers).

Dead CSS removed (~210 lines): the `.ts-*` block (lines 692-859 of
app.css), the `.gate` row layout, `.gate-armed/.gate-paused/.gate-after-
hours/.gate-halted` color classes, the `.srs-*` and `.chip-srs-*` Sovereign
Regime Shield rules, and the `.tgate` row container (kept only `.tgate-
chip` since it's still used by Val/Gene's Proximity permit-side chips).

### Files touched

- `dashboard_static/index.html` — KPI row 6→4, dropped 4 panels, added
  Weather Check banner + Permit Matrix card.
- `dashboard_static/app.css` — added `.kpi-row-4`, full Permit Matrix +
  Weather Check + Sentinel detail rules + ≤720px / ≤400px mobile overrides;
  pruned `.ts-*` / `.gate*` / `.srs-*` / `.chip-srs-*` / `.tgate` row CSS.
- `dashboard_static/app.js` — added `renderWeatherCheck`,
  `renderPermitMatrix`, `_pmtxBuildRow`, `_pmtxSentinelStrip`,
  `_pmtxGateCell`, `_pmtxNum`, `_pmtxMoney`, `_pmtxIndex`; wired into
  `renderAll` after `renderNextScanCountdown`. Dropped Gate + Regime KPI
  refresh from `refreshExecSharedKpis` and `renderExecutor`. Reinstated
  `updateNextScanLabel` (was tangled in the deleted `renderGates`).
- `bot_version.py`, `trade_genius.py` — bump to 5.17.0.
- `tests/test_startup_smoke.py` — pin to `5.17.` prefix.

---

## v5.16.0 — 2026-04-29 — Legacy purge

Removes four classes of legacy code now that Tiger Sovereign vAA-1 is the
sole live strategy. No behavior change on the hot path: every deletion
targets either dead code with no production caller, or stale comment
graveyards left over from earlier-version cleanups.

### What changed

- **Bucket 1 — `engine/titan_grip.py` shim deleted.** The module was a
  v5.13.x compatibility wrapper that mapped the legacy C1/C2/C3/C4 harvest
  reasons to LIMIT/STOP_MARKET order types. Production switched to the
  Velocity Ratchet (`engine.velocity_ratchet`) as the canonical Alarm C
  evaluator in v5.14.x, and no engine emits the C-series reasons anymore.
  The two cross-check tests in `tests/test_order_types.py` are removed;
  the harvest-reason tests in `tests/test_v5_13_7_close_order_type_wiring.py`
  are renamed and re-documented as legacy back-compat pins on the
  reason→order-type lookup table.
- **Bucket 2 — shadow-strategy tombstone comments stripped.** 25+ comments
  across `trade_genius.py`, `dashboard_server.py`, `broker/`, `engine/`,
  `persistence.py`, `volume_profile.py`, `backtest/__init__.py`, and
  `smoke_test.py` referenced the v5.10.x shadow configs that were removed
  in v5.14.0. The idempotent SQLite `DROP TABLE shadow_positions` and
  `DROP INDEX idx_shadow_*` statements in `persistence.py` are preserved
  (one-line cleanup of stale prod DB rows on boot).
- **Bucket 3 — v4 paper_state migration path removed.**
  `persistence.migrate_from_json` (the one-shot v5.0→v5.1.8 importer that
  copied `v5_*_tracks` blobs into SQLite and renamed the source to
  `.migrated.bak`) is deleted, along with its caller in
  `paper_state.load_paper_state` and its smoke test. Robust
  absence-of-v5-keys handling is preserved: a `paper_state.json` that
  lacks `v5_*_tracks` keys still loads as IDLE without raising.
- **Bucket 4 — legacy DI/structural exit path removed.**
  `tiger_buffalo_v5.evaluate_exit` and `tiger_buffalo_v5.hard_exit_di_fail`
  are deleted. They had no production caller as of v5.7.1, when
  `ENABLE_BISON_BUFFALO_EXITS` flipped to permanent True and the Bison /
  Buffalo exit FSM (forensic_stop / be_stop / ema_trail / velocity_fuse /
  per_trade_brake) became the sole live exit surface for every ticker.
  The dead `_v570_unlimited` branch in `broker/orders.check_breakout` (gated
  by the permanently-False `ENABLE_UNLIMITED_TITAN_STRIKES` flag) is
  collapsed to the always-cap-5 path; STRIKE-CAP-3 from v5.15.0 vAA-1 is
  the active strike gate. The `ENABLE_BISON_BUFFALO_EXITS` constant itself
  is removed (no production reads).
- **Banner update.** `CURRENT_MAIN_NOTE` in `trade_genius.py` is rewritten
  to a v5.16.0 banner (12 lines, all under 34 chars for Telegram
  mobile-width compliance).
- **Version bump.** `bot_version.BOT_VERSION` and `trade_genius.BOT_VERSION`
  go 5.15.1 → 5.16.0; `tests/test_startup_smoke.py` prefix assertion goes
  `5.15.` → `5.16.`.

### Test surface

357 pytest tests pass (same count as v5.15.1). Preflight 6/6 PASS. Three
pre-existing failures in `tests/test_tiger_sovereign_vAA_spec.py`
(`test_strike_cap_3_blocks_fourth_entry`,
`test_strike_flat_gate_blocks_until_position_closes`,
`test_strike_cap_3_overrides_titan_flag`) are inherited from main —
out of scope for this PR.

### Surfaces preserved (intentionally)

- `TITAN_TICKERS` constant — the 10-ticker universe is real, not legacy.
- `ENABLE_UNLIMITED_TITAN_STRIKES = False` — pinned by
  `tests/test_tiger_sovereign_vAA_spec.py::test_strike_cap_3_overrides_titan_flag`.
- `_v570_is_titan` helper — referenced by smoke pins; cheap to keep.
- `tiger_buffalo_v5.HARD_EXIT_DI_THRESHOLD`, `structural_stop_hit_long/short`
  — referenced by smoke pins, no production caller; left in place to
  avoid further smoke-suite churn in this already-broad PR.

---

## v5.15.1 — 2026-04-29 — Tiger Sovereign vAA-1 final wiring

Sentinel surface goes from 2-of-5 (A, B) to 5-of-5 (A, B, C, D, E)
with LIMIT-priced Strike entries. v5.15.0 landed the spec scaffolding
and Alarm E module; v5.15.1 wires the live state caches and pricing
hooks so Alarms C, D, and E actually fire in production.

### What changed

- **Live momentum state in `_run_sentinel`** (`broker/positions.py`):
  module-level caches keyed by `(ticker, side)` track an
  `ADXTrendWindow` (last three 1m ADX), a `TradeHVP` (peak 5m ADX
  since fill), and a singleton `DivergenceMemory` (RSI(15) + price
  peaks per ticker/side). Each tick now computes adx_1m / adx_5m via
  `v5_adx_1m_5m`, pushes into the window, updates HVP, refreshes
  divergence memory with current RSI(15), and passes all six new
  kwargs (adx_window, current_adx_5m, trade_hvp, divergence_memory,
  current_rsi_15, ticker) into `evaluate_sentinel`. Alarms C, D, and
  E are now active on the hot path.
- **Wilder ADX helpers in `trade_genius.py`**: `_compute_adx` mirrors
  the smoothing convention used by `_compute_di`; `v5_adx_1m_5m`
  returns the (adx_1m, adx_5m) tuple consumed by the sentinel. Both
  require `2 * DI_PERIOD` bars to seed and return None below that
  threshold.
- **Strike-fill TradeHVP hook** (`broker/orders.py:execute_breakout`):
  immediately after the position dict is populated, calls
  `ensure_trade_hvp(ticker, side, initial_adx_5m)` so the Alarm D
  peak anchor starts from the actual fill-time 5m ADX. The
  corresponding `clear_trade_hvp` runs in `close_breakout`.
- **09:30 ET session reset**
  (`trade_genius.py:_v570_reset_if_new_session`): when the new-session
  branch fires, also calls `broker.positions.reset_session_state()`
  to wipe DivergenceMemory, ADX windows, and TradeHVPs alongside the
  existing strike-count reset. Prevents stale yesterday peaks from
  blocking today's first entries.
- **Alarm E PRE filter on Strike 2/3 entries**
  (`broker/positions.py:_v5104_maybe_fire_entry_2`): after the
  decision returns `fire=True` but before the order request is
  emitted, computes RSI(15) on 1m closes and calls
  `check_alarm_e_pre(memory=..., strike_num=current_strike+1, ...)`.
  Blocked entries log `[SENT-E-PRE]` and short-circuit; previously
  Alarm E only ran POST-fill via the sentinel.
- **MARKET → LIMIT entry switch** (`executors/base.py:_on_signal`):
  new `_build_entry_request(side, qty, coid)` helper fetches a quote
  via `tg._v512_quote_snapshot(ticker)`, computes
  `compute_strike_limit_price(side, ask, bid)`
  (LONG: ask*1.001, SHORT: bid*0.999, rounded to a cent), and emits a
  `LimitOrderRequest`. Any quote-fetch failure or stale-quote
  exception falls back to the prior `MarketOrderRequest` path with a
  warning log so trading continues.
- **`evaluate_strike_sizing` in `eye_of_tiger.py`** (closes the last
  five vAA-PR-1 spec_gap tests): returns a `StrikeSizingDecision`
  with `size_label` ∈ {FULL, SCALED_A, SCALED_B, WAIT} and
  `shares_to_buy`. Anchors on di_5m > 25.0 and gates the held=0 vs
  add-on paths on di_1m thresholds (FULL: di_1m > 30 with intended
  size; SCALED_A: 25 ≤ di_1m ≤ 30 with intended//2; SCALED_B add-on:
  not alarm_e_blocked AND is_fresh_extreme AND di_1m > 30).
  L-P3-AUTH, L-P3-FULL, L-P3-SCALED-A, L-P3-SCALED-B, and S-P3-FULL
  spec_gap markers removed.

### Test count

394 passed, 0 skipped, 0 failed (was 389 passed + 5 spec_gap skipped
on v5.15.0). Three pre-existing Strike-cap failures on
`--run-spec-gaps` are inherited from main and out of scope for this
release.

---

## v5.15.0 — 2026-04-29 — Tiger Sovereign vAA-1 (PR series #237-#244)

### What changed

v5.15.0 lands the Tiger Sovereign vAA-1 morphing-strategy spec as a
series of seven internally-merged PRs plus this ship PR. The spec
turns the entry/exit lifecycle into a five-alarm sentinel that
evaluates A, B, C, D, and E in parallel on every tick, plus a
strike-model rebuild that caps strikes at 3 and prices entries with
LIMIT orders.

#### PR-1 (#237) — spec adoption + tests

- Adopted `tiger_sovereign_spec_vAA-1.md` as the canonical strategy
  contract.
- Added `tests/test_tiger_sovereign_vAA_spec.py` covering every rule
  in the spec; gaps marked with `@pytest.mark.spec_gap(...)` and
  removed PR-by-PR as the implementation lands.

#### PR-3a (#238) — momentum-state foundation

- New module `engine/momentum_state.py` with `ADXTrendWindow`
  (3-element ring of 1m ADX values used by Alarm C) and
  `DivergenceMemory` (per-(ticker, side) RSI/price peak tracker used
  by Alarm E).
- Comprehensive unit tests for both classes.

#### PR-7 (#239) — Alarm A rename

- Renamed legacy `A_one` / `A_loss_one` symbols to `A_LOSS` /
  `A_FLASH` to match the spec; deleted the obsolete strings.
  No behavioral change \u2014 wiring rename only.

#### PR-2 (#240) — Phase 2 gates

- Added Phase-2 entry gates in `engine/eye_of_tiger.py`: volume
  baseline, EMA9 reclaim guard, and the master-anchor authorization
  for Strike-3 fills.
- Added the helper module `engine/volume_baseline.py`.

#### PR-5 (#241) — Alarm D HVP Lock

- New `check_alarm_d` in `engine/sentinel.py`: full MARKET exit when
  the 5m ADX has decayed below 75% of the per-Strike high-water
  mark, gated by the safety-floor ADX of 25 so flat trades cannot
  be flushed.
- `TradeHVP` dataclass tracks the per-trade ADX peak.

#### PR-3b (#242) — Strike model

- Capped strike count at 3 (`STRIKE-CAP-3`).
- Implemented `STRIKE-FLAT-GATE` so Strike 2 and 3 can only fire
  after a flat-base has formed.
- Switched entry pricing from MARKET to LIMIT for the Strike fills.
- Note: a few sub-rules (TradeHVP write-back at fill time,
  `evaluate_strike_sizing`, `_v5104_maybe_fire_entry_1/2`
  refactor, MARKET\u2192LIMIT call-site switch) are deferred to a
  v5.15.1 follow-up; their tests remain `spec_gap`-marked.

#### PR-4 (#243) — Velocity Ratchet (Alarm C)

- New module `engine/velocity_ratchet.py` and `check_alarm_c`
  in `engine/sentinel.py`: when the 1m ADX trend window prints
  three strictly-decreasing samples, propose a tighter STOP MARKET
  at `current_price * (1 \u2213 0.0025)`. Replaces the legacy Titan
  Grip Harvest harvest-take logic.
- `EXIT_REASON_VELOCITY_RATCHET` is the new top-level reason;
  `EXIT_REASON_ALARM_C` is kept as a back-compat alias.

#### PR-6 (this PR, #244) — Divergence Trap (Alarm E) + ship

- Added `check_alarm_e_pre` (entry-time filter for Strike 2 / 3:
  blocks the strike when the candidate tick prints a divergence vs
  the stored peak in `DivergenceMemory`; Strike 1 is never blocked).
- Added `check_alarm_e_post` (in-trade ratchet: proposes a tighter
  STOP MARKET at `current_price * (1 \u2213 0.0025)` when a
  divergence prints; never loosens an existing stop).
- Wired `check_alarm_e_post` into `evaluate_sentinel` alongside A,
  B, C, and D \u2014 all five alarms evaluate in parallel on every
  tick. New kwargs: `divergence_memory`, `current_rsi_15`, `ticker`.
- New constants: `ALARM_E_RATCHET_PCT = 0.0025`,
  `EXIT_REASON_DIVERGENCE_TRAP = "DIVERGENCE_TRAP"`.
- Bumped `BOT_VERSION` to `5.15.0` in `bot_version.py` and
  `trade_genius.py`.

### Test count at ship

426 passing + 3 newly-unmarked PR-6 tests = 429 passing on the
integration tip; 5 `vAA-PR-1`-tagged spec-gap skips remain (see
PR-3b deferral note above) for v5.15.1.

---

## v5.14.0 — 2026-04-29 — Shadow strategy retirement

### What changed

The four shadow configs (`TICKER+QQQ 70/100`, `TICKER_ONLY 70`,
`QQQ_ONLY 100`, `GEMINI_A 110/85`) and every dashboard surface that
rendered them are removed. Diagnostic log emitters that powered the
shadow replay engine — `[V510-CAND]`, `[V510-FSM]`, `[V510-MINUTE]` —
are deliberately kept alive so future replay-based work can still mine
the live trade tape. The bar archive at `/data/bars/YYYY-MM-DD/` keeps
writing on the unchanged schema. Backtests now read from the canonical
`trade_log.jsonl` and the `executor_positions` table instead of the
retired `shadow_positions` table.

#### Engine / shadow scoring (`trade_genius.py`, `volume_profile.py`)

- Deleted the `SHADOW_CONFIGS` tuple and `evaluate_g4_config()` from
  `volume_profile.py` (-77 lines).
- Removed the per-bar shadow evaluation block in `trade_genius.py` that
  emitted `[V510-SHADOW][CFG=...]` lines and the `[V520-SHADOW-PNL]`
  open / mtm / close calls (-303 lines net).
- Renamed `SHADOW_DATA_AVAILABLE` → `VOLUME_FEED_AVAILABLE` and the
  startup warning `[SHADOW DISABLED]` → `[VOLFEED DISABLED]` so the
  surviving health-check still reads correctly.
- KEPT (load-bearing for future replay): `[V510-CAND]` (skip-reason
  candidates), `[V510-FSM]` (state transitions), `[V510-MINUTE]`
  (sample minute bars), and every live engine emitter (`[V5100-PERMIT]`,
  `[V5100-BOUNDARY]`, `[V510-VEL]`, `[V510-VOLBUCKET]`, `[V510-BAR]`,
  `[V510-DI]`).
- Bar archive writer (`/data/bars/<date>/<TICKER>.jsonl`) is unchanged;
  `engine/scan.py::_bars_for_mtm` is preserved (only the shadow MTM
  hook on top of it was removed).

#### Broker (`broker/orders.py`, `broker/lifecycle.py`)

- `execute_breakout` no longer calls `_v520_open_shadow_all`.
- `close_position` no longer calls `_v520_close_shadow_all`.
- EOD lifecycle no longer force-closes shadow positions at the bell.

#### Persistence (`persistence.py`)

- `init_state_db` now executes `DROP TABLE IF EXISTS shadow_positions`
  and `DROP INDEX IF EXISTS ...` on startup so the table is removed
  in-place on first v5.14.0 boot.
- Helpers `save_shadow_position`, `load_open_shadow_positions`,
  `update_shadow_position`, `close_shadow_position`,
  `count_shadow_positions_today`, `delete_shadow_positions_for_today`
  deleted.
- `executor_positions` table and helpers are unchanged.

#### Dashboard server (`dashboard_server.py`)

- Deleted `_SHADOW_PANEL_ORDER`, `_shadow_pnl_snapshot`,
  `_shadow_charts_payload`, the `h_shadow_charts` route, and its
  registration in `register_routes()` (-269 lines).
- The `shadow_pnl` key is no longer attached to `/api/state`.
- Renamed the data-feed health field on `/api/state`:
  `shadow_data_status` → `volume_feed_status`. The pipeline cron
  `58c883b0` already reads the renamed field; no cron change needed.

#### Dashboard UI (`dashboard_static/{index.html, app.js, app.css}`)

- `index.html`: deleted the Chart.js include, the Shadow nav-tab
  button, and the entire `tg-panel-shadow` block (-81 lines).
- `app.js`: removed `renderShadowPnL` and its call site, the four
  shadow strategy P&L panel render functions, the Shadow entry from
  the `TABS` array, the Shadow branch of `selectTab`, and the entire
  shadow charts / oomph panel block (-974 lines, 3371 → 2397).
- `app.css`: removed the shadow rules block (-284 lines). The only
  `shadow` strings that remain are legitimate `box-shadow` CSS
  properties.

#### Backtest module (`backtest/`)

- Deleted `backtest/replay.py` (the SHADOW_CONFIGS-driven replay
  engine).
- Rewrote `backtest/__init__.py` as a thin docstring describing the
  data-access surface.
- Rewrote `backtest/loader.py` around three primitives that read from
  live data: `load_bars`, `load_prod_trades_from_log` (parses
  `trade_log.jsonl`), `load_open_executor_positions`.
- `backtest/__main__.py` now points at `replay_v511_full.main`.

#### Tests

- Deleted `tests/test_saturday_weekly_report.py` and
  `test_v5_5_6_shadow_uses_prev_bucket.py`.
- `smoke_test.py`: removed ~50 shadow test blocks across three sweeps
  (-1677 lines, ~7291 → ~5614). Updated source-string check to assert
  `[VOLFEED DISABLED]`.
- `tests/test_dashboard_state_v5_13_2.py`: renamed
  `shadow_data_status` assertions to `volume_feed_status`.
- `tests/test_v5_13_7_close_order_type_wiring.py`: removed the
  `_v520_close_shadow_all` stub.
- `test_v5_5_6_previous_session_bucket.py`: docstring updated; logic
  unchanged.

#### Scripts / cron

- Deleted `scripts/saturday_weekly_report.py`. The Saturday
  recurring task that invoked it is unscheduled as part of this
  release.

#### Docs

- `ARCHITECTURE.md`: v5.14.0 retirement banner added at top.
- `CLAUDE.md`: shadow references replaced with v5.14.0 retirement
  notes.

### Migration / behavior at boot

On the first v5.14.0 boot, `init_state_db` drops the
`shadow_positions` table and its indexes. `/api/state` returns
`volume_feed_status` instead of `shadow_data_status` and no
`shadow_pnl` key. The Telegram `/version` and startup banner read
the new `CURRENT_MAIN_NOTE`. No engine behavior change for live
trades — entries / exits / sizing / Sentinel A/B/C are byte-identical
to v5.13.10.

---

## v5.13.10 — 2026-04-29 — Dashboard tooltips + SB column removal + Lifecycle facts strip + Telegram entry-message PDC scrub + Legacy Exits removal

### What changed

A broad pass of hover-help across the dashboard, layout cleanup of the
open positions table, a meaningful enrichment of the Lifecycle tab, a
follow-up scrub of the legacy dual-PDC vocabulary from the Telegram
LONG ENTRY / SHORT ENTRY message (the v5.13.5 scrub missed this one),
AND a full removal of the Legacy Exits feature flag plus all the gated
legacy exit code paths it guarded. The only engine surface change is
the last item; the rest is UI / messaging only.

#### Telegram entry message (`broker/orders.py::execute_breakout`)

- Removed the four hard-coded `✓` claims that were no longer real:
  - `Price > PDC ✓` / `Price < PDC ✓`
  - `SPY > PDC ✓` / `SPY < PDC ✓`
  - `QQQ > PDC ✓` / `QQQ < PDC ✓`
- Removed the `PDC: $...` line from the message body (no longer a gate).
- Added the actual Section I gate readouts the entry path enforces:
  - LONG: `QQQ 5m close > 9-EMA <chk> (close vs ema9)` and
    `QQQ 5m close > 09:30 AVWAP <chk> (close vs avwap)`
  - SHORT: same with the comparators flipped.
- Renamed the boundary line for clarity: the prior single
  `1m close > OR High ✓` line is now followed by an explicit
  `2nd 1m close > OR High ✓` line so users can see that the
  two-consecutive-close boundary_hold rule is what fired (`< OR Low`
  for shorts).
- Reads `tg._QQQ_REGIME.last_close` / `.ema9` and `tg._opening_avwap("QQQ")`
  defensively (returns `—` if either is unseeded), so unit tests and
  smoke modes never block the entry message.
- Two new tests in `tests/test_telegram_pdc_scrub_v5_13_5.py` enforce
  the scrub and the new Section I phrasing.

#### Dashboard `index.html`

- Index strip, brand version pill, brand clock, LIVE pill, h-pulse,
  h-tick: tooltips added or improved.
- All five nav tabs (Main / Val / Gene / Shadow / Lifecycle) carry
  per-tab tooltips.
- All six KPI tiles (Equity, Day P&L, Open positions, Gate state,
  Regime, Session) carry tooltips.
- Tiger Sovereign card and its five phase headers carry tooltips.
- Open positions, Proximity, Observer, Gates, Last signal, and
  Today’s trades cards carry tooltips on the title and chips.
- Feature-flag pills carry ON/OFF semantics in their tooltips.
- Shadow summary band (4 cells), strategy chips, charts panel and
  PnL table head carry tooltips.
- Lifecycle controls: filter, position picker, refresh, count
  badges, and timeline header all carry tooltips.

#### Dashboard `app.js`

- **SB (Soft-Block delta) column removed from open positions** —
  the calc block, `<td>` cell, and `<th>` header are gone. The
  `.eot-sb-*` CSS classes are now unused but left in place
  (harmless dead code, slated for next cleanup).
- Open-positions table headers and row badges (Phase A/B/C, TRAIL,
  side dot) carry tooltips.
- Tiger Sovereign Phase 3 row chips: Entry 1 fired, DI+ value,
  NHOD status, and Entry 2 status each carry tooltips.
- Tiger Sovereign Phase 4 row chips: Alarm A1/A2/B (already had
  tooltips), plus Titan Grip stage, anchor, next, and ratchet
  steps now also carry tooltips.
- Volume gate pill and 2-consecutive boundary chip carry tooltips.
- Trades-summary segments (opens, closes, realized, win) carry
  tooltips.
- Per-ticker gate section label carries a tooltip.

#### Lifecycle tab — inline facts strip

Each lifecycle event row used to show only `#seq | timestamp |
EVENT_TYPE chip | reason_text`, with the full JSON payload hidden
behind a click. v5.13.10 now also surfaces the most useful payload
fields inline as small `key=value` chips with per-field tooltips.
The full JSON pre-block is still available on click.

Known-event field ordering and tooltips:

- `ENTRY_DECISION`: entry_price, limit_price, stop_price, shares,
  entry_num, strike_num, or_high, pdc, stop_capped, entry_id
- `ORDER_SUBMIT`: side, qty, limit_price/price, order_type, action,
  raw_reason
- `ORDER_FILL`: side, qty, fill_price, notional, order_type, action
- `ORDER_CANCEL`: side, qty, reason
- `EXIT_DECISION`: exit_reason, exit_price, entry_price, shares,
  raw_reason
- `POSITION_CLOSED`: realized_pnl, realized_pnl_pct, hold_seconds,
  exit_reason
- `PHASE4_SENTINEL`: state, alarm_codes, current_price, fired,
  exit_reason
- `TITAN_GRIP_STAGE`: stage, anchor, shares_remaining

The event-type chip itself also carries a tooltip describing what the
event type means (e.g. “Phase 4 sentinel — alarm A1/A2/B status
changed on an open position”).

Formatting helpers in `_lcFmtVal`:

- `realized_pnl` / `notional` → `$xxx.xx`
- `realized_pnl_pct` → `xx.xx%`
- `hold_seconds` → `MmSSs`
- Integer-y fields → plain integer
- Other floats → trimmed up to 4 decimals
- Booleans → `yes`/`no`
- Arrays of ≤8 scalars → comma-joined; longer arrays/objects skipped
  (still visible via the JSON pre-block)

Lifecycle position picker dropdown — each option now carries a
tooltip showing the full `position_id`, plus cached `realized_pnl`,
latest Titan stage, and latest Phase 4 state when those metadata
fields are present in the `/api/lifecycle/positions` response.

Lifecycle timeline summary chip — now also shows the latest event
type (“… · latest: TITAN_GRIP_STAGE”) so the user can see at a
glance what the most recent transition was without scrolling.

#### Legacy Exits removal (engine surface change)

v5.13.2 introduced `LEGACY_EXITS_ENABLED` (default OFF) as the kill
switch for the pre-Tiger-Sovereign exit paths so they could be re-armed
for canary windows alongside Tiger Sovereign Phase 4 (Sentinel A/B/C +
Titan Grip). The flag has been OFF in prod since v5.13.2 deployed.
v5.13.10 retires the flag and deletes the gated code outright.

Deleted from `broker/positions.py`:

- `manage_positions` long-side: Section IV legacy override
  (Sovereign-Brake / Velocity-Fuse), Phase B/C state-machine tick,
  structural-stop cross, RED_CANDLE polarity exit, Profit-Lock
  Ladder ratchet, cosmetic `trail_active`/`trail_stop` arming, and
  the ladder-stop exit branch. Sentinel A/B/C is now the sole
  exit decision-maker on the long side.
- `manage_short_positions` short-side: the mirror set — Section IV
  short override, Phase B/C state-machine tick, ladder ratchet,
  cosmetic trail arming, stop-cross exit, and the per-ticker
  POLARITY_SHIFT (price > PDC) exit. Sentinel A/B/C is now the sole
  exit decision-maker on the short side too.
- `_log_conflict_exit` helper (only called from legacy paths).
- The `pos["_last_sentinel_alarms"]` stash that fed `[CONFLICT-EXIT]`
  log lines.
- The `from engine import feature_flags as _ff` import (now unused).

Deleted from `engine/feature_flags.py`:

- `LEGACY_EXITS_ENABLED` constant. Removed from `__all__`. Docstring
  rewritten to flag the retirement; the env var is now ignored if
  still set on Railway.

Deleted from the dashboard surface:

- The Legacy Exits ON/OFF pill in `index.html` (`#ts-flag-legacy`)
  and its `setFlag` wiring in `app.js`.
- `legacy_exits_enabled` from the `feature_flags` block of
  `dashboard_server.snapshot()`.
- The Telegram `/flags` row labeled “Legacy exits (opt-in)”.
- The conditional PDC-strategy block in `telegram_ui/commands.py`
  that only rendered when the env var was true (and its now-unused
  `import os`).

Tests:

- Deleted `tests/test_phase4_legacy_flag.py` (470 lines, all
  exercising the gated paths).
- Updated `tests/test_dashboard_state_v5_13_2.py` line 242 from
  `assert "legacy_exits_enabled" in ff` to
  `assert "legacy_exits_enabled" not in ff` (snapshot must no
  longer surface the field).

Net diff: `broker/positions.py` shrinks from 941 → 633 lines
(308 lines removed). No behavior change vs prod since v5.13.2 — the
flag has been OFF in prod the whole time.

### Files touched

- `dashboard_static/index.html` — ~60 tooltip additions/improvements;
  Legacy Exits pill removed.
- `dashboard_static/app.js` — SB column removal, ~30 inline tooltips,
  full Lifecycle `renderEvents` rewrite with `TYPE_TOOLTIPS`,
  `FIELD_TOOLTIPS`, `_lcFmtVal`, `_lcKeyOrder`, `_lcFactsStrip`;
  Legacy Exits `setFlag` line removed.
- `broker/orders.py::execute_breakout` — Telegram entry-message PDC
  scrub + Section I gate readouts.
- `broker/positions.py` — Legacy exit paths removed (308 lines).
- `engine/feature_flags.py` — `LEGACY_EXITS_ENABLED` retired.
- `dashboard_server.py` — `legacy_exits_enabled` removed from snapshot.
- `telegram_commands.py` — `/flags` Legacy exits row removed.
- `telegram_ui/commands.py` — Legacy PDC-strategy conditional removed.
- `tests/test_telegram_pdc_scrub_v5_13_5.py` — 2 new tests for the
  Section I gate phrasing.
- `tests/test_dashboard_state_v5_13_2.py` — assertion flipped.
- `tests/test_phase4_legacy_flag.py` — deleted.
- `bot_version.py`, `trade_genius.py` — BOT_VERSION + CURRENT_MAIN_NOTE
- `CHANGELOG.md` — this entry.

### Tests / preflight

- 11/11 PDC-scrub tests pass.
- Updated dashboard-state test asserts the new shape.
- Preflight em-dash + forbidden-word checks remain `*.py`/`*.md`
  scoped — JS/HTML real em-dashes continue to be allowed (this is
  intentional, see `scripts/preflight.sh`).
- `node -c dashboard_static/app.js` clean.

---

## v5.13.9 — 2026-04-29 — Gate display rewire (index/polarity → Section I + boundary_hold)

### Symptom

Prod v5.13.8 dashboard, 2026-04-29 mid-session: META satisfied the
actual entry gate (Section I permit `long_open=true` because QQQ 5m
close 660.93 was above ema9 659.65 and above 09:30 AVWAP 658.55, plus
two 1m closes [670.93, 672.09] both above OR-high 668.995), but the
dashboard `index` and `polarity` pills both rendered red. The pills
were computing legacy v4 Tiger 2.0 fields the entry path stopped
consulting in v5.9.0/v5.10.x, so they no longer reflected reality.

### Root cause

`_update_gate_snapshot` in `trade_genius.py` was still computing:

- `index_ok = (spy_p > spy_pdc) AND (qqq_p > qqq_pdc)` — dual-index
  prior-day-close compare, which is not in the Tiger Sovereign spec
  (spec STEP 1 = QQQ 5m vs 9 EMA, STEP 2 = QQQ 5m vs 09:30 AVWAP).
- `polarity_ok = price > pdc_val` (or `<` for short) — single-ticker
  prior-day-close compare. The actual gate is `boundary_hold`: two
  consecutive 1m closes outside the 5m OR high/low.

The entry path (`broker/orders.py:check_breakout`) already routed
through `eot_glue.evaluate_section_i` and `evaluate_boundary_hold_gate`
correctly. The display fields were the only remaining PDC consumers.

A matching `engine/scan.py` PDC-anchored `REGIME: BULLISH/BEARISH`
alert was also still firing on dual-index 1m vs PDC flips. It was
decorative-only (no entry, exit, or sentinel path consumed it) and
likewise had no spec basis.

### Fix

1. `_update_gate_snapshot` now sources `index_ok` from
   `eot_glue.evaluate_section_i(side, qqq_5m_close, qqq_5m_ema9,
   qqq_current_price, qqq_avwap_0930)` and reads `result['open']`.
   Same gate the entry path uses.
2. `polarity_ok` now mirrors `eot_glue.evaluate_boundary_hold_gate(
   ticker, side, or_high, or_low)['hold']`. Same gate the entry path
   uses. Returns `None` (not False) when the prereqs aren't set yet
   (`or_not_set` / `insufficient_closes`) so the dashboard can render
   a yellow pending state instead of mis-flagging red.
3. Deleted dead helpers `gate_two_consecutive_1m_above` /
   `gate_two_consecutive_1m_below` from `engine/volume_baseline.py`
   (and from `__all__`). The eight call sites in
   `tests/test_phase2_gates.py` and the two in
   `tests/test_tiger_sovereign_spec.py` were rewired to call
   `eye_of_tiger.evaluate_boundary_hold(side, or_high, or_low, closes)`
   directly (the canonical implementation since v5.10.x).
4. Dropped the PDC regime alert block in `engine/scan.py:155-190`.
   Removed the `_regime_bullish` module global from `trade_genius.py`
   and the no-longer-needed reset in `reset_daily_state`.
5. Removed the unused `pdc_val` lookup from
   `_update_gate_snapshot`'s preamble.

### Behavior change

Dashboard-only. Entry, exit, sentinel, and FSM paths are untouched.
`/version` displays the new release note. Telegram lines all under
34 chars (mobile-width rule preserved).

---

## v5.13.8 — 2026-04-29 — EMA9 pre-market seed fall-through hotfix

### Symptom

Prod v5.13.7 logs from 2026-04-29 at T+5 minutes after market open:

```
13:35:20 [V572-REGIME-SEED] source=archive bars=1 ema3=None ema9=None compass=None
13:35:20 [V5100-PERMIT] qqq_close=657.15 qqq_ema9=None qqq_avwap=657.628 long_open=False short_open=False
13:40:30 [V572-REGIME] qqq_5m_close=658.01 ema3=657.40 ema9=None compass=None
13:45:41 [V572-REGIME] qqq_5m_close=657.97 ema3=657.68 ema9=None compass=None
```

`ema9` stayed `None` for ~25-45 minutes after open, blocking the
`[V5100-PERMIT]` long/short permit gate during the most volatile window
of the session.

### Root cause

`engine/seeders.py:qqq_regime_seed_once()` orchestrates the seed source
fall-through `archive → alpaca → prior_session`. The `if closes:` guard
short-circuits as soon as the archive returns any non-empty list. On a
cold restart at 13:00 UTC (09:00 ET), the bot subscribes to bars only
at market open and starts archiving them at 09:31 ET; by the 09:35 seed
run, `/data/bars/<today>/QQQ.jsonl` contains exactly one finalized 5m
bucket (09:30-09:34). That single bar passed the truthy guard, so the
Alpaca historical fetch (which would have pulled ~66 5m bars covering
04:00-09:30 ET pre-market and immediately defined ema9) never ran.

### Fix

New constant `engine.seeders.MIN_ARCHIVE_BARS = 9` (the EMA9 window).
The orchestration now treats the archive as authoritative only when it
returns ≥9 bars. Smaller reads fall through to Alpaca; if Alpaca and
prior-session both also fail, the partial archive read is used as a
last resort under a new source label `archive_partial`.

```
old: archive → alpaca → prior_session
new: archive(≥9) → alpaca → prior_session → archive_partial
```

Added an info-level log when the archive is below threshold so the
fall-through is visible in production telemetry:

```
[V572-REGIME-SEED] archive has 1 bars (< 9 minimum); falling through to Alpaca historical
```

### Files

- `engine/seeders.py` — `MIN_ARCHIVE_BARS` constant + reworked
  `qqq_regime_seed_once()` orchestration. `_qqq_seed_from_archive` /
  `_qqq_seed_from_alpaca` / `_qqq_seed_from_prior_session` are unchanged.
- `qqq_regime.py` — `_VALID_SEED_SOURCES` adds `"archive_partial"`.
- `tests/test_qqq_regime_seed_orchestration.py` — new test file covering
  the fall-through matrix (full archive uses archive; sparse archive
  uses Alpaca; sparse archive + Alpaca empty + prior session uses
  prior_session; sparse archive + Alpaca empty + prior session empty
  uses archive_partial; everything empty bails out).
- `bot_version.py`, `trade_genius.py:BOT_VERSION` — 5.13.8.
- `trade_genius.py:CURRENT_MAIN_NOTE` — v5.13.8 deploy banner.

### Follow-up: v5.14.0

This hotfix unblocks the immediate symptom by leaning on the Alpaca
historical fall-back, which already works. The deeper fix — having the
bar archive itself capture pre-market bars (so the archive fast path
is usable from the first restart of the day) — is scoped to v5.14.0.
See `/home/user/workspace/diagnostics/ema9_premarket_seed_bug.md` for
the full diagnosis and side-observation about yesterday's archive
ending early at 15:54 ET.

---

## v5.13.7 — 2026-04-29 — Entry-2 share parity (N1) + order-type wiring through close path

Two fixes from the v5.13.4 spec audit:

### N1 (sub-P2): Entry-2 sized to match Entry-1 share count

Spec (STRATEGY.md L-P3-S6 / S-P3-S6) mandates a **50/50 split by share
count**: "BUY remaining 50%" of a 50/50 split means E2 share count
equals E1 share count. Pre-v5.13.7 we computed
`target_full = floor(PAPER_DOLLARS_PER_ENTRY / current_price)` and
`E2 = target_full - E1` — dollar-notional parity, which produced an
asymmetric split whenever the price drifted between Entry-1 fill and
Entry-2 trigger.

**Behavioral effect on paper P&L (long side, $10,000/entry):**

| Scenario | E1 price | E1 shares | E2 price | E2 (pre-v5.13.7) | E2 (v5.13.7) |
|---|---|---|---|---|---|
| Same price | $50 | 100 | $50 | 100 | 100 |
| Price up 10% | $50 | 100 | $55 | 81 (asymmetric) | 100 (spec-correct) |
| Price down 4% | $50 | 100 | $48 | 108 (asymmetric) | 100 (spec-correct) |

Defensive fallback: if `e1_shares == 0` (Entry-1 didn't actually fire —
shouldn't happen but be safe), Entry-2 keeps the legacy dollar-parity
sizing so we never silently size to 1 share.

The `ENTRY_1_SIZE_PCT + ENTRY_2_SIZE_PCT == 1.0` runtime assert is
preserved.

### MINOR: order-type wiring through close path

`broker.order_types.order_type_for_reason` already maps reason codes
to `LIMIT` / `STOP_MARKET` / `MARKET` per spec, but `close_breakout`
in `broker/orders.py` did not consume it — the resolved type was
metadata-only. The paper book is unaffected (it ignores order_type),
but a future Alpaca live bridge would have always submitted MARKET.

Now:
- `close_breakout` resolves `order_type_for_reason(reason)` and
  threads it into the `_emit_signal` payload (`order_type` field) so
  any downstream executor sees it.
- The v5.13.6 lifecycle log emits a new `ORDER_SUBMIT` event on close
  carrying the resolved `order_type`. `ORDER_FILL` on close also now
  carries it for forensic alignment.

**No behavior change in paper P&L** — paper book ignores order_type;
this fix is exclusively for live broker correctness.

### Out of scope

- No changes to entry sizing percentages (still 50/50).
- No spec rule changes.
- Legacy exit code path (gated, unrelated) untouched.

---

## v5.13.6 — 2026-04-29 — Per-position lifecycle event log

Adds a forensic, append-only per-position event log so every gate
change, decision reason, and trade activity for a stock is captured
from entry through close. Operates entirely alongside the existing
trading logic — **no algorithm or trading-logic changes**. If the log
write path fails, the trading path is unaffected (best-effort writer).

### What's logged

One JSONL file per `position_id` under `/data/lifecycle/`. The
`position_id` is `<TICKER>_<YYYYMMDDTHHMMSSZ>_<long|short>` — a stable
deterministic function of the entry timestamp + ticker, so it survives
bot restarts.

Event types: `ENTRY_DECISION`, `PHASE1_EVAL`, `PHASE2_EVAL`,
`PHASE3_CANDIDATE`, `PHASE4_SENTINEL`, `TITAN_GRIP_STAGE`,
`ORDER_SUBMIT`, `ORDER_FILL`, `ORDER_CANCEL`, `EXIT_DECISION`,
`POSITION_CLOSED`, `REASON`.

### New module: `lifecycle_logger.py`

Public API:

- `LifecycleLogger.open_position(ticker, side, entry_ts_utc, payload)`
  → `position_id`. Writes the ENTRY_DECISION line.
- `.log_event(position_id, event_type, payload, reason_text=None)` —
  appends one line.
- `.close_position(position_id, payload)` — writes POSITION_CLOSED.
- Read-side: `.list_positions(status=open|recent|closed|all)` and
  `.read_events(position_id, since_seq=N)`.

Thread-safe append via per-position `RLock`. Cached metadata index so
the API can answer `list_positions` without re-reading every file.

### Wiring (no behavior change to trading)

- `broker/orders.py::execute_breakout` — calls `open_position(...)`
  with Phase 1-3 snapshots from `v5_13_2_snapshot`, then
  `ORDER_SUBMIT` + `ORDER_FILL`. Stores `lifecycle_position_id` on
  the position dict so subsequent events target the same file.
- `broker/positions.py::_run_sentinel` — diff-based: emits
  `PHASE4_SENTINEL` only when the alarm code set changes vs prior
  tick; emits `TITAN_GRIP_STAGE` only when the stage advances.
  Bounded log volume.
- `broker/orders.py::close_breakout` — emits `EXIT_DECISION`,
  closing `ORDER_FILL`, then the terminal `POSITION_CLOSED` with
  realized P&L summary.

All hooks wrapped in `try/except` and never re-raise.

### Dashboard

New **Lifecycle** tab with:

- `GET /api/lifecycle/positions?status=open|recent|closed|all&limit=N`
  — list of position summaries (ticker, side, entry_ts, status,
  last_event_ts, latest Phase 4 alarm summary).
- `GET /api/lifecycle/{position_id}?since_seq=N` — full timeline /
  pagination by sequence number for tail-follow polling.

Renders a type-coded event timeline (green=ENTRY, blue=PHASE4,
yellow=ORDER, red=EXIT) with click-to-expand payloads. Polls
`since_seq` every 2s while an open position is selected.

### Operational

- One file per position; ~200 events × ~150 bytes = ~30 KB per
  position. No rotation needed in this PR.
- Storage path: `/data/lifecycle/` (Railway volume — already mounted).
  Override with `LIFECYCLE_DIR` env if needed for tests.

---

## v5.13.5 — 2026-04-29 — Telegram surface vocabulary cleanup (Phase 1-4)

Telegram-bot user-facing copy was still describing the pre-v5.9.0
dual-PDC entry/exit model. v5.13.4 cleaned the dashboard but the
Telegram surface still surfaced PDC as an active gate in `/strategy`,
`/proximity`, `/orb`, `/price <ticker>`, and the deploy banner ("PDC
anchor"). **Strings only — no algorithm or trading-logic changes.**

### What changed

- **`/strategy`** — rewritten from the legacy ORB-Long + Wounded-Buffalo
  bullet list (PDC-based entry rules, OR_High − $0.90 / PDC + $0.90
  stops) into the Tiger Sovereign Phase 1–4 description:
  - Phase 1: QQQ 5m close vs 9 EMA + QQQ vs 09:30 AVWAP permit
  - Phase 2: Volume Bucket (PASS/FAIL/COLD/OFF, `VOLUME_GATE_ENABLED`
    env var, default OFF) and Boundary Hold (2 consecutive 1m closes)
  - Phase 3: Entry-1 (DI+ ≥ 25, NHOD) → 50%, Entry-2 (DI+ cross 30,
    fresh NHOD) → remaining 50%
  - Phase 4: Sentinel A (Emergency), B (9-EMA Shield), C (Titan Grip
    harvest + 0.25% ratchet)
  - Stops: entry ± 0.75% cap (matches `MAX_STOP_PCT` in
    `trade_genius.py`)
  - Footnote on `LEGACY_EXITS_ENABLED` (default OFF) for the opt-in
    RED_CANDLE / POLARITY_SHIFT exits.
- **`/proximity`** — global "SPY/QQQ vs PDC" gate replaced with the
  Phase 1 (Section I) QQQ permit booleans (read from
  `v5_10_6_snapshot._section_i_permit`, same source the dashboard
  uses). Per-ticker rows keep gap-to-OR-High / gap-to-OR-Low; the
  bottom "Prices & Polarity vs PDC" block is now just "Prices" — PDC
  polarity arrows dropped.
- **`/regime`** (new) — diagnostic command surfacing QQQ last, 5m
  close, 9 EMA, 09:30 AVWAP, plus `Long permit ✓/✗` and `Short permit
  ✓/✗`. Replaces the never-existed-but-frequently-asked-for
  `/spy_qqq` global-gate diagnostic.
- **`/price <ticker>`** — per-ticker PDC line now suppressed by
  default; only rendered when `LEGACY_EXITS_ENABLED=true` and clearly
  labeled "PDC (legacy)". Long/short eligibility now reads from the
  Phase 1 permit instead of the SPY/QQQ-PDC gate; "above PDC" / "below
  PDC" reasons removed.
- **`/orb`** — bottom "SPY PDC / QQQ PDC" rows replaced with "Phase 1
  long permit / Phase 1 short permit". Per-ticker rows drop the PDC
  column.
- **Deploy banner** (`telegram_ui/runtime.py`) — strategy line changed
  from `"ORB Long + Wounded Buffalo Short | PDC anchor"` to
  `"Tiger Sovereign | Phase 1-4"`; stops line changed from
  `"Long OR_High−$0.90 | Short PDC+$0.90"` to `"entry ± 0.75% (cap)"`.
- **`CURRENT_MAIN_NOTE`** updated to describe v5.13.5.
- **`/help`** menu now lists `/regime` alongside `/proximity` and
  `/mode`.

### Tests

- `tests/test_telegram_pdc_scrub_v5_13_5.py` (new) asserts:
  - `/strategy` body does NOT contain `"SPY > PDC"`, `"SPY < PDC"`,
    `"QQQ > PDC"`, `"QQQ < PDC"`, `"PDC + $0.90"`, or
    `"OR High − $0.90"`.
  - `/strategy` body DOES contain `"Phase 1"` and `"Permit"`.
  - `/proximity` rendered text does NOT contain `"above PDC"` or
    `"below PDC"`.
  - `/proximity` rendered text DOES contain `"Long permit"` /
    `"Short permit"`.

### Out of scope

- No algorithm changes. The Tiger Sovereign FSM, sentinel loop,
  legacy-exit gating, and snapshot helpers are all unchanged.
- Per-ticker PDC is still computed and stored in `tg.pdc` (still
  consumed by the optional legacy exits) — only the user-facing
  display changed.

---

## v5.13.4 — 2026-04-29 — Dashboard pill cleanup (Phase 2 surface) + proximity to entry boundaries only

Frontend-only refactor of the operator dashboard so the per-ticker chips
and the Proximity widget reflect the Tiger Sovereign Phase 1–4 spec
(live as of v5.13.2) instead of legacy v5.9.x gate names. **No
algorithm or trading-logic changes** — the underlying snapshot already
publishes the correct shape; this PR only changes how `dashboard_server`
shapes proximity output and how `app.js` renders the pills.

### Per-ticker pills (Phase 2 surface)

The `GATES · ENTRY CHECKS` per-ticker rows previously rendered legacy
chips `Brk · PDC · Idx · DI` (pre-Tiger-Sovereign concepts: index-PDC
regime gate retired in v5.9.1, per-ticker PDC is now legacy-exit-only,
DI was a v5.10.x concept). Replaced with a 3-chip set sourced from
`tiger_sovereign.phase1` + `tiger_sovereign.phase2`:

- **Permit** (Phase 1) — `L✓/L✗ S✓/S✗` two-pill pair from
  `phase1.long.permit` and `phase1.short.permit`.
- **Vol** — `phase2[ticker].vol_gate_status` (PASS/FAIL/COLD/OFF).
  COLD renders amber; OFF is dimmed and the section sub-label shows
  the runtime `VOLUME_GATE_ENABLED` override notice.
- **Boundary** — `↑↑` / `↓↓` / `…` from `two_consec_above` /
  `two_consec_below` (Boundary Hold 2-close confirmation).
- **E1✓ / E2✓** — appended when `phase3` shows entry 1 / entry 2
  fired for the ticker.

Section sub-label changed:
`Per-ticker · Brk · PDC · Idx · DI` → `Per-ticker · Permit · Vol · Boundary`.

Applied to both the Main view and the Executor panel renderers.

### Proximity widget

`_proximity_rows()` previously reported distance to the nearest of
{OR-high, OR-low, **PDC**}. Under Tiger Sovereign, PDC is not an
entry-relevant level (it stayed in the dashboard from the v5.9.x
regime gate that was retired). New behaviour:

- OR-high is candidate when `phase1.long.permit` is true.
- OR-low is candidate when `phase1.short.permit` is true.
- When neither permit is active: closer of OR-high/OR-low is shown
  but the row is dimmed and tagged `no permit` so the operator sees
  a breakout would not currently fire.
- PDC is dropped entirely from proximity. (Per-ticker PDC remains in
  per-position legacy-exit telemetry only when `LEGACY_EXITS_ENABLED=true`.)

New backend field on each proximity row: `permit_side`
∈ {LONG, SHORT, BOTH, NONE}. The legacy `pdc` field is removed.

### Backend snapshot fields

The `_ticker_gates` snapshot fields (`break / polarity / index / di`)
are preserved on `gates.per_ticker` for any non-dashboard surface
that may still consume them (Telegram etc.); only the dashboard
renderers stopped reading them.

### Tests

- `tests/test_dashboard_state_v5_13_2.py` — new
  `test_proximity_rows_drops_pdc_and_carries_permit_side` asserting
  `nearest_label ∈ {"OR-high","OR-low",""}`, `permit_side` present,
  and the legacy `pdc` field absent.

### Manual verification

- Load the dashboard with no positions → per-ticker rows render
  three chips (Permit / Vol / Boundary) per ticker; section sub-label
  reads `Per-ticker · Permit · Vol · Boundary`.
- Proximity widget shows percent-to-OR-high/OR-low and a permit-side
  chip; when no permit is active, rows are dimmed and the chip reads
  `no permit`.

---

## v5.13.3 — 2026-04-29 — Hotfix: ship v5_13_2_snapshot.py in Docker image

v5.13.2 added `v5_13_2_snapshot.py` (consumed by `dashboard_server.py`
for the `tiger_sovereign` block in `/api/state`) but the Dockerfile only
explicitly copies a curated set of top-level Python modules. The new
snapshot module was missed, so production raised
`ModuleNotFoundError: No module named 'v5_13_2_snapshot'` on every
dashboard poll (`dashboard_server.py:906`). Trading logic itself was
unaffected — only the dashboard `/api/state` endpoint logged the error.

### Fix

- **Dockerfile**: added `COPY v5_13_2_snapshot.py .` alongside the other
  top-level module copies so the snapshot ships in the runtime image.
- **tests/test_startup_smoke.py**: extended
  `test_dockerfile_copies_every_top_level_python_module` to walk the AST
  imports of both `trade_genius.py` and `dashboard_server.py` (was only
  `trade_genius.py`). Lazy/optional imports inside try blocks or
  functions are caught by the AST walk so a missed COPY fails the smoke
  test in CI rather than at runtime.

No behavioural changes. No spec changes.

---

## v5.13.2 — 2026-04-29 — Spec-of-record cleanup + dashboard rewrite

Addresses the v5.13.1 spec compliance audit (verdict WARN). Two P0 fixes,
one P1 architectural cleanup, one P1 sentinel-baseline correctness fix,
plus dashboard rewrite to surface Tiger Sovereign Phase 1-4 state
correctly. **No spec changes.** Production behaviour shifts only when
operators set `LEGACY_EXITS_ENABLED=true` (default OFF).

### Track A — Entry sizing 50/50 + Daily Circuit Breaker close-positions (P0)

- **Entry sizing now matches spec.** `paper_shares_for` consumes
  `ENTRY_1_SIZE_PCT = 0.50`; `_v5104_maybe_fire_entry_2` tops up to full
  notional rather than adding `e1_shares // 2`. Total position notional
  is now ≈ `PAPER_DOLLARS_PER_ENTRY` (was ~1.5×).
- **Daily Circuit Breaker force-closes open positions.** When
  `today_pnl <= -1500`, after `_trading_halted = True` is set, the bot
  iterates `positions` and `short_positions` and calls
  `close_position` / `close_short_position` with reason
  `DAILY_LOSS_LIMIT` (maps to MARKET via `REASON_CIRCUIT_BREAKER`).
  Idempotent on the false→true transition.
- `ENTRY_1_SIZE_PCT` / `ENTRY_2_SIZE_PCT` are no longer dead code.

### Track B — `LEGACY_EXITS_ENABLED` flag + Alarm A baseline reset (P1)

- New env var `LEGACY_EXITS_ENABLED` (default `false`). When OFF, Tiger
  Sovereign Phase 4 (Sentinel A/B/C + Titan Grip) is the sole exit
  authority. When ON, the legacy v5.10/v5.11 exit paths run alongside
  Sentinel and emit `[CONFLICT-EXIT]` structured logs whenever both fire
  on the same tick.
- Gated paths: Profit-Lock Ladder, Section IV brake/fuse, Phase A/B/C
  state machine, RED_CANDLE long exit, POLARITY_SHIFT short exit.
- PDC dict population stays (still consumed by dashboard pills,
  `[V510-IDX]` shadow logger, position records).
- **Alarm A velocity baseline reset on Entry-2 fill.** `check_alarm_a`
  detects `last_known_shares` change and clears `pnl_history` to avoid
  artificial deltas from pre-Entry-2 P&L samples being divided by
  post-Entry-2 notional.

### Track C — Dashboard rewrite

- **Deleted retired "Sovereign Regime Shield" panel** (described the
  PDC-based dual-index eject retired in v5.9.1).
- **Rewrote "Eye of the Tiger · live gates" panel** as Tiger Sovereign
  Phase 1-4 surface: Phase 1 permits per side, Phase 2 gates per
  ticker, Phase 3 entry candidates, Phase 4 active management with
  Sentinel A1/A2/B distance-to-trip and Titan Grip stage + ratchet
  anchor + next harvest target.
- **KPI row feature-flag indicators**: `Volume Gate: ON|OFF` and
  `Legacy Exits: ON|OFF` so ops can see the runtime overrides at a
  glance.
- **Dropped legacy PDC pills** from per-position cards (cosmetic only,
  no longer represent live gates).
- `/api/state` shape: added `feature_flags` and `tiger_sovereign`
  (phase1/2/3/4) blocks. `shadow_data_status` preserved (cron
  dependency unbroken).

### Track D — Stale constant cleanup + behavioural test conversion (P2)

- Deleted `EOD_FLUSH_HHMMSS_ET = "15:59:50"` and `is_eod_flush_time`
  from `eye_of_tiger.py`; canonical EOD constant lives in
  `engine/timing.EOD_FLUSH_ET = time(15, 49, 59)`.
- `tests/test_tiger_sovereign_spec.py` — 30 rule tests rewritten from
  shallow grep-existence checks to behavioural assertions against the
  actual evaluators (`evaluate_global_permit`, `gate_volume_pass`,
  `check_alarm_a/b`, `check_titan_grip`, `is_after_cutoff_et`,
  `order_type_for_reason`, etc.).
- Test names + rule IDs preserved so audit/cron tooling that greps
  test names still works.

### Verification

- 304 tests passing (was 287 in v5.13.1; +17 across the four tracks).
- Full preflight green.

### Defaults at deploy

| Flag | Default |
|------|---------|
| `VOLUME_GATE_ENABLED` | `false` (carried from v5.13.1) |
| `LEGACY_EXITS_ENABLED` | `false` (new) |

With both flags off, the bot runs the spec-strict Tiger Sovereign
logic only: no Phase 2 volume gate, no legacy exit paths. Set either
to `true` on Railway to restore prior behaviour.

---

## v5.13.1 — 2026-04-29 — Volume-gate runtime flag (default OFF)

Adds `VOLUME_GATE_ENABLED` env var to toggle the Phase 2 volume gate
(L-P2-S3 / S-P2-S3) at runtime. **Default is `false`** — the gate is
DISABLED in production by default. Set `VOLUME_GATE_ENABLED=true` on
Railway to restore spec-strict behavior.

### Why

Backtest analysis on 2026-04-28 (`/v5_13_0_today_backtest_no_volume/report.md`
vs `/v5_13_0_today_backtest/report.md`) showed the spec-strict 100%-of-55-day
volume gate filtered out trades that, when re-admitted with the full
Sentinel Loop exit logic in place, returned net positive (+$251 cohort
P&L). Until multi-day analysis confirms direction we operate with the
gate OFF; the env var lets us flip back to spec-strict without a code
change.

### Scope

- New module `engine/feature_flags.py` — single env-var-read constant
  `VOLUME_GATE_ENABLED` (read once at import, default `False`).
- `engine/volume_baseline.gate_volume_pass` — early-return `(True, None)`
  (DISABLED_BY_FLAG) when the flag is False.
- `eye_of_tiger.evaluate_volume_bucket` — same early-return so the live
  hot path mirrors the spec-named gate.
- `trade_genius.py` — boot log line emits the active state next to the
  existing `[V560]` startup banner.
- `STRATEGY.md` — new "Operational Overrides" section near the top;
  L-P2-S3 / S-P2-S3 footnoted with `*` referencing the flag.
- Tests:
    - `tests/test_phase2_gates.py` — flag-OFF auto-pass cases (volume
      regardless of ratio), flag-ON spec-strict cases pinned via
      monkeypatch, parity check that the 2-consecutive-1m gate stays
      enforced when only the volume flag is OFF.
    - `tests/test_tiger_sovereign_spec.py` — L-P2-S3 / S-P2-S3 docstrings
      cite the flag; assertion still validates the source-level baseline
      plumbing exists so the rule can be re-enabled at runtime.
    - `tests/test_startup_smoke.py` — assertion that with the env var
      unset, the constant resolves to `False`.

### Behavior change

The Phase 2 volume gate auto-passes on production by default. The
2-consecutive-1m-candle gate (L-P2-S4 / S-P2-S4) and **all** other
Phase 2 logic continue to apply unchanged. Phase 1 / 3 / 4 are
untouched.

---

## v5.13.0 — 2026-04-29 — Tiger Sovereign migration

Adopts the Tiger Sovereign spec (`STRATEGY.md` v2026-04-28h) as the
canonical strategy, replacing the v5 strategy doc. Rule IDs from the
spec are wired into per-rule tests in `tests/test_tiger_sovereign_spec.py`
and code comments cite the rule IDs throughout. All 30 rule tests pass
with zero `@pytest.mark.spec_gap` markers remaining.

### PR 1 (#216) — Spec adoption + per-rule test scaffold

- Replaced `STRATEGY.md` with the Tiger Sovereign spec verbatim
- Archived prior strategy at `docs/spec_archive/v5_strategy_pre_tiger_sovereign.md`
- Added `tests/test_tiger_sovereign_spec.py` with one test per rule ID
  (12 long, 12 short, 6 shared = 30 tests)
- Added `tests/spec_gap_report.py` to inventory `@pytest.mark.spec_gap`
  markers (PR-2 through PR-6)
- Behavior change: NONE — read-only

### PR 2 (#217) — Sentinel Loop with parallel Alarms A & B

- New `engine/sentinel.py` with pure functions:
  `check_alarm_a` (-$500 hard floor + -1%/min velocity),
  `check_alarm_b` (5m close vs 9-EMA),
  `evaluate_sentinel` (parallel — never short-circuits)
- Wired into per-tick loop in `broker/positions.py`
- Per-position bounded P&L history (deque, maxlen=120)

### PR 3 (#219) — Titan Grip Harvest ratchet (Alarm C)

- New `engine/titan_grip.py` state machine implementing Stages 1-4:
  Stage 1 anchor at OR_High + 0.93%, Stage 1 stop at +0.40%,
  Stage 2 micro-ratchet (+0.25% per step),
  Stage 3 second harvest at +1.88%,
  Stage 4 runner with continued +0.25% trail
- Short side mirrors with OR_Low minus the same offsets
- Replaced prior tier-table ratchet
- Each emitted action carries a spec-mandated `order_type`
  (LIMIT for harvests, STOP_MARKET for ratchet/runner)

### PR 4 (#218) — Phase 2 entry gates

- New `engine/volume_baseline.py` with 55-day rolling per-minute
  volume baseline (`L-P2-S3` / `S-P2-S3`: volume ≥ 100% of average)
- Two-consecutive-1m-candle confirmation above OR_High / below OR_Low
  (`L-P2-S4` / `S-P2-S4`)
- Tightens Phase 2 — entries that previously fired on a single candle
  now require the volume gate AND two confirmed closes

### PR 5 (#220) — Timing rules

- `SHARED-CUTOFF`: New-position cutoff moved from 15:30 ET to 15:44:59 ET
- `SHARED-EOD`: EOD flush moved from 15:59:50 ET to 15:49:59 ET
- `SHARED-HUNT`: Verified unlimited hunting until cutoff
- `SHARED-CB`: Verified daily circuit breaker at -$1,500

### PR 6 (this PR) — Order types + version bump

- New `broker/order_types.py` — single source of truth for the
  reason → order-type mapping per STRATEGY.md §3:
  - Profit-taking (Stage 1, Stage 3 harvest) → LIMIT
  - Defensive stops (Alarm A1, Alarm A2, Alarm B, Stage 2 ratchet,
    Stage 4 runner exit) → STOP MARKET
  - EOD flush, Daily Circuit Breaker → MARKET
- Public helpers: `order_type_for_reason(reason)`, `submit_exit(...)`
- Removed both `@pytest.mark.spec_gap("PR-6", ...)` markers; tests now
  assert `order_type_for_reason` returns the spec-correct type
- Added `tests/test_order_types.py` covering long + short side
  scenarios for every reason class
- BOT_VERSION 5.12.0 → 5.13.0 in both `trade_genius.py` and `bot_version.py`
- CURRENT_MAIN_NOTE updated to v5.13.0 — Tiger Sovereign

### Order-type mapping (PR 6 reference table)

| Spec rule | Reason code | Order type |
|---|---|---|
| `L-P4-C-S1` / `S-P4-C-S1` | `C1_STAGE1_HARVEST` | LIMIT |
| `L-P4-C-S2` / `S-P4-C-S2` | `C2_RATCHET` | STOP MARKET |
| `L-P4-C-S3` / `S-P4-C-S3` | `C3_STAGE3_HARVEST` | LIMIT |
| `L-P4-C-S4` / `S-P4-C-S4` | `C4_RUNNER_EXIT` | STOP MARKET |
| `L-P4-A` / `S-P4-A` | `sentinel_alarm_a` | STOP MARKET |
| `L-P4-B` / `S-P4-B` | `sentinel_alarm_b` | STOP MARKET |
| `SHARED-EOD` | `EOD` | MARKET |
| `SHARED-CB` | `DAILY_LOSS_LIMIT` | MARKET |

### Validation

- 30/30 spec tests pass; `tests/spec_gap_report.py` inventory empty
- Golden harness (`tests/golden/v5_10_7_session_2026-04-28.jsonl`)
  unchanged at 10,971,364 bytes — PR 6 only touches order-submission
  decision logic, not the 5m EMA computation that the harness records
- Synthetic harness baseline preserved
- Smoke test: 361 + 30 spec tests passing

---

## v5.12.0 — 2026-04-29 — Executors extraction + alias purge

### PR 1 (#212) — executors/base.py — TradeGeniusBase extraction

- Created `executors/` package
- Moved `TradeGeniusBase` (~600 lines) out of `trade_genius.py` to `executors/base.py`
- Re-exported in `trade_genius` for back-compat with `m.TradeGeniusBase`
  lookups in smoke_test and dashboard probes
- Boot log: `[EXEC] modules loaded: base`

### PR 2 (#213) — executors/val.py + executors/gene.py

- Moved `TradeGeniusVal` and `TradeGeniusGene` subclasses out of `trade_genius.py`
- Both classes preserved as subclasses of `TradeGeniusBase`
- Re-exported in `trade_genius` for back-compat with the 9 `m.TradeGeniusVal` /
  `m.TradeGeniusGene` smoke-test cases
- Boot log: `[EXEC] modules loaded: base, val, gene`

### PR 3 (#214) — executors/bootstrap.py wiring helpers

- Extracted the val/gene executor bootstrap block into
  `executors.bootstrap.{build_val_executor, build_gene_executor, install_globals}`
- `install_globals()` writes `val_executor` / `gene_executor` into both
  `trade_genius` and `telegram_commands` module namespaces so the
  `globals().get('val_executor')` lookup at telegram_commands.py:647 keeps
  working
- Boot log: `[EXEC] modules loaded: base, val, gene, bootstrap`

### PR 4 (#211) — synthetic_harness + smoke_test explicit imports

- `synthetic_harness/runner.py` and `smoke_test.py` now import the names they
  exercise directly from their canonical homes (`broker.*`, `engine.*`,
  `telegram_ui.*`, `executors.*`) instead of relying on the v5.11.x
  deprecation aliases in `trade_genius`
- The 7 `inspect.getsource()` sites in smoke_test resolve via the canonical
  module, no longer bouncing through the trade_genius alias

### PR 5 (this) — alias removal + BOT_VERSION 5.12.0

- Removed all v5.11.x deprecation alias blocks from `trade_genius.py`:
  - v5.11.2 broker re-imports (stops/orders/positions/lifecycle re-exports
    that no longer carry the "deprecation aliases — removed in v5.12.0"
    comment; the names trade_genius itself still calls remain as ordinary
    canonical imports)
  - v5.11.1 telegram_ui chart/commands/menu re-imports — purged. Callers
    (`telegram_commands.py`) now import from `telegram_ui.{charts, commands, menu}`
    directly.
  - v5.11.0 `_v5105_compute_5m_ohlc_and_ema9` and `_v5105_phase_machine_tick`
    private aliases — purged. `broker/positions.py` updated to call
    `tg._engine_phase_machine_tick` directly.
- `tests/test_telegram_ui_imports.py::test_trade_genius_deprecation_aliases`
  inverted to assert the aliases are **gone**
- `tests/test_executors_imports.py::test_no_deprecation_aliases_remain_in_trade_genius`
  added as a permanent guard
- **BOT_VERSION → "5.12.0"**, `CURRENT_MAIN_NOTE` synced

### v5.12.0 release composition

- PR 1 (#212): executors/base.py — TradeGeniusBase extraction
- PR 2 (#213): executors/val.py + executors/gene.py — Val/Gene extraction
- PR 3 (#214): executors/bootstrap.py — wiring helpers
- PR 4 (#211): synthetic_harness + smoke_test explicit imports
- PR 5 (this): v5.11.x deprecation aliases purged + BOT_VERSION 5.11.2 → 5.12.0
- **Net: trade_genius.py 7,239 → 6,016 lines (-17% in v5.12.0; -52% session-cumulative from 12,512)**
- Golden harness byte-equal (10,971,364 bytes) preserved across all five PRs
- Synthetic harness baseline (38/12 of 50) preserved across all five PRs
- Smoke baseline 361 passed / 28 failed held throughout

---

## v5.11.2 — 2026-04-29 — Broker / position-management extraction

### PR 1 — broker/stops.py

- Created `broker/` package
- Moved stop-management helpers (~600 lines) out of `trade_genius.py`:
  `_breakeven_long_stop`, `_breakeven_short_stop`, `_capped_long_stop`,
  `_capped_short_stop`, `_ladder_stop_long`, `_ladder_stop_short`,
  `_retighten_long_stop`, `_retighten_short_stop`, `retighten_all_stops`
- `telegram_commands.py` now imports `retighten_all_stops` from `broker.stops`
- `side.py` `getattr(trade_genius, capped_stop_fn_name)` continues to resolve via deprecation alias
- Deprecation aliases in `trade_genius.py` (one-release window, removed v5.12.0)
- Boot log: `[BROKER] modules loaded: stops`

### PR 2 — broker/orders.py
- Moved order-execution functions (~850 lines) out of `trade_genius.py`:
  `check_breakout`, `paper_shares_for`, `execute_breakout`, `close_breakout`
- Cross-package imports: `broker.orders` imports `_capped_long_stop` and
  `_capped_short_stop` from `broker.stops` directly
- Deprecation aliases in `trade_genius.py` (one-release window, removed v5.12.0)
- Boot log: `[BROKER] modules loaded: stops, orders`

### PR 3 — broker/positions.py
- Moved per-tick position management (~430 lines) out of `trade_genius.py`:
  `_v5104_maybe_fire_entry_2`, `manage_positions`, `manage_short_positions`
- `broker.orders.check_breakout` continues to reach `_v5104_maybe_fire_entry_2`
  via the `_tg()` shim to avoid a circular import (positions → orders, orders → positions)
- Cross-package imports: `broker.positions` imports `check_breakout` from
  `broker.orders` and stop helpers from `broker.stops`
- Deprecation aliases in `trade_genius.py` (one-release window, removed v5.12.0)
- Boot log: `[BROKER] modules loaded: stops, orders, positions`

### PR 4 — broker/lifecycle.py + version bump
- Moved entry/exit dispatchers and EOD close out of `trade_genius.py`:
  `check_entry`, `check_short_entry`, `execute_entry`, `execute_short_entry`,
  `close_position`, `close_short_position`, `eod_close`
- Added `tests/test_broker_imports.py` regression guard
- **BOT_VERSION → "5.11.2"**, CURRENT_MAIN_NOTE synced
- Boot log: `[BROKER] modules loaded: stops, orders, positions, lifecycle`

### v5.11.2 release composition
- PR 1 (#207): broker/stops.py — stop-management helpers
- PR 2 (#208): broker/orders.py — order execution
- PR 3 (#209): broker/positions.py — per-tick position management
- PR 4 (this): broker/lifecycle.py — entry/exit dispatchers + version bump
- **Net: trade_genius.py 8,933 → ~7,150 lines (~20% reduction)**
- Golden harness byte-equal across all four PRs
- Synthetic harness baseline (38/12) preserved across all four PRs
- Smoke baseline 361 / 28 held throughout

---

## v5.11.1 — 2026-04-29 — Telegram handler extraction

### PR 1 — telegram_ui/charts.py

- Created `telegram_ui/` package
- Moved chart and dayreport helpers (470 lines) out of `trade_genius.py`:
  `_chart_dayreport`, `_chart_equity_curve`, `_chart_portfolio_pie`,
  `_open_positions_as_pseudo_trades`, `_format_dayreport_section`,
  `_collect_day_rows`, `_reply_in_chunks`, plus formatter helpers
  (`_dayreport_time`, `_dayreport_sort_key`, `_short_reason`, `_fmt_pnl`)
- Deprecation aliases in `trade_genius.py` (one-release window, removed v5.12.0)
- Boot log: `[TELEGRAM-UI] modules loaded: charts`

### PR 2 — telegram_ui/commands.py
- Moved sync command builders (~900 lines) out of `trade_genius.py`:
  `_log_sync`, `_replay_sync`, `_reset_buttons`, `_perf_compute`, `_price_sync`,
  `_proximity_sync`, `_proximity_keyboard`, `_orb_sync`, `_fetch_or_for_ticker`,
  `_or_now_sync`, `_fmt_tickers_list`, `_fmt_add_reply`, `_fmt_remove_reply`
- Deprecation aliases in `trade_genius.py` (one-release window, removed v5.12.0)
- Boot log: `[TELEGRAM-UI] modules loaded: charts, commands`

### PR 3 — telegram_ui/menu.py
- Moved keyboards, callback handlers, and dispatch shim (~750 lines) out of `trade_genius.py`:
  `positions_callback`, `proximity_callback`, `monitoring_callback`,
  `_build_menu_keyboard`, `_build_advanced_menu_keyboard`, `_menu_button`,
  `_cb_open_menu`, `_CallbackUpdateShim`, `_invoke_from_callback`, `menu_callback`
- Updated `telegram_commands.py` to import `_build_menu_keyboard` from `telegram_ui.menu`
- Deprecation aliases in `trade_genius.py` (one-release window, removed v5.12.0)
- Boot log: `[TELEGRAM-UI] modules loaded: charts, commands, menu`

### PR 4 — telegram_ui/runtime.py + version bump
- Moved bot lifecycle (~250 lines) out of `trade_genius.py`:
  `_set_bot_commands`, `_send_startup_menu`, `send_startup_message`,
  `_auth_guard`, `run_telegram_bot`
- `send_telegram` intentionally stays in `trade_genius.py` (broker-side
  notification entry used by paper_state, error_state, scheduler)
- Added `tests/test_telegram_ui_imports.py` regression guard
- **BOT_VERSION → "5.11.1"**
- Boot log: `[TELEGRAM-UI] modules loaded: charts, commands, menu, runtime`

### v5.11.1 release composition
- PR 1 (#203): telegram_ui/charts.py — chart and dayreport helpers
- PR 2 (#204): telegram_ui/commands.py — sync command builders
- PR 3 (#205): telegram_ui/menu.py — keyboards and callback dispatch
- PR 4 (this): telegram_ui/runtime.py — bot lifecycle + version bump
- **Net: trade_genius.py 10,752 → ~8,800 lines (~18% reduction)**
- Golden harness remained byte-equal (10,971,364 bytes) across all four PRs
- Smoke baseline 361 passed / 28 failed held throughout

---

## v5.11.0 — 2026-04-28 — Engine extraction (trading-rules engine moved into `engine/`)

Engine extraction release. The trading rules engine — bar aggregation,
pre-market seeding, phase machine, and per-minute scan loop — has been
moved into a dedicated `engine/` package behind a small `EngineCallbacks`
Protocol seam. **Zero behavior change.** `trade_genius.py` shrinks from
12,576 → 10,757 lines; the per-tick decision path is now importable in
isolation, which makes the replay/test surface dramatically smaller.

### Modules added

- `engine/__init__.py` — public surface re-exports: `compute_5m_ohlc_and_ema9`, `qqq_regime_seed_once`, `qqq_regime_tick`, `seed_di_buffer`, `seed_di_all`, `seed_opening_range`, `seed_opening_range_all`, `phase_machine_tick`, `EngineCallbacks`, `scan_loop`. Boot log: `[ENGINE] modules loaded: bars, seeders, phase_machine, callbacks, scan`.
- `engine/bars.py` — 5-minute OHLC + EMA(9) aggregation (`compute_5m_ohlc_and_ema9`) and bar normalization helpers shared between prod and replay.
- `engine/seeders.py` — pre-market warmup: QQQ regime seed/tick, DI(15) buffer seed, opening-range seed (single-ticker and universe variants).
- `engine/phase_machine.py` — Phase A → B-Layered → B-Locked → C-Extraction state machine (`phase_machine_tick`); pure decision function over `eye_of_tiger.evaluate_*`.
- `engine/callbacks.py` — `EngineCallbacks` Protocol that names every side effect the engine performs (broker calls, position lookup, alerts, clock). Prod injects a wrapper around `trade_genius` functions; replay injects a record-only mock.
- `engine/scan.py` — per-minute `scan_loop()` over the universe; the production `scan_loop` in `trade_genius.py` is now a thin shim that builds the callback impl and delegates.

### Removed

- `_MAIN_HISTORY_TAIL` (~801 lines of in-code rolling release history) deleted from `trade_genius.py`. The `/version` Telegram command now shows only `CURRENT_MAIN_NOTE` (the active release). **`CHANGELOG.md` is the canonical history** — every prior release's note is preserved here.

### Validation

A byte-equal synthetic harness (`tests/golden/record_session.py` +
`verify.py`) was introduced in PR 1 and re-run after every subsequent
extraction PR. The harness streams archived 1m bars from
`today_bars/2026-04-28/` through the engine and writes a deterministic
JSONL trace; the trace must be byte-identical pre/post each move.
Verified across all five extraction PRs — no floating-point drift, no
log line drift. Smoke baseline holds at ~364 passed / 25 pre-existing
failures.

### Migration notes

For one release only, version-prefixed private aliases are kept in
`trade_genius.py` for callers we haven't migrated yet (e.g.
`_v5105_compute_5m_ohlc_and_ema9 = compute_5m_ohlc_and_ema9`,
`_v590_qqq_regime_seed_once = qqq_regime_seed_once`,
`_v5105_phase_machine_tick = phase_machine_tick`). **These shims will
be removed in v5.12.0.** New code should import from `engine` directly.

### Release composition (per-PR audit trail)

- **PR 1** (#196): synthetic harness + `engine/bars.py`. Introduced `tests/golden/{record_session,verify}.py`, the deterministic JSONL trace gate that every subsequent PR re-ran; moved `compute_5m_ohlc_and_ema9` verbatim from `trade_genius.py:8270` (was `_v5105_compute_5m_ohlc_and_ema9`); added `engine/__init__.py`; `Dockerfile` updated to `COPY engine/`; first boot log line `[ENGINE] modules loaded: bars`. `BOT_VERSION` bumped to 5.11.0 here; subsequent PRs do not bump.
- **PR 2** (#197): `engine/seeders.py`. Pre-market QQQ regime + DI(15) buffer + opening-range seeders moved as a unit (was `_v590_qqq_regime_seed_once`, `_seed_di_buffer`, `_seed_di_all`, `_seed_opening_range`, `_seed_opening_range_all`). No behavior change.
- **PR 3** (#199): `engine/phase_machine.py`. Phase A/B/C state machine moved (was `_v5105_phase_machine_tick`, ~500 lines). Harness byte-equal across move.
- **PR 4** (#200): `engine/callbacks.py` + `engine/scan.py`. `EngineCallbacks` Protocol introduced; `scan_loop` body moved behind it; production `scan_loop` in `trade_genius.py` reduced to a thin shim that constructs the callback impl and delegates.
- **Cleanup PR** (#198): removed `_MAIN_HISTORY_TAIL` (~801 lines) from `trade_genius.py`; `/version` Telegram surface now shows only `CURRENT_MAIN_NOTE`.
- **PR 5** (#201): `ARCHITECTURE.md` refresh for the new module map; algo PDF regenerated; this CHANGELOG entry tidied; `CURRENT_MAIN_NOTE` updated to v5.11.0.
- **PR 6** (this PR): canonical replay harness ported into the repo and rewired to consume `engine.scan.scan_loop` directly via a new `RecordOnlyCallbacks` impl of the `EngineCallbacks` Protocol. New file `backtest/replay_v511_full.py` replaces the workspace-only `replay_v510_full_v4.py` (791 lines, parallel re-implementation of the per-tick scan/seed/phase logic that drifted from prod every release). The replay driver installs a `SimpleNamespace` stub at `sys.modules["trade_genius"]` so `engine.scan._tg()` resolves to record-only state; the prod path is untouched. Ships a minimal CI fixture (`tests/fixtures/replay_v511_minimal/`, AAPL+QQQ first 60 RTH + 10 pre-market 1m bars from 2026-04-28) and a regression test (`tests/test_replay_v511_engine_seam.py`) that asserts the engine seam was actually exercised (≥1 tick, ≥1 `fetch_1min_bars` call, no driver-side exceptions). The workspace `replay_v510_full_v4.py` becomes deprecated; full removal is a v5.12.0 cleanup item. Zero changes to `trade_genius.py` or `engine/*` modules — golden harness remains byte-equal.

---

## v5.10.7 — 2026-04-28 — QBTS removed from default universe

- Removed QBTS from `TICKERS_DEFAULT` in trade_genius.py. QBTS is not a Titan and was carried in the default list as a convenience; users who want to track it can still `/ticker add QBTS` at runtime. Aligns the default deployed universe with the 10 Titans + SPY + QQQ anchors.
- Updated stale QBTS comments in trade_genius.py to reflect the new default.

---

## v5.10.6 — 2026-04-28 — Eye-of-the-Tiger closeout (dashboard panel + legacy cleanup + backtest + strategy)

Final v5.10.x patch. Closes out everything deferred from v5.10.1 / v5.10.4 / v5.10.5.

**Item 1 — Dashboard /api/state v5.10 panel.** `dashboard_server.snapshot()` now carries three new top-level keys: `section_i_permit` (QQQ Market Shield + Sovereign Anchor permit lights for both rails), `per_ticker_v510` (Volume Bucket + Boundary Hold gate state per trade ticker), and `per_position_v510` (phase, Sovereign Brake distance, Entry-2 fired flag per open position keyed by `"TICKER:SIDE"`). Per-position rows under `positions[]` also carry `phase`, `sovereign_brake_distance_dollars`, and `entry_2_fired`. Frontend (`dashboard_static/app.js` + `app.css`) renders an "Eye of the Tiger · live gates" card with Section I pills, per-ticker grid, and a Phase badge + SB-Δ column on every open-position row. Implementation lives in `v5_10_6_snapshot.py`. Test contract pinned at `tests/test_api_state_v510_payload.py`.

**Item 2 — Legacy emitter cleanup (real this time).** Six v5.1.x – v5.9.x emitters that survived the v5.10.5 audit are now deleted: `_v519_arm_rehunt_watch`, `_v519_check_rehunt`, `_v519_check_oomph`, `_v512_emit_candidate_log`, `_v590_log_abort_if_flip`, `_v570_log_strike` (plus support state). All six had zero live call sites. Test blocks were removed from `smoke_test.py` and `test_v5_5_6_shadow_uses_prev_bucket.py`. `REHUNT_VOL_CONFIRM` / `OOMPH_ALERT` survive only as shadow-config registry names so persisted shadow positions still close cleanly. 477 lines removed from trade_genius.py.

**Item 3 — Full-algorithm backtest replay.** New `backtest_v510/replay_v510_full.py` reproduces the v5.10 six-section pipeline against archived 1-minute bars and emits `backtest_v510/replay_v510_full_report.md` with per-day P&L plus Section I–V invocation counts. Guard rails fail the run when a single session loses more than $5,000 or aggregate loss exceeds $10,000. Tested in `tests/test_replay_v510_full.py`.

**Item 4 — STRATEGY.md regenerated.** STRATEGY.md gains a "v5.10 Eye of the Tiger" section that supersedes the v5.0 two-stage state machine for `BOT_VERSION >= 5.10.0`. The v5.0 spec is preserved as the historical reference. **PDF note:** `scripts/build_algo_pdf.py` reads `ARCHITECTURE.md`, not STRATEGY.md, so no PDF is regenerated in this release. Operators who want a fresh PDF should refresh ARCHITECTURE.md to mirror the v5.10 spec and then re-run the script.

---

## v5.10.5 — 2026-04-28 — Phase B/C Triple-Lock wiring + legacy-emitter audit

**Item 1 — Phase B/C stops wired into the live hot path (Section V — Triple-Lock).** The pure helpers `step_two_bar_lock_on_5m`, `step_phase_c_if_eligible`, and `evaluate_phase_c_exit` shipped in v5.10.0/v5.10.1 (and unit-tested) but were never called from `manage_positions` / `manage_short_positions`. v5.10.5 wires them in.

A new helper `_v5105_phase_machine_tick(ticker, side, pos, bars)` runs once per open position per scan tick, after the Section IV overrides (Sovereign Brake / Velocity Fuse) and before the existing ladder/Maffei block. It:

- Lazy-initialises the v5.10 phase state on first sight (`init_position_state_on_entry_1`) and snaps Entry-2 from the dict's `avg_entry` if scaling has already happened.
- Computes closed 5m OHLC + EMA9 from the existing `fetch_1min_bars` payload (per-ticker, no extra HTTP round-trip).
- On every NEW closed 5m bucket, advances the Two-Bar Lock counter via `step_two_bar_lock_on_5m`.
- Promotes to Phase C (`step_phase_c_if_eligible`) once the lock has fired and the 5m EMA9 is seeded (≥ 9 closed 5m bars since 9:30 ET).
- Mirrors the v5.10 phase onto a simple `pos["phase"]` field (`'A' | 'B' | 'C'`) for /api/state visibility.
- Fires `be_stop` (Phase B break-even) when `pos["phase"] == "B"` and current price has crossed the locked level (`current_stop` ← `avg_entry`).
- Fires `ema_trail` (Phase C "Leash") when `pos["phase"] == "C"` and `evaluate_phase_c_exit` returns True on the most recent closed 5m close.

Phase A (forensic_stop / Maffei) intentionally continues to flow through the existing ladder / STOP / TRAIL / RED_CANDLE / POLARITY_SHIFT plumbing — this PR does not replace it, since `evaluate_phase_a_exit` is not defined in `eye_of_tiger.py`.

`close_breakout` now calls `eot_glue.clear_position_state(...)` and drops the `(ticker, side)` entry from the new `_v5105_last_5m_bucket` debounce dict, so a re-entry after a flat starts the phase machine clean (Phase A, counter 0).

Canonical exit_reason strings: `forensic_stop` (existing Phase A path unchanged in this release), `be_stop` (new), `ema_trail` (new). Section IV (`sovereign_brake`, `velocity_fuse`), Section VI (`daily_circuit_breaker`, `eod`), and the dashboard's `TRAIL` / `STOP` / `RED_CANDLE` / `POLARITY_SHIFT` attribution are all preserved unchanged.

New log lines:

- `[V5100-PHASE]` — phase transitions (existing — emitted from `eot_glue`).
- `[V5100-PHASE-B-BE]` — Phase B break-even exit fires.
- `[V5100-PHASE-C-EMA-TRAIL]` — Phase C "Leash" exit fires.

**Item 2 — Legacy-emitter audit (cleanup deferred).** The six legacy emitters listed for deletion (`_v519_arm_rehunt_watch`, `_v519_check_rehunt`, `_v519_check_oomph`, `_v512_emit_candidate_log`, `_v570_log_strike`, `_v590_log_abort_if_flip`) have no live call sites in `trade_genius.py` after v5.10.3 — but `smoke_test.py` (the post-deploy CI gate) and `test_v5_5_6_shadow_uses_prev_bucket.py` still exercise four of them directly. Per the v5.10.5 spec ("If anything still calls them, document and skip that one — don't break callers"), deletion is deferred to a follow-up PR that will retire the corresponding test cases in the same change. No `exit_reason` switch/dispatch in `trade_genius.py` or `dashboard_static/app.js` references the legacy strings (`LORDS_LEFT`, `BULL_VACUUM`, `HARD_EJECT_TIGER`, `V572-ABORT`, `V510-CAND`, `V570`, `REHUNT_VOL_CONFIRM`); only docstrings, log-line tags, and shadow-config metadata reference them, all of which are fine to leave in place.

**Hard rules preserved.** `#h-tick` is not hidden, the health-pill count is not dropped, and the universe is unchanged: AAPL, AMZN, AVGO, GOOG, META, MSFT, NFLX, NVDA, ORCL, TSLA + QBTS, QQQ, SPY.

---

## v5.10.4 — 2026-04-28 — Docker-build-and-boot CI gate + Entry 2 scaling (Section III)

**Two intentionally narrow changes; nothing else in this PR.**

**1. Docker-build-and-boot CI gate.** Adds a new `docker-boot` job in `.github/workflows/docker-boot.yml` that runs on every PR. It `docker build`s the image, runs the container with `SSM_SMOKE_TEST=1` and `DASHBOARD_PASSWORD=ci-smoke-test-pw`, polls `http://localhost:8080/api/version` for up to 30s, and fails the PR if the endpoint does not return a 200 with valid JSON. On failure, it captures `docker logs` and uploads them as an artifact. This catches the exact regression class that crash-looped v5.10.1 — a top-level import error inside the container — *before* it can merge, instead of relying on the post-deploy smoke job that fires after main is already on Railway.

To make `SSM_SMOKE_TEST=1` actually keep the container alive long enough to be polled, the existing smoke-test guard at the bottom of `trade_genius.py` now blocks the main thread on a sleep loop when invoked as `__main__`. Pytest imports of the module (which set `SSM_SMOKE_TEST=1` and rely on the import returning promptly) are unaffected — the block is gated on `__name__ == "__main__"`.

**2. Entry 2 scaling (Section III).** Wires the `evaluate_entry_2_decision` / `record_entry_2` helpers from `v5_10_1_integration.py` (already unit-tested, dormant since v5.10.1) into the live hot path. Per the canonical Eye-of-the-Tiger spec (§III, rev c):

- Entry 2 fires on a **1m DI cross > 30** (`di_1m_prev <= 30 and di_1m_now > 30`) plus a **fresh NHOD/NLOD** that extends past Entry 1's running HWM (long) or LWM (short), with `now_ts > entry_1_ts`.
- Section II gates (Volume Bucket, Boundary Hold) are NOT re-applied — Entry 2 is intentionally a momentum scale-in.
- Section I (global permit) IS re-evaluated fresh at the trigger (per spec §XIV.3); we do not cache the Entry-1 permit decision.
- Sized at 50% of the original Entry 1 share count (min 1). Long Entry 2 debits cash; short Entry 2 credits cash. The position's `entry_price` is updated to the share-weighted average of Entry 1 + Entry 2, and `shares` grows accordingly so all downstream stop / trail / P&L math sees the combined book.

Wired in `_v5104_maybe_fire_entry_2`, called from `check_breakout` whenever a position already exists for `(ticker, side)` and `v5104_entry2_fired` is False. Emits `[V5100-ENTRY] entry_num=2 di_1m=… fresh_extreme=… fill_price=… shares=… new_avg=…`.

**Out of scope (separate PRs in flight).** Phase B/C, dashboard updates, backtest. Do not let v5.10.4 grow.

---

## v5.10.3 — 2026-04-28 — Re-ship Eye-of-the-Tiger integration with boot-hang fix + startup smoke test

**Re-ships PR #189 (the v5.10.1 live-hot-path integration of the Eye-of-the-Tiger evaluators) on top of v5.10.2's revert, with the actual root cause of the Railway boot regression fixed and a startup smoke test added so it cannot happen again.**

**Root cause.** The "indefinite ReadTimeout on `/api/version`" was not a hang at all — Railway's pre-revert logs (deployment `bf1f917e-ce11-4a1a-bd3a-2c142a9adb25`) show the container crash-looping at module-import time:

```
File "/app/trade_genius.py", line 46, in <module>
    import eye_of_tiger as eot  # noqa: E402
ModuleNotFoundError: No module named 'eye_of_tiger'
```

The `Dockerfile` uses explicit per-file `COPY` directives. v5.10.0 added `eye_of_tiger.py` and `volume_bucket.py` to the repo but never imported them at module load (only the test suite touched them), so v5.10.0 booted fine. v5.10.1 added `import eye_of_tiger as eot` and `import v5_10_1_integration as eot_glue` at the top of `trade_genius.py` but did NOT update `Dockerfile`'s `COPY` block, so the production image was missing all three modules. The container's `restartPolicyType: ON_FAILURE` (max 5 retries) cycled through five immediate ImportError exits and then idled, which is what the post-deploy poller saw as `502` followed by `ReadTimeout` on `/api/version`. Local CI / preflight passed because every `.py` file is on disk locally regardless of what the Dockerfile copies.

**The fix.** `Dockerfile` now contains:

```
COPY eye_of_tiger.py .
COPY volume_bucket.py .
COPY v5_10_1_integration.py .
```

That's it. The `_QQQ_REGIME` initial-state, `/data/bars` archive read, and `fetch_1min_bars` first-call audit lines from the v5.10.2 CHANGELOG were red herrings — those code paths are exception-guarded and tolerant of unseeded state. The container never got far enough to reach any of them.

**Startup smoke test.** `tests/test_startup_smoke.py` wires four guards into `pytest`, each runs in < 1s:

1. `test_trade_genius_imports_clean_with_smoke_env` \u2014 imports `trade_genius` with `SSM_SMOKE_TEST=1` and asserts `BOT_VERSION` is set. A missing import surfaces here as `ImportError`.
2. `test_dockerfile_copies_every_top_level_python_module` \u2014 strict structural guard. Parses the AST of `trade_genius.py` for every `import X` / `from X import ...` whose target is a sibling `.py` file, and the `COPY *.py` directives in `Dockerfile`, and fails if any imported module is missing a `COPY`. This is the regression check that would have caught v5.10.1.
3. `test_eye_of_tiger_modules_are_present_in_dockerfile` \u2014 belt-and-suspenders explicit assertion for the three v5.10.1 modules.
4. `test_scan_loop_no_blocking_at_first_call_with_empty_state` \u2014 calls every v5.10.1 orchestrator entry point with `None`/empty state (unseeded `_QQQ_REGIME`, no `/data/bars`, no streamed bars) and asserts no exception is raised.

Verified by stashing the `Dockerfile` fix and re-running pytest: tests 2 and 3 fail loudly with the specific module name. With the fix applied: 4/4 pass.

`scripts/preflight.sh` now invokes the smoke test as its dedicated step `[2/6]` so a regression in this contract surfaces with a clear PASS/FAIL label.

**Algorithm content.** Identical to the v5.10.1 PR #189 cherry-pick. Section I/II/III gates in `check_breakout`, Section IV overrides at the top of `manage_positions`/`manage_short_positions`, `_tiger_hard_eject_check` retired, 15-min cooldown removed, per-ticker $50 loss cap removed. 102/102 unit tests passing. See the v5.10.1 entry below for full surface details.

**References.** PR #189 (original integration; reverted), PR #190 (revert), PR #191 (this).

---

---

## v5.10.2 — 2026-04-28 — Revert v5.10.1 (Railway deploy regression)

**Reverts PR #189 (`78877b3`).** v5.10.1 merged green (CI + preflight passed) but the Railway deploy never came up: the post-deploy smoke poll observed v5.10.0 → 502 → indefinite read-timeout, and `/api/version` continued to 502 well past the 5-minute deploy budget. Root cause not yet identified — the code imports cleanly locally and 102 unit tests pass, so the failure is something exposed only by the Railway boot path (likely an import-time or first-cycle interaction with `/data/bars`, `_QQQ_REGIME` initial state, or fetch_1min_bars on the live tick path).

This revert restores main to the v5.10.0 algorithm-evaluator + legacy hot path state from `aa0fcd7`. The bot continues to run the legacy v5.0–v5.9 Tiger/Buffalo state machine with the daily-loss-limit (-$1500) and EOD time (15:59:50) updates from v5.10.0 still in place.

---

## v5.10.1 — 2026-04-28 — Eye-of-the-Tiger live-hot-path integration

**Wires the v5.10.0 pure-function evaluators (`eye_of_tiger.py`, `volume_bucket.py`) into `trade_genius.py`'s scan loop.** v5.10.0 shipped the building blocks but left the legacy v5.0–v5.9 Tiger/Buffalo state machine on the hot path; v5.10.1 makes the v5.10.0 evaluators authoritative.

**New module.** `v5_10_1_integration.py` — orchestrator that owns Volume Bucket lifecycle (lazy load + 9:29 ET refresh), Section I/II/III/IV evaluation, per-(ticker, side) Boundary Hold + di_1m_prev caches, and the v5.10.0 `[V5100-*]` log signatures.

**`trade_genius.py` surgery.**
- `scan_loop` calls `refresh_volume_baseline_if_needed(now_et)` once per cycle and `maybe_log_permit_state(...)` (emits `[V5100-PERMIT]` only on state change).
- Per-ticker scan caches the just-closed 1m bar via `eot_glue.record_1m_close(ticker, close)` for the Boundary Hold evaluator.
- `check_breakout` Section I + II + III gates replace the v5.6.0–v5.9.4 G1/G3/G4 + V570 expansion + DI gate + extension + stop-cap block. Entry 1 fires on `[V5100-ENTRY] entry_num=1` only when Section I OPEN + Volume Bucket PASS/COLDSTART + Boundary Hold + DI(15) 5m & 1m > 25 + NHOD/NLOD all align.
- Section IV (Sovereign Brake -$500 unrealized + Velocity Fuse > 1.0% from current 1m candle open) now runs at the top of `manage_positions` and `manage_short_positions`, emitting `[V5100-SOVEREIGN-BRAKE]` / `[V5100-VELOCITY-FUSE]` and routing to `close_position` / `close_short_position` with canonical `exit_reason` strings.
- 15-minute per-ticker cooldown removed (Section VI Unlimited Hunting); per-ticker $50 loss cap removed (replaced by Section IV per-trade Sovereign Brake at -$500).
- `_tiger_hard_eject_check` definition deleted. `tests/test_titan_bypass_hard_eject.py` retired.

**Smoke tags.** `scripts/post_deploy_smoke.sh` now greps for `[V5100-PERMIT]`, `[V5100-VOLBUCKET]`, `[V5100-BOUNDARY]`, `[V5100-ENTRY]`.

**Tests.** 102/102 passing (108 baseline minus 6 deleted Titan-bypass tests covering the removed `_tiger_hard_eject_check`). REVERTED in v5.10.2 — Railway boot regression. Re-shipped as v5.10.3 with boot-hang fix.

**Rollback.** No feature flag. Rollback path is revert-the-PR + redeploy.

---

## v5.10.0 — 2026-04-28 — Project Eye of the Tiger (full algorithm rewrite, with volume bucket)

**Full algorithmic rewrite per Gene Stepanov's authoritative spec (revised 2026-04-28 12:38 PM CDT, third revision today).** No feature flag. v5.6.0 → v5.9.4 entry/exit logic is replaced.

Canonical truth source: [`specs/canonical/eye_of_the_tiger_gene_2026-04-28c.md`](specs/canonical/eye_of_the_tiger_gene_2026-04-28c.md). Implementation spec: [`specs/v5_10_0_eye_of_the_tiger.md`](specs/v5_10_0_eye_of_the_tiger.md).

**The three new pieces in revision c (vs the 28b planning).**

1. **Institutional Oomph (Volume Bucket) — Section II.1, Entry-1 gate.** Current 1m volume of the target ticker must be ≥ 100% of its 55-trading-day rolling average for that minute-of-day (HH:MM ET, RTH only). Data source: `/data/bars` archive that v5.5.x's `bar_archive.py` writes. Baseline is computed on bot startup, cached in memory, and refreshed once per session at 9:29 ET. **Cold-start (< 55 days history): pass-through with rate-limited warning log** per Val 28c decision — strict blocking would prevent virtually all entries until ~July 2026 given the recent bar-archive wiring. Volume Bucket is explicitly an Entry-1-only gate per Gene's note ("Entry 2 does NOT require... the volume bucket fill").

2. **Boundary Hold (Entry-1 gate).** Two consecutive closed 1m candles strictly outside the 5m Opening Range boundary (close > OR_High for LONG, close < OR_Low for SHORT). OR window is [9:30:00 ET, 9:34:59.999 ET]. Earliest possible Entry-1 satisfaction is 9:36:00 ET. Stateless: re-evaluated against the most recent two closed 1m candles on every 1m close; a single close at/inside the boundary breaks the hold and the next two consecutive outside closes re-arm it. Also Entry-1-only.

3. **Entry 2 requires FRESH NHOD/NLOD.** Entry 2 fires when 1m DI+/-(15) **crosses** > 30 (edge transition from `<= 30` on prior tick to `> 30` on current tick) **AND** price prints a fresh session-extreme strictly after `entry_1_ts` and beyond the high-water-mark recorded at Entry 1. Entry 2 fires at most once per Entry-1 lifecycle. Section II permits are NOT re-checked at Entry 2 (per Gene). Conservative interpretation: Section I (Global Permit) must still be OPEN at the moment of Entry 2 trigger to authorize the new add — flagged as an open question for Val/Gene.

**Six-section structure.**

- **I. Global Permit (Index Shield).** QQQ 5m close vs 9-EMA + QQQ current price vs 9:30 AVWAP. Both must align with the side. Mid-trade flip is observational only — does NOT force exit (Section V owns exit authority).
- **II. Ticker-Specific Permits (Entry-1 gates).** Volume Bucket + Boundary Hold (both above).
- **III. Entry & Sizing (Scaled 50/50).** Entry 1 (50%): Section I OPEN + Section II both gates + 5m DI(15) > 25 + 1m DI(15) > 25 + price printing NHOD/NLOD on the same tick. Entry 2 (additional 50%): the crossing-edge + fresh-extreme rule above.
- **IV. High-Priority Overrides.** Sovereign Brake (per-trade -$500, immediate market exit) + Velocity Fuse (Flash Crash Protection: > 1.0% adverse move from current 1m candle open, strict). Both fire regardless of phase.
- **V. Stop-Loss Hierarchy (Triple-Lock).** Phase A — Maffei 1-2-3 Recursive Gate: fires when current 1m candle closes back **INSIDE** the OR (Gene's wording — corrects the v5.9.0 spec drift). Audit: LONG exits if low < prior low; SHORT exits if high > prior high. Phase B step 1 — Layered Shield (on Entry 2): first 50% gets BE stop at Entry 1 price; Maffei deactivates for first 50%. Phase B step 2 — Two-Bar Lock (after 2 consecutive favorable 5m closes post-Entry-2): entire 100% stop = avg_entry. Phase C — The Leash: 5m close on the wrong side of the 5m 9-EMA exits 100% of position.
- **VI. Systematic Machine Rules.** Unlimited Hunting (no per-session entry cap; bot strikes every time NHOD/NLOD + DMI + Section I + Section II align), Daily Circuit Breaker at -$1,500 cumulative realized loss (raised from v5.9.x's -$500), EOD Flush at 15:59:50 ET (was 15:55).

**`exit_reason` enum.** `sovereign_brake`, `velocity_fuse`, `forensic_stop`, `be_stop`, `ema_trail`, `daily_circuit_breaker`, `eod`, `manual`. All legacy strings dropped.

**Locked defaults (Section IX, no feature flag).** See `eye_of_tiger.py` constants. `DAILY_LOSS_LIMIT_DOLLARS` raised from -500.0 to -1500.0. EOD scheduler tick moved from 15:55 to 15:59. `_tiger_hard_eject_check` call site retired (function is dead code, kept for grep audit).

**Cold-start caveat for backtests.** The `/data/bars` archive started accumulating in v5.5.x and currently has only ~5 trading days of history. For any backtest run before mid-July 2026, the Volume Bucket gate will be in cold-start (PASS-THROUGH) for nearly every (ticker, day) cell and will not materially constrain entries. Reviewers should read backtest results with this in mind: the gate is wired and tested, but its enforcement teeth come in once the archive ramps. Transition from pass-through to enforced is automatic; no code change needed.

**New modules.** `eye_of_tiger.py` (pure decision functions for Sections I-VI, over plain dicts, no I/O, directly testable) and `volume_bucket.py` (`VolumeBucketBaseline` class, lazy-loaded, refresh-once-per-session at 9:29 ET).

**Tests.** New unit suites at `tests/test_eye_of_tiger.py` and `tests/test_volume_bucket.py` cover all six sections including the three new pieces (Volume Bucket post-cold-start PASS, FAIL at 99%, COLDSTART pass-through with rate-limited log, Entry-2 exclusion; Boundary Hold single-close insufficient, equality break, SHORT mirror, 9:36 earliest-satisfaction timing, Entry-2 exclusion; Entry 2 crossing edge + fresh NHOD pass, no fresh NHOD fail, sustained DI without crossing edge fail, never reaches 30, fires only once).

**Rollback.** No feature flag. Rollback path is revert-the-PR + redeploy.

Touched (high-level): `eye_of_tiger.py` (NEW), `volume_bucket.py` (NEW), `trade_genius.py` (BOT_VERSION, DAILY_LOSS_LIMIT, EOD scheduler tick, retire `_tiger_hard_eject_check` call site), `bot_version.py` (5.9.4 → 5.10.0), `CHANGELOG.md` (this entry), `ARCHITECTURE.md` (six-section rewrite), `tests/test_eye_of_tiger.py` (NEW), `tests/test_volume_bucket.py` (NEW), `dashboard_server.py` (REASON_LABELS exit_reason enum updates).

---

## v5.9.4 — 2026-04-28 — Hotfix — wire Titan bypass into `_tiger_hard_eject_check`

**Hotfix. Partial fix.** Stops today's bleeding. Full FSM wiring is still deferred to v5.10.0.

**Trigger.** This morning (2026-04-28), `HARD_EJECT_TIGER` fired against Titan tickers MSFT and TSLA a combined five times — MSFT 09:23 / 09:24 CDT, TSLA 09:16 CDT, plus MSFT trade #4 at 15:25 UTC under the current SUCCESS deployment, and a TSLA companion. All five fires happened because the Titan-list bypass that v5.7.1 promised for the DI<25 hard-eject was specced, library-coded, unit-tested, log-emitter-named, and CHANGELOG'd, but the call site in `trade_genius.py:_tiger_hard_eject_check` was never edited. The function iterated every open position with no Titan filter, so MSFT's DI+ touching 23.55 below `TIGER_V2_DI_THRESHOLD=25` flushed the position straight through the legacy gate.

**Root cause (v5.7.1 implementation gap).** v5.7.1 (`f000fb8`) shipped the `tiger_buffalo_v5.evaluate_exit(..., is_titan=True)` DI<25 bypass on the library side, the `tiger_buffalo_v5.evaluate_titan_exit(...)` three-phase FSM (forensic_stop / per_trade_brake / be_stop / ema_trail / velocity_fuse), the `ENABLE_BISON_BUFFALO_EXITS` feature flag (default True), the `_v571_log_exit_phase` / `_v571_log_velocity_fuse` / `_v571_log_ema_seed` emitters in `trade_genius.py`, 19+ unit tests in `smoke_test.py` plus the `tests/test_forensic_stop.py` suite, and `ARCHITECTURE.md §23` documenting the wiring as live — but ZERO new call sites in `trade_genius.py`. Every helper above has only its definition, no callers. `_tiger_hard_eject_check` was untouched. The shipped tests pass against the library; production never invoked any of it.

**What this PR does.** Four-line guard at the top of each loop body in `_tiger_hard_eject_check` (one for longs, one for shorts):

```python
if _v570_is_titan(ticker) and ENABLE_BISON_BUFFALO_EXITS:
    logger.info("[TITAN-BYPASS] HARD_EJECT_TIGER skipped for %s long (Titan; FSM authority)", ticker)
    continue
```

Plus a unit test (`tests/test_titan_bypass_hard_eject.py`) that confirms a Titan ticker (MSFT) with DI+ < threshold is **not** flushed by `_tiger_hard_eject_check`, while a non-Titan (PLTR) with the same DI+ profile **is**. The test exercises the live `trade_genius._tiger_hard_eject_check` function — not the library — so a future regression that re-removes the guard is caught.

**What this PR does NOT do.** It does not wire `evaluate_titan_exit` into the scan loop. It does not seed the 5m EMA at 10:15 ET. It does not initialize `init_titan_exit_state` from `close_breakout`. It does not route forensic_stop / per_trade_brake / be_stop / ema_trail / velocity_fuse return reasons into `close_position` / `close_short_position`. The `_v571_log_*` emitters remain orphaned. Until v5.10.0 ships, **Titans exit ONLY via**: structural stop, trail stop, EOD, daily loss limit. `forensic_stop` / `velocity_fuse` / `per_trade_brake` / `be_stop` / `ema_trail` are still dead code.

**Behavior change.** For Titan tickers (AAPL, AMZN, AVGO, GOOG, META, MSFT, NFLX, NVDA, ORCL, TSLA), `HARD_EJECT_TIGER` no longer fires. New log line `[TITAN-BYPASS] HARD_EJECT_TIGER skipped for <TICKER> <side> (Titan; FSM authority)` is emitted in Railway logs each cycle a Titan position is held below DI threshold — a diagnosable smoking-gun for v5.10.0 work and a confirmation the guard is wired. Non-Titan behavior is unchanged.

Touched: `trade_genius.py` (`_tiger_hard_eject_check` body — long loop + short loop guard + comment + `BOT_VERSION` → 5.9.4); `bot_version.py` → 5.9.4; `tests/test_titan_bypass_hard_eject.py` (new); `CHANGELOG.md`.

---

## v5.9.3 — 2026-04-28 — Eradicate `LORDS_LEFT` / `BULL_VACUUM` stragglers

Pure cleanup. **No new behavior.** Closes out the three-step retirement of the PDC-based Sovereign Regime Shield: v5.9.1 removed the helper and call sites, v5.9.2 stripped the dual-PDC half of `HARD_EJECT_TIGER`, and v5.9.3 eradicates the residue that was left as "harmless legacy" in v5.9.1 but kept the retired vocabulary alive in user-facing surfaces.

**Trigger.** Four trades with `reason=LORDS_LEFT` (MSFT 08:50 / 09:06 CDT, plus two TSLA companions) appeared in this morning's paper-trade output despite `/api/state` correctly reporting `version: 5.9.2` and `regime.sovereign.status: DISARMED`. Audit of every `close_position` / `close_short_position` call site in `trade_genius.py` confirmed there is **no live `LORDS_LEFT` / `BULL_VACUUM` emission path anywhere in the bot** — every reason argument is one of `STOP`, `TRAIL`, `RED_CANDLE`, `POLARITY_SHIFT`, `RETRO_CAP`, `EOD`, or `HARD_EJECT_TIGER`. The morning trades were therefore residue: stale stringly-typed labels carried over from a pre-v5.9.1 row in the persistent trade log, surfaced through `REASON_LABELS` lookup at display time. The fix is to drop the labels themselves so a stray legacy token cannot round-trip through the dashboard / Telegram exit message and look like a live rule.

**Removed.**
- `REASON_LABELS["LORDS_LEFT"]`, `["LORDS_LEFT[1m]"]`, `["LORDS_LEFT[5m]"]`, `["BULL_VACUUM"]`, `["BULL_VACUUM[1m]"]`, `["BULL_VACUUM[5m]"]` — all six legacy-label entries dropped from `trade_genius.py`. Any historical trade-log row that still carries those raw reasons now renders as the raw token rather than a pretty Telegram label, which is the desired behavior post-retirement.
- `_SHORT_REASON` 👑 (Lords Left) and 🌀 (Bull Vacuum) entries dropped — `/dayreport` compact display no longer claims those rules exist.
- `trade_genius.py` v5.9.1 retirement-history comment block (lines 7725-7738) collapsed to a single self-contained note that points at v5.9.3 as the source of truth.
- `trade_genius.py` persistent-trade-log schema comment (lines 6482-6485) now lists the active reason vocabulary (`RED_CANDLE`, `POLARITY_SHIFT`, `HARD_EJECT_TIGER`, `forensic_stop`, `per_trade_brake`, `be_stop`, `ema_trail`, `velocity_fuse`) instead of the retired `BULL_VACUUM` / `LORDS_LEFT` tokens.
- `telegram_commands.py` `/strategy` help text — the SOVEREIGN REGIME SHIELD block (which still described "Lords Left & Bull Vacuum require BOTH SPY AND QQQ to cross PDC") rewritten to describe the v5.9.0 entry-side EMA compass + the v5.9.2 DI-only Tiger, and the long/short Eye of the Tiger exit summaries no longer claim Lords Left / Bull Vacuum are live exits.
- Stale synthetic-harness goldens (`loop_full_cycle.json`, `loop_trail_promotion.json`, `edge_trail_promotion_threshold.json`, plus 8 others that drifted on adjacent fields) re-recorded against the v5.9.3 emission vocabulary.

**Strengthened.** `smoke_test.py` C-R6 now additionally asserts that `REASON_LABELS` contains no key starting with `LORDS_LEFT` or `BULL_VACUUM` — so a future revert that re-adds the labels alongside re-adding the helper (or re-adds them alone, as v5.9.1 did) is caught at smoke time.

**Kept.** `ARCHITECTURE.md` C-R6 row, the `dashboard_server.py` `_sovereign_regime_snapshot` docstring, and the inline retirement-history comment in `trade_genius.py` all still reference the retired token names — they document the retirement path so a future contributor can grep for `LORDS_LEFT` and find the audit trail. The `🛡 INDEX REGIME` block in `/strategy` references the v5.9.0 → v5.9.3 timeline by version number for the same reason.

**Verified.** `pytest tests/` (43 pass), `python -m synthetic_harness replay` (50 pass), and `grep -rn "LORDS_LEFT\|BULL_VACUUM"` returns only intentional historical/audit references after this PR.

Touched: `trade_genius.py` (REASON_LABELS / _SHORT_REASON / two comment blocks / `BOT_VERSION`); `bot_version.py` → 5.9.3; `telegram_commands.py` (3 `/strategy` text blocks); `smoke_test.py` (C-R6 strengthened); `ARCHITECTURE.md` (C-R6 row, signal-bus reason comment); 11 `synthetic_harness/goldens/*.json` re-recorded; `CHANGELOG.md`. Algo PDF unchanged (cleanup only).

---

## v5.9.2 — 2026-04-28 — Retire dual-PDC half of `HARD_EJECT_TIGER` (DI-only)

Pure removal. **No new behavior.** Completes the dual-PDC eject retirement that v5.9.1 started. After this release no exit rule in the bot consumes SPY/QQQ vs PDC index-regime checks — entry and exit both look at the v5.9.0 5m EMA compass for index regime, and per-ticker PDC rules (RED_CANDLE / POLARITY_SHIFT) are unchanged.

**Removed.** The dual-index PDC trigger inside `_tiger_hard_eject_check()` in `trade_genius.py`. The function previously OR-ed two triggers — DI weakness (`DI < TIGER_V2_DI_THRESHOLD`) and the index flip (`SPY_cur < SPY_PDC AND QQQ_cur < QQQ_PDC` for longs, mirror for shorts). The index-flip half is gone, along with its `spy_bars` / `qqq_bars` / `spy_pdc_v` / `qqq_pdc_v` lookups and the `index_flip_down` / `index_flip_up` flags. The `idx_flip=…` field is dropped from the `HARD_EJECT_TIGER` log line.

**Kept (deliberately).** The DI-decay trigger itself — Tiger now fires on `DI+ < TIGER_V2_DI_THRESHOLD` (long) / `DI- < TIGER_V2_DI_THRESHOLD` (short) only. `TIGER_V2_DI_THRESHOLD` constant unchanged. `REHUNT_VOL_CONFIRM` arming after eject (orthogonal feature) unchanged. v5.9.0's QQQ Regime Shield (5m EMA compass at G1) and forensic_stop / per_trade_brake unchanged. Per-ticker pos_pdc / prev_close logic, the `pdc` dict and its population, dashboard PDC pills, and the `[V510-IDX]` observational logger all remain — `_tiger_hard_eject_check` was the last algo consumer that gated on dual-PDC, but other code (display, observational shadow) still reads the dict.

Touched: `trade_genius.py` (function body + docstring + comment block + scan-loop comment + `BOT_VERSION`); `bot_version.py` → 5.9.2; `CHANGELOG.md`, `ARCHITECTURE.md`. Algo PDF unchanged (no algorithm-text update; this is a removal/cleanup).

---

## v5.9.1 — 2026-04-28 — Retire PDC-based Sovereign Regime Eject (exit-side cleanup)

Pure removal. **No new behavior.** Pairs with v5.9.0's G1 swap from AVWAP/PDC to a 5m EMA compass — the exit side now uses the same regime philosophy as the entry side instead of running a parallel PDC-based rule.

**Removed.** `_sovereign_regime_eject(side)` (the dual-index PDC eject) and its only consumer/helper `_last_finalized_1min_close(ticker)`. Both `lords_left` (long-side) and `bull_vacuum` (short-side) callers in `manage_positions` / `manage_short_positions` are gone, along with their downstream `LORDS_LEFT` / `BULL_VACUUM` exit triggers. Dashboard `long_eject` / `short_eject` payload fields and the corresponding eject tiles in `dashboard_static/app.js` are removed; the SPY/QQQ vs PDC pills remain as cosmetic display only. Smoke C-R6 was inverted to assert the helper is *gone* so a future revert is caught.

**Kept (deliberately).** v5.9.0's QQQ Regime Shield (5m EMA compass at G1) and forensic_stop / per_trade_brake exit logic. Per-ticker `pos_pdc` / `prev_close` references in the long-side RED_CANDLE check, the short-side POLARITY_SHIFT check, the `pdc` dict population, the `[V510-IDX]` shadow logger, and the cosmetic dashboard PDC pills are all per-ticker rules unrelated to the SPY/QQQ index eject. `LORDS_LEFT` / `BULL_VACUUM` entries in `REASON_LABELS` are retained as legacy labels so persistent trade-log rows from prior versions still render their human-readable reason.

Touched: `trade_genius.py` (function + comment block + callers + REASON_LABELS comments + regime-alert comment); `dashboard_server.py` (`_sovereign_regime_snapshot` rewritten — no eject booleans, direct `fetch_1min_bars` call replaces the now-removed helper); `dashboard_static/app.js` (eject tiles removed, both regime cards); `smoke_test.py` (C-R6 assertion inverted); `bot_version.py` → 5.9.1; `CHANGELOG.md`, `ARCHITECTURE.md`. Algo PDF unchanged (no algorithm-text update; this is a removal/cleanup).

---

## v5.9.0 — 2026-04-28 — QQQ Regime Shield + Recursive Forensic Stop

Two-part algo release.

**Part 1: QQQ Regime Shield (Permission Gate G1).** Replaces the v5.6.0 `QQQ.last vs QQQ.opening_avwap` AVWAP penny-switch with a structural `QQQ 5m EMA(3) vs EMA(9)` cross. The compass (UP / DOWN / FLAT) is the new G1 source for both LONG and SHORT permission paths; G3 (ticker AVWAP) and G4 (OR breakout) are unchanged. EMAs are seeded pre-market (04:00–09:30 ET) with priority `bar archive → Alpaca historical IEX → prior session` so G1 has a meaningful compass at the 09:35 OR boundary instead of warming up live. New `qqq_regime.py` module owns the EMA state. New log tags: `[V572-REGIME]` (per closed 5m bar), `[V572-REGIME-SEED]` (one line at first compass eval), `[V572-ABORT]` (re-strike blocked because mid-strike compass flipped). Re-entry block is the only gate that fires — already-open trades let their existing exit FSM run.

**Part 2: Recursive Forensic Stop + per-trade Sovereign Brake (Phase A).** Retires `hard_stop_2c` (two consecutive 1m closes outside OR). Replaces it with the Maffei 1-2-3 audit: every 1m close that gates outside OR audits `low(current) vs low(prior)` (LONG) / `high(current) vs high(prior)` (SHORT). Lower-low / higher-high → EXIT (`forensic_stop`); equality or higher-low / lower-high → STAY (consolidation / wick). Adds a per-trade Sovereign Brake at -$500 unrealized P&L, distinct from the existing portfolio-level realized brake. Order in `evaluate_titan_exit`: velocity_fuse → per_trade_brake → BE-stop. New exit_reasons: `forensic_stop`, `per_trade_brake`. New log tags: `[V590-FORENSIC]`, `[V590-PER-TRADE-BRAKE]`.

Touched modules: new `qqq_regime.py`; `tiger_buffalo_v5.py` (G1 signature swap, forensic helpers, per-trade brake, init_titan_exit_state extension); `trade_genius.py` (regime singleton, seed chain, tick driver, abort logger, gate callsite); `bot_version.py` → 5.9.0; CHANGELOG / ARCHITECTURE / Dockerfile / smoke_test / saturday_weekly_report enum updated.

---

## v5.8.4 — 2026-04-27 — Saturday weekly report parser

Pure tooling. **No algorithm or live-trading paths touched.** Replaces the broken Saturday cron `873854a1`, which still parses `[V510-SHADOW]` lines that haven't existed since v5.5.x. Live prod (v5.8.x) emits `[V560-GATE]` / `[V570-STRIKE]` / `[V571-EXIT_PHASE]` / `[ENTRY]` / `[TRADE_CLOSED]` / `[SKIP]` — the new report reads exactly that schema.

- **`scripts/saturday_weekly_report.py`** — CLI: `python scripts/saturday_weekly_report.py --week-start <YYYY-MM-DD> [--out-dir …] [--logs-dir …]`. Online mode pulls `deploymentLogs` from Railway GraphQL (env: `RAILWAY_API_TOKEN` / `RAILWAY_PROJECT` / `RAILWAY_SERVICE` / `RAILWAY_ENVIRONMENT`), persists per-day `day_YYYY-MM-DD.jsonl`, then parses. `--logs-dir` enables offline mode for testing against historical snapshots. Default `--week-start` is the most recent Monday before today.
- **Report sections:** (1) headline P&L / entries / win-rate, (2) 4-config comparison table for `TICKER+QQQ` / `TICKER_ONLY` / `QQQ_ONLY` / `GEMINI_A` (allowed vs blocked, P&L sums, win rate, net swing vs actual — pairs `[ENTRY]` to `[TRADE_CLOSED]` by `entry_id` and attributes per-config decisions from the most recent `[V510-SHADOW][CFG=…]` verdict for that ticker), (3) per-exit-reason P&L breakdown (`hard_stop_2c` / `ema_trail` / `be_stop` / `velocity_fuse` / `eod` / `kill_switch`), (4) `[SKIP]` stats with top-3 most-skipped gates and 5 most-affected tickers each, (5) cumulative two-week + comparison vs prior week (auto-discovers `<out-dir>/week_<MONDAY>/report.json`), (6) anomalies / data gaps. Also writes `report.json` for next week's cumulative comparison.
- **Tests.** `tests/test_saturday_weekly_report.py` (11 tests, all PASS) with synthetic fixtures under `tests/fixtures/saturday_report/` covering every event type, every exit reason, allowed-win + allowed-loss + blocked + skip lines, and an end-to-end offline CLI run.

The Saturday cron task body should now invoke `python scripts/saturday_weekly_report.py --week-start <Monday>` instead of the legacy parser. See ARCHITECTURE.md and CLAUDE.md for the dry-run command against last week's data.

---

## v5.8.3 — 2026-04-27

Fix shadow_positions DB path in scripts/lib/checks.sh: /data/shadow.db -> /data/state.db

---

## v5.8.2 — 2026-04-28 — Infra-B smoke library bug-fixes (dogfood follow-up)

Pure infra/tooling patch. **No algorithm logic touched, no live trading paths modified.** Dogfooding v5.8.1 against the live Railway deploy surfaced two bugs in `scripts/lib/checks.sh`:

- **Bash parameter-default brace-expansion bug.** `local variables="${2:-{}}"` in `_build_gql_payload` and `_railway_gql` was being parsed by bash as `${2:-{}` followed by literal `}`, so the GraphQL variables JSON gained an extra trailing `}` on every call (and a doubled extra brace once the value bounced through both functions). The resulting request payload was invalid JSON, Railway returned an error, and `json.loads` blew up at column 156 with "Extra data". Fixed by switching to a sentinel default (`local variables="${2:-}"; [ -z "${variables}" ] && variables='{}'`), which sidesteps the brace-default lex.
- **`check_deploy_status` printed the entire commit message as the version.** Railway's `meta.commitMessage` is the full multi-line commit text (e.g. `"v5.8.1: Infra-B…\n\nPure infra/tooling release. …"`), not a bare version string. The parser now regex-extracts the first `X.Y.Z` SemVer token from `meta.version`, falling back to `commitMessage` then `branch`, so the echoed line is `DEPLOY SUCCESS <8-char-id> v5.8.1` instead of a paragraph.

**Tests.** `tests/test_checks_lib.sh` still PASSES 37/37 (fixtures already exercised the SemVer-token path because their `meta.commitMessage` was just `"5.8.1"`; tightening to a multi-line fixture for v5.8.2 isn't strictly needed but is added in a follow-up if regressions reappear).

**Out of scope.** Same as v5.8.1: weekday cron `58c883b0` and Saturday cron `873854a1` task bodies live outside the repo and are updated by the parent agent post-merge.

---

## v5.8.1 — 2026-04-27 — Infra-B post-deploy smoke + checks library

Pure infra/tooling release. **No algorithm logic touched, no live trading paths modified.** Replaces the per-release manual smoke ritual (railway ssh → version → universe → log-tag schema → bar archive → shadow_data_status, ~5–10 min × 3 releases per session) with a single sourceable bash library that both the post-deploy verifier and the recurring weekday/Saturday crons call.

**Deliverables:**

- **`scripts/lib/checks.sh`** — sourceable library, 7 pure functions, structured one-line stdout per check, 0/1 return codes:
  - `check_deploy_status` — Railway GraphQL `deployments(first:1)` → `DEPLOY <status> <8-char-id> v<version>`
  - `check_universe_loaded` — greps last 200 log lines for `[UNIVERSE_GUARD]` (current schema) → `UNIVERSE <count> tickers: <list>`
  - `check_log_tags <tag…>` — counts each tag in last 500 log lines → `TAG <name> <count>` per tag
  - `check_no_errors` — counts `must be a coroutine` / `websocket error` / `Traceback` / `[ERROR]` → `ERRORS coroutine=N ws=N traceback=N error=N`
  - `check_bar_archive_today` — `railway ssh` `ls /data/bars/<UTC-date>` + `du -sb` → `BARS_TODAY exists=… ticker_count=… bytes=…` (passes whenever dir exists; soft-warn on empty dir for market-closed days)
  - `check_shadow_db_count` — sqlite3 total + last-24h breakdown by `config_name` → `SHADOW_DB total=N last_24h=…` (informational, always returns 0)
  - `check_dashboard_state` — POST `/login` + GET `/api/state` → `DASHBOARD shadow_data_status=… version=…`
  - All HTTP/SSH I/O is overridable via `RAILWAY_LOGS_FIXTURE` / `RAILWAY_DEPLOY_FIXTURE` / `RAILWAY_SSH_FIXTURE` / `DASHBOARD_STATE_FIXTURE` env vars so tests run offline.
- **`scripts/post_deploy_smoke.sh`** — orchestrator. `bash scripts/post_deploy_smoke.sh <expected_version>` runs all 7 checks, tallies PASS/FAIL, exits 0 on all-pass, 1 on any-fail. Default `EXPECTED_TAGS`: `STARTUP SUMMARY`, `[UNIVERSE_GUARD]`, `[V560-GATE]`, `[V570-STRIKE]`, `[V571-EXIT_PHASE]`. Failures are informational — this script does NOT block automated merges.
- **`tests/test_checks_lib.sh`** — 13 cases × 37 assertions, all 7 checks happy + sad paths, fixtures under `tests/fixtures/checks/`. Plain bash, no bats dependency. Run with `bash tests/test_checks_lib.sh`.
- **`.github/workflows/scripts-lint.yml`** — soft-fail shellcheck over `scripts/*.sh` and `scripts/lib/*.sh` on every PR that touches `scripts/`. Doesn't block merges yet because pre-existing scripts may not pass.

**Out of scope (handled by parent agent after this PR merges):**

- Weekday cron `58c883b0` (8:35am CT) rewrite to `source scripts/lib/checks.sh` and call `scripts/post_deploy_smoke.sh`.
- Saturday cron `873854a1` (10am CT) parser refactor onto current log schema (`[V560-GATE]` / `[V570-STRIKE]` / `[V571-EXIT_PHASE]` / `[TRADE_CLOSED]`) plus per-exit-reason P&L breakdown. Both cron task bodies live outside the repo.

**Rollback.** Revert the PR. All deliverables are additive dev tooling; live runtime behavior is unchanged.

---

## v5.8.0 — 2026-04-27 — Developer Velocity Bundle

Pure repo/tooling release. **No algorithm logic touched, no live trading paths modified.** Cuts subagent cold-start time, prevents CI-fail iteration cycles, and eliminates the universe-drift recovery class of incidents that hit v5.7.0.

**Deliverables:**

- **`CLAUDE.md`** at repo root — concise agent guide subagents read on first cold-start (where things live, mandatory PR rules, pre-push checklist, PR submission flow). Parallel **`AGENTS.md`** `@import`s it so Codex picks up the same guide.
- **`specs/_TEMPLATE.md`** — spec scaffolding so every future release starts from a consistent shape (Decisions / Goals / Scope / Logging schema / Tests / Rollout).
- **`scripts/preflight.sh`** — local CI mirror. BLOCKS on five checks: pytest, `BOT_VERSION` ↔ CHANGELOG consistency, em-dash literal in `.py`, forbidden-word (`scrape|crawl|scraping|crawling`), ruff format. Em-dash and forbidden-word checks are scoped to files **changed in this PR vs `origin/main`** so the pre-v5.8.0 codebase (hundreds of grandfathered literal em-dashes) does not block local runs.
- **`bot_version.py`** — canonical version constant (mirrored to `trade_genius.py.BOT_VERSION` so the existing `version-bump-check` CI workflow keeps working unchanged).
- **`[UNIVERSE_GUARD]` startup check** — new `_ensure_universe_consistency()` helper runs at boot in `trade_genius.py`, before `_init_tickers()`. Reads `/data/tickers.json`, compares against canonical `TICKERS_DEFAULT`, and rewrites (preserving the existing envelope format) if the file is missing, corrupt, or has drifted. Tolerant of both flat-list and `{"tickers": [...]}` envelope JSON formats.

**New log tag:** `[UNIVERSE_GUARD]` — emits exactly one of three lines on every boot for post-deploy observability:

- `[UNIVERSE_GUARD] universe consistent (N tickers)` — happy path
- `[UNIVERSE_GUARD] DRIFT detected: disk=… code=… — rewriting to code` — drift caught
- `[UNIVERSE_GUARD] /data/tickers.json corrupt (…), rewriting` — corrupt JSON

If none of these appears in startup logs, the guard didn't run.

**Tests.** `tests/test_universe_guard.py` covers four cases (missing file, corrupt JSON, drift detected, consistent / no rewrite needed) using pytest's `tmp_path` fixture and `monkeypatch`.

**Rollback.** Revert the PR; the only runtime change is the startup-time guard call. `preflight.sh`, `CLAUDE.md`, `AGENTS.md`, `specs/_TEMPLATE.md`, and `bot_version.py` are dev-tooling only — no rollback needed for those.

---

## v5.7.1 — 2026-04-28 — Bison & Buffalo exit-logic optimization

Rewrites the exit-logic state machine for the **Ten Titans only**. Non-Titan tickers (anything added later via `[WATCHLIST_ADD]`) keep the legacy `evaluate_exit` path (DI<25 hard eject + structural stop) byte-for-byte. v5.7.0 carved `tiger_buffalo_v5.py` out completely; v5.7.1 carves it back in with pure Bison/Buffalo exit-FSM helpers, exercised by 15 new smoke tests. `ENABLE_BISON_BUFFALO_EXITS = True` is the default; `False` reverts every Titan to the legacy path.

**Originally specced as v5.6.3** — promoted to v5.7.1 since current main is v5.7.0 and CI requires monotonic version bumps.

**Deliverables:**

- **D1 — Three-phase exit FSM (Titans only):**
  - `initial_risk` — Hard stop fires on **2 consecutive 1-min CLOSES** outside OR (LONG: below `OR_High`; SHORT: above `OR_Low`). Counter resets to 0 only when a 1-min candle closes back inside OR — slow grind-down keeps counting. `exit_reason=hard_stop_2c`.
  - `house_money` — After the close of the **2nd green 5-min** post-entry (LONG; `close > open`) — or 2nd red for SHORT — the stop ratchets to entry price. Hard-stop counter is now inactive. `exit_reason=be_stop`.
  - `sovereign_trail` — Once the 5-min 9-period EMA is seeded (close of the 9th 5-min bar since 9:30 ET = **10:15 ET**), a 5-min CLOSE strictly below the EMA (LONG) — or strictly above (SHORT) — fires `exit_reason=ema_trail`. Before 10:15 ET the EMA is `None` and only Hard Stop / BE / Velocity Fuse apply.
- **D2 — Velocity Fuse (global override):** runs every tick on every Titan position regardless of phase. Comparison base is the **OPEN of the current (in-flight) 1-min candle**, not the prior candle's close. LONG fires on `current_price < open * 0.99` (strict; -1.00% does NOT trigger; -1.001% does); SHORT mirrors. On fire: `[V571-VELOCITY_FUSE]` line, then immediate market exit, then `[TRADE_CLOSED] exit_reason=velocity_fuse`. Strike counter still increments correctly so the v5.7.0 expansion gate re-arms on next entry.
- **D3 — DI exit deletion (Titans only):** the legacy `DI+(1m) < 25` (LONG exit) and `DI-(1m) < 25` (SHORT exit) triggers are bypassed for Titans via `evaluate_exit(..., is_titan=True)`. **Non-Titan tickers retain both DI exits** — the v5.0.0 priority order is preserved verbatim for the legacy path. Wholesale deletion was avoided by design.
- **D4 — Per-position state additions:** `phase`, `hard_stop_consec_1m_count`, `green_5m_count` (LONG), `red_5m_count` (SHORT), `ema_5m`, `current_stop` — all initialized by `init_titan_exit_state`. Pure helpers in `tiger_buffalo_v5.py` mutate the track dict only.
- **D5 — New + extended log lines:**
  - `[V571-EXIT_PHASE] ticker=<T> side=<L|S> entry_id=<id> from_phase=<…> to_phase=<…> trigger=<…> current_stop=<f> ts=<utc>` — emitted on phase transition only.
  - `[V571-VELOCITY_FUSE] ticker=<T> side=<L|S> candle_open=<f> current_price=<f> pct_move=<f> ts=<utc>` — emitted on every fuse fire, immediately before the market exit.
  - `[V571-EMA_SEED] ticker=<T> ema_value=<f> ts=<utc>` — emitted exactly once per ticker per session at 10:15 ET.
  - `[TRADE_CLOSED] … exit_reason=…` — the v5.6.1 enum gains four new values: `hard_stop_2c`, `be_stop`, `ema_trail`, `velocity_fuse`. The legacy `stop|target|time|eod|manual` values remain valid for non-Titan paths.
- **D6 — Configuration:** `ENABLE_BISON_BUFFALO_EXITS = True` (default ON; emergency rollback flag), `VELOCITY_FUSE_PCT = 0.01` (strict 1.0% threshold).
- **D7 — Smoke tests:** 15 new tests covering every scenario in §7 of the spec — LONG hard stop fire/reset, BE transition, EMA trail, EMA-not-yet-seeded handling, velocity fuse fire/non-fire/across-phases, SHORT mirrors, DI deletion for Titans, DI preserved for non-Titans, log-line emit on transitions, EMA_SEED line at 10:15 ET. Smoke 375 → ~390 passed.

**Module placement.** v5.7.1 carves `tiger_buffalo_v5.py` back in: pure Bison/Buffalo exit-FSM helpers (`init_titan_exit_state`, `update_hard_stop_counter_long/short`, `update_green_5m_count_long`, `update_red_5m_count_short`, `update_ema_5m`, `velocity_fuse_long/short`, `evaluate_titan_exit`, `transition_to_house_money`, `transition_to_sovereign_trail`) live in `tiger_buffalo_v5.py`. `evaluate_exit` gains an `is_titan` kwarg for the DI deletion. The `trade_genius.py` runtime owns the log emitters, config flags, and the wiring between live ticks and these pure helpers.

---

## v5.7.0 — 2026-04-27 — Unlimited Titan Strikes

For the **Ten Titans only** the fixed re-entry cap (`L-P5-R3` / `S-P5-R3`) is replaced by an unlimited HOD/LOD-gated re-entry path. Strike 1 is unchanged (still gated by the v5.6.0 unified AVWAP permission set, L-P1 / S-P1, G1-G3-G4). The -$500 daily loss limit is wired explicitly to every entry path and now emits a single canonical `[KILL_SWITCH]` line on first breach. `tiger_buffalo_v5.py` is untouched — all v5.7.0 logic lives in `trade_genius.py`.

**Universe — Ten Titans (new):** `AAPL, AMZN, AVGO, GOOG, META, MSFT, NFLX, NVDA, ORCL, TSLA`. NFLX and ORCL are added to `TICKERS_DEFAULT`; the QQQ-archive, OR-snapshot, and `[UNIVERSE]` boot line all pick them up automatically through the existing v5.6.1 paths.

**Deliverables:**

- **D1 — NFLX + ORCL:** added to `TICKERS_DEFAULT`. Bar archive at `/data/bars/<UTC>/{NFLX,ORCL}.jsonl` and OR snapshot at `/data/or/<UTC>/{NFLX,ORCL}.json` are wired through the existing v5.6.1 helpers (no separate writer code needed). `[UNIVERSE]` boot line now emits the full 10-Titan + QQQ + SPY + QBTS list alphabetically.
- **D2 — `TITAN_TICKERS` constant:** new module-level `TITAN_TICKERS = ["AAPL", "AMZN", "AVGO", "GOOG", "META", "MSFT", "NFLX", "NVDA", "ORCL", "TSLA"]` plus the feature flag `ENABLE_UNLIMITED_TITAN_STRIKES = True` (default on; `False` falls back to the v5.6.0 R3 cap path). `DAILY_LOSS_LIMIT_DOLLARS = -500.0` is published explicitly for parity with the spec.
- **D3 — Strike 2+ Expansion Gate:** for Titans only, when strike_num >= 2:
  - **LONG** passes iff `current_price > prior_session_HOD` (strict, fresh print) AND `index_price > index_avwap` (strict — same comparator as v5.6.0 G1). AVWAP None FAILs.
  - **SHORT** mirrors with strict `<`.
  - Session HOD/LOD is tracked per-ticker per-day, seeded from the first 9:30 ET print (pre-market does NOT seed), reset at 9:30 ET each session.
  - Strike counter is per-ticker per-side per-day; increments only on successful ENTRY (not SKIP); resets at 9:30 ET.
- **D4 — R3 bypass for Titans:** the `daily_count >= 5` cap in `check_breakout` is bypassed for tickers in `TITAN_TICKERS` when the feature flag is on. Non-Titan tickers (anything added later via `[WATCHLIST_ADD]`) still see the 5-cap and the v5.6.0 R3 re-hunt budget on `tiger_buffalo_v5`.
- **D5 — `-$500` daily loss kill switch (sovereign brake):** the existing `_check_daily_loss_limit` (originally added in v4.7.0 at -$500) is preserved and not retuned. v5.7.0 layers a v5.7.0-native latch (`_v570_kill_switch_*`) directly on top of `[TRADE_CLOSED]` emissions so realized P&L is summed lock-step with the lifecycle log. On first breach (`<= -500.00`) every entry path returns `[SKIP] reason=daily_loss_limit_hit gate_state=null` and a single `[KILL_SWITCH] reason=daily_loss_limit triggered_at=<utc> realized_pnl=<f>` line is emitted (de-duped — never spammed). Open positions are NOT force-closed; they exit on their own normal exits and continue to emit `[TRADE_CLOSED]`. Latch resets at the next ET session boundary.
- **D6 — New + extended log lines:**
  - `[V570-STRIKE] ticker=<T> side=<L|S> ts=<utc> strike_num=<int> is_first=<bool> hod=<f|null> lod=<f|null> hod_break=<bool> lod_break=<bool> expansion_gate_pass=<bool>` — emitted on every entry-path evaluation. Replaces `[V560-GATE]` on Strike 2+; alongside `[V560-GATE]` on Strike 1.
  - `[ENTRY]` gains `strike_num=<int>`. `entry_id` schema unchanged.
  - `[TRADE_CLOSED]` gains `strike_num=<int>` (echoes the entry's strike) and `daily_realized_pnl=<f>` (running cumulative for the day after this close).
  - `[KILL_SWITCH]` line above.

**Investigation result — kill switch existed pre-PR:** `_check_daily_loss_limit` has been live since v4.7.0 with threshold sourced from `DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "-500"))`. Threshold left untouched per spec. v5.7.0 layers the new `[KILL_SWITCH]` line and the `daily_loss_limit_hit` SKIP reason on top so replay tooling can identify a halt without reading Telegram.

**Conventions:**

- `BOT_VERSION` bumped 5.6.1 → 5.7.0.
- `CURRENT_MAIN_NOTE` rewritten for v5.7.0 (every line ≤ 34 chars). The v5.6.1 note rolls onto `_MAIN_HISTORY_TAIL`.
- New string literals introduced in this release use `\u2014` escape sequences rather than literal em-dashes (CHANGELOG / ARCHITECTURE / README still use real em-dashes).
- `tiger_buffalo_v5.py` source is byte-identical to v5.6.1 (the v5.6.0 unified AVWAP gates and the v5.0.0 state machine are unchanged).

**Tests / smoke:**

- 18+ new v5.7.0 assertions covering: TITAN_TICKERS shape; feature flag; HOD/LOD seeding; strike counter increment + reset; Strike 1 path (unchanged); Strike 2+ pass/fail variants (HOD break, AVWAP None, Index direction); R3 bypass for Titans + R3 still applied for non-Titans; kill-switch threshold ≤ -500.00 (boundary, just-under, just-over); kill-switch single-emission de-dupe; `[V570-STRIKE]` line shape; `[ENTRY]` and `[TRADE_CLOSED]` strike_num field; `[KILL_SWITCH]` line shape; `[UNIVERSE]` boot line includes all 10 Titans; feature-flag rollback behavior.
- The 8 historical "BOT_VERSION bumped to 5.5.x" pinned tests, plus `version: BOT_VERSION is …`, plus `CHANGELOG.md has v… heading at top`, plus `ARCHITECTURE.md last-refresh footer pinned to …` are all re-pinned to `5.7.0`.

**Docs:**

- `ARCHITECTURE.md` updated — new section §22 covers the Ten Titans universe, the Strike 2+ Expansion Gate, the v5.7.0 strike counter, and the kill switch surface; last-refresh footer bumped to `BOT_VERSION = "5.7.0"`.
- `trade_genius_algo.pdf` regenerated (Titan universe + Strike 2+ Expansion Gate + kill-switch wiring all changed).

**Out of scope (per spec):** any change to v5.6.0 G1/G3/G4 comparators or AVWAP computation; true OHLC bars / volume capture; bid/ask population. Saturday cron task description update is a separate non-PR change.

---

## v5.6.1 — 2026-04-27 — Data-Collection Improvements

Pure observability/data-collection patch. **No gate-logic changes** — `tiger_buffalo_v5.py` is untouched and the v5.6.0 unified AVWAP permission gates remain canonical. This release expands the on-disk archive surface and richens the structured log lines so downstream replay/analysis tooling has a complete picture of every entry consideration.

**Data-collection deliverables:**

- **D1 — QQQ bar archive:** `_v561_archive_qqq_bar` writes the per-cycle 1m QQQ snapshot to `/data/bars/<UTC-date>/QQQ.jsonl` alongside the 8 trade tickers. Same `bar_archive` schema, same atomic-append guarantees.
- **D2 — OR backfill + persistence:** scan_loop now runs a pre-open archive path between 09:29:30–09:35 ET so the OR window's 5 closing 1m bars land on disk. At/after 09:35 ET, `_v561_persist_or_snapshot` writes `{ticker, or_high, or_low, computed_at_utc}` to `/data/or/<UTC-date>/<TICKER>.json` (idempotent — at most one snapshot per ticker per day).
- **D3 — `[V560-GATE]` richened schema:** every gate evaluation now emits a single structured line carrying all 14 fields: `ticker, side, ts, ticker_price, ticker_avwap, index_price, index_avwap, or_high, or_low, g1, g3, g4, pass, reason`.
- **D4 — Trade lifecycle:** every `[ENTRY]` line carries an `entry_id=<TICKER>-<YYYYMMDDHHMMSS>` deterministic id, and every exit emits a paired `[TRADE_CLOSED]` line with `entry_id, side, exit_reason, hold_s, pnl_usd`.
- **D5 — `[SKIP]` with gate_state:** skip lines now embed the full gate snapshot as canonical JSON. Pre-gate skips (e.g. cooldown, loss-cap) emit `gate_state=null`.
- **D6 — `[UNIVERSE]` boot line + `[WATCHLIST_ADD]`/`[WATCHLIST_REMOVE]`:** the alpha-sorted ticker universe (with QQQ included) is logged once at boot, and runtime watchlist mutations emit structured lines for replay.

**Conventions:**

- `BOT_VERSION` bumped to `5.6.1`.
- New string literals introduced in this release use `\u2014` escape sequences rather than literal em-dashes.
- Smoke test suite gains 16 new v5.6.1 assertions.

---

## v5.6.0 — 2026-04-27 — Unified AVWAP Permission Gates (Healing/Limping Bison)

Hard-cut to prod: replaces the legacy 4-gate L-P1/S-P1 permission set with a unified 3-gate AVWAP-anchored system, symmetric for longs and shorts. Ships before Tuesday Apr 28 RTH open. No feature flag, no shadow rollout — every entry consideration after this deploy uses the new gates.

**New permission semantics (locked by Val 2026-04-27 from Gene's spec):**

- **L-P1 (long, ALL three must PASS):**
  - **G1 (Index)**: `Index.Last > Index.Opening_AVWAP`
  - **G3 (Ticker)**: `Ticker.Last > Ticker.Opening_AVWAP`
  - **G4 (Structure)**: `Ticker.Last > Ticker.OR_High`
- **S-P1 (short, ALL three must PASS):**
  - **G1 (Index)**: `Index.Last < Index.Opening_AVWAP`
  - **G3 (Ticker)**: `Ticker.Last < Ticker.Opening_AVWAP`
  - **G4 (Structure)**: `Ticker.Last < Ticker.OR_Low`

**Conventions (locked):**

- **Index = QQQ only** (single-index gate; SPY no longer participates in the permission scan).
- **G2 retired entirely** — the old SPY-vs-PDC index gate is deleted from the permission scan and from `tiger_buffalo_v5.gates_pass_*`.
- **AVWAP** = session-open anchored VWAP. Anchor at 09:30 ET regular-session open, reset daily, recomputed on every 1-minute bar close from the per-cycle 1m bar cache. Implementation: `trade_genius._opening_avwap(ticker)`. Cumulative-volume zero or no bars yet ⇒ returns `None`.
- **OR window** = 5-minute opening range, 09:30–09:35 ET (existing convention preserved).
- **Comparators**: strict `>` and `<`. Equality (price == AVWAP, price == OR_High/Low) returns FAIL. Boundary blocks the gate.
- **Pre-9:35 ET (OR not yet defined)**: G4 returns `False` deterministically (no raise, no `None` return). Documented in `tiger_buffalo_v5.gate_g4_long`/`gate_g4_short`.
- **AVWAP None**: G1/G3 return `False` deterministically. No entries before AVWAP has at least one bar of cumulative volume.

**Code changes:**

- `tiger_buffalo_v5.py`: deleted the 7-arg `gates_pass_long`/`gates_pass_short` (which required SPY/PDC inputs). New 5-arg signatures: `gates_pass_long(qqq_last, qqq_opening_avwap, ticker_last, ticker_opening_avwap, ticker_or_high)` and the symmetric short. Six new strict per-gate predicates (`gate_g1_long`, `gate_g1_short`, `gate_g3_long`, `gate_g3_short`, `gate_g4_long`, `gate_g4_short`) so callers and tests can evaluate each leg independently.
- `trade_genius.py`: new `_opening_avwap(ticker)` helper computes session-open AVWAP from `fetch_1min_bars` (`(high+low+close)/3 × volume` summed since the 09:30 ET cutoff, divided by cumulative volume). Returns `None` when no bars are in the window or cumulative volume is zero. New `_v560_log_gate(ticker, side, gate, value, threshold, result)` forensic logger emits one `[V560-GATE]` line per G1/G3/G4 evaluation with all four fields plus the boolean result — Saturday's report parses these to validate the change. `check_breakout` now reads QQQ AVWAP, ticker AVWAP, and `or_high`/`or_low` and dispatches through the v5 strict gate predicates; the legacy SPY-PDC + QQQ-PDC + ticker-PDC polarity block was deleted. On block, emits `[V560-GATE][BLOCK]` with the `failed=` list (e.g. `failed=G1,G3`); on pass, emits `[V560-GATE][PASS]` with all four values.
- `trade_genius.py`: `BOT_VERSION` 5.5.11 → 5.6.0. New STARTUP SUMMARY line `[V560] Unified AVWAP gates: L-P1 (G1/G3/G4), S-P1 (G1/G3/G4)` confirms on every boot that the new gate set is wired. `CURRENT_MAIN_NOTE` rewritten for v5.6.0 (each line ≤ 34 chars), with the v5.5.11 AS-OF-hotfix note pushed onto `_MAIN_HISTORY_TAIL`.

**Tests / smoke (+19 net new, all passing):**

- 6 unit tests for the per-gate predicates (`gate_g1_long`/`_short`, `gate_g3_long`/`_short`, `gate_g4_long`/`_short`) — each covers PASS, equality FAIL, below/above FAIL, and `None`-input FAIL.
- 8 integration tests for the full `gates_pass_long` / `gates_pass_short` paths — 1 pass + 3 single-gate-block scenarios per direction.
- 5 guards: `BOT_VERSION == "5.6.0"`; `CHANGELOG.md` has a v5.6.0 heading; `gates_pass_long` and `gates_pass_short` both have 5-parameter signatures (G2 retired); `tiger_buffalo_v5.py` source has no remaining `L-P1-G2` / `S-P1-G2` references; `trade_genius.py` exposes `_opening_avwap` and `_v560_log_gate`.
- The existing `v5.5.5: ARCHITECTURE.md last-refresh footer pinned to …` and `v5.5.5: CHANGELOG.md has v… heading at top` guards re-pinned to `5.6.0`. The 8 stale `BOT_VERSION` pin tests (one per v5.5.x release) re-pinned to `5.6.0`. The `v5 C-R7` test now asserts QQQ-only wiring in `check_breakout` (SPY removed with G2).

**Docs:**

- `ARCHITECTURE.md` updated — permission-scan section now describes the unified 3-gate AVWAP system; G2 removed from §19 / §20 tables; last-refresh footer bumped to `BOT_VERSION = "5.6.0"`.
- `trade_genius_algo.pdf` regenerated (algo text changed: G2 retired; G1/G3 now AVWAP-anchored).
- `STRATEGY.md` and `COMMANDS.md` left as-is (the rule-ID surfaces are tracked from the v5.6.0 unit tests above; STRATEGY.md is being replaced wholesale in a follow-up doc PR per Gene/Val's separate refactor).

**Out of scope for this PR (per spec):** no SPY combination logic, no PDC checks, no 3-speed Yellow-state branches, no feature flag / kill switch, no prior-day or multi-day AVWAP variants.

---

## v5.5.11 — 2026-04-27

Smoking-gun summary: v5.5.10 shipped a 2-line fix that swapped the Shadow tab AS OF cell from `s.as_of` to `s.server_time` and rendered via `_scFmtTs(asof)`. The read was correct — `/api/state` does include `server_time`. But on prod v5.5.10 the cell still rendered the static em-dash placeholder. Verified live at 18:21 UTC: `document.getElementById('ssb-asof')` resolved fine, a 12-second mutation observer saw `ssb-open`/`ssb-unr`/`ssb-active` mutate 6× each but `ssb-asof` mutate 0×, and calling `_scFmtTs` from the page-global console threw `ReferenceError: _scFmtTs is not defined`. Root cause: `dashboard_static/app.js` is split into two IIFEs. `_shadowSummaryBand` lives in IIFE-1 (lines 1–1236) and the `_scFmtTs` formatter lives in IIFE-2 (lines 1238–2785). IIFE locals are not visible to sibling IIFEs and `_scFmtTs` is never bridged to `window`, so the call threw at runtime. The throw was swallowed by the `try { _shadowSummaryBand(s); } catch (e) {}` wrapper inside `renderShadowPnL`, which is why the open / unrealized / most-active cells (lines 957–970, all *before* the failing line) updated normally on every state tick while ssb-asof never wrote. Pre-fix code never tripped this latent bug because `s.as_of` was always falsy → the `_scFmtTs` branch was never taken.

- fix (`dashboard_static/app.js` `_shadowSummaryBand`): inlined a self-contained 14-line ET timestamp formatter directly inside the function so it no longer depends on the IIFE-2-local `_scFmtTs`. Reads `s.server_time` first, falls back to `s.shadow_pnl.as_of`, formats via `Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", … }).formatToParts(d)` into `MM/DD HH:MM ET`, with two layers of fallback: invalid-date → `String(asof)`, and any throw inside the format path → also `String(asof)`. The em-dash placeholder only renders when both server_time and shadow_pnl.as_of are absent. `_scFmtTs` in IIFE-2 is left untouched (it has other callers — the per-config chart tooltips). The `try { _shadowSummaryBand(s); } catch (e) {}` wrapper in `renderShadowPnL` is preserved so any future regression still fails-soft on the rest of the panel rather than crashing the whole shadow tab render.
- tests: 1 new in-suite smoke guard plus the existing version pin —
  - `v5.5.11: _shadowSummaryBand does not call _scFmtTs (cross-IIFE guard)` — parses the function body of `_shadowSummaryBand` out of `dashboard_static/app.js` and asserts the literal substring `_scFmtTs(` does not appear inside it. This pins the cross-IIFE separation so a future refactor that re-introduces the bridge from IIFE-1 to IIFE-2 fails CI loudly. Existing v5.5.10 guard (`s.server_time` present, pre-fix `s.as_of`/`_scFmtTs` line absent) still passes unchanged.
  - Smoke version pin bumped from `5.5.10` → `5.5.11`. All earlier regression guards still pass unchanged.
- CI guard: `BOT_VERSION` bumped to `5.5.11` (matches this heading; the version-bump-check workflow gates on both). `CURRENT_MAIN_NOTE` rewritten for v5.5.11 (each line ≤ 34 chars), with the v5.5.10 persist-positions note pushed onto `_MAIN_HISTORY_TAIL`.
- docs: ARCHITECTURE.md unchanged (this is not an architectural change — same data flow, same `/api/state` shape, same DOM target; only the formatter-call path moved). `trade_genius_algo.pdf` unchanged (no algorithm or trading-decision change).

No trading-decision change. v5.5.11 is a single-file dashboard JS hotfix that makes v5.5.10's correct read finally render. No change to `dashboard_server.py`, `/api/state` payload shape, persistence layer, executor logic, or any algo path.

---

## v5.5.10 — 2026-04-27

Smoking-gun summary: at every Val (or Gene) executor reboot during a live session with open broker positions, a Telegram fired — `⚠️ Reconcile: grafted N broker orphan(s) on Val boot` — even though the bot had simply restarted into a session it had opened normally. Today's example: v5.5.9 deployed at 17:40 UTC, Val rebooted, found META 14 @ $680.28 on the broker (a clean v5.5.8 signal-driven entry), grafted it as an "orphan", and Telegram'd. Root cause: `TradeGeniusBase.__init__` initializes `self.positions: dict = {}` empty on every boot. The dict was only populated by `_record_position` (in-memory after a successful submit) and `_reconcile_broker_positions` (at start-time via `client.get_all_positions()`). It was NEVER persisted to disk and NEVER rehydrated at boot, so every reboot looked like total state divergence to the reconcile path. Separately, the Shadow tab top summary band's `AS OF` field was stuck on the em-dash placeholder because `dashboard_static/app.js` read `s.as_of` from `/api/state` and that key was never emitted at the top level (the canonical field is `server_time`).

- feat (`persistence.py`): new `executor_positions` table in `state.db` (`/data/state.db` on Railway) with PRIMARY KEY `(executor_name, mode, ticker)` so Val/paper, Val/live, Gene/paper, Gene/live each have independent buckets and never overwrite each other. New helpers `save_executor_position`, `load_executor_positions`, `delete_executor_position` follow the existing `BEGIN IMMEDIATE`/`COMMIT` write pattern with `INSERT OR REPLACE` semantics for idempotent writes. Schema is created in `init_db()` alongside `fired_set` / `v5_long_tracks` / `shadow_positions`, so any existing TradeGenius boot after this release auto-migrates.
- feat (`trade_genius.TradeGeniusBase`): three new methods — `_load_persisted_positions()` (read all rows for `(self.NAME, self.mode)` and populate `self.positions`), `_persist_position(ticker)` (mirror one in-memory row to the DB), and `_delete_persisted_position(ticker)` (delete one row). Plus a `_remove_position(ticker)` helper that drops the dict entry AND the DB row in one call so every position-close path stays consistent with one line of code.
- feat (`__init__`): `_load_persisted_positions()` now runs at the end of `__init__`, BEFORE `start()` calls `_reconcile_broker_positions()`. So a plain reboot during a live session sees the persisted dict already populated and the reconcile path stays silent.
- fix (`_record_position`): mirrors every successful entry to `executor_positions` via `_persist_position(ticker)` immediately after stamping `self.positions[ticker]`, so the next reboot picks it up.
- fix (`_reconcile_broker_positions`): rewritten as a true safety net with three explicit outcomes, distinguished by set comparison of persisted-tickers vs broker-tickers:
  1. Persisted == broker → INFO log `[RECONCILE] clean: N position(s) match broker`, no Telegram (the common reboot case, today's META scenario).
  2. Broker has tickers persisted does not → graft as today (source=`RECONCILE`, persist the new row), WARN log per orphan, single Telegram suffixed `(true divergence)` so the operator can tell a real divergence from the legacy noisy alert.
  3. Persisted has tickers broker does not → quiet self-heal: WARN log `[RECONCILE] stale local position: ticker=X — broker says no position, removing` then `_remove_position(ticker)`. No Telegram, no close/exit-path call — the broker is already in the desired state.
- fix (close paths): every code path that closes a position now calls `_remove_position(ticker)` to drop both the in-memory dict entry and the persisted row — `EXIT_LONG`/`EXIT_SHORT` dispatch in `_on_signal`, `EOD_CLOSE_ALL` dispatch, and `cmd_halt`. Pre-fix, none of them touched `self.positions` at all (the dict was already write-only), so a stray row could only have appeared via reconcile-then-restart; with persistence on, every removal must propagate to the DB or stale rows accumulate.
- fix (`set_mode`): a paper⇄live flip now wipes `self.positions` and calls `_load_persisted_positions()` so the executor sees the bucket for the new mode (paper rows do not bleed into live or vice versa). The `(executor_name, mode, ticker)` PK already enforces the storage-side separation; this hook makes the in-memory view match.
- fix (`dashboard_static/app.js` `_shadowSummaryBand`): the top-summary `AS OF` cell read `s.as_of` from `/api/state`, which is never emitted at the top level. The canonical timestamp is `s.server_time` (line 880 in `dashboard_server.py`), with `s.shadow_pnl.as_of` as a fallback. Two-line change reads `s.server_time` first, falls back to `s.shadow_pnl.as_of`, then renders via the existing `_scFmtTs` helper (em-dash placeholder only when both are absent).
- tests: 7 new in-suite smoke guards plus the existing version pin —
  - `v5.5.10: executor_positions table exists in state.db schema after init_db`
  - `v5.5.10: _record_position writes an executor_positions row`
  - `v5.5.10: _load_persisted_positions populates self.positions on __init__`
  - `v5.5.10: _reconcile_broker_positions is silent when persisted matches broker`
  - `v5.5.10: _reconcile_broker_positions self-heals stale persisted entries quietly`
  - `v5.5.10: _reconcile_broker_positions still grafts + Telegrams on true divergence`
  - `v5.5.10: shadow tab AS OF reads s.server_time (not s.as_of) in app.js`
  - Smoke version pin bumped from `5.5.9` → `5.5.10`. All v5.5.5 → v5.5.9 regression guards still pass unchanged.
- CI guard: `BOT_VERSION` bumped to `5.5.10` (matches this heading; the version-bump-check workflow gates on both). `CURRENT_MAIN_NOTE` rewritten for v5.5.10 (each line ≤ 34 chars), with the v5.5.9 shadow-charts-polish note pushed onto `_MAIN_HISTORY_TAIL`.
- docs: `ARCHITECTURE.md` § persistence section gained a paragraph describing the new `executor_positions` table — schema, PK rationale, and the three reconcile outcomes. `trade_genius_algo.pdf` left unchanged: this release is plumbing (executor-side persistence layer) plus a 2-line dashboard JS fix; no algorithm or trading-decision change touches the algo PDF's scope.

No trading-decision change. The grafted-orphan heuristic (broker says we own X, we do not know about it → graft as RECONCILE-sourced position) is preserved exactly; v5.5.10 only stops misclassifying a stale-restart as a divergence. The pre-fix per-ticker WARN log line is preserved verbatim so existing log alerts keyed on `[RECONCILE] grafted broker orphan` continue to fire on real divergences. No change to `_emit_signal`, `evaluate_g4`, the WS consumer, paper-book sizing, or any executor entry/exit dispatch logic.

---

## v5.5.9 — 2026-04-27

Smoking-gun summary: OOMPH_ALERT had 207 open shadow positions and ~−$7.8k unrealized at the end of the v5.5.8 trading day, but ZERO closed trades — the weekly batch that materializes closed-trade rows doesn't run until Sat May 2. Result: every per-config chart on the Shadow tab rendered "no closed trades", the equity / win-rate / heatmap groups painted 7 blank rows, and the SHADOW STRATEGIES table gave no at-a-glance sentiment cue while the user scrolled its long open-positions detail. v5.5.9 makes the panel useful TODAY using only the existing `/api/state` shadow payload — no server change.

- feat (`dashboard_static/app.js` `_scBuildEquityRows` + new `_scBuildBarChart`, `_scOpenPositionsByConfig`): when a shadow config has an empty `equity_curve` but non-empty `open_positions`, the EQUITY CURVES card now renders a per-ticker unrealized P&L bar chart instead of the "no closed trades" placeholder. Bars are sorted descending (largest gains left → largest losses right), colored against the existing `--up` / `--down` CSS tokens (no new hex literals), and capped at 30 (top 15 winners + top 15 losers when count > 30) with an "… and N more" footer. A title overlay reads `<config> · <N> open · <±$total> unrealized`. Once `equity_curve` becomes non-empty for a config (Sat May 2 onwards), the existing equity-curve line chart wins automatically — the bar chart is strictly a fallback and never replaces a populated curve.
- feat (`dashboard_static/index.html` + new `#shadow-summary-band`, `_shadowSummaryBand` in `app.js`): top of the Shadow tab now carries a compact summary strip showing total open positions across all configs, total unrealized $ (color-coded green/red via the `--up` / `--down` tokens), the most-active config + its open count, and the state's `as_of` timestamp formatted via the existing `_scFmtTs`. Same visual vocabulary as the index ticker strip (`.shadow-summary-band` in `app.css`). Refreshes on every `renderShadowPnL` tick (5s state poll cadence).
- feat (`_scBuildEquityRows`, `_scBuildHeatmap`, `_scRender`): configs with neither closed nor open trades are now hidden from the EQUITY CURVES, DAY P&L HEATMAP, and ROLLING WIN RATE groups (instead of rendering 7 blank-placeholder rows). The CHARTS section header count `· X / 7` now reflects rendered configs (configs with closed *or* open data), so a state with only OOMPH_ALERT active reads `CHARTS · 1 / 7`. Edge case: when every config is empty the EQUITY CURVES body falls back to a single "Waiting for shadow data…" message rather than 7 hidden rows leaving a blank stripe.
- feat (`renderShadowPnL`): SHADOW STRATEGIES rows gain a subtle `sp-tint-pos` / `sp-tint-neg` background tint by today's P&L sign (`color-mix(in srgb, var(--up) 8%, transparent)` and the `--down` mirror, so we never hardcode a sentiment hex). The tint is suppressed when the row is already painted `sp-best` / `sp-worst` so those saturated highlights stay dominant. Tint applies when the config has either today-trades or open positions — pure-zero rows remain untinted.
- feat (`app.css`): `#shadow-pnl-card .shadow-pnl-head` is now `position: sticky; top: 0; z-index: 2` so the `CONFIG · TODAY · CUMULATIVE` header stays visible while the user scrolls the open-positions table inside an expanded config row.
- ARCHITECTURE.md: dashboard section gained a paragraph noting the client-side fallback bar chart and the empty-group hide in the Shadow tab. `trade_genius_algo.pdf` left unchanged — this is a dashboard-only release with no algo changes; the existing PDF cover already reads through v5.5.8 and the architecture text touched here is dashboard-render detail outside the algo PDF's scope.
- CI guard: `BOT_VERSION` bumped to `5.5.9` (matches this heading; the version-bump-check workflow gates on both). `CURRENT_MAIN_NOTE` rewritten for v5.5.9 (each line ≤ 34 chars), with the v5.5.8 SHORT-entry-row note pushed onto `_MAIN_HISTORY_TAIL`.
- No server changes — `dashboard_server.py` diff = 0 lines. The bar chart reads `state.shadow_pnl.configs[i].open_positions` from the existing `/api/state` payload that the page already polls every 5s.

No trading-decision change. No change to `paper_trades` / `short_trade_history` storage, the shadow_pnl tracker, or any /api/* server logic. v5.5.9 is purely a Shadow-tab dashboard polish release.

---

## v5.5.8 — 2026-04-27

Smoking-gun summary: with v5.5.7 in place, the Main tab's classification rule finally treated SHORT/COVER as opens/closes — and the live NVDA short trade exposed the next layer of the bug. The header now read `0 opens · 1 close · realized −$28.32 · win 0%`, with only the COVER row visible, even though the trade had been a clean entry+exit pair. Root cause: the Main tab payload only ever carried the COVER. `dashboard_server._today_trades()` walks `paper_trades` (BUYs/SELLs) and `short_trade_history` (COVERs), but short *entries* are intentionally never written to either list — `short_trade_history` is the single source of truth for shorts and avoids double-counting on `/trades`. The dashboard had no row to render for the entry side because no row existed.

- fix (`dashboard_server._today_trades`): for every row in `short_trade_history` (today, post date-filter) we now emit BOTH a synthesized SHORT entry row built from the cover's embedded `entry_*` fields AND the existing COVER row. The synthesized row carries `action="SHORT"`, `side="SHORT"`, `shares`, `price`/`entry_price`, `time`/`entry_time` (the cover's `entry_time`, so the existing sort places it before the cover), `entry_time_iso`, `entry_num`, `date`, `cost = shares * entry_price`, and `portfolio="paper"`. No `pnl`/`exit_*` fields — it is an entry row by construction. The cover row's shape is unchanged. Both rows pass through the existing `_key`-based dedup so a stray COVER double-write would still collapse to a single row, and the synthesized SHORT entry is keyed by `action="SHORT"` so it cannot collide with any BUY/SELL/COVER.
- fix (open shorts): `_today_trades` also sweeps the live `short_positions` dict for entries dated today and emits a synthesized SHORT entry row for any ticker whose `(ticker, entry_time)` was not already covered by the previous loop. Live `short_positions` stores `entry_time` as `"HH:MM:SS"` while covers store it as `"HH:MM CDT"`; the synthesizer normalizes via `_to_cdt_hhmm(entry_ts_utc)` (with a defensive `HH:MM CDT` fallback for legacy positions missing `entry_ts_utc`) so an open-then-cover sequence on the same ticker does not double-emit.
- fix (sort): the close branch (`SELL`/`COVER`) now sorts by `exit_time` when no unified `time` field is set, so an entry+cover pair stays correctly ordered relative to a long BUY/SELL pair on the same day. Pre-fix, the COVER row used its `entry_time` as the sort key and could land before a BUY that fired *between* the SHORT entry and the COVER.
- header math (already correct on client after v5.5.7): `computeTradesSummary` treats `BUY`/`SHORT` as opens and `SELL`/`COVER` as closes, so a synthesized SHORT entry now flips the header from `0 opens · 1 close` to `1 open · 1 close · realized −$28.32 · win 0%` automatically — no JS change needed.
- render layer (already correct on client after v5.5.7): `renderTrades` keys row tails off `isOpen`/`isClose`, so the synthesized SHORT row's tail renders the cost (`shares * entry_price`) and the COVER row continues to render P&L.
- tests: 1 new test file —
  - `test_v5_5_8_main_short_entry_row.py` — closed short emits 2 rows (SHORT entry + COVER, correct field shape, sorted entry-before-cover); open short emits 1 row (entry only) with `entry_time` normalized to `HH:MM CDT`; open short dated yesterday is filtered out; long trade still emits 2 rows from `paper_trades` unchanged; mixed day (long pair + closed short + open short) yields 5 rows in correct chronological order; a stray COVER in `paper_trades` (defensive double-write case) still dedups to a single COVER row plus the synth SHORT entry; cover-then-fresh-open-short on the same ticker emits 3 rows (synth entry + cover + new open synth) without duplicating the entry leg.
  - Smoke version pin bumped from `5.5.7` → `5.5.8`. New in-suite smoke guard `v5.5.8: _today_trades synthesizes SHORT entry rows from short_trade_history` greps `dashboard_server.py` for the synthesis comment so a future refactor that drops the entry-row emit fails CI loudly. All v5.5.5 / v5.5.6 / v5.5.7 regression guards still pass unchanged.
- CI guard: `BOT_VERSION` bumped to `5.5.8` (matches this heading; the version-bump-check workflow gates on both). `CURRENT_MAIN_NOTE` rewritten for v5.5.8 (each line ≤ 34 chars), with the v5.5.7 Main-tab-fix entry pushed onto `_MAIN_HISTORY_TAIL`.
- docs: `ARCHITECTURE.md` dashboard section gained a one-paragraph note that Main's `trades_today` now emits paired entry+exit rows for shorts via synthesis from `short_trade_history` plus a sweep of `short_positions` (no storage change). `trade_genius_algo.pdf` regenerated via `scripts/build_algo_pdf.py`; cover now reads **v5.5.8**.

No trading-decision change. No change to `paper_trades` / `short_trade_history` storage, `_emit_signal` / `last_signal`, `evaluate_g4`, or the WS consumer. The storage invariant ("short opens are intentionally NOT appended") is preserved by design — v5.5.8 is purely a read-side synthesis in the dashboard payload.

---

## v5.5.7 — 2026-04-27

Smoking-gun summary: with v5.5.6 in place, NVDA executed a clean SHORT entry and a paired COVER exit on the paper book. Val's executor tab rendered the trade correctly — `LAST SIGNAL: EXIT_SHORT NVDA @ $208.53 · POLARITY_SHIFT` plus a paired entry+exit row with realized P&L. The Main tab, however, still showed `0 opens 0 closes realized — win —` and the COVER row's tail column was stuck on the em-dash placeholder, even though the row itself was visible. Root cause: purely client-side. `static/app.js` classified rows by literal `BUY`/`SELL` strings only, silently dropping `SHORT` opens and `COVER` closes from both the summary header and the row-tail P&L column. Separately, the Main panel had no LAST SIGNAL card at all — that surface only existed inside the per-executor (Val/Gene) panels, and the top-level `/api/state` payload didn't expose `last_signal` for the paper book.

- fix (`static/app.js` `computeTradesSummary`): treats `BUY` *or* `SHORT` as opens and `SELL` *or* `COVER` as closes. The realized-P&L branch now applies to any close action carrying a numeric `pnl`, so a SHORT+COVER pair finally contributes to the daily realized total and win-rate denominator. The pre-fix path produced `0 opens / 0 closes / realized —` for short trades.
- fix (`static/app.js` `renderTrades`): row-tail logic re-keyed off `isOpen` / `isClose` instead of `isBuy` / `isSell`. COVER rows now render `+/-$pnl  pnl%` in the tail column (matching SELL); SHORT rows render the cost (matching BUY). Action-chip class flips to `act-sell` for both SELL and COVER, `act-buy` for both BUY and SHORT.
- feat (`trade_genius._emit_signal` + `dashboard_server.snapshot`): `_emit_signal` now mirrors the most recent event into a module-level `last_signal` dict (kind / ticker / price / reason / timestamp_utc) before dispatching to listeners, so even a listener-less moment still updates what the Main tab renders. `snapshot()` reads it via `getattr(m, "last_signal", None)` and surfaces it on the top-level `/api/state` payload, mirroring the per-executor payload's `last_signal` field.
- feat (`dashboard_static/index.html` + `static/app.js`): new LAST SIGNAL card on the Main panel (`#last-sig-chip`, `#last-sig-body`) placed beside Today's Trades. New `renderLastSignal(s)` reads `s.last_signal` and renders kind / ticker / price / reason / timestamp in the same mono format the Val/Gene exec panels use; null/empty → "No signals received yet." Wired into the Main render loop alongside the existing renderers.
- tests: 1 new test file —
  - `test_v5_5_7_dashboard_main_fix.py` — Python mirror of the JS `computeTradesSummary` rule with assertions for SHORT+COVER (realized = -$28.32, 0 wins, 0% win rate), the legacy BUY+SELL path, a mixed long+short day, unknown-action ignore, empty-list, and close-without-pnl. Two server-side assertions cover the new surface: `/api/state` snapshot includes `last_signal` when `trade_genius.last_signal` is set, and `_emit_signal` mirrors the event into `trade_genius.last_signal` correctly.
  - Smoke version pin bumped from `5.5.6` → `5.5.7`. All v5.5.5 / v5.5.6 regression guards still pass unchanged.
- CI guard: `BOT_VERSION` bumped to `5.5.7` (matches this heading). `CURRENT_MAIN_NOTE` rewritten for v5.5.7 (each line ≤ 34 chars), with the v5.5.6 shadow-race entry pushed onto `_MAIN_HISTORY_TAIL`.
- docs: `ARCHITECTURE.md` dashboard section gained a one-paragraph note on the Main-tab `last_signal` surface and the open/close classification rule. `trade_genius_algo.pdf` regenerated via `scripts/build_algo_pdf.py`; cover now reads **v5.5.7**.

No trading-decision change. No change to `_today_trades()` data shape, `paper_trades` / `short_trade_history` storage, `evaluate_g4`, or the WS consumer. Pure client-side rendering plus a one-field server payload addition.

---

## v5.5.6 — 2026-04-27

Smoking-gun summary: with v5.5.5 in place, `/api/ws_state` proved that the WS feed was healthy — `volumes_size_per_symbol = 5` per ticker — yet every shadow log line still reported `cur_v=0` / `t_pct=0` / `qqq_pct=0` / `verdict=BLOCK`. Root cause: the shadow gate computed `session_bucket(datetime.now(ET))`, which returns the still-forming current minute. The Alpaca IEX websocket only delivers a 1-minute bar at the END of that minute, so reading `_ws_consumer.current_volume(ticker, current_bucket)` always raced the WS bar close-out and returned `None` (silently coerced to 0 by the `or 0` guard). The bug pre-existed v5.5.x; v5.5.5's observability is what finally made it visible. See `diagnostics/v55x_ws_silent_smoking_gun.md` for the full forensic timeline.

- fix (`volume_profile.previous_session_bucket`): new helper that floors `ts_et` to the minute boundary, subtracts one minute, and returns `session_bucket(prev)`. The just-closed minute IS in `_ws_consumer._volumes` within ~100 ms of close, so the shadow gate finally reads real volumes. Outside-session rules are inherited from `session_bucket` (premarket / weekend / holiday / post-close all return `None`). The bar-archive caller is intentionally NOT changed — it still uses `session_bucket(now_et)` because its job is to label the bar being archived right now via `et_bucket`.
- fix (shadow callers in `trade_genius.py`): four shadow-path call sites switched from `volume_profile.session_bucket(now_et)` to `volume_profile.previous_session_bucket(now_et)` — `_shadow_log_g4` (~L2326), the REHUNT_VOL_CONFIRM check (~L2743), the OOMPH_ALERT check (~L2830), and `_v512_emit_candidate_log` (~L3230). The pure functions `evaluate_g4` / `evaluate_g4_config` are unchanged: only what the shadow callers pass as `minute_bucket` was affected.
- tests: 2 new test files —
  - `test_v5_5_6_previous_session_bucket.py` — walks across a representative trading day in 30 s steps and asserts the returned bucket matches "the minute that just closed". Premarket / 09:31:00 / weekend / holiday / 16:01:00 / naive datetimes all return `None`. 16:00:00 and 16:00:30 both return `'1559'`.
  - `test_v5_5_6_shadow_uses_prev_bucket.py` — mocks `_ws_consumer` with `{AAPL: {'1026': 5000}}`, freezes wall clock to 10:27:30 ET, calls `_shadow_log_g4("AAPL", stage=1, existing_decision="HOLD")` and asserts the `[V510-SHADOW]` line carries `bucket=1026` and `ticker_pct=100` (derived from the WS bar in the just-closed bucket, NOT 0). Equivalent test for `_v512_emit_candidate_log`. Third test confirms an outside-session timestamp still returns silently (no `[V510-SHADOW]` emit).
  - 2 new in-suite smoke guards — `v5.5.6: previous_session_bucket exists and returns just-closed bucket` and `v5.5.6: shadow paths in trade_genius use previous_session_bucket` — the latter greps `trade_genius.py` for `previous_session_bucket(now_et)` inside the two named function bodies so a future refactor that reverts to the racey path fails CI loudly.
  - All v5.5.5 regression guards (WS observability, watchdog, archive source switch) still pass unchanged.
- CI guard: `BOT_VERSION` bumped to `5.5.6` (matches this heading; the version-bump-check workflow gates on both). `CURRENT_MAIN_NOTE` rewritten for v5.5.6 (each line ≤ 34 chars), with the v5.5.5 observability entry pushed onto `_MAIN_HISTORY_TAIL`.
- docs: `ARCHITECTURE.md` shadow section gained a one-paragraph note that the shadow gate evaluates the just-closed minute, not the still-forming one (current-bucket reads always race the IEX WS bar close-out). `trade_genius_algo.pdf` regenerated via `scripts/build_algo_pdf.py`; cover now reads **v5.5.6**.

No trading-decision change. No change to live entry logic, sizing, exit, or paper-book accounting. The bar-archive `et_bucket` field still uses the current minute (it labels the bar being written, not a read against future state).

---

## v5.5.5 — 2026-04-27

Smoking-gun summary: v5.5.4's WS handler was async, the connection stayed up, and `subscribe_bars` succeeded — but no `[VOLPROFILE]` log line ever fired in 11.5 hours of prod runtime. With zero observability we couldn't tell whether bars were never reaching `_on_bar`, whether an exception inside the handler was being swallowed silently, or whether the daemon-thread asyncio loop was starved. v5.5.5 closes that blind spot with bar-counter instrumentation, a watchdog, and a dashboard surface, and starts feeding the bar archive from the WS so `--validate` actually has IEX volumes to replay against. See `diagnostics/v55x_ws_silent_smoking_gun.md` for the full forensic timeline.

- feat (observability — `volume_profile.WebsocketBarConsumer`): every successful `_on_bar` call now bumps `self._bars_received`, stamps `self._last_bar_ts = datetime.now(UTC)`, and records exceptions in `self._last_handler_error` before the existing warning log. The first 5 bars emit `[VOLPROFILE] sample bar #N sym=… ts=… vol=… bucket=…` at INFO so an operator can see live data flowing within seconds of connect; every 100th bar emits `[VOLPROFILE] heartbeat: total=N last_sym=…`. New public methods `stats_snapshot()` (thread-safe; takes `self._lock`) and `time_since_last_bar_seconds()` expose the same numbers programmatically.
- feat (resiliency — WS watchdog): a daemon thread (`VolProfileWatchdog`) polls every 30 s. While the regular session is open (`session_bucket(now_et)` is not None), if no bar has arrived for ≥ `VOLPROFILE_WATCHDOG_SEC` (default 120, clamped to ≥ 30), the watchdog logs `[VOLPROFILE] watchdog: no bars for Ns (received=N) — forcing reconnect`, bumps `_watchdog_reconnects`, and calls `self._stream.stop()` so the existing `_run_forever` outer loop reconnects with backoff. Outside RTH the watchdog is a no-op. The loop is wrapped in `try/except` end-to-end — a watchdog-internal exception logs and continues so it can never silently die.
- feat (dashboard): new `GET /api/ws_state` returns `{available, bars_received, last_bar_ts, last_handler_error, volumes_size_per_symbol, tickers, watchdog_reconnects, silence_threshold_sec}`. Same `spike_session` cookie auth as `/api/state`; returns `{available: false}` when `_ws_consumer` is None (e.g., shadow disabled). No keys/secrets exposed.
- fix (bar archive — `trade_genius.py` ~L8166-8210): `iex_volume` now prefers `_ws_consumer.current_volume(ticker, bucket)` whenever the WS path is up and `session_bucket(now_et)` resolves; falls back to the existing Yahoo `vols[idx]` value otherwise. `et_bucket` is now populated from the same `session_bucket()` call (was hardcoded `None` since v5.5.2). Yahoo's intraday endpoint frequently returned `volume=null` on the leading-edge bar, leaving `--validate` replays running against zeroes; with v5.5.5 the WS source closes that gap whenever it is healthy.
- tests: 3 new test files exercising the surface end-to-end —
  - `test_v5_5_5_volprofile_observability.py` — `_on_bar` increments `_bars_received` on a valid bar; first 5 bars log sample lines; the 100th bar logs a heartbeat; an exception inside the handler body sets `_last_handler_error`; `stats_snapshot()` returns the expected keys with thread-safe access.
  - `test_v5_5_5_watchdog.py` — watchdog forces `_stream.stop()` after the silence threshold (mock time + mock stream); skipped outside RTH (mock `session_bucket` to None); a watchdog-internal exception is caught/logged and the loop keeps running.
  - `test_v5_5_5_archive_source.py` — when `_ws_consumer.current_volume` returns an int the archive entry uses it; when it returns None the archive falls back to Yahoo `vols[idx]`; `et_bucket` is now populated.
  - The v5.5.4 regression guard (`inspect.iscoroutinefunction(_on_bar)`) still passes — the new instrumentation lives inside the coroutine body and inside the (unchanged-signature) `except`.
- CI guard: `BOT_VERSION` bumped to `5.5.5` (matches this heading; the version-bump-check workflow gates on both). `CURRENT_MAIN_NOTE` rewritten for v5.5.5 (each line ≤ 34 chars), with the v5.5.4 handler-fix entry pushed onto `_MAIN_HISTORY_TAIL`.
- docs: `ARCHITECTURE.md` shadow section gained a paragraph on the watchdog + observability surface (heartbeat log lines + `/api/ws_state`). `trade_genius_algo.pdf` regenerated via `scripts/build_algo_pdf.py`; cover now reads **v5.5.5**.

---

## v5.5.4 — 2026-04-27

- fix (data pipeline / shadow): the shadow WS bar handler `volume_profile.WebsocketBarConsumer._on_bar` is now `async def`. alpaca-py's `StockDataStream.subscribe_bars()` requires its handler to be a coroutine function — registering a plain `def` raised `handler must be a coroutine function` inside `run()` and crash-looped the consumer every ~6 seconds (Railway logs: `[VOLPROFILE] websocket error: handler must be a coroutine function; reconnecting`). With v5.5.3 the cred lookup was finally resolving (`VAL_ALPACA_PAPER_KEY` picked up for all 10 tickers), but the connection couldn't stay up so `cur_v` stayed at 0 and no `shadow_positions` were recorded. The handler body itself is purely synchronous; only the function declaration needed to be a coroutine function so the SDK accepts it.
- tests: 1 new regression guard — `v5.5.4: shadow WS bar handler is a coroutine function` imports `volume_profile` and asserts `inspect.iscoroutinefunction(WebsocketBarConsumer._on_bar)`. A future refactor that drops `async` will fail this test loudly. Existing v5.5.3 smoke guards (DataFeed.IEX pin, cred-chain order, `[SHADOW DISABLED]` token) still pass — this hotfix doesn't touch them.
- CI guard: `BOT_VERSION` bumped to `5.5.4` (matches this heading; the version-bump-check workflow gates on both). `CURRENT_MAIN_NOTE` rewritten for v5.5.4 (each line ≤34 chars), with the v5.5.3 cred-fix entry pushed onto `_MAIN_HISTORY_TAIL`.
- docs: one-line note added to `ARCHITECTURE.md` shadow section noting the handler must be `async def`. PDF regen deferred — this is a single-bug hotfix; PDF will roll up at the next non-hotfix release.

---

## v5.5.3 — 2026-04-27

- fix (data pipeline / shadow): `_start_volume_profile()` now resolves Alpaca market-data credentials in the order `VAL_ALPACA_PAPER_KEY` / `VAL_ALPACA_PAPER_SECRET` → `ALPACA_PAPER_KEY` / `ALPACA_PAPER_SECRET` → `ALPACA_KEY` / `ALPACA_SECRET` → fail. Prod is configured with the `VAL_*` pair, so the legacy-only chain in v5.5.2 silently early-returned, leaving `_ws_consumer = None`, `cur_v = 0`, every G4 evaluation in `BLOCK / LOW_TICKER`, and `_v520_open_shadow` permanently unreachable. See `diagnostics/shadow_data_pipeline.md` Issue 2 for the full root-cause walk-through.
- constraint (architectural): the shadow path may read `VAL_ALPACA_PAPER_KEY/SECRET` **only for market data** — `/v2/stocks/*` REST and `wss://stream.data.alpaca.markets/v2/*` WS. Trading endpoints (`/v2/positions`, `/v2/account`, `/v2/orders`, `/v2/portfolio/history`) remain forbidden in this code path. Shadow positions stay in our own SQLite ledger (`shadow_positions`), never in Val's Alpaca account. An inline comment at the cred lookup pins this for future readers, and a new smoke test guards `volume_profile.py` against any future trading-API import (`TradingClient` / `TradingStream` / forbidden URL paths).
- feat (visibility): replace the soft `[VOLPROFILE] no Alpaca data credentials found; shadow gate will run with empty live volumes.` warning with an explicit `[SHADOW DISABLED] no Alpaca market-data credentials found (set VAL_ALPACA_PAPER_KEY/SECRET or ALPACA_PAPER_KEY/SECRET); shadow_positions will not record any rows this session.` log line. Module-level `SHADOW_DATA_AVAILABLE: bool` flag in `trade_genius.py` reflects whether the WS consumer started; `True` only after `_ws_consumer.start()` returns without raising.
- feat (frontend): `/api/state` now exposes `shadow_data_status: "live" | "disabled_no_creds"`, sourced from `trade_genius.SHADOW_DATA_AVAILABLE`. The Shadow strategies card-head renders a new `chip-warn` pill `SHADOW DISABLED — no market-data creds` whenever the status is `disabled_no_creds`, hidden otherwise. Existing `chip-warn` styling reused; no new CSS.
- audit (read-only): grepped `_start_volume_profile()`, the `_ws_consumer` (`volume_profile.WebsocketBarConsumer`), and `volume_profile.py` for `/v2/positions`, `/v2/account`, `/v2/orders`, `/v2/portfolio`, `TradingClient`, `TradingStream`. **Result: clean.** The shadow path uses only `alpaca.data.historical.StockHistoricalDataClient` (REST market data) and `alpaca.data.live.StockDataStream` with `feed=DataFeed.IEX` (market-data WS at `wss://stream.data.alpaca.markets/v2/iex`).
- tests: 3 new smoke guards —
  - `v5.5.3: BOT_VERSION bumped to 5.5.3`
  - `v5.5.3: shadow WS uses market-data feed (DataFeed.IEX), not trading WS` parses `volume_profile.py` and asserts (a) `StockDataStream` is the WS class, (b) `DataFeed.IEX` is pinned, and (c) no trading-API symbol (`TradingClient` / `TradingStream` / `/v2/positions` / `/v2/account` / `/v2/orders` / `/v2/portfolio`) appears anywhere in the file.
  - `v5.5.3: _start_volume_profile prefers VAL_ALPACA_PAPER_KEY over legacy` parses `trade_genius.py` and asserts the cred chain checks `VAL_ALPACA_PAPER_KEY` strictly before `"ALPACA_PAPER_KEY"`, and that the new `[SHADOW DISABLED]` log token is present.
- CI guard: `BOT_VERSION` bumped to `5.5.3` (matches this heading; the version-bump-check workflow gates on both). `CURRENT_MAIN_NOTE` rewritten for v5.5.3 (each line ≤34 chars), with the v5.5.2 bar-archive entry pushed onto `_MAIN_HISTORY_TAIL`.
- docs: `ARCHITECTURE.md` shadow section updated — cred-lookup chain (`VAL_*` → legacy → fail), market-data-only constraint on Val's Alpaca paper key, and the new `SHADOW_DATA_AVAILABLE` / dashboard surface. `trade_genius_algo.pdf` regenerated via `scripts/build_algo_pdf.py`.

---

## v5.5.2 — 2026-04-27

- fix (data pipeline): wire the bar archive writer into the scan loop. `_v512_archive_minute_bar()` was added in v5.1.2 at `trade_genius.py:3303-3325` but had **zero callers** — the wiring step was missed, so `/data/bars/` never existed on prod and the v5.4.0 backtest CLI had nothing to replay. The call now lives alongside the v5.2.1 H3 MTM hook in the per-ticker scan branch (~`trade_genius.py:8094-8150`), reusing the cached `fetch_1min_bars` result so it adds no network cost. The most-recently-completed bar is projected onto `bar_archive.BAR_SCHEMA_FIELDS` (canonical 11-field schema; downstream `backtest/loader.py` expects this exact shape) and persisted to `/data/bars/YYYY-MM-DD/{TICKER}.jsonl`. The call is wrapped in its own `try/except` so any archive failure logs `[V510-BAR] archive hook` and continues — archival must never disrupt the trading scan. See `diagnostics/shadow_data_pipeline.md` Issue 1 for the full root-cause analysis.
- feat (retention): invoke `bar_archive.cleanup_old_dirs(retain_days=90)` from `eod_close()` so archived bars don't accumulate forever on the Railway 1 GB volume. Failure-tolerant: a cleanup error logs at warning level and never raises.
- tests: 2 new smoke guards next to the existing v5.1.2 `bar_archive` block —
  - `v5.5.2: _v512_archive_minute_bar has a caller outside its own def` parses `trade_genius.py` and asserts the literal `_v512_archive_minute_bar(` appears at least once outside the def line. If a future refactor silently re-orphans the writer (the original v5.1.2 bug), this test fails loudly.
  - `v5.5.2: bar_archive.cleanup_old_dirs is invoked from eod_close` introspects `eod_close`'s source and asserts the cleanup call is present, so retention can't silently drop out either.
- CI guard: `BOT_VERSION` bumped to `5.5.2` (matches this heading; the version-bump-check workflow gates on both). `CURRENT_MAIN_NOTE` rewritten for v5.5.2 (each line ≤34 chars), with the v5.5.1 chart-interactivity entry pushed onto `_MAIN_HISTORY_TAIL`.
- docs: `ARCHITECTURE.md` bar-archive section updated to reflect that the writer is now wired, the call site lives in the scan-loop per-ticker branch, and 90-day retention runs at EOD. `trade_genius_algo.pdf` regen note: see PR description — manual regen may be required if `scripts/build_algo_pdf.py` cannot run in this environment.

---

## v5.5.1 — 2026-04-26

- feat (frontend): rich Chart.js tooltips on all three Shadow-tab chart groups. Equity curves now show `MM/DD HH:MM ET · ±$cum_pnl · config_name`; the day-P&L heatmap shows `config_name · YYYY-MM-DD · ±$pnl · N trades`; rolling win-rate sparklines show `config_name · trade #N · win_rate%`. Implemented via Chart.js's built-in `plugins.tooltip.callbacks` so mobile-tap tooltips work out of the box without a custom overlay layer.
- feat (frontend): click-to-isolate config. Clicking on any equity row, win-rate sparkline, or heatmap cell highlights that config across **all three** chart groups simultaneously — non-isolated configs fade to ~20% opacity. Click the same config again, click the heatmap empty area, or click the new "Showing only: GEMINI_A · click to clear" hint (with X button) above the charts to restore full opacity. Single `__scIsolated` state variable in the Shadow-tab module keeps all three groups in sync; mobile tap counts as click.
- docs: §21 (Shadow tab charts) in `ARCHITECTURE.md` extended with a paragraph describing the v5.5.1 interactivity additions (rich tooltips + click-to-isolate). `trade_genius_algo.pdf` regenerated; cover now reads **v5.5.1**.
- tests: 2 new smoke tests — `test_v551_tooltip_callbacks_present` (parses `app.js` and asserts a `plugins.tooltip.callbacks` block exists for each of the 3 chart constructors) and `test_v551_isolation_handler_present` (asserts a click handler that mutates `__scIsolated` exists). Existing version-pinned smoke assertions retargeted from `5.4.2` → `5.5.1`.
- CI guard: `BOT_VERSION` bumped to `5.5.1` (matches this heading; the version-bump-check workflow gates on both). `CURRENT_MAIN_NOTE` rewritten for v5.5.1 (each line ≤34 chars), with the v5.4.2 doc-refresh entry pushed onto `_MAIN_HISTORY_TAIL`.

---

## v5.4.2 — 2026-04-26

- docs: refresh `ARCHITECTURE.md` to reflect every shipped change between v5.3.0 (the previous arch-doc refresh, [PR #158](https://github.com/valira3/stock-spike-monitor/pull/158)) and v5.4.1. Header version v5.3.0 → v5.4.1; intro now covers v5.4.0 offline backtest CLI and v5.4.1 Shadow tab charts; §16.5 release table extended through v5.4.2 (adds v5.3.1 / v5.4.0 / v5.4.1 / v5.4.2 rows); §17 forensic-volume header + §17.7 rollout plan extended through v5.4.1; §19 G4 status updated to "shadow only … through v5.4.1"; §20 retained from v5.4.0 (offline backtest CLI); new §21 *Shadow tab charts* documents the v5.4.1 `GET /api/shadow_charts` endpoint, response shape (`equity_curve` / `daily_pnl` / `win_rate_rolling`), 30 s server-side cache, three Chart.js groups (equity curves, day-P&L heatmap, rolling 20-trade win-rate sparklines), stable per-config color palette, mobile-first collapsible "Charts" header, and tab-aware 60 s polling. `Source of truth` line now lists `backtest/{loader,ledger,replay,__main__}.py`. Last-refresh footer bumped to `BOT_VERSION = "5.4.2"`.
- docs: regenerate `trade_genius_algo.pdf` from the refreshed `ARCHITECTURE.md` via `scripts/build_algo_pdf.py`. Cover page now reads **v5.4.2**.
- CI guard: `BOT_VERSION` bumped to `5.4.2` (matches this heading; the version-bump-check workflow gates on both). `CURRENT_MAIN_NOTE` rewritten for v5.4.2 (each line ≤34 chars), with the v5.4.1 charts entry pushed onto `_MAIN_HISTORY_TAIL`.
- tests: two version-pinned smoke assertions retargeted from `5.4.1` → `5.4.2`. No other test changes — Val confirmed no test run needed for a doc-only PR.

---

## v5.4.1 — 2026-04-26

- feat (backend): new `GET /api/shadow_charts` endpoint in `dashboard_server`. Returns three blocks per `SHADOW_CONFIG` (`equity_curve`, `daily_pnl`, `win_rate_rolling`) sourced from the persisted `shadow_positions` SQLite table — closed trades only (`exit_ts_utc IS NOT NULL`). Cached for 30 s using the same lock-protected `(ts, payload)` pattern as `/api/indices`, so multiple browsers polling the Shadow tab in parallel collapse to one SQLite read per window. Always emits all 7 configs in a fixed order; configs with no closed trades render as empty arrays rather than missing keys. Same session-cookie auth as the rest of `/api/*`.
- feat (frontend): the Shadow tab now renders three vertically-stacked chart groups above the existing per-config rows. (1) Equity curves — one Chart.js line chart per config (~100 px desktop, ~80 px mobile), Y-axis cumulative $, X-axis time. (2) Day-P&L heatmap — single ~300 px scatter chart, rows = configs, columns = trading days, cell color = green/red intensity scaled to abs-max P&L across all cells. (3) Rolling win-rate sparklines — one per config (~60 px), Y-axis 0–1, hidden if a config has < 20 closed trades.
- feat (frontend): each config gets a stable hue across all three groups (`SHADOW_CFG_COLORS`) so `GEMINI_A`'s equity curve, heatmap row, and win-rate sparkline are always the same color. Axis colors and gridlines read from existing CSS variables (`--text-dim`, `--border`); no new color literals.
- feat (frontend): Chart.js 4.4.0 is loaded from jsDelivr CDN with a `defer` attribute. The chart code falls back gracefully if `window.Chart` is undefined — empty wrappers render and the rest of the dashboard keeps working.
- feat (frontend): "Charts" header is collapsible. Click / Enter / Space toggles. Default is expanded on desktop and collapsed on ≤ 720 px viewports so the Shadow tab is not dominated by chart real estate on a phone.
- feat (frontend): tab-aware polling. `/api/shadow_charts` is fetched once on Shadow-tab activation and then every 60 s **only while the Shadow tab is active** — Main / Val / Gene ticks skip the call entirely. Matches the existing `pollExecutor` pattern.
- tests: 3 new smoke tests — `test_v541_shadow_charts_endpoint`, `test_v541_shadow_charts_cache`, `test_v541_shadow_charts_html_present`.
- CI guard: `BOT_VERSION` bumped to `5.4.1` (matches this heading; the version-bump-check workflow gates on both).

---

## v5.4.0 — 2026-04-26

- Added offline backtest CLI: `python -m backtest.replay` with replay-vs-prod validation mode. See ARCHITECTURE.md for usage.

---

## v5.3.1 — 2026-04-26

- docs: refresh `ARCHITECTURE.md` to reflect every shipped change between v5.1.2 (the previous arch-doc refresh, PR #147) and v5.3.0. New / updated sections: header version + intro; repo layout (adds `persistence.py`, `shadow_pnl.py`); §8.4 Alpaca order idempotency (v5.2.1 H1 — deterministic `client_order_id` + `_reconcile_broker_positions`); §9.4 shadow strategy P&L (v5.2.0); §9.5 Shadow tab (v5.3.0 — Main / Val / Gene / Shadow); §10.3 market data (Yahoo as sole 1m bar source after v5.1.3 Finnhub removal); §10.4 persistence (`STATE_DB_PATH=/data/state.db`, v5.1.8); §10.8 forensic-volume env vars now reflect 7-config state (5 in `SHADOW_CONFIGS` + `REHUNT_VOL_CONFIRM` / `OOMPH_ALERT`); §11.1 Dockerfile whitelist (adds `persistence.py`, `shadow_pnl.py`); §12 persistence (SQLite store + JSON-import migration path); §13.1 testing (262/262 at v5.3.0); §16.5 v5.0.x → v5.3.x change-summary table; §17 forensic volume filter rewritten end-to-end with the v5.1.6 `BUCKET_FILL_100` row, the v5.1.9 event-driven extras, and the v5.2.1 M3 `_v521_all_shadow_config_names()` registry; §17.7 rollout plan extended through v5.3.0; §19 G4 status updated.
- docs: regenerate `trade_genius_algo.pdf` from the refreshed `ARCHITECTURE.md` via `scripts/build_algo_pdf.py`. Cover page now reads **v5.3.1**.
- CI guard: `BOT_VERSION` bumped to `5.3.1` (matches this heading; the version-bump-check workflow gates on both).
- No code-logic change; smoke suite remains 262/262.

---

## v5.3.0 — 2026-04-26

- feat (Part 1): new top-level **Shadow** tab in the dashboard tab strip, ordered Main / Val / Gene / Shadow. The button mirrors the existing tab styling and a fresh `#tg-panel-shadow` div hosts the panel. `app.js` now declares `TABS = ["main","val","gene","shadow"]`; `selectTab("shadow")` warms `/api/state` and re-renders the shadow card on first visit so the panel paints immediately.
- feat (Part 2): the v5.2.0 Shadow strategies card moved out of the bottom of the Main panel and into the new Shadow tab. The v5.2.0 main-only CSS gate (`body[data-tg-active-tab="val|gene"] #shadow-pnl-card`) was replaced with an explicit Shadow-only gate so the card no longer renders on Main, Val, or Gene.
- feat (Part 3): every config row in the Shadow card is now expandable. Click (or Enter/Space) toggles a per-config detail block that lists open shadow positions (ticker, side, qty, entry, mark, $ + % unrealized, entry HH:MM ET) and the last 10 closed trades (ticker, side, qty, entry, exit, $ + % realized, exit reason, exit HH:MM ET). Multiple rows may be expanded at once; expanded state survives state-poll re-renders via `__shadowExpanded`.
- feat (backend): `shadow_pnl.ShadowPnL` exposes two new helpers — `open_positions_for(config_name)` and `recent_closed_for(config_name, limit=10)` — that snapshot the in-memory `_open` / `_closed` state as plain dicts. `dashboard_server._shadow_pnl_snapshot` now embeds `open_positions` and `recent_trades` lists on every config row of the `shadow_pnl` block in `/api/state`. No schema migration required — the underlying `shadow_positions` SQLite table is unchanged.
- tests: 3 new smoke tests in the `# === v5.3.0 Shadow tab ===` section — `test_v530_shadow_tab_html_present`, `test_v530_shadow_card_not_on_main`, `test_v530_shadow_detail_endpoint`.
- CI guard: `BOT_VERSION` bumped to `5.3.0`.

---

## v5.2.1 — 2026-04-26

- fix (H1): every `client.submit_order(...)` call in `TradeGeniusBase._on_signal` now carries a deterministic `client_order_id` of the form `f"{NAME}-{ticker}-{utc_iso_minute}-{direction}"` (NAME ∈ `VAL`/`GENE`, ticker sanitized to alphanumeric upper-case, minute precision is sufficient because the scanner is single-writer). Closes the timeout-after-accept double-up failure mode: if Alpaca's HTTP layer raises after the broker has already accepted an order, a retry with the same coid is rejected with HTTP 422 and the bot now treats that rejection as success (looks the original order up via `client.get_order_by_client_id` and proceeds). Re-raises any non-coid APIError so genuine submit failures still surface. New helpers: `_build_client_order_id`, `_submit_order_idempotent`. Both `ENTRY_LONG` and `ENTRY_SHORT` paths route through the wrapper.
- fix (H1): new `_reconcile_broker_positions(self)` method on `TradeGeniusBase` runs once at executor boot (after `_build_alpaca_client` succeeds, before the scan loop subscribes), pulls `client.get_all_positions()`, and grafts every broker-side ticker missing from the new `self.positions` dict with `source="RECONCILE"`, reconstructed `side`/`qty`/`entry_price`/`entry_ts_utc`, and `stop=None`/`trail=None` (next MTM cycle rebuilds them from current price). Each graft logs a `[RECONCILE]` WARN; if any orphans were grafted the executor fans a one-line Telegram alert via the existing `_send_own_telegram` owner-notify path. Wrapped in try/except so a bad reconcile never blocks scanner startup. Runs independently for Val and Gene.
- feat: new `_record_position(ticker, side, qty, entry_price)` helper stamps a `source="SIGNAL"` entry into `self.positions` after a successful submit, so subsequent reconciles can tell apart bot-originated trades from broker orphans.
- tests: 5 new local smoke tests in the new `# === v5.2.1 Idempotency + Reconcile ===` section — `test_v521_client_order_id_present`, `test_v521_duplicate_coid_rejected_as_success`, `test_v521_timeout_after_accept_no_doubleup`, `test_v521_reconcile_grafts_orphans`, `test_v521_reconcile_skips_known`.
- fix (H3): shadow MTM now runs **unconditionally** for every ticker each scan cycle. Previously `_v520_mtm_ticker` was nested inside the `if not paper_holds:` branch in `scan_loop()`, so the moment paper opened a position on a ticker every shadow position on that same ticker stopped getting marked. The MTM call is now a sibling block that fires after `fetch_1min_bars(ticker)` regardless of paper state; only the entry-decision path stays gated.
- fix (H2): `ShadowPnL.close_all_for_eod` previously `continue`d on any open position whose ticker was missing from the per-ticker `prices` dict, leaving orphaned shadow positions open in SQLite forever and marking them against a stale `entry_price` the next session. EOD now force-closes every still-open shadow position whose ticker has no mark using its own `entry_price` as the exit (realized P&L = 0 by definition), with `exit_reason="EOD_NO_MARK"` and a WARN log per orphan. The live `eod_close` hook in `trade_genius.py` also no longer requires `last_mark_price` to be set when constructing the per-ticker `prices` dict — it falls back to `entry_price` so every config gets some mark, matching the live long/short EOD pattern.
- fix (M3): `_v520_close_shadow_all` no longer enumerates a hardcoded subset of config names. The fanout now iterates the canonical registry — `SHADOW_CONFIGS` plus the event-driven extras (`REHUNT_VOL_CONFIRM`, `OOMPH_ALERT`) — through a single helper so future configs are picked up automatically.
- fix (M4): `_v519_rehunt_watch` is now keyed on `(ticker, side)` instead of `ticker` alone. Long+short whipsaws on the same ticker on the same minute previously clobbered one of the two arms; both arms now coexist and are evaluated independently.
- tests: 4 new local smoke tests (`test_v521_eod_orphan_force_close`, `test_v521_shadow_mtm_runs_when_paper_holds`, `test_v521_close_shadow_all_iterates_registry`, `test_v521_rehunt_watch_long_short_coexist`) cover the four shadow-accounting fixes above.
- CI guard: `BOT_VERSION` bumped to `5.2.1`.

---

## v5.2.0 — 2026-04-26

- feat: real-time mark-to-market P&L tracker for all 7 SHADOW_CONFIGS (`TICKER+QQQ`, `TICKER_ONLY`, `QQQ_ONLY`, `GEMINI_A`, `BUCKET_FILL_100`, `REHUNT_VOL_CONFIRM`, `OOMPH_ALERT`). Each config now owns a per-process virtual portfolio: when a config's would-have-entered verdict fires, a virtual position is sized via the v5.1.4 equity-aware formula (`min(dollars_per_entry, equity * max_pct/100, cash - min_reserve)`) using Val's LIVE executor's account so shadow P&L is directly comparable to live bot P&L. Each open position is marked-to-market every scan cycle from the IEX 1m close; exits mirror the live bot's `HARD_EJECT_TIGER` / trail / structural-stop / EOD path one-for-one (close hook lives in `close_breakout`, EOD hook in `eod_close`).
- feat: new dashboard panel at the bottom of the main dashboard (`/`) with two columns per config — "Today" (intraday only) and "Cumulative" (since v5.2.0 deploy). Each row shows `n=…`, win rate, realized P&L, and unrealized in parentheses when there's an open position. Best-performing config today is highlighted green; worst red. A bolded `LIVE BOT (Val)` row sits below the configs for direct comparison. Mobile (iPhone Pro Max + iPhone 13) wraps to a 2-column card layout under 560 px.
- feat: new `shadow_pnl.py` module owns the per-process virtual-portfolio store. Public API: `open_position`, `mark_to_market`, `close_position`, `close_all_for_eod`, `summary`. Thread-safe (RLock); failure-tolerant (every public method swallows and logs internal errors so a bad equity snapshot or stale price never takes down the live trading path). Singleton accessor: `shadow_pnl.tracker()`.
- feat: persistence extended with two new SQLite artifacts (`shadow_positions` table + `idx_shadow_open` / `idx_shadow_today` indexes) plus four helpers — `save_shadow_position`, `update_shadow_position_close`, `load_open_shadow_positions`, `load_shadow_positions_since`. State survives restarts: at boot, every open row is rehydrated into memory and every row whose `entry_ts_utc >= DEPLOY_TS_UTC` is reloaded into `_closed` so cumulative totals don't reset.
- The 7 SHADOW_CONFIGS are unchanged in scope. Open hooks fire from `_shadow_log_g4` (the 5 v5.1.6 configs, on stage-1 candidates where `existing_decision == ENTER` AND that config's verdict is PASS) and from the `_v519_check_rehunt` / `_v519_check_oomph` emit sites (REHUNT and OOMPH).
- No live trading behavior change. `VOL_GATE_ENFORCE=0` default preserved. Health-pill count and `#h-tick` countdown are untouched.
- tests: 10 new local smoke tests cover sizing, full open→MTM→close lifecycle, short-side direction, persistence round-trip, today vs cumulative split, dedup, dashboard snapshot wiring, best/worst highlight selection, BOT_VERSION bump, and the new `shadow_positions` schema. All 248 local tests pass (was 238).
- amend: shadow sizing now reads paper-portfolio equity (`paper_cash + sum(long_mv) - sum(short_liab)`) instead of the live Alpaca account snapshot. Shadow flow is now 100% paper-portfolio-driven \u2014 no Alpaca round-trip in the shadow open path. New helper `_v520_paper_equity_snapshot()` replaces `_v520_equity_snapshot()`; new env vars `PAPER_MAX_PCT_PER_ENTRY` (default `10.0`) and `PAPER_MIN_RESERVE_CASH` (default `500.0`) mirror the v5.1.4 live executor caps for the paper book.
- amend: bottom comparison row in the shadow panel renamed from "LIVE BOT (Val)" to "PAPER BOT" (the same paper portfolio whose equity now drives shadow sizing). Dashboard snapshot key renamed `live_bot` \u2192 `paper_bot`; `app.js` keeps a `paper_bot || live_bot` fallback so a stale browser tab still renders during rollout.
- amend: shadow panel renders ONLY on the Main tab. Body-scoped CSS rule on `[data-tg-active-tab]` hides `#shadow-pnl-card` and `#shadow-pnl-section` when Val or Gene is active.
- tests: 2 new local smoke tests cover the paper-equity snapshot formula and the Main-tab-only panel gate. Local total now 250.

---

## v5.1.9 — 2026-04-26

- feat: `REHUNT_VOL_CONFIRM` added as a 6th shadow config. Pure observation, NOT enforced. Event-driven (not per-minute): when a position closes via `HARD_EJECT_TIGER`, the same ticker is watched for the next 10 minutes. On the FIRST 1-min bar inside that window where `cur_volume / per-minute baseline median >= 100%` AND DI on the exit side is still > 25, one `[V510-SHADOW][CFG=REHUNT_VOL_CONFIRM]` line is emitted with `ticker, side, exit_ts, rehunt_offset_min, vol_pct, di_plus, di_minus, shadow_entry_price`. The Saturday backtest report pairs the shadow re-entry to the next exit signal and computes P&L. Apr 20-24 backtest verdict: +$21.56 / +4.3% net swing across 12 confirmed re-hunts (67% win rate); shipping in shadow because the sample is tiny and two outliers (MSFT 11:48, AMZN 12:16) lost −$86 between them.
- feat: `OOMPH_ALERT` added as a 7th shadow config. Pure observation, NOT enforced. Per-minute gate that inverts which minute carries the volume burden: minute 1 requires DI+ > 25 (long) OR DI- > 25 (short) AND `BUCKET_FILL >= 100%` SIMULTANEOUSLY; minute 2 requires DI > 25 only on the same side (no volume check). Today's flow does the opposite: minute 1 is DI-only and minute 2 is DI+volume. On a minute-2 confirmation, one `[V510-SHADOW][CFG=OOMPH_ALERT]` line is emitted with `ticker, side, minute1_ts, minute1_di, minute1_vol_pct, minute2_ts, minute2_di, shadow_entry_price`. Per-ticker prev-minute qualification state is held in memory; non-qualifying minutes clear the carry. Untested in backtest — awaiting May 9 weekly report.
- The five v5.1.6 shadow configs (TICKER+QQQ, TICKER_ONLY, QQQ_ONLY, GEMINI_A, BUCKET_FILL_100) are unchanged.
- No live trading behavior change. `VOL_GATE_ENFORCE=0` default preserved.

---

## v5.1.8 — 2026-04-26

- feat: SQLite persistence for `fired_set` (timed-job idempotency) and `v5_long_tracks` (Tiger/Buffalo paper-trade state). New module `persistence.py` wraps a WAL-mode SQLite store at `STATE_DB_PATH` (default `/data/state.db` on Railway). Replaces the in-memory `fired = set()` in `scheduler_thread()` so an EOD job that fires before a Railway container restart at 15:59:30 ET cannot double-fire at 16:00 after the container comes back up. Also replaces the non-atomic `json.dump` of `v5_long_tracks` / `v5_short_tracks` inside `paper_state.json` so a crash mid-write can no longer corrupt the wider portfolio file.
- feat: helpers `mark_fired(job_key)` / `was_fired(job_key)` / `prune_fired(prefix)` replace the in-memory set; `save_track`, `load_track`, `load_all_tracks(direction)`, `replace_all_tracks(long, short)` replace the JSON path.
- feat: every write runs inside `BEGIN IMMEDIATE … COMMIT`; `PRAGMA journal_mode=WAL` + `synchronous=NORMAL` so dashboard reads do not block the writer.
- feat: one-shot migration on startup. If `paper_state.json` already contains `v5_long_tracks` / `v5_short_tracks` keys, they are imported once into SQLite then the source file is renamed to `paper_state.json.migrated.bak` so a subsequent boot does not re-apply it. Idempotent — re-runs are a no-op.
- env: new `STATE_DB_PATH` (default `/data/state.db`). Documented in `.env.example`.
- tests: round-trip + transaction-rollback unit tests for both tables; existing v5-track round-trip and legacy-v4 paper-state tests adjusted to point at the SQLite-backed store; smoke `version: BOT_VERSION` updated to 5.1.8.

---

## v5.1.6 — 2026-04-26

- feat: `BUCKET_FILL_100` added as 5th shadow config (ticker ≥100% AND qqq ≥100% bucket fill). Pure observation, NOT enforced. Defaults unchanged.
- feat: new `[V510-VEL]` log captures the second-mark when ticker running volume first crosses 100% of its bucket within a candle. Validates the "fires at second 40" velocity insight in shadow-mode reports.
- feat: new `[V510-IDX]` log captures SPY+QQQ close vs PDC on every candidate. Required for full L-P1 / S-P1 validation in shadow.
- feat: new `[V510-DI]` log captures DI+/DI- (current and t-1) on every candidate. Required for L-P2 / S-P2 "double-tap" validation in shadow.
- feat: `indicators.py` adds `di_plus(bars, period=14)` and `di_minus(bars, period=14)` using Wilder's smoothing.
- No live trading behavior change. All paths still controlled by existing env vars; `VOL_GATE_ENFORCE=0` default preserved.

---

## v5.1.5 — 2026-04-26

- fix: /test command no longer times out with "Command failed: Timed out". Removed per-step `edit_text` calls inside the loop; progress message is now updated once at completion. Eliminates Telegram per-chat edit rate-limit race that surfaced as cosmetic httpx ReadTimeout. Underlying _test_* steps were always healthy. Adds TimedOut fallback to send a fresh reply if the final edit still fails.

---

## v5.1.4 — 2026-04-25

- feat: equity-aware sizing for live executors. Each entry now sized as `min(DOLLARS_PER_ENTRY, equity * MAX_PCT_PER_ENTRY/100, cash - MIN_RESERVE_CASH)`. Defaults: `MAX_PCT_PER_ENTRY=10.0`, `MIN_RESERVE_CASH=500`. Falls back to legacy fixed-size sizing if `get_account()` fails. Paper book unchanged. Logs `[SIZE_CAPPED]` when scaled down, `[INSUFFICIENT_EQUITY]` when can't afford even 1 share within caps.

---

## v5.1.3 — 2026-04-25

- chore: removed unused Finnhub SPY-quote fallback from /health diagnostic. FMP already provides SPY in the same diagnostic. No trading-path impact. `FINNHUB_TOKEN` env var no longer read.

---

## v5.1.2 — 2026-04-26 — Forensic capture (Tier-1 + Tier-2) + GEMINI_A as 4th shadow config — STILL SHADOW MODE.

**Why this exists.** Two motivations rolled into one release. First, after the Apr 20-24 backtest replay of Gene's Gemini-suggested configs, **GEMINI_A (ticker ≥110% AND QQQ ≥85%)** emerged as the only config with positive net P&L swing vs unfiltered (9 trades, +$497.92, 78% win rate, +$1.86 net swing). Val wants live shadow data on it next week alongside the three v5.1.1 configs so the post-hoc analysis includes it cleanly. Second, Val asked: "what additional data should we record so we can replicate, run scenarios, and backtest options at a later date?" The audit identified meaningful gaps: today we only log when a trade fires, only at the candidate moment, only at the active threshold, and we don't persist the underlying 1m bars or the indicator state at decision time. v5.1.2 closes those gaps so any future backtest is fully replayable from disk.

**(1) GEMINI_A added as 4th `SHADOW_CONFIGS` entry.** `volume_profile.SHADOW_CONFIGS` is now a 4-tuple: `TICKER+QQQ` 70/100, `TICKER_ONLY` 70, `QQQ_ONLY` 100, **`GEMINI_A` 110/85** (ticker ≥110% AND QQQ ≥85%). `_shadow_log_g4` now emits **4** `[V510-SHADOW][CFG=...]` lines per candidate (was 3). The original `[V510-SHADOW]` back-compat line is preserved unchanged. Existing v5.1.1 smoke test that asserted "3 lines" is updated to assert "4 lines"; new test asserts GEMINI_A is present with 110/85 thresholds.

**(2) Tier-1 (T1.1): 1m bar JSONL persistence.** New module `bar_archive.py`. For every minute close per ticker (+ QQQ + SPY) that is in the active TICKERS list, append one JSONL line to `/data/bars/YYYY-MM-DD/{TICKER}.jsonl`. Schema: `{ts, et_bucket, open, high, low, close, iex_volume, iex_sip_ratio_used, bid, ask, last_trade_price}`. Append in `a` mode (atomic per line on Linux ext4 for sub-PIPE_BUF writes — no tmp+rename needed). Lazy directory creation. Failure-tolerant — never raises into the trading loop. Disk usage projection: ~18 tickers × 390 minutes × ~150 bytes = ~1MB/day. 30-symbol IEX cap guard inherited from v5.1.0. Stale/empty minute = no line written. Nightly cleanup keeps last 90 days; older dated directories are removed.

**(3) Tier-1 (T1.2): every-minute volume-percentile log.** New `[V510-MINUTE]` prefix. Emitted once per minute per ticker on bar close, regardless of candidate state: `[V510-MINUTE] ticker=AMD bucket=1448 t_pct=84 qqq_pct=112 close=346.19 vol=12345`. This lets us replay "what if the candidate threshold itself were different" without re-pulling 1m bars from Alpaca.

**(4) Tier-1 (T1.3): skipped-candidate logging.** Today we only log candidates that fire. v5.1.2 closes this asymmetric blind spot. New `[V510-CAND]` prefix, emitted on **every entry consideration** — fired AND not-fired. Format: `[V510-CAND] ticker=AMD bucket=1448 stage=1 fsm_state=ARMED entered=NO reason=NO_BREAKOUT t_pct=84 qqq_pct=112 close=346.19 stop=null rsi14=null ema9=null ema21=null atr14=null vwap_dist_pct=null spread_bps=null`. Reason is enumerated: `NO_BREAKOUT`, `STAGE_NOT_READY`, `ALREADY_OPEN`, `COOL_DOWN`, `MAX_POSITIONS`, `BREAKOUT_CONFIRMED`. Wired into the entry-consideration loop next to the existing `_shadow_log_g4` call.

**(5) Tier-1 (T1.4): entry log line carries bid/ask + account state.** When a trade fires, a new `[V510-ENTRY]` line is emitted alongside the existing entry surface (Telegram + paper_log). Fields: `bid, ask, cash, equity, open_positions, total_exposure_pct, current_drawdown_pct`. **Strictly additive** — the existing entry log line, paper_log entry, and Telegram card are unchanged byte-for-byte (the synthetic harness 50/50 byte-equal goldens still pass).

**(6) Tier-2 (T2.1): FSM state-transition log.** New `[V510-FSM]` prefix. Emitter `_v512_log_fsm_transition` is a pure observation hook, refuses to emit on `from == to` no-ops (asserted by a smoke test). Format: `[V510-FSM] ticker=AMD from=IDLE to=WATCHING reason=VOL_SPIKE_DETECTED bucket=1445`. v5.1.2 ships the emitter; the wider FSM-call-site sweep is intentionally minimal so we don't accidentally change v5.0.0 Tiger/Buffalo behavior. Future PR will fan out the emitter to every transition site.

**(7) Tier-2 (T2.2): pre-trade indicator snapshots.** New module `indicators.py`: pure functions `rsi14`, `ema9`, `ema21`, `atr14`, `vwap_dist_pct`, `spread_bps`. All return `None` (rendered as `null` in logs, **not zero**) when there are insufficient bars. Wired into `[V510-CAND]` so every candidate moment carries the indicator state at decision time.

**(8) Out of scope.** News/halt flags (needs Polygon or Benzinga subscription); L2 / order-book snapshots; tick-level trades; enabling enforcement (`VOL_GATE_ENFORCE` stays `0`); new env-driven configs beyond v5.1.1; adaptive runtime config switching. Deferred per brief.

**(9) Defaults preserve v5.1.1 behavior.** Nothing in v5.1.2 changes the trading decision. `VOL_GATE_ENFORCE=0` is still the default. The four observation log streams (`[V510-MINUTE]`, `[V510-CAND]`, `[V510-FSM]`, `[V510-ENTRY]`) are pure additions — none of them affect entry/exit, position sizing, or stop placement. Existing 194/194 smoke tests still pass; synthetic harness 50/50 byte-equal still passes.

**(10) Smoke tests.** New v5.1.2 section adds 16+ tests: `SHADOW_CONFIGS` is now a 4-tuple with GEMINI_A correctly configured at 110/85; `_shadow_log_g4` emits exactly 4 `[CFG=...]` lines per candidate; `evaluate_g4_config` PASS/BLOCK paths for GEMINI_A; `bar_archive.write_bar` writes the expected file path with valid JSON schema; `bar_archive.cleanup_old_dirs` keeps recent and deletes old; `indicators.rsi14` / `ema9` / `ema21` / `atr14` / `vwap_dist_pct` / `spread_bps` happy-path + insufficient-bars-returns-None; `[V510-MINUTE]` emitter format; `[V510-CAND]` emits on entered=YES and entered=NO with all indicator fields; `[V510-FSM]` emits on transition, NOT on no-op; `[V510-ENTRY]` emitter format; Dockerfile COPY contains `indicators.py` and `bar_archive.py` (v5.0.2 infra-guard). Test count: **194 → 210+**.

---

## v5.1.1 — 2026-04-26 — Env-driven A/B toggles + 3-config parallel shadow logging — STILL SHADOW MODE.

**Why this exists.** v5.1.0 (PR #144, squash `5776007f`) shipped the forensic volume gate in shadow mode hard-coded at ticker ≥120% AND QQQ ≥100%. The Apr 20-24 backtest (38 entries, 18 tickers) showed the as-spec 120/100 thresholds would have killed 79% of trades for only 81% upside retention (−$93.65 P&L swing), while **70%/100% is the best risk-adjusted config**: 11 trades, +$482.90, 82% win rate (9W / 1L), keeps 96% of the upside. QQQ ≥100% is the heavy lifter; ticker threshold barely matters once QQQ is pinned. Val wants a clean A/B next week (Apr 27 – May 1) **with and without the index anchor**, so v5.1.1 makes the shadow gate env-driven and adds three parallel shadow verdicts per candidate so a single week of live data can be analysed cleanly post-hoc — no env-var flipping mid-week.

**(1) Env-driven active config.** New env vars (read at module import via `volume_profile.load_active_config()`): `VOL_GATE_ENFORCE` (default `0`, master enforcement flag — stays 0 all next week), `VOL_GATE_TICKER_ENABLED` (default `1`), `VOL_GATE_INDEX_ENABLED` (default `1`), `VOL_GATE_TICKER_PCT` (default `70`), `VOL_GATE_QQQ_PCT` (default `100`), `VOL_GATE_INDEX_SYMBOL` (default `QQQ`, hard-locked to QQQ per Val's call). The garbage-input parser falls back to defaults rather than crashing on a typo. **Defaults preserve current v5.1.0 behavior** — same anchors enabled, same recommended thresholds, no enforcement.

**(2) 3-config parallel shadow logging.** On every candidate entry the bot now emits three structured shadow log lines, one per fixed analysis config — `TICKER+QQQ` at 70/100, `TICKER_ONLY` at 70, `QQQ_ONLY` at 100. The three configs are hard-coded module constants (`volume_profile.SHADOW_CONFIGS`) and are NOT env-driven; env vars only control which one is the "active" (potentially-enforcing) config. Format example:

```
[V510-SHADOW][CFG=TICKER+QQQ][PCT=70/100] ticker=AMD bucket=1448 stage=1 t_pct=84 qqq_pct=112 verdict=PASS reason=OK entry_decision=ENTER
[V510-SHADOW][CFG=TICKER_ONLY][PCT=70] ticker=AMD bucket=1448 stage=1 t_pct=84 verdict=PASS reason=OK entry_decision=ENTER
[V510-SHADOW][CFG=QQQ_ONLY][PCT=100] ticker=AMD bucket=1448 stage=1 qqq_pct=112 verdict=PASS reason=OK entry_decision=ENTER
```

Verdict ∈ {`PASS`, `BLOCK`}. Reason ∈ {`OK`, `LOW_TICKER`, `LOW_QQQ`, `STALE_PROFILE`, `NO_BARS`, `NO_PROFILE`, `DISABLED`}. Lines emit on **every** candidate, regardless of which config is currently active in env, so end-of-week grep + post-hoc analysis is a pure observation of all three configs against the same live-volume timeline.

**(3) New helper `evaluate_g4_config`.** `volume_profile.evaluate_g4_config(ticker, minute_bucket, current_volume, profile, index_current_volume, index_profile, *, ticker_enabled, index_enabled, ticker_pct, index_pct)` returns `{verdict, reason, ticker_pct, qqq_pct}`. Per-anchor configurable evaluator used for the parallel shadow lines; the existing `evaluate_g4` (fixed 120/100 thresholds, the `green/reason/ticker_pct/qqq_pct/rule` shape) is unchanged so v5.1.0 grep tooling and the synthetic harness 50/50 byte-equal test still pass.

**(4) Original `[V510-SHADOW]` line preserved.** The `_shadow_log_g4` hook still emits the v5.1.0 line (no `[CFG=...]` prefix) in addition to the three new config lines, so the v5.1.0 backtest grep + Apr 20-24 tooling continues to work unchanged. Back-compat is asserted by a new smoke test.

**(5) Implementation note: env read at startup, not per-request.** Env vars are read by `load_active_config()` on each call (cheap dict lookup; no side-effects), but the design intent is "set once at deploy, don't flip mid-week". If Val needs to flip mid-week he redeploys. The three analysis configs are fixed module constants regardless of env; that's the point — every line of next week's data is comparable across configs.

**(6) Smoke tests.** 13 new tests in the v5.1.1 section: `load_active_config` defaults preserve v5.1.0 behavior; env-var override (toggles + thresholds + symbol normalisation); garbage-int parser fallback; `SHADOW_CONFIGS` is the fixed 3-config tuple; `evaluate_g4_config` PASS/BLOCK paths for TICKER+QQQ / TICKER_ONLY / QQQ_ONLY; DISABLED short-circuit; `_shadow_log_g4` emits exactly 3 `[CFG=...]` lines per candidate; `VOL_GATE_ENFORCE` default is `0`; original `[V510-SHADOW]` line still emitted (back-compat). Test count: **181 → 194**. v5.1.0's 181 existing tests untouched and still pass. Synthetic harness 50/50 byte-equal preserved (no algo change — still observation only).

**(7) Files touched.** `volume_profile.py`: new `SHADOW_CONFIGS` tuple, `_env_bool`/`_env_int` helpers, `load_active_config()`, `evaluate_g4_config()`. `trade_genius.py`: `BOT_VERSION` 5.1.0 → 5.1.1; `CURRENT_MAIN_NOTE` rotated (v5.1.0 entry moved into `_MAIN_HISTORY_TAIL`); `_shadow_log_g4` rewritten to fan out three `[CFG=...]` lines on top of the original line. `smoke_test.py`: version assert + suite header bumped 5.1.0 → 5.1.1; new v5.1.1 section. `CHANGELOG.md`: this entry.

**(8) Out of scope (deferred).** Enforcement still OFF — `VOL_GATE_ENFORCE` defaults to `0` and stays at `0` all next week. No FSM changes. No new index symbols beyond QQQ (Val explicitly anchored on QQQ for this window). No baseline rebuild changes. v5.1.2 will flip enforcement on after Val reviews next week's three-config shadow data.

---

## v5.1.0 — 2026-04-25 — Forensic Volume Filter (Anaplan logic) — SHADOW MODE ONLY.

**Why this exists.** v5.0.x asks "is volume high?" with ad-hoc tests against the current minute's bar. Val approved Gene's "Anaplan / Forensic Auditor" addendum, which replaces that with a stricter question: *is this minute's volume higher than the 55-trading-day seasonal average for THIS exact ET timestamp?* The v5.1.0 release ships the data layer + observation layer for that gate. Entry decisions are unchanged in v5.1.0 — every minute is logged with the `[V510-SHADOW]` prefix so Val can review a week of shadow data, then v5.1.1 (separate PR) flips enforcement on.

**(1) New module `volume_profile.py`.** Top-level (alongside `trade_genius.py`), so the v5.0.2 infra-guard test catches the Dockerfile `COPY` for it. Public surface: `is_trading_day`, `trading_days_back`, `session_bucket`, `build_profile`, `save_profile`, `load_profile`, `is_profile_stale`, `evaluate_g4`, `rebuild_all_profiles`, and `WebsocketBarConsumer`. All sync; no asyncio in callers' codepaths.

**(2) Baseline build (free / hybrid feed strategy).** `build_profile(ticker, end_dt_utc, key, secret)` fetches Alpaca historical 1-minute bars for the 55 most recent NYSE trading days using `feed=sip` with `end < now() - 16min` to comply with the free-plan 15-minute SIP restriction. The same window is also fetched on `feed=iex`. The published bucket median is on the IEX scale — when direct IEX samples exist for the bucket they are used; otherwise SIP samples are scaled by the per-ticker IEX/SIP ratio (mean-IEX / mean-SIP across the window). Stored shape per bucket: `{"median": int, "p75": int, "p90": int, "n": int}`.

**(3) Window: 55 NYSE trading days.** Hard-coded `NYSE_HOLIDAYS` and `EARLY_CLOSE_DATES` for 2026-2027 inside `volume_profile.py` — no new dependency. Per-minute buckets `"0931".."1559"` (regular session); early-close days populate only buckets up to the early close.

**(4) Live feed: Alpaca `/iex` websocket.** `WebsocketBarConsumer` is a daemon-thread-backed persistent connection to `wss://stream.data.alpaca.markets/v2/iex`, subscribed to `bars` for every symbol in `TICKERS`. Free-plan websocket cap = 30 symbols; if `len(TICKERS) > 30` at startup the module hard-disables itself (`VOLUME_PROFILE_ENABLED = False`) and the bot trades normally. On disconnect: jittered backoff reconnect, then a 5-minute REST replay (`feed=iex`) repopulates the in-memory volume table before resuming.

**(5) G4 evaluator (§17.2 V-P1 grid).** `evaluate_g4(ticker, minute_bucket, current_volume, profile, qqq_current_volume, qqq_profile, stage)` returns `{green, reason, ticker_pct, qqq_pct, rule}`. Stage 1 (Jab): ticker ≥ 120% AND QQQ ≥ 100% (V-P1-R1 + V-P1-R2). Stage 2 (Strike): ticker ≥ 100% (V-P1-R3). Failure modes: `NO_PROFILE_X`, `STALE_PROFILE_X` (>36h), `NO_BUCKET_X_<bucket>` for out-of-session, and `DISABLED` when the module is off.

**(6) Shadow hook.** `trade_genius.py` calls `_shadow_log_g4(ticker, stage, existing_decision)` from the per-minute long-entry path. The line emitted: `[V510-SHADOW] ticker=… bucket=… stage=… g4=GREEN/RED ticker_pct=… qqq_pct=… reason=… entry_decision=…`. **The existing entry decision is unchanged** — this is observation only. Synthetic harness 50/50 byte-equal preserved.

**(7) Profile cache + nightly rebuild.** Process-local `_volume_profile_cache` populated at startup by `load_profile(t)` for every `t` in `TICKERS`. Synchronous rebuild on startup if any profile is missing/stale. A daemon thread sleeps until 21:00 ET and calls `rebuild_all_profiles` nightly. Disk format: `/data/volume_profiles/<TICKER>.json` (overridable via `VOLUME_PROFILE_DIR`).

**(8) Smoke tests.** New `[VOLPROFILE]` section in `smoke_test.py` (~14 new tests): `is_trading_day` weekday/weekend/holiday cases, `trading_days_back(date(2026,4,25), 55)` returns 55 dates none of them weekends or in `NYSE_HOLIDAYS`, `session_bucket` boundary cases (09:30 → None, 09:31 → '0931', 15:59 → '1559', 16:00 → None, early-close honoured), `evaluate_g4` Stage 1 GREEN at exact 120%/100%, RED at 119%/100% (off-by-one), RED at 120%/99%, Stage 2 GREEN at 100%, `NO_PROFILE_X` / `STALE_PROFILE_X` / `NO_BUCKET_X_0930` / `DISABLED` failure-mode tests, JSON round-trip persistence, `len(TICKERS) > 30` disables module. All offline (no live Alpaca calls).

**(9) Files touched.** `volume_profile.py` (NEW). `trade_genius.py`: `BOT_VERSION` 5.0.4 → 5.1.0; `CURRENT_MAIN_NOTE` rotated; `import volume_profile`; new `_start_volume_profile()` + `_shadow_log_g4()`; per-minute long-entry hook. `Dockerfile`: `COPY volume_profile.py .`. `smoke_test.py`: suite header bumped + new tests. `requirements.txt`: unchanged (alpaca-py 0.43.2 already supports SIP historical + IEX websocket). `ARCHITECTURE.md`: new §17 + §18.1 G4 entry. `CHANGELOG.md`: this entry.

---

## v5.0.4 — 2026-04-25 — Hotfix: revert v5.0.3 alpaca paper-key fallback (chat-map auto-learn from v5.0.3 stays).

**Why this exists.** PR #142 (v5.0.3, squash commit `d262e80b`) added a fallback in `TradeGeniusBase.__init__` that read `<PREFIX>ALPACA_PAPER_KEY` and silently fell back to `<PREFIX>ALPACA_KEY` if the paper key was unset. The intent was to fix Gene's executor at startup because Railway had `GENE_ALPACA_KEY` set but the code only read `GENE_ALPACA_PAPER_KEY`. This was wrong on two counts: (a) **architecturally** — Alpaca paper keys and live (real-money) keys are independent credentials with different endpoints; falling back from one to the other can route paper-mode traffic through a live account, and the two are not interchangeable; (b) **confirmed dangerous in this repo** — Val confirmed that `GENE_ALPACA_KEY` / `GENE_ALPACA_SECRET` on Railway are LIVE keys, not paper. Had `GENE_ENABLED=1` caused the executor to instantiate, the v5.0.3 fallback would have submitted "paper" orders against the live brokerage account.

**(1) What was reverted.** `TradeGeniusBase.__init__` is restored to the v5.0.2 strict reads: `self.paper_key = os.getenv(p + "ALPACA_PAPER_KEY", "").strip()` and `self.paper_secret = os.getenv(p + "ALPACA_PAPER_SECRET", "").strip()`. No fallback to the un-prefixed `<PREFIX>ALPACA_KEY` / `<PREFIX>ALPACA_SECRET`. The executor startup gate near the bottom of the file (~line 9299/9316) was already correct (it only checks `<PREFIX>ALPACA_PAPER_KEY`) and is unchanged.

**(2) What stays from v5.0.3.** The chat-map auto-learn / fan-out / persistence work is unaffected and stays: per-executor `/data/executor_chats_{name}.json` map, `_load_owner_chats` / `_save_owner_chats` / `_record_owner_chat` helpers, `_send_own_telegram` fan-out rewrite, and the `_auth_guard` auto-learn hook. Operator action for Val/Gene to start receiving DMs (each owner sends any message to their executor bot once) is unchanged.

**(3) Operator action to start Gene's paper executor.** Val will set fresh `GENE_ALPACA_PAPER_KEY` and `GENE_ALPACA_PAPER_SECRET` env vars on Railway from Gene's paper Alpaca account. **Do NOT rename or repurpose the existing `GENE_ALPACA_KEY` / `GENE_ALPACA_SECRET`** — those are live keys and stay off-limits to the paper code path. No code change is required for Gene to start once the new env vars are present.

**(4) Smoke tests.** The v5.0.3 fallback-path test (`executor v5.0.3: alpaca paper key falls back to ALPACA_KEY when primary unset`) was removed. The primary-read test (`executor v5.0.3: alpaca paper key reads ALPACA_PAPER_KEY when set`) was kept and re-tagged as v5.0.4 — it now serves as the explicit assertion that paper reads only the prefixed paper env var. Test count: **162 → 161**. Synthetic harness 50/50 byte-equal preserved (no algo change).

**(5) Files touched.** `trade_genius.py`: `BOT_VERSION` 5.0.3 → 5.0.4; `__init__` fallback reverted; `CURRENT_MAIN_NOTE` rotated (v5.0.3 note moved into `_MAIN_HISTORY_TAIL` with a brief edit clarifying the v5.0.4 partial revert); v5.0.3 history-tail entry edited so its claim about the alpaca-key fallback no longer asserts the fallback exists. `smoke_test.py`: version assert + suite header bumped 5.0.3 → 5.0.4; one fallback-path test removed; primary-read test re-tagged v5.0.4. `CHANGELOG.md`: this entry plus a one-line partial-revert note prepended to the v5.0.3 entry below. `ARCHITECTURE.md`: §10.7 table restored to the v5.0.2 wording (no fallback note); a brief v5.0.4 note appended explaining why paper/live keys must not share a fallback.

---

## v5.0.3 — 2026-04-25 — Hotfix: per-executor trade-confirmation DM (auto-learn chat_id) + Gene alpaca-key fallback.

**Note (v5.0.4).** The alpaca-key fallback described in (4) below was reverted in v5.0.4 — see the v5.0.4 entry above. The chat-map auto-learn / fan-out / persistence work in (1)–(3), (5), (6) is unaffected by the revert and remains in production.

**Why this exists.** Friday Apr 24 2026 was the first prod session after the v5.0.2 deploy. The bot fired multiple paper trades on Val's account (15 BUYs, 10 SELLs in `trade_genius.py` logs) but Val's Telegram bot pushed **zero** trade confirmations. Root cause confirmed from prod logs and inspection of `TradeGeniusBase._send_own_telegram` at `trade_genius.py:925`: the method early-returns if **either** `self.telegram_token` or `self.telegram_chat_id` is empty, and `<PREFIX>TELEGRAM_CHAT_ID` was never set on Railway (only `<PREFIX>TELEGRAM_TG`). So every call from `_on_signal` (ENTRY_LONG, ENTRY_SHORT, EXIT_LONG, EXIT_SHORT, EOD_CLOSE_ALL) silently no-op'd — the trades hit Alpaca, but the operator never saw a confirmation. Separately, Gene's executor was `[Gene] skipped (GENE_ENABLED=1, GENE_ALPACA_PAPER_KEY set=False)` at startup because Railway had `GENE_ALPACA_KEY` set but the code at `trade_genius.py:736` only read `GENE_ALPACA_PAPER_KEY` — env-var name mismatch. Both bugs are pure plumbing, no algo change; the v5 state-machine fired correctly.

**Routing decision (clarified with Val).** Each executor bot DMs each owner; per-account separation is preserved (Val DMs Val's bot, Gene DMs Gene's bot, every owner sees every learned executor's trades on that bot). **No hand-set `<PREFIX>TELEGRAM_CHAT_ID` env var is required.** Each owner just sends `/start` (or any message) to their executor bot once and the bot auto-learns the chat_id, persists it, and fans out trade confirmations to every learned owner thereafter. The map survives Railway redeploys via the existing `/data` volume.

**(1) Chat-map persistence + auto-learn.** New per-executor file `/data/executor_chats_{name}.json` (path overridable via `<PREFIX>EXECUTOR_CHATS_PATH`, mirroring how `PAPER_STATE_PATH` already works). `TradeGeniusBase.__init__` loads the map on startup; `_load_owner_chats` / `_save_owner_chats` handle disk I/O with an atomic `os.replace` write. `_record_owner_chat(owner_id, chat_id)` is the single mutation point — it skips the disk write if the value didn't change. The auto-learn hook lives inside the existing `_auth_guard` choke point, which already runs on every inbound Update and already validates the user_id against `TRADEGENIUS_OWNER_IDS`; right after the owner-id check passes, we read `update.effective_chat.id` (with a `update.message.chat.id` fallback for older-shape updates) and call `_record_owner_chat`. No new top-level slash command — auto-learn is transparent.

**(2) Backwards-compat seed for `<PREFIX>TELEGRAM_CHAT_ID`.** If any operator had hand-set `VAL_TELEGRAM_CHAT_ID` or `GENE_TELEGRAM_CHAT_ID` previously, the env var still works as a seed value: on first boot (chat-map empty) it's keyed under every owner_id in `TRADEGENIUS_OWNER_IDS`. The first inbound DM from each owner overwrites their slot with the real chat_id. Documented inline in `__init__`.

**(3) `_send_own_telegram` fan-out.** Rewritten: bail when `telegram_token` is unset (token still required — that's a server-side capability check, not an addressing one), then warn-once-and-bail when the chat-map is empty (the warning includes the file path so the operator can see exactly where the map lives), otherwise iterate the map and POST `sendMessage` for each entry with the existing sync `urllib` pattern. Each chat_id failure is logged with its `owner_id` / `chat_id` and the loop continues. Two owners = max 20s worst-case in the scan-loop thread (10s timeout × 2 chats); acceptable for now and unchanged in nature from the v4 single-chat behavior.

**(4) Gene alpaca-key fallback.** `trade_genius.py:736-737` now reads `<PREFIX>ALPACA_PAPER_KEY` and falls back to `<PREFIX>ALPACA_KEY` if the primary is unset; same for `_SECRET`. Symmetric on the VAL prefix for consistency. **Live keys are intentionally NOT given this fallback** — lower urgency, higher blast radius (don't want a key meant for a different env to silently route through). Result: Gene's executor will start on Monday's open without any Railway env-var change.

**(5) Smoke tests.** Six new tests in `smoke_test.py`: chat-map persistence round-trip; `_send_own_telegram` empty-map no-op (mocked `urllib.urlopen` asserted not called); `_send_own_telegram` fan-out to N entries (mocked `urlopen`, asserted N calls with correct chat_ids in payload); paper-key reads ALPACA_PAPER_KEY when set; paper-key falls back to ALPACA_KEY when primary unset; `_auth_guard` auto-learn path updates the persisted map. Test count: **156 → 162**. The v5.0.2 infra-guard test (`infra: Dockerfile COPY whitelist includes every top-level imported module`) is preserved unchanged. Synthetic harness 50/50 byte-equal preserved (no algo change).

**(6) Files touched.** `trade_genius.py`: `BOT_VERSION` 5.0.2 → 5.0.3; `CURRENT_MAIN_NOTE` rotated (v5.0.2 note moves into `_MAIN_HISTORY_TAIL`); `__init__` loads chat-map and accepts the alpaca-key fallback; new `_load_owner_chats` / `_save_owner_chats` / `_record_owner_chat` helpers; `_send_own_telegram` rewritten to fan-out; `_auth_guard` records the owner's chat_id on every authorized inbound. `smoke_test.py`: version assert + suite header bumped 5.0.2 → 5.0.3; six new tests appended in a v5.0.3 block. `CHANGELOG.md`: this entry. `ARCHITECTURE.md`: §10.7 documents `<PREFIX>EXECUTOR_CHATS_PATH`, §11 adds a known-gotcha note about the chat-map auto-learn pattern. **No new top-level Python module** was added (helpers live inside `TradeGeniusBase`), so the v5.0.2 Dockerfile COPY whitelist is unchanged. PDF cover stays at v5.0.2 — this is plumbing, not strategy, and `STRATEGY.md` / the algo PDF cover the trading logic.

**Operator action after merge + Railway redeploy.** Val sends any message (e.g. `/start`) to the Val executor Telegram bot from his phone. Gene does the same on the Gene executor bot. Trade confirmations resume on the next signal. No env var changes required.

---

## v5.0.2 — 2026-04-25 — Hotfix: Dockerfile COPY whitelist + infra-guard test.

**Why this exists:** v5.0.0 (squash commit `8fcb68a`) shipped the new top-level module `tiger_buffalo_v5.py`, but the per-file `Dockerfile` `COPY` whitelist was not updated to include it. The container built and pushed cleanly, then crash-looped on every boot with `ModuleNotFoundError: No module named 'tiger_buffalo_v5'` (raised by `import tiger_buffalo_v5 as v5` at the top of `trade_genius.py`). Prod was down from the v5.0.0 / v5.0.1 deploy until this hotfix landed. This is the same class of bug as v4.11.0 → v4.11.1, which `ARCHITECTURE.md` §11.1 already documented as a known footgun.

**Fix.** One added line in `Dockerfile`: `COPY tiger_buffalo_v5.py .` (placed alongside the other per-file COPYs, after `error_state.py`). `BOT_VERSION` bumped 5.0.1 → 5.0.2 and `CURRENT_MAIN_NOTE` rotated (the v5.0.0 note moves into `_MAIN_HISTORY_TAIL`). Algo PDF cover regenerated at v5.0.2.

**Guard against recurrence — new infra smoke test.** `smoke_test.py` now includes `infra: Dockerfile COPY whitelist includes every top-level imported module`. The test scans the repo root for local `.py` modules, parses every `import` / `from` line in `trade_genius.py`, intersects against local modules, then reads `Dockerfile` and grep-extracts every `COPY <module>.py ` directive. If any imported local module is missing from the COPY whitelist, the test fails with the names of the offending modules. This converts the v4.11.0 / v5.0.0 footgun into a CI-blocking failure: a future PR that adds a new top-level module without updating the Dockerfile cannot merge until the COPY line is present.

## v5.0.1 — 2026-04-25 — DMI/ADX period corrected from 14 to 15 (Gene's flag).

Spec-fidelity fix on the same-day v5.0.0 release. STRATEGY.md C-R2 and L-P2-R1 originally specified DMI/ADX period **14** (Wilder's classical default), but Gene flagged that the canonical period in this codebase — and in his original spec — has always been **15** (`DI_PERIOD = 15` in `trade_genius.py`, in place since v4). v5 is now aligned: `tiger_buffalo_v5.DMI_PERIOD = 15`, the v5 1m DI helper passes `period=15` through `_compute_di`, and the 5m DI reuses the existing v4 `tiger_di` helper (which already normalized on `DI_PERIOD = 15`). Result: v5 decision-engine signals agree byte-for-byte with the v4 dashboard / executor on the same period. State-machine logic is unchanged. Updated smoke tests `v5 module: DMI period is 15 (C-R2)` and `v5 C-R2: DMI period is 15`. STRATEGY.md change history updated with a v5.0.1 row documenting the fix.

---

## v5.0.0 — 2026-04-25 — Tiger/Buffalo two-stage state machine replaces v4 ORB Breakout (long) and Wounded Buffalo (short).

**Major version bump.** This release replaces the v4.x trade-trigger logic — ORB-edge break + 2-bar confirmation on the long side, mirror-image breakdown on the short side, and the 4-layer stop chain (initial / breakeven / +$1 trail / hard-eject) — with a single per-ticker per-direction state machine specified in `STRATEGY.md` (new file at the repo root, the canonical authority for trading logic going forward). Every code-level decision in the new state machine cites a rule ID (e.g. `L-P2-R3` = "Long, Phase 2 — Stage 1 entry, Rule 3: 50% of unit on") and every smoke test docstring references the rule it covers, so a spec change traces straight through to a test failure.

The state machine has eight states: `IDLE → ARMED → STAGE_1 → STAGE_2 → TRAILING → EXITED → RE_HUNT_PENDING → LOCKED_FOR_DAY`. The long protocol is metaphorically "The Tiger Hunts the Bison"; the short protocol is "The Wounded Buffalo / Gravity Trade." Both share the state machine; they differ only in the direction of inequalities, which DMI line is read (DI+ vs DI−), where the structural stop sits (5m candle low vs. high), and which structural pivot drives the ratchet (Higher Low vs. Lower High).

**(1) Permission gates (L-P1 / S-P1).** Four boolean gates per direction must all be true before the bot transitions `IDLE → ARMED`: index polarity (QQQ vs PDC, SPY vs PDC), ticker polarity (ticker vs PDC), and a structural gate (long: ticker > first-hour high 09:30–10:30 ET; short: ticker < opening-range-low 09:30–09:35 ET). The dashboard surfaces these as four green/red lights per ticker — if any light is not green, the bot is "Off" for that name. The short side has a hard rule: if either index gate is green (S-P1-G1 or S-P1-G2 fails), shorts are forbidden regardless of the ticker's own weakness.

**(2) Stage 1 — "The Jab" (L-P2 / S-P2, 50% on).** Once ARMED, the bot watches for `DI+(1m) > 25 AND DI+(5m) > 25` simultaneously (long) or the DI− mirror (short), confirmed across **two consecutive closed 1-minute candles** (the "double-tap"). Entry fires on the close of the second confirming candle at 50% of the v4 unit size; the v4 unit-sizing math itself is preserved unchanged — v5 only changes how that unit is staged in. Initial stop ("Emergency Exit") is the low of the previous closed 5m candle (long) or the high of the previous closed 5m candle (short). This is a hard stop that does NOT move during STAGE_1. The bot records `original_entry_price` = fill price of this Stage-1 order — that value is the anchor for the Stage 2 winning rule, the safety lock, and the re-hunt reclamation gate.

**(3) Stage 2 — "The Strike" (L-P3 / S-P3, full size).** From STAGE_1, the bot watches for `DI+(1m) > 30` (long) or `DI-(1m) > 30` (short) confirmed across two more consecutive closed 1-minute candles. The "Winning Rule" is the gate that prevents averaging down: at the moment of the second confirming close, ticker.last must be in profit vs. `original_entry_price` (above for longs, below for shorts). If price has slipped to or below the original entry on the long side (or rallied to or above on the short side), Stage 2 does NOT fire — the bot stays in STAGE_1 with the original stop. When Stage 2 does fire, the bot adds the remaining 50% (position is now 100% — "Full Port"), and the **Safety Lock** instantly moves the stop on the entire 100% position to `original_entry_price`. The trade is now risk-free vs. its original cost basis ("House Money" / "Gravity Trade").

**(4) The Guardrail — 5m structural ratchet (L-P4 / S-P4, TRAILING).** On the close of every 5m candle after Stage 2 fills, the bot computes the most recent Higher Low (long: a 5m low strictly above the immediately preceding 5m low) or Lower High (short: a 5m high strictly below the preceding 5m high). The stop ratchets in the favorable direction only — it never moves down on a long or up on a short. Hard exits: long flattens 100% on `ticker.last < current_stop` OR `DI+(1m) < 25` on a closed 1m candle; short flattens on `DI−(1m) < 25` (priority-1, BEFORE the structural-stop check) OR `ticker.last > current_stop` (priority-2). The short-side priority inversion is intentional and is justified in the spec: "fear moves faster than greed" — momentum decay on the short side typically precedes a squeeze, so the bot covers on DI failure ahead of any structural-stop hit.

**(5) Re-Hunt — one shot (L-P5 / S-P5).** After an exit, the ticker is dormant in `EXITED` until price reclaims `original_entry_price` (long: ticker.last > original entry; short: ticker.last < original entry). On reclamation, the state machine returns to ARMED and the full L-P2 → L-P3 → L-P4 (or short equivalent) sequence runs again with **fresh** values (new original_entry_price, fresh stops, fresh DMI confirmations). Maximum **one** Re-Hunt per ticker per session. After a second L-P4 / S-P4 exit, the ticker is `LOCKED_FOR_DAY` regardless of subsequent reclamations.

**(6) Cross-cutting rules (C-R1 .. C-R7).** C-R1: long and short on the same ticker are mutually exclusive within a session — entering one direction means the other direction's gates are ignored until EOD. C-R2: all DMI/ADX values use period **15** on the relevant timeframe (matches v4's longstanding `DI_PERIOD = 15` and Gene's spec; the original v5.0.0 doc text said 14 and was corrected in v5.0.1). C-R3: closed-candle confirmation only — real-time intra-candle prints do NOT trigger entries; hard-stop *exits* are the exception, evaluated on every live tick because exits prioritize speed over confirmation. C-R4: the v4 daily-loss-limit (incl. v4.7.0 short-side cap) remains the portfolio-level brake on top of v5's per-trade risk; if it fires, all v5 state machines transition to LOCKED_FOR_DAY. C-R5: EOD force-close (15:55 ET) flattens any open v5 position regardless of state. C-R6: Sovereign Regime Shield (Eye of the Tiger) override remains a global kill — when active, all gates are forced false and any open position is flattened. C-R7: the v5 universe is identical to v4 (the existing 9-ticker spike list); SPY and QQQ remain pinned filter rows on the dashboard and serve as the L-P1-G1/G2 and S-P1-G1/G2 permission inputs — they are NEVER traded directly.

**Files touched.** New: `STRATEGY.md` at repo root (canonical spec, copied from `/home/user/workspace/STRATEGY.md`) and `tiger_buffalo_v5.py` (pure-function state-machine helpers, fully unit-testable in isolation; this module has no imports from `trade_genius` so it loads cleanly under any Python interpreter for spec-driven testing). `trade_genius.py`: `BOT_VERSION` 4.13.0 → 5.0.0, `CURRENT_MAIN_NOTE` rotated (v4.13.0 pushed onto `_MAIN_HISTORY_TAIL`), top-level `import tiger_buffalo_v5 as v5`, new global tracker dicts (`v5_long_tracks`, `v5_short_tracks`, `v5_active_direction`), new helpers (`v5_get_track`, `v5_di_1m_5m`, `v5_first_hour_high`, `v5_opening_range_low_5m`, `v5_lock_all_tracks`), C-R4 wired into `_check_daily_loss_limit`, C-R5 wired into `eod_close`, daily reset now clears v5 tracks. `paper_state.py`: `save_paper_state` writes `v5_long_tracks`/`v5_short_tracks`/`v5_active_direction`; `load_paper_state` reads them through `v5.load_track()` so v4 state files migrate transparently to IDLE on next start. `ARCHITECTURE.md`: sections 6 (Trading algorithm) and 7 (Risk: 4-layer stop chain) replaced with the v5 model; cross-reference to `STRATEGY.md` added as the source of truth; version stamps bumped to v5.0.0 throughout. `trade_genius_algo.pdf`: regenerated by `scripts/build_algo_pdf.py`; cover reads "v5.0.0 · April 2026"; sections 6/7 mirror the new ARCHITECTURE.md. `smoke_test.py`: new v5 test block — every L-P*-R*, S-P*-R*, and C-R* rule is covered by at least one test; each test docstring cites the rule ID it covers (search for `L-P2-R2` etc. to find the test that verifies that rule).

**v4 features explicitly preserved.** Unit sizing math (whatever `paper_shares_for(price)` and `PAPER_DOLLARS_PER_ENTRY` compute today is "100% of unit"; v5 50/50 staging means "50% of the v4 unit, then add the other 50%"). Daily-loss-limit incl. v4.7.0 short-side cap (now also locks every v5 track via C-R4). 9-ticker spike universe (`TRADE_TICKERS` unchanged; v5 universe is identical per C-R7). SPY/QQQ pinned filter rows on the dashboard (used as permission-gate inputs per L-P1-G1/G2 and S-P1-G1/G2). EOD force-close at 15:55 ET (now also locks every v5 track via C-R5). Sovereign Regime Shield (Eye of the Tiger) global kill (preserved as C-R6). Dashboard, Yahoo indices feed (v4.13.0), marquee ticker (v4.12.0), health pill (v4.11.0), LIVE pill (v4.11.5) — all unchanged. Two Alpaca executors (Val + Gene) — unchanged. `TRADEGENIUS_OWNER_IDS` — unchanged.

**Tests.** Smoke test count: 90 (v4.13.0) → 132 (v5.0.0); 42 new tests covering every rule ID in `STRATEGY.md`. The v4 synthetic harness goldens are preserved as the v4 baseline; replaying them against v5 still produces byte-equal output for the v4 entry/close paths (v5's runtime gating is layered on top — v4 trigger code remains the executor). Each new test docstring cites a rule ID; e.g. `t("v5 L-P2-R2: stage-1 entry requires 2 consecutive 1m DI+>25 closes")`.

**Risk.** This is a major-version structural change. v5's first production session will be the first time the new state machine is exercised against a live tape; the operator should expect a cold-start cycle while DI seeds populate (the existing v4.0.2-beta DI seed buffer is reused by v5 — no second warmup cost). The loss-limit, EOD force-close, and Sovereign Regime Shield all remain in place as v4-style portfolio-level brakes (C-R4/R5/R6) — if anything in v5's per-trade logic misfires, the v4 brakes still flatten the book.

**Spec ambiguities resolved.**
- "Previous closed 5-minute candle" for the L-P2-R4 / S-P2-R4 initial stop: interpreted as the most recent fully-closed 5m candle at the moment Stage 1 fires (the candle whose epoch-bucket index is `floor((entry_ts - 1) / 300)`). The currently-forming 5m candle is excluded.
- L-P3-R3 "in profit" interpretation: strict inequality (`ticker.last > original_entry_price` for longs, strictly below for shorts). Equality is treated as not-in-profit, matching the conservative reading of "the Stage-1 fills are in profit."
- L-P4-R3 ordering on the long side: spec says "EITHER trigger" without an order. We evaluate structural-stop first (cheap price compare, fires every tick) and DI<25 second (only on closed 1m candles). Either fires the same flatten — order is observability-only.
- C-R3 "closed candle" for the 5m ratchet: the ratchet itself runs on each 5m close; the structural-stop *exit* check then runs on every tick using whatever `current_stop` was last set. This matches the spec's separation of confirmation (closed-candle) from exits (every-tick).

---

## v4.13.0 — 2026-04-25 — Major indices via Yahoo: ticker now also shows real S&P 500/Nasdaq/Dow/Russell 2K/VIX cash indices plus an inline futures badge ([ES +0.40%]) on each, so on weekends and overnight you can see what futures are pricing for the open. ETF rows stay on top; if Yahoo fails the ETF rows continue to render and a dim 'data delayed' marker is prepended.

**Background.** v4.12.0 added the AH/PRE badge for ETF rows but Val noted on review that (a) VIX still rendered `n/a` because Alpaca's equity feed doesn't carry the VIX index symbol, and (b) the ETFs are *proxies* for the indices — the real S&P 500, Nasdaq Composite, and Dow are not on the wire. He also wanted index futures (ES/NQ/YM/RTY) so on a weekend you can see how the market is pricing Monday's open. The Alpaca feed cannot answer either question, so this release adds a Yahoo Finance v8/chart fallback for index symbols only — the existing ETF rows are untouched and still come from Alpaca exactly as before.

**(1) Yahoo helper.** New `_fetch_yahoo_quote_one(symbol)` and `_fetch_yahoo_quotes(symbols)` in `dashboard_server.py`. The single-symbol helper hits `https://query1.finance.yahoo.com/v8/finance/chart/{enc}?interval=1m&range=1d&includePrePost=true` (URL-encoding the caret/equals so `^GSPC` and `ES=F` round-trip cleanly) and returns `{last, prev_close}` or `None` on any failure. The batch helper fans the symbol list out across a `ThreadPoolExecutor` (capped at 8 workers) so the 9-symbol batch (5 cash + 4 futures) completes in roughly the cost of a single request rather than 9× sequential. Per-symbol failures simply omit that symbol from the result dict; total failure (zero rows back) is what triggers the frontend's 'data delayed' marker. Headers reuse the existing Mozilla UA pattern from `trade_genius.py`.

**(2) Cash + futures rows.** `_fetch_indices()` now appends 5 cash-index rows (`^GSPC`, `^IXIC`, `^DJI`, `^RUT`, `^VIX`) after the existing ETF rows, each carrying `display_label` ("S&P 500" / "Nasdaq" / "Dow" / "Russell 2K" / "VIX") plus, for the four with a liquid front-month future, a `future` sub-object `{symbol: "ES=F", label: "ES", change_pct: …}`. The future's percent is computed against the future's own previous close so the badge tells the user *where futures are pointing*, which is the whole reason to show futures. ^VIX has no front-month future on this surface (VX=F is on CFE with different conventions) so its row simply has no badge. Top-level keys: `yahoo_ok` (bool) and `yahoo_error` (str on failure). The 30-second indices cache absorbs the Yahoo cost — at most 2 outbound requests per minute per cache miss.

**(3) Frontend.** `renderIndices()` in `dashboard_static/app.js` now reads `r.display_label` (with `r.symbol` fallback) so the cash rows scroll as "S&P 500 7165.08 +0.80%" instead of "^GSPC 7165.08". The `r.future` object renders as a bracketed inline badge `[ES +0.40%]` styled with the existing `.idx-ah` class so spacing/font weight/color sizing stay consistent with the v4.12.0 AH badge. When `data.yahoo_ok === false`, a single dim `data delayed` chip is prepended to the strip — Val keeps the ETF/Alpaca rows live, just informed that the Yahoo cash/futures view is stale. AH layer on the cash rows is intentionally disabled (`ah: false`) since the futures badge is itself the after-hours signal for those rows; ETF rows still carry the v4.12.0 AH/PRE badge unchanged.

**Files touched.** `dashboard_server.py` (new `urllib` imports, `_YAHOO_HEADERS`/`_YAHOO_TIMEOUT`/`_YAHOO_INDEX_LABELS`/`_YAHOO_INDEX_FUTURE` constants, `_fetch_yahoo_quote_one`, `_fetch_yahoo_quotes`, extended `_fetch_indices` with cash+futures append block + `yahoo_ok`/`yahoo_error` top-level keys); `dashboard_static/app.js` (`renderIndices` honors `display_label`, renders inline futures badge, prepends `data delayed` chip on `yahoo_ok===false`); `smoke_test.py` gains 4 new tests for the Yahoo helper API surface, the futures-pairing schema on the indices payload, the `yahoo_ok` flag presence, and the cash-index `display_label` keys.

**Tests.** Local smoke verified `_fetch_yahoo_quotes` returns 9/9 symbols against Yahoo live on Saturday 14:32 UTC (^GSPC=7165.08 prev=7108.4, ES=F=7194.75 prev=7143.5 — futures up ~0.71% over Friday's close, which is the visible weekend signal). Existing 86 tests still pass (no regressions in `_classify_session_et` or the v4.12.0 AH layer). New tests assert: (a) `_fetch_yahoo_quote_one` returns `None` on a guaranteed-bad symbol, (b) `_fetch_indices` payload includes `yahoo_ok` key, (c) at least one cash-index row carries a `display_label`, (d) the `future` sub-object on cash rows that have one always carries a `change_pct`. 50/50 synthetic harness replays still byte-equal (no harness fields touched).

**Risk.** Yahoo's chart endpoint is keyless and has been stable for years, but it's a third-party surface. Failure modes are bounded: per-symbol failure simply skips that row (5 ETFs still render); total failure (entire batch returns nothing) flips `yahoo_ok=false` and the frontend paints the `data delayed` chip while keeping all ETF rows live. The ThreadPool is bounded at 8 workers and each request has a 6-second timeout, so the worst-case latency added to the 30-second cache miss is 6 seconds. No new dependencies (urllib is stdlib, ThreadPoolExecutor is stdlib).

---

## v4.12.0 — 2026-04-25 — Index ticker upgrade: auto-marquee when overflowing + after-hours indicator with AH/PRE badge and AH change vs the relevant base close.

**Background.** The top index strip (SPY/QQQ/DIA/IWM/VIX) has rendered all 5 tickers since v4.0.0-beta but on a 390 px iPhone only the first 3 fit visibly — the other two have been hidden behind a horizontal touch-scroll. Val asked for two things: (a) make the strip auto-scroll across the screen if items don't fit, and (b) show after-hours numbers when the market isn't open. (a) is purely cosmetic; (b) is information that's been on Alpaca's wire the whole time and we were just not surfacing it.

**(1) Auto-marquee when content overflows.** New CSS class `.idx-marquee` on `#idx-strip` enables a single CSS keyframe (`idx-marquee-scroll`) that translates the inner `.idx-track` from `0` to `-50%` over 30 seconds, looping. Seamlessness is achieved by JS duplicating the items inside the track on overflow detection — because the second copy is identical, the `-50%` end-state visually matches the `0` start-state of the next loop. The strip uses `requestAnimationFrame` after each render to compare `track.scrollWidth` to `strip.clientWidth`; if items fit, no marquee class is set and no duplication happens (avoids paying for animation on desktop where everything fits). Pause-on-interact: `:hover`, `:focus-within`, and a tap-to-toggle `.is-paused` class all apply `animation-play-state: paused` so a user can read a value mid-scroll. `prefers-reduced-motion: reduce` disables the animation entirely and falls back to native `overflow-x: auto`. The viewport-resize debounce introduced in v4.10.0 still re-renders, so portrait↔landscape recovers the right marquee/no-marquee state without a re-poll.

**(2) After-hours indicator.** Backend gains `_classify_session_et()` returning one of `rth | pre | post | closed` based purely on weekday + ET clock (04:00–09:30 = pre, 09:30–16:00 = rth, 16:00–20:00 = post, otherwise closed; weekends always `closed`). No holiday calendar — on a holiday the snapshot's `daily_bar` simply won't update and the frontend will read `closed`, which is correct. `_fetch_indices()` now writes a top-level `session` key plus three new per-row keys: `ah` (bool), `ah_change`, `ah_change_pct` (numbers). When session is `pre|post|closed` AND the latest trade differs from the relevant base close (today's RTH close if we have one, else prior-day close), the row is tagged `ah=true` with the AH delta. The regular-session `change`/`change_pct` (vs prior-day close) is unchanged so the RTH view is byte-identical.

Frontend renders the AH layer as a small amber badge after the percent: `· AH +0.42 +0.06%` (or `PRE` during 04:00–09:30 ET). Color of the AH delta is green/red on its own sign, independent of the regular-session change — so a stock that closed up but is sliding pre-market shows green RTH delta + red PRE delta, which is the actual story.

**Files touched.** `dashboard_server.py` (new `_classify_session_et`, extended `_fetch_indices`); `dashboard_static/index.html` (drop inline `overflow:hidden` so the CSS state-machine controls overflow); `dashboard_static/app.css` (new `.idx-track`, `.idx-marquee`, `.idx-ah` rules + `prefers-reduced-motion` block); `dashboard_static/app.js` (`renderIndices` rewritten to wrap items in `.idx-track`, measure overflow, duplicate on overflow, render AH badge; new `wireIdxStripPause` for tap-to-pause). `smoke_test.py` gains 2 new tests for the session classifier and the indices payload schema.

**Tests.** 86/86 local smoke green (84 prior + 2 new). 50/50 synthetic harness replays still byte-equal (v4.11.5 version-strip holds). Verified locally that on Saturday 04:13 PT the classifier returns `closed`. Visual verification at 390 px on prod after merge will confirm: (a) the marquee starts when items overflow, (b) tap pauses, (c) AH badges appear (Saturday with last trades from Friday post-close).

**Risk.** Frontend-only animation; if anything breaks the `prefers-reduced-motion` block + the `:not(.idx-marquee) { overflow-x: auto }` no-JS fallback both keep the tickers reachable. Backend AH math has guard rails: `ah=true` requires session != rth AND a positive `last` AND a positive `base` AND `|last - base| > 1e-6` — any one failing leaves `ah=false` and the row degrades to the prior v4.11.x render.

---

## v4.11.5 — 2026-04-25 — Two cleanups: LIVE pill always shows `♻ NN` countdown (with `♻ --` placeholder when scanner has no schedule) + synthetic harness replay ignores `trade_genius_version` so a bot version bump alone never churns 50 goldens.

**(1) LIVE pill — always render the recycle countdown.** Before this PR, `updateNextScanLabel()` in `dashboard_static/app.js` painted `♻ NNs` only when `window.__nextScanSec` was a number; otherwise the 1 s tick interval fell back to a counting-up `tick NNs` label. On weekends and during scanner-idle windows the backend's `/state` reports `gates.next_scan_sec: null` (verified in prod: `_next_scan_seconds()` in `dashboard_server.py:339` returns `None` when `_last_scan_time` is `None`), so users on a weekend would see `tick 47s`, `tick 48s`, `tick 49s` … forever, which Val described as confusing — the brand-row pill is supposed to communicate "next scan", not "seconds since the page loaded". This rewrites `updateNextScanLabel()` to always emit `♻` plus a 2-character value: `NNs` when we have a number, `--` when we don't. The 1 s `streamTickTimer` interval is simplified accordingly: it decrements `__nextScanSec` if it's a number and unconditionally calls `updateNextScanLabel()`. The brand-row width budget stays constant (always two characters) so the v4.11.2/.3/.4 mobile fits are preserved at 390 / 430 / 500 px. `#h-tick` is still NEVER hidden — Val's hard rule preserved.

**(2) Synthetic harness replay strips `trade_genius_version` before compare.** The harness goldens stored under `synthetic_harness/goldens/*.json` include a top-level `trade_genius_version` key that's stamped by `run_scenario()` from the live `BOT_VERSION` constant. That meant every release that bumped `BOT_VERSION` invalidated all 50 goldens for cosmetic reasons — `replay_scenario()` would diff the version string and fail with 50 single-line diffs that look identical. Operators (and CI) had to either re-record all goldens on every release or accept the noise. Fix: `replay_scenario()` now `pop("trade_genius_version", None)` from BOTH the observed dict and the loaded golden dict before `json.dumps` compare. `record_scenario()` is **NOT** touched — fresh recordings still stamp the current version into the file, so an operator inspecting a golden can still see what version produced it. This only affects the byte-equal compare path.

No HTML change. Python change scoped to `synthetic_harness/runner.py::replay_scenario`. JS change scoped to `dashboard_static/app.js`'s `updateNextScanLabel()` and the `streamTickTimer` interval inside `connectStream()`. Desktop ≥501 px untouched. 84/84 local smoke green; 50/50 synthetic replays now byte-equal.

---

## v4.11.4 — 2026-04-25 — HOTFIX: repoint CI smoke `DASHBOARD_URL` + close last 2 px clock clip at 390.

Two unrelated tiny fixes shipped together because both are one-line edits.

**(1) Post-deploy CI smoke has been red on every PR since v4.9.3.** Root cause: `.github/workflows/post-deploy-smoke.yml` hardcoded `DASHBOARD_URL: https://stock-spike-monitor-production.up.railway.app`, the pre-rename Railway domain. The service was renamed to TradeGenius in v3.5.1 and the old domain has been returning 404 since (visible in the workflow logs as `poll N: version=None ok=status=404`). The workflow's 5-minute Railway poll then times out and the whole job fails. Fix: change the env line to `https://tradegenius.up.railway.app`. We've shipped 7 PRs (v4.10.0 → v4.11.3) with red post-deploy smoke despite Railway being healthy on every one of them; this lifts the noise floor so a real post-deploy regression will actually surface in CI.

**(2) Trim brand-row horizontal padding 10px → 6px at ≤400px.** v4.11.3 dropped the clock font to 10px and brought 390 px from `12:38:1…` clipping to `12:47:24 E` clipping (the trailing `T` was hairline-clipped by ~2 px). 8 px recovered from the row's left+right padding lets the clock fit fully inside the 390 viewport. The 380 and 360 sub-bands below this block already use their own paddings and are unaffected.

No HTML/JS/Python change beyond `BOT_VERSION` + `CURRENT_MAIN_NOTE`/`_MAIN_HISTORY_TAIL` rotation. Desktop ≥501 px untouched. 84/84 local smoke green.

---

## v4.11.3 — 2026-04-25 — HOTFIX: close 390 px brand-row clipping (CSS-only).

v4.11.2 dropped the brand-row clock font from 13 px to 11 px under the existing `@media (max-width: 500px)` band. That fixed 430 px (iPhone Pro Max) cleanly — the clock rendered fully as `HH:MM:SS ET` with the LIVE pill's `tick NNs` on a single line. But at 390 px (iPhone 13 / 14 / 15 standard) the clock still clipped at `12:38:1…`; the line was ~30–40 px short of fitting.

This ships a new `@media (max-width: 400px)` sub-band between the existing 500 and 380 bands. Inside it: clock font 11 px → 10 px, brand-row gap 6 px → 4 px, version slug 10.5 px → 9.5 px, LIVE pill horizontal padding nudged in by 1 px. Hard rules from Val (preserved): `#h-tick` is NOT hidden in this band (the older 380 band still hides it; that band is unchanged for now), and the health-pill count stays visible.

No HTML, JS, or Python change beyond `BOT_VERSION` + the `CURRENT_MAIN_NOTE`/`_MAIN_HISTORY_TAIL` rotation. Desktop ≥501 px untouched. The 380 px and 360 px sub-bands below override at their widths and are unaffected.

---

## v4.11.2 — 2026-04-25 — HOTFIX: shrink mobile clock font so brand row fits at 390/430 widths (CSS-only).

v4.11.0 added a per-executor health pill into the brand row between `#tg-live-pill` and `#tg-brand-clock`. With that extra item, the brand row overflowed at iPhone Pro Max class viewports (390 px and 430 px): the clock was clipped on the right edge (`12:16:54 ET` rendered as `12:16:54` or `12:16:4…` with the `ET` suffix lost), and the LIVE pill's inline `tick NNs` countdown wrapped to two lines inside the pill, distorting the row height.

Fix is CSS-only, scoped to the existing `@media (max-width: 500px)` mobile breakpoint introduced in v4.10.2. Three changes inside that block: (1) `#tg-brand-clock` font drops from 13px → 11px with `letter-spacing: 0` and explicit `white-space: nowrap`; (2) `#tg-live-pill` and `#h-tick` get `white-space: nowrap !important` so the inline countdown stays on one line inside the pill regardless of horizontal-room budget; (3) `#tg-brand-row` `gap` tightens 8px → 6px to give the row a few more pixels of breathing room.

No HTML change. No JS change. No Python change beyond `BOT_VERSION` + the `CURRENT_MAIN_NOTE`/`_MAIN_HISTORY_TAIL` rotation. Desktop ≥501px is untouched. The 380px and 360px tighter sub-bands below already had their own clock font sizes (12px) and are unaffected.

50/50 synthetic harness replays byte-equal except for the `trade_genius_version` field. 84/84 local smoke green.

---

## v4.11.1 — 2026-04-25 — HOTFIX: add `error_state.py` to Dockerfile COPY whitelist (prod-down).

v4.11.0 introduced a new top-level module `error_state.py` but the Dockerfile uses an explicit `COPY` whitelist that was not updated in the same PR. The container crashed on every start with `ModuleNotFoundError: No module named 'error_state'`, taking https://tradegenius.up.railway.app down with a 502 on every endpoint for ~3 hours. One-line fix: add `COPY error_state.py .` next to the other top-level Python COPYs. No behavior change otherwise; 50/50 synthetic replay byte-equal except the `trade_genius_version` field. Lesson: any new top-level Python module must also be added to the Dockerfile in the same PR.

---

## v4.11.0 — 2026-04-25 — Per-portfolio health pill replaces the dashboard log tail; errors fan out to the matching executor's Telegram channel.

Substantial UI + observability change. Smoke tests grow from 84 → 94 (10 new for `error_state` + the wiring). All 50 synthetic goldens still replay byte-equal except for the `trade_genius_version` field (no error-path scenario fired `report_error` because the converted sites are exception handlers that the harness's golden paths don't reach — confirmed by inspection).

**Motivation.** The dashboard's "Log tail" card surfaced `INFO`/`WARNING`/`ERROR` lines indiscriminately, without summarizing whether the bot was actually unhealthy. Most lines were noise (heartbeats, scan completions, OR seed reports). Errors that mattered scrolled off-screen in seconds and no one was paged in real time. Two failure modes resulted:

1. A user staring at the dashboard had no fast read on "is anything broken right now?" — they'd have to scan a 200-line tail and parse logger names.
2. Errors on Val or Gene executors only ever fanned out via the *main* Telegram bot (because that was the only `send_telegram` path), so the right side-bot channels stayed silent during the very incidents they should have been paging on.

This release replaces the log tail with a single health pill (colored dot + count) in the brand row, expandable on tap to show the last ~10 error entries. Errors are recorded into per-executor ring buffers and dispatched to the *matching* executor's Telegram channel (Main / Val / Gene each have their own bot), with a per-`(executor, code)` 5-minute dedup so a flapping error code can't spam.

**Backend — new module `error_state.py`.** Owns three bounded `deque`s (one per executor; `maxlen=50`) plus a dedup table keyed by `(executor, code)`. Public API:

- `record_error(executor, code, severity, summary, detail, *, ts=None, now_fn=time.time) -> bool` — appends an entry and returns `True` iff the dedup cooldown has elapsed for this `(executor, code)` pair. The caller decides what to do with the boolean (in `trade_genius.py`'s `report_error()` wrapper: dispatch to Telegram).
- `snapshot(executor) -> dict` — returns `{"executor", "count", "severity", "entries"}`, where `severity` is `green` (no entries), `warning` (only warning-tier entries), or `red` (any error/critical). `entries` is the last 10 newest-first.
- `reset_daily(executor=None)` — clears either one or all three rings + the dedup table. Wired into `reset_daily_state()` next to `daily_short_entry_date`.
- `_reset_for_tests()` — wipe all state, used by the smoke suite.

**Backend — `report_error()` wrapper in `trade_genius.py`.** Logs via the existing logger (the only thing the codebase did before), then calls `error_state.record_error()`, and if the dedup gate elapsed, dispatches a Telegram message. The dispatch path:

- For `executor in ("val", "gene")` → calls `inst._send_own_telegram(text)` on the executor instance, which uses that executor's *own* bot token (Val / Gene each have their own).
- For `executor == "main"` → falls back to the global `send_telegram(text)` path.

The Telegram body is hard-wrapped at ≤34 chars/line by a small word-wrap helper so the message renders correctly on the narrowest mobile clients.

**Backend — converted sites (9 `logger.error` call sites).** Every trading- or ops-relevant `logger.error` was either upgraded to `report_error()` or left alone if it was non-actionable (e.g. a one-off init-time warning that doesn't need to page). The 9 converted sites:

1. `RETRO_CAP_LONG_FAILED` — retroactive long-stop cap tightening raised.
2. `RETRO_CAP_SHORT_FAILED` — retroactive short-stop cap tightening raised.
3. `SYSTEM_TEST_FAILED` — `/test` self-check raised.
4. `MANAGE_POSITIONS_EXCEPTION` — long-side position manager loop raised. (Also removed the ad-hoc `send_telegram` pair adjacent to this site, which was duplicating what `report_error()` now does centrally.)
5. `MANAGE_SHORT_POSITIONS_EXCEPTION` — short-side position manager loop raised.
6. `HARD_EJECT_EXCEPTION` — Tiger-mode hard-eject raised.
7. `PAPER_ENTRY_EXCEPTION` — long paper-entry execution raised.
8. `PAPER_SHORT_ENTRY_EXCEPTION` — short paper-entry execution raised.
9. `SCAN_LOOP_EXCEPTION` — the main scan loop raised at top level.

**Backend — `dashboard_server.py`.**

- New endpoint `GET /api/errors/{executor}` (auth-cookie required; 401 otherwise; 400 on unknown executor name; 500 wrapped on exception). Returns the live `error_state.snapshot(name)` payload.
- `/api/state` (Main snapshot) now embeds `errors: error_state.snapshot("main")` so the SSE pulse paints the pill without an extra round-trip.
- `/api/executor/{name}` (Val/Gene snapshot) embeds the matching executor's `errors` snapshot, including on every early-return path (executor disabled, client build failed, alpaca client `None`, cached-payload return). The cached return overlays a *fresh* errors snapshot so the pill stays live even when the rest of the payload is 15-second-cached.
- The `/stream` SSE handler no longer emits a `logs` event — only `state` + heartbeat ping. The corresponding `last_log_seq` parameter and the `_logs_since(...)` call were removed.
- The whole log-buffer infrastructure (`_LOG_BUFFER_SIZE`, `_log_buffer`, `_log_seq`, `_log_lock`, `_RingBufferHandler`, `_install_log_handler`, `_logs_since`, the `_install_log_handler()` boot-time call) was deleted as dead code. A smoke test asserts none of these symbols still exist on the module to guard against partial reverts.

**Frontend — `dashboard_static/index.html`.**

- Removed the entire `Log tail` `<section>` (lines 171–176 in the old file).
- Added a `<button id="tg-health-pill">` to `#tg-brand-row`, between `#tg-live-pill` and `#tg-brand-clock`. It carries a colored dot (`#tg-health-dot`) + a tabular count (`#tg-health-count`). A sibling `<div id="tg-health-pop" role="dialog">` is the dropdown that the pill toggles open.

**Frontend — `dashboard_static/app.css`.**

- Removed the `.log {…}` block (and its `.log .t/.ok/.warn/.err/.info` color rules) and the `≤900px` mobile override that targeted `.log`.
- Added the `.tg-health-pill` / `.tg-health-dot` / `.tg-health-count` rules with `h-green` (#34d399) / `h-warn` (#fbbf24) / `h-red` (#ef4444) severity classes that JS toggles. Plus the `.tg-health-pop` dropdown (fixed-position, anchored under the pill, scrollable) and `.tg-health-row` entry styling.
- Mobile breakpoints (≤500 / ≤380 / ≤360 px) get progressively-tighter pill paddings so the brand row still fits one line on iPhone 13 / 14 Pro Max / iPhone SE.

**Frontend — `dashboard_static/app.js`.**

- Removed `appendLogs(entries)` (the rendering helper) and the SSE `logs` event listener that called it. `LOG_MAX` and `logCount` removed too.
- Added `applyHealthPill(executor, snapshot)` to IIFE-1 and exposed it as `window.__tgApplyHealthPill`. Mirrors the v4.10.2 cross-IIFE bridge pattern. IIFE-2 aliases via `const applyHealthPill = window.__tgApplyHealthPill || (() => {})` so a missing bridge silently no-ops instead of throwing.
- `renderAll(s)` (Main /api/state arrival) calls `applyHealthPill("main", s.errors)`.
- `pollExecutor(name)` (Val/Gene /api/executor poll) calls `applyHealthPill(name, data.errors)`.
- `selectTab(name)` writes the active executor name to `document.body[data-tg-active-tab]`. `applyHealthPill()` reads that attribute and only paints when the snapshot's executor matches the active tab — so a stale Val poll arriving after the user switched back to Main can't overpaint the Main pill.
- The pill is wired with `click → toggle dropdown`, click-outside-to-close, Escape-to-close, and a resize listener that re-positions the dropdown.

**Files touched.**

- `error_state.py` — **NEW** module. Per-executor error rings, dedup table, snapshot/reset API.
- `trade_genius.py` — `BOT_VERSION` 4.10.2 → 4.11.0; `CURRENT_MAIN_NOTE` rewritten for v4.11.0 (≤34 chars/line); `_MAIN_HISTORY_TAIL` carries v4.10.2 entry forward; `import error_state`; new `report_error()` wrapper + `_format_error_telegram()` word-wrap helper + `_executor_inst()` helper; `reset_daily_state()` calls `error_state.reset_daily()`; 9 `logger.error` sites converted to `report_error()`.
- `dashboard_server.py` — new `/api/errors/{executor}` route + `h_errors` handler + `_errors_snapshot_safe()` helper; `errors` embedded in `/api/state` and every return path of `_executor_snapshot`; `/stream` no longer emits the `logs` SSE event; entire log-buffer ring + handler + install hook + `_logs_since` deleted.
- `dashboard_static/index.html` — health pill button + dropdown markup; log-tail card removed.
- `dashboard_static/app.css` — health-pill + dropdown CSS; mobile-breakpoint pill paddings; `.log` rules removed.
- `dashboard_static/app.js` — health-pill renderer + cross-IIFE bridge + dropdown wiring + IIFE-2 aliasing; Main `renderAll` and Val/Gene `pollExecutor` paint the pill; `selectTab` tags the active tab on `<body>`; `appendLogs` and SSE `logs` listener deleted.
- `smoke_test.py` — version assertions bumped to 4.11.0; 10 new tests covering `error_state` (ring-cap, dedup cooldown, daily reset, severity tiers, executor/severity normalization), `report_error` existence, `/api/errors/{executor}` route registration, `errors` embedded in `/api/state` and `/api/executor`, and absence of the deleted log-buffer symbols.
- `synthetic_harness/goldens/*.json` — 50 files re-recorded; only the `trade_genius_version` field changed across all of them.
- `CHANGELOG.md` — this entry.

**Explicitly NOT touched.** `paper_state.py`, `telegram_commands.py`, `side.py`, the synthetic harness scenarios themselves, signal bus, executor classes (`TradeGeniusBase` / `TradeGeniusVal` / `TradeGeniusGene`), order-placement code paths, Alpaca client wiring. The conversion to `report_error()` is strictly additive — every converted site still calls `logger.error(...)` first (via the wrapper), so existing log-aggregation pipelines see no change.

**Lessons logged.**

- Per-executor side-bots existed since v4.0.0-alpha but `send_telegram()` was the only path the rest of the code knew about. Anything that needs to page Val/Gene users specifically must dispatch via `inst._send_own_telegram(text)`, not the global send. `report_error()` is now the canonical wrapper that handles the routing — new code should use it instead of inlining a fresh `logger.error(…); send_telegram(…)` pair.
- Cross-IIFE helpers in `dashboard_static/app.js` should be exposed on `window.__tg*` at definition time, the moment they are first used in IIFE-2. v4.10.2 set the precedent for the gate helper; this release follows the exact same pattern for `applyHealthPill`.
- Don't paint stale data: cached snapshots are fine for KPIs but the health pill must always reflect the latest error count, so the cached path overlays a fresh `error_state.snapshot()` — the rest of the payload stays cached.

---

## v4.10.2 — 2026-04-25 — Hotfix: two more v4.10.0-introduced dashboard bugs (Val/Gene tabs threw "Fetch failed: applyGateTriState is not defined"; mobile clock wrapped to row 2 on iPhone Pro Max class viewports).

Dashboard-only patch. No `trade_genius.py` business logic change. The 50-scenario synthetic harness still replays byte-equal (only the embedded `trade_genius_version` field changes). All 119 smoke tests pass.

**Motivation.** User caught two bugs on iPhone after v4.10.1 deployed:

1. **`Fetch failed: Can't find variable: applyGateTriState`** red banner on the Val tab (and Gene). Every executor poll surfaced this. The error wording is iOS Safari's `ReferenceError`; Chromium says `applyGateTriState is not defined`. Same bug.
   - Root cause: `dashboard_static/app.js` is two independent IIFEs. Lines 1–807 are the main-tab IIFE (KPIs, positions, proximity, trades, log tail, SSE stream). Lines 809–1643 are the tab-switcher / per-executor poll IIFE. v4.10.0 added `applyGateTriState` to the *first* IIFE only. `renderExecutor()` and `refreshExecSharedKpis()` live in the *second* IIFE and called the helper directly — `ReferenceError` the moment a Val/Gene `/api/executor/{name}` poll completed. `pollExecutor()`'s catch block then displayed the message as `"Fetch failed: " + e.message`, which is why it looked like a network failure.
   - Fix: at the bottom of the first IIFE, expose `window.__tgApplyGateTriState = applyGateTriState`. At the top of the second IIFE, alias `const applyGateTriState = window.__tgApplyGateTriState || (() => {})` so every existing call site works as-is and a no-op fallback prevents the same class of regression. The two IIFEs stay otherwise independent (per the design comment that explicitly says "Independent from the main-tab IIFE above").
   - Why the two-IIFE design existed in the first place: v4.0.0-beta added the tab switcher as an additive overlay so the existing main-tab logic couldn't accidentally break it. Keeping them separate is fine — but any helper that needs to be shared must be bridged on `window`. v4.10.0 was the first cross-cutting helper, so this exposure pattern is new.

2. **Mobile clock wraps to row 2 on iPhone Pro Max class viewports (414–430 px CSS px).** The brand row (`#tg-brand-row`) is a `flex-wrap: wrap` flexbox containing logo + title + version + `LIVE` pill (with `margin-left:auto`) + clock. The existing `flex-wrap: nowrap !important` override was capped at `@media (max-width: 420px)`. Anything wider — iPhone 14/15/16 Plus and Pro Max — landed in the wrap regime and the clock got bumped to a second row.
   - Root cause: `dashboard_static/app.css` line 424 — `@media (max-width: 420px)`. The 420 cap was set in v4.3.1 when iPhone 13 (390) was the target. Larger iPhones never had the nowrap rule applied.
   - Fix: lift the breakpoint 420 → 500. The shrinks already in this band (gap 8 px, padding 8 12, version mar-left 2, pill gap/padding tightened, clock 13 px) easily fit on 414–430 px without further work; verified at 430 width that everything fits on one line.

**Files touched.**

- `dashboard_static/app.js` — add `window.__tgApplyGateTriState = applyGateTriState` at end of first IIFE; add aliasing `const applyGateTriState = window.__tgApplyGateTriState || (() => {})` at top of second IIFE.
- `dashboard_static/app.css` — change `@media (max-width: 420px)` (the brand-row nowrap rule) to `@media (max-width: 500px)`.
- `trade_genius.py` — `BOT_VERSION` 4.10.1 → 4.10.2; `CURRENT_MAIN_NOTE` rewritten for this hotfix (≤34 chars/line); `_MAIN_HISTORY_TAIL` carries v4.10.1 entry forward.
- `smoke_test.py` — version assertions bumped to 4.10.2.
- `CHANGELOG.md` — this entry.

**Explicitly NOT touched.** `dashboard_server.py` business logic, `side.py`, `paper_state.py`, `telegram_commands.py`, `synthetic_harness/`, any executor/portfolio code path, the main-tab GATE tri-state (which was working — only the cross-IIFE bridge for Val/Gene was broken).

**Lesson logged.** `dashboard_static/app.js` is two IIFEs, not one. Any helper added in IIFE-1 that IIFE-2 needs must be bridged on `window`. Going forward, helpers shared across both should always be exposed on `window.__tg*` at definition time.

---

## v4.10.1 — 2026-04-25 — Hotfix: finish the two v4.10.0 fixes that shipped half-complete (empty Open Positions card collapse + mobile void below proximity).

Dashboard-only patch. No `trade_genius.py` business logic change. The 50-scenario synthetic harness still replays byte-equal (only the embedded `trade_genius_version` field changes). All 119 smoke tests pass.

**Motivation.** Visual verification of v4.10.0 caught two of the five fixes shipped incomplete:

1. **Empty Open Positions card still tall (~500 px) when 0 positions.** v4.10.0 added `#port-strip-empty` and JS that hid `#pos-body`, but the card itself still stretched to match the Proximity card next to it because `.grid.grid-2` defaults to `align-items: stretch`. Hiding the body did nothing about the card's outer height.
   - Root cause: `.card { display: flex; flex-direction: column }` inside `.grid-2` with no override — every empty-state card stretched to the tallest sibling regardless of its own content height. `dashboard_static/app.css` ~line 120 (.card rule) + ~line 117 (.grid-2 grid template).
   - Fix: JS now toggles a `.is-empty` modifier on the `.card` element when `positions.length === 0`. CSS adds `.card.is-empty { align-self: start; min-height: 0 }` so the card sizes to its (header + one-row strip) content. Card collapses to ~80 px on desktop and on mobile.

2. **Mobile void below proximity STILL there at 390×844.** v4.10.0 dropped `.app { min-height: 100dvh }` from the `(max-width: 900px)` block, but that was the wrong target — the underlying problem was `html, body { overflow: hidden }` on the page root combined with `.app { display: grid; grid-template-rows: auto 1fr }`. On mobile the `1fr` ghost row plus body's `overflow: hidden` clipped Today's Trades / Observer / Log Tail entirely off the bottom of the viewport with no scroll context to reach them.
   - Root cause: `dashboard_static/app.css` line 25 `html, body { overflow: hidden }` (kept for desktop, briefly flipped to `auto` only at ≤900 px) interacting with `.app { grid-template-rows: auto 1fr; height: 100dvh }` at line 38–43.
   - Fix: drop `overflow: hidden` from `html, body` entirely (it was a safety belt for desktop where `.app { height: 100dvh }` already prevents body overflow — removing it costs nothing on desktop and frees the mobile page to scroll). On `≤900 px`, switch `.app` to `display: block; height: auto` so the `1fr` ghost row goes away and `.main` flows naturally inside it.

The other three v4.10.0 fixes (mobile compact index ticker, log wrap, GATE tri-state coloring) are confirmed working and are untouched.

**Files touched.**

- `dashboard_static/app.css` — drop `overflow: hidden` from `html, body`; switch `.app` to `display: block; height: auto` on `≤900 px`; add `.card.is-empty { align-self: start; min-height: 0 }` rule.
- `dashboard_static/app.js` — `renderPositions` now toggles `.is-empty` on the Open Positions card alongside the existing `#pos-body` / `#port-strip` / `#port-strip-empty` show/hide logic.
- `trade_genius.py` — `BOT_VERSION` 4.10.0 → 4.10.1; `CURRENT_MAIN_NOTE` rewritten for this hotfix (≤34 chars/line, no literal em-dashes); `_MAIN_HISTORY_TAIL` carries the v4.10.0 entry forward.
- `smoke_test.py` — version assertions bumped to 4.10.1.
- `CHANGELOG.md` — this entry.

**Explicitly NOT touched.** `dashboard_server.py` business logic, `side.py`, `paper_state.py`, `telegram_commands.py`, `synthetic_harness/`, any executor/portfolio code path. The other three v4.10.0 dashboard fixes (compact ticker, log wrap, GATE tri-state) — those are working and were left alone.

---

## v4.10.0 — 2026-04-25 — UI polish: 5 dashboard fixes (mobile compact ticker, mobile void, collapsed empty positions, log wrap, GATE tri-state).

Dashboard-only release. No `trade_genius.py` business logic change. The 50-scenario synthetic harness still replays byte-equal (only the embedded `trade_genius_version` field changes). All 119 smoke tests pass.

**Motivation.** Five small but accumulating UI papercuts that have been sitting in the polish backlog:

1. **Mobile index ticker overflow.** The `#idx-strip` (SPY/QQQ/DIA/IWM/VIX) renders symbol + price + Δ$ + Δ% per item. On a 390px iPhone, five items at ~280 chars wide simply do not fit and the strip became a thin horizontal-scroll trough that nobody used. Phones now hide the absolute Δ$ value (price + Δ% remain), trim per-item padding, and add `scroll-snap-type: x mandatory` so a swipe settles cleanly on a symbol boundary instead of mid-cell. A 150 ms-debounced `resize` listener re-renders the strip on portrait↔landscape rotation so the layout recovers without waiting for the 30 s poll.

2. **Mobile dead void below proximity.** On phones, the page rendered ~5 proximity rows then a large empty band before the rest of the layout resumed. Root cause: the `(max-width: 900px)` block set `.app { min-height: 100dvh }`, which forced the panel container to fill a full viewport _on top of_ the `idx-strip`/brand/tabs already consuming ~140 px above it. Dropped the `min-height` so `.app` sizes to its content; tall pages still scroll naturally via the body. Verified portrait 390×844 (no void) and desktop 1280×900 (no broken layout, no double-scroll).

3. **Empty Open Positions card collapses to a one-row strip.** When `positions.length === 0` (most of the time outside RTH and on Val/Gene paper), the card was rendering its title, a "No open positions." empty state, _plus_ the full 2-row Equity/BP + Cash/Invested/Shorted strip — eating ~25 % of desktop vertical real estate. Now the empty branch hides the body and the 2-row strip and shows a single-row condensed strip with `Equity · Buying power · Cash`. The card title and `· 0` count remain visible. Returning to ≥1 position restores the full layout untouched.

4. **Log tail wraps cleanly.** `.log` was `white-space: pre; overflow-x: auto`, which produced a horizontal scrollbar on the LOG TAIL section whenever a single line (URL, JSON dump, traceback frame) overflowed 1280 px. Switched to `white-space: pre-wrap; word-break: break-all; overflow-x: hidden`. Newlines between entries are still preserved.

5. **GATE KPI tri-state coloring.** The GATE cell showed amber `PAUSED` 24/7 outside market hours because `gates.scan_paused` is the union of `_scan_paused` (operator `/pause`) and `_scan_idle_hours` (auto-idle outside RTH). That conflated "manually paused" with "market closed". Three semantic states are now distinguished by inferring after-hours from `regime.mode === "CLOSED"`:
   - **ARMED** (green) — market open, scanner ready (was "READY").
   - **AFTER HOURS** (muted grey) — market closed; the bot is correctly idle (NEW state).
   - **PAUSED** (amber) — operator-initiated halt during RTH (preserved semantics, narrower trigger).
   - **HALTED** (red) — emergency halt (unchanged).
   - **WAIT** (amber) — opening range still being collected (unchanged).

  The same renderer is now shared by the Main, Val, and Gene panels (extracted into `applyGateTriState(gateEl, gateSubEl, gates, regime)` so the three call sites stay in sync).

**Files touched.**

- `dashboard_static/index.html` — added `class="idx-strip"` hook and the new `port-strip-empty` element.
- `dashboard_static/app.css` — mobile `min-height` fix; log wrap rules; new `.idx-compact`, `.port-strip-empty.*`, and `.gate-armed/-paused/-after-hours/-halted` classes.
- `dashboard_static/app.js` — compact-mode toggle on `#idx-strip` + debounced resize re-render; collapsed empty Open Positions branch; shared `applyGateTriState` helper used by Main + both exec panels.
- `trade_genius.py` — `BOT_VERSION` 4.9.3 → 4.10.0; `CURRENT_MAIN_NOTE` rewritten for this release (still ≤34 chars/line, no literal em-dashes); `_MAIN_HISTORY_TAIL` carries the v4.9.3 entry forward.
- `smoke_test.py` — version assertions bumped to 4.10.0.
- `CHANGELOG.md` — this entry.

**Explicitly NOT touched.** `dashboard_server.py` business logic, `side.py`, `paper_state.py`, `telegram_commands.py`, `synthetic_harness/`, any executor/portfolio code path. The harness goldens record Telegram messages and bot state, not dashboard rendering, so the UI is free of byte-equal constraints.

---

## v4.9.3 — 2026-04-25 — cleanup: delete unused SideConfig fields and methods (M2/M3).

Small, targeted cleanup PR. No bot behavior change.

**Motivation.** The side.py review at v4.9.0 flagged six `SideConfig` fields and two methods with zero references in `trade_genius.py` — pre-staged in anticipation of a future Stage B3 Telegram-string collapse that may not happen. Val decided: delete now, re-add with confidence later if actually needed. The synthetic harness (50 byte-equal goldens) guarantees any regression is caught immediately.

**Removed from `side.py::SideConfig`:**

- Fields: `entry_label`, `entry_emoji`, `exit_emoji`, `cash_word`, `polarity_op`, `di_attr`.
- Methods: `or_breakout(current_price, or_h, or_l)` (superseded by the inline `_tiger_two_bar_long/short` checks in the unified body), `di_aligned(plus_di, minus_di)` (superseded by the inline comparison in the unified body).
- The `CONFIGS` dict at the bottom of `side.py` drops the now-deleted assignments for both `Side.LONG` and `Side.SHORT`.

**Verification before delete.** Repo-wide grep for `cfg\.entry_label`, `cfg\.entry_emoji`, `cfg\.exit_emoji`, `cfg\.cash_word`, `cfg\.polarity_op`, `cfg\.di_attr`, `cfg\.or_breakout`, `cfg\.di_aligned`, `\.or_breakout(`, `\.di_aligned(` across all .py files returned zero hits. The `exit_emoji_glyph` local variable in `trade_genius.py::close_breakout` is a different symbol (not `cfg.exit_emoji`) and is untouched.

**What was NOT touched.**

- The `*_attr` fields (`or_attr`, `positions_attr`, `daily_count_attr`, `daily_date_attr`, `trade_history_attr`) and `capped_stop_fn_name` — all live, all still validated by the v4.9.2 `_validate_side_config_attrs()` guard at import.
- Every other `SideConfig` field (`history_side_label`, `paper_log_entry_verb`, `entry_cash_delta`, etc.) — all in use by the unified bodies.
- The unified `check_breakout` / `execute_breakout` / `close_breakout` bodies in `trade_genius.py`.
- `synthetic_harness/`, `paper_state.py`, `telegram_commands.py`, `dashboard_server.py`.

**Changed:**

- `BOT_VERSION = "4.9.3"`. `CURRENT_MAIN_NOTE` updated; v4.9.2 entry pushed to `_MAIN_HISTORY_TAIL`.
- `smoke_test.py` expected-version assertions bumped to `4.9.3`.

**`side.py` LOC delta:** 176 → 145 lines (-31). The file is now a pure lookup table + three direction helpers (`realized_pnl`, `entry_cash_delta`, `close_cash_delta`).

**Verification after delete.**

- `SSM_SMOKE_TEST=1 python3 -c "import trade_genius"` → clean import; v4.9.2 validator still passes (no `*_attr` field was removed).
- `SSM_SMOKE_TEST=1 python3 smoke_test.py --local --synthetic` → 119/119 pass.
- `SSM_SMOKE_TEST=1 python3 -m synthetic_harness replay` → 50/50 byte-equal (only `trade_genius_version` field bumps 4.9.2 → 4.9.3).

---

## v4.9.2 — 2026-04-25 — hardening: fail-fast SideConfig attr validator (M1).

Small, targeted hardening PR. No bot behavior change.

**Motivation.** The Stage B2 collapse (v4.9.0) introduced six string-keyed `globals()[cfg.attr_name]` lookups in `trade_genius.py`'s unified `check_breakout` / `execute_breakout` / `close_breakout` bodies. If anyone renames one of the referenced module-level names (e.g. `positions` → `open_positions`) and forgets to update `side.py`, the failure manifests as a `KeyError` on the first entry attempt of the day — potentially hours into a trading session. We want this class of rot to fail at import instead.

**Added:**

- `trade_genius.py::_validate_side_config_attrs()` — asserts that every `SideConfig` `*_attr` / `*_fn_name` field (`or_attr`, `positions_attr`, `daily_count_attr`, `daily_date_attr`, `trade_history_attr`, `capped_stop_fn_name`) resolves to a real entry in `globals()`. Called once at module top level, immediately after `_capped_short_stop` — the latest top-level definition of any referenced name — so all six dicts and both stop-helper functions are in scope.
- If a name is missing, raises `AssertionError: SideConfig(<side>) references missing global '<name>' in trade_genius.py` at import time.

**Changed:**

- `BOT_VERSION = "4.9.2"`. `CURRENT_MAIN_NOTE` updated; v4.9.1 entry pushed to `_MAIN_HISTORY_TAIL`.
- `smoke_test.py` expected-version assertions bumped to `4.9.2`.

**Verification.** Before shipping, the validator was proven non-trivial by temporarily setting `LONG.positions_attr = "positions_TYPO"` in `side.py` and running `SSM_SMOKE_TEST=1 python3 -c "import trade_genius"` — import raised `AssertionError: SideConfig(long) references missing global 'positions_TYPO' in trade_genius.py`. The typo was reverted and import cleaned; the diff below does not include it.

**What was NOT touched.**

- No change to `side.py` (other than the reverted typo test).
- No change to any unified body (`check_breakout`, `execute_breakout`, `close_breakout`).
- No new validators for the M2/M3 dead fields flagged in the side.py review — those remain deliberately untouched until Stage B3.
- No change to `synthetic_harness/`, `paper_state.py`, `telegram_commands.py`, or `dashboard_server.py`.

**Tests:**

- `SSM_SMOKE_TEST=1 python3 smoke_test.py --local --synthetic` → 119/119 pass.
- `SSM_SMOKE_TEST=1 python3 -m synthetic_harness replay` → 50/50 byte-equal.

---

## v4.9.1 — 2026-04-25 — fix: CI post-deploy poller + rate-limit investigation.

Two related dashboard/CI fixes. No bot business-logic change.

**Issue 1 — Post-deploy poller has been failing on every release since v4.7.0.**
The GitHub Actions "Wait for Railway rollout" step polled `/api/state`, which requires a session cookie. The poller was unauthenticated, so every poll returned `status=404` and the step always timed out. Deploys were healthy; only the CI step was broken.

**Issue 2 — Prod rate-limit smoke test failure.**
`smoke_test.py --prod` consistently returned `[401, 401, 401, 401, 401, 401, 401]` for the 7 consecutive bad-password POSTs, when it expects the 6th+ to be `429`. Root cause: `DASHBOARD_TRUST_PROXY` env var is not set on Railway. With it unset, `_client_ip` falls back to `request.remote`, which behind Railway's proxy fleet varies per request — different proxy node = different IP bucket = no rate-limiting trip. The limiter code itself is correct (verified by new unit test). Fix is operational: Val needs to set `DASHBOARD_TRUST_PROXY=1` on Railway so we key off `X-Forwarded-For` instead.

**Added:**

- `dashboard_server.py::h_version` — `GET /api/version` returns `{"version": BOT_VERSION}` without requiring auth. Version is not sensitive; this lets CI confirm rollout without holding a session cookie.
- `smoke_test.py` — three new local tests:
  - `v4.9.1: rate-limiter blocks 6th attempt within window` — exercises `_rate_limit_check` directly with the same IP 7 times, asserts `[True]*5 + [False]*2`.
  - `v4.9.1: rate-limiter buckets per-IP independently` — verifies separate IPs don't share a bucket.
  - `v4.9.1: /api/version endpoint registered` — guards against the route being dropped from `_build_app`.

**Changed:**

- `.github/workflows/post-deploy-smoke.yml` — the wait step now polls `/api/version` (no login) instead of `/api/state` (auth required). Drops the `initial login` and `re-login` plumbing entirely. The PROD-smoke step's `sleep 65` cushion is dropped to `sleep 5` since the wait step no longer consumes the per-IP rate-limit bucket.
- `BOT_VERSION = "4.9.1"`. `CURRENT_MAIN_NOTE` updated; v4.9.0 entry pushed to `_MAIN_HISTORY_TAIL`.

**Operational follow-up (Val):**

- Set `DASHBOARD_TRUST_PROXY=1` on the Railway service so the login rate-limiter keys off the real client IP (`X-Forwarded-For`) instead of the proxy hop. Without this, the 6th-bad-attempt prod smoke will keep failing even though the limiter logic is sound.

**Tests:**

- Local smoke: 3 new tests. All existing tests still pass.
- After merge + Railway rollout: `curl /api/version` should return `{"version":"4.9.1"}`. Once `DASHBOARD_TRUST_PROXY=1` is set, prod smoke is expected to be 7/7.

---

## v4.9.0 — 2026-04-25 — refactor: Stage B2 real collapse — unified bodies, legacy deleted.

The actual collapse the v4.8.0 PR described but never finished. `check_breakout`, `execute_breakout`, and `close_breakout` are now ONE unified body each, parameterized by `Side` enum + `SideConfig` from `side.py`. The 6 legacy long/short twin bodies and the `SSM_USE_COLLAPSED` rollback flag are deleted. `trade_genius.py` shrinks by ~700 LOC.

The 50-scenario synthetic harness (v4.8.1 + v4.8.2) is the safety net: every golden replays byte-equal against the unified bodies. Only the `trade_genius_version` field bumped (4.8.2 → 4.9.0); no behavior changed.

**Removed:**

- `_legacy_check_entry`, `_legacy_check_short_entry` — long+short entry-gate twins (~540 LOC combined).
- `_legacy_execute_entry`, `_legacy_execute_short_entry` — long+short execute twins (~230 LOC combined).
- `_legacy_close_position`, `_legacy_close_short_position` — long+short close twins (~260 LOC combined).
- `USE_COLLAPSED_PATH = os.environ.get("SSM_USE_COLLAPSED", ...)` — the v4.8.0 rollback flag and its env-var plumbing.
- 13 `differential:` smoke tests in `smoke_test.py` — tautological once legacy bodies no longer exist.

**Changed:**

- `side.py::SideConfig` extended with the side-specific labels needed by the unified bodies: `history_side_label`, `log_side_label`, `paper_log_entry_verb`, `paper_log_close_verb`, `skip_label`, `or_side_label`, `or_side_short_label`, `di_sign_label`, `stop_baseline_label`, `stop_capped_label`, `entry_signal_kind`, `exit_signal_kind`, `entry_signal_reason`, `trail_peak_attr`, `limit_offset`. Plus methods `entry_cash_delta` and `close_cash_delta` for symmetric cash bookkeeping.
- `trade_genius.py::check_breakout(ticker, side)` — single body that resolves all side-specific values via `cfg = CONFIGS[side]` and `globals()[cfg.attr_name]` for module-level dicts.
- `trade_genius.py::execute_breakout(ticker, current_price, side)` — single body. Preserves the long-only `paper_trades` / `paper_all_trades` append (shorts continue to write only to `short_trade_history`).
- `trade_genius.py::close_breakout(ticker, price, side, reason)` — single body. Telegram message format branches on `cfg.side.is_long` for the "EXIT" vs "SHORT CLOSED" headers.
- Public wrappers `check_entry`, `check_short_entry`, `execute_entry`, `execute_short_entry`, `close_position`, `close_short_position` are now thin one-line forwarders to the unified `*_breakout` functions. No callers changed.
- `BOT_VERSION = "4.9.0"`. `CURRENT_MAIN_NOTE` updated; v4.8.2 entry pushed to `_MAIN_HISTORY_TAIL`.

**Tests:**

- `python -m synthetic_harness replay` — 50/50 byte-equal (re-recorded only for the `trade_genius_version` field bump).
- `smoke_test.py --local --synthetic` — 119/119 (was 132; -13 from differential-test deletion).

---

## v4.8.2 — 2026-04-25 — testing: edge-case scenarios for synthetic harness.

Pure addition. Zero behavior change to `trade_genius.py`. Extends the v4.8.1 corpus from 25 to 50 scenarios by covering gate paths the original suite left unexercised: cooldown windows, per-ticker pnl cap, OR-staleness, volume gating, extension cap, sovereign regime, DI threshold, stop cap, market-open clock, midnight rollover, ring-buffer eviction, and trail-promotion threshold crossing.

**Added:**

- `synthetic_harness/scenarios/edge_cases.py` — 25 new deterministic scenarios:
  - **Cooldown:** `edge_cooldown_blocks_reentry`, `edge_cooldown_releases_at_901s`.
  - **Per-ticker pnl cap:** `edge_per_ticker_pnl_cap`.
  - **OR / data sanity:** `edge_or_price_sane_reject`, `edge_bars_none_data_failure`, `edge_current_price_zero`.
  - **Volume gating (TIGER_V2_REQUIRE_VOL=true):** `edge_volume_not_ready`, `edge_volume_below_threshold`.
  - **Extension / stop-cap rejects:** `edge_extension_max_pct`, `edge_stop_cap_reject`.
  - **Sovereign regime (index polarity):** `edge_sovereign_long_eject`, `edge_sovereign_short_eject`.
  - **DI gate:** `edge_di_below_threshold`, `edge_di_none`.
  - **Pre-market / time gate:** `edge_before_market_open`.
  - **Daily date reset:** `edge_daily_date_reset`.
  - **execute_entry edges:** `edge_shares_zero_high_price`, `edge_insufficient_cash`, `edge_stop_capped_path`.
  - **close_position edges:** `edge_idempotent_close_no_position`, `edge_trade_history_ring_buffer`, `edge_retro_cap_close`.
  - **Multi-action:** `edge_midnight_rollover`, `edge_short_count_isolated_reset`, `edge_trail_promotion_threshold`.
- 25 golden JSON outputs under `synthetic_harness/goldens/` recorded against v4.8.1 production code; replay is byte-equal.
- `synthetic_harness/scenarios/__init__.py` registers the new module so `python -m synthetic_harness list` shows 50 scenarios.

**Harness:**

- `synthetic_harness/runner.py::_reset_module_state` now also resets `TIGER_V2_REQUIRE_VOL` to its default (`False`) at scenario start. Volume scenarios flip the flag via `setup_callbacks`; without an explicit reset, that flag would leak into subsequent scenarios.

**Counts:**

- Scenarios: 25 → 50 (`python -m synthetic_harness list`).
- Smoke tests with `--synthetic`: 107 → 132 (`smoke_test.py --local --synthetic`).

---

## v4.8.1 (2026-04-24) — testing: synthetic trading harness + 25 scenario goldens.

This is a pure addition. Zero behavior change to `trade_genius.py`. The release introduces a hermetic, deterministic test harness that replays full bot decision paths against frozen "golden" outputs.

**Added:**

- New package `synthetic_harness/` — hermetic, deterministic test harness. Replaces external dependencies (clock, market data, FMP quote, Tiger DI, Telegram send, paper-state save, signals, trade log, near-miss writes) with in-memory stand-ins via monkeypatching. A `FrozenClock` makes `_now_et`, `_now_cdt`, `_utc_now_iso`, and `datetime.now()` deterministic so wall-clock drift never leaks into output.
- 25 named scenarios (`synthetic_harness/scenarios/`) covering the full decision surface: 5 long entries (`long_clean_entry`, `long_blocked_in_position`, `long_blocked_at_cap`, `long_blocked_polarity`, `long_blocked_loss_limit`), 5 short entries (mirrors), 5 long closes (STOP, TRAIL, EOD, HARD_EJECT_TIGER, MANUAL), 5 short closes (mirrors), 5 scan-loop scenarios (`loop_full_cycle`, `loop_trail_promotion`, `loop_eod_cleanup`, `loop_halted_trading`, `loop_scan_paused`).
- 25 golden JSON outputs under `synthetic_harness/goldens/` recorded against v4.8.0 production code. Each golden captures Telegram outbox, paper log, signals, trade-log writes, save_paper_state calls, gate snapshots, near-miss writes, and a recursive state-delta. Replay asserts byte-equal output (`json.dumps(..., sort_keys=True, indent=2)`).
- CLI: `python -m synthetic_harness {list,record,replay,diff}`. Subcommands: `list` enumerates scenarios; `record` writes/refreshes goldens; `replay` runs all scenarios and compares to goldens; `diff <name>` shows per-scenario diff.
- `smoke_test.py --synthetic` flag — registers the 25 scenarios as `t()` smoke tests. With `--synthetic`, the local suite expands from 82 → 107 tests.

**Structure:**

- `synthetic_harness/clock.py` — `FrozenClock` + `make_frozen_datetime_class(clock)` factory used to replace `trade_genius.datetime`.
- `synthetic_harness/market.py` — `SyntheticMarket` with `TickerFrame` dataclass; helpers `make_long_breakout_frame`, `make_short_breakdown_frame`, `make_index_bull_frame`, `make_index_bear_frame` produce 5-bar timelines tuned for clean-entry vs. blocked-entry shapes (controlled `breakout_vol_ratio`, `avg_vol`, gap, OR placement).
- `synthetic_harness/recorder.py` — `OutputRecorder` with `capture_*` callbacks for every external surface, plus `to_dict()` serializer.
- `synthetic_harness/state.py` — `state_snapshot(module, keys=CAPTURE_KEYS)` and recursive `state_diff(before, after)` for stable JSON diffs.
- `synthetic_harness/install.py` — `install(harness)` / `uninstall()` patches `PATCH_TARGETS` on the live `trade_genius` module; idempotent.
- `synthetic_harness/scenarios/__init__.py` — `Action` and `Scenario` dataclasses; registry built from all 5 scenario submodules.
- `synthetic_harness/runner.py` — `record_scenario(name)`, `replay_scenario(name)`, and `_dispatch(action)` for action kinds (`check_entry`, `execute_entry`, `close_position`, the short mirrors, `scan_loop`, `manage_positions`, `manage_short_positions`, `eod_close`, `tick_minutes`, `tick_seconds`, `set_price`, `set_frame`, `set_global`).
- `synthetic_harness/cli.py` + `synthetic_harness/__main__.py` — argparse CLI entry point.

**Behavior:**

- Zero change. `trade_genius.py` logic is untouched aside from the version-string + release-note bump. `synthetic_harness/` is test infrastructure only and is **not** referenced from any runtime code path. It is intentionally **not** copied into the Docker image (test infra only, not used at runtime).

**Tests:**

- Local smoke suite: 82 unchanged. With `--synthetic`: 82 + 25 = 107 (each scenario registers as one `t()` entry that calls `replay_scenario(name)` and asserts `ok`).
- All 25 goldens are byte-equal idempotent: re-recording produces identical files (`md5sum` stable across runs).

**Rollout:**

- No env var, no feature flag. The harness is opt-in via `--synthetic` for the smoke suite and via `python -m synthetic_harness` for ad-hoc use. Production path is unaffected.

---

## v4.8.0 (2026-04-24) — refactor: long/short collapsed via Side enum, dual-path under SSM_USE_COLLAPSED feature flag (Stage B1).

This is Stage B1 of the long/short harmonization (Stage A shipped in v4.7.0). The 6 near-mirror functions (`check_entry`/`check_short_entry`, `execute_entry`/`execute_short_entry`, `close_position`/`close_short_position`) are collapsed into 3 side-parameterized functions: `check_breakout(ticker, side)`, `execute_breakout(ticker, current_price, side)`, `close_breakout(ticker, price, side, reason)`.

**Bugs fixed:**

- None. Stage B1 is a pure structural refactor with the explicit invariant that every Telegram payload, every state mutation, and every return value is byte-identical to v4.7.0 for the same input. The differential test family (13 new smoke tests) asserts this against both the legacy and the collapsed paths.

**Structure:**

- New module `side.py` (~110 LOC) — defines the `Side` enum (`LONG`, `SHORT`) and the `SideConfig` frozen dataclass. The `CONFIGS` dict maps each side to its configuration: Telegram labels (`entry_label`, `entry_emoji`, `exit_emoji`, `cash_word`), state-dict attribute names (`positions_attr`, `daily_count_attr`, `daily_date_attr`, `trade_history_attr`), and direction methods (`realized_pnl`, `entry_cash_delta`, `close_cash_delta`, `or_breakout`, `di_aligned`).
- `Dockerfile` updated to `COPY side.py .` next to `paper_state.py` (lesson from PR #115/#117 — every new top-level Python module MUST be added to the Docker image or prod crashes on boot).
- 3 new collapsed functions in `trade_genius.py`: `check_breakout`, `execute_breakout`, `close_breakout`. Each accepts a `Side` argument and dispatches to the correct legacy body for byte-equal behavior in B1.
- The 6 v4.7.0 functions are renamed to `_legacy_check_entry`, `_legacy_check_short_entry`, `_legacy_execute_entry`, `_legacy_execute_short_entry`, `_legacy_close_position`, `_legacy_close_short_position`. Bodies are unchanged.
- 6 thin wrappers preserve the public names (`check_entry`, `check_short_entry`, `execute_entry`, `execute_short_entry`, `close_position`, `close_short_position`). Each wrapper routes to the collapsed path or the legacy path based on `SSM_USE_COLLAPSED` (default `"1"` = collapsed).
- 3 new helper functions: `_state_dict(cfg)`, `_daily_count(cfg)`, `_trade_history(cfg)` — return the live module-level dict/list for a given `SideConfig` via `globals()` lookup. Used by future PR B2 collapsed bodies; introduced in B1 for completeness.
- New env var `SSM_USE_COLLAPSED` (default `"1"`). Setting it to `"0"` in Railway env restores the v4.7.0 code path without a git revert. Provides instant rollback during the one-week soak.
- No callsites in `scan_loop`, `manage_positions`, `eod_close`, or anywhere else changed — they continue to call the public names which are now wrappers.

**Behavior:**

- Zero user-visible change. Telegram message wording identical, dashboard `/api/state` shape identical, paper-state schema identical, trade decisions identical. Differential tests prove parity across 13 fixtures covering every check / execute / close shape (in-position, post-cap, polarity-fail, polarity-pass, time-gate, clean entry, daily-loss-limit-blocked, dup-entry-blocked, stop-cap-reject, stop close, trail close, manual close, force-close-after-hours).

**State format:**

- Unchanged. Same fields as v4.7.0.

**Tests:**

- 13 new smoke tests under `differential:` family. Each fixture snapshots module state + Telegram outbox, runs the legacy path, snapshots deltas, resets to identical baseline, runs the collapsed path, snapshots deltas, then asserts byte-equal return value + state delta + Telegram payload.
- New helpers `run_diff_fixture`, `_capture_state`, `_drain_telegram_outbox`. Total 69 → 82.

**Rollout:**

- PR B2 (v4.8.1) ships only after one full trading week of B1 in prod with `SSM_USE_COLLAPSED=1` and zero anomalies. B2 inlines a single shared body into each `*_breakout` function and deletes the legacy functions, the wrappers' conditional, and the `differential:` test family.

---

## v4.7.0 (2026-04-24) — refactor + risk fixes: long/short entry/execute/close functions are now structural mirror images of each other, with three real bugs fixed in the process.

This is Stage A of long/short harmonization (Stage B — collapse to single `check_breakout(side)` / `execute_breakout(side)` / `close_position(side)` plus a Side enum — is deferred to a future PR).

**Bugs fixed:**

- **Daily loss limit now enforced for shorts.** Previously `execute_entry` had a ~50-LOC block at the top that summed today's realized + unrealized P&L across longs and shorts, set `_trading_halted=True` and aborted on breach. `execute_short_entry` had **none of this**, so shorts continued to open after the halt fired. Logic is now extracted into `_check_daily_loss_limit(ticker)` and called by both execute paths.
- **`daily_short_entry_count` now resets on a new day.** Previously `check_entry` reset the long counter when `daily_entry_date != today`, but `check_short_entry` had no equivalent block — the dict relied on key not-existing on day 1, which silently broke on process restart (yesterday's counts persisted in the state file). Added a `daily_short_entry_date` global, registered it in `paper_state.py` save/load round-trip, and added the parallel reset block to `check_short_entry`.
- **`scan_loop` control flow is now symmetric.** Previously `check_entry` returned `(bool, bars)` and `scan_loop` called `execute_entry` separately, while `check_short_entry` returned `None` and called `execute_short_entry` itself. Any future code that wants to gate the entry between check and execute (kill switch, second-bar confirmation) had to handle two control flows. `check_short_entry` now returns `(bool, bars)` and `scan_loop` calls `execute_short_entry(ticker, current_price)` after a `True` return — same pattern as long.

**Structure (zero behavior change):**

- New helper `_check_daily_loss_limit(ticker) -> bool` — single source of truth for the daily P&L sum + halt gate.
- New helper `_ticker_today_realized_pnl(ticker) -> float` — sums today's realized P&L for a given ticker from both `trade_history` (long closes) and `short_trade_history` (short COVERs). Used by both `check_entry` and `check_short_entry` for the per-ticker $-50 loss cap.
- Gate ordering harmonized: both `check_entry` and `check_short_entry` now run gates in the same canonical order (halt → pause → time → daily-counter reset → OR data → daily cap → in-position → cooldown → ticker loss cap → fetch → sanity → polarity → PDC → sovereign → DI → stop-cap → return).
- Return contract harmonized: both check functions return `(False, None)` on every guard and `(True, bars)` on success.
- `execute_entry` / `execute_short_entry` / `close_position` / `close_short_position` rewritten as structural mirrors of each other. Differences are now ONLY direction symbols (`>` vs `<`, `+` vs `-`, `or_high` vs `or_low`, `+DI` vs `-DI`), state name flips (`positions` vs `short_positions`), helper flips (`_capped_long_stop` vs `_capped_short_stop`), and intentional asymmetry (`paper_trades` is long-only — short rows are synthesized from `short_positions` + `short_trade_history` for the dashboard/trades surfaces).
- Stripped unnecessary `global` declarations (Python only needs `global` for assignment, not for mutating dict/list contents).

**Behavior:**

- Shorts will now (correctly) be halted by the daily loss limit going forward. This is a behavior change but is the intended risk-control behavior — previously shorts could keep firing after longs were halted.
- All other behavior unchanged: entry prices, stop pcts, daily caps, trail rules, telegram message text are all identical.

**State format:**

- Added one new field `daily_short_entry_date` (string) to the load/save schema. Loaders default to `""` when missing, so the field is forward+backward compatible.

**Tests:**

- 7 new smoke tests under `# v4.7.0 — long/short harmonization`. Total 62 → 69.

---

## v4.6.0 (2026-04-24) — refactor: extract paper-state I/O (load/save/reset + dedicated lock + `_state_loaded` guard) out of `trade_genius.py` into a new `paper_state.py` module. Pure code motion, zero behavior change.

`trade_genius.py` previously hosted all paper-book persistence inline alongside trading logic. This release pulls the persistence cluster — `save_paper_state()`, `load_paper_state()`, `_do_reset_paper()`, the `_paper_save_lock` threading lock, and the `_state_loaded` startup guard — into a dedicated module that owns just the I/O concern. The actual mutable state globals (`paper_cash`, `positions`, `short_positions`, `trade_history`, `or_high`, etc.) STAY in `trade_genius.py` because they have ~200 read sites across `trade_genius.py`, `dashboard_server.py`, `telegram_commands.py`, and `smoke_test.py`; migrating them to attribute access on a singleton is out of scope for this PR.

**Added (`paper_state.py`, ~240 LOC):**

- `save_paper_state()` — atomic JSON write to `PAPER_STATE_FILE`, gated by `_state_loaded`, with the v4.1.1 inside-the-lock snapshot construction and the v3.3.1 data-loss guard preserved verbatim.
- `load_paper_state()` — disk → in-memory hydration, including the v4.1.2 `.clear()` symmetry, the v4.0.8 fresh-book hard reset on parse failure, the per-key `last_exit_time` try/except, and the cross-day daily-counts reset.
- `_do_reset_paper()` — `/reset` confirm callback target.
- `_paper_save_lock` (module-owned) and `_state_loaded` (module-owned).
- `_tg()` helper that returns the live `trade_genius` module whether it's running as `__main__` (production) or imported as `trade_genius` (smoke tests). Same pattern that `telegram_commands.py` uses since v4.5.4.
- The `__main__` aliasing prelude — same trick from v4.5.4 — to make `from trade_genius import (...)` resolve to the already-loaded `__main__` module instead of re-executing `trade_genius.py` from disk under a second module name.

**Changed (`trade_genius.py`):**

- Three function definitions removed (~220 LOC): `save_paper_state`, `load_paper_state`, `_do_reset_paper`.
- `_paper_save_lock = threading.Lock()` and `_state_loaded = False` lines removed; both globals are now owned by `paper_state.py`.
- Re-export shim added BEFORE `import telegram_commands` (telegram_commands imports `save_paper_state` and `_do_reset_paper` from `trade_genius` at its own import time, so the re-export must be visible by then):
  ```python
  import paper_state
  from paper_state import save_paper_state, load_paper_state, _do_reset_paper
  ```
- Every existing internal call site (`save_paper_state()` at L4099/L4637/L4779/L5205/L5421/L5521/L6340/L8487, `load_paper_state()` at startup, `_do_reset_paper()` from the reset callback) resolves to the re-exported name and continues to work without edits.
- `BOT_VERSION` 4.5.4 → 4.6.0 (minor bump because a new module shipped).
- `CURRENT_MAIN_NOTE` rewritten for v4.6.0; `_MAIN_HISTORY_TAIL` prepended with the v4.5.4 entry.

**Changed (`Dockerfile`):**

- Added `COPY paper_state.py .` after `COPY telegram_commands.py .`. This is critical: Railway uses the Dockerfile (not nixpacks) when one exists, and the Dockerfile enumerates Python files explicitly. v4.5.2 crashed at startup because it forgot this step (fixed in v4.5.3); v4.6.0 doesn't repeat the mistake.

**Operational notes:**

- Deployment pattern follows v4.5.4's lessons: `paper_state.py` aliases `__main__` in `sys.modules` exactly the way `telegram_commands.py` does, so prod's `python trade_genius.py` entrypoint (which registers trade_genius as `__main__`, NOT as `trade_genius`) doesn't trigger a circular re-execution when `paper_state.py` resolves trade_genius globals via `_tg()`.
- The re-export shim means callsites in `telegram_commands.py` (`from trade_genius import ..., _do_reset_paper, ..., save_paper_state, ...`) keep working without edits.
- State file format on disk is byte-identical. No migration. `git revert` the merge commit is a clean rollback.
- Smoke suite grows 59 → 62 tests; three new `v4.6.0:` checks lock in module presence, the re-export identity, and ownership of `_state_loaded` / `_paper_save_lock`.

**Validation:**

- `python3 -c "import ast; ast.parse(open(f).read()) for f in ['trade_genius.py','telegram_commands.py','paper_state.py']"` — all parse.
- `SSM_SMOKE_TEST=1 python3 smoke_test.py --local` — 62 passed · 0 failed.
- Simulated prod startup via `runpy.run_path('trade_genius.py', run_name='__main__')` — `cmd_help` resolves on `telegram_commands`, `save_paper_state` is identical to `paper_state.save_paper_state`, `BOT_VERSION == "4.6.0"`.

---

## v4.5.4 (2026-04-24) — fix(deploy): resolve circular-import crash introduced by v4.5.2's Telegram-command extraction. Prod runs `python trade_genius.py`, so the file is registered in `sys.modules` as `__main__` — NOT as `trade_genius`. When `run_telegram_bot()` did `import telegram_commands`, the new module's top-level `from trade_genius import (...)` re-executed trade_genius.py from disk under a second module name, which re-entered `run_telegram_bot()` while `telegram_commands` was still partially initialized — `AttributeError: ... has no attribute 'cmd_help'`.

Fix: at the very top of `telegram_commands.py`, alias the already-loaded `__main__` module to `trade_genius` in `sys.modules` (guarded by a `BOT_NAME == "TradeGenius"` sentinel check so we don't accidentally overwrite a real trade_genius import in tests). Also replaced the three direct `trade_genius._scan_paused` references in `cmd_monitoring` with `_tg_module()._scan_paused` for symmetry. No behavior change — same module object, just both names resolve to it.

---

## v4.5.3 (2026-04-24) — fix(deploy): add `COPY telegram_commands.py .` to Dockerfile so the new module ships in the container image. v4.5.2 crashed at startup with `ModuleNotFoundError: No module named 'telegram_commands'` because Railway uses the Dockerfile (not nixpacks) when one exists, and the Dockerfile only enumerated `trade_genius.py`, `dashboard_server.py`, and `dashboard_static/`. The new `telegram_commands.py` introduced in v4.5.2 was therefore absent from the build context. One-line additive change to the Dockerfile, no Python edits beyond the version bump. Hot-restores production.

---

## v4.5.2 (2026-04-24) — refactor: extracted main-bot Telegram command handlers into `telegram_commands.py` (~1164 LOC) for maintainability. Pure code motion, zero behavior change.

All 25 top-level `cmd_*` handlers plus `reset_callback` and `_reset_authorized` moved out of `trade_genius.py` into a new `telegram_commands.py` module. Handler registrations in `run_telegram_bot()` updated to reference the new module (e.g. `CommandHandler("status", telegram_commands.cmd_status)`). Menu-callback invocations via `_invoke_from_callback` likewise updated. Sub-bot class methods (`TradeGeniusBase/Val/Gene.cmd_*`) are bound methods and were NOT touched. The `_auth_guard` TypeHandler stays in `trade_genius.py` since it owns owner-ID enforcement for the whole bot. Smoke suite grows from 57 → 59 tests (two new `refactor:` tests verify the move).

---

## v4.5.1 (2026-04-24) — refactor: split dashboard `index.html` into `index.html` + `app.css` + `app.js` for cleaner separation of concerns. Zero visual change.

Pure code motion. The previous `dashboard_static/index.html` carried a ~440-line `<style>` block and two `<script>` blocks totalling ~1,580 lines of inline JS, all in one 2,211-line file. Every CSS tweak invalidated the whole file for diffs; every JS tweak did the same; reviewers had to scroll past tokens they didn't care about. The file is now three files, each addressing one concern.

**Changed (`dashboard_static/`):**

- **`index.html`** — now 185 lines, pure HTML markup. The `<style>` block was replaced with `<link rel="stylesheet" href="/static/app.css">` in `<head>`. The two inline `<script>` blocks were removed; a single `<script src="/static/app.js" defer></script>` is injected before `</body>`. All element IDs, classes, DOM ordering, inline `style="..."` attributes, and inline `onclick`/event handlers were preserved byte-for-byte. The external font `<link>` to fontshare.com is unchanged.
- **`app.css`** (new, 438 lines) — every rule extracted verbatim from the original `<style>` block. `:root` variables, responsive overrides, media queries (mobile/tablet/desktop), `.kpi-value`/`.trade-row`/etc all live here.
- **`app.js`** (new, 1,585 lines) — the two original `<script>` IIFEs concatenated in their original order (main-tab IIFE first, then the tab-switcher / index-strip / per-executor-poll IIFE). Both IIFEs were already independent (separate `(function(){...})()` wrappers), so concatenation is equivalent. `defer` preserves execution-after-parsing semantics; every `document.getElementById()` call still resolves because the DOM is fully parsed before the deferred script runs.

**Server wiring (`dashboard_server.py`):**

- **No change required.** The existing `app.router.add_static("/static/", path=_STATIC_DIR, show_index=False)` mount at line 1557 already serves every file under `dashboard_static/` at `/static/<name>`. `/static/app.css` and `/static/app.js` are picked up automatically alongside the existing `/static/index.html`.

**Operational notes:**

- Visual rendering is byte-identical before vs after. Same CSS rules, same DOM, same JS execution order.
- Static assets are served without the login gate (matching the existing behavior for `/static/*`), so the browser can fetch `app.css` and `app.js` before the session cookie is set. This was the pre-existing behavior for any file dropped into `dashboard_static/`.
- Smoke tests (`smoke_test.py --local`) pass 57/57. `python3 -c "import ast; ast.parse(open('dashboard_server.py').read())"` parses clean.
- `grep -c "<style" dashboard_static/index.html` → `0`. `grep -c "<script>" dashboard_static/index.html` → `0`. Only the deferred `<script src=...>` tag remains.

---

## v4.4.1 (2026-04-24) — fix: regime banner no longer sticks on the last pre-close bucket after 15:55 ET; `_refresh_market_mode()` now runs at every scheduler tick, independent of market hours, and `gates.scan_paused` reflects auto-idle outside trading hours.

Before v4.4.1, `scan_loop()` short-circuited with a bare `return` when the clock was outside 09:35–15:55 ET, BEFORE it got to `_refresh_market_mode()`. So the cached `_current_mode` / `_current_mode_reason` globals stayed frozen at their last intra-session values (e.g. `POWER "14:00-15:55 ET"`) and `/api/state` kept serving them until the next open. At 16:58 ET the dashboard was still reporting `POWER`.

**Changed (`trade_genius.py`):**

- **`scan_loop()`** — `_refresh_market_mode()` is now called at the top of every cycle, ahead of the weekend / pre-open / after-close early returns. The classifier `get_current_mode()` already returns `(CLOSED, "outside market hours", 0.0)` for those windows and `(CLOSED, "weekend", 0.0)` for Sat/Sun; moving the refresh above the returns is what lets those classifications actually reach the cached globals. The refresh is observation-only and stays wrapped in the existing try/except.
- **`_scan_idle_hours`** — new module-level bool. `scan_loop()` sets it True when the cycle short-circuits because of market hours, False during live cycles. Orthogonal to `_scan_paused` (which is the user-set /pause flag).
- **Scheduler** — `scheduler_thread()` already calls `scan_loop()` every `SCAN_INTERVAL` seconds unconditionally (no clock gate of its own), so moving the refresh inside `scan_loop` is sufficient — the refresh fires even outside trading hours.

**Changed (`dashboard_server.py`):**

- **`/api/state` → `gates.scan_paused`** — now serialized as `_scan_paused OR _scan_idle_hours`. Previously this was just `_scan_paused`, so the UI showed "ACTIVE" all night even though no scanning was happening. After close / on weekends the flag is now True, matching reality.

**Added — smoke tests (`smoke_test.py`):**

- `regime: scan_loop refreshes mode to CLOSED after market close (16:30 ET simulated)` — monkeypatches `_now_et` to 16:30 ET, runs one `scan_loop()` cycle, asserts cached globals are `CLOSED` / `"outside market hours"`.
- `regime: scan_loop refreshes mode to CLOSED on weekend (Saturday simulated)` — monkeypatches `_now_et` to Saturday, asserts cached globals are `CLOSED` / `"weekend"`.
- `regime: _scan_idle_hours reflects after-hours idle state` — locks in that the auto-idle bool goes True after close and False during trading hours.
- `regime: /api/state scan_paused reflects auto-idle after close` — exercises `dashboard_server` serializer and confirms the union surfaces to the JSON payload.

**Operational notes:**

- No behavior change inside trading hours. During 09:35–15:55 ET `_scan_idle_hours` is False and `scan_paused` serializes identically to before.
- `_scan_paused` semantics are unchanged: the `/pause` and `/resume` Telegram commands still flip the user-pause flag, and `_scan_paused` continues to drive the "block NEW entries" gate inside `scan_loop()` (L6175). The dashboard just now OR's the idle state on top of it for display.

---

## v4.4.0 (2026-04-24) — security: all bot commands and reset callbacks now require user_id in TRADEGENIUS_OWNER_IDS; chat-based authorization fallback removed. CHAT_ID retained for outbound routing only.

The `/reset` callback gate (`_reset_authorized` in `trade_genius.py`) previously authorized any tap whose `chat_id` matched the configured `CHAT_ID` — meaning any member of the configured Telegram group chat could click Confirm on a reset button, even if their user_id was not in `TRADEGENIUS_OWNER_IDS`. The main-bot command gate (`_auth_guard`) and the Val/Gene sub-bot command gate (`TradeGeniusBase._auth_guard`) were already user-id-only; this release brings the reset-callback surface into line.

**Changed:**

- **`_reset_authorized`** in `trade_genius.py` no longer consults `chat_id` at all. The single gate is `user_id_str in TRADEGENIUS_OWNER_IDS`. If `query.from_user` is absent (channel posts, edited messages with no sender), the callback is denied with reason `no user_id`.
- **`reset_callback`** docstring and blocked-diagnostics message updated: the "allowed paper: <CHAT_ID>" line is removed (CHAT_ID is no longer an auth input and printing it as one was misleading). The log line no longer includes `CHAT_ID=%r` for the same reason.
- **Audit:** `CHAT_ID` and per-bot `TELEGRAM_CHAT_ID` in `trade_genius.py` are confirmed to be used only for outbound routing (`send_telegram`, `TradeGeniusBase._send_own_telegram`). `dashboard_server.py` does not reference `CHAT_ID` at all. No other auth path reads a chat id as an authorization signal.
- **Sub-bot verification:** `TradeGeniusVal` and `TradeGeniusGene` inherit `self.owner_ids = set(TRADEGENIUS_OWNER_IDS)` from `TradeGeniusBase.__init__`. No subclass override, no auth-relaxation path — confirmed by grep for `owner_ids`, `is_authorized`, `authorized_users`.

**Added — smoke tests (`smoke_test.py`):**

- `reset: v4.4.0 rejects non-owner user even when chat_id == CHAT_ID` — locks in that the pre-v4.4.0 bypass is gone.
- `reset: accepts fresh confirm from owner user_id (any chat)` — arbitrary chat, owner user_id → allowed.
- `reset: blocks unauthorized user from arbitrary chat`, `reset: v4.4.0 denies when user_id cannot be determined`, `reset: blocks stale confirm (>60s old) even for owner` — cover the freshness/missing-user-id edges under the new gate.
- `auth: sub-bot _auth_guard drops non-owner user` / `auth: sub-bot _auth_guard passes owner through` — confirms Val/Gene inherited guard behavior.

**Operational notes:**

- `TRADEGENIUS_OWNER_IDS=5165570192,167005578` (Val, Gene) on Railway is unchanged. The code hardening is the whole of this change.
- `CHAT_ID` env var is still required for outbound notifications; removing it would stop messages from going anywhere. It just no longer grants authority.

---

## v4.3.4 — Dashboard UI: zero-pad refresh countdown + pin `tabular-nums` on `#h-tick` (2026-04-24)

Row 2's refresh countdown (the `♻ Ns` chip next to the LIVE pill, introduced in v4.3.2) was rendering the seconds without zero-padding, so the chip width visibly shifted when the counter crossed the 10s boundary (`♻ 5s` → `♻ 13s`). v4.3.4 zero-pads the seconds to two digits and pins `font-variant-numeric: tabular-nums` on `#h-tick` so digit widths can't drift either way.

**Changed:**

- **`updateNextScanLabel`** in `dashboard_static/index.html` now formats the countdown as `♻ ${String(s).padStart(2,"0")}s` — emits `♻ 05s`, `♻ 13s`, `♻ 59s`. 3-digit values (if a scan interval ever exceeds 99s) still flow naturally. The matching `aria-label` / `title` use the padded string too.
- **Fallback tick branch** (the `tick ${n}s` path that fires before the first `__nextScanSec` value arrives) now zero-pads identically so the early-render state is also stable-width.
- **`#h-tick` inline style** gains `font-variant-numeric: tabular-nums` so proportional digits can't shift the chip independently of padding. Combined with zero-padding, the countdown is width-stable across every 00-99 tick.

**Why:** Zero-padding alone doesn't guarantee no-shift because JetBrains Mono-via-system-font-stack can still fall back to a proportional digit face on some platforms; `tabular-nums` is the real layout fix. Both together = rock-solid width.

**Changed:** `BOT_VERSION = "4.3.4"`; `CURRENT_MAIN_NOTE` rewritten; v4.3.3 note rolled into `_MAIN_HISTORY_TAIL`.

**Validation:** `smoke_test.py --local` PASS (49/49).

**Breaking:** None. Pure rendering tweak; no state, API, or layout change beyond width-stability of a single chip.

---

## v4.3.3 — Dashboard API: serialize `extension_pct` on `/api/state` per-ticker gates (2026-04-24)

PR #107 (v4.3.0) added an `extension_pct` field to the per-ticker gate snapshot in `trade_genius.py` (signed distance of live price past the OR edge, rounded to 2 decimals; `None` when the OR envelope has not been seeded). `dashboard_server.py`'s `_ticker_gates` hardcodes the list of keys it copies onto `/api/state` and dropped the new field. v4.3.3 extends the serializer so the dashboard (and any other `/api/state` consumer) can see how extended each break is at entry-eval time without tailing Railway logs.

**Changed:**

- **`dashboard_server.py::_ticker_gates`** now copies `extension_pct` from the gate snapshot onto the serialized per-ticker row. Float values are rounded to 2 decimals (defense-in-depth — the source already rounds); `None` / missing values pass through unchanged (OR not yet seeded for that ticker). Existing key order preserved; the new key is appended after `or_stale_skip_count`.

**Why:** After v4.3.0 shipped the `ENTRY_EXTENSION_MAX_PCT` guard, the gate snapshot carried the computed extension but the dashboard API stripped it. Surfacing the value lets the UI distinguish a fresh break (`ext ≈ 0%`) from a late chase (`ext > 1.5%`) at a glance, and matches the v4.3.0 note that promised dashboard pickup in a follow-up.

**Changed:** `BOT_VERSION = "4.3.3"`; `CURRENT_MAIN_NOTE` rewritten; v4.3.2 note rolled into `_MAIN_HISTORY_TAIL`.

**Validation:** `python3 -c "import ast; ast.parse(...)"` clean; `smoke_test.py --local` PASS (49/49).

**Breaking:** None. Additive field; consumers that ignore unknown keys are unaffected. Tickers with no OR yet emit `extension_pct: null`, matching the pre-seed contract.

---

## v4.3.2 — Dashboard UI: replace "scan in" label with ♻ recycle glyph (2026-04-24)

Row 2's next-scan countdown used to render as the text `scan in 13s` next to the LIVE pill. v4.3.2 swaps the literal word `scan in` for the `♻` (U+267B) recycle glyph so the countdown reads `♻ 13s` — a few pixels narrower, and unambiguous at a glance.

**Changed:**

- **`updateNextScanLabel`** in `dashboard_static/index.html` now writes `\u267B ${s}s` into `#h-tick` instead of `scan in ${s}s`. Font size, color, and font-family are unchanged (still inherits from the LIVE pill container).
- **Accessibility preserved.** `#h-tick` gets `title` + `aria-label` set to the full phrase `next scan in Ns` on every tick, so screen readers still describe the countdown semantically. Static fallback `title`/`aria-label="next scan countdown"` is set in the HTML for the initial render before the first tick.

**Why:** Visual tightening — the glyph is immediately recognizable as a refresh/next-scan indicator, and it trims ~6 characters from row 2. Row-2 on mobile (v4.3.1) already hides this chip at ≤420px, but on desktop and tablet the chip is visible and the shorter form reads cleaner.

**Changed:** `BOT_VERSION = "4.3.2"`; `CURRENT_MAIN_NOTE` rewritten; v4.3.1 note rolled into `_MAIN_HISTORY_TAIL`.

**Validation:** `smoke_test.py --local` PASS (49/49).

**Breaking:** None. Pure text swap; no layout, state, or API change.

---

## v4.3.1 — Dashboard UI: fit row-2 clock inline on iPhone (2026-04-24)

Row 2 (logo / TradeGenius / version / LIVE pill / clock) wrapped at 375px after v4.2.2 because the 14px bold `HH:MM:SS TZ` clock pushed the line over budget. v4.3.1 squeezes everything onto a single row across common phone widths (414 / 390 / 375 / 360).

**Changed:**

- **Row-2 now uses `flex-wrap: nowrap` at ≤420px** so items can't drop to a second line. Row padding trimmed (`16px → 12/10/8px` horizontal) and gap tightened (`10px → 8/6/5px`) as width decreases.
- **Clock font scales down with viewport.** ≤420px: 13px. ≤380px: 12px. Still white, still semi-bold JetBrains Mono with `tabular-nums`.
- **LIVE pill padding tightened on mobile** (`3px 10px 3px 8px → 3px 8px 3px 7px`, gap `8px → 6px`) so the pill costs a few fewer pixels without changing its visual identity.
- **Version text (`v4.3.1`) shrinks to 9.5px at ≤380px** with zero left margin so it stays adjacent to the wordmark rather than floating.
- **Seconds drop at ≤360px.** `__tgTickClock` checks `window.matchMedia("(max-width: 360px)")` and renders `HH:MM TZ` instead of `HH:MM:SS TZ` on the tightest phones. Above 360px the seconds still advance at 1Hz.

**Why:** User reported row 2 still wrapping on iPhone SE (375px) after v4.2.2 — the clock was the largest element on the line and even with the "scan in Ns" chip hidden, logo + wordmark + version + LIVE pill + 14px clock still overflowed the 343px inner width budget.

**Changed:** `BOT_VERSION = "4.3.1"`; `CURRENT_MAIN_NOTE` rewritten; v4.3.0 note rolled into `_MAIN_HISTORY_TAIL`.

**Validation:** `smoke_test.py --local` PASS (40/40).

**Breaking:** None. Desktop/tablet layout unchanged (all new rules are `@media (max-width: 420px)` or narrower).

---

## v4.3.0 — Entry-extension + stop-cap rejection guards (2026-04-24)

Two new signal-layer entry guards that prevent late/extended chase entries on otherwise-green breakouts.

**Added:**

- **`ENTRY_EXTENSION_MAX_PCT`** (default `1.5`) — maximum distance, in percent, that live price may sit past the OR edge when an entry is evaluated. LONG: `(price − or_high) / or_high * 100`. SHORT: `(or_low − price) / or_low * 100`. When exceeded, `check_entry` / `check_short_entry` log `SKIP {SYM} [EXTENDED] price=$X or_hi/or_lo=$Y ext=Z.ZZ%` and return without submitting the paper order.
- **`ENTRY_STOP_CAP_REJECT`** (default `1` / enabled) — when true, `check_entry` / `check_short_entry` reject any entry whose baseline stop (`OR_High − $0.90` for longs, `PDC + $0.90` for shorts) would need to be capped to `entry ± MAX_STOP_PCT` (0.75%). These are the same entries the code already logged as `stop capped`; the new flag treats the cap itself as a signal that the entry bar is too far past the OR trigger for the historical stop baseline to be meaningful. Logs: `SKIP {SYM} [STOP_CAPPED] baseline=$X requested_cap=$Y`. Disabling the flag restores the prior behavior where the capped stop is still placed.
- **Gate-snapshot exposure** — `_update_gate_snapshot` now writes `extension_pct` (rounded to 2 decimals; `None` if the OR envelope has not yet been seeded) per ticker. LONG snapshots measure distance above OR_High; SHORT snapshots measure distance below OR_Low. Dashboard serialization will pick this up in a follow-up.
- **Smoke coverage** — 8 new tests under the `guard:` prefix cover: env flag defaults, long-side 0.5% / 2.0% thresholds, short-side 2.0% threshold, `_capped_long_stop` / `_capped_short_stop` capped-flag semantics, and the `_update_gate_snapshot` extension_pct field. All 49 local tests pass.

**Why:** On 2026-04-24 12:42 CDT, META entered long at $677.06 while OR_High was $659.85 — entry was +2.61% above OR. All four gates (break / polarity / index / DI) were green, and the existing stop-cap logic clamped the stop to entry − 0.75% ($671.98). 32 min later HARD_EJECT_TIGER fired at −0.3% when DI+ wobbled to 24.59. A 0.75%-capped stop on an already-extended entry has near-zero room for noise, producing a predictable stop-out. The four entry gates never measured *how far past OR* the break had already traveled.

**Changed:** `BOT_VERSION = "4.3.0"`; `CURRENT_MAIN_NOTE` rewritten; v4.2.2 note rolled into `_MAIN_HISTORY_TAIL`.

**Validation:** `python3 -c "import ast; ast.parse(...)"` clean; `smoke_test.py --local` PASS (49/49).

**Breaking:** None. Disabling both flags (`ENTRY_EXTENSION_MAX_PCT=99 ENTRY_STOP_CAP_REJECT=0`) reproduces pre-v4.3.0 behavior exactly.

---

## v4.2.2 — Dashboard UI: clock right-aligned + iPhone-fit trade rows (2026-04-24)

Two UX refinements on top of v4.2.1.

**Changed:**

- **Row-2 clock moved to the far right** of the TradeGenius brand row. New order: `[logo] TradeGenius [ver] ............. [LIVE · scan in Ns] [CLOCK]`. The clock is now the rightmost element on the row, which gives it a fixed anchor instead of floating between version and LIVE pill.
- **Clock format is now `HH:MM:SS TZ`** (e.g. `18:49:13 CDT`), rendered in white (`#ffffff`), 14px semi-bold, JetBrains Mono with `tabular-nums` so digits don't wiggle as seconds advance. Makes the clock the most prominent numeric element on the row alongside the TradeGenius wordmark.
- **Client-side 1Hz tick** — `setInterval(window.__tgTickClock, 1000)` renders HH:MM:SS from `new Date()` locally so seconds advance smoothly between SSE frames. The tz token (`ET`/`CDT`/`CT`/`PT`/…) is extracted once from `server_time_label` via the existing regex and cached in `window.__tgClockTz`; we re-render whenever a new state frame lands.
- **Narrow-phone rule flipped.** `#tg-brand-clock` no longer hides at `≤380px`; instead the `scan in Ns` chip (`#h-tick`) hides first so the clock always stays visible. Clock also shrinks to 13px on narrow phones.
- **Today's Trades rows retuned to fit a 375px iPhone viewport** without wrap or horizontal scroll. Tightened base gaps (`10px → 6px`), tightened row padding (`6px 14px → 6px 10px`), added `font-variant-numeric: tabular-nums` on the row so cost/pnl/price columns line up perfectly. On `≤640px` the grid shrinks to `38px / 40px min / 30px / 42px / 74px min / auto`, row font drops to 12px, and the BUY/SELL chip shrinks (`padding: 1px 4px; font-size: 9.5px`). Unit-price column now uses `text-dim` + no `$` prefix for the dollar glyph (it's already in the column alignment).
- **New `@media (max-width: 400px)` rule** hides the unit-price column before the qty column, keeping the more load-bearing tail (total cost for BUY, signed pnl + pct for SELL) intact.
- Existing `@media (max-width: 360px)` rule updated to cascade from the `≤400px` layout and also hide the qty column, leaving `time | sym | action | tail` on the tightest phones.
- `BOT_VERSION = "4.2.2"`; `CURRENT_MAIN_NOTE` rewritten; v4.2.1 note rolled into `_MAIN_HISTORY_TAIL`.

**Validation:** `ast.parse` clean, `smoke_test.py --local` PASS (40/40). Expected iPhone 375px render: `18:49:13 CDT` white/bold at the right edge; trade row `10:09 TQQQ 162 SELL +$51.84 +0.52% 61.68` fits one line.

**Breaking:** None. No server API changes.

---

## v4.2.1 — Dashboard UI: row-2 clock restored + Today's Trades collapsed to one line (2026-04-24)

Two small dashboard-only UX changes bundled together.

**Added:**

- **Row-2 time clock** — a `#tg-brand-clock` span in the TradeGenius brand row, positioned between the version text and the LIVE pill. Shows `HH:MM ET` parsed out of the existing `server_time_label` field from `/api/state` (format `"Fri Apr 24 · 13:09:13 ET"` → `"13:09 ET"`). No date and no seconds — rows 1 and the index strip already carry that context. Refresh piggybacks on the state poll; label regex is time-only so any tz label (`ET` / `CDT` / `UTC`) passes through. Falls back silently to the `&mdash;` placeholder if the server label is empty or shaped unexpectedly.

**Changed:**

- **Today's Trades rows are now one line, not two.** Previous layout used a 3-row grid-template-area on mobile and a loose 2-line layout on desktop; replaced with a single-line grid `time | sym | qty | act | tail | price` with fixed min-widths so the numbers align cleanly down the list. `white-space: nowrap` on the row prevents wrap regardless of viewport. SELL rows now put signed P&L (`+$51.84`) with the matching-colour P&L % (`+0.52%`) in the tail column; BUY rows keep the total cost. The unit fill price moves to the end of the row.
- **`renderHeader()`** in `dashboard_static/index.html` sets `#tg-brand-clock.textContent` from a regex pulling `HH:MM:SS TZ` out of `server_time_label`.
- **Same treatment on the Val/Gene executor-tab trade render** so all three panels look identical.
- **CSS**: `.trade-row` grid + responsive rules rewritten. New `@media (max-width: 360px)` rule hides the QTY column so ticker/action/pnl/price stay on one line on the narrowest phones. `#tg-brand-clock` hides below 380px for the same reason.
- `BOT_VERSION = "4.2.1"`; `CURRENT_MAIN_NOTE` rewritten; v4.2.0 note rolled into `_MAIN_HISTORY_TAIL`.

**Validation:** `ast.parse` clean, `smoke_test.py --local` PASS (40/40). Row 2 header now reads: `[logo] TradeGenius v4.2.1 ... 13:09 ET [LIVE · scan in Ns]`. Trades example: `10:09  TQQQ  162  SELL  +$51.84 +0.52%  $61.68` on a single row.

**Breaking:** None. No server API changes; trade dict shape is unchanged.

---

## v4.2.0 — Dashboard UI cleanup: redundant header row + Sign Out + "· live" removed (2026-04-24)

User-visible dashboard chrome cleanup. The mobile header previously carried four rows: SPY/QQQ strip, TradeGenius logo + version + LIVE pill, Main/Val/Gene tab switcher, and a fourth row duplicating information already on rows 2-3 (today's date, the active executor name, a "Paper" chip, and a second LIVE pill). The fourth row was a hold-over from the pre-tabs single-panel layout and now just adds vertical noise on phones. This release deletes it across all three portfolio panels and removes two smaller redundancies on the same header.

**Removed:**

- **Main panel's `<header class="header">`** — the `#h-date` span, `#clock`, `#sb-conn`, and the `Sign out` link. The `/logout` route in `dashboard_server.py` is preserved (`app.router.add_post("/logout", h_logout)` still routes) so bookmarked URLs and direct hits keep working; only the visible button is gone.
- **Executor skeleton `<header>`** in `execSkeleton()` (Val + Gene panels) — the date, executor-name, mode (`📄 Paper` / `🟢 Live`), and the per-panel `.live-badge` LIVE pill. All duplicated the brand-row pill above.
- **`"· live"` suffix** on the version subtitle in `renderHeader()` — `v4.1.9 · live` → `v4.2.0`. The green LIVE pill on the right side of the brand row is kept; the text "live" next to the version was redundant with it.
- **Dead CSS**: `.header`, `.h-title`, `.h-right`, `.h-clock`, `.h-account`, `.h-account .h-conn`, `.h-account a`, `.h-account .h-acct-sep`, `.live-badge`, plus their `@media (max-width: 900px)` and `@media (max-width: 640px)` overrides.
- **Dead JS**: `tickN` / `clockTick()` / `setInterval(clockTick, 1000)`, the `$("h-date").textContent` line in `renderHeader`, the `const sb = $("sb-conn")` variable + all its `sb.textContent` / `sb.style.color` writes in `setConn`, the `setField(panel, "h-date"/"h-mode"/"h-acct", ...)` calls in `renderExecutor`, the `h-health` + `h-pulse` per-panel drivers (main `#h-pulse` on the brand row is unchanged), and the `setField(panel, "h-health", "fetch failed")` in the executor error path.

**Changed:**

- `setConn()` now only toggles the shared brand-row `#h-pulse` class (with a `null` guard) and drives the banner — no more dead writes to a hidden `#sb-conn` node.
- `CURRENT_MAIN_NOTE` rewritten for v4.2.0; v4.1.9 note rolls into `_MAIN_HISTORY_TAIL` (shortened to a 5-line summary).
- `BOT_VERSION = "4.2.0"`.

**Validation:** `ast.parse` clean, `smoke_test.py --local` PASS (40/40). Header still renders: SPY/QQQ strip → TradeGenius logo + `v4.2.0` + LIVE pill + "scan in Ns" → Main/Val/Gene tab nav. Fourth row is gone.

**Breaking:** None on the server API. `/logout` still accepts POST and clears the cookie; users who want to log out can POST to it directly (or we'll surface a menu later if needed).

---

## v4.1.9 — Dashboard audit deferred: M11 h_stream snapshot TTL cache (2026-04-24)

Dashboard-only performance fix for the deferred MEDIUM finding from the prior audit (`/tmp/audit_dash.md` M11). The SSE `h_stream` endpoint was calling `snapshot()` on every 2s tick per connected client, and `snapshot()` issues a live Alpaca snapshot request for ~30 symbols. With 3 browsers open across Val/Gene/home tabs, this fanned out to ~21.6k Alpaca round-trips per hour even though the underlying data only meaningfully changes on our 2-5s polling cadence.

**Added:**

- **`_cached_snapshot()`** in `dashboard_server.py` — module-level 10s TTL cache around `snapshot()`. Uses `threading.Lock` + double-checked locking so the thread-pool executor callers don't trigger duplicate Alpaca calls on simultaneous cache-miss.
- **Module globals**: `_SNAPSHOT_CACHE_TTL = 10.0`, `_snapshot_cache_lock`, `_snapshot_cache_value`, `_snapshot_cache_ts`.

**Changed:**

- **`h_stream`** now calls `_cached_snapshot` via `run_in_executor` instead of `snapshot`. Effective Alpaca fan-out drops from ~21.6k/h → ~4.3k/h regardless of how many SSE clients are connected (cache is process-wide).
- `/api/state` still calls `snapshot()` directly — explicit polls and Val-tab warmup see fresh data.
- `CURRENT_MAIN_NOTE` rewritten for v4.1.9; v4.1.8 note rolls into `_MAIN_HISTORY_TAIL`.
- `BOT_VERSION = "4.1.9"`.
- Smoke test pins BOT_VERSION to `4.1.9`.

**Validation:** `ast.parse` clean, `smoke_test.py --local` PASS (40/40). SSE cadence unchanged (still 2s); payload content is identical — clients can't tell the difference other than faster response.

**Breaking:** None.

---

## v4.1.8 — Dashboard audit deferred: M7 Robinhood toggle cleanup (2026-04-24)

Dashboard-only cleanup for the deferred MEDIUM finding from the prior audit (`/tmp/audit_dash.md` M7). Robinhood was removed in v3.5.0 along with all server-side `rh_*` payload keys, but the frontend kept ~70 lines of toggle machinery — two segmented buttons in the header, a localStorage-persisted `currentView`, a `slice(s, view)` indirection that proxy-read `rh_portfolio || portfolio`, and a click handler that re-rendered from `lastSnapshot`. All of it always resolved to paper because the server no longer ships `rh_*`.

**Removed:**

- **`.view-toggle` + `.view-toggle-btn` CSS block** (~28 lines)
- **`<span class="view-toggle">` in the header** with `view-btn-paper` / `view-btn-rh` buttons
- **`VIEW_KEY`, `loadView`, `saveView`, `currentView`** localStorage-backed state
- **`slice(s, view)`** — replaced with an inline `paperSlice(s)` that only reads paper keys. `renderAll` now passes this directly to `renderKPIs` / `renderPositions` / `renderTrades`.
- **`syncToggleButtons`, `setView`, and the document click handler** that delegated on `.view-toggle-btn`
- **`rh_portfolio` / `rh_positions` availability probe in `renderAll`** — the fallback that hid the RH button and force-reset the view if the server stopped shipping RH keys (always true for ~7 months now)

**Changed:**

- `CURRENT_MAIN_NOTE` rewritten for v4.1.8; v4.1.7 note rolls into `_MAIN_HISTORY_TAIL`.
- `BOT_VERSION = "4.1.8"`.
- Smoke test pins BOT_VERSION to `4.1.8`. The existing `rh_*` negative-assertions (the test that enforces these keys are NOT in the snapshot) stay — they are the contract we are now also honouring client-side.

**Validation:** `ast.parse` clean, `smoke_test.py --local` PASS (40/40).

**Breaking:** None. Users will notice the Paper/Robinhood pill is gone from the header (it already only had a Paper button visible because RH was hidden by server-key probe); all other UX is identical.

---

## v4.1.7 — Dashboard audit deferred: H7 _today_trades dedup (2026-04-24)

Dashboard-only fix for the last deferred HIGH finding from the prior dashboard audit (`/tmp/audit_dash.md` H7). Documented invariant (`trade_genius.py` ~L2530): long BUY/SELL rows live in `paper_trades`, short COVER rows live in `short_trade_history`. `_today_trades` iterates both lists but trusted the contract; if the contract is ever violated (future bug, state migration, a replay path that dual-writes) a short cover would appear in both lists and the UI would render it twice.

**Fixed:**

- **`_today_trades` defensively de-duplicates across lists (`dashboard_server.py:_today_trades`)** — a per-row key `(ticker.upper(), time|entry_time|exit_time, side, action)` is tracked in a set; a cross-list duplicate is collapsed to the first occurrence (paper_trades wins over short_trade_history). No behavioural change when the invariant holds, which is the steady state today.

**Added:**

- **Smoke test `dashboard: _today_trades de-duplicates cross-list short`** — seeds the same COVER row into both `paper_trades` and `short_trade_history`, asserts the dashboard returns exactly one FAKE-ticker row. Prevents regression if someone ever wires `close_short_position` to dual-write.

**Changed:**

- `CURRENT_MAIN_NOTE` rewritten for v4.1.7; v4.1.6 note rolls into `_MAIN_HISTORY_TAIL`.
- `BOT_VERSION = "4.1.7"`.
- Smoke test pins BOT_VERSION to `4.1.7`.

**Validation:** `ast.parse` clean, `smoke_test.py --local` PASS (40/40 — new dedup test added).

**Breaking:** None.

---

## v4.1.6 — Dashboard audit deferred: H6 _fetch_indices VIX sentinel (2026-04-24)

Dashboard-only fix for a deferred HIGH finding from the prior audit (`/tmp/audit_dash.md` H6). `_fetch_indices` conflated two failure modes behind a single `last is None or last <= 0` predicate: VIX (legitimately has no equity feed) and real equities that transiently report a 0 quote pre-market. The new shape emits VIX's placeholder row through an explicit branch tagged with `reason="vix_no_equity_feed"`, and keeps the real-equity loop strictly about real-equity semantics.

**Fixed:**

- **`_fetch_indices` distinguishes VIX sentinel from real-equity price=0 (`dashboard_server.py:_fetch_indices`)** — VIX is now emitted from an explicit branch at the top of the per-symbol loop (never participates in the snapshot response; it's not in `equity_symbols`). The row carries `available: false, reason: "vix_no_equity_feed"` so the frontend (and future log scrapers) can tell "intentional placeholder" from "real equity with a weird quote".
- **Numeric parsing tightened** — `latest_trade.price`, `daily_bar.close`, `previous_daily_bar.close` are each read through an explicit `if raw is not None: last = float(raw)` gate instead of `float(getattr(..., 0) or 0)`. A missing field now surfaces as `None` (drives `available=false`) instead of the ambiguous `0.0`. Change / change-pct math requires `last > 0 and prev_close > 0` so a 0 prev-close can never trigger a ZeroDivisionError, and a real ticker with `last=0.0` now renders with null-styling rather than being silently dropped.

**Changed:**

- `CURRENT_MAIN_NOTE` rewritten for v4.1.6; v4.1.5 note rolls into `_MAIN_HISTORY_TAIL`.
- `BOT_VERSION = "4.1.6"`.
- Smoke test pins BOT_VERSION to `4.1.6`.

**Validation:** `ast.parse` clean, `smoke_test.py --local` PASS (39/39).

**Breaking:** None. Frontend already treats `!r.available || r.last == null` as "n/a" — the new `reason` field is additive and ignored by older clients.

---

## v4.1.5 — Audit cleanup bundle (trade_genius): H6 + L1 + L2 + M3 (2026-04-24)

Picks up the three cosmetic / hygiene items deferred from `/tmp/audit_tg.md`. No functional behaviour change in the happy path; M3 adds a DEBUG-level log on Telegram edit-failure (previously silent), which is why this ships as a real patch bump rather than `[skip-version]`.

**Fixed:**

- **H6 / L1: dead `index_ok` local in `check_entry` (`trade_genius.py`)** — the intermediate boolean was computed and never consulted; the explicit per-index guards on the following lines do the actual gate work. Variable removed; comment adjusted to explain the history (why `snap["index"]` is no longer written from this spot).
- **L2 / M3: `/test` `prog.edit_text` silent swallow (`trade_genius.py`, 6 sites)** — each step's edit was wrapped in `try: ... except Exception: pass`, masking real Telegram / network failures behind the legitimate "message is not modified" case. Narrowed to `except telegram.error.BadRequest as e:` with `logger.debug("cmd_test: edit_text step <label>: %s", e)`. Behaviour is unchanged in the happy path (BadRequest still swallowed), but a real network failure now leaves a traceable breadcrumb. A top-level import `from telegram.error import BadRequest as TelegramBadRequest` was added for the narrower except.

**Changed:**

- `CURRENT_MAIN_NOTE` rewritten for v4.1.5; v4.1.4 note rolls into `_MAIN_HISTORY_TAIL`.
- `BOT_VERSION = "4.1.5"`.
- Smoke test pins BOT_VERSION to `4.1.5`.

**Not fixed:** None remaining from `/tmp/audit_tg.md`'s LOW / MEDIUM cleanup bucket.

**Validation:** `ast.parse` clean, `smoke_test.py --local` PASS (39/39), `grep index_ok` shows only live uses remain.

**Breaking:** None.

---

## v4.1.4 — Dashboard audit deferred: H2 Val/Gene tab warmup (2026-04-24)

Dashboard-only fix for a deferred HIGH finding from the prior audit (`/tmp/audit_dash.md` H2). If a user lands directly on the Val or Gene tab before Main has completed its first `/api/state` poll, the shared KPI row (Gate / Regime / Session) and scanner-level widgets render as blank "—" placeholders for up to 15 s until either Main's SSE tick or the executor poll lands. `window.__tgLastState` is the handoff channel and it starts unset on a cold page.

**Fixed:**

- **`selectTab` now warms `window.__tgLastState` on Val/Gene landing (`dashboard_static/index.html`)** — a new `warmupSharedState()` helper fires a one-shot `fetch("/api/state")` the first time a Val/Gene tab is selected while the shared cache is still empty. On success it writes `window.__tgLastState` and invokes the existing `__tgOnState` callback so `renderExecMarketState` + `refreshExecSharedKpis` paint immediately. Re-entrancy guarded by `__tgWarmupInFlight`, and the warmup runs in parallel with `pollExecutor(name)` so the executor-specific panel does not have to wait on the shared state round-trip. The existing 15 s Main SSE / poll cadence is untouched.

**Changed:**

- `CURRENT_MAIN_NOTE` rewritten for v4.1.4; v4.1.3 note rolls into `_MAIN_HISTORY_TAIL`.
- `BOT_VERSION = "4.1.4"`.
- Smoke test pins BOT_VERSION to `4.1.4`.

**Validation:** `ast.parse` clean, `smoke_test.py --local` PASS.

**Breaking:** None.

---

## v4.1.3 — Audit H3 (trade_genius): cross-day cooldown prune TZ consistency (2026-04-24)

Finishes one of the deferred HIGH-severity items from `/tmp/audit_tg.md`. No live bug was observed, but the cross-day cooldown prune in `reset_daily_state` mixed ET and UTC date arithmetic — fragile around DST transitions and midnight ET.

**Fixed:**

- **Cross-day cooldown prune now ET-only (`trade_genius.py:6062-6085`)** — `reset_daily_state` used `now_et.replace(09:30).astimezone(timezone.utc)` and compared the stored UTC `_last_exit_time` values against that UTC cutoff. Comparison was still UTC-to-UTC, but deriving the cutoff from an ET wall-clock time and then converting left the invariant opaque. Reworked: the cutoff is computed directly as ET (`session_open_et = now_et.replace(09:30)`) and each stored UTC exit is converted to ET via `.astimezone(ET)` before comparison. A comment spells out the invariant ("all date/session comparisons done in ET").

**Changed:**

- `CURRENT_MAIN_NOTE` rewritten for v4.1.3; v4.1.2 note rolls into `_MAIN_HISTORY_TAIL`.
- `BOT_VERSION = "4.1.3"`.
- Smoke test pins BOT_VERSION to `4.1.3`.

**Validation:** `ast.parse` clean, `smoke_test.py --local` PASS (39/39).

**Breaking:** None.

---

## v4.1.2 — Audit batch M (trade_genius): MEDIUM hygiene fixes (2026-04-24)

Batch M of the `trade_genius.py` audit. Cleans up three MEDIUM-severity items from `/tmp/audit_tg.md`. No behaviour change in the happy path; narrows failure modes and removes dead/tautological code.

**Fixed:**

- **`load_paper_state` dicts not cleared before `.update()` (`trade_genius.py:2789-2813`)** — `paper_trades` / `paper_all_trades` / `trade_history` / `short_trade_history` were already cleared-before-extend, but `positions`, `short_positions`, `daily_entry_count`, `or_high`, `or_low`, `pdc`, `user_config`, `daily_short_entry_count` merged via `.update()` only. If `load_paper_state` is ever called twice (module re-init, hot patch, a future test harness), stale in-memory keys survive across reloads. All dict loads now `.clear()` first to match the list semantics.
- **`_warm_matplotlib` silently swallowed exceptions (`trade_genius.py:1040-1049`)** — a broken matplotlib install would abort the warmup thread with no trace, and only surface an hour later when `/dayreport` tried to plot. A `logger.debug("matplotlib warmup failed: %s", e)` now gives operators a breadcrumb.
- **Dead `try/except` around `self.last_signal = {...}` in `TradeGeniusBase._on_signal` (`trade_genius.py:541-550`)** — the only operations inside the try were a dict-literal assignment and `float(price)` on an already-numeric value from line 534. The wrapper was tautological. Dropped.

**Changed:**

- `CURRENT_MAIN_NOTE` rewritten for v4.1.2; v4.1.1 note rolls into `_MAIN_HISTORY_TAIL`.
- `BOT_VERSION = "4.1.2"`.
- Smoke test pins BOT_VERSION to `4.1.2`.

**Not fixed (docs-only items):** M3 (Telegram edit_text silent pass — acceptable), M4 (rolled into v4.1.0's `entry_ts_utc` fix), M6 (false positive — retries already sleep).

**Validation:** `ast.parse` clean, `smoke_test.py --local` PASS.

**Breaking:** None.

---

## v4.1.1 — Audit batch H (trade_genius): HIGH fixes, race + fail-open gates (2026-04-24)

Batch H of the `trade_genius.py` audit. Concurrency around the signal bus + state saver, upstream sanity guards so downstream fail-open gates can't be tripped by bad quotes, and a correctness fix on the daily-loss halt's short-side filter.

**Fixed:**

- **`_signal_listeners` register/emit race (`trade_genius.py:231-280`)** — `register_signal_listener` did a compound `fn in list` → `list.append`, and `_emit_signal` snapshotted via `list(...)`. Two concurrent `start()` calls (supervisor respawn + init retry, hot-reload during patch) could both observe "not present" and both append the same callable, which would then double-execute every ENTRY/EXIT against Alpaca for the life of the process. A `threading.Lock` now scopes the read-test-append and the snapshot so registration is atomic and the iterate-snapshot is consistent.
- **`save_paper_state` built snapshot outside `_paper_save_lock` (`trade_genius.py:2715-2746`)** — the state dict was assembled from module globals (`positions`, `short_positions`, `trade_history`, …) before the lock was taken. `close_position` calls `save_paper_state`, and the 5-minute periodic saver also calls `save_paper_state`, so two savers could overlap — one building the dict while the other mutated the same globals, producing an inconsistent serialisation or a `RuntimeError: dictionary changed size during iteration`. Snapshot construction moved inside the lock; each mutable global is now shallow-copied into the snapshot under the lock so `json.dump` never re-reads live state.
- **`check_entry` / `check_short_entry` accepted `current_price <= 0` (`trade_genius.py:4071`, `4765`)** — Yahoo has been observed to return a 0 quote on thin names during pre-market. Every downstream sanity helper (`_or_price_sane`, staleness checks) returns True when fed 0 because it treats the zero as "no data" and fails open, so a bad quote would have slipped past every gate. Both entry paths now reject 0/negative prices right after `fetch_1min_bars`.
- **Daily-halt today-pnl filter on shorts (`trade_genius.py:4271-4279`)** — `today_pnl += sum(pnl for t in short_trade_history if t.get("date") == today_str and t.get("action") == "COVER")`. `paper_trades` is reset daily; `short_trade_history` is a rolling last-500 window. Any COVER row missing `date` or storing a divergent format would be silently dropped from today's loss sum, understating the day's realised loss by the full short P&L. Replaced with `_is_today(exit_time_iso)` — the canonical today-predicate already used by the per-ticker loss cap. Consistent after the v4.1.0 `exit_time_iso`-is-real-ISO fix.

**Changed:**

- `CURRENT_MAIN_NOTE` rewritten for v4.1.1; v4.1.0 note rolls into `_MAIN_HISTORY_TAIL`.
- `BOT_VERSION = "4.1.1"`.
- Smoke test now pins BOT_VERSION to `4.1.1`.

**Deferred:** H3 cooldown-prune window (no live bug, documented in audit doc), H6 dead `index_ok` local (cosmetic).

**Validation:** `ast.parse` clean, `smoke_test.py --local` PASS.

**Breaking:** None.

---

## v4.1.0 — Audit batch C (trade_genius): CRITICAL state + trade-log correctness (2026-04-24)

Focused audit of `trade_genius.py` only. Two critical correctness bugs that corrupted persisted state and mis-populated the persistent trade log. Runs in parallel with the v4.0.8/v4.0.9 dashboard audits on `dashboard_server.py`.

**Fixed:**

- **`load_paper_state` partial-load → next save wipes disk (`trade_genius.py:2805`)** — the prior handler caught every exception, set `_state_loaded = True`, and returned. If the load raised after assigning some globals (e.g. `paper_cash` loaded, then a format error before `positions.update(...)` ran), the periodic saver 5 minutes later would stamp that partial snapshot over the good on-disk file, permanently losing positions and trade history. The failure path now resets every in-memory global to a clean fresh-book state before unblocking saves, so at worst we persist a legitimate `$100k / no-positions` snapshot on top of the corrupted file — never a truncated one — and logs a loud ERROR with traceback instead of a terse one-liner.
- **`hold_seconds` always `null` + `entry_time_iso` not actually ISO (`trade_genius.py:4278, 4861, 4408, 5039, 5050`)** — `execute_entry` / `execute_short_entry` stored `entry_time` as the local CDT `HH:MM:SS` display string and also wrote that same string into `entry_time_iso` on close. `datetime.fromisoformat("15:30:45")` raises `ValueError`, which was silently swallowed by the persistent-trade-log hold-time try/except — so every trade-log row has shipped with `hold_seconds: null`. The mis-typed `entry_time_iso` in `trade_history` also poisoned every downstream `_is_today(...)` consumer (per-ticker loss cap, etc.), which could silently skip today's rows. Fix: every position dict now carries an `entry_ts_utc` field (UTC ISO from `_utc_now_iso()` at entry). Close paths (longs and shorts) prefer `entry_ts_utc` over `entry_time`, so `hold_seconds` is now populated and `entry_time_iso` is a real ISO string. `entry_time` stays as `HH:MM:SS` CDT for display.

**Changed:**

- `CURRENT_MAIN_NOTE` rewritten for v4.1.0; the v4.0.9 (dashboard MEDIUM) note rolls into `_MAIN_HISTORY_TAIL`.
- `BOT_VERSION = "4.1.0"`.
- Smoke test now pins BOT_VERSION to `4.1.0`.

**Not fixed (deferred):**

- HIGH: `_signal_listeners` lock, `save_paper_state` snapshot-under-lock, `today_pnl` short filter, `current_price <= 0` upstream guard — shipping in the next trade_genius audit PR.
- MEDIUM: state-load symmetrize with `.clear()` before `.update()`, matplotlib warmup DEBUG log, remove tautological `_on_signal` try/except — shipping in the MEDIUM batch PR.

---

## v4.0.9 — Audit batch 5: MEDIUM fixes, dashboard polish (2026-04-24)

Batch 5 of the audit pass. MEDIUM severity, scope restricted to `dashboard_server.py` + `dashboard_static/index.html`. Each edit fixes a latent correctness bug or removes dead code that would confuse future readers. No trading-logic changes.

**Fixed:**
- **Alpaca key regex missed mixed-case suffixes (`dashboard_server.py:_ALPACA_KEY_RE`)** — the scrubber used `[A-Z0-9]{10,}` after the `PK/AK/CK/SK` prefix, but Alpaca emits mixed-case key material. A real leaked key in an upstream error body would have slipped past the redactor. Pattern relaxed to `[A-Za-z0-9]{10,}`.
- **`_serialize_positions` crashed on bad on-disk numeric field (`dashboard_server.py`)** — a single malformed value on `trail_stop` / `trail_high` / `stop` / `entry_price` / `shares` (e.g. `"N/A"`) raised `ValueError` inside the snapshot serializer, which surfaced as HTTP 500 from `/api/state` and blanked the whole dashboard. Every numeric read now goes through a new `_safe_float` helper: one bad field drops only that position's trail info instead of exploding the snapshot.
- **`day_pnl` KPI colour class when value missing (`dashboard_static/index.html:renderKPIs`)** — `(pnl ?? 0) >= 0 ? 'delta-up' : 'delta-down'` painted the value green when `day_pnl` was actually `null` (boot, halted, no trades yet). Both the main number and the percentage sub-label now drop the colour class entirely when the value isn't finite.

**Removed:**
- **Dead `renderTpSync` + `tp-banner` DOM element** — the TP surfaces were deleted in v3.5.0, the server no longer ships `tp_sync` in state (smoke_test asserts this in the "bad keys" list), and the banner hasn't been reachable since. Dead JS + orphan DOM node deleted.

**Changed:**
- Login page title + brand wordmark `Spike Monitor` → `TradeGenius` (`dashboard_server.py:_login_page`). The project was renamed in v3.5.1 and the login page was the last stale surface.
- `BOT_VERSION` bumped `4.0.8` → `4.0.9`. `CURRENT_MAIN_NOTE` rewritten; lines ≤ 34 chars.

**Validation:**
- `ast.parse` clean on `dashboard_server.py`.
- `smoke_test.py --local` passes (version assertions retargeted to `4.0.9`).

**Breaking:** None.

---

## v4.0.8 — Audit batch 4: HIGH fixes, dashboard correctness (2026-04-24)

Batch 4 of the audit pass. Scope restricted to `dashboard_server.py` + `dashboard_static/index.html`. Every edit fixes a likely-wrong behaviour in a common path.

**Fixed:**
- **`/login` `FileField` crash (`dashboard_server.py:h_login`)** — `data.get("password")` can return a `FileField` for multipart POSTs, and `.strip()` on that raised `AttributeError`, surfacing as HTTP 500 instead of a clean 401. Now coerced via `str()`.
- **`/api/trade_log` stale `portfolio=tp` filter** — TP surfaces were deleted in v3.5.0, but the endpoint still accepted `portfolio=tp` and passed it through to the reader. Now rejected with 400 and a clear message.
- **Log tail row template (`dashboard_static/index.html:appendLogs`)** — rendered from `msg.slice(0,8)` / `msg.slice(9)` assuming every message began with an 8-char time prefix + space. Short messages or a future formatter change would have rendered garbled rows. Now renders from the structured `ts` / `level` / `msg` fields; a prefix-strip regex removes the duplicated time token from the body so the row doesn't show the time twice.
- **SSE reconnect race (`dashboard_static/index.html:scheduleStreamReconnect`)** — the 3-second stale-data watchdog and the SSE `onerror` handler both called `setTimeout(startStream, 15000)` directly. Back-to-back watchdog ticks could queue multiple reconnects, causing the browser to briefly hold two `EventSource` connections. A `streamReconnectTimer` guard now collapses duplicate schedules into one.

**Changed:**
- `BOT_VERSION` bumped `4.0.7` → `4.0.8`. `CURRENT_MAIN_NOTE` rewritten; lines ≤ 34 chars.

**Validation:**
- `ast.parse` clean on `dashboard_server.py`.
- `smoke_test.py --local` passes (version assertions retargeted to `4.0.8`).

**Breaking:** None.

---

## v4.0.7 — Audit batch 3a: MEDIUM fixes, dashboard hygiene/security (2026-04-24)

Batch 3a of the audit pass. MEDIUM severity, scope restricted to `dashboard_server.py` — logging hygiene, login-page XSS, session-secret floor, X-Forwarded-For trust, and Alpaca-key redaction in error bodies. No trading-logic changes.

**Fixed:**
- **`_RingBufferHandler.emit` fallback** — if the formatter raised, the record was silently dropped. Now falls back to a minimal record (`level name: message`) so handler failures still surface in `/stream`.
- **`_next_scan_seconds` silent exception** — now logs at DEBUG before returning `None`.
- **Sovereign-Regime snapshot warnings** — the three `except Exception: pass` blocks in `_sovereign_regime_snapshot` swallowed broken calls as benign `False`. They now `logger.warning` with tracebacks.
- **Login rate-limit XFF spoofing (`_client_ip`)** — X-Forwarded-For was trusted unconditionally, so an attacker hitting the app directly could rotate the header to bypass the 5-attempt-per-minute lock. XFF is now only trusted when `DASHBOARD_TRUST_PROXY=1`; otherwise the lock keys on `request.remote`.
- **`_login_page` error interpolation XSS** — the `error` argument was substituted into HTML unescaped. Now `html.escape(error)`.
- **Env session-secret floor raised to 32 bytes** — the env branch accepted ≥ 16 bytes while the file branch required ≥ 32. Unified: both branches now require ≥ 32.
- **Alpaca key redaction in `/stream`** — `_executor_snapshot` echoed the raw Alpaca error body into the ring buffer, so a 401 response referencing a bad key could surface `PK...` / `AK...` / `CK...` / `SK...` fragments to anyone reading the log viewer. A regex pass now replaces those prefixes (plus 10+ alnum chars) with `[REDACTED]` before the string is logged.

**Changed:**
- `BOT_VERSION` bumped `4.0.6` → `4.0.7`. `CURRENT_MAIN_NOTE` rewritten; lines ≤ 34 chars.

**Validation:**
- `ast.parse` clean on both modules.
- `smoke_test.py --local` passes (version assertions retargeted to `4.0.7`).

**Breaking:** None. Operators who currently rely on `X-Forwarded-For` behind a proxy must now set `DASHBOARD_TRUST_PROXY=1` explicitly.

---

## v4.0.6 — Audit batch 2: HIGH fixes (state resets, gate-snapshot latch, trail attribution, Telegram edge cases) (2026-04-24)

Batch 2 of the audit pass. All items are HIGH severity — likely-wrong behaviour in common paths, but not money/safety/auth (those were v4.0.5). No trading-logic changes; every edit either makes an existing path behave the way its comments already claim, or hardens a command against an edge-case crash.

**Fixed:**

- **Cross-day cooldown leak (`reset_daily_state`)** — `_last_exit_time` is persisted across restarts but was never pruned at session open. A previous-day exit at 15:54 ET would hold today's first-5-min re-entry under the 15-minute post-exit cooldown on a cold restart. Now `reset_daily_state` drops every entry older than today's 09:30 ET and logs the count.
- **Regime-transition spurious alerts (`reset_daily_state`)** — `_regime_bullish` and `_current_rsi_regime` are module globals used for "first transition of the session" attribution. They were never reset, so a mid-session restart on a bullish tape compared today's fresh regime to yesterday's stale cached value and fired a bogus `REGIME` alert on the next scan. Now both are reset to `None` / `"UNKNOWN"` at session open, so the first-of-day classification is a clean first transition.
- **`_last_exit_time` dict-comp wipe on load (`load_paper_state`)** — a single malformed ISO timestamp in `paper_state.json` raised inside the load dict-comp and wiped the *entire* cooldown map, disabling the 15-min guard for every ticker. Per-key try/except now skips (and logs) the bad row, keeping good rows intact.
- **`_gate_snapshot["index"]` per-side stamping (`check_entry`, `check_short_entry`)** — same failure class as PR #83's side-selection latch. Both the LONG and SHORT entry paths wrote `snap["index"]` keyed on the current side, which on a mid-cycle LONG→SHORT flip could stamp the wrong side's index flag over the canonical value. Canonical `_update_gate_snapshot()` already runs once per scan cycle with the authoritative side — the per-entry writes have been removed.
- **TRAIL vs STOP attribution (`manage_positions`)** — `pos.get("trail_active")` was set True the first time the position touched +1% gain and never unset. A position that went +1%, pulled back, and hit the original structural stop was still reported as "TRAIL" even though no profit was ever locked. Now the attribution is derived from whether the stop has actually ratcheted above entry (`pos["stop"] > pos["entry_price"]`), which is what the comment already claimed.
- **`cmd_retighten` TypeError (`trade_genius.py`)** — `"%.2f" % old` raised on any row where `old_stop` / `new_stop` came back `None`, killing the handler. Now coerced via `float(v) if v is not None else 0.0`.
- **`cmd_mode` NameError + unhandled set_mode errors** — bare references to `val_executor` / `gene_executor` raised `NameError` on a boot where those module globals were never bound (e.g. missing Alpaca keys). Now uses `globals().get(...)`. Unknown sub-modes (anything outside `paper`/`live`) are rejected with a friendly message instead of forwarded to `executor.set_mode`, and `set_mode` exceptions are caught and surfaced to the user.
- **`cmd_price` silent exception swallow** — the edit / delete / reply block was wrapped in bare `try / except Exception: pass`, which hid every BadRequest and left the user staring at "⏳ Fetching…" forever. Now logs the failure at DEBUG and attempts a plain `reply_text` as a fallback.

**Changed:**
- `BOT_VERSION` bumped `4.0.5` → `4.0.6`. `CURRENT_MAIN_NOTE` rewritten; lines ≤ 34 chars.

**Validation:**
- `ast.parse` clean on `trade_genius.py`.
- `smoke_test.py --local` passes (version assertions retargeted to `4.0.6`).

**Breaking:** None. No trading-logic changes. Dashboard unaffected.

---

## v4.0.5 — Audit batch 1: CRITICAL fixes (halt gate, signal bus, dashboard TZ, login CSRF) (2026-04-24)

First batch of a full-codebase audit pass. All items here are CRITICAL — money / safety / auth — and each edit is the smallest change that removes the bug. Trading/signal logic is **unchanged**.

**Fixed:**
- **Daily-loss halt gate (`trade_genius.py:check_entry` P&L aggregation)** — `today_pnl = sum(t["pnl"] ...)` raised `KeyError` on any closed trade missing the `pnl` key, aborting the halt gate for that scan tick (the daily-loss ceiling was effectively bypassed on malformed rows). Now uses `t.get("pnl") or 0`. The `short_trade_history` aggregation was also missing the symmetric `action == "COVER"` filter that the long branch had — closed shorts from prior sessions could be double-counted when they leaked into the day's list. Unrealized-P&L branches used `pos.get("shares", 10)` which silently substituted a 10-share fallback for dollar-sized positions, under-counting realized losses by ~10× on the slice of the book that's sized by dollar exposure. Now `pos.get("shares") or 0`. Net effect: halt gate triggers when it's supposed to, not several scans late.
- **Signal-bus listener idempotency (`register_signal_listener`)** — the listener list had no dedup, no lock, and no unregister path. Any secondary `executor.start()` (future supervisor re-spawn, retry path, hot-patch) registered the same callable N times, firing N Alpaca orders per ENTRY / EXIT event. Now a re-registration of an already-subscribed callable is a no-op that logs `signal_bus: listener already registered, skipping`.
- **`/api/executor/<name>` today's trades (`dashboard_server._executor_snapshot` trades block)** — the Alpaca `after` filter was `datetime.strptime(today_et, "%Y-%m-%d").replace(tzinfo=utc)`, i.e. the ET date string reparsed as UTC. Between 00:00–05:00 ET the ET date and UTC date differ, so today's fills were invisible on the dashboard for the first few hours of the day. The downstream `fdate != today_et` comparison used `filled_at`'s raw UTC date too, so fills after 20:00 ET were attributed to "tomorrow" and dropped. Both sides now use a real ET midnight (`datetime.combine(now_et.date(), time(0,0), tzinfo=et_tz).astimezone(utc)` for the API filter, and `filled_at.astimezone(et_tz).strftime("%Y-%m-%d")` for the day comparison).
- **`/login` CSRF hardening (`dashboard_server.h_login`)** — session cookie was `samesite="Lax"`, which still permits top-level form POSTs from foreign origins, and there was no `Origin` / `Referer` check. A login-CSRF or session-fixation attacker could pin a victim's browser to a password the attacker controls by getting them to submit a cross-site form. `/login` now rejects POSTs whose `Origin` or `Referer` host does not match the request `Host`; empty both (e.g. `requests.Session().post` from the CI smoke runner, which sends neither header) is still accepted, so CI is unaffected. Session cookie raised from `samesite="Lax"` to `samesite="Strict"`.

**Changed:**
- `BOT_VERSION` bumped `4.0.4` → `4.0.5`. `CURRENT_MAIN_NOTE` rewritten; lines ≤ 34 chars.

**Validation:**
- `ast.parse` clean on `trade_genius.py` and `dashboard_server.py`.
- `smoke_test.py --local` passes (version assertions re-targeted to `4.0.5`).
- Smoke-test prod `/login` flow unchanged (no Origin / Referer headers → allowed).

**Breaking:** None. No trading-logic changes. Dashboard and Telegram surfaces unchanged except for the corrected numbers they now show.

---

## v4.0.4 — Leaving beta + header consolidation + Val KPI sync (2026-04-24)

Drops the `-beta` moniker after four betas' worth of stability fixes (OR seed, DI seed, gate/scanner repairs, dashboard tab parity) and ships a round of UI cleanup that had been accumulating.

**Changed:**
- **`BOT_VERSION`** bumped `4.0.3-beta` → `4.0.4`. No more `-beta` suffix anywhere in release surfaces (startup card, `/version`, dashboard footer). `CURRENT_MAIN_NOTE` rewritten for the new version; `v4.0.3-beta` note rotated into `_MAIN_HISTORY_TAIL`.
- **Dashboard header consolidated.** The header used to render on three rows: brand row (TradeGenius + version), a per-tab meta row (`Fri Apr 24 · 11:18 ET  [Paper]  [● LIVE next scan 15s]  connected · Sign out`), and the tab switcher. The per-tab meta row duplicated status that lives better once: the `Paper` pill was redundant with the tab switcher's per-tab Paper/Live badge, the `connected` text was redundant with the pulsing `LIVE` pill itself, and `·` separator before `Sign out` was visual noise. The `LIVE` pill + scan countdown now sit on the brand row — shared across Main / Val / Gene tabs — so the status indicator is identical regardless of which tab is active. Per-tab row now reads `date · time ET  …  Sign out` with no duplicate status chrome.
- **"next scan Ns" → "scan in Ns"** in the live pill label. Reads as a sentence instead of a label.
- **Val / Gene KPI row mirrors Main.** Previously Val's KPI cells rendered literal `+` placeholders (the per-executor `fmtUsd` prefixed every non-negative value with `+` and under some Intl currency fallbacks produced a bare `+` when the currency formatter returned empty). Now the per-executor `fmtUsd` matches Main's (`$...` / `−$...`, no `+` surprise), Day P&L is computed server-side as `equity − last_equity` from Alpaca's Account object (same math Main uses), and Gate / Regime / Session are sourced from Main's shared `/api/state` — market-wide values identical on every tab.

**Added:**
- **`account.last_equity`, `account.day_pnl`** on `/api/executor/{name}`. Exposes prior-close equity (Alpaca's `last_equity`) alongside current equity so the front-end can render Day P&L + percent without a second round-trip.
- **`refreshExecSharedKpis(panel)`** JS helper. Called from `window.__tgOnState` whenever Main's `/api/state` arrives, so Val/Gene panels update Gate / Regime / Session in lockstep with Main without waiting for the next 15s executor poll.
- **Smoke test `version: no -beta suffix`** asserting `BOT_VERSION` does not contain the substring `beta`. Protects against accidental rollback to a beta moniker.

**Validation:**
- `ast.parse` clean on `trade_genius.py`, `dashboard_server.py`, `smoke_test.py`.
- `python smoke_test.py --local` → **39 / 39 PASS** (added one; the two `BOT_VERSION is 4.0.3-beta` / `CURRENT_MAIN_NOTE begins with v4.0.3-beta` assertions were rewritten to target `4.0.4`).
- `CURRENT_MAIN_NOTE` begins with `v4.0.4` and every line ≤ 34 chars.
- Mobile (375px viewport): `#tg-brand-row` wraps — LIVE pill drops below the brand/version line rather than overflowing, courtesy of `flex-wrap: wrap` on the container. KPI row still stacks 2-up at ≤640px.

**Breaking:** None. Existing `/api/executor/{name}` consumers see two new account fields (`last_equity`, `day_pnl`); missing-data case returns `null` (front-end renders em-dash).

---

## v4.0.3-beta — Opening Range seed + staleness guard tuning (2026-04-24)

Hot fix. v4.0.2-beta shipped mid-session and the scanner booted with stale Opening Range values: `or_high`/`or_low` were reloaded from persisted state (or filled via `collect_or()`'s FMP `dayHigh`/`dayLow` fallback, which is the whole-day range, not the 9:30–9:35 window). The `_or_price_sane` guard then tripped at its 1.5 % threshold on every ticker both sides and logged `SKIP <TICKER> (stale?)` before the break/gate evaluation ever ran. Result: zero signals, zero trades, for the entire 2026-04-24 session until this fix shipped. This release pulls today's real 9:30 ET opening range from Alpaca historical bars at boot (mirroring the v4.0.2-beta DI seeder) and widens the staleness guard to a real "something's broken" threshold.

**Added:**
- **`_seed_opening_range(ticker)`** in `trade_genius.py` — pulls 1m bars from Alpaca's `StockHistoricalDataClient.get_stock_bars` for the window `[today 09:30 ET, 09:30 ET + OR_WINDOW_MINUTES]`, picks the max high and min low, and writes them directly into `or_high[ticker]` / `or_low[ticker]`. No-op when `now_et < window_end` (pre-9:35 restarts; the scheduled `collect_or()` still runs). Safe on any Alpaca failure — logs warning and returns, existing Yahoo+FMP path in `collect_or()` unaffected.
- **`_seed_opening_range_all(tickers)`** — runs the seeder for every watchlist ticker, emits a `OR_SEED_DONE tickers=N seeded=M skipped=K` summary, and once at least one ticker is seeded marks `or_collected_date=today` so the 09:35 ET `collect_or()` doesn't overwrite the fresher Alpaca-sourced values. Called from the startup block **before** `startup_catchup()` and the DI seeder. Wrapped in try/except — failures are non-fatal.
- **`OR_WINDOW_MINUTES` env var** (default `5`) — matches the existing 09:30–09:35 ET convention in `collect_or`; configurable so a future release can widen the OR without touching code.
- **`OR_STALE_THRESHOLD` env var** (default `0.05` = 5 %) — replaces the previous hard-coded 1.5 % floor in `_or_price_sane`. The old value fired for routine intraday moves on volatile names (OKLO, QBTS, LEU regularly drift > 5 % within a single session) which killed every signal. 5 % is a real "OR vs live drift looks wrong" guard, not a "normal volatility" guard. `_or_price_sane(or_price, live_price, threshold=None)` still accepts an explicit threshold override for callers that want the old tight behaviour.
- **`or_stale_skip_count` module global** — per-ticker counter, incremented every time the staleness guard fires in `evaluate_long` or `evaluate_short`. Cleared on `reset_daily_state` when the trading day rolls over.
- **`/api/state` → `gates.per_ticker[].or_stale_skip_count`** (via `dashboard_server._ticker_gates`) — surfaces the counter alongside the existing `break`/`polarity`/`index`/`di` fields so silent OR-drift failures are visible without tailing Railway logs.

**Logging:**
- Per ticker: `OR_SEED ticker=META or_high=665.50 or_low=662.20 bars_used=5 window_et=09:30-09:35 source=alpaca_historical` (INFO).
- Summary: `OR_SEED_DONE tickers=16 seeded=16 skipped=0` (INFO). Pre-open restarts log `tickers=0 seeded=0 skipped=N — pre-OR-window`.

**Validation:**
- `ast.parse` clean on `trade_genius.py`.
- `python smoke_test.py --local` → **38 / 38 PASS** (added two: `or_seed: _seed_opening_range function exists`, `or_seed: staleness guard uses configurable threshold`).
- `CURRENT_MAIN_NOTE` begins with `v4.0.3-beta` and every line ≤ 34 chars.

**Breaking:** None. Seeder is best-effort; missing Alpaca credentials or network failures leave the bot in the pre-v4.0.3 behaviour (OR comes from `collect_or()`'s Yahoo+FMP chain at 09:35 ET). Staleness threshold widening is purely additive — the guard still fires on true staleness, just not on normal intraday volatility.

---

## v4.0.2-beta — DI pre-market seed at boot (2026-04-24)

A focused follow-on to v4.0.1-beta (#84) where DI was promoted to a real gate. Prior to this release DI started `null` on every ticker at boot and took ~`DI_PERIOD * 2` = ~30 closed 5m bars (~70 min of live RTH) to warm up. That meant every Railway redeploy during the trading day silently disarmed the DI gate for the first hour-plus of the session. This release pre-fills the DI 5m buffer from Alpaca historical bars at scanner startup so the gate is armed on the very first scan cycle.

**Added:**
- **`_seed_di_buffer(ticker)`** in `trade_genius.py` — pulls 1m bars from Alpaca's `StockHistoricalDataClient.get_stock_bars` for the window `[today 04:00 ET, now]`, resamples into closed 5m OHLC buckets, and classifies each bucket as today-RTH (≥ 09:30 ET) or today-premarket (< 09:30 ET). If the combined count is less than `DI_PERIOD * 2` the seeder additionally pulls the last ~70 min of yesterday's RTH session (14:50 → 16:00 ET) and prepends those bars as a fallback. The final oldest→newest stream is stored in `_DI_SEED_CACHE[ticker]`.
- **`_seed_di_all(tickers)`** — runs the seeder for every watchlist ticker and emits a `DI_SEED_DONE tickers=N seeded_with_nonnull_di=M skipped=K` summary line. Called from the startup block (after `startup_catchup()`, before `scheduler_thread`) so DI is seeded before the first scan cycle. Wrapped in a try/except: any failure is logged and startup continues — DI will warm up naturally from live ticks as the fallback.
- **`tiger_di(ticker)`** now merges `_DI_SEED_CACHE[ticker]` with live 5m bars resampled from Yahoo, keyed by real epoch bucket (`ts // 300`) so overlapping buckets dedupe cleanly. Live bars win on overlap (last-write-wins) so as the session progresses the seed is transparently superseded.
- **`_resample_to_5min_ohlc_buckets(...)`** — variant of `_resample_to_5min_ohlc` that returns a list of `{bucket, high, low, close}` dicts rather than parallel arrays, used by the merge path in `tiger_di`.
- **`_alpaca_data_client()`** — builds a read-only `StockHistoricalDataClient` from whichever of `VAL_ALPACA_PAPER_KEY` / `GENE_ALPACA_PAPER_KEY` is present. Returns `None` if neither is configured or the `alpaca.data.historical` import fails; callers tolerate `None` and log.
- **`DI_PREMARKET_SEED` env flag** (default `"1"`) — when `"0"` the seeder skips today's premarket bars (04:00–09:30 ET) and relies only on today-RTH + prior-day-RTH. Kill switch in case premarket noise degrades DI signal quality. Documented in `.env.example`.

**Logging:**
- Per ticker: `DI_SEED ticker=META bars_today_rth=12 bars_premarket=22 bars_prior_day=14 di_after_seed=28.5` (INFO level).
- Summary: `DI_SEED_DONE tickers=16 seeded_with_nonnull_di=14 skipped=2` (INFO level).

**Validation:**
- `ast.parse` clean on `trade_genius.py`.
- `python smoke_test.py --local` → **36 / 36 PASS** (added two smoke tests: `di_seed: _seed_di_buffer function exists`, `di_seed: DI_PREMARKET_SEED env var documented in .env.example`).
- `CURRENT_MAIN_NOTE` begins with `v4.0.2-beta` and every line ≤ 34 chars.

**Breaking:** None. Seeder is best-effort; missing Alpaca credentials or network failures simply leave the bot in the pre-v4.0.2 behaviour (DI warms up from live ticks). Gate semantics are unchanged — DI must still be ≥ 25 to clear the gate; the seed only front-loads the buffer so that threshold can be evaluated sooner.

---

## v4.0.1-beta — UI polish + scanner/gate fixes (2026-04-24)

A small follow-on to v4.0.0-beta that cleans up the 3-tab dashboard, fixes two scanner/gate bugs found once Gene was live, and ships a CI guard so future merges cannot silently land without a version bump.

**Changed / Fixed:**
- **#80 — refactor(dashboard): reorder top rows to ticker → brand → tabs.** The index ticker strip now renders above the TradeGenius brand, which in turn sits above the Main/Val/Gene tab row. Why: on mobile the previous order pushed the always-on ticker strip below the fold, defeating the point of an "always-on" market-state readout.
- **#81 — feat(dashboard): expand Val/Gene tabs to mirror Main layout.** Val and Gene panels now render the full widget set the Main tab has (regime banner, positions table, invested/shorted totals, recent-trades timeline) instead of the minimal account-only card. Why: executor tabs were visually disjoint from Main, making it harder to compare paper-book state against each executor at a glance.
- **#82 — feat(dashboard): share market-state widgets + per-executor trades on Val/Gene.** Market-state widgets (regime banner, index ticker strip) are rendered once and shared across tabs; each executor tab now also shows its own per-executor recent-trades list sourced from the Alpaca account activity. Why: duplicating widgets per tab caused three independent polls against the same endpoints, and executor tabs were missing the "what did Val/Gene actually do today" view that Main has always had.
- **#83 — fix(scanner): break side-selection latch; recompute from OR envelope each scan.** The scanner no longer latches to the side (long/short) chosen on the first bar that cleared the Opening Range envelope. Each scan now re-evaluates which side of the OR the current bar is on. Why: once a ticker was latched long, a subsequent bar that broke the OR-low would not produce a short entry until the next day — a silent miss on valid short setups.
- **#84 — fix(gates): remove volume fiction; surface DI as real gate.** The `volume` gate label was removed from the dashboard/status surfaces because no live scan actually consulted a volume threshold; instead, the ADX/DI+ strength check that *is* enforced is now exposed as its own `DI` gate. Why: operators were reading `volume: PASS` as a real confirmation when the check was a no-op, and the real strength gate (DI+ ≥ 25) was hidden inside a composite label.

**Added:**
- **`.github/workflows/version-bump-check.yml`** — a `pull_request` check (`version-bump-required`) that fails on PRs targeting `main` unless both `trade_genius.py` (BOT_VERSION) and `CHANGELOG.md` (top entry) are modified. Includes a `[skip-version]` token escape hatch for doc-only or CI-only PRs. Why: v4.0.0-beta almost shipped to prod without a CHANGELOG entry; a cheap pre-merge gate is the right backstop.

**Validation:**
- `ast.parse` clean on `trade_genius.py`.
- `python smoke_test.py --local` → **34 / 34 PASS**.
- `CURRENT_MAIN_NOTE` begins with `v4.0.1-beta` and every line ≤ 34 chars (verified in smoke).

**Breaking:** None.

---

## v4.0.0-beta — TradeGeniusGene + 3-tab dashboard (2026-04-24)

Second step of the v4 architecture: a second Genius executor (**Gene**) joins Val, and the dashboard grows a tabbed view so main's paper book, Val's Alpaca account, and Gene's Alpaca account are each visible at a glance. An always-on index ticker strip (SPY / QQQ / DIA / IWM / VIX) runs across the top of every tab.

**Added:**
- **`TradeGeniusGene`** (`NAME="Gene"`, `ENV_PREFIX="GENE_"`) — identical semantics to Val, just a different env namespace, state file, Telegram bot, and Alpaca account. Strict paper/live segregation is preserved (`tradegenius_gene_paper.json` vs `tradegenius_gene_live.json` never mix).
- **`gene_executor` module global** + Gene startup block guarded by `GENE_ENABLED` (default `1`) and the presence of `GENE_ALPACA_PAPER_KEY`. Gene registers itself on the signal bus at boot; if keys are missing it is silently skipped, same as Val.
- **`/mode gene …`** router on main bot — same semantics as `/mode val`, including the live sanity-check (`get_account()` must return `status=="ACTIVE"`) before the `confirm` flip is accepted.
- **`last_signal` capture** on `TradeGeniusBase._on_signal` so every executor remembers its most recent event for the dashboard card.
- **`/api/executor/{name}`** endpoint on `dashboard_server.py` (15s server-side cache, per-executor): returns `{enabled, mode, healthy, account:{cash,buying_power,equity,account_number,status}, positions:[...], last_signal, error}`. Cache is keyed by name so Val and Gene don't stomp each other.
- **`/api/indices`** endpoint on `dashboard_server.py` (30s server-side cache): one call to Alpaca's `StockSnapshotRequest` for SPY / QQQ / DIA / IWM plus a separate best-effort pull for VIX. Missing symbols (notably VIX on some feeds) are returned as `{available:false}` so the front-end renders "VIX: n/a" without breaking the strip.
- **3-tab dashboard** (`dashboard_static/index.html`): vanilla HTML/CSS/JS tab switcher with three panels — **Main** (the existing paper-book view, untouched), **Val**, **Gene**. Val/Gene panels poll `/api/executor/<name>` every 15s and render: mode badge (📄 Paper / 🟢 Live), account card (cash, buying power, equity, account number, status), positions table (ticker/side/qty/avg_entry/mark/unrealized $/unrealized %), invested + shorted totals, and the most recent signal line.
- **Index ticker strip** renders at the very top of the page regardless of tab, polls `/api/indices` every 30s, and shows last price + absolute + percent change color-coded green/red.
- **Env vars added:** `GENE_ENABLED`, `GENE_ALPACA_PAPER_KEY/SECRET`, `GENE_ALPACA_LIVE_KEY/SECRET`, `GENE_TELEGRAM_TOKEN`, `GENE_TELEGRAM_CHAT_ID`, `GENE_TELEGRAM_OWNER_IDS`, `GENE_DOLLARS_PER_ENTRY` (default 10000). `.env.example` now documents the full set.
- **Smoke tests (10 new):** `version: BOT_VERSION is 4.0.0-beta`, `version: CURRENT_MAIN_NOTE begins with v4.0.0-beta`, `version: CURRENT_MAIN_NOTE every line <= 34 chars`, `gene: TradeGeniusGene class exists`, `gene: state file path segregates paper vs live`, `gene: gene_executor module global exists`, `shorts_pnl: dashboard snapshot shows profitable short with positive pnl`, `shorts_pnl: positions text shows profitable short with +sign`, `shorts_pnl: realized short pnl storage is positive for profitable cover`, `dashboard: /api/executor/val endpoint exists and returns disabled gracefully when Val is off`, `dashboard: /api/indices endpoint exists`, `dashboard: /api/indices handles missing Alpaca client gracefully`. Total smoke = **34 / 34 PASS** (was 24).

**Shorts P&L investigation — no display bug found:**
Per the PR #69 spec, the parent agent had verified that storage math for short P&L is correct and pointed at the display layer as the likely sign-flip site. An exhaustive audit of every short-P&L surface — `dashboard_server._serialize_positions`, `trade_genius._build_positions_text`, `trade_genius._status_text_sync`, `trade_genius._open_positions_as_pseudo_trades`, `trade_genius._chart_dayreport`, `trade_genius._format_dayreport_section`, and `trade_genius.close_short_position` (storage) — showed every location already computing `(entry - current) × shares` (unrealized) or `(entry - cover) × shares` (realized) with the correct sign. **No code change was needed.** To guard against future regressions, three smoke tests were added that seed a profitable short at `entry=100, current=95, shares=10` and assert the dashboard snapshot, `/status` positions text, and `close_short_position` storage all report a **+$50** P&L.

**Design choices (Gene's call):**
- Per-executor 15s cache on `/api/executor/<name>` and 30s on `/api/indices`: the dashboard polls 4× per minute per tab, but each Alpaca account is only hit at most once per cache window. Live accounts with real rate limits are safe.
- Alpaca's own `StockHistoricalDataClient.get_stock_snapshot` for the index strip (re-uses the executor's paper keys, no new provider added). VIX is fetched in a separate call so its potential absence from the equity snapshot doesn't blank the strip.
- Display-layer-first investigation of the shorts sign as the spec directed; when nothing was found, regression tests were added rather than touching working code.
- Main tab is the unchanged v3.x dashboard wrapped in a panel div — no changes to the existing paper-book rendering path.

**Validation:**
- `ast.parse` clean on `trade_genius.py`, `dashboard_server.py`, `smoke_test.py`.
- `python smoke_test.py` → **34 / 34 PASS** (local mode).

**Scope guardrails respected:**
- Main paper-book logic (Tiger 2.0, stops, EOD), v3.6.0 auth guard, v4.0.0-alpha signal bus / `TradeGeniusBase` / Val — all unchanged.
- No new third-party deps (Alpaca SDK already pinned for v4.0.0-alpha).

**Breaking:**
- None. Gene startup is opt-in via env: without `GENE_ALPACA_PAPER_KEY` (or with `GENE_ENABLED=0`), Gene is silently skipped and behavior matches v4.0.0-alpha.

**Deploy note:**
To activate Gene on Railway, set at minimum `GENE_ALPACA_PAPER_KEY`, `GENE_ALPACA_PAPER_SECRET`, and `GENE_TELEGRAM_TOKEN`. The dashboard's Val/Gene tabs will show "disabled" for any executor that's not booted.

---

## v4.0.0-alpha — TradeGeniusVal executor on Alpaca paper (2026-04-24)

First step of the v4 architecture: main's paper book is the **brain**, executor bots are **executors**. Main continues to run Tiger 2.0 against the paper book exactly as before; newly, every paper entry/exit fires an in-process signal that one or more executor bots mirror onto Alpaca. Val is the first executor; Gene arrives in v4.0.0-beta.

**Added:**
- **In-process signal bus** in `trade_genius.py`: `register_signal_listener(fn)`, `_emit_signal(event)`. Dispatch is async fire-and-forget — each listener runs in its own daemon thread so main never blocks on Alpaca and a single bad listener can't break the bus. Per-listener exceptions are logged and swallowed.
- **Signal event schema:** `{kind, ticker, price, reason, timestamp_utc, main_shares}`. Kinds: `ENTRY_LONG`, `ENTRY_SHORT`, `EXIT_LONG`, `EXIT_SHORT`, `EOD_CLOSE_ALL`.
- **`TradeGeniusBase`** — shared executor base. Per-bot Alpaca client (paper or live), per-bot state file (`tradegenius_<name>_<mode>.json` — **strict paper/live segregation**, two files never mixed), own Telegram bot with own `_auth_guard`, own owner whitelist env var.
- **`TradeGeniusVal`** (`NAME="Val"`, `ENV_PREFIX="VAL_"`) — the first executor instance. Nothing to override; all behavior is in the base. Gene (v4.0.0-beta) will be identical with `GENE_`.
- **Signal emission** wired into existing paper-book functions (no logic change, just an `_emit_signal(...)` call after the trade is recorded): `execute_entry` → `ENTRY_LONG`, `close_position` → `EXIT_LONG`, `execute_short_entry` → `ENTRY_SHORT`, `close_short_position` → `EXIT_SHORT`, `eod_close` → `EOD_CLOSE_ALL` (once at top, before per-position closes still fire).
- **`/mode val …`** router on main bot: `/mode val` shows Val's mode + account, `/mode val paper` flips immediately, `/mode val live confirm` requires the literal `confirm` token AND passes a **live sanity check** (`get_account()` on live creds, asserts `status=="ACTIVE"`, logs `account_number/cash/buying_power`) before the flip.
- **Val's own Telegram bot** (separate process loop, own `VAL_TELEGRAM_TOKEN` + `VAL_TELEGRAM_CHAT_ID`, own `_auth_guard` against `VAL_TELEGRAM_OWNER_IDS`). Commands: `/mode`, `/status`, `/halt` (emergency `close_all_positions(cancel_orders=True)`), `/version`.
- **Dependency:** `alpaca-py==0.43.2` added to `requirements.txt`. Imported lazily inside the executor (module import still works without it; Val just logs and skips orders if the SDK is missing).
- **Env vars:** `VAL_ENABLED` (default `1`), `VAL_ALPACA_PAPER_KEY/SECRET`, `VAL_ALPACA_LIVE_KEY/SECRET`, `VAL_TELEGRAM_TOKEN`, `VAL_TELEGRAM_CHAT_ID`, `VAL_TELEGRAM_OWNER_IDS`, `VAL_DOLLARS_PER_ENTRY` (default 10000), plus optional `ALPACA_ENDPOINT_PAPER/TRADE` URL overrides.
- **Smoke tests (6 new):** `val: TradeGeniusVal class exists`, `val: signal bus registration works`, `val: _emit_signal dispatches to all listeners`, `val: mode defaults to paper, flip to live without confirm fails`, `val: state file path segregates paper vs live`, plus the updated version-string assertions.

**Design choices (Val's call):**
- Async fire-and-forget dispatch (not synchronous, not a queue): Alpaca ack/reject can never block main's trade loop. Notifications go to Val's own Telegram when the order result returns.
- Sanity check before live flip: building a live client and calling `get_account()` is cheap and catches wrong-account / unfunded / restricted states before the first live order.
- Separate Val Telegram bot (not a channel inside main's bot): mirrors how Gene will work and keeps per-executor auth scopes clean.
- Strict paper/live state segregation: flipping modes reloads the right JSON; the two histories never cross-contaminate.

**Scope guardrails respected:**
- Dashboard (`dashboard_server.py`) untouched — the 3-tab dashboard is v4.0.0-beta (PR #69).
- Gene not built yet — PR #69.
- Main paper-book logic (Tiger 2.0, stops, EOD) unchanged — executor is additive, only `_emit_signal(...)` calls are new at trade-recording points.
- v3.6.0 main-bot auth guard unchanged.

**Breaking:**
- None for main's paper book. Val startup is **opt-in** via env: if `VAL_ENABLED=0` or `VAL_ALPACA_PAPER_KEY` is unset, Val is silently skipped at startup and the bot behaves exactly like v3.6.0.

**Deploy note:**
To enable Val on Railway, set at minimum `VAL_ALPACA_PAPER_KEY`, `VAL_ALPACA_PAPER_SECRET`, and `VAL_TELEGRAM_TOKEN`. Without those, Val is a no-op. Live requires `VAL_ALPACA_LIVE_KEY/SECRET` and the explicit `/mode val live confirm` command.

**Updated:**
- `BOT_VERSION` bumped from `3.6.0` to `4.0.0-alpha`.
- `CURRENT_MAIN_NOTE` rewritten for v4.0.0-alpha (every line ≤34 chars, em-dash as `\u2014`).
- `_MAIN_HISTORY_TAIL` rotated: v3.6.0 pushed in, v3.4.45 dropped.
- `.env.example` documents the new `VAL_*` section.
- `cmd_mode` (main bot) extended with the `/mode val …` sub-router; existing MarketMode behavior is unchanged for all other invocations.

**Validation:**
- `python3 -c "import ast; ast.parse(open('trade_genius.py').read())"` → OK
- `SSM_SMOKE_TEST=1 python3 -c "import trade_genius; print(trade_genius.BOT_VERSION)"` → `4.0.0-alpha`
- Smoke tests: 24/24 PASS (18 prior + 6 new).

---

## v3.6.0 — Telegram owner auth guard (2026-04-24)

Add a hard perimeter around the Telegram bot. Every incoming update is checked against `TRADEGENIUS_OWNER_IDS` before any command, callback, or message handler runs. Non-owners get **zero response** and the update is dropped server-side.

**Added:**
- `_auth_guard(update, context)` async function in `trade_genius.py` (above `run_telegram_bot`). Reads `update.effective_user.id`, compares it as a string against `TRADEGENIUS_OWNER_IDS`. On miss (including when `effective_user` is `None` — channel posts, etc.) it logs a warning with `update_id`/`user_id`/`chat_id` and raises `telegram.ext.ApplicationHandlerStop`. On match it returns silently so downstream handlers can run.
- `TypeHandler(Update, _auth_guard)` installed at `group=-1` in `run_telegram_bot()` so it fires **before** any default `group=0` handler (commands, callbacks, menus).
- Smoke tests: `auth: TRADEGENIUS_OWNER_IDS exists, RH_OWNER_USER_IDS removed`, `auth: _auth_guard exists and blocks non-owners`, `auth: _auth_guard passes owner through (no raise)`, `auth: _auth_guard drops update with no effective_user`.

**Renamed (HARD — no fallback):**
- Env var `RH_OWNER_USER_IDS` → `TRADEGENIUS_OWNER_IDS`. The old name is no longer read. Deployers **must** rename this var in their environment at deploy time.
- Module globals `_RH_OWNER_USERS_RAW` → `_TRADEGENIUS_OWNERS_RAW`, `RH_OWNER_USER_IDS` → `TRADEGENIUS_OWNER_IDS`. Internal references in `_reset_authorized` and `reset_callback` diagnostics updated accordingly.
- `.env.example` now documents `TRADEGENIUS_OWNER_IDS` with the v3.6.0 semantics (auth-guard whitelist, not just a /reset allow-list).

**Updated:**
- Telegram imports: added `TypeHandler` and `ApplicationHandlerStop` from `telegram.ext`.
- `BOT_VERSION` bumped from `3.5.1` to `3.6.0`.
- `CURRENT_MAIN_NOTE` rewritten for v3.6.0; `_MAIN_HISTORY_TAIL` rotated (v3.5.1 pushed in, v3.4.44 dropped).

**Unchanged:**
- Paper book, Eye of the Tiger 2.0, Hard Eject, EOD Close, scheduler, dashboard (owner check is at the Telegram layer only, not the HTTP layer; the dashboard still uses its `DASHBOARD_PASSWORD` gate).
- `_reset_authorized` logic — the second-layer /reset check still fires for defence in depth.

**Validation:**
- `python3 -m ast` OK on all 3 .py files
- `SSM_SMOKE_TEST=1 python3 -c "import trade_genius"` OK
- `smoke_test.py`: 17/17 PASS (14 prior + 3 new auth-guard tests; TRADEGENIUS_OWNER_IDS test replaces the old RH_OWNER_USER_IDS check)

**Deploy note:**
- Railway env var must be renamed `RH_OWNER_USER_IDS` → `TRADEGENIUS_OWNER_IDS` **at merge time**. If the rename is missed the whitelist falls back to the built-in default (Val only).

**Next:**
- v4.0.0-alpha adds `TradeGeniusBase` + `TradeGeniusVal` executors mirroring main paper signals onto Alpaca paper, `/mode paper|live` command, in-process signal bus.
- v4.0.0-beta adds `TradeGeniusGene` + 3-tab dashboard (Main/Val/Gene) with paper/live badges and index ticker strip.

---

## v3.5.1 — TradeGenius rename (2026-04-24)

Rename the project from Stock Spike Monitor to TradeGenius. No behavioural changes.

**Renamed:**
- File: `stock_spike_monitor.py` → `trade_genius.py`
- Asset: `stock_spike_monitor_algo.pdf` → `trade_genius_algo.pdf`
- Log file: `stock_spike_monitor.log` → `trade_genius.log`
- Dashboard HTML `<title>` and brand mark: "Spike Monitor" → "TradeGenius"
- Telegram startup card and `/version` command: "Stock Spike Monitor vX.Y.Z" → `BOT_NAME v BOT_VERSION`
- Algo PDF caption and filename: `TradeGenius_Algorithm_vX.Y.Z.pdf`

**Added:**
- `BOT_NAME = "TradeGenius"` constant in `trade_genius.py` (line 51)

**Updated entry points:**
- `railway.json` `startCommand` → `python trade_genius.py`
- `nixpacks.toml` `[start]` cmd → `python trade_genius.py`
- `Dockerfile` `COPY` and `CMD` → `trade_genius.py`
- `.github/workflows/post-deploy-smoke.yml` BOT_VERSION read → `trade_genius.py`

**Updated imports:**
- `dashboard_server.py` `sys.modules.get("stock_spike_monitor")` → `"trade_genius"` (and `import stock_spike_monitor` → `import trade_genius`)
- `smoke_test.py` `import stock_spike_monitor as m` → `import trade_genius as m`

**Unchanged:**
- Repo name stays `valira3/stock-spike-monitor` (and Railway project URL `stock-spike-monitor-production.up.railway.app`)
- Eye of the Tiger 2.0, Hard Eject, EOD Close, paper book, scheduler, dashboard layout
- `BOT_VERSION` bumped from `3.5.0` to `3.5.1`

**Validation:**
- `python3 -m ast` OK on all 3 .py files
- `SSM_SMOKE_TEST=1 python3 -c "import trade_genius"` OK
- `smoke_test.py`: 13/13 PASS

**Next:**
- v3.6.0 adds a Telegram owner auth guard (`TRADEGENIUS_OWNER_IDS` whitelist via `TypeHandler`)
- v4.0.0 introduces Alpaca-backed TradeGenius executors (Val + Gene) mirroring main paper signals

---

## v3.5.0 — Deletion Pass (2026-04-24)

Strip TradersPost, Robinhood, and Gmail/IMAP surfaces to clear the codebase before adding Alpaca connectivity in v4.0.0.

**Removed:**
- TradersPost webhook (`send_traderspost_order`, `TRADERSPOST_WEBHOOK_URL`)
- TradersPost paper book (`tp_positions`, `tp_paper_trades`, `tp_paper_cash`, `tp_*` state, `tp_state.json`)
- TradersPost Telegram bot (`TELEGRAM_TP_TOKEN`, `TELEGRAM_TP_CHAT_ID`, dual-bot wiring, `_run_both`)
- Robinhood IMAP poll (`rh_imap_poll_once`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `RH_IMAP_*`)
- Robinhood execution (`execute_rh_entry`, `rh_shares_for`, `RH_STARTING_CAPITAL`, `RH_MAX_*`, `RH_LONG_ONLY`, `RH_DOLLARS_PER_ENTRY`)
- Commands: `/rh_enable`, `/rh_disable`, `/rh_status`, `/tp_sync`, `/tp_sync_on_main`
- Dashboard TP snapshot from `/api/state`
- Smoke tests: all `tp_*`, `rh_*`, `traderspost`, `robinhood`, `imap`, `gmail` tests
- Module globals: `tp_positions`, `tp_paper_cash`, `tp_trade_history`, `tp_short_positions`, `tp_short_trade_history`, `tp_unsynced_exits`, `tp_state`, `tp_daily_entry_count`, `_tp_save_lock`, `_tp_state_loaded`, `_rh_reconcile_seen`

**Unchanged:**
- Eye of the Tiger 2.0 entry/exit logic (paper book)
- Hard Eject, EOD Close, morning OR breakout
- All paper-book state, dashboard paper tab, Telegram main bot

**Next:** v4.0.0 will add Alpaca-backed TradeGenius bots (Val + Gene) mirroring main paper signals.

---

## v3.4.36 — Peak-anchored profit-lock ladder (2026-04-22)

### Why

v3.4.35 shipped earlier today and got the direction wrong. The
ladder was *entry-anchored* — each tier said "at +N% peak gain,
set stop to entry + X%" with X growing from 0 (breakeven) up to
4.5% at the Harvest tier. The math looked clean on a spreadsheet
but broke the core trailing-stop instinct: as price rose *past*
entry + X, the gap between peak and stop *widened* instead of
tightening.

On Eugene's AVGO example (entry $411.30, peak $420.69, +2.28%
gain) v3.4.35 placed the stop at entry + 1.0% = $415.41 — a
$5.28 give-back, *worse* than the old flat 1% rule's $4.21.
Every additional cent of peak widened the give-back by exactly
one cent because entry + X is frozen and peak keeps climbing.
That is the opposite of what a profit-lock ladder should do.

v3.4.36 inverts the anchor. Every tier is now expressed as
*peak − X%* (long) or *peak + X%* (short), with X *shrinking*
as the peak climbs. The gap between peak and stop now narrows
monotonically with every higher tier — the trailing-stop
instinct restored and made explicit.

### What changed

**The ladder (peak gain → stop, give-back shrinks)**

| Peak gain | Long stop        | Short stop       | Phase   |
| :-------- | :--------------- | :--------------- | :------ |
| < 1.0%    | initial hard stop| initial hard stop| Bullet  |
| ≥ 1.0%    | peak − 0.50%     | peak + 0.50%     | Arm     |
| ≥ 2.0%    | peak − 0.40%     | peak + 0.40%     | Lock    |
| ≥ 3.0%    | peak − 0.30%     | peak + 0.30%     | Tight   |
| ≥ 4.0%    | peak − 0.20%     | peak + 0.20%     | Tighter |
| ≥ 5.0%    | peak − 0.10%     | peak + 0.10%     | Harvest |

Bullet tier (<1% peak) keeps the initial hard stop untouched
so micro-noise right after entry cannot knock the trade out.
From +1% onward the ladder owns the stop, strictly tighter
than the old rules. One-way ratchet preserved: long stops
take `max(tier_stop, initial_stop)` and short stops take
`min(tier_stop, initial_stop)` — stop can only tighten.

**Eugene's AVGO scenario (entry $411.30, peak $420.69)**

| Rule             | Stop    | Gap   | Lock-in  |
| :--------------- | :------ | :---- | :------- |
| Old 1%/$1 flat   | $416.48 | $4.21 | +1.26%   |
| v3.4.35 (broken) | $415.41 | $5.28 | +1.00%   |
| v3.4.36 (fixed)  | $419.01 | $1.68 | +1.87%   |

At Harvest tier (+5% peak) the give-back collapses to 0.10%
— effectively a snap-close on any give-back, which matches
the "lock the gain, don't ride it back down" intent.

**Code changes**

- `LADDER_TIERS_LONG` rewritten from `(gain_threshold, stop_pct_offset)` tuples to `(gain_threshold, give_back_pct)` tuples — semantics inverted
- `LADDER_HARVEST_FRACTION = 0.0010` kept as an alias to the ≥5% give-back (back-compat)
- `_ladder_stop_long` now computes `peak * (1.0 - give_back_pct)` instead of `entry * (1.0 + offset_pct)`
- `_ladder_stop_short` mirrors with `peak * (1.0 + give_back_pct)`
- /strategy and /algo bodies updated to display the new table ("peak − 0.50%" etc., all lines within 34-char Telegram width)
- CURRENT_MAIN_NOTE / CURRENT_TP_NOTE lead with the peak-anchored framing
- v3.4.35 rolled into history tails with a "now superseded by peak-anchored" note so /version makes the correction visible

### Tests

25 new tier-math tests covering every band (Bullet, Arm, Lock,
Tight, Tighter, Harvest) on both sides, plus a dedicated
AVGO-Eugene-scenario assertion and a monotonic gap-shrinking
design assertion (gap(peak=+5%) < gap(peak=+4%) < … < gap(peak=+1%)).
One-way ratchet, legacy fallback (no `initial_stop`), and the
34-char Telegram width budget all still covered.

Float-precision edge in two tier tests surfaced during rollout:
`99.00 × 1.005 = 99.49499…` rounds to 99.49 on most platforms
(and `101.00 × 0.995 = 100.495` rides the same knife-edge).
Both tests now accept either rounding with a 1-cent tolerance.

### Migration notes

Nothing to migrate — ladder is stateless, reads only `peak`,
`entry_price`, and `initial_stop` on each evaluation. Positions
open under v3.4.35 pick up v3.4.36 behavior on the next stop
check. The v3.4.23 0.75%-cap and v3.4.25 breakeven layers are
kept as idempotent safeguards and continue to run; the ladder
dominates once peak ≥ 1%.

---

## v3.4.35 — Profit-lock ladder (2026-04-22)

### Why

Eugene pinged the bot at 1:20 PM CDT with a screenshot of the
live AVGO trail: entry $411.30, peak $420.69, trail stop
$416.48. His message: "shouldn't we be closer with trail."
Pulling the code confirmed why: the live rule was
`max(peak × 1.0%, $1.00)` — a flat 1% distance with a $1
floor. On a $420 stock that's a $4.20 give-back; on the +2.28%
gain AVGO had printed, a $416.48 stop is locking in only about
+1.26% when price has already run +2.28%. Val's response was
that the existing rule surrenders too much hard-won gain as
the trade works, and the bot should tighten more aggressively
the further price travels away from entry.

The original spec (5R profit lock) was scrapped after one
iteration in favor of a cleaner approach: a six-tier
peak-based ladder that makes the tightening explicit and
scales the buffer with the move. The rule is readable in two
lines and predictable at every price point, which matters on
a bot that wakes the user with alerts.

### What changed

**The ladder (peak gain → stop)**

| Peak gain | Long stop            | Short stop           |
| :-------- | :------------------- | :------------------- |
| < 1.0%    | initial hard stop    | initial hard stop    |
| ≥ 1.0%    | entry (breakeven)    | entry (breakeven)    |
| ≥ 2.0%    | entry + 1.0%         | entry − 1.0%         |
| ≥ 3.0%    | entry + 2.0%         | entry − 2.0%         |
| ≥ 4.0%    | entry + 3.5%         | entry − 3.5%         |
| ≥ 5.0%    | entry + 0.9×peak     | entry − 0.9×peak     |

The stop tier is always driven by the highest gain reached
(`trail_high` for long, `trail_low` for short), not the
current gain. A pullback from +5% to +2% keeps the Harvest
stop; if price crosses it, the exit locks 90% of the run.

**Replaces the old 1%/$1 armed trail entirely**

- Old behavior: once peak hit +1%, trail at `max(peak × 1%,
  $1.00)` below peak. Flat 1% buffer regardless of how far
  the trade had worked.
- New behavior: structural stop below +1%, breakeven at +1%,
  and the ladder tightens monotonically as peak climbs. At
  +5% and above, stop locks in 90% of the peak gain (Harvest
  phase) and scales with the move — a +10% peak locks
  +9.00%, a +7% peak locks +6.30%.
- No `$1.00` minimum distance anymore. Percentage-of-entry
  buffers scale naturally with price: a $50 stock's +1% tier
  is $0.50, a $500 stock's +1% is $5.00.

**Peak-based, one-way ratchet**

- `_ladder_stop_long` returns `max(tier_stop, initial_stop)`
  — never looser than the structural floor. On every call
  the ratchet tightens or holds; it never loosens.
- `_ladder_stop_short` mirrors with `min(...)` for shorts
  (tighter = lower stop).
- `manage_positions`, `manage_tp_positions`, and
  `manage_short_positions` (paper + TP) update `trail_high`
  / `trail_low` every tick, compute the ladder stop, and
  ratchet `pos["stop"]` in the tightening direction only.

**`initial_stop` persisted in all four entry paths**

- Long paper, long TP, short paper, short TP position dicts
  now capture the entry-time hard stop as `initial_stop`.
  The ladder uses it as the sub-1% floor and the
  never-looser guard.
- Legacy positions (no `initial_stop` key) fall back to the
  live `pos["stop"]` — no crash, no surprise widening.

**Exit attribution preserved**

- `pos["trail_active"]` is set to `True` once peak ≥ 1%
  (ladder has armed past the structural stop), so the
  `/api/state` surface and exit-reason attribution still
  render **TRAIL** vs **STOP** correctly.
- `trail_stop` is kept as a cosmetic mirror of `pos["stop"]`
  once armed, for back-compat with state consumers.

**Display: /strategy, /algo rewired**

- Both command bodies now print the ladder block in place of
  the old `Trail: +1.0% trigger | max(1.0%, $1.00) distance`
  line. Mobile 34-char budget verified.
- `/help` untouched — it doesn't reference trail mechanics.

**Retightening layers (v3.4.23 0.75% cap, v3.4.25 breakeven
ratchet) kept as idempotent safeguards**

- The ladder dominates both once peak ≥ 1%, and the retight
  layers only tighten (guarded by `new_stop <= current_stop:
  return already_tight`). They stay in as fail-closed
  safeguards for positions that never climb past +1%.

### Why this is safer than the old rule

- Pre-1%: stop is the OR-based structural stop. Exactly the
  same as before.
- +1% to +4%: stop locks progressively more of the gain —
  breakeven, then +1%, +2%, +3.5%. At every band the bot
  gives up less on a reversal than the old 1% trail would.
- +5% and beyond: stop locks 90% of peak gain. A +5% peak
  locks +4.50% (was +4.00% under the old $1 floor). A +10%
  peak locks +9.00% (was +9.00% — here the new rule matches
  the best case of the old rule, but it's reached
  mechanically, not by coincidence of the $1 floor clamp).

### Sanity tests

- AVGO entry $411.30, `initial_stop` $408.22 (0.75% cap):
  - Peak $419.53 (+2.00%) → stop $415.41 (Lock 1%)
  - Peak $429.00 (+4.30%) → stop $425.70 (Tightening)
  - Peak $431.87 (+5.00%) → stop $429.81 (Harvest)
  - Peak $440 (+7%) → stop $437.13 (Harvest scales)
- Short entry $100, `initial_stop` $100.75: all tiers mirror
  correctly with `min` constraint against the ceiling.
- Legacy position without `initial_stop`: falls back to
  `pos["stop"]`, no crash, ladder arms above +1%.

### Verification

- `python3 -m py_compile stock_spike_monitor.py dashboard_server.py smoke_test.py` → OK.
- `python3 smoke_test.py --local` → 213 passed / 0 failed
  (186 baseline + 25 new v3.4.35 tests + retargeted v3.4.34
  history test).
- Telegram mobile 34-char budget re-verified for
  `CURRENT_MAIN_NOTE`, `CURRENT_TP_NOTE`, history tails, and
  both `/strategy` and `/algo` ladder blocks.

### Files touched

- `stock_spike_monitor.py`
  - `BOT_VERSION` → `"3.4.35"`.
  - `CURRENT_MAIN_NOTE` / `CURRENT_TP_NOTE` rewritten; v3.4.34
    AVWAP→PDC context rolled into the history tails.
  - New: `LADDER_TIERS_LONG`, `LADDER_HARVEST_FRACTION`,
    `_ladder_stop_long`, `_ladder_stop_short`.
  - `manage_positions`, `manage_tp_positions`,
    `manage_short_positions` (paper + TP) rewired to the
    ladder; old `max(peak × 1%, $1.00)` trail removed.
  - `initial_stop` captured in all four position-entry dicts.
  - `/strategy` and `/algo` text rewritten with the ladder
    table.
- `smoke_test.py`
  - 25 new v3.4.35 tests (tier math, peak-based, one-way,
    legacy fallback, harvest scaling, mirror, display wiring,
    34-char budget).
  - Retargeted one v3.4.34 test that asserted CURRENT_MAIN
    still led with v3.4.34 — it now checks MAIN_RELEASE_NOTE
    history.
- `CHANGELOG.md` — this entry prepended above v3.4.34.

---

## v3.4.34 — AVWAP → PDC full migration (2026-04-22)

### Why

Eugene pinged the bot at 11:40 CDT with a screenshot: the
regime-change alert was firing "🔴 REGIME: BEARISH / SPY
$709.58 < AVWAP $709.59 / QQQ $652.71 < AVWAP $651.37 / The
Lords have left." on spreads of one penny — noise from a
drifting intraday VWAP anchor, not signal. His verdict:
"This is a distraction. Regardless of the AVWAP for indexes,
this new regime change replaces the old one."

v3.4.28 had already retired AVWAP as the *entry* anchor on
both long and short sides (PDC is the stable daily reference
— yesterday's close doesn't drift with the morning's volume),
but the regime-change alert, the long/short gate blocks, and
every display string still read off the old `avwap_data` dict.
The module carried two state dicts, an updater function
(`update_avwap`), and a dead helper (`_dual_index_eject`) that
no code path called. That's the condition that produced
Eugene's alert: live-updating AVWAP compared against live SPY
tick, published as a regime shift when the two cross by a
single cent.

The choice was narrow-scope (rewrite the alert alone) or full
migration (rip AVWAP out of everything). Val picked full
migration: one anchor, one vocabulary, one source of truth.
This release is that cleanup.

### What changed

**Regime-change alert now reads PDC**

- `scan_loop` regime block (lines ~5015–5051) rewritten to
  compare `last_spy`/`last_qqq` against `pdc.get("SPY")` /
  `pdc.get("QQQ")`. The "Lords have left" / "Lords are back"
  messaging is preserved verbatim — only the anchor changed.
- Alert format is now `"SPY $X.XX < PDC $Y.YY"` (was
  `"< AVWAP"`). Same two-line shape, same emoji, same CDT
  timestamp.
- The alert no longer cares about 5-minute bar finalization
  (`_last_finalized_5min_close`), because PDC is a
  once-per-day constant and the previous-close comparison is
  valid on every tick. That removes an entire class of
  timing races where the alert could fire on a partial bar.

**Long + short entry gates on PDC**

- `check_entry` long gate (lines ~2820–2845) now requires
  `last_spy > spy_pdc and last_qqq > qqq_pdc`. Missing SPY or
  QQQ PDC → return `False` (fail-closed, no entry).
- `check_short_entry` short gate (lines ~3900–3920) now
  requires `last_spy < spy_pdc and last_qqq < qqq_pdc`.
  Missing PDC → return `False`. **This is a behavior
  tightening**: the old AVWAP short gate fail-opened
  (`spy_below` / `qqq_below` defaulted to `True` on missing
  data and let the entry through). PDC is available every
  trading day from the FMP snapshot, so a missing value is
  now treated as a real data problem, not a green light.

Both gates share the canonical pattern:

    spy_pdc = pdc.get("SPY")
    qqq_pdc = pdc.get("QQQ")
    if not spy_pdc or not qqq_pdc or spy_pdc <= 0 or qqq_pdc <= 0:
        return False  # fail-closed

This is consistent with the locked principle: adaptive logic
only makes things more conservative than baseline, never
looser.

**Every user-facing string migrated**

Audited and rewritten in one pass so the vocabulary is
uniform across surfaces:

- Entry reply: `"SPY > PDC ✓"` / `"QQQ > PDC ✓"` (was
  `"> AVWAP"`).
- `/proximity` and `/proximity_sync` — index filter lines.
- `/dashboard` INDEX FILTERS card.
- `/status` helper block.
- `/strategy` body — all four index-check lines and the
  "Lords Left" / "Bull Vacuum" exit-rule descriptions.
- `/strategy_ticker` per-symbol view.
- `/summary` end-of-session recap.
- `/help` and `/algo` bodies — now say "SPY & QQQ > PDC".
- Deploy banner — "PDC anchor" replaces "AVWAP anchor".

**Observer breadth detail moves to PDC**

`_classify_breadth` now emits
`"SPY %+.2f%% above PDC | QQQ %+.2f%% below PDC"` (or the
corresponding combinations) in `sovereign.breadth_detail`.
This changes what `/api/state` surfaces — users reading the
JSON directly will see the new anchor label.

**Dead code removed**

- `update_avwap` function — gone.
- `_dual_index_eject` helper — gone. Nothing called it; the
  ejection path has been PDC-based since v3.4.28.
- `_last_finalized_5min_close` tracker — gone (regime alert
  no longer cares about bar finalization).
- `avwap_data` dict — gone.
- `avwap_last_ts` dict — gone.
- `reset_daily_state` AVWAP reset block — gone.

The removed block is replaced with a one-paragraph comment
citing v3.4.34 (this release) and v3.4.28 (the original
entry-side migration) so the next person reading the file
knows why AVWAP is absent.

**Persistence back-compat (no migration needed)**

- `save_paper_state` no longer writes `avwap_data` or
  `avwap_last_ts` into the state file.
- `load_paper_state` reads with `dict.get(...)` and silently
  ignores those two keys if they exist in a legacy state
  file from a pre-v3.4.34 deploy. No migration script, no
  upgrade path, no user action.

**Legacy back-compat (intentionally kept)**

- `REASON_LABELS["LORDS_LEFT[1m]"]`, `LORDS_LEFT[5m]`,
  `BULL_VACUUM[1m]`, `BULL_VACUUM[5m]` — retained. Old
  trade-log rows written before v3.4.28 still reference
  these codes, and the label dictionary is what renders
  them in `/summary` and `/trade_log`. The v3.4.28
  rationale comment (AVWAP drift caused false ejects) is
  kept alongside.
- Regime-change messaging — "The Lords have left" / "The
  Lords are back" still reads the same. Only the anchor
  changed.

**Smoke test coverage**

16 new `v3.4.34:` tests cover:

- `BOT_VERSION >= 3.4.34`.
- `update_avwap`, `_dual_index_eject`,
  `_last_finalized_5min_close` are absent.
- `avwap_data` and `avwap_last_ts` module state is absent.
- `save_paper_state` doesn't write the legacy keys.
- `load_paper_state` tolerates legacy keys in input.
- `check_entry` gates on `SPY_PDC` and `QQQ_PDC`.
- `check_short_entry` gates on `SPY_PDC` and `QQQ_PDC`.
- `check_short_entry` fails closed on missing PDC.
- Regime alert body uses PDC and emits the Lords messaging.
- `_classify_breadth` observer anchors on PDC.
- `/help` / `/algo` says "SPY & QQQ > PDC".
- `/strategy` body uses PDC in all four index-check lines.
- `reset_daily_state` no longer touches removed AVWAP dicts.
- `CURRENT_MAIN_NOTE` leads with v3.4.34 and mentions PDC.
- v3.4.33 `/ticker` release line persists in history.
- Legacy `LORDS_LEFT[1m]` / `BULL_VACUUM[1m]` labels retained.

Plus two fixes to previously-breaking tests:

- v3.4.33 `/ticker` test now checks `MAIN_RELEASE_NOTE`
  history (not `CURRENT`, since v3.4.33 has rolled off).
- v3.4.16 `_TP_HISTORY_TAIL` test re-asserts `/tp_sync`
  mention after the v3.4.34 note rewrite.

**Result: 186 / 186 passing** (170 baseline + 16 new).

### Files touched

- `stock_spike_monitor.py` — 9324 lines, AVWAP call sites
  rewritten, dead code removed, comment block replaces it.
- `smoke_test.py` — 16 new tests, 2 fixes.
- `CHANGELOG.md` — this entry.

### Upgrade notes

- No state file migration. Drop-in deploy.
- `/api/state` → `sovereign.breadth_detail` now contains
  "PDC" where it used to contain "AVWAP". Any downstream
  consumers of that string need to update their regex.
- Trade-log entries written before v3.4.28 still render
  with the same `LORDS_LEFT` / `BULL_VACUUM` labels.

---

## v3.4.33 — Unified `/ticker` + thorough metric fill (2026-04-22)

### Why

v3.4.32 shipped three separate Telegram commands for managing the
watchlist: `/tickers`, `/add_ticker`, `/remove_ticker`. On a mobile
keyboard that's three autocomplete paths to remember and three
places the menu has to surface — for what is conceptually one
command with three verbs. The right shape is `git`-style
sub-commands: `/ticker list`, `/ticker add SYM`, `/ticker remove SYM`.

Second motivation: "make sure all metrics are populated when a
ticker is added." The v3.4.32 fill only primed PDC and OR; it said
nothing about whether the data provider could actually reach the
symbol, and it skipped RSI entirely (leaving the first scan cycle
to cold-start it from live bars). When adding a freshly-discovered
symbol mid-session, the user deserves to know, in one reply, every
metric the bot is going to rely on — what's ready, what's pending,
and what failed.

### What changed

**Unified `/ticker` command**

- New `cmd_ticker` dispatcher accepts sub-commands (case-
  insensitive, each with short aliases):
  - `list` / `ls` / `show` — render the current watchlist.
  - `add` / `+` — add a symbol and prime every metric.
  - `remove` / `rm` / `del` / `-` — stop new entries on a symbol.
- Bare `/ticker` (no args) defaults to `list` — the most common case.
- Unknown sub-commands show the usage block instead of failing silently.
- `BotCommand` menu advertises a single line:
  `/ticker  Ticker: list | add SYM | remove SYM`.

**Back-compat aliases (hidden but wired)**

- `/tickers`, `/add_ticker`, `/remove_ticker` remain registered as
  `CommandHandler`s on both the main bot and the TP bot. They are
  intentionally omitted from `MAIN_BOT_COMMANDS` so the menu stays
  tight, but any saved Telegram shortcuts or muscle-memory typed
  commands keep working with identical replies.

**Thorough metric fill on add**

`_fill_metrics_for_ticker` now primes every tracked metric, with
explicit source tracking and per-field status:

- **Bars liveness probe** — a single `fetch_1min_bars` call now
  doubles as a "can the data provider reach this symbol?" check.
  Reply shows `✅ reachable` or `⚠ unreachable`.
- **PDC dual-source** — tries FMP first (works any time of day,
  including pre-open), then falls back to the bars snapshot if
  FMP returned nothing. Reply tags the source: `PDC: $X.XX (fmp)`
  or `PDC: $X.XX (bars)`.
- **OR high + low** — populated from the 09:30–09:35 ET window if
  the current time is past 09:35. Pre-09:35 is now an explicit
  `or_pending` status (not an error), and the reply says
  `OR: pending 09:35 ET` so the user knows `collect_or()` will
  handle it at the scheduled cutover.
- **RSI warm-up** — when bars return at least `RSI_PERIOD + 1`
  closes, the fill computes a warm-up RSI value. This doesn't
  cache (the scanner recomputes each cycle from live bars), but
  it proves the history is deep enough and surfaces the current
  reading for the user: `RSI: 54.7 (warm)`.

All sourcing is still fail-soft — any provider error adds to an
`errors` list but still returns a valid dict; the ticker is added
regardless so the scanner can retry. This is consistent with the
locked principle that missing data never ejects a position and
should never block a legitimate add either.

**Reply layout**

`/ticker add` now returns a 5-line status block under the 34-char
mobile budget:

```
Bars:  ✅ reachable
PDC:   $10.50 (fmp)
OR:    $10.40 – $10.80
RSI:   61.4 (warm)
```

Each row has an explicit pending / missing state (e.g. `OR: pending
09:35 ET`, `RSI: — (warms on scan)`) so the user knows whether to
wait or retry.

### Tests

Added **10 new smoke tests** (160 → **170 passed · 0 failed**):

- `BOT_VERSION >= 3.4.33`.
- `cmd_ticker` exists and is a coroutine.
- `BotCommand` menu advertises `/ticker` (and specifically does
  **not** advertise the old per-verb entries).
- `/ticker` usage text mentions list / add / remove and stays
  within 34 chars.
- `_fill_metrics_for_ticker` returns the full metric dict shape
  (`bars`, `pdc`, `pdc_src`, `or`, `or_pending`, `rsi`, `rsi_val`,
  `errors`) with FMP + bars stubs.
- PDC falls back to the bars snapshot when FMP returns nothing,
  and the source tag reports `bars`.
- Unreachable bars + FMP failure → `bars=False`, `pdc=False`,
  `rsi=False`, with errors populated — confirming the fail-soft
  path is wired.
- Add-reply formatter emits Bars / PDC / OR / RSI rows and every
  line stays within the mobile budget.
- Release notes + `/help` corpus still reference every entry point
  (`/ticker`, `/tickers`, `/add_ticker`, `/remove_ticker`).

The one v3.4.32 test that asserted `BotCommand` menu entries for
`/tickers`, `/add_ticker`, `/remove_ticker` was rewritten to verify
the alias handlers exist instead — these commands live on as hidden
aliases, just not in the menu.

### Files touched

- `stock_spike_monitor.py` — `cmd_ticker` dispatcher, expanded
  `_fill_metrics_for_ticker`, richer `_fmt_add_reply`, alias
  handler registrations, updated `/help` body, updated release notes.
- `smoke_test.py` — one v3.4.32 test rewritten, 10 new v3.4.33 tests.
- `CHANGELOG.md` — v3.4.33 entry.

---

## v3.4.32 — Editable ticker universe from Telegram + QBTS (2026-04-22)

### Why

The watchlist was baked into the module as a constant: editing it
meant a code push, a PR, and a Railway redeploy just to try a new
symbol. That's a real friction tax on the core job of the bot —
hunting for overnight gappers and intraday breakouts — because
the universe of interesting tickers moves around week to week.

Specifically, QBTS (quantum-computing name) had become a persistent
side-request, and adding it by hand every time was a poor workflow.
Beyond that one symbol, the bot needed a first-class way to treat
the watchlist as user state, not source code: add a name when a
thesis appears, drop it when the thesis dies, and have those edits
survive restarts.

### What changed

**QBTS is now a first-class default**

- Added `QBTS` to `TICKERS_DEFAULT` alongside the core megacaps
  and the SPY/QQQ regime anchors. Fresh installs and cold boots
  with no `tickers.json` will now pick it up automatically.

**Persistent, runtime-editable watchlist**

- The watchlist now lives in `tickers.json` at the repo root
  (path overridable via `$TICKERS_FILE`). The bot loads it at
  startup, falls back to `TICKERS_DEFAULT` if the file is
  missing or malformed, and rewrites it atomically on every
  change (`tmp + os.replace`).
- `SPY` and `QQQ` are pinned as regime anchors: they're always
  present in the tracked list and explicitly excluded from
  `TRADE_TICKERS` (the list the entry scanner iterates). They
  cannot be removed via Telegram.
- `TICKERS` and `TRADE_TICKERS` stay as the same mutable
  module-level lists the rest of the codebase already reads;
  a new `_rebuild_trade_tickers()` mutates them in place so
  none of the ~25 existing `for t in TICKERS:` call sites need
  to change.
- `TICKERS_MAX = 40` caps the universe so a runaway add can't
  blow the per-cycle scan budget.

**Three new Telegram commands**

- `/tickers` — shows the current tracked list, pinned anchors
  first, then the trade universe, in a 34-char-safe code block.
- `/add_ticker SYM` — validates the symbol against
  `^[A-Z][A-Z0-9.\-]{0,7}$` (after uppercasing and stripping a
  leading `$`), adds it to `TICKERS`, persists, and immediately
  fills its metrics:
  - **PDC** via `get_fmp_quote` (blocking call runs in an
    executor so Telegram doesn't hang).
  - **OR** (opening range) via `fetch_1min_bars` if the current
    ET time is past 09:35. Before 09:35 the scheduled
    `collect_or()` will fill it at the normal cutover.
  - RSI is on-demand in the entry scanner, so no seeding is
    needed there.
  The reply confirms what was filled and notes anything
  pending, in 34-char-safe mobile lines.
- `/remove_ticker SYM` — blocks new entries on a symbol. A
  currently open position on that ticker keeps managing until
  it closes normally; the cached PDC / OR entries stay in place
  so exit logic still has what it needs. Attempting to remove
  `SPY` or `QQQ` returns a clear "pinned" reply and is a no-op.

**Fail-soft design, consistent with existing locked principles**

- Missing / malformed `tickers.json` → fall back to defaults,
  don't crash. A missing metric fill (network error on PDC or
  pre-open OR) → add the ticker anyway and surface the issue
  in the reply — in the same spirit as the existing
  "missing data never ejects a position" rule.
- The editable universe only shrinks what's tradeable by
  adding the ability to drop a name; it never loosens a
  filter, consistent with "adaptive logic only makes things
  MORE conservative than baseline, never looser."

### Tests

Added **12 new smoke tests** (148 → 160):

- `BOT_VERSION >= 3.4.32`
- `QBTS in TICKERS_DEFAULT` and `TICKERS`
- `SPY`/`QQQ` pinned and excluded from `TRADE_TICKERS`
- `_normalise_ticker` handles lowercase, `$`-prefix, whitespace
  and rejects bad chars / overlong symbols
- `add_ticker` add / repeat / remove semantics (with
  `_fill_metrics_for_ticker` and `_save_tickers_file` stubbed)
- `add_ticker` rejects invalid symbols
- `remove_ticker` refuses `SPY` and `QQQ`
- `tickers.json` save / load round-trip preserves order (using
  a tmp path so the live file is never touched)
- `cmd_tickers` / `cmd_add_ticker` / `cmd_remove_ticker` exist
  and are coroutine functions
- `MAIN_BOT_COMMANDS` advertises the three new commands
- All reply formatters stay within the 34-char mobile budget
- Release notes + `/help` corpus advertise the new commands

`python smoke_test.py --local` → **160 passed · 0 failed**.

### Files touched

- `stock_spike_monitor.py` — ticker persistence + helpers +
  commands + handler registration + help wiring + release notes
- `smoke_test.py` — 12 new v3.4.32 tests appended to `run_local`
- `tickers.json` — new persisted state file (seeded with the
  default list including QBTS)

---

## v3.4.31 — Richer Today's Trades card (2026-04-22)

### Why

After v3.4.30 fixed the mobile layout regression, the Today's
Trades card was finally visible on phones — but it still carried
its original "thin log" design: one line per fill with just
`time / sym / action / qty / price`. For a trader using the
dashboard as the primary at-a-glance P&L view, that shape
breaks down on two fronts:

1. **No running scorecard.** The card showed fills but not the
   resulting day. You couldn't tell at a glance how many trades
   had opened, how many had closed, whether the day was net
   green or red, or what the win rate was. Those numbers lived
   only in the KPI strip (Day P&L) and required counting rows
   to sanity-check.
2. **Per-row data was too thin on closes.** SELL rows carried
   the exit price but nothing about the trade outcome — no P&L
   $, no P&L %, no colour cue. BUY rows didn't show cost, so
   there was no fast way to see "how much did this position
   tie up?" without multiplying in your head.

### What changed

**Summary header**

- Added a chip in the card header showing running realized $
  for the day (green / red / neutral), visible at a glance next
  to the trade count.
- Added a summary line above the rows:
  `N opens · M closes · realized $X · win Y%`. Win rate is
  wins / closes with a reported P&L — missing values skip the
  denominator rather than inflating it.
- Both are driven by a single `computeTradesSummary(trades)`
  helper so the chip and the line can't drift.

**Per-row fields**

- BUY rows now show the **cost** (shares × price, or the
  server-provided `cost` field) in the trailing cell,
  monospace and subdued.
- SELL rows now show the **realized P&L** in the trailing
  cell: dollar amount in green / red, with the P&L % dimmed
  alongside. The LONG / SHORT colour on the symbol is kept as
  a side cue; the action badge (BUY green / SELL red) carries
  the direction.

**Layout — grid rows instead of a `<table>`**

- Rewrote the render to emit a `<div class="trades-list">` of
  `<div class="trade-row">` elements driven by CSS Grid with
  named areas. Desktop uses a single row:
  `"time sym act qty price tail"` on a 6-track layout.
- Mobile (`@media (max-width: 640px)`) overrides just the
  `grid-template-areas` and column tracks so the same DOM
  collapses into three stacked lines per trade:
  `"time sym act" / ". qty tail" / ". . price"`. No DOM
  duplication, no horizontal scroll, no JS breakpoint.
- Extracted the HH:MM formatter from `renderTrades` into a
  `fmtTradeTime(rawT)` helper. Same v3.4.30 regex —
  `/^\d{4}-\d{2}-\d{2}T/` for ISO, `/^\d{1,2}:\d{2}/` for
  pre-formatted `"09:11 CDT"` strings.

### Tests

Added 8 new smoke tests (148/148 pass):

- `v3.4.31: BOT_VERSION is >= 3.4.31` (relaxes the v3.4.30
  exact-version check to a lower bound).
- `v3.4.31: dashboard carries trades summary header + realized chip`
- `v3.4.31: dashboard uses .trade-row grid rows instead of a <table>`
- `v3.4.31: desktop .trade-row grid-template-areas = 'time sym act qty price tail'`
- `v3.4.31: mobile (≤640px) collapses .trade-row into stacked rows`
- `v3.4.31: renderTrades emits .trade-row markup — not a <table>`
- `v3.4.31: computeTradesSummary counts opens/closes + sums realized`
- `v3.4.31: renderTrades populates summary line + realized chip`

The v3.4.30 `renderTrades accepts pre-formatted 'HH:MM TZ'`
test was rewritten to target the new `fmtTradeTime` helper —
same invariants (no `.includes("T")`, full ISO prefix regex,
HH:MM extraction), new function name.

### Files

- `stock_spike_monitor.py` — `BOT_VERSION = "3.4.31"`,
  `CURRENT_MAIN_NOTE` / `CURRENT_TP_NOTE` rewritten at ≤34
  chars/line, v3.4.30 entry rolled into history tails.
- `dashboard_static/index.html` — new summary chip + line
  markup, new CSS for `.trades-summary`, `.act-badge`,
  `.trade-pnl`, `.trade-cost`, `.trades-list`, `.trade-row`,
  plus the 640px media-query overrides. `renderTrades`
  rewritten to emit grid rows and populate the summary;
  `computeTradesSummary` + `fmtTradeTime` helpers extracted.
- `smoke_test.py` — +8 tests, 1 rewritten.
- `CHANGELOG.md` — this entry.

---

## v3.4.30 — Mobile layout fix + Today's Trades time display (2026-04-22)

### Why

Two regressions surfaced after v3.4.29 shipped:

1. **Dashboard overflowed the iPhone viewport.** The v3.4.29
   Sovereign Regime card introduced long content lines (e.g.
   `SPY and QQQ both 1m close > PDC — shorts would eject`) as
   well as multi-track grids for the SPY/QQQ rows. Combined
   with the existing Gates card's nowrap labels, this pushed
   the *intrinsic min-content width* of `.main` beyond the
   viewport. Because `.app` is a CSS grid with a single `1fr`
   column and its `.main` child lacked `min-width: 0`, the
   grid track inflated to fit the widest descendant instead
   of being constrained to the viewport. Every card rendered
   ~1980px wide on a 390px phone; bars and tables spilled off
   the right edge.

2. **Today's Trades showed blank time cells.** The renderer
   expected an ISO-8601 string like `2026-04-22T09:11:00...`
   and sliced characters 11–15 (`HH:MM`). The server actually
   produces a pre-formatted `"09:11 CDT"` string. Slicing a
   9-char string at offset 11 returns `""`, which is why
   every row showed a dash.

### What changed

**Mobile layout fix**

- Added `min-width: 0` to `.main` — the universal CSS
  escape-hatch that lets a flex/grid child shrink below its
  intrinsic content width.
- Added `min-width: 0` to `.main > section`, `.grid`, and
  `.grid > *` so every nested track gets the same treatment.
- Changed `.srs-idx` (SPY/QQQ rows) from `1fr` to
  `minmax(0, 1fr)` so the flexible track can actually shrink.
- Added `word-break: break-word` and `overflow-wrap: anywhere`
  to `.srs-reason` so the long human-readable verdict line
  wraps instead of pushing the card width.

**Today's Trades time parsing**

- `renderTrades()` now branches on the *shape* of the time
  string rather than the presence of the letter `T`. Previous
  attempt used `.includes("T")`, which mis-routed `"09:11 CDT"`
  (tz label contains T) into the ISO-slice branch. The new
  code matches the full ISO prefix `YYYY-MM-DDT` for ISO
  strings and extracts the leading `HH:MM` via regex for
  pre-formatted strings.

### Safety

- No trading-logic changes. Dashboard-only release.
- 140/140 smoke tests pass (133 prior + 7 new covering the
  CSS min-width invariants, the `minmax(0, 1fr)` track, the
  `.srs-reason` wrap rule, the ISO-prefix time regex, and the
  regression guard against the broken `.includes("T")` branch).
- Visual regression check: at 390×844 viewport the body
  width equals the viewport width (390px) and
  `document.querySelectorAll('*')` returns zero elements
  extending past the right edge.

---

## v3.4.29 — Persistent dashboard session + Sovereign Regime card (2026-04-22)

### Why

Two small-but-annoying frictions on the dashboard:

1. **Every Railway redeploy logged Val out.** The cookie-auth
   secret was a random 32 bytes generated in memory at
   `start_in_thread()`, so each container restart invalidated
   every session. A 7-day cookie only lasts as long as the
   container. Val ships patches multiple times a day; this meant
   re-entering the dashboard password several times a day.

2. **The Sovereign Regime Shield (v3.4.28) was invisible.** The
   bot's most-important global gate — the dual-index PDC eject —
   had no surface on the dashboard. You could only infer its
   state by reading the log tail. Val asked for a first-class
   panel.

### What changed

**Persistent session secret**

- New helper `_load_or_create_session_secret()` in
  `dashboard_server.py` resolves the HMAC key in three tiers:
  1. Env `DASHBOARD_SESSION_SECRET` (hex) — operator override.
  2. On-disk file `dashboard_secret.key` in the same directory
     as `PAPER_STATE_FILE` (inherits Railway volume mount).
     Must be ≥ 32 bytes or it is rejected and regenerated.
  3. Generate 32 random bytes and persist via atomic
     tmp+`os.replace`, chmod 0600 (best-effort).
- Fail-safe: if the disk write fails, the key lives in memory
  for this process — no crash, no downtime. The next deploy
  simply regenerates (same behaviour as pre-v3.4.29).
- 7-day cookies now survive container restarts. Val logs in
  once per device per week.

**Sovereign Regime Shield card**

- New helper `_sovereign_regime_snapshot(m)` in
  `dashboard_server.py` reads the Shield's ground-truth
  primitives (`m._sovereign_regime_eject` and
  `m._last_finalized_1min_close`) and returns a stable
  12-field dict: per-index price, PDC, delta%, above-PDC flag,
  plus long_eject / short_eject booleans, a compact status tag
  (`ARMED_LONG` | `ARMED_SHORT` | `DISARMED` | `AWAITING` |
  `NO_PDC`), and a human reason string.
- Wired into `snapshot()` as `regime.sovereign` so the front
  end can render it without recomputing anything.
- New dashboard card "Sovereign Regime Shield" renders SPY and
  QQQ rows (price, PDC, signed delta%) plus two verdict tiles
  (LONGS · SHORTS) that turn red when the Shield is armed
  against that side. The status chip at the top matches the
  bot's internal state. Fails closed: when either PDC is
  missing the card shows `NO PDC` and both eject tiles go
  neutral — matching the core gate's fail-closed semantics.

### Safety

- No trading-logic changes in this release. The Shield itself
  is untouched; the card is a pure read-out of existing
  state.
- The session-secret change is additive: it cannot reduce
  security (still HMAC-signed, still HttpOnly + Secure cookies)
  and cannot loosen the existing 7-day expiry.
- 133/133 smoke tests pass (122 prior + 11 new covering secret
  persistence, env override, corrupt-file rejection, regime
  snapshot shape, NO_PDC fail-closed, ARMED_LONG path, and
  HTML card presence).

### Locked design principles (unchanged)

- Adaptive logic only makes things MORE conservative than
  baseline, never looser.
- Fail-closed: missing data → do NOT eject.

---

## v3.4.28 — Sovereign Regime Shield (2026-04-22)

### Why

For eleven minor versions the global eject gate has been the
"Dual-Index Confluence Shield": exit every long when both SPY
and QQQ close a **5-minute** bar below their **AVWAP**, mirror
for shorts. The logic shipped in v3.2.0 and earned its keep on
macro-driven flush days.

But AVWAP is a volume-weighted *drift* line. On slow, choppy
tape — the kind of day where SPY closes flat ±0.20% — the AVWAP
shuffles within a narrow band, and the 5-minute close can bob
above and below it a dozen times before lunch. Each bob that
happens to catch both indices triggers a LORDS_LEFT or
BULL_VACUUM eject, stomping the trade book regardless of whether
the tape has actually regime-shifted. Val calls the resulting
churn "regime flim-flam."

**PDC — Prior Day Close — is a better anchor.** It is one static
number per symbol per day. A cross of PDC is a structural event:
the overnight-holder cost basis has been reclaimed (or
surrendered). It does not drift, it does not repaint, and it
cannot shuffle back and forth intraday on volume-weighting noise.

v3.4.28 replaces the AVWAP-based dual-index eject with a
PDC-based one. The global shield now fires only on a true
structural break of both major indices — exactly the regime it
was meant to guard against.

### What

**New helpers** (`stock_spike_monitor.py`):

- `_last_finalized_1min_close(ticker)` — returns `closes[-2]` so
  the eject reads a *sealed* bar, never the still-ticking
  in-progress minute. Returns `None` on `<2` finalized bars.
- `_sovereign_regime_eject(side)` — the new gate. Returns `True`
  iff **both** SPY and QQQ 1-minute finalized closes are on the
  losing side of their respective PDC:
  - `side="long"`  → both closes **below** PDC
  - `side="short"` → both closes **above** PDC

**Hysteresis by construction.** The AND logic *is* the
divergence buffer: if SPY breaks below PDC but QQQ stays above,
the gate returns `False`. The regime is UNCHANGED. No eject.
End users running longs on a mixed tape will no longer be
flushed by one index's isolated flush.

**Fail-closed, always.** Any missing input — PDC not yet
collected (pre-open cycle), 1-minute bars unavailable, fewer
than two finalized bars, invalid `side` argument — returns
`False`. Matches the locked design principle: adaptive logic
only makes things more conservative than baseline, and missing
data means stay in the trade.

**1-minute finalized close, not 5-minute.** Per Val's spec:
sub-5-minute resolution catches the structural break the moment
the bar seals, without subjecting the decision to intrabar wick
noise (which `closes[-2]` eliminates).

**Three call sites swapped** from `_dual_index_eject` to
`_sovereign_regime_eject`:

- `manage_positions()` (paper long loop)
- `manage_tp_positions()` (TP long mirror loop)
- `manage_short_positions()` (both short sub-loops share one
  `bull_vacuum` local, so one call covers both)

Exit-reason strings are now plain `LORDS_LEFT` / `BULL_VACUUM`.
The legacy `LORDS_LEFT[5m]` / `BULL_VACUUM[5m]` entries remain
in `REASON_LABELS` so old rows in `trade_log.jsonl` render
cleanly in the dashboard.

**`_dual_index_eject` kept intact** in the source as a
reference/fallback. It is no longer called in live code.

### Coverage

15 new smoke tests (`smoke_test.py`), all green:

- `_last_finalized_1min_close` returns `closes[-2]` (not
  intrabar) and `None` when `<2` finalized bars exist
- `_sovereign_regime_eject("long")` fires when both below PDC,
  does not fire when both above PDC (inverse)
- `_sovereign_regime_eject("short")` fires when both above PDC
- Divergence (SPY below, QQQ above) does NOT eject either side
- Missing `SPY_PDC` or `QQQ_PDC` returns `False` (fail-closed)
- Insufficient 1-minute bars returns `False` (fail-closed)
- Invalid `side` (`"bogus"`, `""`, `None`) returns `False`
- `manage_positions` / `manage_short_positions` /
  `manage_tp_positions` all invoke the new gate and no longer
  reference `_dual_index_eject` in live paths
- Plain `LORDS_LEFT` / `BULL_VACUUM` are registered in
  `REASON_LABELS` and their labels mention PDC; legacy
  `[5m]`-suffixed labels preserved for old trade rows

Total local suite: **122 passed / 0 failed.**

### Files touched

- `stock_spike_monitor.py` — `BOT_VERSION=3.4.28`,
  `CURRENT_MAIN_NOTE` + `CURRENT_TP_NOTE` (34-char-safe),
  `_MAIN_HISTORY_TAIL` + `_TP_HISTORY_TAIL` rolled,
  `REASON_LABELS` extended (plain + legacy coexist),
  `_last_finalized_1min_close` + `_sovereign_regime_eject`
  added, 3 call sites + 4 exit-reason strings updated
- `smoke_test.py` — 15 new tests under the v3.4.28 section

### Design principles reaffirmed

- **Adaptive logic only makes things MORE conservative than
  baseline, never looser.** The new shield is stricter: it
  requires a structural PDC break, not just AVWAP drift.
- **Fail-closed:** any missing data → no eject → stay in trade.
- **Divergence → no action:** hysteresis baked into the AND.

---

## v3.4.27 — Persistent trade log (append-only JSONL) (2026-04-21)

### Why

Today's 9-trade paper session showed a sharp expectancy split by
exit reason: **TRAIL +$6.10 (1W/0L)** was the only positive bucket,
while **BULL_VACUUM −$21.70 (1W/2L)** and **EOD −$9.00 (1W/3L)**
bled the book. The observation is obvious in one session's tape
— but to trust it as a policy input (tightening BULL_VACUUM, or
gating POWER-hour re-entries) we need dozens of sessions of
matched data. Until now the bot's in-memory trade history died
with every deploy, so that sample never accumulated.

v3.4.27 fixes that by writing every closed trade to a persistent
log on the Railway volume — the same volume that already survives
redeploys for `paper_state.json` and `tp_state.json`.

### What

**Append-only JSONL writer** (`trade_log_append`). Every close
path — paper long, TP long mirror, TP-only long, and the shared
short path — appends one JSON line to `trade_log.jsonl`
(overridable via `TRADE_LOG_PATH`). Thread-locked, best-effort
(any IO error is logged and swallowed — a broken disk never
breaks trade execution).

**Schema v1.** Each row captures everything needed for expectancy
analysis:

- `schema_version`, `bot_version`, `date`, `portfolio` (paper/tp)
- `ticker`, `side`, `shares`, `entry_price`, `exit_price`
- `entry_time`, `exit_time`, `hold_seconds`
- `pnl`, `pnl_pct`, `reason`, `entry_num`
- `trail_active_at_exit`, `trail_stop_at_exit`,
  `trail_anchor_at_exit` (trail_high for longs, trail_low for
  shorts), `hard_stop_at_exit`, `effective_stop_at_exit`

The trail/stop snapshot matters because `reason` alone doesn't
tell you whether the exit was the hard stop or the trail stop
taking the trade. `effective_stop_at_exit` resolves the hierarchy
at close time so downstream analysis sees what the exit decision
actually saw.

**Reader + endpoints.**

- `trade_log_read_tail(limit, since_date, portfolio)` — newest-
  last, safe on missing file, skips corrupted lines rather than
  raising.
- `GET /api/trade_log?limit=500&since=YYYY-MM-DD&portfolio=paper|tp`
  on the authenticated dashboard server. Returns
  `{ok, count, schema_version, rows, last_error}`.
- `/trade_log` Telegram command — last 10 trades with W/L summary
  and by-reason P&L buckets. Width-safe for mobile (≤34 cpl).
  Registered on both main and TP bots.

### Schema stability

`TRADE_LOG_SCHEMA_VERSION = 1` is written to every row. Future
breaking changes will bump this number so old-and-new rows can
coexist in the same file without a migration.

### Tests

**108/108 local smoke** (up from 97). Ten new v3.4.27 tests
cover: path sits beside `PAPER_STATE_FILE` (= same Railway
volume), writer roundtrip with `schema_version=1`, required-
field guard, `since_date` + `portfolio` + `limit` filter matrix,
missing-file returns `[]`, trail+stop snapshot for long/short/
empty positions, every close path calls both
`trade_log_append` and `_trade_log_snapshot_pos`, `/api/trade_log`
+ `/trade_log` registration on both app routers.

### Next (deliberately not in this release)

This release is infrastructure only. The analysis piece —
reason-bucket expectancy report, POWER-hour entry cutoff — lands
in a follow-up once we have ≥5 sessions of live data.

---

## v3.4.26 — Ratchet-through-trail + dashboard trail diagnostics (2026-04-21)

### The silent bypass v3.4.25 left behind

v3.4.25 deployed clean — GOOG ratcheted to entry immediately on
deploy. AAPL, at the same time and past the +0.50% arm, did not.
Reason: AAPL's short trail had armed earlier in the session on a
dip near `entry × 0.990 = $266.08`. Once `trail_active=True`,
v3.4.25's `_retighten_short_stop` short-circuited the entire
retighten pass (cap AND breakeven ratchet) with a single guard:

```python
if pos.get("trail_active"):
    return ("no_op", None, None)
```

The rationale was "trail is always tighter than the 0.75% cap by
construction." True for the cap — but not for the breakeven ratchet.
A short trail that arms on an unfavorable dip can leave `trail_stop`
wider than entry (e.g. `trail_low $266.08 + $2.66 = $268.74` vs
entry $268.77). The +0.50% breakeven ratchet could tighten that
further, but v3.4.25 refused to try.

Compounding the problem: the dashboard showed `pos["stop"]` ($270.79)
even when trail was actually managing the position. No surface
indication of which logic was in effect.

### The fix

**Ratchet runs through trail.** When `trail_active=True`, the cap
layer stays skipped (trail was designed to replace it), but the
breakeven ratchet now runs against `pos["trail_stop"]` instead of
`pos["stop"]` — because once trail is armed, `manage_positions` uses
`trail_stop` for exit decisions, not `pos["stop"]`.

- Longs: `new_trail_stop = max(current_trail_stop, entry)`
- Shorts: `new_trail_stop = min(current_trail_stop, entry)`

Pure tighten, never loosens. Same locked design principle as every
prior stop-management change.

New status tuple `("ratcheted_trail", old_trail, new_trail)` is
returned when this path fires. `retighten_all_stops` gains a
`ratcheted_trail` counter. `/retighten` output shows `trail→entry`
for these rows.

**Dashboard trail diagnostic.** `/api/state` positions now expose:

- `trail_active: bool`
- `trail_stop: float | null`
- `trail_anchor: float | null` (trail_high for longs, trail_low for
  shorts)
- `effective_stop: float` — `trail_stop if trail_active else stop`,
  matching `manage_positions`' exit-decision rule

The UI now renders `effective_stop` in the Stop column with a small
`TRAIL` badge when trail is armed. At a glance, you can see what's
actually managing the position. The raw `stop` field is kept for
backward compatibility — older payload consumers still work.

### Expected post-deploy behavior

On startup, the retroactive retighten pass fires before any new
scan. For AAPL (entry $268.77, current mark $266.38, trail_active
assumed True from an earlier dip):

- If trail_stop > $268.77: ratchet pulls it down to $268.77.
- Effective dashboard stop should read $268.77 with a TRAIL badge.
- Hard `pos["stop"]` ($270.79) is untouched — it's a stale number
  that matters only if `trail_active` is ever cleared (which it
  isn't, per current design).

### Test coverage

+10 smoke tests covering: above-arm no-op, ratchet-through-trail
for both sides, pure-tighten invariant, defensive fall-through on
missing trail_stop, summary counter, dashboard state exposure, and
index.html rendering. Totals: **97 local, unchanged prod surface**.
The one v3.4.23 test that asserted `("no_op", None, None)` was
retired in favor of asserting the underlying invariant ("hard stop
untouched when trail is active"), which still holds.

---

## v3.4.25 — Breakeven ratchet at +0.50% profit (2026-04-21)

### The gap this closes

v3.4.21 introduced a 0.75% entry-cap on stops. v3.4.23 retro-applied
it to any existing position. But once a position moved *in our
favor*, the stop stayed anchored at `entry ± 0.75%` until the 1%
trail-arm threshold — and in that window the stop is frequently
wider than the current profit. Live example this afternoon:

```
AAPL SHORT: entry $268.77, current $266.59 (+0.82% profit),
            stop $270.79 — still 1.58% above market
```

If AAPL popped back to $270.79, we'd not only give back all $22 of
current profit, we'd take another $20 loss — a ~193% give-back of
the running gain. The same pattern showed on NVDA and GOOG to
varying degrees.

### The fix

**Two-stage stop management (Stage 1):**

- Stage 0 (v3.4.21/v3.4.23, unchanged): fixed stop at `entry ± 0.75%`.
- **Stage 1 (NEW): breakeven ratchet.** When current price moves
  ≥0.50% in our favor, pull the stop to entry price (breakeven).
- Stage 2 (existing trail logic, unchanged): at +1.00% profit, the
  trailing stop arms and takes over.

### Implementation

- New constant: `BREAKEVEN_RATCHET_PCT = 0.0050`.
- New pure helpers `_breakeven_long_stop` and `_breakeven_short_stop`
  that return `(new_stop, armed)`. `armed` is True once the
  threshold is met; `new_stop` is `max(current_stop, entry)` for
  longs and `min(current_stop, entry)` for shorts — guaranteed to
  only ever tighten.
- Integrated into the existing `_retighten_long_stop` and
  `_retighten_short_stop` helpers as Layer 2 (Layer 1 is the 0.75%
  cap). These are the single choke-point that startup, manage
  cycles, and `/retighten` all call — so the ratchet applies in
  every place the cap does, automatically.
- New status tuple `("ratcheted", old_stop, new_stop)` returned when
  the breakeven layer is what caused the tightening (distinct from
  `("tightened", ...)` which is pure cap). Summary dict gains a
  `ratcheted` counter. `/retighten` output distinguishes cap vs
  ratchet per position.

### Retroactive behavior (same philosophy as v3.4.23)

Fires on startup and every manage cycle. On the first deploy, the
live positions that are already past the threshold get ratcheted
immediately. Expected for AAPL on this deploy: stop moves from
$270.79 → $268.77.

### Locked design principles preserved

- **Only tightens, never loosens.** If the stop is already past
  breakeven (closer to market than entry), the ratchet is a no-op.
- **Fail-closed.** Missing data → `summary["errors"] += 1` and the
  existing stop is preserved — position is never ejected on a
  missing-data edge case.
- **Trail interaction.** When `pos["trail_active"]` is True, the
  entire retighten pass short-circuits to `no_op`. Trail logic is
  already at least as tight as breakeven by construction.

### Tests

11 new v3.4.25 regression tests (87/87 local pass, up from 76):

- Constant sanity: `BREAKEVEN_RATCHET_PCT == 0.005`
- Below-threshold no-op for both sides
- Exactly-at-threshold arming (boundary behavior)
- Past-threshold ratchet (AAPL live scenario reproduced)
- Never-loosen guarantee: existing tighter stop is preserved
- `"ratcheted"` status returned from `_retighten_*_stop`
- `trail_active` no-op precedence over ratchet
- `retighten_all_stops` summary dict gains `ratcheted` key

Two existing v3.4.23 tests had their `current_price` adjusted to stay
below the new +0.50% threshold so they continue to isolate pure-cap
behavior.

---

## v3.4.24 — Dashboard portfolio strip polish (2026-04-21)

Two fixes on the mobile dashboard's portfolio strip, prompted by a
live observation this morning: with two shorts open the strip read
**Cash $108,545** next to **Short Liab $8,552**, which looked wrong
at a glance. The numbers were correct — short-sale proceeds land in
cash, and the offsetting liability tracks what you owe to buy back
the shares — but putting Cash in the headline position made it seem
like liabilities were being counted as cash.

### Changes

- **Hero row now shows Equity + Buying Power.** These are the net
  numbers that actually matter. Equity was already in the strip as
  a small footer; it's now a top-row KPI. Buying Power is new and
  computed client-side as `cash − short_liab` — the unencumbered
  portion of cash.
- **Cash / Long MV / Short Liab demoted to a components row.** Same
  three columns as before, but smaller text and muted labels so
  it's clear they're inputs to the hero numbers rather than the
  headline itself.
- **Equation-line overflow fixed.** The old "Equation: cash + long
  MV − short liab = $X" footer wrapped awkwardly on narrow (≤412px)
  screens, with the `= $X` landing on a third line. Row 2 is now a
  plain grid and no equation text is needed — the math is visible
  from the labels alone.

### No strategy / accounting changes

The underlying portfolio math is unchanged. `portfolio.cash`,
`portfolio.short_liab`, `portfolio.long_mv`, and `portfolio.equity`
in `/api/state` all have exactly the same semantics as v3.4.23.
This is a pure display change in `dashboard_static/index.html`.

### Tests

BOT_VERSION guard relaxed to `>= 3.4.23` floor (tuple compare) so
future minor bumps don't regress it. 76/76 local pass.

---

## v3.4.23 — Retro-tighten existing stops (2026-04-21)

v3.4.21 introduced the 0.75% entry-cap (`MAX_STOP_PCT = 0.0075`) but
it only fired **at entry**. Positions opened before v3.4.21 shipped
still carried wider baseline stops — and we had two such positions
live when the live symptoms appeared this morning:

| Ticker | Side  | Entry    | Stop     | Risk  |
|--------|-------|----------|----------|-------|
| AAPL   | SHORT | $268.77  | $273.95  | 1.93% |
| TSLA   | SHORT | $388.00  | $393.40  | 1.39% |

Both were entered at ~09:59–10:06 CDT, before the v3.4.21 merge at
10:14 CDT. v3.4.21's cap never got a chance to touch them.

### Design

The cap is a hard risk ceiling, not a hint. v3.4.23 walks every open
position — paper and TP, longs and shorts — and applies the same
0.75% cap retroactively. Three new helpers:

- `_retighten_long_stop(ticker, pos, current_price, portfolio, force_exit=True)`
- `_retighten_short_stop(ticker, pos, current_price, portfolio, force_exit=True)`
- `retighten_all_stops(force_exit=True, fetch_prices=True)` — returns
  a summary dict `{tightened, exited, no_op, already_tight, errors,
  details}`.

Each per-position helper returns one of:

- `("no_op", None, None)` — trail already armed (by construction,
  trail is tighter than the 0.75% fixed cap, so we leave it alone).
- `("already_tight", stop, None)` — baseline stop is not wider than
  the cap; nothing to do.
- `("tightened", old_stop, new_stop)` — baseline was wider, stop
  moved to the cap floor/ceiling.
- `("exit", new_stop, None)` — new capped stop already breached by
  market; exit fired immediately with `reason="RETRO_CAP"`.

### Hooks

Three call sites — safe because the helpers are cycle-idempotent:

1. **Startup** (entry-point, after `load_paper_state()` / `load_tp_state()`).
   `fetch_prices=False` to avoid a cold Yahoo fetch at process start;
   uses `entry_price` as the current-price proxy. By construction,
   entry ± 0.75% never equals entry, so force_exit is silent on
   startup. The immediate-exit path fires from the first manage cycle
   instead, where real quotes are available.
2. **`manage_positions()`** — top of each long-management cycle.
3. **`manage_short_positions()`** — top of each short-management
   cycle.

### New `/retighten` command

Manual trigger. Mostly a transparency / "show me what the cap would
do right now" tool, since the automatic passes cover it. Output:

```
🔧 Retro-cap (0.75%)
──────────────────────────────────
AAPL SHORT [paper]
  stop $273.95 → $270.79
TSLA SHORT [paper] EXITED
  breached at cap $390.91
──────────────────────────────────
Summary: 1 tightened, 1 exited,
0 no-op, 0 already-tight
```

Registered on both the main bot and the TP bot (handler + BotCommand).

### Design principles preserved

- **More conservative than baseline, never looser.** The cap only
  tightens; a stop that's already tighter is left alone.
- **Fail-closed.** Missing position data → `summary["errors"] += 1`
  and the position keeps its existing stop; we do not eject.
- **Trail interaction.** When `pos["trail_active"]` is True, the
  retighten pass is a no-op. Trail logic is already tighter than
  0.75% by construction.

### Tests

11 new v3.4.23 regression tests (76/76 local pass, up from 65):

- BOT_VERSION bump
- Helpers exist and return 3-tuples
- Already-tight short (entry 100, stop 100.50) → `already_tight`
- Wide short (AAPL 268.77 / 273.95) → `tightened` to 270.79
- Wide long (200 / 195) → `tightened` to 198.50
- `trail_active=True` → `no_op`, stop untouched
- `retighten_all_stops` shape check (all 6 summary keys)
- `manage_positions` / `manage_short_positions` source contains the
  retighten call
- Startup entry-point invokes retighten with `fetch_prices=False`
- `cmd_retighten` is async + `retighten` in MAIN_BOT_COMMANDS
- `/retighten` CommandHandler wired on both main and TP apps

---

## v3.4.22 — Hotfix: TradersPost short webhook actions (2026-04-21)

Short entries and short covers sent to TradersPost were being rejected
with HTTP 400 INVALID ACTION. First caught live this morning (4/21)
when the AAPL short attempt at 09:59 CDT came back rejected; paper
side took the trade, TP side never touched the account.

### Root cause

TradersPost's webhook API only accepts these `action` values:

- `buy`, `sell`, `exit`, `reverse`, `breakeven`, `cancel`, `add`

We were sending:

- `sell_short` on short entry (`execute_short_entry`)
- `buy_to_cover` on short cover (`execute_short_exit` path)

Both are flagged invalid by TradersPost. The long side already used
the legal `buy` / `sell` values, which is why long trades (MSFT this
morning) completed normally while shorts failed silently.

### Fix

TradersPost is single-URL bidirectional for Val's setup — the strategy
config + open-position state is what determines direction. So the
correct wire values are:

| intent       | wire action |
|--------------|-------------|
| Long entry   | `buy`       |
| Long exit    | `sell`      |
| Short entry  | `sell`      |
| Short cover  | `buy`       |

Changes:

- `execute_short_entry` — `sell_short` → `sell`.
- Short cover path in `execute_cover` — `buy_to_cover` → `buy`.
- `send_traderspost_order` — docstring rewritten to describe the
  TradersPost allowlist; the `if action in ("buy", "buy_to_cover")`
  limit-price branch tightened to `if action == "buy"` since
  `buy_to_cover` no longer exists as a wire value.
- The internal `tp_unsynced_exits` tracking dict still uses the
  human-readable `"buy_to_cover"` label so `/tp_sync` reads naturally
  — that label is never sent over the wire.

### No strategy or gate changes

Same adaptive logic, same gates, same stops, same near-miss log.
Purely a wire-protocol fix.

### Tests

Five new v3.4.22 regressions:

1. `short entry sends TradersPost-legal action=sell`
2. `short cover sends TradersPost-legal action=buy`
3. `no webhook sends action='sell_short'`
4. `every send_traderspost_order action is TP-legal` (regex-scans every
   call site and asserts the literal action is in the allowlist)
5. `send_traderspost_order limit-price branch is 'buy'-only` (tightens
   the limit-direction guard so a future `exit` or `reverse` can't
   silently end up on the wrong side)

65 local tests pass (was 60).

---

## v3.4.21 — Stop cap, near-miss log, dashboard gates, deploy card split (2026-04-21)

This release bundles four themed changes that came out of the same
morning session. Each is small on its own; together they tighten risk
control on late entries, make declined breakouts visible after the
fact, give the dashboard a per-ticker view of why a ticker is or isn't
arming, and trim the deploy card down to just what shipped this time.

### 1. Stop cap: max 0.75% from entry (Option A)

MSFT long entered this morning at $425.93 with a stop of $419.26 — the
baseline `OR_High − $0.90` formula. Problem: the price had already
climbed 1.37% above OR_High by the time the entry confirmed, so the
"OR-buffer" stop was sitting $6.67 below entry — a 1.57% risk on a
strategy whose thesis decays well before then. The formula ignored
entry price entirely.

**Fix.** New constant `MAX_STOP_PCT = 0.0075` and two helpers:

```python
def _capped_long_stop(or_high_val, entry_price, max_pct=MAX_STOP_PCT):
    baseline = or_high_val - 0.90
    floor = entry_price * (1.0 - max_pct)
    final = max(baseline, floor)       # tighter of the two
    return round(final, 2), final > baseline, round(baseline, 2)

def _capped_short_stop(pdc_val, entry_price, max_pct=MAX_STOP_PCT):
    baseline = pdc_val + 0.90
    ceiling = entry_price * (1.0 + max_pct)
    final = min(baseline, ceiling)     # tighter of the two
    return round(final, 2), final < baseline, round(baseline, 2)
```

**Invariant (locked design principle):** the cap can only *tighten* the
stop, never loosen it. For both sides, the entry-relative cap replaces
the baseline only when it sits closer to entry than baseline does. A
near-OR / near-PDC entry keeps its original baseline stop unchanged.

Applied in both `execute_entry` and `execute_short_entry`. The entry
Telegram card now shows `stop: entry −0.75%` when the cap kicks in and
the original `stop: OR_High−$0.90` / `stop: PDC+$0.90` otherwise.

Worked example (MSFT, 4/21):

| field           | before  | after   |
|-----------------|---------|---------|
| entry           | 425.93  | 425.93  |
| stop            | 419.26  | 422.74  |
| risk ($)        | 6.67    | 3.19    |
| risk (%)        | −1.57   | −0.75   |

### 2. Near-miss diagnostic log

When a breakout clears price but fails the volume gate (`LOW_VOL` or
`DATA_NOT_READY`), we now record it in an in-memory ring buffer
(`_near_miss_log`, capped at `_NEAR_MISS_MAX = 20`). Each entry captures
ticker, side, reason, volume%, close vs level, and timestamp.

A new Telegram command, `/near_misses`, prints the last 10 entries
formatted as `HH:MM TICKER SIDE REASON` with vol% and close-vs-level
margins — enough to answer "did we see this breakout and decline it,
or did we never see it?" without digging through Railway logs. The
command is registered on both the main bot and TP bot, and advertised
in `MAIN_BOT_COMMANDS`.

**This does NOT change trade behavior.** The gates still decline the
trade; we just record the decision. Consistent with the fail-closed
principle: no catch-up trades are attempted even if the conditions
would have passed a cycle later.

### 3. Dashboard: per-ticker gate chips + next-scan countdown

The dashboard's gates panel used to show only global status. Now
each active ticker gets its own chip row:

```
MSFT · L ·  Brk  ·  Vol 142%  ·  PDC  ·  Idx
AAPL · S ·  Brk  ·  Vol  na   ·  PDC  ·  Idx
```

Chips render `on` (green) when a gate passes, `off` (red) when it
fails, and `na` (muted) when the gate hasn't been evaluated this cycle.
The four chips are exactly what Val asked for — no more, no less:
Break, Volume, PDC, Index.

The header's `tick Xs` counter now falls back to `next scan Xs` while a
scan cycle is mid-flight, decrementing each second from the value
`/api/state` reports in `gates.next_scan_sec` (derived from
`SCAN_INTERVAL − age(_last_scan_time)`).

API shape additions in `/api/state`:

- `gates.per_ticker` — list of `{ticker, side, break, vol_pct, vol_ok,
  pdc_ok, index_ok}` rows from the module-level `_gate_snapshot` dict.
- `gates.next_scan_sec` — integer seconds until the next scheduled
  scan, or `null` off-hours.
- `near_misses` — top-level list mirroring `_near_miss_log`, capped
  at `_NEAR_MISS_MAX`.

### 4. Deploy card split

The startup "deployed" card in both bots used to embed
`MAIN_RELEASE_NOTE` / `TP_RELEASE_NOTE`, which carried a rolling
history of the last several versions. Over time that pushed the card
past a useful screen height on mobile.

Now the card embeds `CURRENT_MAIN_NOTE` / `CURRENT_TP_NOTE` —
current-release-only prose that must start with the current
`BOT_VERSION` and contains no references to older versions. `/version`
and its menu button still show the full rolling history unchanged.

Enforced by smoke tests:

- `CURRENT_MAIN_NOTE` / `CURRENT_TP_NOTE` must start with
  `v{BOT_VERSION}` and must not mention any prior version.
- Every line in both notes must fit the 34-char Telegram mobile
  code-block width.
- `send_startup_message` must embed the `CURRENT_*` placeholders and
  must not embed `MAIN_RELEASE_NOTE` / `TP_RELEASE_NOTE`.
- The rolling `MAIN_RELEASE_NOTE` / `TP_RELEASE_NOTE` must still lead
  with the current version so `/version` stays current-first.

### Tests

15 new v3.4.21 regression tests added to `smoke_test.py`:

1. `CURRENT_MAIN_NOTE/CURRENT_TP_NOTE scope + width`
2. `rolling RELEASE_NOTE still leads with current version`
3. `deploy card uses CURRENT_* notes, not rolling RELEASE_NOTE`
4. `MAX_STOP_PCT == 0.0075 (0.75% cap)`
5. `_capped_long_stop tightens when entry is far above OR`
6. `_capped_long_stop leaves baseline alone for near-OR entries`
7. `_capped_short_stop tightens when entry is far below PDC`
8. `_capped_short_stop leaves baseline alone for near-PDC entries`
9. `execute_entry / execute_short_entry use capped stop helpers`
10. `near-miss ring buffer exists and _record_near_miss works`
11. `_near_miss_log respects _NEAR_MISS_MAX cap`
12. `_gate_snapshot dict exists for per-ticker dashboard chips`
13. `check_entry / check_short_entry populate gate snapshot + near-miss`
14. `/near_misses command is a registered handler`
15. `dashboard_server exposes per_ticker gates + next_scan_sec + near_misses`

60 local tests pass (was 45).

---

## v3.4.20 — LOW VOL gate: walk back to last valid bar (2026-04-21)

Today's session opened with zero trades despite multiple clean breakouts
(META, GOOG, MSFT all traded above OR_High for extended periods). Railway
logs showed exactly one gate firing, over and over, across every ticker
and every scan cycle:

```
SKIP META [LOW VOL] entry bar 0 vs avg 56677
SKIP GOOG [LOW VOL] entry bar 0 vs avg 62631
SKIP NVDA [LOW VOL] entry bar 0 vs avg 843762
```

224 LOW VOL skips between 09:45 and 09:59 ET. Zero other gate firings.
The "entry bar" volume was literally `0` on every ticker on every cycle.

**Root cause.** The LOW VOL gate read `volumes[-2]` directly from the
Yahoo 1-min bar response — the most-recently-closed bar. When Yahoo
returns a series where that bar's volume has not yet been populated
(None or 0), the existing code collapsed it to 0 and compared to the
`avg_vol * 1.5` threshold. Average ~56K vs entry 0 → always below →
always skip. Current prices on the same response were fresh, so the
proximity board looked healthy — but no entry could ever be confirmed.

**Fix.** New helper `_entry_bar_volume(volumes, lookback=5)` walks back
from `volumes[-2]` through up to 5 prior bars, returning the first
non-null, positive value. If every candidate bar is null or zero, it
returns `(0, False)` and the caller emits a distinct `[DATA NOT READY]`
log and skips. The original LOW VOL log is now only emitted when a real
bar is found whose volume genuinely fails the 1.5x threshold.

Both LOW VOL gate sites — long-entry (around line 1756) and short-entry
(around line 2670) — were updated.

**Fail-closed.** If the data source returns nothing usable, we still
skip the entry. This matches the locked principle *"adaptive logic only
makes things MORE conservative than baseline, never looser."* The fix
never enters a trade on missing data — it just stops mislabeling missing
data as low volume.

**Tests.** Two new local smoke tests:

- `v3.4.20: _entry_bar_volume walks back past null/zero bars` exercises
  the helper with happy-path, stale-bar, all-stale, empty, and
  lookback-window cases.
- `v3.4.20: entry gates call _entry_bar_volume + emit DATA NOT READY`
  scans module source to enforce that both gate sites use the helper,
  emit `[DATA NOT READY]`, and no longer contain the raw
  `volumes[-2] if volumes[-2] is not None else 0` pattern.

45 local tests pass (was 43).

---

## v3.4.19 — Menu/refresh callbacks: token-based bot routing (2026-04-20)

Second half of the cross-bot data leak fix. After v3.4.18 shipped,
`/status` via the TP visual menu still rendered paper data while the
typed `/status` command on the same bot rendered TP data correctly.

**Root cause.** Three callback handlers —
`positions_callback`, `proximity_callback`, and `menu_callback` —
routed data by comparing `query.message.chat_id` to the
`TELEGRAM_TP_CHAT_ID` env var. In production the TP bot is used in a
chat whose id does **not** match that env var (the startup-menu
sendMessage to that id returns "Chat not found"). So the comparison
returned `False` and the TP bot's menu taps rendered paper data.

Typed `cmd_*` handlers are already correct because they use
`is_tp_update(update)`, which reads the bot **token** on the update —
the authoritative source, since each Application polls with its own
token and only receives updates addressed to its bot.

**Fix.** All three callbacks now use `is_tp_update(update)` (same
path as every `cmd_*`). Chat-id comparisons remain only in the
`_reset_authorized` helper, where they function as an explicit
authorization guard (not as data routing) and are deliberately kept.

**Tests.** Added a local smoke test
(`v3.4.19: menu/refresh callbacks route by token, not chat_id`)
that inspects the source of each callback and enforces
`is_tp_update(update)` in code and no `TELEGRAM_TP_CHAT_ID`
comparisons outside of comments. 43 local + 9 prod tests, all green.

---

## v3.4.18 — Menu-button bot routing fix (2026-04-20)

Fix for a cross-bot data leak: on the TP bot, any command invoked via
a `/menu` inline button rendered **paper** data instead of TP data.
The user-visible symptom was a "mix of paper and TP" on the TP bot
(e.g. `/perf`, `/dayreport`, `/log`, `/replay`, `/proximity`, `/help`,
`/algo`, `/mode`, `/reset` when reached through menu taps).

**Root cause.** Menu taps route through `menu_callback`, which calls
`_invoke_from_callback` with a minimal `_CallbackUpdateShim` wrapper
that stands in for `update`. The shim forwarded `message`,
`effective_message`, `effective_user`, `effective_chat`, and
`callback_query` — but **not** `get_bot()`. Every downstream
`is_tp_update(update)` call therefore raised `AttributeError`, hit the
`try/except` fallback, and returned `False`. Commands that branch on
`is_tp_update` (most of them) then read the paper dicts even on the
TP bot.

**Fix.** `_CallbackUpdateShim` now forwards `get_bot()` to the
underlying `CallbackQuery`, so `is_tp_update()` resolves the real bot
token whether the handler was reached via a typed command or a menu
button.

**Surface affected (before the fix).** Every `cmd_*` dispatched via
`_invoke_from_callback`: `cmd_help`, `cmd_algo`, `cmd_mode`, `cmd_log`,
`cmd_replay`, `cmd_or_now`, `cmd_reset`, `cmd_dayreport`,
`cmd_proximity`, `cmd_perf`. Typed commands were already correct.

**Tests.** Added regression test
`v3.4.18: _CallbackUpdateShim forwards get_bot() for is_tp routing`
in `smoke_test.py` that constructs a shim over a fake query with the
TP token and asserts `is_tp_update(shim) is True` (and False for a
non-TP token). 42/42 local tests pass.

**Release notes.** Bumped both `MAIN_RELEASE_NOTE` (detailed prose)
and `TP_RELEASE_NOTE` (headline-only, still ≤34 char/line).

---

## v3.4.17 — /status refresh fix + deploy card cleanup (2026-04-20)

Two small follow-ups to v3.4.16.

**Fix: `/status` Refresh button error.** Tapping Refresh when nothing
had changed since the last render raised `Message is not modified:
specified new message content and reply markup are exactly the same
as a current content`, and the global error handler surfaced it to
the user as a command failure. Two changes:

- `positions_callback` now appends a `↻ Refreshed HH:MM:SS CDT`
  footer to the rebuilt message so each tap produces visibly different
  content — Telegram no longer rejects the edit.
- The `edit_message_text` call is wrapped in `try/except` that swallows
  any remaining race (e.g. rapid double-tap within the same second) and
  logs at debug level instead of propagating. The user already got the
  button-tap acknowledgment via `query.answer()`.

**Fix: main-bot deploy card felt empty.** v3.4.16's `MAIN_RELEASE_NOTE`
was a three-line meta note about the bot split itself — informative but
not the detailed release prose the main bot had shown before. Rewrote
both notes to hit the right tone per bot:

- `MAIN_RELEASE_NOTE`: detailed prose describing what shipped this
  release (matches the pre-v3.4.16 style).
- `TP_RELEASE_NOTE`: abbreviated — one line per recent TP-relevant
  version, plus a `/tp_sync` pointer.

Smoke test for `main is TP-free` was relaxed: it now forbids broker
internals (`webhook`, `broker`, `unsynced`) in the main note but
permits a brief `/tp_sync` context mention pointing readers at the TP
bot. The width check (≤34 chars/line) still covers both notes.

---

## v3.4.16 — TP bot isolation cleanup (2026-04-20)

The dual-bot setup (main + TP) shared every command, every release note,
and every startup card. That worked while TradersPost was a small feature
but now leaks broker details into the paper-trading bot. v3.4.16 isolates
all TradersPost surface area onto the TP bot so the main bot stays a
clean paper portfolio + scanner view.

**Changes**

- **`/tp_sync` is TP-bot-only.** Removed from `MAIN_BOT_COMMANDS` (so it
  no longer appears in the main bot's `/` menu). `TP_BOT_COMMANDS` is now
  constructed as `MAIN_BOT_COMMANDS + [tp_sync]` instead of a copy.
- **Graceful redirect on main.** A misdirected `/tp_sync` to the main bot
  gets a friendly "This command lives on the TP bot" reply via the new
  `cmd_tp_sync_on_main` handler, instead of silence.
- **Split release notes.** `RELEASE_NOTE` is now two constants:
  `MAIN_RELEASE_NOTE` (scanner/portfolio only, never mentions TP) and
  `TP_RELEASE_NOTE` (full TP context incl. v3.4.15 webhook history).
  `/version` and the Version menu callback both branch on
  `is_tp_update(update)` to pick the right one.
- **`/help` is bot-aware.** TP bot's `/help` gets a "Broker" section
  listing `/tp_sync`. Main bot's `/help` is unchanged (no TP mention).
- **Startup card split.** `send_startup_message()` now builds two cards:
  main gets paper cash/positions only + `MAIN_RELEASE_NOTE`; TP gets TP
  cash/positions + `TP_RELEASE_NOTE`. Previously both bots received the
  same combined card.

**Tests added**

- `tp_sync lives on TP bot only` — asserts absence from main commands,
  presence in TP commands.
- `release notes split` — forbids `tp_sync`/`webhook`/`broker`/`unsynced`
  in `MAIN_RELEASE_NOTE`; requires `/tp_sync` in `TP_RELEASE_NOTE`.
- `main-bot /tp_sync redirect handler exists` — asserts
  `cmd_tp_sync_on_main` is defined and distinct from `cmd_tp_sync`.
- `release notes within 34-char Telegram width` — regression guard on
  both notes together.

**What did NOT change**

- Data-layer routing via `is_tp_update(update)` was already correct
  across `cmd_dashboard`, `cmd_status`, `cmd_dayreport`, `cmd_eod`,
  `cmd_log`, `cmd_replay`, and all `send_telegram` / `send_tp_telegram`
  callsites. Those required no edits.
- `RELEASE_NOTE` is kept as a backwards-compat alias of
  `MAIN_RELEASE_NOTE` in case any external tooling imports it.

---

## v3.4.15 — Webhook response handling (2026-04-20)

v3.4.14 flipped the switch but left the return trip unverified: when
TradersPost rejected an order we logged the response and carried on.
This release closes that loop — broker responses are parsed, failures
are surfaced, entries are broker-first, and any exit rejection is
tracked in a dedicated dict so nothing silently drifts out of sync.

**Changes**

- `send_traderspost_order()` now returns a structured dict:
  `{success, skipped, message, http_status, raw}`. Callers branch on
  `success or skipped` (where `skipped=True` means the webhook was
  intentionally not called and should not block paper trading).
- New helper `_extract_broker_message()` parses TradersPost's possible
  response shapes: top-level `message`, `error`, or `errors[]` (list
  of strings or list of dicts). Result is length-capped at 80 chars.
- TP Telegram alerts now include the broker reason and HTTP status
  on failure: `✗ TP webhook rejected\nBUY SPY 10 @ $450.00\n`
  `Limit: $450.02\nReason: Insufficient buying power\nHTTP: 400`.
- `tp_state["recent_orders"]` entries now carry `message` +
  `http_status` fields alongside `success`.

**Ordering changes**

- **Entries are webhook-first.** `execute_entry` and
  `execute_short_entry` fire the webhook BEFORE mutating
  `tp_positions` / `tp_short_positions`. If TradersPost rejects, the
  TP mirror block is skipped entirely — paper stays simulated, TP
  stays empty, nothing to unwind. `skipped=True` (broker off) counts
  as OK so entries still work when `TRADERSPOST_ENABLED=false`.
- `tp_positions[ticker]["broker_synced"] = True` is set on successful
  entries so the dashboard and `/tp_sync` can distinguish "definitely
  open on broker" from "orphaned local entry".
- **Exits keep state-first ordering** (we never want to lose a local
  close). Rejections are captured in a new module-level
  `tp_unsynced_exits` dict keyed by ticker, carrying `{action, price,
  shares, message, http_status, time}`. Applies to all three exit
  TP-branches: `close_position`, `close_tp_position`, and
  `close_short_position`'s TP cover.

**Observability**

- `/api/state` now exposes a `tp_sync` section with `enabled`,
  `unsynced_exits`, `recent_orders` (last 5), and lifetime
  sent/success/fail counts.
- Dashboard shows an amber banner under the connection banner when
  any exit is unsynced, listing the first few tickers and the broker
  reason.
- New `/tp_sync` Telegram command (registered on both main + TP bot)
  lists open TP positions with a broker-synced checkmark, the last 5
  webhook outcomes with reason on failures, and any unsynced exits
  flagged for manual reconciliation.

**Smoke tests**

- 6 new local tests cover: skipped-dict contract, broker-message
  parsing across all response shapes, unsynced dict population on
  rejection, the skipped-doesn't-track invariant, `tp_sync` snapshot
  shape, and `/tp_sync` handler registration.
- 1 new prod test: `/api/state` exposes `tp_sync` with the expected
  nested shape.

**Design discipline**

- "Adaptive logic only makes things MORE conservative" — the
  webhook-first entry path aborts entries rather than creating
  phantom state; the exit path refuses to discard a local close.
- Fail-safe: if any webhook response field is missing or malformed,
  we treat it as failure (never trust a non-JSON 200).

---

## v3.4.14 — TradersPost wiring fix (2026-04-20)

Webhook bot is wired to TradersPost for real this time. Previously
`PAPER_MODE = True` was hardcoded at module load, so every call to
`send_traderspost_order()` returned `None` before touching the
network — no webhooks ever fired regardless of env vars. Separately,
even if the flag had been flipped, the close-side wiring was
asymmetric: paper-only close paths called the webhook while the
TP-specific close paths (`close_tp_position`, TP branch of
`close_short_position`) did not, which would have left positions
open on TradersPost after exits.

**Changes**

- Replaced `PAPER_MODE = True` with env-gated `TRADERSPOST_ENABLED`
  (default **off**). Set `TRADERSPOST_ENABLED=true` in Railway when
  ready to go live.
- Re-routed every webhook callsite to the **TP portfolio only**.
  Paper is now simulation-only and never hits TradersPost.
  - `execute_entry`: webhook moved from paper section to TP mirror
    block (fires after `tp_positions[ticker]` is set).
  - `execute_short_entry`: webhook stays after the TP short block
    (was already effectively TP-timed).
  - `close_position` (paper LONG close): webhook removed from paper
    section, added inside the `if ticker in tp_positions:` mirror
    block so TP exits fire reliably.
  - `close_tp_position` (TP-only LONG close): webhook **added**
    (was missing — primary bug).
  - `close_short_position` paper branch: webhook removed.
  - `close_short_position` TP branch: webhook **added** (was
    missing — primary bug).
- `send_traderspost_order` now posts a `✓ sent` / `✗ rejected` /
  `✗ failed` line to the TP Telegram chat after every webhook send,
  so Val sees broker-side confirmations without opening TradersPost.
- `TELEGRAM_TP_CHAT_ID` at line 36 now reads from env (fallback to
  the existing hardcoded value), matching `TELEGRAM_TP_TOKEN`'s
  pattern. This resolves the Railway env-var-vs-code discrepancy.

No trade-logic changes. Stop levels, entry signals, sizing, and
PnL accounting are all untouched — this is a plumbing fix.

---

## v3.4.13 — proximity pct left-align (2026-04-20)

Follow-up to v3.4.12. Right-aligning the pct column pushed the
values up against the card edge and left an inconsistent gap
between the progress bar and the text. Switched `.prox-pct` to
`text-align: left` so each `0.02% · OR-low` starts in the same
spot immediately after the bar.

CSS-only. No trade-logic or backend changes.

---

## v3.4.12 — proximity row fix (2026-04-20)

Purely cosmetic. The dashboard proximity card's right-most column
(`0.02% · OR-low`) wrapped onto a second line at mobile widths
because `.prox-pct` was pinned to 64-80px and the full string needs
~100px in the monospace font.

**Fix:** widen `.prox-pct` from 80 → 110 (desktop) and 64 → 100
(mobile). Since `.prox-bar` uses `flex: 1`, it shrinks to fill the
remainder — the bar gets slightly narrower, the pct + label fit on
one line. Added `white-space: nowrap` on `.prox-pct` as a belt-and-
suspenders guard against a future longer label.

CSS-only change. No trade-logic, no backend, no API changes.

---

## Tooling — post-deploy smoke workflow (2026-04-20)

Not a bot release — CI-only change, no version bump.

Adds `.github/workflows/post-deploy-smoke.yml`. On every push to
`main` (and on manual dispatch), the workflow:

1. Reads the committed `BOT_VERSION` from `stock_spike_monitor.py`.
2. Polls `https://.../api/state` every 10s for up to 5 minutes until
   `version` matches the committed value — i.e. Railway is live on
   the new build.
3. Runs `python smoke_test.py` (31 local tests).
4. Runs `python smoke_test.py --prod --expected-version <v>` (9 prod
   tests against the live dashboard), with a 65s cushion after the
   wait step so the rate-limit bucket has cleared.
5. If anything fails, posts a Telegram alert to the TP chat with the
   failing test names and a link to the Action run, and uploads logs
   as an artifact.

Required GitHub secrets: `DASHBOARD_PASSWORD`, `TELEGRAM_TP_TOKEN`,
`TELEGRAM_TP_CHAT_ID`.

The workflow uses `concurrency: cancel-in-progress` so rapid-fire
merges don't stack — only the newest commit's rollout is verified.

---

## v3.4.11 — smoke test harness (2026-04-20)

Adds a standalone `smoke_test.py` that covers the full bot in two modes:

- **Local (31 tests):** utility helpers, short-symmetry helpers,
  `_today_pnl_breakdown` paper/TP paths, `_compute_today_realized_pnl`,
  `_per_ticker_today_pnl`, N5 open-position `date` field, M1
  `load_paper_state` clearing `daily_short_entry_count`, state
  save/load round-trip, v3.4.10 `/reset` guards (stale/fresh/cross-bot/
  unauthorized/malformed), v3.4.9 dashboard auth (roundtrip/expired/
  wrong-secret/malformed/missing/future-dated), M6 rate limiter
  (5 OK, 6th blocked, per-IP buckets), `_build_eod_report` report
  builders with L+S tags, `_collect_day_rows`, DEFENSIVE gate
  regression, and the weekly digest long+short merge.
- **Prod (9 tests):** live dashboard `/login` 302/401, `/api/state`
  version + expected keys, cookie required + forged-cookie rejection,
  `/stream` SSE emits within 5s, rate limiter trips on the 6th bad
  attempt in <60s, and `/static/` assets serve without auth.

Run `python3 smoke_test.py` for local mode or
`python3 smoke_test.py --prod --password <pw>` for prod mode. Exit
code is 0 only when every test passes.

**SSM_SMOKE_TEST guard.** The test harness needs to import
`stock_spike_monitor.py` to exercise its helpers, but the module
normally boots the Telegram client, scheduler thread, and catch-up
on import. A new env-var guard at the bottom of the module short-
circuits all of that when `SSM_SMOKE_TEST=1`. Production behavior
is unchanged — the guard only fires when the env var is set to the
exact string `"1"`.

**Tests caught two real bugs in the initial draft.** The EOD report
expects `"side": "short"` on short trades (set in `close_short_position`
at line 2725), which an earlier test fixture omitted. And
`_collect_day_rows` takes three positional args (`target_str`,
`today_str`, `is_tp`), not a `portfolio` kwarg. Both were fixed as
the harness was built, exercising the "tests catch test bugs" loop.

No trade-logic changes.

---

## v3.4.10 — /reset guards (2026-04-20)

Addresses C7 from the code review. The `/reset` callback handler
previously had **zero validation** before wiping portfolios — any tap
on any surviving Confirm button would execute the reset. Three guards
now sit in front of `_do_reset_*()`:

**1. Owner check.** The callback's chat_id must match either `CHAT_ID`
(paper bot) or `TELEGRAM_TP_CHAT_ID` (TP bot). A stray user added to
either chat can no longer wipe state.

**2. Action/bot match.** A paper reset must be confirmed from the paper
bot; a TP reset from the TP bot. `both` may come from either. This
prevents a callback routed to the wrong bot from taking destructive
action.

**3. Freshness window.** Confirm buttons now embed a Unix timestamp in
`callback_data` (format: `reset_paper_confirm:1776720173`). The handler
rejects any confirm older than `RESET_CONFIRM_WINDOW_SEC` (60s). This
eliminates the scroll-back failure mode where tapping an old /reset
message would silently wipe the current portfolio.

When a reset is blocked, the handler logs a warning and replaces the
message with an explicit error (e.g. `❌ Reset blocked: expired
confirm (347s old).`).

No trade-logic changes. Entries, exits, sizing, stops unchanged.

---

## v3.4.9 — Dashboard security hardening (2026-04-20)

Web dashboard hardening only — no bot trade-logic changes. Addresses three
findings from the v3.4.7 code review.

**Login rate-limiting (M6)**

Per-IP in-memory sliding-window rate limiter on `POST /login`: 5 attempts
per 60-second window. Excess attempts return HTTP 429 with a `Retry-After`
header. The bucket key uses `X-Forwarded-For` (Railway proxy) and falls
back to the peer address.

**Secure cookie flag (M7)**

Session cookie now sets `Secure=True`, ensuring browsers only send it over
HTTPS. Railway terminates TLS at the proxy, so this is the correct value.

**Session token redesign (M8)**

The old token was a deterministic `HMAC(password, fixed-string)` — same
value forever, no expiry, no replay protection. Replaced with:

- A random 32-byte `_SESSION_SECRET` generated at process start (kept in
  memory only). Optional `DASHBOARD_SESSION_SECRET` env var for testing.
- Token format: `HMAC_SHA256(_SESSION_SECRET, big-endian-uint64-ts).hex():ts`
- `_check_auth` validates the signature in constant time, then enforces
  the issue-timestamp is within `SESSION_DAYS` (7) and not future-dated
  beyond a 60-second clock-skew tolerance.
- A bot restart invalidates every session (the secret is regenerated).
  Cheapest possible global logout.

**Hardening**

- `DASHBOARD_PASSWORD` must now be ≥ 8 characters or the dashboard
  refuses to start (logs a warning).
- Per-process secret means no DB or filesystem state needed.

**Operational note** — you will be logged out and need to sign in again
with the existing 24-character password (`...bD8Z`). Cookie format change
is not backward-compatible with v3.4.8 sessions.

---

## v3.4.8 — Short-symmetry fixes from code review (2026-04-20)

A full code review surfaced **six places** in the codebase where short P&L
was silently dropped because the code only iterated `paper_trades` looking
for `action == "SELL"` (or `"COVER"`, which is dead code — COVERs only
live in `short_trade_history`). Same root-cause class as v3.4.6 / v3.4.7.

**Critical financial-calc fixes**

- **DEFENSIVE mode gate** (`_compute_today_realized_pnl`) now sums long
  SELLs + short COVERs. Previously, a short-only losing day would never
  trigger DEFENSIVE mode — a risk-management hole.
- **EOD CLOSE summary** Telegram message now reports correct trade count,
  W/L, and Day P&L on days with shorts (paper + TP).
- **`/dashboard` TP branch** Day P&L now includes TP shorts.
- **`/dashboard` paper branch** open-position count now includes open
  shorts (was longs-only).
- **`/mode` per-ticker P&L** observer now includes short losses, so red-
  list tickers reflect short concentration.
- **Web dashboard** (`dashboard_server.py`) `realized` field is now
  date-filtered for both paper_trades and short_trade_history. Prevents
  yesterday's P&L bleeding into today's equity figure on a post-midnight
  restart before 09:30 ET.
- **Sunday weekly digest** (`send_weekly_digest`) now merges
  `trade_history + short_trade_history` before building the digest, so
  shorts appear in win-rate, total P&L, best day, and top-performers.

**Architectural cleanup**

- New canonical helper `_today_pnl_breakdown(is_tp)` returns
  `(sells, covers, total_pnl, wins, losses, n_trades)` for the given
  portfolio. Single source of truth — replaces five hand-rolled
  summations across EOD, /dashboard, and weekly code paths.

**Edge-case fixes**

- Open long positions now carry a `"date"` field (set in `execute_entry`
  for both paper and TP). `_open_positions_as_pseudo_trades` already
  filtered on this field; without it, `/dayreport today` was silently
  dropping all open longs.
- `load_paper_state` now clears `daily_short_entry_count` on a new-day
  restart (previously only `daily_entry_count` was cleared). Without
  this, yesterday's per-ticker short caps could silently block today's
  shorts after an overnight restart.

No trade-logic changes — entries, exits, sizing, and stops are unchanged.

---

## v3.4.7 — /log + /replay fix: include today's shorts (2026-04-20)

Sister bug to v3.4.6. The `/log` and `/replay` commands' **today branch**
only read from `paper_trades` (or `tp_paper_trades`), which never holds
shorts. Result: on a short-only day, both commands reported “No trades
on …”. Past-date queries already worked because that branch reads from
`trade_history` + `short_trade_history`.

**Fixes**

- New `_collect_day_rows(target_str, today_str, is_tp)` helper rebuilds
  `/log` rows from up to four sources for the today branch:
  long opens/closes (`paper_trades`), closed shorts
  (`short_trade_history`, synthesized OPEN + COVER rows), and
  currently-open shorts (`short_positions`, OPEN row only).
- `/replay` now also reads `short_trade_history` and `short_positions`
  on its today branch (was history-only before).
- The `/log` Day P&L line now sums **longs + shorts** (was longs only).
- Open-position count now includes open shorts.
- Past-date branches were already correct — unchanged.

No trade-logic changes — only the report builders.

---

## v3.4.6 — EOD report fix: include shorts (2026-04-20)

The auto EOD report sent at 15:58 ET was reporting 0 trades / $0 P&L on
days when only shorts had closed. Root cause: the report filter only
looked at `paper_trades` for `action='SELL'`. Paper short closes are
logged with `action='COVER'` and live in `short_trade_history`, not
`paper_trades`. They were silently dropped. The same bug affected the TP
report. All-time totals also excluded short P&L.

**Fixes**

- EOD report now rebuilds from `trade_history` (longs) +
  `short_trade_history` (shorts) for paper, and the TP equivalents for
  TP. Both portfolios filter today's trades by `date == today` from the
  full history lists.
- All-time P&L and W/L now sum **longs + shorts** (was longs only).
- Trade-count line now breaks out by side: `Trades today: 1 (L:0 S:1)`.
- Per-trade rows are tagged `[L]` or `[S]` and sorted by exit time.
- New **`/eod`** command re-sends today's report on demand (paper or TP
  depending on which chat you use).

No trade-logic changes — only the report-building function and a new
command handler.

---

## v3.4.5 — Dashboard cleanup + regime terminology (2026-04-20)

The dashboard had nine pieces of duplicated information and used
`POWER` (a market-session label) where the bot actually reports a
directional **breadth regime** (`BULLISH / NEUTRAL / BEARISH`). This
release cleans up the redundancies and aligns the dashboard's
vocabulary with the bot itself.

**Terminology — now matches the bot**

- **Regime KPI** shows the breadth regime: **BULLISH / NEUTRAL /
  BEARISH** (was previously showing the market mode `POWER`, which
  is a session-window label, not a directional regime). Sub-line
  shows the RSI regime (`OVERBOUGHT / NEUTRAL / OVERSOLD`).
- **New Session KPI** added at the end of the KPI row, showing the
  market mode: **POWER / CHOP / OPEN / DEFENSIVE / CLOSED**, with
  the mode reason as the sub-line.
- **Gate KPI** now reads **READY / WAIT / PAUSED / HALTED** instead
  of `LIVE` (which duplicated the header LIVE pill).

**Redundancies removed**

- Header `mode` chip and its sub-text — duplicated by the new
  Session KPI; removed entirely.
- Whole **System card** removed. Its rows were all duplicates:
  - Trading halted / Scan paused → already in Gate KPI.
  - Server time → already in the header clock.
  - Version → already in the header brand.
  - OR collected → already in Gate KPI sub-text.
- Observer card no longer shows mode reason (now in Session KPI).
  Breadth and RSI rows show the numeric detail only — labels are
  in the KPI cards.
- Three-column grid (Today's trades / Observer / System) is now a
  two-column grid (Today's trades / Observer).
- Gates card heading clarified to “Gates · entry checks”.

No backend changes; `/api/state` payload is unchanged. The bot
module is bumped to v3.4.5 only so the version pill and Telegram
deploy ping reflect the new dashboard.

---

## v3.4.4 — Dashboard sidebar removed (2026-04-20)

The sidebar held only the brand mark, a one-line stream status, and a
sign-out link — all of which fit naturally in the top header. Killed
the whole left column.

**Changes**

- Sidebar `<aside>` deleted; the app grid is now a single column.
- Brand (logo + name + version) moved to the left of the header.
- Stream status (“connected / disconnected”) and “Sign out” link moved
  to the right of the header, after the LIVE pill / clock.
- Mobile media queries updated — sidebar-specific rules removed; the
  header simply wraps to two rows on narrow widths.
- Content area gains ~180 px of horizontal room on desktop.

No backend changes.

---

## v3.4.3 — Dashboard mobile + cleanup (2026-04-20)

First pass at making the dashboard usable on iPhone, plus removing dead
UI weight on desktop.

**Changes**

- **Removed dead “Overview” nav** from the sidebar (it had a single
  non-functional “Dashboard” link).
- **Sidebar trimmed** 220 → 180 px on desktop — more horizontal room
  for the actual data.
- **Tablet layout (≤ 900 px)**: sidebar collapses to a top strip with
  brand, stream status, and sign-out inline. Page becomes naturally
  scrollable instead of full-viewport-locked.
- **Phone layout (≤ 640 px)**: KPIs stack 2-up, all multi-column grids
  collapse to single column, tables get horizontal-scroll containers,
  log tail caps at 200 px height.
- **Small phone (≤ 380 px)**: KPI value font shrinks one step so
  multi-digit equity numbers don’t truncate.
- Tested at iPhone 14 Pro (393 px), iPhone SE (375 px), and 1280 px
  desktop.

No backend changes; static HTML/CSS only.

---

## v3.4.2 — Dashboard hotfix #2 (2026-04-20)

v3.4.1 made the dashboard reachable, but every request to `/api/state`
(and `/stream`) returned 500. Root cause: `_ssm()` in
`dashboard_server.py` did `import stock_spike_monitor as m` from inside
an executor thread. Because the bot is launched via
`python stock_spike_monitor.py`, the running module lives in
`sys.modules['__main__']`, not under its file name. So that import
*re-executed* the entire bot file under a second module name —
including the top-level entry point and `_run_both()`, which calls
`loop.add_signal_handler(...)`. That fails outside the main thread:

```
RuntimeError: set_wakeup_fd only works in main thread of the main interpreter
```

**Fix**

- `_ssm()` now grabs the live bot module via
  `sys.modules['__main__']` (or `sys.modules['stock_spike_monitor']`
  if it was imported by name). Falls back to a fresh import only as a
  last resort (tests / standalone use).
- No re-execution of top-level bot code from worker threads.

---

## v3.4.1 — Dashboard hotfix (2026-04-20)

The v3.4.0 build succeeded but the dashboard never started on Railway.
The Railway service uses the `Dockerfile` (not Nixpacks), and the
Dockerfile only copied `stock_spike_monitor.py` into the image. As a
result, `import dashboard_server` failed at startup with `No module
named 'dashboard_server'`. The bot caught the exception and kept
running (fail-safe wrapper), but the web UI was never available.

**Fix**

- Dockerfile now also copies `dashboard_server.py` and the
  `dashboard_static/` directory.
- No code changes; v3.4.0 dashboard logic unchanged.

---

## v3.4.0 — Live web dashboard (2026-04-20)

Added a private, read-only web UI that mirrors everything the Telegram
commands show and pushes updates in real time over SSE.

**What's included**

- **Auth**: single shared password via `DASHBOARD_PASSWORD` env var.
  Server does **not start** unless this is set. On success, a signed
  `HttpOnly` cookie is issued (7-day expiry).
- **Endpoints** (all require a valid cookie except `/` and `/login`):
  `/` (dashboard or login page), `/login`, `/logout`,
  `/api/state` (JSON snapshot), `/stream` (Server-Sent Events push).
- **Isolation**: runs in a dedicated daemon thread with its own
  asyncio loop. Zero coupling with the python-telegram-bot event
  loop. If the dashboard module raises at any point, the bot keeps
  running.
- **Read-only by design**: no endpoint mutates bot state. No order
  placement, no toggles, no parameter changes. This respects the
  locked principle that adaptive logic only makes things more
  conservative — the dashboard adds zero new attack surface.
- **What it shows**: equity (with v3.3.3 cash / long MV / short
  liab breakdown), day P&L, open positions, proximity scanner
  with live prices and open markers, today's trades, regime
  observer (breadth, RSI, mode reason), gate status, and a
  live-scrolling log tail.
- **Resilience**: client auto-falls back to `/api/state` polling
  every 5s if SSE drops, with stale-data watchdog.

**Config**

- `DASHBOARD_PASSWORD` — required. Unset = server disabled.
- `DASHBOARD_PORT` — optional, defaults to `8080`.

On Railway, expose the service on a second public port to route
traffic to the dashboard.

No trade-logic changes.

---

## v3.3.3 — Hotfix: short accounting in portfolio snapshot (2026-04-20)

NVDA short fired this morning at $198.00 on 10 shares. `/positions`
showed the correct $-5.00 unrealized P&L line, but the Portfolio
Snapshot below it read:

```
Cash:           $101,980.00
Market Value:   $1,980.00
Total Equity:   $103,960.00
Unrealized P&L:      -$5.00
vs Start:         +$3,960.00   (started at $100,000)
```

That $3,960 gain is bogus. The snapshot was ~$3,965 too high relative
to reality.

**Root cause**

Short accounting. On entry, we credit `entry_price * shares` to
`paper_cash` — correctly, that's the proceeds of the short sale. But
the snapshot math also **added** `entry_price * shares` to the
"Market Value" field and then summed `cash + market_value` for
equity. That double-counts the proceeds and silently treats a short
as a long with the same dollar exposure.

The correct mental model:
- Short proceeds live in Cash (already credited on entry).
- The short itself is a **liability** equal to the current buy-back
  cost: `current_price * shares`.
- Equity contribution of an open short = `entry_price * shares -
  current_price * shares` = `short_unreal`.

So the correct equation is:
```
equity = cash + long_market_value - short_liability
```
not
```
equity = cash + long_market_value + short_entry_cost_as_if_long  ❌
```

**Fix**
- All three portfolio-snapshot sites (`/positions` paper, `/positions`
  TP, and the generic `_build_positions_text` used by the refresh
  callback) rewritten to compute `short_liability = sum(current_px *
  shares)` per open short and subtract it from equity.
- Snapshot output replaces the single `Market Value` line with two
  clearer lines so the math is auditable:
  - `Long MV: $X` — long-side market value.
  - `Short Liab: $Y` — current buy-back cost (only shown when >0).
- `/status` Est. Value for the paper portfolio also corrected to
  subtract short liability (previously ignored shorts entirely).
- `Unrealized P&L` line already used the right formula; unchanged.
- `vs Start` now derived from the corrected equity, so it matches
  `Unrealized P&L` to the cent when there are no closed trades.

**What the NVDA screen now shows**
```
Cash:        $101,980.00
Long MV:         $0.00
Short Liab:  $1,985.00
Total Equity: $99,995.00
Unrealized P&L:     -$5.00
vs Start:           -$5.00   (started at $100,000)
```

**Not changed**
- Zero trade-logic changes. No entry gates, exits, stops, trails,
  sizing, adaptive bounds, or safety floors touched.
- No state / persistence / env var changes.
- v3.3.2 /proximity UX, v3.3.1 open-positions-in-perf, v3.3.0
  proximity scanner all unchanged.
- All 14 existing unit tests still pass.

---

## v3.3.2 — /proximity UX polish (2026-04-20)

Small UX pass on the v3.3.0 proximity scanner based on live use of the
NVDA short this morning. Three additive tweaks — zero changes to trade
logic, adaptive parameters, safety floors, or persistence.

**Refresh button**
- `/proximity` now returns with an inline 🔄 Refresh button, same
  pattern as `/positions` and `/status`. Tapping it re-runs the
  executor-backed build and edits the existing message in place.
- Also keeps a 🏠 Menu button alongside for quick return.

**Current prices**
- The old "Polarity vs PDC" compact block is replaced by a richer
  **Prices & Polarity vs PDC** block that shows each ticker's live
  price alongside its polarity arrow. Format per cell:
  `AAPL $234.56 ↑`.
- Two cells per row in the common case (fits ≤34 mobile chars). If a
  pair would exceed 34 cells (4-digit price + emoji lead), falls back
  to single-cell rows for that pair. No wrapping.

**Open-position markers**
- Tickers with an open paper position now carry a colored circle
  instead of the leading 2-space indent:
  - 🟢 long open
  - 🔴 short open
- Marker appears in all three per-ticker sections: LONGS table,
  SHORTS table, and Prices & Polarity block. In a chat where the TP
  bot issued the command, it reads from `tp_positions` /
  `tp_short_positions` instead.
- Legend line renders at the bottom only when at least one marker
  is present, so the scanner stays clean on days with no opens.

**Not changed**
- Global SPY/QQQ AVWAP gate, long/short sort order, OR-High / OR-Low
  gap math — all unchanged.
- No new state, persistence, env vars, or handlers beyond a single
  `proximity_refresh` callback (registered on both paper + TP apps).
- v3.3.1 behavior (open positions in /perf + /dayreport) unchanged.
- All 14 existing unit tests still pass.

---

## v3.3.1 — Hotfix: Open Positions in /perf + /dayreport (2026-04-20)

Live bug surfaced right after v3.3.0 deployed. NVDA short fired at
10:07 CDT (10 shares @ $198.00, stop $202.58) and `/status` correctly
showed the open position, but `/perf` and `/dayreport` both reported
"No completed trades yet." Paper cash also reflected the $1,980 short
sale proceeds ($101,980 vs $100,000 start), proving state was intact.

**Root cause**
- `short_trade_history` (and `trade_history` on the long side) is only
  appended on EXIT — i.e., when a position is covered / sold. On entry,
  the bot writes to `short_positions[ticker]` (or `positions[ticker]`)
  and credits cash, but does not append to the history list.
- `/status` reads the live positions dicts directly, so it sees open
  trades fine.
- `/perf` and `/dayreport` only read the history lists, so an open
  position with no prior closes looks like "no trades" to both views.
- Day-of trading with all positions still open was therefore invisible
  from the two commands most likely to be checked.

As a secondary effect, the DATA LOSS GUARD in `save_paper_state()` was
warning on every tick because it only checked `not trade_history` —
ignoring open positions and the short history entirely. It interpreted
"NVDA short open, cash != start" as a corrupted state.

**Fix**
- New helper `_open_positions_as_pseudo_trades(is_tp, target_date)`
  builds synthetic trade records from the live `positions` /
  `short_positions` dicts with current unrealized P&L. Records are
  marked `unrealized=True` and omit `exit_time*` fields so the existing
  formatter renders them as `→open`.
- `cmd_dayreport` now merges opens into both paper and TP paths when
  the target date is today. Past-date reports are unchanged
  (history-only), since past days have no live opens to fold in.
- `_format_dayreport_section` summary line now splits realized vs
  unrealized: `Paper: N closed  P&L: $X` followed by a conditional
  `Open: M  Unreal: $Y` when opens exist.
- `_perf_compute` / `cmd_perf` render a new **📌 Open Positions**
  section at the top of `/perf` with per-ticker entry → current price,
  unrealized $ / %, and a total unrealized line. Opens are NOT folded
  into realized win-rate math — win-rate still reflects only closed
  trades.
- `cmd_perf` "No completed trades yet" gate relaxed to also check for
  any open positions before short-circuiting.
- `save_paper_state()` DATA LOSS GUARD tightened: now checks
  `has_any_activity = trade_history or short_trade_history or
  positions or short_positions`. Only warns when literally no activity
  exists and cash drifted from start. Eliminates the false-positive
  spam from this morning.

**Not changed**
- Zero trade-logic changes. Entry gates, exits, adaptive bounds, hard
  floors, sizing, trail — all untouched.
- No new state, no new persistence, no new env vars.
- v3.3.0 Proximity Scanner, v3.2.1 tz-naive fix, and v3.2.0 Confluence
  Shield behavior all unchanged.
- All existing unit tests still pass.

---

## v3.3.0 — Proximity Scanner (2026-04-20)

Adds a `/proximity` command that answers the question "how close are we to
a trade right now?" without having to eyeball `/dashboard` + `/orb` side by
side. Read-only diagnostic view — no trade logic, adaptive parameters, or
safety floors are touched.

**What it shows**
- **Global gate row** — SPY and QQQ current price vs session AVWAP with
  ✅ / ❌ markers, plus a one-line verdict: `LONGS enabled`,
  `SHORTS enabled`, or `NO NEW TRADES`. This is the same dual-index
  confluence gate that v3.2.0 uses for ejects, shown forward-looking for
  entries.
- **LONGS table** — every tradable ticker sorted by distance to OR High.
  Names already above trigger (✅) come first, then the closest-below,
  then the rest ascending by gap. Format: `AAPL ✅ +$0.10 (+0.04%)`.
- **SHORTS table** — same ticker set, sorted ascending by gap to OR Low.
  Names already below trigger (✅) come first. Format mirrors the long
  side: `TSLA ✅ -$2.10 (-0.80%)`.
- **Polarity row** — compact `TICKER ↑ / ↓ / =` grid showing price vs PDC.

All rows fit inside Telegram's mobile code-block width (≤ 34 chars with
the leading 2-space indent) so nothing wraps on phone.

**Menu layout**
- Main menu: the OR tile now pairs with a new **🎯 Proximity** tile
  (replacing Day Report in that row).
- Advanced menu: **📅 Day Report** moved here, paired with Log. Day Report
  is a historical / post-session view, so it's a better fit for Advanced
  alongside Log and Replay.

**Registration**
- `/proximity` registered on both main and TP bots.
- Added to `MAIN_BOT_COMMANDS` so it shows in Telegram's native `/` picker.
- Added to `/help` under the Market Data section.

**Not changed**
- Entry gates, exit logic, adaptive bounds, hard floors, sizing, trail —
  all untouched.
- No new state, no new persistence, no new env vars.
- v3.2.0 Confluence Shield and v3.2.1 tz-naive fix behavior unchanged.

---

## v3.2.1 — Hotfix: tz-naive datetimes in persisted state (2026-04-20)

Latent bug surfaced right after the v3.2.0 deploy-restart this morning.
`_last_exit_time` was persisted per-ticker via `datetime.now(timezone.utc).isoformat()`,
but older entries had been written at some point without tz info. On load,
`datetime.fromisoformat(v)` returns a tz-naive datetime for those strings.
Mixing that with `datetime.now(timezone.utc)` in the cooldown check raises
`TypeError: can't subtract offset-naive and offset-aware datetimes`, which
the entry loop caught and logged as `Entry check error <TICKER>: ...` —
silently skipping every long **and** short entry for the affected tickers.

Observable symptom: no trades fired on 2026-04-20 despite OR data, AVWAPs,
and volume all looking fine for most names. Railway logs showed the error
firing every 60s for AAPL, META, GOOG, AVGO (tickers whose persisted exit
time was naive) while other tickers skipped for valid reasons (LOW VOL,
OR sanity).

**Fix**
- `load_paper_state()` now normalizes every `_last_exit_time` entry on
  load: if the parsed datetime is naive, assume UTC and attach
  `tzinfo=timezone.utc`. This matches the original write-site semantics
  (all writes go through `datetime.now(timezone.utc)`).

**Not changed**
- v3.2.0 Confluence Shield behavior unchanged.
- No entry/exit/sizing/stop/trail logic changed.
- All existing unit tests still pass.

---

## v3.2.0 — Dual-Index Confluence Shield (2026-04-20)

Tightens the global eject signal (`LORDS_LEFT` / `BULL_VACUUM`) to fire only
on a **market-systemic** move, not a sector-specific wick. Historically a
1-minute close on either SPY *or* QQQ below AVWAP was enough to flip the
trigger — that produced Flim-Flam noise during sector divergence and
sub-5-min liquidity probes ("Hormuz wicks"). This release requires
Confluence (AND) across both indices and confirmation on a finalized 5-min
bar close before abandonment.

**Rule change**
- Old (v2.9.8 → v3.1.4): `SPY_1m < AVWAP` **OR** `QQQ_1m < AVWAP` → eject longs.
- New (v3.2.0): `SPY_5m_close < SPY_AVWAP` **AND** `QQQ_5m_close < QQQ_AVWAP`
  on the most recently **finalized** 5-min bar → eject longs.
- Mirror for shorts (both indices' 5m close *above* AVWAP).
- If either index reclaims its AVWAP before the 5m bar finalizes, the
  eject is suppressed for that bar.

**Fail-safe**
- Any missing data (fetch failure, < 5 min elapsed, AVWAP not seeded) →
  helper returns `False` → **stay in the trade**. Ambiguity never forces an
  exit.

**Implementation**
- New `_last_finalized_5min_close(ticker)` — reuses `_resample_to_5min`,
  which already drops the in-progress (newest) bucket.
- New `_dual_index_eject(side)` — 'long' / 'short' gate returning bool.
- Four call sites switched: `manage_positions`, `manage_tp_positions`
  (long side + TP loop), `manage_short_positions` (main + TP loop).
- Exit reason keys now emit `LORDS_LEFT[5m]` and `BULL_VACUUM[5m]`.
  Legacy `[1m]` keys preserved in `REASON_LABELS` so historical `/replay`
  and `/log` renders still format correctly.
- `/algo` and `/strategy` text updated to describe AND + 5m confluence.
- 6 new deterministic unit tests in `/tmp/test_observers.py` covering:
  (1) long both below → True, (2) long only SPY below → False,
  (3) short both above → True, (4) short only QQQ above → False,
  (5) missing AVWAP / bar data → False, (6) invalid `side` → False.

**Unchanged**
- Hard stops, trailing stops (min $1.00), RED_CANDLE, POLARITY_SHIFT,
  DAILY_LOSS_LIMIT, min-1-share floor, entry logic, position sizing.
- MarketMode observers still observation-only (no adaptive param yet).

---

## v3.1.4 — /menu Main + Advanced Submenu (2026-04-18)

v3.1.3's 17-button grid felt cluttered and some labels truncated on mobile.
Split into a lean main menu and an Advanced submenu so the daily-use stuff
is one tap and everything else is two.

**Main /menu (10 tiles, 2 columns)**
- Dashboard, Status
- Perf, Price
- OR, Day Report
- Mode, Help
- Monitor (full width)
- Advanced (full width, opens submenu)

**Advanced submenu (8 tiles + Back)**
- Log, Replay
- OR Recover, Test
- Strategy, Algo
- Version, Reset
- ⬅️ Back (returns to main)

**Implementation**
- New `_build_advanced_menu_keyboard()` alongside `_build_menu_keyboard()`.
- `menu_advanced` callback edits the existing menu message in place to swap
  keyboards (no new messages, clean UX).
- `menu_back` callback does the reverse.
- All nine command-executing callbacks from v3.1.3 still work; they're just
  reachable from either menu depending on placement.
- No button callbacks removed — only regrouped.

No behavior changes to scanning, entries, exits, sizing, or observers.

---

## v3.1.3 — /menu Covers Every /help Command (2026-04-18)

Makes the `/help` ↔ `/menu` split useful: `/help` is the polished reference
(non-tappable monospace), `/menu` is the tap grid that covers **every single
command** listed in `/help`.

**New buttons** (in addition to the 10 that were already there):
- Perf, Mode, Log, Replay, OR Recover, Algo, Help, Reset — 8 new taps.
- Total grid: 17 buttons across 7 rows, grouped portfolio → market data →
  reports → system → reference → admin.

**Taps now execute the command**
- Previously `menu_dayreport` and `menu_perf` just echoed "Use /dayreport"
  instead of running the command. Now they actually run it.
- New `_CallbackUpdateShim` + `_invoke_from_callback` helper forwards a
  callback_query through any `cmd_*` handler by faking the Update fields the
  handlers touch (`message`, `effective_message`, `effective_user`,
  `effective_chat`). Keeps the helpers reusable for future tap-button work.
- `context.args` is scoped per invocation and restored after, so passing a
  date through the shim wouldn't leak across taps.
- `/reset` tap delegates to `cmd_reset`, which runs the same two-step
  confirmation flow as the typed command — no accidental resets from a tap.

**/help footer**
- Added one-line tip: `Tip: /menu for tap buttons`. Still within the 33-char
  mobile-code-block width limit.

No behavior changes to scanning, entries, exits, sizing, or observers.

---

## v3.1.2 — /help Rendering Fix (2026-04-18)

Cosmetic fix. Telegram renders regular text in a proportional font, so the
column alignment in v3.1.1's `/help` didn't line up on mobile and several
descriptions wrapped awkwardly onto a second line.

- Help body is now wrapped in a Markdown code block, so Telegram renders it
  in monospace and space-padded columns actually align.
- Descriptions trimmed so every line stays ≤ 33 chars and nothing wraps at
  phone widths. Section headers simplified (no emoji, single word per row).
- Removed the horizontal rule separators (the code block provides its own
  visual frame).

No behavior changes to scanning, entries, exits, sizing, or observers.

---

## v3.1.1 — Help Menu Cleanup + Command Consolidation (2026-04-18)

Small UX release. No behavior changes to scanning, entries, exits, sizing, or
observers — purely command surface cleanup.

**/help additions**
- `/status` now listed (was registered but missing from `/help`).
- `/mode` now listed under Market Data (was missing from `/help`).
- `/orb recover` documented — folds in the old `/or_now` as a subcommand.

**Consolidation (backward compatible)**
- `/positions` stays as a silent alias of `/status`. Removed from `/help` and
  from the Telegram / menu to tighten the surface; the command itself still
  works for anyone who has it in muscle memory.
- `/or_now` stays as a silent alias of `/orb recover`. Same treatment —
  removed from `/help` and the Telegram / menu, command still works.
- `/orb` gains `recover` / `recollect` / `refresh` subcommand that dispatches
  to the existing OR-recovery flow.

**Telegram / menu reorganized**
- Grouped by use: portfolio → market data → reports → system → reference →
  admin. Aliases (`/positions`, `/or_now`) dropped from the menu.
- `TP_BOT_COMMANDS = list(MAIN_BOT_COMMANDS)` — single source of truth for
  both bots.

---

## v3.1.0 — MarketMode Observers (2026-04-18)

Adds three observation-only signals on top of v3.0.0 scaffolding.

- **Breadth observer** — SPY/QQQ vs AVWAP with ±0.1% tolerance →
  BULLISH / NEUTRAL / BEARISH.
- **RSI observer** — Wilder RSI(14) on 5-min bars resampled from the existing
  1-min Yahoo feed. Aggregate = mean(SPY, QQQ) → OVERBOUGHT (≥70) /
  NEUTRAL / OVERSOLD (≤30). Plus per-ticker RSI map for all TRADE_TICKERS.
- **Ticker heat** — per-ticker realized P&L today + per-ticker RSI extremes;
  red list (P&L ≤ -$5) and extremes list surfaced in `/mode`.
- **Per-cycle 1-min bar cache** — `fetch_1min_bars` dedupes within a scan
  cycle with a `__FAILED__` negative-cache sentinel, so observers add ~0
  network calls over v3.0.0.
- Each observer lives in its own try/except and short-circuits when
  `mode=CLOSED`. Nothing reads observer state for trading decisions.
- 8 unit tests for `_resample_to_5min` and `_compute_rsi` (Wilder 1978
  reference sample verified at 74.21).

---

## v3.0.0 — MarketMode Scaffolding + Platform Hardening (2026-04-18)

Milestone release rolling up the significant work of the past week. No breaking
changes; all behavior at the trading layer is backward compatible with v2.9.x.

**MarketMode scaffolding (new)**
- Classifier tags each scan cycle as `OPEN` / `CHOP` / `POWER` / `DEFENSIVE` / `CLOSED`.
- Frozen per-mode advisory profiles with hard clamp bounds on every adaptive
  parameter: `trail_pct` ∈ [0.6%, 1.8%], `max_entries` ∈ [1, 5], `shares` ∈ [1, 10],
  `min_score_delta` ∈ [0.00, 0.15]. `_clamp()` is applied at profile construction
  so out-of-range values are impossible.
- Hard floors (`DAILY_LOSS_LIMIT`, min trail distance $1.00, min 1 share) remain
  constants outside the profile system.
- `scan_loop()` logs `mode=<X>` each cycle and `MarketMode: X -> Y (reason)` on
  transitions.
- `/mode` command shows current classification, advisory profile, and bounds.
- **Observation only in v3.0.0** — no entry, exit, sizing, score, or trail code
  reads the profile yet. Observe in production before wiring the first knob.

**Reliability & UX fixes**
- `/replay` historical view: normalize all four sources (`paper_trades`,
  `trade_history`, `short_trade_history`, TP variants) into a common row shape;
  synthesize both open and close rows from each closed-trade record using
  `entry_time`/`entry_price` + `exit_time`/`exit_price`. Past-date replays no
  longer show `--:--` / `$0.00` placeholders.
- `/dayreport`: threaded chart generation + empty-trades guard (was hanging on
  no-trade days); inline `Text must be non-empty` path fixed.
- `/log` and `/replay`: moved sync work to the executor with 15s timeout and a
  loading message. Historical queries read the correct source (`trade_history`,
  not `paper_trades`).
- `/positions`: full equity snapshot (cash, unrealized P&L, total equity,
  vs-start performance) on paper + TP; refresh action with live-price updates;
  trail-stop details for active trails.
- Menu → Dashboard now renders the same full snapshot `/dashboard` produces (was
  a 2-line summary).
- Menu UX: removed auto-menu after every command; replaced with an opt-in
  `[🗂 Menu]` button.
- Silent-crash fix: replaced Python `%` formatting of `+,.2f` with `.format()`;
  added a global error handler so Telegram surfaces failures instead of freezing.

**Platform**
- TP Portfolio independence hardened (shares signals, separate tracking/UI).
- Multi-instance deployment: env-var-driven config for a second `valstradebot`
  instance on a separate Railway service.

---

## v2.7.0 — Full Gap Analysis Implementation (2026-03-17)

Comprehensive upgrade based on deep industry research across quantitative finance
literature and professional systematic trading practices. Implements all 7
recommendations from the gap analysis report.

### 1. ATR-Based Dynamic Stops (Rec #1 — CRITICAL)
- Replaced fixed 3–6% trailing and 6% hard stops with ATR(14)-based dynamic stops.
- Initial hard stop: entry − (ATR × 2.5).
- Trailing stop: highest high − (ATR × multiplier), where multiplier tightens with profit:
  - At entry: 3.0× ATR → At +5%: 2.5× → At +10%: 2.0× → At +15%: 1.5×
- Market regime multiplier applied to stop distances.
- Backward compatible: positions without ATR data fall back to fixed % stops.

### 2. Volatility-Normalized Position Sizing (Rec #2 — CRITICAL)
- Position sizes now based on equal-risk contribution using ATR.
- Risk budget: 1% of portfolio per trade.
- Position size = risk_budget / (ATR × 2.5 stop distance).
- Still applies signal-strength scaling (50–100%), ToD multiplier, and AI boost.
- Falls back to dollar-based sizing if ATR unavailable.

### 3. Portfolio Heat Limit (Rec #3 — HIGH)
- New `_calculate_portfolio_heat()` tracks total risk if all stops hit simultaneously.
- New buys blocked if portfolio heat ≥ 6% of total portfolio value.
- Prevents catastrophic drawdowns in correlated selloffs.
- Heat logged in scan status messages.

### 4. Per-Ticker Re-Entry Cooldown (Rec #4 — HIGH)
- After any SELL, the same ticker is blocked from re-entry:
  - 4 hours after a winning sell.
  - 8 hours after a losing sell.
- Prevents buy→stop→rebuy→stop churn cycle.
- Cooldown tracked per-ticker with `_record_cooldown()` / `_check_cooldown()`.

### 5. Multi-Regime Market Classification (Rec #5 — MEDIUM-HIGH)
- Replaces binary Fear & Greed model with 4-regime system:
  - **trending_up**: SPY > SMA20 > SMA50, VIX < 22 → easier entry, larger positions.
  - **trending_down**: SPY < SMA20 < SMA50 → +10 threshold, smaller positions, tighter stops.
  - **range_bound**: SMAs converged → +5 threshold, slightly smaller.
  - **crisis**: VIX > 30 or SPY < SMA50 by >3% → +15 threshold, half size, very tight stops.
- Regime cached for 15 minutes. Adjusts threshold, max positions, stop multiplier, and sizing.

### 6. Signal Decay / Dynamic Weighting (Rec #6 — MEDIUM)
- New `_recalculate_signal_weights()` analyzes signal_log.jsonl trade outcomes.
- Correlates each signal component (RSI, MACD, etc.) with winning vs losing trades.
- Components that predict winners get up to 1.5× weight; losers down to 0.5×.
- Requires 10+ wins and 5+ losses to activate (defaults to 1.0× until then).
- Recalculated daily during morning reset.

### 7. Correlation-Aware Position Limits (Rec #7 — MEDIUM)
- New `_check_correlation()` calculates 20-day Pearson correlation between
  a candidate ticker and all held positions.
- Blocks entry if 2+ existing positions have correlation > 0.7 with the candidate.
- Catches crypto clustering (BITO + IBIT + MARA) that sector labels miss.
- Daily returns cached for 1 hour to reduce API calls.

### Integration & Infrastructure
- `get_atr()`: New ATR(14) calculation using Finnhub daily candles, 5-min cache.
- Regime + heat + cooldown info logged in scan cycle messages.
- BUY notifications now show ATR-based stop/trail levels.
- SELL notifications include ATR-HARD-STOP and ATR-TRAIL reason types.
- All changes backward compatible with existing position data.

---

## v2.6 — Intraday Time-of-Day Awareness (2026-03-16)

### Signal Score Modifier (Component #12, ±8 pts)
- New `Time-of-Day` component added to the 12-component signal engine (max score now 158).
- Based on the well-documented U-shaped intraday volume/volatility pattern:
  - **Power Open** (9:30–10:30 AM ET): +8 pts — highest volume and volatility, most reliable signals.
  - **Morning** (10:30–11:30 AM ET): +3 pts — still elevated activity.
  - **Transition** (11:30 AM–12:00 PM ET): 0 pts — neutral.
  - **Lunch Lull** (12:00–2:00 PM ET): -8 pts — lowest volume, more false breakouts, less conviction.
  - **Transition** (2:00–3:00 PM ET): -3 pts — volume recovering.
  - **Afternoon** (3:00–3:30 PM ET): +3 pts — building toward close.
  - **Power Close** (3:30–4:00 PM ET): +6 pts — strong close activity, rebalancing flows.
- Naturally raises the effective threshold during lunch and lowers it during power hours.

### Position Sizing by Time Zone
- Position size now scaled by intraday zone:
  - **Power hours** (open/close): 100% of calculated size.
  - **Morning/Afternoon**: 90%.
  - **Transition**: 80–85%.
  - **Lunch Lull**: 65% — even if a signal passes threshold, trade smaller during low-conviction periods.
  - **Extended hours**: 85%.

### Signal Log & BUY Notification
- `signal_log.jsonl` now captures `tod_zone`, `tod_pts`, `tod_size_mult` for backtesting.
- BUY notification shows the time-of-day zone, point adjustment, and size multiplier.

---

## v2.5.1 — TP Portfolio Independence (2026-03-16)

### TP Portfolio is fully independent from Paper
- `/tpsync reset` now wipes all TP positions and restores starting cash ($100k). Previously it cloned the paper portfolio.
- `/tpsync status` shows TP portfolio snapshot on its own (no paper comparison).
- Removed all "shadow" and "mirror" terminology from user-facing messages and comments.
- `/shadow` command now shows "TP Trading: ON/OFF" instead of "Shadow Mode".
- `/tp` mode label now shows "Active" / "Disabled" instead of "Shadow (Paper Mirror)".

---

## v2.5 — TP Portfolio Sync Fix (2026-03-16)

### Cash Guard on BUY
- TP portfolio BUY path now checks available cash before deducting.
- If cost exceeds cash, shares are capped to 95% of available cash.
- If less than 1 share is affordable, the BUY is skipped entirely.
- Prevents TP cash from ever going negative on new buys.

### Failed EXIT Webhook Sync
- When a TradersPost EXIT webhook fails, the TP portfolio now still removes the position and returns proceeds to cash.
- Previously, a failed EXIT left the position in TP while the scanner had already exited — causing cash drift on subsequent buy cycles.

### Negative Cash Warning
- `/tppos` now displays a warning if TP cash is negative, with instructions to fix via `/tpsync reset` or `/tpedit cash`.

---

## v2.4 — Robinhood Hours + Limit Orders (2026-03-16)

### Trading Hours Fix
- Extended session now correctly matches Robinhood: **7:00 AM – 8:00 PM ET**.
- Previous: bot ran 8:00 AM – 9:00 PM ET — missed 1 hour of pre-market and traded 1 hour past Robinhood's close.
- `get_trading_session()` updated: extended = 6:00–19:00 CT (= 7:00 AM–8:00 PM ET).

### All Orders Now Use Limit Pricing
- Every TradersPost order is now a **limit order** instead of market.
- BUY orders: limit price = current price + 0.5% buffer.
- EXIT orders: limit price = current price − 0.5% buffer.
- Eliminates slippage risk and complies with Robinhood's extended-hours rule (market orders rejected during pre/post-market).
- Constants `LIMIT_ORDER_BUY_BUFFER` and `LIMIT_ORDER_SELL_BUFFER` (default 0.5%) are tunable.
- TP notifications now show "LIMIT BUY" / "LIMIT EXIT" with the limit price.
- Order records include `limit_price` for audit trail.

---

## v2.3 — AI Reasoning in Signal Log (2026-03-15)

### Enhanced Signal Logger
- Signal log (`signal_log.jsonl`) now captures `grok_reason` — Claude's text explanation for BUY/HOLD/AVOID calls.
- Signal log now captures `news_catalyst` — the key news catalyst identified by AI sentiment analysis.
- BUY action log entries now include full AI context: `grok_signal`, `grok_reason`, `news_sentiment`, `news_catalyst`, `fg_index`.
- These fields enable future backtests to analyze why AI recommended or avoided specific trades, and to filter by AI sentiment in replay mode.

### Existing Backtest Engine
- `/backtest` already replays from `signal_log.jsonl` with the full AI-scored composite signals.
- Adaptive thresholds (F&G + VIX) are replayed from logged values.
- AVWAP gates, RSI overbought guards, and signal-collapse exits all use logged data.

---

## v2.2 — Graduated Trailing Stop (2026-03-15)

### Exit Strategy Overhaul
- Removed fixed 10% take-profit exit. Winners now ride with a graduated trailing stop that widens as profit grows:
  - `<5% profit`: 3% trail (base)
  - `5–10%`: 4% trail
  - `10–15%`: 5% trail
  - `15%+`: 6% trail (wide, let runners run)
- Hard stop (-6% from entry) remains as a safety net.
- Applied to both paper trading and backtest engine.

### Updated Notifications & Config
- BUY notifications now show graduated trail zones instead of a fixed target price.
- `/set` display shows the graduated trail table. `/set take_profit` now explains the new system.
- Adaptive config no longer adjusts `PAPER_TAKE_PROFIT_PCT` (graduated trail replaces it).

---

## v2.1 — Portfolio Value Fix, Command Menu & TP Bot Cleanup (2026-03-15)

### Bug Fixes
- `/tp` portfolio value now uses live market prices instead of cost basis (avg_price). Previously always showed ~$100,000 regardless of actual market value.
- `post_init` callback for `set_my_commands` wasn't firing in dual-bot mode. Moved command registration inline into `_run_both()`.

### Improvements
- Command menus registered for both private and group chat scopes (`BotCommandScopeAllPrivateChats` + `BotCommandScopeAllGroupChats`).
- Removed `/paper` command from TP bot — TP bot now focuses exclusively on TradersPost trading.
- Renamed all user-visible "Shadow Portfolio" references to "TP Portfolio" throughout the TP bot.
- Updated TP bot welcome, help, and command descriptions to reflect independent trading (not shadow/mirror).

---

## v2.0 — AVWAP, Backtesting & Cash Account (2026-03-15)

Major version bump reflecting three significant feature additions.

### AVWAP Integration
- Added Anchored VWAP (session-anchored to 9:30 AM ET open) as signal component 11/11 (up to 10 pts, or -5 penalty if below)
- AVWAP entry gate: during regular hours, only opens new positions when price is above AVWAP
- AVWAP stop-loss: exits position if price drops below AVWAP after having reclaimed it
- Signal scoring raised from 140 to 150 max points
- BUY notifications now show AVWAP price, % distance, points, and AVWAP stop level

### Backtesting Engine
- Persistent signal logger: every signal evaluation is appended to `signal_log.jsonl` with all 20+ indicator values, composite score, market context (F&G, VIX), and trade actions
- `/backtest` Telegram command: replays logged signal data with custom parameters (tp, sl, trail, threshold, max_pos), generates and sends a dark-themed PDF report
- Report includes: equity curve, KPIs, trade statistics, exit reason breakdown, drawdown chart, per-ticker P&L, best/worst trades
- Signal log auto-trimmed to 30 days on morning reset (~3 MB/day)
- Standalone `backtest.py` script also available for historical backtests using API data

### Cash Account
- Removed PDT (Pattern Day Trader) tracker — no longer needed with cash account
- Removed drift detection between paper and shadow portfolios
- Added T+1 settlement tracking for cash account
  - `record_settlement()` tracks unsettled funds from sells
  - `get_settled_cash()` returns settled vs. unsettled balances
  - `/settlement` command shows settlement status
- Replaced `/pdt` command with `/settlement`
- Updated `/start`, `/shadow`, and `/tp` displays to show settlement info

## v1.18 — VIX Put-Selling Alert (2026-03-14)

- Added automatic VIX put-selling alerts when VIX crosses threshold (default: 33)
- Estimates put premiums on GOOG, NVDA, AMZN, META using Black-Scholes approximation
- Suggests OTM strikes (~3% below current price) with 3-week expiry
- New `/vixalert` command to view status and configuration
- New `/vixalert check` to manually trigger a scan regardless of VIX level
- Runs automatically every scan cycle during market hours

## v1.17 — Full Channel Separation (2026-03-13)

- TradersPost commands exclusive to the TP bot — no longer registered on main bot when TP token is set
- Cleaner command separation between market analysis (main bot) and trade management (TP bot)

## v1.16 — Separate Telegram Channel (2026-03-12)

- Added support for a separate Telegram bot token for TradersPost notifications
- TP bot runs alongside main bot in the same process
- Both bots share state and paper trading engine

## v1.15 — Shadow Portfolio Tracker (2026-03-11)

- Shadow portfolio tracks what TradersPost/Robinhood should hold
- `/tpsync reset` resets shadow to match paper portfolio
- `/tpsync status` shows side-by-side comparison of paper vs. shadow positions
- `/tpedit` command for manual shadow portfolio adjustments (add, remove, shares, cash, clear)

## v1.14 — Shadow Mode (2026-03-10)

- TradersPost webhook integration for live trade mirroring
- Shadow mode toggle (`/shadow`) to enable/disable trade forwarding
- `/tp` status command showing orders sent, success rate, portfolio summary
- Webhook sends BUY and EXIT signals with ticker, action, and signal metadata

## v1.13 — Adaptive Trading (2026-03-09)

- All trading parameters auto-adjust to market conditions
- Fear & Greed Index + VIX drive adaptive rebalancing every 30 minutes
- Parameters widen in calm markets, tighten in volatile markets
- `/set` command for manual overrides that persist across deploys
- User config saved to paper_state.json

## v1.12 — Extended Hours Paper Trading (2026-03-08)

- Portfolio, positions, and sell logic now use live pre-market and after-hours prices from yfinance
- Trailing stops and take-profit evaluated against extended-hours prices
- More accurate portfolio valuation outside regular trading hours

## v1.11 — Smart Trading (2026-03-07)

- Trailing stops (3% from high-water mark)
- Adaptive thresholds based on market conditions
- Sector guards to limit exposure
- Earnings filter — avoids buying stocks reporting earnings within 2 days
- `/perf` performance dashboard with win rate, avg gain/loss, Sharpe-like metric
- `/set` command to view and change trading configuration
- Signal learning: tracks signal effectiveness over time
- Support/resistance level awareness
- `/paper chart` for intraday portfolio value visualization
- Daily P&L summary at 4:05 PM CT

## v1.10 — News Sentiment Scoring (2026-03-06)

- AI-powered news sentiment analysis (component 10/10 in signal engine, up to 15 pts)
- `/news TICK` shows sentiment scores and source timestamps
- Claude Haiku scores headlines as bullish/neutral/bearish with confidence
- Integrated into the composite trading signal

## v1.9 — Extended Hours Pricing (2026-03-05)

- Pre-market and after-hours prices from yfinance
- Dashboard and `/price` quotes show live extended session data
- Trading session detection (pre-market, regular, after-hours, closed)

## v1.8 — Dashboard Sharpness (2026-03-04)

- 220 DPI rendering for crisp charts on mobile
- Larger fonts throughout dashboard
- Sent as Telegram document (not compressed photo) for full resolution

## v1.7 — Alert Spam Fix (2026-03-03)

- 15-minute cooldown between alerts for the same ticker
- 1% escalation threshold — re-alerts only if move increases by 1%+ beyond last alert
- Startup grace period (300 seconds) prevents false alerts on boot

## v1.6 — Chart & RSI (2026-03-02)

- `/chart TICK` command using yfinance data (replaced Finnhub candles)
- `/rsi TICK` command showing RSI, Bollinger Bands, bandwidth, squeeze score
- VWAP crash fix

## v1.5 — Startup Rate Fix (2026-03-01)

- Removed duplicate scan on boot
- Eliminated 75+ Finnhub 429 errors that occurred at startup

## v1.4 — Multi-Day Trends (2026-02-28)

- 5-day SMA trend + momentum + volume component (15 pts)
- Signal component 9/10 for longer-term trend confirmation
- Daily candle data loaded from yfinance

## v1.3 — Paper Trading Boost (2026-02-27)

- Day-change MOVER alerts for significant overnight gaps
- Price history primed on startup (fills deques before first scan)
- Signal cache TTL increased to 120 seconds

## v1.2 — Crypto & Batching (2026-02-26)

- Rewritten `/crypto` command with live BTC, ETH, SOL, DOGE, XRP
- TTL caching layer for all API responses
- Batch scanning for efficient ticker processing
- Wider dashboard layout

## v1.1 — Mobile & AI Watchlist (2026-02-25)

- Compact `/help` menu optimized for mobile (64-char width)
- Mobile-friendly dashboard layout
- AI-driven watchlist rotation with conviction scores
- `/aistocks` command for AI picks

## v1.0 — Initial Release (2026-02-24)

- 30-stock scanner polling Finnhub every 60 seconds
- 3%+ spike alerts via Telegram
- $100,000 paper trading portfolio
- Automated buy/sell based on signal scoring
- Claude AI integration for stock analysis
- `/overview`, `/price`, `/analyze`, `/compare`, `/movers`, `/earnings`, `/macro`
- `/paper` portfolio management commands
- `/ask` free-form AI chat
- Morning briefing, close summary, weekly digest
