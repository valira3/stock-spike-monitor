"""v5.13.2 \u2014 dashboard snapshot helper for the Tiger Sovereign panel.

This module exposes a single public function `build_tiger_sovereign_snapshot`
that dashboard_server can call from inside its `snapshot()` builder to
surface the spec-correct Phase 1\u20134 state to the dashboard.

The block sits *alongside* the existing v5.10.6 snapshot fields
(section_i_permit, per_ticker_v510, per_position_v510); it does not
replace them. Track C is dashboard-only \u2014 no algo logic changes.

Returned shape (every field is optional / None-safe so the renderer can
draw em-dashes when state has not yet warmed up):

    {
      "phase1": {
        "long":  {"qqq_5m_close", "qqq_5m_ema9", "qqq_avwap_0930",
                  "qqq_last", "permit"},
        "short": {... "permit"}
      },
      "phase2": [
        {"ticker", "vol_gate_status" \u2208 {"PASS","FAIL","COLD","OFF"},
         "two_consec_above", "two_consec_below"}, \u2026
      ],
      "phase3": [
        {"ticker", "side", "entry1_fired", "entry1_di", "entry1_nhod",
         "entry2_fired", "entry2_di", "entry2_cross_pending"}, \u2026
      ],
      "phase4": [
        {"ticker", "side",
         "sentinel": {"a1_pnl", "a1_threshold", "a2_velocity",
                      "a2_threshold", "b_close", "b_ema9", "b_delta"},
         "titan_grip": {"stage", "anchor", "next_target",
                        "ratchet_steps"}}, \u2026
      ]
    }

Helper is defensive: every read is wrapped so a malformed cache value
drops *that* field, not the whole snapshot. Top-level failure returns
the empty skeleton (`{"phase1": {}, "phase2": [], "phase3": [],
"phase4": []}`) so the parent /api/state still serializes successfully.
"""

from __future__ import annotations

from typing import Any

import v5_10_6_snapshot as _v510


# ---------------------------------------------------------------------------
# Phase 1 \u2014 per-side permit
# ---------------------------------------------------------------------------


def _phase1_block(m) -> dict:
    """Build the Phase 1 (Section I permit) block. Reads QQQ regime
    state + AVWAP_0930 from the v5.10.6 helper and evaluates the
    permit for each side independently. Both sides are surfaced so the
    operator sees what's open / closed without inferring from a single
    pill.
    """
    out = {"long": {}, "short": {}}
    try:
        sip = _v510._section_i_permit(m)
    except Exception:
        sip = {}
    qqq_close = sip.get("qqq_5m_close")
    qqq_ema9 = sip.get("qqq_5m_ema9")
    qqq_avwap = sip.get("qqq_avwap_0930")
    qqq_last = sip.get("qqq_current_price")
    base = {
        "qqq_5m_close": qqq_close,
        "qqq_5m_ema9": qqq_ema9,
        "qqq_avwap_0930": qqq_avwap,
        "qqq_last": qqq_last,
    }
    out["long"] = dict(base, permit=bool(sip.get("long_open")))
    out["short"] = dict(base, permit=bool(sip.get("short_open")))
    return out


# ---------------------------------------------------------------------------
# Phase 2 \u2014 per-ticker gates
# ---------------------------------------------------------------------------


_VOL_GATE_MAP = {
    "PASS": "PASS",
    "FAIL": "FAIL",
    "COLDSTART": "COLD",
}


def _phase2_block(m, tickers: list[str]) -> list[dict]:
    """Build the Phase 2 (per-ticker gate) list.

    Reuses v5.10.6 helpers for the underlying Volume Bucket + Boundary
    Hold reads, then maps the resulting state names onto the
    Tiger-Sovereign-spec vocabulary the dashboard renders. When the
    runtime VOLUME_GATE_ENABLED flag is OFF, vol_gate_status is set to
    "OFF" so the operator sees the override directly.
    """
    rows: list[dict] = []
    if not tickers:
        return rows
    try:
        now_et = m._now_et()
        minute_hhmm = now_et.strftime("%H%M")
    except Exception:
        minute_hhmm = ""
    try:
        from engine import feature_flags as _ff

        vol_enabled = bool(getattr(_ff, "VOLUME_GATE_ENABLED", False))
    except Exception:
        vol_enabled = False
    try:
        if minute_hhmm:
            vol = _v510._vol_bucket_per_ticker(m, list(tickers), minute_hhmm)
        else:
            vol = {}
    except Exception:
        vol = {}
    try:
        bnd = _v510._boundary_hold_per_ticker(m, list(tickers))
    except Exception:
        bnd = {}
    for t in tickers:
        try:
            vb = vol.get(t) or {}
            vb_state = str(vb.get("state") or "COLDSTART")
            mapped = _VOL_GATE_MAP.get(vb_state, "COLD")
            if not vol_enabled:
                mapped = "OFF"
            bh = bnd.get(t) or {}
            bh_state = str(bh.get("state") or "ARMED")
            bh_side = bh.get("side")
            two_above = bool(bh_state == "SATISFIED" and bh_side == "LONG")
            two_below = bool(bh_state == "SATISFIED" and bh_side == "SHORT")
            rows.append(
                {
                    "ticker": t,
                    "vol_gate_status": mapped,
                    "two_consec_above": two_above,
                    "two_consec_below": two_below,
                }
            )
        except Exception:
            continue
    return rows


# ---------------------------------------------------------------------------
# Phase 3 \u2014 entry candidates
# ---------------------------------------------------------------------------


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _di_for(m, ticker: str) -> tuple[float | None, float | None]:
    """Defensive DI(+ / -) read. Falls back to (None, None) on any
    error (warmup not complete, helper missing, etc.).
    """
    try:
        fn = getattr(m, "tiger_di", None)
        if fn is None:
            return (None, None)
        dp, dm = fn(ticker)
        return (_safe_float(dp), _safe_float(dm))
    except Exception:
        return (None, None)


def _phase3_row(m, ticker: str, pos: dict, side: str) -> dict | None:
    try:
        dp, dm = _di_for(m, ticker)
        if side == "LONG":
            entry1_di = dp
        else:
            entry1_di = dm
        entry2_fired = bool(pos.get("v5104_entry2_fired"))
        # NHOD flag \u2014 best-effort read; v5.13 may not surface it. Use
        # explicit None when absent so the renderer can dim the cell.
        nhod_raw = pos.get("entry1_nhod_flag")
        if nhod_raw is None:
            nhod_raw = pos.get("entry1_nhod")
        if nhod_raw is None:
            entry1_nhod = None
        else:
            entry1_nhod = bool(nhod_raw)
        return {
            "ticker": ticker,
            "side": side,
            "entry1_fired": True,  # position exists \u21d2 entry 1 has fired
            "entry1_di": entry1_di,
            "entry1_nhod": entry1_nhod,
            "entry2_fired": entry2_fired,
            "entry2_di": entry1_di,  # same DI snapshot; entry 2 reuses live DI
            "entry2_cross_pending": (not entry2_fired),
        }
    except Exception:
        return None


def _phase3_block(m, longs: dict, shorts: dict) -> list[dict]:
    rows: list[dict] = []
    try:
        for tkr, pos in (longs or {}).items():
            r = _phase3_row(m, tkr, pos or {}, "LONG")
            if r is not None:
                rows.append(r)
        for tkr, pos in (shorts or {}).items():
            r = _phase3_row(m, tkr, pos or {}, "SHORT")
            if r is not None:
                rows.append(r)
    except Exception:
        pass
    return rows


# ---------------------------------------------------------------------------
# Phase 4 \u2014 active management (Sentinel A/B/C + Titan Grip)
# ---------------------------------------------------------------------------


_SENTINEL_A1_THRESHOLD = -500.0
_SENTINEL_A2_THRESHOLD = -0.01


def _qqq_5m_state(m) -> tuple[float | None, float | None]:
    """Reuse v5.10.6 helper \u2014 Phase 4 Alarm B uses the same QQQ 5m
    close + 9-EMA the regime shield tracks.
    """
    try:
        return _v510._qqq_regime_state(m)
    except Exception:
        return (None, None)


def _sentinel_block(m, ticker: str, pos: dict, side: str, prices: dict) -> dict:
    """Build the Phase 4 sentinel sub-block for one open position.

    A1 = unrealized P&L in dollars (fires at <= -$500).
    A2 = velocity over the last sample window in $/sec (fires <= -0.01).
    B  = QQQ 5m close vs 9-EMA (Alarm B uses the per-side rule).

    All values are best-effort reads. Missing state \u2192 None so the
    renderer dims the row instead of crashing.
    """
    out = {
        "a1_pnl": None,
        "a1_threshold": _SENTINEL_A1_THRESHOLD,
        "a2_velocity": None,
        "a2_threshold": _SENTINEL_A2_THRESHOLD,
        "b_close": None,
        "b_ema9": None,
        "b_delta": None,
    }
    try:
        entry = _safe_float(pos.get("entry_price")) or 0.0
        shares = int(pos.get("shares", 0) or 0)
        mark = _safe_float(prices.get(ticker)) or entry
        if side == "LONG":
            unreal = (mark - entry) * shares
        else:
            unreal = (entry - mark) * shares
        out["a1_pnl"] = float(unreal)
    except Exception:
        pass
    try:
        # Velocity is recorded by broker.positions when the sentinel
        # runs; expose it if it lives on the position dict.
        v = pos.get("a2_velocity")
        if v is None:
            v = pos.get("sentinel_a2_velocity")
        out["a2_velocity"] = _safe_float(v)
    except Exception:
        pass
    try:
        qqq_close, qqq_ema9 = _qqq_5m_state(m)
        out["b_close"] = qqq_close
        out["b_ema9"] = qqq_ema9
        if qqq_close is not None and qqq_ema9 is not None:
            out["b_delta"] = float(qqq_close) - float(qqq_ema9)
    except Exception:
        pass
    return out


def _titan_grip_block(pos: dict) -> dict:
    """Build the Phase 4 Titan Grip sub-block for one open position.

    Reads the live `titan_grip_state` sidecar dataclass (broker.positions
    stores it under `pos["titan_grip_state"]`). All fields fall back to
    None when the state has not been instantiated yet.
    """
    out = {
        "stage": None,
        "anchor": None,
        "next_target": None,
        "ratchet_steps": None,
    }
    try:
        st = pos.get("titan_grip_state")
        if st is None:
            return out
        out["stage"] = int(getattr(st, "stage", 0) or 0)
        out["anchor"] = _safe_float(getattr(st, "current_stop_anchor", None))
        # Next target depends on stage: stage 0 \u2192 stage1 harvest target,
        # stage 1 \u2192 stage3 harvest target, stage 2 \u2192 ratchet anchor.
        stg = out["stage"]
        if stg == 0:
            out["next_target"] = _safe_float(getattr(st, "stage1_harvest_target", None))
        elif stg == 1:
            out["next_target"] = _safe_float(getattr(st, "stage3_harvest_target", None))
        else:
            out["next_target"] = out["anchor"]
        # Ratchet step count: number of steps the anchor has advanced
        # past the original Stage-1 stop. Best-effort \u2014 surfaces None
        # when the state lacks an anchor or original baseline.
        try:
            anchor = out["anchor"]
            if anchor is not None and stg >= 1:
                direction = getattr(st, "direction", None)
                or_high = _safe_float(getattr(st, "or_high", None))
                or_low = _safe_float(getattr(st, "or_low", None))
                if direction == "LONG" and or_high:
                    step = or_high * 0.0040
                    if step > 0:
                        out["ratchet_steps"] = int(round((anchor - or_high) / step))
                elif direction == "SHORT" and or_low:
                    step = or_low * 0.0040
                    if step > 0:
                        out["ratchet_steps"] = int(round((or_low - anchor) / step))
        except Exception:
            pass
    except Exception:
        pass
    return out


def _phase4_block(m, longs: dict, shorts: dict, prices: dict) -> list[dict]:
    rows: list[dict] = []
    try:
        for tkr, pos in (longs or {}).items():
            try:
                p = pos or {}
                rows.append(
                    {
                        "ticker": tkr,
                        "side": "LONG",
                        "sentinel": _sentinel_block(m, tkr, p, "LONG", prices),
                        "titan_grip": _titan_grip_block(p),
                    }
                )
            except Exception:
                continue
        for tkr, pos in (shorts or {}).items():
            try:
                p = pos or {}
                rows.append(
                    {
                        "ticker": tkr,
                        "side": "SHORT",
                        "sentinel": _sentinel_block(m, tkr, p, "SHORT", prices),
                        "titan_grip": _titan_grip_block(p),
                    }
                )
            except Exception:
                continue
    except Exception:
        pass
    return rows


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------


def build_tiger_sovereign_snapshot(
    m,
    tickers: list[str],
    longs: dict,
    shorts: dict,
    prices: dict,
) -> dict:
    """Top-level Tiger Sovereign snapshot. Never raises; on internal
    error returns the empty skeleton so the parent /api/state still
    serializes successfully.
    """
    out: dict = {"phase1": {}, "phase2": [], "phase3": [], "phase4": []}
    try:
        out["phase1"] = _phase1_block(m)
    except Exception:
        out["phase1"] = {}
    try:
        out["phase2"] = _phase2_block(m, list(tickers or []))
    except Exception:
        out["phase2"] = []
    try:
        out["phase3"] = _phase3_block(m, longs or {}, shorts or {})
    except Exception:
        out["phase3"] = []
    try:
        out["phase4"] = _phase4_block(m, longs or {}, shorts or {}, prices or {})
    except Exception:
        out["phase4"] = []
    return out


__all__ = ["build_tiger_sovereign_snapshot"]
