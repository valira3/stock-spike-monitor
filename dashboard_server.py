"""
dashboard_server.py — private live web dashboard for Stock Spike Monitor

Design principles (locked):
  • READ-ONLY. No endpoint mutates bot state. No order placement, no toggles.
  • Fail-safe: if the dashboard module raises anywhere, the bot keeps running.
  • Env-gated: if DASHBOARD_PASSWORD is unset, the server does not start at all.
  • Isolated: runs in a dedicated thread with its own asyncio loop.
                Zero coupling with python-telegram-bot's event loop.

Endpoints:
  GET  /              → login page (if no valid cookie) or dashboard
  POST /login         → form-encoded password, sets HttpOnly cookie
  POST /logout        → clears cookie
  GET  /api/state     → JSON snapshot of all live state
  GET  /stream        → Server-Sent Events: pushes state every 2s + log lines
  GET  /static/*      → dashboard_static/ (HTML/CSS/JS assets)

Cookie auth:
  Token = HMAC_SHA256(DASHBOARD_PASSWORD, "spike-dashboard-session").hexdigest()
  Stored in cookie "spike_session"; HttpOnly; SameSite=Lax; 7-day expiry.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Log ring buffer — attached to root logger once at import time
# ─────────────────────────────────────────────────────────────
_LOG_BUFFER_SIZE = 500
_log_buffer: deque = deque(maxlen=_LOG_BUFFER_SIZE)
_log_seq = 0
_log_lock = threading.Lock()


class _RingBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        global _log_seq
        try:
            msg = self.format(record)
        except Exception:
            return
        with _log_lock:
            _log_seq += 1
            _log_buffer.append({
                "seq": _log_seq,
                "ts": record.created,
                "level": record.levelname,
                "msg": msg,
            })


def _install_log_handler() -> None:
    """Attach the ring buffer to the root logger exactly once."""
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, _RingBufferHandler):
            return
    h = _RingBufferHandler()
    h.setLevel(logging.INFO)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s",
                                     datefmt="%H:%M:%S"))
    root.addHandler(h)


def _logs_since(seq: int, limit: int = 80) -> list[dict]:
    with _log_lock:
        items = [e for e in _log_buffer if e["seq"] > seq]
    if len(items) > limit:
        items = items[-limit:]
    return items


# ─────────────────────────────────────────────────────────────
# State snapshot — read live globals from stock_spike_monitor
# ─────────────────────────────────────────────────────────────
def _ssm():
    """Get the live bot module without re-executing it.

    The bot is launched via ``python stock_spike_monitor.py``, so it lives
    in ``sys.modules['__main__']``. A naive ``import stock_spike_monitor``
    here would *re-execute* the entire file (top-level entry point and
    all), which calls ``loop.add_signal_handler(...)`` from a non-main
    thread and crashes. So: prefer the already-loaded instance.
    """
    import sys
    # Prefer the already-loaded bot module (running as __main__)
    main_mod = sys.modules.get("__main__")
    if main_mod is not None and getattr(main_mod, "BOT_VERSION", None):
        return main_mod
    # Fallback: already-imported by name
    m = sys.modules.get("stock_spike_monitor")
    if m is not None:
        return m
    # Last resort (tests / standalone): import fresh
    import stock_spike_monitor as m  # noqa: F811
    return m


def _safe(fn, default):
    try:
        v = fn()
        return v if v is not None else default
    except Exception:
        return default


def _price_for(ticker: str) -> float | None:
    """Fetch current price via existing helper. Best-effort, never raises."""
    try:
        m = _ssm()
        bars = m.fetch_1min_bars(ticker)
        if bars and "current_price" in bars:
            return float(bars["current_price"])
    except Exception:
        pass
    return None


def _equity(cash: float, longs: dict, shorts: dict, prices: dict) -> tuple[float, float, float]:
    """Return (long_mv, short_liab, equity). Uses v3.3.3 equation."""
    long_mv = 0.0
    for tkr, pos in longs.items():
        px = prices.get(tkr)
        if px is None:
            px = pos.get("entry_price", 0.0)
        long_mv += float(px) * float(pos.get("shares", 0))
    short_liab = 0.0
    for tkr, pos in shorts.items():
        px = prices.get(tkr)
        if px is None:
            px = pos.get("entry_price", 0.0)
        short_liab += float(px) * float(pos.get("shares", 0))
    return long_mv, short_liab, cash + long_mv - short_liab


def _serialize_positions(longs: dict, shorts: dict, prices: dict) -> list[dict]:
    rows: list[dict] = []
    for tkr, p in longs.items():
        px = prices.get(tkr) or p.get("entry_price")
        entry = float(p.get("entry_price", 0.0))
        shares = int(p.get("shares", 0))
        unreal = (float(px) - entry) * shares if px else 0.0
        rows.append({
            "ticker": tkr,
            "side": "LONG",
            "shares": shares,
            "entry": entry,
            "mark": float(px) if px else entry,
            "stop": float(p.get("stop", 0.0)),
            "unrealized": unreal,
            "entry_time": p.get("entry_time", ""),
            "entry_count": int(p.get("entry_count", 1)),
        })
    for tkr, p in shorts.items():
        px = prices.get(tkr) or p.get("entry_price")
        entry = float(p.get("entry_price", 0.0))
        shares = int(p.get("shares", 0))
        unreal = (entry - float(px)) * shares if px else 0.0
        rows.append({
            "ticker": tkr,
            "side": "SHORT",
            "shares": shares,
            "entry": entry,
            "mark": float(px) if px else entry,
            "stop": float(p.get("stop", 0.0)),
            "unrealized": unreal,
            "entry_time": p.get("entry_time", ""),
            "entry_count": 1,
        })
    return rows


def _today_trades() -> list[dict]:
    m = _ssm()
    out: list[dict] = []
    for t in list(getattr(m, "paper_trades", []) or []):
        out.append({**t, "side": t.get("side", "LONG"), "portfolio": "paper"})
    # also include today's shorts from short_trade_history filtered by date
    try:
        today = m._now_et().strftime("%Y-%m-%d")
    except Exception:
        today = ""
    for t in list(getattr(m, "short_trade_history", []) or []):
        if t.get("date") == today:
            out.append({**t, "side": "SHORT", "portfolio": "paper"})
    # sort by time if present
    out.sort(key=lambda x: x.get("time", x.get("entry_time", "")))
    return out


def _proximity_rows() -> list[dict]:
    """Compute simple proximity metric for TRADE_TICKERS: distance to nearest level."""
    m = _ssm()
    rows: list[dict] = []
    try:
        tickers = list(getattr(m, "TRADE_TICKERS", []) or [])
    except Exception:
        tickers = []
    open_longs = set(getattr(m, "positions", {}) or {})
    open_shorts = set(getattr(m, "short_positions", {}) or {})
    for t in tickers:
        px = _price_for(t)
        orh = (getattr(m, "or_high", {}) or {}).get(t)
        orl = (getattr(m, "or_low", {}) or {}).get(t)
        pdc_v = (getattr(m, "pdc", {}) or {}).get(t)
        # distance to nearest of OR-high / OR-low / PDC, expressed as % of price
        best_pct = None
        best_label = ""
        if px and px > 0:
            for label, lvl in (("OR-high", orh), ("OR-low", orl), ("PDC", pdc_v)):
                if lvl:
                    d = abs(px - lvl) / px
                    if best_pct is None or d < best_pct:
                        best_pct = d
                        best_label = label
        open_side = None
        if t in open_longs:
            open_side = "LONG"
        elif t in open_shorts:
            open_side = "SHORT"
        rows.append({
            "ticker": t,
            "price": px,
            "or_high": orh,
            "or_low": orl,
            "pdc": pdc_v,
            "nearest_label": best_label,
            "nearest_pct": best_pct,  # smaller = closer
            "open_side": open_side,
        })
    # sort by closeness (closer first); None goes last
    rows.sort(key=lambda r: (r["nearest_pct"] is None, r["nearest_pct"] or 1e9))
    return rows


def snapshot() -> dict[str, Any]:
    """Build the full read-only snapshot. Must never raise."""
    m = _ssm()
    try:
        tickers = list(getattr(m, "TRADE_TICKERS", []) or [])
        # Price cache — one fetch per ticker in current open positions + indexes.
        # Proximity has its own fetches (best-effort).
        longs = dict(getattr(m, "positions", {}) or {})
        shorts = dict(getattr(m, "short_positions", {}) or {})
        prices: dict[str, float] = {}
        for t in set(list(longs) + list(shorts)):
            px = _price_for(t)
            if px is not None:
                prices[t] = px

        paper_cash = float(getattr(m, "paper_cash", 0.0))
        long_mv, short_liab, equity = _equity(paper_cash, longs, shorts, prices)
        start_cap = float(getattr(m, "PAPER_STARTING_CAPITAL", 100_000.0))

        # Today realized P&L from paper_trades (+ short closes today)
        realized = 0.0
        for t in (getattr(m, "paper_trades", []) or []):
            realized += float(t.get("pnl", 0.0) or 0.0)
        try:
            today = m._now_et().strftime("%Y-%m-%d")
        except Exception:
            today = ""
        for t in (getattr(m, "short_trade_history", []) or []):
            if t.get("date") == today:
                realized += float(t.get("pnl", 0.0) or 0.0)

        unreal_sum = 0.0
        for row in _serialize_positions(longs, shorts, prices):
            unreal_sum += row["unrealized"]
        day_pnl = realized + unreal_sum

        # Regime / observer
        mode = str(getattr(m, "_current_mode", "UNKNOWN"))
        mode_reason = str(getattr(m, "_current_mode_reason", ""))
        breadth = str(getattr(m, "_current_breadth", "UNKNOWN"))
        breadth_detail = str(getattr(m, "_current_breadth_detail", ""))
        rsi_regime = str(getattr(m, "_current_rsi_regime", "UNKNOWN"))
        rsi_detail = str(getattr(m, "_current_rsi_detail", ""))

        halted = bool(getattr(m, "_trading_halted", False))
        halt_reason = str(getattr(m, "_trading_halted_reason", ""))
        scan_paused = bool(getattr(m, "_scan_paused", False))
        or_date = str(getattr(m, "or_collected_date", ""))

        # ticker_pnl / red list
        ticker_pnl = dict(getattr(m, "_current_ticker_pnl", {}) or {})
        ticker_red = list(getattr(m, "_current_ticker_red", []) or [])

        version = str(getattr(m, "BOT_VERSION", "?"))

        try:
            now_et = m._now_et()
            now_iso = now_et.isoformat()
            now_label = now_et.strftime("%a %b %d · %H:%M:%S ET")
        except Exception:
            now_iso = ""
            now_label = ""

        return {
            "ok": True,
            "version": version,
            "server_time": now_iso,
            "server_time_label": now_label,
            "portfolio": {
                "cash": paper_cash,
                "long_mv": long_mv,
                "short_liab": short_liab,
                "equity": equity,
                "start": start_cap,
                "vs_start": equity - start_cap,
                "day_pnl": day_pnl,
                "day_pnl_realized": realized,
                "day_pnl_unrealized": unreal_sum,
            },
            "positions": _serialize_positions(longs, shorts, prices),
            "trades_today": _today_trades(),
            "proximity": _proximity_rows(),
            "regime": {
                "mode": mode,
                "mode_reason": mode_reason,
                "breadth": breadth,
                "breadth_detail": breadth_detail,
                "rsi_regime": rsi_regime,
                "rsi_detail": rsi_detail,
            },
            "gates": {
                "trading_halted": halted,
                "halt_reason": halt_reason,
                "scan_paused": scan_paused,
                "or_collected_date": or_date,
            },
            "observer": {
                "ticker_pnl": ticker_pnl,
                "ticker_red": ticker_red[:5],
            },
            "tickers": tickers,
        }
    except Exception as e:
        logger.exception("dashboard snapshot failed: %s", e)
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────
# HTTP server
# ─────────────────────────────────────────────────────────────
SESSION_COOKIE = "spike_session"
SESSION_DAYS = 7

_PW: str = ""
_TOKEN: str = ""
_STATIC_DIR = Path(__file__).parent / "dashboard_static"


def _make_token(pw: str) -> str:
    return hmac.new(pw.encode("utf-8"), b"spike-dashboard-session",
                    hashlib.sha256).hexdigest()


def _check_auth(request) -> bool:
    if not _TOKEN:
        return False
    c = request.cookies.get(SESSION_COOKIE, "")
    return hmac.compare_digest(c, _TOKEN)


def _login_page(error: str = "") -> str:
    err_html = f'<div class="err">{error}</div>' if error else ""
    return """<!doctype html><html><head><meta charset="utf-8"><title>Spike Monitor — sign in</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
html,body{margin:0;height:100%;background:#0a0d12;color:#e7ecf3;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
display:grid;place-items:center}
form{background:#10151c;border:1px solid #1f2937;border-radius:10px;
padding:28px 32px;min-width:320px;display:flex;flex-direction:column;gap:14px}
h1{font-size:16px;margin:0 0 6px;letter-spacing:-0.01em}
label{font-size:11px;text-transform:uppercase;letter-spacing:0.08em;color:#8a96a7}
input{background:#0a0d12;border:1px solid #2a3646;color:#e7ecf3;
padding:9px 12px;border-radius:6px;font-size:14px;font-family:inherit}
input:focus{outline:none;border-color:#7dd3fc}
button{background:#7dd3fc;color:#0a0d12;border:none;padding:9px 12px;
border-radius:6px;font-weight:600;cursor:pointer;font-size:13px}
button:hover{background:#a8e0fc}
.err{color:#f87171;font-size:12px}
.brand{display:flex;align-items:center;gap:10px;color:#7dd3fc;margin-bottom:4px}
.brand svg{width:24px;height:24px}
.brand span{color:#e7ecf3;font-weight:700;font-size:14px}
</style></head><body>
<form method="post" action="/login">
  <div class="brand">
    <svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2.2">
      <path d="M3 22 L11 22 L15 10 L19 22 L29 22" stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="15" cy="10" r="2" fill="currentColor" stroke="none"/>
    </svg>
    <span>Spike Monitor</span>
  </div>
  <h1>Sign in</h1>
  __ERR__
  <label for="pw">Password</label>
  <input id="pw" name="password" type="password" autofocus required>
  <button type="submit">Enter dashboard</button>
</form></body></html>""".replace("__ERR__", err_html)


async def h_root(request):
    from aiohttp import web
    if not _check_auth(request):
        return web.Response(text=_login_page(), content_type="text/html")
    idx = _STATIC_DIR / "index.html"
    if not idx.exists():
        return web.Response(text="dashboard_static/index.html missing",
                            status=500, content_type="text/plain")
    return web.FileResponse(idx)


async def h_login(request):
    from aiohttp import web
    data = await request.post()
    pw = (data.get("password") or "").strip()
    if not pw or not hmac.compare_digest(pw, _PW):
        return web.Response(text=_login_page("Invalid password"),
                            content_type="text/html", status=401)
    resp = web.HTTPFound("/")
    resp.set_cookie(
        SESSION_COOKIE, _TOKEN,
        max_age=SESSION_DAYS * 24 * 3600,
        httponly=True, samesite="Lax", secure=False,
    )
    return resp


async def h_logout(request):
    from aiohttp import web
    resp = web.HTTPFound("/")
    resp.del_cookie(SESSION_COOKIE)
    return resp


async def h_state(request):
    from aiohttp import web
    if not _check_auth(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    # snapshot() may do blocking I/O (fetch_1min_bars). Run in executor.
    loop = asyncio.get_running_loop()
    snap = await loop.run_in_executor(None, snapshot)
    return web.json_response(snap)


async def h_stream(request):
    from aiohttp import web
    if not _check_auth(request):
        return web.Response(status=401, text="unauthorized")
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(request)
    last_log_seq = 0
    loop = asyncio.get_running_loop()
    try:
        while True:
            snap = await loop.run_in_executor(None, snapshot)
            payload = json.dumps({"t": "state", "data": snap})
            await resp.write(f"event: state\ndata: {payload}\n\n".encode("utf-8"))

            new_logs = _logs_since(last_log_seq)
            if new_logs:
                last_log_seq = new_logs[-1]["seq"]
                payload = json.dumps({"t": "logs", "data": new_logs})
                await resp.write(f"event: logs\ndata: {payload}\n\n".encode("utf-8"))

            # heartbeat comment to keep proxies from closing
            await resp.write(b": ping\n\n")
            await asyncio.sleep(2.0)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    except Exception as e:
        logger.warning("dashboard stream closed: %s", e)
    return resp


# ─────────────────────────────────────────────────────────────
# Thread entrypoint — started from stock_spike_monitor.py
# ─────────────────────────────────────────────────────────────
def _build_app():
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/", h_root)
    app.router.add_post("/login", h_login)
    app.router.add_post("/logout", h_logout)
    app.router.add_get("/api/state", h_state)
    app.router.add_get("/stream", h_stream)
    if _STATIC_DIR.exists():
        app.router.add_static("/static/", path=_STATIC_DIR, show_index=False)
    return app


def _run_forever(port: int):
    import asyncio as _a
    from aiohttp import web
    loop = _a.new_event_loop()
    _a.set_event_loop(loop)
    app = _build_app()
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    loop.run_until_complete(site.start())
    logger.info("Dashboard listening on 0.0.0.0:%d", port)
    try:
        loop.run_forever()
    finally:
        loop.run_until_complete(runner.cleanup())


def start_in_thread() -> bool:
    """Start the dashboard server in a daemon thread. Returns True if started."""
    global _PW, _TOKEN
    _PW = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if not _PW:
        logger.info("Dashboard disabled: DASHBOARD_PASSWORD not set")
        return False
    try:
        port = int(os.getenv("DASHBOARD_PORT", "8080"))
    except ValueError:
        port = 8080
    _TOKEN = _make_token(_PW)

    _install_log_handler()

    t = threading.Thread(target=_run_forever, args=(port,),
                         name="dashboard-http", daemon=True)
    t.start()
    logger.info("Dashboard thread started (port=%d)", port)
    return True
