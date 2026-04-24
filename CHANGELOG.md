# Changelog

All notable changes to TradeGenius (formerly Stock Spike Monitor, renamed in v3.5.1).

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
