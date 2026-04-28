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


def _vol_bucket_per_ticker(m, tickers: list[str], minute_hhmm: str) -> dict:
    """For each ticker, return the latest Volume Bucket state without
    triggering a re-evaluation. We read the live baseline singleton
    plus the WS consumer's just-closed bucket count. If either is
    unavailable, fall back to COLDSTART defaults.
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
    for t in tickers:
        cur_v = 0
        try:
            if consumer is not None:
                cur_v = int(consumer.current_volume(t, minute_hhmm) or 0)
        except Exception:
            cur_v = 0
        gate = "COLDSTART"
        ratio = None
        baseline_med = None
        try:
            if bb is not None:
                res = bb.check(t, minute_hhmm, cur_v)
                gate = str(res.get("gate") or "COLDSTART")
                ratio = _safe_float(res.get("ratio"))
                baseline_med = _safe_float(res.get("baseline"))
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
        if glue is not None:
            try:
                long_res = glue.evaluate_boundary_hold_gate(t, "LONG", oh, ol)
                short_res = glue.evaluate_boundary_hold_gate(t, "SHORT", oh, ol)
                if bool(long_res.get("hold")):
                    state = "SATISFIED"
                    side = "LONG"
                elif bool(short_res.get("hold")):
                    state = "SATISFIED"
                    side = "SHORT"
                else:
                    long_n = int(long_res.get("consecutive_outside") or 0)
                    short_n = int(short_res.get("consecutive_outside") or 0)
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


def _per_position_v510(longs: dict, shorts: dict, prices: dict) -> dict:
    """Build {key: {phase, sovereign_brake_distance_dollars,
    entry_2_fired}} for every open position. Key is "TICKER:SIDE" so
    the dashboard can match it against the existing positions array.
    """
    out: dict = {}
    for tkr, pos in (longs or {}).items():
        try:
            entry = _safe_float(pos.get("entry_price")) or 0.0
            shares = int(pos.get("shares", 0) or 0)
            mark = _safe_float(prices.get(tkr)) or entry
            unreal = (mark - entry) * shares
            out[f"{tkr}:LONG"] = {
                "phase": _phase_for_position(pos),
                "sovereign_brake_distance_dollars": _sovereign_brake_distance(unreal),
                "entry_2_fired": bool(pos.get("v5104_entry2_fired")),
            }
        except Exception:
            continue
    for tkr, pos in (shorts or {}).items():
        try:
            entry = _safe_float(pos.get("entry_price")) or 0.0
            shares = int(pos.get("shares", 0) or 0)
            mark = _safe_float(prices.get(tkr)) or entry
            unreal = (entry - mark) * shares
            out[f"{tkr}:SHORT"] = {
                "phase": _phase_for_position(pos),
                "sovereign_brake_distance_dollars": _sovereign_brake_distance(unreal),
                "entry_2_fired": bool(pos.get("v5104_entry2_fired")),
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
        minute_hhmm = ""
    out: dict = {
        "section_i_permit": _section_i_permit(m),
        "per_ticker_v510": {},
        "per_position_v510": {},
    }
    try:
        if minute_hhmm:
            vol = _vol_bucket_per_ticker(m, list(tickers), minute_hhmm)
        else:
            vol = {
                t: {
                    "state": "COLDSTART",
                    "current_1m_vol": 0,
                    "baseline_at_minute": None,
                    "ratio": None,
                }
                for t in tickers
            }
    except Exception:
        vol = {}
    try:
        bnd = _boundary_hold_per_ticker(m, list(tickers))
    except Exception:
        bnd = {}
    per_t: dict = {}
    for t in tickers:
        per_t[t] = {
            "vol_bucket": vol.get(t)
            or {
                "state": "COLDSTART",
                "current_1m_vol": 0,
                "baseline_at_minute": None,
                "ratio": None,
            },
            "boundary_hold": bnd.get(t)
            or {
                "state": "ARMED",
                "side": None,
                "last_two_closes": [],
                "or_high": None,
                "or_low": None,
            },
        }
    out["per_ticker_v510"] = per_t
    try:
        out["per_position_v510"] = _per_position_v510(longs, shorts, prices)
    except Exception:
        pass
    return out
