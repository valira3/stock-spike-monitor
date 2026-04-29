"""broker.positions \u2014 per-tick position management.

Extracted from trade_genius.py in v5.11.2 PR 3.
"""
from __future__ import annotations

import logging
import sys as _sys

from broker.orders import check_breakout  # noqa: F401
from broker.stops import (
    _ladder_stop_long,
    _ladder_stop_short,
    _retighten_long_stop,  # noqa: F401
    _retighten_short_stop,  # noqa: F401
)

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


logger = logging.getLogger(__name__)


def _v5104_maybe_fire_entry_2(ticker, side, pos):
    """Per-tick Entry 2 evaluator. Mutates ``pos`` in place on fire.
    Always returns ``None``; check_breakout discards the return value.
    """
    tg = _tg()
    if pos.get("v5104_entry2_fired"):
        return
    cfg = tg.CONFIGS[side]
    side_label = "LONG" if cfg.side.is_long else "SHORT"

    bars = tg.fetch_1min_bars(ticker)
    if not bars:
        return
    current_price = bars.get("current_price")
    if not current_price or current_price <= 0:
        return
    fmp_q = tg.get_fmp_quote(ticker)
    if fmp_q:
        fmp_price = fmp_q.get("price")
        if fmp_price and fmp_price > 0:
            current_price = fmp_price

    # Track the running HWM (long) / LWM (short) for Entry 1 since
    # last fill; needed for the "fresh NHOD/NLOD past Entry 1" check.
    e1_hwm = pos.get("v5104_entry1_hwm")
    if e1_hwm is None:
        e1_hwm = pos.get("v5104_entry1_price", current_price)
    if cfg.side.is_long:
        if current_price > e1_hwm:
            pos["v5104_entry1_hwm"] = float(current_price)
            fresh_extreme = True
        else:
            fresh_extreme = False
    else:
        if current_price < e1_hwm:
            pos["v5104_entry1_hwm"] = float(current_price)
            fresh_extreme = True
        else:
            fresh_extreme = False

    # Re-evaluate Section I fresh at trigger time (spec XIV.3).
    qqq_bars = tg.fetch_1min_bars("QQQ")
    if not qqq_bars:
        return
    qqq_last = qqq_bars.get("current_price")
    qqq_avwap = tg._opening_avwap("QQQ")
    qqq_5m_close = tg._QQQ_REGIME.last_close
    qqq_ema9 = tg._QQQ_REGIME.ema9
    permit = tg.eot_glue.evaluate_section_i(
        side_label, qqq_5m_close, qqq_ema9, qqq_last, qqq_avwap,
    )
    permit_open = bool(permit.get("open"))

    # 1m DI for the appropriate polarity.
    di_streams = tg.v5_di_1m_5m(ticker)
    di_1m_now = (
        di_streams.get("di_plus_1m") if cfg.side.is_long
        else di_streams.get("di_minus_1m")
    )

    decision = tg.eot_glue.evaluate_entry_2_decision(
        ticker, side_label,
        entry_1_active=True,
        permit_open_at_trigger=permit_open,
        di_1m_now=di_1m_now,
        fresh_nhod_or_nlod=fresh_extreme,
        entry_2_already_fired=False,
    )
    if not decision.get("fire"):
        return

    # Entry-1 ts must precede now (spec III.2).
    e1_ts = pos.get("v5104_entry1_ts_utc")
    now_iso = tg._utc_now_iso()
    if e1_ts and e1_ts >= now_iso:
        return

    # 50% of Entry-1 shares, min 1.
    e1_shares = int(pos.get("v5104_entry1_shares") or pos.get("shares") or 0)
    e2_shares = max(1, e1_shares // 2)
    if e2_shares <= 0:
        return

    # Paper cash: long debits, short credits.
    notional = float(current_price) * e2_shares
    if cfg.side.is_long and notional > tg.paper_cash:
        logger.info(
            "[V5100-ENTRY] %s skip entry_2 \u2014 insufficient cash (need $%.2f, have $%.2f)",
            ticker, notional, tg.paper_cash,
        )
        return
    tg.paper_cash += cfg.entry_cash_delta(e2_shares, current_price)

    # Average down/up the entry price; grow share count.
    e1_price = float(pos.get("v5104_entry1_price") or pos.get("entry_price"))
    total_shares = e1_shares + e2_shares
    new_avg = (e1_price * e1_shares + float(current_price) * e2_shares) / total_shares
    pos["entry_price"] = new_avg
    pos["shares"] = total_shares
    pos["v5104_entry2_price"] = float(current_price)
    pos["v5104_entry2_shares"] = int(e2_shares)
    pos["v5104_entry2_ts_utc"] = now_iso
    pos["v5104_entry2_fired"] = True

    try:
        logger.info(
            "[V5100-ENTRY] ticker=%s side=%s entry_num=2 di_1m=%s "
            "fresh_extreme=%s fill_price=%.4f shares=%d new_avg=%.4f",
            ticker, side_label,
            ("%.2f" % di_1m_now) if di_1m_now is not None else "None",
            fresh_extreme, float(current_price), e2_shares, new_avg,
        )
    except Exception:
        pass

    try:
        tg.save_paper_state()
    except Exception:
        pass


# ============================================================
# MANAGE POSITIONS (stop + trail logic)
# ============================================================
def manage_positions():
    """Check stops and update trailing stops for all open positions."""
    tg = _tg()
    eot = tg.eot
    eot_glue = tg.eot_glue
    positions = tg.positions
    tickers_to_close = []

    # v3.4.23 \u2014 enforce 0.75% entry cap on every open long position
    # before the regular stop/trail pass. This catches pre-cap positions
    # and any position whose stored stop has drifted wider than the cap.
    # Also fires immediate exit on positions that have already breached
    # the retro-tightened stop. Idempotent \u2014 fast when everything is
    # already tight.
    tg.retighten_all_stops(force_exit=True, fetch_prices=True)

    # v5.9.1: Sovereign Regime Shield (PDC eject) retired. Entry-side
    # index regime now lives in the 5m EMA compass (v5.9.0); the exit
    # side intentionally has no global index eject.

    for ticker in list(positions.keys()):
        bars = tg.fetch_1min_bars(ticker)
        if not bars:
            continue

        current_price = bars["current_price"]
        pos = positions[ticker]

        # v5.10.1 \u2014 Section IV high-priority overrides (per-tick). The
        # Sovereign Brake (-$500 unrealized) and Velocity Fuse (>1%
        # against the current 1m candle open) are evaluated on every
        # tick and take priority over phase-specific stops.
        try:
            entry_p = pos.get("entry_price")
            shares = int(pos.get("shares") or 0)
            unrealized = (current_price - entry_p) * shares if entry_p else 0.0
            opens_eot = bars.get("opens") or []
            cur_1m_open = None
            if opens_eot:
                cur_1m_open = opens_eot[-1] if opens_eot[-1] is not None else (
                    opens_eot[-2] if len(opens_eot) >= 2 else None
                )
            override = eot_glue.evaluate_section_iv(
                eot.SIDE_LONG,
                unrealized_pnl_dollars=unrealized,
                current_price=current_price,
                current_1m_open=cur_1m_open,
            )
            if override == eot.EXIT_REASON_SOVEREIGN_BRAKE:
                logger.warning(
                    "[V5100-SOVEREIGN-BRAKE] ticker=%s side=LONG entry_avg=%.4f "
                    "current_price=%.4f unrealized_pnl=%.2f qty=%d",
                    ticker, entry_p or 0.0, current_price, unrealized, shares,
                )
                tickers_to_close.append((ticker, current_price, "sovereign_brake"))
                continue
            if override == eot.EXIT_REASON_VELOCITY_FUSE:
                logger.warning(
                    "[V5100-VELOCITY-FUSE] ticker=%s side=LONG cur=%.4f open=%s",
                    ticker, current_price,
                    ("%.4f" % cur_1m_open) if cur_1m_open else "None",
                )
                tickers_to_close.append((ticker, current_price, "velocity_fuse"))
                continue
        except Exception as _eot_e:
            logger.warning("[V5100-OVERRIDE] long %s: %s", ticker, _eot_e)

        # v5.10.5 \u2014 Phase B/C Triple-Lock. Phase A continues to use the
        # ladder/Maffei plumbing below; Phase B (be_stop) and Phase C
        # (ema_trail) exits fire here when the post-Entry-2 lock /
        # 5m-EMA9 leash trip.
        phase_exit, _ = tg._v5105_phase_machine_tick(
            ticker, eot.SIDE_LONG, pos, bars,
        )
        if phase_exit is not None:
            tickers_to_close.append((ticker, current_price, phase_exit))
            continue

        # v3.4.35 \u2014 Stop hit. "TRAIL" when the ladder has ratcheted past
        # the initial structural stop (capital already safe), else "STOP"
        # (initial structural stop hit with no profit locked).
        if current_price <= pos["stop"]:
            # Derive TRAIL vs STOP from whether the stop has actually
            # ratcheted above entry (i.e. capital was locked in). The
            # previous `pos.get("trail_active")` flag was set true the
            # first time peak_gain hit +1 % and was never unset \u2014 so a
            # position that went +1 %, came back, and hit the *initial*
            # structural stop was still attributed as "TRAIL" even
            # though no profit was locked. Derive from stop level.
            reason = "TRAIL" if pos["stop"] > pos["entry_price"] else "STOP"
            tickers_to_close.append((ticker, current_price, reason))
            continue

        # \u2500\u2500 Eye of the Tiger: "The Red Candle" \u2014 lost Daily Polarity \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        # Fires when 1-min confirmed close < day open OR < PDC
        closes = [c for c in bars.get("closes", []) if c is not None]
        ticker_1min_close = closes[-2] if len(closes) >= 2 else (closes[-1] if closes else current_price)
        opens = [o for o in bars.get("opens", []) if o is not None]
        day_open = opens[0] if opens else None
        pos_pdc = pos.get("pdc") or pos.get("prev_close")
        lost_polarity = False
        if day_open is not None and ticker_1min_close < day_open:
            lost_polarity = True
        if pos_pdc and ticker_1min_close < pos_pdc:
            lost_polarity = True
        if lost_polarity:
            tickers_to_close.append((ticker, current_price, "RED_CANDLE"))
            continue

        entry_price = pos["entry_price"]

        # v3.4.35 \u2014 Profit-Lock Ladder replaces the 1%/$1 armed-trail.
        # Update peak (trail_high) every tick \u2014 ladder reads this.
        if current_price > pos.get("trail_high", entry_price):
            pos["trail_high"] = current_price
        peak = pos["trail_high"]
        peak_gain_pct = (peak - entry_price) / entry_price if entry_price > 0 else 0.0

        # Compute ladder stop; ratchet pos["stop"] upward only.
        ladder_stop = _ladder_stop_long(pos)
        if ladder_stop > pos.get("stop", 0):
            old_stop = pos.get("stop", 0)
            pos["stop"] = ladder_stop
            logger.info(
                "[LADDER] %s LONG stop ratcheted $%.2f \u2192 $%.2f "
                "(peak=$%.2f, +%.2f%%)",
                ticker, old_stop, ladder_stop, peak, peak_gain_pct * 100,
            )

        # Arm cosmetic trail_active / trail_stop once past the 1% gate
        # (Bullet phase ends). Keeps /api/state + exit-reason attribution
        # (TRAIL vs STOP in _finalize_pos) working.
        if peak_gain_pct >= 0.01:
            if not pos.get("trail_active"):
                pos["trail_active"] = True
                logger.info(
                    "Trail armed for %s at $%.2f (+%.2f%% peak) \u2014 ladder active",
                    ticker, current_price, peak_gain_pct * 100,
                )
            pos["trail_stop"] = pos["stop"]

        # Exit when current price crosses the ladder stop.
        if current_price <= pos["stop"]:
            # Derive TRAIL vs STOP from whether the stop has actually
            # ratcheted above entry (i.e. capital was locked in). The
            # previous `pos.get("trail_active")` flag was set true the
            # first time peak_gain hit +1 % and was never unset \u2014 so a
            # position that went +1 %, came back, and hit the *initial*
            # structural stop was still attributed as "TRAIL" even
            # though no profit was locked. Derive from stop level.
            reason = "TRAIL" if pos["stop"] > pos["entry_price"] else "STOP"
            tickers_to_close.append((ticker, current_price, reason))
            continue

    # Close positions outside the loop to avoid mutation during iteration
    for ticker, price, reason in tickers_to_close:
        tg.close_position(ticker, price, reason)


# ============================================================
# MANAGE SHORT POSITIONS (stop + trail logic)
# ============================================================
def manage_short_positions():
    """Check stops and trailing stops for all open short positions."""
    tg = _tg()
    eot = tg.eot
    eot_glue = tg.eot_glue
    short_positions = tg.short_positions
    pdc = tg.pdc

    # v3.4.23 \u2014 enforce 0.75% entry cap retroactively on every open
    # short (see manage_positions for rationale). Note: manage_positions
    # and manage_short_positions are called back-to-back by the scan
    # loop, so calling retighten_all_stops from both is redundant-but-
    # cheap. Kept in both for defensive symmetry: if a future refactor
    # reorders or skips one manager, the cap still holds for the other
    # book.
    tg.retighten_all_stops(force_exit=True, fetch_prices=True)

    # v5.9.1: Sovereign Regime Shield (PDC eject) retired on the short
    # side too. Per-ticker POLARITY_SHIFT below remains.

    _short_to_close = []
    for ticker in list(short_positions.keys()):
        pos = short_positions[ticker]
        entry_price = pos["entry_price"]
        shares = pos["shares"]

        bars = tg.fetch_1min_bars(ticker)
        if not bars:
            continue
        current_price = bars["current_price"]

        # v5.10.1 \u2014 Section IV high-priority overrides (per-tick).
        # SHORT P&L sign convention: unrealized = (entry - current) * shares.
        try:
            unrealized = (entry_price - current_price) * int(shares or 0)
            opens_eot_s = bars.get("opens") or []
            cur_1m_open_s = None
            if opens_eot_s:
                cur_1m_open_s = opens_eot_s[-1] if opens_eot_s[-1] is not None else (
                    opens_eot_s[-2] if len(opens_eot_s) >= 2 else None
                )
            override_s = eot_glue.evaluate_section_iv(
                eot.SIDE_SHORT,
                unrealized_pnl_dollars=unrealized,
                current_price=current_price,
                current_1m_open=cur_1m_open_s,
            )
            if override_s == eot.EXIT_REASON_SOVEREIGN_BRAKE:
                logger.warning(
                    "[V5100-SOVEREIGN-BRAKE] ticker=%s side=SHORT entry_avg=%.4f "
                    "current_price=%.4f unrealized_pnl=%.2f qty=%d",
                    ticker, entry_price or 0.0, current_price, unrealized,
                    int(shares or 0),
                )
                tg.close_short_position(ticker, current_price, reason="sovereign_brake")
                continue
            if override_s == eot.EXIT_REASON_VELOCITY_FUSE:
                logger.warning(
                    "[V5100-VELOCITY-FUSE] ticker=%s side=SHORT cur=%.4f open=%s",
                    ticker, current_price,
                    ("%.4f" % cur_1m_open_s) if cur_1m_open_s else "None",
                )
                tg.close_short_position(ticker, current_price, reason="velocity_fuse")
                continue
        except Exception as _eot_e:
            logger.warning("[V5100-OVERRIDE] short %s: %s", ticker, _eot_e)

        # v5.10.5 \u2014 Phase B/C Triple-Lock (short mirror).
        phase_exit_s, _ = tg._v5105_phase_machine_tick(
            ticker, eot.SIDE_SHORT, pos, bars,
        )
        if phase_exit_s is not None:
            tg.close_short_position(ticker, current_price, reason=phase_exit_s)
            continue

        # v3.4.35 \u2014 Profit-Lock Ladder replaces the 1%/$1 armed-trail.
        # Track trail_low every tick (peak = deepest price reached).
        trail_low = pos.get("trail_low", entry_price)
        if current_price < trail_low:
            trail_low = current_price
            pos["trail_low"] = trail_low
        peak_gain_pct = (entry_price - trail_low) / entry_price if entry_price > 0 else 0.0

        # Compute ladder stop; ratchet pos["stop"] downward only (tighter).
        ladder_stop = _ladder_stop_short(pos)
        if ladder_stop < pos.get("stop", float("inf")):
            old_stop = pos.get("stop", 0)
            pos["stop"] = ladder_stop
            logger.info(
                "[LADDER] %s SHORT stop ratcheted $%.2f \u2192 $%.2f "
                "(trail_low=$%.2f, +%.2f%%)",
                ticker, old_stop, ladder_stop, trail_low, peak_gain_pct * 100,
            )

        # Arm cosmetic trail_active / trail_stop past the 1% gate.
        if peak_gain_pct >= 0.01:
            if not pos.get("trail_active"):
                pos["trail_active"] = True
                logger.info(
                    "Trail armed for %s SHORT at $%.2f (+%.2f%% peak) \u2014 ladder active",
                    ticker, current_price, peak_gain_pct * 100,
                )
            pos["trail_stop"] = pos["stop"]

        stop = pos["stop"]
        trail_active = pos.get("trail_active", False)

        # Exit on stop hit. TRAIL vs STOP per ladder-armed state.
        exit_reason = None
        if current_price >= stop:
            exit_reason = "TRAIL" if trail_active else "STOP"


        # \u2500\u2500 Eye of the Tiger: "The Polarity Shift" \u2014 Price > PDC \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        # Uses completed 1m bar close (per-ticker; not part of the index shield)
        if not exit_reason:
            ticker_pdc = pdc.get(ticker, 0)
            if ticker_pdc > 0:
                ps_closes = [c for c in bars.get("closes", []) if c is not None]
                ps_1min_close = ps_closes[-2] if len(ps_closes) >= 2 else (ps_closes[-1] if ps_closes else current_price)
                if ps_1min_close > ticker_pdc:
                    exit_reason = "POLARITY_SHIFT"

        if exit_reason:
            tg.close_short_position(ticker, current_price, exit_reason)
