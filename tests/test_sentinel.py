"""Unit tests for engine/sentinel.py (v5.13.0 PR 2).

Covers Tiger Sovereign Phase 4 alarms A (-$500 / -1%/min) and B
(closed 5m vs 9-EMA). Asserts both fire INDEPENDENTLY in the same
tick (parallel-not-sequential) and that exactly one exit reason is
emitted even when multiple alarms trip.
"""

from __future__ import annotations

from collections import deque

import pytest

from engine.sentinel import (
    ALARM_A_HARD_LOSS_DOLLARS,
    EXIT_REASON_ALARM_A,
    EXIT_REASON_ALARM_B,
    EXIT_REASON_R2_HARD_STOP,
    SIDE_LONG,
    SIDE_SHORT,
    check_alarm_a,
    check_alarm_b,
    evaluate_sentinel,
    new_pnl_history,
    record_pnl,
    record_session_5m_adx,
)
from engine.sentinel import _SESSION_5M_ADX_HWM


# ---------------------------------------------------------------------------
# Alarm A_LOSS \u2014 hard floor at -$500
# ---------------------------------------------------------------------------


def test_alarm_a1_fires_at_exactly_minus_500():
    """A_LOSS must fire at the spec-literal boundary: pnl <= -$500."""
    fired = check_alarm_a(
        side=SIDE_LONG,
        unrealized_pnl=-500.0,
        position_value=10000.0,
        pnl_history=None,
        now_ts=1000.0,
    )
    assert any(a.alarm == "A_LOSS" for a in fired), (
        "A_LOSS must fire at exactly -$500 (boundary inclusive)"
    )
    a1 = next(a for a in fired if a.alarm == "A_LOSS")
    # vAA-1 / RULING #4: A_LOSS sub-alarm uses EXIT_REASON_R2_HARD_STOP
    # (Tiger Sovereign v15.0 Risk Rails R-2 \u2014 STOP MARKET).
    # A_FLASH sub-alarm continues to use EXIT_REASON_ALARM_A.
    assert a1.reason == EXIT_REASON_R2_HARD_STOP


def test_alarm_a1_does_not_fire_at_minus_499():
    """At -$499 the position still has -$1 of headroom \u2014 no fire."""
    fired = check_alarm_a(
        side=SIDE_LONG,
        unrealized_pnl=-499.0,
        position_value=10000.0,
        pnl_history=None,
        now_ts=1000.0,
    )
    assert not any(a.alarm == "A_LOSS" for a in fired)


def test_alarm_a1_constant_is_exactly_minus_500():
    assert ALARM_A_HARD_LOSS_DOLLARS == -500.0


# ---------------------------------------------------------------------------
# Alarm A_FLASH \u2014 -1% over 60 seconds
# ---------------------------------------------------------------------------


def test_alarm_a2_fires_when_60s_velocity_is_minus_1_01_percent():
    """A_FLASH fires when (delta / position_value) <= -0.01 over the last 60s.

    Delta of -101 on a $10,000 notional is -1.01% \u2014 fires.
    """
    history = new_pnl_history()
    record_pnl(history, ts=1000.0, pnl=0.0)
    fired = check_alarm_a(
        side=SIDE_LONG,
        unrealized_pnl=-101.0,
        position_value=10000.0,
        pnl_history=history,
        now_ts=1060.0,
    )
    assert any(a.alarm == "A_FLASH" for a in fired), "A_FLASH must fire when 60s velocity is -1.01%"


def test_alarm_a2_does_not_fire_at_minus_0_99_percent():
    """At -0.99% over 60s, A_FLASH does NOT fire."""
    history = new_pnl_history()
    record_pnl(history, ts=1000.0, pnl=0.0)
    fired = check_alarm_a(
        side=SIDE_LONG,
        unrealized_pnl=-99.0,
        position_value=10000.0,
        pnl_history=history,
        now_ts=1060.0,
    )
    assert not any(a.alarm == "A_FLASH" for a in fired)


def test_alarm_a2_does_not_fire_without_history():
    """No history sample at-or-before now-60s \u2014 cannot evaluate."""
    history = deque()
    fired = check_alarm_a(
        side=SIDE_LONG,
        unrealized_pnl=-200.0,
        position_value=10000.0,
        pnl_history=history,
        now_ts=1060.0,
    )
    assert not any(a.alarm == "A_FLASH" for a in fired)


def test_alarm_a2_short_side_uses_signed_pnl():
    """Short side: P&L convention (entry - current) * shares.

    A short whose P&L drops by -$200 over 60s on $10k notional is
    -2% velocity \u2014 fires.
    """
    history = new_pnl_history()
    record_pnl(history, ts=1000.0, pnl=0.0)
    fired = check_alarm_a(
        side=SIDE_SHORT,
        unrealized_pnl=-200.0,
        position_value=10000.0,
        pnl_history=history,
        now_ts=1060.0,
    )
    assert any(a.alarm == "A_FLASH" for a in fired)


# ---------------------------------------------------------------------------
# Alarm B \u2014 closed 5m close vs 9-EMA
# ---------------------------------------------------------------------------


def test_alarm_b_long_fires_on_close_below_ema9():
    fired = check_alarm_b(
        side=SIDE_LONG,
        last_5m_close=99.50,
        last_5m_ema9=100.00,
    )
    assert len(fired) == 1
    assert fired[0].alarm == "B"
    assert fired[0].reason == EXIT_REASON_ALARM_B


def test_alarm_b_long_does_not_fire_on_close_above_ema9():
    fired = check_alarm_b(
        side=SIDE_LONG,
        last_5m_close=100.50,
        last_5m_ema9=100.00,
    )
    assert fired == []


def test_alarm_b_short_fires_on_close_above_ema9():
    fired = check_alarm_b(
        side=SIDE_SHORT,
        last_5m_close=100.50,
        last_5m_ema9=100.00,
    )
    assert len(fired) == 1
    assert fired[0].alarm == "B"


def test_alarm_b_short_does_not_fire_on_close_below_ema9():
    fired = check_alarm_b(
        side=SIDE_SHORT,
        last_5m_close=99.50,
        last_5m_ema9=100.00,
    )
    assert fired == []


def test_alarm_b_skipped_when_ema9_unseeded():
    """No EMA9 yet (less than 9 closed 5m bars) \u2014 alarm is silent."""
    assert (
        check_alarm_b(
            side=SIDE_LONG,
            last_5m_close=99.0,
            last_5m_ema9=None,
        )
        == []
    )


# ---------------------------------------------------------------------------
# PARALLEL semantics \u2014 the headline test
# ---------------------------------------------------------------------------


def test_alarms_a_and_b_fire_independently_in_same_tick():
    """Construct a scenario where BOTH A and B trip on the same tick.

    The spec emphasizes "These Alarms are NOT a sequence." This test
    is the parallel-not-sequential guarantee: both must appear in the
    SentinelResult, not just whichever evaluator runs first.
    """
    history = new_pnl_history()
    record_pnl(history, ts=1000.0, pnl=0.0)

    result = evaluate_sentinel(
        side=SIDE_LONG,
        unrealized_pnl=-600.0,  # triggers A_LOSS (hard floor)
        position_value=10000.0,
        pnl_history=history,
        now_ts=1060.0,  # 60s after history start
        last_5m_close=99.0,  # below EMA9 \u2014 triggers B
        last_5m_ema9=100.0,
    )

    codes = result.alarm_codes
    assert "A_LOSS" in codes, f"expected A_LOSS in {codes}"
    assert "B" in codes, f"expected B in {codes}"
    # And A_FLASH because pnl dropped from 0 to -600 over 60s on $10k = -6%
    assert "A_FLASH" in codes, f"expected A_FLASH in {codes}"


def test_one_exit_reason_even_with_multiple_alarms():
    """If A and B both fire, ``exit_reason`` returns exactly one code.

    The downstream broker emits one exit order \u2014 but the full alarm
    list is preserved for telemetry.
    """
    history = new_pnl_history()
    record_pnl(history, ts=1000.0, pnl=0.0)
    result = evaluate_sentinel(
        side=SIDE_LONG,
        unrealized_pnl=-600.0,
        position_value=10000.0,
        pnl_history=history,
        now_ts=1060.0,
        last_5m_close=99.0,
        last_5m_ema9=100.0,
    )
    # vAA-1 / RULING #4: priority is R-2 > A_FLASH > B > D. With
    # unrealized_pnl=-600 the A_LOSS sub-alarm fires first, mapped to
    # EXIT_REASON_R2_HARD_STOP, which outranks B.
    assert result.exit_reason == EXIT_REASON_R2_HARD_STOP
    assert len(result.alarms) >= 2  # but B is still in the list


def test_evaluate_sentinel_no_fire_at_clean_state():
    """Healthy position: no history conflict, close above EMA9, pnl flat."""
    history = new_pnl_history()
    record_pnl(history, ts=1000.0, pnl=0.0)
    result = evaluate_sentinel(
        side=SIDE_LONG,
        unrealized_pnl=10.0,
        position_value=10000.0,
        pnl_history=history,
        now_ts=1060.0,
        last_5m_close=101.0,
        last_5m_ema9=100.0,
    )
    assert not result.fired
    assert result.exit_reason is None


def test_evaluate_sentinel_only_b_fires_when_a_quiet():
    """Alarm B must fire even when A is silent. Independence test."""
    history = new_pnl_history()
    record_pnl(history, ts=1000.0, pnl=-50.0)
    result = evaluate_sentinel(
        side=SIDE_LONG,
        unrealized_pnl=-90.0,  # well above A_LOSS floor (-$500)
        position_value=10000.0,  # delta of -$40 / $10k = -0.40% (< 1%)
        pnl_history=history,
        now_ts=1060.0,
        last_5m_close=99.0,  # below EMA9
        last_5m_ema9=100.0,
    )
    assert result.alarm_codes == ["B"]
    assert result.exit_reason == EXIT_REASON_ALARM_B


def test_pnl_history_is_bounded():
    """Memory hygiene: history deque caps at PNL_HISTORY_MAXLEN."""
    history = new_pnl_history()
    for i in range(500):
        record_pnl(history, ts=float(i), pnl=float(i))
    from engine.sentinel import PNL_HISTORY_MAXLEN

    assert len(history) == PNL_HISTORY_MAXLEN


# ---------------------------------------------------------------------------
# Spec wiring sanity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("side", [SIDE_LONG, SIDE_SHORT])
def test_a_and_b_fire_for_both_sides(side):
    """Spec rule mirrors: the same semantics apply long and short."""
    history = new_pnl_history()
    record_pnl(history, ts=1000.0, pnl=0.0)
    if side == SIDE_LONG:
        close, ema = 99.0, 100.0
    else:
        close, ema = 101.0, 100.0
    result = evaluate_sentinel(
        side=side,
        unrealized_pnl=-700.0,
        position_value=10000.0,
        pnl_history=history,
        now_ts=1060.0,
        last_5m_close=close,
        last_5m_ema9=ema,
    )
    codes = result.alarm_codes
    assert "A_LOSS" in codes and "B" in codes


# ---------------------------------------------------------------------------
# v5.13.2 P1 #4 \u2014 Alarm A baseline reset on share-count change (Entry-2)
# ---------------------------------------------------------------------------


from engine.sentinel import maybe_reset_pnl_baseline_on_shares_change


def test_baseline_reset_first_call_records_silently():
    """First call after pos creation seeds the share-count cache, no clear."""
    history = new_pnl_history()
    record_pnl(history, ts=1000.0, pnl=-10.0)
    record_pnl(history, ts=1010.0, pnl=-15.0)
    pos = {"shares": 10, "entry_price": 100.0}
    cleared = maybe_reset_pnl_baseline_on_shares_change(
        pos,
        history,
        now_ts=1020.0,
        current_unrealized_pnl=-20.0,
    )
    assert cleared is False
    assert len(history) == 2  # untouched
    assert pos["_sentinel_last_known_shares"] == 10


def test_baseline_reset_unchanged_shares_no_clear():
    """Same share count tick-after-tick \u2014 history accumulates normally."""
    history = new_pnl_history()
    record_pnl(history, ts=1000.0, pnl=-10.0)
    pos = {"shares": 10, "entry_price": 100.0}
    # Seed cache.
    maybe_reset_pnl_baseline_on_shares_change(
        pos,
        history,
        now_ts=1010.0,
        current_unrealized_pnl=-12.0,
    )
    # Tick again, same shares.
    cleared = maybe_reset_pnl_baseline_on_shares_change(
        pos,
        history,
        now_ts=1020.0,
        current_unrealized_pnl=-15.0,
    )
    assert cleared is False
    assert len(history) == 1  # caller hasn't appended yet; helper untouched


def test_baseline_reset_on_shares_change_clears_and_reseeds():
    """When pos['shares'] changes (Entry-2 fill), history is cleared + reseeded."""
    history = new_pnl_history()
    for i in range(5):
        record_pnl(history, ts=1000.0 + i * 10, pnl=-10.0 - i)
    pos = {"shares": 10, "entry_price": 100.0}
    # Seed.
    maybe_reset_pnl_baseline_on_shares_change(
        pos,
        history,
        now_ts=1050.0,
        current_unrealized_pnl=-15.0,
    )
    assert len(history) == 5

    # Entry-2 fills: shares 10 -> 15, entry_price recomputed to avg.
    pos["shares"] = 15
    pos["entry_price"] = 98.33
    cleared = maybe_reset_pnl_baseline_on_shares_change(
        pos,
        history,
        now_ts=1060.0,
        current_unrealized_pnl=-50.0,
    )
    assert cleared is True
    # History was cleared and reseeded with the (1060, -50.0) sample.
    assert len(history) == 1
    assert history[0] == (1060.0, -50.0)
    assert pos["_sentinel_last_known_shares"] == 15


def test_alarm_a2_does_not_trip_on_artificial_entry_2_delta():
    """Entry-2 step-shift in dollar P&L MUST NOT trip A_FLASH velocity.

    Scenario: 10 shares @ $100 (Entry-1, notional $1000). Position
    drifts mildly -$10 over 30s. Then Entry-2 fills, bumping shares
    to 15 @ avg $98.33 (notional ~$1475). Without the baseline reset,
    the cached samples were computed against the pre-Entry-2 notional,
    and the first post-Entry-2 unrealized P&L (which jumps step-wise
    because the avg entry just moved) compared against a 60s-old
    sample produces an artificial >1% / 60s "velocity" reading.
    """
    history = new_pnl_history()
    pos = {"shares": 10, "entry_price": 100.0}

    # Phase 1 \u2014 Entry-1 only, mild drift over 30s.
    for i, pnl in enumerate([0.0, -2.0, -5.0, -10.0]):
        ts = 1000.0 + i * 10
        # Detector first (no-op since shares unchanged across ticks),
        # then record this tick's sample.
        maybe_reset_pnl_baseline_on_shares_change(
            pos,
            history,
            now_ts=ts,
            current_unrealized_pnl=pnl,
        )
        record_pnl(history, ts=ts, pnl=pnl)

    # Phase 2 \u2014 Entry-2 fills at t=1040. Shares 10 -> 15, avg
    # entry $100 -> ~$98.33. Suppose unrealized P&L jumps from -$10
    # (vs $100 entry) to -$30 (vs new avg, with extra shares at the
    # current price). Without reset, the "1 minute ago" sample is 0.0
    # (t=1000), so delta = -30 - 0 = -$30 over current notional
    # ~$1475 = -2.03% \u2014 A_FLASH trips spuriously.
    pos["shares"] = 15
    pos["entry_price"] = 98.33
    fill_ts = 1040.0
    fill_pnl = -30.0
    cleared = maybe_reset_pnl_baseline_on_shares_change(
        pos,
        history,
        now_ts=fill_ts,
        current_unrealized_pnl=fill_pnl,
    )
    assert cleared is True, "Entry-2 must reset the velocity baseline"
    # Helper reseeds with the fill-time sample; caller would also
    # record but the helper already inserted (fill_ts, fill_pnl).

    # Phase 3 \u2014 30s of mild drift after Entry-2 (-$30 -> -$33). With
    # the baseline reset, the only sample <= now-60s is the post-fill
    # seed at t=1040 (-$30), so delta is -$3 / $1475 = -0.20% \u2014 NO trip.
    for i, pnl in enumerate([-31.0, -32.0, -33.0]):
        ts = 1050.0 + i * 10
        maybe_reset_pnl_baseline_on_shares_change(
            pos,
            history,
            now_ts=ts,
            current_unrealized_pnl=pnl,
        )
        record_pnl(history, ts=ts, pnl=pnl)

    now_ts = 1100.0  # 60s after the Entry-2 fill seed
    notional = 15 * 98.33
    fired = check_alarm_a(
        side=SIDE_LONG,
        unrealized_pnl=-33.0,
        position_value=notional,
        pnl_history=history,
        now_ts=now_ts,
    )
    assert not any(a.alarm == "A_FLASH" for a in fired), (
        "A_FLASH must NOT trip on the post-Entry-2 step-shift artefact"
    )


def test_alarm_a2_still_trips_on_real_velocity_after_entry_2():
    """After baseline reset, A_FLASH must still fire on a genuine post-Entry-2 drop.

    Sanity check that the reset doesn't permanently disable A2: if
    real velocity emerges after the reset, A_FLASH must still trigger.
    """
    history = new_pnl_history()
    pos = {"shares": 10, "entry_price": 100.0}
    record_pnl(history, ts=1000.0, pnl=0.0)
    maybe_reset_pnl_baseline_on_shares_change(
        pos,
        history,
        now_ts=1000.0,
        current_unrealized_pnl=0.0,
    )

    # Entry-2 fills.
    pos["shares"] = 15
    pos["entry_price"] = 98.33
    cleared = maybe_reset_pnl_baseline_on_shares_change(
        pos,
        history,
        now_ts=1010.0,
        current_unrealized_pnl=-30.0,
    )
    assert cleared is True

    # 60s later, real -2% velocity vs the post-Entry-2 baseline.
    notional = 15 * 98.33  # ~$1475
    drop = -0.02 * notional - 30.0  # 2% of notional below the seed
    fired = check_alarm_a(
        side=SIDE_LONG,
        unrealized_pnl=drop,
        position_value=notional,
        pnl_history=history,
        now_ts=1070.0,
    )
    assert any(a.alarm == "A_FLASH" for a in fired), "Real post-reset velocity must still trip A2"


# ---------------------------------------------------------------------------
# Alarm D \u2014 HVP Lock (vAA-1 SENT-D)
# ---------------------------------------------------------------------------


def _hvp_with_peak(initial: float, peak: float):
    """Helper: TradeHVP opened at ``initial`` then ratcheted up to ``peak``.

    v5.26.0 RULING #2: this still constructs a TradeHVP for tests that
    pass it through evaluate_sentinel, but Alarm D no longer reads
    .peak \u2014 it reads from the session-wide HWM keyed by ticker.
    Tests that exercise Alarm D must seed the session HWM separately.
    """
    from engine.momentum_state import TradeHVP

    hvp = TradeHVP()
    hvp.on_strike_open(initial_adx_5m=initial)
    hvp.update(current_adx_5m=peak)
    return hvp


def _seed_session_5m_adx(ticker: str, *samples: float) -> None:
    """Seed the session 5m ADX HWM dict for Alarm D tests.

    Resets the per-ticker HWM first so cross-test ordering does not
    leak peaks. Records each sample via record_session_5m_adx.
    """
    _SESSION_5M_ADX_HWM.pop(ticker, None)
    for s in samples:
        record_session_5m_adx(ticker, s)


def test_alarm_d_fires_when_5m_adx_below_75pct_of_peak():
    """SENT-D: strict less-than 0.75 * peak fires when peak >= 25.

    v5.26.0 RULING #2: peak is read from the session HWM keyed by ticker.
    """
    from engine.sentinel import EXIT_REASON_HVP_LOCK, check_alarm_d

    _seed_session_5m_adx("NVDA", 20.0, 40.0)  # 0.75 * 40 = 30.0
    res = check_alarm_d(
        ticker="NVDA", current_adx_5m=29.99, side=SIDE_LONG
    )
    assert res is not None
    assert res.alarm == "D"
    assert res.reason == EXIT_REASON_HVP_LOCK


def test_alarm_d_does_not_fire_when_peak_below_safety_floor():
    """SENT-D safety floor: session peak < 25 \u2192 no fire even if adx collapses."""
    from engine.sentinel import check_alarm_d

    _seed_session_5m_adx("NVDA", 10.0, 24.99)  # peak just under 25 floor
    # Even adx_now = 0 must not fire \u2014 the session never had momentum.
    res = check_alarm_d(ticker="NVDA", current_adx_5m=0.0, side=SIDE_LONG)
    assert res is None


def test_alarm_d_strict_less_than_does_not_fire_at_exact_75pct():
    """SENT-D: equality at 0.75 * peak is NOT a trigger (strict <)."""
    from engine.sentinel import check_alarm_d

    _seed_session_5m_adx("NVDA", 20.0, 40.0)  # 0.75 * 40 = 30.0 exactly
    res = check_alarm_d(ticker="NVDA", current_adx_5m=30.0, side=SIDE_LONG)
    assert res is None


def test_alarm_d_side_agnostic_long_and_short_both_fire():
    """SENT-D is side-symmetric: ADX is unsigned, so LONG and SHORT
    fire under identical conditions on the same session HWM.
    """
    from engine.sentinel import check_alarm_d

    # Use distinct tickers so the session HWMs are isolated.
    _seed_session_5m_adx("AAPL", 20.0, 40.0)
    _seed_session_5m_adx("NVDA", 20.0, 40.0)
    long_res = check_alarm_d(
        ticker="AAPL", current_adx_5m=20.0, side=SIDE_LONG
    )
    short_res = check_alarm_d(
        ticker="NVDA", current_adx_5m=20.0, side=SIDE_SHORT
    )
    assert long_res is not None and long_res.alarm == "D"
    assert short_res is not None and short_res.alarm == "D"
    # detail string carries the side label for observability
    assert "LONG" in long_res.detail
    assert "SHORT" in short_res.detail


def test_alarm_d_returns_none_when_no_session_hwm_recorded():
    """v5.26.0 RULING #2: when the session HWM is below the safety floor
    (e.g. early-session, no 5m ADX samples yet), Alarm D returns None.
    """
    from engine.sentinel import check_alarm_d

    # Reset the HWM \u2014 no samples recorded.
    _SESSION_5M_ADX_HWM.pop("GOOG", None)
    res = check_alarm_d(
        ticker="GOOG", current_adx_5m=10.0, side=SIDE_LONG
    )
    assert res is None


def test_evaluate_sentinel_threads_alarm_d_when_session_hwm_seeded():
    """SENT-D wires through evaluate_sentinel and contributes to has_full_exit.

    v5.26.0 RULING #2: evaluate_sentinel reads the session HWM via the
    ``ticker`` kwarg; trade_hvp is accepted for back-compat but ignored.
    """
    from engine.sentinel import EXIT_REASON_HVP_LOCK

    _seed_session_5m_adx("TSLA", 20.0, 40.0)
    result = evaluate_sentinel(
        side=SIDE_LONG,
        unrealized_pnl=0.0,  # no A
        position_value=10000.0,
        pnl_history=None,
        now_ts=1000.0,
        last_5m_close=None,  # no B
        last_5m_ema9=None,
        ticker="TSLA",
        current_adx_5m=29.0,  # below 30 \u2192 D fires
    )
    assert any(a.alarm == "D" for a in result.alarms)
    assert result.has_full_exit is True
    assert result.exit_reason == EXIT_REASON_HVP_LOCK


def test_evaluate_sentinel_skips_alarm_d_when_trade_hvp_absent():
    """Defensive: evaluate_sentinel must not raise when trade_hvp is omitted
    (PR-3b is parallel and may merge later \u2014 this code is dead-code-safe
    until then).
    """
    result = evaluate_sentinel(
        side=SIDE_LONG,
        unrealized_pnl=0.0,
        position_value=10000.0,
        pnl_history=None,
        now_ts=1000.0,
        last_5m_close=None,
        last_5m_ema9=None,
        # trade_hvp + current_adx_5m intentionally omitted
    )
    assert not any(a.alarm == "D" for a in result.alarms)
    assert result.fired is False
