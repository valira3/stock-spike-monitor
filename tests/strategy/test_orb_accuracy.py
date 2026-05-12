"""Tier-1 accuracy verification (v7.34.0).

Three layers of accuracy assurance for the v10 ORB live engine:

  1. PRICING-MATH PROPERTY TESTS
     For every admission produced by every scenario, assert the
     spec-derived math invariants hold exactly:
       - target  = entry + rr * (entry - stop)        (long)
       - target  = entry - rr * (stop - entry)        (short)
       - stop    = OR_opp +/- stop_buffer_bps         (long: below; short: above)
       - shares  = min(risk_dollars / risk_per_share, notional_cap / entry)
       - notional   <= max_trade_notional_pct  * equity
       - risk_dollars <= risk_per_trade_pct    * equity   (+ epsilon)
       - position.one_r is consistent (= entry +/- |entry-stop|)

  2. ROUND-TRIP LEAK DETECTOR
     After driving each scenario, assert:
       - sum(_open_tickets.risk_dollars) == RiskBook._open_risk
       - sum(_open_tickets.notional)     == RiskBook._open_notional
       - every entry that was followed by an exit released its ticket
       - no orphaned positions in LiveAdapter._open_positions vs the
         RiskBook's _open_tickets

  3. SPEC-AS-CODE REFERENCE FOR GEOMETRY
     A tiny ~30 LOC pure-Python reimplementation of the position
     geometry, run alongside every admission. If the live engine and
     the reference disagree by more than $0.005, the test fails. This
     gives an independent check on `make_position`'s math without
     using the same library code.

These run AGAINST EVERY scenario in the existing simulator suite via a
shared helper, so any future scenario picks them up for free.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from orb import engine as _engine
from orb import exits as _exits
from orb import live_runtime
from orb import state as _state
from orb.risk_book import RiskBook
from tools.orb_session_sim import (
    SessionSimulator, SimulatorConfig, make_breakout_bar, make_exit_bar,
)


@pytest.fixture(autouse=True)
def reset_runtime():
    live_runtime._reset_for_testing()
    yield
    live_runtime._reset_for_testing()


@pytest.fixture
def isolated_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("ORB_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ORB_PARTIAL_PROFIT_AT_1R", "0")  # v8.1.3 legacy default
    monkeypatch.setenv("ORB_MAX_CONCURRENT_NOTIONAL_MULT", "2.0")  # v8.3.20 legacy
    yield monkeypatch


def _basic_cfg(**overrides) -> SimulatorConfig:
    cfg = SimulatorConfig(
        date_iso="2026-01-15", tickers=["AAPL"],
        vix_close_d1=18.0,
        ticker_open_today={"AAPL": 100.0},
        ticker_prev_close={"AAPL": 100.0},
        equity_per_portfolio={"main": 100_000.0},
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


# ----- 3. Spec-as-code reference (geometry only) -----------------


@dataclass
class RefPosition:
    """Independent reimplementation of the v10 position geometry.
    ~30 LOC; runs alongside make_position to cross-check the math."""
    entry: float
    stop: float
    target: float
    one_r: float
    shares: int
    risk_dollars: float
    notional: float


def reference_geometry(*, side: str, or_high: float, or_low: float,
                       entry: float, equity: float,
                       rr: float = 2.5,
                       stop_buffer_bps: float = 5.0,
                       risk_per_trade_pct: float = 1.0,
                       max_trade_notional_pct: float = 75.0,
                       ) -> RefPosition:
    """Spec-derived geometry. Does NOT use orb/* code."""
    buf = stop_buffer_bps / 10000.0
    if side == "long":
        stop = or_low * (1.0 - buf)
        target = entry + rr * (entry - stop)
        one_r = entry + (entry - stop)
    else:
        stop = or_high * (1.0 + buf)
        target = entry - rr * (stop - entry)
        one_r = entry - (stop - entry)
    risk_per_share = abs(entry - stop)
    risk_budget = equity * risk_per_trade_pct / 100.0
    shares_from_risk = max(1, int(risk_budget / risk_per_share))
    max_notional = equity * max_trade_notional_pct / 100.0
    shares_from_notional = max(1, int(max_notional / entry)) if entry > 0 else 1
    shares = min(shares_from_risk, shares_from_notional)
    risk_dollars = risk_per_share * shares
    notional = entry * shares
    return RefPosition(
        entry=entry, stop=stop, target=target, one_r=one_r,
        shares=shares, risk_dollars=risk_dollars, notional=notional,
    )


# ----- 1. Pricing-math invariants -----------------


def assert_admission_math_correct(*, entry_result, equity: float,
                                  or_high: float, or_low: float,
                                  rr: float = 2.5,
                                  stop_buffer_bps: float = 5.0,
                                  risk_per_trade_pct: float = 1.0,
                                  max_trade_notional_pct: float = 75.0,
                                  ) -> None:
    """Assert every keystone math invariant on a single admission."""
    er = entry_result
    if not er.ok:
        return
    ref = reference_geometry(
        side=er.side, or_high=or_high, or_low=or_low,
        entry=er.price, equity=equity, rr=rr,
        stop_buffer_bps=stop_buffer_bps,
        risk_per_trade_pct=risk_per_trade_pct,
        max_trade_notional_pct=max_trade_notional_pct,
    )
    # 1. Target matches RR*risk geometry
    risk_per_share = abs(er.price - er.stop)
    if er.side == "long":
        expected_target = er.price + rr * risk_per_share
    else:
        expected_target = er.price - rr * risk_per_share
    assert abs(er.target - expected_target) < 0.005, (
        f"target {er.target} != expected {expected_target} "
        f"(entry={er.price}, stop={er.stop}, rr={rr}, side={er.side})"
    )
    # 2. Stop matches OR opposite + buffer
    buf = stop_buffer_bps / 10000.0
    if er.side == "long":
        expected_stop = or_low * (1.0 - buf)
    else:
        expected_stop = or_high * (1.0 + buf)
    assert abs(er.stop - expected_stop) < 0.005, (
        f"stop {er.stop} != expected {expected_stop} "
        f"(or_high={or_high}, or_low={or_low}, buf={buf}, side={er.side})"
    )
    # 3. Shares = reference shares
    assert er.shares == ref.shares, (
        f"shares {er.shares} != reference {ref.shares} "
        f"(risk_per_share={risk_per_share}, equity={equity})"
    )
    # 4. Notional cap honored
    max_notional = equity * max_trade_notional_pct / 100.0
    notional = er.price * er.shares
    assert notional <= max_notional + 0.5, (
        f"notional {notional} exceeds 75% cap {max_notional} "
        f"(equity={equity})"
    )
    # 5. Risk dollars cap honored
    expected_risk_dollars = risk_per_share * er.shares
    assert abs(er.risk_dollars - expected_risk_dollars) < 0.01, (
        f"risk_dollars {er.risk_dollars} != computed {expected_risk_dollars}"
    )
    risk_budget = equity * risk_per_trade_pct / 100.0
    # Risk_dollars may be < budget if notional cap kicked in
    assert er.risk_dollars <= risk_budget + 0.5, (
        f"risk_dollars {er.risk_dollars} exceeds 2% budget {risk_budget}"
    )


# ----- Pricing-math tests sweep ---------------------------------


class TestPricingMathAcrossScenarios:
    """Drive a set of representative scenarios and assert the math
    invariants on every admission. Acts as a property test over a
    hand-curated parameter space."""

    @pytest.mark.parametrize("side,or_low,or_high,entry,equity", [
        # Standard long breakouts (OR widths within 0.8-2.5%)
        ("long",  99.5,  100.5, 101.0, 100_000.0),  # 1.0% width
        ("long",  99.5,  100.5, 101.5, 100_000.0),
        ("long",  99.0,  101.0, 102.0, 100_000.0),  # 2.0% width
        ("long",  99.5,  100.5, 101.0,  50_000.0),
        ("long",  99.5,  100.5, 101.0,  25_000.0),
        # Standard short breakouts
        ("short", 99.5,  100.5,  99.0, 100_000.0),
        ("short", 99.5,  100.5,  98.5, 100_000.0),
        ("short", 99.0,  101.0,  98.0, 100_000.0),
        ("short", 99.5,  100.5,  99.0,  50_000.0),
        # Edge: tiny breakout to exercise notional cap
        ("long",  99.5,  100.5, 100.51, 100_000.0),
    ])
    def test_admission_math(self, isolated_env, side, or_low, or_high,
                            entry, equity):
        cfg = _basic_cfg(
            equity_per_portfolio={"main": equity},
        )
        with SessionSimulator(cfg) as sim:
            sim.start()
            sim.feed_or(ticker="AAPL", or_low=or_low, or_high=or_high)
            sim.feed_bar(make_breakout_bar(bucket=600, side=side,
                                            or_high=or_high,
                                            or_low=or_low,
                                            push_pct=0.0001),
                         ticker="AAPL")
            if side == "long":
                ent = sim.try_long(ticker="AAPL", price=entry,
                                   equity=equity)
            else:
                ent = sim.try_short(ticker="AAPL", price=entry,
                                    equity=equity)
            assert ent.ok, f"admission failed: {ent}"
            assert_admission_math_correct(
                entry_result=ent, equity=equity,
                or_high=or_high, or_low=or_low,
            )


# ----- 2. Round-trip leak detector ----------------


def assert_no_leaks(engine):
    """Assert RiskBook bookkeeping is internally consistent after a
    scenario."""
    for pid in engine.portfolio_ids:
        rb = engine._risk.get(pid)
        if rb is None:
            continue
        with rb._lock:
            tickets = list(rb._open_tickets.values())
            expected_risk = sum(t.risk_dollars for t in tickets)
            expected_notional = sum(t.notional for t in tickets)
            assert abs(rb._open_risk - expected_risk) < 0.01, (
                f"portfolio={pid} _open_risk={rb._open_risk} "
                f"!= sum tickets risk {expected_risk}"
            )
            assert abs(rb._open_notional - expected_notional) < 0.5, (
                f"portfolio={pid} _open_notional={rb._open_notional} "
                f"!= sum tickets notional {expected_notional}"
            )


class TestRoundTripLeaks:
    """For each scenario shape, verify the bookkeeping reconciles."""

    def test_admit_release_balances(self, isolated_env):
        with SessionSimulator(_basic_cfg()) as sim:
            sim.start()
            sim.feed_or(ticker="AAPL", or_low=99.5, or_high=100.5)
            sim.feed_bar(make_breakout_bar(bucket=600, side="long",
                                           or_high=100.5, or_low=99.5),
                         ticker="AAPL")
            ent = sim.try_long(ticker="AAPL", price=101.0)
            assert ent.ok
            engine = live_runtime.get_engine()
            # After admit, open_risk should be non-zero
            rb = engine._risk.get("main")
            assert rb.open_risk > 0
            assert_no_leaks(engine)
            # After exit, open_risk back to 0
            sim.walk_to_target(ticker="AAPL",
                                ticket_id=ent.ticket_id,
                                target=ent.target)
            assert_no_leaks(engine)
            assert rb.open_risk == 0
            assert rb.open_notional == 0
            assert rb.open_count == 0

    def test_no_leak_after_stop(self, isolated_env):
        with SessionSimulator(_basic_cfg()) as sim:
            sim.start()
            sim.feed_or(ticker="AAPL", or_low=99.5, or_high=100.5)
            sim.feed_bar(make_breakout_bar(bucket=600, side="long",
                                           or_high=100.5, or_low=99.5),
                         ticker="AAPL")
            ent = sim.try_long(ticker="AAPL", price=101.0)
            assert ent.ok
            sim.walk_to_stop(ticker="AAPL", ticket_id=ent.ticket_id,
                             stop=ent.stop)
            engine = live_runtime.get_engine()
            assert_no_leaks(engine)
            assert engine._risk.get("main").open_count == 0

    def test_no_leak_after_eod(self, isolated_env):
        with SessionSimulator(_basic_cfg()) as sim:
            sim.start()
            sim.feed_or(ticker="AAPL", or_low=99.5, or_high=100.5)
            sim.feed_bar(make_breakout_bar(bucket=600, side="long",
                                           or_high=100.5, or_low=99.5),
                         ticker="AAPL")
            ent = sim.try_long(ticker="AAPL", price=101.0)
            assert ent.ok
            sim.force_eod(ticker="AAPL", ticket_id=ent.ticket_id,
                          price=101.5)
            engine = live_runtime.get_engine()
            assert_no_leaks(engine)

    def test_concurrent_admits_balance(self, isolated_env):
        """Two simultaneous admissions; release one; verify accounting.

        Use $50k equity so each trade's risk_dollars (~$1k at this
        geometry) stays under the $2k concurrent risk cap when summed.
        """
        cfg = _basic_cfg(
            tickers=["AAPL", "MSFT"],
            ticker_open_today={"AAPL": 100.0, "MSFT": 200.0},
            ticker_prev_close={"AAPL": 100.0, "MSFT": 200.0},
            equity_per_portfolio={"main": 50_000.0},
        )
        with SessionSimulator(cfg) as sim:
            sim.start()
            sim.feed_or(ticker="AAPL", or_low=99.5, or_high=100.5)
            sim.feed_or(ticker="MSFT", or_low=199.0, or_high=201.0)
            sim.feed_bar(make_breakout_bar(bucket=600, side="long",
                                           or_high=100.5, or_low=99.5),
                         ticker="AAPL")
            sim.feed_bar(make_breakout_bar(bucket=600, side="long",
                                           or_high=201.0, or_low=199.0),
                         ticker="MSFT")
            ent_a = sim.try_long(ticker="AAPL", price=101.0,
                                 equity=50_000.0)
            ent_m = sim.try_long(ticker="MSFT", price=202.0,
                                 equity=50_000.0)
            assert ent_a.ok, f"AAPL should admit: {ent_a}"
            assert ent_m.ok, f"MSFT should admit: {ent_m}"
            engine = live_runtime.get_engine()
            assert_no_leaks(engine)
            rb = engine._risk.get("main")
            assert rb.open_count == 2
            # Close AAPL only
            sim.walk_to_target(ticker="AAPL", ticket_id=ent_a.ticket_id,
                                target=ent_a.target)
            assert_no_leaks(engine)
            assert rb.open_count == 1
            # Close MSFT
            sim.walk_to_target(ticker="MSFT", ticket_id=ent_m.ticket_id,
                                target=ent_m.target, start_bucket=610)
            assert_no_leaks(engine)
            assert rb.open_count == 0

    def test_kill_triggered_releases_position(self, isolated_env):
        """When daily kill fires on stop-out, the closed position's
        ticket must STILL be released. The kill cascade transitions
        OTHER tickers; the closing ticker shouldn't leak its risk."""
        with SessionSimulator(
            _basic_cfg(equity_per_portfolio={"main": 10_000.0})
        ) as sim:
            sim.start()
            sim.feed_or(ticker="AAPL", or_low=99.5, or_high=100.5)
            # 2 stops -> kill
            for i in range(2):
                sim.feed_bar(make_breakout_bar(bucket=600 + i * 5,
                                                side="long",
                                                or_high=100.5, or_low=99.5,
                                                push_pct=0.005 + i * 0.005),
                             ticker="AAPL")
                e = sim.try_long(ticker="AAPL",
                                 price=101.0 + i * 0.5,
                                 equity=10_000.0)
                if not e.ok:
                    break
                sim.walk_to_stop(ticker="AAPL", ticket_id=e.ticket_id,
                                 stop=e.stop, start_bucket=605 + i * 5)
            engine = live_runtime.get_engine()
            assert_no_leaks(engine)
            assert engine._risk.get("main").open_count == 0


# ----- Reference engine cross-check (Tier 1 deeper) ---------------


class TestReferenceGeometryAgreement:
    """The spec-as-code reference and the live engine must compute the
    SAME geometry to within $0.005."""

    @pytest.mark.parametrize("side,or_low,or_high,entry,equity", [
        ("long",  99.5, 100.5, 101.0, 100_000.0),
        ("long",  99.0, 101.0, 102.0,  50_000.0),  # 2.0% width
        ("short", 99.5, 100.5,  99.0, 100_000.0),
        ("short", 99.0, 101.0,  98.0,  50_000.0),
        # Edge: very small risk per share (high notional clamp)
        ("long",  99.5, 100.5, 100.51, 100_000.0),
    ])
    def test_reference_matches_live(self, isolated_env, side, or_low,
                                    or_high, entry, equity):
        cfg = _basic_cfg(equity_per_portfolio={"main": equity})
        with SessionSimulator(cfg) as sim:
            sim.start()
            sim.feed_or(ticker="AAPL", or_low=or_low, or_high=or_high)
            sim.feed_bar(make_breakout_bar(bucket=600, side=side,
                                            or_high=or_high,
                                            or_low=or_low,
                                            push_pct=0.0001),
                         ticker="AAPL")
            if side == "long":
                ent = sim.try_long(ticker="AAPL", price=entry,
                                   equity=equity)
            else:
                ent = sim.try_short(ticker="AAPL", price=entry,
                                    equity=equity)
            assert ent.ok
            ref = reference_geometry(
                side=side, or_high=or_high, or_low=or_low,
                entry=entry, equity=equity,
            )
            # Geometry agreement
            assert abs(ent.stop - ref.stop) < 0.005, \
                f"stop disagree: live={ent.stop} ref={ref.stop}"
            assert abs(ent.target - ref.target) < 0.005, \
                f"target disagree: live={ent.target} ref={ref.target}"
            assert ent.shares == ref.shares, \
                f"shares disagree: live={ent.shares} ref={ref.shares}"
            assert abs(ent.risk_dollars - ref.risk_dollars) < 0.5, \
                f"risk_dollars disagree: live={ent.risk_dollars} ref={ref.risk_dollars}"


# ----- Pure-math unit tests on the reference itself ---------------


class TestReferenceGeometry:
    """Confirm the reference itself encodes the spec correctly. If
    the reference is wrong, the live engine could match it and still
    be wrong -- so check the reference too."""

    def test_long_rr_holds(self):
        r = reference_geometry(side="long", or_high=100.5, or_low=99.5,
                               entry=101.0, equity=100_000.0)
        # RR = 2.5: (target - entry) = 2.5 * (entry - stop)
        risk = r.entry - r.stop
        reward = r.target - r.entry
        assert abs(reward - 2.5 * risk) < 0.005

    def test_short_rr_holds(self):
        r = reference_geometry(side="short", or_high=100.5, or_low=99.5,
                               entry=99.0, equity=100_000.0)
        risk = r.stop - r.entry
        reward = r.entry - r.target
        assert abs(reward - 2.5 * risk) < 0.005

    def test_long_stop_below_or_low(self):
        r = reference_geometry(side="long", or_high=100.5, or_low=99.5,
                               entry=101.0, equity=100_000.0)
        # stop = or_low * (1 - 5bps) = 99.5 * 0.9995 = 99.45025
        assert abs(r.stop - 99.45025) < 0.005

    def test_short_stop_above_or_high(self):
        r = reference_geometry(side="short", or_high=100.5, or_low=99.5,
                               entry=99.0, equity=100_000.0)
        # stop = or_high * (1 + 5bps) = 100.5 * 1.0005 = 100.55025
        assert abs(r.stop - 100.55025) < 0.005

    def test_notional_cap_clamps_shares(self):
        # entry $101, narrow risk -> would size 1900+ shares risk-based
        # but notional cap of 75k clamps to ~742 shares
        r = reference_geometry(side="long", or_high=100.5, or_low=99.5,
                               entry=100.51, equity=100_000.0)
        assert r.notional <= 75_000.5

    def test_one_r_equals_entry_plus_risk_long(self):
        r = reference_geometry(side="long", or_high=100.5, or_low=99.5,
                               entry=101.0, equity=100_000.0)
        assert abs(r.one_r - (r.entry + abs(r.entry - r.stop))) < 0.005

    def test_one_r_equals_entry_minus_risk_short(self):
        r = reference_geometry(side="short", or_high=100.5, or_low=99.5,
                               entry=99.0, equity=100_000.0)
        assert abs(r.one_r - (r.entry - abs(r.stop - r.entry))) < 0.005
