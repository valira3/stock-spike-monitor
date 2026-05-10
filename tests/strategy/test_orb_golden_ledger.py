"""v7.37.0 -- golden ledger snapshot regression.

Each named scenario produces a deterministic ledger when driven
through `orb.live_runtime`. We check in those ledgers as JSON
"golden" files and diff against them on every test run. Any v10
code change that alters the admit/exit timing/price/size/reason
causes the test to fail with a clear diff.

Workflow on intentional changes:

  1. Run `pytest tests/strategy/test_orb_golden_ledger.py
     --regen-goldens` to regenerate the JSON files.
  2. Commit the new goldens alongside the code change. The diff
     in the PR makes the strategy-level intent visible.

Workflow on regressions:

  1. Test fails with a JSON diff (e.g. shares changed from 742 to
     721, exit_price drifted).
  2. Author reads the diff, decides if intentional, either
     regenerates or fixes.

Goldens live in `tests/strategy/goldens/<scenario_name>.golden.json`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orb import live_runtime
from tools.orb_session_sim import (
    SessionSimulator, SimulatorConfig, make_breakout_bar, make_exit_bar,
)


GOLDEN_DIR = Path(__file__).parent / "goldens"


# Regeneration is triggered by an env var (conftest pytest_addoption
# would work too but adding a conftest just for this test seems
# heavier than needed). Usage:
#
#   REGEN_GOLDENS=1 pytest tests/strategy/test_orb_golden_ledger.py
#
# After regenerating, commit the updated *.golden.json files.
def _regen_goldens_flag() -> bool:
    return os.environ.get("REGEN_GOLDENS", "0") == "1"


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
    yield monkeypatch


# ----- Ledger normalization --------------------------------------


def _round(v):
    if isinstance(v, float):
        return round(v, 4)
    return v


def _normalize_history(history) -> list[dict]:
    """Project scenario history -> JSON-stable ledger list."""
    out = []
    for step in history:
        if step.kind == "session_start":
            out.append({
                "kind": "session_start",
                "date": step.detail.get("date", ""),
            })
        elif step.kind == "entry":
            out.append({
                "kind": "admit" if step.detail.get("ok") else "reject",
                "ticker": step.detail.get("ticker"),
                "pid": step.detail.get("pid", "main"),
                "side": step.detail.get("side"),
                "shares": step.detail.get("shares", 0),
                "stop": _round(step.detail.get("stop", 0.0)),
                "target": _round(step.detail.get("target", 0.0)),
                "reason_no": step.detail.get("reason_no", ""),
            })
        elif step.kind == "exit":
            out.append({
                "kind": "exit",
                "ticker": step.detail.get("ticker"),
                "reason": step.detail.get("reason", ""),
                "price": _round(step.detail.get("price", 0.0)),
                "bucket": step.detail.get("bucket", 0),
            })
        # feed_bar steps are NOT included; they're high-volume + low-info
    return out


# ----- Scenario runners --------------------------------------------


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


def scenario_golden_long_target():
    with SessionSimulator(_basic_cfg()) as sim:
        sim.start()
        sim.feed_or(ticker="AAPL", or_low=99.5, or_high=100.5)
        sim.feed_bar(make_breakout_bar(bucket=600, side="long",
                                       or_high=100.5, or_low=99.5),
                     ticker="AAPL")
        ent = sim.try_long(ticker="AAPL", price=101.0)
        sim.walk_to_target(ticker="AAPL", ticket_id=ent.ticket_id,
                            target=ent.target)
        return _normalize_history(sim.history())


def scenario_short_target():
    with SessionSimulator(_basic_cfg()) as sim:
        sim.start()
        sim.feed_or(ticker="AAPL", or_low=99.5, or_high=100.5)
        sim.feed_bar(make_breakout_bar(bucket=600, side="short",
                                       or_high=100.5, or_low=99.5),
                     ticker="AAPL")
        ent = sim.try_short(ticker="AAPL", price=99.0)
        sim.walk_to_target(ticker="AAPL", ticket_id=ent.ticket_id,
                            target=ent.target)
        return _normalize_history(sim.history())


def scenario_long_stop():
    with SessionSimulator(_basic_cfg()) as sim:
        sim.start()
        sim.feed_or(ticker="AAPL", or_low=99.5, or_high=100.5)
        sim.feed_bar(make_breakout_bar(bucket=600, side="long",
                                       or_high=100.5, or_low=99.5),
                     ticker="AAPL")
        ent = sim.try_long(ticker="AAPL", price=101.0)
        sim.walk_to_stop(ticker="AAPL", ticket_id=ent.ticket_id,
                          stop=ent.stop)
        return _normalize_history(sim.history())


def scenario_long_eod():
    with SessionSimulator(_basic_cfg()) as sim:
        sim.start()
        sim.feed_or(ticker="AAPL", or_low=99.5, or_high=100.5)
        sim.feed_bar(make_breakout_bar(bucket=600, side="long",
                                       or_high=100.5, or_low=99.5),
                     ticker="AAPL")
        ent = sim.try_long(ticker="AAPL", price=101.0)
        sim.force_eod(ticker="AAPL", ticket_id=ent.ticket_id,
                       price=101.5)
        return _normalize_history(sim.history())


def scenario_vix_kill_blocks_entry():
    cfg = _basic_cfg(vix_close_d1=25.0)
    with SessionSimulator(cfg) as sim:
        sim.start()
        sim.feed_or(ticker="AAPL", or_low=99.5, or_high=100.5)
        sim.feed_bar(make_breakout_bar(bucket=600, side="long",
                                       or_high=100.5, or_low=99.5),
                     ticker="AAPL")
        sim.try_long(ticker="AAPL", price=101.0)
        return _normalize_history(sim.history())


def scenario_gap_skip():
    cfg = _basic_cfg(
        ticker_open_today={"AAPL": 102.0},
        ticker_prev_close={"AAPL": 100.0},
    )
    with SessionSimulator(cfg) as sim:
        sim.start()
        sim.feed_or(ticker="AAPL", or_low=101.5, or_high=102.5)
        sim.feed_bar(make_breakout_bar(bucket=600, side="long",
                                       or_high=102.5, or_low=101.5),
                     ticker="AAPL")
        sim.try_long(ticker="AAPL", price=103.0)
        return _normalize_history(sim.history())


SCENARIOS = {
    "golden_long_target": scenario_golden_long_target,
    "short_target": scenario_short_target,
    "long_stop": scenario_long_stop,
    "long_eod": scenario_long_eod,
    "vix_kill_blocks_entry": scenario_vix_kill_blocks_entry,
    "gap_skip": scenario_gap_skip,
}


# ----- Golden assertion -----------------------------------------


def _golden_path(name: str) -> Path:
    return GOLDEN_DIR / f"{name}.golden.json"


def _read_golden(name: str):
    p = _golden_path(name)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def _write_golden(name: str, ledger) -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    p = _golden_path(name)
    p.write_text(json.dumps(ledger, indent=2) + "\n")


@pytest.mark.parametrize("scenario_name", sorted(SCENARIOS.keys()))
def test_golden_ledger_matches(scenario_name, isolated_env):
    """For each named scenario, produce the ledger and either:
    - regenerate the golden (when REGEN_GOLDENS=1 env is set), or
    - diff against the checked-in golden and fail on disagreement.
    """
    actual = SCENARIOS[scenario_name]()
    if _regen_goldens_flag():
        _write_golden(scenario_name, actual)
        pytest.skip(f"regenerated {scenario_name}")
        return
    expected = _read_golden(scenario_name)
    if expected is None:
        # First run: write the golden + skip with a note. Re-run will
        # exercise the diff. CI MUST have these committed.
        _write_golden(scenario_name, actual)
        pytest.skip(
            f"no golden checked in for {scenario_name}; wrote one "
            f"from current run -- commit it and re-run"
        )
    assert actual == expected, (
        f"\nGOLDEN DIFF for {scenario_name}:\n"
        f"Expected (committed):\n{json.dumps(expected, indent=2)}\n\n"
        f"Actual (current run):\n{json.dumps(actual, indent=2)}\n\n"
        f"If the change is intentional, run:\n"
        f"  pytest tests/strategy/test_orb_golden_ledger.py "
        f"--regen-goldens\n"
        f"and commit the updated {scenario_name}.golden.json."
    )
