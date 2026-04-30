"""v5.20.6 \u2014 Card metric hotfix tests.

Three behaviors must hold after the v5.20.6 dashboard tweaks:

1. The Weather card must source QQQ price/EMA9/AVWAP from
   ``section_i_permit`` field names (``qqq_current_price``,
   ``qqq_5m_close``, ``qqq_5m_ema9``, ``qqq_avwap_0930``). The pre-hotfix
   wiring read them off ``regime``, where they don't exist, and the card
   silently rendered every row as a dim em dash.

2. The component-card metric stack must NOT cap with an inner scrollbar
   (``max-height`` + ``overflow-y: auto`` removed). The longest stack
   after this hotfix is 6 rows (Weather), which fits naturally inside
   the card. The inner scrollbar was trapping the mouse wheel on
   desktop.

3. When the volume gate is bypassed via ``VOLUME_GATE_ENABLED=false``,
   the dashboard surfaces ``vol_gate_status=\"OFF\"`` and the JS shows a
   single \"Volume gate: bypassed (warming)\" row instead of the four
   baseline / ratio / days-avail rows that would otherwise render as
   meaningless em dashes during the 55-day warm-up.

These are source-grep assertions because the dashboard JS / CSS run in
the browser, not the Python test runner. The grep approach mirrors the
``smoke_test.py`` source-grep guards already used elsewhere in the
codebase to pin commits we don't want silently undone.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_JS = REPO_ROOT / "dashboard_static" / "app.js"
APP_CSS = REPO_ROOT / "dashboard_static" / "app.css"
BOT_VERSION_PY = REPO_ROOT / "bot_version.py"


def test_bot_version_is_5_20_6():
    text = BOT_VERSION_PY.read_text(encoding="utf-8")
    assert 'BOT_VERSION = "5.20.6"' in text, "bot_version.py must report 5.20.6 for this hotfix"


def test_weather_card_reads_section_i_permit_fields():
    """The p1Metrics block must reference all four section_i_permit
    QQQ fields. The pre-hotfix wiring used ``reg.qqq_*`` which silently
    rendered em dashes because those keys never existed on regime.
    """
    js = APP_JS.read_text(encoding="utf-8")

    # Each of the four QQQ fields must be referenced through `sip.`
    # (the local alias for section_i_permit set up earlier in the
    # function). The exact SIP key names come from
    # v5_10_6_snapshot._section_i_permit_block.
    required = [
        "sip.qqq_current_price",
        "sip.qqq_5m_close",
        "sip.qqq_5m_ema9",
        "sip.qqq_avwap_0930",
    ]
    for token in required:
        assert token in js, (
            f"Weather card metric stack must reference {token!r} \u2014 "
            "without it the row renders as a dim em dash because the "
            "previous reg.qqq_* fields don't exist on the regime block."
        )

    # Pre-hotfix tokens must NOT survive (would mean the rewire was
    # only partial). We allow `reg.breadth` / `reg.rsi_regime` since
    # those DO exist on regime; only the four QQQ fields are at issue.
    forbidden_legacy = [
        "reg.qqq_price",
        "reg.qqq_5m_close",
        "reg.qqq_ema9",
        "reg.qqq_avwap",
    ]
    for token in forbidden_legacy:
        assert token not in js, (
            f"Pre-hotfix Weather wiring still references {token!r}. "
            "Those fields don't exist on the regime block and produce "
            "empty card rows. Use sip.qqq_* instead."
        )


def test_metric_stack_has_no_inner_scroll_cap():
    """The .pmtx-comp-metrics rule must not declare max-height or
    overflow-y. Both were dropped in v5.20.6 because the inner
    scrollbar fought the page scroll on desktop.
    """
    import re

    css = APP_CSS.read_text(encoding="utf-8")

    # Find the rule body for .pmtx-comp-metrics. Naive but sufficient:
    # the file is hand-authored and the selector appears once.
    marker = ".pmtx-comp-metrics {"
    idx = css.find(marker)
    assert idx >= 0, "Cannot find .pmtx-comp-metrics rule in app.css"
    end = css.find("}", idx)
    assert end > idx, "Cannot find closing brace for .pmtx-comp-metrics"
    body = css[idx:end]

    # Strip CSS block comments before scanning so an explanatory
    # comment that mentions the dropped properties doesn't trip the
    # test. We only care about actual CSS declarations.
    body_no_comments = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)

    # Match "max-height:" / "overflow-y:" with a colon so we catch only
    # real declarations, not the property name appearing in prose.
    assert "max-height:" not in body_no_comments, (
        ".pmtx-comp-metrics must not declare max-height \u2014 the inner "
        "scrollbar fights the page scroll on desktop. Removed in v5.20.6."
    )
    assert "overflow-y:" not in body_no_comments, (
        ".pmtx-comp-metrics must not declare overflow-y \u2014 the inner "
        "scrollbar traps the mouse wheel on desktop. Removed in v5.20.6."
    )


def test_volume_card_renders_bypassed_label_when_gate_off():
    """When the gate is bypassed (vol_gate_status=\"OFF\" surfaced from
    VOLUME_GATE_ENABLED=false), p2vMetrics must render a single
    explanatory row, not the warming-up em-dash stack.
    """
    js = APP_JS.read_text(encoding="utf-8")

    # The conditional branch must compare volStatus against \"OFF\" and
    # produce a \"bypassed\" label. We look for both halves so a
    # half-finished refactor still trips the test.
    assert '"OFF"' in js, (
        'Volume card must check volStatus === "OFF" so the bypassed '
        "branch fires when feature_flags.VOLUME_GATE_ENABLED=false."
    )
    assert "bypassed (warming)" in js, (
        "Volume card must render a 'bypassed (warming)' label when the "
        "gate is off, instead of four meaningless em-dash rows."
    )

    # And the original four metric labels must still exist somewhere
    # in the file so the gate-on path is unaffected.
    for label in ("Current vol", "Baseline 55d", "Ratio 55-bar", "Days avail"):
        assert label in js, (
            f"Volume card must still ship the {label!r} row in the "
            "gate-on path; only the gate-off branch is collapsed."
        )


def test_data_pmtx_comp_grid_version_bumped():
    """The component grid version marker must mention v5.20.6 so the
    operator can confirm the hotfix shipped from devtools without
    digging through /api/version.
    """
    js = APP_JS.read_text(encoding="utf-8")
    assert 'data-pmtx-comp-grid="v5.20.6"' in js, (
        "data-pmtx-comp-grid attribute must be bumped to v5.20.6"
    )
