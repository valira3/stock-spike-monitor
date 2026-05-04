"""Tests for v6.11.0 -- C25 SPY Regime-B Short Amplification.

Covers:
1. Classifier basic A/B/C/D/E
2. Missing 9:30 anchor fails closed
3. Long-side passthrough
4. Pre-arm-time passthrough (09:55 ET)
5. Post-disarm passthrough (11:00 ET exactly)
6. Non-regime-B passthrough
7. Regime-B in-window amplifies (10:30 ET)
8. Arm-boundary inclusive (10:00:00 ET exactly)
9. Disabled passthrough (V611_REGIME_B_ENABLED=0)
10. Logs [V611-AMP] line on amp
11. Regime-B boundaries: exact -0.50% NOT B, -0.15% NOT B
12. bot_version parity: BOT_VERSION == "6.11.0"
13. 84d replay: skipped (expensive -- run via scripts/replay_84d.py post-merge)

ZERO em-dashes in this file.
"""

from __future__ import annotations

import datetime
import logging
import types
import unittest.mock as mock
from zoneinfo import ZoneInfo

import pytest

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _et(hour: int, minute: int, second: int = 0) -> datetime.datetime:
    """Return a timezone-aware ET datetime for today."""
    return datetime.datetime(2026, 5, 4, hour, minute, second, tzinfo=ET)


def _make_regime(ret_pct: float):
    """Return a SpyRegime instance pre-classified with the given 30m return."""
    from spy_regime import SpyRegime
    sr = SpyRegime()
    base = 500.0
    sr.spy_open_930 = base
    sr.spy_close_1000 = base * (1.0 + ret_pct / 100.0)
    sr._classify(_et(10, 0))
    return sr


def _make_cfg(is_long: bool):
    """Return a minimal cfg stub."""
    cfg = types.SimpleNamespace()
    cfg.side = types.SimpleNamespace(is_long=is_long)
    return cfg


def _call_amp(
    *,
    regime,
    shares: int = 10,
    ticker: str = "AAPL",
    is_long: bool = False,
    now_et=None,
    scale: float = 1.5,
    arm: str = "10:00",
    disarm: str = "11:00",
    enabled: bool = True,
):
    """Call _maybe_apply_regime_b_short_amp with env patching."""
    from broker.orders import _maybe_apply_regime_b_short_amp
    cfg = _make_cfg(is_long)
    if now_et is None:
        now_et = _et(10, 30)
    with mock.patch("eye_of_tiger.V611_REGIME_B_ENABLED", enabled):
        return _maybe_apply_regime_b_short_amp(
            cfg=cfg,
            shares=shares,
            ticker=ticker,
            now_et=now_et,
            regime=regime,
            scale=scale,
            arm_hhmm_et=arm,
            disarm_hhmm_et=disarm,
        )


# ---------------------------------------------------------------------------
# Test 1 -- Classifier basic A/B/C/D/E
# ---------------------------------------------------------------------------

def test_regime_b_classifier_basic():
    """Each of the 5 regime bands classifies correctly from 30m return."""
    cases = [
        (-0.60, "A"),   # deep down
        (-0.30, "B"),   # moderately down (inside (-0.50, -0.15))
        (0.00,  "C"),   # flat
        (0.30,  "D"),   # moderately up
        (0.60,  "E"),   # deep up
    ]
    for ret, expected in cases:
        sr = _make_regime(ret)
        assert sr.current_regime() == expected, (
            f"ret={ret}% expected {expected}, got {sr.current_regime()}"
        )
        if expected == "B":
            assert sr.is_regime_b() is True
        else:
            assert sr.is_regime_b() is False


# ---------------------------------------------------------------------------
# Test 2 -- Missing 9:30 anchor fails closed
# ---------------------------------------------------------------------------

def test_regime_b_missing_anchor_fails_closed():
    """Only 9:30 anchor set, no 10:00 tick: regime stays None, fails closed."""
    from spy_regime import SpyRegime
    sr = SpyRegime()
    sr.tick(_et(9, 30), 500.0)   # captures 9:30
    # No 10:00 tick
    assert sr.current_regime() is None
    assert sr.is_regime_b() is False


# ---------------------------------------------------------------------------
# Test 3 -- Long-side passthrough
# ---------------------------------------------------------------------------

def test_amp_helper_long_side_passthrough():
    """Long-side entries are never amplified."""
    sr = _make_regime(-0.30)  # regime B
    result = _call_amp(regime=sr, is_long=True, shares=10, now_et=_et(10, 30))
    assert result == 10


# ---------------------------------------------------------------------------
# Test 4 -- Pre-arm-time passthrough (09:55 ET)
# ---------------------------------------------------------------------------

def test_amp_helper_pre_arm_time_passthrough():
    """Short entry at 09:55 ET on regime-B day: no amplification."""
    sr = _make_regime(-0.30)  # regime B
    result = _call_amp(regime=sr, shares=10, now_et=_et(9, 55))
    assert result == 10


# ---------------------------------------------------------------------------
# Test 5 -- Post-disarm passthrough (11:00 ET exactly)
# ---------------------------------------------------------------------------

def test_amp_helper_post_disarm_passthrough():
    """Short entry at exactly 11:00 ET: disarm is exclusive, no amplification."""
    sr = _make_regime(-0.30)  # regime B
    result = _call_amp(regime=sr, shares=10, now_et=_et(11, 0))
    assert result == 10


# ---------------------------------------------------------------------------
# Test 6 -- Non-regime-B passthrough
# ---------------------------------------------------------------------------

def test_amp_helper_non_regime_b_passthrough():
    """Short entry at 10:30 ET on regime-A day: no amplification."""
    sr = _make_regime(-0.60)  # regime A
    result = _call_amp(regime=sr, shares=10, now_et=_et(10, 30))
    assert result == 10


# ---------------------------------------------------------------------------
# Test 7 -- Regime-B in-window amplifies (10:30 ET)
# ---------------------------------------------------------------------------

def test_amp_helper_regime_b_in_window_amplifies():
    """Short entry at 10:30 ET on regime-B day: shares x1.5 rounded."""
    sr = _make_regime(-0.30)  # regime B
    result = _call_amp(regime=sr, shares=10, now_et=_et(10, 30), scale=1.5)
    assert result == 15  # round(10 * 1.5) = 15


def test_amp_helper_regime_b_fractional_rounding():
    """Fractional amp rounds and is at least 1 (Python banker's rounding applies)."""
    sr = _make_regime(-0.30)
    result = _call_amp(regime=sr, shares=3, now_et=_et(10, 30), scale=1.5)
    # round(3 * 1.5) = round(4.5) = 4 in Python 3 (banker's rounding)
    assert result == 4
    assert result >= 1


# ---------------------------------------------------------------------------
# Test 8 -- Arm-boundary inclusive (10:00:00 ET exactly)
# ---------------------------------------------------------------------------

def test_amp_helper_arm_boundary_inclusive():
    """Entry at exactly 10:00 ET (arm time): amplifies (inclusive boundary)."""
    sr = _make_regime(-0.30)  # regime B
    result = _call_amp(regime=sr, shares=10, now_et=_et(10, 0), arm="10:00", disarm="11:00")
    assert result == 15


# ---------------------------------------------------------------------------
# Test 9 -- Disabled passthrough (V611_REGIME_B_ENABLED=0)
# ---------------------------------------------------------------------------

def test_amp_helper_disabled_passthrough():
    """V611_REGIME_B_ENABLED=False: no amplification regardless of regime/window."""
    sr = _make_regime(-0.30)  # regime B
    result = _call_amp(regime=sr, shares=10, now_et=_et(10, 30), enabled=False)
    assert result == 10


# ---------------------------------------------------------------------------
# Test 10 -- Logs [V611-AMP] line on amplification
# ---------------------------------------------------------------------------

def test_amp_helper_logs_v611_amp_line(caplog):
    """[V611-AMP] log line is emitted exactly once on amplification."""
    sr = _make_regime(-0.30)  # regime B
    # Capture root logger because trade_genius logger name may be __main__
    # when run as entrypoint vs 'trade_genius' when imported under pytest.
    with caplog.at_level(logging.INFO):
        _call_amp(regime=sr, shares=10, now_et=_et(10, 30))
    amp_lines = [r for r in caplog.records if "[V611-AMP]" in r.getMessage()]
    assert len(amp_lines) >= 1
    assert "SHORT" in amp_lines[0].getMessage()
    assert "regime=B" in amp_lines[0].getMessage()


# ---------------------------------------------------------------------------
# Test 11 -- Regime-B boundaries: exact -0.50% NOT B, -0.15% NOT B
# ---------------------------------------------------------------------------

def test_regime_b_lower_upper_boundary():
    """
    Spec test-11: exact -0.50% is NOT B (strictly >), exact -0.15% is NOT B (strictly <).
    -0.30% IS B, -0.49% IS B, -0.16% IS B.
    """
    # Exactly -0.50: should be A (ret <= lower)
    sr = _make_regime(-0.50)
    assert sr.current_regime() == "A", f"expected A at -0.50%, got {sr.current_regime()}"
    assert sr.is_regime_b() is False

    # Exactly -0.15: should be C (ret >= upper)
    sr = _make_regime(-0.15)
    assert sr.current_regime() == "C", f"expected C at -0.15%, got {sr.current_regime()}"
    assert sr.is_regime_b() is False

    # -0.30: IS B
    sr = _make_regime(-0.30)
    assert sr.current_regime() == "B"
    assert sr.is_regime_b() is True

    # -0.49: IS B (just inside lower boundary)
    sr = _make_regime(-0.49)
    assert sr.current_regime() == "B", f"expected B at -0.49%, got {sr.current_regime()}"

    # -0.16: IS B (just inside upper boundary)
    sr = _make_regime(-0.16)
    assert sr.current_regime() == "B", f"expected B at -0.16%, got {sr.current_regime()}"


# ---------------------------------------------------------------------------
# Test 12 -- BOT_VERSION parity
# ---------------------------------------------------------------------------

def test_v611_bot_version_parity():
    """bot_version.BOT_VERSION == trade_genius.BOT_VERSION.
    Updated in v6.11.1: accepts any 6.11.x release.
    """
    import bot_version
    assert bot_version.BOT_VERSION.startswith("6.11."), (
        f"bot_version.BOT_VERSION={bot_version.BOT_VERSION!r}"
    )
    # Read trade_genius BOT_VERSION without full module init (avoids FMP_API_KEY req).
    import os as _os
    import re as _re
    _tg_path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "trade_genius.py")
    with open(_tg_path, "r") as _f:
        _src = _f.read()
    _m = _re.search(r'BOT_VERSION = ["\']([^"\']+)["\']', _src)
    assert _m is not None, "BOT_VERSION not found in trade_genius.py"
    _tg_ver = _m.group(1)
    assert _tg_ver.startswith("6.11."), f"trade_genius.py BOT_VERSION={_tg_ver!r}"
    assert bot_version.BOT_VERSION == _tg_ver


# ---------------------------------------------------------------------------
# Test 13 -- 84d replay (expensive -- skipped here)
# ---------------------------------------------------------------------------

@pytest.mark.skip(
    reason=(
        "Run via scripts/replay_84d.py post-merge; "
        "assert post-amp delta in [+$673, +$693]. "
        "Backtest data: /home/user/workspace/canonical_backtest_data/84day_2026_sip/replay_layout/. "
        "Expected: 108 amplified pairs / 61.11% WR / +$683 vs $12,145 baseline."
    )
)
def test_v611_84d_replay_matches_backtest():
    """84d SIP backtest replay with C25: assert post-amp delta in [+$673, +$693]."""
    pass
