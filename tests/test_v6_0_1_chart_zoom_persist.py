# tests/test_v6_0_1_chart_zoom_persist.py
# v6.0.1 -- chart zoom/pan/dblclick view must survive the periodic
# /api/state matrix re-render. Source-level checks (we cannot run the
# browser here): verify the per-ticker persist dict, the persist hooks
# on every mutation path, and the collapse-reset wiring.
# No em-dashes in this file (constraint for .py test files).
from __future__ import annotations

import os


APP_JS = os.path.join(
    os.path.dirname(__file__), "..", "dashboard_static", "app.js"
)


def _read():
    with open(APP_JS, "r", encoding="utf-8") as f:
        return f.read()


def test_per_ticker_persist_dict_exists():
    js = _read()
    # The per-ticker persistence dict must be a plain object so the
    # zoom/pan window survives canvas DOM teardown across /api/state
    # polls. A WeakMap keyed by canvas would not.
    assert "_chartViewByTkr = {}" in js, "_chartViewByTkr plain dict missing"


def test_persist_hooks_in_three_mutation_paths():
    js = _read()
    # Every mutation path (wheel zoom, drag pan, dblclick reset) must
    # call _chartPersistView so the next render seeds from the saved
    # window instead of resetting to the full session.
    occurrences = js.count("_chartPersistView(canvas, _vs)")
    assert occurrences >= 3, (
        "expected >=3 _chartPersistView calls (wheel/drag/dblclick), got "
        + str(occurrences)
    )


def test_collapse_resets_persisted_view():
    js = _read()
    # Outside-click and re-click-toggle handlers must drop the persisted
    # view so the next time the row opens we start at the full session.
    assert "_pmtxResetChartViewsFor" in js, "collapse-reset helper missing"
    # The helper deletes from _chartViewByTkr.
    assert "delete _chartViewByTkr[t]" in js, (
        "collapse-reset helper does not delete from _chartViewByTkr"
    )


def test_state_seeded_from_persist_dict():
    js = _read()
    # _chartGetState must seed xMin / xMax from _chartViewByTkr[tkr]
    # when present, so a freshly-mounted canvas inherits the user's
    # view instead of snapping to the full session.
    assert "_chartViewByTkr[tkr]" in js, "_chartGetState does not consult per-ticker persist"
    assert "persisted ? persisted.xMin" in js, "xMin not seeded from persisted view"
    assert "persisted ? persisted.xMax" in js, "xMax not seeded from persisted view"
