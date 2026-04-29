"""v5.13.6 \u2014 dashboard /api/lifecycle/* contract tests.

Direct in-process invocation of the aiohttp handlers. Bypasses the auth
check via a request stub that always passes.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def lifecycle_server(monkeypatch, tmp_path):
    """Spin up the dashboard server module pointed at a temp lifecycle dir."""
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    monkeypatch.setenv("LIFECYCLE_DIR", str(tmp_path))
    if "lifecycle_logger" in sys.modules:
        del sys.modules["lifecycle_logger"]
    if "dashboard_server" in sys.modules:
        del sys.modules["dashboard_server"]
    import lifecycle_logger as ll
    import dashboard_server as ds

    # Ensure default logger uses the temp dir.
    fresh = ll.reset_default_logger_for_tests(data_dir=str(tmp_path), bot_version="t")
    # Bypass dashboard auth for tests.
    monkeypatch.setattr(ds, "_check_auth", lambda req: True)
    return ds, fresh


class FakeRequest:
    def __init__(self, query=None, match_info=None):
        self.query = query or {}
        self.match_info = match_info or {}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_lifecycle_positions_empty(lifecycle_server):
    ds, _ = lifecycle_server
    resp = _run(ds.h_lifecycle_positions(FakeRequest({"status": "all"})))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["ok"] is True
    assert body["positions"] == []


def test_lifecycle_positions_lists_after_open(lifecycle_server):
    ds, ll = lifecycle_server
    pid = ll.open_position("AAPL", "LONG", "2026-04-29T14:30:12Z", {"x": 1})
    resp = _run(ds.h_lifecycle_positions(FakeRequest({"status": "open", "limit": "5"})))
    body = json.loads(resp.body)
    assert body["ok"] is True
    assert len(body["positions"]) == 1
    assert body["positions"][0]["position_id"] == pid
    assert body["positions"][0]["status"] == "open"


def test_lifecycle_position_full_timeline(lifecycle_server):
    ds, ll = lifecycle_server
    pid = ll.open_position("MSFT", "SHORT", "2026-04-29T15:00:00Z", {})
    ll.log_event(pid, "PHASE4_SENTINEL", {"alarm_codes": ["A1"]})
    ll.log_event(pid, "EXIT_DECISION", {"exit_reason": "stop"})
    ll.close_position(pid, {"realized_pnl": -50.0})

    resp = _run(ds.h_lifecycle_position(FakeRequest(match_info={"position_id": pid})))
    body = json.loads(resp.body)
    assert body["ok"] is True
    assert body["count"] == 4
    assert [e["event_type"] for e in body["events"]] == [
        "ENTRY_DECISION",
        "PHASE4_SENTINEL",
        "EXIT_DECISION",
        "POSITION_CLOSED",
    ]


def test_lifecycle_position_since_seq_pagination(lifecycle_server):
    ds, ll = lifecycle_server
    pid = ll.open_position("NVDA", "LONG", "2026-04-29T15:00:00Z", {})
    for _ in range(3):
        ll.log_event(pid, "REASON", {"x": 1})

    resp = _run(
        ds.h_lifecycle_position(
            FakeRequest(
                query={"since_seq": "2"},
                match_info={"position_id": pid},
            )
        )
    )
    body = json.loads(resp.body)
    assert body["count"] == 2
    assert all(e["event_seq"] > 2 for e in body["events"])


def test_lifecycle_position_rejects_bad_id(lifecycle_server):
    ds, _ = lifecycle_server
    resp = _run(
        ds.h_lifecycle_position(
            FakeRequest(
                match_info={"position_id": "../../etc/passwd"},
            )
        )
    )
    assert resp.status == 400
