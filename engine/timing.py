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

# v5.13.0 PR-5 SHARED-EOD: EOD flush (was 15:59:50 ET in v5.12.0,
# moved earlier to 15:49:59 per Tiger Sovereign §3).
# v9.1.23 -- moved back to 15:59:59.
# v9.1.125 -- moved earlier to 15:57:00. The 2026-05-18 incident
# (V10 EOD scan-loop tick missed the 15:58 target and fired at
# 16:00:11 -- 12 SECONDS POST market close) demonstrated that a
# single safety-net at 15:59:59 leaves zero margin for scan-loop
# delays. New ordering:
#
#   15:56 ET     -- v10 EOD reversal engine's exit_et_minutes fires
#                   close via scan._eod_reversal_pass (scan-loop path).
#   15:56 ET     -- v10 ORB engine's eod_cutoff_minutes closes morning
#                   ORB positions via exits.py:EXIT_EOD.
#   15:57:00 ET  -- legacy paper-book safety-net (this constant) catches
#                   any straggler still in paper_state.positions after
#                   the V10 engines have run. Uses direct
#                   client.close_position() API which bypasses the
#                   notional cap (different code path from V10 close).
EOD_FLUSH_ET: time = time(15, 57, 0)

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
        # v7.8.4: route the now=None fallback through tg._now_et so the
        # replay clock applies. Defensive -- current callers always pass
        # an explicit datetime, but new callers may not.
        try:
            import sys as _sys

            _tg = _sys.modules.get("trade_genius") or _sys.modules.get("__main__")
            return _tg._now_et() if _tg is not None else datetime.now(tz=ET)
        except Exception:
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


def minutes_since_et_midnight(ts_utc) -> int:
    """Return minutes-since-midnight ET for a UTC timestamp.

    Handles DST automatically via zoneinfo("America/New_York"). Used by
    the v10 ORB live runtime (orb/live_runtime.py:feed_bar) to compute
    the bar-bucket integer that the OR window add_bar() expects.

    Args:
        ts_utc: a timezone-aware datetime (UTC) OR a UNIX timestamp
            (int/float seconds since epoch). Naive datetimes are
            treated as UTC.

    Returns:
        int: hour*60 + minute in ET. Range [0, 1440).

    Look-ahead audit: pure conversion of an already-known timestamp.
    No future data consulted.
    """
    if isinstance(ts_utc, (int, float)):
        dt = datetime.fromtimestamp(int(ts_utc), tz=timezone.utc)
    elif isinstance(ts_utc, datetime):
        if ts_utc.tzinfo is None:
            dt = ts_utc.replace(tzinfo=timezone.utc)
        else:
            dt = ts_utc
    else:
        raise TypeError(f"unexpected ts type: {type(ts_utc).__name__}")
    et = dt.astimezone(ET)
    return et.hour * 60 + et.minute


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
    "minutes_since_et_midnight",
]
