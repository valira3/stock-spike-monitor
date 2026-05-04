# v6.13.0 — Cancel-first-then-enter (broker order plumbing)

**Status:** SPEC. No code changes in this version.
**Owner:** Val
**Targets:** v6.13.0 (alongside the 09:50 long-lockout fix)

## Background

On 2026-05-04 at 13:56:54 UTC, an Alpaca paper order for GOOG was rejected
with `error 40310000 "potential wash trade detected. use complex orders"`
referencing existing order id `d93911e6`. The bot had just covered a SHORT
on `sentinel_a_stop_price` at 13:56:52.443, then submitted a new SHORT
entry at 13:56:53.658 — a ~1.2 second gap. The protective stop from the
first position was still working at the broker when the new market entry
hit.

Root cause: when the sentinel triggers an exit, the bot submits a fresh
market/cover order at Alpaca to close the position, but does NOT cancel
the original protective stop order first. The original stop sits as
"working" in Alpaca's book until the broker itself routes the trigger
fill, which can take hundreds of ms to several seconds. During that
window, any opposite-side order on the same symbol trips the wash-trade
detector.

v6.11.13 ships a band-aid: a 10 s same-ticker post-exit cooldown that
delays re-entries long enough for the broker to reconcile. That fixes the
common case but leaves two gaps:

1. The cooldown delays legitimate re-entries by 10 s even when the
   protective stop was already filled (most cases).
2. The cooldown does NOT prevent the same race when an exit fires from a
   non-stop path (e.g. EOD flush, manual close, trailing-stop override) —
   if the protective stop is still in the book, a same-second entry on the
   reverse side can still race.

## Goal

Replace the cooldown band-aid with a real broker-side ordering protocol:
**before placing any new entry on a ticker that has an open protective
stop, cancel that stop and wait for the cancel ack.**

## Design

### Phase 1 — Track open broker orders by symbol

Add a per-symbol map `_open_broker_orders: dict[str, list[OrderRef]]`
populated whenever the broker confirms an order placement
(`paper_submit_order` / `submit_order` returns an order id). Entries are
removed when:

- The order fills (status → `filled`)
- The order is cancelled (status → `canceled`)
- The order is rejected (status → `rejected`)

Hook the existing Alpaca trade-update websocket handler to keep this map
fresh. Replay path uses a synthetic version that mirrors the same lifecycle.

### Phase 2 — Cancel-first guard at entry

In `broker/orders.execute_breakout` (current entry callsite), before
constructing the new order:

```python
# v6.13.0 — cancel-first guard. Before entering on a ticker, cancel any
# open opposite-side stop or limit order so Alpaca's wash-trade detector
# does not reject the new entry. Wait up to CANCEL_ACK_TIMEOUT_MS for the
# cancel to be acknowledged.
open_orders = tg._open_broker_orders.get(ticker, [])
opposing = [o for o in open_orders if o.side != _side_label and o.is_protective]
for o in opposing:
    tg.broker.cancel_order(o.id)
if opposing:
    if not tg._wait_for_cancel_acks([o.id for o in opposing], timeout_ms=CANCEL_ACK_TIMEOUT_MS):
        tg.logger.warning(
            "[V6130-CANCEL-FIRST] %s: cancel ack timeout for %d orders, skipping entry",
            ticker, len(opposing),
        )
        return
```

Default `CANCEL_ACK_TIMEOUT_MS = 1500`.

### Phase 3 — Promote the v6.11.13 cooldown to env-tunable, default OFF

When v6.13.0 ships and the cancel-first guard is verified in prod for
≥1 full RTH session with zero 40310000 rejects, set
`POST_EXIT_SAME_TICKER_COOLDOWN_SEC=0` in production env. Keep the code
in place as a guardrail in case the cancel-first path fails — code path
is ~30 lines and the env-var off makes it a free no-op.

### Phase 4 — Replay path

`backtest/replay_v511_full` already simulates instant fills on the bar
boundary. The cancel-first guard should be a no-op in replay (no real
broker, no working orders to cancel). Confirm via smoke test that
v6.13.0 produces byte-identical replay output to v6.12.0 with the
guard's env disabled.

## Affected files

| File | Change |
|---|---|
| `broker/orders.py` | Add cancel-first guard in `execute_breakout` |
| `trade_genius.py` | Add `_open_broker_orders` registry + helpers |
| `broker/alpaca_ws.py` | Hook trade-updates to maintain registry |
| `backtest/replay_v511_full.py` | Synthetic registry mirror for replay parity |
| `eye_of_tiger.py` | Add `CANCEL_ACK_TIMEOUT_MS` env var |
| `bot_version.py` / `trade_genius.py` | Bump to 6.13.0 |
| `ARCHITECTURE.md` | Section on broker order lifecycle |
| `trade_genius_algo.pdf` | Update — v6.13.0 is a minor, PDF is mandatory |

## Validation plan

1. **Replay parity**: 84-day SIP corpus, v6.13.0 vs v6.12.0 with cancel-first
   gate disabled. Total P/L delta must be `<= $0.01`.
2. **Replay with gate enabled** (synthetic): inject a few simulated working
   orders into the registry, verify cancel-then-wait path fires.
3. **Paper smoke (1 day)**: deploy to Railway, watch for
   `[V6130-CANCEL-FIRST]` logs, confirm zero 40310000 rejects in 24 h.
4. **Promote**: leave v6.11.13 cooldown env at 10 s for 1 week, then drop
   to 0 once stable.

## Open questions

- Does Alpaca's `cancel_order` accept a wait-for-ack flag, or do we need
  to poll order status? (Investigate at implementation time.)
- For the dashboard, do we want a "broker reconciliation lag" stat
  surfaced? Could help diagnose if Alpaca's behaviour changes.
- Should the cancel-first path also fire on entries for tickers with
  open same-side limit orders (e.g. partial fills)? Probably yes —
  worth a sentinel test.

## Followups linked

- 09:50 long lockout (REDUCE_NEG_DAYS.md Fix #1) — unrelated but ships in
  the same v6.13.0 minor. Wire it as a hard pre-permit time gate (NOT
  inside `evaluate_global_permit`) so the local-weather override cannot
  punch through; see `/home/user/workspace/v6_12_0_sweep/FOLLOWUPS.md`.
