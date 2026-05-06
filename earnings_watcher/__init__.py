"""v6.16.0 \u2014 earnings_watcher: pre/post-market DMI runaway-capture engine.

This package is intentionally isolated from the RTH core (eye_of_tiger.py,
trade_genius.py main scan loop). It implements the v4 NHOD + Wilder DMI
strategy that fires on earnings events outside regular trading hours.

Hard boundary:
  - earnings_watcher modules MUST NOT import from eye_of_tiger or mutate
    RTH state.
  - The RTH core MUST NOT import from earnings_watcher.
  - Shared infrastructure (Alpaca client, equity fetch) is accessed only
    through the top-level wiring layer in trade_genius.py (PR #2+),
    behind a feature flag.

Calibrated on the Phase 0 replay corpus (400 events, 27 trades, 6/6 gates
pass at $10k base + 3% hard stop + 50% portfolio exposure cap on a $100k
portfolio). See earnings_watcher_spec/replay/EXPONENTIAL_CAPTURE.md for
derivation.

Phase 1 PR rollout:
  - PR #1 (this change, v6.16.0): sizing helpers + tests, NO wiring.
  - PR #2: signals + entry, shadow path only.
  - PR #3: promote to paper trading.
  - PR #4: tighten hard stop.
"""
from __future__ import annotations

from earnings_watcher.sizing import (
    DMI_BASE_NOTIONAL_PCT,
    DMI_CONVICTION_SIZE_MAX,
    DMI_HARD_STOP,
    DMI_MAX_PORTFOLIO_EXPOSURE_PCT,
    DMI_MAX_POSITION_PCT,
    DMI_MIN_POSITION_PCT,
    DMI_TIME_STOP_MIN,
    DMI_TRAIL_PCT,
    DMI_TRAIL_TRIGGER,
    dmi_conviction_multiplier,
    dmi_sized_notional,
)

__all__ = [
    "DMI_BASE_NOTIONAL_PCT",
    "DMI_CONVICTION_SIZE_MAX",
    "DMI_HARD_STOP",
    "DMI_MAX_PORTFOLIO_EXPOSURE_PCT",
    "DMI_MAX_POSITION_PCT",
    "DMI_MIN_POSITION_PCT",
    "DMI_TIME_STOP_MIN",
    "DMI_TRAIL_PCT",
    "DMI_TRAIL_TRIGGER",
    "dmi_conviction_multiplier",
    "dmi_sized_notional",
]
