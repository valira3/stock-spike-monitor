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
# v6.2.0 \u2014 Entry-1 DI threshold lowered 25.0 -> 22.0 globally.
# Forensics on 5/1 prod showed 44 V5100_ENTRY1:di_5m rejections on TSLA
# alone where DI+_5m sat in [22, 25) on a +2.03% trend day; 100%
# would-have-profited at 5-min forward returns. The same gate also
# blocked clean breakouts on AVGO/GOOG/AMZN. ADX>20 (the strongest-edge
# gate) is unchanged so this stays a momentum-confirmed entry; we are
# only widening the directional-imbalance hurdle.
ENTRY_1_DI_THRESHOLD = 22.0
ENTRY_2_DI_THRESHOLD = 30.0
ENTRY_1_SIZE_PCT = 0.50
ENTRY_2_SIZE_PCT = 0.50
ENTRY_2_REQUIRE_FRESH_NHOD = True

# v5.31.4 \u2014 percent-of-entry stop. The R-2 hard stop in v5.26.0
# reverse-derived a price stop from a fixed $500 rail divided by
# share count, which produced 5%%+ stops on $200 tickers and tight
# stops on $5 tickers. Switched to a symmetric percent-of-entry rule
# at operator request ("stop should not be sized by the number of
# shares"). The dollar-rail R-2 backstop remains in evaluate_sentinel
# as the deeper safety net.
STOP_PCT_OF_ENTRY = 0.005  # 0.5%% \u2014 long stop = entry * 0.995 (v6.4.1: short uses STOP_PCT_SHORT)

# v6.4.1 \u2014 asymmetric stops. Backtest sweep (Apr 27\u2013May 1, see
# /home/user/workspace/v640_short_stop_sweep/report.md) showed shorts have
# asymmetric per-share variance: avg short loss was -$2.02/share vs avg
# short win at +$1.65/share. Tightening short stops to 30bp (vs symmetric
# 50bp baseline) lifted weekly P&L by +$262 (+30%) without hurting longs.
# 25bp was too tight (chopped out on noise). 30bp is the empirical sweet
# spot. Long pct unchanged at 50bp.
STOP_PCT_LONG = 0.005   # 50bp \u2014 long stop = entry * (1 - 0.005)
STOP_PCT_SHORT = 0.003  # 30bp \u2014 short stop = entry * (1 + 0.003)

# v6.4.2 \u2014 post-loss cooldown. After a stop-out (any losing exit), block
# new entries on the same (ticker, side) for POST_LOSS_COOLDOWN_MIN minutes.
# Apr 27\u2013May 1 backtest at /home/user/workspace/v641_week_backtest/report.md
# showed three same-side same-ticker re-entries fired within 30 minutes of
# a stop-out (TSLA short, META short, AMZN short) and ALL three lost money
# again \u2014 a clean chase pattern. Adding a 30-minute cooldown captures all
# three on the sample (+$107/wk lift) without blocking productive
# post-WIN re-entry chains (NVDA shorts, MSFT shorts, ORCL longs).
#
# v6.4.3 \u2014 asymmetric per-side cooldowns. Apr 27\u2013May 1 sweep at
# /home/user/workspace/v642_cooldown_sweep/report.md showed the long-side
# cooldown blocks more legitimate winners than chase losses (NFLX +$45 x2,
# TSLA +$97). Default longs OFF, shorts 30 min. Mirrors v6.4.1 stops
# asymmetry: shorts have larger per-share variance and chase harder on this
# universe; longs ride momentum back up after a stop.
#
# Resolution order for each side:
#   1. POST_LOSS_COOLDOWN_MIN_LONG   / _SHORT  (per-side override)
#   2. POST_LOSS_COOLDOWN_MIN                  (legacy single-window fallback)
#   3. baked-in default: long=0 (off), short=30
# 0 disables that side.
import os as _os


def _read_int(env_name, default):
    try:
        v = _os.getenv(env_name)
        return int(v) if v is not None else default
    except ValueError:
        return default


# Legacy alias \u2014 still read by older callers; resolves to the SHORT default
# so a v6.4.2 operator who set POST_LOSS_COOLDOWN_MIN=15 keeps that as a
# baseline for both sides until they upgrade.
POST_LOSS_COOLDOWN_MIN = _read_int("POST_LOSS_COOLDOWN_MIN", 30)

# Per-side overrides (v6.4.3 default: long=off, short=30)
POST_LOSS_COOLDOWN_MIN_LONG = _read_int(
    "POST_LOSS_COOLDOWN_MIN_LONG",
    _read_int("POST_LOSS_COOLDOWN_MIN", 0),
)
POST_LOSS_COOLDOWN_MIN_SHORT = _read_int(
    "POST_LOSS_COOLDOWN_MIN_SHORT",
    _read_int("POST_LOSS_COOLDOWN_MIN", 30),
)

SOVEREIGN_BRAKE_DOLLARS = -500.0
VELOCITY_FUSE_PCT = 0.01

TWO_BAR_LOCK_FAVORABLE_COUNT = 2
LEASH_EMA_PERIOD = 9
LEASH_EMA_TIMEFRAME_MIN = 5

DAILY_CIRCUIT_BREAKER_DOLLARS = -1500.0

# v5.27.0 \u2014 portfolio-scaled stop tiers.
#
# The legacy absolute thresholds (per-trade Sovereign Brake at -$500;
# daily Circuit Breaker at -$1500) were calibrated against the v5.x
# default $100,000 paper book. On smaller portfolios (e.g. a $20k
# personal account or a paper run after drawdown) those absolute caps
# let a single bad trade or three losing trades chew through 5\u201315%%
# of the book before any halt fires.
#
# v5.27.0 introduces a percentage-of-portfolio scaling layer. The
# scaling factors are calibrated to reproduce the legacy absolutes
# at $100k (0.5%% per-trade, 1.5%% daily) and clamp to a hard floor
# of $100 / $300 so the ratchet still has bite at very small books.
# An upper cap preserves the legacy absolutes for larger books \u2014
# never get bigger than $500 / $1500 even if the user funds beyond
# the calibration point.
SOVEREIGN_BRAKE_PORTFOLIO_PCT = 0.005  # 0.5%% of portfolio per-trade
DAILY_CIRCUIT_BREAKER_PORTFOLIO_PCT = 0.015  # 1.5%% of portfolio per-day
SOVEREIGN_BRAKE_FLOOR_DOLLARS = 100.0
SOVEREIGN_BRAKE_CEILING_DOLLARS = 500.0  # match legacy absolute
DAILY_CIRCUIT_BREAKER_FLOOR_DOLLARS = 300.0
DAILY_CIRCUIT_BREAKER_CEILING_DOLLARS = 1500.0  # match legacy absolute


def scaled_sovereign_brake_dollars(portfolio_value: float | None) -> float:
    """v5.27.0 \u2014 per-trade brake scaled to portfolio.

    Returns a NEGATIVE dollar threshold (e.g. -250.0 means a single
    position trips the brake at -$250 unrealized). When ``portfolio``
    is None or non-positive we fall back to the legacy absolute
    ``SOVEREIGN_BRAKE_DOLLARS`` so warm-up paths stay deterministic.
    """
    if portfolio_value is None or portfolio_value <= 0:
        return float(SOVEREIGN_BRAKE_DOLLARS)
    raw = float(portfolio_value) * SOVEREIGN_BRAKE_PORTFOLIO_PCT
    clamped = max(SOVEREIGN_BRAKE_FLOOR_DOLLARS, min(SOVEREIGN_BRAKE_CEILING_DOLLARS, raw))
    return -clamped


def scaled_daily_circuit_breaker_dollars(portfolio_value: float | None) -> float:
    """v5.27.0 \u2014 daily realized-loss halt scaled to portfolio.

    Returns a NEGATIVE dollar threshold. Falls back to the legacy
    ``DAILY_CIRCUIT_BREAKER_DOLLARS`` when ``portfolio`` is None or
    non-positive.
    """
    if portfolio_value is None or portfolio_value <= 0:
        return float(DAILY_CIRCUIT_BREAKER_DOLLARS)
    raw = float(portfolio_value) * DAILY_CIRCUIT_BREAKER_PORTFOLIO_PCT
    clamped = max(
        DAILY_CIRCUIT_BREAKER_FLOOR_DOLLARS,
        min(DAILY_CIRCUIT_BREAKER_CEILING_DOLLARS, raw),
    )
    return -clamped


OR_WINDOW_START_HHMM_ET = "09:30"
# v15.0 SPEC: ORH/ORL fixed at exactly 09:35:59 ET. End-of-OR is the half-open
# minute boundary 09:36, so bars with close < 09:36 belong to the OR (this
# includes the 09:35 candle).
OR_WINDOW_END_HHMM_ET = "09:36"
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
    if (
        qqq_5m_close is None
        or qqq_5m_ema9 is None
        or qqq_current_price is None
        or qqq_avwap_0930 is None
    ):
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


def evaluate_volume_bucket(
    check_result: dict | None,
    now_et: datetime | None = None,
) -> bool:
    """Translate volume baseline check output to a gate-open boolean.

    spec: L-P2-S3 / S-P2-S3 (Tiger Sovereign vAA-1) \u2014 the volume
    gate is **time-conditional**:

    * Before 10:00:00 ET \u2192 auto-pass (gate is TRUE regardless of ratio)
    * At/after 10:00:00 ET \u2192 require ``ratio_to_55bar_avg >= 1.00``

    Two input shapes are supported for backward compatibility:

    * vAA shape (preferred): ``check_result`` carries
      ``{"ratio_to_55bar_avg": float}``. ``now_et`` MUST be provided so
      the time gate can be applied.
    * Legacy shape (v5.10.x): ``check_result`` carries
      ``{"gate": "PASS"|"FAIL"|"COLDSTART", ...}``. ``now_et`` may be
      omitted; result is gate-string lookup with COLDSTART
      pass-through.

    Runtime override (v5.13.1): when
    ``engine.feature_flags.VOLUME_GATE_ENABLED`` is False (production
    default as of v5.13.1) the legacy v5.10.x gate auto-passes
    regardless of input. The vAA-1 time-conditional path is spec-
    required and is NOT subject to the legacy flag \u2014 callers that
    supply ``now_et`` and ``ratio_to_55bar_avg`` always get the spec
    behaviour. The 2-consecutive-1m boundary-hold gate
    (L-P2-S4 / S-P2-S4) is unaffected and still fully enforced.
    """
    # vAA-1 time-conditional path \u2014 takes precedence when caller
    # supplies a wall-clock and a ratio_to_55bar_avg field. This path
    # is spec-mandated by L-P2-S3 / S-P2-S3 and is NOT gated by the
    # legacy VOLUME_GATE_ENABLED flag, which existed only to bypass
    # the v5.10.x string-gate lookup.
    if now_et is not None and check_result is not None and ("ratio_to_55bar_avg" in check_result):
        # spec L-P2-S3 / S-P2-S3: pre-10:00 ET auto-pass.
        if now_et.time() < dtime(10, 0):
            return True
        ratio = check_result.get("ratio_to_55bar_avg")
        if ratio is None:
            # cold-start: insufficient archive history \u2192 pass-through.
            return True
        try:
            return float(ratio) >= 1.0
        except (TypeError, ValueError):
            return False

    # Legacy v5.10.x path \u2014 string gate lookup, subject to the
    # runtime flag.
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
    last_n_1m_closes: list[float] | None = None,
    required_closes: int = BOUNDARY_HOLD_REQUIRED_CLOSES,
    *,
    prev_1m_close: float | None = None,
    curr_1m_close: float | None = None,
) -> dict:
    """Two consecutive closed 1m candles strictly outside the 5m OR.

    spec: L-P2-S4 / S-P2-S4 (Tiger Sovereign vAA-1) \u2014 the breakout
    permit fires only on the close of the SECOND qualifying 1m bar.
    A close at the boundary breaks the hold (strict ``>`` for LONG,
    strict ``<`` for SHORT).

    Two input shapes are supported:

    * vAA shape (preferred): ``prev_1m_close`` + ``curr_1m_close``
      kwargs name the last two closed 1m candles directly.
    * Legacy shape (v5.10.x): ``last_n_1m_closes`` newest-last list
      \u2014 the function inspects the most recent ``required_closes``
      entries. Used by ``v5_10_1_integration.evaluate_boundary_hold_gate``.
    """
    if side not in (SIDE_LONG, SIDE_SHORT):
        return {"hold": False, "reason": f"bad_side:{side}", "consecutive_outside": 0}
    if or_high is None or or_low is None:
        return {"hold": False, "reason": "or_not_set", "consecutive_outside": 0}

    # vAA shape: prev/curr kwargs take precedence when supplied.
    if prev_1m_close is not None or curr_1m_close is not None:
        if prev_1m_close is None or curr_1m_close is None:
            return {"hold": False, "reason": "insufficient_closes", "consecutive_outside": 0}
        if side == SIDE_LONG:
            prev_outside = prev_1m_close > or_high
            curr_outside = curr_1m_close > or_high
        else:
            prev_outside = prev_1m_close < or_low
            curr_outside = curr_1m_close < or_low
        if prev_outside and curr_outside:
            return {"hold": True, "reason": "satisfied", "consecutive_outside": 2}
        cnt = 1 if curr_outside else 0
        return {"hold": False, "reason": "not_satisfied", "consecutive_outside": cnt}

    # Legacy shape: rolling window list.
    if not last_n_1m_closes or len(last_n_1m_closes) < required_closes:
        return {"hold": False, "reason": "insufficient_closes", "consecutive_outside": 0}
    closes = list(last_n_1m_closes)[-required_closes:]
    if side == SIDE_LONG:
        outside = [c is not None and c > or_high for c in closes]
    else:
        outside = [c is not None and c < or_low for c in closes]
    if all(outside):
        return {"hold": True, "reason": "satisfied", "consecutive_outside": required_closes}
    # report best-effort consecutive count for diagnostics
    cnt = 0
    for x in reversed(outside):
        if x:
            cnt += 1
        else:
            break
    return {"hold": False, "reason": "not_satisfied", "consecutive_outside": cnt}


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
    if (
        or_high is None
        or or_low is None
        or current_1m_open is None
        or current_1m_close is None
        or current_1m_low is None
        or current_1m_high is None
        or prior_1m_low is None
        or prior_1m_high is None
    ):
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
        "entry_1_hwm": None,  # session-extreme at Entry 1 time (HWM for LONG, LWM for SHORT)
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


# =====================================================================
# v5.15.1 vAA-1 \u2014 Phase 3 momentum-sensitive sizing (evaluate_strike_sizing)
# =====================================================================
#
# Spec rules:
#   L-P3-AUTH      master anchor: side-correct 5m DMI must exceed 25
#   L-P3-FULL      1m DI > 30 \u2192 FULL Strike (100% of intended)
#   L-P3-SCALED-A  25 <= 1m DI <= 30 AND held=0 \u2192 SCALED_A (50%)
#   L-P3-SCALED-B  add-on (held>0): 1m DI > 30 AND fresh extreme AND
#                  Alarm E PRE = False \u2192 SCALED_B (50% on top of held)
#   S-P3-* mirrors apply for SHORT \u2014 caller passes side-correct DI
#                  values (DI- for SHORT) so the function is side-symmetric.
#
# This is a pure decision function: no I/O, no state mutation. The
# caller is responsible for:
#   - mapping side LONG/SHORT to DI+/DI-
#   - feeding ``is_fresh_extreme`` (NHOD for LONG, NLOD for SHORT)
#   - consulting check_alarm_e_pre to populate ``alarm_e_blocked``
#   - applying ``intended_shares`` floor / ceiling externally

P3_AUTH_DI_THRESHOLD = 25.0  # 5m DMI master anchor (strict >)
P3_FULL_DI_THRESHOLD = 30.0  # 1m DI for FULL / add-on (strict >)
P3_SCALED_A_DI_LO = 22.0  # v6.8.0 C3: aligned with Entry-1 gate (was 25.0)
P3_SCALED_A_DI_HI = 30.0  # 1m DI upper bound for SCALED_A (inclusive)

SIZE_LABEL_FULL = "FULL"
SIZE_LABEL_SCALED_A = "SCALED_A"
SIZE_LABEL_SCALED_B = "SCALED_B"
SIZE_LABEL_WAIT = "WAIT"


class StrikeSizingDecision:
    """Decision record returned by ``evaluate_strike_sizing``.

    Attributes
    ----------
    size_label : str
        One of ``FULL``, ``SCALED_A``, ``SCALED_B``, ``WAIT``.
    shares_to_buy : int
        Number of shares the caller should send on the LIMIT order.
        Zero when ``size_label == WAIT``.
    reason : str
        Short human-readable rationale string for log lines.
    """

    __slots__ = ("size_label", "shares_to_buy", "reason")

    def __init__(self, size_label: str, shares_to_buy: int, reason: str = "") -> None:
        self.size_label = size_label
        self.shares_to_buy = int(shares_to_buy)
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover \u2014 debug aid only
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
    """Pure decision: how big a Strike entry should fire (or whether to wait).

    Spec: vAA-1 L-P3-AUTH / FULL / SCALED-A / SCALED-B and S-P3 mirrors.

    Inputs are side-correct DMI values: for LONG pass DI+, for SHORT
    pass DI- (caller maps the polarity). ``intended_shares`` is the
    full-fill share count; SCALED tiers return ``intended_shares // 2``.

    The function never raises on bad inputs \u2014 missing DI values
    (None) deterministically degrade to WAIT.
    """
    side_u = (side or "").strip().upper()
    if side_u not in (SIDE_LONG, SIDE_SHORT):
        return StrikeSizingDecision(SIZE_LABEL_WAIT, 0, f"unknown side {side!r}")

    intended = max(0, int(intended_shares))
    if intended <= 0:
        return StrikeSizingDecision(SIZE_LABEL_WAIT, 0, "intended_shares <= 0")

    # L-P3-AUTH master anchor: side-correct 5m DMI must STRICTLY exceed
    # 25. Equality fails (test_l_p3_master_anchor_5m_di_must_exceed_25
    # passes di_5m=25.0 and expects WAIT).
    if di_5m is None or float(di_5m) <= P3_AUTH_DI_THRESHOLD:
        return StrikeSizingDecision(
            SIZE_LABEL_WAIT, 0, f"5m DI {di_5m} <= {P3_AUTH_DI_THRESHOLD} (anchor fail)"
        )

    if di_1m is None:
        return StrikeSizingDecision(SIZE_LABEL_WAIT, 0, "1m DI is None")
    di1 = float(di_1m)

    held = max(0, int(held_shares_this_strike))

    # Add-on (SCALED-B) decision applies when the trade already holds
    # shares from a prior tier within this Strike. Spec L-P3-SCALED-B:
    # add-on requires 1m DI > 30 AND fresh extreme AND Alarm E PRE = False.
    if held > 0:
        if alarm_e_blocked:
            return StrikeSizingDecision(SIZE_LABEL_WAIT, 0, "add-on blocked by Alarm E PRE")
        if not is_fresh_extreme:
            return StrikeSizingDecision(
                SIZE_LABEL_WAIT, 0, "add-on requires fresh extreme (NHOD/NLOD)"
            )
        if di1 > P3_FULL_DI_THRESHOLD:
            return StrikeSizingDecision(
                SIZE_LABEL_SCALED_B,
                intended // 2,
                f"add-on SCALED_B 1m DI {di1:.2f} > {P3_FULL_DI_THRESHOLD}",
            )
        return StrikeSizingDecision(
            SIZE_LABEL_WAIT, 0, f"add-on requires 1m DI > {P3_FULL_DI_THRESHOLD}"
        )

    # First fill of this Strike (held == 0). Alarm E PRE never blocks
    # the FIRST entry per spec (memory has no peak yet by definition).
    if di1 > P3_FULL_DI_THRESHOLD:
        return StrikeSizingDecision(
            SIZE_LABEL_FULL,
            intended,
            f"FULL 1m DI {di1:.2f} > {P3_FULL_DI_THRESHOLD}",
        )
    if P3_SCALED_A_DI_LO <= di1 <= P3_SCALED_A_DI_HI:
        return StrikeSizingDecision(
            SIZE_LABEL_SCALED_A,
            intended // 2,
            f"SCALED_A 1m DI {di1:.2f} in [{P3_SCALED_A_DI_LO},{P3_SCALED_A_DI_HI}]",
        )
    return StrikeSizingDecision(
        SIZE_LABEL_WAIT,
        0,
        f"1m DI {di1:.2f} below {P3_SCALED_A_DI_LO} (no tier match)",
    )


# ---------------------------------------------------------------------------
# v6.11.0 -- C25 SPY Regime-B Short Amplification env-var contract.
# ---------------------------------------------------------------------------
V611_REGIME_B_SHORT_SCALE_MULT = float(_os.getenv("V611_REGIME_B_SHORT_SCALE_MULT", "1.5"))
V611_REGIME_B_SHORT_ARM_HHMM_ET = _os.getenv("V611_REGIME_B_SHORT_ARM_HHMM_ET", "10:00")
V611_REGIME_B_SHORT_DISARM_HHMM_ET = _os.getenv("V611_REGIME_B_SHORT_DISARM_HHMM_ET", "11:00")
V611_REGIME_B_LOWER_PCT = float(_os.getenv("V611_REGIME_B_LOWER_PCT", "-0.50"))
V611_REGIME_B_UPPER_PCT = float(_os.getenv("V611_REGIME_B_UPPER_PCT", "-0.15"))
V611_REGIME_B_ENABLED = _os.getenv("V611_REGIME_B_ENABLED", "1") == "1"
