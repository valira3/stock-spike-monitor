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
import threading
from typing import Mapping, Optional

from orb.engine import OrbConfig, OrbEngine
from orb.eod_reversal import EodReversalConfig, EodReversalEngine
from orb.live_adapter import LiveAdapter, LiveAdapterRegistry, EntryResult, ExitResult

logger = logging.getLogger(__name__)


# --- module-level singleton state ---
#
# v7.30.0: thread safety hardening.
#   - `_bootstrap_lock` guards bootstrap() so a partial init from one
#     thread can't be observed by another (e.g. _engine set but
#     _adapters not). Bootstrap also commits via a local-then-swap
#     pattern so a mid-construction exception leaves the module in a
#     consistent "not bootstrapped" state.
#   - `_sizes_lock` guards _pending_v10_sizes which is written by the
#     scan thread (stash_v10_size) and read/popped by the broker
#     thread (consume_v10_size). Python dicts are NOT thread-safe for
#     concurrent read+write.

_engine: Optional[OrbEngine] = None
_adapters: Optional[LiveAdapterRegistry] = None
_session_date: str = ""  # iso date when the current session was started; "" if not yet
_bootstrapped: bool = False
# v9.1.8 -- engine-state persistence throttle. persist_engine_state()
# is called from the scan loop every cycle, but writing a ~kB JSON
# file every ~5s is unnecessary. The throttle ensures we dump no
# more than once per _persist_min_interval_s. Tuneable via
# ORB_PERSIST_INTERVAL_S env at bootstrap time.
_persist_last_iso: str = ""
_persist_min_interval_s: float = 30.0
# v9.1.0 -- EOD reversal addon engine. Runs alongside the morning ORB
# engine; fires a single cross-sectional reversal trade at 15:30 ET
# and flattens at 15:59 ET. Independent state.
_eod_engine: Optional[EodReversalEngine] = None
_bootstrap_lock = threading.RLock()
_sizes_lock = threading.RLock()

# v7.45.0 -- recent activity ring buffer. Captures the last 50 events
# (admits, rejects, exits, session_start, day_block, kill) so the
# dashboard's Activity Feed card can render a unified timeline. Each
# event is a dict with at least {ts_iso, kind, ticker, pid, detail}.
# Thread-safe via _activity_lock.
import collections as _collections
import datetime as _datetime

_RECENT_ACTIVITY_MAXLEN = 50
_recent_activity: _collections.deque = _collections.deque(maxlen=_RECENT_ACTIVITY_MAXLEN)
_activity_lock = threading.RLock()


def _record_activity(*, kind: str, ticker: str = "", pid: str = "", detail: str = "") -> None:
    """Append a single event to the recent-activity ring buffer.

    `kind` is one of: session_start | admit | reject | exit |
    day_block | kill | or_lock. The dashboard activity-feed renderer
    color-codes by kind.
    """
    try:
        ts_iso = _datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        with _activity_lock:
            _recent_activity.append(
                {
                    "ts_iso": ts_iso,
                    "kind": kind,
                    "ticker": ticker,
                    "pid": pid,
                    "detail": detail,
                }
            )
    except Exception:
        pass  # never break the trading path


def get_recent_activity(limit: int = 20) -> list:
    """Read the most recent N activity events (newest first)."""
    with _activity_lock:
        items = list(_recent_activity)
    items.reverse()
    return items[: max(1, min(limit, _RECENT_ACTIVITY_MAXLEN))]


def clear_recent_activity() -> None:
    """For session reset + tests."""
    with _activity_lock:
        _recent_activity.clear()


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

    def _et_to_min(name: str, default_min: int) -> int:
        """v9.1.7 -- parse HH:MM env var to minutes-since-midnight.

        Mirrors tools/orb_backtest.py:_et_to_minutes. Falls back to
        default_min on any parse error so a typo in Railway env can't
        strand the cutoff (logs the failure for forensic).
        """
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            return default_min
        try:
            h, m = raw.strip().split(":", 1)
            return int(h) * 60 + int(m)
        except (ValueError, TypeError) as e:
            logger.warning(
                "[ORB-ENV] %s=%r malformed (expected HH:MM): %s -- falling back to %d:%02d",
                name,
                raw,
                e,
                default_min // 60,
                default_min % 60,
            )
            return default_min

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
        risk_per_trade_pct=_f("ORB_RISK_PER_TRADE_PCT", 1.0),
        max_concurrent_risk_dollars=_f("ORB_MAX_CONCURRENT_RISK_DOLLARS", 2000.0),
        # v8.3.20 -- env default 2.0 -> 0.95. Operator directive: total
        # notional (longs + shorts) must stay BELOW account equity with
        # safety margins; no over-leverage. The 0.95 multiplier matches
        # the broker-side legacy v7.86.0 cap so both v10 RiskBook AND
        # paper-book ask the same question. Operator can override
        # via ORB_MAX_CONCURRENT_NOTIONAL_MULT in Railway env for
        # research backtests where higher exposure is intended.
        max_concurrent_notional_mult=_f("ORB_MAX_CONCURRENT_NOTIONAL_MULT", 0.95),
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
        # v8.0.0 -- ATR-based stop placement
        # v8.0.1 -- default flipped 0.0 -> 1.75 to activate live (backtest-
        # validated 39% headline lift, 0/4 negative quarters). Setting
        # ORB_ATR_STOP_MULT=0 in Railway env still disables the feature
        # (back to v7.111.0 OR-edge stop).
        atr_stop_mult=_f("ORB_ATR_STOP_MULT", 1.75),
        atr_lookback_5m=_i("ORB_ATR_LOOKBACK_5M", 14),
        # v8.1.0 -- partial-profit-at-1R. v8.1.3 -- env-fallback default
        # flipped False -> True. With everything wired end-to-end
        # (engine FSM in v8.1.0, executor Alpaca partial-sell in v8.1.1,
        # dashboard UI in v8.1.2), the operator-flag-required gate is
        # the last brake. v8.1.3 lifts it: next Railway deploy
        # auto-activates partial-profit-at-1R. Operator can revert by
        # setting ORB_PARTIAL_PROFIT_AT_1R=0 in Railway env (no redeploy).
        partial_profit_at_1r=_b("ORB_PARTIAL_PROFIT_AT_1R", True),
        # v8.3.34 -- day-end-giveback defenses (R6 sweep winners).
        # Both default 0.0 = off (R10 research showed they hurt when
        # stacked with v9 chase-prevention; do not enable in v9).
        loss_lock_threshold_usd=_f("ORB_LOSS_LOCK_THRESHOLD_USD", 0.0),
        peak_dd_halt_usd=_f("ORB_PEAK_DD_HALT_USD", 0.0),
        # v9.0.0 -- chase-prevention filters (R10 winning config).
        # Defaults ON. Operator can disable via env: set to 0 to
        # bypass either filter.
        min_break_bps=_f("ORB_MIN_BREAK_BPS", 5.0),
        max_vwap_dev_bps=_f("ORB_MAX_VWAP_DEV_BPS", 25.0),
        max_vwap_dev_tickers=tuple(
            t.strip().upper()
            for t in os.environ.get(
                "ORB_MAX_VWAP_DEV_TICKERS",
                "META,MSFT,AAPL,AMZN,GOOG,AVGO",
            ).split(",")
            if t.strip()
        ),
        # v9.0.0 -- prior-day SPY regime gate (R12). Default -40 bps
        # threshold; skip whole day on moderate SPY drop carryover.
        skip_prior_spy_ret_lt_bps=_f("ORB_SKIP_PRIOR_SPY_RET_LT_BPS", -40.0),
        fail_closed_on_missing_spy=_b("ORB_FAIL_CLOSED_SPY", False),
        # v9.1.7 -- ORB_TIME_CUTOFF_ET wires the env var through to the
        # live engine. Default 11:00 ET matches the R12 backtest winner
        # baked into the v13 final report. Set to "0:00" in Railway
        # env to disable (allows all-day entries until eod_cutoff).
        time_cutoff_minutes=_et_to_min("ORB_TIME_CUTOFF_ET", 11 * 60),
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

    v7.30.0: atomic + thread-safe.
      - The lock prevents a concurrent scan-thread/telegram-thread
        bootstrap race.
      - The local-then-swap pattern ensures a mid-construction
        exception (e.g. LiveAdapterRegistry blows up after OrbEngine
        succeeds) leaves the module in a consistent "not bootstrapped"
        state -- _engine and _adapters are only assigned once BOTH
        constructors return successfully.
    """
    global _engine, _adapters, _eod_engine, _bootstrapped
    with _bootstrap_lock:
        if _bootstrapped and not force:
            return
        cfg = _build_config_from_env()
        portfolio_ids = _resolve_portfolio_ids()
        earnings_fn = _resolve_earnings_fn()
        # Local-then-swap: build all, then publish atomically.
        try:
            _new_engine = OrbEngine(
                cfg, portfolio_ids=portfolio_ids, is_earnings_window_fn=earnings_fn
            )
            _new_adapters = LiveAdapterRegistry(_new_engine)
            # v9.1.0 -- EOD reversal addon engine. Lives alongside the
            # morning ORB. Its config is read from env (defaults all ON
            # per the r17 backtest winning spec); operator can disable
            # via ORB_EOD_REVERSAL_ENABLED=0.
            _new_eod_cfg = EodReversalConfig.from_env()
            _new_eod_engine = EodReversalEngine(
                _new_eod_cfg,
                portfolio_ids=portfolio_ids,
            )
        except Exception:
            logger.exception("[V79-ORB-BOOT] construction failed; module state unchanged")
            raise
        _engine = _new_engine
        _adapters = _new_adapters
        _eod_engine = _new_eod_engine
        _bootstrapped = True
        logger.info(
            "[V79-ORB-BOOT] portfolios=%s rr=%s or_min=%s vix_thr=%s",
            portfolio_ids,
            cfg.rr,
            cfg.or_minutes,
            cfg.skip_vix_above,
        )


def get_engine() -> Optional[OrbEngine]:
    """Return the singleton engine, or None if bootstrap() hasn't run."""
    return _engine


def get_adapter(portfolio_id: str) -> Optional[LiveAdapter]:
    """Return the adapter for a portfolio, or None if not registered."""
    if _adapters is None:
        return None
    return _adapters.get(portfolio_id)


# --- session lifecycle ---


def ensure_session_started(
    *,
    date_iso: str,
    tickers: list[str],
    vix_close_d1: Optional[float],
    ticker_open_today: dict[str, Optional[float]],
    ticker_prev_close: dict[str, Optional[float]],
    equity_per_portfolio: dict[str, float],
    spy_prior_ret_bps: Optional[float] = None,
) -> bool:
    """Idempotent session start. Returns True if a fresh session was
    started, False if the session was already started for `date_iso`.

    v9.0.0: `spy_prior_ret_bps` is the prior-session SPY return in bps.
    When omitted, the function auto-loads via orb_spy_loader (bar
    archive or CSV fallback). Pass an explicit value to override the
    loader in tests.
    """
    global _session_date
    if not _bootstrapped or _engine is None:
        return False
    if _session_date == date_iso:
        return False
    if spy_prior_ret_bps is None and _engine.cfg.skip_prior_spy_ret_lt_bps != 0.0:
        try:
            from tools.orb_spy_loader import prior_spy_return_bps

            spy_prior_ret_bps = prior_spy_return_bps(date_iso)
        except Exception as _e:
            logger.warning("[V900-SPY-LOADER] auto-load failed: %s", _e)
            spy_prior_ret_bps = None
    result = _engine.start_new_session(
        date_iso=date_iso,
        tickers=tickers,
        vix_close_d1=vix_close_d1,
        ticker_open_today=ticker_open_today,
        ticker_prev_close=ticker_prev_close,
        equity_per_portfolio=equity_per_portfolio,
        spy_prior_ret_bps=spy_prior_ret_bps,
    )
    if _adapters is not None:
        _adapters.reset_all_sessions()
    _session_date = date_iso
    logger.info(
        "[V79-ORB-RESET] date=%s vix_d1=%s block_day=%s reason=%s",
        date_iso,
        vix_close_d1,
        result.block_day,
        result.block_reason,
    )
    # v7.45.0: activity-feed event for session start + day-block if any
    clear_recent_activity()
    # v8.3.4 -- attempt to rehydrate engine state from disk for today.
    # Runs AFTER start_new_session's reset so any disk state for the
    # same date_iso overlays what the reset cleared (OR windows, FSM
    # phases, RiskBook realized P&L, etc.). Idempotent + no-op when
    # no disk file exists for today (fresh-day path).
    try:
        _try_rehydrate_engine_state(date_iso)
    except Exception as _e:
        logger.debug("[V834-PERSIST] rehydrate failed: %s", _e)
    # v8.3.22 -- one-shot purge of orphan uuid tickets that survived
    # the rehydrate. Any non-`recover-*` ticket in _open_tickets is a
    # leftover from a try_admit that wasn't released (broker fire
    # failed without rollback_admit, OR mid-cycle dump captured the
    # ticket without the position write that should follow). v8.3.6
    # mirror re-adds clean recover-* tickets from held positions on
    # boot, so this purge frees the cap from ghosts without nuking
    # real tracking.
    try:
        purged = _engine.purge_non_recover_tickets()
        if purged:
            logger.warning(
                "[V8322-UUID-PURGE] cleared orphan uuid tickets at session_start: %s",
                purged,
            )
    except Exception as _e:
        logger.debug("[V8322-UUID-PURGE] failed: %s", _e)
    # v9.1.61 -- inject positions that survived in Alpaca but were lost
    # from the OrbEngine adapter during pre-Fix-A deploys. Read from env
    # vars ORB_ORPHAN_POSITIONS_VAL / ORB_ORPHAN_POSITIONS_GENE.
    # Format: "TICKER:side:entry:stop:shares[,TICKER2:...]"
    # e.g. ORB_ORPHAN_POSITIONS_VAL=NVDA:long:235.76:232.94:324
    try:
        _inject_orphan_positions()
    except Exception as _oe:
        logger.debug("[V9161-ORPHAN] inject failed: %s", _oe)
    _record_activity(
        kind="session_start",
        detail="date "
        + date_iso
        + " · VIX_d1="
        + (("%.2f" % vix_close_d1) if isinstance(vix_close_d1, (int, float)) else "n/a"),
    )
    if result.block_day:
        _record_activity(
            kind="day_block",
            detail="day-level gate fired: " + (result.block_reason or "?"),
        )
    return True


def _inject_orphan_positions() -> None:
    """v9.1.61 -- restore OrbPositions from ORB_ORPHAN_POSITIONS_{PID} env vars.

    Used when a position survived in Alpaca but the OrbEngine adapter lost
    tracking due to pre-Fix-A deploys (before v9.1.52). Format per var:
      TICKER:side:entry_price:stop:shares[,TICKER2:...]
    Side effects: safe if ticker already tracked (idempotent).
    """
    import os as _os

    if _engine is None or _adapters is None:
        return
    from orb import risk_book as _rb_mod
    from orb.exits import make_position as _make_pos

    for pid in _engine.portfolio_ids:
        env_key = f"ORB_ORPHAN_POSITIONS_{pid.upper()}"
        raw = (_os.environ.get(env_key) or "").strip()
        if not raw:
            continue
        adapter = _adapters.get(pid)
        rb = _engine._risk.get(pid)
        if adapter is None or rb is None:
            continue
        tracked = {pos.ticker.upper() for pos in adapter._open_positions.values()}
        for spec in raw.split(","):
            spec = spec.strip()
            if not spec:
                continue
            parts = spec.split(":")
            if len(parts) < 5:
                logger.warning(
                    "[V9161-ORPHAN] bad spec %r for %s (need TICKER:side:entry:stop:shares)",
                    spec,
                    pid,
                )
                continue
            try:
                ticker = parts[0].upper()
                side = parts[1].lower()
                entry = float(parts[2])
                stop = float(parts[3])
                shares = int(parts[4])
                if ticker in tracked:
                    logger.debug("[V9161-ORPHAN] %s/%s already tracked, skipping", pid, ticker)
                    continue
                if not entry or not stop or not shares or abs(entry - stop) < 0.001:
                    logger.warning("[V9161-ORPHAN] invalid params for %s/%s, skipping", pid, ticker)
                    continue
                rr = float(getattr(_engine.cfg, "rr", 2.5) or 2.5)
                recover_tid = f"recover-orphan-{ticker}-{pid}"
                pos = _make_pos(
                    portfolio_id=pid,
                    ticker=ticker,
                    side=side,
                    entry_price=entry,
                    stop=stop,
                    rr=rr,
                    shares=shares,
                    risk_ticket_id=recover_tid,
                )
                with rb._lock:
                    rb._open_tickets[recover_tid] = _rb_mod._Ticket(
                        ticket_id=recover_tid,
                        risk_dollars=pos.risk_dollars,
                        notional=pos.notional,
                    )
                    rb._open_risk += pos.risk_dollars
                    rb._open_notional += pos.notional
                adapter._open_positions[recover_tid] = pos
                logger.info(
                    "[V9161-ORPHAN] injected %s %s %s entry=%.4f stop=%.4f shares=%d tid=%s",
                    pid,
                    ticker,
                    side,
                    entry,
                    stop,
                    shares,
                    recover_tid,
                )
            except Exception as _e:
                logger.warning("[V9161-ORPHAN] failed to inject %s/%r: %s", pid, spec, _e)


# v8.3.4 -- engine state persistence wrappers.
#
# Why: prior to v8.3.4, every in-memory engine artifact (OR windows,
# DayState FSM, RiskBook realized_pnl_today, activity feed, wash-sale
# tracker, pending v10 sizes) was wiped on every Railway redeploy. v8.3.0
# patched the OR-window symptom via per-cycle backfill; v8.3.4 attacks
# the root cause by persisting EVERYTHING the engine needs to a JSON
# snapshot on /data, then rehydrating on the next bootstrap.
#
# The wrappers below are thin: they delegate to orb.persistence. Tests
# exercise orb.persistence directly; live_runtime tests cover the
# bootstrap / ensure_session_started hookup.


def _bot_version_str() -> str:
    try:
        from bot_version import BOT_VERSION

        return str(BOT_VERSION)
    except Exception:
        return ""


def dump_engine_state_now(*, date_iso: str = "") -> bool:
    """Snapshot engine + live-runtime side state to disk.

    Returns True on success; False on any failure / runtime not
    bootstrapped / live mode off. Never raises.

    Called from engine/scan.py once per scan cycle (~60s cadence).
    """
    if _engine is None:
        return False
    if not date_iso:
        date_iso = _session_date or ""
    if not date_iso:
        return False
    try:
        from orb.persistence import dump_state_to_disk
    except Exception:
        return False
    with _activity_lock:
        activity = list(_recent_activity)
    with _sizes_lock:
        sizes = dict(_pending_v10_sizes)
    return dump_state_to_disk(
        _engine,
        recent_activity=activity,
        pending_v10_sizes=sizes,
        date_iso=date_iso,
        bot_version=_bot_version_str(),
    )


def _try_rehydrate_engine_state(date_iso: str) -> dict:
    """Attempt to load + overlay yesterday's-or-today's state from disk.

    Called from bootstrap() and ensure_session_started() right after
    the engine's reset, so the loaded data refills what the reset
    cleared. Returns the apply-counters dict (empty on failure).
    """
    if _engine is None or not date_iso:
        return {}
    try:
        from orb.persistence import (
            load_state_from_disk,
            apply_loaded_state,
        )
    except Exception:
        return {}
    loaded = load_state_from_disk(date_iso)
    if not loaded:
        return {}
    counters = apply_loaded_state(
        _engine,
        loaded,
        recent_activity=_recent_activity,
        pending_v10_sizes=_pending_v10_sizes,
        adapters=_adapters,
    )
    if counters and any(counters.values()):
        logger.info(
            "[V834-PERSIST] rehydrated date=%s or_windows=%d "
            "day_states=%d risk_books=%d positions=%d activity=%d sizes=%d",
            date_iso,
            counters.get("or_windows_loaded", 0),
            counters.get("day_states_loaded", 0),
            counters.get("risk_books_loaded", 0),
            counters.get("open_positions_loaded", 0),
            counters.get("activity_loaded", 0),
            counters.get("pending_sizes_loaded", 0),
        )
    return counters


def persist_engine_state() -> bool:
    """v9.1.8 -- public hook to dump the live engine state to disk.

    Called by the scan loop (engine/scan.py:scan_loop) once per cycle
    so day_states + risk_books + or_windows + recent activity survive
    a Railway redeploy. Pre-v9.1.8 the dump function existed
    (orb.persistence.dump_state_to_disk) but had NO production caller,
    so 'top ticker N/5' counters etc. reset on every restart.

    Throttled by _persist_min_interval_s so a fast scan loop doesn't
    write the same payload 60x/min. Fail-soft: never raises.
    Returns True on a successful write, False otherwise (including
    when throttled).
    """
    global _persist_last_iso
    if _engine is None or not _session_date:
        return False
    now_iso = _datetime.datetime.now(_datetime.timezone.utc).isoformat()
    if _persist_last_iso:
        try:
            elapsed = (
                _datetime.datetime.fromisoformat(now_iso)
                - _datetime.datetime.fromisoformat(_persist_last_iso)
            ).total_seconds()
            if elapsed < _persist_min_interval_s:
                return False
        except Exception:
            pass
    try:
        from orb.persistence import dump_state_to_disk
        from bot_version import BOT_VERSION
    except Exception:
        return False
    ok = dump_state_to_disk(
        _engine,
        recent_activity=list(_recent_activity),
        pending_v10_sizes=dict(_pending_v10_sizes),
        date_iso=_session_date,
        bot_version=BOT_VERSION,
        adapters=_adapters,
    )
    if ok:
        _persist_last_iso = now_iso
    return ok


def reset_session() -> None:
    """Manual session reset (e.g. on shutdown / EOD).

    v7.32.0: also clears `_pending_v10_sizes`. Stale stashed sizes
    from a prior session were persisting across the reset; if the
    same (portfolio, ticker) admitted a fresh trade the next day,
    broker.orders.paper_shares_for could consume the wrong size.
    """
    global _session_date
    with _bootstrap_lock:
        _session_date = ""
        adapters = _adapters
    if adapters is not None:
        adapters.reset_all_sessions()
    with _sizes_lock:
        _pending_v10_sizes.clear()


# v7.24.0: intraday equity refresh.
#
# Why: ensure_session_started seeds each RiskBook with the per-portfolio
# session-start equity. Throughout the day, mark-to-market gains/losses
# move the portfolio's actual equity, but the RiskBook's _equity stays
# frozen. The risk-cap math (max_concurrent_risk_dollars is absolute and
# unaffected, but max_concurrent_notional = equity * max_notional_mult
# IS affected) drifts from reality.
#
# Solution: scan.py calls refresh_equity_from_books() once per scan
# cycle (~every 60s). It pulls each PortfolioBook.current_equity(prices)
# and pushes via RiskBook.update_equity(). Cheap (no broker round-trip;
# just paper_cash + MTM math we already do for the dashboard).
#
# Failure-tolerant: any exception is swallowed and a single warn log
# emitted. The risk caps stay at their last-good values.
def refresh_equity_from_books(
    prices: Optional[Mapping[str, float]] = None,
) -> dict[str, float]:
    """Pull current per-portfolio equity and push it into each
    RiskBook via update_equity().

    v7.77.0 -- routes through engine.portfolio_equity.resolve_equity
    instead of reading book.current_equity() directly. Pre-v7.77.0
    this function read book.current_equity() (paper_cash + MTM)
    every scan cycle and called update_equity() -- which for Val/Gene
    silently overwrote v7.76.0's session-start equity seed back to
    0 (their paper_cash defaults to 0 and is never bridged from
    Alpaca). Result: notional cap stayed at $0 forever, every entry
    rejected on notional_cap. Now uses the same Alpaca-first source
    as session start, so the equity is consistent across both paths.

    Args:
        prices: optional {ticker: float} for mark-to-market. Used only
            for the Main book; Val/Gene equity comes from Alpaca's
            authoritative account balance and ignores `prices`.

    Returns:
        {portfolio_id: equity} that was actually applied. Empty dict if
        the runtime is not bootstrapped or PORTFOLIOS is unavailable.
    """
    if _engine is None:
        return {}
    try:
        from engine.portfolio_book import PORTFOLIOS, ALL_PORTFOLIO_IDS
        from engine.portfolio_equity import resolve_equity
    except Exception as e:
        logger.debug("[V79-ORB-EQUITY] portfolio_book unavailable: %s", e)
        return {}
    applied: dict[str, float] = {}
    for pid in ALL_PORTFOLIO_IDS:
        book = PORTFOLIOS.get(pid)
        if book is None:
            continue
        eq = 0.0
        # 1. Try book.current_equity first. For main this returns
        #    paper_cash + MTM (kept in sync with tg.paper_cash via the
        #    v7.72.0 bridge). For val/gene this returns 0 because
        #    paper_cash defaults to 0 and is never bridged from Alpaca.
        try:
            eq = float(book.current_equity(prices))
        except Exception as e:
            logger.debug("[V79-ORB-EQUITY] %s current_equity failed: %s", pid, e)
        # 2. v7.77.0 -- if the book gave us nothing useful (val/gene
        #    or a misbehaving book), fall back to resolve_equity. That
        #    pulls from Alpaca's authoritative account balance for
        #    val/gene and from tg.paper_cash for main.
        if eq <= 0:
            try:
                eq = float(resolve_equity(pid))
            except Exception as e:
                logger.debug("[V79-ORB-EQUITY] %s resolve_equity failed: %s", pid, e)
        # 3. Last-ditch fallback: the book's raw paper_cash attribute.
        if eq <= 0:
            try:
                eq = float(getattr(book, "paper_cash", 0.0))
            except Exception:
                continue
        rb = _engine._risk.get(pid)
        if rb is None:
            continue
        try:
            rb.update_equity(eq)
            applied[pid] = eq
        except Exception as e:
            logger.debug("[V79-ORB-EQUITY] %s update_equity failed: %s", pid, e)
    return applied


# --- per-tick API for scan.py ---


def feed_bar(
    *,
    ticker: str,
    bar_high: float,
    bar_low: float,
    bar_open: float,
    bar_close: float,
    bar_volume: float,
    bar_bucket_min: int,
) -> None:
    """Forward a 1-min bar to the OR window. No-op if not bootstrapped
    or live mode is off.

    v7.32.0: snapshot the _engine reference under the bootstrap lock
    so a concurrent bootstrap can't race the check-then-deref.
    """
    if not is_live_mode_on():
        return
    with _bootstrap_lock:
        engine = _engine
    if engine is None:
        return
    engine.on_bar_arrival(
        ticker=ticker,
        bar_high=bar_high,
        bar_low=bar_low,
        bar_open=bar_open,
        bar_close=bar_close,
        bar_volume=bar_volume,
        bar_bucket_min=bar_bucket_min,
    )


def backfill_or_windows(*, bars_by_ticker: dict, current_et_minutes: int) -> dict:
    """v8.3.0 -- runtime wrapper for OrbEngine.backfill_or_windows.

    Forwards pre-bucketed 1m bars (caller converts timestamps to ET
    buckets) to the engine so any OR window not locked in real-time
    gets rebuilt. Safe to call on every scan cycle; the engine
    no-ops already-locked tickers + bars going to a locked window.

    Returns the engine's counter dict, or an empty {} if runtime
    isn't bootstrapped / live mode is off.
    """
    if not is_live_mode_on():
        return {}
    with _bootstrap_lock:
        engine = _engine
    if engine is None:
        return {}
    return engine.backfill_or_windows(
        bars_by_ticker=bars_by_ticker,
        current_et_minutes=current_et_minutes,
    )


def check_entry(
    *,
    portfolio_id: str,
    ticker: str,
    side: str,
    five_min_close: float,
    next_open: float,
    equity: float,
    signal_iso: str = "",
    recent_5m_highs: Optional[list[float]] = None,
    recent_5m_lows: Optional[list[float]] = None,
    recent_5m_closes: Optional[list[float]] = None,
    session_vwap: Optional[float] = None,
) -> EntryResult:
    """Per-portfolio entry decision. Returns no-op EntryResult if the
    runtime isn't ready or live mode is off.

    v9.0.0: `session_vwap` is the cumulative session VWAP for `ticker`
    from session open through the signal bar (caller supplies). When
    omitted or zero, the v9 chase filter fails open (entry allowed).
    """
    if not is_live_mode_on():
        return EntryResult(ok=False, reason_no="live_mode_off")
    with _bootstrap_lock:
        adapters = _adapters
    if adapters is None:
        return EntryResult(ok=False, reason_no="live_mode_off")
    a = adapters.get(portfolio_id)
    if a is None:
        return EntryResult(ok=False, reason_no=f"no_adapter:{portfolio_id}")
    result = a.check_entry(
        ticker,
        side=side,
        five_min_close=five_min_close,
        next_open=next_open,
        equity=equity,
        signal_iso=signal_iso,
        recent_5m_highs=recent_5m_highs,
        recent_5m_lows=recent_5m_lows,
        recent_5m_closes=recent_5m_closes,
        session_vwap=session_vwap,
    )
    # v7.45.0: record admit / informative-reject in activity feed.
    # Skip "no_signal" rejects since they fire every tick when no
    # breakout is happening -- would flood the buffer.
    try:
        if result.ok:
            _record_activity(
                kind="admit",
                ticker=ticker,
                pid=portfolio_id,
                detail=(
                    side.upper()
                    + " · "
                    + str(int(result.shares or 0))
                    + " sh @ "
                    + ("%.2f" % (result.price or 0))
                ),
            )
        elif result.reason_no and result.reason_no != "no_signal":
            _record_activity(
                kind="reject",
                ticker=ticker,
                pid=portfolio_id,
                detail=side.upper() + " · " + (result.reason_no or ""),
            )
    except Exception:
        pass
    return result


def check_exit(
    *,
    portfolio_id: str,
    ticker: str,
    ticket_id: str,
    bar_high: float,
    bar_low: float,
    bar_close: float,
    bar_bucket_min: int,
) -> ExitResult:
    """Per-portfolio exit decision. Returns no-op ExitResult if
    runtime isn't ready or live mode is off.

    v7.32.0: snapshot _adapters under the bootstrap lock.
    """
    if not is_live_mode_on():
        return ExitResult(exit=False, reason="live_mode_off")
    with _bootstrap_lock:
        adapters = _adapters
    if adapters is None:
        return ExitResult(exit=False, reason="live_mode_off")
    a = adapters.get(portfolio_id)
    if a is None:
        return ExitResult(exit=False, reason=f"no_adapter:{portfolio_id}")
    result = a.check_exit(
        ticker,
        ticket_id,
        bar_high=bar_high,
        bar_low=bar_low,
        bar_close=bar_close,
        bar_bucket_min=bar_bucket_min,
    )
    if result and result.exit:
        try:
            _record_activity(
                kind="exit",
                ticker=ticker,
                pid=portfolio_id,
                detail=str(result.reason or "") + " @ " + ("%.2f" % (result.price or 0)),
            )
        except Exception:
            pass
    # v8.1.2 -- partial-fills surface in the activity feed too.
    elif result and getattr(result, "partial", False):
        try:
            _record_activity(
                kind="partial",
                ticker=ticker,
                pid=portfolio_id,
                detail=str(int(getattr(result, "partial_shares", 0) or 0))
                + " sh @ "
                + ("%.2f" % (getattr(result, "partial_price", 0) or 0))
                + " (booked $"
                + ("%.2f" % (getattr(result, "partial_pnl_dollars", 0) or 0))
                + ")",
            )
        except Exception:
            pass
    return result


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
    """Store v10's computed shares for an imminent execute_entry call.

    v7.30.0: serialized via _sizes_lock so scan-thread writes don't
    interleave with broker-thread pops on the same key.
    """
    with _sizes_lock:
        _pending_v10_sizes[(portfolio_id, ticker)] = int(shares)


def consume_v10_size(portfolio_id: str, ticker: str) -> Optional[int]:
    """Pop the stashed shares for a (portfolio_id, ticker). Returns None
    if no v10 size was stashed (legacy fallback).

    v7.30.0: serialized via _sizes_lock."""
    with _sizes_lock:
        return _pending_v10_sizes.pop((portfolio_id, ticker), None)


def peek_v10_size(portfolio_id: str, ticker: str) -> Optional[int]:
    """Read without consuming (for diagnostics/tests).

    v7.30.0: serialized via _sizes_lock."""
    with _sizes_lock:
        return _pending_v10_sizes.get((portfolio_id, ticker))


def rollback_admit(portfolio_id: str, ticker: str, ticket_id: str = "", reason: str = "") -> bool:
    """v7.81.0 -- unwind a v10 admit when the downstream broker call
    returned without filling.

    Pre-v7.81.0 the FSM transitioned to IN_POS and the RiskBook
    reserved capacity in `OrbEngine.try_enter` BEFORE the broker call
    ran. If `callbacks.execute_entry` early-returned (daily-loss
    limit, new-position cutoff, post-loss cooldown, insufficient
    cash, etc.) the FSM stayed stuck IN_POS with no actual position.
    The dashboard surfaced this as the "phantom IN_POS" pattern
    caught by `inv_v10_in_pos_has_internal_position`.

    Same shape for Val/Gene in mirror mode (ORB_PORTFOLIO_FIRE=0):
    they admit and transition IN_POS, but their broker fire is
    deferred so no position is ever opened in their book.

    This helper undoes both:
      1. Releases the RiskBook ticket so capacity flows back.
      2. Transitions the FSM back to ARMED so the ticker can re-fire
         when the breakout condition still holds.

    Idempotent and safe to call when the runtime isn't bootstrapped
    (returns False). Returns True if any state was actually rolled
    back.
    """
    if _engine is None:
        return False
    any_change = False
    # 1. Release the RiskBook ticket (if we know the id).
    if ticket_id:
        try:
            rb = _engine._risk.get(portfolio_id)
            if rb is not None and rb.release_by_id(ticket_id):
                any_change = True
        except Exception as e:
            logger.warning(
                "[V79-ORB-ROLLBACK] release_by_id %s/%s ticket=%s: %s",
                portfolio_id,
                ticker,
                ticket_id[:8],
                e,
            )
    # 2. Transition FSM back to ARMED so the ticker can re-fire.
    try:
        from orb import state as _state

        ds = _engine._state.get_day_state(portfolio_id, ticker)
        if ds.phase == _state.PHASE_IN_POS or ds.in_position:
            ds.transition(_state.PHASE_ARMED)
            ds.in_position = False
            any_change = True
    except Exception as e:
        logger.warning(
            "[V79-ORB-ROLLBACK] FSM rollback %s/%s: %s",
            portfolio_id,
            ticker,
            e,
        )
    # 3. Remove from LiveAdapter._open_positions so engine_positions on
    # the dashboard clears and the ticker isn't shown as an open position
    # when the broker order was never filled.
    # v9.1.96: _open_positions is keyed by risk_ticket_id (not ticker).
    # Use _ticker_to_ticket to find the correct key. v9.1.90's ticker-key
    # lookup was a no-op; this is the correct version.
    try:
        adapter = get_adapter(portfolio_id)
        if adapter is not None:
            tk_upper = ticker.upper()
            tid = adapter._ticker_to_ticket.get(tk_upper)
            if tid and tid in adapter._open_positions:
                del adapter._open_positions[tid]
                adapter._ticker_to_ticket.pop(tk_upper, None)
                any_change = True
            elif tk_upper in adapter._ticker_to_ticket:
                # Dangling reverse-lookup with no position — clean it up.
                adapter._ticker_to_ticket.pop(tk_upper, None)
                any_change = True
    except Exception as e:
        logger.warning(
            "[V79-ORB-ROLLBACK] adapter clear %s/%s: %s",
            portfolio_id,
            ticker,
            e,
        )

    if any_change:
        logger.warning(
            "[V79-ORB-ROLLBACK] %s/%s ticket=%s reason=%s -- FSM->ARMED, RiskBook released, adapter cleared",
            portfolio_id,
            ticker,
            ticket_id[:8] if ticket_id else "?",
            reason or "<none>",
        )
        # Flush state immediately so a redeploy mid-rollback restores the
        # post-rollback state, not the pre-rollback (stale) state.
        try:
            persist_engine_state()
        except Exception as _fe:
            logger.debug("[V79-ORB-ROLLBACK] state flush failed: %s", _fe)
    return any_change


def purge_phantom_engine_positions(
    portfolio_id: str,
    broker_tickers: "frozenset[str]",
) -> list[str]:
    """v9.1.96 -- remove engine positions that don't exist in the broker.

    Called from engine/scan.py after fetching executor Alpaca positions
    so that phantom state (positions tracked by the ORB engine that were
    never filled, or were force-closed by the broker) can be cleaned up
    without waiting for an EOD flush.

    Returns a list of ticker names that were purged.

    Safe to call when the runtime isn't bootstrapped (returns []).
    """
    if _engine is None or _adapters is None:
        return []
    adapter = _adapters.get(portfolio_id)
    if adapter is None:
        return []
    purged: list[str] = []
    broker_upper = frozenset(t.upper() for t in (broker_tickers or []))
    # Build a complete ticker→ticket map including positions that were
    # restored from state without _ticker_to_ticket being populated
    # (state restore only writes _open_positions, not the reverse lookup).
    _full_map: dict[str, str] = dict(adapter._ticker_to_ticket)
    for tid, pos in list(adapter._open_positions.items()):
        tk_from_pos = (getattr(pos, "ticker", None) or "").upper()
        if tk_from_pos and tk_from_pos not in _full_map:
            _full_map[tk_from_pos] = tid
    # Snapshot the complete map to avoid mutation-during-iteration.
    for tk, tid in list(_full_map.items()):
        if tk.upper() not in broker_upper:
            # Phantom: engine thinks it's open but broker doesn't.
            adapter._open_positions.pop(tid, None)
            adapter._ticker_to_ticket.pop(tk, None)
            # Release the RiskBook ticket so concurrent risk flows back.
            try:
                rb = _engine._risk.get(portfolio_id)
                if rb is not None:
                    rb.release_by_id(tid)
            except Exception as _re:
                logger.debug("[V9196-RECONCILE] RiskBook release %s/%s: %s", portfolio_id, tk, _re)
            purged.append(tk)
            logger.warning(
                "[V9196-RECONCILE] purged phantom engine position %s/%s (not in broker positions)",
                portfolio_id,
                tk,
            )
    if purged:
        try:
            persist_engine_state()
        except Exception as _fe:
            logger.debug("[V9196-RECONCILE] state flush failed: %s", _fe)
    return purged


def inject_missing_engine_positions(
    portfolio_id: str,
    broker_positions: "list[tuple[str, str, float, int]]",
) -> list[str]:
    """v9.1.97 -- inject Alpaca positions that are missing from the engine.

    Complement to purge_phantom_engine_positions: handles the reverse
    case where the broker holds a real position that the ORB engine lost
    track of (e.g. NFLX SHORT surviving after a mid-rollback state
    mismatch). Without injection the position card shows no stop/bar.

    Args:
        portfolio_id: "val" or "gene".
        broker_positions: list of (ticker, side, entry_price, qty) tuples
            from the Alpaca client (avg_entry_price for entry_price).
            Use 0.0 for entry_price when Alpaca doesn't provide it —
            the synthetic stop will fall back to a fixed-pct estimate
            from current adapter close price.

    Uses a synthetic stop at 1.5% from entry (enough to render the bar;
    actual stop management still flows through the Main bus EXIT signal).

    Returns list of ticker names that were injected. Safe when not
    bootstrapped (returns []).
    """
    if _engine is None or _adapters is None:
        logger.warning("[V9197-INJECT] %s: engine or adapters not bootstrapped", portfolio_id)
        return []
    adapter = _adapters.get(portfolio_id)
    rb = _engine._risk.get(portfolio_id)
    if adapter is None:
        logger.warning("[V9197-INJECT] %s: no adapter", portfolio_id)
        return []
    if rb is None:
        logger.warning(
            "[V9197-INJECT] %s: no risk_book (portfolio_ids=%s)",
            portfolio_id,
            list(_engine._risk.keys()) if _engine else "N/A",
        )
        return []

    from orb import risk_book as _rb_mod
    from orb.exits import make_position as _make_pos

    # Build current set of engine-tracked tickers.
    tracked = {pos.ticker.upper() for pos in adapter._open_positions.values()}
    logger.warning(
        "[V9197-INJECT] %s: checking %d broker positions, tracked=%s",
        portfolio_id,
        len(broker_positions),
        sorted(tracked),
    )
    injected: list[str] = []

    for broker_ticker, side, entry_price, qty in broker_positions:
        tk = broker_ticker.upper()
        if tk in tracked:
            continue  # already in engine
        if qty <= 0:
            continue

        # Estimate entry from adapter's last close if not provided.
        ep = float(entry_price or 0.0)
        if ep <= 0:
            ep = adapter._last_5m_close.get(tk, 0.0)
        if ep <= 0:
            logger.warning(
                "[V9197-INJECT] %s/%s: no entry price available, skipping", portfolio_id, tk
            )
            continue

        # Synthetic stop at 1.5% from entry (renders the bar; not used for exit decisions).
        side_lo = (side or "").lower()
        if side_lo in ("short", "sell"):
            synth_stop = ep * 1.015
        else:
            synth_stop = ep * 0.985

        rr = float(getattr(_engine.cfg, "rr", 2.5) or 2.5)
        recover_tid = f"recover-inject-{tk}-{portfolio_id}"
        try:
            pos = _make_pos(
                portfolio_id=portfolio_id,
                ticker=tk,
                side="short" if side_lo in ("short", "sell") else "long",
                entry_price=ep,
                stop=synth_stop,
                rr=rr,
                shares=int(qty),
                risk_ticket_id=recover_tid,
            )
            with rb._lock:
                rb._open_tickets[recover_tid] = _rb_mod._Ticket(
                    ticket_id=recover_tid,
                    risk_dollars=pos.risk_dollars,
                    notional=pos.notional,
                )
                rb._open_risk += pos.risk_dollars
                rb._open_notional += pos.notional
            adapter._open_positions[recover_tid] = pos
            adapter._ticker_to_ticket[tk] = recover_tid
            injected.append(tk)
            logger.warning(
                "[V9197-INJECT] injected missing engine position %s/%s %s entry=%.4f stop=%.4f shares=%d",
                portfolio_id,
                tk,
                side_lo,
                ep,
                synth_stop,
                int(qty),
            )
        except Exception as _ie:
            logger.warning("[V9197-INJECT] %s/%s failed: %s", portfolio_id, tk, _ie)

    if injected:
        try:
            persist_engine_state()
        except Exception as _fe:
            logger.debug("[V9197-INJECT] state flush failed: %s", _fe)
    return injected


def check_exit_by_ticker(
    *,
    portfolio_id: str,
    ticker: str,
    bar_high: float,
    bar_low: float,
    bar_close: float,
    bar_bucket_min: int,
) -> ExitResult:
    """Per-portfolio exit decision by ticker (no ticket_id required).

    Convenience for callers (broker/positions.py:manage_positions) that
    don't track v10 ticket ids on their position dicts. Returns
    `exit=False, reason="no_open_v10_position"` when there's no v10
    position open for this (portfolio, ticker) -- the common case
    during the v10/legacy coexistence window where some open positions
    in tg.positions are still legacy and have no v10 ticket.
    """
    if not is_live_mode_on():
        return ExitResult(exit=False, reason="live_mode_off")
    with _bootstrap_lock:
        adapters = _adapters
    if adapters is None:
        return ExitResult(exit=False, reason="live_mode_off")
    a = adapters.get(portfolio_id)
    if a is None:
        return ExitResult(exit=False, reason=f"no_adapter:{portfolio_id}")
    result = a.check_exit_by_ticker(
        ticker,
        bar_high=bar_high,
        bar_low=bar_low,
        bar_close=bar_close,
        bar_bucket_min=bar_bucket_min,
    )
    if result and result.exit:
        try:
            _record_activity(
                kind="exit",
                ticker=ticker,
                pid=portfolio_id,
                detail=str(result.reason or "") + " @ " + ("%.2f" % (result.price or 0)),
            )
        except Exception:
            pass
    # v8.1.2 -- partial-fills surface in the activity feed too.
    elif result and getattr(result, "partial", False):
        try:
            _record_activity(
                kind="partial",
                ticker=ticker,
                pid=portfolio_id,
                detail=str(int(getattr(result, "partial_shares", 0) or 0))
                + " sh @ "
                + ("%.2f" % (getattr(result, "partial_price", 0) or 0))
                + " (booked $"
                + ("%.2f" % (getattr(result, "partial_pnl_dollars", 0) or 0))
                + ")",
            )
        except Exception:
            pass
    return result


def snapshot() -> dict:
    """JSON-shaped state for /api/state. Returns minimal stub if not
    bootstrapped.

    v7.32.0: snapshot _engine under the bootstrap lock to avoid a
    partial-init read race.
    v9.1.0: includes EOD reversal addon engine snapshot under "eod"
    when bootstrapped."""
    with _bootstrap_lock:
        engine = _engine
        eod = _eod_engine
        session_date = _session_date
    if engine is None:
        return {
            "bootstrapped": False,
            "live_mode": is_live_mode_on(),
            "session_date": "",
        }
    snap = engine.snapshot()
    snap["bootstrapped"] = True
    snap["live_mode"] = is_live_mode_on()
    snap["session_date"] = session_date
    # v7.45.0: recent activity feed for the dashboard
    snap["activity"] = get_recent_activity(limit=25)
    # v9.1.0: EOD reversal addon state
    if eod is not None:
        snap["eod"] = eod.snapshot()
    return snap


# v9.1.0 -- EOD reversal hooks. Called by engine/scan.py at 15:30 ET
# (entry attempt) and 15:59 ET (flatten). Each hook is idempotent
# and safe to call repeatedly within its time window.


def get_eod_engine() -> Optional[EodReversalEngine]:
    """Return the EOD engine (None if not bootstrapped or disabled)."""
    with _bootstrap_lock:
        eod = _eod_engine
    if eod is None or not eod.cfg.enabled:
        return None
    return eod


def eod_reset_session_if_needed(date_iso: str) -> None:
    """Reset the EOD engine state for a new trading day. Idempotent."""
    eod = get_eod_engine()
    if eod is not None:
        eod.reset_for_session(date_iso)


# --- diagnostic helpers (for tests + manual ops) ---


def _reset_for_testing() -> None:
    """Tear down the singleton. ONLY for tests.

    v7.32.0: acquires _bootstrap_lock + _sizes_lock so an aggressive
    parallel test runner can't observe a half-reset state.
    """
    global _engine, _adapters, _bootstrapped, _session_date
    with _bootstrap_lock:
        _engine = None
        _adapters = None
        _bootstrapped = False
        _session_date = ""
    with _sizes_lock:
        _pending_v10_sizes.clear()
