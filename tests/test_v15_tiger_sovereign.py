"""Tests for Tiger Sovereign v15.0 spec-conformance changes.

Covers the four flag-gated rule changes shipped on the
``experiment/tiger-sovereign-v15`` branch:

1. V15_HARD_STRIKE_CAP \u2014 disables the v7.0.2 recursive unlock so
   strike count >= 3 is a hard stop.
2. V15_SCALED_DI_FLOOR \u2014 raises the 1m-DI floor for SCALED_A from
   22.0 (v6.8.0 relaxation) back to the spec value of 25.0.
3. V15_REQUIRE_5M_ADX_20 \u2014 adds a momentum gate to
   evaluate_strike_sizing requiring 5m ADX > 20.
4. V15_ALARM_E_POST_ENABLED \u2014 flips the long-existing
   ``ALARM_E_ENABLED`` switch in engine.sentinel via the v15 flags
   module.
"""

from __future__ import annotations

import importlib
import os

import pytest

# Ensure trade_genius can import in unit-test context (it requires FMP_API_KEY
# at import time). Use a benign placeholder \u2014 no live HTTP is made.
os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:fake")
os.environ.setdefault("FMP_API_KEY", "test_dummy_key")
os.environ.setdefault("TRADEGENIUS_OWNER_IDS", "123")


# -- 1. Hard strike cap ----------------------------------------------------


def _reset_strike_state(tg):
    # Force the new-session reset path to run once so subsequent calls
    # are idempotent (won't wipe the counts the test just set).
    tg._v570_reset_if_new_session()
    tg._v570_strike_counts.clear()


def test_v15_hard_strike_cap_blocks_after_3_even_when_all_winners(monkeypatch):
    import trade_genius as tg
    from engine import v15_flags

    monkeypatch.setattr(v15_flags, "V15_HARD_STRIKE_CAP", True)
    _reset_strike_state(tg)

    monkeypatch.setattr(tg, "_v702_all_closed_strikes_positive", lambda t: True)
    monkeypatch.setattr(tg, "_v570_strike_must_be_flat", lambda *a, **k: True)

    # Pump strike count up to 3.
    tg._v570_strike_counts["NVDA"] = 3

    assert tg.strike_entry_allowed("NVDA", "LONG") is False


def test_v15_hard_strike_cap_off_falls_back_to_v702_unlock(monkeypatch):
    import trade_genius as tg
    from engine import v15_flags

    monkeypatch.setattr(v15_flags, "V15_HARD_STRIKE_CAP", False)
    _reset_strike_state(tg)

    monkeypatch.setattr(tg, "_v702_all_closed_strikes_positive", lambda t: True)
    monkeypatch.setattr(tg, "_v570_strike_must_be_flat", lambda *a, **k: True)
    tg._v570_strike_counts["NVDA"] = 3

    # v7.0.2 path: all closed strikes positive -> recursive unlock allows strike 4.
    assert tg.strike_entry_allowed("NVDA", "LONG") is True


def test_v15_hard_strike_cap_under_3_unaffected(monkeypatch):
    import trade_genius as tg
    from engine import v15_flags

    monkeypatch.setattr(v15_flags, "V15_HARD_STRIKE_CAP", True)
    _reset_strike_state(tg)
    monkeypatch.setattr(tg, "_v570_strike_must_be_flat", lambda *a, **k: True)

    tg._v570_strike_counts["NVDA"] = 2
    assert tg.strike_entry_allowed("NVDA", "LONG") is True


# -- 2. Scaled DI floor ----------------------------------------------------


def test_v15_scaled_floor_blocks_di_23_when_on(monkeypatch):
    from engine import v15_flags
    import eye_of_tiger as eot

    monkeypatch.setattr(v15_flags, "V15_SCALED_DI_FLOOR_ENABLED", True)
    monkeypatch.setattr(v15_flags, "V15_SCALED_DI_FLOOR", 25.0)
    monkeypatch.setattr(v15_flags, "V15_REQUIRE_5M_ADX_20", False)

    decision = eot.evaluate_strike_sizing(
        side="LONG",
        di_5m=27.0,
        di_1m=23.0,
        is_fresh_extreme=False,
        intended_shares=200,
        held_shares_this_strike=0,
    )
    assert decision.size_label == "WAIT"
    assert "below 25.0" in decision.reason


def test_v15_scaled_floor_passes_di_26_when_on(monkeypatch):
    from engine import v15_flags
    import eye_of_tiger as eot

    monkeypatch.setattr(v15_flags, "V15_SCALED_DI_FLOOR_ENABLED", True)
    monkeypatch.setattr(v15_flags, "V15_SCALED_DI_FLOOR", 25.0)
    monkeypatch.setattr(v15_flags, "V15_REQUIRE_5M_ADX_20", False)

    decision = eot.evaluate_strike_sizing(
        side="LONG",
        di_5m=27.0,
        di_1m=26.0,
        is_fresh_extreme=False,
        intended_shares=200,
        held_shares_this_strike=0,
    )
    assert decision.size_label == "SCALED_A"
    assert decision.shares_to_buy == 100


def test_v15_scaled_floor_off_keeps_v68_relaxation(monkeypatch):
    from engine import v15_flags
    import eye_of_tiger as eot

    monkeypatch.setattr(v15_flags, "V15_SCALED_DI_FLOOR_ENABLED", False)
    monkeypatch.setattr(v15_flags, "V15_REQUIRE_5M_ADX_20", False)

    decision = eot.evaluate_strike_sizing(
        side="LONG",
        di_5m=27.0,
        di_1m=23.0,
        is_fresh_extreme=False,
        intended_shares=200,
        held_shares_this_strike=0,
    )
    assert decision.size_label == "SCALED_A"


# -- 3. 5m ADX > 20 momentum gate -----------------------------------------


def test_v15_momentum_gate_blocks_low_adx(monkeypatch):
    from engine import v15_flags
    import eye_of_tiger as eot

    monkeypatch.setattr(v15_flags, "V15_REQUIRE_5M_ADX_20", True)
    monkeypatch.setattr(v15_flags, "V15_MOMENTUM_ADX_5M_MIN", 20.0)
    monkeypatch.setattr(v15_flags, "V15_SCALED_DI_FLOOR_ENABLED", False)

    decision = eot.evaluate_strike_sizing(
        side="LONG",
        di_5m=30.0,
        di_1m=35.0,
        is_fresh_extreme=False,
        intended_shares=200,
        held_shares_this_strike=0,
        adx_5m=15.0,
    )
    assert decision.size_label == "WAIT"
    assert "v15 momentum gate" in decision.reason


def test_v15_momentum_gate_blocks_missing_adx(monkeypatch):
    from engine import v15_flags
    import eye_of_tiger as eot

    monkeypatch.setattr(v15_flags, "V15_REQUIRE_5M_ADX_20", True)
    monkeypatch.setattr(v15_flags, "V15_SCALED_DI_FLOOR_ENABLED", False)

    decision = eot.evaluate_strike_sizing(
        side="LONG",
        di_5m=30.0,
        di_1m=35.0,
        is_fresh_extreme=False,
        intended_shares=200,
        held_shares_this_strike=0,
        adx_5m=None,
    )
    assert decision.size_label == "WAIT"
    assert "5m ADX is None" in decision.reason


def test_v15_momentum_gate_passes_high_adx(monkeypatch):
    from engine import v15_flags
    import eye_of_tiger as eot

    monkeypatch.setattr(v15_flags, "V15_REQUIRE_5M_ADX_20", True)
    monkeypatch.setattr(v15_flags, "V15_MOMENTUM_ADX_5M_MIN", 20.0)
    monkeypatch.setattr(v15_flags, "V15_SCALED_DI_FLOOR_ENABLED", False)

    decision = eot.evaluate_strike_sizing(
        side="LONG",
        di_5m=30.0,
        di_1m=35.0,
        is_fresh_extreme=False,
        intended_shares=200,
        held_shares_this_strike=0,
        adx_5m=25.0,
    )
    assert decision.size_label == "FULL"


def test_v15_momentum_gate_off_ignores_adx(monkeypatch):
    from engine import v15_flags
    import eye_of_tiger as eot

    monkeypatch.setattr(v15_flags, "V15_REQUIRE_5M_ADX_20", False)
    monkeypatch.setattr(v15_flags, "V15_SCALED_DI_FLOOR_ENABLED", False)

    decision = eot.evaluate_strike_sizing(
        side="LONG",
        di_5m=30.0,
        di_1m=35.0,
        is_fresh_extreme=False,
        intended_shares=200,
        held_shares_this_strike=0,
        adx_5m=5.0,
    )
    # Low ADX must NOT block when the flag is off.
    assert decision.size_label == "FULL"


# -- 4. Alarm E post-entry sentinel flip ----------------------------------


def test_v15_alarm_e_post_flag_flips_existing_switch():
    """Importing engine.sentinel with V15_ALARM_E_POST_ENABLED=True
    should produce ALARM_E_ENABLED=True at module level."""
    # Import-side effect: confirm the v15_flags module truthiness
    # propagates to engine.sentinel.ALARM_E_ENABLED on a fresh import.
    import engine.v15_flags as vf
    import engine.sentinel as sen

    if vf.V15_ALARM_E_POST_ENABLED:
        assert sen.ALARM_E_ENABLED is True
    else:
        # With flag off the legacy default applies (False).
        assert sen.ALARM_E_ENABLED is False


# -- 5. Defaults sanity ----------------------------------------------------


def test_v15_flags_module_defaults_match_spec():
    from engine import v15_flags

    assert v15_flags.V15_HARD_STRIKE_CAP is True
    assert v15_flags.V15_SCALED_DI_FLOOR_ENABLED is True
    assert v15_flags.V15_REQUIRE_5M_ADX_20 is True
    assert v15_flags.V15_ALARM_E_POST_ENABLED is True
    assert v15_flags.V15_SCALED_DI_FLOOR == 25.0
    assert v15_flags.V15_MOMENTUM_ADX_5M_MIN == 20.0
