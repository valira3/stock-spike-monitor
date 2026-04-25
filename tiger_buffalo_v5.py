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
DMI_PERIOD = 14

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


def gates_pass_long(qqq_last, qqq_pdc, spy_last, spy_pdc,
                    ticker_last, ticker_pdc, ticker_first_hour_high) -> bool:
    """L-P1 — Long Permission Gates (G1..G4).

    All four must be strictly true. Any None input fails closed.
    """
    if None in (qqq_last, qqq_pdc, spy_last, spy_pdc,
                ticker_last, ticker_pdc, ticker_first_hour_high):
        return False
    if not (qqq_last > qqq_pdc):              # L-P1-G1
        return False
    if not (spy_last > spy_pdc):              # L-P1-G2
        return False
    if not (ticker_last > ticker_pdc):        # L-P1-G3
        return False
    if not (ticker_last > ticker_first_hour_high):  # L-P1-G4
        return False
    return True


def gates_pass_short(qqq_last, qqq_pdc, spy_last, spy_pdc,
                     ticker_last, ticker_pdc, opening_range_low_5m) -> bool:
    """S-P1 — Short Permission Gates (G1..G4).

    Mirror of long gates. If indices are green, shorts are forbidden
    regardless of ticker weakness.
    """
    if None in (qqq_last, qqq_pdc, spy_last, spy_pdc,
                ticker_last, ticker_pdc, opening_range_low_5m):
        return False
    if not (qqq_last < qqq_pdc):              # S-P1-G1
        return False
    if not (spy_last < spy_pdc):              # S-P1-G2
        return False
    if not (ticker_last < ticker_pdc):        # S-P1-G3
        return False
    if not (ticker_last < opening_range_low_5m):  # S-P1-G4
        return False
    return True


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
def evaluate_exit(track: dict, ticker_last, di_1m_closed) -> Optional[str]:
    """Run the priority-ordered exit checks for a STAGE_2/TRAILING track.

    Returns the exit reason ("DI_HARD_EJECT", "STRUCTURAL_STOP") or None.

    Direction-aware ordering:
      - Long (L-P4-R3): structural stop OR DI<25; either fires.
      - Short (S-P4-R3): DI<25 priority-1 (BEFORE structural-stop check),
        S-P4-R4 structural priority-2.

    di_1m_closed should be passed only on a closed 1m candle; pass None
    for intra-candle ticks (per C-R3 the structural stop still evaluates
    on every tick).
    """
    direction = track["direction"]
    state = track["state"]
    if state not in (STATE_STAGE_2, STATE_TRAILING):
        return None
    current_stop = track.get("current_stop")

    if direction == DIR_SHORT:
        # S-P4-R3 priority-1
        if di_1m_closed is not None and hard_exit_di_fail(DIR_SHORT, di_1m_closed):
            return "DI_HARD_EJECT"
        # S-P4-R4 priority-2
        if structural_stop_hit_short(ticker_last, current_stop):
            return "STRUCTURAL_STOP"
        return None

    # Long
    # L-P4-R3 (a) structural exit
    if structural_stop_hit_long(ticker_last, current_stop):
        return "STRUCTURAL_STOP"
    # L-P4-R3 (b) DI failure
    if di_1m_closed is not None and hard_exit_di_fail(DIR_LONG, di_1m_closed):
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
