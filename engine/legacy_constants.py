"""Live trading constants + sizing helpers carved out of the retired
eye_of_tiger.py module in v10.0.1.

History: these used to live in eye_of_tiger.py alongside the
Section I / Boundary Hold / Entry-1 / Entry-2 / Volume Bucket
admission gates. Those gates were deleted in v10.0.1 once the audit
confirmed the Keystone backtest path never ran through them (live
was running a more-conservative strategy than the +$50k/yr the
backtest measured). The constants + sizing helpers below survived
the gate deletion because broker/* still consumes them for share
sizing, stop calculation, and the regime-B short amplifier.

Single source of truth -- importers throughout broker/* and
engine/* read these directly. The v611 regime-B tests patch them
via `engine.legacy_constants.<NAME>`.
"""
from __future__ import annotations

import os as _os


# ---------------------------------------------------------------------------
# Env-read helpers (same semantics as the retired eye_of_tiger._read_*
# helpers -- silent fallback to default on parse error).
# ---------------------------------------------------------------------------


def _read_int(env_name: str, default: int) -> int:
    try:
        v = _os.getenv(env_name)
        return int(v) if v is not None else default
    except ValueError:
        return default


def _read_float(env_name: str, default: float) -> float:
    try:
        v = _os.getenv(env_name)
        return float(v) if v is not None else default
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Sizing constants
# ---------------------------------------------------------------------------

# Entry-1 / Entry-2 size split. broker/orders.paper_shares_for() applies
# ENTRY_1_SIZE_PCT to the dollars-per-entry budget; broker/positions
# Entry-2 add-on uses ENTRY_2_SIZE_PCT. Sum should equal 1.0 (asserted
# in broker/positions when Entry-2 fires).
ENTRY_1_SIZE_PCT = 0.50
ENTRY_2_SIZE_PCT = 0.50

# Asymmetric percent-of-entry stops (v6.4.1). Long stop = entry * (1 - PCT).
# Short stop = entry * (1 + PCT). Env-overridable so an operator can
# widen the rail mid-day without a redeploy.
STOP_PCT_LONG = _read_float("STOP_PCT_LONG", 0.005)   # 50 bp default
STOP_PCT_SHORT = _read_float("STOP_PCT_SHORT", 0.003)  # 30 bp default

# ---------------------------------------------------------------------------
# Cooldowns (v6.11.13)
# ---------------------------------------------------------------------------

# Same-ticker post-exit cooldown. Independent of post-loss-cooldown
# (which is about avoiding revenge trades on losers); this window
# gives the broker time to reconcile the prior protective stop order
# before a new entry on the same ticker is submitted, defending
# against Alpaca's wash-trade rejection (error 40310000).
POST_EXIT_SAME_TICKER_COOLDOWN_SEC = _read_int(
    "POST_EXIT_SAME_TICKER_COOLDOWN_SEC", 10
)

# Cancel-first-then-enter timeout (v6.14.0). When a new entry would
# race against a still-open opposite-side protective order, the entry
# path cancels the opposing order and polls broker state for up to
# this many milliseconds before submitting the new entry. 1500 ms
# = 5x the empirical 50-300 ms cancel-ack window.
CANCEL_ACK_TIMEOUT_MS = _read_int("CANCEL_ACK_TIMEOUT_MS", 1500)

# ---------------------------------------------------------------------------
# Side enum (string for JSON-serialisability)
# ---------------------------------------------------------------------------

SIDE_LONG = "LONG"
SIDE_SHORT = "SHORT"

# ---------------------------------------------------------------------------
# v6.11.0 SPY Regime-B Short Amplification env contract.
# Tests at tests/test_v611_regime_b.py patch V611_REGIME_B_ENABLED
# via `mock.patch("engine.legacy_constants.V611_REGIME_B_ENABLED", ...)`.
# ---------------------------------------------------------------------------

V611_REGIME_B_SHORT_SCALE_MULT = float(
    _os.getenv("V611_REGIME_B_SHORT_SCALE_MULT", "1.5")
)
V611_REGIME_B_SHORT_ARM_HHMM_ET = _os.getenv(
    "V611_REGIME_B_SHORT_ARM_HHMM_ET", "10:00"
)
V611_REGIME_B_SHORT_DISARM_HHMM_ET = _os.getenv(
    "V611_REGIME_B_SHORT_DISARM_HHMM_ET", "11:00"
)
V611_REGIME_B_ENABLED = _os.getenv("V611_REGIME_B_ENABLED", "1") == "1"

# ---------------------------------------------------------------------------
# Portfolio-scaled sovereign brake (v5.27.0). Used by engine/sentinel.py
# for the Alarm-A deeper-rail R-2 hard stop.
# ---------------------------------------------------------------------------

SOVEREIGN_BRAKE_DOLLARS = -500.0
SOVEREIGN_BRAKE_PORTFOLIO_PCT = 0.005  # 0.5% per-trade
SOVEREIGN_BRAKE_FLOOR_DOLLARS = 100.0
SOVEREIGN_BRAKE_CEILING_DOLLARS = 500.0


def scaled_sovereign_brake_dollars(portfolio_value: float | None) -> float:
    """Per-trade Sovereign Brake threshold scaled to portfolio size.

    Returns a NEGATIVE dollar threshold (e.g. -250.0 means a single
    position trips the brake at -$250 unrealized). Falls back to the
    legacy absolute SOVEREIGN_BRAKE_DOLLARS when portfolio_value is
    None or non-positive so warm-up paths stay deterministic.
    Clamped to [FLOOR, CEILING].
    """
    if portfolio_value is None or portfolio_value <= 0:
        return float(SOVEREIGN_BRAKE_DOLLARS)
    raw = float(portfolio_value) * SOVEREIGN_BRAKE_PORTFOLIO_PCT
    clamped = max(
        SOVEREIGN_BRAKE_FLOOR_DOLLARS,
        min(SOVEREIGN_BRAKE_CEILING_DOLLARS, raw),
    )
    return -clamped


# Daily circuit breaker -- portfolio-scaled per-session realized-loss halt.
DAILY_CIRCUIT_BREAKER_DOLLARS = -1500.0
DAILY_CIRCUIT_BREAKER_PORTFOLIO_PCT = 0.015  # 1.5% per-day
DAILY_CIRCUIT_BREAKER_FLOOR_DOLLARS = 300.0
DAILY_CIRCUIT_BREAKER_CEILING_DOLLARS = 1500.0


def scaled_daily_circuit_breaker_dollars(portfolio_value: float | None) -> float:
    """Daily realized-loss halt threshold scaled to portfolio size.

    Returns a NEGATIVE dollar threshold. Falls back to the legacy
    absolute DAILY_CIRCUIT_BREAKER_DOLLARS when portfolio_value is
    None or non-positive. Clamped to
    [DAILY_CIRCUIT_BREAKER_FLOOR_DOLLARS,
    DAILY_CIRCUIT_BREAKER_CEILING_DOLLARS].
    """
    if portfolio_value is None or portfolio_value <= 0:
        return float(DAILY_CIRCUIT_BREAKER_DOLLARS)
    raw = float(portfolio_value) * DAILY_CIRCUIT_BREAKER_PORTFOLIO_PCT
    clamped = max(
        DAILY_CIRCUIT_BREAKER_FLOOR_DOLLARS,
        min(DAILY_CIRCUIT_BREAKER_CEILING_DOLLARS, raw),
    )
    return -clamped


# ---------------------------------------------------------------------------
# v15.0 / vAA-1 Strike sizing (Phase 3 momentum-sensitive tiers)
# Live broker/orders.py:execute_breakout uses this to decide FULL vs
# SCALED_A vs SCALED_B vs WAIT for each strike entry.
# ---------------------------------------------------------------------------

P3_AUTH_DI_THRESHOLD = 25.0   # 5m DMI master anchor (strict >)
P3_FULL_DI_THRESHOLD = 30.0   # 1m DI for FULL / add-on tier (strict >)
P3_SCALED_A_DI_LO = 22.0      # 1m DI lower bound for SCALED_A (inclusive)
P3_SCALED_A_DI_HI = 30.0      # 1m DI upper bound for SCALED_A (inclusive)

SIZE_LABEL_FULL = "FULL"
SIZE_LABEL_SCALED_A = "SCALED_A"
SIZE_LABEL_SCALED_B = "SCALED_B"
SIZE_LABEL_WAIT = "WAIT"


class StrikeSizingDecision:
    """Decision record returned by ``evaluate_strike_sizing``."""

    __slots__ = ("size_label", "shares_to_buy", "reason")

    def __init__(self, size_label: str, shares_to_buy: int, reason: str = "") -> None:
        self.size_label = size_label
        self.shares_to_buy = int(shares_to_buy)
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover -- debug aid only
        return (
            f"StrikeSizingDecision(size_label={self.size_label!r}, "
            f"shares_to_buy={self.shares_to_buy}, reason={self.reason!r})"
        )


def evaluate_strike_sizing(
    *,
    side: str,
    di_5m: float | None,
    di_1m: float | None,
    is_fresh_extreme: bool,
    intended_shares: int,
    held_shares_this_strike: int = 0,
    alarm_e_blocked: bool = False,
) -> StrikeSizingDecision:
    """Pure decision: how big a Strike entry should fire.

    Returns FULL, SCALED_A (half size), SCALED_B (half-size add-on),
    or WAIT. Inputs are side-correct DMI values: for LONG pass DI+,
    for SHORT pass DI- (caller maps the polarity). Never raises on
    bad inputs -- missing DI values deterministically degrade to WAIT.
    """
    side_u = (side or "").strip().upper()
    if side_u not in (SIDE_LONG, SIDE_SHORT):
        return StrikeSizingDecision(SIZE_LABEL_WAIT, 0, f"unknown side {side!r}")

    intended = max(0, int(intended_shares))
    if intended <= 0:
        return StrikeSizingDecision(SIZE_LABEL_WAIT, 0, "intended_shares <= 0")

    # Master anchor: side-correct 5m DMI must STRICTLY exceed P3_AUTH.
    if di_5m is None or float(di_5m) <= P3_AUTH_DI_THRESHOLD:
        return StrikeSizingDecision(
            SIZE_LABEL_WAIT, 0,
            f"5m DI {di_5m} <= {P3_AUTH_DI_THRESHOLD} (anchor fail)",
        )

    if di_1m is None:
        return StrikeSizingDecision(SIZE_LABEL_WAIT, 0, "1m DI is None")
    di1 = float(di_1m)

    held = max(0, int(held_shares_this_strike))

    # Add-on (SCALED-B): trade already holds shares from a prior tier.
    if held > 0:
        if alarm_e_blocked:
            return StrikeSizingDecision(
                SIZE_LABEL_WAIT, 0, "add-on blocked by Alarm E PRE",
            )
        if not is_fresh_extreme:
            return StrikeSizingDecision(
                SIZE_LABEL_WAIT, 0,
                "add-on requires fresh extreme (NHOD/NLOD)",
            )
        if di1 > P3_FULL_DI_THRESHOLD:
            return StrikeSizingDecision(
                SIZE_LABEL_SCALED_B,
                intended // 2,
                f"add-on SCALED_B 1m DI {di1:.2f} > {P3_FULL_DI_THRESHOLD}",
            )
        return StrikeSizingDecision(
            SIZE_LABEL_WAIT, 0,
            f"add-on requires 1m DI > {P3_FULL_DI_THRESHOLD}",
        )

    # First fill of this Strike (held == 0).
    if di1 > P3_FULL_DI_THRESHOLD:
        return StrikeSizingDecision(
            SIZE_LABEL_FULL, intended,
            f"FULL 1m DI {di1:.2f} > {P3_FULL_DI_THRESHOLD}",
        )
    if P3_SCALED_A_DI_LO <= di1 <= P3_SCALED_A_DI_HI:
        return StrikeSizingDecision(
            SIZE_LABEL_SCALED_A, intended // 2,
            f"SCALED_A 1m DI {di1:.2f} in [{P3_SCALED_A_DI_LO},{P3_SCALED_A_DI_HI}]",
        )
    return StrikeSizingDecision(
        SIZE_LABEL_WAIT, 0,
        f"1m DI {di1:.2f} below {P3_SCALED_A_DI_LO} (no tier match)",
    )
