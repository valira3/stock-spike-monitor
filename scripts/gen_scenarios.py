#!/usr/bin/env python3
"""gen_scenarios.py -- generate 3 fake dashboard scenarios and upload to R2.

Scenarios:
  1. off-market  -- Saturday 8am ET, all trades closed from Friday
  2. morning     -- 10:15 ET, ORCL LONG open at 0.5R, scanner active
  3. eod         -- 15:45 ET, AVGO LONG + MSFT SHORT open, morning closed

Outputs 3 presigned R2 URLs.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Load .env.monitor
for line in (Path(__file__).parent.parent / ".env.monitor").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import scripts.replay_dashboard as rd

# ── fetch base state ──────────────────────────────────────────────────────────
def fetch_base() -> dict:
    jar = urllib.request.HTTPCookieProcessor()
    opener = urllib.request.build_opener(jar)
    opener.open(urllib.request.Request(
        "https://tradegenius.up.railway.app/login",
        data=urllib.parse.urlencode({"password": "3YhCoi5AIZYAFG7eDua8bD8Z"}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    ))
    return json.loads(opener.open("https://tradegenius.up.railway.app/api/state").read())


# ── realistic OR windows ──────────────────────────────────────────────────────
OR_WINDOWS = {
    "META":  {"or_high": 614.09, "or_low": 609.49, "current_price": 614.23, "bars_seen": 30, "locked": True},
    "GOOG":  {"or_high": 393.20, "or_low": 389.87, "current_price": 393.32, "bars_seen": 30, "locked": True},
    "AVGO":  {"or_high": 432.62, "or_low": 424.44, "current_price": 425.19, "bars_seen": 30, "locked": True},
    "NVDA":  {"or_high": 230.00, "or_low": 226.41, "current_price": 225.32, "bars_seen": 30, "locked": True},
    "AAPL":  {"or_high": 302.80, "or_low": 298.10, "current_price": 300.23, "bars_seen": 30, "locked": True},
    "MSFT":  {"or_high": 425.40, "or_low": 420.10, "current_price": 421.92, "bars_seen": 30, "locked": True},
    "ORCL":  {"or_high": 194.20, "or_low": 191.80, "current_price": 192.98, "bars_seen": 30, "locked": True},
    "AMZN":  {"or_high": 267.40, "or_low": 261.20, "current_price": 264.14, "bars_seen": 30, "locked": True},
    "NFLX":  {"or_high": 89.40,  "or_low": 85.20,  "current_price": 87.02,  "bars_seen": 30, "locked": True},
    "TSLA":  {"or_high": 428.60, "or_low": 418.20, "current_price": 422.24, "bars_seen": 30, "locked": True},
}

PROXIMITY_BASE = [
    {"ticker": "ORCL", "price": 193.52, "or_high": 194.20, "or_low": 191.80, "nearest_label": "OR-high", "nearest_pct": 0.0035, "open_side": None, "permit_side": "NONE"},
    {"ticker": "META", "price": 613.50, "or_high": 614.09, "or_low": 609.49, "nearest_label": "OR-high", "nearest_pct": 0.0010, "open_side": None, "permit_side": "NONE"},
    {"ticker": "GOOG", "price": 392.80, "or_high": 393.20, "or_low": 389.87, "nearest_label": "OR-high", "nearest_pct": 0.0010, "open_side": None, "permit_side": "NONE"},
    {"ticker": "AVGO", "price": 424.60, "or_high": 432.62, "or_low": 424.44, "nearest_label": "OR-low",  "nearest_pct": 0.0004, "open_side": None, "permit_side": "NONE"},
    {"ticker": "NVDA", "price": 226.50, "or_high": 230.00, "or_low": 226.41, "nearest_label": "OR-low",  "nearest_pct": 0.0004, "open_side": None, "permit_side": "NONE"},
    {"ticker": "AAPL", "price": 300.10, "or_high": 302.80, "or_low": 298.10, "nearest_label": "OR-low",  "nearest_pct": 0.0007, "open_side": None, "permit_side": "NONE"},
    {"ticker": "MSFT", "price": 420.50, "or_high": 425.40, "or_low": 420.10, "nearest_label": "OR-low",  "nearest_pct": 0.0010, "open_side": None, "permit_side": "NONE"},
    {"ticker": "NFLX", "price": 87.00,  "or_high": 89.40,  "or_low": 85.20,  "nearest_label": "OR-low",  "nearest_pct": 0.0023, "open_side": None, "permit_side": "NONE"},
    {"ticker": "TSLA", "price": 422.10, "or_high": 428.60, "or_low": 418.20, "nearest_label": "OR-low",  "nearest_pct": 0.0093, "open_side": None, "permit_side": "NONE"},
    {"ticker": "AMZN", "price": 264.00, "or_high": 267.40, "or_low": 261.20, "nearest_label": "OR-low",  "nearest_pct": 0.0104, "open_side": None, "permit_side": "NONE"},
]


def make_rb(open_risk=0.0, open_count=0, admit=0, reject=0, realized=0.0, equity=100000.0) -> dict:
    avail = max(0.0, 2000.0 - open_risk)
    return {
        "portfolio_id": "main", "equity": equity,
        "max_risk_dollars": 2000.0, "max_notional": 95000.0,
        "open_risk": open_risk, "open_notional": 0.0, "open_count": open_count,
        "admit_count": admit, "reject_count": reject, "last_reject_reason": "",
        "available_risk": avail, "utilization_pct": round(open_risk / 2000 * 100, 1),
        "realized_pnl_today": realized, "session_start_equity": equity,
        "daily_kill_threshold": 2000.0, "daily_kill_triggered": False,
        "daily_loss_kill_pct": 2.0, "loss_lock_threshold_usd": 0.0,
        "peak_dd_halt_usd": 0.0, "locked_pairs": [],
        "peak_pnl_today": max(0.0, realized), "current_dd_from_peak": 0.0,
    }


FRIDAY_TRADES = [
    {"action": "BUY",   "ticker": "ORCL", "price": 193.375, "limit_price": 193.40, "shares": 349, "cost": 67487.875, "stop": 192.41, "entry_num": 1, "time": "10:26 ET", "date": "2026-05-15", "side": "LONG",  "portfolio": "paper"},
    {"action": "SELL",  "ticker": "ORCL", "price": 193.735, "shares": 349, "pnl": 125.64,  "pnl_pct": 0.19,  "reason": "sentinel_a_stop_price", "entry_price": 193.375, "time": "10:48 ET", "date": "2026-05-15", "side": "LONG",  "portfolio": "paper"},
    {"action": "BUY",   "ticker": "ORCL", "price": 193.735, "limit_price": 193.76, "shares": 377, "cost": 73038.095, "stop": 192.77, "entry_num": 2, "time": "10:48 ET", "date": "2026-05-15", "side": "LONG",  "portfolio": "paper"},
    {"action": "SELL",  "ticker": "ORCL", "price": 192.64,  "shares": 377, "pnl": -412.82, "pnl_pct": -0.57, "reason": "sentinel_a_stop_price", "entry_price": 193.735, "time": "11:07 ET", "date": "2026-05-15", "side": "LONG",  "portfolio": "paper"},
    {"action": "BUY",   "ticker": "AVGO", "symbol": "AVGO", "side": "LONG",  "shares": 84, "qty": 84, "price": 426.03,  "entry_price": 426.03,  "cost": 35786.52, "time": "15:46 ET", "date": "2026-05-15", "portfolio": "eod", "eod": True},
    {"action": "SHORT", "ticker": "MSFT", "symbol": "MSFT", "side": "SHORT", "shares": 84, "qty": 84, "price": 423.50,  "entry_price": 423.50,  "cost": 35574.0,  "time": "15:46 ET", "date": "2026-05-15", "portfolio": "eod", "eod": True},
    {"action": "SELL",  "ticker": "AVGO", "symbol": "AVGO", "side": "LONG",  "shares": 84, "qty": 84, "price": 425.4501,"exit_price": 425.4501, "entry_price": 426.03,  "pnl": -48.71,  "pnl_pct": -0.14, "time": "15:59 ET", "date": "2026-05-15", "portfolio": "eod", "eod": True},
    {"action": "COVER", "ticker": "MSFT", "symbol": "MSFT", "side": "SHORT", "shares": 84, "qty": 84, "price": 421.89,  "exit_price": 421.89,  "entry_price": 423.50,  "pnl": 135.24, "pnl_pct": 0.38,  "time": "15:59 ET", "date": "2026-05-15", "portfolio": "eod", "eod": True},
]

SPY_REGIME = {"regime": "LONG_BIAS", "spy_open_930": 538.20, "spy_close_1000": 539.40, "ret_pct": 0.0022, "classified_at": "10:00:02"}

# ── fake /api/indices payload ─────────────────────────────────────────────────
def _idx(sym, label, last, chg, pct, ah=False, ah_chg=None, ah_pct=None, fut_label=None, fut_pct=None, session="rth"):
    row = {"symbol": sym, "display_label": label, "available": True,
           "last": last, "change": chg, "change_pct": pct, "ah": ah}
    if ah and ah_chg is not None:
        row.update({"ah_change": ah_chg, "ah_change_pct": ah_pct})
    if fut_label and fut_pct is not None:
        row["future"] = {"label": fut_label, "symbol": fut_label, "change_pct": fut_pct}
    return row

# RTH: clean prices, no AH/futures clutter
FAKE_INDICES_RTH = {
    "session": "rth", "yahoo_ok": True,
    "indices": [
        _idx("SPY",    "S&P 500",   538.74,  +1.40, +0.26),
        _idx("QQQ",    "Nasdaq",    473.81,  +2.11, +0.45),
        _idx("IWM",    "Russell",   204.32,  -0.44, -0.21),
        _idx("^VIX",   "VIX",        17.82,  -0.61, -3.31),
    ],
}

# Off-hours: AH deltas + futures shown
FAKE_INDICES_CLOSED = {
    "session": "ah", "yahoo_ok": True,
    "indices": [
        _idx("SPY",  "S&P 500",  537.62, +0.72, +0.13, ah=True, ah_chg=-1.12, ah_pct=-0.21, fut_label="ES", fut_pct=-0.18, session="ah"),
        _idx("QQQ",  "Nasdaq",   473.11, +1.44, +0.31, ah=True, ah_chg=-0.70, ah_pct=-0.15, fut_label="NQ", fut_pct=-0.12, session="ah"),
        _idx("IWM",  "Russell",  204.76, -0.88, -0.43, ah=True, ah_chg=+0.32, ah_pct=+0.16, session="ah"),
        _idx("^VIX", "VIX",       18.43, +0.12, +0.65, session="ah"),
    ],
}


def build_scenario_1(base: dict) -> dict:
    """Off-market: Saturday 8am ET, all Friday trades visible, no positions."""
    s = copy.deepcopy(base)
    s["server_time"] = "2026-05-16T08:00:00.000000-04:00"
    s["server_time_label"] = "Sat May 16 | 08:00:00 ET"
    s["positions"] = []
    s["trades_today"] = FRIDAY_TRADES
    s["regime"] = {"mode": "CLOSED", "mode_reason": "weekend", "breadth": "UNKNOWN", "breadth_detail": "", "rsi_regime": "UNKNOWN", "rsi_detail": ""}
    s["gates"]["scan_paused"] = True
    s["gates"]["scan_idle_hours"] = True
    s["v10"]["day_status"].update({"session_date": "2026-05-15", "vix_current": 18.43, "block_day": False})
    s["v10"]["or_windows"] = OR_WINDOWS
    realized_eod = -48.71 + 135.24 + 108.60  # AVGO + MSFT + ORCL EOD
    # Morning: NVDA +$586.40, ORCL net -$287.18 → morning net +$299.22
    realized_morning = 166.40 + 420.00 + 125.64 - 412.82
    realized_total = round(realized_morning + realized_eod, 2)  # +$494.75
    s["v10"]["risk_books"] = {
        "main": make_rb(realized=realized_total),
        "val":  make_rb(realized=0.0),
        "gene": make_rb(realized=0.0),
    }
    s["v10"]["eod"]["session_date"] = "2026-05-15"
    s["v10"]["eod"]["per_portfolio"] = {
        "main": {"open_count": 0, "open_positions": [], "realized_pnl_today": realized_eod, "entry_attempted": True, "rejected_count": 0, "closed_legs": [
            {"ticker": "AVGO", "side": "LONG",  "shares": 84,  "entry_price": 426.03,  "exit_price": 425.45,  "pnl": -48.71,  "exit_reason": "eod"},
            {"ticker": "MSFT", "side": "SHORT", "shares": 84,  "entry_price": 423.50,  "exit_price": 421.89,  "pnl": 135.24,  "exit_reason": "eod"},
            {"ticker": "ORCL", "side": "LONG",  "shares": 181, "entry_price": 192.80,  "exit_price": 193.40,  "pnl": 108.60,  "exit_reason": "eod"},
        ]},
        "val":  {"open_count": 0, "open_positions": [], "realized_pnl_today": 0.0, "entry_attempted": False, "rejected_count": 0, "closed_legs": []},
        "gene": {"open_count": 0, "open_positions": [], "realized_pnl_today": 0.0, "entry_attempted": False, "rejected_count": 0, "closed_legs": []},
    }
    s["v10"]["activity"] = [
        {"kind": "EOD_EXIT",      "ticker": "AVGO+MSFT+ORCL", "pid": "main", "ts_et": "15:59:03", "detail": "EOD closed: AVGO -$48.71, MSFT +$135.24, ORCL +$108.60"},
        {"kind": "EOD_ENTRY",     "ticker": "AVGO+MSFT+ORCL", "pid": "main", "ts_et": "15:30:00", "detail": "EOD: LONG AVGO + SHORT MSFT + LONG ORCL"},
        {"kind": "EXIT",          "ticker": "NVDA",           "pid": "main", "ts_et": "12:45:00", "detail": "target 2.5R: fill=232.35 pnl=+$420.00 (runner)"},
        {"kind": "EXIT",          "ticker": "NVDA",           "pid": "main", "ts_et": "11:30:00", "detail": "target 1R: fill=229.95 pnl=+$166.40 (partial)"},
        {"kind": "KILL",          "ticker": "",               "pid": "main", "ts_et": "11:07:43", "detail": "daily_loss_kill: ORCL realized -$287.18 | NVDA +$255 unr"},
        {"kind": "EXIT",          "ticker": "ORCL",           "pid": "main", "ts_et": "11:07:43", "detail": "stop: fill=192.64 pnl=-$412.82"},
        {"kind": "ADMIT",         "ticker": "ORCL",           "pid": "main", "ts_et": "10:48:32", "detail": "LONG 377sh @ 193.735 stop=192.77"},
        {"kind": "EXIT",          "ticker": "ORCL",           "pid": "main", "ts_et": "10:48:30", "detail": "stop: fill=193.735 pnl=+$125.64"},
        {"kind": "ADMIT",         "ticker": "ORCL",           "pid": "main", "ts_et": "10:26:34", "detail": "LONG 349sh @ 193.375 stop=192.41"},
        {"kind": "ADMIT",         "ticker": "NVDA",           "pid": "main", "ts_et": "10:08:15", "detail": "LONG 209sh @ 228.35 stop=226.75"},
        {"kind": "OR_LOCK",       "ticker": "ORCL",           "pid": "main", "ts_et": "10:00:01", "detail": "OR locked: H=194.20 L=191.80 rng=1.24%"},
        {"kind": "SESSION_START", "ticker": "",               "pid": "main", "ts_et": "09:30:00", "detail": "v10 session started"},
    ]
    s["portfolio"] = {"cash": 100000 + realized_total, "long_mv": 0, "short_liab": 0,
                      "equity": round(100000 + realized_total, 2), "start": 100000,
                      "vs_start": realized_total, "day_pnl": realized_total}
    s["proximity"] = sorted(PROXIMITY_BASE, key=lambda x: x["nearest_pct"])
    s["spy_regime_today"] = {"regime": "LONG_BIAS", "spy_open_930": 537.34, "spy_close_1000": 538.90, "ret_pct": 0.0029, "classified_at": "10:00:01"}
    s["_indices"] = FAKE_INDICES_CLOSED
    return s


def build_scenario_2(base: dict) -> dict:
    """Morning market: 10:15 ET, ORCL LONG open at ~0.5R gain, scanner active."""
    s = copy.deepcopy(base)
    s["server_time"] = "2026-05-16T10:15:00.000000-04:00"
    s["server_time_label"] = "Fri May 16 | 10:15:00 ET"

    ENTRY = 193.375; STOP = 192.41; MARK = 193.89; SH = 349
    RISK = round((ENTRY - STOP) * SH, 2)
    UPL  = round((MARK - ENTRY) * SH, 2)

    # Progress bar needs: p.entry (not entry_price), p.unrealized (not unrealized_pnl),
    # p.entry_stop (immutable axis anchor), p.stop (current effective stop)
    s["positions"] = [{
        "ticker": "ORCL", "side": "LONG", "shares": SH,
        "entry": ENTRY, "mark": MARK,
        "cost": round(ENTRY * SH, 3),
        "entry_stop": STOP,     # immutable axis anchor for progress bar
        "stop": STOP,           # current effective stop
        "effective_stop": STOP,
        "risk_dollars": RISK,
        "unrealized": UPL,      # field name the renderer reads
        "unrealized_pnl": UPL,  # kept for executor-tab renderers
        "held_seconds": 2706,
        "entry_ts_utc": "2026-05-16T13:30:34Z",
        "portfolio": "paper", "phase": "A", "entry_num": 1, "trail_active": False,
    }]
    s["trades_today"] = [
        {"action": "BUY", "ticker": "ORCL", "price": ENTRY, "limit_price": 193.40, "shares": SH,
         "cost": round(ENTRY * SH, 3), "stop": STOP, "entry_num": 1,
         "time": "09:30 ET", "date": "2026-05-16", "side": "LONG", "portfolio": "paper"},
    ]
    s["regime"] = {"mode": "OR", "mode_reason": "Opening Range: 9:30-10:00 ET", "breadth": "BULLISH", "breadth_detail": "", "rsi_regime": "NEUTRAL", "rsi_detail": ""}
    s["gates"]["scan_paused"] = False
    s["gates"]["scan_idle_hours"] = False
    s["v10"]["day_status"].update({"session_date": "2026-05-16", "vix_current": 17.82, "block_day": False})
    s["v10"]["or_windows"] = OR_WINDOWS
    s["v10"]["mbr_reject_count"] = 1
    s["v10"]["vwap_chase_reject_count"] = 1
    s["v10"]["risk_books"] = {
        "main": make_rb(open_risk=RISK, open_count=1, admit=1, reject=2),
        "val":  make_rb(open_risk=RISK, open_count=1, admit=1, reject=1),
        "gene": make_rb(),
    }
    s["v10"]["eod"]["session_date"] = "2026-05-16"
    s["v10"]["eod"]["per_portfolio"] = {
        "main": {"open_count": 0, "open_positions": [], "realized_pnl_today": 0.0, "entry_attempted": False, "rejected_count": 0, "closed_legs": []},
        "val":  {"open_count": 0, "open_positions": [], "realized_pnl_today": 0.0, "entry_attempted": False, "rejected_count": 0, "closed_legs": []},
        "gene": {"open_count": 0, "open_positions": [], "realized_pnl_today": 0.0, "entry_attempted": False, "rejected_count": 0, "closed_legs": []},
    }
    s["v10"]["activity"] = [
        {"kind": "ADMIT",         "ticker": "ORCL", "pid": "main", "ts_et": "09:30:34", "detail": "LONG 349sh @ 193.375 stop=192.41 risk=$331"},
        {"kind": "REJECT",        "ticker": "GOOG", "pid": "main", "ts_et": "10:03:22", "detail": "mbr_reject: break 3bps < 5bps min_break"},
        {"kind": "REJECT",        "ticker": "META", "pid": "main", "ts_et": "10:01:44", "detail": "vwap_chase: 19bps > 15bps gate"},
        {"kind": "OR_LOCK",       "ticker": "MSFT", "pid": "main", "ts_et": "10:00:01", "detail": "OR locked: H=425.40 L=420.10 rng=1.26%"},
        {"kind": "OR_LOCK",       "ticker": "AAPL", "pid": "main", "ts_et": "10:00:01", "detail": "OR locked: H=302.80 L=298.10 rng=1.57%"},
        {"kind": "OR_LOCK",       "ticker": "AVGO", "pid": "main", "ts_et": "10:00:01", "detail": "OR locked: H=432.62 L=424.44 rng=1.93%"},
        {"kind": "OR_LOCK",       "ticker": "META", "pid": "main", "ts_et": "10:00:01", "detail": "OR locked: H=614.09 L=609.49 rng=0.75%"},
        {"kind": "OR_LOCK",       "ticker": "ORCL", "pid": "main", "ts_et": "10:00:01", "detail": "OR locked: H=194.20 L=191.80 rng=1.24%"},
        {"kind": "SESSION_START", "ticker": "",     "pid": "main", "ts_et": "09:30:00", "detail": "v10 session started — LONG_BIAS"},
    ]
    s["portfolio"] = {"cash": 100000.0, "long_mv": round(MARK * SH, 2), "short_liab": 0,
                      "equity": round(100000 + UPL, 2), "start": 100000,
                      "vs_start": round(UPL, 2), "day_pnl": round(UPL, 2)}
    # Proximity: ORCL near OR-high (open position)
    prox = copy.deepcopy(PROXIMITY_BASE)
    for p in prox:
        if p["ticker"] == "ORCL":
            p.update({"price": MARK, "nearest_pct": round((194.20 - MARK) / MARK, 4),
                      "open_side": "long", "permit_side": "LONG"})
    s["proximity"] = sorted(prox, key=lambda x: x["nearest_pct"])
    s["spy_regime_today"] = SPY_REGIME
    s["section_i_permit"] = {"qqq_5m_close": 473.11, "long_open": True, "short_open": False}
    s["_indices"] = FAKE_INDICES_RTH
    return s


def build_scenario_3(base: dict) -> dict:
    """EOD market: 15:45 ET, AVGO LONG + MSFT SHORT + ORCL LONG open."""
    s = copy.deepcopy(base)
    s["server_time"] = "2026-05-16T15:45:00.000000-04:00"
    s["server_time_label"] = "Fri May 16 | 15:45:00 ET"

    AE = 426.03; AM = 425.55; ASH = 84
    ME = 423.50; MM = 422.10; MSH = 84
    OE = 192.80; OM = 193.15; OSH = 181   # ORCL EOD long
    AUPL = round((AM - AE) * ASH, 2)
    MUPL = round((ME - MM) * MSH, 2)
    OUPL = round((OM - OE) * OSH, 2)
    # Morning: NVDA +$586.40 + ORCL net -$287.18
    MORN = 166.40 + 420.00 + 125.64 - 412.82

    s["positions"] = [
        {"ticker": "AVGO", "side": "LONG",  "shares": ASH,
         "entry": AE, "mark": AM, "cost": round(AE * ASH, 3),
         "entry_stop": 424.44, "stop": 424.44, "effective_stop": 424.44,
         "risk_dollars": round((AE - 424.44) * ASH, 2),
         "unrealized": AUPL, "unrealized_pnl": AUPL,
         "held_seconds": 900, "entry_ts_utc": "2026-05-16T19:30:00Z",
         "portfolio": "eod", "phase": "A", "eod": True},
        {"ticker": "MSFT", "side": "SHORT", "shares": MSH,
         "entry": ME, "mark": MM, "cost": round(ME * MSH, 3),
         "entry_stop": 425.40, "stop": 425.40, "effective_stop": 425.40,
         "risk_dollars": round((425.40 - ME) * MSH, 2),
         "unrealized": MUPL, "unrealized_pnl": MUPL,
         "held_seconds": 900, "entry_ts_utc": "2026-05-16T19:30:00Z",
         "portfolio": "eod", "phase": "A", "eod": True},
        {"ticker": "ORCL", "side": "LONG",  "shares": OSH,
         "entry": OE, "mark": OM, "cost": round(OE * OSH, 3),
         "entry_stop": 191.80, "stop": 191.80, "effective_stop": 191.80,
         "risk_dollars": round((OE - 191.80) * OSH, 2),
         "unrealized": OUPL, "unrealized_pnl": OUPL,
         "held_seconds": 900, "entry_ts_utc": "2026-05-16T19:30:00Z",
         "portfolio": "eod", "phase": "A", "eod": True},
    ]
    s["trades_today"] = [
        {"action": "BUY",  "ticker": "NVDA", "price": 228.35, "shares": 209, "stop": 226.75, "entry_price": 228.35, "time": "10:08 ET", "date": "2026-05-16", "side": "LONG", "portfolio": "paper"},
        {"action": "BUY",  "ticker": "ORCL", "price": 193.375, "shares": 349, "stop": 192.41, "entry_price": 193.375, "time": "10:26 ET", "date": "2026-05-16", "side": "LONG", "portfolio": "paper"},
        {"action": "SELL", "ticker": "ORCL", "price": 193.735, "shares": 349, "pnl": 125.64, "entry_price": 193.375, "time": "10:48 ET", "date": "2026-05-16", "side": "LONG", "portfolio": "paper"},
        {"action": "BUY",  "ticker": "ORCL", "price": 193.735, "shares": 377, "stop": 192.77, "entry_price": 193.735, "time": "10:48 ET", "date": "2026-05-16", "side": "LONG", "portfolio": "paper"},
        {"action": "SELL", "ticker": "ORCL", "price": 192.64,  "shares": 377, "pnl": -412.82, "entry_price": 193.735, "time": "11:07 ET", "date": "2026-05-16", "side": "LONG", "portfolio": "paper"},
        {"action": "SELL", "ticker": "NVDA", "price": 229.95, "shares": 104, "pnl": 166.40, "entry_price": 228.35, "time": "11:30 ET", "date": "2026-05-16", "side": "LONG", "portfolio": "paper", "reason": "target_1r"},
        {"action": "SELL", "ticker": "NVDA", "price": 232.35, "shares": 105, "pnl": 420.00, "entry_price": 228.35, "time": "12:45 ET", "date": "2026-05-16", "side": "LONG", "portfolio": "paper", "reason": "target_2r"},
        {"action": "BUY",   "ticker": "AVGO", "price": AE, "shares": ASH, "entry_price": AE, "time": "15:30 ET", "date": "2026-05-16", "side": "LONG",  "portfolio": "eod", "eod": True},
        {"action": "SHORT", "ticker": "MSFT", "price": ME, "shares": MSH, "entry_price": ME, "time": "15:30 ET", "date": "2026-05-16", "side": "SHORT", "portfolio": "eod", "eod": True},
        {"action": "BUY",   "ticker": "ORCL", "price": OE, "shares": OSH, "entry_price": OE, "time": "15:30 ET", "date": "2026-05-16", "side": "LONG",  "portfolio": "eod", "eod": True},
    ]
    s["regime"] = {"mode": "POWER", "mode_reason": "Power hour: 3:00-4:00 ET", "breadth": "BULLISH", "breadth_detail": "", "rsi_regime": "NEUTRAL", "rsi_detail": ""}
    s["gates"]["scan_paused"] = True
    s["gates"]["scan_idle_hours"] = False
    s["v10"]["day_status"].update({"session_date": "2026-05-16", "vix_current": 17.82, "block_day": False})
    s["v10"]["or_windows"] = OR_WINDOWS
    s["v10"]["risk_books"] = {
        "main": make_rb(realized=MORN, open_count=0),
        "val":  make_rb(realized=MORN, open_count=0),
        "gene": make_rb(),
    }
    s["v10"]["eod"]["session_date"] = "2026-05-16"
    s["v10"]["eod"]["per_portfolio"] = {
        "main": {"open_count": 3, "open_positions": [
            {"ticker": "AVGO", "side": "LONG",  "shares": ASH, "entry_price": AE, "current_price": AM, "unrealized_pnl": AUPL},
            {"ticker": "MSFT", "side": "SHORT", "shares": MSH, "entry_price": ME, "current_price": MM, "unrealized_pnl": MUPL},
            {"ticker": "ORCL", "side": "LONG",  "shares": OSH, "entry_price": OE, "current_price": OM, "unrealized_pnl": OUPL},
        ], "realized_pnl_today": MORN, "entry_attempted": True, "rejected_count": 0, "closed_legs": []},
        "val":  {"open_count": 3, "open_positions": [
            {"ticker": "AVGO", "side": "LONG",  "shares": ASH, "entry_price": AE, "current_price": AM, "unrealized_pnl": AUPL},
            {"ticker": "MSFT", "side": "SHORT", "shares": MSH, "entry_price": ME, "current_price": MM, "unrealized_pnl": MUPL},
            {"ticker": "ORCL", "side": "LONG",  "shares": OSH, "entry_price": OE, "current_price": OM, "unrealized_pnl": OUPL},
        ], "realized_pnl_today": MORN, "entry_attempted": True, "rejected_count": 0, "closed_legs": []},
        "gene": {"open_count": 0, "open_positions": [], "realized_pnl_today": 0.0, "entry_attempted": False, "rejected_count": 0, "closed_legs": []},
    }
    s["v10"]["activity"] = [
        {"kind": "EOD_ENTRY", "ticker": "AVGO+MSFT+ORCL", "pid": "main", "ts_et": "15:30:00", "detail": "EOD: LONG AVGO 84sh @ 426.03, SHORT MSFT 84sh @ 423.50, LONG ORCL 181sh @ 192.80"},
        {"kind": "EXIT",      "ticker": "NVDA",            "pid": "main", "ts_et": "12:45:00", "detail": "target 2.5R: fill=232.35 pnl=+$420.00 (runner)"},
        {"kind": "EXIT",      "ticker": "NVDA",            "pid": "main", "ts_et": "11:30:00", "detail": "target 1R: fill=229.95 pnl=+$166.40 (partial)"},
        {"kind": "KILL",      "ticker": "",                "pid": "main", "ts_et": "11:07:43", "detail": "daily_loss_kill: ORCL -$287.18 realized"},
        {"kind": "EXIT",      "ticker": "ORCL",            "pid": "main", "ts_et": "11:07:43", "detail": "stop: fill=192.64 pnl=-$412.82"},
        {"kind": "ADMIT",     "ticker": "ORCL",            "pid": "main", "ts_et": "10:48:32", "detail": "LONG 377sh @ 193.735 stop=192.77"},
        {"kind": "EXIT",      "ticker": "ORCL",            "pid": "main", "ts_et": "10:48:30", "detail": "stop: fill=193.735 pnl=+$125.64"},
        {"kind": "ADMIT",     "ticker": "ORCL",            "pid": "main", "ts_et": "10:26:34", "detail": "LONG 349sh @ 193.375 stop=192.41"},
        {"kind": "ADMIT",     "ticker": "NVDA",            "pid": "main", "ts_et": "10:08:15", "detail": "LONG 209sh @ 228.35 stop=226.75"},
        {"kind": "SESSION_START", "ticker": "",            "pid": "main", "ts_et": "09:30:00", "detail": "v10 session started"},
    ]
    total_upl = AUPL + MUPL + OUPL
    total_pnl = round(MORN + total_upl, 2)
    s["portfolio"] = {
        "cash": round(100000 + MORN, 2),
        "long_mv": round(AM * ASH + OM * OSH, 2),
        "short_liab": round(MM * MSH, 2),
        "equity": round(100000 + total_pnl, 2),
        "start": 100000, "vs_start": total_pnl, "day_pnl": total_pnl,
    }
    s["proximity"] = sorted(PROXIMITY_BASE, key=lambda x: x["nearest_pct"])
    s["spy_regime_today"] = SPY_REGIME
    s["_indices"] = FAKE_INDICES_RTH
    return s


def build_and_upload(state: dict, label: str, date: str = "2026-05-16") -> str:
    """Build a single-snapshot HTML and upload to R2, return presigned URL."""
    # Wrap in a single-element diff list for replay_dashboard.build_html
    base_state = dict(state)
    base_state.pop("trades_today", None)
    base_state.pop("positions", None)

    diff = {
        "ts_et":             state["server_time_label"],
        "captured_at_utc":   f"{date}T12:00:00Z",
        "kind":              "",
        "label":             label,
        "diff": {
            "trades_today":      state.get("trades_today") or [],
            "positions":         state.get("positions") or [],
            "server_time":       state.get("server_time", ""),
            "server_time_label": state.get("server_time_label", ""),
            "eod":               state.get("v10", {}).get("eod") or {},
        },
    }

    html = rd.build_html([diff], base_state, start_idx=0)
    body = html.encode("utf-8")
    key  = f"replay/scenario_{label.replace(' ', '_').lower()}.html"
    print(f"  Uploading {label} ({len(body)//1024} KB) -> {key} ...", end=" ", flush=True)
    rd.upload_r2(body, key)
    url = rd.presigned(key, expires=7200)  # 2 hours
    print("done")
    return url


# ---------------------------------------------------------------------------
# Full-day combined replay (single page, time scrubber)
# ---------------------------------------------------------------------------

# Ordered activity events for May 15 — shown in the Recent Activity feed
_ACTIVITY_MAY15 = [
    {"kind":"SESSION_START","ticker":"","pid":"main","ts_et":"09:30:00","time":"09:30","detail":"v10 session started -- LONG_BIAS"},
    {"kind":"OR_LOCK","ticker":"ORCL","pid":"main","ts_et":"10:00:01","time":"10:00","detail":"OR locked: H=194.20 L=191.80 rng=1.24%"},
    {"kind":"OR_LOCK","ticker":"META","pid":"main","ts_et":"10:00:01","time":"10:00","detail":"OR locked: H=614.09 L=609.49 rng=0.75%"},
    {"kind":"OR_LOCK","ticker":"AVGO","pid":"main","ts_et":"10:00:01","time":"10:00","detail":"OR locked: H=432.62 L=424.44 rng=1.93%"},
    {"kind":"OR_LOCK","ticker":"AAPL","pid":"main","ts_et":"10:00:01","time":"10:00","detail":"OR locked: H=302.80 L=298.10 rng=1.57%"},
    {"kind":"OR_LOCK","ticker":"MSFT","pid":"main","ts_et":"10:00:01","time":"10:00","detail":"OR locked: H=425.40 L=420.10 rng=1.26%"},
    {"kind":"OR_LOCK","ticker":"GOOG","pid":"main","ts_et":"10:00:01","time":"10:00","detail":"OR locked: H=393.20 L=389.87 rng=0.85%"},
    {"kind":"OR_LOCK","ticker":"NVDA","pid":"main","ts_et":"10:00:01","time":"10:00","detail":"OR locked: H=230.00 L=226.41 rng=1.58%"},
    {"kind":"OR_LOCK","ticker":"AMZN","pid":"main","ts_et":"10:00:01","time":"10:00","detail":"OR locked: H=267.40 L=261.20 rng=2.37%"},
    {"kind":"OR_LOCK","ticker":"NFLX","pid":"main","ts_et":"10:00:01","time":"10:00","detail":"OR locked: H=89.40 L=85.20 rng=4.93%"},
    {"kind":"OR_LOCK","ticker":"TSLA","pid":"main","ts_et":"10:00:01","time":"10:00","detail":"OR locked: H=428.60 L=418.20 rng=2.49%"},
    {"kind":"REJECT","ticker":"META","pid":"main","ts_et":"10:01:44","time":"10:01","detail":"vwap_chase: 19bps > 15bps gate"},
    {"kind":"REJECT","ticker":"GOOG","pid":"main","ts_et":"10:03:22","time":"10:03","detail":"mbr_reject: break 3bps < 5bps min_break"},
    {"kind":"ADMIT","ticker":"NVDA","pid":"main","ts_et":"10:08:15","time":"10:08","detail":"LONG 209sh @ 228.35 stop=226.75 risk=$334"},
    {"kind":"ADMIT","ticker":"ORCL","pid":"main","ts_et":"10:26:34","time":"10:26","detail":"LONG 349sh @ 193.375 stop=192.41 risk=$337"},
    {"kind":"EXIT","ticker":"ORCL","pid":"main","ts_et":"10:48:30","time":"10:48","detail":"stop: fill=193.735 pnl=+$125.64"},
    {"kind":"ADMIT","ticker":"ORCL","pid":"main","ts_et":"10:48:32","time":"10:48","detail":"LONG 377sh @ 193.735 stop=192.77 risk=$364"},
    {"kind":"EXIT","ticker":"ORCL","pid":"main","ts_et":"11:07:43","time":"11:07","detail":"stop: fill=192.64 pnl=-$412.82"},
    {"kind":"KILL","ticker":"","pid":"main","ts_et":"11:07:43","time":"11:07","detail":"daily_loss_kill: realized -$287.18 (ORCL) | NVDA still running +$255 unr"},
    {"kind":"EXIT","ticker":"NVDA","pid":"main","ts_et":"11:30:00","time":"11:30","detail":"target 1R: fill=229.95 pnl=+$166.40 (104sh partial)"},
    {"kind":"EXIT","ticker":"NVDA","pid":"main","ts_et":"12:45:00","time":"12:45","detail":"target 2.5R: fill=232.35 pnl=+$420.00 (105sh runner)"},
    {"kind":"EOD_ENTRY","ticker":"AVGO+MSFT+ORCL","pid":"main","ts_et":"15:30:00","time":"15:30","detail":"EOD: LONG AVGO 84sh @ 426.03, SHORT MSFT 84sh @ 423.50, LONG ORCL 181sh @ 192.80"},
    {"kind":"EOD_EXIT","ticker":"AVGO+MSFT+ORCL","pid":"main","ts_et":"15:59:00","time":"15:59","detail":"EOD closed: AVGO -$48.71, MSFT +$135.24, ORCL +$108.60"},
]

# May 15 real trades (used for position simulation)
_TRADES_MAY15 = [
    # NVDA: enters 10:08, partial exit at 1R 11:30, runner exits at 2.5R 12:45
    {"action":"BUY",  "ticker":"NVDA","time":"10:08","price":228.35,"shares":209,
     "stop":226.75,"entry_price":228.35,"side":"LONG","portfolio":"paper"},
    {"action":"SELL", "ticker":"NVDA","time":"11:30","price":229.95,"shares":104,
     "pnl":166.40,"entry_price":228.35,"side":"LONG","portfolio":"paper","reason":"target_1r"},
    {"action":"SELL", "ticker":"NVDA","time":"12:45","price":232.35,"shares":105,
     "pnl":420.00,"entry_price":228.35,"side":"LONG","portfolio":"paper","reason":"target_2r"},
    # ORCL round-trip 1
    {"action":"BUY",  "ticker":"ORCL","time":"10:26","price":193.375,"shares":349,
     "stop":192.41,"entry_price":193.375,"side":"LONG","portfolio":"paper"},
    {"action":"SELL", "ticker":"ORCL","time":"10:48","price":193.735,"shares":349,
     "pnl":125.64,"entry_price":193.375,"side":"LONG","portfolio":"paper","reason":"sentinel_a_stop_price"},
    # ORCL round-trip 2
    {"action":"BUY",  "ticker":"ORCL","time":"10:48","price":193.735,"shares":377,
     "stop":192.77,"entry_price":193.735,"side":"LONG","portfolio":"paper"},
    {"action":"SELL", "ticker":"ORCL","time":"11:07","price":192.64,"shares":377,
     "pnl":-412.82,"entry_price":193.735,"side":"LONG","portfolio":"paper","reason":"sentinel_a_stop_price"},
    # EOD: AVGO long + MSFT short + ORCL long
    {"action":"BUY",   "ticker":"AVGO","time":"15:30","price":426.03,"shares":84,
     "entry_price":426.03,"side":"LONG","portfolio":"eod","eod":True},
    {"action":"SHORT", "ticker":"MSFT","time":"15:30","price":423.50,"shares":84,
     "entry_price":423.50,"side":"SHORT","portfolio":"eod","eod":True},
    {"action":"BUY",   "ticker":"ORCL","time":"15:30","price":192.80,"shares":181,
     "entry_price":192.80,"side":"LONG","portfolio":"eod","eod":True},
    {"action":"SELL",  "ticker":"AVGO","time":"15:59","price":425.45,"shares":84,
     "pnl":-48.71,"entry_price":426.03,"side":"LONG","portfolio":"eod","eod":True},
    {"action":"COVER", "ticker":"MSFT","time":"15:59","price":421.89,"shares":84,
     "pnl":135.24,"entry_price":423.50,"side":"SHORT","portfolio":"eod","eod":True},
    {"action":"SELL",  "ticker":"ORCL","time":"15:59","price":193.40,"shares":181,
     "pnl":108.60,"entry_price":192.80,"side":"LONG","portfolio":"eod","eod":True},
]


def _hhmm_to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _interp(a: float, b: float, t: float) -> float:
    """Linear interpolation."""
    return round(a + (b - a) * t, 2)


def _open_positions_at(et_hhmm: str) -> tuple[list[dict], float]:
    """Return (open_positions, realized_pnl) at a given ET time."""
    cur = _hhmm_to_min(et_hhmm)
    positions: list[dict] = []
    realized = 0.0

    def _utc(hhmm_et: str, date: str = "2026-05-15") -> str:
        h, m = map(int, hhmm_et.split(":"))
        uh, um = divmod(h * 60 + m + 240, 60)
        return f"{date}T{uh:02d}:{um:02d}:00Z"

    # NVDA: 10:08-11:30 full (209sh), 11:30-12:45 runner (105sh at BE stop)
    e_n = _hhmm_to_min("10:08")
    x_n1 = _hhmm_to_min("11:30")  # partial exit at 1R
    x_n2 = _hhmm_to_min("12:45")  # runner exits at 2.5R
    if e_n <= cur < x_n1:
        t = (cur - e_n) / (x_n1 - e_n)
        mark = _interp(228.35, 229.95, t)
        upl  = round((mark - 228.35) * 209, 2)
        positions.append({"ticker":"NVDA","side":"LONG","shares":209,
            "entry":228.35,"mark":mark,"cost":round(228.35*209,3),
            "entry_stop":226.75,"stop":226.75,"effective_stop":226.75,
            "unrealized":upl,"unrealized_pnl":upl,"risk_dollars":round((228.35-226.75)*209,2),
            "held_seconds":(cur-e_n)*60,"entry_ts_utc":_utc("10:08"),"phase":"A"})
    elif x_n1 <= cur < x_n2:
        realized += 166.40  # partial exit at 1R
        t = (cur - x_n1) / (x_n2 - x_n1)
        mark = _interp(229.95, 232.35, t)
        upl  = round((mark - 228.35) * 105, 2)
        positions.append({"ticker":"NVDA","side":"LONG","shares":105,
            "entry":228.35,"mark":mark,"cost":round(228.35*105,3),
            "entry_stop":226.75,"stop":228.35,"effective_stop":228.35,  # moved to BE
            "unrealized":upl,"unrealized_pnl":upl,"risk_dollars":0,
            "held_seconds":(cur-e_n)*60,"entry_ts_utc":_utc("10:08"),"phase":"B"})
    elif cur >= x_n2:
        realized += 166.40 + 420.00

    # ORCL T1: 10:26 - 10:48
    e1, x1 = _hhmm_to_min("10:26"), _hhmm_to_min("10:48")
    if e1 <= cur < x1:
        t = (cur - e1) / (x1 - e1)
        mark = _interp(193.375, 193.735, t)
        upl  = round((mark - 193.375) * 349, 2)
        positions.append({"ticker":"ORCL","side":"LONG","shares":349,
            "entry":193.375,"mark":mark,"cost":round(193.375*349,3),
            "entry_stop":192.41,"stop":192.41,"effective_stop":192.41,
            "unrealized":upl,"unrealized_pnl":upl,"risk_dollars":round((193.375-192.41)*349,2),
            "held_seconds":(cur-e1)*60,"entry_ts_utc":_utc("10:26"),"phase":"A"})
    elif cur >= x1:
        realized += 125.64

    # ORCL T2: 10:48 - 11:07
    e2, x2 = _hhmm_to_min("10:48"), _hhmm_to_min("11:07")
    if e2 <= cur < x2:
        t = (cur - e2) / (x2 - e2)
        mark = _interp(193.735, 192.64, t)
        upl  = round((mark - 193.735) * 377, 2)
        positions.append({"ticker":"ORCL","side":"LONG","shares":377,
            "entry":193.735,"mark":mark,"cost":round(193.735*377,3),
            "entry_stop":192.77,"stop":192.77,"effective_stop":192.77,
            "unrealized":upl,"unrealized_pnl":upl,"risk_dollars":round((193.735-192.77)*377,2),
            "held_seconds":(cur-e2)*60,"entry_ts_utc":_utc("10:48"),"phase":"A"})
    elif cur >= x2:
        realized += -412.82

    # EOD: 15:30-15:59 — AVGO long + MSFT short + ORCL long
    # entry_stop = OR-low for longs / OR-high for shorts so the progress bar renders
    e3, x3 = _hhmm_to_min("15:30"), _hhmm_to_min("15:59")
    if e3 <= cur < x3:
        t = (cur - e3) / (x3 - e3)
        avgo_m = _interp(426.03, 425.45, t)
        msft_m = _interp(423.50, 421.89, t)
        orcl_m = _interp(192.80, 193.40, t)
        aupl = round((avgo_m - 426.03) * 84, 2)
        mupl = round((423.50 - msft_m) * 84, 2)
        oupl = round((orcl_m - 192.80) * 181, 2)
        # AVGO LONG: stop at OR-low 424.44, target at OR-high 432.62
        positions.append({"ticker":"AVGO","side":"LONG","shares":84,
            "entry":426.03,"mark":avgo_m,"cost":round(426.03*84,3),
            "entry_stop":424.44,"stop":424.44,"effective_stop":424.44,
            "unrealized":aupl,"unrealized_pnl":aupl,
            "risk_dollars":round((426.03-424.44)*84,2),
            "held_seconds":(cur-e3)*60,"entry_ts_utc":_utc("15:30"),
            "phase":"A","eod":True})
        # MSFT SHORT: stop at OR-high 425.40, target at OR-low 420.10
        positions.append({"ticker":"MSFT","side":"SHORT","shares":84,
            "entry":423.50,"mark":msft_m,"cost":round(423.50*84,3),
            "entry_stop":425.40,"stop":425.40,"effective_stop":425.40,
            "unrealized":mupl,"unrealized_pnl":mupl,
            "risk_dollars":round((425.40-423.50)*84,2),
            "held_seconds":(cur-e3)*60,"entry_ts_utc":_utc("15:30"),
            "phase":"A","eod":True})
        # ORCL LONG: stop at OR-low 191.80, target at OR-high 194.20
        positions.append({"ticker":"ORCL","side":"LONG","shares":181,
            "entry":192.80,"mark":orcl_m,"cost":round(192.80*181,3),
            "entry_stop":191.80,"stop":191.80,"effective_stop":191.80,
            "unrealized":oupl,"unrealized_pnl":oupl,
            "risk_dollars":round((192.80-191.80)*181,2),
            "held_seconds":(cur-e3)*60,"entry_ts_utc":_utc("15:30"),
            "phase":"A","eod":True})
    elif cur >= x3:
        realized += -48.71 + 135.24 + 108.60

    return positions, round(realized, 2)


def build_full_day(base: dict, date: str = "2026-05-15") -> list[dict]:
    """Generate 5-min snapshots + trade events for the full May 15 trading day."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
    snapshots: list[dict] = []
    seen: set[str] = set()

    # Every 5 minutes 9:30 -> 16:00
    t = datetime(2026, 5, 15, 9, 30, tzinfo=ET)
    end = datetime(2026, 5, 15, 16, 5, tzinfo=ET)
    times = []
    while t <= end:
        times.append(t.strftime("%H:%M"))
        t += timedelta(minutes=5)

    # Add trade event times
    for tr in _TRADES_MAY15:
        times.append(tr["time"])

    for hhmm in sorted(set(times)):
        if hhmm in seen:
            continue
        seen.add(hhmm)

        cur_min = _hhmm_to_min(hhmm)
        positions, realized = _open_positions_at(hhmm)

        # Determine kind from trade events at this exact time
        kind, label = "", ""
        for tr in _TRADES_MAY15:
            if tr["time"] == hhmm:
                act = tr["action"]
                if act in ("BUY", "SHORT"):
                    kind, label = "entry", f"{act} {tr['ticker']}"
                elif act in ("SELL", "COVER"):
                    pnl = tr.get("pnl", 0)
                    pnl_s = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
                    kind = "exit_win" if pnl >= 0 else "exit_loss"
                    label = f"{act} {tr['ticker']} {pnl_s}"

        # trades_today: all trades up to this time
        trades = [dict(t, **{"time": t["time"] + " ET", "date": date})
                  for t in _TRADES_MAY15 if t["time"] <= hhmm]

        # Regime mode
        if cur_min < 600:
            mode, mode_reason = "OR", "Opening Range: 9:30-10:00 ET"
        elif cur_min < 900:
            mode, mode_reason = "OPEN", "RTH open"
        else:
            mode, mode_reason = "POWER", "Power hour"

        # Scan paused after daily kill at 11:07
        scan_paused = cur_min >= _hhmm_to_min("11:07")

        # Admit/reject counts build up as events fire
        # Cumulative admit/reject counts (for risk_book display)
        admit_count  = 0
        if cur_min >= _hhmm_to_min("10:08"): admit_count  = 1  # NVDA
        if cur_min >= _hhmm_to_min("10:26"): admit_count  = 2  # ORCL T1
        if cur_min >= _hhmm_to_min("10:48"): admit_count  = 3  # ORCL T2
        reject_count = 0
        if cur_min >= _hhmm_to_min("10:01"): reject_count = 1
        if cur_min >= _hhmm_to_min("10:03"): reject_count = 2

        # v10.day_states: per-ticker FSM admit counts for TRADES TODAY gauge.
        # Accumulates: NVDA=1 always, ORCL=1 after 10:26 then 2 after 10:48.
        day_states_snap = []
        if cur_min >= _hhmm_to_min("10:08"):
            day_states_snap.append({"portfolio_id":"main","ticker":"NVDA",
                                    "trades_today":1,"side":"LONG","phase":"A"})
        if cur_min >= _hhmm_to_min("10:26"):
            orcl_admits = 1
            if cur_min >= _hhmm_to_min("10:48"): orcl_admits = 2
            day_states_snap.append({"portfolio_id":"main","ticker":"ORCL",
                                    "trades_today":orcl_admits,"side":"LONG","phase":"A"})

        # Total unrealized
        total_upl = sum(p["unrealized"] for p in positions)
        day_pnl   = round(realized + total_upl, 2)
        equity    = round(100000 + day_pnl, 2)

        # Build state snapshot
        s = copy.deepcopy(base)
        s["server_time"] = f"{date}T{int(hhmm[:2])+4:02d}:{hhmm[3:]}:00Z"  # approx UTC
        s["server_time_label"] = f"Fri May 15 | {hhmm}:00 ET"
        s["positions"] = positions
        s["trades_today"] = trades
        s["regime"] = {"mode": mode, "mode_reason": mode_reason, "breadth": "BULLISH",
                       "breadth_detail": "", "rsi_regime": "NEUTRAL", "rsi_detail": ""}
        s["gates"]["scan_paused"] = scan_paused
        s["gates"]["scan_idle_hours"] = False
        s["portfolio"] = {"cash": 100000.0, "long_mv": sum(p["mark"]*p["shares"] for p in positions if p["side"]=="LONG"),
                          "short_liab": sum(p["mark"]*p["shares"] for p in positions if p["side"]=="SHORT"),
                          "equity": equity, "start": 100000, "vs_start": day_pnl, "day_pnl": day_pnl}
        s["v10"]["day_status"].update({"session_date": date, "vix_current": 17.82, "block_day": False})
        s["v10"]["or_windows"] = OR_WINDOWS
        s["v10"]["risk_books"]["main"]["realized_pnl_today"] = realized
        s["v10"]["risk_books"]["main"]["daily_kill_triggered"] = scan_paused
        s["spy_regime_today"] = SPY_REGIME
        s["proximity"] = sorted(PROXIMITY_BASE, key=lambda x: x["nearest_pct"])
        s["_indices"] = FAKE_INDICES_RTH

        h_utc, m_utc = divmod(_hhmm_to_min(hhmm) + 240, 60)  # ET -> UTC (+4h)
        utc_iso = f"{date}T{h_utc:02d}:{m_utc:02d}:00Z"

        # Activity feed: all events up to this time
        activity = [e for e in _ACTIVITY_MAY15 if e["time"] <= hhmm]
        # Newest first (matches dashboard convention)
        activity = list(reversed(activity))

        snapshots.append({
            "ts_et": f"{date}T{hhmm}:00 ET",
            "captured_at_utc": utc_iso,
            "kind": kind,
            "label": label,
            "diff": {
                "trades_today":       trades,
                "positions":          positions,
                "server_time":        s["server_time"],
                "server_time_label":  s["server_time_label"],
                "eod":                s["v10"]["eod"],
                # Time-varying overrides applied by currentState() in JS
                "portfolio":          s["portfolio"],
                "regime":             s["regime"],
                "v10_activity":       activity,
                "gates_scan_paused":  scan_paused,
                "v10_kill_triggered": scan_paused,  # kill fires at same time as scan pause
                "v10_realized_pnl":   realized,     # for DAILY-KILL gauge
                "v10_admit_count":    admit_count,       # risk_book counter (concurrent risk display)
                "v10_reject_count":   reject_count,      # risk_book counter
                "v10_day_states":     day_states_snap,   # TRADES TODAY / top-ticker gauge
            },
            "_full_state": s,
        })

    return snapshots


def build_and_upload_combined(snapshots: list[dict]) -> str:
    """Build a single HTML with all snapshots and the scrubber UI."""
    # Use midday state as the base (most complete OR data)
    mid = next((s for s in snapshots if "12:00" in s["ts_et"]), snapshots[len(snapshots)//2])
    base_state = dict(mid["_full_state"])
    base_state.pop("trades_today", None)
    base_state.pop("positions", None)

    # Convert to the diff format build_html expects
    diffs = []
    for s in snapshots:
        d = dict(s)
        d.pop("_full_state", None)
        diffs.append(d)

    # Also extend the diff format to include portfolio/regime overrides
    import scripts.replay_dashboard as rd

    # Patch build_html to pass extra diff fields through
    slim_diffs = []
    for d in diffs:
        diff_inner = d.get("diff", {})
        slim_diffs.append({
            "ts_et":             d["ts_et"],
            "captured_at_utc":   d["captured_at_utc"],
            "kind":              d.get("kind", ""),
            "label":             d.get("label", ""),
            "trades_today":      diff_inner.get("trades_today", []),
            "positions":         diff_inner.get("positions", []),
            "server_time":       diff_inner.get("server_time", ""),
            "server_time_label": diff_inner.get("server_time_label", ""),
            "eod":               diff_inner.get("eod", {}),
            "portfolio":          diff_inner.get("portfolio", {}),
            "regime":             diff_inner.get("regime", {}),
            "v10_activity":       diff_inner.get("v10_activity", []),
            "gates_scan_paused":  diff_inner.get("gates_scan_paused", False),
            "v10_kill_triggered": diff_inner.get("v10_kill_triggered", False),
            "v10_realized_pnl":   diff_inner.get("v10_realized_pnl",   0.0),
            "v10_admit_count":    diff_inner.get("v10_admit_count",    0),
            "v10_reject_count":   diff_inner.get("v10_reject_count",   0),
            "v10_day_states":     diff_inner.get("v10_day_states",     []),
        })

    # Build HTML using the internal helpers directly
    app_js   = (rd.STATIC / "app.js").read_text(encoding="utf-8")
    app_css  = (rd.STATIC / "app.css").read_text(encoding="utf-8")
    html     = (rd.STATIC / "index.html").read_text(encoding="utf-8")
    import re
    html = html.replace('<link rel="stylesheet" href="/static/app.css">', f"<style>{app_css}</style>")
    html = re.sub(r'src="/static/app\.js[^"]*"', '', html)

    replay_css = "<style id='__tt_css'>body{margin-top:84px!important}</style>\n"
    head_inject = (
        replay_css +
        f"<script>\n"
        f"window.__TT_BASE={rd._js(base_state)};\n"
        f"window.__TT_DIFFS={rd._js(slim_diffs)};\n"
        f"window.__TT_IDX=0;\n"
        f"</script>\n" +
        rd._HEAD_PATCH
    )
    html = html.replace("</head>", head_inject + "</head>", 1)

    # Static bar (scrubber)
    bar = rd._bar_html(slim_diffs, 0)
    html = re.sub(r"(<body[^>]*>)", r"\1" + bar, html, count=1)

    app_js_safe = app_js.replace("</script>", "<\\/script>")
    html = html.replace("</body>", f"<script>\n{app_js_safe}\n</script>\n{rd._NAV_SCRIPT}\n</body>", 1)

    body = html.encode("utf-8")
    key  = "replay/full_day_2026-05-15.html"
    print(f"  Uploading combined ({len(body)//1024} KB) -> {key} ...", end=" ", flush=True)
    rd.upload_r2(body, key)
    url = rd.presigned(key, expires=7200)
    print("done")
    return url


if __name__ == "__main__":
    print("Fetching base state from production...")
    base = fetch_base()
    print(f"  v{base.get('version')}")

    print("\nBuilding scenarios...")
    s1 = build_scenario_1(base)
    s2 = build_scenario_2(base)
    s3 = build_scenario_3(base)

    print("\nUploading to R2 (2-hour URLs)...")
    url1 = build_and_upload(s1, "off_market")
    url2 = build_and_upload(s2, "morning")
    url3 = build_and_upload(s3, "eod")

    print("\n--- Building full-day combined replay ---")
    snapshots = build_full_day(base)
    print(f"  {len(snapshots)} snapshots")
    url_combined = build_and_upload_combined(snapshots)

    print("\n" + "=" * 60)
    print("SCENARIO URLS (2 hours)")
    print("=" * 60)
    print(f"\n1. OFF-MARKET:\n{url1}\n")
    print(f"2. MORNING:\n{url2}\n")
    print(f"3. EOD:\n{url3}\n")
    print(f"4. COMBINED FULL DAY (scrubber + playback):\n{url_combined}\n")
