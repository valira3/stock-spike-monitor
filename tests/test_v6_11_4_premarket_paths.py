"""Regression tests for v6.11.4 premarket_check.py fixes.

Three independent bugs were exposed on the first live cron run after v6.11.3
shipped scripts/ into the container:

1. ModuleNotFoundError: when run as `python3 /app/scripts/premarket_check.py`,
   sys.path[0] is /app/scripts/, so cross-package imports of root-level
   modules (bot_version, spy_regime, broker.orders) all fail.
   Fix: insert /app at sys.path[0] when the directory exists.

2. dashboard_state always WARN with bot_version='': dashboard /api/state
   emits the version field as `version`, not `bot_version`.
   Fix: read data.get("version", "").

3. time_sync always WARN with HTTP 405: Alpaca /v2/clock rejects HEAD.
   Fix: switch to GET.

These tests are pure (no external network, no actual /app filesystem)
and exercise the fixes in-source where practical.
"""

from __future__ import annotations

import os
import re
import socket
import sys
import threading
import unittest
from pathlib import Path

# Match the import-time env shape of test_v6_11_2_dashboard_auth.
os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("FMP_API_KEY", "test_key_for_ci")
os.environ.setdefault("TELEGRAM_TOKEN", "123:test")
os.environ.setdefault("CHAT_ID", "999")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Bug 1: sys.path injection so cross-package imports work in-container
# ---------------------------------------------------------------------------


class TestSysPathAppRootGuard(unittest.TestCase):
    """The script must add /app to sys.path early, before any cross-package
    import would be evaluated. We assert this at the source level (so we
    don't have to fake /app on the test runner's filesystem)."""

    def setUp(self):
        self.src = (_REPO_ROOT / "scripts" / "premarket_check.py").read_text()

    def test_sys_path_insert_app_root_present(self):
        # The guard must use sys.path.insert at index 0 (not append) and
        # target /app -- either as a literal string or via a constant whose
        # value is the literal /app. We check both forms.
        # Either:  sys.path.insert(0, "/app")
        # Or:      sys.path.insert(0, _TG_APP_ROOT)  where _TG_APP_ROOT = "/app"
        literal_form = re.search(
            r'sys\.path\.insert\s*\(\s*0\s*,\s*["\']/app["\']\s*\)', self.src,
        )
        constant_form = re.search(
            r'sys\.path\.insert\s*\(\s*0\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)',
            self.src,
        )
        if constant_form is not None:
            const_name = constant_form.group(1)
            const_def = re.search(
                r'^' + re.escape(const_name) + r'\s*=\s*["\']/app["\']\s*$',
                self.src, re.MULTILINE,
            )
            self.assertTrue(
                literal_form or const_def,
                "sys.path.insert target is %r but no `%s = \"/app\"` definition found"
                % (const_name, const_name),
            )
        else:
            self.assertIsNotNone(
                literal_form,
                "expected sys.path.insert(0, \"/app\") or via constant",
            )

    def test_sys_path_guard_is_idempotent(self):
        # The guard must check that /app is not already on sys.path before
        # inserting. Otherwise repeated invocations (e.g. from /test) keep
        # piling duplicate entries on.
        self.assertIn(
            "not in sys.path",
            self.src,
            "expected idempotency check (`/app not in sys.path`)",
        )

    def test_sys_path_guard_runs_before_cross_package_imports(self):
        # The guard MUST appear in the file before the first cross-package
        # import that would otherwise fail. We verify this by ensuring the
        # guard's source location is earlier than any of the fragile imports
        # we know exist further down the file.
        m_guard = re.search(
            r'sys\.path\.insert\s*\(\s*0\s*,\s*(?:_TG_APP_ROOT|[\"\']/app[\"\'])\s*\)',
            self.src,
        )
        self.assertIsNotNone(m_guard, "sys.path.insert guard not found")
        guard_idx = m_guard.start()
        # Match the actual import STATEMENT (indented) -- not docstring text.
        m_import = re.search(
            r'^\s+import bot_version as ', self.src, re.MULTILINE
        )
        self.assertIsNotNone(
            m_import,
            "expected real `import bot_version as ...` statement in script",
        )
        self.assertLess(
            guard_idx, m_import.start(),
            "sys.path guard must run before the first `import bot_version as` statement",
        )

    def test_sys_path_guard_existence_check_on_dir(self):
        # /app may not exist on dev boxes; the guard must be a no-op there.
        self.assertIn("os.path.isdir", self.src)


# ---------------------------------------------------------------------------
# Bug 2 + Bug 3 e2e: real aiohttp stub server (mirrors v6.11.2 pattern)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _StubDashboardServer:
    """Mirror of the v6.11.2 stub but with arbitrary payload override."""

    def __init__(self, password: str, payload: dict):
        self.password = password
        self.payload = payload
        self.port = _free_port()
        self._loop = None
        self._runner = None
        self._site = None
        self._thread = None
        self._ready = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("stub dashboard failed to start within 10s")

    def stop(self):
        import asyncio

        if self._loop is None:
            return
        fut = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        try:
            fut.result(timeout=5)
        except Exception:
            pass

    async def _shutdown(self):
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()
        self._loop.stop()

    def _run(self):
        import asyncio

        from aiohttp import web

        async def h_login(request):
            data = await request.post()
            pw = (data.get("password") or "").strip()
            if pw != self.password:
                return web.Response(status=401, text="bad password")
            resp = web.HTTPFound("/")
            resp.set_cookie(
                "spike_session",
                "stub-token-v6114",
                max_age=7 * 24 * 3600,
                httponly=True,
                samesite="Strict",
                secure=True,
            )
            return resp

        async def h_state(request):
            cookie_hdr = request.headers.get("Cookie", "")
            if "spike_session=stub-token-v6114" not in cookie_hdr:
                return web.json_response({"ok": False}, status=401)
            return web.json_response(self.payload)

        async def h_root(request):
            return web.Response(text="ok")

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        app = web.Application()
        app.router.add_post("/login", h_login)
        app.router.add_get("/api/state", h_state)
        app.router.add_get("/", h_root)
        self._runner = web.AppRunner(app)

        async def _start():
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, "127.0.0.1", self.port)
            await self._site.start()
            self._ready.set()

        self._loop.create_task(_start())
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except Exception:
                pass


class _ServerEnv:
    def __init__(self, payload):
        self.payload = payload
        self.password = "test-pw-v6114"
        self.server = _StubDashboardServer(self.password, payload)

    def __enter__(self):
        self.server.start()
        self._old_pw = os.environ.get("DASHBOARD_PASSWORD")
        self._old_port = os.environ.get("DASHBOARD_PORT")
        os.environ["DASHBOARD_PASSWORD"] = self.password
        os.environ["DASHBOARD_PORT"] = str(self.server.port)
        return self

    def __exit__(self, *exc):
        try:
            self.server.stop()
        finally:
            if self._old_pw is None:
                os.environ.pop("DASHBOARD_PASSWORD", None)
            else:
                os.environ["DASHBOARD_PASSWORD"] = self._old_pw
            if self._old_port is None:
                os.environ.pop("DASHBOARD_PORT", None)
            else:
                os.environ["DASHBOARD_PORT"] = self._old_port


def _aiohttp_available() -> bool:
    try:
        import aiohttp  # noqa: F401

        return True
    except Exception:
        return False


@unittest.skipUnless(_aiohttp_available(), "aiohttp required for stub server")
class TestDashboardVersionKey(unittest.TestCase):
    """Bug 2: read `version` (not `bot_version`) from /api/state."""

    def test_dashboard_passes_when_version_key_matches(self):
        from scripts import premarket_check

        payload = {
            # ONLY `version` -- no `bot_version` -- proves the script reads
            # the right key now.
            "version": premarket_check.BOT_VERSION_EXPECTED,
            "spy_regime_today": "B",
            "v611_window": {"arm": "10:00", "disarm": "11:00"},
        }
        with _ServerEnv(payload):
            result = premarket_check.check_dashboard_state()
        self.assertEqual(
            result["status"], "PASS",
            "expected PASS, got %r (detail=%r)" % (result["status"], result.get("detail")),
        )
        # And the data block carries the version through:
        self.assertEqual(
            result["data"].get("bot_version"),
            premarket_check.BOT_VERSION_EXPECTED,
        )

    def test_dashboard_warns_when_only_legacy_bot_version_key_present(self):
        # This is the scenario that produced the v6.11.3 false alarm:
        # dashboard ONLY emits `bot_version`, no `version`. After v6.11.4
        # we read `version`, so this should now WARN with empty version.
        from scripts import premarket_check

        payload = {
            "bot_version": premarket_check.BOT_VERSION_EXPECTED,
            "spy_regime_today": "B",
            "v611_window": {"arm": "10:00", "disarm": "11:00"},
        }
        with _ServerEnv(payload):
            result = premarket_check.check_dashboard_state()
        self.assertEqual(result["status"], "WARN")
        # detail mentions version (not bot_version) per the v6.11.4 wording.
        self.assertIn("version=", result.get("detail", ""))


# ---------------------------------------------------------------------------
# Bug 3: Alpaca /v2/clock must use GET (not HEAD)
# ---------------------------------------------------------------------------


class _MethodCaptureAlpacaServer:
    """Tiny stub that records the HTTP method used to hit /v2/clock."""

    def __init__(self):
        self.port = _free_port()
        self.captured_methods = []
        self._loop = None
        self._runner = None
        self._site = None
        self._ready = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("alpaca stub failed to start within 10s")

    def stop(self):
        import asyncio

        if self._loop is None:
            return
        fut = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        try:
            fut.result(timeout=5)
        except Exception:
            pass

    async def _shutdown(self):
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()
        self._loop.stop()

    def _run(self):
        import asyncio

        from aiohttp import web

        async def h_clock(request):
            self.captured_methods.append(request.method)
            if request.method == "HEAD":
                return web.Response(status=405, text="Method Not Allowed")
            # Must include a Date header for the script's parser.
            return web.json_response(
                {
                    "timestamp": "2026-05-04T08:30:00Z",
                    "is_open": False,
                    "next_open": "2026-05-04T13:30:00Z",
                    "next_close": "2026-05-04T20:00:00Z",
                },
                headers={"Date": "Mon, 04 May 2026 08:30:00 GMT"},
            )

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        app = web.Application()
        app.router.add_route("*", "/v2/clock", h_clock)
        self._runner = web.AppRunner(app)

        async def _start():
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, "127.0.0.1", self.port)
            await self._site.start()
            self._ready.set()

        self._loop.create_task(_start())
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except Exception:
                pass


@unittest.skipUnless(_aiohttp_available(), "aiohttp required for stub server")
class TestAlpacaClockUsesGet(unittest.TestCase):
    """Bug 3: must use GET against /v2/clock; HEAD returns 405."""

    def test_check_time_sync_issues_get(self):
        from scripts import premarket_check

        srv = _MethodCaptureAlpacaServer()
        srv.start()
        try:
            old_url = premarket_check._ALPACA_CLOCK_URL
            old_key = os.environ.get("VAL_ALPACA_PAPER_KEY")
            old_secret = os.environ.get("VAL_ALPACA_PAPER_SECRET")
            try:
                premarket_check._ALPACA_CLOCK_URL = (
                    "http://127.0.0.1:%d/v2/clock" % srv.port
                )
                os.environ["VAL_ALPACA_PAPER_KEY"] = "stub-key"
                os.environ["VAL_ALPACA_PAPER_SECRET"] = "stub-secret"
                result = premarket_check.check_time_sync()
            finally:
                premarket_check._ALPACA_CLOCK_URL = old_url
                if old_key is None:
                    os.environ.pop("VAL_ALPACA_PAPER_KEY", None)
                else:
                    os.environ["VAL_ALPACA_PAPER_KEY"] = old_key
                if old_secret is None:
                    os.environ.pop("VAL_ALPACA_PAPER_SECRET", None)
                else:
                    os.environ["VAL_ALPACA_PAPER_SECRET"] = old_secret
        finally:
            srv.stop()

        # Captured at least one request and NONE of them used HEAD.
        self.assertTrue(srv.captured_methods, "no requests captured")
        self.assertNotIn(
            "HEAD", srv.captured_methods,
            "premarket_check still uses HEAD against /v2/clock (should be GET)",
        )
        self.assertIn("GET", srv.captured_methods)
        # And the result is not a 405-flavored WARN.
        self.assertNotIn("405", result.get("detail", ""))


# ---------------------------------------------------------------------------
# Source-level guard: the v6.11.4 BOT_VERSION_EXPECTED bumped correctly
# ---------------------------------------------------------------------------


class TestVersionExpectedBump(unittest.TestCase):
    def test_bot_version_expected_is_6_11_4(self):
        from scripts import premarket_check

        # v6.11.5 bumped this in lockstep with bot_version.py.
        # Forward-compat guard: must equal bot_version.BOT_VERSION.
        import bot_version as _bv
        self.assertEqual(
            premarket_check.BOT_VERSION_EXPECTED, _bv.BOT_VERSION,
        )

    def test_bot_version_py_matches(self):
        import bot_version as bv

        # v6.11.5 bumped this. Treat as forward-compat: just assert BOT_VERSION
        # follows the 6.11.X family (any patch >= 4 is acceptable).
        self.assertTrue(
            bv.BOT_VERSION.startswith("6.11."),
            "BOT_VERSION must remain in 6.11.X family, got %r" % bv.BOT_VERSION,
        )


if __name__ == "__main__":
    unittest.main()
