"""v5.11.0 \u2014 engine.phase_machine: Phase A/B/C tick state machine.

Extracted verbatim from `trade_genius.py` (v5.10.7 lines ~6938\u20137057,
shifted after PR 197/198). Public name drops the `_v5105_` prefix per
the v5.11.0 refactor convention; the private alias remains in
trade_genius.py for one release as a deprecation shim.

Zero behavior change. Validated byte-equal pre/post the move via
`tests/golden/verify.py`.

Module-level state:
- `_v5105_last_5m_bucket` \u2014 per-(ticker, side) debounce of the most
  recent closed 5m bar fed into the phase machine. Owned by this
  module; trade_genius.py accesses it for the position-close cleanup
  at `_close_position_common` via `clear_phase_bucket(ticker, side)`.

Direct imports (no `_tg()` indirection needed):
- `engine.bars.compute_5m_ohlc_and_ema9` \u2014 already extracted in PR 196.
- `eye_of_tiger` (as `eot`) \u2014 spec gate constants + side enums.
- `v5_10_1_integration` (as `eot_glue`) \u2014 position-state mutators.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import eye_of_tiger as eot
import v5_10_1_integration as eot_glue

from engine.bars import compute_5m_ohlc_and_ema9

logger = logging.getLogger("trade_genius")


# ============================================================
# v5.10.5 \u2014 Phase B/C Triple-Lock helpers
# ============================================================
# Per-ticker tracking of the most recent closed 5m bar fed into the
# phase machine, keyed by (ticker, side). Used to debounce repeated
# step_two_bar_lock_on_5m calls until a fresh bar appears.
_v5105_last_5m_bucket: dict[tuple[str, str], int] = {}


def clear_phase_bucket(ticker: str, side: str) -> None:
    """Drop the (ticker, side) entry from the 5m-bucket debounce map.

    Called from trade_genius._close_position_common after a position
    closes so a re-entry starts in Phase A with a clean slate.
    """
    _v5105_last_5m_bucket.pop((ticker, side), None)


def phase_machine_tick(ticker: str, side: str, pos: dict, bars: dict) -> tuple[str | None, str | None]:
    """Run one tick of the v5.10 Phase B/C machine for an open position.

    Lazily initializes per-position phase state (phase 'A' on first
    sight). Steps two-bar-lock on each new closed 5m bar. Promotes to
    Phase C when the 5m EMA9 is seeded and the lock has fired. Emits
    [V5100-PHASE-B-BE] / [V5100-PHASE-C-EMA-TRAIL] log lines on exit.

    Returns (exit_reason, label) where exit_reason is None (no exit),
    'be_stop', or 'ema_trail'.
    """
    try:
        entry_p = pos.get("entry_price")
        if not entry_p:
            return None, None
        state = eot_glue.get_position_state(ticker, side)
        if state is None:
            shares = int(pos.get("shares") or 0)
            eot_glue.init_position_state_on_entry_1(
                ticker, side,
                entry_price=float(entry_p),
                shares=shares,
                entry_ts=datetime.now(tz=timezone.utc),
                hwm_at_entry=float(entry_p),
            )
            state = eot_glue.get_position_state(ticker, side)
        if state is None:
            return None, None

        # Snap Entry-2 into the v5.10 state machine when v5.10.4 has
        # already fired the scale-in. This is the trigger for advancing
        # the phase machine from SURVIVAL \u2192 NEUT_LAYERED, after which
        # the Two-Bar Lock counter starts running on closed 5m bars.
        if (not state.get("entry_2_fired")) and pos.get("v5104_entry2_fired"):
            e2_price = pos.get("v5104_entry2_price") or pos.get("entry_price")
            e2_shares = int(pos.get("v5104_entry2_shares") or 0) or int(pos.get("shares") or 0)
            eot_glue.record_entry_2(
                ticker, side,
                entry_2_price=float(e2_price),
                entry_2_shares=int(e2_shares),
                entry_2_ts=datetime.now(tz=timezone.utc),
            )
            state = eot_glue.get_position_state(ticker, side)

        # Compute closed 5m bars + EMA9.
        bundle = compute_5m_ohlc_and_ema9(bars)
        if bundle is None:
            return None, None

        # On a NEW closed 5m bucket, advance the two-bar-lock counter.
        bucket_key = (ticker, side)
        last_seen = _v5105_last_5m_bucket.get(bucket_key)
        new_bucket = bundle["last_bucket"]
        if last_seen != new_bucket:
            _v5105_last_5m_bucket[bucket_key] = new_bucket
            last_open = bundle["opens"][-1]
            last_close = bundle["closes"][-1]
            eot_glue.step_two_bar_lock_on_5m(
                ticker, side, last_open, last_close,
            )
            state = eot_glue.get_position_state(ticker, side)

        # Promote to Phase C if EMA9 is seeded and we are LOCKED.
        eot_glue.step_phase_c_if_eligible(
            ticker, side, bundle["ema9"], bundle["seeded"],
        )
        state = eot_glue.get_position_state(ticker, side)

        # Update the simple per-position phase label for /api/state.
        phase_v5 = (state or {}).get("phase")
        if phase_v5 == eot.PHASE_EXTRACTION:
            pos["phase"] = "C"
        elif phase_v5 == eot.PHASE_NEUT_LOCKED:
            pos["phase"] = "B"
        else:
            pos["phase"] = "A"

        # Phase C \u2014 ema_trail check on the most recent closed 5m close.
        if pos["phase"] == "C":
            last_close = bundle["closes"][-1]
            if eot_glue.evaluate_phase_c_exit(ticker, side, last_close):
                logger.warning(
                    "[V5100-PHASE-C-EMA-TRAIL] ticker=%s side=%s "
                    "5m_close=%.4f ema9=%s",
                    ticker, side, last_close,
                    ("%.4f" % bundle["ema9"]) if bundle["ema9"] is not None else "None",
                )
                return eot.EXIT_REASON_EMA_TRAIL, "ema_trail"

        # Phase B \u2014 break-even stop on current price vs avg_entry.
        if pos["phase"] == "B":
            current_price = bars.get("current_price")
            be_level = (state or {}).get("current_stop") or pos.get("entry_price") or entry_p
            if current_price is not None and be_level is not None:
                if side == eot.SIDE_LONG and current_price <= float(be_level):
                    logger.warning(
                        "[V5100-PHASE-B-BE] ticker=%s side=LONG cur=%.4f be=%.4f",
                        ticker, current_price, float(be_level),
                    )
                    return eot.EXIT_REASON_BE_STOP, "be_stop"
                if side == eot.SIDE_SHORT and current_price >= float(be_level):
                    logger.warning(
                        "[V5100-PHASE-B-BE] ticker=%s side=SHORT cur=%.4f be=%.4f",
                        ticker, current_price, float(be_level),
                    )
                    return eot.EXIT_REASON_BE_STOP, "be_stop"
        return None, None
    except Exception as _phase_e:
        logger.warning("[V5100-PHASE] tick error %s/%s: %s", ticker, side, _phase_e)
        return None, None


__all__ = [
    "phase_machine_tick",
    "clear_phase_bucket",
]
