#!/usr/bin/env python3
"""snapshot_state.py -- lightweight RTH state capturer for replay.

Fetches /api/state, /api/executor/val, /api/executor/gene, and
/api/trade_log from the dashboard, then appends one JSON line to a
daily JSONL file in the format replay_dashboard.py expects.

Usage:
    python scripts/snapshot_state.py                    # prod, data/snapshots/
    python scripts/snapshot_state.py --env staging      # staging dashboard
    python scripts/snapshot_state.py --out /some/path   # custom output dir

Called by .github/workflows/state-snapshot.yml every 5 min during RTH.
Reads DASHBOARD_BASE_URL + DASHBOARD_PASSWORD from env (or .env.monitor).

Output: data/snapshots/YYYY-MM-DD.jsonl  (one line per call, appended)
Format: {captured_at_utc, ts_et, version, dashboard: {/api/state, ...}}

No external dependencies -- stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

PROD_URL = "https://tradegenius.up.railway.app"
STAGING_URL = "https://tradegenius-staging.up.railway.app"

ENDPOINTS = [
    "/api/state",
    "/api/executor/val",
    "/api/executor/gene",
    "/api/trade_log?limit=5000",
]


def _load_env(repo_root: Path) -> None:
    for name in (".env.monitor", ".env.monitor.staging"):
        f = repo_root / name
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        break


def _login(base_url: str, password: str) -> urllib.request.OpenerDirector:
    jar = urllib.request.HTTPCookieProcessor()
    opener = urllib.request.build_opener(jar)
    data = urllib.parse.urlencode({"password": password}).encode()
    req = urllib.request.Request(
        f"{base_url}/login",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    opener.open(req, timeout=15)
    return opener


def _fetch(opener: urllib.request.OpenerDirector, url: str) -> dict:
    try:
        return json.loads(opener.open(url, timeout=10).read())
    except Exception as e:
        return {"__error__": str(e)}


def capture(base_url: str, password: str) -> dict:
    opener = _login(base_url, password)
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(ET)

    dashboard = {ep: _fetch(opener, f"{base_url}{ep}") for ep in ENDPOINTS}
    version = (dashboard.get("/api/state") or {}).get("version", "?")

    return {
        "captured_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts_et": now_et.strftime("%Y-%m-%dT%H:%M:%S ET"),
        "version": version,
        "dashboard": dashboard,
    }


def main() -> None:
    repo = Path(__file__).resolve().parent.parent
    _load_env(repo)

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--env", default="prod", choices=["prod", "staging"])
    ap.add_argument("--out", default=None, help="Output directory (default: data/snapshots)")
    ap.add_argument("--dry-run", action="store_true", help="Print snapshot, don't write")
    args = ap.parse_args()

    base_url = PROD_URL if args.env == "prod" else STAGING_URL
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not password:
        # Prod password hardcoded for GHA (secret injected via env var)
        # For local use it must be in .env.monitor
        print("ERROR: DASHBOARD_PASSWORD not set", file=sys.stderr)
        sys.exit(1)

    print(f"Capturing {base_url} ...", flush=True)
    try:
        snap = capture(base_url, password)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"  {snap['captured_at_utc']}  v{snap['version']}")

    if args.dry_run:
        print(json.dumps(snap, indent=2, default=str)[:500] + "...")
        return

    out_dir = Path(args.out) if args.out else repo / "data" / "snapshots"
    out_dir.mkdir(parents=True, exist_ok=True)

    date_str = snap["captured_at_utc"][:10]
    out_file = out_dir / f"{date_str}.jsonl"
    line = json.dumps(snap, separators=(",", ":"), default=str)

    with out_file.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")

    print(f"  Appended to {out_file}")


if __name__ == "__main__":
    main()
