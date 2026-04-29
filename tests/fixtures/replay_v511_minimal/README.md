# replay_v511_minimal fixture

Minimal CI/regression fixture for `backtest/replay_v511_full.py`.

Contents:
- `2026-04-28/AAPL.jsonl` — first 60 RTH 1m bars sliced from the workspace
  archive at `/home/user/workspace/today_bars/2026-04-28/AAPL.jsonl`.
- `2026-04-28/QQQ.jsonl` — same, 60 bars for the index ticker.
- `2026-04-28/premarket/{AAPL,QQQ}.jsonl` — first 10 pre-market 1m bars each.

Bar shape matches `bar_archive.BAR_SCHEMA_FIELDS` (`ts`, `open`, `high`,
`low`, `close`, `iex_volume`, …) for RTH bars and the pre-market dump
shape (`ts`, `epoch`, `open`, …, `volume`, `session`) for the premarket
files. The replay loader handles both.

This fixture is intentionally tiny — its only purpose is to drive
`engine.scan.scan_loop` through enough minutes that the regression test
can assert the engine seam is wired up (callbacks recorded ≥1 fetch,
≥1 tick, no exceptions). For a full P&L replay use the workspace data
at `/home/user/workspace/today_bars/2026-04-28/`.
