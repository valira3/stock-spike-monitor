# simulator/ — swap-in test mode for the live trading bot

A complete in-process mock of every external service the bot touches, plus a
deterministic driver that feeds bars and observes behavior. The v10 ORB
production decision pipeline (`orb.live_runtime` + `engine.scan` + `broker.*`
+ `executors.*`) runs **unchanged**; the simulator just intercepts at the
network boundary.

Built 2026-05-19 after the v10.0.1 legacy purge. Sister surface to
`tools/orb_session_sim.py` (which drives the runtime with synthetic bars
for rule-level unit tests) — this one targets the bigger surface: full
HTTP-mocked external services, corpus-driven replay, telegram capture,
broker order book, time-warp clock.

---

## What it mocks

| Service | Patched at | Behavior |
|---|---|---|
| **Alpaca TradingClient** | `alpaca.trading.client.TradingClient` (module attr) | Accepts limit/market orders, fills immediately, tracks positions, realized P&L |
| **Alpaca historical data** | `alpaca.data.historical.StockHistoricalDataClient` | `get_stock_bars()` serves from the simulator bar feeder |
| **FMP REST** | `urllib.request.urlopen` for `financialmodelingprep.com` | `/quote`, `/earning_calendar`, `/profile` endpoints |
| **Yahoo Finance** | `urllib.request.urlopen` for `query{1,2}.finance.yahoo.com` | `/v8/finance/chart/<TICKER>` endpoint |
| **Telegram bot API** | `urllib.request.urlopen` for `api.telegram.org` | `sendMessage` / `sendPhoto` — captures every send, returns `{"ok": true}` |
| **Railway GraphQL** | `urllib.request.urlopen` for `backboard.railway.app` | No-op success |

The simulator also patches `datetime.now` / `time.time` / `time.monotonic`
via `SimulatedClock` so the bot's notion of "now" advances on the runner's
schedule — not the wall clock.

---

## CLI usage

```bash
# List built-in scenarios
python -m simulator.runner --list

# Run a synthetic scenario
python -m simulator.runner --scenario golden_orb_long --verbose

# Replay a historical day from the bar corpus
python -m simulator.runner --replay 2026-05-15 --tickers AAPL,MSFT,NVDA
```

Output:

```
===== Scenario Summary =====
name:           golden_orb_long
entries:        0
exits:          0
telegram sends: 0
alpaca orders:  0
fmp calls:      0
yahoo calls:    0
open positions: 0
--- Expectations: PASS ---
```

Verbose mode prints every entry/exit fire with bucket time and price.

---

## Programmatic usage

```python
from simulator import SimulatorRunner

# Synthetic scenario
runner = SimulatorRunner.from_scenario("golden_orb_long", verbose=True)
state = runner.run()

assert len(state["entries"]) <= 1
assert state["alpaca_realized_pl"].get("AAPL", 0) >= 0
assert all(s["chat_id"] for s in state["telegram_sends"])

# Historical replay
runner = SimulatorRunner.from_replay("2026-05-15", ["AAPL", "MSFT"])
state = runner.run()
```

The `state` dict is the single source of truth for assertions. Keys:

- `entries`: list of `{ticker, side, bucket, price, stop, target, shares}`
- `exits`: list of `{ticker, reason, bucket, price}`
- `alpaca_orders`: list of every order submitted
- `alpaca_positions`: dict of open positions (symbol → `_MockPosition`)
- `alpaca_realized_pl`: dict of realized P&L by symbol
- `telegram_sends`: list of every Telegram send (method, chat_id, text)
- `fmp_calls`: list of every FMP URL the bot hit
- `yahoo_calls`: list of every Yahoo URL the bot hit
- `log`: list of internal runner log lines

---

## Adding a scenario

Edit `simulator/scenarios.py` and add an entry to `SCENARIOS`:

```python
SCENARIOS["my_scenario"] = {
    "name": "my_scenario",
    "description": "What this scenario tests in one sentence.",
    "date": "2026-05-15",
    "universe": ["AAPL"],
    "bars": my_bars_builder,  # callable returning {ticker: [bar_dict, ...]}
    "config_overrides": {
        "ORB_LIVE_MODE": "1",
        "ORB_OR_MINUTES": "30",
        # ... any env vars
    },
    "expected": {
        "min_entries": 0, "max_entries": 1,
        "telegram_sends_max": 10,
    },
}
```

Use `simulator.bar_feeder.make_bar(date, hh, mm, open_, high, low, close, volume)`
to assemble bars. Built-in helpers cover the keystone test set
(golden_orb_long, gap_skip, range_too_narrow).

---

## Adding a corpus day

The bar corpus lives at `data/YYYY-MM-DD/<TICKER>.jsonl` — same layout as
production's `/data/bars/`. Most days from 2025-01-02 through 2026-05-15
are available locally (gitignored). To replay any of them:

```bash
python -m simulator.runner --replay 2026-04-22 --tickers AAPL,MSFT,NVDA,SPY,QQQ
```

---

## Architecture

```
simulator/
├── __init__.py             # exports SimulatorRunner
├── runner.py               # SimulatorRunner -- orchestrates a scenario
├── clock.py                # SimulatedClock -- frozen datetime + time
├── bar_feeder.py           # BarFeeder -- reads JSONL corpus, serves bars
├── scenarios.py            # SCENARIOS registry + bar builders
├── mocks/
│   ├── __init__.py         # install_all / uninstall_all
│   ├── alpaca.py           # MockTradingClient + MockStockHistoricalDataClient
│   ├── fmp.py              # urlopen interceptor for FMP
│   ├── yahoo.py            # urlopen interceptor for Yahoo
│   └── telegram.py         # urlopen interceptor for Telegram bot API
├── corpus/                 # placeholder for synthetic-corpus tooling
└── README.md
```

### Design choices

- **In-process, not out-of-process.** The mocks live in the same Python
  process as the bot. No fake REST servers, no port management. Patching
  at `urllib.request.urlopen` covers FMP/Yahoo/Telegram/Railway in one
  hook.
- **Patch the alpaca-py client classes, not the bot.** The bot imports
  `from alpaca.trading.client import TradingClient` inside helper
  functions; module-level swap takes effect on next import.
- **Time-warp via `time.sleep = lambda: None`.** Combined with the
  `SimulatedClock`, this lets a full trading day finish in well under a
  second.
- **Shared state dict for assertions.** Mocks write into
  `scenario_state["telegram_sends"]` etc. — assertion code reads the
  same dict.
- **No `SIMULATOR_MODE` env coupling in the bot.** The bot doesn't know
  it's under the simulator. All the swapping happens in mocks.

### What this is NOT

- **Not a backtester.** Use `tools/orb_backtest.py` + `tools/afternoon_backtest.py`
  for historical P&L verification. The simulator validates **behavior** —
  did the bot fire the right entries, send the right alerts, write the
  right journal records?
- **Not a load test.** No concurrency stress, no slippage model.
- **Not the only test surface.** `tests/strategy/test_orb_session_sim.py`
  (25+ rule-level scenarios) and the 1,173-test strategy suite remain
  the primary regression gates.

---

## Roadmap

Phase 1 (this commit, shipped):
- 5 mock services + clock + bar feeder + runner CLI
- 3 demonstration scenarios (golden_orb_long, gap_skip, range_too_narrow)
- Corpus-driven replay

Phase 2 (future):
- Expand scenarios to 20-30 (EOD reversal, partial-at-1R, BE-after-1R,
  VIX-kill day, earnings-window skip, blocklist, multi-portfolio
  independence, halt mid-session)
- Build corpus extractor that identifies "interesting" days from
  historical data (high vol, gap, halt, earnings, EOD winners) and
  generates scenario manifests automatically
- Expectation DSL: declarative rules ("if SPY regime is up and AAPL
  range is in band, expect a long entry between OR end and 11:00 ET")

Phase 3 (future):
- Plug into CI: add `python -m simulator.runner --scenario-all` to
  `scripts/run_ci.py` as an additional gate
- Comparative replay: run the simulator over the last N days, diff the
  observed entries/exits against the live bot's recorded forensics
  (from `/data/trade_log.jsonl` and the snapshot branch)
