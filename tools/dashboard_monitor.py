"""tools.dashboard_monitor -- live dashboard validator.

Periodically (every 5 min during US premarket + RTH; see
.github/workflows/dashboard-monitor.yml) hits the live production
dashboard's read endpoints, runs a battery of invariants against the
responses, and on any violation:

  * Posts a structured alert to the configured Telegram channel.
  * Files a GitHub issue with full diagnostic context (observed vs
    expected, repro steps, raw payloads), tagged with a @claude
    mention so the Claude Code GitHub app can auto-draft a fix PR.

The monitor itself is read-only against production -- it never writes
back to the bot. v7.68.0: authenticates by POSTing to /login with the
operator's DASHBOARD_PASSWORD, capturing the spike_session cookie from
the 302 response, and reusing that cookie on subsequent GETs. Matches
the same auth flow a human operator uses in the browser.

Usage:
    DASHBOARD_BASE_URL=https://tradegenius.up.railway.app \\
    DASHBOARD_PASSWORD=<password> \\
    TELEGRAM_BOT_TOKEN=<token> \\
    TELEGRAM_ADMIN_CHAT_ID=<chat-id> \\
    GH_TOKEN=<pat-with-issues:write> \\
    GH_REPO=valira3/stock-spike-monitor \\
    python -m tools.dashboard_monitor

Env vars (all required except where noted):
    DASHBOARD_BASE_URL          e.g. https://tradegenius.up.railway.app
    DASHBOARD_PASSWORD          login password (>= 8 chars); same value the
                                live bot has under DASHBOARD_PASSWORD env
    TELEGRAM_BOT_TOKEN          existing bot token
    TELEGRAM_ADMIN_CHAT_ID      chat-id to post alerts (numeric or @handle)
    GH_TOKEN                    PAT or workflow GITHUB_TOKEN with issues:write
    GH_REPO                     owner/repo, e.g. valira3/stock-spike-monitor
    MONITOR_DRY_RUN             optional; "1" disables Telegram + GH issue
                                side effects (prints diagnostics only).
    MONITOR_LABEL               optional issue label (default: "dashboard-monitor")
    MONITOR_HEARTBEAT           optional; "1" emits a silent Telegram
                                heartbeat once per hour (at the first
                                cron tick of each hour). Useful to
                                confirm the cron is firing when no
                                violations are surfacing on their own.

Exit codes:
    0  all invariants passed (or violations were filed successfully)
    1  hard failure (login failure, secret missing, etc.)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from urllib.parse import urlparse

from tools.dashboard_monitor_invariants import INVARIANTS, InvariantContext

logger = logging.getLogger("dashboard_monitor")


SESSION_COOKIE_NAME = "spike_session"


# ---------------------------------------------------------------------------
# Dashboard HTTP client -- login + cookie reuse
# ---------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Block urllib from auto-following the /login 302 so we can read
    the Set-Cookie header off the response."""

    def redirect_request(self, *args, **kwargs):
        return None


class DashboardClient:
    """Browser-shape login client.

    POSTs the password form-encoded to /login, captures the
    spike_session cookie from the 302 response, then re-uses it on
    subsequent GETs. Mirrors what an operator's browser does.

    The CSRF check on /login requires Origin/Referer to match the
    Host (when either is present). We send a matching Origin so the
    check passes.
    """

    def __init__(self, base_url: str, password: str, timeout: float = 15.0):
        # v7.69.0 -- defensive scheme normalization. The DASHBOARD_URL
        # GHA secret was set as "tradegenius.up.railway.app" (no
        # scheme), which Python's urllib rejects with "unknown url
        # type". Assume https:// when the operator omits it.
        bu = base_url.strip().rstrip("/")
        if "://" not in bu:
            bu = "https://" + bu
        self.base_url = bu
        self.password = password
        self.timeout = timeout
        self._cookie_value: str | None = None
        parsed = urlparse(self.base_url)
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self._host = parsed.netloc

    def login(self) -> None:
        if self._cookie_value is not None:
            return
        url = self.base_url + "/login"
        body = urllib.parse.urlencode({"password": self.password}).encode()
        # v7.69.0 -- DON'T send Origin/Referer. dashboard_server's CSRF
        # check is `src_host = _host_of(origin) or _host_of(referer);
        # if src_host and src_host != host: return 403`. With both
        # headers absent, src_host is "" and the check short-circuits.
        # Browsers always send Origin so the operator-flow stays
        # protected; server-to-server callers (like this monitor)
        # pass through cleanly. (Reverse-proxy-rewritten Host headers
        # at Railway can cause spurious 403s when Origin/Referer are
        # sent verbatim.)
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "tg-dashboard-monitor/7.70.0",
            },
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            opener.open(req, timeout=self.timeout)
            raise RuntimeError("Expected /login to return a 302, got 200")
        except urllib.error.HTTPError as e:
            if e.code != 302:
                if e.code == 401:
                    raise RuntimeError("/login returned 401 -- DASHBOARD_PASSWORD wrong?")
                if e.code == 429:
                    raise RuntimeError("/login rate-limited (429) -- retry in 60s")
                raise RuntimeError(f"/login returned unexpected status {e.code}")
            set_cookies = e.headers.get_all("Set-Cookie") or []
            for c in set_cookies:
                if c.startswith(SESSION_COOKIE_NAME + "="):
                    val = c.split(";", 1)[0]
                    self._cookie_value = val.split("=", 1)[1]
                    return
            raise RuntimeError("/login 302 had no spike_session Set-Cookie header")
        except urllib.error.URLError as e:
            raise RuntimeError(f"/login request failed: {e.reason}")

    def get_json(self, path: str) -> dict:
        if self._cookie_value is None:
            raise RuntimeError("Not logged in; call login() first")
        url = self.base_url + path
        req = urllib.request.Request(
            url,
            headers={
                "Cookie": f"{SESSION_COOKIE_NAME}={self._cookie_value}",
                "User-Agent": "tg-dashboard-monitor/7.70.0",
            },
        )
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


def send_telegram(
    token: str,
    chat_id: str,
    text: str,
    dry_run: bool = False,
    silent: bool = False,
) -> bool:
    if dry_run:
        print(f"[dry-run] TG -> {chat_id}: {text[:200]}...")
        return True
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    fields = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": "true",
    }
    if silent:
        fields["disable_notification"] = "true"
    payload = urllib.parse.urlencode(fields).encode()
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
# Main
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
        "Automated detection from the live-dashboard monitor",
        f"(`tools/dashboard_monitor.py`). Hit `{base_url}` and observed"
        f" {len(violations)} invariant violation(s).",
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
            "DASHBOARD_PASSWORD=<password> \\",
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


def _heartbeat_should_fire() -> bool:
    """v7.70.0 -- gate hourly heartbeat to one ping per hour.

    Cron is `7-57/10` so ticks land at :07, :17, ..., :57. Firing the
    heartbeat only at :07 yields one silent TG ping per active hour
    (13/day at peak). Operator can globally disable by unsetting
    MONITOR_HEARTBEAT.

    v7.71.0 -- on manual `workflow_dispatch` invocations, always fire
    the heartbeat regardless of the current minute. Manual runs are
    explicit liveness checks ("did I wire this up correctly?"), so
    suppressing them based on the cron-minute gate was a UX bug.
    GitHub Actions sets GITHUB_EVENT_NAME=workflow_dispatch for the
    Run-workflow button; the cron path sets it to "schedule".
    """
    if os.environ.get("MONITOR_HEARTBEAT", "").strip() != "1":
        return False
    if os.environ.get("GITHUB_EVENT_NAME", "").strip() == "workflow_dispatch":
        return True
    return time.gmtime().tm_min < 10


def _heartbeat_text(payloads: dict[str, Any], base_url: str) -> str:
    state = payloads.get("state") or {}
    mode = state.get("regime_mode") or state.get("mode") or "?"
    v10 = state.get("v10") or {}
    live_mode = v10.get("live_mode", "?")
    equity = state.get("equity_usd")
    if equity is None:
        portfolio = state.get("portfolio") or {}
        equity = portfolio.get("equity", "?")
    return (
        f"❤️ *dashboard-monitor ok*\n"
        f"`{base_url}`\n"
        f"regime: `{mode}` · v10 live\\_mode: `{live_mode}` · equity: `{equity}`"
    )


def _emit_heartbeat(payloads: dict[str, Any], base_url: str, dry_run: bool) -> None:
    if not _heartbeat_should_fire():
        return
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()
    if not (tg_token and tg_chat):
        logger.info("heartbeat skipped (no TG creds)")
        return
    ok = send_telegram(
        tg_token,
        tg_chat,
        _heartbeat_text(payloads, base_url),
        dry_run=dry_run,
        silent=True,
    )
    logger.info("heartbeat sent=%s", ok)


def _emit_alerts(violations: list[dict], base_url: str, dry_run: bool) -> None:
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


def run_once() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    base_url = _require_env("DASHBOARD_BASE_URL")
    password = _require_env("DASHBOARD_PASSWORD")
    dry_run = os.environ.get("MONITOR_DRY_RUN", "").strip() == "1"

    client = DashboardClient(base_url, password)
    try:
        client.login()
        logger.info("login ok against %s", base_url)
    except RuntimeError as e:
        logger.error("login failed: %s", e)
        violations = [
            {
                "name": "login",
                "ok": False,
                "summary": f"login failed: {e}",
                "detail": "",
            }
        ]
        _emit_alerts(violations, base_url, dry_run)
        return 1

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
        _emit_heartbeat(payloads, base_url, dry_run)
        return 0

    _emit_alerts(violations, base_url, dry_run)
    _emit_heartbeat(payloads, base_url, dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(run_once())
