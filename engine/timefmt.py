"""ET / CDT time helpers carved from trade_genius.py in v10.0.1.

Pure utility module: zero dependencies on trade_genius. Callers can
`from engine.timefmt import _now_et` directly, OR keep importing the
old `tg._now_et` -- trade_genius re-exports every name here.

What lives in this module:
  Zone constants:
    ET   -- America/New_York   (market clock; all gate logic)
    CDT  -- America/Chicago    (legacy CDT user-display channel,
                                still consumed by Telegram surface;
                                dashboard moved to ET in v7.89.0)

  Live clock readers (current time, fresh each call):
    _now_et()        -> tz-aware datetime in ET
    _now_cdt()       -> tz-aware datetime in CDT
    _utc_now_iso()   -> str  ("2026-05-19T22:30:00.123456+00:00")

  ISO timestamp formatters (str -> "HH:MM <ZONE>"):
    _to_cdt_hhmm(iso)    -> "HH:MM CDT" (legacy ET-stored values
                           are auto-treated as ET for the convert)
    _to_cdt_hhmmss(iso)  -> "HH:MM:SS"  (CDT)
    _to_et_hhmm(iso)     -> "HH:MM ET"
    _to_et_hhmmss(iso)   -> "HH:MM:SS"  (ET)
    _parse_time_to_cdt(ts)  -- lenient parser for legacy / mixed
                               timestamp shapes -> "HH:MM"

  Date predicates / parsers:
    _is_today(iso_str)              -> bool  (ET trading day)
    _parse_date_arg(args)           -> date  (Telegram /perf style:
                                      "yesterday", "Apr 17", "Mon",
                                      "YYYY-MM-DD", "<N>" days)

History. These lived in trade_genius.py from v3.x through v9.1.140
(99 call sites across the codebase). The carve to a focused module
makes them independently testable + removes ~160 LOC from the
monolith. No behavior change.
"""
from __future__ import annotations

import datetime as _dt
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
CDT = ZoneInfo("America/Chicago")   # user display timezone


def _now_et() -> datetime:
    """Current time in ET -- for market-hour gate logic only."""
    return datetime.now(timezone.utc).astimezone(ET)


def _now_cdt() -> datetime:
    """Current time in CDT -- for all user-facing display."""
    return datetime.now(timezone.utc).astimezone(CDT)


def _utc_now_iso() -> str:
    """UTC ISO timestamp string for internal storage."""
    return datetime.now(timezone.utc).isoformat()


def _to_cdt_hhmm(iso_str: str) -> str:
    """Decode a stored ISO timestamp to 'HH:MM CDT' for display.
    Handles both UTC-stored (new) and ET-stored (legacy) strings."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)   # legacy ET-stored fallback
        return dt.astimezone(CDT).strftime("%H:%M CDT")
    except Exception:
        return iso_str


def _to_cdt_hhmmss(iso_str: str) -> str:
    """Decode a stored ISO timestamp to 'HH:MM:SS' (CDT) for display."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)
        return dt.astimezone(CDT).strftime("%H:%M:%S")
    except Exception:
        return iso_str


# v7.89.0 -- ET-zoned twins of the CDT helpers above. The dashboard
# (and the broker order labels it consumes) now render times in ET
# instead of CT so the web UI matches the market clock everywhere.
# The CDT helpers above stay in place because the Telegram surface
# still consumes them; they'll migrate in a follow-up release.
def _to_et_hhmm(iso_str: str) -> str:
    """Decode a stored ISO timestamp to 'HH:MM ET' for display."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)
        return dt.astimezone(ET).strftime("%H:%M ET")
    except Exception:
        return iso_str


def _to_et_hhmmss(iso_str: str) -> str:
    """Decode a stored ISO timestamp to 'HH:MM:SS' (ET) for display."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)
        return dt.astimezone(ET).strftime("%H:%M:%S")
    except Exception:
        return iso_str


def _parse_time_to_cdt(ts):
    """Normalise any stored timestamp format to HH:MM CDT."""
    if not ts:
        return "??:??"
    ts = str(ts).strip()
    # ISO format with timezone offset (stored as UTC)
    if "T" in ts and ("+" in ts or ts.endswith("Z")):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            cdt_dt = dt.astimezone(CDT)
            return cdt_dt.strftime("%H:%M")
        except Exception:
            pass
    # HH:MM:SS or HH:MM -- already local (CDT), just truncate
    parts = ts.split(":")
    if len(parts) >= 2:
        return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}"
    return ts[:5]


def _is_today(ts_str: str) -> bool:
    """Check if an ISO timestamp string is from today (ET-based)."""
    if not ts_str:
        return False
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        today_et = _now_et().date()
        return dt.astimezone(ET).date() == today_et
    except Exception:
        return False


def _parse_date_arg(args):
    """Parse optional date argument from command args. Returns date in ET."""
    today = _now_et().date()
    if not args:
        return today
    raw = " ".join(args).strip().lower()
    if raw == "yesterday":
        d = today - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d
    # Try YYYY-MM-DD
    try:
        return _dt.date.fromisoformat(raw)
    except ValueError:
        pass
    # Try integer = last N days (for /perf)
    try:
        n = int(raw)
        if 1 <= n <= 365:
            return today - timedelta(days=n)
    except ValueError:
        pass
    # Try "Apr 17" or "April 17"
    for fmt in ["%b %d", "%B %d"]:
        try:
            parsed = _dt.datetime.strptime(raw, fmt)
            return parsed.replace(year=today.year).date()
        except ValueError:
            pass
    # Try weekday names
    days_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    for abbr, num in days_map.items():
        if raw.startswith(abbr):
            delta = (today.weekday() - num) % 7
            if delta == 0:
                delta = 7
            return today - timedelta(days=delta)
    return today  # fallback
