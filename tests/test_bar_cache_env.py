"""v6.9.6 -- bar_cache SSM_BAR_CACHE_DIR env isolation tests.

Tests verifying that _cache_root(), _build_ticker_cache(), _ensure_cache(),
and get_bars() all honour the SSM_BAR_CACHE_DIR environment variable
introduced in v6.9.5 to fix PermissionError when sweep workers try to write
.cache_v2/ into a read-only canonical bars directory.

v6.9.6 adds Test 3 (test_build_and_ensure_with_readonly_bars_dir) which
directly exercises the _build_ticker_cache and _ensure_cache call sites that
were failing in production sweeps.

Real production stack trace this test guards against:
    File "/tmp/ssm_v661/backtest/bar_cache.py", line 263, in _build_ticker_cache
        ticker_dir.mkdir(parents=True, exist_ok=True)
    PermissionError: [Errno 13] Permission denied:
        '/home/user/workspace/canonical_backtest_data/84day_2026_sip/replay_layout/.cache_v2'

This test MUST fail on any version where _build_ticker_cache still uses
bars_dir / '.cache_v2' directly and MUST pass when _cache_root(bars_dir) is
used throughout.

Rules:
- Zero em-dashes in this file (Val rule: test files must be dash-free).
- No forbidden words (scrape, crawl, scraping, crawling).
"""

from __future__ import annotations

import json
import os
import stat
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
        # Restore write bits before cleanup so TemporaryDirectory.cleanup()
        # can remove the read-only tree we may have created in test 3.
        self._restore_write_bits(self.tmp)
        self._tmpdir.cleanup()

    @staticmethod
    def _restore_write_bits(path: Path) -> None:
        """Recursively add write+exec bits so the temp dir can be removed."""
        if not path.exists():
            return
        try:
            current = stat.S_IMODE(os.stat(path).st_mode)
            os.chmod(path, current | 0o700)
        except OSError:
            pass
        if path.is_dir():
            for child in path.iterdir():
                TestBarCacheEnvVar._restore_write_bits(child)

    @staticmethod
    def _write_minimal_jsonl(day_dir: Path, ticker: str) -> None:
        """Write a minimal one-bar JSONL fixture for a ticker into day_dir."""
        jsonl_path = day_dir / f"{ticker.upper()}.jsonl"
        bar = {
            "ts": "2026-01-06T14:30:00Z",
            "open": 100.0,
            "high": 101.0,
            "low": 99.5,
            "close": 100.5,
            "volume": 1000,
            "vw": 100.3,
            "n": 10,
            "session": "rth",
            "date": "2026-01-06",
        }
        jsonl_path.write_text(json.dumps(bar) + "\n", encoding="utf-8")

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

    # Test 3 -- v6.9.6: _build_ticker_cache and _ensure_cache must not write to bars_dir
    # when SSM_BAR_CACHE_DIR is set and bars_dir is read-only.
    #
    # This test was added in v6.9.6 specifically to catch the production failure where
    # _build_ticker_cache() still used bars_dir / '.cache_v2' directly instead of
    # routing through _cache_root(bars_dir).
    #
    # Real stack trace (Wave 2 worker, post-v6.9.5):
    #   File ".../backtest/bar_cache.py", line 263, in _build_ticker_cache
    #       ticker_dir.mkdir(parents=True, exist_ok=True)
    #   PermissionError: [Errno 13] Permission denied:
    #       '.../replay_layout/.cache_v2'
    #
    # On v6.9.5 main (commit 91618d9) this test PASSES because _cache_root() was
    # already wired into _build_ticker_cache and _ensure_cache.
    # On any version where the call sites use bars_dir / '.cache_v2' directly this
    # test FAILS with PermissionError.
    def test_build_and_ensure_with_readonly_bars_dir(self):
        """_build_ticker_cache and _ensure_cache must write to SSM_BAR_CACHE_DIR
        even when bars_dir is chmod 0o555 (read-only, no write permission)."""
        # Import here so the module is not loaded before the env var is set.
        from backtest.bar_cache import _build_ticker_cache, _ensure_cache, get_bars
        # Also clear the process-level verified cache between tests.
        from backtest import bar_cache as _bc
        _bc._CACHE_VERIFIED.clear()
        _bc._lru_read_bars.cache_clear()

        # 1. Create a writable bars_dir with one day subdir and one ticker JSONL.
        bars_dir = self.tmp / "readonly_bars"
        bars_dir.mkdir()
        day_dir = bars_dir / "2026-01-06"
        day_dir.mkdir()
        self._write_minimal_jsonl(day_dir, "TSLA")

        # 2. Set SSM_BAR_CACHE_DIR to a separate writable path.
        cache_dir = self.tmp / "cache_override"
        os.environ["SSM_BAR_CACHE_DIR"] = str(cache_dir)

        # 3. Lock down bars_dir and its subtree (read+exec only, no write).
        os.chmod(day_dir, 0o555)
        os.chmod(bars_dir, 0o555)

        # 4. Calling _build_ticker_cache must NOT raise PermissionError.
        try:
            _build_ticker_cache(bars_dir, "TSLA")
        except PermissionError as exc:
            self.fail(
                f"_build_ticker_cache raised PermissionError with SSM_BAR_CACHE_DIR set: {exc}\n"
                "This means _build_ticker_cache is still writing to bars_dir directly "
                "instead of routing through _cache_root(bars_dir). "
                "Fix: replace bars_dir / '.cache_v2' with _cache_root(bars_dir) at the "
                "_build_ticker_cache call site."
            )

        # 5. Cache files must have appeared under the override dir, NOT under bars_dir.
        tsla_cache_dir = cache_dir / "TSLA"
        self.assertTrue(
            tsla_cache_dir.is_dir(),
            f"Expected cache dir {tsla_cache_dir} to exist under SSM_BAR_CACHE_DIR "
            f"{cache_dir}, but it does not. "
            "Parquet files were not written to the override location."
        )
        parquets = list(tsla_cache_dir.glob("*.parquet"))
        self.assertTrue(
            len(parquets) >= 1,
            f"Expected at least one .parquet file under {tsla_cache_dir}, found none."
        )

        # 6. No .cache_v2 must exist under bars_dir (confirming no write to read-only dir).
        forbidden = bars_dir / _CACHE_DIR_NAME
        self.assertFalse(
            forbidden.exists(),
            f"Found {forbidden} -- _build_ticker_cache wrote cache into bars_dir "
            "instead of SSM_BAR_CACHE_DIR. This is the v6.9.5 bug."
        )

        # 7. _ensure_cache must also complete without PermissionError.
        _bc._CACHE_VERIFIED.clear()
        try:
            _ensure_cache(bars_dir, "TSLA")
        except PermissionError as exc:
            self.fail(
                f"_ensure_cache raised PermissionError with SSM_BAR_CACHE_DIR set: {exc}\n"
                "This means _ensure_cache (or a function it calls) still touches bars_dir."
            )

        # 8. get_bars must also return bars from the override cache without error.
        _bc._lru_read_bars.cache_clear()
        try:
            bars = get_bars(bars_dir, "TSLA", "2026-01-06")
        except PermissionError as exc:
            self.fail(
                f"get_bars raised PermissionError with SSM_BAR_CACHE_DIR set: {exc}"
            )
        self.assertIsInstance(bars, list, "get_bars must return a list")


if __name__ == "__main__":
    unittest.main()
