"""Pytest config for the repo's test suite.

Registers custom markers used by the Tiger Sovereign rule-compliance
scaffold. The ``spec_gap`` marker is used by the vAA-1 (v5.15.0)
migration to mark tests for rules whose implementation is scheduled
for a specific later PR in the series. By default these tests are
SKIPPED so the migration's PR-1 (spec adoption + test scaffold) can
merge with green CI even though most behavioural rules are not yet
implemented. Pass ``--run-spec-gaps`` to a pytest invocation to
actually execute them (each later PR's CI run uses this flag to verify
the rules it claims to close are now passing).
"""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-spec-gaps",
        action="store_true",
        default=False,
        help="Run tests marked @pytest.mark.spec_gap (default: skip them).",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "spec_gap(pr, rule_id): Test for spec rule not yet implemented; closed by named PR.",
    )
    # v6.14.4: slow marker. Tests in this group are skipped by default
    # in scripts/preflight.sh (which sets PYTEST_SKIP_SLOW=1) so the
    # per-PR fast path runs in seconds rather than minutes. CI and
    # explicit `pytest -m slow` invocations still execute them. The 11
    # currently-marked tests collectively account for ~70s of the
    # previous ~90s preflight wall time; subprocess-based replay,
    # aiohttp E2E setup/teardown, and intentional sleep-then-timeout
    # safe_check assertions dominate that budget.
    config.addinivalue_line(
        "markers",
        "slow: Test takes >= 1s wall; skipped by default in preflight (set PYTEST_SKIP_SLOW=0 to include).",
    )


def pytest_collection_modifyitems(config, items):
    skip_spec_gap = pytest.mark.skip(
        reason="spec_gap: rule scheduled for a later PR in the vAA-1 migration; pass --run-spec-gaps to execute",
    )
    skip_slow = pytest.mark.skip(
        reason="slow: skipped by default in preflight; set PYTEST_SKIP_SLOW=0 or run `pytest -m slow` to include.",
    )
    skip_spec_gaps_enabled = not config.getoption("--run-spec-gaps")
    import os
    skip_slow_enabled = os.environ.get("PYTEST_SKIP_SLOW", "0") == "1"

    for item in items:
        if skip_spec_gaps_enabled and item.get_closest_marker("spec_gap") is not None:
            item.add_marker(skip_spec_gap)
        if skip_slow_enabled and item.get_closest_marker("slow") is not None:
            item.add_marker(skip_slow)
