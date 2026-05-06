"""v6.16.0 \u2014 unit tests for earnings_watcher.sizing.

Validates:
  - dmi_conviction_multiplier piecewise curve
  - dmi_sized_notional portfolio scaling, conviction multiplier, exposure cap,
    minimal-room skip, no-equity fallback, small-portfolio scaling.
"""
from __future__ import annotations

import pytest

from earnings_watcher.sizing import (
    DMI_BASE_NOTIONAL_PCT,
    DMI_CONVICTION_SIZE_MAX,
    DMI_MAX_PORTFOLIO_EXPOSURE_PCT,
    DMI_MAX_POSITION_PCT,
    DMI_MIN_POSITION_PCT,
    dmi_conviction_multiplier,
    dmi_sized_notional,
)


# Constants sanity ----------------------------------------------------------

def test_constants_consistent():
    """Per-position cap should equal base x max multiplier."""
    assert DMI_MAX_POSITION_PCT == pytest.approx(
        DMI_BASE_NOTIONAL_PCT * DMI_CONVICTION_SIZE_MAX
    )
    assert 0 < DMI_MIN_POSITION_PCT < DMI_BASE_NOTIONAL_PCT
    assert DMI_MAX_PORTFOLIO_EXPOSURE_PCT > DMI_MAX_POSITION_PCT


# Conviction multiplier curve ----------------------------------------------

def test_conviction_multiplier_piecewise():
    assert dmi_conviction_multiplier(0) == 1.0
    assert dmi_conviction_multiplier(2.99) == 1.0
    assert dmi_conviction_multiplier(3) == 1.0
    assert dmi_conviction_multiplier(6) == 2.0
    assert dmi_conviction_multiplier(8) == 2.0
    assert dmi_conviction_multiplier(10) == 2.5
    assert dmi_conviction_multiplier(12) == 3.0
    assert dmi_conviction_multiplier(20) == 3.0


# Notional sizing ----------------------------------------------------------

def test_below_threshold_no_trade():
    """conv < 3 -> 1x base, $10k on $100k account."""
    n, r = dmi_sized_notional(100_000, conviction=2.5, open_dmi_exposure=0)
    assert n == 10_000.0
    assert r == "ok"


def test_cap_3x_at_high_conviction():
    """conv >= 12 -> 3x base, $30k on $100k account."""
    n, r = dmi_sized_notional(100_000, conviction=15, open_dmi_exposure=0)
    assert n == 30_000.0
    assert r == "ok"


def test_proportional_scale_at_50pct_exposure():
    """conv=10 (2.5x = $25k) but open=$40k caps at 50% -> scale to $10k."""
    n, r = dmi_sized_notional(100_000, conviction=10, open_dmi_exposure=40_000)
    assert n == 10_000.0
    assert r == "exposure_cap"


def test_skip_when_minimal_room():
    """Open exposure leaves <2% room -> skip with reason exposure_minimal."""
    n, r = dmi_sized_notional(100_000, conviction=10, open_dmi_exposure=49_000)
    assert n == 0.0
    assert r == "exposure_minimal"


def test_no_equity_fallback():
    """None or zero equity -> 0 notional, no_equity reason."""
    n, r = dmi_sized_notional(None, conviction=10, open_dmi_exposure=0)
    assert n == 0.0
    assert r == "no_equity"
    n, r = dmi_sized_notional(0, conviction=10, open_dmi_exposure=0)
    assert n == 0.0
    assert r == "no_equity"


def test_small_portfolio_scales_down():
    """$25k account at conv=12 -> 3x base = 30% = $7,500."""
    n, r = dmi_sized_notional(25_000, conviction=12, open_dmi_exposure=0)
    assert n == 7_500.0
    assert r == "ok"
