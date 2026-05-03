"""tests/test_gap_audit.py - Unit tests for ingest/audit.py AuditLog lifecycle.

Tests gap detection, enqueue, backfill completion, verification, dedup,
last_24h query, and 180-day retention prune.

No em-dashes in this file per team rules.
"""
import os
import sys
import tempfile
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture(autouse=True)
def temp_audit_db(monkeypatch, tmp_path):
    """Redirect AuditLog to a temporary DB for each test."""
    db_path = str(tmp_path / "test_ingest_audit.db")
    monkeypatch.setenv("INGEST_AUDIT_DB_PATH", db_path)
    # Force re-import to pick up new path
    import importlib
    import ingest.audit as audit_mod
    importlib.reload(audit_mod)
    audit_mod._db_initialized = False
    # Wipe thread-local connection
    audit_mod._tls.__dict__.clear()
    yield audit_mod
    audit_mod._tls.__dict__.clear()


class TestGapDetection:
    def test_record_gap_detected_inserts_open_row(self, temp_audit_db):
        al = temp_audit_db.AuditLog
        t0 = datetime(2026, 5, 4, 14, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 5, 4, 14, 5, 0, tzinfo=timezone.utc)
        al.record_gap_detected("AAPL", t0, t1)
        rows = temp_audit_db.AuditLog.last_24h()
        assert len(rows) == 1
        assert rows[0]["status"] == "open"
        assert rows[0]["ticker"] == "AAPL"

    def test_record_gap_enqueued_updates_status(self, temp_audit_db):
        al = temp_audit_db.AuditLog
        t0 = datetime(2026, 5, 4, 14, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 5, 4, 14, 5, 0, tzinfo=timezone.utc)
        al.record_gap_detected("AAPL", t0, t1)
        al.record_gap_enqueued("AAPL", t0)
        rows = al.last_24h()
        assert rows[0]["status"] == "backfilling"
        assert rows[0]["backfill_enqueued_ts"] is not None

    def test_record_backfill_completed_updates_bars_written(self, temp_audit_db):
        al = temp_audit_db.AuditLog
        t0 = datetime(2026, 5, 4, 14, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 5, 4, 14, 5, 0, tzinfo=timezone.utc)
        al.record_gap_detected("AAPL", t0, t1)
        al.record_gap_enqueued("AAPL", t0)
        al.record_backfill_completed("AAPL", t0, t1, bars_written=5)
        rows = al.last_24h()
        assert rows[0]["bars_written"] == 5
        assert rows[0]["backfill_completed_ts"] is not None

    def test_record_verification_closed(self, temp_audit_db):
        al = temp_audit_db.AuditLog
        t0 = datetime(2026, 5, 4, 14, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 5, 4, 14, 5, 0, tzinfo=timezone.utc)
        al.record_gap_detected("AAPL", t0, t1)
        al.record_verification("AAPL", t0, "closed")
        rows = al.last_24h()
        assert rows[0]["status"] == "closed"
        assert rows[0]["verification_ts"] is not None

    def test_record_verification_missing(self, temp_audit_db):
        al = temp_audit_db.AuditLog
        t0 = datetime(2026, 5, 4, 14, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 5, 4, 14, 5, 0, tzinfo=timezone.utc)
        al.record_gap_detected("AAPL", t0, t1)
        al.record_verification("AAPL", t0, "missing")
        rows = al.last_24h()
        assert rows[0]["status"] == "missing"


class TestDeduplication:
    def test_duplicate_insert_does_not_duplicate(self, temp_audit_db):
        al = temp_audit_db.AuditLog
        t0 = datetime(2026, 5, 4, 14, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 5, 4, 14, 5, 0, tzinfo=timezone.utc)
        al.record_gap_detected("AAPL", t0, t1)
        al.record_gap_detected("AAPL", t0, t1)  # duplicate
        rows = al.last_24h()
        assert len(rows) == 1


class TestLast24h:
    def test_returns_only_last_24h_rows(self, temp_audit_db):
        al = temp_audit_db.AuditLog
        now = datetime.now(timezone.utc)
        old = now - timedelta(hours=25)
        t_end = old + timedelta(minutes=5)
        # Insert old row directly via SQL
        conn = sqlite3.connect(os.environ["INGEST_AUDIT_DB_PATH"])
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(temp_audit_db._SCHEMA_SQL)
        conn.execute(
            """
            INSERT OR IGNORE INTO ingest_gap_audit
                (date_et, ticker, gap_start_utc, gap_end_utc, gap_minutes,
                 gap_detected_ts, status)
            VALUES (?, ?, ?, ?, ?, ?, 'open')
            """,
            ("2026-05-03", "AAPL", old.isoformat(), t_end.isoformat(), 5, old.isoformat()),
        )
        conn.commit()
        conn.close()
        rows = al.last_24h()
        # Old row should NOT appear
        for r in rows:
            assert r["ticker"] != "AAPL" or r["gap_detected_ts"] > (now - timedelta(hours=24)).isoformat()


class TestRetentionPrune:
    def test_prune_deletes_old_rows(self, temp_audit_db, monkeypatch):
        al = temp_audit_db.AuditLog
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=200)
        old_end = old + timedelta(minutes=5)
        # Insert old row directly
        conn = sqlite3.connect(os.environ["INGEST_AUDIT_DB_PATH"])
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(temp_audit_db._SCHEMA_SQL)
        conn.execute(
            """
            INSERT OR IGNORE INTO ingest_gap_audit
                (date_et, ticker, gap_start_utc, gap_end_utc, gap_minutes,
                 gap_detected_ts, status)
            VALUES (?, ?, ?, ?, ?, ?, 'open')
            """,
            ("2025-10-15", "AAPL", old.isoformat(), old_end.isoformat(), 5, old.isoformat()),
        )
        conn.commit()
        conn.close()
        # Run prune with 180-day retention
        import ingest_config
        monkeypatch.setattr(ingest_config, "AUDIT_RETENTION_DAYS", 180)
        al.prune_old_rows()
        # Reload connection
        temp_audit_db._tls.__dict__.clear()
        conn2 = sqlite3.connect(os.environ["INGEST_AUDIT_DB_PATH"])
        count = conn2.execute("SELECT COUNT(*) FROM ingest_gap_audit").fetchone()[0]
        conn2.close()
        assert count == 0


class TestDailySummary:
    def test_daily_summary_counts(self, temp_audit_db):
        al = temp_audit_db.AuditLog
        t0 = datetime(2026, 5, 4, 14, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 5, 4, 14, 5, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 5, 4, 14, 10, 0, tzinfo=timezone.utc)
        t3 = datetime(2026, 5, 4, 14, 15, 0, tzinfo=timezone.utc)
        # Gap 1: closed
        al.record_gap_detected("AAPL", t0, t1)
        al.record_verification("AAPL", t0, "closed")
        # Gap 2: missing
        al.record_gap_detected("AAPL", t2, t3)
        al.record_verification("AAPL", t2, "missing")
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        today = _dt.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        summary = al.daily_summary(today)
        assert summary["gaps_detected"] == 2
        assert summary["gaps_closed"] == 1
        assert summary["gaps_missing"] == 1
