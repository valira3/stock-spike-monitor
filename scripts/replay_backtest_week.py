#!/usr/bin/env python3
"""replay_backtest_week.py -- counterfactual weekly replay viewer.

Produces a self-contained HTML showing how the current Keystone (v9.1.114)
algorithm would have traded the past trading week against real SIP bars,
for both Main and Val portfolios. Date dropdown defaults to the most
recent day; user can switch days and portfolios client-side.

Reads:
    data/<DATE>/<TICKER>.jsonl                    -- 1-min RTH bars
    results/week_replay/<portfolio>_morning/per_day/<DATE>.json
    results/week_replay/<portfolio>_eod/per_day/<DATE>.json

Produces:
    replay_week.html  -- standalone, no server needed.

Usage:
    python tools/orb_backtest.py ...   # one per (portfolio, leg) -- run first
    python scripts/replay_backtest_week.py \\
        --out replay_week.html \\
        --dates 2026-05-11,2026-05-12,2026-05-13,2026-05-14,2026-05-15
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")

TICKERS = (
    "AAPL", "AMZN", "AVGO", "GOOG", "META", "MSFT",
    "NFLX", "NVDA", "ORCL", "QQQ", "SPY", "TSLA",
)


def _et_min(iso_ts: str) -> int:
    dt = datetime.fromisoformat(iso_ts)
    et = dt.astimezone(ET)
    return et.hour * 60 + et.minute


def _load_bars(date_str: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for tk in TICKERS:
        f = REPO / "data" / date_str / f"{tk}.jsonl"
        if not f.exists():
            out[tk] = []
            continue
        bars: list[dict] = []
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            try:
                b = json.loads(line)
            except json.JSONDecodeError:
                continue
            dt_utc = datetime.fromisoformat(b["ts"])
            et = dt_utc.astimezone(ET)
            m = et.hour * 60 + et.minute
            if m < 570 or m > 960:
                continue
            bars.append({
                "t": m,
                "o": round(float(b.get("open") or 0), 4),
                "h": round(float(b.get("high") or 0), 4),
                "l": round(float(b.get("low") or 0), 4),
                "c": round(float(b.get("close") or 0), 4),
                "v": int(b.get("total_volume") or 0),
            })
        out[tk] = bars
    return out


def _load_trades(date_str: str, portfolio: str) -> list[dict]:
    trades: list[dict] = []
    for leg in ("morning", "eod"):
        f = REPO / "results" / "week_replay" / f"{portfolio}_{leg}" / "per_day" / f"{date_str}.json"
        if not f.exists():
            continue
        try:
            d = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        for p in d.get("pnl_pairs", []):
            # Morning schema uses entry_ts / exit_ts (ISO-8601).
            # EOD schema uses entry_bucket / exit_bucket (minutes past midnight ET).
            if "entry_ts" in p:
                entry_min = _et_min(p["entry_ts"])
                exit_min = _et_min(p["exit_ts"])
            else:
                entry_min = int(p["entry_bucket"])
                exit_min = int(p["exit_bucket"])
            tr = {
                "leg": leg,
                "ticker": p["ticker"],
                "side": p["side"],
                "entry_min": entry_min,
                "exit_min": exit_min,
                "entry_price": round(float(p["entry_price"]), 4),
                "exit_price": round(float(p["exit_price"]), 4),
                "shares": int(p["shares"]),
                "pnl": round(float(p["pnl_dollars"]), 2),
                "exit_reason": p.get("exit_reason", "?"),
                "stop_price": round(float(p.get("stop_price") or 0), 4),
            }
            if leg == "morning":
                tr["or_high"] = round(float(p.get("or_high") or 0), 4)
                tr["or_low"] = round(float(p.get("or_low") or 0), 4)
            trades.append(tr)
    # Sort by entry minute, earliest first.
    trades.sort(key=lambda x: (x["entry_min"], x["ticker"]))
    return trades


def _day_summary(date_str: str, portfolio: str, trades: list[dict]) -> dict:
    """Day-level summary derived from trades + the backtest summary blocks."""
    summary = {"morning_pnl": 0.0, "morning_n": 0, "eod_pnl": 0.0, "eod_n": 0}
    for t in trades:
        if t["leg"] == "morning":
            summary["morning_pnl"] += t["pnl"]
            summary["morning_n"] += 1
        else:
            summary["eod_pnl"] += t["pnl"]
            summary["eod_n"] += 1
    summary["day_pnl"] = round(summary["morning_pnl"] + summary["eod_pnl"], 2)
    summary["morning_pnl"] = round(summary["morning_pnl"], 2)
    summary["eod_pnl"] = round(summary["eod_pnl"], 2)
    return summary


def build_data(dates: list[str]) -> dict:
    out: dict = {
        "dates": dates,
        "default_date": dates[-1],
        "portfolios": ["main", "val"],
        "tickers": list(TICKERS),
        "bars": {},
        "trades": {},
        "summary": {},
    }
    for d in dates:
        out["bars"][d] = _load_bars(d)
        out["trades"][d] = {}
        out["summary"][d] = {}
        for p in ("main", "val"):
            trades = _load_trades(d, p)
            out["trades"][d][p] = trades
            out["summary"][d][p] = _day_summary(d, p, trades)
    return out


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>TradeGenius Week Replay -- Counterfactual (v9.1.114)</title>
<style>
  :root {
    --bg: #0e1318; --panel: #151c24; --border: #2a3441; --text: #e6edf3;
    --muted: #94a3b8; --accent: #38bdf8; --green: #22c55e; --red: #ef4444;
    --yellow: #fbbf24; --purple: #a78bfa;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font: 13px system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); }
  header { background: var(--panel); border-bottom: 1px solid var(--border); padding: 10px 16px; display: flex; align-items: center; gap: 14px; flex-wrap: wrap; position: sticky; top: 0; z-index: 10; }
  header h1 { font-size: 14px; margin: 0; font-weight: 600; color: var(--accent); }
  header .sub { color: var(--muted); font-size: 11px; }
  select, button { background: #1f2937; color: var(--text); border: 1px solid var(--border); padding: 5px 10px; border-radius: 4px; font: inherit; cursor: pointer; }
  select:hover, button:hover { background: #2a3441; }
  .tabs { display: flex; gap: 4px; }
  .tab { padding: 5px 14px; border-radius: 4px; background: #1f2937; border: 1px solid var(--border); cursor: pointer; user-select: none; }
  .tab.active { background: var(--accent); color: #0e1318; border-color: var(--accent); font-weight: 600; }
  .summary { display: flex; gap: 14px; padding: 8px 16px; background: var(--panel); border-bottom: 1px solid var(--border); flex-wrap: wrap; font-size: 12px; }
  .summary .kpi { display: flex; flex-direction: column; min-width: 80px; }
  .summary .kpi .k { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; }
  .summary .kpi .v { font-size: 16px; font-weight: 600; }
  .summary .pos { color: var(--green); }
  .summary .neg { color: var(--red); }
  .scrubber { padding: 10px 16px; background: var(--panel); border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 12px; position: sticky; top: 48px; z-index: 9; }
  .scrubber input[type=range] { flex: 1; }
  .scrubber .time { font-family: ui-monospace, monospace; font-size: 13px; font-weight: 600; color: var(--accent); min-width: 70px; }
  .scrubber .controls { display: flex; gap: 4px; }
  main { padding: 12px 16px; display: grid; grid-template-columns: 280px 1fr; gap: 14px; }
  .trades-panel { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; align-self: start; position: sticky; top: 130px; max-height: calc(100vh - 150px); overflow-y: auto; }
  .trades-panel h2 { margin: 0; padding: 8px 12px; font-size: 12px; text-transform: uppercase; color: var(--muted); border-bottom: 1px solid var(--border); background: #1f2937; }
  .trade { padding: 8px 12px; border-bottom: 1px solid var(--border); font-size: 11px; }
  .trade.future { opacity: 0.35; }
  .trade.active { background: rgba(56, 189, 248, 0.08); border-left: 2px solid var(--accent); }
  .trade .row1 { display: flex; justify-content: space-between; align-items: center; }
  .trade .ticker { font-weight: 600; font-size: 13px; }
  .trade .side-long { color: var(--green); }
  .trade .side-short { color: var(--red); }
  .trade .leg-tag { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 9px; font-weight: 600; margin-left: 4px; }
  .trade .leg-morning { background: rgba(56, 189, 248, 0.15); color: var(--accent); }
  .trade .leg-eod { background: rgba(167, 139, 250, 0.15); color: var(--purple); }
  .trade .pnl { font-family: ui-monospace, monospace; font-weight: 600; }
  .trade .meta { color: var(--muted); font-size: 10px; margin-top: 3px; }
  .charts { display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 10px; }
  .chart { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
  .chart .hd { padding: 6px 10px; display: flex; justify-content: space-between; font-size: 11px; background: #1f2937; border-bottom: 1px solid var(--border); }
  .chart .hd .tk { font-weight: 600; font-size: 13px; }
  .chart .hd .px { font-family: ui-monospace, monospace; color: var(--muted); }
  .chart canvas { display: block; width: 100%; height: 200px; background: #0a0f14; }
  .empty-msg { padding: 24px; text-align: center; color: var(--muted); font-style: italic; }
  footer { padding: 10px 16px; text-align: center; color: var(--muted); font-size: 10px; border-top: 1px solid var(--border); }
</style>
</head>
<body>

<header>
  <h1>WEEK REPLAY</h1>
  <span class="sub">counterfactual · real SIP bars · Keystone v9.1.114</span>
  <label>Date <select id="dateSel"></select></label>
  <div class="tabs">
    <div class="tab active" data-portfolio="main">Main · $100k</div>
    <div class="tab" data-portfolio="val">Val · $30,185</div>
  </div>
</header>

<div class="summary" id="summary"></div>

<div class="scrubber">
  <button id="playBtn">▶</button>
  <input type="range" id="scrub" min="570" max="960" step="1" value="960">
  <span class="time" id="timeLabel">16:00 ET</span>
  <div class="controls">
    <button id="speedBtn">3×</button>
    <button id="jumpOpen">9:30</button>
    <button id="jumpEntry">10:30</button>
    <button id="jumpEod">15:30</button>
    <button id="jumpClose">16:00</button>
  </div>
</div>

<main>
  <aside class="trades-panel" id="tradesPanel"><h2>Trades</h2><div id="tradesList"></div></aside>
  <section class="charts" id="charts"></section>
</main>

<footer>
  Generated by <code>scripts/replay_backtest_week.py</code>.
  Trades simulated by <code>tools/orb_backtest.py</code> + <code>tools/afternoon_backtest.py</code>
  against real Alpaca SIP 1-min bars. Keystone v9.1.114 levers (15bps VWAP gate, VIX≤25, sym-10m cooldown, TSLA in EOD fence).
</footer>

<script id="dataPayload" type="application/json">__DATA__</script>
<script>
(function () {
  'use strict';
  var DATA = JSON.parse(document.getElementById('dataPayload').textContent);
  var state = {
    date: DATA.default_date,
    portfolio: 'main',
    minute: 960,
    playing: false,
    speedIdx: 2,
    timer: null,
  };
  var SPEEDS = [400, 150, 50];  // ms per minute step
  var X_MIN = 570, X_MAX = 960;  // 9:30 to 16:00 ET

  // ---- DOM refs ----
  var $ = function (id) { return document.getElementById(id); };
  var dateSel = $('dateSel');
  var scrub = $('scrub');
  var timeLabel = $('timeLabel');
  var summary = $('summary');
  var charts = $('charts');
  var tradesList = $('tradesList');
  var playBtn = $('playBtn');
  var speedBtn = $('speedBtn');

  // ---- Populate date dropdown ----
  DATA.dates.forEach(function (d) {
    var opt = document.createElement('option');
    opt.value = d;
    var dt = new Date(d + 'T12:00:00');
    var DAYS = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    var MOS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    opt.textContent = DAYS[dt.getDay()] + ' ' + MOS[dt.getMonth()] + ' ' + dt.getDate();
    if (d === state.date) opt.selected = true;
    dateSel.appendChild(opt);
  });

  // ---- Format helpers ----
  function fmtMin(m) {
    var h = Math.floor(m / 60), mm = m % 60;
    return (h < 10 ? '0' : '') + h + ':' + (mm < 10 ? '0' : '') + mm;
  }
  function fmtUsd(n) {
    var s = n >= 0 ? '+' : '-';
    return s + '$' + Math.abs(n).toFixed(0).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }
  function pnlClass(n) { return n > 0 ? 'pos' : (n < 0 ? 'neg' : ''); }

  // ---- Render summary ----
  function renderSummary() {
    var s = DATA.summary[state.date][state.portfolio];
    var startEq = state.portfolio === 'main' ? 100000 : 30185;
    summary.innerHTML =
      '<div class="kpi"><div class="k">Starting Equity</div><div class="v">$' + startEq.toLocaleString() + '</div></div>' +
      '<div class="kpi"><div class="k">Morning P&amp;L</div><div class="v ' + pnlClass(s.morning_pnl) + '">' + fmtUsd(s.morning_pnl) + '</div></div>' +
      '<div class="kpi"><div class="k">EOD P&amp;L</div><div class="v ' + pnlClass(s.eod_pnl) + '">' + fmtUsd(s.eod_pnl) + '</div></div>' +
      '<div class="kpi"><div class="k">Day P&amp;L</div><div class="v ' + pnlClass(s.day_pnl) + '">' + fmtUsd(s.day_pnl) + '</div></div>' +
      '<div class="kpi"><div class="k">Trades (M / EOD)</div><div class="v">' + s.morning_n + ' / ' + s.eod_n + '</div></div>' +
      '<div class="kpi"><div class="k">Return</div><div class="v ' + pnlClass(s.day_pnl) + '">' + (s.day_pnl / startEq * 100).toFixed(2) + '%</div></div>';
  }

  // ---- Render trades list ----
  function renderTrades() {
    var trades = DATA.trades[state.date][state.portfolio];
    if (!trades.length) {
      tradesList.innerHTML = '<div class="empty-msg">No trades fired for ' + state.portfolio + ' on ' + state.date + '.</div>';
      return;
    }
    var html = '';
    trades.forEach(function (t) {
      var status = 'future';
      if (state.minute >= t.exit_min) status = 'closed';
      else if (state.minute >= t.entry_min) status = 'active';
      html += '<div class="trade ' + status + '">';
      html += '<div class="row1">';
      html += '<span class="ticker">' + t.ticker + '</span>';
      html += '<span class="side-' + t.side + '">' + t.side.toUpperCase() + '</span>';
      html += '<span class="leg-tag leg-' + t.leg + '">' + t.leg.toUpperCase() + '</span>';
      html += '<span class="pnl ' + pnlClass(t.pnl) + '">' + (status === 'future' ? '...' : fmtUsd(t.pnl)) + '</span>';
      html += '</div>';
      html += '<div class="meta">';
      html += fmtMin(t.entry_min) + ' @ $' + t.entry_price + ' → ' + fmtMin(t.exit_min) + ' @ $' + t.exit_price;
      html += ' · ' + t.shares + 'sh · ' + t.exit_reason;
      html += '</div>';
      html += '</div>';
    });
    tradesList.innerHTML = html;
  }

  // ---- Draw one chart ----
  function drawChart(canvas, ticker) {
    var bars = (DATA.bars[state.date][ticker] || []);
    if (!bars.length) return;
    var trades = DATA.trades[state.date][state.portfolio].filter(function (t) {
      return t.ticker === ticker;
    });
    var dpr = window.devicePixelRatio || 1;
    var W = canvas.clientWidth, H = canvas.clientHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    var ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    var PAD_L = 50, PAD_R = 8, PAD_T = 6, PAD_B = 14;
    var plotW = W - PAD_L - PAD_R;
    var plotH = H - PAD_T - PAD_B;

    // Y range from bars
    var yMin = Infinity, yMax = -Infinity;
    bars.forEach(function (b) {
      if (b.t > state.minute + 30) return;  // small look-ahead for stability
      yMin = Math.min(yMin, b.l);
      yMax = Math.max(yMax, b.h);
    });
    trades.forEach(function (t) {
      [t.entry_price, t.exit_price, t.stop_price].forEach(function (p) {
        if (p > 0) { yMin = Math.min(yMin, p); yMax = Math.max(yMax, p); }
      });
      if (t.or_high) { yMin = Math.min(yMin, t.or_high); yMax = Math.max(yMax, t.or_high); }
      if (t.or_low)  { yMin = Math.min(yMin, t.or_low);  yMax = Math.max(yMax, t.or_low);  }
    });
    if (!isFinite(yMin) || !isFinite(yMax) || yMin === yMax) return;
    var yPad = (yMax - yMin) * 0.05; yMin -= yPad; yMax += yPad;

    var xOf = function (m) { return PAD_L + (m - X_MIN) / (X_MAX - X_MIN) * plotW; };
    var yOf = function (p) { return PAD_T + (1 - (p - yMin) / (yMax - yMin)) * plotH; };

    // Background grid
    ctx.strokeStyle = '#1a2128'; ctx.lineWidth = 1;
    [10, 11, 12, 13, 14, 15].forEach(function (h) {
      var x = xOf(h * 60);
      ctx.beginPath(); ctx.moveTo(x, PAD_T); ctx.lineTo(x, PAD_T + plotH); ctx.stroke();
    });
    ctx.fillStyle = '#475569'; ctx.font = '9px ui-monospace, monospace'; ctx.textAlign = 'center';
    [10, 11, 12, 13, 14, 15, 16].forEach(function (h) {
      ctx.fillText((h < 10 ? '0' : '') + h + ':00', xOf(h * 60), H - 3);
    });
    // Price scale
    ctx.textAlign = 'right';
    var nTicks = 5;
    for (var i = 0; i <= nTicks; i++) {
      var p = yMin + (yMax - yMin) * (i / nTicks);
      var y = yOf(p);
      ctx.strokeStyle = '#1a2128';
      ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(PAD_L + plotW, y); ctx.stroke();
      ctx.fillStyle = '#475569';
      ctx.fillText('$' + p.toFixed(2), PAD_L - 4, y + 3);
    }

    // OR window highlight (9:30 - 10:00)
    ctx.fillStyle = 'rgba(56, 189, 248, 0.04)';
    ctx.fillRect(xOf(570), PAD_T, xOf(600) - xOf(570), plotH);

    // EOD entry window highlight (15:00 - 15:58)
    ctx.fillStyle = 'rgba(167, 139, 250, 0.04)';
    ctx.fillRect(xOf(900), PAD_T, xOf(958) - xOf(900), plotH);

    // Draw candles up to scrubber position
    bars.forEach(function (b) {
      if (b.t > state.minute) return;
      var x = xOf(b.t);
      var bw = Math.max(1, plotW / (X_MAX - X_MIN) * 0.7);
      var up = b.c >= b.o;
      ctx.strokeStyle = up ? '#22c55e' : '#ef4444';
      ctx.beginPath(); ctx.moveTo(x, yOf(b.h)); ctx.lineTo(x, yOf(b.l)); ctx.stroke();
      ctx.fillStyle = up ? '#22c55e' : '#ef4444';
      var top = yOf(Math.max(b.o, b.c)), bot = yOf(Math.min(b.o, b.c));
      ctx.fillRect(x - bw / 2, top, bw, Math.max(1, bot - top));
    });

    // Overlay trade markers (entry / exit / stop / OR boundaries)
    trades.forEach(function (t) {
      var inWindow = state.minute >= t.entry_min;
      if (!inWindow) return;
      var x1 = xOf(t.entry_min);
      var x2 = xOf(Math.min(state.minute, t.exit_min));
      // OR boundaries (morning only)
      if (t.leg === 'morning') {
        if (t.or_high) {
          ctx.strokeStyle = '#64748b'; ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(xOf(570), yOf(t.or_high)); ctx.lineTo(xOf(600), yOf(t.or_high)); ctx.stroke();
        }
        if (t.or_low) {
          ctx.strokeStyle = '#64748b'; ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(xOf(570), yOf(t.or_low)); ctx.lineTo(xOf(600), yOf(t.or_low)); ctx.stroke();
        }
      }
      ctx.setLineDash([]);
      // Stop line during trade lifespan
      if (t.stop_price && t.leg === 'morning') {
        ctx.strokeStyle = '#ef4444'; ctx.lineWidth = 1.2; ctx.setLineDash([5, 3]);
        ctx.beginPath(); ctx.moveTo(x1, yOf(t.stop_price)); ctx.lineTo(x2, yOf(t.stop_price)); ctx.stroke();
        ctx.setLineDash([]);
      }
      // Entry line
      ctx.strokeStyle = t.side === 'long' ? '#22c55e' : '#ef4444';
      ctx.lineWidth = 1.5; ctx.setLineDash([2, 3]);
      ctx.beginPath(); ctx.moveTo(x1, yOf(t.entry_price)); ctx.lineTo(x2, yOf(t.entry_price)); ctx.stroke();
      ctx.setLineDash([]);
      // Entry marker (triangle)
      var ye = yOf(t.entry_price);
      ctx.fillStyle = t.side === 'long' ? '#22c55e' : '#ef4444';
      ctx.beginPath();
      if (t.side === 'long') {
        ctx.moveTo(x1, ye + 7); ctx.lineTo(x1 - 4, ye + 13); ctx.lineTo(x1 + 4, ye + 13);
      } else {
        ctx.moveTo(x1, ye - 7); ctx.lineTo(x1 - 4, ye - 13); ctx.lineTo(x1 + 4, ye - 13);
      }
      ctx.closePath(); ctx.fill();
      // Exit marker (circle) if exit has occurred by scrubber position
      if (state.minute >= t.exit_min) {
        var ex = xOf(t.exit_min);
        var exitY = yOf(t.exit_price);
        ctx.fillStyle = t.pnl > 0 ? '#22c55e' : '#ef4444';
        ctx.beginPath(); ctx.arc(ex, exitY, 4, 0, Math.PI * 2); ctx.fill();
        ctx.strokeStyle = '#fff'; ctx.lineWidth = 1; ctx.stroke();
      }
    });

    // Scrubber vertical line
    var xCur = xOf(state.minute);
    ctx.strokeStyle = 'rgba(56, 189, 248, 0.5)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(xCur, PAD_T); ctx.lineTo(xCur, PAD_T + plotH); ctx.stroke();
  }

  // ---- Render all charts ----
  function renderCharts() {
    // Lazy create chart DOM once
    if (!charts.firstChild) {
      DATA.tickers.forEach(function (tk) {
        var div = document.createElement('div');
        div.className = 'chart';
        div.setAttribute('data-tk', tk);
        div.innerHTML =
          '<div class="hd"><span class="tk">' + tk + '</span><span class="px" data-px></span></div>' +
          '<canvas></canvas>';
        charts.appendChild(div);
      });
    }
    DATA.tickers.forEach(function (tk) {
      var div = charts.querySelector('[data-tk="' + tk + '"]');
      var canvas = div.querySelector('canvas');
      var bars = DATA.bars[state.date][tk] || [];
      // Update header price (last bar at or before scrubber)
      var pxEl = div.querySelector('[data-px]');
      var lastBar = null;
      for (var i = bars.length - 1; i >= 0; i--) {
        if (bars[i].t <= state.minute) { lastBar = bars[i]; break; }
      }
      pxEl.textContent = lastBar ? '$' + lastBar.c.toFixed(2) : '--';
      drawChart(canvas, tk);
    });
  }

  // ---- Render everything ----
  function renderAll() {
    timeLabel.textContent = fmtMin(state.minute) + ' ET';
    scrub.value = state.minute;
    renderSummary();
    renderTrades();
    renderCharts();
  }

  // ---- Playback ----
  function startPlay() {
    if (state.playing) return;
    state.playing = true;
    playBtn.textContent = '⏸';
    state.timer = setInterval(function () {
      if (state.minute >= X_MAX) { stopPlay(); return; }
      state.minute += 1;
      renderAll();
    }, SPEEDS[state.speedIdx]);
  }
  function stopPlay() {
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
    state.playing = false;
    playBtn.textContent = '▶';
  }
  playBtn.onclick = function () { state.playing ? stopPlay() : startPlay(); };
  speedBtn.onclick = function () {
    state.speedIdx = (state.speedIdx + 1) % SPEEDS.length;
    speedBtn.textContent = (state.speedIdx + 1) + '×';
    if (state.playing) { stopPlay(); startPlay(); }
  };

  // ---- Wire scrubber ----
  scrub.oninput = function () {
    stopPlay();
    state.minute = parseInt(this.value, 10);
    renderAll();
  };
  $('jumpOpen').onclick  = function () { stopPlay(); state.minute = 570; renderAll(); };
  $('jumpEntry').onclick = function () { stopPlay(); state.minute = 630; renderAll(); };
  $('jumpEod').onclick   = function () { stopPlay(); state.minute = 900; renderAll(); };
  $('jumpClose').onclick = function () { stopPlay(); state.minute = 960; renderAll(); };
  document.addEventListener('keydown', function (e) {
    if (e.target && (e.target.tagName === 'SELECT' || e.target.tagName === 'INPUT')) return;
    if (e.key === ' ')          { e.preventDefault(); playBtn.click(); }
    if (e.key === 'ArrowLeft')  { stopPlay(); state.minute = Math.max(X_MIN, state.minute - 1); renderAll(); }
    if (e.key === 'ArrowRight') { stopPlay(); state.minute = Math.min(X_MAX, state.minute + 1); renderAll(); }
  });

  // ---- Wire date dropdown ----
  dateSel.onchange = function () {
    stopPlay();
    state.date = this.value;
    state.minute = X_MAX;  // reset to end of day
    renderAll();
  };

  // ---- Wire portfolio tabs ----
  document.querySelectorAll('.tab').forEach(function (tab) {
    tab.onclick = function () {
      stopPlay();
      document.querySelectorAll('.tab').forEach(function (t) { t.classList.remove('active'); });
      tab.classList.add('active');
      state.portfolio = tab.getAttribute('data-portfolio');
      renderAll();
    };
  });

  // ---- Re-render on resize (canvases need to rescale) ----
  var resizeTimer = null;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(renderCharts, 100);
  });

  // ---- Initial render ----
  renderAll();
})();
</script>

</body>
</html>
"""


def render_html(data: dict) -> str:
    inline_json = json.dumps(data, separators=(",", ":"))
    return _HTML_TEMPLATE.replace("__DATA__", inline_json)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="replay_week.html")
    ap.add_argument(
        "--dates",
        default="2026-05-11,2026-05-12,2026-05-13,2026-05-14,2026-05-15",
        help="Comma-separated dates (YYYY-MM-DD). Default: last week.",
    )
    args = ap.parse_args()
    dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    data = build_data(dates)
    html = render_html(data)
    out = Path(args.out)
    out.write_text(html, encoding="utf-8")
    print(f"[OK] wrote {out} ({len(html):,} bytes)")
    print(f"     dates: {', '.join(dates)}")
    print(f"     bars per date: {sum(len(b) for b in next(iter(data['bars'].values())).values())}")
    total_trades = sum(
        len(data["trades"][d][p]) for d in dates for p in data["portfolios"]
    )
    print(f"     trades total: {total_trades}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
