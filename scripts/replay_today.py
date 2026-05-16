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
    base_state = copy.deepcopy(mid.get("state", {}))
    base_state.pop("trades_today", None)
    base_state.pop("positions", None)

    diffs: list[dict] = []
    prev_trade_count = 0

    for snap in snaps:
        state    = snap.get("state", {})
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

def build_today_replay(date: str | None = None) -> str | None:
    """Build a replay from today's live snapshots. Returns presigned R2 URL."""
    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print(f"\nBuilding replay for {date}...")
    snaps = fetch_snapshots(date)
    if not snaps:
        print("  Aborting — no snapshots")
        return None

    diffs, base_state = snapshots_to_diffs(snaps)
    print(f"  {len(diffs)} diffs, base from midday")

    html = rd.build_html(diffs, base_state, start_idx=0)
    body = html.encode("utf-8")
    key  = f"replay/live_{date}.html"

    print(f"  Uploading ({len(body)//1024} KB) → {key}...")
    rd.upload_r2(body, key)
    url = rd.presigned(key, expires=28800)  # 8-hour link
    print(f"  Done.")
    return url


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build live-data replay for a trading day")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--local", action="store_true", help="Use local snapshot file only")
    args = parser.parse_args()

    if args.local and args.date:
        # Force local by blanking token
        os.environ["GITHUB_TOKEN"] = ""

    url = build_today_replay(args.date)
    if url:
        print(f"\n{'='*60}")
        print(f"LIVE REPLAY URL (8 hours):")
        print(f"{url}")
        print(f"{'='*60}")
    else:
        sys.exit(1)
