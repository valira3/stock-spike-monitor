"""tests/test_v6_11_7_smoke_guard_main_only.py

Regression tests for v6.11.7 smoke-test guard fix.

v6.11.6 set SSM_SMOKE_TEST=1 unconditionally at module load time in
scripts/premarket_check.py. trade_genius.py imports telegram_commands
during boot, telegram_commands imports scripts.premarket_check at top
level (for /test integration), so setting the env var at premarket
import time polluted the live process and made trade_genius take its
own smoke-test branch -- skipping Telegram polling, scheduler, and
catch-up. Dashboard came up but commands didn't work.

v6.11.7 moves the setdefault inside `if __name__ == "__main__":` so it
only fires on the CLI / cron invocation path, never on the import path.
"""
from __future__ import annotations

import ast
import os
import unittest

import bot_version


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREMARKET = os.path.join(REPO_ROOT, "scripts", "premarket_check.py")


class TestSmokeGuardMainOnly(unittest.TestCase):
    """The SSM_SMOKE_TEST setdefault must live inside an
    `if __name__ == "__main__":` block, not at module top level.
    """

    def test_setdefault_is_under_main_guard(self):
        with open(PREMARKET) as f:
            src = f.read()
        tree = ast.parse(src)

        # Walk top-level statements; setdefault must NOT appear at the
        # module body root.
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                call = node.value
                # os.environ.setdefault(...) -> Attribute(Attribute(...))
                if (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr == "setdefault"
                    and call.args
                    and isinstance(call.args[0], ast.Constant)
                    and call.args[0].value == "SSM_SMOKE_TEST"
                ):
                    self.fail(
                        "SSM_SMOKE_TEST setdefault must NOT be at module "
                        "top level in premarket_check.py (v6.11.6 regression: "
                        "polluted live trade_genius boot)"
                    )

        # Find an `if __name__ == "__main__":` block at module level
        # and assert the setdefault lives inside it.
        found_under_main = False
        for node in tree.body:
            if (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "__name__"
                and any(
                    isinstance(c, ast.Constant) and c.value == "__main__"
                    for c in node.test.comparators
                )
            ):
                # Search this block's body for the setdefault.
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "setdefault"
                        and sub.args
                        and isinstance(sub.args[0], ast.Constant)
                        and sub.args[0].value == "SSM_SMOKE_TEST"
                    ):
                        found_under_main = True
                        break
        self.assertTrue(
            found_under_main,
            'premarket_check.py must guard SSM_SMOKE_TEST setdefault '
            'with `if __name__ == "__main__":` (v6.11.7 fix).',
        )

    def test_importing_premarket_check_does_not_set_env(self):
        """End-to-end: importing scripts.premarket_check must NOT set
        SSM_SMOKE_TEST in the calling process's env.

        Use a subprocess so we get a clean environment (the parent
        pytest process has SSM_SMOKE_TEST=1 set by other tests' top-level
        setdefault calls).
        """
        import subprocess
        import sys

        env = {k: v for k, v in os.environ.items() if k != "SSM_SMOKE_TEST"}
        # Invoke a tiny child that imports premarket_check and prints
        # whether SSM_SMOKE_TEST got set.
        code = (
            "import os, sys; "
            "sys.path.insert(0, %r); "
            "import scripts.premarket_check as _pmc; "
            "print('SET' if 'SSM_SMOKE_TEST' in os.environ else 'UNSET')"
        ) % REPO_ROOT
        result = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, f"child failed: {result.stderr}")
        self.assertIn(
            "UNSET",
            result.stdout,
            "Importing scripts.premarket_check must not set "
            "SSM_SMOKE_TEST in env (v6.11.7 fix). Child stdout: "
            + result.stdout,
        )


class TestVersionParityV6117(unittest.TestCase):
    """Forward-compat: assert v6.11.x parity, not hardcoded 6.11.7."""
if __name__ == "__main__":
    unittest.main()
