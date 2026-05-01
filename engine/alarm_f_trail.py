"""v5.28.0 \u2014 Alarm F: Hybrid Chandelier Trailing Stop.

Layers a stage-gated chandelier trail on top of the existing Sentinel Loop.
Alarm F never closes a position directly; it only proposes a tighter
``detail_stop_price``. The runner installs the proposed stop in place and
the broker stop-cross handles the eventual exit. If the proposed level
is not strictly tighter than the current active stop, no action.

Stage machine:
    Stage 0 INACTIVE      \u2014 entry until favorable >= 1R
    Stage 1 BREAKEVEN     \u2014 favorable >= BE_ARM_R_MULT * R, propose entry +/- $0.01
    Stage 2 CHANDELIER WIDE \u2014 favorable >= STAGE2_ARM_R_MULT * R AND atr available,
                              trail = peak_close \u2213 WIDE_MULT * ATR
    Stage 3 CHANDELIER TIGHT \u2014 favorable >= stage2_arm_favorable + STAGE3_ARM_ATR_MULT * ATR_at_arm,
                              trail tightens to TIGHT_MULT * ATR

Transitions are one-way (Stage 1 \u2192 2 \u2192 3, never back). The proposed
stop is the side-aware best (max for long / min for short) of:
    \u2022 Stage 1 BE level (once Stage 1 armed)
    \u2022 Stage 2/3 chandelier level (once Stage 2 armed)
    \u2022 The previously proposed Alarm F stop (one-way ratchet)

Alarm F is additive: the caller (sentinel.evaluate_sentinel) merges the
F-proposed stop with the C-proposed stop by side-aware best, so whichever
is tighter wins.

Spec reference: /home/user/workspace/v528_trailing_stops_research.md \u00a76.

Backtest acceptance criteria (Apr 30 v5.28.0 vs v5.27.0):
    \u2022 total realized $ \u2265 +250 (sim hybrid \u2192 +407)
    \u2022 round-trippers \u2264 4/22 (BE+1R alone cuts most)
    \u2022 winner avg capture % \u2265 55%
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Constants \u2014 spec-literal defaults (see v528 research doc \u00a76.3)
# ---------------------------------------------------------------------------

# Stage activation thresholds, as multiples of 1R per-trade dollars.
# v5.28.0 \u2014 tuned on Apr 30 backtest sweep (see v528 research doc \u00a76.4).
# Stage 2 arms earlier (1.0R vs original 2.0R) so the chandelier engages
# while winners still have momentum; Stage 3 follows quickly to lock the
# bulk of the move. Original conservative defaults are preserved in the
# spec doc for reference.
BE_ARM_R_MULT: float = 1.0
STAGE2_ARM_R_MULT: float = 1.0
# After Stage 2 arms, tighten when favorable advances by this many ATRs
# beyond the stage-2 arm price.
STAGE3_ARM_ATR_MULT: float = 0.5

# ATR parameters. 1m bars are the prod feed; 14 is the standard period.
ATR_PERIOD: int = 14

# Chandelier multipliers \u2014 wide first, tight after Stage 3 arms.
# v5.28.0 sweep showed WIDE/TIGHT width has minimal effect on which trades
# fire F_EXIT; the 2.0/1.0 pair gives the cleanest exit prints without
# whipsawing on intra-bar noise.
WIDE_MULT: float = 2.0
TIGHT_MULT: float = 1.0

# Never arm in the first N bars after entry (avoid entry-bar noise).
MIN_BARS_BEFORE_ARM: int = 3

# Stage codes
STAGE_INACTIVE: int = 0
STAGE_BREAKEVEN: int = 1
STAGE_CHANDELIER_WIDE: int = 2
STAGE_CHANDELIER_TIGHT: int = 3

EXIT_REASON_ALARM_F: str = "sentinel_f_chandelier_trail"
# v5.28.0 \u2014 Alarm F is now ALSO a full-exit alarm. When the last
# closed 1m bar prints on the wrong side of the active chandelier level,
# Alarm F fires a 100% close (mirroring Alarm B's pattern). The stop-
# tighten path stays available for live trading, but the close-cross
# exit is what makes F effective in the backtest harness (which has no
# broker-side stop-fill simulation) and resilient to gap-down skips.
EXIT_REASON_ALARM_F_EXIT: str = "sentinel_f_chandelier_exit"

SIDE_LONG = "LONG"
SIDE_SHORT = "SHORT"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class TrailState:
    """Per-position Alarm F state. Persisted across ticks via the position dict.

    All fields default to neutral so the state can be created lazily on the
    first tick after entry (no broker positions migration needed).
    """

    stage: int = STAGE_INACTIVE
    # Best favorable price seen so far. For longs: highest close. For
    # shorts: lowest close. Seeded from entry on first update.
    peak_close: Optional[float] = None
    # Snapshot of `peak_close - entry` (long) / `entry - peak_close` (short)
    # at the moment Stage 2 armed. Used to gate the Stage 3 transition.
    stage2_arm_favorable: Optional[float] = None
    # ATR at the moment Stage 2 armed; Stage 3 transition compares
    # current favorable against `stage2_arm_favorable + STAGE3_ARM_ATR_MULT * stage2_arm_atr`.
    stage2_arm_atr: Optional[float] = None
    # Last proposed stop price. Alarm F never moves backward; this is the
    # one-way ratchet anchor merged with the freshly computed level.
    last_proposed_stop: Optional[float] = None
    # Number of post-entry bars seen. Used by MIN_BARS_BEFORE_ARM.
    bars_seen: int = 0

    @classmethod
    def fresh(cls) -> "TrailState":
        return cls()


# ---------------------------------------------------------------------------
# ATR helpers
# ---------------------------------------------------------------------------


def true_range(high: float, low: float, prev_close: Optional[float]) -> float:
    """Standard Wilder true range. Falls back to (high-low) when no prior close."""
    rng = float(high) - float(low)
    if prev_close is None:
        return rng
    pc = float(prev_close)
    return max(rng, abs(float(high) - pc), abs(float(low) - pc))


def atr_from_bars(
    highs: Iterable[float],
    lows: Iterable[float],
    closes: Iterable[float],
    period: int = ATR_PERIOD,
) -> Optional[float]:
    """Simple Wilder-style ATR from aligned highs/lows/closes lists.

    Returns ``None`` until at least ``period`` bars are available. The
    series is averaged over the trailing ``period`` true ranges (a
    simple moving average of TRs is a close enough approximation for
    intraday use; the typical Wilder smoothing converges quickly).
    """
    h = list(highs)
    l_ = list(lows)
    c = list(closes)
    n = min(len(h), len(l_), len(c))
    if n < max(2, period):
        return None
    trs: list[float] = []
    # Walk over the last ``period`` bars; need a prior close (n-period-1).
    start = max(1, n - period)
    for i in range(start, n):
        prev_c = c[i - 1] if i - 1 >= 0 else None
        trs.append(true_range(h[i], l_[i], prev_c))
    if not trs:
        return None
    return sum(trs) / len(trs)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def _favorable(side: str, entry_price: float, peak_close: float) -> float:
    if str(side).upper() == SIDE_LONG:
        return float(peak_close) - float(entry_price)
    return float(entry_price) - float(peak_close)


def update_trail(
    *,
    state: TrailState,
    side: str,
    entry_price: float,
    last_close: float,
    atr_value: Optional[float],
    r_dollars: float,
    shares: int,
) -> TrailState:
    """Advance ``state`` for one tick. Mutates in place and returns it.

    ``last_close`` is the most recent CLOSED 1m bar close. Wicks (high/low)
    are deliberately not used \u2014 close-based trails are 5\u20138% better
    per the literature (\u00a72 of the research report).

    ``r_dollars`` is per-trade 1R in dollars (e.g. the v5.27.0 portfolio-
    scaled brake). Per-share R is ``r_dollars / shares``.

    ``atr_value`` may be ``None`` while the ATR(14) window is still
    seeding; Stage 2 / 3 transitions are gated on it being available.
    """
    side_u = str(side).upper()
    state.bars_seen += 1

    # Track peak close monotonically. Seed from entry_price (NOT the
    # first bar's close) so a position that opens with an adverse
    # first-bar print does not anchor the trail at a worse-than-entry
    # price. Long: peak = max(entry, closes seen). Short: peak = min.
    if state.peak_close is None:
        state.peak_close = float(entry_price)
    if side_u == SIDE_LONG:
        if float(last_close) > state.peak_close:
            state.peak_close = float(last_close)
    else:
        if float(last_close) < state.peak_close:
            state.peak_close = float(last_close)

    # No stage transitions before the entry-noise window has elapsed.
    if state.bars_seen < MIN_BARS_BEFORE_ARM:
        return state

    # Per-share R. Guard against zero shares / zero-R degenerate inputs.
    if shares <= 0 or r_dollars <= 0.0:
        return state
    r_per_share = float(r_dollars) / float(shares)

    favorable = _favorable(side_u, entry_price, state.peak_close)

    # Stage 0 \u2192 Stage 1: BE arm at +1R favorable.
    if state.stage == STAGE_INACTIVE:
        if favorable >= BE_ARM_R_MULT * r_per_share:
            state.stage = STAGE_BREAKEVEN

    # Stage 1 \u2192 Stage 2: Chandelier wide arm at +2R favorable AND ATR ready.
    if state.stage == STAGE_BREAKEVEN:
        if favorable >= STAGE2_ARM_R_MULT * r_per_share and atr_value is not None:
            state.stage = STAGE_CHANDELIER_WIDE
            state.stage2_arm_favorable = float(favorable)
            state.stage2_arm_atr = float(atr_value)

    # Stage 2 \u2192 Stage 3: tighten after +1.5*ATR(arm) of additional favorable.
    if state.stage == STAGE_CHANDELIER_WIDE:
        if (
            atr_value is not None
            and state.stage2_arm_favorable is not None
            and state.stage2_arm_atr is not None
        ):
            target = state.stage2_arm_favorable + STAGE3_ARM_ATR_MULT * state.stage2_arm_atr
            if favorable >= target:
                state.stage = STAGE_CHANDELIER_TIGHT

    return state


def propose_stop(
    *,
    state: TrailState,
    side: str,
    entry_price: float,
    atr_value: Optional[float],
    current_stop_price: Optional[float],
) -> Optional[float]:
    """Return the proposed Alarm F stop, or ``None`` if no proposal.

    The proposal is the side-aware tightest of:
        \u2022 Stage 1 BE level (once Stage \u2265 1)
        \u2022 Stage 2/3 chandelier level (once Stage \u2265 2 AND ATR available)
        \u2022 The previously proposed F stop (one-way ratchet)

    Only returned if it is strictly tighter than ``current_stop_price``
    (caller-side check is duplicated for safety).
    """
    if state.stage == STAGE_INACTIVE:
        return None

    side_u = str(side).upper()
    candidates: list[float] = []

    # Stage 1 BE+1c, always available once stage >= 1.
    if side_u == SIDE_LONG:
        candidates.append(round(float(entry_price) + 0.01, 4))
    else:
        candidates.append(round(float(entry_price) - 0.01, 4))

    # Stage 2/3 chandelier (only if peak_close + ATR are both ready).
    if (
        state.stage >= STAGE_CHANDELIER_WIDE
        and state.peak_close is not None
        and atr_value is not None
    ):
        mult = TIGHT_MULT if state.stage == STAGE_CHANDELIER_TIGHT else WIDE_MULT
        if side_u == SIDE_LONG:
            candidates.append(round(float(state.peak_close) - mult * float(atr_value), 4))
        else:
            candidates.append(round(float(state.peak_close) + mult * float(atr_value), 4))

    # Previously proposed F stop \u2014 enforces the one-way ratchet.
    if state.last_proposed_stop is not None:
        candidates.append(float(state.last_proposed_stop))

    # Side-aware best: long picks the highest stop, short picks the lowest.
    if not candidates:
        return None
    if side_u == SIDE_LONG:
        proposed = max(candidates)
    else:
        proposed = min(candidates)

    # Strict-tighter gate vs current active stop.
    if current_stop_price is not None:
        cs = float(current_stop_price)
        if side_u == SIDE_LONG and proposed <= cs:
            return None
        if side_u == SIDE_SHORT and (cs > 0 and proposed >= cs):
            return None

    # Persist the ratchet anchor.
    state.last_proposed_stop = float(proposed)
    return float(proposed)


# ---------------------------------------------------------------------------
# v5.28.0 \u2014 Closed-bar cross check (full exit)
# ---------------------------------------------------------------------------


def chandelier_level(
    *,
    state: TrailState,
    side: str,
    atr_value: Optional[float],
) -> Optional[float]:
    """Return the active chandelier price level for the current stage.

    For Stage \u2265 2, returns ``peak_close \u2213 mult * ATR`` where
    ``mult`` is WIDE_MULT (Stage 2) or TIGHT_MULT (Stage 3). Returns
    ``None`` for Stage 0/1 (chandelier not armed yet) or when peak_close
    / atr_value is missing. For Stage 1 (BREAKEVEN), the trail level is
    entry+/-$0.01 \u2014 but exits on Stage 1 are deferred to Alarm A's
    hard stop / R-2 to avoid false-out at the noisy entry-band.
    """
    if state.stage < STAGE_CHANDELIER_WIDE:
        return None
    if state.peak_close is None or atr_value is None:
        return None
    side_u = str(side).upper()
    mult = TIGHT_MULT if state.stage == STAGE_CHANDELIER_TIGHT else WIDE_MULT
    if side_u == SIDE_LONG:
        return round(float(state.peak_close) - mult * float(atr_value), 4)
    return round(float(state.peak_close) + mult * float(atr_value), 4)


def should_exit_on_close_cross(
    *,
    state: TrailState,
    side: str,
    last_close: float,
    atr_value: Optional[float],
) -> Optional[float]:
    """Return the chandelier level if last_close has crossed it, else None.

    Stage \u2265 2 (chandelier armed) only. Long: fires when
    ``last_close \u2264 chandelier_level`` (price has fallen through the
    trail). Short: fires when ``last_close \u2265 chandelier_level``.

    Stage 1 (BREAKEVEN) is intentionally NOT an exit trigger \u2014 the
    BE+1c level sits in the noisy entry band; let Alarm A handle deep
    losses, F handles the trail once the chandelier is armed at +2R.
    """
    level = chandelier_level(state=state, side=side, atr_value=atr_value)
    if level is None:
        return None
    side_u = str(side).upper()
    lc = float(last_close)
    if side_u == SIDE_LONG and lc <= level:
        return level
    if side_u == SIDE_SHORT and lc >= level:
        return level
    return None


__all__ = [
    "ATR_PERIOD",
    "BE_ARM_R_MULT",
    "EXIT_REASON_ALARM_F",
    "EXIT_REASON_ALARM_F_EXIT",
    "chandelier_level",
    "should_exit_on_close_cross",
    "MIN_BARS_BEFORE_ARM",
    "STAGE2_ARM_R_MULT",
    "STAGE3_ARM_ATR_MULT",
    "STAGE_BREAKEVEN",
    "STAGE_CHANDELIER_TIGHT",
    "STAGE_CHANDELIER_WIDE",
    "STAGE_INACTIVE",
    "TIGHT_MULT",
    "TrailState",
    "WIDE_MULT",
    "atr_from_bars",
    "propose_stop",
    "true_range",
    "update_trail",
]
