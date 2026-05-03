"""tests/test_v6_7_1_system_test.py

Unit tests for v6.7.1 system-test bug fixes:
  Fix 1: _check_order_round_trip non-RTH skip
  Fix 2: _check_dashboard auth-aware login flow
  Fix 3: _check_telegram_config TRADEGENIUS_OWNER_IDS env var
  Fix 4: _check_ingest_gate import (engine.ingest_gate, Dockerfile fix)

Rules: zero em-dashes (literal or escaped). No web-gathering keyword restrictions.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("FMP_API_KEY", "test-key")
os.environ.setdefault("TELEGRAM_TOKEN", "123:test")
os.environ.setdefault("CHAT_ID", "999")

import trade_genius as tg  # noqa: E402


# ---------------------------------------------------------------------------
# Fix 1: Order round-trip non-RTH skip
# ---------------------------------------------------------------------------

class TestCheckOrderRoundTripNonRTH(unittest.TestCase):
    """v6.7.1 Fix 1: IOC orders rejected outside RTH -- check must skip."""

    def test_non_rth_returns_skip(self):
        """Outside RTH (off session): severity must be skip regardless of creds."""
        with patch.object(tg, "_market_session", return_value="off"), \
             patch.dict(os.environ, {
                 "VAL_ALPACA_PAPER_KEY": "key123",
                 "VAL_ALPACA_PAPER_SECRET": "secret123",
             }):
            cr = tg._check_order_round_trip()
        self.assertEqual(cr.severity, "skip")
        self.assertIn("markets closed", cr.message)

    def test_non_rth_skip_even_without_creds(self):
        """Off session skip fires before the creds check."""
        with patch.object(tg, "_market_session", return_value="off"), \
             patch.dict(os.environ, {
                 "VAL_ALPACA_PAPER_KEY": "",
                 "GENE_ALPACA_PAPER_KEY": "",
                 "VAL_ALPACA_PAPER_SECRET": "",
                 "GENE_ALPACA_PAPER_SECRET": "",
             }):
            cr = tg._check_order_round_trip()
        self.assertEqual(cr.severity, "skip")
        self.assertIn("markets closed", cr.message)

    def test_rth_alpaca_api_error_is_critical(self):
        """Inside RTH: an Alpaca APIError must produce critical severity."""
        with patch.object(tg, "_market_session", return_value="rth"), \
             patch.dict(os.environ, {
                 "VAL_ALPACA_PAPER_KEY": "k",
                 "VAL_ALPACA_PAPER_SECRET": "s",
             }):
            try:
                import alpaca.trading.client as _atc
                mock_tc = MagicMock()
                mock_tc.submit_order.side_effect = Exception(
                    "APIError: ioc orders are only accepted during market hours"
                )
                with patch.object(_atc, "TradingClient", return_value=mock_tc), \
                     patch("trade_genius.get_fmp_quote", return_value={"bid": 500.0}):
                    cr = tg._check_order_round_trip()
                self.assertEqual(cr.severity, "critical")
                self.assertIn("APIError", cr.message)
            except ImportError:
                self.skipTest("alpaca-py not installed")

    def test_rth_no_creds_skip(self):
        """Inside RTH: no creds still produces skip (existing behavior)."""
        with patch.object(tg, "_market_session", return_value="rth"), \
             patch.dict(os.environ, {
                 "VAL_ALPACA_PAPER_KEY": "",
                 "GENE_ALPACA_PAPER_KEY": "",
                 "VAL_ALPACA_PAPER_SECRET": "",
                 "GENE_ALPACA_PAPER_SECRET": "",
             }):
            cr = tg._check_order_round_trip()
        self.assertEqual(cr.severity, "skip")
        self.assertIn("no creds", cr.message)


# ---------------------------------------------------------------------------
# Fix 2: Dashboard auth-aware login flow
# ---------------------------------------------------------------------------

class TestCheckDashboardAuthAware(unittest.TestCase):
    """v6.7.1 Fix 2: Dashboard check must use login flow, not bare /api/state."""

    def test_no_password_env_skip(self):
        """Missing DASHBOARD_PASSWORD: severity must be skip."""
        env = {k: v for k, v in os.environ.items()}
        env.pop("DASHBOARD_PASSWORD", None)
        with patch.dict(os.environ, env, clear=True):
            cr = tg._check_dashboard()
        self.assertEqual(cr.severity, "skip")
        self.assertIn("no dashboard password", cr.message)

    def _make_login_ok_opener(self, state_data):
        """Return a mock opener that succeeds login and returns state_data."""
        login_resp = MagicMock()
        login_resp.__enter__ = lambda s: s
        login_resp.__exit__ = MagicMock(return_value=False)
        login_resp.status = 302

        state_resp = MagicMock()
        state_resp.__enter__ = lambda s: s
        state_resp.__exit__ = MagicMock(return_value=False)
        state_resp.status = 200
        state_resp.read.return_value = json.dumps(state_data).encode()

        mock_opener = MagicMock()
        mock_opener.open.side_effect = [login_resp, state_resp]
        return mock_opener

    def test_login_success_api_state_ok(self):
        """Login succeeds + /api/state 200: severity ok with shadow_data_status."""
        state_data = {"ingest_status": {"status": "live"}}
        mock_opener = self._make_login_ok_opener(state_data)
        with patch.dict(os.environ, {"DASHBOARD_PASSWORD": "correct-pw"}), \
             patch("urllib.request.build_opener", return_value=mock_opener):
            cr = tg._check_dashboard()
        self.assertEqual(cr.severity, "ok")
        self.assertIn("shadow_data_status=live", cr.message)

    def test_login_failure_401_critical(self):
        """Login returns 401: severity must be critical."""
        login_resp = MagicMock()
        login_resp.__enter__ = lambda s: s
        login_resp.__exit__ = MagicMock(return_value=False)
        login_resp.status = 401

        mock_opener = MagicMock()
        mock_opener.open.return_value = login_resp
        with patch.dict(os.environ, {"DASHBOARD_PASSWORD": "wrong-pw"}), \
             patch("urllib.request.build_opener", return_value=mock_opener):
            cr = tg._check_dashboard()
        self.assertEqual(cr.severity, "critical")
        self.assertIn("login failed", cr.message)

    def test_login_exception_critical(self):
        """Login raises an exception: severity must be critical."""
        mock_opener = MagicMock()
        mock_opener.open.side_effect = ConnectionRefusedError("refused")
        with patch.dict(os.environ, {"DASHBOARD_PASSWORD": "pw"}), \
             patch("urllib.request.build_opener", return_value=mock_opener):
            cr = tg._check_dashboard()
        self.assertEqual(cr.severity, "critical")
        self.assertIn("login failed", cr.message)

    def test_api_state_non_200_warn(self):
        """Login ok but /api/state non-200: severity must be warn."""
        login_resp = MagicMock()
        login_resp.__enter__ = lambda s: s
        login_resp.__exit__ = MagicMock(return_value=False)
        login_resp.status = 302

        state_resp = MagicMock()
        state_resp.__enter__ = lambda s: s
        state_resp.__exit__ = MagicMock(return_value=False)
        state_resp.status = 503

        mock_opener = MagicMock()
        mock_opener.open.side_effect = [login_resp, state_resp]
        with patch.dict(os.environ, {"DASHBOARD_PASSWORD": "pw"}), \
             patch("urllib.request.build_opener", return_value=mock_opener):
            cr = tg._check_dashboard()
        self.assertEqual(cr.severity, "warn")
        self.assertIn("503", cr.message)


# ---------------------------------------------------------------------------
# Fix 3: Telegram env var -- TRADEGENIUS_OWNER_IDS
# ---------------------------------------------------------------------------

class TestCheckTelegramConfigOwnerIds(unittest.TestCase):
    """v6.7.1 Fix 3: Production env var is TRADEGENIUS_OWNER_IDS not TELEGRAM_OWNER_CHAT_ID."""

    def _clean_env(self):
        """Return env dict with both old and new vars removed."""
        env = dict(os.environ)
        env.pop("TRADEGENIUS_OWNER_IDS", None)
        env.pop("TELEGRAM_OWNER_CHAT_ID", None)
        return env

    def test_single_valid_id_ok(self):
        """Single integer in TRADEGENIUS_OWNER_IDS: severity ok."""
        env = self._clean_env()
        env["TRADEGENIUS_OWNER_IDS"] = "5165570192"
        with patch.dict(os.environ, env, clear=True):
            cr = tg._check_telegram_config()
        self.assertEqual(cr.severity, "ok")
        self.assertIn("owner_ids set", cr.message)
        self.assertIn("1", cr.message)

    def test_multiple_valid_ids_ok(self):
        """Comma-separated integers: severity ok, count reported."""
        env = self._clean_env()
        env["TRADEGENIUS_OWNER_IDS"] = "5165570192,9876543210"
        with patch.dict(os.environ, env, clear=True):
            cr = tg._check_telegram_config()
        self.assertEqual(cr.severity, "ok")
        self.assertIn("2", cr.message)

    def test_missing_critical(self):
        """TRADEGENIUS_OWNER_IDS absent: severity critical."""
        env = self._clean_env()
        with patch.dict(os.environ, env, clear=True):
            cr = tg._check_telegram_config()
        self.assertEqual(cr.severity, "critical")
        self.assertIn("TRADEGENIUS_OWNER_IDS", cr.message)
        self.assertIn("missing or invalid", cr.message)

    def test_empty_string_critical(self):
        """Empty TRADEGENIUS_OWNER_IDS: severity critical."""
        env = self._clean_env()
        env["TRADEGENIUS_OWNER_IDS"] = ""
        with patch.dict(os.environ, env, clear=True):
            cr = tg._check_telegram_config()
        self.assertEqual(cr.severity, "critical")

    def test_non_integer_entries_critical(self):
        """Non-parseable string: severity critical."""
        env = self._clean_env()
        env["TRADEGENIUS_OWNER_IDS"] = "not-an-int"
        with patch.dict(os.environ, env, clear=True):
            cr = tg._check_telegram_config()
        self.assertEqual(cr.severity, "critical")
        self.assertIn("missing or invalid", cr.message)

    def test_old_env_var_ignored(self):
        """TELEGRAM_OWNER_CHAT_ID set but TRADEGENIUS_OWNER_IDS absent: critical."""
        env = self._clean_env()
        env["TELEGRAM_OWNER_CHAT_ID"] = "5165570192"
        with patch.dict(os.environ, env, clear=True):
            cr = tg._check_telegram_config()
        self.assertEqual(cr.severity, "critical")


# ---------------------------------------------------------------------------
# Fix 4: ingest_config module import via engine.ingest_gate
# ---------------------------------------------------------------------------

class TestCheckIngestGateImport(unittest.TestCase):
    """v6.7.1 Fix 4: engine.ingest_gate import must succeed when ingest_config is present."""

    def test_resolve_gate_mode_dry_run_info(self):
        """Patching _resolve_gate_mode to dry_run: severity info, message correct."""
        with patch("engine.ingest_gate._resolve_gate_mode", return_value="dry_run"):
            cr = tg._check_ingest_gate()
        self.assertEqual(cr.severity, "info")
        self.assertIn("dry_run=True", cr.message)

    def test_resolve_gate_mode_enforce_info(self):
        """Patching _resolve_gate_mode to enforce: severity info, dry_run=False."""
        with patch("engine.ingest_gate._resolve_gate_mode", return_value="enforce"):
            cr = tg._check_ingest_gate()
        self.assertEqual(cr.severity, "info")
        self.assertIn("dry_run=False", cr.message)

    def test_import_error_graceful_info(self):
        """If ingest_config import fails (e.g., missing file), result is info not critical."""
        import importlib
        import sys
        saved = sys.modules.pop("engine.ingest_gate", None)
        try:
            with patch.dict("sys.modules", {"ingest_config": None}):
                cr = tg._check_ingest_gate()
            self.assertEqual(cr.severity, "info")
            self.assertIn("gate mode unreadable", cr.message)
        finally:
            if saved is not None:
                sys.modules["engine.ingest_gate"] = saved

    def test_ingest_config_file_exists(self):
        """ingest_config.py must exist at repo root (Dockerfile Fix 4)."""
        import os.path
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cfg_path = os.path.join(repo_root, "ingest_config.py")
        self.assertTrue(
            os.path.isfile(cfg_path),
            "ingest_config.py not found at repo root -- Dockerfile COPY will fail",
        )


if __name__ == "__main__":
    unittest.main()
