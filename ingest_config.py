"""ingest_config.py \u2014 v6.6.0 Ingest Hardening tunable constants.

Single override point for all SLA thresholds and gate parameters.
Val / Devon: after Monday cron calibration, update the constants here
(or set the corresponding env vars in Railway) to apply calibrated values.

All env-var overrides are resolved at import time so that test code can
patch os.environ before importing this module.

Decision A1 (ratified): defaults ship as coded constants.
Decision A4 (ratified): hysteresis defaults are 5 min RED -> BLOCK,
                         2 min GREEN -> ALLOW.
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _float(env_key: str, default: float) -> float:
    """Read float from env or return default."""
    val = os.environ.get(env_key)
    if val is not None:
        try:
            return float(val)
        except (TypeError, ValueError):
            pass
    return default


def _int(env_key: str, default: int) -> int:
    """Read int from env or return default."""
    val = os.environ.get(env_key)
    if val is not None:
        try:
            return int(val)
        except (TypeError, ValueError):
            pass
    return default


# ---------------------------------------------------------------------------
# SLA Thresholds (Decision A1)
# ---------------------------------------------------------------------------

# last_bar_age_s: seconds since the last bar was written by BarAssembler.
# GREEN when age <= SLA_BAR_AGE_GREEN_S
# YELLOW when SLA_BAR_AGE_GREEN_S < age <= SLA_BAR_AGE_RED_S
# RED when age > SLA_BAR_AGE_RED_S
SLA_BAR_AGE_GREEN_S: float = _float("SLA_BAR_AGE_GREEN_S", 90.0)
SLA_BAR_AGE_RED_S: float = _float("SLA_BAR_AGE_RED_S", 300.0)

# open_gaps_today: count of open (un-closed) gaps detected this session.
# GREEN when gaps == 0
# YELLOW when 1 <= gaps <= SLA_GAPS_RED - 1
# RED when gaps > SLA_GAPS_RED   (default: > 2 -> RED, decision A1)
SLA_GAPS_YELLOW: int = _int("SLA_GAPS_YELLOW", 1)
SLA_GAPS_RED: int = _int("SLA_GAPS_RED", 3)

# backfill_queue_depth: pending jobs in RestBackfillWorker queue.
SLA_QUEUE_DEPTH_YELLOW: int = _int("SLA_QUEUE_DEPTH_YELLOW", 5)
SLA_QUEUE_DEPTH_RED: int = _int("SLA_QUEUE_DEPTH_RED", 20)

# backfill_lag_s: seconds from gap detection to verification confirmed closed.
SLA_BACKFILL_LAG_YELLOW_S: float = _float("SLA_BACKFILL_LAG_YELLOW_S", 60.0)
SLA_BACKFILL_LAG_RED_S: float = _float("SLA_BACKFILL_LAG_RED_S", 300.0)

# P1 ceiling (locked): maximum REST_ONLY duration before BLOCK.
# The calibrated N from Monday cron is min(observed + 5 min, 15 min).
# Hard ceiling is 20 min \u2014 no higher value ships.
GATE_REST_ONLY_CEILING_S: float = _float("GATE_REST_ONLY_CEILING_S", 1200.0)  # 20 min

# ---------------------------------------------------------------------------
# Gate Hysteresis (Decision A4)
# ---------------------------------------------------------------------------

# Seconds of continuous RED before the gate enters BLOCKED state.
SLA_GATE_RED_MIN: float = _float("SLA_GATE_RED_MIN", 300.0)  # 5 min

# Seconds of continuous GREEN before the gate exits BLOCKED state.
SLA_GATE_GREEN_MIN: float = _float("SLA_GATE_GREEN_MIN", 120.0)  # 2 min

# ---------------------------------------------------------------------------
# RTH Window (Decision P3)
# ---------------------------------------------------------------------------

RTH_START_HOUR_ET: int = _int("RTH_START_HOUR_ET", 9)
RTH_START_MIN_ET: int = _int("RTH_START_MIN_ET", 30)
RTH_END_HOUR_ET: int = _int("RTH_END_HOUR_ET", 16)
RTH_END_MIN_ET: int = _int("RTH_END_MIN_ET", 0)

# ---------------------------------------------------------------------------
# Retention (Decision P4)
# ---------------------------------------------------------------------------

# Audit log retention in calendar days (gate decisions + gap-fill audit).
AUDIT_RETENTION_DAYS: int = _int("AUDIT_RETENTION_DAYS", 180)
