"""v6.11.3 regression: Dockerfile must COPY scripts/ into the container.

Why this exists
---------------
v6.11.1 shipped the pre-market readiness check at scripts/premarket_check.py
along with a 04:30 ET cron that invokes it via railway ssh. The Dockerfile
was not updated to COPY the new scripts/ directory, so the production image
ships without the file:

    $ ls /app/scripts
    ls: cannot access /app/scripts: No such file or directory

Telegram /test silently swallows the ImportError (the import is wrapped in
try/except at module top), so the bug was invisible until the cron tried
to fire. The cron would have failed every morning at 04:30 ET.

This test mirrors the existing test_startup_smoke.py Dockerfile-COPY
contract pattern (the v5.10.3 startup-smoke check that prevents top-level
imports drifting away from COPY directives).
"""

from __future__ import annotations

import re
from pathlib import Path
import unittest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _read_dockerfile() -> str:
    return (_REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")


class TestDockerfileScriptsCopy(unittest.TestCase):
    """The Dockerfile must include a COPY directive that ships scripts/."""

    def test_dockerfile_copies_scripts_directory(self):
        df = _read_dockerfile()
        # Accept either `COPY scripts/ ./scripts/` or `COPY scripts ./scripts`,
        # case-sensitive (Docker is case-sensitive).
        pattern = re.compile(
            r"^COPY\s+scripts/?\s+(?:\./scripts/?|/app/scripts/?)\s*$",
            re.MULTILINE,
        )
        self.assertRegex(
            df,
            pattern,
            "Dockerfile must COPY the scripts/ directory into the image. "
            "Without it, /app/scripts/premarket_check.py is missing and the "
            "04:30 ET pre-market cron fails with FileNotFoundError.",
        )

    def test_premarket_check_script_exists_in_repo(self):
        # If the file disappears from the repo, the COPY would still pass but
        # the cron would still break -- catch that case too.
        path = _REPO_ROOT / "scripts" / "premarket_check.py"
        self.assertTrue(
            path.is_file(),
            "scripts/premarket_check.py is missing from the repo. The 04:30 "
            "ET cron and Telegram /test depend on it.",
        )

    def test_scripts_init_exists_for_package_import(self):
        # telegram_commands.py imports `from scripts.premarket_check import ...`
        # which only works if scripts/ is a real package. Without __init__.py
        # the import would fail at runtime even with the COPY.
        path = _REPO_ROOT / "scripts" / "__init__.py"
        self.assertTrue(
            path.is_file(),
            "scripts/__init__.py is missing. telegram_commands imports "
            "scripts.premarket_check as a package; without __init__.py the "
            "import fails on Python interpreters that disallow implicit "
            "namespace packages in this configuration.",
        )


if __name__ == "__main__":
    unittest.main()
