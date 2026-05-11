"""v7.92.0 -- tests for Railway GraphQL auth-header fallback.

Pre-v7.92.0 _gql only sent `Authorization: Bearer <token>`. Project-
scoped Railway tokens use `Project-Access-Token: <token>` instead
and silently fail under Bearer auth. v7.92.0 tries Bearer first
and falls back to Project-Access-Token on 401/403.

We mock urllib.request.urlopen rather than reaching out over the
network, asserting the request headers we'd actually send.
"""

import io
import json
from unittest.mock import patch

import urllib.error
import urllib.request

from tools.railway_log_tail import _gql


def _make_response(payload: dict):
    body = json.dumps(payload).encode()
    return io.BytesIO(body)


class _Resp(io.BytesIO):
    """Minimal stand-in for the context-manager response object
    yielded by urllib.request.urlopen. Closes cleanly with __exit__."""
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def _resp(payload: dict) -> _Resp:
    return _Resp(json.dumps(payload).encode())


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://backboard.railway.com/graphql/v2",
        code=code,
        msg="auth fail",
        hdrs=None,
        fp=io.BytesIO(b'{"errors": ["unauthorized"]}'),
    )


def test_gql_succeeds_with_bearer_on_first_try():
    """Personal/team token -- Bearer works first time, no fallback fires."""
    calls: list[dict] = []
    fake_payload = {"data": {"hello": "world"}}

    def fake_urlopen(req, *a, **kw):
        calls.append(dict(req.header_items()))
        return _resp(fake_payload)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = _gql("tok-personal", "query{x}", {})

    assert out == fake_payload
    assert len(calls) == 1
    headers = {k.lower(): v for k, v in calls[0].items()}
    assert headers.get("authorization") == "Bearer tok-personal"
    assert "project-access-token" not in headers


def test_gql_falls_back_to_project_token_on_401():
    """Project token -- Bearer returns 401, retry with Project-Access-Token wins."""
    calls: list[dict] = []
    fake_payload = {"data": {"hello": "project"}}

    def fake_urlopen(req, *a, **kw):
        h = dict(req.header_items())
        calls.append(h)
        # First call (Bearer) -> 401; second call (Project-Access-Token) -> ok.
        if any(v.startswith("Bearer ") for v in h.values()):
            raise _http_error(401)
        return _resp(fake_payload)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = _gql("tok-project", "query{x}", {})

    assert out == fake_payload
    assert len(calls) == 2
    # First call carried Bearer; second carried Project-Access-Token.
    h1 = {k.lower(): v for k, v in calls[0].items()}
    h2 = {k.lower(): v for k, v in calls[1].items()}
    assert h1.get("authorization") == "Bearer tok-project"
    assert "project-access-token" not in h1
    assert h2.get("project-access-token") == "tok-project"
    assert "authorization" not in h2


def test_gql_returns_none_when_both_attempts_auth_fail():
    """A token that is wrong/expired/scopeless fails on BOTH headers
    and the helper falls back to None for downstream callers."""
    calls: list[dict] = []

    def fake_urlopen(req, *a, **kw):
        calls.append(dict(req.header_items()))
        raise _http_error(401)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = _gql("garbage-token", "query{x}", {})

    assert out is None
    assert len(calls) == 2  # tried both shapes


def test_gql_returns_none_on_non_auth_http_error_without_fallback():
    """A 500 (server-side) error is not an auth problem; we should
    return None immediately rather than wasting a retry. v7.92.0
    only retries on 401/403."""
    calls: list[int] = []

    def fake_urlopen(req, *a, **kw):
        calls.append(1)
        raise _http_error(500)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = _gql("tok", "query{x}", {})

    assert out is None
    assert len(calls) == 1  # no fallback attempt
