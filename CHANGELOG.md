# Changelog

All notable changes to Stock Spike Monitor.

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
