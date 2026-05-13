---
name: state-snapshot-retrieval
description: Pull live dashboard state (positions, OR boundaries, RiskBook, day_states, trade_log) + Alpaca broker state (account equity, open positions, today's orders) without secrets in the sandbox. Uses the snapshots-live + alpaca-live branches updated by GHA cron every 10 min during US RTH.
---

# Retrieving live state from sandbox

The Claude Code sandbox is firewalled from `tradegenius.up.railway.app` (Host-not-in-allowlist) and from Alpaca's API. To analyze live trading state without the operator pasting JSON, pull the snapshots committed by dedicated GHA workflows. Two relays live today:

| Relay | Branch | Path | Source | Workflow |
|---|---|---|---|---|
| Dashboard (`/api/state` + executors + trade_log) | `snapshots-live` | `data/snapshots/latest.json` | `tradegenius.up.railway.app` | `.github/workflows/state-snapshot.yml` |
| Alpaca broker state (per-portfolio equity, positions, orders) | `alpaca-live` | `data/alpaca/latest.json` | `paper-api.alpaca.markets` | `.github/workflows/alpaca-snapshot.yml` |

```
mcp__github__get_file_contents(
    owner="valira3", repo="stock-spike-monitor",
    path="data/snapshots/latest.json", ref="snapshots-live"
)
# or
mcp__github__get_file_contents(
    owner="valira3", repo="stock-spike-monitor",
    path="data/alpaca/latest.json", ref="alpaca-live"
)
```

Both cron schedules use the v9.1.6 off-peak slot pattern (`*,12-22 UTC, Mon-Fri`):
* dashboard relay: `2,12,22,32,42,52 12-22 * * 1-5`
* alpaca relay:   `5,15,25,35,45,55 12-22 * * 1-5` (offset by ~5 min so a single GH-cron contention slot doesn't take out both)

Daily JSONL history at `data/snapshots/YYYY-MM-DD.jsonl` and `data/alpaca/YYYY-MM-DD.jsonl`.

For an immediate refresh outside the cron window: Actions tab → `state-snapshot` / `alpaca-snapshot` → Run workflow (`workflow_dispatch`).

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

## Alpaca snapshot shape (`alpaca-live` branch, `data/alpaca/latest.json`)

```json
{
  "schema_version": 1,
  "captured_at_utc": "2026-05-13T17:26:32Z",
  "portfolios": {
    "main": { "account": {...}, "positions": [...], "orders_today": [...] },
    "val":  { "account": {...}, "positions": [...], "orders_today": [...] },
    "gene": { "account": {...}, "positions": [...], "orders_today": [...] }
  }
}
```

Per-portfolio fields:
* `account.equity / cash / last_equity / buying_power / long_market_value / short_market_value` — broker-side numerics. Sidesteps the v9.1.8 risk_book persistence gap (where engine-internal `realized_pnl_today` can fall out of sync).
* `account.account_blocked / status / pattern_day_trader` — gate checks.
* `positions[]` — broker view of open positions: `{symbol, side, qty, avg_entry_price, current_price, market_value, cost_basis, unrealized_pl, unrealized_plpc, change_today}`.
* `orders_today[]` — every order submitted today, all statuses: `{symbol, side, qty, filled_qty, filled_avg_price, status in (filled|canceled|partially_filled|accepted|rejected), submitted_at, filled_at}`. Critical for diagnosing fills, slippage, and rejections that don't appear in `trades_today`.

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

## Practical command pattern (v9.1.13+)

`mcp__github__get_file_contents` on this path returns an error saying the result exceeds the token cap. The harness automatically writes the full payload to a temp file under `/root/.claude/projects/.../tool-results/`. The recipe to extract and analyze:

```bash
# 1. The MCP wraps the response as a JSON array:
#    [{"type":"text","text":"successfully downloaded ..."},
#     {"type":"text","text":"[Resource from github ...] {...actual JSON...}"}]
# 2. Index [1] is the resource. Strip the leading "[Resource from github ...]"
#    preamble (everything up to the first '{').
jq -r '.[1].text' /path/to/mcp-github-get_file_contents-*.txt \
  | sed -e '1s/^[^{]*//' > /tmp/snap.json

# 3. Now parse normally with python or jq.
python3 << 'PY'
import json
d = json.load(open('/tmp/snap.json'))
st = d['endpoints']['/api/state']
print(st.get('version'), len(st.get('trades_today') or []))
PY
```

## Live watchdog pattern (background diff stream)

When you want to be notified of state transitions during a session (deploy lands, new trade fires, EOD entry attempt, error count rises) without polling manually, arm a Monitor that polls the raw GitHub URL — **no auth required** since the repo is public:

```bash
URL="https://raw.githubusercontent.com/valira3/stock-spike-monitor/snapshots-live/data/snapshots/latest.json"
prev_state=""
while true; do
  body=$(curl -sS --max-time 15 -H 'Cache-Control: no-cache' "$URL?cb=$RANDOM" 2>/dev/null || true)
  state=$(echo "$body" | python3 -c "
import sys,json
d=json.load(sys.stdin)
st=d.get('endpoints',{}).get('/api/state',{})
print('|'.join([
  d.get('captured_at_utc','?'),
  st.get('version','?'),
  str(len(st.get('trades_today') or [])),
  str(len(st.get('positions') or [])),
  str((st.get('errors') or {}).get('count',0)),
]))
")
  if [ "$state" != "$prev_state" ]; then
    echo "[$(date -u +%H:%M:%SZ)] $state"
    prev_state="$state"
  fi
  sleep 90
done
```

Each `echo` line becomes a Monitor notification. Emit only when state changes so the operator's chat isn't spammed with no-ops. Monitors are capped at 30 min wall-clock; re-arm on timeout if you need a longer watch.

## When the cron is stale + Claude can't refresh

Two non-obvious limits to know:

1. **Claude can't trigger workflow_dispatch via MCP.** There's no `mcp__github__run_workflow_dispatch` tool. Asking the operator to click `Actions → state-snapshot → Run workflow` is the only refresh path.
2. **The cron itself is unreliable.** GitHub Actions cron is best-effort and frequently delays/skips runs (especially `*/10` patterns that land on top-of-hour). v9.1.6 shifted the schedule to off-peak minutes (`2,12,22,32,42,52`) and widened the window (`12-22 UTC`). When the latest snapshot is more than ~20 min old during RTH, assume the cron has gaps and prompt the operator to manually fire.
