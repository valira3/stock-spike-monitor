"""tools.dashboard_monitor -- RTH live dashboard validator.

Periodically (every 5 min during US RTH) hits the live production
dashboard's read endpoints, runs a battery of invariants against the
responses, and on any violation:

  * Posts a structured alert to the configured Telegram channel.
  * Files a GitHub issue with full diagnostic context (observed vs
    expected, repro steps, raw payloads), tagged with a @claude
    mention so the Claude Code GitHub app can auto-draft a fix PR.

The monitor itself is read-only against production -- it never writes
back to the bot. It mints its own session cookie via the same
HMAC_SHA256 scheme dashboard_server uses for browser sessions, given
the shared DASHBOARD_SESSION_SECRET (stored as a GHA secret).

Usage:
    DASHBOARD_BASE_URL=https://tradegenius.up.railway.app \\
    DASHBOARD_SESSION_SECRET=<hex32> \\
    TELEGRAM_BOT_TOKEN=<token> \\
    TELEGRAM_ADMIN_CHAT_ID=<chat-id> \\
    GH_TOKEN=<pat-with-issues:write> \\
    GH_REPO=valira3/stock-spike-monitor \\
    python -m tools.dashboard_monitor

Env vars (all required except where noted):
    DASHBOARD_BASE_URL          e.g. https://tradegenius.up.railway.app
    DASHBOARD_SESSION_SECRET    hex; same value the live bot uses
    TELEGRAM_BOT_TOKEN          existing bot token
    TELEGRAM_ADMIN_CHAT_ID      chat-id to post alerts (numeric or @handle)
    GH_TOKEN                    PAT or workflow GITHUB_TOKEN with issues:write
    GH_REPO                     owner/repo, e.g. valira3/stock-spike-monitor
    MONITOR_DRY_RUN             optional; "1" disables Telegram + GH issue side
                                effects (prints diagnostics only). Useful for
                                local debugging.
    MONITOR_LABEL               optional issue label (default: "dashboard-monitor")

Exit codes:
    0  all invariants passed (or violations were filed successfully)
    1  hard failure (couldn't reach dashboard, secret missing, etc.)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import struct
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

from tools.dashboard_monitor_invariants import INVARIANTS, InvariantContext

logger = logging.getLogger("dashboard_monitor")


# ---------------------------------------------------------------------------
# Session cookie minting (mirrors dashboard_server._check_auth + _mint_session)
# ---------------------------------------------------------------------------

SESSION_COOKIE_NAME = "spike_session"


def mint_session_cookie(secret_hex: str) -> str:
    """Build a fresh HMAC-signed session cookie value.

    Matches dashboard_server._mint_session:
        cookie = hex(HMAC_SHA256(secret, big-endian uint64 timestamp)) + ":" + ts
    """
    try:
        secret = bytes.fromhex(secret_hex.strip())
    except ValueError as e:
        raise RuntimeError(f"DASHBOARD_SESSION_SECRET is not valid hex: {e}")
    if len(secret) < 32:
        raise RuntimeError(
            f"DASHBOARD_SESSION_SECRET too short ({len(secret)} bytes, need >= 32)"
        )
    ts = int(time.time())
    sig = hmac.new(secret, struct.pack(">Q", ts), hashlib.sha256).hexdigest()
    return f"{sig}:{ts}"


# ---------------------------------------------------------------------------
# Dashboard HTTP client
# ---------------------------------------------------------------------------


class DashboardClient:
    def __init__(self, base_url: str, session_cookie: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.cookie = f"{SESSION_COOKIE_NAME}={session_cookie}"
        self.timeout = timeout

    def get_json(self, path: str) -> dict:
        url = self.base_url + path
        req = urllib.request.Request(url, headers={"Cookie": self.cookie})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status} on {path}")
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            raise RuntimeError(f"GET {path} failed: {e}")


# ---------------------------------------------------------------------------
# Alert sinks
# ---------------------------------------------------------------------------


def send_telegram(token: str, chat_id: str, text: str, dry_run: bool = False) -> bool:
    if dry_run:
        print(f"[dry-run] TG -> {chat_id}: {text[:200]}...")
        return True
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": "Markdown",
         "disable_web_page_preview": "true"}
    ).encode()
    req = urllib.request.Request(url, data=payload)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)
        return False


def file_github_issue(
    gh_token: str,
    gh_repo: str,
    title: str,
    body: str,
    labels: list[str],
    dry_run: bool = False,
) -> str | None:
    if dry_run:
        print(f"[dry-run] GH issue -> {gh_repo}: {title}\n{body[:400]}...")
        return None
    url = f"https://api.github.com/repos/{gh_repo}/issues"
    payload = json.dumps({"title": title, "body": body, "labels": labels}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status not in (200, 201):
                logger.warning("GH issue create returned %d", r.status)
                return None
            data = json.loads(r.read().decode("utf-8"))
            return data.get("html_url")
    except Exception as e:
        logger.warning("GH issue create failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"FATAL: env var {name} is required", file=sys.stderr)
        sys.exit(1)
    return v


def _format_violation_telegram(violations: list[dict]) -> str:
    lines = [f"⚠️ *Dashboard monitor: {len(violations)} violation(s)*", ""]
    for v in violations[:10]:
        lines.append(f"• *{v['name']}*: {v['summary'][:140]}")
    if len(violations) > 10:
        lines.append(f"... +{len(violations) - 10} more")
    return "\n".join(lines)


def _format_violation_issue(
    violations: list[dict], ts_iso: str, base_url: str
) -> tuple[str, str]:
    title = (
        f"[dashboard-monitor] {len(violations)} invariant violation(s) "
        f"at {ts_iso}"
    )
    body_lines = [
        "Automated detection from the RTH live-dashboard monitor",
        f"(`tools/dashboard_monitor.py`). Hit `{base_url}` and ran"
        f" {len(violations)} invariant violation(s) against the response.",
        "",
        "## Violations",
        "",
    ]
    for v in violations:
        body_lines.append(f"### {v['name']}")
        body_lines.append("")
        body_lines.append(v["summary"])
        body_lines.append("")
        if v.get("detail"):
            body_lines.append("```")
            body_lines.append(v["detail"][:2000])
            body_lines.append("```")
            body_lines.append("")
    body_lines.extend(
        [
            "## Repro",
            "",
            "```bash",
            "# Re-run the monitor locally against production:",
            f"DASHBOARD_BASE_URL={base_url} \\",
            "DASHBOARD_SESSION_SECRET=<hex> \\",
            "MONITOR_DRY_RUN=1 \\",
            "python -m tools.dashboard_monitor",
            "```",
            "",
            "## Auto-fix",
            "",
            "@claude please investigate the violation(s) above and open a"
            " **draft** PR with a proposed fix. Do not auto-merge -- the"
            " operator reviews every monitor-triggered change before it"
            " ships.",
        ]
    )
    return title, "\n".join(body_lines)


def run_once() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    base_url = _require_env("DASHBOARD_BASE_URL")
    secret = _require_env("DASHBOARD_SESSION_SECRET")
    dry_run = os.environ.get("MONITOR_DRY_RUN", "").strip() == "1"

    try:
        cookie = mint_session_cookie(secret)
    except RuntimeError as e:
        logger.error("Session secret invalid: %s", e)
        return 1

    client = DashboardClient(base_url, cookie)

    # Fetch the four core endpoints.
    payloads: dict[str, Any] = {}
    for name, path in [
        ("state", "/api/state"),
        ("exec_val", "/api/executor/val"),
        ("exec_gene", "/api/executor/gene"),
        ("v10_proj", "/api/v10/projection"),
    ]:
        try:
            payloads[name] = client.get_json(path)
            logger.info("fetched %s ok (%d bytes)", path, len(json.dumps(payloads[name])))
        except RuntimeError as e:
            logger.error("fetch %s failed: %s", path, e)
            # Treat a fetch failure as a violation too -- something
            # serious is wrong with production if /api/state is down.
            payloads[name] = {"_fetch_error": str(e)}

    ctx = InvariantContext(payloads=payloads, base_url=base_url)
    violations: list[dict] = []
    for inv in INVARIANTS:
        try:
            result = inv(ctx)
        except Exception as e:
            result = {
                "name": getattr(inv, "__name__", "<unknown>"),
                "ok": False,
                "summary": f"invariant raised: {e}",
                "detail": "",
            }
        if not result.get("ok"):
            violations.append(result)
            logger.warning("VIOLATION: %s -- %s", result.get("name"), result.get("summary"))
        else:
            logger.info("OK: %s", result.get("name"))

    if not violations:
        logger.info("all %d invariants passed", len(INVARIANTS))
        return 0

    # Alert
    ts_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()
    if tg_token and tg_chat:
        ok = send_telegram(
            tg_token, tg_chat, _format_violation_telegram(violations), dry_run=dry_run
        )
        logger.info("telegram alert sent=%s", ok)
    else:
        logger.warning("Telegram credentials missing; skipping TG alert")

    gh_token = os.environ.get("GH_TOKEN", "").strip()
    gh_repo = os.environ.get("GH_REPO", "").strip()
    label = os.environ.get("MONITOR_LABEL", "dashboard-monitor").strip()
    if gh_token and gh_repo:
        title, body = _format_violation_issue(violations, ts_iso, base_url)
        url = file_github_issue(
            gh_token, gh_repo, title, body, [label], dry_run=dry_run
        )
        if url:
            logger.info("GH issue filed: %s", url)
    else:
        logger.warning("GH credentials missing; skipping issue filing")

    return 0


if __name__ == "__main__":
    sys.exit(run_once())
