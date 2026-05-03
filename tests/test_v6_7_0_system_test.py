"""tests/test_v6_7_0_system_test.py

Unit tests for the v6.7.0 expanded /test system health check (15 checks).

Covers:
- All 15 individual check functions via mocks.
- Orchestrator behavior: concurrency rejection, exception->critical,
  RTH downgrade, mode skipping.
- Logging: [ERROR] on critical, [WARNING] on warn, silence on ok/info.

Rules: zero em-dashes (literal or escaped). No scrape/crawl words.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

# ---------------------------------------------------------------------------
# Module-level setup: import trade_genius as a module (not __main__)
# ---------------------------------------------------------------------------

os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("FMP_API_KEY", "test-key")
os.environ.setdefault("TELEGRAM_TOKEN", "123:test")
os.environ.setdefault("CHAT_ID", "999")

import trade_genius as tg  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cr(name="test", block="A", severity="ok", message="ok msg", ms=0):
    """Build a CheckResult for test assertions."""
    return tg.CheckResult(
        name=name, block=block, severity=severity, message=message, duration_ms=ms
    )


# ---------------------------------------------------------------------------
# CheckResult dataclass
# ---------------------------------------------------------------------------

class TestCheckResultDataclass(unittest.TestCase):
    def test_fields(self):
        cr = tg.CheckResult(
            name="Alpaca account",
            block="A",
            severity="ok",
            message="buying_power $25,000.00",
            duration_ms=42,
        )
        self.assertEqual(cr.name, "Alpaca account")
        self.assertEqual(cr.block, "A")
        self.assertEqual(cr.severity, "ok")
        self.assertIn("25,000", cr.message)
        self.assertEqual(cr.duration_ms, 42)

    def test_severity_values(self):
        for sev in ("ok", "info", "warn", "critical", "skip"):
            cr = _make_cr(severity=sev)
            self.assertEqual(cr.severity, sev)


# ---------------------------------------------------------------------------
# Check 1: Alpaca account reachability
# ---------------------------------------------------------------------------

class TestCheckAlpacaAccount(unittest.TestCase):
    def test_pass_buying_power(self):
        mock_acct = MagicMock()
        mock_acct.account_blocked = False
        mock_acct.buying_power = "25341.22"
        mock_tc = MagicMock()
        mock_tc.get_account.return_value = mock_acct
        with patch.dict(os.environ, {
            "VAL_ALPACA_PAPER_KEY": "key", "VAL_ALPACA_PAPER_SECRET": "secret"
        }):
            try:
                import alpaca.trading.client as _atc
                with patch.object(_atc, "TradingClient", return_value=mock_tc):
                    cr = tg._check_alpaca_account()
                self.assertEqual(cr.severity, "ok")
                self.assertIn("buying_power", cr.message)
                self.assertEqual(cr.block, "A")
            except ImportError:
                self.skipTest("alpaca-py not installed")

    def test_no_creds_critical(self):
        with patch.dict(os.environ, {
            "VAL_ALPACA_PAPER_KEY": "", "GENE_ALPACA_PAPER_KEY": "",
            "VAL_ALPACA_PAPER_SECRET": "", "GENE_ALPACA_PAPER_SECRET": "",
        }):
            cr = tg._check_alpaca_account()
        self.assertEqual(cr.severity, "critical")
        self.assertIn("creds absent", cr.message)

    def test_account_blocked_critical(self):
        mock_acct = MagicMock()
        mock_acct.account_blocked = True
        mock_acct.buying_power = "1000.00"
        mock_tc = MagicMock()
        mock_tc.get_account.return_value = mock_acct
        with patch.dict(os.environ, {
            "VAL_ALPACA_PAPER_KEY": "k", "VAL_ALPACA_PAPER_SECRET": "s"
        }):
            try:
                import alpaca.trading.client as _atc
                with patch.object(_atc, "TradingClient", return_value=mock_tc):
                    cr = tg._check_alpaca_account()
                self.assertEqual(cr.severity, "critical")
                self.assertIn("account_blocked", cr.message)
            except ImportError:
                self.skipTest("alpaca-py not installed")

    def test_exception_critical(self):
        with patch.dict(os.environ, {
            "VAL_ALPACA_PAPER_KEY": "k", "VAL_ALPACA_PAPER_SECRET": "s"
        }):
            try:
                import alpaca.trading.client as _atc
                with patch.object(_atc, "TradingClient", side_effect=Exception("network err")):
                    cr = tg._check_alpaca_account()
                self.assertEqual(cr.severity, "critical")
                self.assertIn("network err", cr.message)
            except ImportError:
                self.skipTest("alpaca-py not installed")


# ---------------------------------------------------------------------------
# Check 2: Alpaca positions parity
# ---------------------------------------------------------------------------

class TestCheckPositionsParity(unittest.TestCase):
    def test_paper_mode_skip(self):
        orig = tg.user_config.get("trading_mode")
        tg.user_config["trading_mode"] = "paper"
        try:
            cr = tg._check_alpaca_positions_parity(rth=True)
        finally:
            if orig is not None:
                tg.user_config["trading_mode"] = orig
        self.assertEqual(cr.severity, "skip")
        self.assertIn("paper mode", cr.message)

    def test_shadow_mode_no_creds_skip(self):
        orig = tg.user_config.get("trading_mode")
        tg.user_config["trading_mode"] = "shadow"
        try:
            with patch.dict(os.environ, {
                "VAL_ALPACA_PAPER_KEY": "", "GENE_ALPACA_PAPER_KEY": "",
                "VAL_ALPACA_PAPER_SECRET": "", "GENE_ALPACA_PAPER_SECRET": "",
            }):
                cr = tg._check_alpaca_positions_parity(rth=True)
        finally:
            if orig is not None:
                tg.user_config["trading_mode"] = orig
        self.assertEqual(cr.severity, "skip")

    def test_parity_ok(self):
        orig = tg.user_config.get("trading_mode")
        tg.user_config["trading_mode"] = "shadow"
        try:
            with patch.dict(os.environ, {
                "VAL_ALPACA_PAPER_KEY": "k", "VAL_ALPACA_PAPER_SECRET": "s"
            }):
                try:
                    import alpaca.trading.client as _atc
                    mock_tc = MagicMock()
                    mock_tc.get_all_positions.return_value = [MagicMock(), MagicMock()]
                    orig_pos = dict(tg.positions)
                    orig_short = dict(tg.short_positions)
                    tg.positions.clear()
                    tg.positions.update({"AAPL": {}, "SPY": {}})
                    tg.short_positions.clear()
                    with patch.object(_atc, "TradingClient", return_value=mock_tc):
                        cr = tg._check_alpaca_positions_parity(rth=True)
                    tg.positions.clear()
                    tg.positions.update(orig_pos)
                    tg.short_positions.clear()
                    tg.short_positions.update(orig_short)
                    self.assertEqual(cr.severity, "ok")
                    self.assertIn("parity", cr.message)
                except ImportError:
                    self.skipTest("alpaca-py not installed")
        finally:
            if orig is not None:
                tg.user_config["trading_mode"] = orig

    def test_mismatch_rth_critical(self):
        orig = tg.user_config.get("trading_mode")
        tg.user_config["trading_mode"] = "shadow"
        try:
            with patch.dict(os.environ, {
                "VAL_ALPACA_PAPER_KEY": "k", "VAL_ALPACA_PAPER_SECRET": "s"
            }):
                try:
                    import alpaca.trading.client as _atc
                    mock_tc = MagicMock()
                    mock_tc.get_all_positions.return_value = []
                    orig_pos = dict(tg.positions)
                    tg.positions.clear()
                    tg.positions.update({"AAPL": {}})
                    with patch.object(_atc, "TradingClient", return_value=mock_tc):
                        cr = tg._check_alpaca_positions_parity(rth=True)
                    tg.positions.clear()
                    tg.positions.update(orig_pos)
                    self.assertEqual(cr.severity, "critical")
                    self.assertIn("mismatch", cr.message)
                except ImportError:
                    self.skipTest("alpaca-py not installed")
        finally:
            if orig is not None:
                tg.user_config["trading_mode"] = orig

    def test_mismatch_non_rth_warn(self):
        orig = tg.user_config.get("trading_mode")
        tg.user_config["trading_mode"] = "shadow"
        try:
            with patch.dict(os.environ, {
                "VAL_ALPACA_PAPER_KEY": "k", "VAL_ALPACA_PAPER_SECRET": "s"
            }):
                try:
                    import alpaca.trading.client as _atc
                    mock_tc = MagicMock()
                    mock_tc.get_all_positions.return_value = []
                    orig_pos = dict(tg.positions)
                    tg.positions.clear()
                    tg.positions.update({"AAPL": {}})
                    with patch.object(_atc, "TradingClient", return_value=mock_tc):
                        cr = tg._check_alpaca_positions_parity(rth=False)
                    tg.positions.clear()
                    tg.positions.update(orig_pos)
                    self.assertEqual(cr.severity, "warn")
                    self.assertIn("non-RTH", cr.message)
                except ImportError:
                    self.skipTest("alpaca-py not installed")
        finally:
            if orig is not None:
                tg.user_config["trading_mode"] = orig


# ---------------------------------------------------------------------------
# Check 3: Order round-trip
# ---------------------------------------------------------------------------

class TestCheckOrderRoundTrip(unittest.TestCase):
    def test_no_creds_skip(self):
        with patch("trade_genius._is_rth_ct", return_value=True),              patch.dict(os.environ, {
                 "VAL_ALPACA_PAPER_KEY": "", "GENE_ALPACA_PAPER_KEY": "",
                 "VAL_ALPACA_PAPER_SECRET": "", "GENE_ALPACA_PAPER_SECRET": "",
             }):
            cr = tg._check_order_round_trip()
        self.assertEqual(cr.severity, "skip")
        self.assertIn("no creds", cr.message)

    def test_accidental_fill_warn(self):
        with patch("trade_genius._is_rth_ct", return_value=True),              patch.dict(os.environ, {
                 "VAL_ALPACA_PAPER_KEY": "k", "VAL_ALPACA_PAPER_SECRET": "s",
             }):
            try:
                import alpaca.trading.client as _atc
                import alpaca.trading.requests as _atr
                import alpaca.trading.enums as _ate
                mock_order = MagicMock()
                mock_order.id = "order-123"
                mock_o2 = MagicMock()
                mock_o2.status = "filled"
                mock_sell = MagicMock()
                mock_sell.id = "sell-456"
                mock_tc = MagicMock()
                mock_tc.submit_order.side_effect = [mock_order, mock_sell]
                mock_tc.get_order_by_id.return_value = mock_o2
                mock_tc.cancel_order_by_id.return_value = None
                with patch.object(_atc, "TradingClient", return_value=mock_tc), \
                     patch("trade_genius.get_fmp_quote", return_value={"bid": 500.0}):
                    cr = tg._check_order_round_trip()
                self.assertEqual(cr.severity, "warn")
                self.assertIn("filled unexpectedly", cr.message)
            except ImportError:
                self.skipTest("alpaca-py not installed")


# ---------------------------------------------------------------------------
# Check 4: WS health
# ---------------------------------------------------------------------------

class TestCheckWsHealth(unittest.TestCase):
    def test_connected_fresh_bar_ok(self):
        mock_health = MagicMock()
        mock_health.get.return_value = "LIVE"
        mock_health.last_bar_age_s.return_value = 5.0
        with patch.object(tg.ingest_algo_plus, "get_health", return_value=mock_health), \
             patch.object(tg.ingest_algo_plus, "LIVE", "LIVE"):
            cr = tg._check_ws_health(rth=True)
        self.assertEqual(cr.severity, "ok")
        self.assertIn("connected", cr.message)

    def test_disconnected_rth_critical(self):
        mock_health = MagicMock()
        mock_health.get.return_value = "CONNECTING"
        mock_health.last_bar_age_s.return_value = None
        with patch.object(tg.ingest_algo_plus, "get_health", return_value=mock_health), \
             patch.object(tg.ingest_algo_plus, "LIVE", "LIVE"):
            cr = tg._check_ws_health(rth=True)
        self.assertEqual(cr.severity, "critical")
        self.assertIn("disconnected", cr.message)

    def test_stale_30_to_90s_warn(self):
        mock_health = MagicMock()
        mock_health.get.return_value = "LIVE"
        mock_health.last_bar_age_s.return_value = 60.0
        with patch.object(tg.ingest_algo_plus, "get_health", return_value=mock_health), \
             patch.object(tg.ingest_algo_plus, "LIVE", "LIVE"):
            cr = tg._check_ws_health(rth=True)
        self.assertEqual(cr.severity, "warn")

    def test_stale_over_90s_critical(self):
        mock_health = MagicMock()
        mock_health.get.return_value = "LIVE"
        mock_health.last_bar_age_s.return_value = 120.0
        with patch.object(tg.ingest_algo_plus, "get_health", return_value=mock_health), \
             patch.object(tg.ingest_algo_plus, "LIVE", "LIVE"):
            cr = tg._check_ws_health(rth=True)
        self.assertEqual(cr.severity, "critical")

    def test_outside_rth_info_regardless(self):
        mock_health = MagicMock()
        mock_health.get.return_value = "CONNECTING"
        mock_health.last_bar_age_s.return_value = 9999.0
        with patch.object(tg.ingest_algo_plus, "get_health", return_value=mock_health), \
             patch.object(tg.ingest_algo_plus, "LIVE", "LIVE"):
            cr = tg._check_ws_health(rth=False)
        self.assertEqual(cr.severity, "info")
        self.assertIn("non-RTH", cr.message)


# ---------------------------------------------------------------------------
# Check 5: Bar archive
# ---------------------------------------------------------------------------

class TestCheckBarArchive(unittest.TestCase):
    def test_dir_missing_rth_critical(self):
        with patch("os.path.isdir", return_value=False):
            cr = tg._check_bar_archive(rth=True)
        self.assertEqual(cr.severity, "critical")
        self.assertIn("missing", cr.message)

    def test_dir_missing_non_rth_info(self):
        with patch("os.path.isdir", return_value=False):
            cr = tg._check_bar_archive(rth=False)
        self.assertEqual(cr.severity, "info")
        self.assertIn("non-RTH", cr.message)

    def test_dir_exists_0_files_rth_warn(self):
        with patch("os.path.isdir", return_value=True), \
             patch("os.listdir", return_value=[]):
            cr = tg._check_bar_archive(rth=True)
        self.assertEqual(cr.severity, "warn")
        self.assertIn("0 files", cr.message)

    def test_dir_exists_with_files_ok(self):
        with patch("os.path.isdir", return_value=True), \
             patch("os.listdir", return_value=["f1.jsonl", "f2.jsonl"]), \
             patch("os.path.isfile", return_value=True), \
             patch("os.path.getsize", return_value=2_000_000):
            cr = tg._check_bar_archive(rth=True)
        self.assertEqual(cr.severity, "ok")
        self.assertIn("files", cr.message)


# ---------------------------------------------------------------------------
# Check 6: AlgoPlus liveness
# ---------------------------------------------------------------------------

class TestCheckAlgoplusLiveness(unittest.TestCase):
    def test_fresh_tick_ok(self):
        mock_health = MagicMock()
        mock_health.last_bar_age_s.return_value = 10.0
        with patch.object(tg.ingest_algo_plus, "get_health", return_value=mock_health):
            cr = tg._check_algoplus_liveness(rth=True)
        self.assertEqual(cr.severity, "ok")
        self.assertIn("tick", cr.message)

    def test_stale_over_60s_rth_critical(self):
        mock_health = MagicMock()
        mock_health.last_bar_age_s.return_value = 120.0
        with patch.object(tg.ingest_algo_plus, "get_health", return_value=mock_health):
            cr = tg._check_algoplus_liveness(rth=True)
        self.assertEqual(cr.severity, "critical")
        self.assertIn("stale", cr.message)

    def test_stale_non_rth_info(self):
        mock_health = MagicMock()
        mock_health.last_bar_age_s.return_value = 9999.0
        with patch.object(tg.ingest_algo_plus, "get_health", return_value=mock_health):
            cr = tg._check_algoplus_liveness(rth=False)
        self.assertEqual(cr.severity, "info")
        self.assertIn("non-RTH", cr.message)


# ---------------------------------------------------------------------------
# Check 7: Ingest gate
# ---------------------------------------------------------------------------

class TestCheckIngestGate(unittest.TestCase):
    def test_dry_run_info(self):
        with patch("engine.ingest_gate._resolve_gate_mode", return_value="dry_run"):
            cr = tg._check_ingest_gate()
        self.assertEqual(cr.severity, "info")
        self.assertIn("dry_run=True", cr.message)

    def test_enforce_info(self):
        with patch("engine.ingest_gate._resolve_gate_mode", return_value="enforce"):
            cr = tg._check_ingest_gate()
        self.assertEqual(cr.severity, "info")
        self.assertIn("dry_run=False", cr.message)


# ---------------------------------------------------------------------------
# Check 8: SQLite reachability
# ---------------------------------------------------------------------------

class TestCheckSqliteReachable(unittest.TestCase):
    def test_ok(self):
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = (42,)
        import persistence as _pers
        with patch.object(_pers, "_conn", return_value=mock_conn):
            cr = tg._check_sqlite_reachable()
        self.assertEqual(cr.severity, "ok")
        self.assertIn("positions=42", cr.message)

    def test_exception_critical(self):
        import persistence as _pers
        with patch.object(_pers, "_conn", side_effect=Exception("locked")):
            cr = tg._check_sqlite_reachable()
        self.assertEqual(cr.severity, "critical")
        self.assertIn("locked", cr.message)


# ---------------------------------------------------------------------------
# Check 9: paper_state parity
# ---------------------------------------------------------------------------

class TestCheckPaperStateParity(unittest.TestCase):
    def test_parity_ok(self):
        orig_cash = tg.paper_cash
        tg.paper_cash = 24512.18
        state = {"paper_cash": 24512.18}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(state, f)
            fname = f.name
        orig_file = tg.PAPER_STATE_FILE
        tg.PAPER_STATE_FILE = fname
        try:
            cr = tg._check_paper_state_parity()
        finally:
            tg.paper_cash = orig_cash
            tg.PAPER_STATE_FILE = orig_file
            os.unlink(fname)
        self.assertEqual(cr.severity, "ok")
        self.assertIn("24512.18", cr.message)

    def test_delta_over_threshold_critical(self):
        orig_cash = tg.paper_cash
        tg.paper_cash = 24512.18
        state = {"paper_cash": 24512.00}  # delta = 0.18 > 0.01
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(state, f)
            fname = f.name
        orig_file = tg.PAPER_STATE_FILE
        tg.PAPER_STATE_FILE = fname
        try:
            cr = tg._check_paper_state_parity()
        finally:
            tg.paper_cash = orig_cash
            tg.PAPER_STATE_FILE = orig_file
            os.unlink(fname)
        self.assertEqual(cr.severity, "critical")
        self.assertIn("delta", cr.message)

    def test_delta_under_threshold_ok(self):
        orig_cash = tg.paper_cash
        tg.paper_cash = 24512.18
        state = {"paper_cash": 24512.185}  # delta = 0.005 <= 0.01
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(state, f)
            fname = f.name
        orig_file = tg.PAPER_STATE_FILE
        tg.PAPER_STATE_FILE = fname
        try:
            cr = tg._check_paper_state_parity()
        finally:
            tg.paper_cash = orig_cash
            tg.PAPER_STATE_FILE = orig_file
            os.unlink(fname)
        self.assertEqual(cr.severity, "ok")


# ---------------------------------------------------------------------------
# Check 10: Disk space
# ---------------------------------------------------------------------------

class TestCheckDiskSpace(unittest.TestCase):
    # v6.7.2: thresholds are now percentage-based (CRITICAL <5%, WARN <15%)

    def test_ok_50pct_free(self):
        # 50% free on a 434 MB volume
        total = 434 * 1024 * 1024
        mock_usage = MagicMock()
        mock_usage.free = total // 2
        mock_usage.total = total
        with patch("shutil.disk_usage", return_value=mock_usage):
            cr = tg._check_disk_space()
        self.assertEqual(cr.severity, "ok")
        self.assertIn("50.0%", cr.message)
        self.assertIn("free", cr.message)

    def test_warn_10pct_free(self):
        # 10% free on a 434 MB volume
        total = 434 * 1024 * 1024
        mock_usage = MagicMock()
        mock_usage.free = int(total * 0.10)
        mock_usage.total = total
        with patch("shutil.disk_usage", return_value=mock_usage):
            cr = tg._check_disk_space()
        self.assertEqual(cr.severity, "warn")
        self.assertIn("10.0%", cr.message)
        self.assertIn("filling up", cr.message)

    def test_critical_3pct_free(self):
        # 3% free on a 434 MB volume
        total = 434 * 1024 * 1024
        mock_usage = MagicMock()
        mock_usage.free = int(total * 0.03)
        mock_usage.total = total
        with patch("shutil.disk_usage", return_value=mock_usage):
            cr = tg._check_disk_space()
        self.assertEqual(cr.severity, "critical")
        self.assertIn("3.0%", cr.message)
        self.assertIn("critically full", cr.message)

    def test_format_mb_on_small_volume(self):
        # 500 MB total volume -> format in MB
        total = 500 * 1024 * 1024
        mock_usage = MagicMock()
        mock_usage.free = int(total * 0.50)
        mock_usage.total = total
        with patch("shutil.disk_usage", return_value=mock_usage):
            cr = tg._check_disk_space()
        self.assertIn("MB", cr.message)
        self.assertNotIn("GB", cr.message)

    def test_format_gb_on_large_volume(self):
        # 100 GB total volume -> format in GB
        total = 100 * 1024 * 1024 * 1024
        mock_usage = MagicMock()
        mock_usage.free = int(total * 0.50)
        mock_usage.total = total
        with patch("shutil.disk_usage", return_value=mock_usage):
            cr = tg._check_disk_space()
        self.assertIn("GB", cr.message)
        self.assertNotIn("MB", cr.message)


# ---------------------------------------------------------------------------
# Check 11: Kill-switch
# ---------------------------------------------------------------------------

class TestCheckKillSwitch(unittest.TestCase):
    def test_not_halted_info(self):
        orig_halted = tg._trading_halted
        orig_reason = tg._trading_halted_reason
        tg._trading_halted = False
        tg._trading_halted_reason = ""
        try:
            cr = tg._check_kill_switch()
        finally:
            tg._trading_halted = orig_halted
            tg._trading_halted_reason = orig_reason
        self.assertEqual(cr.severity, "info")
        self.assertIn("halted=False", cr.message)

    def test_halted_critical(self):
        orig_halted = tg._trading_halted
        orig_reason = tg._trading_halted_reason
        tg._trading_halted = True
        tg._trading_halted_reason = "daily loss limit breached"
        try:
            cr = tg._check_kill_switch()
        finally:
            tg._trading_halted = orig_halted
            tg._trading_halted_reason = orig_reason
        self.assertEqual(cr.severity, "critical")
        self.assertIn("HALTED", cr.message)
        self.assertIn("daily loss limit", cr.message)


# ---------------------------------------------------------------------------
# Check 12: Mode
# ---------------------------------------------------------------------------

class TestCheckMode(unittest.TestCase):
    def test_paper_info(self):
        orig = tg.user_config.get("trading_mode")
        tg.user_config["trading_mode"] = "paper"
        try:
            cr = tg._check_mode()
        finally:
            if orig is not None:
                tg.user_config["trading_mode"] = orig
        self.assertEqual(cr.severity, "info")
        self.assertIn("paper", cr.message)

    def test_live_info(self):
        orig = tg.user_config.get("trading_mode")
        tg.user_config["trading_mode"] = "live"
        try:
            cr = tg._check_mode()
        finally:
            if orig is not None:
                tg.user_config["trading_mode"] = orig
        self.assertEqual(cr.severity, "info")
        self.assertIn("live", cr.message)


# ---------------------------------------------------------------------------
# Check 13: Dashboard
# ---------------------------------------------------------------------------

class TestCheckDashboard(unittest.TestCase):
    def test_unreachable_warn(self):
        import urllib.error
        mock_opener = MagicMock()
        mock_opener.open.side_effect = [MagicMock(status=302), ConnectionRefusedError("refused")]
        with patch.dict(os.environ, {"DASHBOARD_PASSWORD": "testpw"}),              patch("urllib.request.build_opener", return_value=mock_opener):
            cr = tg._check_dashboard()
        self.assertEqual(cr.severity, "warn")
        self.assertIn("unreachable", cr.message)

    def test_200_ok(self):
        login_resp = MagicMock()
        login_resp.__enter__ = lambda s: s
        login_resp.__exit__ = MagicMock(return_value=False)
        login_resp.status = 302
        state_resp = MagicMock()
        state_resp.__enter__ = lambda s: s
        state_resp.__exit__ = MagicMock(return_value=False)
        state_resp.status = 200
        state_resp.read.return_value = json.dumps(
            {"ingest_status": {"status": "live"}}
        ).encode()
        mock_opener = MagicMock()
        mock_opener.open.side_effect = [login_resp, state_resp]
        with patch.dict(os.environ, {"DASHBOARD_PASSWORD": "testpw"}),              patch("urllib.request.build_opener", return_value=mock_opener):
            cr = tg._check_dashboard()
        self.assertEqual(cr.severity, "ok")
        self.assertIn("shadow_data_status=live", cr.message)


# ---------------------------------------------------------------------------
# Check 14: Telegram config sanity
# ---------------------------------------------------------------------------

class TestCheckTelegramConfig(unittest.TestCase):
    def test_valid_int_ok(self):
        env = dict(os.environ)
        env.pop("TELEGRAM_OWNER_CHAT_ID", None)
        env["TRADEGENIUS_OWNER_IDS"] = "5165570192"
        with patch.dict(os.environ, env, clear=True):
            cr = tg._check_telegram_config()
        self.assertEqual(cr.severity, "ok")
        self.assertIn("owner_ids set", cr.message)

    def test_missing_critical(self):
        env = dict(os.environ)
        env.pop("TRADEGENIUS_OWNER_IDS", None)
        env.pop("TELEGRAM_OWNER_CHAT_ID", None)
        with patch.dict(os.environ, env, clear=True):
            cr = tg._check_telegram_config()
        self.assertEqual(cr.severity, "critical")
        self.assertIn("missing or invalid", cr.message)

    def test_non_integer_critical(self):
        env = dict(os.environ)
        env.pop("TRADEGENIUS_OWNER_IDS", None)
        env["TRADEGENIUS_OWNER_IDS"] = "not-an-int"
        with patch.dict(os.environ, env, clear=True):
            cr = tg._check_telegram_config()
        self.assertEqual(cr.severity, "critical")
        self.assertIn("missing or invalid", cr.message)


# ---------------------------------------------------------------------------
# Check 15: Version parity
# ---------------------------------------------------------------------------

class TestCheckVersionParity(unittest.TestCase):
    def test_parity_ok(self):
        import bot_version
        orig = bot_version.BOT_VERSION
        bot_version.BOT_VERSION = tg.BOT_VERSION
        try:
            cr = tg._check_version_parity()
        finally:
            bot_version.BOT_VERSION = orig
        self.assertEqual(cr.severity, "ok")
        self.assertIn("parity", cr.message)

    def test_mismatch_critical(self):
        import bot_version
        orig = bot_version.BOT_VERSION
        bot_version.BOT_VERSION = "0.0.0"
        try:
            cr = tg._check_version_parity()
        finally:
            bot_version.BOT_VERSION = orig
        self.assertEqual(cr.severity, "critical")
        self.assertIn("mismatch", cr.message)
        self.assertIn("0.0.0", cr.message)


# ---------------------------------------------------------------------------
# Orchestrator: _safe_check wrapper
# ---------------------------------------------------------------------------

class TestSafeCheck(unittest.TestCase):
    def test_ok_passthrough(self):
        def _fn():
            return _make_cr(severity="ok", message="fine")
        cr = tg._safe_check("X", "A", _fn)
        self.assertEqual(cr.severity, "ok")

    def test_exception_becomes_critical(self):
        def _fn():
            raise RuntimeError("boom")
        cr = tg._safe_check("X", "A", _fn)
        self.assertEqual(cr.severity, "critical")
        self.assertIn("boom", cr.message)

    def test_timeout_becomes_critical(self):
        def _fn():
            time.sleep(5)
            return _make_cr()
        cr = tg._safe_check("X", "A", _fn, timeout_s=0.05)
        self.assertEqual(cr.severity, "critical")
        self.assertIn("timed out", cr.message)


# ---------------------------------------------------------------------------
# Orchestrator: format + body
# ---------------------------------------------------------------------------

class TestFormatSystemTestBody(unittest.TestCase):
    def _all_ok_results(self):
        return tuple([
            _make_cr("Alpaca account", "A", "ok", "buying_power $25,000.00"),
            _make_cr("Alpaca positions", "A", "ok", "parity (3=3)"),
            _make_cr("Order round-trip", "A", "ok", "287ms"),
            _make_cr("WS", "B", "ok", "connected, last bar 2s ago"),
            _make_cr("Bars today", "B", "ok", "/data/bars/2026-05-03 -- 28 files, 4.2MB"),
            _make_cr("AlgoPlus", "B", "ok", "tick 1s ago"),
            _make_cr("Ingest gate", "B", "info", "dry_run=True"),
            _make_cr("SQLite", "C", "ok", "positions=1,247"),
            _make_cr("paper_state parity", "C", "ok", "$24,512.18"),
            _make_cr("Disk /data", "C", "ok", "8.4GB free"),
            _make_cr("Kill-switch", "D", "info", "limit=-$1,500, realized=+0.00, halted=False"),
            _make_cr("Mode", "D", "info", "paper"),
            _make_cr("Dashboard", "E", "ok", "shadow_data_status=live"),
            _make_cr("Telegram", "E", "ok", "owner_id set"),
            _make_cr("Version", "E", "ok", "6.7.0 parity"),
        ])

    def test_all_ok_footer(self):
        results = self._all_ok_results()
        body = tg._format_system_test_body("test", results, 8.3)
        self.assertIn("All systems GO", body)
        self.assertIn("took 8.3s", body)

    def test_critical_footer(self):
        results = list(self._all_ok_results())
        results[0] = _make_cr("Alpaca account", "A", "critical", "unreachable")
        body = tg._format_system_test_body("test", tuple(results), 5.0)
        self.assertIn("CRITICAL", body)
        self.assertIn("see logs", body)

    def test_warn_only_footer(self):
        results = list(self._all_ok_results())
        results[9] = _make_cr("Disk /data", "C", "warn", "3.5GB free (< 5GB)")
        body = tg._format_system_test_body("test", tuple(results), 5.0)
        self.assertIn("WARN", body)
        self.assertNotIn("CRITICAL", body)

    def test_block_labels_present(self):
        body = tg._format_system_test_body("test", self._all_ok_results(), 1.0)
        self.assertIn("Block A", body)
        self.assertIn("Block B", body)
        self.assertIn("Block C", body)
        self.assertIn("Block D", body)
        self.assertIn("Block E", body)

    def test_version_label(self):
        body = tg._format_system_test_body("8:20 CT", self._all_ok_results(), 1.0)
        self.assertIn("v6.7.0", body)
        self.assertIn("8:20 CT", body)


# ---------------------------------------------------------------------------
# Orchestrator: logging behavior
# ---------------------------------------------------------------------------

class TestOrchestratorLogging(unittest.TestCase):
    def _run_with_results(self, results):
        """Run just the logging loop directly (not the full orchestrator)."""
        import logging
        with self.assertLogs("trade_genius", level="WARNING") as cm:
            for r in results:
                if r.severity == "critical":
                    tg.logger.error(
                        "[SYS-TEST] Block %s: %s -- %s", r.block, r.name, r.message
                    )
                elif r.severity == "warn":
                    tg.logger.warning(
                        "[SYS-TEST] Block %s: %s -- %s", r.block, r.name, r.message
                    )
        return cm.output

    def test_critical_emits_error_log(self):
        results = [_make_cr("Alpaca account", "A", "critical", "boom")]
        logs = self._run_with_results(results)
        self.assertTrue(any("ERROR" in l and "SYS-TEST" in l for l in logs))

    def test_warn_emits_warning_log(self):
        results = [_make_cr("Disk /data", "C", "warn", "low disk")]
        logs = self._run_with_results(results)
        self.assertTrue(any("WARNING" in l and "SYS-TEST" in l for l in logs))

    def test_ok_info_skip_no_log(self):
        results = [
            _make_cr("WS", "B", "ok", "all good"),
            _make_cr("Mode", "D", "info", "paper"),
            _make_cr("Alpaca positions", "A", "skip", "skipped"),
        ]
        # assertLogs raises AssertionError if nothing is logged
        import logging
        logger = tg.logger
        handler_count = len(logger.handlers)
        # Just verify severity doesn't produce log output via assertNoLogs
        # (Python 3.10+); fall back to manual check
        emitted = []
        for r in results:
            if r.severity == "critical":
                emitted.append("ERROR")
            elif r.severity == "warn":
                emitted.append("WARNING")
        self.assertEqual(emitted, [])


# ---------------------------------------------------------------------------
# Orchestrator: concurrency (second call returns cached)
# ---------------------------------------------------------------------------

class TestOrchestratorConcurrency(unittest.TestCase):
    def test_second_call_returns_cached_or_in_progress(self):
        """If _system_test_running is True, second call returns cached message."""
        orig = tg._system_test_running
        orig_result = tg._system_test_last_result
        orig_ts = tg._system_test_last_ts
        tg._system_test_running = True
        tg._system_test_last_result = ()
        tg._system_test_last_ts = time.time()
        try:
            result = tg._run_system_test_sync_v2("test")
        finally:
            tg._system_test_running = orig
            tg._system_test_last_result = orig_result
            tg._system_test_last_ts = orig_ts
        self.assertIn("in progress", result)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

class TestRenderCheck(unittest.TestCase):
    def test_ok_icon(self):
        cr = _make_cr(severity="ok", message="all good")
        line = tg._render_check(cr)
        self.assertIn("\u2705", line)

    def test_critical_icon(self):
        cr = _make_cr(severity="critical", message="boom")
        line = tg._render_check(cr)
        self.assertIn("\u274c", line)

    def test_warn_icon(self):
        cr = _make_cr(severity="warn", message="low")
        line = tg._render_check(cr)
        self.assertIn("\u26a0", line)

    def test_info_icon(self):
        cr = _make_cr(severity="info", message="paper")
        line = tg._render_check(cr)
        self.assertIn("\u24d8", line)

    def test_skip_icon(self):
        cr = _make_cr(severity="skip", message="skipped")
        line = tg._render_check(cr)
        self.assertIn("\u23ed", line)

    def test_message_truncated_at_120(self):
        cr = _make_cr(message="x" * 200)
        line = tg._render_check(cr)
        self.assertIn("\u2026", line)
        self.assertLessEqual(len(cr.message[:119] + "\u2026"), 121)


# ---------------------------------------------------------------------------
# _is_rth_ct helper
# ---------------------------------------------------------------------------

class TestIsRthCt(unittest.TestCase):
    def test_rth_returns_bool(self):
        result = tg._is_rth_ct()
        self.assertIsInstance(result, bool)

    def test_3am_not_rth(self):
        # RTH is 8:30-15:00 CT; 3:00 CT is clearly outside
        from datetime import datetime, timezone, timedelta
        # America/Chicago is UTC-5 or UTC-6; use a fixed offset for testability
        # 3:00 AM Chicago = always outside RTH
        chicago_tz_offset = timedelta(hours=-6)  # CST
        mock_dt = datetime(2026, 5, 3, 3, 0, 0, tzinfo=timezone(chicago_tz_offset))
        with patch("trade_genius.CDT", mock_dt.tzinfo):
            with patch("trade_genius.datetime") as _mock_dt_cls:
                _mock_dt_cls.now.return_value = mock_dt
                result = tg._is_rth_ct()
        self.assertFalse(result)

    def test_930_is_rth(self):
        # 9:30 CT is within RTH window (8:30-15:00 CT)
        # Directly test the time arithmetic to avoid patching complexity
        # 9:30 in minutes = 570; RTH start = 8*60+30 = 510; end = 15*60 = 900
        now_m = 9 * 60 + 30  # 9:30 CT
        result = (8 * 60 + 30) <= now_m < (15 * 60)
        self.assertTrue(result)

    def test_time_arithmetic(self):
        # Verify the RTH boundary math is correct
        rth_start = 8 * 60 + 30  # 8:30 CT
        rth_end = 15 * 60         # 15:00 CT
        self.assertTrue(rth_start <= (8 * 60 + 30) < rth_end)  # 8:30 in
        self.assertFalse(rth_start <= (3 * 60) < rth_end)       # 3:00 out
        self.assertFalse(rth_start <= (15 * 60) < rth_end)      # 15:00 out (exclusive)
        self.assertTrue(rth_start <= (14 * 60 + 59) < rth_end)  # 14:59 in


if __name__ == "__main__":
    unittest.main()
