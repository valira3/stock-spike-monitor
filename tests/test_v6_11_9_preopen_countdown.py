"""v6.11.9 \u2014 pre-open scan loop must stamp tg._last_scan_time so the
dashboard's "next scan in Ns" countdown ticks during the 8:00\u20139:35 ET
warm-up window. Before v6.11.9, _last_scan_time was only stamped after
9:35 ET, so during pre-market the dashboard rendered "\u267b --" and
Val could not tell whether the bot was alive.

Two checks:

1. ``test_preopen_stamps_last_scan_time`` \u2014 invoke ``engine.scan.scan_loop``
   with a mock callbacks impl during the 8:30 ET pre-open window and
   assert ``trade_genius._last_scan_time`` is set to a recent UTC
   timestamp.

2. ``test_after_close_does_not_stamp`` \u2014 same harness at 17:00 ET (after
   close) must NOT stamp _last_scan_time, so the countdown correctly
   shows "\u267b --" outside trading hours.
"""

from __future__ import annotations

import os

# Required for trade_genius import — stay consistent with the rest of
# the test suite (see test_v6_11_4_premarket_paths.py).
os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("FMP_API_KEY", "test_key_for_ci")
os.environ.setdefault("TELEGRAM_TOKEN", "123:test")
os.environ.setdefault("CHAT_ID", "999")

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest


def _make_callbacks():
    """Minimal ``EngineCallbacks`` mock that lets scan_loop run cleanly
    through the pre-open archive branch without doing real I/O."""
    cb = MagicMock()
    cb.fetch_1min_bars.return_value = None  # archive branch tolerates None
    return cb


def _et(hour: int, minute: int = 0) -> datetime:
    """Make a Mon\u2013Fri ET datetime at the given (hour, minute). 2026-05-04
    is a Monday so weekday() == 0 < 5, exercising the daily branch."""
    return datetime(2026, 5, 4, hour, minute, 0, tzinfo=ZoneInfo("America/New_York"))


@pytest.fixture
def reset_last_scan_time():
    import trade_genius as tg
    prev = tg._last_scan_time
    tg._last_scan_time = None
    yield
    tg._last_scan_time = prev


def test_preopen_stamps_last_scan_time(reset_last_scan_time):
    """At 08:30 ET, scan_loop's pre-open branch must stamp
    tg._last_scan_time so /api/state.gates.next_scan_sec is non-None
    and the countdown pill ticks in the dashboard."""
    import trade_genius as tg
    import engine.scan as engine_scan

    cb = _make_callbacks()
    cb.now_et.return_value = _et(8, 30)

    before = datetime.now(timezone.utc)
    with patch.object(tg, "_refresh_market_mode", return_value=None):
        engine_scan.scan_loop(cb)
    after = datetime.now(timezone.utc)

    assert tg._last_scan_time is not None, (
        "v6.11.9 regression: pre-open scan loop did not stamp "
        "_last_scan_time \u2014 dashboard countdown will read '\u267b --' all "
        "morning."
    )
    assert before <= tg._last_scan_time <= after, (
        "stamped _last_scan_time should be within this test's UTC window"
    )


def test_rth_still_stamps_last_scan_time(reset_last_scan_time):
    """Sanity: existing RTH stamping (post-9:35 ET) is unaffected."""
    import trade_genius as tg
    import engine.scan as engine_scan

    cb = _make_callbacks()
    cb.now_et.return_value = _et(10, 0)  # 10:00 ET, well into RTH

    # The full RTH branch touches a lot of state; we only need to know
    # the stamp happens. Patch out the heavier callbacks paths by
    # short-circuiting at the symbol-loop boundary.
    with patch.object(tg, "_refresh_market_mode", return_value=None), \
         patch.object(tg, "TRADE_TICKERS", []), \
         patch.object(tg, "_clear_cycle_bar_cache", return_value=None), \
         patch.object(engine_scan, "logger"):
        try:
            engine_scan.scan_loop(cb)
        except Exception:
            # The RTH branch may bail later when production-only globals
            # aren't fully wired in this lightweight harness; the only
            # invariant we care about here is the early stamp.
            pass

    assert tg._last_scan_time is not None, (
        "RTH path must still stamp _last_scan_time (existing behaviour)."
    )


def test_after_close_does_not_stamp(reset_last_scan_time):
    """At 17:00 ET (after RTH close), the loop should return early and
    leave tg._last_scan_time unchanged, so the dashboard correctly
    shows '\u267b --' overnight rather than a frozen countdown."""
    import trade_genius as tg
    import engine.scan as engine_scan

    cb = _make_callbacks()
    cb.now_et.return_value = _et(17, 0)

    with patch.object(tg, "_refresh_market_mode", return_value=None):
        engine_scan.scan_loop(cb)

    assert tg._last_scan_time is None, (
        "after-close scan_loop must not stamp _last_scan_time \u2014 the "
        "countdown should read '\u267b --' overnight."
    )
