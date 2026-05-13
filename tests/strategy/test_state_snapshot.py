"""v8.3.24 -- tests for tools/state_snapshot.py.

The snapshot tool is the producer side of the snapshots-live branch
pipeline. These tests cover the unit-testable surface:
  - _pull_all marks failing endpoints with __error__ and keeps the
    successful ones intact.
  - _write_outputs writes both latest.json and the per-day .jsonl.
  - The .jsonl file is append-only (multiple runs accumulate
    lines).
  - main() returns the documented exit codes (1 config, 2 network).

The login flow itself is exercised by tools/dashboard_monitor's
existing test suite -- we reuse the same DashboardClient.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest


# Stub telegram so dashboard_monitor (which state_snapshot imports
# from) loads cleanly in sandboxes without the optional dep.
if "telegram" not in sys.modules:
    _t = ModuleType("telegram")
    sys.modules["telegram"] = _t


from tools import state_snapshot


class TestPullAll:

    def test_records_errors_per_endpoint(self):
        client = MagicMock()
        # /api/state returns a valid payload; the others raise.
        def fake_get(path):
            if path == "/api/state":
                return {"bot_version": "8.3.24"}
            raise RuntimeError(f"boom on {path}")
        client.get_json.side_effect = fake_get

        bundle = state_snapshot._pull_all(client)

        assert bundle["/api/state"] == {"bot_version": "8.3.24"}
        for path in ("/api/executor/val", "/api/executor/gene"):
            assert "__error__" in bundle[path]
            assert "boom on" in bundle[path]["__error__"]

    def test_all_success(self):
        client = MagicMock()
        client.get_json.side_effect = lambda p: {"path": p, "ok": True}
        bundle = state_snapshot._pull_all(client)
        assert len(bundle) == len(state_snapshot.ENDPOINTS_TO_PULL)
        for path in state_snapshot.ENDPOINTS_TO_PULL:
            assert bundle[path] == {"path": path, "ok": True}


class TestWriteOutputs:

    def test_writes_latest_and_jsonl(self, tmp_path):
        snap = {
            "schema_version": 1,
            "captured_at_utc": "2026-05-12T18:30:00Z",
            "dashboard_base_url": "https://example",
            "endpoints": {"/api/state": {"bot_version": "8.3.24"}},
        }
        latest, daily = state_snapshot._write_outputs(snap, tmp_path)

        assert latest == tmp_path / "latest.json"
        assert daily == tmp_path / "2026-05-12.jsonl"

        loaded = json.loads(latest.read_text())
        assert loaded["endpoints"]["/api/state"]["bot_version"] == "8.3.24"

        lines = daily.read_text().splitlines()
        assert len(lines) == 1
        line_doc = json.loads(lines[0])
        assert line_doc["captured_at_utc"] == "2026-05-12T18:30:00Z"

    def test_jsonl_appends_across_runs(self, tmp_path):
        for hh, mm in [("18", "30"), ("18", "40"), ("18", "50")]:
            snap = {
                "schema_version": 1,
                "captured_at_utc": f"2026-05-12T{hh}:{mm}:00Z",
                "dashboard_base_url": "https://example",
                "endpoints": {"/api/state": {"trades_today": int(mm)}},
            }
            state_snapshot._write_outputs(snap, tmp_path)

        daily = tmp_path / "2026-05-12.jsonl"
        lines = daily.read_text().splitlines()
        assert len(lines) == 3
        # latest.json holds only the last write
        latest = json.loads((tmp_path / "latest.json").read_text())
        assert latest["captured_at_utc"] == "2026-05-12T18:50:00Z"

    def test_creates_parent_dir(self, tmp_path):
        nested = tmp_path / "deep" / "tree" / "snapshots"
        snap = {
            "schema_version": 1,
            "captured_at_utc": "2026-05-12T18:00:00Z",
            "dashboard_base_url": "https://example",
            "endpoints": {},
        }
        state_snapshot._write_outputs(snap, nested)
        assert (nested / "latest.json").exists()


class TestMainExitCodes:

    def test_missing_env_returns_1(self, monkeypatch, capsys):
        # _require_env uses sys.exit(1) (caught by pytest as SystemExit).
        monkeypatch.delenv("DASHBOARD_BASE_URL", raising=False)
        monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
        with pytest.raises(SystemExit) as exc:
            state_snapshot.main()
        assert exc.value.code == 1

    def test_state_unreachable_returns_2(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DASHBOARD_BASE_URL", "https://example")
        monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
        monkeypatch.setenv("STATE_SNAPSHOT_DIR", str(tmp_path))
        monkeypatch.setenv("STATE_SNAPSHOT_QUIET", "1")

        # Patch DashboardClient so login is a no-op and every GET
        # raises -- /api/state will end up with __error__ and main()
        # must return 2.
        fake_client = MagicMock()
        fake_client.login = MagicMock(return_value=None)
        fake_client.get_json.side_effect = RuntimeError("network down")
        monkeypatch.setattr(
            state_snapshot, "DashboardClient",
            lambda *a, **kw: fake_client,
        )

        assert state_snapshot.main() == 2

    def test_happy_path_returns_0(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DASHBOARD_BASE_URL", "https://example")
        monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
        monkeypatch.setenv("STATE_SNAPSHOT_DIR", str(tmp_path))
        monkeypatch.setenv("STATE_SNAPSHOT_QUIET", "1")

        fake_client = MagicMock()
        fake_client.login = MagicMock(return_value=None)
        fake_client.get_json.side_effect = lambda p: {
            "path": p, "bot_version": "8.3.24",
        } if p == "/api/state" else {"path": p}
        monkeypatch.setattr(
            state_snapshot, "DashboardClient",
            lambda *a, **kw: fake_client,
        )

        assert state_snapshot.main() == 0
        assert (tmp_path / "latest.json").exists()
        # Per-day jsonl uses today's UTC date -- not asserting the
        # exact filename here since it's clock-dependent; just check
        # at least one .jsonl exists.
        jsonls = list(tmp_path.glob("*.jsonl"))
        assert len(jsonls) == 1


class TestEndpointConstant:

    def test_endpoints_pinned(self):
        # v8.3.25 -- added /api/trade_log to the pull list.
        assert state_snapshot.ENDPOINTS_TO_PULL == (
            "/api/state",
            "/api/executor/val",
            "/api/executor/gene",
            "/api/trade_log?limit=5000",
        )
