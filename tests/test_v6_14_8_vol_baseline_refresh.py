"""v6.14.8 unit tests: pre-market vol-baseline refresh + self-heal.

v6.14.8 makes two changes to
``v5_10_1_integration.refresh_volume_baseline_if_needed``:

  1. The scheduled refresh time moved from 09:29 ET to 04:00 ET (read
     from ``volume_bucket.VOLUME_BUCKET_REFRESH_HHMM_ET`` rather than
     hardcoded ints) so the rolling baseline is loaded for the entire
     pre-market window, not only the 60s before RTH open.
  2. Self-heal: if the scheduled refresh has fired for today but the
     baseline still reports ``days_available=0`` for every ticker
     (e.g., refresh ran before the bar archive was populated), every
     subsequent scan tick triggers a recovery refresh, rate-limited
     to once per ``VOLUME_BUCKET_SELF_HEAL_INTERVAL_SEC`` (60s).

These tests pin the gate boundary, the self-heal trigger condition,
the rate-limit, and the idempotency invariant so future refactors
cannot silently regress the pre-market window again.

NOTE: this test file is intentionally em-dash free per project rules.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _reset_module_state(integration_mod):
    """Wipe v5_10_1_integration module-level state between tests so
    each scenario starts on a fresh per-process cache.
    """
    integration_mod._volume_baseline = None
    integration_mod._baseline_refreshed_for_date = None
    integration_mod._last_self_heal_attempt_utc = None


def _import_integration():
    os.environ.setdefault("FMP_API_KEY", "test_key")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:fake")
    os.environ.setdefault("SSM_SMOKE_TEST", "1")
    import v5_10_1_integration as integration
    import volume_bucket as vb
    _reset_module_state(integration)
    return integration, vb


def test_refresh_gate_below_0400_returns_false():
    """At 03:59:59 ET on a fresh session, the gate must NOT fire.

    This is the boundary on the early side: one second before the
    new 04:00 ET refresh time, the function must return False and
    leave _baseline_refreshed_for_date unset so the next tick still
    has a chance to fire the scheduled refresh.
    """
    integration, _ = _import_integration()

    fake_baseline = mock.MagicMock()
    fake_baseline.days_available_per_ticker = {}

    with mock.patch.object(
        integration, "get_volume_baseline", return_value=fake_baseline
    ):
        result = integration.refresh_volume_baseline_if_needed(
            datetime(2026, 5, 5, 3, 59, 59)
        )

    assert result is False
    assert integration._baseline_refreshed_for_date is None
    fake_baseline.refresh.assert_not_called()


def test_refresh_gate_at_0400_fires_scheduled_refresh():
    """At 04:00:00 ET on a fresh session, the gate MUST fire exactly
    once and stamp _baseline_refreshed_for_date with today's date.
    """
    integration, _ = _import_integration()

    fake_baseline = mock.MagicMock()
    fake_baseline.days_available_per_ticker = {"AAPL": 55, "MSFT": 55}

    with mock.patch.object(
        integration, "get_volume_baseline", return_value=fake_baseline
    ):
        result = integration.refresh_volume_baseline_if_needed(
            datetime(2026, 5, 5, 4, 0, 0)
        )

    assert result is True
    assert integration._baseline_refreshed_for_date == \
        datetime(2026, 5, 5, 4, 0, 0).date()
    fake_baseline.refresh.assert_called_once()


def test_self_heal_fires_when_all_tickers_have_zero_days():
    """If the scheduled refresh has already fired today but the
    baseline reports days_available=0 for every ticker, the next
    scan tick must trigger a recovery refresh.
    """
    integration, _ = _import_integration()

    today = datetime(2026, 5, 5, 8, 30, 0).date()
    integration._baseline_refreshed_for_date = today

    fake_baseline = mock.MagicMock()
    fake_baseline.days_available_per_ticker = {
        "AAPL": 0, "MSFT": 0, "NVDA": 0,
    }

    with mock.patch.object(
        integration, "get_volume_baseline", return_value=fake_baseline
    ):
        result = integration.refresh_volume_baseline_if_needed(
            datetime(2026, 5, 5, 8, 30, 0)
        )

    assert result is True
    fake_baseline.refresh.assert_called_once()
    assert integration._last_self_heal_attempt_utc is not None


def test_self_heal_rate_limited_within_60s():
    """A second self-heal call within
    VOLUME_BUCKET_SELF_HEAL_INTERVAL_SEC must return False without
    calling refresh, even though the empty-baseline trigger
    condition still holds.
    """
    integration, vb = _import_integration()

    today = datetime(2026, 5, 5, 8, 30, 0).date()
    integration._baseline_refreshed_for_date = today
    # Stamp a recent attempt so the next call lands inside the window.
    integration._last_self_heal_attempt_utc = (
        datetime.now(timezone.utc) - timedelta(seconds=5)
    )

    fake_baseline = mock.MagicMock()
    fake_baseline.days_available_per_ticker = {"AAPL": 0, "MSFT": 0}

    with mock.patch.object(
        integration, "get_volume_baseline", return_value=fake_baseline
    ):
        result = integration.refresh_volume_baseline_if_needed(
            datetime(2026, 5, 5, 8, 30, 5)
        )

    assert result is False
    fake_baseline.refresh.assert_not_called()
    # The rate-limit interval constant must remain a positive integer
    # so a misconfiguration to 0 cannot silently disable the limiter.
    assert vb.VOLUME_BUCKET_SELF_HEAL_INTERVAL_SEC > 0


def test_idempotency_five_ticks_after_refresh_is_one_refresh():
    """Five consecutive scan ticks at 04:30 ET with a healthy
    baseline (every ticker has days_available > 0) must yield
    exactly one refresh -- the initial scheduled one. No self-heal
    triggers because the baseline is not empty.
    """
    integration, _ = _import_integration()

    fake_baseline = mock.MagicMock()
    fake_baseline.days_available_per_ticker = {
        "AAPL": 55, "MSFT": 55, "NVDA": 55, "TSLA": 55,
    }

    with mock.patch.object(
        integration, "get_volume_baseline", return_value=fake_baseline
    ):
        results = [
            integration.refresh_volume_baseline_if_needed(
                datetime(2026, 5, 5, 4, 30, sec)
            )
            for sec in range(5)
        ]

    # First tick fires; ticks 2-5 are no-ops because today's
    # scheduled refresh is already stamped and baseline is healthy.
    assert results == [True, False, False, False, False]
    fake_baseline.refresh.assert_called_once()
