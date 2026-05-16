#!/usr/bin/env python3
"""replay_dashboard.py -- time-travel dashboard viewer.

Builds a self-contained HTML file from live production/staging state, uploads
it to Cloudflare R2, and returns a shareable URL. The page contains the real
TradeGenius dashboard UI with all scripts inlined and a time-travel bar that
lets you scrub through snapshots of the day without a page reload.

Usage:
    python scripts/replay_dashboard.py --share              # production, today
    python scripts/replay_dashboard.py --share --env prod   # explicit prod
    python scripts/replay_dashboard.py --share --date 2026-05-15

    python scripts/replay_dashboard.py                      # local server (needs JSONL)
    python scripts/replay_dashboard.py --env staging --port 8899

Architecture (share mode):
  - Fetches /api/state once from the dashboard
  - Builds 19 snapshots (every 30min + each trade event) as tiny diffs vs a
    shared base state, keeping inline JSON ~100KB instead of 836KB
  - Bar HTML is static in <body> (always visible, no JS needed to render it)
  - Fetch+EventSource patched in <head> before app.js runs
  - Navigation JS updates window.__TT_IDX and re-triggers the app poll
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "dashboard_static"
ET = ZoneInfo("America/New_York")
DEFAULT_PORT = 8899

# ---------------------------------------------------------------------------
# Injected into <head> -- runs before app.js.
# Patches window.fetch and window.EventSource.
# Uses __TT_BASE (one copy of full state) + __TT_DIFFS (per-snapshot overrides)
# so total inline data is ~100KB rather than 19 × 44KB = 836KB.
# ---------------------------------------------------------------------------
_HEAD_PATCH = """\
<script id="__tt_patch">
(function(){
  var _orig = window.fetch.bind(window);

  function currentState() {
    var base  = window.__TT_BASE  || {};
    var diffs = window.__TT_DIFFS || [];
    var diff  = diffs[window.__TT_IDX || 0] || {};
    var s = Object.assign({}, base);
    if ('trades_today'      in diff) s.trades_today      = diff.trades_today;
    if ('positions'         in diff) s.positions         = diff.positions;
    if ('server_time'       in diff) s.server_time       = diff.server_time;
    if ('server_time_label' in diff) s.server_time_label = diff.server_time_label;
    if ('eod'               in diff) s.eod               = diff.eod;
    return s;
  }

  function reply(u) {
    var s = currentState();
    if (u.indexOf('/api/state')         >= 0) return s;
    if (u.indexOf('/api/executor/val')  >= 0) return {ok:true,positions:[],trades:[]};
    if (u.indexOf('/api/executor/gene') >= 0) return {ok:true,positions:[],trades:[]};
    if (u.indexOf('/api/trade_log')     >= 0) return {ok:true,count:(s.trades_today||[]).length,rows:[]};
    if (u.indexOf('/api/indices')       >= 0) return {};
    if (u.indexOf('/api/version')       >= 0) return {version:s.version||'?'};
    if (u.indexOf('/api/intraday')      >= 0) return {};
    if (u.indexOf('/api/v10')           >= 0) return {};
    if (u.indexOf('/api/errors')        >= 0) return {errors:[]};
    return null;
  }

  window.fetch = function(url, opts) {
    var u = String(url);
    var data = reply(u);
    if (data !== null)
      return Promise.resolve(new Response(JSON.stringify(data),
        {status:200, headers:{'Content-Type':'application/json'}}));
    return _orig(url, opts);
  };

  /* Fake EventSource -- stays OPEN (readyState=1) so the app never shows
     the "Disconnected" reconnect banner. */
  function FakeES()  { this.readyState=1; this.onopen=this.onmessage=this.onerror=null; }
  FakeES.prototype.close = function(){ this.readyState=2; };
  FakeES.prototype.addEventListener = function(){};
  FakeES.prototype.removeEventListener = function(){};
  FakeES.CONNECTING=0; FakeES.OPEN=1; FakeES.CLOSED=2;
  window.EventSource = FakeES;
})();
</script>
"""

# Navigation script -- injected after app.js.
_NAV_SCRIPT = """\
<script id="__tt_nav">
(function(){
  var DIFFS = window.__TT_DIFFS || [];

  function dotCol(kind) {
    if (kind==='entry')    return '#f59e0b';
    if (kind==='exit_win') return '#4ade80';
    if (kind==='exit_loss')return '#f87171';
    return '#60a5fa';
  }

  function refresh() {
    window.fetch('/api/state',{credentials:'same-origin'})
      .then(function(r){return r.json();})
      .then(function(d){
        window.__tgLastState = d;
        if (typeof window.__tgOnState==='function') window.__tgOnState(d);
      }).catch(function(){});
  }

  function navigate(n) {
    var newIdx = Math.max(0, Math.min(DIFFS.length-1, n));
    window.__TT_IDX = newIdx;
    var d = DIFFS[newIdx] || {};
    var tsEl  = document.getElementById('__tt_ts');
    var cntEl = document.getElementById('__tt_cnt');
    if (tsEl)  tsEl.textContent = d.ts_et || '';
    if (cntEl) cntEl.textContent = (newIdx+1)+' / '+DIFFS.length;
    DIFFS.forEach(function(s,i){
      var el = document.getElementById('__tt_d'+i);
      if (!el) return;
      var col = dotCol(s.kind||'');
      var active = (i===newIdx);
      el.style.background = active ? col : '#374151';
      el.style.border = '2px solid '+(active ? col : '#374151');
      el.style.width  = active ? '10px' : '7px';
      el.style.height = active ? '10px' : '7px';
    });
    var ad = document.getElementById('__tt_d'+newIdx);
    if (ad) ad.scrollIntoView({inline:'center',block:'nearest',behavior:'smooth'});
    refresh();
  }
  window.ttNav = navigate;

  document.addEventListener('keydown', function(e){
    var cur = window.__TT_IDX||0;
    if (e.key==='ArrowLeft')  navigate(cur-1);
    if (e.key==='ArrowRight') navigate(cur+1);
  });

  /* Initial render */
  navigate(window.__TT_IDX||0);
})();
</script>
"""


# ---------------------------------------------------------------------------
# Helper: safe JSON for embedding inside <script> tags
# ---------------------------------------------------------------------------
def _js(obj: object) -> str:
    """JSON-encode and escape sequences that would break a <script> block."""
    return (
        json.dumps(obj, separators=(",", ":"), default=str)
        .replace("</", "<\\/")
        .replace("<!--", "<\\!--")
    )


# ---------------------------------------------------------------------------
# Snapshot loading (local JSONL -- for server mode)
# ---------------------------------------------------------------------------


def _monitor_dir(env: str) -> Path:
    return REPO / "data" / ("monitor-staging" if env == "staging" else "monitor")


def load_snapshots(env: str, date: str | None) -> list[dict]:
    base = _monitor_dir(env)
    if not base.exists():
        return []
    files = (
        [base / f"system_check_{date}.jsonl"]
        if date
        else sorted(base.glob("system_check_????.??.??.jsonl"))
    )
    out: list[dict] = []
    for f in files:
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("dashboard"):
                out.append(e)
    return sorted(out, key=lambda e: e.get("captured_at_utc", ""))


def find_nearest(snaps: list[dict], at_utc: str) -> dict | None:
    if not snaps:
        return None
    try:
        target = datetime.fromisoformat(at_utc.replace("Z", "+00:00"))
    except ValueError:
        return snaps[-1]
    return min(
        snaps,
        key=lambda e: abs(
            (
                datetime.fromisoformat(e["captured_at_utc"].replace("Z", "+00:00")) - target
            ).total_seconds()
        ),
    )


# ---------------------------------------------------------------------------
# Synthetic snapshot generation (share mode)
# ---------------------------------------------------------------------------


def _load_env() -> None:
    env_file = REPO / ".env.monitor"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _login(base_url: str, password: str) -> urllib.request.OpenerDirector:
    jar = urllib.request.HTTPCookieProcessor()
    opener = urllib.request.build_opener(jar)
    data = urllib.parse.urlencode({"password": password}).encode()
    opener.open(
        urllib.request.Request(
            f"{base_url}/login",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    )
    return opener


def _fetch_state(opener: urllib.request.OpenerDirector, base_url: str) -> dict:
    return json.loads(opener.open(f"{base_url}/api/state", timeout=10).read())


def _make_diff(state: dict, target_et: str) -> dict:
    """Build the small per-snapshot diff (only time-varying fields)."""
    trades = [
        t
        for t in (state.get("trades_today") or [])
        if t.get("time", "").replace(" ET", "") < target_et
    ]
    realized = sum(t.get("pnl", 0) for t in trades if "pnl" in t)

    # Zero out EOD state for snapshots before 15:00 ET
    eod = dict(state.get("eod") or {})
    if target_et < "15:00":
        pf = {
            pid: {
                "open_count": 0,
                "open_positions": [],
                "realized_pnl_today": 0.0,
                "entry_attempted": False,
                "rejected_count": 0,
                "closed_legs": [],
            }
            for pid in (eod.get("per_portfolio") or {})
        }
        eod = dict(eod, per_portfolio=pf)

    return {
        "trades_today": trades,
        "positions": [],
        "server_time": f"2026-05-15T{target_et}:00.000000-04:00",
        "server_time_label": f"Fri May 15 | {target_et}:00 ET",
        "eod": eod,
    }


def build_day_snapshots(state: dict, date_et: str = "2026-05-15") -> list[dict]:
    """Return list of {ts_et, captured_at_utc, kind, label, diff} for the day."""
    result: list[dict] = []
    seen: set[str] = set()

    def add(et_hhmm: str, kind: str = "", label: str = "") -> None:
        if et_hhmm in seen:
            return
        seen.add(et_hhmm)
        h, m = map(int, et_hhmm.split(":"))
        utc_iso = f"{date_et}T{h + 4:02d}:{m:02d}:00Z"
        result.append(
            {
                "ts_et": f"{date_et}T{et_hhmm}:00 ET",
                "captured_at_utc": utc_iso,
                "kind": kind,
                "label": label,
                "diff": _make_diff(state, et_hhmm),
            }
        )

    t = datetime(2026, 5, 15, 9, 30, tzinfo=ET)
    while t <= datetime(2026, 5, 15, 16, 1, tzinfo=ET):
        add(t.strftime("%H:%M"))
        t += timedelta(minutes=30)

    for trade in state.get("trades_today") or []:
        raw = trade.get("time", "").replace(" ET", "")
        if not raw:
            continue
        action = trade.get("action", "")
        ticker = trade.get("ticker", "")
        pnl = trade.get("pnl")
        if action in ("BUY", "SHORT"):
            add(raw, kind="entry", label=f"{action} {ticker}")
        elif action in ("SELL", "COVER"):
            pnl_s = f"+${pnl:.2f}" if (pnl or 0) >= 0 else f"-${abs(pnl or 0):.2f}"
            add(
                raw,
                kind="exit_win" if (pnl or 0) >= 0 else "exit_loss",
                label=f"{action} {ticker} {pnl_s}",
            )

    result.sort(key=lambda s: s["captured_at_utc"])
    return result


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------


def _bar_html(diffs: list[dict], start_idx: int) -> str:
    """Build the static time-travel bar HTML with all dots pre-rendered."""

    def dot_col(kind: str) -> str:
        return {"entry": "#f59e0b", "exit_win": "#4ade80", "exit_loss": "#f87171"}.get(
            kind, "#60a5fa"
        )

    dots_html = ""
    for i, d in enumerate(diffs):
        col = dot_col(d.get("kind", ""))
        active = i == start_idx
        bg = col if active else "#374151"
        sz = "10px" if active else "7px"
        title = d.get("ts_et", "") + (" \u2014 " + d.get("label", "") if d.get("label") else "")
        if i > 0:
            dots_html += (
                '<div style="flex-shrink:0;width:12px;height:1px;background:#1f2937"></div>'
            )
        dots_html += (
            f'<button id="__tt_d{i}" onclick="ttNav({i})" title="{title}" '
            f'style="flex-shrink:0;width:{sz};height:{sz};border-radius:50%;'
            f"background:{bg};border:2px solid {bg};cursor:pointer;padding:0;"
            f'transition:all .15s"></button>'
        )

    init_ts = diffs[start_idx]["ts_et"] if diffs else ""
    init_cnt = f"{start_idx + 1} / {len(diffs)}"
    total = len(diffs)

    return (
        '<div id="__tt_bar" style="position:fixed;top:0;left:0;right:0;height:44px;'
        "background:#0a0c14;color:#d1d5db;display:flex;align-items:center;"
        "z-index:2147483647;border-bottom:2px solid #f59e0b;"
        "font:12px/1 &quot;JetBrains Mono&quot;,ui-monospace,monospace;"
        'box-shadow:0 2px 12px rgba(0,0,0,.8)">'
        '<span style="color:#f59e0b;font-weight:700;padding:0 10px;white-space:nowrap;'
        'border-right:1px solid #1f2937">&#9194; TIME TRAVEL</span>'
        '<button onclick="ttNav((window.__TT_IDX||0)-1)" '
        'style="background:none;border:none;color:#9ca3af;font:inherit;cursor:pointer;'
        'padding:0 10px;height:100%;font-size:18px;border-right:1px solid #1f2937">&#8249;</button>'
        f'<span id="__tt_ts" style="color:#60a5fa;padding:0 10px;min-width:165px;'
        f'text-align:center;border-right:1px solid #1f2937">{init_ts}</span>'
        '<button onclick="ttNav((window.__TT_IDX||0)+1)" '
        'style="background:none;border:none;color:#9ca3af;font:inherit;cursor:pointer;'
        'padding:0 10px;height:100%;font-size:18px;border-left:1px solid #1f2937">&#8250;</button>'
        f'<div id="__tt_strip" style="flex:1;overflow-x:auto;display:flex;align-items:center;'
        f'height:100%;padding:0 8px">{dots_html}</div>'
        f'<span id="__tt_cnt" style="color:#6b7280;padding:0 8px;font-size:11px;'
        f'white-space:nowrap;border-left:1px solid #1f2937">{init_cnt}</span>'
        "</div>"
    )


def build_html(diffs: list[dict], base_state: dict, start_idx: int = 0) -> str:
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    app_css = (STATIC / "app.css").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    # Inline CSS
    html = html.replace(
        '<link rel="stylesheet" href="/static/app.css">', f"<style>{app_css}</style>"
    )

    # Remove app.js src (will inline it at end of body)
    html = re.sub(r'src="/static/app\.js[^"]*"', "", html)

    # -- Build small per-snapshot diff list (strips full state, keeps only overrides)
    slim_diffs = [
        {
            "ts_et": d["ts_et"],
            "captured_at_utc": d["captured_at_utc"],
            "kind": d.get("kind", ""),
            "label": d.get("label", ""),
            "trades_today": d["diff"]["trades_today"],
            "positions": d["diff"]["positions"],
            "server_time": d["diff"]["server_time"],
            "server_time_label": d["diff"]["server_time_label"],
            "eod": d["diff"]["eod"],
        }
        for d in diffs
    ]

    # Suppress reconnect banner via CSS
    replay_css = "<style id='__tt_css'>body{margin-top:48px!important}</style>\n"

    # Data + patch script goes in <head> BEFORE anything else
    head_inject = (
        replay_css + f"<script>\n"
        f"window.__TT_BASE={_js(base_state)};\n"
        f"window.__TT_DIFFS={_js(slim_diffs)};\n"
        f"window.__TT_IDX={start_idx};\n"
        f"</script>\n" + _HEAD_PATCH
    )
    html = html.replace("</head>", head_inject + "</head>", 1)

    # Static bar HTML at the very start of <body>
    bar = _bar_html(diffs, start_idx)
    html = re.sub(r"(<body[^>]*>)", r"\1" + bar, html, count=1)

    # Inline app.js + nav script at end of body
    app_js_safe = app_js.replace("</script>", "<\\/script>")
    html = html.replace("</body>", f"<script>\n{app_js_safe}\n</script>\n{_NAV_SCRIPT}\n</body>", 1)

    return html


# ---------------------------------------------------------------------------
# R2 upload + presigned URL
# ---------------------------------------------------------------------------


def _r2_sk(secret: str, date: str, region: str, svc: str) -> bytes:
    def h(k: bytes, m: str) -> bytes:
        return hmac.new(k, m.encode(), "sha256").digest()

    return h(h(h(h(f"AWS4{secret}".encode(), date), region), svc), "aws4_request")


def upload_r2(body: bytes, key: str) -> None:
    acc = os.environ["R2_ACCOUNT_ID"]
    ak = os.environ["R2_ACCESS_KEY_ID"]
    sk = os.environ["R2_SECRET_ACCESS_KEY"]
    bkt = os.environ["R2_BUCKET_NAME"]
    host = f"{acc}.r2.cloudflarestorage.com"
    now = datetime.now(timezone.utc)
    ds, ts = now.strftime("%Y%m%d"), now.strftime("%Y%m%dT%H%M%SZ")
    reg, svc = "auto", "s3"
    scope = f"{ds}/{reg}/{svc}/aws4_request"
    ch = hashlib.sha256(body).hexdigest()
    sh = "content-type;host;x-amz-content-sha256;x-amz-date"
    can = (
        f"PUT\n/{bkt}/{key}\n\n"
        f"content-type:text/html; charset=utf-8\nhost:{host}\n"
        f"x-amz-content-sha256:{ch}\nx-amz-date:{ts}\n\n{sh}\n{ch}"
    )
    sts = f"AWS4-HMAC-SHA256\n{ts}\n{scope}\n" + hashlib.sha256(can.encode()).hexdigest()
    sig = hmac.new(_r2_sk(sk, ds, reg, svc), sts.encode(), "sha256").hexdigest()
    req = urllib.request.Request(f"https://{host}/{bkt}/{key}", data=body, method="PUT")
    for k2, v in [
        ("Content-Type", "text/html; charset=utf-8"),
        ("x-amz-date", ts),
        ("x-amz-content-sha256", ch),
        (
            "Authorization",
            f"AWS4-HMAC-SHA256 Credential={ak}/{scope},SignedHeaders={sh},Signature={sig}",
        ),
    ]:
        req.add_header(k2, v)
    urllib.request.urlopen(req, timeout=60)


def presigned(key: str, expires: int = 3600) -> str:
    acc = os.environ["R2_ACCOUNT_ID"]
    ak = os.environ["R2_ACCESS_KEY_ID"]
    sk = os.environ["R2_SECRET_ACCESS_KEY"]
    bkt = os.environ["R2_BUCKET_NAME"]
    host = f"{acc}.r2.cloudflarestorage.com"
    now = datetime.now(timezone.utc)
    ds, ts = now.strftime("%Y%m%d"), now.strftime("%Y%m%dT%H%M%SZ")
    reg, svc = "auto", "s3"
    scope = f"{ds}/{reg}/{svc}/aws4_request"
    qs = urllib.parse.urlencode(
        {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{ak}/{scope}",
            "X-Amz-Date": ts,
            "X-Amz-Expires": str(expires),
            "X-Amz-SignedHeaders": "host",
        }
    )
    can = f"GET\n/{bkt}/{key}\n{qs}\nhost:{host}\n\nhost\nUNSIGNED-PAYLOAD"
    sts = f"AWS4-HMAC-SHA256\n{ts}\n{scope}\n" + hashlib.sha256(can.encode()).hexdigest()
    sig = hmac.new(_r2_sk(sk, ds, reg, svc), sts.encode(), "sha256").hexdigest()
    return f"https://{host}/{bkt}/{key}?{qs}&X-Amz-Signature={sig}"


# ---------------------------------------------------------------------------
# Local HTTP server (needs JSONL data from run_monitor.py)
# ---------------------------------------------------------------------------


def _index_html(snaps: list[dict], env: str) -> str:
    rows = "".join(
        f'<tr><td><a href="/replay/{s["captured_at_utc"]}" style="color:#60a5fa">'
        f"{s.get('ts_et', '?')}</a></td>"
        f'<td style="color:#4ade80">{s.get("overall", "?")}</td>'
        f"<td>{s.get('version', '?')}</td></tr>"
        for s in reversed(snaps)
    )
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><title>Time Travel</title>'
        f"<style>body{{background:#0a0d12;color:#d1d5db;font-family:monospace;padding:24px}}"
        f"table{{border-collapse:collapse}}td{{padding:5px 12px;border-bottom:1px solid #111}}"
        f"a{{text-decoration:none}}</style></head><body>"
        f'<h2 style="color:#f59e0b">Time Travel &mdash; {env}</h2>'
        f"<table>{rows}</table></body></html>"
    )


def _make_handler(snaps: list[dict], env: str):
    # Build base state from the most recent snapshot
    latest_dash = (snaps[-1].get("dashboard") or {}) if snaps else {}
    base_state = latest_dash.get("/api/state") or {}

    # Convert JSONL snapshots to the diff format
    diffs: list[dict] = []
    for s in snaps:
        dash = s.get("dashboard") or {}
        st = dash.get("/api/state") or {}
        diffs.append(
            {
                "ts_et": s.get("ts_et", ""),
                "captured_at_utc": s.get("captured_at_utc", ""),
                "kind": "",
                "label": "",
                "diff": {
                    "trades_today": st.get("trades_today") or [],
                    "positions": st.get("positions") or [],
                    "server_time": st.get("server_time", ""),
                    "server_time_label": st.get("server_time_label", ""),
                    "eod": st.get("eod") or {},
                },
            }
        )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = unquote(urlparse(self.path).path)
            if path == "/":
                self._html(200, _index_html(snaps, env))
            elif path.startswith("/replay/"):
                at = unquote(path[len("/replay/") :])
                snap = find_nearest(snaps, at) if at else (snaps[-1] if snaps else None)
                idx = snaps.index(snap) if snap and snap in snaps else 0
                self._html(200, build_html(diffs, base_state, idx))
            elif path.startswith("/static/"):
                p = STATIC / path[len("/static/") :]
                if p.exists() and p.is_file():
                    ct, _ = mimetypes.guess_type(str(p))
                    self._raw(200, p.read_bytes(), ct or "application/octet-stream")
                else:
                    self._raw(404, b"Not found", "text/plain")
            else:
                self._raw(200, b"{}", "application/json")

        def _html(self, s, b):
            self._raw(s, b.encode("utf-8"), "text/html; charset=utf-8")

        def _raw(self, s, b, ct):
            self.send_response(s)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *_):
            pass

    return Handler


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _load_captured_snapshots(date_et: str) -> list[dict] | None:
    """Return real captured snapshots from data/snapshots/YYYY-MM-DD.jsonl, or None."""
    snap_file = REPO / "data" / "snapshots" / f"{date_et}.jsonl"
    if not snap_file.exists():
        return None
    out = []
    for line in snap_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("dashboard"):
            out.append(e)
    return out if out else None


def _jsonl_to_diffs(snaps: list[dict]) -> tuple[list[dict], dict]:
    """Convert real captured snapshots (from snapshot_state.py) to diff format."""
    # Use the last snapshot as base state (most complete view of config/static data)
    base_state = dict((snaps[-1]["dashboard"] or {}).get("/api/state") or {})
    base_state.pop("trades_today", None)
    base_state.pop("positions", None)

    diffs = []
    for s in snaps:
        st = (s.get("dashboard") or {}).get("/api/state") or {}
        trades = st.get("trades_today") or []
        # Infer kind from last trade in snapshot (for dot coloring)
        kind, label = "", ""
        if trades:
            last = trades[-1]
            action = last.get("action", "")
            ticker = last.get("ticker", "")
            pnl = last.get("pnl")
            if action in ("BUY", "SHORT"):
                kind, label = "entry", f"{action} {ticker}"
            elif action in ("SELL", "COVER") and pnl is not None:
                pnl_s = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
                kind = "exit_win" if pnl >= 0 else "exit_loss"
                label = f"{action} {ticker} {pnl_s}"

        diffs.append(
            {
                "ts_et": s.get("ts_et", ""),
                "captured_at_utc": s.get("captured_at_utc", ""),
                "kind": kind,
                "label": label,
                "diff": {
                    "trades_today": trades,
                    "positions": st.get("positions") or [],
                    "server_time": st.get("server_time", ""),
                    "server_time_label": st.get("server_time_label", ""),
                    "eod": st.get("eod") or {},
                },
            }
        )
    return diffs, base_state


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--env", default="staging", choices=["staging", "prod"])
    p.add_argument("--date", default=None)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument(
        "--share",
        action="store_true",
        help="Build full-day HTML, upload to R2, print shareable URL",
    )
    args = p.parse_args()

    _load_env()
    date_et = args.date or datetime.now(ET).strftime("%Y-%m-%d")

    if args.share:
        # Prefer real captured snapshots (from GHA state-snapshot workflow)
        # over synthetic 30-min grid built from live API.
        captured = _load_captured_snapshots(date_et)
        if captured:
            print(
                f"Using {len(captured)} real captured snapshots from data/snapshots/{date_et}.jsonl"
            )
            day_snaps, base_state = _jsonl_to_diffs(captured)
        else:
            # Fall back to live API + synthetic grid
            base_url = (
                "https://tradegenius.up.railway.app"
                if args.env == "prod"
                else "https://tradegenius-staging.up.railway.app"
            )
            password = (
                "3YhCoi5AIZYAFG7eDua8bD8Z"
                if args.env == "prod"
                else os.environ.get("DASHBOARD_PASSWORD", "")
            )
            if not password:
                print("DASHBOARD_PASSWORD not set")
                sys.exit(1)

            print(f"No captured snapshots found \u2014 fetching live from {base_url} ...")
            opener = _login(base_url, password)
            state = _fetch_state(opener, base_url)
            print(
                f"  v{state.get('version')}  trades today: {len(state.get('trades_today') or [])}"
            )
            day_snaps = build_day_snapshots(state, date_et)
            base_state = dict(state)
            base_state.pop("trades_today", None)
            base_state.pop("positions", None)

        print(f"  {len(day_snaps)} snapshots")

        print("Generating HTML ...")
        html = build_html(day_snaps, base_state)
        body = html.encode("utf-8")
        print(f"  {len(body) // 1024} KB")

        key = f"replay/{date_et}_full.html"
        print(f"Uploading to R2 ({key}) ...")
        upload_r2(body, key)
        url = presigned(key, expires=3600)
        print(f"\nShareable URL (1 hour):\n{url}\n")
        return

    # --- Local server ---
    snaps = load_snapshots(args.env, args.date)
    if not snaps:
        print(f"No snapshots in {_monitor_dir(args.env)}")
        print("Run: python scripts/run_monitor.py")
        sys.exit(1)

    print(f"Loaded {len(snaps)} snapshots")
    server = HTTPServer(("0.0.0.0", args.port), _make_handler(snaps, args.env))
    print(f"http://localhost:{args.port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
