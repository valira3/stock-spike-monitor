"""SQLite persistence for cross-restart idempotency state.

v5.1.8 \u2014 replaces two pieces of in-memory / non-atomic state with a
durable SQLite store on the Railway volume:

  1. fired_set \u2014 timed-job idempotency keys used by scheduler_thread()
     in trade_genius.py. Previously a process-local Python set; on a
     container restart at e.g. 15:59:30 ET, a 16:00 job could either
     double-fire (paper_state.json snapshot didn't include it) or be
     skipped silently. Now persisted in the fired_set table.

  2. v5_long_tracks / v5_short_tracks \u2014 Tiger/Buffalo state-machine
     tracks. Previously serialized via json.dump inside the larger
     paper_state.json file; a crash mid-write could corrupt the whole
     state file (positions + cash + trade history along with the
     tracks). Now each ticker's track is its own row in v5_long_tracks
     (table is direction-agnostic; the ticker key is namespaced as
     "long:TICKER" or "short:TICKER" to keep a single table per spec).

DB path: configurable via STATE_DB_PATH (default /data/state.db).
Concurrency: WAL mode so dashboard reads do not block writer.
Atomicity: every write is wrapped in BEGIN IMMEDIATE / COMMIT.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
STATE_DB_PATH = os.getenv("STATE_DB_PATH", "/data/state.db")

_LONG_PREFIX = "long:"
_SHORT_PREFIX = "short:"

# Connections are not threadsafe across threads in stdlib sqlite3 by
# default. We hand out per-thread connections via thread-local storage
# and protect schema init / migration with a module-level lock.
_init_lock = threading.Lock()
_initialized = False
_tls = threading.local()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(path: str) -> sqlite3.Connection:
    parent = os.path.dirname(path) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as e:
        logger.warning("persistence: could not mkdir %s: %s", parent, e)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _conn() -> sqlite3.Connection:
    """Return a per-thread connection to STATE_DB_PATH."""
    init_db()
    c = getattr(_tls, "conn", None)
    if c is None:
        c = _connect(STATE_DB_PATH)
        _tls.conn = c
    return c


def init_db(path: Optional[str] = None) -> None:
    """Create tables + run JSON->SQLite migration. Idempotent."""
    global _initialized, STATE_DB_PATH
    if path is not None:
        STATE_DB_PATH = path
        _initialized = False
        if hasattr(_tls, "conn"):
            try:
                _tls.conn.close()
            except Exception:
                pass
            del _tls.conn
    with _init_lock:
        if _initialized:
            return
        bootstrap = _connect(STATE_DB_PATH)
        try:
            bootstrap.execute(
                """
                CREATE TABLE IF NOT EXISTS fired_set (
                    job_key TEXT PRIMARY KEY,
                    fired_at_utc TEXT NOT NULL
                )
                """
            )
            bootstrap.execute(
                """
                CREATE TABLE IF NOT EXISTS v5_long_tracks (
                    ticker TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                )
                """
            )
        finally:
            bootstrap.close()
        _initialized = True


# ----------------------------------------------------------------------
# fired_set helpers
# ----------------------------------------------------------------------
def mark_fired(job_key: str) -> None:
    """Record that a timed job with job_key has fired. Idempotent."""
    c = _conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            "INSERT OR IGNORE INTO fired_set (job_key, fired_at_utc) "
            "VALUES (?, ?)",
            (job_key, _utc_now_iso()),
        )
        c.execute("COMMIT")
    except Exception:
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        raise


def was_fired(job_key: str) -> bool:
    c = _conn()
    cur = c.execute(
        "SELECT 1 FROM fired_set WHERE job_key = ? LIMIT 1", (job_key,)
    )
    return cur.fetchone() is not None


def prune_fired(keep_prefix: str) -> int:
    """Delete fired_set rows whose job_key does NOT start with keep_prefix.

    Used to roll old day's idempotency keys off so the table doesn't
    grow without bound. Returns rows deleted.
    """
    c = _conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        cur = c.execute(
            "DELETE FROM fired_set WHERE job_key NOT LIKE ?",
            (keep_prefix + "%",),
        )
        deleted = cur.rowcount or 0
        c.execute("COMMIT")
        return deleted
    except Exception:
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        raise


# ----------------------------------------------------------------------
# v5_long_tracks helpers
#
# The table is direction-agnostic; callers pass direction="long" or
# "short" so a single table satisfies the spec while still letting the
# scanner keep two distinct buckets in memory.
# ----------------------------------------------------------------------
def _row_key(direction: str, ticker: str) -> str:
    if direction == "long":
        return _LONG_PREFIX + ticker
    if direction == "short":
        return _SHORT_PREFIX + ticker
    raise ValueError("direction must be 'long' or 'short'")


def save_track(ticker: str, state_dict: dict, direction: str = "long") -> None:
    payload = json.dumps(state_dict, default=str, separators=(",", ":"))
    key = _row_key(direction, ticker)
    c = _conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            "INSERT INTO v5_long_tracks (ticker, state_json, updated_at_utc) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(ticker) DO UPDATE SET "
            "  state_json = excluded.state_json, "
            "  updated_at_utc = excluded.updated_at_utc",
            (key, payload, _utc_now_iso()),
        )
        c.execute("COMMIT")
    except Exception:
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        raise


def load_track(ticker: str, direction: str = "long") -> Optional[dict]:
    key = _row_key(direction, ticker)
    c = _conn()
    cur = c.execute(
        "SELECT state_json FROM v5_long_tracks WHERE ticker = ? LIMIT 1",
        (key,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except Exception as e:
        logger.warning("load_track: bad JSON for %s: %s", key, e)
        return None


def delete_track(ticker: str, direction: str = "long") -> None:
    key = _row_key(direction, ticker)
    c = _conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM v5_long_tracks WHERE ticker = ?", (key,))
        c.execute("COMMIT")
    except Exception:
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        raise


def load_all_tracks(direction: str = "long") -> dict[str, dict]:
    """Return {ticker: state_dict} for every row matching direction."""
    if direction not in ("long", "short"):
        raise ValueError("direction must be 'long' or 'short'")
    prefix = _LONG_PREFIX if direction == "long" else _SHORT_PREFIX
    c = _conn()
    cur = c.execute(
        "SELECT ticker, state_json FROM v5_long_tracks WHERE ticker LIKE ?",
        (prefix + "%",),
    )
    out: dict[str, dict] = {}
    for key, payload in cur.fetchall():
        ticker = key[len(prefix):]
        try:
            out[ticker] = json.loads(payload)
        except Exception as e:
            logger.warning("load_all_tracks: bad JSON for %s: %s", key, e)
    return out


def replace_all_tracks(
    long_tracks: dict[str, dict],
    short_tracks: Optional[dict[str, dict]] = None,
) -> None:
    """Atomically wipe and rewrite every track row.

    Called from save_paper_state to keep the SQLite copy in sync with
    the in-memory dict (handles deletions that save_track alone would
    miss). Both buckets in one transaction so a crash mid-rewrite
    rolls back to the previous consistent snapshot.
    """
    short_tracks = short_tracks or {}
    c = _conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM v5_long_tracks")
        now = _utc_now_iso()
        for ticker, st in long_tracks.items():
            c.execute(
                "INSERT INTO v5_long_tracks "
                "(ticker, state_json, updated_at_utc) VALUES (?, ?, ?)",
                (
                    _LONG_PREFIX + ticker,
                    json.dumps(st, default=str, separators=(",", ":")),
                    now,
                ),
            )
        for ticker, st in short_tracks.items():
            c.execute(
                "INSERT INTO v5_long_tracks "
                "(ticker, state_json, updated_at_utc) VALUES (?, ?, ?)",
                (
                    _SHORT_PREFIX + ticker,
                    json.dumps(st, default=str, separators=(",", ":")),
                    now,
                ),
            )
        c.execute("COMMIT")
    except Exception:
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        raise


# ----------------------------------------------------------------------
# JSON -> SQLite migration
# ----------------------------------------------------------------------
def migrate_from_json(json_path: str) -> int:
    """One-shot import of v5 tracks from an existing paper_state.json.

    Returns the number of track rows imported (long + short). Renames
    the source file to <path>.migrated.bak so a subsequent boot does
    not re-apply it. Idempotent: if the .bak already exists or the
    source is missing, this is a no-op.
    """
    if not json_path or not os.path.exists(json_path):
        return 0
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            blob = json.load(f)
    except Exception as e:
        logger.warning("migrate_from_json: could not read %s: %s", json_path, e)
        return 0
    long_raw = blob.get("v5_long_tracks") or {}
    short_raw = blob.get("v5_short_tracks") or {}
    if not long_raw and not short_raw:
        return 0
    imported = 0
    c = _conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        now = _utc_now_iso()
        for ticker, st in long_raw.items():
            c.execute(
                "INSERT OR IGNORE INTO v5_long_tracks "
                "(ticker, state_json, updated_at_utc) VALUES (?, ?, ?)",
                (
                    _LONG_PREFIX + ticker,
                    json.dumps(st, default=str, separators=(",", ":")),
                    now,
                ),
            )
            imported += 1
        for ticker, st in short_raw.items():
            c.execute(
                "INSERT OR IGNORE INTO v5_long_tracks "
                "(ticker, state_json, updated_at_utc) VALUES (?, ?, ?)",
                (
                    _SHORT_PREFIX + ticker,
                    json.dumps(st, default=str, separators=(",", ":")),
                    now,
                ),
            )
            imported += 1
        c.execute("COMMIT")
    except Exception:
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        raise
    bak = json_path + ".migrated.bak"
    try:
        if not os.path.exists(bak):
            os.rename(json_path, bak)
            logger.info(
                "persistence: migrated %d v5 tracks from %s -> SQLite, "
                "renamed source to %s",
                imported, json_path, bak,
            )
    except OSError as e:
        logger.warning(
            "persistence: imported %d tracks but could not rename %s: %s",
            imported, json_path, e,
        )
    return imported


def _close_for_tests() -> None:
    """Test-only helper: drop the per-thread connection + init flag."""
    global _initialized
    c = getattr(_tls, "conn", None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
        del _tls.conn
    _initialized = False
