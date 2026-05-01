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

import logging
from typing import Any

import v5_10_6_snapshot as _v510
from engine import sma_stack as _sma_stack_engine

_logger = logging.getLogger("trade_genius")


# ---------------------------------------------------------------------------
# v6.0.1 -- daily-close cache for the SMA stack panel.
# ---------------------------------------------------------------------------
# The SMA stack needs the most recent ~210 daily closes per ticker so we
# can compute SMA(200). Daily closes only change once per RTH session,
# so we cache (closes, fetched_at_iso_date) per ticker and only refetch
# when the calendar date rolls. Failures are negative-cached for
# CACHE_FAIL_TTL_SECONDS so a flaky network read does not hammer the
# upstream every snapshot tick (the snapshot fires every few seconds).
#
# Wire-up: ``trade_genius._daily_closes_for_sma`` is the canonical
# fetcher; we call it via getattr so this module stays importable in
# tests that do not need trade_genius's heavy runtime.
_DAILY_CLOSES_CACHE: dict[str, dict] = {}
_CACHE_FAIL_TTL_SECONDS = 60.0  # negative-cache window on fetch failure
_NEEDED_CLOSES = 210            # 200 + small buffer for safety


# ---------------------------------------------------------------------------
# EXPECTED_KEYS contract
# ---------------------------------------------------------------------------
# Callers and tests rely on this dict to validate the output schema.
# Keys map to the set of field names present in each sub-block.
# Do NOT remove or rename existing keys -- add only.

EXPECTED_KEYS: dict[str, set] = {
    "sentinel": {
        # Legacy keys -- kept for one-release backwards compatibility.
        "a1_pnl",
        "a1_threshold",
        "a2_velocity",
        "a2_threshold",
        "b_close",
        "b_ema9",
        "b_delta",
        # vAA-1 alarm sub-dicts.
        "a_loss",
        "a_flash",
        "b_trend_death",
        "c_velocity_ratchet",
        "d_hvp_lock",
        "e_divergence_trap",
        # v5.30.0 \u2014 Alarm F chandelier trail state (read from
        # pos["trail_state"]). Always present for open positions; idle
        # default for the no-position render path.
        "f_chandelier",
    }
}


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


# ---------------------------------------------------------------------------
# v5.21.0 -- daily SMA stack helper
# ---------------------------------------------------------------------------


def _compute_sma_stack_safe(ticker: str) -> dict | None:
    """v6.0.1 -- daily SMA stack restored.

    The v5.30.1 stub returned ``None`` after v5.26.0 deleted the old
    engine helpers in the Stage 1 spec-strict cut. Per the v6.0.1
    request, we now fetch the most recent ~210 daily closes via
    ``trade_genius._daily_closes_for_sma`` (Alpaca historical with a
    once-per-RTH-day cache) and feed them through ``engine.sma_stack``
    to produce the full payload the dashboard renders.

    Returns ``None`` if the fetcher is unavailable, the call fails, or
    we got fewer than 12 closes back. The frontend null-guard then
    renders "data not available" \u2014 the same fallback the legacy
    stub gave \u2014 so the row never crashes during warmup.
    """
    if not ticker:
        return None
    sym = str(ticker).strip().upper()
    if not sym:
        return None

    closes = _get_cached_daily_closes(sym)
    if not closes:
        return None
    try:
        return _sma_stack_engine.compute_sma_stack(closes)
    except Exception as e:  # belt-and-braces -- never crash the snapshot
        _logger.debug("sma_stack compute failed for %s: %s", sym, e)
        return None


def _get_cached_daily_closes(ticker: str) -> list[float] | None:
    """Return cached daily closes for ``ticker`` (oldest-first, most
    recent last). Refreshes once per RTH calendar day; on fetch failure
    falls back to whatever was cached last and negative-caches the
    failure for ``_CACHE_FAIL_TTL_SECONDS`` so we do not hammer the
    upstream from every snapshot tick.
    """
    import time
    from datetime import datetime, timezone

    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_mono = time.monotonic()
    cached = _DAILY_CLOSES_CACHE.get(ticker)
    if cached:
        # If today's date matches the last successful fetch, reuse it.
        if cached.get("date_iso") == today_iso and cached.get("closes"):
            return cached.get("closes")
        # Negative-cache window from the most recent failed attempt.
        last_fail = cached.get("last_fail_mono")
        if last_fail is not None and (now_mono - last_fail) < _CACHE_FAIL_TTL_SECONDS:
            # Inside the cooldown window -- reuse the prior closes if any.
            return cached.get("closes") or None

    fetched: list[float] | None = None
    try:
        import trade_genius as _tg  # heavy module -- import lazily

        fetcher = getattr(_tg, "_daily_closes_for_sma", None)
        if callable(fetcher):
            fetched = fetcher(ticker, _NEEDED_CLOSES)
    except Exception as e:
        _logger.debug("daily-closes fetch failed for %s: %s", ticker, e)
        fetched = None

    if fetched and len(fetched) >= 12:
        _DAILY_CLOSES_CACHE[ticker] = {
            "date_iso": today_iso,
            "closes": list(fetched),
            "last_fail_mono": None,
        }
        return list(fetched)

    # Fetch failed -- mark negative-cache window and return whatever we
    # had previously (may be None).
    prior = (cached or {}).get("closes")
    _DAILY_CLOSES_CACHE[ticker] = {
        "date_iso": (cached or {}).get("date_iso"),
        "closes": prior,
        "last_fail_mono": now_mono,
    }
    return prior


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
            # v5.21.0 -- daily SMA stack (informational panel, not a gate).
            # Wrapped in try/except so any network or data failure degrades
            # gracefully to None without crashing the snapshot.
            sma_stack = _compute_sma_stack_safe(t)
            rows.append(
                {
                    "ticker": t,
                    "vol_gate_status": mapped,
                    "two_consec_above": two_above,
                    "two_consec_below": two_below,
                    "sma_stack": sma_stack,
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

# vAA-1 alarm D threshold (spec SENT-D: ratio < 0.75).
_SENTINEL_D_HVP_FRACTION = 0.75

# vAA-1 alarm C/E ratchet offset: 0.25% protective stop.
_SENTINEL_RATCHET_PCT = 0.0025


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

    Returns the 7 legacy keys (a1_pnl, a1_threshold, a2_velocity,
    a2_threshold, b_close, b_ema9, b_delta) for backwards compatibility
    PLUS the 6 vAA-1 alarm sub-dicts (a_loss, a_flash, b_trend_death,
    c_velocity_ratchet, d_hvp_lock, e_divergence_trap).

    All values are best-effort reads. Missing state -> None so the
    renderer dims the row instead of crashing. Read-only: no engine
    exit logic is called.
    """
    # -----------------------------------------------------------------
    # Shared computation: unrealized PnL
    # -----------------------------------------------------------------
    unreal: float | None = None
    try:
        entry = _safe_float(pos.get("entry_price")) or 0.0
        shares = int(pos.get("shares", 0) or 0)
        mark = _safe_float(prices.get(ticker)) or entry
        if side == "LONG":
            unreal = float((mark - entry) * shares)
        else:
            unreal = float((entry - mark) * shares)
    except Exception:
        pass

    # -----------------------------------------------------------------
    # Legacy keys (kept unchanged -- callers rely on these)
    # -----------------------------------------------------------------
    out: dict = {
        "a1_pnl": unreal,
        "a1_threshold": _SENTINEL_A1_THRESHOLD,
        "a2_velocity": None,
        "a2_threshold": _SENTINEL_A2_THRESHOLD,
        "b_close": None,
        "b_ema9": None,
        "b_delta": None,
    }
    try:
        # Velocity is recorded by broker.positions when the sentinel
        # runs; expose it if it lives on the position dict.
        v = pos.get("a2_velocity")
        if v is None:
            v = pos.get("sentinel_a2_velocity")
        out["a2_velocity"] = _safe_float(v)
    except Exception:
        pass
    qqq_close: float | None = None
    qqq_ema9: float | None = None
    try:
        qqq_close, qqq_ema9 = _qqq_5m_state(m)
        out["b_close"] = qqq_close
        out["b_ema9"] = qqq_ema9
        if qqq_close is not None and qqq_ema9 is not None:
            out["b_delta"] = float(qqq_close) - float(qqq_ema9)
    except Exception:
        pass

    # -----------------------------------------------------------------
    # vAA-1 Alarm A_LOSS
    # Spec SENT-A_LOSS: unrealized_pnl <= -$500 -> MARKET EXIT.
    # -----------------------------------------------------------------
    a_loss_armed = unreal is not None
    a_loss_triggered = bool(
        a_loss_armed and unreal is not None and unreal <= _SENTINEL_A1_THRESHOLD
    )
    out["a_loss"] = {
        "pnl": unreal,
        "threshold": _SENTINEL_A1_THRESHOLD,
        "armed": a_loss_armed,
        "triggered": a_loss_triggered,
    }

    # -----------------------------------------------------------------
    # vAA-1 Alarm A_FLASH
    # Spec SENT-A_FLASH: (pnl_now - pnl_60s_ago) / position_value <= -0.01.
    # Source: pos["pnl_history"] deque of (ts, pnl) samples.
    # -----------------------------------------------------------------
    a_flash_velocity: float | None = None
    a_flash_armed = False
    a_flash_triggered = False
    try:
        pnl_history = pos.get("pnl_history")
        now_ts = _safe_float(pos.get("last_sentinel_ts"))
        entry_v = _safe_float(pos.get("entry_price")) or 0.0
        shares_v = int(pos.get("shares", 0) or 0)
        position_value = entry_v * shares_v
        if (
            pnl_history is not None
            and now_ts is not None
            and position_value > 0
            and unreal is not None
        ):
            target_ts = now_ts - 60.0
            prior_pnl: float | None = None
            for ts_s, pnl_s in pnl_history:
                if ts_s <= target_ts:
                    prior_pnl = pnl_s
                else:
                    break
            if prior_pnl is not None:
                delta = unreal - prior_pnl
                velocity_pct = delta / position_value
                a_flash_velocity = velocity_pct
                a_flash_armed = True
                # Spec uses strict less-than for flash (< -0.01).
                a_flash_triggered = velocity_pct < _SENTINEL_A2_THRESHOLD
    except Exception:
        pass
    out["a_flash"] = {
        "velocity_pct": a_flash_velocity,
        "threshold_pct": _SENTINEL_A2_THRESHOLD,
        "window_sec": 60,
        "armed": a_flash_armed,
        "triggered": a_flash_triggered,
    }

    # -----------------------------------------------------------------
    # vAA-1 Alarm B_TREND_DEATH
    # Spec SENT-B: 5m bar close crosses 9-EMA, per-side.
    # Gate: only triggered on a confirmed 5m bar close (on_5m_close).
    # -----------------------------------------------------------------
    b_side_note = "LONG: close < ema9 fires" if side == "LONG" else "SHORT: close > ema9 fires"
    b_armed = bool(side in ("LONG", "SHORT"))
    b_triggered = False
    try:
        if b_armed and qqq_close is not None and qqq_ema9 is not None:
            on_5m = bool(pos.get("on_5m_close", False))
            if on_5m:
                if side == "LONG":
                    b_triggered = qqq_close < qqq_ema9
                else:
                    b_triggered = qqq_close > qqq_ema9
            # When on_5m_close is absent/False, armed stays True but
            # triggered stays False -- dashboard shows "watching".
    except Exception:
        pass
    out["b_trend_death"] = {
        "close": qqq_close,
        "ema9": qqq_ema9,
        "delta": (
            float(qqq_close) - float(qqq_ema9)
            if qqq_close is not None and qqq_ema9 is not None
            else None
        ),
        "armed": b_armed,
        "triggered": b_triggered,
        "side_aware_note": b_side_note,
    }

    # -----------------------------------------------------------------
    # vAA-1 Alarm C_VELOCITY_RATCHET
    # Spec SENT-C: three strictly-decreasing 1m ADX values.
    # Source: pos["adx_1m_history"] -- list or deque of recent values.
    # -----------------------------------------------------------------
    c_adx_window: list[float | None] = [None, None, None]
    c_monotone = False
    c_stop: float | None = None
    c_armed = False
    c_triggered = False
    try:
        adx_hist = pos.get("adx_1m_history")
        if adx_hist is not None:
            hist_list = list(adx_hist)
            if len(hist_list) >= 3:
                h0 = _safe_float(hist_list[-3])
                h1 = _safe_float(hist_list[-2])
                h2 = _safe_float(hist_list[-1])
                c_adx_window = [h0, h1, h2]
                if h0 is not None and h1 is not None and h2 is not None:
                    c_armed = True
                    c_monotone = bool(h0 > h1 > h2)
                    c_triggered = c_monotone
                    if c_triggered:
                        current_price_c = _safe_float(prices.get(ticker))
                        if current_price_c is not None:
                            if side == "LONG":
                                c_stop = round(current_price_c * (1.0 - _SENTINEL_RATCHET_PCT), 4)
                            else:
                                c_stop = round(current_price_c * (1.0 + _SENTINEL_RATCHET_PCT), 4)
    except Exception:
        pass
    out["c_velocity_ratchet"] = {
        "adx_window": c_adx_window,
        "monotone_decreasing": c_monotone,
        "stop_price": c_stop,
        "armed": c_armed,
        "triggered": c_triggered,
    }

    # -----------------------------------------------------------------
    # vAA-1 Alarm D_HVP_LOCK
    # Spec SENT-D: current_5m_adx < 0.75 * trade_hvp.peak.
    # Sources: pos["trade_hvp"] (TradeHVP object) and
    #          pos["adx_5m_current"] or prices dict for 5m ADX.
    # -----------------------------------------------------------------
    d_trade_hvp_val: float | None = None
    d_current_5m: float | None = None
    d_ratio: float | None = None
    d_armed = False
    d_triggered = False
    try:
        trade_hvp_obj = pos.get("trade_hvp")
        if trade_hvp_obj is not None:
            try:
                d_trade_hvp_val = float(trade_hvp_obj.peak)
                d_armed = True
            except (RuntimeError, AttributeError, TypeError):
                d_trade_hvp_val = None
                d_armed = False
        # Fallback: plain float stored under trade_hvp key.
        if d_trade_hvp_val is None:
            raw_hvp = _safe_float(pos.get("trade_hvp"))
            if raw_hvp is not None:
                d_trade_hvp_val = raw_hvp
                d_armed = True
        if d_armed:
            d_current_5m = _safe_float(pos.get("adx_5m_current"))
            if d_current_5m is None:
                # Try prices dict as fallback channel.
                d_current_5m = _safe_float(prices.get("__adx_5m__"))
            if d_trade_hvp_val is not None and d_current_5m is not None and d_trade_hvp_val > 0:
                d_ratio = d_current_5m / d_trade_hvp_val
                d_triggered = d_ratio < _SENTINEL_D_HVP_FRACTION
    except Exception:
        pass
    out["d_hvp_lock"] = {
        "trade_hvp": d_trade_hvp_val,
        "current_5m_adx": d_current_5m,
        "ratio": d_ratio,
        "threshold_ratio": _SENTINEL_D_HVP_FRACTION,
        "armed": d_armed,
        "triggered": d_triggered,
    }

    # -----------------------------------------------------------------
    # vAA-1 Alarm E_DIVERGENCE_TRAP
    # Spec SENT-E: current price makes a new extreme while RSI diverges.
    # Sources: pos["stored_peak_price"], pos["stored_peak_rsi"],
    #          prices dict for current price, pos["current_rsi_15"].
    # -----------------------------------------------------------------
    e_peak_price: float | None = None
    e_peak_rsi: float | None = None
    e_cur_price: float | None = None
    e_cur_rsi: float | None = None
    e_is_extreme = False
    e_rsi_div = False
    e_pre_strike: int | None = None
    e_post_stop: float | None = None
    e_armed = False
    e_triggered = False
    try:
        e_peak_price = _safe_float(pos.get("stored_peak_price"))
        e_peak_rsi = _safe_float(pos.get("stored_peak_rsi"))
        e_cur_price = _safe_float(prices.get(ticker))
        e_cur_rsi = _safe_float(pos.get("current_rsi_15") or pos.get("rsi_15"))
        e_armed = (
            e_peak_price is not None
            and e_peak_rsi is not None
            and e_cur_price is not None
            and e_cur_rsi is not None
        )
        if e_armed:
            if side == "LONG":
                e_is_extreme = e_cur_price > e_peak_price  # type: ignore[operator]
                e_rsi_div = e_cur_rsi < e_peak_rsi  # type: ignore[operator]
            else:
                e_is_extreme = e_cur_price < e_peak_price  # type: ignore[operator]
                e_rsi_div = e_cur_rsi > e_peak_rsi  # type: ignore[operator]
            e_triggered = e_is_extreme and e_rsi_div
            if e_triggered:
                # pre_blocked_for_strike: next strike that would be
                # blocked is 2 (if strike_num < 2) or 3 (if == 2).
                strike_num = pos.get("strike_num")
                if strike_num is not None:
                    try:
                        sn = int(strike_num)
                        if sn < 2:
                            e_pre_strike = 2
                        elif sn == 2:
                            e_pre_strike = 3
                    except (TypeError, ValueError):
                        pass
                # post_ratchet_stop: protective stop price.
                if side == "LONG":
                    e_post_stop = round(e_cur_price * (1.0 - _SENTINEL_RATCHET_PCT), 4)
                else:
                    e_post_stop = round(e_cur_price * (1.0 + _SENTINEL_RATCHET_PCT), 4)
    except Exception:
        pass
    out["e_divergence_trap"] = {
        "stored_peak_price": e_peak_price,
        "stored_peak_rsi": e_peak_rsi,
        "current_price": e_cur_price,
        "current_rsi_15": e_cur_rsi,
        "is_extreme": e_is_extreme,
        "rsi_diverging": e_rsi_div,
        "pre_blocked_for_strike": e_pre_strike,
        "post_ratchet_stop": e_post_stop,
        "armed": e_armed,
        "triggered": e_triggered,
    }

    # -----------------------------------------------------------------
    # v5.30.0 \u2014 Alarm F (Hybrid Chandelier Trailing Stop) status block.
    # Sourced from pos["trail_state"] (engine.alarm_f_trail.TrailState).
    # Stage codes: 0 INACTIVE, 1 BREAKEVEN, 2 CHANDELIER_WIDE,
    # 3 CHANDELIER_TIGHT. Armed once stage >= 1 (BE installed); the
    # "triggered" flag is left False because Alarm F never closes a
    # position by itself \u2014 the broker stop-cross does, and the closed-
    # bar chandelier-cross full-exit fires through evaluate_sentinel
    # rather than this read-only snapshot. Missing trail_state -> idle.
    # -----------------------------------------------------------------
    f_stage: int = 0
    f_peak_close: float | None = None
    f_proposed_stop: float | None = None
    f_bars_seen: int = 0
    f_armed = False
    try:
        ts = pos.get("trail_state") if pos else None
        if ts is not None:
            f_stage = int(getattr(ts, "stage", 0) or 0)
            f_peak_close = _safe_float(getattr(ts, "peak_close", None))
            f_proposed_stop = _safe_float(getattr(ts, "last_proposed_stop", None))
            f_bars_seen = int(getattr(ts, "bars_seen", 0) or 0)
            f_armed = f_stage >= 1
    except Exception:
        pass
    f_stage_name = {
        0: "INACTIVE",
        1: "BREAKEVEN",
        2: "CHANDELIER_WIDE",
        3: "CHANDELIER_TIGHT",
    }.get(f_stage, "INACTIVE")
    out["f_chandelier"] = {
        "stage": f_stage,
        "stage_name": f_stage_name,
        "peak_close": f_peak_close,
        "proposed_stop": f_proposed_stop,
        "bars_seen": f_bars_seen,
        "armed": f_armed,
        "triggered": False,
    }

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


__all__ = ["build_tiger_sovereign_snapshot", "EXPECTED_KEYS"]
