"""v5.20.8 \u2014 Authority green-on-either-side + table column rename tests.

Six behaviors must hold after the v5.20.8 dashboard tweaks:

1. ``BOT_VERSION`` is ``5.20.8`` in ``bot_version.py``.

2. The Authority card ``p3aState`` block sources its booleans from
   ``section_i_permit.long_open`` and ``section_i_permit.short_open``
   (read into ``_sip``), and the four val branches are
   ``\"long+short\"`` / ``\"long\"`` / ``\"short\"`` / ``\"none\"``. v5.20.7
   surfaced the rows but didn't go green when only one side was
   permitted; v5.20.8 follows the gate (``long_open OR short_open``)
   so the card matches reality.

3. Two new helpers exist next to ``_pmtxGateCell``:
   ``_pmtxAuthorityCell(sip)`` returning ``true``/``false``/``null``
   and ``_pmtxAuthorityTooltip(sip)`` returning a descriptive string.
   The table body Authority cell must call them via
   ``_pmtxGateCell(_pmtxAuthorityCell(sectionIPermit), _pmtxAuthorityTooltip(sectionIPermit))``.

4. The four component table column headers are renamed to match the
   card vocabulary above the table: ``ORB \u2192 Boundary``,
   ``Trend \u2192 Momentum``, ``5m DI\u00b1 \u2192 Authority``,
   ``Vol \u2192 Volume``. CSS class names (``.pmtx-col-orb``,
   ``.pmtx-col-adx``, ``.pmtx-col-diplus``, ``.pmtx-col-vol``) are
   unchanged for layout continuity, only the visible ``<th>`` text
   changes. Legacy header text must be absent from executable JS.

5. The ``data-pmtx-comp-grid`` version marker is bumped to
   ``v5.20.8`` so devtools-driven verification can confirm the
   hotfix shipped without hitting ``/api/version``.

6. The previous Authority card description ``Permit & QQQ alignment``
   is preserved (v5.20.7 contract \u2014 v5.20.8 only changes the
   green/red logic, not the tagline).

Source-grep assertions because the dashboard JS runs in the browser,
not the Python test runner. JS block + line comments are stripped
before any legacy-token check so the rationale comment blocks (which
mention the legacy names on purpose) don't trip the guards.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_JS = REPO_ROOT / "dashboard_static" / "app.js"
BOT_VERSION_PY = REPO_ROOT / "bot_version.py"


def _read_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _strip_js_comments(src: str) -> str:
    """Remove /* ... */ and // ... so legacy-token scans only see code."""
    no_block = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", no_block)


def test_bot_version_is_5_20_8():
    text = BOT_VERSION_PY.read_text(encoding="utf-8")
    assert 'BOT_VERSION = "5.21.1"' in text, "bot_version.py must report 5.21.1"


def test_p3a_state_uses_or_semantics_with_sip_booleans():
    """p3aState must follow the gate: long_open OR short_open => pass.
    Pre-hotfix v5.20.7 surfaced the per-side rows but the top-line state
    pill required a stricter mental model than the gate enforces.
    """
    js = _strip_js_comments(_read_js())
    assert "_sip.long_open" in js, "p3aState must read _sip.long_open"
    assert "_sip.short_open" in js, "p3aState must read _sip.short_open"
    # Four explicit val branches match the Weather card style.
    for branch in ('"long+short"', '"long"', '"short"', '"none"'):
        assert branch in js, f"p3aVal branch {branch} missing"


def test_authority_cell_and_tooltip_helpers_exist():
    """_pmtxAuthorityCell and _pmtxAuthorityTooltip must exist and the
    table body Authority cell must call them. The cell helper returns
    a tri-state (true / false / null) so it composes with the existing
    _pmtxGateCell renderer that the other column cells already use.
    """
    js = _strip_js_comments(_read_js())
    assert "function _pmtxAuthorityCell" in js, (
        "_pmtxAuthorityCell helper must be defined in app.js"
    )
    assert "function _pmtxAuthorityTooltip" in js, (
        "_pmtxAuthorityTooltip helper must be defined in app.js"
    )
    assert "_pmtxAuthorityCell(sectionIPermit)" in js, (
        "Authority body cell must call _pmtxAuthorityCell(sectionIPermit)"
    )
    assert "_pmtxAuthorityTooltip(sectionIPermit)" in js, (
        "Authority body cell must call _pmtxAuthorityTooltip(sectionIPermit)"
    )


def test_table_column_headers_renamed_to_card_vocabulary():
    """ORB \u2192 Boundary, Trend \u2192 Momentum, 5m DI\u00b1 \u2192
    Authority, Vol \u2192 Volume. CSS classes unchanged."""
    js_raw = _read_js()
    for header in (
        ">Boundary</th>",
        ">Momentum</th>",
        ">Authority</th>",
        ">Volume</th>",
    ):
        assert header in js_raw, f"renamed header {header!r} missing"

    js = _strip_js_comments(js_raw)
    for legacy in (
        ">ORB</th>",
        ">Trend</th>",
        ">5m DI\\u00b1</th>",
        ">Vol</th>",
    ):
        assert legacy not in js, f"legacy column header {legacy!r} still present in executable JS"


def test_pmtx_col_classes_preserved_for_layout():
    """The CSS classes are kept for layout continuity even though the
    visible header text changed. .pmtx-col-diplus in particular is now
    semantically the Authority column; we keep the class name to avoid
    a CSS rewrite."""
    js = _read_js()
    for cls in (
        'class="pmtx-col-orb"',
        'class="pmtx-col-adx"',
        'class="pmtx-col-diplus"',
        'class="pmtx-col-vol"',
    ):
        assert cls in js, f"layout class {cls!r} missing"


def test_data_pmtx_comp_grid_marker_is_5_20_8():
    js = _read_js()
    assert 'data-pmtx-comp-grid="v5.21.1"' in js, "data-pmtx-comp-grid must be bumped to v5.20.9"


def test_authority_card_tagline_unchanged_from_v5_20_7():
    """v5.20.8 is purely a green-on-either-side + table-rename hotfix.
    The Authority card description from v5.20.7 (Permit & QQQ alignment)
    must still be present \u2014 the wiring contract is unchanged."""
    js = _read_js()
    assert "Permit & QQQ alignment" in js, (
        "Authority card tagline 'Permit & QQQ alignment' must be preserved"
    )
