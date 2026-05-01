"""v5.19.3 \\u2014 Permit Matrix row expand regression fix.

In v5.18.0 the standalone Proximity card folded into the matrix detail
panel, but `hasDetail` in `_pmtxBuildRow` stayed `pos || lastFill`. As a
result, pre-market sessions (no positions, no fills, but plenty of
proximity data) rendered every row as static \\u2014 click did nothing.

The v5.19.3 fix widens `hasDetail` to also include a non-empty proximity
payload (price, nearest_label, or_high, or_low). These string-level
audits pin the wiring so the regression cannot return silently.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_JS = REPO_ROOT / "dashboard_static" / "app.js"


def _read_app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_app_js_exists():
    assert APP_JS.exists(), f"missing {APP_JS}"


def test_has_detail_includes_prox():
    """The hasDetail expression must reference the proxHasDetail flag.

    v5.28.2 widened the gate to also include open Phase-1 permits so
    permit-go rows expand even before any proximity data has flowed.
    The proxHasDetail clause itself must still be present \u2014 v5.19.3
    behavior continues to hold for pre-market rows that have proximity
    info but no permit yet.
    """
    src = _read_app_js()
    assert (
        "const hasDetail = !!(pos || lastFill || proxHasDetail || longPermit || shortPermit);"
        in src
    ), "hasDetail must include proxHasDetail (v5.19.3) and longPermit/shortPermit (v5.28.2)"


def test_prox_has_detail_checks_all_four_payload_keys():
    """proxHasDetail must consider price, nearest_label, or_high, or_low."""
    src = _read_app_js()
    # Find the proxHasDetail block and verify each key is referenced.
    idx = src.find("const proxHasDetail")
    assert idx >= 0, "proxHasDetail flag not declared"
    block = src[idx : idx + 400]
    for needle in (
        'typeof prox.price === "number"',
        "prox.nearest_label",
        'typeof prox.or_high === "number"',
        'typeof prox.or_low === "number"',
    ):
        assert needle in block, f"proxHasDetail missing {needle!r}"


def test_pmtx_detail_row_emitted_only_when_hasdetail():
    """Detail row markup must remain gated on hasDetail."""
    src = _read_app_js()
    # The detail row template literal lives inside `if (hasDetail) { ... }`.
    # Confirm both the gate and the row markup are present.
    assert "if (hasDetail) {" in src
    assert '<tr class="pmtx-detail-row"' in src


def test_expand_handler_still_wired_at_body_level():
    """The delegate click handler at body.__pmtxExpandWired stays intact."""
    src = _read_app_js()
    # body-level delegate that closes on tr.pmtx-row[data-pmtx-tkr] and
    # toggles the pmtx-detail-open class. The handler did not change in
    # v5.19.3 \\u2014 only the upstream gating did \\u2014 so it must still
    # be wired the same way for the fix to take effect.
    assert "body.__pmtxExpandWired" in src
    assert "tr.pmtx-row[data-pmtx-tkr]" in src
    assert "pmtx-detail-open" in src
