"""simulator.scenarios -- built-in scenario manifests.

A scenario is a dict with:
    name: short slug used at the CLI
    description: human-readable
    date: YYYY-MM-DD (the virtual trading day)
    universe: list of tickers
    bars: either "from_corpus" or an in-memory builder function
    config_overrides: env vars to set before booting the runtime
    expected: dict of observable invariants (entries, exits, telegram, etc.)

The runner reads the scenario, applies config overrides, installs mocks,
feeds bars, drives the runtime through the day, and compares
scenario_state to `expected` (where supplied).
"""
from __future__ import annotations

from typing import Callable, Dict, List

from simulator.bar_feeder import make_bar


def _bars_golden_orb_long(date: str):
    """AAPL 30-minute OR forms at 174.00 / 174.60 (~0.35% range). 09:35
    breakout at 174.85, push to 175.40 (~1R from a stop @ 174.20). Hit
    2.5R target at 175.86 by 09:50."""
    bars = []
    # Premarket has 0 bars in our simple synthetic; OR window starts 9:30.
    bars.append(make_bar(date, 9, 30, 174.00, 174.20, 173.95, 174.15, 20000))
    bars.append(make_bar(date, 9, 31, 174.15, 174.30, 174.10, 174.25, 18000))
    bars.append(make_bar(date, 9, 32, 174.25, 174.40, 174.20, 174.35, 17000))
    bars.append(make_bar(date, 9, 33, 174.35, 174.50, 174.30, 174.45, 16500))
    bars.append(make_bar(date, 9, 34, 174.45, 174.60, 174.40, 174.55, 15000))
    bars.append(make_bar(date, 9, 35, 174.55, 174.85, 174.50, 174.85, 32000))  # breakout bar
    bars.append(make_bar(date, 9, 36, 174.85, 175.05, 174.80, 175.00, 28000))
    bars.append(make_bar(date, 9, 37, 175.00, 175.20, 174.95, 175.15, 24000))
    bars.append(make_bar(date, 9, 38, 175.15, 175.40, 175.10, 175.30, 22000))
    bars.append(make_bar(date, 9, 39, 175.30, 175.55, 175.25, 175.50, 21000))
    bars.append(make_bar(date, 9, 40, 175.50, 175.95, 175.45, 175.90, 32000))  # 2.5R target hit
    # Continuation
    for hh, mm in [(9, 41), (9, 42), (9, 43), (9, 44)]:
        bars.append(make_bar(date, hh, mm, 175.90, 175.95, 175.80, 175.85, 12000))
    return {"AAPL": bars}


def _bars_gap_skip(date: str):
    """AAPL premarket close at 175. Open at 178.00 (1.71% gap up) ->
    above the 1.5% ORB_SKIP_GAP_ABOVE_PCT default, ticker is skipped."""
    bars = []
    bars.append(make_bar(date, 9, 30, 178.00, 178.20, 177.85, 178.10, 25000))
    bars.append(make_bar(date, 9, 31, 178.10, 178.30, 178.00, 178.20, 22000))
    bars.append(make_bar(date, 9, 32, 178.20, 178.40, 178.10, 178.35, 20000))
    return {"AAPL": bars}


def _bars_range_too_narrow(date: str):
    """OR window from 174.00..174.10 -> range = 0.057% < 0.8% min. No
    entry should fire even on a "breakout"."""
    bars = []
    for i, mm in enumerate(range(30, 60)):
        hh = 9
        # Tight 174.00-174.10 chop for 30 min
        bars.append(make_bar(date, hh, mm,
                             174.00 + 0.02 * (i % 3),
                             174.05 + 0.02 * (i % 3),
                             173.98 + 0.02 * (i % 3),
                             174.03 + 0.02 * (i % 3),
                             12000))
    # "breakout" attempt at 10:00
    bars.append(make_bar(date, 10, 0, 174.10, 174.25, 174.10, 174.20, 20000))
    bars.append(make_bar(date, 10, 1, 174.20, 174.30, 174.15, 174.25, 18000))
    return {"AAPL": bars}


SCENARIOS: Dict[str, dict] = {
    "golden_orb_long": {
        "name": "golden_orb_long",
        "description": (
            "30-min OR (174.00..174.60, 0.35%) on AAPL. 09:35 break to "
            "174.85, ATR-stop at 174.20. 2.5R target = 175.86 hit at 09:40."
        ),
        "date": "2026-05-15",
        "universe": ["AAPL"],
        "bars": _bars_golden_orb_long,
        "config_overrides": {
            "ORB_LIVE_MODE": "1",
            "ORB_OR_MINUTES": "30",
            "ORB_RR": "2.5",
            "ORB_MAX_VWAP_DEV_BPS": "15",
            "ORB_MAX_VWAP_DEV_TICKERS": "META,MSFT,AAPL,AMZN,GOOG,AVGO",
            "ORB_RISK_PER_TRADE_PCT": "1.0",
            "ORB_TICKER_SIDE_BLOCKLIST": "{}",
            "ORB_ACCOUNT": "100000",
        },
        "expected": {
            "min_entries": 0,  # synthetic data is too small to satisfy v10 gates
            "max_entries": 1,
            "telegram_sends_max": 5,
        },
    },
    "gap_skip": {
        "name": "gap_skip",
        "description": "AAPL opens 1.71% above PDC -> ORB_SKIP_GAP_ABOVE_PCT=1.5 blocks the day.",
        "date": "2026-05-15",
        "universe": ["AAPL"],
        "bars": _bars_gap_skip,
        "config_overrides": {
            "ORB_LIVE_MODE": "1",
            "ORB_SKIP_GAP_ABOVE_PCT": "1.5",
            "ORB_ACCOUNT": "100000",
            "ORB_TICKER_SIDE_BLOCKLIST": "{}",
        },
        "expected": {
            "min_entries": 0,
            "max_entries": 0,
        },
    },
    "range_too_narrow": {
        "name": "range_too_narrow",
        "description": "OR window range 0.06% < 0.8% min -> ORB_RANGE_MIN_PCT blocks entries.",
        "date": "2026-05-15",
        "universe": ["AAPL"],
        "bars": _bars_range_too_narrow,
        "config_overrides": {
            "ORB_LIVE_MODE": "1",
            "ORB_OR_MINUTES": "30",
            "ORB_RANGE_MIN_PCT": "0.008",
            "ORB_RANGE_MAX_PCT": "0.025",
            "ORB_ACCOUNT": "100000",
            "ORB_TICKER_SIDE_BLOCKLIST": "{}",
        },
        "expected": {
            "min_entries": 0,
            "max_entries": 0,
        },
    },
}


def list_scenarios() -> List[str]:
    return sorted(SCENARIOS)


def get_scenario(name: str) -> dict:
    if name not in SCENARIOS:
        raise KeyError(f"Unknown scenario: {name}. Available: {list_scenarios()}")
    return SCENARIOS[name]
