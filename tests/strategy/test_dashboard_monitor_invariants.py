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
    inv_v10_in_pos_has_internal_position,
    inv_risk_book_notional_cap_nonzero,
    inv_railway_logs_clean,
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


# ---------------------------------------------------------------------
# inv_v10_in_pos_has_internal_position (v7.76.0)
# ---------------------------------------------------------------------


def _state_with_v10_day_states(day_states, positions_per_pid=None):
    """Build a /api/state-shaped payload with v10.day_states and
    optionally a portfolios.{pid}.positions list per portfolio."""
    portfolios = {}
    for pid in ("main", "val", "gene"):
        portfolios[pid] = {"positions": (positions_per_pid or {}).get(pid, [])}
    return {
        "regime": {"mode": "OPEN"},
        "portfolios": portfolios,
        "positions": (positions_per_pid or {}).get("main", []),
        "v10": {
            "available": True,
            "bootstrapped": True,
            "day_states": day_states,
        },
    }


class TestV10InPosHasInternalPosition:

    def test_passes_when_no_day_states(self):
        s = _state_with_v10_day_states([])
        r = inv_v10_in_pos_has_internal_position(_ctx(s))
        assert r["ok"]
        assert "skipped" in r["summary"]

    def test_passes_when_in_pos_has_matching_position(self):
        s = _state_with_v10_day_states(
            day_states=[
                {"portfolio_id": "main", "ticker": "AAPL",
                 "phase": "in_pos", "in_position": True,
                 "last_entry_iso": "2026-05-11T13:32:00Z"},
            ],
            positions_per_pid={"main": [{"ticker": "AAPL"}]},
        )
        r = inv_v10_in_pos_has_internal_position(_ctx(s))
        assert r["ok"]

    def test_fails_on_phantom_in_pos(self):
        """The exact 2026-05-11 production scenario: AAPL is IN POS in
        the v10 ticker matrix but no entry in positions."""
        s = _state_with_v10_day_states(
            day_states=[
                {"portfolio_id": "main", "ticker": "AAPL",
                 "phase": "in_pos", "in_position": True,
                 "last_entry_iso": "2026-05-11T13:32:00Z"},
            ],
            positions_per_pid={"main": []},
        )
        r = inv_v10_in_pos_has_internal_position(_ctx(s))
        assert not r["ok"]
        assert "phantom IN_POS" in r["summary"]
        assert "main/AAPL" in r["detail"]

    def test_passes_when_in_pos_for_other_phase(self):
        # ARMED / BLOCKED_* tickers are not checked; only IN_POS.
        s = _state_with_v10_day_states(
            day_states=[
                {"portfolio_id": "main", "ticker": "AAPL",
                 "phase": "armed", "in_position": False},
                {"portfolio_id": "main", "ticker": "NVDA",
                 "phase": "blocked_range", "in_position": False},
            ],
            positions_per_pid={"main": []},
        )
        r = inv_v10_in_pos_has_internal_position(_ctx(s))
        assert r["ok"]

    def test_detects_phantom_in_val_portfolio(self):
        # Val's executor positions live under portfolios.val.positions
        s = _state_with_v10_day_states(
            day_states=[
                {"portfolio_id": "val", "ticker": "TSLA",
                 "phase": "in_pos", "in_position": True},
            ],
            positions_per_pid={"val": []},
        )
        r = inv_v10_in_pos_has_internal_position(_ctx(s))
        assert not r["ok"]
        assert "val/TSLA" in r["detail"]

    def test_falls_back_to_top_level_positions_for_main(self):
        # Legacy / pre-v7.0.0 schema: state.positions (top-level)
        # rather than state.portfolios.main.positions
        s = {
            "regime": {"mode": "OPEN"},
            "positions": [{"ticker": "AAPL"}],
            "portfolios": {"main": {}},  # no positions key here
            "v10": {
                "available": True,
                "bootstrapped": True,
                "day_states": [
                    {"portfolio_id": "main", "ticker": "AAPL",
                     "phase": "in_pos", "in_position": True},
                ],
            },
        }
        r = inv_v10_in_pos_has_internal_position(_ctx(s))
        assert r["ok"]


# ---------------------------------------------------------------------
# inv_risk_book_notional_cap_nonzero (v7.76.0)
# ---------------------------------------------------------------------


def _state_with_risk_books(books, mode="OPEN"):
    return {
        "regime": {"mode": mode},
        "v10": {
            "available": True,
            "bootstrapped": True,
            "risk_books": books,
        },
    }


class TestRiskBookNotionalCapNonzero:

    def test_passes_when_all_books_nonzero(self):
        s = _state_with_risk_books({
            "main": {"equity": 100000.0, "max_notional": 200000.0},
            "val":  {"equity": 99273.10, "max_notional": 198546.20},
            "gene": {"equity": 99500.0, "max_notional": 199000.0},
        })
        r = inv_risk_book_notional_cap_nonzero(_ctx(s))
        assert r["ok"]
        assert "3" in r["summary"]

    def test_skips_when_v10_not_bootstrapped(self):
        s = {"regime": {"mode": "OPEN"},
             "v10": {"available": False}}
        r = inv_risk_book_notional_cap_nonzero(_ctx(s))
        assert r["ok"]
        assert "skipped" in r["summary"]

    def test_skips_outside_rth_modes(self):
        s = _state_with_risk_books(
            {"val": {"equity": 0.0, "max_notional": 0.0}},
            mode="AFTER",
        )
        r = inv_risk_book_notional_cap_nonzero(_ctx(s))
        assert r["ok"]
        assert "AFTER" in r["summary"]

    def test_fails_when_val_has_zero_cap(self):
        """The exact 2026-05-11 Val tab scenario: Val has equity=0
        and max_notional=0, every entry rejects on notional_cap."""
        s = _state_with_risk_books({
            "main": {"equity": 99552.28, "max_notional": 199104.56},
            "val":  {"equity": 0.0, "max_notional": 0.0,
                     "last_reject_reason":
                     "notional_cap (would-be $293 > $0)"},
            "gene": {"equity": 99500.0, "max_notional": 199000.0},
        })
        r = inv_risk_book_notional_cap_nonzero(_ctx(s))
        assert not r["ok"]
        assert "1" in r["summary"]
        assert "val: equity=0.0 max_notional=0.0" in r["detail"]

    def test_fails_when_all_three_have_zero(self):
        s = _state_with_risk_books({
            "main": {"equity": 0.0, "max_notional": 0.0},
            "val":  {"equity": 0.0, "max_notional": 0.0},
            "gene": {"equity": 0.0, "max_notional": 0.0},
        })
        r = inv_risk_book_notional_cap_nonzero(_ctx(s))
        assert not r["ok"]
        assert "3" in r["summary"]


# ---------------------------------------------------------------------
# inv_railway_logs_clean (v7.79.0)
# ---------------------------------------------------------------------


class TestRailwayLogsClean:

    def test_skips_when_log_fetch_returns_empty(self, monkeypatch):
        # No RAILWAY_API_TOKEN / RAILWAY_SERVICE_ID -> fetch returns [].
        import tools.railway_log_tail as rlt
        monkeypatch.setattr(rlt, "fetch_recent_logs", lambda limit=500: [])
        r = inv_railway_logs_clean(_ctx({}))
        assert r["ok"]
        assert "skipped" in r["summary"]

    def test_passes_when_logs_have_no_signals(self, monkeypatch):
        import tools.railway_log_tail as rlt
        clean_logs = [
            {"timestamp": "t1", "message": "[V79-ORB-ENTRY] long X", "severity": "info"},
            {"timestamp": "t2", "message": "SCAN CYCLE done", "severity": "info"},
        ]
        monkeypatch.setattr(rlt, "fetch_recent_logs", lambda limit=500: clean_logs)
        r = inv_railway_logs_clean(_ctx({}))
        assert r["ok"]
        assert "scanned 2 lines" in r["summary"]

    def test_fails_on_critical_signal_at_count_1(self, monkeypatch):
        import tools.railway_log_tail as rlt
        bad_logs = [
            {"timestamp": "t1",
             "message": "[ALPACA-ERR] insufficient_buying_power",
             "severity": "warning"},
        ]
        monkeypatch.setattr(rlt, "fetch_recent_logs", lambda limit=500: bad_logs)
        r = inv_railway_logs_clean(_ctx({}))
        assert not r["ok"]
        assert "alpaca_error" in r["detail"]
        assert "CRITICAL" in r["detail"]

    def test_soft_signal_passes_below_threshold(self, monkeypatch):
        import tools.railway_log_tail as rlt
        # 4 hits is below the >=5 threshold for soft signals
        soft_logs = [
            {"timestamp": f"t{i}",
             "message": f"[paper] skip MSFT -- insufficient cash {i}",
             "severity": "info"}
            for i in range(4)
        ]
        monkeypatch.setattr(rlt, "fetch_recent_logs", lambda limit=500: soft_logs)
        r = inv_railway_logs_clean(_ctx({}))
        assert r["ok"]

    def test_soft_signal_fails_at_threshold(self, monkeypatch):
        import tools.railway_log_tail as rlt
        soft_logs = [
            {"timestamp": f"t{i}",
             "message": f"[paper] skip MSFT -- insufficient cash {i}",
             "severity": "info"}
            for i in range(5)
        ]
        monkeypatch.setattr(rlt, "fetch_recent_logs", lambda limit=500: soft_logs)
        r = inv_railway_logs_clean(_ctx({}))
        assert not r["ok"]
        assert "insufficient_cash" in r["detail"]
        assert "SOFT" in r["detail"]

    def test_handles_import_error_gracefully(self, monkeypatch):
        # Simulate the module disappearing -- should ok-skip, not crash.
        import sys
        # Save original to restore
        orig = sys.modules.get("tools.railway_log_tail")
        sys.modules["tools.railway_log_tail"] = None  # makes import raise
        try:
            r = inv_railway_logs_clean(_ctx({}))
            assert r["ok"]
            assert "import failed" in r["summary"] or "skipped" in r["summary"]
        finally:
            if orig is not None:
                sys.modules["tools.railway_log_tail"] = orig
            else:
                sys.modules.pop("tools.railway_log_tail", None)


# ---------------------------------------------------------------------
# v7.83.0 -- dormant unconfigured portfolio skip
# ---------------------------------------------------------------------


class TestRiskBookDormantSkipV783:
    """v7.83.0 -- portfolios that look unconfigured (admit_count=0 +
    reject_count>0 with $0 equity, e.g. Gene without Alpaca keys) get a
    quieter ok-skip rather than firing a fresh GH issue every 10 min."""

    def test_dormant_gene_skips_not_fails(self):
        s = _state_with_risk_books({
            "main": {"equity": 99552.28, "max_notional": 199104.56,
                     "admit_count": 5, "reject_count": 1},
            "val":  {"equity": 99500.0, "max_notional": 199000.0,
                     "admit_count": 5, "reject_count": 1},
            "gene": {"equity": 0.0, "max_notional": 0.0,
                     "admit_count": 0, "reject_count": 12,
                     "last_reject_reason":
                     "notional_cap (would-be $440 > $0)"},
        })
        r = inv_risk_book_notional_cap_nonzero(_ctx(s))
        assert r["ok"]
        assert "dormant" in r["summary"]
        assert "gene(12)" in r["summary"]

    def test_actively_trading_portfolio_with_zero_equity_still_fails(self):
        """If a portfolio has had admits (admit_count>0) AND now its
        equity dropped to 0, that's a real bug (not the unconfigured
        pattern). Still fail loudly."""
        s = _state_with_risk_books({
            "val": {"equity": 0.0, "max_notional": 0.0,
                    "admit_count": 3, "reject_count": 0,
                    "last_reject_reason": ""},
        })
        r = inv_risk_book_notional_cap_nonzero(_ctx(s))
        assert not r["ok"]
        assert "val: equity=0.0" in r["detail"]

    def test_mixed_dormant_and_real_failure(self):
        """Gene dormant + Val actively-stuck = still a fail (Val is the
        real bug; Gene is just informational)."""
        s = _state_with_risk_books({
            "main": {"equity": 99552.28, "max_notional": 199104.56,
                     "admit_count": 5, "reject_count": 0},
            "val":  {"equity": 0.0, "max_notional": 0.0,
                     "admit_count": 2, "reject_count": 0},
            "gene": {"equity": 0.0, "max_notional": 0.0,
                     "admit_count": 0, "reject_count": 12},
        })
        r = inv_risk_book_notional_cap_nonzero(_ctx(s))
        assert not r["ok"]
        # Val (real bug) appears in detail; Gene (dormant) does not.
        assert "val: equity=0.0" in r["detail"]
        assert "gene" not in r["detail"]


# ---------------------------------------------------------------------
# v7.83.0 -- equity_matches_baseline tolerance loosened
# ---------------------------------------------------------------------


class TestEquityMatchesBaselineToleranceV783:
    """v7.83.0 -- production observed $200-$420 drifts (sub-0.5%)
    between /api/state.portfolio.equity and /api/v10/projection.live_balance
    due to between-snapshot MTM timing race. Loosened from $1+0.01%
    to $500+0.5% so this stops firing every 10min."""

    def test_500_drift_within_tolerance(self):
        from tools.dashboard_monitor_invariants import inv_equity_matches_baseline
        s = {"portfolio": {"equity": 99500.0}}
        proj = {"live_balance": 99100.0}  # $400 drift, sub-0.5%
        ctx = InvariantContext(
            payloads={"state": s, "v10_proj": proj},
            base_url="https://example.com",
        )
        r = inv_equity_matches_baseline(ctx)
        assert r["ok"]

    def test_above_500_but_within_pct_tolerance(self):
        from tools.dashboard_monitor_invariants import inv_equity_matches_baseline
        # $1M book; $499 drift would have failed under absolute-only,
        # but 0.5% of $1M = $5000 covers it.
        s = {"portfolio": {"equity": 1_000_000.0}}
        proj = {"live_balance": 1_000_499.0}
        ctx = InvariantContext(
            payloads={"state": s, "v10_proj": proj},
            base_url="https://example.com",
        )
        r = inv_equity_matches_baseline(ctx)
        assert r["ok"]

    def test_huge_drift_still_fails(self):
        from tools.dashboard_monitor_invariants import inv_equity_matches_baseline
        s = {"portfolio": {"equity": 99500.0}}
        proj = {"live_balance": 90000.0}  # $9500 drift = ~10%
        ctx = InvariantContext(
            payloads={"state": s, "v10_proj": proj},
            base_url="https://example.com",
        )
        r = inv_equity_matches_baseline(ctx)
        assert not r["ok"]
        assert "drifted apart beyond" in r["detail"]
