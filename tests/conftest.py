"""Pytest config for the repo's test suite.

Currently only registers custom markers used by the v5.13.0 Tiger
Sovereign rule-compliance scaffold so pytest does not warn when those
markers are encountered.
"""


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "spec_gap(pr, rule_id): Test for spec rule not yet implemented; closed by named PR.",
    )
