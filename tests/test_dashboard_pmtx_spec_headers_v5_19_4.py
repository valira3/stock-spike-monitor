"""v5.19.4 \u2014 Permit Matrix headers correlate to Tiger Sovereign vAA-1 spec.

Spec source of truth: ``tiger_sovereign_spec_vAA-1.md`` (rule IDs
``L-P2-S3``, ``L-P2-S4``, ``L-P3-AUTH``, ``STRIKE-CAP-3``, etc.).

Prior to v5.19.4 the column tooltips referred to internal naming
(\"Phase 2 PASS/FAIL/COLD/OFF\", \"Entry 1 trigger\", \"Volume Bucket gate\")
that didn't match the spec wording. v5.19.4 rewrites the tooltips to
quote the spec line for line and cite the rule IDs.

These tests are string-level audits of ``dashboard_static/app.js``.
"""

from __future__ import annotations

from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "dashboard_static" / "app.js"


def _read() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_orb_header_and_tooltip_cite_l_p2_s4():
    src = _read()
    # Header text trimmed: \"5m ORB\" -> \"ORB\".
    assert ">ORB</th>" in src
    # Tooltip cites the rule ID and the 09:35:59 ET freeze.
    assert "L-P2-S4 / S-P2-S4" in src
    assert "09:35:59 ET" in src
    assert "Two consecutive 1m candles" in src


def test_trend_column_marked_as_proxy_not_spec_gate():
    src = _read()
    # Header changed: \"ADX>20\" -> \"Trend\". The column stays for visual
    # parity but the tooltip is honest.
    assert ">Trend</th>" in src
    assert "not a primary spec gate" in src


def test_p3_auth_master_anchor_tooltip():
    src = _read()
    # Header trimmed: \"DI\u00b1 5m>25\" -> \"5m DI\u00b1\".
    assert ">5m DI\\u00b1</th>" in src
    # Tooltip cites the rule IDs and the strict \"if FALSE \u2192 no entry\"
    # semantics.
    assert "L-P3-AUTH / S-P3-AUTH" in src
    assert "Phase 3 master anchor" in src


def test_volume_gate_tooltip_cites_l_p2_s3():
    src = _read()
    assert "L-P2-S3 / S-P2-S3" in src
    # Time-conditional: auto-pass before 10:00 ET, then 1.0\u00d7 rolling
    # 55-bar same-minute average.
    assert "Auto-passes before 10:00 ET" in src
    assert "55-bar same-minute average" in src


def test_strikes_tooltip_cites_strike_cap_3_and_flat_gate():
    src = _read()
    assert "STRIKE-CAP-3" in src
    assert "STRIKE-FLAT-GATE" in src
    assert "09:30:00 ET" in src


def test_state_pill_tooltip_lists_all_four_fsm_states():
    src = _read()
    # The State column tooltip should enumerate the FSM states.
    assert "IDLE" in src
    assert "ARMED" in src
    assert "IN POS" in src
    assert "LOCKED" in src
