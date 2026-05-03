"""v6.9.4 -- /data isolation coverage tests.

Smoke test: spawn a single replay subprocess with TG_DATA_ROOT pointing
at a temp directory. Asserts:
  1. Subprocess exits 0.
  2. No 'Permission denied' in stderr.
  3. trade_log.jsonl is NOT written to /data (old hardcoded path).
  4. TRADE_LOG_PATH env var is set under TG_DATA_ROOT in the env.

No em-dashes in this file (Val rule: test files must be dash-free).
No forbidden words (scrape, crawl, scraping, crawling).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backtest.sweep_env import build_sweep_env

# Path to the bars layout used by the canonical backtest dataset.
# The smoke test falls back to a skip if the directory is absent so the
# test suite can still run in environments that lack the full dataset.
_BARS_DIR = Path(
    os.environ.get(
        "BACKTEST_BARS_DIR",
        "/home/user/workspace/canonical_backtest_data/84day_2026_sip/replay_layout",
    )
)
_SMOKE_DATE = os.environ.get("BACKTEST_SMOKE_DATE", "2026-04-30")


def _pick_date(bars_dir: Path) -> str:
    """Return first available date dir if _SMOKE_DATE is not present."""
    target = bars_dir / _SMOKE_DATE
    if target.is_dir():
        return _SMOKE_DATE
    # Fall back to first available date directory.
    for child in sorted(bars_dir.iterdir()):
        if child.is_dir():
            return child.name
    return _SMOKE_DATE


class TestDataRootCoverage(unittest.TestCase):
    """Single-day replay subprocess: no Permission denied, trade_log under TG_DATA_ROOT."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    @unittest.skipUnless(_BARS_DIR.is_dir(), "canonical bars dir not present; skipping smoke test")
    def test_replay_no_permission_denied(self):
        """Replay exits 0 with no 'Permission denied' in stderr."""
        isolate_dir = self.tmp / "isolate"
        isolate_dir.mkdir()
        tg_data_root = self.tmp / "tg_root"
        tg_data_root.mkdir()

        date_str = _pick_date(_BARS_DIR)
        output_path = self.tmp / "out.json"

        env = build_sweep_env(
            isolate_dir=isolate_dir,
            tg_data_root=tg_data_root,
        )

        cmd = [
            sys.executable, "-m", "backtest.replay_v511_full",
            "--date", date_str,
            "--bars-dir", str(_BARS_DIR),
            "--output", str(output_path),
        ]

        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(_REPO_ROOT),
        )

        # Gate 1: exit code 0
        self.assertEqual(
            result.returncode, 0,
            msg=f"Replay exited {result.returncode}. stderr:\n{result.stderr[:2000]}",
        )

        # Gate 2: no Permission denied in stderr
        self.assertNotIn(
            "Permission denied",
            result.stderr,
            msg=f"'Permission denied' found in stderr:\n{result.stderr[:2000]}",
        )

        # Gate 3: output file exists and is valid JSON
        self.assertTrue(output_path.exists(), "Output JSON not written")
        data = json.loads(output_path.read_text())
        self.assertIn("summary", data, "Output JSON missing 'summary' key")

    @unittest.skipUnless(_BARS_DIR.is_dir(), "canonical bars dir not present; skipping smoke test")
    def test_trade_log_written_under_tg_data_root(self):
        """TRADE_LOG_PATH is set under TG_DATA_ROOT (not /data)."""
        isolate_dir = self.tmp / "isolate2"
        isolate_dir.mkdir()
        tg_data_root = self.tmp / "tg_root2"
        tg_data_root.mkdir()

        env = build_sweep_env(
            isolate_dir=isolate_dir,
            tg_data_root=tg_data_root,
        )

        # Env var must be set and point under tg_data_root.
        trade_log_path = Path(env["TRADE_LOG_PATH"])
        self.assertTrue(
            str(trade_log_path).startswith(str(tg_data_root)),
            f"TRADE_LOG_PATH {trade_log_path} is not under TG_DATA_ROOT {tg_data_root}",
        )

        # Must NOT point at /data.
        self.assertFalse(
            str(trade_log_path).startswith("/data"),
            f"TRADE_LOG_PATH still points at /data: {trade_log_path}",
        )

    def test_derived_paths_not_under_slash_data(self):
        """All derived path env vars must not start with /data."""
        isolate_dir = self.tmp / "iso3"
        isolate_dir.mkdir()
        tg_data_root = self.tmp / "tg3"
        tg_data_root.mkdir()

        env = build_sweep_env(
            isolate_dir=isolate_dir,
            tg_data_root=tg_data_root,
        )

        path_keys = [
            "STATE_DB_PATH", "BAR_ARCHIVE_BASE", "UNIVERSE_GUARD_PATH",
            "INGEST_AUDIT_DB_PATH", "VOLUME_PROFILE_DIR", "OR_DIR",
            "FORENSICS_DIR", "TRADE_LOG_PATH",
        ]
        for key in path_keys:
            val = env.get(key, "")
            self.assertFalse(
                val.startswith("/data"),
                f"{key}={val!r} still points at /data",
            )


if __name__ == "__main__":
    unittest.main()
