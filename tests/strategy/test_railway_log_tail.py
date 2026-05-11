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


# ---------------------------------------------------------------------
# v7.84.0 -- grep_logs + format_log_slice
# ---------------------------------------------------------------------


class TestGrepLogsV784:

    def test_returns_empty_when_no_logs(self, monkeypatch):
        monkeypatch.setattr(rlt, "fetch_recent_logs", lambda limit=500: [])
        assert rlt.grep_logs(r"\[V79-MIRROR-\w+\]") == []

    def test_returns_empty_on_invalid_regex(self):
        # Unbalanced ( in pattern
        assert rlt.grep_logs("[V79-MIRROR-(") == []

    def test_filters_by_pattern(self, monkeypatch):
        logs = [
            {"timestamp": "t1", "message": "[V79-MIRROR-RECV] Val kind=...", "severity": "info"},
            {"timestamp": "t2", "message": "SCAN CYCLE done", "severity": "info"},
            {"timestamp": "t3", "message": "[V79-MIRROR-SKIP] Val ENTRY_LONG ...", "severity": "warning"},
            {"timestamp": "t4", "message": "[V79-MIRROR-DISPATCH] Val ENTRY_LONG TSLA qty=181", "severity": "info"},
        ]
        monkeypatch.setattr(rlt, "fetch_recent_logs", lambda limit=500: logs)
        out = rlt.grep_logs(r"\[V79-MIRROR-\w+\]")
        assert len(out) == 3
        assert all("MIRROR" in r["message"] for r in out)

    def test_caps_at_max_matches(self, monkeypatch):
        logs = [
            {"timestamp": f"t{i}", "message": "[V79-MIRROR-RECV] ...", "severity": "info"}
            for i in range(50)
        ]
        monkeypatch.setattr(rlt, "fetch_recent_logs", lambda limit=500: logs)
        out = rlt.grep_logs(r"\[V79-MIRROR-\w+\]", max_matches=10)
        assert len(out) == 10


class TestFormatLogSliceV784:

    def test_empty_returns_empty_string(self):
        assert rlt.format_log_slice([]) == ""

    def test_one_line_per_row(self):
        rows = [
            {"timestamp": "2026-05-11T17:47:48Z",
             "message": "[V79-MIRROR-RECV] Val kind=ENTRY_LONG ticker=TSLA",
             "severity": "info"},
            {"timestamp": "2026-05-11T17:47:49Z",
             "message": "[V79-MIRROR-DISPATCH] Val ENTRY_LONG TSLA qty=181",
             "severity": "info"},
        ]
        out = rlt.format_log_slice(rows)
        assert "2026-05-11T17:47:48Z [V79-MIRROR-RECV]" in out
        assert "2026-05-11T17:47:49Z [V79-MIRROR-DISPATCH]" in out
        assert out.count("\n") == 1  # 2 lines = 1 newline

    def test_truncates_long_messages(self):
        rows = [{"timestamp": "t1", "message": "X" * 500, "severity": "info"}]
        out = rlt.format_log_slice(rows)
        assert len(out) < 500 + 50  # roughly < (280 cap + ellipsis + ts)
        assert "…" in out

    def test_caps_at_max_lines_with_tail_note(self):
        rows = [{"timestamp": f"t{i}", "message": f"line-{i}", "severity": "info"}
                for i in range(30)]
        out = rlt.format_log_slice(rows, max_lines=10)
        assert "line-0" in out
        assert "line-9" in out
        assert "line-10" not in out
        assert "+20 more matches" in out
