"""Tests for the simulator Phase-3 surfaces:

- simulator.diff (shared MATCH/DRIFT/SIM-ONLY/LIVE-ONLY logic)
- simulator.annual divergence aggregation
- simulator.synth_corpus perturbation primitives
"""
from __future__ import annotations

import json
import os

import pytest


# ---- simulator.diff ----------------------------------------------------


def test_diff_match_drift_sim_only_live_only():
    """The shared diff_one_day reports each verdict correctly."""
    from simulator.diff import diff_one_day

    sim_result = {
        "entries": [
            {"ticker": "AAPL", "side": "LONG", "price": 175.00},
            {"ticker": "MSFT", "side": "LONG", "price": 410.00},
            {"ticker": "ORCL", "side": "SHORT", "price": 120.00},
        ],
    }
    live = [
        {"ticker": "AAPL", "side": "LONG", "entry_price": 175.02},  # MATCH
        {"ticker": "MSFT", "side": "LONG", "entry_price": 411.50},  # DRIFT
        {"ticker": "NVDA", "side": "SHORT", "entry_price": 850.0},  # LIVE-ONLY
    ]
    d = diff_one_day(sim_result, live)

    by_key = {tuple(r["key"]): r["verdict"] for r in d["rows"]}
    assert by_key[("AAPL", "LONG")] == "MATCH"
    assert by_key[("MSFT", "LONG")] == "DRIFT"
    assert by_key[("ORCL", "SHORT")] == "SIM-ONLY"
    assert by_key[("NVDA", "SHORT")] == "LIVE-ONLY"
    assert d["drift_count"] == 1
    assert d["verdict"] == "DIVERGE"


def test_diff_pass_when_aligned():
    """No sim-only, no live-only, no drift -> PASS."""
    from simulator.diff import diff_one_day

    sim_result = {"entries": [{"ticker": "AAPL", "side": "LONG", "price": 175.00}]}
    live = [{"ticker": "AAPL", "side": "LONG", "entry_price": 175.05}]
    d = diff_one_day(sim_result, live)
    assert d["verdict"] == "PASS"
    assert d["drift_count"] == 0


def test_load_trade_log_filters_by_date(tmp_path):
    """load_trade_log returns only rows whose entry_ts or exit_ts
    starts with the date."""
    from simulator.diff import load_trade_log

    log = tmp_path / "trade_log.jsonl"
    log.write_text("\n".join([
        json.dumps({"ticker": "AAPL", "side": "LONG",
                    "entry_ts_utc": "2026-05-15T13:35:00Z",
                    "exit_ts_utc": "2026-05-15T13:50:00Z",
                    "entry_price": 175.0}),
        json.dumps({"ticker": "MSFT", "side": "SHORT",
                    "entry_ts_utc": "2026-05-14T14:10:00Z",
                    "exit_ts_utc": "2026-05-14T14:25:00Z",
                    "entry_price": 410.0}),
    ]))
    rows_15 = load_trade_log("2026-05-15", path=str(log))
    rows_14 = load_trade_log("2026-05-14", path=str(log))
    rows_99 = load_trade_log("2099-01-01", path=str(log))
    assert len(rows_15) == 1
    assert rows_15[0]["ticker"] == "AAPL"
    assert len(rows_14) == 1
    assert rows_99 == []


# ---- simulator.annual divergence --------------------------------------


def test_annual_divergence_returns_none_without_log(tmp_path):
    from simulator.annual import aggregate_live_divergence
    out = aggregate_live_divergence([], trade_log_path=str(tmp_path / "missing.jsonl"))
    assert out is None


def test_annual_divergence_counts_totals(tmp_path):
    """aggregate_live_divergence rolls up MATCH / DRIFT / SIM-ONLY /
    LIVE-ONLY across days."""
    from simulator.annual import aggregate_live_divergence

    log = tmp_path / "trade_log.jsonl"
    log.write_text("\n".join([
        # Day 1: MATCH
        json.dumps({"ticker": "AAPL", "side": "LONG",
                    "entry_ts_utc": "2026-05-14T13:35:00Z",
                    "entry_price": 175.00}),
        # Day 2: LIVE-ONLY (sim didn't fire here)
        json.dumps({"ticker": "QQQ", "side": "SHORT",
                    "entry_ts_utc": "2026-05-15T14:00:00Z",
                    "entry_price": 480.00}),
    ]))
    results = [
        {"date": "2026-05-14",
         "entries": [{"ticker": "AAPL", "side": "LONG", "price": 175.02}],
         "exits": []},
        {"date": "2026-05-15",
         "entries": [{"ticker": "NVDA", "side": "LONG", "price": 850.0}],
         "exits": []},
    ]
    out = aggregate_live_divergence(results, trade_log_path=str(log))
    assert out is not None
    assert out["totals"]["matched"] == 1
    assert out["totals"]["live_only"] == 1
    assert out["totals"]["sim_only"] == 1
    assert out["days_with_divergence"] == 1


# ---- simulator.synth_corpus ------------------------------------------


def test_perturb_gap_scales_prices():
    """gap_up shifts every OHLC by +pct%."""
    from simulator.synth_corpus import perturb_gap

    bars = [{"timestamp_utc": "2026-05-15T13:30:00Z",
             "open": 100.0, "high": 100.5, "low": 99.8, "close": 100.2,
             "iex_volume": 1000, "total_volume": 1200}]
    out = perturb_gap(bars, pct=3.0, direction="up")
    assert out[0]["open"] == pytest.approx(103.0, abs=0.01)
    assert out[0]["close"] == pytest.approx(103.206, abs=0.01)
    # Volume is unchanged.
    assert out[0]["iex_volume"] == 1000


def test_perturb_gap_zero_no_op():
    from simulator.synth_corpus import perturb_gap
    bars = [{"open": 100.0, "high": 100.5, "low": 99.8, "close": 100.2}]
    out = perturb_gap(bars, pct=0.0, direction="up")
    assert out[0]["open"] == 100.0


def test_perturb_halt_zeroes_volume_in_window():
    """perturb_halt zeroes the volume of `minutes` consecutive bars
    starting at 10:30 ET."""
    from simulator.synth_corpus import perturb_halt

    bars = []
    for hh, mm in [(10, 30), (10, 31), (10, 35), (10, 45)]:
        bars.append({
            "timestamp_utc": f"2026-05-15T{hh + 4:02d}:{mm:02d}:00Z",  # UTC = ET+4 (EDT)
            "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0,
            "iex_volume": 1000, "total_volume": 1500,
        })
    out = perturb_halt(bars, minutes=10)
    # 10-min halt window covers 10:30..10:39 (inclusive of start, exclusive
    # of end). 10:30, 10:31, 10:35 are inside; 10:45 is outside.
    assert out[0]["total_volume"] == 0
    assert out[1]["total_volume"] == 0
    assert out[2]["total_volume"] == 0
    assert out[3]["total_volume"] == 1500


def test_perturb_vol_3x_widens_range():
    """vol_3x triples the high-low range while keeping open/close intact."""
    from simulator.synth_corpus import perturb_vol_3x

    bars = [{"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.2}]
    out = perturb_vol_3x(bars)
    # Range was 1.0; tripled around midpoint 100.0 -> high>=101.5, low<=98.5.
    assert out[0]["high"] >= 101.5
    assert out[0]["low"] <= 98.5
    # Open/close unchanged (clamped within new high/low).
    assert out[0]["open"] == 100.0
    assert out[0]["close"] == 100.2


def test_generate_batch_writes_outputs(tmp_path):
    """generate_batch wires the perturbation engine end-to-end:
    reads from corpus_root, writes to output_root."""
    from simulator.synth_corpus import generate_batch

    # Seed a real-shaped corpus day.
    corpus = tmp_path / "data"
    day_dir = corpus / "2026-05-15"
    day_dir.mkdir(parents=True)
    bars = [
        {"timestamp_utc": "2026-05-15T13:30:00Z",
         "open": 100.0, "high": 100.5, "low": 99.8, "close": 100.2,
         "iex_volume": 1000, "total_volume": 1500},
        {"timestamp_utc": "2026-05-15T13:31:00Z",
         "open": 100.2, "high": 100.6, "low": 100.1, "close": 100.4,
         "iex_volume": 1100, "total_volume": 1600},
    ]
    (day_dir / "AAPL.jsonl").write_text("\n".join(json.dumps(b) for b in bars))

    out_root = tmp_path / "synth"
    manifests = generate_batch(
        source_dates=["2026-05-15"],
        perturbations=["gap_up_3", "vol_3x"],
        tickers=["AAPL"],
        corpus_root=str(corpus),
        output_root=str(out_root),
        workers=1,  # keep test single-process for reliability
    )
    assert len(manifests) == 2

    # gap_up_3 directory exists with AAPL.jsonl and manifest.json
    gap_dir = out_root / "gap_up_3_2026-05-15"
    assert (gap_dir / "AAPL.jsonl").exists()
    assert (gap_dir / "manifest.json").exists()
    written_bars = [json.loads(l) for l in (gap_dir / "AAPL.jsonl").read_text().splitlines()]
    assert written_bars[0]["open"] == pytest.approx(103.0, abs=0.01)

    # vol_3x widened the range.
    vol_dir = out_root / "vol_3x_2026-05-15"
    vol_bars = [json.loads(l) for l in (vol_dir / "AAPL.jsonl").read_text().splitlines()]
    assert vol_bars[0]["high"] >= 101.0


def test_unknown_perturbation_raises():
    from simulator.synth_corpus import _apply_perturbation
    with pytest.raises(ValueError):
        _apply_perturbation([{"open": 100.0}], "nonexistent_perturbation")
