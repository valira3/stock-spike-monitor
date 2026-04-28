"""v5.9.0 \u2014 unit tests for qqq_regime.QQQRegime (Permission Gate G1 source).

Covers:
  1. warmup behavior \u2014 compass is None until 9 closed bars accumulate
  2. EMA correctness \u2014 standard SMA-seeded EMA recurrence math
  3. compass UP / DOWN / FLAT \u2014 strict comparators
  4. seed() replays a chronological list of pre-market closes
  5. seed() rejects unknown source labels
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import qqq_regime  # noqa: E402


def _hand_ema(closes, period):
    """Reference: SMA-seeded EMA used for cross-checking the module."""
    if len(closes) < period:
        return None
    sma = sum(closes[:period]) / float(period)
    ema = sma
    k = 2.0 / (period + 1.0)
    for c in closes[period:]:
        ema = (c - ema) * k + ema
    return ema


def test_warmup_compass_is_none_until_9_bars():
    qr = qqq_regime.QQQRegime()
    assert qr.current_compass() is None
    for i, c in enumerate([100.0, 100.5, 101.0, 101.5, 102.0, 102.5, 103.0, 103.5]):
        qr.update(c)
        assert qr.current_compass() is None, f"bar {i + 1}: compass should be None"
    qr.update(104.0)  # 9th bar seeds EMA9
    assert qr.current_compass() is not None
    assert qr.bars_seen == 9


def test_compass_up_when_ema3_strictly_above_ema9():
    closes = [100.0, 100.5, 101.0, 101.5, 102.0, 102.5, 103.0, 103.5, 104.0]
    qr = qqq_regime.QQQRegime()
    for c in closes:
        qr.update(c)
    assert qr.current_compass() == qqq_regime.COMPASS_UP
    assert qr.ema3 == pytest.approx(_hand_ema(closes, 3), rel=1e-9)
    assert qr.ema9 == pytest.approx(_hand_ema(closes, 9), rel=1e-9)


def test_compass_down_when_ema3_strictly_below_ema9():
    closes = [104.0, 103.5, 103.0, 102.5, 102.0, 101.5, 101.0, 100.5, 100.0]
    qr = qqq_regime.QQQRegime()
    for c in closes:
        qr.update(c)
    assert qr.current_compass() == qqq_regime.COMPASS_DOWN


def test_compass_flat_when_ema3_equals_ema9():
    qr = qqq_regime.QQQRegime()
    for _ in range(9):
        qr.update(100.0)
    assert qr.ema3 == pytest.approx(100.0)
    assert qr.ema9 == pytest.approx(100.0)
    assert qr.current_compass() == qqq_regime.COMPASS_FLAT


def test_seed_replays_premarket_closes_and_records_source():
    closes = [200.0 + 0.1 * i for i in range(12)]
    qr = qqq_regime.QQQRegime()
    n = qr.seed(closes, source="archive")
    assert n == 12
    assert qr.bars_seen == 12
    assert qr.seed_source == "archive"
    assert qr.seed_bar_count == 12
    assert qr.ema3 == pytest.approx(_hand_ema(closes, 3), rel=1e-9)
    assert qr.ema9 == pytest.approx(_hand_ema(closes, 9), rel=1e-9)


def test_seed_rejects_unknown_source():
    qr = qqq_regime.QQQRegime()
    with pytest.raises(ValueError):
        qr.seed([100.0] * 9, source="bogus")


def test_update_skips_none_close():
    qr = qqq_regime.QQQRegime()
    for c in [100.0, 100.5, 101.0]:
        qr.update(c)
    assert qr.bars_seen == 3
    qr.update(None)
    assert qr.bars_seen == 3


def test_seed_then_live_update_continuity():
    """Pre-market seed + a live closed bar should produce the same EMAs as
    feeding all closes through update() in one stream."""
    seed_closes = [100.0, 100.2, 100.4, 100.6, 100.8, 101.0, 101.2, 101.4, 101.6]
    live_close = 101.8
    qr_a = qqq_regime.QQQRegime()
    qr_a.seed(seed_closes, source="archive")
    qr_a.update(live_close)

    qr_b = qqq_regime.QQQRegime()
    for c in seed_closes + [live_close]:
        qr_b.update(c)

    assert qr_a.ema3 == pytest.approx(qr_b.ema3, rel=1e-12)
    assert qr_a.ema9 == pytest.approx(qr_b.ema9, rel=1e-12)
    assert qr_a.current_compass() == qr_b.current_compass()
