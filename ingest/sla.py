"""ingest/sla.py \u2014 v6.6.0 SLA monitoring layer.

Classifies raw ingest metrics into GREEN / YELLOW / RED states.
All SLA thresholds are loaded from ingest_config (Decision A1).
RTH-only enforcement (Decision P3): thresholds are only applied for
gating during 09:30\u201316:00 ET; pre-market metrics are collected but
not used to trigger gate actions.

Thread safety: _health_states dict is protected by _sla_lock (RLock).
Writers: record_*() methods (called from ingest thread and scheduler thread).
Readers: get_health_state() (called from HTTP thread and engine/ingest_gate.py).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import ingest_config as _cfg

# ---------------------------------------------------------------------------
# Data structures (Section A.1 of architecture doc)
# ---------------------------------------------------------------------------

@dataclass
class SLAThreshold:
    """Green / yellow / red threshold triple, loaded from ingest_config."""
    green_max: float   # value <= green_max -> GREEN
    yellow_max: float  # green_max < value <= yellow_max -> YELLOW
    # value > yellow_max -> RED

    def classify(self, value: float) -> str:
        """Return 'green', 'yellow', or 'red'."""
        if value <= self.green_max:
            return "green"
        if value <= self.yellow_max:
            return "yellow"
        return "red"


@dataclass
class SLAMetric:
    """A single SLA measurement point."""
    name: str
    current_value: float
    threshold: SLAThreshold
    color: str                # "green" | "yellow" | "red"
    sampled_at: float         # time.monotonic() at collection time
    ticker: Optional[str]     # None -> global metric

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "value": self.current_value,
            "color": self.color,
        }


@dataclass
class IngestHealthState:
    """Per-ticker (or global) rolled-up ingest health."""
    ticker: Optional[str]          # None -> global
    color: str                     # "green" | "yellow" | "red"
    metrics: list                  # list[SLAMetric]
    entered_color_at: float        # monotonic time when current color was first set
    transition_history: deque = field(default_factory=lambda: deque(maxlen=20))
    # deque of (from_color, to_color, monotonic_ts)


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_health_states: dict = {}   # Optional[str] -> IngestHealthState (None = global)
_sla_lock = threading.RLock()

# Per-ticker raw metric values (latest reading)
_raw_metrics: dict = {}     # Optional[str] -> dict[str, float]


# ---------------------------------------------------------------------------
# Default thresholds (loaded from ingest_config)
# ---------------------------------------------------------------------------

def _build_thresholds() -> dict:
    """Build the default threshold map from ingest_config constants."""
    return {
        "last_bar_age_s": SLAThreshold(
            green_max=_cfg.SLA_BAR_AGE_GREEN_S,
            yellow_max=_cfg.SLA_BAR_AGE_RED_S,
        ),
        "open_gaps_today": SLAThreshold(
            green_max=0.0,
            yellow_max=float(_cfg.SLA_GAPS_RED - 1),
        ),
        "backfill_queue_depth": SLAThreshold(
            green_max=float(_cfg.SLA_QUEUE_DEPTH_YELLOW - 1),
            yellow_max=float(_cfg.SLA_QUEUE_DEPTH_RED),
        ),
        "backfill_lag_s": SLAThreshold(
            green_max=_cfg.SLA_BACKFILL_LAG_YELLOW_S,
            yellow_max=_cfg.SLA_BACKFILL_LAG_RED_S,
        ),
    }

_THRESHOLDS: dict = _build_thresholds()


# ---------------------------------------------------------------------------
# RTH helper (Decision P3)
# ---------------------------------------------------------------------------

def _is_rth(dt_et: Optional[datetime] = None) -> bool:
    """Return True if the current time (or supplied datetime) is within RTH.

    RTH = 09:30\u201316:00 ET. Uses ingest_config for start/end.
    """
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York")) if dt_et is None else dt_et
        start_minutes = _cfg.RTH_START_HOUR_ET * 60 + _cfg.RTH_START_MIN_ET
        end_minutes = _cfg.RTH_END_HOUR_ET * 60 + _cfg.RTH_END_MIN_ET
        now_minutes = now.hour * 60 + now.minute
        return start_minutes <= now_minutes < end_minutes
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------

def _worst_color(a: str, b: str) -> str:
    """Return the worse of two color strings (red > yellow > green)."""
    order = {"green": 0, "yellow": 1, "red": 2}
    if order.get(a, 0) >= order.get(b, 0):
        return a
    return b


def _derive_color_from_metrics(metrics: list) -> str:
    """Derive overall color as worst-case across all SLAMetric objects."""
    worst = "green"
    for m in metrics:
        worst = _worst_color(worst, m.color)
    return worst


# ---------------------------------------------------------------------------
# Record helpers (called by ingest thread / scheduler thread)
# ---------------------------------------------------------------------------

def _get_or_create_state(ticker: Optional[str]) -> IngestHealthState:
    """Return (creating if needed) the IngestHealthState for this ticker."""
    if ticker not in _health_states:
        _health_states[ticker] = IngestHealthState(
            ticker=ticker,
            color="green",
            metrics=[],
            entered_color_at=time.monotonic(),
            transition_history=deque(maxlen=20),
        )
    return _health_states[ticker]


def _update_state_color(state: IngestHealthState, new_color: str) -> None:
    """Update state color; record transition if color changed."""
    if new_color != state.color:
        state.transition_history.append((state.color, new_color, time.monotonic()))
        state.color = new_color
        state.entered_color_at = time.monotonic()


def update_global_stats(
    last_bar_age_s: Optional[float] = None,
    open_gaps_today: Optional[int] = None,
    backfill_queue_depth: Optional[int] = None,
) -> None:
    """Update global SLA metrics (called from _update_ingest_stats under lock)."""
    with _sla_lock:
        raw = _raw_metrics.setdefault(None, {})
        if last_bar_age_s is not None:
            raw["last_bar_age_s"] = last_bar_age_s
        if open_gaps_today is not None:
            raw["open_gaps_today"] = float(open_gaps_today)
        if backfill_queue_depth is not None:
            raw["backfill_queue_depth"] = float(backfill_queue_depth)
        _recompute_state(None)


def record_backfill_completed(
    ticker: str,
    gap_start: object,
    gap_end: object,
    written: int,
    elapsed_s: float,
) -> None:
    """Record backfill latency for a gap. Called from RestBackfillWorker._backfill()."""
    with _sla_lock:
        raw = _raw_metrics.setdefault(ticker, {})
        raw["backfill_lag_s"] = elapsed_s
        _recompute_state(ticker)


def record_gaps_detected(ticker: str, gap_count: int) -> None:
    """Record gap count for a ticker. Called from gap_detect_task()."""
    with _sla_lock:
        raw = _raw_metrics.setdefault(None, {})
        current = raw.get("open_gaps_today", 0.0)
        raw["open_gaps_today"] = current + float(gap_count)
        _recompute_state(None)


def _recompute_state(ticker: Optional[str]) -> None:
    """Recompute IngestHealthState for ticker using current _raw_metrics."""
    raw = _raw_metrics.get(ticker, {})
    metrics: list = []
    for metric_name, threshold in _THRESHOLDS.items():
        if metric_name in raw:
            value = raw[metric_name]
            color = threshold.classify(value)
            metrics.append(SLAMetric(
                name=metric_name,
                current_value=value,
                threshold=threshold,
                color=color,
                sampled_at=time.monotonic(),
                ticker=ticker,
            ))

    if not metrics:
        return

    new_color = _derive_color_from_metrics(metrics)
    state = _get_or_create_state(ticker)
    state.metrics = metrics
    _update_state_color(state, new_color)


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------

def get_health_state(ticker: Optional[str] = None) -> IngestHealthState:
    """Return the current IngestHealthState for ticker (None = global).

    Fail-safe: returns a GREEN state if no data has been collected yet.
    """
    with _sla_lock:
        if ticker in _health_states:
            return _health_states[ticker]
        # Return a synthetic GREEN state for unknown tickers (fail-open)
        return IngestHealthState(
            ticker=ticker,
            color="green",
            metrics=[],
            entered_color_at=time.monotonic(),
            transition_history=deque(maxlen=20),
        )


def get_health_snapshot(tickers: Optional[list] = None) -> dict:
    """Return the ingest_health dict for /api/state.

    Includes global state, per-ticker states, and gate mode.
    """
    from ingest_config import SLA_GATE_RED_MIN  # noqa: F401
    import os
    gate_mode = os.environ.get("SSM_INGEST_GATE_MODE", "dry_run")

    with _sla_lock:
        global_state = _health_states.get(None)
        global_dict: dict = {
            "color": global_state.color if global_state else "green",
            "metrics": [m.to_dict() for m in (global_state.metrics if global_state else [])],
            "entered_color_at_iso": _monotonic_to_iso(
                global_state.entered_color_at if global_state else time.monotonic()
            ),
        }
        per_ticker: dict = {}
        for t in (tickers or list(_health_states.keys())):
            if t is None:
                continue
            ts = _health_states.get(t)
            if ts:
                per_ticker[t] = {
                    "color": ts.color,
                    "metrics": [m.to_dict() for m in ts.metrics],
                }
    return {
        "global": global_dict,
        "per_ticker": per_ticker,
        "gate_mode": gate_mode,
        "is_rth": _is_rth(),
    }


def _monotonic_to_iso(mono_ts: float) -> str:
    """Convert a monotonic timestamp to an approximate UTC ISO string."""
    try:
        delta = time.monotonic() - mono_ts
        now_utc = datetime.now(timezone.utc)
        from datetime import timedelta
        approx = now_utc - timedelta(seconds=delta)
        return approx.isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()
