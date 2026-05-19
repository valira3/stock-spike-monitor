"""Unit tests for orb.premarket_scanner."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orb.premarket_scanner import (
    PREMARKET_OPEN_BUCKET,
    RTH_OPEN_BUCKET,
    scan_day,
    scan_universe_to_dict,
)


def _make_bar(ts_iso: str, et_bucket: str, close: float, volume: float,
              high: float | None = None, low: float | None = None,
              open_: float | None = None) -> dict:
    return {
        "ts": ts_iso,
        "et_bucket": et_bucket,
        "open": open_ if open_ is not None else close,
        "high": high if high is not None else close,
        "low": low if low is not None else close,
        "close": close,
        "total_volume": volume,
        "iex_volume": None,
        "iex_sip_ratio_used": None,
        "bid": None,
        "ask": None,
        "last_trade_price": None,
        "trade_count": 1.0,
        "bar_vwap": close,
        "feed_source": "sip",
    }


def _write_bars(root: Path, date_str: str, ticker: str, bars: list[dict]) -> None:
    day = root / date_str
    day.mkdir(parents=True, exist_ok=True)
    (day / f"{ticker}.jsonl").write_text("\n".join(json.dumps(b) for b in bars) + "\n")


def _build_corpus(tmp: Path) -> None:
    """Fixture: 3 tickers × 2 days. Prior day = 2026-05-14, scan day = 2026-05-15.

    Construction:
      - GAPUP:   prior close 100, premarket 04:00→09:29 climbs 100→106 (gap +6%).
      - GAPDOWN: prior close 200, premarket 04:00→09:29 drops 200→194 (gap −3%).
      - DULL:   prior close 50, premarket flat at 50.00 (gap 0%, range 0).
    """
    # Prior day RTH closes (last RTH bar)
    for tk, close in [("GAPUP", 100.0), ("GAPDOWN", 200.0), ("DULL", 50.0)]:
        bars = [
            _make_bar("2026-05-14T19:59:00+00:00", "1559", close, 100_000.0)
        ]
        _write_bars(tmp, "2026-05-14", tk, bars)

    # Scan-day premarket (300 bars; enough to clear the min_pm_bars=10 default)
    def _pm_path(start: float, end: float, n: int = 330) -> list[dict]:
        bars = []
        bucket_h = 4
        bucket_m = 0
        for i in range(n):
            frac = i / max(n - 1, 1)
            close = start + (end - start) * frac
            bars.append(
                _make_bar(
                    f"2026-05-15T{bucket_h:02d}:{bucket_m:02d}:00-04:00",
                    f"{bucket_h:02d}{bucket_m:02d}",
                    close,
                    50_000.0,
                    high=close + 0.05,
                    low=close - 0.05,
                    open_=close - 0.01,
                )
            )
            bucket_m += 1
            if bucket_m == 60:
                bucket_m = 0
                bucket_h += 1
        return bars

    _write_bars(tmp, "2026-05-15", "GAPUP", _pm_path(100.0, 106.0))
    _write_bars(tmp, "2026-05-15", "GAPDOWN", _pm_path(200.0, 194.0))
    _write_bars(tmp, "2026-05-15", "DULL", _pm_path(50.0, 50.0))


def test_gap_signal_ranks_largest_absolute_gap_first(tmp_path: Path) -> None:
    _build_corpus(tmp_path)
    out = scan_day(tmp_path, "2026-05-15", ["GAPUP", "GAPDOWN", "DULL"],
                   signal="gap", top_k=3, min_dollar_volume=0)
    assert [r.ticker for r in out] == ["GAPUP", "GAPDOWN", "DULL"]
    assert abs(out[0].gap_pct - 0.06) < 1e-4
    assert abs(out[1].gap_pct + 0.03) < 1e-4


def test_volume_signal_ignored_when_volume_equal(tmp_path: Path) -> None:
    _build_corpus(tmp_path)
    out = scan_day(tmp_path, "2026-05-15", ["GAPUP", "GAPDOWN", "DULL"],
                   signal="volume", top_k=3, min_dollar_volume=0)
    # All three have identical bar volumes; volume signal differentiates by
    # close price × volume (higher price = higher dollar volume).
    assert out[0].ticker == "GAPDOWN"  # $200 × 50k vol
    assert out[-1].ticker == "DULL"    # $50 × 50k vol


def test_range_signal_picks_widest_pm_range(tmp_path: Path) -> None:
    _build_corpus(tmp_path)
    out = scan_day(tmp_path, "2026-05-15", ["GAPUP", "GAPDOWN", "DULL"],
                   signal="range", top_k=3, min_dollar_volume=0)
    # GAPUP travels 100→106 (range = ~6%), GAPDOWN 200→194 (~3%), DULL 0%.
    assert out[0].ticker == "GAPUP"
    assert out[-1].ticker == "DULL"


def test_composite_signal_blends_three(tmp_path: Path) -> None:
    _build_corpus(tmp_path)
    out = scan_day(tmp_path, "2026-05-15", ["GAPUP", "GAPDOWN", "DULL"],
                   signal="composite", top_k=3, min_dollar_volume=0)
    # Composite is z-score sum of (|gap|, log dollar vol, range%).
    # GAPUP wins on gap + range; GAPDOWN wins on dollar volume; DULL loses everywhere.
    assert out[-1].ticker == "DULL"


def test_topk_truncates(tmp_path: Path) -> None:
    _build_corpus(tmp_path)
    out = scan_day(tmp_path, "2026-05-15", ["GAPUP", "GAPDOWN", "DULL"],
                   signal="composite", top_k=2, min_dollar_volume=0)
    assert len(out) == 2


def test_missing_ticker_silently_dropped(tmp_path: Path) -> None:
    _build_corpus(tmp_path)
    out = scan_day(tmp_path, "2026-05-15", ["GAPUP", "MISSING"],
                   signal="gap", top_k=10, min_dollar_volume=0)
    assert [r.ticker for r in out] == ["GAPUP"]


def test_min_dollar_volume_filter(tmp_path: Path) -> None:
    _build_corpus(tmp_path)
    # DULL's PM dollar volume = $50 × 50k × 330 bars ≈ $825M. Set filter higher.
    # GAPUP's PM dollar volume = ~$103 × 50k × 330 ≈ $1.7B; should pass.
    # DULL would also pass at $825M; bump filter to $1B to drop DULL only.
    out = scan_day(tmp_path, "2026-05-15", ["GAPUP", "GAPDOWN", "DULL"],
                   signal="gap", top_k=10, min_dollar_volume=1_000_000_000.0)
    assert "DULL" not in {r.ticker for r in out}


def test_min_pm_bars_filter(tmp_path: Path) -> None:
    # 9-bar premarket: below default min_pm_bars=10, should be dropped.
    bars = [
        {
            "ts": f"2026-05-15T{4 + i//60:02d}:{i%60:02d}:00-04:00",
            "et_bucket": f"{4 + i//60:02d}{i%60:02d}",
            "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0,
            "total_volume": 10000.0, "iex_volume": None, "iex_sip_ratio_used": None,
            "bid": None, "ask": None, "last_trade_price": None,
            "trade_count": 1.0, "bar_vwap": 100.0, "feed_source": "sip",
        }
        for i in range(9)
    ]
    import json as _j
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "2026-05-15").mkdir()
        (root / "2026-05-15" / "THIN.jsonl").write_text(
            "\n".join(_j.dumps(b) for b in bars) + "\n"
        )
        (root / "2026-05-14").mkdir()
        (root / "2026-05-14" / "THIN.jsonl").write_text(
            _j.dumps({
                "ts": "2026-05-14T19:59:00+00:00", "et_bucket": "1559",
                "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
                "total_volume": 10000.0, "iex_volume": None,
                "iex_sip_ratio_used": None, "bid": None, "ask": None,
                "last_trade_price": None, "trade_count": 1.0,
                "bar_vwap": 100.0, "feed_source": "sip",
            }) + "\n"
        )
        out = scan_day(root, "2026-05-15", ["THIN"], signal="gap", top_k=10,
                       min_dollar_volume=0)
        assert out == []


def test_to_dict_wrapper(tmp_path: Path) -> None:
    _build_corpus(tmp_path)
    d = scan_universe_to_dict(tmp_path, "2026-05-15", ["GAPUP", "GAPDOWN"],
                              signal="gap", top_k=2, min_dollar_volume=0)
    assert d["date"] == "2026-05-15"
    assert d["signal"] == "gap"
    assert d["top_k"] == 2
    assert d["n_picks"] == 2
    assert d["picks"][0]["ticker"] == "GAPUP"
    # gap_pct in the dict is multiplied by 100 for human readability
    assert abs(d["picks"][0]["gap_pct"] - 6.0) < 1e-3


def test_unknown_signal_raises(tmp_path: Path) -> None:
    _build_corpus(tmp_path)
    with pytest.raises(ValueError):
        scan_day(tmp_path, "2026-05-15", ["GAPUP"], signal="momentum")


def test_constants() -> None:
    """Sanity: the bucket constants haven't drifted from the on-disk schema."""
    assert PREMARKET_OPEN_BUCKET == "0400"
    assert RTH_OPEN_BUCKET == "0930"
