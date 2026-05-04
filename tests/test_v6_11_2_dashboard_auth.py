"""Regression tests for v6.11.2 dashboard auth fix.

The bug: dashboard `/login` sets the `spike_session` cookie with `Secure=True`,
which is correct for the public https deployment. The in-process system-test
check hits the local bind on plain `http://127.0.0.1:8080`, so
`http.cookiejar.CookieJar` refuses to forward the Secure cookie back to
`/api/state` -- which then 401s.

Fix: read `Set-Cookie` directly off the login response and forward
`spike_session=<value>` as an explicit `Cookie:` request header.

Tests run a tiny aiohttp server that mirrors the real /login + /api/state
contract with `secure=True` cookies, then exercise the v6.11.2 client paths
end-to-end on plain http://127.0.0.1:<port>.

Files exercised:
    trade_genius._check_dashboard
    trade_genius._systest_extract_session_cookie
    scripts.premarket_check.check_dashboard_state
    scripts.premarket_check._extract_session_cookie
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import unittest
from pathlib import Path

import pytest

# trade_genius's import-time env guards. Same shape as test_v6_7_1_system_test.
os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("FMP_API_KEY", "test_key_for_ci")
os.environ.setdefault("TELEGRAM_TOKEN", "123:test")
os.environ.setdefault("CHAT_ID", "999")

# Ensure repo root on path so we can import the production modules.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Pure-function tests (no server)
# ---------------------------------------------------------------------------


class TestExtractSessionCookieHelpers(unittest.TestCase):
    """The Set-Cookie -> name=value extractor on both sides."""

    def test_premarket_extract_session_cookie_typical(self):
        from scripts.premarket_check import _extract_session_cookie

        hdrs = [
            "spike_session=abc.def.ghi; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=604800",
        ]
        self.assertEqual(_extract_session_cookie(hdrs), "spike_session=abc.def.ghi")

    def test_premarket_extract_session_cookie_multiple_set_cookie(self):
        from scripts.premarket_check import _extract_session_cookie

        hdrs = [
            "other=foo; Path=/",
            "spike_session=xyz123; Path=/; HttpOnly; Secure",
        ]
        self.assertEqual(_extract_session_cookie(hdrs), "spike_session=xyz123")

    def test_premarket_extract_session_cookie_missing(self):
        from scripts.premarket_check import _extract_session_cookie

        hdrs = ["other=foo; Path=/"]
        self.assertEqual(_extract_session_cookie(hdrs), "")

    def test_premarket_extract_session_cookie_empty_input(self):
        from scripts.premarket_check import _extract_session_cookie

        self.assertEqual(_extract_session_cookie([]), "")
        self.assertEqual(_extract_session_cookie(None), "")

    def test_systest_extract_session_cookie_typical(self):
        # trade_genius is a giant module; only import inside the test so module
        # import errors don't poison the whole test class.
        import trade_genius

        hdrs = [
            "spike_session=abc.def.ghi; Path=/; HttpOnly; Secure; SameSite=Strict",
        ]
        self.assertEqual(
            trade_genius._systest_extract_session_cookie(hdrs),
            "spike_session=abc.def.ghi",
        )

    def test_systest_extract_session_cookie_missing(self):
        import trade_genius

        self.assertEqual(
            trade_genius._systest_extract_session_cookie(["other=foo; Path=/"]),
            "",
        )


# ---------------------------------------------------------------------------
# End-to-end test: real aiohttp server with Secure cookie
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _StubDashboardServer:
    """Minimal aiohttp server that mirrors the real /login + /api/state contract.

    Crucially sets the session cookie with secure=True, which is what
    triggered the Secure-flag stripping bug in production.
    """

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
        import asyncio

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
                "stub-token-abc",
                max_age=7 * 24 * 3600,
                httponly=True,
                samesite="Strict",
                secure=True,  # <-- key: matches production; triggers the bug
            )
            return resp

        async def h_state(request):
            cookie_hdr = request.headers.get("Cookie", "")
            if "spike_session=stub-token-abc" not in cookie_hdr:
                return web.json_response(
                    {"ok": False, "error": "unauthorized"}, status=401
                )
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
    """Context manager that owns a stub server + DASHBOARD_* env vars."""

    def __init__(self, payload):
        self.payload = payload
        self.password = "test-pw-correct-horse"
        self.server = _StubDashboardServer(self.password, payload)
        self._old_pw = None
        self._old_port = None
        self._had_pw = False
        self._had_port = False

    def __enter__(self):
        self.server.start()
        self._had_pw = "DASHBOARD_PASSWORD" in os.environ
        self._old_pw = os.environ.get("DASHBOARD_PASSWORD")
        self._had_port = "DASHBOARD_PORT" in os.environ
        self._old_port = os.environ.get("DASHBOARD_PORT")
        os.environ["DASHBOARD_PASSWORD"] = self.password
        os.environ["DASHBOARD_PORT"] = str(self.server.port)
        return self

    def __exit__(self, *exc):
        try:
            self.server.stop()
        finally:
            if self._had_pw:
                os.environ["DASHBOARD_PASSWORD"] = self._old_pw or ""
            else:
                os.environ.pop("DASHBOARD_PASSWORD", None)
            if self._had_port:
                os.environ["DASHBOARD_PORT"] = self._old_port or ""
            else:
                os.environ.pop("DASHBOARD_PORT", None)


def _aiohttp_available() -> bool:
    try:
        import aiohttp  # noqa: F401

        return True
    except Exception:
        return False


@unittest.skipUnless(_aiohttp_available(), "aiohttp required for stub server")
class TestPremarketDashboardCheckE2E(unittest.TestCase):
    """End-to-end on the premarket_check side (the easier import surface)."""

    @pytest.mark.slow
    def test_dashboard_check_passes_with_secure_cookie(self):
        from scripts import premarket_check

        # Use the actual BOT_VERSION_EXPECTED so version-parity passes.
        # v6.11.4: dashboard /api/state emits the field as `version`,
        # not `bot_version`. Both keys included for forward-compat.
        payload = {
            "version": premarket_check.BOT_VERSION_EXPECTED,
            "bot_version": premarket_check.BOT_VERSION_EXPECTED,
            "spy_regime_today": "B",
            "v611_window": {"enabled": True, "start_hhmm": "10:00", "end_hhmm": "11:00"},
            "ingest_status": {"status": "live"},
        }
        with _ServerEnv(payload):
            result = premarket_check.check_dashboard_state()
        self.assertEqual(
            result["status"], "PASS",
            "expected PASS, got %r (detail=%r)" % (result["status"], result.get("detail")),
        )

    @pytest.mark.slow
    def test_dashboard_check_warns_on_missing_v611_window(self):
        from scripts import premarket_check

        payload = {
            "version": premarket_check.BOT_VERSION_EXPECTED,
            "bot_version": premarket_check.BOT_VERSION_EXPECTED,
            "spy_regime_today": "A",
            # v611_window absent -> WARN per existing logic
            "ingest_status": {"status": "live"},
        }
        with _ServerEnv(payload):
            result = premarket_check.check_dashboard_state()
        self.assertEqual(result["status"], "WARN")
        self.assertIn("v611_window", result.get("detail", ""))

    def test_dashboard_check_skipped_without_password(self):
        from scripts import premarket_check

        old_pw = os.environ.pop("DASHBOARD_PASSWORD", None)
        try:
            result = premarket_check.check_dashboard_state()
            self.assertEqual(result["status"], "SKIP")
        finally:
            if old_pw is not None:
                os.environ["DASHBOARD_PASSWORD"] = old_pw


@unittest.skipUnless(_aiohttp_available(), "aiohttp required for stub server")
class TestSystestDashboardCheckE2E(unittest.TestCase):
    """End-to-end on the trade_genius._check_dashboard side."""

    @pytest.mark.slow
    def test_check_dashboard_ok_with_secure_cookie(self):
        # Defer trade_genius import: it's a 7000-line module.
        import trade_genius

        payload = {
            "bot_version": trade_genius.BOT_VERSION,
            "ingest_status": {"status": "live"},
        }
        with _ServerEnv(payload):
            cr = trade_genius._check_dashboard()
        self.assertEqual(cr.severity, "ok",
                         "expected ok, got %r (msg=%r)" % (cr.severity, cr.message))
        self.assertIn("shadow_data_status=live", cr.message)

    @pytest.mark.slow
    def test_check_dashboard_critical_on_wrong_password(self):
        import trade_genius

        payload = {"ingest_status": {"status": "live"}}
        with _ServerEnv(payload) as env:
            os.environ["DASHBOARD_PASSWORD"] = "wrong-password"
            cr = trade_genius._check_dashboard()
        self.assertEqual(cr.severity, "critical")
        self.assertIn("login failed", cr.message)


if __name__ == "__main__":
    unittest.main()
