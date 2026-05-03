"""tests/test_v6_7_3_extended_hours.py

Unit tests for v6.7.3 fixes:
  Fix 1: _check_dashboard uses 127.0.0.1 not localhost
  Fix 2: Telegram header reflects runtime BOT_VERSION
  Fix 3: _market_session() tri-state (rth/extended/off) + per-check behavior
  Fix 4: Scheduler has 9 bi-hourly system-test firings

Rules: zero em-dashes (literal or escaped). No scrape/crawl words.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call

os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("FMP_API_KEY", "test-key")
os.environ.setdefault("TELEGRAM_TOKEN", "123:test")
os.environ.setdefault("CHAT_ID", "999")

import trade_genius as tg  # noqa: E402


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_chicago_dt(weekday, hour, minute):
    """Return a datetime with America/Chicago tzinfo at the given weekday/time.

    weekday: 0=Monday ... 6=Sunday
    Uses a fixed CDT offset (UTC-5) so tests are DST-stable.
    """
    from zoneinfo import ZoneInfo
    # Find a Monday in 2026 to anchor weekday math
    anchor = datetime(2026, 5, 4, tzinfo=ZoneInfo("America/Chicago"))  # Monday
    delta_days = weekday - anchor.weekday()
    target_date = anchor.date() + timedelta(days=delta_days)
    return datetime(target_date.year, target_date.month, target_date.day,
                    hour, minute, 0, tzinfo=ZoneInfo("America/Chicago"))


# ---------------------------------------------------------------------------
# _market_session() boundary tests
# ---------------------------------------------------------------------------

class TestMarketSession(unittest.TestCase):
    """Verify _market_session returns correct tri-state at boundary times."""

    def _session_at(self, weekday, hour, minute):
        dt = _make_chicago_dt(weekday, hour, minute)
        # Patch datetime.datetime.now at the stdlib level; _market_session uses
        # 'import datetime as _dt_mod' internally to avoid module-level patching.
        with patch("datetime.datetime") as mock_dt_cls:
            mock_dt_cls.now.return_value = dt
            return tg._market_session()

    def test_07_00_extended(self):
        self.assertEqual(self._session_at(0, 7, 0), "extended")

    def test_08_29_extended(self):
        self.assertEqual(self._session_at(0, 8, 29), "extended")

    def test_08_30_rth(self):
        self.assertEqual(self._session_at(0, 8, 30), "rth")

    def test_14_59_rth(self):
        self.assertEqual(self._session_at(0, 14, 59), "rth")

    def test_15_00_extended(self):
        self.assertEqual(self._session_at(0, 15, 0), "extended")

    def test_18_59_extended(self):
        self.assertEqual(self._session_at(0, 18, 59), "extended")

    def test_19_00_off(self):
        self.assertEqual(self._session_at(0, 19, 0), "off")

    def test_19_30_off(self):
        self.assertEqual(self._session_at(0, 19, 30), "off")

    def test_02_59_off(self):
        self.assertEqual(self._session_at(0, 2, 59), "off")

    def test_03_00_extended(self):
        self.assertEqual(self._session_at(0, 3, 0), "extended")

    def test_saturday_off(self):
        self.assertEqual(self._session_at(5, 10, 0), "off")

    def test_sunday_off(self):
        self.assertEqual(self._session_at(6, 10, 0), "off")

    def test_friday_rth_ok(self):
        self.assertEqual(self._session_at(4, 10, 0), "rth")


# ---------------------------------------------------------------------------
# Fix 1: Dashboard uses 127.0.0.1
# ---------------------------------------------------------------------------

class TestDashboard127(unittest.TestCase):
    """Fix 1: _check_dashboard must POST to 127.0.0.1, not localhost."""

    def test_request_uses_127_not_localhost(self):
        """The Request objects built inside _check_dashboard must target 127.0.0.1."""
        captured_urls = []

        class CapturingOpener:
            def open(self, req, timeout=None):
                captured_urls.append(req.full_url)
                resp = MagicMock()
                resp.__enter__ = lambda s: s
                resp.__exit__ = MagicMock(return_value=False)
                resp.status = 302 if "login" in req.full_url else 200
                if "api/state" in req.full_url:
                    resp.read.return_value = json.dumps(
                        {"ingest_status": {"status": "live"}}
                    ).encode()
                return resp

        with patch.dict(os.environ, {"DASHBOARD_PASSWORD": "pw", "DASHBOARD_PORT": "8080"}), \
             patch("urllib.request.build_opener", return_value=CapturingOpener()):
            tg._check_dashboard()

        self.assertTrue(len(captured_urls) >= 1, "No requests made")
        for url in captured_urls:
            self.assertIn("127.0.0.1", url, f"URL uses localhost instead of 127.0.0.1: {url}")
            self.assertNotIn("localhost", url, f"URL still uses localhost: {url}")


# ---------------------------------------------------------------------------
# Fix 2: Header includes runtime BOT_VERSION
# ---------------------------------------------------------------------------

class TestHeaderBotVersion(unittest.TestCase):
    """Fix 2: _format_system_test_body header must reflect runtime BOT_VERSION."""

    def _make_results(self):
        return tuple([
            tg.CheckResult("Alpaca account", "A", "ok", "ok", 0),
            tg.CheckResult("Alpaca positions", "A", "skip", "paper", 0),
            tg.CheckResult("Order round-trip", "A", "skip", "off", 0),
            tg.CheckResult("WS", "B", "info", "ok", 0),
            tg.CheckResult("Bars today", "B", "info", "ok", 0),
            tg.CheckResult("AlgoPlus", "B", "info", "ok", 0),
            tg.CheckResult("Ingest gate", "B", "info", "ok", 0),
            tg.CheckResult("SQLite", "C", "ok", "ok", 0),
            tg.CheckResult("paper_state parity", "C", "ok", "ok", 0),
            tg.CheckResult("Disk /data", "C", "ok", "ok", 0),
            tg.CheckResult("Kill-switch", "D", "info", "ok", 0),
            tg.CheckResult("Mode", "D", "info", "paper", 0),
            tg.CheckResult("Dashboard", "E", "skip", "ok", 0),
            tg.CheckResult("Telegram", "E", "ok", "ok", 0),
            tg.CheckResult("Version", "E", "ok", "ok", 0),
        ])

    def test_header_contains_bot_version(self):
        """Header line must contain the runtime BOT_VERSION constant."""
        body = tg._format_system_test_body("Manual", self._make_results(), 1.0)
        self.assertIn(tg.BOT_VERSION, body)
        self.assertIn("v%s" % tg.BOT_VERSION, body)

    def test_header_not_hardcoded_670(self):
        """Header must not contain hardcoded v6.7.0 string."""
        body = tg._format_system_test_body("Manual", self._make_results(), 1.0)
        self.assertNotIn("v6.7.0", body)

    def test_header_reflects_patched_version(self):
        """If BOT_VERSION changes at runtime, header should match."""
        original = tg.BOT_VERSION
        try:
            tg.BOT_VERSION = "9.9.9"
            body = tg._format_system_test_body("test", self._make_results(), 1.0)
            self.assertIn("v9.9.9", body)
        finally:
            tg.BOT_VERSION = original


# ---------------------------------------------------------------------------
# Fix 3: Order round-trip -- EXTENDED uses DAY, OFF skips
# ---------------------------------------------------------------------------

class TestOrderRoundTripSession(unittest.TestCase):
    """Fix 3: _check_order_round_trip submits DAY in extended hours, skips when off."""

    def test_off_session_skips(self):
        with patch.object(tg, "_market_session", return_value="off"):
            cr = tg._check_order_round_trip()
        self.assertEqual(cr.severity, "skip")
        self.assertIn("markets closed", cr.message)

    def test_extended_uses_day_order(self):
        """EXTENDED session: order must use DAY time-in-force, not IOC."""
        with patch.object(tg, "_market_session", return_value="extended"), \
             patch.dict(os.environ, {"VAL_ALPACA_PAPER_KEY": "k", "VAL_ALPACA_PAPER_SECRET": "s"}):
            try:
                import alpaca.trading.client as _atc
                import alpaca.trading.enums as _ate
                submitted_reqs = []

                mock_order = MagicMock()
                mock_order.id = "eid-1"
                mock_status = MagicMock()
                mock_status.status = "canceled"
                mock_tc = MagicMock()
                mock_tc.submit_order.side_effect = lambda req: (submitted_reqs.append(req), mock_order)[1]
                mock_tc.get_order_by_id.return_value = mock_status

                with patch.object(_atc, "TradingClient", return_value=mock_tc), \
                     patch("trade_genius.get_fmp_quote", return_value={"bid": 500.0}):
                    cr = tg._check_order_round_trip()

                self.assertTrue(len(submitted_reqs) >= 1, "No order submitted")
                req = submitted_reqs[0]
                tif = getattr(req, "time_in_force", None)
                self.assertIsNotNone(tif, "time_in_force not set on request")
                self.assertNotIn("IOC", str(tif).upper(),
                                 "EXTENDED must use DAY not IOC, got: %s" % tif)
            except ImportError:
                self.skipTest("alpaca-py not installed")

    def test_rth_uses_ioc_order(self):
        """RTH session: order must use IOC time-in-force."""
        with patch.object(tg, "_market_session", return_value="rth"), \
             patch.dict(os.environ, {"VAL_ALPACA_PAPER_KEY": "k", "VAL_ALPACA_PAPER_SECRET": "s"}):
            try:
                import alpaca.trading.client as _atc
                submitted_reqs = []
                mock_order = MagicMock()
                mock_order.id = "eid-2"
                mock_status = MagicMock()
                mock_status.status = "canceled"
                mock_tc = MagicMock()
                mock_tc.submit_order.side_effect = lambda req: (submitted_reqs.append(req), mock_order)[1]
                mock_tc.get_order_by_id.return_value = mock_status
                with patch.object(_atc, "TradingClient", return_value=mock_tc), \
                     patch("trade_genius.get_fmp_quote", return_value={"bid": 500.0}):
                    cr = tg._check_order_round_trip()
                self.assertTrue(len(submitted_reqs) >= 1)
                req = submitted_reqs[0]
                tif = str(getattr(req, "time_in_force", "")).upper()
                self.assertIn("IOC", tif, "RTH must use IOC, got: %s" % tif)
            except ImportError:
                self.skipTest("alpaca-py not installed")


# ---------------------------------------------------------------------------
# Fix 3: WS health -- session tri-state
# ---------------------------------------------------------------------------

class TestWsHealthSession(unittest.TestCase):
    """Fix 3: _check_ws_health tri-state session behavior."""

    def _mock_health(self, state="LIVE", age=5.0):
        mock = MagicMock()
        mock.get.return_value = state
        mock.last_bar_age_s.return_value = age
        return mock

    def test_off_session_returns_info(self):
        with patch.object(tg.ingest_algo_plus, "get_health",
                          return_value=self._mock_health("CONNECTING", 9999.0)), \
             patch.object(tg.ingest_algo_plus, "LIVE", "LIVE"):
            cr = tg._check_ws_health("off")
        self.assertEqual(cr.severity, "info")
        self.assertIn("markets closed", cr.message)

    def test_extended_recent_bar_ok(self):
        with patch.object(tg.ingest_algo_plus, "get_health",
                          return_value=self._mock_health("LIVE", 10.0)), \
             patch.object(tg.ingest_algo_plus, "LIVE", "LIVE"):
            cr = tg._check_ws_health("extended")
        self.assertEqual(cr.severity, "ok")

    def test_extended_stale_60s_warn(self):
        with patch.object(tg.ingest_algo_plus, "get_health",
                          return_value=self._mock_health("LIVE", 60.0)), \
             patch.object(tg.ingest_algo_plus, "LIVE", "LIVE"):
            cr = tg._check_ws_health("extended")
        self.assertEqual(cr.severity, "warn")

    def test_extended_disconnected_critical(self):
        with patch.object(tg.ingest_algo_plus, "get_health",
                          return_value=self._mock_health("CONNECTING", 60.0)), \
             patch.object(tg.ingest_algo_plus, "LIVE", "LIVE"):
            cr = tg._check_ws_health("extended")
        self.assertEqual(cr.severity, "critical")
        self.assertIn("disconnected", cr.message)

    def test_rth_fresh_ok(self):
        with patch.object(tg.ingest_algo_plus, "get_health",
                          return_value=self._mock_health("LIVE", 5.0)), \
             patch.object(tg.ingest_algo_plus, "LIVE", "LIVE"):
            cr = tg._check_ws_health("rth")
        self.assertEqual(cr.severity, "ok")


# ---------------------------------------------------------------------------
# Fix 3: Bar archive -- session tri-state
# ---------------------------------------------------------------------------

class TestBarArchiveSession(unittest.TestCase):
    """Fix 3: _check_bar_archive extended warns on missing dir, rth criticals."""

    def test_rth_missing_dir_critical(self):
        with patch("os.path.isdir", return_value=False):
            cr = tg._check_bar_archive("rth")
        self.assertEqual(cr.severity, "critical")
        self.assertIn("missing", cr.message)

    def test_extended_missing_dir_warn(self):
        with patch("os.path.isdir", return_value=False):
            cr = tg._check_bar_archive("extended")
        self.assertEqual(cr.severity, "warn")
        self.assertIn("pre-market", cr.message)

    def test_off_missing_dir_info(self):
        with patch("os.path.isdir", return_value=False):
            cr = tg._check_bar_archive("off")
        self.assertEqual(cr.severity, "info")
        self.assertIn("markets closed", cr.message)

    def test_rth_dir_exists_files_ok(self):
        with patch("os.path.isdir", return_value=True), \
             patch("os.listdir", return_value=["f1.jsonl"]), \
             patch("os.path.isfile", return_value=True), \
             patch("os.path.getsize", return_value=500_000):
            cr = tg._check_bar_archive("rth")
        self.assertEqual(cr.severity, "ok")

    def test_extended_dir_exists_0_files_warn(self):
        with patch("os.path.isdir", return_value=True), \
             patch("os.listdir", return_value=[]):
            cr = tg._check_bar_archive("extended")
        self.assertEqual(cr.severity, "warn")
        self.assertIn("extended hours", cr.message)


# ---------------------------------------------------------------------------
# Fix 3: AlgoPlus -- session tri-state
# ---------------------------------------------------------------------------

class TestAlgoplusSession(unittest.TestCase):
    """Fix 3: _check_algoplus_liveness extended/off/rth thresholds."""

    def _mock_health(self, age):
        m = MagicMock()
        m.last_bar_age_s.return_value = age
        return m

    def test_off_returns_info(self):
        with patch.object(tg.ingest_algo_plus, "get_health", return_value=self._mock_health(9999.0)):
            cr = tg._check_algoplus_liveness("off")
        self.assertEqual(cr.severity, "info")
        self.assertIn("markets closed", cr.message)

    def test_extended_90s_ok(self):
        """90s old is under 120s threshold -> ok in extended."""
        with patch.object(tg.ingest_algo_plus, "get_health", return_value=self._mock_health(90.0)):
            cr = tg._check_algoplus_liveness("extended")
        self.assertEqual(cr.severity, "ok")

    def test_extended_121s_warn(self):
        """121s old is over 120s extended threshold -> warn."""
        with patch.object(tg.ingest_algo_plus, "get_health", return_value=self._mock_health(121.0)):
            cr = tg._check_algoplus_liveness("extended")
        self.assertEqual(cr.severity, "warn")
        self.assertIn("extended hours", cr.message)

    def test_rth_90s_critical(self):
        """90s old is over 60s RTH threshold -> critical."""
        with patch.object(tg.ingest_algo_plus, "get_health", return_value=self._mock_health(90.0)):
            cr = tg._check_algoplus_liveness("rth")
        self.assertEqual(cr.severity, "critical")
        self.assertIn("stale", cr.message)

    def test_rth_30s_ok(self):
        with patch.object(tg.ingest_algo_plus, "get_health", return_value=self._mock_health(30.0)):
            cr = tg._check_algoplus_liveness("rth")
        self.assertEqual(cr.severity, "ok")


# ---------------------------------------------------------------------------
# Fix 4: Scheduler has 9 bi-hourly firings
# ---------------------------------------------------------------------------

class TestSchedulerFirings(unittest.TestCase):
    """Fix 4: JOBS list must contain exactly 9 system-test entries with v6.7.3 labels."""

    def _get_jobs(self):
        """Extract JOBS list by inspecting scheduler_thread source."""
        import inspect
        import ast
        src = inspect.getsource(tg.scheduler_thread)
        # Count _fire_system_test lambda entries
        return [line.strip() for line in src.split('\n')
                if '_fire_system_test' in line and 'lambda' in line]

    def test_nine_firings_registered(self):
        jobs = self._get_jobs()
        self.assertEqual(len(jobs), 9, f"Expected 9, got {len(jobs)}: {jobs}")

    def test_pre_open_label_present(self):
        jobs = self._get_jobs()
        labels = " ".join(jobs)
        self.assertIn("pre-open", labels)

    def test_post_close_label_present(self):
        jobs = self._get_jobs()
        labels = " ".join(jobs)
        self.assertIn("post-close", labels)

    def test_rth_close_label_present(self):
        jobs = self._get_jobs()
        labels = " ".join(jobs)
        self.assertIn("RTH close", labels)

    def test_no_old_820_label(self):
        jobs = self._get_jobs()
        labels = " ".join(jobs)
        self.assertNotIn("8:20 CT", labels)

    def test_no_old_831_label(self):
        jobs = self._get_jobs()
        labels = " ".join(jobs)
        self.assertNotIn("8:31 CT", labels)


if __name__ == "__main__":
    unittest.main()
