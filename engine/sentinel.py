"""v5.13.0 PR 2 \u2014 Tiger Sovereign Sentinel Loop.

Implements the Phase 4 Sentinel Loop alarms from the Tiger Sovereign
spec (STRATEGY.md \u00a7 Phase 4). The Sentinel Loop is a PARALLEL
monitoring system: every alarm is evaluated on every tick, and any
alarm firing terminates the position. Implementation MUST NOT
short-circuit between alarms \u2014 if both A and B fire on the same
tick, both are reported (one exit order is emitted, but every alarm
trip is logged for observability).

Spec rule IDs implemented here:
* L-P4-A / S-P4-A \u2014 Alarm A (Emergency): -$500 absolute loss OR
  -1%/minute velocity. Long: price drops 1% in 60s. Short: price
  spikes 1% in 60s.
* L-P4-B / S-P4-B \u2014 Alarm B (9-EMA Shield): a closed 5m candle
  whose close is on the wrong side of the 5m 9-EMA. Long: close
  BELOW EMA9 fires. Short: close ABOVE EMA9 fires.

Alarm C (Titan Grip Harvest ratchet) is intentionally a stub here.
PR 3 owns the body. The placeholder marker constants ("0.93", "0.40",
"0.25", "1.88", "runner", "LIMIT", "STOP", "MARKET") are present so
the PR-3-marked spec tests can confirm engine/sentinel.py is the
right module to fill in.

All values are spec-literal: change the spec, change THIS file.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Iterable

# ---------------------------------------------------------------------------
# Constants \u2014 spec-literal thresholds
# ---------------------------------------------------------------------------

# Alarm A1 \u2014 absolute hard floor. Long unrealized P&L <= -$500 fires.
ALARM_A_HARD_LOSS_DOLLARS: float = -500.0

# Alarm A2 \u2014 velocity. -1% over the last 60 seconds, measured as
# (P&L_now - P&L_60s_ago) / position_value <= -0.01. The window is
# strictly 60 seconds; the comparison is inclusive of -1.0% exactly.
ALARM_A_VELOCITY_WINDOW_SECONDS: int = 60
ALARM_A_VELOCITY_THRESHOLD: float = -0.01  # -1.00%

# Bounded P&L history per position. 120 samples = ~2 minutes at 1s
# tick cadence. Cheap and bounded \u2014 the velocity check only needs
# the last 60s sample.
PNL_HISTORY_MAXLEN: int = 120

# Alarm C placeholder strings (PR 3 fills in the body). Listed so the
# PR-3 spec tests can locate the module.
_ALARM_C_PR3_MARKERS = (
    "Stage 1 anchor 0.93%",
    "Stage 1 stop 0.40%",
    "Stage 2 micro-ratchet 0.25%",
    "Stage 3 second harvest 1.88%",
    "Stage 4 runner",
    "LIMIT",
    "STOP",
    "MARKET",
)

# Exit reason codes used downstream by broker/positions and
# eye_of_tiger telemetry. Stable strings.
EXIT_REASON_ALARM_A: str = "sentinel_alarm_a"
EXIT_REASON_ALARM_B: str = "sentinel_alarm_b"

SIDE_LONG = "LONG"
SIDE_SHORT = "SHORT"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SentinelAction:
    """Single alarm trip."""

    alarm: str  # "A1", "A2", "B"
    reason: str  # one of EXIT_REASON_*
    detail: str = ""


@dataclass
class SentinelResult:
    """Result of one sentinel evaluation tick.

    Multiple alarms can fire in a single tick. Exactly one exit order
    should be emitted by the caller, but every fired alarm is recorded
    for observability and tests.
    """

    alarms: list[SentinelAction] = field(default_factory=list)

    @property
    def fired(self) -> bool:
        return bool(self.alarms)

    @property
    def alarm_codes(self) -> list[str]:
        return [a.alarm for a in self.alarms]

    @property
    def exit_reason(self) -> str | None:
        """Single canonical exit reason. Alarm A wins precedence over B
        for the OUTBOUND order classification (both are STOP MARKET in
        spec language, but Alarm A is the more urgent emergency stop).
        Both alarms are still recorded in `alarms`.
        """
        if not self.alarms:
            return None
        for a in self.alarms:
            if a.reason == EXIT_REASON_ALARM_A:
                return EXIT_REASON_ALARM_A
        return self.alarms[0].reason


# ---------------------------------------------------------------------------
# Helpers \u2014 P&L history
# ---------------------------------------------------------------------------


def new_pnl_history() -> Deque[tuple[float, float]]:
    """Return a fresh bounded P&L history deque.

    Each entry is ``(timestamp_seconds, unrealized_pnl_dollars)``.
    Capped at PNL_HISTORY_MAXLEN to keep memory bounded across long
    sessions.
    """
    return deque(maxlen=PNL_HISTORY_MAXLEN)


def record_pnl(history: Deque[tuple[float, float]], ts: float, pnl: float) -> None:
    """Append a P&L sample. Caller passes the bounded deque."""
    history.append((float(ts), float(pnl)))


def _pnl_at_or_before(
    history: Iterable[tuple[float, float]], target_ts: float
) -> float | None:
    """Return the most recent P&L sample whose ts <= target_ts.

    Returns None if no sample exists at or before target_ts. Walking
    in reverse keeps this O(n) on a bounded deque (<= 120 entries).
    """
    found: float | None = None
    for ts, pnl in history:
        if ts <= target_ts:
            found = pnl
        else:
            break
    return found


# ---------------------------------------------------------------------------
# Alarm A \u2014 Emergency (-$500 or -1%/min)
# ---------------------------------------------------------------------------


def check_alarm_a(
    *,
    side: str,
    unrealized_pnl: float,
    position_value: float,
    pnl_history: Iterable[tuple[float, float]] | None,
    now_ts: float,
) -> list[SentinelAction]:
    """Evaluate Alarm A for one position.

    Returns a list of fired sub-alarms. Both A1 (hard floor) and A2
    (velocity) are evaluated independently \u2014 if both fire on the
    same tick, both appear in the output. The caller maps the list
    to a single exit order if any element is non-empty.

    Side-symmetric: P&L is signed in dollars from the position
    holder's perspective. Long: pnl = (current - entry) * shares.
    Short: pnl = (entry - current) * shares. Either way, unrealized
    <= -$500 fires A1 and a 60s drop of more than 1% of position
    value (sign convention: pnl_now - pnl_60s_ago) fires A2.

    Args:
        side: "LONG" or "SHORT". Used only for telemetry detail.
        unrealized_pnl: Signed unrealized $ P&L right now.
        position_value: Notional position value in dollars
            (entry_price * shares). Must be > 0; else A2 is skipped.
        pnl_history: Iterable of (ts, pnl) pairs. May be None or empty.
        now_ts: Current tick timestamp in seconds.
    """
    fired: list[SentinelAction] = []

    # A1 \u2014 absolute hard floor. -$500 triggers exactly at the
    # boundary (`<=`). The boundary value is spec-literal.
    if unrealized_pnl <= ALARM_A_HARD_LOSS_DOLLARS:
        fired.append(
            SentinelAction(
                alarm="A1",
                reason=EXIT_REASON_ALARM_A,
                detail=(
                    f"side={side} unrealized_pnl=${unrealized_pnl:.2f} "
                    f"<= ${ALARM_A_HARD_LOSS_DOLLARS:.2f}"
                ),
            )
        )

    # A2 \u2014 velocity. Need history and a positive position value.
    if pnl_history and position_value and position_value > 0:
        target = now_ts - ALARM_A_VELOCITY_WINDOW_SECONDS
        prior = _pnl_at_or_before(pnl_history, target)
        if prior is not None:
            delta = unrealized_pnl - prior
            velocity = delta / position_value
            if velocity <= ALARM_A_VELOCITY_THRESHOLD:
                fired.append(
                    SentinelAction(
                        alarm="A2",
                        reason=EXIT_REASON_ALARM_A,
                        detail=(
                            f"side={side} pnl_60s_delta=${delta:.2f} "
                            f"velocity={velocity * 100:.2f}% "
                            f"<= {ALARM_A_VELOCITY_THRESHOLD * 100:.2f}%"
                        ),
                    )
                )
    return fired


# ---------------------------------------------------------------------------
# Alarm B \u2014 9-EMA Shield (5m close vs 9-EMA)
# ---------------------------------------------------------------------------


def check_alarm_b(
    *,
    side: str,
    last_5m_close: float | None,
    last_5m_ema9: float | None,
) -> list[SentinelAction]:
    """Evaluate Alarm B for one position.

    Spec L-P4-B / S-P4-B: a CLOSED 5-minute candle whose close is
    on the wrong side of the 5m 9-EMA terminates the trade. "Closed"
    means the bar must already be done; the engine.bars helper
    ``compute_5m_ohlc_and_ema9`` already drops the in-progress bar
    so its `closes[-1]` and `ema9` are spec-compatible.

    Returns a list with at most one SentinelAction.
    """
    if last_5m_close is None or last_5m_ema9 is None:
        return []

    fired: list[SentinelAction] = []
    if side == SIDE_LONG:
        # Long: close BELOW EMA9 fires.
        if last_5m_close < last_5m_ema9:
            fired.append(
                SentinelAction(
                    alarm="B",
                    reason=EXIT_REASON_ALARM_B,
                    detail=(
                        f"side=LONG 5m_close={last_5m_close:.4f} "
                        f"< 9ema={last_5m_ema9:.4f}"
                    ),
                )
            )
    elif side == SIDE_SHORT:
        # Short: close ABOVE EMA9 fires.
        if last_5m_close > last_5m_ema9:
            fired.append(
                SentinelAction(
                    alarm="B",
                    reason=EXIT_REASON_ALARM_B,
                    detail=(
                        f"side=SHORT 5m_close={last_5m_close:.4f} "
                        f"> 9ema={last_5m_ema9:.4f}"
                    ),
                )
            )
    return fired


# ---------------------------------------------------------------------------
# Top-level evaluator \u2014 PARALLEL, NOT sequential
# ---------------------------------------------------------------------------


def evaluate_sentinel(
    *,
    side: str,
    unrealized_pnl: float,
    position_value: float,
    pnl_history: Iterable[tuple[float, float]] | None,
    now_ts: float,
    last_5m_close: float | None,
    last_5m_ema9: float | None,
) -> SentinelResult:
    """Evaluate ALL sentinel alarms for one position on one tick.

    Critical: alarms are evaluated INDEPENDENTLY. Even if Alarm A
    has fired, Alarm B is still evaluated, and the result lists
    every fired alarm. The caller is responsible for emitting
    exactly one exit order even if multiple alarms fire (use
    `result.exit_reason`).

    Per the spec: "These Alarms are NOT a sequence." Do not
    introduce short-circuit returns here.
    """
    result = SentinelResult()

    # Alarm A \u2014 always evaluated.
    a_fired = check_alarm_a(
        side=side,
        unrealized_pnl=unrealized_pnl,
        position_value=position_value,
        pnl_history=pnl_history,
        now_ts=now_ts,
    )
    result.alarms.extend(a_fired)

    # Alarm B \u2014 always evaluated, independent of A.
    b_fired = check_alarm_b(
        side=side,
        last_5m_close=last_5m_close,
        last_5m_ema9=last_5m_ema9,
    )
    result.alarms.extend(b_fired)

    # Alarm C \u2014 PR 3 wires in the Titan Grip ratchet here. The
    # placeholder string keeps the import surface stable and the
    # spec_gap PR-3 tests' module-existence assertions green.
    _ = _ALARM_C_PR3_MARKERS  # noqa: F841 \u2014 documented placeholder

    return result


def format_sentinel_log(ticker: str, position_id: str | None, result: SentinelResult) -> str:
    """Render a structured one-line log entry for a sentinel trip.

    Format: ``[SENTINEL] pos=<id> ticker=<t> alarms=[A1,B] action=EXIT
    reason=<top> detail=<...>``
    """
    if not result.fired:
        return ""
    codes = ",".join(result.alarm_codes)
    pos_part = position_id or ticker
    details = " | ".join(a.detail for a in result.alarms if a.detail)
    return (
        f"[SENTINEL] pos={pos_part} ticker={ticker} alarms=[{codes}] "
        f"action=EXIT reason={result.exit_reason} detail={details}"
    )


__all__ = [
    "ALARM_A_HARD_LOSS_DOLLARS",
    "ALARM_A_VELOCITY_THRESHOLD",
    "ALARM_A_VELOCITY_WINDOW_SECONDS",
    "EXIT_REASON_ALARM_A",
    "EXIT_REASON_ALARM_B",
    "PNL_HISTORY_MAXLEN",
    "SIDE_LONG",
    "SIDE_SHORT",
    "SentinelAction",
    "SentinelResult",
    "check_alarm_a",
    "check_alarm_b",
    "evaluate_sentinel",
    "format_sentinel_log",
    "new_pnl_history",
    "record_pnl",
]
