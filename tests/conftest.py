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


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-spec-gaps"):
        return
    skip_spec_gap = pytest.mark.skip(
        reason="spec_gap: rule scheduled for a later PR in the vAA-1 migration; pass --run-spec-gaps to execute"
    )
    for item in items:
        if item.get_closest_marker("spec_gap") is not None:
            item.add_marker(skip_spec_gap)
