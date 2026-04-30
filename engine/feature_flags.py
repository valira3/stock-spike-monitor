"""engine.feature_flags \u2014 runtime overrides for Tiger Sovereign rule strictness.

Centralizes env-var-driven flags that toggle individual spec rules on/off
without code changes. Flags are evaluated once at import time so the live
loop never pays for repeated ``os.environ`` reads.

Current flags
-------------
VOLUME_GATE_ENABLED
    Controls the v15.0 Phase 2 volume gate (1m volume >= 100% of 55-bar
    rolling avg, REQUIRED after 10:00 AM ET). Default: ``True`` as of
    v5.20.0 (gate ENABLED to match v15.0 spec).

    The live hot path always passes ``now_et`` to
    ``eye_of_tiger.evaluate_volume_bucket``, which routes to the
    spec-mandatory time-conditional path independent of this flag.
    The flag now only governs legacy callers that omit ``now_et``.

Retired flags
-------------
LEGACY_EXITS_ENABLED
    Removed in v5.13.10. The pre-Tiger-Sovereign exit paths
    (Profit-Lock Ladder, Section IV Sovereign-Brake / Velocity-Fuse,
    Phase A/B/C state machine, RED_CANDLE long polarity exit,
    POLARITY_SHIFT short exit) and the entire env-var-gated wiring
    around them have been deleted from broker/positions.py. Tiger
    Sovereign Phase 4 (Sentinel A/B/C + Titan Grip) is now the sole
    exit path. The Railway env var, if still set, is ignored.
"""

from __future__ import annotations

import os


def _read_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() == "true"


# v5.20.0 \u2014 default flipped to True to match v15.0 spec.
VOLUME_GATE_ENABLED: bool = _read_bool("VOLUME_GATE_ENABLED", True)


__all__ = ["VOLUME_GATE_ENABLED"]
