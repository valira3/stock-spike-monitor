"""v5.23.0 \u2014 Click-scroll fix + intraday chart panel + Last signal removal.

Verifies the v5.23.0 dashboard contract:

1. ``BOT_VERSION`` is ``5.23.0`` in ``bot_version.py``.

2. ``data-pmtx-comp-grid`` marker bumped to ``v5.23.0``.

3. The position-row click handler (``_posRowClick``) resolves the
   Permit Matrix body in three steps: ``getElementById("pmtx-body")``
   first (Main), then the active panel's ``[data-f="pmtx-body"]``
   (Val/Gene), then any ``[data-f="pmtx-body"]`` document-wide.

4. The ``Last signal`` card is removed from the Main, Val, and Gene
   tabs:
     - ``renderLastSignal`` function deleted from app.js
     - ``renderLastSignal(s);`` call site removed from ``renderAll``
     - ``last-sig-chip`` / ``last-sig-body`` references gone from
       active code (only allowed inside comments or removal notes)
     - The Main tab no longer wraps Today's trades in ``grid-2``;
       the section now uses bare ``class="grid"``.

5. Intraday chart panel is wired:
     - ``_pmtxIntradayChartPanel(tkr)`` returns HTML with
       ``data-intraday-chart`` and ``data-intraday-canvas`` markers.
     - ``_pmtxHydrateIntradayCharts`` exists and is called from
       ``_pmtxApplyExpanded`` when at least one row is open.
     - The panel is concatenated between ``_pmtxComponentGrid`` and
       ``_pmtxSmaStackPanel`` in the expanded-detail HTML.
     - Mobile breakpoint (``max-width: 720px``) shrinks the canvas.

6. Backend ``/api/intraday/{ticker}`` route is registered and the
   pure-function payload builder produces the expected shape from
   synthetic bars.

Source-grep assertions strip JS/CSS comments before scanning so
rationale comments that mention the retired ``last-sig-*`` hooks do
not trip the removal guards.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_JS = REPO_ROOT / "dashboard_static" / "app.js"
APP_CSS = REPO_ROOT / "dashboard_static" / "app.css"
INDEX_HTML = REPO_ROOT / "dashboard_static" / "index.html"
DASHBOARD_PY = REPO_ROOT / "dashboard_server.py"
BOT_VERSION_PY = REPO_ROOT / "bot_version.py"


def _strip_js_comments(src: str) -> str:
    no_block = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", no_block)


def _strip_html_comments(src: str) -> str:
    return re.sub(r"<!--.*?-->", "", src, flags=re.DOTALL)


def _strip_py_comments(src: str) -> str:
    # Only line comments \u2014 we keep docstrings since they may carry
    # legitimate cross-references that are not code references.
    return re.sub(r"(?m)#[^\n]*$", "", src)


# 1. Version pin -------------------------------------------------------
def test_bot_version_is_5_23_0():
    text = BOT_VERSION_PY.read_text(encoding="utf-8")
    assert 'BOT_VERSION = "5.24.0"' in text, "bot_version.py must report 5.24.0"


# 2. Component grid marker --------------------------------------------
def test_grid_marker_is_v5_23_0():
    js = APP_JS.read_text(encoding="utf-8")
    assert 'data-pmtx-comp-grid="v5.23.0"' in js, "data-pmtx-comp-grid must be bumped to v5.23.0"


# 3. Click-scroll fix --------------------------------------------------
def test_pos_click_uses_main_id_first():
    js = APP_JS.read_text(encoding="utf-8")
    code = _strip_js_comments(js)
    # Main path: getElementById("pmtx-body") must appear before any
    # data-f="pmtx-body" lookup inside the click handler.
    assert 'getElementById("pmtx-body")' in code, (
        "click handler must look up Main's id=pmtx-body first"
    )
    assert '[data-f="pmtx-body"]' in code, "fallback selector for Val/Gene panels must remain"
    # Verify ordering: getElementById appears before the data-f selector
    # inside the _posRowClick body. We slice from the function start.
    start = code.find("_posRowClick")
    assert start > 0, "_posRowClick handler must exist"
    snippet = code[start : start + 2000]
    id_pos = snippet.find('getElementById("pmtx-body")')
    df_pos = snippet.find('[data-f="pmtx-body"]')
    assert id_pos > 0 and df_pos > id_pos, (
        "getElementById lookup must precede data-f lookup in handler"
    )


# 4. Last signal removal ----------------------------------------------
def test_render_last_signal_function_removed():
    js = APP_JS.read_text(encoding="utf-8")
    code = _strip_js_comments(js)
    assert "function renderLastSignal" not in code, "renderLastSignal function must be removed"
    assert "renderLastSignal(s)" not in code, "renderLastSignal call must be removed from renderAll"


def test_last_sig_dom_hooks_removed():
    js = APP_JS.read_text(encoding="utf-8")
    code = _strip_js_comments(js)
    # Active code must not reference last-sig-chip or last-sig-body.
    assert '"last-sig-chip"' not in code, (
        "last-sig-chip data-f hook must be removed from active code"
    )
    assert '"last-sig-body"' not in code, (
        "last-sig-body data-f hook must be removed from active code"
    )
    assert 'data-f="last-sig-chip"' not in code, "last-sig-chip skeleton HTML must be removed"


def test_main_today_trades_full_width():
    html = INDEX_HTML.read_text(encoding="utf-8")
    stripped = _strip_html_comments(html)
    # The Today's trades panel on Main must not be wrapped in grid-2.
    # We allow other grid-2 sections elsewhere in the file but the
    # specific section that previously paired Last signal + trades
    # must collapse to a single-column grid.
    assert 'class="grid"' in stripped, "Main grid section should remain"
    # No section should still carry both last-sig and trades.
    assert "last-sig-body" not in stripped, "Main index.html must not carry last-sig-body markup"
    assert "Last signal" not in stripped, (
        "Main index.html must not still carry the 'Last signal' card title"
    )


# 5. Intraday chart panel wiring --------------------------------------
def test_intraday_panel_function_exists():
    js = APP_JS.read_text(encoding="utf-8")
    assert "function _pmtxIntradayChartPanel" in js, "_pmtxIntradayChartPanel must exist"
    assert "function _pmtxHydrateIntradayCharts" in js, "_pmtxHydrateIntradayCharts must exist"


def test_intraday_panel_html_markers():
    js = APP_JS.read_text(encoding="utf-8")
    assert "data-intraday-chart" in js, "data-intraday-chart marker required"
    assert "data-intraday-canvas" in js, "data-intraday-canvas marker required"
    assert "pmtx-intraday-section" in js, "section class required"


def test_intraday_panel_concat_order():
    js = APP_JS.read_text(encoding="utf-8")
    # v5.23.2 \u2014 expanded-row scan order:
    #   _pmtxComponentGrid \u2192 sentinelStripHtml \u2192 _pmtxSmaStackPanel
    #   \u2192 _pmtxIntradayChartPanel
    # Alarms (sentinel strip) sit right under the cards so they aren't
    # pushed below the heavy chart. Chart sits at the bottom because
    # it's the most visually heavy element.
    code = _strip_js_comments(js)
    grid_calls = [m.start() for m in re.finditer(r"\+\s*_pmtxComponentGrid\(", code)]
    sentinel_calls = [m.start() for m in re.finditer(r"\+\s*\(sentinelStripHtml\b", code)]
    sma_calls = [m.start() for m in re.finditer(r"\+\s*_pmtxSmaStackPanel\(", code)]
    chart_calls = [m.start() for m in re.finditer(r"\+\s*_pmtxIntradayChartPanel\(", code)]
    assert grid_calls and sentinel_calls and sma_calls and chart_calls, (
        "all four concat calls must exist"
    )
    # Required strict order: grid < sentinel < sma < chart.
    assert grid_calls[0] < sentinel_calls[0] < sma_calls[0] < chart_calls[0], (
        "expanded-row concat order must be: _pmtxComponentGrid -> sentinelStripHtml "
        "-> _pmtxSmaStackPanel -> _pmtxIntradayChartPanel"
    )


def test_intraday_hydrate_called_from_apply_expanded():
    js = APP_JS.read_text(encoding="utf-8")
    code = _strip_js_comments(js)
    start = code.find("function _pmtxApplyExpanded")
    assert start > 0, "_pmtxApplyExpanded must exist"
    body = code[start : start + 2500]
    assert "_pmtxHydrateIntradayCharts" in body, (
        "_pmtxApplyExpanded must call _pmtxHydrateIntradayCharts"
    )


def test_intraday_css_mobile_breakpoint():
    css = APP_CSS.read_text(encoding="utf-8")
    assert ".pmtx-intraday-section" in css, "intraday section CSS required"
    assert ".pmtx-intraday-canvas" in css, "canvas selector required"
    # Mobile breakpoint must shrink the canvas.
    mobile_block = re.search(
        r"@media \(max-width: 720px\) \{[^}]*\.pmtx-intraday-canvas[^}]*\}",
        css,
        re.DOTALL,
    )
    assert mobile_block, "mobile @media block must override .pmtx-intraday-canvas height"


# 6. Backend route + payload shape ------------------------------------
def test_backend_route_registered():
    py = DASHBOARD_PY.read_text(encoding="utf-8")
    code = _strip_py_comments(py)
    assert '"/api/intraday/{ticker}"' in code, "/api/intraday/{ticker} route must be registered"
    assert "async def h_intraday(" in code, "h_intraday handler must exist"
    assert "def _intraday_build_payload(" in code, (
        "_intraday_build_payload pure function must exist"
    )


def test_payload_shape_from_synthetic_bars(monkeypatch, tmp_path):
    # Drive the pure-function payload builder with a fake bar archive
    # under tmp_path. We mock _ssm() to return a stub module, redirect
    # _INTRADAY_BARS_DIR to tmp_path, force the Alpaca fetcher to
    # return [] so we exercise the on-disk fallback, and verify the
    # response keys.
    sys.path.insert(0, str(REPO_ROOT))
    import importlib

    mod = importlib.import_module("dashboard_server")
    # Bypass any in-process cache from previous tests / runs.
    mod._INTRADAY_FETCH_CACHE.clear()
    # Force Alpaca fetcher to return [] so we exercise on-disk fallback.
    monkeypatch.setattr(mod, "_intraday_fetch_alpaca_bars", lambda t, d: [])

    # Build a tiny bar file: 3 bars at 09:30, 09:31, 09:32 ET.
    from datetime import datetime, timezone, timedelta

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bars_dir = tmp_path / day
    bars_dir.mkdir(parents=True)
    f = bars_dir / "AAPL.jsonl"
    # 09:30 ET = 13:30 UTC during EDT (April), 14:30 UTC during EST.
    # We use 13:30/13:31/13:32 UTC \u2014 month is April 2026 so EDT.
    base_h = 13
    lines = []
    for mm, price in enumerate([100.0, 101.0, 102.0]):
        ts = f"{day}T{base_h:02d}:{30 + mm:02d}:00+00:00"
        # NOTE: prod archive uses open/high/low/close (NOT o/h/l/c).
        # See backtest/loader.py and the JSONL written by trade_genius.
        lines.append(
            '{"ts": "'
            + ts
            + '", "open": '
            + str(price)
            + ', "high": '
            + str(price + 0.5)
            + ', "low": '
            + str(price - 0.5)
            + ', "close": '
            + str(price + 0.2)
            + ', "iex_volume": 1000}'
        )
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")

    monkeypatch.setattr(mod, "_INTRADAY_BARS_DIR", str(tmp_path))

    class _StubMod:
        or_high = {"AAPL": 105.0}
        or_low = {"AAPL": 99.0}
        # v5.23.3: marker source switched from trade_log to paper_state.
        positions: dict = {}
        short_positions: dict = {}
        trade_history: list = []
        short_trade_history: list = []

        def _alpaca_data_client(self):
            return None

    monkeypatch.setattr(mod, "_ssm", lambda: _StubMod())

    payload = mod._intraday_build_payload("AAPL")
    assert payload["ok"] is True
    assert payload["ticker"] == "AAPL"
    assert payload["date"] == day
    assert payload["or_high"] == 105.0
    assert payload["or_low"] == 99.0
    assert isinstance(payload["bars"], list)
    assert payload["bar_count"] == 3
    # Each bar carries the chart-required keys.
    for b in payload["bars"]:
        assert "et_min" in b
        assert "o" in b and "h" in b and "l" in b and "c" in b
        assert "avwap" in b  # may be None for non-RTH
    # Critical regression guard: confirm OHLC values are populated
    # (not null). v5.23.0 had a bug where prod helpers read o/h/l/c
    # but archive stored open/high/low/close, producing all-null bars.
    first = payload["bars"][0]
    assert first["o"] == 100.0, f"open should be populated, got {first['o']}"
    assert first["h"] == 100.5, f"high should be populated, got {first['h']}"
    assert first["l"] == 99.5, f"low should be populated, got {first['l']}"
    assert first["c"] == 100.2, f"close should be populated, got {first['c']}"
    assert isinstance(payload["trades"], list)


# 7. v5.23.3 \u2014 extended-hours window + paper_state-sourced markers ---
def test_v5_23_3_alpaca_fetcher_exists_and_uses_iex_feed():
    """The new historical fetcher must be defined and request the IEX feed."""
    py = DASHBOARD_PY.read_text(encoding="utf-8")
    code = _strip_py_comments(py)
    assert "def _intraday_fetch_alpaca_bars(" in code, (
        "_intraday_fetch_alpaca_bars helper must exist"
    )
    # Locate the function body and verify it uses DataFeed.IEX (free tier).
    start = code.find("def _intraday_fetch_alpaca_bars(")
    body = code[start : start + 3000]
    assert "DataFeed.IEX" in body, "_intraday_fetch_alpaca_bars must request feed=DataFeed.IEX"
    assert "TimeFrame.Minute" in body, "fetcher must request 1m bars"


def test_v5_23_3_load_today_bars_calls_alpaca_first():
    """_intraday_load_today_bars must try Alpaca before the on-disk archive."""
    py = DASHBOARD_PY.read_text(encoding="utf-8")
    code = _strip_py_comments(py)
    start = code.find("def _intraday_load_today_bars(")
    assert start > 0, "_intraday_load_today_bars must exist"
    body = code[start : start + 2000]
    alpaca_pos = body.find("_intraday_fetch_alpaca_bars(")
    archive_pos = body.find("load_bars(")
    assert alpaca_pos > 0, "must call Alpaca fetcher"
    assert archive_pos > 0, "must keep on-disk archive fallback"
    assert alpaca_pos < archive_pos, "Alpaca fetch must precede on-disk archive fallback"


def test_v5_23_3_today_trades_reads_paper_state():
    """_intraday_today_trades must read paper_state globals, not trade_log."""
    py = DASHBOARD_PY.read_text(encoding="utf-8")
    code = _strip_py_comments(py)
    start = code.find("def _intraday_today_trades(")
    assert start > 0, "_intraday_today_trades must exist"
    # Find the end of the function (next top-level def).
    end = code.find("\ndef ", start + 1)
    body = code[start : end if end > 0 else start + 6000]
    # Required sources.
    assert 'getattr(m, "positions"' in body, "must read paper_state.positions"
    assert 'getattr(m, "short_positions"' in body, "must read paper_state.short_positions"
    assert 'getattr(m, "trade_history"' in body, "must read paper_state.trade_history"
    assert 'getattr(m, "short_trade_history"' in body, "must read paper_state.short_trade_history"
    # Must NOT use the old trade_log_read_tail path.
    assert "trade_log_read_tail" not in body, (
        "v5.23.3 markers no longer come from trade_log_read_tail"
    )


def test_v5_23_3_today_trades_emits_open_flag_and_full_iso():
    """Open positions must surface with open=True and full ISO entry_ts."""
    sys.path.insert(0, str(REPO_ROOT))
    import importlib

    mod = importlib.import_module("dashboard_server")

    class _M:
        positions = {
            "AAPL": {
                "entry_price": 200.0,
                "shares": 10,
                "entry_ts_utc": "2026-04-30T14:35:00+00:00",
            }
        }
        short_positions: dict = {}
        trade_history: list = []
        short_trade_history = [
            {
                "ticker": "NVDA",
                "side": "short",
                "shares": 5,
                "entry_price": 100.0,
                "exit_price": 99.0,
                "pnl": 5.0,
                "reason": "TP",
                "entry_time_iso": "2026-04-30T15:00:00+00:00",
                "exit_time_iso": "2026-04-30T15:30:00+00:00",
            }
        ]

    rows = mod._intraday_today_trades(_M(), "AAPL", "2026-04-30")
    assert len(rows) == 1
    assert rows[0]["open"] is True
    assert rows[0]["side"] == "LONG"
    assert rows[0]["entry_ts"] == "2026-04-30T14:35:00+00:00"
    assert rows[0]["entry_price"] == 200.0
    assert rows[0]["exit_ts"] is None

    nvda = mod._intraday_today_trades(_M(), "NVDA", "2026-04-30")
    assert len(nvda) == 1
    assert nvda[0]["open"] is False
    assert nvda[0]["side"] == "SHORT"
    assert nvda[0]["exit_price"] == 99.0
    assert nvda[0]["realized_pnl"] == 5.0


def test_v5_23_3_extended_hours_x_axis_bumped():
    """app.js plot window must span 8am\u201318:00 ET (480\u20131080 et_min)."""
    js = APP_JS.read_text(encoding="utf-8")
    code = _strip_js_comments(js)
    # New window: minutes-of-ET from 480 (8am ET = 7am CT) to 1080 (18:00 ET = 17:00 CT).
    assert "480" in code and "1080" in code, "new x-axis bounds must appear"
    # Old 240/960 window must not be the active plot bounds in the chart code.
    # We allow the literals to appear in unrelated math, but the canonical
    # `[240, 960]` pair must not survive.
    assert "[240, 960]" not in code, "old plot window must be removed"


def test_v5_23_3_intraday_window_constants():
    """dashboard_server must expose 480/1080 ET-minute window constants."""
    py = DASHBOARD_PY.read_text(encoding="utf-8")
    code = _strip_py_comments(py)
    assert "_INTRADAY_WINDOW_START_ET_MIN" in code, "start-ET window constant required"
    assert "_INTRADAY_WINDOW_END_ET_MIN" in code, "end-ET window constant required"
    assert "_INTRADAY_FETCH_CACHE" in code, "in-process cache map required"
    assert "_INTRADAY_FETCH_TTL_S" in code, "in-process cache TTL constant required"
