"""v5.10.1 \u2014 Eye-of-the-Tiger live-hot-path integration glue.

This module wires the v5.10.0 pure-function evaluators in `eye_of_tiger.py`
and the rolling baseline in `volume_bucket.py` into the live scan loop in
`trade_genius.py`. v5.10.0 shipped the building blocks but left the legacy
v5.0\u2013v5.9 Tiger/Buffalo state machine on the hot path. v5.10.1's job is to
make the v5.10.0 evaluators authoritative.

Public surface used by trade_genius.py:

    - VolumeBucketBaseline singleton wired here, refreshed at 9:29 ET.
    - evaluate_section_i(side) -> dict       \u2014 Section I global permit.
    - evaluate_entry_gates(ticker, side, ...) \u2014 Sections I + II + III check.
    - evaluate_overrides(ticker, side, pos)   \u2014 Section IV per-tick brake.
    - record_entry_1 / record_entry_2 / clear_position \u2014 v5.10.0 state.
    - on_5m_close_qqq() / on_1m_close_ticker() \u2014 phase machine ticks.

Pure logic lives in eye_of_tiger / volume_bucket; this module only wires
state, caches log lines, and emits the [V5100-*] log signatures defined in
spec section VII.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime
from typing import Optional

import eye_of_tiger as eot
import volume_bucket as vb

logger = logging.getLogger("trade_genius.v5_10_1")


# ---------------------------------------------------------------------
# Module-level state. trade_genius.py is the only caller; all access
# goes through the public functions below.
# ---------------------------------------------------------------------

_volume_baseline: Optional[vb.VolumeBucketBaseline] = None
_baseline_refreshed_for_date: Optional[date] = None

# {ticker: [last 1m close, ...]} \u2014 newest last; capped at 4 entries.
_last_1m_closes: dict[str, list[float]] = {}

# {ticker: {"side":..., "state":...}} v5.10.0 position state per side.
_position_state_long: dict[str, dict] = {}
_position_state_short: dict[str, dict] = {}

# Boundary Hold cache so [V5100-BOUNDARY] only logs on state change.
_last_boundary_hold: dict[tuple[str, str], bool] = {}

# Track the last logged Section I permit state so [V5100-PERMIT] fires
# once per state-change rather than every tick.
_last_permit_long: Optional[bool] = None
_last_permit_short: Optional[bool] = None

# di_1m_prev cache for Entry 2 edge-crossing detection.
_di_1m_prev: dict[tuple[str, str], Optional[float]] = {}


# ---------------------------------------------------------------------
# Volume Bucket lifecycle
# ---------------------------------------------------------------------


def get_volume_baseline() -> vb.VolumeBucketBaseline:
    """Lazily instantiate the per-process VolumeBucketBaseline. Reads
    /data/bars on first call so trade_genius.py boot stays fast.
    """
    global _volume_baseline
    if _volume_baseline is None:
        _volume_baseline = vb.VolumeBucketBaseline()
    return _volume_baseline


def refresh_volume_baseline_if_needed(now_et: datetime) -> bool:
    """Call once per scan cycle. Refreshes the baseline at 9:29 ET on
    each new session (Val 28c spec). Returns True if a refresh ran.
    """
    global _baseline_refreshed_for_date
    today = now_et.date()
    # Refresh window: from 09:29:00 onward each session, exactly once.
    if _baseline_refreshed_for_date == today:
        return False
    if now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 29):
        return False
    try:
        get_volume_baseline().refresh(today=today)
    except Exception as e:
        logger.warning("[V5100-VOLBUCKET] refresh error: %s", e)
        return False
    _baseline_refreshed_for_date = today
    return True


# ---------------------------------------------------------------------
# Section I \u2014 Global Permit (QQQ Index Shield)
# ---------------------------------------------------------------------


def evaluate_section_i(
    side: str,
    qqq_5m_close: Optional[float],
    qqq_5m_ema9: Optional[float],
    qqq_current_price: Optional[float],
    qqq_avwap_0930: Optional[float],
) -> dict:
    """Section I evaluation passthrough. trade_genius.py provides the
    raw QQQ inputs; this returns the evaluator dict and emits the
    [V5100-PERMIT] log line on state transitions only.
    """
    return eot.evaluate_global_permit(
        side,
        qqq_5m_close,
        qqq_5m_ema9,
        qqq_current_price,
        qqq_avwap_0930,
    )


def maybe_log_permit_state(
    qqq_5m_close: Optional[float],
    qqq_5m_ema9: Optional[float],
    qqq_current_price: Optional[float],
    qqq_avwap_0930: Optional[float],
) -> None:
    """Emit a single [V5100-PERMIT] line when LONG-permit OR SHORT-permit
    state changes. Called once per scan cycle by trade_genius.py.
    """
    global _last_permit_long, _last_permit_short
    long_p = evaluate_section_i(
        eot.SIDE_LONG,
        qqq_5m_close,
        qqq_5m_ema9,
        qqq_current_price,
        qqq_avwap_0930,
    )
    short_p = evaluate_section_i(
        eot.SIDE_SHORT,
        qqq_5m_close,
        qqq_5m_ema9,
        qqq_current_price,
        qqq_avwap_0930,
    )
    long_open = bool(long_p.get("open"))
    short_open = bool(short_p.get("open"))
    if long_open != _last_permit_long or short_open != _last_permit_short:
        logger.info(
            "[V5100-PERMIT] qqq_close=%s qqq_ema9=%s qqq_avwap=%s long_open=%s short_open=%s",
            _fmt(qqq_5m_close),
            _fmt(qqq_5m_ema9),
            _fmt(qqq_avwap_0930),
            long_open,
            short_open,
        )
        _last_permit_long = long_open
        _last_permit_short = short_open


# ---------------------------------------------------------------------
# Section II.1 \u2014 Volume Bucket gate (per ticker, on 1m close)
# ---------------------------------------------------------------------


def evaluate_volume_bucket_gate(
    ticker: str,
    minute_of_day_hhmm: str,
    current_volume: float | int,
) -> dict:
    """Run baseline.check() and emit [V5100-VOLBUCKET]. Returns the raw
    check_result so callers can inspect gate / ratio / days_available.
    """
    bb = get_volume_baseline()
    res = bb.check(ticker, minute_of_day_hhmm, current_volume)
    try:
        logger.info(
            "[V5100-VOLBUCKET] ticker=%s minute_of_day=%s current_vol=%s "
            "baseline_55d=%s ratio=%s gate=%s days_available=%d",
            ticker,
            minute_of_day_hhmm,
            current_volume,
            _fmt(res.get("baseline")),
            _fmt(res.get("ratio")),
            res.get("gate"),
            res.get("days_available", 0),
        )
    except Exception:
        pass
    return res


# ---------------------------------------------------------------------
# Section II.2 \u2014 Boundary Hold
# ---------------------------------------------------------------------


def record_1m_close(ticker: str, close: float) -> None:
    """Append a closed 1m bar's close to the per-ticker rolling window
    used by Boundary Hold (we keep the last 4 values; only the most
    recent 2 are read).
    """
    if close is None:
        return
    buf = _last_1m_closes.setdefault(ticker, [])
    buf.append(float(close))
    if len(buf) > 4:
        del buf[0 : len(buf) - 4]


def record_latest_1m_close(ticker: str, closes: list) -> bool:
    """Pick the newest non-None close from a Yahoo-style ``closes``
    list and append it to the per-ticker rolling buffer used by
    Boundary Hold. Returns True iff a new value was recorded.

    v5.20.4 \u2014 Yahoo's intraday minute response keeps a forming
    bar at ``closes[-2]`` whose value is ``None`` until the minute
    boundary fully passes; by then the snapshot at ``[-1]`` has
    already shifted everything down a slot. The original guard
    ``if len >= 2 and closes[-2] is not None`` therefore failed on
    nearly every scan cycle, leaving ``_last_1m_closes`` empty for
    the entire session and starving every Phase 2 boundary check.
    This helper mirrors the bar-archive writer's existing
    walk-back: scan up to 4 slots from ``[-2]`` looking for the
    most recent non-None close, then fall back to ``[-1]`` if
    nothing earlier qualifies. De-dup against the last value
    already in the buffer so successive scan cycles within the
    same minute do not register the same closed bar twice.
    """
    if not closes:
        return False
    chosen: Optional[float] = None
    # Walk back from [-2] up to 4 slots to find the newest fully
    # closed bar.
    for back in range(2, min(len(closes), 5) + 1):
        v = closes[-back]
        if v is not None:
            chosen = float(v)
            break
    # Last-resort fallback to the snapshot at [-1].
    if chosen is None and closes[-1] is not None:
        chosen = float(closes[-1])
    if chosen is None:
        return False
    existing = _last_1m_closes.get(ticker) or []
    if existing and existing[-1] == chosen:
        return False
    record_1m_close(ticker, chosen)
    return True


# v6.2.0 \u2014 time-conditional boundary hold. Pre-10:30 ET we relax
# the requirement from 2 closed 1m bars to 1 so early-session breakouts
# are not missed waiting for the second confirmation candle. The 5/1
# TSLA replay shows ~24 of the 09:35-10:25 BOUNDARY:not_satisfied
# rejections sat on a clean uptrend the bot then chased post-11:35.
# AMZN on the same morning logged 16 such rejections, 100% would-have-
# profited, mean +$7.30/share (forensics report).
V620_FAST_BOUNDARY_ENABLED: bool = True
V620_FAST_BOUNDARY_CUTOFF_HHMM_ET: str = "12:00"


def _v620_fast_boundary_active(now_et) -> bool:
    """Return True if the 1-bar boundary relaxation is currently in
    effect. Window is [09:35 ET, V620_FAST_BOUNDARY_CUTOFF). The flag
    must also be ON. Failure-tolerant: any malformed input degrades to
    False (i.e. fall back to the spec-strict 2-bar hold).
    """
    if not V620_FAST_BOUNDARY_ENABLED:
        return False
    if now_et is None:
        return False
    try:
        hh, mm = V620_FAST_BOUNDARY_CUTOFF_HHMM_ET.split(":")
        cutoff_h = int(hh)
        cutoff_m = int(mm)
        t = now_et.time()
    except Exception:
        return False
    cutoff_minutes = cutoff_h * 60 + cutoff_m
    cur_minutes = t.hour * 60 + t.minute
    # Lower bound is the OR window end (09:36) \u2014 boundary hold is
    # not even meaningful before then. Practically the gate is only
    # called after market open so a simple upper bound is sufficient.
    return cur_minutes < cutoff_minutes


def evaluate_boundary_hold_gate(
    ticker: str,
    side: str,
    or_high: Optional[float],
    or_low: Optional[float],
    now_et=None,
) -> dict:
    """Per-ticker, per-side Boundary Hold check. Emits [V5100-BOUNDARY]
    on state changes only. Returns the raw evaluator dict.

    v6.2.0 \u2014 when ``now_et`` is supplied AND fast-boundary is
    active (pre-10:30 ET window), we require only 1 closed 1m bar
    strictly outside the boundary instead of the spec-strict 2. After
    10:30 ET (or when ``now_et`` is omitted) we keep the 2-bar hold.
    """
    closes = list(_last_1m_closes.get(ticker, []))
    if _v620_fast_boundary_active(now_et):
        required = 1
    else:
        required = eot.BOUNDARY_HOLD_REQUIRED_CLOSES
    res = eot.evaluate_boundary_hold(
        side, or_high, or_low, closes, required_closes=required
    )
    key = (ticker, side)
    prev = _last_boundary_hold.get(key)
    cur = bool(res.get("hold"))
    if prev is None or prev != cur:
        try:
            prior_close = closes[-2] if len(closes) >= 2 else None
            current_close = closes[-1] if closes else None
            logger.info(
                "[V5100-BOUNDARY] ticker=%s side=%s or_high=%s or_low=%s "
                "prior_close=%s current_close=%s consecutive_outside=%d "
                "hold=%s",
                ticker,
                side,
                _fmt(or_high),
                _fmt(or_low),
                _fmt(prior_close),
                _fmt(current_close),
                int(res.get("consecutive_outside") or 0),
                cur,
            )
        except Exception:
            pass
        _last_boundary_hold[key] = cur
    return res


# ---------------------------------------------------------------------
# Section III \u2014 Entry triggers
# ---------------------------------------------------------------------


def evaluate_entry_1_decision(
    ticker: str,
    side: str,
    *,
    permit_open: bool,
    volume_bucket_ok: bool,
    boundary_hold_ok: bool,
    di_5m: Optional[float],
    di_1m: Optional[float],
    is_nhod_or_nlod: bool,
) -> dict:
    """Section III Entry 1. Same arguments as the pure evaluator; this
    wrapper only stamps di_1m_prev for Entry-2 edge detection later.
    """
    _di_1m_prev[(ticker, side)] = di_1m
    return eot.evaluate_entry_1(
        side,
        permit_open=permit_open,
        volume_bucket_ok=volume_bucket_ok,
        boundary_hold_ok=boundary_hold_ok,
        di_5m=di_5m,
        di_1m=di_1m,
        is_nhod_or_nlod=is_nhod_or_nlod,
    )


def evaluate_entry_2_decision(
    ticker: str,
    side: str,
    *,
    entry_1_active: bool,
    permit_open_at_trigger: bool,
    di_1m_now: Optional[float],
    fresh_nhod_or_nlod: bool,
    entry_2_already_fired: bool,
) -> dict:
    """Section III Entry 2. Reads di_1m_prev from local cache so the
    caller does not need to track it. Per spec XIV.3 the conservative
    interpretation requires `permit_open_at_trigger` to be re-evaluated
    fresh by the caller (we receive its result here).
    """
    key = (ticker, side)
    prev = _di_1m_prev.get(key)
    res = eot.evaluate_entry_2(
        side,
        entry_1_active=entry_1_active,
        permit_open_at_trigger=permit_open_at_trigger,
        di_1m_prev=prev,
        di_1m_now=di_1m_now,
        fresh_nhod_or_nlod=fresh_nhod_or_nlod,
        entry_2_already_fired=entry_2_already_fired,
    )
    _di_1m_prev[key] = di_1m_now
    return res


# ---------------------------------------------------------------------
# Section IV \u2014 Tick-by-tick overrides
# ---------------------------------------------------------------------


def evaluate_section_iv(
    side: str,
    *,
    unrealized_pnl_dollars: Optional[float],
    current_price: Optional[float],
    current_1m_open: Optional[float],
    portfolio_value: Optional[float] = None,
) -> Optional[str]:
    """Returns None when no override fires, otherwise an exit_reason
    string from `eye_of_tiger.VALID_EXIT_REASONS`.

    v5.27.0 \u2014 ``portfolio_value`` (optional) drives the per-trade
    Sovereign Brake threshold via
    ``eot.scaled_sovereign_brake_dollars``. Smaller portfolio = tighter
    brake (floor -$100); larger portfolios cap at the legacy -$500.
    When the portfolio is unknown (None or non-positive) we fall back
    to the legacy absolute -$500 threshold.
    """
    if unrealized_pnl_dollars is not None:
        threshold = eot.scaled_sovereign_brake_dollars(portfolio_value)
        if eot.evaluate_sovereign_brake(
            unrealized_pnl_dollars,
            threshold=threshold,
        ):
            return eot.EXIT_REASON_SOVEREIGN_BRAKE
    if eot.evaluate_velocity_fuse(side, current_price, current_1m_open):
        return eot.EXIT_REASON_VELOCITY_FUSE
    return None


# ---------------------------------------------------------------------
# Section V \u2014 Position state helpers (Phase A/B/C)
# ---------------------------------------------------------------------


def get_position_state(ticker: str, side: str) -> Optional[dict]:
    return _state_dict_for(side).get(ticker)


def init_position_state_on_entry_1(
    ticker: str,
    side: str,
    entry_price: float,
    shares: int,
    entry_ts: datetime,
    hwm_at_entry: float,
) -> dict:
    state = eot.new_position_state(side)
    state["entry_1_price"] = float(entry_price)
    state["entry_1_shares"] = int(shares)
    state["entry_1_ts"] = entry_ts.isoformat() if entry_ts else None
    state["avg_entry"] = float(entry_price)
    state["entry_1_hwm"] = float(hwm_at_entry)
    state["phase"] = eot.PHASE_SURVIVAL
    _state_dict_for(side)[ticker] = state
    return state


def record_entry_2(
    ticker: str,
    side: str,
    entry_2_price: float,
    entry_2_shares: int,
    entry_2_ts: datetime,
) -> Optional[dict]:
    state = _state_dict_for(side).get(ticker)
    if state is None:
        return None
    state["entry_2_price"] = float(entry_2_price)
    state["entry_2_shares"] = int(entry_2_shares)
    state["entry_2_ts"] = entry_2_ts.isoformat() if entry_2_ts else None
    e1p, e1s = state.get("entry_1_price"), state.get("entry_1_shares") or 0
    e2p, e2s = state["entry_2_price"], state["entry_2_shares"]
    if e1p is not None and (e1s + e2s) > 0:
        state["avg_entry"] = (e1p * e1s + e2p * e2s) / float(e1s + e2s)
    state = eot.transition_phase_on_entry_2(state)
    _emit_phase_log(
        ticker,
        side,
        eot.PHASE_SURVIVAL,
        eot.PHASE_NEUT_LAYERED,
        "entry_2",
        state.get("current_stop"),
    )
    return state


def step_two_bar_lock_on_5m(
    ticker: str,
    side: str,
    candle_open: float,
    candle_close: float,
) -> Optional[dict]:
    """Advance the post-Entry-2 Two-Bar Lock counter. Transitions to
    NEUT_LOCKED once 2 consecutive favorable closes have been observed.
    """
    state = _state_dict_for(side).get(ticker)
    if state is None:
        return None
    if not state.get("entry_2_fired"):
        return state
    if state.get("phase") not in (eot.PHASE_NEUT_LAYERED,):
        return state
    counter = int(state.get("favorable_5m_count") or 0)
    step = eot.two_bar_lock_step(side, counter, candle_open, candle_close)
    state["favorable_5m_count"] = step["counter"]
    if step["locked"]:
        state = eot.transition_phase_on_two_bar_lock(state)
        _emit_phase_log(
            ticker,
            side,
            eot.PHASE_NEUT_LAYERED,
            eot.PHASE_NEUT_LOCKED,
            "two_bar_lock",
            state.get("current_stop"),
        )
    return state


def step_phase_c_if_eligible(
    ticker: str,
    side: str,
    ema_5m: Optional[float],
    ema_seeded: bool,
) -> Optional[dict]:
    state = _state_dict_for(side).get(ticker)
    if state is None:
        return None
    state["ema_5m"] = ema_5m
    state["ema_seeded"] = bool(ema_seeded)
    if state.get("phase") == eot.PHASE_NEUT_LOCKED and ema_seeded:
        state = eot.transition_phase_to_extraction(state)
        _emit_phase_log(
            ticker,
            side,
            eot.PHASE_NEUT_LOCKED,
            eot.PHASE_EXTRACTION,
            "ema_seed_post_lock",
            state.get("current_stop"),
        )
    return state


def evaluate_phase_c_exit(
    ticker: str,
    side: str,
    candle_5m_close: float,
) -> bool:
    """Returns True when ema_trail (Phase C "Leash") should fire."""
    state = _state_dict_for(side).get(ticker)
    if state is None or state.get("phase") != eot.PHASE_EXTRACTION:
        return False
    return eot.evaluate_ema_trail(side, candle_5m_close, state.get("ema_5m"))


def clear_position_state(ticker: str, side: str) -> None:
    _state_dict_for(side).pop(ticker, None)
    _di_1m_prev.pop((ticker, side), None)
    _last_boundary_hold.pop((ticker, side), None)


# ---------------------------------------------------------------------
# Section VI \u2014 Machine rules (helpers)
# ---------------------------------------------------------------------


def daily_circuit_breaker_tripped(cumulative_realized_pnl: float) -> bool:
    return eot.daily_circuit_breaker_tripped(cumulative_realized_pnl)


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------


def _state_dict_for(side: str) -> dict:
    if side == eot.SIDE_LONG:
        return _position_state_long
    if side == eot.SIDE_SHORT:
        return _position_state_short
    raise ValueError(f"bad side {side!r}")


def _fmt(v) -> str:
    if v is None:
        return "None"
    try:
        return "%.4f" % float(v)
    except (TypeError, ValueError):
        return str(v)


def _emit_phase_log(
    ticker: str,
    side: str,
    frm: str,
    to: str,
    trigger: str,
    stop: Optional[float],
) -> None:
    try:
        logger.info(
            "[V5100-PHASE] ticker=%s side=%s from=%s to=%s trigger=%s stop=%s",
            ticker,
            side,
            frm,
            to,
            trigger,
            _fmt(stop),
        )
    except Exception:
        pass


__all__ = [
    "get_volume_baseline",
    "refresh_volume_baseline_if_needed",
    "evaluate_section_i",
    "maybe_log_permit_state",
    "evaluate_volume_bucket_gate",
    "record_1m_close",
    "record_latest_1m_close",
    "evaluate_boundary_hold_gate",
    "evaluate_entry_1_decision",
    "evaluate_entry_2_decision",
    "evaluate_section_iv",
    "get_position_state",
    "init_position_state_on_entry_1",
    "record_entry_2",
    "step_two_bar_lock_on_5m",
    "step_phase_c_if_eligible",
    "evaluate_phase_c_exit",
    "clear_position_state",
    "daily_circuit_breaker_tripped",
]
