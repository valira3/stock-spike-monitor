"""v7.5.0 \u2014 Early-Ditch flag module.

Single source of truth for the v7.5.0 Early-Ditch filter (Filter #3).

Motivation: forensic analysis of the v7.4.0 frac_05 83-day baseline showed
that 44 trades closed with hold_min < 5 at a 6.4% win rate, contributing
$-2,271.76 of the $-23,936 gross loss with only $128 of upside given up.
The Early-Ditch filter exits any position whose unrealized P/L falls below
a small dollar threshold within the first N seconds after entry, on the
theory that a trade that goes immediately and materially red has lost the
edge that justified the entry.

Defaults are conservative (90s window, $10 red) and can be tuned via env.
"""
from __future__ import annotations
import os


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y", "t")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# Master switch. Default OFF until the 83-day backtest validates it.
V750_EARLY_DITCH_ENABLED: bool = _env_bool("V750_EARLY_DITCH_ENABLED", False)

# Window (seconds since entry) inside which the filter is active.
V750_EARLY_DITCH_WINDOW_SEC: float = _env_float("V750_EARLY_DITCH_WINDOW_SEC", 90.0)

# Unrealized loss in DOLLARS that triggers the early ditch.
# A position whose unrealized_pnl is <= -V750_EARLY_DITCH_RED_DOLLARS inside
# the window will be flagged for full exit with reason 'v750_early_ditch'.
V750_EARLY_DITCH_RED_DOLLARS: float = _env_float("V750_EARLY_DITCH_RED_DOLLARS", 10.0)

EXIT_REASON_V750_EARLY_DITCH: str = "v750_early_ditch"

__all__ = [
    "V750_EARLY_DITCH_ENABLED",
    "V750_EARLY_DITCH_WINDOW_SEC",
    "V750_EARLY_DITCH_RED_DOLLARS",
    "EXIT_REASON_V750_EARLY_DITCH",
]
