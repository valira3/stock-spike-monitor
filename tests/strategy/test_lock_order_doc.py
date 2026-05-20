"""v10.0.1 -- guardrail that docs/lock_order.md stays in sync with the
actual production locks.

This is the lightest possible enforcement of the lock-ordering
convention. It does NOT detect deadlocks at runtime; it only checks
that every `threading.Lock()` or `threading.RLock()` declared in
production code is mentioned in the doc by file path. Maintainers
adding a lock without a doc entry get a fast, clear CI failure.

Detecting actual cycles requires runtime instrumentation that's out
of scope here. The lock_order.md "may-acquire-while-holding" table
is the human review path for new edges.
"""
from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent.parent
DOC_PATH = REPO / "docs" / "lock_order.md"

# Production code paths only. Mirrors the grep filter in the doc's preamble.
# We INCLUDE: top-level .py files, orb/, engine/, executors/, ingest/, broker/,
# telegram_ui/, dashboard_server.py, trade_genius.py.
# We EXCLUDE: tests/, scripts/, tools/, __pycache__/.
PROD_DIRS = (
    "orb", "engine", "executors", "ingest", "broker", "telegram_ui",
)
EXCLUDE_DIRS = ("tests", "scripts", "tools", "__pycache__")
EXCLUDE_TOP = ("smoke_test.py",)

LOCK_DECL_RE = re.compile(
    r"\bthreading\.(?:R?Lock)\s*\(\s*\)",
)


def _is_production(path: Path) -> bool:
    rel = path.relative_to(REPO)
    parts = rel.parts
    # Exclude entire subtrees by first path component.
    if parts and parts[0] in EXCLUDE_DIRS:
        return False
    # Exclude specific top-level helpers.
    if str(rel) in EXCLUDE_TOP:
        return False
    # Allow either top-level .py OR files under PROD_DIRS.
    if len(parts) == 1:
        return parts[0].endswith(".py")
    return parts[0] in PROD_DIRS


def _find_lock_declarations() -> list[tuple[str, int]]:
    """Return list of (relpath, line_number) for every production
    `threading.Lock()` / `threading.RLock()` declaration."""
    out: list[tuple[str, int]] = []
    for p in REPO.rglob("*.py"):
        if not _is_production(p):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if LOCK_DECL_RE.search(line):
                rel = str(p.relative_to(REPO))
                out.append((rel, i))
    return out


def _doc_mentions(doc: str) -> set[str]:
    """Pull the set of `file:line` mentions out of the doc's inventory
    table. Cells look like `orb/live_runtime.py:88` in backtick code spans."""
    return set(re.findall(r"`([^`]+\.py):(\d+)`", doc)) and set(
        f"{f}:{ln}" for f, ln in re.findall(
            r"`([^`]+\.py):(\d+)`", doc
        )
    )


def _doc_files(doc: str) -> set[str]:
    """Files (no line numbers) mentioned in the doc's inventory table.

    Using file-level coverage instead of file:line lets a maintainer
    refactor lock declarations within the same file without forcing
    a doc edit -- as long as the file is still referenced, the lock
    is presumed documented. Adding a NEW production .py file that
    declares a lock still forces a doc update.
    """
    return set(re.findall(r"`([A-Za-z0-9_/.]+\.py):\d+`", doc))


def test_doc_lists_every_file_with_a_production_lock():
    """A new file that declares a lock without a doc entry fails CI."""
    doc = DOC_PATH.read_text(encoding="utf-8")
    documented_files = _doc_files(doc)
    code_locks = _find_lock_declarations()
    files_with_locks = {f for f, _ in code_locks}
    undocumented = files_with_locks - documented_files
    assert not undocumented, (
        "These production .py files declare a threading.Lock/RLock but are "
        "not mentioned in docs/lock_order.md:\n  "
        + "\n  ".join(sorted(undocumented))
        + "\n\nAdd them to the inventory table in docs/lock_order.md."
    )


def test_doc_does_not_reference_files_without_locks():
    """A lock removed from code but left in the doc creates stale
    documentation. The doc-test catches it."""
    doc = DOC_PATH.read_text(encoding="utf-8")
    documented_files = _doc_files(doc)
    code_locks = _find_lock_declarations()
    files_with_locks = {f for f, _ in code_locks}
    stale = documented_files - files_with_locks
    assert not stale, (
        "These files are listed in docs/lock_order.md inventory but no "
        "longer declare a threading.Lock/RLock in production code:\n  "
        + "\n  ".join(sorted(stale))
        + "\n\nRemove the stale entry from docs/lock_order.md."
    )


def test_doc_has_acquire_while_holding_section():
    """The 'May-acquire-while-holding' table is the actual deadlock-
    prevention review surface. The doc must keep it."""
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert "may-acquire-while-holding" in doc.lower() or \
           "acquire-while-holding" in doc.lower() or \
           "Acquires while holding" in doc, (
        "docs/lock_order.md is missing the multi-lock acquisition table. "
        "Restore it."
    )


def test_lock_count_in_bounds():
    """Detect a sudden lock-count explosion (which would either be a
    legitimate large feature OR a refactor that escaped review)."""
    code_locks = _find_lock_declarations()
    n = len(code_locks)
    # As of v10.0.1, the count is 34. Allow drift ±10 without failing
    # the test; a larger swing is worth a human look.
    assert 20 <= n <= 60, (
        f"Production lock count {n} is outside the expected band [20, 60]. "
        "This is a guardrail -- either a real feature added many locks "
        "(update the band) or a refactor escaped review (look at the diff)."
    )
