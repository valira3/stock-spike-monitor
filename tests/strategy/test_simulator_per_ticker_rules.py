"""Tests for the per-ticker expectation rules introduced after the
SPY-proxy false-positive investigation.

The runner now stamps `ticker_gap_pct` and `ticker_or_range_pct` on
every entry. DEFAULT_RULES uses those fields to assert the bot's
*actual* per-ticker gates fired correctly.
"""
from __future__ import annotations

import pytest


def test_per_ticker_gap_gate_blocks_high_gap():
    """An entry with ticker_gap_pct beyond the 1.5% threshold should
    trip the per_ticker_gap_gate ERROR rule."""
    from simulator.expectations import DEFAULT_RULES, evaluate

    day_row = {"categories": ["baseline"]}
    day_result = {
        "entries": [
            {"ticker": "AAPL", "side": "LONG", "ticker_gap_pct": 2.5,
             "ticker_or_range_pct": 1.2},
        ],
        "exits": [], "alpaca_orders": [], "realized_pl_total": 0.0,
        "open_at_eod": [],
    }
    failures = evaluate(day_row, day_result, DEFAULT_RULES)
    names = {f.rule_name for f in failures}
    assert "per_ticker_gap_gate" in names
    err = next(f for f in failures if f.rule_name == "per_ticker_gap_gate")
    assert err.severity == "ERROR"
    assert "2.50%" in err.why_fail or "2.5%" in err.why_fail


def test_per_ticker_gap_gate_passes_in_band():
    """A gap below the threshold should pass."""
    from simulator.expectations import DEFAULT_RULES, evaluate

    day_row = {"categories": ["baseline"]}
    day_result = {
        "entries": [
            {"ticker": "AAPL", "side": "LONG", "ticker_gap_pct": 0.8,
             "ticker_or_range_pct": 1.2},
        ],
        "exits": [], "alpaca_orders": [], "realized_pl_total": 0.0,
        "open_at_eod": [],
    }
    failures = evaluate(day_row, day_result, DEFAULT_RULES)
    names = {f.rule_name for f in failures}
    assert "per_ticker_gap_gate" not in names


def test_per_ticker_gap_handles_negative_gap():
    """abs() means a -2% gap is just as bad as a +2% gap."""
    from simulator.expectations import DEFAULT_RULES, evaluate

    day_row = {"categories": ["baseline"]}
    day_result = {
        "entries": [
            {"ticker": "AAPL", "side": "LONG", "ticker_gap_pct": -2.0,
             "ticker_or_range_pct": 1.0},
        ],
        "exits": [], "alpaca_orders": [], "realized_pl_total": 0.0,
        "open_at_eod": [],
    }
    failures = evaluate(day_row, day_result, DEFAULT_RULES)
    names = {f.rule_name for f in failures}
    assert "per_ticker_gap_gate" in names


def test_per_ticker_range_floor_blocks_too_narrow():
    """An entry where the firing ticker's OR range is below 0.8% trips
    per_ticker_range_floor."""
    from simulator.expectations import DEFAULT_RULES, evaluate

    day_row = {"categories": ["baseline"]}
    day_result = {
        "entries": [
            {"ticker": "AAPL", "side": "LONG", "ticker_gap_pct": 0.5,
             "ticker_or_range_pct": 0.3},
        ],
        "exits": [], "alpaca_orders": [], "realized_pl_total": 0.0,
        "open_at_eod": [],
    }
    failures = evaluate(day_row, day_result, DEFAULT_RULES)
    names = {f.rule_name for f in failures}
    assert "per_ticker_range_floor" in names
    err = next(f for f in failures if f.rule_name == "per_ticker_range_floor")
    assert err.severity == "ERROR"


def test_per_ticker_range_ceiling_blocks_too_wide():
    from simulator.expectations import DEFAULT_RULES, evaluate

    day_row = {"categories": ["baseline"]}
    day_result = {
        "entries": [
            {"ticker": "AAPL", "side": "LONG", "ticker_gap_pct": 0.5,
             "ticker_or_range_pct": 3.5},
        ],
        "exits": [], "alpaca_orders": [], "realized_pl_total": 0.0,
        "open_at_eod": [],
    }
    failures = evaluate(day_row, day_result, DEFAULT_RULES)
    names = {f.rule_name for f in failures}
    assert "per_ticker_range_ceiling" in names


def test_per_ticker_rules_handle_empty_entries():
    """When no entries fire, the per-ticker rules should not flag.
    (The min_or_range default of 0 must not trigger range_floor.)"""
    from simulator.expectations import DEFAULT_RULES, evaluate

    day_row = {"categories": ["baseline"]}
    day_result = {
        "entries": [], "exits": [], "alpaca_orders": [],
        "realized_pl_total": 0.0, "open_at_eod": [],
    }
    failures = evaluate(day_row, day_result, DEFAULT_RULES)
    names = {f.rule_name for f in failures}
    assert "per_ticker_gap_gate" not in names
    assert "per_ticker_range_floor" not in names
    assert "per_ticker_range_ceiling" not in names


def test_per_ticker_rules_take_worst_across_entries():
    """When multiple entries fire on the same day, the gap rule keys on
    the *worst* (max-abs) ticker gap. One bad entry trips the rule
    even if others are fine."""
    from simulator.expectations import DEFAULT_RULES, evaluate

    day_row = {"categories": ["baseline"]}
    day_result = {
        "entries": [
            {"ticker": "AAPL", "side": "LONG", "ticker_gap_pct": 0.3,
             "ticker_or_range_pct": 1.0},
            {"ticker": "MSFT", "side": "LONG", "ticker_gap_pct": 2.1,
             "ticker_or_range_pct": 1.2},  # over-gap
            {"ticker": "NVDA", "side": "SHORT", "ticker_gap_pct": -0.5,
             "ticker_or_range_pct": 1.0},
        ],
        "exits": [], "alpaca_orders": [], "realized_pl_total": 0.0,
        "open_at_eod": [],
    }
    failures = evaluate(day_row, day_result, DEFAULT_RULES)
    names = {f.rule_name for f in failures}
    assert "per_ticker_gap_gate" in names
    err = next(f for f in failures if f.rule_name == "per_ticker_gap_gate")
    assert "2.10%" in err.why_fail or "2.1" in err.why_fail


def test_no_carry_over_still_enforced():
    """The day-level no_carry_over invariant survives the rule overhaul."""
    from simulator.expectations import DEFAULT_RULES, evaluate

    day_row = {"categories": ["baseline"]}
    day_result = {
        "entries": [{"ticker": "AAPL", "side": "LONG",
                     "ticker_gap_pct": 0.3, "ticker_or_range_pct": 1.0}],
        "exits": [], "alpaca_orders": [], "realized_pl_total": 0.0,
        "open_at_eod": ["AAPL"],  # carryover!
    }
    failures = evaluate(day_row, day_result, DEFAULT_RULES)
    carry = [f for f in failures if f.rule_name == "no_carry_over"]
    assert len(carry) == 1
    assert carry[0].severity == "ERROR"
