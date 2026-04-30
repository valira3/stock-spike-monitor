"""v5.13.0 PR 2-3 / v5.15.0 PR-4 \u2014 Tiger Sovereign Sentinel Loop.

Implements the Phase 4 Sentinel Loop alarms from the Tiger Sovereign
spec (STRATEGY.md \u00a7 Phase 4). The Sentinel Loop is a PARALLEL
monitoring system: every alarm is evaluated on every tick, and any
alarm firing terminates the position. Implementation MUST NOT
short-circuit between alarms \u2014 if both A and B fire on the same
tick, both are reported (one exit order is emitted, but every alarm
trip is logged for observability).

Spec rule IDs implemented here:
* L-P4-A / S-P4-A \u2014 Alarm A (Emergency): -$500 absolute loss OR
  -1%/minute velocity. Sub-codes A_LOSS / A_FLASH (vAA-1 rename).
* L-P4-B / S-P4-B \u2014 Alarm B (9-EMA Shield): closed 5m candle whose
  close is on the wrong side of the 5m 9-EMA.
* SENT-C velocity ratchet (vAA-1, replaces the deleted Titan Grip
  Harvest staircase): three strictly-decreasing 1m ADX samples
  tighten the protective stop by 0.25%. Body lives in
  engine/velocity_ratchet.py; this module wires it into the parallel
  evaluator.

Alarm priority on multi-fire: Alarm A wins for the OUTBOUND order
classification (full position exit overrides partial harvests),
Alarm B wins over C for the same reason. ALL fired alarms remain
in `result.alarms` for observability \u2014 nothing is suppressed.

All values are spec-literal: change the spec, change THIS file
(or engine/velocity_ratchet.py).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Deque, Iterable, Optional

from engine.momentum_state import TradeHVP
from engine.velocity_ratchet import (
    EXIT_REASON_VELOCITY_RATCHET,
    RATCHET_STOP_PCT,
    RatchetDecision,
    evaluate_velocity_ratchet,
)

if TYPE_CHECKING:
    from engine.momentum_state import ADXTrendWindow, DivergenceMemory

# ---------------------------------------------------------------------------
# Constants \u2014 spec-literal thresholds
# ---------------------------------------------------------------------------

# Alarm A_LOSS \u2014 absolute hard floor. Long unrealized P&L <= -$500 fires.
# (vAA-1 rename: legacy A_one code replaced by A_LOSS; legacy strings deleted.)
ALARM_A_HARD_LOSS_DOLLARS: float = -500.0

# Alarm A_FLASH \u2014 velocity. -1% over the last 60 seconds, measured as
# (P&L_now - P&L_60s_ago) / position_value <= -0.01. The window is
# strictly 60 seconds; the comparison is inclusive of -1.0% exactly.
ALARM_A_VELOCITY_WINDOW_SECONDS: int = 60
ALARM_A_VELOCITY_THRESHOLD: float = -0.01  # -1.00%

# Bounded P&L history per position. 120 samples = ~2 minutes at 1s
# tick cadence. Cheap and bounded \u2014 the velocity check only needs
# the last 60s sample.
PNL_HISTORY_MAXLEN: int = 120

# Exit reason codes used downstream by broker/positions and
# eye_of_tiger telemetry. Stable strings.
EXIT_REASON_ALARM_A: str = "sentinel_alarm_a"
EXIT_REASON_ALARM_B: str = "sentinel_alarm_b"
# Alarm D \u2014 HVP Lock. Full MARKET exit when 5m ADX has decayed
# below 75% of the per-Strike high-water-mark, gated by a safety
# floor so trades that never built momentum cannot be flushed.
EXIT_REASON_HVP_LOCK: str = "HVP_LOCK"
ALARM_D_HVP_FRACTION: float = 0.75
# Safety floor flagged for review at PR-5 merge: a peak below 25
# means the trade never registered as a real trend, so the lock is
# suppressed.
ALARM_D_SAFETY_FLOOR_ADX: float = 25.0

# vAA-1: the new Alarm C reason. Kept as ``EXIT_REASON_ALARM_C`` for
# backward import compat with broker/positions.py.
EXIT_REASON_ALARM_C: str = EXIT_REASON_VELOCITY_RATCHET

SIDE_LONG = "LONG"
SIDE_SHORT = "SHORT"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SentinelAction:
    """Single alarm trip.

    For alarms that propose a new protective stop (currently Alarm C
    Velocity Ratchet), ``detail_stop_price`` carries the proposed
    price; otherwise it is None.
    """

    alarm: str  # "A_LOSS", "A_FLASH", "B", "C1..C4", "D", "E"
    reason: str  # one of EXIT_REASON_*
    detail: str = ""
    detail_stop_price: Optional[float] = None


@dataclass
class SentinelResult:
    """Result of one sentinel evaluation tick.

    Multiple alarms can fire in a single tick. The caller decides
    whether to emit a full exit (Alarm A or B \u2014 100% close) or a
    stop-tighten (Alarm C). Every fired alarm is recorded for
    observability and tests.

    Priority on multi-fire (spec PR-3 \u00a7 Sentinel parallel-not-
    sequential):
      A wins over C \u2014 full exit overrides stop-tighten
      B wins over C \u2014 same reasoning (full close on 9-EMA shield)
      A and B can co-exist; A wins for OUTBOUND order classification
    All fired alarms still appear in `alarms` so tests / dashboards
    can audit every trip.
    """

    alarms: list[SentinelAction] = field(default_factory=list)
    # Kept for backward compat with broker/positions.py and
    # v5_13_2_snapshot. Always an empty list under vAA-1: Velocity
    # Ratchet emits via ``alarms`` only.
    titan_grip_actions: list = field(default_factory=list)

    @property
    def fired(self) -> bool:
        return bool(self.alarms)

    @property
    def alarm_codes(self) -> list[str]:
        return [a.alarm for a in self.alarms]

    @property
    def has_full_exit(self) -> bool:
        """True if Alarm A, B, or D fired \u2014 caller must do a full close
        and ignore any C stop-tighten on the same tick.
        """
        for a in self.alarms:
            if a.reason in (
                EXIT_REASON_ALARM_A,
                EXIT_REASON_ALARM_B,
                EXIT_REASON_HVP_LOCK,
            ):
                return True
        return False

    @property
    def exit_reason(self) -> str | None:
        """Single canonical exit reason. Priority: A > B > D > C.
        All alarms remain recorded in `alarms` regardless.
        """
        if not self.alarms:
            return None
        for a in self.alarms:
            if a.reason == EXIT_REASON_ALARM_A:
                return EXIT_REASON_ALARM_A
        for a in self.alarms:
            if a.reason == EXIT_REASON_ALARM_B:
                return EXIT_REASON_ALARM_B
        for a in self.alarms:
            if a.reason == EXIT_REASON_HVP_LOCK:
                return EXIT_REASON_HVP_LOCK
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


# v5.13.2 P1 #4 \u2014 share-count key used by the baseline-reset detector
# below. Stored as a sidecar field on the position dict so the
# detector remains stateless and decoupled from any sentinel state
# object the position may or may not carry.
_PNL_BASELINE_LAST_SHARES_KEY = "_sentinel_last_known_shares"


def maybe_reset_pnl_baseline_on_shares_change(
    pos: dict,
    history: Deque[tuple[float, float]],
    now_ts: float,
    current_unrealized_pnl: float,
) -> bool:
    """v5.13.2 P1 #4 \u2014 reset Alarm A velocity baseline on share-count change.

    The Alarm A_FLASH velocity check compares ``unrealized_pnl_now`` with a
    sample drawn from ~60 seconds ago, dividing the delta by current
    ``position_value = entry_price * shares``. When Entry-2 fills, the
    position's ``entry_price`` (an average) and ``shares`` both change.
    The cached ``pnl_history`` deque, however, still holds samples
    computed against the pre-Entry-2 notional. Computing the velocity
    against the new notional produces an artificial spike (a wider
    notional makes the same dollar P&L look like a smaller velocity,
    but the dollar P&L itself shifts step-wise across the fill so the
    delta is large for a single tick).

    Detection is share-count-based, which is the most direct signal:
    Entry-2 and any other share-count mutation flip ``pos["shares"]``
    and thus invalidate the cached baseline. The first call after
    creation records the baseline silently; subsequent calls compare
    and reset on change.

    Returns True iff the deque was cleared and reseeded with the
    current sample (caller can use this for telemetry / tests).
    Otherwise returns False and leaves history untouched.
    """
    try:
        cur_shares = int(pos.get("shares") or 0)
    except (TypeError, ValueError):
        return False
    last_shares = pos.get(_PNL_BASELINE_LAST_SHARES_KEY)
    pos[_PNL_BASELINE_LAST_SHARES_KEY] = cur_shares
    if last_shares is None or last_shares == cur_shares:
        return False
    # Share count changed since last tick \u2014 clear stale baseline and
    # reseed with current sample so the velocity window starts fresh.
    history.clear()
    history.append((float(now_ts), float(current_unrealized_pnl)))
    return True


def _pnl_at_or_before(history: Iterable[tuple[float, float]], target_ts: float) -> float | None:
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

    Returns a list of fired sub-alarms. Both A_LOSS (hard floor) and A_FLASH
    (velocity) are evaluated independently \u2014 if both fire on the
    same tick, both appear in the output. The caller maps the list
    to a single exit order if any element is non-empty.

    Side-symmetric: P&L is signed in dollars from the position
    holder's perspective. Long: pnl = (current - entry) * shares.
    Short: pnl = (entry - current) * shares. Either way, unrealized
    <= -$500 fires A_LOSS and a 60s drop of more than 1% of position
    value (sign convention: pnl_now - pnl_60s_ago) fires A_FLASH.

    Args:
        side: "LONG" or "SHORT". Used only for telemetry detail.
        unrealized_pnl: Signed unrealized $ P&L right now.
        position_value: Notional position value in dollars
            (entry_price * shares). Must be > 0; else A_FLASH is skipped.
        pnl_history: Iterable of (ts, pnl) pairs. May be None or empty.
        now_ts: Current tick timestamp in seconds.
    """
    fired: list[SentinelAction] = []

    # A_LOSS \u2014 absolute hard floor. -$500 triggers exactly at the
    # boundary (`<=`). The boundary value is spec-literal.
    if unrealized_pnl <= ALARM_A_HARD_LOSS_DOLLARS:
        fired.append(
            SentinelAction(
                alarm="A_LOSS",
                reason=EXIT_REASON_ALARM_A,
                detail=(
                    f"side={side} unrealized_pnl=${unrealized_pnl:.2f} "
                    f"<= ${ALARM_A_HARD_LOSS_DOLLARS:.2f}"
                ),
            )
        )

    # A_FLASH \u2014 velocity. Need history and a positive position value.
    if pnl_history and position_value and position_value > 0:
        target = now_ts - ALARM_A_VELOCITY_WINDOW_SECONDS
        prior = _pnl_at_or_before(pnl_history, target)
        if prior is not None:
            delta = unrealized_pnl - prior
            velocity = delta / position_value
            if velocity <= ALARM_A_VELOCITY_THRESHOLD:
                fired.append(
                    SentinelAction(
                        alarm="A_FLASH",
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
                    detail=(f"side=LONG 5m_close={last_5m_close:.4f} < 9ema={last_5m_ema9:.4f}"),
                )
            )
    elif side == SIDE_SHORT:
        # Short: close ABOVE EMA9 fires.
        if last_5m_close > last_5m_ema9:
            fired.append(
                SentinelAction(
                    alarm="B",
                    reason=EXIT_REASON_ALARM_B,
                    detail=(f"side=SHORT 5m_close={last_5m_close:.4f} > 9ema={last_5m_ema9:.4f}"),
                )
            )
    return fired


# ---------------------------------------------------------------------------
# Alarm C \u2014 Velocity Ratchet (delegates to engine.velocity_ratchet)
# ---------------------------------------------------------------------------


def check_alarm_c(
    *,
    adx_window: "ADXTrendWindow",
    side: str,
    current_price: float,
    current_shares: int,
    current_stop_price: float | None,
) -> tuple[list[SentinelAction], list]:
    """Evaluate Alarm C (Velocity Ratchet) for one position.

    Returns ``(sentinel_actions, [])``. The second slot is preserved
    for legacy compatibility with broker.positions but is always
    empty under vAA-1 \u2014 the Titan Grip staircase is gone.

    A trip emits exactly one ``SentinelAction(alarm="C", ...)`` whose
    ``detail_stop_price`` is the new protective stop the caller must
    install (STOP MARKET modify, not a market exit).
    """
    decision: RatchetDecision = evaluate_velocity_ratchet(
        side=side,
        adx_window=adx_window,
        current_price=current_price,
        existing_stop_price=current_stop_price,
    )
    if not decision.should_emit_stop or decision.new_stop_price is None:
        return [], []

    # current_shares is unused by the Velocity Ratchet (the action is a
    # stop modify, not a fill) but is kept in the signature so legacy
    # callers don't have to relearn it.
    _ = current_shares

    action = SentinelAction(
        alarm="C",
        reason=EXIT_REASON_VELOCITY_RATCHET,
        detail=(
            f"side={side} velocity_ratchet new_stop="
            f"{decision.new_stop_price:.4f} (current={current_price:.4f}, "
            f"offset={RATCHET_STOP_PCT * 100:.2f}%)"
        ),
        detail_stop_price=float(decision.new_stop_price),
    )
    return [action], []


# ---------------------------------------------------------------------------
# Alarm D \u2014 HVP Lock (vAA-1 SENT-D)
# ---------------------------------------------------------------------------


def check_alarm_d(
    *,
    trade_hvp: TradeHVP | None,
    current_adx_5m: float,
    side: str = "",
) -> SentinelAction | None:
    """Evaluate Alarm D (HVP Lock) for one position.

    spec: vAA-1 SENT-D HVP lock. Fires when the trade's high-water-mark
    5m ADX has decayed by more than 25% (current_adx_5m strictly less
    than 75% of peak) AND the trade originally registered >= 25 ADX at
    its peak (safety floor). The safety floor value is flagged for
    review at PR-5 merge.

    Side-symmetric: ADX is unsigned, so the trigger and action are
    identical for LONG and SHORT. Returns ``None`` when no alarm fires
    or when no Strike has opened on ``trade_hvp`` yet.
    """
    if trade_hvp is None:
        return None
    try:
        peak = trade_hvp.peak
    except RuntimeError:
        return None
    if peak < ALARM_D_SAFETY_FLOOR_ADX:
        return None
    threshold = ALARM_D_HVP_FRACTION * peak
    if current_adx_5m < threshold:
        return SentinelAction(
            alarm="D",
            reason=EXIT_REASON_HVP_LOCK,
            detail=(
                f"side={side} adx={current_adx_5m:.2f} peak={peak:.2f} threshold={threshold:.2f}"
            ),
        )
    return None


# ---------------------------------------------------------------------------
# Alarm E \u2014 Divergence Trap (vAA-1 SENT-E)
# ---------------------------------------------------------------------------

# Spec literal: in-trade divergence ratchet uses the same 0.25%
# protective offset as the Velocity Ratchet (Alarm C). Co-located
# with the constant in engine.velocity_ratchet but redeclared here
# for spec readability.
ALARM_E_RATCHET_PCT: float = RATCHET_STOP_PCT

EXIT_REASON_DIVERGENCE_TRAP: str = "DIVERGENCE_TRAP"


def check_alarm_e_pre(
    *,
    memory: "DivergenceMemory",
    ticker: str,
    side: str,
    current_price: float,
    current_rsi_15: float,
    strike_num: int,
) -> bool:
    """Pre-fire divergence filter for Strike 2 / Strike 3.

    spec: vAA-1 SENT-E-PRE. Strike 1 is never blocked (no prior
    peak yet by definition); Strike 2 and Strike 3 are blocked when
    the current tick prints a divergence vs the stored peak in
    DivergenceMemory.

    Returns True iff the candidate Strike should be BLOCKED.
    """
    if strike_num < 2:
        return False
    return memory.is_diverging(
        ticker=ticker,
        side=side,
        current_price=current_price,
        current_rsi_15=current_rsi_15,
    )


def check_alarm_e_post(
    *,
    memory: "DivergenceMemory",
    ticker: str,
    side: str,
    current_price: float,
    current_rsi_15: float,
    current_stop_price: float | None,
) -> SentinelAction | None:
    """In-trade divergence ratchet.

    spec: vAA-1 SENT-E-POST. While a position is open, if the
    current tick prints a divergence vs the stored peak, propose a
    tighter STOP MARKET at ``current_price * (1 \u2213 0.0025)`` in the
    protective direction. The ratchet never loosens \u2014 if the
    proposed stop is not strictly tighter than the existing stop,
    return ``None``.

    Returns a single ``SentinelAction(alarm="E", ...)`` carrying the
    proposed stop in ``detail_stop_price``, or ``None``.
    """
    if not memory.is_diverging(
        ticker=ticker,
        side=side,
        current_price=current_price,
        current_rsi_15=current_rsi_15,
    ):
        return None

    side_u = str(side).upper()
    if side_u == SIDE_LONG:
        proposed = round(float(current_price) * (1.0 - ALARM_E_RATCHET_PCT), 4)
        if current_stop_price is not None and proposed <= float(current_stop_price):
            return None
    elif side_u == SIDE_SHORT:
        proposed = round(float(current_price) * (1.0 + ALARM_E_RATCHET_PCT), 4)
        if current_stop_price is not None and proposed >= float(current_stop_price):
            return None
    else:
        return None

    return SentinelAction(
        alarm="E",
        reason=EXIT_REASON_DIVERGENCE_TRAP,
        detail=(
            f"side={side_u} divergence_trap new_stop={proposed:.4f} "
            f"(current={current_price:.4f}, offset={ALARM_E_RATCHET_PCT * 100:.2f}%)"
        ),
        detail_stop_price=proposed,
    )


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
    adx_window: Optional["ADXTrendWindow"] = None,
    current_price: float | None = None,
    current_shares: int = 0,
    trade_hvp: TradeHVP | None = None,
    current_adx_5m: float | None = None,
    current_stop_price: float | None = None,
    divergence_memory: "DivergenceMemory | None" = None,
    current_rsi_15: float | None = None,
    ticker: str | None = None,
) -> SentinelResult:
    """Evaluate ALL sentinel alarms for one position on one tick.

    Critical: alarms are evaluated INDEPENDENTLY. Even if Alarm A
    has fired, Alarm B and C are still evaluated, and the result
    lists every fired alarm. The caller is responsible for choosing
    the OUTBOUND action: full exit if A or B fired (use
    `result.has_full_exit` / `result.exit_reason`); stop-tighten via
    the Alarm C action's ``detail_stop_price`` only if NEITHER A nor
    B fired.

    Per the spec: "These Alarms are NOT a sequence." Do not
    introduce short-circuit returns here. Alarm C is evaluated
    even when A has already tripped \u2014 the priority resolution
    is the CALLER's decision, not the evaluator's.

    ``adx_window`` and ``current_price`` are required for Alarm C to
    fire; if either is missing, C is skipped silently (e.g. the 1m
    ADX window has not seeded yet).
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

    # Alarm C \u2014 always evaluated, independent of A and B. The
    # Velocity Ratchet only fires when the 1m ADX trend window prints
    # three strictly-decreasing samples; a partial window or missing
    # price feed silently skips C.
    if adx_window is not None and current_price is not None:
        c_alarms, _legacy = check_alarm_c(
            adx_window=adx_window,
            side=side,
            current_price=current_price,
            current_shares=current_shares,
            current_stop_price=current_stop_price,
        )
        result.alarms.extend(c_alarms)

    # Alarm D \u2014 HVP Lock. Independent of A/B/C. Defensive: trade_hvp
    # is set in PR-3b at Strike fill time. Until PR-3b lands, this
    # branch is dead-code-safe (the kwarg defaults to None and
    # check_alarm_d returns None on missing state).
    if trade_hvp is not None and current_adx_5m is not None:
        d_action = check_alarm_d(
            trade_hvp=trade_hvp,
            current_adx_5m=float(current_adx_5m),
            side=side,
        )
        if d_action is not None:
            result.alarms.append(d_action)

    # Alarm E \u2014 Divergence Trap (in-trade ratchet). Independent of
    # A/B/C/D. Requires divergence_memory + current_rsi_15 + ticker +
    # current_price + current_stop_price; otherwise silently skipped.
    if (
        divergence_memory is not None
        and current_rsi_15 is not None
        and ticker is not None
        and current_price is not None
        and current_stop_price is not None
    ):
        e_action = check_alarm_e_post(
            memory=divergence_memory,
            ticker=ticker,
            side=side,
            current_price=float(current_price),
            current_rsi_15=float(current_rsi_15),
            current_stop_price=float(current_stop_price),
        )
        if e_action is not None:
            result.alarms.append(e_action)

    return result


def format_sentinel_log(ticker: str, position_id: str | None, result: SentinelResult) -> str:
    """Render a structured one-line log entry for a sentinel trip.

    Format: ``[SENTINEL] pos=<id> ticker=<t> alarms=[A_LOSS,B,D] action=EXIT
    reason=<top> detail=<...>``. The alarm-string list may include any
    combination of ``A_LOSS``, ``A_FLASH``, ``B``, ``C1``..``C4``, ``D``,
    or ``E``.
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
    "ALARM_D_HVP_FRACTION",
    "ALARM_D_SAFETY_FLOOR_ADX",
    "ALARM_E_RATCHET_PCT",
    "EXIT_REASON_ALARM_A",
    "EXIT_REASON_ALARM_B",
    "EXIT_REASON_ALARM_C",
    "EXIT_REASON_DIVERGENCE_TRAP",
    "EXIT_REASON_HVP_LOCK",
    "EXIT_REASON_VELOCITY_RATCHET",
    "PNL_HISTORY_MAXLEN",
    "RATCHET_STOP_PCT",
    "RatchetDecision",
    "SIDE_LONG",
    "SIDE_SHORT",
    "SentinelAction",
    "SentinelResult",
    "check_alarm_a",
    "check_alarm_b",
    "check_alarm_c",
    "check_alarm_d",
    "check_alarm_e_post",
    "check_alarm_e_pre",
    "evaluate_sentinel",
    "evaluate_velocity_ratchet",
    "format_sentinel_log",
    "maybe_reset_pnl_baseline_on_shares_change",
    "new_pnl_history",
    "record_pnl",
]
