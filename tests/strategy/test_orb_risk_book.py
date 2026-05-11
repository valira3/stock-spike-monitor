"""Tests for orb.risk_book -- per-portfolio concurrent-risk admission gate."""
from __future__ import annotations

import threading

import pytest

from orb.risk_book import RiskBook, RiskBookRegistry


class TestRiskBookBasics:

    def test_initial_state(self):
        rb = RiskBook(portfolio_id="main", max_concurrent_risk_dollars=2000.0,
                      equity=100000.0, max_concurrent_notional_mult=2.0)
        assert rb.open_risk == 0.0
        assert rb.open_notional == 0.0
        assert rb.open_count == 0
        assert rb.max_risk_dollars == 2000.0
        assert rb.max_notional == 200000.0
        assert rb.equity == 100000.0

    def test_simple_admit_then_release(self):
        rb = RiskBook(portfolio_id="main", max_concurrent_risk_dollars=2000.0,
                      equity=100000.0)
        ticket = rb.try_admit(risk_dollars=500.0, notional=10000.0)
        assert ticket is not None
        assert rb.open_risk == 500.0
        assert rb.open_notional == 10000.0
        assert rb.open_count == 1
        # Release
        ok = rb.release(ticket)
        assert ok
        assert rb.open_risk == 0.0
        assert rb.open_notional == 0.0
        assert rb.open_count == 0

    def test_admit_at_exact_risk_cap(self):
        rb = RiskBook(portfolio_id="main", max_concurrent_risk_dollars=2000.0,
                      equity=100000.0)
        t = rb.try_admit(risk_dollars=2000.0, notional=10000.0)
        assert t is not None  # exact cap is allowed (with tiny epsilon for fp)
        assert rb.open_risk == 2000.0

    def test_admit_above_risk_cap_rejected(self):
        rb = RiskBook(portfolio_id="main", max_concurrent_risk_dollars=2000.0,
                      equity=100000.0)
        t = rb.try_admit(risk_dollars=2001.0, notional=10000.0)
        assert t is None
        assert "risk_cap" in rb.last_reject_reason
        assert rb.reject_count == 1
        assert rb.admit_count == 0

    def test_admit_above_notional_cap_rejected(self):
        rb = RiskBook(portfolio_id="main", max_concurrent_risk_dollars=2000.0,
                      equity=100000.0, max_concurrent_notional_mult=2.0)
        # max_notional = 200k; try to fit 250k
        t = rb.try_admit(risk_dollars=500.0, notional=250000.0)
        assert t is None
        assert "notional_cap" in rb.last_reject_reason

    def test_negative_size_rejected(self):
        rb = RiskBook(portfolio_id="main")
        t = rb.try_admit(risk_dollars=-100.0, notional=10000.0)
        assert t is None
        assert "negative_size" in rb.last_reject_reason

    def test_release_unknown_ticket_returns_false(self):
        from orb.risk_book import _Ticket
        rb = RiskBook(portfolio_id="main")
        fake = _Ticket(ticket_id="fake-id", risk_dollars=100, notional=1000)
        ok = rb.release(fake)
        assert not ok

    def test_release_idempotent(self):
        rb = RiskBook(portfolio_id="main")
        t = rb.try_admit(risk_dollars=500.0, notional=10000.0)
        assert rb.release(t)
        # Second release of same ticket
        assert not rb.release(t)
        # State remains zero
        assert rb.open_risk == 0.0


class TestRiskBookConcurrent:

    def test_two_admits_under_cap(self):
        rb = RiskBook(portfolio_id="main", max_concurrent_risk_dollars=2000.0,
                      equity=100000.0)
        t1 = rb.try_admit(risk_dollars=750.0, notional=15000.0)
        t2 = rb.try_admit(risk_dollars=750.0, notional=15000.0)
        assert t1 is not None
        assert t2 is not None
        assert rb.open_risk == 1500.0
        assert rb.open_count == 2

    def test_third_admit_over_cap_rejected(self):
        """Two tickets at $750 = $1500. A third $750 would total $2250 > $2000."""
        rb = RiskBook(portfolio_id="main", max_concurrent_risk_dollars=2000.0)
        t1 = rb.try_admit(750.0, 15000.0)
        t2 = rb.try_admit(750.0, 15000.0)
        t3 = rb.try_admit(750.0, 15000.0)
        assert t1 is not None
        assert t2 is not None
        assert t3 is None
        assert rb.admit_count == 2
        assert rb.reject_count == 1

    def test_release_then_readmit_works(self):
        rb = RiskBook(portfolio_id="main", max_concurrent_risk_dollars=2000.0)
        t1 = rb.try_admit(750.0, 15000.0)
        t2 = rb.try_admit(750.0, 15000.0)
        # Now at $1500 of $2000; a third $750 would reject
        rb.release(t1)
        # Now at $750; another $750 fits
        t3 = rb.try_admit(750.0, 15000.0)
        assert t3 is not None
        assert rb.open_risk == 1500.0

    def test_threading_stress(self):
        """20 threads racing to admit; total admitted risk must not exceed cap."""
        rb = RiskBook(portfolio_id="main", max_concurrent_risk_dollars=1000.0)
        results: list = []
        lock = threading.Lock()

        def worker():
            t = rb.try_admit(risk_dollars=200.0, notional=4000.0)
            with lock:
                results.append(t)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        accepted = [t for t in results if t is not None]
        rejected = [t for t in results if t is None]
        # cap=$1000, each=$200, so exactly 5 accepted, 15 rejected
        assert len(accepted) == 5
        assert len(rejected) == 15
        assert rb.open_risk == 1000.0


class TestRiskBookEquityUpdate:

    def test_update_equity_changes_max_notional(self):
        rb = RiskBook(portfolio_id="main", equity=100000.0,
                      max_concurrent_notional_mult=2.0)
        assert rb.max_notional == 200000.0
        rb.update_equity(150000.0)
        assert rb.max_notional == 300000.0

    def test_update_equity_does_not_revoke_open_tickets(self):
        rb = RiskBook(portfolio_id="main", equity=100000.0,
                      max_concurrent_risk_dollars=2000.0)
        t = rb.try_admit(1500.0, 50000.0)
        assert t is not None
        # Equity changes; existing ticket should NOT be revoked.
        rb.update_equity(50000.0)
        assert rb.open_risk == 1500.0
        # Release still works
        assert rb.release(t)


class TestRiskBookSnapshot:

    def test_snapshot_shape(self):
        rb = RiskBook(portfolio_id="main", max_concurrent_risk_dollars=2000.0,
                      equity=100000.0)
        rb.try_admit(500.0, 10000.0)
        s = rb.snapshot()
        assert s["portfolio_id"] == "main"
        assert s["equity"] == 100000.0
        assert s["max_risk_dollars"] == 2000.0
        assert s["max_notional"] == 200000.0
        assert s["open_risk"] == 500.0
        assert s["open_notional"] == 10000.0
        assert s["open_count"] == 1
        assert s["available_risk"] == 1500.0
        assert abs(s["utilization_pct"] - 25.0) < 1e-9

    def test_reset_session_clears_all(self):
        rb = RiskBook(portfolio_id="main")
        rb.try_admit(500.0, 10000.0)
        rb.try_admit(500.0, 10000.0)
        assert rb.open_count == 2
        rb.reset_session()
        assert rb.open_count == 0
        assert rb.open_risk == 0.0
        assert rb.open_notional == 0.0


class TestRiskBookRegistry:

    def test_register_and_get(self):
        reg = RiskBookRegistry()
        rb_main = reg.register("main", max_concurrent_risk_dollars=2000.0,
                               equity=100000.0)
        rb_val = reg.register("val", max_concurrent_risk_dollars=2000.0,
                              equity=50000.0)
        assert reg.get("main") is rb_main
        assert reg.get("val") is rb_val
        assert reg.get("gene") is None
        assert set(reg.all_ids()) == {"main", "val"}

    def test_independent_per_portfolio(self):
        """A risk admission in main does NOT affect val's available risk."""
        reg = RiskBookRegistry()
        reg.register("main", max_concurrent_risk_dollars=2000.0)
        reg.register("val", max_concurrent_risk_dollars=2000.0)
        reg.get("main").try_admit(2000.0, 50000.0)
        # Val should still have its full $2000 available
        assert reg.get("val").open_risk == 0.0
        t = reg.get("val").try_admit(2000.0, 50000.0)
        assert t is not None

    def test_snapshot_all(self):
        reg = RiskBookRegistry()
        reg.register("main", equity=100000.0)
        reg.register("val", equity=50000.0)
        reg.register("gene", equity=25000.0)
        snap = reg.snapshot_all()
        assert set(snap.keys()) == {"main", "val", "gene"}
        assert snap["main"]["equity"] == 100000.0
        assert snap["val"]["equity"] == 50000.0
        assert snap["gene"]["equity"] == 25000.0

    def test_reset_all_sessions(self):
        reg = RiskBookRegistry()
        reg.register("main")
        reg.register("val")
        reg.get("main").try_admit(500.0, 10000.0)
        reg.get("val").try_admit(500.0, 10000.0)
        reg.reset_all_sessions()
        assert reg.get("main").open_count == 0
        assert reg.get("val").open_count == 0


class TestReleaseByIdV781:
    """v7.81.0 -- release_by_id(ticket_id) supports the rollback path
    in orb.live_runtime.rollback_admit where the caller no longer holds
    the original ticket object (e.g. engine/scan.py keeps only the
    string ticket_id from CheckEntryResult)."""

    def test_release_by_id_frees_budget(self):
        rb = RiskBook(portfolio_id="main", max_concurrent_risk_dollars=2000.0,
                      equity=100000.0)
        ticket = rb.try_admit(risk_dollars=500.0, notional=10000.0)
        assert ticket is not None
        # Release by id only -- no ticket reference
        ok = rb.release_by_id(ticket.ticket_id)
        assert ok
        assert rb.open_risk == 0.0
        assert rb.open_notional == 0.0
        assert rb.open_count == 0

    def test_release_by_id_unknown_returns_false(self):
        rb = RiskBook(portfolio_id="main", max_concurrent_risk_dollars=2000.0,
                      equity=100000.0)
        assert rb.release_by_id("never-existed") is False

    def test_release_by_id_empty_string_returns_false(self):
        rb = RiskBook(portfolio_id="main", max_concurrent_risk_dollars=2000.0,
                      equity=100000.0)
        assert rb.release_by_id("") is False

    def test_release_by_id_idempotent(self):
        rb = RiskBook(portfolio_id="main", max_concurrent_risk_dollars=2000.0,
                      equity=100000.0)
        ticket = rb.try_admit(risk_dollars=500.0, notional=10000.0)
        assert rb.release_by_id(ticket.ticket_id) is True
        # Second call returns False (ticket no longer in dict).
        assert rb.release_by_id(ticket.ticket_id) is False
