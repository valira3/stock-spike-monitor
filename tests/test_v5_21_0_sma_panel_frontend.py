# tests/test_v5_21_0_sma_panel_frontend.py
# v5.21.0 -- Daily SMA Stack panel: frontend checks
# No em-dashes in this file (constraint for .py test files).
import re
import os

APP_JS = os.path.join(os.path.dirname(__file__), "..", "dashboard_static", "app.js")
APP_CSS = os.path.join(os.path.dirname(__file__), "..", "dashboard_static", "app.css")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# 1. Function exists
# ---------------------------------------------------------------------------
def test_sma_panel_function_exists():
    """_pmtxSmaStackPanel must be defined as a function in app.js."""
    js = _read(APP_JS)
    # Accept either ES5 function declaration or arrow / assigned form
    assert re.search(
        r"function\s+_pmtxSmaStackPanel\s*\(",
        js,
    ), "_pmtxSmaStackPanel function definition not found in app.js"


# ---------------------------------------------------------------------------
# 2. Null guard
# ---------------------------------------------------------------------------
def test_sma_panel_handles_null():
    """The function body must contain an early-return path for null sma_stack."""
    js = _read(APP_JS)
    # Look for a guard such as: if (!smaStack) { return ...
    assert re.search(
        r"if\s*\(\s*!smaStack",
        js,
    ), "No null-guard (if (!smaStack ...)) found in _pmtxSmaStackPanel"


# ---------------------------------------------------------------------------
# 3. All 5 windows referenced
# ---------------------------------------------------------------------------
def test_sma_panel_renders_all_5_windows():
    """The function must reference each of the 5 SMA windows: 12, 22, 55, 100, 200."""
    js = _read(APP_JS)
    for window in (12, 22, 55, 100, 200):
        # Accept numeric literals or class-name strings like 'sw-12'
        pattern = r"(sw-" + str(window) + r"|[^0-9]" + str(window) + r"[^0-9])"
        assert re.search(pattern, js), "Window " + str(window) + " not referenced in app.js"


# ---------------------------------------------------------------------------
# 4. Classification pill covers all three classes
# ---------------------------------------------------------------------------
def test_sma_classification_pill():
    """The function must reference 'bullish', 'bearish', and at least one mixed substate."""
    js = _read(APP_JS)
    assert "'bullish'" in js or '"bullish"' in js, "'bullish' not found in app.js"
    assert "'bearish'" in js or '"bearish"' in js, "'bearish' not found in app.js"
    mixed_substates = (
        "all_above",
        "all_below",
        "above_short_below_long",
        "below_short_above_long",
        "scrambled",
    )
    found_mixed = any(("'" + s + "'" in js or '"' + s + '"' in js) for s in mixed_substates)
    assert found_mixed, "None of the mixed substate strings found in app.js"


# ---------------------------------------------------------------------------
# 5. Order-line rendering
# ---------------------------------------------------------------------------
def test_sma_order_line_renders():
    """The function must reference order-chip, order-op, a check mark, and order_relations."""
    js = _read(APP_JS)
    assert "order-chip" in js, "order-chip not found in app.js"
    assert "order-op" in js, "order-op not found in app.js"
    # Accept U+2713, literal '\u2713', or the word OK as a check indicator
    has_check = (
        "\u2713" in js  # actual UTF-8 checkmark
        or "\\u2713" in js  # escaped unicode in a string literal
        or "'OK'" in js
        or '"OK"' in js
    )
    assert has_check, "Check character (\\u2713 / OK) not found in app.js"
    assert "order_relations" in js, "order_relations field not consumed in app.js"


# ---------------------------------------------------------------------------
# 6. Wired into detail-inner builder
# ---------------------------------------------------------------------------
def test_sma_panel_wired_into_detail_inner():
    """_pmtxSmaStackPanel must be called inside the detail-inner builder."""
    js = _read(APP_JS)
    # Verify the call exists at all
    assert "_pmtxSmaStackPanel(smaStack)" in js, (
        "_pmtxSmaStackPanel(smaStack) call not found in app.js"
    )
    # The call must appear AFTER the _pmtxComponentGrid call site and BEFORE the
    # closing of the detail panel (sentinelStripHtml line).
    comp_grid_pos = js.find("_pmtxComponentGrid(")
    sma_call_pos = js.find("_pmtxSmaStackPanel(smaStack)")
    sentinel_pos = js.find("sentinelStripHtml")
    assert comp_grid_pos != -1, "_pmtxComponentGrid not found"
    assert sma_call_pos != -1, "_pmtxSmaStackPanel call not found"
    assert sentinel_pos != -1, "sentinelStripHtml not found"
    assert comp_grid_pos < sma_call_pos, (
        "_pmtxSmaStackPanel call must appear after the comp-grid call"
    )
    assert sma_call_pos < sentinel_pos, (
        "_pmtxSmaStackPanel call must appear before sentinelStripHtml"
    )


# ---------------------------------------------------------------------------
# 7. CSS classes present
# ---------------------------------------------------------------------------
def test_sma_css_classes_present():
    """Required .pmtx-sma-* CSS classes must be present in app.css."""
    css = _read(APP_CSS)
    required_selectors = [
        ".pmtx-sma-section",
        ".pmtx-sma-table",
        ".pmtx-sma-swatch",
        ".pmtx-sma-stack-pill",
        ".pmtx-sma-sw-12",
        ".pmtx-sma-sw-22",
        ".pmtx-sma-sw-55",
        ".pmtx-sma-sw-100",
        ".pmtx-sma-sw-200",
    ]
    for sel in required_selectors:
        assert sel in css, "CSS selector '" + sel + "' not found in app.css"


# ---------------------------------------------------------------------------
# 8. Track A hotfix preserved
# ---------------------------------------------------------------------------
def test_v5_21_0_css_hotfix_preserved():
    """Track A's removal of .card-body.flush overflow-x:auto must still be present."""
    css = _read(APP_CSS)
    # Track A removed redundant overflow-x:auto on .card-body.flush at phone
    # breakpoint.  The sentinel comment from that change must still exist.
    assert "v5.21.0" in css, "v5.21.0 sentinel comment missing from app.css"
    # The actual .card-body.flush rule at line ~150 (global, not phone) must
    # still only set padding:0 and must NOT contain overflow-x:auto
    # inside the @media (max-width: ...) block.
    media_block_match = re.search(
        r"@media\s*\(max-width\s*:\s*\d+px\s*\)(.*?)(?=@media|\Z)",
        css,
        re.DOTALL,
    )
    # The hotfix comment says overflow-x:auto was REMOVED; make sure it isn't
    # re-introduced on .card-body.flush inside a media block.
    if media_block_match:
        media_content = media_block_match.group(1)
        assert not re.search(
            r"\.card-body\.flush\s*\{[^}]*overflow-x\s*:\s*auto",
            media_content,
        ), ".card-body.flush { overflow-x: auto was re-introduced inside a @media block"
