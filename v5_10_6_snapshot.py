"""v5.10.6 \u2014 dashboard snapshot helpers for the Eye-of-the-Tiger panel.

This module exposes a single public function `build_v510_snapshot(m)` that
trade_genius / dashboard_server can call from inside `snapshot()` to surface
the live v5.10 state to the dashboard without growing dashboard_server.py
or coupling it to v5_10_1_integration internals.

Returns a dict with three keys:

  - section_i_permit: top-level Section I (QQQ Market Shield + Sovereign
    Anchor) state. {long_open, short_open, qqq_5m_close, qqq_5m_ema9,
    qqq_avwap_0930, sovereign_anchor_open}
  - per_ticker_v510: list of dicts \u2014 one per trade ticker \u2014 with the
    Volume Bucket and Boundary Hold gate state.
  - per_position_v510: dict keyed by (ticker, side) string "TICKER:SIDE"
    \u2014 surfaces phase + sovereign brake distance + entry_2 fired flag.

`m` is the trade_genius module handle (caller supplies it so we don't
import lazily from inside dashboard_server's executor thread).

The helper is defensive: every getattr / state read is wrapped so a
malformed cache value drops *that* field, not the whole snapshot. The
existing /api/state contract stays intact.
"""

from __future__ import annotations

from typing import Any


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _qqq_avwap_open(m) -> float | None:
    """Read the live QQQ opening AVWAP without re-fetching bars.

    The scan loop calls _opening_avwap("QQQ") each cycle; the result is
    only logged, not cached, so we recompute defensively here. Returns
    None on any error (helper must never raise).
    """
    try:
        fn = getattr(m, "_opening_avwap", None)
        if fn is None:
            return None
        return _safe_float(fn("QQQ"))
    except Exception:
        return None


def _qqq_regime_state(m) -> tuple[float | None, float | None]:
    """Pull the QQQ Regime Shield's latest closed-5m close and 9-EMA.

    Mirrors the read pattern used by maybe_log_permit_state in
    trade_genius.py L9977-L9978.
    """
    try:
        regime = getattr(m, "_QQQ_REGIME", None)
        if regime is None:
            return (None, None)
        return (
            _safe_float(getattr(regime, "last_close", None)),
            _safe_float(getattr(regime, "ema9", None)),
        )
    except Exception:
        return (None, None)


def _qqq_current_price(m) -> float | None:
    try:
        fetch = getattr(m, "fetch_1min_bars", None)
        if fetch is None:
            return None
        bars = fetch("QQQ")
        if not bars:
            return None
        return _safe_float(bars.get("current_price"))
    except Exception:
        return None


def _section_i_permit(m) -> dict:
    """Build the section_i_permit block. Calls evaluate_section_i twice
    (LONG, SHORT) so both rails are visible to the operator; the
    Sovereign Anchor leg is shared between sides so we report it once
    on the LONG-side reading.
    """
    qqq_close, qqq_ema9 = _qqq_regime_state(m)
    qqq_cur = _qqq_current_price(m)
    qqq_avwap = _qqq_avwap_open(m)
    out = {
        "qqq_5m_close": qqq_close,
        "qqq_5m_ema9": qqq_ema9,
        "qqq_current_price": qqq_cur,
        "qqq_avwap_0930": qqq_avwap,
        "long_open": False,
        "short_open": False,
        "sovereign_anchor_open": False,
    }
    try:
        glue = getattr(m, "_v510_integration", None) or _import_glue()
        if glue is None:
            return out
        long_p = glue.evaluate_section_i("LONG", qqq_close, qqq_ema9, qqq_cur, qqq_avwap)
        short_p = glue.evaluate_section_i("SHORT", qqq_close, qqq_ema9, qqq_cur, qqq_avwap)
        out["long_open"] = bool(long_p.get("open"))
        out["short_open"] = bool(short_p.get("open"))
        if qqq_cur is not None and qqq_avwap is not None:
            out["sovereign_anchor_open"] = bool(qqq_cur > qqq_avwap)
    except Exception:
        pass
    return out


def _import_glue():
    try:
        import v5_10_1_integration as glue

        return glue
    except Exception:
        return None


def _vol_bucket_per_ticker(
    m, tickers: list[str], minute_hhmm: str, prev_minute_hhmm: str | None
) -> dict:
    """For each ticker, return the latest Volume Bucket state without
    triggering a re-evaluation. We read the live baseline singleton
    plus the WS consumer's just-closed bucket count. If either is
    unavailable, fall back to COLDSTART defaults.

    v5.20.5: looks up the WS consumer at ``prev_minute_hhmm`` (the
    just-closed bucket) instead of ``minute_hhmm`` (the still-forming
    one). Adds ``days_available``, ``lookback_days``, and
    ``ratio_to_55bar_avg`` so dashboard cards can explain why a gate
    is in COLDSTART vs PASS vs FAIL without round-tripping the logs.
    """
    out: dict = {}
    glue = _import_glue()
    consumer = getattr(m, "_ws_consumer", None)
    try:
        if glue is not None:
            bb = glue.get_volume_baseline()
        else:
            bb = None
    except Exception:
        bb = None
    # v5.20.5 \u2014 the lookback constant lives on the baseline; surface
    # it so the card can render "days_available / lookback_days".
    lookback_days = None
    try:
        if bb is not None:
            lookback_days = int(getattr(bb, "lookback_days", 0) or 0) or None
    except Exception:
        lookback_days = None
    lookup_bucket = prev_minute_hhmm or minute_hhmm
    for t in tickers:
        cur_v = 0
        try:
            if consumer is not None and lookup_bucket:
                cur_v = int(consumer.current_volume(t, lookup_bucket) or 0)
        except Exception:
            cur_v = 0
        gate = "COLDSTART"
        ratio = None
        baseline_med = None
        days_available = None
        try:
            if bb is not None:
                # Use the just-closed bucket so the gate result
                # matches what entry-1 would actually see.
                res = bb.check(t, lookup_bucket or minute_hhmm, cur_v)
                gate = str(res.get("gate") or "COLDSTART")
                ratio = _safe_float(res.get("ratio"))
                baseline_med = _safe_float(res.get("baseline"))
                days_available = res.get("days_available")
                if days_available is not None:
                    try:
                        days_available = int(days_available)
                    except (TypeError, ValueError):
                        days_available = None
        except Exception:
            pass
        state = {
            "PASS": "PASS",
            "FAIL": "FAIL",
            "COLDSTART": "COLDSTART",
        }.get(gate, "COLDSTART")
        out[t] = {
            "state": state,
            "current_1m_vol": int(cur_v),
            "baseline_at_minute": baseline_med,
            "ratio": ratio,
            # v5.20.5 \u2014 explanatory metrics surfaced for the card UI.
            "ratio_to_55bar_avg": ratio,
            "days_available": days_available,
            "lookback_days": lookback_days,
            "lookup_bucket": lookup_bucket,
        }
    return out


def _boundary_hold_per_ticker(m, tickers: list[str]) -> dict:
    """Read each ticker's Boundary Hold cache + the OR window levels.
    No re-evaluation \u2014 we only surface what's already in the glue's
    rolling 1m close window.
    """
    out: dict = {}
    glue = _import_glue()
    or_high = getattr(m, "or_high", {}) or {}
    or_low = getattr(m, "or_low", {}) or {}
    closes_cache: dict = {}
    if glue is not None:
        try:
            closes_cache = getattr(glue, "_last_1m_closes", {}) or {}
        except Exception:
            closes_cache = {}
    for t in tickers:
        last_two = list(closes_cache.get(t, []) or [])[-2:]
        oh = _safe_float(or_high.get(t))
        ol = _safe_float(or_low.get(t))
        # Side resolution: we report whichever side is currently
        # SATISFIED if any; otherwise the side closest to qualifying
        # (most outside closes). Default to LONG when ambiguous.
        side = None
        state = "ARMED"
        # v5.20.5 \u2014 surface raw consecutive_outside counts so the card
        # can show "LONG: 1/2 closes outside, SHORT: 0/2 closes outside".
        long_consec = 0
        short_consec = 0
        if glue is not None:
            try:
                long_res = glue.evaluate_boundary_hold_gate(t, "LONG", oh, ol)
                short_res = glue.evaluate_boundary_hold_gate(t, "SHORT", oh, ol)
                long_consec = int(long_res.get("consecutive_outside") or 0)
                short_consec = int(short_res.get("consecutive_outside") or 0)
                if bool(long_res.get("hold")):
                    state = "SATISFIED"
                    side = "LONG"
                elif bool(short_res.get("hold")):
                    state = "SATISFIED"
                    side = "SHORT"
                else:
                    long_n = long_consec
                    short_n = short_consec
                    if long_n == 0 and short_n == 0:
                        state = "ARMED"
                        side = None
                    elif long_n >= short_n:
                        state = "ARMED" if long_n < 2 else "SATISFIED"
                        side = "LONG"
                    else:
                        state = "ARMED" if short_n < 2 else "SATISFIED"
                        side = "SHORT"
                    if (
                        state == "ARMED"
                        and last_two
                        and (
                            (
                                side == "LONG"
                                and oh is not None
                                and last_two[-1] is not None
                                and last_two[-1] <= oh
                                and len(last_two) >= 2
                                and last_two[-2] > oh
                            )
                            or (
                                side == "SHORT"
                                and ol is not None
                                and last_two[-1] is not None
                                and last_two[-1] >= ol
                                and len(last_two) >= 2
                                and last_two[-2] < ol
                            )
                        )
                    ):
                        state = "BROKEN"
            except Exception:
                pass
        out[t] = {
            "state": state,
            "side": side,
            "last_two_closes": [_safe_float(c) for c in last_two],
            "or_high": oh,
            "or_low": ol,
            # v5.20.5 \u2014 expose raw consec counts for the dashboard card.
            # Renders as "LONG: long_consec/2, SHORT: short_consec/2".
            "long_consecutive_outside": long_consec,
            "short_consecutive_outside": short_consec,
        }
    return out


def _di_per_ticker(m, tickers: list[str]) -> dict:
    """v5.20.5 \u2014 surface DI+/DI- on both 1m and 5m for each ticker.

    Reads ``v5_di_1m_5m`` (no recompute incurred since fetch_1min_bars
    is cached per scan cycle) and the ``TIGER_V2_DI_THRESHOLD`` value
    so the card can render "DI+ 28.4 / DI- 11.0 (need >=25)" instead
    of just "PASS" or "FAIL".

    Each per-ticker entry: {di_plus_1m, di_minus_1m, di_plus_5m,
    di_minus_5m, threshold, seed_bars, sufficient}. Any individual
    field is None when warmup is incomplete.
    """
    out: dict = {}
    threshold = _safe_float(getattr(m, "TIGER_V2_DI_THRESHOLD", None))
    seed_cache = getattr(m, "_DI_SEED_CACHE", {}) or {}
    fn = getattr(m, "v5_di_1m_5m", None)
    for t in tickers:
        seed = seed_cache.get(t) or []
        seed_n = len(seed)
        di = {
            "di_plus_1m": None,
            "di_minus_1m": None,
            "di_plus_5m": None,
            "di_minus_5m": None,
        }
        if fn is not None:
            try:
                raw = fn(t) or {}
                for k in di:
                    di[k] = _safe_float(raw.get(k))
            except Exception:
                pass
        out[t] = {
            **di,
            "threshold": threshold,
            "seed_bars": int(seed_n),
            "sufficient": bool(seed_n >= 15),
        }
    return out


def _phase_for_position(pos: dict) -> str:
    """Defensive read of pos["phase"] \u2014 v5.10.5 wires this on every
    manage tick (trade_genius.py:8890). Falls back to "A" if unset.
    """
    phase = str(pos.get("phase") or "A").upper()
    return phase if phase in ("A", "B", "C") else "A"


def _sovereign_brake_distance(unrealized: float | None) -> float | None:
    """Distance until Sovereign Brake fires. The brake fires at
    unrealized P&L \u2264 -$500 (eye_of_tiger.evaluate_sovereign_brake), so
    distance = unrealized + 500. Positive = breathing room; near-zero
    or negative = imminent / already tripped.
    """
    if unrealized is None:
        return None
    try:
        return float(unrealized) + 500.0
    except (TypeError, ValueError):
        return None


# v5.20.5 \u2014 thresholds surfaced on dashboard cards (see Change 3 in spec).
_SOVEREIGN_BRAKE_DOLLARS = -500.0
_VELOCITY_FUSE_PCT = 0.01


def _time_in_position_min(pos: dict) -> float | None:
    """Minutes since entry. Reads ``entry_time`` (ISO) from the position
    dict; returns None if unparseable. v5.20.5 dashboard card metric.
    """
    raw = pos.get("entry_time") or pos.get("entry_ts_utc")
    if not raw:
        return None
    try:
        from datetime import datetime as _dt, timezone as _tz

        s = str(raw).replace("Z", "+00:00")
        ts = _dt.fromisoformat(s)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_tz.utc)
        now = _dt.now(tz=_tz.utc)
        delta = (now - ts).total_seconds() / 60.0
        return round(delta, 1) if delta >= 0 else None
    except Exception:
        return None


def _last_5m_move_pct(tkr: str, pos: dict, prices: dict) -> float | None:
    """Pct change of the current 1m bar (open \u2192 last). Surfaced on the
    Velocity Fuse card so traders can see how close the fuse is to tripping.
    Reads ``current_1m_open`` from the position dict if the manage tick
    stamped it; otherwise returns None (null-safe per spec).
    """
    op = _safe_float(pos.get("current_1m_open"))
    px = _safe_float(prices.get(tkr))
    if op is None or px is None or op <= 0.0:
        return None
    try:
        return round((px - op) / op * 100.0, 4)
    except Exception:
        return None


def _strikes_block(tkr: str) -> dict:
    """Strike counter + (placeholder) recent-event history for the POS
    Strikes card. v5.20.5 surfaces the count from
    ``trade_genius._v570_strike_counts``; ``strike_history`` is a stub
    list (empty) until a per-ticker event log is wired separately.
    """
    count: int | None = None
    try:
        import trade_genius as _tg

        counts = getattr(_tg, "_v570_strike_counts", {}) or {}
        count = int(counts.get(str(tkr).upper(), 0))
    except Exception:
        count = None
    return {
        "strikes_count": count,
        "strike_history": [],  # placeholder; populated by future event log
    }


def _per_position_v510(longs: dict, shorts: dict, prices: dict) -> dict:
    """Build {key: {phase, sovereign_brake_distance_dollars, entry_2_fired,
    sovereign_brake, velocity_fuse, strikes}} for every open position.
    Key is "TICKER:SIDE" so the dashboard can match it against the
    existing positions array. v5.20.5 adds card-metric blocks per spec.
    """
    out: dict = {}
    for tkr, pos in (longs or {}).items():
        try:
            entry = _safe_float(pos.get("entry_price")) or 0.0
            shares = int(pos.get("shares", 0) or 0)
            mark = _safe_float(prices.get(tkr)) or entry
            unreal = (mark - entry) * shares
            position_value = entry * shares if (entry and shares) else 0.0
            unreal_pct: float | None
            brake_pct: float | None
            if position_value > 0.0:
                unreal_pct = round(unreal / position_value * 100.0, 4)
                brake_pct = round(_SOVEREIGN_BRAKE_DOLLARS / position_value * 100.0, 4)
            else:
                unreal_pct = None
                brake_pct = None
            out[f"{tkr}:LONG"] = {
                "phase": _phase_for_position(pos),
                "sovereign_brake_distance_dollars": _sovereign_brake_distance(unreal),
                "entry_2_fired": bool(pos.get("v5104_entry2_fired")),
                "sovereign_brake": {
                    "unrealized_pct": unreal_pct,
                    "brake_threshold_pct": brake_pct,
                    "brake_threshold_dollars": _SOVEREIGN_BRAKE_DOLLARS,
                    "time_in_position_min": _time_in_position_min(pos),
                },
                "velocity_fuse": {
                    "last_5m_move_pct": _last_5m_move_pct(tkr, pos, prices),
                    "fuse_threshold_pct": _VELOCITY_FUSE_PCT * 100.0,
                },
                "strikes": _strikes_block(tkr),
            }
        except Exception:
            continue
    for tkr, pos in (shorts or {}).items():
        try:
            entry = _safe_float(pos.get("entry_price")) or 0.0
            shares = int(pos.get("shares", 0) or 0)
            mark = _safe_float(prices.get(tkr)) or entry
            unreal = (entry - mark) * shares
            position_value = entry * shares if (entry and shares) else 0.0
            unreal_pct: float | None
            brake_pct: float | None
            if position_value > 0.0:
                unreal_pct = round(unreal / position_value * 100.0, 4)
                brake_pct = round(_SOVEREIGN_BRAKE_DOLLARS / position_value * 100.0, 4)
            else:
                unreal_pct = None
                brake_pct = None
            out[f"{tkr}:SHORT"] = {
                "phase": _phase_for_position(pos),
                "sovereign_brake_distance_dollars": _sovereign_brake_distance(unreal),
                "entry_2_fired": bool(pos.get("v5104_entry2_fired")),
                "sovereign_brake": {
                    "unrealized_pct": unreal_pct,
                    "brake_threshold_pct": brake_pct,
                    "brake_threshold_dollars": _SOVEREIGN_BRAKE_DOLLARS,
                    "time_in_position_min": _time_in_position_min(pos),
                },
                "velocity_fuse": {
                    "last_5m_move_pct": _last_5m_move_pct(tkr, pos, prices),
                    "fuse_threshold_pct": _VELOCITY_FUSE_PCT * 100.0,
                },
                "strikes": _strikes_block(tkr),
            }
        except Exception:
            continue
    return out


def build_v510_snapshot(m, tickers: list[str], longs: dict, shorts: dict, prices: dict) -> dict:
    """Top-level v5.10 snapshot. Never raises; on internal error returns
    the partial dict accumulated so far so the parent /api/state still
    serializes successfully.
    """
    try:
        now_et = m._now_et()
        minute_hhmm = now_et.strftime("%H%M")
    except Exception:
        now_et = None
        minute_hhmm = ""
    # v5.20.5 \u2014 the just-closed bucket (current_minute - 1) is what
    # the WS consumer's _volumes dict is keyed by; the still-forming
    # current minute returns 0 until it closes. Compute once here so
    # the volume helper can do a single lookup against real volume.
    prev_minute_hhmm: str | None = None
    if now_et is not None:
        try:
            from volume_profile import previous_session_bucket as _prev_b

            prev_minute_hhmm = _prev_b(now_et)
        except Exception:
            prev_minute_hhmm = None
    out: dict = {
        "section_i_permit": _section_i_permit(m),
        "per_ticker_v510": {},
        "per_position_v510": {},
    }
    try:
        if minute_hhmm:
            vol = _vol_bucket_per_ticker(m, list(tickers), minute_hhmm, prev_minute_hhmm)
        else:
            vol = {
                t: {
                    "state": "COLDSTART",
                    "current_1m_vol": 0,
                    "baseline_at_minute": None,
                    "ratio": None,
                    "ratio_to_55bar_avg": None,
                    "days_available": None,
                    "lookback_days": None,
                    "lookup_bucket": None,
                }
                for t in tickers
            }
    except Exception:
        vol = {}
    try:
        bnd = _boundary_hold_per_ticker(m, list(tickers))
    except Exception:
        bnd = {}
    try:
        di_blk = _di_per_ticker(m, list(tickers))
    except Exception:
        di_blk = {}
    per_t: dict = {}
    for t in tickers:
        per_t[t] = {
            "vol_bucket": vol.get(t)
            or {
                "state": "COLDSTART",
                "current_1m_vol": 0,
                "baseline_at_minute": None,
                "ratio": None,
                "ratio_to_55bar_avg": None,
                "days_available": None,
                "lookback_days": None,
                "lookup_bucket": None,
            },
            "boundary_hold": bnd.get(t)
            or {
                "state": "ARMED",
                "side": None,
                "last_two_closes": [],
                "or_high": None,
                "or_low": None,
                "long_consecutive_outside": 0,
                "short_consecutive_outside": 0,
            },
            "di": di_blk.get(t)
            or {
                "di_plus_1m": None,
                "di_minus_1m": None,
                "di_plus_5m": None,
                "di_minus_5m": None,
                "threshold": None,
                "seed_bars": 0,
                "sufficient": False,
            },
        }
    out["per_ticker_v510"] = per_t
    try:
        out["per_position_v510"] = _per_position_v510(longs, shorts, prices)
    except Exception:
        pass
    return out
