"""
tests/test_v5_21_0_mobile_hscroll_fix.py

Regression tests for the v5.21.0 mobile double-horizontal-scroll fix.

The fix removes the redundant overflow-x:auto on .card-body.flush inside the
@media (max-width: 640px) block. That declaration caused a double horizontal
scroll bar on iPhone because .pmtx-table-wrap already owns its own scroller.
"""

import re
import pathlib

CSS_PATH = pathlib.Path(__file__).parent.parent / "dashboard_static" / "app.css"


def _read_css():
    return CSS_PATH.read_text(encoding="utf-8")


def _strip_css_comments(text):
    """Remove /* ... */ block comments (including multi-line) from CSS text."""
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def _extract_phone_breakpoint_block(css_text):
    """
    Return the content of the @media (max-width: 640px) { ... } block.
    Uses brace counting to handle nested blocks correctly.
    """
    marker = "@media (max-width: 640px)"
    start = css_text.find(marker)
    if start == -1:
        return ""
    brace_start = css_text.index("{", start)
    depth = 0
    i = brace_start
    for i, ch in enumerate(css_text[brace_start:], brace_start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
    return css_text[brace_start : i + 1]


def test_no_card_body_flush_overflow_at_phone_breakpoint():
    """
    The .card-body.flush {{ overflow-x: auto }} declaration must NOT exist
    inside the @media (max-width: 640px) block at the phone breakpoint.

    Strip CSS comments first so the explanatory comment we inserted does not
    accidentally trigger the assertion.
    """
    raw_css = _read_css()
    phone_block = _extract_phone_breakpoint_block(raw_css)
    assert phone_block, "Could not find @media (max-width: 640px) block in app.css"

    # Remove comments so the explanatory comment does not cause a false positive.
    phone_block_no_comments = _strip_css_comments(phone_block)

    forbidden = ".card-body.flush { overflow-x: auto"
    assert forbidden not in phone_block_no_comments, (
        "Found redundant .card-body.flush overflow-x:auto inside the "
        "@media (max-width: 640px) block -- the double-scroll regression "
        "has been re-introduced."
    )


def test_pmtx_table_wrap_still_owns_scroll():
    """
    .pmtx-table-wrap must still have an overflow-x: auto declaration
    somewhere in app.css. This verifies we did not accidentally remove
    the legitimate scroller while applying the hotfix.
    """
    raw_css = _read_css()
    # Strip comments so we only match real declarations, not comment text.
    css_text = _strip_css_comments(raw_css)

    # Find all occurrences of the selector (after comment stripping).
    occurrences = [m.start() for m in re.finditer(r"\.pmtx-table-wrap", css_text)]
    assert occurrences, ".pmtx-table-wrap selector not found in app.css"

    # At least one occurrence must have overflow-x: auto within 500 chars
    # following the selector -- that is the actual rule block.
    found_scroll = any("overflow-x: auto" in css_text[idx : idx + 500] for idx in occurrences)
    assert found_scroll, (
        ".pmtx-table-wrap no longer has overflow-x: auto -- "
        "the legitimate scroller may have been accidentally removed."
    )


def test_v5_21_0_explanatory_comment_present():
    """
    The v5.21.0 explanatory comment must be present in app.css so the
    rationale for the removal survives future grep tests and code review.
    """
    css_text = _read_css()
    assert "v5.21.0" in css_text, (
        "v5.21.0 comment not found in app.css -- rationale comment is missing."
    )
    assert "removed redundant overflow-x:auto on .card-body.flush" in css_text, (
        "Expected explanatory comment text not found in app.css."
    )
    assert "pmtx-table-wrap already owns its own scroller" in css_text, (
        "Expected explanatory comment detail not found in app.css."
    )
