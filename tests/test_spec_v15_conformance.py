"""Tiger Sovereign v15.0 spec conformance tests \u2014 v5.20.0.

These tests pin the engine-side rules introduced or tightened in
v5.20.0 to match the canonical v15.0 spec at
``/home/user/workspace/tiger-sovereign-spec-v15-1.md``. They are
intentionally narrow (one rule per test) so a future regression
points at exactly which spec section drifted.

The tests do NOT exercise the live ``broker.orders.check_entry`` path
end-to-end (that would require building a full ``trade_genius``
runtime mock). Instead they pin the behavior of the small,
single-purpose helpers ``check_entry`` calls into.
"""

from __future__ import annotations

import os
import sys
from datetime import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Importing trade_genius runs its module-level startup. SSM_SMOKE_TEST=1
# short-circuits the catch-up + Telegram loop so the import is safe in
# unit tests. Set BEFORE the first import.
os.environ.setdefault("SSM_SMOKE_TEST", "1")


def _import_real_trade_genius():
    """Return the real ``trade_genius`` module, evicting any stub.

    Some sibling tests (notably ``test_replay_v511_engine_seam.py``)
    install a fake ``trade_genius`` SimpleNamespace into ``sys.modules``
    and never restore the real module. When tests run alphabetically
    those stubs leak into our session and ``import trade_genius``
    resolves to the stub. We defensively evict the stub and re-import
    the real module before each test that needs it.
    """
    mod = sys.modules.get("trade_genius")
    # Detect a stub by the absence of a real module-level attribute the
    # production module always defines (``_v570_strike_counts``).
    if mod is None or not hasattr(mod, "_v570_strike_counts"):
        sys.modules.pop("trade_genius", None)
        import trade_genius as tg  # noqa: PLC0415 \u2014 deliberately late

        return tg
    return mod


# ---------------------------------------------------------------------------
# Section 4 \u2014 Entry window: 09:36:00 to 15:44:59 EST.
# ---------------------------------------------------------------------------


def test_v15_entry_window_starts_at_0936():
    """v15.0 \u00a74: the hunt-window start is 09:36:00 ET."""
    from engine.timing import HUNT_START_ET

    assert HUNT_START_ET == time(9, 36, 0)


def test_v15_entry_window_ends_at_154459():
    """v15.0 \u00a74: the new-position cutoff is 15:44:59 ET."""
    from engine.timing import NEW_POSITION_CUTOFF_ET, HUNT_END_ET

    assert NEW_POSITION_CUTOFF_ET == time(15, 44, 59)
    assert HUNT_END_ET == NEW_POSITION_CUTOFF_ET


# ---------------------------------------------------------------------------
# Section 0 \u2014 ORH/ORL freeze at exactly 09:35:59 (so the OR aggregation
# window is the half-open minute range [09:30, 09:36) and includes the
# 09:35 candle).
# ---------------------------------------------------------------------------


def test_v15_or_window_end_is_0936_half_open():
    """v15.0 \u00a70: ORH/ORL fixed at 09:35:59. Engine OR aggregation
    upper bound is the half-open boundary 09:36 so the 09:35 1m candle
    is INCLUDED in the OR aggregation."""
    import eye_of_tiger as eot

    assert eot.OR_WINDOW_END_HHMM_ET == "09:36"


def test_v15_boundary_hold_required_closes_is_2():
    """v15.0 \u00a71: the permission ladder requires 2 consecutive 1m
    closes outside the target level."""
    import eye_of_tiger as eot

    assert eot.BOUNDARY_HOLD_REQUIRED_CLOSES == 2


# ---------------------------------------------------------------------------
# Section 1 \u2014 Strike sequence: max 3 per ticker per day, sequential.
# ---------------------------------------------------------------------------


def test_v15_strike_cap_3_per_ticker_per_day():
    """v15.0 \u00a71: max 3 Strikes per ticker per day. ``strike_entry_allowed``
    must return False on the 4th attempt and True for the first 3.

    v5.19.1 vAA-1 ULTIMATE Decision 1: cap is per-ticker (long+short share
    a single counter), so the underlying state is ``_v570_strike_counts``
    (plural), keyed by ticker only.
    """
    tg = _import_real_trade_genius()

    # Reset the per-ticker count so the test is deterministic.
    tg._v570_strike_counts.clear()
    tg._v570_strike_date = tg._v570_session_today_str()

    # Strikes 1\u20133 allowed (flat gate satisfied with no positions arg).
    for n in range(3):
        assert tg.strike_entry_allowed("AAPL", "LONG") is True, f"Strike {n + 1} must be allowed"
        # Walk the counter via the canonical hot-path helper.
        tg._v570_record_entry("AAPL", "LONG")

    # 4th attempt blocked.
    assert tg.strike_entry_allowed("AAPL", "LONG") is False, (
        "Strike 4 must be blocked (v15.0 cap = 3)"
    )

    # Cleanup so other tests see a clean slate.
    tg._v570_strike_counts.clear()


def test_v15_strike_sequential_requirement_blocks_when_position_open():
    """v15.0 \u00a71: sequential requirement \u2014 a subsequent strike
    cannot initiate while the previous position is non-flat
    (STRIKE-FLAT-GATE, per-side)."""
    tg = _import_real_trade_genius()

    tg._v570_strike_counts.clear()
    tg._v570_strike_date = tg._v570_session_today_str()

    # Open-position view: AAPL:LONG holds 100 shares. The flat gate
    # consults ``shares`` on the matching key.
    open_view = {"AAPL:LONG": {"shares": 100}}
    assert tg.strike_entry_allowed("AAPL", "LONG", open_view) is False

    # Sanity: when shares=0, the gate is satisfied again.
    flat_view = {"AAPL:LONG": {"shares": 0}}
    assert tg.strike_entry_allowed("AAPL", "LONG", flat_view) is True

    tg._v570_strike_counts.clear()


# ---------------------------------------------------------------------------
# Section 1.2 \u2014 Alarm E pre-entry filter for Strike 2 & Strike 3.
# Strike 1 is never blocked by Alarm E pre-filter (no stored peak yet
# by definition).
# ---------------------------------------------------------------------------


def test_v15_alarm_e_pre_does_not_block_strike_1():
    """v15.0 \u00a71.2: Alarm E pre-filter only blocks new S2/S3."""
    from engine.momentum_state import DivergenceMemory
    from engine.sentinel import check_alarm_e_pre

    mem = DivergenceMemory()
    # Even with a stored divergent peak, S1 is never pre-blocked.
    mem.update("AAPL", "LONG", price=200.0, rsi=72.0)
    blocked = check_alarm_e_pre(
        memory=mem,
        ticker="AAPL",
        side="LONG",
        current_price=205.0,
        current_rsi_15=60.0,  # would diverge
        strike_num=1,
    )
    assert blocked is False


def test_v15_alarm_e_pre_blocks_strike_2_on_long_divergence():
    """v15.0 \u00a71.2: S2 long with a fresh NHOD on FALLING RSI must
    be pre-blocked."""
    from engine.momentum_state import DivergenceMemory
    from engine.sentinel import check_alarm_e_pre

    mem = DivergenceMemory()
    mem.update("AAPL", "LONG", price=200.0, rsi=72.0)
    blocked = check_alarm_e_pre(
        memory=mem,
        ticker="AAPL",
        side="LONG",
        current_price=205.0,  # fresh NHOD
        current_rsi_15=60.0,  # divergence (lower than stored 72)
        strike_num=2,
    )
    assert blocked is True


def test_v15_alarm_e_pre_blocks_strike_3_on_short_divergence():
    """v15.0 \u00a71.2: S3 short with a fresh NLOD on RISING RSI must
    be pre-blocked."""
    from engine.momentum_state import DivergenceMemory
    from engine.sentinel import check_alarm_e_pre

    mem = DivergenceMemory()
    mem.update("NVDA", "SHORT", price=400.0, rsi=30.0)
    blocked = check_alarm_e_pre(
        memory=mem,
        ticker="NVDA",
        side="SHORT",
        current_price=395.0,  # fresh NLOD
        current_rsi_15=45.0,  # divergence (higher than stored 30)
        strike_num=3,
    )
    assert blocked is True


def test_v15_alarm_e_pre_passes_strike_2_when_rsi_confirms():
    """v15.0 \u00a71.2: a fresh NHOD with CONFIRMING RSI is not blocked."""
    from engine.momentum_state import DivergenceMemory
    from engine.sentinel import check_alarm_e_pre

    mem = DivergenceMemory()
    mem.update("AAPL", "LONG", price=200.0, rsi=72.0)
    blocked = check_alarm_e_pre(
        memory=mem,
        ticker="AAPL",
        side="LONG",
        current_price=205.0,
        current_rsi_15=78.0,  # confirming (higher than stored 72)
        strike_num=2,
    )
    assert blocked is False


# ---------------------------------------------------------------------------
# Section 0 + Alarm E \u2014 DivergenceMemory stores price+RSI at the exact
# tick of every new NHOD/NLOD, regardless of RSI direction.
# ---------------------------------------------------------------------------


def test_v15_divergence_memory_stores_on_every_new_long_extreme():
    """v15.0 \u00a70: store unconditionally on a new NHOD (long path)."""
    from engine.momentum_state import DivergenceMemory

    mem = DivergenceMemory()
    mem.update("AAPL", "LONG", price=200.0, rsi=72.0)
    # New NHOD with LOWER RSI (divergent) \u2014 must still store.
    mem.update("AAPL", "LONG", price=205.0, rsi=58.0)
    assert mem.peak("AAPL", "LONG") == (205.0, 58.0)


def test_v15_divergence_memory_stores_on_every_new_short_extreme():
    """v15.0 \u00a70: store unconditionally on a new NLOD (short path)."""
    from engine.momentum_state import DivergenceMemory

    mem = DivergenceMemory()
    mem.update("NVDA", "SHORT", price=400.0, rsi=30.0)
    # New NLOD with HIGHER RSI (divergent) \u2014 must still store.
    mem.update("NVDA", "SHORT", price=395.0, rsi=42.0)
    assert mem.peak("NVDA", "SHORT") == (395.0, 42.0)


# ---------------------------------------------------------------------------
# v5.26.0: BL-3 / BU-3 (Volume Gate) BYPASSED \u2014 removed entirely per
# operator policy (data-quality issues with 1m volume baseline). The
# v15.0 spec mentions a volume gate but it is intentionally not enforced
# in this build; tests for it have been removed.
# ---------------------------------------------------------------------------


def test_v15_volume_bucket_check_returns_ratio_to_55bar_avg_alias(tmp_path):
    """v15.0 spec uses the field name ``ratio_to_55bar_avg``. The
    engine helper must expose that key (alias of the existing
    ``ratio`` field) so spec-named consumers resolve.

    The check() method returns the alias on every code path: PASS,
    FAIL, and COLDSTART. We exercise the COLDSTART path (no seeded
    bars on disk \u2014 ``base_dir`` points at an empty tmpdir) which
    is sufficient to pin the schema.
    """
    from volume_bucket import VolumeBucketBaseline

    bucket = VolumeBucketBaseline(base_dir=str(tmp_path))
    # No refresh()/seed; baseline is empty so any ticker is COLDSTART.
    res = bucket.check("AAPL", "09:45", 1500)
    # Schema invariant: alias key MUST be present, regardless of gate.
    assert "ratio_to_55bar_avg" in res
    assert "ratio" in res
    # The alias must always equal the canonical ratio (both None on
    # the COLDSTART path).
    assert res["ratio_to_55bar_avg"] == res["ratio"]


# ---------------------------------------------------------------------------
# Section 4 \u2014 Risk constants.
# ---------------------------------------------------------------------------


def test_v15_daily_circuit_breaker_is_minus_1500():
    """v15.0 \u00a74: daily circuit breaker fires at \u2212$1,500."""
    import eye_of_tiger as eot

    assert eot.DAILY_CIRCUIT_BREAKER_DOLLARS == -1500.0


# ---------------------------------------------------------------------------
# Addendum \u2014 Alarm A flash-move strict > 1%.
# ---------------------------------------------------------------------------


def test_v15_alarm_a_uses_strict_greater_than_1pct():
    """v15.0 Addendum: \"1m price move > 1% against position\". A move
    of exactly \u22121% over the 60s velocity window must NOT fire
    A_FLASH; a move of more than \u22121% must.

    The engine signature is
    ``check_alarm_a(*, side, unrealized_pnl, position_value,
    pnl_history, now_ts) -> list[SentinelAction]``. ``pnl_history``
    is an iterable of (ts, pnl) pairs over the trailing window.
    """
    from engine.sentinel import check_alarm_a

    now_ts = 1_000_000.0
    position_value = 10_000.0

    # Build a pnl_history where the 60s-ago pnl is 0.0 and the
    # current pnl is exactly -1% of position_value (-100). This is
    # the boundary case: strict ``<`` MUST NOT trigger.
    pnl_history_eq = [(now_ts - 60.0, 0.0), (now_ts, -100.0)]
    fired_eq = check_alarm_a(
        side="LONG",
        unrealized_pnl=-100.0,
        position_value=position_value,
        pnl_history=pnl_history_eq,
        now_ts=now_ts,
    )
    assert all(a.alarm != "A_FLASH" for a in fired_eq), (
        "Exactly \u22121% must NOT trigger A_FLASH (strict < boundary)"
    )

    # Now exceed the boundary: -100.01 of pnl drop on $10k position
    # is -1.0001% \u2014 must trigger.
    pnl_history_gt = [(now_ts - 60.0, 0.0), (now_ts, -100.01)]
    fired_gt = check_alarm_a(
        side="LONG",
        unrealized_pnl=-100.01,
        position_value=position_value,
        pnl_history=pnl_history_gt,
        now_ts=now_ts,
    )
    assert any(a.alarm == "A_FLASH" for a in fired_gt), (
        "Move > \u22121% over 60s must trigger A_FLASH"
    )


# ---------------------------------------------------------------------------
# v5.20.0 wire-in: broker.orders.execute_breakout calls
# evaluate_strike_sizing with held=0, alarm_e=False, fresh_extreme=False
# and intended_shares = starter * 2. These tests pin the contract that
# wire-in relies on so a future signature drift is caught here.
# ---------------------------------------------------------------------------


def _eval_with_wire_in_args(*, side, di_5m, di_1m, starter_shares=100):
    """Mirror the exact args execute_breakout passes."""
    from eye_of_tiger import evaluate_strike_sizing

    return evaluate_strike_sizing(
        side=side,
        di_5m=di_5m,
        di_1m=di_1m,
        is_fresh_extreme=False,
        intended_shares=int(starter_shares) * 2,
        held_shares_this_strike=0,
        alarm_e_blocked=False,
    )


def test_v15_wire_in_full_tier_long_doubles_starter_shares():
    """v15.0 §2 Full Strike: 1m DI > 30 → 2 × starter shares."""
    decision = _eval_with_wire_in_args(side="LONG", di_5m=28.0, di_1m=35.0, starter_shares=100)
    assert decision.size_label == "FULL", (
        f"1m DI=35 (>30) must yield FULL, got {decision.size_label}"
    )
    assert decision.shares_to_buy == 200, (
        f"FULL must double starter (100 → 200), got {decision.shares_to_buy}"
    )


def test_v15_wire_in_full_tier_short_doubles_starter_shares():
    """Symmetric SHORT polarity: caller passes DI- as di_1m / di_5m."""
    decision = _eval_with_wire_in_args(side="SHORT", di_5m=28.0, di_1m=35.0, starter_shares=80)
    assert decision.size_label == "FULL"
    assert decision.shares_to_buy == 160


def test_v15_wire_in_scaled_a_tier_yields_starter_shares():
    """v15.0 §2 Scaled Strike: 1m DI in [25, 30] → 50% starter."""
    decision = _eval_with_wire_in_args(side="LONG", di_5m=28.0, di_1m=27.5, starter_shares=100)
    assert decision.size_label == "SCALED_A", (
        f"1m DI=27.5 must yield SCALED_A, got {decision.size_label}"
    )
    # intended=200, SCALED returns intended//2 = 100 (= starter).
    assert decision.shares_to_buy == 100, (
        f"SCALED_A must equal starter (100), got {decision.shares_to_buy}"
    )


def test_v15_wire_in_scaled_a_lower_boundary_25():
    """Boundary: 1m DI = 25.0 is inclusive of SCALED_A range."""
    decision = _eval_with_wire_in_args(side="LONG", di_5m=28.0, di_1m=25.0, starter_shares=100)
    assert decision.size_label == "SCALED_A"


def test_v15_wire_in_scaled_a_upper_boundary_30():
    """Boundary: 1m DI = 30.0 is inclusive of SCALED_A (FULL is strict >30)."""
    decision = _eval_with_wire_in_args(side="LONG", di_5m=28.0, di_1m=30.0, starter_shares=100)
    assert decision.size_label == "SCALED_A", (
        "1m DI = 30.0 exactly must be SCALED_A (FULL requires strict > 30)"
    )


def test_v15_wire_in_wait_below_scaled_range_aborts_entry():
    """v15.0: 1m DI < 25 with held=0 → WAIT (live path defensively aborts)."""
    decision = _eval_with_wire_in_args(side="LONG", di_5m=28.0, di_1m=20.0, starter_shares=100)
    assert decision.size_label == "WAIT", (
        f"1m DI=20 (<25) must yield WAIT, got {decision.size_label}"
    )
    assert decision.shares_to_buy == 0


def test_v15_wire_in_wait_when_5m_di_anchor_fails():
    """L-P3-AUTH master anchor: 5m DI ≤ 25 → WAIT regardless of 1m."""
    decision = _eval_with_wire_in_args(side="LONG", di_5m=24.0, di_1m=40.0, starter_shares=100)
    assert decision.size_label == "WAIT", (
        "5m DI anchor fail must yield WAIT even when 1m DI is strong"
    )


def test_v15_wire_in_wait_when_di_streams_missing():
    """Defensive: missing DI from v5_di_1m_5m (None) must yield WAIT."""
    # 5m None
    d1 = _eval_with_wire_in_args(side="LONG", di_5m=None, di_1m=40.0)
    assert d1.size_label == "WAIT"
    # 1m None
    d2 = _eval_with_wire_in_args(side="LONG", di_5m=28.0, di_1m=None)
    assert d2.size_label == "WAIT"


def test_v15_wire_in_full_implies_entry2_no_top_up():
    """Wire-in pre-sets v5104_entry2_fired=True when FULL fires so the
    legacy Entry-2 add-on (50% → 100% top-up) does NOT double-fill.

    This test pins the Boolean mapping that broker/orders.py line ~737
    encodes: "v5104_entry2_fired": (size_label == "FULL").
    """
    full = _eval_with_wire_in_args(side="LONG", di_5m=28.0, di_1m=35.0)
    scaled = _eval_with_wire_in_args(side="LONG", di_5m=28.0, di_1m=27.0)

    # The wire-in expression evaluated on each tier:
    full_entry2_fired = full.size_label == "FULL"
    scaled_entry2_fired = scaled.size_label == "FULL"

    assert full_entry2_fired is True, (
        "FULL tier fills 100% in one fill → must mark Entry-2 as fired"
    )
    assert scaled_entry2_fired is False, (
        "SCALED_A tier fills 50% → must leave Entry-2 unfired (top-up allowed)"
    )
