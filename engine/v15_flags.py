"""engine.v15_flags \u2014 Tiger Sovereign v15.0 spec-compliance flags.

Each flag turns on a v15 spec rule that diverges from the current
v7.4.0 production behavior. All flags default to ON in this fork; in
production they can be disabled via env vars to re-enable the v7.x
relaxations.

Spec source: /home/user/workspace/tiger-sovereign-spec-v15-1.md

Flags
-----
V15_HARD_STRIKE_CAP
    spec line 21: "Maximum 3 Strikes per ticker per day."
    When True (default), the v7.0.2 recursive-unlock relaxation is
    disabled \u2014 strike count >= 3 is a hard stop regardless of
    closed-strike P/L history.

V15_SCALED_DI_FLOOR
    spec line 46/64: scaled strike triggers on 1m DI in [25, 30].
    When True (default), Phase-3 sizing uses the spec floor of 25.0
    instead of the v6.8.0 relaxed floor of 22.0.

V15_ALARM_E_POST_ENABLED
    spec lines 30 + 83: divergence detected while a position is open
    ratchets STOP MARKET to current price \u00b1 0.25%.
    When True (default), this fork enables the existing
    ``ALARM_E_ENABLED`` post-entry sentinel path. The pre-entry filter
    is always-on regardless of this flag.

V15_REQUIRE_5M_ADX_20
    spec lines 43/61: "Momentum Check: 5m ADX > 20".
    When True (default), Phase-3 entry decisions also require live
    5m ADX strictly greater than 20.0. Below threshold (or None during
    warmup) the entry is suppressed.
"""

from __future__ import annotations

import os


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


V15_HARD_STRIKE_CAP: bool = _bool_env("V15_HARD_STRIKE_CAP", True)
V15_SCALED_DI_FLOOR_ENABLED: bool = _bool_env("V15_SCALED_DI_FLOOR_ENABLED", True)
V15_ALARM_E_POST_ENABLED: bool = _bool_env("V15_ALARM_E_POST_ENABLED", True)
V15_REQUIRE_5M_ADX_20: bool = _bool_env("V15_REQUIRE_5M_ADX_20", True)

# Numeric thresholds (spec-locked; env overrides allowed for sweeps).
V15_SCALED_DI_FLOOR: float = float(os.getenv("V15_SCALED_DI_FLOOR", "25.0"))
V15_MOMENTUM_ADX_5M_MIN: float = float(os.getenv("V15_MOMENTUM_ADX_5M_MIN", "20.0"))


__all__ = [
    "V15_HARD_STRIKE_CAP",
    "V15_SCALED_DI_FLOOR_ENABLED",
    "V15_ALARM_E_POST_ENABLED",
    "V15_REQUIRE_5M_ADX_20",
    "V15_SCALED_DI_FLOOR",
    "V15_MOMENTUM_ADX_5M_MIN",
]
