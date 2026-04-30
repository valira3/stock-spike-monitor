"""v5.20.3 \u2014 Permit Matrix expanded view: pipeline component card grid.

The expanded permit-matrix row used to dump the verbatim Tiger Sovereign
v15.0 spec text inside every ticker (16 ``<dt>/<dd>`` pairs). v5.20.3
replaces that with a responsive component card grid \u2014 each card is one
pipeline component (Phase 1/2/3, an alarm, or the strike counter) showing
a phase chip, name, short description, status badge, and numeric value.

These tests are string-level audits of ``dashboard_static/app.js`` and
``dashboard_static/app.css`` so the wiring cannot regress silently.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_JS = REPO_ROOT / "dashboard_static" / "app.js"
APP_CSS = REPO_ROOT / "dashboard_static" / "app.css"


def _read_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _read_css() -> str:
    return APP_CSS.read_text(encoding="utf-8")


def test_component_grid_helper_defined():
    """The new helper ``_pmtxComponentGrid(d)`` must exist."""
    src = _read_js()
    assert "function _pmtxComponentGrid(d)" in src, "_pmtxComponentGrid helper missing"


def test_expanded_panel_calls_component_grid_helper():
    """The expanded detail panel must call the new helper instead of\n    inlining the retired v15.0 ``<dl>``."""
    src = _read_js()
    # The helper is invoked with the live per-ticker payload.
    assert "_pmtxComponentGrid({" in src, "expanded panel must call _pmtxComponentGrid"
    # Required payload keys must all be passed.
    for key in (
        "tkr:",
        "longPermit:",
        "shortPermit:",
        "orb:",
        "vol:",
        "volStatus:",
        "adx:",
        "di5:",
        "di5Val:",
        "strikesUsed:",
        "pos:",
        "p4:",
    ):
        assert key in src, f"_pmtxComponentGrid call missing key {key!r}"


def test_retired_spec_defs_block_is_gone():
    """The verbose v15.0 spec ``<dl>`` must no longer be emitted."""
    src = _read_js()
    css = _read_css()
    # No more pmtx-spec-defs HTML or CSS class.
    assert "pmtx-spec-defs" not in src, "retired pmtx-spec-defs HTML still present in app.js"
    assert "pmtx-spec-defs" not in css, "retired pmtx-spec-defs CSS still present in app.css"
    # A representative spec-text fragment from the retired block.
    assert "Tiger Sovereign v15.0 \\u00b7 spec definitions" not in src


def test_grid_emits_eight_components():
    """The grid surfaces 8 cards: P1 Weather, P2 Boundary, P2 Volume,\n    P3 Authority, P3 Momentum, AL Sov.Brake, AL Velocity Fuse, POS Strikes."""
    src = _read_js()
    # Each card is defined by its `card("CHIP", "Name", "desc", state, val)`
    # invocation inside _pmtxComponentGrid.
    expected_cards = [
        ('"P1"', '"Weather"'),
        ('"P2"', '"Boundary"'),
        ('"P2"', '"Volume"'),
        ('"P3"', '"Authority"'),
        ('"P3"', '"Momentum"'),
        ('"AL"', '"Sov. Brake"'),
        ('"AL"', '"Velocity Fuse"'),
        ('"POS"', '"Strikes"'),
    ]
    for chip, name in expected_cards:
        # The chip and name appear on the same `card(...)` line.
        assert chip in src and name in src, f"missing card chip={chip} name={name}"
    # Spot-check the short descriptions (no verbatim spec text).
    for desc in (
        '"QQQ regime + AVWAP"',
        '"Two consec 1m closes thru OR"',
        '"5m DI\\u00b1 > 25"',
        '"5m ADX > 20"',
        '"Per-position $ stop"',
        '"Per-position velocity stop"',
        '"Strikes used today (cap 3)"',
    ):
        assert desc in src, f"missing card description {desc!r}"


def test_grid_card_html_uses_required_classes():
    """Every card's markup must wear the expected class hooks so CSS\n    state tints apply."""
    src = _read_js()
    # The card grid markup uses single-quoted JS string literals; check
    # for the exact substring each class hook generates in the rendered
    # HTML.
    for cls in (
        "pmtx-comp-grid",
        "pmtx-comp-head-line",
        "pmtx-comp-cards",
        "pmtx-comp-card pmtx-comp-",
        "pmtx-comp-head",
        "pmtx-comp-chip",
        "pmtx-comp-name",
        "pmtx-comp-desc",
        "pmtx-comp-state",
        "pmtx-comp-badge",
        "pmtx-comp-val",
    ):
        assert cls in src, f"app.js missing class hook {cls!r}"


def test_grid_state_classes_defined_in_css():
    """Every state tint used by the helper must have a CSS rule."""
    css = _read_css()
    for state in (
        "pmtx-comp-pass",
        "pmtx-comp-fail",
        "pmtx-comp-warn",
        "pmtx-comp-pend",
        "pmtx-comp-off",
        "pmtx-comp-safe",
        "pmtx-comp-trip",
        "pmtx-comp-inpos",
        "pmtx-comp-locked",
        "pmtx-comp-used",
        "pmtx-comp-idle",
    ):
        assert "." + state in css, f"app.css missing state tint .{state}"


def test_grid_responsive_breakpoints_defined():
    """The card grid must reflow at desktop/tablet/narrow/mobile widths."""
    css = _read_css()
    # Default desktop: 4 columns.
    assert ".pmtx-comp-cards {" in css
    assert "grid-template-columns: repeat(4, 1fr)" in css
    # Tablet 1024px: 3 columns.
    assert "@media (max-width: 1024px)" in css
    assert "grid-template-columns: repeat(3, 1fr)" in css
    # Narrow 720px: 2 columns.
    assert "@media (max-width: 720px)" in css
    assert "grid-template-columns: repeat(2, 1fr)" in css
    # Mobile 480px: 1 column.
    assert "@media (max-width: 480px)" in css


def test_position_only_cards_show_off_when_no_position():
    """Alarm A / Alarm B cards must collapse to ``OFF`` when ``pos`` is\n    falsy. The branch must be present in the helper source."""
    src = _read_js()
    # The "no pos" labelling for both alarm cards.
    helper_idx = src.find("function _pmtxComponentGrid(d)")
    assert helper_idx >= 0
    helper_block = src[helper_idx : helper_idx + 5000]
    # Both alarms must short-circuit on `!d.pos`.
    no_pos_branches = helper_block.count("if (!d.pos)")
    assert no_pos_branches >= 2, (
        f"expected 2 `if (!d.pos)` branches (one per alarm), got {no_pos_branches}"
    )
    assert '"no pos"' in helper_block


def test_strikes_card_reflects_strike_counter_states():
    """The Strikes card must distinguish IN POS, LOCKED (3/3), USED, IDLE."""
    src = _read_js()
    helper_idx = src.find("function _pmtxComponentGrid(d)")
    helper_block = src[helper_idx : helper_idx + 5000]
    assert '"inpos"' in helper_block
    assert '"locked"' in helper_block
    assert '"used"' in helper_block
    assert '"idle"' in helper_block
    # The locked label is "3/3 \u00b7 locked".
    assert "3/3 \\u00b7 locked" in helper_block


def test_expand_handler_still_wired_at_body_level():
    """The delegated click handler (body.__pmtxExpandWired) must remain\n    intact \u2014 v5.20.3 only changed the inner expanded markup, not the\n    expand/collapse plumbing."""
    src = _read_js()
    assert "body.__pmtxExpandWired" in src
    assert "tr.pmtx-row[data-pmtx-tkr]" in src
    assert "pmtx-detail-open" in src


def test_has_detail_gate_unchanged():
    """``hasDetail`` is still ``pos || lastFill || proxHasDetail`` \u2014\n    v5.20.3 must not have regressed the v5.19.3 fix."""
    src = _read_js()
    assert "const hasDetail = !!(pos || lastFill || proxHasDetail);" in src
