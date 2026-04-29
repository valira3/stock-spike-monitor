"""engine.feature_flags — runtime overrides for Tiger Sovereign rule strictness.

Centralizes env-var-driven flags that toggle individual spec rules on/off
without code changes. Flags are evaluated once at import time so the live
loop never pays for repeated ``os.environ`` reads.

Current flags
-------------
VOLUME_GATE_ENABLED
    Controls L-P2-S3 / S-P2-S3 (Phase 2 volume gate, 100% of 55-day
    rolling per-minute baseline). Default: ``False`` (gate DISABLED).
    When False the gate auto-passes with reason ``DISABLED_BY_FLAG``;
    the 2-consecutive-1m-candle gate (L-P2-S4 / S-P2-S4) and all other
    Phase 2 logic continue to apply.

    Rationale: 2026-04-28 backtest showed the spec-strict volume gate
    filtered out trades that, when allowed with full Sentinel exit
    logic, returned net positive (+$251 cohort P&L). Operating with
    gate OFF until multi-day analysis confirms direction. Set
    ``VOLUME_GATE_ENABLED=true`` on Railway to re-enable spec-strict
    behavior.
"""
from __future__ import annotations

import os


def _read_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() == "true"


VOLUME_GATE_ENABLED: bool = _read_bool("VOLUME_GATE_ENABLED", False)


__all__ = ["VOLUME_GATE_ENABLED"]
