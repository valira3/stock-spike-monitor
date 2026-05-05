"""v6.15.3 \u2014 SPY regime cold-start backfill from the bar archive.

Why this exists: the in-memory `_SPY_REGIME` singleton is wiped on
every Railway deploy / pod restart. The class only captures
`spy_open_930` during the `hh==9 mm==30` minute window, which means a
mid-session deploy (e.g. shipping a hotfix at 12:01 CDT) permanently
loses today's anchor and leaves `regime=None` for the rest of the day.
The 2026-05-05 v6.15.0 / v6.15.1 / v6.15.2 deploy storm hit exactly
this pathology.

`SpyRegime.backfill_from_bars(now_et, bars_path=...)` rebuilds the
anchors from `/data/bars/<YYYY-MM-DD>/SPY.jsonl` (which IS persisted
across deploys), then runs `_classify` so the regime is correct
immediately after process startup.

Cases covered:
  1. Full backfill: both 0930 and 1000 bars present \u2192 regime classified
  2. Only 0930 present \u2192 partial state, no classification yet
  3. Only 1000 present \u2192 partial state, no classification yet
  4. Neither bucket present \u2192 no-op, regime stays None
  5. Already classified \u2192 no-op (idempotent)
  6. Both anchors already set \u2192 no-op (don't overwrite live anchors)
  7. Missing file \u2192 no-op, no exception
  8. Malformed JSON line in file \u2192 skipped, valid lines still parsed
  9. Bucket close=0 / negative / null \u2192 skipped
 10. Backfill picks the FIRST 0930 / 1000 bar (idempotent order)
 11. Real-world band sanity: 0.0914% return classifies as C
 12. Default path resolves to BAR_ARCHIVE_BASE/<date>/SPY.jsonl
"""
from __future__ import annotations

import datetime
import json
import os
import sys

import pytest


@pytest.fixture
def sr_module(monkeypatch):
    """Reload spy_regime fresh for each test so module-level env knobs
    are re-read."""
    if "spy_regime" in sys.modules:
        del sys.modules["spy_regime"]
    import spy_regime
    return spy_regime


def _bar(et_bucket, close, ts="2026-05-05T13:30:00+00:00"):
    return {
        "ts": ts,
        "et_bucket": et_bucket,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "total_volume": 100000,
        "iex_volume": None,
        "feed_source": "sip",
    }


def _write_bars(path, bars):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for b in bars:
            f.write(json.dumps(b) + "\n")


# Use the real 2026-05-05 SPY closes from production: 09:30 close=721.81,
# 10:00 close=722.47 \u2192 ret = +0.0914% \u2192 regime C (flat).
NOW_ET = datetime.datetime(2026, 5, 5, 12, 30)  # 12:30 ET, well after 10:00


def test_backfill_full_classifies_regime(sr_module, tmp_path):
    sr = sr_module.SpyRegime()
    bars_path = tmp_path / "2026-05-05" / "SPY.jsonl"
    _write_bars(str(bars_path), [
        _bar("0400", 719.80),
        _bar("0930", 721.81),
        _bar("0935", 721.95),
        _bar("1000", 722.47),
        _bar("1030", 722.10),
    ])
    ok = sr.backfill_from_bars(NOW_ET, bars_path=str(bars_path))
    assert ok is True
    assert sr.spy_open_930 == 721.81
    assert sr.spy_close_1000 == 722.47
    # +0.0914% \u2192 inside [-0.15, +0.15] \u2192 regime C.
    assert sr.regime == "C"
    assert sr.spy_30m_return_pct == pytest.approx(0.0914, abs=0.001)


def test_backfill_only_0930_partial(sr_module, tmp_path):
    sr = sr_module.SpyRegime()
    bars_path = tmp_path / "SPY.jsonl"
    _write_bars(str(bars_path), [
        _bar("0930", 721.81),
        _bar("1030", 722.10),
    ])
    ok = sr.backfill_from_bars(NOW_ET, bars_path=str(bars_path))
    assert ok is False
    assert sr.spy_open_930 == 721.81
    assert sr.spy_close_1000 is None
    assert sr.regime is None


def test_backfill_only_1000_partial(sr_module, tmp_path):
    sr = sr_module.SpyRegime()
    bars_path = tmp_path / "SPY.jsonl"
    _write_bars(str(bars_path), [
        _bar("1000", 722.47),
        _bar("1030", 722.10),
    ])
    ok = sr.backfill_from_bars(NOW_ET, bars_path=str(bars_path))
    assert ok is False
    assert sr.spy_open_930 is None
    assert sr.spy_close_1000 == 722.47
    assert sr.regime is None


def test_backfill_no_buckets_in_file(sr_module, tmp_path):
    sr = sr_module.SpyRegime()
    bars_path = tmp_path / "SPY.jsonl"
    _write_bars(str(bars_path), [
        _bar("0400", 719.80),
        _bar("0500", 720.00),
        _bar("1500", 723.00),
    ])
    ok = sr.backfill_from_bars(NOW_ET, bars_path=str(bars_path))
    assert ok is False
    assert sr.spy_open_930 is None
    assert sr.spy_close_1000 is None
    assert sr.regime is None


def test_backfill_idempotent_when_already_classified(sr_module, tmp_path):
    """If regime is already set, backfill must be a no-op even if the
    file would yield different anchors."""
    sr = sr_module.SpyRegime()
    sr.spy_open_930 = 700.00
    sr.spy_close_1000 = 705.00
    sr._classify(NOW_ET)  # locks regime in
    locked_regime = sr.regime
    assert locked_regime is not None

    bars_path = tmp_path / "SPY.jsonl"
    _write_bars(str(bars_path), [_bar("0930", 800.0), _bar("1000", 810.0)])
    ok = sr.backfill_from_bars(NOW_ET, bars_path=str(bars_path))
    assert ok is False
    # Anchors and regime untouched.
    assert sr.spy_open_930 == 700.00
    assert sr.spy_close_1000 == 705.00
    assert sr.regime == locked_regime


def test_backfill_skips_when_both_anchors_already_set(sr_module, tmp_path):
    """If live ticks already populated both anchors but _classify hasn't
    run yet for some reason, backfill should not stomp them."""
    sr = sr_module.SpyRegime()
    sr.spy_open_930 = 721.81
    sr.spy_close_1000 = 722.47
    # regime is still None (hypothetical race \u2014 _classify pending)
    bars_path = tmp_path / "SPY.jsonl"
    _write_bars(str(bars_path), [_bar("0930", 999.0), _bar("1000", 999.0)])
    ok = sr.backfill_from_bars(NOW_ET, bars_path=str(bars_path))
    assert ok is False
    assert sr.spy_open_930 == 721.81
    assert sr.spy_close_1000 == 722.47


def test_backfill_missing_file_no_op(sr_module, tmp_path):
    sr = sr_module.SpyRegime()
    bars_path = tmp_path / "does_not_exist" / "SPY.jsonl"
    ok = sr.backfill_from_bars(NOW_ET, bars_path=str(bars_path))
    assert ok is False
    assert sr.spy_open_930 is None
    assert sr.spy_close_1000 is None
    assert sr.regime is None


def test_backfill_skips_malformed_lines(sr_module, tmp_path):
    sr = sr_module.SpyRegime()
    bars_path = tmp_path / "SPY.jsonl"
    os.makedirs(os.path.dirname(str(bars_path)), exist_ok=True)
    with open(str(bars_path), "w", encoding="utf-8") as f:
        f.write("not-json-at-all\n")
        f.write("\n")
        f.write(json.dumps(_bar("0930", 721.81)) + "\n")
        f.write("{broken: json,\n")
        f.write(json.dumps(_bar("1000", 722.47)) + "\n")
    ok = sr.backfill_from_bars(NOW_ET, bars_path=str(bars_path))
    assert ok is True
    assert sr.spy_open_930 == 721.81
    assert sr.spy_close_1000 == 722.47
    assert sr.regime == "C"


def test_backfill_skips_zero_close(sr_module, tmp_path):
    """A bar with close=0 (broker bug / corrupt write) must be skipped
    so the second valid bar wins."""
    sr = sr_module.SpyRegime()
    bars_path = tmp_path / "SPY.jsonl"
    _write_bars(str(bars_path), [
        _bar("0930", 0.0),
        _bar("0930", 721.81),
        _bar("1000", 722.47),
    ])
    ok = sr.backfill_from_bars(NOW_ET, bars_path=str(bars_path))
    assert ok is True
    assert sr.spy_open_930 == 721.81


def test_backfill_skips_null_close(sr_module, tmp_path):
    sr = sr_module.SpyRegime()
    bars_path = tmp_path / "SPY.jsonl"
    bar_null = _bar("0930", 721.81)
    bar_null["close"] = None
    _write_bars(str(bars_path), [
        bar_null,
        _bar("0930", 721.81),
        _bar("1000", 722.47),
    ])
    ok = sr.backfill_from_bars(NOW_ET, bars_path=str(bars_path))
    assert ok is True
    assert sr.spy_open_930 == 721.81
    assert sr.spy_close_1000 == 722.47


def test_backfill_picks_first_bucket_match(sr_module, tmp_path):
    """If the file has multiple bars stamped 0930 (shouldn't happen but
    the writer is append-only), backfill takes the FIRST one for
    determinism."""
    sr = sr_module.SpyRegime()
    bars_path = tmp_path / "SPY.jsonl"
    _write_bars(str(bars_path), [
        _bar("0930", 721.81),
        _bar("0930", 999.99),  # later duplicate \u2014 ignored
        _bar("1000", 722.47),
        _bar("1000", 888.88),  # later duplicate \u2014 ignored
    ])
    sr.backfill_from_bars(NOW_ET, bars_path=str(bars_path))
    assert sr.spy_open_930 == 721.81
    assert sr.spy_close_1000 == 722.47


def test_backfill_real_2026_05_05_classifies_C(sr_module, tmp_path):
    """Real-world sanity: SPY 09:30=721.81 / 10:00=722.47 \u2192 +0.0914%
    return \u2192 inside [-0.15, +0.15] \u2192 regime C (flat).

    This is the actual 2026-05-05 production data that triggered the
    fix: today's regime SHOULD have been C (flat) but stayed None
    because of the deploy storm.
    """
    sr = sr_module.SpyRegime()
    bars_path = tmp_path / "SPY.jsonl"
    _write_bars(str(bars_path), [_bar("0930", 721.81), _bar("1000", 722.47)])
    sr.backfill_from_bars(NOW_ET, bars_path=str(bars_path))
    assert sr.regime == "C"
    assert sr.is_regime_b() is False  # so short-amp gate stays disarmed


def test_backfill_default_path_uses_archive_base(sr_module, tmp_path, monkeypatch):
    """When bars_path is omitted, the helper resolves the path off
    BAR_ARCHIVE_BASE so production picks up /data/bars/<date>/SPY.jsonl.
    """
    # Reload module with archive base pointing at our temp dir.
    monkeypatch.setenv("BAR_ARCHIVE_BASE", str(tmp_path))
    if "spy_regime" in sys.modules:
        del sys.modules["spy_regime"]
    import spy_regime as sr_mod_fresh

    bars_path = tmp_path / "2026-05-05" / "SPY.jsonl"
    _write_bars(str(bars_path), [_bar("0930", 721.81), _bar("1000", 722.47)])

    sr = sr_mod_fresh.SpyRegime()
    ok = sr.backfill_from_bars(NOW_ET)  # no explicit bars_path
    assert ok is True
    assert sr.regime == "C"
