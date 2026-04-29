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

LEGACY_EXITS_ENABLED
    Controls whether the pre-Tiger-Sovereign exit paths (Profit-Lock
    Ladder, Section IV Sovereign-Brake / Velocity-Fuse, Phase A/B/C
    state machine, RED_CANDLE long polarity exit, POLARITY_SHIFT short
    exit) run alongside Tiger Sovereign Phase 4 (Sentinel A/B/C +
    Titan Grip). Default: ``False`` (legacy exits DISABLED).

    When False: positions are managed exclusively by ``_run_sentinel``;
    legacy exit blocks are skipped entirely. PDC dict population is
    untouched (still consumed by dashboard pills, ``[V510-IDX]``
    shadow logger, and position records); only the exit-decision use
    of legacy paths is gated.

    When True: existing v5.13.1 behaviour preserved. Whenever a legacy
    exit fires AND ``_run_sentinel`` for the same position on the same
    tick produced a non-empty ``alarms`` set, a ``[CONFLICT-EXIT]``
    structured log line is emitted for shadow analysis. Set
    ``LEGACY_EXITS_ENABLED=true`` on Railway to re-enable legacy paths
    for canary windows.
"""
from __future__ import annotations

import os


def _read_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() == "true"


VOLUME_GATE_ENABLED: bool = _read_bool("VOLUME_GATE_ENABLED", False)
LEGACY_EXITS_ENABLED: bool = _read_bool("LEGACY_EXITS_ENABLED", False)


__all__ = ["VOLUME_GATE_ENABLED", "LEGACY_EXITS_ENABLED"]
