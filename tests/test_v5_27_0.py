"""v5.27.0 \u2014 unit tests for the four behavioural changes:

1. Alarm B 2-bar EMA9 confirmation (default 1, prod opts in to 2).
2. ``scaled_sovereign_brake_dollars`` / ``scaled_daily_circuit_breaker_dollars``
   floor / ceiling clamps.
3. Share-aware P&L pairing in the v5.11 backtest harness.
4. NFLX / ORCL present in the runtime ``tickers.json`` and the
   ``TICKERS_DEFAULT`` fallback.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from engine.sentinel import (
    ALARM_A_HARD_LOSS_DOLLARS,
    ALARM_B_CONFIRM_BARS,
    SIDE_LONG,
    SIDE_SHORT,
    check_alarm_a,
    check_alarm_b,
    evaluate_sentinel,
    new_pnl_history,
)
from eye_of_tiger import (
    DAILY_CIRCUIT_BREAKER_CEILING_DOLLARS,
    DAILY_CIRCUIT_BREAKER_DOLLARS,
    DAILY_CIRCUIT_BREAKER_FLOOR_DOLLARS,
    SOVEREIGN_BRAKE_CEILING_DOLLARS,
    SOVEREIGN_BRAKE_DOLLARS,
    SOVEREIGN_BRAKE_FLOOR_DOLLARS,
    scaled_daily_circuit_breaker_dollars,
    scaled_sovereign_brake_dollars,
)


# ---------------------------------------------------------------------------
# 1. Alarm B 2-bar EMA9 confirmation
# ---------------------------------------------------------------------------


def test_alarm_b_constant_is_2():
    """Production wiring opts in to 2-bar confirm via this constant."""
    assert ALARM_B_CONFIRM_BARS == 2


def test_alarm_b_2bar_long_both_below_fires():
    """Long: prev close < prev ema9 AND last close < last ema9 \u2192 FIRE."""
    fired = check_alarm_b(
        side=SIDE_LONG,
        last_5m_close=99.0,
        last_5m_ema9=100.0,
        prev_5m_close=99.5,
        prev_5m_ema9=100.5,
        confirm_bars=2,
    )
    assert len(fired) == 1
    assert fired[0].alarm == "B"


def test_alarm_b_2bar_long_only_last_below_does_not_fire():
    """Long: only the most recent bar is below \u2192 sit out under 2-bar gate."""
    fired = check_alarm_b(
        side=SIDE_LONG,
        last_5m_close=99.0,
        last_5m_ema9=100.0,
        prev_5m_close=101.0,  # prev was ABOVE the EMA
        prev_5m_ema9=100.5,
        confirm_bars=2,
    )
    assert fired == []


def test_alarm_b_2bar_long_only_prev_below_does_not_fire():
    fired = check_alarm_b(
        side=SIDE_LONG,
        last_5m_close=101.0,  # last is ABOVE
        last_5m_ema9=100.0,
        prev_5m_close=99.5,
        prev_5m_ema9=100.5,
        confirm_bars=2,
    )
    assert fired == []


def test_alarm_b_2bar_short_both_above_fires():
    fired = check_alarm_b(
        side=SIDE_SHORT,
        last_5m_close=101.0,
        last_5m_ema9=100.0,
        prev_5m_close=100.5,
        prev_5m_ema9=100.0,
        confirm_bars=2,
    )
    assert len(fired) == 1
    assert fired[0].alarm == "B"


def test_alarm_b_2bar_missing_prev_does_not_fire():
    """Insufficient history: 2-bar gate must sit out, never raise."""
    fired = check_alarm_b(
        side=SIDE_LONG,
        last_5m_close=99.0,
        last_5m_ema9=100.0,
        prev_5m_close=None,
        prev_5m_ema9=None,
        confirm_bars=2,
    )
    assert fired == []


def test_alarm_b_default_1bar_back_compat():
    """Default ``confirm_bars=1`` keeps the spec-strict 1-bar behaviour."""
    fired = check_alarm_b(
        side=SIDE_LONG,
        last_5m_close=99.0,
        last_5m_ema9=100.0,
        # prev unset \u2192 1-bar still fires.
    )
    assert len(fired) == 1


# ---------------------------------------------------------------------------
# 2. Scaled brake helpers
# ---------------------------------------------------------------------------


def test_scaled_sovereign_brake_falls_back_when_no_portfolio():
    """None / 0 / negative \u2192 legacy -$500 (sign-correct, negative)."""
    assert scaled_sovereign_brake_dollars(None) == float(SOVEREIGN_BRAKE_DOLLARS)
    assert scaled_sovereign_brake_dollars(0) == float(SOVEREIGN_BRAKE_DOLLARS)
    assert scaled_sovereign_brake_dollars(-1000) == float(SOVEREIGN_BRAKE_DOLLARS)


def test_scaled_sovereign_brake_floor_at_small_portfolio():
    """$10K * 0.5%% = $50 \u2192 floor $100. Returned negative \u2192 -$100."""
    assert scaled_sovereign_brake_dollars(10_000.0) == -float(SOVEREIGN_BRAKE_FLOOR_DOLLARS)


def test_scaled_sovereign_brake_proportional_in_band():
    """$50K * 0.5%% = $250 \u2192 in-band \u2192 -$250."""
    assert scaled_sovereign_brake_dollars(50_000.0) == pytest.approx(-250.0)


def test_scaled_sovereign_brake_calibrated_at_100k():
    """$100K * 0.5%% = $500 \u2192 matches the legacy absolute (-$500)."""
    assert scaled_sovereign_brake_dollars(100_000.0) == pytest.approx(-500.0)


def test_scaled_sovereign_brake_ceiling_at_large_portfolio():
    """$1M * 0.5%% = $5K \u2192 ceiling $500 \u2192 -$500."""
    assert scaled_sovereign_brake_dollars(1_000_000.0) == -float(SOVEREIGN_BRAKE_CEILING_DOLLARS)


def test_scaled_daily_circuit_breaker_floor():
    """$10K * 1.5%% = $150 \u2192 floor $300 \u2192 -$300."""
    assert scaled_daily_circuit_breaker_dollars(10_000.0) == -float(
        DAILY_CIRCUIT_BREAKER_FLOOR_DOLLARS
    )


def test_scaled_daily_circuit_breaker_proportional():
    """$50K * 1.5%% = $750 \u2192 in-band \u2192 -$750."""
    assert scaled_daily_circuit_breaker_dollars(50_000.0) == pytest.approx(-750.0)


def test_scaled_daily_circuit_breaker_calibrated_at_100k():
    """$100K * 1.5%% = $1500 \u2192 matches legacy (-$1500)."""
    assert scaled_daily_circuit_breaker_dollars(100_000.0) == pytest.approx(-1500.0)


def test_scaled_daily_circuit_breaker_ceiling():
    """$1M * 1.5%% = $15K \u2192 ceiling $1500 \u2192 -$1500."""
    assert scaled_daily_circuit_breaker_dollars(1_000_000.0) == -float(
        DAILY_CIRCUIT_BREAKER_CEILING_DOLLARS
    )


def test_scaled_daily_circuit_breaker_fallback():
    assert scaled_daily_circuit_breaker_dollars(None) == float(DAILY_CIRCUIT_BREAKER_DOLLARS)


# ---------------------------------------------------------------------------
# 3. evaluate_sentinel forwards portfolio_value into Alarm A threshold
# ---------------------------------------------------------------------------


def test_evaluate_sentinel_uses_scaled_threshold_for_small_portfolio():
    """At $10K portfolio the floor is -$100 \u2192 a -$120 unrealized fires."""
    history = new_pnl_history()
    result = evaluate_sentinel(
        side=SIDE_LONG,
        unrealized_pnl=-120.0,
        position_value=2_000.0,
        pnl_history=history,
        now_ts=1_000.0,
        last_5m_close=None,
        last_5m_ema9=None,
        portfolio_value=10_000.0,
    )
    a_alarms = [a for a in result.alarms if a.alarm == "A_LOSS"]
    assert len(a_alarms) >= 1


def test_evaluate_sentinel_legacy_threshold_when_portfolio_missing():
    """No portfolio_value \u2192 -$120 must NOT fire (legacy threshold -$500)."""
    history = new_pnl_history()
    result = evaluate_sentinel(
        side=SIDE_LONG,
        unrealized_pnl=-120.0,
        position_value=2_000.0,
        pnl_history=history,
        now_ts=1_000.0,
        last_5m_close=None,
        last_5m_ema9=None,
    )
    a_alarms = [a for a in result.alarms if a.alarm == "A_LOSS"]
    assert a_alarms == []


def test_evaluate_sentinel_legacy_at_minus_500_fires_without_portfolio():
    """Sanity: spec-default still fires at \u2264 -$500 with no portfolio."""
    history = new_pnl_history()
    result = evaluate_sentinel(
        side=SIDE_LONG,
        unrealized_pnl=ALARM_A_HARD_LOSS_DOLLARS,
        position_value=2_000.0,
        pnl_history=history,
        now_ts=1_000.0,
        last_5m_close=None,
        last_5m_ema9=None,
    )
    a_alarms = [a for a in result.alarms if a.alarm == "A_LOSS"]
    assert len(a_alarms) >= 1


# ---------------------------------------------------------------------------
# 4. NFLX / ORCL universe presence
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_tickers_json_includes_nflx_and_orcl():
    raw = json.loads((REPO_ROOT / "tickers.json").read_text())
    syms = {str(s).upper() for s in raw["tickers"]}
    assert "NFLX" in syms
    assert "ORCL" in syms


def test_tickers_default_source_includes_nflx_and_orcl():
    """Module-level default fallback list (read from source to avoid
    pulling the rest of the trade_genius runtime into the test)."""
    src = (REPO_ROOT / "trade_genius.py").read_text()
    # Grab the TICKERS_DEFAULT block.
    start = src.index("TICKERS_DEFAULT = [")
    end = src.index("]", start)
    block = src[start:end]
    assert '"NFLX"' in block
    assert '"ORCL"' in block


def test_backtest_default_tickers_includes_nflx_and_orcl():
    from backtest.replay_v511_full import DEFAULT_TICKERS

    syms = {str(s).upper() for s in DEFAULT_TICKERS}
    assert "NFLX" in syms
    assert "ORCL" in syms


# ---------------------------------------------------------------------------
# 5. Share-aware P&L pairing in the v5.11 backtest replay
# ---------------------------------------------------------------------------


def _entry(ticker: str, ts: float, price: float, shares: int | None = None) -> Dict[str, Any]:
    e: Dict[str, Any] = {
        "ticker": ticker,
        "ts": ts,
        "price": price,
        "side": "long",
    }
    if shares is not None:
        e["shares"] = shares
    return e


def _exit(ticker: str, ts: float, price: float, shares: int | None = None) -> Dict[str, Any]:
    x: Dict[str, Any] = {
        "ticker": ticker,
        "ts": ts,
        "price": price,
        "side": "long",
    }
    if shares is not None:
        x["shares"] = shares
    return x


def test_pair_entries_to_exits_uses_shares_when_present():
    from backtest.replay_v511_full import pair_entries_to_exits, summarize

    entries = [_entry("NFLX", 1.0, 600.0, 10)]
    exits = [_exit("NFLX", 2.0, 605.0, 10)]
    pairs = pair_entries_to_exits(entries, exits)
    assert len(pairs) == 1
    pair = pairs[0]
    # 10 shares * (605 - 600) = $50 dollar P&L (NOT $5 per-share).
    assert pair["pnl_dollars"] == pytest.approx(50.0)
    assert pair["shares"] == 10

    s = summarize(entries, exits, pairs)
    assert s["total_pnl"] == pytest.approx(50.0)
    assert s["pairs_missing_shares"] == 0


def test_pair_entries_to_exits_falls_back_per_share_when_shares_missing():
    from backtest.replay_v511_full import pair_entries_to_exits, summarize

    entries = [_entry("ORCL", 1.0, 200.0)]
    exits = [_exit("ORCL", 2.0, 203.0)]
    pairs = pair_entries_to_exits(entries, exits)
    assert len(pairs) == 1
    pair = pairs[0]
    # Per-share fallback: $3 P&L recorded with shares=None.
    assert pair["pnl_dollars"] == pytest.approx(3.0)
    assert pair["shares"] is None

    s = summarize(entries, exits, pairs)
    assert s["pairs_missing_shares"] == 1
