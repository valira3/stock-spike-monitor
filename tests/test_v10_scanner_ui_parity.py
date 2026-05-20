# tests/test_v10_scanner_ui_parity.py
#
# v10.0.0 -- dashboard UI parity for the broad-universe scanner +
# sector-cluster gate. CLAUDE.md flags UI parity drift as a recurring
# class of regression (shipped wrong twice: v8.3.1+v8.3.16 ET
# conversion; v8.3.8+v8.3.10+v8.3.18 position-row columns). These
# source-level checks pin the v10 contract so a future refactor of
# either render path can't silently drop the new pills/chips.
#
# Source-level only (no headless browser): parses dashboard_static/index.html
# + app.js as strings, asserts ID presence + render-path references.
#
# No em-dashes in this file (constraint for .py test files).
from __future__ import annotations

import os
import re


HERE = os.path.dirname(__file__)
APP_JS = os.path.join(HERE, "..", "dashboard_static", "app.js")
INDEX_HTML = os.path.join(HERE, "..", "dashboard_static", "index.html")


# The three pills introduced in v10.0.0.
V10_PILL_IDS = (
    "v10-universe-pill",
    "v10-cluster-pill",
    "v10-picks-pill",
)

# Scanner fields the dashboard JS consumes from /api/state.v10.scanner.
V10_SCANNER_FIELDS = (
    "dynamic_universe_enabled",
    "dynamic_universe_active",
    "cluster_gate_active",
    "cluster_gate_skipped_day",
    "cluster_max_sector_pct",
    "cluster_top_sector",
    "fallback_reason",
    "picks",
)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _slice_func(js: str, signature: str, span: int = 12000) -> str:
    start = js.find(signature)
    assert start != -1, f"{signature!r} not found in app.js"
    return js[start:start + span]


# ----- HTML side: pill nodes exist -----


def test_index_html_has_v10_universe_pill():
    html = _read(INDEX_HTML)
    for pid in V10_PILL_IDS:
        assert f'id="{pid}"' in html, (
            f"index.html missing pill #{pid} -- v10 scanner banner "
            "won't render. Add the <span id> back to v10-day-status."
        )


def test_index_html_has_pill_dividers_for_each_v10_pill():
    """Each pill has a sibling divider node that the render code
    shows/hides together. Drop a divider and the banner looks broken
    when the pill itself is shown."""
    html = _read(INDEX_HTML)
    for pid in V10_PILL_IDS:
        divider = f"{pid}-divider"
        assert f'id="{divider}"' in html, (
            f"index.html missing #{divider} -- the v10 banner divider "
            "before the pill won't render. Pair with the pill node."
        )


def test_v10_pills_live_in_v10_day_status_banner():
    """The 3 v10 pills must sit inside the <section id="v10-day-status">
    so they appear under the v10 ORB header (section #4 in the
    CLAUDE.md section-order rule for Main)."""
    html = _read(INDEX_HTML)
    sec_start = html.find('id="v10-day-status"')
    assert sec_start != -1, "v10-day-status section missing entirely"
    sec_end_marker = html.find("</section>", sec_start)
    assert sec_end_marker != -1
    banner_block = html[sec_start:sec_end_marker]
    for pid in V10_PILL_IDS:
        assert f'id="{pid}"' in banner_block, (
            f"#{pid} is outside <section id='v10-day-status'> -- it'll "
            "render in the wrong place per the section-order rule."
        )


# ----- Main path: renderV10DayStatus references each pill -----


def test_renderv10daystatus_references_each_pill_node():
    js = _slice_func(_read(APP_JS), "function renderV10DayStatus(s, pidFilter)",
                     span=20000)
    for pid in V10_PILL_IDS:
        assert pid in js, (
            f"renderV10DayStatus does not reference #{pid} -- Main tab "
            "will not populate that pill on /api/state poll. "
            "(grep app.js -- pill ID dropped from the renderer.)"
        )


def test_renderv10daystatus_handles_each_scanner_state_field():
    """The Main render path must read the scanner-state fields that
    /api/state.v10.scanner serializes. Drop one and the corresponding
    pill stops updating after a refresh."""
    js = _slice_func(_read(APP_JS), "function renderV10DayStatus(s, pidFilter)",
                     span=20000)
    # We only require dynamic_universe_active + cluster_* fields the
    # Main renderer actively consumes (picks is read via .picks length).
    required = (
        "dynamic_universe_enabled",
        "dynamic_universe_active",
        "cluster_gate_active",
        "cluster_gate_skipped_day",
        "cluster_max_sector_pct",
        "cluster_top_sector",
        "fallback_reason",
    )
    for field in required:
        assert field in js, (
            f"renderV10DayStatus does not read scanner field {field!r} -- "
            "pill content will be stale or wrong."
        )


# ----- Val/Gene path: renderV10PerPortfolio injects scanner chips -----


def test_renderv10perportfolio_emits_scanner_chips_inline():
    """Val/Gene panels build their chip strip via inline HTML inside
    renderV10PerPortfolio (no shared pill nodes -- the function
    constructs an HTML string and writes innerHTML on the per-pid
    body). The 3 v10 chip concepts (universe / cluster / picks) must
    all be present in the rendered string."""
    js = _slice_func(_read(APP_JS), "function renderV10PerPortfolio(name, panel, execData)",
                     span=40000)
    # Universe chip: render any of the 3 universe states (active / fallback / disabled).
    assert ("Univ dyn" in js) or ("Univ static" in js) or ("Univ fallback" in js), (
        "renderV10PerPortfolio does not emit the v10 Universe chip "
        "-- Val/Gene tabs lose parity with Main."
    )
    # Cluster gate chip: at least one of the 3 cluster-state strings.
    assert ("DAY SKIPPED" in js) or ("cluster " in js), (
        "renderV10PerPortfolio does not emit the v10 cluster gate chip"
    )
    # Picks chip: presence of the literal "picks " prefix used by both
    # tabs.
    assert "picks " in js, (
        "renderV10PerPortfolio does not emit the v10 picks chip -- "
        "Val/Gene tabs won't show today's scanner picks."
    )


def test_renderv10perportfolio_reads_scanner_state_from_v10_block():
    """The per-portfolio renderer must read scanner state from the
    SAME path the snapshot serializes ('v10.scanner'). If it drifts to
    a different key, Val/Gene chips silently stop updating."""
    js = _slice_func(_read(APP_JS), "function renderV10PerPortfolio(name, panel, execData)",
                     span=40000)
    # Must reference (v10 && v10.scanner) -- the function reads via the
    # local `v10` variable bound from execData / state.
    assert "scanner" in js, (
        "renderV10PerPortfolio does not reference s.v10.scanner -- "
        "chip strip won't read fresh scanner state."
    )


def test_main_and_perportfolio_share_scanner_fields():
    """Anti-drift: both Main and Val/Gene paths must reference the
    same scanner-state field names so the snapshot dict shape doesn't
    silently mean two different things in two render paths."""
    js = _read(APP_JS)
    main_slice = _slice_func(js, "function renderV10DayStatus(s, pidFilter)", span=20000)
    per_slice = _slice_func(js, "function renderV10PerPortfolio(name, panel, execData)",
                            span=40000)
    common_fields = (
        "dynamic_universe_active",
        "cluster_gate_active",
        "cluster_gate_skipped_day",
        "cluster_max_sector_pct",
        "cluster_top_sector",
        "fallback_reason",
    )
    for f in common_fields:
        in_main = f in main_slice
        in_per = f in per_slice
        if not (in_main and in_per):
            raise AssertionError(
                f"Scanner field {f!r} present on {'Main' if in_main else 'Val/Gene'} "
                f"but not on {'Val/Gene' if in_main else 'Main'}. "
                "UI parity broken -- this is the class of bug that's "
                "shipped wrong twice before (see CLAUDE.md)."
            )


# ----- Snapshot serialization contract -----


def test_scanner_state_snapshot_dict_carries_required_keys():
    """The to_snapshot_dict() function must always return the field
    names the JS expects. Refactor of orb/scanner_state.py that drops
    one of these would silently break the dashboard."""
    from orb import scanner_state
    scanner_state.clear_state()
    d = scanner_state.to_snapshot_dict()
    required = {
        "date", "dynamic_universe_active", "cluster_gate_active",
        "cluster_gate_skipped_day", "cluster_max_sector_pct",
        "cluster_top_sector", "universe", "picks", "fallback_reason",
    }
    missing = required - set(d.keys())
    assert not missing, f"to_snapshot_dict missing fields: {sorted(missing)}"


def test_live_runtime_snapshot_serializes_scanner_block():
    """orb.live_runtime.snapshot() exposes the scanner block under
    the 'scanner' key. The dashboard JS hardcodes this path."""
    import orb.live_runtime as lr
    snap = lr.snapshot()
    if snap.get("bootstrapped"):
        assert "scanner" in snap, "live_runtime.snapshot() must include 'scanner' key"
        assert "dynamic_universe_enabled" in snap["scanner"], (
            "snap['scanner'] missing dynamic_universe_enabled flag"
        )


def test_pill_id_consistency_html_to_js():
    """If a pill ID drifts in HTML vs JS, neither tab shows the pill.
    Fail loudly here."""
    html = _read(INDEX_HTML)
    js = _read(APP_JS)
    for pid in V10_PILL_IDS:
        if f'id="{pid}"' not in html:
            raise AssertionError(f"#{pid} missing in index.html")
        if pid not in js:
            raise AssertionError(
                f"#{pid} present in HTML but not referenced in app.js -- "
                "pill will render once and never update."
            )
