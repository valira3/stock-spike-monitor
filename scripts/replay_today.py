#!/usr/bin/env python3
"""replay_today.py -- build a replay from today's LIVE snapshots.

Fetches today's JSONL from the snapshots-live GitHub branch
(captured every 5 min by state-snapshot.yml), converts to the
replay diff format, uploads to R2, and returns a presigned URL.

Usage:
    python scripts/replay_today.py              # today
    python scripts/replay_today.py --date 2026-05-15
    python scripts/replay_today.py --local      # use local data/snapshots/
"""
from __future__ import annotations
import argparse, base64, copy, json, os, pathlib, re, sys, urllib.request, urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

# Load .env.monitor
_env = pathlib.Path(__file__).parent.parent / ".env.monitor"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import scripts.replay_dashboard as rd

REPO_OWNER  = "valira3"
REPO_NAME   = "stock-spike-monitor"
BRANCH      = "snapshots-live"
LOCAL_DIR   = pathlib.Path(__file__).parent.parent / "data" / "snapshots"


# ---------------------------------------------------------------------------
# Fetch snapshots
# ---------------------------------------------------------------------------

def _github_raw(path: str, token: str = "") -> str | None:
    """Download a file from GitHub at the given branch path, return text."""
    url = (f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}"
           f"/contents/{urllib.parse.quote(path)}?ref={BRANCH}")
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github.raw")
    try:
        return urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
    except Exception as e:
        print(f"  GitHub fetch failed: {e}")
        return None


def fetch_snapshots(date: str) -> list[dict]:
    """Return list of {ts_et, captured_at_utc, state} dicts for the given date."""
    token = os.environ.get("GITHUB_TOKEN", "")
    path  = f"data/snapshots/{date}.jsonl"

    print(f"  Fetching {path} from {BRANCH} branch...")
    raw = _github_raw(path, token)

    if not raw:
        # Fallback: local file
        local = LOCAL_DIR / f"{date}.jsonl"
        if local.exists():
            print(f"  Using local {local}")
            raw = local.read_text(encoding="utf-8")
        else:
            print(f"  No snapshots found for {date}")
            return []

    snaps: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if line:
            try:
                snaps.append(json.loads(line))
            except Exception:
                pass

    print(f"  {len(snaps)} snapshots loaded")
    return snaps


# ---------------------------------------------------------------------------
# Convert snapshots → replay diffs
# ---------------------------------------------------------------------------

def _et_min_from_server_time(server_time: str) -> int | None:
    """Extract ET minute from a server_time label like 'Fri May 16 | 10:26:00 ET'."""
    m = re.search(r"(\d{2}):(\d{2}):\d{2}", server_time or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _extract_state(snap: dict) -> dict:
    """Return the /api/state payload from a snapshot regardless of which
    writer schema produced it.

    Three schemas observed in the wild:
      - flat:        snap["state"]              (synthetic backfills, v9.1.115+ target)
      - dashboard:   snap["dashboard"]["/api/state"]   (scripts/snapshot_state.py)
      - endpoints:   snap["endpoints"]["/api/state"]   (tools/state_snapshot.py, pre-v9.1.115)
    """
    s = snap.get("state")
    if isinstance(s, dict):
        return s
    for wrapper_key in ("dashboard", "endpoints"):
        wrapped = snap.get(wrapper_key)
        if isinstance(wrapped, dict):
            inner = wrapped.get("/api/state")
            if isinstance(inner, dict) and "__error__" not in inner:
                return inner
    return {}


def _slim_portfolios(portfolios: dict) -> dict:
    """Slim the portfolios map for the per-snapshot diff. Carries only the
    fields the dashboard's per-portfolio renderers actually read."""
    out: dict = {}
    for pid, pdata in (portfolios or {}).items():
        if not isinstance(pdata, dict):
            continue
        out[pid] = {
            "portfolio_id": pdata.get("portfolio_id", pid),
            "equity": pdata.get("equity"),
            "day_pnl": pdata.get("day_pnl"),
            "realized_pnl": pdata.get("realized_pnl"),
            "unrealized_pnl": pdata.get("unrealized_pnl"),
            "positions": pdata.get("positions") or [],
            "trades_today": pdata.get("trades_today") or [],
            "strip": pdata.get("strip") or {},
        }
    return out


def snapshots_to_diffs(snaps: list[dict]) -> tuple[list[dict], dict]:
    """Convert JSONL snapshot list → (diffs, base_state) for build_html()."""
    if not snaps:
        return [], {}

    # Sort by ts_et ascending
    def _sort_key(s):
        return s.get("ts_et", "") or s.get("captured_at_utc", "")
    snaps = sorted(snaps, key=_sort_key)

    # Use midday snapshot as HTML base (most complete state)
    mid = next(
        (s for s in snaps if "12:0" in s.get("ts_et", "") or "13:0" in s.get("ts_et", "")),
        snaps[len(snaps) // 2]
    )
    base_state = copy.deepcopy(_extract_state(mid))
    base_state.pop("trades_today", None)
    base_state.pop("positions", None)

    diffs: list[dict] = []
    prev_trade_count = 0

    for snap in snaps:
        state    = _extract_state(snap)
        ts_et    = snap.get("ts_et", snap.get("captured_at_utc", ""))
        cap_utc  = snap.get("captured_at_utc", "")
        v10      = state.get("v10", {})
        rb       = v10.get("risk_books", {}).get("main", {})
        gates    = state.get("gates", {})
        trades   = state.get("trades_today", [])

        # Determine snapshot kind + label from trade delta
        kind, label = "", ""
        if len(trades) > prev_trade_count and trades:
            last_t = trades[-1]
            action = (last_t.get("action") or "").upper()
            ticker = last_t.get("ticker", "")
            price  = last_t.get("price", "")
            pnl    = last_t.get("pnl")
            if action in ("BUY", "SHORT"):
                kind  = "entry"
                label = f"{action} {ticker} @ ${price}"
            elif action in ("SELL", "COVER"):
                kind  = "exit_win" if (pnl or 0) >= 0 else "exit_loss"
                sign  = "+" if (pnl or 0) >= 0 else ""
                label = f"{action} {ticker} {sign}${pnl:.2f}" if pnl is not None else f"{action} {ticker}"
        prev_trade_count = len(trades)

        # Build ts_et in replay format ("2026-05-16T10:26:00 ET")
        server_label = state.get("server_time_label", "")
        if not ts_et or len(ts_et) < 10:
            ts_et = cap_utc

        diff_entry = {
            "ts_et":          ts_et,
            "captured_at_utc": cap_utc,
            "kind":            kind,
            "label":           label,
            "diff": {
                "trades_today":       trades,
                "positions":          state.get("positions", []),
                # Per-portfolio slim view so Val/Gene tabs update with the
                # scrubber. Without this they stay frozen on base-state values
                # because currentState() only merges the top-level legacy fields.
                "portfolios":         _slim_portfolios(state.get("portfolios") or {}),
                "server_time":        state.get("server_time", ""),
                "server_time_label":  server_label,
                "eod":                v10.get("eod", {}),
                "portfolio":          state.get("portfolio", {}),
                "regime":             state.get("regime", {}),
                "v10_activity":       v10.get("activity", []),
                "gates_scan_paused":  gates.get("scan_paused", False),
                "v10_kill_triggered": rb.get("daily_kill_triggered", False),
                "v10_realized_pnl":   rb.get("realized_pnl_today", 0.0),
                "v10_admit_count":    rb.get("admit_count", 0),
                "v10_reject_count":   rb.get("reject_count", 0),
                "v10_day_states":     v10.get("day_states", []),
            },
            "_full_state": state,
        }
        diffs.append(diff_entry)

    return diffs, base_state


# ---------------------------------------------------------------------------
# Build + upload
# ---------------------------------------------------------------------------

def _is_trading_day(date_str: str) -> bool:
    from datetime import date as _date
    return _date.fromisoformat(date_str).weekday() < 5  # Mon-Fri


def find_most_recent_snapshots(start_date: str | None = None,
                               max_lookback: int = 7
                               ) -> tuple[str | None, list[dict]]:
    """Find the most recent trading day with snapshot data.
    Falls back up to max_lookback weekdays before start_date (or today)."""
    from datetime import timedelta
    base_dt = (datetime.fromisoformat(start_date) if start_date
               else datetime.now(timezone.utc))

    for i in range(max_lookback + 1):
        candidate = base_dt.date() if i == 0 else (base_dt - timedelta(days=i)).date()
        date_str = candidate.strftime("%Y-%m-%d")
        if not _is_trading_day(date_str):
            continue
        snaps = fetch_snapshots(date_str)
        if snaps:
            return date_str, snaps
        print(f"  {date_str}: no snapshots -- looking earlier")

    return None, []


def find_recent_trading_days(end_date: str | None = None,
                             n_days: int = 5,
                             max_lookback_calendar: int = 14,
                             min_snapshots: int = 10,
                             ) -> dict[str, list[dict]]:
    """Walk backwards from end_date (or today) and return up to ``n_days``
    distinct trading days that have at least ``min_snapshots`` snapshots.
    Skips weekends and days the snapshot capture missed (or only wrote a
    metadata header without per-bucket /api/state payloads)."""
    from datetime import timedelta
    base_dt = (datetime.fromisoformat(end_date) if end_date
               else datetime.now(timezone.utc))
    found: dict[str, list[dict]] = {}
    for i in range(max_lookback_calendar + 1):
        if len(found) >= n_days:
            break
        candidate = (base_dt - timedelta(days=i)).date()
        date_str = candidate.strftime("%Y-%m-%d")
        if not _is_trading_day(date_str):
            continue
        snaps = fetch_snapshots(date_str)
        # Count snapshots that carry an actual /api/state payload, not just
        # metadata header lines.
        payload_count = sum(
            1 for s in snaps
            if isinstance(s, dict)
            and (s.get("state") or (s.get("dashboard") or {}).get("/api/state"))
        )
        if payload_count >= min_snapshots:
            found[date_str] = snaps
        elif snaps:
            print(f"  {date_str}: only {payload_count} payload-bearing snapshots -- skipping")
    return found


def build_today_replay(date: str | None = None,
                       multi_day: bool = True) -> tuple[str | None, str | None]:
    """Build a replay from snapshots and upload to R2.

    Two modes:
      - ``date`` explicit:           single-day replay for that date.
      - ``date`` None + multi_day:   most recent 5 trading days bundled
        with a date dropdown (default behavior; what the dashboard
        button hits).
      - ``date`` None + ``multi_day=False``: legacy single-day fallback
        on the most recent available day.

    Returns (presigned_url, default_date_in_url) or (None, None) on failure.
    """
    # Explicit single date -> legacy path.
    if date:
        print(f"\nBuilding single-day replay for {date}...")
        actual_date, snaps = find_most_recent_snapshots(date)
        if not snaps:
            print("  No snapshots found")
            return None, None
        diffs, base_state = snapshots_to_diffs(snaps)
        print(f"  {len(diffs)} diffs from {actual_date}")
        html = rd.build_html(diffs, base_state, start_idx=0)
        body = html.encode("utf-8")
        key = f"replay/live_{actual_date}.html"
        print(f"  Uploading ({len(body)//1024} KB) -> {key}...")
        rd.upload_r2(body, key)
        url = rd.presigned(key, expires=28800)
        print(f"  Done.")
        return url, actual_date

    if not multi_day:
        # Legacy path: most recent single day.
        actual_date, snaps = find_most_recent_snapshots(None)
        if not snaps:
            return None, None
        diffs, base_state = snapshots_to_diffs(snaps)
        html = rd.build_html(diffs, base_state, start_idx=0)
        rd.upload_r2(html.encode("utf-8"), f"replay/live_{actual_date}.html")
        return rd.presigned(f"replay/live_{actual_date}.html", expires=28800), actual_date

    # Default: bundle last 5 trading days.
    print(f"\nLooking for multi-day replay data (last 5 trading days)...")
    found = find_recent_trading_days(end_date=None, n_days=5)
    if not found:
        print("  No snapshots found in the last 14 calendar days")
        return None, None
    dates_sorted = sorted(found.keys())
    default_date = dates_sorted[-1]
    print(f"  Bundling {len(found)} days, default = {default_date}")

    days_map: dict[str, dict] = {}
    for d, snaps in found.items():
        diffs, base = snapshots_to_diffs(snaps)
        days_map[d] = {"diffs": diffs, "base": base}
        print(f"    {d}: {len(diffs)} diffs")

    default_data = days_map[default_date]
    html = rd.build_html(
        default_data["diffs"], default_data["base"], start_idx=0,
        days_map=days_map, default_date=default_date,
    )
    body = html.encode("utf-8")
    key = f"replay/week_{default_date}.html"
    print(f"  Uploading ({len(body)//1024} KB) -> {key}...")
    rd.upload_r2(body, key)
    url = rd.presigned(key, expires=28800)
    print(f"  Done.")
    return url, default_date


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build live-data replay for a trading day")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: most recent trading day with data)")
    parser.add_argument("--local", action="store_true", help="Use local snapshot file only")
    args = parser.parse_args()

    if args.local:
        os.environ["GITHUB_TOKEN"] = ""

    url, actual_date = build_today_replay(args.date)
    if url:
        print(f"\n{'='*60}")
        print(f"LIVE REPLAY URL ({actual_date}, 8 hours):")
        print(f"{url}")
        print(f"{'='*60}")
    else:
        sys.exit(1)
