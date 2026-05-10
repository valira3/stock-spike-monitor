"""v7.8.0 \u2014 Opening-bell entry-delay flag module (Filter #8).

Single source of truth for the v7.8.0 Opening Delay filter.

Motivation: live trading on 2026-05-08 showed the 09:30-10:00 ET window
took $-90.78 on 7 trades at 14.3% WR (5 Alarm-A stops, 2 v750 fires,
1 chandelier winner). 09:30-09:45 specifically was the worst sub-window:
AAPL 09:37 (+36 mfe \u2192 -$14 stop), GOOG 09:46 (-$7 immediate stop),
TSLA 09:52 (v750 fire). The opening-bar fakeout pattern is well-known
across the wider tape; the 84-day forensic also flagged 09:30-10:00 as
profitable IN AGGREGATE but heavily lumpy with first-15min toxicity.

Filter #8 blocks new entries whose entry timestamp is BEFORE
V780_OPENING_DELAY_UNTIL_ET (default 09:45 ET). It deliberately uses
ET wall-clock time so it tracks DST automatically.

Defaults: 09:45 ET cutoff, ENABLED by default.
"""
from __future__ import annotations
import os
from datetime import datetime, time, timezone
from typing import Optional, Tuple

# Lazy import zoneinfo \u2014 it's stdlib in 3.9+ but we keep the module
# importable even if zoneinfo can't resolve America/New_York at import
# time (e.g. in stripped containers). Fallback below uses a fixed -4
# offset which is correct EDT-only; production has tzdata.
try:
    from zoneinfo import ZoneInfo
    _NY = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover \u2014 defensive, not expected
    _NY = None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y", "t")


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip()


def _parse_hhmm(s: str) -> time:
    """Parse 'HH:MM' to a datetime.time. Raises ValueError on bad input."""
    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError(f"bad HH:MM string: {s!r}")
    hh = int(parts[0])
    mm = int(parts[1])
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(f"out-of-range HH:MM: {s!r}")
    return time(hh, mm)


# Master switch. Default OFF \u2014 promote to ON via Railway env var only.
V780_OPENING_DELAY_ENABLED: bool = _env_bool("V780_OPENING_DELAY_ENABLED", False)

# Cutoff in ET wall-clock. Entries whose ET timestamp is strictly BEFORE
# this time are blocked. Default 09:45 ET (skip first 15min of RTH).
_RAW_CUTOFF: str = _env_str("V780_OPENING_DELAY_UNTIL_ET", "09:45")
try:
    V780_OPENING_DELAY_UNTIL_ET: time = _parse_hhmm(_RAW_CUTOFF)
except ValueError:
    # Defensive: fall through to default if env var is malformed.
    V780_OPENING_DELAY_UNTIL_ET = time(9, 45)

SKIP_REASON_V780_OPENING_DELAY: str = "v780_opening_delay"


def _to_et(dt: datetime) -> datetime:
    """Convert any tz-aware (or naive-as-UTC) datetime to ET."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if _NY is not None:
        return dt.astimezone(_NY)
    # Fallback: assume EDT (-4) when zoneinfo is unavailable. This is
    # acceptable in production where tzdata is installed and _NY is
    # always set; the fallback only matters for unit tests.
    return dt.astimezone(timezone.utc).replace(tzinfo=timezone(_offset_edt()))


def _offset_edt():
    from datetime import timedelta as _td
    return _td(hours=-4)


def is_before_open_delay(now_utc=None) -> Tuple[bool, Optional[str]]:
    """Return (blocked, et_time_str) for entries fired at now_utc.

    blocked is True iff V780_OPENING_DELAY_ENABLED is set AND the ET
    wall-clock time of now_utc is strictly before V780_OPENING_DELAY_UNTIL_ET.
    et_time_str is the formatted ET time for telemetry.
    """
    if not V780_OPENING_DELAY_ENABLED:
        return (False, None)
    if now_utc is None:
        # v7.8.4: route the now=None fallback through tg._now_utc so the
        # replay clock applies. Defensive -- production callers pass an
        # explicit ts.
        try:
            import sys as _sys
            _tg = _sys.modules.get("trade_genius") or _sys.modules.get("__main__")
            dt_utc = _tg._now_utc() if _tg is not None else datetime.now(timezone.utc)
        except Exception:
            dt_utc = datetime.now(timezone.utc)
    elif isinstance(now_utc, datetime):
        dt_utc = now_utc if now_utc.tzinfo else now_utc.replace(tzinfo=timezone.utc)
    else:
        try:
            s = str(now_utc).replace("Z", "+00:00")
            dt_utc = datetime.fromisoformat(s)
            if dt_utc.tzinfo is None:
                dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return (False, None)
    et = _to_et(dt_utc)
    et_t = et.time()
    et_str = et.strftime("%H:%M:%S")
    if et_t < V780_OPENING_DELAY_UNTIL_ET:
        return (True, et_str)
    return (False, et_str)


__all__ = [
    "V780_OPENING_DELAY_ENABLED",
    "V780_OPENING_DELAY_UNTIL_ET",
    "SKIP_REASON_V780_OPENING_DELAY",
    "is_before_open_delay",
]
