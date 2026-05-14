"""v7.91.0 -- tests for tools.railway_log_tail.probe_railway_access.

The probe is a one-call structured diagnostic that distinguishes
"secret not set" from "secret set but Railway rejected the call"
from "auth ok but no deployment" from "ok". Today's diagnostic
ambiguity (issue #568 "no slice attached -- secrets may be unset
OR window empty") is exactly what the probe is designed to break.
"""

from unittest.mock import patch

from tools.railway_log_tail import probe_railway_access


def test_probe_missing_token():
    with patch.dict(
        "os.environ",
        {"RAILWAY_API_TOKEN": "", "RAILWAY_SERVICE_ID": "svc-abc", "RAILWAY_USE_CLI": "0"},
        clear=False,
    ):
        out = probe_railway_access()
    assert out["status"] == "missing_token"
    assert out["token_set"] is False
    assert out["service_set"] is True


def test_probe_missing_service():
    with patch.dict(
        "os.environ",
        {"RAILWAY_API_TOKEN": "tok-xyz", "RAILWAY_SERVICE_ID": ""},
        clear=False,
    ):
        out = probe_railway_access()
    assert out["status"] == "missing_service"
    assert out["token_set"] is True
    assert out["service_set"] is False


def test_probe_auth_failed_when_gql_returns_none():
    """_gql returns None on HTTP error / network failure / schema
    drift. The probe must map that to status='auth_failed' rather
    than masking it as 'ok'.
    """
    with (
        patch.dict(
            "os.environ",
            {"RAILWAY_API_TOKEN": "tok", "RAILWAY_SERVICE_ID": "svc"},
            clear=False,
        ),
        patch("tools.railway_log_tail._gql", return_value=None),
    ):
        out = probe_railway_access()
    assert out["status"] == "auth_failed"
    assert out["token_set"] is True
    assert out["service_set"] is True


def test_probe_auth_failed_when_no_data_key():
    """A 200 response with no 'data' key is a schema drift, treated
    the same as a hard failure -- not 'ok'.
    """
    with (
        patch.dict(
            "os.environ",
            {"RAILWAY_API_TOKEN": "tok", "RAILWAY_SERVICE_ID": "svc"},
            clear=False,
        ),
        patch("tools.railway_log_tail._gql", return_value={"errors": ["whatever"]}),
    ):
        out = probe_railway_access()
    assert out["status"] == "auth_failed"


def test_probe_no_deployment_when_edges_empty():
    fake = {"data": {"deployments": {"edges": []}}}
    with (
        patch.dict(
            "os.environ",
            {"RAILWAY_API_TOKEN": "tok", "RAILWAY_SERVICE_ID": "svc"},
            clear=False,
        ),
        patch("tools.railway_log_tail._gql", return_value=fake),
    ):
        out = probe_railway_access()
    assert out["status"] == "no_deployment"


def test_probe_ok_returns_deployment_id():
    fake = {
        "data": {
            "deployments": {
                "edges": [
                    {
                        "node": {
                            "id": "dep-123",
                            "status": "SUCCESS",
                            "createdAt": "2026-05-12T00:30:00Z",
                        }
                    }
                ]
            }
        }
    }
    with (
        patch.dict(
            "os.environ",
            {"RAILWAY_API_TOKEN": "tok", "RAILWAY_SERVICE_ID": "svc"},
            clear=False,
        ),
        patch("tools.railway_log_tail._gql", return_value=fake),
    ):
        out = probe_railway_access()
    assert out["status"] == "ok"
    assert out["deployment_id"] == "dep-123"
    # v7.97.0 -- deployment status returned alongside the id
    assert out["deployment_status"] == "SUCCESS"
    # v7.98.0 -- createdAt returned alongside id+status
    assert out["deployment_created"] == "2026-05-12T00:30:00Z"


def test_probe_ok_returns_deployment_status_when_stale():
    """v7.97.0 -- regression: when the resolver picks a non-running
    deployment (REMOVED / FAILED / CRASHED), the probe must surface
    that so the monitor footer can flag it. This is the smoking-gun
    pattern from issue #583 (lines_fetched=0 with status=ok).
    """
    fake = {
        "data": {
            "deployments": {
                "edges": [
                    {
                        "node": {
                            "id": "dep-old",
                            "status": "REMOVED",
                            "createdAt": "2026-04-01T12:00:00Z",
                        }
                    }
                ]
            }
        }
    }
    with (
        patch.dict(
            "os.environ",
            {"RAILWAY_API_TOKEN": "tok", "RAILWAY_SERVICE_ID": "svc"},
            clear=False,
        ),
        patch("tools.railway_log_tail._gql", return_value=fake),
    ):
        out = probe_railway_access()
    assert out["status"] == "ok"  # auth works
    assert out["deployment_id"] == "dep-old"
    assert out["deployment_status"] == "REMOVED"
    # v7.98.0 -- old createdAt would tell the operator this
    # deployment is stale even if Railway reports it as REMOVED.
    assert out["deployment_created"] == "2026-04-01T12:00:00Z"


# v7.100.0 -- regression: when Railway returns HTTP 200 with a
# non-empty `errors` array (schema drift, deprecated field), _gql
# should still return the dict (so callers can read it) AND record
# the error messages in _last_gql_errors so the monitor footer
# surfaces them.
def test_gql_captures_errors_into_last_gql_errors():
    from tools import railway_log_tail as rlt

    fake = {
        "errors": [
            {"message": "Cannot query field 'startDate' on type 'Query'"},
            {"message": "Unknown argument 'endDate' on field 'deploymentLogs'"},
        ],
        "data": None,
    }
    rlt._last_gql_errors = []  # reset
    with patch(
        "tools.railway_log_tail._gql", side_effect=lambda *a, **kw: rlt._record_errors(fake)
    ):
        # Force the probe path that calls _gql -> _record_errors.
        with patch.dict(
            "os.environ", {"RAILWAY_API_TOKEN": "tok", "RAILWAY_SERVICE_ID": "svc"}, clear=False
        ):
            probe_railway_access()
    errs = rlt.get_last_gql_errors()
    assert any("startDate" in e for e in errs)
    assert any("deploymentLogs" in e for e in errs)


def test_gql_clears_errors_on_clean_response():
    """When a response has no `errors` field, _record_errors should
    clear the module-global so a stale error from a previous call
    doesn't leak into the next probe's footer."""
    from tools import railway_log_tail as rlt

    rlt._last_gql_errors = ["stale error from previous run"]
    rlt._record_errors({"data": {"deployments": {"edges": []}}})
    assert rlt.get_last_gql_errors() == []


def test_probe_token_only_whitespace_treated_missing():
    with patch.dict(
        "os.environ",
        {"RAILWAY_API_TOKEN": "   ", "RAILWAY_SERVICE_ID": "svc"},
        clear=False,
    ):
        out = probe_railway_access()
    assert out["status"] == "missing_token"
    assert out["token_set"] is False
