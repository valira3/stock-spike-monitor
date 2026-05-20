"""Tests for the simulator Phase-2 surfaces: batch, expectations,
anomaly, replay, annual."""
from __future__ import annotations

import json
import os

import pytest


def test_imports_clean():
    """All Phase-2 modules import without side effects."""
    from simulator import batch  # noqa: F401
    from simulator import expectations  # noqa: F401
    from simulator import anomaly  # noqa: F401
    from simulator import replay  # noqa: F401
    from simulator import annual  # noqa: F401
    from simulator import corpus_index  # noqa: F401
    assert hasattr(batch, "run_days")
    assert hasattr(expectations, "DEFAULT_RULES")
    assert hasattr(annual, "aggregate")


def test_expectations_dsl_basic():
    """The expectation DSL evaluates rules correctly."""
    from simulator.expectations import Rule, evaluate

    rules = [Rule(
        name="gap_blocks_entry",
        matcher={"categories__contains": "gap_up_1_5pct"},
        expect={"max_entries": 0},
        severity="WARN",
        why="gap-up days should not produce entries",
    )]

    # Matching day with 0 entries -> PASS
    day_row = {"categories": ["gap_up_1_5pct", "vix_high"]}
    day_result = {"entries": [], "exits": [], "alpaca_orders": [],
                  "realized_pl_total": 0.0, "open_at_eod": []}
    fails = evaluate(day_row, day_result, rules)
    assert fails == []

    # Matching day with 1 entry -> FAIL
    day_result_with_entry = dict(day_result, entries=[{"ticker": "AAPL"}])
    fails = evaluate(day_row, day_result_with_entry, rules)
    assert len(fails) == 1
    assert fails[0].rule_name == "gap_blocks_entry"
    assert fails[0].severity == "WARN"

    # Non-matching day with entries -> PASS (rule didn't apply)
    day_row_normal = {"categories": ["baseline"]}
    fails = evaluate(day_row_normal, day_result_with_entry, rules)
    assert fails == []


def test_expectations_open_at_eod_rule():
    """The "no positions open at EOD" rule fires when any position
    remains after the flush."""
    from simulator.expectations import DEFAULT_RULES, evaluate

    day_row = {"categories": ["baseline"]}
    day_result_clean = {"entries": [{"ticker": "AAPL"}], "exits": [{"ticker": "AAPL"}],
                        "alpaca_orders": [{"id": "x"}], "realized_pl_total": 10.0,
                        "open_at_eod": []}
    fails_clean = evaluate(day_row, day_result_clean, DEFAULT_RULES)
    # The "no_carry_over" rule should pass.
    carry = [f for f in fails_clean if f.rule_name == "no_carry_over"]
    assert carry == []

    # Same day, but a position is still open at EOD -> ERROR.
    day_result_dirty = dict(day_result_clean, open_at_eod=["AAPL"])
    fails_dirty = evaluate(day_row, day_result_dirty, DEFAULT_RULES)
    carry = [f for f in fails_dirty if f.rule_name == "no_carry_over"]
    assert len(carry) == 1
    assert carry[0].severity == "ERROR"


def test_corpus_index_classify_skips_missing():
    """classify_day returns None when SPY bars are missing."""
    from simulator.corpus_index import classify_day
    assert classify_day("9999-01-01", corpus_root="/nonexistent") is None


def test_annual_aggregate_handles_empty():
    from simulator.annual import aggregate
    out = aggregate([], starting_equity=100_000.0)
    assert "error" in out


def test_annual_aggregate_one_day():
    """Single-day aggregate produces sane summary."""
    from simulator.annual import aggregate
    results = [{
        "date": "2026-05-15",
        "entries": [{"ticker": "AAPL"}],
        "exits": [{"ticker": "AAPL"}],
        "alpaca_orders": [{"id": "x"}, {"id": "y"}],
        "realized_pl": {"AAPL": 250.0},
        "realized_pl_total": 250.0,
        "open_at_eod": [],
        "telegram_count": 2,
        "fmp_count": 5,
        "yahoo_count": 0,
    }]
    agg = aggregate(results, starting_equity=100_000.0)
    assert agg["n_days"] == 1
    assert agg["n_entries"] == 1
    assert agg["wins"] == 1
    assert agg["losses"] == 0
    assert agg["win_rate_pct"] == 100.0
    assert agg["total_pl"] == 250.0
    assert agg["ending_equity"] == 100_250.0


def test_replay_diff_pairs_entries_and_live(tmp_path):
    """_diff_one_day groups entries by (ticker, side) and labels each
    pair as MATCH / DRIFT / SIM-ONLY / LIVE-ONLY."""
    from simulator.replay import _diff_one_day

    sim_result = {
        "entries": [
            {"ticker": "AAPL", "side": "LONG", "price": 175.00,
             "bucket": 9 * 60 + 35},
            {"ticker": "MSFT", "side": "LONG", "price": 410.00,
             "bucket": 10 * 60 + 30},
        ],
        "exits": [],
    }
    live_trades = [
        # AAPL matches sim closely
        {"ticker": "AAPL", "side": "LONG", "entry_price": 175.02},
        # NVDA didn't fire in sim
        {"ticker": "NVDA", "side": "SHORT", "entry_price": 850.0},
    ]
    d = _diff_one_day(sim_result, live_trades)
    rows_by_verdict = {r["verdict"] for r in d["rows"]}
    assert "MATCH" in rows_by_verdict
    assert "SIM-ONLY" in rows_by_verdict
    assert "LIVE-ONLY" in rows_by_verdict
    assert d["verdict"] == "DIVERGE"


def test_batch_single_process_path(tmp_path):
    """batch.run_days with workers=1 + a synthetic 1-day request
    completes without raising. We don't assert on the strategy's
    fires (real bot decisioning depends on full corpus + SPY)."""
    from simulator.batch import BatchConfig, run_days

    # Build a tiny corpus on disk so the bar feeder has something to read.
    corpus = tmp_path / "data"
    day_dir = corpus / "2026-05-15"
    day_dir.mkdir(parents=True)
    bars = []
    for mm in range(30, 45):
        bars.append({
            "timestamp_utc": f"2026-05-15T13:{mm:02d}:00Z",
            "open": 100.0, "high": 100.1, "low": 99.95, "close": 100.05,
            "iex_volume": 5000, "total_volume": 5500,
        })
    with open(day_dir / "AAPL.jsonl", "w") as fh:
        for b in bars:
            fh.write(json.dumps(b) + "\n")

    os.environ["TG_DATA_ROOT"] = str(tmp_path / "tgdata")
    cfg = BatchConfig(workers=1, corpus_root=str(corpus),
                      show_progress=False)
    results = run_days(["2026-05-15"], ["AAPL"], cfg)

    assert len(results) == 1
    r = results[0]
    assert r["date"] == "2026-05-15"
    assert "entries" in r
    assert r.get("error") is None, f"Worker errored: {r.get('error')}"


def test_anomaly_picks_representative_days():
    """pick_representative pulls up to N dates per category."""
    from simulator.corpus_index import pick_representative

    index = [
        {"date": "2025-01-02", "categories": ["baseline"]},
        {"date": "2025-01-03", "categories": ["baseline"]},
        {"date": "2025-01-04", "categories": ["gap_up_1_5pct"]},
        {"date": "2025-01-05", "categories": ["gap_up_1_5pct", "vix_high"]},
        {"date": "2025-01-06", "categories": ["vix_high"]},
    ]
    sample = pick_representative(index, per_category=2)
    # 3 categories, ≤2 dates each. baseline and vix_high have 2 candidates;
    # gap_up_1_5pct has 2 candidates as well.
    assert len(sample) >= 3
    assert "2025-01-04" in sample or "2025-01-05" in sample  # gap-up
    assert "2025-01-05" in sample or "2025-01-06" in sample  # vix


def test_anomaly_categories_filter():
    from simulator.corpus_index import pick_representative

    index = [
        {"date": "2025-01-02", "categories": ["baseline"]},
        {"date": "2025-01-04", "categories": ["gap_up_1_5pct"]},
        {"date": "2025-01-06", "categories": ["vix_high"]},
    ]
    out = pick_representative(index, per_category=5,
                              categories=["gap_up_1_5pct"])
    assert out == ["2025-01-04"]
