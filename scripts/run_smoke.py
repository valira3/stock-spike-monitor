"""scripts/run_smoke.py -- local post-deploy smoke test runner.

Replaces .github/workflows/post-deploy-smoke.yml for local execution.
Runs after a push to main: waits for Railway to roll out the new
BOT_VERSION, then runs the full smoke suite (local + prod).

Usage:
    python scripts/run_smoke.py                # wait for deploy + run both
    python scripts/run_smoke.py --local-only   # skip Railway wait + prod tests
    python scripts/run_smoke.py --no-wait      # skip Railway wait, run both
    python scripts/run_smoke.py --timeout 600  # extend Railway wait (default 300s)

Reads credentials from .env.monitor (same file as run_monitor.py).
Sends @tgval3_bot Telegram alert on failure.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("run_smoke")

_REPO_ROOT = Path(__file__).parent.parent
ENV_FILE = _REPO_ROOT / ".env.monitor"
DASHBOARD_URL = "https://tradegenius.up.railway.app"


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    loaded = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val
            loaded += 1
    if loaded:
        logger.info("Loaded %d var(s) from %s", loaded, path.name)


def _committed_version() -> str:
    """Read BOT_VERSION from the checked-in bot_version.py."""
    bvp = _REPO_ROOT / "bot_version.py"
    m = re.search(r'^BOT_VERSION\s*=\s*"([^"]+)"', bvp.read_text(encoding="utf-8"), re.M)
    if not m:
        raise RuntimeError("BOT_VERSION not found in bot_version.py")
    return m.group(1)


def _live_version(url: str, timeout: float = 10.0) -> str | None:
    try:
        req = urllib.request.Request(f"{url}/api/version")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read()).get("version")
    except Exception:
        return None


def wait_for_deploy(expected: str, timeout_s: int = 300) -> bool:
    """Poll /api/version until it matches expected. Returns True on match."""
    logger.info("Waiting for Railway to deploy v%s (up to %ds)...", expected, timeout_s)
    deadline = time.time() + timeout_s
    poll = 0
    while time.time() < deadline:
        poll += 1
        live = _live_version(DASHBOARD_URL)
        logger.info("  poll %d: live=%s want=%s", poll, live, expected)
        if live == expected:
            logger.info("Railway is live on v%s", expected)
            return True
        time.sleep(10)
    logger.error("Timeout: Railway never reported v%s (last seen: %s)", expected, live)
    return False


def run_smoke(args: list[str]) -> int:
    """Run smoke_test.py with given args. Returns exit code."""
    cmd = [sys.executable, str(_REPO_ROOT / "smoke_test.py")] + args
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(cmd, cwd=str(_REPO_ROOT), env=env)
    return result.returncode


def send_telegram_alert(version: str, failures: str) -> None:
    token = os.environ.get("TELEGRAM_TP_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_TP_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.warning("Telegram creds not set -- skipping alert")
        return
    msg = (
        f"POST-DEPLOY SMOKE FAILED\n"
        f"Version: {version}\n"
        f"{failures}\n"
        f"Run: python scripts/run_smoke.py"
    )
    payload = json.dumps({"chat_id": chat_id, "text": msg}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            logger.info("Telegram alert sent (HTTP %d)", r.status)
    except urllib.error.HTTPError as e:
        logger.warning("Telegram alert failed: HTTP %d", e.code)
    except Exception as e:
        logger.warning("Telegram alert error: %s", e)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-only", action="store_true",
                        help="Run local smoke tests only (no Railway wait or prod tests)")
    parser.add_argument("--no-wait", action="store_true",
                        help="Skip Railway version wait, run both local + prod")
    parser.add_argument("--timeout", type=int, default=300,
                        help="Railway rollout wait timeout in seconds (default 300)")
    parser.add_argument("--version", default=None,
                        help="Expected version (default: read from bot_version.py)")
    args = parser.parse_args()

    _load_env(ENV_FILE)

    expected = args.version or _committed_version()
    logger.info("Expected version: %s", expected)

    password = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if not password:
        logger.warning(".env.monitor has no DASHBOARD_PASSWORD -- prod tests will fail auth")

    # Step 1: wait for Railway rollout
    if not args.local_only and not args.no_wait:
        if not wait_for_deploy(expected, args.timeout):
            send_telegram_alert(expected, "(Railway never deployed the new version)")
            return 1
        time.sleep(5)  # let Railway settle

    # Step 2: local smoke tests (31)
    logger.info("=== LOCAL smoke tests ===")
    local_rc = run_smoke(["--local"])
    if local_rc != 0:
        logger.error("LOCAL smoke FAILED (exit %d)", local_rc)

    if args.local_only:
        return local_rc

    # Step 3: prod smoke tests (9)
    logger.info("=== PROD smoke tests ===")
    prod_args = ["--prod", "--url", DASHBOARD_URL, "--expected-version", expected]
    if password:
        prod_args += ["--password", password]
    prod_rc = run_smoke(prod_args)
    if prod_rc != 0:
        logger.error("PROD smoke FAILED (exit %d)", prod_rc)

    overall_rc = max(local_rc, prod_rc)
    if overall_rc != 0:
        send_telegram_alert(expected, f"local_rc={local_rc} prod_rc={prod_rc}")
    else:
        logger.info("All smoke tests PASSED for v%s", expected)

    return overall_rc


if __name__ == "__main__":
    sys.exit(main())
