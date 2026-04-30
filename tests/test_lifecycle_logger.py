"""v5.13.6 \u2014 unit + integration tests for lifecycle_logger."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

# Ensure the repo root is on sys.path so ``import lifecycle_logger``
# resolves when pytest is invoked from the tests/ directory.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import lifecycle_logger as ll


@pytest.fixture
def tmp_logger(tmp_path) -> ll.LifecycleLogger:
    return ll.LifecycleLogger(data_dir=str(tmp_path), bot_version="5.13.6-test")


def _read_lines(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_compose_position_id_is_deterministic():
    pid = ll.compose_position_id("AAPL", "2026-04-29T14:30:12.453Z", "LONG")
    assert pid == "AAPL_20260429T143012Z_long"
    pid2 = ll.compose_position_id("aapl", "2026-04-29T14:30:12.453Z", "long")
    assert pid2 == pid
    pid_short = ll.compose_position_id("MSFT", "2026-04-29T14:30:12Z", "SHORT")
    assert pid_short == "MSFT_20260429T143012Z_short"


def test_open_position_writes_entry_decision(tmp_logger, tmp_path):
    pid = tmp_logger.open_position(
        ticker="AAPL",
        side="LONG",
        entry_ts_utc="2026-04-29T14:30:12.453Z",
        payload={"entry_price": 150.0, "shares": 10},
        reason_text="entry test",
    )
    assert pid == "AAPL_20260429T143012Z_long"
    file_path = tmp_path / f"{pid}.jsonl"
    assert file_path.exists()
    rows = _read_lines(file_path)
    assert len(rows) == 1
    e = rows[0]
    assert e["event_type"] == "ENTRY_DECISION"
    assert e["ticker"] == "AAPL"
    assert e["side"] == "LONG"
    assert e["event_seq"] == 1
    assert e["payload"]["entry_price"] == 150.0
    assert e["bot_version"] == "5.13.6-test"


def test_log_event_appends_with_seq(tmp_logger):
    pid = tmp_logger.open_position("MSFT", "LONG", "2026-04-29T15:00:00Z", {})
    tmp_logger.log_event(pid, "ORDER_SUBMIT", {"qty": 5})
    tmp_logger.log_event(pid, "ORDER_FILL", {"qty": 5, "fill_price": 100.0})
    rows = tmp_logger.read_events(pid)
    assert len(rows) == 3
    assert [r["event_type"] for r in rows] == ["ENTRY_DECISION", "ORDER_SUBMIT", "ORDER_FILL"]
    assert [r["event_seq"] for r in rows] == [1, 2, 3]


def test_close_position_terminal_event(tmp_logger):
    pid = tmp_logger.open_position("NVDA", "LONG", "2026-04-29T15:00:00Z", {})
    tmp_logger.log_event(pid, "EXIT_DECISION", {"exit_reason": "stop"})
    tmp_logger.close_position(pid, {"realized_pnl": -100.0, "exit_reason": "stop"})
    rows = tmp_logger.read_events(pid)
    assert rows[-1]["event_type"] == "POSITION_CLOSED"
    assert rows[-1]["payload"]["realized_pnl"] == -100.0


def test_list_positions_status_filters(tmp_logger):
    pid_open = tmp_logger.open_position("AAPL", "LONG", "2026-04-29T14:00:00Z", {})
    pid_closed = tmp_logger.open_position("MSFT", "SHORT", "2026-04-29T13:00:00Z", {})
    tmp_logger.close_position(pid_closed, {"realized_pnl": 25.0})

    open_only = tmp_logger.list_positions("open")
    closed_only = tmp_logger.list_positions("closed")
    all_rows = tmp_logger.list_positions("all")

    assert {r["position_id"] for r in open_only} == {pid_open}
    assert {r["position_id"] for r in closed_only} == {pid_closed}
    assert {r["position_id"] for r in all_rows} == {pid_open, pid_closed}


def test_read_events_since_seq_pagination(tmp_logger):
    pid = tmp_logger.open_position("TSLA", "LONG", "2026-04-29T15:00:00Z", {})
    for _ in range(4):
        tmp_logger.log_event(pid, "PHASE4_SENTINEL", {"alarm_codes": []})
    all_rows = tmp_logger.read_events(pid)
    assert len(all_rows) == 5
    tail = tmp_logger.read_events(pid, since_seq=2)
    assert [r["event_seq"] for r in tail] == [3, 4, 5]


def test_concurrent_appends_serialize(tmp_logger):
    pid = tmp_logger.open_position("AMD", "LONG", "2026-04-29T15:00:00Z", {})
    n = 50
    threads = []

    def worker(i):
        tmp_logger.log_event(pid, "REASON", {"i": i})

    for i in range(n):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    rows = tmp_logger.read_events(pid)
    # ENTRY_DECISION + 50 worker REASONs.
    assert len(rows) == n + 1
    # All event_seq values should be unique (no race lost a write).
    seqs = sorted(r["event_seq"] for r in rows)
    assert seqs == list(range(1, n + 2))


def test_position_id_survives_restart(tmp_path):
    log1 = ll.LifecycleLogger(data_dir=str(tmp_path), bot_version="t")
    pid = log1.open_position("AAPL", "LONG", "2026-04-29T14:30:12.453Z", {})
    log1.log_event(pid, "ORDER_FILL", {"qty": 1})
    # Simulate restart - new logger instance, same dir.
    log2 = ll.LifecycleLogger(data_dir=str(tmp_path), bot_version="t")
    rows = log2.read_events(pid)
    assert len(rows) == 2
    metas = log2.list_positions("all")
    assert any(m["position_id"] == pid for m in metas)


def test_bad_position_id_rejected(tmp_logger):
    assert tmp_logger.read_events("../../etc/passwd") == []
    assert tmp_logger.read_events("not_a_position_id") == []


def test_list_recent_sorts_by_last_event(tmp_logger):
    pid_a = tmp_logger.open_position("AAPL", "LONG", "2026-04-29T14:00:00Z", {})
    pid_b = tmp_logger.open_position("MSFT", "LONG", "2026-04-29T13:00:00Z", {})
    # B is older by entry_ts but gets a fresh event so "recent" puts it first.
    import time

    time.sleep(0.001)
    tmp_logger.log_event(pid_b, "PHASE4_SENTINEL", {"alarm_codes": ["A_LOSS"]})
    rows = tmp_logger.list_positions("recent")
    assert rows[0]["position_id"] == pid_b


def test_unknown_event_type_still_writes(tmp_logger):
    pid = tmp_logger.open_position("AAPL", "LONG", "2026-04-29T14:00:00Z", {})
    ok = tmp_logger.log_event(pid, "WEIRD_EVENT", {"x": 1})
    assert ok is True
    rows = tmp_logger.read_events(pid)
    assert rows[-1]["event_type"] == "WEIRD_EVENT"


def test_default_logger_singleton_idempotent():
    a = ll.get_default_logger("v")
    b = ll.get_default_logger("v")
    assert a is b


def test_reset_default_logger_for_tests(tmp_path):
    fresh = ll.reset_default_logger_for_tests(data_dir=str(tmp_path), bot_version="x")
    assert isinstance(fresh, ll.LifecycleLogger)
    assert fresh._data_dir == Path(str(tmp_path))
