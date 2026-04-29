"""broker.positions \u2014 per-tick position management.

Extracted from trade_genius.py in v5.11.2 PR 3.
"""

from __future__ import annotations

import logging
import sys as _sys

import time as _time

from broker.orders import check_breakout  # noqa: F401
from broker.stops import (
    _ladder_stop_long,
    _ladder_stop_short,
    _retighten_long_stop,  # noqa: F401
    _retighten_short_stop,  # noqa: F401
)
from engine.bars import compute_5m_ohlc_and_ema9
from engine.sentinel import (
    EXIT_REASON_ALARM_A,
    EXIT_REASON_ALARM_B,
    EXIT_REASON_ALARM_C,
    SIDE_LONG as _SENTINEL_SIDE_LONG,
    SIDE_SHORT as _SENTINEL_SIDE_SHORT,
    evaluate_sentinel,
    format_sentinel_log,
    maybe_reset_pnl_baseline_on_shares_change,
    new_pnl_history,
    record_pnl,
)
from engine.titan_grip import (
    ACTION_RATCHET,
    ACTION_RUNNER_EXIT,
    ACTION_STAGE1_HARVEST,
    ACTION_STAGE3_HARVEST,
    TitanGripState,
)

# v5.13.6 \u2014 best-effort import of lifecycle logger.
try:
    import lifecycle_logger as _lifecycle  # noqa: F401
except Exception:  # pragma: no cover
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


def _lifecycle_log_phase4_change(ticker, side, pos, result, current_price):
    """Emit PHASE4_SENTINEL / TITAN_GRIP_STAGE events when state changes
    vs the prior tick. Best-effort: any exception swallowed.
    """
    try:
        ll = _lifecycle_logger()
        if ll is None:
            return
        position_id = pos.get("lifecycle_position_id")
        if not position_id:
            return
        side_lbl = "LONG" if side == _SENTINEL_SIDE_LONG else "SHORT"
        # Sentinel state summary - the codes that fired this tick.
        codes = list(getattr(result, "alarm_codes", None) or [])
        prior = pos.get("_lifecycle_prev_alarm_codes")
        if prior != codes:
            pos["_lifecycle_prev_alarm_codes"] = list(codes)
            ll.log_event(
                position_id,
                "PHASE4_SENTINEL",
                {
                    "alarm_codes": codes,
                    "fired": bool(getattr(result, "fired", False)),
                    "exit_reason": getattr(result, "exit_reason", None),
                    "current_price": float(current_price),
                    "state": ",".join(codes) if codes else "OK",
                },
                ticker=ticker,
                side=side_lbl,
                entry_ts_utc=pos.get("entry_ts_utc"),
                reason_text=(f"sentinel {','.join(codes)}" if codes else "sentinel ok"),
            )
        # Titan grip stage transition.
        st = pos.get("titan_grip_state")
        cur_stage = getattr(st, "stage", None) if st is not None else None
        prev_stage = pos.get("_lifecycle_prev_titan_stage", "__init__")
        if cur_stage is not None and cur_stage != prev_stage:
            pos["_lifecycle_prev_titan_stage"] = cur_stage
            ll.log_event(
                position_id,
                "TITAN_GRIP_STAGE",
                {
                    "stage": cur_stage,
                    "anchor": float(getattr(st, "current_stop_anchor", 0) or 0),
                    "shares_remaining": int(pos.get("shares") or 0),
                },
                ticker=ticker,
                side=side_lbl,
                entry_ts_utc=pos.get("entry_ts_utc"),
                reason_text=f"titan grip stage -> {cur_stage}",
            )
    except Exception as e:
        try:
            logger.debug("[lifecycle] phase4 change %s: %s", ticker, e)
        except Exception:
            pass


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


def _ensure_titan_grip(ticker, side, pos):
    """Lazily attach a TitanGripState to a position once Phase 4 is
    active. Returns the existing or newly-created state, or None if
    OR_High / OR_Low are not yet seeded for this ticker (in which
    case Alarm C is skipped this tick).

    The state is stored on ``pos["titan_grip_state"]`` (sidecar dict
    pattern from PR 2). It's created exactly once per position and
    survives until the position closes \u2014 close_breakout pops the
    whole pos dict, taking the state with it.
    """
    state = pos.get("titan_grip_state")
    if state is not None:
        return state
    tg = _tg()
    or_high = (tg.or_high or {}).get(ticker)
    or_low = (tg.or_low or {}).get(ticker)
    if or_high is None or or_low is None:
        return None
    entry_p = pos.get("entry_price")
    shares = int(pos.get("shares") or 0)
    if not entry_p or shares <= 0:
        return None
    state = TitanGripState(
        position_id=str(pos.get("position_id") or ticker),
        direction=side,
        entry_price=float(entry_p),
        or_high=float(or_high),
        or_low=float(or_low),
        original_shares=int(shares),
    )
    pos["titan_grip_state"] = state
    return state


def _apply_titan_grip_partial(ticker, side, pos, action, current_price):
    """Apply a Titan Grip partial-harvest action to a position.

    For Stage 1 / Stage 3 harvests (25% LIMIT each) we reduce
    pos["shares"] in place and emit a partial-harvest signal so
    executors / dashboards see the action. The remaining position
    continues to be managed by the existing manage_positions loop;
    its stop is updated via pos["stop"] so the existing exit-on-stop
    branch fires the runner exit.

    Returns True if the action consumed shares (and thus reduced
    the position), False otherwise (ratchet-only / runner-exit).
    """
    tg = _tg()
    code = action.code
    if code in (ACTION_STAGE1_HARVEST, ACTION_STAGE3_HARVEST):
        cur_shares = int(pos.get("shares") or 0)
        n = int(min(action.shares, cur_shares))
        if n <= 0:
            return False
        pos["shares"] = cur_shares - n
        # Mirror the partial-harvest into paper accounting using the
        # same long/short cash-flow conventions as close_breakout. The
        # SIDE_LONG branch credits sale proceeds; SHORT debits cover.
        try:
            if side == _SENTINEL_SIDE_LONG:
                tg.paper_cash += float(current_price) * n
            else:
                tg.paper_cash -= float(current_price) * n
        except Exception:
            pass
        # Emit a structured signal so any executor wired into
        # _emit_signal can route the partial. Order type recorded
        # on the action is LIMIT per spec; PR 6 owns the executor
        # swap to actually submit a LIMIT order.
        try:
            tg._emit_signal(
                {
                    "kind": "TITAN_GRIP_PARTIAL",
                    "ticker": ticker,
                    "side": side,
                    "stage": code,
                    "shares": int(n),
                    "price": float(current_price),
                    "order_type": action.order_type,
                    "reason": EXIT_REASON_ALARM_C,
                    "timestamp_utc": tg._utc_now_iso(),
                }
            )
        except Exception:
            pass
        logger.info(
            "[TITAN-GRIP] %s side=%s %s shares=%d price=%.4f order_type=%s",
            ticker,
            side,
            code,
            n,
            float(current_price),
            action.order_type,
        )
        return True
    if code == ACTION_RATCHET:
        # Move the existing pos["stop"] to the new ratchet anchor so
        # the existing manage_positions stop-cross branch fires the
        # runner exit naturally.
        anchor = float(action.price)
        state = pos.get("titan_grip_state")
        if state is not None and state.current_stop_anchor is not None:
            anchor = float(state.current_stop_anchor)
        if side == _SENTINEL_SIDE_LONG:
            old_stop = pos.get("stop") or 0.0
            if anchor > old_stop:
                pos["stop"] = anchor
                logger.info(
                    "[TITAN-GRIP] %s LONG ratchet stop %.4f -> %.4f",
                    ticker,
                    old_stop,
                    anchor,
                )
        else:
            old_stop = pos.get("stop") or 0.0
            if old_stop == 0.0 or anchor < old_stop:
                pos["stop"] = anchor
                logger.info(
                    "[TITAN-GRIP] %s SHORT ratchet stop %.4f -> %.4f",
                    ticker,
                    old_stop,
                    anchor,
                )
        return False
    return False


def _run_sentinel(ticker, side, pos, current_price, bars):
    """v5.13.0 PR 2-3 \u2014 evaluate Tiger Sovereign Sentinel Loop.

    Runs Alarm A (-$500 / -1%/min), Alarm B (5m close vs 9-EMA), AND
    Alarm C (Titan Grip Harvest ratchet) INDEPENDENTLY \u2014 not
    short-circuited. Per the spec: "These Alarms are NOT a sequence."

    Priority on multi-fire (returned exit reason):
      A wins over B and C \u2014 -$500 / velocity is an emergency stop.
      B wins over C \u2014 9-EMA shield is a full close.
      A and B both fired: A's reason wins; both appear in the log.
    The log line lists every fired alarm regardless.

    Returns the sentinel EXIT reason string if any FULL-EXIT alarm
    fires (A or B), else None. Alarm C partial harvests are applied
    in-place (pos["shares"] reduced, stop ratcheted) and return None
    so the caller does NOT close the position; the runner exits
    through the existing manage_positions stop-cross branch when
    the ratcheted stop is hit.

    Side: ``"LONG"`` or ``"SHORT"`` matching the sentinel SIDE_*
    constants.
    """
    try:
        entry_p = pos.get("entry_price")
        shares = int(pos.get("shares") or 0)
        if not entry_p or shares <= 0:
            return None
        if side == _SENTINEL_SIDE_LONG:
            unrealized = (current_price - entry_p) * shares
        else:
            unrealized = (entry_p - current_price) * shares
        position_value = float(entry_p) * shares

        history = pos.get("pnl_history")
        if history is None:
            history = new_pnl_history()
            pos["pnl_history"] = history
        now_ts = _time.time()

        # v5.13.2 P1 #4 \u2014 Alarm A velocity baseline reset on Entry-2 fill.
        # When share count changes (Entry-2 fills, partial harvests), the
        # cached pnl_history holds samples computed against pre-change
        # notional. Computing velocity against new notional produces an
        # artificial spike. Detect the change and rebuild baseline.
        maybe_reset_pnl_baseline_on_shares_change(
            pos,
            history,
            now_ts,
            unrealized,
        )
        record_pnl(history, now_ts, unrealized)

        last_5m_close = None
        last_5m_ema9 = None
        try:
            five = compute_5m_ohlc_and_ema9(bars)
            if five and five.get("seeded"):
                closes_5m = five.get("closes") or []
                if closes_5m:
                    last_5m_close = closes_5m[-1]
                last_5m_ema9 = five.get("ema9")
        except Exception:
            last_5m_close = None
            last_5m_ema9 = None

        # PR 3 \u2014 Alarm C state. Created lazily: if OR_High/OR_Low
        # aren't seeded yet, the Titan Grip arm is skipped silently
        # this tick (state stays None inside evaluate_sentinel).
        grip_state = _ensure_titan_grip(ticker, side, pos)

        result = evaluate_sentinel(
            side=side,
            unrealized_pnl=unrealized,
            position_value=position_value,
            pnl_history=history,
            now_ts=now_ts,
            last_5m_close=last_5m_close,
            last_5m_ema9=last_5m_ema9,
            titan_grip_state=grip_state,
            current_price=current_price,
            current_shares=shares,
        )
        # v5.13.6 \u2014 emit lifecycle PHASE4 / TITAN_GRIP_STAGE events on
        # state changes (best-effort, no-op when logger absent).
        _lifecycle_log_phase4_change(ticker, side, pos, result, current_price)
        if not result.fired:
            return None
        # Always log every fired alarm \u2014 multi-fire trips include
        # both A/B/C codes for observability.
        logger.warning(
            "%s",
            format_sentinel_log(ticker, pos.get("position_id"), result),
        )

        # Priority: if A or B fired, full exit overrides any C
        # actions (don't double-harvest before closing). This is the
        # "A wins" rule. C partial actions are still in
        # result.titan_grip_actions for the log but NOT applied.
        if result.has_full_exit:
            return result.exit_reason

        # Alarm C only path \u2014 apply partial harvests / ratchet in
        # place. The runner exit (C4) is signalled by setting the
        # exit reason; everything else (C1/C2/C3) keeps the position
        # alive with reduced shares / new stop.
        runner_exit = False
        for action in result.titan_grip_actions:
            if action.code == ACTION_RUNNER_EXIT:
                runner_exit = True
                continue
            _apply_titan_grip_partial(
                ticker,
                side,
                pos,
                action,
                current_price,
            )
        if runner_exit:
            return EXIT_REASON_ALARM_C
        return None
    except Exception as e:
        logger.warning("[SENTINEL] error ticker=%s side=%s: %s", ticker, side, e)
        return None


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
        side_label,
        qqq_5m_close,
        qqq_ema9,
        qqq_last,
        qqq_avwap,
    )
    permit_open = bool(permit.get("open"))

    # 1m DI for the appropriate polarity.
    di_streams = tg.v5_di_1m_5m(ticker)
    di_1m_now = di_streams.get("di_plus_1m") if cfg.side.is_long else di_streams.get("di_minus_1m")

    decision = tg.eot_glue.evaluate_entry_2_decision(
        ticker,
        side_label,
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

    # v5.13.7 \u2014 N1: spec L-P3-S6 / S-P3-S6 mandates a 50/50 split by
    # SHARE COUNT, not by dollar notional. Pre-v5.13.7 we computed
    # target_full = floor(PAPER_DOLLARS_PER_ENTRY / current_price) and
    # then E2 = target_full - e1_shares; that produced an asymmetric
    # share split whenever the price drifted between Entry-1 fill and
    # Entry-2 trigger. The spec says "BUY remaining 50%" of a 50/50
    # split, which means E2 == E1 in the typical full-fill case.
    # Defensive fallback: if e1_shares is missing/zero (Entry-1 didn't
    # actually fire \u2014 shouldn't happen), preserve the old dollar-parity
    # behavior so we never silently size to 1 share.
    from eye_of_tiger import ENTRY_1_SIZE_PCT, ENTRY_2_SIZE_PCT  # noqa: F401

    e1_shares = int(pos.get("v5104_entry1_shares") or pos.get("shares") or 0)
    # ENTRY_2_SIZE_PCT participates in the sanity check: full = E1 + E2,
    # so E1+E2 \u2248 1.0. If somebody changes the constants in eye_of_tiger
    # the assertion below catches it before we ship a non-spec sizing.
    assert abs((ENTRY_1_SIZE_PCT + ENTRY_2_SIZE_PCT) - 1.0) < 1e-6, (
        "ENTRY_1_SIZE_PCT + ENTRY_2_SIZE_PCT must sum to 1.0"
    )
    if e1_shares > 0:
        e2_shares = e1_shares
    else:
        target_full = max(1, int(tg.PAPER_DOLLARS_PER_ENTRY // float(current_price)))
        e2_shares = max(1, target_full)
    if e2_shares <= 0:
        return

    # Paper cash: long debits, short credits.
    notional = float(current_price) * e2_shares
    if cfg.side.is_long and notional > tg.paper_cash:
        logger.info(
            "[V5100-ENTRY] %s skip entry_2 \u2014 insufficient cash (need $%.2f, have $%.2f)",
            ticker,
            notional,
            tg.paper_cash,
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
            ticker,
            side_label,
            ("%.2f" % di_1m_now) if di_1m_now is not None else "None",
            fresh_extreme,
            float(current_price),
            e2_shares,
            new_avg,
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

        # v5.13.0 PR 2 \u2014 Tiger Sovereign Sentinel Loop (parallel
        # alarms A & B & C). Spec-literal: A_LOSS=-$500 hard floor,
        # A2=-1% over 60s, B=closed 5m close < 9-EMA, C=Titan Grip
        # Harvest. Alarms are evaluated INDEPENDENTLY (not
        # short-circuited). Sole exit decision-maker as of v5.13.10
        # \u2014 the legacy phase-machine / ladder / RED_CANDLE path
        # was removed when LEGACY_EXITS_ENABLED retired.
        _sentinel_reason = _run_sentinel(
            ticker,
            _SENTINEL_SIDE_LONG,
            pos,
            current_price,
            bars,
        )
        if _sentinel_reason is not None:
            tickers_to_close.append((ticker, current_price, _sentinel_reason))
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
    short_positions = tg.short_positions

    # v3.4.23 \u2014 enforce 0.75% entry cap retroactively on every open
    # short (see manage_positions for rationale). Note: manage_positions
    # and manage_short_positions are called back-to-back by the scan
    # loop, so calling retighten_all_stops from both is redundant-but-
    # cheap. Kept in both for defensive symmetry: if a future refactor
    # reorders or skips one manager, the cap still holds for the other
    # book.
    tg.retighten_all_stops(force_exit=True, fetch_prices=True)

    # v5.9.1: Sovereign Regime Shield (PDC eject) retired on the short
    # side too. v5.13.10: per-ticker POLARITY_SHIFT exit also retired
    # along with the rest of the legacy phase-machine path.

    for ticker in list(short_positions.keys()):
        pos = short_positions[ticker]

        bars = tg.fetch_1min_bars(ticker)
        if not bars:
            continue
        current_price = bars["current_price"]

        # v5.13.0 PR 2 \u2014 Tiger Sovereign Sentinel Loop (short side
        # mirror). Alarm A: -$500 / -1%/min. Alarm B: 5m close ABOVE
        # 9-EMA fires. Alarms run in parallel; sole exit path as of
        # v5.13.10.
        _sentinel_reason_s = _run_sentinel(
            ticker,
            _SENTINEL_SIDE_SHORT,
            pos,
            current_price,
            bars,
        )
        if _sentinel_reason_s is not None:
            tg.close_short_position(ticker, current_price, reason=_sentinel_reason_s)
            continue
