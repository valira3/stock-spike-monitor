"""v5.5.6 — unit tests for volume_profile.previous_session_bucket.

Asserts the helper returns the bucket key for the minute that JUST
closed at the given timestamp, with the same outside-session rules as
session_bucket. (Originally written for the shadow gate; kept in v5.14.0
because the helper is still consumed by other readers of the volume
baseline.)

Standalone (no pytest dep), matching the v5.5.5 test style:

    python test_v5_5_6_previous_session_bucket.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import volume_profile as vp  # noqa: E402

ET = vp.ET


def _ts(y, mo, d, h, m, s=0):
    return datetime(y, mo, d, h, m, s, tzinfo=ET)


# 2026-04-27 is a Monday and not a NYSE holiday — confirmed via the
# NYSE_HOLIDAYS frozen set in volume_profile.py.
TRADING_DAY = (2026, 4, 27)
WEEKEND_DAY = (2026, 4, 25)  # Saturday
HOLIDAY_DAY = (2026, 5, 25)  # Memorial Day 2026


def test_premarket_returns_none() -> None:
    # 09:00 ET is well before the first valid 09:31 bucket — the
    # just-closed minute (08:59) is also outside the session.
    assert vp.previous_session_bucket(_ts(*TRADING_DAY, 9, 0, 0)) is None
    assert vp.previous_session_bucket(_ts(*TRADING_DAY, 9, 30, 0)) is None
    # Edge: 09:31:00 — the just-closed minute is 09:30, which sits BELOW
    # REGULAR_OPEN (09:31) so the helper returns None. This matches the
    # actual session_bucket semantics: '0930' is an excluded auction bar.
    assert vp.previous_session_bucket(_ts(*TRADING_DAY, 9, 31, 0)) is None


def test_first_real_bucket() -> None:
    # 09:32:00 ET — the 09:31 minute just closed. Its bucket is '0931'.
    assert vp.previous_session_bucket(_ts(*TRADING_DAY, 9, 32, 0)) == "0931"
    assert vp.previous_session_bucket(_ts(*TRADING_DAY, 9, 32, 30)) == "0931"


def test_midmorning_buckets() -> None:
    # 10:27:30 ET — task spec example. The 10:26 minute closed at 10:27.
    assert vp.previous_session_bucket(_ts(*TRADING_DAY, 10, 27, 30)) == "1026"
    # 10:28:00 — the 10:27 minute closed at 10:28.
    assert vp.previous_session_bucket(_ts(*TRADING_DAY, 10, 28, 0)) == "1027"
    # 09:35:30 — task spec asserts '0934'.
    assert vp.previous_session_bucket(_ts(*TRADING_DAY, 9, 35, 30)) == "0934"


def test_close_edges() -> None:
    # 16:00:00 ET — the just-closed minute is 15:59, which is the last
    # valid bucket.
    assert vp.previous_session_bucket(_ts(*TRADING_DAY, 16, 0, 0)) == "1559"
    # 16:00:30 — same bucket '1559' (we floor and subtract one minute).
    assert vp.previous_session_bucket(_ts(*TRADING_DAY, 16, 0, 30)) == "1559"
    # 16:01:00 — the 16:00 minute is OUTSIDE the regular session
    # (REGULAR_CLOSE is exclusive at 16:00). previous_session_bucket
    # returns None.
    assert vp.previous_session_bucket(_ts(*TRADING_DAY, 16, 1, 0)) is None


def test_weekend_returns_none() -> None:
    assert vp.previous_session_bucket(_ts(*WEEKEND_DAY, 10, 30, 0)) is None


def test_holiday_returns_none() -> None:
    assert vp.previous_session_bucket(_ts(*HOLIDAY_DAY, 10, 30, 0)) is None


def test_naive_datetime_returns_none() -> None:
    naive = datetime(2026, 4, 27, 10, 30, 0)  # tzinfo=None
    assert vp.previous_session_bucket(naive) is None


def test_walks_30s_steps_match_just_closed() -> None:
    """Across a slice of the trading day, the helper should always
    return either None (outside session) or a bucket label
    corresponding to the minute that closed strictly before ts."""
    base = _ts(*TRADING_DAY, 9, 30, 0)
    for step_sec in range(0, 60 * 60 * 7, 30):  # 7 hours, 30s steps
        ts = base.replace(
            hour=9 + (step_sec // 3600),
            minute=(30 + (step_sec % 3600) // 60) % 60
            if step_sec < 1800
            else (30 + (step_sec % 3600) // 60) % 60,
        )
        # Easier: build via timedelta.
        from datetime import timedelta

        ts = base + timedelta(seconds=step_sec)
        got = vp.previous_session_bucket(ts)
        if got is None:
            continue
        # The just-closed minute is floor(ts) - 1 minute.
        floored = ts.replace(second=0, microsecond=0)
        prev = floored - timedelta(minutes=1)
        expected = vp.session_bucket(prev)
        assert got == expected, (ts, got, expected)


TESTS = [
    test_premarket_returns_none,
    test_first_real_bucket,
    test_midmorning_buckets,
    test_close_edges,
    test_weekend_returns_none,
    test_holiday_returns_none,
    test_naive_datetime_returns_none,
    test_walks_30s_steps_match_just_closed,
]


def main() -> int:
    fails = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  +  {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  X  {fn.__name__}: {type(e).__name__}: {e}")
    total = len(TESTS)
    print(f"\n  {total - fails} passed \u00b7 {fails} failed \u00b7 {total} total\n")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
