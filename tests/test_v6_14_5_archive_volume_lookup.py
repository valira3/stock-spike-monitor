"""v6.14.5 -- regression tests for `current_1m_vol` falling back to the
bar archive when the legacy WS consumer is missing (which is the live
state since v5.14.0).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import bot_version
import v5_10_6_snapshot as snap


def test_bot_version_is_6_14_5_or_newer():
    parts = [int(p) for p in bot_version.BOT_VERSION.split(".")]
    assert parts >= [6, 14, 5]


def _write_bar(day_dir: Path, ticker: str, total_volume):
    day_dir.mkdir(parents=True, exist_ok=True)
    bar = {
        "ts": "2026-05-04T20:30:00+00:00",
        "et_bucket": None,
        "open": 100.0,
        "high": 100.5,
        "low": 99.5,
        "close": 100.2,
        "total_volume": total_volume,
        "iex_volume": None,
        "feed_source": "sip",
    }
    fp = day_dir / f"{ticker}.jsonl"
    with open(fp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(bar) + "\n")


def test_archive_lookup_returns_total_volume(tmp_path, monkeypatch):
    from datetime import datetime
    today = datetime.utcnow().strftime("%Y-%m-%d")
    day_dir = tmp_path / "bars" / today
    _write_bar(day_dir, "AAPL", 12345.0)
    _write_bar(day_dir, "MSFT", 67890)

    monkeypatch.setenv("BAR_ARCHIVE_BASE", str(tmp_path / "bars"))
    lookup = snap._build_archive_volume_lookup()
    assert lookup("AAPL") == 12345
    assert lookup("MSFT") == 67890
    assert lookup("aapl") == 12345  # case-insensitive


def test_archive_lookup_returns_zero_for_missing_ticker(tmp_path, monkeypatch):
    monkeypatch.setenv("BAR_ARCHIVE_BASE", str(tmp_path / "bars"))
    lookup = snap._build_archive_volume_lookup()
    assert lookup("NOPE") == 0


def test_archive_lookup_returns_zero_for_missing_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("BAR_ARCHIVE_BASE", str(tmp_path / "does_not_exist"))
    lookup = snap._build_archive_volume_lookup()
    assert lookup("AAPL") == 0


def test_archive_lookup_uses_last_line(tmp_path, monkeypatch):
    from datetime import datetime
    today = datetime.utcnow().strftime("%Y-%m-%d")
    day_dir = tmp_path / "bars" / today
    day_dir.mkdir(parents=True, exist_ok=True)
    fp = day_dir / "AAPL.jsonl"
    with open(fp, "w", encoding="utf-8") as fh:
        for i, vol in enumerate([100, 200, 300, 999]):
            fh.write(json.dumps({"ts": f"2026-05-04T15:{i:02d}:00Z", "total_volume": vol}) + "\n")

    monkeypatch.setenv("BAR_ARCHIVE_BASE", str(tmp_path / "bars"))
    lookup = snap._build_archive_volume_lookup()
    assert lookup("AAPL") == 999


def test_archive_lookup_falls_back_to_iex_volume(tmp_path, monkeypatch):
    """Legacy bars (pre v6.14.0) carry only iex_volume."""
    from datetime import datetime
    today = datetime.utcnow().strftime("%Y-%m-%d")
    day_dir = tmp_path / "bars" / today
    day_dir.mkdir(parents=True, exist_ok=True)
    fp = day_dir / "AAPL.jsonl"
    with open(fp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "2026-05-04T15:00:00Z", "iex_volume": 555}) + "\n")
    monkeypatch.setenv("BAR_ARCHIVE_BASE", str(tmp_path / "bars"))
    lookup = snap._build_archive_volume_lookup()
    assert lookup("AAPL") == 555


def test_archive_lookup_caches_per_ticker(tmp_path, monkeypatch):
    """Calling the lookup twice for the same ticker should not re-read."""
    from datetime import datetime
    today = datetime.utcnow().strftime("%Y-%m-%d")
    day_dir = tmp_path / "bars" / today
    _write_bar(day_dir, "AAPL", 4242)

    monkeypatch.setenv("BAR_ARCHIVE_BASE", str(tmp_path / "bars"))
    lookup = snap._build_archive_volume_lookup()
    assert lookup("AAPL") == 4242
    # Mutate the file; cache should still return the original.
    fp = day_dir / "AAPL.jsonl"
    fp.write_text(json.dumps({"ts": "x", "total_volume": 1}) + "\n")
    assert lookup("AAPL") == 4242


def test_vol_bucket_per_ticker_uses_archive_when_consumer_missing(tmp_path, monkeypatch):
    """End-to-end: with no _ws_consumer on m, current_1m_vol comes from archive."""
    from datetime import datetime
    today = datetime.utcnow().strftime("%Y-%m-%d")
    day_dir = tmp_path / "bars" / today
    _write_bar(day_dir, "AAPL", 4321)
    monkeypatch.setenv("BAR_ARCHIVE_BASE", str(tmp_path / "bars"))

    class FakeM:
        pass
    fake_m = FakeM()  # no _ws_consumer attr
    out = snap._vol_bucket_per_ticker(fake_m, ["AAPL"], "1430", "1429")
    assert out["AAPL"]["current_1m_vol"] == 4321
