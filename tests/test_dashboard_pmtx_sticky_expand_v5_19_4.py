"""v5.19.4 \u2014 sticky expand on the Permit Matrix.

The bug shipped in v5.19.3: the click handler was correct and the detail
row was emitted, but every /api/state SSE push (every 1\u20132s) rebuilt
``body.innerHTML`` and wiped the live ``pmtx-row-expanded`` class. The
operator perceived a click that flashed the panel open and snapped it
closed.

The fix lives in ``dashboard_static/app.js``:

* ``body.__pmtxExpandedSet`` (Set) survives across renders.
* ``_pmtxApplyExpanded()`` re-applies the classes after every render.
* Click handler clears the Set then conditionally re-adds (single-open
  semantics; clicking a different row replaces the prior expansion).
* Document-level click handler clears the Set when the click lands
  outside the matrix body.

These tests are string-level audits because the runtime behavior is
exercised in a Playwright harness; the assertions here pin the patch
shape so a future refactor that loses any piece fails CI loudly.
"""

from __future__ import annotations

from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "dashboard_static" / "app.js"


def _read() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_expanded_set_initializer_present():
    src = _read()
    assert "body.__pmtxExpandedSet = new Set()" in src, (
        "Sticky expand requires a per-body Set living outside the rendered DOM."
    )


def test_apply_expanded_helper_present():
    src = _read()
    assert "function _pmtxApplyExpanded()" in src
    # The helper must toggle BOTH the main row and the detail row.
    assert "pmtx-row-expanded" in src
    assert "pmtx-detail-open" in src


def test_click_handler_uses_single_open_semantics():
    src = _read()
    # The handler should clear the set, then conditionally re-add.
    assert "set.clear()" in src, "Single-open semantics requires clearing the set on every click."
    assert "if (!wasOpen) set.add(tkr)" in src, (
        "Re-clicking the same row must collapse it (don't re-add when it was open)."
    )


def test_outside_click_collapses_expanded():
    src = _read()
    # Document-level listener must clear the set when click lands outside.
    assert 'document.addEventListener("click"' in src
    assert "body.contains(ev.target)" in src, (
        "Outside-click logic must short-circuit on in-matrix clicks via body.contains()."
    )


def test_apply_expanded_called_after_render():
    """The classes are re-applied at the end of renderPermitMatrix(), AFTER
    body.innerHTML is rebuilt. Without this call the Set survives but the
    DOM doesn't reflect it.
    """
    src = _read()
    # The trailing call inside renderPermitMatrix.
    assert "_pmtxApplyExpanded();" in src
    # And the helper is exposed on the body so future refactors can find it.
    assert "body.__pmtxApplyExpanded = _pmtxApplyExpanded" in src
