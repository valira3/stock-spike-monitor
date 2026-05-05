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
from datetime import date, datetime, time as dtime, timezone
from typing import Optional

import eye_of_tiger as eot
import volume_bucket as vb

logger = logging.getLogger("trade_genius.v5_10_1")


def _parse_refresh_hhmm_et() -> tuple[int, int]:
    """Parse VOLUME_BUCKET_REFRESH_HHMM_ET (\"HH:MM\") into (hour, minute).

    Defaults to (4, 0) if the constant is malformed; the gate stays
    fail-closed (will not fire before the hardcoded fallback time).
    """
    raw = getattr(vb, "VOLUME_BUCKET_REFRESH_HHMM_ET", "04:00")
    try:
        hh, mm = raw.split(":", 1)
        return int(hh), int(mm)
    except Exception:
        logger.warning(
            "[V5100-VOLBUCKET] malformed VOLUME_BUCKET_REFRESH_HHMM_ET=%r, "
            "using 04:00 fallback",
            raw,
        )
        return 4, 0


# ---------------------------------------------------------------------
# Module-level state. trade_genius.py is the only caller; all access
# goes through the public functions below.
# ---------------------------------------------------------------------

_volume_baseline: Optional[vb.VolumeBucketBaseline] = None
_baseline_refreshed_for_date: Optional[date] = None
# v6.14.8 \u2014 self-heal rate-limit. Stamped (UTC) every time we attempt
# a recovery refresh. Compared against
# vb.VOLUME_BUCKET_SELF_HEAL_INTERVAL_SEC so a wedged scan loop cannot
# spam refreshes faster than once per interval.
_last_self_heal_attempt_utc: Optional[datetime] = None

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


def _baseline_is_empty(baseline: vb.VolumeBucketBaseline) -> bool:
    """v6.14.8 \u2014 True iff every ticker reports days_available=0.

    A non-empty per-ticker map with all zeros means refresh ran but
    found no archive coverage (Val\u2019s \u201c0/55 days available\u201d symptom).
    An empty map means refresh has never been called yet \u2014 the gate
    handles that path separately.
    """
    per_ticker = baseline.days_available_per_ticker
    if not per_ticker:
        return True
    return all(v == 0 for v in per_ticker.values())


def refresh_volume_baseline_if_needed(now_et: datetime) -> bool:
    """Call once per scan cycle. Refreshes the rolling volume baseline
    on the first scan tick at-or-after VOLUME_BUCKET_REFRESH_HHMM_ET
    (default 04:00 ET, v6.14.8) on each new session.

    Self-heal (v6.14.8): if the scheduled refresh has already fired for
    today but the in-memory baseline is empty (every ticker has
    days_available=0), trigger a recovery refresh, rate-limited to once
    per VOLUME_BUCKET_SELF_HEAL_INTERVAL_SEC. This rescues sessions
    where the 04:00 ET refresh ran before the bar archive had been
    populated for the lookback window.

    Returns True iff a refresh actually ran (scheduled or recovery).
    """
    global _baseline_refreshed_for_date, _last_self_heal_attempt_utc
    today = now_et.date()
    refresh_h, refresh_m = _parse_refresh_hhmm_et()

    # ---- Scheduled path: first tick at-or-after refresh time today ----
    if _baseline_refreshed_for_date != today:
        if now_et.hour < refresh_h or (
            now_et.hour == refresh_h and now_et.minute < refresh_m
        ):
            return False
        try:
            get_volume_baseline().refresh(today=today)
        except Exception as e:
            logger.warning("[V5100-VOLBUCKET] refresh error: %s", e)
            return False
        _baseline_refreshed_for_date = today
        return True

    # ---- Self-heal path: scheduled refresh already ran today ----
    baseline = get_volume_baseline()
    if not _baseline_is_empty(baseline):
        return False

    now_utc = datetime.now(timezone.utc)
    interval = getattr(vb, "VOLUME_BUCKET_SELF_HEAL_INTERVAL_SEC", 60)
    if (
        _last_self_heal_attempt_utc is not None
        and (now_utc - _last_self_heal_attempt_utc).total_seconds() < interval
    ):
        return False
    _last_self_heal_attempt_utc = now_utc

    logger.warning(
        "[V5100-VOLBUCKET-RECOVERY] baseline empty after scheduled "
        "refresh, retrying (interval=%ds)",
        interval,
    )
    try:
        baseline.refresh(today=today)
    except Exception as e:
        logger.warning("[V5100-VOLBUCKET-RECOVERY] retry error: %s", e)
        return False
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
# v6.14.10 \u2014 Live volume-bucket gate (Section II.1, vAA-1)
# ---------------------------------------------------------------------
#
# History: v5.26.0 spec amendment (2026-04-30) BYPASSED the BL-3/BU-3
# Volume Bucket gate in the production entry path; broker/orders.py
# hardcoded ``volume_bucket_ok=True`` when calling
# ``evaluate_entry_1_decision``. The dashboard kept reading the gate
# state for display, but no live entry could be rejected on volume.
#
# v6.14.10 reverses the bypass behind a new env knob
# ``VOLUME_GATE_LIVE_ENFORCE`` (default True). The evaluator below
# mirrors the dashboard's bucket-and-archive-volume lookup so prod
# and dashboard can never disagree about whether a given tick passes
# the gate. vAA-1 spec rules are honoured exactly:
#
#   * VOLUME_GATE_ENABLED False  -> True   (kill switch)
#   * VOLUME_GATE_LIVE_ENFORCE False -> True (kill switch, layer 2)
#   * now_et < 10:00 ET          -> True   (spec L-P2-S3 / S-P2-S3)
#   * baseline COLDSTART          -> True   (insufficient history)
#   * gate PASS                   -> True
#   * gate FAIL                   -> False  (the only rejection path)
#
# Entries inherit ``VOLUME_BUCKET_THRESHOLD_RATIO`` from
# ``volume_bucket.py`` exactly as the dashboard does; setting that env
# var lower (e.g. 0.85) admits trades the strict 1.00 default would
# reject.


def evaluate_volume_bucket_live(
    ticker: str,
    now_et: datetime,
    bars: dict | None,
) -> dict:
    """Live entry-path volume-bucket evaluator (v6.14.10).

    Returns a dict with keys:
        ok: bool          - True means "gate open, do not reject".
        reason: str       - human-readable status (passthrough, pass,
                            fail, coldstart, disabled, no_bucket, etc).
        gate: str | None  - raw bb.check() gate when evaluated, else None.
        ratio: float|None - raw ratio when evaluated.
        bucket: str|None  - just-closed bucket key when evaluated.

    The caller in broker/orders.py uses ``ok`` directly as the
    ``volume_bucket_ok`` argument to ``evaluate_entry_1_decision``.
    Every non-FAIL path returns ok=True so the gate can only
    *reject* trades when the spec demands it (post-10:00 ET, with
    sufficient baseline coverage, and ratio strictly under the
    configured threshold).
    """
    import os as _os
    from engine import feature_flags as _ff

    # Layer 1: spec-level kill switch (also gates the dashboard chip).
    if not _ff.VOLUME_GATE_ENABLED:
        return {"ok": True, "reason": "disabled", "gate": None,
                "ratio": None, "bucket": None}

    # Layer 2: v6.14.10 isolation knob \u2014 lets us flip live enforcement
    # off without touching VOLUME_GATE_ENABLED (which the dashboard
    # also reads). Default True so the gate enforces by default.
    enforce_raw = _os.environ.get("VOLUME_GATE_LIVE_ENFORCE")
    if enforce_raw is not None:
        if enforce_raw.strip().lower() not in {"1", "true", "yes", "on"}:
            return {"ok": True, "reason": "live_enforce_off",
                    "gate": None, "ratio": None, "bucket": None}

    # vAA-1 spec L-P2-S3 / S-P2-S3: pre-10:00 ET auto-pass.
    if now_et is None or now_et.time() < dtime(10, 0):
        return {"ok": True, "reason": "pre_10am_passthrough",
                "gate": None, "ratio": None, "bucket": None}

    # Resolve the just-closed bucket key from now_et. The vAA-1 path
    # compares against the minute that JUST closed, never the still-
    # forming minute, so the WS bar (or archive bar) is fully written.
    try:
        from volume_profile import previous_session_bucket as _prev_b
        bucket = _prev_b(now_et)
    except Exception:
        bucket = None
    if not bucket:
        return {"ok": True, "reason": "no_bucket", "gate": None,
                "ratio": None, "bucket": None}

    # Resolve the bar volume for that bucket. Two sources, in order:
    #   1. The fetched 1m bars dict (already in hand inside
    #      check_breakout). The most recent fully-closed bar is
    #      bars["volumes"][-2] when bars["volumes"][-1] is the still-
    #      forming minute. Fall back to [-1] when only one bar exists
    #      or the last entry is a clean close.
    #   2. The bar archive (same path the dashboard uses), so prod and
    #      dashboard can never disagree.
    cv: int = 0
    try:
        if bars is not None:
            vols = bars.get("volumes") or []
            if len(vols) >= 2 and vols[-2] is not None:
                cv = int(float(vols[-2]))
            elif len(vols) >= 1 and vols[-1] is not None:
                cv = int(float(vols[-1]))
    except (TypeError, ValueError):
        cv = 0

    bb = get_volume_baseline()
    res = bb.check(ticker, bucket, cv)
    gate = res.get("gate")
    ratio = res.get("ratio")

    # Spec-mandated cold-start passthrough \u2014 never fail-closed when the
    # baseline simply has not collected enough history yet.
    if gate == "COLDSTART":
        return {"ok": True, "reason": "coldstart", "gate": gate,
                "ratio": ratio, "bucket": bucket}

    if gate == "PASS":
        return {"ok": True, "reason": "pass", "gate": gate,
                "ratio": ratio, "bucket": bucket}

    # gate == FAIL \u2014 the only rejection path. Log forensically so we
    # can audit which tickers/buckets get rejected in prod.
    try:
        logger.info(
            "[V6_14_10-VOLGATE-FAIL] ticker=%s bucket=%s ratio=%s "
            "threshold=%s current_vol=%s",
            ticker, bucket, _fmt(ratio),
            _fmt(getattr(vb, "VOLUME_BUCKET_THRESHOLD_RATIO", 1.0)),
            cv,
        )
    except Exception:
        pass
    return {"ok": False, "reason": "fail", "gate": gate,
            "ratio": ratio, "bucket": bucket}


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
    "evaluate_volume_bucket_live",
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
