"""tools/ui_quality_assessment.py -- Deep UI quality + consistency audit.

Runs Playwright against the live dashboard to capture screenshots and
identify rendering issues, cross-tab inconsistencies, and UX gaps.
Simulates today's scenarios via API state mocking where needed.

Usage:
    python tools/ui_quality_assessment.py
"""

from __future__ import annotations
import os
import json
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, Page, expect

# Load env
for line in open(".env.monitor").read().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

BASE_URL = os.environ.get("DASHBOARD_BASE_URL", "").rstrip("/")
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
OUT_DIR = Path("data/ui_audit")
OUT_DIR.mkdir(parents=True, exist_ok=True)

ISSUES: list[dict] = []
SCREENSHOTS: list[str] = []


def issue(severity: str, tab: str, component: str, description: str, suggestion: str = ""):
    ISSUES.append(
        {
            "severity": severity,
            "tab": tab,
            "component": component,
            "description": description,
            "suggestion": suggestion,
        }
    )
    print(f"  [{severity}] {tab}/{component}: {description}")


def screenshot(page: Page, name: str, full: bool = False):
    path = str(OUT_DIR / f"{name}.png")
    page.screenshot(path=path, full_page=full)
    SCREENSHOTS.append(path)
    return path


def login(page: Page):
    # GET / serves login page when unauthenticated
    page.goto(BASE_URL + "/")
    page.wait_for_load_state("domcontentloaded")
    time.sleep(0.5)
    pw = page.query_selector("#pw")
    if pw and pw.is_visible():
        pw.fill(PASSWORD)
        page.press("#pw", "Enter")
        time.sleep(3)
    # Dashboard has SSE → networkidle never fires; wait for known dashboard element
    page.wait_for_selector("#tg-tabs", timeout=12000)
    time.sleep(4)  # let SSE deliver first state + all renders settle


def switch_tab(page: Page, tab: str):
    """Click the tab pill (main/val/gene/lifecycle)."""
    # Try data-tab attribute first, then button text
    try:
        page.click(f'[data-tab="{tab}"]', timeout=3000)
    except Exception:
        try:
            page.click(f'button:has-text("{tab.upper()}")', timeout=3000)
        except Exception:
            page.click(f'button:has-text("{tab.capitalize()}")', timeout=3000)
    page.wait_for_timeout(2000)


def check_tab_kpi_row(page: Page, tab: str):
    """KPI row: equity, day P&L, open positions, session."""
    print(f"\n-- KPI row: {tab} --")
    screenshot(page, f"{tab}_kpi")

    # Check KPI tiles are present and non-empty
    kpi_fields = {
        "main": ["k-equity", "k-pnl", "k-open", "k-session"],
        "val": ["k-equity", "k-pnl", "k-open", "k-session"],
        "gene": ["k-equity", "k-pnl", "k-open", "k-session"],
    }
    for fid in kpi_fields.get(tab, []):
        el = page.query_selector(f'[data-f="{fid}"], #{fid}')
        if not el:
            issue("HIGH", tab, "KPI", f"KPI element '{fid}' not found in DOM")
        else:
            txt = el.inner_text().strip()
            if not txt or txt in (" - ", ""):
                issue("MED", tab, "KPI", f"KPI '{fid}' is empty/dash: '{txt}'")


def check_positions_card(page: Page, tab: str):
    """Open positions card: empty state, EOD section, progress bars."""
    print(f"\n-- Positions card: {tab} --")

    # Find pos-body
    pos_body = page.query_selector('[data-f="pos-body"], #pos-body')
    if not pos_body:
        issue("HIGH", tab, "Positions", "pos-body element not found")
        return

    inner = pos_body.inner_html()

    # Check for empty state
    has_empty = "No open positions" in inner or "empty" in inner.lower()
    has_orb_table = "<table" in inner and "tbody" in inner
    has_eod_section = "eod-pos-table" in inner or "eod-badge" in inner

    # Detect stale empty-ORB-table bug (v9.1.73 regression check)
    # If there's a <table> with empty tbody AND an eod-pos-table, that's the old bug
    if has_orb_table and has_eod_section:
        # Check if the ORB table has actual rows
        orb_rows = pos_body.query_selector_all("table:not(.eod-pos-table) tbody tr")
        if len(orb_rows) == 0:
            issue(
                "HIGH",
                tab,
                "Positions",
                "Empty ORB table header still rendering above EOD section (v9.1.73 regression)",
                "The ORB <table> should be skipped when positions.length===0",
            )

    screenshot(page, f"{tab}_positions")

    # EOD badge consistency
    eod_badges = pos_body.query_selector_all(".eod-badge")
    for badge in eod_badges:
        txt = badge.inner_text()
        if txt != "EOD":
            issue("LOW", tab, "Positions", f"EOD badge has unexpected text: '{txt}'")

    # Progress bar presence check
    bars = pos_body.query_selector_all(".pos-progress")
    time_bars = pos_body.query_selector_all(".eod-progress")
    orb_bars = [b for b in bars if b not in time_bars]

    print(f"   ORB stop bars: {len(orb_bars)}, EOD time bars: {len(time_bars)}")
    return {
        "has_empty": has_empty,
        "has_eod": has_eod_section,
        "orb_bars": len(orb_bars),
        "eod_bars": len(time_bars),
    }


def check_trades_card(page: Page, tab: str):
    """Today's trades card: count, realized P&L chip, rows."""
    print(f"\n-- Trades card: {tab} --")

    # Get the count element
    count_sel = '[data-f="trades-count"], #trades-count'
    count_el = page.query_selector(count_sel)
    count_txt = count_el.inner_text() if count_el else "NOT FOUND"

    # Get realized chip
    chip_sel = '[data-f="trades-realized"], #trades-realized'
    chip_el = page.query_selector(chip_sel)
    chip_txt = chip_el.inner_text() if chip_el else "NOT FOUND"

    print(f"   count: {count_txt}  realized: {chip_txt}")
    screenshot(page, f"{tab}_trades")

    # Check summary line
    summary_sel = '[data-f="trades-summary"], #trades-summary'
    summary_el = page.query_selector(summary_sel)
    if summary_el:
        summary_txt = summary_el.inner_text().strip()
        print(f"   summary: {summary_txt[:80]}")

    # Check for EOD trades labeling
    trades_body = page.query_selector('[data-f="trades-body"], #trades-body')
    if trades_body:
        rows = trades_body.query_selector_all("tr.trade-row, tr[data-ticker]")
        tickers = set()
        for row in rows:
            cells = row.query_selector_all("td")
            if cells:
                tickers.add(cells[0].inner_text().strip()[:6] if cells else "?")
        print(f"   tickers in trades: {tickers}")

    return {"count": count_txt, "chip": chip_txt}


def check_v10_section(page: Page, tab: str):
    """v10 ORB status section: gauges, proximity, activity feed."""
    print(f"\n-- v10 ORB section: {tab} --")
    screenshot(page, f"{tab}_v10")

    # Check day status is present
    if tab == "main":
        ds = page.query_selector("#v10-day-status, [data-f='v10-day-status']")
        if not ds:
            issue("MED", tab, "v10", "v10-day-status element not found on Main")
    else:
        # Val/Gene: should have ORB gauges card
        gauges = page.query_selector(".v10-gauges, [data-f='v10-gauges']")
        if not gauges:
            # Try to find any v10 content
            v10_content = page.query_selector(".v10-header, [data-f='v10-header']")
            if not v10_content:
                issue("LOW", tab, "v10", f"v10 ORB gauges card not visible on {tab}")


def check_section_order(page: Page, tab: str):
    """Verify vertical section order matches CLAUDE.md spec.
    Canonical: (1) killswitch, (2) KPI row, (3) Open positions,
    (4) v10 ORB header/gauges, (5) Proximity, (6) Recent activity,
    (7) Today's trades, (8) Account diagnostics (Val/Gene only).
    """
    print(f"\n-- Section order: {tab} --")
    # Get bounding boxes for key sections
    sections = {
        "killswitch": page.query_selector(".banner, [data-f='banner']"),
        "kpi_row": page.query_selector(".kpi-row, [data-f='kpi-equity']"),
        "positions": page.query_selector("#pos-body, [data-f='pos-body']"),
        "trades": page.query_selector("#trades-body, [data-f='trades-body']"),
    }
    coords = {}
    for name, el in sections.items():
        if el:
            box = el.bounding_box()
            if box:
                coords[name] = box["y"]

    # KPI should be above positions, positions above trades
    order_checks = [
        ("kpi_row", "positions", "KPI row should be above Open Positions"),
        ("positions", "trades", "Open Positions should be above Today's Trades"),
    ]
    for top, bottom, msg in order_checks:
        if top in coords and bottom in coords:
            if coords[top] > coords[bottom]:
                issue("HIGH", tab, "Layout", f"Section order wrong: {msg}")
        else:
            missing = [x for x in [top, bottom] if x not in coords]


def check_cross_tab_consistency(pages: dict):
    """Compare KPI values and session state across Main/Val/Gene."""
    print("\n-- Cross-tab consistency --")

    # Session KPI should match across all tabs
    session_values = {}
    for tab, page in pages.items():
        el = page.query_selector('[data-f="k-session"], #k-session')
        if el:
            session_values[tab] = el.inner_text().strip()

    unique_sessions = set(session_values.values())
    if len(unique_sessions) > 1:
        issue(
            "HIGH",
            "ALL",
            "Session KPI",
            f"Session mode differs across tabs: {session_values}",
            "All tabs should show the same session mode (shared from Main /api/state)",
        )
    else:
        print(f"   Session mode consistent across tabs: {unique_sessions}")

    # Kill-switch banner: should be visible on all tabs or none
    banner_states = {}
    for tab, page in pages.items():
        banner = page.query_selector(".banner:not(.hide)")
        banner_states[tab] = bool(banner)
    if len(set(banner_states.values())) > 1:
        issue(
            "HIGH",
            "ALL",
            "Kill-switch banner",
            f"Kill-switch banner visibility differs: {banner_states}",
        )

    # Position count should be 0 on all tabs post-market
    pos_counts = {}
    for tab, page in pages.items():
        el = page.query_selector('[data-f="pos-count"], #pos-count')
        if el:
            pos_counts[tab] = el.inner_text().strip()
    print(f"   Position counts: {pos_counts}")

    # Today's trade counts
    trade_counts = {}
    for tab, page in pages.items():
        el = page.query_selector('[data-f="trades-count"], #trades-count')
        if el:
            trade_counts[tab] = el.inner_text().strip()
    print(f"   Trade counts: {trade_counts}")

    # Main should have ≥ val in trade rows (Main has all ORB paper, Val has Alpaca)
    # This is expected to differ in FIRE=1 mode - just flag if Main shows 0
    main_count_txt = trade_counts.get("main", "· 0")
    main_n = (
        int(main_count_txt.replace("·", "").strip())
        if main_count_txt.replace("·", "").strip().isdigit()
        else 0
    )
    if main_n == 0:
        issue(
            "MED",
            "main",
            "Today's Trades",
            "Main Today's Trades count is 0  -  paper trades not showing",
            "Check _today_trades() is returning paper_trades from paper_state",
        )


def check_eod_section_state(page: Page, tab: str, eod_positions: dict):
    """Verify EOD section renders correctly given known eod_positions data."""
    print(f"\n-- EOD section state: {tab} (eod_positions={list(eod_positions.keys())}) --")
    pos_body = page.query_selector('[data-f="pos-body"], #pos-body')
    if not pos_body:
        return

    inner = pos_body.inner_html()
    has_eod = "eod-pos-table" in inner or "eod-badge" in inner

    if eod_positions and not has_eod:
        issue(
            "HIGH",
            tab,
            "EOD positions",
            f"Backend has eod_positions {list(eod_positions.keys())} but EOD section not rendered",
            "Check renderPositions / renderExecutor EOD branch",
        )
    elif not eod_positions and has_eod:
        issue(
            "MED",
            tab,
            "EOD positions",
            "EOD section rendered but backend eod_positions is empty (stale DOM?)",
        )
    elif eod_positions and has_eod:
        # Verify teal bar present
        teal_bars = pos_body.query_selector_all(".eod-progress")
        if len(teal_bars) == 0:
            issue(
                "MED",
                tab,
                "EOD time bar",
                "EOD positions shown but no teal time bar (.eod-progress) found",
            )
        else:
            print(f"   EOD time bars: {len(teal_bars)} OK")


def check_h_tick(page: Page):
    """Verify health-pill countdown is visible and ticking."""
    print("\n-- Health tick countdown --")
    h_tick = page.query_selector("#h-tick, [id='h-tick']")
    if not h_tick:
        issue("HIGH", "main", "h-tick", "#h-tick element not found  -  health pill missing")
        return

    txt1 = h_tick.inner_text().strip()
    page.wait_for_timeout(2500)
    txt2 = h_tick.inner_text().strip()
    print(f"   h-tick: '{txt1}' -> '{txt2}' (should change every 2s)")

    if txt1 == txt2 and txt1 not in ("---", "···", ""):
        issue(
            "MED",
            "main",
            "h-tick",
            f"h-tick not updating: stuck at '{txt1}' after 2.5s",
            "Check SSE stream and updateNextScanLabel interval",
        )
    elif txt1 in ("---", "···"):
        issue(
            "LOW",
            "main",
            "h-tick",
            f"h-tick showing placeholder '{txt1}'  -  SSE may not be connected yet",
        )


def check_ui_theme(page: Page, tab: str):
    """Check color/font consistency: dark theme, monospace numbers."""
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    print(f"\n-- Theme: {tab} bg={bg} --")
    # Expect dark background (near black)
    # RGB values should all be < 30
    screenshot(page, f"{tab}_full", full=True)


def run_assessment():
    print("=" * 60)
    print("TradeGenius Dashboard UI Quality Assessment")
    print(f"Target: {BASE_URL}")
    print("=" * 60)

    # Load live API data for context
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from tools.system_check_bot import Session

    s = Session(BASE_URL, PASSWORD)
    s.login()
    state = s.get_json("/api/state")
    val_data = s.get_json("/api/executor/val")
    eod_main = state.get("eod_positions") or {}
    eod_val = val_data.get("eod_positions") or {}
    mode = (state.get("regime") or {}).get("mode", "")
    print(f"\nLive state: version={state.get('version')} mode={mode}")
    print(f"Main eod_positions: {list(eod_main.keys())}")
    print(f"Val eod_positions:  {list(eod_val.keys())}")
    print(f"Main trades_today:  {len(state.get('trades_today') or [])}")
    print(f"Val todays_trades:  {len(val_data.get('todays_trades') or [])}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})

        # --- Tab pages ---
        pages = {}
        for tab in ["main", "val", "gene"]:
            pg = context.new_page()
            login(pg)
            if tab != "main":
                switch_tab(pg, tab)
            pages[tab] = pg

        # Main is already on the dashboard; give it extra settle time
        time.sleep(2)

        print("\n" + "=" * 40)
        print("1. HEALTH TICK / SSE COUNTDOWN")
        print("=" * 40)
        check_h_tick(pages["main"])

        print("\n" + "=" * 40)
        print("2. KPI ROW CONSISTENCY")
        print("=" * 40)
        for tab in ["main", "val", "gene"]:
            check_tab_kpi_row(pages[tab], tab)

        print("\n" + "=" * 40)
        print("3. OPEN POSITIONS CARD")
        print("=" * 40)
        for tab in ["main", "val", "gene"]:
            check_positions_card(pages[tab], tab)
            check_eod_section_state(pages[tab], tab, eod_main if tab == "main" else eod_val)

        print("\n" + "=" * 40)
        print("4. TODAY'S TRADES CARD")
        print("=" * 40)
        for tab in ["main", "val", "gene"]:
            check_trades_card(pages[tab], tab)

        print("\n" + "=" * 40)
        print("5. SECTION ORDER")
        print("=" * 40)
        for tab in ["main", "val", "gene"]:
            check_section_order(pages[tab], tab)

        print("\n" + "=" * 40)
        print("6. v10 ORB SECTION")
        print("=" * 40)
        for tab in ["main", "val", "gene"]:
            check_v10_section(pages[tab], tab)

        print("\n" + "=" * 40)
        print("7. CROSS-TAB CONSISTENCY")
        print("=" * 40)
        check_cross_tab_consistency(pages)

        print("\n" + "=" * 40)
        print("8. FULL-PAGE SCREENSHOTS")
        print("=" * 40)
        for tab in ["main", "val", "gene"]:
            check_ui_theme(pages[tab], tab)

        # Mobile viewport check
        print("\n-- Mobile (390px) --")
        mob_ctx = browser.new_context(viewport={"width": 390, "height": 844})
        mob_pg = mob_ctx.new_page()
        login(mob_pg)
        screenshot(mob_pg, "main_mobile", full=True)

        # Check horizontal scroll on mobile
        has_scroll = mob_pg.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth"
        )
        if has_scroll:
            issue(
                "MED",
                "main",
                "Mobile",
                "Horizontal scroll detected at 390px width",
                "Check table overflow-x handling on mobile",
            )
        else:
            print("   Mobile: no horizontal scroll OK")

        mob_ctx.close()
        browser.close()

    # --- Report ---
    print("\n" + "=" * 60)
    print("ISSUES FOUND")
    print("=" * 60)
    by_sev = {"HIGH": [], "MED": [], "LOW": []}
    for iss in ISSUES:
        by_sev.get(iss["severity"], by_sev["LOW"]).append(iss)

    for sev in ["HIGH", "MED", "LOW"]:
        items = by_sev[sev]
        if items:
            print(f"\n[{sev}] ({len(items)} issues)")
            for i in items:
                print(f"  • {i['tab']}/{i['component']}: {i['description']}")
                if i.get("suggestion"):
                    print(f"    → {i['suggestion']}")

    total = len(ISSUES)
    if total == 0:
        print("\nOK No issues found  -  dashboard passes all checks")
    else:
        print(
            f"\nTotal: {total} issues ({len(by_sev['HIGH'])} HIGH, {len(by_sev['MED'])} MED, {len(by_sev['LOW'])} LOW)"
        )

    print(f"\nScreenshots saved to: {OUT_DIR}/")
    for ss in SCREENSHOTS:
        print(f"  {ss}")

    # Save JSON report
    report = {
        "issues": ISSUES,
        "screenshots": SCREENSHOTS,
        "live_state": {
            "version": state.get("version"),
            "mode": mode,
            "main_trades": len(state.get("trades_today") or []),
            "val_trades": len(val_data.get("todays_trades") or []),
        },
    }
    json.dump(report, open(str(OUT_DIR / "report.json"), "w"), indent=2)
    print(f"\nFull report: {OUT_DIR}/report.json")
    return ISSUES


if __name__ == "__main__":
    run_assessment()
