# Dashboard keystone — v7.39.0 (2026-05-10)

This directory is a frozen snapshot of the production dashboard at the
v7.39.0 release. It is the **reference baseline** that the redesign
work (PRs 32–41, v7.40.0 → v7.48.0) is measured against. The files
here MUST NOT be edited — they are an immutable artifact for
operator comparison and reviewer context.

## Contents

| File | What it is |
|---|---|
| `index.html` | Production dashboard HTML at v7.39.0 |
| `app.css` | Production CSS at v7.39.0 (~2030 lines) |
| `app.js` | Production JS at v7.39.0 (~5350 lines) |

## How to view

```
cd docs/dashboard_keystone_v7_39
python -m http.server 8080
# open http://localhost:8080
```

Note that the keystone is intentionally divorced from the backend —
opening `index.html` against a live `/api/state` endpoint won't work
because the JS expects same-origin. View it for visual reference, not
as a live-running dashboard.

## What's in this snapshot

The v7.39.0 dashboard surfaces:
- Brand row + clock + health pill + live pulse pill
- 4-up KPI grid (Equity / Day P&L / Open / Session)
- v10 day-status banner (mode pill + VIX + day-status + trades + risk)
- v10 ticker matrix card (per-portfolio FSM state table)
- v10 projection card (CAGR / Sharpe / max-DD)
- Legacy permit matrix card (hidden under `body.v10-live`)
- Legacy weather check banner (hidden under `body.v10-live`)
- Open positions card
- Today's trades card
- Earnings watcher card (collapsible)
- Lifecycle tab (per-position event timeline)
- Val / Gene executor panels (poll `/api/executor/{name}`)
- Market index strip (SPY / QQQ / VIX marquee)

## Proposed v7.40+ changes (referenced by `docs/dashboard_redesign_v2.html`)

1. **Kill-switch banner** — visible when `scan_paused` /
   `trading_halted` / `daily_kill_triggered` / `ORB_LIVE_MODE=0`.
2. **Position progress bars** — stop / 1R / target visual axis.
3. **Risk + trades + daily-kill gauges** — replace raw numbers with
   visual bars on the v10 day-status banner.
4. **Broker P&L row** with delta chip vs paper.
5. **v10 ticker matrix mobile card stack** — replace horizontal-scroll
   table at < 720 px.
6. **Activity feed** — surfaces `near_misses` + `[V79-ORB-*]` log tail.
7. **Hero zone reorder** — move open positions above the KPI row.
8. **Index strip demoted** — small footer chip row, not top marquee.
9. **Per-portfolio parity** — all the above must render in Main / Val
   / Gene panels via the existing tab system.
10. **Icon + visual-polish pass** — consistent iconography across all
    cards.

All proposals will be implemented in PRs 33-41 against the live
`dashboard_static/` files. This keystone snapshot stays untouched as
the comparison baseline.
