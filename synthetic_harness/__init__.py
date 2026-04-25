"""Synthetic trading harness for TradeGenius.

A hermetic, deterministic test harness that drives trade_genius.py through
realistic market scenarios with no network calls, captures every observable
output, and compares against recorded "golden" outputs.

Public API:
    list_scenarios() -> list[str]
    record_scenario(name) -> dict       # records and writes golden
    replay_scenario(name) -> tuple[bool, str]  # (ok, diff_text)
    run_scenario(name) -> dict          # runs scenario, returns observed
"""
from synthetic_harness.scenarios import (
    list_scenarios,
    get_scenario,
    SCENARIOS,
)
from synthetic_harness.recorder import OutputRecorder
from synthetic_harness.runner import (
    run_scenario,
    record_scenario,
    replay_scenario,
)

__all__ = [
    "list_scenarios",
    "get_scenario",
    "SCENARIOS",
    "OutputRecorder",
    "run_scenario",
    "record_scenario",
    "replay_scenario",
]

HARNESS_VERSION = 1
