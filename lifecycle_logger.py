"""v5.13.6 - per-position lifecycle event log.

Append-only JSONL log per position. One file per position_id under
``data_dir`` (default ``/data/lifecycle``). Each event is one JSON line.

Failure mode: best-effort. If a write fails the logger emits a warning
but never raises - the trading path must NEVER be blocked by lifecycle
log persistence.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Public constants ----------------------------------------------------------

EVENT_TYPES = (
    "ENTRY_DECISION",
    "PHASE1_EVAL",
    "PHASE2_EVAL",
    "PHASE3_CANDIDATE",
    "PHASE4_SENTINEL",
    "TITAN_GRIP_STAGE",
    "ORDER_SUBMIT",
    "ORDER_FILL",
    "ORDER_CANCEL",
    "EXIT_DECISION",
    "POSITION_CLOSED",
    "REASON",
)

DEFAULT_DATA_DIR = "/data/lifecycle"
_POSITION_ID_RE = re.compile(r"^[A-Z0-9._-]+_\d{8}T\d{6}Z_(long|short)$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    )


def _compact_ts(iso_ts: str) -> str:
    """Convert an ISO-8601 UTC timestamp into a compact ``YYYYMMDDTHHMMSSZ``
    token suitable for embedding in a position_id.
    """
    try:
        s = iso_ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def compose_position_id(ticker: str, entry_ts_utc: str, side: str) -> str:
    """Stable per-position identifier.

    Shape: ``<TICKER>_<YYYYMMDDTHHMMSSZ>_<long|short>``. This survives bot
    restarts because it is a deterministic function of the entry timestamp
    and ticker - both already persisted on the position dict.
    """
    t = re.sub(r"[^A-Z0-9._-]", "", (ticker or "").upper()) or "UNKNOWN"
    s = (side or "").lower()
    if s not in ("long", "short"):
        s = "long"
    return f"{t}_{_compact_ts(entry_ts_utc)}_{s}"


class LifecycleLogger:
    """Append-only per-position JSONL writer.

    Thread-safe via a per-position re-entrant lock so concurrent writes
    on different positions don't serialize through a single global lock.
    The file is opened/closed for each event - a JSONL append is a
    single ``write()`` so this is fast on local disk and avoids long
    open file handles across the trading thread.
    """

    def __init__(self, data_dir: str | None = None, bot_version: str = "") -> None:
        self._data_dir = Path(data_dir or os.getenv("LIFECYCLE_DIR") or DEFAULT_DATA_DIR)
        self._bot_version = str(bot_version or "")
        self._dir_lock = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        # Cached per-position metadata so the API can answer ``list``
        # without re-reading every file. Populated lazily via _scan_files
        # and refreshed on every open/log/close call.
        self._meta: dict[str, dict[str, Any]] = {}
        self._meta_lock = threading.Lock()
        self._seq_counter: dict[str, int] = {}

    # ----- internal helpers -----

    def _ensure_dir(self) -> bool:
        try:
            with self._dir_lock:
                self._data_dir.mkdir(parents=True, exist_ok=True)
            return True
        except Exception as e:  # disk failure, RO mount, etc.
            logger.warning("[lifecycle] cannot create %s: %s", self._data_dir, e)
            return False

    def _lock_for(self, position_id: str) -> threading.RLock:
        with self._locks_guard:
            lk = self._locks.get(position_id)
            if lk is None:
                lk = threading.RLock()
                self._locks[position_id] = lk
            return lk

    def _path_for(self, position_id: str) -> Path:
        return self._data_dir / f"{position_id}.jsonl"

    def _next_seq(self, position_id: str) -> int:
        cur = self._seq_counter.get(position_id, 0) + 1
        self._seq_counter[position_id] = cur
        return cur

    def _refresh_meta(
        self,
        position_id: str,
        event_type: str,
        ts: str,
        payload: dict,
        ticker: str | None = None,
        side: str | None = None,
        entry_ts_utc: str | None = None,
    ) -> None:
        with self._meta_lock:
            meta = self._meta.setdefault(
                position_id,
                {
                    "position_id": position_id,
                    "ticker": None,
                    "side": None,
                    "entry_ts_utc": None,
                    "status": "open",
                    "last_event_ts": None,
                    "latest_phase4_state": None,
                    "latest_titan_stage": None,
                    "event_count": 0,
                },
            )
            if ticker is not None:
                meta["ticker"] = ticker
            if side is not None:
                meta["side"] = side
            if entry_ts_utc is not None:
                meta["entry_ts_utc"] = entry_ts_utc
            meta["last_event_ts"] = ts
            meta["event_count"] = int(meta.get("event_count", 0)) + 1
            if event_type == "PHASE4_SENTINEL":
                meta["latest_phase4_state"] = payload.get("state") or payload.get("alarm_summary")
            elif event_type == "TITAN_GRIP_STAGE":
                meta["latest_titan_stage"] = payload.get("stage")
            elif event_type == "POSITION_CLOSED":
                meta["status"] = "closed"
                meta["realized_pnl"] = payload.get("realized_pnl")

    def _append_line(self, position_id: str, event: dict) -> bool:
        if not self._ensure_dir():
            return False
        path = self._path_for(position_id)
        try:
            with self._lock_for(position_id):
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, separators=(",", ":")))
                    fh.write("\n")
            return True
        except Exception as e:
            logger.warning("[lifecycle] append failed for %s: %s", position_id, e)
            return False

    # ----- public API -----

    def open_position(
        self,
        ticker: str,
        side: str,
        entry_ts_utc: str | None = None,
        payload: dict | None = None,
        reason_text: str | None = None,
    ) -> str:
        """Create the JSONL file (if not present) and write the
        ENTRY_DECISION event. Returns the position_id.

        Best-effort: if the underlying file write fails, the position_id
        is still returned so the caller can keep using it for further
        events - subsequent appends will retry.
        """
        try:
            ts = entry_ts_utc or _utc_now_iso()
            side_norm = "LONG" if str(side or "").upper().startswith("L") else "SHORT"
            position_id = compose_position_id(ticker, ts, side_norm)
            self._seq_counter.setdefault(position_id, 0)
            evt = {
                "position_id": position_id,
                "ticker": ticker,
                "side": side_norm,
                "entry_ts_utc": ts,
                "event_ts_utc": _utc_now_iso(),
                "event_seq": self._next_seq(position_id),
                "event_type": "ENTRY_DECISION",
                "payload": dict(payload or {}),
                "reason_text": reason_text,
                "bot_version": self._bot_version,
            }
            self._append_line(position_id, evt)
            self._refresh_meta(
                position_id,
                "ENTRY_DECISION",
                evt["event_ts_utc"],
                evt["payload"],
                ticker=ticker,
                side=side_norm,
                entry_ts_utc=ts,
            )
            return position_id
        except Exception as e:
            logger.warning("[lifecycle] open_position error: %s", e)
            try:
                return compose_position_id(ticker, entry_ts_utc or "", side or "long")
            except Exception:
                return ""

    def log_event(
        self,
        position_id: str,
        event_type: str,
        payload: dict | None = None,
        reason_text: str | None = None,
        ticker: str | None = None,
        side: str | None = None,
        entry_ts_utc: str | None = None,
    ) -> bool:
        """Append a single event. ``event_type`` should be one of
        :data:`EVENT_TYPES` but unknown values are accepted (logged with
        a debug warning). Returns True iff the file write succeeded.
        """
        if not position_id:
            return False
        if event_type not in EVENT_TYPES:
            logger.debug("[lifecycle] unknown event_type %r", event_type)
        try:
            evt = {
                "position_id": position_id,
                "ticker": ticker,
                "side": side,
                "entry_ts_utc": entry_ts_utc,
                "event_ts_utc": _utc_now_iso(),
                "event_seq": self._next_seq(position_id),
                "event_type": event_type,
                "payload": dict(payload or {}),
                "reason_text": reason_text,
                "bot_version": self._bot_version,
            }
            ok = self._append_line(position_id, evt)
            self._refresh_meta(
                position_id,
                event_type,
                evt["event_ts_utc"],
                evt["payload"],
                ticker=ticker,
                side=side,
                entry_ts_utc=entry_ts_utc,
            )
            return ok
        except Exception as e:
            logger.warning("[lifecycle] log_event %s/%s error: %s", position_id, event_type, e)
            return False

    def close_position(
        self,
        position_id: str,
        payload: dict | None = None,
        reason_text: str | None = None,
    ) -> bool:
        """Append the terminal POSITION_CLOSED event."""
        return self.log_event(position_id, "POSITION_CLOSED", payload, reason_text=reason_text)

    # ----- read API (used by dashboard) -----

    def list_positions(self, status: str = "all", limit: int = 20) -> list[dict]:
        """Return position metadata sorted by entry_ts_utc descending.

        ``status`` is one of ``open|closed|recent|all``. ``recent``
        returns the most-recently-active positions regardless of state.
        """
        self._scan_files()
        with self._meta_lock:
            metas = list(self._meta.values())
        if status == "open":
            metas = [m for m in metas if m.get("status") == "open"]
        elif status == "closed":
            metas = [m for m in metas if m.get("status") == "closed"]
        # ``recent`` and ``all`` keep everything; recent sorts by
        # last_event instead of entry timestamp.
        if status == "recent":
            metas.sort(key=lambda m: m.get("last_event_ts") or "", reverse=True)
        else:
            metas.sort(key=lambda m: m.get("entry_ts_utc") or "", reverse=True)
        try:
            limit_int = int(limit)
        except (TypeError, ValueError):
            limit_int = 20
        if limit_int < 1:
            limit_int = 1
        if limit_int > 500:
            limit_int = 500
        return metas[:limit_int]

    def read_events(self, position_id: str, since_seq: int = 0) -> list[dict]:
        """Read all events for a position. ``since_seq=N`` skips events
        with seq <= N (used for tail-follow polling).
        """
        if not position_id or not _POSITION_ID_RE.match(position_id):
            return []
        path = self._path_for(position_id)
        if not path.exists():
            return []
        out: list[dict] = []
        try:
            with self._lock_for(position_id):
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            evt = json.loads(line)
                        except Exception:
                            continue
                        if int(evt.get("event_seq") or 0) <= int(since_seq or 0):
                            continue
                        out.append(evt)
        except Exception as e:
            logger.warning("[lifecycle] read_events %s: %s", position_id, e)
            return []
        return out

    def _scan_files(self) -> None:
        """Walk the data dir and rebuild meta for any position_id we
        haven't seen yet. Cheap because only first/last lines are read.
        """
        try:
            if not self._data_dir.exists():
                return
            for path in self._data_dir.glob("*.jsonl"):
                position_id = path.stem
                with self._meta_lock:
                    if position_id in self._meta:
                        continue
                # Read first and last events to populate meta.
                first_evt: dict[str, Any] | None = None
                last_evt: dict[str, Any] | None = None
                last_phase4: Any = None
                last_titan: Any = None
                count = 0
                max_seq = 0
                try:
                    with path.open("r", encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                e = json.loads(line)
                            except Exception:
                                continue
                            count += 1
                            try:
                                seq = int(e.get("event_seq") or 0)
                                if seq > max_seq:
                                    max_seq = seq
                            except Exception:
                                pass
                            if first_evt is None:
                                first_evt = e
                            last_evt = e
                            if e.get("event_type") == "PHASE4_SENTINEL":
                                last_phase4 = (e.get("payload") or {}).get("state") or (
                                    e.get("payload") or {}
                                ).get("alarm_summary")
                            elif e.get("event_type") == "TITAN_GRIP_STAGE":
                                last_titan = (e.get("payload") or {}).get("stage")
                except Exception:
                    continue
                if first_evt is None:
                    continue
                status = "open"
                realized = None
                if last_evt and last_evt.get("event_type") == "POSITION_CLOSED":
                    status = "closed"
                    realized = (last_evt.get("payload") or {}).get("realized_pnl")
                meta = {
                    "position_id": position_id,
                    "ticker": first_evt.get("ticker"),
                    "side": first_evt.get("side"),
                    "entry_ts_utc": first_evt.get("entry_ts_utc"),
                    "status": status,
                    "last_event_ts": (last_evt or {}).get("event_ts_utc"),
                    "latest_phase4_state": last_phase4,
                    "latest_titan_stage": last_titan,
                    "event_count": count,
                    "realized_pnl": realized,
                }
                with self._meta_lock:
                    self._meta[position_id] = meta
                # Restore seq counter so subsequent appends don't collide.
                if max_seq > 0:
                    cur = self._seq_counter.get(position_id, 0)
                    if max_seq > cur:
                        self._seq_counter[position_id] = max_seq
        except Exception as e:
            logger.warning("[lifecycle] scan_files error: %s", e)


# Module-level default singleton --------------------------------------------

_default_logger: LifecycleLogger | None = None
_default_logger_lock = threading.Lock()


def get_default_logger(bot_version: str = "") -> LifecycleLogger:
    global _default_logger
    with _default_logger_lock:
        if _default_logger is None:
            _default_logger = LifecycleLogger(bot_version=bot_version)
        elif bot_version and not _default_logger._bot_version:
            _default_logger._bot_version = bot_version
        return _default_logger


def reset_default_logger_for_tests(
    data_dir: str | None = None, bot_version: str = ""
) -> LifecycleLogger:
    """Test hook - install a fresh logger pointed at a temp dir."""
    global _default_logger
    with _default_logger_lock:
        _default_logger = LifecycleLogger(data_dir=data_dir, bot_version=bot_version)
        return _default_logger


__all__ = [
    "EVENT_TYPES",
    "LifecycleLogger",
    "compose_position_id",
    "get_default_logger",
    "reset_default_logger_for_tests",
]
