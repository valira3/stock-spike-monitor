"""v5.31.5 \u2014 unit tests for the per-stock local-weather override.

Covers four scenarios:

1. TSLA-style override \u2014 short-side QQQ is closed but TSLA's local
   structure is decisively long; the override returns open=True.
2. Trap case \u2014 price has crossed AVWAP but DI rejects, so structure
   alone shouldn't open the gate.
3. Veto case \u2014 DI confirms but price structure is wrong-way; the
   override stays closed.
4. Data-missing case \u2014 None inputs collapse to data_missing.

Plus a classifier sanity check for the 'flat' / 'up' / 'down' tags
the dashboard reads for the per-stock Weather column glyph.
"""

from __future__ import annotations

import pytest

from engine.local_weather import (
    SIDE_LONG,
    SIDE_SHORT,
    WEATHER_DOWN,
    WEATHER_FLAT,
    WEATHER_UP,
    classify_local_weather,
    evaluate_local_override,
)


# ---------------------------------------------------------------------------
# 1. TSLA-style override (the conversation that motivated v5.31.5)
# ---------------------------------------------------------------------------


def test_long_override_opens_when_local_decisively_long():
    """TSLA at 11:17 ET on May 1 2026: 1m closes above ORH, DI+ > DI-,
    last > AVWAP. Global QQQ might be closed for short, but the
    per-stock long override should open the gate.
    """
    res = evaluate_local_override(
        side=SIDE_LONG,
        ticker_5m_close=392.10,
        ticker_5m_ema9=388.40,  # close > EMA9 (long structure)
        ticker_last=392.07,
        ticker_avwap=389.20,    # last > AVWAP (long structure)
        di_plus_1m=28.85,
        di_minus_1m=16.95,
    )
    assert res["open"] is True
    assert res["reason"] == "open"
    assert res["weather_direction"] == WEATHER_UP


def test_short_override_opens_when_local_decisively_short():
    """Symmetric short case: close < EMA9, last < AVWAP, DI- > DI+."""
    res = evaluate_local_override(
        side=SIDE_SHORT,
        ticker_5m_close=199.10,
        ticker_5m_ema9=200.80,
        ticker_last=199.20,
        ticker_avwap=200.55,
        di_plus_1m=14.10,
        di_minus_1m=29.20,
    )
    assert res["open"] is True
    assert res["weather_direction"] == WEATHER_DOWN


def test_long_override_opens_when_only_one_structure_leg_aligns():
    """Loose rule: structure leg is `EMA9 OR AVWAP`, not both. If EMA9
    is misaligned but AVWAP is aligned and DI confirms, the override
    still fires.
    """
    res = evaluate_local_override(
        side=SIDE_LONG,
        ticker_5m_close=388.00,
        ticker_5m_ema9=389.00,  # close < EMA9 (would block long alone)
        ticker_last=392.00,
        ticker_avwap=390.00,    # last > AVWAP (carries long alone)
        di_plus_1m=27.00,
        di_minus_1m=15.00,
    )
    assert res["open"] is True


# ---------------------------------------------------------------------------
# 2. Trap: structure is past EMA9/AVWAP but DI rejects.
# ---------------------------------------------------------------------------


def test_long_override_blocked_when_di_rejects():
    """A bull-trap above AVWAP with DI- > DI+ should NOT open the
    long-side override even though the structure leg passes.
    """
    res = evaluate_local_override(
        side=SIDE_LONG,
        ticker_5m_close=392.10,
        ticker_5m_ema9=388.40,
        ticker_last=392.07,
        ticker_avwap=389.20,
        di_plus_1m=12.10,       # DI+ < DI- => reject
        di_minus_1m=28.30,
    )
    assert res["open"] is False
    assert res["reason"] == "di_misaligned"
    assert res["di_aligned"] is False
    # Direction reads as 'flat' \u2014 no side has both legs aligned.
    assert res["weather_direction"] == WEATHER_FLAT


# ---------------------------------------------------------------------------
# 3. Veto: DI confirms but price structure is wrong-way.
# ---------------------------------------------------------------------------


def test_long_override_blocked_when_structure_misaligned():
    """DI+ exceeds DI- but the ticker is below EMA9 AND below AVWAP \u2014
    the structure leg vetoes.
    """
    res = evaluate_local_override(
        side=SIDE_LONG,
        ticker_5m_close=386.00,
        ticker_5m_ema9=388.40,  # below EMA9
        ticker_last=386.10,
        ticker_avwap=389.20,    # below AVWAP
        di_plus_1m=28.85,
        di_minus_1m=16.95,
    )
    assert res["open"] is False
    assert res["reason"] == "structure_misaligned"
    assert res["ema9_aligned"] is False
    assert res["avwap_aligned"] is False


# ---------------------------------------------------------------------------
# 4. Data-missing collapses to closed.
# ---------------------------------------------------------------------------


def test_override_data_missing_when_di_none():
    res = evaluate_local_override(
        side=SIDE_LONG,
        ticker_5m_close=392.10,
        ticker_5m_ema9=388.40,
        ticker_last=392.07,
        ticker_avwap=389.20,
        di_plus_1m=None,
        di_minus_1m=16.95,
    )
    assert res["open"] is False
    assert res["reason"] == "data_missing"


def test_override_bad_side_returns_closed():
    res = evaluate_local_override(
        side="GAMMA",
        ticker_5m_close=1.0,
        ticker_5m_ema9=1.0,
        ticker_last=1.0,
        ticker_avwap=1.0,
        di_plus_1m=20.0,
        di_minus_1m=10.0,
    )
    assert res["open"] is False
    assert res["reason"].startswith("bad_side")


def test_override_with_partial_structure_data_still_evaluates():
    """If EMA9 is None but AVWAP confirms, the override still fires
    (loose 'OR' structure rule); the missing leg is treated as
    not-aligned, not a hard data-missing reject.
    """
    res = evaluate_local_override(
        side=SIDE_LONG,
        ticker_5m_close=None,
        ticker_5m_ema9=None,
        ticker_last=392.07,
        ticker_avwap=389.20,
        di_plus_1m=28.85,
        di_minus_1m=16.95,
    )
    assert res["open"] is True
    assert res["ema9_aligned"] is False
    assert res["avwap_aligned"] is True


# ---------------------------------------------------------------------------
# 5. Classifier (drives the per-stock Weather column glyph).
# ---------------------------------------------------------------------------


def test_classifier_returns_up_for_long_aligned_inputs():
    direction = classify_local_weather(
        ticker_5m_close=392.10,
        ticker_5m_ema9=388.40,
        ticker_last=392.07,
        ticker_avwap=389.20,
        di_plus_1m=28.85,
        di_minus_1m=16.95,
    )
    assert direction == WEATHER_UP


def test_classifier_returns_down_for_short_aligned_inputs():
    direction = classify_local_weather(
        ticker_5m_close=199.10,
        ticker_5m_ema9=200.80,
        ticker_last=199.20,
        ticker_avwap=200.55,
        di_plus_1m=14.10,
        di_minus_1m=29.20,
    )
    assert direction == WEATHER_DOWN


def test_classifier_returns_flat_when_di_disagrees_with_structure():
    direction = classify_local_weather(
        ticker_5m_close=392.10,
        ticker_5m_ema9=388.40,
        ticker_last=392.07,
        ticker_avwap=389.20,
        di_plus_1m=12.10,
        di_minus_1m=28.30,
    )
    assert direction == WEATHER_FLAT


def test_classifier_returns_flat_when_inputs_missing():
    direction = classify_local_weather(
        ticker_5m_close=None,
        ticker_5m_ema9=None,
        ticker_last=None,
        ticker_avwap=None,
        di_plus_1m=None,
        di_minus_1m=None,
    )
    assert direction == WEATHER_FLAT


# ---------------------------------------------------------------------------
# 6. Both-sides classifier: when structure & DI confirm BOTH directions
#    (which shouldn't happen with normal data), classifier returns flat.
#    Engineered case: tick where DI+ > DI- and DI- > DI+ can't both be
#    true, so we contrive structure inputs that satisfy both directions.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ema9, avwap, last_close, last",
    [
        # close == EMA9 and last == AVWAP \u2014 neither strict inequality
        # can fire, so both legs are False for both sides.
        (100.0, 100.0, 100.0, 100.0),
    ],
)
def test_classifier_returns_flat_on_neutral_structure(ema9, avwap, last_close, last):
    direction = classify_local_weather(
        ticker_5m_close=last_close,
        ticker_5m_ema9=ema9,
        ticker_last=last,
        ticker_avwap=avwap,
        di_plus_1m=20.0,
        di_minus_1m=10.0,
    )
    assert direction == WEATHER_FLAT
