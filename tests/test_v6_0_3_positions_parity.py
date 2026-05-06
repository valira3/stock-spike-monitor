# tests/test_v6_0_3_positions_parity.py
# v6.0.3 -- positions-table column parity between Main and Val/Gene.
# Main was missing the % column; Val/Gene was missing the Stop column.
# Source-level checks against dashboard_static/app.js (we cannot run a
# real browser here).
# No em-dashes in this file (constraint for .py test files).
from __future__ import annotations

import os


APP_JS = os.path.join(
    os.path.dirname(__file__), "..", "dashboard_static", "app.js"
)


def _read() -> str:
    with open(APP_JS, "r", encoding="utf-8") as f:
        return f.read()


def _slice_main_positions(js: str) -> str:
    # The Main positions renderer is renderPositions -- find that block.
    start = js.find("function renderPositions(s, sl)")
    assert start != -1, "renderPositions not found"
    # Take a generous window; the body is < 4 KB.
    return js[start:start + 6000]


def _slice_exec_positions(js: str) -> str:
    # The Val/Gene positions renderer lives inside renderExecutor. As of
    # v7.0.3 it uses Main's exact header text (Ticker / Side / Sh / Entry /
    # Mark / Stop / Unreal. / %), so we can no longer key off a divergent
    # 'Avg Entry' needle. Locate the block by the unique 'Open positions
    # card' comment that opens it and the 'Cash / BP / Invested / Shorted
    # footer' comment that closes it -- both are still present in the
    # exec renderer and unique to it.
    head = js.find("Open positions card")
    assert head != -1, "executor pos-body region start not found"
    tail = js.find("Cash / BP / Invested / Shorted footer", head)
    assert tail != -1, "executor positions block end not found"
    return js[head:tail]


# ----- Main positions: % column -----

def test_main_positions_table_has_percent_header():
    js = _slice_main_positions(_read())
    # Headers we expect in order: Ticker, Side, Sh, Entry, Mark, Stop,
    # Unreal., %.
    assert ">Ticker<" in js, "Main: Ticker header missing"
    assert ">Sh<" in js, "Main: Sh header missing"
    assert ">Stop<" in js, "Main: Stop header missing"
    assert ">Unreal.<" in js, "Main: Unreal. header missing"
    assert ">%<" in js, "Main: % header missing (parity break)"


def test_main_positions_percent_cell_uses_unrealized_over_cost_basis():
    js = _slice_main_positions(_read())
    # The % cell is computed client-side from unrealized / (entry * shares),
    # then formatted via fmtPct (which already produces the leading +/-).
    # We assert all three numeric inputs are read and the divisor is the
    # cost-basis product (not just entry, not just shares).
    assert "_unrNum / (_entryNum * _shNum)" in js, (
        "Main: % cell does not divide unrealized by entry*shares"
    )
    assert "fmtPct((_unrNum / (_entryNum * _shNum)) * 100)" in js, (
        "Main: % cell does not format with fmtPct after multiplying by 100"
    )


def test_main_positions_percent_cell_color_matches_pnl():
    js = _slice_main_positions(_read())
    # The % cell must use the same pnlCls as the Unreal. cell so green/red
    # always match between the two columns. The cell is the row's last td
    # immediately after the unrealized td.
    assert '<td class="right ${pnlCls}">${pctTxt}</td>' in js, (
        "Main: % cell does not reuse pnlCls"
    )


def test_main_positions_percent_handles_missing_data():
    js = _slice_main_positions(_read())
    # The pctTxt sentinel must default to em-dash when entry/shares/
    # unrealized aren't all finite numbers, so we never display NaN%.
    assert 'let pctTxt = "\\u2014";' in js, "Main: pctTxt missing em-dash default"
    assert "Number.isFinite(_entryNum) && _entryNum > 0" in js, (
        "Main: pctTxt does not guard against non-finite/zero entry"
    )


# ----- Val/Gene executor positions: Stop column -----

def test_exec_positions_table_has_stop_header():
    js = _slice_exec_positions(_read())
    # v7.0.3: Val/Gene now use Main's exact header text. Headers in order:
    # Ticker, Side, Sh, Entry, Mark, Stop, Unreal., %.
    assert ">Ticker<" in js, "Exec: Ticker header missing"
    assert ">Sh<" in js, "Exec: Sh header missing"
    assert ">Entry<" in js, "Exec: Entry header missing"
    assert ">Stop<" in js, "Exec: Stop header missing (parity break)"
    assert ">Unreal.<" in js, "Exec: Unreal. header missing"
    assert ">%<" in js, "Exec: % header missing"


def test_exec_positions_stop_cross_references_main_state():
    js = _slice_exec_positions(_read())
    # The /api/executor/<name> payload doesn't carry stop levels, so the
    # client looks them up by symbol against window.__tgLastState.positions
    # which Main publishes on every poll.
    assert "window.__tgLastState" in js, (
        "Exec: Stop column does not consult window.__tgLastState"
    )
    # A symbol-keyed lookup table is built from Main positions.
    assert "_stopBySym[_mp.ticker]" in js, (
        "Exec: stop lookup table not keyed by ticker"
    )
    # Effective stop preferred over hard stop, matching Main's logic.
    assert 'typeof _mp.effective_stop === "number"' in js, (
        "Exec: stop fallback does not prefer effective_stop"
    )


def test_exec_positions_stop_handles_missing_main_state():
    js = _slice_exec_positions(_read())
    # If __tgLastState hasn't populated yet (e.g. exec tab opened before
    # main poll lands) we render em-dash, never crash. The default text
    # must be u2014 and only flip when the eff number is finite.
    assert 'let _stopTxt = "\\u2014";' in js, (
        "Exec: stop cell does not default to em-dash"
    )
    assert "Number.isFinite(_stopInfo.eff)" in js, (
        "Exec: stop cell does not guard against non-finite eff"
    )


def test_exec_positions_stop_renders_trail_badge_when_armed():
    js = _slice_exec_positions(_read())
    # When the trail stop is armed on Main, the Val/Gene Stop cell must
    # show the same TRAIL badge that Main shows, so operators reading
    # either tab see the same exit posture.
    assert 'class=\\"trail-badge\\"' in js or 'class="trail-badge"' in js, (
        "Exec: Stop cell does not render TRAIL badge when armed"
    )
    assert "_stopInfo.trail" in js, (
        "Exec: TRAIL badge not gated on _stopInfo.trail"
    )


# ----- Both tables: column counts match -----

def test_main_and_exec_have_same_column_count():
    js = _read()
    main_block = _slice_main_positions(js)
    exec_block = _slice_exec_positions(js)
    # Count <th> tags in each header block. Both must be 8 (parity).
    main_thead_start = main_block.find("<thead>")
    main_thead_end = main_block.find("</thead>", main_thead_start)
    exec_thead_start = exec_block.find("<thead>")
    exec_thead_end = exec_block.find("</thead>", exec_thead_start)
    assert main_thead_start != -1 and exec_thead_start != -1, "thead boundaries"
    import re
    main_th = len(re.findall(r"<th\b", main_block[main_thead_start:main_thead_end]))
    exec_th = len(re.findall(r"<th\b", exec_block[exec_thead_start:exec_thead_end]))
    assert main_th == 8, f"Main thead expected 8 <th>, got {main_th}"
    assert exec_th == 8, f"Exec thead expected 8 <th>, got {exec_th}"
