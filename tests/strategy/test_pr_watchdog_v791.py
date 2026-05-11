"""v7.91.0 -- unit tests for tools.pr_watchdog.

The watchdog has two pure-logic surfaces we can exercise without
hitting GitHub: `_classify_pr` (decides whether a single PR is
actionable) and `_render_finding_block` (formats the comment body).
The HTTP path (_gh, _list_open_prs, ...) is covered by the workflow
itself in production.
"""

from tools.pr_watchdog import _classify_pr, _render_finding_block


def _check(name: str, conclusion: str = "success", status: str = "completed") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion}


def test_classify_draft_pr_is_never_actionable():
    pr = {"draft": True, "merged": False, "mergeable_state": "clean"}
    assert _classify_pr(pr, [_check("pytest"), _check("docker-boot")]) is None


def test_classify_merged_pr_is_never_actionable():
    pr = {"draft": False, "merged": True, "mergeable_state": "clean"}
    assert _classify_pr(pr, [_check("pytest")]) is None


def test_classify_all_green_clean_returns_green_unmerged():
    pr = {"draft": False, "merged": False, "mergeable_state": "clean"}
    result = _classify_pr(pr, [_check("pytest"), _check("docker-boot")])
    assert result is not None
    assert result[0] == "green_unmerged"


def test_classify_any_failure_returns_failing_check():
    pr = {"draft": False, "merged": False, "mergeable_state": "dirty"}
    result = _classify_pr(
        pr,
        [_check("pytest"), _check("docker-boot", conclusion="failure")],
    )
    assert result is not None
    assert result[0] == "failing_check"
    assert "docker-boot" in result[1]


def test_classify_pending_checks_not_actionable():
    pr = {"draft": False, "merged": False, "mergeable_state": "clean"}
    result = _classify_pr(
        pr,
        [_check("pytest", status="in_progress", conclusion=None)],
    )
    assert result is None


def test_classify_mergeable_unknown_with_green_not_actionable():
    """`mergeable_state` other than 'clean' is non-mergeable today
    (dirty, blocked, behind, unstable, unknown). Only 'clean' means
    GitHub will accept the merge call without complaint.
    """
    pr = {"draft": False, "merged": False, "mergeable_state": "behind"}
    result = _classify_pr(pr, [_check("pytest")])
    assert result is None


def test_classify_neutral_and_skipped_treated_as_green():
    pr = {"draft": False, "merged": False, "mergeable_state": "clean"}
    result = _classify_pr(
        pr,
        [_check("pytest"), _check("docs-skip", conclusion="skipped")],
    )
    assert result is not None
    assert result[0] == "green_unmerged"


def test_render_pr_findings_section():
    findings = [
        {
            "kind": "green_unmerged",
            "number": 600,
            "summary": "all checks green, mergeable=clean",
            "url": "https://github.com/x/y/pull/600",
        },
        {
            "kind": "failing_check",
            "number": 601,
            "summary": "failing: docker-boot",
            "url": "https://github.com/x/y/pull/601",
        },
    ]
    out = _render_finding_block(findings)
    assert "Pull requests needing attention" in out
    assert "PR #600" in out
    assert "GREEN, unmerged" in out
    assert "PR #601" in out
    assert "FAILING" in out


def test_render_monitor_issue_findings_section():
    findings = [
        {
            "kind": "new_monitor_issue",
            "number": 700,
            "summary": "[dashboard-monitor] 2 invariant violation(s)",
            "url": "https://github.com/x/y/issues/700",
        },
    ]
    out = _render_finding_block(findings)
    assert "New dashboard-monitor issue(s)" in out
    assert "#700" in out


def test_render_mixed_findings_keeps_both_sections():
    findings = [
        {
            "kind": "green_unmerged",
            "number": 600,
            "summary": "all checks green, mergeable=clean",
            "url": "https://github.com/x/y/pull/600",
        },
        {
            "kind": "new_monitor_issue",
            "number": 700,
            "summary": "[dashboard-monitor] 2 violation(s)",
            "url": "https://github.com/x/y/issues/700",
        },
    ]
    out = _render_finding_block(findings)
    assert "Pull requests needing attention" in out
    assert "New dashboard-monitor issue(s)" in out
    assert out.index("Pull requests needing attention") < out.index(
        "New dashboard-monitor issue(s)"
    )
