"""v5.20.0 \u2014 Permit Matrix headers correlate to Tiger Sovereign v15.0 spec.

Spec source of truth: ``tiger_sovereign-spec-v15-1.md`` (\u00a71 strikes,
\u00a72/\u00a73 long/short permits, \u00a74 risk/timing).

Prior to v5.19.4 the tooltips referred to internal naming. v5.19.4
quoted the spec line for line and cited rule IDs (``L-P2-S3``,
``STRIKE-CAP-3``, etc.). v5.20.0 supersedes that wording: rule IDs
were retired (the Tiger Sovereign v15.0 spec dropped the per-rule ID
hierarchy and re-indexed by section number) and the tooltips now
quote v15.0 directly. These tests pin the v15.0 wording.

These tests are string-level audits of ``dashboard_static/app.js``.
"""

from __future__ import annotations

from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "dashboard_static" / "app.js"


def _read() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_orb_header_and_tooltip_cite_v15_section():
    # Test name pinned to release; assertions follow the current contract.
    # v5.20.8 renamed the column header ``ORB \u2192 Boundary`` to match the
    # Phase 2 card vocabulary. The CSS class (.pmtx-col-orb) is unchanged
    # for layout continuity, and the underlying spec citations the
    # tooltip carries are still required by this audit.
    src = _read()
    assert ">Boundary</th>" in src
    # Tooltip cites Tiger Sovereign v15.0 wording (still phase-2 boundary).
    assert "two consecutive 1m closes" in src
    assert "09:35:59 ET" in src
    # Strike sequence is explicit: S1 hunts ORH/ORL, S2/S3 hunt NHOD/NLOD.
    assert "ORH" in src and "ORL" in src
    assert "NHOD" in src and "NLOD" in src


def test_trend_column_marked_as_primary_spec_gate():
    """v15.0 \u2014 5m ADX>20 is now a PRIMARY spec gate (was a proxy in
    v5.19.4). The tooltip must reflect the upgrade.

    v5.20.8 renamed the column header ``Trend \u2192 Momentum`` to match
    the Phase 3 Momentum card vocabulary. The ADX>20 spec citation in
    the tooltip is unchanged.
    """
    src = _read()
    assert ">Momentum</th>" in src
    # v15.0: ADX>20 is a primary entry gate, not a proxy.
    assert "5m ADX > 20" in src


def test_p3_auth_master_anchor_tooltip():
    # v5.20.8 rewired the Authority column body cell to mirror the
    # Authority card (Section-I permit alignment) and renamed the
    # header ``5m DI\u00b1 \u2192 Authority``. Per-ticker DI\u00b1
    # detail (DI+ 1m/5m, DI\u2212 1m/5m, threshold) lives in the
    # Momentum card metric stack inside the expanded row, so the
    # column itself no longer carries the legacy DI tooltip text.
    # This test now pins the new header + tooltip citations.
    src = _read()
    assert ">Authority</th>" in src
    # Tooltip must mention Section-I permit alignment + the canonical
    # permit booleans, plus point operators at the expanded Momentum
    # card metric stack for DI\u00b1 detail.
    assert "Section-I permit alignment" in src
    assert "long_open" in src and "short_open" in src
    assert "Momentum card metric stack" in src


def test_volume_gate_tooltip_cites_55bar_average_and_10am_threshold():
    src = _read()
    # v15.0: 1m volume \u2265 100% of 55-bar rolling average. Required
    # after 10:00 ET; auto-passes before 10:00 ET.
    assert "55-bar rolling average" in src
    assert "10:00" in src and "ET" in src
    # \"Required after 10:00\" + \"auto-passes\" / \"before 10:00\" wording.
    assert "auto-passes" in src.lower() or "auto-pass" in src.lower()


def test_strikes_tooltip_cites_3_per_day_cap_and_sequential():
    src = _read()
    # v15.0 \u00a71: max 3 Strikes per ticker per day.
    assert "Maximum 3 Strikes" in src
    assert "per ticker per day" in src
    # Sequential Requirement \u2014 spec verbatim phrase.
    assert "Sequential Requirement" in src
    # Daily reset boundary.
    assert "09:30:00 ET" in src


def test_state_pill_tooltip_lists_all_four_fsm_states():
    src = _read()
    # The State column tooltip should enumerate the FSM states.
    assert "IDLE" in src
    assert "ARMED" in src
    assert "IN POS" in src
    assert "LOCKED" in src
