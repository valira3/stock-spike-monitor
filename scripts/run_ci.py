#!/usr/bin/env python3
"""run_ci.py -- cross-platform local CI runner (Windows/macOS/Linux).

Supersedes the bash-only scripts/preflight.sh for day-to-day development on
Windows. Runs the strategy-test / version-consistency / em-dash / ruff checks
that every push needs.

Usage:
    python scripts/run_ci.py              # fast checks (pytest + version + em-dash + ruff)
    python scripts/run_ci.py --wide       # also run pytest tests/ (needs prod deps)
    python scripts/run_ci.py --smoke      # also run python smoke_test.py (31 local tests)
    python scripts/run_ci.py --slow       # include pytest.mark.slow tests
    python scripts/run_ci.py --all        # fast + wide + smoke + slow

Checks run in order:
  [1/5] pytest tests/strategy/   (fast, no telegram dep, 1100+ tests)
  [2/5] BOT_VERSION consistency  (bot_version.py == trade_genius.py == CHANGELOG top)
  [3/5] CURRENT_MAIN_NOTE guard  (leading line must start with vX.Y.Z)
  [4/5] Em-dash literal check    (new .py lines added vs origin/main must not carry U+2014)
  [5/5] ruff check + ruff format --check  (only if ruff is installed)
  [opt] pytest tests/ (wide)     (top-level suite; needs telegram+alpaca-py+lxml+.env.monitor;
                                  pass --wide or --all; skipped if deps missing)
  [opt] python smoke_test.py     (31 local smoke tests; pass --smoke or --all)

Sibling local runners:
  - scripts/run_smoke.py    (post-push smoke: Railway-version wait + 31 local + 9 prod tests)
  - scripts/run_monitor.py  (every-5-min dashboard health check during US RTH)

GitHub Actions are no longer the canonical path for any of these; the
.github/workflows/*.yml files that remain are kept for emergency
workflow_dispatch only.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO = Path(__file__).resolve().parent.parent
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BOLD = "\033[1m"
RESET = "\033[0m"

_WINDOWS = sys.platform == "win32"

# Windows cmd/PowerShell doesn't render ANSI by default in older terminals.
# Enable it if possible; fall back to plain output otherwise.
if _WINDOWS:
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        GREEN = RED = YELLOW = BOLD = RESET = ""


def _hdr(step: str, total: int, label: str) -> None:
    print(f"\n{BOLD}[{step}/{total}] {label}...{RESET}", flush=True)


def _ok(msg: str = "OK") -> None:
    print(f"  {GREEN}[OK] {msg}{RESET}", flush=True)


def _skip(msg: str) -> None:
    print(f"  {YELLOW}[SKIP] {msg}{RESET}", flush=True)


def _fail(msg: str) -> None:
    print(f"  {RED}[FAIL] {msg}{RESET}", flush=True)


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command, streaming stdout/stderr live."""
    return subprocess.run(cmd, cwd=REPO, **kwargs)


def _git(*args: str) -> str:
    """Run a git command and return trimmed stdout (empty string on error)."""
    result = subprocess.run(
        ["git", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _changed_py_files(base_ref: str) -> list[Path]:
    """Return .py files changed (vs base_ref) that still exist on disk."""
    lines: list[str] = []
    # Committed changes vs base
    lines += _git("diff", "--name-only", f"{base_ref}...HEAD", "--", "*.py").splitlines()
    # Uncommitted changes (staged + unstaged)
    lines += _git("diff", "--name-only", "--", "*.py").splitlines()
    # Untracked new files
    lines += _git("ls-files", "--others", "--exclude-standard", "--", "*.py").splitlines()
    seen: set[str] = set()
    result: list[Path] = []
    for rel in lines:
        rel = rel.strip()
        if rel and rel not in seen:
            seen.add(rel)
            p = REPO / rel
            if p.exists():
                result.append(p)
    return result


def _base_ref() -> str | None:
    """Return the best available merge base ref (origin/main > main > None)."""
    for ref in ("origin/main", "main"):
        out = _git("rev-parse", "--verify", "--quiet", ref)
        if out:
            return ref
    return None


# ---------------------------------------------------------------------------
# Check implementations
# ---------------------------------------------------------------------------


def check_pytest(slow: bool) -> bool:
    """[1] Run pytest tests/strategy/ (fast lane; no telegram dep).

    The fast lane is tests/strategy/ -- pure-Python unit tests that don't
    need the `telegram` package or live env vars. v10.0.1 broadened the
    pre-push gate scope, but tests/ (the wider top-level suite) still
    requires the full prod-dep set (telegram, FMP_API_KEY, alpaca-py,
    etc.). Operators who have those installed locally can run
    `pytest tests/` directly; the GHA path is retired (v10.0.1).
    """
    # Verify pytest is importable before building a full command.
    probe = subprocess.run(
        [sys.executable, "-m", "pytest", "--version"],
        cwd=REPO,
        capture_output=True,
    )
    if probe.returncode != 0:
        _skip(
            f"pytest not installed in this Python env ({sys.executable}) -- run: pip install pytest"
        )
        # Return True so missing pytest doesn't block other checks;
        # the operator is warned clearly.
        return True

    cmd = [sys.executable, "-m", "pytest", "tests/strategy/", "-q", "--tb=short"]
    if not slow:
        cmd += ["-m", "not slow"]
    # Use pytest-xdist parallelism if available
    xdist_probe = subprocess.run(
        [sys.executable, "-c", "import xdist"],
        cwd=REPO,
        capture_output=True,
    )
    if xdist_probe.returncode == 0:
        cmd += ["-n", "auto"]
    proc = _run(cmd)
    return proc.returncode == 0


def check_pytest_wide() -> bool:
    """[opt] Run pytest tests/ (full top-level suite excluding strategy).

    Requires the production dependency set (telegram, FMP_API_KEY,
    alpaca-py credentials in .env.monitor, lxml). Skipped gracefully
    when those aren't available so CI stays green on a slimmer env.

    Toggled by --wide; the post-v10.0.1 cleanup retired 36 legacy test
    files so this lane now passes cleanly when the env is wired up.
    """
    probe = subprocess.run(
        [sys.executable, "-m", "pytest", "--version"],
        cwd=REPO,
        capture_output=True,
    )
    if probe.returncode != 0:
        _skip("pytest not installed -- skipping wide lane")
        return True

    # Sanity-check that the deps trade_genius import needs are reachable.
    # If not, skip the wide lane with a clear breadcrumb rather than
    # surfacing a confusing ModuleNotFoundError later.
    needed = ("telegram", "alpaca", "lxml")
    missing = []
    for name in needed:
        rc = subprocess.run(
            [sys.executable, "-c", f"import {name}"],
            cwd=REPO,
            capture_output=True,
        )
        if rc.returncode != 0:
            missing.append(name)
    if missing:
        _skip(f"wide lane skipped -- missing: {', '.join(missing)} (pip install -r requirements.txt)")
        return True

    cmd = [
        sys.executable, "-m", "pytest", "tests/",
        "--ignore=tests/strategy",
        "-q", "--tb=short",
    ]
    xdist_probe = subprocess.run(
        [sys.executable, "-c", "import xdist"],
        cwd=REPO,
        capture_output=True,
    )
    if xdist_probe.returncode == 0:
        cmd += ["-n", "auto"]
    proc = _run(cmd)
    return proc.returncode == 0


def check_version() -> bool:
    """[2+3] BOT_VERSION consistency + CURRENT_MAIN_NOTE guard."""
    ok = True

    # --- parse bot_version.py ---
    bvp = REPO / "bot_version.py"
    m = re.search(r'^BOT_VERSION\s*=\s*"([^"]+)"', bvp.read_text(encoding="utf-8"), re.M)
    if not m:
        _fail("BOT_VERSION not found in bot_version.py")
        return False
    version = m.group(1)

    # --- parse trade_genius.py ---
    tgp = REPO / "trade_genius.py"
    m2 = re.search(r'^BOT_VERSION\s*=\s*"([^"]+)"', tgp.read_text(encoding="utf-8"), re.M)
    if not m2:
        _fail("BOT_VERSION not found in trade_genius.py")
        return False
    tg_version = m2.group(1)

    if version != tg_version:
        _fail(
            f"bot_version.py BOT_VERSION={version!r} but trade_genius.py"
            f" BOT_VERSION={tg_version!r} -- keep them in sync"
        )
        ok = False

    # --- parse CHANGELOG.md top heading ---
    clp = REPO / "CHANGELOG.md"
    cl_top_m = re.search(r"^## v([0-9][^\s]*)", clp.read_text(encoding="utf-8"), re.M)
    if not cl_top_m:
        _fail("No '## vX.Y.Z' heading found in CHANGELOG.md")
        ok = False
    else:
        cl_version = cl_top_m.group(1)
        # Strip trailing " (date)" suffix if present so we compare cleanly
        cl_version_clean = cl_version.split()[0].rstrip(")")
        if version != cl_version_clean:
            _fail(
                f"bot_version.py BOT_VERSION={version!r} but CHANGELOG.md top heading"
                f" is v{cl_version!r} -- add a new ## v{version} entry"
            )
            ok = False

    if not ok:
        return False

    # --- CURRENT_MAIN_NOTE guard: leading line must start with vX.Y.Z ---
    # We extract the note using the same logic as preflight.sh (parse-and-exec approach).
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                r"""
import re, pathlib
src = pathlib.Path('trade_genius.py').read_text(encoding='utf-8')
# The note is built by _derive_current_main_note() which reads CHANGELOG.md.
# Instead of importing (which would pull all deps), parse the CHANGELOG directly.
import sys, pathlib
cl = pathlib.Path('CHANGELOG.md').read_text(encoding='utf-8')
m = re.search(r'^## v([0-9][^\n]*)', cl, re.M)
if not m:
    print('')
else:
    print('v' + m.group(1).split()[0].rstrip(')'))
""",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        note_head = proc.stdout.strip()
    except Exception:
        note_head = ""

    # The note head should start with vX.Y.Z matching BOT_VERSION
    expected_prefix = f"v{version}"
    if note_head and not note_head.startswith(expected_prefix):
        _fail(
            f"CHANGELOG.md top entry ({note_head!r}) does not start with {expected_prefix!r}"
            f" -- update CHANGELOG.md before bumping BOT_VERSION"
        )
        ok = False

    if ok:
        _ok(f"v{version} consistent across bot_version.py / trade_genius.py / CHANGELOG.md")
    return ok


def check_emdash(base_ref: str | None) -> bool:
    """[4] Em-dash literal check on lines added vs base_ref."""
    EM = "\u2014"

    if base_ref is None:
        _skip("no git base ref found -- skipping em-dash diff check")
        return True

    changed = _changed_py_files(base_ref)
    if not changed:
        _ok("no changed .py files")
        return True

    found = False
    for fpath in changed:
        rel = str(fpath.relative_to(REPO))
        issues: list[str] = []

        # Lines added in committed diff
        committed = _git("diff", f"{base_ref}...HEAD", "--", rel)
        for line in committed.splitlines():
            if line.startswith("+") and not line.startswith("+++") and EM in line:
                issues.append(f"  committed: {line[:120]}")

        # Lines added in uncommitted diff
        uncommitted = _git("diff", "--", rel)
        for line in uncommitted.splitlines():
            if line.startswith("+") and not line.startswith("+++") and EM in line:
                issues.append(f"  staged/unstaged: {line[:120]}")

        # Untracked new file: every line counts
        is_tracked = bool(_git("ls-files", "--error-unmatch", rel))
        if not is_tracked:
            for i, raw_line in enumerate(
                fpath.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if EM in raw_line:
                    issues.append(f"  untracked line {i}: {raw_line[:120]}")

        if issues:
            _fail(f"literal em-dash (U+2014) added in {rel} -- use \\u2014 escape")
            for iss in issues[:5]:
                print(f"    {RED}{iss}{RESET}", flush=True)
            found = True

    if found:
        return False
    _ok("no literal em-dashes in changed .py files")
    return True


def check_ruff(base_ref: str | None) -> bool:
    """[5] ruff check + ruff format --check on changed .py files."""
    # Detect ruff -- try standalone `ruff` first, then `python -m ruff`.
    ruff_path = None
    for candidate in [["ruff"], [sys.executable, "-m", "ruff"]]:
        try:
            probe = subprocess.run(
                candidate + ["--version"],
                capture_output=True,
                cwd=REPO,
            )
            if probe.returncode == 0:
                ruff_path = candidate
                break
        except FileNotFoundError:
            continue

    if ruff_path is None:
        _skip("ruff not installed (pip install ruff)")
        return True

    if base_ref is not None:
        targets = [str(p) for p in _changed_py_files(base_ref)]
    else:
        targets = []

    if not targets:
        _skip("no changed .py files to lint")
        return True

    ok = True
    for sub in (["check", "--quiet"], ["format", "--check", "--quiet"]):
        proc = _run(ruff_path + sub + targets)
        if proc.returncode != 0:
            ok = False

    if ok:
        _ok("ruff check + format clean")
    else:
        _fail("ruff reported issues -- run: ruff check . && ruff format .")
    return ok


def check_smoke() -> bool:
    """[opt] Run python smoke_test.py (31 local tests)."""
    smoke = REPO / "smoke_test.py"
    if not smoke.exists():
        _skip("smoke_test.py not found in repo root")
        return True
    proc = _run([sys.executable, "smoke_test.py"])
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


MIN_PYTHON = (3, 10)
"""Minimum Python version required to run + lint the codebase.

Several test files (e.g. tests/strategy/test_riskbook_persistence_v7105.py)
use PEP 604 native `X | None` annotation syntax that ONLY parses on
Python 3.10+. Running preflight on 3.9 fails pytest collection with a
TypeError that looks unrelated to the actual problem. Pinning here
fail-fasts with a clear message instead.

To raise the floor in the future: bump this tuple + remove any
`from __future__ import annotations` markers you no longer need.
"""


def check_simulator() -> bool:
    """Run a fast simulator anomaly check on a tiny representative set.

    Picks one day per category (~3-5 days), runs them through the
    simulator in parallel, evaluates DEFAULT_RULES. Fails only on
    ERROR-severity rule violations. Designed to complete in <30s on
    a 4-worker machine. Gracefully degrades when the corpus is
    missing (e.g. sandbox/CI without /data corpus mounted).
    """
    corpus_root = os.environ.get("SIMULATOR_CORPUS_ROOT", "data")
    if not os.path.isdir(corpus_root):
        print(
            f"  {YELLOW}[SKIP] simulator corpus not found at {corpus_root}; "
            f"set SIMULATOR_CORPUS_ROOT or mount data/ to enable{RESET}",
            flush=True,
        )
        return True

    # The simulator package lives at the repo root, but this script
    # runs from scripts/. Make sure the import path covers both.
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    try:
        from simulator.batch import BatchConfig, run_days
        from simulator.corpus_index import (
            build_index, load_index, pick_representative,
        )
        from simulator.expectations import DEFAULT_RULES, evaluate
    except Exception as exc:
        print(f"  {YELLOW}[SKIP] simulator import failed: {exc}{RESET}",
              flush=True)
        return True

    index_path = str(REPO / "simulator" / "corpus" / "day_index.json")
    rows = load_index(index_path)
    if not rows:
        # First-time build (cheap -- a couple of seconds for 343 days).
        try:
            rows = build_index(corpus_root=corpus_root, out_path=index_path)
        except Exception as exc:
            print(f"  {YELLOW}[SKIP] corpus index build failed: {exc}{RESET}",
                  flush=True)
            return True

    if not rows:
        print(f"  {YELLOW}[SKIP] corpus index empty{RESET}", flush=True)
        return True

    dates = pick_representative(rows, per_category=1)
    if not dates:
        print(f"  {YELLOW}[SKIP] no representative days{RESET}", flush=True)
        return True

    universe = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOG", "AVGO",
                "NFLX", "ORCL", "TSLA", "QQQ", "SPY"]
    cfg = BatchConfig(workers=0, show_progress=False, corpus_root=corpus_root)
    results = run_days(dates, universe, cfg)
    rows_by_date = {r["date"]: r for r in rows}

    errors = 0
    warnings = 0
    bad_days: list[str] = []
    for r in results:
        if r.get("error"):
            errors += 1
            bad_days.append(f"{r['date']}: {r['error']}")
            continue
        failures = evaluate(rows_by_date.get(r["date"], {}), r, DEFAULT_RULES)
        for f in failures:
            if f.severity == "ERROR":
                errors += 1
                bad_days.append(f"{r['date']} [{f.rule_name}]: {f.why_fail}")
            elif f.severity == "WARN":
                warnings += 1

    if errors:
        print(f"  {RED}[FAIL] simulator: {errors} ERROR-severity anomaly(ies) "
              f"across {len(results)} days, {warnings} warnings{RESET}",
              flush=True)
        for b in bad_days[:10]:
            print(f"    {b}", flush=True)
        return False

    print(f"  {GREEN}[OK]{RESET} simulator: {len(results)} representative days "
          f"clean ({warnings} warnings)", flush=True)
    return True


def check_python_version() -> bool:
    """Warn (don't fail) on older Python. Tests that strictly need 3.10+
    use `from __future__ import annotations` to keep 3.9 compatible.
    Returns True always; the warning surfaces the future-hazard so a
    new file using PEP 604 without the `__future__` import gets a
    breadcrumb when its collection eventually fails downstream.
    """
    cur = sys.version_info[:2]
    if cur < MIN_PYTHON:
        print(
            f"  {YELLOW}[WARN] Python {'.'.join(map(str, cur))} below "
            f"recommended {'.'.join(map(str, MIN_PYTHON))}+ -- tests that use "
            f"PEP 604 `X | None` syntax need `from __future__ import "
            f"annotations`. If pytest collection fails below, that's why.{RESET}",
            flush=True,
        )
    else:
        print(
            f"  {GREEN}[OK] Python {'.'.join(map(str, cur))} "
            f"(min {'.'.join(map(str, MIN_PYTHON))}){RESET}",
            flush=True,
        )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Local CI runner for stock-spike-monitor (cross-platform).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Also run python smoke_test.py (31 local tests, needs env vars).",
    )
    parser.add_argument(
        "--slow",
        action="store_true",
        help="Include pytest.mark.slow tests (adds ~70s).",
    )
    parser.add_argument(
        "--wide",
        action="store_true",
        help=(
            "Also run pytest tests/ (excluding tests/strategy). Needs the "
            "full prod dep set (telegram, alpaca-py, lxml, .env.monitor). "
            "Skipped gracefully when deps are missing."
        ),
    )
    parser.add_argument(
        "--simulator",
        action="store_true",
        help=(
            "Run a fast simulator-based anomaly gate (~30s). Picks one "
            "representative day per market-regime category and asserts "
            "the v10 gates behave as defined in DEFAULT_RULES. Skipped "
            "gracefully if the corpus is not mounted."
        ),
    )
    parser.add_argument(
        "--all",
        dest="all_checks",
        action="store_true",
        help="Equivalent to --smoke --slow --wide --simulator.",
    )
    args = parser.parse_args()

    smoke = args.smoke or args.all_checks
    slow = args.slow or args.all_checks
    wide = args.wide or args.all_checks
    sim = args.simulator or args.all_checks

    total_fast = 5
    total = total_fast + (1 if smoke else 0) + (1 if wide else 0) + (1 if sim else 0)

    print(f"{BOLD}=== run_ci.py (stock-spike-monitor local CI) ==={RESET}", flush=True)
    if slow:
        print("  slow tests: ON", flush=True)
    if smoke:
        print("  smoke_test.py: ON", flush=True)

    base_ref = _base_ref()
    if base_ref:
        print(f"  diff base: {base_ref}", flush=True)
    else:
        print(
            f"  {YELLOW}diff base: not found (em-dash/ruff checks scope to all changed files){RESET}",
            flush=True,
        )

    failures: list[str] = []

    # [0] Python version (warn-only -- check_pytest is the real gate)
    _hdr("0", total, f"Python >= {'.'.join(map(str, MIN_PYTHON))} (warn-only)")
    check_python_version()

    # [1] pytest
    _hdr("1", total, "pytest tests/strategy/")
    if not check_pytest(slow):
        failures.append("pytest tests/strategy/")

    # [2+3] version consistency
    _hdr("2", total, "BOT_VERSION consistency + CURRENT_MAIN_NOTE")
    if not check_version():
        failures.append("version-bump consistency")

    # [4] em-dash
    _hdr("3", total, "em-dash literal check (changed .py files)")
    if not check_emdash(base_ref):
        failures.append("em-dash literal in changed .py files")

    # [5] ruff
    _hdr("4", total, "ruff lint + format check")
    if not check_ruff(base_ref):
        failures.append("ruff lint/format")

    # [opt] wide lane
    next_step = total_fast + 1
    if wide:
        _hdr(str(next_step), total, "pytest tests/ (wide lane; needs prod deps)")
        if not check_pytest_wide():
            failures.append("pytest tests/ (wide lane)")
        next_step += 1

    # [opt] simulator anomaly gate
    if sim:
        _hdr(str(next_step), total,
             "simulator anomaly gate (representative days)")
        if not check_simulator():
            failures.append("simulator anomaly gate")
        next_step += 1

    # [opt] smoke
    if smoke:
        _hdr(str(next_step), total, "python smoke_test.py (31 local tests)")
        if not check_smoke():
            failures.append("smoke_test.py")

    # Summary
    print(flush=True)
    if failures:
        print(f"{BOLD}{RED}=== run_ci.py FAIL ==={RESET}", flush=True)
        for f in failures:
            print(f"  {RED}[FAIL] {f}{RESET}", flush=True)
        print(flush=True)
        print(
            "Fix the issues above, then re-run:\n"
            "  python scripts/run_ci.py",
            flush=True,
        )
        return 1

    print(f"{BOLD}{GREEN}=== run_ci.py PASS ==={RESET}", flush=True)
    print(
        "\nReady to push. After the push, run:\n"
        "  python scripts/run_smoke.py   (waits for Railway rollout, then 31 local + 9 prod tests)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
