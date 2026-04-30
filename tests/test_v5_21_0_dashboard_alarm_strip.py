"""
tests/test_v5_21_0_dashboard_alarm_strip.py

Source-grep tests for v5.21.0 dashboard changes:
  1. Sentinel strip uses vAA-1 labels (A1 Loss, A2 Flash, B Trend Death, etc.)
  2. No em-dash placeholders for cells D and E (real data now flows)
  3. Position row click handler is wired (data-pos-ticker + __pmtxExpandedSet)
  4. Legacy A1/A2 tooltip vocabulary removed from non-comment text
  5. Strip header mentions "parallel" (spec Section 5 architectural rule)
  6. __pmtxApplyExpanded is called from within the position-click handler block

All tests operate on dashboard_static/app.js via text search; no browser
required. Comments are stripped before vocabulary-absence assertions (tests 2
and 4) to avoid false triggers from explanatory comment text.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

APP_JS = Path(__file__).parent.parent / "dashboard_static" / "app.js"


def _read() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """Remove JS line comments (// ...) and block comments (/* ... */)."""
    # Block comments first (non-greedy, DOTALL)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    # Line comments
    src = re.sub(r"//[^\n]*", "", src)
    return src


def _extract_function(src: str, fn_name: str) -> str:
    """Extract the body of a named function (from function keyword to matching
    closing brace). Returns empty string when the function is not found.
    Works for simple non-nested top-level definitions; good enough for grep
    tests since _pmtxSentinelStrip is a flat function."""
    pattern = r"function\s+" + re.escape(fn_name) + r"\s*\("
    m = re.search(pattern, src)
    if not m:
        return ""
    start = m.start()
    # Walk forward to find the matching closing brace.
    depth = 0
    i = src.index("{", start)
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    return src[start:]


# ---------------------------------------------------------------------------
# Test 1: vAA-1 labels present in _pmtxSentinelStrip
# ---------------------------------------------------------------------------


def test_sentinel_strip_uses_vaa1_labels():
    """_pmtxSentinelStrip must contain all six vAA-1 cell label strings."""
    src = _read()
    fn_body = _extract_function(src, "_pmtxSentinelStrip")
    assert fn_body, "_pmtxSentinelStrip function not found in app.js"

    required_labels = [
        "A1 Loss",
        "A2 Flash",
        "B Trend Death",
        "C Vel. Ratchet",
        "D HVP Lock",
        "E Div. Trap",
    ]
    for label in required_labels:
        assert label in fn_body, (
            f"Expected label {label!r} not found inside _pmtxSentinelStrip. "
            "Ensure the vAA-1 rewrite is present."
        )


# ---------------------------------------------------------------------------
# Test 2: No em-dash placeholders for D and E
# ---------------------------------------------------------------------------


def test_sentinel_strip_no_em_dash_placeholders_for_d_e():
    """D and E cells must not use a hardcoded em-dash as their value.

    The old implementation had:
        cell("D", "ADX Collapse", "\\u2014", dState)
        cell("E", "Div. Trap",    "\\u2014", eState)

    After the v5.21.0 rewrite the values come from real backend data.
    We strip JS comments before asserting to avoid false triggers from
    explanatory comment text that may quote the old pattern.
    """
    src = _read()
    fn_body = _extract_function(src, "_pmtxSentinelStrip")
    assert fn_body, "_pmtxSentinelStrip function not found in app.js"

    src_no_comments = _strip_comments(fn_body)

    # The old hardcoded pattern was:  cell("D", ..., "\u2014", ...)
    # We check the em-dash character itself (U+2014) and also its JS literal
    # escape form (\u2014) as a hardcoded second argument to a D cell.
    bad_d_patterns = [
        # literal em-dash as 3rd positional arg immediately following "D HVP Lock"
        r'"D HVP Lock"[^,]*,[^,]*,\s*["\u2014\\u2014]',
        # old label + em-dash
        r'"ADX Collapse"[^,]*,[^,]*,\s*["\u2014\\u2014]',
    ]
    for pat in bad_d_patterns:
        assert not re.search(pat, src_no_comments), (
            f"Pattern {pat!r} found in _pmtxSentinelStrip (after comment strip)."
            " Cell D should use real backend data, not a hardcoded em-dash."
        )

    bad_e_patterns = [
        r'"E Div\. Trap"[^,]*,[^,]*,\s*["\u2014\\u2014]',
        r'"Div\. Trap"[^,]*,[^,]*,\s*["\u2014\\u2014]',
    ]
    for pat in bad_e_patterns:
        assert not re.search(pat, src_no_comments), (
            f"Pattern {pat!r} found in _pmtxSentinelStrip (after comment strip)."
            " Cell E should use real backend data, not a hardcoded em-dash."
        )


# ---------------------------------------------------------------------------
# Test 3: Position-row click handler is wired
# ---------------------------------------------------------------------------


def test_position_row_click_handler_present():
    """app.js must contain:
    - data-pos-ticker attribute on position rows (cooperative render change)
    - a click handler that references __pmtxExpandedSet from a position
      table context (proving the cross-panel wire-up is present)
    """
    src = _read()
    assert "data-pos-ticker" in src, (
        "data-pos-ticker attribute not found in app.js. "
        "renderPositions should add this attribute to each <tr>."
    )
    assert "__pmtxExpandedSet" in src, (
        "__pmtxExpandedSet not referenced in app.js. "
        "The click-to-titan handler must mutate the Permit Matrix expand set."
    )
    # The reference to __pmtxExpandedSet must occur AFTER data-pos-ticker in
    # the same file (the handler is wired below the row render).
    pos_ticker_idx = src.index("data-pos-ticker")
    expanded_set_idx = src.index("__pmtxExpandedSet")
    assert expanded_set_idx > pos_ticker_idx, (
        "__pmtxExpandedSet reference appears before data-pos-ticker. "
        "The click handler should be wired after the row attribute is added."
    )


# ---------------------------------------------------------------------------
# Test 4: No legacy A1/A2 vocabulary in tooltip strings
# ---------------------------------------------------------------------------


def test_no_legacy_a1_a2_in_tooltips():
    """Legacy tooltip strings must not appear in non-comment text.

    Patterns we check for absence (after stripping JS comments):
      - 'Sov. Brake'
      - 'Velocity Fuse'
      - '9-EMA Shield'
      - 'Titan Grip'
      - 'Emergency Shield'
      - 'ADX Collapse'  (old Cell D label)
    """
    src = _read()
    src_no_comments = _strip_comments(src)

    forbidden = [
        "Sov. Brake",
        "Velocity Fuse",
        "9-EMA Shield",
        "Titan Grip",
        "Emergency Shield",
        "ADX Collapse",
    ]
    for term in forbidden:
        assert term not in src_no_comments, (
            f"Legacy tooltip string {term!r} still present in app.js "
            "(after JS comment strip). Rewrite to vAA-1 vocabulary."
        )


# ---------------------------------------------------------------------------
# Test 5: Strip header mentions "parallel"
# ---------------------------------------------------------------------------


def test_strip_header_mentions_parallel():
    """The sentinel strip outer div title tooltip must mention 'parallel'
    to communicate the Section 5 architectural rule (all alarms evaluated
    in parallel, not sequentially)."""
    src = _read()
    fn_body = _extract_function(src, "_pmtxSentinelStrip")
    assert fn_body, "_pmtxSentinelStrip function not found in app.js"
    assert "parallel" in fn_body, (
        "The word 'parallel' not found inside _pmtxSentinelStrip. "
        "The strip header tooltip must describe the parallel-evaluation "
        "architecture per Section 5 of tiger_sovereign_spec_vAA-1.md."
    )


# ---------------------------------------------------------------------------
# Test 6: __pmtxApplyExpanded called from within the position-click handler
# ---------------------------------------------------------------------------


def test_pmtx_apply_expanded_called_from_pos_click():
    """The position-click handler block must call __pmtxApplyExpanded.

    We do a loose structural check: the string __pmtxApplyExpanded must
    appear after data-pos-ticker (set up in renderPositions) and before
    the next top-level function keyword that follows the click handler,
    which is the renderNextScanCountdown / another function.
    """
    src = _read()

    # Locate the click handler block by finding __posClickWired which is
    # unique to the position-click wiring block.
    wired_idx = src.find("__posClickWired")
    assert wired_idx != -1, (
        "__posClickWired sentinel not found in app.js. "
        "The position-click handler must use this once-wire guard."
    )

    # __pmtxApplyExpanded must be called within a reasonable window after
    # __posClickWired appears (within 3000 chars covers the full handler body).
    handler_block = src[wired_idx : wired_idx + 3000]
    assert "__pmtxApplyExpanded" in handler_block, (
        "__pmtxApplyExpanded not found in the position-click handler block "
        "(within 3000 chars of __posClickWired). "
        "The handler must call this function to expand the Titan row."
    )
