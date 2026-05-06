"""Paper-book state persistence: load, save, reset.

Extracted from trade_genius.py in v4.6.0 for maintainability. Pure code
motion \u2014 zero behavior change. The mutable state globals
(paper_cash, positions, etc.) still live in trade_genius; this module
reads/writes them through the live-module accessor below.
"""

from __future__ import annotations

# v4.5.4 / v4.6.0 \u2014 prod runs `python trade_genius.py`, so trade_genius
# is registered in sys.modules as `__main__`, NOT as `trade_genius`.
# Without the alias below, `from trade_genius import (...)` would
# re-execute trade_genius.py from disk under a second module name,
# which re-enters run_telegram_bot() while this module is still
# partially initialized. Aliasing __main__ as `trade_genius` makes
# both names point at the same already-loaded module object.
import sys as _sys

if "trade_genius" not in _sys.modules and "__main__" in _sys.modules:
    _main = _sys.modules["__main__"]
    if getattr(_main, "BOT_NAME", None) == "TradeGenius":
        _sys.modules["trade_genius"] = _main


def _tg():
    """Live trade_genius module (handles __main__ vs imported cases)."""
    return _sys.modules.get("trade_genius") or _sys.modules.get("__main__")


import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import persistence

logger = logging.getLogger(__name__)

# Owned state \u2014 moved out of trade_genius in v4.6.0.
_paper_save_lock = threading.Lock()
_state_loaded = False


# v6.0.4 \u2014 Sentinel persistence rehydration.
#
# Background: ``save_paper_state`` writes ``state`` with
# ``json.dump(..., default=str)``. The ``default=str`` callback fires for
# any non-JSON-serializable value (e.g. ``collections.deque``,
# ``engine.alarm_f_trail.TrailState``) and stringifies the value to its
# ``repr()``. On reload, ``json.load`` returns those fields as plain
# strings rather than the live container. The Sentinel hot path then
# crashes on the first ``history.append(...)`` call (str has no append),
# the broad ``try/except`` in ``broker.positions._maybe_emit_sentinel``
# swallows the AttributeError, and Alarms A/B/C/F never run for the
# rest of the process lifetime.
#
# Two-pronged fix in v6.0.4:
#
#  (1) STRIP-ON-SAVE: ``_strip_runtime_caches`` removes the per-tick
#      caches from each position dict before they reach ``json.dump``.
#      These fields are pure runtime memory \u2014 ``pnl_history`` is a
#      bounded deque rebuilt within seconds of any restart, and
#      ``trail_state`` reseats itself within MIN_BARS_BEFORE_ARM=3 1m
#      bars. Persisting them was a latent bug since v5.13.2 / v5.28.0.
#
#  (2) REHYDRATE-ON-LOAD: ``_rehydrate_runtime_caches`` is a defensive
#      pass over loaded ``positions`` / ``short_positions``. If older
#      paper_state.json files (from v6.0.3 and earlier) still hold
#      string remnants, this resets them to fresh objects so the very
#      next Sentinel tick succeeds.
_RUNTIME_CACHE_KEYS = ("pnl_history", "trail_state", "v531_prior_alarm_codes")


def _strip_runtime_caches(pos_map):
    """Return a copy of ``pos_map`` with non-JSON-friendly runtime caches dropped.

    Each value is shallow-copied so the in-memory dict the live engine
    keeps using retains the deque / TrailState references; only the
    on-disk snapshot loses them.
    """
    out = {}
    for ticker, pos in (pos_map or {}).items():
        if isinstance(pos, dict):
            stripped = {k: v for k, v in pos.items() if k not in _RUNTIME_CACHE_KEYS}
            out[ticker] = stripped
        else:
            out[ticker] = pos
    return out


def _rehydrate_runtime_caches(pos_map):
    """Repair string-typed runtime caches surviving from older saves.

    Mutates ``pos_map`` in place. Imports happen lazily so this module
    stays importable even if engine.* hasn't loaded yet.
    """
    if not pos_map:
        return
    try:
        from engine.sentinel import new_pnl_history
    except Exception:
        new_pnl_history = None
    try:
        from engine.alarm_f_trail import TrailState
    except Exception:
        TrailState = None
    repaired = 0
    for ticker, pos in pos_map.items():
        if not isinstance(pos, dict):
            continue
        ph = pos.get("pnl_history")
        if ph is None or isinstance(ph, str):
            if new_pnl_history is not None:
                pos["pnl_history"] = new_pnl_history()
                repaired += 1
            else:
                pos.pop("pnl_history", None)
        ts = pos.get("trail_state")
        if ts is None or isinstance(ts, str):
            if TrailState is not None:
                pos["trail_state"] = TrailState.fresh()
                repaired += 1
            else:
                pos.pop("trail_state", None)
        prior = pos.get("v531_prior_alarm_codes")
        if isinstance(prior, str):
            pos["v531_prior_alarm_codes"] = []
            repaired += 1
    if repaired:
        logger.info(
            "[PERSISTENCE] rehydrated %d runtime cache field(s) on load", repaired
        )


def save_paper_state():
    """Persist paper trading + strategy state to disk. Thread-safe, atomic."""
    tg = _tg()
    t0 = time.time()
    if not _state_loaded:
        logger.warning("save_paper_state skipped \u2014 state not yet loaded")
        return
    # Data-loss guard: warn if history empty but cash changed (trades
    # happened then vanished). v3.3.1: also check for currently-open
    # positions \u2014 a short entry credits cash immediately but only
    # appends to short_trade_history on COVER, so an open-short session
    # is a legitimate state with empty history and moved cash. Only
    # warn when there's no record of ANY activity (no history AND no
    # open positions) yet cash has moved.
    has_any_activity = (
        bool(tg.trade_history)
        or bool(tg.short_trade_history)
        or bool(tg.positions)
        or bool(tg.short_positions)
    )
    if (not has_any_activity) and tg.paper_cash != tg.PAPER_STARTING_CAPITAL:
        logger.warning(
            "DATA LOSS GUARD: no trade history or open positions but "
            "cash=$%.2f (start=$%.0f) \u2014 possible trade history wipe!",
            tg.paper_cash,
            tg.PAPER_STARTING_CAPITAL,
        )
    # v4.1.1: snapshot construction moved INSIDE the lock so a concurrent
    # save (5-min periodic + scan-loop close_position) cannot build a
    # half-mutated state dict while another save is iterating the same
    # globals for json.dump. Two saves racing on the same globals used to
    # risk "dictionary changed size during iteration".
    with _paper_save_lock:
        state = {
            "paper_cash": tg.paper_cash,
            # v6.0.4 \u2014 strip per-tick runtime caches (pnl_history /
            # trail_state / v531_prior_alarm_codes) before serialization.
            # See _strip_runtime_caches docstring for the why.
            "positions": _strip_runtime_caches(tg.positions),
            "paper_trades": list(tg.paper_trades),
            "paper_all_trades": list(tg.paper_all_trades[-500:]),
            "daily_entry_count": dict(tg.daily_entry_count),
            "daily_entry_date": tg.daily_entry_date,
            "or_high": dict(tg.or_high),
            "or_low": dict(tg.or_low),
            "pdc": dict(tg.pdc),
            "or_collected_date": tg.or_collected_date,
            "user_config": dict(tg.user_config),
            "trade_history": list(tg.trade_history),
            "short_positions": _strip_runtime_caches(tg.short_positions),
            "short_trade_history": list(tg.short_trade_history[-500:]),
            # v3.4.34: avwap_data / avwap_last_ts no longer persisted.
            "daily_short_entry_count": dict(tg.daily_short_entry_count),
            "daily_short_entry_date": tg.daily_short_entry_date,
            "last_exit_time": {k: v.isoformat() for k, v in tg._last_exit_time.items()},
            "_scan_paused": tg._scan_paused,
            "_trading_halted": tg._trading_halted,
            "_trading_halted_reason": tg._trading_halted_reason,
            # v5.1.8 \u2014 v5_long_tracks / v5_short_tracks are now persisted
            # in SQLite (persistence.v5_long_tracks table) rather than
            # serialized into paper_state.json. Reasons: (a) avoid the
            # non-atomic json.dump corrupting the whole portfolio file
            # on a mid-write crash, (b) decouple Tiger/Buffalo state
            # from the slower 5-minute cadence here. v5_active_direction
            # is small + cheap and stays in JSON.
            "v5_active_direction": dict(getattr(tg, "v5_active_direction", {})),
            "saved_at": tg._utc_now_iso(),
        }
        tmp = tg.PAPER_STATE_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, default=str)
            os.replace(tmp, tg.PAPER_STATE_FILE)
            try:
                persistence.replace_all_tracks(
                    dict(getattr(tg, "v5_long_tracks", {})),
                    dict(getattr(tg, "v5_short_tracks", {})),
                )
            except Exception as v5e:
                logger.error("save_paper_state: SQLite track sync failed: %s", v5e)
            logger.debug("Paper state saved -> %s (%.3fs)", tg.PAPER_STATE_FILE, time.time() - t0)
        except Exception as e:
            logger.error("save_paper_state failed: %s", e)


def load_paper_state():
    """Load paper trading state from disk on startup."""
    global _state_loaded
    tg = _tg()

    # Ensure SQLite store is initialized before the JSON load path so the
    # subsequent load_all_tracks call returns any tracks that were already
    # persisted by a prior boot.
    try:
        persistence.init_db()
    except Exception as e:
        logger.warning("persistence init failed: %s", e)

    if not os.path.exists(tg.PAPER_STATE_FILE):
        tg.paper_log(
            "No saved state at %s. Starting fresh $%.0f."
            % (tg.PAPER_STATE_FILE, tg.PAPER_STARTING_CAPITAL)
        )
        # Pull any v5 tracks already in SQLite (e.g. left over from a
        # previous run whose JSON file was rotated away).
        try:
            from tiger_buffalo_v5 import load_track as _v5_load, DIR_LONG, DIR_SHORT

            # v7.0.0 Phase 2B: use .clear() + .update() to preserve dict identity
            # so _MAIN_BOOK.v5_long_tracks stays bound to the same object after load.
            tg.v5_long_tracks.clear()
            tg.v5_long_tracks.update(
                {t: _v5_load(s, DIR_LONG) for t, s in persistence.load_all_tracks("long").items()}
            )
            tg.v5_short_tracks.clear()
            tg.v5_short_tracks.update(
                {t: _v5_load(s, DIR_SHORT) for t, s in persistence.load_all_tracks("short").items()}
            )
        except Exception as v5e:
            logger.warning("v5 SQLite restore failed: %s", v5e)
            tg.v5_long_tracks.clear()
            tg.v5_short_tracks.clear()
        tg.v5_active_direction.clear()
        _state_loaded = True
        return

    try:
        with open(tg.PAPER_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        tg.paper_cash = float(state.get("paper_cash", tg.PAPER_STARTING_CAPITAL))
        # v4.1.2: .clear() before .update() to symmetrize load semantics
        # with paper_trades/paper_all_trades/trade_history/short_trade_history
        # below. A second load_paper_state call (module re-init, hot patch)
        # would otherwise merge stale in-memory state on top of disk state.
        tg.positions.clear()
        tg.positions.update(state.get("positions", {}))
        # v6.0.4 \u2014 defense-in-depth: repair any string-typed runtime
        # caches left over from older saves so the first Sentinel tick
        # after restart succeeds. New saves strip these via
        # _strip_runtime_caches and never round-trip through JSON.
        _rehydrate_runtime_caches(tg.positions)
        tg.paper_trades.clear()
        tg.paper_trades.extend(state.get("paper_trades", []))
        tg.paper_all_trades.clear()
        tg.paper_all_trades.extend(state.get("paper_all_trades", []))
        tg.daily_entry_count.clear()
        tg.daily_entry_count.update(state.get("daily_entry_count", {}))
        tg.daily_entry_date = state.get("daily_entry_date", "")
        tg.or_high.clear()
        tg.or_high.update(state.get("or_high", {}))
        tg.or_low.clear()
        tg.or_low.update(state.get("or_low", {}))
        tg.pdc.clear()
        tg.pdc.update(state.get("pdc", {}))
        tg.or_collected_date = state.get("or_collected_date", "")
        tg.user_config.clear()
        tg.user_config.update(state.get("user_config", {}))
        tg.trade_history.clear()
        tg.trade_history.extend(state.get("trade_history", []))
        tg.short_positions.clear()
        tg.short_positions.update(state.get("short_positions", {}))
        _rehydrate_runtime_caches(tg.short_positions)
        tg.short_trade_history.clear()
        tg.short_trade_history.extend(state.get("short_trade_history", []))
        # v3.4.34: legacy "avwap_data"/"avwap_last_ts" keys in old
        # state files are silently ignored (no longer loaded).
        tg.daily_short_entry_count.clear()
        tg.daily_short_entry_count.update(state.get("daily_short_entry_count", {}))
        # v4.7.0: persist daily_short_entry_date so the daily-counter reset
        # in check_short_entry survives process restarts. Default "" for
        # backward-compat with state files written by v4.6.0 and earlier.
        tg.daily_short_entry_date = state.get("daily_short_entry_date", "")
        raw_exit = state.get("last_exit_time", {})

        # Normalize to UTC-aware. Older persisted state may contain
        # tz-naive ISO strings; mixing those with tz-aware datetime.now
        # raises "can't subtract offset-naive and offset-aware" and kills
        # entry checks silently. Assume naive == UTC (the original write
        # site has always used datetime.now(timezone.utc)).
        def _parse_exit_ts(v):
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        # Per-key try/except: one malformed stored value MUST NOT wipe
        # every ticker's cooldown map. Previously a single bad row in
        # paper_state.json disabled the 15-min re-entry guard for the
        # entire watchlist.
        tg._last_exit_time = {}
        for _k, _v in raw_exit.items():
            try:
                tg._last_exit_time[_k] = _parse_exit_ts(_v)
            except Exception:
                logger.warning(
                    "load_paper_state: dropping malformed last_exit_time[%r]=%r",
                    _k,
                    _v,
                )

        # Load persisted flags
        tg._scan_paused = state.get("_scan_paused", False)
        tg._trading_halted = state.get("_trading_halted", False)
        tg._trading_halted_reason = state.get("_trading_halted_reason", "")

        # Tiger/Buffalo tracks live in SQLite via persistence.load_all_tracks().
        # Any v5_* keys in the JSON file itself are ignored; the SQLite store is
        # the sole source of truth. Malformed rows are sanitized via
        # tiger_buffalo_v5.load_track.
        try:
            from tiger_buffalo_v5 import load_track, DIR_LONG, DIR_SHORT

            sql_long = persistence.load_all_tracks("long")
            sql_short = persistence.load_all_tracks("short")
            # v7.0.0 Phase 2B: .clear() + .update() to preserve dict identity.
            tg.v5_long_tracks.clear()
            tg.v5_long_tracks.update(
                {t: load_track(sql_long.get(t), DIR_LONG) for t in sql_long}
            )
            tg.v5_short_tracks.clear()
            tg.v5_short_tracks.update(
                {t: load_track(sql_short.get(t), DIR_SHORT) for t in sql_short}
            )
            tg.v5_active_direction.clear()
            tg.v5_active_direction.update(
                dict(state.get("v5_active_direction", {}) or {})
            )
        except Exception as v5e:
            logger.warning(
                "v5 tracks restore failed: %s \u2014 starting clean",
                v5e,
            )
            tg.v5_long_tracks.clear()
            tg.v5_short_tracks.clear()
            tg.v5_active_direction.clear()

        # Reset daily counts if saved on a different day
        today = tg._now_et().strftime("%Y-%m-%d")
        if tg.daily_entry_date != today:
            tg.daily_entry_count.clear()
            tg.daily_short_entry_count.clear()
            tg.paper_trades.clear()
            tg._trading_halted = False
            tg._trading_halted_reason = ""

        _state_loaded = True
        logger.info(
            "Loaded paper state: cash=$%.2f, %d positions, %d trade_history",
            tg.paper_cash,
            len(tg.positions),
            len(tg.trade_history),
        )
    except Exception as e:
        # v4.0.8 \u2014 previously we set _state_loaded = True and returned,
        # which let the next periodic save stamp a partially-loaded
        # snapshot (e.g. paper_cash loaded, positions never populated
        # before the exception) over the on-disk file. That silently
        # wiped positions/history and was unrecoverable. Now we hard-
        # reset in-memory state to a clean fresh-start book BEFORE
        # unblocking saves, so at worst we persist a legitimate
        # $100k / no-positions snapshot instead of a truncated one.
        logger.error(
            "load_paper_state failed: %s \u2014 resetting to fresh book to "
            "avoid persisting partial state on top of the on-disk file",
            e,
            exc_info=True,
        )
        tg.paper_cash = tg.PAPER_STARTING_CAPITAL
        tg.positions.clear()
        tg.paper_trades.clear()
        tg.paper_all_trades.clear()
        tg.daily_entry_count.clear()
        tg.daily_entry_date = ""
        tg.or_high.clear()
        tg.or_low.clear()
        tg.pdc.clear()
        tg.or_collected_date = ""
        tg.user_config.clear()
        tg.trade_history.clear()
        tg.short_positions.clear()
        tg.short_trade_history.clear()
        tg.daily_short_entry_count.clear()
        tg.daily_short_entry_date = ""
        tg._last_exit_time = {}
        tg._scan_paused = False
        tg._trading_halted = False
        tg._trading_halted_reason = ""
        # v5: clear tracks on a recovery reset. v7.0.0 Phase 2B: .clear() to
        # preserve dict identity so _MAIN_BOOK.v5_* stays bound to same objects.
        tg.v5_long_tracks.clear()
        tg.v5_short_tracks.clear()
        tg.v5_active_direction.clear()
        _state_loaded = True


def _do_reset_paper():
    """Execute paper portfolio reset."""
    tg = _tg()
    tg.positions.clear()
    tg.short_positions.clear()
    tg.paper_trades.clear()
    tg.paper_all_trades.clear()
    tg.trade_history.clear()
    tg.short_trade_history.clear()
    tg.daily_entry_count.clear()
    tg.daily_short_entry_count.clear()
    tg.daily_entry_date = ""
    tg.daily_short_entry_date = ""
    tg.paper_cash = tg.PAPER_STARTING_CAPITAL
    tg._trading_halted = False
    tg._trading_halted_reason = ""
    # v5.0.0 \u2014 reset Tiger/Buffalo tracks on a paper-book reset.
    # v5.1.8: tracks live in SQLite; replace_all_tracks with empty
    # dicts is what actually clears the persisted store.
    # v7.0.0 Phase 2B: .clear() to preserve dict identity.
    tg.v5_long_tracks.clear()
    tg.v5_short_tracks.clear()
    tg.v5_active_direction.clear()
    try:
        persistence.replace_all_tracks({}, {})
    except Exception as e:
        logger.warning("paper reset: SQLite track clear failed: %s", e)
    save_paper_state()
