"""engine.timing — centralized time-of-day constants for v5.13.0 Tiger Sovereign.

Spec rules (STRATEGY.md §3 SHARED SYSTEM RULES):

* SHARED-CUTOFF: New-position cutoff at 15:44:59 ET — entries blocked at/after.
* SHARED-EOD:    EOD flush at 15:49:59 ET — all open positions force-closed.
* SHARED-HUNT:   Unlimited hunting until SHARED-CUTOFF (no early stop based on
                 N-trades-per-day or cooldown — only the cutoff and the daily
                 circuit breaker can stop new entries).

All comparisons happen in America/New_York time using zoneinfo (DST-aware).
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")

# v5.13.0 PR-5 SHARED-CUTOFF: new-position cutoff (was 15:30 ET in v5.12.0).
NEW_POSITION_CUTOFF_ET: time = time(15, 44, 59)

# v5.13.0 PR-5 SHARED-EOD: EOD flush (was 15:59:50 ET in v5.12.0).
EOD_FLUSH_ET: time = time(15, 49, 59)

# Hunt window: from regular-session open through SHARED-CUTOFF.
# v15.0 SPEC: Entry Window 09:36:00 to 15:44:59 EST. ORH/ORL freeze at 09:35:59;
# the earliest valid 2x 1m close above/below ORH/ORL completes on the 09:37 close,
# so the hunt window opens at 09:36:00 (one bar before the earliest possible fire).
HUNT_START_ET: time = time(9, 36, 0)
HUNT_END_ET: time = NEW_POSITION_CUTOFF_ET

# Regular session bounds — used by callers that need a market-hours predicate.
MARKET_OPEN_ET: time = time(9, 30, 0)
MARKET_CLOSE_ET: time = time(16, 0, 0)


def _to_et(now: datetime | None = None) -> datetime:
    """Convert *now* (UTC, naive, or any tz) to America/New_York.

    Naive datetimes are assumed to already be in ET (matches the existing
    ``_now_et`` helper in trade_genius.py). ``None`` means *right now*.
    """
    if now is None:
        return datetime.now(tz=ET)
    if now.tzinfo is None:
        # treat as ET — same convention as test fixtures
        return now.replace(tzinfo=ET)
    return now.astimezone(ET)


def is_after_cutoff_et(now: datetime | None = None) -> bool:
    """True iff *now* is at or after SHARED-CUTOFF (15:44:59 ET).

    At/after the cutoff, no NEW positions may be opened. Existing positions
    are unaffected — sentinel/ratchet manage them until SHARED-EOD.
    """
    et = _to_et(now)
    return et.timetz().replace(tzinfo=None) >= NEW_POSITION_CUTOFF_ET


def is_after_eod_et(now: datetime | None = None) -> bool:
    """True iff *now* is at or after SHARED-EOD (15:49:59 ET).

    At/after EOD, all open positions are force-closed regardless of
    sentinel/ratchet state.
    """
    et = _to_et(now)
    return et.timetz().replace(tzinfo=None) >= EOD_FLUSH_ET


def is_in_hunt_window(now: datetime | None = None) -> bool:
    """True iff *now* is inside the hunt window [HUNT_START_ET, HUNT_END_ET).

    SHARED-HUNT: unlimited hunting until 15:44:59 cutoff. We treat the cutoff
    as exclusive on the hunt side (entries blocked at exactly 15:44:59).
    """
    et = _to_et(now)
    t = et.timetz().replace(tzinfo=None)
    return HUNT_START_ET <= t < HUNT_END_ET


__all__ = [
    "ET",
    "NEW_POSITION_CUTOFF_ET",
    "EOD_FLUSH_ET",
    "HUNT_START_ET",
    "HUNT_END_ET",
    "MARKET_OPEN_ET",
    "MARKET_CLOSE_ET",
    "is_after_cutoff_et",
    "is_after_eod_et",
    "is_in_hunt_window",
]
