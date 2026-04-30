"""v5.19.3 \\u2014 dashboard login lifetime extended to 90 days.

The signing key has been persistent across redeploys since v3.4.29, but
SESSION_DAYS was still 7 \\u2014 forcing Val to re-enter the dashboard
password every week even though the cookie infrastructure could carry
him much longer. This test pins the new value so a future drive-by edit
can't quietly shorten it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DASHBOARD_PASSWORD", "smoketest1234")

import dashboard_server  # noqa: E402


def test_session_days_is_90():
    """SESSION_DAYS controls cookie Max-Age; 90 days is the v5.19.3 floor."""
    assert dashboard_server.SESSION_DAYS == 90


def test_session_cookie_name_unchanged():
    """Cookie name is part of the operator runbook (curl -c jar.txt etc)."""
    assert dashboard_server.SESSION_COOKIE == "spike_session"


def test_make_token_round_trips_via_check_auth(monkeypatch):
    """A freshly-issued token validates immediately via _check_auth."""
    # Force a known secret so the test is deterministic.
    monkeypatch.setattr(dashboard_server, "_SESSION_SECRET", bytes([0x42]) * 32)
    token = dashboard_server._make_token()
    assert ":" in token

    class _FakeReq:
        cookies = {dashboard_server.SESSION_COOKIE: token}

    assert dashboard_server._check_auth(_FakeReq())


def test_make_token_rejects_after_session_window(monkeypatch):
    """A token older than SESSION_DAYS * 86400 seconds must fail _check_auth."""
    monkeypatch.setattr(dashboard_server, "_SESSION_SECRET", bytes([0x99]) * 32)
    # Issue a token timestamped 91 days ago (just past the 90-day window).
    import time

    old_now = time.time() - (91 * 86400)
    token = dashboard_server._make_token(now=old_now)

    class _FakeReq:
        cookies = {dashboard_server.SESSION_COOKIE: token}

    assert not dashboard_server._check_auth(_FakeReq())
