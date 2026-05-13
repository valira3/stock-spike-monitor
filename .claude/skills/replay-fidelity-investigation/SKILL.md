---
name: replay-fidelity-investigation
description: When live trading produces different PnL than the orb_replay backtest, follow this end-to-end recipe to diagnose. Distills the v8.3.28-v8.3.33 investigation (tick-data pull, ATR comparison, signal-timing forensics, multi-hypothesis testing) into a reproducible workflow.
---

# Replay-vs-Production fidelity investigation

When the operator says "the backtest shows X but live shows Y, why?", follow this layered diagnostic. Each step has falsifiable claims; stop the moment you have a definitive answer.

## Layer 1 — Cheap classical-backtest sweep (start here)

1. **Look up the rule history** in `docs/pl_optimization_final_report_v<N>.md` and `docs/research/r{2..N}_*.py`. Falsified theories live in the report's "Falsified" section AND in each `r<N>` script's top-of-file docstring. **Check there before re-running a dead theory.**
2. **Lever-sweep via existing GHA path** (see [CLAUDE.md "GHA-driven backtest via lever-sweep"](../../../CLAUDE.md) — anti-pattern is building a parallel `corpus_backtest.py`/workflow):
   - Add new theory env vars to `tools/orb_backtest.py:ORBConfig` (field + `_envf`/`_envs` parse + behavior in simulate loop)
   - Write a sweep in `docs/research/r<N>_<theme>.py` mirroring r2-r5
   - Dispatch the `Lever Sweep` GHA workflow with the variants from `python3 docs/research/r<N>_<theme>.py --print-variants`
   - Read results: `mcp__github__get_file_contents(owner=valira3, repo=stock-spike-monitor, path=sweeps/run-<id>/<vid>/summary.json, ref=sweep-results)`

## Layer 2 — Live-engine replay (when classical is misleading)

The classical engine in `tools/orb_backtest.py` is a separate codebase from the live engine (`orb.live_runtime`). For rules whose behavior depends on multi-fire same-side re-entries (like Rule #1 loss-lock), the classical backtest will show 0 effect even when the live engine has clear value. Use `tools/orb_replay_day.py` for live-engine replay.

Critical fidelity gaps to address in any custom replay (these were all discovered v8.3.28-v8.3.33):

- **Continuous state across days**: do NOT call `_reset_for_testing()` between days. The 5-min ATR window needs prior-day data; cold-start replays produce wider ATR → wider stops → over-large positions → notional-cap rejects.
- **5-min aggregation passed to `check_entry`**: production's `scan.py:_5m` lists (`recent_5m_highs/lows/closes`) feed `live_runtime.check_entry`. If you don't supply them, the engine falls back to OR-edge stops instead of ATR stops.
- **Entry cutoff at 15:55 ET**: production stops admitting new entries after the cutoff; replay must too.
- **VIX gate with `vix_close_d1`**: load from `data/external/vix-daily.csv` per-date.
- **Chandelier (Alarm-F) exit sentinel from `engine/alarm_f_trail.py`**: production layers this on top of v10 exits. Without it, replay positions hold too long, blocking same-(ticker, side) re-entries.

Reference: `tools/replay_corpus.py` (introduced v8.3.27 before being deleted as duplicate work) had the full pattern. Re-build minimum needed pieces inline; do not commit a new `corpus_backtest.py`.

## Layer 3 — Tick-data pull + ATR fidelity check (sometimes unnecessary)

**Hypothesis**: "production's ATR is computed from streaming ticks while replay uses 1-min OHLC."

**Verdict (proven in v8.3.33)**: This is **mathematically false** for a properly-filtered comparison. Alpaca's 1-min OHLC bars are SIP-consolidated tape-eligible aggregates. ATR(tick-derived 5m bars) = ATR(1m-bar-derived 5m bars) EXACTLY when ticks are filtered to last-sale-eligible conditions (drop `I` odd-lot, `Z` sold-out-of-sequence, `T` Form T, `U` extended hours, `9` cross, etc.).

**Don't re-pull tick data for ATR fidelity**. If you must, see `tools/fetch_alpaca_ticks.py` (pulls to R2; gzip JSONL) and `tools/compare_atr.py` (with `is_tape_eligible` filter).

## Layer 4 — Signal-timing forensics

When replay enters a ticker much earlier than production fires (e.g., replay 10:04 ET, production 11:00 ET), check **signal magnitude**:

```python
# For each ticker, compute 5-min closes past OR_HIGH (long) / OR_LOW (short)
# and the magnitude in bps. Compare to production's actual entry time.
```

Reference: see the AMZN/GOOG case from 2026-05-12 (in v8.3.33 conversation log). Production fired AMZN SHORT on the **3rd** consecutive bar past OR_LOW (-23bps), not the first marginal break (-4.5bps).

**This is most likely a `scan.py` startup-latency / cadence issue**, not a strategy-code difference. The replay sees every 5-min signal; production may miss the first few if the scan loop hadn't started yet when OR closed.

## Layer 5 — Equity / position-sizing forensics

Production's `RiskBook.session_start_equity` may be **stale** (we observed $20K when actual equity was $100K). This makes production's `notional_cap = 75% × $20K = $15K` per trade. Match this in replay via `--equity` arg.

The `notional_cap` rule in `orb/engine.py:try_enter` is:
```python
shares_cap = max(1, int(max_notional / entry_price))
shares = min(shares, shares_cap)  # <-- shrinks shares to fit cap
```

Per-trade cap SHRINKS; concurrent cap REJECTS.

## Layer 6 — Production state telemetry (when desperate)

The `snapshots-live` branch has `data/snapshots/latest.json` updated every 10 min during RTH. It carries `/api/state.v10.or_windows`, `risk_books`, `day_states` etc. **Compare to your replay's same fields at the same time**:

```
mcp__github__get_file_contents(
    owner="valira3", repo="stock-spike-monitor",
    path="data/snapshots/latest.json", ref="snapshots-live"
)
```

If the snapshot you need is outside the cron window, **don't dispatch the snapshot workflow yourself** — the operator does that. From the sandbox you cannot reach `tradegenius.up.railway.app` directly (firewall: "Host not in allowlist").

## Decision matrix at investigation end

| Verdict | Action |
|---|---|
| Single root cause identified + cheap to fix | Ship a code PR. Use v8.3.x version bump pattern. |
| Multi-factor / unfixable | Document in the v<N+1> P&L report. **Recommend the rule sweep DELTA**, not absolute backtest numbers. |
| Investigation exhausted | **Stop**. Don't keep pulling tick data hoping for a different answer. Ship the relative-improvement-validated config. |

## Falsified hypotheses (do not retry)

- Tick-level ATR is tighter than 1m-bar ATR — **mathematically equivalent with tape filter**
- Production has a 2-consecutive-bars-past-OR confirmation filter — **not in code**; observed pattern was coincidental
- Hidden signal-magnitude threshold in `detect_breakout` — **doesn't exist**
- Filtering tick data via condition codes makes ATR tighter — **makes it identical to 1m-bar ATR**, not tighter

## Anti-patterns to avoid

- Building a parallel `tools/corpus_backtest.py` or `.github/workflows/corpus-backtest.yml`. The `lever-sweep.yml` + `tools/orb_backtest.py` + `docs/research/r{N}_*.py` chain is the production research path.
- Polling GitHub API unauthenticated from inside a `Monitor` shell — hits 60/hr rate limit fast. Use `mcp__github__*` tools instead (authenticated).
- Re-running a tick pull when only the validation logic changed — dispatch `Tick-vs-Bar ATR validation` workflow directly with `workflow_dispatch`.
- Polling for results without changing `captured_at_utc` — a stale summary already on the branch will satisfy a naive "does the file exist" check. Always validate timestamp.
