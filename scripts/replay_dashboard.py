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

  var _ttVersion = 0;  /* increments each call to force renderAll to re-run */

  /* Override Date.now() so HELD times, clocks, and other time-relative
     UI elements use the SCENARIO time, not the real wall clock. */
  (function() {
    var _origNow = Date.now.bind(Date);
    Date.now = function() {
      var d = (window.__TT_DIFFS || [])[window.__TT_IDX || 0];
      if (d && d.server_time) {
        try { var t = new Date(d.server_time).getTime(); if (t > 0) return t; } catch(e) {}
      }
      return _origNow();
    };
  })();

  function currentState() {
    _ttVersion++;
    var base  = window.__TT_BASE  || {};
    var diffs = window.__TT_DIFFS || [];
    var diff  = diffs[window.__TT_IDX || 0] || {};
    var s = Object.assign({}, base);
    if ('trades_today'      in diff) s.trades_today      = diff.trades_today;
    if ('positions'         in diff) s.positions         = diff.positions;
    if ('server_time'       in diff) s.server_time       = diff.server_time;
    if ('server_time_label' in diff) s.server_time_label = diff.server_time_label;
    if ('eod'               in diff) s.eod               = diff.eod;
    /* Time-varying fields: P&L, session mode, activity feed, scan state */
    if ('portfolio'         in diff) s.portfolio         = diff.portfolio;
    if ('regime'            in diff) s.regime            = diff.regime;
    if ('v10_activity'      in diff && s.v10) {
      s.v10 = Object.assign({}, s.v10);
      s.v10.activity = diff.v10_activity;
    }
    /* Override ALL gates flags so no base-state leakage into replay.
       scan_paused_user / trading_halted / scan_idle_hours from the midday
       production fetch can bleed into early snapshots and show the kill
       banner when no kill has actually occurred in the scenario. */
    if (s.gates) {
      var _scanPaused = (diff.gates_scan_paused != null) ? !!diff.gates_scan_paused : false;
      s.gates = Object.assign({}, s.gates, {
        scan_paused:       _scanPaused,
        scan_paused_user:  _scanPaused,   /* mirrors scan state in scenario */
        scan_idle_hours:   false,          /* always a live trading day */
        trading_halted:    false,          /* legacy flag, not used in v10 */
      });
    }
    /* Override v10.day_states so TRADES TODAY / top-ticker counts are correct
       for each scenario time point (the gauge reads day_states, not risk_books). */
    if ('v10_day_states' in diff && s.v10) {
      s.v10 = Object.assign({}, s.v10);
      s.v10.day_states = diff.v10_day_states;
    }
    /* Override risk book fields so the kill banner, P&L gauges, and
       TRADES TODAY admit/reject counts reflect the correct scenario state. */
    if (('v10_kill_triggered' in diff || 'v10_realized_pnl' in diff ||
         'v10_admit_count' in diff || 'v10_reject_count' in diff) && s.v10 && s.v10.risk_books) {
      s.v10 = Object.assign({}, s.v10);
      s.v10.risk_books = Object.assign({}, s.v10.risk_books);
      var _kill        = diff.v10_kill_triggered != null ? !!diff.v10_kill_triggered : null;
      var _realPnl     = diff.v10_realized_pnl   != null ? diff.v10_realized_pnl    : null;
      var _admitCount  = diff.v10_admit_count     != null ? diff.v10_admit_count     : null;
      var _rejectCount = diff.v10_reject_count    != null ? diff.v10_reject_count    : null;
      /* Val has its own separate account ($30,185.24). Its realized P&L is scaled
         proportionally (~30%) and its kill threshold is 2% of $30k = ~$604, so
         the morning scenario never triggers Val's kill (worst Val P&L = -$14). */
      var _VAL_BASE_EQ = 30185.24;
      var _VAL_RATIO   = _VAL_BASE_EQ / 100000;
      var _valRealPnl  = _realPnl !== null ? Math.round(_realPnl * _VAL_RATIO * 100) / 100 : null;
      var _valKillThresh = Math.round(_VAL_BASE_EQ * 0.02 * 10) / 10; /* ~$603.70 */
      Object.keys(s.v10.risk_books).forEach(function(pid) {
        s.v10.risk_books[pid] = Object.assign({}, s.v10.risk_books[pid]);
        if (pid === 'main') {
          if (_kill        !== null) s.v10.risk_books[pid].daily_kill_triggered = _kill;
          if (_realPnl     !== null) s.v10.risk_books[pid].realized_pnl_today   = _realPnl;
          if (_admitCount  !== null) s.v10.risk_books[pid].admit_count           = _admitCount;
          if (_rejectCount !== null) s.v10.risk_books[pid].reject_count          = _rejectCount;
        } else if (pid === 'val') {
          /* Val: scale P&L to account size, never trigger kill in this scenario */
          if (_valRealPnl  !== null) s.v10.risk_books[pid].realized_pnl_today   = _valRealPnl;
          s.v10.risk_books[pid].daily_kill_triggered = false;
          s.v10.risk_books[pid].daily_kill_threshold = _valKillThresh;
          s.v10.risk_books[pid].max_risk_dollars     = _valKillThresh;
          s.v10.risk_books[pid].equity = Math.round((_VAL_BASE_EQ + (_valRealPnl||0)) * 100) / 100;
        }
        /* gene: leave unchanged (no active positions in this scenario) */
      });
    }
    /* Inject a unique last_scan_at so app.js's SSE optimization
       (_scanAt !== _lastRenderedScanAt) always triggers renderAll(). */
    if (s.gates) {
      s.gates = Object.assign({}, s.gates);
      s.gates.last_scan_at = 'tt-' + (window.__TT_IDX || 0) + '-' + _ttVersion;
    }
    return s;
  }

  /* Override __tgNowEtMinutes so the EOD time-bar uses scenario time instead of
     real wall clock. app.js defines this with new Date() which ignores our Date.now
     patch. We override it here, after currentState() is defined, so it can read
     the scenario server_time_label for the current scrubber position. */
  window.__tgNowEtMinutes = function() {
    var s = currentState();
    var tMatch = (s.server_time_label || '').match(/([0-9]{2}):([0-9]{2}):[0-9]{2}/);
    if (tMatch) return parseInt(tMatch[1],10)*60 + parseInt(tMatch[2],10);
    /* Fallback: parse server_time UTC and subtract 4h for ET */
    var stMatch = (s.server_time || '').match(/T([0-9]{2}):([0-9]{2}):/);
    if (stMatch) return ((parseInt(stMatch[1],10) - 4 + 24) % 24) * 60 + parseInt(stMatch[2],10);
    return 0;
  };

  /* Generate realistic fake 1m OHLC bars from OR levels.
     Uses a seeded PRNG so the same ticker always produces the same bars. */
  /* chartStartMin: first bar to INCLUDE in output. PRNG always advances from 570
     so price continuity is maintained even when zooming to 14:00 for EOD-only tickers. */
  function _fakeBars(ticker, orHigh, orLow, endEtMin, date, chartStartMin) {
    var barDate = date || '2026-05-15';
    var chartStart = (chartStartMin != null) ? chartStartMin : 570;
    var seed = 0;
    for (var i = 0; i < ticker.length; i++) seed = (seed * 31 + ticker.charCodeAt(i)) | 0;
    function rand() {
      seed = (Math.imul(seed, 1664525) + 1013904223) | 0;
      return ((seed >>> 0) / 4294967296);
    }
    var vol = (orHigh - orLow) / 40;
    var price = orLow + (orHigh - orLow) * 0.25;
    var bars = [];
    var end = endEtMin || 615;
    for (var m = 570; m <= end; m++) {
      var open = price;
      var drift = (m < 600) ? vol * 0.15 : vol * 0.02;
      var chg = (rand() - 0.47) * vol * 2 + drift;
      var close = open + chg;
      var hi = Math.max(open, close) + rand() * vol * 0.8;
      var lo = Math.min(open, close) - rand() * vol * 0.8;
      if (m >= chartStart) {
        var hh = Math.floor(m / 60), mm = m % 60;
        var ts = barDate + 'T' + (hh<10?'0':'') + hh + ':' + (mm<10?'0':'') + mm + ':00-04:00';
        bars.push({
          ts:ts, et_min:m,
          o:+open.toFixed(2), h:+hi.toFixed(2), l:+lo.toFixed(2), c:+close.toFixed(2),
          v: Math.floor(rand()*80000+15000),
          avwap:null, avwap_hi:null, avwap_lo:null, pm_avwap:null, ema9_5m:null
        });
      }
      price = close;
    }
    return bars;
  }

  function _intradayReply(u) {
    var parts = u.split('/api/intraday/');
    if (parts.length < 2) return {};
    var ticker = parts[1].split('?')[0].toUpperCase();
    var s = currentState();
    /* Find OR levels — try proximity list first, fall back to v10.or_windows dict */
    var prox = (s.proximity || []).filter(function(p){ return p.ticker === ticker; })[0] || {};
    var orHigh = prox.or_high || null;
    var orLow  = prox.or_low  || null;
    if (!orHigh || !orLow) {
      var _orWin = (s.v10 && s.v10.or_windows && s.v10.or_windows[ticker]) || {};
      orHigh = _orWin.or_high || null;
      orLow  = _orWin.or_low  || null;
    }
    /* Derive end ET minute from server_time_label (e.g. "Fri May 16 | 10:15:00 ET") */
    var endMin = 615; /* default 10:15 */
    var tMatch = (s.server_time_label || '').match(/([0-9]{2}):([0-9]{2}):[0-9]{2}/);
    if (tMatch) endMin = parseInt(tMatch[1],10)*60 + parseInt(tMatch[2],10);
    /* Extend bars 15 min past scenario time; always cover through 16:05 (EOD close) */
    endMin = Math.max(endMin + 15, Math.min(endMin + 15, 965));
    /* Derive date before the empty-bar guard so error return uses correct date */
    var sceneDate = '2026-05-15';
    var dtMatch = (s.server_time || '').match(/^(\d{4}-\d{2}-\d{2})/);
    if (dtMatch) sceneDate = dtMatch[1];
    if (!orHigh || !orLow) return {ok:true,ticker:ticker,date:sceneDate,bars:[],or_high:null,or_low:null,or_fresh:false,pdc:null,trades:[],sentinel_events:[],lifecycle:{},bar_count:0};

    /* Build lifecycle overlay data from trades.
       IMPORTANT: do NOT put entry_ts/exit_ts in payload.trades.
       app.js calls utcIsoToEtMin() (declared as `const` at line 2005) from the
       trades overlay loop at line 1962 — temporal dead zone ReferenceError silently
       kills _drawIntradayChart before entry/exit triangles ever render.
       Fix: use lifecycle.entries/exits/open (et_min numbers, no TDZ risk) and
       draw stop/1R/target lines ourselves via _stop_refs in _autoExpandCharts. */
    var _rawEntries = [], _rawExits = [];
    (s.trades_today || []).forEach(function(t) {
      if ((t.ticker||'').toUpperCase() !== ticker) return;
      var action = (t.action||'').toUpperCase();
      var tm2 = (t.time||'').replace(' ET','').replace('ET','').match(/([0-9]+):([0-9]+)/);
      if (!tm2) return;
      var etMin = parseInt(tm2[1],10)*60 + parseInt(tm2[2],10);
      if (action === 'BUY' || action === 'SHORT') {
        _rawEntries.push({etMin:etMin, price:t.price, side:(t.side||'LONG').toLowerCase(), shares:t.shares||0});
      } else if (action === 'SELL' || action === 'COVER') {
        _rawExits.push({etMin:etMin, price:t.price});
      }
    });

    /* EOD zoom: if ticker has ANY morning entries (before 14:00 = 840 ET min),
       show the full day so morning + EOD context is both visible (e.g. ORCL).
       Pure EOD-only tickers (AVGO, MSFT) zoom chart to 14:00-16:00 so the
       29-min trade window isn't squashed into the rightmost 7% of the axis. */
    var _hasMorningActivity = _rawEntries.some(function(e){ return e.etMin < 840; });
    var _chartStartMin = _hasMorningActivity ? 570 : 840;

    var bars = _fakeBars(ticker, orHigh, orLow, endMin, sceneDate, _chartStartMin);

    /* Find open position for this ticker — gives us actual stop/mark prices */
    var openPos = null;
    (s.positions || []).forEach(function(p) {
      if ((p.ticker||'').toUpperCase() === ticker && !openPos) openPos = p;
    });
    /* Pair entries with exits; build lifecycle + stop/target refs */
    var lcEntries=[], lcExits=[], lcOpen=[], stopRefs=[], usedExits=[];
    _rawEntries.forEach(function(en) {
      var ex = null;
      for (var xi = 0; xi < _rawExits.length; xi++) {
        if (usedExits.indexOf(xi) < 0 && _rawExits[xi].etMin > en.etMin) {
          ex = _rawExits[xi]; usedExits.push(xi); break;
        }
      }
      lcEntries.push({et_min:en.etMin, price:en.price, side:en.side, shares:en.shares});
      if (ex) {
        lcExits.push({et_min:ex.etMin, entry_et_min:en.etMin, price:ex.price, entry_price:en.price, side:en.side});
      } else {
        lcOpen.push({et_min:en.etMin, entry_price:en.price, side:en.side});
      }
      /* Chart overlay style: use entry time not openPos.eod so ORCL morning trades
         get ORB-style overlays even when an EOD ORCL position is also open.
         EOD entries are those placed at or after 15:00 ET (900 min). */
      var isEodEntry = en.etMin >= 900;
      var isLong = en.side !== 'short';
      var markPx = (openPos && openPos.mark != null && !ex) ? openPos.mark : null;
      var origStopPx = null, currStopPx = null, bePx = null, targPx = null;
      if (!isEodEntry) {
        /* Morning ORB: use actual position stop → 1R → 2.5R */
        origStopPx = (openPos && openPos.entry_stop != null) ? openPos.entry_stop
                     : (orHigh && orLow ? (isLong ? orLow : orHigh) : null);
        currStopPx = (openPos && openPos.stop != null && !ex) ? openPos.stop : origStopPx;
        var risk = origStopPx != null ? Math.abs(en.price - origStopPx) : 0;
        bePx   = risk > 0 ? en.price + (isLong ? 1 : -1) * risk       : null;
        targPx = risk > 0 ? en.price + (isLong ? 1 : -1) * 2.5*risk  : null;
      }
      stopRefs.push({
        entry_et_min:  en.etMin, exit_et_min: ex ? ex.etMin : null,
        entry_price:   en.price,
        stop_price:    currStopPx,  /* null for EOD entries */
        be_price:      bePx,        /* null for EOD entries */
        target_price:  targPx,      /* null for EOD entries */
        mark_price:    markPx,
        is_eod:        isEodEntry
      });
    });

    return {
      ok:true, ticker:ticker, date:sceneDate,
      bars:bars, or_high:orHigh, or_low:orLow, or_fresh:true,
      pdc: +(orLow*0.994).toFixed(2),
      sess_hod: +Math.max.apply(null,bars.map(function(b){return b.h;})).toFixed(2),
      sess_lod: +Math.min.apply(null,bars.map(function(b){return b.l;})).toFixed(2),
      trades: [],  /* empty: non-empty triggers TDZ bug in app.js ~line 1962 */
      lifecycle: {entries:lcEntries, exits:lcExits, open:lcOpen},
      _stop_refs: stopRefs,
      sentinel_events:[], bar_count:bars.length
    };
  }

  function reply(u) {
    var s = currentState();
    if (u.indexOf('/api/state')         >= 0) return s;
    /* Val mirrors Main ORB positions with executor-compatible field names.
       The executor renderer uses symbol/entry_price/current_price/unrealized_pnl
       (not ticker/entry/mark/unrealized as the main state uses). */
    if (u.indexOf('/api/executor/val')  >= 0) {
      /* Val paper account: equity $30,185.24, scale positions proportionally vs Main $100k */
      var _VAL_EQUITY  = 30185.24;
      var _VAL_RATIO   = _VAL_EQUITY / 100000;
      var _mainEq = ((s.portfolio||{}).equity) || 100000;
      var _valDayPnl = Math.round(((_mainEq - 100000) * _VAL_RATIO) * 100) / 100;
      var _valEq   = Math.round((_VAL_EQUITY + _valDayPnl) * 100) / 100;
      /* Include ALL positions (morning + EOD) scaled to Val's account size.
         Executor renderer uses avg_entry (not entry_price) for entry/notional/progress bar.
         EOD positions also need to be in the eod_positions dict for the EOD time-bar. */
      var _valPos = (s.positions||[]).map(function(p) {
        var _sh  = Math.max(1, Math.round((p.shares||1) * _VAL_RATIO));
        var _unr = Math.round((p.unrealized != null ? p.unrealized : (p.unrealized_pnl||0)) * _VAL_RATIO * 100) / 100;
        var _pct = (p.entry && _sh) ? _unr / (p.entry * _sh) * 100 : 0;
        var _pctRounded = Math.round(_pct * 100) / 100;
        return {
          symbol: p.ticker, ticker: p.ticker,
          side: (p.side||'LONG').toUpperCase(),   /* executor renderer expects UPPER */
          entry_price: p.entry,
          avg_entry:   p.entry,                   /* executor renderer uses avg_entry for Entry/Notional/progress */
          current_price: p.mark, limit_price: p.entry,
          unrealized_pnl: _unr,
          unrealized_pct: _pctRounded,
          unrealized_pnl_pct: _pctRounded,        /* executor renderer reads unrealized_pnl_pct */
          shares: _sh, qty: _sh,
          stop_price: p.stop, held_seconds: p.held_seconds || 0,
          entry_ts_utc: p.entry_ts_utc,
          phase: p.phase || 'A', entry_num: p.entry_num || 1,
          portfolio: p.eod ? 'val-eod' : 'val',
          cost: Math.round(p.entry * _sh * 100) / 100,
          eod: !!p.eod
        };
      });
      /* eod_positions dict: executor renderer uses this to classify positions as EOD
         (renders time-based progress bar instead of ORB stop-based bar). */
      var _eodPosDict = {};
      _valPos.forEach(function(vp) {
        if (vp.eod) _eodPosDict[vp.symbol] = {eod: true, entry_price: vp.avg_entry};
      });
      /* All trades (morning + EOD) scaled; executor renderer reads todays_trades */
      var _valTrades = (s.trades_today||[]).map(function(t){
        return Object.assign({}, t, {portfolio: t.eod ? 'val-eod' : 'val',
          shares: Math.max(1, Math.round((t.shares||1)*_VAL_RATIO))});
      });
      return {ok:true, positions:_valPos, eod_positions:_eodPosDict,
              todays_trades:_valTrades, trades_today:_valTrades,
              account:{equity:_valEq, cash:_valEq, portfolio_value:_valEq,
                       day_pnl:_valDayPnl, status:'ACTIVE'}};
    }
    if (u.indexOf('/api/executor/gene') >= 0) return {ok:true,positions:[],trades_today:[],trades:[]};
    if (u.indexOf('/api/trade_log')     >= 0) return {ok:true,count:(s.trades_today||[]).length,rows:[]};
    if (u.indexOf('/api/indices')       >= 0) return (window.__TT_BASE||{})._indices || {};
    if (u.indexOf('/api/version')       >= 0) return {version:s.version||'?'};
    if (u.indexOf('/api/intraday/')     >= 0) return _intradayReply(u);
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

  /* Fake EventSource -- stays OPEN (readyState=1) to suppress the
     "Disconnected" banner, AND fires a real "state" SSE event so that
     app.js's streamConn.addEventListener("state", ...) triggers renderAll().
     Without this, renderAll() is never called and the page stays empty. */
  var _fakeESInstances = [];

  function FakeES() {
    this.readyState = 1;
    this.onopen = this.onmessage = this.onerror = null;
    this._handlers = {};
    _fakeESInstances.push(this);
    /* Fire initial state event after app.js has wired its listeners */
    var self = this;
    setTimeout(function() { _fireSSE(self); }, 120);
  }
  FakeES.prototype.close = function() { this.readyState = 2; };
  FakeES.prototype.addEventListener = function(type, fn) {
    if (!this._handlers[type]) this._handlers[type] = [];
    this._handlers[type].push(fn);
  };
  FakeES.prototype.removeEventListener = function(type, fn) {
    if (!this._handlers[type]) return;
    this._handlers[type] = this._handlers[type].filter(function(h){ return h !== fn; });
  };
  FakeES.CONNECTING = 0; FakeES.OPEN = 1; FakeES.CLOSED = 2;
  window.EventSource = FakeES;

  /* Fire the SSE "state" event on all live instances with current snapshot */
  function _fireSSE(instance) {
    var es = instance || (_fakeESInstances[_fakeESInstances.length - 1]);
    if (!es || es.readyState === 2) return;
    var s = currentState();
    /* app.js parses ev.data as JSON({data: <state>}) (line ~3714) */
    var payload = JSON.stringify({data: s});
    var evt = {data: payload, type: 'state'};
    (es._handlers['state'] || []).forEach(function(h){ try { h(evt); } catch(e){} });
  }
  /* Exported so _NAV_SCRIPT can re-fire on each navigation */
  window.__ttFireSSE = _fireSSE;
})();
</script>
"""

# Scrubber + playback script -- injected after app.js.
_NAV_SCRIPT = """\
<script id="__tt_nav">
(function(){
  var DIFFS  = window.__TT_DIFFS || [];
  var idx    = window.__TT_IDX   || 0;
  var timer  = null;
  /* ms per snapshot: index maps to 1x/2x/3x */
  var SPEEDS = [1000, 400, 150];
  var speedI = 2; /* default 3x */

  function refresh() {
    if (typeof window.__ttFireSSE === 'function') window.__ttFireSSE();
  }

  /* Auto-expand inline charts for every open position row.
     Inserts the chart row AFTER the progress-bar row so the bar
     appears above the chart (user expectation). */
  function _autoExpandCharts() {
    if (typeof window.__tgRenderTickerChart !== 'function') return;
    var rows = document.querySelectorAll(
      '#pos-body tr[data-pos-ticker]:not(.pos-progress-row):not(.pos-chart-row)');
    rows.forEach(function(row) {
      var ticker = row.getAttribute('data-pos-ticker');
      if (!ticker) return;
      /* Walk past the progress-bar row to find the right insertion point */
      var insertAfter = row;
      var sib = row.nextElementSibling;
      if (sib && sib.classList.contains('pos-progress-row')) {
        insertAfter = sib;
        sib = sib.nextElementSibling;
      }
      /* Skip if chart row already exists at the insertion point */
      if (sib && sib.classList.contains('pos-chart-row')) return;
      /* Insert chart row after progress bar */
      var chartRow = document.createElement('tr');
      chartRow.className = 'pos-chart-row';
      chartRow.setAttribute('data-pos-chart', ticker);
      var td = document.createElement('td');
      td.setAttribute('colspan', '11');
      td.className = 'pos-chart-cell';
      var mount = document.createElement('div');
      mount.className = 'pos-chart-mount';
      mount.setAttribute('data-chart-mount', ticker);
      td.appendChild(mount);
      chartRow.appendChild(td);
      if (insertAfter.parentNode) insertAfter.parentNode.insertBefore(chartRow, insertAfter.nextSibling);
      window.__tgRenderTickerChart(ticker, mount);
      /* After chart renders: draw entry/stop/mark/1R/target lines.
         The app.js code path for these is dead (TDZ bug at line 1962) — we draw here.
         Uses distinct colors per level so each line is immediately identifiable. */
      (function(m) {
        setTimeout(function() {
          var canvas = m.querySelector('[data-intraday-canvas]');
          if (!canvas || !canvas._lastPayload) return;
          var pl = canvas._lastPayload;
          if (!Array.isArray(pl._stop_refs) || !pl._stop_refs.length) return;
          var ctx = canvas.getContext('2d');
          var cssW = canvas.clientWidth  || 390;
          var cssH = canvas.clientHeight || 280;
          var X_MIN=570, X_MAX=960, PAD_L=56, PAD_R=12, PAD_T=14, PAD_B=22;
          var plotW = cssW - PAD_L - PAD_R;
          var plotH = cssH - PAD_T - PAD_B;
          var priceH = Math.max(40, plotH * 0.85 - 4);
          var bars = pl.bars || [];
          /* Expand Y-range to include ALL reference prices (stop, entry, 1R, target, mark) */
          var yMin=Infinity, yMax=-Infinity;
          bars.forEach(function(b) {
            if (typeof b.et_min!=='number'||b.et_min<X_MIN||b.et_min>X_MAX) return;
            if (typeof b.l==='number') yMin=Math.min(yMin,b.l);
            if (typeof b.h==='number') yMax=Math.max(yMax,b.h);
          });
          if (pl.or_high){yMin=Math.min(yMin,pl.or_high);yMax=Math.max(yMax,pl.or_high);}
          if (pl.or_low) {yMin=Math.min(yMin,pl.or_low); yMax=Math.max(yMax,pl.or_low);}
          pl._stop_refs.forEach(function(r){
            [r.stop_price,r.entry_price,r.be_price,r.target_price,r.mark_price].forEach(function(p){
              if(typeof p==='number'){yMin=Math.min(yMin,p);yMax=Math.max(yMax,p);}
            });
          });
          if (!isFinite(yMin)||!isFinite(yMax)) return;
          var yPad=(yMax-yMin)*0.1||0.5; yMin-=yPad; yMax+=yPad;
          var xOf=function(m){return PAD_L+(m-X_MIN)/(X_MAX-X_MIN)*plotW;};
          var yOf=function(p){return PAD_T+(1-(p-yMin)/(yMax-yMin))*priceH;};

          pl._stop_refs.forEach(function(ref) {
            var x1=xOf(Math.max(X_MIN,ref.entry_et_min));
            var x2=(ref.exit_et_min!=null)?xOf(Math.min(X_MAX,ref.exit_et_min)):PAD_L+plotW;
            function hline(price,color,dash,lbl,lw){
              if(price==null||typeof price!=='number') return;
              ctx.save();
              ctx.strokeStyle=color; ctx.lineWidth=lw||1.3;
              ctx.setLineDash(dash||[]);
              ctx.beginPath(); ctx.moveTo(x1,yOf(price)); ctx.lineTo(x2,yOf(price)); ctx.stroke();
              ctx.setLineDash([]);
              /* Label at right edge */
              ctx.fillStyle=color; ctx.font='bold 9px system-ui,sans-serif'; ctx.textAlign='right';
              ctx.fillText(lbl+'  $'+price.toFixed(2), PAD_L+plotW-2, yOf(price)-3);
              ctx.restore();
            }
            if (ref.is_eod) {
              /* EOD reversal: entry + "close 15:59" tick. No stop/1R/target. */
              hline(ref.entry_price, '#94a3b8',[2,3],'entry');
              var xClose = xOf(Math.min(959, X_MAX));
              ctx.save();
              ctx.strokeStyle='#a78bfa'; ctx.lineWidth=1.5; ctx.setLineDash([4,3]);
              ctx.beginPath(); ctx.moveTo(xClose,PAD_T); ctx.lineTo(xClose,PAD_T+priceH); ctx.stroke();
              ctx.setLineDash([]);
              ctx.fillStyle='#a78bfa'; ctx.font='8px system-ui'; ctx.textAlign='center';
              ctx.fillText('exit 15:59', xClose, PAD_T+priceH-3);
              ctx.restore();
            } else {
              /* Morning ORB: stop / entry / 1R / +2.5R target */
              hline(ref.stop_price,   '#ef4444',[5,3],'stop');
              hline(ref.entry_price,  '#94a3b8',[2,3],'entry');
              hline(ref.be_price,     '#fbbf24',[4,3],'1R');
              hline(ref.target_price, '#22c55e',[5,3],'+2.5R');
            }
            /* mark — sky-blue solid with circle + price badge */
            if (ref.mark_price!=null) {
              var mp=ref.mark_price, my=yOf(mp), mx=x2;
              ctx.save();
              ctx.strokeStyle='#38bdf8'; ctx.lineWidth=1.8; ctx.setLineDash([2,2]);
              ctx.beginPath(); ctx.moveTo(x1,my); ctx.lineTo(mx,my); ctx.stroke();
              ctx.setLineDash([]);
              /* dot at current price on line */
              ctx.fillStyle='#38bdf8';
              ctx.beginPath(); ctx.arc(mx-6,my,5,0,Math.PI*2); ctx.fill();
              /* pill badge */
              var lbl2='$'+mp.toFixed(2);
              ctx.font='bold 10px system-ui,sans-serif';
              var tw=ctx.measureText(lbl2).width;
              ctx.fillStyle='rgba(14,19,24,0.85)';
              ctx.fillRect(x1+2, my-11, tw+8, 14);
              ctx.fillStyle='#38bdf8';
              ctx.textAlign='left'; ctx.fillText(lbl2,x1+6,my-1);
              ctx.restore();
            }
          });
        }, 280);
      })(mount);
    });
  }

  function navigate(n, skipRefresh) {
    idx = Math.max(0, Math.min(DIFFS.length - 1, n));
    window.__TT_IDX = idx;
    /* sync range input */
    var rng = document.getElementById('__tt_range');
    if (rng) rng.value = idx;
    /* update timestamp label — show "May 15 · HH:MM ET" */
    var d   = DIFFS[idx] || {};
    var raw = d.ts_et || '';
    var tm  = raw.match(/T([0-9]{2}:[0-9]{2})/);
    var timePart = tm ? tm[1] + ' ET' : raw;
    /* Extract date for label */
    var dateLabel = '';
    var dm = raw.match(/^([0-9]{4})-([0-9]{2})-([0-9]{2})/);
    if (dm) {
      var MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
      dateLabel = MONTHS[parseInt(dm[2],10)-1] + ' ' + parseInt(dm[3],10) + ' · ';
    }
    var tsEl = document.getElementById('__tt_ts');
    if (tsEl) {
      tsEl.innerHTML = '<span style="color:#4b5563;font-size:11px;font-weight:400">' + dateLabel + '</span>' + timePart;
    }
    /* update counter */
    var cntEl = document.getElementById('__tt_cnt');
    if (cntEl) cntEl.textContent = (idx+1)+' / '+DIFFS.length;
    /* move timeline cursor to the snapshot's actual ET time */
    var cursor2 = document.getElementById('__tt_cursor');
    if (cursor2) {
      var _d2 = DIFFS[idx] || {};
      var _raw2 = _d2.ts_et || '';
      var _tm2  = _raw2.match(/T([0-9]{2}):([0-9]{2})/);
      var _pct2 = idx / Math.max(1, DIFFS.length - 1) * 100; /* fallback */
      if (_tm2) {
        var _etMin2 = parseInt(_tm2[1],10)*60 + parseInt(_tm2[2],10);
        _pct2 = Math.max(0, Math.min(100, (_etMin2 - 570) / 390 * 100));
      }
      cursor2.style.left = _pct2.toFixed(1) + '%';
    }
    if (!skipRefresh) refresh();
    /* Simplify verbose scan-paused banner to a short one-liner */
    setTimeout(function() {
      var b = document.getElementById('banner');
      if (!b || b.classList.contains('hide')) return;
      /* Replace the inner HTML with a compact version */
      var inner = b.innerHTML || '';
      if (inner.indexOf('SCAN PAUSED') >= 0 || inner.indexOf('KILL') >= 0 ||
          inner.indexOf('scan_paused') >= 0) {
        var kill = inner.indexOf('kill') >= 0 || inner.indexOf('KILL') >= 0;
        b.innerHTML = '<div style="padding:6px 16px;font-size:11px;color:#64748b;display:flex;align-items:center;gap:8px">'
          + '<span style="font-size:13px">&#9646;</span>'          /* ▐ pause glyph */
          + (kill ? '<span>Scanner paused &mdash; daily-loss limit reached &middot; existing positions still managed</span>'
                  : '<span>Scanner paused &mdash; outside trading window</span>')
          + '</div>';
      }
    }, 750);
    /* Auto-show charts for all open positions after render settles */
    setTimeout(_autoExpandCharts, 600);
    /* Inject mini sparklines into OR Proximity matrix rows */
    setTimeout(_proximitySparklines, 900);
    /* Inject day P&L sparkline below the DAY P&L KPI */
    setTimeout(_pnlSparkline, 700);
  }

  function startPlay() {
    if (timer) return;
    var playBtn = document.getElementById('__tt_play');
    if (playBtn) playBtn.textContent = '⏸'; /* pause icon */
    timer = setInterval(function(){
      if (idx >= DIFFS.length - 1) { stopPlay(); return; }
      navigate(idx + 1);
    }, SPEEDS[speedI]);
  }

  function stopPlay() {
    clearInterval(timer);
    timer = null;
    var playBtn = document.getElementById('__tt_play');
    if (playBtn) playBtn.textContent = '▶'; /* play icon */
  }

  function togglePlay() {
    if (timer) stopPlay(); else startPlay();
  }

  function cycleSpeed() {
    speedI = (speedI + 1) % SPEEDS.length;
    var labels = ['1\xD7','2\xD7','3\xD7'];
    var spdEl = document.getElementById('__tt_spd');
    if (spdEl) spdEl.textContent = labels[speedI];
  }

  /* P&L sparkline: draws history up to current snapshot, dims future trajectory.
     Re-draws on every navigation so the "filled" portion tracks the scrubber. */
  function _pnlSparkline() {
    var kpnl = document.getElementById('k-pnl');
    if (!kpnl) return;
    var card = kpnl.closest('.card, [class*="kpi"], [class*="pnl"]') || kpnl.parentElement;
    if (!card) return;
    var diffs = window.__TT_DIFFS || [];
    /* Build full day P&L series (all snapshots) for scale */
    var allPoints = diffs.map(function(d) {
      return (d.portfolio && d.portfolio.day_pnl != null) ? d.portfolio.day_pnl : null;
    });
    var validPoints = allPoints.filter(function(v){ return v !== null; });
    if (validPoints.length < 2) return;

    /* Create or reuse canvas */
    var cv = card.__pnlSparkCv;
    if (!cv) {
      cv = document.createElement('canvas');
      cv.style.cssText = 'display:block;width:100%;height:28px;margin-top:4px;opacity:0.9;';
      kpnl.parentElement.insertBefore(cv, kpnl.nextSibling);
      card.__pnlSparkCv = cv;
    }
    cv.width = Math.min(card.clientWidth || 140, 140);
    cv.height = 28;
    var ctx2 = cv.getContext('2d');
    var w = cv.width, h = cv.height, pad = 2;
    var curIdx = Math.min(window.__TT_IDX || 0, diffs.length - 1);

    /* Y scale uses the FULL day range so scale doesn't jump */
    var lo = Math.min.apply(null, validPoints);
    var hi = Math.max.apply(null, validPoints);
    var rng = hi - lo || 1;
    var xOf3 = function(i){ return pad + i/(diffs.length-1)*(w-2*pad); };
    var yOf3 = function(v){ return pad + (1-(v-lo)/rng)*(h-2*pad); };

    ctx2.clearRect(0, 0, w, h);

    /* Zero baseline */
    if (lo < 0 && hi > 0) {
      var y0 = yOf3(0);
      ctx2.strokeStyle = 'rgba(255,255,255,0.08)'; ctx2.lineWidth = 0.5;
      ctx2.setLineDash([2,3]);
      ctx2.beginPath(); ctx2.moveTo(pad,y0); ctx2.lineTo(w-pad,y0); ctx2.stroke();
      ctx2.setLineDash([]);
    }

    /* Full-day ghost line (dim) — gives context for the full trajectory */
    ctx2.strokeStyle = 'rgba(255,255,255,0.08)'; ctx2.lineWidth = 1;
    ctx2.beginPath();
    var _started = false;
    for (var k=0;k<diffs.length;k++) {
      var v = allPoints[k]; if (v == null) continue;
      if (!_started){ ctx2.moveTo(xOf3(k),yOf3(v)); _started=true; }
      else ctx2.lineTo(xOf3(k),yOf3(v));
    }
    if (_started) ctx2.stroke();

    /* History line up to curIdx (solid, colored) */
    var pnlAtCur = allPoints[curIdx];
    var lineColor = (pnlAtCur != null && pnlAtCur >= 0) ? '#3ec28f' : '#ef4444';
    ctx2.strokeStyle = lineColor; ctx2.lineWidth = 1.5; ctx2.lineJoin = 'round';
    ctx2.beginPath();
    var _hStarted = false;
    for (var m=0;m<=curIdx;m++) {
      var hv = allPoints[m]; if (hv == null) continue;
      if (!_hStarted){ ctx2.moveTo(xOf3(m),yOf3(hv)); _hStarted=true; }
      else ctx2.lineTo(xOf3(m),yOf3(hv));
    }
    if (_hStarted) ctx2.stroke();

    /* Current position dot */
    if (pnlAtCur != null) {
      ctx2.fillStyle = lineColor;
      ctx2.beginPath(); ctx2.arc(xOf3(curIdx),yOf3(pnlAtCur),3,0,Math.PI*2); ctx2.fill();
    }
  }

  /* Inject 60×24px sparklines into proximity matrix ticker rows */
  function _proximitySparklines() {
    var rows = document.querySelectorAll('tr[data-prox-ticker], [data-prox-ticker]');
    if (!rows.length) {
      /* Try common class names */
      rows = document.querySelectorAll('.prox-row, .proximity-row');
    }
    rows.forEach(function(row) {
      var ticker = row.getAttribute('data-prox-ticker') || row.getAttribute('data-ticker') || '';
      if (!ticker) {
        /* Try first cell text */
        var fc = row.querySelector('td');
        if (fc) ticker = fc.textContent.trim().split(' ')[0].toUpperCase();
      }
      if (!ticker || row.__sparkDone) return;
      /* Find a cell to inject into — prefer the last td */
      var cells = row.querySelectorAll('td');
      if (!cells.length) return;
      var cell = cells[cells.length - 1];
      /* Create mini canvas */
      var cv = document.createElement('canvas');
      cv.width = 64; cv.height = 22;
      cv.style.cssText = 'display:block;margin:0 auto;opacity:0.85;';
      cell.appendChild(cv);
      row.__sparkDone = true;
      /* Fetch intraday bars and draw mini close line */
      fetch('/api/intraday/' + encodeURIComponent(ticker))
        .then(function(r){ return r.ok ? r.json() : null; })
        .then(function(pl) {
          if (!pl || !pl.ok || !pl.bars || !pl.bars.length) return;
          var ctx2 = cv.getContext('2d');
          var bars = pl.bars.filter(function(b){ return b.et_min>=570 && b.et_min<=960 && typeof b.c==='number'; });
          if (bars.length < 2) return;
          var closes = bars.map(function(b){ return b.c; });
          var lo=Math.min.apply(null,closes), hi=Math.max.apply(null,closes);
          var rng = hi - lo || 1;
          var w=cv.width, h=cv.height, pad=2;
          var xOf2=function(i){ return pad + i/(closes.length-1)*(w-2*pad); };
          var yOf2=function(v){ return pad + (1-(v-lo)/rng)*(h-2*pad); };
          /* Sparkline direction color */
          var up = closes[closes.length-1] >= closes[0];
          ctx2.strokeStyle = up ? '#3ec28f' : '#ef4444';
          ctx2.lineWidth = 1.5; ctx2.lineJoin = 'round';
          ctx2.beginPath();
          ctx2.moveTo(xOf2(0), yOf2(closes[0]));
          for (var i=1;i<closes.length;i++) ctx2.lineTo(xOf2(i), yOf2(closes[i]));
          ctx2.stroke();
          /* Dot at current price */
          var last = closes.length-1;
          ctx2.fillStyle = up ? '#3ec28f' : '#ef4444';
          ctx2.beginPath(); ctx2.arc(xOf2(last), yOf2(closes[last]), 2.5, 0, Math.PI*2); ctx2.fill();
        })
        .catch(function(){});
    });
  }

  /* Wire controls after DOM ready */
  function wireControls() {
    var playBtn = document.getElementById('__tt_play');
    if (playBtn) playBtn.onclick = togglePlay;
    var spdBtn = document.getElementById('__tt_spd');
    if (spdBtn) spdBtn.onclick = cycleSpeed;
    var rng = document.getElementById('__tt_range');
    if (rng) {
      rng.oninput = function(){ stopPlay(); navigate(parseInt(this.value,10), true); refresh(); };
      rng.onchange = function(){ navigate(parseInt(this.value,10)); };
    }
  }

  document.addEventListener('keydown', function(e){
    if (e.key === 'ArrowLeft')  { stopPlay(); navigate(idx-1); }
    if (e.key === 'ArrowRight') { stopPlay(); navigate(idx+1); }
    if (e.key === ' ')          { e.preventDefault(); togglePlay(); }
  });

  /* Export for external callers (playwright, console) */
  window.ttNav  = navigate;
  window.ttStop = stopPlay;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function(){ wireControls(); navigate(idx); });
  } else {
    wireControls();
    navigate(idx);
  }
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
    """Build the replay bar: controls + rich session timeline with zones + events."""
    import re as _re

    START_MIN, END_MIN = 570, 960  # 9:30 ET .. 16:00 ET
    SPAN = END_MIN - START_MIN

    def _et_min(ts_et: str) -> int | None:
        m = _re.search(r"T(\d{2}):(\d{2})(?::|$)", ts_et or "")
        return int(m.group(1)) * 60 + int(m.group(2)) if m else None

    def _pct(ts_et: str) -> float:
        m = _et_min(ts_et)
        return 0.0 if m is None else max(0.0, min(100.0, (m - START_MIN) / SPAN * 100))

    # ── Session zone bands ──────────────────────────────────────────────
    # (start_min, end_min, bg, label, text_color)
    ZONES = [
        (570, 600, "rgba(120,53,15,0.7)",   "OR",     "#fbbf24"),   # 9:30-10:00
        (600, 660, "rgba(6,78,59,0.7)",     "ACTIVE", "#34d399"),   # 10:00-11:00
        (660, 900, "rgba(15,23,42,0.5)",    "QUIET",  "#4b5563"),   # 11:00-15:00
        (900, 960, "rgba(76,29,149,0.7)",   "EOD",    "#a78bfa"),   # 15:00-16:00
    ]
    zones_html = ""
    for zs, ze, zbg, zlbl, ztxt in ZONES:
        zl = (zs - START_MIN) / SPAN * 100
        zw = (ze - zs) / SPAN * 100
        zones_html += (
            f'<div style="position:absolute;top:0;bottom:0;left:{zl:.1f}%;width:{zw:.1f}%;'
            f'background:{zbg};pointer-events:none;display:flex;align-items:flex-end;'
            f'justify-content:flex-start;padding-bottom:2px;padding-left:4px">'
            f'<span style="color:{ztxt};font-size:7px;font-weight:700;letter-spacing:.3px;'
            f'opacity:0.85;text-shadow:0 1px 3px rgba(0,0,0,.9)">{zlbl}</span></div>'
        )

    # Zone boundary dividers
    for boundary_min in [600, 660, 900]:
        p = (boundary_min - START_MIN) / SPAN * 100
        zones_html += (
            f'<div style="position:absolute;top:0;bottom:0;left:{p:.1f}%;width:1px;'
            f'background:rgba(255,255,255,0.12);pointer-events:none"></div>'
        )

    # ── Event markers ──────────────────────────────────────────────────
    # Detect kill transitions: snapshot where v10_kill_triggered flips True
    events_html = ""
    prev_kill = False
    for i, d in enumerate(diffs):
        kind  = d.get("kind", "")
        label = d.get("label", "")
        ts    = d.get("ts_et", "")
        p     = _pct(ts)
        kill  = bool(d.get("v10_kill_triggered"))

        # Kill event marker (fires when kill transitions True)
        if kill and not prev_kill:
            events_html += (
                f'<div title="Kill triggered" onclick="if(window.ttNav)window.ttNav({i});" '
                f'style="position:absolute;top:3px;left:{p:.1f}%;transform:translateX(-50%);'
                f'width:8px;height:8px;border-radius:50%;background:#dc2626;cursor:pointer;'
                f'z-index:12;border:1.5px solid #7f1d1d;box-shadow:0 0 8px rgba(220,38,38,.8)'
                f'"></div>'
            )
        prev_kill = kill

        # Entry/exit triangle markers
        if kind == "entry":
            col, icon = "#f59e0b", "▲"
        elif kind == "exit_win":
            col, icon = "#34d399", "▼"
        elif kind == "exit_loss":
            col, icon = "#f87171", "▼"
        else:
            continue

        tip = (label or ts).replace('"', "'")
        events_html += (
            f'<div title="{tip}" onclick="if(window.ttNav)window.ttNav({i});" '
            f'style="position:absolute;top:50%;left:{p:.1f}%;'
            f'transform:translate(-50%,-50%);font-size:9px;line-height:1;'
            f'color:{col};cursor:pointer;z-index:11;pointer-events:auto;'
            f'text-shadow:0 0 5px rgba(0,0,0,.9);user-select:none">{icon}</div>'
        )

    # ── Time labels ────────────────────────────────────────────────────
    labels_html = ""
    for h, m, lbl in [(9,30,"9:30"),(10,0,"10"),(11,0,"11"),(12,0,"12"),
                       (13,0,"13"),(14,0,"14"),(15,0,"15"),(16,0,"16")]:
        p = ((h * 60 + m) - START_MIN) / SPAN * 100
        if 0 <= p <= 100:
            labels_html += (
                f'<span style="position:absolute;left:{p:.1f}%;transform:translateX(-50%);'
                f'font-size:8px;color:#374151;white-space:nowrap">{lbl}</span>'
            )

    n   = len(diffs)
    d0  = diffs[start_idx] if diffs and start_idx < len(diffs) else {}
    raw = d0.get("ts_et", "")
    tm  = _re.search(r"T(\d{2}:\d{2})", raw)
    init_ts   = (tm.group(1) + " ET") if tm else raw
    init_cnt  = f"{start_idx + 1} / {n}"
    init_pct  = _pct(d0.get("ts_et", ""))

    btn = ("background:#111827;color:#d1d5db;border:1px solid #374151;"
           "border-radius:5px;cursor:pointer;font:11px/1 inherit;padding:5px 11px;")

    return f"""<div id="__tt_bar" style="position:sticky;top:0;left:0;right:0;z-index:999;
background:#070a0f;color:#d1d5db;
font:11px/1 'JetBrains Mono',ui-monospace,monospace;
border-bottom:1px solid #1a2535;box-shadow:0 2px 12px rgba(0,0,0,.8)">

  <!-- Row 1: Controls + timestamp -->
  <div style="display:flex;align-items:center;gap:8px;padding:5px 10px 4px">
    <span style="color:#f59e0b;font-weight:700;white-space:nowrap;font-size:11px;letter-spacing:.5px">&#9194; REPLAY</span>
    <button id="__tt_play" style="{btn}min-width:32px">&#9654;</button>
    <button id="__tt_spd"  style="{btn}font-size:10px;padding:4px 7px;color:#9ca3af">3&times;</button>
    <span id="__tt_ts" style="color:#60a5fa;flex:1;text-align:center;font-size:14px;font-weight:700;letter-spacing:.5px">{init_ts}</span>
    <span id="__tt_cnt" style="color:#374151;font-size:9px;white-space:nowrap">{init_cnt}</span>
  </div>

  <!-- Row 2: Session timeline — zones + event markers + cursor + invisible range input -->
  <div style="padding:0 10px 0">
    <div style="position:relative;height:28px;border-radius:3px;overflow:hidden;
                background:#0d1117;border:1px solid #1a2535">
      <!-- Zone backgrounds -->
      <div style="position:absolute;inset:0">{zones_html}</div>
      <!-- Event markers + kill dots -->
      <div style="position:absolute;inset:0;overflow:hidden">{events_html}
        <!-- Cursor: thick white bar, easy to grab/drag via the range input -->
        <div id="__tt_cursor" style="position:absolute;top:0;bottom:0;
          left:{init_pct:.1f}%;width:4px;
          background:rgba(255,255,255,0.95);
          box-shadow:0 0 10px rgba(255,255,255,0.7),0 0 3px rgba(255,255,255,1);
          border-radius:2px;
          z-index:13;transform:translateX(-50%);pointer-events:none"></div>
      </div>
      <!-- Invisible range input overlaid for drag interaction -->
      <input id="__tt_range" type="range" min="0" max="{n-1}" value="{start_idx}"
        style="position:absolute;inset:0;width:100%;height:100%;margin:0;
               z-index:14;cursor:pointer;opacity:0;
               -webkit-appearance:none;appearance:none">
    </div>
    <!-- Time labels -->
    <div style="position:relative;height:11px;margin-top:1px">{labels_html}</div>
  </div>

  <style>
    #__tt_range::-webkit-slider-thumb{{-webkit-appearance:none;width:2px;height:28px;
      background:transparent;margin-top:0;cursor:ew-resize}}
    #__tt_range::-moz-range-thumb{{width:2px;height:28px;background:transparent;
      border:none;cursor:ew-resize}}
    #__tt_range::-webkit-slider-runnable-track{{background:transparent}}
    #__tt_range::-moz-range-track{{background:transparent}}
  </style>
</div>"""


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

    # -- Build per-snapshot diff list. Includes all time-varying fields so
    # currentState() in the HEAD_PATCH can apply them on navigation.
    slim_diffs = [
        {
            "ts_et":             d["ts_et"],
            "captured_at_utc":   d["captured_at_utc"],
            "kind":              d.get("kind", ""),
            "label":             d.get("label", ""),
            # Core state fields (applied by currentState() in JS)
            "trades_today":      d["diff"]["trades_today"],
            "positions":         d["diff"]["positions"],
            "server_time":       d["diff"]["server_time"],
            "server_time_label": d["diff"]["server_time_label"],
            "eod":               d["diff"]["eod"],
            # Time-varying display fields (applied by extended currentState())
            "portfolio":          d["diff"].get("portfolio", {}),
            "regime":             d["diff"].get("regime", {}),
            "v10_activity":       d["diff"].get("v10_activity", []),
            "gates_scan_paused":  d["diff"].get("gates_scan_paused", False),
            "v10_kill_triggered": d["diff"].get("v10_kill_triggered", False),
            "v10_realized_pnl":   d["diff"].get("v10_realized_pnl",   0.0),
            "v10_admit_count":    d["diff"].get("v10_admit_count",    0),
            "v10_reject_count":   d["diff"].get("v10_reject_count",   0),
            "v10_day_states":     d["diff"].get("v10_day_states",     []),
        }
        for d in diffs
    ]

    # Suppress reconnect banner via CSS
    replay_css = """<style id='__tt_css'>
/* Replay bar is sticky in-page, no body top-margin needed */
#tg-replay-btn{display:none!important}  /* hide Replay Day btn in replay mode */
/* Hide static backtest baseline — no live data in replay */
#v10-baseline{display:none!important}
/* Hide empty ||| gauge placeholders in v10 ORB header */
#v10-day-status>.v10-gauge-head,
#v10-day-status>*:empty,
.v10-banner .v10-gauge:empty{display:none!important}
/* Remove redundant "main" portfolio badge from trade/position rows on Main tab */
.trade-row td[data-col="portfolio"] span,
tr[data-pos-ticker] .pos-portfolio-badge{display:none!important}
/* Scan-paused / kill banner: muted in replay — expected mid-day, not a crisis.
   Replace red with subtle slate so the operator eye isn't drawn to a false alarm. */
#banner:not(.hide){
  background:rgba(17,24,39,0.55)!important;
  border:1px solid #1e293b!important;
  box-shadow:none!important;
}
#banner:not(.hide) *{color:#64748b!important;}
#banner:not(.hide) strong,
#banner:not(.hide) b{color:#94a3b8!important;font-weight:600!important;}
#banner:not(.hide) .banner-pill,
#banner:not(.hide) [style*="#ef4444"],
#banner:not(.hide) [style*="#dc2626"]{
  background:#1e293b!important;color:#94a3b8!important;
  border-color:#334155!important;}
/* Remove error health pill — not used */
#tg-health-pill,#tg-health-pop{display:none!important}
</style>\n"""

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
