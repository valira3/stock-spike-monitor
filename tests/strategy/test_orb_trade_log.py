"""v10.0.1 -- orb.trade_log extracted from trade_genius.py.

Internal-contract tests for the carved module. The pre-existing
end-to-end coverage (broker/orders.py callers, telegram_commands
read paths, synthetic_harness monkeypatch) is unchanged because
trade_genius.py keeps the public re-exports.

Covered:
  - trade_log_append: writes one JSONL line, includes schema_version
    + bot_version, rejects rows missing required fields without
    raising, clears _trade_log_last_error on success
  - trade_log_append: OSError on write sets _trade_log_last_error
    + returns False without raising
  - _trade_log_snapshot_pos: long / short / non-dict / missing
    initial_stop fallback paths
  - trade_log_read_tail: empty file, malformed-line skip, since_date
    filter, portfolio filter, limit truncation
  - get_last_error: returns the live module value
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest import mock

import pytest


def _fresh_module(tmp_path, monkeypatch):
    """Reload orb.trade_log with TRADE_LOG_PATH pointed at tmp_path."""
    target = tmp_path / "trade_log.jsonl"
    monkeypatch.setenv("TRADE_LOG_PATH", str(target))
    import orb.trade_log as tl
    importlib.reload(tl)
    return tl, target


# ---------------------------------------------------------------------------
# trade_log_append -- happy path + required-field validation
# ---------------------------------------------------------------------------


def test_trade_log_append_writes_jsonl_line(tmp_path, monkeypatch):
    tl, target = _fresh_module(tmp_path, monkeypatch)
    ok = tl.trade_log_append(
        {"ticker": "AAPL", "side": "LONG", "pnl": 123.45, "reason": "STOP"}
    )
    assert ok is True
    assert tl._trade_log_last_error is None
    lines = target.read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["ticker"] == "AAPL"
    assert row["schema_version"] == tl.TRADE_LOG_SCHEMA_VERSION
    assert "bot_version" in row


def test_trade_log_append_rejects_missing_field(tmp_path, monkeypatch):
    tl, target = _fresh_module(tmp_path, monkeypatch)
    ok = tl.trade_log_append(
        {"ticker": "AAPL", "side": "LONG"},  # missing pnl + reason
    )
    assert ok is False
    assert tl._trade_log_last_error is not None
    assert "missing field" in tl._trade_log_last_error
    # File should NOT have been opened for write
    assert not target.exists() or target.read_text() == ""


def test_trade_log_append_clears_last_error_on_success(tmp_path, monkeypatch):
    tl, target = _fresh_module(tmp_path, monkeypatch)
    # First, force an error by missing field
    tl.trade_log_append({"ticker": "AAPL"})
    assert tl._trade_log_last_error is not None
    # Then a valid call must clear it
    tl.trade_log_append(
        {"ticker": "AAPL", "side": "LONG", "pnl": 1.0, "reason": "STOP"}
    )
    assert tl._trade_log_last_error is None


def test_trade_log_append_oserror_records_state(tmp_path, monkeypatch):
    tl, target = _fresh_module(tmp_path, monkeypatch)
    # Point at a non-writable path so the open() fails
    tl.TRADE_LOG_FILE = str(tmp_path / "nonexistent_dir" / "x.jsonl")
    ok = tl.trade_log_append(
        {"ticker": "AAPL", "side": "LONG", "pnl": 1.0, "reason": "STOP"}
    )
    assert ok is False
    assert tl._trade_log_last_error is not None
    # Error message includes the exception type
    assert any(t in tl._trade_log_last_error for t in ("FileNotFoundError", "NotADirectoryError", "OSError"))


def test_trade_log_append_multiple_lines_appended(tmp_path, monkeypatch):
    tl, target = _fresh_module(tmp_path, monkeypatch)
    for i in range(5):
        tl.trade_log_append(
            {"ticker": f"T{i}", "side": "LONG", "pnl": float(i), "reason": "STOP"}
        )
    rows = [json.loads(ln) for ln in target.read_text().splitlines() if ln.strip()]
    assert len(rows) == 5
    assert [r["ticker"] for r in rows] == ["T0", "T1", "T2", "T3", "T4"]


# ---------------------------------------------------------------------------
# _trade_log_snapshot_pos -- shape across long/short/non-dict/legacy
# ---------------------------------------------------------------------------


def test_snapshot_pos_non_dict_returns_all_none(tmp_path, monkeypatch):
    tl, _ = _fresh_module(tmp_path, monkeypatch)
    d = tl._trade_log_snapshot_pos(None)
    for k in (
        "trail_active_at_exit", "trail_stop_at_exit", "trail_anchor_at_exit",
        "hard_stop_at_exit", "effective_stop_at_exit", "entry_stop",
    ):
        assert d[k] is None


def test_snapshot_pos_long_with_trail_active(tmp_path, monkeypatch):
    tl, _ = _fresh_module(tmp_path, monkeypatch)
    pos = {
        "trail_active": True, "trail_stop": 200.0, "trail_high": 210.0,
        "stop": 195.0, "initial_stop": 190.0,
    }
    d = tl._trade_log_snapshot_pos(pos)
    assert d["trail_active_at_exit"] is True
    assert d["trail_stop_at_exit"] == 200.0
    assert d["trail_anchor_at_exit"] == 210.0
    assert d["hard_stop_at_exit"] == 195.0
    # trail active + trail_stop present -> effective = trail_stop
    assert d["effective_stop_at_exit"] == 200.0
    assert d["entry_stop"] == 190.0


def test_snapshot_pos_short_uses_trail_low(tmp_path, monkeypatch):
    tl, _ = _fresh_module(tmp_path, monkeypatch)
    pos = {
        "trail_active": True, "trail_stop": 100.0, "trail_low": 95.0,
        "stop": 105.0, "initial_stop": 110.0,
    }
    d = tl._trade_log_snapshot_pos(pos)
    assert d["trail_anchor_at_exit"] == 95.0


def test_snapshot_pos_trail_not_active_uses_hard_stop(tmp_path, monkeypatch):
    tl, _ = _fresh_module(tmp_path, monkeypatch)
    pos = {
        "trail_active": False, "trail_stop": 200.0, "trail_high": 210.0,
        "stop": 195.0, "initial_stop": 190.0,
    }
    d = tl._trade_log_snapshot_pos(pos)
    # trail NOT active -> effective falls back to hard_stop
    assert d["effective_stop_at_exit"] == 195.0


def test_snapshot_pos_missing_initial_stop_falls_back_to_hard_stop(tmp_path, monkeypatch):
    tl, _ = _fresh_module(tmp_path, monkeypatch)
    pos = {
        "trail_active": False, "stop": 195.0,
        # no `initial_stop` key (pre-v7.107.0 legacy position)
    }
    d = tl._trade_log_snapshot_pos(pos)
    # Pre-v7.107.0 fallback: entry_stop = hard_stop
    assert d["entry_stop"] == 195.0


# ---------------------------------------------------------------------------
# trade_log_read_tail
# ---------------------------------------------------------------------------


def test_read_tail_returns_empty_when_file_missing(tmp_path, monkeypatch):
    tl, target = _fresh_module(tmp_path, monkeypatch)
    assert not target.exists()
    assert tl.trade_log_read_tail() == []


def test_read_tail_returns_rows_newest_last(tmp_path, monkeypatch):
    tl, target = _fresh_module(tmp_path, monkeypatch)
    for i, t in enumerate(("AAPL", "MSFT", "NVDA")):
        tl.trade_log_append(
            {"ticker": t, "side": "LONG", "pnl": float(i), "reason": "STOP"}
        )
    rows = tl.trade_log_read_tail()
    assert [r["ticker"] for r in rows] == ["AAPL", "MSFT", "NVDA"]


def test_read_tail_skips_malformed_lines(tmp_path, monkeypatch):
    tl, target = _fresh_module(tmp_path, monkeypatch)
    # Write 2 valid lines plus a corrupted one in the middle
    tl.trade_log_append(
        {"ticker": "A", "side": "LONG", "pnl": 1.0, "reason": "STOP"}
    )
    with target.open("a") as f:
        f.write("not-json-{{{\n")
    tl.trade_log_append(
        {"ticker": "B", "side": "LONG", "pnl": 2.0, "reason": "STOP"}
    )
    rows = tl.trade_log_read_tail()
    assert [r["ticker"] for r in rows] == ["A", "B"]


def test_read_tail_filters_by_since_date(tmp_path, monkeypatch):
    tl, target = _fresh_module(tmp_path, monkeypatch)
    tl.trade_log_append(
        {"ticker": "A", "side": "LONG", "pnl": 1.0, "reason": "STOP", "date": "2026-01-01"}
    )
    tl.trade_log_append(
        {"ticker": "B", "side": "LONG", "pnl": 2.0, "reason": "STOP", "date": "2026-06-01"}
    )
    rows = tl.trade_log_read_tail(since_date="2026-03-01")
    assert [r["ticker"] for r in rows] == ["B"]


def test_read_tail_filters_by_portfolio(tmp_path, monkeypatch):
    tl, target = _fresh_module(tmp_path, monkeypatch)
    tl.trade_log_append(
        {"ticker": "A", "side": "LONG", "pnl": 1.0, "reason": "STOP", "portfolio": "paper"}
    )
    tl.trade_log_append(
        {"ticker": "B", "side": "LONG", "pnl": 2.0, "reason": "STOP", "portfolio": "tp"}
    )
    rows = tl.trade_log_read_tail(portfolio="paper")
    assert [r["ticker"] for r in rows] == ["A"]


def test_read_tail_limit_truncates_to_newest(tmp_path, monkeypatch):
    tl, target = _fresh_module(tmp_path, monkeypatch)
    for i in range(10):
        tl.trade_log_append(
            {"ticker": f"T{i}", "side": "LONG", "pnl": float(i), "reason": "STOP"}
        )
    rows = tl.trade_log_read_tail(limit=3)
    assert [r["ticker"] for r in rows] == ["T7", "T8", "T9"]


# ---------------------------------------------------------------------------
# get_last_error accessor
# ---------------------------------------------------------------------------


def test_get_last_error_returns_live_value(tmp_path, monkeypatch):
    tl, target = _fresh_module(tmp_path, monkeypatch)
    assert tl.get_last_error() is None
    tl.trade_log_append({"ticker": "X"})  # missing fields -> error
    assert tl.get_last_error() is not None
    assert "missing field" in tl.get_last_error()


# ---------------------------------------------------------------------------
# trade_genius re-export shim
# ---------------------------------------------------------------------------
# Note: directly importing trade_genius requires the `telegram` package,
# which the dev sandbox doesn't install. We exercise the shim path
# indirectly via the existing test_v5_5_7_dashboard_main_fix.py + the
# broker/orders.py integration that already imports trade_genius.
