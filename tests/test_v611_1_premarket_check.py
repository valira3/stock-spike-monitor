"""Tests for v6.11.1 -- pre-market readiness check (scripts/premarket_check.py).

Covers:
1. test_check_result_format           -- every check function returns a dict with required keys
2. test_overall_status_aggregation    -- aggregator returns worst status
3. test_classifier_smoke_fixture      -- classifier_smoke check passes in normal env
4. test_sizing_helper_smoke_each_branch -- exercises all 6 sizing-helper branches
5. test_json_output_shape             -- --json flag output has required top-level keys
6. test_run_all_checks_callable_returns_dict -- run_all_checks() returns expected dict shape
7. test_format_for_telegram_under_4096_chars         -- all-PASS result stays under 3500 chars
8. test_format_for_telegram_under_4096_chars_with_failures -- all-FAIL/WARN stays under 3500 chars

ZERO em-dashes in this file.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import types
import unittest.mock as mock
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup -- ensure repo root is on sys.path so `from scripts...` works.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Import the module under test.
from scripts.premarket_check import (
    _aggregate_status,
    _result,
    check_classifier_smoke,
    check_sizing_helper_smoke,
    format_for_telegram,
    run_all_checks,
)

# All individual check functions for format testing.
import scripts.premarket_check as _pmc

_ALL_CHECK_FNS = [
    _pmc.check_process_alive,
    _pmc.check_version_parity,
    _pmc.check_module_imports,
    _pmc.check_persistence_reachable,
    _pmc.check_bar_archive_yesterday,
    _pmc.check_alpaca_auth_paper,
    _pmc.check_alpaca_auth_live,
    _pmc.check_alpaca_data_feed_recent_trade,
    _pmc.check_classifier_smoke,
    _pmc.check_sizing_helper_smoke,
    _pmc.check_dashboard_state,
    _pmc.check_disk_space,
    _pmc.check_time_sync,
    _pmc.check_cron_introspection,
    _pmc.check_replay_smoke,
]

_REQUIRED_KEYS = {"name", "status", "detail", "elapsed_ms", "data"}
_VALID_STATUSES = {"PASS", "WARN", "FAIL", "SKIP"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_result(status: str, name: str = "fake", detail: str = "") -> dict:
    return _result(name, status, detail, 1, {})


def _synthetic_result(checks: list[dict], overall: str = None) -> dict:
    """Build a synthetic run_all_checks()-shaped result dict."""
    if overall is None:
        overall = _aggregate_status(checks)
    return {
        "version": "1",
        "timestamp_utc": "2026-05-04T08:30:00Z",
        "bot_version": "6.11.1",
        "overall_status": overall,
        "n_pass": sum(1 for c in checks if c["status"] == "PASS"),
        "n_warn": sum(1 for c in checks if c["status"] == "WARN"),
        "n_fail": sum(1 for c in checks if c["status"] == "FAIL"),
        "n_skip": sum(1 for c in checks if c["status"] == "SKIP"),
        "elapsed_total_ms": 1234,
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# 1. test_check_result_format
# ---------------------------------------------------------------------------

class TestCheckResultFormat:
    """Every check function must return a dict with the required keys + valid status."""

    @pytest.mark.parametrize("fn", _ALL_CHECK_FNS, ids=[f.__name__ for f in _ALL_CHECK_FNS])
    def test_required_keys_present(self, fn):
        result = fn()
        assert isinstance(result, dict), "%s must return a dict" % fn.__name__
        for key in _REQUIRED_KEYS:
            assert key in result, "%s missing key %r" % (fn.__name__, key)

    @pytest.mark.parametrize("fn", _ALL_CHECK_FNS, ids=[f.__name__ for f in _ALL_CHECK_FNS])
    def test_status_is_valid(self, fn):
        result = fn()
        assert result["status"] in _VALID_STATUSES, (
            "%s returned invalid status %r" % (fn.__name__, result["status"])
        )

    @pytest.mark.parametrize("fn", _ALL_CHECK_FNS, ids=[f.__name__ for f in _ALL_CHECK_FNS])
    def test_elapsed_ms_is_non_negative_int(self, fn):
        result = fn()
        assert isinstance(result["elapsed_ms"], int), (
            "%s elapsed_ms must be int, got %r" % (fn.__name__, type(result["elapsed_ms"]))
        )
        assert result["elapsed_ms"] >= 0, "%s elapsed_ms < 0" % fn.__name__

    @pytest.mark.parametrize("fn", _ALL_CHECK_FNS, ids=[f.__name__ for f in _ALL_CHECK_FNS])
    def test_data_is_dict(self, fn):
        result = fn()
        assert isinstance(result["data"], dict), (
            "%s data must be dict, got %r" % (fn.__name__, type(result["data"]))
        )


# ---------------------------------------------------------------------------
# 2. test_overall_status_aggregation
# ---------------------------------------------------------------------------

class TestOverallStatusAggregation:
    """_aggregate_status returns the worst status from a list of check dicts."""

    def test_all_pass_returns_pass(self):
        checks = [_fake_result("PASS"), _fake_result("PASS")]
        assert _aggregate_status(checks) == "PASS"

    def test_pass_and_skip_returns_pass(self):
        checks = [_fake_result("PASS"), _fake_result("SKIP")]
        assert _aggregate_status(checks) == "PASS"

    def test_all_skip_returns_pass(self):
        checks = [_fake_result("SKIP"), _fake_result("SKIP")]
        assert _aggregate_status(checks) == "PASS"

    def test_warn_beats_pass(self):
        checks = [_fake_result("PASS"), _fake_result("WARN")]
        assert _aggregate_status(checks) == "WARN"

    def test_fail_beats_warn(self):
        checks = [_fake_result("WARN"), _fake_result("FAIL")]
        assert _aggregate_status(checks) == "FAIL"

    def test_fail_beats_everything(self):
        checks = [
            _fake_result("PASS"),
            _fake_result("SKIP"),
            _fake_result("WARN"),
            _fake_result("FAIL"),
        ]
        assert _aggregate_status(checks) == "FAIL"

    def test_single_fail_returns_fail(self):
        assert _aggregate_status([_fake_result("FAIL")]) == "FAIL"

    def test_single_warn_returns_warn(self):
        assert _aggregate_status([_fake_result("WARN")]) == "WARN"

    def test_empty_returns_pass(self):
        assert _aggregate_status([]) == "PASS"


# ---------------------------------------------------------------------------
# 3. test_classifier_smoke_fixture
# ---------------------------------------------------------------------------

class TestClassifierSmokeFixture:
    """classifier_smoke check passes (PASS) under normal importable env."""

    def test_classifier_smoke_passes(self):
        result = check_classifier_smoke()
        assert result["status"] == "PASS", (
            "classifier_smoke returned %s: %s" % (result["status"], result["detail"])
        )

    def test_classifier_smoke_has_data(self):
        result = check_classifier_smoke()
        data = result["data"]
        assert "spy_regime" in data, "expected spy_regime in data"
        assert "qqq_compass" in data, "expected qqq_compass in data"

    def test_classifier_smoke_spy_regime_b(self):
        """The synthetic tick (ret=-0.32pct) should produce regime=B."""
        result = check_classifier_smoke()
        assert result["data"].get("spy_regime") == "B"


# ---------------------------------------------------------------------------
# 4. test_sizing_helper_smoke_each_branch
# ---------------------------------------------------------------------------

class TestSizingHelperSmokeEachBranch:
    """_maybe_apply_regime_b_short_amp covers all 6 branches correctly."""

    def test_all_branches_pass(self):
        result = check_sizing_helper_smoke()
        assert result["status"] == "PASS", (
            "sizing_helper_smoke returned %s: %s" % (result["status"], result["detail"])
        )

    def test_branches_tested_count(self):
        result = check_sizing_helper_smoke()
        assert result["data"].get("branches_tested") == 6

    def test_long_side_passthrough(self):
        """Branch 1: long side is always passthrough regardless of regime."""
        import datetime
        import types as _types
        from zoneinfo import ZoneInfo
        from broker.orders import _maybe_apply_regime_b_short_amp

        ET = ZoneInfo("America/New_York")
        cfg = _types.SimpleNamespace(side=_types.SimpleNamespace(is_long=True))
        regime = _types.SimpleNamespace(is_regime_b=lambda: True)
        now_et = datetime.datetime(2026, 5, 4, 10, 30, tzinfo=ET)
        out = _maybe_apply_regime_b_short_amp(
            cfg=cfg, shares=20, ticker="SPY", now_et=now_et,
            regime=regime, scale=1.5, arm_hhmm_et="10:00", disarm_hhmm_et="11:00",
        )
        assert out == 20

    def test_short_in_window_regime_b_amplifies(self):
        """Branch 5: short, regime-B, in-window applies 1.5x scale."""
        import datetime
        import types as _types
        from zoneinfo import ZoneInfo
        from broker.orders import _maybe_apply_regime_b_short_amp

        ET = ZoneInfo("America/New_York")
        cfg = _types.SimpleNamespace(side=_types.SimpleNamespace(is_long=False))
        regime = _types.SimpleNamespace(is_regime_b=lambda: True)
        now_et = datetime.datetime(2026, 5, 4, 10, 30, tzinfo=ET)
        out = _maybe_apply_regime_b_short_amp(
            cfg=cfg, shares=10, ticker="SPY", now_et=now_et,
            regime=regime, scale=1.5, arm_hhmm_et="10:00", disarm_hhmm_et="11:00",
        )
        assert out == 15

    def test_short_non_regime_b_passthrough(self):
        """Branch 2: short but not regime-B -- passthrough."""
        import datetime
        import types as _types
        from zoneinfo import ZoneInfo
        from broker.orders import _maybe_apply_regime_b_short_amp

        ET = ZoneInfo("America/New_York")
        cfg = _types.SimpleNamespace(side=_types.SimpleNamespace(is_long=False))
        regime = _types.SimpleNamespace(is_regime_b=lambda: False)
        now_et = datetime.datetime(2026, 5, 4, 10, 30, tzinfo=ET)
        out = _maybe_apply_regime_b_short_amp(
            cfg=cfg, shares=10, ticker="SPY", now_et=now_et,
            regime=regime, scale=1.5, arm_hhmm_et="10:00", disarm_hhmm_et="11:00",
        )
        assert out == 10


# ---------------------------------------------------------------------------
# 5. test_json_output_shape
# ---------------------------------------------------------------------------

class TestJsonOutputShape:
    """run_all_checks() result dict must have all required top-level keys."""

    _REQUIRED_TOP_KEYS = {
        "version", "timestamp_utc", "bot_version",
        "overall_status", "n_pass", "n_warn", "n_fail", "n_skip",
        "elapsed_total_ms", "checks",
    }

    def test_top_level_keys_present(self):
        # Use write_artifact=False to avoid /data dependency in CI.
        result = run_all_checks(in_container=True, write_artifact=False)
        for key in self._REQUIRED_TOP_KEYS:
            assert key in result, "top-level key %r missing from result" % key

    def test_overall_status_is_valid(self):
        result = run_all_checks(in_container=True, write_artifact=False)
        assert result["overall_status"] in {"PASS", "WARN", "FAIL"}

    def test_checks_is_list(self):
        result = run_all_checks(in_container=True, write_artifact=False)
        assert isinstance(result["checks"], list)
        assert len(result["checks"]) > 0

    def test_counts_are_consistent(self):
        result = run_all_checks(in_container=True, write_artifact=False)
        checks = result["checks"]
        assert result["n_pass"] == sum(1 for c in checks if c["status"] == "PASS")
        assert result["n_warn"] == sum(1 for c in checks if c["status"] == "WARN")
        assert result["n_fail"] == sum(1 for c in checks if c["status"] == "FAIL")
        assert result["n_skip"] == sum(1 for c in checks if c["status"] == "SKIP")

    def test_version_field_is_string_1(self):
        result = run_all_checks(in_container=True, write_artifact=False)
        assert result["version"] == "1"

    def test_remote_mode_raises(self):
        with pytest.raises(RuntimeError, match="not yet implemented"):
            run_all_checks(in_container=False, write_artifact=False)


# ---------------------------------------------------------------------------
# 6. test_run_all_checks_callable_returns_dict
# ---------------------------------------------------------------------------

class TestRunAllChecksCallable:
    """run_all_checks(in_container=True, write_artifact=False) returns the right shape."""

    def test_returns_dict(self):
        result = run_all_checks(in_container=True, write_artifact=False)
        assert isinstance(result, dict)

    def test_elapsed_total_ms_is_positive(self):
        result = run_all_checks(in_container=True, write_artifact=False)
        assert result["elapsed_total_ms"] >= 0

    def test_all_check_dicts_have_required_keys(self):
        result = run_all_checks(in_container=True, write_artifact=False)
        for c in result["checks"]:
            for key in _REQUIRED_KEYS:
                assert key in c, "check dict missing key %r: %r" % (key, c)

    def test_write_artifact_false_does_not_write(self, tmp_path, monkeypatch):
        """When write_artifact=False, no file should be written."""
        monkeypatch.setattr(_pmc, "_TG_DATA_ROOT", str(tmp_path))
        result = run_all_checks(in_container=True, write_artifact=False)
        preflight_dir = tmp_path / "preflight"
        assert not preflight_dir.exists(), "preflight dir should not be created when write_artifact=False"


# ---------------------------------------------------------------------------
# 7. test_format_for_telegram_under_4096_chars (all-PASS)
# ---------------------------------------------------------------------------

class TestFormatForTelegramUnder4096Chars:
    """format_for_telegram output stays under 3500 chars for all-PASS result."""

    def _all_pass_checks(self, n: int = 15) -> list[dict]:
        return [
            _result("check_%02d" % i, "PASS", "everything is fine", 10, {})
            for i in range(n)
        ]

    def test_all_pass_under_3500(self):
        checks = self._all_pass_checks()
        result = _synthetic_result(checks)
        text = format_for_telegram(result)
        assert len(text) <= 3500, (
            "format_for_telegram output too long: %d chars" % len(text)
        )

    def test_all_pass_contains_overall(self):
        checks = self._all_pass_checks()
        result = _synthetic_result(checks)
        text = format_for_telegram(result)
        assert "PASS" in text

    def test_all_pass_contains_version(self):
        checks = self._all_pass_checks()
        result = _synthetic_result(checks)
        text = format_for_telegram(result)
        assert "6.11.1" in text

    def test_all_pass_is_string(self):
        checks = self._all_pass_checks()
        result = _synthetic_result(checks)
        text = format_for_telegram(result)
        assert isinstance(text, str)


# ---------------------------------------------------------------------------
# 8. test_format_for_telegram_under_4096_chars_with_failures
# ---------------------------------------------------------------------------

class TestFormatForTelegramWithFailures:
    """Even all-FAIL/WARN with long detail strings, output stays under 3500 chars."""

    def _long_detail(self, n: int = 200) -> str:
        return "x" * n

    def _all_fail_checks(self, n: int = 15) -> list[dict]:
        return [
            _result(
                "check_%02d" % i,
                "FAIL" if i % 2 == 0 else "WARN",
                self._long_detail(200),
                10,
                {},
            )
            for i in range(n)
        ]

    def test_all_fail_warn_under_3500(self):
        checks = self._all_fail_checks()
        result = _synthetic_result(checks, overall="FAIL")
        text = format_for_telegram(result)
        assert len(text) <= 3500, (
            "format_for_telegram output too long with failures: %d chars" % len(text)
        )

    def test_extreme_long_detail_under_3500(self):
        """Even with 500-char detail strings on every check, still truncates properly."""
        checks = [
            _result("check_%02d" % i, "FAIL", self._long_detail(500), 10, {})
            for i in range(15)
        ]
        result = _synthetic_result(checks, overall="FAIL")
        text = format_for_telegram(result)
        assert len(text) <= 3500, (
            "format_for_telegram failed to truncate extreme input: %d chars" % len(text)
        )

    def test_fail_warn_contains_fail_indicator(self):
        checks = self._all_fail_checks(3)
        result = _synthetic_result(checks, overall="FAIL")
        text = format_for_telegram(result)
        assert "FAIL" in text

    def test_output_is_string_with_failures(self):
        checks = self._all_fail_checks()
        result = _synthetic_result(checks, overall="FAIL")
        text = format_for_telegram(result)
        assert isinstance(text, str)
