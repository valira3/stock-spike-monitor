# v7.0.0 — Per-portfolio independent books

## Why a major version bump

This is the largest architectural change since the v5.x → v6.x consolidation
(eye_of_tiger split). It rewrites the assumption that there's a single global
paper book underneath every executor, and that every signal lifecycle (entry
sizing, fill recording, stops, sentinels, cooldowns, day-P&L, daily-loss-halt)
is keyed by `(ticker, side)` against that one book.

After v7.0.0, the engine runs **N independent books** in parallel — one per
portfolio (Main = engine paper, Val = Alpaca PA3X1P36WR2V, Gene = TBD) — and
every piece of per-position / per-day state is keyed by `(portfolio_id, ticker,
side)`. Signals fan out through a config-gated layer. The dashboard mirrors
each book in its own tab plus a per-portfolio status strip under the tab
switcher.

Plus: AON entries, quiet messaging, and the chandelier reset bug fix.

## Drivers (real incidents from 2026-05-06)

1. **AAPL fill divergence** — engine sized 34 shares, Val's broker filled 6.
   Cause: IOC LIMIT without `all_or_none`, plus quote shifted up mid-flight.
   Engine kept its sentinels keyed off 34-share intent while broker held 6.
2. **ORCL slippage divergence** — engine SHORT @ 183.785, Val's broker @ 183.68.
   V651 deep-stop fired off the engine entry at -0.80%; Val's account took the
   cover at a 12% worse round-trip ($-44.55 vs engine's $-39.82) because the
   deep-stop reference was the wrong entry baseline.
3. **AVGO chandelier carry-over** — third re-entry of the day inherited a
   stale `peak_close=418.18` from the previous leg. The fresh SHORT @ $419.57
   had its chandelier trail snap to $419.56 (entry-1¢) within 3 minutes. Stop
   was breached almost immediately; only V644-MIN-HOLD prevented an instant
   stop-out at a loss. Position is currently bleeding with both A_STOP_PRICE
   and F_EXIT firing every tick, gated only by min-hold.
4. **TRAIL pill UX confusion** — dashboard shows green TRAIL pill even when
   the trail is breached and the position is being held open ONLY by min-hold.
5. **Noisy ZEROFILL messaging** — every entry today double-pinged Telegram
   (⚠️ "unfilled, reconciling…" then ✅ "grafted late fill"). 4 of 4 grafted
   clean. Alarm fatigue on a non-event.
6. **Day-P&L Main vs Val mismatch** — Main shows +$72, Val shows -$22. Two
   different books, two different ledgers, no way to compare apples-to-apples.

## Scope

### A — Per-portfolio books (the structural refactor)

3 independent books: `main` (engine paper), `val` (Alpaca PA3X1P36WR2V),
`gene` (Alpaca acct TBD). Each owns:

- positions (longs + shorts)
- entry baseline = **broker fill price** for val/gene (not engine intent)
- sentinel state: protective stops, V644 hold clock, V651 deep-stop entry
  reference, chandelier high-water (`peak_close` per book, per side)
- cooldown registry (post-loss, post-exit, V61113-EXIT-CD)
- daily-loss-limit + trading_halted flag
- realized_pnl ledger + day_pnl computation
- error stream (Alpaca errors per book, engine errors on main)

Per-portfolio config:
- `enabled` (master switch)
- `tickers` (subset of universe; default = all)
- `sides_allowed` ({LONG, SHORT}; default = both)
- `earnings_watcher_enabled` (default = main: true; val/gene: false until validated)
- `daily_loss_limit_dollars`
- `dollars_per_entry` — sizing per book (Val + Main: $10k = ~10% of $100k; not
  buying-power-based)

**Sizing rule (per Val 2026-05-06):** size off portfolio equity (~$100k each),
NOT broker buying power. Val and Gene paper accounts each have $387k+ BP from
4x margin; ignore that. Each book sizes from its own $100k equity floor.

### B — Signal fanout layer

Engine emits one ENTRY decision per `(ticker, side, ts)` as today. New layer
between signal-generation and order-placement:

```
SignalEmitted(ticker, side, intended_qty, intended_price)
    │
    └── for each portfolio:
          if not portfolio.enabled: skip (silent)
          if ticker not in portfolio.tickers: skip
          if side not in portfolio.sides_allowed: skip
          if portfolio.book.in_cooldown(ticker, side): skip
          if portfolio.book.has_position(ticker): skip
          if portfolio.book.daily_halted(): skip

          qty = portfolio.size_for(ticker, intended_price)  # per-book sizing
          fill = portfolio.executor.place(ticker, side, qty, price=intended_price)
          portfolio.book.record_entry(ticker, side, fill_price=fill.price, ...)
```

Critically: `record_entry` uses the **broker's actual fill price**, not the
engine's intended. This kills the ORCL slippage class of bug — Val's deep-stop
keys off Val's fill, Main's off Main's.

### C — All-or-nothing entries (kills AAPL-style partials)

Every IOC LIMIT entry order becomes all-or-nothing. Either full qty fills or
zero crosses. Two implementation paths depending on Alpaca paper API support:

- **Native AON** (preferred): pass `all_or_none=True` on `LimitOrderRequest`.
  Probe at boot — if accepted, use it.
- **Software AON** (fallback): if SDK rejects, detect `0 < filled < requested`
  and immediately close the partial back to flat with a market order. Single
  ⚠️ Telegram explaining "partial rejected, closed back to flat."

Either way, the engine never sees a partial position with mismatched intent.

**Status check at boot** logs `[V700-AON] mode=native` or `[V700-AON] mode=software`.

### D — Quiet ZEROFILL messaging

Replace the current ⚠️ "unfilled, reconciling…" / ✅ "grafted late fill" pair
with a single message per entry:

| Outcome | Today | After v7.0.0 |
|---|---|---|
| Synchronous fill (qty == requested) | `✅ AAPL BUY 34 @ $285.07` | unchanged |
| IOC ack=0, reconcile grafts late fill | ⚠️ + ✅ | single `✅ AAPL BUY 34 @ $285.07 (late fill)` |
| IOC ack=0, reconcile finds broker flat | ⚠️ + ✅ | single `⚠️ AAPL LONG rejected — limit did not cross (no broker fill)` |
| IOC ack=0, reconcile raises | ⚠️ then nothing | single `⚠️ AAPL LONG reconcile inconclusive — verify on broker` |
| Partial fill (software-AON path) | ⚠️ partial then ✅ closed | single `⚠️ AAPL LONG partial rejected (6/34) — closed back to flat` |

Logs (WARNING-level forensics) unchanged. Only Telegram surface changes.

⚠️ glyph reserved for "the order didn't make it into your book at the
requested size." Late-fill grafting becomes ✅ with `(late fill)` suffix.

### E — Chandelier reset on entry boundary (AVGO bug fix)

**Bug:** when a position is covered/closed and a fresh entry opens on the
same `(ticker, side)`, the chandelier trail's `peak_close` reference (and
`atr` baseline, and stage tracking) is currently inherited from the prior
leg. Result on AVGO 2026-05-06: 3rd entry @ $419.57 had `peak_close=$418.18`
locked from the previous cover, so chandelier trail snapped to $419.56
(entry-1¢) within 3 minutes — no breathing room, position bled immediately.

**Fix:** on `record_entry` (book-level), reset all chandelier state for that
`(ticker, side)`:
- `peak_close = entry_price` (start fresh; trail can only ratchet from here)
- `chandelier_stage = 1` (re-enter from stage 1, not whatever stage the prior
  leg ended on)
- `atr_baseline = None` (recompute from this entry's bar window)

Add `[V700-CHANDELIER-RESET] AVGO SHORT entry#3 \u2014 peak_close 418.18 → 419.57,
stage 3 → 1` log line so it's auditable.

### E.5 — Re-entry HOD/LOD ratchet (Eugene's rule)

**Bug:** when a `(ticker, side)` position is opened and closed multiple times
in the same session, the next Entry-1 only requires a fresh HOD/LOD relative
to the OR window — not relative to the **prior leg's extreme**. This permits
re-entry stacking inside the same price band. Today (2026-05-06):

- AVGO SHORT: 6 separate `entry_num=1` events between 13:41:43 and 13:47:04 ET
  at fills 431.72 → 431.26 → 431.05 → 430.91 → 430.19 → 429.36. Each was a
  fresh LOD vs. OR low, but the ratchet was sliding only $0.10–$0.50 per leg.
  Six positions in 6 minutes inside a $2.36 band.
- NFLX LONG (single leg today, but same shape risk): entered 14:31:09 ET at
  $88.40, stopped out 14:50:29 at $87.95 for -$50.56. If a second leg had been
  permitted on a fresh-but-marginal HOD shortly after the stop-out, it would
  have averaged into a worse fill on the same downtrend.

**Eugene's rule (verbatim, 2026-05-06 09:51 CDT):**

> 2nd and 3rd strikes have to be on a new HOD (long) or LOD (short).

**Fix:** introduce a per-`(portfolio_id, ticker, side)` **session ratchet**
that tracks the most-favorable extreme across ALL prior legs of the day:

- `prior_legs_max_high_long: dict[(portfolio_id, ticker), float]` — highest
  intra-leg high seen across all closed long legs today.
- `prior_legs_min_low_short: dict[(portfolio_id, ticker), float]` — lowest
  intra-leg low seen across all closed short legs today.
- On each leg close (`record_exit`), update the ratchet with that leg's high
  (long) or low (short).

**Gate change** in `evaluate_entry_1`:

When `prior_legs_max_high_long[(pid, ticker)]` exists for a LONG attempt, the
fresh-extreme requirement strengthens from "new HOD vs OR" to "new HOD beyond
prior_legs_max_high_long." Same for shorts vs `prior_legs_min_low_short`.

New rejection reason on Entry 1 when the ratchet bites:
```
[SKIP] ticker=AVGO reason=V5100_ENTRY1:re_entry_ratchet ts=... \
       detail=current_low=430.19 prior_min_low=430.91 (need < prior min)
```

This lets the FIRST leg fire on standard `is_nhod_or_nlod`. Re-entries 2, 3, ...
N must each push the day's extreme strictly past every prior leg's extreme,
not just past the OR boundary.

**Edge cases:**
- Different portfolio books have separate ratchets (Val and Main can each take
  Leg 1; the ratchet is per-book).
- Entry 2 (scale-in) is unaffected — it already enforces
  `fresh_nhod_or_nlod` past Entry-1 HWM and is intra-leg, not inter-leg.
- The ratchet resets at EOD (clears with `paper_state` daily reset).

**Test:** `tests/test_v700_re_entry_ratchet.py`
- Open + close LONG leg 1 with intra-leg high $431.72.
- Attempt Entry 1 again with current_high=$431.20 (lower than ratchet) →
  REJECT with `re_entry_ratchet`.
- Attempt Entry 1 with current_high=$432.00 (higher) → PASS.
- Repeat for SHORT.
- Verify ratchet is per-(portfolio_id, ticker): book A's ratchet does not
  block book B's first leg.

### F — Dashboard polish: TRAIL pill state-aware

**Bug:** TRAIL pill shows green even when the trail is breached and only
V644-MIN-HOLD is keeping the position open.

**Fix:** three states:
- `TRAIL · armed` (green) — protective stop set, mark hasn't crossed it.
- `TRAIL · breached / hold` (amber) — mark crossed stop but min-hold gate is
  blocking exit. Show countdown: "exit in 4m 37s if still breached."
- `TRAIL · breached / firing` (red) — mark crossed stop AND min-hold expired
  AND deep-stop hasn't fired yet. Position is in cover queue.

Computed server-side from existing `effective_stop`, `mark`,
`v644_hold_seconds`. Pure dashboard change.

### G — Per-portfolio dashboard strip

New thin row under the tab switcher, sourced from `portfolios[active_tab].strip`:

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Main 🟢 Paper · Val 🟢 P · Gene ✕ · Lifecycle —    ← tab switcher (existing) │
├──────────────────────────────────────────────────────────────────────────────┤
│ ⏱ 2 cooldowns (L:1 S:1) · ⚠ 0 errors · ▣ 3 open · -$22 day · ✓ active        │  ← NEW
├──────────────────────────────────────────────────────────────────────────────┤
│ ... rest of tab content (KPIs, positions, weather, permit matrix) ...        │
└──────────────────────────────────────────────────────────────────────────────┘
```

Chips: cooldowns (this book), errors (this book), positions, day P&L, state
(`active` / `halted_daily_loss` / `paused` / `disabled`). Cooldowns + errors
are tappable popovers filtered to that book.

Lifecycle tab gets cross-book summary view.

What stays global: weather/PERMIT matrix, TO countdown, LIVE pill (all
market-wide, identical for all books).

### H — `/api/state` shape extension

```json
{
  "portfolios": {
    "main": {
      "equity": ..., "day_pnl": ..., "positions": [...], "trades_today": [...],
      "strip": { "cooldowns": {...}, "errors": {...}, "state": "active" }
    },
    "val":  { ..., "strip": {...} },
    "gene": { ..., "strip": {...} }
  },
  "portfolio": { ... }   // BACKCOMPAT: alias for portfolios.main, removed v7.1.0
}
```

## Migration phases (ship in this order on the same branch)

1. **Phase 1 — extract `PortfolioBook`** (mechanical refactor, 1 book, no
   behavior change).
2. **Phase 2 — re-key cooldowns + sentinel state + chandelier** by
   `(portfolio_id, ticker, side)`. Still 1 book. Includes E (chandelier reset).
3. **Phase 2.5 — re-entry HOD/LOD ratchet** (E.5). Adds the per-book
   `prior_legs_max_high_long` / `prior_legs_min_low_short` ratchet on the
   `PortfolioBook`, updates `record_exit` to refresh it, and tightens
   `evaluate_entry_1` to require breaking past the ratchet (not just OR) on
   any leg after the first. Behavior change — ships without flag.
4. **Phase 3 — register Main + Val + Gene**. Migrate existing
   `paper_state.json` → `paper_state_main.json` (additive). No flag — books
   are live from this commit; signal flow stays single-book until Phase 4.
5. **Phase 4 — fanout layer + per-book sizing + per-book config**. Includes
   broker-fill-price as entry baseline.
6. **Phase 5 — AON entries** (C) + **quiet messaging** (D). Touches the same
   entry path, ship together.
7. **Phase 6 — `/api/state` portfolios map + per-portfolio strip + TRAIL state
   polish** (F + G + H).

## Tests (new files)

- `tests/test_v700_book_isolation.py` — different fills on same ticker → different
  deep-stops fire at different marks; cooldown on book A doesn't gate book B;
  daily-halt on A doesn't halt B.
- `tests/test_v700_fanout.py` — ticker filter, side filter, per-book sizing.
- `tests/test_v700_aon.py` — native AON path + software AON fallback +
  partial-rejected close-to-flat.
- `tests/test_v700_zerofill_messaging.py` — late-fill graft → 1 ✅; flat reject
  → 1 ⚠️; reconcile-raises → 1 ⚠️; full fill → 1 ✅; partial → 1 ⚠️ closed.
- `tests/test_v700_chandelier_reset.py` — same `(ticker, side)` re-entry resets
  peak_close to new entry, stage to 1, atr_baseline to None.
- `tests/test_v700_re_entry_ratchet.py` — leg 1 closes with intra-leg high X;
  leg 2 attempt below X rejects with `re_entry_ratchet`; leg 2 above X passes.
  Same for shorts (intra-leg low). Ratchet is per-(portfolio_id, ticker):
  book A's ratchet does not block book B's first leg.
- `tests/test_v700_dashboard_shape.py` — `/api/state` returns `portfolios` map
  with all 3 IDs; legacy `portfolio` matches `portfolios.main`; each book has
  `strip` sub-object; TRAIL state computed from stop/mark/hold.

## Rollout

- Single PR, one branch (`v7.0.0`).
- Phases 1-2 land first as commits with zero behavior change. Smoke test +
  1-day replay between each.
- Phase 3 onward gated by `PER_PORTFOLIO_BOOKS_ENABLED=false` default.
  AON+messaging (Phase 5) ship without the flag — they're correctness fixes
  for the existing single-book path too.
- Migration step in deploy: copy `paper_state.json` → `paper_state_main.json`
  on first boot if the latter is missing.
- Flip `PER_PORTFOLIO_BOOKS_ENABLED=true` once Val confirms the dashboard
  shows three books with sane state and Val's broker fills are recording the
  right entry baseline.
- Rollback: flip flag false; engine falls back to single-book mode reading
  `paper_state_main.json`.

## Estimated effort

- Phase 1: 1 day
- Phase 2: 1.5 days (cooldowns + sentinels + chandelier reset)
- Phase 3: 2 days (multi-book registration + state-file migration + 1-day verify)
- Phase 4: 1 day (fanout + sizing + broker-fill baseline)
- Phase 5: 0.5 day (AON probe + entry path rewrite + messaging)
- Phase 6: 1 day (dashboard JSON + strip UI + TRAIL state polish + iPhone smoke)

Total: ~7 working days end-to-end.

## Open questions / deferred to v7.1.0

1. **Cross-book risk caps** (total drawdown limit across all 3 books).
2. **A/B backtest mode** (one signal stream, two books with different configs,
   side-by-side report).
3. **Sizing-divergence message** (when book intent diverges from engine intent
   by >10%, surface it on Telegram). Out of v7.0.0 scope; AON enforcement makes
   it irrelevant for now.
4. **Drop legacy `portfolio` alias** in `/api/state`.
5. **Broker-fill slippage attribution** report (engine intent vs Val fill vs
   Gene fill, dollar attribution per leg).
