"""v8.3.4 -- engine state persistence to /data/orb_state_<date>.json.

Covers categories A-F from the operator's persistence directive:

  A. OR windows           -> _state.or_windows
  B. DayState FSM         -> _state.day_states (phase, in_position,
                             trades_today, last_*_iso, etc.)
  C. RiskBook              -> per-portfolio realized_pnl_today,
                             daily_kill_triggered, _open_tickets
  D. Activity feed         -> live_runtime._recent_activity (last 50)
  E. Wash-sale tracker     -> engine._recent_losses, wash_risk_count
  F. Pending v10 sizes     -> live_runtime._pending_v10_sizes

Schema is documented inline in ``serialize_engine_state``. Writer is
called once per scan cycle from engine/scan.py (cheap JSON dump,
typically <5 KB). Reader is called from live_runtime.bootstrap and
ensure_session_started; overlay only fires when the disk file's
date_iso matches today's date.

Storage path: defaults to ``<data_dir>/orb_state_<date>.json`` where
``data_dir`` matches ``trade_genius.PAPER_STATE_FILE``'s directory.
Override via env var ``ORB_STATE_PERSIST_PATH`` (full path including
the date placeholder, e.g. ``/data/orb_state_{date}.json``).

Failure-tolerant: all I/O is wrapped in try/except. A write failure
emits a debug log; a read failure returns None. Neither blocks the
trading path.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

PERSIST_SCHEMA_VERSION = 1


def _default_path_template() -> str:
    """Resolve where orb_state files live. Honors ORB_STATE_PERSIST_PATH
    when set; falls back to ``<paper_state_dir>/orb_state_{date}.json``.
    """
    env = os.environ.get("ORB_STATE_PERSIST_PATH", "").strip()
    if env:
        return env if "{date}" in env else env  # accept either form
    # Match the existing PAPER_STATE_FILE directory convention.
    paper_state = os.environ.get("PAPER_STATE_PATH", "paper_state.json")
    base_dir = os.path.dirname(paper_state) or "."
    return os.path.join(base_dir, "orb_state_{date}.json")


def resolve_path(date_iso: str) -> str:
    """Resolve the absolute path for `date_iso`'s state file."""
    template = _default_path_template()
    if "{date}" in template:
        return template.replace("{date}", date_iso)
    # Env var without a {date} placeholder = single fixed path; the date
    # filtering then happens via the file's contents (date_iso key).
    return template


# ----- Serialize ---------------------------------------------------


def serialize_engine_state(engine,
                           *,
                           recent_activity: Optional[list] = None,
                           pending_v10_sizes: Optional[dict] = None,
                           date_iso: str = "",
                           bot_version: str = "") -> dict:
    """Build the on-disk JSON for the current engine + live_runtime state.

    Pure (no I/O); caller passes in the live_runtime side data.
    """
    state = engine._state
    risk_registry = engine._risk

    # A. OR windows (full snapshot including bars_seen + lock flag)
    or_windows = {}
    for ticker, w in state.or_windows.items():
        or_windows[ticker] = {
            "ticker": w.ticker,
            "or_minutes": w.or_minutes,
            "or_high": w.or_high,
            "or_low": w.or_low,
            "or_open": w.or_open,
            "or_close": w.or_close,
            "or_volume": w.or_volume,
            "bars_seen": w.bars_seen,
            "locked": w.locked,
            "locked_at_iso": w.locked_at_iso,
        }

    # B. DayState FSM (every (pid, ticker) row)
    day_states = []
    for (pid, ticker), ds in state.day_states.items():
        day_states.append({
            "portfolio_id": pid,
            "ticker": ticker,
            "phase": ds.phase,
            "block_reason": ds.block_reason,
            "trades_today": ds.trades_today,
            "in_position": ds.in_position,
            "last_signal_bucket": ds.last_signal_bucket,
            "last_entry_iso": ds.last_entry_iso,
            "last_exit_iso": ds.last_exit_iso,
            "consecutive_losses": ds.consecutive_losses,
        })

    # C. RiskBook per-portfolio (realized P&L + open tickets + daily-kill)
    risk_books = {}
    for pid in engine.portfolio_ids:
        rb = risk_registry.get(pid)
        if rb is None:
            continue
        # Access open_tickets under the lock for a consistent snapshot.
        with rb._lock:
            tickets = [
                {"ticket_id": t.ticket_id,
                 "risk_dollars": t.risk_dollars,
                 "notional": t.notional}
                for t in rb._open_tickets.values()
            ]
            risk_books[pid] = {
                "session_start_equity": rb._session_start_equity,
                "equity": rb._equity,
                "realized_pnl_today": rb._realized_pnl_today,
                "open_risk": rb._open_risk,
                "open_notional": rb._open_notional,
                "daily_kill_triggered": rb.daily_kill_triggered,
                "open_tickets": tickets,
                "admit_count": rb.admit_count,
                "reject_count": rb.reject_count,
            }

    # E. Wash-sale tracker (engine-level, session-scoped counter + recent)
    wash_risk = {
        "wash_risk_count": int(getattr(engine, "wash_risk_count", 0) or 0),
        "recent_losses": {},
    }
    for (ticker, side), entries in getattr(engine, "_recent_losses", {}).items():
        # Tuple key -> string "ticker|side" so it survives JSON.
        wash_risk["recent_losses"][f"{ticker}|{side}"] = list(entries)

    # D. Activity feed (deque or list of dicts)
    activity = list(recent_activity or [])

    # F. Pending v10 sizes. Keys are (portfolio_id, ticker) tuples;
    # JSON can't store tuple keys, so we encode as "pid|ticker".
    pending_sizes: dict = {}
    for k, v in (pending_v10_sizes or {}).items():
        if isinstance(k, tuple) and len(k) == 2:
            pending_sizes[f"{k[0]}|{k[1]}"] = v
        else:
            pending_sizes[str(k)] = v

    return {
        "schema_version": PERSIST_SCHEMA_VERSION,
        "bot_version": bot_version,
        "date_iso": date_iso,
        "saved_at_iso": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "session_date": state.session_date,
        "or_windows": or_windows,
        "day_states": day_states,
        "risk_books": risk_books,
        "wash_risk": wash_risk,
        "activity": activity,
        "pending_v10_sizes": pending_sizes,
    }


# ----- Write -------------------------------------------------------


def dump_state_to_disk(engine,
                       *,
                       recent_activity: Optional[list] = None,
                       pending_v10_sizes: Optional[dict] = None,
                       date_iso: str,
                       bot_version: str = "",
                       path: Optional[str] = None) -> bool:
    """Serialize engine state and atomically write to disk.

    Returns True on success, False on any failure. Never raises.

    Atomic write: writes to ``<path>.tmp`` then renames; on POSIX
    rename is atomic so a crash mid-write can't produce a corrupt
    half-file.
    """
    if not date_iso:
        return False
    target = path or resolve_path(date_iso)
    try:
        payload = serialize_engine_state(
            engine,
            recent_activity=recent_activity,
            pending_v10_sizes=pending_v10_sizes,
            date_iso=date_iso,
            bot_version=bot_version,
        )
    except Exception as e:
        logger.debug("[V834-PERSIST] serialize failed: %s", e)
        return False
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        # Write to a sibling tempfile in the same directory so the
        # final rename is on the same filesystem.
        dir_part = os.path.dirname(target) or "."
        fd, tmp = tempfile.mkstemp(prefix=".orb_state.", suffix=".tmp",
                                   dir=dir_part)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"), default=str)
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception as e:
        logger.debug("[V834-PERSIST] dump failed path=%s: %s", target, e)
        return False


# ----- Read --------------------------------------------------------


def load_state_from_disk(date_iso: str,
                         *, path: Optional[str] = None,
                         ) -> Optional[dict]:
    """Read the persisted state for `date_iso`. Returns the parsed dict
    if (a) the file exists, (b) loads as valid JSON, and (c) its
    ``date_iso`` matches the requested date. Otherwise returns None.

    The date-match guard means a yesterday's file lingering on disk
    can't pollute today's session.
    """
    if not date_iso:
        return None
    target = path or resolve_path(date_iso)
    if not os.path.exists(target):
        return None
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.debug("[V834-PERSIST] load failed path=%s: %s", target, e)
        return None
    if not isinstance(data, dict):
        return None
    if data.get("date_iso") != date_iso:
        return None
    return data


# ----- Apply (overlay onto live engine state) ----------------------


def apply_loaded_state(engine, loaded: dict,
                       *,
                       recent_activity=None,
                       pending_v10_sizes=None) -> dict:
    """Overlay a previously-dumped state onto the live engine + live-
    runtime side data. Designed to be called AFTER
    ``OrbEngine.start_new_session()`` runs its reset; the loaded data
    re-fills exactly what the reset cleared, plus the side data.

    Returns counters: {or_windows_loaded, day_states_loaded,
    risk_books_loaded, activity_loaded, pending_sizes_loaded}.

    Failure-tolerant per category: a malformed RiskBook entry doesn't
    prevent OR windows from rehydrating, etc.
    """
    from orb import state as _state_mod
    from orb import risk_book as _rb_mod
    from collections import defaultdict
    counters = {
        "or_windows_loaded": 0,
        "day_states_loaded": 0,
        "risk_books_loaded": 0,
        "activity_loaded": 0,
        "pending_sizes_loaded": 0,
    }
    if not isinstance(loaded, dict):
        return counters
    state = engine._state

    # A. OR windows
    for ticker, raw in (loaded.get("or_windows") or {}).items():
        if not isinstance(raw, dict):
            continue
        try:
            w = _state_mod.OrWindow(
                ticker=str(raw.get("ticker") or ticker),
                or_minutes=int(raw.get("or_minutes") or 30),
                or_high=raw.get("or_high"),
                or_low=raw.get("or_low"),
                or_open=raw.get("or_open"),
                or_close=raw.get("or_close"),
                or_volume=float(raw.get("or_volume") or 0.0),
                bars_seen=int(raw.get("bars_seen") or 0),
                locked=bool(raw.get("locked")),
                locked_at_iso=raw.get("locked_at_iso"),
            )
        except Exception:
            continue
        state.or_windows[ticker] = w
        counters["or_windows_loaded"] += 1

    # B. DayState FSM
    for raw in (loaded.get("day_states") or []):
        if not isinstance(raw, dict):
            continue
        pid = str(raw.get("portfolio_id") or "")
        ticker = str(raw.get("ticker") or "")
        if not pid or not ticker:
            continue
        try:
            ds = _state_mod.TickerDayState(
                portfolio_id=pid,
                ticker=ticker,
                phase=str(raw.get("phase") or _state_mod.PHASE_WARMUP),
                block_reason=str(raw.get("block_reason") or ""),
                trades_today=int(raw.get("trades_today") or 0),
                in_position=bool(raw.get("in_position")),
                last_signal_bucket=raw.get("last_signal_bucket"),
                last_entry_iso=raw.get("last_entry_iso"),
                last_exit_iso=raw.get("last_exit_iso"),
                consecutive_losses=int(raw.get("consecutive_losses") or 0),
            )
        except Exception:
            continue
        state.day_states[(pid, ticker)] = ds
        counters["day_states_loaded"] += 1

    # C. RiskBook per-portfolio
    risk_registry = engine._risk
    for pid, raw in (loaded.get("risk_books") or {}).items():
        if not isinstance(raw, dict):
            continue
        rb = risk_registry.get(pid)
        if rb is None:
            continue
        try:
            with rb._lock:
                rb._session_start_equity = float(
                    raw.get("session_start_equity") or rb._session_start_equity)
                rb._equity = float(raw.get("equity") or rb._equity)
                rb._realized_pnl_today = float(
                    raw.get("realized_pnl_today") or 0.0)
                rb._open_risk = float(raw.get("open_risk") or 0.0)
                rb._open_notional = float(raw.get("open_notional") or 0.0)
                rb.daily_kill_triggered = bool(
                    raw.get("daily_kill_triggered"))
                rb.admit_count = int(raw.get("admit_count") or 0)
                rb.reject_count = int(raw.get("reject_count") or 0)
                rb._open_tickets.clear()
                for t_raw in (raw.get("open_tickets") or []):
                    if not isinstance(t_raw, dict):
                        continue
                    tid = str(t_raw.get("ticket_id") or "")
                    if not tid:
                        continue
                    rb._open_tickets[tid] = _rb_mod._Ticket(
                        ticket_id=tid,
                        risk_dollars=float(t_raw.get("risk_dollars") or 0.0),
                        notional=float(t_raw.get("notional") or 0.0),
                    )
        except Exception:
            continue
        counters["risk_books_loaded"] += 1

    # E. Wash-sale tracker
    wash = loaded.get("wash_risk") or {}
    try:
        engine.wash_risk_count = int(wash.get("wash_risk_count") or 0)
        # Rebuild _recent_losses defaultdict from the {ticker|side: [...]}
        # serialization.
        rl = defaultdict(list)
        for key, entries in (wash.get("recent_losses") or {}).items():
            if "|" not in str(key):
                continue
            ticker, side = str(key).split("|", 1)
            if isinstance(entries, list):
                rl[(ticker, side)] = list(entries)
        engine._recent_losses = rl
    except Exception:
        pass

    # D. Activity feed (caller-side ring buffer)
    if recent_activity is not None:
        try:
            events = loaded.get("activity") or []
            if isinstance(events, list):
                recent_activity.clear()
                for ev in events:
                    if isinstance(ev, dict):
                        recent_activity.append(ev)
                        counters["activity_loaded"] += 1
        except Exception:
            pass

    # F. Pending v10 sizes (decode "pid|ticker" back to (pid, ticker) tuple)
    if pending_v10_sizes is not None:
        try:
            sizes = loaded.get("pending_v10_sizes") or {}
            if isinstance(sizes, dict):
                pending_v10_sizes.clear()
                for k, v in sizes.items():
                    skey = str(k)
                    if "|" not in skey:
                        continue
                    pid, ticker = skey.split("|", 1)
                    try:
                        pending_v10_sizes[(pid, ticker)] = int(v)
                        counters["pending_sizes_loaded"] += 1
                    except (TypeError, ValueError):
                        continue
        except Exception:
            pass

    return counters


def prune_stale_state_files(today_iso: str, *,
                            keep_days: int = 5,
                            base_dir: Optional[str] = None) -> int:
    """Delete orb_state_YYYY-MM-DD.json files older than `keep_days`
    days from `today_iso`. Returns the number of files removed.

    Defensive: silently swallows any I/O errors; never raises into the
    trading path.
    """
    template = _default_path_template()
    if "{date}" not in template:
        return 0  # single fixed path; nothing to prune
    if base_dir is None:
        base_dir = os.path.dirname(template) or "."
    basename_tmpl = os.path.basename(template)
    pre, post = basename_tmpl.split("{date}", 1)
    try:
        files = os.listdir(base_dir)
    except OSError:
        return 0
    try:
        today_dt = datetime.strptime(today_iso, "%Y-%m-%d")
    except ValueError:
        return 0
    removed = 0
    for fname in files:
        if not fname.startswith(pre) or not fname.endswith(post):
            continue
        date_part = fname[len(pre):len(fname) - len(post)] if post else fname[len(pre):]
        try:
            file_dt = datetime.strptime(date_part, "%Y-%m-%d")
        except ValueError:
            continue
        age_days = (today_dt - file_dt).days
        if age_days > keep_days:
            try:
                os.remove(os.path.join(base_dir, fname))
                removed += 1
            except OSError:
                continue
    return removed
