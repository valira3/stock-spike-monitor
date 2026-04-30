"""v5.19.4 \u2014 panel order on Main.

Operator request: the Open positions card should sit ABOVE the Weather
Check banner so currently-held risk is the first thing visible. The
prior order put the conditional \"can I take a new entry?\" verdict
first, which buried the open-positions table on small screens.

This test reads ``dashboard_static/index.html`` and asserts the
positions card appears before the weather banner.
"""

from __future__ import annotations

from pathlib import Path

INDEX_HTML = Path(__file__).resolve().parent.parent / "dashboard_static" / "index.html"


def _read() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def test_open_positions_appears_before_weather_check():
    src = _read()
    # Anchor markers \u2014 these strings are stable inside their respective
    # sections and don't appear elsewhere in the file.
    pos_marker = 'id="pos-body"'
    weather_marker = 'id="pmtx-weather"'
    pos_idx = src.find(pos_marker)
    weather_idx = src.find(weather_marker)
    assert pos_idx > 0, "Open positions section marker missing in index.html"
    assert weather_idx > 0, "Weather Check section marker missing in index.html"
    assert pos_idx < weather_idx, (
        f"Open positions ({pos_idx}) must appear before Weather Check "
        f"({weather_idx}) in the document order."
    )


def test_weather_section_still_present():
    src = _read()
    # The reorder must not have deleted Weather Check.
    assert 'class="pmtx-weather-section"' in src
    assert 'id="pmtx-weather-verdict"' in src
    assert 'id="pmtx-weather-detail"' in src


def test_open_positions_section_still_present():
    src = _read()
    # The reorder must not have deleted the Open positions card.
    assert 'id="pos-body"' in src
    assert 'id="pos-count"' in src
    assert 'id="port-strip"' in src


def test_permit_matrix_remains_below_weather():
    """Panel order on Main is now: Open positions \u2192 Weather Check \u2192
    Permit Matrix. The matrix is a separate big card; its position
    relative to Weather should not have moved.
    """
    src = _read()
    weather_idx = src.find('id="pmtx-weather"')
    matrix_idx = src.find('id="pmtx-body"')
    assert weather_idx > 0 and matrix_idx > 0
    assert weather_idx < matrix_idx, (
        "Permit Matrix must remain BELOW Weather Check in document order."
    )
