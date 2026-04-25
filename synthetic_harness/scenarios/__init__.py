"""Scenario corpus — registry of all 25 named scenarios.

Each scenario module exposes a build() factory that returns a Scenario
instance. The registry below imports each and collects them by name.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from synthetic_harness.market import SyntheticMarket
from synthetic_harness.clock import FrozenClock


@dataclass
class Action:
    """One harness step.

    kind selects the dispatch function inside the runner.

    Supported kinds:
      check_entry       (ticker)
      check_short_entry (ticker)
      execute_entry     (ticker, current_price)
      execute_short_entry (ticker, current_price)
      close_position    (ticker, price, reason)
      close_short_position (ticker, price, reason)
      scan_loop         ()
      manage_positions  ()
      manage_short_positions ()
      tick_minutes      (n)        # advance clock; recorded as no-op
      set_price         (ticker, current_price)  # mutate market
      set_global        (attr, value)            # mutate module attr
    """
    kind: str
    args: tuple = ()
    label: str | None = None


@dataclass
class Scenario:
    name: str
    description: str
    initial_state: dict = field(default_factory=dict)
    initial_market: dict = field(default_factory=dict)
    initial_time: datetime | None = None
    actions: list[Action] = field(default_factory=list)
    setup_callbacks: list[Callable] = field(default_factory=list)


# ------------------------------------------------------------------
# Registry: collect all scenarios from sub-modules.
# ------------------------------------------------------------------
def _build_registry() -> dict[str, Scenario]:
    from synthetic_harness.scenarios import long_entries
    from synthetic_harness.scenarios import short_entries
    from synthetic_harness.scenarios import long_closes
    from synthetic_harness.scenarios import short_closes
    from synthetic_harness.scenarios import scan_loops

    builders = []
    for mod in (long_entries, short_entries, long_closes,
                short_closes, scan_loops):
        builders.extend(mod.SCENARIOS)
    out = {}
    for build in builders:
        sc = build()
        if sc.name in out:
            raise RuntimeError(f"duplicate scenario name: {sc.name}")
        out[sc.name] = sc
    return out


SCENARIOS: dict[str, Scenario] = _build_registry()


def list_scenarios() -> list[str]:
    return list(SCENARIOS)


def get_scenario(name: str) -> Scenario:
    if name not in SCENARIOS:
        raise KeyError(f"unknown scenario: {name}")
    return SCENARIOS[name]
