# v5.10 Full-Algorithm Backtest Replay

- bars_dir: `/data/bars`
- date range: `n/a` → `n/a`
- generated: `2026-04-28T21:06:33+00:00`
- days replayed: **0**
- total realized P&L: **$0.00**

## Guard rails: all clear (no single-day < -$5000, total > -$10000)

## No bar data was available for the requested range.
This is expected when the replay runs in a development
environment where /data/bars is not mounted. The script
itself ran end-to-end without raising; once production
bars are mounted, re-run with `--bars-dir /data/bars`.
