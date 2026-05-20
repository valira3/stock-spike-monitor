"""orb.engine -- v10 ORB live engine public surface.

Brings together the foundation modules (state, risk_book, day_gates,
exits) into a single API that the live engine layer (engine/scan.py)
can call without knowing the internals.

Architecture (per-bar callback flow):

  on_bar_arrival(portfolio_ids, ticker, bar):
    1. ORWindow.add_bar() if bar is inside the OR window
    2. If bar is the LAST OR bar (bucket == or_end - 1), lock the OR
       window for this ticker
    3. For each portfolio_id, evaluate per-portfolio FSM transitions:
         - WARMUP -> OR_LOCKED at OR close
         - OR_LOCKED -> blocked or armed based on day gates
    4. If portfolio is ARMED on this ticker AND a 5m candle just closed
       past the OR boundary, return a BreakoutSignal (caller will
       attempt admission via try_enter())

  try_enter(portfolio_id, signal) -> Admission | None:
    Calls RiskBook.try_admit(); returns None on rejection. On
    acceptance, returns an Admission containing the OrbPosition + the
    risk ticket id for later release.

  on_exit(portfolio_id, position, exit_decision):
    Releases the risk ticket; transitions FSM to CLOSED; logs forensic.

Multi-portfolio note: callers iterate over PORTFOLIOS and call
try_enter() per portfolio. The engine handles per-portfolio state
internally; callers just need to know which portfolio is making the
decision.

Look-ahead audit per rule #7b:

  - on_bar_arrival is called only for bars that have ALREADY closed.
    The OR window's add_bar() rejects any bar with bucket >= or_end.
  - Day gates are evaluated at session start using prior-day VIX +
    public earnings calendar + prior-session close + today's 09:30
    open. All causally clean (see orb/day_gates.py).
  - Breakout signals are detected on the close of a 5m candle, fired
    on the next 5m candle's open. The engine returns the signal but
    does NOT execute -- caller decides whether to act.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from orb import day_gates as _day_gates
from orb import exits as _exits
from orb import risk_book as _risk_book
from orb import state as _state


# v9.1.7 -- ET timezone constants (no zoneinfo dep so module remains
# import-clean in older runtimes). EDT/EST is selected by US DST window
# checked below. ET = UTC offset; the cutoff comparison only cares about
# absolute minutes-since-midnight so the DST switch is the only nuance.
_ET_DST_OFFSET = timedelta(hours=-4)
_ET_STD_OFFSET = timedelta(hours=-5)


def _utc_iso_to_et_minutes(iso_utc: str) -> Optional[int]:
    """Convert a UTC ISO 8601 timestamp to ET minutes-since-midnight.

    Used by try_enter() to compare the signal bar's timestamp against
    cfg.time_cutoff_minutes. Returns None if the input is malformed --
    callers treat that as "fail-open" (no cutoff applied) so a single
    malformed timestamp can't strand the engine.
    """
    if not iso_utc:
        return None
    try:
        # Accept both 'Z' suffix and explicit '+00:00'. Strip 'Z' for
        # fromisoformat which doesn't accept it on older Pythons.
        s = iso_utc.replace("Z", "+00:00") if iso_utc.endswith("Z") else iso_utc
        dt_utc = datetime.fromisoformat(s)
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        # Detect US DST window: second Sunday of March -> first Sunday
        # of November. We approximate via month boundaries -- the only
        # case where this matters for the cutoff is the 9:30-11:00 ET
        # window and the offset boundary days never fall in market
        # hours, so month-level granularity is fine for the cutoff
        # check (the cutoff itself is precision to the minute).
        m = dt_utc.month
        offset = _ET_DST_OFFSET if 3 <= m <= 10 else _ET_STD_OFFSET
        # March + November have intra-month DST transitions. For our
        # purposes (entry-cutoff comparison) treat the whole month as
        # the dominant regime; if/when the operator changes the cutoff
        # to a value crossing 02:00 ET (when DST flips), revisit.
        dt_et = dt_utc + offset
        return dt_et.hour * 60 + dt_et.minute
    except (ValueError, TypeError):
        return None


logger = logging.getLogger(__name__)


# ----- Configuration --------------------------------------------------


@dataclass
class OrbConfig:
    """All v10 anchor knobs in one place. Defaults match v10 keystone.

    Construct from env vars via from_env() in production.
    """

    or_minutes: int = 30
    rr: float = 2.5
    stop_buffer_bps: float = 5.0
    range_min_pct: float = 0.008
    range_max_pct: float = 0.025
    max_trades_per_day: int = 5
    risk_per_trade_pct: float = 1.0
    max_concurrent_risk_dollars: float = 2000.0
    max_concurrent_notional_mult: float = 2.0
    max_trade_notional_pct: float = 75.0
    daily_loss_kill_pct: float = 2.0
    move_to_be_after_1r: bool = True
    eod_cutoff_minutes: int = 15 * 60 + 55  # 15:55 ET
    session_start_minutes: int = 9 * 60 + 30  # 09:30 ET
    # v9.1.7 -- entry-window cutoff. No new entries are admitted at or
    # after this ET wall-clock minute. v9.0.0 shipped assuming this
    # would be wired to the live engine (v12/v13 backtest projections
    # of +$24,784/yr / 0/4 neg quarters were computed with the R12
    # cutoff of 11:00 ET) but the env var ORB_TIME_CUTOFF_ET was only
    # honored by tools/orb_backtest.py -- the live engine had no
    # time-cutoff field at all. Setting this to 0 disables the
    # cutoff (entries allowed until eod_cutoff_minutes).
    time_cutoff_minutes: int = 11 * 60  # 11:00 ET (R12 winner)
    # Day-gate config (forwarded to evaluate_day)
    skip_vix_above: float = 22.0
    skip_earnings_window: bool = True
    earnings_days_before: int = 1
    earnings_days_after: int = 0
    skip_gap_above_pct: float = 1.5
    fail_closed_on_missing_vix: bool = True  # True for live; False for backtest parity
    ticker_side_blocklist: dict = None  # type: ignore
    # v8.0.0 -- ATR-based stop. If > 0, replaces the OR-edge stop with
    # entry +/- atr_stop_mult * ATR(14) on 5m bars. Caller (scan.py)
    # supplies the recent 5m HLC history to detect_breakout(). When the
    # ATR window isn't warm yet (<2 bars), the engine silently falls
    # back to the OR-edge stop so the strategy is never stop-less.
    atr_stop_mult: float = 0.0
    atr_lookback_5m: int = 14  # standard Wilder ATR lookback
    # v8.1.0 -- partial-profit-at-1R. When True, the exit evaluator
    # emits EXIT_PARTIAL on first 1R touch, signalling the caller to
    # sell half. Defaults False so v8.1.0 is strictly additive until
    # the operator flips ORB_PARTIAL_PROFIT_AT_1R=1 in Railway env.
    partial_profit_at_1r: bool = False
    # v8.3.34 -- day-end-giveback defenses (R6 sweep winners).
    # Both default 0.0 = off (R10/R12 research showed they hurt
    # when stacked with chase-prevention, so stay off in v9).
    loss_lock_threshold_usd: float = 0.0
    peak_dd_halt_usd: float = 0.0
    # v9.0.0 -- chase-prevention filters (R10 + R10b research).
    # Defaults ON: per the v13 report, this filter set + the env
    # vars (cut=11, VIX<=20, daily_loss_kill=1.0) produce a
    # backtested +$24,784/yr / 0/4 neg quarters / 24.8% CAGR /
    # Sharpe 2.80.
    min_break_bps: float = 5.0
    max_vwap_dev_bps: float = 25.0
    # Mega-cap chase-fence: filter only applies to these tickers.
    # Empty tuple = filter applies globally (do not change in v9).
    max_vwap_dev_tickers: tuple = (
        "META",
        "MSFT",
        "AAPL",
        "AMZN",
        "GOOG",
        "AVGO",
    )
    # v9.1.124 -- OR-retracement gate. When set > 0, an entry is
    # rejected at try_enter time if the proposed entry price has
    # retraced past the OR boundary by more than this tolerance.
    # Motivating case (AVGO 2026-05-18 10:10 ET): short signal fired
    # on 5m close below OR_low ($416.30), but the VWAP-chase gate
    # blocked entry until price had rallied to $418.37 (49bps inside
    # the OR range). The combination of "fire on 5m close" + "wait
    # for VWAP-allowed entry" lets stale signals execute at prices
    # that violate the breakout premise. Default 25 bps is the same
    # tolerance the dashboard monitor's or_break invariant uses for
    # WARN classification. Set to 0.0 to disable.
    or_retracement_tolerance_bps: float = 25.0
    # R21 (v9.1.x) -- runner_eod_prep: time-based forced exit on the
    # runner half AFTER the partial-at-1R fires. 0 = off; production
    # winner = 14*60 = 14:00 ET. Quarterly stability check showed
    # +$2,414/yr (+12.7%) on Val ($30k) vs Keystone baseline, with
    # no negative quarters. The signal is: "after locking half at 1R,
    # the runner has typically peaked by mid-afternoon; afternoon
    # chop tends to give back unrealized gains." Time-based exit
    # captures the local max without bar-level MFE tracking.
    runner_eod_prep_minutes: int = 0
    # R26 (v9.1.130) -- stale FULL-position exit. Mirror of R21 for the
    # un-partialed cohort: close the WHOLE position at this ET-minute
    # if the 1R partial hasn't fired yet. 0 = off; production winner =
    # 14*60+30 = 14:30 ET. Optional MFE-in-R floor (stale_full_exit_mfe_floor_r)
    # spares trades that came close to 1R. Quarterly stability: 6 of 8
    # Val/Main quarter-cells positive, +$2,955/yr combined; worst quarter
    # -$1,086 on Main Q3'25. Replaces the legacy sentinel A safety net
    # that v9.1.128 portfolio independence removed for Val/Gene.
    stale_full_exit_minutes: int = 0
    stale_full_exit_mfe_floor_r: float = 0.0
    # R32 (v9.1.134) -- asymmetric reversal circuit-breaker. Closes the
    # position when it WAS clearly favorable (MFE_R >= min_mfe_r) AND
    # has now given back at least min_giveback_r in round-trip swing.
    # Catches the rare clear round-trip (e.g. NFLX wild-swing days)
    # without affecting trending winners. Both 0 = disabled (default).
    # Production thresholds (operator-approved 2026-05-19):
    #   ORB_REVERSAL_CIRCUIT_MIN_MFE_R=1.0
    #   ORB_REVERSAL_CIRCUIT_MIN_GIVEBACK_R=1.5
    # ~23 fires/year on 252-day rth-expand corpus, cost -$21/yr (noise).
    reversal_circuit_min_mfe_r: float = 0.0
    reversal_circuit_min_giveback_r: float = 0.0
    # v9.0.0 -- prior-day SPY regime gate (R12 research). Default
    # threshold -40 bps; skip the whole day if prior session SPY
    # close-to-close return was below this. Set to 0.0 to disable.
    skip_prior_spy_ret_lt_bps: float = -40.0
    fail_closed_on_missing_spy: bool = False  # fail-open: trade if
    # SPY daily feed
    # missing (data feed
    # outage shouldn't
    # strand the system)

    @property
    def or_end_minutes(self) -> int:
        return self.session_start_minutes + self.or_minutes


# ----- Signals + admissions ------------------------------------------


@dataclass
class BreakoutSignal:
    """A signal candidate generated by on_bar_arrival. Caller may pass
    this to try_enter() to attempt admission."""

    portfolio_id: str
    ticker: str
    side: str  # "long" or "short"
    signal_bar_close_iso: str  # timestamp of the 5m bar that triggered
    signal_bar_close: float  # close price at signal
    or_high: float
    or_low: float
    proposed_stop: float  # final stop fed to risk-book sizing
    proposed_entry: float  # next 5m candle open (caller fills this in at fire)
    # v8.0.0 -- forensic: how the stop was derived ("or_edge" | "atr"),
    # plus the ATR value that fed the calc (None when atr branch off /
    # not warm). Used by the [V79-ORB-ENTRY] log emitter to surface
    # which stop mode actually fired live.
    stop_source: str = "or_edge"
    atr_used: Optional[float] = None


@dataclass
class Admission:
    """Returned by try_enter() on success. None on rejection."""

    position: _exits.OrbPosition
    risk_ticket: object  # _risk_book._Ticket; opaque to caller


# ----- ATR helper (v8.0.0) -------------------------------------------


def atr_from_5m(
    highs: list[float], lows: list[float], closes: list[float], lookback: int = 14
) -> float:
    """Wilder ATR on 5m bars. Returns 0.0 when fewer than 2 bars provided
    (caller's contract: fall back to OR-edge stop). Closed-form mean of
    True Range over the last `lookback+1` bars; the first TR can only be
    computed against the previous bar's close, so a series of N bars
    yields N-1 TRs and we average up to `lookback` of those.

    Look-ahead clean: True Range for bar i uses bar i's high/low and
    bar i-1's close. No data from i+1 or later contributes.
    """
    n = min(len(highs), len(lows), len(closes))
    if n < 2:
        return 0.0
    trs: list[float] = []
    for i in range(1, n):
        h = highs[i]
        lo = lows[i]
        prev_close = closes[i - 1]
        if h is None or lo is None or prev_close is None:
            continue
        tr = max(h - lo, abs(h - prev_close), abs(lo - prev_close))
        trs.append(tr)
    if not trs:
        return 0.0
    window = trs[-lookback:] if len(trs) > lookback else trs
    return sum(window) / len(window)


# ----- Public surface ------------------------------------------------


class OrbEngine:
    """Coordinates state, day gates, risk books across portfolios.

    Single instance per process. Callers construct via:
        engine = OrbEngine(cfg, portfolio_ids=["main", "val", "gene"],
                           is_earnings_window_fn=...)
    """

    def __init__(
        self,
        cfg: OrbConfig,
        portfolio_ids: list[str],
        *,
        is_earnings_window_fn=None,
        vix_close_loader=None,
    ) -> None:
        self.cfg = cfg
        self.portfolio_ids = list(portfolio_ids)
        self._earnings_fn = is_earnings_window_fn
        self._vix_loader = vix_close_loader  # callable () -> dict[date, float]

        self._state = _state.OrbStateRegistry()
        self._risk = _risk_book.RiskBookRegistry()
        for pid in self.portfolio_ids:
            self._risk.register(
                pid,
                max_concurrent_risk_dollars=cfg.max_concurrent_risk_dollars,
                max_concurrent_notional_mult=cfg.max_concurrent_notional_mult,
                equity=100_000.0,  # caller refresh via update_equity
                daily_loss_kill_pct=cfg.daily_loss_kill_pct,
                # v8.3.34 -- day-end-giveback defenses
                loss_lock_threshold_usd=cfg.loss_lock_threshold_usd,
                peak_dd_halt_usd=cfg.peak_dd_halt_usd,
            )

        # Cached day-gate result (computed once per session)
        self._day_result: Optional[_day_gates.DayGateResult] = None
        self._day_result_date: str = ""

        # v8.1.8 -- wash-sale risk tracker. NOT tax-grade -- this is
        # operator-facing signaling for §1091 wash-sale visibility.
        # When a position closes at a LOSS, we record (ticker, side,
        # ts, pnl_dollars) here. On the next try_enter() for the SAME
        # (ticker, side), if a recorded loss falls within the last
        # 30 calendar days, we log [V81-WASH-RISK] and increment the
        # session counter. The entry is NEVER blocked -- the operator
        # opted into this strategy knowing about the rule (and
        # typically files §475(f) MTM which exempts them anyway).
        #
        # Bounded by recency (>30d entries are pruned on each check).
        # No persistence across Railway restarts; this is in-memory
        # session signaling only. For year-end tax reconciliation
        # the operator should rely on the broker 1099-B + their
        # accountant, NOT this counter.
        from collections import defaultdict as _dd

        self._recent_losses: dict = _dd(list)
        # Session-scoped counter -- resets to 0 each new session.
        self.wash_risk_count: int = 0
        # v9.0.0 -- session-scoped rejection counters for chase filters.
        # Reset in start_new_session() and exposed via snapshot() for
        # the dashboard.
        self._mbr_reject_count: int = 0
        self._vwap_chase_reject_count: int = 0
        # v9.1.7 -- new entries blocked after cfg.time_cutoff_minutes.
        self._time_cutoff_reject_count: int = 0
        # v9.1.124 -- entries blocked when price retraced past OR boundary.
        self._or_retrace_reject_count: int = 0

    # --- session lifecycle ---

    def backfill_or_windows(self, *, bars_by_ticker: dict, current_et_minutes: int) -> dict:
        """v8.3.0 -- replay pre-bucketed 1m bars to rebuild any OR
        window that wasn't locked in real-time.

        Why: a Railway redeploy mid-RTH wipes in-memory engine state.
        On the next bootstrap + ensure_session_started, all OR windows
        start empty. The live scan only feeds the latest bar per
        cycle, so 09:30-09:59 bars are never replayed -> WARMUP
        forever -> zero trades the rest of the day. Without this
        function, a redeploy at 10:00 ET cooks the entire trading
        day.

        Idempotent: bars going to a locked OR window are silently
        rejected by add_bar() (orb.state.OrWindow.add_bar drops
        anything when locked=True). Safe to call on every scan cycle.
        Tickers already locked are skipped fast.

        No-op when current_et_minutes < or_end_minutes (the live scan
        will populate OR windows normally during the active OR).

        Args:
            bars_by_ticker: {ticker: list[row]} where each row is a
                tuple (or list / dict-like with index access) of
                (bucket_min, high, low, open, close, volume). The
                caller is responsible for converting raw timestamps
                to ET-minute buckets (since orb/ doesn't import
                from engine/ to keep the dependency graph clean).
                Bars are filtered here -- in-OR bars feed
                on_bar_arrival; the first post-OR bar feeds
                on_bar_arrival to trigger the v7.73.0 lock fallback
                if 09:59 was missing.
            current_et_minutes: minutes since ET midnight (now).
                Used to short-circuit when we're still inside or
                before the OR window.

        Returns:
            {"backfilled": int, "locked": int, "skipped": int,
             "failed": int} -- per-ticker counters. "skipped" counts
             tickers that were already locked OR the entire call
             when we're pre-or-end; "failed" counts tickers with no
             usable bars; "backfilled" counts tickers that received
             at least one in-window bar; "locked" counts tickers
             that were force-locked via a post-window bar.
        """
        out = {"backfilled": 0, "locked": 0, "skipped": 0, "failed": 0}
        cfg = self.cfg
        if current_et_minutes < cfg.or_end_minutes:
            out["skipped"] = len(bars_by_ticker)
            return out
        for ticker, rows in bars_by_ticker.items():
            w = self._state.or_windows.get(ticker)
            if w is not None and w.locked:
                out["skipped"] += 1
                continue
            if not rows:
                out["failed"] += 1
                continue
            fed_in_window = 0
            for r in rows:
                try:
                    bucket = int(r[0])
                    hi = float(r[1])
                    lo = float(r[2])
                    op = float(r[3])
                    cl = float(r[4])
                    vo = float(r[5] or 0.0) if len(r) > 5 else 0.0
                except (TypeError, ValueError, IndexError):
                    continue
                if bucket < cfg.session_start_minutes:
                    continue
                if bucket >= cfg.or_end_minutes:
                    continue
                try:
                    self.on_bar_arrival(
                        ticker=ticker,
                        bar_high=hi,
                        bar_low=lo,
                        bar_open=op,
                        bar_close=cl,
                        bar_volume=vo,
                        bar_bucket_min=bucket,
                    )
                    fed_in_window += 1
                except Exception as e:
                    logger.debug(
                        "[V83-OR-BACKFILL] feed_bar %s b=%d: %s",
                        ticker,
                        bucket,
                        e,
                    )
            if fed_in_window == 0:
                out["failed"] += 1
                continue
            out["backfilled"] += 1
            # Pass 2: force-lock via the first post-window bar so the
            # OR locks even when the 09:59 bucket was missing from
            # the source (Alpaca IEX occasionally drops a bar).
            w2 = self._state.or_windows.get(ticker)
            if w2 is not None and not w2.locked:
                for r in rows:
                    try:
                        bucket = int(r[0])
                        hi = float(r[1])
                        lo = float(r[2])
                        op = float(r[3])
                        cl = float(r[4])
                        vo = float(r[5] or 0.0) if len(r) > 5 else 0.0
                    except (TypeError, ValueError, IndexError):
                        continue
                    if bucket < cfg.or_end_minutes:
                        continue
                    try:
                        self.on_bar_arrival(
                            ticker=ticker,
                            bar_high=hi,
                            bar_low=lo,
                            bar_open=op,
                            bar_close=cl,
                            bar_volume=vo,
                            bar_bucket_min=bucket,
                        )
                        out["locked"] += 1
                    except Exception as e:
                        logger.debug(
                            "[V83-OR-BACKFILL] post-bar %s b=%d: %s",
                            ticker,
                            bucket,
                            e,
                        )
                    break  # one post-window bar is enough to lock
        return out

    def start_new_session(
        self,
        *,
        date_iso: str,
        tickers: list[str],
        vix_close_d1: Optional[float],
        ticker_open_today: dict[str, Optional[float]],
        ticker_prev_close: dict[str, Optional[float]],
        equity_per_portfolio: dict[str, float],
        spy_prior_ret_bps: Optional[float] = None,
    ) -> _day_gates.DayGateResult:
        """Reset all state for a new trading day.

        Called once at session start (e.g. at 09:25 ET pre-warm). After
        this, on_bar_arrival can be called for the first 09:30 bar.
        """
        # 1. Reset state
        self._state.reset_for_new_session(date_iso)
        # v8.1.8 -- reset session-scoped wash-risk counter. The
        # _recent_losses buffer is INTENTIONALLY NOT cleared here --
        # a losing close on Monday + new entry Tuesday still hits
        # the 30-day window and should fire the flag.
        self.wash_risk_count = 0
        # v9.0.0 -- reset chase-filter rejection counters.
        self._mbr_reject_count = 0
        self._vwap_chase_reject_count = 0
        self._time_cutoff_reject_count = 0
        # v9.1.124 -- reset OR-retracement rejection counter.
        self._or_retrace_reject_count = 0
        # v7.33.0: update equity BEFORE reset_all_sessions so the
        # session-start equity snapshot inside RiskBook.reset_session
        # captures the actual session-start equity (not the stale
        # registration default). The daily-loss kill threshold reads
        # _session_start_equity, so this order matters.
        for pid, eq in equity_per_portfolio.items():
            rb = self._risk.get(pid)
            if rb is not None:
                rb.update_equity(eq)
        self._risk.reset_all_sessions()

        # 2. Evaluate day gates ONCE for this session
        gate_cfg = _day_gates.DayGateConfig(
            skip_vix_above=self.cfg.skip_vix_above,
            fail_closed_on_missing_vix=self.cfg.fail_closed_on_missing_vix,
            skip_earnings_window=self.cfg.skip_earnings_window,
            earnings_days_before=self.cfg.earnings_days_before,
            earnings_days_after=self.cfg.earnings_days_after,
            skip_gap_above_pct=self.cfg.skip_gap_above_pct,
            ticker_side_blocklist=self.cfg.ticker_side_blocklist,
            skip_prior_spy_ret_lt_bps=self.cfg.skip_prior_spy_ret_lt_bps,
            fail_closed_on_missing_spy=self.cfg.fail_closed_on_missing_spy,
        )
        self._day_result = _day_gates.evaluate_day(
            gate_cfg,
            date_iso=date_iso,
            vix_close_d1=vix_close_d1,
            tickers=tickers,
            ticker_open_today=ticker_open_today,
            ticker_prev_close=ticker_prev_close,
            is_earnings_window_fn=self._earnings_fn,
            spy_prior_ret_bps=spy_prior_ret_bps,
        )
        self._day_result_date = date_iso

        # 3. Pre-populate per-portfolio per-ticker FSM with initial blocks
        for pid in self.portfolio_ids:
            for tk in tickers:
                ds = self._state.get_day_state(pid, tk)
                if self._day_result.block_day:
                    ds.transition(_state.PHASE_BLOCKED_VIX, reason=self._day_result.block_reason)
                    continue
                tres = self._day_result.per_ticker.get(tk)
                if tres is not None and tres.blocked:
                    if "blocklist" in tres.block_reason:
                        ds.transition(_state.PHASE_BLOCKED_BLOCKLIST, reason=tres.block_reason)
                    elif "earnings" in tres.block_reason:
                        ds.transition(_state.PHASE_BLOCKED_EARNINGS, reason=tres.block_reason)
                    elif "gap" in tres.block_reason:
                        ds.transition(_state.PHASE_BLOCKED_GAP, reason=tres.block_reason)
                # Else: leave at PHASE_WARMUP
        return self._day_result

    # --- per-bar event ---

    def on_bar_arrival(
        self,
        *,
        ticker: str,
        bar_high: float,
        bar_low: float,
        bar_open: float,
        bar_close: float,
        bar_volume: float,
        bar_bucket_min: int,
    ) -> None:
        """Feed a 1-min bar to the OR window. Idempotent on bars after
        OR lock (window rejects them silently).

        This is the ONLY way the OR window grows. Callers must invoke
        once per bar arrival. Order matters: 09:30, 09:31, ..., 09:59
        are accepted; 10:00+ are rejected.

        If this bar is the last bar of the OR window, the window is
        locked and per-portfolio FSMs transition WARMUP -> OR_LOCKED ->
        ARMED (or the appropriate block phase).
        """
        cfg = self.cfg
        w = self._state.get_or_window(ticker, cfg.or_minutes)
        added = w.add_bar(
            bar_high=bar_high,
            bar_low=bar_low,
            bar_open=bar_open,
            bar_close=bar_close,
            bar_volume=bar_volume,
            bar_bucket_min=bar_bucket_min,
            or_end_min=cfg.or_end_minutes,
        )
        if not added:
            # v7.73.0 -- defensive fallback. Pre-v7.73.0 the OR lock was
            # gated on `bar_bucket_min == or_end_minutes - 1` (a strict
            # equality match on the very last bar). If that exact bar
            # was missed -- bot down for a Railway redeploy during the
            # OR window, bar source returned None for that minute, off-
            # by-one in bucket math, etc. -- the OR window NEVER locked
            # for the rest of the day. add_bar() rejects any bar with
            # bucket >= or_end_min, so no subsequent bar could trigger
            # the lock either. Symptom: WARMUP forever, 0/N tickers
            # locked, no trades possible.
            #
            # Catch the missed-last-bar case here: if a post-window bar
            # arrives and the OR is still unlocked, lock now using
            # whatever data we accumulated. _lock_and_arm internally
            # routes to BLOCKED_OR_INSUFFICIENT when bars_seen <
            # or_minutes // 2, so a thin window correctly blocks
            # entries rather than firing on garbage OR bounds.
            if not w.locked and bar_bucket_min >= cfg.or_end_minutes and w.bars_seen > 0:
                self._lock_and_arm(ticker, w)
            return  # outside OR window or already locked

        # If this was the last bar in the OR window, lock now.
        if bar_bucket_min == cfg.or_end_minutes - 1:
            self._lock_and_arm(ticker, w)

    def _lock_and_arm(self, ticker: str, w: _state.OrWindow) -> None:
        """Lock the OR window + transition portfolios to ARMED if eligible."""
        # Use ticker name in iso for forensic; not a real timestamp.
        w.lock(locked_at_iso=f"or_close.{ticker}")

        # Range-band check (uses width % vs configured min/max)
        cfg = self.cfg
        width = w.or_width_pct or 0.0
        range_ok = cfg.range_min_pct <= width <= cfg.range_max_pct

        for pid in self.portfolio_ids:
            ds = self._state.get_day_state(pid, ticker)
            if ds.is_blocked():
                continue  # already blocked from session start
            if not range_ok:
                ds.transition(
                    _state.PHASE_BLOCKED_RANGE,
                    reason=f"width {width * 100:.2f}% outside "
                    f"[{cfg.range_min_pct * 100:.2f}, "
                    f"{cfg.range_max_pct * 100:.2f}]%",
                )
                continue
            if w.bars_seen < cfg.or_minutes // 2:
                ds.transition(
                    _state.PHASE_BLOCKED_OR_INSUFFICIENT,
                    reason=f"{w.bars_seen} bars seen, expected {cfg.or_minutes}",
                )
                continue
            ds.transition(_state.PHASE_ARMED)

    # --- entry path ---

    def detect_breakout(
        self,
        *,
        portfolio_id: str,
        ticker: str,
        five_min_close: float,
        five_min_close_iso: str,
        next_open: float,
        recent_5m_highs: Optional[list[float]] = None,
        recent_5m_lows: Optional[list[float]] = None,
        recent_5m_closes: Optional[list[float]] = None,
    ) -> Optional[BreakoutSignal]:
        """Check whether a 5m bar's close just broke past the OR
        boundary AND the portfolio's FSM allows a new entry.

        Returns a BreakoutSignal on a fresh signal; None otherwise.

        v8.0.0 -- if `cfg.atr_stop_mult > 0` AND `recent_5m_*` lists are
        supplied AND the ATR(14) window is warm (>=2 bars), the
        BreakoutSignal's `proposed_stop` is computed from ATR instead
        of the OR edge. The `stop_source` field on the signal records
        which branch fired ("or_edge" | "atr"). Cold-ATR ("warm-up")
        falls back to the OR-edge stop transparently so the strategy
        is never stop-less.
        """
        cfg = self.cfg
        ds = self._state.get_day_state(portfolio_id, ticker)
        if not ds.can_enter(cfg.max_trades_per_day):
            return None
        w = self._state.or_windows.get(ticker)
        if w is None or not w.locked:
            return None

        buffer_pct = cfg.stop_buffer_bps / 10000.0

        # Pre-compute ATR (independent of side) so both branches can
        # use the same value + same warmup-fallback decision.
        atr = 0.0
        if cfg.atr_stop_mult > 0 and recent_5m_highs and recent_5m_lows and recent_5m_closes:
            atr = atr_from_5m(
                recent_5m_highs,
                recent_5m_lows,
                recent_5m_closes,
                lookback=cfg.atr_lookback_5m,
            )

        # Long signal: 5m close strictly above OR_high
        if five_min_close > w.or_high:
            or_edge_stop = w.or_low * (1.0 - buffer_pct)
            if atr > 0:
                atr_stop = next_open - cfg.atr_stop_mult * atr
                stop = atr_stop
                stop_source = "atr"
            else:
                stop = or_edge_stop
                stop_source = "or_edge"
            return BreakoutSignal(
                portfolio_id=portfolio_id,
                ticker=ticker,
                side="long",
                signal_bar_close_iso=five_min_close_iso,
                signal_bar_close=five_min_close,
                or_high=w.or_high,
                or_low=w.or_low,
                proposed_stop=stop,
                proposed_entry=next_open,
                stop_source=stop_source,
                atr_used=(atr if atr > 0 else None),
            )
        # Short signal: 5m close strictly below OR_low
        if five_min_close < w.or_low:
            or_edge_stop = w.or_high * (1.0 + buffer_pct)
            if atr > 0:
                atr_stop = next_open + cfg.atr_stop_mult * atr
                stop = atr_stop
                stop_source = "atr"
            else:
                stop = or_edge_stop
                stop_source = "or_edge"
            return BreakoutSignal(
                portfolio_id=portfolio_id,
                ticker=ticker,
                side="short",
                signal_bar_close_iso=five_min_close_iso,
                signal_bar_close=five_min_close,
                or_high=w.or_high,
                or_low=w.or_low,
                proposed_stop=stop,
                proposed_entry=next_open,
                stop_source=stop_source,
                atr_used=(atr if atr > 0 else None),
            )
        return None

    def try_enter(
        self,
        signal: BreakoutSignal,
        *,
        equity: float,
        fill_price: Optional[float] = None,
        session_vwap: Optional[float] = None,
    ) -> Optional[Admission]:
        """Attempt admission for a breakout signal. Returns Admission on
        success, None if RiskBook rejected.

        If `fill_price` is provided, it OVERRIDES `signal.proposed_entry`
        (use this when the broker reported a different fill).

        v9.0.0 -- two new pre-admission filters:
          * min_break_bps: reject if the signal-bar close is too close
            to OR boundary (weak breakout)
          * max_vwap_dev_bps: reject if entry price has chased too far
            past session VWAP in the breakout direction (apply only to
            fenced tickers if max_vwap_dev_tickers is non-empty)
        Both are short-circuited when their threshold is 0.

        Position is constructed but NOT recorded on any external book;
        caller must call on_exit() with the same position when it closes
        to release the risk ticket.
        """
        cfg = self.cfg
        if signal is None:
            # Defensive: detect_breakout returns None when no breakout
            # fires this tick. Callers should check, but tests have
            # been observed to pass None through, and the v10
            # daily-loss kill in v7.29.0 can transition the FSM
            # between detect_breakout and try_enter such that the
            # caller no longer has a signal to act on.
            return None
        ds = self._state.get_day_state(signal.portfolio_id, signal.ticker)
        if not ds.can_enter(cfg.max_trades_per_day):
            return None
        rb = self._risk.get(signal.portfolio_id)
        if rb is None:
            return None

        # v9.1.7 -- entry-time cutoff. Reject when the signal bar's ET
        # wall-clock minute is at or past cfg.time_cutoff_minutes. Per
        # R12 backtest research the morning-only window (9:30 -> 11:00
        # ET) is materially more profitable than the all-day window;
        # admitting afternoon entries was a documented backtest-live
        # drift bug from v9.0.0 through v9.1.6.
        # Fail-open on malformed timestamp (None) so a single bad bar
        # can't strand the engine. Setting cutoff to 0 also disables.
        if cfg.time_cutoff_minutes > 0:
            sig_et = _utc_iso_to_et_minutes(signal.signal_bar_close_iso)
            if sig_et is not None and sig_et >= cfg.time_cutoff_minutes:
                self._time_cutoff_reject_count += 1
                logger.info(
                    "[V917-TIME-CUTOFF-REJECT] %s/%s %s sig_et=%d:%02d >= cutoff=%d:%02d",
                    signal.portfolio_id,
                    signal.ticker,
                    signal.side,
                    sig_et // 60,
                    sig_et % 60,
                    cfg.time_cutoff_minutes // 60,
                    cfg.time_cutoff_minutes % 60,
                )
                return None

        # Use the actual fill price if provided
        entry_price = fill_price if fill_price is not None else signal.proposed_entry
        risk_per_share = abs(entry_price - signal.proposed_stop)
        if risk_per_share <= 0.001:
            return None

        # v9.0.0 -- min_break_bps: reject weak breakouts. Measured on
        # the signal-bar close vs the OR boundary in the breakout's
        # natural direction; independent of any fenced-ticker logic.
        if cfg.min_break_bps > 0:
            if signal.side == "long" and signal.or_high > 0:
                break_bps = (signal.signal_bar_close - signal.or_high) / signal.or_high * 10000.0
            elif signal.side == "short" and signal.or_low > 0:
                break_bps = (signal.or_low - signal.signal_bar_close) / signal.or_low * 10000.0
            else:
                break_bps = 0.0
            if break_bps < cfg.min_break_bps:
                self._mbr_reject_count += 1
                logger.info(
                    "[V900-MBR-REJECT] %s/%s %s break=%.1fbps < threshold=%.1fbps",
                    signal.portfolio_id,
                    signal.ticker,
                    signal.side,
                    break_bps,
                    cfg.min_break_bps,
                )
                return None

        # v9.0.0 -- max_vwap_dev_bps: chase-prevention. Reject when
        # entry has extended too far past session VWAP in the breakout
        # direction. Applies globally if max_vwap_dev_tickers is empty;
        # otherwise only to the fenced ticker set (R10 winning config:
        # fence applied to META/MSFT/AAPL/AMZN/GOOG/AVGO only).
        # When session_vwap is missing or zero we fail OPEN (allow the
        # entry); the caller's scan loop is responsible for supplying it.
        if cfg.max_vwap_dev_bps > 0 and session_vwap is not None and session_vwap > 0:
            fence = cfg.max_vwap_dev_tickers or ()
            if not fence or signal.ticker in fence:
                if signal.side == "long":
                    dev_bps = (entry_price - session_vwap) / session_vwap * 10000.0
                else:
                    dev_bps = (session_vwap - entry_price) / session_vwap * 10000.0
                if dev_bps > cfg.max_vwap_dev_bps:
                    self._vwap_chase_reject_count += 1
                    logger.info(
                        "[V900-VWAP-CHASE] %s/%s %s entry=%.2f vwap=%.2f dev=%.1fbps "
                        "> threshold=%.1fbps",
                        signal.portfolio_id,
                        signal.ticker,
                        signal.side,
                        entry_price,
                        session_vwap,
                        dev_bps,
                        cfg.max_vwap_dev_bps,
                    )
                    return None

        # v9.1.124 -- OR-retracement gate. The 5m-close trigger
        # (detect_breakout) and the actual entry price (proposed_entry
        # = current price at scan time) are decoupled, so the
        # VWAP-chase gate above can stall a signal until price has
        # retraced back inside the OR range. When that happens the
        # entry no longer reflects the breakout premise; reject it.
        # Tolerance bps lets normal slippage through. Default 25 bps
        # matches the dashboard monitor's `or_break` invariant threshold.
        # Set ORB_OR_RETRACEMENT_TOLERANCE_BPS=0 to disable.
        if cfg.or_retracement_tolerance_bps > 0:
            tol = cfg.or_retracement_tolerance_bps / 10000.0
            if signal.side == "long" and signal.or_high > 0:
                min_entry = signal.or_high * (1.0 - tol)
                if entry_price < min_entry:
                    self._or_retrace_reject_count += 1
                    retrace_bps = (signal.or_high - entry_price) / signal.or_high * 10000.0
                    logger.info(
                        "[V9124-OR-RETRACE] %s/%s LONG entry=%.2f below "
                        "or_high=%.2f by %.1fbps > tol=%.1fbps -- stale signal",
                        signal.portfolio_id,
                        signal.ticker,
                        entry_price,
                        signal.or_high,
                        retrace_bps,
                        cfg.or_retracement_tolerance_bps,
                    )
                    return None
            elif signal.side == "short" and signal.or_low > 0:
                max_entry = signal.or_low * (1.0 + tol)
                if entry_price > max_entry:
                    self._or_retrace_reject_count += 1
                    retrace_bps = (entry_price - signal.or_low) / signal.or_low * 10000.0
                    logger.info(
                        "[V9124-OR-RETRACE] %s/%s SHORT entry=%.2f above "
                        "or_low=%.2f by %.1fbps > tol=%.1fbps -- stale signal",
                        signal.portfolio_id,
                        signal.ticker,
                        entry_price,
                        signal.or_low,
                        retrace_bps,
                        cfg.or_retracement_tolerance_bps,
                    )
                    return None

        risk_dollars_target = equity * cfg.risk_per_trade_pct / 100.0
        shares = max(1, int(risk_dollars_target / risk_per_share))
        # Notional cap (per-trade)
        max_notional = equity * cfg.max_trade_notional_pct / 100.0
        if entry_price > 0:
            shares_cap = max(1, int(max_notional / entry_price))
            shares = min(shares, shares_cap)
        risk_dollars_actual = risk_per_share * shares
        notional = entry_price * shares

        ticket = rb.try_admit(
            risk_dollars=risk_dollars_actual,
            notional=notional,
            ticker=signal.ticker,  # v8.3.34 -- Rule #1 lookup
            side=signal.side,  # v8.3.34 -- Rule #1 lookup
        )
        if ticket is None:
            return None

        try:
            pos = _exits.make_position(
                portfolio_id=signal.portfolio_id,
                ticker=signal.ticker,
                side=signal.side,
                entry_price=entry_price,
                stop=signal.proposed_stop,
                rr=cfg.rr,
                shares=shares,
                risk_ticket_id=ticket.ticket_id,
            )
        except ValueError:
            # bad geometry; release and bail
            rb.release(ticket)
            return None

        # Transition FSM
        ds.transition(_state.PHASE_IN_POS)
        ds.in_position = True
        ds.last_signal_bucket = None
        ds.last_entry_iso = signal.signal_bar_close_iso

        # v8.1.8 -- wash-sale check. If a losing close on the SAME
        # (ticker, side) happened within the last 30 calendar days,
        # log [V81-WASH-RISK] + bump the session counter. Operator-
        # facing only; never blocks the entry. Pruning happens here
        # so the buffer doesn't grow unbounded.
        try:
            import time as _time

            key = (signal.ticker, signal.side)
            cutoff = _time.time() - 30 * 24 * 3600
            recent = [r for r in self._recent_losses.get(key, []) if r.get("ts_unix", 0) > cutoff]
            self._recent_losses[key] = recent
            if recent:
                prior = recent[-1]
                self.wash_risk_count += 1
                logger.info(
                    "[V81-WASH-RISK] %s %s entry @ $%.2f -- prior "
                    "loss $%.2f on same (ticker, side) at %s. "
                    "Counter: %d this session.",
                    signal.ticker,
                    signal.side,
                    entry_price,
                    prior.get("pnl_dollars", 0.0),
                    prior.get("exit_iso") or "?",
                    self.wash_risk_count,
                )
        except Exception:
            # Wash-sale tracking is best-effort signaling. Never
            # block the trading path on its bookkeeping.
            pass

        return Admission(position=pos, risk_ticket=ticket)

    # --- exit path ---

    def evaluate_position_exit(
        self,
        pos: _exits.OrbPosition,
        *,
        bar_high: float,
        bar_low: float,
        bar_close: float,
        bar_bucket_min: int,
    ) -> Optional[_exits.ExitDecision]:
        """Convenience wrapper. Returns the exit decision if the bar
        closes the position, else None.

        v8.1.0 -- forwards cfg.partial_profit_at_1r so exits.evaluate()
        can emit EXIT_PARTIAL on first 1R touch.
        R21 -- forwards cfg.runner_eod_prep_minutes so exits.evaluate()
        can emit EXIT_RUNNER_EOD_PREP after the partial fires.

        R26 -- forwards cfg.stale_full_exit_minutes +
        cfg.stale_full_exit_mfe_floor_r so exits.evaluate() can emit
        EXIT_STALE_FULL_EXIT on un-partialed positions that drift past
        the time cutoff.
        """
        return _exits.evaluate(
            pos,
            bar_high=bar_high,
            bar_low=bar_low,
            bar_close=bar_close,
            bar_bucket_min=bar_bucket_min,
            eod_cutoff_min=self.cfg.eod_cutoff_minutes,
            partial_profit_at_1r=self.cfg.partial_profit_at_1r,
            runner_eod_prep_min=self.cfg.runner_eod_prep_minutes,
            stale_full_exit_min=self.cfg.stale_full_exit_minutes,
            stale_full_exit_mfe_floor_r=self.cfg.stale_full_exit_mfe_floor_r,
            reversal_circuit_min_mfe_r=self.cfg.reversal_circuit_min_mfe_r,
            reversal_circuit_min_giveback_r=self.cfg.reversal_circuit_min_giveback_r,
        )

    def on_partial_exit(self, pos: _exits.OrbPosition, partial_price: float) -> tuple[int, float]:
        """v8.1.0 -- engine-side partial-fill bookkeeping.

        Called by the adapter AFTER the broker partial-close order has
        been submitted (caller's responsibility). Mutates the position
        and releases proportional risk-book budget. Returns
        (shares_closed, pnl_dollars_booked).

        On (0, 0.0), the partial was a no-op (already taken or shares
        too small). Caller should not have submitted the broker order
        in that case; defensive guard against double-fire.

        Idempotent on duplicate calls (apply_partial_fill is itself
        idempotent on partial_taken=True).
        """
        # Apply partial bookkeeping on the position itself.
        shares_closed, pnl = _exits.apply_partial_fill(pos, partial_price)
        if shares_closed == 0:
            return (0, 0.0)
        # Release half the risk-book budget for this ticket so the
        # remaining position carries only its remaining risk weight.
        rb = self._risk.get(pos.portfolio_id)
        if rb is not None and pos.risk_ticket_id:
            with rb._lock:
                ticket = rb._open_tickets.get(pos.risk_ticket_id)
            if ticket is not None:
                rb.release_partial(ticket, frac=0.5)
        return (shares_closed, pnl)

    def on_exit(
        self, pos: _exits.OrbPosition, exit_decision: _exits.ExitDecision, *, exit_iso: str = ""
    ) -> None:
        """Release the risk ticket and transition FSM to CLOSED.

        Position becomes eligible for re-entry on the same ticker if
        trades_today < max_trades_per_day.

        v7.29.0: also records realized P&L into the RiskBook for the
        daily-loss kill gate. If this exit causes the kill threshold to
        cross, all (portfolio, ticker) FSM rows for this portfolio that
        are currently armed/warmup transition to PHASE_BLOCKED_DAILY_KILL
        so no further entries fire today.
        """
        rb = self._risk.get(pos.portfolio_id)
        if rb is not None and pos.risk_ticket_id:
            # Reconstruct a thin _Ticket-like to release; simpler: scan
            # internal tickets and release by id.
            with rb._lock:
                ticket = rb._open_tickets.get(pos.risk_ticket_id)
            if ticket is not None:
                rb.release(ticket)
        # v7.29.0: realized P&L accounting
        # v7.32.0: defensive validation -- a position with shares<=0 or
        # entry/exit_price<=0 should NEVER reach on_exit (try_enter
        # clamps shares to max(1, ...) and make_position requires
        # entry != stop). But if a buggy caller passes one in, the kill
        # gate would have been silently bypassed pre-v7.32 (P&L = 0).
        # Now we WARN and skip the accounting so the bug surfaces.
        kill_just_triggered = False
        if rb is not None:
            try:
                exit_price = float(exit_decision.price)
                entry_price = float(pos.entry_price)
                shares = int(pos.shares or 0)
                if shares <= 0 or entry_price <= 0.0 or exit_price <= 0.0:
                    logger.warning(
                        "[V79-ORB-KILL] skipping P&L accounting -- "
                        "malformed position pos=%s.%s entry=%.4f "
                        "exit=%.4f shares=%d (kill gate may be bypassed; "
                        "this should not happen in production)",
                        pos.portfolio_id,
                        pos.ticker,
                        entry_price,
                        exit_price,
                        shares,
                    )
                else:
                    if pos.side == "long":
                        pnl = shares * (exit_price - entry_price)
                    else:  # short
                        pnl = shares * (entry_price - exit_price)
                    # v8.1.0 -- include any partial-profit P&L booked
                    # earlier in this position's lifecycle so the
                    # daily-loss-kill gate sees the full realized P&L
                    # of this trade, not just the runner half. Matches
                    # backtest semantics where pnl = (exit-entry) *
                    # remaining + partial_pnl_dollars.
                    pnl += float(getattr(pos, "partial_pnl_dollars", 0.0) or 0.0)
                    # v8.3.34 -- pass ticker+side so the risk_book can
                    # apply Rule #1 (loss-lock) on this exit's pnl.
                    kill_just_triggered = rb.record_realized_pnl(
                        pnl,
                        ticker=pos.ticker,
                        side=pos.side,
                    )
                    # v8.1.8 -- record losing closes for the wash-sale
                    # tracker. The threshold is a couple cents (not
                    # strictly zero) to avoid recording rounding noise
                    # from slippage at break-even. A re-entry on the
                    # same (ticker, side) within 30 days will trigger
                    # a [V81-WASH-RISK] log + counter bump.
                    if pnl < -0.01:
                        import time as _time

                        self._recent_losses[(pos.ticker, pos.side)].append(
                            {
                                "ts_unix": _time.time(),
                                "pnl_dollars": float(pnl),
                                "exit_iso": exit_iso or "",
                            }
                        )
            except Exception as e:
                logger.warning(
                    "[V79-ORB-KILL] pnl accounting failed pos=%s.%s: %s",
                    pos.portfolio_id,
                    pos.ticker,
                    e,
                )
        ds = self._state.get_day_state(pos.portfolio_id, pos.ticker)
        ds.in_position = False
        ds.trades_today += 1
        ds.last_exit_iso = exit_iso
        ds.transition(_state.PHASE_CLOSED)

        # v7.29.0: if the kill just triggered, block all eligible
        # (portfolio, ticker) rows for THIS portfolio so the next
        # signal can't fire. Skip already-blocked / in-position rows.
        if kill_just_triggered and rb is not None:
            threshold = rb.daily_kill_threshold_dollars
            logger.warning(
                "[V79-ORB-KILL] daily-loss kill TRIGGERED portfolio=%s "
                "realized=$%.2f threshold=-$%.2f",
                pos.portfolio_id,
                rb.realized_pnl_today,
                threshold,
            )
            self._block_portfolio_for_daily_kill(pos.portfolio_id)

    def _block_portfolio_for_daily_kill(self, portfolio_id: str) -> None:
        """Transition every armed / warmup / closed FSM row for this
        portfolio to PHASE_BLOCKED_DAILY_KILL. Leaves in-position rows
        alone (the existing position is allowed to manage to its exit)
        and skips already-blocked rows."""
        for (pid, ticker), ds in self._state.day_states.items():
            if pid != portfolio_id:
                continue
            if ds.is_blocked():
                continue
            if ds.phase == _state.PHASE_IN_POS:
                continue
            ds.transition(
                _state.PHASE_BLOCKED_DAILY_KILL,
                reason=f"daily_loss_kill portfolio={portfolio_id}",
            )

    # --- consistency sweeps (v8.3.15) ---

    def find_phantom_in_pos(self, *, held_tickers_by_pid: dict) -> list:
        """v8.3.15 -- find FSM rows that say in_position=True but the
        ticker isn't in the caller's held-positions map.

        Self-heal target: stale `/data/orb_state_<date>.json` files
        where the bot closed a position in a prior process but the
        snapshot was written BEFORE the v8.3.12 unmirror landed (or
        the close path crashed mid-write). v8.3.4 rehydrate then
        reloads the stale row on every subsequent bootstrap; the
        FSM stays IN_POS forever, surfacing as the watchdog's
        `v10_in_pos_has_internal_position` invariant.

        Pure: no side effects. Returns [(pid, ticker)] list of
        phantoms; caller decides how to clear each (different
        clearing paths for main vs val/gene executors).

        Args:
            held_tickers_by_pid: {pid: set(tickers)} -- the tickers
                each portfolio actually holds right now, as reported
                by tg.positions (main) or executor.positions (val/
                gene). Should include both long and short side.

        Returns:
            list[tuple[str, str]] -- (portfolio_id, ticker) pairs
            that need unmirroring. Empty list when state is clean.
        """
        out: list = []
        for (pid, ticker), ds in self._state.day_states.items():
            if not ds.in_position:
                continue
            held = held_tickers_by_pid.get(pid)
            if held is None:
                # No data for this pid -- can't determine; skip.
                continue
            if ticker in held:
                continue
            out.append((pid, ticker))
        return out

    def clear_phantom_in_pos(self, portfolio_id: str, ticker: str) -> bool:
        """v8.3.15 -- direct phantom clearing helper, used by the
        sweep when the executor's _unmirror_position_from_engine
        path isn't available (e.g. for main, which has no executor
        instance -- it's the trade_genius module itself).

        Returns True if any state was actually cleared.

        v9.1.26 -- ALSO route through on_exit for the REAL engine
        ticket (uuid-style from try_admit) when an adapter exposes it.
        Pre-v9.1.26 this helper only handled the synthetic recover-*
        ticket + FSM transition, skipping trades_today and leaving
        uuid tickets to leak. Symmetric with the executor-side fix
        in executors/base.py:_unmirror_position_from_engine.
        """
        cleared = False
        # v9.1.26 -- prefer on_exit path if a real engine ticket exists.
        try:
            import orb.live_runtime as _orb_runtime
            from orb.exits import ExitDecision

            adapter = _orb_runtime._adapters.get(portfolio_id) if _orb_runtime._adapters else None
            if adapter is not None:
                real_ticket_id = adapter._ticker_to_ticket.get(ticker)
                if real_ticket_id and not real_ticket_id.startswith("recover-"):
                    pos = adapter._open_positions.get(real_ticket_id)
                    if pos is not None:
                        decision = ExitDecision(
                            reason="phantom_sweep",
                            price=float(pos.entry_price),
                        )
                        self.on_exit(pos, decision)
                        adapter._open_positions.pop(real_ticket_id, None)
                        if adapter._ticker_to_ticket.get(ticker) == real_ticket_id:
                            del adapter._ticker_to_ticket[ticker]
                        # on_exit handled FSM + trades_today + ticket
                        # release. Still attempt synthetic ticket cleanup
                        # below for paranoia (in case both were tracked).
                        cleared = True
        except Exception:
            # Fall through to legacy path. Don't raise into the sweep.
            pass
        # Legacy FSM transition (when no real adapter ticket).
        ds = self._state.get_day_state(portfolio_id, ticker)
        if ds.in_position:
            ds.in_position = False
            if ds.phase == _state.PHASE_IN_POS:
                ds.transition(_state.PHASE_CLOSED)
            cleared = True
        rb = self._risk.get(portfolio_id)
        if rb is not None:
            ticket_id = f"recover-{portfolio_id}-{ticker}"
            with rb._lock:
                ticket = rb._open_tickets.pop(ticket_id, None)
                if ticket is not None:
                    rb._open_risk -= float(ticket.risk_dollars)
                    rb._open_notional -= float(ticket.notional)
                    if rb._open_risk < 0:
                        rb._open_risk = 0.0
                    if rb._open_notional < 0:
                        rb._open_notional = 0.0
                    cleared = True
        return cleared

    def find_phantom_recover_tickets(self, *, held_tickers_by_pid: dict) -> list:
        """v8.3.20 -- second-level phantom sweep, finds orphan
        `recover-{pid}-{ticker}` tickets in RiskBook._open_tickets
        where the ticker isn't held by the corresponding portfolio.

        Why v8.3.15's existing sweep isn't enough: that one only
        catches phantoms where FSM `in_position=True` AND ticker not
        held. But it's possible for `in_position` to be False (e.g.
        rehydrated from disk that way, or set False by a partial
        _unmirror that crashed mid-way) while the recover ticket
        still sits in `_open_tickets`. Pre-v8.3.20 those tickets
        leaked their reserved risk + notional forever, surfacing as
        the watchdog `no_phantom_positions` invariant ("main has 1
        position in /api/state but RiskBook reports open_count=4")
        AND blocking new entries via `risk_reject:notional_cap`
        because the cap was already consumed by ghosts.

        Returns [(portfolio_id, ticket_id, ticker)] for each orphan
        recover ticket. Caller releases each via
        `release_recover_ticket(pid, ticket_id)`.

        Scope: only `recover-{pid}-{ticker}`-prefixed tickets (the
        deterministic ids v8.3.6 mirror creates). uuid-style tickets
        from the normal `try_admit` path aren't touched -- they're
        either real in-flight admits or v7.81.0 rollback failures
        that need a separate solution (we'd need a ticket -> ticker
        map on the position to identify them safely).
        """
        out: list = []
        for pid, rb in self._risk._books.items():
            held = held_tickers_by_pid.get(pid)
            if held is None:
                continue
            prefix = f"recover-{pid}-"
            with rb._lock:
                ticket_ids = list(rb._open_tickets.keys())
            for tid in ticket_ids:
                if not tid.startswith(prefix):
                    continue
                ticker = tid[len(prefix) :]
                if ticker in held:
                    continue
                out.append((pid, tid, ticker))
        return out

    def release_recover_ticket(self, portfolio_id: str, ticket_id: str) -> bool:
        """v8.3.20 -- pop a phantom `recover-*` ticket from
        RiskBook._open_tickets and decrement open_risk +
        open_notional. Used by the v8.3.20 sweep to free budget
        held by leaked tickets without needing to also touch FSM
        state (which v8.3.15's `clear_phantom_in_pos` handles
        when applicable).

        Returns True iff a ticket was actually released.
        """
        rb = self._risk.get(portfolio_id)
        if rb is None:
            return False
        with rb._lock:
            ticket = rb._open_tickets.pop(ticket_id, None)
            if ticket is None:
                return False
            rb._open_risk -= float(ticket.risk_dollars)
            rb._open_notional -= float(ticket.notional)
            if rb._open_risk < 0:
                rb._open_risk = 0.0
            if rb._open_notional < 0:
                rb._open_notional = 0.0
        return True

    def purge_non_recover_tickets(self, adapters=None) -> dict:
        """v8.3.22 -- nuke RiskBook tickets that have no backing position.

        Originally a prefix-only sweep (purge anything not starting with
        `recover-{pid}-{ticker}`); v9.1.140 broadened the prefix to any
        `recover-`; v9.1.141 makes the check position-aware so it
        correctly handles the section-C / section-G double-load from
        `apply_loaded_state`.

        The canonical invariant: every RiskBook ticket must have a
        corresponding entry in `adapter._open_positions[tid]`. Anything
        else is an orphan -- either a `try_admit` leak (broker fire
        failed without `rollback_admit`) or a section-C duplicate from
        the previous boot's `dump_state_to_disk` payload
        (`apply_loaded_state` rebuilds `rb._open_tickets` from
        `risk_books.open_tickets` AND then layers
        `recover-{original_tid}` tickets from `open_positions` on top,
        so without the position-aware purge the same logical position
        ends up with two RiskBook tickets after every redeploy).

        Behavior:
          - adapters is None (legacy callers / unit tests):
              fall back to the v9.1.140 prefix-only behavior. Anything
              starting with `recover-` is kept, anything else is purged.
          - adapters is provided (production caller in
            `live_runtime._ensure_session_started_internal`):
              keep only tickets whose tid is in
              `adapters.get(pid)._open_positions`. Every other ticket --
              bare uuid leaks OR `recover-*` tickets without a backing
              position -- gets purged.

        Why both branches: the unit tests (`tests/strategy/
        test_orb_uuid_ticket_purge.py`) construct engines without
        adapters and assert the prefix-only contract. Production
        passes adapters and gets the tighter invariant.

        The 2026-05-20 NVDA incident chain:
          v9.1.139 and prior: prefix check was `recover-{pid}-{ticker}`
            but V834-PERSIST section G writes `recover-{original_tid}`.
            Shapes don't overlap, section G tickets purged at every
            session_start, RiskBook empty while adapter held NVDA,
            `INVARIANTS/no_phantom_positions` CRIT.
          v9.1.140: broadened to `recover-` prefix. Section G tickets
            survived -- but so did section C's `recover-<uuid>` from
            the prior boot's persistence, producing 2 tickets for the
            same position. Risk over-counted instead of under-counted
            (safer direction) but the monitor still CRIT'd because
            `open_count != len(positions)`.
          v9.1.141: position-aware. Section C duplicates get purged
            (no matching adapter position); section G tickets stay
            (they ARE the adapter positions). Net: 1 ticket per held
            position -- the invariant the monitor checks.

        Returns counters {pid: n_purged} so the caller can log a
        single `[V8322-UUID-PURGE]` summary line.

        Side effects: decrements `_open_risk` + `_open_notional` per
        cleared ticket; clamps non-negative.
        """
        out: dict = {}
        for pid, rb in self._risk._books.items():
            with rb._lock:
                ticket_ids = list(rb._open_tickets.keys())
            adapter = adapters.get(pid) if adapters is not None else None
            backed_tids: set = set()
            if adapter is not None:
                try:
                    backed_tids = set(adapter._open_positions.keys())
                except Exception:
                    backed_tids = set()
            cleared = 0
            for tid in ticket_ids:
                if adapter is not None:
                    # Position-aware: keep only tickets with a matching
                    # adapter position. Bare uuid leaks AND `recover-*`
                    # tickets without a backing position both get purged.
                    if tid in backed_tids:
                        continue
                else:
                    # Legacy prefix-only mode for unit tests + callers
                    # that don't pass adapters.
                    if tid.startswith("recover-"):
                        continue
                with rb._lock:
                    ticket = rb._open_tickets.pop(tid, None)
                    if ticket is None:
                        continue
                    rb._open_risk -= float(ticket.risk_dollars)
                    rb._open_notional -= float(ticket.notional)
                    if rb._open_risk < 0:
                        rb._open_risk = 0.0
                    if rb._open_notional < 0:
                        rb._open_notional = 0.0
                cleared += 1
            if cleared > 0:
                out[pid] = cleared
        return out

    # --- snapshots / introspection ---

    def snapshot(self) -> dict:
        """JSON-shaped state for the dashboard /api/state."""
        return {
            "config": {
                "or_minutes": self.cfg.or_minutes,
                # v7.103.0 -- expose session window so the monitor's
                # inv_entries_inside_window invariant can compute the
                # eligible-entry window dynamically rather than
                # hardcoding 09:30-15:55 ET.
                "session_start_minutes": self.cfg.session_start_minutes,
                "eod_cutoff_minutes": self.cfg.eod_cutoff_minutes,
                "rr": self.cfg.rr,
                "max_trades_per_day": self.cfg.max_trades_per_day,
                "risk_per_trade_pct": self.cfg.risk_per_trade_pct,
                "max_concurrent_risk_dollars": self.cfg.max_concurrent_risk_dollars,
                "daily_loss_kill_pct": self.cfg.daily_loss_kill_pct,
                "skip_vix_above": self.cfg.skip_vix_above,
                "skip_gap_above_pct": self.cfg.skip_gap_above_pct,
                "skip_earnings_window": self.cfg.skip_earnings_window,
                "blocklist": self.cfg.ticker_side_blocklist or {},
                "atr_stop_mult": self.cfg.atr_stop_mult,
                "atr_lookback_5m": self.cfg.atr_lookback_5m,
                "partial_profit_at_1r": self.cfg.partial_profit_at_1r,
                # v9.0.0 chase-prevention + regime-skip config
                "min_break_bps": self.cfg.min_break_bps,
                "max_vwap_dev_bps": self.cfg.max_vwap_dev_bps,
                "max_vwap_dev_tickers": list(self.cfg.max_vwap_dev_tickers or ()),
                "skip_prior_spy_ret_lt_bps": self.cfg.skip_prior_spy_ret_lt_bps,
                # v9.1.7 -- entry-time cutoff (R12 winner, default 11:00 ET).
                "time_cutoff_minutes": self.cfg.time_cutoff_minutes,
                # v9.1.12 -- OR-width admissibility bounds. Dashboard
                # reads these to colour the "Range" column on the v10
                # Matrix; pre-v9.1.12 the column rendered as "-" for
                # every row because the config block didn't surface
                # range_min_pct / range_max_pct.
                "range_min_pct": self.cfg.range_min_pct,
                "range_max_pct": self.cfg.range_max_pct,
            },
            # v8.1.8 -- wash-sale risk counter (session-scoped).
            # Operator-facing signaling for §1091 visibility.
            "wash_risk_count": self.wash_risk_count,
            # v9.0.0 chase-filter rejection counters (session-scoped).
            "mbr_reject_count": self._mbr_reject_count,
            "vwap_chase_reject_count": self._vwap_chase_reject_count,
            "time_cutoff_reject_count": self._time_cutoff_reject_count,
            # v9.1.124 OR-retracement counter.
            "or_retrace_reject_count": self._or_retrace_reject_count,
            "day_status": {
                "block_day": self._day_result.block_day if self._day_result else False,
                "block_reason": self._day_result.block_reason if self._day_result else "",
                "vix_d1_close": self._day_result.vix_d1_close if self._day_result else None,
                "vix_threshold": self._day_result.vix_threshold if self._day_result else 0.0,
                # v9.0.0 SPY regime fields surfaced for the dashboard.
                "spy_d1_ret_bps": self._day_result.spy_d1_ret_bps if self._day_result else None,
                "spy_threshold_bps": self._day_result.spy_threshold_bps
                if self._day_result
                else 0.0,
                "session_date": self._day_result_date,
            },
            "or_windows": self._state.snapshot_or_windows(),
            "day_states": self._state.snapshot_day_states(),
            "risk_books": self._risk.snapshot_all(),
        }
