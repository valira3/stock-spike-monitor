"""FrozenClock — deterministic time control for the synthetic harness.

Drives trade_genius._now_et / _now_cdt / _utc_now_iso. Also intercepts
datetime.datetime.now(timezone.utc) used by close paths to stamp
_last_exit_time and exit_time_iso.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CDT = ZoneInfo("America/Chicago")


class FrozenClock:
    """Wall-clock the harness can advance step-by-step.

    Set the current ET datetime via set_time(); read it through the
    same accessors trade_genius.py calls (now_et / now_cdt /
    utc_now_iso). Underlying storage is a UTC-aware datetime.
    """

    def __init__(self, ts_et: datetime | None = None) -> None:
        if ts_et is None:
            ts_et = datetime(2026, 4, 24, 10, 0, 0, tzinfo=ET)
        self._utc = ts_et.astimezone(timezone.utc)

    def set_time(self, ts_et: datetime) -> None:
        if ts_et.tzinfo is None:
            ts_et = ts_et.replace(tzinfo=ET)
        self._utc = ts_et.astimezone(timezone.utc)

    def tick_seconds(self, n: int) -> None:
        self._utc = self._utc + timedelta(seconds=n)

    def tick_minutes(self, n: int) -> None:
        self.tick_seconds(n * 60)

    def now_utc(self) -> datetime:
        return self._utc

    def now_et(self) -> datetime:
        return self._utc.astimezone(ET)

    def now_cdt(self) -> datetime:
        return self._utc.astimezone(CDT)

    def utc_now_iso(self) -> str:
        return self._utc.isoformat()


def make_frozen_datetime_class(clock: FrozenClock):
    """Return a subclass of datetime whose .now(tz) reads `clock`.

    Used to monkeypatch trade_genius.datetime so that
    `datetime.now(timezone.utc)` (called for _last_exit_time etc.)
    becomes deterministic.
    """

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return clock.now_utc().replace(tzinfo=None)
            return clock.now_utc().astimezone(tz)

    return _FrozenDatetime
