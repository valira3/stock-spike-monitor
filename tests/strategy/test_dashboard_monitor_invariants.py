"""Tests for tools.dashboard_monitor_invariants -- the v7.75.0
cross-check invariants that detect anomalies from /api/state alone.

These invariants are pure functions of the payload dict, so we don't
need network or the live dashboard.  Each test constructs the minimum
payload required to exercise the path and asserts ok/!ok + summary.
"""
from __future__ import annotations

from tools.dashboard_monitor_invariants import (
    InvariantContext,
    inv_or_locked_after_or_end,
    inv_or_window_data_quality,
    inv_position_count_three_way,
    inv_equity_self_consistent,
)


def _ctx(state, exec_val=None, exec_gene=None, v10_proj=None):
    return InvariantContext(
        payloads={
            "state": state,
            "exec_val": exec_val or {},
            "exec_gene": exec_gene or {},
            "v10_proj": v10_proj or {},
        },
        base_url="https://example.com",
    )


def _state_with_v10(or_windows, mode="OPEN", config=None):
    return {
        "regime": {"mode": mode},
        "v10": {
            "available": True,
            "bootstrapped": True,
            "or_windows": or_windows,
            "config": config or {"or_minutes": 30},
        },
    }


# ---------------------------------------------------------------------
# inv_or_locked_after_or_end
# ---------------------------------------------------------------------


class TestOrLockedAfterOrEnd:

    def test_skips_when_v10_not_bootstrapped(self):
        ctx = _ctx({"regime": {"mode": "OPEN"},
                    "v10": {"available": False}})
        r = inv_or_locked_after_or_end(ctx)
        assert r["ok"]
        assert "skipped" in r["summary"]

    def test_skips_when_regime_is_PRE(self):
        s = _state_with_v10({}, mode="PRE")
        r = inv_or_locked_after_or_end(_ctx(s))
        assert r["ok"]
        assert "PRE" in r["summary"]

    def test_skips_when_regime_is_OR(self):
        # OR phase legitimately has unlocked windows
        s = _state_with_v10({}, mode="OR")
        r = inv_or_locked_after_or_end(_ctx(s))
        assert r["ok"]

    def test_fails_when_zero_locked_during_OPEN(self):
        """The exact 2026-05-11 production scenario: 0/10 LOCKED at
        10:14 ET (mode=OPEN), nothing locked."""
        s = _state_with_v10({
            "AAPL": {"locked": False, "bars_seen": 0},
            "NVDA": {"locked": False, "bars_seen": 0},
            "TSLA": {"locked": False, "bars_seen": 0},
        }, mode="OPEN")
        r = inv_or_locked_after_or_end(_ctx(s))
        assert not r["ok"]
        assert "0/3" in r["summary"]
        assert "OPEN" in r["summary"]

    def test_passes_when_at_least_one_locked(self):
        s = _state_with_v10({
            "AAPL": {"locked": True, "bars_seen": 30, "or_high": 101, "or_low": 99},
            "NVDA": {"locked": False, "bars_seen": 0},
        }, mode="OPEN")
        r = inv_or_locked_after_or_end(_ctx(s))
        assert r["ok"]
        assert "1/2" in r["summary"]

    def test_fails_when_or_windows_dict_is_empty(self):
        s = _state_with_v10({}, mode="OPEN")
        r = inv_or_locked_after_or_end(_ctx(s))
        assert not r["ok"]
        assert "empty" in r["summary"]


# ---------------------------------------------------------------------
# inv_or_window_data_quality
# ---------------------------------------------------------------------


class TestOrWindowDataQuality:

    def test_passes_with_full_or_windows(self):
        s = _state_with_v10({
            "AAPL": {"locked": True, "bars_seen": 30},
            "NVDA": {"locked": True, "bars_seen": 29},
            "TSLA": {"locked": True, "bars_seen": 30},
        }, mode="OPEN")
        r = inv_or_window_data_quality(_ctx(s))
        assert r["ok"]

    def test_passes_when_single_ticker_thin(self):
        # 1 thin ticker is normal -- maybe genuinely illiquid
        s = _state_with_v10({
            "AAPL": {"locked": True, "bars_seen": 30},
            "NVDA": {"locked": True, "bars_seen": 5},
        }, mode="OPEN")
        r = inv_or_window_data_quality(_ctx(s))
        assert r["ok"]

    def test_fails_when_three_or_more_thin_locked_windows(self):
        # 3+ thin windows -> upstream bar-source problem
        s = _state_with_v10({
            "AAPL": {"locked": True, "bars_seen": 5},
            "NVDA": {"locked": True, "bars_seen": 8},
            "TSLA": {"locked": True, "bars_seen": 10},
            "GOOG": {"locked": True, "bars_seen": 30},  # this one OK
        }, mode="OPEN")
        r = inv_or_window_data_quality(_ctx(s))
        assert not r["ok"]
        assert "3" in r["summary"]
        # Unlocked windows must not count
        assert "GOOG" not in r["detail"]

    def test_ignores_unlocked_windows(self):
        # Unlocked windows can have any bars_seen and shouldn't trip the check
        s = _state_with_v10({
            "AAPL": {"locked": False, "bars_seen": 0},
            "NVDA": {"locked": False, "bars_seen": 0},
            "TSLA": {"locked": False, "bars_seen": 0},
        }, mode="OPEN")
        r = inv_or_window_data_quality(_ctx(s))
        assert r["ok"]


# ---------------------------------------------------------------------
# inv_position_count_three_way
# ---------------------------------------------------------------------


class TestPositionCountThreeWay:

    def test_skips_when_state_missing(self):
        r = inv_position_count_three_way(_ctx({}))
        assert r["ok"]
        # Note: empty {} is a valid dict so it goes through, but
        # without portfolio key the values are 0/0/0/0 -> ok

    def test_passes_when_all_zero(self):
        s = {
            "portfolio": {"broker_open_n": 0},
            "portfolios": {"main": {}, "val": {}, "gene": {}},
            "positions": [],
        }
        r = inv_position_count_three_way(_ctx(s))
        assert r["ok"]
        assert "main=0" in r["summary"]

    def test_passes_when_internal_has_positions(self):
        # Internal book has positions -- not a phantom even if broker
        # also reports them.
        s = {
            "portfolio": {"broker_open_n": 2},
            "portfolios": {
                "main": {"positions": [{"ticker": "AAPL"}]},
                "val": {"positions": [{"ticker": "NVDA"}]},
                "gene": {"positions": []},
            },
            "positions": [{"ticker": "AAPL"}],
        }
        r = inv_position_count_three_way(_ctx(s))
        assert r["ok"]

    def test_fails_when_broker_has_positions_but_all_books_empty(self):
        # The exact scenario the operator is asking about:
        # broker reports an open position but no internal book knows.
        s = {
            "portfolio": {"broker_open_n": 1},
            "portfolios": {"main": {}, "val": {}, "gene": {}},
            "positions": [],
        }
        r = inv_position_count_three_way(_ctx(s))
        assert not r["ok"]
        assert "phantom at broker" in r["summary"]
        assert "broker_open_n=1" in r["detail"]


# ---------------------------------------------------------------------
# inv_equity_self_consistent
# ---------------------------------------------------------------------


class TestEquitySelfConsistent:

    def test_passes_when_components_match(self):
        s = {"portfolio": {
            "cash": 50000.0,
            "long_mv": 60000.0,
            "short_liab": 5000.0,
            "equity": 105000.0,
        }}
        r = inv_equity_self_consistent(_ctx(s))
        assert r["ok"]

    def test_passes_within_float_tolerance(self):
        s = {"portfolio": {
            "cash": 50000.001,
            "long_mv": 60000.002,
            "short_liab": 5000.003,
            "equity": 105000.0,
        }}
        r = inv_equity_self_consistent(_ctx(s))
        assert r["ok"]

    def test_fails_on_large_divergence(self):
        s = {"portfolio": {
            "cash": 50000.0,
            "long_mv": 60000.0,
            "short_liab": 5000.0,
            # eq should be 105000 but reports 99000 (off by $6000)
            "equity": 99000.0,
        }}
        r = inv_equity_self_consistent(_ctx(s))
        assert not r["ok"]
        assert "99000.00" in r["summary"]
        assert "105000.00" in r["summary"]

    def test_skips_when_components_missing(self):
        s = {"portfolio": {"equity": 100000.0}}
        r = inv_equity_self_consistent(_ctx(s))
        assert r["ok"]
        assert "skipped" in r["summary"]
