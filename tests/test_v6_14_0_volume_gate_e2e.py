"""v6.14.0 end-to-end tests for the volume-gate fix (issue #354).

These tests cover the full ingest -> archive -> baseline chain that
v6.14.0 wires up:

  1. Bars on disk carry both ``et_bucket`` and ``total_volume``.
  2. ``VolumeBucketBaseline.refresh`` reads ``total_volume`` first
     and falls back to ``iex_volume`` so legacy bars still count.
  3. ``check`` returns a numeric ``ratio_to_55bar_avg`` (the v15 spec
     field name that the 10:00 ET conditional path keys on) once the
     archive has at least 55 trading days for the ticker.
  4. Zero-volume bars (the SIP path historically wrote
     ``iex_volume=0`` because IEX is roughly 3 percent of total US
     volume) are skipped by the rolling mean and do not silently null
     the baseline.
  5. ``ingest.algo_plus._compute_et_bucket`` produces RTH HHMM keys
     that match what the baseline reader expects.

NOTE: this test file is intentionally em-dash free (escaped or
literal) per the v6.14.0 author guidelines.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import volume_bucket as vb
from ingest.algo_plus import _compute_et_bucket


PROD_TICKERS = (
    "AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOG",
    "AMZN", "AVGO", "NFLX", "ORCL", "SPY", "QQQ",
)


def _trading_days_back(today: date, n: int) -> list[date]:
    out: list[date] = []
    d = today - timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


def _write_v614_bar(
    base: Path,
    day: date,
    ticker: str,
    et_bucket: str,
    total_volume: float,
    iex_volume: float | None = 0.0,
) -> None:
    """Write one v6.14.0-shape bar (total_volume populated)."""
    p = base / day.strftime("%Y-%m-%d")
    p.mkdir(parents=True, exist_ok=True)
    fp = p / f"{ticker}.jsonl"
    bar = {
        "et_bucket": et_bucket,
        "total_volume": total_volume,
        "iex_volume": iex_volume,
        "open": 100.0,
        "high": 100.5,
        "low": 99.5,
        "close": 100.2,
        "ts": f"{day.isoformat()}T13:35:00+00:00",
    }
    with open(fp, "a") as fh:
        fh.write(json.dumps(bar) + "\n")


def test_e2e_55day_archive_total_volume_drives_baseline(tmp_path):
    """Build a 55-day synthetic archive with v6.14.0 fields populated
    and assert the full chain works.
    """
    today = date(2026, 5, 4)
    days = _trading_days_back(today, 55)

    for d in days:
        _write_v614_bar(tmp_path, d, "AAPL", "0935", total_volume=120_000.0)

    bb = vb.VolumeBucketBaseline(base_dir=str(tmp_path))
    bb.refresh(today=today)

    assert bb.days_available("AAPL") == 55, (
        f"expected 55 days, got {bb.days_available('AAPL')}"
    )

    res = bb.check("AAPL", "09:35", 100_000)
    assert res["gate"] in ("PASS", "FAIL"), res
    assert res["days_available"] == 55
    assert res["baseline"] is not None
    assert abs(res["baseline"] - 120_000.0) < 1e-6
    assert res["ratio_to_55bar_avg"] is not None
    assert isinstance(res["ratio_to_55bar_avg"], (int, float))
    assert abs(res["ratio_to_55bar_avg"] - (100_000.0 / 120_000.0)) < 1e-9


def test_e2e_iex_volume_fallback_for_legacy_bars(tmp_path):
    """Legacy bars (pre v6.14.0) only have ``iex_volume`` populated.
    Those should still contribute to the baseline as a fallback so
    the disk-resident historical archive is not invalidated.
    """
    today = date(2026, 5, 4)
    days = _trading_days_back(today, 55)

    p_root = tmp_path
    for d in days:
        day_dir = p_root / d.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        with open(day_dir / "MSFT.jsonl", "a") as fh:
            bar = {
                "et_bucket": "0935",
                "iex_volume": 5000.0,
                "open": 400.0, "high": 400.5, "low": 399.5, "close": 400.2,
            }
            fh.write(json.dumps(bar) + "\n")

    bb = vb.VolumeBucketBaseline(base_dir=str(p_root))
    bb.refresh(today=today)

    assert bb.days_available("MSFT") == 55
    res = bb.check("MSFT", "09:35", 5000)
    assert res["baseline"] is not None
    assert abs(res["baseline"] - 5000.0) < 1e-6
    assert res["ratio_to_55bar_avg"] is not None


def test_e2e_total_volume_preferred_over_iex_volume(tmp_path):
    """When both fields exist, ``total_volume`` wins.

    A SIP-sourced bar typically has ``iex_volume`` near zero and
    ``total_volume`` at the real exchange-aggregate level. The
    baseline must reflect the SIP value, not the IEX slice.
    """
    today = date(2026, 5, 4)
    days = _trading_days_back(today, 55)

    for d in days:
        _write_v614_bar(
            tmp_path, d, "NVDA", "1000",
            total_volume=80_000.0,
            iex_volume=2_400.0,
        )

    bb = vb.VolumeBucketBaseline(base_dir=str(tmp_path))
    bb.refresh(today=today)

    assert bb.days_available("NVDA") == 55
    res = bb.check("NVDA", "10:00", 80_000)
    assert res["baseline"] is not None
    assert abs(res["baseline"] - 80_000.0) < 1e-6, (
        "baseline must come from total_volume, not iex_volume"
    )


def test_e2e_zero_volume_bars_skipped(tmp_path):
    """Zero-volume bars do not poison the rolling mean.

    Pre v6.14.0 the SIP path wrote ``iex_volume=0`` on most bars,
    which dragged the baseline toward zero. The v6.14.0 reader skips
    ``vf == 0.0`` entries entirely.

    Every day carries TWO bars at the same bucket: one real
    (200_000) and one zero. Days_available counts unique dates, so
    we still hit 55 trading days, but the rolling mean must reflect
    only the 200_000 readings.
    """
    today = date(2026, 5, 4)
    days = _trading_days_back(today, 55)

    for d in days:
        _write_v614_bar(tmp_path, d, "TSLA", "0945", total_volume=200_000.0)
        _write_v614_bar(tmp_path, d, "TSLA", "0945", total_volume=0.0)

    bb = vb.VolumeBucketBaseline(base_dir=str(tmp_path))
    bb.refresh(today=today)

    assert bb.days_available("TSLA") == 55
    res = bb.check("TSLA", "09:45", 200_000)
    assert res["baseline"] is not None
    assert abs(res["baseline"] - 200_000.0) < 1e-6, (
        f"zero-volume bars must be skipped; got baseline={res['baseline']}"
    )


def test_e2e_compute_et_bucket_rth_minutes():
    """``_compute_et_bucket`` returns canonical HHMM strings during
    RTH and None outside of it. The baseline reader's ``_bucket_key``
    must accept these without modification.
    """
    # 14:35 UTC on a weekday in May is 10:35 ET (EDT).
    ts = datetime(2026, 5, 4, 14, 35, 0, tzinfo=timezone.utc)
    bucket = _compute_et_bucket(ts)
    assert bucket == "1035", f"expected '1035', got {bucket!r}"
    assert vb._bucket_key(bucket) == "10:35"

    # ISO string acceptance.
    bucket_iso = _compute_et_bucket("2026-05-04T13:30:00+00:00")
    assert bucket_iso == "0930", f"expected '0930' for the open, got {bucket_iso!r}"

    # 09:29 ET is pre-RTH and must be rejected.
    pre = _compute_et_bucket(datetime(2026, 5, 4, 13, 29, 0, tzinfo=timezone.utc))
    assert pre is None, f"pre-RTH must return None, got {pre!r}"

    # 16:01 ET is post-close and must be rejected.
    post = _compute_et_bucket(datetime(2026, 5, 4, 20, 1, 0, tzinfo=timezone.utc))
    assert post is None, f"post-close must return None, got {post!r}"


def test_e2e_chain_uses_compute_et_bucket_path(tmp_path):
    """Mirror the ingest path: feed timestamps through
    ``_compute_et_bucket`` and write bars; baseline must populate.
    This is the failure mode v6.14.0 fixes (et_bucket was hardcoded
    None on the SIP REST + WS paths).
    """
    today = date(2026, 5, 4)
    days = _trading_days_back(today, 55)

    written = 0
    for d in days:
        # 14:35 UTC -> 09:35 ET (EDT) or 10:35 ET (EST). We pick a
        # UTC time that lands inside RTH on BOTH sides of the March
        # 8, 2026 DST transition: 15:00 UTC is 11:00 ET (EDT) or
        # 10:00 ET (EST). Both are RTH, both are valid HHMM keys.
        ts_utc = datetime(d.year, d.month, d.day, 15, 0, 0, tzinfo=timezone.utc)
        bucket = _compute_et_bucket(ts_utc)
        assert bucket is not None, (
            f"15:00 UTC on {d} must be RTH ET; _compute_et_bucket returned None"
        )
        _write_v614_bar(tmp_path, d, "SPY", bucket, total_volume=5_000_000.0)
        written += 1

    assert written == 55

    bb = vb.VolumeBucketBaseline(base_dir=str(tmp_path))
    bb.refresh(today=today)

    assert bb.days_available("SPY") == 55
    # The bucket key varies by DST so we look up via the same path
    # the bot's runtime would use: convert 15:00 UTC for `today` and
    # check that exact minute.
    today_bucket = _compute_et_bucket(
        datetime(today.year, today.month, today.day, 15, 0, 0, tzinfo=timezone.utc)
    )
    assert today_bucket is not None
    minute_str = f"{today_bucket[:2]}:{today_bucket[2:]}"
    res = bb.check("SPY", minute_str, 6_000_000)
    # Baseline came from a mix of EDT (11:00 ET) and EST (10:00 ET)
    # bars; the lookup will hit whichever bucket today resolves to,
    # which has SOME of the 55 days. The point of the test is that
    # the chain wires through end-to-end and produces a numeric
    # ratio_to_55bar_avg, not what its exact value is.
    assert res["baseline"] is not None
    assert res["ratio_to_55bar_avg"] is not None
    assert isinstance(res["ratio_to_55bar_avg"], (int, float))
