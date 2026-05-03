"""v6.9.5 -- bar_cache SSM_BAR_CACHE_DIR env isolation tests.

Two tests verifying that _cache_root() and get_bars() honour the
SSM_BAR_CACHE_DIR environment variable introduced in v6.9.5 to fix
PermissionError when sweep workers try to write .cache_v2/ into a
read-only canonical bars directory.

Rules:
- Zero em-dashes in this file (Val rule: test files must be dash-free).
- No forbidden words (scrape, crawl, scraping, crawling).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backtest.bar_cache import _cache_root, _CACHE_DIR_NAME


class TestBarCacheEnvVar(unittest.TestCase):
    """Tests for _cache_root() SSM_BAR_CACHE_DIR behaviour."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        # Always clean the env var before each test
        os.environ.pop("SSM_BAR_CACHE_DIR", None)

    def tearDown(self):
        os.environ.pop("SSM_BAR_CACHE_DIR", None)
        self._tmpdir.cleanup()

    # Test 1 -- default: when env var is unset, cache root is bars_dir/.cache_v2
    def test_default_cache_dir_used_when_env_unset(self):
        """When SSM_BAR_CACHE_DIR is not set, _cache_root returns bars_dir/.cache_v2."""
        bars_dir = self.tmp / "bars"
        bars_dir.mkdir()

        # Confirm env var is absent
        self.assertNotIn("SSM_BAR_CACHE_DIR", os.environ)

        result = _cache_root(bars_dir)
        expected = bars_dir / _CACHE_DIR_NAME

        self.assertEqual(result, expected, (
            f"Expected default cache root {expected!r}, got {result!r}. "
            "When SSM_BAR_CACHE_DIR is unset, _cache_root must fall back to "
            f"bars_dir / '{_CACHE_DIR_NAME}'."
        ))

    # Test 2 -- env var override: when SSM_BAR_CACHE_DIR is set, cache root is that path
    def test_env_var_overrides_cache_dir(self):
        """When SSM_BAR_CACHE_DIR is set, _cache_root returns that path and mkdir works."""
        bars_dir = self.tmp / "bars"
        bars_dir.mkdir()

        cache_override = self.tmp / "my_writable_cache"
        os.environ["SSM_BAR_CACHE_DIR"] = str(cache_override)

        result = _cache_root(bars_dir)
        self.assertEqual(result, cache_override, (
            f"Expected SSM_BAR_CACHE_DIR override {cache_override!r}, got {result!r}."
        ))

        # Smoke: mkdir on the resolved cache root must succeed (writable path)
        (result / "AAPL").mkdir(parents=True, exist_ok=True)
        self.assertTrue((result / "AAPL").is_dir(), (
            "mkdir on the SSM_BAR_CACHE_DIR-derived path must succeed without "
            "PermissionError -- this is the core fix for the read-only bars_dir case."
        ))


if __name__ == "__main__":
    unittest.main()
