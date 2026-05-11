"""tools.pr_watchdog -- v7.91.0 -- scheduled CI + monitor-issue watchdog.

Polls the repo's open pull requests and the most recent
`dashboard-monitor`-labeled issue. When any of the following is
actionable, opens a fresh tracking issue (or appends a comment to
an existing one) so the operator sees the alert in the standard
GitHub notifications path:

  - Open non-draft PR with all checks green AND mergeable_state=clean
    AND merged=false -- "ready to merge but no one merged it"
  - Open non-draft PR with any check completed with conclusion=failure
    -- "needs attention"
  - New dashboard-monitor issue opened since the last watchdog run
    (tracked by the `last_seen_issue` value in the tracking issue body)

The workflow that schedules this lives at
`.github/workflows/pr-watchdog.yml`. It runs every 5 minutes during
RTH (13:00-21:00 UTC Mon-Fri), covering 8:00 AM CT through ~4 PM ET
year-round (GitHub cron has no DST awareness, so the window spans
the DST/standard delta).

## Environment

  GH_TOKEN     PAT or workflow GITHUB_TOKEN with `issues: write`
               + `pull-requests: read`.
  GH_REPO      `owner/repo`. Provided by GHA as `${{ github.repository }}`.
  WATCHDOG_DRY_RUN  When "1" -- print the planned action, do not
                    open/comment.

## Tracking-issue contract

Single issue with label `pr-watchdog`. The script searches for the
most recent OPEN one; if found, posts a comment with the new
findings. If none open, creates one. Each comment includes a
`<!-- watchdog-cycle:HH:MM ET -->` HTML marker so the operator can
visually scan when the alerts landed.

When all findings clear (all monitored PRs merged, no new monitor
issues), the script closes the tracking issue with a "all clear"
comment. The next actionable finding opens a fresh one.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger("pr_watchdog")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ET = ZoneInfo("America/New_York")
TRACKING_LABEL = "pr-watchdog"
MONITOR_LABEL = "dashboard-monitor"


# ---------------------------------------------------------------------------
# GitHub REST helpers
# ---------------------------------------------------------------------------


def _gh(token: str, method: str, path: str, body: dict | None = None) -> dict | list:
    """Single-call GitHub REST wrapper.

    Returns parsed JSON. Raises urllib.error.HTTPError on non-2xx so
    the caller can decide whether to swallow or surface.
    """
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-watchdog/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode("utf-8")
        return json.loads(raw) if raw else {}


# ---------------------------------------------------------------------------
# Findings collection
# ---------------------------------------------------------------------------


def _list_open_prs(token: str, repo: str) -> list[dict]:
    return _gh(token, "GET", f"/repos/{repo}/pulls?state=open&per_page=50")  # type: ignore[return-value]


def _pr_check_runs(token: str, repo: str, sha: str) -> list[dict]:
    out = _gh(token, "GET", f"/repos/{repo}/commits/{sha}/check-runs?per_page=100")
    if isinstance(out, dict):
        return list(out.get("check_runs") or [])
    return []


def _pr_full(token: str, repo: str, num: int) -> dict:
    return _gh(token, "GET", f"/repos/{repo}/pulls/{num}")  # type: ignore[return-value]


def _list_label_issues(token: str, repo: str, label: str, state: str = "open") -> list[dict]:
    q = urllib.parse.urlencode({"state": state, "labels": label, "per_page": 30})
    out = _gh(token, "GET", f"/repos/{repo}/issues?{q}")
    # The /issues endpoint returns PRs too; filter to true issues.
    return [i for i in out if isinstance(i, dict) and "pull_request" not in i]  # type: ignore[union-attr]


def _classify_pr(pr: dict, check_runs: list[dict]) -> tuple[str, str] | None:
    """Return (kind, reason) when actionable, else None.

    kinds: "green_unmerged" | "failing_check"
    """
    if pr.get("draft"):
        return None
    if pr.get("merged"):
        return None
    completed = [c for c in check_runs if c.get("status") == "completed"]
    if not completed:
        return None
    failures = [c for c in completed if c.get("conclusion") not in ("success", "neutral", "skipped")]
    if failures:
        names = ", ".join(sorted({c.get("name", "?") for c in failures}))
        return ("failing_check", f"failing: {names}")
    if pr.get("mergeable_state") == "clean":
        return ("green_unmerged", "all checks green, mergeable=clean")
    return None


# ---------------------------------------------------------------------------
# Tracking issue
# ---------------------------------------------------------------------------


def _find_tracking_issue(token: str, repo: str) -> dict | None:
    issues = _list_label_issues(token, repo, TRACKING_LABEL, state="open")
    if not issues:
        return None
    issues.sort(key=lambda i: i.get("number") or 0, reverse=True)
    return issues[0]


def _open_tracking_issue(
    token: str, repo: str, title: str, body: str, dry_run: bool
) -> str | None:
    if dry_run:
        logger.info("[dry-run] would OPEN tracking issue: %s", title)
        return None
    out = _gh(
        token,
        "POST",
        f"/repos/{repo}/issues",
        {"title": title, "body": body, "labels": [TRACKING_LABEL]},
    )
    return out.get("html_url") if isinstance(out, dict) else None


def _comment_issue(token: str, repo: str, number: int, body: str, dry_run: bool) -> bool:
    if dry_run:
        logger.info("[dry-run] would COMMENT on #%d: %s", number, body[:120])
        return True
    _gh(token, "POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})
    return True


def _close_issue(token: str, repo: str, number: int, dry_run: bool) -> bool:
    if dry_run:
        logger.info("[dry-run] would CLOSE #%d", number)
        return True
    _gh(token, "PATCH", f"/repos/{repo}/issues/{number}", {"state": "closed"})
    return True


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def _now_et_label() -> str:
    return datetime.now(timezone.utc).astimezone(ET).strftime("%Y-%m-%d %H:%M ET")


def _render_finding_block(findings: list[dict]) -> str:
    lines: list[str] = []
    pr_lines = [f for f in findings if f["kind"] in ("green_unmerged", "failing_check")]
    monitor_lines = [f for f in findings if f["kind"] == "new_monitor_issue"]
    if pr_lines:
        lines.append("### Pull requests needing attention")
        for f in pr_lines:
            tag = "GREEN, unmerged" if f["kind"] == "green_unmerged" else "FAILING"
            lines.append(f"- **PR #{f['number']}** ({tag}) — {f['summary']} — {f['url']}")
    if monitor_lines:
        lines.append("")
        lines.append("### New dashboard-monitor issue(s) since last cycle")
        for f in monitor_lines:
            lines.append(f"- **#{f['number']}** ({f['summary']}) — {f['url']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"FATAL: env var {name} is required", file=sys.stderr)
        sys.exit(1)
    return v


def main() -> int:
    token = _require_env("GH_TOKEN")
    repo = _require_env("GH_REPO")
    dry_run = os.environ.get("WATCHDOG_DRY_RUN", "").strip() == "1"

    findings: list[dict] = []

    # PR sweep.
    try:
        prs = _list_open_prs(token, repo)
    except Exception as e:
        logger.warning("list-open-prs failed: %s", e)
        prs = []
    for pr in prs:
        num = pr.get("number")
        sha = (pr.get("head") or {}).get("sha")
        if not num or not sha:
            continue
        try:
            check_runs = _pr_check_runs(token, repo, sha)
            full = _pr_full(token, repo, num)
        except Exception as e:
            logger.warning("pr #%s read failed: %s", num, e)
            continue
        classified = _classify_pr(full, check_runs)
        if classified is None:
            continue
        kind, reason = classified
        findings.append(
            {
                "kind": kind,
                "number": num,
                "summary": reason,
                "url": full.get("html_url") or f"https://github.com/{repo}/pull/{num}",
            }
        )

    # Monitor-issue sweep. We use the tracking issue's body to remember
    # the highest seen dashboard-monitor issue number across cycles.
    tracking = _find_tracking_issue(token, repo)
    last_seen = 0
    if tracking:
        body = tracking.get("body") or ""
        for line in body.splitlines():
            if line.startswith("last_seen_monitor_issue:"):
                try:
                    last_seen = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass

    try:
        monitor_issues = _list_label_issues(token, repo, MONITOR_LABEL, state="open")
    except Exception as e:
        logger.warning("list monitor issues failed: %s", e)
        monitor_issues = []
    new_monitor_issues = sorted(
        (i for i in monitor_issues if (i.get("number") or 0) > last_seen),
        key=lambda i: i.get("number") or 0,
    )
    highest_monitor = last_seen
    for i in new_monitor_issues:
        n = i.get("number")
        if n and n > highest_monitor:
            highest_monitor = n
        findings.append(
            {
                "kind": "new_monitor_issue",
                "number": n,
                "summary": (i.get("title") or "")[:120],
                "url": i.get("html_url") or "",
            }
        )

    # Empty cycle -- close tracking issue if open.
    if not findings:
        logger.info("[OK] cycle clean at %s, no actionable items", _now_et_label())
        if tracking:
            _comment_issue(
                token,
                repo,
                tracking["number"],
                f"All clear at {_now_et_label()} — closing.",
                dry_run,
            )
            _close_issue(token, repo, tracking["number"], dry_run)
        return 0

    # Findings -- open or comment.
    finding_block = _render_finding_block(findings)
    header = f"<!-- watchdog-cycle:{_now_et_label()} -->"
    state_line = f"last_seen_monitor_issue:{highest_monitor}"
    body = f"{header}\n\nCycle at {_now_et_label()}.\n\n{finding_block}\n\n---\n{state_line}\n"

    logger.info("findings=%d at %s", len(findings), _now_et_label())
    if tracking:
        _comment_issue(token, repo, tracking["number"], body, dry_run)
        # Update the tracking issue body's last_seen marker.
        if not dry_run:
            new_body = tracking.get("body") or ""
            # Replace or append the marker line.
            if "last_seen_monitor_issue:" in new_body:
                lines = new_body.splitlines()
                lines = [
                    state_line if line.startswith("last_seen_monitor_issue:") else line
                    for line in lines
                ]
                new_body = "\n".join(lines)
            else:
                new_body = new_body.rstrip() + f"\n\n{state_line}\n"
            try:
                _gh(
                    token,
                    "PATCH",
                    f"/repos/{repo}/issues/{tracking['number']}",
                    {"body": new_body},
                )
            except Exception as e:
                logger.warning("tracking-issue body update failed: %s", e)
    else:
        title = f"[pr-watchdog] {len(findings)} actionable item(s) at {_now_et_label()}"
        body_root = (
            "Automated detection from the scheduled PR + dashboard-monitor "
            "watchdog (`tools/pr_watchdog.py`).\n\n"
            "This issue stays OPEN while at least one PR or monitor issue "
            "remains actionable. Each subsequent cycle appends a comment "
            "with the latest findings. The issue auto-closes when a cycle "
            "finds nothing actionable.\n\n"
            f"{finding_block}\n\n---\n{state_line}\n"
        )
        _open_tracking_issue(token, repo, title, body_root, dry_run)

    return 0


if __name__ == "__main__":
    sys.exit(main())
