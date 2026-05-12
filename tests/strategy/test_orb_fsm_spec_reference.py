"""v7.39.0 -- spec-as-code FSM reference engine.

The accuracy framework so far covers:
  - PRICING math (PR26: pricing-math + leak + geometry reference)
  - BACKTEST PARITY (PR27)
  - RANDOM properties (PR28)
  - GOLDEN ledgers (PR29)
  - BOUNDARY values (PR30)

What's still missing: a SECOND implementation of the FSM transition
logic. PR26's geometry reference covers `make_position`. This PR
extends to the **state-transition rules** that decide WHICH (ticker,
portfolio) tuples admit on a given day.

`SpecFSM` reimplements the keystone state machine in ~80 LOC of pure
Python -- WITHOUT touching any `orb.state` / `orb.day_gates` /
`orb.engine` code. It encodes the spec text directly:

  PHASE_WARMUP    -- before OR locks
  PHASE_OR_LOCKED -- OR locked, deciding admission gates
  PHASE_ARMED     -- gates passed, eligible for breakout
  PHASE_IN_POS    -- breakout fired, position open
  PHASE_CLOSED    -- position closed, eligible for re-entry up to max_trades
  PHASE_BLOCKED_* -- one of: VIX / EARNINGS / GAP / BLOCKLIST / RANGE / OR_INSUFFICIENT / DAILY_KILL

Run alongside the live engine on a curated set of state-transition
scenarios. If both implementations agree on the resulting phase, the
state machine is correct. If they disagree, the spec, the live
engine, or the reference is wrong -- the disagreement points at the
exact transition that differs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import pytest

from orb import engine as _engine
from orb import live_runtime
from orb import state as _state
from tools.orb_session_sim import (
    SessionSimulator, SimulatorConfig, make_breakout_bar,
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
    yield monkeypatch


# ----- Spec-as-code FSM reference --------------------------------


@dataclass
class SpecFSMConfig:
    or_minutes: int = 30
    range_min_pct: float = 0.008
    range_max_pct: float = 0.025
    skip_vix_above: float = 22.0
    skip_gap_above_pct: float = 1.5
    max_trades_per_day: int = 5
    ticker_side_blocklist: dict = field(default_factory=dict)


def spec_fsm_phase(*, cfg: SpecFSMConfig,
                   vix_d1: Optional[float],
                   prev_close: float,
                   today_open: float,
                   or_low: Optional[float],
                   or_high: Optional[float],
                   bars_seen_in_or: int,
                   ticker: str,
                   side: str,                # "LONG" / "SHORT" -- for blocklist
                   in_earnings_window: bool,
                   trades_today: int,
                   in_position: bool,
                   daily_kill_triggered: bool,
                   ) -> str:
    """Spec-derived FSM phase resolver. Returns one of:

      "WARMUP" (before OR lock)
      "OR_LOCKED" (transient)
      "ARMED" (eligible to admit on next breakout)
      "IN_POS" (position open)
      "CLOSED" (closed, eligible to re-enter if trades_today < max)
      "BLOCKED_VIX" / "BLOCKED_EARNINGS" / "BLOCKED_GAP" /
      "BLOCKED_BLOCKLIST" / "BLOCKED_RANGE" /
      "BLOCKED_OR_INSUFFICIENT" / "BLOCKED_DAILY_KILL"

    Order of evaluation follows the live engine to make per-step
    comparisons easier:
      1. Daily kill (intraday)
      2. VIX kill (day-level)
      3. Earnings (per-ticker)
      4. Gap (per-ticker)
      5. Blocklist (per-ticker, per-side)
      6. OR-state-dependent rules:
         a. WARMUP if OR not locked yet
         b. BLOCKED_OR_INSUFFICIENT if bars_seen < or_minutes/2
         c. BLOCKED_RANGE if width pct outside [min, max]
         d. ARMED otherwise (until breakout)

    In-position / closed override breakout-eligibility:
      - in_position -> "IN_POS"
      - trades_today >= max -> "CLOSED" (cap-locked; no re-entry)
    """
    # 1. Daily kill (intraday)
    if daily_kill_triggered:
        return "BLOCKED_DAILY_KILL"
    # 2. VIX kill
    if (vix_d1 is None or vix_d1 > cfg.skip_vix_above):
        return "BLOCKED_VIX"
    # 3. Earnings
    if in_earnings_window:
        return "BLOCKED_EARNINGS"
    # 4. Gap
    if prev_close > 0:
        gap_pct = abs(today_open - prev_close) / prev_close * 100.0
        if gap_pct > cfg.skip_gap_above_pct:
            return "BLOCKED_GAP"
    # 5. Blocklist (per-side; case-insensitive)
    blocked_sides = {s.upper() for s in
                     cfg.ticker_side_blocklist.get(ticker, [])}
    if side.upper() in blocked_sides:
        return "BLOCKED_BLOCKLIST"
    # 6. In-position state (regardless of OR state)
    if in_position:
        return "IN_POS"
    # 7. Max trades cap -- once exhausted, no further admits
    if trades_today >= cfg.max_trades_per_day:
        return "CLOSED"
    # 8. OR-window state
    if or_low is None or or_high is None:
        return "WARMUP"
    # OR locked. Width check.
    mid = (or_high + or_low) / 2.0
    width_pct = (or_high - or_low) / mid if mid > 0 else 0.0
    if bars_seen_in_or < cfg.or_minutes // 2:
        return "BLOCKED_OR_INSUFFICIENT"
    if not (cfg.range_min_pct <= width_pct <= cfg.range_max_pct):
        return "BLOCKED_RANGE"
    return "ARMED"


# ----- Phase comparison helpers ---------------------------------


# Translation from live engine's phase strings to spec FSM strings.
LIVE_TO_SPEC = {
    _state.PHASE_WARMUP: "WARMUP",
    _state.PHASE_OR_LOCKED: "OR_LOCKED",
    _state.PHASE_ARMED: "ARMED",
    _state.PHASE_IN_POS: "IN_POS",
    _state.PHASE_CLOSED: "CLOSED",
    _state.PHASE_BLOCKED_VIX: "BLOCKED_VIX",
    _state.PHASE_BLOCKED_EARNINGS: "BLOCKED_EARNINGS",
    _state.PHASE_BLOCKED_GAP: "BLOCKED_GAP",
    _state.PHASE_BLOCKED_BLOCKLIST: "BLOCKED_BLOCKLIST",
    _state.PHASE_BLOCKED_RANGE: "BLOCKED_RANGE",
    _state.PHASE_BLOCKED_OR_INSUFFICIENT: "BLOCKED_OR_INSUFFICIENT",
    _state.PHASE_BLOCKED_DAILY_KILL: "BLOCKED_DAILY_KILL",
}


def _basic_cfg(**overrides) -> SimulatorConfig:
    cfg = SimulatorConfig(
        date_iso="2026-01-15", tickers=["AAPL"], vix_close_d1=18.0,
        ticker_open_today={"AAPL": 100.0},
        ticker_prev_close={"AAPL": 100.0},
        equity_per_portfolio={"main": 100_000.0},
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


# ----- Spec-as-code unit tests (verify reference correctness) ----


class TestSpecFsmReference:
    """First, confirm the reference itself encodes the spec correctly.
    If the reference is wrong, the live engine could match it and
    still be wrong."""

    def _cfg(self) -> SpecFSMConfig:
        return SpecFSMConfig(
            ticker_side_blocklist={"META": ["LONG"]},
        )

    def test_warmup_before_or_lock(self):
        p = spec_fsm_phase(
            cfg=self._cfg(),
            vix_d1=18.0, prev_close=100.0, today_open=100.0,
            or_low=None, or_high=None, bars_seen_in_or=0,
            ticker="AAPL", side="LONG", in_earnings_window=False,
            trades_today=0, in_position=False,
            daily_kill_triggered=False,
        )
        assert p == "WARMUP"

    def test_armed_after_clean_or_lock(self):
        p = spec_fsm_phase(
            cfg=self._cfg(),
            vix_d1=18.0, prev_close=100.0, today_open=100.0,
            or_low=99.5, or_high=100.5, bars_seen_in_or=30,
            ticker="AAPL", side="LONG", in_earnings_window=False,
            trades_today=0, in_position=False,
            daily_kill_triggered=False,
        )
        assert p == "ARMED"

    def test_vix_kill_overrides_or(self):
        # VIX kill fires regardless of OR state
        p = spec_fsm_phase(
            cfg=self._cfg(),
            vix_d1=25.0, prev_close=100.0, today_open=100.0,
            or_low=99.5, or_high=100.5, bars_seen_in_or=30,
            ticker="AAPL", side="LONG", in_earnings_window=False,
            trades_today=0, in_position=False,
            daily_kill_triggered=False,
        )
        assert p == "BLOCKED_VIX"

    def test_daily_kill_overrides_all(self):
        # Daily kill is intraday + highest priority
        p = spec_fsm_phase(
            cfg=self._cfg(),
            vix_d1=18.0, prev_close=100.0, today_open=100.0,
            or_low=99.5, or_high=100.5, bars_seen_in_or=30,
            ticker="AAPL", side="LONG", in_earnings_window=False,
            trades_today=2, in_position=False,
            daily_kill_triggered=True,
        )
        assert p == "BLOCKED_DAILY_KILL"

    def test_earnings_blocks_per_ticker(self):
        p = spec_fsm_phase(
            cfg=self._cfg(),
            vix_d1=18.0, prev_close=100.0, today_open=100.0,
            or_low=99.5, or_high=100.5, bars_seen_in_or=30,
            ticker="AAPL", side="LONG",
            in_earnings_window=True,
            trades_today=0, in_position=False,
            daily_kill_triggered=False,
        )
        assert p == "BLOCKED_EARNINGS"

    def test_gap_blocks_per_ticker(self):
        # 2% gap -> > 1.5% threshold
        p = spec_fsm_phase(
            cfg=self._cfg(),
            vix_d1=18.0, prev_close=100.0, today_open=102.0,
            or_low=101.5, or_high=102.5, bars_seen_in_or=30,
            ticker="AAPL", side="LONG", in_earnings_window=False,
            trades_today=0, in_position=False,
            daily_kill_triggered=False,
        )
        assert p == "BLOCKED_GAP"

    def test_blocklist_per_side(self):
        # META LONG is in blocklist
        p_long = spec_fsm_phase(
            cfg=self._cfg(),
            vix_d1=18.0, prev_close=200.0, today_open=200.0,
            or_low=199.0, or_high=201.0, bars_seen_in_or=30,
            ticker="META", side="LONG", in_earnings_window=False,
            trades_today=0, in_position=False,
            daily_kill_triggered=False,
        )
        assert p_long == "BLOCKED_BLOCKLIST"
        # META SHORT is not blocked
        p_short = spec_fsm_phase(
            cfg=self._cfg(),
            vix_d1=18.0, prev_close=200.0, today_open=200.0,
            or_low=199.0, or_high=201.0, bars_seen_in_or=30,
            ticker="META", side="SHORT", in_earnings_window=False,
            trades_today=0, in_position=False,
            daily_kill_triggered=False,
        )
        assert p_short == "ARMED"

    def test_range_too_narrow_blocks(self):
        # 0.4% width -> below 0.8% min
        p = spec_fsm_phase(
            cfg=self._cfg(),
            vix_d1=18.0, prev_close=100.0, today_open=100.0,
            or_low=99.8, or_high=100.2, bars_seen_in_or=30,
            ticker="AAPL", side="LONG", in_earnings_window=False,
            trades_today=0, in_position=False,
            daily_kill_triggered=False,
        )
        assert p == "BLOCKED_RANGE"

    def test_range_too_wide_blocks(self):
        # 3% width -> above 2.5% max
        p = spec_fsm_phase(
            cfg=self._cfg(),
            vix_d1=18.0, prev_close=100.0, today_open=100.0,
            or_low=98.5, or_high=101.5, bars_seen_in_or=30,
            ticker="AAPL", side="LONG", in_earnings_window=False,
            trades_today=0, in_position=False,
            daily_kill_triggered=False,
        )
        assert p == "BLOCKED_RANGE"

    def test_in_position_overrides_eligibility(self):
        p = spec_fsm_phase(
            cfg=self._cfg(),
            vix_d1=18.0, prev_close=100.0, today_open=100.0,
            or_low=99.5, or_high=100.5, bars_seen_in_or=30,
            ticker="AAPL", side="LONG", in_earnings_window=False,
            trades_today=1, in_position=True,
            daily_kill_triggered=False,
        )
        assert p == "IN_POS"

    def test_max_trades_cap_closes(self):
        p = spec_fsm_phase(
            cfg=self._cfg(),
            vix_d1=18.0, prev_close=100.0, today_open=100.0,
            or_low=99.5, or_high=100.5, bars_seen_in_or=30,
            ticker="AAPL", side="LONG", in_earnings_window=False,
            trades_today=5, in_position=False,
            daily_kill_triggered=False,
        )
        assert p == "CLOSED"

    def test_or_insufficient_bars_blocks(self):
        # Only 5 bars seen of 30; min is 15
        p = spec_fsm_phase(
            cfg=self._cfg(),
            vix_d1=18.0, prev_close=100.0, today_open=100.0,
            or_low=99.5, or_high=100.5, bars_seen_in_or=5,
            ticker="AAPL", side="LONG", in_earnings_window=False,
            trades_today=0, in_position=False,
            daily_kill_triggered=False,
        )
        assert p == "BLOCKED_OR_INSUFFICIENT"


# ----- Live <-> spec agreement ----------------------------------


def _live_phase(sim_engine, pid: str, ticker: str) -> str:
    """Map live engine's per-(portfolio, ticker) phase to spec strings."""
    ds = sim_engine._state.get_day_state(pid, ticker)
    return LIVE_TO_SPEC.get(ds.phase, "UNKNOWN")


class TestLiveSpecAgreement:
    """Drive a set of curated scenarios and assert the live engine
    transitions to the SAME phase the spec reference says it should."""

    def test_clean_or_admits_armed_in_both(self, isolated_env):
        cfg_spec = SpecFSMConfig()
        spec = spec_fsm_phase(
            cfg=cfg_spec,
            vix_d1=18.0, prev_close=100.0, today_open=100.0,
            or_low=99.5, or_high=100.5, bars_seen_in_or=30,
            ticker="AAPL", side="LONG", in_earnings_window=False,
            trades_today=0, in_position=False,
            daily_kill_triggered=False,
        )
        assert spec == "ARMED"
        with SessionSimulator(_basic_cfg()) as sim:
            sim.start()
            sim.feed_or(ticker="AAPL", or_low=99.5, or_high=100.5)
            live = _live_phase(live_runtime.get_engine(), "main", "AAPL")
            assert live == spec, f"live={live} spec={spec}"

    def test_vix_kill_agrees(self, isolated_env):
        cfg_spec = SpecFSMConfig()
        spec = spec_fsm_phase(
            cfg=cfg_spec,
            vix_d1=25.0, prev_close=100.0, today_open=100.0,
            or_low=None, or_high=None, bars_seen_in_or=0,
            ticker="AAPL", side="LONG", in_earnings_window=False,
            trades_today=0, in_position=False,
            daily_kill_triggered=False,
        )
        assert spec == "BLOCKED_VIX"
        with SessionSimulator(_basic_cfg(vix_close_d1=25.0)) as sim:
            sim.start()
            live = _live_phase(live_runtime.get_engine(), "main", "AAPL")
            assert live == spec, f"live={live} spec={spec}"

    def test_gap_block_agrees(self, isolated_env):
        cfg_spec = SpecFSMConfig()
        spec = spec_fsm_phase(
            cfg=cfg_spec,
            vix_d1=18.0, prev_close=100.0, today_open=102.0,
            or_low=None, or_high=None, bars_seen_in_or=0,
            ticker="AAPL", side="LONG", in_earnings_window=False,
            trades_today=0, in_position=False,
            daily_kill_triggered=False,
        )
        assert spec == "BLOCKED_GAP"
        cfg_sim = _basic_cfg(
            ticker_open_today={"AAPL": 102.0},
            ticker_prev_close={"AAPL": 100.0},
        )
        with SessionSimulator(cfg_sim) as sim:
            sim.start()
            live = _live_phase(live_runtime.get_engine(), "main", "AAPL")
            assert live == spec, f"live={live} spec={spec}"

    def test_range_too_narrow_agrees(self, isolated_env):
        cfg_spec = SpecFSMConfig()
        spec = spec_fsm_phase(
            cfg=cfg_spec,
            vix_d1=18.0, prev_close=100.0, today_open=100.0,
            or_low=99.8, or_high=100.2, bars_seen_in_or=30,
            ticker="AAPL", side="LONG", in_earnings_window=False,
            trades_today=0, in_position=False,
            daily_kill_triggered=False,
        )
        assert spec == "BLOCKED_RANGE"
        with SessionSimulator(_basic_cfg()) as sim:
            sim.start()
            sim.feed_or(ticker="AAPL", or_low=99.8, or_high=100.2)
            live = _live_phase(live_runtime.get_engine(), "main", "AAPL")
            assert live == spec, f"live={live} spec={spec}"

    def test_range_too_wide_agrees(self, isolated_env):
        cfg_spec = SpecFSMConfig()
        spec = spec_fsm_phase(
            cfg=cfg_spec,
            vix_d1=18.0, prev_close=100.0, today_open=100.0,
            or_low=98.5, or_high=101.5, bars_seen_in_or=30,
            ticker="AAPL", side="LONG", in_earnings_window=False,
            trades_today=0, in_position=False,
            daily_kill_triggered=False,
        )
        assert spec == "BLOCKED_RANGE"
        with SessionSimulator(_basic_cfg()) as sim:
            sim.start()
            sim.feed_or(ticker="AAPL", or_low=98.5, or_high=101.5)
            live = _live_phase(live_runtime.get_engine(), "main", "AAPL")
            assert live == spec, f"live={live} spec={spec}"

    def test_blocklist_agrees(self, isolated_env):
        cfg_spec = SpecFSMConfig(
            ticker_side_blocklist={"META": ["long"]},
        )
        spec = spec_fsm_phase(
            cfg=cfg_spec,
            vix_d1=18.0, prev_close=200.0, today_open=200.0,
            or_low=199.0, or_high=201.0, bars_seen_in_or=30,
            ticker="META", side="long", in_earnings_window=False,
            trades_today=0, in_position=False,
            daily_kill_triggered=False,
        )
        assert spec == "BLOCKED_BLOCKLIST"
        cfg_sim = SimulatorConfig(
            date_iso="2026-01-15", tickers=["META"],
            vix_close_d1=18.0,
            ticker_open_today={"META": 200.0},
            ticker_prev_close={"META": 200.0},
            equity_per_portfolio={"main": 100_000.0},
            env_overrides={
                "ORB_TICKER_SIDE_BLOCKLIST": '{"META":["long"]}',
            },
        )
        with SessionSimulator(cfg_sim) as sim:
            sim.start()
            sim.feed_or(ticker="META", or_low=199.0, or_high=201.0)
            live = _live_phase(live_runtime.get_engine(), "main", "META")
            assert live == spec, f"live={live} spec={spec}"

    def test_in_pos_agrees(self, isolated_env):
        cfg_spec = SpecFSMConfig()
        spec = spec_fsm_phase(
            cfg=cfg_spec,
            vix_d1=18.0, prev_close=100.0, today_open=100.0,
            or_low=99.5, or_high=100.5, bars_seen_in_or=30,
            ticker="AAPL", side="long", in_earnings_window=False,
            trades_today=0, in_position=True,
            daily_kill_triggered=False,
        )
        assert spec == "IN_POS"
        with SessionSimulator(_basic_cfg()) as sim:
            sim.start()
            sim.feed_or(ticker="AAPL", or_low=99.5, or_high=100.5)
            sim.feed_bar(
                make_breakout_bar(bucket=600, side="long",
                                  or_high=100.5, or_low=99.5),
                ticker="AAPL",
            )
            ent = sim.try_long(ticker="AAPL", price=101.0)
            assert ent.ok
            live = _live_phase(live_runtime.get_engine(), "main", "AAPL")
            assert live == spec, f"live={live} spec={spec}"
