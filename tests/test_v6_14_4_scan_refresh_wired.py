"""v6.14.4 unit tests: scan_loop wires volume_bucket baseline refresh.

The hook ``v5_10_1_integration.refresh_volume_baseline_if_needed`` was
exported and unit-tested since v5.10.1 but never invoked from the live
scan loop, so the baseline stayed empty and the dashboard sat in
COLDSTART regardless of how many days were on disk. This test pins the
wire-up so any future refactor of engine.scan that drops the call gets
caught at CI time.

NOTE: this test file is intentionally em-dash free per project rules.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_scan_loop_calls_refresh_volume_baseline_if_needed():
    """Per scan cycle, scan_loop must invoke the volume baseline refresh
    hook exactly once with the current ET timestamp.
    """
    import os
    os.environ.setdefault("FMP_API_KEY", "test_key")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:fake")
    os.environ.setdefault("SSM_SMOKE_TEST", "1")

    from engine import scan as engine_scan

    fake_now_et = datetime(2026, 5, 4, 16, 45, 0)

    callbacks = mock.MagicMock()
    callbacks.now_et.return_value = fake_now_et

    fake_tg = mock.MagicMock()
    fake_tg._refresh_market_mode = mock.MagicMock()

    with mock.patch.object(engine_scan, "_tg", return_value=fake_tg), \
         mock.patch.object(engine_scan, "eot_glue") as glue:
        glue.refresh_volume_baseline_if_needed = mock.MagicMock(return_value=True)
        try:
            engine_scan.scan_loop(callbacks)
        except Exception:
            # Downstream scan-loop work may need more wiring than the
            # mock provides; we only care that the refresh hook was
            # invoked before any of that ran.
            pass
        glue.refresh_volume_baseline_if_needed.assert_called_once_with(fake_now_et)


def test_scan_loop_swallows_refresh_exceptions():
    """A raise in the refresh hook must not crash the scan loop."""
    import os
    os.environ.setdefault("FMP_API_KEY", "test_key")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:fake")
    os.environ.setdefault("SSM_SMOKE_TEST", "1")

    from engine import scan as engine_scan

    fake_now_et = datetime(2026, 5, 4, 16, 45, 0)
    callbacks = mock.MagicMock()
    callbacks.now_et.return_value = fake_now_et

    fake_tg = mock.MagicMock()
    fake_tg._refresh_market_mode = mock.MagicMock()

    with mock.patch.object(engine_scan, "_tg", return_value=fake_tg), \
         mock.patch.object(engine_scan, "eot_glue") as glue:
        glue.refresh_volume_baseline_if_needed = mock.MagicMock(
            side_effect=RuntimeError("boom"),
        )
        try:
            engine_scan.scan_loop(callbacks)
        except RuntimeError as e:
            if "boom" in str(e):
                raise AssertionError(
                    "scan_loop must swallow refresh hook exceptions",
                )
        glue.refresh_volume_baseline_if_needed.assert_called_once()


def test_refresh_hook_is_called_before_weekend_short_circuit():
    """Even on a weekend tick, the refresh hook fires before scan_loop
    short-circuits. This matters because a Saturday redeploy on
    fully-populated bars should populate the baseline immediately
    rather than waiting for Monday's first 09:29 ET tick.
    """
    import os
    os.environ.setdefault("FMP_API_KEY", "test_key")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:fake")
    os.environ.setdefault("SSM_SMOKE_TEST", "1")

    from engine import scan as engine_scan

    saturday_noon_et = datetime(2026, 5, 9, 12, 0, 0)
    assert saturday_noon_et.weekday() == 5

    callbacks = mock.MagicMock()
    callbacks.now_et.return_value = saturday_noon_et

    fake_tg = mock.MagicMock()
    fake_tg._refresh_market_mode = mock.MagicMock()

    with mock.patch.object(engine_scan, "_tg", return_value=fake_tg), \
         mock.patch.object(engine_scan, "eot_glue") as glue:
        glue.refresh_volume_baseline_if_needed = mock.MagicMock(return_value=False)
        engine_scan.scan_loop(callbacks)
        glue.refresh_volume_baseline_if_needed.assert_called_once_with(saturday_noon_et)
