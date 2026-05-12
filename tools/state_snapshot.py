"""v8.3.24 -- live-dashboard state snapshot uploader.

Pulls /api/state + /api/executor/{val,gene} from the production
dashboard, bundles them into a single JSON document, and writes it
to disk so a companion GHA workflow (state-snapshot.yml) can commit
the file to a dedicated `snapshots-live` branch.

The pattern mirrors railway_log_tail.py / dashboard_monitor.py: a
read-only tool whose output a workflow forwards somewhere
durable. The downstream goal is operator-side AI agents being able
to retrieve the latest live state through the GitHub MCP (`get_file_contents`)
without needing DASHBOARD_PASSWORD in their sandbox.

Two artifacts are written per run:
  data/snapshots/latest.json
      Overwritten every tick. The single source of truth for
      "what is the bot doing right now?"
  data/snapshots/YYYY-MM-DD.jsonl
      Append-only one-line-per-tick history for the trading day.

Required env:
  DASHBOARD_BASE_URL    e.g. https://tradegenius.up.railway.app
  DASHBOARD_PASSWORD    same value the live bot has under the same name

Optional env:
  STATE_SNAPSHOT_DIR    output directory (default: ./data/snapshots)
  STATE_SNAPSHOT_QUIET  if "1", suppress per-endpoint progress logs

Exit codes:
  0  success
  1  config error (missing env)
  2  network or auth error
  3  serialization error
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Reuse the production-tested login client.
from tools.dashboard_monitor import DashboardClient


ENDPOINTS_TO_PULL: tuple[str, ...] = (
    "/api/state",
    "/api/executor/val",
    "/api/executor/gene",
)


def _require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"::error::{name} env var is not set", flush=True)
        sys.exit(1)
    return v


def _log(msg: str) -> None:
    if os.environ.get("STATE_SNAPSHOT_QUIET") == "1":
        return
    print(msg, flush=True)


def _pull_all(client: DashboardClient) -> dict[str, Any]:
    """Return {endpoint: payload-or-error} for every endpoint."""
    bundle: dict[str, Any] = {}
    for path in ENDPOINTS_TO_PULL:
        t0 = time.time()
        try:
            bundle[path] = client.get_json(path)
            _log(f"  GET {path} OK ({time.time() - t0:.2f}s)")
        except Exception as e:
            bundle[path] = {"__error__": str(e)}
            _log(f"  GET {path} FAILED: {e}")
    return bundle


def _write_outputs(snapshot: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    """Write latest.json + append a JSONL line for today.

    Returns the two written paths.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    latest = out_dir / "latest.json"
    payload = json.dumps(snapshot, indent=2, sort_keys=True)
    latest.write_text(payload, encoding="utf-8")

    # Append-only per-day history. One JSON document per line, no
    # indentation so each tick stays a single record under typical
    # JSONL conventions.
    ts = snapshot["captured_at_utc"]
    day = ts.split("T", 1)[0]
    daily = out_dir / f"{day}.jsonl"
    line = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    with daily.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return latest, daily


def main() -> int:
    base = _require_env("DASHBOARD_BASE_URL")
    password = _require_env("DASHBOARD_PASSWORD")

    out_dir = Path(os.environ.get(
        "STATE_SNAPSHOT_DIR", "data/snapshots"
    )).resolve()

    _log(f"[state-snapshot] base={base} out={out_dir}")

    try:
        client = DashboardClient(base, password, timeout=20.0)
        client.login()
        _log("  login OK")
    except Exception as e:
        print(f"::error::login failed: {e}", flush=True)
        return 2

    bundle = _pull_all(client)

    # Did the auth-required endpoint come back with content? If even
    # /api/state failed, treat the whole snapshot as a network error
    # so the workflow doesn't commit a near-empty file.
    state = bundle.get("/api/state")
    if not isinstance(state, dict) or "__error__" in state:
        print(f"::error::/api/state unreachable: {state}", flush=True)
        return 2

    snapshot = {
        "schema_version": 1,
        "captured_at_utc": datetime.now(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dashboard_base_url": base,
        "endpoints": bundle,
    }

    try:
        latest, daily = _write_outputs(snapshot, out_dir)
        _log(f"  wrote {latest}")
        _log(f"  appended {daily}")
    except Exception as e:
        print(f"::error::write failed: {e}", flush=True)
        return 3

    bot_version = ""
    if isinstance(state, dict):
        bot_version = str(state.get("bot_version", ""))
    _log(f"[state-snapshot] OK v={bot_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
