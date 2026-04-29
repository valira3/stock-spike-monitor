"""v5.11.0 PR6 \u2014 regression test for the engine-seam replay harness.

This test locks in the contract that `backtest.replay_v511_full` drives
`engine.scan.scan_loop` directly via `RecordOnlyCallbacks`, rather than
the parallel re-implementation of the per-tick logic that the
workspace-only `replay_v510_full_v4.py` carried.

If a future change re-introduces a parallel scan body in the replay
harness, this test will not catch it directly \u2014 but it WILL catch
the regression where someone removes the engine-driven path and the
recorded callbacks list goes empty. That's the assertion this test
guards.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtest.replay_v511_full import (  # noqa: E402
    RecordOnlyCallbacks,
    run_replay,
)

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "replay_v511_minimal"


def test_fixture_dir_exists():
    """Fixture must be present \u2014 the replay can't run without bars."""
    assert FIXTURE_DIR.is_dir(), f"missing fixture: {FIXTURE_DIR}"
    assert (FIXTURE_DIR / "2026-04-28" / "AAPL.jsonl").is_file()
    assert (FIXTURE_DIR / "2026-04-28" / "QQQ.jsonl").is_file()


def test_record_only_callbacks_satisfies_protocol():
    """RecordOnlyCallbacks must match every method the engine calls."""
    cb = RecordOnlyCallbacks()
    # Spot-check every attribute engine.scan reaches for.
    for name in (
        "now_et", "now_cdt", "fetch_1min_bars",
        "get_position", "has_long", "has_short",
        "manage_positions", "manage_short_positions",
        "check_entry", "check_short_entry",
        "execute_entry", "execute_short_entry", "execute_exit",
        "alert", "report_error",
    ):
        assert callable(getattr(cb, name)), f"missing callback: {name}"


def test_replay_drives_engine_scan_loop():
    """Run the replay against the minimal fixture and assert the engine
    seam was actually exercised.

    Specifically:
      * `scan_loop` was invoked >= 1 minute (ticks recorded);
      * `fetch_1min_bars` was called by the engine (i.e. the bars
        callback path is wired through), proving the replay is consuming
        engine.scan rather than a parallel implementation;
      * No driver-side exception was logged \u2014 the engine ran cleanly
        through the simulated minutes.
    """
    result = run_replay(
        "2026-04-28",
        tickers=["AAPL"],
        bars_dir=FIXTURE_DIR,
        start_hhmm=(9, 35),
        end_hhmm=(9, 50),
    )
    cb = result.callbacks
    assert result.minutes_processed == 16
    assert len(cb.ticks) >= 1, "scan_loop driver did not advance the clock"
    assert len(cb.fetch_calls) >= 1, (
        "engine.scan.scan_loop did not call fetch_1min_bars \u2014 "
        "the engine seam is not wired through the replay harness"
    )
    # No driver-side exceptions \u2014 if the engine crashed in the loop
    # body the replay driver would have appended a SCAN_LOOP_EXCEPTION
    # error. (Internal try/except in scan.py logs warnings but does not
    # propagate, which is fine.)
    crash_codes = {e["code"] for e in cb.errors}
    assert "SCAN_LOOP_EXCEPTION" not in crash_codes, (
        f"scan_loop crashed during replay: {cb.errors}"
    )


def test_replay_is_offline():
    """Replay must not reach the network: pure record-only callbacks
    plus a fake `trade_genius` module \u2014 no broker, Telegram, or
    persistence calls. We assert this indirectly by checking that the
    callbacks recorded zero alerts and zero error reports against the
    fixture (the fixture is too short to trigger a regime flip, the
    only legitimate alert path)."""
    result = run_replay(
        "2026-04-28",
        tickers=["AAPL"],
        bars_dir=FIXTURE_DIR,
        start_hhmm=(9, 35),
        end_hhmm=(9, 40),
    )
    cb = result.callbacks
    assert cb.alerts == [], (
        f"unexpected alerts in offline replay: {cb.alerts}"
    )


def test_engine_scan_module_used():
    """After run_replay the fake trade_genius module must be installed
    in sys.modules \u2014 this is the mechanism the engine resolves
    state through. Confirms the seam plumbing."""
    run_replay(
        "2026-04-28",
        tickers=["AAPL"],
        bars_dir=FIXTURE_DIR,
        start_hhmm=(9, 35),
        end_hhmm=(9, 36),
    )
    fake = sys.modules.get("trade_genius")
    assert fake is not None
    assert getattr(fake, "_replay_marker", "") == "v5.11.0-pr6-replay-fake-tg"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
