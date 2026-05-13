---
name: state-snapshot-retrieval
description: Pull live dashboard state (positions, OR boundaries, RiskBook, day_states, trade_log) without DASHBOARD_PASSWORD. Uses the snapshots-live branch updated by .github/workflows/state-snapshot.yml every 10 min during US RTH.
---

# Retrieving live state from sandbox

The Claude Code sandbox is firewalled from `tradegenius.up.railway.app` (Host-not-in-allowlist). To analyze live trading state without the operator pasting JSON, pull the latest snapshot from the dedicated `snapshots-live` branch via the GitHub MCP:

```
mcp__github__get_file_contents(
    owner="valira3", repo="stock-spike-monitor",
    path="data/snapshots/latest.json", ref="snapshots-live"
)
```

The cron workflow `.github/workflows/state-snapshot.yml` updates `latest.json` every 10 min during US RTH (Mon-Fri, 13:00-21:00 UTC) by running `python -m tools.state_snapshot` against `/api/state` + `/api/executor/val` + `/api/executor/gene` + `/api/trade_log?limit=5000`. Daily JSONL history at `data/snapshots/YYYY-MM-DD.jsonl`.

For an immediate refresh outside the cron window: Actions tab → state-snapshot → Run workflow (`workflow_dispatch`).

## Snapshot shape

```json
{
  "schema_version": 1,
  "captured_at_utc": "2026-05-13T00:11:23Z",
  "dashboard_base_url": "https://tradegenius.up.railway.app",
  "endpoints": {
    "/api/state":              { ... full /api/state ... },
    "/api/executor/val":       { ... val executor diagnostics ... },
    "/api/executor/gene":      { ... gene executor diagnostics ... },
    "/api/trade_log?limit=5000": { "rows": [...], "count": N }
  }
}
```

## Common reads

- **Open positions per portfolio**: `endpoints./api/state.portfolios.{main,val,gene}.positions`
- **OR boundaries per ticker**: `endpoints./api/state.v10.or_windows.<TICKER>` → `{or_high, or_low, locked, locked_at_iso, or_close, or_width_pct}`
- **RiskBook snapshot**: `endpoints./api/state.v10.risk_books.<pid>` → admit/reject counts, open_risk, open_notional, daily_kill state, **and (v8.3.34+) `loss_lock_threshold_usd`, `peak_dd_halt_usd`, `locked_pairs`, `peak_pnl_today`, `current_dd_from_peak`**
- **FSM per (portfolio, ticker)**: `endpoints./api/state.v10.day_states` → list of `{portfolio_id, ticker, phase, in_position, last_entry_iso}`
- **Persistent trade log** (multiple days): `endpoints./api/trade_log?limit=5000.rows` → up to 5000 closed legs with `entry_time` (ET), `exit_time` (UTC), `pnl`, `reason`, etc.

## File-size guard

The snapshot file is large (often 150-400KB). `mcp__github__get_file_contents` will report file size and offer to truncate. Don't fight it: use `Bash` + `python3 -c "...json..."` to extract specific fields rather than reading the whole thing into context.

## When the snapshot is stale

If you need state from a specific point in time and the cron didn't capture it:
1. Check `data/snapshots/YYYY-MM-DD.jsonl` for an earlier same-day capture (one line per cron tick)
2. If the historical capture you need doesn't exist, **don't try to recreate it from live** — the data is gone. Note the gap in your analysis.
