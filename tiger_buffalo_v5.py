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
    STATE_IDLE, STATE_ARMED, STATE_STAGE_1, STATE_STAGE_2,
    STATE_TRAILING, STATE_EXITED, STATE_RE_HUNT_PENDING, STATE_LOCKED,
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


# v5.6.0 \u2014 Unified AVWAP permission gates. G2 retired.
#   L-P1: G1 = Index.Last > Index.Opening_AVWAP
#         G3 = Ticker.Last > Ticker.Opening_AVWAP
#         G4 = Ticker.Last > Ticker.OR_High
#   S-P1: G1 = Index.Last < Index.Opening_AVWAP
#         G3 = Ticker.Last < Ticker.Opening_AVWAP
#         G4 = Ticker.Last < Ticker.OR_Low
# Index = QQQ only. Strict comparators: equality FAILs.
# Pre-9:35 ET (OR not yet defined) -> G4 returns False. Opening AVWAP None
# (no bars yet) -> G1/G3 return False. Fail-closed everywhere.


def gate_g1_long(qqq_last, qqq_opening_avwap) -> bool:
    """L-P1-G1: QQQ.Last > QQQ.Opening_AVWAP. Strict; AVWAP None -> False."""
    if qqq_last is None or qqq_opening_avwap is None:
        return False
    return qqq_last > qqq_opening_avwap


def gate_g1_short(qqq_last, qqq_opening_avwap) -> bool:
    """S-P1-G1: QQQ.Last < QQQ.Opening_AVWAP. Strict; AVWAP None -> False."""
    if qqq_last is None or qqq_opening_avwap is None:
        return False
    return qqq_last < qqq_opening_avwap


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


def gates_pass_long(qqq_last, qqq_opening_avwap,
                    ticker_last, ticker_opening_avwap, ticker_or_high) -> bool:
    """L-P1 \u2014 Long Permission Gates (G1, G3, G4). G2 retired (v5.6.0).

    All three must be strictly true. AVWAP None or OR_High None fails closed.
    Equality fails (strict >).
    """
    return (
        gate_g1_long(qqq_last, qqq_opening_avwap)
        and gate_g3_long(ticker_last, ticker_opening_avwap)
        and gate_g4_long(ticker_last, ticker_or_high)
    )


def gates_pass_short(qqq_last, qqq_opening_avwap,
                     ticker_last, ticker_opening_avwap, ticker_or_low) -> bool:
    """S-P1 \u2014 Short Permission Gates (G1, G3, G4). G2 retired (v5.6.0).

    Mirror of long gates. AVWAP None or OR_Low None fails closed.
    Equality fails (strict <).
    """
    return (
        gate_g1_short(qqq_last, qqq_opening_avwap)
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


def hard_exit_di_fail(direction: str, di_1m) -> bool:
    """L-P4-R3 (b) / S-P4-R3: DI < 25 on closed 1m candle.

    On the short side this is priority-1 over the structural-stop check
    per S-P4-R3 — caller must enforce that ordering.
    """
    if di_1m is None:
        return False
    return di_1m < HARD_EXIT_DI_THRESHOLD


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


def ratchet_long_higher_low(prev_5m_low, this_5m_low,
                            current_stop) -> Optional[float]:
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


def ratchet_short_lower_high(prev_5m_high, this_5m_high,
                             current_stop) -> Optional[float]:
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
# Combined exit evaluator
# ------------------------------------------------------------
def evaluate_exit(track: dict, ticker_last, di_1m_closed,
                  is_titan: bool = False) -> Optional[str]:
    """Run the priority-ordered exit checks for a STAGE_2/TRAILING track.

    Returns the exit reason ("DI_HARD_EJECT", "STRUCTURAL_STOP") or None.

    Direction-aware ordering:
      - Long (L-P4-R3): structural stop OR DI<25; either fires.
      - Short (S-P4-R3): DI<25 priority-1 (BEFORE structural-stop check),
        S-P4-R4 structural priority-2.

    di_1m_closed should be passed only on a closed 1m candle; pass None
    for intra-candle ticks (per C-R3 the structural stop still evaluates
    on every tick).

    v5.7.1 \u2014 when is_titan=True, the DI<25 hard-eject is bypassed for
    both LONG and SHORT. Bison/Buffalo Titans rely on the new exit FSM
    (hard_stop_2c / be_stop / ema_trail / velocity_fuse). Non-Titan
    tickers preserve the legacy DI exit path unchanged.
    """
    direction = track["direction"]
    state = track["state"]
    if state not in (STATE_STAGE_2, STATE_TRAILING):
        return None
    current_stop = track.get("current_stop")

    if direction == DIR_SHORT:
        # S-P4-R3 priority-1 \u2014 skipped for Titans (v5.7.1).
        if (not is_titan) and di_1m_closed is not None and \
                hard_exit_di_fail(DIR_SHORT, di_1m_closed):
            return "DI_HARD_EJECT"
        # S-P4-R4 priority-2
        if structural_stop_hit_short(ticker_last, current_stop):
            return "STRUCTURAL_STOP"
        return None

    # Long
    # L-P4-R3 (a) structural exit
    if structural_stop_hit_long(ticker_last, current_stop):
        return "STRUCTURAL_STOP"
    # L-P4-R3 (b) DI failure \u2014 skipped for Titans (v5.7.1).
    if (not is_titan) and di_1m_closed is not None and \
            hard_exit_di_fail(DIR_LONG, di_1m_closed):
        return "DI_HARD_EJECT"
    return None


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
# v5.7.1 \u2014 Bison & Buffalo exit FSM (Titan-only)
# ============================================================
# Spec: specs/v5_7_1_stop_loss_optimization.md
# Scope: Ten Titans only (AAPL, AMZN, AVGO, GOOG, META, MSFT, NFLX,
#                        NVDA, ORCL, TSLA).
# Phases:
#   initial_risk     \u2014 hard stop on 2 consec 1-min closes outside OR
#   house_money      \u2014 stop ratcheted to entry price (BE)
#   sovereign_trail  \u2014 5-min close vs 9-EMA(5m); BE inactive when
#                       EMA tightens further
# A global Velocity Fuse runs every tick regardless of phase.
# Non-Titan tickers continue to use the legacy DI/structural exits
# above; v5.7.1 helpers are pure functions and do not mutate the
# legacy track schema unless `init_titan_exit_state` is called.
PHASE_INITIAL_RISK = "initial_risk"
PHASE_HOUSE_MONEY = "house_money"
PHASE_SOVEREIGN_TRAIL = "sovereign_trail"
ALL_PHASES = (PHASE_INITIAL_RISK, PHASE_HOUSE_MONEY, PHASE_SOVEREIGN_TRAIL)

EXIT_REASON_HARD_STOP_2C = "hard_stop_2c"
EXIT_REASON_BE_STOP = "be_stop"
EXIT_REASON_EMA_TRAIL = "ema_trail"
EXIT_REASON_VELOCITY_FUSE = "velocity_fuse"

# 9-period EMA on 5-min candles. EMA seeds at the close of the 9th
# 5-min bar since 9:30 ET = 10:15 ET.
EMA_5M_PERIOD = 9
EMA_5M_SEED_BARS = 9
EMA_5M_SEED_ET_HHMM = "10:15"  # informational; callers compute time

# Velocity Fuse: strict >1.0% adverse move from current 1-min open.
VELOCITY_FUSE_PCT_DEFAULT = 0.01


def init_titan_exit_state(track: dict, entry_price: float) -> None:
    """v5.7.1 \u2014 Initialize Bison/Buffalo per-position exit state.

    Adds to `track`:
      phase                      : str  (initial_risk)
      hard_stop_consec_1m_count  : int  (0)
      green_5m_count             : int  (0; LONG only)
      red_5m_count               : int  (0; SHORT only)
      ema_5m                     : float|None (None until 10:15 ET)
      ema_5m_bars_seen           : int  (count of closed 5m bars
                                          observed since 9:30)
      current_stop               : float (entry-derived; stays at the
                                          OR boundary semantics in
                                          initial_risk)
    Caller assigns `current_stop` to the entry-time OR boundary.
    """
    track["phase"] = PHASE_INITIAL_RISK
    track["hard_stop_consec_1m_count"] = 0
    track["green_5m_count"] = 0
    track["red_5m_count"] = 0
    track["ema_5m"] = None
    track["ema_5m_bars_seen"] = 0
    track["current_stop"] = float(entry_price)


def update_hard_stop_counter_long(track: dict, candle_close: float,
                                  or_high: float) -> bool:
    """LONG hard-stop counter on a CLOSED 1-min candle.

    Returns True iff this close fires the hard stop (counter >= 2).
    Counter resets to 0 only on a 1-min close back inside OR
    (i.e. close >= or_high). Slow grind-down keeps the counter
    incrementing on each consecutive sub-OR close.
    """
    if candle_close is None or or_high is None:
        return False
    if candle_close < or_high:
        track["hard_stop_consec_1m_count"] = int(
            track.get("hard_stop_consec_1m_count", 0)) + 1
    else:
        track["hard_stop_consec_1m_count"] = 0
    return track["hard_stop_consec_1m_count"] >= 2


def update_hard_stop_counter_short(track: dict, candle_close: float,
                                   or_low: float) -> bool:
    """SHORT hard-stop counter on a CLOSED 1-min candle.

    Mirror of LONG: increments on close > or_low, resets on
    close <= or_low.
    """
    if candle_close is None or or_low is None:
        return False
    if candle_close > or_low:
        track["hard_stop_consec_1m_count"] = int(
            track.get("hard_stop_consec_1m_count", 0)) + 1
    else:
        track["hard_stop_consec_1m_count"] = 0
    return track["hard_stop_consec_1m_count"] >= 2


def update_green_5m_count_long(track: dict, candle_open: float,
                               candle_close: float) -> bool:
    """LONG: increment green-5m counter on a CLOSED 5-min bar with
    close > open. Returns True iff a BE move should fire on THIS
    bar (i.e. count just hit 2 and we're still in initial_risk).
    """
    if candle_open is None or candle_close is None:
        return False
    if candle_close > candle_open:
        track["green_5m_count"] = int(track.get("green_5m_count", 0)) + 1
    return (
        track.get("phase") == PHASE_INITIAL_RISK
        and track.get("green_5m_count", 0) >= 2
    )


def update_red_5m_count_short(track: dict, candle_open: float,
                              candle_close: float) -> bool:
    """SHORT: increment red-5m counter on a CLOSED 5-min bar with
    close < open. Returns True iff a BE move should fire on THIS
    bar (count just hit 2 and we're still in initial_risk).
    """
    if candle_open is None or candle_close is None:
        return False
    if candle_close < candle_open:
        track["red_5m_count"] = int(track.get("red_5m_count", 0)) + 1
    return (
        track.get("phase") == PHASE_INITIAL_RISK
        and track.get("red_5m_count", 0) >= 2
    )


def transition_to_house_money(track: dict, entry_price: float) -> None:
    """v5.7.1 \u2014 Ratchet stop to entry price; hard-stop becomes inactive."""
    track["phase"] = PHASE_HOUSE_MONEY
    track["current_stop"] = float(entry_price)


def transition_to_sovereign_trail(track: dict) -> None:
    """v5.7.1 \u2014 Promote phase once 9-EMA seeded (10:15 ET)."""
    track["phase"] = PHASE_SOVEREIGN_TRAIL


def update_ema_5m(track: dict, candle_close: float,
                  period: int = EMA_5M_PERIOD,
                  seed_bars: int = EMA_5M_SEED_BARS) -> Optional[float]:
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
        track["_ema_seed_sum"] = float(
            track.get("_ema_seed_sum", 0.0)) + float(candle_close)
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


def velocity_fuse_long(current_price: float, candle_1m_open: float,
                       pct: float = VELOCITY_FUSE_PCT_DEFAULT) -> bool:
    """LONG velocity fuse: current_price < open * (1 - pct), strict.

    Strict comparator: an exact 1.00% drop returns False; 1.001%
    returns True. Ignores phase \u2014 caller invokes on every tick.
    """
    if current_price is None or candle_1m_open is None:
        return False
    return float(current_price) < float(candle_1m_open) * (1.0 - float(pct))


def velocity_fuse_short(current_price: float, candle_1m_open: float,
                        pct: float = VELOCITY_FUSE_PCT_DEFAULT) -> bool:
    """SHORT velocity fuse: current_price > open * (1 + pct), strict."""
    if current_price is None or candle_1m_open is None:
        return False
    return float(current_price) > float(candle_1m_open) * (1.0 + float(pct))


def evaluate_titan_exit(track: dict, *,
                        side: str,
                        current_price: Optional[float],
                        candle_1m_open: Optional[float],
                        velocity_fuse_pct: float = VELOCITY_FUSE_PCT_DEFAULT
                        ) -> Optional[str]:
    """v5.7.1 \u2014 Run every-tick Titan exit checks.

    Returns one of the v5.7.1 exit_reason values
    (`velocity_fuse`, `hard_stop_2c`, `be_stop`, `ema_trail`) or None.
    Velocity Fuse is checked first; phase-conditional stops follow.

    Hard-stop / BE / EMA-trail counters are advanced by the dedicated
    update_* helpers on candle CLOSES; this evaluator only checks the
    fast intra-candle guards (Velocity Fuse + active stop level).
    """
    if side == DIR_LONG:
        if velocity_fuse_long(current_price, candle_1m_open,
                              velocity_fuse_pct):
            return EXIT_REASON_VELOCITY_FUSE
        stop = track.get("current_stop")
        if (current_price is not None and stop is not None
                and float(current_price) < float(stop)
                and track.get("phase") == PHASE_HOUSE_MONEY):
            return EXIT_REASON_BE_STOP
    elif side == DIR_SHORT:
        if velocity_fuse_short(current_price, candle_1m_open,
                               velocity_fuse_pct):
            return EXIT_REASON_VELOCITY_FUSE
        stop = track.get("current_stop")
        if (current_price is not None and stop is not None
                and float(current_price) > float(stop)
                and track.get("phase") == PHASE_HOUSE_MONEY):
            return EXIT_REASON_BE_STOP
    return None
