"""v6.15.0 broker fidelity: open-position P/L snapshot to JSONL.

Covers:
  - Schema of a snapshot row written to /data/open_pnl.jsonl
  - Aggregation of total_unrealized across multiple positions
  - Empty-positions case (no open positions still writes a header row)
  - read_latest_open_pnl tail-reads the most recent row
  - Missing/malformed file degrades gracefully (returns None)
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _fake_position(symbol, qty, avg, mark, unreal):
    return SimpleNamespace(
        symbol=symbol,
        qty=qty,
        avg_entry_price=avg,
        current_price=mark,
        unrealized_pl=unreal,
    )


class _FakeClient:
    def __init__(self, positions):
        self._positions = positions
        self.call_count = 0

    def get_all_positions(self):
        self.call_count += 1
        return self._positions


def test_snapshot_schema_single_position(tmp_path, monkeypatch):
    """Single-position snapshot writes one JSONL row with full schema."""
    out = tmp_path / "open_pnl.jsonl"
    monkeypatch.setenv("OPEN_PNL_PATH", str(out))
    # Reload the module so OPEN_PNL_PATH picks up the env override.
    import importlib

    import broker.open_pnl as _op

    importlib.reload(_op)

    client = _FakeClient(
        [_fake_position("AVGO", 22.0, 263.41, 262.70, -15.62)]
    )
    rec = _op.snapshot_open_pnl(client, bot_version="6.15.0")

    assert rec is not None
    assert rec["bot_version"] == "6.15.0"
    assert rec["n_open"] == 1
    assert rec["total_unrealized"] == pytest.approx(-15.62)
    assert "ts_utc" in rec
    assert rec["ts_utc"].endswith("Z")
    assert rec["positions"] == [
        {"symbol": "AVGO", "qty": 22.0, "avg": 263.41,
         "mark": 262.70, "unrealized": -15.62}
    ]

    # File written, one line, parses back.
    raw = out.read_text().splitlines()
    assert len(raw) == 1
    assert json.loads(raw[0]) == rec


def test_snapshot_aggregates_multiple_positions(tmp_path, monkeypatch):
    out = tmp_path / "open_pnl.jsonl"
    monkeypatch.setenv("OPEN_PNL_PATH", str(out))
    import importlib

    import broker.open_pnl as _op

    importlib.reload(_op)

    positions = [
        _fake_position("AVGO", 22.0, 263.41, 262.70, -15.62),
        _fake_position("NFLX", 5.0, 980.10, 990.20, 50.50),
        _fake_position("TSLA", 10.0, 200.00, 199.10, -9.00),
    ]
    rec = _op.snapshot_open_pnl(_FakeClient(positions), bot_version="6.15.0")

    assert rec["n_open"] == 3
    # -15.62 + 50.50 + -9.00 = 25.88
    assert rec["total_unrealized"] == pytest.approx(25.88)
    syms = [p["symbol"] for p in rec["positions"]]
    assert syms == ["AVGO", "NFLX", "TSLA"]


def test_snapshot_empty_positions_writes_zero_row(tmp_path, monkeypatch):
    """Flat account still emits a heartbeat row so the dashboard knows
    the snapshotter is alive (and total_unrealized is genuinely 0).
    """
    out = tmp_path / "open_pnl.jsonl"
    monkeypatch.setenv("OPEN_PNL_PATH", str(out))
    import importlib

    import broker.open_pnl as _op

    importlib.reload(_op)

    rec = _op.snapshot_open_pnl(_FakeClient([]), bot_version="6.15.0")
    assert rec is not None
    assert rec["n_open"] == 0
    assert rec["total_unrealized"] == pytest.approx(0.0)
    assert rec["positions"] == []
    assert out.exists() and out.read_text().strip() != ""


def test_snapshot_returns_none_on_no_client(tmp_path, monkeypatch):
    out = tmp_path / "open_pnl.jsonl"
    monkeypatch.setenv("OPEN_PNL_PATH", str(out))
    import importlib

    import broker.open_pnl as _op

    importlib.reload(_op)

    assert _op.snapshot_open_pnl(None, bot_version="6.15.0") is None
    assert not out.exists()


def test_snapshot_returns_none_on_get_positions_error(tmp_path, monkeypatch):
    out = tmp_path / "open_pnl.jsonl"
    monkeypatch.setenv("OPEN_PNL_PATH", str(out))
    import importlib

    import broker.open_pnl as _op

    importlib.reload(_op)

    class _Bad:
        def get_all_positions(self):
            raise RuntimeError("alpaca down")

    assert _op.snapshot_open_pnl(_Bad(), bot_version="6.15.0") is None
    assert not out.exists()


def test_read_latest_open_pnl_tails_last_row(tmp_path):
    """read_latest_open_pnl returns the most recent JSONL row only."""
    from broker.open_pnl import read_latest_open_pnl

    p = tmp_path / "open_pnl.jsonl"
    rows = [
        {"ts_utc": "2026-05-05T16:00:00Z", "bot_version": "6.15.0",
         "n_open": 1, "total_unrealized": -10.00, "positions": []},
        {"ts_utc": "2026-05-05T16:00:30Z", "bot_version": "6.15.0",
         "n_open": 2, "total_unrealized": -25.50, "positions": []},
        {"ts_utc": "2026-05-05T16:01:00Z", "bot_version": "6.15.0",
         "n_open": 1, "total_unrealized": -7.25, "positions": []},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    rec = read_latest_open_pnl(p)
    assert rec is not None
    assert rec["ts_utc"] == "2026-05-05T16:01:00Z"
    assert rec["total_unrealized"] == pytest.approx(-7.25)


def test_read_latest_open_pnl_missing_file_returns_none(tmp_path):
    from broker.open_pnl import read_latest_open_pnl

    assert read_latest_open_pnl(tmp_path / "does_not_exist.jsonl") is None


def test_read_latest_open_pnl_malformed_last_line_returns_none(tmp_path):
    from broker.open_pnl import read_latest_open_pnl

    p = tmp_path / "open_pnl.jsonl"
    p.write_text("{not valid json\n")
    assert read_latest_open_pnl(p) is None
