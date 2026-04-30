"""TradeGenius v5 — Tiger/Buffalo two-stage state machine.

Canonical spec: STRATEGY.md (in repo root). Every decision in this
module cites a rule ID from that spec. When the strategy changes,
update STRATEGY.md first, then patch this module.

States (spec D):
    IDLE             - waiting for permission gates (L-P1 / S-P1)
    ARMED            - gates pass; waiting for DI 25 confirmation
    STAGE_1          - 50% on after L-P2/S-P2; waiting for 30 confirm
    STAGE_2          - full size; stop = original_entry_price
    TRAILING         - 5m structural ratchet active (HL up / LH down)
    EXITED           - flat after L-P4 / S-P4 exit; awaiting reclaim
    RE_HUNT_PENDING  - alias of EXITED awaiting reclamation (L/S-P5-R1)
    LOCKED_FOR_DAY   - second exit OR loss-limit OR shield OR EOD

This module is intentionally self-contained: pure helpers operate on
plain dicts, so they can be exercised in smoke tests without spinning
up the full bot. The integration glue lives in trade_genius.py.

Direction mutual exclusion (C-R1) is enforced via active_direction.
"""

from __future__ import annotations

from typing import Optional


STATE_IDLE = "IDLE"
STATE_ARMED = "ARMED"
STATE_STAGE_1 = "STAGE_1"
STATE_STAGE_2 = "STAGE_2"
STATE_TRAILING = "TRAILING"
STATE_EXITED = "EXITED"
STATE_RE_HUNT_PENDING = "RE_HUNT_PENDING"
STATE_LOCKED = "LOCKED_FOR_DAY"

ALL_STATES = (
    STATE_IDLE,
    STATE_ARMED,
    STATE_STAGE_1,
    STATE_STAGE_2,
    STATE_TRAILING,
    STATE_EXITED,
    STATE_RE_HUNT_PENDING,
    STATE_LOCKED,
)

DIR_LONG = "long"
DIR_SHORT = "short"

# C-R2: ADX/DMI period.
# Gene's spec: 'DI+ (15 period, 5m)'. v4 trade_genius.DI_PERIOD = 15
# has been canonical since pre-v5; we match it here so v5 signals
# agree with what the v4 helpers / dashboard / executor compute.
DMI_PERIOD = 15

# Stage-1 / Stage-2 DI thresholds (L-P2-R1, L-P3-R1, S-P2-R1, S-P3-R1).
STAGE1_DI_THRESHOLD = 25.0
STAGE2_DI_THRESHOLD = 30.0
HARD_EXIT_DI_THRESHOLD = 25.0  # L-P4-R3, S-P4-R3


def new_track(direction: str) -> dict:
    """Fresh per-ticker per-direction state record.

    Schema is intentionally additive over the v4 paper_state.json. A
    paper_state file written by v4 (no v5 fields) loads as IDLE via
    `load_track` below.
    """
    if direction not in (DIR_LONG, DIR_SHORT):
        raise ValueError(f"unknown direction {direction!r}")
    return {
        "direction": direction,
        "state": STATE_IDLE,
        "original_entry_price": None,
        "current_stop": None,
        "stage1_confirms": 0,
        "stage2_confirms": 0,
        "re_hunt_used": False,
        # Most recent post-entry 5m HL/LH for ratchet (L-P4-R1 / S-P4-R1).
        "last_pivot": None,
        # 5m highs/lows captured post-entry; used by ratchet.
        "post_entry_5m_lows": [],
        "post_entry_5m_highs": [],
    }


def load_track(raw: Optional[dict], direction: str) -> dict:
    """Hydrate a track from a possibly-empty/v4 paper_state record.

    Backward-compat: if raw is missing or lacks the v5 'state' field,
    return a fresh IDLE track. v4 paper_state files migrate transparently.
    """
    if not raw or "state" not in raw or raw.get("state") not in ALL_STATES:
        return new_track(direction)
    track = new_track(direction)
    for k in track:
        if k in raw:
            track[k] = raw[k]
    return track


# v5.9.0 \u2014 QQQ Regime Shield. G1 swaps from Index AVWAP penny-switch
# to a structural 5m EMA(3) vs EMA(9) cross. G3/G4 untouched.
#   L-P1: G1 = QQQ.5m_3EMA > QQQ.5m_9EMA
#         G3 = Ticker.Last > Ticker.Opening_AVWAP
#         G4 = Ticker.Last > Ticker.OR_High
#   S-P1: G1 = QQQ.5m_3EMA < QQQ.5m_9EMA
#         G3 = Ticker.Last < Ticker.Opening_AVWAP
#         G4 = Ticker.Last < Ticker.OR_Low
# Strict comparators: equality FAILs. Either EMA None (warmup) -> False.


def gate_g1_long(qqq_5m_3ema, qqq_5m_9ema) -> bool:
    """v5.9.0 L-P1-G1: QQQ 5m EMA3 > EMA9. Equality and None FAIL."""
    if qqq_5m_3ema is None or qqq_5m_9ema is None:
        return False
    return qqq_5m_3ema > qqq_5m_9ema


def gate_g1_short(qqq_5m_3ema, qqq_5m_9ema) -> bool:
    """v5.9.0 S-P1-G1: QQQ 5m EMA3 < EMA9. Equality and None FAIL."""
    if qqq_5m_3ema is None or qqq_5m_9ema is None:
        return False
    return qqq_5m_3ema < qqq_5m_9ema


def gate_g3_long(ticker_last, ticker_opening_avwap) -> bool:
    """L-P1-G3: Ticker.Last > Ticker.Opening_AVWAP. AVWAP None -> False."""
    if ticker_last is None or ticker_opening_avwap is None:
        return False
    return ticker_last > ticker_opening_avwap


def gate_g3_short(ticker_last, ticker_opening_avwap) -> bool:
    """S-P1-G3: Ticker.Last < Ticker.Opening_AVWAP. AVWAP None -> False."""
    if ticker_last is None or ticker_opening_avwap is None:
        return False
    return ticker_last < ticker_opening_avwap


def gate_g4_long(ticker_last, ticker_or_high) -> bool:
    """L-P1-G4: Ticker.Last > Ticker.OR_High. Pre-9:35 (OR_High None) -> False."""
    if ticker_last is None or ticker_or_high is None:
        return False
    return ticker_last > ticker_or_high


def gate_g4_short(ticker_last, ticker_or_low) -> bool:
    """S-P1-G4: Ticker.Last < Ticker.OR_Low. Pre-9:35 (OR_Low None) -> False."""
    if ticker_last is None or ticker_or_low is None:
        return False
    return ticker_last < ticker_or_low


def gates_pass_long(
    qqq_5m_3ema, qqq_5m_9ema, ticker_last, ticker_opening_avwap, ticker_or_high
) -> bool:
    """L-P1 \u2014 Long Permission Gates (G1, G3, G4).

    v5.9.0: G1 is now QQQ 5m EMA3 > EMA9 (structural compass). G3/G4
    unchanged. Any None input fails closed; equality fails strict.
    """
    return (
        gate_g1_long(qqq_5m_3ema, qqq_5m_9ema)
        and gate_g3_long(ticker_last, ticker_opening_avwap)
        and gate_g4_long(ticker_last, ticker_or_high)
    )


def gates_pass_short(
    qqq_5m_3ema, qqq_5m_9ema, ticker_last, ticker_opening_avwap, ticker_or_low
) -> bool:
    """S-P1 \u2014 Short Permission Gates (G1, G3, G4).

    v5.9.0: G1 is now QQQ 5m EMA3 < EMA9. Mirror of long path.
    """
    return (
        gate_g1_short(qqq_5m_3ema, qqq_5m_9ema)
        and gate_g3_short(ticker_last, ticker_opening_avwap)
        and gate_g4_short(ticker_last, ticker_or_low)
    )


def stage1_signal_long(di_plus_1m, di_plus_5m) -> bool:
    """L-P2-R1: DI+(1m) > 25 AND DI+(5m) > 25."""
    if di_plus_1m is None or di_plus_5m is None:
        return False
    return di_plus_1m > STAGE1_DI_THRESHOLD and di_plus_5m > STAGE1_DI_THRESHOLD


def stage1_signal_short(di_minus_1m, di_minus_5m) -> bool:
    """S-P2-R1: DI-(1m) > 25 AND DI-(5m) > 25."""
    if di_minus_1m is None or di_minus_5m is None:
        return False
    return di_minus_1m > STAGE1_DI_THRESHOLD and di_minus_5m > STAGE1_DI_THRESHOLD


def stage2_signal_long(di_plus_1m) -> bool:
    """L-P3-R1: DI+(1m) > 30."""
    return di_plus_1m is not None and di_plus_1m > STAGE2_DI_THRESHOLD


def stage2_signal_short(di_minus_1m) -> bool:
    """S-P3-R1: DI-(1m) > 30."""
    return di_minus_1m is not None and di_minus_1m > STAGE2_DI_THRESHOLD


def winning_rule_long(ticker_last, original_entry_price) -> bool:
    """L-P3-R3: Stage 2 only fires if stage-1 fills are in profit."""
    if original_entry_price is None or ticker_last is None:
        return False
    return ticker_last > original_entry_price


def winning_rule_short(ticker_last, original_entry_price) -> bool:
    """S-P3-R3: Stage 2 only fires if the short is in profit (price fell)."""
    if original_entry_price is None or ticker_last is None:
        return False
    return ticker_last < original_entry_price


def ratchet_long_higher_low(prev_5m_low, this_5m_low, current_stop) -> Optional[float]:
    """L-P4-R1, L-P4-R2 — compute new stop given a freshly closed 5m candle.

    Definition of HL: this_5m_low > prev_5m_low (the low rose).
    Ratchet up only: stop only moves if the new HL is strictly above
    the current stop. Returns the new stop (could be unchanged).
    """
    if current_stop is None:
        return None
    if prev_5m_low is None or this_5m_low is None:
        return current_stop
    if this_5m_low <= prev_5m_low:
        return current_stop  # not a Higher Low
    new_hl = this_5m_low
    if new_hl > current_stop:
        return new_hl
    return current_stop


def ratchet_short_lower_high(prev_5m_high, this_5m_high, current_stop) -> Optional[float]:
    """S-P4-R1, S-P4-R2 — symmetric to long ratchet.

    Definition of LH: this_5m_high < prev_5m_high (high fell).
    Ratchet down only.
    """
    if current_stop is None:
        return None
    if prev_5m_high is None or this_5m_high is None:
        return current_stop
    if this_5m_high >= prev_5m_high:
        return current_stop  # not a Lower High
    new_lh = this_5m_high
    if new_lh < current_stop:
        return new_lh
    return current_stop


def structural_stop_hit_long(ticker_last, current_stop) -> bool:
    """L-P4-R3 (a): ticker.last < current_stop."""
    if ticker_last is None or current_stop is None:
        return False
    return ticker_last < current_stop


def structural_stop_hit_short(ticker_last, current_stop) -> bool:
    """S-P4-R4: ticker.last > current_stop."""
    if ticker_last is None or current_stop is None:
        return False
    return ticker_last > current_stop


def reclamation_long(ticker_last, original_entry_price) -> bool:
    """L-P5-R1: dormant until ticker.last > original_entry_price."""
    if ticker_last is None or original_entry_price is None:
        return False
    return ticker_last > original_entry_price


def reclamation_short(ticker_last, original_entry_price) -> bool:
    """S-P5-R1: dormant until ticker.last < original_entry_price."""
    if ticker_last is None or original_entry_price is None:
        return False
    return ticker_last < original_entry_price


# ------------------------------------------------------------
# Confirmation counters (L-P2-R2, L-P3-R2, S-P2-R2, S-P3-R2)
# ------------------------------------------------------------
# C-R3: closed-candle confirmation only — entries fire on the close
# of the second confirming 1m candle. Callers must drive these on
# every CLOSED 1m candle, not on every tick.
def tick_stage1_confirm(track: dict, signal_now: bool) -> bool:
    """Update stage-1 confirmation counter. Return True iff entry fires.

    Two consecutive confirmed closes => fire once and reset.
    """
    if not signal_now:
        track["stage1_confirms"] = 0
        return False
    track["stage1_confirms"] += 1
    if track["stage1_confirms"] >= 2:
        track["stage1_confirms"] = 0
        return True
    return False


def tick_stage2_confirm(track: dict, signal_now: bool) -> bool:
    """Update stage-2 confirmation counter. Return True iff add fires."""
    if not signal_now:
        track["stage2_confirms"] = 0
        return False
    track["stage2_confirms"] += 1
    if track["stage2_confirms"] >= 2:
        track["stage2_confirms"] = 0
        return True
    return False


# ------------------------------------------------------------
# State transitions
# ------------------------------------------------------------
def transition_to_stage1(track: dict, fill_price: float, initial_stop: float) -> None:
    """L-P2-R3..R5 / S-P2-R3..R5.

    50% of unit on, hard stop = prior 5m candle low (long) or high (short).
    record original_entry_price.
    """
    track["state"] = STATE_STAGE_1
    track["original_entry_price"] = float(fill_price)
    track["current_stop"] = float(initial_stop)
    track["stage1_confirms"] = 0
    track["stage2_confirms"] = 0
    track["last_pivot"] = None
    track["post_entry_5m_lows"] = []
    track["post_entry_5m_highs"] = []


def transition_to_stage2(track: dict) -> None:
    """L-P3-R4..R5 / S-P3-R4..R5.

    Add remaining 50% (full size). Move stop on 100% to original_entry_price.
    """
    track["state"] = STATE_STAGE_2
    track["current_stop"] = float(track["original_entry_price"])
    track["stage2_confirms"] = 0


def transition_to_trailing(track: dict) -> None:
    """STAGE_2 -> TRAILING (implicit on the next 5m close per spec D)."""
    track["state"] = STATE_TRAILING


def transition_to_exited(track: dict) -> None:
    """L-P4-R4 / S-P4-R5: enter EXITED, awaiting reclamation."""
    track["state"] = STATE_EXITED
    track["current_stop"] = None


def transition_to_locked(track: dict) -> None:
    """LOCKED_FOR_DAY — second exit, daily-loss-limit (C-R4),
    Sovereign Regime Shield (C-R6), or EOD (C-R5).
    """
    track["state"] = STATE_LOCKED
    track["current_stop"] = None


def transition_re_hunt(track: dict) -> bool:
    """L-P5 / S-P5: on reclamation, return ARMED with fresh values.

    If re_hunt_used is already True, force LOCKED instead (L-P5-R3 /
    S-P5-R3). Returns True iff the track is now ARMED for re-hunt.
    """
    if track["re_hunt_used"]:
        transition_to_locked(track)
        return False
    track["re_hunt_used"] = True
    track["state"] = STATE_ARMED
    track["original_entry_price"] = None
    track["current_stop"] = None
    track["stage1_confirms"] = 0
    track["stage2_confirms"] = 0
    track["last_pivot"] = None
    track["post_entry_5m_lows"] = []
    track["post_entry_5m_highs"] = []
    return True


# ------------------------------------------------------------
# Direction-mutex helper (C-R1)
# ------------------------------------------------------------
def can_arm_direction(active_direction: Optional[str], wanted: str) -> bool:
    """C-R1: long and short on the same ticker are mutually exclusive
    within a session. If a direction is already active (anything other
    than IDLE), the other direction is forbidden until EOD.
    """
    if active_direction is None:
        return True
    return active_direction == wanted


# ------------------------------------------------------------
# Re-hunt budget (L-P5-R3 / S-P5-R3)
# ------------------------------------------------------------
def on_post_exit(track: dict) -> None:
    """Decide whether the post-exit track lands in EXITED (one re-hunt
    available) or LOCKED_FOR_DAY (re-hunt already burned).
    """
    if track["re_hunt_used"]:
        transition_to_locked(track)
    else:
        transition_to_exited(track)


# ------------------------------------------------------------
# State-machine version stamp — bumped when wire format changes.
# ------------------------------------------------------------
V5_STATE_VERSION = 1


# ============================================================
# v5.7.1 / v5.9.0 \u2014 Bison & Buffalo exit FSM (Titan-only)
# ============================================================
# Spec: specs/v5_7_1_stop_loss_optimization.md (Phase A/B/C scaffolding)
#       specs/v5_9_0_qqq_regime_shield.md (Phase A re-build: forensic
#       stop + per-trade sovereign brake)
# Scope: Ten Titans only (AAPL, AMZN, AVGO, GOOG, META, MSFT, NFLX,
#                        NVDA, ORCL, TSLA).
# Phases:
#   initial_risk     \u2014 v5.9.0 Recursive Forensic Stop (Maffei 1-2-3)
#                       on every 1m close while gate triggers; per-trade
#                       Sovereign Brake on every tick.
#   house_money      \u2014 stop ratcheted to entry price (BE)
#   sovereign_trail  \u2014 5-min close vs 9-EMA(5m); BE inactive when
#                       EMA tightens further
# Global Velocity Fuse + per-trade brake run every tick regardless of phase.
# Non-Titan tickers continue to use the legacy DI/structural exits above.
PHASE_INITIAL_RISK = "initial_risk"
PHASE_HOUSE_MONEY = "house_money"
PHASE_SOVEREIGN_TRAIL = "sovereign_trail"
ALL_PHASES = (PHASE_INITIAL_RISK, PHASE_HOUSE_MONEY, PHASE_SOVEREIGN_TRAIL)

# v5.9.0: hard_stop_2c retired. forensic_stop and per_trade_brake added.
EXIT_REASON_FORENSIC_STOP = "forensic_stop"
EXIT_REASON_PER_TRADE_BRAKE = "per_trade_brake"
EXIT_REASON_BE_STOP = "be_stop"
EXIT_REASON_EMA_TRAIL = "ema_trail"
EXIT_REASON_VELOCITY_FUSE = "velocity_fuse"

# v5.9.0: per-trade Sovereign Brake threshold. UNREALIZED P&L on a single
# open Titan trade reaching this value triggers an immediate market exit.
# Distinct from the portfolio-level realized -$500 brake (unchanged).
PER_TRADE_BRAKE_USD = -500.0

# 9-period EMA on 5-min candles. EMA seeds at the close of the 9th
# 5-min bar since 9:30 ET = 10:15 ET.
EMA_5M_PERIOD = 9
EMA_5M_SEED_BARS = 9
EMA_5M_SEED_ET_HHMM = "10:15"  # informational; callers compute time

# Velocity Fuse: strict >1.0% adverse move from current 1-min open.
VELOCITY_FUSE_PCT_DEFAULT = 0.01


def init_titan_exit_state(track: dict, entry_price: float, qty: int = 0) -> None:
    """v5.7.1 / v5.9.0 \u2014 Initialize Bison/Buffalo per-position exit state.

    Adds to `track`:
      phase                       : str  (initial_risk)
      forensic_consecutive_count  : int  (0; advanced by Forensic Stop)
      green_5m_count              : int  (0; LONG only)
      red_5m_count                : int  (0; SHORT only)
      ema_5m                      : float|None (None until 10:15 ET)
      ema_5m_bars_seen            : int  (closed 5m bars since 9:30)
      current_stop                : float (entry price)
      entry_price                 : float (cached for per-trade brake)
      qty                         : int   (cached for per-trade brake)
      prior_1m_low                : float|None  (Forensic Stop carryover)
      prior_1m_high               : float|None  (Forensic Stop carryover)
    """
    track["phase"] = PHASE_INITIAL_RISK
    track["forensic_consecutive_count"] = 0
    track["green_5m_count"] = 0
    track["red_5m_count"] = 0
    track["ema_5m"] = None
    track["ema_5m_bars_seen"] = 0
    track["current_stop"] = float(entry_price)
    track["entry_price"] = float(entry_price)
    track["qty"] = int(qty)
    track["prior_1m_low"] = None
    track["prior_1m_high"] = None


# ============================================================
# v5.9.0 \u2014 Recursive Forensic Stop (Phase A "Maffei 1-2-3")
# ============================================================
# Replaces the v5.7.1 hard_stop_2c counter. While Phase A is active,
# every 1m candle that closes outside the OR boundary runs a structural
# audit against the prior 1m candle. Wicks / consolidation extend the
# field time; structural expansion exits.


def forensic_audit_long(prior_low, current_low) -> bool:
    """LONG audit on a Phase A candle that closed below OR_High.

    Returns True iff EXIT (current_low < prior_low \u2014 structural rot).
    Equality and higher-low STAY (returns False). None inputs STAY.
    """
    if prior_low is None or current_low is None:
        return False
    return float(current_low) < float(prior_low)


def forensic_audit_short(prior_high, current_high) -> bool:
    """SHORT audit on a Phase A candle that closed above OR_Low.

    Returns True iff EXIT (current_high > prior_high). Mirror of LONG.
    """
    if prior_high is None or current_high is None:
        return False
    return float(current_high) > float(prior_high)


def update_forensic_stop_long(
    track: dict,
    *,
    candle_1m_close: float,
    candle_1m_low: float,
    prior_candle_1m_low,
    or_high: float,
) -> bool:
    """LONG Phase A close-of-candle update.

    Gate: candle closed below OR_High. If gate fires AND audit returns
    EXIT, set track['exit_reason'] = forensic_stop and return True.
    Otherwise advance/reset the consecutive_count and return False.
    Caller is responsible for not invoking this outside Phase A.
    """
    if candle_1m_close is None or candle_1m_low is None or or_high is None:
        return False
    if float(candle_1m_close) >= float(or_high):
        # Inside OR \u2014 reset the rot streak.
        track["forensic_consecutive_count"] = 0
        return False
    # Closed outside OR \u2014 run audit.
    track["forensic_consecutive_count"] = int(track.get("forensic_consecutive_count", 0)) + 1
    if forensic_audit_long(prior_candle_1m_low, candle_1m_low):
        track["exit_reason"] = EXIT_REASON_FORENSIC_STOP
        return True
    return False


def update_forensic_stop_short(
    track: dict,
    *,
    candle_1m_close: float,
    candle_1m_high: float,
    prior_candle_1m_high,
    or_low: float,
) -> bool:
    """SHORT Phase A close-of-candle update. Mirror of LONG."""
    if candle_1m_close is None or candle_1m_high is None or or_low is None:
        return False
    if float(candle_1m_close) <= float(or_low):
        track["forensic_consecutive_count"] = 0
        return False
    track["forensic_consecutive_count"] = int(track.get("forensic_consecutive_count", 0)) + 1
    if forensic_audit_short(prior_candle_1m_high, candle_1m_high):
        track["exit_reason"] = EXIT_REASON_FORENSIC_STOP
        return True
    return False


def per_trade_sovereign_brake(
    track: dict, current_price, threshold: float = PER_TRADE_BRAKE_USD
) -> bool:
    """v5.9.0 \u2014 Per-trade Sovereign Brake on UNREALIZED P&L.

    UnrealizedPnL = (current - entry) * qty   (LONG)
                  = (entry   - current) * qty (SHORT)
    Returns True iff unrealized <= threshold ($-500.0 by default).
    Threshold is comparison-as-loss; -500.0 means -$500 fires, -$499 does not.
    None inputs / missing entry / qty<=0 fail-closed (return False).
    """
    if current_price is None:
        return False
    entry_price = track.get("entry_price")
    qty = track.get("qty")
    if entry_price is None or qty is None or int(qty) <= 0:
        return False
    direction = track.get("direction")
    if direction == DIR_SHORT:
        unrealized = (float(entry_price) - float(current_price)) * int(qty)
    else:
        unrealized = (float(current_price) - float(entry_price)) * int(qty)
    return unrealized <= float(threshold)


def update_green_5m_count_long(track: dict, candle_open: float, candle_close: float) -> bool:
    """LONG: increment green-5m counter on a CLOSED 5-min bar with
    close > open. Returns True iff a BE move should fire on THIS
    bar (i.e. count just hit 2 and we're still in initial_risk).
    """
    if candle_open is None or candle_close is None:
        return False
    if candle_close > candle_open:
        track["green_5m_count"] = int(track.get("green_5m_count", 0)) + 1
    return track.get("phase") == PHASE_INITIAL_RISK and track.get("green_5m_count", 0) >= 2


def update_red_5m_count_short(track: dict, candle_open: float, candle_close: float) -> bool:
    """SHORT: increment red-5m counter on a CLOSED 5-min bar with
    close < open. Returns True iff a BE move should fire on THIS
    bar (count just hit 2 and we're still in initial_risk).
    """
    if candle_open is None or candle_close is None:
        return False
    if candle_close < candle_open:
        track["red_5m_count"] = int(track.get("red_5m_count", 0)) + 1
    return track.get("phase") == PHASE_INITIAL_RISK and track.get("red_5m_count", 0) >= 2


def transition_to_house_money(track: dict, entry_price: float) -> None:
    """v5.7.1 \u2014 Ratchet stop to entry price; hard-stop becomes inactive."""
    track["phase"] = PHASE_HOUSE_MONEY
    track["current_stop"] = float(entry_price)


def transition_to_sovereign_trail(track: dict) -> None:
    """v5.7.1 \u2014 Promote phase once 9-EMA seeded (10:15 ET)."""
    track["phase"] = PHASE_SOVEREIGN_TRAIL


def update_ema_5m(
    track: dict, candle_close: float, period: int = EMA_5M_PERIOD, seed_bars: int = EMA_5M_SEED_BARS
) -> Optional[float]:
    """Roll the 9-period EMA on closed 5-min closes.

    Standard formula: EMA_t = (close - EMA_{t-1}) * (2/(period+1))
                              + EMA_{t-1}
    Returns the new EMA value (or None until `seed_bars` closed bars
    have accumulated). Callers MUST drive this only on closed 5-min
    candles; once seeded, the value persists in `track["ema_5m"]`.
    """
    if candle_close is None:
        return track.get("ema_5m")
    track["ema_5m_bars_seen"] = int(track.get("ema_5m_bars_seen", 0)) + 1
    seen = track["ema_5m_bars_seen"]
    prev_ema = track.get("ema_5m")
    if prev_ema is None:
        # Seed-on-the-Nth-bar: simple average of the first `seed_bars`
        # closes. We accumulate the running sum in `_ema_seed_sum`.
        track["_ema_seed_sum"] = float(track.get("_ema_seed_sum", 0.0)) + float(candle_close)
        if seen >= seed_bars:
            ema = track["_ema_seed_sum"] / float(seed_bars)
            track["ema_5m"] = float(ema)
            try:
                del track["_ema_seed_sum"]
            except KeyError:
                pass
            return float(ema)
        return None
    # Already seeded \u2014 standard EMA recurrence.
    k = 2.0 / (period + 1.0)
    ema = (float(candle_close) - float(prev_ema)) * k + float(prev_ema)
    track["ema_5m"] = float(ema)
    return float(ema)


def ema_trail_exit_long(track: dict, candle_close: float) -> bool:
    """LONG sovereign trail: 5-min CLOSE strictly below the EMA fires."""
    ema = track.get("ema_5m")
    if ema is None or candle_close is None:
        return False
    return float(candle_close) < float(ema)


def ema_trail_exit_short(track: dict, candle_close: float) -> bool:
    """SHORT sovereign trail: 5-min CLOSE strictly above the EMA fires."""
    ema = track.get("ema_5m")
    if ema is None or candle_close is None:
        return False
    return float(candle_close) > float(ema)


def velocity_fuse_long(
    current_price: float, candle_1m_open: float, pct: float = VELOCITY_FUSE_PCT_DEFAULT
) -> bool:
    """LONG velocity fuse: current_price < open * (1 - pct), strict.

    Strict comparator: an exact 1.00% drop returns False; 1.001%
    returns True. Ignores phase \u2014 caller invokes on every tick.
    """
    if current_price is None or candle_1m_open is None:
        return False
    return float(current_price) < float(candle_1m_open) * (1.0 - float(pct))


def velocity_fuse_short(
    current_price: float, candle_1m_open: float, pct: float = VELOCITY_FUSE_PCT_DEFAULT
) -> bool:
    """SHORT velocity fuse: current_price > open * (1 + pct), strict."""
    if current_price is None or candle_1m_open is None:
        return False
    return float(current_price) > float(candle_1m_open) * (1.0 + float(pct))


def evaluate_titan_exit(
    track: dict,
    *,
    side: str,
    current_price: Optional[float],
    candle_1m_open: Optional[float],
    velocity_fuse_pct: float = VELOCITY_FUSE_PCT_DEFAULT,
    per_trade_brake_usd: float = PER_TRADE_BRAKE_USD,
) -> Optional[str]:
    """v5.7.1 / v5.9.0 \u2014 Run every-tick Titan exit checks.

    Returns one of the exit_reason values
    (`velocity_fuse`, `per_trade_brake`, `be_stop`) or None.
    Order: Velocity Fuse \u2192 Per-Trade Sovereign Brake \u2192 BE-stop.

    Forensic Stop runs on candle CLOSE (not tick) and is advanced by
    `update_forensic_stop_*`. EMA trail is advanced separately.
    """
    if side == DIR_LONG:
        if velocity_fuse_long(current_price, candle_1m_open, velocity_fuse_pct):
            return EXIT_REASON_VELOCITY_FUSE
        if per_trade_sovereign_brake(track, current_price, per_trade_brake_usd):
            return EXIT_REASON_PER_TRADE_BRAKE
        stop = track.get("current_stop")
        if (
            current_price is not None
            and stop is not None
            and float(current_price) < float(stop)
            and track.get("phase") == PHASE_HOUSE_MONEY
        ):
            return EXIT_REASON_BE_STOP
    elif side == DIR_SHORT:
        if velocity_fuse_short(current_price, candle_1m_open, velocity_fuse_pct):
            return EXIT_REASON_VELOCITY_FUSE
        if per_trade_sovereign_brake(track, current_price, per_trade_brake_usd):
            return EXIT_REASON_PER_TRADE_BRAKE
        stop = track.get("current_stop")
        if (
            current_price is not None
            and stop is not None
            and float(current_price) > float(stop)
            and track.get("phase") == PHASE_HOUSE_MONEY
        ):
            return EXIT_REASON_BE_STOP
    return None
