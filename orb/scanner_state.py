"""Module-level state for v10's dynamic-universe scanner.

Holds the most recent `LiveScanResult` so:
  - The morning ORB engine can read today's universe at session start.
  - The dashboard /api/state endpoint can serialize today's picks +
    cluster gate state per portfolio for UI consumption.
  - Tests can inject a synthetic result.

State is session-scoped; cleared via `clear_state()` on session restart.
Thread-safe (single _lock guards both reader + writer).
"""
from __future__ import annotations

import threading
from typing import Optional

from orb.live_premarket_scanner import LiveScanResult


_lock = threading.Lock()
_current: Optional[LiveScanResult] = None


def set_current(result: LiveScanResult) -> None:
    """Replace the active scanner result. Called at session start."""
    global _current
    with _lock:
        _current = result


def get_current() -> Optional[LiveScanResult]:
    """Return the active scanner result, or None if not yet computed."""
    with _lock:
        return _current


def clear_state() -> None:
    """Drop the active result. Used at end-of-day or in tests."""
    global _current
    with _lock:
        _current = None


def to_snapshot_dict() -> dict:
    """Serialize the active scanner state for /api/state consumption.

    Always returns a dict (never None) so dashboard JS can rely on
    consistent shape; uses `dynamic_universe_active=False` and
    `picks=[]` to signal "not active" when state is missing.
    """
    with _lock:
        r = _current
    if r is None:
        return {
            "date": "",
            "dynamic_universe_active": False,
            "cluster_gate_active": False,
            "cluster_gate_skipped_day": False,
            "cluster_max_sector_pct": 0.0,
            "cluster_top_sector": "",
            "universe": [],
            "picks": [],
            "fallback_reason": "not_initialized",
        }
    return {
        "date": r.date_str,
        "dynamic_universe_active": r.dynamic_universe_active,
        "cluster_gate_active": r.cluster_gate_active,
        "cluster_gate_skipped_day": r.cluster_gate_skipped_day,
        "cluster_max_sector_pct": round(r.cluster_max_sector_pct, 2),
        "cluster_top_sector": r.cluster_top_sector,
        "universe": list(r.universe),
        "picks": list(r.picks),
        "fallback_reason": r.fallback_reason,
    }
