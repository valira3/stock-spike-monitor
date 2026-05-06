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

import os as _os
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Deque, Iterable, Optional


# v6.11.14 \u2014 env-read helper for module-level trail constants. Returns
# the supplied default when the env var is absent or unparseable.
def _read_float(env_name, default):
    try:
        v = _os.getenv(env_name)
        return float(v) if v is not None else default
    except ValueError:
        return default

from engine.alarm_f_trail import (
    EXIT_REASON_ALARM_F,
    EXIT_REASON_ALARM_F_EXIT,
    TrailState,
    chandelier_level as _f_chandelier_level,
    propose_stop as _f_propose_stop,
    should_exit_on_close_cross as _f_should_exit_on_close_cross,
    update_trail as _f_update_trail,
)
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

# Exit reason codes per RULING #1 (LIMIT exits) + RULING #4 (R-2 hard
# stop tagging). Stable strings consumed by broker/positions and
# broker/order_types.
#
# RULING #1: A-A flash loss, A-B EMA cross, A-D ADX decline emit LIMIT
# exits at +/-0.5% from current. R-2 hard stop (-$500, A_LOSS sub-
# alarm) emits STOP MARKET per Tiger Sovereign v15.0 \u00a7Risk Rails R-2.
EXIT_REASON_R2_HARD_STOP: str = "sentinel_r2_hard_stop"
EXIT_REASON_ALARM_A: str = "sentinel_a_flash_loss"
EXIT_REASON_ALARM_B: str = "sentinel_b_ema_cross"
EXIT_REASON_ALARM_D: str = "sentinel_d_adx_decline"
# v5.31.4 \u2014 price-based protective stop. Fires STOP MARKET when the
# current mark crosses the position's protective stop price (entry \u00d7
# 0.995 long / entry \u00d7 1.005 short, set in broker/orders.py from
# eye_of_tiger.STOP_PCT_OF_ENTRY). This sub-alarm sits next to A_LOSS
# under Alarm A: it is the price rail backstop, while A_LOSS is the
# dollar rail. The price rail typically fires first in a slow drift
# scenario where the dollar threshold has not yet been reached.
EXIT_REASON_PRICE_STOP: str = "sentinel_a_stop_price"
# v6.5.1 \u2014 deep-stop rail that fires during the v6.4.4 min_hold
# blocking window when price blows through 75 bp past entry. Bypasses
# the gate so blow-through losses are capped without disabling it.
EXIT_REASON_V651_DEEP_STOP: str = "sentinel_v651_deep_stop"
# Backward-compat alias \u2014 some callers still import EXIT_REASON_HVP_LOCK.
EXIT_REASON_HVP_LOCK: str = EXIT_REASON_ALARM_D

# Alarm D \u2014 ADX decline: 5m ADX falls below 75% of session HWM
# (per RULING #2, session-scoped, NOT per-Strike Trade_HVP).
ALARM_D_HVP_FRACTION: float = 0.75
ALARM_D_SAFETY_FLOOR_ADX: float = 25.0

# vAA-1: the new Alarm C reason. Kept as ``EXIT_REASON_ALARM_C`` for
# backward import compat with broker/positions.py.
EXIT_REASON_ALARM_C: str = EXIT_REASON_VELOCITY_RATCHET

# v5.27.0 \u2014 Alarm B 2-bar confirmation. Spec L-P4-B / S-P4-B is
# spec-literal 1-bar ("a closed 5m candle"); v5.27.0 widens it to 2
# consecutive closed 5m bars on the wrong side of the 9-EMA so the
# sentinel does not chop winners on the first transient cross. The
# 1-bar default is preserved when the caller does not supply the prior
# bar values \u2014 spec-strict tests stay green; prod (broker.positions)
# and the backtest harness (replay_v511_full) supply both bars.
ALARM_B_CONFIRM_BARS: int = 2

# v5.28.0 \u2014 Alarm portfolio simplification. Ablation on Apr 30 prod
# data (see /tmp/ablation_results.json, /tmp/abl2_results.json) showed:
#   \u2022 Alarm C (Velocity Ratchet) fires constantly but never causes an
#     exit because its 0.25%-of-current trail is co-dominated by Alarm
#     B / R-2 \u2014 it adds noise without P&L (\u0394 = -$14 of 13 pairs).
#   \u2022 Alarms D and E never fired on Apr 30. They remain in the code
#     for spec compliance but are gated off pending a prod-data review.
# Alarm F's new closed-bar exit (this release) is intended to subsume
# both C's role (faster trail) and a portion of B's role (faster
# confirmation). Alarms A and B remain enabled as the deep safety net
# and the structural EMA cross. The flags are class-style constants so
# tests can flip them per-case via monkeypatch.
ALARM_C_ENABLED: bool = False
ALARM_D_ENABLED: bool = False

# v6.4.0 \u2014 Alarm B (EMA9 cross) gated off by default. Apr 27\u2013May 1 sweep:
# B-off + Chandelier 1.5/0.7 swung the week +$217.93 (+$831.50 \u2192 +$1,049.43,
# 60 pairs, WR 45.2%\u219261.7%). Avg trade $11\u2192$17, avg hold 41m\u219262m. The
# noise-cross filter and stateful counter remain in place; they're zero-cost
# when B is disabled and ready if a future release flips the flag back on.
# Set True (or monkeypatch in tests) to restore B firing.
ALARM_B_ENABLED: bool = False

# ---------------------------------------------------------------------------
# v6.1.0 \u2014 ATR-scaled trailing stop feature flag
# ---------------------------------------------------------------------------
# Set False to fall back to the fixed-cents protective stop path (backward
# compat). Flip off via monkeypatch in tests or env override if shadow data
# shows regression before a full rollback is warranted.
_V610_ATR_TRAIL_ENABLED: bool = True
ALARM_E_ENABLED: bool = False

# v6.4.4 \u2014 min-hold gate on PRICE_STOP (Alarm-A protective stop).
# Devi 84day_2026_sip analysis: 266/269 under-10min pairs exit on
# sentinel_a_stop_price for -$6,649 (vs +$13,235 run total). Block the
# 50 bp protective stop under 10 minutes from entry; deeper rails (R-2
# -$500, daily circuit -$1,500, Alarm-A flash >1%/min) still fire.
# Applied in broker/positions.py:_run_sentinel right before the
# ``has_full_exit`` short-circuit. Flag exists so the gate can be
# disabled via monkeypatch without a deploy.
_V644_MIN_HOLD_GATE_ENABLED: bool = True
# v7.0.5 \u2014 default lowered from 600 to 120 after 84-day SIP sweep showed
# the gate's net cost is $-1,908 / 84d at 600s vs the optimum at ~120s
# (peak +$1,908; mh=0/60/120 ≈ tied at top, mh=180 starts bending back).
# Wired to env so prod can be tuned without a deploy. Keeping 120s rather
# than 0s preserves a small chandelier-warmup buffer (the trail rarely
# ratchets above entry under T+60-90s anyway).
try:
    _V644_MIN_HOLD_SECONDS: int = int(_os.getenv("V644_MIN_HOLD_SEC", "120"))
except (TypeError, ValueError):
    _V644_MIN_HOLD_SECONDS = 120

# v6.5.1 \u2014 deep-stop during min_hold window. The 50 bp protective rail
# is blocked under 10 minutes from entry (v6.4.4 gate). When mark blows
# through -0.75% (long) or +0.75% (short) inside that window, this rail
# fires immediately to cap blow-through losses.
# Devi 84day_2026_sip: 30/164 forced losers exited worse than -0.75%
# at the 10-min mark, accounting for $-2,500 (vs $-715 if cut at 75bp).
_V651_DEEP_STOP_ENABLED: bool = True
_V651_DEEP_STOP_PCT: float = 0.0075  # 75 bp
# v6.8.0 C1: extended deep-stop to shorts — W-E fix (STOP_MARKET routing)
# is prerequisite. Original long-only default was conservative; no
# forensic exclusion rationale found in v651_implementation_log.md.
# Short P&L is majority of total P&L in 63-day SIP; TSLA/NVDA/NFLX/AVGO
# are highest-variance names on the short side.
_V651_DEEP_STOP_LONG_ONLY: bool = False

SIDE_LONG = "LONG"
SIDE_SHORT = "SHORT"


# === v6.1.0 ema-cross confirmation state ===
# Feature flag: flip to False to restore single-bar (legacy) behaviour
# for check_alarm_b without removing any code.
_V610_EMA_CONFIRM_ENABLED: bool = True

# Feature flag: flip to False to disable the 11:30-13:00 ET lunch
# suppression window (useful for back-testing or manual override).
_V610_LUNCH_SUPPRESSION_ENABLED: bool = True

# Lunch-chop suppression window boundaries (Eastern Time, inclusive start,
# exclusive end). Exits are blocked inside [LUNCH_START, LUNCH_END).
_V610_LUNCH_START_HOUR: int = 11
_V610_LUNCH_START_MIN: int = 30
_V610_LUNCH_END_HOUR: int = 13
_V610_LUNCH_END_MIN: int = 0

# Per-position counter: number of consecutive bars where the EMA cross
# condition has been True for a given position_id. Keyed by position_id
# (string). Resets to 0 when the condition flips False for that position.
_ema_cross_pending: dict[str, int] = {}
# === end v6.1.0 ema-cross confirmation state ===


# === v6.3.0 \u2014 Sentinel B noise-cross filter ===
# Weekly backtest Apr 27\u2014May 1 found Sentinel B had 6% win-rate /
# -$277 across 17 fires, vs Sentinel A 70% / +$259. The 16 losers all
# closed at -0.05% to -0.37% adverse \u2014 within typical 1m noise. The
# noise-cross filter gates the v6.1.0 2-bar EMA exit on a minimum ATR-
# scaled adverse move from entry. Sit-out (don't reset counter) when
# adverse < k\u00d7ATR; the counter keeps tracking until the move is
# either confirmed by price OR the cross condition naturally resets.
V630_NOISE_CROSS_FILTER_ENABLED: bool = True
# Minimum adverse-move-from-entry, expressed as a multiple of the
# latest 1m ATR. 0.10 was chosen because the 16 EMA-cross losers in
# the Apr 27\u2014May 1 sweep averaged 0.19% adverse \u2014 the typical
# 1m ATR for the universe is around 0.30\u20140.50% so 0.10\u00d7ATR
# (\u2248 0.03\u20140.05% adverse) admits roughly the bottom 80% of
# noise crosses while letting the deepest-conviction structural
# crosses fire normally.
V630_NOISE_CROSS_ATR_K: float = 0.10
# === end v6.3.0 noise-cross filter ===


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

    alarm: str  # "A_LOSS", "A_FLASH", "B", "C1..C4", "D", "E", "F"
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
        """True if any full-exit alarm fired \u2014 caller must do a 100%
        close and ignore any stop-tighten proposals on the same tick.

        v5.28.0: Alarm F's closed-bar chandelier cross (alarm code
        ``F_EXIT``) is now a full-exit reason alongside R-2/A/B/D.
        v5.31.4: Alarm A_STOP_PRICE (mark crossed protective stop
        price) is also a full-exit reason \u2014 STOP MARKET, 100% close.
        """
        for a in self.alarms:
            if a.reason in (
                EXIT_REASON_R2_HARD_STOP,
                EXIT_REASON_PRICE_STOP,
                EXIT_REASON_V651_DEEP_STOP,
                EXIT_REASON_ALARM_A,
                EXIT_REASON_ALARM_B,
                EXIT_REASON_ALARM_D,
                EXIT_REASON_ALARM_F_EXIT,
            ):
                return True
        return False

    @property
    def exit_reason(self) -> str | None:
        """Single canonical exit reason.
        Priority: R-2 hard stop > A_STOP_PRICE > A-A > A-B > F-EXIT > A-D > C.
        v5.31.4: A_STOP_PRICE (price-rail mark cross) is wedged just
        below R-2. Both are STOP MARKET full-exit rails; R-2 stays on
        top because the dollar floor is the deepest rail (R-2 only fires
        when price rail has either been disabled or moved past).
        F-EXIT (chandelier cross) is wedged between B and D \u2014 above
        D because in v5.28.0 D is gated off, and below B because B's
        2-bar 9-EMA confirmation is a stronger structural signal.
        All alarms remain recorded in `alarms` regardless.
        """
        if not self.alarms:
            return None
        for a in self.alarms:
            if a.reason == EXIT_REASON_R2_HARD_STOP:
                return EXIT_REASON_R2_HARD_STOP
        for a in self.alarms:
            if a.reason == EXIT_REASON_V651_DEEP_STOP:
                return EXIT_REASON_V651_DEEP_STOP
        for a in self.alarms:
            if a.reason == EXIT_REASON_PRICE_STOP:
                return EXIT_REASON_PRICE_STOP
        for a in self.alarms:
            if a.reason == EXIT_REASON_ALARM_A:
                return EXIT_REASON_ALARM_A
        for a in self.alarms:
            if a.reason == EXIT_REASON_ALARM_B:
                return EXIT_REASON_ALARM_B
        for a in self.alarms:
            if a.reason == EXIT_REASON_ALARM_F_EXIT:
                return EXIT_REASON_ALARM_F_EXIT
        for a in self.alarms:
            if a.reason == EXIT_REASON_ALARM_D:
                return EXIT_REASON_ALARM_D
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
    hard_loss_threshold: float = ALARM_A_HARD_LOSS_DOLLARS,
) -> list[SentinelAction]:
    """Evaluate Alarm A for one position.

    Returns a list of fired sub-alarms. Both A_LOSS (hard floor) and A_FLASH
    (velocity) are evaluated independently \u2014 if both fire on the
    same tick, both appear in the output. The caller maps the list
    to a single exit order if any element is non-empty.

    Side-symmetric: P&L is signed in dollars from the position
    holder's perspective. Long: pnl = (current - entry) * shares.
    Short: pnl = (entry - current) * shares. Either way, unrealized
    <= the configured hard-loss threshold fires A_LOSS and a 60s drop
    of more than 1% of position value (sign convention:
    pnl_now - pnl_60s_ago) fires A_FLASH.

    v5.27.0 \u2014 ``hard_loss_threshold`` (default -$500.0) is configurable
    so the caller can pass a portfolio-scaled brake derived from
    ``eye_of_tiger.scaled_sovereign_brake_dollars``. Older callers that
    don't supply the kwarg keep the legacy absolute -$500 floor.

    Args:
        side: "LONG" or "SHORT". Used only for telemetry detail.
        unrealized_pnl: Signed unrealized $ P&L right now.
        position_value: Notional position value in dollars
            (entry_price * shares). Must be > 0; else A_FLASH is skipped.
        pnl_history: Iterable of (ts, pnl) pairs. May be None or empty.
        now_ts: Current tick timestamp in seconds.
        hard_loss_threshold: NEGATIVE dollar threshold; A_LOSS fires
            when ``unrealized_pnl`` is at or below this value.
    """
    fired: list[SentinelAction] = []

    # A_LOSS \u2014 R-2 hard stop (legacy default -$500; v5.27.0 portfolio-
    # scaled when caller supplies ``hard_loss_threshold``). Per Tiger
    # Sovereign v15.0 \u00a7Risk Rails R-2, this is a STOP MARKET (NOT a
    # LIMIT, unlike the A-A / A-B / A-D RULING #1 LIMIT exits). The
    # reason string routes to ORDER_TYPE_STOP_MARKET via
    # broker.order_types.
    if unrealized_pnl <= hard_loss_threshold:
        fired.append(
            SentinelAction(
                alarm="A_LOSS",
                reason=EXIT_REASON_R2_HARD_STOP,
                detail=(
                    f"side={side} R-2 hard stop unrealized_pnl=${unrealized_pnl:.2f} "
                    f"<= ${hard_loss_threshold:.2f}"
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
            # v15.0 SPEC: "1m price move > 1% against position -> MARKET EXIT".
            # Use a strict less-than comparison so an exact -1.000% move does
            # not trigger; only moves that exceed -1% fire the alarm.
            if velocity < ALARM_A_VELOCITY_THRESHOLD:
                fired.append(
                    SentinelAction(
                        alarm="A_FLASH",
                        reason=EXIT_REASON_ALARM_A,
                        detail=(
                            f"side={side} pnl_60s_delta=${delta:.2f} "
                            f"velocity={velocity * 100:.2f}% "
                            f"< {ALARM_A_VELOCITY_THRESHOLD * 100:.2f}%"
                        ),
                    )
                )
    return fired


# ---------------------------------------------------------------------------
# Alarm A_STOP_PRICE \u2014 v5.31.4 price-rail protective stop
# v6.1.0 \u2014 enhanced with ATR-scaled 3-stage trailing stop with profit-protect ratchet
# ---------------------------------------------------------------------------

# v6.1.0 stage thresholds (multiples of ATR).
#
# v6.11.14 \u2014 made env-overridable so an operator can widen the Stage 1
# trail without a code deploy when chop-day forensics show the 1x-ATR
# trail is engaging instantly on low-range tickers (5m ATR < entry rail).
# Defaults preserved so absent env vars keep current production behavior.
_ATR_TRAIL_STAGE1_THRESHOLD: float = _read_float("ATR_TRAIL_STAGE1_THRESHOLD", 1.0)
_ATR_TRAIL_STAGE2_THRESHOLD: float = _read_float("ATR_TRAIL_STAGE2_THRESHOLD", 3.0)
_ATR_TRAIL_STAGE1_MULT: float = _read_float("ATR_TRAIL_STAGE1_MULT", 1.0)
_ATR_TRAIL_STAGE2_MULT: float = _read_float("ATR_TRAIL_STAGE2_MULT", 1.5)
_ATR_TRAIL_LOCKIN_FRAC: float = _read_float("ATR_TRAIL_LOCKIN_FRAC", 0.5)
_ATR_TRAIL_FLOOR_MULT: float = _read_float("ATR_TRAIL_FLOOR_MULT", 0.3)

# v6.11.14 \u2014 trail-active gate. Trail does not engage until the
# position is at least this many ATRs in profit. Default 0.0 preserves
# v6.1.0 behavior (trail goes live the instant a position opens). Set
# to e.g. 0.5 to delay activation until the position has banked half
# an ATR of profit, preventing the trail from clipping winners that
# barely cleared the entry stop on noise.
_ATR_TRAIL_ACTIVATE_PNL_FRAC: float = _read_float("ATR_TRAIL_ACTIVATE_PNL_FRAC", 0.0)


def _compute_atr_trail_distance(
    atr: float,
    position_pnl_per_share: float,
    peak_open_profit_per_share: float,
) -> float:
    """Compute the v6.1.0 ATR-scaled trail distance (in price units).

    Three stages, keyed on how much per-share profit the position is
    carrying relative to the current ATR:

      Stage 1 (pnl < 1 x ATR in profit): trail = 1.0 x ATR
      Stage 2 (1 x ATR <= pnl < 3 x ATR in profit): trail = 1.5 x ATR
      Stage 3 (pnl >= 3 x ATR in profit): lock-in mode \u2014 trail =
          max(0.5 x peak_open_profit_per_share, 0.3 x ATR)

    An absolute floor of 0.3 x ATR is applied in all stages so the
    trail never collapses to a micro-stop in low-volatility regimes.

    Arguments are pre-validated by the caller; ``atr`` must be > 0.
    Returns a positive float (price distance, not direction-adjusted).
    """
    floor = _ATR_TRAIL_FLOOR_MULT * atr
    if position_pnl_per_share >= _ATR_TRAIL_STAGE2_THRESHOLD * atr:
        # Stage 3 \u2014 lock-in: cap give-back at 50%% of peak open profit.
        # Peak may be 0 or negative (trade never ran positive); fall back
        # to Stage 2 distance so we never widen unexpectedly.
        lockin_dist = _ATR_TRAIL_LOCKIN_FRAC * max(peak_open_profit_per_share, 0.0)
        if lockin_dist <= 0.0:
            lockin_dist = _ATR_TRAIL_STAGE2_MULT * atr
        return max(lockin_dist, floor)
    elif position_pnl_per_share >= _ATR_TRAIL_STAGE1_THRESHOLD * atr:
        # Stage 2 \u2014 widened trail as position moves further in profit.
        return max(_ATR_TRAIL_STAGE2_MULT * atr, floor)
    else:
        # Stage 1 \u2014 initial trail equal to one ATR.
        return max(_ATR_TRAIL_STAGE1_MULT * atr, floor)


def check_alarm_a_stop_price(
    *,
    side: str,
    current_price: float | None,
    current_stop_price: float | None,
    # v6.1.0 ATR trail optional params \u2014 ignored when _V610_ATR_TRAIL_ENABLED
    # is False or when ATR data is unavailable (falls back to fixed-cents path).
    atr_value: float | None = None,
    position_pnl_per_share: float | None = None,
    peak_open_profit_per_share: float | None = None,
    entry_price: float | None = None,
) -> list[SentinelAction]:
    """Evaluate the v5.31.4 price-based protective stop.

    Fires a full-exit STOP MARKET when the current mark crosses the
    position's protective stop price. The stop price itself is set
    by ``broker/orders.py`` from ``eye_of_tiger.STOP_PCT_OF_ENTRY``
    at entry: long stop = entry \u00d7 0.995, short stop = entry \u00d7 1.005.

    v6.1.0 enhancement (``_V610_ATR_TRAIL_ENABLED=True``): when
    ``atr_value``, ``position_pnl_per_share``, ``peak_open_profit_per_share``,
    and ``entry_price`` are all provided, the protective stop level is
    recomputed as an ATR-scaled trailing stop with a 3-stage ratchet
    and a 50%% peak-profit lock-in at Stage 3. The fixed-cents
    ``current_stop_price`` is used as the fallback when ATR data is
    unavailable or the feature flag is off.

    Side-symmetric:
      * LONG: fires when ``current_price <= stop_level``.
      * SHORT: fires when ``current_price >= stop_level``.

    Sits out silently when either price input is None (e.g. price feed
    gap or no protective stop on file). The position-level R-2 dollar
    rail and Alarm B's 9-EMA shield remain as deeper backstops.

    Returns a list with at most one SentinelAction.
    """
    if current_price is None or current_stop_price is None:
        return []
    try:
        cp = float(current_price)
        sp = float(current_stop_price)
    except (TypeError, ValueError):
        return []

    # v6.1.0 \u2014 attempt to recompute stop via ATR-scaled trail when the
    # feature flag is enabled and all required inputs are present.
    atr_trail_active: bool = False
    if (
        _V610_ATR_TRAIL_ENABLED
        and atr_value is not None
        and position_pnl_per_share is not None
        and peak_open_profit_per_share is not None
        and entry_price is not None
    ):
        try:
            atr_f = float(atr_value)
            pnl_ps = float(position_pnl_per_share)
            peak_ps = float(peak_open_profit_per_share)
            ep = float(entry_price)
            # v6.11.14 \u2014 trail-active gate. Skip the trail entirely when
            # the position has not yet earned ACTIVATE_PNL_FRAC * ATR of
            # profit per share. Default 0.0 preserves v6.1.0 behavior
            # (trail goes live immediately).
            activate_threshold = _ATR_TRAIL_ACTIVATE_PNL_FRAC * atr_f
            if atr_f > 0.0 and pnl_ps >= activate_threshold:
                trail_dist = _compute_atr_trail_distance(atr_f, pnl_ps, peak_ps)
                # Ratchet the ATR-computed stop: never move it against the
                # position direction \u2014 only tighten (raise for long, lower for
                # short). The fixed-cents stop from broker/orders.py acts as
                # the hard minimum boundary below this ratchet.
                if side == SIDE_LONG:
                    atr_stop = ep + pnl_ps - trail_dist
                    sp = max(sp, atr_stop)  # never loosen below the initial stop
                elif side == SIDE_SHORT:
                    atr_stop = ep - pnl_ps + trail_dist
                    sp = min(sp, atr_stop)  # never loosen above the initial stop
                atr_trail_active = True
        except (TypeError, ValueError):
            pass  # fall through to fixed-cents path

    fired: list[SentinelAction] = []
    if side == SIDE_LONG:
        if cp <= sp:
            trail_tag = " atr_trail=1" if atr_trail_active else ""
            fired.append(
                SentinelAction(
                    alarm="A_STOP_PRICE",
                    reason=EXIT_REASON_PRICE_STOP,
                    detail=(f"side=LONG mark={cp:.4f} <= stop={sp:.4f}{trail_tag}"),
                    detail_stop_price=sp,
                )
            )
    elif side == SIDE_SHORT:
        if cp >= sp:
            trail_tag = " atr_trail=1" if atr_trail_active else ""
            fired.append(
                SentinelAction(
                    alarm="A_STOP_PRICE",
                    reason=EXIT_REASON_PRICE_STOP,
                    detail=(f"side=SHORT mark={cp:.4f} >= stop={sp:.4f}{trail_tag}"),
                    detail_stop_price=sp,
                )
            )
    return fired


# Alarm B \u2014 9-EMA Shield (5m close vs 9-EMA)
# ---------------------------------------------------------------------------


def check_alarm_b(
    *,
    side: str,
    last_5m_close: float | None,
    last_5m_ema9: float | None,
    prev_5m_close: float | None = None,
    prev_5m_ema9: float | None = None,
    confirm_bars: int = 1,
    # v6.1.0 params: stateful two-bar confirmation via per-position counter.
    # When position_id is supplied AND _V610_EMA_CONFIRM_ENABLED is True,
    # the function uses _ema_cross_pending[position_id] to track consecutive
    # cross bars and only fires after two consecutive cross bars. Callers that
    # do not supply position_id get the legacy (confirm_bars-based) path.
    position_id: str | None = None,
    # now_et: datetime in America/New_York used for lunch-chop suppression.
    # When None the suppression check is skipped (treats as outside window).
    now_et: "datetime | None" = None,
    # v6.3.0 \u2014 noise-cross filter inputs. When all three are supplied AND
    # ``V630_NOISE_CROSS_FILTER_ENABLED`` is True, the v6.1.0 stateful path
    # gates the exit on a minimum adverse move from entry of
    # ``V630_NOISE_CROSS_ATR_K \u00d7 last_1m_atr``. When any input is None
    # the filter is bypassed (back-compat).
    entry_price: float | None = None,
    current_price: float | None = None,
    last_1m_atr: float | None = None,
) -> list[SentinelAction]:
    """Evaluate Alarm B for one position.

    Spec L-P4-B / S-P4-B: a CLOSED 5-minute candle whose close is
    on the wrong side of the 5m 9-EMA terminates the trade. "Closed"
    means the bar must already be done; the engine.bars helper
    ``compute_5m_ohlc_and_ema9`` already drops the in-progress bar
    so its `closes[-1]` and `ema9` are spec-compatible.

    v5.27.0 \u2014 ``confirm_bars`` widens the cross requirement to N
    consecutive closed 5m bars on the wrong side. Default ``1`` is the
    spec-literal behaviour (back-compat: every existing single-bar
    caller stays green). When ``confirm_bars=2`` the caller must also
    supply ``prev_5m_close`` + ``prev_5m_ema9`` (the bar before the
    most recent closed 5m bar and its 9-EMA reading at that bucket);
    if either prior value is missing the alarm sits out (insufficient
    history) rather than firing on the single bar. Higher
    ``confirm_bars`` values are not supported \u2014 we only have prev/
    last in the contract.

    v6.1.0 \u2014 when ``position_id`` is provided and
    ``_V610_EMA_CONFIRM_ENABLED`` is True, a stateful per-position
    counter (``_ema_cross_pending``) replaces the prev/last pair
    approach. The counter increments on each consecutive cross bar and
    fires only at counter >= 2. The counter resets to 0 when the cross
    condition is False. If ``_V610_LUNCH_SUPPRESSION_ENABLED`` is True
    and ``now_et`` falls inside [11:30, 13:00) ET, the exit is blocked
    regardless of counter value (counter still increments so the state
    is consistent once the window reopens).

    Returns a list with at most one SentinelAction.
    """
    if last_5m_close is None or last_5m_ema9 is None:
        return []

    # -----------------------------------------------------------------------
    # v6.1.0 stateful two-bar confirmation path.
    # Only taken when a position_id is supplied AND the feature flag is on.
    # -----------------------------------------------------------------------
    if position_id is not None and _V610_EMA_CONFIRM_ENABLED:
        # Determine whether the cross condition is currently True.
        cross_true: bool
        if side == SIDE_LONG:
            cross_true = last_5m_close < last_5m_ema9
        elif side == SIDE_SHORT:
            cross_true = last_5m_close > last_5m_ema9
        else:
            cross_true = False

        if cross_true:
            _ema_cross_pending[position_id] = _ema_cross_pending.get(position_id, 0) + 1
        else:
            # Cross condition is False: reset counter and sit out.
            _ema_cross_pending[position_id] = 0
            return []

        count = _ema_cross_pending[position_id]

        # Lunch-chop suppression: block the exit during 11:30-13:00 ET.
        # The counter still incremented above so state remains consistent.
        if _V610_LUNCH_SUPPRESSION_ENABLED and now_et is not None:
            from engine.timing import ET as _ET

            # Normalise to ET so comparisons are DST-aware.
            if now_et.tzinfo is None:
                now_et_norm = now_et.replace(tzinfo=_ET)
            else:
                now_et_norm = now_et.astimezone(_ET)
            lunch_start_mins = _V610_LUNCH_START_HOUR * 60 + _V610_LUNCH_START_MIN
            lunch_end_mins = _V610_LUNCH_END_HOUR * 60 + _V610_LUNCH_END_MIN
            now_mins = now_et_norm.hour * 60 + now_et_norm.minute
            if lunch_start_mins <= now_mins < lunch_end_mins:
                return []

        if count < 2:
            return []

        # v6.3.0 noise-cross filter \u2014 require min adverse move from entry.
        # Sit out when price has not yet moved k\u00d7ATR against entry. The
        # counter is intentionally NOT reset; the next bar will re-evaluate.
        if (
            V630_NOISE_CROSS_FILTER_ENABLED
            and entry_price is not None
            and current_price is not None
            and last_1m_atr is not None
            and last_1m_atr > 0
        ):
            if side == SIDE_LONG:
                adverse = entry_price - current_price
            else:  # SHORT
                adverse = current_price - entry_price
            min_adverse = V630_NOISE_CROSS_ATR_K * last_1m_atr
            if adverse < min_adverse:
                return []

        # Two or more consecutive cross bars confirmed \u2014 fire the exit.
        if side == SIDE_LONG:
            detail_str = (
                f"side=LONG v610 2bar count={count} "
                f"last_close={last_5m_close:.4f}<last_ema9={last_5m_ema9:.4f}"
            )
        else:
            detail_str = (
                f"side=SHORT v610 2bar count={count} "
                f"last_close={last_5m_close:.4f}>last_ema9={last_5m_ema9:.4f}"
            )
        return [
            SentinelAction(
                alarm="B",
                reason=EXIT_REASON_ALARM_B,
                detail=detail_str,
            )
        ]

    # -----------------------------------------------------------------------
    # Legacy path: v5.27.0 prev/last pair confirmation (confirm_bars).
    # Taken when position_id is None or _V610_EMA_CONFIRM_ENABLED is False.
    # -----------------------------------------------------------------------

    # 2-bar confirm path \u2014 require both the most recent closed bar
    # AND the bar before it to be on the wrong side of THEIR
    # respective EMA9 readings. Insufficient prior data = no fire.
    if confirm_bars >= 2:
        if prev_5m_close is None or prev_5m_ema9 is None:
            return []
        fired: list[SentinelAction] = []
        if side == SIDE_LONG:
            if last_5m_close < last_5m_ema9 and prev_5m_close < prev_5m_ema9:
                fired.append(
                    SentinelAction(
                        alarm="B",
                        reason=EXIT_REASON_ALARM_B,
                        detail=(
                            f"side=LONG 2bar prev_close={prev_5m_close:.4f}<"
                            f"prev_ema9={prev_5m_ema9:.4f} "
                            f"last_close={last_5m_close:.4f}<"
                            f"last_ema9={last_5m_ema9:.4f}"
                        ),
                    )
                )
        elif side == SIDE_SHORT:
            if last_5m_close > last_5m_ema9 and prev_5m_close > prev_5m_ema9:
                fired.append(
                    SentinelAction(
                        alarm="B",
                        reason=EXIT_REASON_ALARM_B,
                        detail=(
                            f"side=SHORT 2bar prev_close={prev_5m_close:.4f}>"
                            f"prev_ema9={prev_5m_ema9:.4f} "
                            f"last_close={last_5m_close:.4f}>"
                            f"last_ema9={last_5m_ema9:.4f}"
                        ),
                    )
                )
        return fired

    # 1-bar (spec-strict) path.
    fired_1: list[SentinelAction] = []
    if side == SIDE_LONG:
        # Long: close BELOW EMA9 fires.
        if last_5m_close < last_5m_ema9:
            fired_1.append(
                SentinelAction(
                    alarm="B",
                    reason=EXIT_REASON_ALARM_B,
                    detail=(f"side=LONG 5m_close={last_5m_close:.4f} < 9ema={last_5m_ema9:.4f}"),
                )
            )
    elif side == SIDE_SHORT:
        # Short: close ABOVE EMA9 fires.
        if last_5m_close > last_5m_ema9:
            fired_1.append(
                SentinelAction(
                    alarm="B",
                    reason=EXIT_REASON_ALARM_B,
                    detail=(f"side=SHORT 5m_close={last_5m_close:.4f} > 9ema={last_5m_ema9:.4f}"),
                )
            )
    return fired_1


# === v6.1.0 ema-cross confirmation helpers ===


def reset_ema_cross_pending(position_id: str | None = None) -> None:
    """Reset the v6.1.0 EMA-cross pending counter.

    When called with a ``position_id``, only that position's counter is
    cleared (use on position close). When called with no argument (or
    ``None``), the entire module-level dict is cleared (use in tests or
    at session reset).
    """
    if position_id is None:
        _ema_cross_pending.clear()
    else:
        _ema_cross_pending.pop(position_id, None)


# === end v6.1.0 ema-cross confirmation helpers ===


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
# Alarm D \u2014 ADX decline (RULING #2: session-wide HWM, NOT per-Strike)
# ---------------------------------------------------------------------------

# RULING #2 SessionHWM tracker. Session-wide max 5m ADX per ticker since
# 09:30 ET, decoupled from per-Strike Trade_HVP scope. scan.py calls
# ``record_session_5m_adx`` on each closed 5m bar; the daily session
# reset hook clears the dict.
_SESSION_5M_ADX_HWM: dict[str, float] = {}


def record_session_5m_adx(ticker: str, adx: float) -> None:
    """Update the session-wide 5m ADX HWM for ``ticker``.

    Idempotent: only raises the recorded peak; never lowers it. scan.py
    calls this on every closed 5m bar regardless of position state.
    """
    if not ticker:
        return
    try:
        a = float(adx)
    except (TypeError, ValueError):
        return
    cur = _SESSION_5M_ADX_HWM.get(ticker, 0.0)
    if a > cur:
        _SESSION_5M_ADX_HWM[ticker] = a


def get_session_5m_adx_hwm(ticker: str) -> float:
    """Return the session-wide 5m ADX HWM for ``ticker`` (0.0 if unset)."""
    return _SESSION_5M_ADX_HWM.get(ticker, 0.0)


def reset_session_5m_adx() -> None:
    """Clear all session-wide 5m ADX HWM state. Called at 09:30 ET reset."""
    _SESSION_5M_ADX_HWM.clear()


def check_alarm_d(
    *,
    ticker: str,
    current_adx_5m: float,
    side: str = "",
    trade_hvp: TradeHVP | None = None,  # legacy, ignored under RULING #2
) -> SentinelAction | None:
    """Evaluate Alarm D (ADX decline) for one position.

    RULING #2: read peak from session-wide ``_SESSION_5M_ADX_HWM`` keyed
    by ticker, NOT from per-Strike ``trade_hvp.peak``. Fires when
    current 5m ADX drops below 75% of the session peak AND the session
    peak was at least 25 ADX (safety floor).

    Side-symmetric: ADX is unsigned. Returns ``None`` when the session
    HWM is below the safety floor (e.g. early-session or low-ADX day).

    The ``trade_hvp`` kwarg is retained for signature stability but is
    no longer consulted \u2014 RULING #2 deliberately decouples Alarm D
    from per-Strike Trade_HVP scope.
    """
    _ = trade_hvp  # explicit: legacy kwarg, RULING #2 ignores it
    peak = get_session_5m_adx_hwm(ticker)
    if peak < ALARM_D_SAFETY_FLOOR_ADX:
        return None
    threshold = ALARM_D_HVP_FRACTION * peak
    if current_adx_5m < threshold:
        return SentinelAction(
            alarm="D",
            reason=EXIT_REASON_ALARM_D,
            detail=(
                f"side={side} ticker={ticker} adx={current_adx_5m:.2f} "
                f"session_peak={peak:.2f} threshold={threshold:.2f}"
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
# Alarm F \u2014 Hybrid Chandelier Trailing Stop (v5.28.0)
# ---------------------------------------------------------------------------


def check_alarm_f(
    *,
    state: TrailState,
    side: str,
    entry_price: float,
    last_close: float | None,
    atr_value: float | None,
    r_dollars: float,
    shares: int,
    current_stop_price: float | None,
) -> list[SentinelAction]:
    """Evaluate Alarm F (Hybrid Chandelier Trail) for one position.

    v5.28.0 \u2014 Alarm F now serves two roles:
      1. **Closed-bar exit** (alarm code ``F_EXIT``, reason
         ``EXIT_REASON_ALARM_F_EXIT``): when Stage \u2265 2 (chandelier
         armed) and ``last_close`` has crossed the chandelier level
         (long: close \u2264 level; short: close \u2265 level), fire a
         100% exit. Mirrors Alarm B's pattern; this is what makes F
         effective in the backtest harness (no broker stop fills) and
         resilient to gap-down skips in production.
      2. **Stop tighten** (alarm code ``F``, reason
         ``EXIT_REASON_ALARM_F``): propose a strictly-tighter
         protective stop carried in ``detail_stop_price`` for the live
         broker. Same as v5.28.0 pre-redesign behaviour.

    Both can fire on the same tick (the close just crossed AND the
    chandelier is still tighter than the current stop). The caller's
    full-exit priority (``has_full_exit``) ensures the exit wins.

    Mutates ``state`` in place (peak_close ratchet, stage transitions,
    last_proposed_stop bookkeeping).

    Silently sits out when ``last_close`` is None (no closed bar yet)
    or when shares/r_dollars are non-positive (degenerate position).
    Stage 2/3 transitions further require ``atr_value`` to be available.
    """
    if last_close is None:
        return []
    if shares <= 0 or r_dollars <= 0.0:
        return []

    _f_update_trail(
        state=state,
        side=side,
        entry_price=float(entry_price),
        last_close=float(last_close),
        atr_value=atr_value,
        r_dollars=float(r_dollars),
        shares=int(shares),
    )

    actions: list[SentinelAction] = []

    # 1. Closed-bar exit \u2014 stage >= 2 AND close has crossed the level.
    cross_level = _f_should_exit_on_close_cross(
        state=state,
        side=side,
        last_close=float(last_close),
        atr_value=atr_value,
    )
    if cross_level is not None:
        side_u = str(side).upper()
        cmp_glyph = "<=" if side_u == SIDE_LONG else ">="
        atr_disp = atr_value if atr_value is not None else "na"
        actions.append(
            SentinelAction(
                alarm="F_EXIT",
                reason=EXIT_REASON_ALARM_F_EXIT,
                detail=(
                    f"side={side_u} chandelier_cross stage={state.stage} "
                    f"close={float(last_close):.4f} {cmp_glyph} "
                    f"level={cross_level:.4f} "
                    f"peak_close={state.peak_close:.4f} atr={atr_disp}"
                ),
            )
        )

    # 2. Stop tighten \u2014 always evaluated (propose tighter active stop).
    proposed = _f_propose_stop(
        state=state,
        side=side,
        entry_price=float(entry_price),
        atr_value=atr_value,
        current_stop_price=current_stop_price,
    )
    if proposed is not None:
        actions.append(
            SentinelAction(
                alarm="F",
                reason=EXIT_REASON_ALARM_F,
                detail=(
                    f"side={str(side).upper()} chandelier stage={state.stage} "
                    f"new_stop={proposed:.4f} peak_close="
                    f"{state.peak_close:.4f} atr={atr_value if atr_value is not None else 'na'}"
                ),
                detail_stop_price=proposed,
            )
        )

    return actions


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
    prev_5m_close: float | None = None,
    prev_5m_ema9: float | None = None,
    alarm_b_confirm_bars: int = 1,
    portfolio_value: float | None = None,
    adx_window: Optional["ADXTrendWindow"] = None,
    current_price: float | None = None,
    current_shares: int = 0,
    trade_hvp: TradeHVP | None = None,
    current_adx_5m: float | None = None,
    current_stop_price: float | None = None,
    divergence_memory: "DivergenceMemory | None" = None,
    current_rsi_15: float | None = None,
    ticker: str | None = None,
    # v5.28.0 \u2014 Alarm F initial-stop pin. When provided, 1R per share
    # is taken as ``abs(entry_price - initial_stop_price)`` (the actual
    # per-share risk taken at entry). When omitted, falls back to a
    # 0.5%-of-entry proxy (matches the v528 sim that produced +$407).
    initial_stop_price: float | None = None,
    # v5.28.0 \u2014 Alarm F (Hybrid Chandelier Trail). Optional; the alarm
    # silently sits out when ``trail_state`` or ``entry_price`` is None,
    # or when ``shares`` is 0, or when ``last_1m_close`` is None.
    trail_state: TrailState | None = None,
    entry_price: float | None = None,
    last_1m_close: float | None = None,
    last_1m_atr: float | None = None,
    # v6.1.0 \u2014 pass-through to check_alarm_b stateful counter path.
    position_id: str | None = None,
    now_et: "datetime | None" = None,
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

    # Alarm A \u2014 always evaluated. v5.27.0: when the caller supplies
    # ``portfolio_value`` (positive float), the per-trade hard-loss
    # threshold scales with portfolio size via
    # ``eye_of_tiger.scaled_sovereign_brake_dollars``. Otherwise the
    # spec-default ALARM_A_HARD_LOSS_DOLLARS (-$500) is used.
    if portfolio_value is not None and portfolio_value > 0:
        from eye_of_tiger import scaled_sovereign_brake_dollars

        hard_loss_threshold = scaled_sovereign_brake_dollars(portfolio_value)
    else:
        hard_loss_threshold = ALARM_A_HARD_LOSS_DOLLARS
    a_fired = check_alarm_a(
        side=side,
        unrealized_pnl=unrealized_pnl,
        position_value=position_value,
        pnl_history=pnl_history,
        now_ts=now_ts,
        hard_loss_threshold=hard_loss_threshold,
    )
    result.alarms.extend(a_fired)

    # v5.31.4 \u2014 Alarm A_STOP_PRICE (price-rail protective stop).
    # Independent of A_LOSS / A_FLASH; fires when the live mark crosses
    # the per-position protective stop set at entry by broker/orders.py
    # (entry \u00d7 (1 \u2213 STOP_PCT_OF_ENTRY)). Sits out silently when
    # either price input is missing.
    a_stop_fired = check_alarm_a_stop_price(
        side=side,
        current_price=current_price,
        current_stop_price=current_stop_price,
    )
    result.alarms.extend(a_stop_fired)

    # Alarm B \u2014 v6.4.0 gated off by default (``ALARM_B_ENABLED=False``).
    # When disabled the function is not called at all so no state is mutated
    # and no log lines are emitted. v5.27.0 widens the cross to 2-bar confirm
    # by default; spec-strict 1-bar fires only when caller explicitly passes
    # ``alarm_b_confirm_bars=1``.
    if ALARM_B_ENABLED:
        b_fired = check_alarm_b(
            side=side,
            last_5m_close=last_5m_close,
            last_5m_ema9=last_5m_ema9,
            prev_5m_close=prev_5m_close,
            prev_5m_ema9=prev_5m_ema9,
            confirm_bars=alarm_b_confirm_bars,
            position_id=position_id,
            now_et=now_et,
            # v6.3.0 noise-cross filter \u2014 entry/current/atr already in scope.
            entry_price=entry_price,
            current_price=current_price,
            last_1m_atr=last_1m_atr,
        )
        result.alarms.extend(b_fired)

    # Alarm C \u2014 v5.28.0 gated off by default (``ALARM_C_ENABLED=False``).
    # Ablation showed C tightens a stop that the harness can't fire from,
    # adding log noise without P&L. The check stays in the code so a
    # future release can flip the flag back on without a rewrite.
    if ALARM_C_ENABLED and adx_window is not None and current_price is not None:
        c_alarms, _legacy = check_alarm_c(
            adx_window=adx_window,
            side=side,
            current_price=current_price,
            current_shares=current_shares,
            current_stop_price=current_stop_price,
        )
        result.alarms.extend(c_alarms)

    # Alarm D \u2014 v5.28.0 gated off by default (``ALARM_D_ENABLED=False``).
    # Did not fire on Apr 30; held back pending a richer prod-data review.
    if ALARM_D_ENABLED and ticker is not None and current_adx_5m is not None:
        d_action = check_alarm_d(
            ticker=ticker,
            current_adx_5m=float(current_adx_5m),
            side=side,
            trade_hvp=trade_hvp,
        )
        if d_action is not None:
            result.alarms.append(d_action)

    # Alarm E \u2014 v5.28.0 gated off by default (``ALARM_E_ENABLED=False``).
    # Did not fire on Apr 30; held back pending a richer prod-data review.
    if (
        ALARM_E_ENABLED
        and divergence_memory is not None
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

    # Alarm F \u2014 Hybrid Chandelier Trail (v5.28.0). Independent of
    # A/B/C/D/E. Requires trail_state + entry_price + last_1m_close +
    # current_shares > 0; ATR is optional (Stage 1 BE proposal works
    # without it; Stage 2/3 chandelier waits for ATR readiness).
    # ``r_dollars`` reuses the same portfolio-scaled threshold as
    # Alarm A above (positive value): hard_loss_threshold is negative,
    # so flip the sign to get 1R per-trade dollars.
    if (
        trail_state is not None
        and entry_price is not None
        and last_1m_close is not None
        and current_shares > 0
    ):
        # Per-share R = actual entry-time stop distance when known,
        # else 0.5%-of-entry fallback (matches the v528 simulation that
        # produced +$407 on the Apr 30 22-pair set). The position-level
        # ``hard_loss_threshold`` (-$500 portfolio brake) is too coarse
        # for an intraday trail \u2014 across 16-106 share lots it works
        # out to $5-$30 per share, so +1R favorable rarely fires.
        if initial_stop_price is not None and entry_price is not None:
            r_per_share = abs(float(entry_price) - float(initial_stop_price))
        else:
            r_per_share = abs(float(entry_price)) * 0.005
        if r_per_share <= 0.0:
            r_per_share = abs(float(entry_price)) * 0.005
        # Convert to position-level dollars so ``check_alarm_f`` /
        # ``update_trail`` recover ``r_per_share = r_dollars / shares``.
        r_dollars = r_per_share * float(current_shares)
        f_actions = check_alarm_f(
            state=trail_state,
            side=side,
            entry_price=float(entry_price),
            last_close=float(last_1m_close),
            atr_value=last_1m_atr,
            r_dollars=r_dollars,
            shares=int(current_shares),
            current_stop_price=current_stop_price,
        )
        result.alarms.extend(f_actions)

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
    "EXIT_REASON_ALARM_D",
    "EXIT_REASON_ALARM_F",
    "EXIT_REASON_ALARM_F_EXIT",
    "ALARM_B_ENABLED",
    "ALARM_C_ENABLED",
    "ALARM_D_ENABLED",
    "ALARM_E_ENABLED",
    "TrailState",
    "EXIT_REASON_DIVERGENCE_TRAP",
    "EXIT_REASON_HVP_LOCK",
    "EXIT_REASON_PRICE_STOP",
    "EXIT_REASON_R2_HARD_STOP",
    "EXIT_REASON_VELOCITY_RATCHET",
    "_V644_MIN_HOLD_GATE_ENABLED",
    "_V644_MIN_HOLD_SECONDS",
    "_V651_DEEP_STOP_ENABLED",
    "_V651_DEEP_STOP_PCT",
    "_V651_DEEP_STOP_LONG_ONLY",
    "EXIT_REASON_V651_DEEP_STOP",
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
    "check_alarm_f",
    "evaluate_sentinel",
    "evaluate_velocity_ratchet",
    "format_sentinel_log",
    "get_session_5m_adx_hwm",
    "maybe_reset_pnl_baseline_on_shares_change",
    "new_pnl_history",
    "record_pnl",
    "record_session_5m_adx",
    "reset_session_5m_adx",
    # v6.1.0 exports
    "_V610_EMA_CONFIRM_ENABLED",
    "_V610_LUNCH_SUPPRESSION_ENABLED",
    "_ema_cross_pending",
    "reset_ema_cross_pending",
]
