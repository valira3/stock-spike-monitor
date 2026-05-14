"""tools/system_check_bot.py -- unified system health check for TradeGenius.

Replaces unified_monitor.py + dashboard_analysis.py. Single login, single
session, all data fetched concurrently, three check sections:

  SYSTEM     -- login, HTML render, static assets, version, reachability
  INVARIANTS -- 18 production invariants (from dashboard_monitor_invariants)
  STRATEGY   -- 14 config/state checks against Keystone baseline
  TRADE_LOG  -- deep trade log audit: entry windows, risk sizing, cooldown
                violations, EOD exit reasons, version consistency, win rate

Also pulls Alpaca account snapshots and Railway forensic logs alongside
the dashboard check, matching unified_monitor's full data surface.

Usage:
    python tools/system_check_bot.py              # full check + pretty print
    python tools/system_check_bot.py --json       # machine-readable JSON
    python tools/system_check_bot.py --no-railway # skip Railway log pull
    python tools/system_check_bot.py --no-alpaca  # skip Alpaca pull
    python tools/system_check_bot.py --alert      # Telegram on CRIT/fail

Exit codes: 0 = all green, 1 = warnings, 2 = critical/invariant failures.

Env:
    DASHBOARD_BASE_URL      required
    DASHBOARD_PASSWORD      required
    TELEGRAM_TP_TOKEN       alert sink
    TELEGRAM_TP_CHAT_ID     alert sink
    VAL_ALPACA_PAPER_KEY / _SECRET    Alpaca (optional)
    RAILWAY_USE_CLI / RAILWAY_API_TOKEN  Railway logs (optional)
    MONITOR_DRY_RUN=1       skip Telegram side-effects
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Ensure repo root is importable regardless of how this script is invoked
# (python tools/system_check_bot.py adds tools/ to sys.path[0], not repo root).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
logger = logging.getLogger("system_check_bot")

# ---------------------------------------------------------------------------
# Keystone baseline constants (mirrors tools/dashboard_analysis.py)
# ---------------------------------------------------------------------------

KEYSTONE: dict[str, Any] = {
    "or_minutes": 30,
    "rr": 2.5,
    "risk_per_trade_pct": 1.0,
    "max_trades_per_day": 5,
    "max_concurrent_risk_dollars": 2000.0,
    "daily_loss_kill_pct": 2.0,
    "skip_vix_above": 22.0,
    "skip_gap_above_pct": 1.5,
    "atr_stop_mult": 1.75,
    "partial_profit_at_1r": True,
    "max_vwap_dev_bps": 25.0,
    "entry_open_min": 570,  # 09:30 ET
    "entry_close_min": 660,  # 11:00 ET
    "eod_entry_min": 900,  # 15:00 ET
    "eod_exit_min": 959,  # 15:59 ET
    "account": 100_000.0,
}

# Expected DOM IDs that must be present in the rendered HTML
REQUIRED_HTML_IDS = [
    "v10-day-status",
    "v10-eod-section",
    "v10-proximity-section",
    "v10-activity-section",
    "tg-tabs",
    "tg-panel-main",
    "tg-panel-val",
    "pos-body",
    "trades-body",
    "idx-strip",
    "tg-brand-row",
]

# ---------------------------------------------------------------------------
# Check result model
# ---------------------------------------------------------------------------

OK = "OK"
WARN = "WARN"
CRIT = "CRIT"
INFO = "INFO"


class Check:
    __slots__ = ("section", "name", "status", "detail")

    def __init__(self, section: str, name: str, status: str, detail: str):
        self.section = section
        self.name = name
        self.status = status
        self.detail = detail

    def to_dict(self) -> dict:
        return {
            "section": self.section,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

SESSION_COOKIE = "spike_session"
UA = "tg-system-check-bot/9.1.36"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # type: ignore[override]
        return None


class Session:
    """Single authenticated session. Login once, reuse cookie for all GETs."""

    def __init__(self, base: str, password: str, timeout: float = 15.0):
        self.base = base.rstrip("/")
        self._password = password
        self._timeout = timeout
        self._cookie: str | None = None
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar),
        )

    def login(self) -> None:
        data = urllib.parse.urlencode({"password": self._password}).encode()
        req = urllib.request.Request(
            self.base + "/login",
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": UA,
                "Origin": self.base,
                "Referer": self.base + "/",
            },
        )
        no_redir = urllib.request.build_opener(_NoRedirect)
        try:
            no_redir.open(req, timeout=self._timeout)
            raise RuntimeError("/login returned 200, expected 302")
        except urllib.error.HTTPError as e:
            if e.code != 302:
                raise RuntimeError(f"/login HTTP {e.code}")
            for c in e.headers.get_all("Set-Cookie") or []:
                if c.startswith(SESSION_COOKIE + "="):
                    self._cookie = c.split(";", 1)[0].split("=", 1)[1]
                    return
            raise RuntimeError("/login 302 but no spike_session cookie")

    def _req(self, path: str) -> urllib.request.Request:
        if not self._cookie:
            raise RuntimeError("Not logged in")
        return urllib.request.Request(
            self.base + path,
            headers={"Cookie": f"{SESSION_COOKIE}={self._cookie}", "User-Agent": UA},
        )

    def get_json(self, path: str) -> dict:
        try:
            with urllib.request.urlopen(self._req(path), timeout=self._timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            raise RuntimeError(f"GET {path}: {e}")

    def get_html(self, path: str = "/") -> tuple[int, str]:
        """Return (status_code, html_text). Never raises."""
        try:
            with urllib.request.urlopen(self._req(path), timeout=self._timeout) as r:
                return r.status, r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, ""
        except Exception:
            return 0, ""

    def get_status(self, path: str) -> int:
        """Return HTTP status for a path without reading the body."""
        try:
            with urllib.request.urlopen(self._req(path), timeout=self._timeout) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            return 0


# ---------------------------------------------------------------------------
# Data fetch (concurrent)
# ---------------------------------------------------------------------------

DASHBOARD_PATHS = [
    "/api/state",
    "/api/executor/val",
    "/api/executor/gene",
    "/api/indices",
    "/api/trade_log?limit=5000",
]


def fetch_all(session: Session) -> dict[str, Any]:
    """Fetch all API endpoints + HTML concurrently. Returns raw payloads."""
    results: dict[str, Any] = {}
    lock = threading.Lock()

    def _get_json(path: str) -> None:
        try:
            data = session.get_json(path)
        except Exception as e:
            data = {"__error__": str(e)}
        with lock:
            results[path] = data

    def _get_html() -> None:
        code, html = session.get_html("/")
        js_code = session.get_status("/static/app.js")
        css_code = session.get_status("/static/app.css")
        with lock:
            results["__ui__"] = {
                "status": code,
                "html": html,
                "js_status": js_code,
                "css_status": css_code,
            }

    threads = [threading.Thread(target=_get_html)]
    for p in DASHBOARD_PATHS:
        threads.append(threading.Thread(target=_get_json, args=(p,)))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    return results


# ---------------------------------------------------------------------------
# SECTION 1 \u2014 SYSTEM checks (web UI + static assets + reachability)
# ---------------------------------------------------------------------------


def checks_system(raw: dict[str, Any], expected_version: str | None) -> list[Check]:
    s = "SYSTEM"
    checks = []
    ui = raw.get("__ui__") or {}

    # --- web UI ---
    ui_status = ui.get("status", 0)
    if ui_status == 200:
        checks.append(Check(s, "ui.load", OK, "dashboard HTML 200 OK"))
    else:
        checks.append(
            Check(s, "ui.load", CRIT, f"dashboard root returned HTTP {ui_status} (not 200)")
        )

    html = ui.get("html", "")
    if html:
        # Title check
        if "<title>TradeGenius" in html:
            checks.append(Check(s, "ui.title", OK, "title tag correct"))
        else:
            checks.append(Check(s, "ui.title", WARN, "title tag missing or changed"))

        # Server-side error leak
        if "Internal Server Error" in html or "Traceback (most recent call" in html:
            checks.append(
                Check(
                    s,
                    "ui.server_error",
                    CRIT,
                    "Python traceback visible in dashboard HTML response",
                )
            )
        else:
            checks.append(Check(s, "ui.server_error", OK, "no server-error leak in HTML"))

        # Required DOM IDs
        missing = [id_ for id_ in REQUIRED_HTML_IDS if f'id="{id_}"' not in html]
        if missing:
            checks.append(Check(s, "ui.dom_ids", WARN, f"missing DOM IDs: {', '.join(missing)}"))
        else:
            checks.append(
                Check(s, "ui.dom_ids", OK, f"all {len(REQUIRED_HTML_IDS)} required IDs present")
            )

        # Version in brand row \u2014 the static HTML sets it via JS; check the
        # span exists and is not hardcoded to a stale value
        if 'id="tg-brand-ver"' in html:
            checks.append(Check(s, "ui.brand_ver_span", OK, "brand version span present"))
        else:
            checks.append(
                Check(
                    s,
                    "ui.brand_ver_span",
                    WARN,
                    "brand version span (tg-brand-ver) missing from HTML",
                )
            )

        # Check EOD section is present and correct class (visible when enabled)
        if 'id="v10-eod-section"' in html:
            checks.append(Check(s, "ui.eod_section", OK, "EOD section ID in HTML"))
        else:
            checks.append(
                Check(s, "ui.eod_section", WARN, "EOD section (v10-eod-section) missing from HTML")
            )

        # PAPER chip vs emoji (v9.1.34 replaced emoji)
        if "\U0001f4c4" in html:  # 📄
            checks.append(
                Check(
                    s,
                    "ui.paper_badge",
                    WARN,
                    "old emoji paper badge still in HTML (should be PAPER chip)",
                )
            )
        else:
            checks.append(Check(s, "ui.paper_badge", OK, "PAPER chip (no emoji)"))

    # --- static assets ---
    js_code = ui.get("js_status", 0)
    css_code = ui.get("css_status", 0)
    checks.append(
        Check(s, "assets.app_js", OK if js_code == 200 else CRIT, f"/static/app.js HTTP {js_code}")
    )
    checks.append(
        Check(
            s, "assets.app_css", OK if css_code == 200 else CRIT, f"/static/app.css HTTP {css_code}"
        )
    )

    # --- API reachability ---
    state = raw.get("/api/state") or {}
    if state.get("ok"):
        ver = state.get("version", "?")
        checks.append(Check(s, "api.state", OK, f"/api/state ok v{ver}"))
        if expected_version and ver != expected_version:
            checks.append(
                Check(s, "api.version", WARN, f"live=v{ver} expected=v{expected_version}")
            )
        else:
            checks.append(Check(s, "api.version", OK, f"v{ver}"))
    elif "__error__" in state:
        checks.append(
            Check(s, "api.state", CRIT, f"/api/state unreachable: {state['__error__'][:120]}")
        )
    else:
        checks.append(Check(s, "api.state", CRIT, "/api/state returned ok=false"))

    for path in ("/api/executor/val", "/api/executor/gene", "/api/indices"):
        payload = raw.get(path) or {}
        err = payload.get("__error__")
        checks.append(
            Check(
                s,
                f"api.{path.split('/')[2][:8]}",
                WARN if err else OK,
                err[:80] if err else f"{path} OK",
            )
        )

    # --- server_time_label encoding ---
    label = state.get("server_time_label", "")
    if label and "�" in label:
        checks.append(
            Check(
                s,
                "api.time_label",
                WARN,
                f"server_time_label contains U+FFFD: {label!r} (encoding bug)",
            )
        )
    elif label:
        checks.append(Check(s, "api.time_label", OK, label))

    return checks


# ---------------------------------------------------------------------------
# SECTION 2 \u2014 INVARIANTS (production invariant battery)
# ---------------------------------------------------------------------------


def checks_invariants(raw: dict[str, Any], base_url: str) -> list[Check]:
    s = "INVARIANTS"
    try:
        from tools.dashboard_monitor_invariants import INVARIANTS, InvariantContext
    except Exception as e:
        return [Check(s, "import", CRIT, f"dashboard_monitor_invariants import failed: {e}")]

    payloads: dict[str, Any] = {
        "state": raw.get("/api/state") or {},
        "exec_val": raw.get("/api/executor/val") or {},
        "exec_gene": raw.get("/api/executor/gene") or {},
        "trade_log": raw.get("/api/trade_log?limit=5000") or {},
    }
    ctx = InvariantContext(payloads=payloads, base_url=base_url)
    out = []
    for fn in INVARIANTS:
        try:
            r = fn(ctx)
        except Exception as e:
            r = {
                "name": getattr(fn, "__name__", "?"),
                "ok": False,
                "summary": f"raised {type(e).__name__}: {str(e)[:120]}",
            }
        status = OK if r.get("ok", False) else CRIT
        out.append(
            Check(s, r.get("name", "?"), status, r.get("summary", "") or r.get("detail", ""))
        )
    return out


# ---------------------------------------------------------------------------
# SECTION 3 \u2014 STRATEGY (config + live state vs Keystone)
# ---------------------------------------------------------------------------


def checks_strategy(raw: dict[str, Any]) -> list[Check]:
    s = "STRATEGY"
    state = raw.get("/api/state") or {}
    val = raw.get("/api/executor/val") or {}
    idx = raw.get("/api/indices") or {}
    now_et = datetime.now(ET)
    now_min = now_et.hour * 60 + now_et.minute
    is_rth = 9 <= now_et.hour < 16 and now_et.weekday() < 5
    # Session reset fires on the first scan tick after 09:30 ET (confirmed
    # empirically: fires at ~09:30:32 ET). Use 09:33 as the threshold so
    # the monitor doesn't false-positive during the first few scan cycles.
    is_post_open = now_min >= 573 and now_et.weekday() < 5  # 573 = 09:33 ET
    out: list[Check] = []

    # -- session --
    v10 = state.get("v10") or {}
    ds = v10.get("day_status") or {}
    bootstrapped = v10.get("bootstrapped", False)
    live_mode = v10.get("live_mode", False)
    session_date = v10.get("session_date", "")
    if not bootstrapped:
        out.append(Check(s, "session", WARN, "ORB engine not bootstrapped"))
    elif not live_mode:
        out.append(
            Check(s, "session", CRIT, "ORB_LIVE_MODE=0 -- LEGACY fallback active, v10 not trading")
        )
    elif is_post_open and not session_date:
        # Two patterns where session_date is legitimately empty after 09:33 ET:
        #
        # A) Post-deploy startup (< ~60s after process restart): activity buffer
        #    is empty (new process) AND day_states=0, but ingest has been running
        #    all day (bars_today > 100). Session reset will fire on next scan tick.
        #
        # B) Mid-tick race already seen today: activity log has a session_start
        #    event but session_date hasn't populated yet in this snapshot.
        #
        # Genuine failure: session_date empty, no activity events, AND day_states=0
        # for >5 min with bars flowing -- but we can't distinguish that from
        # case A on the first post-deploy check. Accept one transient OK on
        # restart; if the next 5-min tick still shows empty, it WILL fire.
        activity = v10.get("activity") or []
        day_states = v10.get("day_states") or []
        ingest = state.get("ingest_status") or {}
        bars_today = int(ingest.get("bars_today") or 0)
        today_et = datetime.now(ET).date().isoformat()
        had_session_start = any(
            e.get("kind") == "session_start" and (e.get("ts_iso") or "")[:10] >= today_et
            for e in activity
        )
        is_post_deploy_startup = not day_states and bars_today > 100
        if had_session_start or is_post_deploy_startup:
            reason = (
                "session_start in activity"
                if had_session_start
                else f"post-deploy startup (bars_today={bars_today}, session reset pending)"
            )
            out.append(Check(s, "session", OK, f"session_date transiently empty -- {reason}"))
        else:
            out.append(
                Check(
                    s,
                    "session",
                    WARN,
                    "session_date empty after 09:33 ET, no session_start events, "
                    f"day_states=0, bars_today={bars_today} -- session reset may have failed",
                )
            )
    else:
        out.append(Check(s, "session", OK, f"live_mode=ON session={session_date or '(off-hours)'}"))

    # -- config vs Keystone --
    cfg = v10.get("config") or {}
    drift = []
    for field, expected in KEYSTONE.items():
        if field.endswith("_min") or field == "account" or field not in cfg:
            continue
        live = cfg[field]
        if isinstance(expected, bool):
            ok = bool(live) == expected
        elif isinstance(expected, float):
            ok = abs(float(live) - expected) < 1e-6
        else:
            ok = live == expected
        if not ok:
            drift.append(f"{field}: live={live!r} expected={expected!r}")
    if drift:
        for d in drift:
            out.append(Check(s, f"config.{d.split(':')[0]}", WARN, d))
    else:
        out.append(Check(s, "config", OK, f"{len(cfg)} fields match Keystone"))

    # -- VIX --
    thr = float(ds.get("vix_threshold") or cfg.get("skip_vix_above") or 22.0)
    vix = ds.get("vix_d1_close")
    vix_src = "prior_day"
    if vix is None:
        vix = ds.get("vix_current")
        vix_src = "current"
    if vix is None:
        for row in idx.get("indices") or []:
            if row.get("symbol") == "^VIX" and row.get("available") and row.get("last"):
                vix = row["last"]
                vix_src = "yahoo_live"
                break
    if vix is None:
        out.append(Check(s, "vix", WARN, "VIX unavailable from all sources"))
    else:
        vix = float(vix)
        if vix > thr:
            out.append(
                Check(
                    s,
                    "vix",
                    CRIT,
                    f"VIX {vix:.2f} > {thr:.0f} ({vix_src}) \u2014 block_day expected",
                )
            )
        else:
            out.append(
                Check(s, "vix", OK, f"VIX {vix:.2f}/{thr:.0f} ({vix_src}) \u2014 gate clear")
            )

    # -- EOD --
    eod = v10.get("eod") or {}
    eod_cfg = eod.get("config") or {}
    enabled = eod.get("enabled", False)
    fire = eod_cfg.get("fire_broker", False)
    entry_et = eod_cfg.get("entry_et", "?")
    out.append(
        Check(
            s,
            "eod.enabled",
            OK if enabled else WARN,
            "armed" if enabled else "disabled (ORB_EOD_REVERSAL_ENABLED=0)",
        )
    )
    out.append(
        Check(
            s,
            "eod.fire_mode",
            INFO if fire else WARN,
            "LIVE ORDERS" if fire else "paper-observe (ORB_EOD_FIRE_BROKER=0)",
        )
    )
    if entry_et != "15:00":
        out.append(
            Check(
                s,
                "eod.entry_window",
                WARN,
                f"entry_et={entry_et!r} expected '15:00' (prod default since v9.1.2)",
            )
        )
    else:
        out.append(
            Check(s, "eod.window", OK, f"window {entry_et}–{eod_cfg.get('exit_et', '?')} ET")
        )
    long_t = set(eod_cfg.get("long_tickers") or [])
    short_t = set(eod_cfg.get("short_tickers") or [])
    exp_l = {"ORCL", "AAPL", "MSFT", "AVGO"}
    exp_s = {"ORCL", "NFLX", "AAPL", "MSFT"}
    if long_t == exp_l and short_t == exp_s:
        out.append(
            Check(
                s,
                "eod.fence",
                OK,
                "r17 fence correct (long ORCL/AAPL/MSFT/AVGO, short ORCL/NFLX/AAPL/MSFT)",
            )
        )
    else:
        out.append(Check(s, "eod.fence", WARN, f"long={sorted(long_t)} short={sorted(short_t)}"))

    # -- risk books --
    for pid, rb in (v10.get("risk_books") or {}).items():
        eq = rb.get("equity", 0)
        util = rb.get("utilization_pct", 0)
        pnl = rb.get("realized_pnl_today", 0)
        kill = rb.get("daily_kill_triggered", False)
        if kill:
            out.append(Check(s, f"risk.{pid}", CRIT, f"daily kill triggered pnl=${pnl:.0f}"))
        elif util > 90:
            out.append(
                Check(
                    s,
                    f"risk.{pid}",
                    WARN,
                    f"util {util:.0f}% open_risk=${rb.get('open_risk', 0):.0f}",
                )
            )
        else:
            out.append(
                Check(s, f"risk.{pid}", OK, f"eq=${eq:,.0f} pnl=${pnl:+.0f} util={util:.0f}%")
            )

    # -- executors --
    execs = state.get("executors_status") or {}
    val_en = (execs.get("val") or {}).get("enabled", False)
    out.append(
        Check(s, "executor.val", OK if val_en else WARN, "enabled paper" if val_en else "disabled")
    )
    gene_en = (execs.get("gene") or {}).get("enabled", False)
    out.append(
        Check(
            s,
            "executor.gene",
            INFO if not gene_en else OK,
            "disabled (ALPACA_SKIP_PORTFOLIOS=gene)" if not gene_en else "enabled",
        )
    )
    if val_en and val:
        acc = val.get("account") or {}
        eq = acc.get("equity", 0)
        dpnl = acc.get("day_pnl", 0)
        err = val.get("error")
        if err:
            out.append(Check(s, "executor.val.health", CRIT, f"error: {err}"))
        else:
            out.append(
                Check(s, "executor.val.equity", OK, f"${eq:,.2f} day_pnl=${dpnl:+.2f} (Alpaca)")
            )

    # -- cooldowns --
    by_pid = state.get("active_cooldowns_by_portfolio") or {}
    total = sum(len(v) for v in by_pid.values())
    if total == 0:
        out.append(Check(s, "cooldowns", OK, "0 active cooldowns"))
    else:
        items = []
        for pid, cds in by_pid.items():
            for c in cds:
                rem = int((c.get("remaining_sec") or 0) / 60)
                items.append(f"{pid}:{c.get('ticker')}({c.get('side')}) {rem}min")
        out.append(Check(s, "cooldowns", INFO, f"{total} active: " + ", ".join(items)))

    # -- ingest --
    ingest = state.get("ingest_status") or {}
    ws = ingest.get("ws_state", "?")
    gaps = ingest.get("open_gaps_today", 0)
    bars = ingest.get("bars_today", 0)
    is_rth_ingest = (ingest.get("ingest_health") or {}).get("is_rth", False)
    if not is_rth_ingest:
        out.append(Check(s, "ingest", OK, f"off-hours (ws={ws} bars={bars})"))
    elif gaps > 0:
        out.append(Check(s, "ingest", WARN, f"ws={ws} open_gaps={gaps} bars={bars}"))
    elif ws not in ("CONNECTED", "STREAMING", "LIVE", "OK"):
        out.append(Check(s, "ingest", WARN, f"ws={ws} status={ingest.get('status', '?')}"))
    else:
        out.append(Check(s, "ingest", OK, f"ws={ws} bars={bars} gaps={gaps}"))

    # -- RTH trade correlation --
    if is_rth:
        trades = state.get("trades_today") or []
        entries = [t for t in trades if t.get("action") in ("BUY", "SHORT", "SELL_SHORT")]
        closes = [t for t in trades if t.get("action") in ("SELL", "COVER", "BUY_TO_COVER")]
        pnl_sum = sum(float(t.get("pnl") or 0) for t in closes)
        wins = sum(1 for t in closes if float(t.get("pnl") or 0) > 0)
        out_win = []
        for t in entries:
            raw_t = t.get("time") or t.get("entry_time") or ""
            try:
                parts = str(raw_t).replace(" ET", "").split(":")
                em = int(parts[0]) * 60 + int(parts[1])
                in_orb = KEYSTONE["entry_open_min"] <= em <= KEYSTONE["entry_close_min"]
                in_eod = KEYSTONE["eod_entry_min"] <= em <= KEYSTONE["eod_exit_min"]
                if not in_orb and not in_eod:
                    out_win.append(f"{t.get('ticker')}@{raw_t}")
            except (ValueError, TypeError, IndexError):
                pass
        out.append(
            Check(
                s,
                "trades.summary",
                OK,
                f"{len(entries)} entries, {len(closes)} closes "
                f"W{wins}/L{len(closes) - wins} P&L=${pnl_sum:+.2f}",
            )
        )
        if out_win:
            out.append(
                Check(
                    s,
                    "trades.window",
                    WARN,
                    f"entries outside ORB/EOD window: {', '.join(out_win)}",
                )
            )

    return out


# ---------------------------------------------------------------------------
# Side-channel pulls (Alpaca + Railway \u2014 run concurrently with dashboard)
# ---------------------------------------------------------------------------


def pull_alpaca() -> dict[str, Any]:
    try:
        from tools.alpaca_snapshot import PORTFOLIOS, _pull_portfolio
    except Exception as e:
        return {"__error__": str(e)}
    skip = {
        p.strip().lower()
        for p in os.environ.get("ALPACA_SKIP_PORTFOLIOS", "").split(",")
        if p.strip()
    }
    out: dict[str, Any] = {}
    for pid in PORTFOLIOS:
        if pid in skip:
            continue
        out[pid] = _pull_portfolio(pid)
    return {"portfolios": out}


def pull_railway() -> dict[str, Any]:
    try:
        from tools.railway_log_tail import fetch_recent_logs, grep_logs, probe_railway_access
    except Exception as e:
        return {"__error__": str(e)}
    probe = probe_railway_access()
    if probe.get("status") != "ok":
        return {"__probe_fail__": probe}
    PATTERN = (
        r"\[V9\d{2}-|\[V79-ORB-|\[V10-FIRE\]|\[V834-|\[V8\d{2}-|"
        r"ENTRY|EXIT|TRADE_CLOSED|Traceback|ERROR|daily_kill|killswitch"
    )
    try:
        rows = fetch_recent_logs(limit=2000) or []
        forensic = grep_logs(PATTERN, limit=2000) or []
    except Exception as e:
        return {"__error__": str(e)}
    # per-tag sampling (mirrors unified_monitor)
    BUCKETS = (
        "[V910-",
        "[V900-",
        "[V917-",
        "[V79-ORB-",
        "[V10-FIRE]",
        "[V834-",
        "[V83-",
        "[V8",
        "[V9",
        "Traceback",
        "ERROR",
    )
    buckets: dict[str, list] = {}
    summary: dict[str, int] = {}
    for r in forensic:
        msg = str(r.get("message", "") or r.get("text", ""))
        tag = next((b for b in BUCKETS if b in msg), "_other")
        summary[tag] = summary.get(tag, 0) + 1
        buckets.setdefault(tag, []).append(r)
    sampled: list = []
    for tag in list(BUCKETS) + ["_other"]:
        sampled.extend(buckets.get(tag, [])[-20:])
    return {
        "total_fetched": len(rows),
        "forensic_total": len(forensic),
        "forensic_matches": sampled[-500:],
        "filter_summary": summary,
        "probe": probe,
    }


# ---------------------------------------------------------------------------
# Report + alert
# ---------------------------------------------------------------------------


def build_report(
    checks: list[Check],
    raw: dict[str, Any],
    alpaca: dict[str, Any],
    railway: dict[str, Any],
    elapsed: float,
) -> dict[str, Any]:
    crits = [c for c in checks if c.status == CRIT]
    warns = [c for c in checks if c.status == WARN]
    overall = OK if not crits and not warns else (WARN if not crits else CRIT)
    now = datetime.now(timezone.utc)
    return {
        "schema_version": 2,
        "tool": "system_check_bot",
        "captured_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts_et": datetime.now(ET).strftime("%Y-%m-%dT%H:%M:%S ET"),
        "version": (raw.get("/api/state") or {}).get("version", "?"),
        "overall": overall,
        "elapsed_s": round(elapsed, 2),
        "summary": {
            "total": len(checks),
            "ok": sum(1 for c in checks if c.status == OK),
            "warn": len(warns),
            "crit": len(crits),
            "info": sum(1 for c in checks if c.status == INFO),
        },
        "checks": [c.to_dict() for c in checks],
        "crits": [c.name for c in crits],
        "warns": [c.name for c in warns],
        # raw data for forensic replay
        "dashboard": {p: raw.get(p) for p in DASHBOARD_PATHS},
        "alpaca": alpaca,
        "railway_logs": railway,
    }


def print_report(report: dict[str, Any]) -> None:
    overall = report["overall"]
    icon = {"OK": "[OK]", "WARN": "[WARN]", "CRIT": "[CRIT]"}.get(overall, "[?]")
    print(f"\n=== System Check Bot {icon} ===  {report['ts_et']}  v{report['version']}")
    status_icon = {OK: "+", WARN: "!", CRIT: "X", INFO: "."}
    prev_section = None
    for c in report["checks"]:
        if c["section"] != prev_section:
            print(f"\n  -- {c['section']} --")
            prev_section = c["section"]
        icon_ = status_icon.get(c["status"], "?")
        print(f"  {icon_}  [{c['status']:<4}]  {c['name']:<38}  {c['detail']}")
    sm = report["summary"]
    print(
        f"\n  {sm['ok']} ok  {sm['warn']} warn  {sm['crit']} crit  "
        f"{sm['info']} info  ({report['elapsed_s']:.1f}s)\n"
    )


# ---------------------------------------------------------------------------
# SECTION 4 \u2014 TRADE LOG (deep comparison against strategy expectations)
# ---------------------------------------------------------------------------

# Tolerance band around expected risk per trade (1% of $100k = $1,000).
# ATR-based stops vary by volatility so we allow 40-200% of nominal.
_RISK_LOW_USD = 100.0  # below this = stop too tight or tiny position; 100 = 0.1% of $100k floor
_RISK_HIGH_USD = 2500.0  # above this = stop too loose or oversized

# Cooldown window: same (ticker, side) re-entry within this many minutes
# of a stop exit is a cooldown violation (should be blocked by the engine).
_COOLDOWN_MIN = 30


def _parse_entry_min(entry_time: str) -> int | None:
    """Parse 'HH:MM:SS' entry_time into minutes-past-midnight. Returns None on failure."""
    try:
        parts = str(entry_time).split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError, TypeError):
        return None


def _parse_exit_dt_et(exit_time: str) -> datetime | None:
    """Parse ISO UTC exit_time and convert to ET datetime. Returns None on failure."""
    try:
        dt = datetime.fromisoformat(exit_time.replace("Z", "+00:00"))
        return dt.astimezone(ET)
    except Exception:
        return None


def checks_trade_log(raw: dict[str, Any]) -> list[Check]:
    """Deep trade log analysis: compare today's + recent closed trades
    against v10 ORB + r17 EOD strategy expectations.

    Checks (per-trade and aggregate):
      1. Entry time inside ORB (09:30-11:00 ET) or EOD (15:00-15:59 ET) window
      2. Implied risk per trade within $200-$2,500 (ATR-based stop range)
      3. No same-(ticker, side) re-entry within 30 min of a stop exit
         (cooldown violation -- engine should have blocked this)
      4. No orphaned stop exits with entry_stop=0 (data quality)
      5. Version consistency -- all today's trades on current bot_version
      6. EOD exit reason is 'EOD' for 15:xx ET trades
      7. Aggregate: trade count, realized P&L, win rate vs expectations
    """
    s = "TRADE_LOG"
    checks: list[Check] = []

    payload = raw.get("/api/trade_log?limit=5000") or {}
    rows = payload.get("rows") or []
    state = raw.get("/api/state") or {}
    live_version = state.get("version", "")

    # Filter to today ET
    today_et = datetime.now(ET).date().isoformat()
    today = [r for r in rows if r.get("date") == today_et]

    if not rows and not payload.get("ok", True):
        checks.append(Check(s, "fetch", WARN, "/api/trade_log fetch failed or empty"))
        return checks

    if not today:
        # No trades today is normal before 09:30 or on a no-signal day
        now_min = datetime.now(ET).hour * 60 + datetime.now(ET).minute
        if now_min > 660:  # after 11:00 ET \u2014 would expect some activity or at least a session
            checks.append(
                Check(
                    s,
                    "today",
                    INFO,
                    "0 closed trades today (after 11:00 ET) -- normal if no signals fired",
                )
            )
        else:
            checks.append(
                Check(s, "today", INFO, "0 closed trades today -- pre-window or no signals yet")
            )
        return checks

    # ------------------------------------------------------------------ #
    # 1. Entry time window                                                #
    # ------------------------------------------------------------------ #
    outside_window: list[str] = []
    for t in today:
        em = _parse_entry_min(t.get("entry_time", ""))
        if em is None:
            continue
        in_orb = KEYSTONE["entry_open_min"] <= em <= KEYSTONE["entry_close_min"]
        in_eod = KEYSTONE["eod_entry_min"] <= em <= KEYSTONE["eod_exit_min"]
        if not in_orb and not in_eod:
            outside_window.append(f"{t.get('ticker')} {t.get('side')} @{t.get('entry_time')}")
    if outside_window:
        checks.append(
            Check(
                s,
                "entry_window",
                WARN,
                f"{len(outside_window)} trade(s) entered outside ORB/EOD window: "
                + ", ".join(outside_window),
            )
        )
    else:
        checks.append(
            Check(s, "entry_window", OK, f"all {len(today)} trade(s) in valid entry window")
        )

    # ------------------------------------------------------------------ #
    # 2. Risk per trade sizing                                            #
    # ------------------------------------------------------------------ #
    risk_issues: list[str] = []
    for t in today:
        ep = float(t.get("entry_price") or 0)
        stop = float(t.get("entry_stop") or 0)
        sh = float(t.get("shares") or 0)
        if not (ep and stop and sh):
            continue
        risk = abs(ep - stop) * sh
        if risk < _RISK_LOW_USD:
            risk_issues.append(f"{t.get('ticker')} ${risk:.0f} (too tight)")
        elif risk > _RISK_HIGH_USD:
            risk_issues.append(f"{t.get('ticker')} ${risk:.0f} (oversized)")
    if risk_issues:
        checks.append(
            Check(
                s,
                "risk_sizing",
                WARN,
                f"{len(risk_issues)} trade(s) outside ${_RISK_LOW_USD:.0f}-${_RISK_HIGH_USD:.0f} risk band: "
                + ", ".join(risk_issues),
            )
        )
    else:
        checks.append(Check(s, "risk_sizing", OK, f"all {len(today)} trade(s) within risk band"))

    # ------------------------------------------------------------------ #
    # 3. Cooldown violation: same (ticker, side) re-entry within 30 min  #
    #    of a stop exit                                                   #
    # ------------------------------------------------------------------ #
    STOP_REASONS = {
        "V10_STOP",
        "sentinel_a_stop_price",
        "sentinel_r2_hard_stop",
        "sentinel_v651_deep_stop",
        "v750_early_ditch",
        "be_stop",
    }
    # Build timeline of stop exits keyed by (ticker, side)
    stop_exits: dict[tuple, list[datetime]] = {}
    for t in today:
        reason = (t.get("reason") or "").upper()
        if any(s_ in reason for s_ in ("STOP", "DITCH", "V750")):
            exit_dt = _parse_exit_dt_et(t.get("exit_time", ""))
            if exit_dt:
                key = (t.get("ticker"), t.get("side"))
                stop_exits.setdefault(key, []).append(exit_dt)

    cooldown_violations: list[str] = []
    for t in today:
        em = _parse_entry_min(t.get("entry_time", ""))
        if em is None:
            continue
        key = (t.get("ticker"), t.get("side"))
        for stop_dt in stop_exits.get(key, []):
            # Convert entry time to a comparable ET datetime
            entry_dt = datetime.now(ET).replace(
                hour=em // 60, minute=em % 60, second=0, microsecond=0
            )
            diff_min = (entry_dt - stop_dt).total_seconds() / 60
            if 0 < diff_min < _COOLDOWN_MIN:
                cooldown_violations.append(
                    f"{t.get('ticker')} {t.get('side')} re-entered {diff_min:.0f}min after stop"
                )
    if cooldown_violations:
        checks.append(
            Check(
                s,
                "cooldown",
                CRIT,
                f"{len(cooldown_violations)} cooldown violation(s): "
                + "; ".join(cooldown_violations),
            )
        )
    else:
        checks.append(Check(s, "cooldown", OK, "no cooldown violations"))

    # ------------------------------------------------------------------ #
    # 4. Data quality: entry_stop=0 on stop exits                        #
    # ------------------------------------------------------------------ #
    bad_stop_data = [
        f"{t.get('ticker')} {t.get('reason')}"
        for t in today
        if (
            float(t.get("entry_stop") or 0) == 0
            and any(s_ in (t.get("reason") or "").upper() for s_ in ("STOP", "DITCH"))
        )
    ]
    if bad_stop_data:
        checks.append(
            Check(
                s, "data_quality", WARN, f"entry_stop=0 on stop exit(s): {', '.join(bad_stop_data)}"
            )
        )

    # ------------------------------------------------------------------ #
    # 5. Bot version on today's trades matches live version               #
    # ------------------------------------------------------------------ #
    if live_version:
        old_version_trades = [
            f"{t.get('ticker')}@{t.get('bot_version')}"
            for t in today
            if t.get("bot_version") and t.get("bot_version") != live_version
        ]
        if old_version_trades:
            checks.append(
                Check(
                    s,
                    "version_consistency",
                    INFO,
                    f"trades from prior version: {', '.join(old_version_trades[:5])}",
                )
            )

    # ------------------------------------------------------------------ #
    # 6. EOD exit reason                                                  #
    # ------------------------------------------------------------------ #
    eod_trades = [
        t
        for t in today
        if (_parse_entry_min(t.get("entry_time", "")) or 0) >= KEYSTONE["eod_entry_min"]
    ]
    eod_wrong_reason = [
        f"{t.get('ticker')} reason={t.get('reason')}"
        for t in eod_trades
        if (t.get("reason") or "").upper() not in ("EOD", "EOD_EXIT", "EOD_CLOSE")
    ]
    if eod_wrong_reason:
        checks.append(
            Check(
                s,
                "eod_reason",
                WARN,
                f"EOD trade(s) with unexpected exit reason: {', '.join(eod_wrong_reason)}",
            )
        )
    elif eod_trades:
        checks.append(
            Check(s, "eod_reason", OK, f"{len(eod_trades)} EOD trade(s) all exited correctly")
        )

    # ------------------------------------------------------------------ #
    # 7. Aggregate: count, P&L, win rate                                 #
    # ------------------------------------------------------------------ #
    n = len(today)
    wins = sum(1 for t in today if float(t.get("pnl") or 0) > 0)
    total_pnl = sum(float(t.get("pnl") or 0) for t in today)
    wr = (wins / n * 100) if n else 0

    status = OK
    detail = f"{n} trades W{wins}/L{n - wins} P&L=${total_pnl:+.2f} WR={wr:.0f}%"

    # Cap breach (ORB_PORTFOLIO_FIRE=1 means each pid counts separately,
    # so main alone should not exceed 5 trades).
    main_trades = [t for t in today if t.get("portfolio") in ("paper", "main", None)]
    if len(main_trades) > KEYSTONE["max_trades_per_day"]:
        status = WARN
        detail += f" -- main count {len(main_trades)} > cap {KEYSTONE['max_trades_per_day']}"

    if n >= 3 and wr < 20:
        status = WARN
        detail += " -- win rate below 20%, review signals"
    elif n >= 3 and wr > 90:
        status = INFO
        detail += " -- win rate >90%, verify data integrity"

    checks.append(Check(s, "aggregate", status, detail))

    return checks


# ---------------------------------------------------------------------------
# SECTION 5 \u2014 MARKET VALIDATION (Alpaca SIP bars + fill cross-check)
# ---------------------------------------------------------------------------


def _compute_atr(bars: list[dict[str, Any]], n: int = 14) -> float | None:
    """Wilder's ATR(n) from a list of {o,h,l,c} bar dicts, oldest first."""
    if len(bars) < n + 1:
        return None
    trs = [
        max(
            bars[i]["h"] - bars[i]["l"],
            abs(bars[i]["h"] - bars[i - 1]["c"]),
            abs(bars[i]["l"] - bars[i - 1]["c"]),
        )
        for i in range(1, len(bars))
    ]
    atr = sum(trs[:n]) / n
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n
    return atr


def pull_alpaca_market_data(tickers: list[str], date_et: str) -> dict[str, Any]:
    """Fetch 5m SIP bars for *tickers* on *date_et* (YYYY-MM-DD ET)
    plus today's filled orders from the Val portfolio.

    Returns {bars: {ticker: [{t,o,h,l,c,v}]}, fills: [{...}], errors: [...]}.
    """
    out: dict[str, Any] = {"bars": {}, "fills": [], "errors": []}
    if not tickers and not date_et:
        return out

    # Credentials from env (already loaded by run_monitor)
    key = (os.environ.get("VAL_ALPACA_PAPER_KEY") or "").strip()
    secret = (os.environ.get("VAL_ALPACA_PAPER_SECRET") or "").strip()
    if not key or not secret:
        out["errors"].append("VAL_ALPACA_PAPER_KEY/SECRET not set")
        return out

    # ----- bars -----
    if tickers:
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

            dc = StockHistoricalDataClient(key, secret)
            # Full RTH window: 09:30–16:00 ET  = 13:30–20:00 UTC
            start_utc = datetime.fromisoformat(f"{date_et}T13:30:00+00:00")
            end_utc = datetime.fromisoformat(f"{date_et}T20:00:00+00:00")
            req = StockBarsRequest(
                symbol_or_symbols=tickers,
                timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                start=start_utc,
                end=end_utc,
                feed="sip",
            )
            resp = dc.get_stock_bars(req)
            for ticker, bar_list in (resp.data or {}).items():
                out["bars"][ticker] = [
                    {
                        "t": b.timestamp.isoformat(),
                        "o": float(b.open),
                        "h": float(b.high),
                        "l": float(b.low),
                        "c": float(b.close),
                        "v": float(b.volume),
                        "vw": float(b.vwap) if b.vwap else None,
                    }
                    for b in bar_list
                ]
        except Exception as e:
            out["errors"].append(f"bars fetch: {type(e).__name__}: {str(e)[:120]}")

    # ----- fills (via today's orders) -----
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        tc = TradingClient(key, secret, paper=True)
        req_o = GetOrdersRequest(
            status=QueryOrderStatus.ALL,
            after=f"{date_et}T00:00:00Z",
            limit=500,
        )
        orders = tc.get_orders(filter=req_o) or []
        out["fills"] = [
            {
                "symbol": str(o.symbol or ""),
                "side": str(o.side.value if hasattr(o.side, "value") else o.side),
                "filled_qty": float(o.filled_qty or 0),
                "filled_avg_price": float(o.filled_avg_price) if o.filled_avg_price else None,
                "filled_at": str(o.filled_at) if o.filled_at else None,
                "status": str(o.status.value if hasattr(o.status, "value") else o.status),
            }
            for o in orders
            if float(o.filled_qty or 0) > 0  # only filled orders
        ]
    except Exception as e:
        out["errors"].append(f"fills fetch: {type(e).__name__}: {str(e)[:120]}")

    return out


def checks_market_validation(
    raw: dict[str, Any],
    market_data: dict[str, Any],
) -> list[Check]:
    """Section 5: market-data-driven validation using Alpaca SIP bars + fills.

    Checks:
      or_break   -- entry price vs OR_high/OR_low from live or_windows
      atr_stop   -- actual stop distance vs ATR(14)x1.75 from real 5m bars
      fill_match -- Alpaca fills present for all traded tickers + P&L consistency
      rr_ratio   -- pnl / risk_dollars in expected R-multiple range
      vwap_gate  -- mega-cap entries checked against VWAP deviation (if vwap in bars)
    """
    s = "MARKET"
    checks: list[Check] = []

    if not market_data or (not market_data.get("bars") and not market_data.get("fills")):
        if market_data.get("errors"):
            checks.append(
                Check(
                    s,
                    "fetch",
                    WARN,
                    "market data unavailable: " + "; ".join(market_data.get("errors", [])),
                )
            )
        return checks

    if market_data.get("errors"):
        for err in market_data["errors"]:
            checks.append(Check(s, "fetch_warn", WARN, err))

    state = raw.get("/api/state") or {}
    v10 = state.get("v10") or {}
    or_windows = v10.get("or_windows") or {}
    cfg = v10.get("config") or {}
    vwap_tickers = set(cfg.get("max_vwap_dev_tickers") or [])
    vwap_thr_bps = float(cfg.get("max_vwap_dev_bps") or 25.0)

    trade_log = raw.get("/api/trade_log?limit=5000") or {}
    today_et = datetime.now(ET).date().isoformat()
    today = [r for r in (trade_log.get("rows") or []) if r.get("date") == today_et]

    bars = market_data.get("bars") or {}
    fills = market_data.get("fills") or []

    if not today:
        checks.append(Check(s, "no_trades", INFO, "no trades today to validate"))
        return checks

    # --------------------------------------------------------------------- #
    # 1. OR Break entry validation                                           #
    # --------------------------------------------------------------------- #
    or_issues: list[str] = []
    for t in today:
        ticker = t.get("ticker", "")
        side = (t.get("side") or "").upper()
        entry = float(t.get("entry_price") or 0)
        ow = or_windows.get(ticker)
        if not ow or not ow.get("locked"):
            continue  # OR not locked \u2014 pre-OR-close entry, skip
        or_high = float(ow.get("or_high") or 0)
        or_low = float(ow.get("or_low") or 0)
        if side == "LONG" and or_high:
            bps = (entry - or_high) / or_high * 10000
            if bps < -25:  # -25bps: covers slippage + OR_high revision after entry bar
                or_issues.append(f"{ticker} LONG ${entry:.2f} below OR_high ${or_high:.2f}")
            elif bps > 75:
                or_issues.append(f"{ticker} LONG ${entry:.2f} {bps:.0f}bps above OR_high (chasing)")
        elif side == "SHORT" and or_low:
            bps = (or_low - entry) / or_low * 10000
            if bps < -25:  # -25bps: same tolerance as LONG
                or_issues.append(f"{ticker} SHORT ${entry:.2f} above OR_low ${or_low:.2f}")
            elif bps > 75:
                or_issues.append(f"{ticker} SHORT ${entry:.2f} {bps:.0f}bps below OR_low (chasing)")

    if or_issues:
        checks.append(
            Check(
                s, "or_break", WARN, f"{len(or_issues)} OR break issue(s): " + " | ".join(or_issues)
            )
        )
    elif or_windows:
        checks.append(Check(s, "or_break", OK, f"all {len(today)} entries at/near OR break level"))

    # --------------------------------------------------------------------- #
    # 2. ATR(14) stop validation using real 5m SIP bars                     #
    # --------------------------------------------------------------------- #
    atr_issues: list[str] = []
    atr_ok_count = 0
    for t in today:
        ticker = t.get("ticker", "")
        entry = float(t.get("entry_price") or 0)
        stop = float(t.get("entry_stop") or 0)
        if not entry or not stop:
            continue
        ticker_bars = bars.get(ticker, [])
        if len(ticker_bars) < 15:
            continue  # not enough bars to compute ATR

        # Use bars up to the bar containing the entry time
        entry_time_s = str(t.get("entry_time") or "")
        try:
            h, m = int(entry_time_s[:2]), int(entry_time_s[3:5])
            entry_min_utc = h * 60 + m + 240  # ET → UTC offset (EDT=+4h)
        except (ValueError, IndexError):
            entry_min_utc = 999
        # Filter bars whose timestamp is before the entry
        pre_entry = [
            b
            for b in ticker_bars
            if (datetime.fromisoformat(b["t"]).hour * 60 + datetime.fromisoformat(b["t"]).minute)
            < entry_min_utc
        ]
        if len(pre_entry) < 15:
            pre_entry = ticker_bars[: max(15, len(ticker_bars) // 2)]

        atr = _compute_atr(pre_entry)
        if atr is None or atr <= 0:
            continue
        expected_dist = atr * 1.75
        actual_dist = abs(entry - stop)
        ratio = actual_dist / expected_dist

        if ratio < 0.25:  # 0.25: tight-OR days produce small stops; 0.35 was too strict
            atr_issues.append(
                f"{ticker} stop ${actual_dist:.2f} << ATR×1.75=${expected_dist:.2f} "
                f"(ratio={ratio:.2f})"
            )
        elif ratio > 3.0:
            atr_issues.append(
                f"{ticker} stop ${actual_dist:.2f} >> ATR×1.75=${expected_dist:.2f} "
                f"(ratio={ratio:.2f})"
            )
        else:
            atr_ok_count += 1

    if atr_issues:
        checks.append(
            Check(
                s,
                "atr_stop",
                WARN,
                f"{len(atr_issues)} stop(s) outside 0.35-3× ATR band: " + " | ".join(atr_issues),
            )
        )
    elif atr_ok_count > 0:
        checks.append(Check(s, "atr_stop", OK, f"all {atr_ok_count} stop(s) within ATR×1.75 band"))

    # --------------------------------------------------------------------- #
    # 3. Alpaca fill vs trade log: execution presence + P&L consistency    #
    #                                                                       #
    # Per-price matching is noisy: paper uses simulated mid prices while    #
    # Alpaca records actual fills (50-150bps normal slippage). Instead:     #
    #   a) Alpaca has fills for every ticker the bot traded today           #
    #   b) Alpaca day_pnl is within 30% of paper state realized P&L        #
    # --------------------------------------------------------------------- #
    if fills:
        # Exclude trades from prior bot versions: those are orphan positions from a
        # pre-deploy session that were never routed through Alpaca and will always
        # be missing from the fills set. Only check current-session trades.
        _live_ver = (raw.get("/api/state") or {}).get("version", "")
        traded_tickers = {
            t.get("ticker")
            for t in today
            if t.get("ticker") and (not _live_ver or t.get("bot_version") == _live_ver)
        }
        filled_tickers = {f.get("symbol") for f in fills if f.get("symbol")}
        missing_fills = traded_tickers - filled_tickers

        paper_pnl = sum(float(t.get("pnl") or 0) for t in today)
        val_exec = raw.get("/api/executor/val") or {}
        alpaca_pnl = float((val_exec.get("account") or {}).get("day_pnl") or 0)
        pnl_delta = abs(paper_pnl - alpaca_pnl)
        pnl_pct = (pnl_delta / max(abs(paper_pnl), 1)) * 100 if paper_pnl else 0

        fill_issues: list[str] = []
        if missing_fills:
            fill_issues.append(f"no Alpaca fills for: {sorted(missing_fills)}")
        if abs(paper_pnl) > 20 and pnl_pct > 30:
            fill_issues.append(
                f"P&L divergence: paper=${paper_pnl:+.2f} "
                f"Alpaca=${alpaca_pnl:+.2f} ({pnl_pct:.0f}% gap)"
            )
        if fill_issues:
            checks.append(Check(s, "fill_match", WARN, " | ".join(fill_issues)))
        else:
            checks.append(
                Check(
                    s,
                    "fill_match",
                    OK,
                    f"fills present for {len(traded_tickers)} ticker(s); "
                    f"P&L paper=${paper_pnl:+.2f} Alpaca=${alpaca_pnl:+.2f}",
                )
            )
    else:
        checks.append(Check(s, "fill_match", INFO, "no Alpaca fills today yet"))

    # --------------------------------------------------------------------- #
    # 4. R-multiple consistency: pnl / risk_dollars                          #
    # --------------------------------------------------------------------- #
    r_issues: list[str] = []
    for t in today:
        ep = float(t.get("entry_price") or 0)
        stop = float(t.get("entry_stop") or 0)
        sh = float(t.get("shares") or 0)
        pnl = float(t.get("pnl") or 0)
        risk = abs(ep - stop) * sh if ep and stop and sh else 0
        if risk < 50:
            continue
        r = pnl / risk
        ticker = t.get("ticker", "?")
        reason = t.get("reason", "")
        if pnl > 0:
            # Win: partial at 1R runner to 2.5R, or full exit.
            # With ORB_PARTIAL_PROFIT_AT_1R + BE stop, runners can hold
            # to large R multiples on trend days. Flag only extreme outliers.
            if r > 10.0:
                r_issues.append(f"{ticker} win +{r:.2f}R (>10R -- data check?)")
        else:
            # Loss: stop at -1R + slippage. Beyond -1.5R = unusual.
            if r < -1.8:
                r_issues.append(
                    f"{ticker} loss {r:.2f}R (<-1.8R -- excessive slippage on '{reason}'?)"
                )

    if r_issues:
        checks.append(
            Check(
                s,
                "rr_ratio",
                WARN,
                f"{len(r_issues)} R-multiple outlier(s): " + " | ".join(r_issues),
            )
        )
    else:
        checks.append(
            Check(s, "rr_ratio", OK, f"all {len(today)} trade R-multiples within expected range")
        )

    # --------------------------------------------------------------------- #
    # 5. VWAP gate for mega-caps (using bar vwap field)                      #
    # --------------------------------------------------------------------- #
    vwap_issues: list[str] = []
    for t in today:
        ticker = t.get("ticker", "")
        if ticker not in vwap_tickers:
            continue
        entry = float(t.get("entry_price") or 0)
        if not entry:
            continue
        ticker_bars = bars.get(ticker, [])
        if not ticker_bars:
            continue
        # Use VWAP from bar at/just before entry
        entry_time_s = str(t.get("entry_time") or "")
        try:
            h, m = int(entry_time_s[:2]), int(entry_time_s[3:5])
            entry_min_utc = h * 60 + m + 240
        except (ValueError, IndexError):
            continue
        bar_at_entry = None
        for b in reversed(ticker_bars):
            bt = datetime.fromisoformat(b["t"])
            if bt.hour * 60 + bt.minute <= entry_min_utc:
                bar_at_entry = b
                break
        if not bar_at_entry or not bar_at_entry.get("vw"):
            continue
        vwap = float(bar_at_entry["vw"])
        dev_bps = abs(entry - vwap) / vwap * 10000
        if dev_bps > vwap_thr_bps:
            vwap_issues.append(
                f"{ticker} entry=${entry:.2f} VWAP=${vwap:.2f} "
                f"dev={dev_bps:.0f}bps > {vwap_thr_bps:.0f}bps gate"
            )

    if vwap_issues:
        checks.append(
            Check(
                s,
                "vwap_gate",
                CRIT,
                f"{len(vwap_issues)} mega-cap entry(ies) exceeded VWAP gate: "
                + " | ".join(vwap_issues),
            )
        )
    elif vwap_tickers:
        mega_traded = [t.get("ticker") for t in today if t.get("ticker") in vwap_tickers]
        if mega_traded and bars:
            checks.append(
                Check(s, "vwap_gate", OK, f"VWAP gate respected for {sorted(set(mega_traded))}")
            )

    return checks


def send_telegram_alert(report: dict[str, Any]) -> None:
    """Send Telegram alert on any CRIT or WARN check.

    CRIT = urgent header; WARN = FYI header. Both fire so the operator
    sees config drift and trade deviations as well as hard failures.
    """
    # v9.1.72 -- guard against login-failure early-return path where run()
    # returns {"ok": False, "error": "...", "overall": CRIT} without "checks".
    _chks = report.get("checks") or []
    crits = [c for c in _chks if c["status"] == CRIT]
    warns = [c for c in _chks if c["status"] == WARN]
    if not crits and not warns:
        return
    token = (os.environ.get("TELEGRAM_TP_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_TP_CHAT_ID") or "").strip()
    if not token or not chat_id:
        return
    dry = os.environ.get("MONITOR_DRY_RUN") == "1"
    ver = report.get("version", "?")
    ts = report.get("ts_et", "")

    if crits:
        header = f"SYSTEM CHECK CRIT v{ver} | {ts}"
        body = "\n".join(f"X [{c['section']}] {c['name']}: {c['detail']}" for c in crits)
        if warns:
            body += "\n\nWarnings:\n" + "\n".join(f"! {c['name']}: {c['detail']}" for c in warns)
    else:
        header = f"SYSTEM CHECK WARN v{ver} | {ts}"
        body = "\n".join(f"! [{c['section']}] {c['name']}: {c['detail']}" for c in warns)

    msg = header + "\n\n" + body
    payload = json.dumps({"chat_id": chat_id, "text": msg}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    if dry:
        logger.info("[dry-run] would send Telegram: %s", msg[:200])
        return
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            logger.info("Telegram alert sent HTTP %d", r.status)
    except Exception as e:
        logger.warning("Telegram alert error: %s", e)


def save_report(report: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    latest = out_dir / "system_check_latest.json"
    text = json.dumps(report, indent=2, default=str)
    latest.write_text(text, encoding="utf-8")
    # daily JSONL
    day = report["captured_at_utc"][:10]
    line = json.dumps(report, separators=(",", ":"), default=str)
    with (out_dir / f"system_check_{day}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return latest


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------


def run(
    base_url: str,
    password: str,
    expected_version: str | None = None,
    skip_railway: bool = False,
    skip_alpaca: bool = False,
) -> dict[str, Any]:
    t0 = time.monotonic()

    # Login once
    session = Session(base_url, password)
    try:
        session.login()
    except Exception as e:
        return {
            "schema_version": 2,
            "tool": "system_check_bot",
            "ok": False,
            "error": f"login failed: {e}",
            "overall": CRIT,
            "ts_et": datetime.now(ET).strftime("%Y-%m-%dT%H:%M:%S ET"),
            "captured_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    # Fetch dashboard + side-channel concurrently
    alpaca: dict[str, Any] = {}
    railway: dict[str, Any] = {}
    raw: dict[str, Any] = {}

    def _dash() -> None:
        nonlocal raw
        raw = fetch_all(session)

    def _alp() -> None:
        nonlocal alpaca
        if not skip_alpaca:
            alpaca = pull_alpaca()

    def _rail() -> None:
        nonlocal railway
        if not skip_railway:
            railway = pull_railway()

    threads = [threading.Thread(target=fn) for fn in (_dash, _alp, _rail)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    # Phase 2: market data fetch \u2014 needs today's tickers from the trade log.
    # Runs after phase 1 so we know which tickers to pull bars for.
    market_data: dict[str, Any] = {}
    try:
        today_et = datetime.now(ET).date().isoformat()
        trade_log_rows = (raw.get("/api/trade_log?limit=5000") or {}).get("rows") or []
        traded_tickers = list(
            {r["ticker"] for r in trade_log_rows if r.get("date") == today_et and r.get("ticker")}
        )
        if traded_tickers:
            market_data = pull_alpaca_market_data(traded_tickers, today_et)
    except Exception as e:
        market_data = {"errors": [f"market data phase: {e}"]}

    # Run all five check sections
    checks: list[Check] = []
    checks.extend(checks_system(raw, expected_version))
    checks.extend(checks_invariants(raw, base_url))
    checks.extend(checks_strategy(raw))
    checks.extend(checks_trade_log(raw))
    checks.extend(checks_market_validation(raw, market_data))

    report = build_report(checks, raw, alpaca, railway, time.monotonic() - t0)
    report["market_data_tickers"] = list(market_data.get("bars", {}).keys())
    return report


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.environ.get(
            "DASHBOARD_BASE_URL",
            os.environ.get("DASHBOARD_URL", "https://tradegenius.up.railway.app"),
        ),
    )
    parser.add_argument("--password", default=os.environ.get("DASHBOARD_PASSWORD"))
    parser.add_argument("--version", default=None)
    parser.add_argument("--no-railway", action="store_true")
    parser.add_argument("--no-alpaca", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--alert", action="store_true")
    parser.add_argument("--save", metavar="DIR", default=None)
    args = parser.parse_args()

    if not args.password:
        print("ERROR: --password or DASHBOARD_PASSWORD required")
        return 2

    report = run(
        args.url.rstrip("/"),
        args.password,
        expected_version=args.version,
        skip_railway=args.no_railway,
        skip_alpaca=args.no_alpaca,
    )

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        if not report.get("checks"):
            print(f"ERROR: {report.get('error', 'unknown')}")
            return 2
        print_report(report)

    if args.save:
        out = save_report(report, Path(args.save))
        if not args.json:
            print(f"  Saved: {out}")

    if args.alert:
        send_telegram_alert(report)

    overall = report.get("overall", CRIT)
    return 0 if overall == OK else (1 if overall == WARN else 2)


if __name__ == "__main__":
    sys.exit(main())
