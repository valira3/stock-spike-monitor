"""v5.10.0 \u2014 unit tests for VolumeBucketBaseline (Section II.1)."""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import volume_bucket as vb


def _trading_days_back(today: date, n: int) -> list[date]:
    # v6.14.3: production volume_bucket._trading_days_back skips both
    # weekends AND US market holidays. Tests must use the same helper
    # so generated bar coverage matches what the baseline counts as
    # "days available" (otherwise a 55-day weekend-only window grazes
    # Presidents Day or Good Friday and pins days_available at 53).
    return vb._trading_days_back(today, n)


def _write_bar(base: Path, day: date, ticker: str, et_bucket: str, volume: int):
    p = base / day.strftime("%Y-%m-%d")
    p.mkdir(parents=True, exist_ok=True)
    fp = p / f"{ticker}.jsonl"
    with open(fp, "a") as fh:
        bar = {"et_bucket": et_bucket, "iex_volume": volume,
               "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.2}
        fh.write(json.dumps(bar) + "\n")


def test_post_cold_start_pass_at_110pct(tmp_path):
    today = date(2026, 4, 28)
    days = _trading_days_back(today, 60)
    for d in days:
        _write_bar(tmp_path, d, "AAPL", "0935", 1000)
    bb = vb.VolumeBucketBaseline(base_dir=str(tmp_path))
    bb.refresh(today=today)
    assert bb.days_available("AAPL") >= vb.VOLUME_BUCKET_LOOKBACK_DAYS
    res = bb.check("AAPL", "09:35", 1100)
    assert res["gate"] == "PASS"
    assert abs(res["ratio"] - 1.1) < 1e-9


def test_post_cold_start_fail_at_99pct(tmp_path):
    today = date(2026, 4, 28)
    days = _trading_days_back(today, 60)
    for d in days:
        _write_bar(tmp_path, d, "AAPL", "0935", 1000)
    bb = vb.VolumeBucketBaseline(base_dir=str(tmp_path))
    bb.refresh(today=today)
    res = bb.check("AAPL", "09:35", 990)
    assert res["gate"] == "FAIL"


def test_pass_at_exactly_100pct(tmp_path):
    today = date(2026, 4, 28)
    days = _trading_days_back(today, 60)
    for d in days:
        _write_bar(tmp_path, d, "AAPL", "0935", 1000)
    bb = vb.VolumeBucketBaseline(base_dir=str(tmp_path))
    bb.refresh(today=today)
    res = bb.check("AAPL", "09:35", 1000)
    assert res["gate"] == "PASS"


def test_cold_start_passthrough(tmp_path):
    today = date(2026, 4, 28)
    days = _trading_days_back(today, 30)  # < 55
    for d in days:
        _write_bar(tmp_path, d, "MSFT", "0935", 1000)
    bb = vb.VolumeBucketBaseline(base_dir=str(tmp_path))
    bb.refresh(today=today)
    res = bb.check("MSFT", "09:35", 100)  # well below threshold
    assert res["gate"] == "COLDSTART"
    assert res["days_available"] == 30


def test_cold_start_log_rate_limited(tmp_path, caplog):
    today = date(2026, 4, 28)
    days = _trading_days_back(today, 10)
    for d in days:
        _write_bar(tmp_path, d, "TSLA", "0935", 1000)
    bb = vb.VolumeBucketBaseline(base_dir=str(tmp_path))
    bb.refresh(today=today)
    with caplog.at_level(logging.WARNING, logger="trade_genius.volume_bucket"):
        for _ in range(5):
            bb.check("TSLA", "09:35", 100)
    cs_logs = [r for r in caplog.records if "VOLBUCKET-COLDSTART" in r.getMessage()]
    assert len(cs_logs) == 1


def test_minute_of_day_bucketing_per_minute(tmp_path):
    today = date(2026, 4, 28)
    days = _trading_days_back(today, 60)
    for d in days:
        _write_bar(tmp_path, d, "AAPL", "0935", 1000)
        _write_bar(tmp_path, d, "AAPL", "1000", 5000)
    bb = vb.VolumeBucketBaseline(base_dir=str(tmp_path))
    bb.refresh(today=today)
    # 09:35 baseline = 1000; 1100 vol -> ratio 1.1 PASS
    r1 = bb.check("AAPL", "09:35", 1100)
    assert r1["gate"] == "PASS" and abs(r1["ratio"] - 1.1) < 1e-9
    # 10:00 baseline = 5000; 1100 vol -> ratio 0.22 FAIL
    r2 = bb.check("AAPL", "10:00", 1100)
    assert r2["gate"] == "FAIL"


def test_premarket_bucket_rejected(tmp_path):
    today = date(2026, 4, 28)
    days = _trading_days_back(today, 60)
    for d in days:
        _write_bar(tmp_path, d, "AAPL", "0935", 1000)
    bb = vb.VolumeBucketBaseline(base_dir=str(tmp_path))
    bb.refresh(today=today)
    # 09:00 is pre-market; key normalisation rejects it -> FAIL
    res = bb.check("AAPL", "09:00", 1100)
    assert res["gate"] == "FAIL"


def test_unknown_ticker_returns_coldstart(tmp_path):
    today = date(2026, 4, 28)
    bb = vb.VolumeBucketBaseline(base_dir=str(tmp_path))
    bb.refresh(today=today)
    res = bb.check("ZZZZ", "09:35", 1000)
    assert res["gate"] == "COLDSTART"
    assert res["days_available"] == 0


def test_baseline_recompute_after_new_day_added(tmp_path):
    today = date(2026, 4, 28)
    days = _trading_days_back(today, 60)
    for d in days:
        _write_bar(tmp_path, d, "AAPL", "0935", 1000)
    bb = vb.VolumeBucketBaseline(base_dir=str(tmp_path))
    bb.refresh(today=today)
    base1 = bb.baseline["AAPL"]["09:35"]
    # Add a new day with much larger volume; refresh again
    _write_bar(tmp_path, today, "AAPL", "0935", 9999)
    bb.refresh(today=today + timedelta(days=1))
    base2 = bb.baseline["AAPL"]["09:35"]
    assert base2 > base1
