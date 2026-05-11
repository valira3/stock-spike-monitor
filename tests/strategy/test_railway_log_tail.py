"""Tests for tools.railway_log_tail -- v7.79.0 log-tail module."""
from __future__ import annotations

import os

import pytest

from tools import railway_log_tail as rlt


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Clear Railway env vars per-test so leaked credentials can't
    influence test outcomes."""
    for k in ("RAILWAY_API_TOKEN", "RAILWAY_SERVICE_ID", "RAILWAY_API_URL"):
        monkeypatch.delenv(k, raising=False)
    yield


class TestFetchRecentLogs:

    def test_returns_empty_when_token_missing(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_SERVICE_ID", "abc")
        assert rlt.fetch_recent_logs() == []

    def test_returns_empty_when_service_id_missing(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "tok")
        assert rlt.fetch_recent_logs() == []

    def test_returns_empty_when_both_missing(self):
        assert rlt.fetch_recent_logs() == []

    def test_returns_empty_when_gql_fails(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "tok")
        monkeypatch.setenv("RAILWAY_SERVICE_ID", "svc")
        monkeypatch.setattr(rlt, "_gql", lambda *a, **k: None)
        assert rlt.fetch_recent_logs() == []

    def test_returns_empty_when_no_deployment_found(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "tok")
        monkeypatch.setenv("RAILWAY_SERVICE_ID", "svc")
        # _gql returns valid shape but with no edges
        monkeypatch.setattr(
            rlt, "_gql",
            lambda *a, **k: {"data": {"deployments": {"edges": []}}},
        )
        assert rlt.fetch_recent_logs() == []

    def test_returns_parsed_logs_when_successful(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "tok")
        monkeypatch.setenv("RAILWAY_SERVICE_ID", "svc")
        # First call: latest deployment id; second call: actual logs.
        responses = iter([
            {"data": {"deployments": {"edges": [
                {"node": {"id": "deploy-1", "status": "SUCCESS"}}
            ]}}},
            {"data": {"deploymentLogs": [
                {"timestamp": "2026-05-11T15:30:00Z",
                 "message": "[V79-ORB-ENTRY] long AAPL ...",
                 "severity": "info"},
                {"timestamp": "2026-05-11T15:30:01Z",
                 "message": "[ALPACA-ERR] insufficient_buying_power",
                 "severity": "warning"},
            ]}},
        ])
        monkeypatch.setattr(rlt, "_gql", lambda *a, **k: next(responses))
        logs = rlt.fetch_recent_logs(limit=10)
        assert len(logs) == 2
        assert logs[0]["message"].startswith("[V79-ORB-ENTRY]")
        assert logs[1]["message"].startswith("[ALPACA-ERR]")
        assert logs[1]["severity"] == "warning"

    def test_handles_malformed_response(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "tok")
        monkeypatch.setenv("RAILWAY_SERVICE_ID", "svc")
        # Response missing 'data' key.
        monkeypatch.setattr(rlt, "_gql", lambda *a, **k: {"errors": ["bad"]})
        assert rlt.fetch_recent_logs() == []

    def test_filters_non_dict_rows(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "tok")
        monkeypatch.setenv("RAILWAY_SERVICE_ID", "svc")
        responses = iter([
            {"data": {"deployments": {"edges": [{"node": {"id": "d1"}}]}}},
            {"data": {"deploymentLogs": [
                {"timestamp": "t1", "message": "valid", "severity": "info"},
                None,  # malformed
                "string-instead-of-dict",  # malformed
                {"timestamp": "t2", "message": "valid2", "severity": "info"},
            ]}},
        ])
        monkeypatch.setattr(rlt, "_gql", lambda *a, **k: next(responses))
        logs = rlt.fetch_recent_logs()
        assert len(logs) == 2
        assert logs[0]["message"] == "valid"
        assert logs[1]["message"] == "valid2"


class TestScanForFailures:

    def test_returns_empty_when_no_matches(self):
        logs = [
            {"timestamp": "t1", "message": "normal log line", "severity": "info"},
            {"timestamp": "t2", "message": "another normal line", "severity": "info"},
        ]
        assert rlt.scan_for_failures(logs) == {}

    def test_detects_alpaca_error(self):
        logs = [
            {"timestamp": "t1",
             "message": "[VAL] [ALPACA-ERR] req=(sym=AAPL ...) err=APIError",
             "severity": "warning"},
        ]
        findings = rlt.scan_for_failures(logs)
        assert "alpaca_error" in findings
        assert findings["alpaca_error"]["count"] == 1

    def test_detects_sentinel_critical(self):
        logs = [
            {"timestamp": "t1",
             "message": "[SENTINEL][CRITICAL] fetch_1min_bars META failed",
             "severity": "error"},
        ]
        findings = rlt.scan_for_failures(logs)
        assert "sentinel_critical" in findings

    def test_detects_insufficient_cash(self):
        logs = [
            {"timestamp": "t1",
             "message": "[paper] skip MSFT -- insufficient cash (need $148K)",
             "severity": "info"},
        ]
        findings = rlt.scan_for_failures(logs)
        assert "insufficient_cash" in findings

    def test_detects_risk_reject_notional(self):
        logs = [
            {"timestamp": "t1",
             "message": "risk_reject:notional_cap (would-be $293 > $0)",
             "severity": "info"},
        ]
        findings = rlt.scan_for_failures(logs)
        assert "risk_reject_notional_cap" in findings

    def test_detects_v15_wait_abort(self):
        logs = [
            {"timestamp": "t1",
             "message": "[V15-SIZING] AAPL side=LONG WAIT (defensive abort): ...",
             "severity": "info"},
        ]
        findings = rlt.scan_for_failures(logs)
        assert "v15_wait_abort" in findings

    def test_detects_uncaught_traceback(self):
        logs = [
            {"timestamp": "t1",
             "message": "Traceback (most recent call last):",
             "severity": "error"},
        ]
        findings = rlt.scan_for_failures(logs)
        assert "uncaught_traceback" in findings

    def test_counts_repeated_signals(self):
        logs = [
            {"timestamp": "t1", "message": "[ALPACA-ERR] one", "severity": "warning"},
            {"timestamp": "t2", "message": "[ALPACA-ERR] two", "severity": "warning"},
            {"timestamp": "t3", "message": "[ALPACA-ERR] three", "severity": "warning"},
        ]
        findings = rlt.scan_for_failures(logs)
        assert findings["alpaca_error"]["count"] == 3
        # first_message captures the first hit
        assert "one" in findings["alpaca_error"]["first_message"]
        # last_timestamp is the latest
        assert findings["alpaca_error"]["last_timestamp"] == "t3"

    def test_multiple_signals_distinct_buckets(self):
        logs = [
            {"timestamp": "t1", "message": "[ALPACA-ERR] x", "severity": "warning"},
            {"timestamp": "t2", "message": "[V79-ORB-REJECT] y", "severity": "info"},
            {"timestamp": "t3", "message": "insufficient cash -- bot says [paper] skip Z -- insufficient cash", "severity": "info"},
        ]
        findings = rlt.scan_for_failures(logs)
        assert "alpaca_error" in findings
        assert "risk_reject_other" in findings
        assert "insufficient_cash" in findings
        assert findings["alpaca_error"]["count"] == 1

    def test_custom_signatures_override_default(self):
        logs = [
            {"timestamp": "t1", "message": "MY_CUSTOM_TAG fired", "severity": "info"},
        ]
        findings = rlt.scan_for_failures(
            logs, signatures={"custom": r"MY_CUSTOM_TAG"},
        )
        assert findings == {
            "custom": {
                "count": 1,
                "first_message": "MY_CUSTOM_TAG fired",
                "last_timestamp": "t1",
            }
        }
