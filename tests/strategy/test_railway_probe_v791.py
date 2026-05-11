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
    with patch.dict("os.environ", {"RAILWAY_API_TOKEN": "", "RAILWAY_SERVICE_ID": "svc-abc"}, clear=False):
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
    with patch.dict(
        "os.environ",
        {"RAILWAY_API_TOKEN": "tok", "RAILWAY_SERVICE_ID": "svc"},
        clear=False,
    ), patch("tools.railway_log_tail._gql", return_value=None):
        out = probe_railway_access()
    assert out["status"] == "auth_failed"
    assert out["token_set"] is True
    assert out["service_set"] is True


def test_probe_auth_failed_when_no_data_key():
    """A 200 response with no 'data' key is a schema drift, treated
    the same as a hard failure -- not 'ok'.
    """
    with patch.dict(
        "os.environ",
        {"RAILWAY_API_TOKEN": "tok", "RAILWAY_SERVICE_ID": "svc"},
        clear=False,
    ), patch("tools.railway_log_tail._gql", return_value={"errors": ["whatever"]}):
        out = probe_railway_access()
    assert out["status"] == "auth_failed"


def test_probe_no_deployment_when_edges_empty():
    fake = {"data": {"deployments": {"edges": []}}}
    with patch.dict(
        "os.environ",
        {"RAILWAY_API_TOKEN": "tok", "RAILWAY_SERVICE_ID": "svc"},
        clear=False,
    ), patch("tools.railway_log_tail._gql", return_value=fake):
        out = probe_railway_access()
    assert out["status"] == "no_deployment"


def test_probe_ok_returns_deployment_id():
    fake = {"data": {"deployments": {"edges": [{"node": {"id": "dep-123"}}]}}}
    with patch.dict(
        "os.environ",
        {"RAILWAY_API_TOKEN": "tok", "RAILWAY_SERVICE_ID": "svc"},
        clear=False,
    ), patch("tools.railway_log_tail._gql", return_value=fake):
        out = probe_railway_access()
    assert out["status"] == "ok"
    assert out["deployment_id"] == "dep-123"


def test_probe_token_only_whitespace_treated_missing():
    with patch.dict(
        "os.environ",
        {"RAILWAY_API_TOKEN": "   ", "RAILWAY_SERVICE_ID": "svc"},
        clear=False,
    ):
        out = probe_railway_access()
    assert out["status"] == "missing_token"
    assert out["token_set"] is False
