"""orb.live_runtime -- production wiring singleton for v10 ORB.

This module holds the live OrbEngine + LiveAdapter registry and provides
the entry points that engine/scan.py and trade_genius.py call.

Design (per the v10 keystone + framework rule #0/#22):

  At process startup (trade_genius.py main()):
    orb.live_runtime.bootstrap()

  At first scan cycle after 09:30 ET each session:
    orb.live_runtime.ensure_session_started(date_iso, ...)

  Per-ticker, per-tick (engine/scan.py:_per_ticker_tick):
    1. orb.live_runtime.feed_bar(ticker, bar) -- routes to all adapters
    2. orb.live_runtime.check_entry(portfolio_id, ticker, side, ...)
    3. orb.live_runtime.check_exit(portfolio_id, ticker, ticket, ...)

  At session end (or on shutdown):
    orb.live_runtime.reset_session()

Feature flag: ORB_LIVE_MODE env var (default "1" = on). Set to "0" to
disable v10 routing entirely; scan.py will then fall back to whatever
the legacy code path did before. The runtime module is still imported
but its tick() returns a no-op.

Look-ahead audit per rule #7b: the runtime DOES NOT introduce any new
data sources beyond what the underlying OrbEngine + LiveAdapter use.
Bootstrap reads the daily VIX CSV (refreshed by GHA workflow at 07:00
ET, well before market open) and the static earnings calendar (public
schedule).

Multi-portfolio: the runtime automatically discovers all 3 portfolios
(main / val / gene) from engine.portfolio_book.PORTFOLIOS. If only one
is enabled, only one adapter is wired.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from orb.engine import OrbConfig, OrbEngine
from orb.live_adapter import LiveAdapter, LiveAdapterRegistry, EntryResult, ExitResult

logger = logging.getLogger(__name__)


# --- module-level singleton state ---

_engine: Optional[OrbEngine] = None
_adapters: Optional[LiveAdapterRegistry] = None
_session_date: str = ""  # iso date when the current session was started; "" if not yet
_bootstrapped: bool = False


def is_live_mode_on() -> bool:
    """True if the ORB_LIVE_MODE env flag is set to "1" (default).

    Set ORB_LIVE_MODE=0 for emergency rollback to legacy strategy. The
    runtime stays loaded; tick() functions return no-ops.
    """
    return os.environ.get("ORB_LIVE_MODE", "1") == "1"


# --- bootstrap ---

def _build_config_from_env() -> OrbConfig:
    """Read v10 config from env vars. Defaults match v10 keystone."""
    def _f(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default
    def _i(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default
    def _b(name: str, default: bool) -> bool:
        v = os.environ.get(name)
        if v is None:
            return default
        return v.strip() in ("1", "true", "True", "yes", "YES")
    import json as _json
    bl_raw = os.environ.get("ORB_TICKER_SIDE_BLOCKLIST", "")
    blocklist = None
    if bl_raw.strip():
        try:
            blocklist = _json.loads(bl_raw)
        except Exception as e:
            logger.warning("ORB_TICKER_SIDE_BLOCKLIST parse failed: %s", e)
    return OrbConfig(
        or_minutes=_i("ORB_OR_MINUTES", 30),
        rr=_f("ORB_RR", 2.5),
        stop_buffer_bps=_f("ORB_STOP_BUFFER_BPS", 5.0),
        range_min_pct=_f("ORB_RANGE_MIN_PCT", 0.008),
        range_max_pct=_f("ORB_RANGE_MAX_PCT", 0.025),
        max_trades_per_day=_i("ORB_MAX_TRADES_PER_DAY", 5),
        risk_per_trade_pct=_f("ORB_RISK_PER_TRADE_PCT", 2.0),
        max_concurrent_risk_dollars=_f("ORB_MAX_CONCURRENT_RISK_DOLLARS", 2000.0),
        max_concurrent_notional_mult=_f("ORB_MAX_CONCURRENT_NOTIONAL_MULT", 2.0),
        max_trade_notional_pct=_f("ORB_MAX_TRADE_NOTIONAL_PCT", 75.0),
        daily_loss_kill_pct=_f("ORB_DAILY_LOSS_KILL_PCT", 2.0),
        move_to_be_after_1r=_b("ORB_MOVE_TO_BE_AFTER_1R", True),
        skip_vix_above=_f("ORB_SKIP_VIX_ABOVE", 22.0),
        skip_earnings_window=_b("ORB_SKIP_EARNINGS_WINDOW", True),
        earnings_days_before=_i("ORB_EARNINGS_DAYS_BEFORE", 1),
        earnings_days_after=_i("ORB_EARNINGS_DAYS_AFTER", 0),
        skip_gap_above_pct=_f("ORB_SKIP_GAP_ABOVE_PCT", 1.5),
        fail_closed_on_missing_vix=_b("ORB_FAIL_CLOSED_VIX", True),
        ticker_side_blocklist=blocklist,
    )


def _resolve_portfolio_ids() -> list[str]:
    """Discover the live portfolio_ids. Fall back to ['main'] if the
    PortfolioRegistry is unavailable (e.g. tests or older builds).
    """
    try:
        from engine.portfolio_book import PORTFOLIOS, ALL_PORTFOLIO_IDS
        ids = []
        for pid in ALL_PORTFOLIO_IDS:
            book = PORTFOLIOS.get(pid)
            if book is None:
                continue
            cfg = getattr(book, "config", None)
            if cfg is None or getattr(cfg, "enabled", True):
                ids.append(pid)
        return ids if ids else ["main"]
    except Exception as e:
        logger.info("portfolio_book unavailable, defaulting to ['main']: %s", e)
        return ["main"]


def _resolve_earnings_fn():
    try:
        from tools.orb_earnings_calendar import is_earnings_window
        return is_earnings_window
    except ImportError:
        return None


def bootstrap(*, force: bool = False) -> None:
    """Build the engine + adapters from env config + portfolio registry.

    Idempotent: subsequent calls without `force=True` are no-ops.

    Called by trade_genius.py at process startup, after PORTFOLIOS is
    initialized.
    """
    global _engine, _adapters, _bootstrapped
    if _bootstrapped and not force:
        return
    cfg = _build_config_from_env()
    portfolio_ids = _resolve_portfolio_ids()
    earnings_fn = _resolve_earnings_fn()
    _engine = OrbEngine(cfg, portfolio_ids=portfolio_ids,
                        is_earnings_window_fn=earnings_fn)
    _adapters = LiveAdapterRegistry(_engine)
    _bootstrapped = True
    logger.info("[V79-ORB-BOOT] portfolios=%s rr=%s or_min=%s vix_thr=%s",
                portfolio_ids, cfg.rr, cfg.or_minutes, cfg.skip_vix_above)


def get_engine() -> Optional[OrbEngine]:
    """Return the singleton engine, or None if bootstrap() hasn't run."""
    return _engine


def get_adapter(portfolio_id: str) -> Optional[LiveAdapter]:
    """Return the adapter for a portfolio, or None if not registered."""
    if _adapters is None:
        return None
    return _adapters.get(portfolio_id)


# --- session lifecycle ---

def ensure_session_started(*, date_iso: str,
                           tickers: list[str],
                           vix_close_d1: Optional[float],
                           ticker_open_today: dict[str, Optional[float]],
                           ticker_prev_close: dict[str, Optional[float]],
                           equity_per_portfolio: dict[str, float],
                           ) -> bool:
    """Idempotent session start. Returns True if a fresh session was
    started, False if the session was already started for `date_iso`.
    """
    global _session_date
    if not _bootstrapped or _engine is None:
        return False
    if _session_date == date_iso:
        return False
    result = _engine.start_new_session(
        date_iso=date_iso,
        tickers=tickers,
        vix_close_d1=vix_close_d1,
        ticker_open_today=ticker_open_today,
        ticker_prev_close=ticker_prev_close,
        equity_per_portfolio=equity_per_portfolio,
    )
    if _adapters is not None:
        _adapters.reset_all_sessions()
    _session_date = date_iso
    logger.info(
        "[V79-ORB-RESET] date=%s vix_d1=%s block_day=%s reason=%s",
        date_iso, vix_close_d1,
        result.block_day, result.block_reason,
    )
    return True


def reset_session() -> None:
    """Manual session reset (e.g. on shutdown / EOD)."""
    global _session_date
    _session_date = ""
    if _adapters is not None:
        _adapters.reset_all_sessions()


# --- per-tick API for scan.py ---

def feed_bar(*, ticker: str,
             bar_high: float, bar_low: float, bar_open: float,
             bar_close: float, bar_volume: float,
             bar_bucket_min: int) -> None:
    """Forward a 1-min bar to the OR window. No-op if not bootstrapped
    or live mode is off.
    """
    if not is_live_mode_on() or _engine is None:
        return
    _engine.on_bar_arrival(
        ticker=ticker,
        bar_high=bar_high, bar_low=bar_low,
        bar_open=bar_open, bar_close=bar_close,
        bar_volume=bar_volume,
        bar_bucket_min=bar_bucket_min,
    )


def check_entry(*, portfolio_id: str, ticker: str, side: str,
                five_min_close: float, next_open: float,
                equity: float, signal_iso: str = "",
                ) -> EntryResult:
    """Per-portfolio entry decision. Returns no-op EntryResult if the
    runtime isn't ready or live mode is off."""
    if not is_live_mode_on() or _adapters is None:
        return EntryResult(ok=False, reason_no="live_mode_off")
    a = _adapters.get(portfolio_id)
    if a is None:
        return EntryResult(ok=False, reason_no=f"no_adapter:{portfolio_id}")
    return a.check_entry(
        ticker, side=side,
        five_min_close=five_min_close, next_open=next_open,
        equity=equity, signal_iso=signal_iso,
    )


def check_exit(*, portfolio_id: str, ticker: str, ticket_id: str,
               bar_high: float, bar_low: float, bar_close: float,
               bar_bucket_min: int) -> ExitResult:
    """Per-portfolio exit decision. Returns no-op ExitResult if
    runtime isn't ready or live mode is off."""
    if not is_live_mode_on() or _adapters is None:
        return ExitResult(exit=False, reason="live_mode_off")
    a = _adapters.get(portfolio_id)
    if a is None:
        return ExitResult(exit=False, reason=f"no_adapter:{portfolio_id}")
    return a.check_exit(
        ticker, ticket_id,
        bar_high=bar_high, bar_low=bar_low, bar_close=bar_close,
        bar_bucket_min=bar_bucket_min,
    )


# v7.18.0: v10 sizing handoff to broker.orders.paper_shares_for.
#
# Why: engine/scan.py:_orb_long_entry calls callbacks.execute_entry(ticker,
# price) which routes through broker/lifecycle.execute_entry ->
# broker/orders.execute_breakout. The latter computes shares via
# paper_shares_for(price) -- the LEGACY sizing path that doesn't know about
# v10's per-trade risk_per_trade_pct + max_concurrent_risk_dollars caps.
#
# This stash bridges the two: when v10 admits an entry, it stores the
# computed shares here keyed by (portfolio_id, ticker). paper_shares_for
# checks the stash FIRST when ORB_LIVE_MODE=1; if a fresh v10 size is
# present, it uses that instead of legacy. The stash is one-shot per entry
# (consume_v10_size pops the entry).
_pending_v10_sizes: dict[tuple[str, str], int] = {}


def stash_v10_size(portfolio_id: str, ticker: str, shares: int) -> None:
    """Store v10's computed shares for an imminent execute_entry call."""
    _pending_v10_sizes[(portfolio_id, ticker)] = int(shares)


def consume_v10_size(portfolio_id: str, ticker: str) -> Optional[int]:
    """Pop the stashed shares for a (portfolio_id, ticker). Returns None
    if no v10 size was stashed (legacy fallback)."""
    return _pending_v10_sizes.pop((portfolio_id, ticker), None)


def peek_v10_size(portfolio_id: str, ticker: str) -> Optional[int]:
    """Read without consuming (for diagnostics/tests)."""
    return _pending_v10_sizes.get((portfolio_id, ticker))


def check_exit_by_ticker(*, portfolio_id: str, ticker: str,
                         bar_high: float, bar_low: float, bar_close: float,
                         bar_bucket_min: int) -> ExitResult:
    """Per-portfolio exit decision by ticker (no ticket_id required).

    Convenience for callers (broker/positions.py:manage_positions) that
    don't track v10 ticket ids on their position dicts. Returns
    `exit=False, reason="no_open_v10_position"` when there's no v10
    position open for this (portfolio, ticker) -- the common case
    during the v10/legacy coexistence window where some open positions
    in tg.positions are still legacy and have no v10 ticket.
    """
    if not is_live_mode_on() or _adapters is None:
        return ExitResult(exit=False, reason="live_mode_off")
    a = _adapters.get(portfolio_id)
    if a is None:
        return ExitResult(exit=False, reason=f"no_adapter:{portfolio_id}")
    return a.check_exit_by_ticker(
        ticker,
        bar_high=bar_high, bar_low=bar_low, bar_close=bar_close,
        bar_bucket_min=bar_bucket_min,
    )


def snapshot() -> dict:
    """JSON-shaped state for /api/state. Returns minimal stub if not
    bootstrapped."""
    if _engine is None:
        return {
            "bootstrapped": False,
            "live_mode": is_live_mode_on(),
            "session_date": "",
        }
    snap = _engine.snapshot()
    snap["bootstrapped"] = True
    snap["live_mode"] = is_live_mode_on()
    snap["session_date"] = _session_date
    return snap


# --- diagnostic helpers (for tests + manual ops) ---

def _reset_for_testing() -> None:
    """Tear down the singleton. ONLY for tests."""
    global _engine, _adapters, _bootstrapped, _session_date
    _engine = None
    _adapters = None
    _bootstrapped = False
    _session_date = ""
