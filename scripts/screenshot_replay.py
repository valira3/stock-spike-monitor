#!/usr/bin/env python3
"""screenshot_replay.py -- capture dashboard screenshots at key time periods.

Builds the replay HTML, opens it in headless Chromium via playwright,
navigates to each key snapshot, and saves PNG screenshots for visual audit.
Captures Main and Val tabs separately, and expands EOD positions to check charts.
"""
import json, os, sys, pathlib, urllib.request, urllib.parse, re, time
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright

REPO = pathlib.Path(__file__).parent.parent
OUT  = REPO / "data" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)

# Key snapshots to capture: (index, label)
KEY_SNAPSHOTS = [
    ( 0, "0930_market_open"),
    ( 6, "1000_or_locked"),
    ( 8, "1008_nvda_entry"),
    (13, "1026_orcl_entry"),
    (15, "1035_two_positions"),
    (18, "1048_t1_exit_t2_entry"),
    (21, "1100_nvda_orcl_running"),
    (23, "1107_kill_triggered"),
    (24, "1110_nvda_survivor"),
    (28, "1130_nvda_partial"),
    (34, "1200_nvda_runner"),
    (43, "1245_nvda_target"),
    (46, "1300_quiet"),
    (70, "1500_eod_armed"),
    (76, "1530_eod_open"),
    (79, "1545_eod_running"),
    (82, "1559_eod_close"),
    (83, "1600_end_of_day"),
]

# EOD snapshots where we also click a position to check the chart
EOD_CHART_SNAPSHOTS = {76, 79, 82}
EOD_CHART_TICKER = "AVGO"   # ticker to expand and screenshot chart for

def build_html_file() -> pathlib.Path:
    """Build the replay HTML and write to a temp file."""
    jar = urllib.request.HTTPCookieProcessor()
    opener = urllib.request.build_opener(jar)
    opener.open(urllib.request.Request(
        'https://tradegenius.up.railway.app/login',
        data=urllib.parse.urlencode({'password':'3YhCoi5AIZYAFG7eDua8bD8Z'}).encode(),
        headers={'Content-Type':'application/x-www-form-urlencoded'}))
    base = json.loads(opener.open('https://tradegenius.up.railway.app/api/state').read())

    from scripts.gen_scenarios import build_full_day, build_and_upload_combined
    import scripts.replay_dashboard as rd

    print("Building full-day snapshots...")
    snaps = build_full_day(base)
    print(f"  {len(snaps)} snapshots")

    mid = next(s for s in snaps if '12:00' in s['ts_et'])
    base_state = dict(mid['_full_state'])
    base_state.pop("trades_today", None)
    base_state.pop("positions", None)

    html = rd.build_html(snaps, base_state, start_idx=0)

    slim_diffs = []
    for d in snaps:
        di = d.get("diff", {})
        slim_diffs.append({
            "ts_et": d["ts_et"], "captured_at_utc": d["captured_at_utc"],
            "kind": d.get("kind",""), "label": d.get("label",""),
            "positions": di.get("positions",[]), "trades_today": di.get("trades_today",[]),
            "portfolio": di.get("portfolio",{}),
            "v10_admit_count": di.get("v10_admit_count", 0),
        })
    out  = REPO / "data" / "replay_local.html"
    out.write_text(html, encoding="utf-8")
    print(f"  Written to {out} ({out.stat().st_size // 1024} KB)")
    return out, slim_diffs


def _nav(page, idx):
    page.evaluate(f"if(typeof window.ttNav==='function') window.ttNav({idx}); "
                  f"else {{ window.__TT_IDX={idx}; if(window.__ttFireSSE) window.__ttFireSSE(); }}")
    page.wait_for_timeout(900)


def _shot(page, path, full=True):
    page.screenshot(path=str(path), full_page=full)


def _txt(page, sel, default="?"):
    try:
        el = page.query_selector(sel)
        return el.inner_text().strip() if el else default
    except:
        return default


def _switch_tab(page, tab_id):
    """Click a dashboard tab button. tab_id: 'main', 'val', 'gene'"""
    try:
        # Try data-tab attribute first, then text match
        sel = f'[data-tab="{tab_id}"], button#{tab_id}-tab, a[href="#{tab_id}"]'
        btn = page.query_selector(sel)
        if btn:
            btn.click()
            page.wait_for_timeout(600)
            return True
        # Fallback: click any tab button whose text matches
        for btn in page.query_selector_all('button, a'):
            try:
                t = btn.inner_text().strip().lower()
                if tab_id in t:
                    btn.click()
                    page.wait_for_timeout(600)
                    return True
            except:
                pass
    except:
        pass
    return False


def _expand_position_chart(page, ticker):
    """Expand a position's inline chart via JS (bypasses click delegation issues)."""
    try:
        # Try direct row click first
        row = page.query_selector(f'tr[data-pos-ticker="{ticker}"]')
        if not row:
            return False
        row.scroll_into_view_if_needed()
        row.click(force=True)
        page.wait_for_timeout(400)

        # If chart not mounted yet, trigger __tgRenderTickerChart directly via JS
        has_canvas = page.query_selector('[data-intraday-canvas]')
        if not has_canvas:
            page.evaluate(f"""
                (function() {{
                    // Find or create the chart mount for {ticker!r}
                    var mount = document.querySelector('[data-chart-mount="{ticker}"]');
                    if (!mount) {{
                        // Find the pos-body and look for expanded row
                        var row = document.querySelector('tr[data-pos-ticker="{ticker}"]');
                        if (!row) return;
                        // Simulate toggle via the pos-body __posExpanded set
                        var body = row.closest('tbody, [id^="pos"]');
                        if (body) {{
                            if (!body.__posExpanded) body.__posExpanded = new Set();
                            body.__posExpanded.add("{ticker}");
                        }}
                        row.click();
                        return;
                    }}
                    if (typeof window.__tgRenderTickerChart === 'function') {{
                        window.__tgRenderTickerChart("{ticker}", mount);
                    }}
                }})();
            """)
            page.wait_for_timeout(1200)

        return True
    except Exception as e:
        print(f"    [chart-expand-err] {e}")
        return False


def screenshot_all(html_path: pathlib.Path, slim_diffs: list) -> list[dict]:
    """Open in headless browser, screenshot each snapshot across Main+Val tabs."""
    url = html_path.as_uri()
    findings = []
    js_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=2,
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
        )
        page = ctx.new_page()
        page.on("console", lambda m: js_errors.append(f"[{m.type}] {m.text}") if m.type == "error" else None)

        print(f"\nOpening {url}")
        page.goto(url)
        page.wait_for_timeout(1200)

        for idx, label in KEY_SNAPSHOTS:
            if idx >= len(slim_diffs):
                print(f"  [skip] idx={idx} out of range")
                continue

            hhmm = slim_diffs[idx]["ts_et"][11:16]
            snap = slim_diffs[idx]

            _nav(page, idx)

            # ── MAIN TAB ──────────────────────────────────────────────────────
            shot_main = OUT / f"{label}_main.png"
            _shot(page, shot_main)

            equity_val  = _txt(page, "#k-equity")
            pnl_val     = _txt(page, "#k-pnl")
            session_val = _txt(page, "#k-session")
            # Scope to #pos-body so Val/Gene tab rows (also in DOM) aren't counted
            pos_count   = len(page.query_selector_all("#pos-body tr[data-pos-ticker]:not(.pos-progress-row)"))
            trade_count = len(page.query_selector_all("#trades-body .trade-row, #trades-body tr"))
            prox_count  = len(page.query_selector_all(".prox-row, tr[data-prox-ticker]"))
            act_count   = len(page.query_selector_all(".act-row, .activity-row, [data-act-kind]"))
            banner_vis  = page.query_selector("#banner:not(.hide)") is not None
            tt_ts       = _txt(page, "#__tt_ts")
            admit_txt   = page.evaluate("""
                () => {
                    var els = document.querySelectorAll('.v10-gauge-value');
                    for (var i = 0; i < els.length; i++) {
                        if (els[i].textContent.indexOf('total') >= 0) return els[i].textContent;
                    }
                    return '?';
                }
            """)

            expected_pos    = len(snap.get("positions", []))
            expected_trades = len(snap.get("trades_today", []))
            expected_admit  = snap.get("v10_admit_count", 0)

            pos_ok   = pos_count   == expected_pos
            trade_ok = trade_count == expected_trades
            issues   = []
            if not pos_ok:   issues.append(f"pos: DOM={pos_count} exp={expected_pos}")
            if not trade_ok: issues.append(f"trd: DOM={trade_count} exp={expected_trades}")
            if banner_vis:   issues.append("disconnect banner")

            # ── EOD chart check via direct JS fetch (bypasses DOM click) ────────
            chart_bars = None
            if idx in EOD_CHART_SNAPSHOTS:
                chart_data = page.evaluate(f"""
                    async () => {{
                        try {{
                            const r = await fetch('/api/intraday/{EOD_CHART_TICKER}');
                            const d = await r.json();
                            return {{
                                ok: d.ok,
                                bar_count: (d.bars || []).length,
                                or_high: d.or_high,
                                or_low: d.or_low,
                                date: d.date,
                                first_bar: (d.bars||[])[0] ? (d.bars[0].et_min + '/' + d.bars[0].c) : null,
                                last_bar:  (d.bars||[]).slice(-1)[0] ? (d.bars.slice(-1)[0].et_min + '/' + d.bars.slice(-1)[0].c) : null,
                            }};
                        }} catch(e) {{ return {{error: String(e)}}; }}
                    }}
                """)
                chart_bars = chart_data
                bar_count = chart_data.get("bar_count", 0) if isinstance(chart_data, dict) else 0
                issues_chart = []
                if bar_count == 0:
                    issues_chart.append(f"chart bars=0: or_high={chart_data.get('or_high')} or_low={chart_data.get('or_low')}")
                if chart_data.get("error"):
                    issues_chart.append(f"chart error: {chart_data['error']}")
                print(f"  [{idx:2d}] {hhmm}  CHART {EOD_CHART_TICKER}: bars={bar_count}  "
                      f"OR={chart_data.get('or_high')}/{chart_data.get('or_low')}  "
                      f"date={chart_data.get('date')}  "
                      f"first={chart_data.get('first_bar')}  last={chart_data.get('last_bar')}"
                      + (f"  !! {' | '.join(issues_chart)}" if issues_chart else "  OK"))
                issues += issues_chart

            # ── VAL TAB ───────────────────────────────────────────────────────
            val_switched = _switch_tab(page, "val")
            shot_val = OUT / f"{label}_val.png"
            _shot(page, shot_val)
            val_pos   = len(page.query_selector_all("[data-f='pos-body'] tr[data-pos-ticker]:not(.pos-progress-row), #val-pos-body tr[data-pos-ticker]:not(.pos-progress-row)"))
            val_trade = len(page.query_selector_all("[data-f='trades-body'] .trade-row, [data-f='trades-body'] tr"))
            val_admit = page.evaluate("""
                () => {
                    var els = document.querySelectorAll('.v10-gauge-value');
                    for (var i = 0; i < els.length; i++) {
                        if (els[i].textContent.indexOf('total') >= 0) return els[i].textContent;
                    }
                    return '?';
                }
            """)
            # Switch back to Main for next iteration
            _switch_tab(page, "main")
            page.wait_for_timeout(400)

            def safe(s): return str(s).encode('ascii','replace').decode()
            status = "OK" if not issues else "ISSUES"
            print(f"  [{idx:2d}] {hhmm}  {status:6s}  "
                  f"equity={safe(equity_val):14}  pnl={safe(pnl_val):14}  "
                  f"pos={pos_count}(exp={expected_pos})  trd={trade_count}(exp={expected_trades})  "
                  f"admit={safe(admit_txt)}(exp={expected_admit})  "
                  f"val_pos={val_pos}  val_trd={val_trade}  val_admit={safe(val_admit)}"
                  + (f"\n         !! {' | '.join(issues)}" if issues else ""))

            findings.append({
                "idx": idx, "hhmm": hhmm, "label": label,
                "equity": equity_val, "pnl": pnl_val, "session": session_val,
                "dom_positions": pos_count, "dom_trades": trade_count,
                "expected_positions": expected_pos, "expected_trades": expected_trades,
                "banner_visible": banner_vis,
                "val_positions": val_pos, "val_trades": val_trade,
                "chart_bars": chart_bars,
                "issues": issues,
                "shot_main": str(shot_main),
                "shot_val": str(shot_val),
            })

        browser.close()

    if js_errors:
        print(f"\n=== JS ERRORS ({len(js_errors)}) ===")
        for e in js_errors[:20]:
            print(f"  {e}")

    return findings


if __name__ == "__main__":
    html_path, slim_diffs = build_html_file()
    findings = screenshot_all(html_path, slim_diffs)

    print(f"\n=== SUMMARY ===")
    total_issues = sum(len(f["issues"]) for f in findings)
    print(f"Screenshots saved to: {OUT}")
    print(f"Total issues found: {total_issues}")
    for f in findings:
        if f["issues"]:
            print(f"  [{f['idx']:2d}] {f['hhmm']}: {' | '.join(f['issues'])}")
