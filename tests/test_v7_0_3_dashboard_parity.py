# tests/test_v7_0_3_dashboard_parity.py
# v7.0.3 -- dashboard val/gene parity + mobile positions overflow.
#
# Source-level checks against dashboard_static/app.js and app.css (no real
# browser available in CI). Pins the parity behaviour shipped in v7.0.3
# so a future refactor doesn't quietly regress to the old divergent
# inline-styled exec table or drop the trades-summary line.
#
# No em-dashes in this file (constraint for .py test files).
from __future__ import annotations

import os
import re


HERE = os.path.dirname(__file__)
APP_JS = os.path.join(HERE, "..", "dashboard_static", "app.js")
APP_CSS = os.path.join(HERE, "..", "dashboard_static", "app.css")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _slice_exec_skeleton(js: str) -> str:
    # The exec panel skeleton template lives inside execSkeleton().
    start = js.find("function execSkeleton(exec)")
    assert start != -1, "execSkeleton not found"
    # Skeleton is < 6 KB; take a generous window.
    return js[start:start + 8000]


def _slice_render_exec_trades(js: str) -> str:
    start = js.find("function renderExecTrades(panel, data, disabled)")
    assert start != -1, "renderExecTrades not found"
    return js[start:start + 6000]


# ----- Trades skeleton: trades-summary line present (parity with Main) -----

def test_exec_skeleton_emits_trades_summary_div():
    js = _slice_exec_skeleton(_read(APP_JS))
    # Main has <div class="trades-summary" id="trades-summary">. The exec
    # panels can't reuse the id (multiple panels), so they use the same
    # data-f hook pattern the rest of the panel uses.
    assert 'class="trades-summary" data-f="trades-summary"' in js, (
        "Exec: trades-summary skeleton missing -- exec panels won't show "
        "the opens/closes/realized/win-rate one-liner"
    )


def test_exec_trades_card_head_has_title_and_chip():
    js = _slice_exec_skeleton(_read(APP_JS))
    # v7.0.3 adds the same tooltip Main carries on the card title.
    assert "All fills (opens + closes) recorded today, newest first" in js, (
        "Exec: Today's trades card title missing parity tooltip"
    )
    assert 'data-f="trades-realized"' in js, "Exec: trades-realized chip hook missing"


# ----- renderExecTrades populates the summary line + classifies open/close -----

def test_render_exec_trades_uses_compute_summary_helper():
    js = _slice_render_exec_trades(_read(APP_JS))
    assert "computeTradesSummaryExec(trades)" in js, (
        "renderExecTrades must call computeTradesSummaryExec so the chip "
        "and summary aggregate match Main exactly"
    )


def test_compute_trades_summary_exec_classifies_short_and_cover():
    js = _read(APP_JS)
    # The helper is defined once in the val/gene IIFE.
    start = js.find("function computeTradesSummaryExec(trades)")
    assert start != -1, "computeTradesSummaryExec not defined"
    block = js[start:start + 800]
    # BUY|SHORT count as opens; SELL|COVER count as closes. This is the
    # parity guarantee with Main's computeTradesSummary.
    assert 'act === "BUY" || act === "SHORT"' in block, (
        "computeTradesSummaryExec must treat SHORT entries as opens"
    )
    assert 'act === "SELL" || act === "COVER"' in block, (
        "computeTradesSummaryExec must treat COVER fills as closes"
    )


def test_render_exec_trades_row_classifies_short_and_cover():
    js = _slice_render_exec_trades(_read(APP_JS))
    # The row template's open/close classification must match the summary
    # helper, otherwise SHORT entries render with buy-style chips.
    assert 'const isOpen = (act === "BUY" || act === "SHORT");' in js, (
        "Trade row template: SHORT not recognized as open"
    )
    assert 'const isClose = (act === "SELL" || act === "COVER");' in js, (
        "Trade row template: COVER not recognized as close"
    )
    # And the action chip class is gated on isClose, not on a raw
    # act === "SELL" check (which would miss COVER fills).
    assert 'const actCls = isClose ? "act-sell" : "act-buy";' in js, (
        "Trade row chip: act-sell class not gated on isClose"
    )


# ----- Val/Gene positions: no inline styles, uses Main's CSS classes -----

def _slice_exec_positions_block(js: str) -> str:
    head = js.find("Open positions card")
    assert head != -1, "exec pos-body region start not found"
    tail = js.find("Cash / BP / Invested / Shorted footer", head)
    assert tail != -1, "exec positions block end not found"
    return js[head:tail]


def test_exec_positions_table_has_no_inline_width_style():
    block = _slice_exec_positions_block(_read(APP_JS))
    # The pre-v7.0.3 markup used <table style="width:100%;...">. Main's
    # table has no inline styles, so the parity rule is: exec must not
    # either.
    assert 'style="width:100%' not in block, (
        "Exec positions table still uses inline style; should rely on "
        "the .card-body table CSS rule like Main does"
    )


def test_exec_positions_rows_use_side_dot_mark():
    block = _slice_exec_positions_block(_read(APP_JS))
    # Main rows render the side dot via:
    #   <span class="ticker">SYM <span class="mark mark-long|mark-short">...
    # v7.0.3 brings the same affordance to Val/Gene.
    assert 'class="ticker"' in block, "Exec positions: .ticker class missing"
    assert 'class="mark ${markCls}"' in block, (
        "Exec positions: side-dot mark span missing"
    )
    assert 'markCls = p.side === "SHORT" ? "mark-short" : "mark-long"' in block, (
        "Exec positions: markCls computation missing"
    )


def test_exec_positions_rows_carry_data_pos_ticker_attribute():
    block = _slice_exec_positions_block(_read(APP_JS))
    # Mirrors Main rows so any future click-to-Titan wiring on exec tabs
    # has a stable attribute to read.
    assert 'data-pos-ticker="${esc(p.symbol)}"' in block, (
        "Exec positions: data-pos-ticker attribute missing"
    )


def test_exec_positions_pnl_cell_uses_delta_classes():
    block = _slice_exec_positions_block(_read(APP_JS))
    # Main uses delta-up / delta-down on td.right; v7.0.3 mirrors this so
    # both tabs share the same green/red classes (and dark-mode friendly
    # CSS variables) instead of inline color: var(--up) styles.
    assert "delta-up" in block, "Exec positions: delta-up class missing"
    assert "delta-down" in block, "Exec positions: delta-down class missing"


# ----- Mobile CSS: positions table on phones drops % then Mark -----

def test_css_640px_block_drops_8th_column_on_pos_body():
    css = _read(APP_CSS)
    # Find the @media (max-width: 640px) block. There may be more than
    # one, but the positions rules live in the first one (Phone block).
    m = re.search(r"@media\s*\(\s*max-width:\s*640px\s*\)\s*\{", css)
    assert m is not None, "640px media block not found"
    # Walk forward to the matching close brace by depth count.
    start = m.end()
    depth = 1
    i = start
    while i < len(css) and depth > 0:
        c = css[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    block = css[start:i]
    # Must hide column 8 (the % column) on BOTH Main and exec selectors.
    assert "#pos-body table th:nth-child(8)" in block, (
        "640px block: missing #pos-body % column hide"
    )
    assert '[data-f="pos-body"] table th:nth-child(8)' in block, (
        "640px block: missing [data-f=pos-body] % column hide"
    )


def test_css_400px_block_drops_5th_column_on_pos_body():
    css = _read(APP_CSS)
    m = re.search(r"@media\s*\(\s*max-width:\s*400px\s*\)\s*\{", css)
    assert m is not None, "400px media block not found"
    start = m.end()
    depth = 1
    i = start
    while i < len(css) and depth > 0:
        c = css[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    block = css[start:i]
    # Must hide column 5 (the Mark column) on BOTH Main and exec selectors.
    assert "#pos-body table th:nth-child(5)" in block, (
        "400px block: missing #pos-body Mark column hide"
    )
    assert '[data-f="pos-body"] table th:nth-child(5)' in block, (
        "400px block: missing [data-f=pos-body] Mark column hide"
    )


def test_css_640px_rules_target_both_main_and_exec_selectors():
    css = _read(APP_CSS)
    # Sanity: the same rule body must be applied symmetrically to both.
    # We don't verify exact CSS shape -- just that we touch both selectors
    # in the v7.0.3 block (search broadly across the file is fine since
    # the only place these selectors appear is the v7.0.3 block).
    assert css.count("#pos-body table th:nth-child(8)") >= 1
    assert css.count('[data-f="pos-body"] table th:nth-child(8)') >= 1
    assert css.count("#pos-body table th:nth-child(5)") >= 1
    assert css.count('[data-f="pos-body"] table th:nth-child(5)') >= 1
