"""broker.orders \u2014 order execution: check_breakout, execute_breakout, close_breakout, paper_shares_for.

Extracted from trade_genius.py in v5.11.2 PR 2.
"""
from __future__ import annotations

import sys as _sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from broker.stops import _capped_long_stop, _capped_short_stop  # noqa: F401

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
    capped_stop_fn = getattr(tg, cfg.capped_stop_fn_name)

    if tg._trading_halted:
        return False, None
    if tg._scan_paused:
        return False, None

    now_et = tg._now_et()
    today = now_et.strftime("%Y-%m-%d")

    # Timing gate: after 09:35 ET (OR window close + 2-bar confirm)
    market_open = now_et.replace(hour=9, minute=35, second=0, microsecond=0)
    if now_et < market_open:
        return False, None
    eod_time = now_et.replace(hour=15, minute=55, second=0, microsecond=0)
    if now_et >= eod_time:
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
            ticker=ticker, reason="daily_loss_limit_hit",
            ts_utc=tg._utc_now_iso(), gate_state=None,
        )
        return False, None

    # OR data available
    if ticker not in or_dict or ticker not in tg.pdc:
        return False, None

    # Daily entry cap (max 5). v5.7.0 \u2014 bypassed for Ten Titans
    # when ENABLE_UNLIMITED_TITAN_STRIKES is True; Titan re-entry
    # is governed by the Strike 2+ Expansion Gate further down.
    _v570_titan = tg._v570_is_titan(ticker)
    _v570_unlimited = bool(tg.ENABLE_UNLIMITED_TITAN_STRIKES) and _v570_titan
    if not _v570_unlimited:
        if daily_count.get(ticker, 0) >= 5:
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

    # OR sanity check: OR-edge must be within OR_STALE_THRESHOLD of live price.
    if not tg._or_price_sane(or_dict[ticker], current_price):
        pct = abs(or_dict[ticker] - current_price) / current_price * 100
        tg.or_stale_skip_count[ticker] = tg.or_stale_skip_count.get(ticker, 0) + 1
        tg.logger.warning(
            "SKIP %s %s \u2014 %s $%.2f is %.1f%% from live $%.2f (stale?)",
            ticker, cfg.skip_label, cfg.or_side_label,
            or_dict[ticker], pct, current_price,
        )
        return False, None

    or_edge_val = or_dict[ticker]
    pdc_val_e = tg.pdc[ticker]
    # 2-bar OR breakout/breakdown confirmation (Tiger 2.0).
    if cfg.side.is_long:
        price_break = tg._tiger_two_bar_long(closes, or_edge_val)
    else:
        price_break = tg._tiger_two_bar_short(closes, or_edge_val)
    polarity_ok = (
        current_price > pdc_val_e if cfg.side.is_long
        else current_price < pdc_val_e
    )

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

    # Volume confirmation: entry bar volume >= 1.5x session average.
    # Gated by TIGER_V2_REQUIRE_VOL (default False); Tiger 2.0 replaces
    # the vol filter with DI.
    if tg.TIGER_V2_REQUIRE_VOL and len(volumes) >= 5:
        if not vol_ready_flag:
            tg.logger.info("SKIP %s [DATA NOT READY] no closed bar with volume in last 5", ticker)
            if price_break:
                tg._record_near_miss(
                    ticker=ticker, side=cfg.log_side_label, reason="DATA_NOT_READY",
                    close=round(last_close, 2), level=round(or_edge_val, 2),
                    vol_bar=None, vol_avg=None, vol_pct=None,
                )
            return False, None
        if avg_vol > 0 and entry_bar_vol < avg_vol * 1.5:
            tg.logger.info("SKIP %s [LOW VOL] entry bar %.0f vs avg %.0f", ticker, entry_bar_vol, avg_vol)
            if price_break:
                tg._record_near_miss(
                    ticker=ticker, side=cfg.log_side_label, reason="LOW_VOL",
                    close=round(last_close, 2), level=round(or_edge_val, 2),
                    vol_bar=int(entry_bar_vol), vol_avg=int(avg_vol),
                    vol_pct=round(vol_pct, 1) if vol_pct is not None else None,
                )
            return False, None

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
        side_label, qqq_5m_close, qqq_ema9, qqq_last, qqq_avwap,
    )
    if not permit_res.get("open"):
        tg._v561_log_skip(
            ticker=ticker,
            reason="V5100_PERMIT:%s" % permit_res.get("reason", "closed"),
            ts_utc=tg._utc_now_iso(), gate_state=None,
        )
        return False, None

    # Section II.1 \u2014 Volume Bucket (Entry-1 only). Determine the
    # minute_of_day from the last completed 1m bar.
    try:
        now_et_eb = tg.datetime.now(tz=ZoneInfo("America/New_York"))
        minute_of_day_hhmm = now_et_eb.strftime("%H:%M")
    except Exception:
        minute_of_day_hhmm = "09:30"
    volumes_eb = bars.get("volumes", []) or []
    last_completed_vol = None
    if len(volumes_eb) >= 2 and volumes_eb[-2] is not None:
        last_completed_vol = volumes_eb[-2]
    elif volumes_eb and volumes_eb[-1] is not None:
        last_completed_vol = volumes_eb[-1]
    vol_check = tg.eot_glue.evaluate_volume_bucket_gate(
        ticker, minute_of_day_hhmm, last_completed_vol or 0,
    )
    volume_bucket_ok = tg.eot.evaluate_volume_bucket(vol_check)
    if not volume_bucket_ok:
        tg._v561_log_skip(
            ticker=ticker, reason="V5100_VOLBUCKET:%s" % vol_check.get("gate"),
            ts_utc=tg._utc_now_iso(), gate_state=None,
        )
        return False, None

    # Section II.2 \u2014 Boundary Hold (Entry-1 only). Stateless: the
    # last two closed 1m closes vs the OR edge.
    boundary_res = tg.eot_glue.evaluate_boundary_hold_gate(
        ticker, side_label, or_high_val, or_low_val,
    )

    # v5.13.0 PR 4 \u2014 Tiger Sovereign Phase 2 gate audit line. Emits
    # one [V510-CAND] line per entry consideration with both Phase 2
    # gates' verdicts side-by-side so the JSONL log captures exactly
    # what blocked or admitted the entry.
    try:
        vc_gate = (vol_check or {}).get("gate")
        vc_ratio = (vol_check or {}).get("ratio")
        vol_pass_str = "PASS" if vc_gate in ("PASS", "COLDSTART") else "FAIL"
        try:
            ratio_str = "%.3f" % float(vc_ratio) if vc_ratio is not None else "null"
        except (TypeError, ValueError):
            ratio_str = "null"
        bh_consec = int(boundary_res.get("consecutive_outside") or 0)
        candle_pass_str = "PASS" if boundary_res.get("hold") else "FAIL"
        last2_str = "n=%d" % bh_consec
        tg.logger.info(
            "[V510-CAND] symbol=%s gate_volume=%s ratio=%s "
            "gate_2candle=%s last2=%s",
            ticker, vol_pass_str, ratio_str, candle_pass_str, last2_str,
        )
    except Exception:
        pass

    if not boundary_res.get("hold"):
        tg._v561_log_skip(
            ticker=ticker, reason="V5100_BOUNDARY:%s" % boundary_res.get("reason"),
            ts_utc=tg._utc_now_iso(), gate_state=None,
        )
        return False, None

    # Section III Trend Confirmation \u2014 5m DI > 25, 1m DI > 25, NHOD/NLOD.
    di_streams = tg.v5_di_1m_5m(ticker)
    if cfg.side.is_long:
        di_5m = di_streams.get("di_plus_5m")
        di_1m = di_streams.get("di_plus_1m")
    else:
        di_5m = di_streams.get("di_minus_5m")
        di_1m = di_streams.get("di_minus_1m")

    # NHOD / NLOD: derive from session HOD/LOD vs current_price (strict).
    _prev_hod, _prev_lod, hod_break, lod_break = tg._v570_update_session_hod_lod(
        ticker, current_price,
    )
    is_extreme_print = bool(hod_break if cfg.side.is_long else lod_break)

    entry1_decision = tg.eot_glue.evaluate_entry_1_decision(
        ticker, side_label,
        permit_open=True,
        volume_bucket_ok=True,
        boundary_hold_ok=True,
        di_5m=di_5m, di_1m=di_1m,
        is_nhod_or_nlod=is_extreme_print,
    )
    if not entry1_decision.get("fire"):
        tg._v561_log_skip(
            ticker=ticker,
            reason="V5100_ENTRY1:%s" % entry1_decision.get("reason", ""),
            ts_utc=tg._utc_now_iso(), gate_state=None,
        )
        return False, None

    # All Eye-of-the-Tiger gates pass. Bars dict carries current_price
    # forward to execute_breakout.
    try:
        tg.logger.info(
            "[V5100-ENTRY] ticker=%s side=%s entry_num=1 di_5m=%s di_1m=%s "
            "fill_price=%.4f",
            ticker, side_label,
            ("%.2f" % di_5m) if di_5m is not None else "None",
            ("%.2f" % di_1m) if di_1m is not None else "None",
            current_price,
        )
    except Exception:
        pass
    return True, bars


def paper_shares_for(price: float) -> int:
    """Dollar-sized paper order: floor(PAPER_DOLLARS_PER_ENTRY / price),
    min 1. Returns 0 only when price <= 0 (invalid).

    v3.4.45 \u2014 paper now sizes by notional like RH does, scaled to the
    $100k paper book (default $10k/entry vs RH's $1.5k/$25k). This
    fixes the old flat 10-share behavior that made $400 NVDA cost 80x
    more risk per entry than $5 QBTS.
    """
    if price <= 0:
        return 0
    return max(1, int(_tg().PAPER_DOLLARS_PER_ENTRY // price))


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
    capped_stop_fn = getattr(tg, cfg.capped_stop_fn_name)

    # Daily loss limit (shared between long/short).
    if not tg._check_daily_loss_limit(ticker):
        return

    now_et = tg._now_et()
    limit_price = round(current_price + cfg.limit_offset, 2)
    or_dict = getattr(tg, cfg.or_attr)
    if cfg.side.is_long:
        cap_arg = or_dict.get(ticker, current_price)
    else:
        cap_arg = tg.pdc.get(ticker, current_price)
    stop_price, _stop_capped, _stop_baseline = capped_stop_fn(
        cap_arg, current_price
    )
    if _stop_capped:
        if cfg.side.is_long:
            tg.logger.info(
                "%s stop capped: baseline=$%.2f -> capped=$%.2f (entry=$%.2f, %.2f%% cap)",
                ticker, _stop_baseline, stop_price, current_price, tg.MAX_STOP_PCT * 100,
            )
        else:
            tg.logger.info(
                "%s short stop capped: baseline=$%.2f -> capped=$%.2f (entry=$%.2f, %.2f%% cap)",
                ticker, _stop_baseline, stop_price, current_price, tg.MAX_STOP_PCT * 100,
            )

    # Dollar-sized paper entry; shares scale with price.
    shares = paper_shares_for(current_price)
    notional = current_price * shares
    if shares <= 0:
        if cfg.side.is_long:
            tg.logger.warning("[paper] skip %s \u2014 invalid price $%.2f",
                              ticker, current_price)
        else:
            tg.logger.warning("[paper] skip short %s \u2014 invalid price $%.2f",
                              ticker, current_price)
        return

    # Long entry needs cash to buy; short entry credits cash on open.
    if cfg.side.is_long and notional > tg.paper_cash:
        tg.logger.info(
            "[paper] skip %s \u2014 insufficient cash (need $%.2f, have $%.2f)",
            ticker, notional, tg.paper_cash,
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
                ticker, _v570_side_label, _entry_compass,
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
        "v5104_entry1_price": float(current_price),
        "v5104_entry1_shares": int(shares),
        "v5104_entry1_hwm": float(current_price),
        "v5104_entry1_ts_utc": _entry_ts_utc,
        "v5104_entry2_fired": False,
    }
    if cfg.side.is_short:
        pos["side"] = "SHORT"
        pos["trail_stop"] = None
    positions_dict[ticker] = pos
    daily_count[ticker] = entry_num
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
        % (cfg.paper_log_entry_verb, ticker, shares, current_price,
           limit_price, stop_price, entry_num)
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
            bid=bid_v, ask=ask_v,
            cash=round(tg.paper_cash, 2),
            equity=round(equity_v, 2),
            open_positions=open_pos,
            total_exposure_pct=round(expo_pct, 4),
            current_drawdown_pct=dd_pct,
        )
    except Exception as e:
        tg.logger.warning("[V510-ENTRY] snapshot error %s: %s", ticker, e)

    or_edge_e = or_dict.get(ticker, 0)
    pdc_e = tg.pdc.get(ticker, 0)
    SEP_E = "\u2500" * 34
    stop_label = (
        cfg.stop_capped_label if _stop_capped else cfg.stop_baseline_label
    )
    if cfg.side.is_long:
        sig_lines = "Signal : ORB Breakout \u2191\n"
        sig_lines += "  1m close > OR High \u2713\n"
        sig_lines += "  Price > PDC \u2713\n"
        sig_lines += "  SPY > PDC \u2713\n"
        sig_lines += "  QQQ > PDC \u2713\n"
        msg = (
            "\U0001f4c8 LONG ENTRY %s  #%d\n"
            "%s\n"
            "Price  : $%.2f  (limit $%.2f)\n"
            "Shares : %d   Cost: $%s\n"
            "Stop   : $%.2f  (%s)\n"
            "OR High: $%.2f   PDC: $%.2f\n"
            "%s"
            "Time   : %s\n"
            "%s"
        ) % (ticker, entry_num, SEP_E,
             current_price, limit_price,
             shares, format(notional, ",.2f"),
             stop_price, stop_label, or_edge_e, pdc_e, sig_lines, now_hhmm, SEP_E)
    else:
        sig_lines = "Signal   : Wounded Buffalo \u2193\n"
        sig_lines += "  1m close < OR Low \u2713\n"
        sig_lines += "  Price < PDC \u2713\n"
        sig_lines += "  SPY < PDC \u2713\n"
        sig_lines += "  QQQ < PDC \u2713\n"
        msg = (
            "\U0001fa78 SHORT ENTRY #%d\n"
            "%s\n"
            "Ticker   : %s\n"
            "Entry    : $%.2f (limit)\n"
            "Shares   : %d   Proceeds: $%s\n"
            "Stop     : $%.2f (%s)\n"
            "OR Low   : $%.2f\n"
            "PDC      : $%.2f\n"
            "%s"
            "Time     : %s\n"
            "%s"
        ) % (entry_num, SEP_E, ticker, current_price,
             shares, format(notional, ",.2f"),
             stop_price, stop_label, or_edge_e, pdc_e, sig_lines, now_hhmm, SEP_E)
    tg.send_telegram(msg)

    tg.save_paper_state()

    tg._emit_signal({
        "kind": cfg.entry_signal_kind,
        "ticker": ticker,
        "price": float(current_price),
        "reason": cfg.entry_signal_reason,
        "timestamp_utc": tg._utc_now_iso(),
        "main_shares": int(shares),
    })


def close_breakout(ticker, price, side, reason="STOP"):
    """Side-parameterized close.

    v4.9.0 \u2014 unified body. The legacy long/short twins were deleted;
    this single body is parameterized by SideConfig. Synthetic-harness
    goldens enforce byte-equal Telegram + paper_log + trade_log output
    against the v4.8.2 baseline.
    """
    tg = _tg()
    cfg = tg.CONFIGS[side]
    positions_dict = getattr(tg, cfg.positions_attr)
    history_list = getattr(tg, cfg.trade_history_attr)

    if ticker not in positions_dict:
        return

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
        history_list[:] = history_list[-tg.TRADE_HISTORY_MAX:]

    # v5.2.0 \u2014 mirror live exit decision to all shadow configs. Same
    # ticker/price/reason as the live close, so shadow P&L tracks the
    # exact same exit logic. Failure-tolerant.
    try:
        tg._v520_close_shadow_all(ticker, price, reason)
    except Exception as e:
        tg.logger.warning("[V520-SHADOW-PNL] close hook %s: %s", ticker, e)

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

    tg.paper_log("%s %s %d @ $%.2f reason=%s pnl=$%.2f (%.1f%%)"
                 % (cfg.paper_log_close_verb, ticker, shares, price,
                    reason, pnl_val, pnl_pct))

    # v5.6.1 D4 \u2014 [TRADE_CLOSED] lifecycle line. Pairs to [ENTRY] via
    # entry_id. Reason maps the legacy short token to the spec'd
    # canonical exit_reason vocabulary (stop|target|time|eod|manual).
    # v5.7.1 / v5.9.0 \u2014 also passes through the Bison/Buffalo Titan
    # exit vocabulary. v5.9.0 retires hard_stop_2c and adds forensic_stop
    # and per_trade_brake.
    try:
        _entry_id_close = pos.get("entry_id") or tg._v561_compose_entry_id(
            ticker, pos.get("entry_ts_utc") or "")
        _reason_lc = str(reason or "").lower()
        _v571_reasons = {
            "forensic_stop", "per_trade_brake",
            "be_stop", "ema_trail", "velocity_fuse",
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
        ) % (exit_emoji_glyph, ticker, SEP_X,
             shares, entry_price, price,
             format(entry_total_val, ",.2f"), format(notional, ",.2f"),
             pnl_val, pnl_pct, reason_label, entry_hhmm, now_hhmm, SEP_X)
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
        ) % (exit_emoji_glyph, SEP_X, ticker, shares,
             entry_price, format(entry_total_val, ",.2f"),
             price, format(notional, ",.2f"),
             pnl_val, pnl_pct, reason_label, entry_hhmm, now_hhmm, SEP_X)
    tg.send_telegram(msg)

    tg.save_paper_state()

    tg._emit_signal({
        "kind": cfg.exit_signal_kind,
        "ticker": ticker,
        "price": float(price),
        "reason": reason,
        "timestamp_utc": tg._utc_now_iso(),
        "main_shares": int(shares),
    })
