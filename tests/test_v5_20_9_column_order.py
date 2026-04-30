"""v5.20.9 \u2014 Permit Matrix table column order matches card/process order.

Three behaviors must hold:

1. ``BOT_VERSION`` is ``5.20.9`` in ``bot_version.py``.

2. The component table header row, in DOM order, lists the four gate
   columns as: **Boundary \u2192 Volume \u2192 Authority \u2192 Momentum**.
   That matches the card grid above the table (Weather \u2192 Boundary
   \u2192 Volume \u2192 Authority \u2192 Momentum) and the natural pipeline
   order (Phase 2 boundary, Phase 2 volume, Phase 3 permit alignment,
   Phase 3 ADX).

3. The matching ``<td>`` body cells in ``_pmtxBuildRow`` follow the
   same order: ``pmtx-col-orb``, ``pmtx-col-vol``, ``pmtx-col-diplus``,
   ``pmtx-col-adx``. Header and body must agree or every cell after
   the first reorder ends up under the wrong header.

Source-grep assertions because the dashboard JS runs in the browser.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_JS = REPO_ROOT / "dashboard_static" / "app.js"
BOT_VERSION_PY = REPO_ROOT / "bot_version.py"


def _read_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_bot_version_is_5_20_9():
    text = BOT_VERSION_PY.read_text(encoding="utf-8")
    assert 'BOT_VERSION = "5.24.0"' in text, "bot_version.py must report 5.24.0"


def test_table_header_column_order_matches_card_order():
    """The four gate ``<th>`` cells must appear in DOM order: Boundary,
    Volume, Authority, Momentum. We assert ``find()`` index ordering on
    the unique header substrings.
    """
    js = _read_js()
    idx_boundary = js.find(">Boundary</th>")
    idx_volume = js.find(">Volume</th>")
    idx_authority = js.find(">Authority</th>")
    idx_momentum = js.find(">Momentum</th>")

    for name, idx in (
        ("Boundary", idx_boundary),
        ("Volume", idx_volume),
        ("Authority", idx_authority),
        ("Momentum", idx_momentum),
    ):
        assert idx >= 0, f"header {name!r} missing from app.js"

    assert idx_boundary < idx_volume < idx_authority < idx_momentum, (
        "Permit Matrix table header order must be Boundary \u2192 Volume "
        "\u2192 Authority \u2192 Momentum to match the card grid above "
        "the table. Got "
        f"Boundary@{idx_boundary}, Volume@{idx_volume}, "
        f"Authority@{idx_authority}, Momentum@{idx_momentum}."
    )


def test_table_body_cell_order_matches_header_order():
    """Body ``<td class=\"pmtx-col-...\">`` cells must follow the same
    order as the header: orb, vol, diplus, adx. Otherwise data lands
    under the wrong header.
    """
    js = _read_js()
    idx_orb = js.find('<td class="pmtx-col-orb">')
    idx_vol = js.find('<td class="pmtx-col-vol">')
    idx_diplus = js.find('<td class="pmtx-col-diplus">')
    idx_adx = js.find('<td class="pmtx-col-adx">')

    for name, idx in (
        ("pmtx-col-orb", idx_orb),
        ("pmtx-col-vol", idx_vol),
        ("pmtx-col-diplus", idx_diplus),
        ("pmtx-col-adx", idx_adx),
    ):
        assert idx >= 0, f"body cell {name!r} missing from app.js"

    assert idx_orb < idx_vol < idx_diplus < idx_adx, (
        "Permit Matrix body cell order must mirror header order: "
        "pmtx-col-orb (Boundary) \u2192 pmtx-col-vol (Volume) \u2192 "
        "pmtx-col-diplus (Authority) \u2192 pmtx-col-adx (Momentum). "
        f"Got orb@{idx_orb}, vol@{idx_vol}, diplus@{idx_diplus}, adx@{idx_adx}."
    )


def test_card_vocabulary_still_in_place():
    """v5.20.8 contract \u2014 the four card-vocabulary headers must
    still be present (this hotfix only reorders them)."""
    js = _read_js()
    for header in (
        ">Boundary</th>",
        ">Volume</th>",
        ">Authority</th>",
        ">Momentum</th>",
    ):
        assert header in js, f"card-vocabulary header {header!r} missing"
