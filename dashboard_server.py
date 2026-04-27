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
import html
import json
import logging
import os
import re
import secrets
import struct
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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


# v4.11.0 \u2014 wrap error_state.snapshot so a (theoretical) failure here
# can never tank the whole /api/state or /api/executor response.
def _errors_snapshot_safe(executor: str) -> dict:
    try:
        import error_state as _es
        return _es.snapshot(executor)
    except Exception:
        return {"executor": executor, "count": 0, "severity": "green", "entries": []}


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


def _safe_float(v):
    """Coerce a position-dict field to float, returning None if
    conversion fails. A single bad entry on disk (e.g. ``trail_high:
    "N/A"``) must not explode the whole snapshot."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _serialize_positions(longs: dict, shorts: dict, prices: dict) -> list[dict]:
    rows: list[dict] = []
    for tkr, p in longs.items():
        px = prices.get(tkr) or p.get("entry_price")
        entry = _safe_float(p.get("entry_price")) or 0.0
        shares = int(p.get("shares", 0) or 0)
        px_f = _safe_float(px)
        unreal = (px_f - entry) * shares if px_f is not None else 0.0
        # v3.4.26: expose trail state + effective stop so the UI can
        # show what's actually managing the position (hard stop vs
        # armed trail). effective_stop mirrors the exit-decision rule
        # in manage_positions. v4.0.9 \u2014 _safe_float guards every
        # numeric read so a malformed on-disk value only drops that
        # one position's trail info instead of the whole snapshot.
        hard_stop = _safe_float(p.get("stop")) or 0.0
        trail_active = bool(p.get("trail_active", False))
        trail_stop = _safe_float(p.get("trail_stop"))
        trail_anchor = _safe_float(p.get("trail_high"))
        effective_stop = trail_stop if (trail_active and trail_stop is not None) else hard_stop
        rows.append({
            "ticker": tkr,
            "side": "LONG",
            "shares": shares,
            "entry": entry,
            "mark": px_f if px_f is not None else entry,
            "stop": hard_stop,
            "trail_active": trail_active,
            "trail_stop": trail_stop,
            "trail_anchor": trail_anchor,
            "effective_stop": effective_stop,
            "unrealized": unreal,
            "entry_time": p.get("entry_time", ""),
            "entry_count": int(p.get("entry_count", 1) or 1),
        })
    for tkr, p in shorts.items():
        px = prices.get(tkr) or p.get("entry_price")
        entry = _safe_float(p.get("entry_price")) or 0.0
        shares = int(p.get("shares", 0) or 0)
        px_f = _safe_float(px)
        unreal = (entry - px_f) * shares if px_f is not None else 0.0
        hard_stop = _safe_float(p.get("stop")) or 0.0
        trail_active = bool(p.get("trail_active", False))
        trail_stop = _safe_float(p.get("trail_stop"))
        trail_anchor = _safe_float(p.get("trail_low"))
        effective_stop = trail_stop if (trail_active and trail_stop is not None) else hard_stop
        rows.append({
            "ticker": tkr,
            "side": "SHORT",
            "shares": shares,
            "entry": entry,
            "mark": px_f if px_f is not None else entry,
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
    """Build today's trade list for the dashboard.

    Storage asymmetry (mirrors the invariant documented in
    ``trade_genius.py`` ~L2530): long BUYs / SELLs live in
    ``paper_trades``; short COVERs live in ``short_trade_history``.
    If that invariant is ever violated (future bug, replayed state, a
    migration that dual-writes) a short cover would appear in BOTH
    lists and the UI would show it twice. v4.1.7-dash \u2014 defensively
    de-duplicate by (ticker, time/entry_time, side, action) before
    returning, so a cross-list dupe is collapsed to a single row.
    """
    m = _ssm()
    out: list[dict] = []
    seen: set = set()

    def _key(t: dict, side: str) -> tuple:
        # Prefer the field each list actually carries; fall back
        # through both so the key is stable no matter which list the
        # row originated from.
        time_key = (
            t.get("time")
            or t.get("entry_time")
            or t.get("exit_time")
            or ""
        )
        return (
            (t.get("ticker") or "").upper(),
            str(time_key),
            side,
            t.get("action") or "",
        )

    for t in list(getattr(m, "paper_trades", []) or []):
        side = t.get("side", "LONG")
        k = _key(t, side)
        if k in seen:
            continue
        seen.add(k)
        out.append({**t, "side": side, "portfolio": "paper"})

    # also include today's shorts from short_trade_history filtered by date
    try:
        today = m._now_et().strftime("%Y-%m-%d")
    except Exception:
        today = ""
    for t in list(getattr(m, "short_trade_history", []) or []):
        if t.get("date") != today:
            continue
        k = _key(t, "SHORT")
        if k in seen:
            continue
        seen.add(k)
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
    v4.0.3-beta \u2014 includes or_stale_skip_count so silent OR drift
    failures are visible without tailing Railway logs.
    """
    snap = dict(getattr(m, "_gate_snapshot", {}) or {})
    skip_counts = dict(getattr(m, "or_stale_skip_count", {}) or {})
    rows = []
    for t in tickers:
        g = snap.get(t) or {}
        ext = g.get("extension_pct")
        if isinstance(ext, float):
            ext = round(ext, 2)
        rows.append({
            "ticker": t,
            "side": g.get("side"),
            "break": g.get("break"),
            "polarity": g.get("polarity"),
            "index": g.get("index"),
            "di": g.get("di"),
            "ts": g.get("ts"),
            "or_stale_skip_count": int(skip_counts.get(t, 0)),
            "extension_pct": ext,
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
        logger.debug("_next_scan_seconds failed", exc_info=True)
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
                logger.warning("sovereign_regime_snapshot: SPY close helper failed", exc_info=True)
            try:
                qc = helper("QQQ")
                if isinstance(qc, (int, float)):
                    out["qqq_price"] = float(qc)
            except Exception:
                logger.warning("sovereign_regime_snapshot: QQQ close helper failed", exc_info=True)

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
                logger.warning("sovereign_regime_snapshot: long eject failed", exc_info=True)
                out["long_eject"] = False
            try:
                out["short_eject"] = bool(eject("short"))
            except Exception:
                logger.warning("sovereign_regime_snapshot: short eject failed", exc_info=True)
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


# v4.1.9-dash \u2014 M11: snapshot() is O(N_positions) Alpaca round-trips
# through fetch_1min_bars, and h_stream calls it every 2s per SSE
# client. For a tab open 12 hours that is ~21,600 blocking calls per
# client, scaling linearly with concurrent viewers. The data does not
# actually change meaningfully every 2s \u2014 the 2s cadence exists for
# log streaming, not state freshness. Cache the snapshot result for a
# short TTL and share it across every h_stream client. /api/state
# still hits snapshot() uncached so explicit polls / the Val-tab
# warmup see fresh data.
_SNAPSHOT_CACHE_TTL = 10.0  # seconds; conservative given 2s poll floor
_snapshot_cache_lock = threading.Lock()
_snapshot_cache_value: dict[str, Any] | None = None
_snapshot_cache_ts: float = 0.0


def _cached_snapshot() -> dict[str, Any]:
    """Return a recent snapshot, recomputing at most once per TTL.

    Thread-safe: snapshot() is invoked from aiohttp's default thread
    pool (``run_in_executor(None, ...)``), so multiple SSE clients can
    call this concurrently. The lock ensures exactly one rebuild per
    expiry window; the other callers wait briefly and then observe the
    freshly-stored value. When the cache is warm the lock is released
    almost immediately \u2014 no blocking Alpaca work happens under it.
    """
    global _snapshot_cache_value, _snapshot_cache_ts
    now = time.monotonic()
    # Fast-path: return the cached value without taking the lock if
    # it is still fresh. Reads of the two globals are atomic in
    # CPython for dict/float refs; a stale read here just means we
    # serialize through the lock on the next call.
    cached = _snapshot_cache_value
    if cached is not None and (now - _snapshot_cache_ts) < _SNAPSHOT_CACHE_TTL:
        return cached
    with _snapshot_cache_lock:
        # Double-check after acquiring the lock \u2014 another thread may
        # have refreshed the cache while we were waiting.
        now = time.monotonic()
        if (
            _snapshot_cache_value is not None
            and (now - _snapshot_cache_ts) < _SNAPSHOT_CACHE_TTL
        ):
            return _snapshot_cache_value
        fresh = snapshot()
        _snapshot_cache_value = fresh
        _snapshot_cache_ts = time.monotonic()
        return fresh


# v5.2.0 \u2014 ordered list of shadow configs as they appear on the
# dashboard panel. The first 5 mirror volume_profile.SHADOW_CONFIGS;
# REHUNT_VOL_CONFIRM and OOMPH_ALERT are the v5.1.9 additions.
_SHADOW_PANEL_ORDER = (
    ("TICKER+QQQ", "TICKER+QQQ (70/100)"),
    ("TICKER_ONLY", "TICKER_ONLY (70)"),
    ("QQQ_ONLY", "QQQ_ONLY (100)"),
    ("GEMINI_A", "GEMINI_A (110/85)"),
    ("BUCKET_FILL_100", "BUCKET_FILL_100"),
    ("REHUNT_VOL_CONFIRM", "REHUNT_VOL_CONFIRM"),
    ("OOMPH_ALERT", "OOMPH_ALERT"),
)


def _shadow_pnl_snapshot(
    m, today: str, today_realized: float, today_unrealized: float,
) -> dict[str, Any]:
    """Build the dashboard payload for the bottom shadow-strategy panel.

    Returns:
      { "configs": [ {name, label, today: {...}, cumulative: {...}}, ... ],
        "paper_bot": {label, today, cumulative},
        "best_today": "TICKER+QQQ" | None,
        "worst_today": "BUCKET_FILL_100" | None }
    """
    configs: list[dict[str, Any]] = []
    tr = None
    try:
        import shadow_pnl as _sp
        tr = _sp.tracker()
        summary = tr.summary(today_str=today or None)
    except Exception as e:
        logger.warning("shadow_pnl summary failed: %s", e)
        summary = {}
    for name, label in _SHADOW_PANEL_ORDER:
        s = summary.get(name) or {}
        n_today = int(s.get("today_n_trades", 0) or 0)
        wins_today = int(s.get("today_wins", 0) or 0)
        wr_today = (wins_today / n_today * 100.0) if n_today else None
        n_cum = int(s.get("cumulative_n_trades", 0) or 0)
        wins_cum = int(s.get("cumulative_wins", 0) or 0)
        wr_cum = (wins_cum / n_cum * 100.0) if n_cum else None
        # v5.3.0 \u2014 per-config detail for the Shadow tab. Safe
        # against tracker errors (tr is None / bad call) so the panel
        # still renders without detail.
        open_positions: list[dict[str, Any]] = []
        recent_trades: list[dict[str, Any]] = []
        if tr is not None:
            try:
                open_positions = tr.open_positions_for(name)
            except Exception as e:
                logger.warning(
                    "shadow_pnl open_positions_for(%s) failed: %s", name, e)
            try:
                recent_trades = tr.recent_closed_for(name, limit=10)
            except Exception as e:
                logger.warning(
                    "shadow_pnl recent_closed_for(%s) failed: %s", name, e)
        configs.append({
            "name": name,
            "label": label,
            "today": {
                "n": n_today,
                "wr": round(wr_today, 1) if wr_today is not None else None,
                "realized": float(s.get("today_realized", 0.0) or 0.0),
                "unrealized": float(s.get("today_unrealized", 0.0) or 0.0),
                "total": float(s.get("today_total", 0.0) or 0.0),
            },
            "cumulative": {
                "n": n_cum,
                "wr": round(wr_cum, 1) if wr_cum is not None else None,
                "realized": float(
                    s.get("cumulative_realized", 0.0) or 0.0),
                "unrealized": float(
                    s.get("cumulative_unrealized", 0.0) or 0.0),
                "total": float(s.get("cumulative_total", 0.0) or 0.0),
            },
            # v5.3.0 \u2014 expandable detail payload.
            "open_positions": open_positions,
            "recent_trades": recent_trades,
        })

    # Best / worst by today_total (only counts configs with at least
    # one trade today \u2014 zero-trade configs render as "--").
    active = [c for c in configs if c["today"]["n"] > 0]
    best_today = max(
        active, key=lambda c: c["today"]["total"], default=None,
    )
    worst_today = min(
        active, key=lambda c: c["today"]["total"], default=None,
    )

    # Paper bot comparison row \u2014 mirrors the same paper portfolio
    # whose equity now drives shadow sizing, so the row is a true
    # apples-to-apples comparison vs the per-config rollups above.
    # Source: paper_trades (long SELLs) + short_trade_history (short
    # COVERs), date-filtered to today.
    paper_today_n = 0
    paper_today_wins = 0
    today_paper_pnl = 0.0
    for t in (getattr(m, "paper_trades", []) or []):
        if t.get("date") == today and t.get("action") == "SELL":
            paper_today_n += 1
            pnl = float(t.get("pnl", 0.0) or 0.0)
            today_paper_pnl += pnl
            if pnl > 0:
                paper_today_wins += 1
    for t in (getattr(m, "short_trade_history", []) or []):
        if t.get("date") == today:
            paper_today_n += 1
            pnl = float(t.get("pnl", 0.0) or 0.0)
            today_paper_pnl += pnl
            if pnl > 0:
                paper_today_wins += 1
    paper_wr = (paper_today_wins / paper_today_n * 100.0) if paper_today_n else None
    paper_total_today = today_paper_pnl + float(today_unrealized or 0.0)
    paper_bot = {
        "label": "PAPER BOT",
        "today": {
            "n": paper_today_n,
            "wr": round(paper_wr, 1) if paper_wr is not None else None,
            "realized": round(today_paper_pnl, 2),
            "unrealized": round(float(today_unrealized or 0.0), 2),
            "total": round(paper_total_today, 2),
        },
        "cumulative": {
            # The paper cumulative tracker lives in paper_state and is
            # not summed here \u2014 we surface today's paper for a
            # direct shadow vs paper comparison and let the rest of the
            # dashboard cover all-time paper equity.
            "n": paper_today_n,
            "wr": round(paper_wr, 1) if paper_wr is not None else None,
            "realized": round(today_paper_pnl, 2),
            "unrealized": round(float(today_unrealized or 0.0), 2),
            "total": round(paper_total_today, 2),
        },
    }

    return {
        "configs": configs,
        "paper_bot": paper_bot,
        "best_today": best_today["name"] if best_today else None,
        "worst_today": worst_today["name"] if worst_today else None,
    }


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
        # v4.4.1 — union of user-pause (/pause) and the auto-idle state
        # set by scan_loop when it short-circuits outside market hours.
        # Before v4.4.1 this only reflected _scan_paused, so the UI said
        # "ACTIVE" all night even though no scanning was happening.
        scan_paused = bool(
            getattr(m, "_scan_paused", False)
            or getattr(m, "_scan_idle_hours", False)
        )
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
            # v4.11.0 \u2014 health-pill snapshot for the Main tab. Embedded
            # directly so the pill updates on every SSE / state poll
            # tick without a separate /api/errors round-trip.
            "errors": _errors_snapshot_safe("main"),
            # v5.2.0 \u2014 shadow strategy P&L block for the bottom panel.
            # Every config row + a PAPER_BOT comparison row built from
            # the same paper trades / unrealized totals shown above
            # (the paper book is also what drives shadow sizing).
            "shadow_pnl": _shadow_pnl_snapshot(
                m, today, realized, unreal_sum,
            ),
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
            if len(b) >= 32:
                return b[:32]
            logger.warning(
                "DASHBOARD_SESSION_SECRET too short (%d bytes, need \u2265 32) \u2014 ignoring env",
                len(b),
            )
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

# Redact Alpaca key/secret fragments from error bodies before they
# land in the ring-buffer log viewer. Alpaca keys are prefixed PK...
# (live) or AK... / CK... (test). Secrets don't share a fixed prefix,
# but any 16+ char run of [A-Za-z0-9] inside an Alpaca error body
# next to "key" / "secret" / "Bearer" is suspect.
# v4.0.9 \u2014 allow mixed-case suffixes. The original regex only
# matched uppercase alnum, so a key like `PKabcd1234...` leaked
# through. Alpaca keys are mixed-case in practice.
_ALPACA_KEY_RE = re.compile(r"\b(?:PK|AK|CK|SK)[A-Za-z0-9]{10,}\b")


def _redact_alpaca_secrets(s: str) -> str:
    if not s:
        return s
    return _ALPACA_KEY_RE.sub("[REDACTED]", s)

# Login rate-limiter: per-IP attempt timestamps (sliding window)
_LOGIN_WINDOW_SEC = 60
_LOGIN_MAX_ATTEMPTS = 5
_login_attempts: dict = defaultdict(list)
_login_attempts_lock = threading.Lock()


def _client_ip(request) -> str:
    """Best-effort client IP for the login rate-limiter.

    X-Forwarded-For is only trusted when DASHBOARD_TRUST_PROXY=1 (set this
    when you are actually behind a trusted reverse proxy like Railway). By
    default we use the peer address so a direct-to-app attacker can't
    rotate XFF to bypass the 5-attempt lock."""
    if os.getenv("DASHBOARD_TRUST_PROXY", "").strip() == "1":
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
    err_html = f'<div class="err">{html.escape(error)}</div>' if error else ""
    return """<!doctype html><html><head><meta charset="utf-8"><title>TradeGenius \u2014 sign in</title>
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
    <span>TradeGenius</span>
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
    # Login-CSRF / session-fixation hardening: require Origin (or Referer)
    # to match the Host the request came in on. samesite="Lax" on its own
    # still permits top-level form POSTs from foreign origins. Without
    # this check, an off-site form could pin a victim's browser to a
    # password the attacker knows.
    host = (request.headers.get("Host") or "").strip().lower()
    origin = (request.headers.get("Origin") or "").strip()
    referer = (request.headers.get("Referer") or "").strip()
    if host:
        def _host_of(url):
            if not url:
                return ""
            try:
                from urllib.parse import urlparse
                return (urlparse(url).netloc or "").lower()
            except Exception:
                return ""
        src_host = _host_of(origin) or _host_of(referer)
        if src_host and src_host != host:
            logger.warning(
                "dashboard /login cross-origin POST rejected host=%s src=%s ip=%s",
                host, src_host, ip,
            )
            return web.Response(
                text="Cross-origin login request rejected.",
                status=403, content_type="text/plain",
            )
    data = await request.post()
    # v4.0.8 \u2014 coerce to str before strip(). A multipart POST smuggling
    # `password` as a file part returns a FileField here, and .strip()
    # on that raises AttributeError \u2014 which would have surfaced as a
    # 500 instead of a clean 401.
    raw_pw = data.get("password")
    pw = "" if raw_pw is None else str(raw_pw).strip()
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
        httponly=True, samesite="Strict", secure=True,
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


# v4.11.0 \u2014 health-pill error endpoint. Returns the per-executor
# today-only error state used by the dashboard pill. Same session-cookie
# auth as the rest of the dashboard. Read-only and cheap, so no rate
# limit. The pill itself reads errors from /api/state and
# /api/executor/{name}; this endpoint exists for the tap-to-expand
# dropdown so opening the dropdown does not need a full state rebuild.
async def h_errors(request):
    from aiohttp import web
    if not _check_auth(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    name = (request.match_info.get("executor") or "").strip().lower()
    if name not in ("main", "val", "gene"):
        return web.json_response(
            {"ok": False, "error": f"unknown executor {name!r}"}, status=400,
        )
    try:
        import error_state as _es
        snap = _es.snapshot(name)
    except Exception as e:
        logger.exception("h_errors: snapshot(%s) failed", name)
        return web.json_response(
            {"ok": False, "error": f"snapshot failed: {e}"}, status=500,
        )
    return web.json_response(snap)


# v5.4.1 \u2014 /api/shadow_charts: equity curves, day-heatmap, rolling
# win-rate sparklines, sourced from the persisted shadow_positions table
# (closed trades only). Cached for 30s to avoid hammering SQLite when
# multiple browsers poll the Shadow tab in parallel.
_SHADOW_CHARTS_CACHE_TTL = 30.0
_shadow_charts_cache: dict = {"ts": 0.0, "payload": None}
_shadow_charts_cache_lock = threading.Lock()


def _shadow_charts_payload() -> dict:
    """Build the /api/shadow_charts response from shadow_positions.

    Returns a dict shaped like:
      { "configs": { <name>: {equity_curve, daily_pnl, win_rate_rolling}, \u2026 },
        "as_of": "<utc iso>" }

    Closed trades only (exit_ts_utc IS NOT NULL). One entry per
    SHADOW_CONFIG name from _SHADOW_PANEL_ORDER so the response always
    carries all 7 configs even when some have no trades yet.
    """
    cfg_names = [n for (n, _label) in _SHADOW_PANEL_ORDER]
    out: dict[str, dict] = {n: {
        "equity_curve": [],
        "daily_pnl": [],
        "win_rate_rolling": [],
    } for n in cfg_names}
    rows: list[dict] = []
    try:
        import persistence as _p
        # Lexical compare on ISO-8601 strings is correct for UTC, so a
        # very early sentinel pulls every closed row.
        all_rows = _p.load_shadow_positions_since("0000-01-01T00:00:00+00:00")
        rows = [r for r in all_rows if r.get("exit_ts_utc")]
    except Exception:
        logger.exception("shadow_charts: load_shadow_positions_since failed")
        rows = []
    by_cfg: dict[str, list[dict]] = {n: [] for n in cfg_names}
    for r in rows:
        n = r.get("config_name")
        if n in by_cfg:
            by_cfg[n].append(r)
    for n in cfg_names:
        cfg_rows = sorted(
            by_cfg[n],
            key=lambda r: (r.get("exit_ts_utc") or ""),
        )
        cum = 0.0
        equity_curve: list[dict] = []
        daily: dict[str, dict] = {}
        wr_rolling: list[dict] = []
        wins_window: list[int] = []
        for idx, r in enumerate(cfg_rows, start=1):
            pnl = float(r.get("realized_pnl") or 0.0)
            cum += pnl
            ts = r.get("exit_ts_utc") or ""
            equity_curve.append({"ts": ts, "cum_pnl": round(cum, 2)})
            day = ts[:10] if len(ts) >= 10 else ts
            d = daily.setdefault(day, {"date": day, "pnl": 0.0, "trades": 0})
            d["pnl"] = round(d["pnl"] + pnl, 2)
            d["trades"] += 1
            wins_window.append(1 if pnl > 0 else 0)
            if len(wins_window) > 20:
                wins_window.pop(0)
            if idx >= 20:
                wr = sum(wins_window) / 20.0
                wr_rolling.append({
                    "trade_idx": idx,
                    "win_rate": round(wr, 4),
                })
        out[n]["equity_curve"] = equity_curve
        out[n]["daily_pnl"] = sorted(daily.values(), key=lambda d: d["date"])
        out[n]["win_rate_rolling"] = wr_rolling
    from datetime import datetime as _dt, timezone as _tz
    return {
        "configs": out,
        "as_of": _dt.now(_tz.utc).isoformat().replace("+00:00", "Z"),
    }


async def h_shadow_charts(request):
    """GET /api/shadow_charts \u2014 chart-ready shadow strategy data.

    Returns a JSON document with equity curves, daily P&L, and rolling
    20-trade win rates per SHADOW_CONFIG. Cached for
    _SHADOW_CHARTS_CACHE_TTL seconds since the underlying SQLite table
    only changes when a shadow position closes.
    """
    from aiohttp import web
    if not _check_auth(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    now = time.time()
    with _shadow_charts_cache_lock:
        ts = _shadow_charts_cache.get("ts", 0.0)
        payload = _shadow_charts_cache.get("payload")
        if payload is not None and (now - ts) < _SHADOW_CHARTS_CACHE_TTL:
            return web.json_response(payload)
    loop = asyncio.get_running_loop()
    try:
        payload = await loop.run_in_executor(None, _shadow_charts_payload)
    except Exception as e:
        logger.exception("h_shadow_charts: payload build failed")
        return web.json_response(
            {"ok": False, "error": f"payload failed: {e}"}, status=500,
        )
    with _shadow_charts_cache_lock:
        _shadow_charts_cache["ts"] = now
        _shadow_charts_cache["payload"] = payload
    return web.json_response(payload)


# v4.9.1: unauthenticated endpoint so the post-deploy GHA poller can
# confirm Railway has rolled out the new BOT_VERSION without holding a
# session cookie. Version is not sensitive; everything else still
# requires login.
async def h_version(request):
    from aiohttp import web
    try:
        m = _ssm()
        version = str(getattr(m, "BOT_VERSION", "?"))
    except Exception:
        version = "?"
    return web.json_response({"version": version})


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
        return {
            "enabled": False,
            "error": f"{name} executor not enabled",
            # v4.11.0 \u2014 still report the empty health-pill snapshot so
            # the UI does not need a separate handler for the disabled case.
            "errors": _errors_snapshot_safe(name),
        }

    mode = executor.mode
    cache_key = (name, mode)
    now = time.time()
    with _executor_cache_lock:
        ent = _executor_cache.get(cache_key)
        if ent and (now - ent[0]) < _EXECUTOR_CACHE_TTL:
            cached = dict(ent[1])
            # v4.11.0 \u2014 always overlay a fresh errors snapshot so the
            # pill count tracks live state instead of the 15s cache.
            cached["errors"] = _errors_snapshot_safe(name)
            return cached

    payload: dict = {
        "enabled": True,
        "mode": mode,
        "healthy": False,
        "account": None,
        "positions": [],
        "todays_trades": [],
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
        payload["errors"] = _errors_snapshot_safe(name)
        with _executor_cache_lock:
            _executor_cache[cache_key] = (now, payload)
        return payload

    if client is None:
        payload["error"] = "alpaca client unavailable (missing keys?)"
        payload["errors"] = _errors_snapshot_safe(name)
        with _executor_cache_lock:
            _executor_cache[cache_key] = (now, payload)
        return payload

    # Diagnostic: expose the Alpaca base URL the client is actually using,
    # so we can tell whether a stale ALPACA_ENDPOINT_PAPER env var on Railway
    # is pointing at a wrong host / missing /v2 prefix and causing 404s.
    try:
        _base_url = None
        for _attr in ("_base_url", "base_url", "_url", "_trading_url"):
            _val = getattr(client, _attr, None)
            if _val:
                _base_url = str(_val)
                break
        if _base_url:
            payload["alpaca_base_url"] = _base_url
    except Exception:
        pass

    try:
        acct = client.get_account()
        # v4.0.4 \u2014 include last_equity (equity at prior trading close
        # per Alpaca) so the front-end can compute Day P&L the same way
        # Main does (equity - last_equity). Alpaca exposes this as a
        # top-level field on the Account object.
        def _as_float(obj, attr):
            v = getattr(obj, attr, None)
            if v is None or v == "":
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        equity = _as_float(acct, "equity")
        last_equity = _as_float(acct, "last_equity")
        day_pnl = None
        if equity is not None and last_equity is not None:
            day_pnl = equity - last_equity
        payload["account"] = {
            "cash": _as_float(acct, "cash"),
            "buying_power": _as_float(acct, "buying_power"),
            "equity": equity,
            "last_equity": last_equity,
            "day_pnl": day_pnl,
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

        # Today's trades \u2014 filled Alpaca orders dated today in ET,
        # shaped to match Main's Today's Trades row template (action,
        # ticker, price, shares, cost, time, date; + pnl/pnl_pct for SELL)
        # so the same frontend template renders on Val/Gene tabs.
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            m = _ssm()
            # Build the `after` filter from real ET midnight, not UTC
            # midnight of the ET date string. Between 00:00-05:00 ET the
            # ET date and UTC date differ, so the prior naive build
            # dropped today's trades for the first few hours of the day.
            after_dt = None
            et_tz = None
            try:
                from datetime import datetime as _dt, timezone as _tz, time as _tm
                now_et = m._now_et()
                et_tz = now_et.tzinfo
                et_midnight = _dt.combine(now_et.date(), _tm(0, 0), tzinfo=et_tz)
                after_dt = et_midnight.astimezone(_tz.utc)
                today_et = now_et.strftime("%Y-%m-%d")
            except Exception:
                from datetime import datetime as _dt2, timezone as _tz2
                today_et = _dt2.now(_tz2.utc).strftime("%Y-%m-%d")
                try:
                    after_dt = _dt2.strptime(today_et, "%Y-%m-%d").replace(tzinfo=_tz2.utc)
                except Exception:
                    after_dt = None
            try:
                req = GetOrdersRequest(
                    status=QueryOrderStatus.CLOSED, limit=500,
                    **({"after": after_dt} if after_dt else {}),
                )
                orders = client.get_orders(filter=req) or []
            except Exception:
                orders = []
            trades_out = []
            for o in orders:
                try:
                    filled_at = getattr(o, "filled_at", None)
                    if filled_at is None:
                        continue
                    # Alpaca returns tz-aware UTC timestamps. Convert to
                    # the ET day for comparison with `today_et` so orders
                    # filled after 20:00 ET aren't shunted to "tomorrow"
                    # and orders filled 00:00-05:00 ET aren't dropped as
                    # "yesterday".
                    try:
                        fa_et = filled_at.astimezone(et_tz) if (et_tz is not None and hasattr(filled_at, "astimezone")) else filled_at
                        fdate = fa_et.strftime("%Y-%m-%d")
                        ftime = fa_et.strftime("%H:%M")
                        fiso = fa_et.isoformat()
                    except Exception:
                        try:
                            fdate = filled_at.strftime("%Y-%m-%d")
                            ftime = filled_at.strftime("%H:%M")
                            fiso = filled_at.isoformat()
                        except Exception:
                            fdate = str(filled_at)[:10]
                            ftime = str(filled_at)[11:16]
                            fiso = str(filled_at)
                    if fdate != today_et:
                        continue
                    side_raw = getattr(getattr(o, "side", None), "value", "") or ""
                    side_str = str(side_raw).lower()
                    action = "BUY" if side_str == "buy" else ("SELL" if side_str == "sell" else side_str.upper())
                    sym = str(getattr(o, "symbol", "") or "")
                    qty = float(getattr(o, "filled_qty", 0) or getattr(o, "qty", 0) or 0)
                    fap = getattr(o, "filled_avg_price", None)
                    try:
                        price = float(fap) if fap is not None else None
                    except Exception:
                        price = None
                    cost = (qty * price) if (price is not None) else None
                    trades_out.append({
                        "action": action,
                        "ticker": sym,
                        "symbol": sym,
                        "side": "LONG",
                        "shares": qty,
                        "qty": qty,
                        "price": price,
                        "avg_fill_price": price,
                        "cost": cost,
                        "time": ftime,
                        "filled_at": fiso,
                        "date": fdate,
                    })
                except Exception:
                    continue
            trades_out.sort(key=lambda t: t.get("filled_at", ""))
            payload["todays_trades"] = trades_out
        except Exception as te:
            logger.warning("executor %s todays_trades fetch failed: %s", name, te)
            payload["todays_trades"] = []

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
            err_msg = _redact_alpaca_secrets(str(e) or "(no message)")
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
                extras.append(f"body={_redact_alpaca_secrets(str(raw_body)[:200])!r}")
            except Exception:
                pass
        resp = inst.get("response") or inst.get("_response")
        if resp is not None and "body=" not in " ".join(extras):
            body = getattr(resp, "text", None)
            if body:
                try:
                    extras.append(f"body={_redact_alpaca_secrets(str(body)[:200])!r}")
                except Exception:
                    pass
        extra_str = (" " + " ".join(extras)) if extras else ""
        payload["error"] = f"alpaca fetch failed: {err_type}: {err_msg}{extra_str}"
        logger.warning(
            "executor %s alpaca fetch failed: %s: %s%s",
            name, err_type, err_msg, extra_str,
        )

    # v4.11.0 \u2014 attach health-pill snapshot to the per-executor
    # response. Lives outside the cache check so the count refreshes
    # within the 15s cache TTL.
    payload["errors"] = _errors_snapshot_safe(name)

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


# v4.13.0 \u2014 Yahoo Finance v8/chart helper for cash indices and index
# futures. Alpaca's equity feed does not carry index symbols (^GSPC, ^IXIC,
# ^DJI, ^RUT, ^VIX) or futures (ES=F, NQ=F, YM=F, RTY=F), which is why VIX
# always rendered n/a in v4.12.0. The chart endpoint is keyless, returns
# JSON, and tolerates a single-symbol-per-request shape \u2014 we batch by
# issuing N parallel requests inside a thread pool so the whole helper
# completes inside the same 30s indices cache window without serializing.
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}
_YAHOO_TIMEOUT = 6  # seconds per symbol; whole batch budget ~6s parallel

# Cash index labels are cosmetic on the wire \u2014 the frontend prefers
# display_label over the raw caret symbol for a friendlier ticker. Symbols
# without an entry here fall back to the bare symbol minus the leading caret.
_YAHOO_INDEX_LABELS = {
    "^GSPC": "S&P 500",
    "^IXIC": "Nasdaq",
    "^DJI":  "Dow",
    "^RUT":  "Russell 2K",
    "^VIX":  "VIX",
}

# Cash-index \u2192 front-month-future mapping. The futures number rides as
# an inline badge on the matching cash row (per Val's spec) instead of
# getting its own line, so we never show ES=F as a standalone item.
_YAHOO_INDEX_FUTURE = {
    "^GSPC": ("ES=F", "ES"),
    "^IXIC": ("NQ=F", "NQ"),
    "^DJI":  ("YM=F", "YM"),
    "^RUT":  ("RTY=F", "RTY"),
    # ^VIX has no liquid front-month future on this surface (VX=F is on
    # CFE, not the same nearest-month convention) \u2014 deliberately omitted.
}

_YAHOO_CASH_SYMBOLS    = ["^GSPC", "^IXIC", "^DJI", "^RUT", "^VIX"]
_YAHOO_FUTURES_SYMBOLS = ["ES=F", "NQ=F", "YM=F", "RTY=F"]


def _fetch_yahoo_quote_one(symbol: str) -> dict | None:
    """Fetch one symbol from Yahoo's v8 chart endpoint. Never raises.

    Returns ``{"last": float, "prev_close": float}`` on success, or None on
    any failure (network error, HTTP non-200, malformed JSON, missing
    fields). The caller decides what to do with None \u2014 v4.13.0 treats a
    None result as "hide that row" rather than a hard error so a single
    flaky symbol never blacks out the whole strip.
    """
    try:
        enc = urllib.parse.quote(symbol, safe="")
        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/%s"
            "?interval=1m&range=1d&includePrePost=true" % enc
        )
        req = urllib.request.Request(url, headers=_YAHOO_HEADERS)
        with urllib.request.urlopen(req, timeout=_YAHOO_TIMEOUT) as resp:
            data = json.loads(resp.read())
        results = (data or {}).get("chart", {}).get("result") or []
        if not results:
            return None
        meta = results[0].get("meta") or {}
        last = meta.get("regularMarketPrice")
        prev = meta.get("previousClose")
        if prev is None:
            prev = meta.get("chartPreviousClose")
        if last is None or prev is None:
            return None
        return {"last": float(last), "prev_close": float(prev)}
    except Exception:
        return None


def _fetch_yahoo_quotes(symbols: list[str]) -> dict:
    """Fetch many symbols from Yahoo in parallel. Never raises.

    Returns a dict ``{symbol: {"last", "prev_close"}}`` keyed by the input
    symbol string (caret/equals preserved). Symbols whose individual fetch
    failed are simply absent from the dict \u2014 the caller checks ``in``
    before reading. We use a small thread pool sized to the request count
    so a slow tail symbol does not block the rest, but cap at 8 to play
    nicely with shared HTTP infrastructure.
    """
    if not symbols:
        return {}
    out: dict = {}
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as ex:
            for sym, res in zip(symbols, ex.map(_fetch_yahoo_quote_one, symbols)):
                if res is not None:
                    out[sym] = res
    except Exception:
        # Pool blew up before any worker ran (very rare) \u2014 fall back to
        # serial so we still return whatever we can collect.
        for sym in symbols:
            res = _fetch_yahoo_quote_one(sym)
            if res is not None:
                out[sym] = res
    return out


def _classify_session_et() -> str:
    """Return one of 'rth' | 'pre' | 'post' | 'closed' based on now-in-ET.

    v4.12.0 - used by the index ticker strip so the UI can label after-hours
    quotes and compute AH change against the right base close. Pure clock
    classification: weekday 04:00-09:30 ET = pre, 09:30-16:00 = rth,
    16:00-20:00 = post, otherwise closed. We deliberately do NOT consult a
    holiday calendar here; on a holiday the snapshot's daily_bar will simply
    not have updated and the frontend reads 'closed' which is correct.
    """
    try:
        from datetime import datetime as _dt, timezone as _tz
        try:
            from zoneinfo import ZoneInfo
            now_et = _dt.now(_tz.utc).astimezone(ZoneInfo("America/New_York"))
        except Exception:
            # Fallback: rough EST offset; AH labelling is cosmetic so this
            # never blocks the ticker.
            now_et = _dt.now(_tz.utc)
        weekday = now_et.weekday()  # 0=Mon ... 6=Sun
        if weekday >= 5:
            return "closed"
        minutes = now_et.hour * 60 + now_et.minute
        if 4 * 60 <= minutes < 9 * 60 + 30:
            return "pre"
        if 9 * 60 + 30 <= minutes < 16 * 60:
            return "rth"
        if 16 * 60 <= minutes < 20 * 60:
            return "post"
        return "closed"
    except Exception:
        return "rth"  # safe default - no AH labelling


def _fetch_indices() -> dict:
    """Build the index ticker strip payload. Never raises."""
    symbols = ["SPY", "QQQ", "DIA", "IWM", "VIX"]
    session = _classify_session_et()
    out = {
        "ok": True,
        "as_of": "",
        "session": session,  # v4.12.0
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
    # index (not an equity), so Alpaca's equity feed likely refuses it.
    # VIX is requested separately only so we can tag its placeholder row
    # with an explicit reason distinct from a genuine zero/missing quote
    # on a real equity \u2014 see the VIX-specific branch below.
    equity_symbols = [s for s in symbols if s != "VIX"]
    snapshots: dict = {}
    try:
        resp = client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=equity_symbols))
        snapshots = resp if isinstance(resp, dict) else {}
    except Exception as e:
        out["error"] = f"snapshot failed: {e}"
        snapshots = {}

    for sym in symbols:
        # VIX is an index, not in equity_symbols \u2014 it never appears in
        # the snapshot response. Emit a sentinel row tagged with
        # reason="vix_no_equity_feed" so the frontend can distinguish
        # "intentional placeholder" from "real equity with weird quote".
        if sym == "VIX":
            out["indices"].append({
                "symbol": "VIX",
                "last": None,
                "change": None,
                "change_pct": None,
                "available": False,
                "reason": "vix_no_equity_feed",
                "ah": False,
            })
            continue

        snap = snapshots.get(sym)
        last = None
        prev_close = None
        today_close = None  # v4.12.0 - today's RTH close (post-16:00 ET)
        try:
            if snap is not None:
                latest_trade = getattr(snap, "latest_trade", None)
                if latest_trade is not None:
                    raw_price = getattr(latest_trade, "price", None)
                    if raw_price is not None:
                        last = float(raw_price)
                daily_bar = getattr(snap, "daily_bar", None)
                prev_daily_bar = getattr(snap, "previous_daily_bar", None)
                if prev_daily_bar is not None:
                    raw_prev = getattr(prev_daily_bar, "close", None)
                    if raw_prev is not None:
                        prev_close = float(raw_prev)
                if daily_bar is not None:
                    raw_close = getattr(daily_bar, "close", None)
                    if raw_close is not None:
                        today_close = float(raw_close)
                if last is None and today_close is not None:
                    last = today_close
        except Exception:
            pass

        # v4.12.0 - regular-session change vs prior-day close. Same as
        # before: this is what the trader watches during RTH.
        change = None
        change_pct = None
        if last and last > 0 and prev_close and prev_close > 0:
            change = round(last - prev_close, 4)
            change_pct = round((last - prev_close) / prev_close * 100.0, 4)

        # v4.12.0 - after-hours layer. We tag a row as ah=True only when
        # we are NOT in the regular session AND we have a base close to
        # measure against. Base close picks today's RTH close if we have
        # one (typical post-16:00); otherwise prior-day close (typical
        # pre-market or weekend, when daily_bar may already be the
        # most-recent-trading-day close - we treat that as base too).
        ah_flag = False
        ah_change = None
        ah_change_pct = None
        if session in ("pre", "post", "closed") and last and last > 0:
            base = today_close if (today_close and today_close > 0) else prev_close
            # Only mark AH when last actually differs from the base; an
            # exact match means no extended-hours trade has printed yet
            # and the user would just see "+0.00 AH" which is noise.
            if base and base > 0 and abs(last - base) > 1e-6:
                ah_flag = True
                ah_change = round(last - base, 4)
                ah_change_pct = round((last - base) / base * 100.0, 4)

        # Real equity: show the row whenever we got a numeric last trade,
        # even if it is exactly 0 (pre-market quirk on a thin name).
        out["indices"].append({
            "symbol": sym,
            "last": last,
            "change": change,
            "change_pct": change_pct,
            "available": last is not None,
            "ah": ah_flag,
            "ah_change": ah_change,
            "ah_change_pct": ah_change_pct,
        })

    # v4.13.0 \u2014 append real cash indices + index futures via Yahoo. The
    # ETF rows above stay as-is so SPY/QQQ/DIA/IWM/VIX still scroll first;
    # then we add ^GSPC/^IXIC/^DJI/^RUT/^VIX with the matching front-month
    # future as an inline badge ([ES +0.40%]) on each cash row. We fetch
    # cash + futures in a single parallel batch so the 30s indices cache
    # absorbs the cost (one Yahoo call per cache miss, not per request).
    yahoo_ok = True
    yahoo_error = None
    try:
        all_yahoo = _YAHOO_CASH_SYMBOLS + _YAHOO_FUTURES_SYMBOLS
        quotes = _fetch_yahoo_quotes(all_yahoo)
        # Whole-batch failure (every single symbol came back None) is the
        # "data delayed" signal Val asked for: we keep the existing ETF
        # rows and tell the frontend to paint the dim notice. A partial
        # miss is normal (Yahoo sometimes returns 0 rows for ^RUT for a
        # second), so we only flip yahoo_ok when literally nothing came
        # back.
        if not quotes:
            yahoo_ok = False
            yahoo_error = "yahoo: no quotes returned"
        else:
            for cash_sym in _YAHOO_CASH_SYMBOLS:
                q = quotes.get(cash_sym)
                if not q:
                    # Per-symbol miss \u2014 skip the row. The strip already
                    # has ETF coverage (e.g. SPY for ^GSPC) so the user is
                    # not blind to the index, just missing one duplicate row.
                    continue
                last = q["last"]
                prev = q["prev_close"]
                change = None
                change_pct = None
                if last and last > 0 and prev and prev > 0:
                    change = round(last - prev, 4)
                    change_pct = round((last - prev) / prev * 100.0, 4)

                # Inline future badge: ES/NQ/YM/RTY pct vs the future's own
                # prev close. We deliberately use the future's own change
                # (not last-cash vs future-prev) so the badge tells the user
                # "futures are pricing the open at +X%" \u2014 which is the
                # whole reason to show futures in the first place. ^VIX has
                # no entry in _YAHOO_INDEX_FUTURE so its row simply has no
                # badge.
                future_payload = None
                fut_entry = _YAHOO_INDEX_FUTURE.get(cash_sym)
                if fut_entry:
                    fut_sym, fut_label = fut_entry
                    fq = quotes.get(fut_sym)
                    if fq and fq["prev_close"] and fq["prev_close"] > 0:
                        f_change_pct = round(
                            (fq["last"] - fq["prev_close"]) / fq["prev_close"] * 100.0,
                            4,
                        )
                        future_payload = {
                            "symbol": fut_sym,
                            "label": fut_label,
                            "change_pct": f_change_pct,
                        }

                out["indices"].append({
                    "symbol": cash_sym,
                    "display_label": _YAHOO_INDEX_LABELS.get(
                        cash_sym, cash_sym.lstrip("^")
                    ),
                    "last": last,
                    "change": change,
                    "change_pct": change_pct,
                    "available": True,
                    "source": "yahoo",
                    # AH layer N/A here \u2014 the chart endpoint's
                    # regularMarketPrice already reflects the most recent
                    # session close on a weekend, so the futures badge IS
                    # the after-hours signal for these rows.
                    "ah": False,
                    "ah_change": None,
                    "ah_change_pct": None,
                    "future": future_payload,
                })
    except Exception as e:
        yahoo_ok = False
        yahoo_error = f"yahoo helper raised: {e}"

    out["yahoo_ok"] = yahoo_ok
    if yahoo_error:
        out["yahoo_error"] = yahoo_error

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
    # v4.0.8 \u2014 TP surfaces were deleted in v3.5.0. Reject the stale
    # filter value with 400 instead of passing it through to the reader
    # (where behavior is undefined / format-dependent).
    if portfolio and portfolio not in ("paper",):
        return web.json_response(
            {"ok": False, "error": "portfolio must be 'paper' (tp removed in v3.5.0)"},
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
    loop = asyncio.get_running_loop()
    try:
        while True:
            # v4.1.9-dash \u2014 use the TTL cache so concurrent SSE clients
            # share one snapshot rebuild per 10s instead of each paying
            # ~O(N_positions) Alpaca round-trips every 2s. Client-visible
            # SSE cadence is unchanged (still 2s).
            #
            # v4.11.0 \u2014 the "logs" SSE event is gone along with the
            # dashboard log tail card. Errors fan out to the executor's
            # Telegram channel via report_error() instead.
            snap = await loop.run_in_executor(None, _cached_snapshot)
            payload = json.dumps({"t": "state", "data": snap})
            await resp.write(f"event: state\ndata: {payload}\n\n".encode("utf-8"))

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
    app.router.add_get("/api/shadow_charts", h_shadow_charts)
    app.router.add_get("/api/version", h_version)
    app.router.add_get("/api/trade_log", h_trade_log)
    # v4.0.0-beta — per-executor tabs + index ticker strip.
    app.router.add_get("/api/executor/{name}", h_executor)
    app.router.add_get("/api/indices", h_indices)
    # v4.11.0 \u2014 health-pill tap-to-expand endpoint.
    app.router.add_get("/api/errors/{executor}", h_errors)
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

    t = threading.Thread(target=_run_forever, args=(port,),
                         name="dashboard-http", daemon=True)
    t.start()
    logger.info("Dashboard thread started (port=%d)", port)
    return True
