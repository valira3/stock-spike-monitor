# v8.1.0 Design — Live `partial_profit_at_1r`

**Status**: Drafted 2026-05-12, post-v8.0.1. Not yet implemented in code.
**Owner**: next session (Val + Claude).
**Backtest evidence**: full-year corpus shows stacking `partial_profit_at_1r=1` on top of v8.0.0 ATR-stop lifts FY from $+34,486 → $+44,431 (+29% incremental, +79% vs pre-v7.109 baseline). Q4-2025 jumps from $+1,989 → $+9,262. WR climbs to 59%. See `/tmp/research_r{7,8}/all.json` for the full sweep.

---

## What it does (backtest semantics, verbatim from `tools/orb_backtest.py:749-755`)

```python
# v9 lever: take partial profit at 1R (sell 50%).
if (cfg.partial_profit_at_1r and (not partial_taken)
        and fb.high >= one_r_long):
    half = remaining_shares // 2
    if half > 0:
        partial_pnl_dollars = (one_r_long - entry_price) * half
        remaining_shares -= half
        partial_taken = True
```

When the bar's high crosses the 1R level (entry + 1×risk for long, entry − 1×risk for short):
1. Sell **half** the open shares **at the 1R price** (synthetic in backtest; needs a real broker order live).
2. The remaining half stays open; the BE-arm logic still fires off the same 1R touch, so the runner has stop=entry (zero risk).
3. Subsequent target / stop / EOD logic operates on the remaining half only.

Final realized P&L:
```
pnl = (exit_price − entry_price) × remaining_shares + partial_pnl_dollars
```

---

## Why this is non-trivial live

The backtest never actually sends a partial order — it just books the partial P&L synthetically. Live, the broker holds **N** shares and must sell **N/2** of them at the 1R touch, then hold the remaining **N/2** through to the final exit.

Three integrations are required in current real-money code paths:

### 1. Broker primitive: `partial_close_shares`

Currently `broker/orders.py` and `executors/base.py:_close_position_idempotent()` only support full-position close. Need:

```python
# broker/orders.py
def partial_close_breakout(ticker: str, shares_to_close: int, price: float,
                            side: str, reason: str = "PARTIAL_1R") -> bool:
    """Submit a market sell/buy for `shares_to_close` of an open
    breakout position. Updates broker.orders state in-place:
      - pos['qty'] -= shares_to_close
      - pos['risk_dollars'] -= proportional risk
      - pos['notional'] -= proportional notional
    Idempotent on duplicate coid (same as full close path).
    Returns True if order submitted (or recognized as duplicate);
    False on no-op (insufficient shares, no client, exception).
    """
```

And `executors/base.py`:
```python
def partial_close_position_idempotent(self, client, ticker: str,
                                       shares: int, label: str,
                                       reason: str) -> bool:
    """Mirror of _close_position_idempotent but with custom qty.
    Reuses the same order-type-by-reason logic from
    broker.order_types.order_type_for_reason but uses MARKET (or
    LIMIT IOC) since the partial fires off a hot signal.
    """
```

**Risk surface**: this code path executes real-money sells. A bug here could close more or fewer shares than intended.

**Test plan**: mock `alpaca.trading.client.submit_order` and assert qty=N/2 (not N), assert the position dict is updated correctly, assert duplicate coid handling.

### 2. Engine FSM addition

`orb/exits.py:OrbPosition` gains:
```python
partial_taken: bool = False
partial_pnl_dollars: float = 0.0
original_shares: int = 0     # for forensic
```

`orb/exits.py:EXIT_PARTIAL = "partial_1r"` constant.

`orb/exits.py:evaluate()` modification — BEFORE the BE-arm and stop/target checks, on the same bar:
```python
if (cfg.partial_profit_at_1r
        and not pos.partial_taken
        and pos.shares >= 2
        and ((pos.side == "long" and bar_high >= pos.one_r)
             or (pos.side == "short" and bar_low <= pos.one_r))):
    return ExitDecision(reason=EXIT_PARTIAL, price=pos.one_r)
```

**Subtlety**: `evaluate()` becomes order-dependent. Currently order is: BE-arm → stop → target → EOD. Partial must fire FIRST on 1R touch (before be-arm flips the stop to entry, which is harmless but cleaner if explicit), and the caller must act on EXIT_PARTIAL **before** re-evaluating exit on the remaining half.

**Test plan**: 8 tests covering long/short × {partial_taken=false → fires, partial_taken=true → no-op, shares<2 → no-op, atr off → never fires, partial then later stop on remaining, partial then later target on remaining, partial then EOD on remaining, partial price = one_r exactly}.

### 3. Caller integration in `engine/scan.py` + `live_runtime`

The current exit path (`live_runtime.check_exit`, or the legacy path in `callbacks.execute_exit` for Main) calls engine.evaluate_position_exit and on a non-None ExitDecision routes to `_close_position_idempotent`. With partial:

```python
exit_dec = engine.evaluate_position_exit(pos, bar=...)
if exit_dec is None:
    return  # still open
if exit_dec.reason == EXIT_PARTIAL:
    half = pos.shares // 2
    executor.partial_close_position(ticker, shares=half, reason="PARTIAL_1R")
    pos.shares -= half
    pos.partial_pnl_dollars = (pos.one_r - pos.entry_price) * half * (1 if pos.side == "long" else -1)
    pos.partial_taken = True
    # DON'T release the risk ticket; this position is still open.
    # DO reduce risk_book reservations proportionally (see #4 below).
    return  # caller continues evaluating next bar normally
else:
    # final exit
    executor.close_position(ticker, reason=exit_dec.reason)
    engine.on_exit(pos, exit_dec)
```

### 4. Risk-book partial release

`orb/risk_book.py:RiskBook.release()` currently fully releases the ticket. Need a `release_partial(ticket, frac=0.5)` method that:
- Halves `ticket.risk_dollars` and `ticket.notional`
- Reduces `_open_risk` and `_open_notional` by the released amounts
- Keeps the ticket open (don't pop from `_open_tickets`)

This is straightforward but needs careful concurrency review since `_lock` is held during open-risk math.

### 5. Realized P&L accounting

`engine.on_exit()` currently does:
```python
if pos.side == "long":
    pnl = shares * (exit_price - entry_price)
else:
    pnl = shares * (entry_price - exit_price)
```

With partial, the final on_exit P&L must include the previously-booked partial P&L:
```python
pnl_remaining = pos.shares * (exit_price - entry_price)  # long; mirror for short
pnl = pnl_remaining + pos.partial_pnl_dollars
```

This pnl is what flows into the daily-loss-kill accounting. Important: the daily-loss-kill PnL credit happens at FINAL exit, not at partial. This matches backtest semantics.

### 6. Forensic logs

New `[V81-ORB-PARTIAL]` tag emitted on partial fill. Existing `[V79-ORB-EXIT]` continues to fire on final exit.

Dashboard `/api/state` snapshot of day_states should add `partial_taken_count: int` per portfolio.

---

## Implementation plan (estimated ~6-8 hours focused work)

| Step | Files | Tests | Risk |
|---|---|---|---|
| 1. `orb/exits.py` FSM | exits.py | 8 tests | low (pure logic) |
| 2. `orb/risk_book.py:release_partial` | risk_book.py | 4 tests | low (locked, contained) |
| 3. `orb/engine.py` snapshot + on_exit pnl path | engine.py | 3 tests | medium (touches kill-gate) |
| 4. `broker/orders.py:partial_close_breakout` | orders.py | 6 tests (mocked) | **HIGH** (real-money path) |
| 5. `executors/base.py:partial_close_position_idempotent` | base.py | 4 tests (mocked) | **HIGH** (real-money path) |
| 6. `engine/scan.py` integration + Main callbacks | scan.py + callbacks.py | 3 scenario tests | medium |
| 7. Env wiring `ORB_PARTIAL_PROFIT_AT_1R` | live_runtime.py | 1 test | low |
| 8. Goldens regen if scenario sim emits new exit kind | tests/strategy/goldens/ | — | low |
| 9. ARCHITECTURE.md + CHANGELOG | docs | — | low |

Total: ~30 new tests + 1 paper-fire observation cycle (5 trading days) before shipping live-default activation. The first 4-5 days of v8.1.0 should ship with `ORB_PARTIAL_PROFIT_AT_1R=0` default (off in env, on with explicit Railway set) so we can validate the broker path on a small subset of trades before defaulting on.

---

## Open questions for Val before implementation

1. **Partial-fill-price slippage**: backtest assumes exact `one_r` fill. Real broker may fill ~5 bps worse on a market sell during a fast move. Want a `partial_max_slippage_bps` knob (default 10) that aborts the partial if quote is too far from 1R?
2. **Short-side partial**: backtest just mirrors the long math. Shorts have borrow constraints — confirm Alpaca paper handles partial-close on shorts without issues.
3. **Re-entry semantics**: after partial fires, the position is still "open" (half size). Should `trades_today` increment now or only at final close? Backtest increments only at final close. Suggest matching.
4. **Activation policy**: ship v8.1.0 with `ORB_PARTIAL_PROFIT_AT_1R=0` default (env-off) and observe for 5 trading days, then flip in v8.1.1? Or default-on like we did with ATR? ATR was env-flip first (v8.0.0) then default-on (v8.0.1); same pattern preferred for partial.

---

## Why not now

This file exists because Claude-the-previous-session (2026-05-12, 23:00 ET) attempted to bundle partial-profit into v8.0.0 but recognized that the broker integration is high-stakes and deserves its own focused PR. v8.0.0 shipped ATR-stop only; v8.0.1 activated it; v8.1.0 (this design) is the next major lift.

Backtest evidence is strong but **a bug in `partial_close_breakout` could leak open positions across the EOD flush** — exactly the kind of risk that prefers a separate, well-tested PR over a rushed bundle.
