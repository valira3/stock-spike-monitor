"""v5.10.3 \u2014 Startup smoke test.

Catches the boot regression that v5.10.1 shipped (PR #189): trade_genius.py
acquired three new top-level imports (`eye_of_tiger`, `volume_bucket`,
`v5_10_1_integration`) but the per-file `COPY` directives in `Dockerfile`
were not updated, so the Railway container crashed with
`ModuleNotFoundError` on boot and entered a restart-loop. Local CI passed
because the local filesystem has every .py file regardless of what
Dockerfile copies.

Two checks here, both fast (< 2s):

1. ``test_trade_genius_imports_clean_with_smoke_env`` \u2014 imports
   trade_genius.py with SSM_SMOKE_TEST=1 to confirm every top-level
   import resolves and nothing hangs the main thread before the web
   server bind. A regression here would manifest as ImportError or a
   hang past the timeout.

2. ``test_dockerfile_copies_every_top_level_python_module`` \u2014 the
   strict guard. Parses the import graph of trade_genius.py (every
   sibling .py module imported at top level) and the COPY lines in
   Dockerfile, and fails if any imported module is missing a COPY.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _local_module_imports(py_path: Path) -> set[str]:
    """Return the set of `import X` / `from X import ...` names in
    ``py_path`` whose target is a sibling .py file in the same repo.
    Standard-library and third-party imports are filtered out by
    requiring the module name to match an actual `<name>.py` file at
    the repo root.
    """
    src = py_path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(py_path))
    sibling_modules = {p.stem for p in REPO_ROOT.glob("*.py") if p.name != py_path.name}
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in sibling_modules:
                    found.add(top)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module:
                top = node.module.split(".")[0]
                if top in sibling_modules:
                    found.add(top)
    return found


def _dockerfile_copied_modules(dockerfile: Path) -> set[str]:
    """Return the set of top-level Python module names that the
    Dockerfile explicitly COPYs into the container image (e.g.
    ``COPY trade_genius.py .`` -> {'trade_genius'}). Wildcarded copies
    like ``COPY *.py .`` would also satisfy the contract; if such a
    pattern is detected the function returns the sentinel ``{"*"}``.
    """
    out: set[str] = set()
    for raw_line in dockerfile.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"COPY\s+([^\s]+(?:\s+[^\s]+)*)\s+\.\s*$", line)
        if not m:
            continue
        for token in m.group(1).split():
            if token in ("--chown", ".") or token.startswith("--"):
                continue
            base = os.path.basename(token)
            if base in ("*.py", "*"):
                out.add("*")
                continue
            if base.endswith(".py"):
                out.add(base[: -len(".py")])
    return out


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------


def test_trade_genius_imports_clean_with_smoke_env(monkeypatch):
    """Imports the bot module with SSM_SMOKE_TEST=1 and confirms every
    top-level statement resolves without raising. This is the check
    that would have caught v5.10.1 if it had run inside the container
    image: ``import eye_of_tiger`` fails fast with ModuleNotFoundError,
    so this test would have flagged the missing Dockerfile COPY.
    """
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    sys.path.insert(0, str(REPO_ROOT))
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius

    assert trade_genius.BOT_VERSION, "BOT_VERSION must be non-empty"


def test_dockerfile_copies_every_top_level_python_module():
    """Strict guard. Every sibling .py module imported (top-level OR
    lazily inside a function/try block) by ``trade_genius.py`` or
    ``dashboard_server.py`` must also be COPYed by the Dockerfile,
    otherwise the production container will crash with
    ModuleNotFoundError on first execution. This is the v5.10.1
    root-cause regression \u2014 extended in v5.13.3 to cover
    ``dashboard_server.py`` after v5.13.2 shipped a lazy
    ``import v5_13_2_snapshot`` inside ``snapshot()`` that was missed
    by the trade_genius-only check.
    """
    dockerfile = REPO_ROOT / "Dockerfile"
    copied = _dockerfile_copied_modules(dockerfile)
    if "*" in copied:
        return
    for entry_module in ("trade_genius.py", "dashboard_server.py"):
        imported = _local_module_imports(REPO_ROOT / entry_module)
        missing = sorted(imported - copied)
        assert not missing, (
            f"{entry_module} imports modules that are NOT COPYed by "
            "the Dockerfile. The Railway container will crash with "
            "ModuleNotFoundError. Add a `COPY <name>.py .` line for "
            f"each. Missing: {missing}"
        )


def test_eye_of_tiger_modules_are_present_in_dockerfile():
    """Belt-and-suspenders explicit assertion for the v5.10.1 trio.
    If a future refactor renames or splits these modules, this fails
    loudly so the Dockerfile is updated in lockstep.
    """
    dockerfile = REPO_ROOT / "Dockerfile"
    copied = _dockerfile_copied_modules(dockerfile)
    if "*" in copied:
        return
    for required in ("eye_of_tiger", "volume_bucket", "v5_10_1_integration"):
        assert required in copied, (
            f"Dockerfile missing `COPY {required}.py .` \u2014 the v5.10.1 "
            "live-hot-path integration depends on this module being "
            "in the container image."
        )


@pytest.mark.parametrize("scan_loop_step", ["import_only"])
def test_scan_loop_no_blocking_at_first_call_with_empty_state(
    monkeypatch,
    scan_loop_step,
):
    """Imports trade_genius and confirms that calling key v5.10.1
    integration functions with EMPTY/UNSEEDED state does NOT raise.
    Mirrors the Railway boot scenario where ``/data/bars`` is empty,
    ``_QQQ_REGIME`` is unseeded, and the OR window has not opened.
    """
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    sys.path.insert(0, str(REPO_ROOT))
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius
    import v5_10_1_integration as eot_glue
    from datetime import datetime

    try:
        from zoneinfo import ZoneInfo

        now_et = datetime.now(tz=ZoneInfo("America/New_York"))
    except Exception:
        now_et = datetime.utcnow()
    eot_glue.refresh_volume_baseline_if_needed(now_et)
    eot_glue.maybe_log_permit_state(None, None, None, None)
    permit = eot_glue.evaluate_section_i("LONG", None, None, None, None)
    assert permit.get("open") is False
    boundary = eot_glue.evaluate_boundary_hold_gate("AAPL", "LONG", None, None)
    assert boundary.get("hold") is False
    override = eot_glue.evaluate_section_iv(
        "LONG",
        unrealized_pnl_dollars=0.0,
        current_price=100.0,
        current_1m_open=100.0,
    )
    assert override is None
    # Sanity: the v5.19.x line is what's on the hot path. Asserting an
    # exact version number would require a test edit on every release;
    # the version-bump CI gate already pins the expected value.
    assert trade_genius.BOT_VERSION.startswith("5.25.")


def test_volume_gate_enabled_default_on_when_env_unset(monkeypatch):
    """v5.20.0 \u2014 VOLUME_GATE_ENABLED defaults to True when env unset.

    Tiger Sovereign v15.0 \u00a72/\u00a73 makes the volume gate a primary
    Phase-2 permit (1m volume >= 100% of 55-bar avg, REQUIRED after
    10:00 AM ET). Production default is therefore ON; only an explicit
    operator override disables the gate. This pin replaces the
    pre-v5.20.0 \"default OFF\" guard and protects against the
    constant flipping back.
    """
    monkeypatch.delenv("VOLUME_GATE_ENABLED", raising=False)
    sys.path.insert(0, str(REPO_ROOT))
    # Force re-import so the module-level read of os.environ honors the
    # monkeypatched (deleted) env var rather than a previously-cached
    # value from another test. We must drop both the sys.modules entry
    # AND the parent package attribute, otherwise the ``from engine
    # import feature_flags`` form resolves through the still-bound
    # parent attribute and returns the previously-loaded module.
    if "engine.feature_flags" in sys.modules:
        del sys.modules["engine.feature_flags"]
    import engine as _engine_pkg  # noqa: F401

    if hasattr(_engine_pkg, "feature_flags"):
        delattr(_engine_pkg, "feature_flags")
    from engine import feature_flags as ff

    assert ff.VOLUME_GATE_ENABLED is True, (
        "VOLUME_GATE_ENABLED must default to True (gate ENABLED) when "
        "the env var is unset (v15.0 spec primary permit). Got False \u2014 "
        "production default has regressed to pre-v5.20.0 behavior."
    )
