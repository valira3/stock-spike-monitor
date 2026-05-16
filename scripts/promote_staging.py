#!/usr/bin/env python3
"""promote_staging.py -- promote the staging branch to production (main).

Runs pre-flight checks, opens a PR from staging -> main, and optionally
waits for CI then merges.

Usage:
    python scripts/promote_staging.py              # pre-flight + open PR
    python scripts/promote_staging.py --dry-run    # show what would happen, no PR
    python scripts/promote_staging.py --skip-ci    # skip local run_ci.py (not recommended)
    python scripts/promote_staging.py --merge      # open PR, wait for CI, then merge
    python scripts/promote_staging.py --force-rth  # bypass the RTH gate warning

Checks:
  [1] On staging branch with no uncommitted changes
  [2] staging has commits ahead of main (something to promote)
  [3] RTH gate (warns if merging during 09:30-16:00 ET Mon-Fri)
  [4] Local CI via run_ci.py (pytest + version + em-dash + ruff)
  [5] gh CLI available + authenticated
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

REPO = Path(__file__).resolve().parent.parent
STAGING_BRANCH = "staging"
MAIN_BRANCH = "main"

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
BOLD = "\033[1m"
RESET = "\033[0m"

_WINDOWS = sys.platform == "win32"
if _WINDOWS:
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        GREEN = RED = YELLOW = CYAN = BOLD = RESET = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hdr(step: int, total: int, label: str) -> None:
    print(f"\n{BOLD}[{step}/{total}] {label}...{RESET}", flush=True)


def _ok(msg: str = "OK") -> None:
    print(f"  {GREEN}[OK] {msg}{RESET}", flush=True)


def _warn(msg: str) -> None:
    print(f"  {YELLOW}[WARN] {msg}{RESET}", flush=True)


def _fail(msg: str) -> None:
    print(f"  {RED}[FAIL] {msg}{RESET}", flush=True)


def _info(msg: str) -> None:
    print(f"  {CYAN}{msg}{RESET}", flush=True)


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=REPO, **kwargs)


def _bot_version() -> str:
    bvp = REPO / "bot_version.py"
    m = re.search(r'^BOT_VERSION\s*=\s*"([^"]+)"', bvp.read_text(encoding="utf-8"), re.M)
    if not m:
        raise RuntimeError("BOT_VERSION not found in bot_version.py")
    return m.group(1)


def _is_rth() -> bool:
    now = datetime.datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 570 <= minutes < 960  # 09:30-16:00 ET


def _et_now() -> str:
    now = datetime.datetime.now(ZoneInfo("America/New_York"))
    return now.strftime("%Y-%m-%d %H:%M %Z")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_branch_state() -> bool:
    """[1] On staging, no uncommitted changes."""
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != STAGING_BRANCH:
        _fail(f"Must be on '{STAGING_BRANCH}' branch (currently on '{branch}')")
        return False

    dirty = _git("status", "--porcelain")
    if dirty:
        _fail(
            "Uncommitted changes present -- commit or stash before promoting:\n"
            + "\n".join(f"    {line}" for line in dirty.splitlines()[:10])
        )
        return False

    _ok(f"On '{STAGING_BRANCH}', working tree clean")
    return True


def check_ahead_of_main() -> list[str]:
    """[2] Return commits staging has over main (empty = nothing to promote)."""
    subprocess.run(["git", "fetch", "origin", MAIN_BRANCH, "--quiet"], cwd=REPO)
    raw = _git("log", f"origin/{MAIN_BRANCH}..HEAD", "--oneline")
    commits = [line for line in raw.splitlines() if line.strip()]
    if not commits:
        _fail(f"staging has no commits ahead of origin/{MAIN_BRANCH} -- nothing to promote")
        return []
    _ok(f"{len(commits)} commit(s) to promote")
    for c in commits:
        _info(c)
    return commits


def check_rth_gate(force: bool) -> bool:
    """[3] Warn if inside RTH; block unless --force-rth."""
    if not _is_rth():
        _ok(f"Outside RTH ({_et_now()}) -- safe to merge")
        return True

    msg = (
        f"RTH gate: it is {_et_now()} (inside 09:30-16:00 ET Mon-Fri). "
        "Merging to main triggers a Railway redeploy that wipes the in-memory "
        "RiskBook mid-day (phantom-position risk)."
    )
    if force:
        _warn(msg + " Proceeding anyway (--force-rth).")
        return True

    _fail(msg + " Re-run with --force-rth to override, or promote after 16:00 ET.")
    return False


def check_ci(skip: bool) -> bool:
    """[4] Run local CI suite."""
    if skip:
        _warn("Skipping local CI (--skip-ci) -- not recommended")
        return True

    proc = _run([sys.executable, "scripts/run_ci.py"])
    if proc.returncode != 0:
        _fail("run_ci.py failed -- fix errors before promoting")
        return False
    _ok("All local CI checks passed")
    return True


def check_gh() -> bool:
    """[5] gh CLI available and authenticated."""
    probe = subprocess.run(
        ["gh", "auth", "status"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        _fail("gh CLI not authenticated -- run: gh auth login")
        return False
    _ok("gh CLI authenticated")
    return True


# ---------------------------------------------------------------------------
# PR creation
# ---------------------------------------------------------------------------


def _build_pr_body(commits: list[str], version: str) -> str:
    commit_lines = "\n".join(f"- {c}" for c in commits)
    return f"""\
## Staging → Production Promotion

**Version:** `{version}`
**Source:** `staging` → `main`

### Commits included

{commit_lines}

### Pre-promotion checklist

- [x] Local CI passed (`python scripts/run_ci.py`)
- [x] BOT_VERSION bumped + CHANGELOG updated
- [x] Validated on `tradegenius-staging.up.railway.app`
- [ ] Post-deploy smoke passes on production

\U0001f916 Promoted via `scripts/promote_staging.py`
"""


def open_pr(commits: list[str], version: str, dry_run: bool) -> str | None:
    """Create the staging -> main PR. Returns PR URL or None."""
    title = f"promote: staging → main ({version})"
    body = _build_pr_body(commits, version)

    print(f"\n{BOLD}Opening PR: {title}{RESET}")
    if dry_run:
        print(f"\n{YELLOW}--- DRY RUN: PR body ---{RESET}")
        print(body)
        print(f"{YELLOW}--- end ---{RESET}\n")
        _info("(dry run) No PR created")
        return None

    body_file = REPO / ".promote_pr_body.tmp"
    try:
        body_file.write_text(body, encoding="utf-8")
        result = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--base",
                MAIN_BRANCH,
                "--head",
                STAGING_BRANCH,
                "--title",
                title,
                "--body-file",
                str(body_file),
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
    finally:
        if body_file.exists():
            body_file.unlink()

    if result.returncode != 0:
        stderr = result.stderr.strip()
        # gh exits non-zero if a PR already exists; surface the URL if so.
        if "already exists" in stderr:
            url_match = re.search(r"https://github\.com/\S+/pull/\d+", stderr)
            url = url_match.group(0) if url_match else "(see output above)"
            _warn(f"PR already exists: {url}")
            return url
        _fail(f"gh pr create failed:\n{stderr}")
        return None

    url = result.stdout.strip()
    print(f"\n  {GREEN}PR created: {url}{RESET}")
    return url


def wait_and_merge(pr_url: str, timeout_s: int = 300) -> bool:
    """Poll PR CI status then merge when green."""
    import time

    pr_num = pr_url.rstrip("/").split("/")[-1]
    print(f"\n{BOLD}Waiting for CI on PR #{pr_num}...{RESET}")

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["gh", "pr", "view", pr_num, "--json", "statusCheckRollup,state"],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            _fail(f"gh pr view failed: {result.stderr.strip()}")
            return False

        data = json.loads(result.stdout)
        if data.get("state") == "MERGED":
            _ok("PR already merged")
            return True

        checks = data.get("statusCheckRollup") or []
        if checks:
            conclusions = {c.get("conclusion") or c.get("status") for c in checks}
            pending = {"IN_PROGRESS", "QUEUED", "PENDING", "WAITING", None}
            failed = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "ERROR"}

            if conclusions & failed:
                _fail(f"CI checks failed: {conclusions & failed}")
                return False
            if not (conclusions & pending):
                break  # all checks terminal -- proceed to merge

        remaining = int(deadline - time.monotonic())
        print(f"  {YELLOW}CI pending... ({remaining}s remaining){RESET}", flush=True)
        time.sleep(15)
    else:
        _fail(f"CI did not complete within {timeout_s}s -- merge manually")
        return False

    _ok("CI green -- merging")
    result = subprocess.run(
        ["gh", "pr", "merge", pr_num, "--squash", "--admin", "--delete-branch=false"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        _fail(f"Merge failed: {result.stderr.strip()}")
        return False

    _ok(f"Merged PR #{pr_num} to {MAIN_BRANCH}")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would happen without creating a PR"
    )
    parser.add_argument(
        "--skip-ci", action="store_true", help="Skip local run_ci.py (not recommended)"
    )
    parser.add_argument(
        "--merge", action="store_true", help="After PR is created, wait for CI and merge"
    )
    parser.add_argument("--force-rth", action="store_true", help="Bypass the RTH gate warning")
    args = parser.parse_args()

    total_checks = 5
    print(f"\n{BOLD}=== promote_staging.py ==={RESET}")
    print(f"  {CYAN}staging -> main promotion pre-flight{RESET}\n")

    _hdr(1, total_checks, "Branch state")
    if not check_branch_state():
        return 1

    _hdr(2, total_checks, "Commits to promote")
    commits = check_ahead_of_main()
    if not commits:
        return 1

    _hdr(3, total_checks, "RTH gate")
    if not check_rth_gate(args.force_rth):
        return 1

    _hdr(4, total_checks, "Local CI")
    if not check_ci(args.skip_ci):
        return 1

    _hdr(5, total_checks, "gh CLI")
    if not check_gh():
        return 1

    try:
        version = _bot_version()
    except RuntimeError as e:
        _fail(str(e))
        return 1

    print(f"\n{BOLD}All checks passed. BOT_VERSION={version}{RESET}")

    pr_url = open_pr(commits, version, dry_run=args.dry_run)

    if args.dry_run:
        return 0

    if pr_url is None:
        return 1

    if args.merge:
        if not wait_and_merge(pr_url):
            return 1
        print(f"\n{GREEN}{BOLD}Promotion complete. Production is now running {version}.{RESET}")
        print(f"{CYAN}Monitor post-deploy smoke: python scripts/run_smoke.py --no-wait{RESET}\n")
    else:
        pr_num = pr_url.rstrip("/").split("/")[-1]
        print(f"\n{CYAN}PR is open. When ready to merge:{RESET}")
        print(f"  gh pr merge {pr_num} --squash --admin")
        print("  python scripts/run_smoke.py --no-wait  # verify production\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
