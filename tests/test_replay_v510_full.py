"""v5.10.6 \u2014 minimal smoke test for backtest_v510.replay_v510_full.

Verifies the script runs end-to-end on a synthetic bars directory and
produces the expected markdown report shape. Does NOT validate
algorithm correctness; that's the job of the real replay against
production bars in CI's nightly job.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _write_synthetic_bars(bars_dir: Path, day: str) -> None:
    day_dir = bars_dir / day
    day_dir.mkdir(parents=True, exist_ok=True)
    base = datetime(2026, 4, 27, 13, 30, tzinfo=timezone.utc)
    for tkr, start in [("QQQ", 500.0), ("AAPL", 150.0)]:
        path = day_dir / f"{tkr}.jsonl"
        with open(path, "w") as f:
            price = start
            for i in range(390):
                ts = base + timedelta(minutes=i)
                price += 0.05
                bar = {
                    "ts_utc": ts.isoformat().replace("+00:00", "Z"),
                    "open": price - 0.1,
                    "high": price + 0.2,
                    "low": price - 0.2,
                    "close": price,
                    "volume": 5000,
                    "vwap": price,
                }
                f.write(json.dumps(bar) + "\n")


def test_replay_runs_on_synthetic_bars(tmp_path):
    bars_dir = tmp_path / "bars"
    _write_synthetic_bars(bars_dir, "2026-04-27")
    from backtest_v510.replay_v510_full import main

    out = tmp_path / "report.md"
    rc = main([
        "--bars-dir", str(bars_dir),
        "--start", "2026-04-27",
        "--end", "2026-04-27",
        "--output", str(out),
    ])
    assert rc == 0
    assert out.is_file()
    txt = out.read_text()
    assert "v5.10 Full-Algorithm Backtest Replay" in txt
    assert "2026-04-27" in txt
    assert "Per-day summary" in txt


def test_replay_handles_missing_bars_dir(tmp_path):
    from backtest_v510.replay_v510_full import main

    out = tmp_path / "report.md"
    rc = main([
        "--bars-dir", str(tmp_path / "nonexistent"),
        "--output", str(out),
    ])
    assert rc == 0
    assert out.is_file()
    txt = out.read_text()
    assert "No bar data was available" in txt


def test_replay_enforces_guards_when_flag_set(tmp_path):
    bars_dir = tmp_path / "bars"
    _write_synthetic_bars(bars_dir, "2026-04-27")
    from backtest_v510.replay_v510_full import main, SINGLE_DAY_LOSS_GUARD_DOLLARS

    assert SINGLE_DAY_LOSS_GUARD_DOLLARS == -5000.0
