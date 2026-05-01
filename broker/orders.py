"""broker.orders \u2014 order execution: check_breakout, execute_breakout, close_breakout, paper_shares_for.

Extracted from trade_genius.py in v5.11.2 PR 2.
"""

from __future__ import annotations

import sys as _sys
import time as _time_orders
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from broker.order_types import order_type_for_reason

# v5.26.0 \u2014 broker.stops module deleted. R-2 -$500 hard stop is
# computed inline in execute_breakout per Tiger Sovereign v15.0
# \u00a7Risk Rails.

# v5.13.6 \u2014 per-position lifecycle log. Best-effort import so a missing
# module never blocks trading; the wrappers below silently no-op when the
# logger is unavailable.
try:
    import lifecycle_logger as _lifecycle  # noqa: F401
except Exception:  # pragma: no cover - only triggered by deploy regressions
    _lifecycle = None


def _lifecycle_logger():
    if _lifecycle is None:
        return None
    try:
        tg = _tg()
        ver = getattr(tg, "BOT_VERSION", "") if tg else ""
        return _lifecycle.get_default_logger(bot_version=ver)
    except Exception:
        return None


# v5.11.2 \u2014 prod runs `python trade_genius.py`, so trade_genius is
# registered in sys.modules as `__main__`, NOT as `trade_genius`.
# Mirror the alias trick used by paper_state / telegram_ui to make
# both names point at the same already-loaded module object.
if "trade_genius" not in _sys.modules and "__main__" in _sys.modules:
    _main = _sys.modules["__main__"]
    if getattr(_main, "BOT_NAME", None) == "TradeGenius":
        _sys.modules["trade_genius"] = _main


def _tg():
    """Live trade_genius module (handles __main__ vs imported cases)."""
    return _sys.modules.get("trade_genius") or _sys.modules.get("__main__")


# v5.15.0 vAA-1 \u2014 spec rules ORDER-LIMIT-PRICE-LONG / -SHORT.
# Strike entries cross the spread by 0.10% (LONG) / -0.10% (SHORT)
# to favour fills while bounding slippage. The Sentinel STOP MARKET
# defensive exits remain MARKET orders \u2014 only entries are LIMIT.
ALARM_STRIKE_LIMIT_LONG_FACTOR: float = 1.001  # ask * 1.001
ALARM_STRIKE_LIMIT_SHORT_FACTOR: float = 0.999  # bid * 0.999


def compute_strike_limit_price(side: str, ask: float, bid: float) -> float:
    """Strike-entry LIMIT price per vAA-1 ORDER-LIMIT-PRICE-LONG/SHORT.

    LONG  -> ``ask * 1.001``
    SHORT -> ``bid * 0.999``

    Side is matched case-insensitively against ``"LONG"``/``"SHORT"``.
    The result is NOT rounded \u2014 the spec test asserts the raw float
    via ``math.isclose(..., abs_tol=1e-6)``; rounding to 2 decimals
    fails on SHORT (``99.95 * 0.999 = 99.85005`` vs ``99.85``).
    """
    s = (side or "").strip().upper()
    if s == "LONG":
        return float(ask) * ALARM_STRIKE_LIMIT_LONG_FACTOR
    if s == "SHORT":
        return float(bid) * ALARM_STRIKE_LIMIT_SHORT_FACTOR
    raise ValueError(f"compute_strike_limit_price: unknown side {side!r}")


def check_breakout(ticker, side):
    """Side-parameterized entry gate.

    Returns (True, bars_dict) if all entry conditions for `side` are
    met, else (False, None).
    """
    tg = _tg()
    cfg = tg.CONFIGS[side]
    or_dict = getattr(tg, cfg.or_attr)
    positions_dict = getattr(tg, cfg.positions_attr)
    daily_count = getattr(tg, cfg.daily_count_attr)

    if tg._trading_halted:
        return False, None
    if tg._scan_paused:
        return False, None

    # v5.31.0 \u2014 forensic decision-stack latency timer. Captures the
    # wall-time cost of the gate stack from entry through emission.
    _decision_start = _time_orders.monotonic()

    now_et = tg._now_et()
    today = now_et.strftime("%Y-%m-%d")

    # v15.0 SPEC Entry Window: 09:36:00 to 15:44:59 EST.
    # ORH/ORL freeze at 09:35:59; earliest valid 2x 1m close completes
    # on the 09:37 candle close, but we open the gate at 09:36:00 (one
    # bar before earliest fire) to align with the spec wording.
    market_open = now_et.replace(hour=9, minute=36, second=0, microsecond=0)
    if now_et < market_open:
        return False, None
    # SHARED-CUTOFF: no new entries at/after 15:44:59 ET (matches
    # engine.timing.NEW_POSITION_CUTOFF_ET, also enforced in execute_breakout).
    cutoff_time = now_et.replace(hour=15, minute=44, second=59, microsecond=0)
    if now_et >= cutoff_time:
        return False, None

    # Reset daily entry counts if new day
    if getattr(tg, cfg.daily_date_attr) != today:
        daily_count.clear()
        setattr(tg, cfg.daily_date_attr, today)

    # v5.7.0 \u2014 sovereign daily-loss kill switch. Once latched, every
    # entry path returns SKIP daily_loss_limit_hit until the next
    # session boundary. Existing open positions exit on their own
    # normal exits; this gate only blocks NEW entries.
    if tg._v570_kill_switch_active():
        tg._v561_log_skip(
            ticker=ticker,
            reason="daily_loss_limit_hit",
            ts_utc=tg._utc_now_iso(),
            gate_state=None,
        )
        return False, None

    # OR data available
    if ticker not in or_dict or ticker not in tg.pdc:
        return False, None

    # v15.0 SPEC STRIKE-CAP-3: Maximum 3 Strikes per ticker per day
    # (long + short combined). Enforced via _v570_strike_count which is
    # incremented on each successful entry; the strike_entry_allowed
    # helper also enforces the sequential (flat-gate) requirement.
    # `positions_dict` is keyed by ticker (paper_positions/short_positions);
    # we project it into the (ticker:side) shape expected by the flat-gate.
    side_label_for_cap = "LONG" if cfg.side.is_long else "SHORT"
    _flat_gate_view: dict = {}
    try:
        for _t, _p in (tg.positions or {}).items():
            _flat_gate_view[f"{str(_t).upper()}:LONG"] = _p
        for _t, _p in (tg.short_positions or {}).items():
            _flat_gate_view[f"{str(_t).upper()}:SHORT"] = _p
    except Exception:
        _flat_gate_view = {}
    if not tg.strike_entry_allowed(ticker, side_label_for_cap, _flat_gate_view):
        tg._v561_log_skip(
            ticker=ticker,
            reason="strike_cap_3_or_flat_gate",
            ts_utc=tg._utc_now_iso(),
            gate_state=None,
        )
        return False, None
    # Belt-and-suspenders: legacy daily_count cap aligned to spec value (3).
    if daily_count.get(ticker, 0) >= 3:
        return False, None

    # Already in a position on this side for this ticker (paper).
    # v5.10.4 \u2014 if Entry 1 is active and Entry 2 has not yet fired,
    # evaluate Section III Entry 2 (1m DI cross > 30 + fresh NHOD/NLOD
    # past Entry 1's HWM, after Entry 1's ts). Always returns
    # (False, None) from check_breakout; Entry 2 fills are placed in
    # _v5104_maybe_fire_entry_2 directly so we don't recycle the
    # Entry-1 execute_breakout path.
    if ticker in positions_dict:
        try:
            tg._v5104_maybe_fire_entry_2(ticker, side, positions_dict[ticker])
        except Exception as _e2:
            tg.logger.warning("[V5100-ENTRY] entry_2 eval error %s: %s", ticker, _e2)
        return False, None

    # v5.10.1 \u2014 Unlimited Hunting (Section VI): no 15-min cooldown,
    # no per-ticker $50 loss cap. Re-entry on the next NHOD/NLOD +
    # DMI alignment is the spec. The global -$1,500 daily circuit
    # breaker (handled via _v570_kill_switch_active above) plus the
    # Section IV per-trade Sovereign Brake (-$500 unrealized) are the
    # only loss-side gates.

    # Fetch current bar (Yahoo)
    bars = tg.fetch_1min_bars(ticker)
    if not bars:
        return False, None

    current_price = bars["current_price"]
    # v4.1.1: a 0 or negative current_price (Yahoo has shipped 0.0 quotes
    # on thinly traded names during pre-market extensions) would bypass
    # every downstream sanity gate because those gates fail-open when
    # fed 0/None. Reject here.
    if not current_price or current_price <= 0:
        return False, None
    closes = [c for c in bars["closes"] if c is not None]
    last_close = closes[-1] if closes else current_price

    # FMP primary quote \u2014 override price and PDC if available
    fmp_q = tg.get_fmp_quote(ticker)
    if fmp_q:
        fmp_price = fmp_q.get("price")
        if fmp_price and fmp_price > 0:
            current_price = fmp_price
            last_close = fmp_price
        fmp_pdc = fmp_q.get("previousClose")
        if fmp_pdc and fmp_pdc > 0:
            tg.pdc[ticker] = fmp_pdc

    # v5.26.0 \u2014 OR sanity check (_or_price_sane / OR_STALE_THRESHOLD)
    # deleted: not part of Tiger Sovereign v15.0. OR freeze at 09:35:59
    # is authoritative.

    or_edge_val = or_dict[ticker]
    pdc_val_e = tg.pdc[ticker]
    # 2-bar OR breakout/breakdown confirmation (Tiger 2.0).
    if cfg.side.is_long:
        price_break = tg._tiger_two_bar_long(closes, or_edge_val)
    else:
        price_break = tg._tiger_two_bar_short(closes, or_edge_val)
    polarity_ok = current_price > pdc_val_e if cfg.side.is_long else current_price < pdc_val_e

    volumes = bars.get("volumes", [])
    vol_pct = None
    vol_ok = False
    vol_ready_flag = True
    entry_bar_vol = 0.0
    avg_vol = 0.0
    if len(volumes) >= 5:
        valid_vols = [v for v in volumes[:-1] if v is not None and v > 0]
        avg_vol = sum(valid_vols) / len(valid_vols) if valid_vols else 0
        entry_bar_vol, vol_ready = tg._entry_bar_volume(volumes)
        vol_ready_flag = vol_ready
        if vol_ready and avg_vol > 0:
            vol_pct = (entry_bar_vol / avg_vol) * 100.0
            vol_ok = vol_pct >= 150.0

    # v5.26.0 \u2014 TIGER_V2_REQUIRE_VOL legacy 1.5x-avg vol filter
    # deleted. Tiger Sovereign v15.0 BL-3 / BU-3 Volume Gate is BYPASSED
    # as of 2026-04-30 per spec amendment.

    # ------------------------------------------------------------------
    # v5.10.1 \u2014 Eye-of-the-Tiger authoritative gates (Sections I + II + III).
    # Replaces the v5.0\u2013v5.9 G1/G3/G4 + V570 expansion + extension +
    # stop-cap stack. The Section IV (Sovereign Brake / Velocity Fuse)
    # is enforced inside manage_positions / manage_short_positions.
    # ------------------------------------------------------------------
    qqq_bars = tg.fetch_1min_bars("QQQ")
    if not qqq_bars:
        return False, None
    qqq_last = qqq_bars.get("current_price")
    qqq_avwap = tg._opening_avwap("QQQ")
    qqq_5m_close = tg._QQQ_REGIME.last_close
    qqq_ema9 = tg._QQQ_REGIME.ema9
    or_high_val = tg.or_high.get(ticker)
    or_low_val = tg.or_low.get(ticker)
    side_label = "LONG" if cfg.side.is_long else "SHORT"

    # Section I \u2014 Global Permit
    permit_res = tg.eot_glue.evaluate_section_i(
        side_label,
        qqq_5m_close,
        qqq_ema9,
        qqq_last,
        qqq_avwap,
    )
    if not permit_res.get("open"):
        # v5.31.5 \u2014 per-stock local-weather override. When the global
        # QQQ permit is closed for this side, check whether the ticker's
        # OWN price action (5m close past EMA9 OR last past opening AVWAP)
        # plus 1m DI confirmation is decisively pointing the other way.
        # If so, the gate opens locally and the entry path proceeds.
        # Either rejection (or both) collapses to the same skip log,
        # tagged so we can audit how often the override fires in prod.
        try:
            from engine.local_weather import evaluate_local_override
            _local_reg = (tg._TICKER_REGIME or {}).get(ticker.upper()) or {}
            _local_di = tg.v5_di_1m_5m(ticker) or {}
            _override = evaluate_local_override(
                side_label,
                _local_reg.get("last_close_5m"),
                _local_reg.get("ema9_5m"),
                _local_reg.get("last"),
                _local_reg.get("avwap"),
                _local_di.get("di_plus_1m"),
                _local_di.get("di_minus_1m"),
            )
        except Exception as _e:
            tg.logger.warning(
                "[LOCAL_OVERRIDE] eval error %s/%s: %s",
                ticker,
                side_label,
                _e,
            )
            _override = {"open": False, "reason": "eval_error"}
        if _override.get("open"):
            tg.logger.info(
                "[LOCAL_OVERRIDE] ticker=%s side=%s OPEN reason=%s qqq_reason=%s",
                ticker,
                side_label,
                _override.get("reason"),
                permit_res.get("reason"),
            )
            # Fall through to the rest of the gate stack \u2014 the
            # ticker's own structure has earned the entry chance.
        else:
            tg.logger.info(
                "[LOCAL_OVERRIDE] ticker=%s side=%s REJECT qqq_reason=%s local_reason=%s",
                ticker,
                side_label,
                permit_res.get("reason"),
                _override.get("reason"),
            )
            tg._v561_log_skip(
                ticker=ticker,
                reason="V5100_PERMIT:%s" % permit_res.get("reason", "closed"),
                ts_utc=tg._utc_now_iso(),
                gate_state=None,
            )
            return False, None

    # v5.26.0 \u2014 Volume Bucket gate (Section II.1) deleted. BL-3 / BU-3
    # are BYPASSED per spec amendment 2026-04-30.

    # v15.0 SPEC Permission Ladder:
    #   Strike 1 \u2014 2x consecutive 1m close above ORH (long) / below ORL (short).
    #   Strike 2 & 3 \u2014 2x consecutive 1m close above NHOD (long) / below NLOD (short).
    # The session HOD/LOD tracker (_v570_session_hod / _v570_session_lod) holds
    # the running session extremes. Fall back to ORH/ORL if no session extreme
    # is recorded yet (very early in the session).
    _next_strike_num = tg._v570_strike_count(ticker) + 1
    if _next_strike_num >= 2:
        _sess_hod = tg._v570_session_hod.get(ticker.upper())
        _sess_lod = tg._v570_session_lod.get(ticker.upper())
        boundary_high = _sess_hod if _sess_hod is not None else or_high_val
        boundary_low = _sess_lod if _sess_lod is not None else or_low_val
    else:
        boundary_high = or_high_val
        boundary_low = or_low_val

    # v5.26.2 \u2014 forensic decision-record helper. Built once with the
    # context that is invariant across the gate-stack body (current_price,
    # ORH/ORL, PDC, QQQ snapshot). Mutated locals (DI, ADX, RSI, prev
    # session HOD/LOD, boundary holds) are read off the enclosing frame
    # via getlocals at emit time. The helper is fully wrapped \u2014 a
    # forensic write can NEVER raise into the trading path.
    _decision_qqq_last = qqq_last
    _decision_qqq_avwap = qqq_avwap
    _decision_qqq_5m = qqq_5m_close
    _decision_qqq_ema9 = qqq_ema9
    _decision_or_high = or_high_val
    _decision_or_low = or_low_val
    _decision_pdc = pdc_val_e

    # v5.31.0 \u2014 quote snapshot at decision time (live bid/ask). Used
    # by the forensic decision record to score fill quality and capture
    # spread context for backtest replay. Failure-tolerant: returns
    # (None, None) when the data client is unreachable.
    _decision_bid = None
    _decision_ask = None
    _decision_spread_bps = None
    try:
        _b, _a = tg._v512_quote_snapshot(ticker)
        _decision_bid = _b
        _decision_ask = _a
        if _b and _a and _b > 0 and _a > 0 and (_a + _b) > 0:
            _mid = (_a + _b) / 2.0
            if _mid > 0:
                _decision_spread_bps = round((_a - _b) / _mid * 10000.0, 2)
    except Exception:
        pass

    def _emit_decision(decision_str: str) -> None:
        try:
            from forensic_capture import (
                write_decision_record as _write_decision,
            )

            _frame_locals = locals()
            # Walk up to the enclosing function frame so we read the live
            # values of di_5m / di_1m / adx_5m / boundary_res / nhod_res
            # at the moment of the decision rather than helper-frame None.
            import sys as _sys_d

            _f = _sys_d._getframe(1)
            _outer = _f.f_locals if _f is not None else {}

            def _g(name):
                return _outer.get(name) if name in _outer else None

            _b_pri = _g("boundary_res") or {}
            _b_s1 = _g("nhod_res") or {}
            _sess_hod = None
            _sess_lod = None
            try:
                _sess_hod = tg._v570_session_hod.get(ticker.upper())
                _sess_lod = tg._v570_session_lod.get(ticker.upper())
            except Exception:
                pass
            _write_decision(
                ticker=ticker,
                side=side_label,
                ts_utc=tg._utc_now_iso(),
                strike_num=_g("_next_strike_num"),
                decision=decision_str,
                current_price=current_price,
                last_close=last_close,
                or_high=_decision_or_high,
                or_low=_decision_or_low,
                pdc=_decision_pdc,
                qqq_last=_decision_qqq_last,
                qqq_avwap=_decision_qqq_avwap,
                qqq_5m_close=_decision_qqq_5m,
                qqq_ema9=_decision_qqq_ema9,
                sess_hod=_sess_hod,
                sess_lod=_sess_lod,
                prev_sess_hod=_g("_prev_hod"),
                prev_sess_lod=_g("_prev_lod"),
                di_1m=_g("di_1m"),
                di_5m=_g("di_5m"),
                adx_1m=(
                    (_g("adx_streams") or {}).get("adx_1m")
                    if _g("adx_streams") is not None
                    else None
                ),
                adx_5m=_g("adx_5m"),
                rsi_15=_g("_rsi15_e"),
                boundary_hold_or=(_b_pri.get("hold") if isinstance(_b_pri, dict) else None),
                boundary_hold_nhod_nlod=(_b_s1.get("hold") if isinstance(_b_s1, dict) else None),
                is_extreme_print=_g("is_extreme_print"),
                permit_open=True,
                alarm_e_blocked=_g("_e_blocked"),
                sentinel_state=None,
                # v5.31.0 \u2014 quote snapshot + decision-stack latency
                entry_bid=_decision_bid,
                entry_ask=_decision_ask,
                spread_bps=_decision_spread_bps,
                decision_latency_ms=round((_time_orders.monotonic() - _decision_start) * 1000.0, 2),
            )
        except Exception:
            pass

    # Section II.2 \u2014 Boundary Hold (Entry-1 only). Stateless: the
    # last two closed 1m closes vs the boundary edge (ORH/ORL or NHOD/NLOD).
    boundary_res = tg.eot_glue.evaluate_boundary_hold_gate(
        ticker,
        side_label,
        boundary_high,
        boundary_low,
    )

    # v5.26.2 \u2014 forensic capture of the primary boundary gate result.
    try:
        from forensic_capture import write_boundary_record as _write_boundary_pri

        _closes_pri = list(tg.eot_glue._last_1m_closes.get(ticker, []))
        _label_pri = "NHOD_NLOD" if _next_strike_num >= 2 else "ORH_ORL"
        _write_boundary_pri(
            ticker=ticker,
            side=side_label,
            ts_utc=tg._utc_now_iso(),
            boundary_label=_label_pri,
            boundary_high=boundary_high,
            boundary_low=boundary_low,
            last_close=(_closes_pri[-1] if _closes_pri else None),
            prior_close=(_closes_pri[-2] if len(_closes_pri) >= 2 else None),
            consecutive_outside=boundary_res.get("consecutive_outside"),
            hold=boundary_res.get("hold"),
            reason=boundary_res.get("reason"),
            strike_num=_next_strike_num,
        )
    except Exception:
        pass

    # v5.26.0 \u2014 [V510-CAND] gate-audit log line deleted (Volume Gate
    # bypass leaves only the 2-candle hold for this telemetry).

    if not boundary_res.get("hold"):
        tg._v561_log_skip(
            ticker=ticker,
            reason="V5100_BOUNDARY:%s" % boundary_res.get("reason"),
            ts_utc=tg._utc_now_iso(),
            gate_state=None,
        )
        _emit_decision("SKIP:V5100_BOUNDARY:%s" % boundary_res.get("reason"))
        return False, None

    # Section III Trend Confirmation \u2014 5m DI > 25, 1m DI > 25, NHOD/NLOD.
    di_streams = tg.v5_di_1m_5m(ticker)
    if cfg.side.is_long:
        di_5m = di_streams.get("di_plus_5m")
        di_1m = di_streams.get("di_plus_1m")
    else:
        di_5m = di_streams.get("di_minus_5m")
        di_1m = di_streams.get("di_minus_1m")

    # v15.0 SPEC Phase 3 Momentum Check: 5m ADX > 20 AND Alarm E = FALSE.
    # The ADX > 20 condition is a hard pre-entry gate (was missing pre-v5.20.0).
    try:
        adx_streams = tg.v5_adx_1m_5m(ticker)
        adx_5m = adx_streams.get("adx_5m") if adx_streams else None
    except Exception:
        adx_5m = None
    if adx_5m is None or float(adx_5m) <= 20.0:
        tg._v561_log_skip(
            ticker=ticker,
            reason="V15_MOMENTUM_ADX_5M:%s"
            % (("%.2f" % float(adx_5m)) if adx_5m is not None else "none"),
            ts_utc=tg._utc_now_iso(),
            gate_state=None,
        )
        _emit_decision(
            "SKIP:V15_MOMENTUM_ADX_5M:%s"
            % (("%.2f" % float(adx_5m)) if adx_5m is not None else "none")
        )
        return False, None

    # NHOD / NLOD: derive from session HOD/LOD vs current_price (strict).
    _prev_hod, _prev_lod, hod_break, lod_break = tg._v570_update_session_hod_lod(
        ticker,
        current_price,
    )
    is_extreme_print = bool(hod_break if cfg.side.is_long else lod_break)

    # v5.31.3 \u2014 Strike-1 NHOD/NLOD-on-close gate REMOVED.
    # Until v5.31.2 Strike 1 had to clear two gates: (1) the
    # Section II.2 boundary hold (2x consecutive 1m close vs ORH/ORL)
    # and (2) a NHOD/NLOD-on-close confirmation (latest 1m close past
    # the prior closed-bar session HOD/LOD). The NHOD/NLOD layer was
    # introduced in v5.26.2 as additional confirmation but in practice
    # filtered out clean Strike-1 setups whose break of OR happened
    # *before* the day's running session high; those entries would
    # have been profitable. Removed here so Strike 1 again only
    # requires the OR boundary hold (consistent with the rest of the
    # v15 spec, which scopes NHOD/NLOD to the post-entry sentinel).
    # is_extreme_print and _prev_hod/_prev_lod above are still used
    # by the forensic decision record (is_nhod_or_nlod, prev_sess_hod
    # / prev_sess_lod fields), so the upstream call to
    # _v570_update_session_hod_lod is intentionally retained.

    # v15.0 SPEC Alarm E pre-entry filter:
    #   Spec \u00a71.2: "If a price prints a new extreme but RSI(15) is
    #   diverging (lower for Longs, higher for Shorts), the bot is prohibited
    #   from opening new Strike 2 or Strike 3 positions."
    # Strike 1 is unaffected by the pre-filter; the post-entry sentinel covers it.
    if _next_strike_num >= 2:
        try:
            from engine.sentinel import check_alarm_e_pre as _alarm_e_pre
            from broker.positions import get_divergence_memory as _get_dm

            _closes_1m_e = (bars or {}).get("closes") or []
            _rsi15_e = (
                tg._compute_rsi(_closes_1m_e, period=15)
                if _closes_1m_e and hasattr(tg, "_compute_rsi")
                else None
            )
            if _rsi15_e is not None:
                _e_blocked = _alarm_e_pre(
                    memory=_get_dm(),
                    ticker=ticker,
                    side=side_label,
                    current_price=float(current_price),
                    current_rsi_15=float(_rsi15_e),
                    strike_num=_next_strike_num,
                )
                if _e_blocked:
                    tg._v561_log_skip(
                        ticker=ticker,
                        reason="V15_ALARM_E_PRE_STRIKE%d" % _next_strike_num,
                        ts_utc=tg._utc_now_iso(),
                        gate_state=None,
                    )
                    _emit_decision("SKIP:V15_ALARM_E_PRE_STRIKE%d" % _next_strike_num)
                    return False, None
        except Exception as _alarm_e_err:
            tg.logger.warning(
                "[V15-ALARM-E] %s pre-filter eval error: %s",
                ticker,
                _alarm_e_err,
            )

    entry1_decision = tg.eot_glue.evaluate_entry_1_decision(
        ticker,
        side_label,
        permit_open=True,
        volume_bucket_ok=True,
        boundary_hold_ok=True,
        di_5m=di_5m,
        di_1m=di_1m,
        is_nhod_or_nlod=is_extreme_print,
    )
    if not entry1_decision.get("fire"):
        tg._v561_log_skip(
            ticker=ticker,
            reason="V5100_ENTRY1:%s" % entry1_decision.get("reason", ""),
            ts_utc=tg._utc_now_iso(),
            gate_state=None,
        )
        _emit_decision("SKIP:V5100_ENTRY1:%s" % entry1_decision.get("reason", ""))
        return False, None

    # All Eye-of-the-Tiger gates pass. Bars dict carries current_price
    # forward to execute_breakout.
    try:
        tg.logger.info(
            "[V5100-ENTRY] ticker=%s side=%s entry_num=1 di_5m=%s di_1m=%s fill_price=%.4f",
            ticker,
            side_label,
            ("%.2f" % di_5m) if di_5m is not None else "None",
            ("%.2f" % di_1m) if di_1m is not None else "None",
            current_price,
        )
    except Exception:
        pass
    _emit_decision("ENTER")
    return True, bars


def paper_shares_for(price: float) -> int:
    """Dollar-sized paper order for Entry-1: floor(
    PAPER_DOLLARS_PER_ENTRY * ENTRY_1_SIZE_PCT / price), min 1.
    Returns 0 only when price <= 0 (invalid).

    v3.4.45 \u2014 paper now sizes by notional like RH does, scaled to the
    $100k paper book (default $10k/entry vs RH's $1.5k/$25k). This
    fixes the old flat 10-share behavior that made $400 NVDA cost 80x
    more risk per entry than $5 QBTS.

    v5.13.2 Track A \u2014 spec L-P3-S5 / S-P3-S5 says Entry-1 = 50% of the
    full target. ENTRY_1_SIZE_PCT (eye_of_tiger) is now wired in here;
    Entry-2 in broker/positions.py tops the position up to ~100% of
    PAPER_DOLLARS_PER_ENTRY notional.
    """
    if price <= 0:
        return 0
    from eye_of_tiger import ENTRY_1_SIZE_PCT

    dollars = _tg().PAPER_DOLLARS_PER_ENTRY * ENTRY_1_SIZE_PCT
    return max(1, int(dollars // price))


def execute_breakout(ticker, current_price, side):
    """Side-parameterized entry executor.

    v4.9.0 \u2014 unified body. The legacy long/short twins were deleted;
    this single body is parameterized by SideConfig. The synthetic
    harness goldens enforce byte-equal Telegram + paper_log output
    against the v4.8.2 baseline.
    """
    tg = _tg()
    cfg = tg.CONFIGS[side]
    positions_dict = getattr(tg, cfg.positions_attr)
    daily_count = getattr(tg, cfg.daily_count_attr)

    # Daily loss limit (shared between long/short).
    if not tg._check_daily_loss_limit(ticker):
        return

    # v5.13.0 PR-5 SHARED-CUTOFF: block NEW entries at/after 15:44:59 ET.
    if not tg._check_new_position_cutoff(ticker):
        return

    now_et = tg._now_et()
    limit_price = round(current_price + cfg.limit_offset, 2)
    or_dict = getattr(tg, cfg.or_attr)

    # v5.31.4 \u2014 percent-of-entry stop. Spec change from v5.26.0:
    # the stop is no longer reverse-derived from the R-2 dollar rail.
    # Operator request "stop should not be sized by the number of
    # shares" \u2014 a $500 fixed dollar rail divided by share count
    # produced wildly different percent stops on cheap vs expensive
    # tickers ($5 stocks got tight stops, $200+ tickers got 5%+ stops).
    # New rule: STOP_PCT_OF_ENTRY (default 0.005 = 0.5%) is symmetric
    # for long and short. Share sizing remains notional ($10k/entry,
    # 50% Entry-1 starter, doubled by Entry-2 to FULL).
    #
    # The R-2 dollar-rail backstop in engine.sentinel.evaluate_sentinel
    # remains unchanged \u2014 a runaway move that beats the price stop
    # in absolute $ terms still trips R-2 as the deeper safety net.
    from eye_of_tiger import STOP_PCT_OF_ENTRY

    _pct = float(STOP_PCT_OF_ENTRY)
    if cfg.side.is_long:
        stop_price = round(current_price * (1.0 - _pct), 2)
    else:
        stop_price = round(current_price * (1.0 + _pct), 2)
    _stop_capped = False
    _stop_baseline = stop_price

    # Dollar-sized paper entry; shares scale with price.
    # ``paper_shares_for`` returns the legacy 50% Entry-1 starter. The v15.0
    # spec sizing tier (\u00a72/\u00a73) is decided by ``evaluate_strike_sizing``
    # against the live 1m DI value on the side-correct polarity:
    #   FULL     (1m DI > 30)         \u2192 100% in one fill (= 2 \u00d7 starter)
    #   SCALED_A (1m DI in [25, 30])  \u2192 50% starter; Entry-2 may top up
    #   WAIT                          \u2192 don't enter (defensive: check_breakout's
    #                                    L-P3-AUTH gate already covers this)
    starter_shares = paper_shares_for(current_price)
    shares = starter_shares
    _v15_size_label = "FULL"  # legacy default for telemetry
    _v15_size_reason = ""
    try:
        from eye_of_tiger import evaluate_strike_sizing as _v15_eval_sizing

        _di_streams = tg.v5_di_1m_5m(ticker) if hasattr(tg, "v5_di_1m_5m") else {}
        if cfg.side.is_long:
            _v15_di_5m = _di_streams.get("di_plus_5m")
            _v15_di_1m = _di_streams.get("di_plus_1m")
        else:
            _v15_di_5m = _di_streams.get("di_minus_5m")
            _v15_di_1m = _di_streams.get("di_minus_1m")
        _v15_decision = _v15_eval_sizing(
            side="LONG" if cfg.side.is_long else "SHORT",
            di_5m=_v15_di_5m,
            di_1m=_v15_di_1m,
            is_fresh_extreme=False,
            intended_shares=int(starter_shares) * 2,
            held_shares_this_strike=0,
            alarm_e_blocked=False,
        )
        _v15_size_label = _v15_decision.size_label
        _v15_size_reason = _v15_decision.reason
        # Map the spec tier back to the legacy two-leg sizing model:
        #   FULL       \u2192 fill 100% now (2 \u00d7 starter); Entry-2 must NOT top up
        #   SCALED_A   \u2192 fill 50% starter (existing behavior); Entry-2 may top up
        #   SCALED_B   \u2192 not reachable here (held=0 path); fall through
        #   WAIT       \u2192 abort entry; check_breakout should have caught this
        if _v15_size_label == "FULL":
            shares = int(_v15_decision.shares_to_buy)
        elif _v15_size_label == "SCALED_A":
            shares = int(_v15_decision.shares_to_buy)
        elif _v15_size_label == "WAIT":
            tg.logger.info(
                "[V15-SIZING] %s side=%s WAIT (defensive abort): %s",
                ticker,
                "LONG" if cfg.side.is_long else "SHORT",
                _v15_decision.reason,
            )
            return
        tg.logger.info(
            "[V15-SIZING] %s side=%s tier=%s shares=%d (1m DI=%s, 5m DI=%s)",
            ticker,
            "LONG" if cfg.side.is_long else "SHORT",
            _v15_size_label,
            int(shares),
            ("%.2f" % _v15_di_1m) if _v15_di_1m is not None else "None",
            ("%.2f" % _v15_di_5m) if _v15_di_5m is not None else "None",
        )
    except Exception as _v15_err:
        # Defensive: a sizing-helper exception MUST NOT block the trade.
        # Fall through to the legacy ``starter_shares`` (50% Entry-1).
        tg.logger.warning("[V15-SIZING] %s eval error: %s", ticker, _v15_err)
        shares = starter_shares
        _v15_size_label = "FULL"
        _v15_size_reason = "sizing eval error \u2014 fell back to legacy starter"
    notional = current_price * shares
    if shares <= 0:
        if cfg.side.is_long:
            tg.logger.warning("[paper] skip %s \u2014 invalid price $%.2f", ticker, current_price)
        else:
            tg.logger.warning(
                "[paper] skip short %s \u2014 invalid price $%.2f", ticker, current_price
            )
        return

    # Long entry needs cash to buy; short entry credits cash on open.
    if cfg.side.is_long and notional > tg.paper_cash:
        tg.logger.info(
            "[paper] skip %s \u2014 insufficient cash (need $%.2f, have $%.2f)",
            ticker,
            notional,
            tg.paper_cash,
        )
        return

    entry_num = daily_count.get(ticker, 0) + 1
    now_str = tg._now_cdt().strftime("%H:%M:%S")
    now_hhmm = tg._now_cdt().strftime("%H:%M CDT")
    now_date = now_et.strftime("%Y-%m-%d")

    _entry_ts_utc = tg._utc_now_iso()
    _entry_id = tg._v561_compose_entry_id(ticker, _entry_ts_utc)
    # v5.7.0 \u2014 record this entry against the per-ticker per-side
    # strike counter and stash strike_num on the position so the
    # paired [TRADE_CLOSED] can echo it back.
    _v570_side_label = "LONG" if cfg.side.is_long else "SHORT"
    try:
        _v570_strike_num = tg._v570_record_entry(ticker, _v570_side_label)
    except Exception:
        _v570_strike_num = 1
    # v5.9.0 \u2014 record the strike-1 compass for [V572-ABORT] mid-strike
    # flip detection. Only stamped on the FIRST strike of a session.
    if _v570_strike_num == 1:
        try:
            _, _, _entry_compass = tg._v590_compass_for_gate()
            tg._v590_record_entry_compass(
                ticker,
                _v570_side_label,
                _entry_compass,
            )
        except Exception:
            pass
    pos = {
        "entry_price": current_price,
        "shares": shares,
        "stop": stop_price,
        "initial_stop": stop_price,
        "trail_active": False,
        cfg.trail_peak_attr: current_price,
        "entry_count": entry_num,
        "entry_time": now_str,
        "entry_ts_utc": _entry_ts_utc,
        "entry_id": _entry_id,
        "strike_num": _v570_strike_num,
        "date": now_date,
        "pdc": tg.pdc.get(ticker, 0),
        # v5.10.4 \u2014 Eye-of-the-Tiger Section III Entry 2 scaling state.
        # Stamped on Entry 1 fill so check_breakout can later detect a
        # 1m DI-30 cross + fresh NHOD/NLOD past Entry 1's HWM and fire
        # a 50%-sized scale-in. Cleared on close_breakout via the pos
        # pop. v5104_entry1_hwm starts at the entry price; subsequent
        # ticks update it on each scan cycle in check_breakout.
        #
        # v5.20.0 v15.0-sizing wire-in: when ``evaluate_strike_sizing``
        # returned FULL (1m DI > 30), we already filled 100% of the
        # intended notional in this single fill, so Entry-2 must NOT
        # try to top up. We pre-set ``v5104_entry2_fired = True`` to
        # short-circuit ``_v5104_maybe_fire_entry_2``. SCALED_A leaves
        # the flag False (legacy 50%-starter behavior) so Entry-2 can
        # add the remaining 50% under the spec scale-in conditions.
        "v5104_entry1_price": float(current_price),
        "v5104_entry1_shares": int(shares),
        "v5104_entry1_hwm": float(current_price),
        "v5104_entry1_ts_utc": _entry_ts_utc,
        "v5104_entry2_fired": (_v15_size_label == "FULL"),
        # v5.20.0: forensic stamp of the v15.0 sizing tier on the
        # position so [TRADE_CLOSED] / lifecycle log can echo it.
        "v15_size_label": str(_v15_size_label),
        "v15_size_reason": str(_v15_size_reason),
    }
    if cfg.side.is_short:
        pos["side"] = "SHORT"
        pos["trail_stop"] = None
    positions_dict[ticker] = pos
    daily_count[ticker] = entry_num

    # v5.15.1 vAA-1 \u2014 SENT-D HVP lock fill hook. Install (or re-seed)
    # the per-(ticker, side) TradeHVP at Strike open with the live
    # 5m ADX so subsequent sentinel ticks can detect a >25% decay
    # from peak and exit. ADX warmup may not be complete yet (boot
    # window, fresh ticker); ensure_trade_hvp tolerates None and
    # the safety-floor branch in check_alarm_d holds the alarm
    # dormant until the seed exceeds 25.
    try:
        from broker.positions import ensure_trade_hvp as _ensure_trade_hvp

        _adx_streams = tg.v5_adx_1m_5m(ticker) if hasattr(tg, "v5_adx_1m_5m") else {}
        _ensure_trade_hvp(ticker, _v570_side_label, _adx_streams.get("adx_5m"))
    except Exception as _e:
        try:
            tg.logger.debug("[SENT-D] HVP fill-hook %s: %s", ticker, _e)
        except Exception:
            pass
    # v5.13.6 \u2014 lifecycle log: capture Phase 1-4 evals + write ENTRY_DECISION,
    # then ORDER_SUBMIT + ORDER_FILL. Best-effort: any exception swallowed,
    # trading path must not be blocked by the lifecycle logger.
    try:
        ll = _lifecycle_logger()
        if ll is not None:
            try:
                import v5_13_2_snapshot as _snap

                ph1 = _snap._phase1_block(tg)
            except Exception:
                ph1 = {}
            try:
                ph2 = _snap._phase2_block(tg, [ticker])
            except Exception:
                ph2 = []
            try:
                ph3 = _snap._phase3_block(
                    tg,
                    {ticker: pos} if cfg.side.is_long else {},
                    {ticker: pos} if cfg.side.is_short else {},
                )
            except Exception:
                ph3 = []
            entry_payload = {
                "entry_price": float(current_price),
                "limit_price": float(limit_price),
                "shares": int(shares),
                "stop_price": float(stop_price),
                "stop_capped": bool(_stop_capped),
                "entry_num": int(entry_num),
                "strike_num": int(_v570_strike_num),
                "entry_id": _entry_id,
                "phase1": ph1,
                "phase2": ph2,
                "phase3": ph3,
                "or_high": float(or_dict.get(ticker, 0) or 0),
                "pdc": float(tg.pdc.get(ticker, 0) or 0),
            }
            position_id = ll.open_position(
                ticker=ticker,
                side=_v570_side_label,
                entry_ts_utc=_entry_ts_utc,
                payload=entry_payload,
                reason_text=f"{_v570_side_label} entry #{entry_num} fired",
            )
            pos["lifecycle_position_id"] = position_id
            ll.log_event(
                position_id,
                "ORDER_SUBMIT",
                {
                    "side": _v570_side_label,
                    "qty": int(shares),
                    "limit_price": float(limit_price),
                    "order_type": "limit",
                },
                ticker=ticker,
                side=_v570_side_label,
                entry_ts_utc=_entry_ts_utc,
            )
            ll.log_event(
                position_id,
                "ORDER_FILL",
                {
                    "side": _v570_side_label,
                    "qty": int(shares),
                    "fill_price": float(current_price),
                    "notional": float(notional),
                },
                reason_text="paper-fill (assumed marketable)",
                ticker=ticker,
                side=_v570_side_label,
                entry_ts_utc=_entry_ts_utc,
            )
    except Exception as _e:
        try:
            tg.logger.debug("[lifecycle] entry hook %s: %s", ticker, _e)
        except Exception:
            pass

    # v5.6.1 D4 \u2014 [ENTRY] line with entry_id for replay pairing.
    try:
        tg._v561_log_entry(
            ticker=ticker,
            side=_v570_side_label,
            entry_id=_entry_id,
            entry_ts_utc=_entry_ts_utc,
            entry_price=float(current_price),
            qty=int(shares),
            strike_num=int(_v570_strike_num),
        )
    except Exception as _e:
        tg.logger.warning("[V561-ENTRY] emit error %s: %s", ticker, _e)

    # Paper accounting: long debits, short credits.
    tg.paper_cash += cfg.entry_cash_delta(shares, current_price)

    # Long BUYs are appended to paper_trades / paper_all_trades; short
    # opens are intentionally NOT appended (short_trade_history is the
    # source of truth for shorts and avoids double-counting on /trades).
    if cfg.side.is_long:
        trade = {
            "action": "BUY",
            "ticker": ticker,
            "price": current_price,
            "limit_price": limit_price,
            "shares": shares,
            "cost": notional,
            "stop": stop_price,
            "entry_num": entry_num,
            "time": now_hhmm,
            "date": now_date,
        }
        tg.paper_trades.append(trade)
        tg.paper_all_trades.append(trade)

    tg.paper_log(
        "%s %s %d @ $%.2f (limit $%.2f) stop=$%.2f entry#%d"
        % (
            cfg.paper_log_entry_verb,
            ticker,
            shares,
            current_price,
            limit_price,
            stop_price,
            entry_num,
        )
    )

    # v5.1.2 \u2014 emit forensic entry snapshot. Strictly additive: this
    # logger.info call goes nowhere observable to the synthetic
    # harness (recorder only captures send_telegram / paper_log /
    # _emit_signal / trade_log_append / save_paper_state, so the
    # byte-equal goldens stay green).
    try:
        bid_v, ask_v = tg._v512_quote_snapshot(ticker)
        equity_v = tg.paper_cash + sum(
            float(p.get("entry_price", 0.0)) * int(p.get("shares", 0))
            for p in tg.positions.values()
        )
        open_pos = len(tg.positions) + len(tg.short_positions)
        # Exposure as % of equity (sum of long notional only \u2014
        # shorts net to credit). Guard against div-by-zero.
        long_notional = sum(
            float(p.get("entry_price", 0.0)) * int(p.get("shares", 0))
            for p in tg.positions.values()
        )
        expo_pct = (long_notional / equity_v * 100.0) if equity_v > 0 else 0.0
        # Drawdown is rough \u2014 we don't track high-water-mark in
        # paper_state so report 0 unless caller wants more later.
        dd_pct = 0.0
        tg._v512_log_entry_extension(
            ticker,
            bid=bid_v,
            ask=ask_v,
            cash=round(tg.paper_cash, 2),
            equity=round(equity_v, 2),
            open_positions=open_pos,
            total_exposure_pct=round(expo_pct, 4),
            current_drawdown_pct=dd_pct,
        )
    except Exception as e:
        tg.logger.warning("[V510-ENTRY] snapshot error %s: %s", ticker, e)

    or_edge_e = or_dict.get(ticker, 0)
    SEP_E = "\u2500" * 34
    stop_label = cfg.stop_capped_label if _stop_capped else cfg.stop_baseline_label

    # v5.13.10 \u2014 Telegram entry signal lines now mirror the ACTUAL
    # gates the entry path enforces (Tiger Sovereign Section I + boundary_hold)
    # rather than the legacy dual-PDC vocabulary. Reads the same inputs the
    # dashboard pills use post-v5.13.9: _QQQ_REGIME.last_close / ema9 plus
    # the 09:30 AVWAP for QQQ. Wrapped in try/except so unit tests and
    # smoke modes that don't seed the regime never block the entry message.
    try:
        _qqq_close = getattr(tg._QQQ_REGIME, "last_close", None)
        _qqq_ema9 = getattr(tg._QQQ_REGIME, "ema9", None)
    except Exception:
        _qqq_close, _qqq_ema9 = None, None
    try:
        _qqq_avwap = tg._opening_avwap("QQQ")
    except Exception:
        _qqq_avwap = None

    def _gate_chk(ok):
        # ok=True \u2192 \u2713, ok=False \u2192 \u2717, ok=None \u2192 \u2014
        if ok is True:
            return "\u2713"
        if ok is False:
            return "\u2717"
        return "\u2014"

    if cfg.side.is_long:
        _ema9_ok = _qqq_close is not None and _qqq_ema9 is not None and _qqq_close > _qqq_ema9
        _avwap_ok = _qqq_close is not None and _qqq_avwap is not None and _qqq_close > _qqq_avwap
        sig_lines = "Signal : ORB Breakout \u2191\n"
        sig_lines += "  1m close > OR High \u2713\n"
        sig_lines += "  2nd 1m close > OR High \u2713\n"
        if _qqq_close is not None and _qqq_ema9 is not None:
            sig_lines += "  QQQ 5m close > 9-EMA %s  (%.2f vs %.2f)\n" % (
                _gate_chk(_ema9_ok),
                _qqq_close,
                _qqq_ema9,
            )
        else:
            sig_lines += "  QQQ 5m close > 9-EMA \u2014\n"
        if _qqq_close is not None and _qqq_avwap is not None:
            sig_lines += "  QQQ 5m close > 09:30 AVWAP %s  (%.2f vs %.2f)\n" % (
                _gate_chk(_avwap_ok),
                _qqq_close,
                _qqq_avwap,
            )
        else:
            sig_lines += "  QQQ 5m close > 09:30 AVWAP \u2014\n"
        msg = (
            "\U0001f4c8 LONG ENTRY %s  #%d\n"
            "%s\n"
            "Price  : $%.2f  (limit $%.2f)\n"
            "Shares : %d   Cost: $%s\n"
            "Stop   : $%.2f  (%s)\n"
            "OR High: $%.2f\n"
            "%s"
            "Time   : %s\n"
            "%s"
        ) % (
            ticker,
            entry_num,
            SEP_E,
            current_price,
            limit_price,
            shares,
            format(notional, ",.2f"),
            stop_price,
            stop_label,
            or_edge_e,
            sig_lines,
            now_hhmm,
            SEP_E,
        )
    else:
        _ema9_ok = _qqq_close is not None and _qqq_ema9 is not None and _qqq_close < _qqq_ema9
        _avwap_ok = _qqq_close is not None and _qqq_avwap is not None and _qqq_close < _qqq_avwap
        sig_lines = "Signal   : Wounded Buffalo \u2193\n"
        sig_lines += "  1m close < OR Low \u2713\n"
        sig_lines += "  2nd 1m close < OR Low \u2713\n"
        if _qqq_close is not None and _qqq_ema9 is not None:
            sig_lines += "  QQQ 5m close < 9-EMA %s  (%.2f vs %.2f)\n" % (
                _gate_chk(_ema9_ok),
                _qqq_close,
                _qqq_ema9,
            )
        else:
            sig_lines += "  QQQ 5m close < 9-EMA \u2014\n"
        if _qqq_close is not None and _qqq_avwap is not None:
            sig_lines += "  QQQ 5m close < 09:30 AVWAP %s  (%.2f vs %.2f)\n" % (
                _gate_chk(_avwap_ok),
                _qqq_close,
                _qqq_avwap,
            )
        else:
            sig_lines += "  QQQ 5m close < 09:30 AVWAP \u2014\n"
        msg = (
            "\U0001fa78 SHORT ENTRY #%d\n"
            "%s\n"
            "Ticker   : %s\n"
            "Entry    : $%.2f (limit)\n"
            "Shares   : %d   Proceeds: $%s\n"
            "Stop     : $%.2f (%s)\n"
            "OR Low   : $%.2f\n"
            "%s"
            "Time     : %s\n"
            "%s"
        ) % (
            entry_num,
            SEP_E,
            ticker,
            current_price,
            shares,
            format(notional, ",.2f"),
            stop_price,
            stop_label,
            or_edge_e,
            sig_lines,
            now_hhmm,
            SEP_E,
        )
    tg.send_telegram(msg)

    tg.save_paper_state()

    tg._emit_signal(
        {
            "kind": cfg.entry_signal_kind,
            "ticker": ticker,
            "price": float(current_price),
            "reason": cfg.entry_signal_reason,
            "timestamp_utc": tg._utc_now_iso(),
            "main_shares": int(shares),
        }
    )


def close_breakout(ticker, price, side, reason="STOP", suppress_signal=False):
    """Side-parameterized close.

    v4.9.0 \u2014 unified body. The legacy long/short twins were deleted;
    this single body is parameterized by SideConfig. Synthetic-harness
    goldens enforce byte-equal Telegram + paper_log + trade_log output
    against the v4.8.2 baseline.

    v5.13.7 \u2014 the resolved order type from ``order_type_for_reason``
    is now threaded into the ``_emit_signal`` payload and the lifecycle
    log's ``ORDER_SUBMIT`` event so a future Alpaca live bridge submits
    LIMIT / STOP_MARKET / MARKET per spec instead of always defaulting
    to MARKET.

    v5.24.0 \u2014 ``suppress_signal=True`` skips the ``_emit_signal``
    fan-out so the EOD loop can flatten the paper book per-ticker (for
    cash/trade_log bookkeeping) without spamming executors with a
    duplicate ``EXIT_LONG`` event on top of the single ``EOD_CLOSE_ALL``
    that ``eod_close`` already fires up front. Lifecycle log + Telegram
    + paper_state still update normally.
    """
    tg = _tg()
    cfg = tg.CONFIGS[side]
    positions_dict = getattr(tg, cfg.positions_attr)
    history_list = getattr(tg, cfg.trade_history_attr)

    if ticker not in positions_dict:
        return

    resolved_order_type = order_type_for_reason(reason)

    tg._last_exit_time[ticker] = tg.datetime.now(timezone.utc)

    pos = positions_dict.pop(ticker)
    # v5.10.5 \u2014 Clear v5.10 phase state + 5m-bucket debounce on close
    # so a fresh re-entry starts in Phase A with a clean slate.
    try:
        _eot_side = tg.eot.SIDE_LONG if cfg.side.is_long else tg.eot.SIDE_SHORT
        tg.eot_glue.clear_position_state(ticker, _eot_side)
        tg._engine_clear_phase_bucket(ticker, _eot_side)
    except Exception:
        pass

    # v5.15.1 vAA-1 \u2014 drop the per-(ticker, side) TradeHVP and ADX
    # window when the position closes so a re-entry starts with a
    # fresh peak. Idempotent: silently no-ops on missing keys.
    try:
        from broker.positions import clear_trade_hvp as _clear_trade_hvp

        _close_side_label = "LONG" if cfg.side.is_long else "SHORT"
        _clear_trade_hvp(ticker, _close_side_label)
    except Exception:
        pass
    entry_price = pos["entry_price"]
    shares = pos["shares"]
    pnl_val = cfg.realized_pnl(entry_price, price, shares)
    if entry_price:
        if cfg.side.is_long:
            pnl_pct = (price - entry_price) / entry_price * 100
        else:
            pnl_pct = (entry_price - price) / entry_price * 100
    else:
        pnl_pct = 0
    now_et = tg._now_et()
    now_hhmm = tg._now_cdt().strftime("%H:%M CDT")
    now_date = now_et.strftime("%Y-%m-%d")

    entry_time_str = pos.get("entry_time", "")
    entry_hhmm = tg._to_cdt_hhmm(entry_time_str) if entry_time_str else ""

    # Paper accounting: long credits sale proceeds, short debits cover cost.
    notional = price * shares  # "proceeds" for long, "cover_total" for short
    tg.paper_cash += cfg.close_cash_delta(shares, price)

    # Long SELLs are appended to paper_trades / paper_all_trades; short
    # COVERs are intentionally NOT appended (short_trade_history is the
    # source of truth so /trades doesn't double-count).
    if cfg.side.is_long:
        trade = {
            "action": "SELL",
            "ticker": ticker,
            "price": price,
            "shares": shares,
            "pnl": round(pnl_val, 2),
            "pnl_pct": round(pnl_pct, 2),
            "reason": reason,
            "entry_price": entry_price,
            "time": now_hhmm,
            "date": now_date,
        }
        tg.paper_trades.append(trade)
        tg.paper_all_trades.append(trade)

    history_record = {
        "ticker": ticker,
        "side": cfg.history_side_label,
        "action": cfg.paper_log_close_verb,
        "shares": shares,
        "entry_price": entry_price,
        "exit_price": price,
        "pnl": round(pnl_val, 2),
        "pnl_pct": round(pnl_pct, 2),
        "reason": reason,
        "entry_time": entry_hhmm,
        "exit_time": now_hhmm,
        "entry_time_iso": pos.get("entry_ts_utc") or entry_time_str,
        "exit_time_iso": tg._utc_now_iso(),
        "entry_num": pos.get("entry_count", 1),
        "date": now_date,
    }
    history_list.append(history_record)
    if len(history_list) > tg.TRADE_HISTORY_MAX:
        history_list[:] = history_list[-tg.TRADE_HISTORY_MAX :]

    # The live close
    # already feeds [V510-CAND] / lifecycle / persistent trade log

    # Persistent trade log (paper close).
    _entry_iso = pos.get("entry_ts_utc") or entry_time_str or ""
    _hold_s = None
    try:
        if _entry_iso:
            _ent_dt = tg.datetime.fromisoformat(_entry_iso)
            if _ent_dt.tzinfo is None:
                _ent_dt = _ent_dt.replace(tzinfo=timezone.utc)
            _hold_s = (tg.datetime.now(timezone.utc) - _ent_dt).total_seconds()
    except (TypeError, ValueError):
        _hold_s = None
    _log_row = {
        "date": now_date,
        "portfolio": "paper",
        "ticker": ticker,
        "side": cfg.log_side_label,
        "shares": int(shares),
        "entry_price": float(entry_price),
        "exit_price": float(price),
        "entry_time": entry_time_str,
        "exit_time": tg._utc_now_iso(),
        "hold_seconds": _hold_s,
        "pnl": round(pnl_val, 2),
        "pnl_pct": round(pnl_pct, 2),
        "reason": reason,
        "entry_num": int(pos.get("entry_count", 1)),
    }
    _log_row.update(tg._trade_log_snapshot_pos(pos))
    tg.trade_log_append(_log_row)

    tg.paper_log(
        "%s %s %d @ $%.2f reason=%s pnl=$%.2f (%.1f%%)"
        % (cfg.paper_log_close_verb, ticker, shares, price, reason, pnl_val, pnl_pct)
    )

    # v5.31.0 \u2014 forensic exit record. Single funnel for ALL exits
    # (sentinel A/A2/B/F, EOD, manual). Captures alarm code, peak
    # excursion, MAE/MFE, and trail stage so a backtest can replay the
    # full trade lifecycle. Failure-tolerant: a forensic write must
    # NEVER raise into the trading path.
    try:
        from forensic_capture import write_exit_record as _write_exit

        _trail_state = pos.get("trail_state")
        _trail_stage = getattr(_trail_state, "stage", None) if _trail_state else None
        _peak_close = getattr(_trail_state, "peak_close", None) if _trail_state else None
        _bars_seen = getattr(_trail_state, "bars_seen", None) if _trail_state else None
        if _peak_close is None:
            try:
                if cfg.side.is_long:
                    _peak_close = tg._v570_session_hod.get(ticker.upper())
                else:
                    _peak_close = tg._v570_session_lod.get(ticker.upper())
            except Exception:
                _peak_close = None
        if _bars_seen is None and _hold_s is not None:
            try:
                _bars_seen = int(max(0, _hold_s // 60))
            except Exception:
                _bars_seen = None

        # Map reason string to alarm label. The reason vocabulary is
        # spec-stable: A/A2 from sentinel A (per_trade_brake / velocity),
        # B from sentinel B (ema_trail), F from chandelier.
        _reason_lc = (str(reason or "")).lower()
        if "per_trade_brake" in _reason_lc or "a_loss" in _reason_lc:
            _alarm_label = "A1"
        elif "velocity" in _reason_lc or "a_flash" in _reason_lc or "a2" in _reason_lc:
            _alarm_label = "A2"
        elif "ema_trail" in _reason_lc or "alarm_b" in _reason_lc or "9_ema" in _reason_lc:
            _alarm_label = "B"
        elif "chandelier" in _reason_lc or "alarm_f" in _reason_lc:
            _alarm_label = "F"
        elif "eod" in _reason_lc:
            _alarm_label = "EOD"
        elif "manual" in _reason_lc:
            _alarm_label = "MANUAL"
        else:
            _alarm_label = None

        # MAE / MFE in bps from the per-position min-adverse / max-favorable
        # trackers maintained by the sentinel loop (v5.31.0). Falls back to
        # peak_close as a one-sided MFE proxy when the trackers are missing.
        _mae_bps = None
        _mfe_bps = None
        try:
            if entry_price and entry_price > 0:
                _min_adv = pos.get("v531_min_adverse_price")
                _max_fav = pos.get("v531_max_favorable_price") or _peak_close
                if cfg.side.is_long:
                    if _min_adv is not None:
                        _mae_bps = round((_min_adv - entry_price) / entry_price * 10000.0, 1)
                    if _max_fav is not None:
                        _mfe_bps = round((_max_fav - entry_price) / entry_price * 10000.0, 1)
                else:
                    if _min_adv is not None:
                        _mae_bps = round((entry_price - _min_adv) / entry_price * 10000.0, 1)
                    if _max_fav is not None:
                        _mfe_bps = round((entry_price - _max_fav) / entry_price * 10000.0, 1)
        except Exception:
            pass

        _write_exit(
            ticker=ticker,
            side=("LONG" if cfg.side.is_long else "SHORT"),
            ts_utc=tg._utc_now_iso(),
            exit_price=float(price) if price is not None else None,
            entry_price=float(entry_price) if entry_price is not None else None,
            entry_ts_utc=(pos.get("entry_ts_utc") or entry_time_str or None),
            shares=int(shares) if shares is not None else None,
            fill_slippage_bps=None,
            alarm_triggered=_alarm_label,
            exit_reason_code=str(reason) if reason is not None else None,
            peak_close_at_exit=_peak_close,
            trail_stage_at_exit=_trail_stage,
            bars_in_trade=_bars_seen,
            mae_bps=_mae_bps,
            mfe_bps=_mfe_bps,
            pnl_dollars=round(pnl_val, 2) if pnl_val is not None else None,
            pnl_pct=round(pnl_pct, 2) if pnl_pct is not None else None,
        )
    except Exception:
        pass

    # v5.6.1 D4 \u2014 [TRADE_CLOSED] lifecycle line. Pairs to [ENTRY] via
    # entry_id. Reason maps the legacy short token to the spec'd
    # canonical exit_reason vocabulary (stop|target|time|eod|manual).
    # v5.7.1 / v5.9.0 \u2014 also passes through the Bison/Buffalo Titan
    # exit vocabulary. v5.9.0 retires hard_stop_2c and adds forensic_stop
    # and per_trade_brake.
    try:
        _entry_id_close = pos.get("entry_id") or tg._v561_compose_entry_id(
            ticker, pos.get("entry_ts_utc") or ""
        )
        _reason_lc = str(reason or "").lower()
        _v571_reasons = {
            "forensic_stop",
            "per_trade_brake",
            "be_stop",
            "ema_trail",
            "velocity_fuse",
        }
        if _reason_lc in _v571_reasons:
            _exit_reason = _reason_lc
        elif "trail" in _reason_lc or "stop" in _reason_lc:
            _exit_reason = "stop"
        elif "target" in _reason_lc or "tp" in _reason_lc:
            _exit_reason = "target"
        elif "eod" in _reason_lc or "close" in _reason_lc:
            _exit_reason = "eod"
        elif "time" in _reason_lc or "shield" in _reason_lc:
            _exit_reason = "time"
        elif "manual" in _reason_lc:
            _exit_reason = "manual"
        else:
            _exit_reason = _reason_lc or "manual"
        tg._v561_log_trade_closed(
            ticker=ticker,
            side=("LONG" if cfg.side.is_long else "SHORT"),
            entry_id=_entry_id_close,
            entry_ts_utc=(pos.get("entry_ts_utc") or entry_time_str or ""),
            entry_price=float(entry_price or 0.0),
            exit_ts_utc=tg._utc_now_iso(),
            exit_price=float(price),
            exit_reason=_exit_reason,
            qty=int(shares),
            pnl_dollars=float(pnl_val),
            pnl_pct=float(pnl_pct),
            hold_seconds=int(_hold_s) if _hold_s is not None else 0,
            strike_num=int(pos.get("strike_num") or 1),
        )
    except Exception as _e:
        tg.logger.warning("[V561-TRADE-CLOSED] emit error %s: %s", ticker, _e)

    exit_emoji_glyph = "\u2705" if pnl_val >= 0 else "\u274c"
    entry_total_val = round(entry_price * shares, 2)
    SEP_X = "\u2500" * 34
    reason_label = tg.REASON_LABELS.get(reason, reason)
    if reason == "TRAIL":
        peak = pos.get(cfg.trail_peak_attr, price)
        t_dist = max(round(peak * 0.010, 2), 1.00)
        reason_label = "\U0001f3af Trail Stop (1.0%% / $%.2f)" % t_dist
    if cfg.side.is_long:
        msg = (
            "%s EXIT %s\n"
            "%s\n"
            "Shares : %d\n"
            "Entry  : $%.2f  \u2192  $%.2f\n"
            "Cost   : $%s  \u2192  $%s\n"
            "P&L    : $%+.2f  (%+.1f%%)\n"
            "Reason : %s\n"
            "In: %s   Out: %s\n"
            "%s"
        ) % (
            exit_emoji_glyph,
            ticker,
            SEP_X,
            shares,
            entry_price,
            price,
            format(entry_total_val, ",.2f"),
            format(notional, ",.2f"),
            pnl_val,
            pnl_pct,
            reason_label,
            entry_hhmm,
            now_hhmm,
            SEP_X,
        )
    else:
        msg = (
            "%s SHORT CLOSED\n"
            "%s\n"
            "Ticker : %s\n"
            "Shares : %d\n"
            "Entry  : $%.2f  (total $%s)\n"
            "Cover  : $%.2f  (total $%s)\n"
            "P&L    : $%+.2f  (%+.1f%%)\n"
            "Reason : %s\n"
            "In: %s   Out: %s\n"
            "%s"
        ) % (
            exit_emoji_glyph,
            SEP_X,
            ticker,
            shares,
            entry_price,
            format(entry_total_val, ",.2f"),
            price,
            format(notional, ",.2f"),
            pnl_val,
            pnl_pct,
            reason_label,
            entry_hhmm,
            now_hhmm,
            SEP_X,
        )
    tg.send_telegram(msg)

    tg.save_paper_state()

    if not suppress_signal:
        tg._emit_signal(
            {
                "kind": cfg.exit_signal_kind,
                "ticker": ticker,
                "price": float(price),
                "reason": reason,
                "timestamp_utc": tg._utc_now_iso(),
                "main_shares": int(shares),
                # v5.13.7 \u2014 spec-correct order type for the live broker.
                # Paper book ignores this; the Alpaca bridge consumes it.
                "order_type": resolved_order_type,
            }
        )

    # v5.13.6 \u2014 lifecycle log: EXIT_DECISION + POSITION_CLOSED. Best-effort.
    try:
        ll = _lifecycle_logger()
        if ll is not None:
            position_id = pos.get("lifecycle_position_id")
            if not position_id:
                # Reconstruct stable id when an older position was opened
                # before the lifecycle hook landed.
                position_id = _lifecycle.compose_position_id(
                    ticker,
                    pos.get("entry_ts_utc") or "",
                    "LONG" if cfg.side.is_long else "SHORT",
                )
            side_lbl = "LONG" if cfg.side.is_long else "SHORT"
            ll.log_event(
                position_id,
                "EXIT_DECISION",
                {
                    "exit_reason": _exit_reason,
                    "raw_reason": str(reason or ""),
                    "exit_price": float(price),
                    "shares": int(shares),
                    "entry_price": float(entry_price or 0.0),
                },
                reason_text=str(reason_label),
                ticker=ticker,
                side=side_lbl,
                entry_ts_utc=pos.get("entry_ts_utc"),
            )
            # v5.13.7 \u2014 forensic record of the order type the close
            # would submit to a live broker. Paper book ignores this;
            # carrying it on ORDER_SUBMIT means a future Alpaca bridge
            # has the spec-mandated LIMIT / STOP_MARKET / MARKET split.
            ll.log_event(
                position_id,
                "ORDER_SUBMIT",
                {
                    "side": side_lbl,
                    "qty": int(shares),
                    "price": float(price),
                    "raw_reason": str(reason or ""),
                    "order_type": resolved_order_type,
                    "action": "close",
                },
                ticker=ticker,
                side=side_lbl,
                entry_ts_utc=pos.get("entry_ts_utc"),
            )
            ll.log_event(
                position_id,
                "ORDER_FILL",
                {
                    "side": side_lbl,
                    "qty": int(shares),
                    "fill_price": float(price),
                    "action": "close",
                    "order_type": resolved_order_type,
                },
                ticker=ticker,
                side=side_lbl,
                entry_ts_utc=pos.get("entry_ts_utc"),
            )
            ll.close_position(
                position_id,
                {
                    "realized_pnl": float(pnl_val),
                    "realized_pnl_pct": float(pnl_pct),
                    "hold_seconds": int(_hold_s) if _hold_s is not None else None,
                    "exit_reason": _exit_reason,
                    "exit_price": float(price),
                },
                reason_text=f"{side_lbl} closed: {_exit_reason}",
            )
    except Exception as _e:
        try:
            tg.logger.debug("[lifecycle] close hook %s: %s", ticker, _e)
        except Exception:
            pass
