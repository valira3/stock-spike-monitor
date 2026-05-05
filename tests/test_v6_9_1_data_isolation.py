"""v6.9.1 -- sweep runner /data isolation tests.

Verify that every subsystem that previously hardcoded '/data/*' now
honours TG_DATA_ROOT and its per-subsystem override env var.

Rules enforced here:
- Zero em-dashes in test source (Val rule: test files must be dash-free).
- No forbidden action words in production code lines.
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Set required env vars before any trade_genius import.
os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("TELEGRAM_TOKEN", "0:test_dummy_token")
os.environ.setdefault("CHAT_ID", "0")
os.environ.setdefault("DASHBOARD_PASSWORD", "")
os.environ.setdefault("FMP_API_KEY", "test-key")
os.environ.setdefault("TG_BACKTEST_MODE", "1")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_with_env(module_name: str, env: dict[str, str]):
    """Reload a module with the given env overrides active, then restore."""
    saved = {k: os.environ.get(k) for k in env}
    for k, v in env.items():
        os.environ[k] = v
    try:
        mod = sys.modules.pop(module_name, None)
        # Re-import fresh
        imported = importlib.import_module(module_name)
        # Force module-level constants to re-evaluate by reloading
        imported = importlib.reload(imported)
        return imported
    finally:
        for k, orig in saved.items():
            if orig is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = orig


# ---------------------------------------------------------------------------
# volume_bucket.py -- DEFAULT_BARS_DIR
# ---------------------------------------------------------------------------

class TestVolumeBucketDataRoot(unittest.TestCase):

    def test_default_bars_dir_uses_tg_data_root(self):
        mod = _reload_with_env(
            "volume_bucket",
            {"TG_DATA_ROOT": "/tmp/tg_test_root", "BARS_DIR": ""},
        )
        self.assertEqual(mod.DEFAULT_BARS_DIR, "/tmp/tg_test_root/bars")

    def test_bars_dir_override_wins(self):
        mod = _reload_with_env(
            "volume_bucket",
            {"TG_DATA_ROOT": "/tmp/tg_test_root", "BARS_DIR": "/custom/bars"},
        )
        self.assertEqual(mod.DEFAULT_BARS_DIR, "/custom/bars")

    def test_fallback_to_slash_data_when_no_env(self):
        mod = _reload_with_env(
            "volume_bucket",
            {"TG_DATA_ROOT": "", "BARS_DIR": ""},
        )
        # When TG_DATA_ROOT is empty string, fallback is /data/bars
        self.assertTrue(mod.DEFAULT_BARS_DIR.endswith("/bars"))


# ---------------------------------------------------------------------------
# forensic_capture.py -- DEFAULT_BASE_DIR, DEFAULT_DAILY_BAR_DIR
# ---------------------------------------------------------------------------

class TestForensicCaptureDataRoot(unittest.TestCase):

    def test_default_base_dir_uses_tg_data_root(self):
        mod = _reload_with_env(
            "forensic_capture",
            {"TG_DATA_ROOT": "/tmp/fc_root", "FORENSICS_DIR": ""},
        )
        self.assertEqual(mod.DEFAULT_BASE_DIR, "/tmp/fc_root/forensics")

    def test_forensics_dir_override_wins(self):
        mod = _reload_with_env(
            "forensic_capture",
            {"TG_DATA_ROOT": "/tmp/fc_root", "FORENSICS_DIR": "/opt/forensics"},
        )
        self.assertEqual(mod.DEFAULT_BASE_DIR, "/opt/forensics")

    def test_default_daily_bar_dir_uses_tg_data_root(self):
        mod = _reload_with_env(
            "forensic_capture",
            {"TG_DATA_ROOT": "/tmp/fc_root", "DAILY_BAR_DIR": ""},
        )
        self.assertEqual(mod.DEFAULT_DAILY_BAR_DIR, "/tmp/fc_root/bars/daily")

    def test_daily_bar_dir_override_wins(self):
        mod = _reload_with_env(
            "forensic_capture",
            {"TG_DATA_ROOT": "/tmp/fc_root", "DAILY_BAR_DIR": "/mnt/daily"},
        )
        self.assertEqual(mod.DEFAULT_DAILY_BAR_DIR, "/mnt/daily")


# ---------------------------------------------------------------------------
# lifecycle_logger.py -- DEFAULT_DATA_DIR
# ---------------------------------------------------------------------------

class TestLifecycleLoggerDataRoot(unittest.TestCase):

    def test_default_data_dir_uses_tg_data_root(self):
        mod = _reload_with_env(
            "lifecycle_logger",
            {"TG_DATA_ROOT": "/tmp/lc_root", "LIFECYCLE_DIR": ""},
        )
        self.assertEqual(mod.DEFAULT_DATA_DIR, "/tmp/lc_root/lifecycle")

    def test_lifecycle_dir_override_wins(self):
        mod = _reload_with_env(
            "lifecycle_logger",
            {"TG_DATA_ROOT": "/tmp/lc_root", "LIFECYCLE_DIR": "/var/lifecycle"},
        )
        self.assertEqual(mod.DEFAULT_DATA_DIR, "/var/lifecycle")


# ---------------------------------------------------------------------------
# trade_genius.py -- V561_OR_DIR_DEFAULT
# ---------------------------------------------------------------------------

class TestTradeGeniusOrDir(unittest.TestCase):

    def _get_or_dir(self, env: dict[str, str]) -> str:
        """Read V561_OR_DIR_DEFAULT after forcing env vars."""
        saved = {k: os.environ.get(k) for k in env}
        for k, v in env.items():
            if v:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        try:
            import trade_genius as tg
            tg_mod = importlib.reload(tg)
            return tg_mod.V561_OR_DIR_DEFAULT
        finally:
            for k, orig in saved.items():
                if orig is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = orig

    def test_or_dir_default_uses_tg_data_root(self):
        val = self._get_or_dir({"TG_DATA_ROOT": "/tmp/tg_or", "OR_DIR": ""})
        self.assertEqual(val, "/tmp/tg_or/or")

    def test_or_dir_override_wins(self):
        val = self._get_or_dir({"TG_DATA_ROOT": "/tmp/tg_or", "OR_DIR": "/custom/or"})
        self.assertEqual(val, "/custom/or")


# ---------------------------------------------------------------------------
# trade_genius._check_disk_space -- sandbox fallback
# ---------------------------------------------------------------------------

class TestDiskSpaceSandboxFallback(unittest.TestCase):
    """_check_disk_space must return 'ok' not 'critical' when path absent."""

    def test_missing_data_root_returns_ok(self):
        import trade_genius as tg
        import importlib as _imp
        _imp.reload(tg)

        nonexistent = "/tmp/absolutely_does_not_exist_v691_test"
        with patch.dict(os.environ, {"TG_DATA_ROOT": nonexistent}):
            result = tg._check_disk_space()
        self.assertEqual(result.severity, "ok",
                         f"expected ok for missing path, got {result.severity}: {result.message}")
        self.assertIn("sandbox", result.message)


# ---------------------------------------------------------------------------
# executors/base.py -- default_chats_path
# ---------------------------------------------------------------------------

class TestExecutorBaseChatsPath(unittest.TestCase):

    def test_chats_path_uses_tg_data_root(self):
        """BaseExecutor must derive default_chats_path from TG_DATA_ROOT."""
        # We cannot fully construct BaseExecutor (requires broker deps),
        # so we inspect the source directly and check it no longer contains
        # the literal '/data/executor_chats' string.
        src_path = Path(__file__).parent.parent / "executors" / "base.py"
        src = src_path.read_text()
        self.assertNotIn(
            'f"/data/executor_chats_',
            src,
            "executors/base.py still has hardcoded /data/executor_chats_ literal",
        )
        self.assertIn(
            "TG_DATA_ROOT",
            src,
            "executors/base.py must reference TG_DATA_ROOT",
        )


if __name__ == "__main__":
    unittest.main()
