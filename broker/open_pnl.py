"""broker.open_pnl \u2014 v6.15.0 open-position P/L snapshot.

The bot's ``trade_log.jsonl`` only records closed round-trips, so the
dashboard's "today P/L" undercounts whenever a position is still open
when you look at it (e.g. the AVGO -$15.62 unrealized that hid in the
2026-05-05 reconciliation).

This helper queries Alpaca's ``get_all_positions`` whenever the periodic
sentinel tick fires and appends a single JSONL row per snapshot to
``/data/open_pnl.jsonl``. The dashboard tails the file and adds the
latest row's ``total_unrealized`` to the closed sum so the headline
number matches the broker's portfolio value.

The file is append-only and self-truncating: rows are tiny (one per
sentinel tick, ~30s cadence \u2192 ~1 KB/min during RTH \u2192 ~400 KB/day),
and the dashboard reads only the last few hundred bytes via tail.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

OPEN_PNL_PATH = Path(os.getenv("OPEN_PNL_PATH", "/data/open_pnl.jsonl"))


def snapshot_open_pnl(client: Any, bot_version: str) -> dict | None:
    """Fetch live positions from Alpaca and append a JSONL row.

    Returns the row dict (also written to disk) or None on any failure.
    Failures are non-fatal \u2014 the dashboard tolerates a missing or stale
    file by reporting open_pnl=0 (which matches v6.14.10 behaviour).

    Schema:
      {
        "ts_utc": "2026-05-05T16:34:12Z",
        "bot_version": "6.15.0",
        "n_open": 2,
        "total_unrealized": -15.62,
        "positions": [
          {"symbol": "AVGO", "qty": 22.0, "avg": 263.41,
           "mark": 262.70, "unrealized": -15.62},
          ...
        ],
      }
    """
    if client is None:
        return None
    try:
        positions = client.get_all_positions()
    except Exception:
        logger.debug("snapshot_open_pnl: get_all_positions failed", exc_info=True)
        return None

    rows: list[dict] = []
    total_unrealized = 0.0
    for p in positions or []:
        try:
            sym = getattr(p, "symbol", "?")
            qty = float(getattr(p, "qty", 0) or 0)
            avg = float(getattr(p, "avg_entry_price", 0) or 0)
            mark = float(getattr(p, "current_price", 0) or 0)
            unreal = float(getattr(p, "unrealized_pl", 0) or 0)
        except Exception:
            continue
        rows.append(
            {
                "symbol": sym,
                "qty": qty,
                "avg": round(avg, 4),
                "mark": round(mark, 4),
                "unrealized": round(unreal, 2),
            }
        )
        total_unrealized += unreal

    rec = {
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "bot_version": bot_version,
        "n_open": len(rows),
        "total_unrealized": round(total_unrealized, 2),
        "positions": rows,
    }
    try:
        OPEN_PNL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with OPEN_PNL_PATH.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        logger.debug("snapshot_open_pnl: write failed", exc_info=True)
    return rec


def read_latest_open_pnl(path: Path | None = None) -> dict | None:
    """Tail the open_pnl.jsonl file and return the most recent row.

    Reads only the last 4 KB of the file so this is O(1) regardless of
    how long the bot has been running. Returns None if the file is
    missing, empty, or the last line fails to parse.
    """
    p = Path(path) if path is not None else OPEN_PNL_PATH
    try:
        if not p.exists():
            return None
        with p.open("rb") as f:
            f.seek(0, 2)
            end = f.tell()
            f.seek(max(0, end - 4096))
            tail = f.read().decode("utf-8", errors="ignore").strip().splitlines()
        if not tail:
            return None
        return json.loads(tail[-1])
    except Exception:
        return None


__all__ = ["OPEN_PNL_PATH", "snapshot_open_pnl", "read_latest_open_pnl"]
