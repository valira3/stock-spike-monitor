"""v6.9.3 -- sweep runner hardening tests.

Ten tests covering build_sweep_env() and preflight_smoke().

Rules enforced here:
- Zero em-dashes in test source (Val rule: test files must be dash-free).
- No forbidden action words (scrape, crawl, scraping, crawling).
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Ensure the repo root is importable when running directly.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backtest.sweep_env import REQUIRED_ENV, build_sweep_env, preflight_smoke


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dirs(tmp_path: Path, *names: str) -> tuple[Path, ...]:
    """Create and return multiple subdirectories under tmp_path."""
    dirs = []
    for name in names:
        d = tmp_path / name
        d.mkdir(parents=True, exist_ok=True)
        dirs.append(d)
    return tuple(dirs)


# ---------------------------------------------------------------------------
# Tests for build_sweep_env()
# ---------------------------------------------------------------------------

class TestBuildSweepEnv(unittest.TestCase):
    """Tests 1-4: build_sweep_env correctness and validation."""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    # Test 1 -- all REQUIRED_ENV keys present and correctly valued
    def test_required_env_keys_present(self):
        isolate_dir, tg_data_root = _make_dirs(self.tmp, "isolate", "data")
        env = build_sweep_env(isolate_dir=isolate_dir, tg_data_root=tg_data_root)
        for key, expected_value in REQUIRED_ENV.items():
            self.assertIn(key, env, f"Missing required key: {key}")
            self.assertEqual(
                env[key], expected_value,
                f"REQUIRED_ENV[{key!r}] should be {expected_value!r}, got {env[key]!r}",
            )

    # Test 2 -- raises ValueError when isolate_dir does not exist
    def test_raises_if_isolate_dir_missing(self):
        tg_data_root = self.tmp / "data"
        tg_data_root.mkdir()
        missing_isolate = self.tmp / "no_such_dir"
        with self.assertRaises(ValueError) as ctx:
            build_sweep_env(isolate_dir=missing_isolate, tg_data_root=tg_data_root)
        self.assertIn("isolate_dir", str(ctx.exception))

    # Test 3 -- raises ValueError when tg_data_root does not exist
    def test_raises_if_tg_data_root_missing(self):
        isolate_dir = self.tmp / "isolate"
        isolate_dir.mkdir()
        missing_root = self.tmp / "no_such_root"
        with self.assertRaises(ValueError) as ctx:
            build_sweep_env(isolate_dir=isolate_dir, tg_data_root=missing_root)
        self.assertIn("tg_data_root", str(ctx.exception))

    # Test 4 -- extra param overlays on top of base env
    def test_extra_overlays_on_top(self):
        isolate_dir, tg_data_root = _make_dirs(self.tmp, "isolate", "data")
        extra = {"STOP_PCT": "0.025", "MY_CUSTOM_FLAG": "hello"}
        env = build_sweep_env(
            isolate_dir=isolate_dir,
            tg_data_root=tg_data_root,
            extra=extra,
        )
        self.assertEqual(env["STOP_PCT"], "0.025")
        self.assertEqual(env["MY_CUSTOM_FLAG"], "hello")
        # REQUIRED_ENV keys must still be present (extra should not erase them
        # unless extra explicitly overrides one)
        for key, val in REQUIRED_ENV.items():
            if key not in extra:
                self.assertEqual(env[key], val)


# ---------------------------------------------------------------------------
# Tests for preflight_smoke()
# ---------------------------------------------------------------------------

class TestPreflightSmoke(unittest.TestCase):
    """Tests 5-10: preflight_smoke happy path and all failure modes."""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.workdir = self.tmp / "smoke_work"
        self.workdir.mkdir()
        self.bars_dir = self.tmp / "bars"
        self.bars_dir.mkdir()
        self.sample_date = "2026-04-28"
        self.env = {"PATH": os.environ.get("PATH", "")}

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write_output(self, content: dict | str) -> None:
        """Pre-write output JSON so the smoke checker finds it."""
        out = self.workdir / "smoke_check_output.json"
        if isinstance(content, dict):
            out.write_text(json.dumps(content))
        else:
            out.write_text(content)

    def _mock_run(self, returncode=0, stdout="", stderr=""):
        mock_result = MagicMock()
        mock_result.returncode = returncode
        mock_result.stdout = stdout
        mock_result.stderr = stderr
        return mock_result

    # Test 5 -- happy path: passes with valid output JSON
    def test_happy_path_passes(self):
        happy_payload = {"summary": {"entries": 3, "exits": 3, "pnl": 42.0}}
        self._write_output(happy_payload)
        mock_result = self._mock_run(returncode=0, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            # Should not raise
            preflight_smoke(
                workdir=self.workdir,
                bars_dir=self.bars_dir,
                sample_date=self.sample_date,
                env=self.env,
            )

    # Test 6 -- raises RuntimeError when returncode != 0
    def test_raises_on_nonzero_returncode(self):
        mock_result = self._mock_run(returncode=1, stderr="some error output")
        with patch("subprocess.run", return_value=mock_result):
            with self.assertRaises(RuntimeError) as ctx:
                preflight_smoke(
                    workdir=self.workdir,
                    bars_dir=self.bars_dir,
                    sample_date=self.sample_date,
                    env=self.env,
                )
        self.assertIn("returncode=1", str(ctx.exception))

    # Test 7 -- raises when output JSON missing or empty
    def test_raises_on_missing_output_json(self):
        mock_result = self._mock_run(returncode=0, stderr="")
        # Do not write output file
        with patch("subprocess.run", return_value=mock_result):
            with self.assertRaises(RuntimeError) as ctx:
                preflight_smoke(
                    workdir=self.workdir,
                    bars_dir=self.bars_dir,
                    sample_date=self.sample_date,
                    env=self.env,
                )
        self.assertIn("not found", str(ctx.exception).lower())

    def test_raises_on_empty_output_json(self):
        self._write_output("")
        mock_result = self._mock_run(returncode=0, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            with self.assertRaises(RuntimeError) as ctx:
                preflight_smoke(
                    workdir=self.workdir,
                    bars_dir=self.bars_dir,
                    sample_date=self.sample_date,
                    env=self.env,
                )
        self.assertIn("empty", str(ctx.exception).lower())

    # Test 8 -- raises when stderr contains "Traceback"
    def test_raises_on_traceback_in_stderr(self):
        happy_payload = {"summary": {"entries": 1, "exits": 1}}
        self._write_output(happy_payload)
        mock_result = self._mock_run(
            returncode=0,
            stderr="Traceback (most recent call last):\n  File ...",
        )
        with patch("subprocess.run", return_value=mock_result):
            with self.assertRaises(RuntimeError) as ctx:
                preflight_smoke(
                    workdir=self.workdir,
                    bars_dir=self.bars_dir,
                    sample_date=self.sample_date,
                    env=self.env,
                )
        self.assertIn("Traceback", str(ctx.exception))

    # Test 9 -- raises when stderr contains "Permission denied"
    def test_raises_on_permission_denied_in_stderr(self):
        happy_payload = {"summary": {"entries": 1, "exits": 1}}
        self._write_output(happy_payload)
        mock_result = self._mock_run(
            returncode=0,
            stderr="[Errno 13] Permission denied: '/data/bars'",
        )
        with patch("subprocess.run", return_value=mock_result):
            with self.assertRaises(RuntimeError) as ctx:
                preflight_smoke(
                    workdir=self.workdir,
                    bars_dir=self.bars_dir,
                    sample_date=self.sample_date,
                    env=self.env,
                )
        self.assertIn("Permission denied", str(ctx.exception))

    # Test 10 -- raises when summary missing 'entries' key
    def test_raises_when_summary_missing_entries(self):
        bad_payload = {"summary": {"exits": 5, "pnl": 100.0}}
        self._write_output(bad_payload)
        mock_result = self._mock_run(returncode=0, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            with self.assertRaises(RuntimeError) as ctx:
                preflight_smoke(
                    workdir=self.workdir,
                    bars_dir=self.bars_dir,
                    sample_date=self.sample_date,
                    env=self.env,
                )
        self.assertIn("entries", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Tests for v6.9.4 derived path env vars (Tests 11-17)
# ---------------------------------------------------------------------------

class TestDerivedPathEnvVars(unittest.TestCase):
    """v6.9.4 -- verify all derived path vars are set and directories created."""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _build(self, **kw):
        isolate_dir = self.tmp / "iso"
        isolate_dir.mkdir(parents=True, exist_ok=True)
        tg_data_root = self.tmp / "root"
        tg_data_root.mkdir(parents=True, exist_ok=True)
        return build_sweep_env(isolate_dir=isolate_dir, tg_data_root=tg_data_root, **kw)

    # Test 11 -- STATE_DB_PATH derived correctly
    def test_state_db_path_derived(self):
        env = self._build()
        root = self.tmp / "root"
        self.assertEqual(env["STATE_DB_PATH"], str(root / "state.db"))

    # Test 12 -- BAR_ARCHIVE_BASE derived correctly
    def test_bar_archive_base_derived(self):
        env = self._build()
        root = self.tmp / "root"
        self.assertEqual(env["BAR_ARCHIVE_BASE"], str(root / "bars"))

    # Test 13 -- UNIVERSE_GUARD_PATH derived correctly
    def test_universe_guard_path_derived(self):
        env = self._build()
        root = self.tmp / "root"
        self.assertEqual(env["UNIVERSE_GUARD_PATH"], str(root / "tickers.json"))

    # Test 14 -- TRADE_LOG_PATH derived correctly
    def test_trade_log_path_derived(self):
        env = self._build()
        root = self.tmp / "root"
        self.assertEqual(env["TRADE_LOG_PATH"], str(root / "trade_log.jsonl"))

    # Test 15 -- OR_DIR and FORENSICS_DIR derived correctly
    def test_or_dir_forensics_dir_derived(self):
        env = self._build()
        root = self.tmp / "root"
        self.assertEqual(env["OR_DIR"], str(root / "or"))
        self.assertEqual(env["FORENSICS_DIR"], str(root / "forensics"))

    # Test 16 -- directory-type paths are created on disk
    def test_directory_paths_created_on_disk(self):
        env = self._build()
        for key in ("BAR_ARCHIVE_BASE", "VOLUME_PROFILE_DIR", "OR_DIR", "FORENSICS_DIR"):
            p = Path(env[key])
            self.assertTrue(p.is_dir(), f"{key} dir not created: {p}")

    # Test 17 -- file-type path parents are created on disk
    def test_file_path_parents_created_on_disk(self):
        env = self._build()
        for key in ("STATE_DB_PATH", "UNIVERSE_GUARD_PATH", "TRADE_LOG_PATH", "INGEST_AUDIT_DB_PATH"):
            p = Path(env[key])
            self.assertTrue(p.parent.is_dir(), f"{key} parent dir not created: {p.parent}")
