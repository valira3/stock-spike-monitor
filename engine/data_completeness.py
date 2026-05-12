"""v8.3.2 -- SIP completeness helpers for trade_genius._fetch_1min_bars_alpaca.

Used to detect thin SIP results and merge with an IEX retry. Pre-v8.3.2
the SIP -> IEX fallback only fired on zero bars; transient feed glitches
that delivered 9 of 30 OR-window bars slipped through, locking the OR
window with bars_seen < 15 and blocking entries for the rest of the day.

Pure functions: no I/O, no alpaca-py dependency (Bar objects accessed
via duck-typed getattr). Tests in tests/strategy/test_sip_completeness.py
exercise these directly with dummy bar objects.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# OR window is hardcoded to 09:30-09:59 ET (30 bars). If config changes,
# adjust here too. Match orb.engine.OrbConfig.{session_start_minutes,
# or_end_minutes}.
OR_START_MIN_ET = 9 * 60 + 30   # 570
OR_END_MIN_ET = 10 * 60          # 600


def _or_expected_bars(now_et: datetime) -> int:
    """How many in-OR-window bars we expect the data feed to have given
    the current ET clock.

    Returns 0 pre-09:30, the partial count during the active OR, and 30
    once the OR window has closed (any time after 10:00).
    """
    cur_min = now_et.hour * 60 + now_et.minute
    if cur_min < OR_START_MIN_ET:
        return 0
    if cur_min < OR_END_MIN_ET:
        return max(0, cur_min - OR_START_MIN_ET)
    return OR_END_MIN_ET - OR_START_MIN_ET  # 30


def _count_alpaca_rows_in_or_window(rows, et) -> int:
    """Count duck-typed Bar objects whose ET timestamp falls in
    [09:30, 10:00) ET. Bars without a parseable timestamp are skipped.

    `rows` is whatever alpaca-py returns from response.data[<symbol>]:
    each element has a `.timestamp` attribute (UTC datetime, sometimes
    naive). `et` is the target ET zoneinfo (passed in to avoid re-
    constructing per call).
    """
    n = 0
    for b in rows:
        ts = getattr(b, "timestamp", None)
        if ts is None:
            continue
        if getattr(ts, "tzinfo", None) is None:
            try:
                ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                continue
        try:
            ts_et = ts.astimezone(et)
        except Exception:
            continue
        bucket = ts_et.hour * 60 + ts_et.minute
        if OR_START_MIN_ET <= bucket < OR_END_MIN_ET:
            n += 1
    return n


def _is_or_coverage_thin(got: int, expected: int,
                         *, hard: bool = False) -> bool:
    """True when the in-OR-window bar count is suspiciously low.

    `hard=False` (default): fire the IEX retry when coverage is < 80%
    of expected. Tolerant of normal sparse-trading-minute behavior on
    pre-RTH minutes.

    `hard=True`: fire the Yahoo fallback when coverage is < 50% of
    expected even after the IEX merge. Only triggered when even the
    consolidated SIP+IEX result is structurally broken.

    No-op (returns False) when expected < 5 -- too small a window to
    distinguish noise from real gaps. Also False when got >= expected
    (over-supply is fine; e.g. duplicate timestamps from merge).
    """
    if expected < 5:
        return False
    threshold = 0.5 if hard else 0.8
    return got < int(expected * threshold)


def _merge_alpaca_rows_by_timestamp(*row_lists) -> list:
    """Merge alpaca-py Bar lists by UNIX timestamp; one bar per minute,
    earliest source wins on duplicate. Returns oldest-first.

    Used by the SIP -> IEX merge in _fetch_1min_bars_alpaca: SIP rows
    come first, IEX fills the gaps. The earliest-source-wins policy
    keeps SIP (consolidated tape) as the source of truth for any
    minute where it had data; IEX-only minutes are imported as-is.

    Rows without a parseable .timestamp are silently dropped.
    """
    seen: dict = {}
    for rows in row_lists:
        if not rows:
            continue
        for b in rows:
            ts = getattr(b, "timestamp", None)
            if ts is None:
                continue
            if getattr(ts, "tzinfo", None) is None:
                try:
                    ts = ts.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
            try:
                key = int(ts.timestamp())
            except (TypeError, ValueError, AttributeError):
                continue
            if key not in seen:
                seen[key] = b
    return [seen[k] for k in sorted(seen.keys())]
