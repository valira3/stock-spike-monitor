"""v6.15.5 regression tests \u2014 clock-aware bar archive.

Covers four leak surfaces eliminated in v6.15.5:

1. ``bar_archive._today_str`` falls back to wall clock when no replay
   harness is present (legacy / production behaviour preserved).
2. ``bar_archive._today_str`` returns the BacktestClock simulated date
   when ``trade_genius._now_utc`` is patched onto a clock-like object
   (replay behaviour).
3. ``bar_archive.write_bar`` partitions writes by simulated date in
   replay so live-loop bar persistence does not pile into the wall-clock
   directory.
4. ``volume_bucket._read_bars_for_day`` filters out future-dated bars
   when running under the replay harness, even if the seed archive
   contains them. Belt-and-braces guard against any future code path
   that lands a future bar in the archive.

Also covers the lifecycle / env wiring fix:

5. ``broker.lifecycle.eod_close`` no longer hardcodes retain_days=90
   \u2014 it reads ``bar_archive.DEFAULT_RETAIN_DAYS`` so
   ``BAR_ARCHIVE_RETAIN_DAYS=9999`` actually keeps long-history seeds.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


class _FakeClock:
    """Minimal BacktestClock-shaped object.

    The harness check uses ``hasattr(clock, 'now')`` plus
    ``getattr(fn, '__self__')`` so any object with a ``now`` attribute
    is treated as a clock. ``now_utc`` is the bound method we install
    onto ``trade_genius._now_utc``.
    """

    def __init__(self, when: datetime) -> None:
        self.now = when

    def now_utc(self) -> datetime:
        return self.now


class TestReplayClockToday(unittest.TestCase):
    def setUp(self) -> None:
        # Always import fresh so module-level _replay_clock_today picks
        # up the current trade_genius state.
        if "bar_archive" in sys.modules:
            importlib.reload(sys.modules["bar_archive"])
        import bar_archive  # noqa: F401
        self.bar_archive = sys.modules["bar_archive"]

    def test_no_harness_falls_back_to_wall_clock(self) -> None:
        """Outside replay, _replay_clock_today returns None and
        _today_str uses datetime.utcnow().date()."""
        # Ensure trade_genius._now_utc, if loaded, is the unpatched
        # free function (no __self__).
        tg = sys.modules.get("trade_genius")
        if tg is not None:
            # The free function _now_utc has no __self__.
            self.assertIsNone(getattr(tg._now_utc, "__self__", None))
        result = self.bar_archive._replay_clock_today()
        self.assertIsNone(result)
        # _today_str(None) should still work and return a YYYY-MM-DD
        # string (no exception).
        s = self.bar_archive._today_str(None)
        self.assertRegex(s, r"^\d{4}-\d{2}-\d{2}$")

    def test_explicit_today_arg_overrides_everything(self) -> None:
        """Passing today= directly bypasses the harness path entirely."""
        s = self.bar_archive._today_str(date(2026, 4, 30))
        self.assertEqual(s, "2026-04-30")

    def test_clock_patched_returns_simulated_today(self) -> None:
        """When trade_genius._now_utc is bound to a clock-like object,
        _replay_clock_today returns clock.now.date()."""
        clock = _FakeClock(datetime(2026, 4, 30, 14, 35, tzinfo=timezone.utc))
        # Build a mock trade_genius module exposing _now_utc as the
        # bound method (has __self__ pointing at clock with .now).
        fake_tg = mock.MagicMock()
        fake_tg._now_utc = clock.now_utc
        with mock.patch.dict(sys.modules, {"trade_genius": fake_tg}):
            result = self.bar_archive._replay_clock_today()
            self.assertEqual(result, date(2026, 4, 30))

    def test_today_str_uses_replay_clock_when_patched(self) -> None:
        clock = _FakeClock(datetime(2026, 4, 29, 9, 35, tzinfo=timezone.utc))
        fake_tg = mock.MagicMock()
        fake_tg._now_utc = clock.now_utc
        with mock.patch.dict(sys.modules, {"trade_genius": fake_tg}):
            self.assertEqual(self.bar_archive._today_str(None), "2026-04-29")


class TestWriteBarReplayClockPartition(unittest.TestCase):
    def test_write_bar_uses_simulated_date_under_replay(self) -> None:
        """End-to-end: under the replay harness, write_bar lands in
        bars/<replay_date>/ \u2014 not bars/<wall_clock>/."""
        if "bar_archive" in sys.modules:
            importlib.reload(sys.modules["bar_archive"])
        import bar_archive

        clock = _FakeClock(datetime(2026, 3, 15, 14, 35, tzinfo=timezone.utc))
        fake_tg = mock.MagicMock()
        fake_tg._now_utc = clock.now_utc

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(sys.modules, {"trade_genius": fake_tg}):
                bar = {
                    "ts": "2026-03-15T14:35:00Z",
                    "et_bucket": "1035",
                    "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.2,
                    "iex_volume": 12345, "total_volume": 50000,
                }
                fp = bar_archive.write_bar("AAPL", bar, base_dir=td)
                self.assertIsNotNone(fp)
                # The write must land at the simulated 2026-03-15 dir,
                # not at today's wall-clock dir.
                expected = Path(td) / "2026-03-15" / "AAPL.jsonl"
                self.assertTrue(expected.exists(),
                                f"expected {expected} to exist")
                wall_today = datetime.utcnow().date().strftime("%Y-%m-%d")
                # Only fail if wall date differs from simulated date \u2014
                # otherwise the assertion is vacuous.
                if wall_today != "2026-03-15":
                    wall_path = Path(td) / wall_today / "AAPL.jsonl"
                    self.assertFalse(wall_path.exists(),
                                     f"leak detected at {wall_path}")


class TestVolumeBucketFutureBarFilter(unittest.TestCase):
    """Defence-in-depth: even if a future-dated bar lands in the
    archive (seed contamination, wall-clock write_bar from older
    code, etc.), the volume baseline reader must drop it when running
    under the replay harness.
    """

    def setUp(self) -> None:
        if "volume_bucket" in sys.modules:
            importlib.reload(sys.modules["volume_bucket"])
        import volume_bucket  # noqa: F401
        self.vb = sys.modules["volume_bucket"]

    def _write_bars(self, base: Path, day: date, ticker: str,
                    bars: list[dict]) -> None:
        d = base / day.strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        with open(d / f"{ticker}.jsonl", "w", encoding="utf-8") as fh:
            for b in bars:
                fh.write(json.dumps(b) + "\n")

    def test_no_harness_passes_all_bars_through(self) -> None:
        """Production / non-replay: every bar in the file is yielded."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            day = date(2026, 4, 28)
            self._write_bars(base, day, "AAPL", [
                {"ts": "2026-04-28T13:30:00Z", "et_bucket": "0930",
                 "iex_volume": 100, "total_volume": 1000},
                {"ts": "2026-04-28T13:31:00Z", "et_bucket": "0931",
                 "iex_volume": 200, "total_volume": 2000},
            ])
            # Make sure we are NOT under a fake harness here.
            tg = sys.modules.get("trade_genius")
            if tg is not None:
                self.assertIsNone(getattr(tg._now_utc, "__self__", None))
            bars = list(self.vb._read_bars_for_day(str(base), day, "AAPL"))
            self.assertEqual(len(bars), 2)

    def test_future_dated_bars_dropped_under_replay(self) -> None:
        """Replay clock = 2026-04-29 09:35 UTC. A bar timestamped
        2026-04-30T13:30Z (next-day, future relative to clock) must
        be dropped even though the file is named 2026-04-28."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            day = date(2026, 4, 28)  # lookback day, on disk
            self._write_bars(base, day, "AAPL", [
                {"ts": "2026-04-28T13:30:00Z", "et_bucket": "0930",
                 "iex_volume": 100, "total_volume": 1000},
                # Poison: future bar that should be filtered out.
                {"ts": "2026-04-30T13:30:00Z", "et_bucket": "0930",
                 "iex_volume": 999999, "total_volume": 999999},
            ])
            clock = _FakeClock(
                datetime(2026, 4, 29, 13, 35, tzinfo=timezone.utc)
            )
            fake_tg = mock.MagicMock()
            fake_tg._now_utc = clock.now_utc
            with mock.patch.dict(sys.modules, {"trade_genius": fake_tg}):
                bars = list(self.vb._read_bars_for_day(str(base), day, "AAPL"))
            self.assertEqual(len(bars), 1, f"got {bars!r}")
            self.assertEqual(bars[0]["iex_volume"], 100)

    def test_bars_without_ts_are_passed_through(self) -> None:
        """Legacy bars without a parsable ts must not be dropped \u2014
        we'd lose the entire baseline if ts parse failures filtered."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            day = date(2026, 4, 28)
            self._write_bars(base, day, "AAPL", [
                {"et_bucket": "0930", "iex_volume": 100,
                 "total_volume": 1000},  # no ts
                {"ts": "garbage", "et_bucket": "0931",
                 "iex_volume": 200, "total_volume": 2000},
            ])
            clock = _FakeClock(
                datetime(2026, 4, 29, 13, 35, tzinfo=timezone.utc)
            )
            fake_tg = mock.MagicMock()
            fake_tg._now_utc = clock.now_utc
            with mock.patch.dict(sys.modules, {"trade_genius": fake_tg}):
                bars = list(self.vb._read_bars_for_day(str(base), day, "AAPL"))
            self.assertEqual(len(bars), 2)

    def test_baseline_refresh_immune_to_future_seed(self) -> None:
        """Full integration: build a baseline whose lookback overlaps a
        seed file containing a future-dated bar with extreme volume.
        Under replay, the extreme bar must NOT pollute the mean.
        """
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            # Put a regular bar on the lookback day.
            self._write_bars(base, date(2026, 4, 28), "AAPL", [
                {"ts": "2026-04-28T13:30:00Z", "et_bucket": "0930",
                 "iex_volume": 100, "total_volume": 100},
                # Poison \u2014 ts is in the future relative to replay clock.
                {"ts": "2026-05-01T13:30:00Z", "et_bucket": "0930",
                 "iex_volume": 999999, "total_volume": 999999},
            ])
            clock = _FakeClock(
                datetime(2026, 4, 29, 13, 35, tzinfo=timezone.utc)
            )
            fake_tg = mock.MagicMock()
            fake_tg._now_utc = clock.now_utc
            with mock.patch.dict(sys.modules, {"trade_genius": fake_tg}):
                bb = self.vb.VolumeBucketBaseline(base_dir=str(base))
                bb.refresh(today=date(2026, 4, 29))
            # AAPL @ 09:30 mean must be 100 (poison filtered), not
            # ~500049 (poison included).
            mean = bb.baseline.get("AAPL", {}).get("09:30")
            # Lookback is 55 days; only one day has data, so days_available
            # = 1 (cold-start). Whether the gate is COLDSTART or PASS is
            # secondary \u2014 the assertion is that the mean did not get
            # polluted by the poison bar.
            if mean is not None:
                self.assertLess(mean, 1000.0,
                                f"poison bar leaked into baseline mean: {mean}")


class TestLifecycleRespectRetainEnv(unittest.TestCase):
    """v6.15.5: broker.lifecycle.eod_close must read
    bar_archive.DEFAULT_RETAIN_DAYS, not hardcode 90."""

    def test_eod_close_passes_default_retain_days(self) -> None:
        # Source-grep: the line `cleanup_old_dirs(retain_days=90)`
        # must NOT be present in lifecycle.py. The replacement uses
        # ``getattr(tg.bar_archive, 'DEFAULT_RETAIN_DAYS', 90)``.
        repo_root = Path(__file__).resolve().parent.parent
        src = (repo_root / "broker" / "lifecycle.py").read_text()
        self.assertNotIn("cleanup_old_dirs(retain_days=90)", src,
                         "lifecycle.py still hardcodes retain_days=90")
        self.assertIn("DEFAULT_RETAIN_DAYS", src,
                      "lifecycle.py must read DEFAULT_RETAIN_DAYS")

    def test_default_retain_days_respects_env(self) -> None:
        """Re-import bar_archive with BAR_ARCHIVE_RETAIN_DAYS set and
        verify the constant reflects the env override."""
        with mock.patch.dict(os.environ, {"BAR_ARCHIVE_RETAIN_DAYS": "9999"}):
            if "bar_archive" in sys.modules:
                importlib.reload(sys.modules["bar_archive"])
            import bar_archive
            self.assertEqual(bar_archive.DEFAULT_RETAIN_DAYS, 9999)
        # Reload back to default state so other tests aren't affected.
        if "bar_archive" in sys.modules:
            importlib.reload(sys.modules["bar_archive"])


if __name__ == "__main__":
    unittest.main()
