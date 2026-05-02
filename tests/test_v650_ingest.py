"""tests/test_v650_ingest.py

v6.5.0 ingest module test suite.

Covers:
  1. Credential resolution chain: VAL -> GENE -> (None, None)
  2. BAR_SCHEMA_FIELDS includes feed_source (M-4)
  3. GapDetector math (gap detection returns correct spans)
  4. ConnectionHealth state transitions
  5. [INGEST SHADOW DISABLED] log emitted when no creds present

No em-dashes anywhere in this file.
"""

import importlib
import logging
import os
import sys
import tempfile
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

# Ensure repo root is on the path so imports resolve without install
_REPO_ROOT = str(Path(__file__).parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------

import ingest.algo_plus as ap


# ---------------------------------------------------------------------------
# 1. Credential resolution chain
# ---------------------------------------------------------------------------

class TestResolveAlpacaCreds:
    """_resolve_alpaca_creds must prefer VAL then GENE then return (None, None)."""

    def test_val_key_used_when_set(self, monkeypatch, caplog):
        monkeypatch.setenv("VAL_ALPACA_PAPER_KEY", "val-key")
        monkeypatch.setenv("VAL_ALPACA_PAPER_SECRET", "val-secret")
        monkeypatch.delenv("GENE_ALPACA_PAPER_KEY", raising=False)
        monkeypatch.delenv("GENE_ALPACA_PAPER_SECRET", raising=False)
        key, secret = ap._resolve_alpaca_creds()
        assert key == "val-key"
        assert secret == "val-secret"

    def test_gene_key_used_when_val_absent(self, monkeypatch):
        monkeypatch.delenv("VAL_ALPACA_PAPER_KEY", raising=False)
        monkeypatch.delenv("VAL_ALPACA_PAPER_SECRET", raising=False)
        monkeypatch.setenv("GENE_ALPACA_PAPER_KEY", "gene-key")
        monkeypatch.setenv("GENE_ALPACA_PAPER_SECRET", "gene-secret")
        key, secret = ap._resolve_alpaca_creds()
        assert key == "gene-key"
        assert secret == "gene-secret"

    def test_val_preferred_over_gene_when_both_set(self, monkeypatch):
        monkeypatch.setenv("VAL_ALPACA_PAPER_KEY", "val-key")
        monkeypatch.setenv("VAL_ALPACA_PAPER_SECRET", "val-secret")
        monkeypatch.setenv("GENE_ALPACA_PAPER_KEY", "gene-key")
        monkeypatch.setenv("GENE_ALPACA_PAPER_SECRET", "gene-secret")
        key, secret = ap._resolve_alpaca_creds()
        assert key == "val-key", "VAL must take priority over GENE"

    def test_none_returned_when_no_creds(self, monkeypatch):
        monkeypatch.delenv("VAL_ALPACA_PAPER_KEY", raising=False)
        monkeypatch.delenv("VAL_ALPACA_PAPER_SECRET", raising=False)
        monkeypatch.delenv("GENE_ALPACA_PAPER_KEY", raising=False)
        monkeypatch.delenv("GENE_ALPACA_PAPER_SECRET", raising=False)
        key, secret = ap._resolve_alpaca_creds()
        assert key is None
        assert secret is None

    def test_shadow_disabled_logged_on_no_creds(self, monkeypatch, caplog):
        monkeypatch.delenv("VAL_ALPACA_PAPER_KEY", raising=False)
        monkeypatch.delenv("VAL_ALPACA_PAPER_SECRET", raising=False)
        monkeypatch.delenv("GENE_ALPACA_PAPER_KEY", raising=False)
        monkeypatch.delenv("GENE_ALPACA_PAPER_SECRET", raising=False)
        with caplog.at_level(logging.WARNING, logger="ingest.algo_plus"):
            ap._resolve_alpaca_creds()
        assert any(
            "INGEST SHADOW DISABLED" in rec.message
            for rec in caplog.records
        ), "Expected [INGEST SHADOW DISABLED] warning when no creds"


# ---------------------------------------------------------------------------
# 2. BAR_SCHEMA_FIELDS includes feed_source (M-4)
# ---------------------------------------------------------------------------

class TestBarSchemaFields:
    def test_feed_source_in_bar_schema_fields(self):
        import bar_archive
        assert "feed_source" in bar_archive.BAR_SCHEMA_FIELDS, (
            "M-4: feed_source must be in BAR_SCHEMA_FIELDS"
        )

    def test_bar_assembler_sets_feed_source_sip(self, tmp_path):
        written_bars = []

        def mock_write_bar(ticker, bar):
            written_bars.append((ticker, dict(bar)))

        assembler = ap.BarAssembler()
        with mock.patch("bar_archive.write_bar", side_effect=mock_write_bar):
            ok = assembler.accept("NVDA", {
                "ts": "2026-04-28T14:00:00+00:00",
                "open": 800.0,
                "high": 805.0,
                "low": 799.0,
                "close": 803.0,
            })
        assert ok is True
        assert written_bars, "write_bar should have been called"
        ticker, bar = written_bars[0]
        assert ticker == "NVDA"
        assert bar.get("feed_source") == "sip", (
            "BarAssembler must tag feed_source='sip'"
        )


# ---------------------------------------------------------------------------
# 3. GapDetector math
# ---------------------------------------------------------------------------

class TestGapDetector:
    def _make_ts_set(self, session_start: datetime, minutes: list) -> set:
        """Build a set of ts strings for the given minute offsets from session_start."""
        ts_set = set()
        for m in minutes:
            ts = session_start + timedelta(minutes=m)
            ts_set.add(ts.isoformat())
            ts_set.add(ts.strftime("%Y-%m-%dT%H:%M:%SZ"))
        return ts_set

    def test_no_gap_when_all_bars_present(self, tmp_path, monkeypatch):
        detector = ap.GapDetector()
        session_start = datetime(2026, 4, 28, 4, 0, 0, tzinfo=timezone.utc)
        now = session_start + timedelta(minutes=10)
        # Write bars for every minute 0-9
        ts_set = self._make_ts_set(session_start, list(range(10)))
        with mock.patch("ingest.algo_plus._read_ts_set", return_value=ts_set):
            gaps = detector.detect_gaps("NVDA", session_start, now)
        assert gaps == [], f"Expected no gaps but got: {gaps}"

    def test_gap_of_3_detected(self, tmp_path, monkeypatch):
        detector = ap.GapDetector()
        session_start = datetime(2026, 4, 28, 4, 0, 0, tzinfo=timezone.utc)
        now = session_start + timedelta(minutes=15)
        # Bars present at minutes 0, 1, 2, then missing 3,4,5, then present 6-14
        present = list(range(0, 3)) + list(range(6, 15))
        ts_set = self._make_ts_set(session_start, present)
        with mock.patch("ingest.algo_plus._read_ts_set", return_value=ts_set):
            gaps = detector.detect_gaps("NVDA", session_start, now)
        assert len(gaps) == 1, f"Expected 1 gap, got {len(gaps)}: {gaps}"
        gap_start, gap_end = gaps[0]
        expected_start = session_start + timedelta(minutes=3)
        expected_end = session_start + timedelta(minutes=6)
        assert gap_start == expected_start, f"gap_start mismatch: {gap_start}"
        assert gap_end == expected_end, f"gap_end mismatch: {gap_end}"

    def test_gap_below_threshold_not_reported(self, monkeypatch):
        detector = ap.GapDetector()
        session_start = datetime(2026, 4, 28, 9, 30, 0, tzinfo=timezone.utc)
        now = session_start + timedelta(minutes=10)
        # Only 2 consecutive minutes missing (below threshold of 3)
        present = list(range(0, 4)) + list(range(6, 10))
        ts_set = self._make_ts_set(session_start, present)
        with mock.patch("ingest.algo_plus._read_ts_set", return_value=ts_set):
            gaps = detector.detect_gaps("NVDA", session_start, now)
        assert gaps == [], f"Gap below threshold should not be reported: {gaps}"

    def test_gap_threshold_is_3(self):
        assert ap.GAP_THRESHOLD_MINUTES == 3, (
            "GAP_THRESHOLD_MINUTES must be 3 per spec section 4.3"
        )

    def test_multiple_gaps_detected(self, monkeypatch):
        detector = ap.GapDetector()
        session_start = datetime(2026, 4, 28, 9, 30, 0, tzinfo=timezone.utc)
        now = session_start + timedelta(minutes=25)
        # Present: 0-4, gap 5-8 (4 min), present 9-14, gap 15-19 (5 min), present 20-24
        present = list(range(0, 5)) + list(range(9, 15)) + list(range(20, 25))
        ts_set = self._make_ts_set(session_start, present)
        with mock.patch("ingest.algo_plus._read_ts_set", return_value=ts_set):
            gaps = detector.detect_gaps("NVDA", session_start, now)
        assert len(gaps) == 2, f"Expected 2 gaps, got {len(gaps)}: {gaps}"


# ---------------------------------------------------------------------------
# 4. ConnectionHealth state transitions
# ---------------------------------------------------------------------------

class TestConnectionHealth:
    def setup_method(self):
        self.health = ap.ConnectionHealth()

    def test_initial_state_is_connecting(self):
        assert self.health.get() == ap.CONNECTING

    def test_set_live(self):
        self.health.set(ap.LIVE)
        assert self.health.get() == ap.LIVE

    def test_set_degraded(self):
        self.health.set(ap.DEGRADED)
        assert self.health.get() == ap.DEGRADED

    def test_set_reconnecting(self):
        self.health.set(ap.RECONNECTING)
        assert self.health.get() == ap.RECONNECTING

    def test_set_rest_only(self):
        self.health.set(ap.REST_ONLY)
        assert self.health.get() == ap.REST_ONLY

    def test_all_valid_states(self):
        for state in [ap.CONNECTING, ap.LIVE, ap.DEGRADED, ap.RECONNECTING, ap.REST_ONLY]:
            self.health.set(state)
            assert self.health.get() == state

    def test_invalid_state_raises(self):
        with pytest.raises(ValueError):
            self.health.set("INVALID_STATE")

    def test_thread_safe_set_get(self):
        import threading
        results = []
        errors = []

        def worker(state):
            try:
                self.health.set(state)
                results.append(self.health.get())
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=worker, args=(ap.LIVE,)),
            threading.Thread(target=worker, args=(ap.DEGRADED,)),
            threading.Thread(target=worker, args=(ap.RECONNECTING,)),
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert not errors, f"Thread errors: {errors}"
        assert all(r in ap._VALID_STATES for r in results)

    def test_last_bar_age_none_before_any_bar(self):
        assert self.health.last_bar_age_s() is None

    def test_last_bar_age_updates_after_record(self):
        self.health.record_bar()
        age = self.health.last_bar_age_s()
        assert age is not None
        assert age >= 0.0
        assert age < 5.0  # should be nearly instant


# ---------------------------------------------------------------------------
# 5. [INGEST SHADOW DISABLED] log on no creds
# ---------------------------------------------------------------------------

class TestIngestShadowDisabled:
    def test_shadow_disabled_log_present_in_source(self):
        src_path = Path(__file__).parent.parent / "ingest" / "algo_plus.py"
        src = src_path.read_text(encoding="utf-8")
        assert "INGEST SHADOW DISABLED" in src, (
            "Source must contain [INGEST SHADOW DISABLED] log string"
        )

    def test_ingest_loop_returns_early_when_no_creds(self, monkeypatch):
        monkeypatch.delenv("VAL_ALPACA_PAPER_KEY", raising=False)
        monkeypatch.delenv("VAL_ALPACA_PAPER_SECRET", raising=False)
        monkeypatch.delenv("GENE_ALPACA_PAPER_KEY", raising=False)
        monkeypatch.delenv("GENE_ALPACA_PAPER_SECRET", raising=False)

        started = []

        with mock.patch.object(ap.AlgoPlusIngest, "start", side_effect=lambda: started.append(1)):
            # ingest_loop should return without calling AlgoPlusIngest.start
            # because _resolve_alpaca_creds returns (None, None)
            ap.ingest_loop()

        assert not started, (
            "AlgoPlusIngest.start must NOT be called when no creds are present"
        )

    def test_shadow_data_status_unconfigured_on_no_creds(self, monkeypatch):
        monkeypatch.delenv("VAL_ALPACA_PAPER_KEY", raising=False)
        monkeypatch.delenv("VAL_ALPACA_PAPER_SECRET", raising=False)
        monkeypatch.delenv("GENE_ALPACA_PAPER_KEY", raising=False)
        monkeypatch.delenv("GENE_ALPACA_PAPER_SECRET", raising=False)
        # Reset health singleton to CONNECTING (default)
        ap._health.set(ap.CONNECTING)
        snap = ap._ingest_health_snapshot()
        # CONNECTING state with no bar received -> unconfigured
        assert snap["ws_state"] == ap.CONNECTING
        assert snap["status"] == "unconfigured"
