"""
dashboard_server.py — private live web dashboard for TradeGenius

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

Cookie auth (v3.4.9+; persistent in v3.4.29):
  Token = HMAC_SHA256(_SESSION_SECRET, b"<8-byte BE timestamp>").hex() + ":<ts>"
  _SESSION_SECRET is a random 32-byte secret generated ONCE on first boot
  and written to ``dashboard_secret.key`` (sibling of ``paper_state.json``,
  on the Railway volume). Every subsequent boot reads the same file, so
  redeploys no longer invalidate sessions. Override with
  ``DASHBOARD_SESSION_SECRET`` env (tests).
  Tokens carry the issue-timestamp and are checked for expiry (SESSION_DAYS).
  Stored in cookie "spike_session"; HttpOnly; SameSite=Lax; Secure=True;
  7-day max age.

Login rate-limiting:
  Per-IP in-memory rate limiter on POST /login — 5 attempts per 60s window.
  Failed attempts beyond the limit return HTTP 429.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import struct
import threading
import time
from collections import defaultdict, deque
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
# State snapshot — read live globals from trade_genius
# ─────────────────────────────────────────────────────────────
def _ssm():
    """Get the live bot module without re-executing it.

    The bot is launched via ``python trade_genius.py``, so it lives
    in ``sys.modules['__main__']``. A naive ``import trade_genius``
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
    m = sys.modules.get("trade_genius")
    if m is not None:
        return m
    # Last resort (tests / standalone): import fresh
    import trade_genius as m  # noqa: F811
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
        # v3.4.26: expose trail state + effective stop so the UI can
        # show what's actually managing the position (hard stop vs
        # armed trail). effective_stop mirrors the exit-decision rule
        # in manage_positions.
        hard_stop = float(p.get("stop", 0.0))
        trail_active = bool(p.get("trail_active", False))
        trail_stop_raw = p.get("trail_stop")
        trail_stop = float(trail_stop_raw) if trail_stop_raw is not None else None
        trail_high_raw = p.get("trail_high")
        trail_anchor = float(trail_high_raw) if trail_high_raw is not None else None
        effective_stop = trail_stop if (trail_active and trail_stop is not None) else hard_stop
        rows.append({
            "ticker": tkr,
            "side": "LONG",
            "shares": shares,
            "entry": entry,
            "mark": float(px) if px else entry,
            "stop": hard_stop,
            "trail_active": trail_active,
            "trail_stop": trail_stop,
            "trail_anchor": trail_anchor,
            "effective_stop": effective_stop,
            "unrealized": unreal,
            "entry_time": p.get("entry_time", ""),
            "entry_count": int(p.get("entry_count", 1)),
        })
    for tkr, p in shorts.items():
        px = prices.get(tkr) or p.get("entry_price")
        entry = float(p.get("entry_price", 0.0))
        shares = int(p.get("shares", 0))
        unreal = (entry - float(px)) * shares if px else 0.0
        hard_stop = float(p.get("stop", 0.0))
        trail_active = bool(p.get("trail_active", False))
        trail_stop_raw = p.get("trail_stop")
        trail_stop = float(trail_stop_raw) if trail_stop_raw is not None else None
        trail_low_raw = p.get("trail_low")
        trail_anchor = float(trail_low_raw) if trail_low_raw is not None else None
        effective_stop = trail_stop if (trail_active and trail_stop is not None) else hard_stop
        rows.append({
            "ticker": tkr,
            "side": "SHORT",
            "shares": shares,
            "entry": entry,
            "mark": float(px) if px else entry,
            "stop": hard_stop,
            "trail_active": trail_active,
            "trail_stop": trail_stop,
            "trail_anchor": trail_anchor,
            "effective_stop": effective_stop,
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


def _ticker_gates(m, tickers: list[str]) -> list[dict]:
    """v3.4.21 — serialize _gate_snapshot for the dashboard.

    Returns one row per known ticker in the same order as TRADE_TICKERS.
    Tickers with no snapshot yet get a placeholder with side=None.
    """
    snap = dict(getattr(m, "_gate_snapshot", {}) or {})
    rows = []
    for t in tickers:
        g = snap.get(t) or {}
        rows.append({
            "ticker": t,
            "side": g.get("side"),
            "break": g.get("break"),
            "vol_pct": g.get("vol_pct"),
            "vol_ok": g.get("vol_ok"),
            "polarity": g.get("polarity"),
            "index": g.get("index"),
            "ts": g.get("ts"),
        })
    return rows


def _next_scan_seconds(m) -> int | None:
    """v3.4.21 — seconds until the next scan cycle, or None if unknown.

    Computes max(0, SCAN_INTERVAL - age_of_last_scan). Clamps to
    [0, SCAN_INTERVAL]. Returns None if the scanner hasn't started.
    """
    last = getattr(m, "_last_scan_time", None)
    if last is None:
        return None
    interval = int(getattr(m, "SCAN_INTERVAL", 60) or 60)
    try:
        from datetime import datetime, timezone
        age = (datetime.now(timezone.utc) - last).total_seconds()
    except Exception:
        return None
    remaining = interval - age
    if remaining < 0:
        return 0
    if remaining > interval:
        return interval
    return int(remaining)


def _sovereign_regime_snapshot(m) -> dict[str, Any]:
    """v3.4.29 — Live Sovereign Regime Shield state for the dashboard.

    Mirrors the data the Shield itself reads (SPY/QQQ PDC + the most
    recent FINALIZED 1-minute close). The front-end uses this to tell
    Val at a glance whether the long-eject or short-eject gate would
    fire on the next tick, and why.

    Shape (every field is optional — fail-closed in the UI):

        {
          "spy_price":      float|None,    # last finalized 1m close
          "spy_pdc":        float|None,
          "spy_delta_pct":  float|None,    # (price - pdc) / pdc * 100
          "spy_above_pdc":  bool|None,
          "qqq_price":      float|None,
          "qqq_pdc":        float|None,
          "qqq_delta_pct":  float|None,
          "qqq_above_pdc":  bool|None,
          "long_eject":     bool,          # _sovereign_regime_eject("long")
          "short_eject":    bool,          # _sovereign_regime_eject("short")
          "status":         str,           # ARMED_LONG | ARMED_SHORT |
                                           # DISARMED | AWAITING | NO_PDC
          "reason":         str,           # short human explanation
        }

    Never raises. Returns a fully-populated dict with None/False
    fields if any data is missing, so the UI always has a stable
    shape.
    """
    out: dict[str, Any] = {
        "spy_price": None, "spy_pdc": None, "spy_delta_pct": None,
        "spy_above_pdc": None,
        "qqq_price": None, "qqq_pdc": None, "qqq_delta_pct": None,
        "qqq_above_pdc": None,
        "long_eject": False, "short_eject": False,
        "status": "NO_PDC", "reason": "",
    }
    try:
        pdc_map = getattr(m, "pdc", {}) or {}
        spy_pdc = pdc_map.get("SPY")
        qqq_pdc = pdc_map.get("QQQ")
        if isinstance(spy_pdc, (int, float)) and spy_pdc > 0:
            out["spy_pdc"] = float(spy_pdc)
        if isinstance(qqq_pdc, (int, float)) and qqq_pdc > 0:
            out["qqq_pdc"] = float(qqq_pdc)

        # Finalized 1m close the Shield actually reads.
        helper = getattr(m, "_last_finalized_1min_close", None)
        if callable(helper):
            try:
                sc = helper("SPY")
                if isinstance(sc, (int, float)):
                    out["spy_price"] = float(sc)
            except Exception:
                pass
            try:
                qc = helper("QQQ")
                if isinstance(qc, (int, float)):
                    out["qqq_price"] = float(qc)
            except Exception:
                pass

        # Deltas (only if both price and PDC present).
        if out["spy_price"] is not None and out["spy_pdc"]:
            out["spy_delta_pct"] = (out["spy_price"] - out["spy_pdc"]) / out["spy_pdc"] * 100.0
            out["spy_above_pdc"] = out["spy_price"] > out["spy_pdc"]
        if out["qqq_price"] is not None and out["qqq_pdc"]:
            out["qqq_delta_pct"] = (out["qqq_price"] - out["qqq_pdc"]) / out["qqq_pdc"] * 100.0
            out["qqq_above_pdc"] = out["qqq_price"] > out["qqq_pdc"]

        # Ask the Shield itself for the ground-truth eject booleans.
        eject = getattr(m, "_sovereign_regime_eject", None)
        if callable(eject):
            try:
                out["long_eject"] = bool(eject("long"))
            except Exception:
                out["long_eject"] = False
            try:
                out["short_eject"] = bool(eject("short"))
            except Exception:
                out["short_eject"] = False

        # Human-readable status.
        if out["spy_pdc"] is None or out["qqq_pdc"] is None:
            out["status"] = "NO_PDC"
            out["reason"] = "PDC not yet collected (pre-open)"
        elif out["spy_price"] is None or out["qqq_price"] is None:
            out["status"] = "AWAITING"
            out["reason"] = "waiting for first finalized 1m close"
        elif out["long_eject"]:
            out["status"] = "ARMED_LONG"
            out["reason"] = "SPY and QQQ both 1m close < PDC — longs would eject"
        elif out["short_eject"]:
            out["status"] = "ARMED_SHORT"
            out["reason"] = "SPY and QQQ both 1m close > PDC — shorts would eject"
        else:
            # Both below PDC only triggers long eject; both above only short
            # eject. Divergence is the last remaining case: one each side.
            if (out["spy_above_pdc"] is not None
                    and out["qqq_above_pdc"] is not None
                    and out["spy_above_pdc"] != out["qqq_above_pdc"]):
                out["status"] = "DISARMED"
                out["reason"] = "SPY/QQQ diverge vs PDC — hysteresis holds"
            else:
                out["status"] = "DISARMED"
                out["reason"] = "SPY and QQQ both on expected side of PDC"
    except Exception as e:
        logger.debug("_sovereign_regime_snapshot failed: %s", e)
    return out


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

        # Today realized P&L from paper_trades (long SELLs, today only) +
        # short_trade_history (short COVERs, today only). Date-filter both
        # lists — paper_trades may carry yesterday's rows after a
        # post-midnight restart before reset_daily_state() runs at 09:30 ET.
        try:
            today = m._now_et().strftime("%Y-%m-%d")
        except Exception:
            today = ""
        realized = 0.0
        for t in (getattr(m, "paper_trades", []) or []):
            if t.get("date") == today and t.get("action") == "SELL":
                realized += float(t.get("pnl", 0.0) or 0.0)
        for t in (getattr(m, "short_trade_history", []) or []):
            if t.get("date") == today:
                realized += float(t.get("pnl", 0.0) or 0.0)

        unreal_sum = 0.0
        for row in _serialize_positions(longs, shorts, prices):
            unreal_sum += row["unrealized"]
        day_pnl = realized + unreal_sum

        # v3.4.29 — Sovereign Regime Shield live state for the dashboard.
        sovereign = _sovereign_regime_snapshot(m)

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
                # v3.4.29 — Sovereign Regime Shield (live PDC-based eject).
                "sovereign": sovereign,
            },
            "gates": {
                "trading_halted": halted,
                "halt_reason": halt_reason,
                "scan_paused": scan_paused,
                "or_collected_date": or_date,
                # v3.4.21 — per-ticker entry-gate chips: Break / Vol / PDC / Idx.
                "per_ticker": _ticker_gates(m, tickers),
                # v3.4.21 — next scan countdown (seconds until next tick).
                "next_scan_sec": _next_scan_seconds(m),
            },
            "near_misses": list(getattr(m, "_near_miss_log", []) or []),
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
_SESSION_SECRET: bytes = b""   # set at startup; see _load_or_create_session_secret


def _session_secret_path() -> str:
    """Path for the persistent session signing key.

    Lives beside PAPER_STATE_FILE so it inherits the Railway volume
    mount automatically. Never hard-codes the volume path — it derives
    from whatever the bot uses for state, so local dev, tests, and
    prod all get consistent behavior.
    """
    m = _ssm()
    paper_state = getattr(m, "PAPER_STATE_FILE", "paper_state.json")
    d = os.path.dirname(paper_state) or "."
    return os.path.join(d, "dashboard_secret.key")


def _load_or_create_session_secret() -> bytes:
    """Resolve the dashboard session signing key.

    Resolution order:
      1. DASHBOARD_SESSION_SECRET env (hex) — forces an explicit secret,
         used by tests and manual rotation.
      2. dashboard_secret.key on disk — the persistent case. Read 32
         bytes; reject anything shorter (treat as missing).
      3. Generate fresh 32 random bytes, best-effort write to disk so
         the next boot can reuse. Disk write failure is logged and
         swallowed — the server still starts with the in-memory secret.

    Always returns 32 bytes suitable for HMAC-SHA256.
    """
    env = os.getenv("DASHBOARD_SESSION_SECRET", "").strip()
    if env:
        try:
            b = bytes.fromhex(env)
            if len(b) >= 16:
                return b
        except ValueError:
            pass  # fall through to file/gen

    path = _session_secret_path()
    try:
        if os.path.exists(path):
            with open(path, "rb") as f:
                data = f.read()
            if len(data) >= 32:
                logger.info("Dashboard session secret loaded from %s (persistent)", path)
                return data[:32]
            logger.warning(
                "Dashboard session secret at %s too short (%d bytes) — regenerating",
                path, len(data),
            )
    except OSError as e:
        logger.warning("Dashboard session secret read failed (%s): %s", path, e)

    new_secret = secrets.token_bytes(32)
    try:
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(new_secret)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass  # non-POSIX filesystems
        os.replace(tmp, path)
        logger.info("Dashboard session secret generated and persisted to %s", path)
    except OSError as e:
        logger.warning(
            "Dashboard session secret write failed (%s): %s — using in-memory for this boot",
            path, e,
        )
    return new_secret


_STATIC_DIR = Path(__file__).parent / "dashboard_static"

# Login rate-limiter: per-IP attempt timestamps (sliding window)
_LOGIN_WINDOW_SEC = 60
_LOGIN_MAX_ATTEMPTS = 5
_login_attempts: dict = defaultdict(list)
_login_attempts_lock = threading.Lock()


def _client_ip(request) -> str:
    """Best-effort client IP — prefer X-Forwarded-For (Railway proxy) and
    fall back to peer address. Used only as a rate-limit bucket key."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote or "unknown"


def _rate_limit_check(ip: str) -> bool:
    """Returns True if request is allowed; False if rate-limited.
    Prunes old timestamps and records a new attempt on success."""
    now = time.time()
    with _login_attempts_lock:
        attempts = _login_attempts[ip]
        # prune outside window
        attempts[:] = [t for t in attempts if now - t < _LOGIN_WINDOW_SEC]
        if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
            return False
        attempts.append(now)
        return True


def _make_token(now: float | None = None) -> str:
    """Issue a session token: HMAC(_SESSION_SECRET, ts) + ":" + ts.
    The timestamp lets us enforce expiry without DB state. Secret is
    process-local random bytes, so restarts invalidate all sessions.
    """
    if now is None:
        now = time.time()
    ts = int(now)
    msg = struct.pack(">Q", ts)
    sig = hmac.new(_SESSION_SECRET, msg, hashlib.sha256).hexdigest()
    return f"{sig}:{ts}"


def _check_auth(request) -> bool:
    """Validate the session cookie. Rejects:
      • missing/malformed cookie
      • wrong signature (constant-time compare)
      • expired token (issue ts older than SESSION_DAYS)
      • future-dated token (clock-skew > 60s)
    """
    if not _SESSION_SECRET:
        return False
    c = request.cookies.get(SESSION_COOKIE, "")
    if not c or ":" not in c:
        return False
    try:
        sig, ts_str = c.rsplit(":", 1)
        ts = int(ts_str)
    except (ValueError, AttributeError):
        return False
    expected = hmac.new(_SESSION_SECRET, struct.pack(">Q", ts),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    age = time.time() - ts
    if age < -60:    # future-dated beyond clock-skew tolerance
        return False
    if age > SESSION_DAYS * 86400:
        return False
    return True


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
    ip = _client_ip(request)
    if not _rate_limit_check(ip):
        logger.warning("dashboard /login rate-limited ip=%s", ip)
        return web.Response(
            text="Too many attempts. Try again in 60 seconds.",
            status=429, content_type="text/plain",
            headers={"Retry-After": str(_LOGIN_WINDOW_SEC)},
        )
    data = await request.post()
    pw = (data.get("password") or "").strip()
    if not pw or not hmac.compare_digest(pw, _PW):
        return web.Response(text=_login_page("Invalid password"),
                            content_type="text/html", status=401)
    # On success, issue a fresh timestamped token so each login starts a
    # new 7-day window. Cookie is Secure (Railway terminates TLS).
    token = _make_token()
    resp = web.HTTPFound("/")
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_DAYS * 24 * 3600,
        httponly=True, samesite="Lax", secure=True,
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


# ─────────────────────────────────────────────────────────────
# v4.0.0-beta — per-executor tab + index ticker strip
# ─────────────────────────────────────────────────────────────
# Each per-tab endpoint hits live Alpaca on demand so Val and Gene tabs
# reflect reality (not main's paper book). Results are cached server-side
# for 15 s keyed by (name, mode) so multiple browsers + auto-refresh
# don't fan out into Alpaca rate limits.
#
# The index ticker strip reuses whichever executor's paper keys exist to
# open a StockHistoricalDataClient. VIX is an index, not an equity; if
# Alpaca's equity feed refuses the symbol we surface "n/a" for just VIX
# and keep the rest of the strip alive.

_EXECUTOR_CACHE_TTL = 15.0
_INDICES_CACHE_TTL = 30.0

_executor_cache: dict = {}        # {(name, mode): (ts, payload)}
_executor_cache_lock = threading.Lock()
_indices_cache: dict = {"ts": 0.0, "payload": None}
_indices_cache_lock = threading.Lock()


def _get_executor(name: str):
    """Resolve the executor instance from the live bot module."""
    m = _ssm()
    attr = "val_executor" if name == "val" else "gene_executor"
    return getattr(m, attr, None)


def _executor_snapshot(name: str) -> dict:
    """Build the JSON payload for one per-executor tab.

    Returns {enabled, mode, healthy, account, positions, last_signal,
    error}. Never raises — any Alpaca failure surfaces via error field
    with enabled=True so the front-end can render a graceful panel.
    """
    executor = _get_executor(name)
    if executor is None:
        return {"enabled": False, "error": f"{name} executor not enabled"}

    mode = executor.mode
    cache_key = (name, mode)
    now = time.time()
    with _executor_cache_lock:
        ent = _executor_cache.get(cache_key)
        if ent and (now - ent[0]) < _EXECUTOR_CACHE_TTL:
            return ent[1]

    payload: dict = {
        "enabled": True,
        "mode": mode,
        "healthy": False,
        "account": None,
        "positions": [],
        "last_signal": None,
        "error": None,
    }
    try:
        payload["last_signal"] = getattr(executor, "last_signal", None)
    except Exception:
        payload["last_signal"] = None

    try:
        client = executor._ensure_client()
    except Exception as e:
        payload["error"] = f"client build failed: {e}"
        with _executor_cache_lock:
            _executor_cache[cache_key] = (now, payload)
        return payload

    if client is None:
        payload["error"] = "alpaca client unavailable (missing keys?)"
        with _executor_cache_lock:
            _executor_cache[cache_key] = (now, payload)
        return payload

    try:
        acct = client.get_account()
        payload["account"] = {
            "cash": float(getattr(acct, "cash", 0) or 0),
            "buying_power": float(getattr(acct, "buying_power", 0) or 0),
            "equity": float(getattr(acct, "equity", 0) or 0),
            "account_number": str(getattr(acct, "account_number", "") or ""),
            "status": str(getattr(acct, "status", "") or ""),
        }
        positions = client.get_all_positions()
        rows = []
        for p in positions:
            qty = float(getattr(p, "qty", 0) or 0)
            avg_entry = float(getattr(p, "avg_entry_price", 0) or 0)
            cur = float(getattr(p, "current_price", 0) or 0)
            side_raw = str(getattr(p, "side", "") or "").lower()
            side = "SHORT" if "short" in side_raw or qty < 0 else "LONG"
            unreal = float(getattr(p, "unrealized_pl", 0) or 0)
            unreal_pct_raw = getattr(p, "unrealized_plpc", None)
            try:
                unreal_pct = float(unreal_pct_raw) * 100.0 if unreal_pct_raw is not None else 0.0
            except (TypeError, ValueError):
                unreal_pct = 0.0
            rows.append({
                "symbol": str(getattr(p, "symbol", "") or ""),
                "side": side,
                "qty": abs(qty),
                "avg_entry": avg_entry,
                "current_price": cur,
                "unrealized_pnl": unreal,
                "unrealized_pnl_pct": unreal_pct,
            })
        payload["positions"] = rows
        payload["healthy"] = True
    except Exception as e:
        # Surface the full Alpaca exception so credential / endpoint /
        # account-type problems are diagnosable from /api/executor/<name>
        # instead of hidden behind "Not Found".
        #
        # NOTE: alpaca-py APIError exposes `.code` as a @property that calls
        # json.loads(self._error) and raises JSONDecodeError when the HTTP
        # body is empty / non-JSON (e.g. Alpaca's 404 empty-body for an
        # unknown key). getattr(e, "code", None) INVOKES that property, so
        # we only read attrs we know are safe (__dict__ / class attrs) and
        # never trigger descriptors from inside the error handler.
        err_type = type(e).__name__
        try:
            err_msg = str(e) or "(no message)"
        except Exception:
            err_msg = "(str(e) raised)"
        extras = []
        # Only pull from instance __dict__ so property-backed attrs
        # (like alpaca-py APIError.code) can never raise here.
        inst = getattr(e, "__dict__", {}) or {}
        status_code = inst.get("_status_code") or inst.get("status_code")
        if status_code is not None:
            extras.append(f"status_code={status_code}")
        raw_body = inst.get("_error") or inst.get("_body")
        if raw_body:
            try:
                extras.append(f"body={str(raw_body)[:200]!r}")
            except Exception:
                pass
        resp = inst.get("response") or inst.get("_response")
        if resp is not None and "body=" not in " ".join(extras):
            body = getattr(resp, "text", None)
            if body:
                try:
                    extras.append(f"body={str(body)[:200]!r}")
                except Exception:
                    pass
        extra_str = (" " + " ".join(extras)) if extras else ""
        payload["error"] = f"alpaca fetch failed: {err_type}: {err_msg}{extra_str}"
        logger.warning(
            "executor %s alpaca fetch failed: %s: %s%s",
            name, err_type, err_msg, extra_str,
        )

    with _executor_cache_lock:
        _executor_cache[cache_key] = (now, payload)
    return payload


def _resolve_data_client():
    """Pick the first available executor's paper keys and build a
    StockHistoricalDataClient. Returns None if nothing is usable."""
    for name in ("val", "gene"):
        ex = _get_executor(name)
        if ex is None:
            continue
        key = getattr(ex, "paper_key", "") or ""
        secret = getattr(ex, "paper_secret", "") or ""
        if key and secret:
            try:
                from alpaca.data.historical import StockHistoricalDataClient
                return StockHistoricalDataClient(key, secret)
            except Exception as e:
                logger.warning("data client build failed for %s: %s", name, e)
                continue
    return None


def _fetch_indices() -> dict:
    """Build the index ticker strip payload. Never raises."""
    symbols = ["SPY", "QQQ", "DIA", "IWM", "VIX"]
    out = {
        "ok": True,
        "as_of": "",
        "indices": [],
        "error": None,
    }
    client = _resolve_data_client()
    if client is None:
        out["ok"] = False
        out["error"] = "no executor paper keys available"
        return out

    try:
        from alpaca.data.requests import StockLatestQuoteRequest, StockSnapshotRequest
    except Exception as e:
        out["ok"] = False
        out["error"] = f"alpaca-py imports failed: {e}"
        return out

    # Snapshot gives previous-close + latest-trade in one shot. VIX is an
    # index (not an equity), so Alpaca's equity feed likely refuses it —
    # we isolate that failure so SPY/QQQ/DIA/IWM still render.
    equity_symbols = [s for s in symbols if s != "VIX"]
    snapshots: dict = {}
    try:
        resp = client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=equity_symbols))
        snapshots = resp if isinstance(resp, dict) else {}
    except Exception as e:
        out["error"] = f"snapshot failed: {e}"
        snapshots = {}

    for sym in symbols:
        snap = snapshots.get(sym)
        last = None
        prev_close = None
        try:
            if snap is not None:
                latest_trade = getattr(snap, "latest_trade", None)
                if latest_trade is not None:
                    last = float(getattr(latest_trade, "price", 0) or 0)
                daily_bar = getattr(snap, "daily_bar", None)
                prev_daily_bar = getattr(snap, "previous_daily_bar", None)
                if prev_daily_bar is not None:
                    prev_close = float(getattr(prev_daily_bar, "close", 0) or 0)
                if last is None and daily_bar is not None:
                    last = float(getattr(daily_bar, "close", 0) or 0)
        except Exception:
            pass

        if sym == "VIX" and (last is None or last <= 0):
            out["indices"].append({
                "symbol": "VIX",
                "last": None,
                "change": None,
                "change_pct": None,
                "available": False,
            })
            continue

        change = None
        change_pct = None
        if last and prev_close:
            change = round(last - prev_close, 4)
            change_pct = round((last - prev_close) / prev_close * 100.0, 4)

        out["indices"].append({
            "symbol": sym,
            "last": last,
            "change": change,
            "change_pct": change_pct,
            "available": last is not None,
        })

    try:
        from datetime import datetime as _dt, timezone as _tz
        out["as_of"] = _dt.now(_tz.utc).isoformat()
    except Exception:
        out["as_of"] = ""
    return out


async def h_executor(request):
    """GET /api/executor/{name} — per-executor tab data, cached 15s."""
    from aiohttp import web
    if not _check_auth(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    name = (request.match_info.get("name") or "").strip().lower()
    if name not in ("val", "gene"):
        return web.json_response(
            {"enabled": False, "error": f"unknown executor {name!r}"}, status=200,
        )
    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(None, _executor_snapshot, name)
    return web.json_response(payload)


async def h_indices(request):
    """GET /api/indices — SPY/QQQ/DIA/IWM/VIX ticker strip, cached 30s."""
    from aiohttp import web
    if not _check_auth(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    now = time.time()
    with _indices_cache_lock:
        ts = _indices_cache.get("ts", 0.0)
        payload = _indices_cache.get("payload")
        if payload is not None and (now - ts) < _INDICES_CACHE_TTL:
            return web.json_response(payload)
    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(None, _fetch_indices)
    with _indices_cache_lock:
        _indices_cache["ts"] = now
        _indices_cache["payload"] = payload
    return web.json_response(payload)


async def h_trade_log(request):
    """v3.4.27 — persistent trade-log reader.

    Query params:
      limit      int, default 500 (max 5000)
      since      YYYY-MM-DD, optional
      portfolio  "paper" | "tp", optional
    """
    from aiohttp import web
    if not _check_auth(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        limit = int(request.query.get("limit", "500"))
    except (TypeError, ValueError):
        limit = 500
    if limit < 1:
        limit = 1
    if limit > 5000:
        limit = 5000
    since = request.query.get("since") or None
    portfolio = request.query.get("portfolio") or None
    if portfolio and portfolio not in ("paper", "tp"):
        return web.json_response(
            {"ok": False, "error": "portfolio must be 'paper' or 'tp'"},
            status=400,
        )
    # Resolve live bot module without re-executing the entry point.
    m = _ssm()
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(
        None,
        lambda: m.trade_log_read_tail(
            limit=limit, since_date=since, portfolio=portfolio,
        ),
    )
    return web.json_response({
        "ok": True,
        "count": len(rows),
        "schema_version": getattr(m, "TRADE_LOG_SCHEMA_VERSION", 1),
        "rows": rows,
        "last_error": getattr(m, "_trade_log_last_error", None),
    })


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
# Thread entrypoint — started from trade_genius.py
# ─────────────────────────────────────────────────────────────
def _build_app():
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/", h_root)
    app.router.add_post("/login", h_login)
    app.router.add_post("/logout", h_logout)
    app.router.add_get("/api/state", h_state)
    app.router.add_get("/api/trade_log", h_trade_log)
    # v4.0.0-beta — per-executor tabs + index ticker strip.
    app.router.add_get("/api/executor/{name}", h_executor)
    app.router.add_get("/api/indices", h_indices)
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
    global _PW, _SESSION_SECRET
    _PW = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if not _PW:
        logger.info("Dashboard disabled: DASHBOARD_PASSWORD not set")
        return False
    if len(_PW) < 8:
        logger.warning(
            "Dashboard disabled: DASHBOARD_PASSWORD must be at least 8 characters"
        )
        return False
    try:
        port = int(os.getenv("DASHBOARD_PORT", "8080"))
    except ValueError:
        port = 8080
    # Session secret resolution order (v3.4.29 — persistent across deploys):
    #   1. DASHBOARD_SESSION_SECRET env override (tests / forced rotation).
    #   2. dashboard_secret.key on the Railway volume — same directory as
    #      paper_state.json, so it inherits the volume's persistence
    #      automatically. Read if present.
    #   3. Generate 32 random bytes and persist them for next boot.
    # Fail-closed: if step 3 cannot write (disk full, RO mount), the server
    # still starts with the in-memory secret. Sessions then behave exactly
    # as they did pre-3.4.29 for this boot only.
    _SESSION_SECRET = _load_or_create_session_secret()

    _install_log_handler()

    t = threading.Thread(target=_run_forever, args=(port,),
                         name="dashboard-http", daemon=True)
    t.start()
    logger.info("Dashboard thread started (port=%d)", port)
    return True
