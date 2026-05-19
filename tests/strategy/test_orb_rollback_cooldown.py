"""v9.1.131 -- rollback-cooldown gate in orb.live_runtime.

Validates:
1. Default OFF (env unset or ORB_ROLLBACK_COOLDOWN_AFTER_N=0): never blocks.
2. With N=3: 4th admit on same (pid, ticker, side) within window is blocked.
3. Sliding-window pruning: rollbacks outside the window don't count.
4. side="" parameter on rollback_admit() is a no-op for tracking (backward-compat).
5. Different sides on the same ticker are tracked independently.
"""
from __future__ import annotations

import os
import time

import pytest

from orb import live_runtime as lr


@pytest.fixture(autouse=True)
def _clean_env_and_state():
    """Snapshot + restore env, clear cooldown state between tests."""
    keys = ("ORB_ROLLBACK_COOLDOWN_AFTER_N", "ORB_ROLLBACK_COOLDOWN_MIN")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    with lr._rollback_lock:
        lr._rollback_history.clear()
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    with lr._rollback_lock:
        lr._rollback_history.clear()


def test_on_by_default_with_default_threshold():
    """v9.1.132: no env vars set -> defaults to N=3 in 10-min, blocks at 3+."""
    lr._record_rollback("main", "TSLA", "short")
    lr._record_rollback("main", "TSLA", "short")
    assert lr._check_rollback_cooldown("main", "TSLA", "short") is None
    lr._record_rollback("main", "TSLA", "short")
    reason = lr._check_rollback_cooldown("main", "TSLA", "short")
    assert reason is not None and "rollback_cooldown" in reason


def test_disabled_when_after_n_is_zero():
    """Explicit ORB_ROLLBACK_COOLDOWN_AFTER_N=0 disables the gate."""
    os.environ["ORB_ROLLBACK_COOLDOWN_AFTER_N"] = "0"
    for _ in range(20):
        lr._record_rollback("main", "TSLA", "short")
    assert lr._check_rollback_cooldown("main", "TSLA", "short") is None


def test_blocks_after_threshold():
    os.environ["ORB_ROLLBACK_COOLDOWN_AFTER_N"] = "3"
    os.environ["ORB_ROLLBACK_COOLDOWN_MIN"] = "10"
    # First 3 rollbacks do not yet trigger
    for i in range(3):
        # Before this rollback, count = i, < N=3, so no block
        assert lr._check_rollback_cooldown("main", "TSLA", "short") is None
        lr._record_rollback("main", "TSLA", "short")
    # After 3 rollbacks, gate fires
    reason = lr._check_rollback_cooldown("main", "TSLA", "short")
    assert reason is not None
    assert "rollback_cooldown" in reason
    assert "3 rollbacks" in reason


def test_window_pruning():
    """Old rollbacks outside the window don't count toward threshold."""
    os.environ["ORB_ROLLBACK_COOLDOWN_AFTER_N"] = "3"
    os.environ["ORB_ROLLBACK_COOLDOWN_MIN"] = "10"
    # Plant 5 fake-old rollbacks (15 min ago) directly in state
    now = time.time()
    old_ts = now - (15 * 60.0)  # 15 minutes ago, outside 10-min window
    key = ("main", "TSLA", "short")
    with lr._rollback_lock:
        lr._rollback_history[key] = [old_ts] * 5
    # Gate should NOT fire -- pruning drops all 5 old entries
    assert lr._check_rollback_cooldown("main", "TSLA", "short") is None
    # After pruning, state should be empty for this key
    with lr._rollback_lock:
        assert lr._rollback_history.get(key) == []


def test_side_empty_is_noop_for_tracking():
    """rollback_admit called without side= shouldn't record (backward-compat)."""
    os.environ["ORB_ROLLBACK_COOLDOWN_AFTER_N"] = "1"
    # Calling _record_rollback with empty side is a no-op
    for _ in range(10):
        lr._record_rollback("main", "TSLA", "")
    # Still no entries under any key
    with lr._rollback_lock:
        assert all(
            len(v) == 0 for k, v in lr._rollback_history.items()
        ) or not lr._rollback_history


def test_independent_sides():
    """long rollbacks shouldn't affect short cooldown and vice versa."""
    os.environ["ORB_ROLLBACK_COOLDOWN_AFTER_N"] = "2"
    os.environ["ORB_ROLLBACK_COOLDOWN_MIN"] = "10"
    # 3 long rollbacks
    for _ in range(3):
        lr._record_rollback("main", "TSLA", "long")
    # Long is blocked
    assert lr._check_rollback_cooldown("main", "TSLA", "long") is not None
    # Short still clear
    assert lr._check_rollback_cooldown("main", "TSLA", "short") is None


def test_independent_portfolios():
    """main rollbacks shouldn't affect val cooldown."""
    os.environ["ORB_ROLLBACK_COOLDOWN_AFTER_N"] = "2"
    for _ in range(3):
        lr._record_rollback("main", "TSLA", "short")
    assert lr._check_rollback_cooldown("main", "TSLA", "short") is not None
    assert lr._check_rollback_cooldown("val", "TSLA", "short") is None
