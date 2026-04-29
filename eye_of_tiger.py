"""v5.10.0 \u2014 Project Eye of the Tiger: pure decision functions.

This module is the canonical implementation of Gene Stepanov's
"Eye of the Tiger" algorithm (canonical truth source:
specs/canonical/eye_of_the_tiger_gene_2026-04-28c.md). It fully
replaces the v5.6.0 \u2192 v5.9.4 entry/exit logic. No feature flag.

Six sections:
  I.   Global Permit (QQQ Index Shield)        \u2014 evaluate_global_permit
  II.  Ticker-Specific Permits (Entry-1 only)  \u2014 evaluate_volume_bucket / evaluate_boundary_hold
  III. Entry & Sizing (Scaled 50/50)           \u2014 evaluate_entry_1 / evaluate_entry_2
  IV.  High-Priority Overrides (tick-by-tick)  \u2014 evaluate_sovereign_brake / evaluate_velocity_fuse
  V.   Stop-Loss Hierarchy (Triple-Lock)       \u2014 evaluate_maffei_inside_or / two_bar_lock_step / evaluate_ema_trail
  VI.  Systematic Machine Rules                \u2014 daily_circuit_breaker_tripped

Pure functions over plain dicts. No I/O. The integration glue lives
in trade_genius.py.
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timezone
from typing import Optional

# ---------------------------------------------------------------------
# Section I/IX configuration (locked defaults)
# ---------------------------------------------------------------------

DMI_PERIOD = 15
ENTRY_1_DI_THRESHOLD = 25.0
ENTRY_2_DI_THRESHOLD = 30.0
ENTRY_1_SIZE_PCT = 0.50
ENTRY_2_SIZE_PCT = 0.50
ENTRY_2_REQUIRE_FRESH_NHOD = True

SOVEREIGN_BRAKE_DOLLARS = -500.0
VELOCITY_FUSE_PCT = 0.01

TWO_BAR_LOCK_FAVORABLE_COUNT = 2
LEASH_EMA_PERIOD = 9
LEASH_EMA_TIMEFRAME_MIN = 5

DAILY_CIRCUIT_BREAKER_DOLLARS = -1500.0

OR_WINDOW_START_HHMM_ET = "09:30"
OR_WINDOW_END_HHMM_ET = "09:35"
BOUNDARY_HOLD_REQUIRED_CLOSES = 2

PERMIT_QQQ_EMA_PERIOD = 9
PERMIT_QQQ_TIMEFRAME_MIN = 5
PERMIT_AVWAP_ANCHOR_HHMM = "09:30"

# ---------------------------------------------------------------------
# Side / phase enums (string for JSON-serialisability)
# ---------------------------------------------------------------------

SIDE_LONG = "LONG"
SIDE_SHORT = "SHORT"

PHASE_SURVIVAL = "survival"
PHASE_NEUT_LAYERED = "neutrality_layered"
PHASE_NEUT_LOCKED = "neutrality_locked"
PHASE_EXTRACTION = "extraction"

EXIT_REASON_SOVEREIGN_BRAKE = "sovereign_brake"
EXIT_REASON_VELOCITY_FUSE = "velocity_fuse"
EXIT_REASON_FORENSIC_STOP = "forensic_stop"
EXIT_REASON_BE_STOP = "be_stop"
EXIT_REASON_EMA_TRAIL = "ema_trail"
EXIT_REASON_DAILY_CIRCUIT_BREAKER = "daily_circuit_breaker"
EXIT_REASON_EOD = "eod"
EXIT_REASON_MANUAL = "manual"

VALID_EXIT_REASONS = (
    EXIT_REASON_SOVEREIGN_BRAKE,
    EXIT_REASON_VELOCITY_FUSE,
    EXIT_REASON_FORENSIC_STOP,
    EXIT_REASON_BE_STOP,
    EXIT_REASON_EMA_TRAIL,
    EXIT_REASON_DAILY_CIRCUIT_BREAKER,
    EXIT_REASON_EOD,
    EXIT_REASON_MANUAL,
)


# =====================================================================
# Section I \u2014 Global Permit (Index Shield)
# =====================================================================

def evaluate_global_permit(
    side: str,
    qqq_5m_close: float | None,
    qqq_5m_ema9: float | None,
    qqq_current_price: float | None,
    qqq_avwap_0930: float | None,
) -> dict:
    """Section I evaluation. Returns {open: bool, reason: str}.

    Both Market Shield (QQQ 5m close vs 9-EMA) and Sovereign Anchor
    (QQQ current price vs 9:30 AVWAP) must align with the side.
    None inputs collapse to CLOSED with a `data_missing` reason.
    """
    if side not in (SIDE_LONG, SIDE_SHORT):
        return {"open": False, "reason": f"bad_side:{side}"}
    if (qqq_5m_close is None or qqq_5m_ema9 is None
            or qqq_current_price is None or qqq_avwap_0930 is None):
        return {"open": False, "reason": "data_missing"}
    if side == SIDE_LONG:
        shield = qqq_5m_close > qqq_5m_ema9
        anchor = qqq_current_price > qqq_avwap_0930
    else:
        shield = qqq_5m_close < qqq_5m_ema9
        anchor = qqq_current_price < qqq_avwap_0930
    if shield and anchor:
        return {"open": True, "reason": "open"}
    if not shield and not anchor:
        return {"open": False, "reason": "shield_and_anchor"}
    if not shield:
        return {"open": False, "reason": "shield_misaligned"}
    return {"open": False, "reason": "anchor_misaligned"}


# =====================================================================
# Section II.1 \u2014 Volume Bucket gate (helper)
# =====================================================================

def evaluate_volume_bucket(check_result: dict | None) -> bool:
    """Translate VolumeBucketBaseline.check() output to gate-open
    boolean. COLDSTART counts as PASS-THROUGH (gate satisfied).

    Runtime override (v5.13.1): when
    ``engine.feature_flags.VOLUME_GATE_ENABLED`` is False (production
    default), the gate auto-passes regardless of bucket result —
    reason ``DISABLED_BY_FLAG``. The 2-consecutive-1m boundary-hold
    gate is unaffected and still fully enforced.
    """
    from engine import feature_flags as _ff
    if not _ff.VOLUME_GATE_ENABLED:
        return True
    if not check_result:
        return False
    g = check_result.get("gate")
    return g == "PASS" or g == "COLDSTART"


# =====================================================================
# Section II.2 \u2014 Boundary Hold
# =====================================================================

def evaluate_boundary_hold(
    side: str,
    or_high: float | None,
    or_low: float | None,
    last_n_1m_closes: list[float],
    required_closes: int = BOUNDARY_HOLD_REQUIRED_CLOSES,
) -> dict:
    """Two consecutive closed 1m candles strictly outside the 5m OR.

    `last_n_1m_closes` is newest-last; the function inspects the most
    recent `required_closes` entries. A close at the boundary breaks
    the hold (strict `>` for LONG, strict `<` for SHORT).
    """
    if side not in (SIDE_LONG, SIDE_SHORT):
        return {"hold": False, "reason": f"bad_side:{side}",
                "consecutive_outside": 0}
    if or_high is None or or_low is None:
        return {"hold": False, "reason": "or_not_set",
                "consecutive_outside": 0}
    if not last_n_1m_closes or len(last_n_1m_closes) < required_closes:
        return {"hold": False, "reason": "insufficient_closes",
                "consecutive_outside": 0}
    closes = list(last_n_1m_closes)[-required_closes:]
    if side == SIDE_LONG:
        outside = [c is not None and c > or_high for c in closes]
    else:
        outside = [c is not None and c < or_low for c in closes]
    n_consec = sum(1 for x in outside if x) if all(outside) else 0
    if all(outside):
        return {"hold": True, "reason": "satisfied",
                "consecutive_outside": required_closes}
    # report best-effort consecutive count for diagnostics
    cnt = 0
    for x in reversed(outside):
        if x:
            cnt += 1
        else:
            break
    return {"hold": False, "reason": "not_satisfied",
            "consecutive_outside": cnt}


def boundary_hold_earliest_satisfaction_et(
    or_window_end_hhmm: str = OR_WINDOW_END_HHMM_ET,
    required_closes: int = BOUNDARY_HOLD_REQUIRED_CLOSES,
) -> dtime:
    """Earliest possible Entry 1 wall-clock time. The OR window is
    [09:30:00, 09:34:59.999]; the first 1m close strictly outside is
    the 9:35 candle (closes at 9:35:00). With required_closes = 2,
    the earliest qualifying second close is the 9:36 candle.
    """
    hh, mm = or_window_end_hhmm.split(":")
    h, m = int(hh), int(mm)
    m += required_closes - 1
    while m >= 60:
        m -= 60
        h += 1
    return dtime(h, m, 0)


# =====================================================================
# Section III \u2014 Entry triggers
# =====================================================================

def evaluate_entry_1(
    side: str,
    *,
    permit_open: bool,
    volume_bucket_ok: bool,
    boundary_hold_ok: bool,
    di_5m: float | None,
    di_1m: float | None,
    is_nhod_or_nlod: bool,
    di_threshold: float = ENTRY_1_DI_THRESHOLD,
) -> dict:
    """Returns {fire: bool, reason: str}. All gates must align on the
    same evaluation tick. DI thresholds are strict `>`.
    """
    if side not in (SIDE_LONG, SIDE_SHORT):
        return {"fire": False, "reason": f"bad_side:{side}"}
    if not permit_open:
        return {"fire": False, "reason": "permit_closed"}
    if not volume_bucket_ok:
        return {"fire": False, "reason": "volume_bucket"}
    if not boundary_hold_ok:
        return {"fire": False, "reason": "boundary_hold"}
    if di_5m is None or di_5m <= di_threshold:
        return {"fire": False, "reason": "di_5m"}
    if di_1m is None or di_1m <= di_threshold:
        return {"fire": False, "reason": "di_1m"}
    if not is_nhod_or_nlod:
        return {"fire": False, "reason": "no_extreme_print"}
    return {"fire": True, "reason": "all_gates_pass"}


def evaluate_entry_2(
    side: str,
    *,
    entry_1_active: bool,
    permit_open_at_trigger: bool,
    di_1m_prev: float | None,
    di_1m_now: float | None,
    fresh_nhod_or_nlod: bool,
    entry_2_already_fired: bool,
    di_threshold: float = ENTRY_2_DI_THRESHOLD,
) -> dict:
    """Entry 2 fires on the edge transition `<= 30` \u2192 `> 30` AND
    a fresh session-extreme print past Entry-1 HWM. At most once per
    Entry-1 lifecycle.

    Conservative interpretation per spec XIV.3: Section I must still
    be OPEN at the moment of trigger (open question; flag with Gene).
    """
    if side not in (SIDE_LONG, SIDE_SHORT):
        return {"fire": False, "reason": f"bad_side:{side}"}
    if not entry_1_active:
        return {"fire": False, "reason": "entry_1_not_active"}
    if entry_2_already_fired:
        return {"fire": False, "reason": "already_fired"}
    if not permit_open_at_trigger:
        return {"fire": False, "reason": "permit_closed"}
    if di_1m_now is None:
        return {"fire": False, "reason": "di_now_missing"}
    prev = di_1m_prev if di_1m_prev is not None else -1.0
    crossed = prev <= di_threshold and di_1m_now > di_threshold
    if not crossed:
        return {"fire": False, "reason": "no_crossing"}
    if ENTRY_2_REQUIRE_FRESH_NHOD and not fresh_nhod_or_nlod:
        return {"fire": False, "reason": "no_fresh_extreme"}
    return {"fire": True, "reason": "crossing_and_fresh_extreme"}


def is_fresh_nhod(current_price: float, entry_1_hwm: float) -> bool:
    return current_price is not None and entry_1_hwm is not None and current_price > entry_1_hwm


def is_fresh_nlod(current_price: float, entry_1_lwm: float) -> bool:
    return current_price is not None and entry_1_lwm is not None and current_price < entry_1_lwm


# =====================================================================
# Section IV \u2014 Tick-by-tick overrides
# =====================================================================

def evaluate_sovereign_brake(
    unrealized_pnl_dollars: float,
    threshold: float = SOVEREIGN_BRAKE_DOLLARS,
) -> bool:
    """Fires on `unrealized_pnl <= -$500`. Returns True \u2192 immediate
    market exit.
    """
    if unrealized_pnl_dollars is None:
        return False
    return float(unrealized_pnl_dollars) <= threshold


def evaluate_velocity_fuse(
    side: str,
    current_price: float | None,
    current_1m_open: float | None,
    pct: float = VELOCITY_FUSE_PCT,
) -> bool:
    """LONG fires on `current_price < open * (1 - pct)` strictly;
    SHORT fires on `current_price > open * (1 + pct)` strictly.
    Exactly 1.000% does NOT trigger (strict `<` / `>`).
    """
    if current_price is None or current_1m_open is None:
        return False
    try:
        cp = float(current_price)
        op = float(current_1m_open)
    except (TypeError, ValueError):
        return False
    if op <= 0.0:
        return False
    if side == SIDE_LONG:
        return cp < op * (1.0 - pct)
    if side == SIDE_SHORT:
        return cp > op * (1.0 + pct)
    return False


# =====================================================================
# Section V \u2014 Stop-loss hierarchy
# =====================================================================

def evaluate_maffei_inside_or(
    side: str,
    or_high: float | None,
    or_low: float | None,
    current_1m_open: float | None,
    current_1m_close: float | None,
    current_1m_low: float | None,
    current_1m_high: float | None,
    prior_1m_low: float | None,
    prior_1m_high: float | None,
) -> dict:
    """Phase A Maffei 1-2-3 Recursive Gate.

    Gate fires when the current 1m candle closes back INSIDE the OR
    (Gene's wording). The audit then exits if low < prior_low (LONG)
    or high > prior_high (SHORT). Equality = STAY.

    Returns {gated: bool, decision: 'STAY'|'EXIT', reason: str}.
    """
    if side not in (SIDE_LONG, SIDE_SHORT):
        return {"gated": False, "decision": "STAY", "reason": f"bad_side:{side}"}
    if (or_high is None or or_low is None
            or current_1m_open is None or current_1m_close is None
            or current_1m_low is None or current_1m_high is None
            or prior_1m_low is None or prior_1m_high is None):
        return {"gated": False, "decision": "STAY", "reason": "data_missing"}
    if side == SIDE_LONG:
        gated = (current_1m_close <= or_high) and (current_1m_open >= or_high)
        if not gated:
            return {"gated": False, "decision": "STAY", "reason": "no_re_entry"}
        if current_1m_low < prior_1m_low:
            return {"gated": True, "decision": "EXIT", "reason": "lower_low"}
        return {"gated": True, "decision": "STAY", "reason": "low_held"}
    # SHORT mirror
    gated = (current_1m_close >= or_low) and (current_1m_open <= or_low)
    if not gated:
        return {"gated": False, "decision": "STAY", "reason": "no_re_entry"}
    if current_1m_high > prior_1m_high:
        return {"gated": True, "decision": "EXIT", "reason": "higher_high"}
    return {"gated": True, "decision": "STAY", "reason": "high_held"}


def is_favorable_5m_candle(side: str, candle_open: float, candle_close: float) -> bool:
    if candle_open is None or candle_close is None:
        return False
    if side == SIDE_LONG:
        return candle_close > candle_open
    if side == SIDE_SHORT:
        return candle_close < candle_open
    return False


def two_bar_lock_step(
    side: str,
    counter: int,
    candle_open: float,
    candle_close: float,
    target: int = TWO_BAR_LOCK_FAVORABLE_COUNT,
) -> dict:
    """Advance the Two-Bar Lock counter on each post-Entry-2
    closed 5m candle. Resets on a non-favorable close.

    Returns {counter: int, locked: bool}.
    """
    fav = is_favorable_5m_candle(side, candle_open, candle_close)
    new = (counter + 1) if fav else 0
    return {"counter": new, "locked": new >= target}


def evaluate_ema_trail(
    side: str,
    candle_5m_close: float | None,
    ema_9_5m: float | None,
) -> bool:
    """The Leash. LONG exits on 5m close < EMA9; SHORT exits on
    5m close > EMA9. Caller is responsible for ensuring EMA is
    seeded (>= 9 closed 5m candles since 9:30 ET).
    """
    if candle_5m_close is None or ema_9_5m is None:
        return False
    if side == SIDE_LONG:
        return candle_5m_close < ema_9_5m
    if side == SIDE_SHORT:
        return candle_5m_close > ema_9_5m
    return False


# =====================================================================
# Section VI \u2014 Machine rules
# =====================================================================

def daily_circuit_breaker_tripped(
    cumulative_realized_pnl: float,
    threshold: float = DAILY_CIRCUIT_BREAKER_DOLLARS,
) -> bool:
    if cumulative_realized_pnl is None:
        return False
    return float(cumulative_realized_pnl) <= threshold


# =====================================================================
# State helpers
# =====================================================================

def new_position_state(side: str) -> dict:
    if side not in (SIDE_LONG, SIDE_SHORT):
        raise ValueError(f"bad side {side!r}")
    return {
        "side": side,
        "entry_1_price": None,
        "entry_1_shares": 0,
        "entry_1_ts": None,
        "entry_2_price": None,
        "entry_2_shares": 0,
        "entry_2_ts": None,
        "avg_entry": None,
        "phase": PHASE_SURVIVAL,
        "current_stop": None,
        "favorable_5m_count": 0,
        "ema_5m": None,
        "ema_seeded": False,
        # bookkeeping
        "entry_1_hwm": None,   # session-extreme at Entry 1 time (HWM for LONG, LWM for SHORT)
        "entry_2_fired": False,
        "di_1m_prev": None,
    }


def transition_phase_on_entry_2(state: dict) -> dict:
    """Phase B step 1: Layered Shield. First 50% gets BE stop at
    Entry 1 price; second 50% has no stop yet. Maffei deactivates
    on the first 50%.
    """
    state["phase"] = PHASE_NEUT_LAYERED
    state["entry_2_fired"] = True
    state["current_stop"] = state.get("entry_1_price")
    return state


def transition_phase_on_two_bar_lock(state: dict) -> dict:
    """Phase B step 2: Two-Bar Lock. Entire 100% stop = avg_entry."""
    state["phase"] = PHASE_NEUT_LOCKED
    state["current_stop"] = state.get("avg_entry")
    return state


def transition_phase_to_extraction(state: dict) -> dict:
    """Phase C: ema_trail takes authority. Two-Bar Lock BE remains
    as floor (caller still honours `current_stop`).
    """
    state["phase"] = PHASE_EXTRACTION
    return state
