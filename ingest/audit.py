"""ingest/audit.py \u2014 v6.6.0 Gap-fill and gate decision audit log.

Stores gap lifecycle events in /data/ingest_audit.db (Decision A2).
This DB is separate from state.db to avoid write contention on the hot
trading path.

Schema:
  ingest_gap_audit   \u2014 gap detection + backfill lifecycle per gap
  ingest_gate_decisions \u2014 per-evaluation gate allow/block records

Retention: 180 ET calendar days (Decision P4).
Thread safety: WAL mode + _audit_lock (threading.Lock) for multi-statement ops.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import ingest_config as _cfg

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB path
# ---------------------------------------------------------------------------

AUDIT_DB_PATH = os.environ.get("INGEST_AUDIT_DB_PATH", "/data/ingest_audit.db")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS ingest_gap_audit (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    date_et                 TEXT NOT NULL,
    ticker                  TEXT NOT NULL,
    gap_start_utc           TEXT NOT NULL,
    gap_end_utc             TEXT NOT NULL,
    gap_minutes             INTEGER NOT NULL,
    gap_detected_ts         TEXT NOT NULL,
    backfill_enqueued_ts    TEXT,
    backfill_completed_ts   TEXT,
    bars_written            INTEGER,
    verification_ts         TEXT,
    status                  TEXT NOT NULL DEFAULT 'open',
    UNIQUE (ticker, gap_start_utc)
);

CREATE INDEX IF NOT EXISTS idx_audit_date_ticker
    ON ingest_gap_audit (date_et, ticker);

CREATE TABLE IF NOT EXISTS ingest_gate_decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    decision_ts     TEXT NOT NULL,
    decision        TEXT NOT NULL,
    reason          TEXT NOT NULL,
    gate_mode       TEXT NOT NULL,
    overridden      INTEGER NOT NULL DEFAULT 0,
    override_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_gate_ticker_ts
    ON ingest_gate_decisions (ticker, decision_ts DESC);
"""

# ---------------------------------------------------------------------------
# Thread-local connections + module-level lock
# ---------------------------------------------------------------------------

_tls = threading.local()
_audit_lock = threading.Lock()
_db_initialized = False
_init_lock = threading.Lock()


def _ensure_db() -> None:
    """Initialize the audit DB (idempotent). Safe to call from any thread."""
    global _db_initialized
    if _db_initialized:
        return
    with _init_lock:
        if _db_initialized:
            return
        try:
            parent = os.path.dirname(AUDIT_DB_PATH)
            if parent:
                os.makedirs(parent, exist_ok=True)
            conn = sqlite3.connect(AUDIT_DB_PATH, timeout=30.0, isolation_level=None)
            conn.executescript(_SCHEMA_SQL)
            conn.close()
            _db_initialized = True
            logger.info("[AUDIT] ingest_audit.db initialized at %s", AUDIT_DB_PATH)
        except Exception as e:
            logger.warning("[AUDIT] DB init failed: %s", e)


def _conn() -> sqlite3.Connection:
    """Return a per-thread connection to AUDIT_DB_PATH."""
    _ensure_db()
    c = getattr(_tls, "conn", None)
    if c is None:
        try:
            c = sqlite3.connect(AUDIT_DB_PATH, timeout=30.0, isolation_level=None)
            c.row_factory = sqlite3.Row
            _tls.conn = c
        except Exception as e:
            logger.warning("[AUDIT] could not open audit DB: %s", e)
            raise
    return c


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class GapAuditEntry:
    """One detected gap and its backfill lifecycle."""
    id: Optional[int]
    date_et: str
    ticker: str
    gap_start_utc: str
    gap_end_utc: str
    gap_minutes: int
    gap_detected_ts: str
    backfill_enqueued_ts: Optional[str]
    backfill_completed_ts: Optional[str]
    bars_written: Optional[int]
    verification_ts: Optional[str]
    status: str   # "open" | "backfilling" | "closed" | "missing"


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _et_date_str() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ts_to_iso(ts: object) -> str:
    """Convert a datetime object or ISO string to UTC ISO string."""
    if isinstance(ts, str):
        return ts
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).isoformat()
    return str(ts)


def _gap_minutes_from_ts(start: object, end: object) -> int:
    """Compute gap duration in minutes from start/end timestamps."""
    try:
        s = start if isinstance(start, datetime) else datetime.fromisoformat(str(start))
        e = end if isinstance(end, datetime) else datetime.fromisoformat(str(end))
        if s.tzinfo is None:
            s = s.replace(tzinfo=timezone.utc)
        if e.tzinfo is None:
            e = e.replace(tzinfo=timezone.utc)
        return max(0, int((e - s).total_seconds() / 60))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# AuditLog (public API)
# ---------------------------------------------------------------------------

class AuditLog:
    """Static-method facade for audit DB operations.

    All methods are fail-safe: exceptions are logged and swallowed so that
    audit failures never propagate into the trading or ingest paths.
    """

    # -- Gap lifecycle --

    @staticmethod
    def record_gap_detected(
        ticker: str,
        gap_start: object,
        gap_end: object,
        now_utc: Optional[datetime] = None,
    ) -> None:
        """Insert a new gap row with status='open'. ON CONFLICT IGNORE (dedup)."""
        try:
            now = _ts_to_iso(now_utc) if now_utc else _utc_now_iso()
            start_iso = _ts_to_iso(gap_start)
            end_iso = _ts_to_iso(gap_end)
            minutes = _gap_minutes_from_ts(gap_start, gap_end)
            date_et = _et_date_str()
            _conn().execute(
                """
                INSERT OR IGNORE INTO ingest_gap_audit
                    (date_et, ticker, gap_start_utc, gap_end_utc,
                     gap_minutes, gap_detected_ts, status)
                VALUES (?, ?, ?, ?, ?, ?, 'open')
                """,
                (date_et, ticker, start_iso, end_iso, minutes, now),
            )
        except Exception as e:
            logger.warning("[AUDIT] record_gap_detected failed for %s: %s", ticker, e)

    @staticmethod
    def record_gap_enqueued(
        ticker: str,
        gap_start: object,
        now_utc: Optional[datetime] = None,
    ) -> None:
        """Update gap row to status='backfilling'."""
        try:
            now = _ts_to_iso(now_utc) if now_utc else _utc_now_iso()
            start_iso = _ts_to_iso(gap_start)
            _conn().execute(
                """
                UPDATE ingest_gap_audit
                SET backfill_enqueued_ts = ?,
                    status = 'backfilling'
                WHERE ticker = ? AND gap_start_utc = ?
                """,
                (now, ticker, start_iso),
            )
        except Exception as e:
            logger.warning("[AUDIT] record_gap_enqueued failed for %s: %s", ticker, e)

    @staticmethod
    def record_backfill_completed(
        ticker: str,
        gap_start: object,
        gap_end: object,
        bars_written: int,
        now_utc: Optional[datetime] = None,
    ) -> None:
        """Update gap row with bars_written and backfill_completed_ts."""
        try:
            now = _ts_to_iso(now_utc) if now_utc else _utc_now_iso()
            start_iso = _ts_to_iso(gap_start)
            _conn().execute(
                """
                UPDATE ingest_gap_audit
                SET backfill_completed_ts = ?,
                    bars_written = ?
                WHERE ticker = ? AND gap_start_utc = ?
                """,
                (now, bars_written, ticker, start_iso),
            )
        except Exception as e:
            logger.warning(
                "[AUDIT] record_backfill_completed failed for %s: %s", ticker, e
            )

    @staticmethod
    def record_verification(
        ticker: str,
        gap_start: object,
        status: str,
        now_utc: Optional[datetime] = None,
    ) -> None:
        """Update gap row with verification outcome ('closed' or 'missing')."""
        try:
            now = _ts_to_iso(now_utc) if now_utc else _utc_now_iso()
            start_iso = _ts_to_iso(gap_start)
            _conn().execute(
                """
                UPDATE ingest_gap_audit
                SET verification_ts = ?,
                    status = ?
                WHERE ticker = ? AND gap_start_utc = ?
                """,
                (now, status, ticker, start_iso),
            )
        except Exception as e:
            logger.warning("[AUDIT] record_verification failed for %s: %s", ticker, e)

    # -- Gate decisions --

    @staticmethod
    def record_gate_decision(
        ticker: str,
        decision: str,
        reason: str,
        gate_mode: str,
        overridden: bool,
        override_reason: str = "",
        now_utc: Optional[datetime] = None,
    ) -> None:
        """Append a gate decision row. Single-statement INSERT (no extra lock needed)."""
        try:
            now = _ts_to_iso(now_utc) if now_utc else _utc_now_iso()
            _conn().execute(
                """
                INSERT INTO ingest_gate_decisions
                    (ticker, decision_ts, decision, reason,
                     gate_mode, overridden, override_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker, now, decision, reason,
                    gate_mode, int(overridden), override_reason,
                ),
            )
        except Exception as e:
            logger.warning("[AUDIT] record_gate_decision failed for %s: %s", ticker, e)

    # -- Reads --

    @staticmethod
    def last_24h() -> list:
        """Return gap audit rows from the last 24 h. Used by /api/state."""
        try:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat()
            rows = _conn().execute(
                """
                SELECT * FROM ingest_gap_audit
                WHERE gap_detected_ts >= ?
                ORDER BY gap_detected_ts DESC
                """,
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("[AUDIT] last_24h query failed: %s", e)
            return []

    @staticmethod
    def daily_summary(date_et: Optional[str] = None) -> dict:
        """Return a summary dict for a given ET date (defaults to today)."""
        date_et = date_et or _et_date_str()
        try:
            rows = _conn().execute(
                """
                SELECT status, COUNT(*) as cnt
                FROM ingest_gap_audit
                WHERE date_et = ?
                GROUP BY status
                """,
                (date_et,),
            ).fetchall()
            counts: dict = {}
            for r in rows:
                counts[r["status"]] = r["cnt"]
            detected = sum(counts.values())
            closed = counts.get("closed", 0)
            partial = counts.get("partial", 0)
            missing = counts.get("missing", 0)
            open_count = counts.get("open", 0)
            backfilling = counts.get("backfilling", 0)
            return {
                "date": date_et,
                "gaps_detected": detected,
                "gaps_closed": closed,
                "gaps_partial": partial,
                "gaps_missing": missing,
                "gaps_open": open_count,
                "gaps_backfilling": backfilling,
                "fill_rate": round(closed / detected, 4) if detected else 0.0,
            }
        except Exception as e:
            logger.warning("[AUDIT] daily_summary failed: %s", e)
            return {"date": date_et, "error": str(e)}

    # -- Retention prune (Decision P4: 180 days) --

    @staticmethod
    def prune_old_rows() -> None:
        """Delete rows older than AUDIT_RETENTION_DAYS. Run once per day."""
        try:
            cutoff = (
                datetime.now(timezone.utc)
                - timedelta(days=_cfg.AUDIT_RETENTION_DAYS)
            ).isoformat()
            with _audit_lock:
                _conn().execute(
                    "DELETE FROM ingest_gap_audit WHERE gap_detected_ts < ?",
                    (cutoff,),
                )
                _conn().execute(
                    "DELETE FROM ingest_gate_decisions WHERE decision_ts < ?",
                    (cutoff,),
                )
            logger.info("[AUDIT] retention prune complete (cutoff=%s)", cutoff)
        except Exception as e:
            logger.warning("[AUDIT] prune failed: %s", e)
