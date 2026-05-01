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


def _chandelier_stage(pos: dict) -> int:
    """Read Alarm-F chandelier stage from pos["trail_state"].

    Stage codes (engine.alarm_f_trail.TrailState):
        0 INACTIVE \u2014 trail not armed yet
        1 BREAKEVEN \u2014 BE installed, trail engaged
        2 CHANDELIER_WIDE \u2014 active trailing
        3 CHANDELIER_TIGHT \u2014 last 30min tight trailing

    v6.0.6 \u2014 added so the dashboard TRAIL badge can fire on
    Alarm-F-driven trails (which never set the legacy trail_active
    flag because Alarm F overwrites pos["stop"] directly via the
    Sentinel pipeline). Returns 0 when trail_state missing or
    malformed; never raises."""
    try:
        ts = pos.get("trail_state") if pos else None
        if ts is None:
            return 0
        return int(getattr(ts, "stage", 0) or 0)
    except Exception:
        return 0


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
        # v6.0.6 \u2014 expose Alarm-F chandelier stage so the UI TRAIL
        # badge fires when the chandelier has armed (stage >= 1) even
        # when the legacy trail_active flag is False. Alarm F mutates
        # pos["stop"] directly via the Sentinel pipeline rather than
        # going through the legacy trail_stop path.
        chandelier_stage = _chandelier_stage(p)
        # v5.10.6 \u2014 surface phase / Sovereign Brake distance / Entry-2
        # fired flag on every position row so the dashboard does not
        # need a second join against the per_position_v510 map.
        phase_v510 = str(p.get("phase") or "A").upper()
        if phase_v510 not in ("A", "B", "C"):
            phase_v510 = "A"
        sb_distance = (unreal + 500.0) if isinstance(unreal, (int, float)) else None
        rows.append(
            {
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
                "chandelier_stage": chandelier_stage,
                "unrealized": unreal,
                "entry_time": p.get("entry_time", ""),
                "entry_count": int(p.get("entry_count", 1) or 1),
                "phase": phase_v510,
                "sovereign_brake_distance_dollars": sb_distance,
                "entry_2_fired": bool(p.get("v5104_entry2_fired")),
            }
        )
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
        chandelier_stage = _chandelier_stage(p)
        phase_v510 = str(p.get("phase") or "A").upper()
        if phase_v510 not in ("A", "B", "C"):
            phase_v510 = "A"
        sb_distance = (unreal + 500.0) if isinstance(unreal, (int, float)) else None
        rows.append(
            {
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
                "chandelier_stage": chandelier_stage,
                "unrealized": unreal,
                "entry_time": p.get("entry_time", ""),
                "entry_count": 1,
                "phase": phase_v510,
                "sovereign_brake_distance_dollars": sb_distance,
                "entry_2_fired": bool(p.get("v5104_entry2_fired")),
            }
        )
    return rows


def _today_trades() -> list[dict]:
    """Build today's trade list for the dashboard.

    Storage asymmetry (mirrors the invariant documented in
    ``trade_genius.py`` ~L2530): long BUYs / SELLs live in
    ``paper_trades``; short COVERs live in ``short_trade_history``.
    Short ENTRIES are intentionally NOT persisted to any trade list \u2014
    ``short_trade_history`` is the single source of truth and avoids
    double-counting on /trades. If that invariant is ever violated
    (future bug, replayed state, a migration that dual-writes) a short
    cover would appear in BOTH lists and the UI would show it twice.
    v4.1.7-dash \u2014 defensively de-duplicate by (ticker, time/entry_time,
    side, action) before returning.

    v5.5.8 \u2014 SHORT entry-row synthesis. The Main tab needs to render
    *two* rows per closed short (entry + exit) just like longs do, so
    for each row in ``short_trade_history`` we emit BOTH a synthesized
    ``action="SHORT"`` entry row built from the cover's ``entry_*``
    fields AND the existing COVER row. Open shorts (live entries in
    ``short_positions`` with no cover yet, dated today) also get a
    synthesized SHORT entry row so the Main tab shows the open leg
    instead of nothing. No storage change \u2014 the synthesis is purely
    a read-side transform.
    """
    m = _ssm()
    out: list[dict] = []
    seen: set = set()

    def _key(t: dict, side: str) -> tuple:
        # Prefer the field each list actually carries; fall back
        # through both so the key is stable no matter which list the
        # row originated from.
        time_key = t.get("time") or t.get("entry_time") or t.get("exit_time") or ""
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

    # v5.5.8: track which (ticker, entry_time) pairs we've synthesized a
    # SHORT entry row for, so the open-short sweep below doesn't duplicate
    # an entry that already came from a cover.
    short_entries_emitted: set = set()

    for t in list(getattr(m, "short_trade_history", []) or []):
        if t.get("date") != today:
            continue
        # v5.5.8 \u2014 synthesize the SHORT entry row from the cover's
        # entry_* fields. Time field uses cover.entry_time so the
        # default sort places the entry before the cover.
        try:
            entry_price = float(t.get("entry_price") or 0.0)
            shares_n = int(t.get("shares") or 0)
        except (TypeError, ValueError):
            entry_price = 0.0
            shares_n = 0
        entry_time_val = t.get("entry_time") or ""
        synth_entry = {
            "action": "SHORT",
            "ticker": t.get("ticker"),
            "side": "SHORT",
            "shares": shares_n,
            "price": entry_price,
            "entry_price": entry_price,
            "time": entry_time_val,
            "entry_time": entry_time_val,
            "entry_time_iso": t.get("entry_time_iso"),
            "entry_num": t.get("entry_num", 1),
            "date": t.get("date"),
            "cost": round(shares_n * entry_price, 2),
            "portfolio": "paper",
        }
        k_entry = _key(synth_entry, "SHORT")
        if k_entry not in seen:
            seen.add(k_entry)
            out.append(synth_entry)
            short_entries_emitted.add(((t.get("ticker") or "").upper(), str(entry_time_val)))

        # The existing COVER row (unchanged shape).
        k_cover = _key(t, "SHORT")
        if k_cover in seen:
            continue
        seen.add(k_cover)
        out.append({**t, "side": "SHORT", "portfolio": "paper"})

    # v5.5.8 \u2014 sweep live short_positions for OPEN shorts (entered today,
    # no cover yet). Long-side analog: open longs surface via positions
    # snapshot in _live_positions(); for trades_today we mirror the
    # paired-row shape so the Main tab shows the open leg.
    for tkr, pos in (getattr(m, "short_positions", {}) or {}).items():
        if not isinstance(pos, dict):
            continue
        if pos.get("date") != today:
            continue
        try:
            entry_price = float(pos.get("entry_price") or 0.0)
            shares_n = int(pos.get("shares") or 0)
        except (TypeError, ValueError):
            entry_price = 0.0
            shares_n = 0
        # short_positions stores entry_time as "HH:MM:SS"; the cover
        # records it as "HH:MM CDT". Normalize live-pos to the cover's
        # format (using the ISO-precise entry_ts_utc when present) so an
        # open-then-cover sequence does not double-emit.
        entry_iso = pos.get("entry_ts_utc") or ""
        entry_time_raw = pos.get("entry_time") or ""
        entry_time_val = entry_time_raw
        try:
            if entry_iso:
                entry_time_val = m._to_cdt_hhmm(entry_iso)
            elif entry_time_raw and ":" in entry_time_raw:
                # Fallback for legacy "HH:MM:SS" with no ISO companion.
                entry_time_val = entry_time_raw[:5] + " CDT"
        except Exception:
            entry_time_val = entry_time_raw
        dedup_key = ((tkr or "").upper(), str(entry_time_val))
        if dedup_key in short_entries_emitted:
            continue
        synth_open = {
            "action": "SHORT",
            "ticker": tkr,
            "side": "SHORT",
            "shares": shares_n,
            "price": entry_price,
            "entry_price": entry_price,
            "time": entry_time_val,
            "entry_time": entry_time_val,
            "entry_time_iso": pos.get("entry_ts_utc"),
            "entry_num": pos.get("entry_count", 1),
            "date": today,
            "cost": round(shares_n * entry_price, 2),
            "portfolio": "paper",
        }
        k_open = _key(synth_open, "SHORT")
        if k_open in seen:
            continue
        seen.add(k_open)
        out.append(synth_open)
        short_entries_emitted.add(dedup_key)

    # sort by time if present. For close actions (SELL / COVER) the
    # canonical timestamp is the exit_time; opens (BUY / SHORT) carry it
    # as entry_time. Falling back through ``time`` first preserves any
    # row that already set the unified field.
    def _sort_key(x: dict) -> str:
        if x.get("time"):
            return str(x["time"])
        action = (x.get("action") or "").upper()
        if action in ("SELL", "COVER"):
            return str(x.get("exit_time") or x.get("entry_time") or "")
        return str(x.get("entry_time") or x.get("exit_time") or "")

    out.sort(key=_sort_key)
    return out


def _proximity_rows() -> list[dict]:
    """Distance to the entry-relevant breakout boundary per ticker.

    Tiger Sovereign: only OR-high (long permit) and OR-low (short permit)
    are entry-relevant. PDC is not part of entry proximity.
    """
    m = _ssm()
    rows: list[dict] = []
    try:
        tickers = list(getattr(m, "TRADE_TICKERS", []) or [])
    except Exception:
        tickers = []
    open_longs = set(getattr(m, "positions", {}) or {})
    open_shorts = set(getattr(m, "short_positions", {}) or {})
    long_permit = False
    short_permit = False
    try:
        import v5_10_6_snapshot as _v510_snap

        sip = _v510_snap._section_i_permit(m) or {}
        long_permit = bool(sip.get("long_open"))
        short_permit = bool(sip.get("short_open"))
    except Exception:
        pass
    if long_permit and short_permit:
        permit_side = "BOTH"
    elif long_permit:
        permit_side = "LONG"
    elif short_permit:
        permit_side = "SHORT"
    else:
        permit_side = "NONE"
    for t in tickers:
        px = _price_for(t)
        orh = (getattr(m, "or_high", {}) or {}).get(t)
        orl = (getattr(m, "or_low", {}) or {}).get(t)
        best_pct = None
        best_label = ""
        if px and px > 0:
            candidates: list[tuple[str, float]] = []
            if long_permit and orh:
                candidates.append(("OR-high", orh))
            if short_permit and orl:
                candidates.append(("OR-low", orl))
            if not candidates:
                # No permit active \u2014 still report whichever boundary is
                # closer so the operator sees how far we are, but mark
                # the row "no permit" via permit_side="NONE".
                if orh:
                    candidates.append(("OR-high", orh))
                if orl:
                    candidates.append(("OR-low", orl))
            for label, lvl in candidates:
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
        rows.append(
            {
                "ticker": t,
                "price": px,
                "or_high": orh,
                "or_low": orl,
                "nearest_label": best_label,
                "nearest_pct": best_pct,  # smaller = closer
                "open_side": open_side,
                "permit_side": permit_side,
            }
        )
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
        rows.append(
            {
                "ticker": t,
                "side": g.get("side"),
                "break": g.get("break"),
                "polarity": g.get("polarity"),
                "index": g.get("index"),
                "di": g.get("di"),
                "ts": g.get("ts"),
                "or_stale_skip_count": int(skip_counts.get(t, 0)),
                "extension_pct": ext,
            }
        )
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
    """v3.4.29 \u2014 SPY/QQQ vs PDC display snapshot for the dashboard.

    Originally fed the live Sovereign Regime Shield (PDC eject) UI;
    that rule was retired in v5.9.1. The function now powers the
    cosmetic SPY/QQQ vs PDC pills only \u2014 it does NOT drive any
    algo decision and the long_eject/short_eject fields are gone.

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
          "status":         str,           # DISARMED | AWAITING | NO_PDC
          "reason":         str,           # short human explanation
        }

    v5.9.1: long_eject / short_eject fields removed along with the
    _sovereign_regime_eject() rule. PDC pills remain for cosmetic
    display only \u2014 they have no algo impact.

    Never raises. Returns a fully-populated dict with None/False
    fields if any data is missing, so the UI always has a stable
    shape.
    """
    out: dict[str, Any] = {
        "spy_price": None,
        "spy_pdc": None,
        "spy_delta_pct": None,
        "spy_above_pdc": None,
        "qqq_price": None,
        "qqq_pdc": None,
        "qqq_delta_pct": None,
        "qqq_above_pdc": None,
        "status": "NO_PDC",
        "reason": "",
    }
    try:
        pdc_map = getattr(m, "pdc", {}) or {}
        spy_pdc = pdc_map.get("SPY")
        qqq_pdc = pdc_map.get("QQQ")
        if isinstance(spy_pdc, (int, float)) and spy_pdc > 0:
            out["spy_pdc"] = float(spy_pdc)
        if isinstance(qqq_pdc, (int, float)) and qqq_pdc > 0:
            out["qqq_pdc"] = float(qqq_pdc)

        # Last finalized 1m close (cosmetic display only post-v5.9.1).
        # Pull directly from fetch_1min_bars now that the previous
        # _last_finalized_1min_close helper has been retired.
        fetch_bars = getattr(m, "fetch_1min_bars", None)

        def _last_close(ticker):
            if not callable(fetch_bars):
                return None
            try:
                bars = fetch_bars(ticker)
            except Exception:
                logger.warning(
                    "sovereign_regime_snapshot: %s bars fetch failed",
                    ticker,
                    exc_info=True,
                )
                return None
            if not bars:
                return None
            closes = [c for c in (bars.get("closes") or []) if c is not None]
            if len(closes) < 2:
                return None
            return float(closes[-2])

        sc = _last_close("SPY")
        if sc is not None:
            out["spy_price"] = sc
        qc = _last_close("QQQ")
        if qc is not None:
            out["qqq_price"] = qc

        # Deltas (only if both price and PDC present).
        if out["spy_price"] is not None and out["spy_pdc"]:
            out["spy_delta_pct"] = (out["spy_price"] - out["spy_pdc"]) / out["spy_pdc"] * 100.0
            out["spy_above_pdc"] = out["spy_price"] > out["spy_pdc"]
        if out["qqq_price"] is not None and out["qqq_pdc"]:
            out["qqq_delta_pct"] = (out["qqq_price"] - out["qqq_pdc"]) / out["qqq_pdc"] * 100.0
            out["qqq_above_pdc"] = out["qqq_price"] > out["qqq_pdc"]

        # Human-readable status (cosmetic post-v5.9.1; no eject is wired).
        if out["spy_pdc"] is None or out["qqq_pdc"] is None:
            out["status"] = "NO_PDC"
            out["reason"] = "PDC not yet collected (pre-open)"
        elif out["spy_price"] is None or out["qqq_price"] is None:
            out["status"] = "AWAITING"
            out["reason"] = "waiting for first finalized 1m close"
        else:
            out["status"] = "DISARMED"
            out["reason"] = "PDC eject retired in v5.9.1 \u2014 informational only"
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
        if _snapshot_cache_value is not None and (now - _snapshot_cache_ts) < _SNAPSHOT_CACHE_TTL:
            return _snapshot_cache_value
        fresh = snapshot()
        _snapshot_cache_value = fresh
        _snapshot_cache_ts = time.monotonic()
        return fresh


# (Shadow strategy panel retired).


def _executors_status_snapshot(m) -> dict:
    """v5.25.0 \u2014 small per-executor enabled/mode block for /api/state.

    Returns ``{"val": {"enabled": bool, "mode": str|None},
    "gene": {...}}``. Used by the dashboard header chips so an
    operator can see at a glance which Alpaca executors are wired
    up. Never raises \u2014 any attribute lookup error degrades to
    ``enabled=False, mode=None``.

    "enabled" mirrors the bootstrap contract: an executor is
    "enabled" iff its bootstrap returned a non-None instance, which
    means ``<PREFIX>_ENABLED`` was truthy AND
    ``<PREFIX>_ALPACA_PAPER_KEY`` was set. A None instance means
    either the operator turned the executor off or the keys are
    missing on Railway \u2014 both render as a dimmed chip.
    """
    out: dict = {}
    for name, attr in (("val", "val_executor"), ("gene", "gene_executor")):
        try:
            inst = getattr(m, attr, None)
        except Exception:
            inst = None
        if inst is None:
            out[name] = {"enabled": False, "mode": None}
            continue
        try:
            mode = str(getattr(inst, "mode", "") or "") or None
        except Exception:
            mode = None
        out[name] = {"enabled": True, "mode": mode}
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
        for t in getattr(m, "paper_trades", []) or []:
            if t.get("date") == today and t.get("action") == "SELL":
                realized += float(t.get("pnl", 0.0) or 0.0)
        for t in getattr(m, "short_trade_history", []) or []:
            if t.get("date") == today:
                realized += float(t.get("pnl", 0.0) or 0.0)

        unreal_sum = 0.0
        for row in _serialize_positions(longs, shorts, prices):
            unreal_sum += row["unrealized"]
        day_pnl = realized + unreal_sum

        # v3.4.29 — Sovereign Regime Shield live state for the dashboard.
        sovereign = _sovereign_regime_snapshot(m)

        # Regime / observer
        # v5.31.2 \u2014 session label is now computed from real ET time.
        # The legacy MarketMode classifier was retired in v5.26.0 but the
        # dashboard kept reading the dead _current_mode global, which was
        # frozen at "CLOSED" forever. The KPI showed CLOSED during RTH.
        # Mirror engine/scan.py's window logic so PRE / RTH OPEN / POWER /
        # AFTER / CLOSED reflect the actual session, with weekends and
        # the standard OR / power / wind-down windows respected.
        try:
            _now_et_session = m._now_et()
            _hh, _mm = _now_et_session.hour, _now_et_session.minute
            _is_weekend = _now_et_session.weekday() >= 5
            if _is_weekend:
                mode, mode_reason = "CLOSED", "weekend"
            elif _hh < 4:
                mode, mode_reason = "CLOSED", "overnight"
            elif _hh < 8:
                mode, mode_reason = "PRE", "early premarket"
            elif _hh < 9 or (_hh == 9 and _mm < 30):
                mode, mode_reason = "PRE", "premarket warm-up"
            elif _hh == 9 and _mm < 35:
                mode, mode_reason = "OR", "opening range (09:30-09:35 ET)"
            elif _hh < 15 or (_hh == 15 and _mm < 30):
                mode, mode_reason = "OPEN", "RTH open"
            elif _hh == 15 and _mm < 55:
                mode, mode_reason = "POWER", "power hour wind-down"
            elif _hh < 16:
                mode, mode_reason = "POWER", "final 5 min - no new entries"
            elif _hh < 20:
                mode, mode_reason = "AFTER", "after-hours session"
            else:
                mode, mode_reason = "CLOSED", "after-hours closed"
        except Exception:
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
            getattr(m, "_scan_paused", False) or getattr(m, "_scan_idle_hours", False)
        )
        or_date = str(getattr(m, "or_collected_date", ""))

        # ticker_pnl / red list
        ticker_pnl = dict(getattr(m, "_current_ticker_pnl", {}) or {})
        ticker_red = list(getattr(m, "_current_ticker_red", []) or [])

        version = str(getattr(m, "BOT_VERSION", "?"))

        # v5.10.6 \u2014 Eye-of-the-Tiger live state snapshot. Surfaces
        # Section I permit, per-ticker Volume Bucket + Boundary Hold,
        # and per-position phase + Sovereign Brake distance so the
        # operator can see what the v5.10 algo is actually running.
        # Defensive: on internal error returns empty blocks so the rest
        # of /api/state still serializes.
        try:
            import v5_10_6_snapshot as _v510_snap

            v510_block = _v510_snap.build_v510_snapshot(
                m,
                tickers,
                longs,
                shorts,
                prices,
            )
        except Exception:
            logger.exception("v5.10.6 snapshot build failed")
            v510_block = {
                "section_i_permit": {},
                "per_ticker_v510": {},
                "per_position_v510": {},
            }

        # v5.13.2 \u2014 Tiger Sovereign Phase 1\u20134 snapshot for the rewritten
        # dashboard panel. Sits alongside the v5.10.6 fields above; the
        # dashboard reads `tiger_sovereign` directly, while the legacy
        # fields stay for backward compatibility.
        try:
            import v5_13_2_snapshot as _ts_snap

            tiger_sovereign_block = _ts_snap.build_tiger_sovereign_snapshot(
                m,
                tickers,
                longs,
                shorts,
                prices,
            )
        except Exception:
            logger.exception("v5.13.2 tiger_sovereign snapshot build failed")
            tiger_sovereign_block = {
                "phase1": {},
                "phase2": [],
                "phase3": [],
                "phase4": [],
            }

        # v5.13.2 \u2014 runtime feature-flag indicators. Dashboard surfaces
        # these as small ON/OFF pills below the KPI row so the operator
        # can see at-a-glance which spec rules are currently overridden
        # via env vars.
        # v5.29.0 \u2014 also surface alarm_{c,d,e}_enabled so the dashboard
        # can hide bypassed components in the Permit Matrix (volume column /
        # card, sentinel-strip cells for Alarms C / D / E). Sourced from
        # engine.sentinel module-level flags (ALARM_C_ENABLED etc.). The
        # legacy engine.feature_flags shim was removed in v5.26.0; we keep
        # the volume_gate_enabled key for compatibility with the existing
        # KPI-row pill but read it from a hard default (False) when the
        # shim is gone, matching production behaviour since v5.13.1.
        # v5.30.0 \u2014 also surface alarm_f_enabled. Alarm F (chandelier
        # trail) has no module-level kill switch in engine.sentinel, so it
        # is unconditionally True whenever the sentinel module imports
        # successfully. The frontend uses this flag to render the F cell
        # in the sentinel strip the same way it conditionally renders
        # C / D / E.
        try:
            from engine import sentinel as _sen

            alarm_c_enabled = bool(getattr(_sen, "ALARM_C_ENABLED", False))
            alarm_d_enabled = bool(getattr(_sen, "ALARM_D_ENABLED", False))
            alarm_e_enabled = bool(getattr(_sen, "ALARM_E_ENABLED", False))
            # Alarm F has no ALARM_F_ENABLED toggle; True when the
            # module imports (i.e. the runtime knows what F is).
            alarm_f_enabled = True
        except Exception:
            alarm_c_enabled = False
            alarm_d_enabled = False
            alarm_e_enabled = False
            alarm_f_enabled = False
        try:
            from engine import feature_flags as _ff  # legacy shim, may be absent

            volume_gate_enabled = bool(getattr(_ff, "VOLUME_GATE_ENABLED", False))
        except Exception:
            volume_gate_enabled = False
        feature_flags_block = {
            "volume_gate_enabled": volume_gate_enabled,
            "alarm_c_enabled": alarm_c_enabled,
            "alarm_d_enabled": alarm_d_enabled,
            "alarm_e_enabled": alarm_e_enabled,
            "alarm_f_enabled": alarm_f_enabled,
        }

        # v5.5.7 \u2014 surface the paper book's most recent emitted
        # signal so the Main tab can render a LAST SIGNAL card with the
        # same shape the per-executor panels already use.
        try:
            last_signal = getattr(m, "last_signal", None)
        except Exception:
            last_signal = None

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
            # v5.5.7 \u2014 paper executor's most recent emitted signal
            # for the Main-tab LAST SIGNAL card. Same shape as the
            # per-executor payload.
            "last_signal": last_signal,
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
            # The underlying volume_profile WS feed is still required by the
            # live engine, so we keep a status indicator (just renamed).
            "volume_feed_status": (
                "live" if bool(getattr(m, "VOLUME_FEED_AVAILABLE", False)) else "disabled_no_creds"
            ),
            # v5.10.6 \u2014 Eye-of-the-Tiger live state surface for the
            # dashboard's v5.10 panel. See v5_10_6_snapshot.py for the
            # field schema. Kept for backward compatibility \u2014 the
            # rewritten v5.13.2 dashboard reads `tiger_sovereign` below.
            "section_i_permit": v510_block.get("section_i_permit", {}),
            "per_ticker_v510": v510_block.get("per_ticker_v510", {}),
            "per_position_v510": v510_block.get("per_position_v510", {}),
            # v5.13.2 \u2014 Tiger Sovereign Phase 1\u20134 snapshot for the
            # rewritten dashboard panel. See v5_13_2_snapshot.py for
            # the field schema.
            "tiger_sovereign": tiger_sovereign_block,
            # v5.13.2 \u2014 runtime feature-flag indicators (Volume Gate).
            # Operator visibility for env-var overrides. v5.13.10 retired
            # the Legacy Exits flag entirely (legacy paths removed).
            #
            "feature_flags": feature_flags_block,
            # v5.25.0 \u2014 enabled-exec chips for the dashboard header.
            # Reports which Alpaca-backed executors are wired up at boot.
            # Each entry: {enabled: bool, mode: str|None}. Disabled means
            # the executor's bootstrap returned None (missing PAPER_KEY,
            # explicit *_ENABLED=0, or build failure). Front-end renders
            # \u2713 / \u2014 chips next to the version pill so an operator
            # sees executor coverage at a glance without switching tabs.
            "executors_status": _executors_status_snapshot(m),
        }
    except Exception as e:
        logger.exception("dashboard snapshot failed: %s", e)
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────
# HTTP server
# ─────────────────────────────────────────────────────────────
SESSION_COOKIE = "spike_session"
# v5.19.3 \u2014 bumped from 7 to 90 days so a power user who only opens
# the dashboard a couple of times per month doesn't get bounced to /login
# every redeploy. The signing key is already persisted to disk via
# _load_or_create_session_secret, so a longer expiry is the only knob
# that controls how often Val has to re-enter the dashboard password.
SESSION_DAYS = 90

_PW: str = ""
_SESSION_SECRET: bytes = b""  # set at startup; see _load_or_create_session_secret


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
                path,
                len(data),
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
            path,
            e,
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
    loaded once at startup from a persistent file on the Railway volume
    (see _load_or_create_session_secret), so the cookie survives across
    container redeploys for the full SESSION_DAYS window. Tests can
    rotate it via the DASHBOARD_SESSION_SECRET env var.
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
    expected = hmac.new(_SESSION_SECRET, struct.pack(">Q", ts), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    age = time.time() - ts
    if age < -60:  # future-dated beyond clock-skew tolerance
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
        return web.Response(
            text="dashboard_static/index.html missing", status=500, content_type="text/plain"
        )
    # v5.28.3 \u2014 cache-bust /static/app.js and /static/app.css with the
    # current BOT_VERSION so a redeploy forces every browser to reload
    # the bundle. Without this, the browser holds onto a cached app.js
    # across deploys (Railway serves no Cache-Control header on static
    # assets but Fastly + browser heuristics still cache). The user hit
    # exactly this on the v5.28.1/v5.28.2 push: the live bundle had the
    # fix but the rendered dashboard was still running an older cached
    # copy. Cheap mitigation: rewrite the two asset references in the
    # served index.html with a ?v=<version> query string. Static files
    # themselves are unchanged so the on-disk path stays valid.
    try:
        _bv = str(getattr(_ssm(), "BOT_VERSION", "unknown"))
    except Exception:
        _bv = "unknown"
    html = idx.read_text(encoding="utf-8")
    html = html.replace('href="/static/app.css"', f'href="/static/app.css?v={_bv}"', 1)
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={_bv}"', 1)
    return web.Response(
        text=html,
        content_type="text/html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


async def h_login(request):
    from aiohttp import web

    ip = _client_ip(request)
    if not _rate_limit_check(ip):
        logger.warning("dashboard /login rate-limited ip=%s", ip)
        return web.Response(
            text="Too many attempts. Try again in 60 seconds.",
            status=429,
            content_type="text/plain",
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
                host,
                src_host,
                ip,
            )
            return web.Response(
                text="Cross-origin login request rejected.",
                status=403,
                content_type="text/plain",
            )
    data = await request.post()
    # v4.0.8 \u2014 coerce to str before strip(). A multipart POST smuggling
    # `password` as a file part returns a FileField here, and .strip()
    # on that raises AttributeError \u2014 which would have surfaced as a
    # 500 instead of a clean 401.
    raw_pw = data.get("password")
    pw = "" if raw_pw is None else str(raw_pw).strip()
    if not pw or not hmac.compare_digest(pw, _PW):
        return web.Response(
            text=_login_page("Invalid password"), content_type="text/html", status=401
        )
    # On success, issue a fresh timestamped token so each login starts a
    # new 7-day window. Cookie is Secure (Railway terminates TLS).
    token = _make_token()
    resp = web.HTTPFound("/")
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DAYS * 24 * 3600,
        httponly=True,
        samesite="Strict",
        secure=True,
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


# v5.5.5 \u2014 volume-feed WS observability surface. Mirrors the same
# session-cookie auth as /api/state. Returns the live WebsocketBarConsumer
# stats so an operator can discriminate "WS idle" from "handler error"
# without having to ssh in and grep logs.
async def h_ws_state(request):
    from aiohttp import web

    if not _check_auth(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        import trade_genius as _tg

        consumer = getattr(_tg, "_ws_consumer", None)
    except Exception as e:
        return web.json_response({"available": False, "error": f"{type(e).__name__}: {e}"})
    if consumer is None:
        return web.json_response({"available": False})
    try:
        snap = consumer.stats_snapshot()
    except Exception as e:
        return web.json_response({"available": False, "error": f"{type(e).__name__}: {e}"})
    snap["available"] = True
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
            {"ok": False, "error": f"unknown executor {name!r}"},
            status=400,
        )
    try:
        import error_state as _es

        snap = _es.snapshot(name)
    except Exception as e:
        logger.exception("h_errors: snapshot(%s) failed", name)
        return web.json_response(
            {"ok": False, "error": f"snapshot failed: {e}"},
            status=500,
        )
    return web.json_response(snap)


# (Shadow tab retired; underlying shadow_positions table dropped).


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

_executor_cache: dict = {}  # {(name, mode): (ts, payload)}
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
            rows.append(
                {
                    "symbol": str(getattr(p, "symbol", "") or ""),
                    "side": side,
                    "qty": abs(qty),
                    "avg_entry": avg_entry,
                    "current_price": cur,
                    "unrealized_pnl": unreal,
                    "unrealized_pnl_pct": unreal_pct,
                }
            )
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
                    status=QueryOrderStatus.CLOSED,
                    limit=500,
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
                        fa_et = (
                            filled_at.astimezone(et_tz)
                            if (et_tz is not None and hasattr(filled_at, "astimezone"))
                            else filled_at
                        )
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
                    action = (
                        "BUY"
                        if side_str == "buy"
                        else ("SELL" if side_str == "sell" else side_str.upper())
                    )
                    sym = str(getattr(o, "symbol", "") or "")
                    qty = float(getattr(o, "filled_qty", 0) or getattr(o, "qty", 0) or 0)
                    fap = getattr(o, "filled_avg_price", None)
                    try:
                        price = float(fap) if fap is not None else None
                    except Exception:
                        price = None
                    cost = (qty * price) if (price is not None) else None
                    trades_out.append(
                        {
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
                        }
                    )
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
            name,
            err_type,
            err_msg,
            extra_str,
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
    "^DJI": "Dow",
    "^RUT": "Russell 2K",
    "^VIX": "VIX",
}

# Cash-index \u2192 front-month-future mapping. The futures number rides as
# an inline badge on the matching cash row (per Val's spec) instead of
# getting its own line, so we never show ES=F as a standalone item.
_YAHOO_INDEX_FUTURE = {
    "^GSPC": ("ES=F", "ES"),
    "^IXIC": ("NQ=F", "NQ"),
    "^DJI": ("YM=F", "YM"),
    "^RUT": ("RTY=F", "RTY"),
    # ^VIX has no liquid front-month future on this surface (VX=F is on
    # CFE, not the same nearest-month convention) \u2014 deliberately omitted.
}

_YAHOO_CASH_SYMBOLS = ["^GSPC", "^IXIC", "^DJI", "^RUT", "^VIX"]
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
            out["indices"].append(
                {
                    "symbol": "VIX",
                    "last": None,
                    "change": None,
                    "change_pct": None,
                    "available": False,
                    "reason": "vix_no_equity_feed",
                    "ah": False,
                }
            )
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
        out["indices"].append(
            {
                "symbol": sym,
                "last": last,
                "change": change,
                "change_pct": change_pct,
                "available": last is not None,
                "ah": ah_flag,
                "ah_change": ah_change,
                "ah_change_pct": ah_change_pct,
            }
        )

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

                out["indices"].append(
                    {
                        "symbol": cash_sym,
                        "display_label": _YAHOO_INDEX_LABELS.get(cash_sym, cash_sym.lstrip("^")),
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
                    }
                )
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
            {"enabled": False, "error": f"unknown executor {name!r}"},
            status=200,
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
            limit=limit,
            since_date=since,
            portfolio=portfolio,
        ),
    )
    return web.json_response(
        {
            "ok": True,
            "count": len(rows),
            "schema_version": getattr(m, "TRADE_LOG_SCHEMA_VERSION", 1),
            "rows": rows,
            "last_error": getattr(m, "_trade_log_last_error", None),
        }
    )


# v5.23.0 \u2014 Intraday chart endpoint. Serves OHLC bars + key levels
# (OR high/low, AVWAP from 9:30 ET, 5m EMA9) and entry/exit markers for
# a single ticker so the dashboard can render an inline chart panel
# inside an expanded Permit Matrix Titan card. Read-only: no globals
# are mutated. Bars come from the on-disk JSONL archive that the WS
# feed writes (no live API call), so latency stays low and the dash
# does not contend with the live scan loop for the rate-limit budget.
_INTRADAY_BARS_DIR = os.getenv("BARS_DIR", "/data/bars")
_INTRADAY_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})")

# v5.23.3 \u2014 Extended-hours bar window: 7am CT \u2192 5pm CT, which is
# 8am ET \u2192 6pm ET. The on-disk archive only carries the live WS
# stream's RTH bars (09:30\u201316:00 ET), so we pull premarket and
# postmarket bars from Alpaca historical on demand. Process-local
# cache keyed by (ticker, day) prevents hammering Alpaca when a user
# expands several rows or refreshes the dashboard.
_INTRADAY_FETCH_CACHE: dict = {}
_INTRADAY_FETCH_TTL_S = 60.0
_INTRADAY_WINDOW_START_ET_MIN = 8 * 60  # 08:00 ET = 7:00 CT
_INTRADAY_WINDOW_END_ET_MIN = 18 * 60  # 18:00 ET = 17:00 CT


def _intraday_fetch_alpaca_bars(ticker: str, day: str) -> list[dict]:
    """Pull 1m bars 8am\u201318:00 ET for `ticker` on `day` from Alpaca.

    Returns dicts in the same shape as the on-disk archive
    ({open,high,low,close,iex_volume,ts}) so downstream helpers can
    consume the result without branching. Empty list on any failure
    (no creds, network error, etc.) so the caller can fall back to
    the on-disk archive without raising.
    """
    try:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import DataFeed
    except Exception:
        return []
    m = _ssm()
    client = None
    try:
        client = m._alpaca_data_client()
    except Exception:
        return []
    if client is None:
        return []
    try:
        et = ZoneInfo("America/New_York")
        # day is 'YYYY-MM-DD' in UTC sense; the chart window is keyed off
        # ET so we anchor the request to ET boundaries and let Alpaca
        # return UTC timestamps.
        d = datetime.strptime(day, "%Y-%m-%d")
        start_et = d.replace(hour=8, minute=0, tzinfo=et)
        end_et = d.replace(hour=18, minute=0, tzinfo=et) + timedelta(minutes=1)
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Minute,
            start=start_et,
            end=end_et,
            feed=DataFeed.IEX,
        )
        resp = client.get_stock_bars(req)
    except Exception as e:
        logger.debug("intraday alpaca fetch failed for %s: %s", ticker, e)
        return []
    raw = []
    try:
        if hasattr(resp, "data"):
            raw = resp.data.get(ticker.upper(), []) or resp.data.get(ticker, []) or []
    except Exception:
        raw = []
    out: list[dict] = []
    for b in raw:
        ts = getattr(b, "timestamp", None)
        if ts is None:
            continue
        try:
            ts_iso = (
                ts.strftime("%Y-%m-%dT%H:%M:%SZ")
                if ts.tzinfo is None
                else (
                    ts.astimezone(__import__("datetime").timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                )
            )
        except Exception:
            continue
        # v5.31.0 \u2014 capture Alpaca-only trade_count + bar_vwap fields.
        # The Bar object exposes them as attributes (raw API keys n / vw);
        # missing on Yahoo-sourced bars and harmless to include here.
        _tc = getattr(b, "trade_count", None)
        _vw = getattr(b, "vwap", None)
        out.append(
            {
                "ts": ts_iso,
                "open": float(getattr(b, "open", 0) or 0),
                "high": float(getattr(b, "high", 0) or 0),
                "low": float(getattr(b, "low", 0) or 0),
                "close": float(getattr(b, "close", 0) or 0),
                "iex_volume": int(getattr(b, "volume", 0) or 0),
                "trade_count": (int(_tc) if _tc is not None else None),
                "bar_vwap": (float(_vw) if _vw is not None else None),
            }
        )
    return out


def _intraday_load_today_bars(ticker: str, day: str) -> list[dict]:
    """Return 8am\u201318:00 ET 1m bars for `ticker` on `day`.

    Strategy:
    1. Try the live Alpaca historical fetcher (covers premarket +
       RTH + postmarket). Result cached in-process for 60s.
    2. Fall back to the on-disk JSONL archive (RTH only, written
       by the live WS bar archiver) if Alpaca returns empty.

    Empty list on total failure. Malformed entries silently skipped.
    """
    import time

    cache_key = (ticker.upper(), day)
    now = time.time()
    cached = _INTRADAY_FETCH_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _INTRADAY_FETCH_TTL_S:
        return cached[1]
    bars = _intraday_fetch_alpaca_bars(ticker, day)
    if not bars:
        try:
            from backtest.loader import load_bars

            bars = load_bars(_INTRADAY_BARS_DIR, day, ticker)
        except Exception:
            bars = []
    # Filter to only today (drop stale carry-overs from previous days
    # that the archiver leaves at the head of the file).
    bars = [b for b in bars if str(b.get("ts") or "").startswith(day)]
    _INTRADAY_FETCH_CACHE[cache_key] = (now, bars)
    return bars


def _intraday_et_minute(ts_iso: str) -> int | None:
    """Map a bar 'ts' (ISO UTC) to ET minute-of-day, or None on parse fail.

    DST-aware via zoneinfo. ET minute-of-day = hour*60 + minute. Used to
    bucket bars into premarket (4:00\u201309:30, mins 240\u2013570), RTH
    (09:30\u201316:00, mins 570\u2013960), and to anchor AVWAP at 09:30.
    """
    try:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        # Tolerate trailing Z and naive timestamps.
        s = ts_iso.rstrip("Z")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        et = dt.astimezone(ZoneInfo("America/New_York"))
        return et.hour * 60 + et.minute
    except Exception:
        return None


def _intraday_compute_avwap(bars: list[dict], anchor_min: int = 570) -> list[float | None]:
    """Anchored VWAP from 09:30 ET. Premarket bars get None (no anchor yet).

    Per-bar typical price = (h + l + c) / 3, weighted by iex_volume. The
    cumulative numerator/denominator reset at the anchor minute, mirroring
    how trade_genius's `_v513_compute_avwap_0930` builds the QQQ AVWAP.
    Bars before the anchor return None so the frontend can leave that
    portion of the line unplotted instead of drawing a misleading curve.

    v5.31.0 \u2014 ``anchor_min`` is now caller-tunable. Pass 480 for a
    premarket-anchored AVWAP starting at 08:00 ET.
    """
    out: list[float | None] = []
    pv_sum = 0.0
    v_sum = 0.0
    started = False
    for b in bars:
        et_min = _intraday_et_minute(str(b.get("ts") or ""))
        if et_min is None or et_min < anchor_min:
            out.append(None)
            continue
        try:
            h = float(b.get("high") or 0)
            lo = float(b.get("low") or 0)
            c = float(b.get("close") or 0)
            v = float(b.get("iex_volume") or 0)
        except (TypeError, ValueError):
            out.append(None)
            continue
        if not h or not lo or not c:
            out.append(out[-1] if out else None)
            continue
        tp = (h + lo + c) / 3.0
        if not started:
            pv_sum = 0.0
            v_sum = 0.0
            started = True
        pv_sum += tp * v
        v_sum += v
        out.append((pv_sum / v_sum) if v_sum > 0 else tp)
    return out


def _intraday_compute_avwap_band(
    bars: list[dict], anchor_min: int = 570
) -> tuple[list[float | None], list[float | None]]:
    """v5.31.0 \u2014 per-bar AVWAP \u00b11\u03c3 band aligned to ``_intraday_compute_avwap``.

    Running volume-weighted variance: the band sigma at bar i is the
    sqrt of the weighted variance of typical-price about the running
    AVWAP through bar i. Returns parallel ``(hi, lo)`` lists matching
    the bars list 1-to-1, with ``None`` entries for bars before the
    anchor (mirroring ``_intraday_compute_avwap``).
    """
    hi: list[float | None] = []
    lo: list[float | None] = []
    pv_sum = 0.0
    v_sum = 0.0
    pv2_sum = 0.0  # \u03a3 v * tp\u00b2
    started = False
    for b in bars:
        et_min = _intraday_et_minute(str(b.get("ts") or ""))
        if et_min is None or et_min < anchor_min:
            hi.append(None)
            lo.append(None)
            continue
        try:
            h_v = float(b.get("high") or 0)
            l_v = float(b.get("low") or 0)
            c_v = float(b.get("close") or 0)
            v_v = float(b.get("iex_volume") or 0)
        except (TypeError, ValueError):
            hi.append(None)
            lo.append(None)
            continue
        if not h_v or not l_v or not c_v:
            hi.append(hi[-1] if hi else None)
            lo.append(lo[-1] if lo else None)
            continue
        tp = (h_v + l_v + c_v) / 3.0
        if not started:
            pv_sum = 0.0
            v_sum = 0.0
            pv2_sum = 0.0
            started = True
        pv_sum += tp * v_v
        v_sum += v_v
        pv2_sum += v_v * tp * tp
        if v_sum > 0:
            mu = pv_sum / v_sum
            var = max(0.0, (pv2_sum / v_sum) - (mu * mu))
            sigma = var**0.5
            hi.append(mu + sigma)
            lo.append(mu - sigma)
        else:
            hi.append(tp)
            lo.append(tp)
    return hi, lo


def _intraday_resample_5m(bars: list[dict]) -> list[dict]:
    """Aggregate 1m bars into 5m bars keyed by ET 5-minute bucket.

    Each output row carries: ts (first bar's ts), et_min (bucket start),
    o/h/l/c, v. Buckets are aligned to wall-clock 5-minute boundaries
    in ET so 09:30, 09:35, 09:40 \u2026 are stable across dates.
    """
    out: list[dict] = []
    cur_bucket: int | None = None
    cur: dict | None = None
    for b in bars:
        et_min = _intraday_et_minute(str(b.get("ts") or ""))
        if et_min is None:
            continue
        bucket = (et_min // 5) * 5
        try:
            o = float(b.get("open") or 0)
            h = float(b.get("high") or 0)
            lo = float(b.get("low") or 0)
            c = float(b.get("close") or 0)
            v = float(b.get("iex_volume") or 0)
        except (TypeError, ValueError):
            continue
        if not (o and h and lo and c):
            continue
        if bucket != cur_bucket:
            if cur is not None:
                out.append(cur)
            cur_bucket = bucket
            cur = {"ts": b.get("ts"), "et_min": bucket, "o": o, "h": h, "l": lo, "c": c, "v": v}
        else:
            assert cur is not None
            cur["h"] = max(cur["h"], h)
            cur["l"] = min(cur["l"], lo)
            cur["c"] = c
            cur["v"] += v
    if cur is not None:
        out.append(cur)
    return out


def _intraday_ema9_5m(
    bars5: list[dict], pdc: float | None = None
) -> list[float | None]:
    """Standard 9-period EMA over the 5m closes.

    v6.0.0: when the real bar count is < 9 and ``pdc`` is provided,
    a synthetic 9-bar history flat at PDC is prepended to the series
    so the EMA9 line is always populated for every real bar. Without
    a synthetic prefix the line stayed empty for the first ~45 minutes
    of every session (and longer on tickers with thin premarket), so
    operators saw "data unavailable" precisely when they needed the
    indicator most.

    Mathematics: 9 synthetic closes flat at PDC produce SMA seed = PDC.
    Standard EMA recursion (alpha = 0.2) then advances on each real
    bar: ema_i = c_i * k + ema_(i-1) * (1 - k). This is equivalent to
    assuming yesterday's close held flat through the unknown stretch
    of premarket and then letting real prints pull the indicator. Once
    enough real bars accumulate, the synthetic prior decays out at the
    standard exponential rate.

    Frontend overlays this on the same time axis as the 1m bars by
    pairing each 5m EMA value with all 1m bars whose et_min falls in
    that 5m bucket. Output length always equals ``len(bars5)``.
    """
    out: list[float | None] = []
    k = 2.0 / (9 + 1)
    n = len(bars5)
    real_closes: list[float] = []
    for b in bars5:
        try:
            real_closes.append(float(b.get("c") or 0))
        except (TypeError, ValueError):
            real_closes.append(0.0)
    if n == 0:
        return out
    if n >= 9:
        # Plenty of real bars: seed = SMA of first 9 real closes.
        seed = sum(real_closes[:9]) / 9.0
        seed_idx = 8  # first slot to emit a value (bar #9)
        for i in range(n):
            if i < seed_idx:
                out.append(None)
                continue
            if i == seed_idx:
                ema = seed
                out.append(ema)
                continue
            ema = real_closes[i] * k + ema * (1 - k)
            out.append(ema)
        return out
    if pdc is not None and pdc > 0:
        # Synthetic 9-bar prefix flat at PDC: SMA seed = PDC.
        # Real bars advance the EMA from this prior.
        ema = float(pdc)
        for i in range(n):
            ema = real_closes[i] * k + ema * (1 - k)
            out.append(ema)
        return out
    # No PDC available: fall back to the strict pre-v6.0.0 rule.
    for _ in range(n):
        out.append(None)
    return out


def _intraday_today_trades(m, ticker: str, day: str) -> list[dict]:
    """Return today's actual entry/exit fills for `ticker`.

    Markers must reflect what the bot actually did, not what the trade
    log happens to surface. The trade log only writes on round-trip
    closure, so for OPEN positions we'd see no entry marker at all if
    we only read it. The right sources are paper_state.json:

    - paper_state.positions[ticker]:           open LONG entries
    - paper_state.short_positions[ticker]:     open SHORT entries
    - paper_state.trade_history (today only):  closed LONG round-trips
    - paper_state.short_trade_history (today): closed SHORT round-trips

    Each emitted row carries:
      side          'LONG' or 'SHORT'
      qty           share count
      entry_ts      full ISO UTC (or None if unknown)
      entry_price   numeric
      exit_ts       full ISO UTC (or None if still open)
      exit_price    numeric (or None if still open)
      realized_pnl  numeric (None for open)
      exit_reason   string (None for open)
      open          True if still open at read-time

    Empty list on any read failure.
    """
    tu = ticker.upper()
    out: list[dict] = []

    # --- Open positions (LONG + SHORT) -----------------------------
    try:
        pos_dict = getattr(m, "positions", None)
        if isinstance(pos_dict, dict):
            p = pos_dict.get(tu) or pos_dict.get(ticker)
            if isinstance(p, dict) and p.get("entry_price") is not None:
                # Filter to today only via entry_ts_utc.
                ets = str(p.get("entry_ts_utc") or "")
                if ets.startswith(day):
                    out.append(
                        {
                            "side": "LONG",
                            "qty": p.get("shares"),
                            "entry_ts": p.get("entry_ts_utc"),
                            "entry_price": float(p.get("entry_price")),
                            "exit_ts": None,
                            "exit_price": None,
                            "realized_pnl": None,
                            "exit_reason": None,
                            "open": True,
                        }
                    )
    except Exception:
        pass
    try:
        spos_dict = getattr(m, "short_positions", None)
        if isinstance(spos_dict, dict):
            p = spos_dict.get(tu) or spos_dict.get(ticker)
            if isinstance(p, dict) and p.get("entry_price") is not None:
                ets = str(p.get("entry_ts_utc") or "")
                if ets.startswith(day):
                    out.append(
                        {
                            "side": "SHORT",
                            "qty": p.get("shares"),
                            "entry_ts": p.get("entry_ts_utc"),
                            "entry_price": float(p.get("entry_price")),
                            "exit_ts": None,
                            "exit_price": None,
                            "realized_pnl": None,
                            "exit_reason": None,
                            "open": True,
                        }
                    )
    except Exception:
        pass

    # --- Closed round-trips (LONG + SHORT) -------------------------
    def _from_history(rows, side_default):
        for r in rows or []:
            try:
                if (r.get("ticker") or "").upper() != tu:
                    continue
                ets = str(r.get("entry_time_iso") or "")
                xts = str(r.get("exit_time_iso") or "")
                # A round-trip belongs to `day` if either leg is on `day`.
                if not (ets.startswith(day) or xts.startswith(day)):
                    continue
                side = (r.get("side") or side_default or "LONG").upper()
                out.append(
                    {
                        "side": side,
                        "qty": r.get("shares"),
                        "entry_ts": r.get("entry_time_iso"),
                        "entry_price": float(r.get("entry_price"))
                        if r.get("entry_price") is not None
                        else None,
                        "exit_ts": r.get("exit_time_iso"),
                        "exit_price": float(r.get("exit_price"))
                        if r.get("exit_price") is not None
                        else None,
                        "realized_pnl": r.get("pnl"),
                        "exit_reason": r.get("reason"),
                        "open": False,
                    }
                )
            except (TypeError, ValueError):
                continue

    try:
        _from_history(getattr(m, "trade_history", None), "LONG")
    except Exception:
        pass
    try:
        _from_history(getattr(m, "short_trade_history", None), "SHORT")
    except Exception:
        pass
    return out


def _intraday_build_lifecycle(ticker: str, day: str) -> dict:
    """v5.31.0 \u2014 build the open-position lifecycle overlay payload.

    Reads the day's forensic JSONL streams (``decisions/{TICKER}.jsonl``,
    ``exits/{TICKER}.jsonl``, ``indicators/{TICKER}.jsonl``) plus live
    open-position state and emits a chart-friendly block:

    ``{"entries": [...], "exits": [...], "trail_series": [...], "open": [...]}``

    All arrays carry ``et_min`` keys so the frontend can plot directly
    via ``xOf(et_min)``. Failure-tolerant: any read error returns the
    empty shape so the chart never breaks on a missing forensic file.
    """
    from pathlib import Path as _P

    sym = (ticker or "").strip().upper()
    out = {"entries": [], "exits": [], "trail_series": [], "open": []}
    if not sym:
        return out
    base = _P("/data/forensics") / day

    def _load_jsonl(path: _P) -> list[dict]:
        rows: list[dict] = []
        try:
            if not path.exists():
                return rows
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            pass
        return rows

    # Entry markers \u2014 only the records with decision == "ENTER".
    try:
        for r in _load_jsonl(base / "decisions" / f"{sym}.jsonl"):
            if str(r.get("decision") or "").upper() != "ENTER":
                continue
            et_min = _intraday_et_minute(str(r.get("ts_utc") or ""))
            if et_min is None:
                continue
            out["entries"].append(
                {
                    "et_min": et_min,
                    "ts_utc": r.get("ts_utc"),
                    "price": r.get("current_price"),
                    "side": r.get("side"),
                    "strike_num": r.get("strike_num"),
                    "entry_bid": r.get("entry_bid"),
                    "entry_ask": r.get("entry_ask"),
                    "spread_bps": r.get("spread_bps"),
                }
            )
    except Exception:
        pass

    # Exit markers \u2014 from the new exits stream (v5.31.0).
    try:
        for r in _load_jsonl(base / "exits" / f"{sym}.jsonl"):
            et_min = _intraday_et_minute(str(r.get("ts_utc") or ""))
            if et_min is None:
                continue
            entry_et = _intraday_et_minute(str(r.get("entry_ts_utc") or ""))
            out["exits"].append(
                {
                    "et_min": et_min,
                    "ts_utc": r.get("ts_utc"),
                    "price": r.get("exit_price"),
                    "side": r.get("side"),
                    "entry_et_min": entry_et,
                    "entry_price": r.get("entry_price"),
                    "alarm": r.get("alarm_triggered"),
                    "reason": r.get("exit_reason_code"),
                    "trail_stage": r.get("trail_stage_at_exit"),
                    "peak_close": r.get("peak_close_at_exit"),
                    "bars_in_trade": r.get("bars_in_trade"),
                    "mae_bps": r.get("mae_bps"),
                    "mfe_bps": r.get("mfe_bps"),
                    "pnl_dollars": r.get("pnl_dollars"),
                    "pnl_pct": r.get("pnl_pct"),
                }
            )
    except Exception:
        pass

    # Trail-stop staircase \u2014 from indicator snapshots' permit_state.trail.
    # Only emit a point when the snapshot has trail data (i.e. the position
    # was open during that minute). Skipped minutes \u2192 frontend draws a
    # break in the staircase.
    try:
        for r in _load_jsonl(base / "indicators" / f"{sym}.jsonl"):
            et_min = _intraday_et_minute(str(r.get("ts_utc") or ""))
            if et_min is None:
                continue
            ps = r.get("permit_state") or {}
            tr = ps.get("trail") if isinstance(ps, dict) else None
            if not isinstance(tr, dict):
                continue
            stop = tr.get("last_proposed_stop")
            if stop is None:
                continue
            out["trail_series"].append(
                {
                    "et_min": et_min,
                    "stop": stop,
                    "stage": tr.get("stage"),
                    "side": tr.get("side"),
                    "peak_close": tr.get("peak_close"),
                }
            )
    except Exception:
        pass

    # Live open-position rail \u2014 still-open trades. Used by the frontend
    # to extend an entry-price horizontal line out to "now" (drift visual).
    try:
        m = _ssm()
        for _attr, _label in (("positions", "LONG"), ("short_positions", "SHORT")):
            book = getattr(m, _attr, None) or {}
            pos = book.get(sym)
            if pos is None:
                continue
            entry_et = _intraday_et_minute(str(pos.get("entry_ts_utc") or ""))
            if entry_et is None:
                continue
            out["open"].append(
                {
                    "et_min": entry_et,
                    "side": _label,
                    "entry_price": pos.get("entry_price"),
                    "shares": pos.get("shares"),
                    "current_stop": pos.get("stop"),
                }
            )
    except Exception:
        pass

    return out


def _intraday_build_payload(ticker: str) -> dict:
    """Compose the /api/intraday/{ticker} response body. Pure function:
    no network I/O, only on-disk JSONL + live globals (or_high/or_low).
    Keeping it pure makes the handler easy to unit-test.

    v5.31.0 \u2014 payload now also carries ``pdc``, ``sess_hod``,
    ``sess_lod``, an AVWAP \u00b11\u03c3 band (``avwap_hi``/``avwap_lo``
    per bar), a premarket-anchored AVWAP series (``pm_avwap`` per bar),
    a ``sentinel_events`` list, and the open-position ``lifecycle``
    overlay block (entries / exits / trail_series / open).
    """
    from datetime import datetime, timezone

    m = _ssm()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bars = _intraday_load_today_bars(ticker, day)
    avwap = _intraday_compute_avwap(bars)
    avwap_hi, avwap_lo = _intraday_compute_avwap_band(bars)
    pm_avwap = _intraday_compute_avwap(bars, anchor_min=480)
    bars5 = _intraday_resample_5m(bars)
    # v6.0.0 \u2014 PDC must be available BEFORE the EMA9 calc so the
    # synthetic-prefix seed can engage on thin-premarket tickers.
    or_high = None
    or_low = None
    pdc = None
    sess_hod = None
    sess_lod = None
    try:
        oh = getattr(m, "or_high", {}) or {}
        ol = getattr(m, "or_low", {}) or {}
        v = oh.get(ticker.upper())
        or_high = float(v) if v is not None else None
        v = ol.get(ticker.upper())
        or_low = float(v) if v is not None else None
        # v5.31.0 \u2014 PDC + session HOD/LOD for the new chart reference lines.
        _pdc_d = getattr(m, "pdc", {}) or {}
        _hod_d = getattr(m, "_v570_session_hod", {}) or {}
        _lod_d = getattr(m, "_v570_session_lod", {}) or {}
        v = _pdc_d.get(ticker.upper())
        pdc = float(v) if v is not None else None
        v = _hod_d.get(ticker.upper())
        sess_hod = float(v) if v is not None else None
        v = _lod_d.get(ticker.upper())
        sess_lod = float(v) if v is not None else None
    except Exception:
        pass
    ema9_5m = _intraday_ema9_5m(bars5, pdc=pdc)
    # Pair each 5m EMA value with its bucket so the frontend can match
    # back to 1m bars by et_min // 5.
    ema9_by_bucket: dict[int, float] = {}
    for b5, e in zip(bars5, ema9_5m):
        if e is not None and isinstance(b5.get("et_min"), int):
            ema9_by_bucket[int(b5["et_min"])] = float(e)
    trades = _intraday_today_trades(m, ticker, day)

    # v5.31.0 \u2014 sentinel arm/trip events from the bounded module-level
    # deque populated by ``broker/positions.py:_run_sentinel``. Filtered
    # to the requested ticker; only events from today are returned.
    sentinel_events: list[dict] = []
    try:
        ev_list = getattr(m, "_sentinel_arm_events", None) or []
        for ev in list(ev_list):
            if not isinstance(ev, dict):
                continue
            if str(ev.get("ticker") or "").upper() != ticker.upper():
                continue
            et_min = _intraday_et_minute(str(ev.get("ts_utc") or ""))
            if et_min is None:
                continue
            sentinel_events.append(
                {
                    "et_min": et_min,
                    "ts_utc": ev.get("ts_utc"),
                    "codes": ev.get("codes") or [],
                    "price": ev.get("price"),
                    "fired": bool(ev.get("fired")),
                }
            )
    except Exception:
        pass

    # Slim per-bar payload \u2014 only fields the chart needs.
    bars_out: list[dict] = []
    for i, b in enumerate(bars):
        et_min = _intraday_et_minute(str(b.get("ts") or ""))
        if et_min is None:
            continue
        bars_out.append(
            {
                "ts": b.get("ts"),
                "et_min": et_min,
                "o": b.get("open"),
                "h": b.get("high"),
                "l": b.get("low"),
                "c": b.get("close"),
                "v": b.get("iex_volume"),
                "avwap": avwap[i] if i < len(avwap) else None,
                # v5.31.0 \u2014 AVWAP band + premarket-anchored AVWAP.
                "avwap_hi": avwap_hi[i] if i < len(avwap_hi) else None,
                "avwap_lo": avwap_lo[i] if i < len(avwap_lo) else None,
                "pm_avwap": pm_avwap[i] if i < len(pm_avwap) else None,
                "ema9_5m": ema9_by_bucket.get((et_min // 5) * 5),
            }
        )

    # v5.31.0 \u2014 open-position lifecycle overlay block (entries / exits /
    # trail-stop staircase / live open positions).
    lifecycle = _intraday_build_lifecycle(ticker, day)

    return {
        "ok": True,
        "ticker": ticker.upper(),
        "date": day,
        "bars": bars_out,
        "or_high": or_high,
        "or_low": or_low,
        "pdc": pdc,
        "sess_hod": sess_hod,
        "sess_lod": sess_lod,
        "trades": trades,
        "sentinel_events": sentinel_events,
        "lifecycle": lifecycle,
        "bar_count": len(bars_out),
    }


async def h_intraday(request):
    """GET /api/intraday/{ticker} \u2014 today's 1m bars + key levels."""
    from aiohttp import web

    if not _check_auth(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    raw = (request.match_info.get("ticker") or "").strip().upper()
    # Defensive: accept only [A-Z0-9.] tickers, max 10 chars.
    if not raw or not re.match(r"^[A-Z0-9.]{1,10}$", raw):
        return web.json_response({"ok": False, "error": "bad ticker"}, status=400)
    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(None, _intraday_build_payload, raw)
    return web.json_response(payload)


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


# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
# v5.13.6 \u2014 per-position lifecycle log endpoints
# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

_LIFECYCLE_POSITION_ID_RE = re.compile(r"^[A-Za-z0-9._-]+_\d{8}T\d{6}Z_(long|short)$")


def _lifecycle_logger_safe():
    try:
        import lifecycle_logger as _ll

        return _ll.get_default_logger()
    except Exception:
        return None


async def h_lifecycle_positions(request):
    """GET /api/lifecycle/positions?status=open|recent|closed|all&limit=20"""
    from aiohttp import web

    if not _check_auth(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    status = (request.query.get("status") or "all").strip().lower()
    if status not in ("open", "closed", "recent", "all"):
        status = "all"
    try:
        limit = int(request.query.get("limit", "20"))
    except (TypeError, ValueError):
        limit = 20
    ll = _lifecycle_logger_safe()
    if ll is None:
        return web.json_response(
            {"ok": False, "error": "lifecycle_unavailable", "positions": []}, status=200
        )
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, lambda: ll.list_positions(status, limit))
    return web.json_response({"ok": True, "count": len(rows), "positions": rows})


async def h_lifecycle_position(request):
    """GET /api/lifecycle/{position_id}?since_seq=N \u2014 full timeline."""
    from aiohttp import web

    if not _check_auth(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    position_id = (request.match_info.get("position_id") or "").strip()
    if not _LIFECYCLE_POSITION_ID_RE.match(position_id):
        return web.json_response({"ok": False, "error": "bad position_id"}, status=400)
    try:
        since_seq = int(request.query.get("since_seq", "0"))
    except (TypeError, ValueError):
        since_seq = 0
    ll = _lifecycle_logger_safe()
    if ll is None:
        return web.json_response(
            {"ok": False, "error": "lifecycle_unavailable", "events": []}, status=200
        )
    loop = asyncio.get_running_loop()
    events = await loop.run_in_executor(None, lambda: ll.read_events(position_id, since_seq))
    return web.json_response(
        {
            "ok": True,
            "position_id": position_id,
            "count": len(events),
            "events": events,
        }
    )


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
    app.router.add_get("/api/ws_state", h_ws_state)
    app.router.add_get("/api/version", h_version)
    app.router.add_get("/api/trade_log", h_trade_log)
    # v4.0.0-beta — per-executor tabs + index ticker strip.
    app.router.add_get("/api/executor/{name}", h_executor)
    app.router.add_get("/api/indices", h_indices)
    # v4.11.0 \u2014 health-pill tap-to-expand endpoint.
    app.router.add_get("/api/errors/{executor}", h_errors)
    # v5.13.6 \u2014 per-position lifecycle event log endpoints.
    app.router.add_get("/api/lifecycle/positions", h_lifecycle_positions)
    app.router.add_get("/api/lifecycle/{position_id}", h_lifecycle_position)
    # v5.23.0 \u2014 inline chart panel data source for the expanded Titan card.
    app.router.add_get("/api/intraday/{ticker}", h_intraday)
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
        logger.warning("Dashboard disabled: DASHBOARD_PASSWORD must be at least 8 characters")
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

    t = threading.Thread(target=_run_forever, args=(port,), name="dashboard-http", daemon=True)
    t.start()
    logger.info("Dashboard thread started (port=%d)", port)
    return True
