"""orb.day_gates -- session-start filters.

Three independent gates evaluated at session start:
  1. VIX gate (market-wide): skip ALL entries today if VIX(D-1) > threshold
  2. Earnings gate (per-ticker): skip ticker today if within blackout window
  3. Gap gate (per-ticker): skip ticker today if today's open is gapped > X% from prior close

A separate fourth gate -- daily kill switch -- fires INTRADAY when the
portfolio's realized day P&L crosses the kill threshold. That's
implemented in the per-portfolio risk path, not here.

Look-ahead audit per rule #7b:

  VIX: uses VIX_close(D-1) read from the cached CSV. The CSV is
       refreshed daily by the GHA `refresh-data-feeds.yml` workflow at
       07:00 ET, well before any 09:30 entry signal can fire.

  Earnings: uses a public schedule announced weeks in advance. No
       look-ahead; using a known-future schedule is causally clean.

  Gap: uses prev_close (from the prior session's last bar) and today's
       09:30 open print. Both are available at OR start (09:30:30).
       Entries fire at 10:05+ (after OR window closes), so there's
       30+ minutes of buffer for these prints to be observed.

Fail-safe behavior: when input data is missing, the gates fail OPEN by
default (allow trading). Live deployment should configure
fail_closed_on_missing_vix=True so a missing VIX feed halts trading
rather than silently letting all signals through. The default is open
for backtest parity.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class DayGateConfig:
    """All thresholds + feature flags for the day-level gates.

    Defaults match the v10 keystone (see docs/v10_strategy_keystone.md).
    """
    # VIX gate
    skip_vix_above: float = 22.0          # 0 = off
    fail_closed_on_missing_vix: bool = False  # True for live; False for backtest parity
    # Earnings gate
    skip_earnings_window: bool = True
    earnings_days_before: int = 1
    earnings_days_after: int = 0
    # Gap gate
    skip_gap_above_pct: float = 1.5       # 0 = off; absolute % from prev close
    # Universe blocklist (e.g. {"META": ["LONG", "SHORT"], "MSFT": ["LONG", "SHORT"]})
    ticker_side_blocklist: dict = None    # type: ignore
    # v9.0.0 -- prior-day SPY regime gate. Skip the whole day when
    # the prior session's SPY close-to-close return was below
    # threshold (in bps; negative number means "drop deeper than").
    # 0.0 = off. R12 backtest: -40 bps captures the bleed band.
    skip_prior_spy_ret_lt_bps: float = -40.0
    fail_closed_on_missing_spy: bool = False  # fail-open when SPY
                                              # data missing (data feed
                                              # outage should not strand
                                              # the system)


@dataclass
class DayGateResult:
    """Outcome of evaluate_day(). Per-ticker results are nested."""
    block_day: bool = False
    block_reason: str = ""           # one of: "", "vix_high", "missing_vix",
                                     # "vix_threshold_zero",
                                     # "spy_regime_low", "missing_spy"
    vix_d1_close: Optional[float] = None
    vix_threshold: float = 0.0
    # v9.0.0 -- SPY regime fields.
    spy_d1_ret_bps: Optional[float] = None
    spy_threshold_bps: float = 0.0
    per_ticker: dict[str, "TickerGateResult"] = None  # type: ignore

    def is_ticker_allowed(self, ticker: str, side: str = "LONG") -> bool:
        """Convenience: True iff `ticker`/`side` may enter today."""
        if self.block_day:
            return False
        per = self.per_ticker or {}
        tres = per.get(ticker)
        if tres is None:
            return True
        return tres.is_allowed(side)


@dataclass
class TickerGateResult:
    """Per-ticker gate outcome."""
    ticker: str
    blocked: bool = False
    block_reason: str = ""           # "blocklist_long" / "blocklist_short" / "blocklist_both" / "earnings" / "gap"
    blocked_sides: tuple = ()        # ("LONG",) / ("SHORT",) / ("LONG", "SHORT") / ()
    gap_pct: Optional[float] = None
    earnings_within_window: bool = False

    def is_allowed(self, side: str) -> bool:
        if self.blocked and side.upper() in self.blocked_sides:
            return False
        if self.blocked and ("LONG" in self.blocked_sides
                             and "SHORT" in self.blocked_sides):
            # Fully blocked
            return False
        if self.blocked and not self.blocked_sides:
            # Generic block (e.g. earnings, gap) -- both sides
            return False
        return True


def _evaluate_blocklist(blocklist: Optional[dict], ticker: str) -> tuple:
    """Return tuple of blocked sides for `ticker`. Empty tuple if
    not blocked or blocklist is None.

    blocklist shape: {"META": ["LONG", "SHORT"], ...}. Case-insensitive
    on side strings.
    """
    if not blocklist:
        return ()
    sides = blocklist.get(ticker)
    if not sides:
        return ()
    return tuple(s.upper() for s in sides if s)


def evaluate_day(cfg: DayGateConfig,
                 *,
                 date_iso: str,
                 vix_close_d1: Optional[float],
                 tickers: list[str],
                 ticker_open_today: dict[str, Optional[float]],
                 ticker_prev_close: dict[str, Optional[float]],
                 is_earnings_window_fn=None,
                 spy_prior_ret_bps: Optional[float] = None,
                 ) -> DayGateResult:
    """Evaluate all day-level gates.

    Args:
        cfg: thresholds and feature flags.
        date_iso: today's date in YYYY-MM-DD form (for the earnings calendar).
        vix_close_d1: prior session's VIX close. None if missing.
        tickers: full universe to evaluate.
        ticker_open_today: today's 09:30 open per ticker. Missing keys -> gap gate fail-open for that ticker.
        ticker_prev_close: prior session's close per ticker. Missing keys -> gap gate fail-open.
        is_earnings_window_fn: callable (ticker, date_iso, days_before, days_after) -> bool.
            Pass tools.orb_earnings_calendar.is_earnings_window in production. None disables the gate.
        spy_prior_ret_bps: prior-session SPY close-to-close return in bps.
            None if missing (gate fails open or closed per cfg.fail_closed_on_missing_spy).

    Returns: DayGateResult.

    Determinism: same inputs -> same outputs. No global state read.
    """
    result = DayGateResult(
        vix_d1_close=vix_close_d1,
        vix_threshold=cfg.skip_vix_above,
        spy_d1_ret_bps=spy_prior_ret_bps,
        spy_threshold_bps=cfg.skip_prior_spy_ret_lt_bps,
        per_ticker={},
    )

    # 1. VIX gate (market-wide). Decided first because if it triggers, all
    # per-ticker work is irrelevant.
    if cfg.skip_vix_above > 0:
        if vix_close_d1 is None:
            if cfg.fail_closed_on_missing_vix:
                # v7.31.0: observability -- a missing VIX in production
                # blocks the whole day. Without this WARNING-level
                # forensic, the operator only sees an INFO-level
                # [V79-ORB-RESET] line and may not notice the data feed
                # broke. The refresh-data-feeds.yml GHA cron at 07:00 ET
                # populates data/external/vix-daily.csv; this fires when
                # that workflow failed silently.
                logger.warning(
                    "[V79-ORB-VIX] missing VIX D-1 close + "
                    "fail_closed=True -> day blocked. "
                    "Check data/external/vix-daily.csv and the "
                    "refresh-data-feeds.yml GHA cron."
                )
                result.block_day = True
                result.block_reason = "missing_vix"
                return result
            # Fail open: no VIX block, continue to per-ticker.
            logger.warning(
                "[V79-ORB-VIX] missing VIX D-1 close + "
                "fail_closed=False -> day NOT blocked (backtest parity). "
                "This should NOT happen in production."
            )
        elif vix_close_d1 > cfg.skip_vix_above:
            result.block_day = True
            result.block_reason = f"vix_high ({vix_close_d1:.2f} > {cfg.skip_vix_above:.2f})"
            return result

    # v9.0.0 -- 1b. SPY-regime gate (market-wide). Skip the day when
    # prior-session SPY return was below threshold (R12 research: bleed
    # zone is moderate post-down days). Threshold is in bps; negative
    # value means "below this drop level".
    if cfg.skip_prior_spy_ret_lt_bps != 0.0:
        if spy_prior_ret_bps is None:
            if cfg.fail_closed_on_missing_spy:
                logger.warning(
                    "[V900-SPY-GATE] missing SPY D-1 return + "
                    "fail_closed=True -> day blocked. "
                    "Check data/external/spy-daily.csv and the "
                    "refresh-data-feeds.yml GHA cron."
                )
                result.block_day = True
                result.block_reason = "missing_spy"
                return result
            logger.info(
                "[V900-SPY-GATE] missing SPY D-1 return + "
                "fail_closed=False -> day NOT blocked (fail-open)."
            )
        elif spy_prior_ret_bps < cfg.skip_prior_spy_ret_lt_bps:
            result.block_day = True
            result.block_reason = (
                f"spy_regime_low ({spy_prior_ret_bps:+.1f}bps < "
                f"{cfg.skip_prior_spy_ret_lt_bps:+.1f}bps)"
            )
            logger.info(
                "[V900-SPY-GATE] day blocked: prior SPY return "
                "%.1fbps < %.1fbps threshold",
                spy_prior_ret_bps, cfg.skip_prior_spy_ret_lt_bps,
            )
            return result

    # 2. + 3. Per-ticker gates (blocklist, earnings, gap).
    for ticker in tickers:
        tres = TickerGateResult(ticker=ticker)
        # Blocklist (always evaluated; both sides get the per-side mask)
        blocked_sides = _evaluate_blocklist(cfg.ticker_side_blocklist, ticker)
        if blocked_sides:
            tres.blocked = True
            tres.blocked_sides = blocked_sides
            tres.block_reason = "blocklist_" + "_".join(s.lower() for s in blocked_sides)
            result.per_ticker[ticker] = tres
            continue
        # Earnings (per-ticker)
        if cfg.skip_earnings_window and is_earnings_window_fn is not None:
            try:
                in_window = is_earnings_window_fn(
                    ticker, date_iso,
                    cfg.earnings_days_before, cfg.earnings_days_after,
                )
            except Exception:
                in_window = False
            if in_window:
                tres.blocked = True
                tres.block_reason = "earnings"
                tres.blocked_sides = ("LONG", "SHORT")
                tres.earnings_within_window = True
                result.per_ticker[ticker] = tres
                continue
        # Gap (per-ticker)
        if cfg.skip_gap_above_pct > 0:
            today_open = ticker_open_today.get(ticker)
            prev_close = ticker_prev_close.get(ticker)
            if today_open is not None and prev_close is not None and prev_close > 0:
                gap_pct = abs(today_open - prev_close) / prev_close * 100.0
                tres.gap_pct = gap_pct
                if gap_pct > cfg.skip_gap_above_pct:
                    tres.blocked = True
                    tres.block_reason = f"gap ({gap_pct:.2f}% > {cfg.skip_gap_above_pct:.2f}%)"
                    tres.blocked_sides = ("LONG", "SHORT")
                    result.per_ticker[ticker] = tres
                    continue
        result.per_ticker[ticker] = tres

    return result
