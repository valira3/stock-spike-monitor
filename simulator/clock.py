"""simulator.clock -- frozen wall-clock that advances on command.

The bot reads "now" via several paths:

  - datetime.now() / datetime.utcnow()
  - datetime.now(timezone.utc)
  - datetime.now(ZoneInfo("America/New_York"))
  - time.time() / time.monotonic()
  - trade_genius._now_et() / _now_cdt() (already wrapped)

SimulatedClock patches all of them at module level. Once installed, the
caller advances virtual time by minutes or seconds; real wall-clock is
ignored.

Usage:
    from simulator.clock import SimulatedClock
    c = SimulatedClock.at_et(date="2026-05-15", hour=9, minute=29)
    c.install()
    # ... run scenario, advance() between bars ...
    c.advance(minutes=1)
    c.uninstall()
"""
from __future__ import annotations

import time as _time_mod
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo


_NY = ZoneInfo("America/New_York")
_CHI = ZoneInfo("America/Chicago")


class SimulatedClock:
    """Module-level monkeypatch of datetime.now / time.time."""

    def __init__(self, t0_utc: datetime):
        if t0_utc.tzinfo is None:
            t0_utc = t0_utc.replace(tzinfo=timezone.utc)
        self._t = t0_utc.astimezone(timezone.utc)
        self._orig: dict = {}

    # ---- factories ------------------------------------------------------

    @classmethod
    def at_et(cls, date: str, hour: int, minute: int) -> "SimulatedClock":
        y, m, d = date.split("-")
        dt = datetime(int(y), int(m), int(d), hour, minute, 0, tzinfo=_NY)
        return cls(dt.astimezone(timezone.utc))

    @classmethod
    def at_utc(cls, dt: datetime) -> "SimulatedClock":
        return cls(dt)

    # ---- accessors ------------------------------------------------------

    @property
    def now_utc(self) -> datetime:
        return self._t

    @property
    def now_et(self) -> datetime:
        return self._t.astimezone(_NY)

    @property
    def now_ct(self) -> datetime:
        return self._t.astimezone(_CHI)

    def bucket_min(self) -> int:
        """Minutes-since-ET-midnight (matches engine.timing convention)."""
        et = self.now_et
        return et.hour * 60 + et.minute

    # ---- advance --------------------------------------------------------

    def advance(self, seconds: int = 0, minutes: int = 0, hours: int = 0) -> None:
        from datetime import timedelta

        self._t = self._t + timedelta(seconds=seconds, minutes=minutes, hours=hours)

    def set_et(self, hour: int, minute: int) -> None:
        et = self.now_et
        new_et = et.replace(hour=hour, minute=minute, second=0, microsecond=0)
        self._t = new_et.astimezone(timezone.utc)

    # ---- patch / unpatch -----------------------------------------------

    def install(self) -> None:
        """Patch datetime.now and time.time / time.monotonic at module
        level. Returns previously-bound originals so uninstall() can
        restore them.

        This replaces the *bound methods* on the datetime class via a
        subclass wrapper. Anything that did `from datetime import
        datetime` before install() will hold a reference to the real
        class -- we patch the module attribute so fresh `datetime.now()`
        calls (the common pattern) see the wrapper.
        """
        if self._orig:
            return  # already installed
        import datetime as _dt

        clock = self

        class _SimDateTime(_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return clock.now_utc.replace(tzinfo=None)
                return clock.now_utc.astimezone(tz)

            @classmethod
            def utcnow(cls):
                return clock.now_utc.replace(tzinfo=None)

        self._orig["datetime"] = _dt.datetime
        _dt.datetime = _SimDateTime

        self._orig["time.time"] = _time_mod.time
        self._orig["time.monotonic"] = _time_mod.monotonic
        self._orig["time.sleep"] = _time_mod.sleep
        _time_mod.time = lambda: clock.now_utc.timestamp()
        _time_mod.monotonic = lambda: clock.now_utc.timestamp()
        # Make time.sleep a no-op in simulator mode (the runner advances
        # the clock explicitly per scenario step).
        _time_mod.sleep = lambda secs: None

    def uninstall(self) -> None:
        if not self._orig:
            return
        import datetime as _dt

        _dt.datetime = self._orig["datetime"]
        _time_mod.time = self._orig["time.time"]
        _time_mod.monotonic = self._orig["time.monotonic"]
        _time_mod.sleep = self._orig["time.sleep"]
        self._orig.clear()


def install_for_scenario(date: str, hour: int = 9, minute: int = 25) -> SimulatedClock:
    """Convenience factory: ET premarket time, ready for a session run."""
    clock = SimulatedClock.at_et(date=date, hour=hour, minute=minute)
    clock.install()
    return clock
