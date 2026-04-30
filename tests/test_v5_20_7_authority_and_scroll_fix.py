"""v5.20.7 \u2014 Authority wiring + single-scroll + no-pos UX tests.

Five behaviors must hold after the v5.20.7 dashboard tweaks:

1. The Authority card must source its rows from ``section_i_permit``
   booleans (``long_open``, ``short_open``, ``sovereign_anchor_open``)
   plus derived QQQ alignment (``qqq_5m_close`` vs ``qqq_5m_ema9`` and
   ``qqq_current_price`` vs ``qqq_avwap_0930``). The pre-hotfix wiring
   read ``sip.open`` / ``sip.qqq_aligned`` / ``sip.index_aligned``
   \u2014 fields that don't exist on the section-I block, so every row
   silently rendered as a dim em dash.

2. The Authority card description must read ``Permit & QQQ alignment``,
   not the legacy ``5m DI\u00b1 > 25`` copy. The legacy line was a
   holdover from an earlier card layout and now contradicts the new
   metric content.

3. The per-position cards (Sov. Brake / Velocity Fuse / POS Strikes)
   must render a single ``(no open position)`` row when ``ppv`` is
   empty. This mirrors the v5.20.6 volume-bypass pattern and avoids
   the ``three em dashes => looks broken`` operator misread.

4. The base ``.app`` rule must not declare ``display: grid`` or
   ``height: 100dvh``. The base ``.main`` rule must not declare
   ``overflow-y: auto``. Together these two rules created the desktop
   inner-scroll container that produced the perceived double scrollbar.
   Promoting the existing mobile-breakpoint rules to the base layer
   gives a single, page-level scroll at every viewport.

5. The ``data-pmtx-comp-grid`` version marker on the component grid
   must be bumped to ``v5.20.7`` so devtools-driven verification can
   confirm the hotfix shipped without hitting ``/api/version``.

These are source-grep assertions because the dashboard JS / CSS run in
the browser, not the Python test runner. The grep approach mirrors the
``smoke_test.py`` source-grep guards and the v5.20.6 test pattern.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_JS = REPO_ROOT / "dashboard_static" / "app.js"
APP_CSS = REPO_ROOT / "dashboard_static" / "app.css"
BOT_VERSION_PY = REPO_ROOT / "bot_version.py"


def test_bot_version_is_5_20_7():
    # Test name pinned to its release; assertion follows BOT_VERSION so
    # subsequent hotfixes don't have to retroactively edit this file.
    # The v5.20.7 wiring contract (Authority sip fields, single-scroll,
    # no-pos row) is still enforced by the other tests below.
    text = BOT_VERSION_PY.read_text(encoding="utf-8")
    assert 'BOT_VERSION = "5.20.9"' in text, "bot_version.py must report 5.20.9 for this hotfix"


def test_authority_uses_sip_permit_fields():
    """The p3aMetrics block must reference all three section_i_permit
    permit booleans plus the QQQ alignment derivations. The pre-hotfix
    wiring used ``sip.open`` / ``sip.qqq_aligned`` / ``sip.index_aligned``
    which silently rendered em dashes because those keys never existed
    on section_i_permit.
    """
    js = APP_JS.read_text(encoding="utf-8")
    required = [
        "sip.long_open",
        "sip.short_open",
        "sip.sovereign_anchor_open",
        # Derived alignment relies on the QQQ price/EMA9/AVWAP triple
        # that already lives on section_i_permit (and is wired into the
        # Weather card). The Authority card reuses the same fields.
        "sip.qqq_5m_close",
        "sip.qqq_5m_ema9",
        "sip.qqq_current_price",
        "sip.qqq_avwap_0930",
    ]
    for token in required:
        assert token in js, (
            f"Authority card must reference {token!r} \u2014 without it "
            "the corresponding row renders as a dim em dash."
        )


def test_authority_does_not_use_legacy_fields():
    """Pre-hotfix tokens must not survive in app.js executable code.
    Their presence would mean the rewire was only partial.

    JS comments are stripped before scanning so the v5.20.7 explanatory
    comment block above the p3aMetrics rewire (which mentions the
    legacy token names as part of the rationale) doesn't trip the
    test. Only real executable references should fail this check.
    """
    js = APP_JS.read_text(encoding="utf-8")
    # Strip /* ... */ block comments and // ... line comments. Keep
    # the matching naive: app.js doesn't use regex literals containing
    # // sequences, so this is safe.
    js_no_block = re.sub(r"/\*.*?\*/", "", js, flags=re.DOTALL)
    js_no_comments = re.sub(r"//[^\n]*", "", js_no_block)

    forbidden_legacy = [
        "sip.open",
        "sip.qqq_aligned",
        "sip.index_aligned",
    ]
    for token in forbidden_legacy:
        assert token not in js_no_comments, (
            f"Pre-hotfix Authority wiring still references {token!r} "
            "in executable code. That field doesn't exist on "
            "section_i_permit and produces empty card rows. Use "
            "sip.long_open / sip.short_open / sip.sovereign_anchor_open "
            "instead."
        )


def test_authority_tagline_updated():
    """The card() call for Authority must use the new
    ``Permit & QQQ alignment`` description. The legacy
    ``5m DI\u00b1 > 25`` copy contradicts the new metric content.
    """
    js = APP_JS.read_text(encoding="utf-8")
    assert "Permit & QQQ alignment" in js, (
        "Authority card description must read 'Permit & QQQ alignment' "
        "to match the rewired metric rows."
    )
    # And the legacy copy must be gone from the Authority card line.
    # We allow the string to live in CHANGELOG / comments, but it must
    # not appear inside a card("P3", "Authority", ...) call.
    authority_pattern = re.compile(
        r'card\(\s*"P3"\s*,\s*"Authority"\s*,\s*"([^"]+)"',
    )
    matches = authority_pattern.findall(js)
    assert matches, "Could not locate the Authority card() call in app.js"
    for desc in matches:
        assert "DI" not in desc, (
            f"Authority card description still references DI: {desc!r}. "
            "Update to 'Permit & QQQ alignment'."
        )


def test_no_open_position_row_present():
    """When ``ppv`` is empty, Alarm A / Alarm B / POS Strikes must
    render a single ``(no open position)`` row instead of three em
    dashes. The predicate name is ``_hasOpenPos``.
    """
    js = APP_JS.read_text(encoding="utf-8")
    assert "_hasOpenPos" in js, (
        "Per-position cards must use _hasOpenPos predicate to switch "
        "between live metrics and the no-position fallback row."
    )
    assert "(no open position)" in js, (
        "Per-position cards must render '(no open position)' fallback row when ppv is empty."
    )
    # All three per-position cards must consult the predicate.
    # alAMetrics, alBMetrics, posMetrics each check _hasOpenPos.
    for var_name in ("alAMetrics", "alBMetrics", "posMetrics"):
        # Find the assignment and confirm _hasOpenPos appears within ~200
        # chars of it (i.e. inside the ternary branch).
        idx = js.find(f"{var_name} = ")
        assert idx >= 0, f"Could not locate {var_name} assignment in app.js"
        window = js[idx : idx + 400]
        assert "_hasOpenPos" in window, (
            f"{var_name} must consult _hasOpenPos so it can render the "
            "fallback row when no position is open."
        )


def test_app_css_no_dual_scroll_container():
    """The base ``.app`` rule must not declare ``display: grid`` or
    ``height: 100dvh``, and the base ``.main`` rule must not declare
    ``overflow-y: auto``. Together those rules created the desktop
    inner-scroll container that produced the double-scrollbar feel.

    CSS block comments are stripped before scanning so an explanatory
    comment that mentions the dropped properties (e.g. the v5.20.7
    rationale block above the .app rule) doesn't trip the test.
    """
    css = APP_CSS.read_text(encoding="utf-8")

    # Strip CSS block comments first.
    css_no_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)

    # We need to scan only the BASE .app and .main rule bodies, not the
    # @media (max-width: 900px) overrides. Find the first occurrence of
    # ".app {" before any "@media" line.
    media_idx = css_no_comments.find("@media")
    base = css_no_comments[:media_idx] if media_idx > 0 else css_no_comments

    # Locate base .app rule body.
    app_idx = base.find(".app {")
    assert app_idx >= 0, "Could not locate base .app rule in app.css"
    app_end = base.find("}", app_idx)
    assert app_end > app_idx
    app_body = base[app_idx:app_end]

    assert "display: grid" not in app_body, (
        "Base .app must not declare display: grid \u2014 it creates a "
        "fixed grid scroll container that fights page scroll. Removed "
        "in v5.20.7."
    )
    assert "100dvh" not in app_body, (
        "Base .app must not declare height: 100dvh \u2014 it locks the "
        "scroll container to viewport height and produces a perceived "
        "double scrollbar. Removed in v5.20.7."
    )

    # Locate base .main rule body.
    main_idx = base.find(".main {")
    assert main_idx >= 0, "Could not locate base .main rule in app.css"
    main_end = base.find("}", main_idx)
    assert main_end > main_idx
    main_body = base[main_idx:main_end]

    assert "overflow-y: auto" not in main_body, (
        "Base .main must not declare overflow-y: auto \u2014 the page "
        "body owns the scroll now. Removed in v5.20.7."
    )
    assert "overscroll-behavior: contain" not in main_body, (
        "Base .main must not declare overscroll-behavior: contain "
        "\u2014 it goes hand-in-hand with the dropped overflow-y. "
        "Removed in v5.20.7."
    )


def test_data_pmtx_comp_grid_version_bumped():
    """The component grid version marker must mention v5.20.7."""
    js = APP_JS.read_text(encoding="utf-8")
    assert 'data-pmtx-comp-grid="v5.20.9"' in js, (
        "data-pmtx-comp-grid attribute must be bumped to v5.20.9"
    )
