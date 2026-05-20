"""Persistent closed-trade log (append-only JSONL).

History. Lived in trade_genius.py from v3.4.27 through v9.1.140. Carved
out to its own module in v10.0.1 as part of the post-architectural-
review monolith reduction. trade_genius.py keeps back-compat re-exports
for the callers that haven't migrated (broker/orders.py,
telegram_commands.py, and several tests).

Every closed trade (longs via close_position, shorts via
close_short_position, and their TP counterparts) writes one JSON line
to TRADE_LOG_FILE. The file lives on the Railway volume so it survives
redeploys. Append-only -- never rewritten, never rotated (a year of
typical volume is ~3 MB).

Schema (one line per row):

    {
      "schema_version": 1,
      "bot_version": "9.1.140",
      "ticker": "AAPL",
      "side": "LONG"|"SHORT",
      "pnl": 123.45,
      "reason": "STOP"|"TRAIL"|"RED_CANDLE"|...,
      ...
      # plus the trail/stop diagnostic fields from
      # _trade_log_snapshot_pos at close time
    }

All writes are best-effort: any IO error is logged and swallowed so a
broken disk never breaks trade execution.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Optional


logger = logging.getLogger(__name__)


TRADE_LOG_FILE = os.getenv(
    "TRADE_LOG_PATH", "trade_log.jsonl",
)
TRADE_LOG_SCHEMA_VERSION = 1

_trade_log_lock = threading.Lock()

# Surfaced via /api/state for dashboard visibility. The carved
# module owns this; trade_genius re-exports it via PEP 562 __getattr__
# so existing callers (`from trade_genius import _trade_log_last_error`)
# keep working unchanged.
_trade_log_last_error: Optional[str] = None


def get_last_error() -> Optional[str]:
    """Read-side accessor for callers that want the live value (vs a
    once-imported snapshot). Used by dashboard_server + telegram cmds."""
    return _trade_log_last_error


def _trade_log_snapshot_pos(pos) -> dict:
    """Extract trail + stop diagnostic fields from a position dict.

    Accepts both long (trail_high) and short (trail_low) shapes.
    Returns a dict of None-safe values. Used at close time so the
    row captures exactly what the exit decision saw.
    """
    if not isinstance(pos, dict):
        return {
            "trail_active_at_exit": None,
            "trail_stop_at_exit": None,
            "trail_anchor_at_exit": None,
            "hard_stop_at_exit": None,
            "effective_stop_at_exit": None,
            # v7.107.0 (audit SEV-2 fix) -- entry_stop is the
            # IMMUTABLE stop captured at entry time, before any
            # trail/BE/ratchet mutation. The classic R-multiple
            # denominator. Pre-v7.107.0 trade_replay used
            # hard_stop_at_exit (= pos["stop"] AT EXIT) which is
            # actually the trailed stop because exits.maybe_arm_be
            # and Alarm-F/Alarm-C all mutate pos["stop"] in place.
            "entry_stop": None,
        }
    trail_active = bool(pos.get("trail_active", False))
    trail_stop = pos.get("trail_stop")
    # Either long (trail_high) or short (trail_low) populates anchor.
    trail_anchor = pos.get("trail_high", pos.get("trail_low"))
    hard_stop = pos.get("stop")
    # v7.107.0 -- read the immutable entry stop. broker.orders sets
    # `initial_stop` at entry time alongside `stop`; nothing mutates
    # `initial_stop` thereafter. Fall back to `stop` only when
    # `initial_stop` is absent (legacy positions opened pre-init_stop).
    initial_stop = pos.get("initial_stop")
    if initial_stop is None:
        initial_stop = hard_stop
    effective_stop = (
        trail_stop if (trail_active and trail_stop is not None) else hard_stop
    )

    def _as_float(v):
        return float(v) if v is not None else None

    return {
        "trail_active_at_exit": trail_active,
        "trail_stop_at_exit": _as_float(trail_stop),
        "trail_anchor_at_exit": _as_float(trail_anchor),
        "hard_stop_at_exit": _as_float(hard_stop),
        "effective_stop_at_exit": _as_float(effective_stop),
        "entry_stop": _as_float(initial_stop),
    }


def trade_log_append(row: dict) -> bool:
    """Append a single closed-trade row to the persistent trade log.

    Best-effort: failures are logged and swallowed, never raised. The
    lock guards against the rare case of two close paths firing at once
    -- writes are atomic at the OS level for small lines on POSIX, but
    the lock keeps log order deterministic and protects the
    _trade_log_last_error surface from races.
    """
    global _trade_log_last_error
    # Defensive: never let a caller ship missing required fields.
    required = ("ticker", "side", "pnl", "reason")
    for f in required:
        if f not in row:
            _trade_log_last_error = f"missing field: {f}"
            logger.warning("[TRADE_LOG] skipping row missing %s: %s", f, row)
            return False
    # Import BOT_VERSION lazily to avoid a circular import; bot_version
    # is the canonical source.
    from bot_version import BOT_VERSION as _BV
    full = {
        "schema_version": TRADE_LOG_SCHEMA_VERSION,
        "bot_version": _BV,
    }
    full.update(row)
    line = json.dumps(full, default=str, separators=(",", ":"))
    try:
        with _trade_log_lock:
            # Open append+ with explicit newline to keep JSONL clean.
            with open(TRADE_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        _trade_log_last_error = None
        return True
    except OSError as e:
        _trade_log_last_error = f"{type(e).__name__}: {e}"
        logger.error(
            "[TRADE_LOG] append failed (%s). Path=%s. Trade still "
            "executed -- only persistence failed.",
            e, TRADE_LOG_FILE,
        )
        return False


def trade_log_read_tail(
    limit: int = 500,
    since_date: Optional[str] = None,
    portfolio: Optional[str] = None,
) -> list[dict]:
    """Read the tail of the trade log, optionally filtered.

    Returns a list of dicts, newest-last (same order as on disk).
    Filtering is applied AFTER reading -- trade log is small enough
    that this is fine. Failures return an empty list; never raises.

    Args:
      limit:       max rows to return (newest)
      since_date:  optional "YYYY-MM-DD"; only rows with date >= this
      portfolio:   optional "paper" or "tp" filter
    """
    if not os.path.exists(TRADE_LOG_FILE):
        return []
    try:
        with open(TRADE_LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        logger.error("[TRADE_LOG] read failed: %s", e)
        return []
    rows: list[dict] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except json.JSONDecodeError:
            # Defensively skip corrupted lines rather than blowing up
            # the whole read.
            continue
    if since_date:
        rows = [r for r in rows if r.get("date", "") >= since_date]
    if portfolio:
        rows = [r for r in rows if r.get("portfolio") == portfolio]
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    return rows
